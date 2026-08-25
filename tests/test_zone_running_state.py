"""The zone's on/off must come from the profile, not core:*OnOffState.

Reproduces a live divergence: every zone reported core:CoolingOnOffState
'off' while the salon was actually cooling. The profile state and
core:TargetTemperatureState both said so; only the OnOff state disagreed,
and the API has no command to refresh it.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from pyoverkiz.enums import DataType
from pyoverkiz.models import EventState
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.components.climate import HVACAction, HVACMode
from homeassistant.core import HomeAssistant

from .conftest import ZONE_URL, build_devices

ZONE = "climate.zone_control_salon"


async def setup_with(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_client: MagicMock,
    **kwargs: str,
) -> None:
    """Set the integration up with a specific device state."""
    devices = build_devices(**kwargs)
    mock_client.get_devices.return_value = devices
    mock_client.get_setup.return_value.devices = devices

    config_entry.add_to_hass(hass)
    with patch(
        "custom_components.atlantic_zone_control.OverkizClient",
        return_value=mock_client,
    ):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()


async def test_running_zone_reported_on_despite_stale_on_off(
    hass: HomeAssistant, config_entry, mock_client: MagicMock
) -> None:
    """The live failure: profile says manu, the stale OnOff state says off."""
    await setup_with(
        hass, config_entry, mock_client, cooling_profile="manu", cooling_on_off="off"
    )

    state = hass.states.get(ZONE)
    assert state.state == HVACMode.AUTO
    assert state.attributes["hvac_action"] == HVACAction.COOLING


async def test_stopped_zone_reported_off(
    hass: HomeAssistant, config_entry, mock_client: MagicMock
) -> None:
    """A stopped profile is off, even if the OnOff state still says on."""
    await setup_with(
        hass, config_entry, mock_client, cooling_profile="stop", cooling_on_off="on"
    )

    state = hass.states.get(ZONE)
    assert state.state == HVACMode.OFF
    assert state.attributes["hvac_action"] == HVACAction.IDLE


async def test_non_stop_profiles_are_all_running(
    hass: HomeAssistant, config_entry, mock_client: MagicMock
) -> None:
    """comfort/eco/derogation all mean the zone is conditioning.

    `cooling_on_off="off"` matters: without it the OnOff fallback would also
    return AUTO and this test would pass against the old code.
    """
    await setup_with(
        hass, config_entry, mock_client, cooling_profile="comfort", cooling_on_off="off"
    )

    assert hass.states.get(ZONE).state == HVACMode.AUTO


async def test_falls_back_to_on_off_without_a_profile(
    hass: HomeAssistant, config_entry, mock_client: MagicMock
) -> None:
    """Devices that report no profile still work off the OnOff state."""
    await setup_with(
        hass, config_entry, mock_client, cooling_profile=None, cooling_on_off="on"
    )

    assert hass.states.get(ZONE).state == HVACMode.AUTO


async def test_fallback_device_off_is_idle_not_cooling(
    hass: HomeAssistant, config_entry, mock_client: MagicMock
) -> None:
    """A zone with no profile reported must still report IDLE when off."""
    await setup_with(
        hass, config_entry, mock_client, cooling_profile=None, cooling_on_off="off"
    )

    state = hass.states.get(ZONE)
    assert state.state == HVACMode.OFF
    assert state.attributes["hvac_action"] == HVACAction.IDLE


async def test_heating_mode_uses_the_heating_profile(
    hass: HomeAssistant, config_entry, mock_client: MagicMock
) -> None:
    """The heating profile drives the zone when the system is heating.

    The fixture's heating profile is `stop`, so a heating system must report
    the zone off even though its cooling profile says `manu`.
    """
    await setup_with(
        hass,
        config_entry,
        mock_client,
        operating_mode="heating",
        cooling_profile="manu",
    )

    state = hass.states.get(ZONE)
    assert state.state == HVACMode.OFF
    assert state.attributes["hvac_action"] == HVACAction.IDLE


async def test_fresh_on_off_beats_stale_profile(
    hass: HomeAssistant, setup_integration, mock_client: MagicMock
) -> None:
    """2026-08-25: zone commanded on, profile refresh never landed.

    core:CoolingOnOffState arrived by event and says on; the profile is still
    the `stop` loaded at startup. The zone is genuinely running, so the fresher
    signal must win.
    """
    coordinator = setup_integration.runtime_data.coordinator
    device = coordinator.devices[ZONE_URL]

    device.states["io:PassAPCCoolingProfileState"] = EventState(
        name="io:PassAPCCoolingProfileState", type=DataType.STRING, value="stop"
    )
    device.states["core:CoolingOnOffState"] = EventState(
        name="core:CoolingOnOffState", type=DataType.STRING, value="on"
    )
    coordinator.note_state_update(ZONE_URL, "core:CoolingOnOffState")
    coordinator.async_update_listeners()
    await hass.async_block_till_done()

    assert hass.states.get(ZONE).state == HVACMode.AUTO


async def test_fresh_profile_beats_stale_on_off(
    hass: HomeAssistant, setup_integration, mock_client: MagicMock
) -> None:
    """2026-08-24: zone running, core:CoolingOnOffState stale at off.

    The mirror image of the case above; the profile is the fresher signal here.
    """
    coordinator = setup_integration.runtime_data.coordinator
    device = coordinator.devices[ZONE_URL]

    device.states["core:CoolingOnOffState"] = EventState(
        name="core:CoolingOnOffState", type=DataType.STRING, value="off"
    )
    device.states["io:PassAPCCoolingProfileState"] = EventState(
        name="io:PassAPCCoolingProfileState", type=DataType.STRING, value="manu"
    )
    coordinator.note_state_update(ZONE_URL, "io:PassAPCCoolingProfileState")
    coordinator.async_update_listeners()
    await hass.async_block_till_done()

    assert hass.states.get(ZONE).state == HVACMode.AUTO


async def test_profile_wins_without_recency_information(
    hass: HomeAssistant, config_entry, mock_client: MagicMock
) -> None:
    """At startup nothing has a timestamp, so the profile decides."""
    await setup_with(
        hass, config_entry, mock_client, cooling_profile="manu", cooling_on_off="off"
    )

    assert hass.states.get(ZONE).state == HVACMode.AUTO


async def test_both_off_stays_off_regardless_of_recency(
    hass: HomeAssistant, setup_integration, mock_client: MagicMock
) -> None:
    """When the two signals agree, recency cannot flip the answer."""
    coordinator = setup_integration.runtime_data.coordinator
    device = coordinator.devices[ZONE_URL]

    device.states["core:CoolingOnOffState"] = EventState(
        name="core:CoolingOnOffState", type=DataType.STRING, value="off"
    )
    device.states["io:PassAPCCoolingProfileState"] = EventState(
        name="io:PassAPCCoolingProfileState", type=DataType.STRING, value="stop"
    )
    coordinator.note_state_update(ZONE_URL, "core:CoolingOnOffState")
    coordinator.async_update_listeners()
    await hass.async_block_till_done()

    assert hass.states.get(ZONE).state == HVACMode.OFF
