"""Plain data types shared by the API package and the integration."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime


@dataclass(frozen=True, slots=True)
class AuthTransaction:
    """One sign-in attempt. Lives only in the config flow's memory."""

    authorize_url: str
    state: str
    nonce: str
    code_verifier: str


@dataclass(frozen=True, slots=True)
class TokenSet:
    """The Auth0 tokens we persist in the config entry."""

    refresh_token: str
    id_token: str
    id_token_exp: float


@dataclass(frozen=True, slots=True)
class Identity:
    """Who is signed in, from the ID token claims."""

    internal_identifier: str
    email: str


@dataclass(frozen=True, slots=True)
class Account:
    """A Duke account, normalized from `account/list` (SPEC §4.2)."""

    account_number: str
    src_sys_cd: str
    src_acct_id: str
    src_acct_id2: str
    primary_bp_number: str
    related_bp_number: str
    zip_code: str


@dataclass(frozen=True, slots=True)
class Meter:
    """A meter from `account-details-v2` (SPEC §4.3)."""

    serial_number: str
    service_type: str
    agreement_active_date: date | None
    agreement_end_date: date | None
    meter_cert_date: date | None
    account: Account = field(repr=False)


@dataclass(frozen=True, slots=True)
class HourlyReading:
    """Energy used during one hour. `start` is a UTC-aware top of hour."""

    start: datetime
    kwh: float


@dataclass(slots=True)
class HourlyResult:
    """Readings for a date range, plus what could not be read."""

    readings: list[HourlyReading] = field(default_factory=list)
    missing_hours: int = 0
    invalid_values: int = 0
    rejected_days: list[date] = field(default_factory=list)
