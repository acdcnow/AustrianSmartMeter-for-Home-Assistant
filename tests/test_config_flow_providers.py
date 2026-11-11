"""Characterisation tests of every provider's setup and options, for the flow split per provider."""
from hashlib import sha256
from unittest.mock import MagicMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.asm.api.errors import SmartmeterError, SmartmeterLoginError
from custom_components.asm.const import DOMAIN

CONTRACTS = [{"zaehlpunkte": [{"zaehlpunktnummer": "AT0040000000000000000000000000001"}]}]


def _hashed(provider: str, identifier: str) -> str:
    return f"{provider}_{sha256(identifier.encode()).hexdigest()[:16]}"


# Each case holds the provider, its form fields with their defaults, the input, and the
# title, data and unique id of the entry.
CASES = [
    ("wiener_netze", {"username": None, "password": None}, {"username": "Me@Example.at", "password": "pw"},
     "Wiener Netze (Me@Example.at)", {"username": "Me@Example.at", "password": "pw"}, "wiener_netze_me@example.at"),
    ("netz_noe", {"username": None, "password": None}, {"username": "me", "password": "pw"},
     "Netz Niederösterreich (EVN) (me)", {"username": "me", "password": "pw"}, "netz_noe_me"),
    ("ewerk_goesting", {"username": None, "password": None}, {"username": "me", "password": "pw"},
     "E-Werk Gösting (me)", {"username": "me", "password": "pw"}, "ewerk_goesting_me"),
    ("energylive", {"api_key": None}, {"api_key": " secretkey1234 "},
     "energyLIVE (smartENERGY) (…1234)", {"api_key": "secretkey1234"}, _hashed("energylive", "secretkey1234")),
    ("energiedaten_at", {"api_key": None}, {"api_key": "abcdefgh"},
     "energiedaten.at (API key) (…efgh)", {"api_key": "abcdefgh"}, _hashed("energiedaten_at", "abcdefgh")),
    ("dsmr", {"port": None, "dsmr_version": "5", "encryption_key": ""}, {"port": " socket://meter:23 ", "dsmr_version": "5", "encryption_key": " key "},
     "DSMR / P1 meter (local) (socket://meter:23)", {"port": "socket://meter:23", "dsmr_version": "5", "encryption_key": "key"},
     _hashed("dsmr", "socket://meter:23")),
    ("awattar", {"market_area": "AT"}, {"market_area": "DE"},
     "aWATTar market prices (Germany)", {"market_area": "DE"}, "awattar_de"),
]


def _client(contracts=CONTRACTS) -> MagicMock:
    client = MagicMock()
    client.zaehlpunkte.return_value = contracts
    return client


async def _credentials(hass: HomeAssistant, provider: str) -> dict:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["step_id"] == "user"
    return await hass.config_entries.flow.async_configure(result["flow_id"], {"provider": provider})


def _defaults(result: dict) -> dict:
    return {str(key): (key.default() if callable(key.default) else None) for key in result["data_schema"].schema}


@pytest.mark.parametrize(("provider", "fields", "user_input", "title", "data", "unique_id"), CASES)
async def test_setup(hass: HomeAssistant, provider, fields, user_input, title, data, unique_id) -> None:
    """Each provider asks for its credentials, checks them and creates its entry."""
    result = await _credentials(hass, provider)
    assert result["step_id"] == "credentials"
    assert result["description_placeholders"]["provider_name"] == title.rsplit(" (", 1)[0]
    assert _defaults(result) == fields
    with patch("custom_components.asm.flows.base.get_client", return_value=_client()) as factory, \
            patch("custom_components.asm.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == title
    assert result["data"] == {"provider": provider, **data}
    assert result["result"].unique_id == unique_id
    assert factory.call_args.args == (provider, {"provider": provider, **data})
    factory.return_value.login.assert_called_once()


@pytest.mark.parametrize(
    ("raised", "contracts", "error"),
    [(SmartmeterLoginError("no"), CONTRACTS, "invalid_auth"), (SmartmeterError("down"), CONTRACTS, "cannot_connect"),
     (ValueError("bug"), CONTRACTS, "unknown"), (None, [], "no_contracts")],
)
async def test_setup_errors(hass: HomeAssistant, raised, contracts, error) -> None:
    """A failed check shows the credentials form again with the reason."""
    result = await _credentials(hass, "wiener_netze")
    client = _client(contracts)
    client.login.side_effect = raised
    with patch("custom_components.asm.flows.base.get_client", return_value=client):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"username": "me", "password": "pw"})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "credentials"
    assert result["errors"] == {"base": error}


async def test_setup_aborts_when_configured(hass: HomeAssistant) -> None:
    """The same account cannot be added twice."""
    MockConfigEntry(domain=DOMAIN, data={"provider": "netz_noe"}, unique_id="netz_noe_me").add_to_hass(hass)
    result = await _credentials(hass, "netz_noe")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"username": "me", "password": "pw"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_selectra_questionnaire(hass: HomeAssistant) -> None:
    """Selectra qualifies the tariff in rounds of questions before the entry is created."""
    result = await _credentials(hass, "selectra")
    assert _defaults(result) == {"token": None, "country_code": "at", "postcode": ""}
    client = MagicMock()
    client.qualify.side_effect = [
        {"questions": [{"field": "tariff", "label": "Tariff", "type": "select",
                        "options": [{"value": 7, "label": "Green"}, {"value": 8, "label": "Grey"}]},
                       {"field": "usage", "type": "input"}]},
        {"done": True, "inputs": {"tariff": 7, "usage": "3500"}},
    ]
    client.zaehlpunkte.return_value = CONTRACTS
    with patch("custom_components.asm.flows.base.get_client", return_value=client) as factory, \
            patch("custom_components.asm.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"token": " tok ", "country_code": "AT", "postcode": "1234"})
        assert result["step_id"] == "qualification"
        assert result["description_placeholders"] == {"question_list": "- Tariff\n- usage"}
        assert sorted(_defaults(result)) == ["tariff", "usage"]
        assert client.qualify.call_args_list[0].args == ({"country_code": "at", "postcode": "1234"},)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"tariff": "7", "usage": " 3500 "})
    assert client.qualify.call_args_list[1].args == ({"country_code": "at", "postcode": "1234", "tariff": 7, "usage": "3500"},)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Selectra tariff planning (Green · AT 1234)"
    inputs = {"tariff": 7, "usage": "3500"}
    assert result["data"] == {"provider": "selectra", "token": "tok", "inputs": inputs, "offer_label": "Green · AT 1234"}
    assert result["result"].unique_id == _hashed("selectra", '{"tariff": 7, "usage": "3500"}')
    assert factory.call_args_list[0].args == ("selectra", {"provider": "selectra", "token": "tok"})


async def test_selectra_rejects_a_bad_country(hass: HomeAssistant) -> None:
    """A country code must be two letters; the token form is shown again."""
    result = await _credentials(hass, "selectra")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"token": "tok", "country_code": "aut", "postcode": ""})
    assert result["step_id"] == "credentials"
    assert result["errors"] == {"country_code": "invalid_input"}


async def test_options_scan_interval(hass: HomeAssistant) -> None:
    """Providers without own options offer the scan interval."""
    entry = MockConfigEntry(domain=DOMAIN, data={"provider": "wiener_netze", "scan_interval": 30}, unique_id="wn")
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "init"
    assert _defaults(result) == {"scan_interval": 60}
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"scan_interval": 90})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {"scan_interval": 90}


def test_every_provider_has_its_flow() -> None:
    """Each selectable provider has a module in flows; an unknown one gets the portal login."""
    from custom_components.asm.const import PROVIDERS
    from custom_components.asm.flows import PROVIDER_FLOWS, ProviderFlow, provider_flow

    assert set(PROVIDER_FLOWS) == set(PROVIDERS)
    for provider, flow in PROVIDER_FLOWS.items():
        assert flow.provider == provider
        assert type(flow).__module__ != "custom_components.asm.flows.base"
    fallback = provider_flow("stromnetz_graz")
    assert type(fallback) is ProviderFlow and fallback.unique_id("Me") == "stromnetz_graz_me"


def test_providers_add_their_steps() -> None:
    """The steps of the provider mixins are steps of the config and the options flow."""
    from custom_components.asm.config_flow import AustriaSmartMeterConfigFlow, AustriaSmartMeterOptionsFlow

    assert callable(AustriaSmartMeterConfigFlow.async_step_qualification)
    assert callable(AustriaSmartMeterConfigFlow.async_step_salzburgnetz)
    assert callable(AustriaSmartMeterOptionsFlow.async_step_salzburgnetz)
