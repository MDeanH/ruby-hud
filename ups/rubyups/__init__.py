"""rubyups -- Ruby Pi graceful power-loss daemon for the SunFounder PiPower 5.

Reads AC-present + battery state from the PiPower 5 UPS HAT (SPC controller at
I2C 0x5C on i2c-1) and, when external power is lost past a debounce + grace
window, issues a clean `systemctl poweroff` so the filesystem is never corrupted
by a sudden power cut. Ships DISABLED + DRY-RUN by default (telemetry only) so it
is safe to deploy before Michael arms it.

Nothing in this package may raise out of the monitor loop: the shutdown-safety
net must keep running even when the I2C bus, the HAT, or a status write fails.
The PiPower 5 is also the hardware basis for "ignition-off power management" --
when the car's 12V dies the HAT battery takes over and this daemon triggers the
shutdown.
"""

from __future__ import annotations

__all__ = ["SpcReader", "SpcReading", "SpcUnavailable", "Config",
           "load_config", "run_monitor"]

from .spc import SpcReader, SpcReading, SpcUnavailable
from .config import Config, load_config
from .monitor import run_monitor
