# Duke Energy for Home Assistant

A custom integration that imports your **hourly Duke Energy electric usage**
into Home Assistant's long-term statistics. You can then track it in the
**Energy dashboard**.

Duke publishes usage about a day late, so this integration creates no sensors.
It writes hourly history that the Energy dashboard reads directly.

- **No browser extension.** You sign in by pasting one URL.
- **Continues existing history** from
  [hunterjm/ha-dukeenergy](https://github.com/hunterjm/ha-dukeenergy) (and HA's
  former built-in Duke Energy integration). Your Energy dashboard history stays
  unbroken.
- **Electric meters only.**
- Requires Home Assistant **2025.12** or newer.

> This project isn't affiliated with Duke Energy. It uses the same private API
> as Duke's mobile app, which Duke can change at any time.

## Install

1. In HACS, open the menu → **Custom repositories**. Add this repository's URL
   as type **Integration**.
2. Install **Duke Energy**, then restart Home Assistant.
3. Go to **Settings → Devices & services → Add integration → Duke Energy**.

### Coming from hunterjm/ha-dukeenergy

The old integration **deletes its statistics whenever its entry is reloaded,
disabled or deleted.** To keep your history, switch over without doing any of
those:

1. **Back up Home Assistant** (at least `home-assistant_v2.db`).
2. **Don't** delete, reload or disable the existing Duke Energy entry.
3. In HACS, remove the old repository and add this one. Both use the domain
   `duke_energy`, so this one replaces the old files.
4. Restart Home Assistant.

The existing entry is migrated in place. If its saved sign-in still works, you
won't need to sign in at all. Otherwise you'll be asked to sign in again (see
below). Usage then continues from the last hour you have, filling in anything
missed since, as far back as Duke provides.

## Sign in

Duke's sign-in page only accepts browsers that look like an iPhone, and it
can't redirect back to Home Assistant. So you sign in in your browser, then
paste the address you land on.

1. **On a computer,** turn on iPhone emulation in your browser's developer
   tools. Keep it on while you sign in.
   - **Chrome / Edge:** press F12, then Ctrl+Shift+M (Cmd+Shift+M on a Mac),
     and pick an iPhone from the device list.
   - **Firefox:** press Ctrl+Shift+M (Cmd+Opt+M on a Mac), and pick an iPhone.
   - **Safari:** Develop → User Agent → Safari — iOS. (Enable the Develop menu
     in Settings → Advanced.)

   **On a phone,** skip this step.
2. Click the sign-in link in the Home Assistant dialog, and sign in with your
   Duke Energy email and password (or passkey).
3. You'll land on a page that says **Not Found**. That's expected. Copy that
   page's full address (it starts with `https://login.duke-energy.com/ios/`)
   and paste it into the dialog.

If you see "Duke Energy rejected the sign-in", or end up on Duke's main
website, your browser wasn't emulating an iPhone. Turn on emulation and use the
new link the dialog shows.

## How it works

- **When it checks.** About an hour after 7 AM Eastern (Duke usually publishes
  yesterday's data by then), the integration fetches anything it's missing.
  After that it makes no requests until the next day. If Duke is late, it tries
  again later in the day.
- **What it writes.** One hourly statistic per electric meter, named
  `duke_energy:electric_<meter serial>_energy_consumption`.
- **Overlap.** Each fetch re-reads the last 30 days, so Duke's late corrections
  are picked up. The running total always continues from what's stored; it
  never restarts.
- **First setup** with no existing history imports up to about 3 years. This
  runs in the background and can take a minute or two.
- **Statistics are never deleted by this integration,** even if you remove it.
  Remove and re-add it as often as you like; history continues. To delete
  history yourself, use **Developer Tools → Statistics**.
- **New meter.** If Duke replaces your meter, a repair notice tells you to add
  the new statistic to the Energy dashboard.

### Energy dashboard

Go to **Settings → Dashboards → Energy → Electricity grid → Add consumption**,
and choose **Duke Energy Electric … Consumption**.

## Options and privacy

Duke only answers requests that identify as a recent version of its iPhone app,
and it periodically raises the minimum version. To keep up without waiting for
a release, the integration checks this repository's
[`user-agents.json`](user-agents.json) **once a week**, and right away if Duke
starts blocking requests. It sends nothing else to GitHub.

Under **Settings → Devices & services → Duke Energy → Configure** you can:

- **Turn off the weekly GitHub check.** The built-in value is used instead.
- **Set your own app User-Agent.** It's always used, and GitHub is never
  checked.

Your Duke sign-in tokens are stored in Home Assistant's config entry, like any
other integration's. They're only sent to Duke.

## Troubleshooting

- **A repair says Duke is blocking requests.** Duke raised its minimum app
  version. The weekly check usually picks up a new value automatically.
  Otherwise, update the integration or set a newer User-Agent in options.
  Signing in again won't help.
- **"Sign in again".** Duke stopped accepting the saved sign-in. Follow
  [Sign in](#sign-in). Your history is safe.
- **Missing hours.** Duke sometimes omits hours. They're filled in
  automatically if Duke supplies them within 30 days.
- **Debug logs.** Add this to `configuration.yaml`:
  ```yaml
  logger:
    logs:
      custom_components.duke_energy: debug
  ```
  Logs never include tokens, your email or account numbers. Diagnostics
  (**⋮ → Download diagnostics**) are redacted the same way.

## Development

See [CLAUDE.md](CLAUDE.md) for commands and layout. See [docs/](docs/) for
everything known about Duke's API ([SPEC](docs/SPEC.md)), the statistics
compatibility contract ([COMPATIBILITY](docs/COMPATIBILITY.md)) and the design
([DESIGN](docs/DESIGN.md)).

## Credits

This is a from-scratch rewrite. What it knows about Duke's API comes from:

- [hunterjm/ha-dukeenergy](https://github.com/hunterjm/ha-dukeenergy) and
  [hunterjm/aiodukeenergy](https://github.com/hunterjm/aiodukeenergy), both
  MIT-licensed;
- the community forks and fixes credited in [docs/SPEC.md](docs/SPEC.md),
  especially crash0verride11, sam2kb, jameslb, jdahmen, jdobbsclt, b-v-co and
  babyhuey.
