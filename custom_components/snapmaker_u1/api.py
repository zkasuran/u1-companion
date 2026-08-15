"""Moonraker client for the Snapmaker U1.

Two transports, because the fork needs both. HTTP covers reads, pause, resume,
cancel, gcode and the U1 preference write. The websocket carries the push
subscription and the calls Moonraker refuses over HTTP: printer.emergency_stop
and every printer.control.* method exclude the HTTP transport
(moonraker/components/klippy_apis.py:77-118).

Nothing in this module imports Home Assistant. It takes an aiohttp session so
the integration can hand it Home Assistant's shared one.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

import aiohttp

from .const import DEFAULT_PORT

_LOGGER = logging.getLogger(__name__)

REQUEST_TIMEOUT = 10
WS_CALL_TIMEOUT = 15


class MoonrakerError(Exception):
    """Base error for anything Moonraker refused or could not answer."""


class MoonrakerConnectionError(MoonrakerError):
    """Moonraker could not be reached."""


class MoonrakerAuthError(MoonrakerError):
    """Moonraker rejected the credentials."""


class MoonrakerCommandError(MoonrakerError):
    """The printer refused the command."""


def _error_message(payload: Any, fallback: str) -> str:
    """Pull the message out of a Moonraker error body."""
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str) and message:
                return message
        if isinstance(error, str) and error:
            return error
        message = payload.get("message")
        if isinstance(message, str) and message:
            return message
    return fallback


class MoonrakerClient:
    """HTTP client for one Moonraker instance."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        port: int = DEFAULT_PORT,
        api_key: str | None = None,
        use_ssl: bool = False,
    ) -> None:
        self._session = session
        self.host = host
        self.port = port
        self.api_key = api_key or None
        self.use_ssl = use_ssl

    @property
    def session(self) -> aiohttp.ClientSession:
        return self._session

    @property
    def base_url(self) -> str:
        scheme = "https" if self.use_ssl else "http"
        return f"{scheme}://{self.host}:{self.port}"

    @property
    def ws_url(self) -> str:
        scheme = "wss" if self.use_ssl else "ws"
        return f"{scheme}://{self.host}:{self.port}/websocket"

    @property
    def headers(self) -> dict[str, str]:
        # The shipped config trusts the LAN so a key is usually not needed
        # (moonraker/lava/moonraker.conf:10-19), but it is honoured when set.
        if self.api_key:
            return {"X-Api-Key": self.api_key}
        return {}

    async def _request(self, method: str, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{self.base_url}{path}"
        try:
            async with self._session.request(
                method,
                url,
                params=params,
                headers=self.headers,
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
            ) as response:
                body: Any = None
                try:
                    body = await response.json(content_type=None)
                except (ValueError, aiohttp.ClientError):
                    body = await response.text()
                if response.status in (401, 403):
                    raise MoonrakerAuthError(_error_message(body, "Moonraker rejected the API key"))
                if response.status == 400:
                    raise MoonrakerCommandError(
                        _error_message(body, "The printer refused the command")
                    )
                if response.status >= 400:
                    raise MoonrakerError(
                        _error_message(body, f"HTTP {response.status} from {path}")
                    )
        except TimeoutError as err:
            raise MoonrakerConnectionError(f"Timeout talking to {url}") from err
        except aiohttp.ClientError as err:
            raise MoonrakerConnectionError(f"Cannot reach {url}: {err}") from err
        if isinstance(body, dict) and "result" in body:
            # Every Moonraker HTTP answer is wrapped
            # (moonraker/components/application.py:706-707).
            return body["result"]
        return body

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return await self._request("GET", path, params)

    async def post(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return await self._request("POST", path, params)

    # Reads -----------------------------------------------------------------

    async def printer_info(self) -> dict[str, Any]:
        """GET /printer/info, the klippy info block."""
        result = await self.get("/printer/info")
        return result if isinstance(result, dict) else {}

    async def server_info(self) -> dict[str, Any]:
        """GET /server/info (moonraker/server.py:120-121)."""
        result = await self.get("/server/info")
        return result if isinstance(result, dict) else {}

    async def objects_list(self) -> list[str]:
        """GET /printer/objects/list."""
        result = await self.get("/printer/objects/list")
        if isinstance(result, dict):
            objects = result.get("objects")
            if isinstance(objects, list):
                return [str(name) for name in objects]
        return []

    async def query_objects(
        self, objects: dict[str, list[str] | None]
    ) -> tuple[float | None, dict[str, Any]]:
        """GET /printer/objects/query, returning (eventtime, status).

        This endpoint uses Moonraker's object parser, where each query string
        key is an object name and its value a comma separated field list, empty
        meaning every field (moonraker/components/application.py:633-644). A
        JSON body would not be read, so the request has to be a query string.
        """
        result = await self.get("/printer/objects/query", objects_query_params(objects))
        if not isinstance(result, dict):
            return None, {}
        status = result.get("status")
        eventtime = result.get("eventtime")
        return (
            eventtime if isinstance(eventtime, (int, float)) else None,
            status if isinstance(status, dict) else {},
        )

    async def file_metadata(self, filename: str) -> dict[str, Any] | None:
        """GET /server/files/metadata for a sliced file.

        Returns None when Moonraker has no metadata for the file, which is the
        normal answer for a file it never scanned.
        """
        try:
            result = await self.get("/server/files/metadata", {"filename": filename})
        except MoonrakerCommandError:
            return None
        except MoonrakerError as err:
            _LOGGER.debug("No metadata for %s: %s", filename, err)
            return None
        return result if isinstance(result, dict) else None

    # Writes ----------------------------------------------------------------

    async def run_gcode(self, script: str) -> None:
        """POST /printer/gcode/script.

        A command the firmware refuses comes back as HTTP 400 carrying the
        firmware's own message, which surfaces as MoonrakerCommandError.
        """
        await self.post("/printer/gcode/script", {"script": script})

    async def pause_print(self) -> None:
        """POST /printer/print/pause (moonraker/components/klippy_apis.py:59)."""
        await self.post("/printer/print/pause")

    async def resume_print(self) -> None:
        """POST /printer/print/resume."""
        await self.post("/printer/print/resume")

    async def cancel_print(self) -> None:
        """POST /printer/print/cancel."""
        await self.post("/printer/print/cancel")

    async def set_print_preferences(self, **values: Any) -> None:
        """POST /printer/print_task_config/set_print_preferences.

        The endpoint always answers 200 shaped, with either {"state":
        "success"} or {"state": "error", "message": ...}
        (klippy/extras/print_task_config.py:181, :185), so the body has to be
        read rather than the status code.
        """
        params: dict[str, Any] = {}
        for key, value in values.items():
            if value is None:
                continue
            params[key] = int(value) if isinstance(value, bool) else value
        if not params:
            return
        result = await self.post("/printer/print_task_config/set_print_preferences", params)
        if isinstance(result, dict) and result.get("state") == "error":
            raise MoonrakerCommandError(
                _error_message(result, "The printer refused the preference change")
            )


def objects_query_params(objects: dict[str, list[str] | None]) -> dict[str, str]:
    """Turn an object subscription map into objects/query query string args."""
    return {name: ",".join(fields) if fields else "" for name, fields in objects.items()}


def parse_status_notification(params: Any) -> tuple[dict[str, Any], float | None]:
    """Split a notify_status_update payload into (status, eventtime).

    params is a two element array, the status dict then the eventtime
    (moonraker/common.py:465-474). The status dict is partial: it carries only
    the fields that changed.
    """
    if not isinstance(params, (list, tuple)) or not params:
        return {}, None
    status = params[0] if isinstance(params[0], dict) else {}
    eventtime = None
    if len(params) > 1 and isinstance(params[1], (int, float)):
        eventtime = float(params[1])
    return status, eventtime


def parse_subscribe_result(result: Any) -> tuple[dict[str, Any], float | None]:
    """Split an objects/subscribe or objects/query reply into (status, eventtime)."""
    if not isinstance(result, dict):
        return {}, None
    status = result.get("status")
    eventtime = result.get("eventtime")
    return (
        status if isinstance(status, dict) else {},
        float(eventtime) if isinstance(eventtime, (int, float)) else None,
    )


class MoonrakerWebsocket:
    """JSON-RPC 2.0 websocket client for one Moonraker instance.

    Requests carry jsonrpc, id, method and params and nothing else. A dev_time
    member more than 600 seconds from the server clock is rejected with error
    -31000 (moonraker/common.py:777-787). Omitting it skips that check, so it is
    never sent. Responses on this fork carry two extra members, cli_time
    and dev_time (:887-894), which are ignored.
    """

    def __init__(
        self,
        client: MoonrakerClient,
        on_notification: Callable[[str, Any], None],
    ) -> None:
        self._client = client
        self._on_notification = on_notification
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[Any]] = {}

    @property
    def connected(self) -> bool:
        return self._ws is not None and not self._ws.closed

    async def connect(self) -> None:
        """Open the websocket at /websocket (moonraker/components/websockets.py:55)."""
        try:
            self._ws = await self._client.session.ws_connect(
                self._client.ws_url,
                headers=self._client.headers,
                heartbeat=25,
            )
        except (TimeoutError, aiohttp.ClientError) as err:
            raise MoonrakerConnectionError(f"Cannot open {self._client.ws_url}: {err}") from err

    async def close(self) -> None:
        ws = self._ws
        self._ws = None
        for future in self._pending.values():
            if not future.done():
                future.set_exception(MoonrakerConnectionError("Websocket closed before the reply"))
        self._pending.clear()
        if ws is not None and not ws.closed:
            await ws.close()

    async def call(self, method: str, params: Any = None, timeout: float = WS_CALL_TIMEOUT) -> Any:
        """Send one JSON-RPC request and wait for its reply."""
        ws = self._ws
        if ws is None or ws.closed:
            raise MoonrakerConnectionError("Websocket is not connected")
        self._next_id += 1
        request_id = self._next_id
        message: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
        }
        if params is not None:
            message["params"] = params
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await ws.send_json(message)
            return await asyncio.wait_for(future, timeout)
        except TimeoutError as err:
            raise MoonrakerConnectionError(f"No reply to {method}") from err
        except (aiohttp.ClientError, ConnectionResetError) as err:
            raise MoonrakerConnectionError(f"Websocket failed on {method}: {err}") from err
        finally:
            self._pending.pop(request_id, None)

    async def subscribe(
        self, objects: dict[str, list[str] | None]
    ) -> tuple[dict[str, Any], float | None]:
        """Subscribe to printer objects and return the reply snapshot.

        A subscribe replaces this connection's previous subscription
        (klippy/webhooks.py:569-570), so it is sent once with the full object
        set. The reply is a full snapshot when it reaches Klippy, but this fork
        can answer it from Moonraker's own cache when the request is already
        covered (moonraker/components/klippy_connection.py:711-763), so the
        caller queries once over HTTP first for a guaranteed snapshot.
        """
        result = await self.call("printer.objects.subscribe", {"objects": objects})
        return parse_subscribe_result(result)

    async def emergency_stop(self) -> None:
        """printer.emergency_stop, which the fork blocks over HTTP."""
        await self.call("printer.emergency_stop")

    async def listen(self) -> None:
        """Read messages until the socket closes.

        Replies resolve the matching call, everything with a method goes to the
        notification callback.
        """
        ws = self._ws
        if ws is None:
            raise MoonrakerConnectionError("Websocket is not connected")
        async for message in ws:
            if message.type is aiohttp.WSMsgType.TEXT:
                try:
                    payload = message.json()
                except ValueError:
                    _LOGGER.debug("Ignoring non JSON websocket frame")
                    continue
                self._dispatch(payload)
            elif message.type is aiohttp.WSMsgType.ERROR:
                raise MoonrakerConnectionError(f"Websocket error: {ws.exception()}")
            elif message.type in (
                aiohttp.WSMsgType.CLOSE,
                aiohttp.WSMsgType.CLOSING,
                aiohttp.WSMsgType.CLOSED,
            ):
                break

    def _dispatch(self, payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        request_id = payload.get("id")
        if request_id is not None and ("result" in payload or "error" in payload):
            future = self._pending.get(request_id)
            if future is None or future.done():
                return
            if "error" in payload:
                future.set_exception(
                    MoonrakerCommandError(_error_message(payload, "Moonraker returned an error"))
                )
            else:
                future.set_result(payload.get("result"))
            return
        method = payload.get("method")
        if isinstance(method, str):
            self._on_notification(method, payload.get("params"))
