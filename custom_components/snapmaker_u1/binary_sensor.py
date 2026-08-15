"""Binary sensors for the Snapmaker U1."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    OBJ_EXCEPTION_MANAGER,
    OBJ_PAUSE_RESUME,
    PHYSICAL_EXTRUDER_NUM,
)
from .coordinator import U1Coordinator
from .entity import U1Entity
from .parsing import U1State


@dataclass(frozen=True, kw_only=True)
class U1BinarySensorDescription(BinarySensorEntityDescription):
    """A binary sensor and the rule that reads it out of the printer state."""

    value_fn: Callable[[U1State], bool | None]
    attrs_fn: Callable[[U1State], dict[str, Any]] | None = None
    exists_fn: Callable[[U1State], bool] | None = None
    placeholders: dict[str, str] | None = None


PRINTER_BINARY_SENSORS: tuple[U1BinarySensorDescription, ...] = (
    U1BinarySensorDescription(
        key="paused",
        translation_key="paused",
        value_fn=lambda state: state.is_paused,
        exists_fn=lambda state: state.has_object(OBJ_PAUSE_RESUME),
    ),
    U1BinarySensorDescription(
        key="exception",
        translation_key="exception",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda state: len(state.exceptions) > 0,
        attrs_fn=lambda state: {"exceptions": state.exceptions},
        exists_fn=lambda state: state.has_object(OBJ_EXCEPTION_MANAGER),
    ),
)


def slot_binary_sensors(slot: int) -> tuple[U1BinarySensorDescription, ...]:
    """The binary sensors for one physical slot."""
    placeholders = {"slot": str(slot)}
    return (
        U1BinarySensorDescription(
            key=f"slot{slot}_filament_present",
            translation_key="slot_filament_present",
            placeholders=placeholders,
            icon="mdi:printer-3d-nozzle",
            # filament_exist is recomputed on every get_status from the slot's
            # motion sensor and its feeder
            # (klippy/extras/print_task_config.py:474-498), so it is live rather
            # than a stored flag. There is no filament device class in Home
            # Assistant and "problem" would be a lie, so none is set.
            value_fn=lambda state: state.slot(slot).present,
        ),
        U1BinarySensorDescription(
            key=f"slot{slot}_in_use",
            translation_key="slot_in_use",
            placeholders=placeholders,
            entity_category=EntityCategory.DIAGNOSTIC,
            value_fn=lambda state: state.slot(slot).in_use,
        ),
        U1BinarySensorDescription(
            key=f"slot{slot}_official_tag",
            translation_key="slot_official_tag",
            placeholders=placeholders,
            entity_category=EntityCategory.DIAGNOSTIC,
            # filament_official is set from the RFID tag's OFFICIAL field
            # (klippy/extras/print_task_config.py:343-349).
            value_fn=lambda state: state.slot(slot).official,
            attrs_fn=lambda state: {
                "sku": state.slot(slot).sku,
                "user_editable": state.slot(slot).user_editable,
            },
        ),
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the U1 binary sensors."""
    coordinator: U1Coordinator = hass.data[DOMAIN][entry.entry_id]
    descriptions: list[U1BinarySensorDescription] = list(PRINTER_BINARY_SENSORS)
    for index in range(PHYSICAL_EXTRUDER_NUM):
        descriptions.extend(slot_binary_sensors(index))
    async_add_entities(
        U1BinarySensor(coordinator, description)
        for description in descriptions
        if description.exists_fn is None or description.exists_fn(coordinator.state)
    )


class U1BinarySensor(U1Entity, BinarySensorEntity):
    """One flag read out of the printer state."""

    entity_description: U1BinarySensorDescription

    def __init__(self, coordinator: U1Coordinator, description: U1BinarySensorDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description
        if description.placeholders:
            self._attr_translation_placeholders = dict(description.placeholders)

    @property
    def is_on(self) -> bool | None:
        return self.entity_description.value_fn(self.coordinator.state)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(self.coordinator.state)
