"""Tests for mode commands, batch execution, and failure retry."""

from __future__ import annotations

from unittest.mock import MagicMock

from freezegun.api import FrozenDateTimeFactory
from pyoverkiz.enums import OverkizCommand
from pyoverkiz.exceptions import BaseOverkizError, ExecutionQueueFullError
from pyoverkiz.models import Command
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.atlantic_zone_control.const import COMMAND_DEBOUNCE
from homeassistant.components.climate import HVACMode
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant

from .conftest import ZONE_CONTROL_URL, ZONE_URL

TWO_DEVICE_QUEUE = {
    ZONE_URL: [Command(name=OverkizCommand.SET_COOLING_ON_OFF, parameters=["on"])],
    ZONE_CONTROL_URL: [
        Command(
            name=OverkizCommand.SET_PASS_APC_OPERATING_MODE, parameters=["heating"]
        )
    ],
}

ZONE = "climate.zone_control_salon"
SYSTEM = "climate.zone_control"


async def set_mode(hass: HomeAssistant, entity: str, mode: str) -> None:
    """Call climate.set_hvac_mode."""
    await hass.services.async_call(
        "climate",
        "set_hvac_mode",
        {ATTR_ENTITY_ID: entity, "hvac_mode": mode},
        blocking=True,
    )


async def flush(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    """Advance past the command debounce."""
    freezer.tick(COMMAND_DEBOUNCE + 1)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


def sent_commands(mock_client: MagicMock) -> list[str]:
    """Return the command names of the most recent action group."""
    actions = mock_client.execute_action_group.await_args.kwargs["actions"]
    return [str(c.name) for action in actions for c in action.commands]


async def test_zone_off_sends_cooling_off(
    hass: HomeAssistant,
    setup_integration,
    mock_client: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Turning a cooling zone off uses the cooling command, not heating."""
    await set_mode(hass, ZONE, HVACMode.OFF)
    await flush(hass, freezer)

    assert sent_commands(mock_client) == ["setCoolingOnOffState"]
    assert hass.states.get(ZONE).state == HVACMode.OFF


async def test_zone_already_in_mode_is_skipped(
    hass: HomeAssistant,
    setup_integration,
    mock_client: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The zone is already on; asking for AUTO sends nothing."""
    await set_mode(hass, ZONE, HVACMode.AUTO)
    await flush(hass, freezer)

    mock_client.execute_action_group.assert_not_awaited()


async def test_system_mode_change_sends_operating_mode(
    hass: HomeAssistant,
    setup_integration,
    mock_client: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Switching the system to heat sets the operating mode."""
    await set_mode(hass, SYSTEM, HVACMode.HEAT)
    assert hass.states.get(SYSTEM).state == HVACMode.HEAT

    await flush(hass, freezer)

    assert sent_commands(mock_client) == ["setPassAPCOperatingMode"]


async def test_system_mode_change_schedules_zone_mode_refresh(
    hass: HomeAssistant,
    setup_integration,
    mock_client: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A heat/cool switch must poke the zones' mode-specific states.

    The refresh commands follow the operating mode the device currently
    reports, which here is still cooling: the switch was sent but no state
    event has confirmed it yet.
    """
    await set_mode(hass, SYSTEM, HVACMode.HEAT)
    await flush(hass, freezer)
    mock_client.execute_action_group.reset_mock()

    freezer.tick(3)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    assert sent_commands(mock_client) == [
        "refreshPassAPCCoolingMode",
        "refreshPassAPCCoolingProfile",
        "refreshTargetTemperature",
    ]


async def test_system_mode_already_set_is_skipped(
    hass: HomeAssistant,
    setup_integration,
    mock_client: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The system already reports cooling; asking for COOL sends nothing."""
    await set_mode(hass, SYSTEM, HVACMode.COOL)
    await flush(hass, freezer)

    mock_client.execute_action_group.assert_not_awaited()


async def test_multi_device_queue_uses_one_action_group(
    hass: HomeAssistant, setup_integration, mock_client: MagicMock
) -> None:
    """Several devices are sent as a single execution."""
    coordinator = setup_integration.runtime_data.coordinator

    await coordinator._send_queue(dict(TWO_DEVICE_QUEUE))

    assert mock_client.execute_action_group.await_count == 1
    actions = mock_client.execute_action_group.await_args.kwargs["actions"]
    assert len(actions) == 2
    assert len(coordinator.tracker) == 1


async def test_batch_failure_falls_back_to_per_device(
    hass: HomeAssistant, setup_integration, mock_client: MagicMock
) -> None:
    """A rejected batch is retried one device at a time."""
    coordinator = setup_integration.runtime_data.coordinator
    calls: list[int] = []

    async def _execute(*, label: str, actions: list) -> str:
        calls.append(len(actions))
        if len(actions) > 1:
            raise BaseOverkizError("batch rejected")
        return f"exec-{len(calls)}"

    mock_client.execute_action_group.side_effect = _execute

    await coordinator._send_queue(dict(TWO_DEVICE_QUEUE))

    assert calls == [2, 1, 1]
    assert len(coordinator.tracker) == 2


async def test_queue_full_retries_then_gives_up(
    hass: HomeAssistant,
    setup_integration,
    mock_client: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """EXEC_QUEUE_FULL is retried briefly, then the assumption is withdrawn."""
    coordinator = setup_integration.runtime_data.coordinator
    mock_client.execute_action_group.side_effect = ExecutionQueueFullError("full")

    await set_mode(hass, ZONE, HVACMode.OFF)
    before = coordinator.optimistic_generation(ZONE_URL)

    for _ in range(4):
        freezer.tick(60)
        async_fire_time_changed(hass)
        await hass.async_block_till_done()

    assert mock_client.execute_action_group.await_count == 3
    assert coordinator.optimistic_generation(ZONE_URL) > before
    assert hass.states.get(ZONE).state == HVACMode.AUTO


async def test_unload_releases_the_event_listener(
    hass: HomeAssistant, setup_integration, mock_client: MagicMock
) -> None:
    """Every reload would otherwise leak a server-side listener."""
    assert await hass.config_entries.async_unload(setup_integration.entry_id)
    await hass.async_block_till_done()

    mock_client.unregister_event_listener.assert_awaited_once()


async def test_diagnostics_report_state(
    hass: HomeAssistant, setup_integration, mock_client: MagicMock
) -> None:
    """Diagnostics expose what is needed to debug drift."""
    from custom_components.atlantic_zone_control.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    await set_mode(hass, ZONE, HVACMode.OFF)
    result = await async_get_config_entry_diagnostics(hass, setup_integration)

    assert result["entry"]["username"] == "**REDACTED**"
    assert result["entry"]["password"] == "**REDACTED**"
    assert sorted(result["reconcile_urls"]) == sorted(
        setup_integration.runtime_data.coordinator.reconcile_urls
    )
    assert result["optimistic"][ZONE]["hvac_mode"]["value"] == HVACMode.OFF
    assert len(result["devices"]) == 3
