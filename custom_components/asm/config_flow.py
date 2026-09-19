"""Config flow for the Austria Smartmeter integration."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
import homeassistant.helpers.config_validation as cv

from .api.client import get_client
from .api.errors import SmartmeterError, SmartmeterLoginError
from .const import (
    CONF_PROVIDER,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    LOGGER,
    MIN_SCAN_INTERVAL,
    PROVIDERS,
    PROVIDER_WIENER_NETZE,
)


class AustriaSmartMeterConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Austria Smartmeter."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
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
        """Handle the credentials step."""
        errors: dict[str, str] = {}
        provider: str = self.context.get(CONF_PROVIDER) or self._provider

        if user_input is not None:
            username = user_input[CONF_USERNAME]
            password = user_input[CONF_PASSWORD]

            await self.async_set_unique_id(f"{provider}_{username.lower()}")
            self._abort_if_unique_id_configured()

            try:
                client = get_client(provider, username, password)
                await self.hass.async_add_executor_job(client.login)
                contracts = await self.hass.async_add_executor_job(client.zaehlpunkte)
            except SmartmeterLoginError as err:
                LOGGER.warning("Config flow: login failed for %s: %s", provider, err)
                errors["base"] = "invalid_auth"
            except SmartmeterError as err:
                LOGGER.warning("Config flow: could not query %s: %s", provider, err)
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                LOGGER.exception("Config flow: unexpected error during login")
                errors["base"] = "unknown"
            else:
                if not contracts:
                    LOGGER.error("Config flow: login succeeded but no contracts found")
                    errors["base"] = "no_contracts"
                else:
                    LOGGER.debug(
                        "Config flow: creating entry for %s (%s)", username, provider
                    )
                    return self.async_create_entry(
                        title=f"{PROVIDERS.get(provider, provider)} ({username})",
                        data={**user_input, CONF_PROVIDER: provider},
                    )

        return self.async_show_form(
            step_id="credentials",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USERNAME): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
            description_placeholders={
                "provider_name": PROVIDERS.get(provider, provider)
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow."""
        return AustriaSmartMeterOptionsFlow()


class AustriaSmartMeterOptionsFlow(OptionsFlow):
    """Handle the options flow."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        # `config_entry` is a read only property provided by Home Assistant, it
        # is only available once the flow has been initialised.
        entry = self.config_entry
        current = (
            entry.options.get(CONF_SCAN_INTERVAL)
            or entry.data.get(CONF_SCAN_INTERVAL)
            or DEFAULT_SCAN_INTERVAL
        )
        try:
            current = max(int(current), MIN_SCAN_INTERVAL)
        except (TypeError, ValueError):
            LOGGER.warning("Invalid scan interval %s, using the default", current)
            current = DEFAULT_SCAN_INTERVAL

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {vol.Optional(CONF_SCAN_INTERVAL, default=current): cv.positive_int}
            ),
        )
