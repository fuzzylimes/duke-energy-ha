"""Shared test helpers. All identifiers here are synthetic."""

from __future__ import annotations

import time
from typing import Any

import jwt

from custom_components.duke_energy.api.const import CLIENT_ID, ISSUER

INTERNAL_ID = "TESTUSER01"
EMAIL = "someone@example.invalid"
SERIAL = "100000001"


def make_id_token(
    *,
    exp: float | None = None,
    nonce: str | None = None,
    iss: str = ISSUER,
    aud: str = CLIENT_ID,
    internal: str | None = INTERNAL_ID,
    email: str = EMAIL,
) -> str:
    claims: dict[str, Any] = {
        "iss": iss,
        "aud": aud,
        "sub": "auth0|test",
        "exp": int(exp if exp is not None else time.time() + 1800),
        "email": email,
    }
    if internal is not None:
        claims["internal_identifier"] = internal
    if nonce is not None:
        claims["nonce"] = nonce
    return jwt.encode(
        claims, "test-secret-not-real-0123456789abcdef", algorithm="HS256"
    )


def hour_label(hour: int) -> str:
    return f"{hour % 12 or 12:02d} {'PM' if hour >= 12 else 'AM'}"


def day_rows(values: list[float] | None = None) -> list[dict[str, Any]]:
    values = values if values is not None else [0.5] * 24
    return [
        {"date": hour_label(hour), "usage": str(value), "temperatureAvg": 50}
        for hour, value in enumerate(values)
    ]
