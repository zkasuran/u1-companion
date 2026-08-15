"""Capture what a real Moonraker returns when u1sim is the Klippy behind it.

Run it while `scripts/prove-real-moonraker.sh` has the pair up. It writes one
file per call into the artifacts directory and prints a short verdict. Nothing
here is hand written into the output: every byte comes off the socket.

The object set is the integration's own WANTED_OBJECTS, read out of
custom_components/snapmaker_u1/const.py, so the capture and the integration can
never drift apart.

Order matters. The reads and both writes happen before the simulated print
starts, because the firmware refuses a colour remap while printing
(klippy/extras/print_task_config.py:511-519). They also happen before the first
RFID scan, so the opening snapshot is the firmware's own empty defaults. Both are
checked rather than assumed. The websocket window then runs long enough to cover
the RFID scans, the remaps and the start of the job. One more full query at the
end gives the test a snapshot to compare the merged deltas against.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys
import time
from typing import Any

import aiohttp

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "custom_components" / "snapmaker_u1"))

from const import WANTED_OBJECTS  # noqa: E402

# A colour remap a client can send, plus one the firmware has to refuse.
GOOD_GCODE = "SET_PRINT_EXTRUDER_MAP CONFIG_EXTRUDER=7 MAP_EXTRUDER=2"
BAD_GCODE = "SET_PRINT_EXTRUDER_MAP CONFIG_EXTRUDER=99 MAP_EXTRUDER=0"


def _write(directory: pathlib.Path, name: str, payload: Any) -> pathlib.Path:
    path = directory / name
    if isinstance(payload, (dict, list)):
        text = json.dumps(payload, indent=2, sort_keys=False) + "\n"
    else:
        text = str(payload)
    path.write_text(text, encoding="utf-8")
    print(f"wrote {path.relative_to(REPO)} ({len(text)} bytes)")
    return path


async def _get(session: aiohttp.ClientSession, base: str, path: str, params: Any = None) -> Any:
    async with session.get(f"{base}{path}", params=params) as response:
        response.raise_for_status()
        return await response.json(content_type=None)


async def _post(
    session: aiohttp.ClientSession, base: str, path: str, params: Any = None
) -> tuple[int, Any]:
    """POST and return (status, body). A refusal is a result, not an exception."""
    async with session.post(f"{base}{path}", params=params) as response:
        return response.status, await response.json(content_type=None)


async def capture(base: str, ws_url: str, directory: pathlib.Path, seconds: float) -> int:
    directory.mkdir(parents=True, exist_ok=True)
    query = {name: ",".join(fields) if fields else "" for name, fields in WANTED_OBJECTS.items()}
    async with aiohttp.ClientSession() as session:
        server_info = await _get(session, base, "/server/info")
        _write(directory, "server-info.json", server_info)
        printer_info = await _get(session, base, "/printer/info")
        _write(directory, "printer-info.json", printer_info)
        objects_list = await _get(session, base, "/printer/objects/list")
        _write(directory, "objects-list.json", objects_list)
        ptc = await _get(session, base, "/printer/objects/query", {"print_task_config": ""})
        _write(directory, "query-print_task_config.json", ptc)
        detect = await _get(session, base, "/printer/objects/query", {"filament_detect": ""})
        _write(directory, "query-filament_detect.json", detect)
        first = await _get(session, base, "/printer/objects/query", query)
        _write(directory, "query-wanted-objects.json", first)

        # The write path, both ways round. Moonraker turns a firmware refusal
        # into HTTP 400 carrying the firmware's own message.
        accepted_status, accepted = await _post(
            session, base, "/printer/gcode/script", {"script": GOOD_GCODE}
        )
        _write(
            directory,
            "post-gcode-accepted.json",
            {"script": GOOD_GCODE, "http_status": accepted_status, "body": accepted},
        )
        refused_status, refused = await _post(
            session, base, "/printer/gcode/script", {"script": BAD_GCODE}
        )
        _write(
            directory,
            "post-gcode-refused.json",
            {"script": BAD_GCODE, "http_status": refused_status, "body": refused},
        )
        after_write = await _get(
            session, base, "/printer/objects/query", {"print_task_config": "extruder_map_table"}
        )
        _write(directory, "query-after-gcode.json", after_write)

        # The websocket half. One subscribe, then every push for a few seconds.
        frames: list[dict[str, Any]] = []
        subscribe_reply: dict[str, Any] | None = None
        async with session.ws_connect(ws_url, heartbeat=25) as websocket:
            await websocket.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "printer.objects.subscribe",
                    "params": {"objects": WANTED_OBJECTS},
                }
            )
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                remaining = max(0.1, deadline - time.monotonic())
                try:
                    message = await asyncio.wait_for(websocket.receive(), remaining)
                except TimeoutError:
                    break
                if message.type is not aiohttp.WSMsgType.TEXT:
                    continue
                payload = message.json()
                if payload.get("id") == 1:
                    subscribe_reply = payload
                    continue
                if payload.get("method") == "notify_status_update":
                    frames.append(payload)
        if subscribe_reply is None:
            print("no reply to printer.objects.subscribe", file=sys.stderr)
            return 1
        _write(directory, "ws-subscribe-reply.json", subscribe_reply)
        _write(directory, "ws-status-updates.json", frames)

        # One more full query, taken after the pushes. A test can merge the
        # deltas into the first snapshot and compare against this one.
        last = await _get(session, base, "/printer/objects/query", query)
        _write(directory, "query-wanted-objects-final.json", last)

    slots = ptc["result"]["status"]["print_task_config"]
    remapped = after_write["result"]["status"]["print_task_config"]["extruder_map_table"]
    delta_objects = {name for frame in frames for name in frame["params"][0]}
    checks = [
        ("klippy_connected", server_info["result"]["klippy_connected"] is True),
        ("klippy_state ready", server_info["result"]["klippy_state"] == "ready"),
        ("no failed Moonraker components", not server_info["result"]["failed_components"]),
        ("no Moonraker startup warnings", not server_info["result"]["warnings"]),
        (
            "no missing klippy requirements",
            not server_info["result"]["missing_klippy_requirements"],
        ),
        ("print_task_config listed", "print_task_config" in objects_list["result"]["objects"]),
        ("filament_detect listed", "filament_detect" in objects_list["result"]["objects"]),
        # The scenario's own timeline puts the first RFID scan several seconds
        # out, so a capture taken this early sees the firmware defaults. If that
        # ever stops being true the capture is late and the tests that read it
        # would be asserting against a printer state nobody intended.
        (
            "the first query landed before the scenario scanned a spool",
            all(vendor == "NONE" for vendor in slots["filament_vendor"]),
        ),
        ("filament_vendor has 4 slots", len(slots["filament_vendor"]) == 4),
        ("filament_color_rgba has 4 slots", len(slots["filament_color_rgba"]) == 4),
        ("filament_color_multi has 4 slots", len(slots["filament_color_multi"]) == 4),
        ("extruder_map_table has 32 entries", len(slots["extruder_map_table"]) == 32),
        ("a colour remap over HTTP was accepted", accepted_status == 200),
        ("the remap reached the printer state", remapped[7] == 2),
        ("an out of range remap was refused", refused_status == 400),
        (
            "subscribe returned print_task_config",
            "print_task_config" in subscribe_reply["result"]["status"],
        ),
        ("at least one push arrived", len(frames) > 0),
        ("print_task_config changed in a push", "print_task_config" in delta_objects),
        ("filament_detect changed in a push", "filament_detect" in delta_objects),
    ]
    failed = [name for name, ok in checks if not ok]
    for name, ok in checks:
        print(f"{'PASS' if ok else 'FAIL'} {name}")
    print(f"{len(frames)} notify_status_update frames in {seconds:g}s")
    print(f"objects seen in pushes: {', '.join(sorted(delta_objects))}")
    for warning in server_info["result"]["warnings"]:
        print(f"Moonraker warning: {warning}")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7125)
    parser.add_argument("--seconds", type=float, default=30.0, help="how long to watch for pushes")
    parser.add_argument(
        "--out",
        default=str(REPO / "artifacts" / "real-moonraker"),
        help="directory for the captured payloads",
    )
    args = parser.parse_args(argv)
    base = f"http://{args.host}:{args.port}"
    ws_url = f"ws://{args.host}:{args.port}/websocket"
    return asyncio.run(capture(base, ws_url, pathlib.Path(args.out), args.seconds))


if __name__ == "__main__":
    raise SystemExit(main())
