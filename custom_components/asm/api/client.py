"""Factory for Smartmeter clients."""
from .base import SmartmeterClient
from .client_wn import WienerNetzeClient
from .client_noe import NetzNoeClient
from ..const import PROVIDER_WIENER_NETZE, PROVIDER_NETZ_NOE

# Re-export errors for compatibility
from .errors import SmartmeterLoginError, SmartmeterConnectionError, SmartmeterQueryError

def get_client(provider: str, username, password) -> SmartmeterClient:
    """Return the correct client based on provider."""
    if provider == PROVIDER_NETZ_NOE:
        return NetzNoeClient(username, password)
    
    # Default to Wiener Netze
    return WienerNetzeClient(username, password)

# For backward compatibility with existing imports in config_flow (initially)
Smartmeter = WienerNetzeClient