"""Fixtures of the Austria Smartmeter tests."""
import pytest

from custom_components.asm.api import client_salzburgnetz

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Load the integration from custom_components in every test."""
    yield


@pytest.fixture(autouse=True)
def no_salzburgnetz_pauses():
    """Start every test without the pauses an earlier one left after an HTTP 429."""
    client_salzburgnetz._THROTTLED_UNTIL.clear()
    yield
    client_salzburgnetz._THROTTLED_UNTIL.clear()
