"""The Atlantic Zone Control integration."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from aiohttp import ClientError
from pyoverkiz.client import OverkizClient
from pyoverkiz.const import SUPPORTED_SERVERS
from pyoverkiz.enums import Server, UIWidget
from pyoverkiz.exceptions import (
    BadCredentialsException,
    MaintenanceException,
    NotAuthenticatedException,
    TooManyRequestsException,
)
from pyoverkiz.models import Device

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .const import ATLANTIC_WIDGET_TO_PLATFORM, DOMAIN, LOGGER, PLATFORMS
from .coordinator import OverkizDataUpdateCoordinator


@dataclass
class AtlanticZoneControlData:
    """Atlantic Zone Control data stored in the runtime data object."""

    coordinator: OverkizDataUpdateCoordinator
    platforms: defaultdict[Platform, list[Device]]


type AtlanticZoneControlConfigEntry = ConfigEntry[AtlanticZoneControlData]


async def async_setup_entry(
    hass: HomeAssistant, entry: AtlanticZoneControlConfigEntry
) -> bool:
    """Set up Atlantic Zone Control from a config entry."""
    session = async_create_clientsession(hass)
    client = OverkizClient(
        username=entry.data[CONF_USERNAME],
        password=entry.data[CONF_PASSWORD],
        server=SUPPORTED_SERVERS[Server.SOMFY_EUROPE],
        session=session,
    )

    try:
        await client.login()
        setup = await client.get_setup()
    except (BadCredentialsException, NotAuthenticatedException) as exception:
        raise ConfigEntryAuthFailed("Invalid authentication") from exception
    except TooManyRequestsException as exception:
        raise ConfigEntryNotReady("Too many requests, try again later") from exception
    except (TimeoutError, ClientError) as exception:
        raise ConfigEntryNotReady("Failed to connect") from exception
    except MaintenanceException as exception:
        raise ConfigEntryNotReady("Server is down for maintenance") from exception

    # Keep all devices in coordinator (including sensors for linked_device lookups)
    coordinator = OverkizDataUpdateCoordinator(
        hass,
        entry,
        LOGGER,
        client=client,
        devices=setup.devices,
        places=setup.root_place,
    )

    await coordinator.async_config_entry_first_refresh()

    # Only create entities for Atlantic Pass APC climate devices
    platforms: defaultdict[Platform, list[Device]] = defaultdict(list)

    for device in coordinator.data.values():
        LOGGER.debug(
            "Device discovered: %s (widget=%s, controllable=%s)",
            device.label,
            device.widget,
            device.controllable_name,
        )

        if platform := ATLANTIC_WIDGET_TO_PLATFORM.get(UIWidget(device.widget)):
            platforms[platform].append(device)

    entry.runtime_data = AtlanticZoneControlData(
        coordinator=coordinator, platforms=platforms
    )

    # Register gateway in device registry
    device_registry = dr.async_get(hass)
    for gateway in setup.gateways:
        LOGGER.debug("Added gateway (%s)", gateway)
        device_registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, gateway.id)},
            model=gateway.type.beautify_name if gateway.type else None,
            model_id=str(gateway.type),
            manufacturer=client.server.manufacturer,
            name=gateway.type.beautify_name if gateway.type else gateway.id,
            sw_version=gateway.connectivity.protocol_version,
            hw_version=f"{gateway.type}:{gateway.sub_type}"
            if gateway.type and gateway.sub_type
            else None,
            configuration_url=client.server.configuration_url,
        )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: AtlanticZoneControlConfigEntry
) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
