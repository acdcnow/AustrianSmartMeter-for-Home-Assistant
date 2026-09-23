"""aWATTar market price client.

aWATTar publishes the EPEX Spot day-ahead prices of the Austrian and German
market through a public API - no account, no token, no metering point. This is
therefore the one provider here that does not read a meter: its "metering point"
is the market itself and its reading is a price per kWh, which is what makes
load shifting (heat pump, wallbox, battery) automatable.

Facts, checked against the live API:

* ``GET https://api.awattar.{at,de}/v1/marketdata`` answers with
  ``{"object": "list", "data": [{"start_timestamp", "end_timestamp",
  "marketprice", "unit"}], "url": …}``. The timestamps are milliseconds, the
  unit is currently ``Eur/MWh``.
* ``start``/``end`` are optional millisecond filters. Without them the feed
  covers the current hour and up to 24 hours ahead; it is refreshed every day at
  14:00 for the following day.
* No token has been required since 2020, but aWATTar asks for fair use:
  **100 requests per day**. One request per poll stays far below that, and the
  three calls of a poll share one response.

Prices are converted to **ct/kWh**, the unit Austrian and German tariffs are
quoted in (1 Eur/MWh = 0.1 ct/kWh); the value as delivered stays visible in the
attributes, and negative prices are passed through unchanged, because they are
the interesting ones.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from .base import SmartmeterClient
from .errors import (
    SmartmeterConnectionError,
    SmartmeterLoginError,
    SmartmeterQueryError,
)

LOGGER = logging.getLogger(__name__)

# Market area -> (endpoint, label). The two markets differ only in their host.
MARKET_AREAS: dict[str, tuple[str, str]] = {
    "AT": ("https://api.awattar.at/v1/marketdata", "Austria"),
    "DE": ("https://api.awattar.de/v1/marketdata", "Germany"),
}
DEFAULT_MARKET_AREA = "AT"

REQUEST_TIMEOUT = 30

# The feed is public; there is no session to hold. It is re-read at most every
# 30 minutes so that a poll never spends more than one of the 100 daily requests.
SESSION_MAX_AGE = timedelta(minutes=30)

# Within one poll the three calls reuse the same response.
CACHE_SECONDS = 5

# The unit the price is reported in, and what the API delivers it in.
TARGET_UNIT = "ct/kWh"
_UNIT_FACTORS: dict[str, float] = {"Eur/MWh": 0.1, "Ct/KWh": 1.0, "ct/kWh": 1.0}

# A price is not an OBIS register, so the reading carries a marker instead of a
# code that would pretend to be one.
OBIS_PRICE = "MARKET-PRICE"
VALUE_TYPE_PRICE = "PRICE"

# How the device should be described in Home Assistant.
DEVICE_MODEL = "Market price feed (EPEX day-ahead)"


def _timestamp(milliseconds: Any) -> datetime | None:
    """Convert the API's millisecond epoch into a datetime."""
    if isinstance(milliseconds, bool) or not isinstance(milliseconds, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(float(milliseconds) / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _price_per_kwh(record: dict[str, Any]) -> float | None:
    """Return a record's price in ct/kWh, or None when it is unusable."""
    price = record.get("marketprice")
    if isinstance(price, bool) or not isinstance(price, (int, float)):
        try:
            price = float(price)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None

    unit = str(record.get("unit") or "")
    factor = _UNIT_FACTORS.get(unit)
    if factor is None:
        LOGGER.debug("aWATTar: unknown price unit %r, reporting it as delivered", unit)
        factor = 1.0
    # Rounding here keeps the float noise of the conversion (199.38 / 10) out of
    # the entity state and every attribute below.
    return round(float(price) * factor, 3)


def _current_record(
    records: list[dict[str, Any]], now: datetime
) -> dict[str, Any] | None:
    """Return the record whose hour contains ``now``.

    Falls back to the first record of the horizon: a price feed answers from the
    current hour on, but a request made just before the top of the hour can
    briefly return only future ones.
    """
    for record in records:
        start = _timestamp(record.get("start_timestamp"))
        end = _timestamp(record.get("end_timestamp"))
        if start is not None and end is not None and start <= now < end:
            return record
    return records[0] if records else None


def _reading_from_prices(
    records: list[dict[str, Any]], market_area: str, now: datetime
) -> list[dict[str, Any]]:
    """Turn the price feed into the integration's reading contract.

    One reading with one value: the price of the hour that is running now. The
    upcoming hours are summarised in the attributes, because the sensor platform
    shows the *latest* value of a reading and a price series would therefore
    display the last hour of the horizon instead of the current one.
    """
    if not records:
        return []

    current = _current_record(records, now)
    if current is None:
        return []
    price = _price_per_kwh(current)
    if price is None:
        raise SmartmeterQueryError(f"Price feed record without a usable price: {current!r}")

    start = _timestamp(current.get("start_timestamp"))
    end = _timestamp(current.get("end_timestamp"))

    # Everything from the current hour on: what a load-shifting automation cares
    # about. Prices can be negative, so min/max are taken over the raw values.
    upcoming = [
        price_value
        for price_value in (
            _price_per_kwh(record)
            for record in records
            if (_timestamp(record.get("end_timestamp")) or now) > now
        )
        if price_value is not None
    ]
    cheapest = min(
        (
            (_price_per_kwh(record), _timestamp(record.get("start_timestamp")))
            for record in records
            if (_timestamp(record.get("end_timestamp")) or now) > now
        ),
        key=lambda item: item[0] if item[0] is not None else float("inf"),
        default=(None, None),
    )

    next_record = None
    if end is not None:
        next_record = next(
            (record for record in records if _timestamp(record.get("start_timestamp")) == end),
            None,
        )

    attributes: dict[str, Any] = {
        # The price exactly as the API delivered it.
        "source_price": current.get("marketprice"),
        "source_unit": current.get("unit"),
        "market_area": market_area,
        "market_name": MARKET_AREAS.get(market_area, ("", market_area))[1],
        "hour_start": start.isoformat() if start else None,
        "hour_end": end.isoformat() if end else None,
        "hours_available": len(records),
    }
    if upcoming:
        attributes["min_price_upcoming"] = round(min(upcoming), 3)
        attributes["max_price_upcoming"] = round(max(upcoming), 3)
        attributes["average_price_upcoming"] = round(sum(upcoming) / len(upcoming), 3)
    if cheapest[0] is not None:
        attributes["cheapest_price_upcoming"] = round(float(cheapest[0]), 3)
        attributes["cheapest_from"] = (
            cheapest[1].isoformat() if cheapest[1] is not None else None
        )
    next_price = _price_per_kwh(next_record) if next_record else None
    if next_price is not None:
        attributes["next_hour_price"] = round(next_price, 3)

    return [
        {
            "obisCode": OBIS_PRICE,
            "name": "Market Price",
            "wertetyp": VALUE_TYPE_PRICE,
            "einheit": TARGET_UNIT,
            "messwerte": [
                {
                    "zeitpunkt": (start or now).isoformat(),
                    "messwert": round(price, 3),
                    "status": "VALID",
                    **attributes,
                }
            ],
        }
    ]


class AwattarClient(SmartmeterClient):
    """Client for the aWATTar market data feed."""

    def __init__(self, market_area: str | None, password: str | None = None):
        # The base class keeps the credential for its subclasses; this provider
        # has neither a user nor a secret - the market area is the "account".
        super().__init__(market_area, None)
        self.market_area = (market_area or DEFAULT_MARKET_AREA).strip().upper()
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/json",
                "User-Agent": (
                    "AustriaSmartmeter-HASS/1.2 "
                    "(+https://github.com/acdcnow/AustrianSmartMeter-for-Home-Assistant)"
                ),
            }
        )
        self._login_time: datetime | None = None
        self._records: list[dict[str, Any]] | None = None
        self._records_read_at: datetime | None = None

    # -------------------------------------------------------------- session

    def is_logged_in(self) -> bool:
        """Return True while the feed has been reachable recently."""
        return self._login_time is not None and not self.is_login_expired()

    def is_login_expired(self) -> bool:
        """Return True when the feed has to be read again."""
        if self._login_time is None:
            return True
        return datetime.now() - self._login_time >= SESSION_MAX_AGE

    def _ensure_session(self) -> None:
        """Read the feed if that has not happened recently."""
        if not self.is_logged_in():
            self.login()

    # ----------------------------------------------------------------- http

    def endpoint(self) -> str:
        """Return the market data URL of the configured area."""
        entry = MARKET_AREAS.get(self.market_area)
        if entry is None:
            raise SmartmeterQueryError(
                f"Unknown aWATTar market area {self.market_area!r}. Supported: "
                f"{', '.join(sorted(MARKET_AREAS))}."
            )
        return entry[0]

    def _get_json(self, url: str) -> Any:
        """GET a URL and return its JSON body."""
        try:
            response = self.session.request("GET", url, timeout=REQUEST_TIMEOUT)
        except requests.exceptions.RequestException as err:
            raise SmartmeterConnectionError(f"Request to {url} failed: {err}") from err

        if response.status_code in (401, 403):
            raise SmartmeterLoginError(
                f"{url} refused the request (HTTP {response.status_code})."
            )
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

    # -------------------------------------------------------------- prices

    def _cached_records(self) -> list[dict[str, Any]] | None:
        """Return the last feed response while it is fresh enough."""
        if self._records is None or self._records_read_at is None:
            return None
        if (datetime.now() - self._records_read_at).total_seconds() > CACHE_SECONDS:
            return None
        return self._records

    def _fetch_records(self, *, force: bool = False) -> list[dict[str, Any]]:
        """Return the price records of the configured market.

        One request per poll: the fair use agreement allows 100 per day and this
        uses 24.
        """
        cached = None if force else self._cached_records()
        if cached is not None:
            return cached

        url = self.endpoint()
        payload = self._get_json(url)
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise SmartmeterQueryError(f"Unexpected market data payload: {payload!r}")

        records = [record for record in payload["data"] if isinstance(record, dict)]
        if not records:
            raise SmartmeterQueryError(
                "The aWATTar feed returned no prices for "
                f"{MARKET_AREAS.get(self.market_area, ('', self.market_area))[1]}."
            )

        self._records = records
        self._records_read_at = datetime.now()
        LOGGER.debug("aWATTar: %s price(s) from %s", len(records), url)
        return records

    # ---------------------------------------------------------------- login

    def login(self):
        """Validate the feed by reading it once.

        There is no authentication; this only proves that the endpoint answers
        with a usable payload.
        """
        self._fetch_records(force=True)
        self._login_time = datetime.now()
        LOGGER.debug("aWATTar: feed for %s is available", self.market_area)
        return self

    # ------------------------------------------------------------- metering

    def zaehlpunkte(self) -> list[dict[str, Any]]:
        """Return the market as a single metering point.

        aWATTar has no meters, so the "metering point" is the market area; it is
        stable and it is what a price sensor belongs to.
        """
        self._ensure_session()
        records = self._fetch_records()
        endpoint, area_name = MARKET_AREAS.get(
            self.market_area, ("", self.market_area)
        )
        info = {
            "zaehlpunktnummer": f"awattar_{self.market_area.lower()}",
            "zaehlpunktName": f"aWATTar ({self.market_area})",
            "zaehlpunktAnlagentyp": "MARKET",
            "device_model": DEVICE_MODEL,
            "smartMeterType": "Market data feed",
            "market_area": self.market_area,
            "market_name": area_name,
            "endpoint": endpoint,
            "unit": TARGET_UNIT,
            "hours_available": len(records),
        }
        return [{"zaehlpunkte": [info]}]

    # ------------------------------------------------------------- readings

    def historical_data(
        self, zaehlpunktnummer: str, date_from=None, date_until=None
    ) -> list[dict[str, Any]]:
        """Return the price of the hour that is running now.

        ``date_from``/``date_until`` are ignored: the feed is a rolling horizon
        of day-ahead prices, not a history. The whole horizon is summarised in
        the attributes of the reading.
        """
        self._ensure_session()
        records = self._fetch_records()
        return _reading_from_prices(records, self.market_area, datetime.now(timezone.utc))

    def consumptions(self) -> list[dict[str, Any]]:
        """A price feed has no consumption statistics."""
        return []
