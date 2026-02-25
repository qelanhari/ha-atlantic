"""Support for Atlantic Pass APC Zone Control climate entities."""

from __future__ import annotations

from asyncio import sleep
from typing import Any, cast

from pyoverkiz.enums import OverkizCommand, OverkizCommandParam, OverkizState, UIWidget
from pyoverkiz.models import Command

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, PRECISION_HALVES, UnitOfTemperature
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import AtlanticZoneControlConfigEntry
from .coordinator import OverkizDataUpdateCoordinator
from .entity import OverkizEntity
from .executor import OverkizExecutor

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


class AtlanticPassAPCZoneControl(OverkizEntity, ClimateEntity):
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
    def hvac_mode(self) -> HVACMode:
        """Return hvac operation ie. heat, cool mode."""
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

        return OVERKIZ_TO_HVAC_MODE[
            cast(
                str,
                self.executor.select_state(OverkizState.IO_PASS_APC_OPERATING_MODE),
            )
        ]

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set new target hvac mode."""
        commands: list[Command] = []

        if self._is_auto_available:
            auto_switch = (
                OverkizCommandParam.ON
                if hvac_mode == HVACMode.AUTO
                else OverkizCommandParam.OFF
            )
            commands.append(
                Command(OverkizCommand.SET_HEATING_COOLING_AUTO_SWITCH, [auto_switch])
            )

        if hvac_mode != HVACMode.AUTO:
            commands.append(
                Command(
                    OverkizCommand.SET_PASS_APC_OPERATING_MODE,
                    [HVAC_MODE_TO_OVERKIZ[hvac_mode]],
                )
            )

        if commands:
            await self.executor.async_execute_commands(commands)


class AtlanticPassAPCZoneControlZone(OverkizEntity, ClimateEntity):
    """Representation of an Atlantic Pass APC Zone (simplified: AUTO/OFF + single temp)."""

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
            if sensor_device := self.executor.linked_device(sensor_index):
                if temp_state := sensor_device.states[OverkizState.CORE_TEMPERATURE]:
                    return cast(float, temp_state.value)
        return None

    @property
    def target_temperature(self) -> float | None:
        """Return target temperature based on zone control mode."""
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
    def hvac_mode(self) -> HVACMode:
        """Return hvac mode: AUTO if on, OFF if off."""
        if self._is_heating_mode:
            heating_state = cast(
                str,
                self.executor.select_state(OverkizState.CORE_HEATING_ON_OFF),
            )
            if heating_state == OverkizCommandParam.ON:
                return HVACMode.AUTO
            return HVACMode.OFF

        if self._is_cooling_mode:
            cooling_state = cast(
                str,
                self.executor.select_state(OverkizState.CORE_COOLING_ON_OFF),
            )
            if cooling_state == OverkizCommandParam.ON:
                return HVACMode.AUTO
            return HVACMode.OFF

        return HVACMode.OFF

    @property
    def hvac_action(self) -> HVACAction | None:
        """Return the current running hvac action."""
        zone_mode = self._zone_control_mode
        if zone_mode is None:
            return HVACAction.OFF

        action = OVERKIZ_TO_HVAC_ACTION.get(zone_mode, HVACAction.OFF)

        # If the zone control is heating/cooling but this zone is off, it's idle
        if action in (HVACAction.HEATING, HVACAction.COOLING):
            if self.hvac_mode == HVACMode.OFF:
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

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set new target hvac mode (AUTO=on+manual, OFF=off)."""
        commands: list[Command] = []

        if hvac_mode == HVACMode.AUTO:
            if self._is_heating_mode:
                commands = [
                    Command(OverkizCommand.SET_HEATING_ON_OFF, [OverkizCommandParam.ON]),
                    Command(
                        OverkizCommand.SET_PASS_APC_HEATING_MODE,
                        [OverkizCommandParam.MANU],
                    ),
                ]
            elif self._is_cooling_mode:
                commands = [
                    Command(OverkizCommand.SET_COOLING_ON_OFF, [OverkizCommandParam.ON]),
                    Command(
                        OverkizCommand.SET_PASS_APC_COOLING_MODE,
                        [OverkizCommandParam.MANU],
                    ),
                ]
        elif hvac_mode == HVACMode.OFF:
            if self._is_heating_mode:
                commands = [
                    Command(
                        OverkizCommand.SET_HEATING_ON_OFF, [OverkizCommandParam.OFF]
                    ),
                ]
            elif self._is_cooling_mode:
                commands = [
                    Command(
                        OverkizCommand.SET_COOLING_ON_OFF, [OverkizCommandParam.OFF]
                    ),
                ]

        if commands:
            await self.executor.async_execute_commands(commands)
            await self._async_refresh_modes()

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set new target temperature based on zone control mode."""
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is None:
            return

        commands: list[Command] = []

        if self._is_heating_mode:
            commands.append(
                Command(
                    OverkizCommand.SET_HEATING_TARGET_TEMPERATURE, [temperature]
                )
            )
        elif self._is_cooling_mode:
            commands.append(
                Command(
                    OverkizCommand.SET_COOLING_TARGET_TEMPERATURE, [temperature]
                )
            )

        if commands:
            await self.executor.async_execute_commands(commands)

    async def _async_refresh_modes(self) -> None:
        """Refresh device modes to get updated states."""
        await sleep(2)

        await self.executor.async_execute_command(
            OverkizCommand.REFRESH_PASS_APC_HEATING_MODE
        )
        await self.executor.async_execute_command(
            OverkizCommand.REFRESH_PASS_APC_HEATING_PROFILE
        )
        await self.executor.async_execute_command(
            OverkizCommand.REFRESH_PASS_APC_COOLING_MODE
        )
        await self.executor.async_execute_command(
            OverkizCommand.REFRESH_PASS_APC_COOLING_PROFILE
        )
        await self.executor.async_execute_command(
            OverkizCommand.REFRESH_TARGET_TEMPERATURE
        )
