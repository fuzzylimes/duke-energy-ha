"""Diagnostics with every token and identifier redacted."""

from __future__ import annotations

from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from homeassistant.components.diagnostics import async_redact_data

from .const import (
    CONF_EMAIL,
    CONF_ID_TOKEN,
    CONF_INTERNAL_IDENTIFIER,
    CONF_REFRESH_TOKEN,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .coordinator import DukeEnergyConfigEntry

TO_REDACT = {CONF_REFRESH_TOKEN, CONF_ID_TOKEN, CONF_EMAIL, CONF_INTERNAL_IDENTIFIER}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: DukeEnergyConfigEntry
) -> dict[str, Any]:
    """Return redacted diagnostics for an entry."""
    coordinator = entry.runtime_data
    state = coordinator.state
    meters = []
    for index, status in enumerate(state.meters.values(), start=1):
        info = asdict(status)
        info["statistic_id"] = f"meter_{index}"
        meters.append(info)
    resolver = coordinator.user_agents
    return {
        "entry": {
            "version": entry.version,
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
        },
        "state": {
            "last_fetch": state.last_fetch,
            "last_error": state.last_error,
            "backoff_until": state.backoff_until,
            "meters": meters,
        },
        "user_agent": {
            "source": resolver.source,
            "in_use": resolver.app_user_agent(),
            "remote_checked_at": resolver.checked_at,
        },
    }
