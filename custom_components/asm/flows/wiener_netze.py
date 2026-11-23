"""Setup of Wiener Netze: the e-mail address and password of the smart meter portal."""
from __future__ import annotations

from ..const import PROVIDER_WIENER_NETZE
from .base import ProviderFlow


class WienerNetzeFlow(ProviderFlow):
    """Wiener Netze logs in to its portal with user name and password."""

    provider = PROVIDER_WIENER_NETZE
