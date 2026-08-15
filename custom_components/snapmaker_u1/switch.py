"""Switches for the Snapmaker U1 print preferences.

Each switch maps to one key of print_task_config that
print_task_config/set_print_preferences accepts
(klippy/extras/print_task_config.py:150-174). The endpoint takes ints for the
boolean keys. It answers 200 with a state member rather than an HTTP error,
which the client checks.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    OBJ_PRINT_TASK_CONFIG,
    PREF_AUTO_REPLENISH,
    PREF_END_LED_OFF,
    PREF_ENTANGLE_DETECT,
    PREF_REPLENISH_IGNORE_COLOR,
)
from .coordinator import U1Coordinator
from .entity import U1Entity


@dataclass(frozen=True, kw_only=True)
class U1SwitchDescription(SwitchEntityDescription):
    """A preference switch and the print_task_config key behind it."""

    preference: str


SWITCHES: tuple[U1SwitchDescription, ...] = (
    U1SwitchDescription(
        key="auto_replenish_filament",
        translation_key="auto_replenish_filament",
        preference=PREF_AUTO_REPLENISH,
    ),
    U1SwitchDescription(
        key="filament_entangle_detect",
        translation_key="filament_entangle_detect",
        preference=PREF_ENTANGLE_DETECT,
    ),
    U1SwitchDescription(
        key="replenish_ignore_color",
        translation_key="replenish_ignore_color",
        preference=PREF_REPLENISH_IGNORE_COLOR,
    ),
    U1SwitchDescription(
        key="end_led_turn_off",
        translation_key="end_led_turn_off",
        preference=PREF_END_LED_OFF,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the U1 preference switches."""
    coordinator: U1Coordinator = hass.data[DOMAIN][entry.entry_id]
    if not coordinator.state.has_object(OBJ_PRINT_TASK_CONFIG):
        return
    async_add_entities(U1PreferenceSwitch(coordinator, description) for description in SWITCHES)


class U1PreferenceSwitch(U1Entity, SwitchEntity):
    """One print preference."""

    entity_description: U1SwitchDescription

    def __init__(self, coordinator: U1Coordinator, description: U1SwitchDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        value = self.coordinator.state.preference(self.entity_description.preference)
        return bool(value) if value is not None else None

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_write(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_write(False)

    async def _async_write(self, value: bool) -> None:
        await self.coordinator.async_set_preferences(**{self.entity_description.preference: value})
