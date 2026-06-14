"""rubyups entrypoint: python -m rubyups [options].

Monitors a SunFounder PiPower 5 UPS HAT (I2C 0x5C on i2c-1) and, when AC/external
power is lost long enough, cleanly powers Ruby off so the SD card / filesystem is
never yanked mid-write. This is the software half of the car's "ignition-off
power management": when the MX-5's ignition cuts, the 12V inverter dies, the
PiPower 5 battery takes over, and THIS daemon then debounces the loss and issues
the graceful shutdown.

Subcommands:
  (default)   run the monitor loop (systemd ExecStart uses this).
  read        read the HAT once and print JSON, then exit (diagnostics).

Run options (override config file + RUBY_UPS_* env):
  --config PATH        JSON config (default /etc/ruby-ups.conf).
  --enabled/--disabled arm/disarm taking shutdown action (default: config).
  --dry-run/--live     log "WOULD POWEROFF" vs actually poweroff.
  --debounce-s N       AC-loss persistence before grace (default 15).
  --grace-s N          grace countdown before poweroff (default 45).
  --poll-s N           bus poll period (default 2).
  --status-path PATH   status drop (default /dev/shm/rubyups/status.json).

SAFE DEFAULTS: with no config the daemon ships DISABLED + DRY-RUN -- it reads and
publishes telemetry but never powers Ruby off. See ups/README.md for the one
"ARM IT" change (set enabled + !dry_run) once Michael is comfortable.

All hardware deps (smbus2) import lazily, so this runs on a bare host: with no
PiPower 5 present it enters NO_HAT mode (telemetry blind, never triggers) and
keeps retrying the bus.
"""

from __future__ import annotations

import argparse
import json
import sys

from .config import load_config
from .monitor import run_monitor


def _build_parser():
    ap = argparse.ArgumentParser(
        prog="rubyups",
        description="Ruby PiPower 5 power-loss shutdown daemon")
    ap.add_argument("--config", default=None,
                    help="JSON config file (default /etc/ruby-ups.conf)")

    # enabled / dry_run as paired flags resolving to True/False/None(=use config)
    en = ap.add_mutually_exclusive_group()
    en.add_argument("--enabled", dest="enabled", action="store_true",
                    default=None, help="arm shutdown action")
    en.add_argument("--disabled", dest="enabled", action="store_false",
                    default=None, help="disarm (telemetry only)")

    dr = ap.add_mutually_exclusive_group()
    dr.add_argument("--dry-run", dest="dry_run", action="store_true",
                    default=None, help="log 'WOULD POWEROFF' instead of acting")
    dr.add_argument("--live", dest="dry_run", action="store_false",
                    default=None, help="actually poweroff when triggered")

    ap.add_argument("--debounce-s", dest="debounce_s", type=float, default=None,
                    help="AC-loss persistence before grace (s)")
    ap.add_argument("--grace-s", dest="grace_s", type=float, default=None,
                    help="grace countdown before poweroff (s)")
    ap.add_argument("--poll-s", dest="poll_s", type=float, default=None,
                    help="bus poll period (s)")
    ap.add_argument("--status-path", dest="status_path", default=None,
                    help="status JSON drop path")

    ap.add_argument("command", nargs="?", default="run",
                    choices=["run", "read"],
                    help="run the monitor (default) or read the HAT once")
    return ap


def _cmd_read(cfg) -> int:
    """Read the HAT once and print JSON. Exit 0 if read OK, 2 if no HAT."""
    from .spc import SpcReader, SpcUnavailable
    reader = SpcReader()
    try:
        reader.open()
    except SpcUnavailable as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 2
    try:
        reading = reader.read()
        out = reading.as_dict()
        out["firmware"] = reader.firmware
        out["addr"] = "0x%02X" % reader.addr
        out["bus"] = reader.busnum
        print(json.dumps(out))
        return 0 if reading.ok else 2
    finally:
        reader.close()


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)
    cfg = load_config(args=args, config_path=args.config)
    if args.command == "read":
        return _cmd_read(cfg)
    return run_monitor(cfg)


if __name__ == "__main__":
    sys.exit(main())
