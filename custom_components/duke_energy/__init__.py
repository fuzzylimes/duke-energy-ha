"""Duke Energy: import hourly electric usage into long-term statistics."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .api import (
    Auth0Client,
    AuthExpiredError,
    DukeEnergyClient,
    DukeEnergyError,
    TokenManager,
    TokenSet,
    Transport,
)
from .api.oauth import decode_id_token
from .const import (
    CONF_EMAIL,
    CONF_ID_TOKEN,
    CONF_ID_TOKEN_EXP,
    CONF_INTERNAL_IDENTIFIER,
    CONF_REFRESH_TOKEN,
    CONFIG_VERSION,
)
from .coordinator import DukeEnergyConfigEntry, DukeEnergyCoordinator
from .user_agent import UserAgentResolver

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


def tokens_from_entry(data: dict[str, Any]) -> TokenSet | None:
    """Read persisted tokens, or None if the entry has none."""
    refresh = data.get(CONF_REFRESH_TOKEN)
    if not isinstance(refresh, str) or not refresh:
        return None
    return TokenSet(
        refresh_token=refresh,
        id_token=str(data.get(CONF_ID_TOKEN) or ""),
        id_token_exp=float(data.get(CONF_ID_TOKEN_EXP) or 0),
    )


def entry_data(
    tokens: TokenSet, internal_identifier: str, email: str
) -> dict[str, Any]:
    """Config entry data for a set of tokens."""
    return {
        CONF_REFRESH_TOKEN: tokens.refresh_token,
        CONF_ID_TOKEN: tokens.id_token,
        CONF_ID_TOKEN_EXP: tokens.id_token_exp,
        CONF_INTERNAL_IDENTIFIER: internal_identifier,
        CONF_EMAIL: email,
    }


async def async_setup_entry(hass: HomeAssistant, entry: DukeEnergyConfigEntry) -> bool:
    """Set up from a config entry."""
    tokens = tokens_from_entry(dict(entry.data))
    if tokens is None:
        msg = "No stored Duke Energy sign-in; please sign in again"
        raise ConfigEntryAuthFailed(msg)

    user_agents = UserAgentResolver(hass, entry)
    await user_agents.async_load()

    # A session of our own keeps Duke's cookies away from other integrations.
    session = async_create_clientsession(hass)
    transport = Transport(session)
    auth0 = Auth0Client(transport, user_agents.app_user_agent)

    def _persist(new: TokenSet) -> None:
        hass.config_entries.async_update_entry(
            entry,
            data={
                **entry.data,
                CONF_REFRESH_TOKEN: new.refresh_token,
                CONF_ID_TOKEN: new.id_token,
                CONF_ID_TOKEN_EXP: new.id_token_exp,
            },
        )

    token_manager = TokenManager(
        transport,
        auth0,
        tokens,
        user_agent=user_agents.app_user_agent,
        on_tokens_updated=_persist,
    )
    client = DukeEnergyClient(transport, token_manager, user_agents.app_user_agent)

    try:
        meters = await client.async_get_all_meters()
    except AuthExpiredError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except DukeEnergyError as err:
        if await user_agents.async_maybe_refresh(urgent=True):
            _LOGGER.info("Retrying setup with an updated app User-Agent")
        raise ConfigEntryNotReady(f"{type(err).__name__}: {err}") from err

    coordinator = DukeEnergyCoordinator(hass, entry, client, user_agents, meters=meters)
    entry.runtime_data = coordinator

    # The coordinator only polls while it has listeners; there are no entities.
    entry.async_on_unload(coordinator.async_add_listener(lambda: None))
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    # A backfill can take a minute or more, so don't hold up startup for it.
    entry.async_create_background_task(
        hass, coordinator.async_refresh(), "duke_energy first refresh"
    )
    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: DukeEnergyConfigEntry) -> bool:
    """Unload. Statistics are never touched (docs/DESIGN.md §6)."""
    await entry.runtime_data.client.transport.session.close()
    return True


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Entry deleted. Drop our cache, but keep every statistic."""
    await UserAgentResolver(hass, entry).async_remove()


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate old entries to v3 without touching statistics.

    v2 is hunterjm/ha-dukeenergy's OAuth2 entry. Its Auth0 refresh token is for
    the same iOS client, so if it's still alive no new sign-in is needed.
    """
    if entry.version > CONFIG_VERSION:
        return False
    if entry.version == CONFIG_VERSION:
        return True

    old_version = entry.version
    data: dict[str, Any] = {}
    token = entry.data.get("token") if old_version == 2 else None
    if isinstance(token, dict) and token.get("refresh_token"):
        id_token = str(token.get("id_token") or "")
        exp = 0.0
        email = ""
        try:
            claims = decode_id_token(id_token, now=0)
            exp = float(claims["exp"])
            email = str(claims.get("email") or "")
        except DukeEnergyError:
            id_token = ""
        data = {
            CONF_REFRESH_TOKEN: str(token["refresh_token"]),
            CONF_ID_TOKEN: id_token,
            CONF_ID_TOKEN_EXP: exp,
            CONF_INTERNAL_IDENTIFIER: entry.unique_id or "",
            CONF_EMAIL: email,
        }
    hass.config_entries.async_update_entry(
        entry, data=data, version=CONFIG_VERSION, minor_version=1
    )
    _LOGGER.info(
        "Migrated Duke Energy entry from v%s to v%s (%s sign-in)",
        old_version,
        CONFIG_VERSION,
        "kept" if data else "needs a new",
    )
    return True
