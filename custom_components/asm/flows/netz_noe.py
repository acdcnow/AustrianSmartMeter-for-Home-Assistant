"""Setup of Netz Niederösterreich (EVN): the user name and password of the smart meter portal."""
from __future__ import annotations

from ..const import PROVIDER_NETZ_NOE
from .base import ProviderFlow


class NetzNoeFlow(ProviderFlow):
    """Netz Niederösterreich (EVN) logs in to its portal with user name and password."""

    provider = PROVIDER_NETZ_NOE
