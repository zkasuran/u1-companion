"""u1sim: a fake Klippy host that speaks the Snapmaker U1 Klippy API.

The simulator binds a Unix domain socket and answers the same endpoints an
unmodified Moonraker asks for, framing every JSON document with a trailing
0x03 byte. It serves the payload shapes the U1 firmware really produces, so
Moonraker, Fluidd and a Home Assistant integration can be developed and
tested without a printer.

Every field name and default in this package is taken from the Snapmaker
forks of Klipper and Moonraker. See docs/PROTOCOL.md for the file and line
citations.
"""

__version__ = "0.1.0"

from .model import PrinterModel
from .scenario import Scenario, ScenarioRunner
from .server import U1SimServer

__all__ = ["PrinterModel", "Scenario", "ScenarioRunner", "U1SimServer", "__version__"]
