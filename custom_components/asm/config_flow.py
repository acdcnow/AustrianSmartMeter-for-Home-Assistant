"""Config flow for the Austria Smartmeter integration.

The steps every provider goes through are here: choosing the provider, the
credentials and their check, and the options. What differs between providers is
in their modules in ``flows`` (see flows/base.py), and the steps a provider adds
come in with its mixins.
"""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback

from .api.errors import SmartmeterError, SmartmeterLoginError
from .const import (
    CONF_PROVIDER,
    DOMAIN,
    LOGGER,
    PROVIDERS,
    PROVIDER_WIENER_NETZE,
)
from .flows import CONFIG_FLOW_MIXINS, OPTIONS_FLOW_MIXINS, provider_flow


class AustriaSmartMeterConfigFlow(*CONFIG_FLOW_MIXINS, ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Austria Smartmeter."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        super().__init__()
        self._provider: str = PROVIDER_WIENER_NETZE

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step (provider selection)."""
        if user_input is not None:
            self._provider = user_input[CONF_PROVIDER]
            # Kept in the flow context so the choice survives a restart while
            # the flow is waiting for the credentials.
            self.context[CONF_PROVIDER] = self._provider
            return await self.async_step_credentials()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_PROVIDER, default=PROVIDER_WIENER_NETZE
                    ): vol.In(PROVIDERS)
                }
            ),
        )

    async def async_step_credentials(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the credentials step.

        The provider decides what it asks for (a portal login, an API key, a port
        or a market area), what the entry stores and is unique by, how the
        credentials are checked and what follows a successful check.
        """
        flow = provider_flow(self.context.get(CONF_PROVIDER) or self._provider)
        if user_input is None:
            return flow.credentials_form(self)

        if (result := await flow.async_submit_credentials(self, user_input)) is not None:
            return result

        identifier, entry_data = flow.entry_data(user_input)
        await self.async_set_unique_id(flow.unique_id(identifier))
        self._abort_if_unique_id_configured()

        errors: dict[str, str] = {}
        try:
            checked = await flow.async_check(self.hass, flow.client(entry_data))
        except SmartmeterLoginError as err:
            LOGGER.warning("Config flow: login failed for %s: %s", flow.provider, err)
            errors["base"] = "invalid_auth"
        except SmartmeterError as err:
            LOGGER.warning("Config flow: could not query %s: %s", flow.provider, err)
            errors["base"] = "cannot_connect"
        except Exception:  # noqa: BLE001
            LOGGER.exception("Config flow: unexpected error during login")
            errors["base"] = "unknown"
        else:
            # Never put a credential into the entry title.
            title = f"{flow.name} ({flow.entry_label(identifier)})"
            return await flow.async_checked(self, title, entry_data, checked)

        return flow.credentials_form(self, errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow."""
        return AustriaSmartMeterOptionsFlow()


class AustriaSmartMeterOptionsFlow(*OPTIONS_FLOW_MIXINS, OptionsFlow):
    """Handle the options flow; the provider decides what it offers."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        provider = self.config_entry.data.get(CONF_PROVIDER, PROVIDER_WIENER_NETZE)
        return await provider_flow(provider).async_options(self, user_input)
