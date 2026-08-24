"""Support for Atlantic Pass APC Zone Control climate entities."""

from __future__ import annotations

import logging
from typing import Any, cast

from pyoverkiz.enums import OverkizCommand, OverkizCommandParam, OverkizState, UIWidget
from pyoverkiz.models import Command

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import (
    ATTR_TEMPERATURE,
    PRECISION_HALVES,
    Platform,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import AtlanticZoneControlConfigEntry
from .const import DOMAIN, TEMPERATURE_TOLERANCE
from .coordinator import OverkizDataUpdateCoordinator
from .entity import OverkizEntity
from .executor import OverkizExecutor
from .optimistic import OptimisticStateMixin

# Zone Control mode mappings
OVERKIZ_TO_HVAC_MODE: dict[str, HVACMode] = {
    OverkizCommandParam.HEATING: HVACMode.HEAT,
    OverkizCommandParam.DRYING: HVACMode.DRY,
    OverkizCommandParam.COOLING: HVACMode.COOL,
    OverkizCommandParam.STOP: HVACMode.OFF,
}

HVAC_MODE_TO_OVERKIZ = {v: k for k, v in OVERKIZ_TO_HVAC_MODE.items()}

# Zone Control HVAC action mapping
OVERKIZ_TO_HVAC_ACTION: dict[str, HVACAction] = {
    OverkizCommandParam.COOLING: HVACAction.COOLING,
    OverkizCommandParam.DRYING: HVACAction.DRYING,
    OverkizCommandParam.HEATING: HVACAction.HEATING,
    OverkizCommandParam.STOP: HVACAction.OFF,
}

ZONE_CONTROL_DEVICE_INDEX = 1

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AtlanticZoneControlConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Atlantic Zone Control climate entities."""
    data = entry.runtime_data
    coordinator = data.coordinator

    entities: list[ClimateEntity] = []

    for device in data.platforms.get(Platform.CLIMATE, []):
        if device.widget == UIWidget.ATLANTIC_PASS_APC_ZONE_CONTROL:
            entities.append(
                AtlanticPassAPCZoneControl(device.device_url, coordinator)
            )
        elif device.widget == UIWidget.ATLANTIC_PASS_APC_HEATING_AND_COOLING_ZONE:
            entities.append(
                AtlanticPassAPCZoneControlZone(device.device_url, coordinator)
            )

    async_add_entities(entities)


class AtlanticPassAPCZoneControl(OptimisticStateMixin, OverkizEntity, ClimateEntity):
    """Representation of Atlantic Pass APC Zone Control (system mode)."""

    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_supported_features = (
        ClimateEntityFeature.TURN_OFF | ClimateEntityFeature.TURN_ON
    )
    _enable_turn_on_off_backwards_compatibility = False

    def __init__(
        self, device_url: str, coordinator: OverkizDataUpdateCoordinator
    ) -> None:
        """Init method."""
        super().__init__(device_url, coordinator)

        self._attr_hvac_modes = [*HVAC_MODE_TO_OVERKIZ]

        if self._is_auto_available:
            self._attr_hvac_modes.append(HVACMode.AUTO)

    @property
    def _is_auto_available(self) -> bool:
        """Check if auto mode is available."""
        return self.executor.has_command(
            OverkizCommand.SET_HEATING_COOLING_AUTO_SWITCH
        ) and self.executor.has_state(OverkizState.CORE_HEATING_COOLING_AUTO_SWITCH)

    @property
    def _real_hvac_mode(self) -> HVACMode:
        """Return the actual hvac mode from device state."""
        if (
            self._is_auto_available
            and cast(
                str,
                self.executor.select_state(
                    OverkizState.CORE_HEATING_COOLING_AUTO_SWITCH
                ),
            )
            == OverkizCommandParam.ON
        ):
            return HVACMode.AUTO

        raw = self.executor.select_state(OverkizState.IO_PASS_APC_OPERATING_MODE)
        mode = OVERKIZ_TO_HVAC_MODE.get(cast(str, raw)) if raw is not None else None

        if mode is None:
            _LOGGER.debug(
                "Unmapped operating mode %r for %s, reporting OFF", raw, self.device_url
            )
            return HVACMode.OFF

        return mode

    @property
    def hvac_mode(self) -> HVACMode:
        """Return hvac operation ie. heat, cool mode."""
        optimistic = self._get_optimistic("hvac_mode")
        if optimistic is not None:
            return cast(HVACMode, optimistic)
        return self._real_hvac_mode

    def _handle_coordinator_update(self) -> None:
        """Drop optimistic state the device has confirmed or refuted."""
        self._reconcile_optimistic(
            {"hvac_mode": lambda wanted: self._real_hvac_mode == wanted}
        )
        super()._handle_coordinator_update()

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set new target hvac mode."""
        pending = self.coordinator.has_pending_commands(self.device_url)
        if not pending:
            await self.coordinator.async_ensure_fresh()

        # Compared against real state, never the optimistic value: comparing
        # against our own assumption is what used to make a wrong assumption
        # permanent. Never skip while a batch is in flight, since that batch
        # may be carrying a different target.
        if not pending and hvac_mode == self._real_hvac_mode:
            _LOGGER.debug("Zone control already in %s, skipping", hvac_mode)
            return

        commands: list[Command] = []

        if self._is_auto_available:
            auto_switch = (
                OverkizCommandParam.ON
                if hvac_mode == HVACMode.AUTO
                else OverkizCommandParam.OFF
            )
            commands.append(
                Command(
                    name=OverkizCommand.SET_HEATING_COOLING_AUTO_SWITCH,
                    parameters=[auto_switch],
                )
            )

        if hvac_mode != HVACMode.AUTO:
            commands.append(
                Command(
                    name=OverkizCommand.SET_PASS_APC_OPERATING_MODE,
                    parameters=[HVAC_MODE_TO_OVERKIZ[hvac_mode]],
                )
            )

        if commands:
            self._queue_optimistically(
                "hvac_mode", hvac_mode, commands, needs_mode_refresh=True
            )


class AtlanticPassAPCZoneControlZone(
    OptimisticStateMixin, OverkizEntity, ClimateEntity
):
    """An Atlantic Pass APC zone (simplified: AUTO/OFF plus a single setpoint)."""

    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_target_temperature_step = PRECISION_HALVES
    _attr_hvac_modes = [HVACMode.AUTO, HVACMode.OFF]
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.TURN_OFF
        | ClimateEntityFeature.TURN_ON
    )
    _enable_turn_on_off_backwards_compatibility = False

    def __init__(
        self, device_url: str, coordinator: OverkizDataUpdateCoordinator
    ) -> None:
        """Init method."""
        super().__init__(device_url, coordinator)

        self._zone_control_executor: OverkizExecutor | None = None

        if (
            zone_control_device := self.executor.linked_device(
                ZONE_CONTROL_DEVICE_INDEX
            )
        ) is not None:
            self._zone_control_executor = OverkizExecutor(
                zone_control_device.device_url,
                coordinator,
            )

    @property
    def _zone_control_mode(self) -> str | None:
        """Return the zone control operating mode (heating/cooling/stop/drying)."""
        if self._zone_control_executor is not None:
            return cast(
                str,
                self._zone_control_executor.select_state(
                    OverkizState.IO_PASS_APC_OPERATING_MODE
                ),
            )
        return None

    @property
    def _is_heating_mode(self) -> bool:
        """Return True if zone control is in heating mode."""
        return self._zone_control_mode == OverkizCommandParam.HEATING

    @property
    def _is_cooling_mode(self) -> bool:
        """Return True if zone control is in cooling mode."""
        return self._zone_control_mode == OverkizCommandParam.COOLING

    @property
    def current_temperature(self) -> float | None:
        """Return the current temperature from the linked sensor."""
        # Temperature sensor is at device index + 1
        if self.index_device_url:
            sensor_index = int(self.index_device_url) + 1
            sensor_device = self.executor.linked_device(sensor_index)
            if sensor_device is not None and (
                temp_state := sensor_device.states.get(OverkizState.CORE_TEMPERATURE)
            ):
                return cast(float, temp_state.value)
        return None

    @property
    def _real_target_temperature(self) -> float | None:
        """Return the actual target temperature from device state."""
        if self._is_cooling_mode:
            return cast(
                float,
                self.executor.select_state(
                    OverkizState.CORE_COOLING_TARGET_TEMPERATURE
                ),
            )

        if self._is_heating_mode:
            return cast(
                float,
                self.executor.select_state(
                    OverkizState.CORE_HEATING_TARGET_TEMPERATURE
                ),
            )

        return None

    @property
    def target_temperature(self) -> float | None:
        """Return target temperature based on zone control mode."""
        optimistic = self._get_optimistic("temperature")
        if optimistic is not None:
            return cast(float, optimistic)
        return self._real_target_temperature

    @property
    def _profile_state(self) -> str | None:
        """Return the active profile for the system's current mode.

        The profile reports what the zone is actually doing right now:
        `stop` means it is not conditioning, any other value names the
        setpoint in use (`manu`, `comfort`, `eco`, ...).
        """
        if self._is_heating_mode:
            state = OverkizState.IO_PASS_APC_HEATING_PROFILE
        elif self._is_cooling_mode:
            state = OverkizState.IO_PASS_APC_COOLING_PROFILE
        else:
            return None

        return cast(str | None, self.executor.select_state(state))

    @property
    def _on_off_state(self) -> str | None:
        """Return the zone's on/off state for the system's current mode."""
        if self._is_heating_mode:
            state = OverkizState.CORE_HEATING_ON_OFF
        elif self._is_cooling_mode:
            state = OverkizState.CORE_COOLING_ON_OFF
        else:
            return None

        return cast(str | None, self.executor.select_state(state))

    @property
    def _real_hvac_mode(self) -> HVACMode:
        """Return the actual hvac mode from device state.

        Derived from the profile, not from core:*OnOffState. The API has no
        refreshCoolingOnOffState/refreshHeatingOnOffState command, so the OnOff
        states are only ever updated by an event -- miss one and the zone reads
        off indefinitely. (The widget does expose a generic `advancedRefresh`
        which might reach them; untested.) The profile carries the same
        information, is refreshable via refreshPassAPC*Profile, and is what the
        appliance actually acts on. `stop` is the only non-conditioning value of
        the eight it can report. The OnOff state is consulted only when no
        profile is reported.

        Residual limit: if the post-command profile refresh never lands, the
        profile stays stale and this reports the old value until the hourly
        zones refresh corrects it. Confirming from core:*OnOffState instead
        would not help -- it clears the optimistic value sooner and then falls
        through to the same stale profile, so it makes the display wrong
        earlier rather than later.
        """
        if (profile := self._profile_state) is not None:
            if profile == OverkizCommandParam.STOP:
                return HVACMode.OFF
            return HVACMode.AUTO

        if self._on_off_state == OverkizCommandParam.ON:
            return HVACMode.AUTO

        return HVACMode.OFF

    @property
    def hvac_mode(self) -> HVACMode:
        """Return hvac mode: AUTO if on, OFF if off."""
        optimistic = self._get_optimistic("hvac_mode")
        if optimistic is not None:
            return cast(HVACMode, optimistic)
        return self._real_hvac_mode

    def _temperature_confirmed(self, wanted: float) -> bool:
        """Return True once the device reports the temperature we asked for.

        Tolerant, and explicitly False when the device reports nothing: the
        target is unreadable while the system sits in stop/drying, and an
        exact comparison against None would hold the assumption forever.
        """
        real = self._real_target_temperature
        return real is not None and abs(real - wanted) <= TEMPERATURE_TOLERANCE

    def _handle_coordinator_update(self) -> None:
        """Drop optimistic state the device has confirmed or refuted."""
        self._reconcile_optimistic(
            {
                "hvac_mode": lambda wanted: self._real_hvac_mode == wanted,
                "temperature": self._temperature_confirmed,
            }
        )
        super()._handle_coordinator_update()

    @property
    def hvac_action(self) -> HVACAction | None:
        """Return the current running hvac action."""
        zone_mode = self._zone_control_mode
        if zone_mode is None:
            return HVACAction.OFF

        action = OVERKIZ_TO_HVAC_ACTION.get(zone_mode, HVACAction.OFF)

        # The system is conditioning, but this zone's vent is closed.
        # Deliberately keyed on hvac_mode, not the raw profile: hvac_mode is
        # already profile-driven, and it also covers the optimistic window and
        # devices that report no profile at all.
        if (
            action in (HVACAction.HEATING, HVACAction.COOLING)
            and self.hvac_mode == HVACMode.OFF
        ):
            return HVACAction.IDLE

        return action

    @property
    def min_temp(self) -> float:
        """Return minimum temperature."""
        if self._is_heating_mode:
            temp = self.executor.select_state(
                OverkizState.CORE_MINIMUM_HEATING_TARGET_TEMPERATURE
            )
            if temp is not None:
                return cast(float, temp)

        if self._is_cooling_mode:
            temp = self.executor.select_state(
                OverkizState.CORE_MINIMUM_COOLING_TARGET_TEMPERATURE
            )
            if temp is not None:
                return cast(float, temp)

        return super().min_temp

    @property
    def max_temp(self) -> float:
        """Return maximum temperature."""
        if self._is_heating_mode:
            temp = self.executor.select_state(
                OverkizState.CORE_MAXIMUM_HEATING_TARGET_TEMPERATURE
            )
            if temp is not None:
                return cast(float, temp)

        if self._is_cooling_mode:
            temp = self.executor.select_state(
                OverkizState.CORE_MAXIMUM_COOLING_TARGET_TEMPERATURE
            )
            if temp is not None:
                return cast(float, temp)

        return super().max_temp

    def _raise_if_zone_uncontrollable(self) -> None:
        """Reject commands the system cannot act on.

        With the zone control stopped or drying there is no heating/cooling
        command to send. This used to fall through silently and report success
        while doing nothing.
        """
        if self._is_heating_mode or self._is_cooling_mode:
            return

        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="zone_control_inactive",
            translation_placeholders={
                "name": self.name or self.entity_id,
                "mode": str(self._zone_control_mode),
            },
        )

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set new target hvac mode (AUTO=on+manual, OFF=off)."""
        if not self.coordinator.has_pending_commands(self.device_url):
            await self.coordinator.async_ensure_fresh()

        self._raise_if_zone_uncontrollable()

        commands: list[Command] = []
        is_on = self._real_hvac_mode == HVACMode.AUTO

        if hvac_mode == HVACMode.AUTO:
            if self._is_heating_mode:
                if not is_on:
                    commands.append(
                        Command(
                            name=OverkizCommand.SET_HEATING_ON_OFF,
                            parameters=[OverkizCommandParam.ON],
                        )
                    )
                heating_mode = self.executor.select_state(
                    OverkizState.IO_PASS_APC_HEATING_MODE
                )
                if heating_mode != OverkizCommandParam.MANU:
                    commands.append(
                        Command(
                            name=OverkizCommand.SET_PASS_APC_HEATING_MODE,
                            parameters=[OverkizCommandParam.MANU],
                        )
                    )
            elif self._is_cooling_mode:
                if not is_on:
                    commands.append(
                        Command(
                            name=OverkizCommand.SET_COOLING_ON_OFF,
                            parameters=[OverkizCommandParam.ON],
                        )
                    )
                cooling_mode = self.executor.select_state(
                    OverkizState.IO_PASS_APC_COOLING_MODE
                )
                if cooling_mode != OverkizCommandParam.MANU:
                    commands.append(
                        Command(
                            name=OverkizCommand.SET_PASS_APC_COOLING_MODE,
                            parameters=[OverkizCommandParam.MANU],
                        )
                    )
        elif hvac_mode == HVACMode.OFF:
            if self._is_heating_mode and is_on:
                commands.append(
                    Command(
                        name=OverkizCommand.SET_HEATING_ON_OFF,
                        parameters=[OverkizCommandParam.OFF],
                    )
                )
            elif self._is_cooling_mode and is_on:
                commands.append(
                    Command(
                        name=OverkizCommand.SET_COOLING_ON_OFF,
                        parameters=[OverkizCommandParam.OFF],
                    )
                )

        if not commands:
            # Genuinely already in the requested state. Withdraw any stale
            # assumption rather than leaving it to be displayed.
            _LOGGER.debug("Zone %s already in %s, skipping", self.name, hvac_mode)
            self._clear_optimistic("hvac_mode")
            self.async_write_ha_state()
            return

        self._queue_optimistically(
            "hvac_mode", hvac_mode, commands, needs_mode_refresh=True
        )

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set new target temperature based on zone control mode."""
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is None:
            return

        pending = self.coordinator.has_pending_commands(self.device_url)
        if not pending:
            await self.coordinator.async_ensure_fresh()

        self._raise_if_zone_uncontrollable()

        # Compared against real state only. Skipping while a batch is still
        # queued would let that batch land a value we then never correct.
        if not pending and self._temperature_confirmed(temperature):
            _LOGGER.debug("Zone %s already at %.1f°C, skipping", self.name, temperature)
            self._clear_optimistic("temperature")
            self.async_write_ha_state()
            return

        commands: list[Command] = []

        if self._is_heating_mode:
            commands.append(
                Command(
                    name=OverkizCommand.SET_HEATING_TARGET_TEMPERATURE,
                    parameters=[temperature],
                )
            )
        elif self._is_cooling_mode:
            commands.append(
                Command(
                    name=OverkizCommand.SET_COOLING_TARGET_TEMPERATURE,
                    parameters=[temperature],
                )
            )

        self._queue_optimistically("temperature", temperature, commands)
