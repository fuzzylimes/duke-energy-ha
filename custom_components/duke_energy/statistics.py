"""Planning and writing external statistics (docs/COMPATIBILITY.md §3).

`plan_import` is pure: it applies the anchor, suffix and coverage rules.
The `async_*` helpers are a thin adapter over the recorder.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from homeassistant.components.recorder import get_instance  # type: ignore[attr-defined]
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    get_metadata,
    statistics_during_period,
)
from homeassistant.const import UnitOfEnergy

from .const import DOMAIN, statistic_id, statistic_name

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .api import HourlyReading

UNIT_CLASS_ENERGY = "energy"
DRIFT_TOLERANCE = 0.01
ANCHOR_SEARCH = timedelta(days=7)


@dataclass(frozen=True, slots=True)
class StoredRow:
    """A row already in the recorder. `start` is a UTC timestamp."""

    start: float
    sum: float
    state: float | None = None


@dataclass(slots=True)
class ImportPlan:
    """The rows to write, and what we learned while planning."""

    rows: list[StatisticData] = field(default_factory=list)
    drift: float | None = None
    skipped: str | None = None

    @property
    def last_start(self) -> datetime | None:
        """Newest hour this plan writes."""
        return self.rows[-1]["start"] if self.rows else None


def plan_import(
    readings: Sequence[HourlyReading],
    *,
    last: StoredRow | None,
    anchor: StoredRow | None,
    stored_after: Sequence[StoredRow] = (),
) -> ImportPlan:
    """Plan rows that continue an existing cumulative sum.

    Args:
        readings: Contiguous fetched data, in any order. The caller has already
            dropped anything after a failed window.
        last: The newest stored row, or None for a brand-new series.
        anchor: The newest stored row strictly before the first reading, or
            None if the series starts inside the fetched range. In that case
            nothing before the series' first stored hour is written.
        stored_after: Stored rows at or after the first reading. Hours Duke no
            longer returns keep their stored value, so the sum stays continuous.

    """
    by_start = {reading.start.timestamp(): reading for reading in readings}
    if not by_start:
        return ImportPlan(skipped="no_data")
    ordered = [by_start[ts] for ts in sorted(by_start)]

    if last is not None and ordered[-1].start.timestamp() < last.start:
        # Rewriting would leave stale sums after the rewritten range.
        return ImportPlan(skipped="coverage")

    base = anchor.sum if anchor is not None else 0.0
    anchor_ts = anchor.start if anchor is not None else -math.inf
    if last is not None and anchor is None and stored_after:
        # The fetched range reaches back before the series' first stored hour.
        # Writing those hours would shift every stored sum, so start at the
        # first stored row and keep its total exactly.
        first_stored = min(stored_after, key=lambda row: row.start)
        base = first_stored.sum - (first_stored.state or 0.0)
        anchor_ts = first_stored.start - 1

    values: dict[float, float] = {
        ts: reading.kwh for ts, reading in by_start.items() if ts > anchor_ts
    }
    if last is not None:
        for row in stored_after:
            if (
                anchor_ts < row.start <= last.start
                and row.start not in values
                and row.state is not None
            ):
                values[row.start] = row.state

    plan = ImportPlan()
    running = base
    sum_at_last: float | None = None
    for ts in sorted(values):
        running += values[ts]
        plan.rows.append(
            StatisticData(
                start=datetime.fromtimestamp(ts, UTC), state=values[ts], sum=running
            )
        )
        if last is not None and ts <= last.start:
            sum_at_last = running
    if last is not None and sum_at_last is not None:
        plan.drift = sum_at_last - last.sum
    return plan


def build_metadata(service_type: str, serial_number: str) -> StatisticMetaData:
    """Metadata identical to the old integration (COMPATIBILITY §2)."""
    return StatisticMetaData(
        mean_type=StatisticMeanType.NONE,
        has_sum=True,
        name=statistic_name(service_type, serial_number),
        source=DOMAIN,
        statistic_id=statistic_id(service_type, serial_number),
        unit_class=UNIT_CLASS_ENERGY,
        unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
    )


class MetadataStatus(StrEnum):
    """Result of comparing stored metadata with ours."""

    MISSING = "missing"
    OK = "ok"
    MISMATCH = "mismatch"


def compare_metadata(
    stored: StatisticMetaData | dict[str, Any] | None, expected: StatisticMetaData
) -> tuple[MetadataStatus, list[str]]:
    """Check the fields that must match. A NULL unit_class is acceptable."""
    if stored is None:
        return MetadataStatus.MISSING, []
    differences = [
        key
        for key in ("source", "unit_of_measurement", "has_sum")
        if stored.get(key) != expected[key]
    ]
    mean_type = stored.get("mean_type")
    if mean_type is not None and mean_type != StatisticMeanType.NONE:
        differences.append("mean_type")
    if stored.get("has_mean"):
        differences.append("has_mean")
    unit_class = stored.get("unit_class")
    if unit_class not in (None, UNIT_CLASS_ENERGY):
        differences.append("unit_class")
    if differences:
        return MetadataStatus.MISMATCH, differences
    return MetadataStatus.OK, []


async def async_get_metadata(
    hass: HomeAssistant, stat_id: str
) -> StatisticMetaData | None:
    """Read stored metadata for one statistic."""
    result = await get_instance(hass).async_add_executor_job(
        lambda: get_metadata(hass, statistic_ids={stat_id})
    )
    entry = result.get(stat_id)
    return entry[1] if entry else None


async def async_duke_statistic_ids(hass: HomeAssistant) -> set[str]:
    """All statistic IDs this integration's domain has ever written."""
    result = await get_instance(hass).async_add_executor_job(
        lambda: get_metadata(hass, statistic_source=DOMAIN)
    )
    return set(result)


def _row(raw: Any) -> StoredRow | None:
    start = raw.get("start")
    total = raw.get("sum")
    if start is None or total is None:
        return None
    if isinstance(start, datetime):
        start = start.timestamp()
    state = raw.get("state")
    return StoredRow(
        start=float(start),
        sum=float(total),
        state=None if state is None else float(state),
    )


async def async_last_row(hass: HomeAssistant, stat_id: str) -> StoredRow | None:
    """Newest stored row for a statistic."""
    result = await get_instance(hass).async_add_executor_job(
        get_last_statistics,
        hass,
        1,
        stat_id,
        True,  # noqa: FBT003
        {"sum", "state"},
    )
    rows = result.get(stat_id) or []
    return _row(rows[0]) if rows else None


async def async_rows(
    hass: HomeAssistant, stat_id: str, start: datetime, end: datetime | None = None
) -> list[StoredRow]:
    """Stored hourly rows with start in [start, end)."""
    result = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        start,
        end,
        {stat_id},
        "hour",
        None,
        {"sum", "state"},
    )
    rows = [_row(raw) for raw in result.get(stat_id) or []]
    return [row for row in rows if row is not None]


async def async_find_anchor(
    hass: HomeAssistant, stat_id: str, first: datetime
) -> StoredRow | None:
    """Newest stored row strictly before `first`.

    Looks back a week first, then at the whole series (rare: only after a long
    hole in the data).
    """
    for since in (first - ANCHOR_SEARCH, datetime(1970, 1, 2, tzinfo=UTC)):
        rows = await async_rows(hass, stat_id, since, first)
        before = [row for row in rows if row.start < first.timestamp()]
        if before:
            return max(before, key=lambda row: row.start)
    return None


def async_import(
    hass: HomeAssistant,
    service_type: str,
    serial_number: str,
    rows: Sequence[StatisticData],
) -> None:
    """Queue rows for the recorder. Upserts on (statistic, start)."""
    if rows:
        async_add_external_statistics(
            hass, build_metadata(service_type, serial_number), rows
        )
