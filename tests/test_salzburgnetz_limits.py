"""Tests of the Salzburg Netz meter details, the pause after an HTTP 429, and sensors added by a later poll."""
import logging
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from requests.structures import CaseInsensitiveDict

from custom_components.asm.api import client_salzburgnetz as module
from custom_components.asm.api.client_salzburgnetz import SalzburgNetzClient
from custom_components.asm.api.errors import SmartmeterConnectionError
from custom_components.asm.const import DOMAIN
from custom_components.asm.sensor import async_setup_entry as async_setup_sensors

OWN = "AT0040000000000000000000000000001"


def _device(number: str, kind: str, smart: str) -> dict:
    """Return a device record in the shape of an /equipment answer."""
    return {"Vertrag": "0000000001", "Anlage": "0030000001", "Zählpunkt": OWN, "Gerätenummer": number,
            "Geräteart": kind, "Smarter_Zähler": smart, "Ist_smart": smart, "Smart-Typ": "Prime Meter (IME)",
            "Granularität_Auslesung": "QH" if smart else "", "15min_Zustimmung_seit": "00.00.0000"}


def test_the_meter_is_taken_among_the_devices() -> None:
    """The smart meter wins over a tariff clock listed before it."""
    payload = {"PartnerNr": "0010000000", "Vertraege": [_device("000000000000000042", "TRE/Uhr", ""), _device("1ELS0000000001", "Zähler", "X")]}
    assert module._equipment_info(payload) == {"geraetNumber": "1ELS0000000001", "smartMeterType": "Prime Meter (IME)"}
    payload["Vertraege"][1]["Ist_smart"] = payload["Vertraege"][1]["Smarter_Zähler"] = ""
    assert module._equipment_info(payload)["geraetNumber"] == "1ELS0000000001"
    payload["Vertraege"][1]["Geräteart"] = "Wandler"
    assert module._equipment_info(payload)["geraetNumber"] == "000000000000000042"


def _response(status: int, headers: dict | None = None, payload=None) -> MagicMock:
    response = MagicMock(status_code=status, headers=CaseInsensitiveDict(headers or {}), text="")
    response.json.return_value = payload if payload is not None else {"status": status, "detail": "Maximale Anzahl an Abfragen je 24 Stunden überschritten."}
    return response


def _client(*responses) -> SalzburgNetzClient:
    client = SalzburgNetzClient("key", "10000000", OWN)
    client.session.request = MagicMock(side_effect=list(responses))
    return client


@pytest.mark.parametrize(
    ("headers", "pause"),
    [
        ({}, module.THROTTLE_PAUSE),
        ({"Retry-After": "90"}, timedelta(seconds=90)),
        ({"Retry--After": "22540"}, timedelta(seconds=22540)),
        ({"Retry-After": "200000"}, module.THROTTLE_PAUSE_MAX),
        ({"Retry-After": "soon"}, module.THROTTLE_PAUSE),
    ],
)
def test_pause_after_a_refusal(headers: dict, pause: timedelta, caplog: pytest.LogCaptureFixture) -> None:
    """A refusal pauses its category only, briefly or as long as Retry-After asks."""
    client = _client(_response(429, {**headers, "X-RateLimit-Remaining": "0", "Server": "nginx"}), _response(201, payload=[]), _response(201, payload=[]))
    before = datetime.now(timezone.utc)
    with caplog.at_level(logging.WARNING), pytest.raises(SmartmeterConnectionError, match="too many requests"):
        client._post(module.EQUIPMENT_URL, {})
    until = client._throttled_until[module.EQUIPMENT_URL]
    assert before + pause <= until <= datetime.now(timezone.utc) + pause
    assert "Maximale Anzahl" in caplog.text and "X-RateLimit-Remaining" in caplog.text and "nginx" not in caplog.text

    with pytest.raises(SmartmeterConnectionError, match="not requested"):
        client._post(module.EQUIPMENT_URL, {})
    assert client.session.request.call_count == 1
    assert client._post(module.PROFILE_URLS["profile_eg"], {}) == []
    client._throttled_until[module.EQUIPMENT_URL] = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert client._post(module.EQUIPMENT_URL, {}) == []
    assert client.session.request.call_count == 3


def test_a_short_pause_is_waited_out(caplog: pytest.LogCaptureFixture) -> None:
    """A refusal that asks for at most a minute is waited out and the request sent again, without a warning."""
    client = _client(_response(429, {"Retry--After": "41"}), _response(201, payload=[]))
    with caplog.at_level(logging.WARNING), patch.object(module.time, "sleep") as sleep:
        assert client._post(module.PROFILE_URLS["profile_eg"], {}) == []
    assert client.session.request.call_count == 2
    assert sleep.call_count == 1 and 41 <= sleep.call_args.args[0] <= 42
    assert "refused" not in caplog.text


def test_a_request_during_a_short_pause_waits() -> None:
    """A request that falls into a short pause left by an earlier refusal is sent once it is over."""
    client = _client(_response(201, payload=[]))
    client._throttled_until[module.EQUIPMENT_URL] = datetime.now(timezone.utc) + timedelta(seconds=30)
    with patch.object(module.time, "sleep") as sleep:
        assert client._post(module.EQUIPMENT_URL, {}) == []
    assert sleep.call_count == 1 and 30 <= sleep.call_args.args[0] <= 31
    assert client.session.request.call_count == 1


def test_waiting_gives_up_after_the_attempts() -> None:
    """A request refused again after every wait gives up after THROTTLE_WAIT_ATTEMPTS waits."""
    refusals = [_response(429, {"Retry--After": "10"}) for _ in range(module.THROTTLE_WAIT_ATTEMPTS + 1)]
    client = _client(*refusals)
    with patch.object(module.time, "sleep") as sleep, pytest.raises(SmartmeterConnectionError, match="too many requests"):
        client._post(module.EQUIPMENT_URL, {})
    assert sleep.call_count == module.THROTTLE_WAIT_ATTEMPTS
    assert client.session.request.call_count == module.THROTTLE_WAIT_ATTEMPTS + 1


def test_a_load_profile_pauses_per_metering_point() -> None:
    """A refused load profile pauses only its metering point; a refused /equipment the whole customer number."""
    other = "AT0040000000000000000000000000002"
    url = module.PROFILE_URLS["profile_eg"]
    refusal = {"Retry--After": "3600"}
    client = _client(_response(429, refusal), _response(201, payload=[]), _response(429, refusal))
    with pytest.raises(SmartmeterConnectionError, match="too many requests"):
        client._post(url, {"ZP": OWN})
    assert client._post(url, {"ZP": other}) == []
    with pytest.raises(SmartmeterConnectionError, match=f"{OWN} not requested"):
        client._post(url, {"ZP": OWN})
    with pytest.raises(SmartmeterConnectionError, match="too many requests"):
        client._post(module.EQUIPMENT_URL, {"ZP": OWN})
    with pytest.raises(SmartmeterConnectionError, match="equipment not requested"):
        client._post(module.EQUIPMENT_URL, {"ZP": other})
    assert client.session.request.call_count == 3


def test_retry_after_as_a_date() -> None:
    """Retry-After can also name the moment."""
    now = datetime(2026, 11, 7, 18, 0, tzinfo=timezone.utc)
    later = format_datetime(now + timedelta(minutes=3), usegmt=True)
    assert module._throttle_pause(CaseInsensitiveDict({"Retry-After": later}), now) == timedelta(minutes=3)


def _records(value: str) -> list[dict]:
    return [{"Datum": "05.11.2026", "Uhrzeit": "00:00:00", "UTC": "+1", "Zählpunktbezeichnung": OWN, "OBIS": "1-1:1.9.0 G.01",
             "ID_der_Energiegemeinschaft": "", "Wert": value, "Einheit": "kWh", "Qualität_Beschreibung": "Wert ist gültig(L1)"}]


async def test_value_sensors_come_with_a_later_poll(hass: HomeAssistant) -> None:
    """A poll without readings creates the diagnostics; the next one with readings adds the value sensor, once."""
    info = {"zaehlpunktnummer": OWN, "geschaeftspartner": "Salzburg Netz GmbH"}
    coordinator = MagicMock(provider="salzburgnetz", data={OWN: {"info": info, "readings": [], "stats": {}}})
    entry = MockConfigEntry(domain=DOMAIN, data={"provider": "salzburgnetz"})
    entry.add_to_hass(hass)
    entry.runtime_data = coordinator
    added: list[list] = []
    await async_setup_sensors(hass, entry, lambda entities: added.append(list(entities)))
    listener = coordinator.async_add_listener.call_args.args[0]
    assert [entity.unique_id for entity in added[0]] == [f"{OWN}_diag_zaehlpunktnummer", f"{OWN}_diag_customer_id"]

    listener()
    assert len(added) == 1
    readings = module._readings_from_profile(_records("0,5"), OWN, "Smart Meter")
    coordinator.data = {OWN: {"info": info, "readings": readings, "stats": {}}}
    listener()
    assert [entity.unique_id for entity in added[1]] == [f"{OWN}_1-1:1.9.0"]
    coordinator.data = {OWN: {"info": info, "readings": module._readings_from_profile(_records("0,7"), OWN, "Smart Meter"), "stats": {}}}
    listener()
    assert len(added) == 2


def test_a_refused_equipment_read_leaves_the_load_profile(caplog: pytest.LogCaptureFixture) -> None:
    """An update whose /equipment is refused still reads the load profile."""
    client = SalzburgNetzClient("key", "10000000", OWN)
    client._validated_on = module._today()

    def answer(method, url, **kwargs):
        if url == module.EQUIPMENT_URL:
            return _response(429, {"Retry--After": "22540"})
        return _response(201, payload=_records("0,5"))

    client.session.request = MagicMock(side_effect=answer)
    with caplog.at_level(logging.WARNING):
        metering_points = client.zaehlpunkte()[0]["zaehlpunkte"]
        readings = client.historical_data(zaehlpunktnummer=OWN)
    assert [point["zaehlpunktnummer"] for point in metering_points] == [OWN]
    assert "geraetNumber" not in metering_points[0]
    assert [reading["obisCode"] for reading in readings] == ["1-1:1.9.0"]
    assert [call.args[1] for call in client.session.request.call_args_list] == [module.EQUIPMENT_URL, module.PROFILE_URLS["profile_eg"], module.PROFILE_URLS["profile"]]


def test_a_pause_outlives_the_client() -> None:
    """A reload's new client keeps the pause of its customer number; another customer number has none."""
    client = _client(_response(429, {"Retry--After": "22540"}))
    with pytest.raises(SmartmeterConnectionError, match="too many requests"):
        client._post(module.EQUIPMENT_URL, {})
    reloaded = _client()
    with pytest.raises(SmartmeterConnectionError, match="not requested"):
        reloaded._post(module.EQUIPMENT_URL, {})
    assert reloaded.session.request.call_count == 0
    other = SalzburgNetzClient("key", "20000000", OWN)
    other.session.request = MagicMock(return_value=_response(201, payload=[]))
    assert other._post(module.EQUIPMENT_URL, {}) == []


ANLAGE = {"PartnerNr": "0010000000", "Vertraege": [{"Zählpunkt": OWN, "Sparte": "Strom", "Anlage_ist_aktiv": "X"}]}


def test_a_failed_listing_is_tried_again_after_its_pause() -> None:
    """With entered metering points, a refused /anlage keeps the last listing and is not sent again within the pause."""
    client = _client(_response(201, payload=ANLAGE))
    client.login()
    yesterday = module._today() - timedelta(days=1)
    client._validated_on = yesterday

    def answer(method, url, **kwargs):
        if url == module.ANLAGE_URL:
            return _response(429, {"Retry-After": "3600"})
        return _response(201, payload=[])

    client.session.request = MagicMock(side_effect=answer)
    client.login()
    assert client._validated_on == yesterday
    assert client._found[OWN]["vertraege"][0]["sparte"] == "Strom"
    assert client._listing_retry_at == client._throttled_until[module.ANLAGE_URL]
    assert client.is_logged_in()
    client.zaehlpunkte()
    client.historical_data(zaehlpunktnummer=OWN)
    assert [call.args[1] for call in client.session.request.call_args_list].count(module.ANLAGE_URL) == 1

    client._listing_retry_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    client._throttled_until.clear()
    assert not client.is_logged_in()
    client.session.request = MagicMock(return_value=_response(201, payload=ANLAGE))
    client.zaehlpunkte()
    assert client._validated_on == module._today() and client._listing_retry_at is None


def test_a_failed_listing_without_a_pause_waits_briefly() -> None:
    """A listing that is not answered is tried again after THROTTLE_PAUSE."""
    client = _client(_response(503))
    before = datetime.now(timezone.utc)
    client.login()
    assert before + module.THROTTLE_PAUSE <= client._listing_retry_at <= datetime.now(timezone.utc) + module.THROTTLE_PAUSE
    assert client._found == {}


def test_device_details_are_read_again_after_a_refusal() -> None:
    """A refused /equipment read is not cached and keeps the last details; a rejected one is cached for the day."""
    client = _client()
    client._validated_on = module._today()
    meter = {"PartnerNr": "0010000000", "Vertraege": [_device("1ELS0000000001", "Zähler", "X")]}
    client._device_details[OWN] = (module._today() - timedelta(days=1), {"geraetNumber": "1ELS0000000000"})
    client.session.request = MagicMock(side_effect=[_response(429, {"Retry--After": "3600"}), _response(201, payload=meter), _response(400)])
    assert client._device_details_for(OWN) == {"geraetNumber": "1ELS0000000000"}
    assert client._device_details_for(OWN) == {"geraetNumber": "1ELS0000000000"}
    assert client.session.request.call_count == 1

    client._throttled_until.clear()
    assert client._device_details_for(OWN)["geraetNumber"] == "1ELS0000000001"
    assert client._device_details_for(OWN)["geraetNumber"] == "1ELS0000000001"
    assert client.session.request.call_count == 2

    client._device_details.clear()
    assert client._device_details_for(OWN) == {}
    assert client._device_details_for(OWN) == {}
    assert client.session.request.call_count == 3
