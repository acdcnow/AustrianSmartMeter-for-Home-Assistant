"""energyLIVE (smartENERGY) API client.

energyLIVE is not a grid operator portal either, but it is also not an
aggregator: a small *interface* is attached to the meter (a gateway forwards
its data over LoRa when there is no WiFi at the meter), and smartENERGY
republishes what that hardware reads. The API is therefore device oriented and
authenticated with an API key that the smartENERGY app or customer portal
issues (*API Key verwalten*). Being a smartENERGY customer is not required.

API surface this adapter uses (all requests carry ``X-API-KEY``):

* ``GET /devices`` → the device ids, e.g. ``I-10082023-01658401`` for an
  interface and ``G-…`` for a gateway.
* ``GET /devices/{id}`` → device details, including ``type`` and ``serial``.
* ``GET /devices/{id}/measurements`` → the measurement keys of the device.
* ``GET /devices/{id}/measurements/latest`` →
  ``[{"measurement": …, "timestamp": <ms>, "value": …}]``.

Measurements are OBIS codes without separators for electrical values — the
``…7.0`` registers carry instantaneous power in W, the ``…8.0`` registers carry
a *cumulative* register in Wh — and plain names for device health
(``batteryVoltage``, ``roomTemperature``, ``loraRssi``, …).

Two consequences for this adapter:

* The registers grow monotonically, so they are reported as meter readings
  (``wertetyp == "METER_READ"``) and Home Assistant may build long-term
  statistics from them. The power values are measurements and are reported with
  the unit W, which the sensor platform turns into a proper power sensor.
* The API has no history: it only ever answers with the value the device sent
  last. ``date_from``/``date_until`` are accepted for interface compatibility
  and ignored. Device health values and the reactive energy registers are not
  exposed — the sensor platform cannot express their units.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
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

BASE_URL = "https://backend.energylive.e-steiermark.com/api/v1"
API_KEY_HEADER = "X-API-KEY"

REQUEST_TIMEOUT = 30

# The key is sent with every request and never expires on its own, but it can be
# revoked in the customer portal. Re-checking it once a day surfaces that early.
SESSION_MAX_AGE = timedelta(hours=24)

# The API reports energy in Wh and power in W already, so nothing is converted.
UNIT_WH = "Wh"
UNIT_W = "W"

# The registers are cumulative meter readings, the power values are not.
VALUE_TYPE_METER_READ = "METER_READ"
VALUE_TYPE_POWER = "POWER"

# Measurement key -> (integration OBIS code, sensor name, unit, value type).
#
# The API writes an OBIS code as ten digits: 0100010800 is 1.0.1.8.0, the
# cumulative consumption register (Wh). The integration's own vocabulary uses
# 1-1 as the channel group, so the codes are normalised onto 1-1:…
_REGISTERS: dict[str, tuple[str, str, str, str]] = {
    "0100010800": (
        "1-1:1.8.0",
        "Energy Consumption Total",
        UNIT_WH,
        VALUE_TYPE_METER_READ,
    ),
    "0100020800": (
        "1-1:2.8.0",
        "Energy Production Total",
        UNIT_WH,
        VALUE_TYPE_METER_READ,
    ),
    "0100010700": (
        "1-1:1.7.0",
        "Current Power Consumption",
        UNIT_W,
        VALUE_TYPE_POWER,
    ),
    "0100020700": (
        "1-1:2.7.0",
        "Current Power Feed-in",
        UNIT_W,
        VALUE_TYPE_POWER,
    ),
}

# Used to tell a metering device (consumption, feed-in) from a gateway.
_CONSUMPTION_CODES = ("0100010800", "0100010700")
_FEED_IN_CODES = ("0100020800", "0100020700")


def _masked(value: str | None) -> str:
    """Return a loggable hint for a secret, never the secret itself."""
    if not value:
        return "<none>"
    return f"…{value[-4:]}" if len(value) > 4 else "…"


def _timestamp_to_iso(value: Any) -> str | None:
    """Convert the API's millisecond epoch into an ISO 8601 UTC timestamp."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(float(value) / 1000, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _facility_type(measurements: list[str]) -> str:
    """Return a display type for a device, based on the values it reports."""
    if any(code in measurements for code in _CONSUMPTION_CODES):
        return "CONSUMING"
    if any(code in measurements for code in _FEED_IN_CODES):
        return "FEEDING"
    # A gateway only forwards the interface's data and reports its own health.
    return "GATEWAY"


def _normalise_device(details: dict[str, Any], measurements: list[str]) -> dict[str, Any]:
    """Convert an API device into the shape the coordinator expects."""
    device_id = details.get("id")
    if not device_id:
        raise SmartmeterQueryError(f"Device payload without an id: {details!r}")

    serial = details.get("serial")
    info = dict(details)
    info["zaehlpunktnummer"] = device_id
    # The serial is the friendlier name of the two; the device id is the key.
    info["zaehlpunktName"] = serial or device_id
    info["geraetNumber"] = serial
    # sensor.py turns these two into the "Smart Meter Type" diagnostic and the
    # model of the device.
    info["smartMeterType"] = details.get("type")
    info["zaehlpunktAnlagentyp"] = _facility_type(measurements)
    info["measurements"] = measurements
    return info


class EnergyliveClient(SmartmeterClient):
    """Client for the energyLIVE API of smartENERGY."""

    def __init__(self, api_key: str | None, password: str | None = None):
        # The base class keeps the credential for its subclasses; the password
        # slot stays empty because this provider has no portal login.
        super().__init__(api_key, None)
        self.api_key = (api_key or "").strip()
        self.session = requests.Session()
        self.session.headers.update(
            {
                API_KEY_HEADER: self.api_key,
                "Accept": "application/json",
                "User-Agent": (
                    "AustriaSmartmeter-HASS/1.2 "
                    "(+https://github.com/acdcnow/AustrianSmartMeter-for-Home-Assistant)"
                ),
            }
        )
        self._login_time: datetime | None = None

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

    def _get_json(self, url: str, **kwargs: Any) -> Any:
        """GET a URL and return its JSON body."""
        response = self._request("GET", url, **kwargs)

        if response.status_code in (401, 403):
            # The API answers 403 for an unknown or revoked key on every
            # endpoint, so this is always a credentials problem.
            raise SmartmeterLoginError(
                "The energyLIVE API key was rejected. Create a new key in the "
                "smartENERGY app or customer portal."
            )
        if response.status_code == 404:
            raise SmartmeterQueryError(f"{url} returned HTTP 404 (unknown device?).")
        if response.status_code != 200:
            raise SmartmeterConnectionError(
                f"{url} returned HTTP {response.status_code}."
            )

        try:
            return response.json()
        except ValueError as err:
            snippet = " ".join(response.text.split())[:200]
            raise SmartmeterConnectionError(
                f"{url} returned a non-JSON response "
                f"(HTTP {response.status_code}): {snippet!r}"
            ) from err

    # ---------------------------------------------------------------- login

    def login(self):
        """Validate the API key.

        There is no session to open — the key is sent with every request — so
        this only proves that the key is accepted.
        """
        if not self.api_key:
            raise SmartmeterLoginError("No API key configured.")

        LOGGER.debug("energyLIVE: validating API key %s", _masked(self.api_key))
        payload = self._get_json(f"{BASE_URL}/devices")
        if not isinstance(payload, list):
            raise SmartmeterConnectionError(
                f"The API key was accepted but the response was not understood: "
                f"{payload!r}"
            )

        self._login_time = datetime.now()
        LOGGER.debug("energyLIVE: API key accepted (%s)", _masked(self.api_key))
        return self

    # ------------------------------------------------------------- devices

    def zaehlpunkte(self) -> list[dict[str, Any]]:
        """Return the energyLIVE devices of this API key."""
        self._ensure_session()
        devices = self._get_json(f"{BASE_URL}/devices")
        if not isinstance(devices, list):
            raise SmartmeterQueryError(f"Unexpected device list: {devices!r}")

        meters: list[dict[str, Any]] = []
        for entry in devices:
            # Documented as a list of ids; accept objects too, just in case.
            device_id = entry if isinstance(entry, str) else None
            if device_id is None and isinstance(entry, dict):
                device_id = entry.get("id")
            if not device_id:
                LOGGER.debug("energyLIVE: skipping unusable device entry %r", entry)
                continue

            details = self._get_json(f"{BASE_URL}/devices/{quote(str(device_id))}")
            if not isinstance(details, dict):
                raise SmartmeterQueryError(f"Unexpected device payload: {details!r}")
            details.setdefault("id", device_id)

            measurements = self._get_json(
                f"{BASE_URL}/devices/{quote(str(device_id))}/measurements"
            )
            if not isinstance(measurements, list):
                raise SmartmeterQueryError(
                    f"Unexpected measurement list: {measurements!r}"
                )

            info = _normalise_device(
                details, [item for item in measurements if isinstance(item, str)]
            )
            LOGGER.debug(
                "energyLIVE: %s (%s) reports %s value(s)",
                info["zaehlpunktnummer"],
                info["zaehlpunktAnlagentyp"],
                len(info["measurements"]),
            )
            meters.append(info)

        if not meters:
            raise SmartmeterQueryError(
                "No energyLIVE devices found for this API key. Check that the "
                "key belongs to the account with the paired interface."
            )
        return [{"zaehlpunkte": meters}]

    # ------------------------------------------------------------- readings

    def historical_data(
        self, zaehlpunktnummer: str, date_from=None, date_until=None
    ) -> list[dict[str, Any]]:
        """Return the values the device sent last.

        The API exposes no history, so ``date_from``/``date_until`` are ignored:
        every poll reports the newest value of each register. A device without
        any of the supported registers (a bare gateway, a device that is not
        paired yet) returns an empty list, which leaves its entities unknown
        instead of faking a zero.
        """
        self._ensure_session()
        url = (
            f"{BASE_URL}/devices/{quote(str(zaehlpunktnummer))}/measurements/latest"
        )
        payload = self._get_json(url)
        if not isinstance(payload, list):
            raise SmartmeterQueryError(f"Unexpected measurement payload: {payload!r}")

        readings: list[dict[str, Any]] = []
        for register in _REGISTERS.items():
            measurement, (obis_code, name, unit, value_type) = register
            record = next(
                (
                    item
                    for item in payload
                    if isinstance(item, dict) and item.get("measurement") == measurement
                ),
                None,
            )
            if record is None:
                continue

            value = record.get("value")
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                LOGGER.debug(
                    "energyLIVE: %s reported a non-numeric %s value: %r",
                    zaehlpunktnummer,
                    measurement,
                    value,
                )
                continue

            # The sensor platform drops a reading without a timestamp, so fall
            # back to now if the device did not send one.
            timestamp = _timestamp_to_iso(record.get("timestamp")) or datetime.now(
                timezone.utc
            ).isoformat()

            readings.append(
                {
                    "obisCode": obis_code,
                    "name": name,
                    "wertetyp": value_type,
                    "einheit": unit,
                    "messwerte": [
                        {
                            "zeitpunkt": timestamp,
                            "messwert": float(value),
                            "status": "VALID",
                            "source_measurement": measurement,
                        }
                    ],
                }
            )

        if not readings:
            LOGGER.debug(
                "energyLIVE: %s has no energy or power value yet", zaehlpunktnummer
            )
        return readings

    def consumptions(self) -> list[dict[str, Any]]:
        """energyLIVE has no ready made daily statistics."""
        return []
