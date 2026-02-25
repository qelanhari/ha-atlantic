"""Constants for the Atlantic Zone Control integration."""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import Final

from pyoverkiz.enums import UIWidget

from homeassistant.const import Platform

DOMAIN: Final = "atlantic_zone_control"
LOGGER: logging.Logger = logging.getLogger(__package__)

UPDATE_INTERVAL: Final = timedelta(seconds=120)

PLATFORMS: list[Platform] = [
    Platform.CLIMATE,
]

IGNORED_OVERKIZ_DEVICES: list[UIWidget] = []

# Widget-to-platform mapping for Atlantic Pass APC devices
ATLANTIC_WIDGET_TO_PLATFORM: dict[UIWidget, Platform] = {
    UIWidget.ATLANTIC_PASS_APC_ZONE_CONTROL: Platform.CLIMATE,
    UIWidget.ATLANTIC_PASS_APC_HEATING_AND_COOLING_ZONE: Platform.CLIMATE,
}

# Widgets to keep in coordinator data for sensor lookups (not as entities)
ATLANTIC_SENSOR_WIDGETS: set[str] = {
    "TemperatureSensor",
}
