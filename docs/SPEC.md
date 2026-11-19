# SPEC: What we know about Duke Energy's API and login

Status: approved research, 2026-11-19. A first live check passed the same day
(§10). Before that, nothing in this repo had been run
against Duke's live API yet. Every claim below comes from the reference code,
its git history, forks, PRs and issues. Each claim names its source.

Labels used below:

- **Confirmed (multiple)**: several independent forks or users report it working.
- **Single source**: one fork or one user, not independently confirmed.
- **⚠ Uncertain** / **⚠ Contradictory**: evidence is weak or conflicting. See §9.

## 0. Research method and sources

- `reference/aiodukeenergy` at `ccddbe6` (v1.1.1) and
  `reference/ha-dukeenergy` at `3108a71` (v0.3.1), including full git history.
- Forks, fetched with `git fetch` and diffed against upstream `main`:
  - **aiodukeenergy:** `crash0verride11` (branch `aiodukeenergy-co`,
    published on PyPI as `aiodukeenergy-co` 1.3.2), `jameslb`, `b-v-co`,
    `jdobbsclt`, `babyhuey`, `flkns`, `chrisduk112`, plus older forks with no
    relevant changes.
  - **ha-dukeenergy:** `sam2kb` (v2.4.0), `crash0verride11`
    (`ha-dukeenergy-aio`), `jameslb`, `jdahmen`, `babyhuey`, `jojobird6`,
    `jdobbsclt`, `ndemarco`, `23brewert`, `flkns`.
- Every PR and issue on both upstream repos, including the comment threads of
  the auth-related ones.
- HA core's former `duke_energy` integration (tag `2025.10.0`, raw file), to
  cross-check statistic IDs.
- **Limitation:** `gh` returned HTTP 401 in this sandbox, so PR and issue
  metadata came from the unauthenticated REST API, and fork code came from
  `git fetch`. I did not read GitHub Discussions, including sam2kb's
  announcement, hunterjm/ha-dukeenergy#64.

## 1. Architecture at a glance

Login takes four hops. Two identity systems are involved: an Auth0 tenant, and
Duke's own API gateway.

```
Browser (must look like iPhone Safari)
  │ 1. GET https://login.duke-energy.com/authorize  (PKCE, iOS app client_id)
  │    user signs in (Universal Login, CAPTCHA/bot checks)
  ▼
  302 → https://login.duke-energy.com/ios/com.duke-energy.app/callback?code=…&state=…
        (a real HTTPS URL that renders "Not Found"; the code sits in the address bar)
  │
HA  │ 2. POST https://login.duke-energy.com/oauth/token   (code + code_verifier)
    │    → Auth0 access_token (24 h), id_token (30 min), refresh_token
    │ 3. POST https://api-v2.cma.duke-energy.app/login/auth-token  {"idToken": …}
    │    (Basic auth with the iOS app's API client id/secret)
    │    → Duke API access_token (~30 min, see §3.4)
    │ 4. GET/POST https://api-v2.cma.duke-energy.app/…   Bearer <Duke API token>
```

Akamai sits in front of `api-v2.cma.duke-energy.app`. Since 2026-10, something
in front of `login.duke-energy.com/authorize` also filters by client: desktop
browsers are rejected (§2.6).

## 2. Auth0 (login.duke-energy.com)

### 2.1 Client configuration (current: iOS app)

| Item | Value | Source |
|---|---|---|
| Authorize URL | `https://login.duke-energy.com/authorize` | upstream |
| Token URL | `https://login.duke-energy.com/oauth/token` | upstream |
| JWKS | `https://login.duke-energy.com/.well-known/jwks.json` | jameslb (used for ID-token validation) |
| Userinfo | `https://login.duke-energy.com/userinfo` | upstream (defined, unused in HA) |
| Issuer (`iss`) | `https://login.duke-energy.com/` | jameslb |
| client_id | `PitoKqxMh8thrFF8rRlYGrAs3LbSD2dj` | crash0verride11 (aiodukeenergy #17, ha-dukeenergy #22), merged upstream |
| redirect_uri | `https://login.duke-energy.com/ios/com.duke-energy.app/callback` | crash0verride11 |
| scope | `openid profile email offline_access` | upstream |
| response_type / mode | `code` / `query` | upstream |
| PKCE | `S256`, verifier = `token_urlsafe(32)` | upstream |
| nonce | random; upstream sends it but never checks it | upstream; jameslb verifies it |
| `auth0Client` query param | base64 of `{"name":"Auth0.swift","env":{"iOS":"27.0","swift":"6.x"},"version":"2.19.0"}` | crash0verride11 (aiodukeenergy #22, 2026-09-18) |

**⚠ Contradictory:** ha-dukeenergy `const.py` still sends the older
`auth0Client` (`iOS 26.2` / `2.13.0`). The library and its forks send `27.0` /
`2.19.0`. jdobbsclt tested both against the desktop-browser rejection and both
failed (ha-dukeenergy #62). So this value is not what that check keys on. It
does not appear to matter today.

### 2.2 Superseded client configurations (history)

| Period | Flow | Identifiers | Source | Fate |
|---|---|---|---|---|
| ≤ 2025-01 | OAuth password grant at `prod.apigee.duke-energy.app/gep/v2/auth-oauth2/token` (Android client `g3c4…`) | API base `…/gep/v2/auth-services/` | hunterjm v0.1 | Paths deprecated 2025-01 |
| 2025-01 → 2025-11 | Password grant at `api-v2.cma.duke-energy.app/login-services/auth-token` (iOS client `ShOncX…`) | | mrala (aiodukeenergy #8) | Credentials rolled with iOS app 6.17.0 (jasongill #12, not merged). Then Duke moved to Auth0 (Nov 2025) |
| 2025-12 | Attempt at scripted Universal Login (`/u/login/password`) with iOS v7.0 client `rNWiQ…` | | chrisduk112 fork | Never merged, no confirmation it worked. Upstream says automated login is blocked by CAPTCHA |
| 2026-01 → 2026-02-13 | Auth0 Android: client `uB67shrSSodJrNKTvZ0cWyAE9VRqkFwI`, redirect `cma-prod://login.duke-energy.com/android/com.dukeenergy.customerapp.release/callback`, Duke API client `M3NF…`, headers `platform: Android`, `okhttp/4.12.0` | | hunterjm (aiodukeenergy #13, v1.0.0) | Blocked 2026-02-13: auth-token returned `403 {"Message":"User is not authorized to access this resource with an explicit deny in an identity-based policy"}` (ha-dukeenergy #18, aiodukeenergy #16) |
| 2026-02 → now | Auth0 iOS (above) | | crash0verride11 | Current |

The Android path appears to be intentionally denied, not just stale. Nobody has
tried to revive it.

### 2.3 Authorization code exchange

```
POST https://login.duke-energy.com/oauth/token
Content-Type: application/json; charset=utf-8
accept-language: en_US
auth0-client: <same base64 as auth0Client>
User-Agent: Duke%20Energy/1374 CFNetwork/3896.100.1.2.1 Darwin/27.0.0

{"grant_type":"authorization_code","client_id":"PitoKq…","code_verifier":"…",
 "code":"…","redirect_uri":"https://login.duke-energy.com/ios/com.duke-energy.app/callback"}
```

These are the library's headers. ha-dukeenergy never uses the library for this
call. It uses HA's `LocalOAuth2ImplementationWithPkce`, which sends a
form-encoded body with HA's default User-Agent. That has worked for every HA
fork through 2026-11. **Confirmed (multiple):** the token endpoint accepts
either form, and it is not currently filtered by User-Agent.

Response (shape from hunterjm's test fixtures, and the lifetimes he stated in
ha-dukeenergy #4):

```json
{"access_token":"<JWT>","refresh_token":"…","id_token":"<JWT>",
 "token_type":"Bearer","expires_in":86400}
```

### 2.4 Token lifetimes and refresh

| Token | Lifetime | Evidence |
|---|---|---|
| Auth0 `access_token` | 24 h (`expires_in: 86400`) | hunterjm, ha-dukeenergy #4. **Not used for any Duke call**: the API never sees it |
| Auth0 `id_token` | 30 min (`exp` claim) | hunterjm, ha-dukeenergy #4 / PR #5. This token is the input to the Duke token exchange, so refresh is driven by **its** expiry |
| Auth0 `refresh_token` | ⚠ Unknown | See below |
| Duke API `access_token` | ~30 min assumed | `expires_in` default of 1800 in upstream code. ⚠ Not directly observed |

The refresh call has the same headers as §2.3, with body
`{"grant_type":"refresh_token","client_id":"PitoKq…","refresh_token":"…"}`. It
returns a new `id_token` and `access_token`, and may also return a new
`refresh_token`. Upstream keeps the old refresh token if no new one comes back.

**⚠ Uncertain: refresh-token lifetime and rotation.**

- hickey (ha-dukeenergy #38) reported forced re-auth "about every 2 weeks to a
  month" on v0.2.x/v0.3.0, after the 30-minute `id_token` bug was fixed. That
  suggests an absolute or inactivity lifetime on the refresh token. It could
  also be a client-side bug or a Duke-side revocation. No one measured it.
- Nobody has checked whether Auth0 refresh-token rotation is on for this
  client. If it is on, losing the newest refresh token (for example, a crash
  before it is persisted) forces re-auth.
- hunterjm's 2026-01 fix: ha-dukeenergy derives `expires_at` from the
  **id_token** `exp`, so HA refreshes every ~30 min. Before that, HA trusted the
  24 h `expires_in`, and the Duke exchange failed with
  `401 {"returnCode":"401","messageText":"Invalid token"}`.

### 2.5 ID token claims

These claims have been observed or used:

- `sub` (`auth0|…`)
- `email`
- `internal_identifier`: Duke's internal user ID. Upstream uses it,
  lowercased, as the config-entry unique ID, and passes it as `internalUserID`
  to `account/list`.
- `exp`, `iss`, `aud` (= client_id), `nonce`
- `de_auth`, `customer_type`, `experiences`, `flow_type`, `redirect_uri`. These
  appear in a commented-out debug log in crash0verride11's iOS refactor
  (`22afd33`). Their meaning is unknown.

The Duke token exchange response also carries `internalUserID`, which
overrides the claim if both are present (upstream).

### 2.6 Browser-side behavior of the authorize step

- **The callback URL renders "Not Found".** This is expected. The iOS redirect
  is a real HTTPS page on `login.duke-energy.com` with no content. The `code`
  and `state` are in the address bar (ha-dukeenergy #38, hickey). This is what
  makes the paste approach work (§8 and DESIGN.md).
- **Auth0 sometimes changes the URL with `history.pushState`** instead of a
  real redirect. The Chrome extension had to add `tabs.onUpdated` to catch it.
  A page refresh sometimes "fixes" the Not Found page for the extension
  (crash0verride11, aiodukeenergy #17; aiodukeenergy #16).
- **Desktop browsers are rejected (2026-10).** After a correct password, the
  callback comes back with an empty `code` and
  `error=access_denied&error_description=An error has occurred`. The same
  authorize URL works when the browser presents an iPhone Safari User-Agent
  (DevTools device emulation, or a UA-override rule). Found by jdobbsclt
  (ha-dukeenergy #62, aiodukeenergy #23) and verified by babyhuey (#24) and
  sam2kb. The UA in use:
  `Mozilla/5.0 (iPhone; CPU iPhone OS 18_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.6 Mobile/15E148 Safari/604.1`.
  jdobbsclt's rule also strips the `Sec-CH-UA*` client hints.
  **⚠ Uncertain:** does Duke key on the UA string alone, or also on client hints
  and viewport? Stripping the hints plus the UA was enough. DevTools emulation
  (UA plus viewport) was also enough. Nobody isolated the variables.
- **Rejected sign-ins can land on `www.duke-energy.com`** (the public site)
  instead of the callback (sam2kb, unreleased changelog entry).
- **Duke outages** make `/oauth/token` return 500 (ha-dukeenergy #55,
  2026-07-15).
- **Passkey and password logins both work** in the browser (PCjabber, #38;
  NukeThemTillTheyGlow, #18).
- **⚠ Uncertain:** on a real iPhone with the Duke app installed, could the
  callback be captured by the app as a Universal Link? Nobody has reported
  this. Same-domain navigations normally don't trigger Universal Links.
- **Authorization codes are short-lived and single-use.** The extension README
  says "usually within 5 minutes". The exact Auth0 lifetime for this tenant is
  not known.

## 3. Duke API token (api-v2.cma.duke-energy.app/login/auth-token)

### 3.1 Request

```
POST https://api-v2.cma.duke-energy.app/login/auth-token
Authorization: Basic base64(HO2JKfv2dVuXhLHhleDr1s6fgVlPduGxVBO6GaS3dDjE7Kp8:g4236o8ROFMD4JuVI4tsgLY7NiIEGXQgzzCnH9RiRrvFC6IN4KFg3A6dBmGIIuW6)
Content-Type: application/json; charset=utf-8
Accept: application/json
platform: iOS
User-Agent: Duke%20Energy/1374 CFNetwork/3896.100.1.2.1 Darwin/27.0.0

{"idToken": "<Auth0 id_token>"}
```

- The client ID and secret are the iOS app's. They come from crash0verride11
  (aiodukeenergy #17), and every fork uses the same values.
- `Accept: application/json` and the `charset=utf-8` content type were added in
  crash0verride11 `53baf1f`, described as a "potential first login fix" for
  setups that needed several reloads. The fix was later reported to work, but
  nobody isolated whether these headers were the cause.

### 3.2 Response

`{"access_token": "...", "expires_in": <s>, "issued_at": <ts>, "internalUserID": "...", "email": "..."}`
(field names from upstream code and test fixtures).

**⚠ Uncertain: `issued_at` units.**

- The pre-Auth0 endpoint returned **milliseconds** (v0.1 divided by 1000).
- hunterjm's 2025-03 change switched to **seconds**.
- The current code treats it as seconds and falls back to local time if it is
  missing.

If Duke sent milliseconds, the cached token would never look expired. A 401 on
the next call would then be the only recovery. Don't trust `issued_at`: compute
expiry from the local receive time.

### 3.3 Failure signatures seen in the wild

| Response | Meaning | Source |
|---|---|---|
| `401 {"returnCode":"401","messageText":"Invalid token"}` | id_token expired | hunterjm, ha-dukeenergy #4 |
| `403 {"Message":"User is not authorized … explicit deny in an identity-based policy"}` | Client credentials and platform denied (Android cut-off, 2026-02-13). Re-auth **cannot** fix it | ha-dukeenergy #18 |
| `403` **HTML** Akamai "Access Denied … errors.edgesuite.net" | Edge/WAF block. Triggered by a stale app User-Agent (2026-09-17). Re-auth **cannot** fix it | QbitPrime/dschoch1165, ha-dukeenergy #59, aiodukeenergy #16 |
| `500 {"errors":[{"error":"Login is disabled at this time"}]}` | Seen once during the 2026-03 transition | sirelewop14, ha-dukeenergy #18 |

**Confirmed (multiple), fixed 2026-09-18:** bumping the UA to
`Duke%20Energy/1374 CFNetwork/3896.100.1.2.1 Darwin/27.0.0` fixed the Akamai
block (crash0verride11, aiodukeenergy #22). QbitPrime first reported that it
did not, then corrected the report: their test had been loading the wrong
package.

Distinguishing edge blocks from auth failures by content type is
crash0verride11's `DukeEnergyBlockedError` (aio-co 1.3.0). jameslb
independently made the same split. The rule: a non-JSON body means an edge
block.

### 3.4 Lifetime

`expires_in` defaults to 1800 in upstream when absent. ⚠ Nobody has logged the
actual value. Upstream caches the token and re-exchanges 60 s before expiry.

## 4. Duke data API (https://api-v2.cma.duke-energy.app)

### 4.1 Common request headers

```
Authorization: Bearer <Duke API access_token>
Content-Type: application/json; charset=utf-8
Accept: application/json
platform: iOS
User-Agent: Duke%20Energy/1374 CFNetwork/3896.100.1.2.1 Darwin/27.0.0
```

The Akamai rule applies to every call, not just auth-token. The UA bump touched
all call sites (crash0verride11, #22).

### 4.2 Account list: `GET /account/list`

- **Moved 2026-10-28** from `GET /account-list`. The old path now returns
  `403 "Invalid key=value pair (missing equal-sign) in Authorization header"`.
  That is an AWS API Gateway "no such route" message, not an auth error.
  Found by b-v-co (aiodukeenergy #25), and independently by crash0verride11
  (`7aabd37`), jameslb and sam2kb (2.3.5).
- Query: `email`, `internalUserID`, `fetchFreshData=true`.
- Response:
  ```json
  {"accounts":[{"accountNumber":"…","primaryBPNumber":"…",
                "serviceAddressParsed":{"zipCode":"…","addressLine1":"…","city":"…","state":"NC"},
                "srcSysCd":"…?","srcAcctId":"…?","srcAcctId2":"…?", …}],
   "relatedBPNumber":"…"}
  ```
  The new route spells `BP` in capitals (`primaryBPNumber`, `relatedBPNumber`)
  and may omit `srcSysCd`, `srcAcctId` and `srcAcctId2`. The forks normalize:
  accept either BP spelling, and default `srcSysCd → "ISU"`,
  `srcAcctId → accountNumber`, `srcAcctId2 → ""`.
- **⚠ Contradictory:** the forks disagree on which BP spelling wins when both
  are present. b-v-co prefers the old `Bp`; crash0verride11 and sam2kb prefer
  `BP`.
- **⚠ Uncertain:** b-v-co verified the `"ISU"` and `accountNumber` defaults on
  one residential electric account (Duke Energy Progress, NC) only. sam2kb added
  synthetic multi-account and gas tests. Nobody verified other jurisdictions.

### 4.3 Account details: `GET /account-details-v2`

- Query: `email`, `srcSysCd`, `srcAcctId`, `primaryBpNumber`,
  `relatedBpNumber`. The query keys keep the old `Bp` spelling (all forks).
- Response (relevant part):
  ```json
  {"meterInfo":[{"serviceType":"ELECTRIC","serialNum":"…",
                 "agreementActiveDate":"YYYY-MM-DD","agreementEndDate":"YYYY-MM-DD",
                 "meterCertificationDate":"YYYY-MM-DD","isCertifiedSmartMeter":…}, …]}
  ```
- `serviceType` is `ELECTRIC` or `GAS`. Other values are skipped.
- **`serialNum` is the meter key, and the statistic IDs are built from it.**
  See COMPATIBILITY.md.

### 4.4 Usage graph: `POST /account/usage/graph`

**GET → POST** happened around 2026-03. crash0verride11, aiodukeenergy #17:
"Recent changes … silently causing a 403. Corrected GET to POST." Before
2025-01 this was `GET /auth-services/energy-usage-graph`.

Request body (JSON):

```json
{
  "srcSysCd": "…", "srcAcctId": "…", "srcAcctId2": "",
  "meterSerialNumber": "<serialNum>", "serviceType": "ELECTRIC",
  "intervalFrequency": "HOURLY",          // HOURLY | DAILY | MONTHLY
  "periodType": "DAY",                    // DAY | WEEK | BILLINGCYCLE | YEAR
  "date": "2026-11-18T14:03:27.123-05:00",// see ⚠ below
  "agrmtStartDt": "MM/DD/YYYY", "agrmtEndDt": "MM/DD/YYYY", "meterCertDt": "MM/DD/YYYY",
  "startDate": "MM/DD/YYYY", "endDate": "MM/DD/YYYY",
  "zipCode": "…"
}
```

The pre-POST version also sent `includeWeatherData` and `showYear`.
crash0verride11 dropped them as unnecessary, since weather data comes back
either way.

**`date` field: ⚠ contradictory, and the subject of several bug reports.**

- Duke's app sends the *selected* date combined with the *current* time of day.
  crash0verride11 captured this and added it in 1.0.1 (#15).
- Upstream today:
  - HOURLY: startDate's Y-M-D plus the current time.
  - DAILY: startDate's Y-M plus the current day, minus 1 day. This crashes when
    today's day number doesn't exist in startDate's month (aiodukeenergy #19,
    "day 29 must be in range 1..28").
- crash0verride11's later capture (PR #20, open) says Duke now uses the full
  start date for DAILY too.
- jameslb deliberately kept the old behavior pending "stronger evidence".
- For MONTHLY (crash0verride11 only): now minus 1 day.

**For our scope (HOURLY electric), all sources agree:** startDate's date plus
the current time of day.

**Window size.** Upstream requests up to 31 days per call (`startDate` to
`endDate`) with `HOURLY`/`DAY`, so `periodType` does not limit the range. 37
calls cover 3 years. No fork reports a cap on window size, but nobody tested
anything larger than 31 days.

Response:

```json
{"usageArray":[{"date":"12 AM","usage":"0.69","temperatureAvg":30}, …]}
```

- `usage` is a **string**. `temperatureAvg` may be a number or a numeric string
  (sam2kb 2.4.0).
- HOURLY rows are labeled only `"%I %p"` (`"12 AM"`, `"01 AM"`, … `"11 PM"`).
  **The rows carry no date.** Each row's timestamp has to be inferred from its
  position, counting from `startDate` 00:00 local time.
- DAILY rows are labeled `M/DD/YYYY`: month without a leading zero, day with
  one, for example `"2/05/2026"`. crash0verride11, #15.
- MONTHLY/YEAR (crash0verride11): billing-cycle rows that include `endDate`.

**Row anomalies** (each one broke a parser at some point):

1. **Missing hours.** Some hours are simply absent: the label sequence skips
   one (hunterjm `5bd3836`, 2024-09). Upstream detects this by comparing
   labels to the expected label and shifting an offset.
2. **DST fall-back.** ⚠ **Changed over time:**
   - **2024-11-03, the older `TickSeries` response:** 25 rows, with `"01 AM"`
     twice (DallasEpperson, aiodukeenergy #6/#7). Upstream since `ebfeaab`
     (2025-03) skips the second `01 AM`, which dropped one real hour of
     energy every November.
   - **2026-11-01, today's `usageArray` response (live, §10):** **24 rows
     with a single `"01 AM"`.** Duke leaves out the repeated hour. Its value
     (0.27 kWh, next to 0.26 on each side) is one hour's usage, not two
     summed.

   We place the single `01 AM` on the first 1 AM (EDT) and treat 01:00 EST
   as missing, consistent with what the old integration stored. The older
   25-row shape is still accepted.
3. **DST spring-forward** (2026-03-08, live, §10): 23 rows with no
   `"02 AM"`, as presumed.
4. **Short arrays.** The response can have fewer rows than the window implies,
   for example when the window reaches past the data horizon. Upstream crashes
   with `IndexError`. Fixed by crash0verride11 (`d642826`, PR #20) and jameslb
   (`3bc44f9`).
5. **Zero usage** is treated as "missing" by upstream (`not usage > 0`). ⚠ We
   can't tell whether Duke uses `"0"` for "no data yet" or for "real zero".
   Net-metering or solar customers could have real zeros, or negative values,
   which are also dropped. Nobody has reported either way.
6. **A whole missing day is undetectable** by upstream's label matching: the
   24-hour label cycle repeats, so every later row silently shifts by a day.
   ⚠ Nobody has reported this happening. It is a structural weakness of the
   positional format. DESIGN.md covers the mitigation.

**History depth.** "Duke Energy provides hourly data all the way back to ~3
years" (comment in HA core's coordinator, carried into ha-dukeenergy).

- ⚠ This is unverified. Nobody has reported the actual horizon.
- History is also bounded below by the meter's `agreementActiveDate`.
- How Duke signals "no more history" is unknown. Upstream treats **any**
  `ClientError` or timeout as the end of history. sam2kb 2.3.4 narrowed that
  to 4xx other than 408/429, and lists the question as open.

**Data latency.**

- Yesterday's data usually appears by about 7–8 AM ET (mat1990dj,
  ha-dukeenergy #15).
- Upstream polls every 12 h.
- crash0verride11 and sam2kb poll at 9:00, 14:00 and 19:00 ET, each shifted by
  a per-entry, per-day random ±2 h offset so users don't all hit Duke at once.

**Timezone.** All parsers assume `America/New_York` for every account: "All of
Duke Energy Service Areas are currently in America/New_York", in core and every
fork. ⚠ This is an assumption. Duke Energy Indiana and Duke Energy Florida
border Central-time areas. No reports either way.

### 4.5 Other endpoints (not needed for consumption statistics)

| Endpoint | Purpose | Source |
|---|---|---|
| `POST /account/usage/comparison` (was `/account/usage/monthly` until 2026-10-29) | This / last / last-year period summary. Now keyed by `accountNumber` instead of the src IDs | crash0verride11 `bcf28b0` |
| `GET /invoice-list?srcSysCd&srcAcctId` | Invoices, with `billEndDate` | crash0verride11 |
| `GET /billing-and-payment/multi-account-payment/info-v3?includeClosedAccounts=1` | Balance, due date, bill status | crash0verride11 |
| Usage graph with `DAILY`/`WEEK` on a GAS meter | Gas usage in CCF, daily only | crash0verride11 (#27), ndemarco (#25), 23brewert, flkns. crash0verride11 says the app UI gates gas to DAILY |

The API exposes no per-interval cost (`usageArray` has only `usage` and
`temperatureAvg`). Every cost feature in the forks (jojobird6 #54,
crash0verride11, sam2kb, jameslb, jdobbsclt) multiplies usage by a rate the
user configures.

## 5. Rate limiting and edge behavior

- **No documented rate limit.** babyhuey (#63) and sam2kb cap concurrency at 3
  with 0.5 s between batches because "Duke's edge has been blocking requests
  that don't look like the mobile app". That is a precaution, not an observed
  limit.
- **Akamai blocks are keyed on User-Agent** (confirmed for an outdated app
  version). QbitPrime speculated about IP reputation and request rate, then
  retracted it when the UA fix worked.
- **Expect the UA floor to move again** whenever Duke raises its minimum app
  version.

## 6. Timeline of breakages (for pattern recognition)

| Date | What broke | Fix | By |
|---|---|---|---|
| 2024-09 | Missing hours in series | Label-check offset | hunterjm |
| 2024-11 | DST duplicate hour shifted data by 24 h | Skip duplicate (drops an hour) | DallasEpperson (PR #7, unmerged); hunterjm (`ebfeaab`) |
| 2025-01 | Apigee paths deprecated | Move to `api-v2.cma.duke-energy.app` | mrala (#8) |
| 2025-03 | Usage response reshaped (`usageArray`) | New parser | hunterjm (#9) |
| 2025-11 | Credentials rolled, then Auth0 + CAPTCHA | Browser login, Chrome extension | jasongill (#12), hunterjm (#13) |
| 2026-01 | id_token expires in 30 min, re-auth every 12 h | Expiry from id_token `exp` | hunterjm (ha #5) |
| 2026-02-13 | Android client denied | iOS client and redirect | crash0verride11 (#17, ha #22) |
| 2026-03 | Usage graph GET → POST; daily `date` format | POST; date construction | crash0verride11 |
| 2026-03-29 | `date` crash on Feb 29/30/31 | ⚠ Unmerged (PR #20) | crash0verride11 |
| 2026-09-17 | Akamai blocks stale app UA | UA bump | crash0verride11 (#22) |
| 2026-10 (~18) | Desktop browsers rejected at authorize | iPhone UA in browser | jdobbsclt (#23) |
| 2026-10-28 | `account-list` retired | `account/list` + normalization | b-v-co (#25), crash0verride11, jameslb, sam2kb |
| 2026-10-29 | `usage/monthly` retired | `usage/comparison` | crash0verride11 |

**Pattern:** something breaks every 1–3 months. Most breaks show up as a 403 or
an HTML page, not as a clean auth error. The upstream code sent those to the
re-auth flow, which can't fix them. That cost users repeated pointless logins
(QbitPrime, ha #59).

## 7. Home Assistant–side facts relevant to the API

- **Statistic ID format (upstream and core):**
  `duke_energy:{serviceType.lower()}_{serialNum}_energy_consumption`. See
  COMPATIBILITY.md.
- **Upstream (core and hunterjm) clears all its statistics on every config-entry
  unload** (`async_on_unload(self._clear_statistics)`): on reload, on disable,
  and on delete. The next start then re-backfills ~3 years with the sum starting
  at 0. crash0verride11 removed this (`76e4ebc`, 2026-07). sam2kb now clears
  only when the last entry is removed.
- The Chrome extension's HA integration: if `state` is a JWT containing
  `flow_id`, the extension redirects to
  `https://my.home-assistant.io/redirect/oauth?code=…&state=…`. That puts
  Duke's callback into HA's normal OAuth external-step flow.

## 8. Approaches to the login step that have been tried

| Approach | Who | Status |
|---|---|---|
| Chrome extension that intercepts the callback (`webNavigation`, `tabs.onUpdated`) and forwards to my.home-assistant.io | hunterjm, crash0verride11 | Upstream default. Since 2026-10 also needs the UA rule (jdobbsclt #23) |
| Safari extension (Xcode conversion) | crash0verride11 (#18) | Unmerged, macOS only |
| Generic "OAuth assist" extension or config | crash0verride11 (2026-10-30, `resources/duke_ha-oauth-assist.json`) | Fork only |
| Paste the full callback URL into the HA config flow | jameslb (2026-08-15, ha #56: PKCE, state check, RS256 + nonce validation), jdahmen (2026-09-16, ha #57), sam2kb (2.2.1, 2026-10-19; extension removed in 2.3.0), crash0verride11 (2026-10-30, alongside the extension) | **Converged on by every active fork** |
| Scripted Universal Login (username + password) | chrisduk112 (2025-12) | Never confirmed. CAPTCHA blocks it |

## 9. Open questions (need a live account to settle)

1. Refresh-token lifetime (absolute and inactivity) and whether rotation is on.
2. The actual `expires_in` of the Duke API token, and the units of `issued_at`.
3. How Duke signals the start of history (status code and body), and how deep
   history really goes.
4. Does `"0"` usage mean no data or real zero? Are negative values possible
   (net metering)?
5. ~~The row count on DST days.~~ Answered live on 2026-11-19 (§10):
   spring-forward has 23 rows, fall-back has 24.
6. Whether a whole day can be missing from an hourly series.
7. Whether the DAILY `date` construction changed (PR #20 vs jameslb). This only
   matters if gas is ever in scope.
8. Which BP-number spelling is authoritative when both are present. Whether the
   `ISU` default is right outside Duke Energy Progress, NC.
9. Whether the desktop-browser check needs the UA only, or the UA plus client
   hints.
10. Whether any Duke service area is outside `America/New_York`.
11. The largest usable window per usage request. 31 days works (§10); larger
    windows are untested.

## 10. Live verification log

Checks run with `scripts/live_check.py` against a real account. Record new
runs here, with date and result, and never with identifying details.

| Date | What was checked | Result |
|---|---|---|
| 2026-11-19 | Paste-callback sign-in in a desktop browser with iPhone emulation; Auth0 code exchange (JSON body, app UA `Duke%20Energy/1374 …`); `login/auth-token` with the iOS API client; `account/list`; `account-details-v2`; one `HOURLY`/`DAY` usage request covering 3 days (EST, no DST change) | All succeeded. 1 account, 1 electric meter. The usage response had exactly 72 rows whose labels matched the expected sequence one-for-one: no missing hours, no invalid values. The meter's `serialNum` produced the maintainer's existing statistic ID unchanged. |

| 2026-11-19 | One 31-day `HOURLY`/`DAY` request, 2026-10-19..2026-11-18, which spans the fall-back day | Accepted. That version of the parser expected 25 rows for 2026-11-01, so the window didn't line up; the day-by-day re-fetch placed 30 days (720 hours) and rejected 2026-11-01. Fixed by the next row. |
| 2026-11-19 | Raw rows for 2026-11-01 (fall-back) and 2026-03-08 (spring-forward), via `live_check.py --day` | Fall-back: **24 rows**, one `"01 AM"`. Spring-forward: **23 rows**, no `"02 AM"`. The parser now expects these shapes, so a 31-day window spanning either day lines up in a single request. |

Still unverified live: §9 questions 1–4, 6 and 8–10.
