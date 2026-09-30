"""DataUpdateCoordinator for Austria Smartmeter."""
from __future__ import annotations

import asyncio
from datetime import date, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api.client import get_client
from .api.errors import SmartmeterError, SmartmeterLoginError
from .const import (
    CONF_PROVIDER,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    LOGGER,
    MIN_SCAN_INTERVAL,
    PROVIDER_WIENER_NETZE,
)
from .statistics import StatisticsImporter, statistic_readings


class AustriaSmartMeterCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Class to manage fetching Austria Smartmeter data."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the coordinator."""
        self.provider: str = entry.data.get(CONF_PROVIDER, PROVIDER_WIENER_NETZE)
        # Every provider reads what it needs out of the entry data.
        self.client = get_client(self.provider, entry.data)
        self._entry = entry
        # A client is synchronous and holds one portal session: a poll and a
        # statistics import take turns with it.
        self._client_lock = asyncio.Lock()
        # Imports the readings that carry a statistic_id into the long-term
        # statistics, in the background (statistics.py).
        self._statistics = StatisticsImporter(hass, self._async_read_history)
        self._shut_down = False

        super().__init__(
            hass,
            LOGGER,
            # Passed explicitly: the entry's unload calls async_shutdown.
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(minutes=_scan_interval(entry)),
        )

    async def async_shutdown(self) -> None:
        """Cancel any scheduled call, ignore new runs, and start no import."""
        self._shut_down = True
        await super().async_shutdown()

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from the provider portal."""
        try:
            async with self._client_lock:
                data = await self._async_read_portal()
        except SmartmeterLoginError as err:
            raise ConfigEntryAuthFailed from err
        except SmartmeterError as err:
            raise UpdateFailed(f"Error communicating with the portal: {err}") from err
        except Exception as err:  # noqa: BLE001
            LOGGER.exception("Unexpected error during update")
            raise UpdateFailed(f"Unexpected error: {err}") from err

        self._async_start_statistics_import(data)
        return data

    async def _async_read_portal(self) -> dict[str, Any]:
        """Read the metering points with their statistics and readings."""
        if not self.client.is_logged_in() or self.client.is_login_expired():
            await self.hass.async_add_executor_job(self.client.login)

        contracts = await self.hass.async_add_executor_job(self.client.zaehlpunkte)
        consumption_stats = await self._async_consumption_stats()

        data: dict[str, Any] = {}
        for contract in contracts:
            zaehlpunkte = contract.get("zaehlpunkte") or []
            for zp_info in zaehlpunkte:
                zp_num = zp_info.get("zaehlpunktnummer")
                if not zp_num:
                    continue

                data[zp_num] = {"info": zp_info, "readings": [], "stats": {}}
                data[zp_num]["stats"] = _match_stats(
                    consumption_stats, zp_num, len(contracts), len(zaehlpunkte)
                )

                try:
                    data[zp_num]["readings"] = (
                        await self.hass.async_add_executor_job(
                            lambda zp=zp_num: self.client.historical_data(
                                zaehlpunktnummer=zp
                            )
                        )
                    )
                except SmartmeterError as err:
                    LOGGER.warning(
                        "Could not fetch historic data for %s: %s", zp_num, err
                    )

        return data

    @callback
    def _async_start_statistics_import(self, data: dict[str, Any]) -> None:
        """Import the poll's statistic readings in the background.

        The entities are updated without waiting for it: the first import reads
        the whole history. An import never overlaps another one.
        """
        # A refresh that finishes while the entry unloads, or after it - also when
        # a reload has set the entry up again with a new coordinator - must not
        # start an import: nothing would cancel it any more, and it would run
        # next to the new coordinator's.
        if self._shut_down or self._entry.state not in (
            ConfigEntryState.SETUP_IN_PROGRESS,
            ConfigEntryState.LOADED,
        ):
            return
        if not any(statistic_readings(data)):
            return
        if self._statistics.running:
            LOGGER.debug(
                "The statistics import of an earlier poll is still running; this "
                "poll's import is skipped"
            )
            return
        # Not started eagerly: the import begins after this poll has returned.
        self._entry.async_create_background_task(
            self.hass,
            self._statistics.async_import(data),
            name="asm statistics import",
            eager_start=False,
        )

    async def _async_read_history(
        self, zaehlpunkt: str, first: date, last: date
    ) -> list[dict[str, Any]]:
        """Read the readings of a metering point for the days first to last."""
        async with self._client_lock:
            return await self.hass.async_add_executor_job(
                self.client.historical_data, zaehlpunkt, first, last
            )

    async def _async_consumption_stats(self) -> list[dict[str, Any]]:
        """Fetch the ready made consumption statistics, if the provider has any."""
        try:
            stats = await self.hass.async_add_executor_job(self.client.consumptions)
        except SmartmeterError as err:
            LOGGER.debug("Consumption statistics unavailable: %s", err)
            return []

        if isinstance(stats, dict):
            # Some providers return a single object instead of a list.
            return [stats]
        if isinstance(stats, list):
            return [stat for stat in stats if isinstance(stat, dict)]

        LOGGER.warning("Unexpected consumption statistics payload: %s", type(stats))
        return []


def _scan_interval(entry: ConfigEntry) -> int:
    """Return the configured scan interval in minutes."""
    raw = entry.options.get(CONF_SCAN_INTERVAL) or entry.data.get(CONF_SCAN_INTERVAL)
    try:
        interval = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_SCAN_INTERVAL
    return max(interval, MIN_SCAN_INTERVAL)


def _match_stats(
    consumption_stats: list[dict[str, Any]],
    zaehlpunkt: str,
    contract_count: int,
    zaehlpunkt_count: int,
) -> dict[str, Any]:
    """Return the statistics that belong to a metering point."""
    for stat in consumption_stats:
        stat_zp = stat.get("zaehlpunktnummer") or stat.get("zaehlpunkt")
        if stat_zp == zaehlpunkt:
            return stat
        if stat_zp is None and contract_count == 1 and zaehlpunkt_count == 1:
            # The API omits the metering point number when the account only has
            # a single meter, so assign it implicitly.
            LOGGER.debug("Assigning statistics to %s (implicit match)", zaehlpunkt)
            return stat
    return {}
