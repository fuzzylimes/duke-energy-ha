import base64
import hashlib
import json
import time
from urllib.parse import parse_qs, urlsplit

import pytest

from custom_components.duke_energy.api import (
    Auth0Client,
    InvalidGrantError,
    InvalidIdTokenError,
    OAuthCallbackError,
    new_transaction,
    parse_callback,
)
from custom_components.duke_energy.api.const import (
    CLIENT_ID,
    REDIRECT_URI,
    TOKEN_URL,
)
from custom_components.duke_energy.api.oauth import decode_id_token

from ..helpers import EMAIL, INTERNAL_ID, make_id_token
from .conftest import headers, json_body

UA = "Duke%20Energy/9999 CFNetwork/1.0 Darwin/1.0"


def _callback(**params):
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return f"{REDIRECT_URI}?{query}"


def test_new_transaction_builds_pkce_url():
    txn = new_transaction()
    query = parse_qs(urlsplit(txn.authorize_url).query)
    assert query["client_id"] == [CLIENT_ID]
    assert query["redirect_uri"] == [REDIRECT_URI]
    assert query["response_type"] == ["code"]
    assert query["state"] == [txn.state]
    assert query["nonce"] == [txn.nonce]
    assert query["code_challenge_method"] == ["S256"]
    digest = hashlib.sha256(txn.code_verifier.encode()).digest()
    assert query["code_challenge"] == [
        base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    ]
    assert "offline_access" in query["scope"][0]
    decoded = json.loads(base64.b64decode(query["auth0Client"][0] + "=="))
    assert decoded["name"] == "Auth0.swift"
    assert new_transaction().state != txn.state


def test_parse_callback_ok():
    assert parse_callback(_callback(code="abc123", state="s1"), "s1") == "abc123"


def test_parse_callback_tolerates_whitespace_and_missing_scheme():
    url = _callback(code="abc", state="s1").removeprefix("https://")
    assert parse_callback(f"  {url}\n", "s1") == "abc"


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        (_callback(code="abc", state="other"), "state_mismatch"),
        (_callback(state="s1"), "invalid_callback_url"),
        (_callback(code="", state="s1"), "invalid_callback_url"),
        (
            _callback(
                code="", state="s1", error="access_denied", error_description="x"
            ),
            "authorize_rejected",
        ),
        ("https://www.duke-energy.com/home", "public_site_redirect"),
        (
            "https://evil.example/ios/com.duke-energy.app/callback?code=a&state=s1",
            "invalid_callback_url",
        ),
        ("https://login.duke-energy.com/other?code=a&state=s1", "invalid_callback_url"),
        (
            "http://login.duke-energy.com/ios/com.duke-energy.app/callback?code=a&state=s1",
            "invalid_callback_url",
        ),
        ("not a url", "invalid_callback_url"),
    ],
)
def test_parse_callback_errors(url, reason):
    with pytest.raises(OAuthCallbackError) as err:
        parse_callback(url, "s1")
    assert err.value.reason == reason


def test_decode_id_token_checks():
    claims = decode_id_token(make_id_token(nonce="n1"), nonce="n1")
    assert claims["internal_identifier"] == INTERNAL_ID
    with pytest.raises(InvalidIdTokenError):
        decode_id_token(make_id_token(nonce="n1"), nonce="n2")
    with pytest.raises(InvalidIdTokenError):
        decode_id_token(make_id_token(iss="https://other/"))
    with pytest.raises(InvalidIdTokenError):
        decode_id_token(make_id_token(aud="someone-else"))
    with pytest.raises(InvalidIdTokenError):
        decode_id_token(make_id_token(exp=time.time() - 10))
    with pytest.raises(InvalidIdTokenError):
        decode_id_token("not-a-jwt")


async def test_exchange_code(transport, mock_http):
    id_token = make_id_token(nonce="n1")
    mock_http.post(
        str(TOKEN_URL),
        json={"id_token": id_token, "refresh_token": "r1", "access_token": "a"},
    )
    tokens, identity = await Auth0Client(transport, lambda: UA).async_exchange_code(
        "code1", "verifier1", nonce="n1"
    )
    assert tokens.refresh_token == "r1"
    assert tokens.id_token == id_token
    assert identity.internal_identifier == INTERNAL_ID
    assert identity.email == EMAIL
    assert json_body(mock_http)["grant_type"] == "authorization_code"
    assert json_body(mock_http)["code_verifier"] == "verifier1"
    assert headers(mock_http)["User-Agent"] == UA


async def test_refresh_keeps_refresh_token_when_not_rotated(transport, mock_http):
    mock_http.post(str(TOKEN_URL), json={"id_token": make_id_token()})
    tokens = await Auth0Client(transport, lambda: UA).async_refresh("r-old")
    assert tokens.refresh_token == "r-old"


async def test_refresh_uses_rotated_token(transport, mock_http):
    mock_http.post(
        str(TOKEN_URL), json={"id_token": make_id_token(), "refresh_token": "r-new"}
    )
    tokens = await Auth0Client(transport, lambda: UA).async_refresh("r-old")
    assert tokens.refresh_token == "r-new"


async def test_refresh_invalid_grant(transport, mock_http):
    mock_http.post(
        str(TOKEN_URL),
        status=403,
        json={"error": "invalid_grant", "error_description": "expired"},
    )
    with pytest.raises(InvalidGrantError):
        await Auth0Client(transport, lambda: UA).async_refresh("r-old")
