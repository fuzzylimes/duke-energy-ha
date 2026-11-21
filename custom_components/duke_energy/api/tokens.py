"""Token lifecycle: Auth0 ID token refresh and the Duke API token exchange.

Auth0's ID token lasts 30 minutes and is what Duke's `login/auth-token`
endpoint accepts, so refresh is driven by the ID token's expiry, not the
access token's (docs/SPEC.md §2.4, docs/DESIGN.md §3).
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from .const import (
    API_CLIENT_ID,
    API_CLIENT_SECRET,
    API_TOKEN_REFRESH_MARGIN,
    AUTH_TOKEN_URL,
    DEFAULT_API_TOKEN_LIFETIME,
    ID_TOKEN_REFRESH_MARGIN,
)
from .errors import ApiChangedError, AuthExpiredError, UnauthorizedError
from .http import Transport
from .models import Identity, TokenSet
from .oauth import Auth0Client, decode_id_token, identity_from_claims

_LOGGER = logging.getLogger(__name__)

TokensUpdatedCallback = Callable[[TokenSet], Awaitable[None] | None]

_BASIC = base64.b64encode(f"{API_CLIENT_ID}:{API_CLIENT_SECRET}".encode()).decode()


class TokenManager:
    """Hands out valid ID and Duke API tokens, refreshing as needed."""

    def __init__(
        self,
        transport: Transport,
        auth0: Auth0Client,
        tokens: TokenSet,
        *,
        user_agent: Callable[[], str],
        on_tokens_updated: TokensUpdatedCallback | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Start from persisted tokens."""
        self._transport = transport
        self._auth0 = auth0
        self._tokens = tokens
        self._user_agent = user_agent
        self._on_tokens_updated = on_tokens_updated
        self._clock = clock
        self._lock = asyncio.Lock()
        self._api_token: str | None = None
        self._api_token_expires = 0.0
        self._last_refresh: float | None = None
        self._identity = self._identity_from(tokens.id_token)

    @property
    def tokens(self) -> TokenSet:
        """Current Auth0 tokens."""
        return self._tokens

    @property
    def identity(self) -> Identity | None:
        """Signed-in identity, if the ID token carried it."""
        return self._identity

    @property
    def last_refresh(self) -> float | None:
        """When Auth0 tokens were last refreshed in this session."""
        return self._last_refresh

    @staticmethod
    def _identity_from(id_token: str) -> Identity | None:
        try:
            claims = decode_id_token(id_token, now=0)
            return identity_from_claims(claims)
        except Exception:  # noqa: BLE001
            return None

    async def async_refresh(self) -> TokenSet:
        """Refresh Auth0 tokens now (also used as a keep-alive)."""
        async with self._lock:
            return await self._refresh_locked()

    async def _refresh_locked(self) -> TokenSet:
        new = await self._auth0.async_refresh(self._tokens.refresh_token)
        if new.refresh_token != self._tokens.refresh_token:
            _LOGGER.debug("Auth0 rotated the refresh token")
        self._tokens = new
        self._last_refresh = self._clock()
        self._api_token = None
        if (identity := self._identity_from(new.id_token)) is not None:
            self._identity = identity
        # Persist before anyone uses the new tokens, in case rotation is on.
        if self._on_tokens_updated is not None:
            result = self._on_tokens_updated(new)
            if inspect.isawaitable(result):
                await result
        return new

    async def async_get_id_token(self, *, force_refresh: bool = False) -> str:
        """Return an ID token valid for at least a few more minutes."""
        async with self._lock:
            if force_refresh or (
                self._tokens.id_token_exp - self._clock() < ID_TOKEN_REFRESH_MARGIN
            ):
                await self._refresh_locked()
            return self._tokens.id_token

    async def async_get_api_token(self, *, force: bool = False) -> str:
        """Return a Duke API token, exchanging the ID token when needed."""
        if (
            not force
            and self._api_token is not None
            and self._api_token_expires - self._clock() > API_TOKEN_REFRESH_MARGIN
        ):
            return self._api_token

        id_token = await self.async_get_id_token(force_refresh=force)
        try:
            payload = await self._exchange(id_token)
        except UnauthorizedError:
            # The ID token was rejected; one fresh one is worth a try.
            id_token = await self.async_get_id_token(force_refresh=True)
            try:
                payload = await self._exchange(id_token)
            except UnauthorizedError as err:
                msg = "Duke rejected a freshly refreshed ID token"
                raise AuthExpiredError(msg) from err

        token = payload.get("access_token") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token:
            raise ApiChangedError("login/auth-token", "response has no access_token")
        try:
            lifetime = float(payload.get("expires_in") or DEFAULT_API_TOKEN_LIFETIME)
        except (TypeError, ValueError):
            lifetime = DEFAULT_API_TOKEN_LIFETIME
        # issued_at is ignored on purpose: its units are unknown (SPEC §3.2).
        self._api_token = token
        self._api_token_expires = self._clock() + lifetime
        internal = payload.get("internalUserID")
        if isinstance(internal, str) and internal and self._identity is not None:
            self._identity = Identity(internal, self._identity.email)
        return token

    def invalidate_api_token(self) -> None:
        """Forget the cached Duke API token."""
        self._api_token = None

    async def _exchange(self, id_token: str) -> Any:
        return await self._transport.request_json(
            "POST",
            AUTH_TOKEN_URL,
            endpoint="login/auth-token",
            headers={
                "Authorization": f"Basic {_BASIC}",
                "Content-Type": "application/json; charset=utf-8",
                "Accept": "application/json",
                "platform": "iOS",
                "User-Agent": self._user_agent(),
            },
            json_body={"idToken": id_token},
        )
