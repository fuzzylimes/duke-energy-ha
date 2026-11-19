"""Error classes and the response classifier (docs/DESIGN.md §5).

Each class maps to exactly one Home Assistant outcome. The point is to send
users to re-auth only when re-auth can fix the problem.
"""

from __future__ import annotations

import json
import re
from typing import Any

_DIGIT_RUN = re.compile(r"\d{6,}")


def redact_body(text: str, limit: int = 200) -> str:
    """Shorten a response body for logs and mask long digit runs."""
    return _DIGIT_RUN.sub("******", " ".join(text.split()))[:limit]


class DukeEnergyError(Exception):
    """Base class for everything this package raises."""


class AuthExpiredError(DukeEnergyError):
    """Stored credentials no longer work; the user must sign in again."""


class InvalidGrantError(AuthExpiredError):
    """Auth0 rejected a code or refresh token (`invalid_grant` and friends)."""


class InvalidIdTokenError(DukeEnergyError):
    """The ID token from Auth0 is malformed or fails its claim checks."""


class UnauthorizedError(DukeEnergyError):
    """A Duke endpoint answered 401. Callers retry once with fresh tokens."""


class BlockedError(DukeEnergyError):
    """Duke's edge (Akamai) refused the request before it reached the API."""


class ClientRejectedError(DukeEnergyError):
    """Duke denied the app's API client credentials; needs a code update."""


class ApiChangedError(DukeEnergyError):
    """An endpoint moved or its response no longer has the expected shape."""

    def __init__(self, endpoint: str, detail: str) -> None:
        """Remember which endpoint changed."""
        super().__init__(f"{endpoint}: {detail}")
        self.endpoint = endpoint


class RequestRejectedError(ApiChangedError):
    """A JSON 4xx that is none of the cases above.

    Treated as an API change, except while probing for the start of usage
    history, where it marks the history boundary.
    """

    def __init__(self, endpoint: str, status: int, detail: str) -> None:
        """Remember the status code."""
        super().__init__(endpoint, f"HTTP {status} {detail}")
        self.status = status


class TransientError(DukeEnergyError):
    """Timeouts, connection errors, 5xx and 429. Worth retrying later."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        """Keep the server's Retry-After hint, if any."""
        super().__init__(message)
        self.retry_after = retry_after


class OAuthCallbackError(DukeEnergyError):
    """A pasted callback URL can't be used. `reason` is a translation key."""

    def __init__(self, reason: str, detail: str | None = None) -> None:
        """Store the reason key."""
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


_INVALID_GRANT_ERRORS = frozenset(
    {"invalid_grant", "unauthorized_client", "access_denied", "invalid_request"}
)


def _parse_json(body: str) -> Any:
    try:
        return json.loads(body)
    except ValueError:
        return None


def _retry_after(headers: dict[str, str]) -> float | None:
    value = headers.get("Retry-After") or headers.get("retry-after")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


def classify_response(
    *,
    endpoint: str,
    status: int,
    content_type: str,
    body: str,
    headers: dict[str, str] | None = None,
    auth0: bool = False,
) -> DukeEnergyError | None:
    """Turn an HTTP response into the error it represents, or None if OK.

    `auth0` marks responses from login.duke-energy.com, whose JSON errors use
    OAuth's `{"error": ...}` shape.
    """
    if 200 <= status < 300:
        return None
    headers = headers or {}
    short = redact_body(body)

    if status in (408, 429) or status >= 500:
        return TransientError(
            f"{endpoint}: HTTP {status} {short}", retry_after=_retry_after(headers)
        )

    payload = _parse_json(body) if "json" in content_type.lower() else None
    if payload is None:
        payload = _parse_json(body) if body.lstrip().startswith("{") else None

    if payload is None:
        # Non-JSON 4xx. Akamai answers with an HTML "Access Denied" page.
        if status in (400, 403, 406):
            return BlockedError(f"{endpoint}: HTTP {status} (non-JSON) {short}")
        return RequestRejectedError(endpoint, status, short)

    if auth0 and isinstance(payload, dict):
        error = str(payload.get("error", ""))
        if error in _INVALID_GRANT_ERRORS or status in (401, 403):
            return InvalidGrantError(f"{endpoint}: {error or status} {short}")

    if status == 401:
        return UnauthorizedError(f"{endpoint}: HTTP 401 {short}")

    text = body.lower()
    if status == 403 and "explicit deny" in text:
        return ClientRejectedError(f"{endpoint}: {short}")
    if status == 403 and "invalid key=value pair" in text:
        # AWS API Gateway's answer for a route that no longer exists.
        return ApiChangedError(endpoint, f"route rejected: {short}")
    if status == 404:
        return ApiChangedError(endpoint, f"not found: {short}")
    return RequestRejectedError(endpoint, status, short)
