# Contributing

Bug reports and patches are welcome. The project has two halves and they are
tested the same way.

- `u1sim/` is a fake Klippy. It binds a Unix socket and speaks the real Klippy
  API protocol so an unmodified Snapmaker Moonraker can run on top of it.
- `custom_components/snapmaker_u1/` is the Home Assistant integration. It talks
  to Moonraker over HTTP plus the websocket.

## Setup

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
pip install -e .
```

## Before you open a pull request

```bash
ruff format .
ruff check .
pytest
```

CI runs the same three commands on Python 3.11 and 3.12, plus three more jobs.
`ruff format --check` is a required check, so run the formatter rather than
fighting it.

The other three jobs, worth running locally when you touch what they cover:

```bash
# Home Assistant's own validator for the manifest, services and translations
docker run --rm -v "$PWD":/github/workspace ghcr.io/home-assistant/hassfest

# every entity evaluated against the captured real payload, needs Home Assistant
pip install homeassistant && python scripts/ha_entity_smoke.py

# the real Moonraker fork on top of the simulator, then the capture's own checks
git clone https://github.com/Snapmaker/U1-Moonraker /tmp/u1-moonraker
MOONRAKER_SRC=/tmp/u1-moonraker scripts/prove-real-moonraker.sh

# the whole stack including a throwaway Home Assistant, then every entity checked
docker compose --profile ha up -d --build
python scripts/ha_live_proof.py --settle 20
```

`ha_live_proof.py` claims the owner account on the instance it talks to, so only
point it at a throwaway one. `docker compose --profile ha down -v` gives you a
fresh one.

That third command rewrites `artifacts/real-moonraker/`, which
`tests/test_real_moonraker_capture.py` reads. Commit the new capture if you
changed the simulator or the scenario. If you changed neither, the diff should be
timestamps and float noise only, so leave it out of your pull request.

## Ground rules

- Every field name, endpoint and payload shape has to come from Snapmaker's own
  forks of Klipper and Moonraker. Cite the file and the line in the pull request
  or in a comment. If a field cannot be found in the source, do not add it.
  `docs/PROTOCOL.md` is the reference and it carries citations for that reason.
- No hardware is required to work on this and none is assumed. Tests run against
  the simulator. The payload assertions run against bytes captured from a real
  Moonraker.
- A test that passes whether the code is right or wrong is worse than no test.
  Assert the value, not that a call did not raise.
- Keep the diff focused. One change per pull request.
- Prose in code and docs stays plain. Short sentences, no marketing voice.

## Reporting a bug

Say which half is involved, paste the Moonraker or Home Assistant log line, then
say whether you saw it against the simulator or against a real U1. That last
part matters, because the maintainers test against the simulator.
