#!/usr/bin/env bash
# Bring the pair up with docker compose, the way the README's quickstart tells a
# reader to, then capture what the containers return into
# artifacts/docker-compose/.
#
# It waits twice. First for Moonraker's own healthcheck, which only passes once
# a ready Klippy is behind it. Then for the simulated job to be running, so the
# captured payload shows four loaded slots rather than the firmware defaults the
# scenario opens with.
#
# Usage:
#   scripts/prove-docker-compose.sh [--no-up] [--down]
#
#   --no-up   use the stack that is already running instead of starting one
#   --down    stop the stack and drop its volumes on the way out
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$REPO/artifacts/docker-compose"
BASE="http://127.0.0.1:7125"
HEALTH_TIMEOUT=180
PRINT_TIMEOUT=360
UP=1
DOWN=0

while [ $# -gt 0 ]; do
    case "$1" in
        --no-up) UP=0; shift ;;
        --down) DOWN=1; shift ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

cd "$REPO"
mkdir -p "$OUT"

cleanup() {
    if [ "$DOWN" = "1" ]; then
        echo "== stopping the stack"
        docker compose --profile ha down -v > /dev/null 2>&1 || true
    fi
}
trap cleanup EXIT

field() {
    # Pull one value out of a JSON document on stdin.
    python3 -c "import json,sys
document = json.load(sys.stdin)
for key in sys.argv[1:]:
    document = document[key]
print(document)" "$@"
}

if [ "$UP" = "1" ]; then
    echo "== docker compose up -d --build"
    docker compose up -d --build
fi

echo "== waiting for Moonraker's healthcheck"
started=$(date +%s)
healthy=0
while [ $(( $(date +%s) - started )) -lt "$HEALTH_TIMEOUT" ]; do
    state=$(docker inspect -f '{{.State.Health.Status}}' u1-moonraker 2>/dev/null || echo "")
    if [ "$state" = "healthy" ]; then healthy=$(( $(date +%s) - started )); break; fi
    if [ "$state" = "unhealthy" ]; then
        echo "Moonraker reported unhealthy" >&2
        docker compose logs --no-color --tail 40 >&2
        exit 1
    fi
    sleep 1
done
if [ "$healthy" = "0" ] && [ "$state" != "healthy" ]; then
    echo "Moonraker never became healthy within ${HEALTH_TIMEOUT}s" >&2
    docker compose logs --no-color --tail 40 >&2
    exit 1
fi
echo "== healthy ${healthy}s after up"

echo "== waiting for the simulated job to be running"
printing=""
started=$(date +%s)
while [ $(( $(date +%s) - started )) -lt "$PRINT_TIMEOUT" ]; do
    printing=$(curl -sf -m 5 "$BASE/printer/objects/query?print_stats" 2>/dev/null \
        | field result status print_stats state 2>/dev/null || echo "")
    if [ "$printing" = "printing" ]; then break; fi
    sleep 2
done
if [ "$printing" != "printing" ]; then
    echo "the scenario never reached a running print, last state: ${printing:-none}" >&2
    exit 1
fi

echo "== capturing"
{
    echo '$ docker compose ps'
    docker compose ps
} > "$OUT/compose-ps.txt"
{
    echo "\$ docker inspect -f '{{.State.Health.Status}}' u1-moonraker"
    docker inspect -f '{{.State.Health.Status}}' u1-moonraker
    if [ "$UP" = "1" ]; then
        echo "reached ${healthy}s after docker compose up"
    else
        echo "the stack was already running when this was captured, so the time"
        echo "it took to get there was not measured"
    fi
} > "$OUT/moonraker-health.txt"
curl -sf -m 10 "$BASE/server/info" | python3 -m json.tool > "$OUT/server-info.json"
curl -sf -m 10 "$BASE/printer/objects/list" | python3 -m json.tool > "$OUT/objects-list.json"
curl -sf -m 10 "$BASE/printer/objects/query?print_task_config" \
    | python3 -m json.tool > "$OUT/query-print_task_config.json"
docker compose logs --no-color moonraker > "$OUT/moonraker-container.log"
docker compose logs --no-color u1sim > "$OUT/u1sim-container.log"
{
    docker --version
    docker compose version
} > "$OUT/docker-versions.txt"

python3 - "$OUT" "$healthy" <<'PYTHON'
import json
import pathlib
import sys

out = pathlib.Path(sys.argv[1])
healthy = sys.argv[2]
info = json.loads((out / "server-info.json").read_text())["result"]
objects = json.loads((out / "objects-list.json").read_text())["result"]["objects"]
config = json.loads((out / "query-print_task_config.json").read_text())
config = config["result"]["status"]["print_task_config"]

checks = [
    ("Moonraker is healthy behind a ready Klippy", info["klippy_state"] == "ready"),
    ("klippy_connected", info["klippy_connected"] is True),
    ("no failed Moonraker components", not info["failed_components"]),
    ("no Moonraker startup warnings", not info["warnings"]),
    ("no missing klippy requirements", not info["missing_klippy_requirements"]),
    ("print_task_config listed", "print_task_config" in objects),
    ("filament_detect listed", "filament_detect" in objects),
    ("all four slots are loaded", config["filament_exist"] == [True] * 4),
    ("no slot is still at the empty default", "NONE" not in config["filament_vendor"]),
    ("extruder_map_table has 32 entries", len(config["extruder_map_table"]) == 32),
]
for name, ok in checks:
    print(f"{'PASS' if ok else 'FAIL'} {name}")
print(f"{len(objects)} printer objects, healthy {healthy}s after up")
raise SystemExit(1 if [name for name, ok in checks if not ok] else 0)
PYTHON

echo "== artifacts in $OUT"
