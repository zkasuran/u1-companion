# What is in here and how it was produced

Everything in this directory is machine generated output. Nothing was typed by
hand and nothing was edited after the fact. It is the evidence behind the
Verification section of the README.

Date of the run: 2026-08-15. Host: Linux, Python 3.11.5, Docker 29.5.3 with
Compose v5.1.4.

Sources under test, both read at their tip on that date:

| Fork | Commit | Date |
| --- | --- | --- |
| `Snapmaker/U1-Klipper` | `7eb32121f04aa15e7a3eb6ec2d872c9cc6596f1a` | 2026-07-30 |
| `Snapmaker/U1-Moonraker` | `3b8357cf2bf33715b17e0899f953d8cf535b3d3f` | 2026-07-30 |

## No printer was involved

There is no Snapmaker U1 in this loop and no claim in this repository rests on
one. What ran is `u1sim` from this repository, with Snapmaker's own unmodified
Moonraker fork on top of it. Moonraker was not patched: it is bind mounted read
only from the checkout and started with `python -m moonraker` on the fork's own
pinned dependencies. The config in `docker/moonraker.conf` keeps the fork's
sections, minus `[mqtt]` which needs a broker and `[zeroconf]` which needs a LAN,
and points `klippy_uds_address` at the simulator's socket. Nothing else differs.

## real-moonraker/

Produced by `scripts/prove-real-moonraker.sh --seconds 32`, which starts
Moonraker first, then u1sim, waits for `klippy_state: ready` and runs
`scripts/capture_real_payload.py`. `tests/test_real_moonraker_capture.py` reads
these files, so they are test input as well as evidence.

| File | What it is |
| --- | --- |
| `server-info.json` | `GET /server/info`. 29 components loaded, no failures, no warnings |
| `printer-info.json` | `GET /printer/info`, the Klippy info block Moonraker forwards |
| `objects-list.json` | `GET /printer/objects/list`, 32 objects |
| `query-print_task_config.json` | `GET /printer/objects/query?print_task_config` before any spool was scanned |
| `query-filament_detect.json` | the same for the RFID object |
| `query-wanted-objects.json` | one query for every object the integration reads, taken early |
| `post-gcode-accepted.json` | `POST /printer/gcode/script` with a colour remap, HTTP 200 |
| `post-gcode-refused.json` | the same with an out of range index, HTTP 400 and the firmware's own message |
| `query-after-gcode.json` | printer state showing the remap landed |
| `ws-subscribe-reply.json` | the reply to `printer.objects.subscribe` over the websocket |
| `ws-status-updates.json` | all 60 `notify_status_update` pushes from a 32 second window |
| `query-wanted-objects-final.json` | the same full query, taken after the pushes stopped |
| `moonraker.stdout.log` | everything Moonraker printed, start to finish |
| `u1sim.log` | the simulator's own log, including the scenario it ran |
| `moonraker-commit.txt` | the fork commit that produced all of the above |
| `moonraker-venv-freeze.txt` | every installed version, so the run can be repeated |

The last two files are the point of the first thirteen. A payload with no commit
and no dependency list behind it cannot be checked by anyone else.

Why both a first and a final full query: merging the 60 pushes into the first one
has to reproduce the final one field for field, for `print_task_config` and
`filament_detect`. That is the strongest assertion in the test suite and it needs
both ends.

The first query is taken before the scenario has scanned anything, so it holds
the firmware's own empty defaults. The capture script checks that rather than
hoping for it: a capture taken late would quietly change what the tests reading
it are asserting about.

## docker-compose/

The same stack through `docker compose up -d`, which is what the README's
quickstart tells a reader to run. Produced by `scripts/prove-docker-compose.sh`,
which waits for Moonraker's healthcheck and then for the simulated job to be
running before it reads anything, so the payload here shows four loaded slots
instead of the defaults the scenario opens with. Moonraker reported healthy 5
seconds after `up`.

| File | What it is |
| --- | --- |
| `compose-ps.txt` | `docker compose ps` with the health column |
| `moonraker-health.txt` | the container health state |
| `server-info.json` | `GET /server/info` from the container, empty warnings and failures |
| `objects-list.json` | the object list through the container |
| `query-print_task_config.json` | the four slot arrays through the container |
| `moonraker-container.log` | Moonraker's container log |
| `u1sim-container.log` | the simulator's container log |
| `docker-versions.txt` | the Docker and Compose versions used |

## home-assistant/

A real Home Assistant, onboarded and driven through the integration's config flow
by `scripts/ha_live_proof.py`, with the Moonraker container as the printer. This
is the end to end run: simulator, unmodified Moonraker fork, real Home Assistant.

| File | What it is |
| --- | --- |
| `config-flow-result.json` | what the config flow returned, a `create_entry` |
| `config-entries.json` | the entry as Home Assistant stored it, `state: loaded` |
| `entity-states.json` | every entity with its state and attributes, straight off `/api/states` |
| `entity-states.txt` | the same as one line per entity, for reading |
| `ha-version.json` | `/api/config`, the Home Assistant version it ran on |

82 entities were created: 59 sensors, 14 binary sensors, 4 switches, 4 buttons
and 1 select. None is unavailable. The eight that read unknown are the ones that
should: slot 3 has no RFID tag because it was written by G-code. Per colour
grams need a sliced file's metadata which the simulator has no upload for.

## checks/

Raw output of the gates, exactly as they printed.

| File | Gate |
| --- | --- |
| `pytest-and-ruff.txt` | `pytest`, `ruff check .`, `ruff format --check .` |
| `hassfest.txt` | Home Assistant's own validator for a custom integration |
| `ha-entity-smoke.txt` | `scripts/ha_entity_smoke.py`, every entity evaluated against the captured payload on Home Assistant 2025.1.4 |
| `prove-real-moonraker.txt` | `scripts/prove-real-moonraker.sh`, 19 checks against the fork running on the simulator |
| `prove-docker-compose.txt` | `scripts/prove-docker-compose.sh`, 10 checks against the same pair in containers |
| `ha-live-proof.txt` | `scripts/ha_live_proof.py`, 42 checks against a live Home Assistant |

## What none of this shows

- Any behaviour of a physical U1: its timing, its error paths, values a simulator
  never produced.
- Whether `filament_weight[i]` in a sliced file's metadata is logical colour `i`.
  The per slot gram estimates assume it is.
- mDNS discovery. The service type a real U1 advertises was never observed, which
  is why the integration has no discovery step.
- The Home Assistant UI itself. The config flow was driven over the HTTP API
  rather than through a browser, so the dialog's appearance is unverified. Its
  schema, its error strings and its translations are covered by hassfest.

## Repeating it

```bash
git clone https://github.com/Snapmaker/U1-Moonraker vendor/u1-moonraker

# the pair, plus the capture the test suite reads
MOONRAKER_SRC=vendor/u1-moonraker scripts/prove-real-moonraker.sh --seconds 32
pytest tests/test_real_moonraker_capture.py

# the same pair in containers
scripts/prove-docker-compose.sh

# the whole stack including Home Assistant
docker compose --profile ha up -d --build
python scripts/ha_live_proof.py --settle 20
```

Float noise and timestamps will differ. So will the push count, because it
depends on how long Moonraker took to attach. The assertions will not. CI runs
all of it on every push, in the `simulator-with-real-moonraker` and
`home-assistant-live` jobs.
