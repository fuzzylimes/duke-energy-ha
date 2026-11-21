"""Turn Duke's hourly `usageArray` rows into timestamped readings.

Hourly rows are labeled only "12 AM" ... "11 PM", with no date (SPEC §4.4).
Timestamps are inferred from position, so this module validates strictly
instead of guessing (docs/DESIGN.md §4.3):

* A multi-day window is accepted only if every label matches the expected
  sequence exactly.
* A single day may have missing hours, as long as the remaining rows can be
  placed unambiguously.

DST, as observed live on 2026-03-08 and 2026-11-01 (docs/SPEC.md §10):

* Spring-forward days have 23 rows, with no "02 AM".
* Fall-back days have 24 rows, with a single "01 AM". Duke leaves out the
  repeated hour; its pre-2025 response sent two "01 AM" rows instead. The
  single "01 AM" is the first 1 AM (EDT), and the repeated hour is missing.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .models import HourlyReading

_LABEL = re.compile(r"^\s*(\d{1,2})\s*([AaPp])\.?\s*[Mm]\.?\s*$")

LabelKey = tuple[int, bool]


@dataclass(frozen=True, slots=True)
class ExpectedHour:
    """One hour we expect Duke to report."""

    start: datetime  # UTC
    key: LabelKey  # (hour on a 12-hour clock, is_pm)
    local_date: date
    repeated: bool = False  # second occurrence of a fall-back wall-clock hour


def label_key(label: Any) -> LabelKey | None:
    """Parse "01 AM" style labels into a comparable key."""
    if not isinstance(label, str):
        return None
    match = _LABEL.match(label)
    if match is None:
        return None
    hour = int(match.group(1))
    if not 1 <= hour <= 12:
        return None
    return hour, match.group(2).upper() == "P"


def _key_for(local: datetime) -> LabelKey:
    return (local.hour % 12 or 12, local.hour >= 12)


def expected_hours(
    start: date, end: date, tz: ZoneInfo, *, include_repeated: bool = True
) -> list[ExpectedHour]:
    """List every real hour from local midnight on `start` through `end`.

    Steps in UTC, so a fall-back day has 25 hours (two "01 AM") and a
    spring-forward day has 23 (no "02 AM"). With `include_repeated=False`, the
    repeated fall-back hour is left out, matching what Duke sends today.
    """
    current = datetime(start.year, start.month, start.day, tzinfo=tz).astimezone(UTC)
    after = end + timedelta(days=1)
    stop = datetime(after.year, after.month, after.day, tzinfo=tz).astimezone(UTC)
    hours: list[ExpectedHour] = []
    while current < stop:
        local = current.astimezone(tz)
        repeated = local.fold == 1
        if include_repeated or not repeated:
            hours.append(ExpectedHour(current, _key_for(local), local.date(), repeated))
        current += timedelta(hours=1)
    return hours


def align_exact(
    rows: Sequence[dict[str, Any]], expected: Sequence[ExpectedHour]
) -> list[tuple[datetime, Any]] | None:
    """Zip rows to expected hours if labels match one-for-one, else None."""
    if len(rows) != len(expected):
        return None
    pairs: list[tuple[datetime, Any]] = []
    for row, hour in zip(rows, expected, strict=True):
        if label_key(row.get("date")) != hour.key:
            return None
        pairs.append((hour.start, row.get("usage")))
    return pairs


def align_day(
    rows: Sequence[dict[str, Any]], expected: Sequence[ExpectedHour]
) -> tuple[list[tuple[datetime, Any]], int] | None:
    """Place one day's rows, allowing missing hours.

    Returns the placed pairs and the number of missing hours, or None if the
    rows can't be placed unambiguously.
    """
    if len(rows) > len(expected):
        return None
    parsed = [label_key(row.get("date")) for row in rows]
    keys = [key for key in parsed if key is not None]
    if len(keys) != len(parsed):
        return None

    # On a fall-back day "01 AM" can occur twice. Duke now sends it once; the
    # in-order placement below then puts it on the first (EDT) hour and
    # leaves the repeated hour missing.
    wanted = Counter(hour.key for hour in expected)
    got = Counter(keys)
    for key, count in got.items():
        if count > wanted.get(key, 0):
            return None

    pairs: list[tuple[datetime, Any]] = []
    position = 0
    for row, key in zip(rows, keys, strict=True):
        while position < len(expected) and expected[position].key != key:
            position += 1
        if position == len(expected):
            return None
        pairs.append((expected[position].start, row.get("usage")))
        position += 1
    return pairs, len(expected) - len(pairs)


def parse_value(raw: Any) -> float | None:
    """Parse a usage value. NaN, infinity and negatives are rejected."""
    if isinstance(raw, bool):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return value


def to_readings(
    pairs: Sequence[tuple[datetime, Any]],
) -> tuple[list[HourlyReading], int]:
    """Convert placed pairs to readings. Returns (readings, invalid count)."""
    readings: list[HourlyReading] = []
    invalid = 0
    for start, raw in pairs:
        value = parse_value(raw)
        if value is None:
            invalid += 1
            continue
        readings.append(HourlyReading(start=start, kwh=value))
    return readings, invalid


def defer_trailing_zeros(
    readings: Sequence[HourlyReading], tz: ZoneInfo
) -> list[HourlyReading]:
    """Drop a trailing run of zeros on the newest local day.

    Duke may publish a day late, and those hours shouldn't be recorded as
    zero. Real zeros get written on a later poll, once newer data follows them.
    """
    ordered = sorted(readings, key=lambda reading: reading.start)
    if not ordered:
        return ordered
    newest_day = ordered[-1].start.astimezone(tz).date()
    end = len(ordered)
    while (
        end > 0
        and ordered[end - 1].kwh == 0
        and ordered[end - 1].start.astimezone(tz).date() == newest_day
    ):
        end -= 1
    return ordered[:end]
