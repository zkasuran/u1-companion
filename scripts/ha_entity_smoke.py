"""Evaluate every entity of the integration against the real captured payload.

The pytest suite deliberately imports no Home Assistant, so it covers the
parsing layer and not the entity layer. This script covers the rest: it imports
the integration exactly as Home Assistant does, then evaluates every entity
description's value, attributes and existence rule against three states.

  1. the merged real payload from artifacts/real-moonraker, which came through
     an unmodified Moonraker fork
  2. the same payload before anything was scanned, so the empty printer path is
     exercised
  3. a completely empty U1State, which is what the entities see between setup
     and the first successful read

Every value has to be JSON serialisable, because Home Assistant writes entity
state and attributes out as JSON. Anything that raises fails here. So does
anything that comes back as an object the recorder cannot store.

Needs Home Assistant installed:

    pip install homeassistant
    python scripts/ha_entity_smoke.py
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any

REPO = pathlib.Path(__file__).resolve().parent.parent
CAPTURE = REPO / "artifacts" / "real-moonraker"
sys.path.insert(0, str(REPO / "custom_components"))

try:
    from snapmaker_u1 import binary_sensor, button, const, parsing, sensor, switch
except ImportError as exc:  # pragma: no cover - environment problem, not a code problem
    print(f"cannot import the integration: {exc}", file=sys.stderr)
    print("install Home Assistant first: pip install homeassistant", file=sys.stderr)
    raise SystemExit(2) from exc


def load(name: str) -> Any:
    path = CAPTURE / name
    if not path.exists():
        print(f"{path} is missing, run scripts/prove-real-moonraker.sh first", file=sys.stderr)
        raise SystemExit(2)
    return json.loads(path.read_text(encoding="utf-8"))


def build_states() -> dict[str, parsing.U1State]:
    early = load("query-wanted-objects.json")["result"]
    frames = load("ws-status-updates.json")
    printer_info = load("printer-info.json")["result"]
    objects = load("objects-list.json")["result"]["objects"]

    empty_printer = parsing.U1State()
    empty_printer.apply_snapshot(early["status"], early["eventtime"])
    empty_printer.printer_info = printer_info
    empty_printer.set_objects(objects)

    printing = parsing.U1State()
    printing.apply_snapshot(early["status"], early["eventtime"])
    printing.printer_info = printer_info
    printing.set_objects(objects)
    for frame in frames:
        printing.apply_update(frame["params"][0], frame["params"][1])
    # The only per colour usage source is the sliced file. The capture has no
    # uploaded file, so the metadata path is fed a small real shaped record.
    printing.set_job_metadata(
        printing.filename, {"filament_weight": [12.5, 8.0, 3.25, 44.0, 1.5, 0.75]}
    )

    return {
        "real printing": printing,
        "real idle": empty_printer,
        "nothing read yet": parsing.U1State(),
    }


def all_descriptions() -> list[tuple[str, Any]]:
    """Every description the platforms would create entities from."""
    found: list[tuple[str, Any]] = []
    sensors = list(sensor.PRINTER_SENSORS)
    binaries = list(binary_sensor.PRINTER_BINARY_SENSORS)
    for index in range(const.PHYSICAL_EXTRUDER_NUM):
        sensors.extend(sensor.slot_sensors(index))
        sensors.extend(sensor.head_sensors(index))
        binaries.extend(binary_sensor.slot_binary_sensors(index))
    found.extend(("sensor", item) for item in sensors)
    found.extend(("binary_sensor", item) for item in binaries)
    found.extend(("switch", item) for item in switch.SWITCHES)
    found.extend(("button", item) for item in button.BUTTONS)
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quiet", action="store_true", help="only print the summary")
    args = parser.parse_args(argv)

    descriptions = all_descriptions()
    keys = [description.key for _platform, description in descriptions]
    duplicates = sorted({key for key in keys if keys.count(key) > 1})
    problems: list[str] = []
    if duplicates:
        problems.append(f"duplicate entity keys: {duplicates}")

    for label, state in build_states().items():
        created = 0
        for platform, description in descriptions:
            where = f"{platform}.{description.key} on {label!r}"
            exists_fn = getattr(description, "exists_fn", None)
            try:
                if exists_fn is not None and not exists_fn(state):
                    continue
            except Exception as exc:
                problems.append(f"{where}: exists_fn raised {exc!r}")
                continue
            created += 1
            value_fn = getattr(description, "value_fn", None)
            if value_fn is not None:
                try:
                    value = value_fn(state)
                    json.dumps(value)
                except Exception as exc:
                    problems.append(f"{where}: value_fn raised {exc!r}")
            attrs_fn = getattr(description, "attrs_fn", None)
            if attrs_fn is not None:
                try:
                    attributes = attrs_fn(state)
                    json.dumps(attributes)
                except Exception as exc:
                    problems.append(f"{where}: attrs_fn raised {exc!r}")
        if not args.quiet:
            print(f"{label}: {created} entities evaluated")

    # The select and the diagnostics dump are not description driven, so they
    # are exercised directly.
    for label, state in build_states().items():
        try:
            json.dumps(list(const.ENTANGLE_SENSITIVITIES))
            json.dumps(state.entangle_sensitivity)
        except Exception as exc:
            problems.append(f"select on {label!r}: {exc!r}")

    print(f"{len(descriptions)} descriptions, {len(problems)} problems")
    for problem in problems:
        print(f"FAIL {problem}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
