"""Constants for the Austria Smartmeter integration."""
import logging

DOMAIN = "asm"
LOGGER = logging.getLogger(__package__)

# Configuration
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
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

# Options (Zwingend erforderlich für Config Flow!)
CONF_SCAN_INTERVAL = "scan_interval"
DEFAULT_SCAN_INTERVAL = 60 * 6  # 6 Stunden
MIN_SCAN_INTERVAL = 60

# Attributes
ATTR_ZAEHLPUNKT = "zaehlpunkt"
ATTR_OBIS_CODE = "obis_code"
ATTR_UNIT = "unit"

# OBIS Mappings
OBIS_NAMES = {
    "1-1:1.8.0": "Energy Consumption Total",
    "1-1:1.9.0": "Energy Consumption Interval",
    "1-1:2.8.0": "Energy Production Total",
    "1-1:2.9.0": "Energy Production Interval",
}