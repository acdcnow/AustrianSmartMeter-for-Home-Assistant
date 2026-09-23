"""Factory for Smartmeter clients."""
from .base import SmartmeterClient
from .client_energylive import EnergyliveClient
from .client_noe import NetzNoeClient
from .client_wn import WienerNetzeClient
from ..const import (
    PROVIDER_ENERGYLIVE,
    PROVIDER_NETZ_NOE,
    PROVIDER_WIENER_NETZE,
)

# Re-export errors for compatibility
from .errors import SmartmeterLoginError, SmartmeterConnectionError, SmartmeterQueryError

def get_client(
    provider: str,
    username: str | None = None,
    password: str | None = None,
    api_key: str | None = None,
) -> SmartmeterClient:
    """Return the correct client based on provider.

    ``api_key`` is only used by providers that authenticate with a token instead
    of a portal login (energyLIVE).
    """
    if provider == PROVIDER_NETZ_NOE:
        return NetzNoeClient(username, password)

    if provider == PROVIDER_ENERGYLIVE:
        return EnergyliveClient(api_key, password)

    # Default to Wiener Netze
    return WienerNetzeClient(username, password)

# For backward compatibility with existing imports in config_flow (initially)
Smartmeter = WienerNetzeClient