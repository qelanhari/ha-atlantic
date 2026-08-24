"""The Atlantic Zone Control integration."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from aiohttp import ClientError
from pyoverkiz.auth.credentials import UsernamePasswordCredentials
from pyoverkiz.client import OverkizClient
from pyoverkiz.enums import Server, UIWidget
from pyoverkiz.exceptions import (
    BadCredentialsError,
    BaseOverkizError,
    MaintenanceError,
    NotAuthenticatedError,
    TooManyRequestsError,
)
from pyoverkiz.models import Device

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.event import async_track_time_interval

from .const import (
    ATLANTIC_WIDGET_TO_PLATFORM,
    DOMAIN,
    LOGGER,
    PLATFORMS,
    RECONCILE_INTERVAL,
)
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
        server=Server.SOMFY_EUROPE,
        credentials=UsernamePasswordCredentials(
            entry.data[CONF_USERNAME], entry.data[CONF_PASSWORD]
        ),
        session=session,
    )

    try:
        await client.login()
        setup = await client.get_setup()
    except (BadCredentialsError, NotAuthenticatedError) as exception:
        raise ConfigEntryAuthFailed("Invalid authentication") from exception
    except TooManyRequestsError as exception:
        raise ConfigEntryNotReady("Too many requests, try again later") from exception
    except (TimeoutError, ClientError) as exception:
        raise ConfigEntryNotReady("Failed to connect") from exception
    except MaintenanceError as exception:
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

    coordinator.reconcile_urls = _reconcile_urls(platforms, coordinator)

    entry.runtime_data = AtlanticZoneControlData(
        coordinator=coordinator, platforms=platforms
    )

    entry.async_on_unload(
        async_track_time_interval(
            hass, coordinator.async_reconcile, RECONCILE_INTERVAL
        )
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
            manufacturer=client.server_config.manufacturer,
            name=gateway.type.beautify_name if gateway.type else gateway.id,
            sw_version=gateway.connectivity.protocol_version,
            hw_version=f"{gateway.type}:{gateway.sub_type}"
            if gateway.type and gateway.sub_type
            else None,
            configuration_url=client.server_config.configuration_url,
        )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


def _reconcile_urls(
    platforms: defaultdict[Platform, list[Device]],
    coordinator: OverkizDataUpdateCoordinator,
) -> frozenset[str]:
    """Return the device URLs whose states actually back an entity.

    Reconciliation reads these one by one, so the set is deliberately narrow:
    the zone control, each zone, and each zone's temperature sensor.
    """
    urls: set[str] = set()

    for devices in platforms.values():
        for device in devices:
            urls.add(device.device_url)

            base, _, index = device.device_url.partition("#")
            if index.isdigit():
                sensor_url = f"{base}#{int(index) + 1}"
                if sensor_url in coordinator.devices:
                    urls.add(sensor_url)

    LOGGER.debug("Reconciling %d device URL(s): %s", len(urls), sorted(urls))

    return frozenset(urls)


async def async_unload_entry(
    hass: HomeAssistant, entry: AtlanticZoneControlConfigEntry
) -> bool:
    """Unload a config entry."""
    coordinator = entry.runtime_data.coordinator
    coordinator.cancel_pending_flush()

    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unloaded:
        # DEVICE_UPDATED reloads the entry, so without this every reload would
        # leak a server-side event listener.
        try:
            await coordinator.client.unregister_event_listener()
        except (BaseOverkizError, ClientError, TimeoutError) as exception:
            LOGGER.debug("Could not unregister event listener: %s", exception)

    return unloaded
