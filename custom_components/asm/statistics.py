"""Long-term statistics for readings whose values arrive late.

Some portals publish a day's values only on the next day (E-Werk Gösting: by
12:00 at the latest, usually earlier). An entity's state is recorded when it is
polled, so statistics compiled from it would put a day-old value at the wrong
time, and they could not hold the history that the portal already has.

A reading that carries a ``statistic_id`` and its ``intervals`` is therefore
imported as an external statistic (source ``asm``) with the values' real
timestamps. The import is hourly, because Home Assistant's long-term statistics
are hourly: the intervals of an hour are summed. ``state`` is the hour's energy,
``sum`` the running total, both in kWh. That statistic is what the Energy
dashboard uses.

The first import reads the whole history from the reading's ``history_start``,
at most three years back. Later imports continue after the last imported hour.
A missing or incomplete hour holds the import back until the portal delivers it.
It is skipped, with a warning, once newer values exist for more than seven days
after it. What could not be read is never taken for a missing hour: a failed read
ends the reading, and a reading that carries an ``error`` (a register the client
could not read) is not imported this time - both are tried again with the next
poll, and nothing is skipped over them.

Every poll also compares its own values with the imported hours up to the last
one - the values it read, never those of a history read. An imported hour whose
value the portal has changed since, or one that is missing although it is complete
now, is corrected: every hour from the earliest of them to the last imported one is
written again, in one recorder call (a correction is written as a whole), with
running sums that go on from the row before it; hours that the recorder holds but
the poll has not keep their values. E-Werk Gösting's poll reads the last four days,
and the settled ones among them count (yesterday from 12:00 on): a day's values can
be corrected from 12:00 of the following day until the end of the third day after
it, about two and a half days.
"""
from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
    valid_statistic_id,
)
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.util.unit_conversion import EnergyConverter

from .api.errors import SmartmeterError
from .const import DOMAIN, LOGGER

# The days of the portals are Austrian days.
VIENNA = ZoneInfo("Europe/Vienna")

HOUR = 3600  # seconds

# A missing or incomplete hour holds the import back (the portal may still
# deliver it) until the newest complete hour lies more than this after it.
GAP_LIMIT = 7 * 24 * HOUR

# The first import reads no further back than this.
MAX_HISTORY_YEARS = 3

# An imported hour (kWh, written with 3 decimals) whose value differs from the
# portal's current one by more than this is corrected.
CORRECTION_TOLERANCE = 0.0005

# Reads the readings of a metering point for the days first to last (inclusive).
ReadHistory = Callable[[str, date, date], Awaitable[list[dict[str, Any]]]]


def _today() -> date:
    """Return the current day in Austria."""
    return datetime.now(VIENNA).date()


def _midnight(day: date) -> int:
    """Return the start of an Austrian day, in epoch seconds."""
    return int(datetime(day.year, day.month, day.day, tzinfo=VIENNA).timestamp())


def _local(instant: int) -> str:
    """Return an instant in Austrian time, for the log."""
    return datetime.fromtimestamp(instant, VIENNA).isoformat(timespec="minutes")


def _years_before(day: date, years: int) -> date:
    """Return the same day ``years`` years earlier (28 February for 29 February)."""
    try:
        return day.replace(year=day.year - years)
    except ValueError:
        return day.replace(year=day.year - years, day=28)


def _months(first: date, last: date) -> Iterator[tuple[date, date]]:
    """Split the days ``first`` to ``last`` (inclusive) into calendar months."""
    while first <= last:
        following = (first.replace(day=1) + timedelta(days=32)).replace(day=1)
        yield first, min(following - timedelta(days=1), last)
        first = following


def _day(value: Any) -> date | None:
    """Return an ISO day ("2024-08-01") as a date, None if it is none."""
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


def _instant(value: Any) -> int | None:
    """Return an ISO timestamp with UTC offset in epoch seconds, else None."""
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    if moment.utcoffset() is None:
        return None
    return int(moment.timestamp())


def _intervals_per_hour(reading: Mapping[str, Any]) -> int | None:
    """Return how many intervals make a complete hour (4 for 15 minutes)."""
    minutes = reading.get("interval_minutes", 15)
    if (
        isinstance(minutes, int)
        and not isinstance(minutes, bool)
        and 0 < minutes <= 60
        and 60 % minutes == 0
    ):
        return 60 // minutes
    return None


def statistic_readings(
    data: Mapping[str, Any],
) -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield (metering point, reading) for the readings of a poll to import."""
    for zaehlpunkt, item in (data or {}).items():
        readings = item.get("readings") if isinstance(item, dict) else None
        if isinstance(readings, dict):
            readings = [readings]
        for reading in readings or []:
            if (
                isinstance(reading, dict)
                and reading.get("statistic_id")
                and isinstance(reading.get("intervals"), list)
            ):
                yield zaehlpunkt, reading


def complete_hours(
    intervals: Iterable[Any], per_hour: int, since: int
) -> tuple[dict[int, float], int]:
    """Sum intervals per UTC hour; return the complete hours from ``since`` on.

    An interval belongs to the hour in which it starts; an hour is complete with
    exactly ``per_hour`` distinct intervals (the same start counts once). Returns
    {hour start in epoch seconds: Wh} and the number of unusable intervals.
    """
    hours: dict[int, dict[int, float]] = {}
    unusable = 0
    for interval in intervals:
        if not isinstance(interval, dict):
            unusable += 1
            continue
        start = _instant(interval.get("start"))
        energy = interval.get("wh")
        if (
            start is None
            or isinstance(energy, bool)
            or not isinstance(energy, (int, float))
            or not math.isfinite(energy)
        ):
            unusable += 1
            continue
        hour = start - start % HOUR
        if hour >= since:
            hours.setdefault(hour, {}).setdefault(start, float(energy))
    complete = {
        hour: sum(of_hour.values())
        for hour, of_hour in hours.items()
        if len(of_hour) == per_hour
    }
    return complete, unusable


def plan_hours(
    hours: Mapping[int, float], after: int | None
) -> tuple[list[tuple[int, float]], list[tuple[int, int]]]:
    """Return the hours to import, oldest first, and the gaps skipped on the way.

    ``hours`` are complete hours (start in epoch seconds: Wh). The import goes on
    from the hour after ``after``, the last imported one; without one, from the
    first complete hour (what lies before it is no gap). At a missing or
    incomplete hour it stops, unless the newest complete hour lies more than
    GAP_LIMIT after it: then it goes on at the next complete hour, and the gap
    (first missing hour, next complete hour) is returned.
    """
    ordered = sorted(hours)
    if not ordered:
        return [], []
    newest = ordered[-1]
    expected = ordered[0] if after is None else after + HOUR
    rows: list[tuple[int, float]] = []
    gaps: list[tuple[int, int]] = []
    for hour in ordered:
        if hour < expected:
            continue
        if hour != expected:
            if newest - expected <= GAP_LIMIT:
                break
            gaps.append((expected, hour))
        rows.append((hour, hours[hour]))
        expected = hour + HOUR
    return rows, gaps


def plan_corrections(
    stored: Mapping[int, float | None], current: Mapping[int, float], last: int
) -> tuple[list[int], list[tuple[int, float | None]], list[int]]:
    """Return the imported hours to correct and the hours to write again.

    ``stored`` are the recorder's hours up to ``last``, the last imported one
    (start in epoch seconds: kWh), ``current`` the complete hours of the poll
    (start: Wh). An hour is stale when its stored value differs from the current
    one by more than CORRECTION_TOLERANCE, or when it is missing in the recorder
    although it is complete now and lies before ``last``. From the earliest stale
    hour to ``last``, every hour that the recorder holds or the poll has complete
    is written again, oldest first, as (start, Wh): with the current value where
    the poll has one, else with the stored one, which is kept - None for a stored
    hour without a value, which stays without one. Returns the stale hours, the
    hours to write and the kept ones - all empty when none is stale.
    """
    stale: list[int] = []
    for hour, energy in current.items():
        if hour > last:
            continue  # a new hour: imported after the last one, not corrected
        if hour not in stored:
            if hour < last:
                stale.append(hour)
            continue
        value = stored[hour]
        if value is None or abs(value - round(energy / 1000, 3)) > CORRECTION_TOLERANCE:
            stale.append(hour)
    if not stale:
        return [], [], []
    stale.sort()
    rows: list[tuple[int, float | None]] = []
    kept: list[int] = []
    for hour in sorted({*stored, *current}):
        if not stale[0] <= hour <= last:
            continue
        if hour in current:
            rows.append((hour, current[hour]))
        else:
            value = stored[hour]
            rows.append((hour, None if value is None else round(value * 1000, 3)))
            kept.append(hour)
    return stale, rows, kept


def _statistic(hour: int, energy: float | None, total: float) -> StatisticData:
    """Return the row of an hour: its energy and the running sum, in kWh.

    An hour without a known energy (a kept row that had none) gets no state.
    """
    row = StatisticData(
        start=datetime.fromtimestamp(hour, UTC), sum=round(total / 1000, 3)
    )
    if energy is not None:
        row["state"] = round(energy / 1000, 3)
    return row


def _sum_of(statistic_id: str, row: Mapping[str, Any]) -> float:
    """Return a stored row's sum; a row without one counts as 0 (logged)."""
    value = row.get("sum")
    if value is None:
        LOGGER.debug(
            "%s: the row of %s has no sum; the running sums after it start from 0",
            statistic_id, _local(int(row["start"])),
        )
        return 0.0
    return float(value)


@dataclass
class _Job:
    """The import of one statistic."""

    reading: dict[str, Any]
    statistic_id: str
    name: str
    per_hour: int
    after: int | None  # the last imported hour (epoch seconds), None: none yet
    total: float  # the sum of the last imported hour, in Wh
    first_day: date  # the first day to read
    since: int  # the first hour that may be imported (epoch seconds)
    earliest: int  # the history start (epoch seconds): nothing before it is imported
    hours: dict[int, float] = field(default_factory=dict)
    unusable: int = 0
    read: bool = False  # True when the poll's intervals did not suffice
    # (first day, last day, reason) of a month whose reading carried an error:
    # nothing of this job is imported in this run
    failed: tuple[date, date, str] | None = None
    # The imported hours whose values changed (see plan_corrections), the hours to
    # write again from the earliest of them to ``after`` (start, Wh), the kept
    # ones among those, and the sum before them (Wh), which their sums go on from
    stale: list[int] = field(default_factory=list)
    rewrite: list[tuple[int, float | None]] = field(default_factory=list)
    kept: list[int] = field(default_factory=list)
    base: float = 0.0

    def add(self, intervals: Iterable[Any]) -> None:
        """Add the complete hours among ``intervals``."""
        complete, unusable = complete_hours(intervals, self.per_hour, self.since)
        self.hours.update(complete)
        self.unusable += unusable

    def covered_by(self, intervals: Iterable[Any]) -> bool:
        """Return True when ``intervals`` reach back to the first hour needed.

        The poll's intervals then hold everything the portal has from there on,
        and nothing has to be read.
        """
        starts = [
            _instant(item.get("start")) for item in intervals if isinstance(item, dict)
        ]
        return any(start is not None and start <= self.since for start in starts)


class StatisticsImporter:
    """Import the readings that carry a statistic_id into long-term statistics."""

    def __init__(self, hass: HomeAssistant, read_history: ReadHistory) -> None:
        """Initialize the importer; ``read_history`` reads a range of days."""
        self.hass = hass
        self._read_history = read_history
        self._lock = asyncio.Lock()
        self._recorder_missing_logged = False
        self._warned: set[tuple[Any, ...]] = set()

    @property
    def running(self) -> bool:
        """Return True while an import runs."""
        return self._lock.locked()

    def _warn_once(self, key: tuple[Any, ...], message: str, *args: Any) -> None:
        """Log a warning the first time, at DEBUG level after that."""
        if key in self._warned:
            LOGGER.debug(message, *args)
        else:
            self._warned.add(key)
            LOGGER.warning(message, *args)

    async def async_import(self, data: Mapping[str, Any]) -> None:
        """Import the statistic readings of a poll. Never raises."""
        if self._lock.locked():
            LOGGER.debug("A statistics import is still running; this one is skipped")
            return
        async with self._lock:
            if "recorder" not in self.hass.config.components:
                if not self._recorder_missing_logged:
                    self._recorder_missing_logged = True
                    LOGGER.debug(
                        "The recorder is not loaded, so no long-term statistics "
                        "are imported"
                    )
                return
            by_point: dict[str, list[dict[str, Any]]] = {}
            for zaehlpunkt, reading in statistic_readings(data):
                by_point.setdefault(zaehlpunkt, []).append(reading)
            for zaehlpunkt, readings in by_point.items():
                try:
                    await self._async_import_point(zaehlpunkt, readings)
                except Exception:  # noqa: BLE001 - the import must not end the task
                    LOGGER.exception(
                        "The statistics import of %s failed; it is tried again "
                        "with the next poll", zaehlpunkt,
                    )

    async def _async_import_point(
        self, zaehlpunkt: str, readings: list[dict[str, Any]]
    ) -> None:
        """Import the statistics of one metering point."""
        today = _today()
        jobs: list[_Job] = []
        for reading in readings:
            if error := reading.get("error"):
                self._warn_once(
                    ("error", str(reading["statistic_id"]), str(error)),
                    "%s (%s): the portal's values could not be read: %s. Nothing is "
                    "imported now and nothing is skipped; the import is tried again "
                    "with the next poll", reading.get("statistic_name"),
                    reading["statistic_id"], error,
                )
                continue
            job = await self._async_job(reading, today)
            if job is None:
                continue
            jobs.append(job)
            if job.covered_by(reading["intervals"]):
                job.add(reading["intervals"])
                await self._async_compare(job, reading["intervals"])
            else:
                job.read = True
        if to_read := [job for job in jobs if job.read]:
            await self._async_read(zaehlpunkt, to_read, today)
        for job in jobs:
            await self._async_write(job)

    async def _async_job(
        self, reading: dict[str, Any], today: date
    ) -> _Job | None:
        """Return what a reading's import needs, starting at its last row."""
        statistic_id = reading["statistic_id"]
        if (
            not isinstance(statistic_id, str)
            or not valid_statistic_id(statistic_id)
            or not statistic_id.startswith(f"{DOMAIN}:")
        ):
            self._warn_once(
                ("statistic_id", str(statistic_id)),
                "%r is no valid statistic id of this integration; its values are "
                "not imported into the long-term statistics", statistic_id,
            )
            return None
        per_hour = _intervals_per_hour(reading)
        if per_hour is None:
            self._warn_once(
                ("interval", statistic_id),
                "%s: intervals of %r minutes cannot be summed per hour; the values "
                "are not imported", statistic_id, reading.get("interval_minutes"),
            )
            return None

        last = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics, self.hass, 1, statistic_id, True, {"sum"}
        )
        rows = last.get(statistic_id) or []
        floor = _years_before(today, MAX_HISTORY_YEARS)
        history_start = max(_day(reading.get("history_start")) or floor, floor)
        if rows:
            after: int | None = int(rows[0]["start"])
            total = round((rows[0].get("sum") or 0.0) * 1000, 3)
            first_day = max(datetime.fromtimestamp(after, VIENNA).date(), floor)
            since = after + HOUR
        else:
            after, total = None, 0.0
            first_day = history_start
            since = _midnight(first_day)
        return _Job(
            reading=reading,
            statistic_id=statistic_id,
            name=str(reading.get("statistic_name") or statistic_id),
            per_hour=per_hour,
            after=after,
            total=total,
            first_day=first_day,
            since=since,
            earliest=_midnight(history_start),
        )

    async def _async_compare(self, job: _Job, intervals: list[Any]) -> None:
        """Compare the imported hours with the poll's; plan their corrections.

        The recorder's hours from the first hour of the poll's intervals to the last
        imported one are compared with the poll's complete hours from the history
        start on - only the poll's own values, never those of a history read.
        """
        if job.after is None:
            return
        starts = [
            start for start in (
                _instant(item.get("start")) for item in intervals
                if isinstance(item, dict)
            ) if start is not None
        ]
        if not starts:
            return
        first = min(starts) - min(starts) % HOUR
        if first > job.after:
            return
        current, _ = complete_hours(intervals, job.per_hour, max(first, job.earliest))
        found = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period, self.hass, datetime.fromtimestamp(first, UTC),
            datetime.fromtimestamp(job.after + HOUR, UTC), {job.statistic_id},
            "hour", None, {"state", "sum"},
        )
        rows = {int(row["start"]): row for row in found.get(job.statistic_id) or []}
        if (last_row := rows.get(job.after)) is not None:
            # The sum the new hours go on from, from the same read as the rows
            # compared: a correction committed after get_last_statistics (an
            # earlier import's, still queued then) must not be missed.
            job.total = round((last_row.get("sum") or 0.0) * 1000, 3)
        absent = sorted(hour for hour in rows if hour not in current)
        if absent:
            LOGGER.debug(
                "%s: %d imported hour(s) from %s to %s are not among the poll's "
                "complete hours; they are kept as they are", job.statistic_id,
                len(absent), _local(absent[0]), _local(absent[-1]),
            )
        stale, rewrite, kept = plan_corrections(
            {hour: row.get("state") for hour, row in rows.items()}, current, job.after
        )
        if not stale:
            return
        before = [hour for hour in rows if hour < stale[0]]
        if before:
            base: float | None = _sum_of(job.statistic_id, rows[max(before)])
        else:
            base = await self._async_sum_before(job.statistic_id, first)
        job.stale, job.rewrite, job.kept = stale, rewrite, kept
        job.base = round((base or 0.0) * 1000, 3)  # no row before: from 0

    async def _async_sum_before(self, statistic_id: str, before: int) -> float | None:
        """Return the sum of a statistic's newest hour before ``before``, if any."""
        for since in (before - 2 * 24 * HOUR, 0):
            found = await get_instance(self.hass).async_add_executor_job(
                statistics_during_period, self.hass, datetime.fromtimestamp(since, UTC),
                datetime.fromtimestamp(before, UTC), {statistic_id}, "hour", None,
                {"sum"},
            )
            if rows := found.get(statistic_id):
                return _sum_of(statistic_id, rows[-1])
        return None

    async def _async_read(
        self, zaehlpunkt: str, jobs: list[_Job], today: date
    ) -> None:
        """Read what the poll did not, a month at a time, into the jobs.

        A failing read ends the reading: the hours from there on count as not
        published, so no gap is skipped over them. A month whose reading for a job
        carries an ``error`` marks that job failed (nothing of it is written in
        this run); the other jobs go on.
        """
        first = min(job.first_day for job in jobs)
        for month_first, month_last in _months(first, today):
            # A read that is over before it is awaited does not give the event
            # loop a turn: a long backfill yields after every month.
            await asyncio.sleep(0)
            try:
                readings = await self._read_history(
                    zaehlpunkt, month_first, month_last
                )
            except SmartmeterError as err:
                LOGGER.warning(
                    "The statistics import of %s could not read %s to %s: %s. It "
                    "goes on with the next poll", zaehlpunkt, month_first,
                    month_last, str(err).rstrip("."),
                )
                return
            for reading in readings or []:
                if not isinstance(reading, dict):
                    continue
                for job in jobs:
                    if job.failed or reading.get("statistic_id") != job.statistic_id:
                        continue
                    if error := reading.get("error"):
                        job.failed = (month_first, month_last, str(error))
                    else:
                        job.add(reading.get("intervals") or [])
            if all(job.failed for job in jobs):
                return

    async def _async_write(self, job: _Job) -> None:
        """Hand the corrected and the new hours of a job to the recorder.

        The corrected hours come first, in one call - one recorder transaction, so
        a correction is written as a whole or not at all. Cut in two (a reload
        between two calls), the later rows would keep their old sums: a step that
        no later comparison of the states finds. Their running sums go on from the
        row before them, and those of the new hours from the corrected last one.
        The new hours go a month at a time: a cut between two months leaves
        consistent rows, which the next poll continues.
        """
        if job.failed is not None:
            first, last, reason = job.failed
            self._warn_once(
                ("error", job.statistic_id, reason),
                "%s (%s): the portal's values of %s to %s could not be read: %s. "
                "Nothing is imported now and nothing is skipped; the import is "
                "tried again with the next poll", job.name, job.statistic_id, first,
                last, reason,
            )
            return
        rows, gaps = plan_hours(job.hours, job.after)
        source = "read from the portal" if job.read else "of the poll"
        if job.unusable:
            LOGGER.debug(
                "%s: %d interval(s) without a usable start or value",
                job.statistic_id, job.unusable,
            )
        for gap_start, gap_end in gaps:
            self._warn_once(
                ("gap", job.statistic_id, gap_start),
                "%s (%s): no complete values from %s to %s. Newer values exist for "
                "more than %d days, so this gap is skipped", job.name,
                job.statistic_id, _local(gap_start), _local(gap_end),
                GAP_LIMIT // (24 * HOUR),
            )
        last = rows[-1][0] if rows else job.after
        held = sum(1 for hour in job.hours if last is None or hour > last)
        if held:
            LOGGER.debug(
                "%s: waiting for the values from %s; %d newer complete hour(s) are "
                "held back until they arrive or are %d days newer", job.statistic_id,
                _local(last + HOUR if last is not None else min(job.hours)), held,
                GAP_LIMIT // (24 * HOUR),
            )
        if not rows and not job.rewrite:
            LOGGER.debug(
                "%s: nothing new to import (%d complete hours %s, last imported "
                "hour %s)", job.statistic_id, len(job.hours), source,
                _local(job.after) if job.after is not None else "none",
            )
            return

        metadata = StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=job.name,
            source=DOMAIN,
            statistic_id=job.statistic_id,
            unit_class=EnergyConverter.UNIT_CLASS,
            unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        )
        handed = 0

        async def hand_over(batch: list[StatisticData]) -> None:
            nonlocal handed
            if handed:
                await asyncio.sleep(0)  # a long import gives the event loop a turn
            async_add_external_statistics(self.hass, metadata, batch)
            handed += 1

        total = job.base if job.rewrite else job.total
        if job.rewrite:
            corrected: list[StatisticData] = []
            for hour, energy in job.rewrite:
                total += energy or 0.0  # a kept hour without a value adds nothing
                corrected.append(_statistic(hour, energy, total))
            await hand_over(corrected)
            LOGGER.warning(
                "Corrected %d hour(s) of %s from %s to %s "
                "(values had changed or were missing)",
                len(job.stale), job.name, _local(job.stale[0]), _local(job.stale[-1]),
            )
            LOGGER.debug(
                "%s: wrote the %d hour(s) from %s to %s again (%d of them with their "
                "stored value), with running sums from %.3f kWh on", job.statistic_id,
                len(job.rewrite), _local(job.rewrite[0][0]), _local(job.rewrite[-1][0]),
                len(job.kept), job.base / 1000,
            )
        month: tuple[int, int] | None = None
        batch: list[StatisticData] = []
        for hour, energy in rows:
            local = datetime.fromtimestamp(hour, VIENNA)
            if batch and (local.year, local.month) != month:
                await hand_over(batch)
                batch = []
            month = (local.year, local.month)
            total += energy
            batch.append(_statistic(hour, energy, total))
        if batch:
            await hand_over(batch)
        if not rows:
            return
        log = LOGGER.info if job.after is None else LOGGER.debug
        log(
            "Imported %d hours from %s to %s into %s (%s, %s)", len(rows),
            _local(rows[0][0]), _local(rows[-1][0]), job.statistic_id, job.name,
            source,
        )
