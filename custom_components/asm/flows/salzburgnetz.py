"""Setup of Salzburg Netz: API key, customer number and metering points."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from ..const import (
    CONF_API_KEY,
    CONF_GPNR,
    CONF_METERING_POINTS,
    CONF_PROVIDER,
    PROVIDER_SALZBURGNETZ,
)
from .base import ProviderFlow


class SalzburgNetzFlow(ProviderFlow):
    """Salzburg Netz: an API key and the customer number it was issued for."""

    provider = PROVIDER_SALZBURGNETZ

    def credentials_schema(self) -> vol.Schema:
        """Return the API key, the customer number and the metering points."""
        # The metering points are discovered through the API where possible;
        # because that listing is undocumented they can also be typed in,
        # separated by commas.
        return vol.Schema(
            {
                vol.Required(CONF_API_KEY): str,
                vol.Required(CONF_GPNR): str,
                vol.Optional(CONF_METERING_POINTS, default=""): str,
            }
        )

    def entry_data(self, user_input: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        """Return the customer number as the identifier, with key and metering points."""
        identifier = (user_input.get(CONF_GPNR) or "").strip()
        return identifier, {
            CONF_PROVIDER: self.provider,
            CONF_API_KEY: (user_input.get(CONF_API_KEY) or "").strip(),
            CONF_GPNR: identifier,
            CONF_METERING_POINTS: (user_input.get(CONF_METERING_POINTS) or "").strip(),
        }
