"""Tests of the Salzburg Netz client: metering points, both load profiles, re-read window and history."""
from datetime import date
from unittest.mock import patch

import pytest

from custom_components.asm.api import client_salzburgnetz as module
from custom_components.asm.api.client import get_client
from custom_components.asm.api.client_salzburgnetz import SalzburgNetzClient

TODAY = date(2026, 11, 7)
OWN = "AT0040000000000000000000000000001"
OTHER = "AT0040000000000000000000000000002"
COMMUNITY = "AT00400000000RC100000000000000001"


def _record(obis: str, value: str, hour: int = 0, community: str = "", quality: str = "Wert ist gültig(L1)",
            day: str = "05.11.2026", description: str = "Verbrauch gesamt lt. Messung") -> dict:
    """Return a load profile record in the API's JSON shape."""
    return {
        "Datum": day,
        "Uhrzeit": f"{hour:02d}:00:00",
        "UTC": "+1",
        "Zählpunktbezeichnung": OWN,
        "Name_der_Energiegemeinschaft": "EEG_Test" if community else "",
        "ID_der_Energiegemeinschaft": community,
        "OBIS": obis,
        "OBIS_Kurzbeschreibung": description,
        "Wert": value,
        "Einheit": "kWh",
        "Qualität_Beschreibung": quality,
    }


RECORDS = [
    _record("1-1:1.9.0 G.01", "0,5"),
    _record("1-1:1.9.0 G.01", "0,5", community=COMMUNITY),
    _record("1-1:1.9.0 G.01T", "0,5"),
    _record("1-1:2.9.0 G.02", "0,1"),
    _record("1-1:2.9.0 G.03", "0,1"),
    _record("1-1:1.9.0 P.01T", "0,4"),
]


def _client(**settings) -> SalzburgNetzClient:
    """Return a client whose session counts as checked today."""
    client = SalzburgNetzClient("key", "10000000", settings.pop("metering_points", OWN), **settings)
    client._validated_on = TODAY
    return client


@pytest.fixture(autouse=True)
def _today():
    """Fix the client's day."""
    with patch.object(module, "_today", return_value=TODAY):
        yield


def test_entered_metering_points_are_the_only_ones_read() -> None:
    """Found metering points are only read when none are entered."""
    client = _client()
    client._found = {OWN: {}, OTHER: {}, "AT0040000000000000000000000000003": {}}
    assert client.metering_points_to_read() == [OWN]
    client.metering_points = []
    assert client.metering_points_to_read() == list(client._found)


GAS = "AT9000000000000000000000000000004"


def _contract(point: str, sparte: str, start: str, end: str, active: str, meter: str = "", kind: str = "", product: str = "Netz") -> dict:
    """Return a contract row in the shape of an /anlage answer."""
    return {
        "Vertragskonto-Nr": "000000000001", "Sparte": sparte, "Anlage": "0030000001", "Zählpunkt": point,
        "Anlage_ist_aktiv": active, "Syntetisches_Lastprofil": "H0 HAUSHALT", "Ist_eine_Einspeiseanlage": "X" if kind else "",
        "Anlagenart": kind, "Adresse_der_Anlage": "AT 1234 Musterort Musterstraße 1" if end == "99991231" else "AT 1234 Musterort Musterstraße 2",
        "Vertragsbeginn": start, "Vertragsende": end, "Produkt": product, "KEA_ab": "00000000", "Gerätenummer": meter,
    }


ANLAGE = {
    "PartnerNr": "0010000000",
    "Vertraege": [
        _contract(GAS, "Gas", "20110901", "20130728", "", product="Erdgas"),
        _contract(OWN, "Strom", "20130605", "99991231", "X", "000000000000000042", product="Heizung NT"),
        _contract(OWN, "Strom", "20130605", "99991231", "X", "1ELS0000000001", product="Heizung NT"),
        _contract(OTHER, "Strom", "20230921", "20231128", "", kind="Photovoltaik Anlage", product="Einspeiser"),
        _contract(OTHER, "Strom", "20231129", "99991231", "X", "1ELS0000000002", kind="Photovoltaik Anlage", product="Einspeiser"),
    ],
}


def test_harvest_of_the_facilities() -> None:
    """Every contract row of a metering point is kept, and ended ones are told apart."""
    found = module._harvest_metering_points(ANLAGE)
    assert list(found) == [GAS, OWN, OTHER]
    assert [found[point]["aktiv"] for point in found] == [False, True, True]
    assert found[GAS]["vertraege"] == [{
        "sparte": "Gas", "produkt": "Erdgas", "adresse": "AT 1234 Musterort Musterstraße 2",
        "beginn": "2011-09-01", "ende": "2013-07-28", "aktiv": False,
    }]
    assert [contract["geraet"] for contract in found[OWN]["vertraege"]] == ["000000000000000042", "1ELS0000000001"]
    assert "ende" not in found[OWN]["vertraege"][0]
    assert [contract["aktiv"] for contract in found[OTHER]["vertraege"]] == [False, True]
    assert found[OTHER]["zaehlpunktAnlagentyp"] == "Photovoltaik Anlage"
    assert "verbrauchsstelle" not in found[OWN]


def test_a_current_contract_describes_the_metering_point() -> None:
    """Name, type and address come from a current contract, even when an ended one is listed first."""
    ended = {**_contract(OTHER, "Strom", "20230921", "20231128", "", kind="Photovoltaik Anlage"), "Name": "Alt", "PLZ": "1234"}
    current = _contract(OTHER, "Strom", "20231129", "99991231", "X", "1ELS0000000002")
    found = module._harvest_metering_points({"Vertraege": [ended, current]})[OTHER]
    assert "zaehlpunktAnlagentyp" not in found and "zaehlpunktName" not in found and "verbrauchsstelle" not in found
    assert [contract["aktiv"] for contract in found["vertraege"]] == [False, True]
    found = module._harvest_metering_points({"Vertraege": [ended, {**current, "Anlagenart": "Bezug"}, {**current, "Anlagenart": "Zweit"}]})[OTHER]
    assert found["zaehlpunktAnlagentyp"] == "Bezug"
    found = module._harvest_metering_points({"Vertraege": [ended, {**ended, "Name": "Später"}]})[OTHER]
    assert (found["zaehlpunktName"], found["aktiv"]) == ("Alt", False)


def test_contract_without_an_active_flag() -> None:
    """Without the active flag, a contract is current until its end."""
    assert module._contract({"Vertragsende": "20261106"})["aktiv"] is False
    assert module._contract({"Vertragsende": "20261107"})["aktiv"] is True
    assert module._contract({"Vertragsbeginn": "20200101", "Vertragsende": "99991231"}) == {"beginn": "2020-01-01", "aktiv": True}
    assert module._contract({"Vertragsende": "00000000"}) == {"aktiv": True}
    assert module._contract({"Datum": "05.11.2026"}) == {}


def test_found_metering_points_of_ended_contracts_are_not_read() -> None:
    """Without entered metering points, those whose contracts have all ended are skipped."""
    client = _client(metering_points="")
    client._found = module._harvest_metering_points(ANLAGE)
    assert client.metering_points_to_read() == [OWN, OTHER]
    client._found[GAS].pop("aktiv")
    assert client.metering_points_to_read() == [GAS, OWN, OTHER]


def test_describe_metering_points() -> None:
    """Found and entered metering points come with what the facilities said, without a request."""
    client = _client(metering_points=f"{OWN}, 0030000001")
    client._found = {OWN: {"zaehlpunktnummer": OWN, "zaehlpunktAnlagentyp": "Photovoltaik Anlage"}, OTHER: {"zaehlpunktnummer": OTHER}}
    with patch.object(client, "_post") as post:
        described = client.describe_metering_points()
    assert post.call_count == 0
    assert described == {OWN: client._found[OWN], OTHER: client._found[OTHER], "0030000001": {}}
    described[OWN]["geraetNumber"] = "1"
    assert "geraetNumber" not in client._found[OWN]


def test_invalid_metering_points() -> None:
    """Only 33-character metering points and 10-digit facility numbers are valid."""
    assert module.invalid_metering_points(f"{OWN}, 0030000001") == []
    assert module.invalid_metering_points(f"{OWN};AT123 x") == ["AT123", "X"]


def test_settings_default_and_bounds() -> None:
    """Missing or invalid settings fall back to the defaults, days to their limits."""
    client = _client()
    assert (client.reread_days, client.history) == (3, "year")
    client = _client(reread_days="99", history="decade")
    assert (client.reread_days, client.history) == (14, "year")
    assert _client(reread_days=0).reread_days == 2
    assert _client(reread_days=1).reread_days == 2


def test_factory_passes_the_settings() -> None:
    """get_client hands the stored settings to the client."""
    client = get_client("salzburgnetz", {
        "api_key": "key", "gpnr": "10000000", "metering_points": OWN,
        "load_profile": "profile_eg", "reread_days": 5, "history": "none",
    })
    assert (client.reread_days, client.history) == (5, "none")


def _member_day(day: str, value: str) -> list[dict]:
    """Return the profile_eg rows of a day of a membership: the own total and the community's rows."""
    return [
        _record("1-1:1.9.0 G.01", value, day=day),
        _record("1-1:1.9.0 G.01", value, day=day, community=COMMUNITY),
        _record("1-1:1.9.0 P.01T", "0,1", day=day, community=COMMUNITY),
    ]


def _standard_day(day: str, value: str) -> dict:
    """Return the profile row of a day."""
    return _record("1-1:1.9.0 P.01", value, day=day, description="Restverbrauch/-einspeisung")


def _read(community: list[dict], standard) -> tuple[list[str], list[dict]]:
    """Read the default period with the given answers; return the categories requested and the readings."""
    client = _client(reread_days=3)
    requested: list[str] = []

    def post(url: str, body: dict):
        requested.append(url.rsplit("/", 1)[1])
        assert (body["AB"], body["BIS"]) == ("2026-11-04", "2026-11-07")
        if url.endswith("/profile_eg"):
            return community
        if isinstance(standard, Exception):
            raise standard
        return standard

    with patch.object(client, "_post", side_effect=post):
        readings = client.historical_data(OWN)
    return requested, readings


@pytest.mark.parametrize(
    ("community", "standard", "requested", "values", "name"),
    [
        (
            _member_day("04.11.2026", "0,5") + _member_day("05.11.2026", "0,6") + _member_day("06.11.2026", "0,7"),
            [_standard_day("04.11.2026", "0,4"), _standard_day("05.11.2026", "0,5"), _standard_day("06.11.2026", "0,6")],
            ["profile_eg"], [500.0, 600.0, 700.0], "Verbrauch gesamt lt. Messung",
        ),
        (
            [],
            [_standard_day("04.11.2026", "0,2"), _standard_day("05.11.2026", "0,3"), _standard_day("06.11.2026", "0,4")],
            ["profile_eg", "profile"], [200.0, 300.0, 400.0], "Restverbrauch/-einspeisung",
        ),
        (
            _member_day("05.11.2026", "0,6") + _member_day("06.11.2026", "0,7"),
            [_standard_day("04.11.2026", "0,2"), _standard_day("05.11.2026", "0,55"), _standard_day("06.11.2026", "0,65")],
            ["profile_eg", "profile"], [200.0, 600.0, 700.0], "Verbrauch gesamt lt. Messung",
        ),
    ],
    ids=["member", "not a member", "joining on the second day"],
)
def test_the_measured_total_of_both_load_profiles(community, standard, requested, values, name) -> None:
    """profile_eg gives the own total of the days it has rows for, profile that of the others; profile is only read when needed."""
    asked, readings = _read(community, standard)
    assert asked == requested
    (reading,) = readings
    assert reading["obisCode"] == "1-1:1.9.0" and reading["name"] == name
    assert [entry["messwert"] for entry in reading["messwerte"]] == values


def test_a_refused_standard_profile_fails_the_read() -> None:
    """Days without profile_eg rows are not left out when profile is refused: the read fails as a whole."""
    with pytest.raises(module.SmartmeterConnectionError, match="too many requests"):
        _read(_member_day("06.11.2026", "0,7"), module.SmartmeterConnectionError("HTTP 429 (too many requests, retry later)"))


@pytest.mark.parametrize(
    ("history", "reread_days", "start"),
    [
        ("none", 3, "2026-11-04"),
        ("none", 7, "2026-10-31"),
        ("month", 3, "2026-10-07"),
        ("year", 3, "2025-11-08"),
        ("maximum", 3, "2023-11-08"),
    ],
)
def test_history_start(history: str, reread_days: int, start: str) -> None:
    """The first import starts at the history setting, and never after the re-read window."""
    client = _client(history=history, reread_days=reread_days)
    with patch.object(client, "_post", return_value=RECORDS[:1]):
        (reading,) = client.historical_data(OWN)
    assert reading["history_start"] == start
    assert reading["statistic_id"] == f"asm:{OWN.lower()}_consumption"


def test_statistics_are_named_after_the_metering_point() -> None:
    """Metering points that share a name get statistics with names of their own."""
    client = _client()
    with patch.object(client, "_post", return_value=RECORDS[:1]):
        (reading,) = client.historical_data(OWN)
    assert reading["statistic_name"] == f"Smart Meter Consumption ({OWN})"


def test_poll_is_cached_and_history_read_is_not() -> None:
    """A poll's period is read once per day; a history read every time (both load profiles, as one day has rows)."""
    client = _client()
    with patch.object(client, "_post", return_value=RECORDS[:1]) as post:
        client.historical_data(OWN)
        client.historical_data(OWN)
        assert post.call_count == 2
        client.historical_data(OWN, date(2026, 10, 1), date(2026, 10, 31))
        client.historical_data(OWN, date(2026, 10, 1), date(2026, 10, 31))
        assert post.call_count == 6
