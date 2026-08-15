# Snapmaker U1 API surface

Everything here was read out of Snapmaker's own forks. Two source trees are cited:

- `u1-klipper` = https://github.com/Snapmaker/u1-klipper (Klipper fork, codename "lava")
- `u1-moonraker` = https://github.com/Snapmaker/u1-moonraker (Moonraker fork)

Citations are `path:line`. Nothing in this document is inferred from upstream Klipper or
upstream Moonraker docs. Where a field does not exist in the fork, this document says so
instead of guessing.

The U1 is a 4 physical toolhead printer that addresses up to 32 logical colors.
`u1-klipper/klippy/extras/print_task_config.py:8` sets `LOGICAL_EXTRUDER_NUM = 32` and line 9
sets `PHYSICAL_EXTRUDER_NUM = 4`.

## 1. Klippy socket protocol

### 1.1 Transport and framing

Klippy serves a Unix domain stream socket. The path comes from the `apiserver` start
argument (`u1-klipper/klippy/webhooks.py:119`), the socket is `AF_UNIX` + `SOCK_STREAM`
(line 125), it is unlinked before bind (line 124) and the backlog is 1 (line 128).

Framing is a single `0x03` byte after each JSON document. There is no length prefix and no
newline framing:

- reader: `requests = data.split(b'\x03')`, with the trailing partial kept for the next read
  (`u1-klipper/klippy/webhooks.py:247` to `:249`)
- writer: `self.send_buffer += jmsg + b"\x03"` (`u1-klipper/klippy/webhooks.py:288`)

Moonraker matches that exactly. It reads with `await reader.readuntil(b'\x03')` then strips
the terminator with `jsonw.loads(data[:-1])`
(`u1-moonraker/moonraker/components/klippy_connection.py:194` and `:208`). It writes
`jsonw.dumps(request.to_dict()) + b"\x03"` (line 221). It opens the socket with
`asyncio.open_unix_connection(str(self.uds_address), limit=UNIX_BUFFER_LIMIT)` where the
limit is 20 MB (lines 62 and 329).

Moonraker also reads `SO_PEERCRED` off the socket
(`u1-moonraker/moonraker/utils/__init__.py:229` to `:255`, called at
`klippy_connection.py:319`). A normal process satisfies this. A pid of 1 makes Moonraker
skip service lookup (`klippy_connection.py:336`), which is not an error.

### 1.2 Request and response objects

A request is a JSON object with `id`, `method` and `params`
(`u1-moonraker/moonraker/components/klippy_connection.py:964` to `:969`). Klippy parses it in
`WebRequest.__init__` (`u1-klipper/klippy/webhooks.py:44` to `:55`): `method` must be a
string and `params` must be an object, otherwise the request is dropped with a log line and
no reply (`webhooks.py:252` to `:259`).

A reply is built by `WebRequest.finish` (`u1-klipper/klippy/webhooks.py:99` to `:109`):

- `{"id": <same id>, "result": <payload>}` on success. An empty payload becomes `{}`
- `{"id": <same id>, "error": {"error": "WebRequestError", "message": "<text>"}}` on failure
  (`webhooks.py:34` to `:37`)
- if the request carried no `id`, nothing is sent at all (`webhooks.py:100`)

Moonraker keys pending requests by `id` and logs an unmatched id rather than failing
(`u1-moonraker/moonraker/components/klippy_connection.py:607` to `:614`). An empty `result`
is rewritten to the string `"ok"` before it reaches the caller (lines 615 to 619), which is
why a successful `gcode/script` comes back to an HTTP client as `{"result": "ok"}`. An error
dict is turned into `ServerError(err["message"], 400)` (lines 620 to 625).

Unsolicited messages from Klippy have a `method` and no `id`. Moonraker dispatches those by
name against its registered remote methods (`klippy_connection.py:595` to `:605`).

### 1.3 Endpoints registered by the U1 firmware

`list_endpoints` returns every registered path (`u1-klipper/klippy/webhooks.py:362`). The
full set in this fork, gathered by grepping `register_endpoint` and `register_mux_endpoint`:

| Endpoint | Registered at |
| --- | --- |
| `list_endpoints` | `webhooks.py:321` |
| `info` | `webhooks.py:324` |
| `emergency_stop` | `webhooks.py:325` |
| `register_remote_method` | `webhooks.py:326` |
| `gcode/help` | `webhooks.py:447` |
| `gcode/script` | `webhooks.py:448` |
| `gcode/restart` | `webhooks.py:449` |
| `gcode/firmware_restart` | `webhooks.py:450` |
| `gcode/subscribe_output` | `webhooks.py:452` |
| `objects/list` | `webhooks.py:489` |
| `objects/query` | `webhooks.py:490` |
| `objects/subscribe` | `webhooks.py:491` |
| `print_task_config/set_print_preferences` | `extras/print_task_config.py:140` |
| `pause_resume/cancel`, `/pause`, `/resume` | `extras/pause_resume.py:35`, `:37`, `:39` |
| `control/main_fan` | `extras/fan.py:145` |
| `control/generic_fan` (mux on `fan`) | `extras/fan_generic.py:22` |
| `control/led` (mux on `led`) | `extras/led.py:31` |
| `control/bed_temp` | `extras/heater_bed.py:20` |
| `control/extruder_temp` | `kinematics/extruder.py:588` |
| `control/nozzle_diameter` | `kinematics/extruder.py:589` |
| `control/print_speed` | `extras/gcode_move.py:53` |
| `control/purifier` | `extras/purifier.py:289` |
| `query_endstops/status` | `extras/query_endstops.py:14` |
| `bed_mesh/dump_mesh`, `bed_mesh/abort_probe_mesh` | `extras/bed_mesh.py:135`, `:138` |
| `auto_screws_tilt_adjust/abort_screws_adjust`, `/next_point_adjust` | `extras/auto_screws_tilt_adjust.py:133`, `:135` |
| `extruder_offset_calibration/probe_abort` | `extras/probe_inductance_coil.py:1336` |
| `defect_detection/config` | `extras/defect_detection.py:125` |
| `control/purifier_factory` | `extras/purifier_factory.py:62` |

`print_task_config/set_print_preferences` is the one U1 specific endpoint an integration
really wants, because it is the supported write path for the print preferences. It reads
`auto_replenish_filament`, `filament_entangle_detect`, `filament_entangle_sen`,
`replenish_ignore_color` and `end_led_turn_off`
(`u1-klipper/klippy/extras/print_task_config.py:150` to `:154`), the four integers are
coerced with `bool()` (lines 157 to 174), the sensitivity string must be one of `low`,
`medium`, `high` (line 166, values at lines 11 to 13). The reply is
`{"state": "success"}` or `{"state": "error", "message": "<text>"}` (lines 181 and 185).
Note that it always answers 200 style, so a caller has to inspect `state`.

Endpoints Moonraker refuses to expose outward, so they cannot be called from Home
Assistant even though Klippy has them: `list_endpoints`, `gcode/subscribe_output`,
`register_remote_method` and `emergency_stop`
(`u1-moonraker/moonraker/components/klippy_connection.py:46` to `:51`). Moonraker
re-implements the estop under its own name, see section 4.4.

### 1.4 `info`

Request: `{"id": 1, "method": "info", "params": {"client_info": {"program": "Moonraker",
"version": "<ver>"}}}`. The `client_info` is optional and is only used to label the
connection (`u1-klipper/klippy/webhooks.py:366` to `:368`).

Response fields, all of them from `_handle_info_request`
(`u1-klipper/klippy/webhooks.py:365` to `:383`): `state`, `state_message`, `hostname`,
`klipper_path`, `python_path`, `process_id`, `user_id`, `group_id`, then `log_file`,
`config_file`, `software_version` and `cpu_info` copied out of the start arguments.

`state` is one of `ready`, `startup`, `shutdown` or `error`
(`u1-klipper/klippy/klippy.py:293` to `:302`). Moonraker maps the string through
`KlippyState.from_string`, which raises on anything else
(`u1-moonraker/moonraker/common.py:146` to `:154`, enum at `:138` to `:143`).

`klipper_path` and `python_path` are mandatory. Moonraker reads both with direct
subscripting in `_save_path_info`
(`u1-moonraker/moonraker/components/klippy_connection.py:356` and `:357`), so a missing key
is a `KeyError` during init, not a warning.

### 1.5 `objects/list`

Request params are empty. The reply is `{"objects": [<name>, ...]}`, built from every printer
object that has a `get_status` attribute
(`u1-klipper/klippy/webhooks.py:492` to `:495`).

Moonraker checks that `virtual_sdcard`, `display_status` and `pause_resume` are in that list
and only logs a warning if they are missing
(`u1-moonraker/moonraker/components/klippy_connection.py:568` to `:575`). It is a warning,
not a failure, but a sim should list all three so the log is clean.

### 1.6 `objects/query`

Request: `{"id": N, "method": "objects/query", "params": {"objects": {"<name>": null,
"<name2>": ["field_a", "field_b"]}}}`. `null` means every field. Validation rejects a
non string key or a value that is neither `null` nor a list of strings
(`u1-klipper/klippy/webhooks.py:557` to `:565`).

Reply: `{"eventtime": <float>, "status": {"<name>": {...}}}`
(`u1-klipper/klippy/webhooks.py:544` and `:580`).

An unknown object name (or one with no `get_status`) yields an empty dict for that name
rather than an error (`u1-klipper/klippy/webhooks.py:517` to `:519`). That is the reason a
simulator can serve a subset of the real object set safely.

A query is always a full snapshot. In `_do_query` the per client tuple has `cconn is None`
for a plain query, which sets `is_query` and disables the change filter
(`u1-klipper/klippy/webhooks.py:508`, `:537`, `:539`).

### 1.7 `objects/subscribe` and pushed updates

Request: same as `objects/query` plus `response_template`, an object that is merged into every
pushed message (`u1-klipper/klippy/webhooks.py:568` and `:543`). Moonraker always sends
`{"method": "process_status_update"}`
(`u1-moonraker/moonraker/components/klippy_connection.py:765`).

The immediate reply is the same shape as a query, a full snapshot, because the subscribe
handler runs the query path first and only then stores the client
(`u1-klipper/klippy/webhooks.py:583`, `:556`, `:573`, `:579` to `:582`).

After that Klippy pushes, on its own timer, messages of the form:

```json
{"method": "process_status_update",
 "params": {"eventtime": 1234.5678, "status": {"print_task_config": {"filament_exist": [true, true, false, false]}}}}
```

The push interval is `SUBSCRIPTION_REFRESH_TIME = .25`, so 4 Hz
(`u1-klipper/klippy/webhooks.py:478` and `:555`). The timer is unregistered when the last
subscription goes away (lines 550 to 554).

Three properties of pushed updates matter for both the simulator and the client:

1. **Pushes carry only changed fields.** `_do_query` keeps a `last_query` per cycle and only
   copies a field into the outgoing dict when `rd != lres.get(ri)`
   (`u1-klipper/klippy/webhooks.py:533` to `:538`). An object with nothing changed is omitted,
   and if no object changed nothing is sent at all (line 542). So a client must merge each
   update into its own copy of the state. Replacing state with the update wipes everything
   that did not change in that tick.
2. **A subscribe replaces the previous subscription on that connection.** `_handle_query` with
   `is_subscribe` deletes the existing entry before adding the new one
   (`u1-klipper/klippy/webhooks.py:569` to `:570`, `:582`). Moonraker works around this by
   sending Klippy the union of every client subscription, see section 2.3.
3. **The field list is frozen at the first query.** When a subscription asks for `null`,
   `_do_query` replaces the `None` with the concrete key list it saw on that pass and writes
   it back into the stored subscription
   (`u1-klipper/klippy/webhooks.py:529` to `:532`). A key that appears in `get_status` later
   is never pushed to that subscription. `print_task_config` can gain a
   `flow_calibrate_extruders` key at runtime
   (`u1-klipper/klippy/extras/print_task_config.py:721`), so that key must not be relied on.

### 1.8 `eventtime`

`eventtime` is the Klipper reactor monotonic clock, not a Unix timestamp. It reaches the
subscription payload straight from the reactor timer callback argument
(`u1-klipper/klippy/webhooks.py:496`, `:544`). Moonraker passes it through unchanged and
caches it as `_last_eventtime`
(`u1-moonraker/moonraker/components/klippy_connection.py:641`), then hands the same value to
websocket clients (`u1-moonraker/moonraker/common.py:465` to `:474`). Do not render it as a
date and do not diff it against wall clock.

### 1.9 `gcode/script`

Request params are `{"script": "<gcode>"}` (`u1-klipper/klippy/webhooks.py:456` to `:457`).
The reply is an empty result on success, which Moonraker turns into `"ok"`.

A G-code error is special cased. `_process_request` catches `command_error`, extracts the
coded message and sets the error on the reply. Unlike every other endpoint it does not
re-raise into a printer shutdown (`u1-klipper/klippy/webhooks.py:269` to `:273`). So a bad
command is a 400 to the caller and the printer keeps running. Any non `command_error`
exception from any other endpoint calls `invoke_shutdown`
(`u1-klipper/klippy/webhooks.py:274` to `:279`), which is worth respecting in a simulator:
answer cleanly rather than throwing.

## 2. What Moonraker sends right after it connects

This is the checklist a simulator has to satisfy. The order below is the real control flow of
`u1-moonraker/moonraker/components/klippy_connection.py`.

### 2.1 Connect loop

`_do_connect` polls every `INIT_TIME` = 0.25 s until the socket path exists and is readable
plus writable (`klippy_connection.py:59`, `:296` to `:309`). A missing socket is not an error,
Moonraker just keeps waiting, so the sim can be started after Moonraker.

Then `_init_klippy_connection` loops on `_check_ready` every 0.25 s until init completes
(`klippy_connection.py:377` to `:389`).

### 2.2 The handshake, in order

1. `info` with `client_info` set, because `send_id` is true on the first pass
   (`klippy_connection.py:468`, `:471`, `klippy_apis.py:454` to `:462`).
2. On the first successful `info` that contains `state`: `_save_path_info` reads
   `klipper_path` and `python_path`, then `list_endpoints`
   (`klippy_connection.py:505` to `:510`). Every returned path that is not reserved is
   registered as an HTTP route and a JSON-RPC method (lines 391 to 401).
3. If `state` is not `startup`, `_request_initial_subscriptions` runs
   (`klippy_connection.py:512`, `:403` to `:417`):
   a. `objects/subscribe` with `{"objects": {"webhooks": null}, "response_template":
      {"method": "process_status_update"}}`
   b. `gcode/subscribe_output` with `{"response_template": {"method":
      "process_gcode_response"}}`
   Both failures are caught and logged, so neither is fatal, but the sim should answer both.
4. `list_endpoints` again (`klippy_connection.py:515`).
5. If `state` is `ready`, `_verify_klippy_requirements` runs
   (`klippy_connection.py:529`, `:563` to `:593`):
   a. `objects/list`
   b. if `virtual_sdcard` is in that list, `objects/query` with `{"objects": {"configfile":
      null}}`, then `config.virtual_sdcard.path` is handed to the file manager. A mismatch
      raises a Moonraker warning, not an error
      (`u1-moonraker/moonraker/components/file_manager/file_manager.py:284` to `:299`).
6. `register_remote_method` once per method Moonraker wants Klippy to be able to call
   (`klippy_connection.py:530` to `:537`, params shape at `klippy_apis.py:536` to `:540`).
   With the shipped U1 config the set is `shutdown_machine` and `reboot_machine`
   (`components/machine.py:180` to `:184`), `clear_exception` and `raise_exception`
   (`components/exception_manager.py:85` to `:86`), `pause_job_queue` and `start_job_queue`
   (`components/job_queue.py:56` to `:57`) and `publish_mqtt_topic`
   (`components/mqtt.py:452`). Answering `{}` to each is enough.
7. `server:klippy_ready` fires and the components do their own queries, see 2.4.

### 2.3 How Moonraker multiplexes subscriptions

Moonraker keeps one Klippy connection and one Klippy subscription. On every client subscribe
it rebuilds the union of all client subscriptions and sends that union to Klippy
(`klippy_connection.py:698` to `:710`, `:764`). Its own host subscription is unioned
separately in `klippy_apis.subscribe_objects`
(`klippy_apis.py:494` to `:506`), which is why a component subscribe never drops another
component's fields.

This fork adds a cache fast path that upstream does not have. If the request is already
covered by an existing subscription and every requested object is already in
`subscription_cache`, Moonraker answers from cache and never talks to Klippy
(`klippy_connection.py:711` to `:763`). Two consequences for a client:

- the `eventtime` in that answer is the cached `_last_eventtime`, so it can be older than now
- the answer is only as complete as the cache, so a client that wants a guaranteed full
  snapshot should call `printer.objects.query` once at startup and then subscribe

Pushed updates from Klippy are merged into `subscription_cache` and then fanned out per
connection, filtered to the fields that connection asked for
(`klippy_connection.py:638` to `:672`). The change-only property from section 1.7 survives
that path, so a Home Assistant client receives partial dicts.

### 2.4 Queries the components fire on `klippy_ready`

Every one of these hits `objects/query` or `objects/subscribe` on the sim:

| Component | Call | Source |
| --- | --- | --- |
| `data_store` | query `{"heaters": null}`, then subscribe to every name in `heaters.available_sensors` plus `available_monitors` | `components/data_store.py:76` to `:90` |
| `job_state` | subscribe `{"print_stats": null}` | `components/job_state.py:47` to `:48` |
| `machine` | subscribe `{"machine_state_manager": null, "extruder": ["nozzle_diameter"], "extruder1": [...], "extruder2": [...], "extruder3": [...]}` | `components/machine.py:202` to `:221` |
| `octoprint_compat` | query `{"heaters": null}`, then subscribe those sensors plus `print_stats` | `components/octoprint_compat.py:136` to `:144` |
| `exception_manager` | query `{"exception_manager": null}` | `components/exception_manager.py:329` to `:336` |
| `job_queue` | query `{"print_stats": null}` when a queued job starts | `components/job_queue.py:158` |

The extruder names are hardcoded to `extruder`, `extruder1`, `extruder2`, `extruder3`
(`components/machine.py:202` to `:204`), which matches the U1 config: `[extruder]`,
`[extruder1]`, `[extruder2]`, `[extruder3]` at `u1-klipper/lava/printer.cfg:663`, `:816`,
`:955` and `:1093`.

None of these are fatal when the object is absent, because a missing object comes back as an
empty dict and each caller guards on the key. A sim that serves them anyway keeps the
Moonraker log clean and makes the demo honest.

## 3. The `print_task_config` object

The class is `PrintTaskConfig` and `load_config` returns it directly
(`u1-klipper/klippy/extras/print_task_config.py:1320` to `:1321`), so its printer object name
is the section name, `print_task_config`. The section is enabled on the real machine at
`u1-klipper/lava/printer.cfg:7`.

`get_status` returns the whole config dict, with two refresh side effects first
(`u1-klipper/klippy/extras/print_task_config.py:500` to `:504`):

```python
def get_status(self, eventtime=None):
    self.update_filament_exist_flag()
    self.update_filament_edit_flag()
    print_task_config = dict(self.print_task_config)
    return print_task_config
```

So `filament_exist` and `filament_edit` are recomputed on every query. `filament_exist` comes
from the per slot `filament_motion_sensor e<n>_filament` and the feeder module state
(lines 474 to 498). `filament_edit` is true only when the slot has filament and the filament
is not official, meaning a user may overwrite its identity (lines 462 to 472).

### 3.1 Key by key

Defaults are `DEFAULT_PRINT_TASK_CONFIG`
(`u1-klipper/klippy/extras/print_task_config.py:23` to `:61`). Lengths are enforced in
`_early_check` (lines 193 to 218), which resets the whole config to defaults if a length is
wrong (lines 255 to 258).

| Key | Type | Length | Notes |
| --- | --- | --- | --- |
| `filament_vendor` | string | 4 | `"NONE"` when unknown. Set from RFID `VENDOR`. |
| `filament_type` | string | 4 | Base material, `"NONE"` when unknown. From RFID `MAIN_TYPE`. |
| `filament_sub_type` | string | 4 | Variant, from RFID `SUB_TYPE`. |
| `filament_color` | int | 4 | ARGB packed into one int, default `0xFFFFFFFF`. |
| `filament_color_rgba` | string | 4 | Exactly 8 hex chars, `RRGGBBAA`. Length is validated at line 213. |
| `filament_color_multi` | object | 4 | `{"nums": int, "alpha": int, "mode": int, "colors": ["RRGGBB", ...]}`. `colors` has `nums` entries, each exactly 6 hex chars (lines 215 to 218), max 5 (`FILAMENT_COLOR_NUMS_MAX` at line 15). |
| `filament_official` | bool | 4 | True only for a verified Snapmaker tag. |
| `filament_sku` | int | 4 | 0 when not official. |
| `filament_edit` | bool | 4 | Recomputed, see above. |
| `filament_exist` | bool | 4 | Recomputed, see above. |
| `filament_soft` | bool | 4 | Soft material flag, looked up from `filament_parameters`. |
| `extruder_map_table` | int | 32 | Logical color index to physical head, each value 0 to 3. Default is `[0,1,2,3]` then 28 zeros (line 38). |
| `extruders_used` | bool | 4 | Which heads this job uses. |
| `extruders_replenished` | int | 4 | Which head took over after an auto replenish, default `[0,1,2,3]`. |
| `time_lapse_camera` | bool | | |
| `auto_bed_leveling` | bool | | |
| `flow_calibrate` | bool | | |
| `flow_calib_extruders` | bool | 4 | |
| `shaper_calibrate` | bool | | |
| `auto_replenish_filament` | bool | | Default true. |
| `replenish_ignore_color` | bool | | |
| `filament_entangle_detect` | bool | | |
| `filament_entangle_sen` | string | | `low`, `medium` or `high`. |
| `end_led_turn_off` | bool | | |
| `end_unload_filament` | bool | 4 | |
| `reprint_info` | object | | Holds `auto_bed_leveling`, `flow_calibrate`, `flow_calib_extruders`, `time_lapse_camera`, `extruder_map_table`, `extruders_used`, `end_unload_filament` (lines 52 to 60). |

### 3.2 A realistic payload

Four loaded slots, three of them official Snapmaker spools read off RFID, one hand entered
dual color silk. Six logical colors mapped onto the four heads. Every key below exists in
`DEFAULT_PRINT_TASK_CONFIG`.

```json
{
  "filament_vendor": ["Snapmaker", "Snapmaker", "Snapmaker", "Generic"],
  "filament_type": ["PLA", "PLA", "PETG", "PLA"],
  "filament_sub_type": ["Basic", "Matte", "HF", "Silk"],
  "filament_color": [4278190080, 4294308065, 4292027178, 4280191205],
  "filament_color_rgba": ["000000FF", "F5F0E1FF", "D3232AFF", "1E88E5FF"],
  "filament_color_multi": [
    {"nums": 1, "alpha": 255, "mode": 0, "colors": ["000000"]},
    {"nums": 1, "alpha": 255, "mode": 0, "colors": ["F5F0E1"]},
    {"nums": 1, "alpha": 255, "mode": 0, "colors": ["D3232A"]},
    {"nums": 2, "alpha": 255, "mode": 1, "colors": ["1E88E5", "43A047"]}
  ],
  "filament_official": [true, true, true, false],
  "filament_sku": [12001, 12042, 13007, 0],
  "filament_edit": [false, false, false, true],
  "filament_exist": [true, true, true, true],
  "filament_soft": [false, false, false, false],
  "extruder_map_table": [0, 1, 2, 3, 1, 2, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
                         0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
  "extruders_used": [true, true, true, true],
  "extruders_replenished": [0, 1, 2, 3],
  "time_lapse_camera": true,
  "auto_bed_leveling": true,
  "flow_calibrate": false,
  "flow_calib_extruders": [true, true, true, true],
  "shaper_calibrate": false,
  "auto_replenish_filament": true,
  "replenish_ignore_color": false,
  "filament_entangle_detect": true,
  "filament_entangle_sen": "medium",
  "end_led_turn_off": false,
  "end_unload_filament": [false, false, false, false],
  "reprint_info": {
    "auto_bed_leveling": true,
    "flow_calibrate": false,
    "flow_calib_extruders": [true, true, true, true],
    "time_lapse_camera": true,
    "extruder_map_table": [0, 1, 2, 3, 1, 2, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
                           0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    "extruders_used": [true, true, true, true],
    "end_unload_filament": [false, false, false, false]
  }
}
```

### 3.3 What is NOT on the API

`DEFAULT_PRINT_TASK_CONFIG_2`
(`u1-klipper/klippy/extras/print_task_config.py:63` to `:75`) holds the per logical extruder
job parameters: `nozzle_temp`, `nozzle_diameter`, `filament_type`, `filament_diameter`,
`filament_used_g`, `filament_used_mm`, `filament_flow_ratio`, `filament_max_vol_speed`, each
32 long, plus `line_width`, `layer_height` and `outer_wall_speed`.

**It is not exposed to Moonraker.** `get_status` returns `self.print_task_config` only, never
`self.print_task_config_2` (line 503). There is no second printer object either: the only
readers of `print_task_config_2` are in process, through the object reference
(`u1-klipper/klippy/extras/flow_calibrator.py:645`, `:655` to `:657`). It is persisted to
`print_task_2.json` (line 21) and set by the `SET_PRINT_TASK_PARAMETERS` G-code command
(lines 1031 to 1306).

So an integration cannot read per color grams from the printer state. The honest sources for
per color usage are:

- **the sliced file's metadata**, which Moonraker parses and serves. `filament_used_mm`,
  `filament_weight`, `nozzle_temp`, `filament_flow_ratio`, `nozzle_diameter_list` and
  `filament_type` are all per filament lists coming out of the PrusaSlicer family parser
  (`u1-moonraker/moonraker/components/file_manager/metadata.py:456` to `:462`, `:570` to
  `:592`, `:538` to `:543`). They are all in `SUPPORTED_DATA` so they land in the metadata dict
  (lines 1131 to 1158). Read it at `GET /server/files/metadata?filename=<path>`
  (`components/file_manager/file_manager.py:128`).
- **`print_stats.filament_used`**, a single total in mm for the running job
  (`u1-klipper/klippy/extras/print_stats.py:330`).

One caution on the metadata key names: the parser writes `filament_max_volumetric_speed`
(`metadata.py:1154`) while the print start path looks for `filament_max_vol_speed`
(`u1-moonraker/moonraker/components/klippy_apis.py:294`). Use the name the parser writes.

### 3.4 RFID, the `filament_detect` object

`[filament_detect]` is enabled on the real machine (`u1-klipper/lava/printer.cfg:61`) and the
NFC hardware is `[fm175xx_reader]` (line 44). `FilamentDetector.get_status` returns
(`u1-klipper/klippy/extras/filament_detect.py:272` to `:276`):

```json
{"info": [ {...}, {...}, {...}, {...} ], "state": [0, 0, 0, 0], "config": {"startup_stay": false}}
```

`state` is one per channel, `0` idle, `1` detecting, `2` self testing
(`filament_detect.py:11` to `:13`). There are 4 channels (line 15).

Each entry of `info` is a copy of `FILAMENT_INFO_STRUCT`
(`u1-klipper/klippy/extras/filament_protocol.py:6` to `:38`), so the keys and defaults are
exactly:

`VERSION` 0, `VENDOR` "NONE", `MANUFACTURER` "NONE", `MAIN_TYPE` "NONE", `SUB_TYPE` "NONE",
`TRAY` 0, `ALPHA` 255, `MULTI_MODE` 0, `COLOR_NUMS` 1, `ARGB_COLOR` 4294967295, `RGB_1`
through `RGB_5` 16777215, `DIAMETER` 0, `WEIGHT` 0, `LENGTH` 0, `DRYING_TEMP` 0,
`DRYING_TIME` 0, `HOTEND_MAX_TEMP` 0, `HOTEND_MIN_TEMP` 0, `BED_TYPE` 0, `BED_TEMP` 0,
`FIRST_LAYER_TEMP` 0, `OTHER_LAYER_TEMP` 0, `SKU` 0, `MF_DATE` "19700101",
`RSA_KEY_VERSION` 0, `OFFICIAL` false, `CARD_UID` 0.

`MAIN_TYPE` is decoded from a fixed table, so the possible strings are `PLA`, `PETG`, `ABS`,
`TPU`, `PVA`, `ASA`, `PA`, `PA-CF`, `PA-GF`, `PC`, `PLA-CF`, `PEBA`, `TPE`, `Reserved`
(`filament_protocol.py:56` to `:71`). `SUB_TYPE` comes from `Basic`, `Matte`, `SnapSpeed`,
`Silk`, `Support`, `HF`, `95A`, `95A HF`, `90A`, `85A`, `Wood`, `Translucent`,
`Full Spectrum`, `""` (lines 89 to 104). Use those strings, do not invent variants.

### 3.5 How a tag becomes slot state

`print_task_config` registers `_rfid_filament_info_update_cb` with `filament_detect` on
`klippy:ready` (`u1-klipper/klippy/extras/print_task_config.py:266` to `:271`). The callback
(lines 308 to 366) does exactly this:

- ignores a non official tag when the slot already has a vendor set (lines 314 to 316)
- ignores a repeat of the same official SKU (lines 318 to 321)
- `filament_color_rgba = f"{info['RGB_1']:06X}" + f"{info['ALPHA']:02X}"` (line 325)
- copies `VENDOR`, `MAIN_TYPE` and `SUB_TYPE` into the vendor, type and sub type arrays, then
  `ARGB_COLOR` straight into `filament_color` (lines 327 to 331)
- builds `filament_color_multi` from `COLOR_NUMS`, `ALPHA`, `MULTI_MODE` and `RGB_1` through
  `RGB_5`, then truncates `colors` to `nums` (lines 333 to 341)
- copies `OFFICIAL` and `SKU`, looks up `filament_soft` from `filament_parameters`
  (lines 343 to 349)
- persists, then runs `FLOW_RESET_K EXTRUDER=<channel>` (lines 352 and 365)

Note the color arithmetic disagreement in the firmware: `filament_color` is the tag's
`ARGB_COLOR` verbatim, while `filament_color_rgba` is rebuilt from `RGB_1` plus `ALPHA`. For a
tag where the two disagree the two fields disagree too. Prefer `filament_color_rgba` and
`filament_color_multi` for anything user facing, because the manual write path keeps all three
consistent (lines 645 to 667) and `_early_check` repairs `filament_color_multi` from
`filament_color_rgba`, not from `filament_color` (lines 228 to 240).

### 3.6 The other objects worth serving

Field lists read straight out of each `get_status`.

| Object | Fields | Source |
| --- | --- | --- |
| `webhooks` | `state`, `state_message` | `u1-klipper/klippy/webhooks.py:410` to `:412` |
| `print_stats` | `filename`, `total_duration`, `print_duration`, `filament_used`, `state`, `exception`, `message`, `info` where `info` is `{"total_layer": int or null, "current_layer": int or null}` | `u1-klipper/klippy/extras/print_stats.py:326` to `:336` |
| `virtual_sdcard` | `file_path`, `progress` (0.0 to 1.0), `is_active`, `file_position`, `file_size`, `pl_env_valid` | `u1-klipper/klippy/extras/virtual_sdcard.py:236` to `:244` |
| `display_status` | `progress`, `message` | `u1-klipper/klippy/extras/display_status.py:34` |
| `pause_resume` | `is_paused` | `u1-klipper/klippy/extras/pause_resume.py:57` to `:60` |
| `toolhead` | kinematics status plus `print_time`, `stalls`, `estimated_print_time`, `extruder`, `position`, `max_velocity`, `max_accel`, `minimum_cruise_ratio`, `square_corner_velocity` | `u1-klipper/klippy/toolhead.py:625` to `:638` |
| `extruder`, `extruder1`, `extruder2`, `extruder3` | heater fields `temperature`, `target`, `power`, then `can_extrude`, `extruder_index`, `nozzle_diameter`, `printing_e_pos`, `activating_move`, the park detector fields, `real_extruder_stats`, `extruder_offset`, plus the extruder stepper fields `pressure_advance`, `smooth_time`, `motion_queue` | `u1-klipper/klippy/kinematics/extruder.py:709` to `:730`, heater at `extras/heaters.py:188` to `:194`, stepper at `kinematics/extruder.py:302` to `:305` |
| `heater_bed` | `temperature`, `target`, `power` | `u1-klipper/klippy/extras/heaters.py:188` to `:194` |
| `heaters` | `available_heaters`, `available_sensors`, `available_monitors` | `u1-klipper/klippy/extras/heaters.py:580` to `:583` |
| `configfile` | `config`, `settings`, `warnings`, `save_config_pending`, `save_config_pending_items` | `u1-klipper/klippy/configfile.py:351` to `:356` |
| `exception_manager` | `exceptions`, a list | `u1-klipper/klippy/exception_manager.py:305` to `:308` |
| `machine_state_manager` | `main_state`, `action_code` | `u1-klipper/klippy/extras/machine_state_manager.py:322` to `:326` |
| `filament_motion_sensor e0_filament` and e1 to e3 | `filament_detected`, `enabled` | `u1-klipper/klippy/extras/filament_switch_sensor.py:162` to `:165` |
| `filament_feed left` and `filament_feed right` | one key per served head, `extruder0` to `extruder3`, each `{module_exist, filament_detected, disable_auto, channel_state, channel_error, channel_error_state, channel_action_state}` | `u1-klipper/klippy/extras/filament_feed.py:1601` to `:1626` |

Two traps in that table:

- the extruder status merges the park detector dict, which contributes a key literally named
  `state` with the values `PARKED`, `ACTIVATE` or `UNKNOWN`, plus `park_pin`, `active_pin` and
  `grab_valid_pin` (`u1-klipper/klippy/extras/park_detector.py:70` to `:85`). It is dock state,
  not print state. `print_stats.state` is the print state.
- `exception_manager.exceptions` must be an empty list unless every entry carries `id`,
  `index`, `code`, `level` and `message`. Moonraker subscripts all five without a default
  (`u1-moonraker/moonraker/components/exception_manager.py:261` to `:265`), so a partial entry
  is a traceback inside Moonraker.

`print_stats.state` values seen in the source are `standby` (line 298), `printing` (line 140),
`paused` (line 151), `complete` (line 157), `error` (line 159) and `cancelled` (line 163), all
in `u1-klipper/klippy/extras/print_stats.py`.


`extruder_switch_recorder` is looked up when building extruder status
(`u1-klipper/klippy/kinematics/extruder.py:721`), but there is no such module in this fork
snapshot, so `switch_count`, `retry_count`, `error_count` and `last_maintenance_count` are
absent. Do not build entities on them.

## 4. The Moonraker surface a Home Assistant client uses

### 4.1 There is no object allowlist

Grepping the whole `u1-moonraker` tree for `print_task_config` returns nothing. Moonraker never
enumerates Klipper objects: it forwards whatever the caller asked for and returns whatever
Klippy answered. `objects/query` and `objects/subscribe` are generic remote endpoints
(`u1-moonraker/moonraker/components/klippy_apis.py:41` to `:43`, forwarding at
`components/klippy_connection.py:674` to `:686`). So `print_task_config` and `filament_detect`
work with an unmodified Moonraker, no patch and no config change.

### 4.2 How a Klippy endpoint becomes an HTTP path and an RPC method

`_request_endpoints` registers each discovered endpoint with `is_remote=True`
(`components/klippy_connection.py:398` to `:401`). `APIDefinition.create` then derives both
names (`moonraker/common.py:257` and `:270` to `:275`):

- HTTP path is `/printer/` + the endpoint, so `objects/query` becomes `/printer/objects/query`
- both GET and POST are accepted, because request types are forced to `GET | POST` (line 274)
- the RPC method is the path minus the leading slash with dots for slashes, so
  `printer.objects.query`

That rule gives, for free, `POST /printer/print_task_config/set_print_preferences` and
`printer.print_task_config.set_print_preferences` for the U1 preferences endpoint.

### 4.3 The calls to make

Default port is 7125 (`u1-moonraker/lava/moonraker.conf:3`). The shipped
`[authorization] trusted_clients` block covers `10.0.0.0/8`, `127.0.0.0/8`, `172.16.0.0/12`,
`192.0.0.0/8`, `172.18.0.0/16`, `169.254.0.0/16`, `FE80::/10` and `::1/128`
(`lava/moonraker.conf:10` to `:19`), so a LAN client needs no API key. Keep an optional API key
field anyway for anyone who tightened the config.

**Full snapshot over HTTP.** The `objects/` endpoints use a different query parser: each query
string key is an object name and its value is a comma separated field list, empty meaning all
fields (`u1-moonraker/moonraker/components/application.py:633` to `:644`, selected by
`need_object_parser` at `moonraker/common.py:235` to `:236`).

```
GET /printer/objects/query?print_task_config&filament_detect&print_stats&virtual_sdcard&toolhead&extruder&extruder1&extruder2&extruder3&heater_bed
GET /printer/objects/query?print_stats=state,filename&virtual_sdcard=progress
```

Response, wrapped in `result` because HTTP handlers wrap by default
(`components/application.py:706` to `:707`):

```json
{"result": {"eventtime": 3412.887, "status": {"print_task_config": { ... }, "print_stats": { ... }}}}
```

**Live updates over the websocket.** Connect to `ws://<host>:7125/websocket`
(`u1-moonraker/moonraker/components/websockets.py:55`). Then:

```json
{"jsonrpc": "2.0", "id": 1, "method": "printer.objects.subscribe",
 "params": {"objects": {"print_task_config": null, "filament_detect": null,
                        "print_stats": null, "virtual_sdcard": null,
                        "toolhead": ["extruder"], "extruder": null,
                        "extruder1": null, "extruder2": null, "extruder3": null,
                        "heater_bed": null}}}
```

The reply is `{"jsonrpc": "2.0", "result": {"eventtime": ..., "status": {...}}, "id": 1,
"cli_time": -1, "dev_time": <unix seconds>}`. `cli_time` and `dev_time` are additions in this
fork (`moonraker/common.py:887` to `:894`), so a client must tolerate extra members rather
than validating the response shape strictly.

Updates then arrive unsolicited as:

```json
{"jsonrpc": "2.0", "method": "notify_status_update",
 "params": [{"print_task_config": {"filament_exist": [true, true, true, false]}}, 3413.14]}
```

`params` is a two element array, status first then eventtime
(`u1-moonraker/moonraker/common.py:465` to `:474`). The status dict is partial, see 1.7.

Other notifications worth handling, all named `notify_` plus the last colon segment of the
server event (`components/websockets.py:66` to `:81`, registrations at
`moonraker/server.py:131` to `:136`): `notify_klippy_ready`, `notify_klippy_shutdown`,
`notify_klippy_disconnected`, `notify_gcode_response`. On `notify_klippy_disconnected` a client
should mark entities unavailable. On `notify_klippy_ready` it must re-query and re-subscribe
because Moonraker clears every subscription and its cache on disconnect
(`components/klippy_connection.py:896` to `:899`).

One thing not to send: `dev_time`. If a request carries a `dev_time` more than 600 seconds away
from the server clock, the request is rejected with error code -31000
(`moonraker/common.py:55`, `:777` to `:787`). Omitting it means the check is skipped.

### 4.4 Writes

| Action | Call | Note |
| --- | --- | --- |
| run G-code | `POST /printer/gcode/script?script=<gcode>` or `printer.gcode.script` with `{"script": "..."}` | success is `{"result": "ok"}`, a bad command is a 400 |
| pause, resume, cancel | `POST /printer/print/pause`, `/printer/print/resume`, `/printer/print/cancel` | registered locally at `components/klippy_apis.py:59` to `:67`, so the paths are exactly these and not `/printer/pause_resume/...` |
| start a job | `POST /printer/print/start?filename=<path>` | `components/klippy_apis.py:68` to `:70`. It refuses when the printer is not ready or already busy and answers `{"state": "error", "message": ...}` (lines 137 to 141) |
| restart, firmware restart | `POST /printer/restart`, `POST /printer/firmware_restart` | `components/klippy_apis.py:72` to `:76` |
| emergency stop | `printer.emergency_stop` over the websocket only | HTTP is explicitly excluded at `components/klippy_apis.py:81` |
| LED, fans, temperatures, print speed | `printer.control.led`, `printer.control.main_fan`, `printer.control.generic_fan`, `printer.control.extruder_temp`, `printer.control.bed_temp`, `printer.control.print_speed`, websocket only | `components/klippy_apis.py:84` to `:118`, each one excludes HTTP |
| set U1 print preferences | `POST /printer/print_task_config/set_print_preferences` | body keys per 1.3, reply carries `state` |
| file metadata | `GET /server/files/metadata?filename=<path>` | `components/file_manager/file_manager.py:128` |
| server info | `GET /server/info` | `moonraker/server.py:120` to `:121` |

The websocket-only rows matter. An integration that only speaks HTTP cannot stop the printer or
set a temperature on this fork.

## 5. The colour to head mapping over G-code

`[printer]` on the real machine sets `max_logical_extruder_num: 32` and
`max_physical_extruder_num: 4` (`u1-klipper/lava/printer.cfg:113` to `:114`), which match the
constants in `print_task_config`.

### 5.1 Reading and writing the map

```
SET_PRINT_EXTRUDER_MAP CONFIG_EXTRUDER=<0..31> MAP_EXTRUDER=<0..3>
GET_PRINT_EXTRUDER_MAP
```

Both are registered at `u1-klipper/klippy/extras/print_task_config.py:112` to `:115`. The
setter rejects out of range indices (lines 524 to 526) and refuses outright while
`print_stats.state` is `printing` or `paused` (lines 511 to 519). It writes both
`extruder_map_table` and `reprint_info.extruder_map_table` (lines 532 to 536). The getter
prints one line per logical index, `T{n} -> T{m}` (lines 541 to 545).

`SET_PRINT_TASK_PARAMETERS MAP_TABLE="[[0,0],[1,2]]"` sets several entries at once, parsed with
`ast.literal_eval` and validated pair by pair (lines 1091 to 1110). It is also blocked while
printing (lines 1063 to 1073).

### 5.2 T codes are logical colours, not heads

This is the part that is easy to get wrong.

`T0` to `T3` are native commands, not macros. Each extruder registers its own
`gcode_id = 'T%d' % extruder_num` against `cmd_SWITCH_EXTRUDER_ADVANCED`
(`u1-klipper/klippy/kinematics/extruder.py:431` and `:528`). That handler reads an `A`
parameter which defaults to 1. When `A` is non zero it maps the index through
`print_task_config.get_extruder_map_index` before switching
(`kinematics/extruder.py:1216` to `:1230`). So a bare `T2` in sliced G-code means logical
colour 2. The head it lands on depends on `extruder_map_table[2]` at run time. `T2 A0`
bypasses the map and picks physical head 2.

`T4` to `T31` are `gcode_macro` definitions that call
`SWITCH_OF_EXTENDED_EXTRUDER INDEX=<n>` (`u1-klipper/lava/fluidd.cfg:67` onward). That command
resolves the map itself, then forces `A=0` on the already resolved extruder so the map is not
applied twice (`u1-klipper/klippy/toolhead.py:694` to `:713`, registration at `:320`).

Two consequences:

- a tool count taken from raw T codes is a colour count, not a physical swap count. The swap
  count only exists once the map is applied.
- `T0` to `T3` are only registered inside the advanced tool changer config branch
  (`kinematics/extruder.py:528` sits inside that block). The repo's test config defines
  `[gcode_macro T0]` through `[gcode_macro T3]` calling `ACTIVATE_EXTRUDER` instead
  (`u1-klipper/test/klippy/snapmaker-lava-p1.cfg:445` onward), which is the plain Klipper way
  and does not consult the map. Do not assume both paths behave the same.

### 5.3 Other useful commands on this object

All registered at `u1-klipper/klippy/extras/print_task_config.py:112` to `:137`:
`SET_PRINT_FILAMENT_CONFIG`, `GET_PRINT_TASK_CONFIG`, `SAVE_CURRENT_PRINT_TASK_CONFIG`,
`RESET_PRINT_TASK_CONFIG`, `LOAD_PRINT_TASK_CONFIG`, `SET_PRINT_PREFERENCES`,
`SET_PRINT_USED_EXTRUDERS`, `SET_PRINT_TASK_PARAMETERS`, plus the internal
`INNER_CHECK_AND_RELOAD_FILAMENT_INFO`, `INNER_AUTO_REPLENISH_FILAMENT` and `INNER_PRINT_END`.
The `INNER_` ones are called by the firmware's own macros. Leave them alone.

`SET_PRINT_FILAMENT_CONFIG` is the write path for slot identity. It takes
`CONFIG_EXTRUDER=<0..3>` plus any of `VENDOR`, `FILAMENT_TYPE`, `FILAMENT_SUBTYPE`, `SOFT`,
`FILAMENT_COLOR`, `FILAMENT_COLOR_RGBA`, `ALPHA`, `COLOR_NUMS`, `COLORS`, `MULTI_MODE`, `FORCE`
(lines 556 to 568). It refuses to touch a slot holding official filament unless `FORCE=1`
(line 578), `VENDOR` plus `FILAMENT_TYPE` plus `FILAMENT_SUBTYPE` must be given together
(lines 589 to 591) and any write clears `filament_official` and zeroes `filament_sku`
(lines 669 to 670). Prefer `set_print_preferences` for preferences and this command only for
identity.

## 6. Simulator acceptance checklist

A fake Klippy is correct when an unmodified `u1-moonraker` reaches "Klippy ready" against it.
Concretely it must:

1. Bind an `AF_UNIX` `SOCK_STREAM` socket at the path in `klippy_uds_address`, unlinking a
   stale file first. The shipped path is
   `/home/lava/printer_data/comms/klippy.sock` (`u1-moonraker/lava/moonraker.conf:4`).
2. Frame every message with a trailing `0x03` and accept multiple messages per read, including a
   document split across two reads.
3. Answer `info` with all of `state`, `state_message`, `hostname`, `klipper_path`,
   `python_path`, `process_id`, `user_id`, `group_id`, `log_file`, `config_file`,
   `software_version`, `cpu_info`. Return `state` as `startup` first if you want to exercise
   Moonraker's wait loop, then `ready`.
4. Answer `list_endpoints` with at least `info`, `emergency_stop`, `register_remote_method`,
   `objects/list`, `objects/query`, `objects/subscribe`, `gcode/script`,
   `gcode/subscribe_output`, `gcode/restart`, `gcode/firmware_restart`, `gcode/help`,
   `pause_resume/pause`, `pause_resume/resume`, `pause_resume/cancel` and
   `print_task_config/set_print_preferences`.
5. Answer `objects/list` including `virtual_sdcard`, `display_status` and `pause_resume`, plus
   `print_task_config`, `filament_detect`, `print_stats`, `toolhead`, `extruder`, `extruder1`,
   `extruder2`, `extruder3`, `heater_bed`, `heaters`, `configfile`, `webhooks`,
   `exception_manager`, `machine_state_manager`.
6. Answer `objects/query` with a full snapshot and an empty dict for any unknown name.
7. Answer `objects/subscribe` with a full snapshot, remember `response_template` per connection,
   then push `{"method": "process_status_update", "params": {"eventtime": ..., "status": ...}}`
   at 4 Hz carrying changed fields only.
8. Answer `gcode/subscribe_output` and `register_remote_method` with `{}`.
9. Serve `configfile.config.virtual_sdcard.path` pointing at the directory Moonraker is
   configured to use for gcodes, otherwise Moonraker raises a `gcode_path` warning.
10. Keep `exception_manager.exceptions` empty. If it is not empty, supply every one of `id`,
    `index`, `code`, `level` and `message` per entry.
11. Never raise out of an endpoint handler. On the real firmware an unhandled exception in any
    endpoint other than `gcode/script` triggers a printer shutdown
    (`u1-klipper/klippy/webhooks.py:274` to `:279`), so mirroring that is the wrong behaviour to
    copy. Return a `WebRequestError` shaped error instead.
12. Accept `gcode/script` and apply the ones that matter, at minimum
    `SET_PRINT_EXTRUDER_MAP`, `GET_PRINT_EXTRUDER_MAP`, `SET_PRINT_FILAMENT_CONFIG` and the
    pause, resume, cancel paths, so the demo drives real state changes rather than a canned
    recording.

The `[machine] provider: none` line in the shipped config
(`u1-moonraker/lava/moonraker.conf:7` to `:8`) means `extract_service_info` returns an empty
dict (`u1-moonraker/moonraker/components/machine.py:1194` to `:1201`), so a simulator does not
need to look like a systemd unit.

## 7. Client gotchas, collected

- Status pushes are deltas. Merge, do not replace.
- `eventtime` is a monotonic clock, not a timestamp.
- The subscribe field list is frozen on first use, so ask for `null` once and expect the key set
  to stay fixed for the life of the connection.
- Query once at startup for a guaranteed full snapshot, because a subscribe can be answered from
  Moonraker's cache.
- Re-query and re-subscribe on `notify_klippy_ready`, since Moonraker drops all subscriptions on
  Klippy disconnect.
- Emergency stop and the `printer.control.*` calls are websocket only on this fork.
- `filament_color` is an ARGB integer, `filament_color_rgba` is `RRGGBBAA` and
  `filament_color_multi.colors` entries are `RRGGBB`. Home Assistant wants RGB, so slice the
  first six characters of `filament_color_rgba` or read `colors[0]`. Take alpha separately.
- `"NONE"` is the firmware's empty marker for `filament_vendor`, `filament_type` and
  `filament_sub_type`. Treat it as unknown, not as a material named NONE.
- Per colour grams are not in printer state. They come from file metadata or not at all.
- The extruder objects carry a `state` field that is dock state, not print state.
- Do not send `dev_time` in JSON-RPC requests.

## 8. Running the fork off a printer

Everything in section 6 was checked by running `Snapmaker/U1-Moonraker` at
`3b8357c` against `u1sim`. It reached `klippy_state: ready` with no startup
warnings, no failed components and nothing missing. The raw payloads and the
Moonraker log are in `artifacts/real-moonraker/`, produced by
`scripts/prove-real-moonraker.sh`.

Five things about the fork have to be dealt with before it will start. None of
them is in its own documentation. All five were found by running it.

1. **`httpx` is a missing dependency.** `moonraker/components/httpx_client.py`
   imports `httpx` at module level and `httpx_client` is in `CORE_COMPONENTS`
   (`moonraker/server.py:62` to `:67`), so the import runs on every start.
   `pyproject.toml` does not list it. Without it Moonraker exits before it
   opens a socket. `docker/requirements-moonraker.txt` adds it, with a note.
2. **`<data path>/config/snapmaker/` must exist.** `machine.py:81` puts
   `product_info.json` there and `_get_product_info` writes it on first start
   (`:1059`) without creating the directory, so a missing one is a
   `FileNotFoundError` during component load, which is fatal.
3. **`<data path>/mqtt/` must exist**, for the same reason:
   `client_manager.py:917` writes `client.json` into it. This one is not fatal,
   the component just lands in `failed_components`.
4. **The tmp directory defaults to `/userdata/.tmp`** (`server.py:679`), which is
   the printer's own layout. Anywhere else that is a startup warning. Pass
   `-t <dir>` or set `MOONRAKER_TMP_DIR`.
5. **The Klipper config path the fake Klippy reports has to sit inside
   Moonraker's own config folder**, otherwise `file_manager.py:269` to `:282`
   raises a warning. `u1sim --config-file` exists for that. The same applies to
   the gcode directory (`:284` to `:297`), which is why `--gcode-path` has to
   match what `[file_manager]` registered.

Two smaller points. `machine.py:774` shells out to `ip -json -det address`, so an
image without `iproute2` fills the log with subprocess errors, harmless but
noisy. And the shipped `[mqtt]` section points at a broker on `127.0.0.1:1883`,
so drop it unless one is running.

For the push in section 6 item 7, the method name is not fixed by the protocol.
Moonraker sends `response_template` with `{"method": "process_status_update"}`
(`klippy_connection.py:765`) and `{"method": "process_gcode_response"}` for gcode
output (`klippy_apis.py:532` to `:533`), then dispatches on those names
(`klippy_connection.py:106` to `:109`). Echo whatever template the client sent
rather than hardcoding the name.
