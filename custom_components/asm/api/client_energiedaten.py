"""energiedaten.at API client.

energiedaten.at is *not* a grid operator. It is an Austrian platform that
collects smart meter data from the grid operators and republishes it through a
REST API (OpenAPI spec version 0.7.0 at the time of writing, API version
``2026-05-08``). Two consequences shape this adapter:

* **Authentication is an API key, not a portal login.** The key is created in
  the energiedaten.at dashboard (*Integrations → API Keys*) and needs at least
  the ``smart-meters:read`` and ``data:read`` scopes. There is no session to
  establish, so :meth:`login` only proves that the key is accepted.
* **Provisioning happens in the dashboard, not in Home Assistant.** A metering
  point has to be added as a smart meter there and the grid operator consent
  has to be granted (``status == "connected"``) before any data arrives. This
  adapter is deliberately read-only; it never creates locations, meters or
  consents.

Contract notes that matter for Home Assistant:

* The API answers in **kWh per interval** (15 minutes for a typical meter),
  never as a cumulative meter register. The interval values are summed up to
  the consumption of one local day and reported as a period value
  (``wertetyp == "DAY"``), so the sensor never claims to be a total_increasing
  meter reading.
* Data is delivered day-after, and the platform's completeness accounting is
  per *Vienna* day. The window is therefore sent as an explicit instant with
  the Vienna UTC offset — a date-only value would be parsed as UTC midnight,
  which is mid-morning in Vienna and would silently drop the first intervals.
* The API vocabulary uses channel suffixes (``1-1:1.9.0 P.01`` is the grid
  residual, ``1-1:1.9.0 G.01`` the meter-wide total). Both are normalised onto
  the integration's OBIS codes (``1-1:1.9.0``) so entity ids stay consistent
  with the other providers; the source code stays visible in the attributes.
"""
from __future__ import annotations

import calendar
import logging
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from urllib.parse import quote

import requests

from .base import SmartmeterClient
from .errors import (
    SmartmeterConnectionError,
    SmartmeterLoginError,
    SmartmeterQueryError,
)

LOGGER = logging.getLogger(__name__)

try:  # zoneinfo is stdlib, the tzdata package it reads is not
    from zoneinfo import ZoneInfo

    TIMEZONE: Any = ZoneInfo("Europe/Vienna")
except Exception:  # noqa: BLE001 - no time zone database available
    # Home Assistant always ships tzdata; this is a safety net for bare test
    # environments, where the EU daylight saving rule is applied manually.
    TIMEZONE = None
    LOGGER.warning(
        "energiedaten.at: no tzdata available, deriving the Austrian UTC "
        "offset from the EU daylight saving rule"
    )

BASE_URL = "https://energiedaten.at/api/v1"

# Pinned contract version. The header is optional (an absent one resolves to the
# current version), but pinning keeps a breaking change from reaching us silently.
API_VERSION = "2026-05-08"

REQUEST_TIMEOUT = 30

# The key is not a session that expires on the server side, but it does have a
# lifetime. Re-validating it once a day is cheap and surfaces a revoked key
# before the user wonders where the data went.
SESSION_MAX_AGE = timedelta(hours=24)

# List endpoints accept at most 100 items per page.
MAX_PER_PAGE = 100
MAX_PAGES = 10

# The server caps a data window at 50 000 records and flags the response.
SERVER_RECORD_CAP = 50_000

# The API reports energy in kWh, the entities are fed Wh (see sensor.py).
KWH_TO_WH = 1000
UNIT_WH = "Wh"

# Marker used by sensor.py to tell period values (which reset every day) apart
# from cumulative meter readings.
VALUE_TYPE_DAY = "DAY"

# Measurement quality as documented by the API ("1 = measured, 2 = estimated,
# 3 = unreliable"). "unknown" only shows up if the enum grows.
QUALITY_LABELS = {1: "measured", 2: "estimated", 3: "unreliable"}
QUALITY_RANK = {"measured": 0, "estimated": 1, "unreliable": 2, "unknown": 3}

# Registers that should end up as one sensor each. The first source code that
# carries data wins; the meter-wide total (G.01) is the fallback for meters that
# do not report a grid residual. All codes are part of the API's default egress
# vocabulary, so no register needs to be asked for explicitly.
_REGISTERS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("1-1:1.9.0", "Daily Consumption", ("1-1:1.9.0 P.01", "1-1:1.9.0 G.01")),
    ("1-1:2.9.0", "Daily Feed-in", ("1-1:2.9.0 P.01", "1-1:2.9.0 G.01")),
    ("7-1:1.9.0", "Daily Gas Consumption", ("7-1:1.9.0 P.01",)),
)

# Codes requested per commodity. Sending the explicit list keeps the response
# small; everything below is part of the modelled vocabulary.
_ELECTRICITY_CODES = (
    "1-1:1.9.0 P.01",
    "1-1:1.9.0 G.01",
    "1-1:2.9.0 P.01",
    "1-1:2.9.0 G.01",
)
_GAS_CODES = ("7-1:1.9.0 P.01",)


def _masked(value: str | None) -> str:
    """Return a loggable hint for a secret, never the secret itself."""
    if not value:
        return "<none>"
    return f"…{value[-4:]}" if len(value) > 4 else "…"


def _decode_json(response: requests.Response, url: str) -> Any:
    """Return the decoded JSON body, or raise a descriptive error."""
    try:
        return response.json()
    except ValueError as err:
        snippet = " ".join(response.text.split())[:200]
        raise SmartmeterConnectionError(
            f"{url} returned a non-JSON response "
            f"(HTTP {response.status_code}): {snippet!r}"
        ) from err


def _error_envelope(payload: Any) -> tuple[str | None, str | None, str | None]:
    """Extract ``(type, code, message)`` from an API error body."""
    if not isinstance(payload, dict):
        return None, None, None
    error = payload.get("error")
    if not isinstance(error, dict):
        return None, None, None
    return error.get("type"), error.get("code"), error.get("message")


def _raise_for_status(
    response: requests.Response, url: str, *, login: bool = False
) -> None:
    """Translate an HTTP error into the integration's own exception types.

    ``login`` marks the key-validation call in :meth:`EnergiedatenAtClient.login`:
    a 403 there means the key may not even list meters, which is a credentials
    problem, whereas a 403 while reading data means the key lacks the data scope.
    """
    if response.status_code < 400:
        return

    payload: Any = None
    try:
        payload = response.json()
    except ValueError:
        payload = None

    error_type, code, message = _error_envelope(payload)
    detail = " ".join(
        part for part in (f"[{error_type}/{code}]" if code else None, message) if part
    )
    text = f"{url} returned HTTP {response.status_code}"
    if detail:
        text = f"{text} {detail}"

    if response.status_code == 401:
        raise SmartmeterLoginError("The API key was rejected. Check the key.")
    if response.status_code == 403:
        if login:
            raise SmartmeterLoginError(
                "The API key was rejected. It needs the 'smart-meters:read' scope."
            )
        raise SmartmeterQueryError(
            f"The API key is not allowed to read this data (missing scope?). {text}"
        )
    if response.status_code == 429:
        # The API sends X-RateLimit-* and Retry-After headers here.
        retry_after = response.headers.get("Retry-After", "unknown")
        raise SmartmeterConnectionError(
            f"{text} Rate limit exceeded, retry after {retry_after}s."
        )
    if response.status_code in (404, 409, 410, 422):
        raise SmartmeterQueryError(text)
    raise SmartmeterConnectionError(text)


def _last_sunday(year: int, month: int) -> date:
    """Return the last Sunday of a month — the EU daylight saving switch day."""
    last = date(year, month, calendar.monthrange(year, month)[1])
    return last - timedelta(days=(last.weekday() + 1) % 7)


def _fallback_offset_hours(naive_local: datetime) -> int:
    """Return 2 for CEST and 1 for CET for a local wall time.

    Only used when no time zone database is available. Summer time spans from
    01:00 UTC on the last Sunday of March to 01:00 UTC on the last Sunday of
    October; the offset that puts the instant inside that window is the correct
    one. That also resolves the ambiguous hour in October to summer time (the
    first occurrence), exactly like ``ZoneInfo`` does.
    """
    for hours in (2, 1):
        candidate = (naive_local - timedelta(hours=hours)).replace(tzinfo=timezone.utc)
        start = datetime.combine(
            _last_sunday(candidate.year, 3), time(1), tzinfo=timezone.utc
        )
        end = datetime.combine(
            _last_sunday(candidate.year, 10), time(1), tzinfo=timezone.utc
        )
        if start <= candidate < end:
            return hours
    return 1


def _local_day_window(day: date) -> tuple[str, str]:
    """Return the instants that span one *local* (Vienna) day, inclusively.

    The API parses a date-only value as UTC midnight, which is 01:00 or 02:00
    local time — that would cut the first intervals of the day off.
    """
    first = datetime.combine(day, time.min)
    last = datetime.combine(day, time(23, 59, 59))

    if TIMEZONE is not None:
        return (
            first.replace(tzinfo=TIMEZONE).isoformat(),
            last.replace(tzinfo=TIMEZONE).isoformat(),
        )

    # Each boundary gets its own offset, so the two days a year with a DST
    # switch still have exactly as many hours as they really have.
    return (
        first.replace(
            tzinfo=timezone(timedelta(hours=_fallback_offset_hours(first)))
        ).isoformat(),
        last.replace(
            tzinfo=timezone(timedelta(hours=_fallback_offset_hours(last)))
        ).isoformat(),
    )


def _sum_interval_values(records: list[dict[str, Any]]) -> float | None:
    """Return the summed kWh of a register's interval records."""
    total: float | None = None
    for record in records:
        value = record.get("value")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        total = (total or 0.0) + float(value)
    return total


def _quality_mix(records: list[dict[str, Any]]) -> dict[str, int]:
    """Count the records per quality label."""
    mix: dict[str, int] = {}
    for record in records:
        label = QUALITY_LABELS.get(record.get("quality"), "unknown")
        mix[label] = mix.get(label, 0) + 1
    return mix


def _worst_quality(mix: dict[str, int]) -> str:
    """Return the least trustworthy quality label of a mixture."""
    if not mix:
        return "unknown"
    return max(mix, key=lambda label: (QUALITY_RANK.get(label, 3), mix[label]))


def _group_by_code(payload: Any) -> dict[str, list[dict[str, Any]]]:
    """Group ``data_window`` records by their OBIS code."""
    groups: dict[str, list[dict[str, Any]]] = {}
    records = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(records, list):
        raise SmartmeterQueryError(f"Unexpected data window payload: {payload!r}")
    for record in records:
        if not isinstance(record, dict):
            continue
        code = record.get("obis_code")
        if isinstance(code, str):
            groups.setdefault(code, []).append(record)
    return groups


def _location_info(raw: dict[str, Any]) -> dict[str, Any]:
    """Flatten the expanded location into the integration's info keys."""
    location = raw.get("location")
    if not isinstance(location, dict):
        return {}

    info: dict[str, Any] = {}
    address = location.get("address")
    if isinstance(address, list):
        address = address[0] if address and isinstance(address[0], dict) else None
    if isinstance(address, dict):
        info["address"] = address
        # Diagnostics are only created for the keys sensor.py knows.
        info["verbrauchsstelle"] = {
            "strasse": address.get("street"),
            "postleitzahl": address.get("postal_code"),
            "ort": address.get("city"),
        }
    if location.get("customer_id"):
        info["geschaeftspartner"] = location["customer_id"]
    if location.get("name"):
        info["location_name"] = location["name"]
    return info


def _normalise_metering_point(raw: Any) -> dict[str, Any]:
    """Convert an API smart meter into the shape the coordinator expects."""
    if not isinstance(raw, dict):
        raise SmartmeterQueryError(f"Unexpected metering point payload: {raw!r}")

    zaehlpunktnummer = raw.get("metering_point_number")
    if not zaehlpunktnummer:
        raise SmartmeterQueryError("The API returned a smart meter without a number.")

    info = {key: value for key, value in raw.items() if key != "location"}
    info.update(_location_info(raw))

    info["zaehlpunktnummer"] = zaehlpunktnummer
    # The dashboard label is the friendly name; the API falls back to the
    # metering point number if none was given.
    info["zaehlpunktName"] = (
        raw.get("display_name") or raw.get("label") or zaehlpunktnummer
    )
    info["zaehlpunktAnlagentyp"] = (
        "FEEDING" if raw.get("energy_direction") == "feed_in" else "CONSUMING"
    )

    # Keep the API's own vocabulary visible as attributes.
    info["smart_meter_id"] = raw.get("id")
    info["kommoditaet"] = raw.get("commodity")
    info["messintervall"] = raw.get("granularity")
    info["meter_status"] = raw.get("status")
    info["demo_meter"] = raw.get("is_demo")

    consent = raw.get("consent")
    if isinstance(consent, dict):
        info["consent_state"] = consent.get("state")
        info["consent_expires_at"] = consent.get("expires_at")
    operator = raw.get("grid_operator")
    if isinstance(operator, dict):
        info["grid_operator"] = operator.get("name")
        info["grid_operator_code"] = operator.get("code")
    return info


class EnergiedatenAtClient(SmartmeterClient):
    """Client for the energiedaten.at REST API."""

    def __init__(self, api_key: str | None, password: str | None = None):
        # The base class keeps the credential for its subclasses; the password
        # slot stays empty because this provider has no portal login.
        super().__init__(api_key, None)
        self.api_key = (api_key or "").strip()
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "application/json",
                "Energiedaten-Version": API_VERSION,
                "User-Agent": (
                    "AustriaSmartmeter-HASS/1.2 "
                    "(+https://github.com/acdcnow/AustrianSmartMeter-for-Home-Assistant)"
                ),
            }
        )
        self._login_time: datetime | None = None
        # metering point number -> API resource, filled by zaehlpunkte().
        self._meters: dict[str, dict[str, Any]] = {}

    # -------------------------------------------------------------- session

    def is_logged_in(self) -> bool:
        """Return True while the API key is expected to be usable."""
        return self._login_time is not None and not self.is_login_expired()

    def is_login_expired(self) -> bool:
        """Return True when the key has to be validated again."""
        if self._login_time is None:
            return True
        return datetime.now() - self._login_time >= SESSION_MAX_AGE

    def _ensure_session(self) -> None:
        """Validate the API key if that has not happened recently."""
        if not self.is_logged_in():
            self.login()

    # ----------------------------------------------------------------- http

    def _request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        """Perform a request, converting transport errors into our own error."""
        try:
            return self.session.request(method, url, timeout=REQUEST_TIMEOUT, **kwargs)
        except requests.exceptions.RequestException as err:
            raise SmartmeterConnectionError(f"Request to {url} failed: {err}") from err

    def _get_json(
        self, url: str, *, login: bool = False, **kwargs: Any
    ) -> Any:
        """GET a URL and return its JSON body."""
        response = self._request("GET", url, **kwargs)
        _raise_for_status(response, url, login=login)
        return _decode_json(response, url)

    def _get_list(self, path: str, params: dict[str, Any] | None = None) -> list[Any]:
        """Follow ``page``/``per_page`` pagination of a list endpoint."""
        query = dict(params or {})
        query.setdefault("per_page", MAX_PER_PAGE)

        items: list[Any] = []
        for page in range(1, MAX_PAGES + 1):
            query["page"] = page
            payload = self._get_json(f"{BASE_URL}{path}", params=query)
            if not isinstance(payload, dict) or not isinstance(
                payload.get("data"), list
            ):
                raise SmartmeterQueryError(
                    f"Unexpected list payload from {path}: {payload!r}"
                )
            items.extend(payload["data"])
            if not payload.get("has_more"):
                return items

        LOGGER.warning(
            "energiedaten.at: stopped paging %s after %s pages", path, MAX_PAGES
        )
        return items

    # ---------------------------------------------------------------- login

    def login(self):
        """Validate the API key.

        The API is stateless — there is no session to open, so this only proves
        that the key is accepted. Every request carries it afterwards.
        """
        if not self.api_key:
            raise SmartmeterLoginError("No API key configured.")

        LOGGER.debug("energiedaten.at: validating API key %s", _masked(self.api_key))
        payload = self._get_json(
            f"{BASE_URL}/smart-meters", params={"per_page": 1}, login=True
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise SmartmeterConnectionError(
                "The API key was accepted but the response was not understood: "
                f"{payload!r}"
            )

        self._login_time = datetime.now()
        LOGGER.debug("energiedaten.at: API key accepted (%s)", _masked(self.api_key))
        return self

    # ------------------------------------------------------------- metering

    def zaehlpunkte(self) -> list[dict[str, Any]]:
        """Return the smart meters (Zählpunkte) of this API key's team."""
        self._ensure_session()
        try:
            raw_meters = self._get_list("/smart-meters", {"expand[]": "location"})
        except SmartmeterConnectionError:
            # The key may have been revoked or rotated; validate it once more.
            self._login_time = None
            self._ensure_session()
            raw_meters = self._get_list("/smart-meters", {"expand[]": "location"})

        metering_points = [
            _normalise_metering_point(item) for item in raw_meters
        ]
        # Remember the API ids so historical_data() does not have to guess them.
        self._meters = {
            info["zaehlpunktnummer"]: item
            for item, info in zip(raw_meters, metering_points)
        }

        if not metering_points:
            raise SmartmeterQueryError(
                "No smart meters found for this API key. Add a metering point in "
                "the energiedaten.at dashboard and wait until its consent is "
                "connected."
            )
        return [{"zaehlpunkte": metering_points}]

    def _meter_resource(self, zaehlpunktnummer: str) -> dict[str, Any]:
        """Return the API resource of a metering point.

        The data endpoints are keyed by the meter's UUID, which is not the
        metering point number. It is resolved from the API instead of being
        guessed, and cached for the lifetime of the coordinator.
        """
        meter = self._meters.get(zaehlpunktnummer)
        if meter is not None:
            return meter

        self.zaehlpunkte()
        meter = self._meters.get(zaehlpunktnummer)
        if meter is None:
            raise SmartmeterQueryError(
                f"No smart meter with the metering point number "
                f"{zaehlpunktnummer} in this account."
            )
        return meter

    # ------------------------------------------------------------- readings

    def _fetch_window(
        self, meter_id: str, codes: tuple[str, ...], day: date
    ) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
        """Return one local day of records, grouped by code, plus the envelope."""
        date_from, date_until = _local_day_window(day)
        url = f"{BASE_URL}/smart-meters/{quote(meter_id)}/data"
        payload = self._get_json(
            url,
            params={
                "from": date_from,
                "to": date_until,
                "obis_codes[]": list(codes),
            },
        )

        window: dict[str, Any] = {}
        if isinstance(payload, dict):
            if payload.get("is_truncated"):
                LOGGER.warning(
                    "energiedaten.at: the data window for %s on %s was truncated "
                    "at %s records; the daily total may be incomplete",
                    meter_id,
                    day,
                    SERVER_RECORD_CAP,
                )
            # Optional fields of the response envelope; both follow the OBIS
            # filter, so they describe the registers we asked for.
            for key in ("data_completeness", "max_updated_at", "unit"):
                if payload.get(key) is not None:
                    window[key] = payload[key]
        return _group_by_code(payload), window

    def historical_data(
        self, zaehlpunktnummer: str, date_from: date = None, date_until: date = None
    ) -> list[dict[str, Any]]:
        """Return the consumption of one local day as OBIS readings.

        The API reports *energy per interval* (kWh), not a cumulative meter
        reading, so the intervals are summed up and the reading is flagged as a
        daily period value. Each OBIS register that carries data becomes one
        reading block; a day without data returns an empty list, which leaves
        the entity ``unknown`` instead of faking a zero.
        """
        self._ensure_session()

        if date_until is None:
            date_until = date.today()
        if date_from is None:
            date_from = date_until - timedelta(days=1)
        # Data is delivered day-after, so one day per call is what matters; a
        # wider range would only make the entity lie about its period.
        day = date_from

        meter = self._meter_resource(zaehlpunktnummer)
        meter_id = meter.get("id")
        if not meter_id:
            raise SmartmeterQueryError(
                f"The API returned no id for metering point {zaehlpunktnummer}."
            )

        codes = _GAS_CODES if meter.get("commodity") == "gas" else _ELECTRICITY_CODES
        groups, window = self._fetch_window(str(meter_id), codes, day)
        if not groups:
            LOGGER.debug(
                "energiedaten.at: no data for %s on %s", zaehlpunktnummer, day
            )
            return []

        readings: list[dict[str, Any]] = []
        for obis_code, name, source_codes in _REGISTERS:
            source = next((code for code in source_codes if groups.get(code)), None)
            if source is None:
                continue

            records = groups[source]
            total_kwh = _sum_interval_values(records)
            if total_kwh is None:
                continue

            mix = _quality_mix(records)
            first = min(record.get("timestamp") or "" for record in records)
            last = max(
                (record.get("timestamp_end") or record.get("timestamp") or "")
                for record in records
            )

            LOGGER.debug(
                "energiedaten.at: %s %s -> %.3f kWh from %s interval(s) [%s]",
                zaehlpunktnummer,
                source,
                total_kwh,
                len(records),
                _worst_quality(mix),
            )

            reading: dict[str, Any] = {
                # 1.9.0 / 2.9.0 are the OBIS codes for energy consumed within an
                # interval, which is what the API reports. The cumulative meter
                # register (1.8.0) is not exposed by the API at all.
                "obisCode": obis_code,
                "name": name,
                "wertetyp": VALUE_TYPE_DAY,
                "einheit": UNIT_WH,
                "messwerte": [
                    {
                        "zeitpunkt": first or day.isoformat(),
                        "messwert": round(total_kwh * KWH_TO_WH, 3),
                        "status": "VALID",
                        "qualitaet": _worst_quality(mix),
                        "intervals": len(records),
                        "period_from": first,
                        "period_to": last,
                        "source_obis_code": source,
                        "quality_measured": mix.get("measured", 0),
                        "quality_estimated": mix.get("estimated", 0),
                        "quality_unreliable": mix.get("unreliable", 0),
                    }
                ],
            }

            # Envelope fields the API only sends when it can compute them.
            for key in ("data_completeness", "max_updated_at"):
                if window.get(key) is not None:
                    reading["messwerte"][0][key] = window[key]

            readings.append(reading)

        return readings

    def consumptions(self) -> list[dict[str, Any]]:
        """energiedaten.at has no ready made daily statistics.

        The daily total is already the main reading of every register, so there
        is nothing extra to fetch here.
        """
        return []
