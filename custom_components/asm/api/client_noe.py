"""Netz Niederösterreich (EVN) API client.

The portal is a ``/orchestration`` ASP.NET service that authenticates with a
JSON POST and then keeps a **short lived session cookie**. Two details of that
contract cost this client two bug reports, and both are handled here:

* The login path is ``Authentication/Login``. A missing "i" made the portal
  answer an HTML error page, so *every* setup attempt failed (issue #1).
* The login **response body is not part of the contract**. The portal answers
  HTTP 200 and sets the cookie; several revisions answer with an empty body, and
  requiring JSON of it made setup fail again although the credentials were
  accepted (issue #5). Only a non-200 status, a 401/403 or an explicit
  ``{"success": false}`` is treated as a failed login.

The consumption endpoint answers 15-minute values in kWh for one day
(``ConsumptionRecord/Day``), and nothing else: the portal exposes no cumulative
meter register, so the summed day is reported as a period value
(``wertetyp == "DAY"``) rather than as a ``total_increasing`` meter reading. A
meter in an energy community is answered with one record per community beside
its own, which is why the meter's own record (the one without an ``ec_id``) is
the one that is summed.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
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

BASE_URL = "https://smartmeter.netz-noe.at/orchestration"

# NOTE: the portal endpoint is spelled "Authentication". An earlier revision of
# this client was missing the "i" in that path, so it posted to a URL that does
# not exist and the portal answered with an HTML error page, which made every
# single login fail.
LOGIN_URL = f"{BASE_URL}/Authentication/Login"

# Endpoint used by the current portal: returns every metering point of the
# logged in business partner in one call.
METERING_POINTS_URL = f"{BASE_URL}/User/GetMeteringPointsByBusinesspartnerId"

# Legacy two step endpoints, kept as a fallback for older portal revisions.
LEGACY_ACCOUNT_URL = f"{BASE_URL}/User/GetAccountIdByBussinespartnerId"
LEGACY_METER_URL = f"{BASE_URL}/User/GetMeteringPointByAccountId"

CONSUMPTION_RECORD_URL = f"{BASE_URL}/ConsumptionRecord/Day"

# The ``context`` query parameter selects the portal's data context. Both
# independent third party clients of this portal use ``2`` for the user and
# metering point endpoints; ``5`` - what this client sent before - and the legacy
# ``1`` are not used by either of them.
API_CONTEXT = 2

REQUEST_TIMEOUT = 30

# The portal session cookie is short lived and the API does not expose its
# expiry, so refresh the session proactively instead of waiting for a 401.
SESSION_MAX_AGE = timedelta(minutes=30)

# The portal reports energy in kWh, the entities are fed Wh (see sensor.py).
KWH_TO_WH = 1000
UNIT_WH = "Wh"

# Marker used by sensor.py to tell consumption values (which reset every day)
# apart from cumulative meter readings.
VALUE_TYPE_DAY = "DAY"


def _decode_json(response: requests.Response, url: str) -> Any:
    """Return the decoded JSON body, or raise a descriptive error.

    The portal answers with an HTML page whenever an endpoint does not exist or
    the session expired. Calling ``response.json()`` on that used to surface as
    the very unhelpful "Expecting value: line 1 column 1 (char 0)".
    """
    try:
        return response.json()
    except ValueError as err:
        snippet = " ".join(response.text.split())[:200]
        raise SmartmeterConnectionError(
            f"{url} returned a non-JSON response "
            f"(HTTP {response.status_code}): {snippet!r}"
        ) from err


def _optional_json(response: requests.Response, url: str) -> Any:
    """Return the decoded JSON body, or None when the response carries none.

    Used for the login endpoint, whose body is **not** part of the contract.
    Several portal revisions answer the login call with HTTP 200 and an empty
    body while they set the session cookie; others report a rejected login with
    ``{"success": false}``. Neither may be turned into a decoding error.
    """
    if not response.content:
        return None
    try:
        return response.json()
    except ValueError:
        snippet = " ".join(response.text.split())[:200]
        LOGGER.debug("%s answered without JSON: %r", url, snippet)
        return None


def _base_record(payload: Any) -> Any:
    """Return the meter's own record of a consumption response.

    A metering point that takes part in an energy community is answered with one
    record per community beside its own (those carry an ``ec_id``). Summing them
    would count the same energy twice, so the record without an ``ec_id`` wins
    and a response without one keeps its first record.
    """
    if not isinstance(payload, list):
        return payload
    if not payload:
        return None
    for record in payload:
        if isinstance(record, dict) and not record.get("ec_id"):
            return record
    return payload[0]


def _sum_consumption(payload: Any) -> float | None:
    """Sum up the consumption values of a ConsumptionRecord/Day response.

    The current portal returns a record holding parallel
    ``peakDemandTimes``/``meteredValues`` arrays, an older revision returned a
    ``consumptionRecords`` list. Both shapes are supported. ``None`` is returned
    when the response contains no usable values at all.
    """
    record = _base_record(payload)
    if record is None:
        return None

    values: list[float] = []
    if isinstance(record, dict):
        for value in record.get("meteredValues") or []:
            if isinstance(value, (int, float)):
                values.append(float(value))
        for entry in record.get("consumptionRecords") or []:
            if isinstance(entry, dict) and isinstance(
                entry.get("value"), (int, float)
            ):
                values.append(float(entry["value"]))

    if not values:
        return None
    return sum(values)


def _normalise_metering_point(raw: Any) -> dict[str, Any]:
    """Convert a portal metering point into the shape the coordinator expects."""
    if not isinstance(raw, dict):
        raise SmartmeterQueryError(f"Unexpected metering point payload: {raw!r}")

    zaehlpunktnummer = (
        raw.get("meteringPointId") or raw.get("meterId") or raw.get("countingPointId")
    )
    if not zaehlpunktnummer:
        raise SmartmeterQueryError("Portal returned a metering point without an id.")

    info = dict(raw)
    info["zaehlpunktnummer"] = zaehlpunktnummer
    # The portal does not always send a friendly name, but every meter of an
    # account still has to end up with a distinguishable device name.
    info["zaehlpunktName"] = raw.get("name") or zaehlpunktnummer
    # "smartMeterType" is e.g. "IME"; None means no smart meter is installed.
    info["zaehlpunktAnlagentyp"] = raw.get("smartMeterType") or raw.get(
        "consumptionType", "CONSUMING"
    )
    return info


class NetzNoeClient(SmartmeterClient):
    """Client for Netz Niederösterreich (EVN)."""

    def __init__(self, username, password):
        super().__init__(username, password)
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/json, text/plain, */*",
                "Origin": "https://smartmeter.netz-noe.at",
                "Referer": "https://smartmeter.netz-noe.at/",
                "User-Agent": (
                    "AustriaSmartmeter-HASS/1.2 "
                    "(+https://github.com/acdcnow/AustrianSmartMeter-for-Home-Assistant)"
                ),
            }
        )
        self._login_time: datetime | None = None

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
        """Drop the session cookies so the next call starts from scratch."""
        self.session.cookies.clear()
        self._login_time = None

    def _ensure_session(self) -> None:
        """Make sure a usable portal session exists."""
        if not self.is_logged_in():
            self._reset()
            self.login()

    # ----------------------------------------------------------------- http

    def _request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        """Perform a request, converting transport errors into our own error."""
        try:
            return self.session.request(method, url, timeout=REQUEST_TIMEOUT, **kwargs)
        except requests.exceptions.RequestException as err:
            raise SmartmeterConnectionError(f"Request to {url} failed: {err}") from err

    def _get_json(self, url: str, **kwargs: Any) -> Any:
        """GET a URL and return its JSON body.

        A session that the portal does not accept is reported as a login error,
        not as a connection error: the config flow then shows "check your
        credentials" instead of "could not connect".
        """
        response = self._request("GET", url, **kwargs)
        if response.status_code in (401, 403):
            raise SmartmeterLoginError(
                f"{url} rejected the session (HTTP {response.status_code}). "
                "Check username and password."
            )
        if response.status_code != 200:
            raise SmartmeterConnectionError(
                f"{url} returned HTTP {response.status_code}."
            )
        return _decode_json(response, url)

    # ---------------------------------------------------------------- login

    def login(self):
        """Establish a portal session.

        The portal answers this call with HTTP 200 and sets the session cookie;
        its **body is not the contract**. Issue #5 reported exactly that: the
        portal answers with an *empty* body, and refusing it made every single
        setup attempt fail with "returned a non-JSON response (HTTP 200): ''"
        although the credentials were accepted.

        What the body is still good for is a rejection. A revision that reports
        ``success: false`` is a credentials problem, and so is a 401/403. An
        unreadable or empty body is accepted - the session cookie is what
        counts - and the authenticated follow-up call in :meth:`zaehlpunkte`
        decides the rest (it reports an unauthenticated call as a login error).
        """
        LOGGER.debug("Netz NOE: authenticating user %s", self.username)

        response = self._request(
            "POST",
            LOGIN_URL,
            json={"user": self.username, "pwd": self.password},
        )

        if response.status_code in (401, 403):
            raise SmartmeterLoginError("Login failed. Check username and password.")
        if response.status_code != 200:
            raise SmartmeterConnectionError(
                f"Login endpoint returned HTTP {response.status_code}."
            )

        payload = _optional_json(response, LOGIN_URL)
        if isinstance(payload, dict) and payload.get("success") is False:
            raise SmartmeterLoginError("Login failed. Check username and password.")
        if payload is None:
            LOGGER.debug(
                "Netz NOE: the login endpoint answered without a JSON body; "
                "the session cookie is the contract"
            )

        self._login_time = datetime.now()
        return self

    # ------------------------------------------------------------- metering

    def zaehlpunkte(self) -> list[dict[str, Any]]:
        """Return the metering points (Zählpunkte) of the account."""
        self._ensure_session()
        try:
            metering_points = self._fetch_metering_points()
        except SmartmeterConnectionError:
            # The session may simply have expired mid-update.
            self._reset()
            self._ensure_session()
            metering_points = self._fetch_metering_points()

        if not metering_points:
            raise SmartmeterQueryError(
                "No metering points found in this account. Please check that the "
                "portal account has an active contract."
            )
        return [{"zaehlpunkte": metering_points}]

    def _fetch_metering_points(self) -> list[dict[str, Any]]:
        """Fetch the metering points, trying the current API first."""
        try:
            raw = self._get_json(METERING_POINTS_URL, params={"context": API_CONTEXT})
            if isinstance(raw, list) and raw:
                metering_points = [_normalise_metering_point(item) for item in raw]
                LOGGER.debug(
                    "Netz NOE: %s metering point(s) from %s",
                    len(metering_points),
                    METERING_POINTS_URL,
                )
                return metering_points
            LOGGER.debug(
                "Netz NOE: %s returned no metering points, trying legacy endpoints",
                METERING_POINTS_URL,
            )
        except (SmartmeterError, TypeError, ValueError) as err:
            LOGGER.debug(
                "Netz NOE: %s not usable (%s), trying legacy endpoints",
                METERING_POINTS_URL,
                err,
            )

        return [
            _normalise_metering_point(item)
            for item in self._fetch_metering_points_legacy()
        ]

    def _fetch_metering_points_legacy(self) -> list[Any]:
        """Fetch the metering points through the pre-2026 two step API."""
        accounts = self._get_json(LEGACY_ACCOUNT_URL, params={"context": API_CONTEXT})
        if not isinstance(accounts, list) or not accounts:
            raise SmartmeterQueryError("Portal returned no account for this login.")

        account_id = accounts[0].get("accountId")
        if not account_id:
            raise SmartmeterQueryError("Portal returned no account id for this login.")

        meters = self._get_json(
            LEGACY_METER_URL, params={"accountId": account_id, "context": API_CONTEXT}
        )
        if not isinstance(meters, list):
            raise SmartmeterQueryError(f"Unexpected metering point response: {meters!r}")
        return meters

    # ------------------------------------------------------------- readings

    def historical_data(
        self, zaehlpunktnummer: str, date_from: date = None, date_until: date = None
    ) -> list[dict[str, Any]]:
        """Return the consumption of a single day as an OBIS reading.

        The portal reports *energy consumed per interval* (kWh), not a
        cumulative meter reading, so the reading is flagged as a daily value.
        """
        self._ensure_session()

        if date_until is None:
            date_until = date.today()
        if date_from is None:
            date_from = date_until - timedelta(days=1)
        day = date_from

        payload = self._get_json(
            CONSUMPTION_RECORD_URL,
            params={"meterId": zaehlpunktnummer, "day": day.isoformat()},
        )

        total_kwh = _sum_consumption(payload)
        if total_kwh is None:
            LOGGER.debug(
                "Netz NOE: no consumption values for %s on %s", zaehlpunktnummer, day
            )
            return []

        return [
            {
                # 1.9.0 is the OBIS code for energy consumed within an interval,
                # which is exactly what the portal reports. The portal does not
                # expose the cumulative meter reading (1.8.0).
                "obisCode": "1-1:1.9.0",
                "name": "Daily Consumption",
                "wertetyp": VALUE_TYPE_DAY,
                "einheit": UNIT_WH,
                "messwerte": [
                    {
                        "zeitpunkt": f"{day.isoformat()}T00:00:00",
                        "messwert": round(total_kwh * KWH_TO_WH, 3),
                        "status": "VALID",
                    }
                ],
            }
        ]

    def consumptions(self) -> list[dict[str, Any]]:
        """Netz NOE does not expose ready made 'yesterday' statistics."""
        return []
