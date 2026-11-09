"""Tests of the Salzburg Netz setup and options dialogs, device removal and their texts."""
import json
import pathlib
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.asm import async_remove_config_entry_device
from custom_components.asm.api.client import get_client
from custom_components.asm.api.client_salzburgnetz import SalzburgNetzClient
from custom_components.asm.api.errors import SmartmeterConnectionError, SmartmeterQueryError
from custom_components.asm.flows.salzburgnetz import metering_point_label as _metering_point_label
from custom_components.asm.const import DOMAIN
from custom_components.asm.coordinator import AustriaSmartMeterCoordinator

OWN = "AT0040000000000000000000000000001"
OTHER = "AT0040000000000000000000000000002"
CREDENTIALS = {"api_key": "key", "gpnr": "10000000"}
SETTINGS = {"reread_days": 5, "history": "none"}
DESCRIBED = {
    OWN: {"zaehlpunktnummer": OWN, "zaehlpunktAnlagentyp": "Photovoltaik Anlage", "geraetNumber": "1ELS0000000009"},
    OTHER: {"zaehlpunktnummer": OTHER},
}
COMPONENT = pathlib.Path(__file__).parents[1] / "custom_components" / DOMAIN


def _client(described: dict | None = None, error: Exception | None = None) -> MagicMock:
    """Return a client that logs in, or fails to, and describes the metering points the API lists."""
    client = MagicMock()
    client.login.side_effect = error
    client.describe_metering_points.return_value = DESCRIBED if described is None else described
    return client


def _load(entry: MockConfigEntry, found: dict | None = None) -> SalzburgNetzClient:
    """Load an entry with a client whose last login listed ``found``; requests are recorded, not sent."""
    client = get_client("salzburgnetz", {**entry.data, **entry.options})
    client._found = {identifier: dict(info) for identifier, info in (DESCRIBED if found is None else found).items()}
    client._post = MagicMock()
    entry.runtime_data = SimpleNamespace(client=client)
    return client


def _fields(result: dict) -> dict:
    return {str(key): (key, value) for key, value in result["data_schema"].schema.items()}


async def _to_settings(hass: HomeAssistant, described: dict | None = None, error: Exception | None = None) -> dict:
    """Start a setup, pick Salzburg Netz and pass the credentials."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"provider": "salzburgnetz"})
    assert result["step_id"] == "credentials"
    assert [str(key) for key in result["data_schema"].schema] == ["api_key", "gpnr"]
    with patch("custom_components.asm.flows.base.get_client", return_value=_client(described, error)) as factory:
        result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDENTIALS)
    assert factory.return_value.zaehlpunkte.call_count == 0
    return result


async def test_setup_offers_checkboxes_with_labels(hass: HomeAssistant) -> None:
    """The listed metering points are checkboxes, all ticked, labelled with what the API says."""
    result = await _to_settings(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "salzburgnetz"
    fields = _fields(result)
    key, selector = fields["metering_points"]
    assert key.default() == [OWN, OTHER]
    config = selector.config
    assert config["multiple"] and config["mode"] == "list"
    assert [option["label"] for option in config["options"]] == [f"{OWN} · Photovoltaik Anlage (1ELS0000000009)", OTHER]
    assert {name: fields[name][0].default() for name in SETTINGS} == {"reread_days": 3, "history": "year"}

    with patch("custom_components.asm.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"metering_points": [OWN], **SETTINGS, "reread_days": 5.0}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Salzburg Netz (10000000)"
    assert result["data"] == {"provider": "salzburgnetz", **CREDENTIALS, "metering_points": OWN, **SETTINGS}
    assert isinstance(result["data"]["reread_days"], int)


async def test_setup_needs_one_metering_point(hass: HomeAssistant) -> None:
    """Unticking all metering points is refused and keeps them unticked."""
    result = await _to_settings(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"metering_points": [], **SETTINGS})
    assert result["errors"] == {"metering_points": "no_metering_points"}
    assert _fields(result)["metering_points"][0].default() == []
    assert _fields(result)["history"][0].default() == "none"


async def test_setup_asks_for_text_when_none_are_listed(hass: HomeAssistant) -> None:
    """Without listed metering points they are typed in, and malformed ones are refused."""
    result = await _to_settings(hass, described={})
    key, validator = _fields(result)["metering_points"]
    assert validator is str
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"metering_points": f"{OWN}, AT123", **SETTINGS})
    assert result["errors"] == {"metering_points": "invalid_metering_points"}
    assert _fields(result)["metering_points"][0].description == {"suggested_value": f"{OWN}, AT123"}
    with patch("custom_components.asm.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"metering_points": f" {OWN} ", **SETTINGS})
    assert result["data"]["metering_points"] == OWN


async def test_setup_goes_on_when_the_listing_is_not_answered(hass: HomeAssistant) -> None:
    """A listing that is refused or not answered leaves the metering points to be typed in, and says so."""
    result = await _to_settings(hass, error=SmartmeterConnectionError("HTTP 429"))
    assert result["step_id"] == "salzburgnetz"
    assert result["errors"] == {"base": "metering_points_not_listed"}
    assert _fields(result)["metering_points"][1] is str
    with patch("custom_components.asm.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"metering_points": OWN, **SETTINGS})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {"provider": "salzburgnetz", **CREDENTIALS, "metering_points": OWN, **SETTINGS}


@pytest.mark.parametrize("status", [400, 403, 404, 422])
async def test_setup_stops_when_the_customer_number_is_refused(hass: HomeAssistant, status: int) -> None:
    """A refusal of the customer number is shown on the credentials form as such."""
    result = await _to_settings(hass, error=SmartmeterQueryError(f"HTTP {status}"))
    assert result["step_id"] == "credentials"
    assert result["errors"] == {"base": "customer_number_refused"}


def test_label_parts() -> None:
    """A label shows name, type and address where known, and the meter in brackets."""
    info = {"zaehlpunktName": "Haus", "zaehlpunktAnlagentyp": "Bezug", "geraetNumber": "123",
            "verbrauchsstelle": {"strasse": "Musterstraße 1", "postleitzahl": "1234", "ort": "Musterort"}}
    assert _metering_point_label(OWN, info) == f"{OWN} · Haus · Bezug · Musterstraße 1, 1234 Musterort (123)"
    assert _metering_point_label(OWN, {"verbrauchsstelle": {"ort": "Musterort"}}) == f"{OWN} · Musterort"


ENDED = {"sparte": "Strom", "produkt": "Netz", "adresse": "AT 1234 Musterort Musterstraße 2", "beginn": "2023-09-21", "ende": "2023-11-28", "aktiv": False}
CURRENT = {"sparte": "Strom", "anlagenart": "Photovoltaik Anlage", "produkt": "Einspeiser", "adresse": "AT 1234 Musterort Musterstraße 1", "beginn": "2023-11-29", "aktiv": True}


def test_label_of_contracts() -> None:
    """The current contracts give the label, with all their meters; an ended metering point shows its last period."""
    info = {"zaehlpunktAnlagentyp": "Photovoltaik Anlage", "aktiv": True,
            "vertraege": [ENDED, {**CURRENT, "geraet": "A1"}, {**CURRENT, "geraet": "B2"}]}
    assert _metering_point_label(OWN, info) == f"{OWN} · Strom · Photovoltaik Anlage · Einspeiser · AT 1234 Musterort Musterstraße 1 (A1, B2)"
    earlier = {**ENDED, "beginn": "2011-09-01", "ende": "2013-07-28"}
    info = {"aktiv": False, "vertraege": [ENDED, earlier]}
    assert _metering_point_label(OWN, info) == f"{OWN} · Strom · Netz · AT 1234 Musterort Musterstraße 2 · 2023-09-21 – 2023-11-28"
    assert _metering_point_label(OWN, {"vertraege": [{"sparte": "Gas"}]}) == f"{OWN} · Gas"


async def test_setup_lists_ended_metering_points_last_and_unticked(hass: HomeAssistant) -> None:
    """Metering points whose contracts have all ended come last and are not ticked."""
    described = {
        OTHER: {"zaehlpunktnummer": OTHER, "aktiv": False, "vertraege": [ENDED]},
        OWN: {"zaehlpunktnummer": OWN, "aktiv": True, "vertraege": [CURRENT]},
    }
    result = await _to_settings(hass, described=described)
    key, selector = _fields(result)["metering_points"]
    assert key.default() == [OWN]
    assert [option["value"] for option in selector.config["options"]] == [OWN, OTHER]


async def test_setup_ticks_none_when_all_contracts_have_ended(hass: HomeAssistant) -> None:
    """Metering points of ended contracts stay unticked even when there are no others."""
    described = {OTHER: {"zaehlpunktnummer": OTHER, "aktiv": False, "vertraege": [ENDED]}}
    result = await _to_settings(hass, described=described)
    assert _fields(result)["metering_points"][0].default() == []


def _entry(hass: HomeAssistant, provider: str = "salzburgnetz", options: dict | None = None, **data) -> MockConfigEntry:
    """Add a config entry of a provider."""
    entry = MockConfigEntry(domain=DOMAIN, data={"provider": provider, **data}, options=options, unique_id=f"{provider}_test")
    entry.add_to_hass(hass)
    return entry


async def test_options_checkboxes_of_a_loaded_entry(hass: HomeAssistant) -> None:
    """A loaded entry offers its known metering points without a request, ticked as stored."""
    entry = _entry(hass, **CREDENTIALS, metering_points=OWN, **SETTINGS)
    client = _load(entry)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "salzburgnetz"
    assert client._post.call_count == 0
    fields = _fields(result)
    assert fields["metering_points"][0].default() == [OWN]
    assert {name: fields[name][0].default() for name in SETTINGS} == SETTINGS
    assert fields["scan_interval"][0].default() == 360

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"metering_points": [OWN, OTHER], "reread_days": 7, "history": "month", "scan_interval": 120},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {
        "metering_points": f"{OWN}, {OTHER}", "reread_days": 7, "history": "month", "scan_interval": 120,
    }


async def test_options_limits(hass: HomeAssistant) -> None:
    """A stored re-read window below the minimum is shown at the minimum and refused."""
    entry = _entry(hass, **CREDENTIALS, metering_points=OWN, **{**SETTINGS, "reread_days": 1})
    _load(entry)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert _fields(result)["reread_days"][0].default() == 2
    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(result["flow_id"], {"metering_points": [OWN], **SETTINGS, "reread_days": 1, "scan_interval": 60})


async def test_options_tick_all_when_none_are_stored(hass: HomeAssistant) -> None:
    """An entry that reads all listed metering points shows all of them ticked."""
    entry = _entry(hass, **CREDENTIALS, metering_points="")
    _load(entry)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert _fields(result)["metering_points"][0].default() == [OWN, OTHER]


@pytest.mark.parametrize("loaded", [False, True])
async def test_options_saved_without_the_listing(hass: HomeAssistant, loaded: bool) -> None:
    """Options saved while the facilities are not listed store the metering points given."""
    entry = _entry(hass, **CREDENTIALS, metering_points=OWN)
    if loaded:
        _load(entry, found={})
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"metering_points": [OWN] if loaded else OWN, **SETTINGS, "scan_interval": 360}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {"metering_points": OWN, **SETTINGS, "scan_interval": 360}


async def test_options_text_of_an_entry_not_loaded(hass: HomeAssistant) -> None:
    """An entry that is not loaded asks for the metering points as text and checks them."""
    entry = _entry(hass, **CREDENTIALS, metering_points=OWN)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert _fields(result)["metering_points"][0].description == {"suggested_value": OWN}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"metering_points": "AT123", **SETTINGS, "scan_interval": 360}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"metering_points": "invalid_metering_points"}
    assert _fields(result)["metering_points"][0].description == {"suggested_value": "AT123"}
    assert _fields(result)["history"][0].default() == "none"


async def test_options_of_other_providers_are_unchanged(hass: HomeAssistant) -> None:
    """Other providers keep the scan interval as their only option."""
    entry = _entry(hass, provider="awattar", market_area="at")
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "init"
    assert [str(key) for key in result["data_schema"].schema] == ["scan_interval"]


async def test_coordinator_prefers_the_options(hass: HomeAssistant) -> None:
    """A setting changed in the options wins over the one from the setup."""
    entry = _entry(hass, **CREDENTIALS, metering_points=OWN, **SETTINGS)
    hass.config_entries.async_update_entry(entry, options={"metering_points": OTHER, "reread_days": 2})
    coordinator = AustriaSmartMeterCoordinator(hass, entry)
    client = coordinator.client
    assert (client.metering_points, client.reread_days, client.history) == ([OTHER], 2, "none")


async def test_device_removal_only_for_unread_metering_points(hass: HomeAssistant) -> None:
    """A device can be deleted once its metering point is no longer read."""
    entry = _entry(hass, **CREDENTIALS)
    entry.runtime_data = SimpleNamespace(data={OWN: {}})
    read = SimpleNamespace(identifiers={(DOMAIN, OWN)})
    unread = SimpleNamespace(identifiers={(DOMAIN, OTHER)})
    assert not await async_remove_config_entry_device(hass, entry, read)
    assert await async_remove_config_entry_device(hass, entry, unread)


async def test_device_removal_of_an_entry_not_loaded(hass: HomeAssistant) -> None:
    """An entry that is not loaded reads nothing, so its devices can be deleted."""
    entry = _entry(hass, **CREDENTIALS)
    assert await async_remove_config_entry_device(hass, entry, SimpleNamespace(identifiers={(DOMAIN, OWN)}))


def _strings(name: str) -> dict:
    return json.loads((COMPONENT / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", ["strings.json", *(f"translations/{code}.json" for code in ("en", "de", "es", "fr", "it"))])
def test_every_setting_is_explained(name: str) -> None:
    """Each language labels and explains every Salzburg Netz field and its errors."""
    strings = _strings(name)
    steps = [strings["config"]["step"]["salzburgnetz"], strings["options"]["step"]["salzburgnetz"]]
    for step in steps:
        assert step["title"] and step["description"] and "{" not in step["description"]
        assert set(step["data"]) == set(step["data_description"])
        assert all(step["data"].values()) and all(step["data_description"].values())
    assert set(steps[0]["data"]) == {"metering_points", *SETTINGS}
    assert set(steps[1]["data"]) == {"metering_points", "scan_interval", *SETTINGS}
    credentials = strings["config"]["step"]["credentials"]
    assert "metering_points" not in credentials["data"]
    assert set(credentials["data_description"]) == {"gpnr"}
    assert set(strings["selector"]) == {"history"}
    assert set(strings["selector"]["history"]["options"]) == {"none", "month", "year", "maximum"}
    for errors in (strings["config"]["error"], strings["options"]["error"]):
        assert errors["invalid_metering_points"] and errors["no_metering_points"]
    assert strings["config"]["error"]["metering_points_not_listed"]
    assert strings["config"]["error"]["customer_number_refused"]


def test_english_translation_matches_strings() -> None:
    """translations/en.json is the copy of strings.json."""
    assert _strings("strings.json") == _strings("translations/en.json")
