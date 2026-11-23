"""Tests of the scan interval: its unit per provider, its floor, and the stored value."""
from datetime import timedelta
from unittest.mock import MagicMock

import pytest
import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.asm.api.errors import SmartmeterRateLimitError
from custom_components.asm.const import DOMAIN
from custom_components.asm.coordinator import AustriaSmartMeterCoordinator
from custom_components.asm.interval import scan_interval_field, scan_interval_seconds

SIX_HOURS = 6 * 60 * 60


def _entry(provider: str, options: dict | None = None, **data) -> MockConfigEntry:
    """Return an entry of a provider, with options and further data."""
    return MockConfigEntry(
        domain=DOMAIN,
        data={"provider": provider, **data},
        options=options or {},
        unique_id=f"{provider}_test",
    )


@pytest.mark.parametrize(
    ("provider", "options", "data", "seconds"),
    [
        # A portal keeps its minutes; the stored value is what the coordinator uses.
        ("wiener_netze", {}, {}, SIX_HOURS),
        ("wiener_netze", {"scan_interval": 90}, {}, 90 * 60),
        # Below the portal floor, the stored value is read at the floor.
        ("wiener_netze", {"scan_interval": 30}, {}, 60 * 60),
        ("netz_noe", {}, {"scan_interval": 120}, 120 * 60),
        # A live provider that never had an interval keeps the six hours it had.
        ("energylive", {}, {}, SIX_HOURS),
        ("dsmr", {}, {}, SIX_HOURS),
        # A stored minutes value is not read as seconds by the live providers.
        ("energylive", {"scan_interval": 60}, {}, 60 * 60),
        ("dsmr", {"scan_interval": 60}, {}, 60 * 60),
        # The seconds option is what a live provider polls with, down to 10.
        ("energylive", {"scan_interval_seconds": 30}, {}, 30),
        ("energylive", {"scan_interval_seconds": 5}, {}, 10),
        ("dsmr", {"scan_interval_seconds": 10}, {}, 10),
        # A saved seconds value wins over an older minutes one.
        ("energylive", {"scan_interval": 60, "scan_interval_seconds": 15}, {}, 15),
    ],
)
def test_interval_in_the_providers_unit(provider, options, data, seconds) -> None:
    """Portals poll in minutes, the live providers in seconds."""
    assert scan_interval_seconds(_entry(provider, options, **data)) == seconds


def test_the_form_asks_a_portal_in_minutes() -> None:
    """A portal's interval is shown in minutes, at least 60."""
    field = scan_interval_field(_entry("wiener_netze", {"scan_interval": 90}))
    (key,) = field
    assert str(key) == "scan_interval"
    assert key.default() == 90
    assert field[key](60) == 60
    with pytest.raises(vol.Invalid):
        field[key](59)


def test_the_form_asks_a_live_provider_in_seconds() -> None:
    """A live provider's interval is shown in seconds, at least 10."""
    field = scan_interval_field(_entry("energylive", {"scan_interval": 60}))
    (key,) = field
    assert str(key) == "scan_interval_seconds"
    # The minutes value of an older entry is shown converted, not as 60 s.
    assert key.default() == 60 * 60
    assert field[key](10) == 10
    with pytest.raises(vol.Invalid):
        field[key](9)


@pytest.mark.parametrize(
    ("provider", "expected"),
    [("energylive", "scan_interval_seconds"), ("dsmr", "scan_interval_seconds"),
     ("wiener_netze", "scan_interval")],
)
async def test_the_options_form_uses_the_providers_unit(
    hass: HomeAssistant, provider: str, expected: str
) -> None:
    """Each provider's options form offers the interval in its own unit."""
    entry = MockConfigEntry(
        domain=DOMAIN, data={"provider": provider}, unique_id=f"{provider}_options"
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "init"
    assert {str(key): key.default() for key in result["data_schema"].schema} == {
        expected: SIX_HOURS if expected.endswith("seconds") else 360
    }
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {expected: 30 if expected.endswith("seconds") else 90}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {expected: 30 if expected.endswith("seconds") else 90}


async def test_a_refused_poll_pauses_and_resumes(hass: HomeAssistant) -> None:
    """A rate limit raises the interval for its pause, and success lowers it again."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"provider": "energylive", "api_key": "key"},
        options={"scan_interval_seconds": 30},
        unique_id="energylive_pause",
    )
    entry.add_to_hass(hass)
    coordinator = AustriaSmartMeterCoordinator(hass, entry)
    assert coordinator.update_interval == timedelta(seconds=30)

    client = MagicMock()
    client.is_logged_in.return_value = True
    client.is_login_expired.return_value = False
    client.zaehlpunkte.side_effect = SmartmeterRateLimitError("too many", 900)
    coordinator.client = client
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()
    assert coordinator.update_interval == timedelta(seconds=900)

    client.zaehlpunkte.side_effect = None
    client.zaehlpunkte.return_value = [{"zaehlpunkte": []}]
    client.consumptions.return_value = []
    await coordinator._async_update_data()
    assert coordinator.update_interval == timedelta(seconds=30)
