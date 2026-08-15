"""Constants for the Snapmaker U1 integration.

Nothing here imports Home Assistant. That keeps the parsing layer importable
in a plain pytest run, so the payload handling can be tested without a running
Home Assistant.

Every value below is taken from the Snapmaker forks of Klipper and Moonraker.
Citations are file:line into those two trees:
  klipper   https://github.com/Snapmaker/U1-Klipper
  moonraker https://github.com/Snapmaker/U1-Moonraker
"""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "snapmaker_u1"

# u1-moonraker/lava/moonraker.conf:3
DEFAULT_PORT: Final = 7125

CONF_API_KEY: Final = "api_key"
CONF_USE_SSL: Final = "use_ssl"

# klippy/extras/print_task_config.py:8-9
LOGICAL_EXTRUDER_NUM: Final = 32
PHYSICAL_EXTRUDER_NUM: Final = 4

# Printer object names. print_task_config and filament_detect are the two U1
# specific ones. Both are enabled in klippy lava/printer.cfg (:7 and :61).
OBJ_PRINT_TASK_CONFIG: Final = "print_task_config"
OBJ_FILAMENT_DETECT: Final = "filament_detect"
OBJ_PRINT_STATS: Final = "print_stats"
OBJ_VIRTUAL_SDCARD: Final = "virtual_sdcard"
OBJ_DISPLAY_STATUS: Final = "display_status"
OBJ_PAUSE_RESUME: Final = "pause_resume"
OBJ_TOOLHEAD: Final = "toolhead"
OBJ_HEATER_BED: Final = "heater_bed"
OBJ_WEBHOOKS: Final = "webhooks"
OBJ_MACHINE_STATE: Final = "machine_state_manager"
OBJ_EXCEPTION_MANAGER: Final = "exception_manager"

# The extruder object for head 0 is named "extruder", heads 1..3 are
# "extruder1".."extruder3" (klippy/kinematics/extruder.py).
EXTRUDER_OBJECTS: Final = ("extruder", "extruder1", "extruder2", "extruder3")

# Objects the integration reads. None means every field, which Klipper freezes
# to the key list seen on the first pass (klippy/webhooks.py:529-532), so the
# subscription is made once with the complete object set.
WANTED_OBJECTS: Final[dict[str, list[str] | None]] = {
    OBJ_PRINT_TASK_CONFIG: None,
    OBJ_FILAMENT_DETECT: None,
    OBJ_PRINT_STATS: None,
    OBJ_VIRTUAL_SDCARD: None,
    OBJ_DISPLAY_STATUS: None,
    OBJ_PAUSE_RESUME: None,
    OBJ_TOOLHEAD: None,
    OBJ_HEATER_BED: None,
    OBJ_WEBHOOKS: None,
    OBJ_MACHINE_STATE: None,
    OBJ_EXCEPTION_MANAGER: None,
    "extruder": None,
    "extruder1": None,
    "extruder2": None,
    "extruder3": None,
}

# Objects that must exist for the integration to be useful. print_task_config
# is what makes a printer a U1 as far as this integration is concerned.
REQUIRED_OBJECTS: Final = (OBJ_PRINT_TASK_CONFIG,)

# The firmware writes "NONE" for an unset string and also checks for the empty
# string (klippy/extras/print_task_config.py:299, :833, :862).
EMPTY_STRINGS: Final = ("", "NONE")

# Unset colour defaults (klippy/extras/print_task_config.py:26-32).
DEFAULT_COLOR_ARGB: Final = 0xFFFFFFFF
DEFAULT_COLOR_RGBA: Final = "FFFFFFFF"
DEFAULT_COLOR_HEX: Final = "FFFFFF"

# klippy/extras/print_task_config.py:11-13
ENTANGLE_SENSITIVITIES: Final = ("low", "medium", "high")

# klippy/extras/filament_detect.py:11-13
SCAN_STATES: Final = {0: "idle", 1: "detecting", 2: "self_testing"}

# klippy/extras/print_stats.py: standby :298, printing :140, paused :151,
# complete :157, error :159, cancelled :163
PRINT_STATES: Final = (
    "standby",
    "printing",
    "paused",
    "complete",
    "cancelled",
    "error",
)

# SET_PRINT_EXTRUDER_MAP and friends are refused in these states
# (klippy/extras/print_task_config.py:511-519, :788-796, :1063-1073).
BLOCKING_PRINT_STATES: Final = ("printing", "paused")

# klippy/extras/park_detector.py:74-80
DOCK_STATES: Final = ("PARKED", "ACTIVATE", "UNKNOWN")

# machine_state_manager.main_state and action_code are IntEnum members
# (klippy/extras/machine_state_manager.py:9-27 and :30-87) and get_status
# returns the members themselves (:322-326). An IntEnum serialises to JSON as
# its number, so a client sees plain ints and has to decode them. Confirmed
# against a real Moonraker running on the simulator: the payload carries
# main_state 1 while printing, see artifacts/real-moonraker/.
MACHINE_MAIN_STATES: Final[dict[int, str]] = {
    0: "idle",
    1: "printing",
    2: "xyz_offset_calibrate",
    3: "bed_leveling",
    4: "flow_calibration",
    5: "shaper_calibrate",
    6: "upgrading",
    7: "abnormal",
    8: "screws_tilt_adjust",
    9: "auto_load",
    10: "auto_unload",
    11: "manual_load",
    12: "park_point_manual_calibration",
    13: "homing_origin_calibration",
}

# klippy/extras/machine_state_manager.py:30-87. The gaps are the firmware's:
# the codes are grouped in blocks of 64 by main state.
ACTION_CODES: Final[dict[int, str]] = {
    0: "idle",
    1: "homing",
    2: "detect_plate",
    3: "prehrat_chamber",
    128: "print_pl_restore",
    129: "print_paused",
    130: "print_resuming",
    131: "print_replenishing",
    132: "print_switch_checking",
    133: "print_auto_feeding",
    134: "print_preextruding",
    135: "print_auto_unloading",
    136: "print_bed_detecting",
    192: "manual_clean_extruder",
    193: "manual_clean_extruder1",
    194: "manual_clean_extruder2",
    195: "manual_clean_extruder3",
    196: "extruder_xyz_offset_probe",
    197: "extruder1_xyz_offset_probe",
    198: "extruder2_xyz_offset_probe",
    199: "extruder3_xyz_offset_probe",
    200: "auto_clean_nozzle",
    201: "wait_nozzle_cooling",
    256: "bed_leveling",
    257: "bed_preheating",
    258: "bed_prescanning",
    320: "extruder_flow_calibrating",
    321: "extruder1_flow_calibrating",
    322: "extruder2_flow_calibrating",
    323: "extruder3_flow_calibrating",
    384: "shaper_calibrating",
    512: "reset_to_initial",
    513: "probe_reference_points",
    514: "manual_tuning",
    515: "probing_adjust_verify",
    576: "auto_loading",
    640: "auto_unloading",
    704: "manual_loading",
    768: "park_point_manual_calibrating",
    769: "extruder_pick_verify",
    770: "extruder_park_verify",
    832: "homing_origin_calibrating",
}

# Preference keys accepted by print_task_config/set_print_preferences
# (klippy/extras/print_task_config.py:150-174). The first four are ints used as
# booleans, the last is one of ENTANGLE_SENSITIVITIES.
PREF_AUTO_REPLENISH: Final = "auto_replenish_filament"
PREF_ENTANGLE_DETECT: Final = "filament_entangle_detect"
PREF_REPLENISH_IGNORE_COLOR: Final = "replenish_ignore_color"
PREF_END_LED_OFF: Final = "end_led_turn_off"
PREF_ENTANGLE_SEN: Final = "filament_entangle_sen"

BOOL_PREFERENCES: Final = (
    PREF_AUTO_REPLENISH,
    PREF_ENTANGLE_DETECT,
    PREF_REPLENISH_IGNORE_COLOR,
    PREF_END_LED_OFF,
)

# Services
SERVICE_SET_COLOR_MAP: Final = "set_color_map"
SERVICE_SET_FILAMENT: Final = "set_filament"
SERVICE_SEND_GCODE: Final = "send_gcode"

ATTR_LOGICAL: Final = "logical"
ATTR_HEAD: Final = "head"
ATTR_SLOT: Final = "slot"
ATTR_SCRIPT: Final = "script"
ATTR_VENDOR: Final = "vendor"
ATTR_TYPE: Final = "filament_type"
ATTR_SUB_TYPE: Final = "sub_type"
ATTR_COLOR: Final = "color"
ATTR_FORCE: Final = "force"

# Seconds between HTTP polls while the websocket is down.
FALLBACK_POLL_INTERVAL: Final = 15

# Websocket reconnect backoff in seconds.
RECONNECT_MIN: Final = 2
RECONNECT_MAX: Final = 60

MANUFACTURER: Final = "Snapmaker"
MODEL: Final = "U1"
