"""Config flow for the Austria Smartmeter integration."""
from __future__ import annotations

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
from homeassistant.core import callback
import homeassistant.helpers.config_validation as cv

from .api.client import get_client
from .api.errors import SmartmeterError, SmartmeterLoginError
from .const import (
    CONF_API_KEY,
    CONF_PROVIDER,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    LOGGER,
    MIN_SCAN_INTERVAL,
    PROVIDERS,
    PROVIDER_ENERGIEDATEN,
    PROVIDER_WIENER_NETZE,
)

# Providers that hand out an API key instead of a portal account. They ask for a
# single secret and have no user name.
_API_KEY_PROVIDERS = {PROVIDER_ENERGIEDATEN}


def _credentials_schema(provider: str) -> vol.Schema:
    """Return the credential form that belongs to a provider."""
    if provider in _API_KEY_PROVIDERS:
        return vol.Schema({vol.Required(CONF_API_KEY): str})
    return vol.Schema(
        {
            vol.Required(CONF_USERNAME): str,
            vol.Required(CONF_PASSWORD): str,
        }
    )


def _unique_id(provider: str, identifier: str) -> str:
    """Return the unique id of a credential.

    An API key is hashed: the unique id ends up in the config entry and in logs
    and must not carry the secret itself.
    """
    if provider in _API_KEY_PROVIDERS:
        return f"{provider}_{sha256(identifier.encode()).hexdigest()[:16]}"
    return f"{provider}_{identifier.lower()}"


def _mask(value: str) -> str:
    """Return a loggable hint for a secret, never the secret itself."""
    return f"…{value[-4:]}" if len(value) > 4 else "…"


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
        """Handle the credentials step.

        Providers with a portal login ask for user name and password, providers
        that authenticate with an API key (energiedaten.at) ask for the key.
        """
        errors: dict[str, str] = {}
        provider: str = self.context.get(CONF_PROVIDER) or self._provider
        wants_api_key = provider in _API_KEY_PROVIDERS

        if user_input is not None:
            if wants_api_key:
                identifier = (user_input.get(CONF_API_KEY) or "").strip()
                client = get_client(provider, None, None, api_key=identifier)
                entry_data = {CONF_PROVIDER: provider, CONF_API_KEY: identifier}
            else:
                identifier = user_input[CONF_USERNAME]
                client = get_client(provider, identifier, user_input[CONF_PASSWORD])
                entry_data = {**user_input, CONF_PROVIDER: provider}

            await self.async_set_unique_id(_unique_id(provider, identifier))
            self._abort_if_unique_id_configured()

            try:
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
                    # Never put the credential itself into the entry title.
                    label = _mask(identifier) if wants_api_key else identifier
                    LOGGER.debug(
                        "Config flow: creating entry for %s (%s)", label, provider
                    )
                    return self.async_create_entry(
                        title=f"{PROVIDERS.get(provider, provider)} ({label})",
                        data=entry_data,
                    )

        return self.async_show_form(
            step_id="credentials",
            data_schema=_credentials_schema(provider),
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
