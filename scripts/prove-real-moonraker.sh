#!/usr/bin/env bash
# Run the real Snapmaker Moonraker fork on top of u1sim, then capture what it
# returns. No printer, no patched Moonraker.
#
# What it does, in order:
#   1. builds a venv holding the fork's own pinned dependencies (plus httpx,
#      see the note below), unless one is already there
#   2. starts Moonraker first, so you can watch it wait for the Klippy socket
#   3. starts u1sim, which creates that socket
#   4. waits for klippy_connected, then runs scripts/capture_real_payload.py
#   5. stops both and leaves the logs next to the captured payloads
#
# Usage:
#   scripts/prove-real-moonraker.sh [--keep] [--seconds N] [--scenario NAME]
#
#   --keep      leave Moonraker and u1sim running after the capture
#   --seconds   how long to watch the websocket for pushes (default 30)
#   --scenario  u1sim scenario to run (default four_color_print)
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MOONRAKER_SRC="${MOONRAKER_SRC:-$REPO/../_recon/u1-moonraker}"
VENV="${MOONRAKER_VENV:-$REPO/../.mrvenv}"
WORK="${U1_PROOF_DIR:-/tmp/u1-companion-proof}"
OUT="$REPO/artifacts/real-moonraker"
SECONDS_TO_WATCH=30
SCENARIO=four_color_print
KEEP=0

while [ $# -gt 0 ]; do
    case "$1" in
        --keep) KEEP=1; shift ;;
        --seconds) SECONDS_TO_WATCH="$2"; shift 2 ;;
        --scenario) SCENARIO="$2"; shift 2 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

if [ ! -f "$MOONRAKER_SRC/moonraker/server.py" ]; then
    echo "no Moonraker source at $MOONRAKER_SRC" >&2
    echo "clone https://github.com/Snapmaker/U1-Moonraker there. Or point" >&2
    echo "MOONRAKER_SRC at an existing checkout." >&2
    exit 1
fi

if [ ! -x "$VENV/bin/python" ]; then
    echo "== building the Moonraker venv at $VENV"
    python3 -m venv "$VENV"
    "$VENV/bin/pip" -q install --upgrade pip wheel
    "$VENV/bin/pip" -q install -r "$REPO/docker/requirements-moonraker.txt"
fi

rm -rf "$WORK"
mkdir -p "$WORK/data/logs" "$WORK/data/gcodes" "$WORK/data/comms" \
         "$WORK/data/config/snapmaker" "$WORK/data/mqtt" \
         "$WORK/run" "$WORK/tmp" "$OUT"

# Three directories the fork opens files in without creating them first, so
# each one is a hard startup failure or a failed component when it is missing:
#   <data>/config/snapmaker  machine.py:81, :1059   product_info.json
#   <data>/mqtt              client_manager.py:917  client.json
#   the tmp dir              server.py:679          defaults to /userdata/.tmp
sed "s#^klippy_uds_address:.*#klippy_uds_address: $WORK/run/klippy.sock#" \
    "$REPO/docker/moonraker.conf" > "$WORK/moonraker.conf"

cleanup() {
    if [ "$KEEP" = "1" ]; then
        echo "== leaving Moonraker (pid ${MOONRAKER_PID:-none}) and u1sim (pid ${SIM_PID:-none}) up"
        echo "   Moonraker: http://127.0.0.1:7125   config: $WORK/moonraker.conf"
        return
    fi
    [ -n "${SIM_PID:-}" ] && kill "$SIM_PID" 2>/dev/null || true
    [ -n "${MOONRAKER_PID:-}" ] && kill "$MOONRAKER_PID" 2>/dev/null || true
    wait 2>/dev/null || true
}
trap cleanup EXIT

echo "== starting real Moonraker from $MOONRAKER_SRC"
( cd "$MOONRAKER_SRC" && "$VENV/bin/python" -m moonraker \
    -d "$WORK/data" -c "$WORK/moonraker.conf" -t "$WORK/tmp" -n ) \
    > "$WORK/moonraker.stdout" 2>&1 &
MOONRAKER_PID=$!

for _ in $(seq 60); do
    if curl -sf -m 2 http://127.0.0.1:7125/server/info > /dev/null 2>&1; then break; fi
    if ! kill -0 "$MOONRAKER_PID" 2>/dev/null; then
        echo "Moonraker died on startup:" >&2
        tail -30 "$WORK/moonraker.stdout" >&2
        exit 1
    fi
    sleep 0.5
done

echo "== Moonraker is up and has no Klippy yet"
curl -sf -m 5 http://127.0.0.1:7125/server/info \
    | python3 -c 'import json,sys; r=json.load(sys.stdin)["result"]; print("   klippy_connected:", r["klippy_connected"], " klippy_state:", r["klippy_state"])'

echo "== starting u1sim on $WORK/run/klippy.sock"
( cd "$REPO" && python3 -m u1sim --socket "$WORK/run/klippy.sock" \
    --scenario "$SCENARIO" --gcode-path "$WORK/data/gcodes" \
    --config-file "$WORK/data/config/printer.cfg" \
    --log-file "$WORK/data/logs/klippy.log" ) > "$WORK/u1sim.log" 2>&1 &
SIM_PID=$!

ready=0
for _ in $(seq 60); do
    state=$(curl -sf -m 2 http://127.0.0.1:7125/server/info 2>/dev/null \
        | python3 -c 'import json,sys; print(json.load(sys.stdin)["result"]["klippy_state"])' 2>/dev/null || echo "")
    if [ "$state" = "ready" ]; then ready=1; break; fi
    sleep 0.5
done
if [ "$ready" != "1" ]; then
    echo "Moonraker never reported klippy_state ready" >&2
    tail -30 "$WORK/moonraker.stdout" >&2
    tail -20 "$WORK/u1sim.log" >&2
    exit 1
fi
echo "== Moonraker reports klippy_state ready"

echo "== capturing, watching the websocket for ${SECONDS_TO_WATCH}s"
python3 "$REPO/scripts/capture_real_payload.py" --seconds "$SECONDS_TO_WATCH" --out "$OUT"
status=$?

cp "$WORK/moonraker.stdout" "$OUT/moonraker.stdout.log"
cp "$WORK/u1sim.log" "$OUT/u1sim.log"
"$VENV/bin/pip" freeze > "$OUT/moonraker-venv-freeze.txt"
( cd "$MOONRAKER_SRC" && git rev-parse HEAD > "$OUT/moonraker-commit.txt" 2>/dev/null ) || true

echo "== artifacts in $OUT"
exit $status
