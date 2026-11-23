"""Setup and options of Salzburg Netz: API key, metering points and settings."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import HomeAssistant
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from ..api.base import SmartmeterClient
from ..api.client_salzburgnetz import (
    DEFAULT_HISTORY,
    DEFAULT_REREAD_DAYS,
    HISTORY_CHOICES,
    MAX_REREAD_DAYS,
    MIN_REREAD_DAYS,
    invalid_metering_points,
    limit_reread_days,
    normalise_metering_points,
)
from ..api.errors import SmartmeterConnectionError, SmartmeterQueryError
from ..const import (
    CONF_API_KEY,
    CONF_GPNR,
    CONF_HISTORY,
    CONF_METERING_POINTS,
    CONF_PROVIDER,
    CONF_REREAD_DAYS,
    CONF_SCAN_INTERVAL,
    LOGGER,
    PROVIDER_SALZBURGNETZ,
)
from .base import ProviderFlow, scan_interval

# What SalzburgNetzFlow.async_check returns when the customer number is refused.
_REFUSED = object()


class SalzburgNetzFlow(ProviderFlow):
    """Salzburg Netz: an API key, then metering points and settings.

    The credentials are checked by logging in, which lists the metering points of
    the customer number. The next step (SalzburgNetzConfigFlowMixin) offers them
    with what the API says about each of them, together with the settings, which
    the options (SalzburgNetzOptionsFlowMixin) can change later.
    """

    provider = PROVIDER_SALZBURGNETZ

    def credentials_schema(self) -> vol.Schema:
        """Return the API key and the customer number it was issued for."""
        return vol.Schema(
            {
                vol.Required(CONF_API_KEY): str,
                vol.Required(CONF_GPNR): str,
            }
        )

    def entry_data(self, user_input: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        """Return the customer number as the identifier, with the API key."""
        identifier = (user_input.get(CONF_GPNR) or "").strip()
        return identifier, {
            CONF_PROVIDER: self.provider,
            CONF_API_KEY: (user_input.get(CONF_API_KEY) or "").strip(),
            CONF_GPNR: identifier,
        }

    async def async_check(self, hass: HomeAssistant, client: SmartmeterClient) -> Any:
        """Log in and return the listed metering points with what the API says.

        None when the API did not answer the listing, so the setup can go on with
        metering points typed in. A rejected key or customer number stops it. The
        listing's request carries nothing but the customer number, so any other
        refusal of it (HTTP 400, 403, 404, 422) is one of the customer number,
        and metering points typed in would be refused the same way.
        """
        try:
            await hass.async_add_executor_job(client.login)
        except SmartmeterConnectionError as err:
            LOGGER.warning("Config flow: Salzburg Netz did not list the metering points: %s", err)
            return None
        except SmartmeterQueryError as err:
            LOGGER.warning("Config flow: Salzburg Netz refused the customer number: %s", err)
            return _REFUSED
        return await hass.async_add_executor_job(client.describe_metering_points)

    async def async_checked(
        self, flow: ConfigFlow, title: str, data: dict[str, Any], checked: Any
    ) -> ConfigFlowResult:
        """Go on with the metering points and settings, or ask for the credentials again."""
        if checked is _REFUSED:
            return self.credentials_form(flow, {"base": "customer_number_refused"})
        return await flow._async_salzburgnetz_choose(data, title, checked)

    async def async_options(
        self, flow: OptionsFlow, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        """Show the metering points and settings (SalzburgNetzOptionsFlowMixin)."""
        return await flow.async_step_salzburgnetz(user_input)


def _settings_fields(values: Mapping[str, Any]) -> dict[Any, Any]:
    """Return the fields of the settings, filled in from ``values``."""
    return {
        vol.Required(
            CONF_REREAD_DAYS,
            default=limit_reread_days(values.get(CONF_REREAD_DAYS) or DEFAULT_REREAD_DAYS),
        ): vol.All(
            NumberSelector(
                NumberSelectorConfig(
                    min=MIN_REREAD_DAYS,
                    max=MAX_REREAD_DAYS,
                    step=1,
                    mode=NumberSelectorMode.BOX,
                )
            ),
            vol.Coerce(int),
        ),
        vol.Required(
            CONF_HISTORY, default=values.get(CONF_HISTORY) or DEFAULT_HISTORY
        ): SelectSelector(
            SelectSelectorConfig(
                options=list(HISTORY_CHOICES),
                translation_key=CONF_HISTORY,
                mode=SelectSelectorMode.LIST,
            )
        ),
    }


def _shown_contracts(info: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Return the contracts a label describes: the current ones, else the last."""
    contracts = [
        contract
        for contract in info.get("vertraege") or []
        if isinstance(contract, Mapping)
    ]
    current = [contract for contract in contracts if contract.get("aktiv", True)]
    if current:
        return current
    return sorted(contracts, key=lambda contract: contract.get("ende") or "")[-1:]


def metering_point_label(identifier: str, info: Mapping[str, Any]) -> str:
    """Return a checkbox label: the metering point, what the API says, the meters.

    A metering point whose contracts have all ended shows the last one's period.
    """
    contracts = _shown_contracts(info)
    address = info.get("verbrauchsstelle") or {}
    place = " ".join(
        part for part in (address.get("postleitzahl"), address.get("ort")) if part
    )
    street = ", ".join(part for part in (address.get("strasse"), place) if part)
    parts: list[str] = []
    for part in (
        info.get("zaehlpunktName"),
        *(contract.get("sparte") for contract in contracts),
        info.get("zaehlpunktAnlagentyp"),
        *(contract.get("anlagenart") for contract in contracts),
        *(contract.get("produkt") for contract in contracts),
        street,
        *(contract.get("adresse") for contract in contracts),
    ):
        if part and part not in parts:
            parts.append(part)
    meters: list[str] = []
    for meter in (
        *(contract.get("geraet") for contract in contracts),
        info.get("geraetNumber"),
    ):
        if meter and meter not in meters:
            meters.append(meter)

    label = " · ".join([identifier, *parts])
    if meters:
        label = f"{label} ({', '.join(meters)})"
    if info.get("aktiv") is False and contracts:
        start, end = (contracts[0].get(key) or "" for key in ("beginn", "ende"))
        if start or end:
            label = f"{label} · {start} – {end}".rstrip()
    return label


def _metering_points_field(
    described: Mapping[str, Mapping[str, Any]], selected: Any
) -> dict[Any, Any]:
    """Return the metering point field: checkboxes, or text when none are known.

    Those whose contracts have all ended come last. ``selected`` are the ticked
    metering points; None ticks all except those.
    """
    chosen = normalise_metering_points(selected)
    if not described:
        return {
            vol.Required(
                CONF_METERING_POINTS,
                description={"suggested_value": ", ".join(chosen)},
            ): str
        }
    current = [
        identifier
        for identifier, info in described.items()
        if info.get("aktiv") is not False
    ]
    ordered = current + [item for item in described if item not in current]
    return {
        vol.Required(
            CONF_METERING_POINTS,
            default=(
                current
                if selected is None
                else [item for item in chosen if item in described]
            ),
        ): SelectSelector(
            SelectSelectorConfig(
                options=[
                    SelectOptionDict(
                        value=identifier,
                        label=metering_point_label(identifier, described[identifier]),
                    )
                    for identifier in ordered
                ],
                multiple=True,
                mode=SelectSelectorMode.LIST,
            )
        )
    }


def _chosen_metering_points(value: Any) -> tuple[str, str | None]:
    """Return the chosen metering points as stored, and the error key if any."""
    if isinstance(value, list):
        chosen = ", ".join(str(item) for item in value)
    else:
        chosen = str(value or "").strip()
    if invalid_metering_points(chosen):
        return chosen, "invalid_metering_points"
    if not chosen:
        return chosen, "no_metering_points"
    return chosen, None


class SalzburgNetzConfigFlowMixin:
    """The metering point and settings step of the config flow."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialize the state of a checked entry waiting for this step."""
        super().__init__(*args, **kwargs)
        # The entry's data and title, and the metering points the API lists, with
        # what it says about them, or None when it did not answer.
        self._salzburgnetz_data: dict[str, Any] | None = None
        self._salzburgnetz_title: str = ""
        self._salzburgnetz_described: dict[str, dict[str, Any]] | None = {}

    async def _async_salzburgnetz_choose(
        self,
        data: dict[str, Any],
        title: str,
        described: dict[str, dict[str, Any]] | None,
    ) -> ConfigFlowResult:
        """Keep a checked entry and ask for its metering points and settings.

        ``described`` are the metering points the API lists; without any, they are
        asked for as text.
        """
        self._salzburgnetz_data = data
        self._salzburgnetz_title = title
        self._salzburgnetz_described = described
        return await self.async_step_salzburgnetz()

    async def async_step_salzburgnetz(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the metering points and settings, then create the checked entry."""
        if self._salzburgnetz_data is None:
            # The flow was resumed after a restart, so the checked credentials are
            # gone and have to be entered again.
            LOGGER.warning("Config flow: Salzburg Netz flow resumed without state")
            return SalzburgNetzFlow().credentials_form(self)

        errors: dict[str, str] = {}
        if self._salzburgnetz_described is None:
            errors["base"] = "metering_points_not_listed"
        values: dict[str, Any] = {}
        if user_input is not None:
            chosen, error = _chosen_metering_points(user_input.get(CONF_METERING_POINTS))
            if error is None:
                LOGGER.debug(
                    "Config flow: creating entry %s", self._salzburgnetz_title
                )
                return self.async_create_entry(
                    title=self._salzburgnetz_title,
                    data={
                        **self._salzburgnetz_data,
                        **user_input,
                        CONF_METERING_POINTS: chosen,
                    },
                )
            errors[CONF_METERING_POINTS] = error
            values = user_input

        selected = user_input.get(CONF_METERING_POINTS, []) if user_input else None
        return self.async_show_form(
            step_id="salzburgnetz",
            data_schema=vol.Schema(
                {
                    **_metering_points_field(self._salzburgnetz_described or {}, selected),
                    **_settings_fields(values),
                }
            ),
            errors=errors,
        )


class SalzburgNetzOptionsFlowMixin:
    """The metering point and settings step of the options flow."""

    async def async_step_salzburgnetz(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the metering points, the settings and the scan interval.

        The metering points are checkboxes of those the loaded entry knows, with
        what the API said about them at its last login, so showing them costs no
        request. An entry that is not loaded asks for them as text. The ticked
        ones are those the client reads, which are all listed ones with a current
        contract for an entry without stored ones; saving the form unchanged
        therefore keeps reading them.
        """
        entry = self.config_entry
        current = {**entry.data, **entry.options}
        client = getattr(getattr(entry, "runtime_data", None), "client", None)
        describe = getattr(client, "describe_metering_points", None)
        described = describe() if callable(describe) else {}
        errors: dict[str, str] = {}
        selected: Any = (
            client.metering_points_to_read()
            if described
            else current.get(CONF_METERING_POINTS) or None
        )
        if user_input is not None:
            chosen, error = _chosen_metering_points(user_input.get(CONF_METERING_POINTS))
            if error is None:
                return self.async_create_entry(
                    data={**user_input, CONF_METERING_POINTS: chosen}
                )
            errors[CONF_METERING_POINTS] = error
            current = {**current, **user_input}
            selected = user_input.get(CONF_METERING_POINTS, [])

        return self.async_show_form(
            step_id="salzburgnetz",
            data_schema=vol.Schema(
                {
                    **_metering_points_field(described, selected),
                    **_settings_fields(current),
                    vol.Optional(
                        CONF_SCAN_INTERVAL, default=scan_interval(entry)
                    ): cv.positive_int,
                }
            ),
            errors=errors,
        )
