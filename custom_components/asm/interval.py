"""The scan interval of an entry, in the unit its provider is polled in.

Most providers read a portal that publishes at most once a day, so their
interval is kept in minutes and never goes below an hour. A provider whose meter
pushes its values - energyLIVE reads the interface attached to the meter, a DSMR
customer interface sends a telegram every 10 seconds - can be polled in seconds
instead, down to :data:`MIN_LIVE_INTERVAL`.

The two units live under two option keys on purpose: an entry that still has the
minutes key keeps its interval after the upgrade (6 hours by default) instead of
having it read as 360 seconds, and only a value the user saved as seconds
shortens the polling. The options form shows the unit of the provider, so an
entry's interval is never converted behind the user's back.
"""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
import homeassistant.helpers.config_validation as cv

from .const import (
    CONF_PROVIDER,
    CONF_SCAN_INTERVAL,
    CONF_SCAN_INTERVAL_SECONDS,
    DEFAULT_SCAN_INTERVAL,
    LIVE_PROVIDERS,
    MIN_LIVE_INTERVAL,
    MIN_SCAN_INTERVAL,
    PROVIDER_WIENER_NETZE,
)


def is_live_provider(provider: str) -> bool:
    """Return True when the provider's meter pushes values and may be polled fast."""
    return provider in LIVE_PROVIDERS


def _provider(entry: ConfigEntry) -> str:
    """Return the provider an entry was set up with."""
    return entry.data.get(CONF_PROVIDER, PROVIDER_WIENER_NETZE)


def _stored(entry: ConfigEntry, key: str) -> int | None:
    """Return an interval stored in the options or, failing that, the data."""
    raw = entry.options.get(key)
    if raw is None:
        raw = entry.data.get(key)
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def scan_interval_seconds(entry: ConfigEntry) -> int:
    """Return the interval the coordinator polls with, in seconds."""
    if is_live_provider(_provider(entry)):
        seconds = _stored(entry, CONF_SCAN_INTERVAL_SECONDS)
        if seconds is not None:
            return max(seconds, MIN_LIVE_INTERVAL)
        # An entry from before the seconds option keeps the minutes value it
        # was set up with: an upgrade must not start polling an API twenty
        # times more often by itself.
        minutes = _stored(entry, CONF_SCAN_INTERVAL)
        if minutes is not None:
            return max(minutes, MIN_SCAN_INTERVAL) * 60
        return DEFAULT_SCAN_INTERVAL * 60

    minutes = _stored(entry, CONF_SCAN_INTERVAL)
    if minutes is None:
        return DEFAULT_SCAN_INTERVAL * 60
    return max(minutes, MIN_SCAN_INTERVAL) * 60


def scan_interval_field(entry: ConfigEntry) -> dict[Any, Any]:
    """Return the scan interval field of an entry's options form.

    The field asks in the provider's unit: seconds for a live provider, minutes
    for a portal. A value below the provider's minimum is refused rather than
    silently shortened, so the form shows what will be polled.
    """
    if is_live_provider(_provider(entry)):
        return {
            vol.Optional(
                CONF_SCAN_INTERVAL_SECONDS, default=scan_interval_seconds(entry)
            ): vol.All(cv.positive_int, vol.Range(min=MIN_LIVE_INTERVAL))
        }
    return {
        vol.Optional(
            CONF_SCAN_INTERVAL, default=scan_interval_seconds(entry) // 60
        ): vol.All(cv.positive_int, vol.Range(min=MIN_SCAN_INTERVAL))
    }
