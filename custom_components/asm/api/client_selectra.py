"""Selectra Electricity Planning API client.

Selectra builds a forward-looking price plan for one *customer configuration*:
country, postcode, provider, offer and option are qualified once, and the
``/planning/prices`` endpoint then returns the priced time bands of exactly that
tariff - day/night bands, night hours, dynamic tariffs and the feed-in price of a
PV contract. That is what makes it interesting next to a spot price feed: a
household can automate against the tariff it actually pays.

It is, however, a commercial B2B API:

* Every call needs a personal bearer token (``Authorization: Bearer …``), which
  the user creates at api.selectra.com - the free tier has **60 calls per
  calendar month**, paid plans start at 1 €/month per market (Austria 200 €/month)
  with per-request tiers above 10 000 calls.
* The API answers with ``X-Quota-Limit``, ``X-Quota-Remaining`` and
  ``X-Quota-Reset`` headers on free tokens.

Two consequences shape this adapter:

* **It spends as few calls as possible.** A prices response carries
  ``next_update``: the plan is cached and only requested again once that moment
  has passed, so a household uses one or two calls per day no matter how short
  the scan interval is. When the quota is used up the last plan keeps being served
  (flagged as expired) instead of failing every poll. The plan *is* the session:
  ``is_login_expired()`` reports whether a new plan is due, so the coordinator's
  periodic login check cannot spend a call of its own.
* **The qualification is a questionnaire.** ``/planning/qualification`` returns
  the next questions (multiple choice, input or raw JSON) until ``done`` is true
  and hands back the ``inputs`` object that ``/planning/prices`` expects. The
  config flow drives that loop; the client only speaks it.

Everything below follows the public OpenAPI document (Electricity Planning API
1.0.0) - the adapter could not be verified against the live service, because
every call is token gated.

The three endpoints it knows:

* ``POST /planning/qualification`` - answer until done, collect ``inputs``
* ``POST /planning/prices`` - the priced periods of the qualified offer
* (``POST /planning/details`` exists as well, but costs another call and is not
  used: the feed-in price already comes with the prices.)
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any

import requests

from .base import SmartmeterClient
from .errors import (
    SmartmeterConnectionError,
    SmartmeterLoginError,
    SmartmeterQueryError,
)

LOGGER = logging.getLogger(__name__)

BASE_URL = "https://api.selectra.com/api"
QUALIFICATION_URL = f"{BASE_URL}/planning/qualification"
PRICES_URL = f"{BASE_URL}/planning/prices"

REQUEST_TIMEOUT = 30

# The API quotes EUR per kWh; Austrian and German tariffs are quoted in ct/kWh.
TARGET_UNIT = "ct/kWh"
_PRICE_FACTOR = 100.0

# A price is not an OBIS register, so the reading carries a marker instead.
OBIS_PRICE = "TARIFF-PRICE"
VALUE_TYPE_PRICE = "PRICE"

DEVICE_MODEL = "Retail tariff price plan"
DEFAULT_COUNTRY_CODE = "at"

# One poll asks the client for data more than once (the metering points, then the
# reading of each of them). Even when the API reports a next_update that has
# already passed - "check back later" - a single poll must never spend more than
# one call of the monthly quota; the next poll, at least MIN_SCAN_INTERVAL later,
# may read again.
POLL_CACHE_SECONDS = 300

# The questionnaire asks in these shapes (OpenAPI: select | input | text | json).
QUESTION_SELECT = "select"

# Headers a free token carries so a client can pace itself.
_QUOTA_HEADERS = {
    "X-Quota-Limit": "quota_limit",
    "X-Quota-Remaining": "quota_remaining",
    "X-Quota-Reset": "quota_reset",
}


def _number(value: Any) -> float | None:
    """Return a value as a float, or None when it is not numeric."""
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_ct_per_kwh(value: Any) -> float | None:
    """Convert a price from the API's EUR/kWh to ct/kWh."""
    number = _number(value)
    if number is None:
        return None
    return round(number * _PRICE_FACTOR, 4)


def _timestamp(value: Any) -> datetime | None:
    """Parse the API's ISO 8601 timestamps."""
    if not isinstance(value, str) or not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _period_at(periods: list[dict[str, Any]], now: datetime) -> dict[str, Any] | None:
    """Return the period whose window contains ``now``, else the first one."""
    for period in periods:
        start = _timestamp(period.get("start"))
        end = _timestamp(period.get("end"))
        if start is not None and end is not None and start <= now < end:
            return period
    return periods[0] if periods else None


def question_options(question: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return the selectable options of a question.

    Maps the key that is submitted in a form to the option's raw value and its
    label. The key has to be a string for Home Assistant's selectors, while the
    API expects the raw value (an integer id, for instance), so both are kept.
    """
    options = question.get("options")
    if not isinstance(options, list):
        return {}

    result: dict[str, dict[str, Any]] = {}
    for index, option in enumerate(options):
        if isinstance(option, dict):
            value = option.get("value")
            label = option.get("label") or value
        else:
            value = option
            label = option
        key = str(value)
        if key in result:
            key = f"{key}#{index}"
        result[key] = {"value": value, "label": str(label)}
    return result


def _reading_from_plan(
    plan: dict[str, Any],
    *,
    now: datetime,
    offer_label: str | None = None,
    extra: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Turn a prices response into the integration's reading contract.

    One reading with one value: the price of the period that is running now. The
    remaining periods are summarised in the attributes, because the sensor
    platform shows the *latest* value of a reading and a period series would
    display the far end of the plan instead of the current band.
    """
    periods = [period for period in plan.get("prices") or [] if isinstance(period, dict)]
    if not periods:
        return []

    current = _period_at(periods, now)
    if current is None:
        return []
    price = _to_ct_per_kwh(current.get("price"))
    if price is None:
        raise SmartmeterQueryError(
            f"The price plan contains a period without a usable price: {current!r}"
        )

    start = _timestamp(current.get("start"))
    end = _timestamp(current.get("end"))
    # True when the served price is not the price of *now* - either because the
    # plan has run out or because it starts later.
    expired = not (
        start is not None and end is not None and start <= now < end
    )

    upcoming: list[tuple[float, dict[str, Any]]] = []
    for period in periods:
        period_end = _timestamp(period.get("end"))
        value = _to_ct_per_kwh(period.get("price"))
        if value is None:
            continue
        if period_end is None or period_end > now:
            upcoming.append((value, period))

    attributes: dict[str, Any] = {
        "period_name": current.get("name"),
        "period_start": current.get("start"),
        "period_end": current.get("end"),
        "periods_available": len(periods),
        "next_update": plan.get("next_update"),
        "currency": plan.get("currency"),
        "plan_expired": expired,
        "source_price": current.get("price"),
        "source_unit": "EUR/kWh",
    }
    if offer_label:
        attributes["offer_label"] = offer_label
    feed_in = _to_ct_per_kwh(current.get("feed_in_price"))
    if feed_in is not None:
        attributes["feed_in_price"] = feed_in
        attributes["source_feed_in_price"] = current.get("feed_in_price")
    if upcoming:
        attributes["min_price_upcoming"] = min(value for value, _ in upcoming)
        attributes["max_price_upcoming"] = max(value for value, _ in upcoming)
        attributes["average_price_upcoming"] = round(
            sum(value for value, _ in upcoming) / len(upcoming), 4
        )
        cheapest_value, cheapest_period = min(upcoming, key=lambda item: item[0])
        attributes["cheapest_price_upcoming"] = cheapest_value
        attributes["cheapest_period_name"] = cheapest_period.get("name")
        attributes["cheapest_from"] = cheapest_period.get("start")
    if end is not None:
        following = next(
            (
                period
                for period in periods
                if _timestamp(period.get("start")) == end
            ),
            None,
        )
        if following is not None:
            attributes["next_period_name"] = following.get("name")
            next_price = _to_ct_per_kwh(following.get("price"))
            if next_price is not None:
                attributes["next_period_price"] = next_price
    if plan.get("requalification_reason"):
        attributes["requalification_reason"] = plan["requalification_reason"]
    if extra:
        attributes.update(extra)

    return [
        {
            "obisCode": OBIS_PRICE,
            "name": "Tariff Price",
            "wertetyp": VALUE_TYPE_PRICE,
            "einheit": TARGET_UNIT,
            "messwerte": [
                {
                    "zeitpunkt": (start or now).isoformat(),
                    "messwert": price,
                    "status": "VALID",
                    **attributes,
                }
            ],
        }
    ]


class SelectraClient(SmartmeterClient):
    """Client for the Selectra Electricity Planning API."""

    def __init__(
        self,
        token: str | None,
        inputs: dict[str, Any] | None = None,
        label: str | None = None,
        password: str | None = None,
    ):
        # The base class keeps the credential for its subclasses; a Selectra
        # account is a bearer token, so it goes into the username slot and never
        # into the password one.
        super().__init__(token, None)
        self.token = (token or "").strip()
        self.inputs: dict[str, Any] = dict(inputs or {})
        self.label = label
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

        self._plan: dict[str, Any] | None = None
        self._plan_next_update: datetime | None = None
        self._plan_fetched_at: datetime | None = None
        self._requalification_logged = False
        self.quota: dict[str, Any] = {}

    # -------------------------------------------------------------- session

    def is_logged_in(self) -> bool:
        """Return True while a price plan is held.

        A Selectra account is a token rather than a session, so what is left to
        hold is the plan that token bought - and it stays valid until the API's
        own ``next_update`` moment.
        """
        return self._plan is not None

    def is_login_expired(self) -> bool:
        """Return True when a new plan has to be read.

        Deliberately tied to the plan instead of to a clock: the coordinator asks
        for a login when - and only when - the held plan has run out, which is
        what keeps a 60-minute poll from spending more than the monthly quota.
        """
        return not self._plan_is_fresh()

    def _ensure_session(self) -> None:
        """Read a plan when none is held or the held one has run out."""
        if not self._plan_is_fresh():
            self.login()

    # ----------------------------------------------------------------- http

    def _post(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        """POST a payload and return the parsed response."""
        try:
            response = self.session.request(
                "POST", url, json=payload, timeout=REQUEST_TIMEOUT
            )
        except requests.exceptions.RequestException as err:
            raise SmartmeterConnectionError(f"Request to {url} failed: {err}") from err

        self._remember_quota(response)

        if response.status_code in (200, 201):
            try:
                return response.json()
            except ValueError as err:
                snippet = " ".join(response.text.split())[:200]
                raise SmartmeterConnectionError(
                    f"{url} returned a non-JSON response "
                    f"(HTTP {response.status_code}): {snippet!r}"
                ) from err

        # Every failure answers with JSON: only the pricing engine's own 400 puts
        # the reason into `error`, everything else into `message`.
        body: Any = None
        try:
            body = response.json()
        except ValueError:
            body = None
        detail = ""
        if isinstance(body, dict):
            detail = str(body.get("message") or body.get("error") or "")
            field_errors = body.get("errors")
            if isinstance(field_errors, dict) and field_errors:
                detail = f"{detail} {field_errors}".strip()
        text = f"{url} returned HTTP {response.status_code}"
        if detail:
            text = f"{text}: {detail}"

        if response.status_code == 401:
            raise SmartmeterLoginError(
                "The Selectra API token was rejected. Create one at "
                f"api.selectra.com ({detail or 'invalid or inactive token'})."
            )
        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After", "unknown")
            raise SmartmeterConnectionError(
                f"{text} Rate limit or monthly quota reached, retry after "
                f"{retry_after}s."
            )
        if response.status_code in (400, 403, 404):
            raise SmartmeterQueryError(text)
        if response.status_code == 422:
            raise SmartmeterQueryError(f"{text} (request validation failed)")
        raise SmartmeterConnectionError(text)

    def _remember_quota(self, response: requests.Response) -> None:
        """Keep the quota information a free token carries."""
        for header, key in _QUOTA_HEADERS.items():
            value = response.headers.get(header)
            if value is None:
                continue
            number = _number(value)
            self.quota[key] = int(number) if number is not None else value
        if response.headers.get("X-Quota-Remaining") == "0":
            LOGGER.warning(
                "Selectra: the monthly quota is used up (%s of %s calls)",
                self.quota.get("quota_remaining"),
                self.quota.get("quota_limit"),
            )

    # ------------------------------------------------------- qualification

    def qualify(self, answers: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send one round of the qualification questionnaire.

        The caller answers the returned ``questions`` and calls again with the
        accumulated answers until the response reports ``done``. The final
        response's ``inputs`` object is what the prices endpoint takes.
        """
        if not self.token:
            raise SmartmeterLoginError("No Selectra API token configured.")
        payload = dict(answers or {})
        response = self._post(QUALIFICATION_URL, payload)
        if not isinstance(response, dict):
            raise SmartmeterQueryError(
                f"Unexpected qualification response: {response!r}"
            )
        LOGGER.debug(
            "Selectra: qualification done=%s, %s question(s)",
            response.get("done"),
            len(response.get("questions") or []),
        )
        return response

    # ------------------------------------------------------------- pricing

    def _plan_is_fresh(self) -> bool:
        """Return True while the cached plan must not be re-requested."""
        if self._plan is None:
            return False
        now = datetime.now(timezone.utc)
        if self._plan_next_update is not None and now < self._plan_next_update:
            return True
        # Either the API did not announce a next_update or it has passed without
        # new data being published. The plan is stale, but the rest of this poll
        # still has to be served from it.
        return (
            self._plan_fetched_at is not None
            and (now - self._plan_fetched_at).total_seconds() < POLL_CACHE_SECONDS
        )

    def _fetch_plan(self, *, force: bool = False) -> dict[str, Any]:
        """Return the price plan, requesting it only when it has expired.

        A plan carries ``next_update``, so one call usually covers a day or more -
        which is what keeps this provider usable with the 60 calls per month of
        the free tier.
        """
        if not force and self._plan_is_fresh():
            return self._plan  # type: ignore[return-value]

        if self.quota.get("quota_remaining") == 0:
            # Do not waste a call that will be rejected; serve the last plan and
            # let the reading flag that it is stale.
            if self._plan is not None:
                LOGGER.warning(
                    "Selectra: quota exhausted, serving the price plan of %s",
                    self._plan_fetched_at,
                )
                return self._plan
            raise SmartmeterConnectionError(
                "The Selectra monthly quota is used up and no price plan has "
                "been read yet."
            )

        if not self.inputs:
            raise SmartmeterQueryError(
                "This Selectra entry has no qualified offer. Re-add the "
                "integration to run the qualification again."
            )

        plan = self._post(PRICES_URL, self.inputs)
        if not isinstance(plan, dict) or not isinstance(plan.get("prices"), list):
            raise SmartmeterQueryError(f"Unexpected price plan response: {plan!r}")

        self._plan = plan
        self._plan_fetched_at = datetime.now(timezone.utc)
        self._plan_next_update = _timestamp(plan.get("next_update"))
        reason = plan.get("requalification_reason")
        if reason and not self._requalification_logged:
            # The offer changed or the stored qualification is stale: the plan is
            # still usable, but the user should run the flow again.
            self._requalification_logged = True
            LOGGER.warning(
                "Selectra: the stored qualification needs to be renewed: %s", reason
            )
        LOGGER.debug(
            "Selectra: %s period(s), next update %s",
            len(plan["prices"]),
            plan.get("next_update"),
        )
        return plan

    # ---------------------------------------------------------------- login

    def login(self):
        """Read the price plan, which is also the token check.

        There is no separate handshake: a plan is the call this provider needs
        anyway, and every call counts against the monthly quota.
        """
        self._fetch_plan(force=True)
        LOGGER.debug("Selectra: token accepted")
        return self

    # ------------------------------------------------------------- metering

    def zaehlpunkte(self) -> list[dict[str, Any]]:
        """Return the qualified offer as a single metering point.

        Selectra has no meters: the "metering point" is the qualified tariff, and
        it is what a tariff price sensor belongs to.
        """
        self._ensure_session()
        plan = self._plan or {}
        periods = plan.get("prices") or []
        label = self.label or "Selectra offer"
        country = str(self.inputs.get("country_code") or "").upper()
        postcode = self.inputs.get("postcode")

        info = {
            "zaehlpunktnummer": f"selectra_{self._identifier()}",
            "zaehlpunktName": label,
            "zaehlpunktAnlagentyp": "TARIFF",
            "device_model": DEVICE_MODEL,
            "smartMeterType": "Electricity planning API",
            "offer_label": label,
            "country_code": country or None,
            "postcode": postcode,
            "currency": plan.get("currency"),
            "periods": len(periods),
            "next_update": plan.get("next_update"),
            "quota_limit": self.quota.get("quota_limit"),
            "quota_remaining": self.quota.get("quota_remaining"),
            "quota_reset": self.quota.get("quota_reset"),
        }
        return [{"zaehlpunkte": [info]}]

    def _identifier(self) -> str:
        """Return a stable identifier of the qualified offer."""
        canonical = json.dumps(self.inputs, sort_keys=True, default=str)
        return hashlib.sha256(canonical.encode()).hexdigest()[:12]

    # ------------------------------------------------------------- readings

    def historical_data(
        self, zaehlpunktnummer: str, date_from=None, date_until=None
    ) -> list[dict[str, Any]]:
        """Return the price of the tariff band that is running now.

        ``date_from``/``date_until`` are ignored: the plan is a projection of the
        coming periods, not a history. All remaining periods are summarised in
        the attributes.
        """
        self._ensure_session()
        plan = self._fetch_plan()
        extra = {
            "quota_limit": self.quota.get("quota_limit"),
            "quota_remaining": self.quota.get("quota_remaining"),
            "quota_reset": self.quota.get("quota_reset"),
        }
        return _reading_from_plan(
            plan,
            now=datetime.now(timezone.utc),
            offer_label=self.label,
            extra={key: value for key, value in extra.items() if value is not None},
        )

    def consumptions(self) -> list[dict[str, Any]]:
        """A price plan has no consumption statistics."""
        return []
