"""HTTP transport: one place for timeouts, retries and error classification."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import aiohttp
import yarl

from .const import REQUEST_TIMEOUT
from .errors import DukeEnergyError, TransientError, classify_response, redact_body

_LOGGER = logging.getLogger(__name__)

MAX_RETRY_AFTER = 60.0


class Transport:
    """Sends requests, retries transient failures, and raises typed errors."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        retry_delays: tuple[float, ...] = (2.0, 8.0),
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """Wrap an aiohttp session."""
        self._session = session
        self._retry_delays = retry_delays
        self._sleep = sleep

    @property
    def session(self) -> aiohttp.ClientSession:
        """The underlying aiohttp session."""
        return self._session

    async def request_json(
        self,
        method: str,
        url: yarl.URL,
        *,
        endpoint: str,
        headers: Mapping[str, str],
        params: Mapping[str, str] | None = None,
        json_body: Any = None,
        auth0: bool = False,
    ) -> Any:
        """Send a request and return its decoded JSON body.

        Transient failures are retried with the configured delays. Everything
        else is raised immediately as a `DukeEnergyError` subclass.
        """
        attempt = 0
        while True:
            try:
                return await self._once(
                    method,
                    url,
                    endpoint=endpoint,
                    headers=headers,
                    params=params,
                    json_body=json_body,
                    auth0=auth0,
                )
            except TransientError as err:
                if attempt >= len(self._retry_delays):
                    raise
                delay = self._retry_delays[attempt]
                if err.retry_after is not None:
                    delay = min(max(delay, err.retry_after), MAX_RETRY_AFTER)
                attempt += 1
                _LOGGER.debug(
                    "%s failed (%s); retry %d in %.0fs", endpoint, err, attempt, delay
                )
                await self._sleep(delay)

    async def _once(
        self,
        method: str,
        url: yarl.URL,
        *,
        endpoint: str,
        headers: Mapping[str, str],
        params: Mapping[str, str] | None,
        json_body: Any,
        auth0: bool,
    ) -> Any:
        try:
            async with self._session.request(
                method,
                url,
                headers=dict(headers),
                params=dict(params) if params else None,
                json=json_body,
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
            ) as response:
                body = await response.text()
                content_type = response.headers.get("Content-Type", "")
                error = classify_response(
                    endpoint=endpoint,
                    status=response.status,
                    content_type=content_type,
                    body=body,
                    headers=dict(response.headers),
                    auth0=auth0,
                )
                if error is not None:
                    _LOGGER.debug(
                        "%s -> HTTP %s (%s): %s",
                        endpoint,
                        response.status,
                        content_type,
                        redact_body(body),
                    )
                    raise error
                _LOGGER.debug("%s -> HTTP %s", endpoint, response.status)
        except DukeEnergyError:
            raise
        except (TimeoutError, aiohttp.ClientError) as err:
            msg = f"{endpoint}: {type(err).__name__}"
            raise TransientError(msg) from err
        try:
            return json.loads(body)
        except ValueError as err:
            msg = f"{endpoint}: response is not JSON: {redact_body(body)}"
            raise TransientError(msg) from err
