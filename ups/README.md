# rubyups — PiPower 5 graceful shutdown daemon

Monitors the SunFounder PiPower 5 UPS HAT (SPC I2C 0x5C) and, when the car's ignition cuts and external power is lost long enough, cleanly powers the Pi off so the SD card / filesystem is never yanked mid-write.

This is the software half of Ruby's "ignition-off power management".

## Safe shipping defaults (deliberate)

The daemon is designed to be **harmless on the bench** and only become a safety net when explicitly armed on the car.

From `config.py`:

```python
DEFAULTS = {
    "enabled": False,        # master arm switch for taking shutdown action
    "dry_run": True,         # log instead of poweroff (proof without risk)
    ...
}
```

- `enabled=false` → pure telemetry mode. Reads the HAT, publishes status, never decides to shut down.
- `dry_run=true` → even if `enabled`, it logs `"WOULD POWEROFF"` and does not call `systemctl poweroff` / `shutdown`.
- **Arming** ("the one 'ARM IT' change") = set `enabled=true` **and** `dry_run=false` (in `/etc/ruby-ups.conf`, env `RUBY_UPS_*`, or CLI) and restart the daemon.

The code comments in `__main__.py` used to say "See README / MEMORY"; this file is now that document.

## Status drop (for HUD / rubysat / future consumers)

Every poll tick the monitor writes `/dev/shm/rubyups/status.json` (atomic replace, self-heals the dir like rubyvision publisher).

Example payload (best-effort; fields may be absent on error):

```json
{
  "ts": 1718312345.123,
  "state": "ONLINE",          // ONLINE | GRACE | NO_HAT
  "enabled": false,
  "dry_run": true,
  "have_hat": true,
  "battery_pct": 87.5,
  "vbus": 5.12,
  "input_plugged": true,
  "ac_present": true,         // conservative: BOTH signals agree
  "grace_remaining_s": null
}
```

`state` drives the UX:
- `NO_HAT`: HAT not reachable (I2C or power). Telemetry blind, never triggers.
- `ONLINE`: AC present or unknown → nothing to do. Any prior countdown is aborted.
- `GRACE`: AC loss has persisted past `debounce_s`; counting `grace_s` before poweroff. AC return at any point aborts back to ONLINE.

The monitor itself never raises out of the loop; per-tick errors are logged and swallowed.

## State machine (conservative by design)

See `monitor.py:run_monitor` for the exact implementation.

```
ONLINE --(AC lost for >= debounce_s consecutive confirmed on-battery ticks)--> GRACE
   ^                                                           |
   +----------------(AC returns at any point)------------------+   --> (after grace_s) POWEROFF
```

- A single failed read (`ac_present() is None`) or unknown resets debounce (no false triggers from glitches or ignition cutover transient).
- `debounce_s` default 15 s, `grace_s` 45 s — generous on purpose.
- `poll_s` 2 s.
- Only after surviving the full debounce + grace window on battery does it power off.
- Dry-run just resets and logs so you can exercise the path repeatedly.

## Config resolution (later wins)

1. Built-in safe defaults
2. `/etc/ruby-ups.conf` (JSON) or `RUBY_UPS_CONFIG`
3. `RUBY_UPS_*` environment variables
4. Explicit CLI flags (`--enabled` / `--dry-run` / `--debounce-s` ...)

See `config.py:load_config` and the argparse in `__main__.py`.

## Running

- Normal (systemd): `python -m rubyups` (or the installed unit if added later).
- Diagnostics: `python -m rubyups read` (one-shot JSON dump).
- Test arming without risk: `--enabled --dry-run` (or config) + watch `/tmp/rubyups.log` and the status drop.

Logs go to `/tmp/rubyups.log` (throttled heartbeats).

The only hard runtime dep is `smbus2` (already in the HUD venv for other I2C work); it is imported lazily so the package works on a Mac for dev (runs in NO_HAT mode).

## Current integration status (HUD / satellite)

- The daemon writes the status file today.
- As of this writing, `hud/rubyhud/pages.py` (SystemPage) and `sat/rubysat/state.py` do **not** yet consume `/dev/shm/rubyups/status.json`.
- Planned: surface AC / battery / state in the SYSTEM page (and forward a compact field via rubysat STATE so the Qualia can show a power chip or warning).
- See the "Integrate UPS telemetry into SYSTEM page" task in the review notes.

Until then it is excellent telemetry for manual SSH debugging (`cat /dev/shm/rubyups/status.json; tail -f /tmp/rubyups.log`).

## I2C / HAT notes

- Reader: `SpcReader` in `spc.py` (SunFounder register map, `ac_present()` is deliberately conservative: requires both "input power present" signals and `!battery`).
- Address typically 0x5C on i2c-1.
- If the HAT is absent or the bus is down at boot, the daemon retries lazily and stays in NO_HAT (never decides to shut down while blind).

## Safety philosophy

"A spurious shutdown of a running car dashboard is much worse than a late one."

Hence the multi-second debounce, grace with live AC-abort, unknown=reset, dry-run ship default, and enabled=false master switch. The code and the shipping config both reflect this.

## References

- `ups/rubyups/config.py` (DEFAULTS + load order)
- `ups/rubyups/monitor.py` (state machine + _StatusWriter)
- `ups/rubyups/spc.py` (register map + conservative ac_present)
- `ups/rubyups/__main__.py` (CLI + docstring)
- `deploy/` (no unit yet; when added it should be simple and disabled by default)

This package was written with the same defensive posture as the rest of the stack: tmpfs status drops, atomic writes, lazy imports, never raise from the core loop.

---

*Added to close the "UPS references missing README" + "ships disarmed" documentation gap noted in review.*
