"""Factory for Smartmeter clients."""
from collections.abc import Mapping
from typing import Any

from .base import SmartmeterClient
from .client_energylive import EnergyliveClient
from .client_noe import NetzNoeClient
from .client_wn import WienerNetzeClient
from ..const import (
    CONF_API_KEY,
    CONF_DSMR_VERSION,
    CONF_ENCRYPTION_KEY,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_USERNAME,
    PROVIDER_DSMR,
    PROVIDER_ENERGYLIVE,
    PROVIDER_NETZ_NOE,
    PROVIDER_WIENER_NETZE,
)

# Re-export errors for compatibility
from .errors import SmartmeterLoginError, SmartmeterConnectionError, SmartmeterQueryError

def get_client(
    provider: str, data: Mapping[str, Any] | None = None
) -> SmartmeterClient:
    """Return the client of a provider, built from its config entry data.

    The factory takes the entry data itself rather than a growing list of
    keyword arguments: every provider stores something different (portal
    credentials, an API key, or a port and a key).
    """
    data = data or {}

    if provider == PROVIDER_NETZ_NOE:
        return NetzNoeClient(data.get(CONF_USERNAME), data.get(CONF_PASSWORD))

    if provider == PROVIDER_ENERGYLIVE:
        return EnergyliveClient(data.get(CONF_API_KEY))

    if provider == PROVIDER_DSMR:
        # Imported on demand: this provider needs dsmr-parser and serialx, and a
        # requirement that cannot be installed must not break the other ones.
        from .client_dsmr import DsmrClient

        return DsmrClient(
            data.get(CONF_PORT),
            data.get(CONF_DSMR_VERSION),
            data.get(CONF_ENCRYPTION_KEY),
        )

    # Default to Wiener Netze
    return WienerNetzeClient(data.get(CONF_USERNAME), data.get(CONF_PASSWORD))

# For backward compatibility with existing imports in config_flow (initially)
Smartmeter = WienerNetzeClient