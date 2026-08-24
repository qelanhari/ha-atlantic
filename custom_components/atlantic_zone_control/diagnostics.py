"""Diagnostics support for Atlantic Zone Control."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_platform as ep

from . import AtlanticZoneControlConfigEntry
from .const import DOMAIN

TO_REDACT = {CONF_USERNAME, CONF_PASSWORD}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: AtlanticZoneControlConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data.coordinator

    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "server": coordinator.client.server_config.name,
        "update_interval": str(coordinator.update_interval),
        "last_update_success": coordinator.last_update_success,
        "consecutive_failures": coordinator.consecutive_failures,
        "executions_in_flight": coordinator.tracker.snapshot(),
        "reconcile_urls": sorted(coordinator.reconcile_urls),
        "optimistic": _optimistic_diagnostics(hass, entry),
        "devices": [
            {
                "device_url": device.device_url,
                "label": device.label,
                "widget": str(device.widget),
                "controllable_name": str(device.controllable_name),
                "available": device.available,
                "states": {
                    name: state.value for name, state in device.states.items()
                },
            }
            for device in coordinator.devices.values()
        ],
    }


def _optimistic_diagnostics(
    hass: HomeAssistant, entry: AtlanticZoneControlConfigEntry
) -> dict[str, Any]:
    """Return every entity's outstanding optimistic values."""
    return {
        entity.entity_id: entity.optimistic_diagnostics()
        for platform in ep.async_get_platforms(hass, DOMAIN)
        if platform.config_entry is entry
        for entity in platform.entities.values()
        if hasattr(entity, "optimistic_diagnostics")
    }
