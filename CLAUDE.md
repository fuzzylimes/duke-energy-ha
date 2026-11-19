# duke_energy: Home Assistant custom integration (rewrite)

This is a ground-up rewrite of a HACS-installable Home Assistant integration.
It imports Duke Energy hourly electric usage into HA long-term statistics for
the Energy dashboard.

The maintainer owns this project. It is meant for any Duke customer to use.

## Read these first (approved 2026-11-19)

- **[docs/SPEC.md](docs/SPEC.md):** everything known about Duke's API and login:
  endpoints, headers, token lifetimes, response quirks, a breakage timeline
  attributed to forks, and open questions (§9).
- **[docs/COMPATIBILITY.md](docs/COMPATIBILITY.md):** the hard contract for continuing
  existing statistics: IDs, metadata, the sum-continuation rules, migration and
  required tests.
- **[docs/DESIGN.md](docs/DESIGN.md):** architecture, the login approach, token
  lifecycle, User-Agent strategy, fetch and import strategy, error handling,
  layout, testing. §10 records the decisions the maintainer made.

If code and docs disagree, the docs win until the maintainer agrees to change
them. Update the docs in the same change as the code.

## Status

The design docs are approved. **Don't start implementation until the
maintainer says so.** No code exists yet. The layout and commands below are
the planned target.

## Non-negotiables

- **Domain is `duke_energy`.** The integration must install through HACS
  (HACS is now called Marketplace).
- **Statistic IDs** are `duke_energy:electric_<serialNum>_energy_consumption`.
  Build them at runtime from Duke's `serialNum`, used verbatim. **Never
  hard-code a real ID, serial, account number or email**, in code, fixtures or
  docs. Fixtures use synthetic, redacted data.
- **Statistic metadata** must match COMPATIBILITY §2 exactly:
  - `source="duke_energy"`
  - `unit="kWh"`
  - `unit_class="energy"`
  - `has_sum=True`
  - `mean_type=NONE`
  - the same name format
- **Never restart or shift the cumulative sum.** Follow COMPATIBILITY §3.2:
  - anchor on a stored row;
  - rewrite only a contiguous suffix;
  - never write rows after a gap you intend to fill later.
- **Never clear statistics.** Not on unload, reload or entry removal.
- **Login means pasting the callback URL into the config flow. No browser
  extension, ever.**
- **Electric meters only.** Store zero-usage hours as data, except a trailing
  run of zeros on the newest day, which is deferred. Ignore negative values.
- **Minimum HA version is 2025.12.0.** CI tests that version and the latest
  release.
- **Re-auth only for failures re-auth can fix.** WAF/edge blocks, revoked
  client credentials and API route changes become repair issues and retries,
  not reauth (DESIGN §5).
- **Never log** tokens, the `Authorization` header, email, account numbers,
  addresses or serials.
- **App User-Agent resolution, in priority order:**
  1. the user override in options;
  2. a remote `user-agents.json` in this repo, fetched weekly or right after a
     block, regex-validated, and possible to disable;
  3. the built-in default.

  See DESIGN §3.4.

## Planned layout (DESIGN §7)

```
custom_components/duke_energy/
  __init__.py  config_flow.py  coordinator.py  statistics.py  user_agent.py
  diagnostics.py  repairs.py  const.py  manifest.json  strings.json  translations/
  api/         # no Home Assistant imports
    const.py  oauth.py  tokens.py  client.py  parse.py  errors.py  models.py
tests/         # api/, statistics planner, recorder-backed, config flow, coordinator, migration
scripts/       # live_check.py, capture_fixture.py (maintainer-only, never in CI)
hacs.json  pyproject.toml  uv.lock  user-agents.json  .github/workflows/ci.yml
```

**Notes:**

- **No runtime requirements.** The integration uses only what HA ships:
  `aiohttp`, `yarl`, `PyJWT`.
- **The API client lives in-tree** under `api/`. It is not a PyPI package.

## Dev commands (once scaffolded)

```bash
uv sync                                  # create env with dev deps
uv run pytest                            # all tests
uv run pytest tests/api                  # API client tests only (no HA)
uv run ruff format . && uv run ruff check .
uv run mypy custom_components            # strict for api/ and statistics.py
uv run python scripts/live_check.py      # manual check against the live API; tokens in .secrets/ (gitignored)
```

CI runs ruff, mypy, pytest on the HA version matrix, hassfest, and HACS
validation.

## Reference material

- **`reference/ha-dukeenergy` and `reference/aiodukeenergy`** hold the old
  hunterjm repos, MIT-licensed. They are **read-only and not committed**
  (`.gitignore`).
- **Treat them as a record of Duke's behavior, not a template.** Don't copy
  their structure, dependencies or tooling by default.
- **Known bugs in the reference code that must not be carried over:**
  - It drops the DST fall-back duplicate hour.
  - It treats every zero as missing.
  - It treats any error as the end of history.
  - It clears statistics on unload.
  - It sends WAF blocks to re-auth.
- **Fork findings are attributed in docs/SPEC.md.**

## Expect Duke to break things

Duke has broken something every 1–3 months (SPEC §6). When something breaks:

1. **Find the failing step** using SPEC §3.3: browser authorize, Auth0 token,
   Duke token exchange, or a data endpoint.
2. **Reproduce it** with `scripts/live_check.py`.
3. **Capture a redacted fixture.**
4. **Fix it,** adding a regression test.
5. **Record the finding** in docs/SPEC.md, with date and source.
