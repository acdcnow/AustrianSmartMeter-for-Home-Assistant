"""Tests of a Salzburg Netz entry set up by version 1.2.2, before the settings existed."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.asm.api.client import get_client
from custom_components.asm.api.client_salzburgnetz import SalzburgNetzClient
from custom_components.asm.const import DOMAIN

OWN = "AT0040000000000000000000000000001"
OTHER = "AT0040000000000000000000000000002"
ENDED = "AT0040000000000000000000000000003"
FACILITY = "0030000001"


def _row(point: str, active: str) -> dict:
    """Return a contract row of an /anlage answer."""
    return {"Zählpunkt": point, "Sparte": "Strom", "Anlage_ist_aktiv": active,
            "Vertragsbeginn": "20130605", "Vertragsende": "99991231" if active else "20200101"}


ANLAGE = {"PartnerNr": "0010000000", "Vertraege": [_row(ENDED, ""), _row(OWN, "X"), _row(OTHER, "X")]}


@pytest.mark.parametrize(
    ("entered", "read"),
    [(OWN, [OWN]), (f"{FACILITY}, {OTHER}", [FACILITY, OTHER]), ("", [OWN, OTHER])],
)
async def test_an_entry_of_version_122_is_set_up_as_it_is(hass: HomeAssistant, entered: str, read: list[str]) -> None:
    """The entry needs no migration and stays unchanged.

    Entered metering points are the only ones read; without any, all listed ones
    with a current contract. The settings take their defaults.
    """
    entry = MockConfigEntry(
        domain=DOMAIN, title="Salzburg Netz (10000000)", unique_id="salzburgnetz_10000000",
        data={"provider": "salzburgnetz", "api_key": "key", "gpnr": "10000000", "metering_points": entered},
        options={"scan_interval": 360},
    )
    entry.add_to_hass(hass)
    before = (dict(entry.data), dict(entry.options), entry.version, entry.minor_version)
    clients: list[SalzburgNetzClient] = []
    reads: list[list[str]] = []

    def coordinator(hass: HomeAssistant, entry: MockConfigEntry) -> MagicMock:
        client = get_client("salzburgnetz", {**entry.data, **entry.options})
        client._post = MagicMock(return_value=ANLAGE)
        clients.append(client)

        def first_refresh() -> None:
            client.login()
            reads.append(client.metering_points_to_read())

        made = MagicMock(provider="salzburgnetz", client=client)
        made.async_config_entry_first_refresh = AsyncMock(side_effect=first_refresh)
        return made

    with (
        patch("custom_components.asm.AustriaSmartMeterCoordinator", side_effect=coordinator),
        patch.object(hass.config_entries, "async_forward_entry_setups", AsyncMock()),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert (dict(entry.data), dict(entry.options), entry.version, entry.minor_version) == before
    assert reads == [read]
    assert [(client.reread_days, client.history) for client in clients] == [(3, "year")]
