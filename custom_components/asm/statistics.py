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
    hours: dict[int, float] = field(default_factory=dict)
    unusable: int = 0
    read: bool = False  # True when the poll's intervals did not suffice
    # (first day, last day, reason) of a month whose reading carried an error:
    # nothing of this job is imported in this run
    failed: tuple[date, date, str] | None = None

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
        if rows:
            after: int | None = int(rows[0]["start"])
            total = round((rows[0].get("sum") or 0.0) * 1000, 3)
            first_day = max(datetime.fromtimestamp(after, VIENNA).date(), floor)
            since = after + HOUR
        else:
            after, total = None, 0.0
            first_day = max(_day(reading.get("history_start")) or floor, floor)
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
        )

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
        """Hand the new hours of a job to the recorder, a month at a time."""
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
        if not rows:
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
        total = job.total
        month: tuple[int, int] | None = None
        chunk: list[StatisticData] = []
        for hour, energy in rows:
            start = datetime.fromtimestamp(hour, UTC)
            local = start.astimezone(VIENNA)
            if chunk and (local.year, local.month) != month:
                async_add_external_statistics(self.hass, metadata, chunk)
                chunk = []
                await asyncio.sleep(0)
            month = (local.year, local.month)
            total += energy
            chunk.append(
                StatisticData(
                    start=start,
                    state=round(energy / 1000, 3),
                    sum=round(total / 1000, 3),
                )
            )
        async_add_external_statistics(self.hass, metadata, chunk)

        log = LOGGER.info if job.after is None else LOGGER.debug
        log(
            "Imported %d hours from %s to %s into %s (%s, %s)", len(rows),
            _local(rows[0][0]), _local(rows[-1][0]), job.statistic_id, job.name,
            source,
        )
