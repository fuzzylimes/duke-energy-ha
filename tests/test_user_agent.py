from __future__ import annotations

from datetime import timedelta

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.duke_energy.api.const import DEFAULT_APP_USER_AGENT
from custom_components.duke_energy.const import (
    DOMAIN,
    OPT_APP_USER_AGENT,
    OPT_REMOTE_USER_AGENT,
    REMOTE_USER_AGENTS_URL,
)
from custom_components.duke_energy.user_agent import (
    UserAgentResolver,
    async_user_agent_for_flow,
    valid_remote_user_agent,
)

NEW = "Duke%20Energy/1500 CFNetwork/4000.1 Darwin/28.0.0"


def entry(hass, **options):
    e = MockConfigEntry(domain=DOMAIN, version=3, data={}, options=options)
    e.add_to_hass(hass)
    return e


def test_remote_validation():
    assert valid_remote_user_agent(NEW)
    assert valid_remote_user_agent(DEFAULT_APP_USER_AGENT)
    assert not valid_remote_user_agent("Mozilla/5.0")
    assert not valid_remote_user_agent(NEW + "\r\nX-Evil: 1")
    assert not valid_remote_user_agent(None)


async def test_default_then_remote(hass, aioclient_mock):
    aioclient_mock.get(REMOTE_USER_AGENTS_URL, json={"app_user_agent": NEW})
    resolver = UserAgentResolver(hass, entry(hass))
    await resolver.async_load()
    assert resolver.app_user_agent() == DEFAULT_APP_USER_AGENT
    assert await resolver.async_maybe_refresh()
    assert resolver.app_user_agent() == NEW
    assert resolver.source == "remote"


async def test_weekly_and_urgent_gating(hass, aioclient_mock, freezer):
    aioclient_mock.get(REMOTE_USER_AGENTS_URL, json={"app_user_agent": NEW})
    resolver = UserAgentResolver(hass, entry(hass))
    await resolver.async_maybe_refresh()
    assert aioclient_mock.call_count == 1
    freezer.tick(timedelta(days=6))
    await resolver.async_maybe_refresh()
    assert aioclient_mock.call_count == 1
    await resolver.async_maybe_refresh(urgent=True)
    assert aioclient_mock.call_count == 2
    await resolver.async_maybe_refresh(urgent=True)  # within the hour
    assert aioclient_mock.call_count == 2
    freezer.tick(timedelta(days=8))
    await resolver.async_maybe_refresh()
    assert aioclient_mock.call_count == 3


async def test_invalid_remote_ignored(hass, aioclient_mock):
    aioclient_mock.get(REMOTE_USER_AGENTS_URL, json={"app_user_agent": "curl/8"})
    resolver = UserAgentResolver(hass, entry(hass))
    assert not await resolver.async_maybe_refresh()
    assert resolver.app_user_agent() == DEFAULT_APP_USER_AGENT


async def test_remote_cached_across_restarts(hass, aioclient_mock):
    aioclient_mock.get(REMOTE_USER_AGENTS_URL, json={"app_user_agent": NEW})
    e = entry(hass)
    await UserAgentResolver(hass, e).async_maybe_refresh()
    again = UserAgentResolver(hass, e)
    await again.async_load()
    assert again.app_user_agent() == NEW


async def test_disabled_never_contacts_github(hass, aioclient_mock):
    resolver = UserAgentResolver(hass, entry(hass, **{OPT_REMOTE_USER_AGENT: False}))
    await resolver.async_maybe_refresh(urgent=True)
    assert aioclient_mock.call_count == 0
    assert resolver.app_user_agent() == DEFAULT_APP_USER_AGENT


async def test_override_wins_and_skips_github(hass, aioclient_mock):
    resolver = UserAgentResolver(hass, entry(hass, **{OPT_APP_USER_AGENT: "Custom/1"}))
    await resolver.async_maybe_refresh(urgent=True)
    assert aioclient_mock.call_count == 0
    assert resolver.app_user_agent() == "Custom/1"
    assert resolver.source == "override"


async def test_flow_user_agent(hass, aioclient_mock):
    aioclient_mock.get(REMOTE_USER_AGENTS_URL, status=500)
    assert await async_user_agent_for_flow(hass, None) == DEFAULT_APP_USER_AGENT
    aioclient_mock.clear_requests()
    aioclient_mock.get(REMOTE_USER_AGENTS_URL, json={"app_user_agent": NEW})
    assert await async_user_agent_for_flow(hass, None) == NEW
    assert (
        await async_user_agent_for_flow(
            hass, entry(hass, **{OPT_REMOTE_USER_AGENT: False})
        )
        == DEFAULT_APP_USER_AGENT
    )
