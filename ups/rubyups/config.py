"""Config for ruby-ups.

Resolution order (later overrides earlier):
  1. built-in safe defaults (below),
  2. an optional JSON config file (default /etc/ruby-ups.conf, override with
     --config / RUBY_UPS_CONFIG),
  3. environment variables (RUBY_UPS_*),
  4. explicit CLI flags.

Shipping defaults are SAFE: enabled=False, dry_run=True. The daemon therefore
reads + publishes telemetry but will not power Ruby off until Michael flips it
(see the README/MEMORY "ARM IT" instruction). debounce/grace are generous.
"""

from __future__ import annotations

import json
import os

DEFAULT_CONFIG_PATH = "/etc/ruby-ups.conf"

# --- safe shipping defaults ------------------------------------------------- #
# enabled=False -> never shuts down (pure telemetry). dry_run=True -> even when
# enabled, logs "WOULD POWEROFF" instead of acting. Arming = enabled+!dry_run.
DEFAULTS = {
    "enabled": False,        # master arm switch for taking shutdown action
    "dry_run": True,         # log instead of poweroff (proof without risk)
    "debounce_s": 15.0,      # AC-loss must persist this long before GRACE
    "grace_s": 45.0,         # then wait this long (AC-return aborts) before off
    "poll_s": 2.0,           # bus poll period
    "status_path": "/dev/shm/rubyups/status.json",
    "broadcast": False,      # reserved: warn other services on power loss
}

_BOOL_KEYS = ("enabled", "dry_run", "broadcast")
_FLOAT_KEYS = ("debounce_s", "grace_s", "poll_s")
_STR_KEYS = ("status_path",)
_ENV = {
    "enabled": "RUBY_UPS_ENABLED",
    "dry_run": "RUBY_UPS_DRY_RUN",
    "debounce_s": "RUBY_UPS_DEBOUNCE_S",
    "grace_s": "RUBY_UPS_GRACE_S",
    "poll_s": "RUBY_UPS_POLL_S",
    "status_path": "RUBY_UPS_STATUS_PATH",
    "broadcast": "RUBY_UPS_BROADCAST",
}


def _as_bool(v, default):
    if isinstance(v, bool):
        return v
    if v is None:
        return default
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "on", "y"):
        return True
    if s in ("0", "false", "no", "off", "n"):
        return False
    return default


def _as_float(v, default, minimum=0.0):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return f if f >= minimum else default


class Config:
    __slots__ = tuple(DEFAULTS.keys())

    def __init__(self, **kw):
        for k, d in DEFAULTS.items():
            setattr(self, k, kw.get(k, d))

    def __repr__(self):
        return "Config(%s)" % ", ".join(
            "%s=%r" % (k, getattr(self, k)) for k in DEFAULTS)


def _load_file(path):
    """Return a dict from a JSON config file, or {} if missing/unreadable.
    Never raises -- a broken config must not stop the safety daemon."""
    try:
        with open(path) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        return {}


def load_config(args=None, config_path=None, environ=None):
    """Build a Config from defaults <- file <- env <- CLI args.

    args: an argparse.Namespace (or None) holding any of the keys with a
          non-None value to override. config_path: explicit file path (else
          RUBY_UPS_CONFIG or DEFAULT_CONFIG_PATH). environ: dict for tests.
    """
    environ = os.environ if environ is None else environ
    vals = dict(DEFAULTS)

    # 1. file
    path = config_path or environ.get("RUBY_UPS_CONFIG") or DEFAULT_CONFIG_PATH
    file_vals = _load_file(path)
    for k in DEFAULTS:
        if k in file_vals:
            vals[k] = file_vals[k]

    # 2. env
    for k, envname in _ENV.items():
        if envname in environ and environ[envname] != "":
            vals[k] = environ[envname]

    # 3. CLI args (only keys explicitly set, i.e. not None)
    if args is not None:
        for k in DEFAULTS:
            v = getattr(args, k, None)
            if v is not None:
                vals[k] = v

    # --- coerce types defensively --- #
    for k in _BOOL_KEYS:
        vals[k] = _as_bool(vals[k], DEFAULTS[k])
    for k in _FLOAT_KEYS:
        vals[k] = _as_float(vals[k], DEFAULTS[k], minimum=0.0)
    for k in _STR_KEYS:
        vals[k] = str(vals[k]) if vals[k] else DEFAULTS[k]

    # poll must be > 0 to avoid a busy loop.
    if vals["poll_s"] <= 0:
        vals["poll_s"] = DEFAULTS["poll_s"]

    return Config(**vals)
