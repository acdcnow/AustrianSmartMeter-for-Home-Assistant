"""Setup of Selectra tariff planning: a token and the API's questionnaire."""
from __future__ import annotations

import json
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from ..api.client_selectra import (
    DEFAULT_COUNTRY_CODE,
    QUESTION_SELECT,
    question_options,
)
from ..api.errors import SmartmeterError, SmartmeterLoginError
from ..const import (
    CONF_COUNTRY_CODE,
    CONF_POSTCODE,
    CONF_PROVIDER,
    CONF_SELECTRA_INPUTS,
    CONF_SELECTRA_LABEL,
    CONF_TOKEN,
    LOGGER,
    PROVIDER_SELECTRA,
)
from .base import ProviderFlow


class SelectraFlow(ProviderFlow):
    """Selectra qualifies the tariff in a questionnaire (SelectraConfigFlowMixin)."""

    provider = PROVIDER_SELECTRA
    hashed_identifier = True

    def credentials_schema(self) -> vol.Schema:
        """Return the token form, with country and postcode."""
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

    async def async_submit_credentials(
        self, flow: ConfigFlow, user_input: dict[str, Any]
    ) -> ConfigFlowResult | None:
        """Start the questionnaire instead of the common check."""
        return await flow._async_selectra_credentials(user_input)


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


class SelectraConfigFlowMixin:
    """The questionnaire steps of the config flow."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialize the state of the qualification questionnaire."""
        super().__init__(*args, **kwargs)
        self._selectra_client = None
        self._selectra_token: str = ""
        self._selectra_answers: dict[str, Any] = {}
        self._selectra_labels: dict[str, str] = {}
        self._selectra_questions: list[dict[str, Any]] = []
        self._selectra_highlight: list[str] = []

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
        self._selectra_client = SelectraFlow().client(
            {CONF_PROVIDER: PROVIDER_SELECTRA, CONF_TOKEN: token}
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

        provider_flow = SelectraFlow()
        entry_data = {
            CONF_PROVIDER: PROVIDER_SELECTRA,
            CONF_TOKEN: self._selectra_token,
            CONF_SELECTRA_INPUTS: inputs,
            CONF_SELECTRA_LABEL: self._selectra_label(),
        }
        await self.async_set_unique_id(
            provider_flow.unique_id(json.dumps(inputs, sort_keys=True, default=str))
        )
        self._abort_if_unique_id_configured()

        errors: dict[str, str] = {}
        try:
            client = provider_flow.client(entry_data)
            contracts = await provider_flow.async_check(self.hass, client)
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
                    title=f"{provider_flow.name} ({label})", data=entry_data
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
        return SelectraFlow().credentials_form(self, errors)
