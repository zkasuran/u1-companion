"""Entangle detection sensitivity select for the Snapmaker U1."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    ENTANGLE_SENSITIVITIES,
    OBJ_PRINT_TASK_CONFIG,
    PREF_ENTANGLE_SEN,
)
from .coordinator import U1Coordinator
from .entity import U1Entity

DESCRIPTION = SelectEntityDescription(
    key=PREF_ENTANGLE_SEN,
    translation_key="filament_entangle_sen",
    options=list(ENTANGLE_SENSITIVITIES),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the U1 selects."""
    coordinator: U1Coordinator = hass.data[DOMAIN][entry.entry_id]
    if not coordinator.state.has_object(OBJ_PRINT_TASK_CONFIG):
        return
    async_add_entities([U1EntangleSensitivity(coordinator)])


class U1EntangleSensitivity(U1Entity, SelectEntity):
    """filament_entangle_sen, one of low, medium or high.

    The firmware rejects anything else (klippy/extras/print_task_config.py:166).
    """

    entity_description = DESCRIPTION

    def __init__(self, coordinator: U1Coordinator) -> None:
        super().__init__(coordinator, DESCRIPTION.key)

    @property
    def current_option(self) -> str | None:
        value = self.coordinator.state.entangle_sensitivity
        return value if value in ENTANGLE_SENSITIVITIES else None

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_set_preferences(**{PREF_ENTANGLE_SEN: option})
