"""Setup of the aWATTar market price feed."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from ..api.client_awattar import DEFAULT_MARKET_AREA, MARKET_AREAS
from ..const import CONF_MARKET_AREA, CONF_PROVIDER, PROVIDER_AWATTAR
from .base import ProviderFlow


class AwattarFlow(ProviderFlow):
    """The market feed has no credential, only a market to pick."""

    provider = PROVIDER_AWATTAR

    def credentials_schema(self) -> vol.Schema:
        """Return the market area to pick."""
        return vol.Schema(
            {
                vol.Required(
                    CONF_MARKET_AREA, default=DEFAULT_MARKET_AREA
                ): vol.In({code: name for code, (_, name) in MARKET_AREAS.items()})
            }
        )

    def entry_data(self, user_input: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        """Return the market area as the identifier and the only entry data."""
        identifier = (user_input.get(CONF_MARKET_AREA) or DEFAULT_MARKET_AREA).strip()
        return identifier, {CONF_PROVIDER: self.provider, CONF_MARKET_AREA: identifier}

    def entry_label(self, identifier: str) -> str:
        """Return the market's name."""
        return MARKET_AREAS.get(identifier, ("", identifier))[1]
