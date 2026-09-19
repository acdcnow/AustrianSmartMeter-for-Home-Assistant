"""The Austria Smartmeter integration."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .coordinator import AustriaSmartMeterCoordinator

PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry[AustriaSmartMeterCoordinator]
) -> bool:
    """Set up Austria Smartmeter from a config entry."""
    # The coordinator takes care of logging in and fetching the readings.
    coordinator = AustriaSmartMeterCoordinator(hass, entry)

    # Fetch data before the entities are created so they have a state right away.
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Reload the entry when the options (e.g. the scan interval) change.
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: ConfigEntry[AustriaSmartMeterCoordinator]
) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_update_listener(
    hass: HomeAssistant, entry: ConfigEntry[AustriaSmartMeterCoordinator]
) -> None:
    """Handle an options update."""
    await hass.config_entries.async_reload(entry.entry_id)
