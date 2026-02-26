"""Parent class for every Atlantic Zone Control entity."""

from __future__ import annotations

import time
from typing import cast

from pyoverkiz.enums import OverkizAttribute, OverkizState
from pyoverkiz.models import Device

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import OverkizDataUpdateCoordinator
from .executor import OverkizExecutor


class OverkizEntity(CoordinatorEntity[OverkizDataUpdateCoordinator]):
    """Representation of an Overkiz device entity."""

    _attr_has_entity_name = True
    _attr_name: str | None = None

    def __init__(
        self, device_url: str, coordinator: OverkizDataUpdateCoordinator
    ) -> None:
        """Initialize the device."""
        super().__init__(coordinator)
        self.device_url = device_url
        split_device_url = self.device_url.split("#")
        self.base_device_url = split_device_url[0]
        self.index_device_url: str | None = (
            split_device_url[1] if len(split_device_url) == 2 else None
        )
        self.executor = OverkizExecutor(device_url, coordinator)

        self._attr_assumed_state = not self.device.states
        self._attr_unique_id = self.device.device_url

        if self.is_sub_device:
            self._attr_name = self.device.label

        self._attr_device_info = self.generate_device_info()

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        return self.device.available and super().available

    @property
    def is_sub_device(self) -> bool:
        """Return True if device is a sub device."""
        return "#" in self.device_url and not self.device_url.endswith("#1")

    @property
    def device(self) -> Device:
        """Return Overkiz device linked to this entity."""
        return self.coordinator.data[self.device_url]

    async def async_refresh_if_stale(self, max_age: float = 1.0) -> None:
        """Refresh coordinator data only if last refresh was older than max_age seconds."""
        now = time.monotonic()
        if now - self.coordinator.last_refresh_time > max_age:
            await self.coordinator.async_request_refresh()
            self.coordinator.last_refresh_time = now

    def generate_device_info(self) -> DeviceInfo:
        """Return device registry information for this entity."""
        if self.is_sub_device:
            return DeviceInfo(
                identifiers={(DOMAIN, self.executor.base_device_url)},
            )

        manufacturer = (
            self.executor.select_attribute(OverkizAttribute.CORE_MANUFACTURER)
            or self.executor.select_state(OverkizState.CORE_MANUFACTURER_NAME)
            or self.coordinator.client.server.manufacturer
        )

        model = (
            self.executor.select_state(
                OverkizState.CORE_MODEL,
                OverkizState.CORE_PRODUCT_MODEL_NAME,
                OverkizState.IO_MODEL,
            )
            or self.device.ui_class.value
        )

        suggested_area = (
            self.coordinator.areas[self.device.place_oid]
            if self.coordinator.areas and self.device.place_oid
            else None
        )

        return DeviceInfo(
            identifiers={(DOMAIN, self.executor.base_device_url)},
            name=self.device.label,
            manufacturer=str(manufacturer),
            model=str(model),
            sw_version=cast(
                str,
                self.executor.select_attribute(OverkizAttribute.CORE_FIRMWARE_REVISION),
            ),
            model_id=self.device.widget,
            hw_version=self.device.controllable_name,
            suggested_area=suggested_area,
            via_device=(DOMAIN, self.executor.get_gateway_id()),
            configuration_url=self.coordinator.client.server.configuration_url,
        )
