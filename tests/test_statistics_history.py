"""Tests of a first statistics import whose history read ends before it finds a complete hour."""
from datetime import UTC, date, datetime, timedelta
from unittest.mock import patch

import pytest
from homeassistant.components.recorder import Recorder
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.components.recorder.common import async_wait_recording_done

from custom_components.asm import statistics
from custom_components.asm.api.errors import SmartmeterConnectionError

POINT = "AT0040000000000000000000000000001"
STATISTIC = "asm:at0040000000000000000000000000001_consumption"
TODAY = date(2026, 11, 8)


@pytest.fixture
async def mock_recorder_before_hass(async_setup_recorder_instance) -> None:
    """Set up the recorder before Home Assistant, for recorder_mock."""


def _midnight(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=statistics.VIENNA)


def _intervals(first: date, last: date) -> list[dict]:
    """Return the quarter hours of the days first to last, 1 Wh each."""
    moment, end = _midnight(first).astimezone(UTC), _midnight(last + timedelta(days=1)).astimezone(UTC)
    intervals = []
    while moment < end:
        intervals.append({"start": moment.isoformat(), "wh": 1.0, "status": "VALID"})
        moment += timedelta(minutes=15)
    return intervals


def _reading(history_start: date, intervals: list[dict]) -> dict:
    return {"statistic_id": STATISTIC, "statistic_name": "Smart Meter Consumption", "interval_minutes": 15,
            "history_start": history_start.isoformat(), "intervals": intervals}


class _History:
    """Reads a month: none of its days before ``values_from`` has values; ``refusals`` months are refused once each."""

    def __init__(self, history_start: date, values_from: date, refusals: list[date]) -> None:
        self.history_start, self.values_from, self.refusals = history_start, values_from, list(refusals)
        self.firsts: list[date] = []
        self.ranges: list[tuple[date, date]] = []

    async def __call__(self, zaehlpunkt: str, first: date, last: date) -> list[dict]:
        self.firsts.append(first)
        self.ranges.append((first, last))
        if first in self.refusals:
            self.refusals.remove(first)
            raise SmartmeterConnectionError("HTTP 429 (too many requests, retry later)")
        if last < self.values_from:
            return [_reading(self.history_start, [])]
        return [_reading(self.history_start, _intervals(max(first, self.values_from), last))]


async def _import(hass: HomeAssistant, importer: statistics.StatisticsImporter, history: _History) -> list[dict]:
    """Run the import of a poll that read the last three days; return the statistic's hours."""
    poll = {POINT: {"readings": [_reading(history.history_start, _intervals(TODAY - timedelta(days=3), TODAY - timedelta(days=1)))]}}
    history.firsts.clear()
    history.ranges.clear()
    with patch.object(statistics, "_today", return_value=TODAY):
        await importer.async_import(poll)
    await async_wait_recording_done(hass)
    rows = await hass.async_add_executor_job(
        statistics_during_period, hass, _midnight(date(2026, 1, 1)), None, {STATISTIC}, "hour", None, {"sum"}
    )
    return rows.get(STATISTIC, [])


@pytest.mark.parametrize("refusals", [[date(2026, 9, 1)], [date(2026, 9, 1), date(2026, 9, 1)]])
async def test_a_refused_first_import_goes_on_after_the_empty_months(
    recorder_mock: Recorder, hass: HomeAssistant, refusals: list[date]
) -> None:
    """The months read without a complete hour are not read again; a refusal at the first month after them keeps that start."""
    history = _History(date(2026, 7, 1), date(2026, 9, 1), refusals)
    importer = statistics.StatisticsImporter(hass, history)

    assert await _import(hass, importer, history) == []
    assert history.firsts == [date(2026, 7, 1), date(2026, 8, 1), date(2026, 9, 1)]
    if len(refusals) > 1:
        assert await _import(hass, importer, history) == []
        assert history.firsts == [date(2026, 9, 1)]

    rows = await _import(hass, importer, history)
    assert history.firsts == [date(2026, 9, 1), date(2026, 10, 1), date(2026, 11, 1)]
    assert rows[0]["start"] == _midnight(date(2026, 9, 1)).timestamp()
    end = _midnight(TODAY + timedelta(days=1))
    assert rows[-1]["start"] == (end - timedelta(hours=1)).timestamp()
    assert len(rows) == (end.timestamp() - _midnight(date(2026, 9, 1)).timestamp()) // 3600
    assert rows[-1]["sum"] == pytest.approx(len(rows) * 4 / 1000)


async def test_an_empty_recent_month_is_read_again(recorder_mock: Recorder, hass: HomeAssistant) -> None:
    """A month that ended less than seven days ago may still get its values, so it is read again."""
    history = _History(date(2026, 11, 1), date(2026, 12, 1), [])
    importer = statistics.StatisticsImporter(hass, history)
    assert await _import(hass, importer, history) == []
    assert await _import(hass, importer, history) == []
    assert history.firsts == [date(2026, 11, 1)]


def test_runs_of_three_months() -> None:
    """Runs of three months start at the first day and end at the last one."""
    assert list(statistics._months(date(2025, 11, 10), date(2026, 11, 9), 3)) == [
        (date(2025, 11, 10), date(2026, 1, 31)),
        (date(2026, 2, 1), date(2026, 4, 30)),
        (date(2026, 5, 1), date(2026, 7, 31)),
        (date(2026, 8, 1), date(2026, 10, 31)),
        (date(2026, 11, 1), date(2026, 11, 9)),
    ]
    assert len(list(statistics._months(date(2023, 11, 10), date(2026, 11, 9), 3))) == 13


async def test_the_history_is_read_three_months_at_a_time(recorder_mock: Recorder, hass: HomeAssistant) -> None:
    """Each read covers three months; the empty ones before a refused read are not read again."""
    history = _History(date(2026, 1, 1), date(2026, 7, 1), [date(2026, 7, 1)])
    importer = statistics.StatisticsImporter(hass, history, months_per_read=3)

    assert await _import(hass, importer, history) == []
    assert history.ranges == [
        (date(2026, 1, 1), date(2026, 3, 31)), (date(2026, 4, 1), date(2026, 6, 30)), (date(2026, 7, 1), date(2026, 9, 30)),
    ]

    rows = await _import(hass, importer, history)
    assert history.ranges == [(date(2026, 7, 1), date(2026, 9, 30)), (date(2026, 10, 1), TODAY)]
    assert rows[0]["start"] == _midnight(date(2026, 7, 1)).timestamp()
    end = _midnight(TODAY + timedelta(days=1))
    assert len(rows) == (end.timestamp() - _midnight(date(2026, 7, 1)).timestamp()) // 3600
    assert rows[-1]["sum"] == pytest.approx(len(rows) * 4 / 1000)
