from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticData
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    list_statistic_ids,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers.aiohttp_client import WARN_CLOSE_MSG
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.duke_energy.api import (
    AuthExpiredError,
    TokenSet,
    TransientError,
)
from custom_components.duke_energy.const import (
    CONF_EMAIL,
    CONF_ID_TOKEN_EXP,
    CONF_INTERNAL_IDENTIFIER,
    CONF_REFRESH_TOKEN,
    DOMAIN,
    OPT_APP_USER_AGENT,
    OPT_REMOTE_USER_AGENT,
    REMOTE_USER_AGENTS_URL,
    statistic_id,
)
from custom_components.duke_energy.coordinator import DukeEnergyData
from custom_components.duke_energy.statistics import build_metadata

from .fakes import meter
from .helpers import EMAIL, INTERNAL_ID, SERIAL, make_id_token

MODULE = "custom_components.duke_energy"


def v3_entry(**data):
    return MockConfigEntry(
        domain=DOMAIN,
        version=3,
        unique_id=INTERNAL_ID.lower(),
        data={
            CONF_REFRESH_TOKEN: "r1",
            "id_token": make_id_token(),
            CONF_ID_TOKEN_EXP: time.time() + 1800,
            CONF_INTERNAL_IDENTIFIER: INTERNAL_ID,
            CONF_EMAIL: EMAIL,
            **data,
        },
    )


@pytest.fixture
def no_polling():
    with patch(
        f"{MODULE}.coordinator.DukeEnergyCoordinator._async_update_data",
        AsyncMock(return_value=DukeEnergyData()),
    ) as update:
        yield update


@pytest.fixture
def meters_ok():
    with patch(
        f"{MODULE}.DukeEnergyClient.async_get_all_meters",
        AsyncMock(return_value=[meter()]),
    ) as get:
        yield get


async def test_setup_and_unload(hass, no_polling, meters_ok, caplog):
    entry = v3_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert no_polling.await_count == 1  # first refresh ran in the background
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED
    # HA detaches the session on unload; closing it ourselves logs a warning.
    assert WARN_CLOSE_MSG not in caplog.text


async def test_token_refresh_does_not_reload(hass, no_polling, meters_ok):
    """Saving refreshed tokens must not reload the entry.

    Regression: an update listener reloaded on every entry update, including
    token saves. A new entry whose data was already current then looped
    forever: setup, keep-alive refresh, save, reload, and back to setup.
    """
    entry = v3_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = entry.runtime_data
    new = TokenSet(
        refresh_token="r2", id_token=make_id_token(), id_token_exp=time.time() + 1800
    )
    with patch(f"{MODULE}.Auth0Client.async_refresh", AsyncMock(return_value=new)):
        await coordinator.client.tokens.async_refresh()
    await hass.async_block_till_done()
    assert entry.data[CONF_REFRESH_TOKEN] == "r2"
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data is coordinator
    assert meters_ok.await_count == 1


async def test_options_change_reloads(hass, no_polling, meters_ok):
    entry = v3_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    result = await hass.config_entries.options.async_init(entry.entry_id)
    await hass.config_entries.options.async_configure(
        result["flow_id"], {OPT_APP_USER_AGENT: "", OPT_REMOTE_USER_AGENT: False}
    )
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert meters_ok.await_count == 2


async def test_setup_without_tokens_needs_reauth(hass, no_polling):
    entry = v3_entry(**{CONF_REFRESH_TOKEN: ""})
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert any(
        flow["context"]["source"] == "reauth"
        for flow in hass.config_entries.flow.async_progress()
    )


async def test_setup_auth_expired_needs_reauth(hass, no_polling, meters_ok):
    meters_ok.side_effect = AuthExpiredError("dead")
    entry = v3_entry()
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_ERROR


async def test_setup_transient_retries(hass, no_polling, meters_ok, aioclient_mock):
    aioclient_mock.get(REMOTE_USER_AGENTS_URL, status=404)
    meters_ok.side_effect = TransientError("down")
    entry = v3_entry()
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_unload_and_remove_never_clear_statistics(hass, no_polling, meters_ok):
    from datetime import UTC, datetime

    sid = statistic_id("ELECTRIC", SERIAL)
    async_add_external_statistics(
        hass,
        build_metadata("ELECTRIC", SERIAL),
        [StatisticData(start=datetime(2026, 1, 1, tzinfo=UTC), state=1.0, sum=1.0)],
    )
    await async_wait_recording_done(hass)
    entry = v3_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    await async_wait_recording_done(hass)
    ids = await get_instance(hass).async_add_executor_job(list_statistic_ids, hass)
    assert sid in {row["statistic_id"] for row in ids}


async def test_migrate_v2_keeps_refresh_token(hass, no_polling, meters_ok):
    id_token = make_id_token()
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        minor_version=1,
        unique_id=INTERNAL_ID.lower(),
        data={
            "auth_implementation": "duke_energy",
            "token": {
                "access_token": "a",
                "refresh_token": "old-refresh",
                "id_token": id_token,
                "expires_at": time.time() + 100,
                "token_type": "Bearer",
            },
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.version == 3
    assert entry.data[CONF_REFRESH_TOKEN] == "old-refresh"
    assert entry.data[CONF_EMAIL] == EMAIL
    assert entry.data[CONF_INTERNAL_IDENTIFIER] == INTERNAL_ID.lower()
    assert entry.state is ConfigEntryState.LOADED


async def test_migrate_v1_needs_reauth(hass, no_polling):
    entry = MockConfigEntry(
        domain=DOMAIN, version=1, unique_id="x", data={"username": "u", "password": "p"}
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    assert entry.version == 3
    assert entry.data == {}
    assert entry.state is ConfigEntryState.SETUP_ERROR
