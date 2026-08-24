"""Tests for execution bookkeeping and the poll interval."""

from __future__ import annotations

from unittest.mock import MagicMock

from freezegun.api import FrozenDateTimeFactory
from pyoverkiz.enums import EventName, ExecutionState, OverkizCommand
from pyoverkiz.models import Command, ExecutionStateChangedEvent
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.atlantic_zone_control.const import (
    COMMAND_DEBOUNCE,
    EXECUTION_MAX_AGE,
    UPDATE_INTERVAL,
)
from custom_components.atlantic_zone_control.coordinator import merge_commands
from homeassistant.const import ATTR_ENTITY_ID, ATTR_TEMPERATURE
from homeassistant.core import HomeAssistant

from .conftest import ZONE_URL

ZONE = "climate.zone_control_salon"


def test_merge_commands_keeps_latest_of_each_name() -> None:
    """Repeated taps collapse to the last value for that command."""
    existing = [
        Command(name=OverkizCommand.SET_COOLING_ON_OFF, parameters=["on"]),
        Command(name=OverkizCommand.SET_COOLING_TARGET_TEMPERATURE, parameters=[21.0]),
    ]
    new = [
        Command(name=OverkizCommand.SET_COOLING_TARGET_TEMPERATURE, parameters=[23.0])
    ]

    merged = merge_commands(existing, new)

    assert [c.parameters for c in merged] == [["on"], [23.0]]


def test_merge_commands_preserves_ordering() -> None:
    """Command sequencing must survive deduplication."""
    existing = [Command(name=OverkizCommand.SET_COOLING_ON_OFF, parameters=["on"])]
    new = [
        Command(name=OverkizCommand.SET_COOLING_TARGET_TEMPERATURE, parameters=[23.0]),
        Command(name=OverkizCommand.SET_COOLING_ON_OFF, parameters=["off"]),
    ]

    merged = merge_commands(existing, new)

    assert [str(c.name) for c in merged] == [
        "setCoolingOnOffState",
        "setCoolingTargetTemperature",
    ]
    assert merged[0].parameters == ["off"]


async def test_stale_execution_unpins_the_fast_poll(
    hass: HomeAssistant,
    setup_integration,
    mock_client: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """An execution that never terminates must not hold the 2s poll forever."""
    coordinator = setup_integration.runtime_data.coordinator

    await hass.services.async_call(
        "climate",
        "set_temperature",
        {ATTR_ENTITY_ID: ZONE, ATTR_TEMPERATURE: 22.0},
        blocking=True,
    )
    freezer.tick(COMMAND_DEBOUNCE + 1)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    assert len(coordinator.tracker) == 1

    # No terminal event ever arrives.
    freezer.tick(EXECUTION_MAX_AGE + 10)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    assert len(coordinator.tracker) == 0
    assert coordinator.update_interval == UPDATE_INTERVAL


async def test_not_transmitted_is_treated_as_a_failure(
    hass: HomeAssistant,
    setup_integration,
    mock_client: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """NOT_TRANSMITTED means the commands never reached the appliance."""
    coordinator = setup_integration.runtime_data.coordinator

    await hass.services.async_call(
        "climate",
        "set_temperature",
        {ATTR_ENTITY_ID: ZONE, ATTR_TEMPERATURE: 22.0},
        blocking=True,
    )
    freezer.tick(COMMAND_DEBOUNCE + 1)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    mock_client.fetch_events.return_value = [
        ExecutionStateChangedEvent(
            name=EventName.EXECUTION_STATE_CHANGED,
            exec_id="exec-1",
            old_state=ExecutionState.INITIALIZED,
            new_state=ExecutionState.NOT_TRANSMITTED,
        )
    ]
    freezer.tick(5)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    assert len(coordinator.tracker) == 0
    assert coordinator.update_interval == UPDATE_INTERVAL
    assert hass.states.get(ZONE).attributes[ATTR_TEMPERATURE] == 24.0


async def test_foreign_execution_does_not_roll_back_our_state(
    hass: HomeAssistant,
    setup_integration,
    mock_client: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A Somfy-app execution failing is not evidence our command failed."""
    coordinator = setup_integration.runtime_data.coordinator

    await hass.services.async_call(
        "climate",
        "set_temperature",
        {ATTR_ENTITY_ID: ZONE, ATTR_TEMPERATURE: 22.0},
        blocking=True,
    )
    freezer.tick(COMMAND_DEBOUNCE + 1)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    before = coordinator.optimistic_generation(ZONE_URL)
    coordinator.tracker.register_foreign("exec-somfy-app")
    coordinator.handle_execution_failure(
        ExecutionStateChangedEvent(
            name=EventName.EXECUTION_STATE_CHANGED,
            exec_id="exec-somfy-app",
            old_state=ExecutionState.IN_PROGRESS,
            new_state=ExecutionState.FAILED,
        )
    )

    assert coordinator.optimistic_generation(ZONE_URL) == before
    assert hass.states.get(ZONE).attributes[ATTR_TEMPERATURE] == 22.0
