"""Update coordinator for the Snapmaker U1.

The printer pushes. One websocket subscription carries every status change at up
to 4 Hz (klippy/webhooks.py:478), so the coordinator holds no poll interval
while the socket is up. If the socket drops it falls back to HTTP polling and
keeps trying to get the push channel back.

Order at startup and after every Klippy restart: read the klippy info
block, read the object list, take one full snapshot over HTTP, then subscribe.
The HTTP query is not redundant. This fork can answer a subscribe entirely from
Moonraker's own cache when the request is already covered
(moonraker/components/klippy_connection.py:711-763), so a subscribe on its own
is not a guaranteed full snapshot.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    MoonrakerClient,
    MoonrakerError,
    MoonrakerWebsocket,
    parse_status_notification,
)
from .const import (
    DOMAIN,
    FALLBACK_POLL_INTERVAL,
    LOGICAL_EXTRUDER_NUM,
    PHYSICAL_EXTRUDER_NUM,
    RECONNECT_MAX,
    RECONNECT_MIN,
    WANTED_OBJECTS,
)
from .parsing import U1State

_LOGGER = logging.getLogger(__name__)


class U1Coordinator(DataUpdateCoordinator[U1State]):
    """Keeps one U1State fed from Moonraker."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: MoonrakerClient) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {client.host}",
            update_interval=None,
        )
        self.client = client
        self.state = U1State()
        self._websocket: MoonrakerWebsocket | None = None
        self._websocket_task: asyncio.Task[None] | None = None
        self._metadata_task: asyncio.Task[None] | None = None
        self._closing = False

    @property
    def device_id(self) -> str:
        """A stable id for this printer.

        The klippy hostname is the printer's own name for itself. Host and port
        are the fallback when a fork answers info without one.
        """
        return self.state.hostname or f"{self.client.host}:{self.client.port}"

    def selected_objects(self) -> dict[str, list[str] | None]:
        """The objects to read, narrowed to the ones this printer has.

        Klipper answers {} for an object it does not know
        (klippy/webhooks.py:517-519) rather than failing the whole query, but
        asking only for what exists keeps the payload and the log clean.
        """
        available = set(self.state.objects)
        if not available:
            return dict(WANTED_OBJECTS)
        return {name: fields for name, fields in WANTED_OBJECTS.items() if name in available}

    async def async_start(self) -> None:
        """Read the printer once over HTTP, then start the push channel."""
        await self.async_config_entry_first_refresh()
        self._websocket_task = self.config_entry.async_create_background_task(
            self.hass, self._websocket_loop(), f"{DOMAIN} websocket"
        )

    async def async_stop(self) -> None:
        """Close the push channel."""
        self._closing = True
        if self._websocket is not None:
            await self._websocket.close()
        for task in (self._websocket_task, self._metadata_task):
            if task is not None and not task.done():
                task.cancel()
        self._websocket_task = None
        self._metadata_task = None

    async def _async_update_data(self) -> U1State:
        """Read everything over HTTP.

        This runs once at setup and then only while the websocket is down.
        """
        try:
            await self._async_read_static()
            eventtime, status = await self.client.query_objects(self.selected_objects())
            self.state.apply_snapshot(status, eventtime)
            await self._async_update_job_metadata()
        except MoonrakerError as err:
            raise UpdateFailed(str(err)) from err
        return self.state

    async def _async_read_static(self) -> None:
        """Read the info blocks and the object list."""
        self.state.printer_info = await self.client.printer_info()
        self.state.server_info = await self.client.server_info()
        self.state.set_objects(await self.client.objects_list())

    async def _async_update_job_metadata(self) -> None:
        """Fetch the sliced file's metadata when the loaded file changed.

        This is the only source of per colour filament usage. The printer keeps
        per logical extruder grams in print_task_config_2 but never returns it
        from a get_status (klippy/extras/print_task_config.py:503), so the
        figures here are the slicer's estimates for the loaded file.
        """
        filename = self.state.filename
        if filename is None:
            if self.state.job_metadata_filename is not None:
                self.state.set_job_metadata(None, None)
            return
        if filename == self.state.job_metadata_filename:
            return
        metadata = await self.client.file_metadata(filename)
        self.state.set_job_metadata(filename, metadata)

    async def _async_metadata_refresh(self) -> None:
        try:
            await self._async_update_job_metadata()
        except MoonrakerError as err:
            _LOGGER.debug("Metadata refresh failed: %s", err)
            return
        self.async_set_updated_data(self.state)

    @callback
    def _schedule_metadata_refresh(self) -> None:
        """Fetch metadata for a newly loaded file, off the websocket task."""
        if self.state.filename == self.state.job_metadata_filename:
            return
        if self._metadata_task is not None and not self._metadata_task.done():
            return
        if self.config_entry is None:
            return
        self._metadata_task = self.config_entry.async_create_background_task(
            self.hass, self._async_metadata_refresh(), f"{DOMAIN} metadata"
        )

    @callback
    def _set_polling(self, enabled: bool) -> None:
        """Turn the HTTP fallback poll on or off.

        async_set_updated_data reschedules from the current interval, so the
        change takes effect on the next push or poll.
        """
        wanted = timedelta(seconds=FALLBACK_POLL_INTERVAL) if enabled else None
        if self.update_interval != wanted:
            self.update_interval = wanted

    async def _websocket_loop(self) -> None:
        """Keep a subscription open, reconnecting with backoff."""
        delay = RECONNECT_MIN
        first_pass = True
        while not self._closing:
            websocket = MoonrakerWebsocket(self.client, self._handle_notification)
            listener: asyncio.Task[None] | None = None
            try:
                await websocket.connect()
                self._websocket = websocket
                listener = asyncio.create_task(websocket.listen())
                if not first_pass:
                    # A reconnect means Klippy or Moonraker restarted or the
                    # link broke. Re-read the object list and take a fresh
                    # snapshot before subscribing again.
                    await self.async_refresh()
                status, eventtime = await websocket.subscribe(self.selected_objects())
                if status:
                    self.state.apply_update(status, eventtime)
                self.state.push_active = True
                self._set_polling(False)
                self.async_set_updated_data(self.state)
                delay = RECONNECT_MIN
                first_pass = False
                await listener
            except asyncio.CancelledError:
                if listener is not None:
                    listener.cancel()
                await websocket.close()
                raise
            except MoonrakerError as err:
                _LOGGER.debug("Websocket to %s dropped: %s", self.client.host, err)
            except Exception:
                _LOGGER.exception("Unexpected websocket failure")
            finally:
                self._websocket = None
                self.state.push_active = False
                await websocket.close()
                if listener is not None and not listener.done():
                    listener.cancel()
            first_pass = False
            if self._closing:
                return
            # No push channel, so poll over HTTP until it is back.
            self._set_polling(True)
            self.async_set_updated_data(self.state)
            await asyncio.sleep(delay)
            delay = min(delay * 2, RECONNECT_MAX)

    @callback
    def _handle_notification(self, method: str, params: Any) -> None:
        """Handle one Moonraker notification.

        Names come from the last colon segment of the server event
        (moonraker/components/websockets.py:66-81,
        moonraker/server.py:131-136).
        """
        if method == "notify_status_update":
            status, eventtime = parse_status_notification(params)
            if not status:
                return
            self.state.apply_update(status, eventtime)
            self._schedule_metadata_refresh()
            self.async_set_updated_data(self.state)
        elif method == "notify_klippy_ready":
            self._schedule_resubscribe()
        elif method in ("notify_klippy_shutdown", "notify_klippy_disconnected"):
            # Moonraker drops every subscription and clears its cache on a
            # Klippy disconnect (klippy_connection.py:896-899), so what we hold
            # is stale from here on.
            self.state.klippy_connected = False
            self.async_set_updated_data(self.state)

    @callback
    def _schedule_resubscribe(self) -> None:
        """Drop the socket so the loop rebuilds the whole subscription.

        Klippy came back. Moonraker threw away every subscription and its
        cache when it went (klippy_connection.py:896-899), so the object list,
        the snapshot and the subscription all have to be made again.
        """
        websocket = self._websocket
        if websocket is None or self.config_entry is None:
            return
        self.config_entry.async_create_background_task(
            self.hass, websocket.close(), f"{DOMAIN} resubscribe"
        )

    # Writes ----------------------------------------------------------------

    async def _run(self, coro: Coroutine[Any, Any, Any]) -> None:
        """Run one write, turning a refusal into a Home Assistant error."""
        try:
            await coro
        except MoonrakerError as err:
            raise HomeAssistantError(str(err)) from err
        if not self.state.push_active:
            # Without the push channel nothing would report the change.
            await self.async_request_refresh()

    async def async_send_gcode(self, script: str) -> None:
        await self._run(self.client.run_gcode(script))

    async def async_pause(self) -> None:
        await self._run(self.client.pause_print())

    async def async_resume(self) -> None:
        await self._run(self.client.resume_print())

    async def async_cancel(self) -> None:
        await self._run(self.client.cancel_print())

    async def async_set_preferences(self, **values: Any) -> None:
        await self._run(self.client.set_print_preferences(**values))

    async def async_emergency_stop(self) -> None:
        """Stop the printer over the websocket.

        This fork excludes the HTTP transport from printer.emergency_stop
        (moonraker/components/klippy_apis.py:77-82), so it can only be sent on
        the websocket.
        """
        websocket = self._websocket
        if websocket is None or not websocket.connected:
            raise HomeAssistantError(
                "Emergency stop needs the websocket and it is not connected. "
                "Moonraker does not accept this call over HTTP."
            )
        try:
            await websocket.emergency_stop()
        except MoonrakerError as err:
            raise HomeAssistantError(str(err)) from err

    async def async_set_color_map(self, logical: int, head: int) -> None:
        """Map a logical colour onto a physical head.

        The firmware refuses this while printing or paused
        (klippy/extras/print_task_config.py:511-519), which is checked here so
        the user gets a clear reason instead of a raw 400.
        """
        if not 0 <= logical < LOGICAL_EXTRUDER_NUM:
            raise HomeAssistantError(f"logical must be 0 to {LOGICAL_EXTRUDER_NUM - 1}")
        if not 0 <= head < PHYSICAL_EXTRUDER_NUM:
            raise HomeAssistantError(f"head must be 0 to {PHYSICAL_EXTRUDER_NUM - 1}")
        if self.state.writes_blocked:
            raise HomeAssistantError(
                f"The printer will not remap colours while it is {self.state.print_state}"
            )
        await self.async_send_gcode(
            f"SET_PRINT_EXTRUDER_MAP CONFIG_EXTRUDER={logical} MAP_EXTRUDER={head}"
        )

    async def async_set_filament(
        self,
        slot: int,
        vendor: str,
        filament_type: str,
        sub_type: str,
        color: str | None = None,
        force: bool = False,
    ) -> None:
        """Write a filament identity into one slot by hand.

        The firmware wants vendor, type and sub type together
        (klippy/extras/print_task_config.py:590-592) and refuses a slot holding
        an official spool unless FORCE=1 (:577-578). Writing a slot also clears
        its official flag and its SKU (:665-666), because a hand typed identity
        is no longer the one from the tag.
        """
        if not 0 <= slot < PHYSICAL_EXTRUDER_NUM:
            raise HomeAssistantError(f"slot must be 0 to {PHYSICAL_EXTRUDER_NUM - 1}")
        parts = [
            "SET_PRINT_FILAMENT_CONFIG",
            f"CONFIG_EXTRUDER={slot}",
            f"VENDOR={_quote(vendor)}",
            f"FILAMENT_TYPE={_quote(filament_type)}",
            f"FILAMENT_SUBTYPE={_quote(sub_type)}",
        ]
        if color:
            parts.append(f"FILAMENT_COLOR_RGBA={_rgba_argument(color)}")
        if force:
            parts.append("FORCE=1")
        await self.async_send_gcode(" ".join(parts))


def _quote(value: str) -> str:
    """Quote a G-code parameter value.

    Klipper's parameter splitter takes quoted values so a vendor name can hold
    a space (klippy/gcode.py:284-311), but the command line is cut at the first
    #, * or ; before that (:279-283), so those characters cannot be sent.
    """
    text = str(value).strip()
    if not text:
        raise HomeAssistantError("Empty value")
    for bad in ('"', "'", "#", "*", ";", "\n", "\r"):
        if bad in text:
            raise HomeAssistantError(f"{bad!r} cannot be sent in a G-code parameter")
    return f'"{text}"'


def _rgba_argument(color: str) -> str:
    """Normalise a colour to what FILAMENT_COLOR_RGBA accepts.

    The command takes 6 or 8 hex characters and pads a 6 character value with
    an opaque alpha itself (klippy/extras/print_task_config.py:636-641).
    """
    text = str(color).strip().lstrip("#").upper()
    if len(text) not in (6, 8) or any(char not in "0123456789ABCDEF" for char in text):
        raise HomeAssistantError(f"{color!r} is not RRGGBB or RRGGBBAA hex")
    return text
