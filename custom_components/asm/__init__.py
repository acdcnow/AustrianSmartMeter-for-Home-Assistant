"""The Austria Smartmeter integration."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .api.client import SmartmeterClient # Type hinting only
from .const import DOMAIN
from .coordinator import AustriaSmartMeterCoordinator

# Unterstützte Plattformen
PLATFORMS: list[Platform] = [Platform.SENSOR]

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Austria Smartmeter from a config entry."""
    
    # 1. Coordinator initialisieren
    # Der Coordinator kümmert sich um Login und Datenabruf
    coordinator = AustriaSmartMeterCoordinator(hass, entry.data, entry.options)

    # 2. Erster Datenabruf (damit Sensoren gleich Daten haben)
    await coordinator.async_config_entry_first_refresh()

    # 3. Coordinator in hass.data speichern
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    # 4. Plattformen (Sensoren) laden
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    
    # 5. Update Listener registrieren (WICHTIG für Options Flow!)
    # Wenn Optionen geändert werden, wird update_listener aufgerufen
    entry.async_on_unload(entry.add_update_listener(update_listener))

    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok

async def update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update."""
    # Lädt die Integration neu, wenn Optionen (z.B. Scan Intervall) geändert wurden
    await hass.config_entries.async_reload(entry.entry_id)