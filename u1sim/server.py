"""The Klippy API server the simulator presents on a Unix domain socket.

One thread, one selector loop. Several clients are supported because Moonraker
opens a second connection for its /klippysocket bridge. A developer poking at
the socket by hand should not disturb the running Moonraker either.

The endpoint table, the reply shapes and the subscription semantics all come
from klippy/webhooks.py. The three subscription rules that matter:

  1. A push carries only the fields whose value changed (webhooks.py:533-538).
     Nothing changed means nothing is sent.
  2. A subscribe replaces that connection's previous subscription (:569-570).
  3. A null field list is frozen to the keys seen on the first pass (:529-532),
     so a key added later is never pushed.

The comparison baseline is one shared snapshot per pass, not per connection,
which is what Klippy does with its single last_query dict.
"""

from __future__ import annotations

import contextlib
import errno
import logging
import os
import selectors
import socket
import sys
import threading
import time
from collections.abc import Callable
from typing import Any

from . import protocol
from .model import PHYSICAL_EXTRUDER_NUM, PrinterModel
from .protocol import Request, WebRequestError
from .scenario import Scenario, ScenarioRunner

LOGGER = logging.getLogger("u1sim")

# webhooks.py:478
SUBSCRIPTION_REFRESH_TIME = 0.25
# Moonraker opens the socket with a 20 MB buffer limit (klippy_connection.py:63).
READ_CHUNK = 65536

# Every endpoint the simulator answers. The core set is what Moonraker asks for
# on connect (webhooks.py:321-326, :447-452, :489-491); the rest are the U1
# extras Moonraker then exposes as /printer/<endpoint>.
ENDPOINT_NAMES = [
    "list_endpoints",
    "info",
    "emergency_stop",
    "register_remote_method",
    "gcode/help",
    "gcode/script",
    "gcode/restart",
    "gcode/firmware_restart",
    "gcode/subscribe_output",
    "objects/list",
    "objects/query",
    "objects/subscribe",
    "pause_resume/cancel",
    "pause_resume/pause",
    "pause_resume/resume",
    "print_task_config/set_print_preferences",
    "control/main_fan",
    "control/bed_temp",
    "control/extruder_temp",
    "control/nozzle_diameter",
    "control/print_speed",
    "control/led",
    "query_endstops/status",
    # Simulator only. Not present on a printer, named so nobody mistakes it
    # for firmware.
    "u1sim/debug",
]


class Connection:
    """One connected client."""

    def __init__(self, sock: socket.socket, uid: int) -> None:
        self.sock = sock
        self.uid = uid
        self.inbuf = b""
        self.outbuf = b""
        self.closed = False
        self.client_info: dict[str, Any] = {}
        # objects/subscribe state
        self.subscription: dict[str, list[str] | None] | None = None
        self.sub_template: dict[str, Any] = {}
        # gcode/subscribe_output state
        self.gcode_template: dict[str, Any] | None = None
        # register_remote_method state, name to response template
        self.remote_methods: dict[str, dict[str, Any]] = {}

    def send(self, document: dict[str, Any]) -> None:
        if self.closed:
            return
        self.outbuf += protocol.encode(document)

    def flush(self) -> None:
        while self.outbuf and not self.closed:
            try:
                sent = self.sock.send(self.outbuf)
            except OSError as exc:
                if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                    return
                self.close()
                return
            if sent <= 0:
                return
            self.outbuf = self.outbuf[sent:]

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        with contextlib.suppress(OSError):
            self.sock.close()


class U1SimServer:
    """A fake Klippy host on a Unix socket."""

    def __init__(
        self,
        socket_path: str,
        model: PrinterModel | None = None,
        scenario: Scenario | None = None,
        speed: float = 1.0,
        gcode_path: str | None = None,
    ) -> None:
        self.socket_path = os.path.abspath(socket_path)
        self.model = model or PrinterModel(
            gcode_path=gcode_path or os.path.join(os.path.dirname(self.socket_path), "gcodes")
        )
        if gcode_path:
            self.model.gcode_path = gcode_path
        self.runner = ScenarioRunner(self.model, scenario, speed=speed)
        self.gcode = self.runner.gcode
        self.connections: dict[int, Connection] = {}
        self._next_uid = 1
        self._selector = selectors.DefaultSelector()
        self._listener: socket.socket | None = None
        self._running = False
        self._stop = threading.Event()
        self._started_at = time.monotonic()
        self._last_tick = self._started_at
        self._next_pass = 0.0
        # The scenario timeline holds at t=0 until the first client connects,
        # then runs from that moment. A demo that begins the instant a
        # controller attaches is what lets any observer, Moonraker or a person,
        # watch it from a cold printer rather than joining partway through.
        self._scenario_started = False
        # The shared previous-pass snapshot the delta comparison uses.
        self._last_query: dict[str, dict[str, Any]] = {}
        self.handlers: dict[str, Callable[[Connection, Request], Any]] = {
            "list_endpoints": self._ep_list_endpoints,
            "info": self._ep_info,
            "emergency_stop": self._ep_emergency_stop,
            "register_remote_method": self._ep_register_remote_method,
            "gcode/help": self._ep_gcode_help,
            "gcode/script": self._ep_gcode_script,
            "gcode/restart": self._ep_gcode_restart,
            "gcode/firmware_restart": self._ep_gcode_restart,
            "gcode/subscribe_output": self._ep_gcode_subscribe_output,
            "objects/list": self._ep_objects_list,
            "objects/query": self._ep_objects_query,
            "objects/subscribe": self._ep_objects_subscribe,
            "pause_resume/pause": self._ep_pause,
            "pause_resume/resume": self._ep_resume,
            "pause_resume/cancel": self._ep_cancel,
            "print_task_config/set_print_preferences": self._ep_set_preferences,
            "control/main_fan": self._ep_control_main_fan,
            "control/bed_temp": self._ep_control_bed_temp,
            "control/extruder_temp": self._ep_control_extruder_temp,
            "control/nozzle_diameter": self._ep_control_nozzle_diameter,
            "control/print_speed": self._ep_control_print_speed,
            "control/led": self._ep_control_led,
            "query_endstops/status": self._ep_query_endstops,
            "u1sim/debug": self._ep_debug,
        }

    # ---- clock -------------------------------------------------------
    def eventtime(self) -> float:
        """The Klipper reactor monotonic clock, seconds since Klippy start.

        webhooks.py:496 and :544 pass the reactor time straight into the reply,
        and Moonraker forwards it unchanged. It is not a Unix timestamp.
        """
        return time.monotonic() - self._started_at

    # ---- lifecycle ---------------------------------------------------
    def bind(self) -> None:
        """Bind and listen. Any stale socket file is removed first.

        webhooks.py:124-128 unlinks the path, binds AF_UNIX SOCK_STREAM then
        listens with a backlog of 1. The simulator uses a larger backlog so a
        second client is not refused while Moonraker is connecting.
        """
        directory = os.path.dirname(self.socket_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.setblocking(False)
        listener.bind(self.socket_path)
        listener.listen(8)
        # Moonraker refuses to connect unless the file is readable and writable
        # (klippy_connection.py:299-309).
        os.chmod(self.socket_path, 0o660)
        self._listener = listener
        self._selector.register(listener, selectors.EVENT_READ, data=None)
        self._started_at = time.monotonic()
        self._last_tick = self._started_at
        self._next_pass = self.eventtime()
        LOGGER.info("u1sim listening on %s", self.socket_path)

    def close(self) -> None:
        for conn in list(self.connections.values()):
            self._drop(conn)
        if self._listener is not None:
            with contextlib.suppress(KeyError, ValueError):
                self._selector.unregister(self._listener)
            self._listener.close()
            self._listener = None
        self._selector.close()
        if os.path.exists(self.socket_path):
            with contextlib.suppress(OSError):
                os.unlink(self.socket_path)
        self._running = False

    def serve_forever(self) -> None:
        if self._listener is None:
            self.bind()
        self._running = True
        try:
            while not self._stop.is_set():
                self.poll_once()
        finally:
            self.close()

    def shutdown(self) -> None:
        self._stop.set()

    def start_in_thread(self) -> threading.Thread:
        """Run the loop on a daemon thread. Used by the tests."""
        if self._listener is None:
            self.bind()
        thread = threading.Thread(target=self.serve_forever, name="u1sim", daemon=True)
        thread.start()
        return thread

    # ---- the loop ----------------------------------------------------
    def poll_once(self, timeout: float | None = None) -> None:
        now = self.eventtime()
        if timeout is None:
            timeout = max(0.0, min(SUBSCRIPTION_REFRESH_TIME, self._next_pass - now))
        for key, mask in self._selector.select(timeout):
            if key.data is None:
                self._accept()
            else:
                conn: Connection = key.data
                if mask & selectors.EVENT_READ:
                    self._read(conn)
                if mask & selectors.EVENT_WRITE and not conn.closed:
                    conn.flush()
        self.tick()

    def tick(self) -> None:
        """Advance the scenario then run a subscription pass when one is due."""
        now = self.eventtime()
        wall = time.monotonic()
        if self._scenario_started:
            self.runner.advance(wall - self._last_tick)
            self._last_tick = wall
        self.gcode = self.runner.gcode
        if now >= self._next_pass:
            self._next_pass = now + SUBSCRIPTION_REFRESH_TIME
            self._subscription_pass(now)
        for conn in list(self.connections.values()):
            conn.flush()
            if conn.closed:
                self._drop(conn)

    def _accept(self) -> None:
        assert self._listener is not None
        try:
            sock, _addr = self._listener.accept()
        except OSError:
            return
        sock.setblocking(False)
        conn = Connection(sock, self._next_uid)
        self._next_uid += 1
        self.connections[conn.uid] = conn
        self._selector.register(sock, selectors.EVENT_READ, data=conn)
        if not self._scenario_started:
            # Anchor the timeline to the first attach so no startup delay is
            # counted against it.
            self._scenario_started = True
            self._last_tick = time.monotonic()
        LOGGER.info("u1sim client %d connected", conn.uid)

    def _drop(self, conn: Connection) -> None:
        self.connections.pop(conn.uid, None)
        with contextlib.suppress(KeyError, ValueError):
            self._selector.unregister(conn.sock)
        conn.close()
        LOGGER.info("u1sim client %d disconnected", conn.uid)

    def _read(self, conn: Connection) -> None:
        try:
            data = conn.sock.recv(READ_CHUNK)
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                return
            data = b""
        if not data:
            self._drop(conn)
            return
        conn.inbuf += data
        documents, conn.inbuf = protocol.decode_stream(conn.inbuf)
        for raw in documents:
            if not raw:
                continue
            self._dispatch(conn, raw)

    def _dispatch(self, conn: Connection, raw: bytes) -> None:
        """Decode one request and answer it.

        A malformed request is logged and dropped with no reply, which is what
        Klippy does (webhooks.py:252-259).
        """
        try:
            request = Request(raw)
        except Exception as exc:
            LOGGER.info("u1sim: error decoding request %r: %s", raw[:200], exc)
            return
        handler = self.handlers.get(request.method)
        if handler is None:
            message = f"webhooks: No registered callback for path '{request.method}'"
            LOGGER.info(message)
            if request.wants_reply:
                conn.send(protocol.failure(request.id, message))
            return
        try:
            payload = handler(conn, request)
        except WebRequestError as exc:
            if request.wants_reply:
                conn.send(protocol.failure(request.id, str(exc)))
            return
        except Exception as exc:  # pragma: no cover - defensive
            LOGGER.exception("u1sim: internal error on %s", request.method)
            if request.wants_reply:
                conn.send(protocol.failure(request.id, str(exc)))
            return
        if request.wants_reply:
            conn.send(protocol.success(request.id, payload))

    # ---- core endpoints ----------------------------------------------
    def _ep_list_endpoints(self, conn: Connection, request: Request) -> dict[str, Any]:
        return {"endpoints": list(ENDPOINT_NAMES)}

    def _ep_info(self, conn: Connection, request: Request) -> dict[str, Any]:
        """webhooks.py:365-383. All twelve keys.

        klipper_path and python_path are mandatory: Moonraker subscripts both
        with no default in _save_path_info (klippy_connection.py:356-357), so a
        missing key is a KeyError during init.
        """
        client_info = request.get_dict("client_info", None)
        if client_info is not None:
            conn.client_info = client_info
        return {
            "state": self.model.klippy_state,
            "state_message": self.model.state_message,
            "hostname": self.model.hostname,
            "klipper_path": os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "python_path": sys.executable,
            "process_id": os.getpid(),
            "user_id": os.getuid(),
            "group_id": os.getgid(),
            "log_file": self.model.log_file,
            "config_file": self.model.config_file,
            "software_version": self.model.software_version,
            "cpu_info": "u1sim simulated host",
        }

    def _ep_emergency_stop(self, conn: Connection, request: Request) -> dict[str, Any]:
        self.model.emergency_stop()
        return {}

    def _ep_register_remote_method(self, conn: Connection, request: Request) -> dict[str, Any]:
        """webhooks.py:389-397. Stores the template and answers {}."""
        template = request.get_dict("response_template")
        method = request.get_str("remote_method")
        if template is None or method is None:
            raise WebRequestError("Missing Argument [remote_method]")
        conn.remote_methods[method] = template
        return {}

    def _ep_gcode_help(self, conn: Connection, request: Request) -> dict[str, str]:
        return {name: f"u1sim implementation of {name}" for name in sorted(self.gcode.handlers)}

    def _ep_gcode_script(self, conn: Connection, request: Request) -> dict[str, Any]:
        """gcode/script. webhooks.py:456-457 reads params.script.

        An empty result is what a good script returns. Moonraker rewrites that
        to the string "ok" (klippy_connection.py:615-619). A refusal comes
        back as an error, which Moonraker turns into HTTP 400.
        """
        script = request.get_str("script")
        if script is None:
            raise WebRequestError("Missing Argument [script]")
        responses = self.gcode.run_script(script)
        for line in responses:
            self._broadcast_gcode(line)
        return {}

    def _ep_gcode_restart(self, conn: Connection, request: Request) -> dict[str, Any]:
        self.model.set_klippy_state("startup", "Printer is not ready")
        self.runner.reset()
        self.gcode = self.runner.gcode
        self.model.set_klippy_state("ready")
        return {}

    def _ep_gcode_subscribe_output(self, conn: Connection, request: Request) -> dict[str, Any]:
        """webhooks.py:469-475. Stores the template and answers {}."""
        conn.gcode_template = request.get_dict("response_template", {}) or {}
        return {}

    def _broadcast_gcode(self, message: str) -> None:
        """Push one G-code response line to every output subscriber."""
        for conn in list(self.connections.values()):
            if conn.gcode_template is None or conn.closed:
                continue
            document = dict(conn.gcode_template)
            document["params"] = {"response": message}
            conn.send(document)

    # ---- status endpoints --------------------------------------------
    def _ep_objects_list(self, conn: Connection, request: Request) -> dict[str, Any]:
        """webhooks.py:492-495.

        Moonraker checks for virtual_sdcard, display_status and pause_resume
        and only logs a warning when one is missing
        (klippy_connection.py:568-575). All three are here.
        """
        return {"objects": list(self.model.objects().keys())}

    @staticmethod
    def _validate_objects(request: Request) -> dict[str, list[str] | None]:
        """webhooks.py:557-565: a str key plus a value that is null or a str list."""
        objects = request.get_dict("objects")
        if objects is None:
            raise WebRequestError("Missing Argument [objects]")
        for key, value in objects.items():
            if not isinstance(key, str) or (value is not None and not isinstance(value, list)):
                raise WebRequestError("Invalid argument")
            if value is not None:
                for field in value:
                    if not isinstance(field, str):
                        raise WebRequestError("Invalid argument")
        return objects

    def _ep_objects_query(self, conn: Connection, request: Request) -> dict[str, Any]:
        objects = self._validate_objects(request)
        eventtime = self.eventtime()
        snapshot = self._snapshot()
        status = self._extract(objects, snapshot, full=True)
        self._last_query = snapshot
        return {"eventtime": eventtime, "status": status}

    def _ep_objects_subscribe(self, conn: Connection, request: Request) -> dict[str, Any]:
        """A subscribe replaces the connection's previous subscription and its
        immediate reply is a full snapshot (webhooks.py:569-570, :582)."""
        objects = self._validate_objects(request)
        template = request.get_dict("response_template", {}) or {}
        eventtime = self.eventtime()
        snapshot = self._snapshot()
        status = self._extract(objects, snapshot, full=True)
        self._last_query = snapshot
        conn.subscription = objects
        conn.sub_template = template
        return {"eventtime": eventtime, "status": status}

    def _snapshot(self) -> dict[str, dict[str, Any]]:
        """One get_status pass over every object, like Klippy's per-pass cache."""
        return self.model.objects()

    def _extract(
        self,
        objects: dict[str, list[str] | None],
        snapshot: dict[str, dict[str, Any]],
        full: bool,
    ) -> dict[str, dict[str, Any]]:
        """Build a reply body from a snapshot.

        full=True gives every requested field, which is what a query and the
        first reply to a subscribe do. full=False gives only the fields whose
        value differs from the previous pass, which is what a push does
        (webhooks.py:529-542). An unknown object answers {} rather than an
        error (:517-519). That is what lets the simulator serve a subset.
        """
        out: dict[str, dict[str, Any]] = {}
        for name, fields in objects.items():
            values = snapshot.get(name)
            if values is None:
                if full:
                    out[name] = {}
                continue
            if fields is None:
                # Freeze the field list on the first pass, in place, exactly as
                # Klippy rewrites the subscription dict (webhooks.py:529-532).
                fields = list(values.keys())
                if fields:
                    objects[name] = fields
            previous = self._last_query.get(name, {})
            selected: dict[str, Any] = {}
            for field in fields:
                value = values.get(field, None)
                if full or value != previous.get(field, None):
                    selected[field] = value
            if selected or full:
                out[name] = selected
        return out

    def _subscription_pass(self, eventtime: float) -> None:
        """Push changed fields to every subscriber, then store the baseline."""
        subscribers = [
            conn
            for conn in self.connections.values()
            if conn.subscription is not None and not conn.closed
        ]
        if not subscribers:
            # Klippy unregisters the timer when the last subscription goes
            # (webhooks.py:550-554). Keeping the baseline stale here is
            # deliberate: the next subscribe replies with a full snapshot and
            # resets it.
            return
        snapshot = self._snapshot()
        for conn in subscribers:
            assert conn.subscription is not None
            status = self._extract(conn.subscription, snapshot, full=False)
            if not status:
                continue
            document = dict(conn.sub_template)
            document["params"] = {"eventtime": eventtime, "status": status}
            conn.send(document)
        self._last_query = snapshot

    # ---- U1 extras ---------------------------------------------------
    def _ep_pause(self, conn: Connection, request: Request) -> dict[str, Any]:
        """pause_resume/pause runs the PAUSE macro (pause_resume.py:50-55)."""
        self.gcode.run_script("PAUSE")
        return {}

    def _ep_resume(self, conn: Connection, request: Request) -> dict[str, Any]:
        self.gcode.run_script("RESUME")
        return {}

    def _ep_cancel(self, conn: Connection, request: Request) -> dict[str, Any]:
        self.gcode.run_script("CANCEL_PRINT")
        return {}

    def _ep_set_preferences(self, conn: Connection, request: Request) -> dict[str, Any]:
        """print_task_config/set_print_preferences. print_task_config.py:140-186.

        Always 200 shaped: success and failure both come back as a result with
        a state member, so a client has to read state rather than trust the
        HTTP code.
        """
        params: dict[str, Any] = {}
        for key in (
            "auto_replenish_filament",
            "filament_entangle_detect",
            "replenish_ignore_color",
            "end_led_turn_off",
        ):
            value = request.get(key)
            if value is not None:
                params[key] = value
        sen = request.get("filament_entangle_sen")
        if sen is not None:
            params["filament_entangle_sen"] = sen
        try:
            self.model.ptc.set_preferences(params)
        except Exception as exc:
            return {"state": "error", "message": str(exc)}
        return {"state": "success"}

    def _ep_control_main_fan(self, conn: Connection, request: Request) -> dict[str, Any]:
        """control/main_fan takes S as a percent, 0 to 100 (fan.py:146-155)."""
        speed = request.get_float("S", 0.0) or 0.0
        speed = min(100.0, max(0.0, speed))
        self.model.fan_speed = speed / 100.0
        return {"state": "success"}

    def _ep_control_bed_temp(self, conn: Connection, request: Request) -> dict[str, Any]:
        """control/bed_temp takes S in Celsius (heater_bed.py:25-33)."""
        temp = request.get_float("S", 0.0) or 0.0
        self.model.heaters["heater_bed"].target = max(0.0, temp)
        return {"state": "success"}

    def _ep_control_extruder_temp(self, conn: Connection, request: Request) -> dict[str, Any]:
        """control/extruder_temp takes S, T and A (extruder.py:1116-1121).

        T is the index and A decides whether it goes through the colour map,
        the same convention the T codes use.
        """
        temp = request.get_float("S", 0.0) or 0.0
        index = request.get_int("T", None)
        use_map = (request.get_int("A", 1) or 0) != 0
        head = self.model.active_head if index is None else index
        if index is not None and use_map:
            head = self.model.ptc.map_index(index)
        if head < 0 or head >= PHYSICAL_EXTRUDER_NUM:
            return {"state": "error", "message": f"extruder{head} does not exist"}
        self.model.heaters[self.model.extruder_name(head)].target = max(0.0, temp)
        return {"state": "success"}

    def _ep_control_nozzle_diameter(self, conn: Connection, request: Request) -> dict[str, Any]:
        index = request.get_int("T", self.model.active_head)
        diameter = request.get_float("D", None)
        if index is None or index < 0 or index >= PHYSICAL_EXTRUDER_NUM:
            return {"state": "error", "message": f"extruder{index} does not exist"}
        if diameter is None:
            return {"state": "success", "diameter": self.model.nozzle_diameter[index]}
        self.model.nozzle_diameter[index] = diameter
        return {"state": "success"}

    def _ep_control_print_speed(self, conn: Connection, request: Request) -> dict[str, Any]:
        """control/print_speed takes percentage (klippy_apis.py:271-276)."""
        percentage = request.get_int("S", None)
        if percentage is None:
            percentage = int(request.get_float("percentage", 100.0) or 100.0)
        self.model.speed_factor = max(0.01, percentage / 100.0)
        return {"state": "success"}

    def _ep_control_led(self, conn: Connection, request: Request) -> dict[str, Any]:
        """control/led is a mux endpoint keyed on led (led.py:31-46)."""
        name = request.get_str("led", None)
        if name not in (None, "cavity_led"):
            raise WebRequestError(f"The value '{name}' is not valid for led")
        self.model.led_color = (
            request.get_float("red", 0.0) or 0.0,
            request.get_float("green", 0.0) or 0.0,
            request.get_float("blue", 0.0) or 0.0,
            request.get_float("white", 0.0) or 0.0,
        )
        return {"state": "success"}

    def _ep_query_endstops(self, conn: Connection, request: Request) -> dict[str, Any]:
        """query_endstops/status (query_endstops.py:14). open or TRIGGERED."""
        return {"last_query": {"x": "open", "y": "open", "z": "open"}}

    def _ep_debug(self, conn: Connection, request: Request) -> dict[str, Any]:
        """Simulator introspection. There is no such endpoint on a printer."""
        payload = self.model.debug_view()
        payload["runner"] = self.runner.status()
        payload["connections"] = len(self.connections)
        payload["recent_gcode"] = self.model.gcode_log[-20:]
        return payload
