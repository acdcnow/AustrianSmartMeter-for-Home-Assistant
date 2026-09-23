"""DSMR / P1 client: reads the local customer interface of a smart meter.

The customer interface of an Austrian smart meter is not a web API. It is a
physical port - "H1" on a Sagemcom or Landis+Gyr meter - that pushes a DSMR
telegram every ten seconds over a short cable. This adapter therefore talks to
either

* a local serial device (``/dev/ttyUSB0``, ``/dev/serial/by-id/...``), or
* a network address (``socket://<host>:<port>``) of a P1 reader on the LAN,

and it needs no cloud account, no portal and no credentials: whatever polling
loop reads the telegram gets the meter's real-time data. Everything that would
be a login for the portal providers is a *connection* here.

Parsing is delegated to ``dsmr-parser``, the same library (and version) Home
Assistant's own DSMR integration uses, so all telegram dialects, the implied
decimal handling and the encrypted-telegram path behave identically. What this
adapter adds is the translation into the integration's reading contract:

* the cumulative registers (``1.8.0`` consumption, ``2.8.0`` feed-in) become
  meter readings in Wh, which the platform turns into total-increasing energy
  sensors, and
* the instantaneous power values (``1.7.0``, ``2.7.0``) become measurements in
  W, which the platform turns into power sensors.

The gas and water values that a DSMR telegram carries over M-Bus are *not*
exposed: the sensor platform speaks energy and power only, and a gas register is
measured in m³, not in Wh.

Two practical notes:

* DSMR has no history either. Every poll reads the current telegram, so the data
  is only as fresh as the configured scan interval.
* The integration's minimum scan interval is 60 minutes. The meter pushes every
  10 seconds, so a lower interval would be realistic - configure it with that in
  mind.
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from dsmr_parser import telegram_specifications
from dsmr_parser.clients.telegram_buffer import EncryptedTelegramBuffer, TelegramBuffer
from dsmr_parser.exceptions import DecryptionError, InvalidChecksumError, ParseError
from dsmr_parser.parsers import TelegramParser
from serialx import SerialException, serial_for_url

from .base import SmartmeterClient
from .dsmr_versions import DEFAULT_DSMR_VERSION, DSMR_VERSIONS
from .errors import (
    SmartmeterConnectionError,
    SmartmeterError,
    SmartmeterLoginError,
    SmartmeterQueryError,
)

LOGGER = logging.getLogger(__name__)

# How long to wait for a connection, for a telegram, and how long a telegram may
# be reused. The meter pushes every ten seconds, so a fresh telegram arrives
# quickly; the cache exists so that the three calls of one poll
# (login -> zaehlpunkte -> historical_data) read the meter once, not three times.
CONNECT_TIMEOUT = 10
READ_TIMEOUT = 30
TELEGRAM_CACHE_SECONDS = 5
READ_CHUNK = 512

# The connection is re-validated with a real telegram on a short interval; it is
# a local read, so this costs nothing.
SESSION_MAX_AGE = timedelta(minutes=5)

# AES-128 keys are 32 hex characters.
ENCRYPTION_KEY_PATTERN = re.compile(r"[0-9a-fA-F]{32}")

# Reduced OBIS code of the telegram -> (integration OBIS code, sensor name, kind).
# The reduced form (C.D.E) is what every DSMR dialect uses for the "same"
# register, which is why the mapping is keyed on it.
_REGISTERS: tuple[tuple[str, str, str, str], ...] = (
    ("1.8.0", "1-1:1.8.0", "Energy Consumption Total", "energy"),
    ("2.8.0", "1-1:2.8.0", "Energy Production Total", "energy"),
    ("1.7.0", "1-1:1.7.0", "Current Power Consumption", "power"),
    ("2.7.0", "1-1:2.7.0", "Current Power Feed-in", "power"),
)

# Value names the telegram objects are published under, per reduced OBIS code.
# The first entry is what the specifications of the currently supported dialects
# use; the rest are the names older ones used, so a specification that renames a
# register still resolves.
_VALUE_NAMES: dict[str, tuple[str, ...]] = {
    "1.8.0": ("ELECTRICITY_IMPORTED_TOTAL", "ELECTRICITY_USED_TOTAL"),
    "2.8.0": ("ELECTRICITY_EXPORTED_TOTAL", "ELECTRICITY_DELIVERED_TOTAL"),
    "1.7.0": ("CURRENT_ELECTRICITY_USAGE",),
    "2.7.0": ("CURRENT_ELECTRICITY_DELIVERY",),
    "1.0.0": ("P1_MESSAGE_TIMESTAMP",),
    "96.1.1": ("EQUIPMENT_IDENTIFIER",),
    "0.2.8": ("P1_MESSAGE_HEADER",),
}

# Units the registers are published in, and what they are converted to.
_ENERGY_UNITS = {"Wh": 1.0, "kWh": 1000.0, "MWh": 1_000_000.0}
_POWER_UNITS = {"W": 1.0, "kW": 1000.0}

VALUE_TYPE_METER_READ = "METER_READ"
VALUE_TYPE_POWER = "POWER"
UNIT_WH = "Wh"
UNIT_W = "W"


def _specification_names(spec: dict[str, Any]) -> dict[str, str]:
    """Map the reduced OBIS code of every object to its value name.

    The specifications describe their objects with a regular expression per
    OBIS code, so the mapping is derived from the specification itself instead
    of being hardcoded per dialect.
    """
    names: dict[str, str] = {}
    for obj in spec.get("objects", []):
        match = re.search(
            r":(\d+)\.(\d+)\.(\d+)", str(obj.get("obis_reference", "")).replace("\\", "")
        )
        if match:
            names[".".join(match.groups())] = obj["value_name"]
    return names


def _object_for(
    telegram: Any, names: dict[str, str], obis_code: str
) -> tuple[Any | None, str | None]:
    """Return the parsed object and the value name used for it."""
    candidates: list[str] = []
    derived = names.get(obis_code)
    if derived:
        candidates.append(derived)
    candidates.extend(_VALUE_NAMES.get(obis_code, ()))

    for name in candidates:
        obj = getattr(telegram, name, None)
        if obj is not None:
            return obj, name
    return None, None


def _decode_equipment_id(raw: Any) -> str | None:
    """Return a readable equipment identifier.

    DSMR publishes it hex encoded (``45303034...`` is ASCII ``E004...``), which
    is unreadable in a device name, so it is decoded when it looks like hex.
    """
    text = str(raw or "").strip()
    if not text:
        return None
    if len(text) % 2 == 0 and all(char in "0123456789abcdefABCDEF" for char in text):
        try:
            decoded = bytes.fromhex(text).decode("ascii")
        except (ValueError, UnicodeDecodeError):
            return text
        if decoded.isprintable():
            return decoded
    return text


def _telegram_timestamp(telegram: Any, spec: dict[str, Any]) -> str | None:
    """Return the meter's own reading time of a telegram as ISO 8601."""
    obj, _ = _object_for(telegram, _specification_names(spec), "1.0.0")
    value = getattr(obj, "value", None) if obj is not None else None
    if isinstance(value, datetime):
        return value.isoformat()
    return None


def _readings_from_telegram(
    telegram: Any, spec: dict[str, Any]
) -> list[dict[str, Any]]:
    """Translate a parsed telegram into the integration's reading blocks."""
    names = _specification_names(spec)
    zeitpunkt = _telegram_timestamp(telegram, spec) or datetime.now(
        timezone.utc
    ).isoformat()

    readings: list[dict[str, Any]] = []
    for obis_code, integration_code, name, kind in _REGISTERS:
        obj, value_name = _object_for(telegram, names, obis_code)
        if obj is None:
            continue

        value = getattr(obj, "value", None)
        unit = getattr(obj, "unit", None)
        if isinstance(value, bool):
            continue
        try:
            # Registers are published as Decimal, timestamps as datetime.
            numeric = float(value)
        except (TypeError, ValueError):
            LOGGER.debug(
                "DSMR: ignoring %s, value %r is not numeric", obis_code, value
            )
            continue

        factors = _ENERGY_UNITS if kind == "energy" else _POWER_UNITS
        factor = factors.get(str(unit))
        if factor is None:
            LOGGER.debug(
                "DSMR: ignoring %s, unit %r is not a known %s unit",
                obis_code,
                unit,
                kind,
            )
            continue

        readings.append(
            {
                "obisCode": integration_code,
                "name": name,
                "wertetyp": (
                    VALUE_TYPE_METER_READ if kind == "energy" else VALUE_TYPE_POWER
                ),
                "einheit": UNIT_WH if kind == "energy" else UNIT_W,
                "messwerte": [
                    {
                        "zeitpunkt": zeitpunkt,
                        "messwert": round(numeric * factor, 3),
                        "status": "VALID",
                        # The telegram's own code and unit, so the conversion is
                        # traceable in the attributes.
                        "source_obis_code": obis_code,
                        "source_unit": unit,
                        "source_value_name": value_name,
                    }
                ],
            }
        )
    return readings


def _metering_point_info(
    telegram: Any, spec: dict[str, Any], port: str, dsmr_version: str
) -> dict[str, Any]:
    """Describe the meter a telegram came from."""
    names = _specification_names(spec)

    equipment_obj, _ = _object_for(telegram, names, "96.1.1")
    equipment = _decode_equipment_id(
        getattr(equipment_obj, "value", None) if equipment_obj is not None else None
    )
    # Meters without an equipment identifier (DSMR 5S, and the Austrian Sagemcom)
    # are identified by their connection, which is stable across polls.
    identifier = equipment or port

    version_marker_obj, _ = _object_for(telegram, names, "0.2.8")
    version_marker = getattr(version_marker_obj, "value", None)

    present = [entry[0] for entry in _REGISTERS]
    registers = [
        obis_code
        for obis_code in present
        if _object_for(telegram, names, obis_code)[0] is not None
    ]
    has_consumption = any(code.startswith("1.") for code in registers)

    _, version_label = DSMR_VERSIONS.get(dsmr_version, ("", dsmr_version))
    return {
        "zaehlpunktnummer": identifier,
        "zaehlpunktName": identifier,
        "zaehlpunktAnlagentyp": "CONSUMING" if has_consumption else "FEEDING",
        # sensor.py turns this into the device serial number and the
        # "Smart Meter Type" diagnostic.
        "geraetNumber": equipment,
        "smartMeterType": f"DSMR {dsmr_version}",
        "dsmr_version": version_label,
        "dsmr_version_marker": version_marker,
        "port": port,
        "encrypted": bool(spec.get("general_global_cipher")),
        "registers": registers,
        "register_count": len(registers),
        "telegram_at": _telegram_timestamp(telegram, spec),
    }


class DsmrClient(SmartmeterClient):
    """Client for a DSMR / P1 customer interface."""

    def __init__(
        self,
        port: str | None,
        dsmr_version: str | None = None,
        encryption_key: str | None = None,
        password: str | None = None,
    ):
        # The base class keeps the credential for its subclasses; a DSMR port has
        # no username and the meter's key lives only in `encryption_key`, so it is
        # not duplicated into the generically named password slot.
        super().__init__(port, None)
        self.port = (port or "").strip()
        self.dsmr_version = (dsmr_version or DEFAULT_DSMR_VERSION).strip()
        self.encryption_key = (encryption_key or "").strip()
        self._login_time: datetime | None = None
        self._telegram: Any | None = None
        self._telegram_read_at: datetime | None = None

    # -------------------------------------------------------------- session

    def is_logged_in(self) -> bool:
        """Return True while a telegram has been read recently."""
        return self._login_time is not None and not self.is_login_expired()

    def is_login_expired(self) -> bool:
        """Return True when the connection has to be validated again."""
        if self._login_time is None:
            return True
        return datetime.now() - self._login_time >= SESSION_MAX_AGE

    def _ensure_session(self) -> None:
        """Read a telegram if that has not happened recently."""
        if not self.is_logged_in():
            self.login()

    # ---------------------------------------------------------- connection

    def specification(self) -> dict[str, Any]:
        """Return the telegram specification of the configured DSMR version."""
        entry = DSMR_VERSIONS.get(self.dsmr_version)
        if entry is None:
            raise SmartmeterQueryError(
                f"Unknown DSMR version {self.dsmr_version!r}. Supported: "
                f"{', '.join(sorted(DSMR_VERSIONS))}."
            )
        spec = getattr(telegram_specifications, entry[0], None)
        if not isinstance(spec, dict):
            raise SmartmeterQueryError(
                f"dsmr-parser has no telegram specification named {entry[0]!r}."
            )
        return spec

    def _check_key(self, spec: dict[str, Any]) -> str:
        """Return the AES key, or explain why the meter cannot be read."""
        if not spec.get("general_global_cipher"):
            return ""
        if not self.encryption_key:
            raise SmartmeterLoginError(
                f"DSMR {self.dsmr_version} sends encrypted telegrams. Enter the "
                "AES key of the meter (32 hex characters)."
            )
        if not ENCRYPTION_KEY_PATTERN.fullmatch(self.encryption_key):
            raise SmartmeterLoginError(
                "The DSMR decryption key must be 32 hexadecimal characters."
            )
        return self.encryption_key

    def _cached_telegram(self) -> Any | None:
        """Return the last telegram while it is fresh enough to be reused."""
        if self._telegram is None or self._telegram_read_at is None:
            return None
        age = datetime.now() - self._telegram_read_at
        if age.total_seconds() > TELEGRAM_CACHE_SECONDS:
            return None
        return self._telegram

    def _read_telegram(self, *, force: bool = False) -> Any:
        """Read and parse one telegram from the meter.

        Raises the integration's own errors: a connection that cannot be opened
        or a meter that stays silent is a connection problem, an unreadable
        telegram is a query problem, and a telegram that cannot be decrypted is
        a credentials problem (the key does not fit the meter).
        """
        cached = None if force else self._cached_telegram()
        if cached is not None:
            return cached

        if not self.port:
            raise SmartmeterQueryError("No DSMR port configured.")

        spec = self.specification()
        encryption_key = self._check_key(spec)
        encrypted = bool(spec.get("general_global_cipher"))
        parser = TelegramParser(spec)

        try:
            handle = serial_for_url(self.port, connect_timeout=CONNECT_TIMEOUT)
            handle.open()
        except SerialException as err:
            raise SmartmeterConnectionError(
                f"Could not open the DSMR port {self.port}: {err}"
            ) from err
        except OSError as err:
            raise SmartmeterConnectionError(
                f"Could not open the DSMR port {self.port}: {err}"
            ) from err

        try:
            handle.timeout = READ_TIMEOUT
            buffer = EncryptedTelegramBuffer() if encrypted else TelegramBuffer()
            deadline = time.monotonic() + READ_TIMEOUT

            while time.monotonic() < deadline:
                data = handle.read(READ_CHUNK)
                if not data:
                    continue
                # An encrypted telegram is a binary DLMS frame; a plain one is
                # text. The library decides where a frame ends.
                buffer.append(data if encrypted else data.decode("latin1"))

                for frame in buffer.get_all():
                    telegram = parser.parse(
                        frame,
                        encryption_key=encryption_key,
                        # None = decrypt without verifying the GCM tag, like Home
                        # Assistant does: the telegram's own CRC still catches
                        # transmission errors.
                        authentication_key=None,
                    )
                    self._telegram = telegram
                    self._telegram_read_at = datetime.now()
                    LOGGER.debug("DSMR: telegram read from %s", self.port)
                    return telegram

            raise SmartmeterConnectionError(
                f"No DSMR telegram from {self.port} within {READ_TIMEOUT} seconds. "
                "Check the cable/adapter and the configured DSMR version."
            )
        except SmartmeterError:
            raise
        except DecryptionError as err:
            # The key does not fit the meter: a configuration problem, so the
            # next poll validates again instead of trusting the session.
            self._login_time = None
            raise SmartmeterLoginError(
                "The DSMR telegram could not be decrypted. Check the AES key "
                f"of the meter ({err})"
            ) from err
        except (InvalidChecksumError, ParseError) as err:
            raise SmartmeterQueryError(f"Unreadable DSMR telegram: {err}") from err
        except (SerialException, OSError) as err:
            raise SmartmeterConnectionError(
                f"Reading {self.port} failed: {err}"
            ) from err
        except Exception as err:  # noqa: BLE001 - never leak a library traceback
            raise SmartmeterConnectionError(
                f"Reading {self.port} failed with an unexpected error: {err}"
            ) from err
        finally:
            try:
                handle.close()
            except Exception:  # noqa: BLE001 - closing must never mask the cause
                LOGGER.debug("DSMR: closing %s failed", self.port, exc_info=True)

    # ---------------------------------------------------------------- login

    def login(self):
        """Validate the connection by reading one telegram."""
        self._read_telegram(force=True)
        self._login_time = datetime.now()
        LOGGER.debug("DSMR: connection to %s works", self.port)
        return self

    # ------------------------------------------------------------- metering

    def zaehlpunkte(self) -> list[dict[str, Any]]:
        """Return the meter behind the configured port.

        A customer interface is a single meter, so this is always one metering
        point, identified by the meter's equipment identifier (or by the port for
        meters that do not publish one).
        """
        self._ensure_session()
        spec = self.specification()
        telegram = self._read_telegram()
        info = _metering_point_info(telegram, spec, self.port, self.dsmr_version)
        LOGGER.debug(
            "DSMR: metering point %s with registers %s",
            info["zaehlpunktnummer"],
            info["registers"],
        )
        return [{"zaehlpunkte": [info]}]

    # ------------------------------------------------------------- readings

    def historical_data(
        self, zaehlpunktnummer: str, date_from=None, date_until=None
    ) -> list[dict[str, Any]]:
        """Return the registers and power values of the current telegram.

        The interface has no history - it pushes the present state - so
        ``date_from``/``date_until`` are ignored. A register the meter does not
        report is left out, which keeps its entity unknown instead of inventing
        a zero.
        """
        self._ensure_session()
        spec = self.specification()
        telegram = self._read_telegram()

        readings = _readings_from_telegram(telegram, spec)
        if not readings:
            LOGGER.debug(
                "DSMR: %s reported none of the supported registers", zaehlpunktnummer
            )
        return readings

    def consumptions(self) -> list[dict[str, Any]]:
        """A customer interface has no ready made daily statistics."""
        return []
