"""G-code the simulator understands.

Only the commands a client actually drives are implemented. Everything else is
accepted and logged, the way an unknown macro on a real printer would either
run or raise, because a simulator that refuses unfamiliar G-code is worse than
one that ignores it.

Two parsing details are copied from the firmware. Command names are matched
case insensitively on the upper cased line (klippy/gcode.py:214). Extended
parameters are split with a quote aware splitter so FILENAME="a b.gcode" works
(klippy/gcode.py:285-311).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .model import LOGICAL_EXTRUDER_NUM, PHYSICAL_EXTRUDER_NUM, PrinterModel
from .protocol import WebRequestError


def split_params(args: str) -> list[str]:
    """Quote aware whitespace split. klippy/gcode.py:285-311."""
    result: list[str] = []
    current: list[str] = []
    quote_char: str | None = None
    for ch in args:
        if quote_char is not None:
            if ch == quote_char:
                quote_char = None
            else:
                current.append(ch)
        elif ch in ('"', "'"):
            quote_char = ch
        elif ch in (" ", "\t"):
            if current:
                result.append("".join(current))
                current = []
        else:
            current.append(ch)
    if current:
        result.append("".join(current))
    return result


def parse_line(line: str) -> tuple[str, dict[str, str]]:
    """Return the upper cased command plus its KEY to VALUE parameters."""
    line = line.split(";", 1)[0].strip()
    if not line:
        return "", {}
    parts = line.split(None, 1)
    command = parts[0].upper()
    params: dict[str, str] = {}
    if len(parts) > 1:
        for token in split_params(parts[1]):
            if "=" in token:
                key, value = token.split("=", 1)
                params[key.upper()] = value
            else:
                # Bare letter parameters, for example M104 S220 or T2 A0.
                key = token[0].upper()
                params[key] = token[1:]
    return command, params


def _int(params: dict[str, str], key: str, default: int | None = None) -> int | None:
    raw = params.get(key)
    if raw is None or raw == "":
        return default
    try:
        return int(float(raw))
    except ValueError as exc:
        raise WebRequestError(f"Unable to parse '{key}' as an integer") from exc


def _float(params: dict[str, str], key: str, default: float | None = None):
    raw = params.get(key)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise WebRequestError(f"Unable to parse '{key}' as a number") from exc


class GcodeDispatcher:
    """Runs a G-code script against the model.

    A handler raises WebRequestError to refuse a command. Klippy turns a
    command_error from gcode/script into an error reply and, uniquely for that
    endpoint, does not shut the printer down (webhooks.py:269-273).
    """

    def __init__(self, model: PrinterModel) -> None:
        self.model = model
        self.responses: list[str] = []
        self.handlers: dict[str, Callable[[dict[str, str]], None]] = {
            "SET_PRINT_EXTRUDER_MAP": self._set_extruder_map,
            "GET_PRINT_EXTRUDER_MAP": self._get_extruder_map,
            "GET_PRINT_TASK_CONFIG": self._get_print_task_config,
            "SET_PRINT_FILAMENT_CONFIG": self._set_filament_config,
            "SET_PRINT_PREFERENCES": self._set_preferences,
            "SET_PRINT_STATS_INFO": self._set_print_stats_info,
            "SDCARD_PRINT_FILE": self._print_file,
            "SDCARD_PRINT_FILE_WITH_PARAMETERS": self._print_file,
            "PAUSE": self._pause,
            "RESUME": self._resume,
            "CANCEL_PRINT": self._cancel,
            "CLEAR_PAUSE": self._clear_pause,
            "TURN_OFF_HEATERS": self._turn_off_heaters,
            "ACTIVATE_EXTRUDER": self._activate_extruder,
            "QUERY_FILAMENT_SENSOR": self._query_filament_sensor,
            "FLOW_RESET_K": self._noop,
            "G28": self._noop,
            "M104": self._m104,
            "M109": self._m104,
            "M140": self._m140,
            "M190": self._m140,
            "M106": self._m106,
            "M107": self._m107,
            "M114": self._m114,
            "M115": self._m115,
        }

    def run_script(self, script: str) -> list[str]:
        """Run every line of a script. Returns the responses it produced."""
        self.responses = []
        for line in script.replace("\r", "\n").split("\n"):
            command, params = parse_line(line)
            if not command:
                continue
            self.model.gcode_log.append(line.strip())
            del self.model.gcode_log[:-200]
            self._run_one(command, params)
        return list(self.responses)

    def _run_one(self, command: str, params: dict[str, str]) -> None:
        if command.startswith("T") and command[1:].isdigit():
            self._tool_change(int(command[1:]), params)
            return
        handler = self.handlers.get(command)
        if handler is None:
            # Unknown macro. A real printer would either run it or raise. The
            # simulator has no way to know which, so it logs and moves on.
            self.responses.append(f"// u1sim ignored unknown command {command}")
            return
        handler(params)

    def respond_info(self, message: str) -> None:
        """gcode.respond_info sends each line prefixed with // on the wire."""
        for line in message.split("\n"):
            if line:
                self.responses.append(f"// {line}")

    # ---- handlers ----------------------------------------------------
    def _noop(self, params: dict[str, str]) -> None:
        return

    def _tool_change(self, index: int, params: dict[str, str]) -> None:
        """T0 to T31.

        A defaults to 1. A non zero A resolves the index through
        extruder_map_table before switching (kinematics/extruder.py:1216-1230).
        T4 to T31 are macros over SWITCH_OF_EXTENDED_EXTRUDER, which resolve
        the map then force A=0 on the head they resolved to, so the visible
        result is the same.
        """
        if index >= LOGICAL_EXTRUDER_NUM:
            raise WebRequestError(
                f"T{index} is outside the 0 to {LOGICAL_EXTRUDER_NUM - 1} colour range"
            )
        use_map = _int(params, "A", 1) != 0 or index >= PHYSICAL_EXTRUDER_NUM
        head = self.model.tool_change(index, use_map=use_map)
        self.respond_info(f"T{index} -> head {head}")

    def _set_extruder_map(self, params: dict[str, str]) -> None:
        logical = _int(params, "CONFIG_EXTRUDER")
        head = _int(params, "MAP_EXTRUDER")
        self.model.ptc.set_extruder_map(logical, head, self.model.printing)

    def _get_extruder_map(self, params: dict[str, str]) -> None:
        """print_task_config.py:540-544 prints one line per logical colour."""
        table = self.model.ptc.data["extruder_map_table"]
        lines = "".join(f"T{n} -> T{table[n]}\n" for n in range(len(table)))
        self.respond_info(lines)

    def _get_print_task_config(self, params: dict[str, str]) -> None:
        self.respond_info(str(self.model.ptc.data))

    def _set_filament_config(self, params: dict[str, str]) -> None:
        colors = params.get("COLORS")
        self.model.ptc.set_filament_config(
            slot=_int(params, "CONFIG_EXTRUDER", -1),
            vendor=params.get("VENDOR"),
            filament_type=params.get("FILAMENT_TYPE"),
            sub_type=params.get("FILAMENT_SUBTYPE"),
            soft=None if "SOFT" not in params else bool(_int(params, "SOFT", 0)),
            color=_int(params, "FILAMENT_COLOR"),
            rgba=params.get("FILAMENT_COLOR_RGBA"),
            alpha=_int(params, "ALPHA"),
            color_nums=_int(params, "COLOR_NUMS"),
            colors=colors.split(",") if colors else None,
            multi_mode=_int(params, "MULTI_MODE", 0),
            force=bool(_int(params, "FORCE", 0)),
        )

    def _set_preferences(self, params: dict[str, str]) -> None:
        """SET_PRINT_PREFERENCES, print_task_config.py:684-786.

        Only the members the HTTP endpoint also carries are implemented.
        """
        mapped: dict[str, Any] = {}
        for gcode_key, config_key in (
            ("AUTO_REPLENISH_FILAMENT", "auto_replenish_filament"),
            ("REPLENISH_IGNORE_COLOR", "replenish_ignore_color"),
            ("FILAMENT_ENTANGLE_DETECT", "filament_entangle_detect"),
            ("END_LED_TURN_OFF", "end_led_turn_off"),
        ):
            if gcode_key in params:
                mapped[config_key] = _int(params, gcode_key, 0)
        if "FILAMENT_ENTANGLE_SEN" in params:
            mapped["filament_entangle_sen"] = params["FILAMENT_ENTANGLE_SEN"]
        self.model.ptc.set_preferences(mapped)

    def _set_print_stats_info(self, params: dict[str, str]) -> None:
        """SET_PRINT_STATS_INFO TOTAL_LAYER= CURRENT_LAYER=, print_stats.py."""
        total = _int(params, "TOTAL_LAYER")
        current = _int(params, "CURRENT_LAYER")
        if total is not None:
            self.model.total_layer = total
        if current is not None:
            self.model.current_layer = current

    def _print_file(self, params: dict[str, str]) -> None:
        """SDCARD_PRINT_FILE and its WITH_PARAMETERS twin.

        Moonraker's /printer/print/start turns into
        SDCARD_PRINT_FILE FILENAME="<path>" (klippy_apis.py:394). The advanced
        form adds the slicer metadata as extra parameters
        (klippy_apis.py:288-307, :364). virtual_sdcard.py:339-341 strips a
        leading slash. A busy card is refused (:333-335).
        """
        if self.model.print_state in ("printing", "paused"):
            raise WebRequestError("SD busy")
        filename = params.get("FILENAME")
        if not filename:
            raise WebRequestError("Missing Argument [FILENAME]")
        if filename[0] == "/":
            filename = filename[1:]
        used_mm = _parse_number_list(params.get("FILAMENT_USED_MM"))
        defaults = self.model.default_job
        # The U1SIM_ parameters are simulator only knobs so a demo can drive a
        # short print. They are not firmware parameters. Without them the
        # scenario's default_job block supplies the numbers.
        self.model.start_print(
            filename=filename,
            duration=_float(params, "U1SIM_DURATION", defaults["duration"]),
            total_layer=_int(params, "U1SIM_TOTAL_LAYER", defaults["total_layer"]),
            file_size=_int(params, "U1SIM_FILE_SIZE", defaults["file_size"]),
            filament_mm=sum(used_mm) if used_mm else defaults["filament_mm"],
            logical=list(range(len(used_mm))) if used_mm else None,
        )

    def _pause(self, params: dict[str, str]) -> None:
        self.model.pause_print()

    def _resume(self, params: dict[str, str]) -> None:
        self.model.resume_print()

    def _cancel(self, params: dict[str, str]) -> None:
        self.model.cancel_print()

    def _clear_pause(self, params: dict[str, str]) -> None:
        self.model.is_paused = False

    def _turn_off_heaters(self, params: dict[str, str]) -> None:
        for heater in self.model.heaters.values():
            heater.target = 0.0

    def _activate_extruder(self, params: dict[str, str]) -> None:
        name = (params.get("EXTRUDER") or "").lower()
        if name == "extruder":
            head = 0
        elif name.startswith("extruder") and name[8:].isdigit():
            head = int(name[8:])
        else:
            raise WebRequestError(f"Unknown extruder {params.get('EXTRUDER')!r}")
        if head >= PHYSICAL_EXTRUDER_NUM:
            raise WebRequestError(f"extruder{head} does not exist")
        # ACTIVATE_EXTRUDER does not consult the colour map.
        self.model.tool_change(head, use_map=False)

    def _query_filament_sensor(self, params: dict[str, str]) -> None:
        name = params.get("SENSOR", "")
        for slot in range(PHYSICAL_EXTRUDER_NUM):
            if name == f"e{slot}_filament":
                detected = self.model.slot_sensor[slot]["filament_detected"]
                seen = "detected" if detected else "not detected"
                self.respond_info(f"Filament Sensor {name}: filament {seen}")
                return
        raise WebRequestError(f"Unknown sensor '{name}'")

    def _m104(self, params: dict[str, str]) -> None:
        """M104 / M109 S<temp> T<index>. kinematics/extruder.py:582-583."""
        temp = _float(params, "S", 0.0)
        head = _int(params, "T", self.model.active_head)
        if head is None or head >= PHYSICAL_EXTRUDER_NUM:
            raise WebRequestError(f"extruder{head} does not exist")
        self.model.heaters[self.model.extruder_name(head)].target = max(0.0, temp)

    def _m140(self, params: dict[str, str]) -> None:
        """M140 / M190 S<temp>. heater_bed.py:26-33."""
        self.model.heaters["heater_bed"].target = max(0.0, _float(params, "S", 0.0))

    def _m106(self, params: dict[str, str]) -> None:
        """M106 S<0-255>. fan.py:184-186 divides by 255."""
        self.model.fan_speed = min(1.0, max(0.0, _float(params, "S", 255.0) / 255.0))

    def _m107(self, params: dict[str, str]) -> None:
        self.model.fan_speed = 0.0

    def _m114(self, params: dict[str, str]) -> None:
        pos = self.model._gcode_move_status()["gcode_position"]
        self.responses.append(f"X:{pos[0]:.3f} Y:{pos[1]:.3f} Z:{pos[2]:.3f} E:{pos[3]:.3f}")

    def _m115(self, params: dict[str, str]) -> None:
        self.responses.append(
            f"FIRMWARE_NAME:Klipper FIRMWARE_VERSION:{self.model.software_version}"
        )


def _parse_number_list(raw: str | None) -> list[float]:
    """Read a bracketed or comma separated number list out of a parameter.

    Moonraker's advanced start path passes the slicer's own lists straight
    through, for example FILAMENT_USED_MM="[1234.5, 678.9]"
    (klippy_apis.py:288-307).
    """
    if not raw:
        return []
    cleaned = raw.strip().strip("[]")
    out: list[float] = []
    for chunk in cleaned.replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            out.append(float(chunk))
        except ValueError:
            continue
    return out
