"""Capture the README screenshots from the stack that is already running.

Every image comes out of a browser pointed at software that is really running:
Home Assistant on :8123 with this integration configured, plus the Moonraker
container behind it. Nothing is mocked, no value is typed into a picture and
nothing is retouched afterwards. The two payload shots render a file out of
artifacts/, which is itself a capture rather than something hand written.

Bring the stack up and configure the integration first, then run this:

    docker compose --profile ha up -d
    python scripts/ha_live_proof.py --settle 20
    python scripts/capture_screenshots.py

Written for a throwaway Home Assistant, because it logs in with the account
ha_live_proof.py creates.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import time
import urllib.parse
import urllib.request
from html import escape

REPO = pathlib.Path(__file__).resolve().parent.parent
IMG = REPO / "docs" / "img"
BASE = "http://127.0.0.1:8123"
WEBSOCKET = "ws://127.0.0.1:8123/api/websocket"
USERNAME = "u1proof"
PASSWORD = "u1proof-throwaway-password"
CLIENT_ID = "http://127.0.0.1:8123/"
PANEL = "u1-panel"
WIDTH = 1280
HEIGHT = 900

SLOT_ROWS = [
    entity
    for slot in range(4)
    for entity in (
        f"sensor.u1sim_slot_{slot}_filament",
        f"sensor.u1sim_slot_{slot}_vendor",
        f"sensor.u1sim_slot_{slot}_color",
        f"binary_sensor.u1sim_slot_{slot}_filament_present",
        f"binary_sensor.u1sim_slot_{slot}_official_spool",
        f"binary_sensor.u1sim_slot_{slot}_in_use",
    )
]

COLOUR_ROWS = [f"sensor.u1sim_slot_{slot}_color" for slot in range(4)]
COLOUR_ROWS += [f"sensor.u1sim_slot_{slot}_assigned_colors" for slot in range(4)]

JOB_ROWS = [
    "sensor.u1sim_print_state",
    "sensor.u1sim_machine_state",
    "sensor.u1sim_progress",
    "sensor.u1sim_layer",
    "sensor.u1sim_active_tool",
    "sensor.u1sim_current_file",
    "sensor.u1sim_print_duration",
    "sensor.u1sim_filament_used",
    "binary_sensor.u1sim_paused",
    "sensor.u1sim_bed_temperature",
]
JOB_ROWS += [f"sensor.u1sim_head_{head}_nozzle_temperature" for head in range(4)]

CONTROL_ROWS = [
    "switch.u1sim_auto_replenish_filament",
    "switch.u1sim_filament_entangle_detection",
    "switch.u1sim_replenish_ignoring_color",
    "switch.u1sim_turn_off_led_at_the_end",
    "select.u1sim_entangle_detection_sensitivity",
    "button.u1sim_pause",
    "button.u1sim_resume",
    "button.u1sim_cancel",
    "button.u1sim_emergency_stop",
]

TAG_ROWS = [
    entity
    for slot in range(4)
    for entity in (
        f"sensor.u1sim_slot_{slot}_tag_manufactured",
        f"sensor.u1sim_slot_{slot}_spool_weight",
        f"sensor.u1sim_slot_{slot}_drying_temperature",
        f"sensor.u1sim_slot_{slot}_recommended_nozzle_temperature",
        f"sensor.u1sim_slot_{slot}_scan_state",
    )
]

TAG0 = "sensor.u1sim_slot_0_tag_manufactured"


def attribute(entity: str, name: str, key: str) -> dict:
    return {"type": "attribute", "entity": entity, "attribute": key, "name": name}


# One spool's whole NFC reading, as attribute rows. Home Assistant only shows
# attributes in the more info dialog when advanced mode is on, so a card is both
# a better picture and a better thing to keep on a wall panel.
RFID_ROWS = [
    {"entity": TAG0, "name": "Tag manufactured"},
    attribute(TAG0, "Vendor", "vendor"),
    attribute(TAG0, "Manufacturer", "manufacturer"),
    attribute(TAG0, "Material", "filament_type"),
    attribute(TAG0, "Sub type", "sub_type"),
    attribute(TAG0, "Colour", "color"),
    attribute(TAG0, "SKU", "sku"),
    attribute(TAG0, "Official spool", "official"),
    attribute(TAG0, "Card UID", "card_uid"),
    attribute(TAG0, "Tag protocol version", "protocol_version"),
    attribute(TAG0, "RSA key version", "rsa_key_version"),
    attribute(TAG0, "Tray", "tray"),
    {"entity": "sensor.u1sim_slot_0_spool_weight", "name": "Spool weight"},
    {"entity": "sensor.u1sim_slot_0_drying_temperature", "name": "Drying temperature"},
    {
        "entity": "sensor.u1sim_slot_0_recommended_nozzle_temperature",
        "name": "Nozzle, other layers",
    },
]


def view(title: str, path: str, card_title: str, rows: list[str]) -> dict:
    return {
        "title": title,
        "path": path,
        "cards": [{"type": "entities", "title": card_title, "entities": rows}],
    }


DASHBOARD = {
    "views": [
        view("Slots", "slots", "Four slots, as the printer reports them", SLOT_ROWS),
        view("Colours", "colours", "Colour and the logical colour map", COLOUR_ROWS),
        view("Job", "job", "The job on the machine right now", JOB_ROWS),
        view("Tags", "tags", "What the NFC reader got off each spool", TAG_ROWS),
        view("RFID", "rfid", "Slot 0, straight off the NFC tag", RFID_ROWS),
        view("Controls", "controls", "What can be written back", CONTROL_ROWS),
    ]
}


def post(path: str, body: dict, form: bool = False, token: str | None = None) -> dict:
    headers = {}
    if form:
        data = urllib.parse.urlencode(body).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    else:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(f"{BASE}{path}", data=data, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = response.read()
    return json.loads(payload) if payload else {}


def access_token() -> str:
    """Log in the same way the browser does, then swap the code for a token."""
    flow = post(
        "/auth/login_flow",
        {
            "client_id": CLIENT_ID,
            "handler": ["homeassistant", None],
            "redirect_uri": CLIENT_ID,
        },
    )
    step = post(
        f"/auth/login_flow/{flow['flow_id']}",
        {"client_id": CLIENT_ID, "username": USERNAME, "password": PASSWORD},
    )
    if "result" not in step:
        raise RuntimeError(f"login did not finish: {json.dumps(step)[:300]}")
    token = post(
        "/auth/token",
        {
            "grant_type": "authorization_code",
            "code": step["result"],
            "client_id": CLIENT_ID,
        },
        form=True,
    )
    return token["access_token"]


async def _save_dashboard(token: str) -> str:
    """Create the panel these shots use, over the websocket API the UI uses.

    Lovelace has no REST surface, so this is the same call the dashboard editor
    makes. A panel that already exists is left alone and its config overwritten.
    """
    import aiohttp

    session_factory = aiohttp.ClientSession()
    async with session_factory as session, session.ws_connect(WEBSOCKET) as socket:
        await socket.receive_json()  # auth_required
        await socket.send_json({"type": "auth", "access_token": token})
        hello = await socket.receive_json()
        if hello.get("type") != "auth_ok":
            raise RuntimeError(f"the websocket refused the token: {hello}")
        message_id = 1

        async def call(payload: dict) -> dict:
            nonlocal message_id
            payload = dict(payload, id=message_id)
            message_id += 1
            await socket.send_json(payload)
            while True:
                reply = await socket.receive_json()
                if reply.get("id") == payload["id"]:
                    return reply

        created = await call(
            {
                "type": "lovelace/dashboards/create",
                "url_path": PANEL,
                "title": "U1",
                "require_admin": False,
                "show_in_sidebar": True,
                "mode": "storage",
            }
        )
        if not created.get("success"):
            print(f"  dashboard already there: {created.get('error', {}).get('message')}")
        saved = await call({"type": "lovelace/config/save", "url_path": PANEL, "config": DASHBOARD})
        if not saved.get("success"):
            raise RuntimeError(f"saving the dashboard failed: {saved}")
        devices = await call({"type": "config/device_registry/list"})
        ours = [
            device
            for device in devices["result"]
            if "snapmaker_u1" in json.dumps(device.get("identifiers", []))
        ]
        if not ours:
            raise RuntimeError("Home Assistant has no device from this integration")
        return str(ours[0]["id"])


def save_dashboard(token: str) -> str:
    """Write the panel and return the device id, so the device page needs no clicks."""
    return asyncio.run(_save_dashboard(token))


def wait_for_state(token: str, entity_id: str, wanted: str, seconds: float = 240.0) -> None:
    """Block until an entity reads what a shot needs it to read."""
    deadline = time.monotonic() + seconds
    state = None
    while time.monotonic() < deadline:
        request = urllib.request.Request(
            f"{BASE}/api/states/{entity_id}", headers={"Authorization": f"Bearer {token}"}
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            state = json.load(response).get("state")
        if state == wanted:
            return
        time.sleep(3)
    raise RuntimeError(f"{entity_id} never reached {wanted!r}, last {state!r}")


TERMINAL_CSS = """
body { margin: 0; background: #eef1f6; font-family: ui-sans-serif, system-ui, sans-serif; }
.window { width: 1180px; margin: 24px; border-radius: 10px; overflow: hidden;
          box-shadow: 0 18px 40px rgba(20, 30, 60, 0.22); background: #10141c; }
.bar { background: #1c2230; color: #c9d4e8; font-size: 13px; padding: 9px 14px;
       letter-spacing: 0.2px; }
pre { margin: 0; padding: 16px 18px; color: #dfe7f5; background: #10141c;
      font-family: ui-monospace, "DejaVu Sans Mono", monospace; font-size: 13px;
      line-height: 1.45; white-space: pre-wrap; }
.prompt { color: #7fd6a6; }
.tail { color: #8a97ad; }
"""


def render_payload(
    page, title: str, command: str, path: pathlib.Path, out: str, lines: int
) -> None:
    """Draw a committed capture file as an image, with its own command above it.

    The bytes come out of artifacts/, so this is a rendering of evidence in the
    repository rather than a picture of a terminal somebody typed into.
    """
    text = path.read_text(encoding="utf-8").splitlines()
    shown = escape("\n".join(text[:lines]))
    more = "" if len(text) <= lines else f"\n... {len(text) - lines} more lines in {path.name}"
    html = (
        f"<!doctype html><meta charset='utf-8'><style>{TERMINAL_CSS}</style>"
        f"<div class='window'><div class='bar'>{title}</div>"
        f"<pre><span class='prompt'>$</span> {command}\n{shown}"
        f"<span class='tail'>{more}</span></pre></div>"
    )
    page.set_content(html)
    page.wait_for_timeout(150)
    page.query_selector(".window").screenshot(path=str(IMG / out))
    print(f"wrote docs/img/{out}")


def shoot(page, out: str, full_page: bool = False) -> None:
    page.wait_for_timeout(1200)
    page.screenshot(path=str(IMG / out), full_page=full_page)
    print(f"wrote docs/img/{out}")


def login(page) -> None:
    page.goto(BASE, wait_until="domcontentloaded")
    page.wait_for_selector('input[name="username"]', timeout=90000)
    page.fill('input[name="username"]', USERNAME)
    page.fill('input[name="password"]', PASSWORD)
    page.keyboard.press("Enter")
    page.wait_for_selector("home-assistant", timeout=90000)
    page.wait_for_timeout(4000)


def open_panel(page, path: str) -> None:
    page.goto(f"{BASE}/{PANEL}/{path}", wait_until="domcontentloaded")
    page.wait_for_timeout(2500)


def more_info(page, friendly_name: str, out: str) -> None:
    """Open a row's dialog, which is where a user lands after a click."""
    page.get_by_text(friendly_name, exact=True).first.click()
    shoot(page, out)
    page.keyboard.press("Escape")
    page.wait_for_timeout(800)


def config_flow_dialog(page, out: str) -> None:
    """Start the flow from the integrations page and stop at the filled form."""
    page.goto(f"{BASE}/config/integrations/dashboard", wait_until="domcontentloaded")
    page.wait_for_timeout(2500)
    page.get_by_role("button", name="Add integration").first.click()
    page.wait_for_timeout(1500)
    page.keyboard.type("Snapmaker")
    page.wait_for_timeout(2000)
    page.locator("dialog-add-integration").get_by_text("Snapmaker U1", exact=True).first.click()
    page.wait_for_selector('input[name="host"]', timeout=30000)
    page.fill('input[name="host"]', "moonraker")
    shoot(page, out)
    page.keyboard.press("Escape")
    page.wait_for_timeout(1000)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--map-change",
        action="store_true",
        help="capture only the colour map pair, which needs an idle printer",
    )
    args = parser.parse_args(argv)
    IMG.mkdir(parents=True, exist_ok=True)

    from playwright.sync_api import sync_playwright

    token = access_token()
    device_id = save_dashboard(token)
    print(f"== dashboard /{PANEL} saved, device {device_id}")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(
            viewport={"width": WIDTH, "height": HEIGHT}, device_scale_factor=2
        )
        page = context.new_page()
        login(page)

        if args.map_change:
            # The firmware refuses a remap while a job runs
            # (print_task_config.py:511-519), so this pair is taken against the
            # idle_loaded scenario, with the printer loaded and in standby.
            wait_for_state(token, "sensor.u1sim_print_state", "standby")
            open_panel(page, "colours")
            shoot(page, "09-map-change-before.png")
            post(
                "/api/services/snapmaker_u1/set_color_map",
                {"logical": 9, "head": 3},
                token=token,
            )
            page.wait_for_timeout(4000)
            page.reload(wait_until="domcontentloaded")
            shoot(page, "09-map-change-after.png")
            browser.close()
            return 0

        render_payload(
            page,
            "Moonraker on the simulator, in a container",
            "curl -s localhost:7125/server/info | python3 -m json.tool",
            REPO / "artifacts" / "docker-compose" / "server-info.json",
            "01-moonraker-ready.png",
            42,
        )
        render_payload(
            page,
            "The four slots, as the printer answered",
            "curl -s 'localhost:7125/printer/objects/query?print_task_config'"
            " | python3 -m json.tool",
            REPO / "artifacts" / "docker-compose" / "query-print_task_config.json",
            "02-objects-query.png",
            42,
        )
        config_flow_dialog(page, "03-config-flow.png")
        page.goto(f"{BASE}/config/devices/device/{device_id}", wait_until="domcontentloaded")
        shoot(page, "04-device-page.png", full_page=True)
        open_panel(page, "slots")
        shoot(page, "05-four-slots.png")
        open_panel(page, "rfid")
        shoot(page, "06-rfid-identity.png")
        open_panel(page, "colours")
        shoot(page, "07-color-swatches.png")
        open_panel(page, "job")
        shoot(page, "08-print-progress.png")
        open_panel(page, "tags")
        shoot(page, "10-tag-panel.png")
        more_info(page, "u1sim Slot 0 tag manufactured", "10-tag-more-info.png")
        open_panel(page, "controls")
        shoot(page, "11-controls.png")
        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
