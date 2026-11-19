"""Fake Duke client for integration-level tests (no HTTP)."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import date, datetime
from zoneinfo import ZoneInfo

from custom_components.duke_energy.api import (
    Account,
    DukeEnergyError,
    HourlyReading,
    HourlyResult,
    Meter,
)
from custom_components.duke_energy.api.parse import expected_hours

from .helpers import SERIAL

TZ = ZoneInfo("America/New_York")

ACCOUNT = Account("9000000001", "ISU", "9000000001", "", "BP1", "BP2", "27601")


def meter(
    serial: str = SERIAL, service_type: str = "ELECTRIC", active=date(2020, 1, 1)
) -> Meter:
    return Meter(serial, service_type, active, None, None, ACCOUNT)


class FakeTokens:
    def __init__(self) -> None:
        self.last_refresh: float | None = time.time()
        self.refreshes = 0

    async def async_refresh(self) -> None:
        self.refreshes += 1
        self.last_refresh = time.time()


class FakeClient:
    """Serves readings from `usage(start_utc) -> kWh | None`."""

    def __init__(
        self,
        meters: list[Meter] | None = None,
        usage: Callable[[datetime], float | None] = lambda _: 1.0,
        *,
        available_from: date | None = None,
        rejected_before: date | None = None,
    ) -> None:
        self.meters = meters if meters is not None else [meter()]
        self.usage = usage
        self.available_from = available_from
        self.rejected_before = rejected_before
        self.tokens = FakeTokens()
        self.calls: list[tuple[date, date]] = []
        self.fail: Callable[[date, date], DukeEnergyError | None] = lambda s, e: None
        self.meter_calls = 0

    async def async_get_all_meters(self) -> list[Meter]:
        self.meter_calls += 1
        return self.meters

    async def async_get_hourly(
        self, meter_: Meter, start: date, end: date
    ) -> HourlyResult:
        self.calls.append((start, end))
        if (error := self.fail(start, end)) is not None:
            raise error
        if self.rejected_before is not None and end < self.rejected_before:
            from custom_components.duke_energy.api import RequestRejectedError

            raise RequestRejectedError("account/usage/graph", 400, "before history")
        result = HourlyResult()
        for hour in expected_hours(start, end, TZ):
            if (
                self.available_from is not None
                and hour.local_date < self.available_from
            ):
                continue
            value = self.usage(hour.start)
            if value is not None:
                result.readings.append(HourlyReading(hour.start, value))
        return result


class FakeUserAgents:
    def __init__(self, refresh_result: bool = False) -> None:
        self.refresh_result = refresh_result
        self.refresh_calls: list[bool] = []
        self.source = "default"
        self.checked_at = None

    async def async_maybe_refresh(self, *, urgent: bool = False) -> bool:
        self.refresh_calls.append(urgent)
        return self.refresh_result if urgent else False

    def app_user_agent(self) -> str:
        return "Duke%20Energy/1 CFNetwork/1 Darwin/1"
