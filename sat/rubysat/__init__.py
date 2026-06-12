"""rubysat -- Ruby Pi state publisher for the Qualia ESP32-S3 LVGL satellite.

Reads vehicle state from rubyhud's DataLayer (CAN), vision status from
rubyvision's /dev/shm status drop, and SoC temperature from sysfs, then maps
them into a newline-delimited JSON STATE line broadcast over TCP to one or more
Qualia clients (Ruby = server on 0.0.0.0:7878). The Qualia renders gauges
locally in LVGL and sends touch CMD lines back.

Nothing in this package may raise out of the publish loop or the server: the
display must keep updating even when CAN, vision, or a client socket dies.
"""

from __future__ import annotations

__all__ = ["TcpStateServer", "build_state"]

from .publisher import TcpStateServer
from .state import build_state
