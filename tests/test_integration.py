"""Tests for the Snapmaker U1 payload parsing.

These run anywhere. Home Assistant is not imported and no printer is needed:
the modules under test are the ones with no Home Assistant imports, loaded
straight off disk. The payloads are the real shapes the U1 firmware returns.

Every fixture value below is either copied from the firmware defaults or built
to satisfy the checks the firmware itself enforces on print_task_config
(klippy/extras/print_task_config.py:193-218).
"""

from __future__ import annotations

import copy
import importlib
import importlib.machinery
import json
import re
import sys
import types
from pathlib import Path

import pytest

COMPONENT_DIR = Path(__file__).resolve().parents[1] / "custom_components" / "snapmaker_u1"
PACKAGE = "u1_under_test"


def _load_package() -> None:
    """Load the Home Assistant free modules as a small private package.

    They use relative imports, so they are registered under a synthetic package
    name pointing at the component directory. Importing the real package would
    pull in Home Assistant through __init__.py, which these tests do not need.
    """
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


# The print_task_config payload of a U1 with four loaded slots: three official
# RFID spools and one hand entered dual colour silk, with six logical colours
# mapped onto the four heads.
PRINT_TASK_CONFIG = {
    "filament_vendor": ["Snapmaker", "Snapmaker", "Snapmaker", "Generic"],
    "filament_type": ["PLA", "PLA", "PETG", "PLA"],
    "filament_sub_type": ["Basic", "Matte", "HF", "Silk"],
    "filament_color": [4278190080, 4294308065, 4292027178, 4280191205],
    "filament_color_rgba": ["000000FF", "F5F0E1FF", "D3232AFF", "1E88E5FF"],
    "filament_color_multi": [
        {"nums": 1, "alpha": 255, "mode": 0, "colors": ["000000"]},
        {"nums": 1, "alpha": 255, "mode": 0, "colors": ["F5F0E1"]},
        {"nums": 1, "alpha": 255, "mode": 0, "colors": ["D3232A"]},
        {"nums": 2, "alpha": 255, "mode": 1, "colors": ["1E88E5", "43A047"]},
    ],
    "filament_official": [True, True, True, False],
    "filament_sku": [12001, 12042, 13007, 0],
    "filament_edit": [False, False, False, True],
    "filament_exist": [True, True, True, True],
    "filament_soft": [False, False, False, False],
    "extruder_map_table": [0, 1, 2, 3, 1, 2] + [0] * 26,
    "extruders_used": [True, True, True, True],
    "extruders_replenished": [0, 1, 2, 3],
    "time_lapse_camera": True,
    "auto_bed_leveling": True,
    "flow_calibrate": False,
    "flow_calib_extruders": [True, True, True, True],
    "shaper_calibrate": False,
    "auto_replenish_filament": True,
    "replenish_ignore_color": False,
    "filament_entangle_detect": True,
    "filament_entangle_sen": "medium",
    "end_led_turn_off": False,
    "end_unload_filament": [False, False, False, False],
    "reprint_info": {
        "auto_bed_leveling": True,
        "flow_calibrate": False,
        "flow_calib_extruders": [True, True, True, True],
        "time_lapse_camera": True,
        "extruder_map_table": [0, 1, 2, 3, 1, 2] + [0] * 26,
        "extruders_used": [True, True, True, True],
        "end_unload_filament": [False, False, False, False],
    },
}


def empty_tag() -> dict:
    """A channel with no spool keeps the struct defaults.

    Copied from FILAMENT_INFO_STRUCT
    (klippy/extras/filament_protocol.py:6-38).
    """
    return {
        "VERSION": 0,
        "VENDOR": "NONE",
        "MANUFACTURER": "NONE",
        "MAIN_TYPE": "NONE",
        "SUB_TYPE": "NONE",
        "TRAY": 0,
        "ALPHA": 255,
        "MULTI_MODE": 0,
        "COLOR_NUMS": 1,
        "ARGB_COLOR": 4294967295,
        "RGB_1": 16777215,
        "RGB_2": 16777215,
        "RGB_3": 16777215,
        "RGB_4": 16777215,
        "RGB_5": 16777215,
        "DIAMETER": 0,
        "WEIGHT": 0,
        "LENGTH": 0,
        "DRYING_TEMP": 0,
        "DRYING_TIME": 0,
        "HOTEND_MAX_TEMP": 0,
        "HOTEND_MIN_TEMP": 0,
        "BED_TYPE": 0,
        "BED_TEMP": 0,
        "FIRST_LAYER_TEMP": 0,
        "OTHER_LAYER_TEMP": 0,
        "SKU": 0,
        "MF_DATE": "19700101",
        "RSA_KEY_VERSION": 0,
        "OFFICIAL": False,
        "CARD_UID": 0,
    }


def official_tag(**overrides) -> dict:
    """An official spool's tag reading."""
    tag = empty_tag()
    tag.update(
        {
            "VERSION": 2,
            "VENDOR": "Snapmaker",
            "MANUFACTURER": "Snapmaker",
            "MAIN_TYPE": "PLA",
            "SUB_TYPE": "Basic",
            "TRAY": 1,
            "ARGB_COLOR": 4278190080,
            "RGB_1": 0x000000,
            "DIAMETER": 175,
            "WEIGHT": 1000,
            "LENGTH": 330,
            "DRYING_TEMP": 55,
            "DRYING_TIME": 8,
            "HOTEND_MAX_TEMP": 240,
            "HOTEND_MIN_TEMP": 190,
            "BED_TYPE": 1,
            "BED_TEMP": 60,
            "FIRST_LAYER_TEMP": 220,
            "OTHER_LAYER_TEMP": 210,
            "SKU": 12001,
            "MF_DATE": "20260114",
            "RSA_KEY_VERSION": 1,
            "OFFICIAL": True,
            "CARD_UID": 305419896,
        }
    )
    tag.update(overrides)
    return tag


FILAMENT_DETECT = {
    "info": [
        official_tag(),
        official_tag(SUB_TYPE="Matte", RGB_1=0xF5F0E1, ARGB_COLOR=4294308065, SKU=12042),
        official_tag(
            MAIN_TYPE="PETG",
            SUB_TYPE="HF",
            RGB_1=0xD3232A,
            ARGB_COLOR=4292027178,
            SKU=13007,
            HOTEND_MIN_TEMP=230,
            HOTEND_MAX_TEMP=260,
            OTHER_LAYER_TEMP=240,
        ),
        empty_tag(),
    ],
    # 0 idle, 1 detecting, 2 self testing
    # (klippy/extras/filament_detect.py:11-13).
    "state": [0, 0, 0, 0],
    "config": {"startup_stay": False},
}


def full_status() -> dict:
    """A full objects/query snapshot of a printing U1."""
    return copy.deepcopy(
        {
            "print_task_config": PRINT_TASK_CONFIG,
            "filament_detect": FILAMENT_DETECT,
            "print_stats": {
                "filename": "prints/hex-vase-4c.gcode",
                "total_duration": 1830.4,
                "print_duration": 1712.9,
                "filament_used": 4821.6,
                "state": "printing",
                "exception": None,
                "message": "",
                "info": {"total_layer": 240, "current_layer": 96},
            },
            "virtual_sdcard": {
                "file_path": "/home/lava/printer_data/gcodes/hex-vase-4c.gcode",
                "progress": 0.4237,
                "is_active": True,
                "file_position": 4128512,
                "file_size": 9744896,
                "pl_env_valid": True,
            },
            "display_status": {"progress": 0.4237, "message": None},
            "pause_resume": {"is_paused": False},
            "toolhead": {"extruder": "extruder2", "print_time": 1712.9},
            "extruder": {
                "temperature": 42.0,
                "target": 0.0,
                "power": 0.0,
                "can_extrude": False,
                "extruder_index": 0,
                "nozzle_diameter": 0.4,
                "state": "PARKED",
            },
            "extruder1": {
                "temperature": 41.0,
                "target": 0.0,
                "power": 0.0,
                "can_extrude": False,
                "extruder_index": 1,
                "nozzle_diameter": 0.4,
                "state": "PARKED",
            },
            "extruder2": {
                "temperature": 245.0,
                "target": 245.0,
                "power": 0.62,
                "can_extrude": True,
                "extruder_index": 2,
                "nozzle_diameter": 0.4,
                "state": "ACTIVATE",
            },
            "extruder3": {
                "temperature": 40.0,
                "target": 0.0,
                "power": 0.0,
                "can_extrude": False,
                "extruder_index": 3,
                "nozzle_diameter": 0.4,
                "state": "PARKED",
            },
            "heater_bed": {"temperature": 60.0, "target": 60.0, "power": 0.31},
            "webhooks": {"state": "ready", "state_message": "Printer is ready"},
            "machine_state_manager": {"main_state": 1, "action_code": 133},
            "exception_manager": {"exceptions": []},
        }
    )


# What objects/list returns on the shipped machine, for the objects this
# integration reads.
OBJECT_LIST = (
    "webhooks",
    "configfile",
    "heaters",
    "print_stats",
    "virtual_sdcard",
    "display_status",
    "pause_resume",
    "print_task_config",
    "filament_detect",
    "machine_state_manager",
    "exception_manager",
    "toolhead",
    "extruder",
    "extruder1",
    "extruder2",
    "extruder3",
    "heater_bed",
)

# GET /printer/info (klippy/webhooks.py:365-383).
PRINTER_INFO = {
    "state": "ready",
    "state_message": "Printer is ready",
    "hostname": "U1-000123",
    "klipper_path": "/home/lava/klipper",
    "python_path": "/home/lava/klippy-env/bin/python",
    "process_id": 812,
    "user_id": 1000,
    "group_id": 1000,
    "log_file": "/home/lava/printer_data/logs/klippy.log",
    "config_file": "/home/lava/printer_data/config/printer.cfg",
    "software_version": "v0.12.0-lava-1",
    "cpu_info": "4 core arm",
}

# The slicer's own numbers for the loaded file, as Moonraker parses them
# (moonraker/components/file_manager/metadata.py:1131-1158). Six filaments,
# one per logical colour the job uses.
JOB_METADATA = {
    "filename": "prints/hex-vase-4c.gcode",
    "filament_type": ["PLA", "PLA", "PETG", "PLA", "PLA", "PETG"],
    "filament_weight": [12.5, 3.0, 8.25, 4.0, 1.5, 2.0],
    "filament_used_mm": [4180.0, 1003.4, 2400.7, 1337.2, 501.7, 668.6],
    "nozzle_temp": [220, 220, 240, 220, 220, 240],
    "nozzle_diameter_list": [0.4, 0.4, 0.4, 0.4, 0.4, 0.4],
    "filament_max_volumetric_speed": [12.0, 12.0, 10.0, 12.0, 12.0, 10.0],
}


@pytest.fixture
def state():
    """A U1State primed the way the coordinator primes it."""
    printer = parsing.U1State()
    printer.set_objects(OBJECT_LIST)
    printer.printer_info = dict(PRINTER_INFO)
    printer.apply_snapshot(full_status(), 3412.887)
    return printer


# Empty string markers and colour helpers ---------------------------------


@pytest.mark.parametrize("value", ["NONE", "none", " None ", "", None])
def test_firmware_empty_markers_read_as_empty(value):
    assert parsing.is_empty_string(value) is True
    assert parsing.clean_string(value) is None


@pytest.mark.parametrize("value", ["Snapmaker", "PLA", "Full Spectrum"])
def test_real_strings_are_kept(value):
    assert parsing.is_empty_string(value) is False
    assert parsing.clean_string(value) == value


@pytest.mark.parametrize(
    ("packed", "expected"),
    [
        (4278190080, "#000000"),
        (4294308065, "#F5F0E1"),
        (4292027178, "#D3232A"),
        (4280191205, "#1E88E5"),
    ],
)
def test_argb_int_decodes_like_the_firmware(packed, expected):
    """The firmware's own decode is (a, r, g, b) off the top byte down.

    klippy/extras/print_task_config.py:653-658.
    """
    alpha, red, green, blue = parsing.argb_parts(packed)
    assert alpha == 0xFF
    assert parsing.argb_to_hex(packed) == expected
    assert f"#{red:02X}{green:02X}{blue:02X}" == expected


def test_rgba_string_splits_into_colour_and_alpha():
    assert parsing.split_rgba("1E88E5FF") == ("1E88E5", 255)
    # The firmware writes uppercase, parsing stays case insensitive.
    assert parsing.split_rgba("1e88e580") == ("1E88E5", 128)


@pytest.mark.parametrize("value", ["1E88E5", "1E88E5FFF", "ZZZZZZFF", 0x1E88E5, None])
def test_rgba_string_rejects_anything_but_eight_hex_digits(value):
    assert parsing.split_rgba(value) is None


# Slots -------------------------------------------------------------------


def test_official_slot_reads_out_whole(state):
    slot = state.slot(0)
    assert slot.filament_type == "PLA"
    assert slot.vendor == "Snapmaker"
    assert slot.sub_type == "Basic"
    assert slot.color == "#000000"
    assert slot.alpha == 255
    assert slot.colors == ["#000000"]
    assert slot.official is True
    assert slot.sku == 12001
    assert slot.present is True
    assert slot.in_use is True
    assert slot.user_editable is False
    assert slot.loaded is True
    assert slot.is_gradient is False


def test_dual_colour_slot_keeps_every_colour(state):
    """filament_color_multi carries the gradient, colours are 6 hex each."""
    slot = state.slot(3)
    assert slot.colors == ["#1E88E5", "#43A047"]
    assert slot.color == "#1E88E5"
    assert slot.color_count == 2
    assert slot.color_mode == 1
    assert slot.is_gradient is True
    assert slot.official is False
    # 0 is the firmware's no SKU.
    assert slot.sku is None
    assert slot.user_editable is True


def test_colours_are_truncated_to_nums(state):
    """The firmware truncates colors to nums and so does the parser.

    klippy/extras/print_task_config.py:333-341.
    """
    status = full_status()
    multi = status["print_task_config"]["filament_color_multi"][3]
    multi["colors"] = ["1E88E5", "43A047", "FFFFFF", "000000"]
    multi["nums"] = 2
    state.apply_snapshot(status)
    assert state.slot(3).colors == ["#1E88E5", "#43A047"]


def test_empty_slot_reads_unknown_not_white(state):
    """A never written slot holds NONE and FFFFFFFF, which is not a colour.

    Defaults at klippy/extras/print_task_config.py:24-35.
    """
    status = full_status()
    config = status["print_task_config"]
    for index in (1,):
        config["filament_vendor"][index] = "NONE"
        config["filament_type"][index] = "NONE"
        config["filament_sub_type"][index] = "NONE"
        config["filament_color"][index] = 0xFFFFFFFF
        config["filament_color_rgba"][index] = "FFFFFFFF"
        config["filament_color_multi"][index] = {
            "nums": 1,
            "alpha": 255,
            "mode": 0,
            "colors": ["FFFFFF"],
        }
        config["filament_official"][index] = False
        config["filament_sku"][index] = 0
        config["filament_exist"][index] = False
    state.apply_snapshot(status)

    slot = state.slot(1)
    assert slot.filament_type is None
    assert slot.vendor is None
    assert slot.sub_type is None
    assert slot.color is None
    assert slot.colors == []
    assert slot.alpha is None
    assert slot.argb is None
    assert slot.sku is None
    assert slot.present is False
    assert slot.loaded is False
    assert slot.color_attributes()["gradient"] is False


def test_white_filament_with_an_identity_keeps_its_colour(state):
    """Only an unwritten slot is treated as unset, not every white spool."""
    status = full_status()
    config = status["print_task_config"]
    config["filament_color_rgba"][2] = "FFFFFFFF"
    config["filament_color"][2] = 0xFFFFFFFF
    config["filament_color_multi"][2] = {
        "nums": 1,
        "alpha": 255,
        "mode": 0,
        "colors": ["FFFFFF"],
    }
    state.apply_snapshot(status)
    assert state.slot(2).color == "#FFFFFF"


def test_packed_int_disagreeing_with_rgba_is_flagged(state):
    """The two can disagree on an RFID spool, so the mismatch is surfaced.

    filament_color is the tag's ARGB_COLOR verbatim while filament_color_rgba
    is rebuilt from RGB_1 plus ALPHA
    (klippy/extras/print_task_config.py:325-331).
    """
    assert state.slot(2).color_mismatch is False
    status = full_status()
    status["print_task_config"]["filament_color"][2] = 4278190080
    state.apply_snapshot(status)
    slot = state.slot(2)
    assert slot.color == "#D3232A"
    assert slot.argb_color == "#000000"
    assert slot.color_mismatch is True


def test_short_arrays_do_not_raise(state):
    """A fork that returns fewer entries must not break the integration."""
    state.apply_snapshot({"print_task_config": {"filament_type": ["PLA"]}})
    slot = state.slot(3)
    assert slot.filament_type is None
    assert slot.present is False
    assert state.slot(0).filament_type == "PLA"


# The colour map ----------------------------------------------------------


def test_logical_colours_map_onto_physical_heads(state):
    """extruder_map_table is 32 long, one head number per logical colour.

    Its unused tail is zeros (klippy/extras/print_task_config.py:38), so head 0
    owns every logical colour the job never touched. That is what the printer
    reports and the parser does not hide it.
    """
    assert state.color_map[:6] == [0, 1, 2, 3, 1, 2]
    assert len(state.color_map) == 32
    assert state.slot(1).assigned_colors == [1, 4]
    assert state.slot(2).assigned_colors == [2, 5]
    assert state.slot(3).assigned_colors == [3]
    assert state.slot(0).assigned_colors == [0, *range(6, 32)]
    assert len(state.slot(0).assigned_colors) == 27


# Delta updates -----------------------------------------------------------


def test_status_update_is_merged_not_swapped(state):
    """Klipper pushes only the fields that changed, so updates are deltas.

    klippy/webhooks.py:533-538 and
    moonraker/components/klippy_connection.py:663-672.
    """
    state.apply_update(
        {"print_task_config": {"filament_exist": [True, True, True, False]}},
        3413.14,
    )
    assert state.slot(3).present is False
    # Everything not in the delta survives.
    assert state.slot(3).filament_type == "PLA"
    assert state.slot(0).colors == ["#000000"]
    assert state.print_state == "printing"
    assert state.eventtime == 3413.14


def test_nested_field_arrives_whole(state):
    """A changed nested dict is sent whole, so it replaces rather than merges."""
    state.apply_update({"print_stats": {"info": {"total_layer": 240, "current_layer": 97}}})
    assert state.current_layer == 97
    assert state.total_layer == 240
    assert state.filename == "prints/hex-vase-4c.gcode"


def test_merge_status_handles_a_non_dict_object():
    store = {"print_stats": {"state": "printing"}}
    parsing.merge_status(store, {"heaters": ["extruder"]})
    assert store["heaters"] == ["extruder"]
    assert store["print_stats"]["state"] == "printing"


def test_a_snapshot_is_copied_so_a_later_delta_cannot_reach_the_response_body():
    """apply_snapshot must not alias the dicts inside the caller's payload.

    Without the copy, merge_status writes the delta straight back into whatever
    the HTTP query returned, because both sides point at the same dict.
    """
    payload = {"print_stats": {"state": "standby", "filename": None}}
    printer = parsing.U1State()
    printer.apply_snapshot(payload)
    printer.apply_update({"print_stats": {"state": "printing"}})
    assert printer.print_state == "printing"
    assert payload["print_stats"]["state"] == "standby"


def test_subscribe_notification_shape():
    """params is a two element array, status then eventtime.

    moonraker/common.py:465-474.
    """
    status, eventtime = api.parse_status_notification(
        [{"print_task_config": {"filament_exist": [True, True, True, False]}}, 3413.14]
    )
    assert status["print_task_config"]["filament_exist"] == [True, True, True, False]
    assert eventtime == 3413.14


@pytest.mark.parametrize("params", [None, [], {}, "nonsense", [None, None]])
def test_bad_notification_payloads_are_ignored(params):
    status, eventtime = api.parse_status_notification(params)
    assert status == {}
    assert eventtime is None


def test_subscribe_reply_is_unwrapped():
    status, eventtime = api.parse_subscribe_result(
        {"eventtime": 3412.887, "status": {"webhooks": {"state": "ready"}}}
    )
    assert status == {"webhooks": {"state": "ready"}}
    assert eventtime == 3412.887


def test_objects_query_params_are_the_query_string_form():
    """Each key is an object name, its value a comma separated field list.

    moonraker/components/application.py:633-644.
    """
    assert api.objects_query_params(
        {"print_task_config": None, "toolhead": ["extruder"], "print_stats": []}
    ) == {"print_task_config": "", "toolhead": "extruder", "print_stats": ""}


# The print job -----------------------------------------------------------


def test_print_job_reads_out(state):
    assert state.print_state == "printing"
    assert state.filename == "prints/hex-vase-4c.gcode"
    assert state.current_layer == 96
    assert state.total_layer == 240
    assert state.print_duration == pytest.approx(1712.9)
    assert state.total_duration == pytest.approx(1830.4)
    assert state.filament_used_mm == pytest.approx(4821.6)
    assert state.is_paused is False
    # The firmware writes "" for no message.
    assert state.print_message is None


def test_progress_is_a_percent(state):
    """virtual_sdcard.progress is 0.0 to 1.0.

    klippy/extras/virtual_sdcard.py:236-244.
    """
    assert state.progress_percent == 42.4
    state.apply_update({"virtual_sdcard": {"progress": 1.0}})
    assert state.progress_percent == 100.0


def test_missing_progress_is_unknown():
    printer = parsing.U1State()
    assert printer.progress_percent is None
    assert printer.print_state is None
    assert printer.filament_used_mm is None


def test_active_tool_prefers_the_printers_own_index(state):
    assert state.active_extruder_object == "extruder2"
    assert state.active_tool == 2


@pytest.mark.parametrize(
    ("name", "expected"),
    [("extruder", 0), ("extruder1", 1), ("extruder3", 3), ("", None), (None, None)],
)
def test_active_tool_falls_back_to_the_object_name(name, expected):
    assert parsing.active_tool_index(name) == expected


def test_active_tool_without_the_extruder_object(state):
    """toolhead names a head the query did not include."""
    state.status.pop("extruder2")
    state.apply_update({"toolhead": {"extruder": "extruder2"}})
    assert state.active_tool == 2


def test_temperatures(state):
    assert state.head_temperature(2) == 245.0
    assert state.head_target(2) == 245.0
    assert state.bed_temperature == 60.0
    assert state.bed_target == 60.0
    assert state.dock_state(2) == "ACTIVATE"
    assert state.dock_state(0) == "PARKED"


@pytest.mark.parametrize(
    ("print_state", "blocked"),
    [
        ("printing", True),
        ("paused", True),
        ("standby", False),
        ("complete", False),
        ("cancelled", False),
        ("error", False),
    ],
)
def test_config_writes_are_blocked_while_printing(state, print_state, blocked):
    """SET_PRINT_EXTRUDER_MAP is refused while printing or paused.

    klippy/extras/print_task_config.py:511-519.
    """
    state.apply_update({"print_stats": {"state": print_state}})
    assert state.writes_blocked is blocked


# RFID --------------------------------------------------------------------


def test_official_tag_reads_out(state):
    tag = state.tag(0)
    assert tag is not None
    assert tag.vendor == "Snapmaker"
    assert tag.main_type == "PLA"
    assert tag.sub_type == "Basic"
    assert tag.official is True
    assert tag.sku == 12001
    assert tag.weight_g == 1000
    assert tag.drying_temp == 55
    assert tag.drying_time == 8
    assert tag.other_layer_temp == 210
    assert tag.first_layer_temp == 220
    assert tag.hotend_min_temp == 190
    assert tag.hotend_max_temp == 240
    assert tag.mf_date == "20260114"
    assert tag.card_uid == 305419896
    assert tag.color == "#000000"
    assert tag.colors == ["#000000"]


def test_petg_tag_has_its_own_temperatures(state):
    tag = state.tag(2)
    assert tag.main_type == "PETG"
    assert tag.hotend_min_temp == 230
    assert tag.other_layer_temp == 240


def test_a_channel_with_no_tag_is_none(state):
    """The hand entered slot has no tag, so there is nothing to report."""
    assert state.tag(3) is None
    assert state.slot(3).official is False


def test_tag_defaults_read_as_unknown():
    """Every numeric tag field uses 0 for not stated.

    klippy/extras/filament_protocol.py:6-38.
    """
    tag = parsing.build_tag(empty_tag() | {"VENDOR": "Generic"}, 0)
    assert tag is not None
    assert tag.weight_g is None
    assert tag.drying_temp is None
    assert tag.length is None
    assert tag.diameter is None
    # 19700101 is the struct default, not a manufacturing date.
    assert tag.mf_date is None


def test_multi_colour_tag_keeps_colour_nums_colours():
    tag = parsing.build_tag(
        empty_tag()
        | {
            "VENDOR": "Snapmaker",
            "COLOR_NUMS": 3,
            "MULTI_MODE": 1,
            "RGB_1": 0x1E88E5,
            "RGB_2": 0x43A047,
            "RGB_3": 0xFDD835,
        },
        3,
    )
    assert tag.colors == ["#1E88E5", "#43A047", "#FDD835"]
    assert tag.multi_mode == 1


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0, "idle"), (1, "detecting"), (2, "self_testing"), (9, "unknown_9")],
)
def test_scan_states(state, value, expected):
    state.apply_update({"filament_detect": {"state": [value, 0, 0, 0]}})
    assert state.scan_state(0) == expected


def test_scan_state_without_the_object():
    assert parsing.U1State().scan_state(0) is None


# Per colour usage --------------------------------------------------------


def test_per_colour_grams_come_from_the_sliced_file(state):
    """The printer never publishes per colour usage, so the file is the source.

    print_task_config_2 holds filament_used_g per logical extruder but no
    get_status returns it (klippy/extras/print_task_config.py:503). Moonraker's
    metadata does have per filament weights
    (moonraker/components/file_manager/metadata.py:1145).
    """
    assert state.job_color_grams == {}
    state.set_job_metadata(JOB_METADATA["filename"], JOB_METADATA)
    assert state.job_color_grams == {
        0: 12.5,
        1: 3.0,
        2: 8.25,
        3: 4.0,
        4: 1.5,
        5: 2.0,
    }


def test_per_slot_grams_sum_the_colours_mapped_onto_it(state):
    """Colours 0 and nothing else on head 0, 1 and 4 on head 1, 2 and 5 on 2."""
    state.set_job_metadata(JOB_METADATA["filename"], JOB_METADATA)
    assert state.head_job_grams(0) == 12.5
    assert state.head_job_grams(1) == 4.5
    assert state.head_job_grams(2) == 10.25
    assert state.head_job_grams(3) == 4.0
    assert sum(state.job_head_grams.values()) == pytest.approx(sum(JOB_METADATA["filament_weight"]))


def test_per_slot_grams_follow_a_remap(state):
    """Remapping colour 5 onto head 3 moves its grams with it."""
    state.set_job_metadata(JOB_METADATA["filename"], JOB_METADATA)
    table = list(state.color_map)
    table[5] = 3
    state.apply_update({"print_task_config": {"extruder_map_table": table}})
    assert state.head_job_grams(2) == 8.25
    assert state.head_job_grams(3) == 6.0


def test_without_metadata_grams_are_unknown(state):
    assert state.head_job_grams(0) is None
    state.set_job_metadata("prints/x.gcode", {"estimated_time": 100})
    assert state.head_job_grams(0) is None


def test_a_single_colour_job_reports_only_its_head(state):
    state.set_job_metadata("prints/one.gcode", {"filament_weight": [30.0]})
    assert state.head_job_grams(0) == 30.0
    assert state.head_job_grams(1) == 0.0


# Machine level -----------------------------------------------------------


def test_klippy_and_machine_state(state):
    assert state.klippy_state == "ready"
    assert state.klippy_message == "Printer is ready"
    assert state.machine_state == "printing"
    assert state.action_code == 133
    assert state.action_name == "print_auto_feeding"
    assert state.exceptions == []
    assert state.hostname == "U1-000123"
    assert state.software_version == "v0.12.0-lava-1"


def test_machine_state_decodes_every_firmware_enum_value():
    """main_state and action_code arrive as ints, never as names.

    They are IntEnum members inside Klippy (machine_state_manager.py:9-27,
    :30-87) and get_status returns the member, which JSON encodes as its number.
    """
    printer = parsing.U1State()
    for number, name in const.MACHINE_MAIN_STATES.items():
        printer.apply_snapshot({"machine_state_manager": {"main_state": number}})
        assert printer.machine_state == name
    for number, name in const.ACTION_CODES.items():
        printer.apply_snapshot({"machine_state_manager": {"action_code": number}})
        assert printer.action_name == name


def test_machine_state_reports_a_number_the_firmware_table_does_not_have():
    printer = parsing.U1State()
    printer.apply_snapshot({"machine_state_manager": {"main_state": 99, "action_code": 98}})
    assert printer.machine_state == "unknown_99"
    assert printer.action_name == "unknown_98"


def test_machine_state_still_accepts_a_name_from_a_fork():
    printer = parsing.U1State()
    printer.apply_snapshot({"machine_state_manager": {"main_state": "PRINTING"}})
    assert printer.machine_state == "printing"


def test_machine_state_is_unknown_when_the_object_is_absent():
    printer = parsing.U1State()
    assert printer.machine_state is None
    assert printer.action_name is None


def test_klippy_state_falls_back_to_the_info_block():
    printer = parsing.U1State()
    printer.printer_info = {"state": "startup", "state_message": "Loading"}
    assert printer.klippy_state == "startup"
    assert printer.klippy_message == "Loading"


def test_availability_needs_a_ready_klippy(state):
    assert state.available is True
    state.apply_update({"webhooks": {"state": "shutdown"}})
    assert state.available is False
    state.apply_update({"webhooks": {"state": "ready"}})
    assert state.available is True
    # This is what a notify_klippy_disconnected does.
    state.klippy_connected = False
    assert state.available is False


def test_object_list_decides_what_exists(state):
    assert state.is_u1 is True
    assert state.has_object("filament_detect") is True
    assert state.has_object("purifier") is False


def test_a_plain_klipper_printer_is_not_a_u1():
    printer = parsing.U1State()
    printer.set_objects(["webhooks", "print_stats", "virtual_sdcard", "toolhead"])
    assert printer.is_u1 is False


def test_preferences_read_out(state):
    assert state.preference(const.PREF_AUTO_REPLENISH) is True
    assert state.preference(const.PREF_ENTANGLE_DETECT) is True
    assert state.preference(const.PREF_REPLENISH_IGNORE_COLOR) is False
    assert state.preference(const.PREF_END_LED_OFF) is False
    assert state.entangle_sensitivity == "medium"
    assert state.entangle_sensitivity in const.ENTANGLE_SENSITIVITIES


def test_head_object_names_match_klipper(state):
    """Head 0 is "extruder", the rest carry their index."""
    assert state.head_object_name(0) == "extruder"
    assert state.head_object_name(1) == "extruder1"
    assert state.head_object_name(3) == "extruder3"


def test_every_slot_and_head_is_covered(state):
    assert len(state.slots()) == const.PHYSICAL_EXTRUDER_NUM
    assert [slot.index for slot in state.slots()] == [0, 1, 2, 3]
    assert len(state.color_map) == const.LOGICAL_EXTRUDER_NUM


# The files Home Assistant reads -------------------------------------------

PLATFORM_FILES = {
    "sensor": "sensor.py",
    "binary_sensor": "binary_sensor.py",
    "switch": "switch.py",
    "select": "select.py",
    "button": "button.py",
}


def _json(name: str) -> dict:
    return json.loads((COMPONENT_DIR / name).read_text())


def test_manifest_is_what_home_assistant_and_hacs_want():
    manifest = _json("manifest.json")
    assert manifest["domain"] == const.DOMAIN
    assert manifest["config_flow"] is True
    # The printer pushes over the websocket.
    assert manifest["iot_class"] == "local_push"
    assert isinstance(manifest["requirements"], list)
    # A custom integration needs a version. HACS reads it too.
    assert manifest["version"]
    assert manifest["codeowners"]
    assert manifest["documentation"].startswith("http")
    assert manifest["issue_tracker"].startswith("http")


def test_english_translations_match_the_strings_file():
    assert _json("translations/en.json") == _json("strings.json")


def test_every_translation_key_has_a_name():
    """A translation_key with no entry shows up as a blank entity name."""
    strings = _json("strings.json")
    for platform, filename in PLATFORM_FILES.items():
        source = (COMPONENT_DIR / filename).read_text()
        keys = set(re.findall(r'translation_key="([a-z0-9_]+)"', source))
        assert keys, f"no translation keys found in {filename}"
        named = strings["entity"].get(platform, {})
        missing = sorted(key for key in keys if key not in named)
        assert not missing, f"{platform} is missing names for {missing}"


def test_config_flow_errors_are_all_translated():
    strings = _json("strings.json")
    source = (COMPONENT_DIR / "config_flow.py").read_text()
    used = set(re.findall(r'errors\["base"\] = "([a-z0-9_]+)"', source))
    assert used == set(strings["config"]["error"])


def test_services_are_declared_everywhere_they_have_to_be():
    """The three names have to agree across const, services.yaml and strings."""
    declared = {
        const.SERVICE_SET_COLOR_MAP,
        const.SERVICE_SET_FILAMENT,
        const.SERVICE_SEND_GCODE,
    }
    yaml_text = (COMPONENT_DIR / "services.yaml").read_text()
    in_yaml = set(re.findall(r"^([a-z_]+):$", yaml_text, re.MULTILINE))
    assert in_yaml == declared
    assert set(_json("strings.json")["services"]) == declared


def test_entity_keys_are_unique():
    """Two entities with the same key would collide on their unique id."""
    keys: list[str] = []
    for filename in PLATFORM_FILES.values():
        source = (COMPONENT_DIR / filename).read_text()
        keys.extend(re.findall(r'(?<![a-z_])key=f?"([a-z0-9_{}]+)"', source))
    assert len(keys) == len(set(keys)), sorted(keys)
