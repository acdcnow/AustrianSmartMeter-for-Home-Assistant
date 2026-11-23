"""Setup of energiedaten.at: the API key of its dashboard."""
from __future__ import annotations

from ..const import PROVIDER_ENERGIEDATEN
from .base import ApiKeyProviderFlow


class EnergiedatenFlow(ApiKeyProviderFlow):
    """energiedaten.at authenticates with an API key."""

    provider = PROVIDER_ENERGIEDATEN
