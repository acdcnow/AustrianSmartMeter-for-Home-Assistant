"""Tests of the energyLIVE client: cached device list, unread gateways, refusals."""
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from unittest.mock import MagicMock

import pytest
from requests.structures import CaseInsensitiveDict

from custom_components.asm.api import client_energylive as module
from custom_components.asm.api.client_energylive import EnergyliveClient
from custom_components.asm.api.errors import (
    SmartmeterLoginError,
    SmartmeterNotFoundError,
    SmartmeterRateLimitError,
)

INTERFACE = "I-10082023-01658401"
GATEWAY = "G-10082023-01658400"


def _response(status: int = 200, headers: dict | None = None, payload=None) -> MagicMock:
    """Return a response mock in the shape ``_get_json`` reads."""
    response = MagicMock(
        status_code=status, headers=CaseInsensitiveDict(headers or {}), text=""
    )
    response.json.return_value = payload
    return response


def _client(*responses: MagicMock) -> EnergyliveClient:
    """Return a client whose key counts as checked today."""
    client = EnergyliveClient("key")
    client._login_time = datetime.now()
    client.session.request = MagicMock(side_effect=list(responses))
    return client


def _listing() -> list[MagicMock]:
    """Return the answers of one device listing: two devices and their keys."""
    return [
        _response(payload=[INTERFACE, GATEWAY]),
        _response(payload={"id": INTERFACE, "serial": "12345678"}),
        _response(payload=["0100010800", "0100010700"]),
        _response(payload={"id": GATEWAY, "serial": "87654321"}),
        _response(payload=["batteryVoltage", "loraRssi"]),
    ]


def test_the_device_list_is_read_once() -> None:
    """A second poll reuses the list, the details and the measurement keys."""
    client = _client(*_listing())
    first = client.zaehlpunkte()
    assert client.session.request.call_count == 5
    assert [info["zaehlpunktnummer"] for info in first[0]["zaehlpunkte"]] == [
        INTERFACE,
        GATEWAY,
    ]
    assert client.zaehlpunkte() is first
    assert client.session.request.call_count == 5


def test_readings_of_the_interface_cost_one_request() -> None:
    """After the listing, a poll reads the latest values with one request."""
    client = _client(
        *_listing(),
        _response(
            payload=[
                {"measurement": "0100010700", "timestamp": 1_700_000_000_000, "value": 1234}
            ]
        ),
    )
    client.zaehlpunkte()
    readings = client.historical_data(INTERFACE)
    assert client.session.request.call_count == 6
    assert [reading["obisCode"] for reading in readings] == ["1-1:1.7.0"]
    assert readings[0]["messwerte"][0]["messwert"] == 1234.0


def test_a_gateway_is_not_asked_for_readings() -> None:
    """A device that reports no register is skipped before the request."""
    client = _client(*_listing())
    client.zaehlpunkte()
    assert client.historical_data(GATEWAY) == []
    assert client.session.request.call_count == 5


def test_a_vanished_device_drops_the_cached_list() -> None:
    """A 404 on a reading makes the next poll list the devices again."""
    client = _client(
        *_listing(),
        _response(status=404),
        _response(payload=[INTERFACE]),
        _response(payload={"id": INTERFACE, "serial": "12345678"}),
        _response(payload=["0100010800"]),
    )
    client.zaehlpunkte()
    with pytest.raises(SmartmeterNotFoundError):
        client.historical_data(INTERFACE)
    assert client.session.request.call_count == 6

    client.zaehlpunkte()
    assert client.session.request.call_count == 9


def test_a_refusal_names_the_pause() -> None:
    """HTTP 429 is a rate limit that carries the pause it asks for."""
    client = _client(_response(status=429, headers={"Retry-After": "90"}))
    with pytest.raises(SmartmeterRateLimitError) as raised:
        client.zaehlpunkte()
    assert raised.value.retry_after == 90


def test_an_http_date_is_understood() -> None:
    """Retry-After may name a moment instead of a number of seconds."""
    moment = datetime.now(timezone.utc) + timedelta(seconds=120)
    client = _client(
        _response(status=429, headers={"Retry-After": format_datetime(moment, usegmt=True)})
    )
    with pytest.raises(SmartmeterRateLimitError) as raised:
        client.zaehlpunkte()
    assert raised.value.retry_after == pytest.approx(120, abs=5)


@pytest.mark.parametrize(
    ("headers", "error"),
    [
        # A 403 that names a pause is a refusal, not a revoked key.
        ({"X-RateLimit-Remaining": "0"}, SmartmeterRateLimitError),
        ({"Retry-After": "60"}, SmartmeterRateLimitError),
        ({"Server": "nginx"}, SmartmeterLoginError),
    ],
)
def test_only_a_plain_403_is_a_rejected_key(headers: dict, error: type[Exception]) -> None:
    """A throttle behind a 403 does not start a reauth flow."""
    client = _client(_response(status=403, headers=headers))
    with pytest.raises(error):
        client.zaehlpunkte()


def test_the_retry_after_helper_ignores_a_meaningless_value() -> None:
    """An unreadable Retry-After leaves the pause to the coordinator."""
    assert module._retry_after(_response(headers={"Retry-After": "soon"})) is None
    assert module._retry_after(_response()) is None
