"""Shared fixtures for the Atlantic Zone Control tests."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from pyoverkiz.converter import converter
from pyoverkiz.models import Device
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.atlantic_zone_control.const import DOMAIN
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant

GATEWAY_ID = "1234-5678-9012"
BASE_URL = f"io://{GATEWAY_ID}/1000001"
ZONE_CONTROL_URL = f"{BASE_URL}#1"
ZONE_URL = f"{BASE_URL}#2"
SENSOR_URL = f"{BASE_URL}#3"


def _device(
    device_url: str,
    label: str,
    widget: str,
    states: dict[str, tuple[int, Any]],
    commands: list[str],
) -> Device:
    """Build a real pyoverkiz Device from an API-shaped payload."""
    return converter.structure(
        {
            "deviceURL": device_url,
            "label": label,
            "type": 1,
            "controllableName": f"io:{widget}Component",
            "uiClass": "HeatingSystem",
            "widget": widget,
            "enabled": True,
            "available": True,
            "placeOID": "place-1",
            "states": [
                {"name": name, "type": type_, "value": value}
                for name, (type_, value) in states.items()
            ],
            "definition": {
                "commands": [{"commandName": c, "nparams": 1} for c in commands],
                "states": [],
                "widgetName": widget,
                "uiClass": "HeatingSystem",
                "qualifiedName": widget,
                "type": "ACTUATOR",
            },
            "attributes": [],
        },
        Device,
    )


def build_devices(
    operating_mode: str = "cooling",
    *,
    cooling_profile: str | None = "manu",
    cooling_on_off: str = "on",
    heating_profile: str = "stop",
    heating_on_off: str = "off",
) -> list[Device]:
    """Build a zone control, one zone, and its temperature sensor.

    ``cooling_profile=None`` models a device that reports no profile state.
    """
    profile_states: dict[str, tuple[int, Any]] = (
        {}
        if cooling_profile is None
        else {
            "io:PassAPCCoolingProfileState": (3, cooling_profile),
            "io:PassAPCHeatingProfileState": (3, heating_profile),
        }
    )

    return [
        _device(
            ZONE_CONTROL_URL,
            "Zone Control",
            "AtlanticPassAPCZoneControl",
            {"io:PassAPCOperatingModeState": (3, operating_mode)},
            ["setPassAPCOperatingMode"],
        ),
        _device(
            ZONE_URL,
            "Salon",
            "AtlanticPassAPCHeatingAndCoolingZone",
            {
                "core:CoolingOnOffState": (3, cooling_on_off),
                "core:HeatingOnOffState": (3, heating_on_off),
                "core:CoolingTargetTemperatureState": (2, 24.0),
                "core:HeatingTargetTemperatureState": (2, 19.0),
                "io:PassAPCCoolingModeState": (3, "manu"),
                "io:PassAPCHeatingModeState": (3, "manu"),
                **profile_states,
            },
            [
                "setCoolingOnOffState",
                "setHeatingOnOffState",
                "setCoolingTargetTemperature",
                "setHeatingTargetTemperature",
                "setPassAPCCoolingMode",
                "setPassAPCHeatingMode",
            ],
        ),
        _device(
            SENSOR_URL,
            "Salon Temperature",
            "TemperatureSensor",
            {"core:TemperatureState": (2, 23.4)},
            [],
        ),
    ]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable loading of the custom integration in every test."""
    return enable_custom_integrations


@pytest.fixture
def devices() -> list[Device]:
    """Return the default device set."""
    return build_devices()


@pytest.fixture
def mock_client(devices: list[Device]) -> MagicMock:
    """Return a mocked OverkizClient backed by real Device objects."""
    client = MagicMock()
    client.login = AsyncMock(return_value=True)
    client.get_devices = AsyncMock(return_value=devices)
    client.get_state = AsyncMock(return_value=[])
    client.fetch_events = AsyncMock(return_value=[])
    client.execute_action_group = AsyncMock(return_value="exec-1")
    client.unregister_event_listener = AsyncMock(return_value=None)
    client.event_listener_id = "listener-1"
    client.server_config = SimpleNamespace(
        name="Somfy Europe",
        manufacturer="Somfy",
        configuration_url="https://example.invalid",
    )
    client.get_setup = AsyncMock(
        return_value=SimpleNamespace(
            devices=devices,
            gateways=[],
            root_place=None,
        )
    )
    return client


@pytest.fixture
def config_entry() -> MockConfigEntry:
    """Return a config entry for the integration."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="test@example.invalid",
        data={CONF_USERNAME: "test@example.invalid", CONF_PASSWORD: "hunter2"},
    )


@pytest.fixture
async def setup_integration(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock
):
    """Set up the integration with a mocked client."""
    config_entry.add_to_hass(hass)

    with patch(
        "custom_components.atlantic_zone_control.OverkizClient",
        return_value=mock_client,
    ):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    return config_entry
