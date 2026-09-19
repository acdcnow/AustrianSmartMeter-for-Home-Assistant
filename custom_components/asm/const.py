"""Constants for the Austria Smartmeter integration."""
from __future__ import annotations

import logging

from homeassistant.const import CONF_PASSWORD, CONF_USERNAME

DOMAIN = "asm"
LOGGER = logging.getLogger(__package__)

# Configuration
CONF_PROVIDER = "provider"

# Providers
PROVIDER_WIENER_NETZE = "wiener_netze"
PROVIDER_NETZ_NOE = "netz_noe"
PROVIDER_STROMNETZ_GRAZ = "stromnetz_graz"

PROVIDERS = {
    PROVIDER_WIENER_NETZE: "Wiener Netze",
    PROVIDER_NETZ_NOE: "Netz Niederösterreich (EVN)",
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
    "CONF_PASSWORD",
    "CONF_PROVIDER",
    "CONF_SCAN_INTERVAL",
    "CONF_USERNAME",
    "DEFAULT_SCAN_INTERVAL",
    "DOMAIN",
    "LOGGER",
    "MIN_SCAN_INTERVAL",
    "OBIS_NAMES",
    "PROVIDERS",
    "PROVIDER_NETZ_NOE",
    "PROVIDER_STROMNETZ_GRAZ",
    "PROVIDER_WIENER_NETZE",
]
