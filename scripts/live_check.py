"""Maintainer tool: check the integration's API client against the live Duke API.

Needs only aiohttp and PyJWT, so it also runs outside the dev environment:
    uv run --no-project --with aiohttp --with pyjwt python scripts/live_check.py ...

Usage:
    uv run python scripts/live_check.py --start      # step 1: print a sign-in link
    uv run python scripts/live_check.py --finish 'https://login.duke-energy.com/ios/...'
                                                     # step 2: finish sign-in, then check
    uv run python scripts/live_check.py              # later runs reuse saved tokens
    uv run python scripts/live_check.py --days 3     # fetch the last 3 days
    uv run python scripts/live_check.py --login      # force a new sign-in
    uv run python scripts/live_check.py --capture tests/fixtures/live
                                                     # also save redacted responses

Tokens are kept in .secrets/duke_tokens.json (gitignored, mode 0600). Output
masks account numbers, serials and emails. Captured responses are redacted the
same way before they're written, so they're safe to turn into test fixtures.
Still review every captured file before committing it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import aiohttp

ROOT = Path(__file__).resolve().parent.parent
# Load the API client as a top-level package, so this script doesn't need Home
# Assistant installed (the api package never imports it). Only aiohttp and
# PyJWT are required.
sys.path.insert(0, str(ROOT / "custom_components" / "duke_energy"))

from api import (  # noqa: E402
    Auth0Client,
    AuthTransaction,
    DukeEnergyClient,
    DukeEnergyError,
    TokenManager,
    TokenSet,
    Transport,
    new_transaction,
    parse_callback,
)
from api.const import (  # noqa: E402
    BROWSER_USER_AGENT,
    DEFAULT_APP_USER_AGENT,
    DUKE_TIMEZONE,
)

TOKENS_FILE = ROOT / ".secrets" / "duke_tokens.json"
PENDING_FILE = ROOT / ".secrets" / "pending_login.json"
SENSITIVE_KEY = re.compile(
    r"email|address|account|bp|serial|meter|zip|phone|name|src|user|id$|number",
    re.IGNORECASE,
)
DIGITS = re.compile(r"\d{5,}")


def mask(value: str) -> str:
    """Show only the last 3 characters."""
    return "…" + value[-3:] if len(value) > 3 else "…"


def redact(value: Any, key: str = "") -> Any:
    """Recursively redact identifying fields in a JSON response."""
    if isinstance(value, dict):
        return {k: redact(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(item, key) for item in value]
    if isinstance(value, str):
        if key and SENSITIVE_KEY.search(key) and key not in {"serviceType"}:
            return "REDACTED"
        if "@" in value:
            return "REDACTED"
        return DIGITS.sub("00000", value)
    return value


class CapturingTransport(Transport):
    """Transport that saves every redacted JSON response."""

    def __init__(self, session: aiohttp.ClientSession, directory: Path) -> None:
        super().__init__(session)
        self._directory = directory
        self._count = 0

    async def request_json(self, method: str, url: Any, **kwargs: Any) -> Any:
        payload = await super().request_json(method, url, **kwargs)
        endpoint = str(kwargs.get("endpoint", "response"))
        if "token" not in endpoint:  # never save token responses
            self._count += 1
            name = re.sub(r"[^a-z0-9]+", "_", endpoint.lower()).strip("_")
            path = self._directory / f"{self._count:02d}_{name}.json"
            path.write_text(json.dumps(redact(payload), indent=2) + "\n")
            print(f"  captured {path.relative_to(ROOT)}")
        return payload


def load_tokens() -> TokenSet | None:
    if not TOKENS_FILE.exists():
        return None
    data = json.loads(TOKENS_FILE.read_text())
    return TokenSet(
        data["refresh_token"], data["id_token"], float(data["id_token_exp"])
    )


def save_tokens(tokens: TokenSet) -> None:
    write_secret(
        TOKENS_FILE,
        {
            "refresh_token": tokens.refresh_token,
            "id_token": tokens.id_token,
            "id_token_exp": tokens.id_token_exp,
        },
    )


def write_secret(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(data))
    os.chmod(path, 0o600)


def print_link(txn: AuthTransaction) -> None:
    print(
        "\nSign in with a browser that emulates an iPhone (or on a phone).\n"
        f"Browser User-Agent: {BROWSER_USER_AGENT}\n\n"
        f"{txn.authorize_url}\n\n"
        "After signing in you'll see a 'Not Found' page; copy its full address."
    )


def start_login() -> int:
    """Step 1 of a two-step sign-in, for shells without interactive input."""
    txn = new_transaction()
    write_secret(
        PENDING_FILE,
        {"state": txn.state, "nonce": txn.nonce, "code_verifier": txn.code_verifier},
    )
    print_link(txn)
    print(
        "\nThen run, with the address in single quotes:\n"
        "  uv run python scripts/live_check.py --finish '<address>'"
    )
    return 0


async def finish_login(auth0: Auth0Client, pasted: str) -> TokenSet:
    if not PENDING_FILE.exists():
        print("No sign-in in progress; run with --start first.", file=sys.stderr)
        raise SystemExit(2)
    pending = json.loads(PENDING_FILE.read_text())
    code = parse_callback(pasted, pending["state"])
    PENDING_FILE.unlink()  # the code is single-use either way
    tokens, identity = await auth0.async_exchange_code(
        code, pending["code_verifier"], nonce=pending["nonce"]
    )
    print(f"Signed in as {mask(identity.email)}")
    return tokens


async def sign_in(auth0: Auth0Client) -> TokenSet:
    if not sys.stdin.isatty():
        print(
            "No saved tokens. Sign in with --start, then --finish '<address>'.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    txn = new_transaction()
    print_link(txn)
    pasted = await asyncio.to_thread(input, "Paste it here: ")
    code = parse_callback(pasted, txn.state)
    tokens, identity = await auth0.async_exchange_code(
        code, txn.code_verifier, nonce=txn.nonce
    )
    print(f"Signed in as {mask(identity.email)}")
    return tokens


async def run(args: argparse.Namespace) -> int:
    user_agent = args.user_agent or DEFAULT_APP_USER_AGENT
    tz = ZoneInfo(DUKE_TIMEZONE)
    async with aiohttp.ClientSession() as session:
        if args.capture:
            directory = Path(args.capture).resolve()
            directory.mkdir(parents=True, exist_ok=True)
            transport: Transport = CapturingTransport(session, directory)
        else:
            transport = Transport(session)
        auth0 = Auth0Client(transport, lambda: user_agent)

        tokens = None if args.login else load_tokens()
        if args.finish:
            tokens = await finish_login(auth0, args.finish)
            save_tokens(tokens)
        elif tokens is None:
            tokens = await sign_in(auth0)
            save_tokens(tokens)

        manager = TokenManager(
            transport,
            auth0,
            tokens,
            user_agent=lambda: user_agent,
            on_tokens_updated=save_tokens,
        )
        client = DukeEnergyClient(transport, manager, lambda: user_agent)

        print("Exchanging tokens…")
        await manager.async_get_api_token()
        accounts = await client.async_get_accounts()
        print(f"Accounts: {len(accounts)}")
        end = datetime.now(tz).date() - timedelta(days=1)
        start = end - timedelta(days=args.days - 1)
        for account in accounts:
            meters = await client.async_get_meters(account)
            print(f"  account {mask(account.account_number)}: {len(meters)} meter(s)")
            for meter in meters:
                print(
                    f"    {meter.service_type} meter {mask(meter.serial_number)}, "
                    f"active since {meter.agreement_active_date}"
                )
                if meter.service_type != "ELECTRIC":
                    continue
                result = await client.async_get_hourly(meter, start, end)
                readings = result.readings
                total = sum(reading.kwh for reading in readings)
                print(
                    f"      {start}..{end}: {len(readings)} hours, {total:.2f} kWh, "
                    f"missing {result.missing_hours}, invalid {result.invalid_values}, "
                    f"rejected days {[d.isoformat() for d in result.rejected_days]}"
                )
                if readings:
                    first = readings[0].start.astimezone(tz)
                    last = readings[-1].start.astimezone(tz)
                    print(
                        f"      first {first:%Y-%m-%d %H:%M %Z}, last {last:%Y-%m-%d %H:%M %Z}"
                    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--days", type=int, default=1, help="days to fetch (default 1)")
    parser.add_argument("--login", action="store_true", help="force a new sign-in")
    parser.add_argument("--start", action="store_true", help="print a sign-in link")
    parser.add_argument(
        "--finish", metavar="URL", help="finish sign-in with the pasted URL"
    )
    parser.add_argument("--capture", metavar="DIR", help="save redacted responses")
    parser.add_argument("--user-agent", help="override the app User-Agent")
    args = parser.parse_args()
    if args.start:
        return start_login()
    try:
        return asyncio.run(run(args))
    except DukeEnergyError as err:
        print(f"\n{type(err).__name__}: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
