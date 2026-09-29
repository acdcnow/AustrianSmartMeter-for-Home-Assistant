"""E-Werk Gösting customer portal client.

E-Werk Gösting Stromversorgungs GmbH (Graz, Styria; ``ewg.at``, which now brands
itself "E-Werk Franz"; grid operator id AT008210) gives its customers a web portal
at ``https://goesting-dav.mein-portal.at/bkp/login``. There is no documented API:
this adapter reads the pages a customer clicks through.

Facts, as reported by a customer who uses the portal and from public web search:

* The portal is a "WPO Frontend" (its HTML title); the same product runs for other
  Austrian utilities on sibling hosts of ``mein-portal.at``. Registration at
  ``/bkp/register`` asks for the contract number and the Anlagennummer.
* The login is the e-mail address and the password of the portal account.
* After the login a dropdown on the top left lists the customer's "Anlagen"
  (facilities), each identified by the ID shown next to the label "Anlage:". A
  customer may have one or several; every Anlage becomes one metering point here,
  with the Anlage ID as its ``zaehlpunktnummer``.
* The values sit behind the sidebar entry "Home" and the button "Zum Verbrauch /
  Erzeugung": a time frame is chosen and exported with the resolution "15 min".
  **The 15-minute export only works when the time frame lies within one calendar
  month** - a wider frame breaks it. This adapter therefore requests one calendar
  month at a time (see :func:`month_windows`) and stitches the months together.
* The values are consumption ("Verbrauch") and production/feed-in ("Erzeugung")
  per 15 minutes, presumably in kWh.

Verified: only what does not need the portal - the month windows, the export
parsers, the Europe/Vienna handling around both DST switches and the readings
contract of the sensor platform, against synthetic exports and pages.

**Unverified: the whole portal protocol.** The environment this adapter was
written in could not reach ``*.mein-portal.at`` at all. How the login form is
submitted, where the Anlagen are listed, which link leads to the values and how
the export is requested follows the portal's visible behaviour and lives in the
last two sections of this module: the page helpers ("portal pages") that
recognise the login form, the Anlagen, the buttons and the time frame form, and
the client's request flow ("Portal protocol"). A check against the live portal
adjusts those two; when a step fails, the debug log describes the page it failed
on (forms, fields and links, never their values). The export format is unknown
as well, so everything that reads it is tolerant instead of clever:

* :func:`parse_export` reads CSV/TSV text (``;`` ``,`` tab or ``|``, decimal comma,
  a header line anywhere in the first lines) in the wide layout (one column per
  register) and in the long layout (one row per register, told apart by an OBIS
  or type column), and JSON as a list of records, an envelope, parallel arrays or
  chart series.
* Column and key names are matched case, space and umlaut insensitively against
  German and English names; an OBIS code in a name or cell decides the register.
* Timestamps without an offset are Europe/Vienna wall clock time. In the hour
  that the autumn switch repeats, the second occurrence of a local time is the
  later (standard time) instant. A Von/Bis pair - also as separate date and
  time columns - is stamped with the END of its interval, a single timestamp is
  taken as given.

A shape the parsers do not recognise yields no values and one warning with a
short snippet. A shape that is recognised but cannot be read safely - another
resolution than 15 minutes, a power unit, cumulative meter readings, one
timestamp with two different values, an Excel or PDF file - raises a
:class:`SmartmeterQueryError` that says what was found. Neither yields a wrong
number. Nor does an account with several Anlagen: an export is only requested
once a page shows the portal switched to the Anlage (or the time frame form
chooses it), and an export that names another Anlage is refused.
"""
from __future__ import annotations

import calendar
import csv
import json
import logging
import math
import re
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from html import unescape
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qsl, urljoin, urlsplit

import requests

from .base import SmartmeterClient
from .errors import (
    SmartmeterConnectionError,
    SmartmeterLoginError,
    SmartmeterQueryError,
)

LOGGER = logging.getLogger(__name__)

PORTAL_HOST = "https://goesting-dav.mein-portal.at"
PORTAL_BASE = f"{PORTAL_HOST}/bkp"
LOGIN_URL = f"{PORTAL_BASE}/login"

REQUEST_TIMEOUT = 30

# The portal does not tell how long its session lives. Like client_noe.py, the
# session is renewed proactively instead of waiting for the login page to appear.
SESSION_MAX_AGE = timedelta(minutes=20)

# The only resolution this adapter reads: the portal's "15 min" export.
INTERVAL_MINUTES = 15

# How an Anlage is described in Home Assistant.
DEVICE_MODEL = "E-Werk Gösting 15-minute values"
GRID_OPERATOR = "E-Werk Gösting Stromversorgungs GmbH"

# Energy consumed / fed in within an interval (like client_noe.py). The portal is
# not known to expose the cumulative meter readings (1.8.0 / 2.8.0).
OBIS_CONSUMPTION = "1-1:1.9.0"
OBIS_PRODUCTION = "1-1:2.9.0"

# sensor.py reports this value type as a period value (state_class total).
VALUE_TYPE = "QUARTER_HOUR"
UNIT_WH = "Wh"

USER_AGENT = (
    "AustriaSmartmeter-HASS/1.2 "
    "(+https://github.com/acdcnow/AustrianSmartMeter-for-Home-Assistant)"
)

# The two registers of the export, and what the sensor platform makes of them.
CONSUMPTION = "consumption"
PRODUCTION = "production"
_REGISTERS = {
    CONSUMPTION: (OBIS_CONSUMPTION, "Consumption 15 min"),
    PRODUCTION: (OBIS_PRODUCTION, "Production 15 min"),
}

# Column and key names, folded with _normalise() (lower case, umlauts written out,
# nothing but a-z and 0-9): the German spelling a portal uses first, then the
# English one. Every name belongs to exactly one role.
_ROLE_NAMES: tuple[tuple[str, frozenset[str]], ...] = (
    ("stamp", frozenset({
        "zeitpunkt", "zeitpunkte", "zeitstempel", "timestamp", "timestamps",
        "datetime", "datumuhrzeit", "datumzeit", "messzeitpunkt", "ablesezeitpunkt",
        "ts", "x",
    })),
    ("date", frozenset({
        "datum", "date", "dates", "tag", "day", "messdatum", "ablesedatum",
    })),
    ("time", frozenset({"zeit", "uhrzeit", "time", "times", "messzeit"})),
    ("start", frozenset({
        "von", "ab", "start", "beginn", "begin", "from", "datumvon", "vondatum",
        "zeitvon", "vonzeit", "uhrzeitvon", "startzeit", "startdatum", "zeitraumvon",
        "intervallvon", "intervallbeginn", "startdate", "starttime", "startdatetime",
        "periodstart", "datetimefrom", "datefrom", "timefrom", "fromdate",
        "validfrom", "gueltigab", "zeitpunktvon",
    })),
    ("end", frozenset({
        "bis", "ende", "end", "to", "until", "datumbis", "bisdatum", "zeitbis",
        "biszeit", "uhrzeitbis", "endzeit", "enddatum", "zeitraumbis", "intervallbis",
        "intervallende", "enddate", "endtime", "enddatetime", "periodend",
        "datetimeto", "dateto", "timeto", "todate", "validto", "gueltigbis",
        "zeitpunktbis",
    })),
    (CONSUMPTION, frozenset({
        "verbrauch", "bezug", "consumption", "wirkenergiebezug", "bezugwirkenergie",
        "verbrauchwirkenergie", "wirkenergieverbrauch", "netzbezug", "strombezug",
        "energiebezug", "stromverbrauch", "energieverbrauch", "verbrauchswert",
        "verbrauchbezug", "bezugverbrauch",
    })),
    (PRODUCTION, frozenset({
        "erzeugung", "einspeisung", "lieferung", "production", "feedin", "generation",
        "wirkenergielieferung", "lieferungwirkenergie", "wirkenergieeinspeisung",
        "einspeisungwirkenergie", "wirkenergieerzeugung", "netzeinspeisung",
        "stromerzeugung", "energieerzeugung", "ueberschusseinspeisung",
        "einspeisewert", "einspeisunglieferung", "erzeugungeinspeisung",
    })),
    ("value", frozenset({
        "wert", "werte", "value", "values", "messwert", "messwerte", "menge",
        "energie", "wirkenergie", "energiemenge", "amount", "quantity", "y",
    })),
    ("register", frozenset({
        "obis", "obiscode", "obiskennzahl", "obiskennziffer", "kennzahl", "register",
        "typ", "type", "art", "richtung", "energierichtung", "flussrichtung",
        "messgroesse", "zaehlwerk", "kanal", "channel", "direction", "name", "label",
        "bezeichnung", "serie", "series", "messart", "datenart",
    })),
    ("unit", frozenset({
        "einheit", "unit", "units", "me", "mengeneinheit", "uom", "masseinheit",
    })),
    ("quality", frozenset({
        "status", "qualitaet", "q", "quality", "qualitaetbeschreibung",
        "qualitaetskennzeichen", "messwertstatus", "statuscode", "wertstatus",
    })),
)
_NAMES_BY_ROLE = dict(_ROLE_NAMES)
_TIME_ROLES = ("stamp", "date", "time", "start", "end")

# JSON: envelopes around the records, chart series, and tables of columns and rows.
_ENVELOPE_KEYS = frozenset({
    "data", "values", "items", "result", "results", "messwerte", "records", "rows",
    "werte", "entries", "content", "payload", "export", "lastgang", "lastprofil",
    "verbrauchsdaten", "zeitreihe", "zeitreihen",
})
_SERIES_KEYS = frozenset({"series", "datasets", "serien", "reihen", "datenreihen"})
_LABEL_KEYS = frozenset({"labels", "categories", "kategorien", "timestamps",
                         "zeitpunkte"})
_SERIES_NAME_KEYS = frozenset({
    "name", "label", "title", "titel", "bezeichnung", "typ", "type", "art", "obis",
    "obiscode", "register", "key", "legend", "serie", "richtung",
})
_SERIES_DATA_KEYS = frozenset({"data", "values", "werte", "messwerte", "points",
                               "punkte", "items", "records"})
_COLUMN_KEYS = frozenset({"columns", "header", "headers", "spalten", "fields",
                          "felder"})
_ROW_KEYS = frozenset({"rows", "data", "values", "werte", "zeilen", "records"})

# Energy units and their factor to Wh; kWh is the default. A power unit is not an
# energy and is refused rather than converted.
_UNIT_FACTORS = {"wh": 1.0, "kwh": 1000.0, "mwh": 1_000_000.0, "gwh": 1_000_000_000.0}
_POWER_UNITS = frozenset({"w", "kw", "mw", "gw"})

# An OBIS code in a column name or cell ("1-1:1.9.0", "1.8.0", "1-1:2.9.0 G.01"):
# medium 1 is electricity; 1.x.x is consumption, 2.x.x production; 8 is a
# cumulative register, 9 and 29 hold the energy of an interval.
_OBIS = re.compile(
    r"(?<![\w.:-])(?:(\d{1,3})-\d{1,3}:)?(\d{1,2})\.(\d{1,2})\.(\d)(?![\d.])"
)

# Unit and noise in a column name: "(kWh)", "[kWh]", "in kWh", "kWh", "15 min".
_BRACKETED = re.compile(r"[(\[{]([^)\]}]*)[)\]}]")
_IN_UNIT = re.compile(r"\b(?:in|je|per)\s+([A-Za-z]+(?:/[A-Za-z0-9]+)?)", re.IGNORECASE)
_BARE_UNIT = re.compile(r"(?<![A-Za-z])(GWh|MWh|kWh|Wh)(?![A-Za-z])", re.IGNORECASE)
_NOISE = re.compile(r"\b\d+\s*-?\s*min(?:uten|\.)?(?![a-z])|\bviertelstunden?(?:werte?)?\b",
                    re.IGNORECASE)


class _SessionExpired(SmartmeterConnectionError):
    """The portal showed its login page again in the middle of a request."""


# --------------------------------------------------------------------- helpers


def _normalise(value: Any) -> str:
    """Fold a key or a label so that it can be compared.

    Lower case, umlauts written out, everything that is not a letter or a digit
    removed - "Qualität Beschreibung" becomes "qualitaetbeschreibung".
    """
    if value is None:
        return ""
    text = str(value).strip().lower()
    for source, target in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        text = text.replace(source, target)
    return re.sub(r"[^a-z0-9]", "", text)


def _vienna() -> tzinfo:
    """Return the operator's timezone, falling back to UTC without tzdata.

    Home Assistant always ships tzdata; the fallback only keeps a bare interpreter
    usable.
    """
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo("Europe/Vienna")
    except Exception:  # noqa: BLE001 - missing tzdata is not a fatal error here
        return timezone.utc


def _today() -> date:
    """Return the current date in the operator's timezone."""
    return datetime.now(_vienna()).date()


def _snippet(value: Any, limit: int = 200) -> str:
    """Return a short, whitespace collapsed excerpt of a body for a log message."""
    if isinstance(value, (bytes, bytearray)):
        value = bytes(value[: limit * 4]).decode("utf-8", "replace")
    elif not isinstance(value, str):
        value = repr(value)[: limit * 4]
    return " ".join(value.split())[:limit]


def _scalar(value: Any) -> str:
    """Return a text value for a scalar, and an empty string for anything else."""
    if value is None or isinstance(value, (dict, list, tuple, set, bool)):
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).strip()


def _finite(value: int | float) -> float | None:
    """Return a number as a float; None for infinity, NaN or an int beyond float."""
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _number(value: Any, decimal_comma: bool | None = None) -> float | None:
    """Return a value as a float, accepting a decimal comma and thousands marks.

    ``decimal_comma`` is the convention of the whole column when it is known (see
    :func:`_decimal_comma`); without it the last separator is the decimal one, as
    in client_salzburgnetz.py. A cell that is not a plain number yields None.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return _finite(value)
    if not isinstance(value, str):
        return None

    text = value.replace("\u00a0", "").replace("\u202f", "").replace(" ", "").strip()
    sign = -1.0 if text.startswith("-") else 1.0
    text = text.lstrip("+-")
    if not re.fullmatch(r"\d[\d.,]*|[.,]\d+", text):
        return None
    if decimal_comma is True:
        text = text.replace(".", "").replace(",", ".")
    elif decimal_comma is False:
        text = text.replace(",", "")
    elif "," in text and "." in text:
        # 1.234,56 and 1,234.56 both mean the same number: the last mark wins.
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    else:
        text = text.replace(",", ".")
    try:
        result = sign * float(text)
    except ValueError:
        return None
    return result if math.isfinite(result) else None


def _decimal_comma(cells: list[Any]) -> bool | None:
    """Tell from the cells of one column whether "," or "." is the decimal mark.

    A mark followed by other than exactly three digits, or after a lone zero
    ("0,125"), can only be a decimal mark; a mark used twice in a cell can only
    group thousands. Cells like "1.234" alone are ambiguous, so the first cell
    that decides wins and None means that no cell did.
    """
    for cell in cells:
        if not isinstance(cell, str):
            continue
        text = cell.strip().lstrip("+-").replace("\u00a0", "").replace(" ", "")
        if not re.fullmatch(r"\d[\d.,]*", text):
            continue
        commas, dots = text.count(","), text.count(".")
        if commas and dots:
            return text.rfind(",") > text.rfind(".")
        if not commas and not dots:
            continue
        mark = "," if commas else "."
        if commas + dots > 1:
            return mark == "."
        head, _, tail = text.partition(mark)
        if len(tail) != 3 or head == "0":
            return mark == ","
    return None


def _is_unit(text: Any) -> bool:
    """Return True for an energy or power unit ("kWh", "kW", "kWh/15min", …)."""
    folded = _normalise(str(text or "").split("/")[0])
    return folded in _UNIT_FACTORS or folded in _POWER_UNITS


def _unit_factor(unit: Any) -> float:
    """Return the factor that turns a value in ``unit`` into Wh (kWh by default).

    A power unit or anything else that is not an energy unit raises: guessing a
    conversion would put a wrong number into the energy statistics.
    """
    text = _scalar(unit)
    folded = _normalise(text.split("/")[0])
    if not folded:
        return 1000.0
    if folded in _UNIT_FACTORS:
        return _UNIT_FACTORS[folded]
    if folded in _POWER_UNITS:
        raise SmartmeterQueryError(
            f"The E-Werk Gösting export reports power ({text}) instead of energy. "
            "This integration reads 15-minute energy values (kWh); please open an "
            "issue with the export format."
        )
    raise SmartmeterQueryError(
        f"The E-Werk Gösting export uses the unit {text!r}, which this integration "
        "does not know. Please open an issue with the export format."
    )


def _quality(value: Any) -> str:
    """Map the portal's quality text onto the integration's status vocabulary."""
    text = _normalise(value)
    if not text:
        return "VALID"
    # A negated "gültig" ("nicht gültig", "keine gültigen Werte") is not valid.
    if any(token in text for token in ("ungueltig", "invalid", "nichtgueltig", "keinegueltig",
                                       "keingueltig", "nichtvalid", "notvalid")):
        return "INVALID"
    if "gueltig" in text or "valid" in text or text in (
        "ok", "gemessen", "measured", "real", "echt", "abgelesen", "messwert"
    ):
        return "VALID"
    if any(token in text for token in ("ersatz", "geschaetzt", "schaetz", "estimated",
                                       "interpoliert", "prognose", "substitut")):
        return "ESTIMATED"
    return "UNKNOWN"


def _header_parts(raw: Any) -> tuple[str, str]:
    """Split a column name into its folded name and the unit it mentions.

    "Verbrauch (kWh)", "Verbrauch [kWh]", "Verbrauch in kWh" and "Verbrauch kWh"
    all become ("verbrauch", "kWh"); remarks in brackets and a "15 min" are left
    out of the name.
    """
    text = str(raw or "")
    unit = ""
    for match in _BRACKETED.finditer(text):
        if _is_unit(match.group(1)):
            unit = match.group(1).strip()
            break
    if not unit:
        match = _IN_UNIT.search(text)
        if match and _is_unit(match.group(1)):
            unit = match.group(1)
    if not unit:
        match = _BARE_UNIT.search(text)
        if match:
            unit = match.group(1)
    text = _BRACKETED.sub(" ", text)
    text = _IN_UNIT.sub(lambda match: " " if _is_unit(match.group(1)) else match.group(0),
                        text)
    text = _NOISE.sub(" ", _BARE_UNIT.sub(" ", text))
    return _normalise(text), unit


def _obis_register(text: Any) -> tuple[str | None, bool] | None:
    """Return (register, cumulative) of the OBIS code in ``text``.

    None when there is no OBIS code; (None, False) for a code that is not active
    electrical energy (another medium, reactive energy, power, …).
    """
    match = _OBIS.search(str(text or ""))
    if match is None:
        return None
    medium, kind, measure = match.group(1), match.group(2), match.group(3)
    if medium not in (None, "1") or kind not in ("1", "2") or measure not in ("8", "9", "29"):
        return None, False
    return (CONSUMPTION if kind == "1" else PRODUCTION), measure == "8"


def _classify(raw: Any) -> dict[str, Any]:
    """Return what a column (or JSON key) holds: its role, register and unit."""
    name, unit = _header_parts(raw)
    info: dict[str, Any] = {"role": None, "register": None, "unit": unit,
                            "cumulative": False}
    obis = _obis_register(raw)
    if obis is not None:
        if obis[0] is not None:
            info.update(role="value", register=obis[0], cumulative=obis[1])
        return info
    for role, names in _ROLE_NAMES:
        if name in names:
            if role in _REGISTERS:
                info.update(role="value", register=role)
            else:
                info["role"] = role
            return info
    if not name and unit:
        # A column named after its unit only ("kWh") holds values.
        info["role"] = "value"
    return info


def _register_of(label: Any) -> tuple[str | None, bool, str]:
    """Return the register a cell or series name stands for, if it is cumulative,
    and the unit it names ("Bezug [Wh]")."""
    if isinstance(label, (dict, list)):
        return None, False, ""
    name, unit = _header_parts(label)
    obis = _obis_register(label)
    if obis is not None:
        return obis[0], obis[1], unit
    for register in _REGISTERS:
        if name in _NAMES_BY_ROLE[register]:
            return register, False, unit
    return None, False, unit


def _unit_columns(values: list[int], units: list[int]) -> dict[int, int | None]:
    """Pair every value column with its unit column, if it has one.

    As many unit columns as value columns pair up in order ("Verbrauch;Einheit;
    Einspeisung;Einheit"); a single unit column serves every value column;
    otherwise a value column takes the nearest unit column on its right that
    comes before the next value column.
    """
    if len(units) == len(values) or len(units) == 1:
        return {index: units[min(position, len(units) - 1)]
                for position, index in enumerate(values)}
    bounds = values[1:] + [math.inf]
    return {index: next((unit for unit in units if index < unit < bound), None)
            for index, bound in zip(values, bounds)}


# ------------------------------------------------------------------ timestamps

_DATE_FORMATS = ("%d.%m.%Y", "%d.%m.%y", "%Y-%m-%d", "%d-%m-%Y", "%Y/%m/%d",
                 "%d/%m/%Y", "%Y.%m.%d")
_MOMENT = re.compile(
    r"(?P<day>\d{1,4}[./-]\d{1,2}[./-]\d{1,4})"
    r"(?:(?:\s*,)?[ T]+(?P<clock>\d{1,2}:\d{2}(?::\d{2})?(?:[.,]\d+)?))?"
    r"\s*(?P<zone>Z|UTC|GMT|[+-]\d{2}(?::?\d{2})?)?"
)
_CLOCK = re.compile(r"(\d{1,2}):(\d{2})(?::(\d{2}))?(?:[.,]\d+)?")
# What a date or a clock time looks like, to tell an unreadable one from a remark
# ("2A:00" is the first 02:00 of the autumn switch in some German tables).
_TIMED = re.compile(r"\d{1,2}[AB]?:\d{2}|\d{1,4}[./-]\d{1,2}[./-]\d{1,4}")
_WEEKDAY = re.compile(r"^[A-Za-zÄÖÜäöü]{2,10}\.?,?\s+(?=\d)")
_RANGE_SEPARATORS = (" - ", " – ", " — ", " bis ", "–", "—", "-")
# "2025-10-15 00:00-00:15": a date and clock time, a dash and a bare clock time.
_CLOCK_RANGE = re.compile(
    r"(?P<left>.*\d{1,2}:\d{2}(?::\d{2})?)\s*[-–—]\s*(?P<right>\d{1,2}:\d{2}(?::\d{2})?)"
)
# "24:00" only occurs in a table that labels every quarter hour by its end.
_DAY_END = re.compile(r"(?<![\d:])24:00(?::00)?(?![\d:])")


def _from_epoch(value: float) -> datetime | None:
    """Return epoch seconds or milliseconds as a UTC timestamp, if plausible."""
    seconds = value / 1000 if abs(value) > 100_000_000_000 else value
    if not 946_684_800 <= seconds <= 4_102_444_800:  # 2000 .. 2100
        return None
    return datetime.fromtimestamp(seconds, tz=timezone.utc)


def _parse_day(text: str) -> date | None:
    """Parse a date in German or ISO spelling (years 2000 to 2100 only)."""
    for pattern in _DATE_FORMATS:
        try:
            parsed = datetime.strptime(text, pattern).date()
        except ValueError:
            continue
        if 2000 <= parsed.year <= 2100:
            return parsed
    return None


def _clock(text: str) -> timedelta | None:
    """Parse a clock time into the time since midnight; "24:00" closes a day."""
    match = _CLOCK.fullmatch(text)
    if match is None:
        return None
    hours, minutes, seconds = int(match.group(1)), int(match.group(2)), int(match.group(3) or 0)
    if minutes > 59 or seconds > 59 or hours > 24 or (hours == 24 and (minutes or seconds)):
        return None
    return timedelta(hours=hours, minutes=minutes, seconds=seconds)


def _zone(text: str) -> timezone | None:
    """Return the timezone of a "Z", "UTC" or "+02:00" suffix.

    None for an offset that no timezone has (beyond 14 hours, or minutes other
    than 00, 30 and 45): such a suffix is not an offset at all.
    """
    if text[0] not in "+-":
        return timezone.utc
    digits = text[1:].replace(":", "")
    hours, minutes = int(digits[:2]), int(digits[2:4] or 0)
    if hours > 14 or minutes not in (0, 30, 45):
        return None
    offset = timedelta(hours=hours, minutes=minutes)
    return timezone(-offset if text[0] == "-" else offset)


def _parse_moment(value: Any) -> datetime | date | timedelta | None:
    """Parse one timestamp cell.

    Returns a datetime (a naive one is Europe/Vienna wall clock time), a date, or
    a clock time as the timedelta since midnight. Epoch seconds and milliseconds
    are UTC.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (datetime, date)):
        return value
    if isinstance(value, (int, float)):
        number = _finite(value)
        return _from_epoch(number) if number is not None else None
    if not isinstance(value, str):
        return None

    text = _WEEKDAY.sub("", " ".join(value.split()))
    if re.fullmatch(r"\d{10}(?:\d{3})?(?:\.\d+)?", text):
        return _from_epoch(float(text))
    if _CLOCK.fullmatch(text):
        return _clock(text)
    match = _MOMENT.fullmatch(text)
    if match is None:
        return None
    zone = match.group("zone")
    if zone and zone[0] in "+-" and not match.group("day")[:4].isdigit():
        # "01.10.2024 00:00-00:15" is an interval, not an offset of -00:15.
        return None
    day = _parse_day(match.group("day"))
    if day is None or not match.group("clock"):
        return day
    clock = _clock(match.group("clock"))
    if clock is None:
        return None
    moment = datetime.combine(day, time()) + clock
    if not zone:
        return moment
    offset = _zone(zone)
    return moment.replace(tzinfo=offset) if offset is not None else None


def _clock_range(text: str) -> tuple[datetime, timedelta] | None:
    """Read "<date> 00:00-00:15" as an interval, not as a time at UTC-00:15.

    The clock time after the dash ends the interval when it lies after the
    first one, is 24:00, is 00:00 after 23:00 (the last interval of a day), or
    is the repeated autumn hour after the switch ("02:45-02:00"); anything else
    is left to _parse_moment. A Europe/Vienna export has no negative UTC offsets.
    """
    match = _CLOCK_RANGE.fullmatch(" ".join(text.split()))
    if match is None:
        return None
    first, last = _parse_moment(match.group("left")), _clock(match.group("right"))
    if not isinstance(first, datetime) or first.tzinfo is not None or last is None:
        return None
    midnight = datetime.combine(first.date(), time())
    if last > first - midnight or last == timedelta(days=1) \
            or (not last and first - midnight >= timedelta(hours=23)) \
            or (_is_ambiguous(midnight + last) and _utc(midnight + last, 1) > _utc(first)):
        return first, last
    return None


def _parse_span(value: Any) -> tuple[Any, Any]:
    """Parse a cell that may hold an interval ("00:00 - 00:15"): (first, last)."""
    if isinstance(value, str):
        found = _clock_range(value)
        if found is not None:
            return found
    moment = _parse_moment(value)
    if moment is not None or not isinstance(value, str):
        return moment, None
    for separator in _RANGE_SEPARATORS:
        left, found, right = value.partition(separator)
        if not found:
            continue
        first, last = _parse_moment(left), _parse_moment(right)
        if isinstance(first, (datetime, timedelta)) and isinstance(last, (datetime, timedelta)):
            return first, last
    return None, None


def _complete(moment: Any, day: date | None) -> datetime | None:
    """Complete a parsed cell to a datetime; a clock time takes ``day``."""
    if isinstance(moment, datetime):
        return moment
    if isinstance(moment, date):
        return datetime.combine(moment, time())
    if isinstance(moment, timedelta) and day is not None:
        return datetime.combine(day, time()) + moment
    return None


def _day_of(moment: Any) -> date | None:
    """Return the date of a parsed cell that has one (a date or a datetime)."""
    if isinstance(moment, datetime):
        return moment.date()
    return moment if isinstance(moment, date) else None


def _merge(parts: list[Any], day: date | None) -> datetime | date | None:
    """Merge the parsed cells of one side of a row into one moment.

    A full timestamp wins; else a date and a clock time from two columns
    ("Startdatum" and "Startzeit") are combined, and a clock time alone takes
    ``day``. A date alone stays a date.
    """
    moment = next((part for part in parts if isinstance(part, datetime)), None)
    if moment is not None:
        return moment
    own = next((part for part in parts if isinstance(part, date)), None)
    clock = next((part for part in parts if isinstance(part, timedelta)), None)
    if clock is not None and (own or day) is not None:
        return datetime.combine(own or day, time()) + clock
    return own


def _has_clock(row: list[Any], roles: dict[str, list[int]]) -> bool:
    """Return True when a time cell of the row holds a clock time."""
    for role in _TIME_ROLES:
        for index in roles.get(role, ()):
            if index < len(row) and row[index] not in (None, ""):
                first, last = _parse_span(row[index])
                if last is not None or isinstance(first, (datetime, timedelta)):
                    return True
    return False


def _unread_time(row: list[Any], roles: dict[str, list[int]]) -> bool:
    """Return True when a time cell of the row looks like a date or time but
    cannot be read ("26.10.2025 02:00 MESZ", "02:00 A")."""
    return any(index < len(row) and _TIMED.search(_scalar(row[index]))
               and _parse_span(row[index]) == (None, None)
               for role in _TIME_ROLES for index in roles.get(role, ()))


def _row_span(row: list[Any], roles: dict[str, list[int]], clocked: bool = True
              ) -> tuple[datetime, datetime | None, bool] | date | None:
    """Return (start or point in time, end or None, end took its date elsewhere).

    A Von/Bis pair makes an interval: two columns, one cell like "00:00 - 00:15",
    or a date and a clock time column for either side ("Startdatum;Startzeit;
    Enddatum;Endzeit"). Otherwise the row has one point in time. A clock time
    takes its date from its own side, else from a date column; the end of an
    interval also from its start. A cell with a date but no clock time only gives
    the day: in a table with clock times (``clocked``) such a row is a daily
    "Summe" and is returned as that date, for the caller to skip; only a table
    without any clock time has one value per day, at midnight. None: the row has
    no usable time, or a cell that looks like a date or time could not be read.
    """
    day: date | None = None
    day_moment: datetime | None = None
    starts: list[Any] = []
    ends: list[Any] = []
    points: list[Any] = []
    unread = False
    for role in _TIME_ROLES:
        for index in roles.get(role, ()):
            if index >= len(row) or row[index] in (None, ""):
                continue
            first, last = _parse_span(row[index])
            if first is None:
                unread = unread or bool(_TIMED.search(_scalar(row[index])))
                continue
            if last is not None:
                starts.append(first)
                ends.append(last)
            elif role in ("start", "end"):
                (starts if role == "start" else ends).append(first)
            elif role == "date" or not isinstance(first, (datetime, timedelta)):
                # A date without a clock time gives the day, whatever its column.
                if day is None and isinstance(first, date):
                    day = _day_of(first)
                if day_moment is None and isinstance(first, datetime):
                    day_moment = first
            else:
                points.append(first)

    start = _merge(starts, day)
    end = _merge(ends, day or _day_of(start))
    if not clocked:
        # A table without clock times: a date stands for its midnight.
        start, end = _complete(start, None), _complete(end, None)
    if isinstance(start, datetime) and isinstance(end, datetime):
        return start, end, not any(isinstance(part, date) for part in ends)
    for moment in (_merge(points, day), start, end, day_moment):
        if isinstance(moment, datetime):
            return moment, None, False
    day = day or _day_of(start) or _day_of(end)
    if unread or day is None:
        return None
    return day if clocked else (datetime.combine(day, time()), None, False)


def _utc(moment: datetime, fold: int = 0) -> datetime:
    """Return a timestamp in UTC; a naive one is Europe/Vienna wall clock time."""
    if moment.tzinfo is not None:
        return moment.astimezone(timezone.utc)
    return moment.replace(tzinfo=_vienna(), fold=fold).astimezone(timezone.utc)


def _is_ambiguous(moment: datetime) -> bool:
    """Return True for a Vienna wall clock time that the autumn switch repeats."""
    if moment.tzinfo is not None:
        return False
    zone = _vienna()
    first = moment.replace(tzinfo=zone, fold=0)
    if first.utcoffset() == moment.replace(tzinfo=zone, fold=1).utcoffset():
        return False
    # A time in the spring gap differs by fold as well, but does not exist:
    # it does not survive the round trip through UTC.
    return first.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) == moment


def _fold_series(moments: list[datetime]) -> list[int]:
    """Return the fold of every timestamp of one series, in row order.

    The autumn switch repeats the hour from 02:00 to 03:00. An export without
    offsets lists that hour twice: the first time is summer time (fold 0), the
    second time standard time (fold 1) - the other way round when the rows run
    backwards in time. The direction is read around every run of rows in that
    hour, from the rows just before and after it: an export that lists its days
    newest first, but the quarter hours of a day in order, runs forward there.
    No row is dropped.
    """
    folds = [0] * len(moments)
    ambiguous = [_is_ambiguous(moment) for moment in moments]
    first = 0
    while first < len(moments):
        if not ambiguous[first]:
            first += 1
            continue
        last = first
        while last + 1 < len(moments) and ambiguous[last + 1]:
            last += 1
        before = next((moments[index] for index in range(first - 1, -1, -1)
                       if moments[index].tzinfo is None), None)
        after = next((moments[index] for index in range(last + 1, len(moments))
                      if moments[index].tzinfo is None), None)
        backwards = (before > after if before is not None and after is not None
                     else before > moments[first] if before is not None
                     else moments[last] > after if after is not None else False)
        seen: Counter[datetime] = Counter()
        for index in range(first, last + 1):
            folds[index] = int((seen[moments[index]] > 0) != backwards)
            seen[moments[index]] += 1
        first = last + 1
    return folds


def _interval_end(first: datetime, start: datetime, end: datetime,
                  end_is_clock: bool) -> datetime | None:
    """Return the end of an interval in UTC, given its start in UTC.

    The end is the earliest instant after the start that the Bis cell can mean:
    either of its readings in the autumn hour, or the start plus the wall clock
    difference. That reads "02:45 - 03:00" and "02:45 - 02:00" (both the last
    summer time quarter of the switch day) and "01:45 - 03:00" (spring) right. A
    Bis clock time before its Von ("23:45 - 00:00") belongs to the next day.
    """
    shifts = (timedelta(0), timedelta(days=1)) if end_is_clock else (timedelta(0),)
    for shift in shifts:
        moment = end + shift
        if moment.tzinfo is not None:
            candidates = [moment.astimezone(timezone.utc)]
        else:
            candidates = [_utc(moment, 0), _utc(moment, 1)]
            if first.tzinfo is None and moment > first:
                candidates.append(start + (moment - first))
        later = [candidate for candidate in candidates
                 if start < candidate <= start + timedelta(hours=25)]
        if later:
            return min(later)
    return None


def _localise_spans(spans: list[tuple[datetime, datetime | None, bool]],
                    series: list[str], end_labels: bool = False
                    ) -> list[tuple[datetime, date] | None]:
    """Return (UTC timestamp, Vienna day) for every span, None if it is unusable.

    ``series`` names the sequence each span belongs to (the register in the long
    layout, one shared sequence in the wide layout); the repeated autumn hour is
    counted per sequence. The day is the one the interval starts on, so the last
    quarter of a day, stamped 00:00 of the next one, counts for its own day. A
    single timestamp counts for its own day - or, in a table that labels quarter
    hours by their end (``end_labels``: it has a "24:00", or its only time column
    is a Bis column), for the day it ends.
    """
    folds = [0] * len(spans)
    groups: dict[str, list[int]] = {}
    for index, key in enumerate(series):
        groups.setdefault(key, []).append(index)
    for indices in groups.values():
        for index, fold in zip(indices, _fold_series([spans[i][0] for i in indices])):
            folds[index] = fold

    result: list[tuple[datetime, date] | None] = []
    for (first, end, end_is_clock), fold in zip(spans, folds):
        start = _utc(first, fold)
        day = start.astimezone(_vienna()).date()
        if end is None:
            if end_labels:
                day = (start - timedelta(seconds=1)).astimezone(_vienna()).date()
            result.append((start, day))
            continue
        stamp = _interval_end(first, start, end, end_is_clock)
        result.append((stamp, day) if stamp is not None else None)
    return result


# ------------------------------------------------------------- export parsing

_BINARY_SIGNATURES = (
    (b"PK\x03\x04", "an Excel workbook (xlsx) or another ZIP archive"),
    (b"\xd0\xcf\x11\xe0", "an Excel 97-2003 workbook (xls)"),
    (b"%PDF", "a PDF document"),
)


def _looks_cumulative(values: list[float]) -> bool:
    """Return True for a meter reading: a series that never falls but rises, and
    whose values are large against its steps.

    The size keeps a morning of 15-minute production (zeros, then rising) from
    counting: it holds a zero, a meter reading dwarfs its steps.
    """
    if len(values) < 8:
        return False
    steps = [later - earlier for earlier, later in zip(values, values[1:])]
    return all(step >= 0 for step in steps) and any(step > 0 for step in steps) \
        and min(values) > 10 * max(steps)


def _finish(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Check the records of one export and order them by time.

    Two different values for one register and instant mean that a timestamp was
    not read completely (or an autumn hour could not be told apart): the export
    is refused instead of guessed; identical duplicates are dropped. A register
    named by a cumulative OBIS code (x.8.x) whose values never fall holds meter
    readings, not 15-minute values: refused as well.
    """
    unique: dict[tuple[str, datetime], dict[str, Any]] = {}
    conflicts: list[dict[str, Any]] = []
    for entry in entries:
        known = unique.setdefault((entry["register"], entry["timestamp"]), entry)
        if known is not entry and abs(known["wh"] - entry["wh"]) > 1e-9:
            conflicts.append(entry)
    if conflicts:
        first = conflicts[0]
        local = first["timestamp"].astimezone(_vienna())
        raise SmartmeterQueryError(
            f"The E-Werk Gösting export holds {len(conflicts)} timestamp(s) twice with "
            f"different values ({first['register']} at {local:%d.%m.%Y %H:%M}), so its "
            "timestamps cannot be read unambiguously. Please open an issue with the "
            "export format."
        )

    records = sorted(unique.values(), key=lambda entry: (entry["timestamp"], entry["register"]))
    for register in sorted({record["register"] for record in records if record["cumulative"]}):
        if _looks_cumulative([record["wh"] for record in records
                              if record["register"] == register]):
            raise SmartmeterQueryError(
                f"The {register} column of the E-Werk Gösting export holds cumulative "
                "meter readings (1.8.0 / 2.8.0), not 15-minute values. Please open an "
                "issue with the export format."
            )
    return [{key: record[key] for key in ("register", "timestamp", "wh", "status", "day")}
            for record in records]


def _table_records(header: list[Any], rows: list[list[Any]], decimal_comma: bool | None,
                   register: str | None = None, cumulative: bool = False
                   ) -> list[dict[str, Any]] | None:
    """Read a table - CSV rows, JSON records or chart points - into records.

    Returns None when the header does not describe an export (no timestamp or no
    value column whose register is known). ``register`` is the register of a
    table whose value column does not name it (one chart series).

    The wide layout has one value column per register. The long layout has a
    single value column and a register column (OBIS, Typ, Art, …) whose cells
    decide; rows of other registers (reactive energy, …) are skipped.
    """
    columns = [_classify(name) for name in header]
    roles: dict[str, list[int]] = {}
    for index, column in enumerate(columns):
        if column["role"]:
            roles.setdefault(column["role"], []).append(index)
    values = roles.get("value", [])
    if not values or not any(role in roles for role in _TIME_ROLES):
        return None

    def cell(row: list[Any], index: int | None) -> Any:
        return row[index] if index is not None and index < len(row) else None

    register_column = next(
        (index for index in roles.get("register", [])
         if any(_register_of(cell(row, index))[0] for row in rows)),
        None,
    )
    long_layout = register is None and register_column is not None and len(values) == 1
    chosen: dict[str, int] = {}
    if not long_layout:
        for index in values:
            target = columns[index]["register"] or register
            current = chosen.get(target) if target else None
            if target and (current is None or (columns[current]["cumulative"]
                                               and not columns[index]["cumulative"])):
                chosen[target] = index
        if not chosen:
            return None

    # The decimal mark is decided per table: a column whose cells alone do not
    # tell ("1.250", "2.375") follows the columns that do, then the delimiter.
    value_columns = [values[0]] if long_layout else list(chosen.values())
    marks = {index: _decimal_comma([cell(row, index) for row in rows])
             for index in value_columns}
    decided = set(marks.values()) - {None}
    fallback = decided.pop() if len(decided) == 1 else decimal_comma
    marks = {index: fallback if mark is None else mark for index, mark in marks.items()}
    # A unit: the row's own unit cell, the register cell ("Bezug [Wh]"), the
    # column name, a unit row under the header (";Wh;Wh"), else kWh.
    unit_columns = _unit_columns(values, roles.get("unit", []))
    row_units: dict[int, str] = {}
    quality_column = (roles.get("quality") or [None])[0]

    spans: list[tuple[datetime, datetime | None, bool]] = []
    series: list[str] = []
    pending: list[tuple[list[tuple[str, float, bool]], str]] = []
    unreadable = 0
    lost: list[list[Any]] = []  # rows with a value whose timestamp cannot be read
    clocked = any(_has_clock(row, roles) for row in rows)
    for row in rows:
        span = _row_span(row, roles, clocked)
        if isinstance(span, date):
            continue  # a daily "Summe" row in a table of quarter hours
        if span is None and not spans:
            units = {index: _scalar(cell(row, index)) for index in value_columns
                     if _is_unit(_scalar(cell(row, index)))}
            if units:
                row_units = {**units, **row_units}  # the first unit row counts
                continue
        if span is None:
            unreadable += 1
            if _unread_time(row, roles) and any(_number(cell(row, index)) is not None
                                                for index in value_columns):
                lost.append(row)
            continue
        if long_layout:
            target, flagged, named = _register_of(cell(row, register_column))
            if target is None:
                continue
            pairs = [(target, values[0], flagged or columns[values[0]]["cumulative"], named)]
        else:
            target = ""  # one shared sequence: every row counts for the autumn hour
            pairs = [(name, index, columns[index]["cumulative"] or cumulative, "")
                     for name, index in chosen.items()]
        readings: list[tuple[str, float, bool]] = []
        for name, index, flagged, named in pairs:
            number = _number(cell(row, index), marks[index])
            if number is not None:
                unit = (_scalar(cell(row, unit_columns.get(index))) or named
                        or columns[index]["unit"] or row_units.get(index, ""))
                readings.append((name, number * _unit_factor(unit), flagged))
            elif _scalar(cell(row, index)) or isinstance(cell(row, index), (dict, list)):
                unreadable += 1
        spans.append(span)
        series.append(target)
        pending.append((readings, _quality(cell(row, quality_column))))

    end_labels = any(_DAY_END.search(_scalar(cell(row, index)))
                     for role in _TIME_ROLES for index in roles.get(role, ())
                     for row in rows) \
        or ("end" in roles and not any(role in roles for role in ("stamp", "time", "start")))
    entries: list[dict[str, Any]] = []
    for place, (readings, status) in zip(_localise_spans(spans, series, end_labels),
                                         pending):
        if place is None:
            continue
        for name, wh, flagged in readings:
            entries.append({"register": name, "timestamp": place[0], "wh": wh,
                            "status": status, "day": place[1], "cumulative": flagged})
    if unreadable and not entries:
        LOGGER.warning(
            "E-Werk Gösting: the export has %s row(s) but none with a readable "
            "timestamp and value. Please open an issue with this excerpt: %r",
            len(rows),
            _snippet(" | ".join(_scalar(item) for item in rows[0])),
        )
    elif lost:
        LOGGER.warning(
            "E-Werk Gösting: %s row(s) of the export have a value but a timestamp that "
            "cannot be read; they are left out. Please open an issue with this "
            "excerpt: %r", len(lost), _snippet(" | ".join(_scalar(item) for item in lost[0])),
        )
    return _finish(entries)


def _decode(body: bytes) -> str:
    """Decode an export: UTF-8 (with or without BOM), else Windows-1252/Latin-1."""
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return body.decode(encoding)
        except UnicodeDecodeError:
            continue
    return body.decode("latin-1")


def _cells(line: str, delimiter: str) -> list[str]:
    """Split one line of a CSV export: quotes are honoured, but never beyond the
    line - a 15-minute table has no multi-line cells, and a stray quote must not
    swallow the rows after it."""
    try:
        return next(csv.reader([line], delimiter=delimiter), [])
    except csv.Error:
        return [line]


def _delimiters(line: str) -> list[str]:
    """Return the delimiters a header line may use, the likeliest first.

    The one that splits the line into the most cells comes first - quotes
    honoured, so a ";" inside a quoted column name does not count; ";" before
    tab, "," and "|" on a tie.
    """
    found = [(len(_cells(line, candidate)), -position, candidate)
             for position, candidate in enumerate((";", "\t", ",", "|")) if candidate in line]
    return [candidate for _, _, candidate in sorted(found, reverse=True)]


def _text_records(text: str) -> list[dict[str, Any]] | None:
    """Read a CSV/TSV export; None when no header line is found.

    Exports often open with a few lines about themselves (Anlage, period), so the
    first of the first 30 lines that names a timestamp and a value column - with
    one of the delimiters it may use - is the header. A ";" or tab separated file
    writes decimals with a comma, a "," separated one with a point - unless the
    cells of the table say otherwise.
    """
    lines = text.lstrip("\ufeff").splitlines()
    for position, line in enumerate(lines[:30]):
        for delimiter in _delimiters(line):
            header = _cells(line, delimiter)
            roles = {_classify(name)["role"] for name in header}
            if "value" not in roles or not roles.intersection(_TIME_ROLES):
                continue
            rows: list[list[str]] = []
            split = 0
            for row in (_cells(following, delimiter) for following in lines[position + 1:]):
                while len(row) > len(header) and not row[-1].strip():
                    row.pop()
                if len(row) > len(header):
                    split += 1  # most likely a decimal comma in a comma separated file
                elif any(item.strip() for item in row):
                    rows.append([item.strip() for item in row])
            found = _table_records(header, rows,
                                   {";": True, "\t": True, ",": False}.get(delimiter))
            if found is None:
                continue
            if split:
                LOGGER.warning(
                    "E-Werk Gösting: %s row(s) of the export have more cells than its "
                    "header and were not read: %r", split, _snippet(line)
                )
            return found
    return None


def _series_list(series: list[dict[str, Any]], labels: list[Any] | None
                 ) -> list[dict[str, Any]] | None:
    """Read chart series: every series has a register name and a list of points.

    Points are [time, value] pairs, {x, y} (or any record) objects, or bare values
    that take their time from the chart's labels.
    """
    records: list[dict[str, Any]] = []
    recognised = False
    for item in series:
        register: str | None = None
        cumulative = False
        unit = name_unit = ""
        data: list[Any] | None = None
        for key, value in item.items():
            folded = _normalise(key)
            if folded in _SERIES_NAME_KEYS and register is None and isinstance(value, str):
                register, cumulative, name_unit = _register_of(value)
            elif folded in _NAMES_BY_ROLE["unit"] and isinstance(value, str):
                unit = value
            elif folded in _SERIES_DATA_KEYS and isinstance(value, list) and data is None:
                data = value
        if register is None or data is None:
            continue
        unit = unit or name_unit
        if data and all(isinstance(point, dict) for point in data):
            header = list(dict.fromkeys(key for point in data for key in point))
            rows = [[point.get(key) for key in header] for point in data]
        elif all(isinstance(point, (list, tuple)) and len(point) >= 2 for point in data):
            header, rows = ["x", "y"], [[point[0], point[1]] for point in data]
        elif labels is not None and len(labels) >= len(data):
            header, rows = ["x", "y"], [[labels[i], point] for i, point in enumerate(data)]
        else:
            continue
        if unit:
            header, rows = header + ["einheit"], [row + [unit] for row in rows]
        found = _table_records(header, rows, None, register, cumulative)
        if found is not None:
            recognised = True
            records.extend(found)
    return sorted(records, key=lambda record: (record["timestamp"], record["register"])) \
        if recognised else None


def _series_records(payload: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Read {"series": [...]} (Highcharts style) or {"labels", "datasets"} (Chart.js)."""
    labels: list[Any] | None = None
    for key, value in payload.items():
        folded = _normalise(key)
        if folded in _LABEL_KEYS and isinstance(value, list):
            labels = value
        elif folded == "xaxis" and isinstance(value, dict) \
                and isinstance(value.get("categories"), list):
            labels = value["categories"]
    for key, value in payload.items():
        if _normalise(key) in _SERIES_KEYS and isinstance(value, list) and value \
                and all(isinstance(item, dict) for item in value):
            return _series_list(value, labels)
    return None


def _parallel_records(payload: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Read parallel arrays: {"timestamps": [...], "verbrauch": [...], ...}."""
    arrays = {key: value for key, value in payload.items()
              if isinstance(value, list) and value
              and not any(isinstance(item, (dict, list)) for item in value)}
    if len(arrays) < 2:
        return None
    length = Counter(len(value) for value in arrays.values()).most_common(1)[0][0]
    header = [key for key, value in arrays.items() if len(value) == length]
    rows = [[arrays[key][position] for key in header] for position in range(length)]
    unit = next((value for key, value in payload.items()
                 if _normalise(key) in _NAMES_BY_ROLE["unit"] and isinstance(value, str)), "")
    if unit:
        header, rows = header + ["einheit"], [row + [unit] for row in rows]
    return _table_records(header, rows, None)


def _columns_records(payload: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Read a table of column names and rows: {"columns": [...], "rows": [[...]]}."""
    header: list[Any] | None = None
    for key, value in payload.items():
        if _normalise(key) in _COLUMN_KEYS and isinstance(value, list) and value:
            names = [item.get("name") or item.get("label") or item.get("title")
                     if isinstance(item, dict) else item for item in value]
            if all(isinstance(name, str) for name in names):
                header = names
                break
    if header is None:
        return None
    for key, value in payload.items():
        if _normalise(key) in _ROW_KEYS and isinstance(value, list) \
                and all(isinstance(row, (list, tuple)) for row in value):
            return _table_records(header, [list(row) for row in value], None)
    return None


def _list_records(items: list[Any], depth: int) -> list[dict[str, Any]] | None:
    """Read a JSON list: records, chart series, rows under a header row, CSV lines."""
    if not items:
        return []
    if all(isinstance(item, dict) for item in items):
        found = _series_list(items, None)
        if found is not None:
            return found
        header = list(dict.fromkeys(key for item in items for key in item))
        found = _table_records(header, [[item.get(key) for key in header] for item in items],
                               None)
        if found is not None:
            return found
        for item in items[:3]:
            found = _json_records(item, depth + 1)
            if found is not None:
                return found
        return None
    if all(isinstance(item, (list, tuple)) for item in items) \
            and all(isinstance(name, str) for name in items[0]):
        return _table_records(list(items[0]), [list(row) for row in items[1:]], None)
    if all(isinstance(item, str) for item in items):
        return _text_records("\n".join(items))
    return None


def _json_records(payload: Any, depth: int = 0) -> list[dict[str, Any]] | None:
    """Read a JSON export; None when its shape is not recognised.

    Tried in this order: a list (records, series, rows, CSV lines), chart series,
    parallel arrays, a table of columns and rows, and an envelope (``data``,
    ``values``, ``items``, ``result(s)``, ``messwerte``, ``records``, ``rows``, …).
    """
    if depth > 6:
        return None
    if isinstance(payload, list):
        return _list_records(payload, depth)
    if not isinstance(payload, dict):
        return None
    for reader in (_series_records, _parallel_records, _columns_records):
        found = reader(payload)
        if found is not None:
            return found
    for key, value in payload.items():
        if _normalise(key) in _ENVELOPE_KEYS and isinstance(value, (dict, list)):
            found = _json_records(value, depth + 1)
            if found is not None:
                return found
    return None


def parse_export(body: bytes | str | dict | list, content_type: str | None = None
                 ) -> list[dict[str, Any]]:
    """Read an export of the portal into normalised records (no I/O).

    Every record is ``{"register": "consumption" | "production", "timestamp":
    <aware datetime, UTC>, "wh": <float>, "status": "VALID" | "ESTIMATED" |
    "INVALID" | "UNKNOWN", "day": <date>}``; ``day`` is the Europe/Vienna day the
    interval belongs to (the day a Von/Bis interval starts on, the day of a single
    timestamp, the day before for end labels: "24:00", or a Bis column alone).
    Ordered by time.

    An unrecognised body yields [] and one warning with a short snippet (an empty
    body, an HTML page and XML count as unrecognised). A recognised export that
    cannot be read safely raises :class:`SmartmeterQueryError`.
    """
    kind = (content_type or "").split(";")[0].strip().lower()
    records: list[dict[str, Any]] | None = None
    text = ""
    try:
        if isinstance(body, (dict, list)):
            records = _json_records(body)
        else:
            if isinstance(body, (bytes, bytearray)):
                for signature, description in _BINARY_SIGNATURES:
                    if bytes(body[:8]).startswith(signature):
                        raise SmartmeterQueryError(
                            f"The E-Werk Gösting export is {description}, which this "
                            "integration cannot read. Please open an issue saying which "
                            "export formats the portal offers."
                        )
                text = _decode(bytes(body))
            else:
                text = body if isinstance(body, str) else ""
            text = text.lstrip("\ufeff").strip()
            if text and (text[0] in "[{" or "json" in kind):
                try:
                    payload = json.loads(text)
                except ValueError:
                    records = _text_records(text)
                else:
                    records = _json_records(payload)
            elif text and text[0] != "<":
                records = _text_records(text)
    except (ValueError, OverflowError, RecursionError, csv.Error) as err:
        # Whatever else goes wrong in reading one export stays with that export.
        raise SmartmeterQueryError(
            f"The E-Werk Gösting export could not be read ({type(err).__name__}). "
            "Please open an issue with the export format."
        ) from err
    if records is None:
        # Of an HTML page the visible text tells what went wrong, not its <head>.
        page = _parse_page(text) if re.match(r"<(?:!doctype\s+html|html|head|body)\b",
                                              text, re.IGNORECASE) else None
        LOGGER.warning(
            "E-Werk Gösting: the export (%s) was not recognised and yields no values. "
            "Please open an issue with this excerpt: %r",
            kind or "no content type",
            _snippet(page.text) if page is not None and page.text.strip() else _snippet(body),
        )
        return []
    return records


# ---------------------------------------------------------- readings contract


def _wh(value: float) -> float:
    """Round an energy value to 3 decimals (and never report -0.0)."""
    return round(value, 3) + 0.0


def _record_day(record: dict[str, Any]) -> date:
    """Return the Vienna day of a record: its own ``day``, else its timestamp's."""
    day = record.get("day")
    if isinstance(day, date) and not isinstance(day, datetime):
        return day
    return _utc(record["timestamp"]).astimezone(_vienna()).date()


def readings_from_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Turn normalised records into the readings contract of sensor.py.

    One reading per register that has at least one value - consumption
    (``1-1:1.9.0``) first, then production (``1-1:2.9.0``) - each carrying every
    value oldest first, in Wh, with the sum of the register on the same
    Europe/Vienna day as ``day_total_wh``. A register and instant that occurs
    twice (overlapping months) is counted once.
    """
    per_register: dict[str, dict[datetime, tuple[float, str, date]]] = {}
    for record in records:
        register = record.get("register")
        stamp = record.get("timestamp")
        wh = _number(record.get("wh"))
        if register not in _REGISTERS or not isinstance(stamp, datetime) or wh is None:
            continue
        per_register.setdefault(register, {}).setdefault(
            _utc(stamp), (wh, str(record.get("status") or "UNKNOWN"), _record_day(record))
        )

    readings: list[dict[str, Any]] = []
    for register, (obis, name) in _REGISTERS.items():
        values = sorted((per_register.get(register) or {}).items())
        if not values:
            continue
        totals: dict[date, float] = {}
        for _, (wh, _, day) in values:
            totals[day] = totals.get(day, 0.0) + wh
        readings.append({
            "obisCode": obis,
            "name": name,
            "wertetyp": VALUE_TYPE,
            "einheit": UNIT_WH,
            "interval_minutes": INTERVAL_MINUTES,
            "records_read": len(values),
            "messwerte": [
                {
                    "zeitpunkt": stamp.isoformat(timespec="seconds"),
                    "messwert": _wh(wh),
                    "status": status,
                    "day_total_wh": _wh(totals[day]),
                }
                for stamp, (wh, status, day) in values
            ],
        })
    return readings


def _interval_minutes(records: list[dict[str, Any]]) -> float | None:
    """Return the shortest distance between two values of one register, in minutes.

    None when no register has two values. Gaps only make distances longer, so a
    15-minute export always yields 15 here.
    """
    stamps: dict[str, set[datetime]] = {}
    for record in records:
        stamps.setdefault(record["register"], set()).add(record["timestamp"])
    shortest: float | None = None
    for values in stamps.values():
        ordered = sorted(values)
        for earlier, later in zip(ordered, ordered[1:]):
            minutes = (later - earlier).total_seconds() / 60
            if minutes > 0 and (shortest is None or minutes < shortest):
                shortest = minutes
    if shortest is not None and shortest.is_integer():
        return int(shortest)
    return shortest


def month_windows(start: date, end: date) -> list[tuple[date, date]]:
    """Split a period into calendar-month windows, oldest first.

    Every window is ``(max(1st of the month, start), min(last of the month,
    end))``, both inclusive: the portal's 15-minute export only works within one
    calendar month. Datetimes count by their date; a reversed period is swapped.
    """
    if isinstance(start, datetime):
        start = start.date()
    if isinstance(end, datetime):
        end = end.date()
    if start > end:
        start, end = end, start
    windows: list[tuple[date, date]] = []
    first = start
    while first <= end:
        month_end = date(first.year, first.month,
                         calendar.monthrange(first.year, first.month)[1])
        last = min(month_end, end)
        windows.append((first, last))
        first = last + timedelta(days=1)
    return windows


def _as_date(value: Any) -> date | None:
    """Return a date for a date, a datetime or a date string (ISO or German)."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return _parse_day(value.strip()[:10])
    return None


def _period(date_from: Any, date_until: Any) -> tuple[date, date]:
    """Return the period to read: by default yesterday up to today (Vienna)."""
    end = _as_date(date_until) or _today()
    start = _as_date(date_from) or end - timedelta(days=1)
    return (start, end) if start <= end else (end, start)


def _within(records: list[dict[str, Any]], start: date, end: date
            ) -> list[dict[str, Any]]:
    """Keep the records of the days ``start`` to ``end``."""
    return [record for record in records if start <= _record_day(record) <= end]


def _reaches(records: list[dict[str, Any]], end: date) -> bool:
    """Return True when the records hold the last quarter hour of the day ``end``."""
    midnight = datetime.combine(end + timedelta(days=1), time(), tzinfo=_vienna())
    last = midnight.astimezone(timezone.utc) - timedelta(minutes=INTERVAL_MINUTES)
    return any(record["timestamp"] >= last for record in records)


def _check_frame(records: list[dict[str, Any]], start: date, end: date
                 ) -> list[dict[str, Any]]:
    """Return the records of an export for the days ``start`` to ``end``, all of them.

    An export whose values all lie outside the requested window means that the
    time frame was not applied; showing those values as the requested ones would
    be wrong, so that is an error. Values next to the window are kept for the
    period to decide: an export that labels quarter hours by their end holds the
    last one of the window at 00:00 of the following day.
    """
    if records and not _within(records, start, end):
        raise SmartmeterQueryError(
            f"The E-Werk Gösting export for {start:%d.%m.%Y}-{end:%d.%m.%Y} only holds "
            f"values of {_record_day(records[0]):%d.%m.%Y}-"
            f"{_record_day(records[-1]):%d.%m.%Y}; the portal did not apply the time "
            "frame. Please open an issue."
        )
    return records


# ---------------------------------------------------------------- portal pages
#
# Pure helpers for the protocol section at the end of this module. What they
# look for - a login form, the label "Anlage:", the button "Zum Verbrauch /
# Erzeugung", a time frame form with "15 min" - follows the portal's visible
# behaviour and is as unverified as that section.

_ID_TOKEN = r"[A-Za-z0-9][\w./-]{2,39}"
_ANLAGE_LABEL = r"\bAnlage(?:n?nummer|n?-?nr\.?|\s+nr\.?|\s*-?\s*id)?"
_LABEL_SCAN = re.compile(
    _ANLAGE_LABEL + r"\s*:\s*(" + _ID_TOKEN + r")[ \t\u00a0]*([^\n]*)", re.IGNORECASE
)
# The ID after every "Anlage:", read without consuming it: "Anlage: Anlage: 4711001"
# (a label, then the ID in the next element) still finds the ID.
_LABEL_AHEAD = re.compile(_ANLAGE_LABEL + r"\s*:\s*(?=(" + _ID_TOKEN + r"))", re.IGNORECASE)
_ENTRY_LABELLED = re.compile(
    r"^\s*" + _ANLAGE_LABEL + r"\s*:?\s*(" + _ID_TOKEN + r")\s*(.*)$", re.IGNORECASE
)
_ENTRY_ID_FIRST = re.compile(r"^\s*(" + _ID_TOKEN + r")\s*[-–—|:,/]\s*(.*)$")
_CONTRACT_SCAN = re.compile(
    r"\bVertrags?(?:nummer|\s*-?\s*nr\.?|konto)\s*:\s*(" + _ID_TOKEN + r")", re.IGNORECASE
)
_TYPE_WORDS = frozenset({
    "bezug", "verbrauch", "einspeisung", "erzeugung", "photovoltaik", "pv",
    "ueberschusseinspeisung", "volleinspeisung", "waermepumpe", "nachtstrom",
    "speicherheizung", "unterbrechbar", "bezugsanlage", "verbrauchsanlage",
    "einspeiseanlage", "erzeugungsanlage", "pvanlage", "photovoltaikanlage",
})
_POSTCODE = re.compile(
    r"^(?P<street>.*?)[,\s]+(?:A-|AT-)?(?P<zip>\d{4})\s+(?P<city>[^\d,][^,]*?)\s*$"
)
_LOGIN_MARKERS = ("logout", "abmelden", "ausloggen", "anlage")
_EXPORT_WORDS = ("export", "download", "csv", "herunterladen")
_FIFTEEN = re.compile(r"15\s*-?\s*min|viertelstund|quarter|pt15m", re.IGNORECASE)
_START_TOKENS = frozenset({"von", "start", "from", "begin", "beginn", "ab"})
_END_TOKENS = frozenset({"bis", "end", "ende", "to", "until"})
_DATE_TOKENS = frozenset({"date", "datum", "zeitraum", "day", "period", "range"}) \
    | _START_TOKENS | _END_TOKENS
_SCRIPT_URL = re.compile(
    r"(?:location(?:\.href)?\s*=|window\.open\(|navigate(?:ByUrl)?\()\s*['\"]([^'\"]+)['\"]"
)


class _PageParser(HTMLParser):
    """Collect what the portal protocol needs from a page, with the stdlib only.

    Forms with their fields and submit buttons, every option list, the links,
    buttons and list items with their text, the title, the visible text (every
    tag becomes a line break; scripts, styles and templates are left out) - once
    as a whole and once without the texts of option lists - and the script and
    stylesheet URLs, which are logged when the login page turns out to be a
    JavaScript application.
    """

    _COLLECT = ("a", "button", "li", "label", "option", "title")
    _HIDDEN = ("script", "style", "template")

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.forms: list[dict[str, Any]] = []
        self.selects: list[dict[str, Any]] = []
        self.links: list[dict[str, Any]] = []
        self.assets: list[str] = []
        self.title = ""
        self._chunks: list[str] = []
        self._plain: list[str] = []
        self._labels: dict[str, str] = {}
        self._fields: list[dict[str, Any]] = []
        self._open: list[dict[str, Any]] = []
        self._form: dict[str, Any] | None = None
        self._select: dict[str, Any] | None = None
        self._hidden = 0

    @property
    def text(self) -> str:
        """Return the visible text, one line break per tag."""
        return "".join(self._chunks)

    @property
    def plain_text(self) -> str:
        """Return the visible text without the options of dropdowns."""
        return "".join(self._plain)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {name.lower(): value or "" for name, value in attrs}
        self._chunks.append("\n")
        self._plain.append("\n")
        if tag in self._HIDDEN:
            if attributes.get("src"):
                self.assets.append(attributes["src"])
            self._hidden += 1
            return
        if self._hidden:
            return  # inside a <template>: not part of the page (yet)
        if tag == "link" and attributes.get("href"):
            self.assets.append(attributes["href"])
        elif tag == "form":
            self._form = {"attrs": attributes, "action": attributes.get("action", "").strip(),
                          "method": (attributes.get("method") or "get").strip().lower(),
                          "fields": [], "buttons": []}
            self.forms.append(self._form)
        elif tag in ("input", "textarea", "select"):
            field = self._add_field(tag, attributes)
            if tag == "select":
                self._close("option")
                field["options"] = []
                self._select = field
                self.selects.append(field)
        elif tag == "option":
            self._close("option")
        if tag in self._COLLECT:
            element = {"tag": tag, "attrs": attributes, "text": "", "fields": []}
            self._open.append(element)
            if tag == "option" and self._select is not None:
                self._select["options"].append(element)
            elif tag == "button" and self._form is not None:
                self._form["buttons"].append(element)

    def handle_endtag(self, tag: str) -> None:
        self._chunks.append("\n")
        self._plain.append("\n")
        if tag in self._HIDDEN:
            self._hidden = max(0, self._hidden - 1)
            return
        if self._hidden:
            return
        if tag == "form":
            self._form = None
        elif tag == "select":
            self._close("option")
            self._select = None
        if tag in self._COLLECT:
            self._close(tag)

    def handle_data(self, data: str) -> None:
        if self._hidden:
            return
        self._chunks.append(data)
        if self._select is None:
            self._plain.append(data)
        for element in self._open:
            element["text"] += data

    def close(self) -> None:
        super().close()
        for element in reversed(self._open):
            self._finish(element)
        self._open.clear()
        for field in self._fields:
            if not field["label"] and field["id"] in self._labels:
                field["label"] = self._labels[field["id"]]

    def _add_field(self, tag: str, attributes: dict[str, str]) -> dict[str, Any]:
        kind = tag if tag != "input" else (attributes.get("type") or "text").strip().lower()
        field = {"tag": tag, "type": kind, "name": attributes.get("name", ""),
                 "id": attributes.get("id", ""), "value": attributes.get("value", ""),
                 "checked": "checked" in attributes, "attrs": attributes, "label": ""}
        if kind in ("submit", "image", "button", "reset"):
            if self._form is not None:
                self._form["buttons"].append(
                    {"tag": tag, "attrs": attributes, "text": field["value"], "fields": []}
                )
            return field
        for element in self._open:
            if element["tag"] == "label":
                element["fields"].append(field)
        self._fields.append(field)
        if self._form is not None:
            self._form["fields"].append(field)
        return field

    def _close(self, tag: str) -> None:
        """Close the innermost open ``tag`` and whatever was left open inside it."""
        for position in range(len(self._open) - 1, -1, -1):
            if self._open[position]["tag"] == tag:
                for element in reversed(self._open[position:]):
                    self._finish(element)
                del self._open[position:]
                return

    def _finish(self, element: dict[str, Any]) -> None:
        element["text"] = " ".join(element["text"].split())
        tag, attrs = element["tag"], element["attrs"]
        if tag == "title":
            self.title = self.title or element["text"]
        elif tag == "label":
            if attrs.get("for"):
                self._labels[attrs["for"]] = element["text"]
            for field in element["fields"]:
                field["label"] = field["label"] or element["text"]
        elif tag == "option":
            element["value"] = attrs["value"] if "value" in attrs else element["text"]
            element["selected"] = "selected" in attrs
        else:
            self.links.append(element)


def _parse_page(html: Any) -> _PageParser:
    """Parse a page; a broken page is read as far as possible."""
    if isinstance(html, (bytes, bytearray)):
        html = _decode(bytes(html))
    parser = _PageParser()
    try:
        parser.feed(str(html or ""))
        parser.close()
    except Exception as err:  # noqa: BLE001 - html.parser rarely raises at all
        LOGGER.debug("E-Werk Gösting: page only partly parsed: %s", err)
    return parser


def _query(href: str) -> list[tuple[str, str]]:
    """Return the query parameters of a link; none for one that is no URL."""
    try:
        return parse_qsl(urlsplit(href).query)
    except ValueError:  # "http://[broken" and the like
        return []


def _safe_url(url: str) -> str:
    """Return a URL for a log or an error: scheme, host and path only - no user,
    no ";jsessionid=…", query or fragment (where tokens and codes travel)."""
    try:
        parts = urlsplit(str(url or ""))
    except ValueError:
        return "(an unreadable URL)"
    path = parts.path.split(";")[0]
    host = parts.netloc.rpartition("@")[2]
    return f"{parts.scheme + ':' if parts.scheme else ''}//{host}{path}" if host else path


def _join(base: str, href: str) -> str:
    """Resolve a link or form action of a portal page against the page's URL."""
    try:
        return urljoin(base, href)
    except ValueError:
        raise SmartmeterConnectionError(
            f"A page of the E-Werk Gösting portal ({_safe_url(base)}) leads to an "
            "unusable URL."
        ) from None


def _id_like(token: str) -> bool:
    """Return True for an identifier: at least four digits, and not a date."""
    return (len(token) <= 40 and sum(char.isdigit() for char in token) >= 4
            and _parse_day(token) is None)


def _context(element: dict[str, Any]) -> str:
    """Return the folded attributes (and label) of an element, to see what it is about."""
    parts = [f"{name} {value}" for name, value in element["attrs"].items()]
    return _normalise(" ".join(parts + [element.get("label") or ""]))


def _split_entry(rest: str) -> tuple[str, str]:
    """Split the text next to an Anlage ID into (address, type of Anlage)."""
    address: list[str] = []
    kind = ""
    for part in re.split(r"\s+[-–—|]\s+|[()\[\]]", rest or ""):
        part = part.strip(" \t\u00a0,;:-–—|")
        if not part:
            continue
        if not kind and _normalise(part) in _TYPE_WORDS:
            kind = part
        elif re.search(r"[A-Za-zÄÖÜäöüß]", part):
            address.append(part)
    return ", ".join(address)[:120], kind


def _entry(text: str, about_anlagen: bool) -> tuple[str, str] | None:
    """Return (ID, rest) of a list entry like "Anlage 12345 …" or "12345 - Straße"."""
    match = _ENTRY_LABELLED.match(text or "")
    if match:
        return match.group(1), match.group(2)
    if about_anlagen:
        match = _ENTRY_ID_FIRST.match(text or "")
        if match and re.search(r"[A-Za-zÄÖÜäöüß]{3}", match.group(2)):
            return match.group(1), match.group(2)
    return None


def anlagen_from_html(html: str) -> list[dict[str, str]]:
    """Return the Anlagen a portal page lists, in page order, without duplicates.

    Every entry is ``{"id": …, "address": …, "type": …}`` (address and type may
    be empty). Found are: the label "Anlage:" followed by an ID anywhere in the
    visible text, whatever tags stand between them (``<b>Anlage:</b>
    <span>12345</span>``); option lists, links, buttons and list items reading
    "Anlage 12345 …", or "12345 - Musterstraße 1" when the list or link is about
    Anlagen (its name, id, class, label or link says so, or it is a dropdown);
    and an ID in an ``anlage…`` query parameter or data attribute. An ID has at
    least four digits. With exactly one Anlage and one "Vertragsnummer:" on the
    page, that number is added as ``contract``.
    """
    page = _parse_page(html)
    found: dict[str, dict[str, str]] = {}

    def add(token: str, rest: str = "") -> None:
        identifier = unescape(token or "").strip().rstrip("./-")
        if not _id_like(identifier):
            return
        address, kind = _split_entry(unescape(rest or ""))
        entry = found.setdefault(identifier, {"id": identifier, "address": "", "type": ""})
        entry["address"] = entry["address"] or address
        entry["type"] = entry["type"] or kind

    for match in _LABEL_SCAN.finditer(page.text):
        add(match.group(1), match.group(2))
    for select in page.selects:
        about = "anlage" in _context(select)
        for option in select["options"]:
            entry = _entry(option["text"], about)
            if entry:
                add(*entry)
            elif about:
                add(option["value"], option["text"].replace(option["value"], " "))
    for element in page.links:
        context = _context(element)
        entry = _entry(element["text"], "anlage" in context or "dropdown" in context)
        if entry:
            add(*entry)
        pairs = _query(element["attrs"].get("href", ""))
        pairs += [(name, value) for name, value in element["attrs"].items()
                  if name.startswith("data-")]
        for name, value in pairs:
            if "anlage" in _normalise(name):
                add(value, element["text"])

    contracts = {unescape(match.group(1)).rstrip("./-")
                 for match in _CONTRACT_SCAN.finditer(page.text)}
    contracts = {contract for contract in contracts if _id_like(contract)}
    if len(found) == 1 and len(contracts) == 1:
        next(iter(found.values()))["contract"] = contracts.pop()
    return list(found.values())


def _address_parts(address: str) -> dict[str, str]:
    """Split "Musterstraße 1, 8051 Graz" into street, postcode and city."""
    match = _POSTCODE.match(address or "")
    if not match or not match.group("street").strip(" ,"):
        return {}
    return {"strasse": match.group("street").strip(" ,"),
            "postleitzahl": match.group("zip"), "ort": match.group("city").strip()}


def _anlage_info(anlage: dict[str, str]) -> dict[str, Any]:
    """Return the metering point description of one Anlage."""
    identifier = anlage["id"]
    address = anlage.get("address") or ""
    info: dict[str, Any] = {
        "zaehlpunktnummer": identifier,
        "zaehlpunktName": address or f"Anlage {identifier}",
        "zaehlpunktAnlagentyp": anlage.get("type") or "",
        "anlage_id": identifier,
        "geschaeftspartner": GRID_OPERATOR,
        "device_model": DEVICE_MODEL,
    }
    location = _address_parts(address)
    if location:
        info["verbrauchsstelle"] = location
    if anlage.get("contract"):
        info["vertragsnummer"] = anlage["contract"]
    return info


def _tokens(*texts: str) -> set[str]:
    """Split names like "dateFrom" or "filter.von_datum" into lower case words."""
    words: set[str] = set()
    for text in texts:
        spaced = re.sub(r"([a-z])([A-Z])", r"\1 \2", text or "").lower()
        for source, target in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
            spaced = spaced.replace(source, target)
        words.update(re.findall(r"[a-z]+", spaced))
    return words


def _login_form(page: _PageParser, user_field: str = "") -> dict[str, Any] | None:
    """Return the login form of a page: a form with a password field.

    With ``user_field`` only a form that also has that field counts, so that a
    "change password" form is not taken for the login page.
    """
    forms = [form for form in page.forms
             if any(field["type"] == "password" for field in form["fields"])
             and (not user_field
                  or any(field["name"] == user_field for field in form["fields"]))]
    forms.sort(key=lambda form: sum(field["type"] == "password"
                                    for field in form["fields"]) != 1)
    return forms[0] if forms else None


def _scripted(form: dict[str, Any]) -> str:
    """Return why a script, not the browser, submits a form; "" for a plain form.

    Its action is "#…" or "javascript:…", or it names neither an action nor a
    method but has a submit handler (onsubmit, @submit, ng-submit, …). A form
    that names a method but no action is sent to the URL of its page.
    """
    action = form["action"]
    if action.startswith("#") or action.lower().startswith("javascript:"):
        return f"its action is {action[:40]!r}"
    handler = next((name for name in form["attrs"] if "submit" in name), "")
    if handler and not action and not form["attrs"].get("method", "").strip():
        return f"it has a {handler[:30]} handler but no action and no method"
    return ""


def _user_input(form: dict[str, Any]) -> dict[str, Any] | None:
    """Return the first text or e-mail field of a form: the user name."""
    return next((field for field in form["fields"]
                 if field["tag"] == "input" and field["name"]
                 and field["type"] in ("text", "email", "tel")), None)


def _form_data(form: dict[str, Any]) -> dict[str, str]:
    """Return what a browser submits for a form as it stands."""
    data: dict[str, str] = {}
    for field in form["fields"]:
        name = field["name"]
        if not name or field["type"] in ("file", "password"):
            continue
        if field["type"] in ("checkbox", "radio"):
            if field["checked"]:
                data[name] = field["value"] or "on"
        elif field["tag"] == "select":
            options = field.get("options") or []
            chosen = next((option for option in options if option["selected"]),
                          options[0] if options else None)
            if chosen is not None:
                data[name] = chosen["value"]
        else:
            data[name] = field["value"]
    return data


def _press(form: dict[str, Any], data: dict[str, str], words: tuple[str, ...]
           ) -> dict[str, Any] | None:
    """Add the submit button whose text mentions one of ``words`` (else the first)
    and return it: its formaction and formmethod decide where the form goes."""
    buttons = [button for button in form["buttons"]
               if (button["attrs"].get("type") or "submit").lower() in ("submit", "image")]
    chosen = next((button for button in buttons if any(
        word in _normalise(" ".join([button["text"], button["attrs"].get("value", ""),
                                     button["attrs"].get("name", "")]))
        for word in words)), buttons[0] if buttons else None)
    if chosen is not None and chosen["attrs"].get("name"):
        data[chosen["attrs"]["name"]] = chosen["attrs"].get("value", "")
    return chosen


def _href(element: dict[str, Any]) -> str:
    """Return where a link or button leads: href, a data-href or an onclick URL.

    A submit button's formaction is no link: it takes the form data along (see
    EwerkGoestingClient._submit).
    """
    attrs = element["attrs"]
    targets = [attrs.get(key, "").strip() for key in ("href", "data-href", "data-url")]
    match = _SCRIPT_URL.search(attrs.get("onclick", ""))
    for target in targets + [match.group(1) if match else ""]:
        if target and not target.lower().startswith(("#", "javascript:")):
            try:
                urlsplit(target)
            except ValueError:  # "http://[broken": no link to follow
                continue
            return target
    return ""


def _chart_link(page: _PageParser) -> str:
    """Return the target of the button "Zum Verbrauch / Erzeugung"."""
    for element in page.links:
        text = _normalise(element["text"])
        if "verbrauch" in text and "erzeugung" in text and _href(element):
            return _href(element)
    return next((_href(element) for element in page.links
                 if "verbrauch" in _href(element).lower()), "")


def _home_link(page: _PageParser) -> str:
    """Return the target of the sidebar entry "Home"."""
    return next((_href(element) for element in page.links
                 if _normalise(element["text"]) in ("home", "startseite")
                 and _href(element)), "")


def _export_link(page: _PageParser) -> str:
    """Return the target of the first link or button that exports or downloads."""
    for element in page.links:
        target = _href(element)
        text = f"{_normalise(element['text'])} {target.lower()}"
        if target and any(word in text for word in _EXPORT_WORDS):
            return target
    return ""


def _date_fields(form: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the named fields of a form that take a date."""
    fields: list[dict[str, Any]] = []
    for field in form["fields"]:
        if field["tag"] != "input" or not field["name"] or field["type"] in (
            "checkbox", "radio", "password", "file", "email", "number"
        ):
            continue
        placeholder = field["attrs"].get("placeholder", "")
        words = _tokens(field["name"], field["id"], placeholder, field["label"])
        if field["type"] in ("date", "datetime-local", "month") or words & _DATE_TOKENS \
                or re.search(r"tt\.mm\.jjjj|dd\.mm\.yyyy|yyyy-mm-dd", placeholder, re.I):
            fields.append(field)
    return fields


def _period_form(page: _PageParser) -> dict[str, Any] | None:
    """Return the form with the most date fields (the time frame), if any."""
    candidates = [(len(_date_fields(form)), -position, form)
                  for position, form in enumerate(page.forms)
                  if not any(field["type"] == "password" for field in form["fields"])]
    candidates = [candidate for candidate in candidates if candidate[0]]
    return max(candidates, key=lambda candidate: candidate[:2])[2] if candidates else None


def _date_value(field: dict[str, Any], day: date, is_end: bool) -> str:
    """Return a date the way a field takes it: dd.mm.yyyy, or ISO for date inputs."""
    if field["type"] == "date" or re.match(r"\d{4}-\d{2}-\d{2}", field["value"] or ""):
        return day.isoformat()
    if field["type"] == "datetime-local":
        return f"{day.isoformat()}T{'23:59' if is_end else '00:00'}"
    if field["type"] == "month":
        return f"{day:%Y-%m}"
    return f"{day:%d.%m.%Y}"


def _fill_period(form: dict[str, Any], data: dict[str, str], start: date, end: date
                 ) -> bool:
    """Enter the time frame into the form's start and end fields; False when the
    form has no recognisable start and end."""
    fields = _date_fields(form)
    words = [_tokens(field["name"], field["id"], field["label"]) for field in fields]
    starts = [field for field, found in zip(fields, words) if found & _START_TOKENS]
    ends = [field for field, found in zip(fields, words)
            if found & _END_TOKENS and not found & _START_TOKENS]
    rest = [field for field in fields
            if all(field is not other for other in starts + ends)]
    if len(fields) == 1 and not starts and not ends:
        # One field for the whole frame, as date range pickers have it.
        data[fields[0]["name"]] = f"{start:%d.%m.%Y} - {end:%d.%m.%Y}"
        return True
    if not starts and rest:
        starts.append(rest.pop(0))
    if not ends and rest:
        ends.append(rest.pop(0))
    for field in starts:
        data[field["name"]] = _date_value(field, start, False)
    for field in ends:
        data[field["name"]] = _date_value(field, end, True)
    return bool(starts and ends)


def _fill_resolution(form: dict[str, Any], data: dict[str, str]) -> bool:
    """Choose "15 min" in a select or radio group of the form; False if none."""
    for field in form["fields"]:
        if not field["name"]:
            continue
        if field["tag"] == "select":
            for option in field.get("options") or []:
                if _FIFTEEN.search(f"{option['text']} {option['value']}"):
                    data[field["name"]] = option["value"]
                    return True
        elif field["type"] == "radio" and _FIFTEEN.search(f"{field['value']} {field['label']}"):
            data[field["name"]] = field["value"]
            return True
    return False


def _names_anlage(text: str, anlage_id: str) -> bool:
    """Return True when ``text`` is or contains ``anlage_id`` as a whole word."""
    return text == anlage_id or re.search(
        r"(?<![\w])" + re.escape(anlage_id) + r"(?![\w])", text or ""
    ) is not None


def _anlage_field(form: dict[str, Any]) -> dict[str, Any] | None:
    """Return the field of a form that chooses the Anlage, if it has one."""
    return next((field for field in form["fields"]
                 if field["name"] and field["type"] not in ("checkbox", "password", "file")
                 and "anlage" in _normalise(f"{field['name']} {field['id']} {field['label']}")),
                None)


def _fill_anlage(form: dict[str, Any], data: dict[str, str], anlage_id: str) -> bool:
    """Choose the Anlage in a form; False when the form has no field for it."""
    field = _anlage_field(form)
    if field is None:
        return False
    if field["tag"] == "select" or field["type"] == "radio":
        choices = field.get("options") or [
            {"value": other["value"], "text": other["label"]} for other in form["fields"]
            if other["name"] == field["name"] and other["type"] == "radio"
        ]
        choice = next((option for option in choices
                       if option["value"] == anlage_id
                       or _names_anlage(option["text"], anlage_id)), None)
        if choice is None:
            raise SmartmeterQueryError(
                f"The E-Werk Gösting portal does not offer Anlage {anlage_id} for selection."
            )
        data[field["name"]] = choice["value"]
    else:
        data[field["name"]] = anlage_id
    return True


def _shown_anlage(html: Any, known: list[str]) -> str | None:
    """Return the Anlage that a page shows as the current one, None if it does not tell.

    The selected option of a dropdown that lists an Anlage decides; without one
    the first "Anlage: <ID>" outside of dropdowns does (the label on the top
    left). Only the account's Anlagen (``known``) count. A dropdown without a
    selected option does not tell: a script may choose one.
    """
    page = _parse_page(html)
    for select in page.selects:
        for option in select["options"]:
            if option.get("selected"):
                for anlage in known:
                    if option["value"].strip() == anlage or _names_anlage(option["text"], anlage):
                        return anlage
    for match in _LABEL_AHEAD.finditer(page.plain_text):
        token = unescape(match.group(1)).strip().rstrip("./-")
        if token in known:
            return token
    return None


def _export_anlagen(body: bytes, disposition: str, known: list[str]) -> list[str]:
    """Return the account's Anlagen that an export names.

    An Anlage counts as named when its ID stands in the file name or in one of
    the first lines that mention an Anlage (a preamble "Anlage;4711001", a JSON
    key "anlage").
    """
    texts = [line for line in _decode(bytes(body[:4096])).splitlines()[:30]
             if "anlage" in line.lower()] + [disposition or ""]
    return [anlage for anlage in known if any(
        re.search(r"(?<![A-Za-z0-9])" + re.escape(anlage) + r"(?![A-Za-z0-9])", text)
        for text in texts)]


# A line of a page that reports a problem ("Anmeldung fehlgeschlagen").
_ALERT = re.compile(r"fehl|ung(?:ü|ue)ltig|falsch|gesperrt|abgelaufen|error|invalid|"
                    r"incorrect|wrong|locked", re.IGNORECASE)


def _describe(page: _PageParser, url: str) -> list[str]:
    """Describe a page for the debug log, in lines of at most 200 characters.

    What a check against the live portal needs, and no values: the URL without
    query, the title, a line that reports a problem, every form (method, action,
    fields as name:type - a dropdown with its first option texts - and buttons)
    and the links and buttons with their text and target.
    """
    lines = [f"page {_safe_url(url)}, title {page.title[:80]!r}"]
    alert = next((" ".join(line.split()) for line in page.text.splitlines()
                  if _ALERT.search(line)), "")
    if alert:
        lines.append(f"it says {alert[:150]!r}")
    for number, form in enumerate(page.forms[:8], 1):
        fields = [f"{field['name'] or '#' + field['id']}:" + (
            f"select({len(field.get('options') or [])}: " + " | ".join(
                option["text"][:20] for option in (field.get("options") or [])[:3]) + ")"
            if field["tag"] == "select" else field["type"]) for field in form["fields"]]
        buttons = [(button["text"] or button["attrs"].get("value", ""))[:20]
                   for button in form["buttons"]]
        lines.append(f"form {number}: {form['method'].upper()} "
                     f"{_safe_url(form['action']) or '(its page)'}, fields "
                     f"{', '.join(fields)}, buttons {buttons}")
    links = [f"{element['text'][:30]!r}->{_safe_url(_href(element)) or '-'}"
             for element in page.links if element["tag"] in ("a", "button")][:40]
    while links:
        chunk: list[str] = []
        while links and len(", ".join(chunk + links[:1])) <= 180:
            chunk.append(links.pop(0))
        lines.append("links " + ", ".join(chunk or [links.pop(0)]))
    return [line[:200] for line in lines]


def _page_error(url: str, html: Any, message: str, error: type = SmartmeterQueryError
                ) -> Exception:
    """Return the error about a portal page, describing the page in the debug log
    first (see _describe) - the log users are asked to send."""
    if LOGGER.isEnabledFor(logging.DEBUG):
        for line in _describe(html if isinstance(html, _PageParser) else _parse_page(html),
                              url):
            LOGGER.debug("E-Werk Gösting: %s", line)
    return error(message)


def _is_page(response: requests.Response) -> bool:
    """Return True for an HTML page, False for a file (an export)."""
    if "attachment" in response.headers.get("Content-Disposition", "").lower():
        return False
    # A UTF-8 byte order mark may stand before the markup.
    head = response.content[:256].lstrip(b"\xef\xbb\xbf \t\r\n\f").lower()
    if head.startswith((b"<!doctype html", b"<html")):
        return True
    return "html" in response.headers.get("Content-Type", "").lower() and head.startswith(b"<")


def _page_text(response: requests.Response) -> str:
    """Return the HTML of a page.

    requests reads a text/html answer that names no charset as ISO-8859-1 (the
    old HTTP default), which turns the "ß" of an address - and with it a device
    name - into "ÃŸ". Such a page is decoded like an export instead: UTF-8, else
    Windows-1252 (see _decode).
    """
    if "charset=" in response.headers.get("Content-Type", "").lower():
        return response.text
    return _decode(response.content)


class EwerkGoestingClient(SmartmeterClient):
    """Client for the E-Werk Gösting customer portal (mein-portal.at)."""

    def __init__(self, username, password):
        super().__init__((username or "").strip(), password or "")
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": (
                    "text/html,application/xhtml+xml,application/json;q=0.9,"
                    "text/csv;q=0.9,*/*;q=0.8"
                ),
                "Accept-Language": "de-AT,de;q=0.9,en;q=0.5",
                "User-Agent": USER_AGENT,
            }
        )
        self._login_time: datetime | None = None
        # The page the login led to (URL, HTML) and the name of its e-mail field.
        self._landing: tuple[str, str] = (LOGIN_URL, "")
        self._user_field = ""
        self._anlagen: list[dict[str, str]] = []
        # (Anlage, first day, last day) -> (day it was read, records of the window).
        self._windows: dict[tuple[str, date, date], tuple[date, list[dict[str, Any]]]] = {}

    # -------------------------------------------------------------- session

    def is_logged_in(self) -> bool:
        """Return True while the portal session is expected to be usable."""
        return self._login_time is not None and not self.is_login_expired()

    def is_login_expired(self) -> bool:
        """Return True when the portal session has to be re-established."""
        if self._login_time is None:
            return True
        return datetime.now() - self._login_time >= SESSION_MAX_AGE

    def _reset(self) -> None:
        """Drop the session so that the next call starts from the login page."""
        self.session.cookies.clear()
        self._login_time = None
        self._landing = (LOGIN_URL, "")
        self._anlagen = []

    def _ensure_session(self) -> None:
        """Make sure a usable portal session exists."""
        if not self.is_logged_in():
            self.login()

    def login(self):
        """Log in to the portal and keep the page it leads to."""
        if not self.username or not self.password:
            raise SmartmeterLoginError(
                "E-Werk Gösting needs the e-mail address and the password of the "
                "customer portal."
            )
        self._reset()
        self._login_request()
        self._login_time = datetime.now()
        return self

    # ------------------------------------------------------------- Anlagen

    def _known_anlagen(self) -> list[dict[str, str]]:
        """Return the Anlagen of the account, discovered once per login."""
        self._ensure_session()
        if not self._anlagen:
            self._anlagen = self._discover_anlagen(self._landing[1])
        return self._anlagen

    def zaehlpunkte(self) -> list[dict[str, Any]]:
        """Return every Anlage of the account as a metering point."""
        return [{"zaehlpunkte": [_anlage_info(anlage) for anlage in self._known_anlagen()]}]

    def consumptions(self) -> list[dict[str, Any]]:
        """The portal has no ready made statistics."""
        return []

    # ------------------------------------------------------------ readings

    def historical_data(
        self, zaehlpunktnummer: str, date_from: date | None = None,
        date_until: date | None = None
    ) -> list[dict[str, Any]]:
        """Return the 15-minute consumption and production of one Anlage.

        The default period is yesterday up to today (Europe/Vienna). It is read
        one calendar month at a time - the portal's 15-minute export works within
        one month only; a month window that lies entirely in the past is read once
        a day (on every call until its last quarter hour has been published), the
        window with today in it on every call.
        """
        anlage_id = str(zaehlpunktnummer or "").strip()
        if not anlage_id:
            raise SmartmeterQueryError("No Anlage given.")
        known = [anlage["id"] for anlage in self._known_anlagen()]
        if anlage_id not in known:
            raise SmartmeterQueryError(
                f"Anlage {anlage_id} is not listed in this E-Werk Gösting account "
                f"(found: {', '.join(known)})."
            )

        start, end = _period(date_from, date_until)
        records: list[dict[str, Any]] = []
        seen: set[tuple[str, datetime]] = set()
        for first, last in month_windows(start, end):
            for record in _within(self._window_records(anlage_id, first, last), start, end):
                key = (record["register"], record["timestamp"])
                if key not in seen:
                    seen.add(key)
                    records.append(record)

        minutes = _interval_minutes(records)
        if minutes is not None and minutes != INTERVAL_MINUTES:
            raise SmartmeterQueryError(
                f"The E-Werk Gösting export of Anlage {anlage_id} holds a value every "
                f"{minutes} minutes instead of every {INTERVAL_MINUTES}: the portal did "
                "not apply the \"15 min\" resolution. Please open an issue."
            )
        return readings_from_records(records)

    def _window_records(self, anlage_id: str, start: date, end: date
                        ) -> list[dict[str, Any]]:
        """Return the records of one month window, from the cache where allowed."""
        today = _today()
        key = (anlage_id, start, end)
        cached = self._windows.get(key)
        if cached is not None and cached[0] == today and end < today:
            return cached[1]

        try:
            content_type, body = self._download_export(anlage_id, start, end)
        except _SessionExpired:
            LOGGER.debug("E-Werk Gösting: the portal session ended, logging in again")
            self.login()
            content_type, body = self._download_export(anlage_id, start, end)
        records = _check_frame(parse_export(body, content_type), start, end)

        # Only what was read today is kept, so the cache never outgrows a day. A
        # window whose values do not reach its end yet (an error page, a portal
        # that publishes late) is not kept: it is asked for again on the next poll.
        self._windows = {window: value for window, value in self._windows.items()
                         if value[0] == today}
        if _reaches(records, end):
            self._windows[key] = (today, records)
        return records

    # ------------------------------------------------------------------
    # Portal protocol (not verified against the live portal)
    #
    # Everything that talks to the portal lives in this section. It does what a
    # customer does - log in, pick the Anlage, "Home", "Zum Verbrauch /
    # Erzeugung", a time frame within one month, "15 min", export - and never
    # guesses an endpoint: every URL comes from a form or a link of a page the
    # portal served. When the live portal differs, this and the page helpers of
    # the "portal pages" section are what has to change. Nothing here logs the
    # password, cookies or whole pages.
    # ------------------------------------------------------------------

    def _request(self, method: str, url: str, login: bool = False, **kwargs: Any
                 ) -> requests.Response:
        """Perform a request. Errors never show a query string or form data."""
        where = _safe_url(url)
        try:
            response = self.session.request(
                method, url, timeout=REQUEST_TIMEOUT, allow_redirects=True, **kwargs
            )
        except requests.exceptions.RequestException as err:
            error = SmartmeterConnectionError(
                f"Request to {where} failed ({type(err).__name__})."
            )
            # A submitted form may hold the password: its details stay out of logs.
            raise error from (None if "data" in kwargs or "params" in kwargs else err)
        if login and response.status_code == 401:
            raise SmartmeterLoginError(
                "The E-Werk Gösting portal rejected the e-mail address or password."
            )
        if login and response.status_code in (400, 403, 422):
            return response  # a rejected login form can come back like this
        if response.status_code >= 400:
            raise SmartmeterConnectionError(f"{where} returned HTTP {response.status_code}.")
        return response

    def _submit(self, page_url: str, form: dict[str, Any], data: dict[str, str],
                login: bool = False, button: dict[str, Any] | None = None
                ) -> requests.Response:
        """Submit a form like a browser: to the pressed button's formaction and with
        its formmethod when it has them (a GET form replaces the action's query).
        A login keeps the form's method: no button moves a password into a URL."""
        attrs = (button or {}).get("attrs") or {}
        action = _join(page_url, attrs.get("formaction", "").strip() or form["action"]
                       or page_url)
        method = form["method"] if login else \
            (attrs.get("formmethod", "").strip().lower() or form["method"])
        if method == "post":
            return self._request("POST", action, login=login, data=data)
        return self._request("GET", action.split("?")[0], login=login, params=data)

    def _check_session(self, html: str) -> None:
        """Raise _SessionExpired when a page is the login page again."""
        if _login_form(_parse_page(html), self._user_field) is not None:
            self._login_time = None
            raise _SessionExpired("The E-Werk Gösting portal session has ended.")

    def _get_page(self, url: str) -> tuple[str, str]:
        """GET a portal page and return its final URL and HTML."""
        response = self._request("GET", url)
        html = _page_text(response)
        self._check_session(html)
        return response.url, html

    def _login_request(self) -> None:
        """Submit the login form: hidden fields as served, e-mail and password.

        Success is a page without the login form that shows a logout link or an
        Anlage. Without any login form the page is a JavaScript application whose
        API is unknown; that is reported instead of guessing endpoints.
        """
        response = self._request("GET", LOGIN_URL)
        page = _parse_page(_page_text(response))
        form = _login_form(page)
        reason = "it has no login form" if form is None else _scripted(form)
        if reason:
            # No login form, or one that a script submits: the page is an
            # application with an API of its own.
            LOGGER.debug(
                "E-Werk Gösting: the login page (%r) is a JavaScript application: %s; "
                "scripts and styles: %s", page.title, reason, page.assets[:20]
            )
            raise SmartmeterConnectionError(
                "The E-Werk Gösting portal login page is a JavaScript application "
                "whose API is not known to this integration yet. Please open an issue "
                "and attach a debug log."
            )
        user = _user_input(form)
        password = next(field for field in form["fields"] if field["type"] == "password")
        if user is None or not password["name"]:
            raise _page_error(response.url, page, "The login form of the E-Werk Gösting "
                              "portal has no recognisable e-mail and password fields.",
                              SmartmeterConnectionError)
        if not form["attrs"].get("method", "").strip():
            # A login form that names no method is POSTed (a browser would send
            # it as GET, with the password in the URL, where logs keep it).
            form = {**form, "method": "post"}
        data = _form_data(form)
        data[user["name"]] = self.username
        data[password["name"]] = self.password
        button = _press(form, data, ("login", "anmelden", "einloggen", "signin"))
        answer = self._submit(response.url, form, data, login=True, button=button)

        text = _page_text(answer)
        landing = _parse_page(text)
        if _login_form(landing, user["name"]) is not None:
            raise _page_error(answer.url, landing, "The E-Werk Gösting portal rejected "
                              "the e-mail address or password.", SmartmeterLoginError)
        if answer.status_code >= 400:
            raise _page_error(answer.url, landing, f"{_safe_url(answer.url)} returned "
                              f"HTTP {answer.status_code}.", SmartmeterConnectionError)
        if not any(marker in text.lower() for marker in _LOGIN_MARKERS):
            raise _page_error(answer.url, landing, "The E-Werk Gösting login led to a page "
                              f"that is not recognisable (title {landing.title!r}).",
                              SmartmeterConnectionError)
        self._user_field = user["name"]
        self._landing = (answer.url, text)
        LOGGER.debug("E-Werk Gösting: logged in, landed on %s", _safe_url(answer.url))

    def _discover_anlagen(self, html: str) -> list[dict[str, str]]:
        """Return the Anlagen listed after the login (see anlagen_from_html).

        The Anlage dropdown sits on the top left of every page; when the landing
        page lacks it, the sidebar entry "Home" is tried once - and becomes the
        page the Anlage is switched on (see _select_anlage).
        """
        anlagen = anlagen_from_html(html)
        url = self._landing[0]
        home = "" if anlagen else _home_link(_parse_page(html))
        if home:
            url, html = self._get_page(_join(url, home))
            anlagen = anlagen_from_html(html)
            if anlagen:
                self._landing = (url, html)
        if not anlagen:
            raise _page_error(url, html, "No Anlagen found on the E-Werk Gösting portal "
                              "after the login (the portal shows them next to the label "
                              "\"Anlage:\"). Please open an issue with a debug log.")
        LOGGER.debug("E-Werk Gösting: Anlagen found: %s",
                     ", ".join(anlage["id"] for anlage in anlagen))
        return anlagen

    def _select_anlage(self, anlage_id: str, url: str, html: str
                       ) -> tuple[str, str, str | None]:
        """Switch the portal to one Anlage where a page offers a way to.

        A form with an Anlage select (the dropdown on the top left) is submitted,
        else a link naming the Anlage in an ``anlage…`` parameter is followed. The
        form is submitted even when the page shows the Anlage as selected: the
        page may be older than the portal's current choice. Returns the page to go
        on from and the Anlage that page shows as the current one (see
        _shown_anlage): a switch that the portal answers without switching is
        seen there, not assumed to have worked.
        """
        known = [anlage["id"] for anlage in self._anlagen]
        page = _parse_page(html)
        for form in page.forms:
            field = _anlage_field(form)
            if field is None or field["tag"] != "select" or _scripted(form):
                continue
            data = _form_data(form)
            if not _fill_anlage(form, data, anlage_id):
                continue
            if len(field.get("options") or []) < 2:
                return url, html, anlage_id
            response = self._submit(url, form, data)
            html = _page_text(response)
            self._check_session(html)
            return response.url, html, _shown_anlage(html, known)
        for element in page.links:
            target = _href(element)
            if any("anlage" in _normalise(name) and value == anlage_id
                   for name, value in _query(target)):
                url, html = self._get_page(_join(url, target))
                break
        return url, html, _shown_anlage(html, known)

    def _download_export(self, anlage_id: str, start: date, end: date
                         ) -> tuple[str | None, bytes]:
        """Export the 15-minute values of one Anlage for one month window.

        Home -> "Zum Verbrauch / Erzeugung" -> time frame (dd.mm.yyyy) and
        "15 min" -> export. An HTML answer is searched for an export link, and its
        own form is submitted once more with the export button. A page that is
        still no file is returned as it is: parse_export warns and finds no values.
        """
        known = [anlage["id"] for anlage in self._known_anlagen()]
        # The last page that shows the current Anlage tells whether the switch
        # took effect; a time frame form with an Anlage field chooses it itself.
        url, html, shown = self._select_anlage(anlage_id, *self._landing)
        target = _chart_link(_parse_page(html))
        home = "" if target else _home_link(_parse_page(html))
        if home:
            url, html = self._get_page(_join(url, home))
            shown = _shown_anlage(html, known) or shown
            target = _chart_link(_parse_page(html))
        if not target:
            raise _page_error(url, html, "The E-Werk Gösting portal page has no button "
                              "\"Zum Verbrauch / Erzeugung\". Please open an issue with a "
                              "debug log.")
        url, html = self._get_page(_join(url, target))

        for attempt in range(2):
            shown = _shown_anlage(html, known) or shown
            form = _period_form(_parse_page(html))
            if form is None:
                if attempt:
                    break
                raise _page_error(url, html, "The E-Werk Gösting portal page \"Verbrauch / "
                                  "Erzeugung\" has no form for the time frame. Please open "
                                  "an issue with a debug log.")
            data = _form_data(form)
            if not _fill_period(form, data, start, end):
                raise _page_error(url, html, "The time frame form of the E-Werk Gösting "
                                  "portal has no recognisable start and end date. Please "
                                  "open an issue with a debug log.")
            if not _fill_resolution(form, data):
                raise _page_error(url, html, "The time frame form of the E-Werk Gösting "
                                  "portal offers no \"15 min\" resolution. Please open an "
                                  "issue with a debug log.")
            if not _fill_anlage(form, data, anlage_id) and shown != anlage_id \
                    and len(known) > 1:
                raise _page_error(url, html, f"Anlage {anlage_id} cannot be selected on the "
                                  "E-Werk Gösting portal" + (f" (it still shows Anlage "
                                  f"{shown})" if shown else "") + "; with several Anlagen "
                                  "its values could not be told apart. Please open an issue "
                                  "with a debug log.")
            button = _press(form, data, _EXPORT_WORDS)
            response = self._submit(url, form, data, button=button)
            if _is_page(response):
                url, html = response.url, _page_text(response)
                self._check_session(html)
                link = _export_link(_parse_page(html))
                if link:
                    response = self._request("GET", _join(url, link))
            if not _is_page(response):
                named = _export_anlagen(response.content,
                                        response.headers.get("Content-Disposition", ""), known)
                if named and anlage_id not in named:
                    raise _page_error(url, html, f"The E-Werk Gösting portal exported Anlage "
                                      f"{named[0]} instead of Anlage {anlage_id}. Please "
                                      "open an issue with a debug log.")
                LOGGER.debug("E-Werk Gösting: export of %s for %s to %s: %s, %s bytes",
                             anlage_id, start, end, response.headers.get("Content-Type"),
                             len(response.content))
                return response.headers.get("Content-Type"), response.content
        # Only a page came back (an error page, or the chart without a file). It
        # is handed on like any answer: parse_export logs one warning with a
        # snippet and finds no values, and the other month windows keep theirs.
        LOGGER.debug("E-Werk Gösting: the export of %s for %s to %s answered with the "
                     "page %r", anlage_id, start, end, _parse_page(_page_text(response)).title)
        return response.headers.get("Content-Type"), response.content
