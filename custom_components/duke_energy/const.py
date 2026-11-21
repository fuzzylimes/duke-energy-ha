"""Constants for the Duke Energy integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "duke_energy"

# Config entry schema version. v2 was hunterjm/ha-dukeenergy's OAuth2 shape.
CONFIG_VERSION: Final = 3

CONF_REFRESH_TOKEN: Final = "refresh_token"
CONF_ID_TOKEN: Final = "id_token"
CONF_ID_TOKEN_EXP: Final = "id_token_exp"
CONF_INTERNAL_IDENTIFIER: Final = "internal_identifier"
CONF_EMAIL: Final = "email"

OPT_APP_USER_AGENT: Final = "app_user_agent"
OPT_REMOTE_USER_AGENT: Final = "remote_user_agent"

SUPPORTED_SERVICE_TYPE: Final = "ELECTRIC"

# Where the shared, known-good app User-Agent is published (DESIGN §3.4).
REPO_SLUG: Final = "fuzzylimes/duke-energy-ha"
REMOTE_USER_AGENTS_URL: Final = (
    f"https://raw.githubusercontent.com/{REPO_SLUG}/main/user-agents.json"
)
REMOTE_CHECK_INTERVAL: Final = timedelta(days=7)
REMOTE_URGENT_MIN_INTERVAL: Final = timedelta(hours=1)

# Scheduling (DESIGN §4.1).
TICK_INTERVAL: Final = timedelta(hours=1)
DATA_READY_HOUR: Final = 7
MAX_JITTER_MINUTES: Final = 90
KEEPALIVE_INTERVAL: Final = timedelta(hours=24)
METER_CACHE_TTL: Final = timedelta(hours=24)
BACKOFF_MAX: Final = timedelta(hours=6)
OVERLAP: Final = timedelta(days=30)
HISTORY_LIMIT: Final = timedelta(days=3 * 365)
BACKFILL_REQUEST_SPACING: Final = 1.0

# Repair issue keys.
ISSUE_BLOCKED: Final = "blocked"
ISSUE_CLIENT_REJECTED: Final = "client_rejected"
ISSUE_API_CHANGED: Final = "api_changed"
ISSUE_METADATA_MISMATCH: Final = "metadata_mismatch"
ISSUE_NEW_METER: Final = "new_meter"
BLOCKED_ISSUE_MIN_FAILURES: Final = 3
BLOCKED_ISSUE_MIN_SPAN: Final = timedelta(hours=12)


def statistic_id(service_type: str, serial_number: str) -> str:
    """Build the statistic ID exactly as the old integration did.

    `duke_energy:electric_<serialNum>_energy_consumption`, with the serial used
    verbatim (docs/COMPATIBILITY.md §1).
    """
    return f"{DOMAIN}:{service_type.lower()}_{serial_number}_energy_consumption"


def statistic_name(service_type: str, serial_number: str) -> str:
    """Build the statistic name exactly as the old integration did."""
    return f"Duke Energy {service_type.capitalize()} {serial_number} Consumption"
