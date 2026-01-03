"""DataUpdateCoordinator for Austria Smartmeter."""
from datetime import timedelta
from typing import Any
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.exceptions import ConfigEntryAuthFailed
from .api.client import get_client, SmartmeterLoginError
from .const import DOMAIN, LOGGER, CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL, CONF_PROVIDER, CONF_USERNAME, CONF_PASSWORD

class AustriaSmartMeterCoordinator(DataUpdateCoordinator):
    """Class to manage fetching Austria Smartmeter data."""

    def __init__(self, hass: HomeAssistant, entry_data: dict, entry_options: dict) -> None:
        provider = entry_data.get(CONF_PROVIDER, "wiener_netze")
        username = entry_data[CONF_USERNAME]
        password = entry_data[CONF_PASSWORD]
        
        self.client = get_client(provider, username, password)
        scan_interval_min = entry_options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        
        super().__init__(hass, LOGGER, name=DOMAIN, update_interval=timedelta(minutes=scan_interval_min))

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from API endpoint."""
        try:
            if not self.client.is_logged_in() or self.client.is_login_expired():
                 await self.hass.async_add_executor_job(self.client.login)

            # 1. Fetch Contracts
            contracts = await self.hass.async_add_executor_job(self.client.zaehlpunkte)
            
            # 2. Fetch Consumption Stats (Yesterday, etc.)
            try:
                consumption_stats = await self.hass.async_add_executor_job(self.client.consumptions)
                
                # FIX: Check structure of consumption_stats
                if isinstance(consumption_stats, dict):
                    # Wenn es ein einzelnes Dict ist, verpacken wir es in eine Liste
                    consumption_stats = [consumption_stats]
                    LOGGER.debug(f"DEBUG Stats: Received single dict. Keys: {consumption_stats[0].keys()}")
                elif isinstance(consumption_stats, list):
                    LOGGER.debug(f"DEBUG Stats: Received list with {len(consumption_stats)} elements.")
                else:
                    LOGGER.warning(f"DEBUG Stats: Unknown format received: {type(consumption_stats)}")
                    consumption_stats = []

            except Exception as e:
                LOGGER.debug(f"Consumptions API call failed (not supported by provider?): {e}")
                consumption_stats = []

            data = {}
            for contract in contracts:
                if "zaehlpunkte" not in contract: continue
                
                for zp_info in contract["zaehlpunkte"]:
                    zp_num = zp_info["zaehlpunktnummer"]
                    data[zp_num] = {
                        "info": zp_info,
                        "readings": {},
                        "stats": {} 
                    }
                    
                    # Match stats to ZP
                    for stat in consumption_stats:
                        # Safety check: ensure stat is a dict
                        if not isinstance(stat, dict):
                            continue
                            
                        # Check if ZP matches OR if stats doesn't have a ZP number (assume it belongs to the only meter?)
                        # API responses sometimes omit the ZP number if only one exists.
                        stat_zp = stat.get("zaehlpunktnummer") or stat.get("zaehlpunkt")
                        
                        if stat_zp == zp_num:
                            data[zp_num]["stats"] = stat
                            break
                        elif stat_zp is None and len(contracts) == 1 and len(contract["zaehlpunkte"]) == 1:
                            # Fallback: If no ZP ID in stats and user only has 1 meter, assign it.
                            LOGGER.debug(f"Assigning stats to {zp_num} (implicit match)")
                            data[zp_num]["stats"] = stat
                            break

                    # 3. Fetch Historical Data (OBIS readings)
                    try:
                        historic = await self.hass.async_add_executor_job(
                             lambda: self.client.historical_data(zaehlpunktnummer=zp_num)
                        )
                        data[zp_num]["readings"] = historic
                    except Exception as e:
                        LOGGER.warning(f"Could not fetch historic data for {zp_num}: {e}")

            return data

        except SmartmeterLoginError as err:
            raise ConfigEntryAuthFailed from err
        except Exception as err:
            LOGGER.exception("Unexpected error during update")
            raise UpdateFailed(f"Error: {err}") from err