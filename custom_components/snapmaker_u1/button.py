"""Buttons for the Snapmaker U1.

Pause, resume and cancel go over HTTP to /printer/print/*
(moonraker/components/klippy_apis.py:59-67). Emergency stop can only go over
the websocket: this fork removes the HTTP transport from
printer.emergency_stop (:77-82).
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import U1Coordinator
from .entity import U1Entity


@dataclass(frozen=True, kw_only=True)
class U1ButtonDescription(ButtonEntityDescription):
    """A button and the coordinator call it makes."""

    press_fn: Callable[[U1Coordinator], Coroutine[Any, Any, None]]


BUTTONS: tuple[U1ButtonDescription, ...] = (
    U1ButtonDescription(
        key="pause",
        translation_key="pause",
        icon="mdi:pause",
        press_fn=lambda coordinator: coordinator.async_pause(),
    ),
    U1ButtonDescription(
        key="resume",
        translation_key="resume",
        icon="mdi:play",
        press_fn=lambda coordinator: coordinator.async_resume(),
    ),
    U1ButtonDescription(
        key="cancel",
        translation_key="cancel",
        icon="mdi:stop",
        press_fn=lambda coordinator: coordinator.async_cancel(),
    ),
    U1ButtonDescription(
        key="emergency_stop",
        translation_key="emergency_stop",
        icon="mdi:alert-octagon",
        press_fn=lambda coordinator: coordinator.async_emergency_stop(),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the U1 buttons."""
    coordinator: U1Coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(U1Button(coordinator, description) for description in BUTTONS)


class U1Button(U1Entity, ButtonEntity):
    """One command sent to the printer."""

    entity_description: U1ButtonDescription

    def __init__(self, coordinator: U1Coordinator, description: U1ButtonDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    async def async_press(self) -> None:
        await self.entity_description.press_fn(self.coordinator)
