"""Setup of a DSMR / P1 meter's customer interface, read locally."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from ..api.dsmr_versions import (
    AUTO_BAUDRATE,
    BAUDRATE_LABELS,
    DEFAULT_DSMR_VERSION,
    VERSION_LABELS,
)
from ..const import (
    CONF_BAUDRATE,
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
    # A serial port that stays silent is local trouble - cable, port, baud rate -
    # not an internet connection.
    connection_error = "cannot_connect_local"

    def credentials_schema(self) -> vol.Schema:
        """Return the port, the DSMR version, the key and the baud rate."""
        # A customer interface is reached by cable or over the LAN, so what is
        # needed is a port and - for encrypted meters - the meter's AES key. The
        # baud rate defaults to the DSMR version's own; it is only asked because
        # a converter or an unusual meter can differ.
        return vol.Schema(
            {
                vol.Required(CONF_PORT): str,
                vol.Required(
                    CONF_DSMR_VERSION, default=DEFAULT_DSMR_VERSION
                ): vol.In(VERSION_LABELS),
                vol.Optional(CONF_ENCRYPTION_KEY, default=""): str,
                vol.Optional(CONF_BAUDRATE, default=AUTO_BAUDRATE): vol.In(
                    BAUDRATE_LABELS
                ),
            }
        )

    def entry_data(self, user_input: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        """Return the port as the identifier, with version, key and baud rate."""
        identifier = (user_input.get(CONF_PORT) or "").strip()
        return identifier, {
            CONF_PROVIDER: self.provider,
            CONF_PORT: identifier,
            CONF_DSMR_VERSION: user_input.get(CONF_DSMR_VERSION, DEFAULT_DSMR_VERSION),
            CONF_ENCRYPTION_KEY: (user_input.get(CONF_ENCRYPTION_KEY) or "").strip(),
            CONF_BAUDRATE: user_input.get(CONF_BAUDRATE, AUTO_BAUDRATE),
        }
