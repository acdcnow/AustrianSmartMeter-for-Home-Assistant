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
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    OBIS_NAMES,
    PROVIDER_ENERGIEDATEN,
    PROVIDER_NETZ_NOE,
    PROVIDER_WIENER_NETZE,
)
from .coordinator import AustriaSmartMeterCoordinator

# Readings that describe the consumption of a period (and therefore reset every
# day) instead of a cumulative meter reading.
_PERIOD_VALUE_TYPES = {"DAY", "CONSUMPTION", "QUARTER_HOUR"}

# Only devices with this state class may carry a `last_reset` attribute.
_PERIOD_STATE_CLASS = SensorStateClass.TOTAL

_PROVIDER_PORTALS = {
    PROVIDER_WIENER_NETZE: ("Wiener Netze", "https://smartmeter-web.wienernetze.at/"),
    PROVIDER_NETZ_NOE: ("Netz Niederösterreich (EVN)", "https://smartmeter.netz-noe.at/"),
    PROVIDER_ENERGIEDATEN: ("energiedaten.at", "https://energiedaten.at/"),
}


def _as_float(value: Any) -> float | None:
    """Return ``value`` as a float, or None when it is not a number."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _provider_details(provider_id: str | None) -> tuple[str, str | None]:
    """Return the manufacturer and portal URL for a provider."""
    return _PROVIDER_PORTALS.get(
        provider_id or "", ("Austria Smartmeter Integration", None)
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Austria Smartmeter sensors."""
    coordinator: AustriaSmartMeterCoordinator = entry.runtime_data
    entities: list[SensorEntity] = []

    # Iterate over all Zählpunkte found in the data
    for zp_num, zp_data in coordinator.data.items():
        readings = zp_data.get("readings", [])
        stats = zp_data.get("stats", {})
        info = zp_data.get("info", {})

        # 1. Main OBIS Sensors (Zählerstände)
        if isinstance(readings, dict):
            readings = [readings]
        for reading_data in readings:
            if isinstance(reading_data, dict) and "obisCode" in reading_data:
                entities.append(
                    AustriaSmartMeterSensor(coordinator, zp_num, reading_data, info)
                )

        # 2. Diagnostic Sensors (Static Info & Address)
        if "zaehlpunktnummer" in info:
            entities.append(AustriaSmartMeterDiagnostic(
                coordinator, zp_num, "zaehlpunktnummer", "Metering Point ID", info["zaehlpunktnummer"]
            ))
        if "geschaeftspartner" in info:
            entities.append(AustriaSmartMeterDiagnostic(
                coordinator, zp_num, "customer_id", "Customer ID", info["geschaeftspartner"]
            ))
        if "isSmartMeterMarketReady" in info:
            entities.append(AustriaSmartMeterDiagnostic(
                coordinator, zp_num, "market_ready", "Market Ready", info["isSmartMeterMarketReady"]
            ))
        if "isActive" in info:
            entities.append(AustriaSmartMeterDiagnostic(
                coordinator, zp_num, "is_active", "Contract Active", info["isActive"]
            ))
        if "smartMeterType" in info and info["smartMeterType"]:
            entities.append(AustriaSmartMeterDiagnostic(
                coordinator, zp_num, "smart_meter_type", "Smart Meter Type", info["smartMeterType"]
            ))
        if "showConsumption" in info:
            entities.append(AustriaSmartMeterDiagnostic(
                coordinator, zp_num, "show_consumption", "Consumption Visible", info["showConsumption"]
            ))

        if "anlage" in info and isinstance(info["anlage"], dict) and "typ" in info["anlage"]:
            entities.append(AustriaSmartMeterDiagnostic(
                coordinator, zp_num, "facility_type", "Facility Type", info["anlage"]["typ"]
            ))

        if "verbrauchsstelle" in info and isinstance(info["verbrauchsstelle"], dict):
            addr = info["verbrauchsstelle"]

            full_addr = (
                f"{addr.get('strasse', '')} {addr.get('hausnummer', '')}, "
                f"{addr.get('postleitzahl', '')} {addr.get('ort', '')}"
            )
            entities.append(AustriaSmartMeterDiagnostic(
                coordinator, zp_num, "address", "Address", full_addr.strip()
            ))

            addr_fields = {
                "strasse": "Street",
                "hausnummer": "Street Number",
                "stiege": "Stair",
                "tuer": "Door",
                "postleitzahl": "Postal Code",
                "ort": "City",
                "laengengrad": "Longitude",
                "breitengrad": "Latitude",
            }
            for key, label in addr_fields.items():
                if addr.get(key):
                    entities.append(AustriaSmartMeterDiagnostic(
                        coordinator, zp_num, f"address_{key}", f"Address {label}", addr[key]
                    ))

        # 3. Statistic Sensors (Consumption Yesterday, etc.)
        statistic_fields = {
            "consumptionYesterday": "Consumption Yesterday",
            "consumptionDayBeforeYesterday": "Consumption Day Before Yesterday",
        }
        for key, label in statistic_fields.items():
            if key in stats:
                entities.append(AustriaSmartMeterStatistic(
                    coordinator, zp_num, stats[key], label, key
                ))

    async_add_entities(entities)


def _get_clean_meter_name(info: dict[str, Any]) -> str:
    """Return a clean meter name without the AT... number."""
    return info.get("zaehlpunktName") or "Smart Meter"


def _get_shared_device_info(
    zaehlpunkt: str, info: dict[str, Any], provider_id: str | None = None
) -> DeviceInfo:
    """Generate the device info dict shared by all entities of a meter."""
    manufacturer, configuration_url = _provider_details(provider_id)

    return DeviceInfo(
        identifiers={(DOMAIN, zaehlpunkt)},
        name=_get_clean_meter_name(info),
        manufacturer=manufacturer,
        model=f"Smart Meter {info.get('zaehlpunktAnlagentyp', '')}".strip(),
        serial_number=info.get("geraetNumber"),
        hw_version=str(info.get("equipmentNumber") or "Unknown"),
        configuration_url=configuration_url,
    )


class AustriaSmartMeterSensor(CoordinatorEntity[AustriaSmartMeterCoordinator], SensorEntity):
    """Main Sensor (OBIS readings)."""

    def __init__(self, coordinator, zaehlpunkt, obis_data, info) -> None:
        super().__init__(coordinator)
        self._zaehlpunkt = zaehlpunkt
        self._obis_code = obis_data.get("obisCode")

        # Unit handling
        self._unit = obis_data.get("einheit")

        # Init defaults
        self._attr_native_unit_of_measurement = None
        self._attr_device_class = None
        self._attr_state_class = None

        readable_obis = (
            obis_data.get("name") or OBIS_NAMES.get(self._obis_code, self._obis_code)
        )

        # Check if this is a known Energy Meter OBIS code
        is_known_energy_obis = self._obis_code in OBIS_NAMES

        # FORCE Energy Configuration with Wh
        if is_known_energy_obis or self._unit in ["kWh", "Wh"]:
            self._attr_device_class = SensorDeviceClass.ENERGY
            self._attr_state_class = _state_class_for(obis_data)

            # The portal values are normalised to Wh by the API clients.
            self._attr_native_unit_of_measurement = UnitOfEnergy.WATT_HOUR

        # Naming
        meter_name = _get_clean_meter_name(info)
        self._attr_name = f"{meter_name} {readable_obis}"
        self._attr_unique_id = f"{zaehlpunkt}_{self._obis_code}"
        self._attr_device_info = _get_shared_device_info(
            zaehlpunkt, info, coordinator.provider
        )

    def _get_current_obis_data(self) -> dict | None:
        """Return the reading block of this sensor's OBIS code."""
        all_readings = self.coordinator.data.get(self._zaehlpunkt, {}).get("readings", [])
        if isinstance(all_readings, dict):
            all_readings = [all_readings]
        for reading in all_readings:
            if isinstance(reading, dict) and reading.get("obisCode") == self._obis_code:
                return reading
        return None

    def _get_latest_reading(self, values: list) -> dict | None:
        """Return the most recent entry of a reading list."""
        valid_values = []
        for value in values or []:
            if not isinstance(value, dict):
                continue
            timestamp = (
                value.get("zeitBis")
                or value.get("zeitVon")
                or value.get("zeitpunkt")
                or value.get("date")
                or value.get("timestamp")
                or value.get("readAt")
            )
            if timestamp:
                valid_values.append((timestamp, value))
        if not valid_values:
            return None
        return sorted(valid_values, key=lambda item: item[0])[-1][1]

    @property
    def native_value(self) -> float | None:
        """Return the latest value of this OBIS code."""
        data = self._get_current_obis_data()
        if not data:
            return None
        latest = self._get_latest_reading(data.get("messwerte"))
        if not latest:
            return None
        for key in ("messwert", "value", "amount"):
            if (result := _as_float(latest.get(key))) is not None:
                return result
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the attributes for the main sensor."""
        data = self._get_current_obis_data() or {}
        attributes: dict[str, Any] = {
            "zaehlpunkt": self._zaehlpunkt,
            "obis_code": self._obis_code,
            "raw_unit": data.get("einheit") or "Wh (assumed)",
        }

        info = self.coordinator.data.get(self._zaehlpunkt, {}).get("info", {})
        for key, value in info.items():
            if isinstance(value, list):
                continue
            if isinstance(value, dict):
                for sub_key, sub_value in value.items():
                    if isinstance(sub_value, (str, int, float, bool)) or sub_value is None:
                        attributes[f"{key}_{sub_key}"] = sub_value
            else:
                attributes[key] = value

        latest = self._get_latest_reading(data.get("messwerte"))
        if latest:
            attributes["last_reading_date"] = (
                latest.get("zeitBis")
                or latest.get("zeitVon")
                or latest.get("zeitpunkt")
                or latest.get("date")
            )
            attributes["validation_status"] = latest.get("qualitaet") or latest.get("status")

            for key, value in latest.items():
                if key not in ["messwert", "value", "amount", "qualitaet", "status", "validated"]:
                    attributes[f"latest_{key}"] = value
        return attributes


class AustriaSmartMeterDiagnostic(
    CoordinatorEntity[AustriaSmartMeterCoordinator], SensorEntity
):
    """Diagnostic Sensor for static info."""

    def __init__(self, coordinator, zaehlpunkt, key, name_suffix, value) -> None:
        super().__init__(coordinator)
        self._zaehlpunkt = zaehlpunkt

        info = coordinator.data.get(zaehlpunkt, {}).get("info", {})
        meter_name = _get_clean_meter_name(info)

        self._attr_name = f"{meter_name} {name_suffix}"
        self._attr_unique_id = f"{zaehlpunkt}_diag_{key}"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_native_value = str(value)
        self._attr_icon = "mdi:information-outline"
        self._attr_device_info = _get_shared_device_info(
            zaehlpunkt, info, coordinator.provider
        )


class AustriaSmartMeterStatistic(
    CoordinatorEntity[AustriaSmartMeterCoordinator], SensorEntity
):
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
        # Daily values, not a cumulative meter reading.
        self._attr_state_class = _PERIOD_STATE_CLASS
        self._attr_native_unit_of_measurement = UnitOfEnergy.WATT_HOUR
        self._attr_device_info = _get_shared_device_info(
            zaehlpunkt, info, coordinator.provider
        )

    @property
    def native_value(self) -> float | None:
        """Return the consumption of the day."""
        stats = self.coordinator.data.get(self._zaehlpunkt, {}).get("stats", {})
        data = (stats or {}).get(self._key_id)
        if not data:
            return None
        return _as_float(data.get("value"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the attributes of the statistic."""
        stats = self.coordinator.data.get(self._zaehlpunkt, {}).get("stats", {})
        data = (stats or {}).get(self._key_id)
        if not data:
            return {}
        return {
            "date": data.get("date"),
            "validated": data.get("validated"),
        }


def _state_class_for(obis_data: dict[str, Any]) -> SensorStateClass:
    """Return the proper state class for a reading.

    Consumption values of a period reset every day, they must not be reported as
    total increasing meter readings.
    """
    value_type = str(obis_data.get("wertetyp") or "").upper()
    if value_type in _PERIOD_VALUE_TYPES:
        return _PERIOD_STATE_CLASS
    return SensorStateClass.TOTAL_INCREASING
