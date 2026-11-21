"""Config flow: sign in by pasting Duke's callback URL (docs/DESIGN.md §2.3)."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from homeassistant import data_entry_flow
from homeassistant.config_entries import (
    SOURCE_REAUTH,
    SOURCE_RECONFIGURE,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from . import entry_data
from .api import (
    ApiChangedError,
    Auth0Client,
    AuthTransaction,
    BlockedError,
    ClientRejectedError,
    DukeEnergyClient,
    DukeEnergyError,
    InvalidGrantError,
    InvalidIdTokenError,
    OAuthCallbackError,
    TokenManager,
    TransientError,
    Transport,
    new_transaction,
    parse_callback,
)
from .api.const import AUTH0_HOST, BROWSER_USER_AGENT, DEFAULT_APP_USER_AGENT
from .const import CONFIG_VERSION, DOMAIN, OPT_APP_USER_AGENT, OPT_REMOTE_USER_AGENT
from .user_agent import async_user_agent_for_flow, valid_override

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

_LOGGER = logging.getLogger(__name__)

# Use whichever schema library this Home Assistant validates flows with:
# probatio from 2026.10, voluptuous before that.
vol: Any = getattr(data_entry_flow, "probatio", None) or data_entry_flow.vol  # type: ignore[attr-defined,unused-ignore]

CONF_CALLBACK_URL = "callback_url"
CALLBACK_PREFIX = f"https://{AUTH0_HOST}/ios/"

LOGIN_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_CALLBACK_URL): TextSelector(
            TextSelectorConfig(type=TextSelectorType.TEXT, multiline=True)
        )
    }
)

_ERRORS: list[tuple[type[DukeEnergyError], str]] = [
    (InvalidGrantError, "code_expired"),
    (InvalidIdTokenError, "invalid_auth"),
    (BlockedError, "blocked"),
    (ClientRejectedError, "client_rejected"),
    (ApiChangedError, "api_changed"),
    (TransientError, "cannot_connect"),
]


class DukeEnergyConfigFlow(ConfigFlow, domain=DOMAIN):
    """Sign in to Duke Energy."""

    VERSION = CONFIG_VERSION
    MINOR_VERSION = 1

    def __init__(self) -> None:
        """Start without a sign-in attempt."""
        self._transaction: AuthTransaction | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> DukeEnergyOptionsFlow:
        """Options: User-Agent override and the shared-value check."""
        return DukeEnergyOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Start a new sign-in."""
        return await self.async_step_login()

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Stored sign-in stopped working."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Explain why a new sign-in is needed."""
        if user_input is None:
            return self.async_show_form(step_id="reauth_confirm")
        return await self.async_step_login()

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Sign in again on purpose."""
        return await self.async_step_login()

    def _existing_entry(self) -> ConfigEntry | None:
        if self.source == SOURCE_REAUTH:
            return self._get_reauth_entry()
        if self.source == SOURCE_RECONFIGURE:
            return self._get_reconfigure_entry()
        return None

    async def async_step_login(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the sign-in link and accept the pasted callback URL."""
        if self._transaction is None:
            self._transaction = new_transaction()
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                code = parse_callback(
                    user_input.get(CONF_CALLBACK_URL, ""), self._transaction.state
                )
            except OAuthCallbackError as err:
                errors["base"] = err.reason
                _LOGGER.debug("Pasted callback rejected: %s", err)
            else:
                result = await self._async_finish(code, errors)
                if result is not None:
                    return result

        return self.async_show_form(
            step_id="login",
            data_schema=LOGIN_SCHEMA,
            errors=errors,
            description_placeholders={
                "authorize_url": self._transaction.authorize_url,
                "browser_user_agent": BROWSER_USER_AGENT,
                "callback_prefix": CALLBACK_PREFIX,
            },
        )

    async def _async_finish(
        self, code: str, errors: dict[str, str]
    ) -> ConfigFlowResult | None:
        assert self._transaction is not None  # noqa: S101
        transaction = self._transaction
        existing = self._existing_entry()
        user_agent = await async_user_agent_for_flow(self.hass, existing)

        def agent() -> str:
            return user_agent

        transport = Transport(async_get_clientsession(self.hass))
        auth0 = Auth0Client(transport, agent)
        try:
            tokens, identity = await auth0.async_exchange_code(
                code, transaction.code_verifier, nonce=transaction.nonce
            )
            # Check Duke accepts these tokens before saving anything.
            client = DukeEnergyClient(
                transport,
                TokenManager(transport, auth0, tokens, user_agent=agent),
                agent,
            )
            await client.async_get_accounts()
            tokens = client.tokens.tokens
        except DukeEnergyError as err:
            key = next((k for cls, k in _ERRORS if isinstance(err, cls)), "unknown")
            _LOGGER.warning("Duke Energy sign-in failed: %s", err)
            errors["base"] = key
            # Codes are single-use, so any failed exchange needs a new link.
            self._transaction = new_transaction()
            return None

        unique_id = identity.internal_identifier.lower()
        await self.async_set_unique_id(unique_id)
        data = entry_data(tokens, identity.internal_identifier, identity.email)
        if existing is not None:
            self._abort_if_unique_id_mismatch(reason="wrong_account")
            return self.async_update_reload_and_abort(existing, data=data)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=identity.email or identity.internal_identifier, data=data
        )


class DukeEnergyOptionsFlow(OptionsFlow):
    """User-Agent settings (docs/DESIGN.md §3.4)."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit the override and the shared-value toggle."""
        errors: dict[str, str] = {}
        options = self.config_entry.options
        if user_input is not None:
            override = str(user_input.get(OPT_APP_USER_AGENT, "") or "").strip()
            if override and not valid_override(override):
                errors[OPT_APP_USER_AGENT] = "invalid_user_agent"
            else:
                return self.async_create_entry(
                    data={
                        OPT_APP_USER_AGENT: override,
                        OPT_REMOTE_USER_AGENT: bool(
                            user_input.get(OPT_REMOTE_USER_AGENT, True)
                        ),
                    }
                )

        schema = vol.Schema(
            {
                vol.Optional(
                    OPT_APP_USER_AGENT,
                    description={
                        "suggested_value": options.get(OPT_APP_USER_AGENT, "")
                    },
                ): TextSelector(),
                vol.Required(
                    OPT_REMOTE_USER_AGENT,
                    default=options.get(OPT_REMOTE_USER_AGENT, True),
                ): BooleanSelector(),
            }
        )
        return self.async_show_form(
            step_id="init",
            data_schema=schema,
            errors=errors,
            description_placeholders={"default_user_agent": DEFAULT_APP_USER_AGENT},
        )
