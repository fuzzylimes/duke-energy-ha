"""Sign-in: PKCE transaction, callback parsing, Auth0 token calls (SPEC §2)."""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, urlsplit

import jwt

from .const import (
    AUTH0_CLIENT,
    AUTH0_HOST,
    AUTHORIZE_URL,
    CALLBACK_PATH,
    CLIENT_ID,
    ISSUER,
    PUBLIC_SITE_HOSTS,
    REDIRECT_URI,
    SCOPE,
    TOKEN_URL,
)
from .errors import ApiChangedError, InvalidIdTokenError, OAuthCallbackError
from .http import Transport
from .models import AuthTransaction, Identity, TokenSet

# Translation keys for OAuthCallbackError.reason (see strings.json).
REASON_INVALID_URL = "invalid_callback_url"
REASON_REJECTED = "authorize_rejected"
REASON_PUBLIC_SITE = "public_site_redirect"
REASON_STATE_MISMATCH = "state_mismatch"


def new_transaction() -> AuthTransaction:
    """Create the PKCE verifier, state, nonce and authorize URL for one attempt."""
    verifier = secrets.token_urlsafe(48)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    state = secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(24)
    url = AUTHORIZE_URL.with_query(
        {
            "client_id": CLIENT_ID,
            "scope": SCOPE,
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "response_mode": "query",
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "auth0Client": AUTH0_CLIENT,
        }
    )
    return AuthTransaction(
        authorize_url=str(url), state=state, nonce=nonce, code_verifier=verifier
    )


def parse_callback(pasted: str, expected_state: str) -> str:
    """Validate a pasted callback URL and return its authorization code.

    Raises:
        OAuthCallbackError: with a `reason` translation key.

    """
    text = pasted.strip()
    if "://" not in text and text.startswith(AUTH0_HOST):
        text = f"https://{text}"
    try:
        parts = urlsplit(text)
        host = (parts.hostname or "").lower()
        query = parse_qs(parts.query, keep_blank_values=True)
    except ValueError as err:
        raise OAuthCallbackError(REASON_INVALID_URL) from err

    if host in PUBLIC_SITE_HOSTS:
        raise OAuthCallbackError(REASON_PUBLIC_SITE)
    if parts.scheme != "https" or host != AUTH0_HOST or parts.path != CALLBACK_PATH:
        raise OAuthCallbackError(REASON_INVALID_URL)

    errors = query.get("error", [])
    if any(errors):
        description = (query.get("error_description") or [""])[0]
        raise OAuthCallbackError(REASON_REJECTED, f"{errors[0]}: {description}")

    states = query.get("state", [])
    if len(states) != 1 or not secrets.compare_digest(states[0], expected_state):
        raise OAuthCallbackError(REASON_STATE_MISMATCH)

    codes = query.get("code", [])
    if len(codes) != 1 or not codes[0]:
        raise OAuthCallbackError(REASON_INVALID_URL)
    return codes[0]


def decode_id_token(
    id_token: str, *, nonce: str | None = None, now: float | None = None
) -> dict[str, Any]:
    """Decode an ID token and check iss, aud, exp and (optionally) nonce.

    The signature isn't verified. The token comes straight from Auth0's token
    endpoint over TLS, which OIDC Core §3.1.3.7 accepts in place of a signature
    check (docs/DESIGN.md §2.3).
    """
    try:
        claims: dict[str, Any] = jwt.decode(
            id_token,
            options={"verify_signature": False, "verify_exp": False},
            algorithms=["RS256", "HS256"],
        )
    except jwt.PyJWTError as err:
        msg = "ID token is not a valid JWT"
        raise InvalidIdTokenError(msg) from err

    if claims.get("iss") != ISSUER:
        msg = "ID token has an unexpected issuer"
        raise InvalidIdTokenError(msg)
    audience = claims.get("aud")
    audiences = audience if isinstance(audience, list) else [audience]
    if CLIENT_ID not in audiences:
        msg = "ID token has an unexpected audience"
        raise InvalidIdTokenError(msg)
    exp = claims.get("exp")
    if not isinstance(exp, (int, float)):
        msg = "ID token has no expiry"
        raise InvalidIdTokenError(msg)
    if exp <= (now if now is not None else time.time()):
        msg = "ID token has already expired"
        raise InvalidIdTokenError(msg)
    if nonce is not None and not secrets.compare_digest(
        str(claims.get("nonce", "")), nonce
    ):
        msg = "ID token nonce does not match this sign-in"
        raise InvalidIdTokenError(msg)
    return claims


def identity_from_claims(claims: dict[str, Any]) -> Identity:
    """Pull the Duke user identity out of ID token claims."""
    internal = str(claims.get("internal_identifier") or "").strip()
    if not internal:
        msg = "ID token has no internal_identifier claim"
        raise InvalidIdTokenError(msg)
    return Identity(
        internal_identifier=internal,
        email=str(claims.get("email") or "").strip(),
    )


class Auth0Client:
    """Token calls against Duke's Auth0 tenant."""

    def __init__(self, transport: Transport, user_agent: Callable[[], str]) -> None:
        """Use the given transport and app User-Agent provider."""
        self._transport = transport
        self._user_agent = user_agent

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Content-Type": "application/json; charset=utf-8",
            "accept-language": "en_US",
            "auth0-client": AUTH0_CLIENT,
            "User-Agent": self._user_agent(),
        }

    async def async_exchange_code(
        self, code: str, code_verifier: str, *, nonce: str
    ) -> tuple[TokenSet, Identity]:
        """Exchange an authorization code for tokens and check the ID token."""
        payload = await self._transport.request_json(
            "POST",
            TOKEN_URL,
            endpoint="oauth/token (code)",
            headers=self._headers(),
            json_body={
                "grant_type": "authorization_code",
                "client_id": CLIENT_ID,
                "code_verifier": code_verifier,
                "code": code,
                "redirect_uri": REDIRECT_URI,
            },
            auth0=True,
        )
        tokens, claims = self._tokens_from(payload, previous_refresh=None, nonce=nonce)
        return tokens, identity_from_claims(claims)

    async def async_refresh(self, refresh_token: str) -> TokenSet:
        """Use a refresh token to get a fresh ID token."""
        payload = await self._transport.request_json(
            "POST",
            TOKEN_URL,
            endpoint="oauth/token (refresh)",
            headers=self._headers(),
            json_body={
                "grant_type": "refresh_token",
                "client_id": CLIENT_ID,
                "refresh_token": refresh_token,
            },
            auth0=True,
        )
        tokens, _ = self._tokens_from(
            payload, previous_refresh=refresh_token, nonce=None
        )
        return tokens

    @staticmethod
    def _tokens_from(
        payload: Any, *, previous_refresh: str | None, nonce: str | None
    ) -> tuple[TokenSet, dict[str, Any]]:
        if not isinstance(payload, dict) or not isinstance(
            payload.get("id_token"), str
        ):
            raise ApiChangedError("oauth/token", "response has no id_token")
        refresh = payload.get("refresh_token") or previous_refresh
        if not isinstance(refresh, str) or not refresh:
            raise ApiChangedError("oauth/token", "response has no refresh_token")
        claims = decode_id_token(payload["id_token"], nonce=nonce)
        return (
            TokenSet(
                refresh_token=refresh,
                id_token=payload["id_token"],
                id_token_exp=float(claims["exp"]),
            ),
            claims,
        )
