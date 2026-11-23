"""DSMR telegram version table.

Kept free of third-party imports on purpose: the config flow needs the labels to
render its dropdown, and importing the adapter (which pulls in dsmr-parser and
serialx) just to get a dict would make the whole integration fail to set up if
those requirements could not be installed.

Each entry maps the version token that is stored in the config entry to the name
of the `dsmr_parser.telegram_specifications` attribute and the label shown in the
UI. The labels mirror Home Assistant's own DSMR integration, so a user who
already knows their meter there finds the same wording here.
"""
from __future__ import annotations

from typing import Any

DEFAULT_DSMR_VERSION = "5"

DSMR_VERSIONS: dict[str, tuple[str, str]] = {
    "2.2": ("V2_2", "DSMR 2.2"),
    "4": ("V4", "DSMR 4"),
    "4+": ("V5", "DSMR 4+"),
    "5": ("V5", "DSMR 5"),
    "5B": ("BELGIUM_FLUVIUS", "DSMR 5B (Belgium, Fluvius)"),
    "5L": ("LUXEMBOURG_SMARTY", "DSMR 5L (Luxembourg, unencrypted)"),
    "5S": ("SWEDEN", "DSMR 5S (Sweden)"),
    "Q3D": ("Q3D", "Q3D (Austria)"),
    "5EONHU": ("EON_HUNGARY", "DSMR 5 (E.ON Hungary)"),
    "ISKRA_IE": ("ISKRA_IE", "Iskra IE"),
    "MSn": ("MSN", "Sagemcom T210-D / Luxembourg Smarty, encrypted"),
    "SAGEMCOM_T210_D_R": (
        "SAGEMCOM_T210_D_R",
        "Sagemcom T210-D-R, encrypted (Austria, Energienetze Steiermark)",
    ),
}

# token -> label, for the config flow dropdown.
VERSION_LABELS: dict[str, str] = {
    token: label for token, (_, label) in DSMR_VERSIONS.items()
}

# How the P1 port is opened, per DSMR version. These are dsmr-parser's own
# SERIAL_SETTINGS (clients/settings.py): DSMR 2.2 sends at 9600 baud 7E1, DSMR 4
# and 4+ at 115200 7E1, and DSMR 5 with every later dialect - including the
# Austrian Q3D, Iskra IE and the Sagemcom T210-D-R - at 115200 8N1. serialx falls
# back to 9600 8N1, which leaves a 115200 meter silent: that was the reason a
# Sagemcom T210-D-R could not be read.
SERIAL_SETTINGS: dict[str, dict[str, Any]] = {
    "2.2": {"baudrate": 9600, "byte_size": 7, "parity": "E", "stopbits": 1},
    "4": {"baudrate": 115200, "byte_size": 7, "parity": "E", "stopbits": 1},
    "4+": {"baudrate": 115200, "byte_size": 7, "parity": "E", "stopbits": 1},
}

# What the remaining, later dialects send: the library's DSMR 5 settings.
DEFAULT_SERIAL_SETTINGS: dict[str, Any] = {
    "baudrate": 115200,
    "byte_size": 8,
    "parity": "N",
    "stopbits": 1,
}

# The value the options-less "automatic" choice of the form is stored as.
AUTO_BAUDRATE = "auto"

# What a user can pick instead of the version's own rate. A DSMR interface runs
# at one of these; a custom value is not worth a free-text field.
COMMON_BAUDRATES: tuple[int, ...] = (9600, 19200, 38400, 57600, 115200)

# token -> label, for the baud rate dropdown of the config flow.
BAUDRATE_LABELS: dict[str, str] = {
    AUTO_BAUDRATE: "Automatic (per DSMR version: 115200 for DSMR 4/5, 9600 for DSMR 2.2)",
    **{str(rate): f"{rate} baud" for rate in COMMON_BAUDRATES},
}


def serial_settings(version: str, baudrate: Any = None) -> dict[str, Any]:
    """Return the port settings of a DSMR version, with an optional baud rate.

    The framing follows the version; only the speed can be overridden, because
    that is the parameter a meter or a converter gets wrong. An override the form
    did not store (None, an empty string, ``"auto"``) keeps the version's rate.
    """
    settings = dict(DEFAULT_SERIAL_SETTINGS)
    settings.update(SERIAL_SETTINGS.get(version, {}))
    if baudrate not in (None, "", AUTO_BAUDRATE):
        try:
            rate = int(baudrate)
        except (TypeError, ValueError):
            rate = 0
        if rate > 0:
            settings["baudrate"] = rate
    return settings


__all__ = [
    "AUTO_BAUDRATE",
    "BAUDRATE_LABELS",
    "COMMON_BAUDRATES",
    "DEFAULT_DSMR_VERSION",
    "DEFAULT_SERIAL_SETTINGS",
    "DSMR_VERSIONS",
    "SERIAL_SETTINGS",
    "VERSION_LABELS",
    "serial_settings",
]
