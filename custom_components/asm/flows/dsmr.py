"""Setup of a DSMR / P1 meter's customer interface, read locally."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from ..api.dsmr_versions import DEFAULT_DSMR_VERSION, VERSION_LABELS
from ..const import (
    CONF_DSMR_VERSION,
    CONF_ENCRYPTION_KEY,
    CONF_PORT,
    CONF_PROVIDER,
    PROVIDER_DSMR,
)
from .base import ProviderFlow


class DsmrFlow(ProviderFlow):
    """A DSMR / P1 meter's customer interface."""

    provider = PROVIDER_DSMR
    hashed_identifier = True

    def credentials_schema(self) -> vol.Schema:
        """Return the port, the DSMR version and the key of an encrypted meter."""
        # A customer interface is reached by cable or over the LAN, so what is
        # needed is a port and - for encrypted meters - the meter's AES key.
        return vol.Schema(
            {
                vol.Required(CONF_PORT): str,
                vol.Required(
                    CONF_DSMR_VERSION, default=DEFAULT_DSMR_VERSION
                ): vol.In(VERSION_LABELS),
                vol.Optional(CONF_ENCRYPTION_KEY, default=""): str,
            }
        )

    def entry_data(self, user_input: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        """Return the port as the identifier, with version and key."""
        identifier = (user_input.get(CONF_PORT) or "").strip()
        return identifier, {
            CONF_PROVIDER: self.provider,
            CONF_PORT: identifier,
            CONF_DSMR_VERSION: user_input.get(CONF_DSMR_VERSION, DEFAULT_DSMR_VERSION),
            CONF_ENCRYPTION_KEY: (user_input.get(CONF_ENCRYPTION_KEY) or "").strip(),
        }
