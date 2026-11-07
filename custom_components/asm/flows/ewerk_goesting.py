"""Setup of E-Werk Gösting: the e-mail address and password of the customer portal (mein-portal.at)."""
from __future__ import annotations

from ..const import PROVIDER_EWERK_GOESTING
from .base import ProviderFlow


class EwerkGoestingFlow(ProviderFlow):
    """E-Werk Gösting logs in to its portal with user name and password."""

    provider = PROVIDER_EWERK_GOESTING
