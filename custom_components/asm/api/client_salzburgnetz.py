"""Salzburg Netz API client.

Facts, taken from the operator's own description of the interface ("Beschreibung
für die Verwendung der API (Programmierschnittstelle) des Serviceportals der
Salzburg Netz GmbH", six pages, published next to the service portal) and from
live probes of the API:

* Base is ``https://api.salzburgnetz.at/api/v1/<category>`` and **every** call is
  a POST - the document says so explicitly.
* Authentication is a personal API key in ``Authorization: Bearer …``. It is
  created in the service portal, can be limited to a validity period of up to two
  years, and opens exactly the customer numbers the account has rights for.
* The body carries ``GPNR`` (the 8-digit customer number, starting with "1"),
  ``ZP`` (the 33-character metering point starting with "AT", or a 10-digit
  facility number starting with "003"), the optional dates ``AB`` and ``BIS`` and
  the optional ``FORMAT`` (``json``, ``csv`` or ``xml``; json is the default).
* Categories: ``profile``, ``profile_eg``, ``anlage``, ``equipment``, ``partner``,
  ``readings``, ``vkonto``, ``consumption``. A load profile can only be read per
  metering point, and at most three years back; the other categories can also be
  read per customer number.
* Load profile data is updated **once a day**. The document suggests reading it
  between 10:00 and 12:00 and asks explicitly not to query the same period more
  than once a day - which is why this adapter caches a period for the rest of the
  day, and why the token is checked once a day instead of on every poll.
* Errors are RFC 7807 style: ``application/problem+json`` with ``error``,
  ``status`` and ``detail``. Verified live: a rejected key answers
  ``401 {"error": "Unauthorized", "detail": "API-Token ist abgelaufen oder ungültig."}``.

**The shape of a successful response is not documented.** The document shows its
example output as a CSV table - ``Datum;Uhrzeit;UTC;Zählpunktbezeichnung;OBIS;OBIS
Kurzbeschreibung;Wert;Einheit;Qualität Beschreibung``, 15-minute values in kWh
with a decimal comma - and the JSON variant, which is the default of the API, is
shown nowhere public: the portal's FAQ is not public, and ``/docs`` and
``/openapi.json`` do not exist. Everything that reads a response is therefore
written to be tolerant instead of clever:

* :func:`_records` finds the list of records inside whatever the API wraps it in:
  a bare list, a ``data``/``values``/``items`` envelope, a columns-and-rows table,
  or even the documented CSV lines as an array of strings.
* :func:`_lookup` matches field names case, space and umlaut insensitively against
  the names of the documented CSV columns and their English equivalents.
* :func:`_record_timestamp` accepts a full ISO timestamp as well as the documented
  ``Datum`` + ``Uhrzeit`` + ``UTC`` triplet.
* :func:`_harvest_metering_points` digs the 33-character metering points out of any
  structure, so the discovery works even though ``/anlage`` is unknown.

A shape that none of these recognise yields an empty result plus a warning rather
than a wrong number, and the metering point numbers can always be entered by hand.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import Any

import requests

from .base import SmartmeterClient
from .errors import (
    SmartmeterConnectionError,
    SmartmeterError,
    SmartmeterLoginError,
    SmartmeterQueryError,
)

LOGGER = logging.getLogger(__name__)

BASE_URL = "https://api.salzburgnetz.at/api/v1"
PROFILE_URL = f"{BASE_URL}/profile"
ANLAGE_URL = f"{BASE_URL}/anlage"
EQUIPMENT_URL = f"{BASE_URL}/equipment"

REQUEST_TIMEOUT = 30

# The API can answer in json, csv or xml; json is its own default and the only one
# this adapter reads.
DATA_FORMAT = "json"

# The documented example is a 15-minute load profile in kWh.
INTERVAL_MINUTES = 15

# How the metering point should be described in Home Assistant.
DEVICE_MODEL = "Salzburg Netz load profile (15 min)"
GRID_OPERATOR = "Salzburg Netz GmbH"

# 33 characters: "AT" plus 31 digits. The 10-digit variant is a facility number.
_METERING_POINT = re.compile(r"^AT\d{31}$")
_FACILITY = re.compile(r"^003\d{7}$")

# Field names, in the spelling of the documented CSV columns and of the obvious
# English equivalents. Matching folds case, spaces, punctuation and umlauts, so
# "Qualität Beschreibung" and "qualitaet_beschreibung" are the same name.
_OBIS_FIELDS = ("obis", "obisCode", "obisNummer", "obisKennzahl", "kennzahl", "code")
_DESCRIPTION_FIELDS = ("obisKurzbeschreibung", "obisBeschreibung", "kurzbeschreibung",
                       "beschreibung", "bezeichnung", "description", "name")
_UNIT_FIELDS = ("einheit", "unit", "mengeneinheit", "uom")
_VALUE_FIELDS = ("wert", "value", "messwert", "menge", "energie", "verbrauch",
                 "quantity", "amount", "kwh")
_QUALITY_FIELDS = ("qualitaetBeschreibung", "qualitaet", "quality", "status",
                   "qualitaetsbeschreibung", "guete")
_STAMP_FIELDS = ("zeitpunkt", "timestamp", "zeitstempel", "datetime",
                 "zeitpunktErfassung", "valuedate", "wertedatum")
_DATE_FIELDS = ("datum", "date", "tag", "von", "ab", "start", "beginn")
_TIME_FIELDS = ("uhrzeit", "zeit", "time", "vonzeit", "beginnzeit")
_OFFSET_FIELDS = ("utc", "utcOffset", "zeitzone", "zone", "offset", "utcZone")

_METERING_POINT_FIELDS = ("zaehlpunktnummer", "zaehlpunkt", "zaehlpunktbezeichnung",
                          "zp", "meteringPoint", "meteringPointId", "messpunkt")
_FACILITY_FIELDS = ("anlagennummer", "anlage", "anlagennr", "facility",
                    "facilityNumber")
_NAME_FIELDS = ("zaehlpunktName", "anlagename", "anlagenbezeichnung", "bezeichnung",
                "name")
_TYPE_FIELDS = ("zaehlpunktAnlagentyp", "anlagentyp", "anlagenart", "verbrauchsart",
                "typ", "art")
_STREET_FIELDS = ("strasse", "strassenname", "street", "adresse", "address")
_ZIP_FIELDS = ("postleitzahl", "plz", "zip", "postalCode")
_CITY_FIELDS = ("ort", "stadt", "city", "gemeinde", "ortschaft")
_ADDRESS_CONTAINERS = ("verbrauchsstelle", "adresse", "address", "anschrift",
                       "standort", "anlagenadresse", "liegenschaft")

_METER_NUMBER_FIELDS = ("zaehlernummer", "zaehlernr", "geraetenummer", "geraetenr",
                        "meternumber", "serialnumber", "serial", "equipmentNumber",
                        "deviceId", "nummer")
_METER_TYPE_FIELDS = ("zaehlertyp", "geraetetyp", "meteringType", "typtext", "typ",
                      "bauart", "modell", "model")
_MANUFACTURER_FIELDS = ("hersteller", "manufacturer", "marke", "brand")
_EXTRA_EQUIPMENT_FIELDS = (("eichjahr", "eichjahr"), ("baujahr", "baujahr"),
                           ("zulassungsnummer", "zulassungsnummer"),
                           ("zaehlpunktAnlagentyp", "zaehlpunktAnlagentyp"))

# Envelope keys a wrapper is likely to use, and the keys of a table style answer.
_ENVELOPE_KEYS = ("data", "values", "werte", "value", "result", "results", "items",
                  "records", "entries", "rows", "messwerte", "lastprofile",
                  "profile", "liste", "list")
_COLUMN_KEYS = ("columns", "column", "header", "headers", "spalten", "felder")
_ROW_KEYS = ("rows", "zeilen", "data", "records", "entries", "werte", "values")

# Energy units, as reported by the documented example ("kWh").
_UNIT_FACTORS = {"wh": 1.0, "kwh": 1000.0, "mwh": 1_000_000.0, "gwh": 1_000_000_000.0}

# The suffix an Austrian load profile uses to name the direction: "P" for Bezug,
# "G" for Einspeisung. It does not change which register is meant.
_OBIS_SUFFIX = re.compile(r"[\s_-]*[A-Z]\.\d+$")


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


# Folded once, so that the address lookup does not refold them per record.
_ADDRESS_CONTAINER_KEYS = tuple(_normalise(name) for name in _ADDRESS_CONTAINERS)


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


def _scalar(value: Any) -> str:
    """Return a text value for a scalar, and an empty string for anything else.

    Responses are read tolerantly, so a lookup can land on the wrong key; a nested
    object there must produce no value rather than a stringified dictionary.
    """
    if value is None or isinstance(value, (dict, list, tuple, set, bool)):
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).strip()


def _number(value: Any) -> float | None:
    """Return a value as a float, accepting the decimal comma of the API."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None

    text = value.replace("\u00a0", "").strip()
    if not text:
        return None
    negative = text.startswith("-")
    text = text.lstrip("+-").replace(" ", "")
    if "," in text and "." in text:
        # 1.234,56 and 1,234.56 both mean the same number: the last separator wins.
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    else:
        text = text.replace(",", ".")
    try:
        return -float(text) if negative else float(text)
    except ValueError:
        return None


def _to_wh(value: float, unit: Any) -> float:
    """Convert an energy value to Wh.

    The documented example reports kWh, so a missing or unknown unit is read as
    kWh rather than as a wrong magnitude.
    """
    return value * _UNIT_FACTORS.get(_normalise(unit), 1000.0)


def _quality(value: Any) -> str:
    """Map the operator's quality text onto the integration's status vocabulary."""
    text = _normalise(value)
    if not text:
        return "VALID"
    if "ungueltig" in text or "invalid" in text:
        return "INVALID"
    if "gueltig" in text or "valid" in text:
        return "VALID"
    if any(token in text for token in ("ersatz", "geschaetzt", "estimated",
                                       "interpoliert", "prognose")):
        return "ESTIMATED"
    return "UNKNOWN"


def _normalise_obis(value: Any) -> str:
    """Return the register of an OBIS label.

    The API labels registers the way an Austrian load profile does, e.g.
    ``1-1:1.9.0 P.01`` for consumption and ``1-1:2.9.0 G.01`` for feed-in. The
    suffix does not change which register is meant, so it is dropped; a record
    without a register at all is not usable and yields an empty string.
    """
    text = str(value or "").strip().upper()
    if not text:
        return ""
    if re.match(r"^[0-9A-F]{1,2}-[0-9A-F]{1,2}:", text):
        return _OBIS_SUFFIX.sub("", text)
    if re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}$", text):
        return text
    return ""


def _lookup(record: dict[str, Any], names: tuple[str, ...], default: Any = None) -> Any:
    """Return the first field of a record whose name matches one of ``names``."""
    folded = {_normalise(key): value for key, value in record.items()}
    for name in names:
        key = _normalise(name)
        if key in folded:
            value = folded[key]
            if value is not None and value != "":
                return value
    return default


def _parse_timestamp(value: Any) -> datetime | None:
    """Parse a timestamp, ISO or written the way the API documents it."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        # Epoch seconds or milliseconds.
        seconds = float(value)
        if abs(seconds) > 10_000_000_000:
            seconds /= 1000
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str):
        return None

    text = value.strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        pass
    for pattern in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y",
                    "%d.%m.%y %H:%M:%S", "%d.%m.%y %H:%M", "%d.%m.%y"):
        try:
            return datetime.strptime(text, pattern)
        except ValueError:
            continue
    return None


def _parse_date(value: Any) -> datetime | None:
    """Parse a date, ISO or in the German spelling of the API."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    if not isinstance(value, str):
        return None

    text = value.strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        pass
    for pattern in ("%d.%m.%Y", "%d.%m.%y"):
        try:
            return datetime.strptime(text, pattern)
        except ValueError:
            continue
    return None


def _parse_time(value: Any) -> tuple[int, int, int] | None:
    """Parse a clock time, e.g. ``00:15:00``."""
    if isinstance(value, str):
        text = value.strip()
        for pattern in ("%H:%M:%S", "%H:%M"):
            try:
                parsed = datetime.strptime(text, pattern)
            except ValueError:
                continue
            return parsed.hour, parsed.minute, parsed.second
    return None


def _offset_from(value: Any) -> timedelta | None:
    """Return the UTC offset of the documented ``UTC`` column.

    The example carries ``+1`` for winter time; ``+01:00``, ``1`` and ``-2`` are
    read the same way.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return timedelta(hours=float(value))
    if not isinstance(value, str):
        return None

    match = re.match(r"^(?:UTC|GMT)?\s*([+-]?)(\d{1,2})(?::?(\d{2}))?$",
                     value.strip(), re.IGNORECASE)
    if not match:
        return None
    sign = -1 if match.group(1) == "-" else 1
    return sign * timedelta(hours=int(match.group(2)), minutes=int(match.group(3) or 0))


def _localise(moment: datetime, offset: timedelta | None) -> datetime:
    """Attach a timezone to a local wall clock time and return it in UTC."""
    if moment.tzinfo is not None:
        return moment.astimezone(timezone.utc)
    if offset is not None:
        return moment.replace(tzinfo=timezone(offset)).astimezone(timezone.utc)
    # No offset column: the timestamps are the operator's local time.
    return moment.replace(tzinfo=_vienna()).astimezone(timezone.utc)


def _record_timestamp(record: dict[str, Any]) -> datetime | None:
    """Return the timestamp of one record.

    Accepts a full timestamp field as well as the documented triplet of a local
    date, a local time and the UTC offset that applied on that day.
    """
    offset = _offset_from(_lookup(record, _OFFSET_FIELDS))

    stamp = _lookup(record, _STAMP_FIELDS)
    if stamp is not None:
        parsed = _parse_timestamp(stamp)
        if parsed is not None:
            return _localise(parsed, offset)

    day = _parse_date(_lookup(record, _DATE_FIELDS))
    if day is None:
        return None
    clock = _parse_time(_lookup(record, _TIME_FIELDS))
    if clock is not None:
        day = day.replace(hour=clock[0], minute=clock[1], second=clock[2])
    return _localise(day, offset)


def _csv_records(lines: list[str]) -> list[dict[str, Any]]:
    """Read records out of CSV lines.

    The API documents its output as a semicolon separated table, so a JSON array
    of those very lines is one of the shapes this adapter accepts.
    """
    rows = [line for line in (str(item).strip() for item in lines) if line]
    if len(rows) < 2:
        return []

    header = rows[0]
    delimiter = max((";", ",", "\t", "|"), key=header.count)
    columns = [column.strip() for column in header.split(delimiter)]
    if len(columns) < 2:
        return []

    records: list[dict[str, Any]] = []
    for row in rows[1:]:
        values = [value.strip() for value in row.split(delimiter)]
        if len(values) < len(columns):
            continue
        records.append(dict(zip(columns, values)))
    return records


def _table_records(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Zip a columns-and-rows style answer into records."""
    columns: list[str] | None = None
    for key, value in payload.items():
        if _normalise(key) in _COLUMN_KEYS and isinstance(value, list) \
                and value and all(isinstance(item, str) for item in value):
            columns = [str(item) for item in value]
            break
    if not columns:
        return []

    for key, value in payload.items():
        if _normalise(key) not in _ROW_KEYS or not isinstance(value, list):
            continue
        records = [
            dict(zip(columns, row))
            for row in value
            if isinstance(row, list) and len(row) >= len(columns)
        ]
        if records:
            return records
    return []


def _records_from_list(items: list[Any]) -> list[dict[str, Any]]:
    """Return the records of a list, whatever the items are."""
    dicts = [item for item in items if isinstance(item, dict)]
    if dicts:
        return dicts

    strings = [item for item in items if isinstance(item, str)]
    rows = _csv_records(strings)
    if rows:
        return rows

    for item in items:
        nested = _records(item)
        if nested:
            return nested
    return []


def _records(payload: Any, depth: int = 0) -> list[dict[str, Any]]:
    """Return the records of a response, whatever the API wrapped them in.

    Tried in this order: a bare list of records, a table of columns and rows, an
    envelope (``data``, ``values``, ``items``, …) and finally the first list of
    records anywhere in the payload.
    """
    if depth > 8:
        return []
    if isinstance(payload, list):
        return _records_from_list(payload)
    if not isinstance(payload, dict):
        return []

    table = _table_records(payload)
    if table:
        return table

    for key, value in payload.items():
        if _normalise(key) in _ENVELOPE_KEYS:
            found = _records(value, depth + 1)
            if found:
                return found

    for value in payload.values():
        if isinstance(value, (dict, list)):
            found = _records(value, depth + 1)
            if found:
                return found
    return []


def _is_metering_point(value: str) -> bool:
    """Return True for a 33-character metering point ("AT" plus 31 digits)."""
    return bool(_METERING_POINT.match(value))


def _is_facility(value: str) -> bool:
    """Return True for a 10-digit facility number starting with "003"."""
    return bool(_FACILITY.match(value))


def _normalise_metering_points(value: Any) -> list[str]:
    """Return the metering points a user entered, cleaned and deduplicated."""
    if value is None:
        return []
    if isinstance(value, str):
        candidates = re.split(r"[,;\s]+", value)
    elif isinstance(value, (list, tuple, set)):
        candidates = [str(item) for item in value]
    else:
        return []

    result: list[str] = []
    for candidate in candidates:
        text = candidate.strip().upper()
        if text and text not in result:
            result.append(text)
    return result


def _iso_date(value: Any) -> str | None:
    """Return a date in the ISO form the API expects for ``AB`` and ``BIS``."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        parsed = _parse_date(value)
        if parsed is not None:
            return parsed.date().isoformat()
    return None


def _address_from(record: dict[str, Any]) -> dict[str, str]:
    """Return the address of a metering point, flat or nested."""
    source: dict[str, Any] = record
    for key, value in record.items():
        if isinstance(value, dict) and _normalise(key) in _ADDRESS_CONTAINER_KEYS:
            source = value
            break

    address: dict[str, str] = {}
    street = _scalar(_lookup(source, _STREET_FIELDS))
    if street:
        address["strasse"] = street
    zip_code = _scalar(_lookup(source, _ZIP_FIELDS))
    if zip_code:
        address["postleitzahl"] = zip_code
    city = _scalar(_lookup(source, _CITY_FIELDS))
    if city:
        address["ort"] = city
    return address


def _metering_point_id(record: dict[str, Any]) -> str:
    """Return the metering point or facility number a record describes."""
    for name in _METERING_POINT_FIELDS + _FACILITY_FIELDS:
        value = _lookup(record, (name,))
        if not isinstance(value, str):
            continue
        candidate = value.strip().upper()
        if _is_metering_point(candidate) or _is_facility(candidate):
            return candidate
    return ""


def _harvest_metering_points(payload: Any, depth: int = 0) -> dict[str, dict[str, Any]]:
    """Collect the metering points of an ``/anlage`` answer.

    The response of that category is not documented, so instead of assuming a
    structure this walks whatever came back and picks up every object that names a
    metering point or a facility number. The first, usually most detailed,
    occurrence of an identifier wins.
    """
    found: dict[str, dict[str, Any]] = {}
    if depth > 8 or payload is None:
        return found

    if isinstance(payload, dict):
        identifier = _metering_point_id(payload)
        if identifier:
            found[identifier] = _metering_point_info(payload, identifier)
        values = payload.values()
    elif isinstance(payload, list):
        values = payload
    else:
        return found

    for value in values:
        if not isinstance(value, (dict, list)):
            continue
        for identifier, info in _harvest_metering_points(value, depth + 1).items():
            found.setdefault(identifier, info)
    return found


def _metering_point_info(record: dict[str, Any], identifier: str) -> dict[str, Any]:
    """Return what a record says about one metering point."""
    info: dict[str, Any] = {"zaehlpunktnummer": identifier}

    name = _scalar(_lookup(record, _NAME_FIELDS))
    if name and name.upper() != identifier:
        info["zaehlpunktName"] = name

    kind = _scalar(_lookup(record, _TYPE_FIELDS))
    if kind:
        info["zaehlpunktAnlagentyp"] = kind

    address = _address_from(record)
    if address:
        info["verbrauchsstelle"] = address
    return info


def _equipment_info(payload: Any) -> dict[str, Any]:
    """Return the device details of an ``/equipment`` answer, read tolerantly."""
    records = _records(payload)
    record: dict[str, Any] | None = records[0] if records else None
    if record is None and isinstance(payload, dict):
        # A single device may well come back as a flat object rather than a list.
        record = payload
    if record is None:
        return {}

    info: dict[str, Any] = {}
    number = _scalar(_lookup(record, _METER_NUMBER_FIELDS))
    if number:
        info["geraetNumber"] = number
    kind = _scalar(_lookup(record, _METER_TYPE_FIELDS))
    if kind:
        info["smartMeterType"] = kind
    manufacturer = _scalar(_lookup(record, _MANUFACTURER_FIELDS))
    if manufacturer:
        info["hersteller"] = manufacturer
    for field, key in _EXTRA_EQUIPMENT_FIELDS:
        value = _scalar(_lookup(record, (field,)))
        if value and key not in info:
            info[key] = value
    return info


def _readings_from_profile(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Turn load profile records into the integration's reading contract.

    One reading per register (consumption, feed-in, …), each carrying every value
    of the requested period, oldest first - the sensor platform reports the most
    recent one and charts the rest.
    """
    readings: dict[str, dict[str, Any]] = {}
    day_totals: dict[tuple[str, date], float] = {}

    for record in records:
        obis = _normalise_obis(_lookup(record, _OBIS_FIELDS))
        if not obis:
            continue
        stamp = _record_timestamp(record)
        if stamp is None:
            continue
        value = _number(_lookup(record, _VALUE_FIELDS))
        if value is None:
            continue

        unit = _lookup(record, _UNIT_FIELDS)
        wh = round(_to_wh(value, unit), 3)
        local_day = stamp.astimezone(_vienna()).date()
        day_totals[(obis, local_day)] = day_totals.get((obis, local_day), 0.0) + wh

        reading = readings.get(obis)
        if reading is None:
            reading = {
                "obisCode": obis,
                "name": _scalar(_lookup(record, _DESCRIPTION_FIELDS)),
                "wertetyp": "QUARTER_HOUR",
                "einheit": "Wh",
                "messwerte": [],
                "interval_minutes": INTERVAL_MINUTES,
                "source_format": DATA_FORMAT,
            }
            readings[obis] = reading

        entry: dict[str, Any] = {
            "zeitpunkt": stamp.isoformat(),
            "messwert": wh,
            "status": _quality(_lookup(record, _QUALITY_FIELDS)),
        }
        source_unit = _scalar(unit)
        if source_unit:
            entry["source_unit"] = source_unit
        reading["messwerte"].append((entry, local_day))

    for obis, reading in readings.items():
        values = sorted(reading["messwerte"], key=lambda item: item[0]["zeitpunkt"])
        for entry, local_day in values:
            total = day_totals.get((obis, local_day))
            if total is not None:
                entry["day_total_wh"] = round(total, 3)
        reading["messwerte"] = [entry for entry, _ in values]
        reading["records_read"] = len(values)
    return [readings[obis] for obis in sorted(readings)]


class SalzburgNetzClient(SmartmeterClient):
    """Client for the Salzburg Netz service portal API."""

    def __init__(
        self,
        token: str | None,
        gpnr: str | None,
        metering_points: Any = None,
        password: str | None = None,
    ):
        # The API key is the credential, so it goes into the username slot of the
        # base class and never into the password one.
        super().__init__(token, None)
        self.token = (token or "").strip()
        self.gpnr = (gpnr or "").strip()
        self.metering_points = _normalise_metering_points(metering_points)
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": (
                    "AustriaSmartmeter-HASS/1.2 "
                    "(+https://github.com/acdcnow/AustrianSmartMeter-for-Home-Assistant)"
                ),
            }
        )
        if self.token:
            self.session.headers["Authorization"] = f"Bearer {self.token}"

        # Everything below is deliberately cached by calendar day: the load profile
        # is published once a day and the operator asks for at most one query per
        # period per day.
        self._validated_on: date | None = None
        self._found: dict[str, dict[str, Any]] = {}
        self._device_details: dict[str, dict[str, Any]] = {}
        self._device_details_day: date | None = None
        self._profiles: dict[tuple[str, str, str], tuple[date, list[dict[str, Any]]]] = {}

    # -------------------------------------------------------------- session

    def is_logged_in(self) -> bool:
        """Return True once the key has been accepted on this day."""
        return self._validated_on == _today()

    def is_login_expired(self) -> bool:
        """Return True while the key has not been checked today.

        The data behind this API changes once a day, so the key is checked with the
        same rhythm instead of on every poll - which keeps a 60-minute scan
        interval down to one request per metering point per day.
        """
        return not self.is_logged_in()

    def _ensure_session(self) -> None:
        """Validate the key and refresh the metering points when that is due."""
        if not self.is_logged_in():
            self.login()

    def login(self):
        """Check the key and read the metering points - one request a day.

        Reading the facilities is the cheapest call that proves both the key and
        the customer number, and it is what the metering point discovery needs
        anyway. Without configured metering points a failure here is fatal; with
        them it is only a warning, because the numbers the user entered are used
        instead.
        """
        if not self.gpnr:
            raise SmartmeterQueryError("No customer number (GPNR) configured.")

        payload: Any = None
        try:
            payload = self._post(ANLAGE_URL, self._body())
        except SmartmeterLoginError:
            raise
        except SmartmeterError as err:
            if not self.metering_points:
                raise
            LOGGER.warning(
                "Salzburg Netz: could not list the metering points of %s (%s); "
                "using the configured ones",
                self.gpnr,
                err,
            )

        self._found = _harvest_metering_points(payload)
        self._validated_on = _today()
        if payload is not None:
            LOGGER.debug(
                "Salzburg Netz: %s metering point(s) found for %s",
                len(self._found),
                self.gpnr,
            )
        return self

    # ----------------------------------------------------------------- http

    def _body(self, zaehlpunkt: str | None = None, ab: str | None = None,
              bis: str | None = None) -> dict[str, Any]:
        """Return the request body of a call.

        Only the fields that are used are sent: the API documents a mandatory
        customer number and metering point and optional dates, and an empty string
        would be a value of its own.
        """
        body: dict[str, Any] = {"GPNR": self.gpnr, "FORMAT": DATA_FORMAT}
        if zaehlpunkt:
            body["ZP"] = zaehlpunkt
        if ab:
            body["AB"] = ab
        if bis:
            body["BIS"] = bis
        return body

    def _post(self, url: str, body: dict[str, Any]) -> Any:
        """POST a request body and return the parsed response."""
        try:
            response = self.session.request(
                "POST", url, json=body, timeout=REQUEST_TIMEOUT
            )
        except requests.exceptions.RequestException as err:
            raise SmartmeterConnectionError(f"Request to {url} failed: {err}") from err

        if response.status_code in (200, 201):
            try:
                return response.json()
            except ValueError as err:
                snippet = " ".join(response.text.split())[:200]
                raise SmartmeterConnectionError(
                    f"{url} returned a non-JSON response "
                    f"(HTTP {response.status_code}): {snippet!r}"
                ) from err

        # Errors are RFC 7807 style ("application/problem+json") with `error`,
        # `status` and `detail` - checked against the live API.
        detail = ""
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            detail = str(
                payload.get("detail") or payload.get("message") or payload.get("error") or ""
            )

        text = f"{url} returned HTTP {response.status_code}"
        if detail:
            text = f"{text}: {detail}"

        if response.status_code == 401:
            raise SmartmeterLoginError(
                "The Salzburg Netz API key was rejected "
                f"({detail or 'expired or invalid'}). Create a new key in the "
                "service portal under Mein Benutzerkonto."
            )
        if response.status_code == 403:
            raise SmartmeterQueryError(
                f"{text} (the key has no rights for this customer number)"
            )
        if response.status_code == 429:
            raise SmartmeterConnectionError(
                f"{text} (too many requests, retry later)"
            )
        if response.status_code in (400, 404, 422):
            raise SmartmeterQueryError(text)
        raise SmartmeterConnectionError(text)

    # ------------------------------------------------------- metering points

    def _metering_point_ids(self) -> list[str]:
        """Return the metering points to read: the configured ones, then found."""
        identifiers = list(self.metering_points)
        for identifier in self._found:
            if identifier not in identifiers:
                identifiers.append(identifier)
        return identifiers

    def _device_details_for(self, identifier: str) -> dict[str, Any]:
        """Return the device details of a metering point, cached for the day.

        The details are a bonus: a metering point stays usable when the category
        is not available for it.
        """
        if self._device_details_day != _today():
            self._device_details = {}
            self._device_details_day = _today()
        if identifier not in self._device_details:
            details: dict[str, Any] = {}
            try:
                details = _equipment_info(
                    self._post(EQUIPMENT_URL, self._body(identifier))
                )
            except SmartmeterError as err:
                LOGGER.debug(
                    "Salzburg Netz: no device details for %s: %s", identifier, err
                )
            self._device_details[identifier] = details
        return self._device_details[identifier]

    def zaehlpunkte(self) -> list[dict[str, Any]]:
        """Return the metering points of the customer number."""
        self._ensure_session()
        identifiers = self._metering_point_ids()
        if not identifiers:
            raise SmartmeterQueryError(
                "No metering points could be determined. The API does not "
                "document how a customer number lists its facilities; enter the "
                "33-character metering point numbers when adding the integration."
            )

        metering_points: list[dict[str, Any]] = []
        for identifier in identifiers:
            info: dict[str, Any] = dict(self._found.get(identifier) or {})
            info["zaehlpunktnummer"] = identifier
            if _is_facility(identifier):
                info["anlagenummer"] = identifier
            info.update(self._device_details_for(identifier))
            info["geschaeftspartner"] = GRID_OPERATOR
            info["gpnr"] = self.gpnr
            info["device_model"] = DEVICE_MODEL
            metering_points.append(info)

        return [{"zaehlpunkte": metering_points}]

    # ------------------------------------------------------------- readings

    def _profile_records(self, identifier: str, ab: str, bis: str) -> list[dict[str, Any]]:
        """Return the load profile records of a period, cached for the day."""
        cache_key = (identifier, ab, bis)
        cached = self._profiles.get(cache_key)
        if cached is not None and cached[0] == _today():
            return cached[1]

        records = _records(self._post(PROFILE_URL, self._body(identifier, ab, bis)))
        if not records:
            LOGGER.warning(
                "Salzburg Netz: the load profile of %s for %s to %s held no record "
                "this adapter recognised. The JSON shape of the API is not "
                "documented, so please report the raw response.",
                identifier,
                ab,
                bis,
            )
        self._profiles[cache_key] = (_today(), records)
        return records

    def historical_data(
        self, zaehlpunktnummer: str, date_from: date | None = None,
        date_until: date | None = None
    ) -> list[dict[str, Any]]:
        """Return the 15-minute load profile of one metering point.

        The default period is yesterday up to today: yesterday is complete (the
        operator publishes a day between 10:00 and 12:00), today is still filling
        up, and the most recent value is the one the sensors show.

        The period is read at most once per day, as the operator asks.
        """
        self._ensure_session()
        identifier = str(zaehlpunktnummer or "").strip()
        if not identifier:
            raise SmartmeterQueryError("No metering point given.")

        ab = _iso_date(date_from) or _iso_date(_today() - timedelta(days=1))
        bis = _iso_date(date_until) or _iso_date(_today())
        if ab and bis and ab > bis:
            ab, bis = bis, ab

        records = self._profile_records(identifier, ab or "", bis or "")
        return _readings_from_profile(records)

    def consumptions(self) -> list[dict[str, Any]]:
        """A load profile has no ready made statistics.

        The billed amounts live in the ``consumption`` category, which this
        adapter does not read yet.
        """
        return []
