import random
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.duke_energy.api import HourlyReading
from custom_components.duke_energy.statistics import StoredRow, plan_import

T0 = datetime(2026, 6, 1, 4, tzinfo=UTC)


def hour(i: int) -> datetime:
    return T0 + timedelta(hours=i)


def readings(start: int, values: list[float]) -> list[HourlyReading]:
    return [HourlyReading(hour(start + i), v) for i, v in enumerate(values)]


def stored(start: int, values: list[float], base: float) -> list[StoredRow]:
    rows, total = [], base
    for i, v in enumerate(values):
        total += v
        rows.append(StoredRow(hour(start + i).timestamp(), total, v))
    return rows


def test_fresh_series_starts_at_zero():
    plan = plan_import(readings(0, [1.0, 2.0, 0.5]), last=None, anchor=None)
    assert [r["sum"] for r in plan.rows] == [1.0, 3.0, 3.5]
    assert plan.drift is None


def test_empty_readings():
    assert plan_import([], last=None, anchor=None).skipped == "no_data"


def test_continues_from_anchor_without_restarting():
    old = stored(0, [1.0] * 10, base=500.0)  # sums 501..510, last at hour 9
    last = old[-1]
    fetched = readings(5, [1.0] * 5 + [2.0, 2.0])  # overlap hours 5-9, new 10-11
    plan = plan_import(fetched, last=last, anchor=old[4], stored_after=old[5:])
    assert plan.rows[0]["start"] == hour(5)
    assert plan.rows[0]["sum"] == pytest.approx(506.0)
    sums = {r["start"]: r["sum"] for r in plan.rows}
    assert sums[hour(9)] == pytest.approx(510.0)
    assert sums[hour(10)] == pytest.approx(512.0)
    assert sums[hour(11)] == pytest.approx(514.0)
    assert plan.drift == pytest.approx(0.0)


def test_revised_history_moves_later_sums_by_delta():
    old = stored(0, [1.0] * 10, base=0.0)
    fetched = readings(5, [1.0, 1.5, 1.0, 1.0, 1.0, 1.0])
    plan = plan_import(fetched, last=old[-1], anchor=old[4], stored_after=old[5:])
    assert plan.drift == pytest.approx(0.5)
    assert plan.rows[-1]["sum"] == pytest.approx(11.5)


def test_hole_filled_by_new_data():
    # Old parser dropped hour 6 (e.g. a zero, or the DST duplicate).
    old = [r for r in stored(0, [1.0] * 10, 0.0) if r.start != hour(6).timestamp()]
    old = [
        StoredRow(
            r.start, r.sum - (1.0 if r.start > hour(6).timestamp() else 0), r.state
        )
        for r in old
    ]
    fetched = readings(5, [1.0, 0.0, 1.0, 1.0, 1.0])
    plan = plan_import(fetched, last=old[-1], anchor=old[4], stored_after=old[5:])
    assert {r["start"] for r in plan.rows} >= {hour(6)}
    assert plan.drift == pytest.approx(0.0)


def test_stored_hour_missing_from_new_data_keeps_its_value():
    old = stored(0, [1.0] * 10, base=100.0)
    fetched = [r for r in readings(5, [1.0] * 6) if r.start != hour(7)]
    plan = plan_import(fetched, last=old[-1], anchor=old[4], stored_after=old[5:])
    sums = {r["start"]: r["sum"] for r in plan.rows}
    assert hour(7) in sums
    assert sums[hour(9)] == pytest.approx(110.0)


def test_coverage_rule_skips_when_data_ends_before_last():
    old = stored(0, [1.0] * 10, base=0.0)
    fetched = readings(3, [1.0] * 4)  # ends at hour 6 < last (9)
    plan = plan_import(fetched, last=old[-1], anchor=old[2], stored_after=old[3:])
    assert plan.skipped == "coverage"
    assert plan.rows == []


def test_gap_after_last_continues_sum():
    old = stored(0, [1.0] * 5, base=50.0)  # last hour 4, sum 55
    fetched = readings(20, [2.0, 2.0])
    plan = plan_import(fetched, last=old[-1], anchor=old[-1])
    assert [r["sum"] for r in plan.rows] == [57.0, 59.0]


def test_window_reaching_before_series_start_never_shifts_it():
    old = stored(3, [1.0] * 3, base=40.0)  # sums 41, 42, 43 at hours 3-5
    fetched = readings(0, [1.0] * 8)  # hours 0-7
    plan = plan_import(fetched, last=old[-1], anchor=None, stored_after=old)
    sums = {r["start"]: r["sum"] for r in plan.rows}
    assert min(sums) == hour(3)  # nothing before the series start
    assert sums[hour(3)] == pytest.approx(41.0)
    assert sums[hour(5)] == pytest.approx(43.0)
    assert sums[hour(7)] == pytest.approx(45.0)
    assert plan.drift == pytest.approx(0.0)


def _simulate(values: list[float], polls: list[tuple[int, int]], overlap: int) -> dict:
    """Run polls against an in-memory store; each poll gets hours [lo, hi)."""
    store: dict[float, StoredRow] = {}
    for lo_hint, hi in polls:
        last = store[max(store)] if store else None
        lo = (
            0
            if last is None
            else max(0, int((last.start - T0.timestamp()) // 3600) - overlap)
        )
        lo = min(lo, lo_hint) if last is None else lo
        fetched = readings(lo, values[lo:hi])
        if not fetched:
            continue
        first_ts = fetched[0].start.timestamp()
        before = [r for ts, r in store.items() if ts < first_ts]
        anchor = max(before, key=lambda r: r.start) if before else None
        after = [r for ts, r in sorted(store.items()) if ts >= first_ts]
        plan = plan_import(fetched, last=last, anchor=anchor, stored_after=after)
        for row in plan.rows:
            ts = row["start"].timestamp()
            store[ts] = StoredRow(ts, row["sum"], row["state"])
    return store


def test_partial_polls_converge_to_clean_import():
    rng = random.Random(1234)
    values = [round(rng.uniform(0, 3), 2) for _ in range(500)]
    for _ in range(30):
        # Each poll reaches a random point beyond the previous one (a partial
        # backfill or a normal daily poll), never going backwards.
        cuts = sorted(rng.sample(range(10, 500), 8)) + [500]
        polls = [(0, cut) for cut in cuts]
        store = _simulate(values, polls, overlap=rng.choice([0, 24, 720]))
        clean = _simulate(values, [(0, 500)], overlap=0)
        assert sorted(store) == sorted(clean)
        for ts, row in clean.items():
            assert store[ts].sum == pytest.approx(row.sum)
