# COMPATIBILITY: Continuing the existing long-term statistics

Status: approved, 2026-11-19.

## 0. Scope

**Must stay compatible:** the external long-term statistics that the Energy
dashboard already uses. That means the same statistic IDs, the same metadata,
and an unbroken cumulative `sum`.

**Free to change:** config-entry format, entity unique IDs, internal
structure, the login flow, dependencies. Section 6 lists these and what
happens to each.

## 1. Statistic IDs

The format below was checked against a real series from the maintainer's
install: a 9-digit, all-numeric serial in exactly this form. **No real ID or
serial appears anywhere in the code, fixtures or docs.** IDs are always built
at runtime from what Duke returns, so the integration works for any Duke
customer. Tests use synthetic serials.

To see your own IDs and metadata, use either:

- **Developer Tools → Statistics:** search for `duke_energy`.
- **SQL**, against the recorder DB:
  ```sql
  SELECT statistic_id, source, unit_of_measurement, has_sum, mean_type, unit_class, name
  FROM statistics_meta WHERE statistic_id LIKE 'duke_energy:%';
  ```

### 1.1 How the old integration builds them (verified)

`reference/ha-dukeenergy/custom_components/duke_energy/coordinator.py`:

```python
id_prefix = f"{meter['serviceType'].lower()}_{serial_number}"
consumption_statistic_id = f"{DOMAIN}:{id_prefix}_energy_consumption"
```

That produces `duke_energy:electric_<serialNum>_energy_consumption`.

- **`serial_number`** is the key of `get_meters()`, which is
  `meterInfo[].serialNum` from `GET /account-details-v2` (SPEC §4.3). The
  integration uses it **verbatim**: no lowercasing, no stripping.
- **The format is identical** in HA core's former `duke_energy` integration
  (tag `2025.10.0`) and in every fork I checked (crash0verride11, sam2kb,
  jameslb). History from the core integration therefore has the same IDs.
- **Only `ELECTRIC` meters** produce statistics upstream.
  `_SUPPORTED_METER_TYPES = ("ELECTRIC",)`.
- **HA only accepts statistic IDs** made of lowercase letters, digits and
  underscores on both sides of the `:`. Any existing ID therefore had a serial
  that already passed that check (typically all digits). The new code must not
  transform the serial at all. That way the same Duke response produces the
  same ID.

### 1.2 Runtime cross-check

**Cross-check at runtime:** on every poll the integration builds the ID from
the live `serialNum`. If the result is not already in `statistics_meta`, but
another `duke_energy:electric_*_energy_consumption` ID is, it raises a repair
issue. That case means a meter replacement or a serial format change. It does
**not** silently start a new series.

## 2. Metadata that must match

The values below are what the old integration sends on every import. The new
integration sends exactly these values.

| Field | Value | Notes |
|---|---|---|
| `statistic_id` | as §1 | |
| `source` | `"duke_energy"` | Must equal the ID's domain, or the recorder rejects external stats |
| `unit_of_measurement` | `"kWh"` (`UnitOfEnergy.KILO_WATT_HOUR`) | |
| `unit_class` | `"energy"` (`EnergyConverter.UNIT_CLASS`) | ha-dukeenergy sets this. Core's older code didn't, so rows written under core may have `NULL` until the first import under ha-dukeenergy |
| `has_sum` | `True` | |
| `mean_type` | `StatisticMeanType.NONE` (`has_mean = False` in older schemas) | |
| `name` | `"Duke Energy Electric <serial> Consumption"` | Not strictly required: the recorder updates the name in place if it changes. Kept identical so dashboard labels don't change |

**Guard:** before the first write in each session, the coordinator reads the
existing metadata (`get_metadata`).

- If `source`, `unit_of_measurement`, `has_sum` or `mean_type` differ from the
  table, it **refuses to import** and raises a repair issue. Re-importing with
  different metadata would silently rewrite the series' meaning.
- A `unit_class` of `NULL` is accepted, and is upgraded to `"energy"` by the
  normal import.

## 3. Continuing the cumulative sum

### 3.1 What's in the database today

- **Rows:** one row per hour in the recorder's `statistics` table. External
  statistics have no short-term rows.
- **`start`:** the UTC top of the hour. The old code built hours from local
  `America/New_York` midnights. ET offsets are whole hours, so every row is
  still hour-aligned in UTC.
- **`state`:** that hour's kWh.
- **`sum`:** the running total since the first imported hour. Under the old
  code the sum started at `0.0`. That happened at the first import, and again
  after **every reload or unload**: the old code cleared its statistics on
  unload (SPEC §7) and then re-backfilled ~3 years starting from 0. So the
  series may be shorter than the install's age, and its origin is the start of
  the last backfill.
- **Holes.** The old parser leaves some hours out:
  - Hours whose usage was `0`. It treats zero as missing.
  - The second 01:00 hour at every DST fall-back. It drops the duplicate.
  - Hours Duke never returned.

  The sum simply skips these hours. That is harmless: the Energy dashboard
  works from sum differences across periods, not from row counts.

### 3.2 Rule: never restart, never shift

The new integration **never writes a `sum` that is not anchored to a stored
row**.

1. **Find the anchor.** `get_last_statistics(hass, 1, id, True, {"sum"})` gives
   `T_last` and `S_last`.
2. **Choose a re-fetch window** of `[T_last − overlap, yesterday 23:00 ET]`.
   The overlap is 30 days, the same as the old code, and is one request. It
   picks up Duke's late corrections and fills holes the old parser left.
3. **Load stored rows** for the window plus one hour before it
   (`statistics_during_period(..., types={"sum", "state"})`).
4. **Pick the anchor row:** the latest stored row with
   `start < first fetched hour`. Its `sum` is the base.
   - **No stored row is that old** (the overlap reaches back before the series
     start): don't write any hour before the series' first stored row. Use
     that row's `sum − state` as the base, so every stored sum stays exactly
     as it is.
   - **No stored rows at all** (a brand-new series): the base is `0.0`.
5. **Walk forward** through fetched hours in ascending order: `sum += kWh`,
   and emit `(start, state=kWh, sum)` for each hour.
   - **Suffix rule:** a rewrite always covers a contiguous range from just
     after the anchor **through the newest hour we write**.
   - **Coverage requirement:** if the fetched data ends *before* `T_last`,
     don't rewrite anything up to `T_last`. Rewriting would leave stale sums
     after the rewritten range. Instead, skip this poll's rewrite, log it, and
     retry next poll.
6. **Never write a gap you intend to fill later.** Suppose a window fails
   mid-backfill. Then write only the contiguous prefix that succeeded, and stop.
   The next poll resumes from the new `T_last`. Writing rows after a gap would
   force every later sum to shift once the gap is filled.
   - **Exception:** permanent holes (hours missing from an otherwise successful
     response) are skipped and the sum carries over them. If Duke later fills
     one in, rule 5 rewrites the whole suffix consistently.
7. **Drift check.** If the new sum at `T_last` differs from `S_last` by more
   than 0.01 kWh, log it at `info` level with the delta.
   - **Cause:** Duke revised history, or the new parser filled an old hole,
     such as a recovered DST hour.
   - **Not an error:** later sums move by the same delta, and period totals
     stay correct.

**Write mechanics:**

- Writes go through `async_add_external_statistics(hass, metadata, rows)`. The
  recorder upserts on `(metadata_id, start)`, so overlapping rows are replaced
  in place.
- The call is queued and returns immediately. The coordinator never reads back
  its own writes in the same poll.

### 3.3 First run after migration: backfill to now

On the first poll under the new integration:

- **Rows exist** for the statistic (the expected case). Run §3.2. The window is
  `[T_last − overlap, yesterday]`, split into ≤ 30-day requests.
  - **Normal case:** a gap of days or weeks takes one or two requests.
  - **Long outage:** a gap of months is fetched in order, **oldest window
    first**, so rule 6 applies.
  - **Gap older than Duke's history** (~3 years, SPEC §4.4): fetch from the
    oldest date Duke serves. The sum continues from `S_last` across the
    unrecoverable stretch. The dashboard shows no data there; nothing else
    breaks.
- **No rows exist** (fresh install for someone else, or the user deleted the
  series): backfill from `max(now − 3 y, agreementActiveDate)` with base 0.
  Probe newest-first to find the history boundary, then import oldest-first.

**"As far as Duke's API allows"** is bounded by:

- the history horizon (unknown, ~3 years);
- `agreementActiveDate`;
- yesterday 23:00 ET, the latest complete day. Today's partial data is never
  written; see DESIGN.md for how trailing zeros are handled.

## 4. Migration procedure (to document in the README)

The old code deletes statistics whenever its config entry is unloaded.
Migration must therefore never unload the old entry **while the old code is
installed**.

1. **Back up HA**, at least `home-assistant_v2.db`.
2. **Don't delete, reload or disable** the existing Duke Energy entry.
3. **In HACS**, replace the old repository with this one at the same domain,
   `duke_energy`. Then restart HA.
4. **On startup**, the new code runs `async_migrate_entry` on the old entry
   (version 2):
   - **Refresh token present:** it keeps the Auth0 refresh token from
     `entry.data["token"]` and tries it. The client ID is the same iOS client,
     so if the token is still alive, no login is needed.
   - **Token missing or dead:** the entry goes to re-auth (the paste flow in
     DESIGN.md).
   - **No statistics are touched** by migration.
5. **First poll:** backfill per §3.3.

**After migration:** the new integration **never** clears statistics. That
covers unload, reload, and removing the entry. Deleting and re-adding the
integration is a common troubleshooting step, and it must not erase history.
Users who want to delete history can do it explicitly in Developer Tools →
Statistics. This differs from sam2kb, which clears on removal of the last
entry. That is a deliberate choice; see DESIGN.md §6.

## 5. Verification (tests that must exist before release)

All of these run against a real recorder, via
`pytest-homeassistant-custom-component`.

1. **Seeded continuation.**
   - **Setup:** pre-populate the statistic with old-integration metadata and N
     days of rows ending at `T_last`, with `S_last` ≠ 0 and holes (a zero
     hour, a missing DST hour).
   - **Run:** one poll, with mocked Duke data covering `T_last − 30 d` to
     yesterday.
   - **Assert:**
     - metadata is unchanged;
     - no row before the anchor changes;
     - sums are monotonic, and each equals the anchor plus the cumulative
       fetched kWh;
     - the first new hour's sum equals `S_last` plus that hour (when no
       overlap data changed);
     - no row has `sum` < `S_last` − 1e-6 unless Duke revised data downward.
2. **Metadata guard.** Seed with `has_sum=False` or a different unit. Assert no
   import happens and a repair issue is raised.
3. **Partial failure.** The second of three windows fails. Assert only the
   first window's rows are written. Then a retry poll completes the series with
   the same final sums as a run with no failure.
4. **Coverage requirement.** Fetched data ends before `T_last`. Assert stored
   rows are untouched.
5. **DST.** Fall-back fixtures, both Duke's current 24-row shape (single
   `01 AM`) and the older 25-row shape, plus spring-forward (23 rows). Assert
   correct UTC starts, that no hour is duplicated, and that the sum stays
   continuous across the missing repeated hour.
6. **Migration.** A v2 entry with an old-format token migrates without touching
   statistics. Unload and remove do not clear statistics.
7. **ID stability.** The ID built from fixture `serialNum` values equals the
   old format byte for byte.

## 6. Explicitly not kept compatible

| Item | Old | New |
|---|---|---|
| Config entry | v2, HA OAuth2 shape (`auth_implementation`, `token`) | New version and shape. Migrated in place (§4) |
| Unique ID | `internal_identifier.lower()` | Kept the same (cheap). This makes the reauth wrong-account check work for migrated entries |
| Entities | none | none planned for v1 |
| Chrome extension | required | not used |
| Python dependency | `aiodukeenergy` | none (API client is in-tree) |
