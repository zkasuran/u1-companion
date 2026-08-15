"""Wire protocol helpers for the Klippy Unix socket API.

Framing: one JSON document per message, followed by a single 0x03 byte. No
length prefix and no newline. Klippy splits its read buffer on 0x03 and keeps
the trailing partial document (klippy/webhooks.py:247-249). It appends 0x03
after every document it writes (webhooks.py:288). Moonraker matches with
readuntil(b'\\x03') and a trailing-byte strip
(moonraker/components/klippy_connection.py:194, :208, :221).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

TERMINATOR = b"\x03"

# Klippy answers a failed request with this shape (webhooks.py:34-37).
ERROR_NAME = "WebRequestError"


class WebRequestError(Exception):
    """Mirrors klippy.webhooks.WebRequestError.

    An endpoint that raises this produces an error reply rather than a
    simulator crash. Klippy re-raises most handler errors into a shutdown,
    the one exception being gcode/script (webhooks.py:269-279), so nothing in
    the simulator may raise anything else out of a handler.
    """

    def to_dict(self) -> dict[str, str]:
        return {"error": ERROR_NAME, "message": str(self)}


def encode(document: dict[str, Any]) -> bytes:
    """Serialise one document and append the 0x03 terminator."""
    return json.dumps(document, separators=(",", ":")).encode("utf-8") + TERMINATOR


def decode_stream(buffer: bytes) -> tuple[list[bytes], bytes]:
    """Split a read buffer into complete documents plus the trailing partial.

    This is the exact split Klippy performs, so a document arriving in
    several reads and several documents arriving in one read both work.
    """
    parts = buffer.split(TERMINATOR)
    partial = parts.pop()
    return parts, partial


def iter_documents(buffer: bytes) -> Iterator[dict[str, Any]]:
    """Yield parsed documents from a complete buffer. Used by tests."""
    parts, _partial = decode_stream(buffer)
    for raw in parts:
        if raw:
            yield json.loads(raw.decode("utf-8"))


class Request:
    """One decoded client request.

    Klippy requires method to be a str and params to be a dict, otherwise the
    request is logged and dropped with no reply at all (webhooks.py:52-53,
    :252-259). A request without an id also gets no reply (webhooks.py:100).
    """

    def __init__(self, raw: bytes) -> None:
        base = json.loads(raw.decode("utf-8"))
        if not isinstance(base, dict):
            raise ValueError("Not a top-level dictionary")
        self.id = base.get("id", None)
        self.method = base.get("method")
        self.params = base.get("params", {})
        if not isinstance(self.method, str) or not isinstance(self.params, dict):
            raise ValueError("Invalid request type")

    @property
    def wants_reply(self) -> bool:
        return self.id is not None

    def get(self, key: str, default: Any = None) -> Any:
        return self.params.get(key, default)

    def get_dict(self, key: str, default: Any = None) -> Any:
        value = self.params.get(key, default)
        if value is not default and not isinstance(value, dict):
            raise WebRequestError(f"Invalid Argument Type [{key}]")
        return value

    def get_str(self, key: str, default: Any = None) -> Any:
        value = self.params.get(key, default)
        if value is not default and not isinstance(value, str):
            raise WebRequestError(f"Invalid Argument Type [{key}]")
        return value

    def get_int(self, key: str, default: Any = None) -> Any:
        value = self.params.get(key, default)
        if value is default:
            return default
        if isinstance(value, bool) or not isinstance(value, int):
            raise WebRequestError(f"Invalid Argument Type [{key}]")
        return value

    def get_float(self, key: str, default: Any = None) -> Any:
        value = self.params.get(key, default)
        if value is default:
            return default
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise WebRequestError(f"Invalid Argument Type [{key}]")
        return float(value)

    def require(self, key: str) -> Any:
        if key not in self.params:
            raise WebRequestError(f"Missing Argument [{key}]")
        return self.params[key]


def success(request_id: Any, payload: Any) -> dict[str, Any]:
    """Build a success reply. An empty payload becomes {} (webhooks.py:105-108)."""
    if payload is None:
        payload = {}
    return {"id": request_id, "result": payload}


def failure(request_id: Any, message: str) -> dict[str, Any]:
    return {"id": request_id, "error": {"error": ERROR_NAME, "message": message}}
