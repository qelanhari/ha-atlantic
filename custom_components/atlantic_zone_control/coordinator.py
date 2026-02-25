"""Helpers to help coordinate updates."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Coroutine
from datetime import timedelta
import logging
from typing import TYPE_CHECKING, Any, cast

from aiohttp import ClientConnectorError, ServerDisconnectedError
from pyoverkiz.client import OverkizClient
from pyoverkiz.enums import EventName, ExecutionState, Protocol
from pyoverkiz.exceptions import (
    BadCredentialsException,
    BaseOverkizException,
    InvalidEventListenerIdException,
    MaintenanceException,
    NotAuthenticatedException,
    TooManyConcurrentRequestsException,
    TooManyRequestsException,
)
from pyoverkiz.models import Command, Device, Event, Place

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util.decorator import Registry

if TYPE_CHECKING:
    from . import AtlanticZoneControlConfigEntry

from .const import DOMAIN, IGNORED_OVERKIZ_DEVICES, LOGGER, UPDATE_INTERVAL

EVENT_HANDLERS: Registry[
    str, Callable[[OverkizDataUpdateCoordinator, Event], Coroutine[Any, Any, None]]
] = Registry()


class OverkizBatchExecutor:
    """Executes commands across multiple devices in a single API call."""

    def __init__(self, client: OverkizClient) -> None:
        """Initialize the batch executor."""
        self._client = client
        self._post = getattr(client, "_OverkizClient__post", None)

    @property
    def supports_multi_device(self) -> bool:
        """Return True if the client exposes the internal __post method."""
        return self._post is not None

    async def execute_multi(
        self,
        actions: list[dict[str, Any]],
        label: str = "Home Assistant",
    ) -> str | None:
        """Execute a multi-device batch. Returns exec_id or None on failure."""
        if not self._post:
            return None
        payload = {"label": label, "actions": actions}
        response = await self._post("exec/apply", payload)
        return cast(str, response["execId"])


class OverkizDataUpdateCoordinator(DataUpdateCoordinator[dict[str, Device]]):
    """Class to manage fetching data from Overkiz platform."""

    config_entry: AtlanticZoneControlConfigEntry
    _default_update_interval: timedelta

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: AtlanticZoneControlConfigEntry,
        logger: logging.Logger,
        *,
        client: OverkizClient,
        devices: list[Device],
        places: Place | None,
    ) -> None:
        """Initialize global data updater."""
        super().__init__(
            hass,
            logger,
            config_entry=config_entry,
            name="device events",
            update_interval=UPDATE_INTERVAL,
        )

        self.data = {}
        self.client = client
        self.devices: dict[str, Device] = {d.device_url: d for d in devices}
        self.executions: dict[str, dict[str, str]] = {}
        self.areas = self._places_to_area(places) if places else None
        self._default_update_interval = UPDATE_INTERVAL
        self.last_refresh_time: float = 0

        self.is_stateless = all(
            device.protocol in (Protocol.RTS, Protocol.INTERNAL)
            for device in devices
            if device.widget not in IGNORED_OVERKIZ_DEVICES
        )

        self._command_queue: dict[str, list[Command]] = {}
        self._flush_handle: asyncio.TimerHandle | None = None
        self._post_flush_callbacks: list[Callable[[], Coroutine[Any, Any, None]]] = []
        self._batch_executor = OverkizBatchExecutor(client)

    async def _async_update_data(self) -> dict[str, Device]:
        """Fetch Overkiz data via event listener."""
        try:
            events = await self.client.fetch_events()
        except (BadCredentialsException, NotAuthenticatedException) as exception:
            raise ConfigEntryAuthFailed("Invalid authentication.") from exception
        except TooManyConcurrentRequestsException as exception:
            raise UpdateFailed("Too many concurrent requests.") from exception
        except TooManyRequestsException as exception:
            raise UpdateFailed("Too many requests, try again later.") from exception
        except MaintenanceException as exception:
            raise UpdateFailed("Server is down for maintenance.") from exception
        except InvalidEventListenerIdException as exception:
            raise UpdateFailed(exception) from exception
        except (TimeoutError, ClientConnectorError) as exception:
            LOGGER.debug("Failed to connect", exc_info=True)
            raise UpdateFailed("Failed to connect.") from exception
        except ServerDisconnectedError:
            self.executions = {}

            try:
                await self.client.login()
                self.devices = await self._get_devices()
            except (BadCredentialsException, NotAuthenticatedException) as exception:
                raise ConfigEntryAuthFailed("Invalid authentication.") from exception
            except TooManyRequestsException as exception:
                raise UpdateFailed("Too many requests, try again later.") from exception

            return self.devices

        for event in events:
            LOGGER.debug(event)

            if event_handler := EVENT_HANDLERS.get(event.name):
                await event_handler(self, event)

        if not self.executions:
            self.update_interval = self._default_update_interval

        return self.devices

    async def _get_devices(self) -> dict[str, Device]:
        """Fetch devices."""
        LOGGER.debug("Fetching all devices and state via /setup/devices")
        return {d.device_url: d for d in await self.client.get_devices(refresh=True)}

    def _places_to_area(self, place: Place) -> dict[str, str]:
        """Convert places with sub_places to a flat dictionary."""
        areas = {}
        if isinstance(place, Place):
            areas[place.oid] = place.label

        if isinstance(place.sub_places, list):
            for sub_place in place.sub_places:
                areas.update(self._places_to_area(sub_place))

        return areas

    def queue_commands(
        self,
        device_url: str,
        commands: list[Command],
        post_flush: Callable[[], Coroutine[Any, Any, None]] | None = None,
    ) -> None:
        """Queue commands for a device, flushing after a short delay."""
        self._command_queue.setdefault(device_url, []).extend(commands)
        LOGGER.debug(
            "Queued %d command(s) for %s: %s",
            len(commands),
            device_url,
            [c.name for c in commands],
        )

        if post_flush is not None and post_flush not in self._post_flush_callbacks:
            self._post_flush_callbacks.append(post_flush)

        if self._flush_handle is not None:
            self._flush_handle.cancel()

        self._flush_handle = self.hass.loop.call_later(
            0.5, lambda: self.hass.async_create_task(self._async_flush_commands())
        )

    async def _async_flush_commands(self) -> None:
        """Flush all queued commands, using a single API call when possible."""
        self._flush_handle = None
        queue = self._command_queue
        self._command_queue = {}
        callbacks = self._post_flush_callbacks
        self._post_flush_callbacks = []

        if not queue:
            return

        LOGGER.debug(
            "Flushing commands for %d device(s): %s",
            len(queue),
            {url: [c.name for c in cmds] for url, cmds in queue.items()},
        )

        if self._batch_executor.supports_multi_device and len(queue) > 1:
            actions = [
                {"deviceURL": url, "commands": cmds}
                for url, cmds in queue.items()
            ]
            try:
                exec_id = await self._batch_executor.execute_multi(actions)
            except BaseOverkizException as exception:
                LOGGER.error("Multi-device batch failed: %s", exception)
                exec_id = None

            if exec_id:
                LOGGER.debug(
                    "Multi-device batch sent: exec_id=%s, %d device(s)",
                    exec_id,
                    len(queue),
                )
                for device_url in queue:
                    self.executions[exec_id] = {
                        "device_url": device_url,
                        "command_name": "multi-device-batch",
                    }
            else:
                LOGGER.debug("Multi-device batch failed, falling back to per-device")
                await self._execute_per_device(queue)
        else:
            LOGGER.debug("Using per-device execution (single device or adapter unavailable)")
            await self._execute_per_device(queue)

        await self.async_refresh()

        for callback in callbacks:
            await callback()

    async def _execute_per_device(
        self, queue: dict[str, list[Command]]
    ) -> None:
        """Execute queued commands one device at a time (fallback)."""
        for device_url, commands in queue.items():
            LOGGER.debug(
                "Executing %d command(s) for %s: %s",
                len(commands),
                device_url,
                [c.name for c in commands],
            )
            try:
                exec_id = await self.client.execute_commands(
                    device_url, commands, "Home Assistant"
                )
            except BaseOverkizException as exception:
                LOGGER.error(
                    "Failed to execute batched commands for %s: %s",
                    device_url,
                    exception,
                )
                continue

            self.executions[exec_id] = {
                "device_url": device_url,
                "command_name": commands[0].name if commands else "batch",
            }

    def set_update_interval(self, update_interval: timedelta) -> None:
        """Set the update interval and store this value."""
        self.update_interval = update_interval
        self._default_update_interval = update_interval


@EVENT_HANDLERS.register(EventName.DEVICE_AVAILABLE)
async def on_device_available(
    coordinator: OverkizDataUpdateCoordinator, event: Event
) -> None:
    """Handle device available event."""
    if event.device_url:
        coordinator.devices[event.device_url].available = True


@EVENT_HANDLERS.register(EventName.DEVICE_UNAVAILABLE)
@EVENT_HANDLERS.register(EventName.DEVICE_DISABLED)
async def on_device_unavailable_disabled(
    coordinator: OverkizDataUpdateCoordinator, event: Event
) -> None:
    """Handle device unavailable / disabled event."""
    if event.device_url:
        coordinator.devices[event.device_url].available = False


@EVENT_HANDLERS.register(EventName.DEVICE_CREATED)
@EVENT_HANDLERS.register(EventName.DEVICE_UPDATED)
async def on_device_created_updated(
    coordinator: OverkizDataUpdateCoordinator, event: Event
) -> None:
    """Handle device created / updated event."""
    coordinator.hass.async_create_task(
        coordinator.hass.config_entries.async_reload(coordinator.config_entry.entry_id)
    )


@EVENT_HANDLERS.register(EventName.DEVICE_STATE_CHANGED)
async def on_device_state_changed(
    coordinator: OverkizDataUpdateCoordinator, event: Event
) -> None:
    """Handle device state changed event."""
    if not event.device_url:
        return

    for state in event.device_states:
        device = coordinator.devices[event.device_url]
        device.states[state.name] = state


@EVENT_HANDLERS.register(EventName.DEVICE_REMOVED)
async def on_device_removed(
    coordinator: OverkizDataUpdateCoordinator, event: Event
) -> None:
    """Handle device removed event."""
    if not event.device_url:
        return

    base_device_url = event.device_url.split("#")[0]
    registry = dr.async_get(coordinator.hass)

    if registered_device := registry.async_get_device(
        identifiers={(DOMAIN, base_device_url)}
    ):
        registry.async_remove_device(registered_device.id)

    if event.device_url:
        del coordinator.devices[event.device_url]


@EVENT_HANDLERS.register(EventName.EXECUTION_REGISTERED)
async def on_execution_registered(
    coordinator: OverkizDataUpdateCoordinator, event: Event
) -> None:
    """Handle execution registered event."""
    if event.exec_id and event.exec_id not in coordinator.executions:
        coordinator.executions[event.exec_id] = {}

    if not coordinator.is_stateless:
        coordinator.update_interval = timedelta(seconds=2)


@EVENT_HANDLERS.register(EventName.EXECUTION_STATE_CHANGED)
async def on_execution_state_changed(
    coordinator: OverkizDataUpdateCoordinator, event: Event
) -> None:
    """Handle execution changed event."""
    if event.exec_id in coordinator.executions and event.new_state in [
        ExecutionState.COMPLETED,
        ExecutionState.FAILED,
    ]:
        del coordinator.executions[event.exec_id]
