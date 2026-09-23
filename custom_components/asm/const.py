"""Constants for the Austria Smartmeter integration."""
from __future__ import annotations

import logging

from homeassistant.const import (
    CONF_API_KEY,
    CONF_COUNTRY_CODE,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_TOKEN,
    CONF_USERNAME,
)

DOMAIN = "asm"
LOGGER = logging.getLogger(__package__)

# Configuration
CONF_PROVIDER = "provider"

# Credentials of the DSMR provider. The names match Home Assistant's own DSMR
# integration, so an entry of this integration is recognisable to anyone who
# knows that one.
CONF_DSMR_VERSION = "dsmr_version"
CONF_ENCRYPTION_KEY = "encryption_key"

# The aWATTar market feed is selected by market area, not by a credential.
CONF_MARKET_AREA = "market_area"

# Selectra is a paid third-party tariff planning API. Every user brings a personal
# bearer token (CONF_TOKEN), and the offer the questionnaire qualified is stored
# with the entry, because re-qualifying would cost calls of the monthly quota.
CONF_POSTCODE = "postcode"
CONF_SELECTRA_INPUTS = "inputs"
CONF_SELECTRA_LABEL = "offer_label"

# Providers
PROVIDER_WIENER_NETZE = "wiener_netze"
PROVIDER_NETZ_NOE = "netz_noe"
PROVIDER_ENERGYLIVE = "energylive"
PROVIDER_DSMR = "dsmr"
PROVIDER_AWATTAR = "awattar"
PROVIDER_SELECTRA = "selectra"
PROVIDER_STROMNETZ_GRAZ = "stromnetz_graz"

PROVIDERS = {
    PROVIDER_WIENER_NETZE: "Wiener Netze",
    PROVIDER_NETZ_NOE: "Netz Niederösterreich (EVN)",
    PROVIDER_ENERGYLIVE: "energyLIVE (smartENERGY)",
    PROVIDER_DSMR: "DSMR / P1 meter (local)",
    PROVIDER_AWATTAR: "aWATTar market prices",
    PROVIDER_SELECTRA: "Selectra tariff planning",
    # PROVIDER_STROMNETZ_GRAZ: "Stromnetz Graz", # In Entwicklung
}

# Options
CONF_SCAN_INTERVAL = "scan_interval"
DEFAULT_SCAN_INTERVAL = 60 * 6  # 6 hours
MIN_SCAN_INTERVAL = 60

# Attributes
ATTR_ZAEHLPUNKT = "zaehlpunkt"
ATTR_OBIS_CODE = "obis_code"
ATTR_UNIT = "unit"

# OBIS mappings
OBIS_NAMES = {
    "1-1:1.8.0": "Energy Consumption Total",
    "1-1:1.9.0": "Energy Consumption Interval",
    "1-1:2.8.0": "Energy Production Total",
    "1-1:2.9.0": "Energy Production Interval",
}

__all__ = [
    "ATTR_OBIS_CODE",
    "ATTR_UNIT",
    "ATTR_ZAEHLPUNKT",
    "CONF_API_KEY",
    "CONF_COUNTRY_CODE",
    "CONF_DSMR_VERSION",
    "CONF_ENCRYPTION_KEY",
    "CONF_MARKET_AREA",
    "CONF_PASSWORD",
    "CONF_POSTCODE",
    "CONF_PROVIDER",
    "CONF_SCAN_INTERVAL",
    "CONF_SELECTRA_INPUTS",
    "CONF_SELECTRA_LABEL",
    "CONF_TOKEN",
    "CONF_USERNAME",
    "DEFAULT_SCAN_INTERVAL",
    "DOMAIN",
    "LOGGER",
    "MIN_SCAN_INTERVAL",
    "OBIS_NAMES",
    "PROVIDERS",
    "PROVIDER_AWATTAR",
    "PROVIDER_DSMR",
    "PROVIDER_ENERGYLIVE",
    "PROVIDER_NETZ_NOE",
    "PROVIDER_SELECTRA",
    "PROVIDER_STROMNETZ_GRAZ",
    "PROVIDER_WIENER_NETZE",
]
