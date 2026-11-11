"""What a provider adds to the integration's config and options flow.

The steps all providers share - choosing the provider, the credentials step and
its errors, the options - are in config_flow.py. Everything in between that
differs between providers is asked from the provider's :class:`ProviderFlow`:
the credentials form, the entry data and its unique id, the check of the
credentials and what follows it, and the options. A provider that needs steps of
its own adds them with a mixin of the config or the options flow, next to its
``ProviderFlow`` in its module; ``flows/__init__.py`` registers both.
"""
from __future__ import annotations

from collections.abc import Mapping
from hashlib import sha256
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
import homeassistant.helpers.config_validation as cv

from ..api.base import SmartmeterClient
from ..api.client import get_client
from ..const import (
    CONF_API_KEY,
    CONF_PROVIDER,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    LOGGER,
    MIN_SCAN_INTERVAL,
    PROVIDERS,
)


def mask(value: str) -> str:
    """Return a loggable hint for a secret, never the secret itself."""
    return f"…{value[-4:]}" if len(value) > 4 else "…"


def scan_interval(entry: ConfigEntry) -> int:
    """Return an entry's scan interval in minutes, for a form's default."""
    current = (
        entry.options.get(CONF_SCAN_INTERVAL)
        or entry.data.get(CONF_SCAN_INTERVAL)
        or DEFAULT_SCAN_INTERVAL
    )
    try:
        return max(int(current), MIN_SCAN_INTERVAL)
    except (TypeError, ValueError):
        LOGGER.warning("Invalid scan interval %s, using the default", current)
        return DEFAULT_SCAN_INTERVAL


class ProviderFlow:
    """A provider's part of the setup and the options.

    The defaults are those of a portal login with user name and password, which
    is checked by logging in and listing the contracts, and the scan interval as
    the only option.
    """

    provider: str = ""

    # Hash the identifier into the unique id instead of using it verbatim: the
    # unique id ends up in the config entry and in logs and must not carry a
    # secret, and a connection string has awkward characters.
    hashed_identifier: bool = False

    @property
    def name(self) -> str:
        """Return the provider's name, as the provider list shows it."""
        return PROVIDERS.get(self.provider, self.provider)

    def credentials_schema(self) -> vol.Schema:
        """Return the credentials form."""
        return vol.Schema(
            {
                vol.Required(CONF_USERNAME): str,
                vol.Required(CONF_PASSWORD): str,
            }
        )

    def credentials_form(
        self, flow: ConfigFlow, errors: dict[str, str] | None = None
    ) -> ConfigFlowResult:
        """Show the credentials form, with the errors of a failed attempt."""
        return flow.async_show_form(
            step_id="credentials",
            data_schema=self.credentials_schema(),
            errors=errors if errors is not None else {},
            description_placeholders={"provider_name": self.name},
        )

    async def async_submit_credentials(
        self, flow: ConfigFlow, user_input: dict[str, Any]
    ) -> ConfigFlowResult | None:
        """Handle submitted credentials in a way of its own, or return None.

        None leaves them to the common check: entry data, unique id, login.
        """
        return None

    def entry_data(self, user_input: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        """Return the identifier the entry is unique by, and the entry data."""
        return user_input[CONF_USERNAME], {**user_input, CONF_PROVIDER: self.provider}

    def entry_label(self, identifier: str) -> str:
        """Return the label in the title of the entry; never a credential."""
        return identifier

    def unique_id(self, identifier: str) -> str:
        """Return the unique id of an entry with this identifier."""
        if self.hashed_identifier:
            return f"{self.provider}_{sha256(identifier.encode()).hexdigest()[:16]}"
        return f"{self.provider}_{identifier.lower()}"

    def client(self, data: Mapping[str, Any]) -> SmartmeterClient:
        """Return the provider's client for entry data."""
        return get_client(self.provider, data)

    async def async_check(self, hass: HomeAssistant, client: SmartmeterClient) -> Any:
        """Check the credentials and return what the setup goes on with.

        By default that is logging in and listing the contracts. The errors of
        the client are shown as the credentials form's errors.
        """
        await hass.async_add_executor_job(client.login)
        return await hass.async_add_executor_job(client.zaehlpunkte)

    async def async_checked(
        self, flow: ConfigFlow, title: str, data: dict[str, Any], checked: Any
    ) -> ConfigFlowResult:
        """Finish the setup after a successful check: create the entry."""
        if not checked:
            LOGGER.error("Config flow: login succeeded but no contracts found")
            return self.credentials_form(flow, {"base": "no_contracts"})
        LOGGER.debug("Config flow: creating entry %s", title)
        return flow.async_create_entry(title=title, data=data)

    async def async_options(
        self, flow: OptionsFlow, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        """Show and save the options: the scan interval."""
        if user_input is not None:
            return flow.async_create_entry(data=user_input)
        return flow.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_SCAN_INTERVAL, default=scan_interval(flow.config_entry)
                    ): cv.positive_int
                }
            ),
        )


class ApiKeyProviderFlow(ProviderFlow):
    """A provider that hands out an API key instead of a portal account."""

    hashed_identifier = True

    def credentials_schema(self) -> vol.Schema:
        """Return a form for the API key alone."""
        return vol.Schema({vol.Required(CONF_API_KEY): str})

    def entry_data(self, user_input: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        """Return the API key as the identifier and the only entry data."""
        identifier = (user_input.get(CONF_API_KEY) or "").strip()
        return identifier, {CONF_PROVIDER: self.provider, CONF_API_KEY: identifier}

    def entry_label(self, identifier: str) -> str:
        """Return a hint of the key, never the key itself."""
        return mask(identifier)
