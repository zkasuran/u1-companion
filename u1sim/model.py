"""Printer state the simulator serves, in the shapes the U1 firmware produces.

Every key here is copied from the Snapmaker Klipper fork. The important
sources are:

  klippy/extras/print_task_config.py   DEFAULT_PRINT_TASK_CONFIG at :23-61,
                                       get_status at :499-504, the RFID
                                       callback at :308-366, the manual write
                                       path at :556-682
  klippy/extras/filament_protocol.py   FILAMENT_INFO_STRUCT at :6-38, the
                                       type tables at :56-104
  klippy/extras/filament_detect.py     get_status at :272-276
  klippy/extras/print_stats.py         get_status at :326-336
  klippy/extras/virtual_sdcard.py      get_status at :236-244
  klippy/kinematics/extruder.py        get_status at :709-730
  klippy/extras/heaters.py             heater status at :188-194
  klippy/toolhead.py                   get_status at :625-638

Numbers the firmware does not model, for example how fast a hotend heats or
how many millimetres a colour consumes per second, are simulator estimates.
They are marked SIMULATED in the code and are never presented as firmware
fields.
"""

from __future__ import annotations

import copy
import math
from typing import Any

from .protocol import WebRequestError

# print_task_config.py:8-9
LOGICAL_EXTRUDER_NUM = 32
PHYSICAL_EXTRUDER_NUM = 4
# print_task_config.py:15
FILAMENT_COLOR_NUMS_MAX = 5
# print_task_config.py:18
VALID_NOZZLE_DIAMETERS = [0.2, 0.4, 0.6, 0.8]
# print_task_config.py:11-13
ENTANGLE_SENSITIVITIES = ("low", "medium", "high")
# The firmware's empty string for vendor, type and sub type.
EMPTY_MARKER = "NONE"

# filament_detect.py:11-13
SCAN_STATE_IDLE = 0
SCAN_STATE_DETECTING = 1
SCAN_STATE_SELF_TESTING = 2

# print_task_config.py:23-61. Reproduced key for key, including the nested
# reprint_info block, so a client that walks the payload sees the real shape.
DEFAULT_PRINT_TASK_CONFIG: dict[str, Any] = {
    "filament_vendor": [EMPTY_MARKER] * PHYSICAL_EXTRUDER_NUM,
    "filament_type": [EMPTY_MARKER] * PHYSICAL_EXTRUDER_NUM,
    "filament_sub_type": [EMPTY_MARKER] * PHYSICAL_EXTRUDER_NUM,
    "filament_color": [0xFFFFFFFF] * PHYSICAL_EXTRUDER_NUM,
    "filament_color_rgba": ["FFFFFFFF"] * PHYSICAL_EXTRUDER_NUM,
    "filament_color_multi": [
        {"nums": 1, "alpha": 0xFF, "mode": 0, "colors": ["FFFFFF"]}
        for _ in range(PHYSICAL_EXTRUDER_NUM)
    ],
    "filament_official": [False] * PHYSICAL_EXTRUDER_NUM,
    "filament_sku": [0] * PHYSICAL_EXTRUDER_NUM,
    "filament_edit": [True] * PHYSICAL_EXTRUDER_NUM,
    "filament_exist": [False] * PHYSICAL_EXTRUDER_NUM,
    "filament_soft": [False] * PHYSICAL_EXTRUDER_NUM,
    "extruder_map_table": list(range(PHYSICAL_EXTRUDER_NUM))
    + [0] * (LOGICAL_EXTRUDER_NUM - PHYSICAL_EXTRUDER_NUM),
    "extruders_used": [False] * PHYSICAL_EXTRUDER_NUM,
    "extruders_replenished": list(range(PHYSICAL_EXTRUDER_NUM)),
    "time_lapse_camera": False,
    "auto_bed_leveling": False,
    "flow_calibrate": False,
    "flow_calib_extruders": [True] * PHYSICAL_EXTRUDER_NUM,
    "shaper_calibrate": False,
    "auto_replenish_filament": True,
    "replenish_ignore_color": False,
    "filament_entangle_detect": False,
    "filament_entangle_sen": "medium",
    "end_led_turn_off": False,
    "end_unload_filament": [False] * PHYSICAL_EXTRUDER_NUM,
    "reprint_info": {
        "auto_bed_leveling": False,
        "flow_calibrate": False,
        "flow_calib_extruders": [True] * PHYSICAL_EXTRUDER_NUM,
        "time_lapse_camera": False,
        "extruder_map_table": list(range(PHYSICAL_EXTRUDER_NUM))
        + [0] * (LOGICAL_EXTRUDER_NUM - PHYSICAL_EXTRUDER_NUM),
        "extruders_used": [False] * PHYSICAL_EXTRUDER_NUM,
        "end_unload_filament": [False] * PHYSICAL_EXTRUDER_NUM,
    },
}

# filament_protocol.py:6-38. One of these per NFC channel, four channels.
FILAMENT_INFO_STRUCT: dict[str, Any] = {
    "VERSION": 0,
    "VENDOR": EMPTY_MARKER,
    "MANUFACTURER": EMPTY_MARKER,
    "MAIN_TYPE": EMPTY_MARKER,
    "SUB_TYPE": EMPTY_MARKER,
    "TRAY": 0,
    "ALPHA": 0xFF,
    "MULTI_MODE": 0,
    "COLOR_NUMS": 1,
    "ARGB_COLOR": 0xFFFFFFFF,
    "RGB_1": 0xFFFFFF,
    "RGB_2": 0xFFFFFF,
    "RGB_3": 0xFFFFFF,
    "RGB_4": 0xFFFFFF,
    "RGB_5": 0xFFFFFF,
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

# filament_protocol.py:56-71 keys. A tag decodes MAIN_TYPE to one of these.
MAIN_TYPES = (
    "PLA",
    "PETG",
    "ABS",
    "TPU",
    "PVA",
    "ASA",
    "PA",
    "PA-CF",
    "PA-GF",
    "PC",
    "PLA-CF",
    "PEBA",
    "TPE",
    "Reserved",
)
# filament_protocol.py:89-104 keys, including the empty string.
SUB_TYPES = (
    "Basic",
    "Matte",
    "SnapSpeed",
    "Silk",
    "Support",
    "HF",
    "95A",
    "95A HF",
    "90A",
    "85A",
    "Wood",
    "Translucent",
    "Full Spectrum",
    "",
)

# SIMULATED. Nominal filament densities in g/cm3, used only to turn simulated
# millimetres into simulated grams for the u1sim debug view. The firmware does
# not publish grams on the API at all, see print_task_config.py:63-75 and the
# note in u1sim/README.md.
NOMINAL_DENSITY_G_CM3: dict[str, float] = {
    "PLA": 1.24,
    "PLA-CF": 1.24,
    "PETG": 1.27,
    "ABS": 1.04,
    "ASA": 1.07,
    "TPU": 1.21,
    "TPE": 1.21,
    "PVA": 1.23,
    "PC": 1.20,
    "PA": 1.15,
}
NOMINAL_FILAMENT_DIAMETER_MM = 1.75


def argb_from_rgb(rgb: int, alpha: int = 0xFF) -> int:
    """Pack a 24 bit colour and an alpha byte the way the firmware does.

    print_task_config.py:653-658 decodes filament_color as alpha in the top
    byte then red, green, blue.
    """
    return ((alpha & 0xFF) << 24) | (rgb & 0xFFFFFF)


def grams_from_mm(length_mm: float, filament_type: str | None) -> float:
    """SIMULATED conversion, not a firmware field."""
    radius = NOMINAL_FILAMENT_DIAMETER_MM / 2.0
    volume_cm3 = math.pi * radius * radius * length_mm / 1000.0
    density = NOMINAL_DENSITY_G_CM3.get(filament_type or "", 1.24)
    return volume_cm3 * density


class PrintTaskConfig:
    """The print_task_config printer object.

    The four per-slot arrays, the 32 entry colour map and the preference flags
    all live here. The write paths reproduce the firmware's own rules, so a
    client that drives the simulator hits the same refusals it would hit on a
    printer.
    """

    def __init__(self) -> None:
        self.data: dict[str, Any] = copy.deepcopy(DEFAULT_PRINT_TASK_CONFIG)

    # ---- reads -------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        return copy.deepcopy(self.data)

    def map_index(self, logical: int) -> int:
        """print_task_config.py:370-374."""
        if logical < 0 or logical + 1 > LOGICAL_EXTRUDER_NUM:
            raise WebRequestError(
                f"[print_task_config] index out of range[0,{LOGICAL_EXTRUDER_NUM - 1}]"
            )
        return self.data["extruder_map_table"][logical]

    def assigned_logical(self, head: int) -> list[int]:
        table = self.data["extruder_map_table"]
        return [i for i, h in enumerate(table) if h == head]

    # ---- derived flags ----------------------------------------------
    def update_filament_edit_flag(self) -> None:
        """print_task_config.py:462-472. Editable only when loaded and not official."""
        edit = list(self.data["filament_edit"])
        for slot in range(PHYSICAL_EXTRUDER_NUM):
            edit[slot] = bool(
                self.data["filament_exist"][slot] and not self.data["filament_official"][slot]
            )
        self.data["filament_edit"] = edit

    def set_filament_exist(self, exist: list[bool]) -> None:
        """print_task_config.py:474-498 computes this from the slot sensors."""
        self.data["filament_exist"] = [bool(v) for v in exist]

    # ---- writes ------------------------------------------------------
    def rfid_update(self, channel: int, info: dict[str, Any]) -> bool:
        """Fill a slot from a scanned tag. print_task_config.py:308-366.

        Returns True when the slot changed. The two early returns are the
        firmware's: a non official tag is ignored when the slot already names a
        vendor. A repeat of the same official SKU is ignored too.
        """
        if channel < 0 or channel >= PHYSICAL_EXTRUDER_NUM:
            raise WebRequestError(
                f"[print_task_config] rfid channel[{channel}] is out of range"
                f"[0, {PHYSICAL_EXTRUDER_NUM - 1}]"
            )
        if not info["OFFICIAL"] and self.data["filament_vendor"][channel] != EMPTY_MARKER:
            return False
        if (
            info["OFFICIAL"]
            and self.data["filament_sku"][channel] == info["SKU"]
            and self.data["filament_official"][channel] == info["OFFICIAL"]
        ):
            return False

        colors = [
            "{:06X}".format(info["RGB_1"]),
            "{:06X}".format(info["RGB_2"]),
            "{:06X}".format(info["RGB_3"]),
            "{:06X}".format(info["RGB_4"]),
            "{:06X}".format(info["RGB_5"]),
        ]
        nums = info["COLOR_NUMS"]
        self.data["filament_vendor"][channel] = info["VENDOR"]
        self.data["filament_type"][channel] = info["MAIN_TYPE"]
        self.data["filament_sub_type"][channel] = info["SUB_TYPE"]
        # The firmware copies ARGB_COLOR verbatim here while rebuilding
        # filament_color_rgba from RGB_1 plus ALPHA, so the two can disagree
        # on a tag whose ARGB_COLOR is stale. Reproduced on purpose.
        self.data["filament_color"][channel] = info["ARGB_COLOR"]
        self.data["filament_color_rgba"][channel] = "{:06X}{:02X}".format(
            info["RGB_1"],
            info["ALPHA"],
        )
        self.data["filament_color_multi"][channel] = {
            "nums": nums,
            "alpha": info["ALPHA"],
            "mode": info["MULTI_MODE"],
            "colors": colors[:nums],
        }
        self.data["filament_official"][channel] = info["OFFICIAL"]
        self.data["filament_sku"][channel] = info["SKU"]
        self.data["filament_soft"][channel] = False
        return True

    def set_filament_config(
        self,
        slot: int,
        vendor: str | None = None,
        filament_type: str | None = None,
        sub_type: str | None = None,
        soft: bool | None = None,
        color: int | None = None,
        rgba: str | None = None,
        alpha: int | None = None,
        color_nums: int | None = None,
        colors: list[str] | None = None,
        multi_mode: int = 0,
        force: bool = False,
    ) -> None:
        """The manual write path, SET_PRINT_FILAMENT_CONFIG.

        print_task_config.py:556-682. Refuses an official slot without FORCE,
        needs vendor plus type plus sub type together, then clears the official
        flag plus the SKU on success.
        """
        if slot < 0 or slot >= PHYSICAL_EXTRUDER_NUM:
            raise WebRequestError(
                f"[print_task_config] extruder{slot} is out of range"
                f"[0, {PHYSICAL_EXTRUDER_NUM - 1}]"
            )
        if self.data["filament_official"][slot] and not force:
            raise WebRequestError(
                "[print_task_config] filament_config, official filament, not configurable!"
            )
        data = copy.deepcopy(self.data)
        if alpha is None:
            alpha = int(data["filament_color_rgba"][slot][6:8], 16)
            old = data["filament_color_rgba"][slot]
            data["filament_color_rgba"][slot] = old[0:6] + f"{alpha:02X}"
            data["filament_color_multi"][slot]["alpha"] = alpha

        if filament_type is not None:
            if vendor is None or sub_type is None:
                raise WebRequestError("[print_task_config] filament_config, incomplete parameters")
            data["filament_vendor"][slot] = vendor
            data["filament_type"][slot] = filament_type
            data["filament_sub_type"][slot] = sub_type
            data["filament_soft"][slot] = bool(soft) if soft is not None else False

        self._apply_color(data, slot, alpha, color, rgba, color_nums, colors, multi_mode)
        data["filament_official"][slot] = False
        data["filament_sku"][slot] = 0
        self.data = data

    @staticmethod
    def _apply_color(
        data: dict[str, Any],
        slot: int,
        alpha: int,
        color: int | None,
        rgba: str | None,
        color_nums: int | None,
        colors: list[str] | None,
        multi_mode: int,
    ) -> None:
        """print_task_config.py:606-667. One of three colour forms wins."""
        if color_nums is None and rgba is None and color is None:
            return
        if color_nums is not None:
            if colors is None:
                raise WebRequestError("[print_task_config] filament_config, incomplete parameters")
            if not 1 <= color_nums <= FILAMENT_COLOR_NUMS_MAX or len(colors) != color_nums:
                raise WebRequestError("[print_task_config] filament_config, colors error")
            for entry in colors:
                if len(entry) != 6 or any(c not in "0123456789abcdefABCDEF" for c in entry):
                    raise WebRequestError("[print_task_config] filament_config, colors error")
            multi = {
                "nums": color_nums,
                "alpha": alpha,
                "mode": multi_mode,
                "colors": list(colors),
            }
            new_rgba = colors[0] + f"{alpha:02X}"
            new_color = argb_from_rgb(int(colors[0], 16), alpha)
        elif rgba is not None:
            if len(rgba) == 6:
                rgba = rgba + "FF"
            if len(rgba) != 8 or any(c not in "0123456789abcdefABCDEF" for c in rgba):
                raise WebRequestError("[print_task_config] filament_config, rgba error")
            multi = {
                "nums": 1,
                "alpha": int(rgba[6:8], 16),
                "mode": 0,
                "colors": [rgba[0:6]],
            }
            new_rgba = rgba
            new_color = argb_from_rgb(int(rgba[0:6], 16), int(rgba[6:8], 16))
        else:
            assert color is not None
            color &= 0xFFFFFFFF
            packed_alpha = (color & 0xFF000000) >> 24
            new_rgba = (
                f"{(color & 0x00FF0000) >> 16:02X}"
                f"{(color & 0x0000FF00) >> 8:02X}"
                f"{color & 0x000000FF:02X}"
                f"{packed_alpha:02X}"
            )
            # The firmware stores the 8 character rgba string in colors here
            # (print_task_config.py:663) while every other path stores 6
            # characters. Reproduced so a client sees what a printer sends.
            multi = {
                "nums": 1,
                "alpha": packed_alpha,
                "mode": 0,
                "colors": [new_rgba],
            }
            new_color = color
        data["filament_color"][slot] = new_color
        data["filament_color_rgba"][slot] = new_rgba
        data["filament_color_multi"][slot] = multi

    def set_extruder_map(self, logical: int, head: int, printing: bool) -> None:
        """SET_PRINT_EXTRUDER_MAP. print_task_config.py:506-538.

        The refusal while printing or paused comes first, before the argument
        check. Its text is the firmware's own.
        """
        if printing:
            raise WebRequestError(
                "[print_task_config] not allowed to set extruder map during printing!"
            )
        if logical is None or head is None:
            raise WebRequestError("[print_task_config] extruder map, incomplete parameters")
        if not 0 <= logical < LOGICAL_EXTRUDER_NUM or not 0 <= head < PHYSICAL_EXTRUDER_NUM:
            raise WebRequestError("[print_task_config] extruder map, invalid extruder index!!!")
        self.data["extruder_map_table"][logical] = head
        self.data["reprint_info"]["extruder_map_table"][logical] = head

    def set_preferences(self, params: dict[str, Any]) -> None:
        """print_task_config/set_print_preferences. print_task_config.py:140-186.

        Five settable members. The three integers are coerced with bool(), the
        sensitivity is checked against the three allowed strings.
        """
        data = copy.deepcopy(self.data)
        for key in (
            "auto_replenish_filament",
            "filament_entangle_detect",
            "replenish_ignore_color",
            "end_led_turn_off",
        ):
            if key in params:
                value = params[key]
                if isinstance(value, bool) or not isinstance(value, int):
                    raise WebRequestError(f"Invalid Argument Type [{key}]")
                data[key] = bool(value)
        if "filament_entangle_sen" in params:
            sen = params["filament_entangle_sen"]
            if sen not in ENTANGLE_SENSITIVITIES:
                raise WebRequestError(f"filament_entangle_sen error: {sen}")
            data["filament_entangle_sen"] = sen
        self.data = data

    def set_used_extruders(self, job_logical: list[int]) -> None:
        """Mirror of print_task_config.py:449-456.

        A job declares the logical colours it needs. Every head those colours
        map to is marked used.
        """
        used = [False] * PHYSICAL_EXTRUDER_NUM
        for logical in job_logical:
            used[self.data["extruder_map_table"][logical]] = True
        self.data["extruders_used"] = used
        self.data["reprint_info"]["extruders_used"] = list(used)


class Heater:
    """A heater's three published fields, heaters.py:188-194.

    The approach to target is SIMULATED. power is a 0.0 to 1.0 PWM duty, not a
    percent.
    """

    # SIMULATED time constants, in seconds.
    HEAT_TAU = 6.0
    COOL_TAU = 40.0
    AMBIENT_C = 25.0

    def __init__(self, temperature: float = AMBIENT_C) -> None:
        self.temperature = temperature
        self.target = 0.0
        self.power = 0.0

    def advance(self, dt: float) -> None:
        goal = self.target if self.target > 0.0 else self.AMBIENT_C
        tau = self.HEAT_TAU if goal > self.temperature else self.COOL_TAU
        step = 1.0 - math.exp(-dt / tau) if dt > 0 else 0.0
        self.temperature += (goal - self.temperature) * step
        if self.target <= 0.0:
            self.power = 0.0
        else:
            gap = self.target - self.temperature
            self.power = max(0.0, min(1.0, gap / 20.0)) if gap > 0.5 else 0.08

    def status(self) -> dict[str, float]:
        return {
            "temperature": round(self.temperature, 2),
            "target": round(self.target, 2),
            "power": round(self.power, 4),
        }


class PrinterModel:
    """Everything the simulator can report, plus the writes that change it."""

    KLIPPY_STATES = ("ready", "startup", "shutdown", "error")

    def __init__(
        self,
        hostname: str = "u1sim",
        software_version: str = "v0.12.0-u1sim",
        gcode_path: str = "/tmp/u1sim/gcodes",
        config_file: str = "/tmp/u1sim/printer.cfg",
        log_file: str = "/tmp/u1sim/klippy.log",
    ) -> None:
        self.hostname = hostname
        self.software_version = software_version
        self.gcode_path = gcode_path
        self.config_file = config_file
        self.log_file = log_file

        self.klippy_state = "ready"
        self.state_message = "Printer is ready"

        self.ptc = PrintTaskConfig()
        self.filament_info: list[dict[str, Any]] = [
            copy.deepcopy(FILAMENT_INFO_STRUCT) for _ in range(PHYSICAL_EXTRUDER_NUM)
        ]
        self.scan_state = [SCAN_STATE_IDLE] * PHYSICAL_EXTRUDER_NUM
        self.detect_config = {"startup_stay": False}
        # filament_switch_sensor.py:162-165, one section per slot in
        # lava/printer.cfg (e0_filament at :773, e1 at :924, e2 at :1063,
        # e3 at :1201).
        self.slot_sensor = [
            {"filament_detected": False, "enabled": True} for _ in range(PHYSICAL_EXTRUDER_NUM)
        ]

        self.heaters: dict[str, Heater] = {
            "extruder": Heater(),
            "extruder1": Heater(),
            "extruder2": Heater(),
            "extruder3": Heater(),
            "heater_bed": Heater(),
        }
        self.nozzle_diameter = [0.4] * PHYSICAL_EXTRUDER_NUM
        self.active_head = 0
        self.active_logical = 0
        self.tool_changes = 0

        self.print_state = "standby"
        self.print_filename = ""
        self.print_message = ""
        self.print_exception: dict[str, Any] = {}
        self.total_duration = 0.0
        self.print_duration = 0.0
        self.filament_used = 0.0
        self.total_layer: int | None = None
        self.current_layer: int | None = None
        self.file_size = 0
        self.file_position = 0
        self.job_duration = 0.0
        self.job_filament_mm = 0.0
        self.job_logical: list[int] = []
        self.is_paused = False
        # SIMULATED per logical colour usage. Not on the firmware API.
        self.usage_mm = [0.0] * LOGICAL_EXTRUDER_NUM

        self.main_state = 0  # machine_state_manager.py:11, MachineMainState.IDLE
        self.action_code = 0  # machine_state_manager.py:33, ActionCode.IDLE
        self.exceptions: list[dict[str, Any]] = []

        self.fan_speed = 0.0
        self.speed_factor = 1.0
        self.led_color = (0.0, 0.0, 0.0, 0.0)
        self.gcode_log: list[str] = []
        # Used when a client starts a print and the simulator has to invent the
        # things a real sliced file would supply. A scenario can override it.
        self.default_job: dict[str, Any] = {
            "duration": 600.0,
            "total_layer": 120,
            "file_size": 4_194_304,
            "filament_mm": 12000.0,
        }

    # ---- lifecycle ---------------------------------------------------
    @property
    def printing(self) -> bool:
        """The state the firmware's write guards test (print_stats.state)."""
        return self.print_state in ("printing", "paused")

    def set_klippy_state(self, state: str, message: str | None = None) -> None:
        """klippy.py:293-302 allows exactly ready, startup, shutdown and error.

        Moonraker raises on any other string (moonraker/common.py:138-154).
        """
        if state not in self.KLIPPY_STATES:
            raise ValueError(f"unknown klippy state {state!r}")
        self.klippy_state = state
        if message is not None:
            self.state_message = message
        elif state == "ready":
            self.state_message = "Printer is ready"
        elif state == "startup":
            self.state_message = "Printer is not ready"
        elif state == "shutdown":
            self.state_message = "Printer is shutdown"
        else:
            self.state_message = "Printer is in an error state"

    def emergency_stop(self) -> None:
        """webhooks.py:384-385 invokes a shutdown with this exact message."""
        self.set_klippy_state("shutdown", "Shutdown due to webhooks request")

    def refresh_derived(self) -> None:
        """What print_task_config.get_status does on every read (:499-504)."""
        self.ptc.set_filament_exist(
            [
                bool(self.slot_sensor[slot]["filament_detected"])
                or not self.slot_sensor[slot]["enabled"]
                for slot in range(PHYSICAL_EXTRUDER_NUM)
            ]
        )
        self.ptc.update_filament_edit_flag()

    # ---- slots and tags ----------------------------------------------
    def load_slot(self, slot: int, present: bool = True) -> None:
        self._check_slot(slot)
        self.slot_sensor[slot]["filament_detected"] = bool(present)
        self.refresh_derived()

    def scan_tag(self, slot: int, fields: dict[str, Any]) -> bool:
        """Present a tag to a slot's NFC reader.

        fields overlays FILAMENT_INFO_STRUCT, so a scenario only names what the
        tag carries. Unknown keys are rejected rather than silently stored,
        because the firmware's struct is closed.
        """
        self._check_slot(slot)
        unknown = sorted(set(fields) - set(FILAMENT_INFO_STRUCT))
        if unknown:
            raise ValueError(f"tag has fields the firmware struct does not: {unknown}")
        if "MAIN_TYPE" in fields and fields["MAIN_TYPE"] not in (*MAIN_TYPES, EMPTY_MARKER):
            main_type = fields["MAIN_TYPE"]
            raise ValueError(f"MAIN_TYPE {main_type!r} is not in the firmware table")
        if "SUB_TYPE" in fields and fields["SUB_TYPE"] not in (*SUB_TYPES, EMPTY_MARKER):
            sub_type = fields["SUB_TYPE"]
            raise ValueError(f"SUB_TYPE {sub_type!r} is not in the firmware table")
        info = copy.deepcopy(FILAMENT_INFO_STRUCT)
        info.update(fields)
        self.filament_info[slot] = info
        self.scan_state[slot] = SCAN_STATE_IDLE
        changed = self.ptc.rfid_update(slot, info)
        self.load_slot(slot, True)
        return changed

    def clear_slot(self, slot: int) -> None:
        """Remove a spool: the tag record goes back to defaults and the sensor clears."""
        self._check_slot(slot)
        self.filament_info[slot] = copy.deepcopy(FILAMENT_INFO_STRUCT)
        self.scan_state[slot] = SCAN_STATE_IDLE
        for key, default in (
            ("filament_vendor", EMPTY_MARKER),
            ("filament_type", EMPTY_MARKER),
            ("filament_sub_type", EMPTY_MARKER),
            ("filament_color", 0xFFFFFFFF),
            ("filament_color_rgba", "FFFFFFFF"),
            ("filament_official", False),
            ("filament_sku", 0),
            ("filament_soft", False),
        ):
            self.ptc.data[key][slot] = default
        self.ptc.data["filament_color_multi"][slot] = {
            "nums": 1,
            "alpha": 0xFF,
            "mode": 0,
            "colors": ["FFFFFF"],
        }
        self.load_slot(slot, False)

    def set_scan_state(self, slot: int, state: int) -> None:
        self._check_slot(slot)
        if state not in (SCAN_STATE_IDLE, SCAN_STATE_DETECTING, SCAN_STATE_SELF_TESTING):
            raise ValueError(f"scan state {state!r} is not 0, 1 or 2")
        self.scan_state[slot] = state

    @staticmethod
    def _check_slot(slot: int) -> None:
        if not 0 <= slot < PHYSICAL_EXTRUDER_NUM:
            raise ValueError(f"slot {slot!r} is outside 0..{PHYSICAL_EXTRUDER_NUM - 1}")

    # ---- tool changes ------------------------------------------------
    def tool_change(self, logical: int, use_map: bool = True) -> int:
        """Select a logical colour and move to whichever head serves it.

        A bare T0..T3 defaults A to 1 and resolves through extruder_map_table
        (kinematics/extruder.py:1216-1230). T4..T31 are macros that resolve the
        map then force A=0 on the resolved extruder. A0 on T0..T3 bypasses the
        map, which is what use_map=False models. So a raw T number is a colour
        index and not a physical head.
        """
        if not 0 <= logical < LOGICAL_EXTRUDER_NUM:
            raise WebRequestError(
                f"[print_task_config] index out of range[0,{LOGICAL_EXTRUDER_NUM - 1}]"
            )
        if use_map:
            head = self.ptc.map_index(logical)
        else:
            if logical >= PHYSICAL_EXTRUDER_NUM:
                raise WebRequestError(f"extruder{logical} does not exist")
            head = logical
        self.active_logical = logical
        if head != self.active_head:
            self.tool_changes += 1
        self.active_head = head
        return head

    def extruder_name(self, head: int) -> str:
        return "extruder" if head == 0 else f"extruder{head}"

    # ---- the job -----------------------------------------------------
    def start_print(
        self,
        filename: str,
        duration: float = 600.0,
        total_layer: int | None = None,
        file_size: int = 0,
        filament_mm: float = 0.0,
        logical: list[int] | None = None,
    ) -> None:
        """Move to the printing state. print_stats.py:140 sets state printing."""
        self.print_filename = filename
        self.print_state = "printing"
        self.print_message = ""
        self.print_exception = {}
        self.is_paused = False
        self.total_duration = 0.0
        self.print_duration = 0.0
        self.filament_used = 0.0
        self.total_layer = total_layer
        self.current_layer = 0 if total_layer else None
        self.file_size = file_size
        self.file_position = 0
        self.job_duration = max(1.0, float(duration))
        self.job_filament_mm = float(filament_mm)
        self.job_logical = list(logical or [self.active_logical])
        self.usage_mm = [0.0] * LOGICAL_EXTRUDER_NUM
        self.ptc.set_used_extruders(self.job_logical)
        self.main_state = 1  # MachineMainState.PRINTING
        self.action_code = 0

    def pause_print(self) -> None:
        if self.print_state == "printing":
            self.print_state = "paused"  # print_stats.py:151
            self.is_paused = True

    def resume_print(self) -> None:
        if self.print_state == "paused":
            self.print_state = "printing"
            self.is_paused = False

    def cancel_print(self) -> None:
        if self.print_state in ("printing", "paused"):
            self.print_state = "cancelled"  # print_stats.py:163
        self.is_paused = False
        self._end_job()

    def complete_print(self) -> None:
        self.print_state = "complete"  # print_stats.py:157
        self.is_paused = False
        if self.total_layer:
            self.current_layer = self.total_layer
        self.file_position = self.file_size
        self._end_job()

    def error_print(self, message: str) -> None:
        self.print_state = "error"  # print_stats.py:159
        self.print_message = message
        self.is_paused = False
        self._end_job()

    def _end_job(self) -> None:
        self.main_state = 0
        self.action_code = 0
        for heater in self.heaters.values():
            heater.target = 0.0

    def advance(self, dt: float) -> None:
        """Move simulated time forward by dt seconds.

        Heaters approach their targets. While printing the durations, progress,
        layer, file position and filament totals all move. Filament
        goes on the account of the logical colour that is active, which is how
        the simulator produces a per colour breakdown from tool changes.
        """
        if dt <= 0:
            return
        for heater in self.heaters.values():
            heater.advance(dt)
        if self.print_state == "printing":
            self.total_duration += dt
            self.print_duration += dt
            if self.job_filament_mm > 0.0:
                used = self.job_filament_mm * (dt / self.job_duration)
                self.filament_used += used
                self.usage_mm[self.active_logical] += used
            fraction = min(1.0, self.print_duration / self.job_duration)
            if self.file_size:
                self.file_position = int(self.file_size * fraction)
            if self.total_layer:
                reached = int(self.total_layer * fraction) + 1
                self.current_layer = max(1, min(self.total_layer, reached))
            if fraction >= 1.0:
                self.complete_print()
        elif self.print_state == "paused":
            self.total_duration += dt

    @property
    def progress(self) -> float:
        """virtual_sdcard.progress, 0.0 to 1.0. file_position over file_size."""
        if self.file_size:
            return min(1.0, self.file_position / self.file_size)
        if self.print_state in ("printing", "paused") and self.job_duration:
            return min(1.0, self.print_duration / self.job_duration)
        if self.print_state == "complete":
            return 1.0
        return 0.0

    def usage_report(self) -> list[dict[str, Any]]:
        """SIMULATED per colour usage. Not a firmware field, see README."""
        report = []
        for logical, used in enumerate(self.usage_mm):
            if used <= 0.0:
                continue
            head = self.ptc.data["extruder_map_table"][logical]
            filament_type = self.ptc.data["filament_type"][head]
            report.append(
                {
                    "logical_extruder": logical,
                    "head": head,
                    "filament_type": None if filament_type == EMPTY_MARKER else filament_type,
                    "used_mm": round(used, 3),
                    "used_g": round(grams_from_mm(used, filament_type), 3),
                }
            )
        return report

    # ---- the status surface ------------------------------------------
    def objects(self) -> dict[str, dict[str, Any]]:
        """Every printer object the simulator publishes, name to status.

        The names match the objects a U1 running lava/printer.cfg exposes.
        Anything not in here answers {} on a query, which is what Klippy does
        for an unknown object (webhooks.py:517-519).
        """
        self.refresh_derived()
        active = self.extruder_name(self.active_head)
        status: dict[str, dict[str, Any]] = {
            "webhooks": {
                "state": self.klippy_state,
                "state_message": self.state_message,
            },
            "configfile": self._configfile_status(),
            "print_task_config": self.ptc.snapshot(),
            "filament_detect": {
                "info": [dict(info) for info in self.filament_info],
                "state": list(self.scan_state),
                "config": dict(self.detect_config),
            },
            "print_stats": {
                "filename": self.print_filename,
                "total_duration": round(self.total_duration, 3),
                "print_duration": round(self.print_duration, 3),
                "filament_used": round(self.filament_used, 4),
                "state": self.print_state,
                "exception": copy.deepcopy(self.print_exception),
                "message": self.print_message,
                "info": {
                    "total_layer": self.total_layer,
                    "current_layer": self.current_layer,
                },
            },
            "virtual_sdcard": {
                "file_path": self._file_path(),
                "progress": round(self.progress, 6),
                "is_active": self.print_state == "printing",
                "file_position": self.file_position,
                "file_size": self.file_size,
                "pl_env_valid": False,
            },
            "display_status": {
                "progress": round(self.progress, 6),
                "message": self.print_message or None,
            },
            "pause_resume": {"is_paused": self.is_paused},
            "idle_timeout": {
                "state": "Printing" if self.print_state == "printing" else "Ready",
                "printing_time": round(self.print_duration, 3),
            },
            "toolhead": self._toolhead_status(active),
            "gcode_move": self._gcode_move_status(),
            "heater_bed": self.heaters["heater_bed"].status(),
            "heaters": {
                "available_heaters": [
                    "extruder",
                    "extruder1",
                    "extruder2",
                    "extruder3",
                    "heater_bed",
                ],
                "available_sensors": [
                    "extruder",
                    "extruder1",
                    "extruder2",
                    "extruder3",
                    "heater_bed",
                    "temperature_sensor cavity",
                ],
                "available_monitors": [],
            },
            "temperature_sensor cavity": {
                "temperature": round(self.heaters["heater_bed"].AMBIENT_C, 0),
                "measured_min_temp": round(self.heaters["heater_bed"].AMBIENT_C, 0),
                "measured_max_temp": round(self.heaters["heater_bed"].AMBIENT_C, 0),
            },
            "fan": {"speed": round(self.fan_speed, 4), "rpm": None},
            "fan_generic cavity_fan": {"speed": 0.0, "rpm": None},
            "led cavity_led": {"color_data": [list(self.led_color)]},
            "exception_manager": {"exceptions": copy.deepcopy(self.exceptions)},
            "machine_state_manager": {
                "main_state": self.main_state,
                "action_code": self.action_code,
            },
        }
        for head in range(PHYSICAL_EXTRUDER_NUM):
            status[self.extruder_name(head)] = self._extruder_status(head)
            status[f"filament_motion_sensor e{head}_filament"] = dict(self.slot_sensor[head])
        # Head 0 uses the plain [fan] section, heads 1 to 3 have their own
        # fan_generic sections (lava/printer.cfg:726, :879, :1018, :1156).
        for head in (1, 2, 3):
            status[f"fan_generic e{head}_fan"] = {"speed": 0.0, "rpm": None}
        status.update(self._filament_feed_status())
        return status

    def _file_path(self) -> str | None:
        if self.print_state in ("printing", "paused") and self.print_filename:
            return f"{self.gcode_path.rstrip('/')}/{self.print_filename}"
        return None

    def _configfile_status(self) -> dict[str, Any]:
        """configfile.py:351-356.

        Moonraker reads config.virtual_sdcard.path and hands it to the file
        manager (klippy_connection.py:583-590). A mismatch is only a warning,
        but pointing it at the real gcode directory keeps the log clean.
        """
        config = {
            "virtual_sdcard": {"path": self.gcode_path},
            "printer": {"kinematics": "corexy"},
            "print_task_config": {},
            "filament_detect": {},
            "pause_resume": {},
            "display_status": {},
        }
        return {
            "config": config,
            "settings": copy.deepcopy(config),
            "warnings": [],
            "save_config_pending": False,
            "save_config_pending_items": {},
        }

    def _toolhead_status(self, active: str) -> dict[str, Any]:
        """toolhead.py:625-638 plus the kinematics block (cartesian.py:124-130)."""
        return {
            "homed_axes": "xyz",
            "axis_minimum": [0.0, 0.0, 0.0, 0.0],
            "axis_maximum": [300.0, 300.0, 300.0, 0.0],
            "print_time": round(self.print_duration, 3),
            "stalls": 0,
            "estimated_print_time": round(self.print_duration, 3),
            "extruder": active,
            "position": [150.0, 150.0, 10.0, 0.0],
            "max_velocity": 500.0,
            "max_accel": 10000.0,
            "minimum_cruise_ratio": 0.5,
            "square_corner_velocity": 5.0,
        }

    def _gcode_move_status(self) -> dict[str, Any]:
        """gcode_move.py:99-110."""
        return {
            "speed_factor": self.speed_factor,
            "speed": 1500.0,
            "extrude_factor": 1.0,
            "absolute_coordinates": True,
            "absolute_extrude": True,
            "homing_origin": [0.0, 0.0, 0.0, 0.0],
            "position": [150.0, 150.0, 10.0, 0.0],
            "gcode_position": [150.0, 150.0, 10.0, 0.0],
        }

    def _extruder_status(self, head: int) -> dict[str, Any]:
        """kinematics/extruder.py:709-730 plus the stepper block at :302-305.

        switch_count and the other counters are absent on purpose: they come
        from an extruder_switch_recorder object (:720-725) that does not exist
        in this fork snapshot, so a real U1 does not publish them either.
        """
        heater = self.heaters[self.extruder_name(head)]
        parked = head != self.active_head
        status: dict[str, Any] = heater.status()
        status.update(
            {
                "can_extrude": heater.temperature >= 170.0,
                "extruder_index": head,
                "nozzle_diameter": self.nozzle_diameter[head],
                "printing_e_pos": 0.0,
                "activating_move": False,
                # park_detector.py:70-84
                "state": "PARKED" if parked else "ACTIVATE",
                "park_pin": parked,
                "active_pin": not parked,
                "grab_valid_pin": not parked,
                "real_extruder_stats": self.extruder_name(self.active_head),
                "extruder_offset": [0.0, 0.0, 0.0],
                "pressure_advance": 0.02,
                "smooth_time": 0.04,
                "motion_queue": self.extruder_name(head) if not parked else None,
            }
        )
        return status

    def _filament_feed_status(self) -> dict[str, Any]:
        """filament_feed.py:1601-1626.

        Two modules, each serving two heads. lava/printer.cfg:1233-1234 puts
        heads 1 and 0 on left and :1272-1273 puts heads 2 and 3 on right. The
        keys are always numeric so head 0 appears as extruder0.
        """

        def entry(head: int) -> dict[str, Any]:
            return {
                "module_exist": True,
                "filament_detected": bool(self.slot_sensor[head]["filament_detected"]),
                "disable_auto": False,
                "channel_state": 0,
                "channel_error": 0,
                "channel_error_state": 0,
                "channel_action_state": 0,
            }

        return {
            "filament_feed left": {"extruder1": entry(1), "extruder0": entry(0)},
            "filament_feed right": {"extruder2": entry(2), "extruder3": entry(3)},
        }

    def debug_view(self) -> dict[str, Any]:
        """Simulator introspection. NOT a firmware endpoint.

        A real U1 has no equivalent. It exists so a test or a demo can read the
        per colour usage the simulator accumulated, which the firmware keeps in
        print_task_config_2 and never publishes (print_task_config.py:63-75,
        get_status at :503 returns print_task_config only).
        """
        return {
            "u1sim_version": __import__("u1sim").__version__,
            "klippy_state": self.klippy_state,
            "active_head": self.active_head,
            "active_logical": self.active_logical,
            "tool_changes": self.tool_changes,
            "print_state": self.print_state,
            "progress": round(self.progress, 6),
            "filament_used_mm_total": round(self.filament_used, 4),
            "per_colour_usage": self.usage_report(),
            "job_logical_extruders": list(self.job_logical),
            "note": "per_colour_usage is simulated and has no firmware equivalent",
        }
