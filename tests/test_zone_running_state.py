"""A zone's on/off comes from core:*OnOffState alone.

io:PassAPC*ProfileState names where the setpoint comes from, not whether the
zone runs. Live, 2026-10-04: Bureau was off (OnOff `off`) while its heating
profile read `comfort`, then `externalSetpoint` after the remote was used,
then nothing at all after re-sending `manu`.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.components.climate import HVACAction, HVACMode
from homeassistant.core import HomeAssistant

from .conftest import build_devices

ZONE = "climate.zone_control_salon"


async def setup_with(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_client: MagicMock,
    **kwargs: str | None,
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


def mode_and_action(hass: HomeAssistant) -> tuple[str, str]:
    """Return the zone's state and hvac_action."""
    state = hass.states.get(ZONE)
    return state.state, state.attributes["hvac_action"]


async def test_on_zone_is_running(
    hass: HomeAssistant, config_entry, mock_client: MagicMock
) -> None:
    """OnOff on: the zone is on and conditioning."""
    await setup_with(hass, config_entry, mock_client, cooling_on_off="on")

    assert mode_and_action(hass) == (HVACMode.AUTO, HVACAction.COOLING)


async def test_off_zone_is_idle_whatever_the_profile(
    hass: HomeAssistant, config_entry, mock_client: MagicMock
) -> None:
    """A setpoint-source profile does not make an off zone run."""
    await setup_with(
        hass, config_entry, mock_client, cooling_profile="manu", cooling_on_off="off"
    )

    assert mode_and_action(hass) == (HVACMode.OFF, HVACAction.IDLE)


async def test_bureau_external_setpoint_is_off(
    hass: HomeAssistant, config_entry, mock_client: MagicMock
) -> None:
    """2026-10-04, Bureau: heating, profile externalSetpoint, OnOff off."""
    await setup_with(
        hass,
        config_entry,
        mock_client,
        operating_mode="heating",
        heating_profile="externalSetpoint",
        heating_on_off="off",
    )

    assert mode_and_action(hass) == (HVACMode.OFF, HVACAction.IDLE)


async def test_bureau_comfort_is_off(
    hass: HomeAssistant, config_entry, mock_client: MagicMock
) -> None:
    """2026-10-04, Bureau: heating, profile comfort, OnOff off."""
    await setup_with(
        hass,
        config_entry,
        mock_client,
        operating_mode="heating",
        heating_profile="comfort",
        heating_on_off="off",
    )

    assert hass.states.get(ZONE).state == HVACMode.OFF


async def test_stop_profile_does_not_turn_an_on_zone_off(
    hass: HomeAssistant, config_entry, mock_client: MagicMock
) -> None:
    """OnOff on wins over a `stop` profile."""
    await setup_with(
        hass, config_entry, mock_client, cooling_profile="stop", cooling_on_off="on"
    )

    assert hass.states.get(ZONE).state == HVACMode.AUTO


async def test_works_without_a_profile(
    hass: HomeAssistant, config_entry, mock_client: MagicMock
) -> None:
    """Devices that report no profile are unaffected."""
    await setup_with(
        hass, config_entry, mock_client, cooling_profile=None, cooling_on_off="on"
    )

    assert hass.states.get(ZONE).state == HVACMode.AUTO


async def test_heating_mode_uses_the_heating_on_off(
    hass: HomeAssistant, config_entry, mock_client: MagicMock
) -> None:
    """A heating system reads core:HeatingOnOffState, not the cooling one."""
    await setup_with(
        hass,
        config_entry,
        mock_client,
        operating_mode="heating",
        cooling_on_off="on",
        heating_on_off="off",
    )

    assert hass.states.get(ZONE).state == HVACMode.OFF
