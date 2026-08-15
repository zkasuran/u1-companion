"""The Snapmaker U1 integration.

Talks to the printer through Moonraker, on the standard endpoints. Nothing here
needs a patched Moonraker: the U1 objects print_task_config and filament_detect
come through objects/query and objects/subscribe like any other Klipper object,
because Moonraker keeps no allowlist of object names.
"""

from __future__ import annotations

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryError, HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import MoonrakerClient
from .const import (
    ATTR_COLOR,
    ATTR_FORCE,
    ATTR_HEAD,
    ATTR_LOGICAL,
    ATTR_SCRIPT,
    ATTR_SLOT,
    ATTR_SUB_TYPE,
    ATTR_TYPE,
    ATTR_VENDOR,
    CONF_API_KEY,
    CONF_USE_SSL,
    DEFAULT_PORT,
    DOMAIN,
    LOGICAL_EXTRUDER_NUM,
    PHYSICAL_EXTRUDER_NUM,
    SERVICE_SEND_GCODE,
    SERVICE_SET_COLOR_MAP,
    SERVICE_SET_FILAMENT,
)
from .coordinator import U1Coordinator

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]

CONF_ENTRY_ID = "config_entry_id"

BASE_SERVICE_SCHEMA = {vol.Optional(CONF_ENTRY_ID): cv.string}

SET_COLOR_MAP_SCHEMA = vol.Schema(
    {
        **BASE_SERVICE_SCHEMA,
        vol.Required(ATTR_LOGICAL): vol.All(
            vol.Coerce(int), vol.Range(min=0, max=LOGICAL_EXTRUDER_NUM - 1)
        ),
        vol.Required(ATTR_HEAD): vol.All(
            vol.Coerce(int), vol.Range(min=0, max=PHYSICAL_EXTRUDER_NUM - 1)
        ),
    }
)

SET_FILAMENT_SCHEMA = vol.Schema(
    {
        **BASE_SERVICE_SCHEMA,
        vol.Required(ATTR_SLOT): vol.All(
            vol.Coerce(int), vol.Range(min=0, max=PHYSICAL_EXTRUDER_NUM - 1)
        ),
        vol.Required(ATTR_VENDOR): cv.string,
        vol.Required(ATTR_TYPE): cv.string,
        vol.Required(ATTR_SUB_TYPE): cv.string,
        vol.Optional(ATTR_COLOR): cv.string,
        vol.Optional(ATTR_FORCE, default=False): cv.boolean,
    }
)

SEND_GCODE_SCHEMA = vol.Schema({**BASE_SERVICE_SCHEMA, vol.Required(ATTR_SCRIPT): cv.string})


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up one printer."""
    client = MoonrakerClient(
        async_get_clientsession(hass),
        entry.data[CONF_HOST],
        entry.data.get(CONF_PORT, DEFAULT_PORT),
        entry.data.get(CONF_API_KEY),
        entry.data.get(CONF_USE_SSL, False),
    )
    coordinator = U1Coordinator(hass, entry, client)
    await coordinator.async_start()
    if not coordinator.state.is_u1:
        # The flow checked this when the entry was added, so getting here means
        # the machine changed. Say so instead of creating a device with no slots.
        await coordinator.async_stop()
        raise ConfigEntryError(
            f"{client.base_url} does not publish print_task_config, so it is not a Snapmaker U1"
        )

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _async_register_services(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Tear one printer down."""
    coordinator: U1Coordinator | None = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if coordinator is not None:
        await coordinator.async_stop()
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unloaded


def _resolve(hass: HomeAssistant, call: ServiceCall) -> U1Coordinator:
    """Pick the printer a service call is for.

    With one printer set up the field can be left out. With more than one it is
    required, because guessing which machine to write to would be wrong.
    """
    coordinators: dict[str, U1Coordinator] = hass.data.get(DOMAIN, {})
    entry_id = call.data.get(CONF_ENTRY_ID)
    if entry_id is not None:
        coordinator = coordinators.get(entry_id)
        if coordinator is None:
            raise HomeAssistantError(f"No Snapmaker U1 set up with id {entry_id}")
        return coordinator
    if not coordinators:
        raise HomeAssistantError("No Snapmaker U1 is set up")
    if len(coordinators) > 1:
        raise HomeAssistantError(
            f"More than one Snapmaker U1 is set up, so {CONF_ENTRY_ID} is needed"
        )
    return next(iter(coordinators.values()))


def _async_register_services(hass: HomeAssistant) -> None:
    """Register the domain services, once."""
    if hass.services.has_service(DOMAIN, SERVICE_SEND_GCODE):
        return

    async def handle_set_color_map(call: ServiceCall) -> None:
        coordinator = _resolve(hass, call)
        await coordinator.async_set_color_map(call.data[ATTR_LOGICAL], call.data[ATTR_HEAD])

    async def handle_set_filament(call: ServiceCall) -> None:
        coordinator = _resolve(hass, call)
        await coordinator.async_set_filament(
            call.data[ATTR_SLOT],
            call.data[ATTR_VENDOR],
            call.data[ATTR_TYPE],
            call.data[ATTR_SUB_TYPE],
            call.data.get(ATTR_COLOR),
            call.data.get(ATTR_FORCE, False),
        )

    async def handle_send_gcode(call: ServiceCall) -> None:
        coordinator = _resolve(hass, call)
        await coordinator.async_send_gcode(call.data[ATTR_SCRIPT])

    hass.services.async_register(
        DOMAIN, SERVICE_SET_COLOR_MAP, handle_set_color_map, SET_COLOR_MAP_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SET_FILAMENT, handle_set_filament, SET_FILAMENT_SCHEMA
    )
    hass.services.async_register(DOMAIN, SERVICE_SEND_GCODE, handle_send_gcode, SEND_GCODE_SCHEMA)
