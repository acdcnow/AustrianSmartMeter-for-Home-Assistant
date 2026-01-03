"""
    API constants for Austria Smartmeter (Wiener Netze).
"""
import enum

PAGE_URL = "https://smartmeter-web.wienernetze.at/"
API_CONFIG_URL = "https://smartmeter-web.wienernetze.at/assets/app-config.json"
API_URL_ALT = "https://service.wienernetze.at/sm/api/"
API_URL = "https://api.wstw.at/gateway/WN_SMART_METER_PORTAL_API_B2C/1.0"
API_URL_B2B = "https://api.wstw.at/gateway/WN_SMART_METER_PORTAL_API_B2B/1.0"
REDIRECT_URI = "https://smartmeter-web.wienernetze.at/"
API_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S.%f"
AUTH_URL = "https://log.wien/auth/realms/logwien/protocol/openid-connect/"

LOGIN_ARGS = {
    "client_id": "wn-smartmeter",
    "redirect_uri": REDIRECT_URI,
    "response_mode": "fragment",
    "response_type": "code",
    "scope": "openid",
    "nonce": "",
    "code_challenge": "",
    "code_challenge_method": "S256"
}

VALID_OBIS_CODES = {
    "1-1:1.8.0", 
    "1-1:1.9.0", 
    "1-1:2.8.0", 
    "1-1:2.9.0"
}

class Resolution(enum.Enum):
    """Possible resolution for consumption data."""
    HOUR = "HOUR"
    QUARTER_HOUR = "QUARTER-HOUR"

class ValueType(enum.Enum):
    """Possible 'wertetyp' for querying historical data."""
    METER_READ = "METER_READ"
    DAY = "DAY"
    QUARTER_HOUR = "QUARTER_HOUR"

class AnlagenType(enum.Enum):
    """Possible types for the zaehlpunkte."""
    CONSUMING = "TAGSTROM"
    FEEDING = "BEZUG"