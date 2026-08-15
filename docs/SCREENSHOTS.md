# Screenshots

Every image in `docs/img/` came out of a browser pointed at this stack actually
running: `u1sim` as the Klippy, the unmodified `Snapmaker/U1-Moonraker` fork on
top of it, then a real Home Assistant with this integration configured. No
mockups, no retouching, no value typed into a picture. All of it is simulated
printer state, because no Snapmaker U1 was used.

Regenerate the whole set:

```bash
pip install playwright==1.60.0 && playwright install chromium
docker compose --profile ha up -d
python scripts/ha_live_proof.py --settle 20     # onboards, runs the config flow
python scripts/capture_screenshots.py
```

Playwright is not in `requirements-dev.txt` on purpose: nothing else needs a
browser, and CI does not take pictures.

The script logs in with the throwaway account `ha_live_proof.py` creates, writes
the `U1` dashboard over the same websocket API the dashboard editor uses, then
takes the shots. Entity ids assume the device is named `u1sim`, which is the
hostname the simulator reports. On a real printer the prefix is the printer's own
hostname.

| File | What it shows |
| --- | --- |
| `img/01-moonraker-ready.png` | `GET /server/info` through the container: `klippy_state: ready`, 29 components, no failures |
| `img/02-objects-query.png` | `GET /printer/objects/query?print_task_config`, the four slot arrays as the printer answered |
| `img/03-config-flow.png` | The config flow dialog with the host filled in, before submit |
| `img/04-device-page.png` | The device page, all 82 entities, full page |
| `img/05-four-slots.png` | Per slot material, vendor, colour, present, official, in use |
| `img/06-rfid-identity.png` | One spool's whole NFC reading as attribute rows: vendor, SKU, official, card UID, tag protocol version |
| `img/07-color-swatches.png` | The four colours plus how many logical colours each head carries |
| `img/08-print-progress.png` | The job: state, progress, layer, active tool, the bed and all four nozzles |
| `img/09-map-change-before.png`, `img/09-map-change-after.png` | `snapmaker_u1.set_color_map` moving logical colour 9 onto head 3, so slot 0 goes 27 to 26 and slot 3 goes 1 to 2 |
| `img/10-tag-panel.png` | The NFC reading for all four slots, including the empty one |
| `img/10-tag-more-info.png` | A row's more info dialog, with the state history the recorder kept |
| `img/11-controls.png` | The four preference switches, the sensitivity select and the print buttons |

Two details worth knowing about how these were taken.

The two payload shots render a file out of `artifacts/docker-compose/`, which is
itself a capture written by `scripts/prove-docker-compose.sh`. The bytes are the
container's answer rather than a terminal somebody typed into. The file sits in
this repository next to the image.

The colour map pair is the only shot taken against another scenario. The firmware
refuses a remap while a job is running
(`u1-klipper/klippy/extras/print_task_config.py:511` to `:519`), so it needs a
loaded printer sitting in standby:

```bash
U1SIM_SCENARIO=idle_loaded docker compose --profile ha up -d
python scripts/capture_screenshots.py --map-change
```

Not committed: a green CI run. The Actions tab is the live version of that, so a
picture of it would only go stale.
