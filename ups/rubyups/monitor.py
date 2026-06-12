"""monitor -- the ruby-ups power-loss state machine.

Polls the PiPower 5 each tick and decides whether to shut Ruby down. The design
priority is SAFETY: a spurious shutdown of a running car dashboard is much worse
than a late one, so the trigger is deliberately conservative on every axis.

State machine
-------------
    ONLINE  --(AC lost, held >= debounce_s)-->  GRACE  --(grace_s elapsed)-->  POWEROFF
       ^                                           |
       +---------------(AC returns)----------------+   (abort, back to ONLINE)

  * AC-loss must PERSIST for `debounce_s` consecutive seconds of *confirmed*
    on-battery readings before we even enter GRACE. A single dip, or a read we
    could not perform (ac_present() is None -> unknown), does NOT count and
    RESETS the debounce. This kills false triggers from bus glitches or the
    brief switchover transient when the car's ignition first cuts.
  * Once in GRACE we wait `grace_s` more seconds. If AC returns at any point in
    debounce OR grace, we abort and go back to ONLINE (logged).
  * Only after surviving debounce + grace fully on battery do we poweroff.

Everything is configurable (see config.py / CLI). `--dry-run` logs
"WOULD POWEROFF" instead of calling systemctl, so the trigger can be proven on
the live Pi without actually killing it. `enabled=False` makes the daemon a pure
telemetry reader (reads + publishes status, never shuts down) -- the safe state
to ship in.

The loop NEVER raises: any per-tick exception is logged and swallowed so a
flaky bus or a logging error can't crash the service (and thus can't, ironically,
take the shutdown-safety net offline).

Status drop
-----------
Each tick writes /dev/shm/rubyups/status.json (atomic replace) with the current
reading + state, so rubysat/HUD can later surface battery%/AC without opening
the bus themselves. Writing it is best-effort and never blocks the state machine.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time

from .spc import SpcReader, SpcUnavailable

# State names (also surfaced in the status drop for the HUD/rubysat).
ST_ONLINE = "ONLINE"      # AC present (or unknown): nothing to do.
ST_GRACE = "GRACE"        # AC-loss confirmed past debounce; counting down.
ST_NO_HAT = "NO_HAT"      # HAT not reachable: telemetry blind, never triggers.

_LOG = "/tmp/rubyups.log"
# Throttle repeated identical log lines (e.g. the per-tick ONLINE heartbeat) so
# a long run does not fill /tmp.
_LOG_HEARTBEAT_S = 30.0


def _log(msg: str) -> None:
    try:
        with open(_LOG, "a") as fh:
            fh.write("%s %s\n" % (time.strftime("%H:%M:%S"), msg))
    except Exception:
        pass


class _StatusWriter:
    """Atomic JSON status drop to /dev/shm/rubyups/status.json. Never raises.

    Self-heals the dir before each write (Debian tmpfiles can sweep tmpfs --
    the same gotcha rubyvision hit), so a cleaned /dev/shm doesn't silently
    stop telemetry."""

    def __init__(self, path: str = "/dev/shm/rubyups/status.json"):
        self.path = path
        self._dir = os.path.dirname(path)

    def write(self, payload: dict) -> None:
        try:
            os.makedirs(self._dir, exist_ok=True)
        except Exception:
            return
        try:
            line = json.dumps(payload, separators=(",", ":"))
        except Exception:
            return
        try:
            fd, tmp = tempfile.mkstemp(prefix=".status-", dir=self._dir)
            try:
                with os.fdopen(fd, "w") as fh:
                    fh.write(line)
                os.replace(tmp, self.path)
            except Exception:
                try:
                    os.unlink(tmp)
                except Exception:
                    pass
        except Exception:
            pass


def _do_poweroff(dry_run: bool) -> None:
    """Issue the actual shutdown (or log it in dry-run). Never raises."""
    if dry_run:
        _log("WOULD POWEROFF (dry-run): systemctl poweroff suppressed")
        return
    _log("POWEROFF: issuing systemctl poweroff now")
    try:
        # systemctl poweroff is the clean path; fall back to shutdown -h now.
        subprocess.Popen(["systemctl", "poweroff"])
    except Exception as exc:
        _log("systemctl poweroff failed (%s); trying shutdown -h now" % exc)
        try:
            subprocess.Popen(["shutdown", "-h", "now"])
        except Exception as exc2:
            _log("shutdown -h now also failed: %s" % exc2)


def run_monitor(cfg, reader=None, on_poweroff=None, max_ticks=None) -> int:
    """Run the power-loss state machine until SIGTERM (or max_ticks for tests).

    Args:
        cfg: a config.Config (enabled, debounce_s, grace_s, poll_s, dry_run,
             status_path, broadcast).
        reader: an SpcReader-like object (injectable for tests). Default: a real
             SpcReader bound to the PiPower 5; if it can't open, we run in
             NO_HAT mode (telemetry blind, never triggers) and keep retrying.
        on_poweroff: optional callable invoked instead of _do_poweroff (tests).
        max_ticks: stop after N ticks (tests); None = run forever.

    Returns process exit code (0).
    """
    import signal

    stop = {"flag": False}

    def _handle(signum, _frame):
        stop["flag"] = True

    try:
        signal.signal(signal.SIGTERM, _handle)
        signal.signal(signal.SIGINT, _handle)
    except Exception:
        pass  # e.g. running off the main thread in a test

    status = _StatusWriter(cfg.status_path)
    poweroff = on_poweroff or (lambda: _do_poweroff(cfg.dry_run))

    # Acquire / (re)acquire the HAT lazily so a not-yet-ready bus at boot does
    # not crash us -- we retry open() while in NO_HAT.
    owns_reader = reader is None
    if reader is None:
        reader = SpcReader()

    def _try_open() -> bool:
        try:
            reader.open()
            _log("PiPower 5 opened (firmware %s) at 0x%02X on i2c-%d"
                 % (getattr(reader, "firmware", "?"),
                    getattr(reader, "addr", 0), getattr(reader, "busnum", 0)))
            return True
        except SpcUnavailable as exc:
            return False
        except Exception as exc:
            _log("reader.open() unexpected error: %s" % exc)
            return False

    have_hat = _try_open()

    _log("ruby-ups start: enabled=%s dry_run=%s debounce=%.0fs grace=%.0fs "
         "poll=%.1fs hat=%s"
         % (cfg.enabled, cfg.dry_run, cfg.debounce_s, cfg.grace_s,
            cfg.poll_s, have_hat))

    state = ST_ONLINE if have_hat else ST_NO_HAT
    loss_since = None    # monotonic ts when confirmed AC-loss began (debounce)
    grace_until = None   # monotonic deadline to poweroff once in GRACE
    last_heartbeat = 0.0
    last_open_retry = 0.0
    ticks = 0

    while not stop["flag"]:
        now = time.monotonic()
        ticks += 1

        # If we have no HAT, periodically retry opening it (the bus may have
        # come up late, or the HAT was repowered). Until then: telemetry blind.
        if not have_hat:
            if now - last_open_retry >= max(5.0, cfg.poll_s):
                last_open_retry = now
                have_hat = _try_open()
                if have_hat:
                    state = ST_ONLINE
                    loss_since = None
                    grace_until = None
            if not have_hat:
                state = ST_NO_HAT

        reading = reader.read() if have_hat else None
        ac = reading.ac_present() if reading is not None else None

        try:
            if not have_hat:
                pass  # NO_HAT: nothing to decide
            elif not cfg.enabled:
                # Telemetry-only mode: read + publish, never act.
                state = ST_ONLINE
                loss_since = None
                grace_until = None
            elif ac is None:
                # Unknown reading: do NOT advance toward shutdown. Treat like a
                # transient and reset any in-progress debounce (conservative).
                if loss_since is not None or grace_until is not None:
                    _log("AC state unknown (read failed); resetting debounce")
                state = ST_ONLINE
                loss_since = None
                grace_until = None
            elif ac:
                # AC present. If we were counting down, abort and recover.
                if state == ST_GRACE:
                    _log("AC RESTORED during grace -- aborting shutdown")
                elif loss_since is not None:
                    _log("AC restored during debounce -- reset")
                state = ST_ONLINE
                loss_since = None
                grace_until = None
            else:
                # AC LOST (confirmed on-battery this tick).
                if loss_since is None:
                    loss_since = now
                    bp = reading.battery_pct if reading else None
                    _log("AC LOST (on battery, %s%%); debounce %.0fs"
                         % (bp if bp is not None else "?", cfg.debounce_s))
                held = now - loss_since
                if state != ST_GRACE and held >= cfg.debounce_s:
                    state = ST_GRACE
                    grace_until = now + cfg.grace_s
                    _log("AC loss confirmed (held %.0fs >= %.0fs); GRACE %.0fs "
                         "before poweroff%s"
                         % (held, cfg.debounce_s, cfg.grace_s,
                            " [DRY-RUN]" if cfg.dry_run else ""))
                if state == ST_GRACE and grace_until is not None \
                        and now >= grace_until:
                    _log("GRACE elapsed -- triggering shutdown%s"
                         % (" [DRY-RUN]" if cfg.dry_run else ""))
                    poweroff()
                    if cfg.dry_run:
                        # In dry-run we don't actually power off; reset so the
                        # logic can be exercised repeatedly without spamming.
                        state = ST_ONLINE
                        loss_since = None
                        grace_until = None
                    else:
                        # Real poweroff issued; stop deciding (we're going down).
                        break
        except Exception as exc:
            _log("tick error (swallowed): %s" % exc)

        # ---- status drop (best-effort, never blocks the machine) ---- #
        try:
            payload = {
                "ts": round(time.time(), 3),
                "state": state,
                "enabled": bool(cfg.enabled),
                "dry_run": bool(cfg.dry_run),
                "have_hat": bool(have_hat),
            }
            if reading is not None:
                payload.update(reading.as_dict())
            if state == ST_GRACE and grace_until is not None:
                payload["grace_remaining_s"] = round(
                    max(0.0, grace_until - now), 1)
            status.write(payload)
        except Exception:
            pass

        # ---- throttled heartbeat to the log ---- #
        if now - last_heartbeat >= _LOG_HEARTBEAT_S:
            last_heartbeat = now
            bp = reading.battery_pct if reading else None
            _log("hb state=%s ac=%s bat=%s%% hat=%s"
                 % (state, ac, bp if bp is not None else "?", have_hat))

        if max_ticks is not None and ticks >= max_ticks:
            break

        # Sleep in small slices so SIGTERM is responsive even with a long poll.
        slept = 0.0
        while slept < cfg.poll_s and not stop["flag"]:
            chunk = min(0.2, cfg.poll_s - slept)
            time.sleep(chunk)
            slept += chunk

    _log("ruby-ups stopping (state=%s)" % state)
    if owns_reader:
        try:
            reader.close()
        except Exception:
            pass
    return 0
