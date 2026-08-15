"""Payload parsing for the Snapmaker U1.

This module holds every rule about the shape of the printer payload and none of
the Home Assistant plumbing, so it can be imported and tested on its own.

Three colour representations exist on this firmware and they are not
interchangeable:

* ``filament_color`` is a packed ARGB int, alpha in the top byte
  (klippy/extras/print_task_config.py:653-658).
* ``filament_color_rgba`` is 8 hex characters, "RRGGBBAA".
* ``filament_color_multi.colors`` entries are 6 hex characters, "RRGGBB". The
  alpha for all of them sits in the same dict.

``filament_color`` is copied verbatim from the RFID tag's ARGB_COLOR while
``filament_color_rgba`` is rebuilt from RGB_1 plus ALPHA
(klippy/extras/print_task_config.py:325-331), so the two can disagree on an
official spool. The manual write path and the startup repair both keep rgba and
multi consistent (:645-667, :228-240), so anything shown to a user is derived
from rgba or from multi, never from the packed int. The packed int is still
exposed as an attribute for anyone who wants it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .const import (
    ACTION_CODES,
    DEFAULT_COLOR_RGBA,
    EMPTY_STRINGS,
    EXTRUDER_OBJECTS,
    LOGICAL_EXTRUDER_NUM,
    MACHINE_MAIN_STATES,
    OBJ_EXCEPTION_MANAGER,
    OBJ_FILAMENT_DETECT,
    OBJ_HEATER_BED,
    OBJ_MACHINE_STATE,
    OBJ_PAUSE_RESUME,
    OBJ_PRINT_STATS,
    OBJ_PRINT_TASK_CONFIG,
    OBJ_TOOLHEAD,
    OBJ_VIRTUAL_SDCARD,
    OBJ_WEBHOOKS,
    PHYSICAL_EXTRUDER_NUM,
    SCAN_STATES,
)

HEX_DIGITS = set("0123456789abcdefABCDEF")


def is_empty_string(value: Any) -> bool:
    """Return True for the firmware's empty string markers.

    The firmware writes "NONE" for an unset vendor, type or sub type and also
    tests for the empty string (klippy/extras/print_task_config.py:299).
    """
    if value is None:
        return True
    if not isinstance(value, str):
        return False
    return value.strip().upper() in EMPTY_STRINGS


def clean_string(value: Any) -> str | None:
    """Return the string or None when it is an empty marker."""
    if is_empty_string(value):
        return None
    if not isinstance(value, str):
        return None
    return value.strip()


def is_hex(text: Any, length: int) -> bool:
    """Return True when text is exactly length hex characters."""
    if not isinstance(text, str) or len(text) != length:
        return False
    return all(char in HEX_DIGITS for char in text)


def argb_parts(value: Any) -> tuple[int, int, int, int] | None:
    """Split a packed ARGB int into (alpha, red, green, blue).

    Same decode the firmware uses at
    klippy/extras/print_task_config.py:653-658.
    """
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    value &= 0xFFFFFFFF
    return (
        (value >> 24) & 0xFF,
        (value >> 16) & 0xFF,
        (value >> 8) & 0xFF,
        value & 0xFF,
    )


def argb_to_hex(value: Any) -> str | None:
    """Return "#RRGGBB" for a packed ARGB int, dropping the alpha."""
    parts = argb_parts(value)
    if parts is None:
        return None
    _, red, green, blue = parts
    return f"#{red:02X}{green:02X}{blue:02X}"


def split_rgba(text: Any) -> tuple[str, int] | None:
    """Split "RRGGBBAA" into ("RRGGBB", alpha).

    The firmware writes uppercase and _early_check enforces exactly 8
    characters (klippy/extras/print_task_config.py:212-213), but parsing stays
    case insensitive.
    """
    if not is_hex(text, 8):
        return None
    assert isinstance(text, str)
    return text[0:6].upper(), int(text[6:8], 16)


def hex_color(text: Any) -> str | None:
    """Return "#RRGGBB" for a 6 character hex string."""
    if not is_hex(text, 6):
        return None
    assert isinstance(text, str)
    return f"#{text.upper()}"


def merge_status(store: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    """Merge a status update into a stored snapshot, in place.

    Klipper only emits fields whose value changed since the last push
    (klippy/webhooks.py:533-538) and Moonraker forwards that subset per
    connection (klippy_connection.py:663-672), so an update is a delta and has
    to be merged rather than swapped in. The delta is per field: a changed list
    or nested dict arrives whole, which is why fields are replaced and not
    merged recursively.
    """
    for name, fields in update.items():
        if not isinstance(fields, dict):
            store[name] = fields
            continue
        current = store.get(name)
        if isinstance(current, dict):
            current.update(fields)
        else:
            store[name] = dict(fields)
    return store


def _list_item(values: Any, index: int, default: Any = None) -> Any:
    """Read values[index] without raising on a short or missing list."""
    if not isinstance(values, list) or index >= len(values) or index < 0:
        return default
    return values[index]


@dataclass(frozen=True)
class Slot:
    """One of the four physical filament slots of a U1.

    Every field comes from an array in print_task_config, indexed by the
    physical head number.
    """

    index: int
    present: bool
    in_use: bool
    official: bool
    soft: bool
    user_editable: bool
    sku: int | None
    vendor: str | None
    filament_type: str | None
    sub_type: str | None
    color: str | None
    alpha: int | None
    colors: list[str] = field(default_factory=list)
    color_count: int | None = None
    color_mode: int | None = None
    argb: int | None = None
    argb_color: str | None = None
    assigned_colors: list[int] = field(default_factory=list)

    @property
    def loaded(self) -> bool:
        """True when the slot has an identity written to it."""
        return self.vendor is not None or self.filament_type is not None

    @property
    def is_gradient(self) -> bool:
        """True when the spool carries more than one colour."""
        return len(self.colors) > 1

    @property
    def color_mismatch(self) -> bool:
        """True when the packed ARGB int disagrees with the rgba string.

        The firmware can leave the two out of step on an RFID spool
        (klippy/extras/print_task_config.py:325-331). Surfaced as an attribute
        so a user can see why a colour looks odd rather than guessing.
        """
        if self.color is None or self.argb_color is None:
            return False
        return self.color != self.argb_color

    def color_attributes(self) -> dict[str, Any]:
        """Colour attributes in a shape a dashboard template can use."""
        return {
            "color": self.color,
            "colors": list(self.colors),
            "color_count": self.color_count,
            "color_mode": self.color_mode,
            "alpha": self.alpha,
            "argb": self.argb,
            "argb_color": self.argb_color,
            "gradient": self.is_gradient,
            "color_mismatch": self.color_mismatch,
        }


def assigned_logical_colors(map_table: Any, head: int) -> list[int]:
    """Logical colour indices currently mapped onto a physical head.

    extruder_map_table is 32 long and each entry is a head number 0..3
    (klippy/extras/print_task_config.py:38). Its unused tail defaults to zeros,
    so head 0 owns every logical colour a job never touched. That is what the
    printer really reports, so it is passed through as is.
    """
    if not isinstance(map_table, list):
        return []
    return [
        logical for logical, mapped in enumerate(map_table[:LOGICAL_EXTRUDER_NUM]) if mapped == head
    ]


def build_slot(config: dict[str, Any], index: int) -> Slot:
    """Build one Slot from a print_task_config payload."""
    vendor = clean_string(_list_item(config.get("filament_vendor"), index))
    filament_type = clean_string(_list_item(config.get("filament_type"), index))
    sub_type = clean_string(_list_item(config.get("filament_sub_type"), index))
    identity_known = vendor is not None or filament_type is not None

    rgba_raw = _list_item(config.get("filament_color_rgba"), index)
    rgba = split_rgba(rgba_raw)
    # A slot that was never written keeps the factory default. Reporting that
    # as white would invent a filament, so it reads as unknown instead. A slot
    # with a known filament that really is white keeps its colour.
    unset = rgba is None or (
        isinstance(rgba_raw, str) and rgba_raw.upper() == DEFAULT_COLOR_RGBA and not identity_known
    )

    color: str | None = None
    alpha: int | None = None
    colors: list[str] = []
    color_count: int | None = None
    color_mode: int | None = None
    argb: int | None = None
    argb_color: str | None = None

    if not unset and rgba is not None:
        color = f"#{rgba[0]}"
        alpha = rgba[1]
        multi = _list_item(config.get("filament_color_multi"), index)
        if isinstance(multi, dict):
            nums = multi.get("nums")
            raw_colors = multi.get("colors")
            if not isinstance(nums, int) or isinstance(nums, bool):
                nums = None
            color_count = nums
            mode = multi.get("mode")
            color_mode = mode if isinstance(mode, int) else None
            if isinstance(raw_colors, list):
                wanted = raw_colors if nums is None else raw_colors[:nums]
                colors = [c for c in (hex_color(item) for item in wanted) if c]
        if not colors:
            colors = [color]
            color_count = color_count or 1
        raw_argb = _list_item(config.get("filament_color"), index)
        if isinstance(raw_argb, int) and not isinstance(raw_argb, bool):
            argb = raw_argb & 0xFFFFFFFF
            argb_color = argb_to_hex(argb)

    sku = _list_item(config.get("filament_sku"), index)
    if not isinstance(sku, int) or isinstance(sku, bool) or sku == 0:
        # 0 is the firmware's "no SKU" (klippy/extras/print_task_config.py:35).
        sku = None

    return Slot(
        index=index,
        present=bool(_list_item(config.get("filament_exist"), index, False)),
        in_use=bool(_list_item(config.get("extruders_used"), index, False)),
        official=bool(_list_item(config.get("filament_official"), index, False)),
        soft=bool(_list_item(config.get("filament_soft"), index, False)),
        user_editable=bool(_list_item(config.get("filament_edit"), index, False)),
        sku=sku,
        vendor=vendor,
        filament_type=filament_type,
        sub_type=sub_type,
        color=color,
        alpha=alpha,
        colors=colors,
        color_count=color_count,
        color_mode=color_mode,
        argb=argb,
        argb_color=argb_color,
        assigned_colors=assigned_logical_colors(config.get("extruder_map_table"), index),
    )


def rgb_int_to_hex(value: Any) -> str | None:
    """Return "#RRGGBB" for one of the tag's RGB_n integers."""
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    return f"#{value & 0xFFFFFF:06X}"


def _positive(value: Any) -> Any:
    """Return the number or None when it is zero or not a number.

    The RFID tag struct uses 0 for "not stated" on every numeric field
    (klippy/extras/filament_protocol.py:6-38).
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if value > 0 else None


def _number(value: Any) -> float | None:
    """Return the value as a float or None when it is not a number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


@dataclass(frozen=True)
class TagInfo:
    """One RFID tag reading from filament_detect.info.

    Field names are the tag's own, decoded by the FM175xx reader into
    FILAMENT_INFO_STRUCT (klippy/extras/filament_protocol.py:6-38).
    """

    index: int
    vendor: str | None
    manufacturer: str | None
    main_type: str | None
    sub_type: str | None
    official: bool
    sku: int | None
    weight_g: Any = None
    length: Any = None
    diameter: Any = None
    drying_temp: Any = None
    drying_time: Any = None
    hotend_min_temp: Any = None
    hotend_max_temp: Any = None
    first_layer_temp: Any = None
    other_layer_temp: Any = None
    bed_temp: Any = None
    bed_type: Any = None
    tray: Any = None
    mf_date: str | None = None
    card_uid: Any = None
    protocol_version: Any = None
    rsa_key_version: Any = None
    color: str | None = None
    colors: list[str] = field(default_factory=list)
    multi_mode: Any = None
    alpha: Any = None


def build_tag(info: Any, index: int) -> TagInfo | None:
    """Build a TagInfo or None when the channel has no tag.

    A channel with no spool keeps the struct defaults, VENDOR "NONE" among
    them, so that is the test for an empty reading.
    """
    if not isinstance(info, dict):
        return None
    vendor = clean_string(info.get("VENDOR"))
    if vendor is None:
        return None

    nums = info.get("COLOR_NUMS")
    if not isinstance(nums, int) or isinstance(nums, bool) or nums < 1:
        nums = 1
    colors = [
        hex_color_value
        for hex_color_value in (rgb_int_to_hex(info.get(f"RGB_{n}")) for n in range(1, nums + 1))
        if hex_color_value
    ]

    mf_date = clean_string(info.get("MF_DATE"))
    if mf_date == "19700101":
        # Struct default, not a real manufacturing date.
        mf_date = None

    return TagInfo(
        index=index,
        vendor=vendor,
        manufacturer=clean_string(info.get("MANUFACTURER")),
        main_type=clean_string(info.get("MAIN_TYPE")),
        sub_type=clean_string(info.get("SUB_TYPE")),
        official=bool(info.get("OFFICIAL", False)),
        sku=_positive(info.get("SKU")),
        weight_g=_positive(info.get("WEIGHT")),
        length=_positive(info.get("LENGTH")),
        diameter=_positive(info.get("DIAMETER")),
        drying_temp=_positive(info.get("DRYING_TEMP")),
        drying_time=_positive(info.get("DRYING_TIME")),
        hotend_min_temp=_positive(info.get("HOTEND_MIN_TEMP")),
        hotend_max_temp=_positive(info.get("HOTEND_MAX_TEMP")),
        first_layer_temp=_positive(info.get("FIRST_LAYER_TEMP")),
        other_layer_temp=_positive(info.get("OTHER_LAYER_TEMP")),
        bed_temp=_positive(info.get("BED_TEMP")),
        bed_type=info.get("BED_TYPE"),
        tray=info.get("TRAY"),
        mf_date=mf_date,
        card_uid=info.get("CARD_UID"),
        protocol_version=info.get("VERSION"),
        rsa_key_version=info.get("RSA_KEY_VERSION"),
        color=rgb_int_to_hex(info.get("RGB_1")),
        colors=colors,
        multi_mode=info.get("MULTI_MODE"),
        alpha=info.get("ALPHA"),
    )


def job_color_weights(metadata: Any) -> dict[int, float]:
    """Grams per logical colour, read from the sliced file's own metadata.

    The printer does not publish per colour usage. print_task_config_2 holds
    filament_used_g per logical extruder but it is never returned by any
    get_status (klippy/extras/print_task_config.py:503), so the only per colour
    figure available to a client is the slicer's estimate, parsed by Moonraker
    into filament_weight (a list, one entry per filament, grams) and served by
    GET /server/files/metadata
    (moonraker/components/file_manager/metadata.py:1145, :456-462).

    These are estimates from the file, not measurements from the printer.
    Every entity built on them says so.
    """
    if not isinstance(metadata, dict):
        return {}
    weights = metadata.get("filament_weight")
    if not isinstance(weights, list):
        return {}
    result: dict[int, float] = {}
    for logical, grams in enumerate(weights[:LOGICAL_EXTRUDER_NUM]):
        if isinstance(grams, bool) or not isinstance(grams, (int, float)):
            continue
        result[logical] = float(grams)
    return result


def job_head_weights(map_table: Any, metadata: Any) -> dict[int, float]:
    """Grams per physical head, summing the colours mapped onto it."""
    per_color = job_color_weights(metadata)
    if not per_color:
        return {}
    result = dict.fromkeys(range(PHYSICAL_EXTRUDER_NUM), 0.0)
    table = map_table if isinstance(map_table, list) else []
    for logical, grams in per_color.items():
        head = _list_item(table, logical)
        if isinstance(head, bool) or not isinstance(head, int):
            continue
        if 0 <= head < PHYSICAL_EXTRUDER_NUM:
            result[head] += grams
    return result


def active_tool_index(extruder_name: Any) -> int | None:
    """Map toolhead.extruder to a head number: "extruder" is 0, "extruder2" 2."""
    if not isinstance(extruder_name, str) or not extruder_name:
        return None
    if extruder_name == "extruder":
        return 0
    if extruder_name.startswith("extruder"):
        tail = extruder_name[len("extruder") :]
        if tail.isdigit():
            return int(tail)
    return None


class U1State:
    """Everything the integration knows about one printer.

    The coordinator owns one of these. It holds the merged printer status, the
    object list Moonraker reported, the klippy info block and, when a job is
    loaded, the sliced file's metadata.
    """

    def __init__(self) -> None:
        self.status: dict[str, Any] = {}
        self.objects: tuple[str, ...] = ()
        self.printer_info: dict[str, Any] = {}
        self.server_info: dict[str, Any] = {}
        self.eventtime: float | None = None
        self.klippy_connected: bool = False
        self.push_active: bool = False
        self.job_metadata: dict[str, Any] | None = None
        self.job_metadata_filename: str | None = None

    def set_objects(self, names: Any) -> None:
        """Record the object list from objects/list."""
        if isinstance(names, (list, tuple)):
            self.objects = tuple(str(name) for name in names)

    def apply_snapshot(self, status: Any, eventtime: float | None = None) -> None:
        """Replace the stored status with a full snapshot.

        Each object's field dict is copied. Without that copy the stored state
        would alias the dicts inside the response body. The next apply_update
        would then write its delta back into the caller's payload through
        merge_status.
        """
        self.status = {}
        if isinstance(status, dict):
            for name, fields in status.items():
                self.status[name] = dict(fields) if isinstance(fields, dict) else fields
        if eventtime is not None:
            self.eventtime = eventtime
        self.klippy_connected = True

    def apply_update(self, status: Any, eventtime: float | None = None) -> None:
        """Merge a partial status update into the stored snapshot."""
        if isinstance(status, dict):
            merge_status(self.status, status)
        if eventtime is not None:
            self.eventtime = eventtime

    def set_job_metadata(self, filename: str | None, metadata: dict[str, Any] | None) -> None:
        """Store the sliced file metadata for the named file."""
        self.job_metadata_filename = filename
        self.job_metadata = metadata

    def has_object(self, name: str) -> bool:
        """True when the printer publishes this object.

        The object list is authoritative when Moonraker gave us one. Before
        that, presence in the status snapshot is the next best test.
        """
        if self.objects:
            return name in self.objects
        return name in self.status

    def obj(self, name: str) -> dict[str, Any]:
        """Return one object's fields or an empty dict."""
        value = self.status.get(name)
        return value if isinstance(value, dict) else {}

    # print_task_config, the U1 specific object -----------------------------

    @property
    def print_task_config(self) -> dict[str, Any]:
        return self.obj(OBJ_PRINT_TASK_CONFIG)

    @property
    def is_u1(self) -> bool:
        """True when the printer publishes print_task_config."""
        return self.has_object(OBJ_PRINT_TASK_CONFIG)

    def slot(self, index: int) -> Slot:
        return build_slot(self.print_task_config, index)

    def slots(self) -> list[Slot]:
        return [self.slot(index) for index in range(PHYSICAL_EXTRUDER_NUM)]

    @property
    def color_map(self) -> list[int]:
        """extruder_map_table, logical colour index to physical head."""
        table = self.print_task_config.get("extruder_map_table")
        if not isinstance(table, list):
            return []
        return [value for value in table[:LOGICAL_EXTRUDER_NUM]]

    def preference(self, key: str) -> Any:
        return self.print_task_config.get(key)

    @property
    def entangle_sensitivity(self) -> str | None:
        value = self.print_task_config.get("filament_entangle_sen")
        return value if isinstance(value, str) else None

    # RFID ------------------------------------------------------------------

    @property
    def filament_detect(self) -> dict[str, Any]:
        return self.obj(OBJ_FILAMENT_DETECT)

    def tag(self, index: int) -> TagInfo | None:
        info = _list_item(self.filament_detect.get("info"), index)
        return build_tag(info, index)

    def scan_state(self, index: int) -> str | None:
        value = _list_item(self.filament_detect.get("state"), index)
        if not isinstance(value, int) or isinstance(value, bool):
            return None
        return SCAN_STATES.get(value, f"unknown_{value}")

    # The print job ---------------------------------------------------------

    @property
    def print_stats(self) -> dict[str, Any]:
        return self.obj(OBJ_PRINT_STATS)

    @property
    def print_state(self) -> str | None:
        value = self.print_stats.get("state")
        return value if isinstance(value, str) else None

    @property
    def print_message(self) -> str | None:
        return clean_string(self.print_stats.get("message"))

    @property
    def print_exception(self) -> Any:
        return self.print_stats.get("exception")

    @property
    def filename(self) -> str | None:
        return clean_string(self.print_stats.get("filename"))

    @property
    def progress_percent(self) -> float | None:
        """virtual_sdcard.progress as a percent.

        The field is 0.0 to 1.0 (klippy/extras/virtual_sdcard.py:236-244).
        """
        value = self.obj(OBJ_VIRTUAL_SDCARD).get("progress")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return round(value * 100, 1)

    @property
    def current_layer(self) -> int | None:
        info = self.print_stats.get("info")
        if not isinstance(info, dict):
            return None
        value = info.get("current_layer")
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    @property
    def total_layer(self) -> int | None:
        info = self.print_stats.get("info")
        if not isinstance(info, dict):
            return None
        value = info.get("total_layer")
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    @property
    def print_duration(self) -> float | None:
        return _number(self.print_stats.get("print_duration"))

    @property
    def total_duration(self) -> float | None:
        return _number(self.print_stats.get("total_duration"))

    @property
    def filament_used_mm(self) -> float | None:
        """print_stats.filament_used, in millimetres, the whole job as one total.

        klippy/extras/print_stats.py:330. There is no per colour split in
        printer state.
        """
        return _number(self.print_stats.get("filament_used"))

    @property
    def is_paused(self) -> bool | None:
        value = self.obj(OBJ_PAUSE_RESUME).get("is_paused")
        return bool(value) if isinstance(value, bool) else None

    @property
    def writes_blocked(self) -> bool:
        """True when the firmware refuses config writes.

        SET_PRINT_EXTRUDER_MAP, SET_PRINT_USED_EXTRUDERS and
        SET_PRINT_TASK_PARAMETERS are all refused while printing or paused
        (klippy/extras/print_task_config.py:511-519, :788-796, :1063-1073).
        """
        return self.print_state in ("printing", "paused")

    # Per colour usage, from the sliced file rather than the printer ---------

    @property
    def job_color_grams(self) -> dict[int, float]:
        return job_color_weights(self.job_metadata)

    @property
    def job_head_grams(self) -> dict[int, float]:
        return job_head_weights(self.print_task_config.get("extruder_map_table"), self.job_metadata)

    def head_job_grams(self, head: int) -> float | None:
        weights = self.job_head_grams
        if head not in weights:
            return None
        return round(weights[head], 2)

    # Heads and heaters -----------------------------------------------------

    def head_object_name(self, head: int) -> str:
        """The printer object name for a physical head."""
        if 0 <= head < len(EXTRUDER_OBJECTS):
            return EXTRUDER_OBJECTS[head]
        return f"extruder{head}"

    def head(self, index: int) -> dict[str, Any]:
        return self.obj(self.head_object_name(index))

    def head_temperature(self, index: int) -> float | None:
        return _number(self.head(index).get("temperature"))

    def head_target(self, index: int) -> float | None:
        return _number(self.head(index).get("target"))

    def dock_state(self, index: int) -> str | None:
        """The park detector's own state, PARKED, ACTIVATE or UNKNOWN.

        Only present when the head has a park detector configured
        (klippy/kinematics/extruder.py:716-717,
        klippy/extras/park_detector.py:70-82).
        """
        value = self.head(index).get("state")
        return value if isinstance(value, str) else None

    @property
    def active_extruder_object(self) -> str | None:
        value = self.obj(OBJ_TOOLHEAD).get("extruder")
        return value if isinstance(value, str) else None

    @property
    def active_tool(self) -> int | None:
        """The physical head currently active.

        toolhead.extruder is the object name. The extruder object also carries
        extruder_index (klippy/kinematics/extruder.py:713), which is preferred
        when it is there because it is the printer's own numbering.
        """
        name = self.active_extruder_object
        if name:
            index = self.obj(name).get("extruder_index")
            if isinstance(index, int) and not isinstance(index, bool):
                return index
        return active_tool_index(name)

    @property
    def bed_temperature(self) -> float | None:
        return _number(self.obj(OBJ_HEATER_BED).get("temperature"))

    @property
    def bed_target(self) -> float | None:
        return _number(self.obj(OBJ_HEATER_BED).get("target"))

    # Machine level ---------------------------------------------------------

    @property
    def klippy_state(self) -> str | None:
        """webhooks.state: ready, startup, shutdown or error.

        klippy/klippy.py:293-302 is the whole set of values.
        """
        value = self.obj(OBJ_WEBHOOKS).get("state")
        if isinstance(value, str):
            return value
        value = self.printer_info.get("state")
        return value if isinstance(value, str) else None

    @property
    def klippy_message(self) -> str | None:
        value = self.obj(OBJ_WEBHOOKS).get("state_message")
        if isinstance(value, str) and value.strip():
            return value.strip()
        value = self.printer_info.get("state_message")
        return value.strip() if isinstance(value, str) and value.strip() else None

    @property
    def machine_state(self) -> str | None:
        """machine_state_manager.main_state, decoded to the firmware's own name.

        main_state is a MachineMainState IntEnum and get_status hands back the
        member itself (klippy/extras/machine_state_manager.py:9-27, :322-326).
        An IntEnum serialises to JSON as its number, so what reaches a client is
        an int, not the name that __str__ would print inside Klippy. A real
        Moonraker running on the simulator returns main_state 1 while printing.
        A fork that sends the name instead is still accepted.
        """
        value = self.obj(OBJ_MACHINE_STATE).get("main_state")
        if isinstance(value, str):
            return value.strip().lower() or None
        if isinstance(value, int) and not isinstance(value, bool):
            return MACHINE_MAIN_STATES.get(value, f"unknown_{value}")
        return None

    @property
    def action_code(self) -> Any:
        """The raw action_code number, passed through as the printer sent it."""
        return self.obj(OBJ_MACHINE_STATE).get("action_code")

    @property
    def action_name(self) -> str | None:
        """action_code decoded, same reasoning as machine_state above."""
        value = self.action_code
        if isinstance(value, str):
            return value.strip().lower() or None
        if isinstance(value, int) and not isinstance(value, bool):
            return ACTION_CODES.get(value, f"unknown_{value}")
        return None

    @property
    def exceptions(self) -> list[Any]:
        value = self.obj(OBJ_EXCEPTION_MANAGER).get("exceptions")
        return list(value) if isinstance(value, list) else []

    @property
    def hostname(self) -> str | None:
        value = self.printer_info.get("hostname")
        return value if isinstance(value, str) and value else None

    @property
    def software_version(self) -> str | None:
        value = self.printer_info.get("software_version")
        return value if isinstance(value, str) and value else None

    @property
    def available(self) -> bool:
        """True when the data on hand is worth showing.

        Moonraker drops every subscription and clears its cache when Klippy
        goes away (moonraker/components/klippy_connection.py:896-899), so a
        disconnect means the stored status is stale.
        """
        return self.klippy_connected and self.klippy_state == "ready"
