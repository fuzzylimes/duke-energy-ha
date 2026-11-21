from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.duke_energy.api import (
    ApiChangedError,
    AuthExpiredError,
    BlockedError,
    ClientRejectedError,
)
from custom_components.duke_energy.const import DOMAIN
from custom_components.duke_energy.coordinator import (
    DukeEnergyCoordinator,
    jitter_minutes,
)

from .fakes import TZ, FakeClient, FakeUserAgents


async def _no_sleep(_):
    return


def make(hass, client=None, agents=None):
    entry = MockConfigEntry(
        domain=DOMAIN, version=3, data={}, unique_id="u", entry_id="entry1"
    )
    entry.add_to_hass(hass)
    client = client or FakeClient()
    agents = agents or FakeUserAgents()
    return (
        DukeEnergyCoordinator(hass, entry, client, agents, sleep=_no_sleep),
        client,
        agents,
    )


def test_jitter_is_stable_and_bounded():
    day = datetime(2026, 6, 1).date()
    assert jitter_minutes("a", day) == jitter_minutes("a", day)
    assert all(0 <= jitter_minutes(str(i), day) <= 90 for i in range(200))


async def test_waits_for_morning_when_only_yesterday_missing(hass, freezer):
    freezer.move_to(datetime(2026, 6, 10, 9, tzinfo=TZ))
    coordinator, client, _ = make(hass)
    await coordinator._async_update_data()  # first run: fresh backfill
    await async_wait_recording_done(hass)
    client.calls.clear()

    freezer.move_to(datetime(2026, 6, 11, 3, tzinfo=TZ))  # 3 AM: too early
    await coordinator._async_update_data()
    assert client.calls == []

    freezer.move_to(
        datetime(2026, 6, 11, 9, 0, 1, tzinfo=TZ)
    )  # after 7 AM + max jitter
    await coordinator._async_update_data()
    await async_wait_recording_done(hass)
    assert client.calls, "should fetch once data is likely published"
    client.calls.clear()

    freezer.move_to(datetime(2026, 6, 11, 15, tzinfo=TZ))  # already complete
    await coordinator._async_update_data()
    assert client.calls == []


async def test_overdue_data_fetches_even_at_night(hass, freezer):
    freezer.move_to(datetime(2026, 6, 10, 9, tzinfo=TZ))
    coordinator, client, _ = make(hass)
    await coordinator._async_update_data()
    await async_wait_recording_done(hass)
    client.calls.clear()
    freezer.move_to(datetime(2026, 6, 13, 2, tzinfo=TZ))  # two days behind
    await coordinator._async_update_data()
    assert client.calls


async def test_keepalive_refreshes_tokens_daily(hass, freezer):
    freezer.move_to(datetime(2026, 6, 10, 9, tzinfo=TZ))
    coordinator, client, _ = make(hass)
    await coordinator._async_update_data()
    await async_wait_recording_done(hass)
    assert client.tokens.refreshes == 0
    # Nothing to fetch at 3 AM, but the last refresh is over 24 hours old.
    freezer.move_to(datetime(2026, 6, 11, 3, tzinfo=TZ))
    client.tokens.last_refresh = datetime(2026, 6, 10, 1, tzinfo=TZ).timestamp()
    await coordinator._async_update_data()
    assert client.calls[-1][1] < datetime(2026, 6, 10).date()
    assert client.tokens.refreshes == 1


async def test_auth_expired_raises_reauth(hass, freezer):
    freezer.move_to(datetime(2026, 6, 10, 9, tzinfo=TZ))
    coordinator, client, _ = make(hass)
    client.fail = lambda s, e: AuthExpiredError("dead")
    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()


@pytest.mark.parametrize(
    ("error", "issue"),
    [
        (ClientRejectedError("deny"), "client_rejected_entry1"),
        (ApiChangedError("account/usage/graph", "moved"), "api_changed_entry1"),
    ],
)
async def test_non_auth_failures_raise_repairs_not_reauth(hass, freezer, error, issue):
    freezer.move_to(datetime(2026, 6, 10, 9, tzinfo=TZ))
    coordinator, client, _ = make(hass)
    client.fail = lambda s, e: error
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue) is not None
    assert coordinator.state.backoff_until is not None

    # Success clears the issue.
    client.fail = lambda s, e: None
    coordinator.state.backoff_until = None
    await coordinator._async_update_data()
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue) is None


async def test_backoff_skips_requests(hass, freezer):
    freezer.move_to(datetime(2026, 6, 10, 9, tzinfo=TZ))
    coordinator, client, _ = make(hass)
    client.fail = lambda s, e: ApiChangedError("x", "y")
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()
    calls = len(client.calls)
    freezer.tick(timedelta(minutes=30))
    await coordinator._async_update_data()  # still backing off: no error, no calls
    assert len(client.calls) == calls


async def test_blocked_refreshes_user_agent_and_raises_issue_after_12h(hass, freezer):
    start = datetime(2026, 6, 10, 9, tzinfo=TZ)
    freezer.move_to(start)
    coordinator, client, agents = make(hass)
    client.fail = lambda s, e: BlockedError("akamai")
    for hours in (0, 6, 13):
        freezer.move_to(start + timedelta(hours=hours))
        coordinator.state.backoff_until = None
        with pytest.raises(UpdateFailed):
            await coordinator._async_update_data()
    assert True in agents.refresh_calls  # urgent check of the shared value
    assert ir.async_get(hass).async_get_issue(DOMAIN, "blocked_entry1") is not None


async def test_blocked_with_new_user_agent_clears_backoff(hass, freezer):
    freezer.move_to(datetime(2026, 6, 10, 9, tzinfo=TZ))
    coordinator, client, _ = make(hass, agents=FakeUserAgents(refresh_result=True))
    client.fail = lambda s, e: BlockedError("akamai")
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()
    assert coordinator.state.backoff_until is None
