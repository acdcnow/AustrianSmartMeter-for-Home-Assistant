"""Config flow for Austria Smartmeter integration."""
from __future__ import annotations
from typing import Any
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
import homeassistant.helpers.config_validation as cv

# API Imports
from .api.client import get_client, SmartmeterLoginError

# Constants Imports
from .const import (
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    LOGGER,
    CONF_PROVIDER,
    PROVIDERS,
    PROVIDER_WIENER_NETZE
)

class AustriaSmartMeterConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Austria Smartmeter."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Handle the initial step (Provider Selection)."""
        if user_input is not None:
            self.context["provider"] = user_input[CONF_PROVIDER]
            return await self.async_step_credentials()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required(CONF_PROVIDER, default=PROVIDER_WIENER_NETZE): vol.In(PROVIDERS)
            })
        )

    async def async_step_credentials(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Handle credentials input."""
        errors: dict[str, str] = {}
        provider = self.context.get("provider", PROVIDER_WIENER_NETZE)

        if user_input is not None:
            username = user_input[CONF_USERNAME]
            password = user_input[CONF_PASSWORD]
            
            await self.async_set_unique_id(f"{provider}_{username.lower()}")
            self._abort_if_unique_id_configured()

            try:
                client = get_client(provider, username, password)
                await self.hass.async_add_executor_job(client.login)
                
                contracts = await self.hass.async_add_executor_job(client.zaehlpunkte)

                if not contracts:
                    errors["base"] = "no_contracts"
                else:
                    data = user_input.copy()
                    data[CONF_PROVIDER] = provider
                    return self.async_create_entry(
                        title=f"{PROVIDERS.get(provider, provider)} ({username})",
                        data=data,
                    )

            except SmartmeterLoginError:
                errors["base"] = "invalid_auth"
            except Exception:
                LOGGER.exception("Unexpected exception during config flow")
                errors["base"] = "cannot_connect"

        return self.async_show_form(
            step_id="credentials",
            data_schema=vol.Schema({
                vol.Required(CONF_USERNAME): str,
                vol.Required(CONF_PASSWORD): str,
            }),
            errors=errors,
            description_placeholders={"provider_name": PROVIDERS.get(provider, provider)}
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Get the options flow for this handler."""
        return AustriaSmartMeterOptionsFlowHandler(config_entry)


class AustriaSmartMeterOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle options flow."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialize options flow."""
        # FIX: Variable umbenannt zu self.entry, um Konflikt mit Property 'config_entry' zu vermeiden
        self.entry = config_entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        try:
            # FIX: Zugriff jetzt über self.entry statt self.config_entry
            current = self.entry.options.get(CONF_SCAN_INTERVAL)
            
            if current is None:
                current = self.entry.data.get(CONF_SCAN_INTERVAL)
            
            if current is None:
                current = DEFAULT_SCAN_INTERVAL
            
            current = int(current)

        except Exception as e:
            LOGGER.error(f"Failed to read current options: {e}. Using defaults.")
            current = int(DEFAULT_SCAN_INTERVAL)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Optional(CONF_SCAN_INTERVAL, default=current): cv.positive_int
            })
        )
