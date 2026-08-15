"""The parsing layer, driven by bytes that came through a real Moonraker.

Everything under artifacts/real-moonraker/ was captured by
scripts/prove-real-moonraker.sh: the unmodified Snapmaker Moonraker fork running
on top of u1sim, with no printer. Nothing in that directory is hand written.

These tests are the reason the capture exists. The other suite feeds the parsing
layer fixtures we wrote ourselves, which can only ever confirm our own reading of
the firmware. This one feeds it what the real server actually sent, so a wrong
assumption shows up as a failure rather than as a fixture that agrees with the
bug. That already caught one: machine_state_manager.main_state arrives as an int,
not the name the hand written fixture used.

If the capture is missing, these tests fail. They do not skip. A skipped proof
is the same as no proof.
"""

from __future__ import annotations

import importlib
import importlib.machinery
import json
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
CAPTURE = REPO / "artifacts" / "real-moonraker"
COMPONENT_DIR = REPO / "custom_components" / "snapmaker_u1"
PACKAGE = "u1_under_test"

CAPTURE_FILES = (
    "server-info.json",
    "printer-info.json",
    "objects-list.json",
    "query-print_task_config.json",
    "query-filament_detect.json",
    "query-wanted-objects.json",
    "query-wanted-objects-final.json",
    "query-after-gcode.json",
    "post-gcode-accepted.json",
    "post-gcode-refused.json",
    "ws-subscribe-reply.json",
    "ws-status-updates.json",
)


def _load_package() -> None:
    """Same trick the other suite uses: import the Home Assistant free modules."""
    if PACKAGE in sys.modules:
        return
    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(COMPONENT_DIR)]
    package.__spec__ = importlib.machinery.ModuleSpec(PACKAGE, None, is_package=True)
    package.__spec__.submodule_search_locations = [str(COMPONENT_DIR)]
    sys.modules[PACKAGE] = package


_load_package()

api = importlib.import_module(f"{PACKAGE}.api")
const = importlib.import_module(f"{PACKAGE}.const")
parsing = importlib.import_module(f"{PACKAGE}.parsing")


def load(name: str):
    path = CAPTURE / name
    if not path.exists():
        raise AssertionError(
            f"{path} is missing. The capture is part of the repository. "
            "Regenerate it with scripts/prove-real-moonraker.sh, which runs the "
            "real Moonraker fork against u1sim and writes every file it returns."
        )
    return json.loads(path.read_text(encoding="utf-8"))


# Every fixture re-reads its file. A shared parsed payload would let one test
# see another test's merge, which is how the aliasing bug in apply_snapshot was
# found in the first place.
@pytest.fixture
def server_info():
    return load("server-info.json")["result"]


@pytest.fixture
def early():
    """The full objects/query taken before the simulated print started."""
    return load("query-wanted-objects.json")["result"]


@pytest.fixture
def final():
    """The same query taken after the websocket window closed."""
    return load("query-wanted-objects-final.json")["result"]


@pytest.fixture
def frames():
    return load("ws-status-updates.json")


@pytest.fixture
def merged(early, frames):
    """Early snapshot plus every push, merged the way the coordinator does."""
    state = parsing.U1State()
    status, eventtime = api.parse_subscribe_result(early)
    state.apply_snapshot(status, eventtime)
    state.printer_info = load("printer-info.json")["result"]
    state.set_objects(load("objects-list.json")["result"]["objects"])
    for frame in frames:
        update, moment = api.parse_status_notification(frame["params"])
        state.apply_update(update, moment)
    return state


# The capture itself ------------------------------------------------------


def test_every_captured_file_is_present():
    missing = [name for name in CAPTURE_FILES if not (CAPTURE / name).exists()]
    assert missing == [], f"regenerate the capture with scripts/prove-real-moonraker.sh: {missing}"


def test_moonraker_was_connected_to_the_simulator_and_had_nothing_to_complain_about(server_info):
    assert server_info["klippy_connected"] is True
    assert server_info["klippy_state"] == "ready"
    assert server_info["failed_components"] == []
    assert server_info["warnings"] == []
    assert server_info["missing_klippy_requirements"] == []


def test_the_snapmaker_specific_moonraker_components_were_all_running(server_info):
    """These four only exist in the Snapmaker fork, so the capture is from it."""
    for name in ("snapmakercloud", "exception_manager", "client_manager", "repeater"):
        assert name in server_info["components"], name


def test_moonraker_listed_every_object_the_integration_reads():
    objects = load("objects-list.json")["result"]["objects"]
    for name in const.WANTED_OBJECTS:
        assert name in objects, name


def test_the_pushes_were_partial_updates_not_snapshots(frames, early):
    """A push carries only what changed (klippy/webhooks.py:533-538).

    If any push had been a full snapshot, merging would be pointless and the
    merge test below would prove nothing.
    """
    assert frames, "the capture recorded no pushes"
    full = set(early["status"])
    assert all(set(frame["params"][0]) != full for frame in frames)
    assert any(len(frame["params"][0]) < len(full) for frame in frames)


def test_replacing_instead_of_merging_would_lose_the_slot_data(frames, early):
    """The failure mode merge_status exists to prevent, shown on real bytes."""
    replaced = parsing.U1State()
    replaced.apply_snapshot(early["status"], early["eventtime"])
    for frame in frames:
        replaced.apply_snapshot(frame["params"][0], frame["params"][1])
    assert "filament_vendor" not in replaced.print_task_config
    assert all(slot.vendor is None for slot in replaced.slots())


def test_applying_a_delta_does_not_write_back_into_the_captured_payload(early, frames):
    """apply_snapshot copies, so merging cannot reach the caller's response body."""
    payload = early["status"]
    before = json.dumps(payload["print_task_config"], sort_keys=True)
    state = parsing.U1State()
    state.apply_snapshot(payload, early["eventtime"])
    for frame in frames:
        state.apply_update(frame["params"][0], frame["params"][1])
    assert json.dumps(payload["print_task_config"], sort_keys=True) == before
    assert state.slot(0).vendor == "Snapmaker"


# Merging the pushes reproduces a full query ------------------------------


def test_merged_pushes_match_the_full_query_taken_afterwards(merged, final):
    """The strongest check here.

    print_task_config and filament_detect only change on an event, so once the
    last push has been merged they have to equal what a fresh HTTP query returns.
    Anything the merge dropped, duplicated or shadowed shows up as a difference.
    """
    for name in (const.OBJ_PRINT_TASK_CONFIG, const.OBJ_FILAMENT_DETECT):
        assert merged.status[name] == final["status"][name], name


# The four slots, decoded from the real payload ---------------------------


def test_the_early_snapshot_has_four_empty_slots(early):
    """Before any spool was scanned the firmware default is "NONE" and FFFFFFFF."""
    state = parsing.U1State()
    state.apply_snapshot(early["status"], early["eventtime"])
    config = state.print_task_config
    assert config["filament_vendor"] == ["NONE"] * const.PHYSICAL_EXTRUDER_NUM
    assert config["filament_color_rgba"] == [const.DEFAULT_COLOR_RGBA] * 4
    for slot in state.slots():
        assert slot.loaded is False
        assert slot.vendor is None
        assert slot.filament_type is None
        # The default white is not reported as a colour, because no spool is
        # there to have one.
        assert slot.color is None
        assert slot.colors == []


def test_the_four_slots_decode_to_what_the_printer_sent(merged):
    slots = merged.slots()
    assert [slot.vendor for slot in slots] == ["Snapmaker", "Snapmaker", "Snapmaker", "Generic"]
    assert [slot.filament_type for slot in slots] == ["PLA", "PLA", "PETG", "PLA"]
    assert [slot.sub_type for slot in slots] == ["Basic", "Matte", "HF", "Silk"]
    assert [slot.color for slot in slots] == ["#000000", "#F5F0E1", "#D3232A", "#1E88E5"]
    assert [slot.official for slot in slots] == [True, True, True, False]
    assert [slot.sku for slot in slots] == [12001, 12042, 13007, None]
    assert [slot.present for slot in slots] == [True] * 4
    assert all(slot.loaded for slot in slots)


def test_the_hand_entered_slot_keeps_both_of_its_colours(merged):
    """Slot 3 was written by G-code, not scanned, with a two colour silk."""
    slot = merged.slot(3)
    assert slot.colors == ["#1E88E5", "#43A047"]
    assert slot.color_count == 2
    assert slot.color_mode == 1
    assert slot.is_gradient is True
    assert slot.user_editable is True
    assert slot.official is False


def test_the_packed_argb_int_agrees_with_the_rgba_string_on_every_slot(merged):
    """On this capture the two never disagreed, so no slot reports a mismatch."""
    for slot in merged.slots():
        assert slot.color_mismatch is False
        assert slot.argb_color == slot.color


# The colour map ----------------------------------------------------------


def test_the_colour_map_carries_the_remap_sent_over_http(merged):
    """The capture posted SET_PRINT_EXTRUDER_MAP CONFIG_EXTRUDER=7 MAP_EXTRUDER=2."""
    table = merged.color_map
    assert len(table) == const.LOGICAL_EXTRUDER_NUM
    assert table[:8] == [0, 1, 2, 3, 1, 2, 0, 2]
    assert merged.slot(2).assigned_colors == [2, 5, 7]
    assert merged.slot(1).assigned_colors == [1, 4]
    assert merged.slot(3).assigned_colors == [3]


def test_the_remap_was_visible_in_printer_state_straight_after_the_post():
    after = load("query-after-gcode.json")["result"]["status"]["print_task_config"]
    assert after["extruder_map_table"][7] == 2


# The write path ----------------------------------------------------------


def test_moonraker_accepted_the_colour_remap():
    accepted = load("post-gcode-accepted.json")
    assert accepted["http_status"] == 200
    # Moonraker rewrites an empty Klippy result to the string "ok"
    # (klippy_connection.py:615-619).
    assert accepted["body"]["result"] == "ok"


def test_moonraker_turned_the_firmware_refusal_into_a_400_with_its_own_message():
    refused = load("post-gcode-refused.json")
    assert refused["http_status"] == 400
    message = api._error_message(refused["body"], "fallback")
    assert message == "[print_task_config] extruder map, invalid extruder index!!!"


# RFID --------------------------------------------------------------------


def test_the_rfid_tag_on_slot_zero_decodes_field_for_field(merged):
    tag = merged.tag(0)
    assert tag is not None
    assert tag.vendor == "Snapmaker"
    assert tag.manufacturer == "Snapmaker"
    assert tag.main_type == "PLA"
    assert tag.sub_type == "Basic"
    assert tag.official is True
    assert tag.sku == 12001
    assert tag.weight_g == 1000
    assert tag.drying_temp == 55
    assert tag.hotend_min_temp == 190
    assert tag.hotend_max_temp == 240
    assert tag.first_layer_temp == 215
    assert tag.bed_temp == 60
    assert tag.mf_date == "20260114"
    assert tag.card_uid == [4, 210, 17, 32]
    assert tag.color == "#000000"
    assert tag.colors == ["#000000"]


def test_the_slot_written_by_hand_has_no_tag(merged):
    """Slot 3 never saw a spool, so its channel keeps the struct defaults."""
    assert merged.tag(3) is None
    assert merged.slot(3).vendor == "Generic"


def test_every_scan_channel_reports_a_known_state(merged):
    states = [merged.scan_state(index) for index in range(const.PHYSICAL_EXTRUDER_NUM)]
    assert states == ["idle"] * const.PHYSICAL_EXTRUDER_NUM
    assert set(const.SCAN_STATES.values()) >= set(states)


def test_a_scan_in_progress_was_pushed_before_each_tag_arrived(frames):
    """filament_detect.state went to 1 (detecting) on the way to each reading."""
    seen = [
        frame["params"][0]["filament_detect"]["state"]
        for frame in frames
        if "state" in frame["params"][0].get("filament_detect", {})
    ]
    assert seen, "no scan state change was pushed"
    assert any(1 in state for state in seen)


# Job and machine state ---------------------------------------------------


def test_the_job_reads_through_from_the_real_payload(merged):
    assert merged.print_state == "printing"
    assert merged.filename == "u1-four-color-demo.gcode"
    assert merged.writes_blocked is True
    assert merged.is_paused is False
    assert 0.0 < merged.progress_percent <= 100.0
    assert merged.current_layer is not None
    assert merged.total_layer == 240
    assert merged.filament_used_mm > 0
    assert merged.active_tool == 0
    assert merged.active_extruder_object == "extruder"


def test_machine_state_comes_through_as_a_number_and_is_decoded(merged, early):
    """The bug the capture found.

    main_state is a MachineMainState IntEnum inside Klippy
    (machine_state_manager.py:9-27) and get_status returns the member itself
    (:322-326), so JSON gives a client the number. A hand written fixture had it
    as the string "printing", which no printer ever sends.
    """
    assert early["status"]["machine_state_manager"]["main_state"] == 0
    raw = merged.status["machine_state_manager"]["main_state"]
    assert isinstance(raw, int)
    assert merged.machine_state == "printing"
    assert merged.action_name == const.ACTION_CODES[merged.action_code]


def test_klippy_reports_itself_ready_and_names_itself(merged):
    assert merged.klippy_state == "ready"
    assert merged.hostname == "u1sim"
    assert merged.available is True


def test_the_bed_and_all_four_heads_report_temperatures(merged):
    assert merged.bed_temperature is not None
    assert merged.bed_target == 60.0
    for head in range(const.PHYSICAL_EXTRUDER_NUM):
        assert merged.head_temperature(head) is not None, head
        assert merged.dock_state(head) in const.DOCK_STATES, head


# The subscribe reply -----------------------------------------------------


def test_the_subscribe_reply_parses_the_same_way_a_query_does():
    reply = load("ws-subscribe-reply.json")
    assert reply["jsonrpc"] == "2.0"
    status, eventtime = api.parse_subscribe_result(reply["result"])
    assert isinstance(eventtime, float)
    for name in const.WANTED_OBJECTS:
        assert name in status, name
