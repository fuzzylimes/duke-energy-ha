from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

import pytest
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.duke_energy.api import (
    BlockedError,
    Identity,
    InvalidGrantError,
    TokenSet,
)
from custom_components.duke_energy.api.const import REDIRECT_URI
from custom_components.duke_energy.const import (
    CONF_EMAIL,
    CONF_INTERNAL_IDENTIFIER,
    CONF_REFRESH_TOKEN,
    DOMAIN,
    OPT_APP_USER_AGENT,
    OPT_REMOTE_USER_AGENT,
)

from .helpers import EMAIL, INTERNAL_ID, make_id_token

TOKENS = TokenSet("refresh-1", make_id_token(), time.time() + 1800)
IDENTITY = Identity(INTERNAL_ID, EMAIL)


@pytest.fixture(autouse=True)
def mock_backend():
    with (
        patch(
            "custom_components.duke_energy.config_flow.async_user_agent_for_flow",
            AsyncMock(return_value="Duke%20Energy/1 CFNetwork/1 Darwin/1"),
        ),
        patch(
            "custom_components.duke_energy.config_flow.Auth0Client.async_exchange_code",
            AsyncMock(return_value=(TOKENS, IDENTITY)),
        ) as exchange,
        patch(
            "custom_components.duke_energy.config_flow.DukeEnergyClient.async_get_accounts",
            AsyncMock(return_value=[]),
        ) as accounts,
        patch(
            "custom_components.duke_energy.async_setup_entry",
            AsyncMock(return_value=True),
        ),
    ):
        yield exchange, accounts


def state_of(result) -> str:
    url = result["description_placeholders"]["authorize_url"]
    return parse_qs(urlsplit(url).query)["state"][0]


def callback_for(state: str, code: str = "code-1") -> str:
    return f"{REDIRECT_URI}?code={code}&state={state}"


async def test_user_flow_creates_entry(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "login"
    assert "iPhone" in result["description_placeholders"]["browser_user_agent"]

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"callback_url": callback_for(state_of(result))}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == EMAIL
    assert result["data"][CONF_REFRESH_TOKEN] == "refresh-1"
    assert result["data"][CONF_INTERNAL_IDENTIFIER] == INTERNAL_ID
    assert result["data"][CONF_EMAIL] == EMAIL
    assert result["result"].unique_id == INTERNAL_ID.lower()


@pytest.mark.parametrize(
    ("pasted", "error"),
    [
        (lambda s: callback_for("someone-elses-state"), "state_mismatch"),
        (
            lambda s: f"{REDIRECT_URI}?code=&state={s}&error=access_denied",
            "authorize_rejected",
        ),
        (lambda s: "https://www.duke-energy.com/", "public_site_redirect"),
        (lambda s: "hello", "invalid_callback_url"),
    ],
)
async def test_bad_paste_shows_error(hass, pasted, error):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    state = state_of(result)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"callback_url": pasted(state)}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}
    # Validation errors keep the same link, so the user can paste again.
    assert state_of(result) == state


async def test_expired_code_gives_fresh_link(hass, mock_backend):
    exchange, _ = mock_backend
    exchange.side_effect = InvalidGrantError("used")
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    state = state_of(result)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"callback_url": callback_for(state)}
    )
    assert result["errors"] == {"base": "code_expired"}
    assert state_of(result) != state


async def test_blocked_during_validation(hass, mock_backend):
    _, accounts = mock_backend
    accounts.side_effect = BlockedError("akamai")
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"callback_url": callback_for(state_of(result))}
    )
    assert result["errors"] == {"base": "blocked"}


async def test_already_configured(hass):
    MockConfigEntry(
        domain=DOMAIN, unique_id=INTERNAL_ID.lower(), version=3
    ).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"callback_url": callback_for(state_of(result))}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def _reauth(hass, entry):
    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "login"
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"callback_url": callback_for(state_of(result))}
    )


async def test_reauth_updates_tokens(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=INTERNAL_ID.lower(),
        version=3,
        data={CONF_REFRESH_TOKEN: "old"},
    )
    entry.add_to_hass(hass)
    result = await _reauth(hass, entry)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_REFRESH_TOKEN] == "refresh-1"


async def test_reauth_wrong_account(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="someone-else",
        version=3,
        data={CONF_REFRESH_TOKEN: "old"},
    )
    entry.add_to_hass(hass)
    result = await _reauth(hass, entry)
    assert result["reason"] == "wrong_account"
    assert entry.data[CONF_REFRESH_TOKEN] == "old"


async def test_reconfigure(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=INTERNAL_ID.lower(),
        version=3,
        data={CONF_REFRESH_TOKEN: "old"},
    )
    entry.add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"callback_url": callback_for(state_of(result))}
    )
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_REFRESH_TOKEN] == "refresh-1"


async def test_options_flow(hass):
    entry = MockConfigEntry(domain=DOMAIN, unique_id="x", version=3, data={})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {OPT_APP_USER_AGENT: "badé", OPT_REMOTE_USER_AGENT: True}
    )
    assert result["errors"] == {OPT_APP_USER_AGENT: "invalid_user_agent"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            OPT_APP_USER_AGENT: " Duke%20Energy/2000 CFNetwork/1 Darwin/1 ",
            OPT_REMOTE_USER_AGENT: False,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {
        OPT_APP_USER_AGENT: "Duke%20Energy/2000 CFNetwork/1 Darwin/1",
        OPT_REMOTE_USER_AGENT: False,
    }
