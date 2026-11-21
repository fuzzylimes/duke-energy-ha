"""Duke Energy API client.

This package must not import Home Assistant, so it can be tested and used from
scripts on its own (docs/DESIGN.md §7).
"""

from .client import DukeEnergyClient
from .errors import (
    ApiChangedError,
    AuthExpiredError,
    BlockedError,
    ClientRejectedError,
    DukeEnergyError,
    InvalidGrantError,
    InvalidIdTokenError,
    OAuthCallbackError,
    RequestRejectedError,
    TransientError,
)
from .http import Transport
from .models import (
    Account,
    AuthTransaction,
    HourlyReading,
    HourlyResult,
    Identity,
    Meter,
    TokenSet,
)
from .oauth import Auth0Client, new_transaction, parse_callback
from .tokens import TokenManager

__all__ = [
    "Account",
    "ApiChangedError",
    "Auth0Client",
    "AuthExpiredError",
    "AuthTransaction",
    "BlockedError",
    "ClientRejectedError",
    "DukeEnergyClient",
    "DukeEnergyError",
    "HourlyReading",
    "HourlyResult",
    "Identity",
    "InvalidGrantError",
    "InvalidIdTokenError",
    "Meter",
    "OAuthCallbackError",
    "RequestRejectedError",
    "TokenManager",
    "TokenSet",
    "TransientError",
    "Transport",
    "new_transaction",
    "parse_callback",
]
