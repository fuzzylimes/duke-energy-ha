from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)

from custom_components.duke_energy.api import Transport


async def _no_sleep(_: float) -> None:
    return


def sequence(*responses: dict[str, Any]):
    """side_effect that returns the given responses in order (last one repeats)."""
    queue = list(responses)

    async def _side_effect(method, url, data):
        spec = queue.pop(0) if len(queue) > 1 else queue[0]
        return AiohttpClientMockResponse(method=method, url=url, **spec)

    return _side_effect


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations():
    """The API package has no Home Assistant dependency; skip hass entirely."""
    return


@pytest.fixture
def mock_http():
    return AiohttpClientMocker()


@pytest.fixture
async def session(mock_http):
    client_session = mock_http.create_session(asyncio.get_running_loop())
    yield client_session
    await client_session.close()


@pytest.fixture
def transport(session):
    return Transport(session, retry_delays=(0, 0), sleep=_no_sleep)


def json_body(mock_http, index: int = -1) -> Any:
    return mock_http.mock_calls[index][2]


def headers(mock_http, index: int = -1) -> dict[str, str]:
    return mock_http.mock_calls[index][3]
