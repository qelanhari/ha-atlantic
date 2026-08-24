"""Regression tests for the optimistic-state lifetime and failure paths."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from freezegun.api import FrozenDateTimeFactory
from pyoverkiz.enums import EventName, ExecutionState, FailureType
from pyoverkiz.exceptions import BaseOverkizError
from pyoverkiz.models import ExecutionStateChangedEvent
import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.atlantic_zone_control.const import (
    COMMAND_DEBOUNCE,
    OPTIMISTIC_MAX_AGE,
)
from homeassistant.components.climate import HVACMode
from homeassistant.const import ATTR_ENTITY_ID, ATTR_TEMPERATURE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError

from .conftest import ZONE_URL, build_devices

ZONE = "climate.zone_control_salon"


def failure_event(exec_id: str) -> ExecutionStateChangedEvent:
    """Build a FAILED execution event."""
    return ExecutionStateChangedEvent(
        name=EventName.EXECUTION_STATE_CHANGED,
        exec_id=exec_id,
        old_state=ExecutionState.IN_PROGRESS,
        new_state=ExecutionState.FAILED,
        failure_type="ACTUATORNOANSWER",
        failure_type_code=FailureType.ACTUATORNOANSWER,
        failed_commands=[{"deviceURL": ZONE_URL, "rank": 0}],
    )


async def set_temperature(hass: HomeAssistant, temperature: float) -> None:
    """Call climate.set_temperature on the zone."""
    await hass.services.async_call(
        "climate",
        "set_temperature",
        {ATTR_ENTITY_ID: ZONE, ATTR_TEMPERATURE: temperature},
        blocking=True,
    )


async def flush(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    """Advance past the command debounce so the queue is sent."""
    freezer.tick(COMMAND_DEBOUNCE + 1)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def test_optimistic_value_is_shown_immediately(
    hass: HomeAssistant, setup_integration
) -> None:
    """The UI must not flicker while the command is debounced."""
    await set_temperature(hass, 22.0)

    assert hass.states.get(ZONE).attributes[ATTR_TEMPERATURE] == 22.0


async def test_optimistic_expires_after_max_age(
    hass: HomeAssistant, setup_integration, freezer: FrozenDateTimeFactory
) -> None:
    """An unconfirmed assumption must not outlive its deadline."""
    await set_temperature(hass, 22.0)
    assert hass.states.get(ZONE).attributes[ATTR_TEMPERATURE] == 22.0

    freezer.tick(OPTIMISTIC_MAX_AGE + 10)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    # Falls back to the real device state (24.0), not the value we asked for.
    assert hass.states.get(ZONE).attributes[ATTR_TEMPERATURE] == 24.0


async def test_failed_execution_rolls_back_optimistic(
    hass: HomeAssistant,
    setup_integration,
    mock_client: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A FAILED execution must refute the assumption it was carrying."""
    await set_temperature(hass, 22.0)
    await flush(hass, freezer)

    assert hass.states.get(ZONE).attributes[ATTR_TEMPERATURE] == 22.0

    mock_client.fetch_events.return_value = [failure_event("exec-1")]
    freezer.tick(5)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    assert hass.states.get(ZONE).attributes[ATTR_TEMPERATURE] == 24.0


async def test_send_error_rolls_back_optimistic(
    hass: HomeAssistant,
    setup_integration,
    mock_client: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A transport error must not leave the assumption standing."""
    mock_client.execute_action_group.side_effect = BaseOverkizError("boom")

    await set_temperature(hass, 22.0)
    await flush(hass, freezer)

    assert hass.states.get(ZONE).attributes[ATTR_TEMPERATURE] == 24.0


async def test_flush_survives_non_overkiz_error(
    hass: HomeAssistant,
    setup_integration,
    mock_client: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """aiohttp/timeout errors used to escape the detached flush task."""
    mock_client.execute_action_group.side_effect = TimeoutError()

    await set_temperature(hass, 22.0)
    await flush(hass, freezer)

    assert hass.states.get(ZONE).attributes[ATTR_TEMPERATURE] == 24.0
    assert mock_client.fetch_events.await_count > 1


async def test_corrective_command_is_sent_when_state_diverges(
    hass: HomeAssistant,
    setup_integration,
    mock_client: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The lock-out regression: re-asking must not be skipped."""
    await set_temperature(hass, 22.0)
    await flush(hass, freezer)
    mock_client.execute_action_group.reset_mock()

    mock_client.fetch_events.return_value = [failure_event("exec-1")]
    freezer.tick(5)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    mock_client.fetch_events.return_value = []

    # Asking again for the value the device never adopted must send a command.
    await set_temperature(hass, 22.0)
    await flush(hass, freezer)

    assert mock_client.execute_action_group.await_count == 1


async def test_repeated_sets_are_deduplicated(
    hass: HomeAssistant,
    setup_integration,
    mock_client: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Five +/- taps must not stack five identical commands."""
    for temperature in (21.0, 21.5, 22.0, 22.5, 23.0):
        await set_temperature(hass, temperature)

    await flush(hass, freezer)

    actions = mock_client.execute_action_group.await_args.kwargs["actions"]
    assert len(actions) == 1
    assert len(actions[0].commands) == 1
    assert actions[0].commands[0].parameters == [23.0]


async def test_optimistic_survives_transient_update_failure(
    hass: HomeAssistant,
    setup_integration,
    mock_client: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A poll blip is not evidence the command failed (anti-flicker)."""
    await set_temperature(hass, 22.0)
    await flush(hass, freezer)

    mock_client.fetch_events.side_effect = TimeoutError()
    freezer.tick(5)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    mock_client.fetch_events.side_effect = None

    entity = hass.data["entity_components"]["climate"].get_entity(ZONE)
    assert entity._get_optimistic("temperature") == 22.0


async def test_stop_mode_rejects_commands(
    hass: HomeAssistant, config_entry, mock_client: MagicMock
) -> None:
    """With the system stopped there is no setpoint to change."""
    stopped = build_devices(operating_mode="stop")
    mock_client.get_devices.return_value = stopped
    mock_client.get_setup.return_value.devices = stopped

    config_entry.add_to_hass(hass)
    with patch(
        "custom_components.atlantic_zone_control.OverkizClient",
        return_value=mock_client,
    ):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    with pytest.raises(ServiceValidationError):
        await set_temperature(hass, 22.0)


async def test_unmapped_operating_mode_does_not_raise(
    hass: HomeAssistant, config_entry, mock_client: MagicMock
) -> None:
    """An unknown operating mode must not raise KeyError from a state write."""
    weird = build_devices(operating_mode="internalScheduling")
    mock_client.get_devices.return_value = weird
    mock_client.get_setup.return_value.devices = weird

    config_entry.add_to_hass(hass)
    with patch(
        "custom_components.atlantic_zone_control.OverkizClient",
        return_value=mock_client,
    ):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    assert hass.states.get("climate.zone_control").state == HVACMode.OFF
