# Screenshots to capture

No images are committed yet. This file is the shot list. Capture each one from a
live run of `docker compose --profile ha up -d`, then save it to `docs/img/`
under the file name given here and link it from the README.

Rules for every shot: real running software only, no mockups, no retouched
values. Crop out anything personal from the Home Assistant sidebar. Every shot
here is of simulated state, because no U1 was used, so the caption has to say
so.

Entity ids below assume the device is named `u1sim`, which is the hostname the
simulator reports. On a real printer the prefix is the printer's own hostname.

| File | What it shows | Where to capture it |
| --- | --- | --- |
| `img/01-moonraker-ready.png` | Moonraker reaching Klippy ready against the simulator, plus `docker compose ps` showing `u1-moonraker` healthy | terminal running `docker compose logs -f moonraker` |
| `img/02-objects-query.png` | `curl` of `GET /printer/objects/query?print_task_config` with the four slot arrays in the response | terminal |
| `img/03-config-flow.png` | The Home Assistant config flow, host and port filled in | Settings, Devices and services, Add integration, Snapmaker U1 |
| `img/04-device-page.png` | The device page with all 82 entities listed | Settings, Devices and services, the U1 device |
| `img/05-four-slots.png` | An entities card holding `sensor.u1sim_slot_0_filament` through `slot_3_filament`, the four `slot_N_vendor` and the four `slot_N_color` | Home Assistant dashboard |
| `img/06-rfid-identity.png` | `sensor.u1sim_slot_0_tag` with its attributes expanded, showing the RFID vendor, SKU, `official`, `weight_g` and `card_uid` | entity more-info dialog |
| `img/07-color-swatches.png` | The four `sensor.u1sim_slot_N_color` entities rendering their `#RRGGBB` states, with slot 3 showing its two colour `colors` attribute | Home Assistant dashboard |
| `img/08-print-progress.png` | `sensor.u1sim_print_state`, `sensor.u1sim_progress`, `sensor.u1sim_layer` and `sensor.u1sim_active_tool` while the simulator runs its job | Home Assistant dashboard |
| `img/09-map-change.png` | Before and after `snapmaker_u1.set_color_map`, showing `sensor.u1sim_slot_2_assigned_colors` change | Developer tools, Actions, beside the dashboard |
| `img/10-ci-green.png` | The CI run green: tests on 3.11 and 3.12, hassfest, the entity job and the real Moonraker job | GitHub Actions |

Two shots are worth the extra effort because they are the ones that prove the
claim in the README. `02-objects-query.png` proves the payload is the real shape.
`09-map-change.png` proves the integration follows live state instead of showing
a snapshot.
