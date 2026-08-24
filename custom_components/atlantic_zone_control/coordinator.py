"""Helpers to help coordinate updates."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine, Iterable
from datetime import datetime, timedelta
import logging
import time
from typing import TYPE_CHECKING, Any, cast

from aiohttp import ClientConnectorError, ClientError, ServerDisconnectedError
from pyoverkiz.client import OverkizClient
from pyoverkiz.enums import (
    EventName,
    ExecutionState,
    OverkizCommand,
    OverkizCommandParam,
    OverkizState,
    Protocol,
)
from pyoverkiz.exceptions import (
    BadCredentialsError,
    BaseOverkizError,
    ExecutionQueueFullError,
    InvalidEventListenerIdError,
    MaintenanceError,
    NotAuthenticatedError,
    ServiceUnavailableError,
    TooManyConcurrentRequestsError,
    TooManyRequestsError,
)
from pyoverkiz.models import (
    Action,
    Command,
    Device,
    DeviceEvent,
    DeviceStateChangedEvent,
    EventState,
    ExecutionRegisteredEvent,
    ExecutionStateChangedEvent,
    FailureEvent,
    Place,
    State,
)

from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util.decorator import Registry

if TYPE_CHECKING:
    from . import AtlanticZoneControlConfigEntry

from .command_tracker import CommandTracker
from .const import (
    COMMAND_DEBOUNCE,
    DOMAIN,
    EVENT_COMMAND_FAILED,
    EXECUTION_MAX_AGE,
    EXECUTION_UPDATE_INTERVAL,
    FRESHNESS_MAX_AGE,
    IGNORED_OVERKIZ_DEVICES,
    LOGGER,
    UPDATE_INTERVAL,
)

EVENT_HANDLERS: Registry[
    str, Callable[[OverkizDataUpdateCoordinator, Any], Coroutine[Any, Any, None]]
] = Registry()

# Errors that mean "the command did not reach the server". Broader than
# BaseOverkizError on purpose: aiohttp and timeout errors used to escape the
# detached flush task and strand optimistic state.
SEND_ERRORS: tuple[type[Exception], ...] = (BaseOverkizError, ClientError, TimeoutError)

# Executions that ended without the commands being applied.
TERMINAL_FAILURE_STATES: tuple[ExecutionState, ...] = (
    ExecutionState.FAILED,
    ExecutionState.NOT_TRANSMITTED,
)

# The exec category is rate-limited to roughly one call per 29 minutes, so
# retrying a full queue is mostly futile. Try twice, then surface the failure.
QUEUE_FULL_RETRY_DELAYS: tuple[int, ...] = (5, 15)


def merge_commands(existing: list[Command], new: list[Command]) -> list[Command]:
    """Merge command lists, keeping only the latest of each command name.

    Repeated +/- taps otherwise stack several ``setHeatingTargetTemperature``
    commands into one action. Keying on a dict preserves first-insertion order,
    so sequencing between different commands survives.
    """
    merged: dict[str, Command] = {str(command.name): command for command in existing}
    superseded = [str(c.name) for c in new if str(c.name) in merged]
    merged.update({str(command.name): command for command in new})

    if superseded:
        LOGGER.debug("Superseded duplicate command(s): %s", superseded)

    return list(merged.values())


class OverkizBatchExecutor:
    """Executes commands across multiple devices in a single API call.

    Wraps the public OverkizClient.execute_action_group API, which accepts a
    list of per-device Action objects and applies them in one execution.
    """

    def __init__(self, client: OverkizClient) -> None:
        """Initialize the batch executor."""
        self._client = client

    async def execute_multi(
        self,
        queue: dict[str, list[Command]],
        label: str = "Home Assistant",
    ) -> str:
        """Execute a multi-device batch. Returns the exec_id."""
        actions = [
            Action(device_url=device_url, commands=commands)
            for device_url, commands in queue.items()
        ]
        LOGGER.debug("Sending action group: %s", [a.to_payload() for a in actions])
        return await self._client.execute_action_group(label=label, actions=actions)


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
        self.tracker = CommandTracker()
        self.areas = self._places_to_area(places) if places else None
        self._default_update_interval = UPDATE_INTERVAL
        self.last_refresh_time: float = 0

        self.is_stateless = all(
            device.identifier.protocol in (Protocol.RTS, Protocol.INTERNAL)
            for device in devices
            if device.widget not in IGNORED_OVERKIZ_DEVICES
        )

        self._command_queue: dict[str, list[Command]] = {}
        self._flush_unsub: Callable[[], None] | None = None
        self._mode_refresh_unsub: Callable[[], None] | None = None
        self._refresh_device_urls: set[str] = set()
        self._batch_executor = OverkizBatchExecutor(client)
        self._optimistic_generation: dict[str, int] = {}
        self._refresh_lock = asyncio.Lock()
        self._flush_attempt = 0
        self._consecutive_failures = 0
        self._degraded = False
        self.reconcile_urls: frozenset[str] = frozenset()

    # ------------------------------------------------------------------
    # Optimistic-state invalidation
    # ------------------------------------------------------------------

    def invalidate_optimistic(self, device_urls: Iterable[str]) -> None:
        """Refute any optimistic assumptions held for these devices.

        A monotonic per-device counter rather than a dispatcher signal: it is
        bumped inside _async_update_data, before listeners fan out, so entities
        observe it on exactly the right tick with no listener lifecycle.
        """
        for device_url in device_urls:
            self._optimistic_generation[device_url] = (
                self._optimistic_generation.get(device_url, 0) + 1
            )

    def optimistic_generation(self, device_url: str) -> int:
        """Return the current invalidation generation for a device."""
        return self._optimistic_generation.get(device_url, 0)

    # ------------------------------------------------------------------
    # Polling
    # ------------------------------------------------------------------

    async def _async_update_data(self) -> dict[str, Device]:
        """Fetch Overkiz data via event listener."""
        self._sweep_stale_executions()
        listener_before = self.client.event_listener_id

        try:
            devices = await self._async_fetch_and_apply_events()
        except UpdateFailed:
            self._degraded = True
            raise
        finally:
            self._restore_update_interval()
            self.last_refresh_time = time.monotonic()

        self._note_listener_change(listener_before)
        self._schedule_recovery_reconcile()

        return devices

    async def _async_fetch_and_apply_events(self) -> dict[str, Device]:
        """Fetch pending events and apply them to the in-memory devices."""
        try:
            events = await self.client.fetch_events()
        except (BadCredentialsError, NotAuthenticatedError) as exception:
            raise ConfigEntryAuthFailed("Invalid authentication.") from exception
        except TooManyConcurrentRequestsError as exception:
            raise UpdateFailed("Too many concurrent requests.") from exception
        except TooManyRequestsError as exception:
            raise UpdateFailed("Too many requests, try again later.") from exception
        except MaintenanceError as exception:
            raise UpdateFailed("Server is down for maintenance.") from exception
        except ServiceUnavailableError as exception:
            raise UpdateFailed("Server is unavailable.") from exception
        except InvalidEventListenerIdError as exception:
            raise UpdateFailed(exception) from exception
        except (TimeoutError, ClientConnectorError) as exception:
            LOGGER.debug("Failed to connect", exc_info=True)
            raise UpdateFailed("Failed to connect.") from exception
        except ServerDisconnectedError:
            return await self._async_recover_session()

        if self.tracker:
            LOGGER.debug("Executions in flight: %s", self.tracker.snapshot())

        for event in events:
            LOGGER.debug(event)

            if isinstance(event, FailureEvent):
                LOGGER.warning(
                    "Overkiz reported %s: failure_type=%s device=%s gateway=%s",
                    event.name,
                    event.failure_type,
                    event.device_url,
                    event.gateway_id,
                )

            if event_handler := EVENT_HANDLERS.get(event.name):
                await event_handler(self, event)

        return self.devices

    async def _async_recover_session(self) -> dict[str, Device]:
        """Re-login and reload devices after the server dropped the session."""
        self.tracker = CommandTracker()
        self._degraded = True

        try:
            await self.client.login()
            self.devices = await self._get_devices()
        except (BadCredentialsError, NotAuthenticatedError) as exception:
            raise ConfigEntryAuthFailed("Invalid authentication.") from exception
        except TooManyRequestsError as exception:
            raise UpdateFailed("Too many requests, try again later.") from exception

        return self.devices

    def _restore_update_interval(self) -> None:
        """Drop back to the slow poll once nothing is in flight."""
        if not self.tracker:
            self.update_interval = self._default_update_interval

    def _sweep_stale_executions(self) -> None:
        """Expire executions that never reported a terminal state."""
        for stale in self.tracker.sweep(EXECUTION_MAX_AGE):
            LOGGER.warning(
                "Execution %s timed out after %.0fs with no terminal state "
                "(devices: %s, commands: %s)",
                stale.exec_id,
                stale.age,
                sorted(stale.device_urls),
                list(stale.command_names),
            )
            self.invalidate_optimistic(stale.device_urls)

    @callback
    def _note_listener_change(self, listener_before: str | None) -> None:
        """Flag a reconcile if pyoverkiz silently re-registered the listener.

        retry_on_listener_error swallows InvalidEventListenerIdError and
        registers a fresh listener; every event emitted in that gap is lost.
        """
        listener_after = self.client.event_listener_id
        if listener_before is not None and listener_before != listener_after:
            LOGGER.warning(
                "Event listener was re-registered; events may have been missed"
            )
            self._degraded = True

    @callback
    def _schedule_recovery_reconcile(self) -> None:
        """Reconcile once after recovering from a degraded event stream."""
        if not self._degraded:
            return
        self._degraded = False
        self.config_entry.async_create_background_task(
            self.hass, self.async_reconcile(), "atlantic_zone_control_reconcile"
        )

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

    # ------------------------------------------------------------------
    # Reconciliation - the cloud is the source of truth
    # ------------------------------------------------------------------

    async def async_reconcile(self, _now: datetime | None = None) -> None:
        """Re-read entity-backing states from the cloud and correct drift.

        The event stream is the primary channel, but events lost to a dropped
        listener or a failed poll are gone for good. This re-reads the states
        that actually back entities. Must never raise: it runs from a timer and
        a failure here must not disturb the event stream.
        """
        if self._command_queue or self.tracker:
            LOGGER.debug("Skipping reconcile: commands in flight")
            return

        try:
            corrections = await self._async_reconcile_states()
        except SEND_ERRORS as exception:
            LOGGER.debug("Reconcile failed (non-fatal): %s", exception)
            return

        if corrections:
            LOGGER.debug("Reconcile applied %d correction(s)", corrections)
            self.async_update_listeners()
        else:
            LOGGER.debug(
                "Reconcile clean: %d device(s) checked", len(self.reconcile_urls)
            )

    async def _async_reconcile_states(self) -> int:
        """Merge fresh cloud states into the in-memory devices."""
        corrections = 0

        for device_url in sorted(self.reconcile_urls):
            device = self.devices.get(device_url)
            if device is None:
                continue

            for state in await self.client.get_state(device_url):
                corrections += self._apply_reconciled_state(device, state)

        return corrections

    def _apply_reconciled_state(self, device: Device, state: State) -> int:
        """Apply one reconciled state, returning 1 if it corrected a value."""
        normalized = self._normalize_state(state)
        if normalized is None:
            return 0

        current = device.states.get(normalized.name)
        if current is not None and current.value == normalized.value:
            return 0

        LOGGER.info(
            "Reconcile corrected %s.%s: %s -> %s",
            device.device_url,
            normalized.name,
            None if current is None else current.value,
            normalized.value,
        )
        device.states[normalized.name] = normalized
        return 1

    @staticmethod
    def _normalize_state(state: State) -> State | None:
        """Cast a raw state the way the event pipeline does.

        get_state() returns plain State objects; the cloud API reports every
        value as a string. Comparing those against event-sourced values without
        casting would report a correction on every single cycle and would break
        float maths downstream.
        """
        try:
            return EventState(name=state.name, type=state.type, value=state.value)
        except (ValueError, TypeError) as exception:
            LOGGER.debug("Could not normalize state %s: %s", state.name, exception)
            return None

    # ------------------------------------------------------------------
    # Freshness
    # ------------------------------------------------------------------

    async def async_ensure_fresh(self, max_age: float = FRESHNESS_MAX_AGE) -> None:
        """Await a real refresh if the in-memory data is older than max_age.

        Undebounced, unlike async_request_refresh, so the caller genuinely gets
        fresh data. The lock is required: without it a scene touching several
        zones fires concurrent fetch_events calls against a 1/second limit.
        """
        if time.monotonic() - self.last_refresh_time <= max_age:
            return

        async with self._refresh_lock:
            if time.monotonic() - self.last_refresh_time <= max_age:
                return
            await self.async_refresh()

    # ------------------------------------------------------------------
    # Command queue
    # ------------------------------------------------------------------

    def has_pending_commands(self, device_url: str) -> bool:
        """Return True if there are queued commands for a device."""
        return bool(self._command_queue.get(device_url))

    def queue_commands(
        self,
        device_url: str,
        commands: list[Command],
        needs_mode_refresh: bool = False,
    ) -> None:
        """Queue commands for a device, flushing after a short delay."""
        self._command_queue[device_url] = merge_commands(
            self._command_queue.get(device_url, []), commands
        )
        LOGGER.debug(
            "Queued %d command(s) for %s, now pending: %s",
            len(commands),
            device_url,
            [str(c.name) for c in self._command_queue[device_url]],
        )

        if needs_mode_refresh:
            self._refresh_device_urls.add(device_url)

        if self._flush_unsub is not None:
            self._flush_unsub()

        # Start fast-polling immediately so we pick up state changes sooner
        if not self.is_stateless:
            self.update_interval = EXECUTION_UPDATE_INTERVAL

        self._flush_unsub = async_call_later(
            self.hass, COMMAND_DEBOUNCE, self._async_flush_commands_callback
        )

    async def _async_flush_commands_callback(self, _now: Any = None) -> None:
        """Run the flush from the async_call_later timer."""
        await self._async_flush_commands()

    async def _async_flush_commands(self) -> None:
        """Flush all queued commands, using a single API call when possible."""
        self._flush_unsub = None
        queue, refresh_urls = self._take_queue()

        if not queue:
            return

        try:
            await self._send_queue(queue)
        except Exception:  # noqa: BLE001 - detached task; must not strand state
            LOGGER.exception("Unexpected error flushing commands for %s", list(queue))
            self.invalidate_optimistic(queue)
        finally:
            await self._safe_refresh()
            self._schedule_mode_refresh(refresh_urls)

    def _take_queue(self) -> tuple[dict[str, list[Command]], set[str]]:
        """Atomically claim the pending queue and mode-refresh set."""
        queue = self._command_queue
        self._command_queue = {}
        refresh_urls = self._refresh_device_urls
        self._refresh_device_urls = set()

        if queue:
            LOGGER.debug(
                "Flushing commands for %d device(s): %s",
                len(queue),
                {url: [str(c.name) for c in cmds] for url, cmds in queue.items()},
            )

        return queue, refresh_urls

    async def _send_queue(self, queue: dict[str, list[Command]]) -> None:
        """Send a queue as one batch, falling back to per-device execution."""
        try:
            if len(queue) > 1:
                await self._send_batch(queue)
            else:
                LOGGER.debug("Using per-device execution (single device)")
                await self._execute_per_device(queue)
        except ExecutionQueueFullError as exception:
            self._requeue_after_queue_full(queue, exception)
            return

        self._flush_attempt = 0

    async def _send_batch(self, queue: dict[str, list[Command]]) -> None:
        """Send every device's commands in one action group."""
        try:
            exec_id = await self._batch_executor.execute_multi(queue)
        except ExecutionQueueFullError:
            raise
        except SEND_ERRORS as exception:
            LOGGER.error("Multi-device batch failed, falling back: %s", exception)
            await self._execute_per_device(queue)
            return

        LOGGER.debug(
            "Multi-device batch sent: exec_id=%s, %d device(s)", exec_id, len(queue)
        )
        self.tracker.register(
            exec_id,
            queue.keys(),
            [str(c.name) for commands in queue.values() for c in commands],
        )

    def _requeue_after_queue_full(
        self, queue: dict[str, list[Command]], exception: Exception
    ) -> None:
        """Re-arm the flush after the gateway's execution queue filled up.

        pyoverkiz defines retry_on_execution_queue_full but never applies it.
        The exec category is rate-limited to roughly one call per 29 minutes,
        so try only briefly, then give up and refute the optimistic state.
        """
        if self._flush_attempt >= len(QUEUE_FULL_RETRY_DELAYS):
            LOGGER.error(
                "Execution queue still full after %d attempts, dropping commands "
                "for %s: %s",
                self._flush_attempt,
                list(queue),
                exception,
            )
            self._flush_attempt = 0
            self.invalidate_optimistic(queue)
            return

        delay = QUEUE_FULL_RETRY_DELAYS[self._flush_attempt]
        self._flush_attempt += 1
        LOGGER.warning(
            "Execution queue full, retrying %d device(s) in %ds: %s",
            len(queue),
            delay,
            exception,
        )

        for device_url, commands in queue.items():
            self._command_queue[device_url] = merge_commands(
                commands, self._command_queue.get(device_url, [])
            )

        if self._flush_unsub is not None:
            self._flush_unsub()
        self._flush_unsub = async_call_later(
            self.hass, delay, self._async_flush_commands_callback
        )

    async def _execute_per_device(self, queue: dict[str, list[Command]]) -> None:
        """Execute queued commands one device at a time (fallback)."""
        for device_url, commands in queue.items():
            LOGGER.debug(
                "Executing %d command(s) for %s: %s",
                len(commands),
                device_url,
                [str(c.name) for c in commands],
            )
            try:
                exec_id = await self.client.execute_action_group(
                    label="Home Assistant",
                    actions=[Action(device_url=device_url, commands=commands)],
                )
            except ExecutionQueueFullError:
                raise
            except SEND_ERRORS as exception:
                LOGGER.error(
                    "Failed to execute batched commands for %s: %s",
                    device_url,
                    exception,
                )
                self.invalidate_optimistic([device_url])
                continue

            self.tracker.register(
                exec_id, [device_url], [str(c.name) for c in commands]
            )

    async def _safe_refresh(self) -> None:
        """Refresh without letting a failure break the rest of the flush."""
        try:
            await self.async_refresh()
        except SEND_ERRORS as exception:
            LOGGER.debug("Post-command refresh failed: %s", exception)

    @callback
    def _schedule_mode_refresh(self, refresh_urls: set[str]) -> None:
        """Schedule a follow-up refresh of heating/cooling mode states."""
        if not refresh_urls:
            return

        if self._mode_refresh_unsub is not None:
            self._mode_refresh_unsub()

        self._mode_refresh_unsub = async_call_later(
            self.hass, 2, self._make_refresh_modes_callback(refresh_urls)
        )

    # ------------------------------------------------------------------
    # Mode refresh
    # ------------------------------------------------------------------

    def _get_operating_mode(self, device_url: str) -> str | None:
        """Return the zone control operating mode from the #1 sibling device."""
        base_url = device_url.split("#")[0]
        zone_control_url = f"{base_url}#1"
        zone_control = self.devices.get(zone_control_url)
        if zone_control is None:
            return None
        state = zone_control.states.get(OverkizState.IO_PASS_APC_OPERATING_MODE)
        if state is None:
            return None
        return cast(str, state.value)

    @callback
    def _make_refresh_modes_callback(
        self, refresh_urls: set[str]
    ) -> Callable[[datetime], None]:
        """Create a callback that schedules a mode refresh."""

        @callback
        def _refresh_callback(_now: datetime) -> None:
            self._mode_refresh_unsub = None
            self.hass.async_create_task(self._async_refresh_modes(refresh_urls))

        return _refresh_callback

    @staticmethod
    def _refresh_commands_for(operating_mode: str | None) -> list[Command]:
        """Return the refresh commands appropriate to an operating mode."""
        if operating_mode == OverkizCommandParam.COOLING:
            return [
                Command(name=OverkizCommand.REFRESH_PASS_APC_COOLING_MODE),
                Command(name=OverkizCommand.REFRESH_PASS_APC_COOLING_PROFILE),
                Command(name=OverkizCommand.REFRESH_TARGET_TEMPERATURE),
            ]
        return [
            Command(name=OverkizCommand.REFRESH_PASS_APC_HEATING_MODE),
            Command(name=OverkizCommand.REFRESH_PASS_APC_HEATING_PROFILE),
            Command(name=OverkizCommand.REFRESH_TARGET_TEMPERATURE),
        ]

    async def _async_refresh_modes(self, device_urls: set[str]) -> None:
        """Refresh mode states for devices, batched into a single API call."""
        # Resolved per device: the operating mode is a property of each
        # device's own zone control, not of an arbitrary member of the set.
        refresh_queue = {
            url: self._refresh_commands_for(self._get_operating_mode(url))
            for url in device_urls
        }

        LOGGER.debug(
            "Refreshing modes for %d device(s): %s",
            len(refresh_queue),
            sorted(refresh_queue),
        )

        try:
            await self._send_queue(refresh_queue)
        except Exception:  # noqa: BLE001 - detached task
            LOGGER.exception("Unexpected error refreshing modes for %s", device_urls)

    # ------------------------------------------------------------------
    # Execution outcomes
    # ------------------------------------------------------------------

    @callback
    def handle_execution_failure(self, event: ExecutionStateChangedEvent) -> None:
        """Refute optimistic state and report why an execution failed."""
        pending = self.tracker.pop(event.exec_id)
        failure_code = event.failure_type_code
        LOGGER.error(
            "Execution %s %s -> %s failed for %s (commands: %s): "
            "failure_type=%s code=%s(%s) failed_commands=%s",
            event.exec_id,
            event.old_state,
            event.new_state,
            sorted(pending.device_urls) if pending else "unknown device(s)",
            list(pending.command_names) if pending else [],
            event.failure_type,
            failure_code.name if failure_code is not None else None,
            int(failure_code) if failure_code is not None else None,
            event.failed_commands,
        )

        self._consecutive_failures += 1

        if pending is None or not pending.device_urls:
            return

        self.invalidate_optimistic(pending.device_urls)
        self.hass.bus.async_fire(
            EVENT_COMMAND_FAILED,
            {
                "device_urls": sorted(pending.device_urls),
                "command_names": list(pending.command_names),
                "failure_type": event.failure_type,
                "failure_type_code": (
                    failure_code.name if failure_code is not None else None
                ),
            },
        )

    @callback
    def handle_execution_success(self, exec_id: str) -> None:
        """Forget a completed execution."""
        if (pending := self.tracker.pop(exec_id)) is not None:
            LOGGER.debug(
                "Execution %s completed for %s after %.1fs",
                exec_id,
                sorted(pending.device_urls),
                pending.age,
            )
        self._consecutive_failures = 0

    @property
    def consecutive_failures(self) -> int:
        """Return how many executions have failed in a row."""
        return self._consecutive_failures

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def cancel_pending_flush(self) -> None:
        """Cancel any pending flush and mode-refresh timers."""
        if self._flush_unsub is not None:
            self._flush_unsub()
            self._flush_unsub = None

        if self._mode_refresh_unsub is not None:
            self._mode_refresh_unsub()
            self._mode_refresh_unsub = None

        if self._command_queue:
            LOGGER.warning(
                "Dropping %d unsent command batch(es) on unload: %s",
                len(self._command_queue),
                list(self._command_queue),
            )
            self.invalidate_optimistic(self._command_queue)
            self._command_queue = {}

    def set_update_interval(self, update_interval: timedelta) -> None:
        """Set the update interval and store this value."""
        self.update_interval = update_interval
        self._default_update_interval = update_interval


@EVENT_HANDLERS.register(EventName.DEVICE_AVAILABLE)
async def on_device_available(
    coordinator: OverkizDataUpdateCoordinator, event: DeviceEvent
) -> None:
    """Handle device available event."""
    if event.device_url in coordinator.devices:
        coordinator.devices[event.device_url].available = True


@EVENT_HANDLERS.register(EventName.DEVICE_UNAVAILABLE)
@EVENT_HANDLERS.register(EventName.DEVICE_DISABLED)
async def on_device_unavailable_disabled(
    coordinator: OverkizDataUpdateCoordinator, event: DeviceEvent
) -> None:
    """Handle device unavailable / disabled event."""
    if event.device_url in coordinator.devices:
        coordinator.devices[event.device_url].available = False


@EVENT_HANDLERS.register(EventName.DEVICE_CREATED)
@EVENT_HANDLERS.register(EventName.DEVICE_UPDATED)
async def on_device_created_updated(
    coordinator: OverkizDataUpdateCoordinator, event: DeviceEvent
) -> None:
    """Handle device created / updated event."""
    coordinator.hass.async_create_task(
        coordinator.hass.config_entries.async_reload(coordinator.config_entry.entry_id)
    )


@EVENT_HANDLERS.register(EventName.DEVICE_STATE_CHANGED)
async def on_device_state_changed(
    coordinator: OverkizDataUpdateCoordinator, event: DeviceStateChangedEvent
) -> None:
    """Handle device state changed event."""
    device = coordinator.devices.get(event.device_url)
    if device is None:
        return

    for state in event.device_states:
        device.states[state.name] = state


@EVENT_HANDLERS.register(EventName.DEVICE_REMOVED)
async def on_device_removed(
    coordinator: OverkizDataUpdateCoordinator, event: DeviceEvent
) -> None:
    """Handle device removed event."""
    base_device_url = event.device_url.split("#")[0]
    registry = dr.async_get(coordinator.hass)

    if registered_device := registry.async_get_device(
        identifiers={(DOMAIN, base_device_url)}
    ):
        registry.async_remove_device(registered_device.id)

    if event.device_url in coordinator.devices:
        del coordinator.devices[event.device_url]


@EVENT_HANDLERS.register(EventName.EXECUTION_REGISTERED)
async def on_execution_registered(
    coordinator: OverkizDataUpdateCoordinator, event: ExecutionRegisteredEvent
) -> None:
    """Handle execution registered event."""
    coordinator.tracker.register_foreign(event.exec_id)

    if not coordinator.is_stateless:
        coordinator.update_interval = EXECUTION_UPDATE_INTERVAL


@EVENT_HANDLERS.register(EventName.EXECUTION_STATE_CHANGED)
async def on_execution_state_changed(
    coordinator: OverkizDataUpdateCoordinator, event: ExecutionStateChangedEvent
) -> None:
    """Handle execution changed event."""
    if event.new_state in TERMINAL_FAILURE_STATES:
        coordinator.handle_execution_failure(event)
    elif event.new_state is ExecutionState.COMPLETED:
        coordinator.handle_execution_success(event.exec_id)
    else:
        coordinator.tracker.touch(event.exec_id)
