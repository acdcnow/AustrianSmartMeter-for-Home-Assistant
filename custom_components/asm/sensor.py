"""Sensor platform for Austria Smartmeter."""
from __future__ import annotations
from typing import Any
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from .const import DOMAIN, OBIS_NAMES, LOGGER, PROVIDER_WIENER_NETZE, PROVIDER_NETZ_NOE
from .coordinator import AustriaSmartMeterCoordinator

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Austria Smartmeter sensors."""
    coordinator: AustriaSmartMeterCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities = []

    # Iterate over all Zählpunkte found in the data
    for zp_num, zp_data in coordinator.data.items():
        readings = zp_data.get("readings", [])
        stats = zp_data.get("stats", {})
        info = zp_data.get("info", {})

        # 1. Main OBIS Sensors (Zählerstände)
        if isinstance(readings, dict): readings = [readings]
        if readings:
            for reading_data in readings:
                if "obisCode" in reading_data:
                    entities.append(AustriaSmartMeterSensor(coordinator, zp_num, reading_data, info))

        # 2. Diagnostic Sensors (Static Info & Address)
        if "zaehlpunktnummer" in info:
             entities.append(AustriaSmartMeterDiagnostic(coordinator, zp_num, "zaehlpunktnummer", "Metering Point ID", info["zaehlpunktnummer"]))
        if "geschaeftspartner" in info:
             entities.append(AustriaSmartMeterDiagnostic(coordinator, zp_num, "customer_id", "Customer ID", info["geschaeftspartner"]))
        if "isSmartMeterMarketReady" in info:
             entities.append(AustriaSmartMeterDiagnostic(coordinator, zp_num, "market_ready", "Market Ready", info["isSmartMeterMarketReady"]))
        if "isActive" in info:
             entities.append(AustriaSmartMeterDiagnostic(coordinator, zp_num, "is_active", "Contract Active", info["isActive"]))
        
        if "anlage" in info and isinstance(info["anlage"], dict) and "typ" in info["anlage"]:
             entities.append(AustriaSmartMeterDiagnostic(coordinator, zp_num, "facility_type", "Facility Type", info["anlage"]["typ"]))

        if "verbrauchsstelle" in info and isinstance(info["verbrauchsstelle"], dict):
            addr = info["verbrauchsstelle"]
            
            full_addr = f"{addr.get('strasse', '')} {addr.get('hausnummer', '')}, {addr.get('postleitzahl', '')} {addr.get('ort', '')}"
            entities.append(AustriaSmartMeterDiagnostic(coordinator, zp_num, "address", "Address", full_addr.strip()))
            
            addr_fields = {
                "strasse": "Street",
                "hausnummer": "Street Number",
                "stiege": "Stair",
                "tuer": "Door",
                "postleitzahl": "Postal Code",
                "ort": "City",
                "laengengrad": "Longitude",
                "breitengrad": "Latitude"
            }
            
            for key, label in addr_fields.items():
                if key in addr and addr[key]:
                     entities.append(AustriaSmartMeterDiagnostic(
                         coordinator, 
                         zp_num, 
                         f"address_{key}", 
                         f"Address {label}", 
                         addr[key]
                     ))

        # 3. Statistic Sensors (Consumption Yesterday, etc.)
        if stats:
            if "consumptionYesterday" in stats:
                entities.append(AustriaSmartMeterStatistic(
                    coordinator, zp_num, stats["consumptionYesterday"], "Consumption Yesterday", "consumptionYesterday"
                ))
            if "consumptionDayBeforeYesterday" in stats:
                entities.append(AustriaSmartMeterStatistic(
                    coordinator, zp_num, stats["consumptionDayBeforeYesterday"], "Consumption Day Before Yesterday", "consumptionDayBeforeYesterday"
                ))

    async_add_entities(entities)


def _get_clean_meter_name(info):
    """Returns a clean name without the AT... number."""
    return info.get('zaehlpunktName') or "Smart Meter"


def _get_shared_device_info(zaehlpunkt, info, provider_id=None):
    """Generates the device info dict shared by all entities of a meter."""
    meter_name = _get_clean_meter_name(info)
    
    manufacturer = "Austria Smartmeter Integration"
    conf_url = None
    
    if provider_id == PROVIDER_WIENER_NETZE:
        manufacturer = "Wiener Netze"
        conf_url = "https://smartmeter-web.wienernetze.at/"
    elif provider_id == PROVIDER_NETZ_NOE:
        manufacturer = "Netz Niederösterreich (EVN)"
        conf_url = "https://smartmeter.netz-noe.at/"
        
    return {
        "identifiers": {(DOMAIN, zaehlpunkt)},
        "name": meter_name,
        "manufacturer": manufacturer,
        "model": f"Smart Meter {info.get('zaehlpunktAnlagentyp', '')}".strip(),
        "serial_number": info.get("geraetNumber"),
        "hw_version": str(info.get("equipmentNumber") or "Unknown"),
        "configuration_url": conf_url
    }


class AustriaSmartMeterSensor(CoordinatorEntity, SensorEntity):
    """Main Sensor (OBIS readings)."""

    def __init__(self, coordinator, zaehlpunkt, obis_data, info) -> None:
        super().__init__(coordinator)
        self._zaehlpunkt = zaehlpunkt
        self._obis_code = obis_data.get("obisCode")
        
        # Unit Handling
        self._unit = obis_data.get("einheit")
        
        # Init defaults
        self._attr_native_unit_of_measurement = None
        self._attr_device_class = None
        self._attr_state_class = None

        readable_obis = OBIS_NAMES.get(self._obis_code, self._obis_code)
        
        # Check if this is a known Energy Meter OBIS Code
        is_known_energy_obis = self._obis_code in OBIS_NAMES
        
        # FORCE Energy Configuration with Wh
        if is_known_energy_obis or self._unit in ["kWh", "Wh"]:
            self._attr_device_class = SensorDeviceClass.ENERGY
            self._attr_state_class = SensorStateClass.TOTAL_INCREASING
            
            # CHANGE: Set to Wh (Watt-hours)
            self._attr_native_unit_of_measurement = UnitOfEnergy.WATT_HOUR
            
            # If unit was missing, assume Wh
            if not self._unit:
                self._unit = "Wh"
        
        # Naming
        meter_name = _get_clean_meter_name(info)
        self._attr_name = f"{meter_name} {readable_obis}"
        self._attr_unique_id = f"{zaehlpunkt}_{self._obis_code}"
        
        provider = PROVIDER_WIENER_NETZE if "WienerNetzeClient" in coordinator.client.__class__.__name__ else PROVIDER_NETZ_NOE
        self._attr_device_info = _get_shared_device_info(zaehlpunkt, info, provider)

    def _get_current_obis_data(self) -> dict | None:
        all_readings = self.coordinator.data.get(self._zaehlpunkt, {}).get("readings", [])
        if isinstance(all_readings, dict): all_readings = [all_readings]
        for r in all_readings:
            if r.get("obisCode") == self._obis_code:
                return r
        return None

    def _get_latest_reading(self, values: list) -> dict | None:
        valid_values = []
        for v in values:
            ts = v.get("zeitBis") or v.get("zeitVon") or v.get("zeitpunkt") or v.get("date") or v.get("timestamp") or v.get("readAt")
            if ts: valid_values.append((ts, v))
        if not valid_values: return None
        return sorted(valid_values, key=lambda x: x[0])[-1][1]

    @property
    def native_value(self) -> float | None:
        data = self._get_current_obis_data()
        if not data or "messwerte" not in data: return None
        latest = self._get_latest_reading(data["messwerte"])
        if not latest: return None
        
        val = latest.get("messwert") or latest.get("value") or latest.get("amount")
        if val is None: return None

        # CHANGE: Direct return without conversion for Wh
        return float(val)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Attributes for main sensor."""
        data = self._get_current_obis_data() or {}
        attributes = {
            "zaehlpunkt": self._zaehlpunkt,
            "obis_code": self._obis_code,
            "raw_unit": data.get("einheit") or "Wh (assumed)"
        }
        
        info = self.coordinator.data.get(self._zaehlpunkt, {}).get("info", {})
        if info:
            for key, value in info.items():
                if isinstance(value, list): continue
                if isinstance(value, dict):
                    for sub_key, sub_value in value.items():
                        if isinstance(sub_value, (str, int, float, bool)) or sub_value is None:
                            attributes[f"{key}_{sub_key}"] = sub_value
                else:
                    attributes[key] = value

        values = data.get("messwerte", [])
        latest = self._get_latest_reading(values)
        if latest:
             ts = latest.get("zeitBis") or latest.get("zeitVon") or latest.get("zeitpunkt") or latest.get("date")
             attributes["last_reading_date"] = ts
             attributes["validation_status"] = latest.get("qualitaet") or latest.get("status")
             
             for k, v in latest.items():
                 if k not in ["messwert", "value", "amount", "qualitaet", "status", "validated"]:
                      attributes[f"latest_{k}"] = v
        return attributes


class AustriaSmartMeterDiagnostic(CoordinatorEntity, SensorEntity):
    """Diagnostic Sensor for static info."""

    def __init__(self, coordinator, zaehlpunkt, key, name_suffix, value) -> None:
        super().__init__(coordinator)
        self._zaehlpunkt = zaehlpunkt
        self._key = key
        self._value = value
        
        info = coordinator.data.get(zaehlpunkt, {}).get("info", {})
        
        meter_name = _get_clean_meter_name(info)
        self._attr_name = f"{meter_name} {name_suffix}"
        
        self._attr_unique_id = f"{zaehlpunkt}_diag_{key}"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_native_value = str(value)
        self._attr_icon = "mdi:information-outline"
        
        provider = PROVIDER_WIENER_NETZE if "WienerNetzeClient" in coordinator.client.__class__.__name__ else PROVIDER_NETZ_NOE
        self._attr_device_info = _get_shared_device_info(zaehlpunkt, info, provider)

class AustriaSmartMeterStatistic(CoordinatorEntity, SensorEntity):
    """Statistic Sensor for Daily Consumptions."""

    def __init__(self, coordinator, zaehlpunkt, stat_data, name_suffix, key_id) -> None:
        super().__init__(coordinator)
        self._zaehlpunkt = zaehlpunkt
        self._key_id = key_id
        
        info = coordinator.data.get(zaehlpunkt, {}).get("info", {})
        
        meter_name = _get_clean_meter_name(info)
        self._attr_name = f"{meter_name} {name_suffix}"
        
        self._attr_unique_id = f"{zaehlpunkt}_stat_{key_id}"
        
        self._attr_device_class = SensorDeviceClass.ENERGY
        self._attr_state_class = SensorStateClass.TOTAL
        
        # CHANGE: Set to Wh (Watt-hours)
        self._attr_native_unit_of_measurement = UnitOfEnergy.WATT_HOUR
        
        provider = PROVIDER_WIENER_NETZE if "WienerNetzeClient" in coordinator.client.__class__.__name__ else PROVIDER_NETZ_NOE
        self._attr_device_info = _get_shared_device_info(zaehlpunkt, info, provider)

    @property
    def native_value(self) -> float | None:
        stats = self.coordinator.data.get(self._zaehlpunkt, {}).get("stats", {})
        if not stats: return None
        data = stats.get(self._key_id)
        if not data: return None
        val = data.get("value")
        if val is None: return None
        
        # CHANGE: Direct return without conversion for Wh
        return float(val)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        stats = self.coordinator.data.get(self._zaehlpunkt, {}).get("stats", {})
        if not stats: return {}
        data = stats.get(self._key_id)
        if not data: return {}
        return {
            "date": data.get("date"),
            "validated": data.get("validated")
        }