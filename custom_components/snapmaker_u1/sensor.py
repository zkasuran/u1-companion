"""Sensors for the Snapmaker U1.

Every sensor below names the printer field it comes from. Nothing is derived
from anything the printer does not publish, with one labelled exception: per
colour filament usage, which the printer keeps internally but never returns
(klippy/extras/print_task_config.py:503), so it is read from the sliced file's
own metadata and marked as an estimate.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfLength,
    UnitOfMass,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    OBJ_FILAMENT_DETECT,
    OBJ_MACHINE_STATE,
    PHYSICAL_EXTRUDER_NUM,
    PRINT_STATES,
)
from .coordinator import U1Coordinator
from .entity import U1Entity
from .parsing import U1State


@dataclass(frozen=True, kw_only=True)
class U1SensorDescription(SensorEntityDescription):
    """A sensor and the rule that reads it out of the printer state."""

    value_fn: Callable[[U1State], Any]
    attrs_fn: Callable[[U1State], dict[str, Any]] | None = None
    exists_fn: Callable[[U1State], bool] | None = None
    placeholders: dict[str, str] | None = None


PRINTER_SENSORS: tuple[U1SensorDescription, ...] = (
    U1SensorDescription(
        key="print_state",
        translation_key="print_state",
        device_class=SensorDeviceClass.ENUM,
        options=list(PRINT_STATES),
        value_fn=lambda state: state.print_state,
        attrs_fn=lambda state: {
            "message": state.print_message,
            "exception": state.print_exception,
        },
    ),
    U1SensorDescription(
        key="progress",
        translation_key="progress",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda state: state.progress_percent,
    ),
    U1SensorDescription(
        key="current_file",
        translation_key="current_file",
        value_fn=lambda state: state.filename,
        attrs_fn=lambda state: {
            "slicer_estimate_grams_per_color": state.job_color_grams,
            "metadata_filename": state.job_metadata_filename,
        },
    ),
    U1SensorDescription(
        key="layer",
        translation_key="layer",
        value_fn=lambda state: state.current_layer,
        attrs_fn=lambda state: {"total": state.total_layer},
    ),
    U1SensorDescription(
        key="print_duration",
        translation_key="print_duration",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        suggested_display_precision=0,
        value_fn=lambda state: state.print_duration,
    ),
    U1SensorDescription(
        key="total_duration",
        translation_key="total_duration",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        suggested_display_precision=0,
        value_fn=lambda state: state.total_duration,
    ),
    U1SensorDescription(
        key="filament_used",
        translation_key="filament_used",
        native_unit_of_measurement=UnitOfLength.MILLIMETERS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda state: state.filament_used_mm,
    ),
    U1SensorDescription(
        key="active_tool",
        translation_key="active_tool",
        value_fn=lambda state: state.active_tool,
        attrs_fn=lambda state: {
            "extruder_object": state.active_extruder_object,
            "color_map": state.color_map,
        },
    ),
    U1SensorDescription(
        key="bed_temperature",
        translation_key="bed_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda state: state.bed_temperature,
        attrs_fn=lambda state: {"target": state.bed_target},
    ),
    U1SensorDescription(
        key="klippy_state",
        translation_key="klippy_state",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda state: state.klippy_state,
        attrs_fn=lambda state: {"message": state.klippy_message},
    ),
    U1SensorDescription(
        key="machine_state",
        translation_key="machine_state",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda state: state.machine_state,
        attrs_fn=lambda state: {
            "action_code": state.action_code,
            "action": state.action_name,
        },
        exists_fn=lambda state: state.has_object(OBJ_MACHINE_STATE),
    ),
)


def _slot_attributes(state: U1State, slot: int) -> dict[str, Any]:
    """Identity and colour of one slot, from the print_task_config arrays."""
    entry = state.slot(slot)
    attributes: dict[str, Any] = {
        "vendor": entry.vendor,
        "sub_type": entry.sub_type,
        "official": entry.official,
        "sku": entry.sku,
        "soft": entry.soft,
        "user_editable": entry.user_editable,
        "present": entry.present,
        "in_use": entry.in_use,
        "assigned_colors": entry.assigned_colors,
    }
    attributes.update(entry.color_attributes())
    return attributes


def _tag_attributes(state: U1State, slot: int) -> dict[str, Any]:
    """Everything on the RFID tag that is not its own entity."""
    tag = state.tag(slot)
    if tag is None:
        return {}
    return {
        "manufacturer": tag.manufacturer,
        "card_uid": tag.card_uid,
        "protocol_version": tag.protocol_version,
        "rsa_key_version": tag.rsa_key_version,
        "tray": tag.tray,
        "diameter": tag.diameter,
        "length": tag.length,
        "sku": tag.sku,
        "official": tag.official,
        "vendor": tag.vendor,
        "filament_type": tag.main_type,
        "sub_type": tag.sub_type,
        "color": tag.color,
        "colors": tag.colors,
        "multi_mode": tag.multi_mode,
    }


def _slot_color_grams(state: U1State, slot: int) -> dict[int, float]:
    """Slicer estimated grams for the colours mapped onto one slot."""
    table = state.color_map
    return {
        logical: grams
        for logical, grams in state.job_color_grams.items()
        if logical < len(table) and table[logical] == slot
    }


def _has_rfid(state: U1State) -> bool:
    """True when the printer runs the RFID reader object."""
    return state.has_object(OBJ_FILAMENT_DETECT)


def slot_sensors(slot: int) -> tuple[U1SensorDescription, ...]:
    """The sensors for one physical slot."""
    placeholders = {"slot": str(slot)}
    has_rfid = _has_rfid
    return (
        U1SensorDescription(
            key=f"slot{slot}_filament",
            translation_key="slot_filament",
            placeholders=placeholders,
            icon="mdi:printer-3d-nozzle",
            value_fn=lambda state: state.slot(slot).filament_type,
            attrs_fn=lambda state: _slot_attributes(state, slot),
        ),
        U1SensorDescription(
            key=f"slot{slot}_vendor",
            translation_key="slot_vendor",
            placeholders=placeholders,
            value_fn=lambda state: state.slot(slot).vendor,
            attrs_fn=lambda state: {
                "sku": state.slot(slot).sku,
                "official": state.slot(slot).official,
            },
        ),
        U1SensorDescription(
            key=f"slot{slot}_color",
            translation_key="slot_color",
            placeholders=placeholders,
            icon="mdi:palette",
            value_fn=lambda state: state.slot(slot).color,
            attrs_fn=lambda state: state.slot(slot).color_attributes(),
        ),
        U1SensorDescription(
            key=f"slot{slot}_assigned_colors",
            translation_key="slot_assigned_colors",
            placeholders=placeholders,
            entity_category=EntityCategory.DIAGNOSTIC,
            value_fn=lambda state: len(state.slot(slot).assigned_colors),
            attrs_fn=lambda state: {
                "colors": state.slot(slot).assigned_colors,
                "color_map": state.color_map,
            },
        ),
        U1SensorDescription(
            key=f"slot{slot}_job_filament",
            translation_key="slot_job_filament",
            placeholders=placeholders,
            device_class=SensorDeviceClass.WEIGHT,
            native_unit_of_measurement=UnitOfMass.GRAMS,
            suggested_display_precision=1,
            value_fn=lambda state: state.head_job_grams(slot),
            attrs_fn=lambda state: {
                "source": "slicer_estimate",
                "filename": state.job_metadata_filename,
                "grams_per_color": _slot_color_grams(state, slot),
            },
        ),
        U1SensorDescription(
            key=f"slot{slot}_spool_weight",
            translation_key="slot_spool_weight",
            placeholders=placeholders,
            device_class=SensorDeviceClass.WEIGHT,
            native_unit_of_measurement=UnitOfMass.GRAMS,
            value_fn=lambda state: getattr(state.tag(slot), "weight_g", None),
            exists_fn=has_rfid,
        ),
        U1SensorDescription(
            key=f"slot{slot}_drying_temperature",
            translation_key="slot_drying_temperature",
            placeholders=placeholders,
            device_class=SensorDeviceClass.TEMPERATURE,
            native_unit_of_measurement=UnitOfTemperature.CELSIUS,
            value_fn=lambda state: getattr(state.tag(slot), "drying_temp", None),
            attrs_fn=lambda state: {
                "drying_time_hours": getattr(state.tag(slot), "drying_time", None)
            },
            exists_fn=has_rfid,
        ),
        U1SensorDescription(
            key=f"slot{slot}_recommended_nozzle_temperature",
            translation_key="slot_recommended_nozzle_temperature",
            placeholders=placeholders,
            device_class=SensorDeviceClass.TEMPERATURE,
            native_unit_of_measurement=UnitOfTemperature.CELSIUS,
            value_fn=lambda state: getattr(state.tag(slot), "other_layer_temp", None),
            attrs_fn=lambda state: {
                "first_layer": getattr(state.tag(slot), "first_layer_temp", None),
                "hotend_min": getattr(state.tag(slot), "hotend_min_temp", None),
                "hotend_max": getattr(state.tag(slot), "hotend_max_temp", None),
                "bed_temp": getattr(state.tag(slot), "bed_temp", None),
                "bed_type": getattr(state.tag(slot), "bed_type", None),
            },
            exists_fn=has_rfid,
        ),
        U1SensorDescription(
            # The state is the tag's MF_DATE, 8 characters as YYYYMMDD
            # (klippy/extras/filament_protocol.py:34, :172-173). It is a plain
            # string rather than a date, because the struct default 19700101 is
            # the only format guarantee there is. The whole reading is in the
            # attributes, which is what this entity is really for.
            key=f"slot{slot}_tag",
            translation_key="slot_tag",
            placeholders=placeholders,
            entity_category=EntityCategory.DIAGNOSTIC,
            value_fn=lambda state: getattr(state.tag(slot), "mf_date", None),
            attrs_fn=lambda state: _tag_attributes(state, slot),
            exists_fn=has_rfid,
        ),
        U1SensorDescription(
            key=f"slot{slot}_scan_state",
            translation_key="slot_scan_state",
            placeholders=placeholders,
            entity_category=EntityCategory.DIAGNOSTIC,
            value_fn=lambda state: state.scan_state(slot),
            exists_fn=has_rfid,
        ),
    )


def head_sensors(head: int) -> tuple[U1SensorDescription, ...]:
    """The sensors for one physical toolhead."""
    placeholders = {"head": str(head)}
    return (
        U1SensorDescription(
            key=f"head{head}_nozzle_temperature",
            translation_key="head_nozzle_temperature",
            placeholders=placeholders,
            device_class=SensorDeviceClass.TEMPERATURE,
            native_unit_of_measurement=UnitOfTemperature.CELSIUS,
            state_class=SensorStateClass.MEASUREMENT,
            suggested_display_precision=1,
            value_fn=lambda state: state.head_temperature(head),
            attrs_fn=lambda state: {
                "target": state.head_target(head),
                "power": state.head(head).get("power"),
                "can_extrude": state.head(head).get("can_extrude"),
                "nozzle_diameter": state.head(head).get("nozzle_diameter"),
                "extruder_index": state.head(head).get("extruder_index"),
            },
            exists_fn=lambda state: state.has_object(state.head_object_name(head)),
        ),
        U1SensorDescription(
            key=f"head{head}_dock_state",
            translation_key="head_dock_state",
            placeholders=placeholders,
            entity_category=EntityCategory.DIAGNOSTIC,
            value_fn=lambda state: state.dock_state(head),
            # Only present when the head has a park detector configured
            # (klippy/kinematics/extruder.py:716-717).
            exists_fn=lambda state: state.dock_state(head) is not None,
        ),
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the U1 sensors."""
    coordinator: U1Coordinator = hass.data[DOMAIN][entry.entry_id]
    descriptions: list[U1SensorDescription] = list(PRINTER_SENSORS)
    for index in range(PHYSICAL_EXTRUDER_NUM):
        descriptions.extend(slot_sensors(index))
        descriptions.extend(head_sensors(index))
    async_add_entities(
        U1Sensor(coordinator, description)
        for description in descriptions
        if description.exists_fn is None or description.exists_fn(coordinator.state)
    )


class U1Sensor(U1Entity, SensorEntity):
    """One value read out of the printer state."""

    entity_description: U1SensorDescription

    def __init__(self, coordinator: U1Coordinator, description: U1SensorDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description
        if description.placeholders:
            self._attr_translation_placeholders = dict(description.placeholders)

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.coordinator.state)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(self.coordinator.state)
