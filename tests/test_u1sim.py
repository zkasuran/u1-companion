"""Tests for u1sim, the Snapmaker U1 Klippy simulator.

Every test talks to the simulator the way Moonraker does: a raw Unix socket,
JSON documents framed by a single 0x03 byte. Nothing here uses a helper client
from the package under test, so the framing itself is exercised.

Run from the repository root:

    python -m pytest tests/test_u1sim.py -v
"""

from __future__ import annotations

import json
import os
import socket
import sys
import time
from typing import Any

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import contextlib

from u1sim import protocol
from u1sim.model import (
    DEFAULT_PRINT_TASK_CONFIG,
    FILAMENT_INFO_STRUCT,
    LOGICAL_EXTRUDER_NUM,
    PHYSICAL_EXTRUDER_NUM,
    PrinterModel,
)
from u1sim.scenario import (
    Scenario,
    ScenarioError,
    ScenarioRunner,
    available_scenarios,
)
from u1sim.server import ENDPOINT_NAMES, U1SimServer

TIMEOUT = 10.0


class RawClient:
    """A hand rolled Klippy socket client. 0x03 framing, nothing else."""

    def __init__(self, path: str) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(TIMEOUT)
        self.sock.connect(path)
        self.buffer = b""
        self.pushes: list[dict[str, Any]] = []
        self._next_id = 1

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self.sock.close()

    # ---- raw plumbing ------------------------------------------------
    def write_raw(self, payload: bytes) -> None:
        self.sock.sendall(payload)

    def read_document(self, timeout: float = TIMEOUT) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while protocol.TERMINATOR not in self.buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(f"no document arrived within {timeout}s")
            self.sock.settimeout(remaining)
            chunk = self.sock.recv(65536)
            if not chunk:
                raise AssertionError("the simulator closed the connection")
            self.buffer += chunk
        raw, self.buffer = self.buffer.split(protocol.TERMINATOR, 1)
        return json.loads(raw.decode("utf-8"))

    def drain(self, seconds: float) -> list[dict[str, Any]]:
        """Collect everything that arrives over a window."""
        out: list[dict[str, Any]] = []
        deadline = time.monotonic() + seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return out
            try:
                self.sock.settimeout(remaining)
                if protocol.TERMINATOR not in self.buffer:
                    chunk = self.sock.recv(65536)
                    if not chunk:
                        return out
                    self.buffer += chunk
            except TimeoutError:
                return out
            while protocol.TERMINATOR in self.buffer:
                raw, self.buffer = self.buffer.split(protocol.TERMINATOR, 1)
                if raw:
                    out.append(json.loads(raw.decode("utf-8")))

    # ---- request and reply -------------------------------------------
    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        self.write_raw(
            protocol.encode({"id": request_id, "method": method, "params": params or {}})
        )
        while True:
            document = self.read_document()
            if document.get("id") == request_id:
                return document
            self.pushes.append(document)

    def result(self, method: str, params: dict[str, Any] | None = None) -> Any:
        reply = self.request(method, params)
        assert "error" not in reply, "{} failed: {}".format(method, reply.get("error"))
        return reply["result"]

    def error(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        reply = self.request(method, params)
        assert "error" in reply, f"{method} unexpectedly succeeded: {reply}"
        return reply["error"]


class Harness:
    """A running simulator plus the socket path it listens on."""

    def __init__(self, server: U1SimServer) -> None:
        self.server = server
        self.thread = server.start_in_thread()

    @property
    def path(self) -> str:
        return self.server.socket_path

    @property
    def model(self) -> PrinterModel:
        return self.server.model

    def client(self) -> RawClient:
        return RawClient(self.path)

    def stop(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=5)

    def wait_for_scenario(self, timeout: float = 10.0) -> None:
        """Block until every scenario step has fired.

        The timeline is anchored to the first client attach (server.py:299-303),
        the way a real run waits for Moonraker, so this attaches one instead of
        waiting on a clock that has not started.
        """
        total = len(self.server.runner.scenario.steps)  # type: ignore[union-attr]
        conn = self.client()
        try:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if self.server.runner.cursor >= total:
                    return
                time.sleep(0.02)
        finally:
            conn.close()
        raise AssertionError(f"scenario stopped at step {self.server.runner.cursor} of {total}")


def make_harness(tmp_path, scenario_name=None, speed=1.0) -> Harness:
    scenario = Scenario.load(scenario_name) if scenario_name else None
    if scenario is not None:
        scenario.loop = False
    server = U1SimServer(
        socket_path=str(tmp_path / "klippy.sock"),
        scenario=scenario,
        speed=speed,
        gcode_path=str(tmp_path / "gcodes"),
    )
    return Harness(server)


@pytest.fixture()
def bare(tmp_path):
    """An idle printer with no scenario. Nothing changes unless a test says so."""
    harness = make_harness(tmp_path)
    try:
        yield harness
    finally:
        harness.stop()


@pytest.fixture()
def loaded(tmp_path):
    """Four slots loaded, three of them by RFID, the colour map spread out."""
    harness = make_harness(tmp_path, "idle_loaded", speed=200.0)
    try:
        harness.wait_for_scenario()
        yield harness
    finally:
        harness.stop()


@pytest.fixture()
def client(bare):
    conn = bare.client()
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------- framing


def test_decode_stream_keeps_the_trailing_partial():
    buffer = b'{"a":1}\x03{"b":2}\x03{"c"'
    documents, partial = protocol.decode_stream(buffer)
    assert documents == [b'{"a":1}', b'{"b":2}']
    assert partial == b'{"c"'


def test_encode_appends_one_terminator():
    payload = protocol.encode({"id": 1, "result": {}})
    assert payload.endswith(protocol.TERMINATOR)
    assert payload.count(protocol.TERMINATOR) == 1


def test_two_requests_in_one_write_get_two_replies(client):
    batch = protocol.encode({"id": 101, "method": "info", "params": {}})
    batch += protocol.encode({"id": 102, "method": "objects/list", "params": {}})
    client.write_raw(batch)
    first = client.read_document()
    second = client.read_document()
    assert {first["id"], second["id"]} == {101, 102}


def test_a_request_split_across_writes_is_reassembled(client):
    payload = protocol.encode({"id": 103, "method": "info", "params": {}})
    client.write_raw(payload[:9])
    time.sleep(0.1)
    client.write_raw(payload[9:])
    assert client.read_document()["id"] == 103


def test_a_request_without_an_id_gets_no_reply(client):
    client.write_raw(protocol.encode({"method": "info", "params": {}}))
    client.write_raw(protocol.encode({"id": 104, "method": "info", "params": {}}))
    reply = client.read_document()
    assert reply["id"] == 104


@pytest.mark.parametrize(
    "document",
    [
        {"id": 1, "method": 42, "params": {}},
        {"id": 1, "method": "info", "params": "not a dict"},
        {"id": 1},
        [1, 2, 3],
    ],
)
def test_a_malformed_request_is_dropped_without_a_reply(client, document):
    client.write_raw(protocol.encode(document))
    client.write_raw(protocol.encode({"id": 105, "method": "info", "params": {}}))
    assert client.read_document()["id"] == 105


def test_an_unknown_endpoint_returns_an_error(client):
    error = client.error("no/such/endpoint")
    assert error["error"] == "WebRequestError"
    assert "No registered callback" in error["message"]


# -------------------------------------------------------------- handshake

INFO_KEYS = {
    "state",
    "state_message",
    "hostname",
    "klipper_path",
    "python_path",
    "process_id",
    "user_id",
    "group_id",
    "log_file",
    "config_file",
    "software_version",
    "cpu_info",
}


def test_info_carries_every_key_moonraker_reads(client):
    info = client.result("info", {"client_info": {"program": "Moonraker", "version": "0.9"}})
    assert set(info) == INFO_KEYS
    # Moonraker subscripts these two with no default (klippy_connection.py:356).
    assert os.path.isdir(info["klipper_path"])
    assert info["python_path"]
    assert info["state"] in ("ready", "startup", "shutdown", "error")


def test_list_endpoints_covers_what_moonraker_needs(client):
    endpoints = client.result("list_endpoints")["endpoints"]
    required = {
        "info",
        "emergency_stop",
        "register_remote_method",
        "objects/list",
        "objects/query",
        "objects/subscribe",
        "gcode/script",
        "gcode/subscribe_output",
        "gcode/restart",
        "gcode/firmware_restart",
        "gcode/help",
        "pause_resume/pause",
        "pause_resume/resume",
        "pause_resume/cancel",
        "print_task_config/set_print_preferences",
    }
    assert required <= set(endpoints)
    assert endpoints == ENDPOINT_NAMES


def test_register_remote_method_answers_empty(client):
    for name in (
        "shutdown_machine",
        "reboot_machine",
        "clear_exception",
        "raise_exception",
        "pause_job_queue",
        "start_job_queue",
        "publish_mqtt_topic",
    ):
        result = client.result(
            "register_remote_method",
            {"response_template": {"method": name}, "remote_method": name},
        )
        assert result == {}


def test_gcode_subscribe_output_answers_empty(client):
    assert (
        client.result(
            "gcode/subscribe_output", {"response_template": {"method": "process_gcode_response"}}
        )
        == {}
    )


def test_objects_list_has_the_three_objects_moonraker_checks_for(client):
    objects = client.result("objects/list")["objects"]
    assert {"virtual_sdcard", "display_status", "pause_resume"} <= set(objects)
    assert {"print_task_config", "filament_detect", "print_stats"} <= set(objects)
    for head in range(PHYSICAL_EXTRUDER_NUM):
        name = "extruder" if head == 0 else f"extruder{head}"
        assert name in objects


def test_configfile_points_at_the_gcode_directory(bare, client):
    status = client.result("objects/query", {"objects": {"configfile": None}})["status"]
    path = status["configfile"]["config"]["virtual_sdcard"]["path"]
    assert path == bare.model.gcode_path


def test_emergency_stop_shuts_the_printer_down(client):
    assert client.result("emergency_stop") == {}
    status = client.result("objects/query", {"objects": {"webhooks": None}})["status"]
    assert status["webhooks"]["state"] == "shutdown"
    assert status["webhooks"]["state_message"] == "Shutdown due to webhooks request"


# ------------------------------------------------------------------ query


def test_query_returns_an_eventtime_and_a_status(client):
    result = client.result("objects/query", {"objects": {"print_stats": None}})
    assert set(result) == {"eventtime", "status"}
    assert isinstance(result["eventtime"], float)
    # Reactor monotonic seconds since start, not a Unix timestamp.
    assert 0.0 <= result["eventtime"] < 3600.0


def test_query_honours_a_field_list(client):
    result = client.result("objects/query", {"objects": {"print_stats": ["state", "filename"]}})
    assert set(result["status"]["print_stats"]) == {"state", "filename"}


def test_query_answers_an_unknown_object_with_an_empty_dict(client):
    result = client.result("objects/query", {"objects": {"nope": None, "print_stats": None}})
    assert result["status"]["nope"] == {}
    assert result["status"]["print_stats"]["state"] == "standby"


def test_a_field_that_does_not_exist_comes_back_as_null(client):
    result = client.result("objects/query", {"objects": {"print_stats": ["not_a_field"]}})
    assert result["status"]["print_stats"] == {"not_a_field": None}


@pytest.mark.parametrize(
    "objects",
    [{"print_stats": "state"}, {"print_stats": [1, 2]}, {"print_stats": 7}],
)
def test_query_rejects_a_bad_objects_argument(client, objects):
    error = client.error("objects/query", {"objects": objects})
    assert error["message"] == "Invalid argument"


def test_query_without_objects_is_an_error(client):
    assert "objects" in client.error("objects/query", {})["message"]


# ------------------------------------------------------------ subscriptions


def test_subscribe_replies_with_a_full_snapshot(client):
    result = client.result(
        "objects/subscribe",
        {
            "objects": {"print_task_config": None, "print_stats": None},
            "response_template": {"method": "process_status_update"},
        },
    )
    assert set(result) == {"eventtime", "status"}
    assert set(result["status"]) == {"print_task_config", "print_stats"}
    assert set(result["status"]["print_task_config"]) == set(DEFAULT_PRINT_TASK_CONFIG)


def test_pushes_carry_the_template_and_only_changed_fields(bare):
    conn = bare.client()
    try:
        conn.result(
            "objects/subscribe",
            {
                "objects": {"print_stats": None, "heaters": None},
                "response_template": {"method": "process_status_update"},
            },
        )
        # Change one field on one object.
        conn.result("gcode/script", {"script": "SET_PRINT_STATS_INFO TOTAL_LAYER=77"})
        pushes = [doc for doc in conn.drain(1.2) if doc.get("method") == "process_status_update"]
        assert pushes, "no status update arrived within a second"
        merged: dict[str, Any] = {}
        for push in pushes:
            assert "id" not in push
            assert set(push["params"]) == {"eventtime", "status"}
            for name, fields in push["params"]["status"].items():
                merged.setdefault(name, {}).update(fields)
        assert merged["print_stats"]["info"] == {"total_layer": 77, "current_layer": None}
        # heaters never changes, so it must not appear in any push.
        assert "heaters" not in merged
    finally:
        conn.close()


def test_a_subscribe_replaces_the_previous_subscription(bare):
    conn = bare.client()
    try:
        conn.result(
            "objects/subscribe",
            {"objects": {"print_stats": None}, "response_template": {"method": "push"}},
        )
        conn.result(
            "objects/subscribe",
            {"objects": {"heater_bed": None}, "response_template": {"method": "push"}},
        )
        conn.result("gcode/script", {"script": "M140 S60"})
        pushes = [doc for doc in conn.drain(1.2) if doc.get("method") == "push"]
        assert pushes
        names = set()
        for push in pushes:
            names.update(push["params"]["status"])
        assert names == {"heater_bed"}
    finally:
        conn.close()


def test_a_null_field_list_is_frozen_on_the_first_pass(bare):
    conn = bare.client()
    try:
        conn.result(
            "objects/subscribe",
            {"objects": {"print_stats": None}, "response_template": {"method": "push"}},
        )
        subscription = bare.server.connections[max(bare.server.connections)].subscription
        assert subscription is not None
        assert subscription["print_stats"] == list(bare.model.objects()["print_stats"].keys())
    finally:
        conn.close()


def test_two_clients_keep_separate_subscriptions(bare):
    first = bare.client()
    second = bare.client()
    try:
        first.result(
            "objects/subscribe",
            {"objects": {"print_stats": ["state"]}, "response_template": {"method": "a"}},
        )
        second.result(
            "objects/subscribe",
            {"objects": {"heater_bed": ["target"]}, "response_template": {"method": "b"}},
        )
        first.result("gcode/script", {"script": "M140 S55"})
        first_pushes = [doc for doc in first.drain(1.0) if doc.get("method") == "a"]
        second_pushes = [doc for doc in second.drain(1.0) if doc.get("method") == "b"]
        assert not first_pushes, "print_stats.state did not change so nothing is due"
        assert second_pushes
        assert second_pushes[0]["params"]["status"]["heater_bed"]["target"] == 55.0
    finally:
        first.close()
        second.close()


# ------------------------------------------------------- print_task_config


def assert_print_task_config_is_well_formed(config: dict[str, Any]) -> None:
    """Every constraint print_task_config._early_check enforces (:193-218)."""
    assert set(config) == set(DEFAULT_PRINT_TASK_CONFIG)
    for key in (
        "filament_vendor",
        "filament_type",
        "filament_sub_type",
        "filament_color",
        "filament_color_rgba",
        "filament_color_multi",
        "filament_official",
        "filament_sku",
        "filament_edit",
        "filament_exist",
        "filament_soft",
        "extruders_used",
        "extruders_replenished",
        "end_unload_filament",
        "flow_calib_extruders",
    ):
        assert len(config[key]) == PHYSICAL_EXTRUDER_NUM, key
    assert len(config["extruder_map_table"]) == LOGICAL_EXTRUDER_NUM
    for slot in range(PHYSICAL_EXTRUDER_NUM):
        rgba = config["filament_color_rgba"][slot]
        assert isinstance(rgba, str) and len(rgba) == 8, rgba
        assert not isinstance(config["filament_color"][slot], str)
        multi = config["filament_color_multi"][slot]
        assert set(multi) == {"nums", "alpha", "mode", "colors"}
        assert 1 <= multi["nums"] <= 5
        assert len(multi["colors"]) == multi["nums"]
        for entry in multi["colors"]:
            assert isinstance(entry, str) and len(entry) == 6, entry
    for head in config["extruder_map_table"]:
        assert 0 <= head < PHYSICAL_EXTRUDER_NUM
    assert config["filament_entangle_sen"] in ("low", "medium", "high")
    assert set(config["reprint_info"]) == set(DEFAULT_PRINT_TASK_CONFIG["reprint_info"])


def test_a_fresh_print_task_config_matches_the_firmware_defaults(client):
    config = client.result("objects/query", {"objects": {"print_task_config": None}})["status"][
        "print_task_config"
    ]
    assert_print_task_config_is_well_formed(config)
    assert config["filament_vendor"] == ["NONE"] * 4
    assert config["filament_color"] == [0xFFFFFFFF] * 4
    assert config["extruder_map_table"][:4] == [0, 1, 2, 3]
    assert config["extruder_map_table"][4:] == [0] * 28
    assert config["extruders_replenished"] == [0, 1, 2, 3]
    assert config["auto_replenish_filament"] is True
    assert config["filament_entangle_sen"] == "medium"


def test_the_loaded_scenario_produces_a_real_four_slot_payload(loaded):
    conn = loaded.client()
    try:
        status = conn.result(
            "objects/query",
            {"objects": {"print_task_config": None, "filament_detect": None}},
        )["status"]
    finally:
        conn.close()
    config = status["print_task_config"]
    assert_print_task_config_is_well_formed(config)
    assert config["filament_vendor"] == ["Snapmaker", "Snapmaker", "Snapmaker", "Generic"]
    assert config["filament_type"] == ["PLA", "PLA", "PETG", "PLA"]
    assert config["filament_sub_type"] == ["Basic", "Matte", "HF", "Silk"]
    assert config["filament_official"] == [True, True, True, False]
    assert config["filament_sku"] == [12001, 12042, 13007, 0]
    assert config["filament_exist"] == [True] * 4
    # Editable only where the slot is loaded and not official (:462-472).
    assert config["filament_edit"] == [False, False, False, True]
    assert config["filament_color_rgba"] == [
        "000000FF",
        "F5F0E1FF",
        "D3232AFF",
        "1E88E5FF",
    ]
    assert config["filament_color"] == [4278190080, 4294308065, 4292027178, 4280191205]
    assert config["filament_color_multi"][3] == {
        "nums": 2,
        "alpha": 255,
        "mode": 1,
        "colors": ["1E88E5", "43A047"],
    }
    # Six logical colours over four heads.
    assert config["extruder_map_table"][:6] == [0, 1, 2, 3, 1, 2]
    assert config["filament_entangle_sen"] == "high"
    assert config["filament_entangle_detect"] is True

    detect = status["filament_detect"]
    assert set(detect) == {"info", "state", "config"}
    assert len(detect["info"]) == PHYSICAL_EXTRUDER_NUM
    assert detect["state"] == [0, 0, 0, 0]
    assert detect["config"] == {"startup_stay": False}
    for entry in detect["info"]:
        assert set(entry) == set(FILAMENT_INFO_STRUCT)
    assert detect["info"][0]["OFFICIAL"] is True
    assert detect["info"][0]["SKU"] == 12001
    assert detect["info"][0]["CARD_UID"] == [4, 210, 17, 32]
    # Slot 3 was filled by hand, so its NFC record stays at the defaults.
    assert detect["info"][3] == FILAMENT_INFO_STRUCT


# --------------------------------------------------------------- RFID rules


def official_tag(**overrides: Any) -> dict[str, Any]:
    tag = {
        "VENDOR": "Snapmaker",
        "MANUFACTURER": "Snapmaker",
        "MAIN_TYPE": "PLA",
        "SUB_TYPE": "Basic",
        "ALPHA": 255,
        "COLOR_NUMS": 1,
        "RGB_1": 0x112233,
        "ARGB_COLOR": 0xFF112233,
        "SKU": 99001,
        "OFFICIAL": True,
    }
    tag.update(overrides)
    return tag


def test_a_scanned_tag_fills_the_slot():
    model = PrinterModel()
    assert model.scan_tag(1, official_tag()) is True
    config = model.objects()["print_task_config"]
    assert config["filament_vendor"][1] == "Snapmaker"
    assert config["filament_color_rgba"][1] == "112233FF"
    assert config["filament_color"][1] == 0xFF112233
    assert config["filament_official"][1] is True
    assert config["filament_sku"][1] == 99001


def test_a_repeat_of_the_same_official_sku_is_ignored():
    """print_task_config.py:318-321."""
    model = PrinterModel()
    assert model.scan_tag(0, official_tag()) is True
    assert model.scan_tag(0, official_tag(SUB_TYPE="Matte")) is False
    assert model.objects()["print_task_config"]["filament_sub_type"][0] == "Basic"


def test_a_different_official_sku_does_overwrite():
    model = PrinterModel()
    model.scan_tag(0, official_tag())
    assert model.scan_tag(0, official_tag(SKU=99002, SUB_TYPE="Matte")) is True
    assert model.objects()["print_task_config"]["filament_sub_type"][0] == "Matte"


def test_a_non_official_tag_is_ignored_when_the_slot_names_a_vendor():
    """print_task_config.py:314-316."""
    model = PrinterModel()
    model.scan_tag(2, official_tag())
    assert model.scan_tag(2, official_tag(OFFICIAL=False, SKU=0, VENDOR="Other")) is False
    assert model.objects()["print_task_config"]["filament_vendor"][2] == "Snapmaker"


def test_a_non_official_tag_fills_an_empty_slot():
    model = PrinterModel()
    assert model.scan_tag(3, official_tag(OFFICIAL=False, SKU=0, VENDOR="Other")) is True
    assert model.objects()["print_task_config"]["filament_official"][3] is False


def test_filament_color_rgba_is_rebuilt_from_rgb_1_not_from_argb_color():
    """The firmware's own inconsistency at print_task_config.py:325 and :330.

    filament_color is the tag's ARGB_COLOR verbatim while filament_color_rgba is
    rebuilt from RGB_1 plus ALPHA, so a tag whose two colour fields disagree
    lands in the payload disagreeing. A client should prefer the rgba form.
    """
    model = PrinterModel()
    model.scan_tag(0, official_tag(RGB_1=0xAABBCC, ARGB_COLOR=0xFF000000))
    config = model.objects()["print_task_config"]
    assert config["filament_color_rgba"][0] == "AABBCCFF"
    assert config["filament_color"][0] == 0xFF000000


def test_a_tag_field_outside_the_firmware_struct_is_rejected():
    model = PrinterModel()
    with pytest.raises(ValueError):
        model.scan_tag(0, {"NOT_A_TAG_FIELD": 1})


def test_a_main_type_outside_the_firmware_table_is_rejected():
    model = PrinterModel()
    with pytest.raises(ValueError):
        model.scan_tag(0, official_tag(MAIN_TYPE="UNOBTAINIUM"))


def test_clearing_a_slot_resets_the_tag_and_the_sensor():
    model = PrinterModel()
    model.scan_tag(0, official_tag())
    model.clear_slot(0)
    config = model.objects()["print_task_config"]
    assert config["filament_vendor"][0] == "NONE"
    assert config["filament_exist"][0] is False
    assert config["filament_color_rgba"][0] == "FFFFFFFF"
    assert model.objects()["filament_detect"]["info"][0] == FILAMENT_INFO_STRUCT


# ---------------------------------------------------------------- G-code


def test_a_good_script_returns_an_empty_result(client):
    """Moonraker rewrites an empty result to "ok" (klippy_connection.py:615)."""
    assert client.result("gcode/script", {"script": "G28"}) == {}


def test_a_script_without_the_script_parameter_is_an_error(client):
    assert "script" in client.error("gcode/script", {})["message"]


def test_a_bare_t_code_resolves_through_the_colour_map(loaded):
    conn = loaded.client()
    try:
        # Logical colour 5 is mapped onto head 2 by the scenario.
        conn.result("gcode/script", {"script": "T5"})
        status = conn.result(
            "objects/query", {"objects": {"toolhead": ["extruder"], "extruder2": ["state"]}}
        )["status"]
        assert status["toolhead"]["extruder"] == "extruder2"
        assert status["extruder2"]["state"] == "ACTIVATE"
        # A0 bypasses the map, so T1 A0 goes to head 1 whatever the map says.
        conn.result("gcode/script", {"script": "T1 A0"})
        status = conn.result("objects/query", {"objects": {"toolhead": ["extruder"]}})["status"]
        assert status["toolhead"]["extruder"] == "extruder1"
    finally:
        conn.close()


def test_a_t_code_above_the_colour_range_is_refused(client):
    error = client.error("gcode/script", {"script": "T32"})
    assert "colour range" in error["message"]


def test_get_print_extruder_map_reports_all_thirty_two_colours(loaded):
    conn = loaded.client()
    try:
        conn.result(
            "gcode/subscribe_output",
            {"response_template": {"method": "process_gcode_response"}},
        )
        conn.result("gcode/script", {"script": "GET_PRINT_EXTRUDER_MAP"})
        # The response lines are queued before the reply to the request, so the
        # request helper has already parked them in pushes.
        documents = conn.pushes + conn.drain(0.5)
        lines = [
            doc["params"]["response"]
            for doc in documents
            if doc.get("method") == "process_gcode_response"
        ]
        assert "// T0 -> T0" in lines
        assert "// T4 -> T1" in lines
        assert "// T5 -> T2" in lines
        assert len([line for line in lines if line.startswith("// T")]) == 32
    finally:
        conn.close()


def test_set_print_extruder_map_works_while_idle(client):
    client.result(
        "gcode/script", {"script": "SET_PRINT_EXTRUDER_MAP CONFIG_EXTRUDER=9 MAP_EXTRUDER=3"}
    )
    config = client.result(
        "objects/query", {"objects": {"print_task_config": ["extruder_map_table", "reprint_info"]}}
    )["status"]["print_task_config"]
    assert config["extruder_map_table"][9] == 3
    assert config["reprint_info"]["extruder_map_table"][9] == 3


@pytest.mark.parametrize(
    "script",
    [
        "SET_PRINT_EXTRUDER_MAP CONFIG_EXTRUDER=32 MAP_EXTRUDER=0",
        "SET_PRINT_EXTRUDER_MAP CONFIG_EXTRUDER=0 MAP_EXTRUDER=4",
        "SET_PRINT_EXTRUDER_MAP CONFIG_EXTRUDER=-1 MAP_EXTRUDER=0",
    ],
)
def test_set_print_extruder_map_rejects_an_index_out_of_range(client, script):
    error = client.error("gcode/script", {"script": script})
    assert error["message"] == "[print_task_config] extruder map, invalid extruder index!!!"


def test_set_print_extruder_map_is_refused_while_printing(client):
    client.result("gcode/script", {"script": 'SDCARD_PRINT_FILE FILENAME="demo.gcode"'})
    error = client.error(
        "gcode/script", {"script": "SET_PRINT_EXTRUDER_MAP CONFIG_EXTRUDER=1 MAP_EXTRUDER=0"}
    )
    assert error["message"] == (
        "[print_task_config] not allowed to set extruder map during printing!"
    )


def test_starting_a_print_over_gcode_moves_the_job_state(client):
    client.result(
        "gcode/script",
        {"script": 'SDCARD_PRINT_FILE FILENAME="/sub dir/demo.gcode" U1SIM_DURATION=50'},
    )
    status = client.result(
        "objects/query", {"objects": {"print_stats": None, "virtual_sdcard": None}}
    )["status"]
    assert status["print_stats"]["state"] == "printing"
    # virtual_sdcard.py:339-341 strips the leading slash.
    assert status["print_stats"]["filename"] == "sub dir/demo.gcode"
    assert status["virtual_sdcard"]["is_active"] is True
    assert status["virtual_sdcard"]["file_path"].endswith("sub dir/demo.gcode")


def test_a_second_print_is_refused_while_the_card_is_busy(client):
    client.result("gcode/script", {"script": 'SDCARD_PRINT_FILE FILENAME="a.gcode"'})
    assert (
        client.error("gcode/script", {"script": 'SDCARD_PRINT_FILE FILENAME="b.gcode"'})["message"]
        == "SD busy"
    )


def test_set_print_filament_config_refuses_an_official_slot_without_force(loaded):
    conn = loaded.client()
    try:
        error = conn.error(
            "gcode/script",
            {"script": "SET_PRINT_FILAMENT_CONFIG CONFIG_EXTRUDER=0 FILAMENT_COLOR_RGBA=00FF00FF"},
        )
        assert error["message"] == (
            "[print_task_config] filament_config, official filament, not configurable!"
        )
        conn.result(
            "gcode/script",
            {
                "script": "SET_PRINT_FILAMENT_CONFIG CONFIG_EXTRUDER=0 "
                "FILAMENT_COLOR_RGBA=00FF00FF FORCE=1"
            },
        )
        config = conn.result("objects/query", {"objects": {"print_task_config": None}})["status"][
            "print_task_config"
        ]
        assert config["filament_color_rgba"][0] == "00FF00FF"
        # A manual write clears the official flag and the SKU (:676-677).
        assert config["filament_official"][0] is False
        assert config["filament_sku"][0] == 0
        assert_print_task_config_is_well_formed(config)
    finally:
        conn.close()


def test_set_print_filament_config_needs_vendor_type_and_sub_type_together(client):
    error = client.error(
        "gcode/script",
        {"script": "SET_PRINT_FILAMENT_CONFIG CONFIG_EXTRUDER=1 FILAMENT_TYPE=PLA"},
    )
    assert error["message"] == "[print_task_config] filament_config, incomplete parameters"


def test_a_multi_colour_manual_write_lands_in_the_payload(client):
    client.result(
        "gcode/script",
        {
            "script": "SET_PRINT_FILAMENT_CONFIG CONFIG_EXTRUDER=3 VENDOR=Generic "
            "FILAMENT_TYPE=PLA FILAMENT_SUBTYPE=Silk COLOR_NUMS=2 "
            "COLORS=1E88E5,43A047 MULTI_MODE=1 ALPHA=255"
        },
    )
    config = client.result("objects/query", {"objects": {"print_task_config": None}})["status"][
        "print_task_config"
    ]
    assert config["filament_color_multi"][3] == {
        "nums": 2,
        "alpha": 255,
        "mode": 1,
        "colors": ["1E88E5", "43A047"],
    }
    assert config["filament_color_rgba"][3] == "1E88E5FF"
    assert config["filament_color"][3] == 4280191205
    assert_print_task_config_is_well_formed(config)


def test_a_colour_list_that_does_not_match_the_count_is_refused(client):
    error = client.error(
        "gcode/script",
        {"script": "SET_PRINT_FILAMENT_CONFIG CONFIG_EXTRUDER=3 COLOR_NUMS=3 COLORS=1E88E5"},
    )
    assert error["message"] == "[print_task_config] filament_config, colors error"


# ------------------------------------------------------------- preferences


def test_set_print_preferences_answers_success(client):
    result = client.result(
        "print_task_config/set_print_preferences",
        {
            "auto_replenish_filament": 0,
            "filament_entangle_detect": 1,
            "replenish_ignore_color": 1,
            "end_led_turn_off": 1,
            "filament_entangle_sen": "low",
        },
    )
    assert result == {"state": "success"}
    config = client.result("objects/query", {"objects": {"print_task_config": None}})["status"][
        "print_task_config"
    ]
    assert config["auto_replenish_filament"] is False
    assert config["filament_entangle_detect"] is True
    assert config["replenish_ignore_color"] is True
    assert config["end_led_turn_off"] is True
    assert config["filament_entangle_sen"] == "low"


def test_a_bad_sensitivity_answers_error_not_an_exception(client):
    """print_task_config.py:181-186 always answers 200 shaped, so a client has
    to read the state member rather than trust the HTTP code."""
    result = client.result(
        "print_task_config/set_print_preferences", {"filament_entangle_sen": "very high"}
    )
    assert result["state"] == "error"
    assert "filament_entangle_sen" in result["message"]


def test_preferences_left_out_are_untouched(client):
    before = client.result("objects/query", {"objects": {"print_task_config": None}})["status"][
        "print_task_config"
    ]
    client.result("print_task_config/set_print_preferences", {"end_led_turn_off": 1})
    after = client.result("objects/query", {"objects": {"print_task_config": None}})["status"][
        "print_task_config"
    ]
    assert after["end_led_turn_off"] is True
    for key in ("auto_replenish_filament", "filament_entangle_sen", "replenish_ignore_color"):
        assert after[key] == before[key]


# ------------------------------------------------------- control endpoints


def test_bed_temp_and_extruder_temp(loaded):
    conn = loaded.client()
    try:
        assert conn.result("control/bed_temp", {"S": 65.0}) == {"state": "success"}
        # T is a logical colour. Colour 5 is mapped onto head 2.
        assert conn.result("control/extruder_temp", {"S": 250.0, "T": 5}) == {"state": "success"}
        status = conn.result(
            "objects/query",
            {"objects": {"heater_bed": ["target"], "extruder2": ["target"]}},
        )["status"]
        assert status["heater_bed"]["target"] == 65.0
        assert status["extruder2"]["target"] == 250.0
    finally:
        conn.close()


def test_extruder_temp_with_a_zero_a_bypasses_the_map(loaded):
    conn = loaded.client()
    try:
        conn.result("control/extruder_temp", {"S": 200.0, "T": 3, "A": 0})
        status = conn.result("objects/query", {"objects": {"extruder3": ["target"]}})["status"]
        assert status["extruder3"]["target"] == 200.0
    finally:
        conn.close()


def test_main_fan_takes_a_percent(client):
    assert client.result("control/main_fan", {"S": 60.0}) == {"state": "success"}
    status = client.result("objects/query", {"objects": {"fan": None}})["status"]
    assert status["fan"]["speed"] == pytest.approx(0.6)


def test_main_fan_clamps_out_of_range_values(client):
    client.result("control/main_fan", {"S": 400.0})
    assert (
        client.result("objects/query", {"objects": {"fan": ["speed"]}})["status"]["fan"]["speed"]
        == 1.0
    )
    client.result("control/main_fan", {"S": -50.0})
    assert (
        client.result("objects/query", {"objects": {"fan": ["speed"]}})["status"]["fan"]["speed"]
        == 0.0
    )


def test_print_speed_sets_the_speed_factor(client):
    client.result("control/print_speed", {"S": 150})
    status = client.result("objects/query", {"objects": {"gcode_move": None}})["status"]
    assert status["gcode_move"]["speed_factor"] == pytest.approx(1.5)


def test_led_is_a_mux_endpoint_keyed_on_led(client):
    assert client.result("control/led", {"led": "cavity_led", "white": 1.0}) == {"state": "success"}
    status = client.result("objects/query", {"objects": {"led cavity_led": None}})["status"]
    assert status["led cavity_led"]["color_data"] == [[0.0, 0.0, 0.0, 1.0]]
    assert "not valid for led" in client.error("control/led", {"led": "no_such_led"})["message"]


def test_nozzle_diameter_can_be_read_and_written(client):
    assert client.result("control/nozzle_diameter", {"T": 2})["diameter"] == 0.4
    client.result("control/nozzle_diameter", {"T": 2, "D": 0.6})
    status = client.result("objects/query", {"objects": {"extruder2": ["nozzle_diameter"]}})
    assert status["status"]["extruder2"]["nozzle_diameter"] == 0.6


def test_query_endstops_answers(client):
    assert set(client.result("query_endstops/status")["last_query"]) == {"x", "y", "z"}


# ------------------------------------------------------- pause and resume


def test_pause_resume_and_cancel_move_the_job_state(client):
    client.result("gcode/script", {"script": 'SDCARD_PRINT_FILE FILENAME="job.gcode"'})

    def job_state():
        status = client.result(
            "objects/query",
            {"objects": {"print_stats": ["state"], "pause_resume": None}},
        )["status"]
        return status["print_stats"]["state"], status["pause_resume"]["is_paused"]

    assert job_state() == ("printing", False)
    assert client.result("pause_resume/pause") == {}
    assert job_state() == ("paused", True)
    assert client.result("pause_resume/resume") == {}
    assert job_state() == ("printing", False)
    assert client.result("pause_resume/cancel") == {}
    assert job_state() == ("cancelled", False)


def test_a_restart_puts_the_printer_back_to_a_clean_ready_state(client):
    client.result("gcode/script", {"script": 'SDCARD_PRINT_FILE FILENAME="job.gcode"'})
    client.result("gcode/restart")
    status = client.result(
        "objects/query",
        {"objects": {"print_stats": None, "webhooks": None, "print_task_config": None}},
    )["status"]
    assert status["webhooks"]["state"] == "ready"
    assert status["print_stats"]["state"] == "standby"
    assert status["print_task_config"]["filament_vendor"] == ["NONE"] * 4


# ------------------------------------------------------ other object shapes


def test_print_stats_has_the_firmware_key_set(client):
    stats = client.result("objects/query", {"objects": {"print_stats": None}})["status"][
        "print_stats"
    ]
    assert set(stats) == {
        "filename",
        "total_duration",
        "print_duration",
        "filament_used",
        "state",
        "exception",
        "message",
        "info",
    }
    assert set(stats["info"]) == {"total_layer", "current_layer"}
    assert stats["state"] in (
        "standby",
        "printing",
        "paused",
        "complete",
        "error",
        "cancelled",
    )


def test_virtual_sdcard_has_the_firmware_key_set(client):
    card = client.result("objects/query", {"objects": {"virtual_sdcard": None}})["status"][
        "virtual_sdcard"
    ]
    assert set(card) == {
        "file_path",
        "progress",
        "is_active",
        "file_position",
        "file_size",
        "pl_env_valid",
    }
    assert 0.0 <= card["progress"] <= 1.0


def test_the_extruder_objects_carry_the_heater_plus_the_park_detector(client):
    status = client.result(
        "objects/query",
        {"objects": {"extruder": None, "extruder1": None, "heater_bed": None}},
    )["status"]
    for name, index in (("extruder", 0), ("extruder1", 1)):
        fields = status[name]
        assert {"temperature", "target", "power"} <= set(fields)
        assert fields["extruder_index"] == index
        assert fields["nozzle_diameter"] in (0.2, 0.4, 0.6, 0.8)
        assert fields["state"] in ("PARKED", "ACTIVATE", "UNKNOWN")
        # power is a PWM duty, not a percent.
        assert 0.0 <= fields["power"] <= 1.0
    assert set(status["heater_bed"]) == {"temperature", "target", "power"}
    # The switch counters come from an object this fork does not ship.
    assert "switch_count" not in status["extruder"]


def test_head_zero_is_active_and_the_others_are_parked(client):
    status = client.result(
        "objects/query",
        {
            "objects": {
                name: ["state"] for name in ("extruder", "extruder1", "extruder2", "extruder3")
            }
        },
    )["status"]
    assert status["extruder"]["state"] == "ACTIVATE"
    assert [status[f"extruder{i}"]["state"] for i in (1, 2, 3)] == ["PARKED"] * 3


def test_heaters_lists_every_sensor_the_simulator_can_serve(client):
    heaters = client.result("objects/query", {"objects": {"heaters": None}})["status"]["heaters"]
    objects = set(client.result("objects/list")["objects"])
    for name in heaters["available_sensors"] + heaters["available_monitors"]:
        assert name in objects, f"{name} is advertised but cannot be queried"


def test_the_filament_feed_modules_key_on_numeric_head_names(client):
    status = client.result(
        "objects/query",
        {"objects": {"filament_feed left": None, "filament_feed right": None}},
    )["status"]
    assert set(status["filament_feed left"]) == {"extruder0", "extruder1"}
    assert set(status["filament_feed right"]) == {"extruder2", "extruder3"}
    assert set(status["filament_feed left"]["extruder0"]) == {
        "module_exist",
        "filament_detected",
        "disable_auto",
        "channel_state",
        "channel_error",
        "channel_error_state",
        "channel_action_state",
    }


def test_machine_state_manager_publishes_integers_not_names(client):
    """MachineMainState and ActionCode are IntEnum members
    (machine_state_manager.py:9-24, :29-33), so they serialise as ints."""
    status = client.result("objects/query", {"objects": {"machine_state_manager": None}})["status"][
        "machine_state_manager"
    ]
    assert set(status) == {"main_state", "action_code"}
    assert isinstance(status["main_state"], int)
    assert isinstance(status["action_code"], int)


def test_exception_manager_starts_empty(client):
    status = client.result("objects/query", {"objects": {"exception_manager": None}})
    assert status["status"]["exception_manager"] == {"exceptions": []}


# --------------------------------------------------------------- scenarios


FOUR_COLOUR = "four_color_print"


def run_scenario(name: str, seconds: float, step: float = 0.25):
    """Drive a scenario off a supplied clock so the result is deterministic."""
    model = PrinterModel()
    scenario = Scenario.load(name)
    scenario.loop = False
    runner = ScenarioRunner(model, scenario)
    for _ in range(int(seconds / step)):
        runner.advance(step)
    return model, runner


def step_at(name: str, action: str, **fields) -> float:
    """The scenario time of the first step matching an action and its fields.

    A test asks the file when something happens rather than repeating a second
    count, so retiming a scenario cannot leave a test asserting against a moment
    the timeline no longer has.
    """
    for step in Scenario.load(name).steps:
        if step["action"] == action and all(
            step.get(key) == value for key, value in fields.items()
        ):
            return float(step["at"])
    raise AssertionError(f"{name} has no {action} step matching {fields}")


def test_the_bundled_scenarios_load_and_validate():
    names = available_scenarios()
    assert "four_color_print" in names
    assert "idle_loaded" in names
    for name in names:
        scenario = Scenario.load(name)
        ScenarioRunner(PrinterModel(), scenario)
        assert scenario.steps


def test_a_scenario_with_an_unknown_action_is_rejected():
    scenario = Scenario({"steps": [{"at": 0, "action": "make_coffee"}]})
    with pytest.raises(ScenarioError):
        ScenarioRunner(PrinterModel(), scenario)


def test_a_scenario_that_goes_backwards_in_time_is_rejected():
    with pytest.raises(ScenarioError):
        Scenario({"steps": [{"at": 5, "action": "pause"}, {"at": 1, "action": "resume"}]})


def test_a_scenario_with_no_steps_is_rejected():
    with pytest.raises(ScenarioError):
        Scenario({"steps": []})


def test_the_four_colour_scenario_loads_four_slots_before_it_prints():
    # Slot 3 is the last one filled and it is written by hand, not scanned.
    model, runner = run_scenario(FOUR_COLOUR, step_at(FOUR_COLOUR, "set_filament", slot=3) + 1.0)
    config = model.objects()["print_task_config"]
    assert_print_task_config_is_well_formed(config)
    assert config["filament_exist"] == [True] * 4
    assert config["filament_official"] == [True, True, True, False]
    assert model.print_state == "standby"
    assert runner.applied.count("scan_tag") == 3
    assert "set_filament" in runner.applied


def test_the_four_colour_scenario_advances_the_print():
    model, _runner = run_scenario(FOUR_COLOUR, step_at(FOUR_COLOUR, "start_print") + 45.0)
    stats = model.objects()["print_stats"]
    assert stats["state"] == "printing"
    assert stats["filename"] == "u1-four-color-demo.gcode"
    assert stats["print_duration"] > 40.0
    assert stats["filament_used"] > 0.0
    assert 0 < stats["info"]["current_layer"] <= stats["info"]["total_layer"]
    card = model.objects()["virtual_sdcard"]
    assert 0.0 < card["progress"] < 1.0
    assert card["file_position"] > 0


def test_tool_changes_accumulate_usage_against_each_logical_colour():
    model, _runner = run_scenario(FOUR_COLOUR, step_at(FOUR_COLOUR, "tool_change", logical=5) + 1.5)
    usage = {entry["logical_extruder"]: entry for entry in model.usage_report()}
    # Six colours have had time on the nozzle by then.
    assert set(usage) == {0, 1, 2, 3, 4, 5}
    assert usage[4]["head"] == 1
    assert usage[5]["head"] == 2
    assert usage[2]["filament_type"] == "PETG"
    for entry in usage.values():
        assert entry["used_mm"] > 0.0
        assert entry["used_g"] > 0.0
    total = sum(entry["used_mm"] for entry in usage.values())
    assert total == pytest.approx(model.filament_used, rel=1e-6)
    assert model.tool_changes >= 4


def test_the_scenario_pauses_then_resumes():
    model, _runner = run_scenario(FOUR_COLOUR, step_at(FOUR_COLOUR, "pause") + 2.0)
    assert model.objects()["print_stats"]["state"] == "paused"
    assert model.objects()["pause_resume"]["is_paused"] is True
    model, _runner = run_scenario(FOUR_COLOUR, step_at(FOUR_COLOUR, "resume") + 2.0)
    assert model.objects()["print_stats"]["state"] == "printing"


def test_a_paused_print_stops_consuming_filament():
    model, runner = run_scenario(FOUR_COLOUR, step_at(FOUR_COLOUR, "pause") + 1.0)
    used_at_pause = model.filament_used
    for _ in range(40):
        runner.advance(0.25)
    assert model.filament_used == pytest.approx(used_at_pause)
    # total_duration keeps running while paused, print_duration does not.
    assert model.total_duration > model.print_duration


def test_the_colour_map_change_is_refused_mid_print():
    # The scenario attempts a remap while the job is running, which the firmware
    # refuses (print_task_config.py:511-519).
    model, _runner = run_scenario(FOUR_COLOUR, step_at(FOUR_COLOUR, "gcode") + 1.0)
    assert any("refused" in line for line in model.gcode_log)
    assert model.objects()["print_task_config"]["extruder_map_table"][6] == 0


def test_the_scenario_finishes_with_a_complete_print():
    model, runner = run_scenario(FOUR_COLOUR, step_at(FOUR_COLOUR, "complete") + 5.0)
    stats = model.objects()["print_stats"]
    assert stats["state"] == "complete"
    assert model.objects()["virtual_sdcard"]["progress"] == 1.0
    assert stats["info"]["current_layer"] == stats["info"]["total_layer"]
    assert model.filament_used == pytest.approx(8600.0, rel=1e-3)
    assert runner.cursor == len(runner.scenario.steps)


def test_a_looping_scenario_starts_over():
    model = PrinterModel()
    scenario = Scenario.load(FOUR_COLOUR)
    scenario.loop = True
    scenario.loop_gap = 2.0
    runner = ScenarioRunner(model, scenario)
    for _ in range(int((scenario.duration + scenario.loop_gap + 5.0) / 0.25)):
        runner.advance(0.25)
    assert runner.laps >= 1
    # A fresh lap starts from a clean printer.
    assert runner.elapsed < scenario.duration


def test_the_debug_endpoint_reports_the_scenario_over_the_socket(tmp_path):
    harness = make_harness(tmp_path, "four_color_print", speed=60.0)
    try:
        conn = harness.client()
        try:
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                debug = conn.result("u1sim/debug")
                if debug["print_state"] == "printing" and debug["per_colour_usage"]:
                    break
                time.sleep(0.05)
            assert debug["print_state"] == "printing"
            assert debug["per_colour_usage"]
            assert debug["runner"]["scenario"]["name"] == "four_color_print"
            assert debug["runner"]["steps_applied"] > 10
            assert "no firmware equivalent" in debug["note"]
        finally:
            conn.close()
    finally:
        harness.stop()


# ----------------------------------------------------------------- plumbing


def test_a_stale_socket_file_is_replaced(tmp_path):
    path = tmp_path / "klippy.sock"
    path.write_text("not a socket")
    server = U1SimServer(socket_path=str(path))
    server.bind()
    try:
        assert os.path.exists(str(path))
        conn = RawClient(str(path))
        conn.close()
    finally:
        server.close()
    assert not os.path.exists(str(path))


def test_the_socket_is_readable_and_writable(tmp_path):
    """Moonraker skips a socket it cannot open R+W (klippy_connection.py:299)."""
    server = U1SimServer(socket_path=str(tmp_path / "klippy.sock"))
    server.bind()
    try:
        assert os.access(server.socket_path, os.R_OK | os.W_OK)
    finally:
        server.close()


def test_only_the_four_klippy_states_are_accepted():
    model = PrinterModel()
    for state in ("ready", "startup", "shutdown", "error"):
        model.set_klippy_state(state)
        assert model.klippy_state == state
    with pytest.raises(ValueError):
        model.set_klippy_state("almost ready")


def test_a_state_change_shows_up_on_the_webhooks_object(bare, client):
    bare.model.set_klippy_state("startup")
    status = client.result("objects/query", {"objects": {"webhooks": None}})["status"]
    assert status["webhooks"] == {
        "state": "startup",
        "state_message": "Printer is not ready",
    }
    assert client.result("info")["state"] == "startup"


def test_eventtime_moves_forward_and_is_not_a_unix_timestamp(client):
    first = client.result("objects/query", {"objects": {"webhooks": None}})["eventtime"]
    time.sleep(0.3)
    second = client.result("objects/query", {"objects": {"webhooks": None}})["eventtime"]
    assert second > first
    assert second < time.time() / 2


def test_the_only_endpoint_the_firmware_does_not_have_is_namespaced():
    extras = [name for name in ENDPOINT_NAMES if name.startswith("u1sim/")]
    assert extras == ["u1sim/debug"]


def test_the_cli_can_list_the_bundled_scenarios(capsys):
    from u1sim.__main__ import main

    assert main(["--list-scenarios"]) == 0
    printed = capsys.readouterr().out.split()
    assert "four_color_print" in printed


def test_the_cli_refuses_an_unknown_scenario(capsys):
    from u1sim.__main__ import main

    assert main(["--scenario", "does_not_exist", "--socket", "/tmp/u1sim-unused.sock"]) == 2
    assert "known scenarios" in capsys.readouterr().err
