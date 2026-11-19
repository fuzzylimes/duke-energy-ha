"""COMPATIBILITY.md §5: continuity tests against a real recorder."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from homeassistant.components.recorder.models import StatisticData, StatisticMeanType
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_metadata,
    statistics_during_period,
)
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.duke_energy.api import TransientError
from custom_components.duke_energy.const import DOMAIN, statistic_id
from custom_components.duke_energy.coordinator import DukeEnergyCoordinator
from custom_components.duke_energy.statistics import build_metadata

from .fakes import TZ, FakeClient, FakeUserAgents, meter
from .helpers import SERIAL

SID = statistic_id("ELECTRIC", SERIAL)
NOW = datetime(2026, 6, 10, 12, 0, tzinfo=TZ)  # noon ET: data for 06-09 is out


def local_hour(day: int, hour: int, month: int = 6) -> datetime:
    return datetime(2026, month, day, hour, tzinfo=TZ).astimezone(UTC)


def seed(hass, start: datetime, hours: int, *, base: float, skip=(), meta=None):
    rows, total = [], base
    for i in range(hours):
        when = start + timedelta(hours=i)
        if when in skip:
            continue
        total += 1.0
        rows.append(StatisticData(start=when, state=1.0, sum=total))
    async_add_external_statistics(
        hass, meta or build_metadata("ELECTRIC", SERIAL), rows
    )
    return rows


async def stored_rows(hass, since: datetime):
    from homeassistant.components.recorder import get_instance

    result = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        since,
        None,
        {SID},
        "hour",
        None,
        {"sum", "state"},
    )
    return result.get(SID, [])


def coordinator_for(hass, client) -> DukeEnergyCoordinator:
    entry = MockConfigEntry(domain=DOMAIN, version=3, data={}, unique_id="testuser01")
    entry.add_to_hass(hass)
    return DukeEnergyCoordinator(hass, entry, client, FakeUserAgents(), sleep=_no_sleep)


async def _no_sleep(_):
    return


@pytest.fixture
def now(freezer):
    freezer.move_to(NOW)
    return NOW


async def test_continues_existing_series_without_restart(recorder_mock, hass, now):
    start = local_hour(1, 0, month=5)
    hole = local_hour(20, 3, month=5)  # a zero hour the old parser dropped
    seeded = seed(hass, start, 36 * 24, base=1000.0, skip={hole})
    await async_wait_recording_done(hass)
    last = seeded[-1]
    assert last["start"] == local_hour(5, 23)

    client = FakeClient(usage=lambda t: 0.0 if t == hole else 1.0)
    coordinator = coordinator_for(hass, client)
    await coordinator._async_update_data()
    await async_wait_recording_done(hass)

    rows = await stored_rows(hass, start)
    by_start = {r["start"]: r for r in rows}
    assert max(by_start) == local_hour(9, 23).timestamp()
    # Rows before the overlap are untouched.
    assert by_start[seeded[0]["start"].timestamp()]["sum"] == pytest.approx(1001.0)
    # The hole is filled with the zero; no sum moves because it adds nothing.
    assert by_start[hole.timestamp()]["state"] == 0.0
    assert by_start[last["start"].timestamp()]["sum"] == pytest.approx(last["sum"])
    # New hours continue from the stored total.
    first_new = local_hour(6, 0).timestamp()
    assert by_start[first_new]["sum"] == pytest.approx(last["sum"] + 1.0)
    sums = [r["sum"] for r in sorted(rows, key=lambda r: r["start"])]
    assert sums == sorted(sums)
    # The window started 30 days before the last stored hour.
    assert (
        client.calls[0][0] == (last["start"].astimezone(TZ) - timedelta(days=30)).date()
    )

    with_metadata = await recorder_mock.async_add_executor_job(
        lambda: get_metadata(hass, statistic_ids={SID})
    )
    meta = with_metadata[SID][1]
    assert meta["source"] == DOMAIN
    assert meta["unit_of_measurement"] == "kWh"
    assert meta["has_sum"] is True
    assert meta["mean_type"] == StatisticMeanType.NONE
    assert meta["name"] == f"Duke Energy Electric {SERIAL} Consumption"


async def test_fall_back_hour_is_kept(recorder_mock, hass, freezer):
    freezer.move_to(datetime(2025, 11, 4, 12, tzinfo=TZ))
    start = datetime(2025, 10, 31, 0, tzinfo=TZ).astimezone(UTC)
    seeded = seed(hass, start, 24, base=0.0)
    await async_wait_recording_done(hass)
    coordinator = coordinator_for(hass, FakeClient())
    await coordinator._async_update_data()
    await async_wait_recording_done(hass)
    rows = await stored_rows(hass, datetime(2025, 11, 2, 0, tzinfo=TZ).astimezone(UTC))
    nov2 = [
        r
        for r in rows
        if datetime.fromtimestamp(r["start"], UTC).astimezone(TZ).date().day == 2
    ]
    assert len(nov2) == 25
    assert seeded


async def test_partial_failure_imports_prefix_then_completes(recorder_mock, hass, now):
    start = local_hour(1, 0, month=1)
    seed(hass, start, 24, base=10.0)  # last stored: 2026-01-01 23:00
    await async_wait_recording_done(hass)

    client = FakeClient()
    failed: dict = {"window": None}

    def fail(window_start, window_end):
        if window_start.month == 3 and failed["window"] is None:
            failed["window"] = window_start
            return TransientError("boom")
        return None

    client.fail = fail
    coordinator = coordinator_for(hass, client)
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()
    await async_wait_recording_done(hass)
    rows = await stored_rows(hass, start)
    newest = datetime.fromtimestamp(max(r["start"] for r in rows), UTC).astimezone(TZ)
    # Stopped exactly at the failed window: a contiguous prefix, no gap.
    assert newest == datetime.combine(
        failed["window"], datetime.min.time(), TZ
    ) - timedelta(hours=1)

    coordinator.state.backoff_until = None
    await coordinator._async_update_data()
    await async_wait_recording_done(hass)
    rows = sorted(await stored_rows(hass, start), key=lambda r: r["start"])
    assert rows[-1]["start"] == local_hour(9, 23).timestamp()
    # One clean, gap-free running total from the seeded base.
    assert rows[-1]["sum"] == pytest.approx(10.0 + len(rows))


async def test_metadata_mismatch_blocks_import(recorder_mock, hass, now):
    bad = build_metadata("ELECTRIC", SERIAL)
    bad["has_sum"] = False
    bad["mean_type"] = StatisticMeanType.ARITHMETIC
    async_add_external_statistics(
        hass,
        bad,
        [StatisticData(start=local_hour(1, 0), mean=1.0, min=1.0, max=1.0)],
    )
    await async_wait_recording_done(hass)
    client = FakeClient()
    coordinator = coordinator_for(hass, client)
    await coordinator._async_update_data()
    await async_wait_recording_done(hass)
    assert client.calls == []
    issue = ir.async_get(hass).async_get_issue(DOMAIN, f"metadata_mismatch_{SID}")
    assert issue is not None


async def test_fresh_backfill_probes_to_history_start(recorder_mock, hass, now):
    client = FakeClient(rejected_before=datetime(2026, 4, 1).date())
    coordinator = coordinator_for(hass, client)
    await coordinator._async_update_data()
    await async_wait_recording_done(hass)
    rows = sorted(
        await stored_rows(hass, local_hour(1, 0, month=1)), key=lambda r: r["start"]
    )
    first = datetime.fromtimestamp(rows[0]["start"], UTC).astimezone(TZ)
    assert first.date() >= datetime(2026, 3, 1).date()
    assert rows[0]["sum"] == pytest.approx(1.0)
    assert rows[-1]["start"] == local_hour(9, 23).timestamp()
    assert rows[-1]["sum"] == pytest.approx(len(rows))


async def test_new_meter_next_to_existing_history_raises_issue(
    recorder_mock, hass, now
):
    seed(hass, local_hour(1, 0), 24, base=0.0)
    await async_wait_recording_done(hass)
    client = FakeClient(meters=[meter("200000002")])
    coordinator = coordinator_for(hass, client)
    await coordinator._async_update_data()
    new_sid = statistic_id("ELECTRIC", "200000002")
    assert (
        ir.async_get(hass).async_get_issue(DOMAIN, f"new_meter_{new_sid}") is not None
    )
