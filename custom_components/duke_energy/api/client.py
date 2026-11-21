"""Duke Energy data endpoints: accounts, meters, hourly usage (SPEC §4)."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import yarl

from .const import (
    ACCOUNT_DETAILS_URL,
    ACCOUNT_LIST_URL,
    DUKE_TIMEZONE,
    MAX_WINDOW_DAYS,
    REQUEST_DATE_FORMAT,
    USAGE_GRAPH_URL,
)
from .errors import ApiChangedError, AuthExpiredError, UnauthorizedError
from .http import Transport
from .models import Account, HourlyResult, Meter
from .parse import align_day, align_exact, expected_hours, to_readings
from .tokens import TokenManager

_LOGGER = logging.getLogger(__name__)


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _first(mapping: Mapping[str, Any], *keys: str) -> str:
    """Return the first non-empty value among `keys`."""
    for key in keys:
        if value := _text(mapping.get(key)):
            return value
    return ""


def _iso_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(_text(value)[:10])
    except ValueError:
        return None


def _request_date(value: date | None) -> str:
    return value.strftime(REQUEST_DATE_FORMAT) if value else ""


def parse_accounts(payload: Any) -> list[Account]:
    """Normalize an `account/list` response.

    The route that replaced `account-list` in 2026-10 spells BP in capitals
    and may omit the source IDs. Fall back the way every working fork does
    (SPEC §4.2).
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("accounts"), list):
        raise ApiChangedError("account/list", "response has no accounts list")
    related = _first(payload, "relatedBPNumber", "relatedBpNumber")
    accounts: list[Account] = []
    for raw in payload["accounts"]:
        if not isinstance(raw, dict):
            continue
        number = _text(raw.get("accountNumber"))
        if not number:
            raise ApiChangedError("account/list", "account without accountNumber")
        address = raw.get("serviceAddressParsed")
        zip_code = _text(address.get("zipCode")) if isinstance(address, dict) else ""
        accounts.append(
            Account(
                account_number=number,
                src_sys_cd=_first(raw, "srcSysCd") or "ISU",
                src_acct_id=_first(raw, "srcAcctId") or number,
                src_acct_id2=_first(raw, "srcAcctId2"),
                primary_bp_number=_first(raw, "primaryBPNumber", "primaryBpNumber"),
                related_bp_number=related,
                zip_code=zip_code,
            )
        )
    return accounts


def parse_meters(payload: Any, account: Account) -> list[Meter]:
    """Extract meters from an `account-details-v2` response."""
    if not isinstance(payload, dict) or not isinstance(payload.get("meterInfo"), list):
        raise ApiChangedError("account-details-v2", "response has no meterInfo list")
    meters: list[Meter] = []
    for raw in payload["meterInfo"]:
        if not isinstance(raw, dict):
            continue
        serial = _text(raw.get("serialNum"))
        if not serial:
            continue
        meters.append(
            Meter(
                serial_number=serial,
                service_type=_text(raw.get("serviceType")).upper(),
                agreement_active_date=_iso_date(raw.get("agreementActiveDate")),
                agreement_end_date=_iso_date(raw.get("agreementEndDate")),
                meter_cert_date=_iso_date(raw.get("meterCertificationDate")),
                account=account,
            )
        )
    return meters


def usage_rows(payload: Any) -> list[dict[str, Any]]:
    """Validate a usage graph response and return its rows."""
    rows = payload.get("usageArray") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ApiChangedError("account/usage/graph", "response has no usageArray")
    return rows


def windows(
    start: date, end: date, size: int = MAX_WINDOW_DAYS
) -> list[tuple[date, date]]:
    """Split [start, end] into consecutive windows of at most `size` days."""
    result: list[tuple[date, date]] = []
    current = start
    while current <= end:
        last = min(current + timedelta(days=size - 1), end)
        result.append((current, last))
        current = last + timedelta(days=1)
    return result


class DukeEnergyClient:
    """Authenticated access to Duke's data endpoints."""

    def __init__(
        self,
        transport: Transport,
        tokens: TokenManager,
        user_agent: Callable[[], str],
        *,
        now: Callable[[ZoneInfo], datetime] | None = None,
    ) -> None:
        """Use the shared transport and token manager."""
        self._transport = transport
        self._tokens = tokens
        self._user_agent = user_agent
        self._tz = ZoneInfo(DUKE_TIMEZONE)
        self._now = now or (datetime.now)

    @property
    def tokens(self) -> TokenManager:
        """The token manager in use."""
        return self._tokens

    @property
    def transport(self) -> Transport:
        """The transport in use."""
        return self._transport

    async def _request(
        self,
        method: str,
        url: yarl.URL,
        *,
        endpoint: str,
        params: Mapping[str, str] | None = None,
        json_body: Any = None,
    ) -> Any:
        """Send an authenticated request, retrying once on 401."""
        for attempt in range(2):
            token = await self._tokens.async_get_api_token(force=attempt > 0)
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
                "Accept": "application/json",
                "platform": "iOS",
                "User-Agent": self._user_agent(),
            }
            try:
                return await self._transport.request_json(
                    method,
                    url,
                    endpoint=endpoint,
                    headers=headers,
                    params=params,
                    json_body=json_body,
                )
            except UnauthorizedError:
                self._tokens.invalidate_api_token()
                if attempt:
                    msg = f"{endpoint}: still unauthorized after fresh tokens"
                    raise AuthExpiredError(msg) from None
        msg = "unreachable"  # pragma: no cover
        raise AssertionError(msg)  # pragma: no cover

    async def _identity_params(self) -> dict[str, str]:
        identity = self._tokens.identity
        if identity is None:
            # e.g. an entry migrated without a usable cached ID token.
            await self._tokens.async_get_id_token(force_refresh=True)
            identity = self._tokens.identity
        if identity is None:
            msg = "ID token has no Duke identity"
            raise AuthExpiredError(msg)
        return {"email": identity.email, "internalUserID": identity.internal_identifier}

    async def async_get_accounts(self) -> list[Account]:
        """List the signed-in user's accounts."""
        payload = await self._request(
            "GET",
            ACCOUNT_LIST_URL,
            endpoint="account/list",
            params={**await self._identity_params(), "fetchFreshData": "true"},
        )
        return parse_accounts(payload)

    async def async_get_meters(self, account: Account) -> list[Meter]:
        """List the meters on one account."""
        identity = await self._identity_params()
        payload = await self._request(
            "GET",
            ACCOUNT_DETAILS_URL,
            endpoint="account-details-v2",
            params={
                "email": identity["email"],
                "srcSysCd": account.src_sys_cd,
                "srcAcctId": account.src_acct_id,
                "primaryBpNumber": account.primary_bp_number,
                "relatedBpNumber": account.related_bp_number,
            },
        )
        return parse_meters(payload, account)

    async def async_get_all_meters(self) -> list[Meter]:
        """List every meter on every account."""
        meters: list[Meter] = []
        for account in await self.async_get_accounts():
            meters.extend(await self.async_get_meters(account))
        return meters

    def usage_body(self, meter: Meter, start: date, end: date) -> dict[str, str]:
        """Build the usage graph request body (SPEC §4.4)."""
        # Duke's app sends the selected date with the current time of day.
        now = self._now(self._tz)
        anchor = datetime(
            start.year,
            start.month,
            start.day,
            now.hour,
            now.minute,
            now.second,
            now.microsecond,
            tzinfo=self._tz,
        )
        account = meter.account
        return {
            "srcSysCd": account.src_sys_cd,
            "srcAcctId": account.src_acct_id,
            "srcAcctId2": account.src_acct_id2,
            "meterSerialNumber": meter.serial_number,
            "serviceType": meter.service_type,
            "intervalFrequency": "HOURLY",
            "periodType": "DAY",
            "date": anchor.isoformat(timespec="milliseconds"),
            "agrmtStartDt": _request_date(meter.agreement_active_date),
            "agrmtEndDt": _request_date(meter.agreement_end_date),
            "meterCertDt": _request_date(meter.meter_cert_date),
            "startDate": start.strftime(REQUEST_DATE_FORMAT),
            "endDate": end.strftime(REQUEST_DATE_FORMAT),
            "zipCode": account.zip_code,
        }

    async def async_get_usage_rows(
        self, meter: Meter, start: date, end: date
    ) -> list[dict[str, Any]]:
        """Fetch raw hourly rows for [start, end] (local dates)."""
        payload = await self._request(
            "POST",
            USAGE_GRAPH_URL,
            endpoint="account/usage/graph",
            json_body=self.usage_body(meter, start, end),
        )
        return usage_rows(payload)

    async def async_get_hourly(
        self, meter: Meter, start: date, end: date
    ) -> HourlyResult:
        """Fetch and place hourly readings for one window.

        If the window's labels don't line up exactly, it's re-fetched one day
        at a time. A day whose rows can't be placed is reported in
        `rejected_days` and contributes no readings.
        """
        result = HourlyResult()
        rows = await self.async_get_usage_rows(meter, start, end)
        expected = expected_hours(start, end, self._tz)
        # Duke's current shape leaves out the repeated fall-back hour. Also
        # accept the older shape that included it.
        duke_shape = expected_hours(start, end, self._tz, include_repeated=False)
        for candidate in (duke_shape, expected):
            pairs = align_exact(rows, candidate)
            if pairs is not None:
                result.readings, result.invalid_values = to_readings(pairs)
                result.missing_hours = len(expected) - len(pairs)
                return result

        if start == end:
            day_rows: dict[date, list[dict[str, Any]]] = {start: rows}
        else:
            _LOGGER.debug(
                "Hourly rows for %s..%s don't line up (%d rows, %d expected); "
                "re-fetching day by day",
                start,
                end,
                len(rows),
                len(expected),
            )
            day_rows = {}
            for day_start, _ in windows(start, end, size=1):
                day_rows[day_start] = await self.async_get_usage_rows(
                    meter, day_start, day_start
                )

        for day, rows_for_day in day_rows.items():
            expected_day = [hour for hour in expected if hour.local_date == day]
            placed = align_day(rows_for_day, expected_day)
            if placed is None:
                result.rejected_days.append(day)
                continue
            day_pairs, missing = placed
            readings, invalid = to_readings(day_pairs)
            result.readings.extend(readings)
            result.missing_hours += missing
            result.invalid_values += invalid
        return result
