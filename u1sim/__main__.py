"""Command line entry point.

    python -m u1sim --socket /tmp/u1sim/klippy.sock --scenario four_color_print

Run it before or after Moonraker: Moonraker polls for the socket file every
0.25 s until it appears (klippy_connection.py:296-309), so the order does not
matter.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys

from . import __version__
from .model import PrinterModel
from .scenario import Scenario, ScenarioError, available_scenarios
from .server import U1SimServer

DEFAULT_SOCKET = "/tmp/u1sim/klippy.sock"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m u1sim",
        description="A fake Klippy host that speaks the Snapmaker U1 API.",
    )
    parser.add_argument(
        "--socket",
        default=os.getenv("U1SIM_SOCKET", DEFAULT_SOCKET),
        help="Unix socket to bind. Point moonraker.conf klippy_uds_address here.",
    )
    parser.add_argument(
        "--scenario",
        default=os.getenv("U1SIM_SCENARIO", "four_color_print"),
        help="scenario name or path. Use none for an idle printer",
    )
    parser.add_argument(
        "--speed",
        type=float,
        default=float(os.getenv("U1SIM_SPEED", "1.0")),
        help="scenario time multiplier, 2.0 runs the timeline twice as fast",
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help="restart the scenario when it ends, whatever the file says",
    )
    parser.add_argument(
        "--hostname",
        default=os.getenv("U1SIM_HOSTNAME", "u1sim"),
        help="hostname reported by the info endpoint",
    )
    parser.add_argument(
        "--gcode-path",
        default=os.getenv("U1SIM_GCODE_PATH", ""),
        help="directory reported as configfile.config.virtual_sdcard.path",
    )
    parser.add_argument(
        "--config-file",
        default=os.getenv("U1SIM_CONFIG_FILE", ""),
        help=(
            "path reported as the Klipper config file. Moonraker warns when it "
            "is not inside its own config folder (file_manager.py:269-282), so "
            "point this at <data path>/config/printer.cfg to run warning free."
        ),
    )
    parser.add_argument(
        "--log-file",
        default=os.getenv("U1SIM_LOG_FILE", ""),
        help="path reported as the Klipper log file",
    )
    parser.add_argument(
        "--list-scenarios", action="store_true", help="print the bundled scenarios and exit"
    )
    parser.add_argument("--verbose", action="store_true", help="log every connection")
    parser.add_argument("--version", action="version", version=f"u1sim {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    if args.list_scenarios:
        for name in available_scenarios():
            print(name)
        return 0

    scenario = None
    if args.scenario and args.scenario.lower() not in ("none", "off", ""):
        try:
            scenario = Scenario.load(args.scenario)
        except ScenarioError as exc:
            print(f"u1sim: {exc}", file=sys.stderr)
            print("known scenarios: {}".format(", ".join(available_scenarios())), file=sys.stderr)
            return 2
        if args.loop:
            scenario.loop = True

    gcode_path = args.gcode_path or os.path.join(
        os.path.dirname(os.path.abspath(args.socket)), "gcodes"
    )
    model = PrinterModel(hostname=args.hostname, gcode_path=gcode_path)
    if args.config_file:
        model.config_file = args.config_file
    if args.log_file:
        model.log_file = args.log_file
    server = U1SimServer(
        socket_path=args.socket,
        model=model,
        scenario=scenario,
        speed=args.speed,
        gcode_path=gcode_path,
    )

    def handle_signal(signum, frame):
        logging.info("u1sim: signal %s, shutting down", signum)
        server.shutdown()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    server.bind()
    logging.info(
        "u1sim %s ready. scenario=%s speed=%s objects=%d",
        __version__,
        scenario.name if scenario else "none",
        args.speed,
        len(model.objects()),
    )
    if scenario is not None:
        logging.info("u1sim scenario: %s", json.dumps(scenario.to_dict()))
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
