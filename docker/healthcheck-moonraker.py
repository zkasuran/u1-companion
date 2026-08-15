#!/usr/bin/env python
"""Container healthcheck for Moonraker.

Healthy means Moonraker answered and has a ready Klippy behind it. Anything
less is no use to a client, so it does not count as up. Standard library only,
so the image needs nothing extra.
"""

from __future__ import annotations

import json
import os
import urllib.request

URL = os.getenv("MOONRAKER_HEALTH_URL", "http://127.0.0.1:7125/server/info")


def main() -> int:
    try:
        with urllib.request.urlopen(URL, timeout=5) as response:
            info = json.load(response).get("result", {})
    except Exception as exc:
        print(f"{URL}: {exc}")
        return 1
    if not info.get("klippy_connected"):
        print("klippy is not connected")
        return 1
    state = info.get("klippy_state")
    if state != "ready":
        print(f"klippy_state is {state}")
        return 1
    print("ready")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
