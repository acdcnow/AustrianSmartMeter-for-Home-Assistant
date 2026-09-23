"""Config flow for the Austria Smartmeter integration."""
from __future__ import annotations

import json
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
from .api.client_awattar import DEFAULT_MARKET_AREA, MARKET_AREAS
from .api.client_selectra import (
    DEFAULT_COUNTRY_CODE,
    QUESTION_SELECT,
    question_options,
)
from .api.dsmr_versions import DEFAULT_DSMR_VERSION, VERSION_LABELS
from .api.errors import SmartmeterError, SmartmeterLoginError
from .const import (
    CONF_API_KEY,
    CONF_COUNTRY_CODE,
    CONF_DSMR_VERSION,
    CONF_ENCRYPTION_KEY,
    CONF_GPNR,
    CONF_MARKET_AREA,
    CONF_METERING_POINTS,
    CONF_PORT,
    CONF_POSTCODE,
    CONF_PROVIDER,
    CONF_SCAN_INTERVAL,
    CONF_SELECTRA_INPUTS,
    CONF_SELECTRA_LABEL,
    CONF_TOKEN,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    LOGGER,
    MIN_SCAN_INTERVAL,
    PROVIDERS,
    PROVIDER_AWATTAR,
    PROVIDER_DSMR,
    PROVIDER_ENERGIEDATEN,
    PROVIDER_ENERGYLIVE,
    PROVIDER_SALZBURGNETZ,
    PROVIDER_SELECTRA,
    PROVIDER_WIENER_NETZE,
)

# Providers that hand out an API key instead of a portal account. They ask for a
# single secret and have no user name.
_API_KEY_PROVIDERS = {PROVIDER_ENERGIEDATEN, PROVIDER_ENERGYLIVE}

# Providers whose identifier is hashed into the unique id instead of being used
# verbatim (a secret, or a connection string with awkward characters).
_HASHED_IDENTIFIERS = {
    PROVIDER_ENERGIEDATEN,
    PROVIDER_ENERGYLIVE,
    PROVIDER_DSMR,
    PROVIDER_SELECTRA,
}


def _credentials_schema(provider: str) -> vol.Schema:
    """Return the credential form that belongs to a provider."""
    if provider == PROVIDER_SELECTRA:
        # A commercial API: the user brings a personal bearer token, and the
        # tariff itself is pinned down afterwards by the API's questionnaire.
        # Country and postcode are collected here because every round trip of
        # that questionnaire costs one call of the monthly quota.
        return vol.Schema(
            {
                vol.Required(CONF_TOKEN): str,
                vol.Optional(CONF_COUNTRY_CODE, default=DEFAULT_COUNTRY_CODE): str,
                vol.Optional(CONF_POSTCODE, default=""): str,
            }
        )
    if provider == PROVIDER_SALZBURGNETZ:
        # A personal key from the service portal and the customer number it was
        # issued for. The metering points are discovered through the API where
        # possible; because that listing is undocumented they can also be typed
        # in, separated by commas.
        return vol.Schema(
            {
                vol.Required(CONF_API_KEY): str,
                vol.Required(CONF_GPNR): str,
                vol.Optional(CONF_METERING_POINTS, default=""): str,
            }
        )
    if provider == PROVIDER_DSMR:
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
    if provider in _API_KEY_PROVIDERS:
        return vol.Schema({vol.Required(CONF_API_KEY): str})
    if provider == PROVIDER_AWATTAR:
        # The market feed has no credential, only a market to pick.
        return vol.Schema(
            {
                vol.Required(
                    CONF_MARKET_AREA, default=DEFAULT_MARKET_AREA
                ): vol.In({code: name for code, (_, name) in MARKET_AREAS.items()})
            }
        )
    return vol.Schema(
        {
            vol.Required(CONF_USERNAME): str,
            vol.Required(CONF_PASSWORD): str,
        }
    )


def _selectra_schema(questions: list[dict[str, Any]]) -> vol.Schema:
    """Build the form for one batch of the Selectra questionnaire.

    The API decides what it asks, so the fields are built from its answer: a
    ``select`` question becomes a dropdown of the options it returned, and
    everything else (``input``, ``text`` and ``json``) is typed in as text.
    """
    fields: dict[Any, Any] = {}
    for question in questions:
        field = str(question.get("field") or "").strip()
        if not field:
            continue
        options = (
            question_options(question)
            if str(question.get("type")) == QUESTION_SELECT
            else {}
        )
        if options:
            fields[vol.Required(field)] = vol.In(
                {key: info["label"] for key, info in options.items()}
            )
        else:
            fields[vol.Required(field)] = str
    return vol.Schema(fields)


def _entry_label(provider: str, identifier: str) -> str:
    """Return the label that goes into the title of the config entry."""
    if provider in _API_KEY_PROVIDERS:
        # Never put the credential itself into the title.
        return _mask(identifier)
    if provider == PROVIDER_AWATTAR:
        return MARKET_AREAS.get(identifier, ("", identifier))[1]
    return identifier


def _unique_id(provider: str, identifier: str) -> str:
    """Return the unique id of a credential.

    A secret or a connection string is hashed: the unique id ends up in the
    config entry and in logs and must not carry the raw value.
    """
    if provider in _HASHED_IDENTIFIERS:
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
        # State of the Selectra qualification questionnaire.
        self._selectra_client = None
        self._selectra_token: str = ""
        self._selectra_answers: dict[str, Any] = {}
        self._selectra_labels: dict[str, str] = {}
        self._selectra_questions: list[dict[str, Any]] = []
        self._selectra_highlight: list[str] = []

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
        that authenticate with an API key (energyLIVE) ask for the key, the DSMR
        customer interface asks for the port it is connected to, and the aWATTar
        feed asks for the market area.
        """
        errors: dict[str, str] = {}
        provider: str = self.context.get(CONF_PROVIDER) or self._provider
        wants_api_key = provider in _API_KEY_PROVIDERS

        if user_input is not None:
            if provider == PROVIDER_SELECTRA:
                return await self._async_selectra_credentials(user_input)
            if provider == PROVIDER_DSMR:
                identifier = (user_input.get(CONF_PORT) or "").strip()
                entry_data = {
                    CONF_PROVIDER: provider,
                    CONF_PORT: identifier,
                    CONF_DSMR_VERSION: user_input.get(
                        CONF_DSMR_VERSION, DEFAULT_DSMR_VERSION
                    ),
                    CONF_ENCRYPTION_KEY: (
                        user_input.get(CONF_ENCRYPTION_KEY) or ""
                    ).strip(),
                }
            elif provider == PROVIDER_SALZBURGNETZ:
                identifier = (user_input.get(CONF_GPNR) or "").strip()
                entry_data = {
                    CONF_PROVIDER: provider,
                    CONF_API_KEY: (user_input.get(CONF_API_KEY) or "").strip(),
                    CONF_GPNR: identifier,
                    CONF_METERING_POINTS: (
                        user_input.get(CONF_METERING_POINTS) or ""
                    ).strip(),
                }
            elif wants_api_key:
                identifier = (user_input.get(CONF_API_KEY) or "").strip()
                entry_data = {CONF_PROVIDER: provider, CONF_API_KEY: identifier}
            elif provider == PROVIDER_AWATTAR:
                identifier = (
                    user_input.get(CONF_MARKET_AREA) or DEFAULT_MARKET_AREA
                ).strip()
                entry_data = {CONF_PROVIDER: provider, CONF_MARKET_AREA: identifier}
            else:
                identifier = user_input[CONF_USERNAME]
                entry_data = {**user_input, CONF_PROVIDER: provider}

            await self.async_set_unique_id(_unique_id(provider, identifier))
            self._abort_if_unique_id_configured()

            try:
                client = get_client(provider, entry_data)
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
                    # Never put a credential into the entry title.
                    label = _entry_label(provider, identifier)
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

    # ------------------------------------------------------------ Selectra

    async def _async_selectra_credentials(
        self, user_input: dict[str, Any]
    ) -> ConfigFlowResult:
        """Start the qualification of a Selectra offer.

        Selectra does not take a customer number: it asks for the details of the
        tariff in a questionnaire and returns the ``inputs`` object that the price
        feed expects. The first round is sent with what this step already knows
        (token, country, postcode), because every round trip costs one call of the
        monthly quota - 60 calls on the free tier.
        """
        token = (user_input.get(CONF_TOKEN) or "").strip()
        country = (
            user_input.get(CONF_COUNTRY_CODE) or DEFAULT_COUNTRY_CODE
        ).strip().lower()
        postcode = (user_input.get(CONF_POSTCODE) or "").strip()

        errors: dict[str, str] = {}
        if not token:
            errors[CONF_TOKEN] = "invalid_input"
        if len(country) != 2 or not country.isascii() or not country.isalpha():
            errors[CONF_COUNTRY_CODE] = "invalid_input"
        if errors:
            return self._selectra_credentials_form(errors)

        self._selectra_token = token
        self._selectra_client = get_client(
            PROVIDER_SELECTRA, {CONF_PROVIDER: PROVIDER_SELECTRA, CONF_TOKEN: token}
        )
        self._selectra_answers = {}
        if country:
            self._selectra_answers[CONF_COUNTRY_CODE] = country
        if postcode:
            self._selectra_answers[CONF_POSTCODE] = postcode

        try:
            response = await self.hass.async_add_executor_job(
                self._selectra_client.qualify, self._selectra_answers
            )
        except SmartmeterLoginError as err:
            LOGGER.warning("Config flow: Selectra rejected the token: %s", err)
            return self._selectra_credentials_form({"base": "invalid_auth"})
        except SmartmeterError as err:
            LOGGER.warning("Config flow: could not qualify with Selectra: %s", err)
            return self._selectra_credentials_form({"base": "cannot_connect"})
        except Exception:  # noqa: BLE001
            LOGGER.exception("Config flow: unexpected error during qualification")
            return self._selectra_credentials_form({"base": "unknown"})

        return await self._async_selectra_continue(response)

    async def async_step_qualification(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show one batch of the Selectra questionnaire and send the answers."""
        if self._selectra_client is None:
            # The flow was resumed after a restart, so the questionnaire state is
            # gone; token, country and postcode have to be entered again.
            LOGGER.warning("Config flow: Selectra flow resumed without state")
            return self._selectra_credentials_form({})

        if user_input is not None:
            errors = self._selectra_remember_answers(user_input)
            if errors:
                return self._selectra_show_form(errors)

            try:
                response = await self.hass.async_add_executor_job(
                    self._selectra_client.qualify, self._selectra_answers
                )
            except SmartmeterLoginError as err:
                LOGGER.warning("Config flow: Selectra rejected the token: %s", err)
                return self._selectra_credentials_form({"base": "invalid_auth"})
            except SmartmeterError as err:
                LOGGER.warning("Config flow: could not qualify with Selectra: %s", err)
                return self._selectra_show_form({"base": "cannot_connect"})
            except Exception:  # noqa: BLE001
                LOGGER.exception("Config flow: unexpected error during qualification")
                return self._selectra_show_form({"base": "unknown"})

            return await self._async_selectra_continue(response)

        return self._selectra_show_form({})

    async def _async_selectra_continue(
        self, response: dict[str, Any]
    ) -> ConfigFlowResult:
        """Ask the next questions, or store the finished qualification."""
        if response.get("done"):
            return await self._async_selectra_create_entry(response.get("inputs"))

        questions = [
            question
            for question in response.get("questions") or []
            if isinstance(question, dict) and str(question.get("field") or "").strip()
        ]
        if not questions:
            LOGGER.error(
                "Config flow: Selectra qualification is not done but asks "
                "nothing: %s",
                response,
            )
            return self._selectra_credentials_form({"base": "unknown"})

        self._selectra_questions = questions
        return self._selectra_show_form({})

    async def _async_selectra_create_entry(self, inputs: Any) -> ConfigFlowResult:
        """Store the qualified offer and check that it yields a price plan."""
        if not isinstance(inputs, dict) or not inputs:
            LOGGER.error("Config flow: Selectra qualification returned no inputs")
            return self._selectra_credentials_form({"base": "unknown"})

        entry_data = {
            CONF_PROVIDER: PROVIDER_SELECTRA,
            CONF_TOKEN: self._selectra_token,
            CONF_SELECTRA_INPUTS: inputs,
            CONF_SELECTRA_LABEL: self._selectra_label(),
        }
        await self.async_set_unique_id(
            _unique_id(
                PROVIDER_SELECTRA, json.dumps(inputs, sort_keys=True, default=str)
            )
        )
        self._abort_if_unique_id_configured()

        errors: dict[str, str] = {}
        try:
            client = get_client(PROVIDER_SELECTRA, entry_data)
            await self.hass.async_add_executor_job(client.login)
            contracts = await self.hass.async_add_executor_job(client.zaehlpunkte)
        except SmartmeterLoginError as err:
            LOGGER.warning("Config flow: login failed for Selectra: %s", err)
            errors["base"] = "invalid_auth"
        except SmartmeterError as err:
            LOGGER.warning("Config flow: the Selectra offer has no prices: %s", err)
            errors["base"] = "cannot_connect"
        except Exception:  # noqa: BLE001
            LOGGER.exception("Config flow: unexpected error during login")
            errors["base"] = "unknown"
        else:
            if contracts:
                label = entry_data[CONF_SELECTRA_LABEL]
                LOGGER.debug("Config flow: creating entry for Selectra (%s)", label)
                return self.async_create_entry(
                    title=f"{PROVIDERS.get(PROVIDER_SELECTRA, PROVIDER_SELECTRA)} "
                    f"({label})",
                    data=entry_data,
                )
            LOGGER.error("Config flow: login succeeded but no contracts found")
            errors["base"] = "no_contracts"

        # An entry is only stored once its offer really returns prices; the
        # qualification is already paid for, so the token can be corrected here.
        return self._selectra_credentials_form(errors)

    def _selectra_remember_answers(
        self, user_input: dict[str, Any]
    ) -> dict[str, str]:
        """Store the answers of the current questions, return field errors."""
        errors: dict[str, str] = {}
        for question in self._selectra_questions:
            field = str(question.get("field") or "").strip()
            if not field or field not in user_input:
                continue

            raw = user_input[field]
            is_select = str(question.get("type")) == QUESTION_SELECT
            options = question_options(question) if is_select else {}
            chosen = options.get(str(raw))
            if chosen is not None:
                # A multiple choice answer is stored by its raw value, because
                # the API usually expects an id and shows a label.
                self._selectra_answers[field] = chosen["value"]
                self._selectra_labels[field] = chosen["label"]
                if chosen["label"] not in self._selectra_highlight:
                    self._selectra_highlight.append(chosen["label"])
                continue

            text = str(raw).strip()
            if str(question.get("type")) == "json":
                try:
                    value: Any = json.loads(text)
                except ValueError:
                    errors[field] = "invalid_input"
                    continue
            else:
                value = text
            self._selectra_answers[field] = value
            self._selectra_labels[field] = text

        return errors

    def _selectra_label(self) -> str:
        """Return a readable name of the qualified offer."""
        parts = list(self._selectra_highlight[:3])
        country = str(self._selectra_answers.get(CONF_COUNTRY_CODE) or "").upper()
        postcode = str(self._selectra_answers.get(CONF_POSTCODE) or "").strip()
        location = " ".join(part for part in (country, postcode) if part)
        if location:
            parts.append(location)
        return " · ".join(parts) or "qualified offer"

    def _selectra_show_form(self, errors: dict[str, str]) -> ConfigFlowResult:
        """Render the current batch of the questionnaire."""
        question_list = "\n".join(
            f"- {question.get('label') or question.get('field')}"
            for question in self._selectra_questions
        )
        return self.async_show_form(
            step_id="qualification",
            data_schema=_selectra_schema(self._selectra_questions),
            errors=errors,
            description_placeholders={"question_list": question_list},
        )

    def _selectra_credentials_form(self, errors: dict[str, str]) -> ConfigFlowResult:
        """Render the token form of the Selectra provider again."""
        return self.async_show_form(
            step_id="credentials",
            data_schema=_credentials_schema(PROVIDER_SELECTRA),
            errors=errors,
            description_placeholders={
                "provider_name": PROVIDERS.get(PROVIDER_SELECTRA, PROVIDER_SELECTRA)
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
