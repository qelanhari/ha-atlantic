"""Constants for the Atlantic Zone Control integration."""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import Final

from pyoverkiz.enums import UIWidget

from homeassistant.const import Platform

DOMAIN: Final = "atlantic_zone_control"
LOGGER: logging.Logger = logging.getLogger(__package__)

UPDATE_INTERVAL: Final = timedelta(seconds=90)

# Fast poll used while executions are in flight.
EXECUTION_UPDATE_INTERVAL: Final = timedelta(seconds=2)

# Command batching debounce.
COMMAND_DEBOUNCE: Final = 2

# An execution with no terminal state after this long is presumed lost. Anything
# still tracked pins the fast poll, so this bound is what keeps UPDATE_INTERVAL
# from being held at 2s indefinitely.
EXECUTION_MAX_AGE: Final = 180.0

# Backstop lifetime for an optimistic value. Must exceed one full restored poll
# cycle: the fast poll un-pins on COMPLETED, so a slow zone's confirming event
# can arrive up to UPDATE_INTERVAL later.
OPTIMISTIC_MAX_AGE: Final = 200.0

# Target temperatures are compared with a tolerance; the device may clamp or
# round what we asked for.
TEMPERATURE_TOLERANCE: Final = 0.05

# Age beyond which async_ensure_fresh() forces a real refresh before commanding.
FRESHNESS_MAX_AGE: Final = 5.0

# How often to reconcile entity-backing states against the cloud.
RECONCILE_INTERVAL: Final = timedelta(minutes=15)

# How often to ask the gateway to re-read the zone profiles. Reconciliation
# only reads the server's cache; this is what refreshes the cache itself, and
# the profile is now what determines whether a zone is on. One execution
# covers every zone, so this is cheap against the exec rate limit.
ZONES_REFRESH_INTERVAL: Final = timedelta(hours=1)

EVENT_COMMAND_FAILED: Final = f"{DOMAIN}_command_failed"

PLATFORMS: list[Platform] = [
    Platform.CLIMATE,
]

IGNORED_OVERKIZ_DEVICES: list[UIWidget] = []

# Widget-to-platform mapping for Atlantic Pass APC devices
ATLANTIC_WIDGET_TO_PLATFORM: dict[UIWidget, Platform] = {
    UIWidget.ATLANTIC_PASS_APC_ZONE_CONTROL: Platform.CLIMATE,
    UIWidget.ATLANTIC_PASS_APC_HEATING_AND_COOLING_ZONE: Platform.CLIMATE,
}
