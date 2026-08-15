"""A scriptable timeline that drives the simulated printer.

A scenario is a JSON document so it can be read and edited without touching
Python. Steps carry an `at` time in seconds from the start of the run. The
runner applies each one once its time arrives. Between steps the model advances
on its own: heaters approach their targets while a running print consumes
filament on the account of whichever logical colour is active.

Actions and their fields:

  set_state        state, message            klippy state, ready by default
  scan_tag         slot, tag{}               present an NFC tag to a slot
  clear_slot       slot                      remove the spool
  scan_state       slot, state               0 idle, 1 detecting, 2 self testing
  load_filament    slot, present             the slot motion sensor
  set_filament     slot plus write fields    the manual, non RFID path
  set_color_map    logical, head             SET_PRINT_EXTRUDER_MAP
  preferences      any of the five members   set_print_preferences
  set_temperature  heater, target            M104 / M140 equivalent
  set_nozzle       head, diameter            0.2, 0.4, 0.6 or 0.8
  start_print      filename plus job numbers
  tool_change      logical, use_map
  pause / resume / cancel / complete
  error            message
  raise_exception  id, index, code, level, message
  clear_exceptions
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from typing import Any

from .gcode import GcodeDispatcher
from .model import VALID_NOZZLE_DIAMETERS, PrinterModel
from .protocol import WebRequestError

SCENARIO_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scenarios")


class ScenarioError(ValueError):
    """A scenario file the runner cannot honour."""


def scenario_path(name: str) -> str:
    """Resolve a scenario by path or by bundled name."""
    if (os.path.sep in name or name.endswith(".json")) and os.path.exists(name):
        return name
    candidate = os.path.join(SCENARIO_DIR, name)
    if os.path.exists(candidate):
        return candidate
    candidate = os.path.join(SCENARIO_DIR, name + ".json")
    if os.path.exists(candidate):
        return candidate
    raise ScenarioError(f"no scenario named {name!r}")


def available_scenarios() -> list[str]:
    if not os.path.isdir(SCENARIO_DIR):
        return []
    return sorted(os.path.splitext(f)[0] for f in os.listdir(SCENARIO_DIR) if f.endswith(".json"))


class Scenario:
    """A parsed scenario file."""

    def __init__(self, document: dict[str, Any], source: str = "<memory>") -> None:
        if not isinstance(document, dict):
            raise ScenarioError("a scenario must be a JSON object")
        self.source = source
        self.name = document.get("name") or os.path.basename(source)
        self.description = document.get("description", "")
        self.loop = bool(document.get("loop", False))
        self.loop_gap = float(document.get("loop_gap", 5.0))
        self.default_job = document.get("default_job") or {}
        self.steps: list[dict[str, Any]] = []
        raw_steps = document.get("steps")
        if not isinstance(raw_steps, list) or not raw_steps:
            raise ScenarioError("a scenario needs a non empty steps list")
        last_at = -1.0
        for index, step in enumerate(raw_steps):
            if not isinstance(step, dict):
                raise ScenarioError(f"step {index} is not an object")
            if "action" not in step:
                raise ScenarioError(f"step {index} has no action")
            at = float(step.get("at", 0.0))
            if at < last_at:
                raise ScenarioError(f"step {index} goes backwards in time ({at} after {last_at})")
            last_at = at
            entry = dict(step)
            entry["at"] = at
            self.steps.append(entry)
        self.duration = self.steps[-1]["at"]

    @classmethod
    def load(cls, name: str) -> Scenario:
        path = scenario_path(name)
        with open(path, encoding="utf-8") as handle:
            return cls(json.load(handle), source=path)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "source": self.source,
            "loop": self.loop,
            "duration": self.duration,
            "step_count": len(self.steps),
        }


class ScenarioRunner:
    """Applies a scenario's steps as simulated time passes.

    Time is supplied by the caller rather than read from a clock, so a test can
    step a whole print through in milliseconds and the server can drive the same
    code from its event loop.
    """

    def __init__(
        self,
        model: PrinterModel,
        scenario: Scenario | None = None,
        speed: float = 1.0,
    ) -> None:
        self.model = model
        self.scenario = scenario
        self.speed = max(0.0, float(speed))
        self.gcode = GcodeDispatcher(model)
        self.elapsed = 0.0
        self.cursor = 0
        self.laps = 0
        self.applied: list[str] = []
        self.actions: dict[str, Callable[[dict[str, Any]], None]] = {
            "set_state": self._set_state,
            "scan_tag": self._scan_tag,
            "clear_slot": self._clear_slot,
            "scan_state": self._scan_state,
            "load_filament": self._load_filament,
            "set_filament": self._set_filament,
            "set_color_map": self._set_color_map,
            "preferences": self._preferences,
            "set_temperature": self._set_temperature,
            "set_nozzle": self._set_nozzle,
            "start_print": self._start_print,
            "tool_change": self._tool_change,
            "pause": lambda step: self.model.pause_print(),
            "resume": lambda step: self.model.resume_print(),
            "cancel": lambda step: self.model.cancel_print(),
            "complete": lambda step: self.model.complete_print(),
            "error": self._error,
            "raise_exception": self._raise_exception,
            "clear_exceptions": lambda step: self.model.exceptions.clear(),
            "gcode": self._gcode,
        }
        if scenario is not None:
            self._validate(scenario)
            if scenario.default_job:
                self.model.default_job.update(scenario.default_job)

    def _validate(self, scenario: Scenario) -> None:
        """Fail on load rather than halfway through a demo."""
        for index, step in enumerate(scenario.steps):
            action = step["action"]
            if action not in self.actions:
                known = ", ".join(sorted(self.actions))
                raise ScenarioError(
                    f"step {index} has unknown action {action!r}, known actions are {known}"
                )
            if action == "set_nozzle":
                diameter = step.get("diameter")
                if diameter not in VALID_NOZZLE_DIAMETERS:
                    raise ScenarioError(
                        f"step {index} nozzle diameter {diameter!r} is not one of "
                        f"{VALID_NOZZLE_DIAMETERS}"
                    )

    # ---- time --------------------------------------------------------
    def advance(self, dt: float) -> list[str]:
        """Move dt real seconds forward. Returns the actions applied."""
        applied: list[str] = []
        if dt <= 0:
            return applied
        step_time = dt * self.speed
        if self.scenario is None:
            self.model.advance(step_time)
            self.elapsed += step_time
            return applied
        remaining = step_time
        guard = 0
        while remaining > 1e-9:
            guard += 1
            if guard > 10000:
                break
            target = self._next_step_time()
            if target is None or target > self.elapsed + remaining:
                self.model.advance(remaining)
                self.elapsed += remaining
                remaining = 0.0
                continue
            gap = max(0.0, target - self.elapsed)
            self.model.advance(gap)
            self.elapsed += gap
            remaining -= gap
            applied.extend(self._fire_due_steps())
            self._maybe_loop()
        return applied

    def _next_step_time(self) -> float | None:
        assert self.scenario is not None
        if self.cursor >= len(self.scenario.steps):
            if self.scenario.loop:
                return self.scenario.duration + self.scenario.loop_gap
            return None
        return self.scenario.steps[self.cursor]["at"]

    def _fire_due_steps(self) -> list[str]:
        assert self.scenario is not None
        applied: list[str] = []
        while self.cursor < len(self.scenario.steps):
            step = self.scenario.steps[self.cursor]
            if step["at"] > self.elapsed + 1e-9:
                break
            self.cursor += 1
            self.actions[step["action"]](step)
            applied.append(step["action"])
            self.applied.append(step["action"])
        return applied

    def _maybe_loop(self) -> None:
        assert self.scenario is not None
        if not self.scenario.loop or self.cursor < len(self.scenario.steps):
            return
        if self.elapsed + 1e-9 < self.scenario.duration + self.scenario.loop_gap:
            return
        self.reset()
        self.laps += 1

    def reset(self) -> None:
        """Start the timeline again from a clean printer.

        The model is re-initialised in place so every holder of the reference,
        the server included, keeps working.
        """
        self.model.__init__(  # type: ignore[misc]
            hostname=self.model.hostname,
            software_version=self.model.software_version,
            gcode_path=self.model.gcode_path,
            config_file=self.model.config_file,
            log_file=self.model.log_file,
        )
        if self.scenario is not None and self.scenario.default_job:
            self.model.default_job.update(self.scenario.default_job)
        self.elapsed = 0.0
        self.cursor = 0
        self.gcode = GcodeDispatcher(self.model)

    def status(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario.to_dict() if self.scenario else None,
            "elapsed": round(self.elapsed, 3),
            "speed": self.speed,
            "steps_applied": self.cursor,
            "laps": self.laps,
        }

    # ---- actions -----------------------------------------------------
    def _set_state(self, step: dict[str, Any]) -> None:
        self.model.set_klippy_state(step.get("state", "ready"), step.get("message"))

    def _scan_tag(self, step: dict[str, Any]) -> None:
        self.model.scan_tag(int(step["slot"]), dict(step.get("tag") or {}))

    def _clear_slot(self, step: dict[str, Any]) -> None:
        self.model.clear_slot(int(step["slot"]))

    def _scan_state(self, step: dict[str, Any]) -> None:
        self.model.set_scan_state(int(step["slot"]), int(step["state"]))

    def _load_filament(self, step: dict[str, Any]) -> None:
        self.model.load_slot(int(step["slot"]), bool(step.get("present", True)))

    def _set_filament(self, step: dict[str, Any]) -> None:
        self.model.ptc.set_filament_config(
            slot=int(step["slot"]),
            vendor=step.get("vendor"),
            filament_type=step.get("type"),
            sub_type=step.get("sub_type"),
            soft=step.get("soft"),
            rgba=step.get("rgba"),
            color=step.get("color"),
            alpha=step.get("alpha"),
            color_nums=step.get("color_nums"),
            colors=step.get("colors"),
            multi_mode=int(step.get("multi_mode", 0)),
            force=bool(step.get("force", False)),
        )
        self.model.load_slot(int(step["slot"]), True)

    def _set_color_map(self, step: dict[str, Any]) -> None:
        self.model.ptc.set_extruder_map(
            int(step["logical"]), int(step["head"]), self.model.printing
        )

    def _preferences(self, step: dict[str, Any]) -> None:
        params = {k: v for k, v in step.items() if k not in ("action", "at")}
        self.model.ptc.set_preferences(params)

    def _set_temperature(self, step: dict[str, Any]) -> None:
        name = step.get("heater", "extruder")
        if name not in self.model.heaters:
            raise ScenarioError(f"no heater named {name!r}")
        self.model.heaters[name].target = float(step.get("target", 0.0))

    def _set_nozzle(self, step: dict[str, Any]) -> None:
        self.model.nozzle_diameter[int(step["head"])] = float(step["diameter"])

    def _start_print(self, step: dict[str, Any]) -> None:
        self.model.start_print(
            filename=step["filename"],
            duration=float(step.get("duration", self.model.default_job["duration"])),
            total_layer=step.get("total_layer", self.model.default_job["total_layer"]),
            file_size=int(step.get("file_size", self.model.default_job["file_size"])),
            filament_mm=float(step.get("filament_mm", self.model.default_job["filament_mm"])),
            logical=step.get("logical"),
        )

    def _tool_change(self, step: dict[str, Any]) -> None:
        self.model.tool_change(int(step["logical"]), use_map=bool(step.get("use_map", True)))

    def _error(self, step: dict[str, Any]) -> None:
        self.model.error_print(step.get("message", "simulated print failure"))

    def _raise_exception(self, step: dict[str, Any]) -> None:
        """Moonraker subscripts id, index, code, level and message with no
        defaults (u1-moonraker exception_manager.py:261-265), so all five are
        required here rather than optional."""
        entry = {}
        for key in ("id", "index", "code", "level"):
            if key not in step:
                raise ScenarioError(f"raise_exception needs {key!r}")
            entry[key] = int(step[key])
        entry["message"] = str(step.get("message", ""))
        self.model.exceptions.append(entry)

    def _gcode(self, step: dict[str, Any]) -> None:
        """Run a script. A refusal is recorded rather than raised.

        A scenario is allowed to attempt something the firmware refuses, for
        example a colour map change during a print, so the refusal itself can be
        demonstrated. Letting it propagate would stop the timeline.
        """
        try:
            self.gcode.run_script(step["script"])
        except WebRequestError as exc:
            self.model.gcode_log.append(f"!! refused: {exc}")
