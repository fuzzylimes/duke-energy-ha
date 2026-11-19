from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from custom_components.duke_energy.api.models import HourlyReading
from custom_components.duke_energy.api.parse import (
    align_day,
    align_exact,
    defer_trailing_zeros,
    expected_hours,
    label_key,
    parse_value,
    to_readings,
)

from ..helpers import day_rows, hour_label

TZ = ZoneInfo("America/New_York")


def test_label_key_variants():
    assert label_key("01 AM") == (1, False)
    assert label_key("12 PM") == (12, True)
    assert label_key("1 pm") == (1, True)
    assert label_key("13 PM") is None
    assert label_key("1/02/2026") is None
    assert label_key(None) is None


def test_expected_hours_normal_day():
    hours = expected_hours(date(2026, 6, 1), date(2026, 6, 1), TZ)
    assert len(hours) == 24
    assert hours[0].start == datetime(2026, 6, 1, 4, tzinfo=UTC)
    assert hours[0].key == (12, False)
    assert hours[13].key == (1, True)


def test_expected_hours_fall_back_has_two_1am():
    hours = expected_hours(date(2025, 11, 2), date(2025, 11, 2), TZ)
    assert len(hours) == 25
    assert [h.key for h in hours[:4]] == [
        (12, False),
        (1, False),
        (1, False),
        (2, False),
    ]
    # The two 1 AM hours are an hour apart in UTC (EDT then EST).
    assert hours[2].start.timestamp() - hours[1].start.timestamp() == 3600


def test_expected_hours_spring_forward_skips_2am():
    hours = expected_hours(date(2026, 3, 8), date(2026, 3, 8), TZ)
    assert len(hours) == 23
    assert (2, False) not in [h.key for h in hours]


def test_align_exact_multi_day():
    expected = expected_hours(date(2026, 6, 1), date(2026, 6, 2), TZ)
    pairs = align_exact(day_rows() + day_rows(), expected)
    assert pairs is not None
    assert len(pairs) == 48
    assert pairs[24][0] == datetime(2026, 6, 2, 4, tzinfo=UTC)


def test_align_exact_rejects_missing_hour():
    expected = expected_hours(date(2026, 6, 1), date(2026, 6, 2), TZ)
    rows = day_rows() + day_rows()
    del rows[30]
    assert align_exact(rows, expected) is None


def test_align_exact_rejects_whole_missing_day():
    # 48 rows for a 3-day window would silently shift a day if zipped.
    expected = expected_hours(date(2026, 6, 1), date(2026, 6, 3), TZ)
    assert align_exact(day_rows() + day_rows(), expected) is None


def test_align_day_with_missing_hour():
    expected = expected_hours(date(2026, 6, 1), date(2026, 6, 1), TZ)
    rows = day_rows()
    del rows[5]
    placed = align_day(rows, expected)
    assert placed is not None
    pairs, missing = placed
    assert missing == 1
    assert expected[5].start not in [start for start, _ in pairs]
    assert pairs[5][0] == expected[6].start


def test_align_day_fall_back_full():
    expected = expected_hours(date(2025, 11, 2), date(2025, 11, 2), TZ)
    rows = [{"date": hour_label(h), "usage": "1"} for h in (0, 1, 1, *range(2, 24))]
    placed = align_day(rows, expected)
    assert placed is not None
    pairs, missing = placed
    assert missing == 0
    assert len(pairs) == 25


def test_align_day_fall_back_ambiguous_single_1am_rejected():
    expected = expected_hours(date(2025, 11, 2), date(2025, 11, 2), TZ)
    rows = [{"date": hour_label(h), "usage": "1"} for h in range(24)]
    assert align_day(rows, expected) is None


def test_align_day_rejects_extra_or_unknown_rows():
    expected = expected_hours(date(2026, 6, 1), date(2026, 6, 1), TZ)
    assert align_day(day_rows() + day_rows()[:1], expected) is None
    rows = day_rows()
    rows[3]["date"] = "garbage"
    assert align_day(rows, expected) is None


def test_align_day_out_of_order_rejected():
    expected = expected_hours(date(2026, 6, 1), date(2026, 6, 1), TZ)
    rows = day_rows()
    rows[3], rows[4] = rows[4], rows[3]
    assert align_day(rows, expected) is None


def test_parse_value():
    assert parse_value("0.59") == 0.59
    assert parse_value("0") == 0.0
    assert parse_value(1.5) == 1.5
    assert parse_value("-0.2") is None
    assert parse_value("nan") is None
    assert parse_value("inf") is None
    assert parse_value(None) is None
    assert parse_value("") is None
    assert parse_value(True) is None


def test_to_readings_counts_invalid():
    start = datetime(2026, 6, 1, 4, tzinfo=UTC)
    readings, invalid = to_readings([(start, "1.0"), (start, "-1"), (start, None)])
    assert len(readings) == 1
    assert invalid == 2


def _reading(day: int, hour: int, kwh: float) -> HourlyReading:
    local = datetime(2026, 6, day, hour, tzinfo=TZ)
    return HourlyReading(start=local.astimezone(UTC), kwh=kwh)


def test_defer_trailing_zeros_only_on_newest_day():
    readings = [
        _reading(1, 22, 0.0),
        _reading(1, 23, 0.0),
        _reading(2, 0, 0.0),
        _reading(2, 1, 1.2),
        _reading(2, 2, 0.0),
        _reading(2, 3, 0.0),
    ]
    kept = defer_trailing_zeros(readings, TZ)
    assert [r.kwh for r in kept] == [0.0, 0.0, 0.0, 1.2]


def test_defer_trailing_zeros_whole_newest_day_zero():
    readings = [_reading(1, 23, 0.7)] + [_reading(2, h, 0.0) for h in range(24)]
    kept = defer_trailing_zeros(readings, TZ)
    assert len(kept) == 1


def test_defer_trailing_zeros_keeps_interior_zeros():
    readings = [_reading(2, 0, 0.0), _reading(2, 1, 0.0), _reading(2, 2, 0.3)]
    assert len(defer_trailing_zeros(readings, TZ)) == 3
