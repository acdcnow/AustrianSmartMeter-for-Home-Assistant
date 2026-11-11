"""Setup of energyLIVE (smartENERGY): its API key."""
from __future__ import annotations

from ..const import PROVIDER_ENERGYLIVE
from .base import ApiKeyProviderFlow


class EnergyliveFlow(ApiKeyProviderFlow):
    """energyLIVE (smartENERGY) authenticates with an API key."""

    provider = PROVIDER_ENERGYLIVE
