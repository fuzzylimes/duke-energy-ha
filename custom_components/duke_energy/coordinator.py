"""Decides when to talk to Duke, then fetches and imports usage.

Scheduling follows docs/DESIGN.md §4.1 and imports follow
docs/COMPATIBILITY.md §3.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    ApiChangedError,
    AuthExpiredError,
    BlockedError,
    ClientRejectedError,
    DukeEnergyClient,
    DukeEnergyError,
    HourlyReading,
    Meter,
    RequestRejectedError,
)
from .api.client import windows
from .api.const import DUKE_TIMEZONE
from .api.parse import defer_trailing_zeros
from .const import (
    BACKFILL_REQUEST_SPACING,
    BACKOFF_MAX,
    BLOCKED_ISSUE_MIN_FAILURES,
    BLOCKED_ISSUE_MIN_SPAN,
    DATA_READY_HOUR,
    DOMAIN,
    HISTORY_LIMIT,
    ISSUE_API_CHANGED,
    ISSUE_BLOCKED,
    ISSUE_CLIENT_REJECTED,
    KEEPALIVE_INTERVAL,
    MAX_JITTER_MINUTES,
    METER_CACHE_TTL,
    OVERLAP,
    SUPPORTED_SERVICE_TYPE,
    TICK_INTERVAL,
    statistic_id,
)
from .repairs import (
    clear_entry_issues,
    clear_metadata_issue,
    create_entry_issue,
    create_metadata_issue,
    create_new_meter_issue,
)
from .statistics import (
    DRIFT_TOLERANCE,
    MetadataStatus,
    StoredRow,
    async_duke_statistic_ids,
    async_find_anchor,
    async_get_metadata,
    async_import,
    async_last_row,
    async_rows,
    build_metadata,
    compare_metadata,
    plan_import,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .user_agent import UserAgentResolver

_LOGGER = logging.getLogger(__name__)

type DukeEnergyConfigEntry = ConfigEntry[DukeEnergyCoordinator]


@dataclass(slots=True)
class MeterStatus:
    """What happened to one meter's statistic."""

    statistic_id: str
    last_hour: datetime | None = None
    last_sum: float | None = None
    last_result: str | None = None
    rows_written: int = 0
    rejected_days: int = 0
    invalid_values: int = 0


@dataclass(slots=True)
class DukeEnergyData:
    """Coordinator state, mostly for diagnostics."""

    meters: dict[str, MeterStatus] = field(default_factory=dict)
    last_fetch: datetime | None = None
    last_error: str | None = None
    backoff_until: datetime | None = None


def jitter_minutes(entry_id: str, day: date) -> int:
    """Stable per-entry, per-day delay so installs don't all poll at once."""
    digest = hashlib.sha256(f"{entry_id}:{day.isoformat()}".encode()).hexdigest()
    return int(digest, 16) % (MAX_JITTER_MINUTES + 1)


class DukeEnergyCoordinator(DataUpdateCoordinator[DukeEnergyData]):
    """Ticks hourly; only talks to Duke when there is data to collect."""

    config_entry: DukeEnergyConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: DukeEnergyConfigEntry,
        client: DukeEnergyClient,
        user_agents: UserAgentResolver,
        *,
        meters: list[Meter] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """Set up with an authenticated client."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=TICK_INTERVAL,
        )
        self.client = client
        self.user_agents = user_agents
        self._tz = ZoneInfo(DUKE_TIMEZONE)
        self._sleep = sleep
        self._meters = meters
        self._meters_at = dt_util.utcnow() if meters is not None else None
        self._failures = 0
        self._first_blocked: datetime | None = None
        self._blocked_count = 0
        self.state = DukeEnergyData()
        self.data = self.state

    # --- scheduling ---------------------------------------------------------

    async def _async_update_data(self) -> DukeEnergyData:
        try:
            await self.user_agents.async_maybe_refresh()
            await self._async_tick()
        except AuthExpiredError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except DukeEnergyError as err:
            await self._async_handle_failure(err)
            msg = f"{type(err).__name__}: {err}"
            raise UpdateFailed(msg) from err
        return self.state

    async def _async_tick(self) -> None:
        now = dt_util.utcnow()
        if self.state.backoff_until is not None and now < self.state.backoff_until:
            await self._async_keepalive()
            return

        meters = await self._async_supported_meters()
        local_now = now.astimezone(self._tz)
        today = local_now.date()
        target = datetime.combine(today, time(0), self._tz) - timedelta(hours=1)
        target_ts = target.timestamp()

        due: list[tuple[Meter, StoredRow | None]] = []
        for meter in meters:
            last = await async_last_row(self.hass, self._sid(meter))
            status = self._status(meter)
            if last is not None:
                status.last_hour = dt_util.utc_from_timestamp(last.start)
                status.last_sum = last.sum
            if last is None or last.start < target_ts:
                due.append((meter, last))

        if not due:
            await self._async_keepalive()
            return

        ready_at = datetime.combine(today, time(DATA_READY_HOUR), self._tz) + timedelta(
            minutes=jitter_minutes(self.config_entry.entry_id, today)
        )
        overdue = any(last is None or last.start < target_ts - 86400 for _, last in due)
        if local_now < ready_at and not overdue:
            await self._async_keepalive()
            return

        for meter, last in due:
            await self._async_update_meter(meter, last, today)

        self._async_handle_success()

    async def _async_keepalive(self) -> None:
        last = self.client.tokens.last_refresh
        if last is None or dt_util.utcnow().timestamp() - last >= (
            KEEPALIVE_INTERVAL.total_seconds()
        ):
            await self.client.tokens.async_refresh()

    async def _async_supported_meters(self) -> list[Meter]:
        now = dt_util.utcnow()
        if (
            self._meters is None
            or self._meters_at is None
            or now - self._meters_at >= METER_CACHE_TTL
        ):
            self._meters = await self.client.async_get_all_meters()
            self._meters_at = now
        return [
            meter
            for meter in self._meters
            if meter.service_type == SUPPORTED_SERVICE_TYPE
        ]

    # --- importing ------------------------------------------------------------

    @staticmethod
    def _sid(meter: Meter) -> str:
        return statistic_id(meter.service_type, meter.serial_number)

    def _status(self, meter: Meter) -> MeterStatus:
        sid = self._sid(meter)
        return self.state.meters.setdefault(sid, MeterStatus(statistic_id=sid))

    async def _async_update_meter(
        self, meter: Meter, last: StoredRow | None, today: date
    ) -> None:
        sid = self._sid(meter)
        status = self._status(meter)

        stored_meta = await async_get_metadata(self.hass, sid)
        meta_status, differences = compare_metadata(
            stored_meta, build_metadata(meter.service_type, meter.serial_number)
        )
        if meta_status is MetadataStatus.MISMATCH:
            _LOGGER.error(
                "Not importing %s: its stored metadata differs (%s)",
                sid,
                ", ".join(differences),
            )
            create_metadata_issue(self.hass, sid, ", ".join(differences))
            status.last_result = "metadata_mismatch"
            return
        clear_metadata_issue(self.hass, sid)
        if meta_status is MetadataStatus.MISSING:
            others = {
                other
                for other in await async_duke_statistic_ids(self.hass)
                if other.startswith(f"{DOMAIN}:electric_") and other != sid
            }
            if others:
                _LOGGER.warning("New meter %s found next to existing history", sid)
                create_new_meter_issue(self.hass, sid)

        yesterday = today - timedelta(days=1)
        floor = today - HISTORY_LIMIT
        if meter.agreement_active_date is not None:
            floor = max(floor, meter.agreement_active_date)

        error: DukeEnergyError | None
        if last is not None:
            last_local = dt_util.utc_from_timestamp(last.start).astimezone(self._tz)
            start = max((last_local - OVERLAP).date(), floor)
            readings, error = await self._async_fetch_forward(
                meter, start, yesterday, status
            )
        else:
            readings, error = await self._async_fetch_fresh(
                meter, floor, yesterday, status
            )
            if error is not None:
                # Nothing to anchor on, so a partial import would have to be
                # shifted later. Import nothing and retry.
                raise error

        readings = defer_trailing_zeros(readings, self._tz)
        if readings:
            await self._async_write(meter, sid, last, readings, status)
        else:
            status.last_result = "no_new_data"
        if error is not None:
            raise error

    async def _async_write(
        self,
        meter: Meter,
        sid: str,
        last: StoredRow | None,
        readings: list[HourlyReading],
        status: MeterStatus,
    ) -> None:
        first = readings[0].start
        anchor: StoredRow | None = None
        stored_after: list[StoredRow] = []
        if last is not None:
            anchor = await async_find_anchor(self.hass, sid, first)
            stored_after = await async_rows(self.hass, sid, first)
        plan = plan_import(
            readings, last=last, anchor=anchor, stored_after=stored_after
        )
        if plan.skipped:
            _LOGGER.info("Skipping import for %s this poll: %s", sid, plan.skipped)
            status.last_result = f"skipped_{plan.skipped}"
            return
        if plan.drift is not None and abs(plan.drift) > DRIFT_TOLERANCE:
            _LOGGER.info(
                "Duke data for %s changed history: total moves by %+.3f kWh",
                sid,
                plan.drift,
            )
        async_import(self.hass, meter.service_type, meter.serial_number, plan.rows)
        status.rows_written = len(plan.rows)
        status.last_result = "imported"
        if plan.rows:
            status.last_hour = plan.rows[-1]["start"]
            status.last_sum = plan.rows[-1]["sum"]
        _LOGGER.debug("Queued %d rows for %s", len(plan.rows), sid)

    async def _async_fetch_forward(
        self, meter: Meter, start: date, end: date, status: MeterStatus
    ) -> tuple[list[HourlyReading], DukeEnergyError | None]:
        """Oldest window first; stop at the first failure (keep the prefix)."""
        readings: list[HourlyReading] = []
        for index, (window_start, window_end) in enumerate(windows(start, end)):
            if index:
                await self._sleep(BACKFILL_REQUEST_SPACING)
            try:
                result = await self.client.async_get_hourly(
                    meter, window_start, window_end
                )
            except DukeEnergyError as err:
                return readings, err
            self._note(result.rejected_days, result.invalid_values, status)
            readings.extend(result.readings)
        return readings, None

    async def _async_fetch_fresh(
        self, meter: Meter, floor: date, end: date, status: MeterStatus
    ) -> tuple[list[HourlyReading], DukeEnergyError | None]:
        """Newest window first, until the start of Duke's history."""
        readings: list[HourlyReading] = []
        spans = list(reversed(windows(floor, end)))
        succeeded = False
        for index, (window_start, window_end) in enumerate(spans):
            if index:
                await self._sleep(BACKFILL_REQUEST_SPACING)
            try:
                result = await self.client.async_get_hourly(
                    meter, window_start, window_end
                )
            except RequestRejectedError as err:
                if succeeded:
                    _LOGGER.debug(
                        "History for %s starts after %s (%s)",
                        self._sid(meter),
                        window_end,
                        err,
                    )
                    break
                return [], err
            except DukeEnergyError as err:
                return [], err
            self._note(result.rejected_days, result.invalid_values, status)
            if not result.readings and succeeded:
                break
            succeeded = succeeded or bool(result.readings)
            readings.extend(result.readings)
        return readings, None

    def _note(self, rejected: list[date], invalid: int, status: MeterStatus) -> None:
        if rejected:
            _LOGGER.warning(
                "Skipped %d day(s) for %s whose hourly rows couldn't be placed: %s",
                len(rejected),
                status.statistic_id,
                ", ".join(day.isoformat() for day in rejected),
            )
        if invalid:
            _LOGGER.warning(
                "Ignored %d negative or invalid hourly value(s) for %s",
                invalid,
                status.statistic_id,
            )
        status.rejected_days += len(rejected)
        status.invalid_values += invalid

    # --- outcomes -------------------------------------------------------------

    def _async_handle_success(self) -> None:
        self._failures = 0
        self._blocked_count = 0
        self._first_blocked = None
        self.state.backoff_until = None
        self.state.last_error = None
        self.state.last_fetch = dt_util.utcnow()
        clear_entry_issues(self.hass, self.config_entry.entry_id)

    async def _async_handle_failure(self, err: DukeEnergyError) -> None:
        now = dt_util.utcnow()
        self._failures += 1
        backoff = min(TICK_INTERVAL * 2 ** (self._failures - 1), BACKOFF_MAX)
        self.state.backoff_until = now + backoff - timedelta(minutes=1)
        self.state.last_error = type(err).__name__
        entry_id = self.config_entry.entry_id

        if isinstance(err, BlockedError):
            self._blocked_count += 1
            self._first_blocked = self._first_blocked or now
            if await self.user_agents.async_maybe_refresh(urgent=True):
                # A new shared value arrived; try again on the next tick.
                self.state.backoff_until = None
            if (
                self._blocked_count >= BLOCKED_ISSUE_MIN_FAILURES
                and now - self._first_blocked >= BLOCKED_ISSUE_MIN_SPAN
            ):
                create_entry_issue(self.hass, entry_id, ISSUE_BLOCKED)
        elif isinstance(err, ClientRejectedError):
            create_entry_issue(self.hass, entry_id, ISSUE_CLIENT_REJECTED)
        elif isinstance(err, ApiChangedError):
            create_entry_issue(
                self.hass, entry_id, ISSUE_API_CHANGED, {"endpoint": err.endpoint}
            )
        _LOGGER.warning("Duke Energy update failed: %s", err)
