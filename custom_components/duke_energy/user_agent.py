"""Choose which app User-Agent to send (docs/DESIGN.md §3.4).

Highest priority wins:
1. A user override set in options.
2. The shared known-good value from this repo's `user-agents.json`, checked
   weekly and soon after a block. Can be turned off in options.
3. The built-in default.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

import aiohttp
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .api.const import APP_USER_AGENT_PATTERN, DEFAULT_APP_USER_AGENT
from .const import (
    DOMAIN,
    OPT_APP_USER_AGENT,
    OPT_REMOTE_USER_AGENT,
    REMOTE_CHECK_INTERVAL,
    REMOTE_URGENT_MIN_INTERVAL,
    REMOTE_USER_AGENTS_URL,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)
STORAGE_VERSION = 1
FETCH_TIMEOUT = 15.0
MAX_OVERRIDE_LENGTH = 300


def valid_remote_user_agent(value: Any) -> bool:
    """Accept only the exact shape of the Duke iOS app's User-Agent."""
    return (
        isinstance(value, str) and APP_USER_AGENT_PATTERN.fullmatch(value) is not None
    )


def valid_override(value: Any) -> bool:
    """Loose check for a user-supplied value: printable ASCII, sane length."""
    return (
        isinstance(value, str)
        and 0 < len(value.strip()) <= MAX_OVERRIDE_LENGTH
        and all(" " <= char <= "~" for char in value)
    )


async def async_fetch_remote(hass: HomeAssistant) -> str | None:
    """Fetch the shared value. Returns None on any problem."""
    session = async_get_clientsession(hass)
    try:
        async with (
            asyncio.timeout(FETCH_TIMEOUT),
            session.get(REMOTE_USER_AGENTS_URL) as response,
        ):
            if response.status != 200:
                _LOGGER.debug("User-Agent list returned HTTP %s", response.status)
                return None
            payload = await response.json(content_type=None)
    except (TimeoutError, aiohttp.ClientError, ValueError) as err:
        _LOGGER.debug("Couldn't fetch the User-Agent list: %s", err)
        return None
    value = payload.get("app_user_agent") if isinstance(payload, dict) else None
    if not valid_remote_user_agent(value):
        _LOGGER.warning(
            "Ignoring an invalid app User-Agent from %s", REMOTE_USER_AGENTS_URL
        )
        return None
    return str(value)


class UserAgentResolver:
    """Per-entry User-Agent selection with a cached remote value."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Bind to an entry's options and storage."""
        self._hass = hass
        self._entry = entry
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, storage_key(entry.entry_id)
        )
        self._remote: str | None = None
        self._checked_at: datetime | None = None

    @property
    def _options(self) -> Mapping[str, Any]:
        return self._entry.options

    @property
    def override(self) -> str | None:
        """The user's override, if one is set."""
        value = self._options.get(OPT_APP_USER_AGENT)
        return str(value).strip() if valid_override(value) else None

    @property
    def remote_enabled(self) -> bool:
        """Whether the shared value is used and checked."""
        return self.override is None and bool(
            self._options.get(OPT_REMOTE_USER_AGENT, True)
        )

    @property
    def remote_value(self) -> str | None:
        """Last validated shared value."""
        return self._remote

    @property
    def checked_at(self) -> datetime | None:
        """When the shared value was last checked."""
        return self._checked_at

    @property
    def source(self) -> str:
        """Which layer is in effect: override, remote or default."""
        if self.override is not None:
            return "override"
        if self.remote_enabled and self._remote is not None:
            return "remote"
        return "default"

    def app_user_agent(self) -> str:
        """The User-Agent to send right now."""
        if (override := self.override) is not None:
            return override
        if self.remote_enabled and self._remote is not None:
            return self._remote
        return DEFAULT_APP_USER_AGENT

    async def async_load(self) -> None:
        """Load the cached remote value."""
        data = await self._store.async_load() or {}
        if valid_remote_user_agent(data.get("app_user_agent")):
            self._remote = data["app_user_agent"]
        checked = data.get("checked_at")
        if isinstance(checked, str):
            self._checked_at = dt_util.parse_datetime(checked)

    async def async_maybe_refresh(self, *, urgent: bool = False) -> bool:
        """Check the shared value if it's due. Returns True if it changed."""
        if not self.remote_enabled:
            return False
        now = dt_util.utcnow()
        interval = REMOTE_URGENT_MIN_INTERVAL if urgent else REMOTE_CHECK_INTERVAL
        if self._checked_at is not None and now - self._checked_at < interval:
            return False
        self._checked_at = now
        value = await async_fetch_remote(self._hass)
        changed = value is not None and value != self._remote
        if value is not None:
            self._remote = value
        if changed:
            _LOGGER.info("Using an updated app User-Agent from the shared list")
        await self._store.async_save(
            {"app_user_agent": self._remote, "checked_at": now.isoformat()}
        )
        return changed

    async def async_remove(self) -> None:
        """Delete the cache (entry removed)."""
        await self._store.async_remove()


def storage_key(entry_id: str) -> str:
    """Storage key for an entry's User-Agent cache."""
    return f"{DOMAIN}.{entry_id}.user_agent"


async def async_user_agent_for_flow(
    hass: HomeAssistant, entry: ConfigEntry | None
) -> str:
    """User-Agent for a config flow, before the integration is running.

    Uses the entry's cache when there is one. A new install with no cache asks
    the shared list once, so it works even if the built-in default went stale.
    """
    if entry is not None:
        resolver = UserAgentResolver(hass, entry)
        await resolver.async_load()
        if resolver.override is not None or not resolver.remote_enabled:
            return resolver.app_user_agent()
        if resolver.remote_value is not None:
            return resolver.remote_value
    return await async_fetch_remote(hass) or DEFAULT_APP_USER_AGENT
