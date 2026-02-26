"""Class for helpers and communication with the Overkiz API."""

from __future__ import annotations

from urllib.parse import urlparse

from pyoverkiz.models import Device
from pyoverkiz.types import StateType as OverkizStateType

from .coordinator import OverkizDataUpdateCoordinator


class OverkizExecutor:
    """Representation of an Overkiz device with execution handler."""

    def __init__(
        self, device_url: str, coordinator: OverkizDataUpdateCoordinator
    ) -> None:
        """Initialize the executor."""
        self.device_url = device_url
        self.coordinator = coordinator
        self.base_device_url = self.device_url.split("#")[0]

    @property
    def device(self) -> Device:
        """Return Overkiz device linked to this entity."""
        return self.coordinator.data[self.device_url]

    def linked_device(self, index: int) -> Device | None:
        """Return Overkiz device sharing the same base url."""
        return self.coordinator.data.get(f"{self.base_device_url}#{index}")

    def select_command(self, *commands: str) -> str | None:
        """Select first existing command in a list of commands."""
        existing_commands = self.device.definition.commands
        return next((c for c in commands if c in existing_commands), None)

    def has_command(self, *commands: str) -> bool:
        """Return True if a command exists in a list of commands."""
        return self.select_command(*commands) is not None

    def select_state(self, *states: str) -> OverkizStateType:
        """Select first existing active state in a list of states."""
        for state in states:
            if current_state := self.device.states[state]:
                return current_state.value

        return None

    def has_state(self, *states: str) -> bool:
        """Return True if a state exists in self."""
        return self.select_state(*states) is not None

    def select_attribute(self, *attributes: str) -> OverkizStateType:
        """Select first existing active state in a list of states."""
        for attribute in attributes:
            if current_attribute := self.device.attributes[attribute]:
                return current_attribute.value

        return None

    def get_gateway_id(self) -> str:
        """Retrieve gateway id from device url."""
        url = urlparse(self.device_url)
        return url.netloc
