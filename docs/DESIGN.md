# DESIGN: duke_energy custom integration (rewrite)

Status: approved, 2026-11-19. It builds on [SPEC.md](SPEC.md) (what
Duke's API does) and [COMPATIBILITY.md](COMPATIBILITY.md) (how existing
statistics are continued).

## 1. Goals and non-goals

### Goals

1. **Import hourly electric consumption** from Duke Energy into HA long-term
   statistics, continuing the existing series unbroken.
2. **Make login survivable.** Use the fewest moving parts that can break, and
   give a clear message when something does break.
3. **Make Duke-side breakage cheap to diagnose and fix.** Duke breaks something
   every 1–3 months (SPEC §6), so the code is organized around that.
4. **Install through HACS**, with domain `duke_energy` and no extra Python
   requirements.

This is a from-scratch rewrite. The reference code and forks are a record of
Duke's behavior, not a template. Nothing here keeps their structure,
dependencies or tooling unless it was chosen on its own merits.

The integration is generic: any Duke customer can install it. No account,
meter or statistic ID is hard-coded. Statistic IDs are built at runtime from
Duke's data.

### Non-goals for v1

- **Gas.** Decided: electric only.
- Cost, temperature, billing and bill-cycle sensors.
- Any entities. Statistics are the product, as in the old integration.
- Non-Eastern timezones. `America/New_York` stays an isolated constant, so it
  can become per-account later.

## 2. Login and token capture

### 2.1 Constraints

These constraints rule out the obvious approaches.

- **Fixed redirect URIs.** Duke's Auth0 tenant only accepts the mobile app's
  registered redirect URIs. The current one is
  `https://login.duke-energy.com/ios/com.duke-energy.app/callback`. HA's own
  redirect (`my.home-assistant.io/redirect/oauth`) can't be registered.
- **Bot checks.** Sign-in is Auth0 Universal Login with CAPTCHA and bot
  checks. Scripted credential login is not viable.
- **Mobile browsers only.** Since 2026-10 the authorize step rejects desktop
  browsers. The browser must present an iPhone Safari User-Agent.
- **The redirect page is visible.** The redirect target is a real HTTPS page
  that renders "Not Found". Its URL, which contains `code` and `state`, is in
  the address bar.

### 2.2 Alternatives considered

**A. Browser extension** (status quo upstream; crash0verride11's variants).

An unpacked Chrome extension watches for the callback URL. It forwards
`code`/`state` to `my.home-assistant.io/redirect/oauth`, and HA's standard
OAuth external step finishes. Since 2026-10 the extension also needs a
`declarativeNetRequest` rule that spoofs an iPhone UA (aiodukeenergy #23).

*Pros*
- After install, the login is close to one click.
- It reuses HA's stock OAuth2 flow.

*Cons*
- **A second artifact** to build, version and ship. Unpacked extensions need
  Developer Mode and **don't auto-update**. Every Duke change so far has meant
  users manually reinstalling (ha #18, #38, #62).
- **Desktop Chromium only.** The Safari port needs Xcode (#18). Firefox needs
  yet another build.
- **Depends on My Home Assistant redirects** and a correctly configured HA URL.
- **Fragile.** It broke on Auth0's `pushState` change, showing the "Not Found"
  page, and again on the desktop-UA check, showing "No code captured".
- **Hard to test.** Its logic lives in JavaScript outside our test suite.

**B. Paste the callback URL** (jameslb ha #56, jdahmen ha #57, sam2kb 2.2.1+;
crash0verride11 offers it alongside the extension). *Recommended.*

The config flow shows a sign-in link and instructions. The user signs in in
any browser that presents a mobile UA. When the "Not Found" page appears, they
copy its full address and paste it into the form. HA validates the URL and
exchanges the code.

*Pros*
- **No extra software.**
- **Works in any browser on any device.** On a phone, no UA change is needed.
- **No dependency on my.home-assistant.io.**
- **All logic is Python** and fully unit-testable.
- **Every active fork has converged on it independently.**
- **Small surface for Duke changes.** A change can only affect the URL we
  generate and how we parse the callback.

*Cons*
- **A manual copy-paste.**
- **Desktop users must turn on device emulation** (one extra instruction; see
  §2.4).
- **Codes expire in minutes,** so a slow user has to start over. The form
  handles this with a fresh link on failure.

**C. HA's native OAuth external step, without an extension.** *Impossible:*
Duke won't redirect to HA (constraint 1). Rejected.

**D. Scripted login with stored username and password** (chrisduk112's
2025-12 attempt).
- Blocked by CAPTCHA and bot detection.
- Means storing the user's Duke password.
- Would break on any change to the login page.

Rejected.

**E. Bookmarklet.** A `javascript:` bookmark the user clicks on the "Not
Found" page, which sends `location.href` to HA.
- It saves the copy-paste, but still needs UA emulation and a reachable HA URL.
- It is awkward on mobile.
- It can be layered on B later, since B's backend accepts the same data.

Optional later.

**F. Headless browser (Playwright) in HA or an add-on.**
- Heavy.
- Fights the same bot detection as D.
- Can't solve a CAPTCHA unattended.

Rejected.

**G. Paste tokens captured from the mobile app** (MITM proxy). Expert only, and
unnecessary when B works. Rejected.

### 2.3 Recommendation: B, with our own OAuth handling

- **A plain `ConfigFlow`**, not `AbstractOAuth2FlowHandler`. The forks bend
  HA's OAuth framework into a paste flow. They do it by synthesizing
  `external_data` with a decoded HA state JWT, and by patching
  `expires_at` to the id_token's `exp`. This design owns the flow instead.

**Flow (`user`, `reauth_confirm`, and `reconfigure` all share one step):**

1. **Create a transaction.** On entering the step, generate a PKCE verifier and
   challenge (S256), a random `state` and a random `nonce`. Keep them in the
   flow handler instance only; nothing is persisted. Build the authorize URL
   (SPEC §2.1).
2. **Show the form.** The description includes:
   - the sign-in link;
   - the iPhone UA string to set (desktop only);
   - one sentence explaining that the "Not Found" page is expected and its
     address is what to copy.

   The form has one field, the callback URL.
3. **Validate the paste.**
   - Host `login.duke-energy.com`, path `/ios/com.duke-energy.app/callback`,
     scheme `https`.
   - Exactly one `state`, matching ours.
   - **`error=` present:** show `authorize_rejected`, which explains that
     this almost always means the desktop UA, and tells the user to turn on
     device emulation.
   - **Host `www.duke-energy.com` or `duke-energy.com`:** show
     `public_site_redirect`, with the same advice. This detection is sam2kb's.
   - **No `code`:** show `invalid_callback_url`.
   - **State mismatch:** show `state_mismatch`, which means the URL came from a
     different attempt.
4. **Exchange the code** at `/oauth/token`. If that fails because the code
   expired or was already used (`invalid_grant`): regenerate the transaction,
   re-render the form with a new link, and show `code_expired`.
5. **Check the ID token.** Decode it without signature verification. Check:
   - `iss = https://login.duke-energy.com/`;
   - `aud = client_id`;
   - `nonce` = ours;
   - `exp` is in the future.

   Signature verification is skipped deliberately. OIDC Core §3.1.3.7 lets a
   client rely on TLS when the ID token comes straight from the token endpoint.
   jameslb's JWKS check adds a network request and a failure mode for no
   practical gain here. (If you want it anyway, it's about 30 lines.)
6. **Validate against Duke before saving:**
   - exchange the id_token at `/login/auth-token`;
   - call `/account/list`.

   An edge block or a credential revocation therefore shows up *in the form*
   (`blocked`, `client_rejected`). It does not show up as a broken entry
   after setup.
7. **Create or update the entry.**
   - `unique_id = internal_identifier.lower()`, the same as the old integration.
   - Title: the email address.
   - Reauth or reconfigure aborts with `wrong_account` if the unique ID differs.

### 2.4 User instructions (shown in the flow and the README)

- **Desktop Chrome / Edge:** open DevTools (F12), toggle the device toolbar
  (Ctrl/Cmd+Shift+M), and pick any iPhone. Keep DevTools open while you click
  the link.
- **Firefox:** Responsive Design Mode (Ctrl/Cmd+Shift+M), then pick an iPhone.
- **Safari (macOS):** Develop → User Agent → Safari — iOS.
- **Phone:** just open the link. To copy the address, long-press the address
  bar on iOS or tap and copy on Android.

  ⚠ Duke's check has only been confirmed with an iPhone UA (SPEC §2.6). An
  Android Chrome UA is unverified, so the README says "if you see an error on
  Android, use an iPhone UA or a desktop with emulation".

## 3. Tokens: storage, refresh, re-auth

### 3.1 Why not HA's `OAuth2Session`

`OAuth2Session` handles expiry, refresh locking and persistence. Here it is a
poor fit:

- **Wrong expiry.** It schedules refresh from `expires_at` of the *access*
  token. We need the *id_token* expiry. Every fork patches this.
- **No control over the refresh request.** It sends a form-encoded body with
  HA's default User-Agent. That works today (SPEC §2.3). But if Duke starts
  filtering `/oauth/token` the way it filters the API and the authorize page,
  we'd have to replace the helper during an outage.
- **Unclassified failures.** Refresh failures arrive as bare
  `ClientResponseError`. We need to tell `invalid_grant` (re-auth) apart from
  5xx, HTML or WAF responses (retry).
- **Coupled to OAuth registration.** It drags in
  `auth_implementation` registration, which exists for the redirect-based flow
  we don't use.

A dedicated `TokenManager` is about 150 lines. It holds an `asyncio.Lock` and a
persist callback.

### 3.2 What's stored

The config entry, version 3, stores:

```python
entry.data = {
    "refresh_token": str,
    "id_token": str,  # cache only, so a restart within 30 min skips a refresh
    "id_token_exp": float,
    "internal_identifier": str,
    "email": str,
}
```

The Duke API token lives **in memory only**. It lasts about 30 minutes and is
cheap to get again.

### 3.3 Lifecycle

- **`async_get_id_token()`.** If `id_token_exp − now < 5 min`, refresh at
  Auth0. If the response includes a new `refresh_token`, **persist it before
  returning** (`hass.config_entries.async_update_entry`). This guards against
  refresh-token rotation (SPEC §2.4, unknown): losing the newest token would
  force a re-auth.
- **`async_get_api_token()`.** Use the cached Duke token unless it is missing
  or within 60 s of expiry. Expiry is computed as **local receive time +
  `expires_in`**, defaulting to 1800. `issued_at` is ignored because its units
  are unknown (SPEC §3.2). Otherwise, exchange the current id_token.
- **401 from a data call.** Drop the Duke token, re-exchange once (this forces
  an id_token refresh if needed), and retry the call once. A second 401 means
  re-auth.
- **Keep-alive.** Refresh Auth0 tokens at least every 24 h, even when no data
  fetch is needed. The refresh token's inactivity lifetime is unknown;
  hickey's 2–4-week re-auths (ha #38) may be exactly that.
- **Re-auth.** `ConfigEntryAuthFailed` makes HA start the reauth flow
  (`reauth_confirm`, then the paste step). On success the entry data is updated
  and the entry is reloaded.
- **Migration from the old entry (v2).** If `data["token"]["refresh_token"]`
  exists, lift it into the v3 shape. The first setup then tries a refresh. If
  the token is dead, that becomes a normal re-auth. The migration never
  touches statistics.

### 3.4 Keeping the app User-Agent current (decided, §10 Q3)

**What the value is.** Every request to Duke's API identifies itself as the
Duke iOS app, for example
`Duke%20Energy/1374 CFNetwork/3896.100.1.2.1 Darwin/27.0.0`. That string
contains three parts:

- the app's build number (`1374`);
- Apple's networking library version (`CFNetwork/…`);
- the iOS kernel version (`Darwin/…`).

Duke's CDN rejects builds below a minimum that it raises from time to time
(SPEC §3.3, §5).

**It can't be derived automatically.**

- Duke's website doesn't publish the value.
- Apple's App Store lookup API returns only the marketing version (like
  "7.x"), not the build number or the CFNetwork and Darwin versions.
- Guessing the format would be fragile.

So a new value always comes from a person: someone captures the app's real
traffic (as crash0verride11 did in 2026-09) and posts it.

**What we can automate is distribution,** so nobody waits for a release.
The integration resolves the User-Agent in three layers. Higher layers win:

1. **User override.** An advanced options field. Most users never touch it.
   Setting it bypasses the remote layer entirely.
2. **Remote "known-good" value.** A small `user-agents.json` on this repo's
   `main` branch, fetched from `raw.githubusercontent.com`:
   - **When:** once a week, and immediately (at most once an hour) after a
     `BlockedError`.
   - **Validation:** a strict regex for the `Duke%20Energy/<n> CFNetwork/<v>
     Darwin/<v>` shape. Anything else is ignored.
   - **Storage:** cached in HA's `Store`.
   - **Content:** the same file can carry the iPhone browser UA shown in the
     login instructions.
3. **Built-in default** in `api/const.py`, updated in normal releases.

**The result:** when Duke blocks the old value, the maintainer commits a new
string to `user-agents.json`, and every install recovers within about an
hour, without a HACS update.

**Costs:**

- **One HTTPS request a week to GitHub.** The README discloses it, and it can
  be turned off in options. When it's off, the integration makes no GitHub
  requests and uses the override or the built-in default.
- **Someone still has to supply the new value.**
- **A compromised repo could push a header value.** The regex limits that
  value to a harmless UA string.

## 4. Data fetch and statistics import

### 4.1 Scheduling

**When to fetch.** A coordinator ticks every hour. Each tick is a local
decision; it makes no request unless one is needed:

```
need_data = last_stored_hour(meter) < yesterday 23:00 ET  (for any meter)
eligible  = now_ET >= 07:00 + jitter(entry_id, date)      (jitter 0–90 min, deterministic)
backoff   = after a failed attempt, wait 1h, 2h, 4h … (cap 6h) before trying again
```

- **Fetch** when `need_data and eligible and not backing_off`.
- **Otherwise** only do the 24 h token keep-alive if it's due.

Once yesterday is complete, the integration sends nothing to Duke until the
next morning. In a typical day that is one fetch, or a few if Duke is late.
Each fetch is about 5 requests:

- token refresh;
- Duke token exchange;
- `account/list`;
- account details (cached for 24 h);
- one usage request per meter.

The deterministic jitter is crash0verride11's idea. It spreads users out
without random behavior that would be hard to test.

**Setup must not block on a backfill.** `async_setup_entry` does a cheap
validation (token plus `account/list`), raising `ConfigEntryNotReady` or
`ConfigEntryAuthFailed` as appropriate. It then schedules the first fetch in
the background (crash0verride11's `e3ae0df`). A 3-year backfill is about 37
requests, about a minute at our pacing.

### 4.2 Requests

- **Hourly windows:** at most 31 calendar days, `HOURLY`/`DAY`, with `date` set
  to startDate's date plus the current local time of day (SPEC §4.4).
- **Strictly sequential**, 1 s apart during backfills. No concurrency. Backfills
  are rare (migration, or long outages), and the WAF history argues for
  caution over speed (babyhuey and sam2kb settled on 3-wide; we don't need it).
- **Order:**
  - *Forward fill from an anchor* (the normal case and migration):
    oldest-first, so a failure leaves a contiguous prefix (COMPATIBILITY §3.2,
    rule 6).
  - *Fresh backfill with no anchor:* probe newest-first to find the history
    boundary, buffer the results in memory, then import oldest-first. Three
    years of hourly data is about 26k floats.
- **History boundary.** During newest-first probing only, a 4xx other than
  401, 403, 408 or 429 on a window *older than a window that succeeded* is
  treated as the boundary. Any other failure is a retryable error. This is
  narrower than upstream, which treats any error as the boundary. That
  upstream behavior is what made a rejected token silently truncate history
  (sam2kb 2.4.0).

### 4.3 Parsing hourly rows

This is a pure function. Hourly rows carry no date (SPEC §4.4), so the parser
validates strictly instead of guessing.

1. **Build the expected sequence.** Step one UTC hour at a time from local
   midnight on `startDate` to local midnight after `endDate`. Each step's label
   is `local.strftime("%I %p")`. This naturally gives 25 labels on fall-back
   day (two `01 AM`, EDT then EST) and 23 on spring-forward day.
2. **Fast path:** if Duke's labels equal the expected labels exactly, zip them
   and use each UTC start directly. The fall-back duplicate maps to
   01:00 EST. That fixes the old parser's lost hour.
3. **Mismatch on a multi-day window:** re-request that window **one day at a
   time**. Within a single day, labels are unique apart from the DST pair. So
   matching Duke's rows as an ordered subsequence of the expected labels is
   unambiguous: skipped labels are missing hours. Any row that can't be placed
   rejects that day. The day is logged and treated as missing.

   This closes the "whole missing day silently shifts everything" hole (SPEC
   §4.4, anomaly 6). The cost is up to 31 extra requests, only when an anomaly
   occurs.
4. **Values:**
   - Parse `usage` as a float.
   - **NaN, inf or negative:** reject the reading and log a warning once per
     meter. Negative values stay rejected until we know Duke's net-metering
     semantics (SPEC §9 Q4).
   - **Zero is real data**, with one exception. A trailing run of zeros at the
     end of the newest day in the response is *deferred*: not written this
     poll, re-fetched next poll. A late-publishing day then never gets
     recorded as zeros. Real zeros become interior zeros once the next day
     arrives, and get written then. (The old code dropped every zero.)

### 4.4 Statistics import

The import implements COMPATIBILITY §3 exactly. It is split into two parts:

- **`statistics.plan_import(anchor, stored_rows, readings) → list[StatisticData]`**
  is pure. It applies the anchor, suffix, coverage and no-gap rules, and the
  drift check.
- **A small recorder adapter** reads `get_last_statistics` and
  `statistics_during_period` in the recorder executor, checks the metadata
  guard, and calls `async_add_external_statistics`.

Keeping the planner pure means the sum-continuity rules are tested without a
recorder. A second suite tests them with a real recorder (§8).

## 5. Error handling

`api/errors.py` defines one exception per failure *class*. The class decides
the HA outcome. Re-auth is reserved for failures that re-auth can fix. That
was the old code's costliest mistake: it sent users through re-auth for WAF
blocks (SPEC §6).

| Class | Detected by | HA outcome |
|---|---|---|
| `AuthExpiredError` | Auth0 `invalid_grant` or `unauthorized_client` on refresh; a second 401 from the API after re-exchange | `ConfigEntryAuthFailed` → reauth |
| `BlockedError` | Any 403/406 with a non-JSON (HTML) body, from any host | `UpdateFailed`; repair issue `blocked` after ≥ 3 consecutive fetch failures spanning ≥ 12 h |
| `ClientRejectedError` | 403 JSON `…explicit deny in an identity-based policy` from `/login/auth-token` | `UpdateFailed`; repair issue `client_rejected` (needs an integration update, not a re-auth) |
| `ApiChangedError` | 404; 403 JSON `Invalid key=value pair…`; response JSON missing expected keys | `UpdateFailed`; repair issue `api_changed` naming the endpoint |
| `TransientError` | Timeout, connection error, 5xx (including `Login is disabled`), 429 | Retry twice in-poll (2 s, 8 s; honor `Retry-After` up to 60 s), then `UpdateFailed` and the §4.1 backoff |
| (during setup) | Transient or Blocked | `ConfigEntryNotReady` |
| (during setup) | AuthExpired | `ConfigEntryAuthFailed` |

**Other rules:**

- **Repair issues clear themselves** on the next successful fetch.
- **Logging:**
  - Never log tokens, the `Authorization` header, email, account numbers or
    addresses.
  - Error logs carry: endpoint path, status, content type, and the first 200
    characters of the body, with digits-only runs of 6+ masked.
  - At `debug` level: request and response summaries, never full bodies.
- **Diagnostics** (`diagnostics.py`):
  - **Entry data**, with every token and identifier redacted.
  - **Meters**, as `meter_1…n` plus service type.
  - **Per statistic:** last stored hour, last sum, last fetch outcome and
    error class.
  - **The User-Agents in use.**

## 6. Statistics lifecycle

- **The integration never clears statistics.** That covers unload, reload, and
  entry removal.
  - **Why:** the user's top priority is unbroken history. Deleting and
    re-adding an integration is the most common troubleshooting step HA users
    take.
  - **Difference from sam2kb:** sam2kb clears on removal of the last entry.
    COMPATIBILITY §4 explains why we don't.
  - **What users do instead:** remove series explicitly in Developer Tools →
    Statistics. The README says so.
- **New meter serial on an account** (for example, a meter replacement): start
  a new statistic and raise a one-time repair issue telling the user to add it
  to the Energy dashboard. The old statistic is left alone.

## 7. Project layout

```
custom_components/duke_energy/
  __init__.py          setup/unload/migrate; builds session, TokenManager, client, coordinator
  manifest.json        domain duke_energy, config_flow, dependencies [recorder], requirements []
  const.py             DOMAIN, Duke tz, statistic ID/metadata builders
  config_flow.py       paste-callback step (user/reauth/reconfigure), options flow (§10 Q3)
  coordinator.py       scheduling/gating, per-meter fetch → plan → import, repair issues
  statistics.py        plan_import (pure) + recorder adapter
  diagnostics.py
  repairs.py           issue creation/clearing helpers
  user_agent.py        override > remote (weekly, validated, cached) > built-in resolution (§3.4)
  strings.json
  translations/en.json
  api/                 no Home Assistant imports — usable from scripts and tests standalone
    __init__.py
    const.py           URLs, client IDs/secrets, User-Agents, auth0Client blob
    oauth.py           PKCE/state/nonce transaction, authorize URL, callback parsing, Auth0 token calls
    tokens.py          TokenManager (id_token refresh, Duke exchange, persistence callback)
    client.py          account/list (+normalization), account-details-v2, usage graph
    parse.py           hourly row alignment (pure)
    errors.py          error classes + response classifier
    http.py            Transport: timeouts, retries, classification in one place
    models.py          dataclasses: TokenSet, Account, Meter, HourlyReading
tests/
  fixtures/            sanitized live captures (from live_check.py --capture)
  helpers.py, fakes.py synthetic IDs, JWT builder, fake Duke client for HA-level tests
  api/                 test_oauth.py, test_tokens.py, test_client.py, test_parse.py, test_errors.py
  test_statistics_plan.py
  test_statistics_recorder.py
  test_config_flow.py
  test_coordinator.py
  test_init.py         setup, unload/removal keep statistics, v1/v2 → v3 migration
  test_user_agent.py
scripts/
  live_check.py        maintainer-only: paste login in a terminal, fetch recent days, print a
                       redacted summary; --capture DIR saves redacted responses as fixtures
hacs.json
pyproject.toml         uv-managed; dev deps only (HA test plugin, ruff, mypy)
uv.lock
.github/workflows/ci.yml   ruff, mypy, pytest (min + latest HA), hassfest, HACS validation
README.md
```

**API client: an in-tree subpackage, not a PyPI package.**

- **One release, not two.** Every Duke break so far needed a library release
  plus an integration release. Forks paid for that split: QbitPrime's import
  confusion with `aiodukeenergy` vs `aiodukeenergy_co` (ha #59), and sam2kb's
  local overrides while waiting for a library release.
- **Still standalone.** Keeping `api/` free of HA imports keeps it testable on
  its own and easy to extract later.

**Dependencies:** none beyond what HA ships (`aiohttp`, `yarl`, `PyJWT`).

**Session:** a dedicated session from `async_create_clientsession(hass)`, so
Duke's cookies (Akamai sets them) don't mix with other integrations. Every
request sets the full header set from SPEC §4.1.

## 8. Testing

**Tooling:**

- `uv` for the environment.
- `pytest` with `pytest-homeassistant-custom-component`, pinned per CI matrix
  entry.
- HA's `AiohttpClientMocker` (from the test plugin) for HTTP in `tests/api`.
  `aioresponses` doesn't work with the aiohttp version HA ships.
- `ruff` for format and lint.
- `mypy`: strict for `api/` and `statistics.py`, default for the HA glue.

**Layers:**

1. **`api` unit tests** (no HA). Cover:
   - authorize URL contents;
   - callback parsing: every error key in §2.3;
   - token refresh, including rotation persistence and expiry from
     `expires_in`;
   - the error classifier against real-shaped bodies (Akamai HTML, explicit
     deny JSON, `Invalid key=value pair`, `Login is disabled`, Auth0
     `invalid_grant`);
   - `account/list` normalization (both BP spellings, missing src IDs);
   - request body construction for the usage graph.
2. **Parser tests.** Fixtures for:
   - a normal day;
   - fall-back (25 rows);
   - spring-forward (23 rows);
   - a missing interior hour;
   - a short array;
   - a whole missing day in a 31-day window (must trigger per-day
     re-requests);
   - trailing zeros;
   - negative or NaN values.
3. **Import planner tests** (pure). Cover every COMPATIBILITY §3.2 rule. Add a
   property-style test: random gap and failure sequences across several polls
   must converge on the same final sums as a single clean import.
4. **Recorder-backed tests:** COMPATIBILITY §5 items 1–7, against a real
   recorder seeded with old-integration metadata and rows.
5. **Config flow tests:** happy path, each validation error, code expiry
   regenerating the link, reauth `wrong_account`, and v2 → v3 migration.
6. **Coordinator tests** with a fake API client at the `api.client` boundary:
   - fetch gating and backoff, using frozen time;
   - mapping from error class to HA outcome;
   - repair issue raise and clear;
   - keep-alive refresh.
7. **Live check (manual, not CI).** `scripts/live_check.py` runs against your
   account when Duke changes something. Tokens go to `.secrets/` (gitignored).
   New response shapes become fixtures via `live_check.py --capture DIR`, which
   redacts them first.

**CI:**

- lint, type check, and tests on two HA versions: the minimum supported (§10
  Q6) and the latest release;
- `hassfest`;
- `hacs/action`.

## 9. Distribution

- **`hacs.json`:** name, minimum HA version, `render_readme`.
- **Releases:** GitHub releases with semver tags. `manifest.json` `version`
  matches the tag, and CI checks that it does.
- **No brand assets:** Duke's logo is their trademark. HA falls back to a
  generic icon.

## 10. Decisions

| # | Topic | Decision |
|---|---|---|
| 1 | Login | **Paste the callback URL (B). No extension, ever.** The bookmarklet (E) is not planned. |
| 2 | Statistics on removal | **Never delete.** The integration doesn't clear statistics on unload, reload or entry removal (§6). |
| 3 | App User-Agent | **Three layers** (§3.4): (1) a manual override in options; (2) a shared known-good value in this repo, checked weekly and right away after a block, which can be turned off in options; (3) the built-in default. |
| 4 | Scope | **Electric only** for v1. |
| 5 | Values | **Zero is real data**, except a trailing run on the newest day, which is deferred. **Negatives are ignored** for now (§4.3). |
| 6 | Minimum HA | **2025.12.0.** Raise it only if a needed API requires it. CI tests that version and the latest release. |
| 7 | Statistic IDs | **Format confirmed** against a real series. Nothing user-specific is hard-coded (COMPATIBILITY §1). |
