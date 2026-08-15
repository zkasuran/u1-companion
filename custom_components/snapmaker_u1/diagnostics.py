"""Diagnostics for the Snapmaker U1."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_API_KEY, DOMAIN
from .coordinator import U1Coordinator

TO_REDACT = {CONF_API_KEY}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return the raw printer state, which is what a bug report needs."""
    coordinator: U1Coordinator = hass.data[DOMAIN][entry.entry_id]
    state = coordinator.state
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "printer_info": state.printer_info,
        "server_info": state.server_info,
        "objects": list(state.objects),
        "push_active": state.push_active,
        "klippy_connected": state.klippy_connected,
        # Klipper's reactor monotonic clock, not a Unix timestamp
        # (klippy/webhooks.py:496).
        "eventtime": state.eventtime,
        "subscribed_objects": sorted(coordinator.selected_objects()),
        "status": state.status,
        "job_metadata_filename": state.job_metadata_filename,
        "job_metadata": state.job_metadata,
        "slots": [asdict(slot) for slot in state.slots()],
    }
