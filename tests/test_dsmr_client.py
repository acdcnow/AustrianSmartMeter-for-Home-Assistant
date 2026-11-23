"""Tests of the DSMR port settings and of the error a silent meter gives."""
from unittest.mock import MagicMock, patch

import pytest

from custom_components.asm.api import client_dsmr as module
from custom_components.asm.api.client_dsmr import DsmrClient
from custom_components.asm.api.dsmr_versions import serial_settings
from custom_components.asm.api.errors import SmartmeterConnectionError

KEY = "00112233445566778899aabbccddeeff"


@pytest.mark.parametrize(
    ("version", "baudrate"),
    [
        ("2.2", 9600),
        ("4", 115200),
        ("4+", 115200),
        ("5", 115200),
        ("5B", 115200),
        ("Q3D", 115200),
        ("SAGEMCOM_T210_D_R", 115200),
        # A version the table does not know keeps the DSMR 5 settings.
        ("unknown", 115200),
    ],
)
def test_the_version_decides_the_speed(version: str, baudrate: int) -> None:
    """Every current DSMR dialect sends at 115200 baud; only 2.2 is slower."""
    assert serial_settings(version)["baudrate"] == baudrate


@pytest.mark.parametrize(
    ("version", "byte_size", "parity"),
    [("2.2", 7, "E"), ("4", 7, "E"), ("4+", 7, "E"), ("5", 8, "N"),
     ("SAGEMCOM_T210_D_R", 8, "N")],
)
def test_the_framing_follows_the_version(
    version: str, byte_size: int, parity: str
) -> None:
    """DSMR 2.2 and 4 use 7E1, DSMR 5 and later 8N1."""
    settings = serial_settings(version)
    assert (settings["byte_size"], settings["parity"], settings["stopbits"]) == (
        byte_size,
        parity,
        1,
    )


@pytest.mark.parametrize(
    ("stored", "baudrate"),
    [
        (None, 115200),
        ("", 115200),
        ("auto", 115200),
        ("9600", 9600),
        (19200, 19200),
        ("nonsense", 115200),
        ("0", 115200),
    ],
)
def test_a_stored_rate_wins_over_the_version(stored, baudrate: int) -> None:
    """The form's override beats the version's rate; an unusable one is ignored."""
    assert serial_settings("SAGEMCOM_T210_D_R", stored)["baudrate"] == baudrate


def test_the_port_is_opened_with_the_versions_settings() -> None:
    """The Sagemcom T210-D-R is opened at 115200 8N1, not at the serialx default."""
    client = DsmrClient("/dev/ttyUSB1", "SAGEMCOM_T210_D_R", KEY)
    assert client.baudrate == 115200
    with patch.object(
        module, "serial_for_url", side_effect=module.SerialException("no port")
    ) as factory, pytest.raises(SmartmeterConnectionError, match="115200"):
        client.login()
    assert factory.call_args.kwargs == {
        "connect_timeout": module.CONNECT_TIMEOUT,
        "baudrate": 115200,
        "byte_size": 8,
        "parity": "N",
        "stopbits": 1,
    }


def test_a_silent_meter_names_the_baud_rate() -> None:
    """The message of a meter that sends nothing names the rate that was tried."""
    handle = MagicMock()
    handle.read.side_effect = lambda size: b""
    client = DsmrClient("/dev/ttyUSB1", "SAGEMCOM_T210_D_R", KEY)
    with patch.object(module, "serial_for_url", return_value=handle), patch.object(
        module, "READ_TIMEOUT", 0.01
    ), pytest.raises(SmartmeterConnectionError) as raised:
        client.login()
    assert "115200 baud" in str(raised.value)
    assert "DSMR version and baud rate" in str(raised.value)
    handle.close.assert_called_once()
