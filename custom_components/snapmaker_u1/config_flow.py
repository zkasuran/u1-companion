"""Config flow for the Snapmaker U1.

Validation is a real conversation with the printer: read the klippy info block,
then read the object list and look for print_task_config. That object is what
makes a U1 a U1 as far as this integration is concerned. It is the multicolor
state, the four slots and the RFID identity. It is enabled on the shipped
machine (klippy/lava/printer.cfg:7). A plain Klipper printer running Moonraker
answers everything else in this flow and fails that one check, which is the
point.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    MoonrakerAuthError,
    MoonrakerClient,
    MoonrakerError,
)
from .const import (
    CONF_API_KEY,
    CONF_USE_SSL,
    DEFAULT_PORT,
    DOMAIN,
    MODEL,
    OBJ_PRINT_TASK_CONFIG,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
        vol.Optional(CONF_API_KEY): str,
        vol.Optional(CONF_USE_SSL, default=False): bool,
    }
)


class NotAU1(Exception):
    """The printer answered, but it is not running the U1 firmware."""


class KlippyNotReady(Exception):
    """Moonraker is up but Klippy is not ready, so the objects cannot be read."""


async def validate_input(hass, data: dict[str, Any]) -> dict[str, Any]:
    """Talk to the printer and return the title and unique id."""
    client = MoonrakerClient(
        async_get_clientsession(hass),
        data[CONF_HOST],
        data.get(CONF_PORT, DEFAULT_PORT),
        data.get(CONF_API_KEY),
        data.get(CONF_USE_SSL, False),
    )
    info = await client.printer_info()
    state = info.get("state")
    if state != "ready":
        raise KlippyNotReady(str(state))
    objects = await client.objects_list()
    if OBJ_PRINT_TASK_CONFIG not in objects:
        raise NotAU1(f"print_task_config is not one of {len(objects)} objects")
    hostname = info.get("hostname")
    unique_id = hostname or f"{client.host}:{client.port}"
    return {
        "title": hostname or f"{MODEL} ({client.host})",
        "unique_id": unique_id,
    }


class SnapmakerU1ConfigFlow(ConfigFlow, domain=DOMAIN):
    """Ask for a host and a port, then prove it is a U1."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                info = await validate_input(self.hass, user_input)
            except MoonrakerAuthError:
                errors["base"] = "invalid_auth"
            except NotAU1:
                errors["base"] = "not_u1"
            except KlippyNotReady:
                errors["base"] = "klippy_not_ready"
            except MoonrakerError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error validating the printer")
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(info["unique_id"])
                self._abort_if_unique_id_configured(updates=dict(user_input))
                return self.async_create_entry(title=info["title"], data=user_input)

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(STEP_USER_SCHEMA, user_input),
            errors=errors,
        )
