"""Shared entity base for the Snapmaker U1."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER, MODEL
from .coordinator import U1Coordinator


class U1Entity(CoordinatorEntity[U1Coordinator]):
    """One entity of one U1.

    Every U1 entity is on the same device and goes unavailable together, which
    is honest: Moonraker throws away its cached printer status when Klippy
    disconnects (moonraker/components/klippy_connection.py:896-899), so no
    entity has fresh data after that.
    """

    _attr_has_entity_name = True

    def __init__(self, coordinator: U1Coordinator, key: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.device_id}_{key}"

    @property
    def device_info(self) -> DeviceInfo:
        client = self.coordinator.client
        return DeviceInfo(
            identifiers={(DOMAIN, self.coordinator.device_id)},
            manufacturer=MANUFACTURER,
            model=MODEL,
            name=self.coordinator.state.hostname or MODEL,
            sw_version=self.coordinator.state.software_version,
            configuration_url=client.base_url,
        )

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.state.available
