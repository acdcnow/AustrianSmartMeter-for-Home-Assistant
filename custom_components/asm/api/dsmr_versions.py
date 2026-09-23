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

__all__ = ["DEFAULT_DSMR_VERSION", "DSMR_VERSIONS", "VERSION_LABELS"]
