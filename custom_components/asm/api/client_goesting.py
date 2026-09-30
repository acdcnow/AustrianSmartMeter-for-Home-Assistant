"""E-Werk Gösting customer portal client.

E-Werk Gösting Stromversorgungs GmbH (Graz) shows its customers the 15-minute
values of their meters in the customer portal
``https://goesting-dav.mein-portal.at/bkp/``, a Next.js 15 application ("WPO
Frontend", the white-label product behind ``*.mein-portal.at``) without a public
API. This client reads what the portal's own pages are built from, as recorded
against the live portal on 2026-09-29:

* **Login** is the Next.js server action ``authenticateAction``: a POST to
  ``/bkp/login`` with the header ``Next-Action: <action id>`` and the body
  ``[{"email": …, "password": …}]``. Success is HTTP 303 (``x-action-redirect:
  /service-zone``) with the cookies ``accessToken`` (JWT, 30 minutes),
  ``refreshToken`` (60 minutes, rotating) and ``userType``; wrong credentials are
  an HTTP 200 with an ``errorMessage``. **The action id changes with every
  deploy of the portal**: an unknown id is answered with HTTP 500, and the
  current one is then looked up in the login page's JavaScript chunks
  (``createServerReference("<id>", …, "authenticateAction")``).
* **Pages** requested with the header ``RSC: 1`` answer their React Server
  Components ("Flight") payload instead of HTML; such a request also renews an
  expired accessToken while the refreshToken is valid. A lost session is an
  HTTP 200 whose payload redirects (``NEXT_REDIRECT``) to ``/login``.
* **Anlagen** (facilities) are the ``asset`` objects of the dashboard's sidebar,
  identified by their ``vertragsID``; ``/dashboard/<vertragsID>/asset-details``
  names the Zählpunkt and the meter.
* **Values** are the ``data`` array next to ``meteringPointIdentifier`` in the
  payload of ``/dashboard/<vertragsID>/consumption`` (``showCurrent=false``,
  ``startDate``, ``endDate``, ``grid=QH``), one item per channel and quarter
  hour, e.g. ``{"timeStamp": "2026-08-01T00:15:00+02:00", "value": 0.002,
  "unit": "kWh", "channelType": "1-1:1.9.0 G.01", "level": "L1"}``. The
  portal's "Export" button builds its .xlsx in the browser from this very array
  - there is no export request - so these are exactly the exported values. Only
  the measured channels (``G.01``) are read: the other channel types are
  energy-community and residual-grid quantities.
* ``startDate``/``endDate`` are the Europe/Vienna midnights of the first and the
  last day (both inclusive), in UTC. ``grid=QH`` is only honoured while
  ``endDate - startDate`` is at most **30 days of real time**; beyond that the
  portal silently answers daily ``from``/``to`` buckets. A period is read in
  windows of at most 30 days, so the month of the autumn DST switch (30 days and
  an hour) takes two.
* ``timeStamp`` is the **end** of the interval in Europe/Vienna time with its
  UTC offset (``…T00:15:00+02:00`` is 00:00-00:15); the hour the autumn switch
  repeats appears twice, with ``+02:00`` and ``+01:00``. A value belongs to the
  day its interval starts in. ``level`` L1 and L2 are measured values, L3
  substitute values.
* A day's values are published on the next day, by 12:00 at the latest and
  usually earlier: today's are missing, and yesterday's can arrive late. While the
  portal is publishing a day, its values can still change (on 2026-09-30, the
  previous day's total read 10,385, then 11,808 and then 13,128 Wh within an
  hour), and no field tells provisional values from final ones. A day's values
  therefore count from 12:00 of the next day on (PUBLISHED_BY_HOUR): the day has
  *settled* then. A poll reads the last four days, one request per Anlage.

A reading per register (consumption, and production when the portal reports it)
therefore carries two views of the settled values. Its ``messwerte`` are the
totals of the complete settled Vienna days, oldest first, and the entity shows the
latest one; its ``data_until`` is the end of the newest settled quarter hour, while
``held_back`` counts the quarter hours read but not settled yet and ``settles_at``
says when the newest of them settle. Its ``intervals`` are the settled quarter
hours themselves with their real timestamps, which
statistics.py sums per hour into Home Assistant's long-term statistics: the
external statistic ``statistic_id`` (``asm:<Zählpunkt>_consumption``), which is
what the Energy dashboard uses. Its first import reads the whole history from
``history_start``, the contract start. Nothing that went wrong may look like a
period without values there: a window that the portal answers with daily values is
an error, and a register that cannot be read keeps its reading, without values and
with the reason as ``error``.
"""
from __future__ import annotations

import json
import logging
import math
import re
from collections.abc import Iterator
from datetime import date, datetime, timedelta, timezone
from typing import Any, NamedTuple
from urllib.parse import urlencode, urlsplit
from zoneinfo import ZoneInfo

import requests

from .base import SmartmeterClient
from .errors import (
    SmartmeterConnectionError,
    SmartmeterError,
    SmartmeterLoginError,
    SmartmeterQueryError,
)

LOGGER = logging.getLogger(__name__)

PORTAL_HOST = "https://goesting-dav.mein-portal.at"
PORTAL_BASE = f"{PORTAL_HOST}/bkp"
LOGIN_URL = f"{PORTAL_BASE}/login"
SERVICE_ZONE_URL = f"{PORTAL_BASE}/service-zone"
DASHBOARD_URL = f"{PORTAL_BASE}/dashboard"

# Id of the login server action ("authenticateAction") in the portal build of
# 2026-09-29. Every deploy of the portal changes it, and the client then looks
# up the current one (see _discover_login_action): this is only the first try.
LOGIN_ACTION_FALLBACK = "40fc3cb767adf276aadce9827cf5fe128d76f63434"

# The accessToken lives 30 minutes (page requests renew it while the 60-minute
# refreshToken is valid). A login is trusted for 25 minutes.
SESSION_MAX_AGE = timedelta(minutes=25)

REQUEST_TIMEOUT = 30

# grid=QH is honoured while endDate - startDate is at most 30 days of real time.
QH_WINDOW_MAX_SECONDS = 30 * 24 * 60 * 60  # 2,592,000 s

INTERVAL_MINUTES = 15

# A poll reads today and the three days before. Yesterday's values count only from
# PUBLISHED_BY_HOUR on, and the sensors still need a value before - also when the
# day before yesterday is incomplete on the portal. The settled days among them are
# compared with what was imported (statistics.py).
DEFAULT_DAYS = 4

# A day's values count from this hour (Vienna time) of the next day on: the portal
# has published the day by then, and while it publishes one, the values can still
# change. Until then the day's quarter hours are held back.
PUBLISHED_BY_HOUR = 12

# A period read is kept this long: reading the same days again costs no request.
PERIOD_CACHE_MAX_AGE = timedelta(minutes=10)

# Energy consumed / fed in within an interval: the OBIS part of the portal's
# channel types ("1-1:1.9.0 G.01"). The cumulative registers are not exposed.
OBIS_CONSUMPTION = "1-1:1.9.0"
OBIS_PRODUCTION = "1-1:2.9.0"

# The entity of a reading shows the total of a day. Its values reach the
# long-term statistics through the statistic_id, not through a state class (see
# sensor.py and statistics.py).
VALUE_TYPE = "DAY"
UNIT_WH = "Wh"

# The long-term statistics of an Anlage are external statistics of the
# integration's domain, "asm:<Zählpunkt>_consumption" (statistics.py).
STATISTIC_SOURCE = "asm"

# The portal reports kWh, the entities are fed Wh.
KWH_TO_WH = 1000

VIENNA = ZoneInfo("Europe/Vienna")

# How an Anlage is described in Home Assistant.
DEVICE_MODEL = "E-Werk Gösting 15-minute values"
GRID_OPERATOR = "E-Werk Gösting Stromversorgungs GmbH"

USER_AGENT = (
    "AustriaSmartmeter-HASS/1.2 "
    "(+https://github.com/acdcnow/AustrianSmartMeter-for-Home-Assistant)"
)


class _Register(NamedTuple):
    """A register of an Anlage and the names it has in Home Assistant."""

    obis: str
    name: str  # the entity, which shows the total of the latest complete day
    statistic: str  # the end of the statistic_id
    label: str  # the statistic's name, after the device name


# The readings of an Anlage, in this order.
_REGISTERS = (
    _Register(
        OBIS_CONSUMPTION, "Consumption Latest Day", "consumption", "Consumption"
    ),
    _Register(OBIS_PRODUCTION, "Production Latest Day", "production", "Production"),
)

# A register is read from its measured channel only ("Verbrauch (gemessen)",
# "Lieferung (gemessen)"). The other channel types of the portal (G.02, G.03,
# G.03R, P.01, P.01T, ...) are energy-community and residual-grid quantities, not
# the Anlage's own consumption or feed-in.
_MEASURED_CHANNEL = "G.01"

# The portal's quality levels in the status vocabulary of the integration.
_STATUS = {"L1": "VALID", "L2": "VALID", "L3": "ESTIMATED"}

# The login errors the portal is known to report, in the integration's language.
_LOGIN_ERRORS = {
    "Falsches Passwort oder E-Mail Adresse.": (
        "The E-Werk Gösting portal rejected the e-mail address or password."
    ),
    "WpoBenutzer not found.": (
        "The E-Werk Gösting portal rejected the e-mail address or password: it "
        "has no account with this e-mail address."
    ),
}

# Another login error that names one of these rejects the credentials. Any other
# one is a problem of the portal (its action wrapper passes e.g. "Network Error"
# on), which must not end in a failed authentication.
_CREDENTIAL_WORDS = (
    "passwort", "password", "e-mail", "email", "benutzer", "kennwort",
    "anmeldedaten", "credentials",
)

# The portal's "eart" (energy type) of an Anlage.
_ENERGY_TYPES = {"S": "Strom"}

_CHUNK_URL = re.compile(r"/bkp/_next/static/chunks/[\w./%-]+\.js")
_LOGIN_ACTION = re.compile(
    r'createServerReference\)\("([0-9a-f]+)"[^)]*"authenticateAction"\)'
)

# Redirect targets, as paths without query and trailing slash, with or without
# /bkp and a locale: the login page means a lost session, and for a dashboard page
# so does the service zone (where the portal bounces an invalid access token).
_LOGIN_PATH = re.compile(r"(?:^|/)login$")
_SERVICE_ZONE_PATH = re.compile(r"(?:^|/)service-zone$")
_DASHBOARD_PATH = re.compile(r"(?:^|/)dashboard/(\d+)(?:/|$)")
_DASHBOARD_PAGES = urlsplit(DASHBOARD_URL).path + "/"


class _Value(NamedTuple):
    """One 15-minute value of a register."""

    instant: datetime  # end of the interval, timezone aware
    stamp: str  # the portal's timeStamp, unchanged
    wh: float
    status: str
    day: date  # the Europe/Vienna day the interval starts in


class _Registers(NamedTuple):
    """The values of an Anlage's registers, and why a register was not read."""

    values: dict[str, list[_Value]]  # OBIS code -> values, oldest first
    errors: dict[str, str]  # OBIS code -> the reason the register was not read


class _SessionLost(SmartmeterConnectionError):
    """The portal answered a page request with a redirect to its login page."""


# ------------------------------------------------------------ Flight payloads


def _parse_flight(payload: bytes) -> dict[str, tuple[str, Any]]:
    """Split a React Server Components ("Flight") payload into its rows.

    A row is ``<hex id>:<content>`` up to the end of the line. The content is
    JSON, behind a tag for special rows: ``I`` a client module, ``E`` an error,
    ``HL`` a resource hint. A text row ``<id>:T<hex byte length>,<text>`` has no
    line end. Returns ``{row id: (tag, value)}`` in stream order, with the tag
    ``"J"`` for plain JSON; content that is not JSON is kept as text.
    """
    rows: dict[str, tuple[str, Any]] = {}
    position, size = 0, len(payload)
    while position < size:
        if payload[position:position + 1] == b"\n":
            position += 1
            continue
        colon = payload.find(b":", position)
        if colon < 0:
            break
        key = payload[position:colon].decode("ascii", "replace")
        start = colon + 1
        if payload[start:start + 1] == b"T":
            comma = payload.find(b",", start)
            try:
                length = int(payload[start + 1:comma], 16) if comma > 0 else -1
            except ValueError:
                length = -1
            if length < 0:
                break
            end = comma + 1 + length
            rows[key] = ("T", payload[comma + 1:end].decode("utf-8", "replace"))
            position = end
            continue
        end = payload.find(b"\n", start)
        if end < 0:
            end = size
        line = payload[start:end].decode("utf-8", "replace")
        tag, content = "J", line
        if line.startswith("HL"):
            tag, content = "HL", line[2:]
        elif line[:1] in ("I", "E", "D", "W") and line[1:2] in ("[", "{"):
            tag, content = line[:1], line[1:]
        try:
            rows[key] = (tag, json.loads(content))
        except ValueError:
            rows[key] = (tag, content)
        position = end + 1
    return rows


def _dicts_with(
    rows: dict[str, tuple[str, Any]], key: str
) -> Iterator[dict[str, Any]]:
    """Yield every object of the JSON rows that has ``key``, in document order."""
    stack = [value for tag, value in reversed(list(rows.values())) if tag == "J"]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if key in node:
                yield node
            stack.extend(reversed(list(node.values())))
        elif isinstance(node, list):
            stack.extend(reversed(node))


def _digests(rows: dict[str, tuple[str, Any]]) -> list[str]:
    """Return the error rows' digests, e.g. ``NEXT_REDIRECT;replace;/login;307;``."""
    return [
        str(value.get("digest") or "")
        for tag, value in rows.values()
        if tag == "E" and isinstance(value, dict)
    ]


def _target_path(target: str) -> str:
    """Return the path of a redirect target, without query and trailing slash."""
    return urlsplit(target.strip()).path.rstrip("/")


def _redirects(rows: dict[str, tuple[str, Any]]) -> list[str]:
    """Return the target paths of the payload's redirects (``NEXT_REDIRECT`` rows).

    Normal pages carry a redirect as well (to the Anlage's dashboard): only the
    target tells a lost session apart.
    """
    paths: list[str] = []
    for digest in _digests(rows):
        parts = digest.split(";")
        if len(parts) > 2 and parts[0] == "NEXT_REDIRECT":
            paths.append(_target_path(parts[2]))
    return paths


def _assets_in(rows: dict[str, tuple[str, Any]]) -> list[dict[str, Any]]:
    """Return the Anlagen of a page: the ``asset`` objects with a vertragsID."""
    found: dict[int, dict[str, Any]] = {}
    for holder in _dicts_with(rows, "asset"):
        asset = holder["asset"]
        if not isinstance(asset, dict):
            continue  # a reference to an asset that is spelled out elsewhere
        anlage_id = asset.get("vertragsID")
        if isinstance(anlage_id, int) and not isinstance(anlage_id, bool):
            found.setdefault(anlage_id, asset)
    return list(found.values())


def _login_error(rows: dict[str, tuple[str, Any]]) -> str | None:
    """Return the error message of a rejected login, if the payload has one."""
    for holder in _dicts_with(rows, "errorMessage"):
        message = holder["errorMessage"]
        if isinstance(message, str) and message.strip():
            return message.strip()
    return None


def _login_failure(message: str, username: str) -> SmartmeterError:
    """Return the error for a login that the portal answered with ``message``.

    Only a rejection of the credentials is a SmartmeterLoginError: that one ends in
    a failed authentication, which stops the polling. The account's e-mail address
    is blanked out of the message, and not searched for credential words.
    """
    known = _LOGIN_ERRORS.get(message)
    if known:
        return SmartmeterLoginError(known)
    account = re.compile(re.escape(username), re.IGNORECASE) if username else None
    text = " ".join((account.sub("<e-mail>", message) if account else message).split())
    words = (account.sub(" ", message) if account else message).lower()
    if any(word in words for word in _CREDENTIAL_WORDS):
        return SmartmeterLoginError(
            "The E-Werk Gösting portal rejected the e-mail address or password "
            f"(portal message: {text[:120]!r})."
        )
    return SmartmeterConnectionError(
        "The E-Werk Gösting portal could not log in "
        f"(portal message: {text[:120]!r}). It may be down; please try again later."
    )


# ---------------------------------------------------------------- time helpers


def _now() -> datetime:
    """Return the current time in Europe/Vienna."""
    return datetime.now(VIENNA)


def _settles_at(day: date) -> datetime:
    """Return when a Vienna day's values count: PUBLISHED_BY_HOUR the next day."""
    following = day + timedelta(days=1)
    return datetime(
        following.year, following.month, following.day, PUBLISHED_BY_HOUR,
        tzinfo=VIENNA,
    )


def _settled(day: date, now: datetime) -> bool:
    """Return True when a Vienna day's values count at ``now`` (see _settles_at)."""
    # Compared as instants: aware datetimes of one tzinfo compare by wall clock.
    return now.astimezone(timezone.utc) >= _settles_at(day)


def _last_settling(now: datetime) -> datetime:
    """Return the latest moment at or before ``now`` at which a day settled."""
    today = now.astimezone(VIENNA).date()
    if _settled(today - timedelta(days=1), now):
        return _settles_at(today - timedelta(days=1))  # today, PUBLISHED_BY_HOUR
    return _settles_at(today - timedelta(days=2))


def _midnight(day: date) -> datetime:
    """Return the Europe/Vienna midnight that starts ``day``, in UTC."""
    return datetime(day.year, day.month, day.day, tzinfo=VIENNA).astimezone(
        timezone.utc
    )


def _url_date(day: date) -> str:
    """Return a day the way the portal's URLs write it: its midnight in UTC."""
    return _midnight(day).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _intervals_in(day: date) -> int:
    """Return the number of quarter hours of a Vienna day (96, 92 or 100)."""
    seconds = (_midnight(day + timedelta(days=1)) - _midnight(day)).total_seconds()
    return int(seconds // (INTERVAL_MINUTES * 60))


def _qh_windows(start: date, end: date) -> list[tuple[date, date]]:
    """Split the days ``start`` to ``end`` (inclusive) into 15-minute windows.

    Measured between the midnights of a window's first and last day, a window
    spans at most 30 days of real time: a 31-day month is one window, the month
    of the autumn DST switch two, and a single day a window of its own.
    """
    windows: list[tuple[date, date]] = []
    first = start
    while first <= end:
        last = first
        while last < end and (
            _midnight(last + timedelta(days=1)) - _midnight(first)
        ).total_seconds() <= QH_WINDOW_MAX_SECONDS:
            last += timedelta(days=1)
        windows.append((first, last))
        first = last + timedelta(days=1)
    return windows


def _as_day(value: Any) -> date:
    """Return the Vienna day of a date or a datetime (naive means local time)."""
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(VIENNA).date()
        return value.date()
    if isinstance(value, date):
        return value
    raise SmartmeterQueryError(f"Not a date: {value!r}.")


def _period(date_from: Any, date_until: Any, today: date) -> tuple[date, date]:
    """Return the days to read: the given ones, else the last DEFAULT_DAYS days."""
    end = _as_day(date_until) if date_until is not None else today
    if date_from is not None:
        start = _as_day(date_from)
    else:
        start = end - timedelta(days=DEFAULT_DAYS - 1)
    return (start, end) if start <= end else (end, start)


def _parse_stamp(value: Any) -> datetime | None:
    """Parse a ``timeStamp``; only one with its UTC offset is unambiguous."""
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        return None
    return stamp if stamp.utcoffset() is not None else None


def _interval_start(instant: datetime) -> datetime:
    """Return the start of the interval ending at ``instant``, in Vienna time."""
    return (instant - timedelta(minutes=INTERVAL_MINUTES)).astimezone(VIENNA)


def _start_day(instant: datetime) -> date:
    """Return the Vienna day in which the interval ending at ``instant`` starts."""
    return _interval_start(instant).date()


# -------------------------------------------------------------------- values


def _round(value: float) -> float:
    """Round an energy value to 3 decimals, and never report -0.0."""
    return round(value, 3) + 0.0


def _status(level: Any) -> str:
    """Map the portal's quality level (L1, L2, L3) onto VALID / ESTIMATED."""
    if isinstance(level, str) and level.strip():
        return _STATUS.get(level.strip(), level.strip())
    return "UNKNOWN"


def _channel_values(
    channel: str, entries: list[tuple[datetime, dict[str, Any]]]
) -> list[_Value]:
    """Return the values of one channel in Wh, oldest first.

    ``entries`` are the portal's items with their parsed ``timeStamp``; an item
    without a number is skipped. Raises SmartmeterQueryError, naming the reason,
    when the values are not kWh or not 15 minutes apart: never a wrong number.
    """
    values: list[_Value] = []
    skipped = 0
    for instant, item in entries:
        value = item.get("value")
        if isinstance(value, bool) or not isinstance(value, (int, float)) \
                or not math.isfinite(value):
            skipped += 1
            continue
        unit = item.get("unit")
        if str(unit or "").strip().lower() != "kwh":
            raise SmartmeterQueryError(f"its values are reported in {unit!r}, not kWh")
        values.append(_Value(
            instant, item["timeStamp"], _round(value * KWH_TO_WH),
            _status(item.get("level")), _start_day(instant),
        ))
    if skipped:
        LOGGER.debug(
            "E-Werk Gösting: %d item(s) of %s carry no number", skipped, channel
        )
    values.sort(key=lambda entry: entry.instant)
    gaps = [
        (later.instant - earlier.instant).total_seconds()
        for earlier, later in zip(values, values[1:])
    ]
    if gaps and min(gaps) != INTERVAL_MINUTES * 60:
        raise SmartmeterQueryError(
            f"its values are {min(gaps) / 60:g} minutes apart, not {INTERVAL_MINUTES}"
        )
    return values


def _statistic_id(zaehlpunkt: str, register: str) -> str:
    """Return the id of a register's long-term statistic.

    The Zählpunkt in lower case, with every run of characters other than a-z and
    0-9 turned into one underscore, e.g.
    ``asm:at0082100000000000000000000012345_consumption``.
    """
    slug = re.sub(r"[^a-z0-9]+", "_", zaehlpunkt.lower()).strip("_")
    return f"{STATISTIC_SOURCE}:{slug}_{register}"


def _day_totals(values: list[_Value]) -> list[dict[str, Any]]:
    """Return the totals of the complete days among ``values``, oldest first.

    A day is complete when all its quarter hours are there (96, or 92 and 100 on
    the days of the DST switches): the total of an incomplete day would be a
    wrong number. A day is validated when every quarter hour is a measured value.
    """
    days: dict[date, list[_Value]] = {}
    for value in values:
        days.setdefault(value.day, []).append(value)
    totals: list[dict[str, Any]] = []
    for day in sorted(days):
        of_day = days[day]
        if len(of_day) != _intervals_in(day):
            continue
        statuses = {value.status for value in of_day}
        if statuses == {"VALID"}:
            status = "VALID"
        elif "ESTIMATED" in statuses:
            status = "ESTIMATED"
        else:
            status = "UNKNOWN"
        totals.append({
            "zeitpunkt": _midnight(day).astimezone(VIENNA).isoformat(),
            "messwert": _round(sum(value.wh for value in of_day)),
            "status": status,
            "validated": status == "VALID",
            "intervals": len(of_day),
        })
    return totals


def _readings(
    registers: _Registers, zaehlpunkt: str, name: str, history_start: date | None,
    now: datetime,
) -> list[dict[str, Any]]:
    """Turn register values into the readings contract of sensor.py.

    One reading per register with values, consumption first. Only the quarter
    hours of the days settled at ``now`` count (see PUBLISHED_BY_HOUR): its
    ``messwerte`` are the totals of the complete settled days (the entity shows
    the latest one), its ``intervals`` every settled quarter hour, for the
    long-term statistic ``statistic_id`` (statistics.py), and ``data_until`` the
    portal's end of the newest settled quarter hour (None without one).
    ``held_back`` counts the quarter hours read but not settled yet, and
    ``settles_at`` is when the newest of them settle (None when none is held back).
    ``history_start`` is the first day worth reading, ``name`` the device name. A
    register that was not read (wrong unit, values off the 15-minute grid) keeps its
    reading, without values and with the reason as ``error``: for the statistics,
    that is no period without values.
    """
    readings: list[dict[str, Any]] = []
    for register in _REGISTERS:
        values = registers.values.get(register.obis) or []
        error = registers.errors.get(register.obis)
        if not values and not error:
            continue
        is_settled = {day: _settled(day, now) for day in {v.day for v in values}}
        settled = [value for value in values if is_settled[value.day]]
        held = [value for value in values if not is_settled[value.day]]
        reading: dict[str, Any] = {
            "obisCode": register.obis,
            "name": register.name,
            "wertetyp": VALUE_TYPE,
            "einheit": UNIT_WH,
            "statistic_id": _statistic_id(zaehlpunkt, register.statistic),
            "statistic_name": f"{name} {register.label}",
            "history_start": history_start.isoformat() if history_start else None,
            "interval_minutes": INTERVAL_MINUTES,
            "records_read": len(settled),
            "data_until": settled[-1].stamp if settled else None,
            "held_back": len(held),
            "settles_at": (
                _settles_at(max(value.day for value in held)).isoformat()
                if held else None
            ),
            "messwerte": _day_totals(settled),
            "intervals": [
                {
                    "start": _interval_start(value.instant).isoformat(),
                    "end": value.stamp,
                    "wh": value.wh,
                    "status": value.status,
                }
                for value in settled
            ],
        }
        if error:
            reading["error"] = error
        readings.append(reading)
    return readings


# ------------------------------------------------------------------- Anlagen


def _text(value: Any) -> str:
    """Return a scalar as stripped text; None, booleans and containers yield ''."""
    if value is None or isinstance(value, (bool, dict, list)):
        return ""
    return str(value).strip()


def _compact(pairs: tuple[tuple[str, str], ...]) -> dict[str, str]:
    """Return the pairs that have a value, as a dict."""
    return {key: value for key, value in pairs if value}


def _history_start(asset: dict[str, Any], point: dict[str, Any]) -> date | None:
    """Return the first day worth reading for an Anlage: its contract start.

    That is the sidebar asset's ``contractStartAt`` ("2024-08-01T00:00:00"), else
    the metering point's ``vondat`` (20240801); None when neither is a date.
    """
    started = asset.get("contractStartAt")
    if isinstance(started, str) and started.strip():
        try:
            return _as_day(datetime.fromisoformat(started.strip()))
        except ValueError:
            pass
    since = _text(point.get("vondat"))
    if len(since) == 8 and since.isdigit():
        try:
            return datetime.strptime(since, "%Y%m%d").date()
        except ValueError:
            pass
    return None


def _anlage_info(
    asset: dict[str, Any], point: dict[str, Any], zaehlpunkt: str
) -> dict[str, Any]:
    """Describe an Anlage (sidebar ``asset`` plus asset-details metering point).

    Every scalar becomes a sensor attribute, so only what describes the Anlage is
    kept - no customer name, no e-mail address. The Anlagennummer is part of the
    name, because Anlagen can share an address.
    """
    anlage_id = asset["vertragsID"]
    number = _text(asset.get("assetnumber"))
    street = _text(asset.get("street"))
    house = _text(asset.get("housenumber")) + _text(asset.get("housenumberExtension"))
    label = f"Anlage {number or anlage_id}"
    place = f"{street} {house}".strip()
    producing = (
        asset.get("isMeteringPointProducer") is True
        or _text(point.get("meteringPointType")) == "O"
    )
    lastprofil = _text(point.get("lastprofilName")) or _text(
        point.get("lastprofilCode")
    )
    energy = _text(asset.get("eart"))

    info: dict[str, Any] = {
        "zaehlpunktnummer": zaehlpunkt,
        "zaehlpunktName": f"{place} ({label})" if street else label,
        "zaehlpunktAnlagentyp": "PRODUCING" if producing else "CONSUMING",
        "device_model": DEVICE_MODEL,
        "anlage_id": str(anlage_id),
    }
    info.update(_compact((
        ("anlagennummer", number),
        ("vertragsnummer", _text(asset.get("contractnumber"))),
        ("geschaeftspartner", _text(asset.get("customernumber"))),
        ("geraetNumber", _text(point.get("serialNumber"))),
        ("lastprofil", lastprofil),
        ("smart_meter_group", _text(point.get("smGruppeShortCut"))),
    )))
    if "isActivated" in asset or "isContractFinished" in asset:
        info["isActive"] = (
            asset.get("isActivated") is not False
            and asset.get("isContractFinished") is not True
        )
    if isinstance(asset.get("isSmartMeter"), bool):
        info["isSmartMeter"] = asset["isSmartMeter"]
    anlage = _compact((
        ("typ", _ENERGY_TYPES.get(energy, energy)), ("lastprofil", lastprofil)
    ))
    if anlage:
        info["anlage"] = anlage
    address = _compact((
        ("strasse", street),
        ("hausnummer", house),
        ("postleitzahl", _text(asset.get("plz"))),
        ("ort", _text(asset.get("location"))),
    ))
    if address:
        info["verbrauchsstelle"] = address
    info["netzbetreiber"] = _text(point.get("nb")) or GRID_OPERATOR
    info.update(_compact((("lieferant", _text(point.get("elName"))),)))
    return info


# -------------------------------------------------------------------- client


class EwerkGoestingClient(SmartmeterClient):
    """Client for the E-Werk Gösting customer portal (mein-portal.at)."""

    def __init__(self, username, password):
        super().__init__((username or "").strip(), password or "")
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        self._login_time: datetime | None = None
        # The login action id that worked last (see LOGIN_ACTION_FALLBACK).
        self._action_id: str | None = None
        # The Anlagen of the account (vertragsID, info dict), read once per login.
        self._anlagen: list[tuple[int, dict[str, Any]]] | None = None
        # Zählpunkt -> vertragsID of every Anlage seen, kept across logins.
        self._contracts: dict[str, int] = {}
        # Zählpunkt -> (device name, first day worth reading) of every Anlage
        # seen, kept across logins: they name and bound its long-term statistics.
        self._described: dict[str, tuple[str, date | None]] = {}
        # vertragsID -> (first day, last day, read at, register values) of the
        # period read last (see PERIOD_CACHE_MAX_AGE).
        self._periods: dict[int, tuple[date, date, datetime, _Registers]] = {}
        # Conditions logged already; later polls log them at DEBUG only.
        self._noticed: set[tuple[Any, ...]] = set()

    def _notice(
        self, level: int, key: tuple[Any, ...], message: str, *args: Any
    ) -> None:
        """Log a condition at ``level`` the first time, at DEBUG on later polls."""
        if key in self._noticed:
            LOGGER.debug(message, *args)
        else:
            self._noticed.add(key)
            LOGGER.log(level, message, *args)

    # -------------------------------------------------------------- session

    def is_logged_in(self) -> bool:
        """Return True while the portal session is expected to be usable."""
        return self._login_time is not None and not self.is_login_expired()

    def is_login_expired(self) -> bool:
        """Return True when the portal session has to be re-established."""
        if self._login_time is None:
            return True
        return datetime.now(timezone.utc) - self._login_time >= SESSION_MAX_AGE

    def _reset(self) -> None:
        """Drop the session, so that the next request starts with a login."""
        self.session.cookies.clear()
        self._login_time = None
        self._anlagen = None

    def _ensure_session(self) -> None:
        """Make sure a usable portal session exists."""
        if not self.is_logged_in():
            self.login()

    def login(self):
        """Log in with the portal's login action and keep the session cookies.

        When the portal does not know the action id, the current one is looked up
        and tried once; a portal that accepts neither is a connection error, as
        the credentials were never checked.
        """
        if not self.username or not self.password:
            raise SmartmeterLoginError(
                "E-Werk Gösting needs the e-mail address and the password of the "
                "customer portal."
            )
        self._reset()
        action_id = self._action_id or LOGIN_ACTION_FALLBACK
        refused = self._login_attempt(action_id)
        if refused is not None:
            LOGGER.debug(
                "E-Werk Gösting: login action %s not accepted (%s), looking it up "
                "on the login page", action_id, refused,
            )
            found, detail = self._discover_login_action()
            if found is not None:
                refused = self._login_attempt(found)
                detail = f"not even the action {found} that its login page names now"
                action_id = found
            if refused is not None:
                raise SmartmeterConnectionError(
                    "The E-Werk Gösting portal did not accept its login action "
                    f"({refused}), {detail}. The portal may be down or may have been "
                    "updated; please try again later, and open an issue if this "
                    "persists."
                )
        self._action_id = action_id
        self._login_time = datetime.now(timezone.utc)
        LOGGER.debug("E-Werk Gösting: logged in with login action %s", action_id)
        return self

    def _login_body(self) -> bytes:
        """Return the argument of the login action, encoded like React does."""

        def escape(text: str) -> str:
            # React's encodeReply marks a string starting with "$" by doubling it.
            return "$" + text if text.startswith("$") else text

        argument = [
            {"email": escape(self.username), "password": escape(self.password)}
        ]
        return json.dumps(argument, separators=(",", ":"), ensure_ascii=False).encode()

    def _login_attempt(self, action_id: str) -> str | None:
        """POST the login action once; return None on success.

        Returns a short description of the answer when the portal may not know the
        action (HTTP 5xx, or a page instead of an action result). Raises
        SmartmeterLoginError when it rejects the credentials, and
        SmartmeterConnectionError for any other failure. Nothing of the request
        (the password) reaches a log or an error.
        """
        headers = {
            "Accept": "text/x-component",
            "Content-Type": "text/plain;charset=UTF-8",
            "Next-Action": action_id,
            "Origin": PORTAL_HOST,
        }
        failure = None
        try:
            response = self.session.request(
                "POST", LOGIN_URL, data=self._login_body(), headers=headers,
                timeout=REQUEST_TIMEOUT, allow_redirects=False,
            )
        except requests.exceptions.RequestException as err:
            failure = type(err).__name__
        if failure is not None:
            # Raised outside the handler, so that the error does not keep the
            # request (with the password in its body) as its __context__.
            raise SmartmeterConnectionError(
                f"The login request to {LOGIN_URL} failed ({failure})."
            )

        status = response.status_code
        content_type = response.headers.get("Content-Type", "")
        LOGGER.debug(
            "E-Werk Gösting: login action %s answered HTTP %s (%s)",
            action_id, status, content_type or "no content type",
        )
        if status == 303:
            redirect = response.headers.get("x-action-redirect", "")
            target = _target_path(redirect.split(";")[0])
            if any(
                cookie.name == "accessToken" and cookie.value
                for cookie in self.session.cookies
            ):
                if target != "/service-zone":
                    LOGGER.debug("E-Werk Gösting: the login leads to %s", target)
                return None
            raise SmartmeterConnectionError(
                f"The E-Werk Gösting portal answered the login with a redirect to "
                f"{target or 'nowhere'} but set no session. Log in once in a browser "
                "to see what the portal asks for."
            )
        success = 200 <= status < 300
        payload = content_type.startswith("text/x-component")
        if status >= 500 or (success and not payload):
            return f"HTTP {status}, {content_type or 'no content type'}"
        if not success:
            raise SmartmeterConnectionError(
                f"The E-Werk Gösting portal answered the login with HTTP {status} "
                f"({content_type or 'no content type'}); please try again later."
            )
        message = _login_error(_parse_flight(response.content))
        if message is not None:
            raise _login_failure(message, self.username)
        raise SmartmeterConnectionError(
            f"The E-Werk Gösting portal answered the login with HTTP {status}, but "
            "neither logged in nor rejected the credentials."
        )

    def _discover_login_action(self) -> tuple[str | None, str]:
        """Look up the login action id in the login page's scripts, in page order.

        Returns the id, or None and what was found instead (for the error message).
        """
        response = self._request("GET", LOGIN_URL)
        if response.status_code != 200:
            return None, f"and its login page answered HTTP {response.status_code}"
        page = response.content.decode("utf-8", "replace")
        chunks = list(dict.fromkeys(_CHUNK_URL.findall(page)))
        LOGGER.debug(
            "E-Werk Gösting: looking for the login action in %d script(s)", len(chunks)
        )
        for path in chunks:
            chunk = self._request("GET", PORTAL_HOST + path)
            if chunk.status_code != 200:
                continue
            match = _LOGIN_ACTION.search(chunk.content.decode("utf-8", "replace"))
            if match:
                LOGGER.debug(
                    "E-Werk Gösting: login action %s found in %s", match.group(1), path
                )
                return match.group(1), ""
        return None, (
            f"and none of the {len(chunks)} scripts of its login page names one"
        )

    # ----------------------------------------------------------------- http

    def _request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        """Perform a request; transport errors become SmartmeterConnectionError."""
        try:
            return self.session.request(
                method, url, timeout=REQUEST_TIMEOUT, allow_redirects=False, **kwargs
            )
        except requests.exceptions.RequestException as err:
            raise SmartmeterConnectionError(f"Request to {url} failed: {err}") from err

    def _fetch_rsc(self, url: str) -> dict[str, tuple[str, Any]]:
        """GET a page as a Flight payload; raise _SessionLost on a lost session.

        A lost session is a redirect (HTTP 3xx or in the payload) to the login page
        or, for a dashboard page, to the service zone.
        """
        LOGGER.debug("E-Werk Gösting: GET %s", url)
        dashboard = urlsplit(url).path.startswith(_DASHBOARD_PAGES)

        def lost(path: str) -> bool:
            return bool(_LOGIN_PATH.search(path)) or (
                dashboard and bool(_SERVICE_ZONE_PATH.search(path))
            )

        response = self._request("GET", url, headers={"RSC": "1"})
        status = response.status_code
        location = _target_path(response.headers.get("Location", ""))
        if 300 <= status < 400 and lost(location):
            raise _SessionLost(f"{url} redirected to {location}.")
        content_type = response.headers.get("Content-Type", "")
        if status != 200 or not content_type.startswith("text/x-component"):
            raise SmartmeterConnectionError(
                f"{url} answered HTTP {status} "
                f"({content_type or 'no content type'}) instead of a page payload."
            )
        rows = _parse_flight(response.content)
        for path in _redirects(rows):
            if lost(path):
                raise _SessionLost(f"{url} redirected to {path}.")
        return rows

    def _rsc(self, url: str) -> dict[str, tuple[str, Any]]:
        """GET a page as a Flight payload, logging in again once if needed."""
        self._ensure_session()
        try:
            return self._fetch_rsc(url)
        except _SessionLost:
            LOGGER.debug("E-Werk Gösting: the portal session ended, logging in again")
        self.login()
        try:
            return self._fetch_rsc(url)
        except _SessionLost:
            raise SmartmeterConnectionError(
                "The E-Werk Gösting portal ended the session again right after a "
                f"new login ({url})."
            ) from None

    # ------------------------------------------------------------- Anlagen

    def _discover(self) -> list[tuple[int, dict[str, Any]]]:
        """Return the Anlagen of the account (vertragsID, info), read once per login.

        Hidden Anlagen and finished contracts are left out. The Zählpunkt is the
        device's identifier, so an Anlage without one is skipped (never given a
        made-up identifier), and of two Anlagen with the same Zählpunkt only the
        first is read.
        """
        self._ensure_session()
        if self._anlagen is not None:
            return self._anlagen

        anlagen: list[tuple[int, dict[str, Any]]] = []
        taken: dict[str, int] = {}
        described: dict[str, tuple[str, date | None]] = {}
        for asset in self._assets():
            anlage_id = asset["vertragsID"]
            if asset.get("isVisible") is False:
                LOGGER.debug("E-Werk Gösting: Anlage %s is hidden", anlage_id)
                continue
            if asset.get("isContractFinished") is True:
                LOGGER.debug(
                    "E-Werk Gösting: the contract of Anlage %s is finished", anlage_id
                )
                continue
            point = self._metering_point(anlage_id)
            zaehlpunkt = _text(point.get("meteringPoint"))
            if not zaehlpunkt:
                self._notice(
                    logging.WARNING, ("no Zählpunkt", anlage_id),
                    "E-Werk Gösting: the portal names no Zählpunkt for Anlage %s, so "
                    "it is not read. Please open an issue.", anlage_id,
                )
                continue
            if zaehlpunkt in taken:
                self._notice(
                    logging.WARNING, ("same Zählpunkt", taken[zaehlpunkt], anlage_id),
                    "E-Werk Gösting: Anlagen %s and %s have the same Zählpunkt %s; "
                    "only Anlage %s is read", taken[zaehlpunkt], anlage_id, zaehlpunkt,
                    taken[zaehlpunkt],
                )
                continue
            taken[zaehlpunkt] = anlage_id
            info = _anlage_info(asset, point, zaehlpunkt)
            anlagen.append((anlage_id, info))
            described[zaehlpunkt] = (
                info["zaehlpunktName"], _history_start(asset, point)
            )

        self._contracts.update(taken)
        self._described.update(described)
        self._anlagen = anlagen
        LOGGER.debug(
            "E-Werk Gösting: %d Anlage(n): %s", len(anlagen), ", ".join(
                f"{anlage_id} ({zaehlpunkt})" for zaehlpunkt, anlage_id in taken.items()
            ),
        )
        return anlagen

    def _assets(self) -> list[dict[str, Any]]:
        """Return the ``asset`` objects of the dashboard sidebar, one per Anlage.

        The service zone of a logged-in account is only a redirect to the
        dashboard of its default Anlage; that dashboard's sidebar lists them all.
        """
        rows = self._rsc(SERVICE_ZONE_URL)
        assets = _assets_in(rows)
        if assets:
            return assets
        for path in _redirects(rows):
            match = _DASHBOARD_PATH.search(path)
            if match:
                return _assets_in(self._rsc(f"{DASHBOARD_URL}/{match.group(1)}/home"))
        return []

    def _metering_point(self, anlage_id: int) -> dict[str, Any]:
        """Return the metering point of an Anlage from its asset-details page."""
        rows = self._rsc(f"{DASHBOARD_URL}/{anlage_id}/asset-details")
        points = [
            point for point in _dicts_with(rows, "meteringPoint")
            if _text(point.get("meteringPoint"))
        ]
        for point in points:
            if _text(point.get("serialNumber")):
                return point
        return points[0] if points else {}

    def _anlage_id(self, zaehlpunktnummer: Any) -> int:
        """Return the vertragsID of the Anlage behind a Zählpunkt."""
        key = str(zaehlpunktnummer or "").strip()
        if key not in self._contracts:
            self._discover()
        anlage_id = self._contracts.get(key)
        if anlage_id is None:
            raise SmartmeterQueryError(
                f"{key or 'An empty Zählpunkt'} is not an Anlage of this E-Werk "
                "Gösting account."
            )
        return anlage_id

    def zaehlpunkte(self) -> list[dict[str, Any]]:
        """Return every Anlage of the account as a metering point ([] for none)."""
        anlagen = self._discover()
        if not anlagen:
            return []
        return [{"zaehlpunkte": [dict(info) for _, info in anlagen]}]

    # ------------------------------------------------------------- readings

    def _window(
        self, anlage_id: int, first: date, last: date
    ) -> list[dict[str, Any]]:
        """Read the 15-minute items of the days ``first`` to ``last`` (one request)."""
        query = urlencode({
            "showCurrent": "false", "startDate": _url_date(first),
            "endDate": _url_date(last), "grid": "QH",
        })
        rows = self._rsc(f"{DASHBOARD_URL}/{anlage_id}/consumption?{query}")
        holder = next(_dicts_with(rows, "meteringPointIdentifier"), None)
        if holder is None:
            chart = next(_dicts_with(rows, "consumptionData"), None)
            if chart is not None and chart["consumptionData"] in ({}, []):
                return []  # a period without values may render no export data
            raise SmartmeterQueryError(
                f"The E-Werk Gösting consumption page of Anlage {anlage_id} for "
                f"{first} to {last} holds its values in an unknown form; the portal "
                "may have changed. Please open an issue."
            )
        data = holder.get("data")
        if not isinstance(data, list):
            raise SmartmeterQueryError(
                f"The E-Werk Gösting consumption page of Anlage {anlage_id} for "
                f"{first} to {last} holds its values in an unknown form."
            )
        items = [
            item for item in data if isinstance(item, dict) and "timeStamp" in item
        ]
        daily = sum(
            1 for item in data
            if isinstance(item, dict) and "timeStamp" not in item
            and ("from" in item or "to" in item)
        )
        if daily:
            # Beyond its 15-minute limit the portal answers daily buckets. Nothing
            # of such an answer is usable, and it must not pass for days without
            # values either (the statistics would skip them for good).
            raise SmartmeterQueryError(
                f"The E-Werk Gösting portal answered {daily} "
                f"daily value(s) instead of 15-minute values for Anlage {anlage_id}, "
                f"{first} to {last}."
            )
        LOGGER.debug(
            "E-Werk Gösting: Anlage %s, %s to %s: %d 15-minute item(s)",
            anlage_id, first, last, len(items),
        )
        return items

    def _register_values(
        self, anlage_id: int, items: list[tuple[datetime, dict[str, Any]]]
    ) -> _Registers:
        """Return the values of the consumption and the production register.

        Each register is read from its measured ``G.01`` channel only; the other
        channels are reported once at INFO level. A register whose values are not
        kWh or not 15-minute values is not read, with a warning and its reason in
        ``errors``; the other one is kept.
        """
        channels: dict[str, list[tuple[datetime, dict[str, Any]]]] = {}
        for instant, item in items:
            channel = str(item.get("channelType") or "").strip()
            channels.setdefault(channel, []).append((instant, item))
        measured = {
            f"{register.obis} {_MEASURED_CHANNEL}": register.obis
            for register in _REGISTERS
        }
        unread = sorted(set(channels) - set(measured))
        if unread:
            self._notice(
                logging.INFO, ("channels", anlage_id, tuple(unread)),
                "E-Werk Gösting: Anlage %s also reports %s; only the measured %s "
                "channels are read", anlage_id,
                ", ".join(name or "(no channel type)" for name in unread),
                _MEASURED_CHANNEL,
            )

        registers: dict[str, list[_Value]] = {}
        errors: dict[str, str] = {}
        for channel, obis in measured.items():
            if channel not in channels:
                continue
            try:
                values = _channel_values(channel, channels[channel])
            except SmartmeterQueryError as err:
                self._notice(
                    logging.WARNING, ("register", anlage_id, channel),
                    "E-Werk Gösting: %s of Anlage %s is not read: %s. Please open an "
                    "issue.", channel, anlage_id, err,
                )
                errors[obis] = f"{channel} is not read: {err}"
                continue
            if values:
                registers[obis] = values
        return _Registers(registers, errors)

    def _period_values(
        self, anlage_id: int, start: date, end: date, now: datetime
    ) -> _Registers:
        """Return the register values of an Anlage for the days start to end.

        Read in windows of at most 30 days, each item counted once. The last period
        per Anlage is kept for PERIOD_CACHE_MAX_AGE: reading the same days again
        within that time costs no request - unless a day has settled since it was
        read (see PUBLISHED_BY_HOUR), as values read before must not count.
        """
        now = now.astimezone(timezone.utc)
        cached = self._periods.get(anlage_id)
        if (
            cached is not None
            and cached[:2] == (start, end)
            and timedelta(0) <= now - cached[2] < PERIOD_CACHE_MAX_AGE
            and cached[2] >= _last_settling(now)
        ):
            return cached[3]

        merged: dict[tuple[str, datetime], tuple[datetime, dict[str, Any]]] = {}
        unusable = 0
        for first, last in _qh_windows(start, end):
            for item in self._window(anlage_id, first, last):
                instant = _parse_stamp(item.get("timeStamp"))
                if instant is None or not start <= _start_day(instant) <= end:
                    unusable += 1
                    continue
                channel = str(item.get("channelType") or "").strip()
                merged.setdefault((channel, instant), (instant, item))
        if unusable:
            LOGGER.debug(
                "E-Werk Gösting: %d item(s) of Anlage %s lie outside %s to %s or "
                "carry no usable timestamp", unusable, anlage_id, start, end,
            )
        values = self._register_values(anlage_id, list(merged.values()))
        self._periods[anlage_id] = (start, end, now, values)
        return values

    def consumptions(self) -> list[dict[str, Any]]:
        """E-Werk Gösting has no ready made statistics.

        The day totals are the readings' ``messwerte``, and the quarter hours go
        into the long-term statistics with their real timestamps (statistics.py).
        """
        return []

    def historical_data(
        self, zaehlpunktnummer: str, date_from: date | None = None,
        date_until: date | None = None
    ) -> list[dict[str, Any]]:
        """Return the consumption and production of one Anlage.

        By default the last four Vienna days up to today (DEFAULT_DAYS), as today is
        never published yet and yesterday counts only from PUBLISHED_BY_HOUR on; given
        dates (or datetimes) count inclusively. Per register the totals of the
        complete settled days, and every settled quarter hour: its start in Vienna
        time and the portal's own end. The quarter hours of a day that has not
        settled yet are only counted (``held_back``).
        """
        anlage_id = self._anlage_id(zaehlpunktnummer)
        zaehlpunkt = str(zaehlpunktnummer).strip()
        name, history_start = self._described.get(zaehlpunkt, (zaehlpunkt, None))
        now = _now()
        start, end = _period(date_from, date_until, now.astimezone(VIENNA).date())
        readings = _readings(
            self._period_values(anlage_id, start, end, now), zaehlpunkt, name,
            history_start, now,
        )
        LOGGER.debug(
            "E-Werk Gösting: Anlage %s, %s to %s: %s", anlage_id, start, end,
            ", ".join(
                f"{item['obisCode']} {item['records_read']}"
                + (" (not read)" if "error" in item else "")
                for item in readings
            ) or "no values",
        )
        for item in readings:
            if item["held_back"]:
                LOGGER.debug(
                    "E-Werk Gösting: Anlage %s, %s: %d quarter hour(s) held back "
                    "until %s, while the portal may still change them", anlage_id,
                    item["obisCode"], item["held_back"], item["settles_at"],
                )
        return readings
