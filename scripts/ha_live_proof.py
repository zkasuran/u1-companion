"""Drive a live Home Assistant through onboarding, then set up the U1 integration.

This is the end to end proof. It talks to a real Home Assistant over its own HTTP
API, has it discover the integration, runs the config flow against the Moonraker
container, then reads back every entity that appeared and what each one holds.
Nothing is mocked and nothing is asserted about state the API did not return.

It only makes sense against a throwaway instance, because the first thing it does
is claim the owner account. `docker compose --profile ha up -d` gives you one.

    python scripts/ha_live_proof.py --moonraker-host moonraker

Writes artifacts/home-assistant/ and prints a verdict. Exit code 0 means Home
Assistant created the entities and none of them is unavailable.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

REPO = pathlib.Path(__file__).resolve().parent.parent
CLIENT_ID = "http://127.0.0.1:8123/"
DOMAIN = "snapmaker_u1"
STATE_UNAVAILABLE = "unavailable"
STATE_UNKNOWN = "unknown"

# 59 sensors, 14 binary sensors, 4 switches, 1 select and 4 buttons.
EXPECTED_ENTITIES = 82

# What the simulator's four_color_print scenario has to look like once it is
# printing. Anything here that disagrees is a real defect, in this integration or
# in the simulator, not a flaky number.
EXPECTED_VALUES = {
    "sensor.u1sim_slot_0_filament": "PLA",
    "sensor.u1sim_slot_1_filament": "PLA",
    "sensor.u1sim_slot_2_filament": "PETG",
    "sensor.u1sim_slot_3_filament": "PLA",
    "sensor.u1sim_slot_0_vendor": "Snapmaker",
    "sensor.u1sim_slot_3_vendor": "Generic",
    "sensor.u1sim_slot_0_color": "#000000",
    "sensor.u1sim_slot_1_color": "#F5F0E1",
    "sensor.u1sim_slot_2_color": "#D3232A",
    "sensor.u1sim_slot_3_color": "#1E88E5",
    "sensor.u1sim_slot_0_spool_weight": "1000",
    "sensor.u1sim_slot_0_tag_manufactured": "20260114",
    "sensor.u1sim_slot_0_scan_state": "idle",
    "binary_sensor.u1sim_slot_0_official_spool": "on",
    "binary_sensor.u1sim_slot_3_official_spool": "off",
    "binary_sensor.u1sim_slot_0_filament_present": "on",
    "sensor.u1sim_print_state": "printing",
    "sensor.u1sim_machine_state": "printing",
    "sensor.u1sim_current_file": "u1-four-color-demo.gcode",
    "sensor.u1sim_klipper_state": "ready",
    "select.u1sim_entangle_detection_sensitivity": "medium",
    "switch.u1sim_auto_replenish_filament": "on",
    "switch.u1sim_replenish_ignoring_color": "off",
}

# Values that move while the scenario runs, so they are checked as attributes or
# as ranges rather than as one number. A heater ramps towards its target, so the
# target is the fixed part.
EXPECTED_ATTRIBUTES = {
    ("sensor.u1sim_bed_temperature", "target"): 60.0,
    ("sensor.u1sim_layer", "total"): 240,
    ("sensor.u1sim_slot_3_color", "color_count"): 2,
    ("sensor.u1sim_slot_3_color", "gradient"): True,
    ("sensor.u1sim_slot_0_color", "color_mismatch"): False,
    ("sensor.u1sim_slot_0_vendor", "sku"): 12001,
    ("sensor.u1sim_slot_0_job_filament_estimated", "source"): "slicer_estimate",
}

# (entity, low, high), inclusive.
EXPECTED_RANGES = [
    ("sensor.u1sim_progress", 0.1, 100.0),
    ("sensor.u1sim_bed_temperature", 20.0, 65.0),
    ("sensor.u1sim_head_2_nozzle_temperature", 20.0, 255.0),
    ("sensor.u1sim_filament_used", 1.0, 20000.0),
    ("sensor.u1sim_layer", 1, 240),
    ("sensor.u1sim_active_tool", 0, 3),
    ("sensor.u1sim_slot_0_assigned_colors", 1, 32),
]

# Slot 3 was written by G-code and never scanned, so it has no RFID tag. Per
# colour grams need a sliced file's metadata and the simulator has no uploaded
# file, so all four read unknown. Both are correct, not missing data.
EXPECT_UNKNOWN = [
    "sensor.u1sim_slot_0_job_filament_estimated",
    "sensor.u1sim_slot_1_job_filament_estimated",
    "sensor.u1sim_slot_2_job_filament_estimated",
    "sensor.u1sim_slot_3_job_filament_estimated",
    "sensor.u1sim_slot_3_drying_temperature",
    "sensor.u1sim_slot_3_recommended_nozzle_temperature",
    "sensor.u1sim_slot_3_spool_weight",
    "sensor.u1sim_slot_3_tag_manufactured",
]


class Client:
    """The smallest possible Home Assistant API client."""

    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        self.token: str | None = None

    def request(
        self, method: str, path: str, body: Any = None, form: bool = False, raw: bool = False
    ) -> Any:
        url = f"{self.base}{path}"
        data = None
        headers = {}
        if body is not None:
            if form:
                data = urllib.parse.urlencode(body).encode()
                headers["Content-Type"] = "application/x-www-form-urlencoded"
            else:
                data = json.dumps(body).encode()
                headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = response.read()
        except urllib.error.HTTPError as err:
            detail = err.read().decode(errors="replace")
            raise RuntimeError(f"{method} {path} -> HTTP {err.code}: {detail[:400]}") from err
        if raw or not payload:
            return payload.decode(errors="replace")
        return json.loads(payload)

    def get(self, path: str) -> Any:
        return self.request("GET", path)

    def post(self, path: str, body: Any = None, form: bool = False) -> Any:
        return self.request("POST", path, body, form=form)


def wait_for_api(client: Client, seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            client.get("/api/onboarding")
            return
        except Exception:
            time.sleep(2)
    raise RuntimeError(f"Home Assistant did not answer within {seconds:g}s")


def wait_for_the_printing_window(client: Client, seconds: float) -> float:
    """Wait until the scenario is mid job, which is what the checks describe.

    The simulator's timeline loops, so what a fixed sleep reads depends on how
    long Home Assistant took to boot: on a slow one the job is already finished,
    on a fast one it has not started. Waiting for the state itself makes the run
    say the same thing every time. Returns the seconds waited.
    """
    started = time.monotonic()
    deadline = started + seconds
    last = "no state yet"
    while time.monotonic() < deadline:
        states = {state["entity_id"]: state for state in client.get("/api/states")}
        printing = states.get("sensor.u1sim_print_state", {}).get("state")
        progress = states.get("sensor.u1sim_progress", {}).get("state")
        layer = states.get("sensor.u1sim_layer", {}).get("state")
        last = f"print_state {printing}, progress {progress}, layer {layer}"
        if printing == "printing" and _positive(progress) and _positive(layer):
            waited = time.monotonic() - started
            print(f"  printing after {waited:.1f}s: {last}")
            return waited
        time.sleep(2)
    raise RuntimeError(f"the scenario never reached a running print within {seconds:g}s: {last}")


def _positive(raw: Any) -> bool:
    try:
        return float(raw) > 0.0
    except (TypeError, ValueError):
        return False


def onboard(client: Client) -> None:
    """Claim the owner account and finish the wizard.

    Every step is idempotent enough for this purpose: a step already done comes
    back as an error and is skipped.
    """
    steps = {step["step"]: step["done"] for step in client.get("/api/onboarding")}
    if not steps.get("user", False):
        result = client.post(
            "/api/onboarding/users",
            {
                "client_id": CLIENT_ID,
                "name": "U1 Proof",
                "username": "u1proof",
                "password": "u1proof-throwaway-password",
                "language": "en",
            },
        )
        token = client.post(
            "/auth/token",
            {
                "grant_type": "authorization_code",
                "code": result["auth_code"],
                "client_id": CLIENT_ID,
            },
            form=True,
        )
        client.token = token["access_token"]
    if client.token is None:
        raise RuntimeError("this instance is already onboarded, use a fresh one")
    for path, body in (
        ("/api/onboarding/core_config", None),
        ("/api/onboarding/analytics", None),
        ("/api/onboarding/integration", {"client_id": CLIENT_ID, "redirect_uri": CLIENT_ID}),
    ):
        try:
            client.post(path, body)
        except RuntimeError as err:
            print(f"  {path}: {err}")


def run_config_flow(client: Client, host: str, port: int) -> dict[str, Any]:
    """Start the integration's config flow and complete its one step."""
    start = client.post("/api/config/config_entries/flow", {"handler": DOMAIN})
    flow_id = start["flow_id"]
    print(f"  flow {flow_id} at step {start.get('step_id')!r}")
    done = client.post(
        f"/api/config/config_entries/flow/{flow_id}",
        {"host": host, "port": port, "api_key": "", "use_ssl": False},
    )
    if done.get("type") != "create_entry":
        raise RuntimeError(f"the flow did not create an entry: {json.dumps(done)[:400]}")
    return done


def write(directory: pathlib.Path, name: str, payload: Any) -> None:
    path = directory / name
    text = payload if isinstance(payload, str) else json.dumps(payload, indent=2) + "\n"
    path.write_text(text, encoding="utf-8")
    print(f"wrote {path.relative_to(REPO)} ({len(text)} bytes)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:8123")
    parser.add_argument(
        "--moonraker-host",
        default="moonraker",
        help="host as Home Assistant sees it, the compose service name by default",
    )
    parser.add_argument("--moonraker-port", type=int, default=7125)
    parser.add_argument("--settle", type=float, default=15.0, help="seconds to let entities fill")
    parser.add_argument(
        "--window",
        type=float,
        default=360.0,
        help="how long to wait for the scenario to be mid print before giving up",
    )
    parser.add_argument("--out", default=str(REPO / "artifacts" / "home-assistant"))
    args = parser.parse_args(argv)

    directory = pathlib.Path(args.out)
    directory.mkdir(parents=True, exist_ok=True)
    client = Client(args.base)

    print("== waiting for Home Assistant")
    wait_for_api(client, 180)
    print("== onboarding")
    onboard(client)
    print("== running the config flow")
    entry = run_config_flow(client, args.moonraker_host, args.moonraker_port)
    write(directory, "config-flow-result.json", entry)

    print(f"== letting the coordinator run for {args.settle:g}s")
    time.sleep(args.settle)

    print("== waiting for the scenario to be mid print")
    wait_for_the_printing_window(client, args.window)

    entries = client.get("/api/config/config_entries/entry")
    ours = [item for item in entries if item.get("domain") == DOMAIN]
    write(directory, "config-entries.json", ours)

    states = [state for state in client.get("/api/states") if _is_ours(state)]
    states.sort(key=lambda state: state["entity_id"])
    write(directory, "entity-states.json", states)
    write(
        directory,
        "entity-states.txt",
        "".join(f"{state['entity_id']:58s} {state['state']}\n" for state in states),
    )
    write(directory, "ha-version.json", client.get("/api/config"))

    by_id = {state["entity_id"]: state for state in states}
    unavailable = [state["entity_id"] for state in states if state["state"] == STATE_UNAVAILABLE]
    # A button has no state until it is pressed, so "unknown" is correct there.
    # Everything else that reads unknown has to be a value the printer really
    # does not have.
    unknown = [
        state["entity_id"]
        for state in states
        if state["state"] == STATE_UNKNOWN and not state["entity_id"].startswith("button.")
    ]
    platforms: dict[str, int] = {}
    for state in states:
        platform = state["entity_id"].split(".", 1)[0]
        platforms[platform] = platforms.get(platform, 0) + 1

    checks = [
        ("the config flow created an entry", bool(ours)),
        ("the entry loaded", all(item.get("state") == "loaded" for item in ours)),
        (f"{EXPECTED_ENTITIES} entities were created", len(states) == EXPECTED_ENTITIES),
        ("no entity is unavailable", not unavailable),
        ("only the expected entities read unknown", sorted(unknown) == sorted(EXPECT_UNKNOWN)),
    ]
    # The values themselves. These are the simulator's four_color_print scenario
    # coming through a real Moonraker into a real Home Assistant.
    for entity_id, wanted in EXPECTED_VALUES.items():
        got = by_id.get(entity_id, {}).get("state")
        checks.append((f"{entity_id} is {wanted}", got == wanted))
    for (entity_id, key), wanted in EXPECTED_ATTRIBUTES.items():
        got = by_id.get(entity_id, {}).get("attributes", {}).get(key)
        checks.append((f"{entity_id} attribute {key} is {wanted}", got == wanted))
    for entity_id, low, high in EXPECTED_RANGES:
        raw = by_id.get(entity_id, {}).get("state")
        try:
            value = float(raw)
        except (TypeError, ValueError):
            checks.append((f"{entity_id} is a number in {low} to {high}", False))
            continue
        checks.append((f"{entity_id} is {value} in {low} to {high}", low <= value <= high))

    for name, ok in checks:
        print(f"{'PASS' if ok else 'FAIL'} {name}")
    print(f"{len(states)} entities: " + ", ".join(f"{n} {c}" for n, c in sorted(platforms.items())))
    if unavailable:
        print(f"unavailable: {', '.join(unavailable)}")
    if unknown:
        print(f"unknown: {', '.join(sorted(unknown))}")
    return 0 if all(ok for _name, ok in checks) else 1


def _is_ours(state: Any) -> bool:
    """True when this entity came from our integration.

    The API does not expose the owning integration on a state object, so the
    device name the integration sets is the marker. u1sim is the hostname the
    simulator reports and the integration names the device after it.
    """
    if not isinstance(state, dict):
        return False
    attributes = state.get("attributes", {})
    return isinstance(attributes, dict) and "u1sim" in str(state.get("entity_id", ""))


if __name__ == "__main__":
    raise SystemExit(main())
