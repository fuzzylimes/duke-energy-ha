import time

import pytest

from custom_components.duke_energy.api import (
    Auth0Client,
    AuthExpiredError,
    BlockedError,
    ClientRejectedError,
    TokenManager,
    TokenSet,
)
from custom_components.duke_energy.api.const import AUTH_TOKEN_URL, TOKEN_URL

from ..helpers import make_id_token
from .conftest import headers, json_body, sequence

UA = "Duke%20Energy/9999 CFNetwork/1.0 Darwin/1.0"


class Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def manager(
    transport, *, id_exp_in: float, clock: Clock, persisted: list | None = None
):
    now = clock.now
    tokens = TokenSet(
        refresh_token="r1",
        id_token=make_id_token(exp=now + id_exp_in),
        id_token_exp=now + id_exp_in,
    )
    return TokenManager(
        transport,
        Auth0Client(transport, lambda: UA),
        tokens,
        user_agent=lambda: UA,
        on_tokens_updated=persisted.append if persisted is not None else None,
        clock=clock,
    )


async def test_valid_id_token_is_reused(transport, mock_http):
    clock = Clock(time.time())
    tm = manager(transport, id_exp_in=1800, clock=clock)
    token = await tm.async_get_id_token()
    assert token == tm.tokens.id_token
    assert mock_http.call_count == 0
    assert tm.identity is not None


async def test_expiring_id_token_is_refreshed_and_persisted(transport, mock_http):
    clock = Clock(time.time())
    persisted: list[TokenSet] = []
    new_id = make_id_token(exp=clock.now + 1800)
    mock_http.post(str(TOKEN_URL), json={"id_token": new_id, "refresh_token": "r2"})
    tm = manager(transport, id_exp_in=60, clock=clock, persisted=persisted)
    assert await tm.async_get_id_token() == new_id
    assert [t.refresh_token for t in persisted] == ["r2"]
    assert tm.tokens.refresh_token == "r2"
    assert json_body(mock_http)["refresh_token"] == "r1"
    assert tm.last_refresh == clock.now


async def test_api_token_cached_by_expires_in(transport, mock_http):
    clock = Clock(time.time())
    mock_http.post(
        str(AUTH_TOKEN_URL), json={"access_token": "api-1", "expires_in": 1800}
    )
    tm = manager(transport, id_exp_in=1800, clock=clock)
    assert await tm.async_get_api_token() == "api-1"
    assert headers(mock_http)["platform"] == "iOS"
    assert headers(mock_http)["Authorization"].startswith("Basic ")
    assert json_body(mock_http) == {"idToken": tm.tokens.id_token}
    clock.now += 1000
    assert await tm.async_get_api_token() == "api-1"
    assert mock_http.call_count == 1
    clock.now += 760  # within 60 s of the API token's expiry
    mock_http.post(
        str(TOKEN_URL), json={"id_token": make_id_token(exp=clock.now + 1800)}
    )
    await tm.async_get_api_token()
    # The ID token was also near expiry, so: refresh, then exchange again.
    assert [str(call[1]) for call in mock_http.mock_calls[1:]] == [
        str(TOKEN_URL),
        str(AUTH_TOKEN_URL),
    ]


async def test_rejected_id_token_triggers_one_refresh(transport, mock_http):
    clock = Clock(time.time())
    mock_http.post(
        str(AUTH_TOKEN_URL),
        side_effect=sequence(
            {
                "status": 401,
                "json": {"returnCode": "401", "messageText": "Invalid token"},
            },
            {"json": {"access_token": "api-2"}},
        ),
    )
    mock_http.post(
        str(TOKEN_URL), json={"id_token": make_id_token(exp=clock.now + 1800)}
    )
    tm = manager(transport, id_exp_in=1800, clock=clock)
    assert await tm.async_get_api_token() == "api-2"


async def test_repeated_401_means_reauth(transport, mock_http):
    clock = Clock(time.time())
    mock_http.post(
        str(AUTH_TOKEN_URL),
        status=401,
        json={"returnCode": "401", "messageText": "Invalid token"},
    )
    mock_http.post(
        str(TOKEN_URL), json={"id_token": make_id_token(exp=clock.now + 1800)}
    )
    tm = manager(transport, id_exp_in=1800, clock=clock)
    with pytest.raises(AuthExpiredError):
        await tm.async_get_api_token()


async def test_explicit_deny_is_client_rejected(transport, mock_http):
    mock_http.post(
        str(AUTH_TOKEN_URL),
        status=403,
        json={
            "Message": "User is not authorized ... explicit deny in an identity-based policy"
        },
    )
    tm = manager(transport, id_exp_in=1800, clock=Clock(time.time()))
    with pytest.raises(ClientRejectedError):
        await tm.async_get_api_token()


async def test_akamai_block_is_blocked(transport, mock_http):
    mock_http.post(
        str(AUTH_TOKEN_URL),
        status=403,
        text="<HTML><TITLE>Access Denied</TITLE></HTML>",
        headers={"Content-Type": "text/html"},
    )
    tm = manager(transport, id_exp_in=1800, clock=Clock(time.time()))
    with pytest.raises(BlockedError):
        await tm.async_get_api_token()
