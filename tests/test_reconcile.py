"""Tests for cloud-as-source-of-truth reconciliation."""

from __future__ import annotations

from unittest.mock import MagicMock

from freezegun.api import FrozenDateTimeFactory
from pyoverkiz.enums import DataType
from pyoverkiz.models import State
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.atlantic_zone_control.const import (
    RECONCILE_INTERVAL,
    UPDATE_INTERVAL,
)
from homeassistant.const import ATTR_TEMPERATURE, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant

from .conftest import SENSOR_URL, ZONE_CONTROL_URL, ZONE_URL

ZONE = "climate.zone_control_salon"


def cooling_target(value: object) -> list[State]:
    """Return a cloud states payload for the zone's cooling setpoint."""
    return [
        State(
            name="core:CoolingTargetTemperatureState",
            type=DataType.FLOAT,
            value=value,
        )
    ]


async def trigger_reconcile(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Advance past the reconcile interval."""
    freezer.tick(RECONCILE_INTERVAL.total_seconds() + 1)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def test_reconcile_covers_entity_backing_urls(
    hass: HomeAssistant, setup_integration
) -> None:
    """Zone control, zone, and the zone's sensor are all reconciled."""
    coordinator = setup_integration.runtime_data.coordinator

    assert coordinator.reconcile_urls == frozenset(
        {ZONE_CONTROL_URL, ZONE_URL, SENSOR_URL}
    )


async def test_reconcile_corrects_drift(
    hass: HomeAssistant,
    setup_integration,
    mock_client: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A change made outside HA is picked up even with no events."""
    assert hass.states.get(ZONE).attributes[ATTR_TEMPERATURE] == 24.0

    mock_client.get_state.side_effect = lambda url: (
        cooling_target(19.5) if url == ZONE_URL else []
    )

    await trigger_reconcile(hass, freezer)

    assert hass.states.get(ZONE).attributes[ATTR_TEMPERATURE] == 19.5


async def test_reconcile_normalizes_string_values(
    hass: HomeAssistant,
    setup_integration,
    mock_client: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Cloud string values must be cast, not written raw over floats."""
    mock_client.get_state.side_effect = lambda url: (
        cooling_target("19.5") if url == ZONE_URL else []
    )

    await trigger_reconcile(hass, freezer)

    assert hass.states.get(ZONE).attributes[ATTR_TEMPERATURE] == 19.5


async def test_reconcile_is_quiet_when_nothing_changed(
    hass: HomeAssistant,
    setup_integration,
    mock_client: MagicMock,
    freezer: FrozenDateTimeFactory,
    caplog,
) -> None:
    """An unchanged value reported as a string must not look like drift."""
    mock_client.get_state.side_effect = lambda url: (
        cooling_target("24.0") if url == ZONE_URL else []
    )

    caplog.clear()
    await trigger_reconcile(hass, freezer)

    assert "Reconcile corrected" not in caplog.text


async def test_reconcile_skipped_while_commands_in_flight(
    hass: HomeAssistant,
    setup_integration,
    mock_client: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Reconciling mid-command would fight the command."""
    coordinator = setup_integration.runtime_data.coordinator
    coordinator.tracker.register("exec-1", [ZONE_URL], ["setCoolingTargetTemperature"])

    await coordinator.async_reconcile()

    mock_client.get_state.assert_not_awaited()


async def test_reconcile_survives_api_error(
    hass: HomeAssistant,
    setup_integration,
    mock_client: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A reconcile failure must never disturb the event stream."""
    mock_client.get_state.side_effect = TimeoutError()

    await trigger_reconcile(hass, freezer)

    assert hass.states.get(ZONE).state is not None


async def test_listener_change_triggers_reconcile(
    hass: HomeAssistant,
    setup_integration,
    mock_client: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A silently re-registered listener means events were lost."""
    mock_client.get_state.reset_mock()
    mock_client.get_state.side_effect = lambda url: (
        cooling_target(18.0) if url == ZONE_URL else []
    )

    # pyoverkiz swallows InvalidEventListenerIdError and re-registers; the only
    # observable trace is the listener id changing across the fetch.
    mock_client.event_listener_id = "listener-2"
    freezer.tick(5)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    assert hass.states.get(ZONE).attributes[ATTR_TEMPERATURE] == 18.0


async def test_update_failure_recovery_triggers_reconcile(
    hass: HomeAssistant,
    setup_integration,
    mock_client: MagicMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Events lost during an outage are recovered on the next success."""
    mock_client.fetch_events.side_effect = TimeoutError()
    freezer.tick(5)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    assert hass.states.get(ZONE).state == STATE_UNAVAILABLE

    mock_client.fetch_events.side_effect = None
    mock_client.get_state.side_effect = lambda url: (
        cooling_target(17.0) if url == ZONE_URL else []
    )
    freezer.tick(UPDATE_INTERVAL.total_seconds() + 1)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    assert hass.states.get(ZONE).attributes[ATTR_TEMPERATURE] == 17.0
