"""Unprivileged client for the ruby-updated root handler.

The HUD never touches git or systemd itself: it drops one-line JSON request
files into /run/ruby-update/queue (a systemd .path unit wakes the root
handler, which consumes them) and reads back /run/ruby-update/status.json
(atomic snapshot of the current/last operation) plus status.jsonl (append-
only phase log).

Every function here is failure-guarded and NEVER raises: on a host without
the updater installed (no /run/ruby-update, missing files, no permissions)
request() returns False and the readers return None / [] so the Settings
page renders "offline" instead of crashing the render loop. Reads are
cached briefly (status 0.5s, version/state 5s) so per-frame calls stay
cheap.

Env overrides for build-host testing (read per call, not at import):
  RUBYHUD_UPDATE_DIR  -> /run/ruby-update   (status.json/.jsonl + queue/)
  RUBYHUD_STATE_DIR   -> /home/michael/hud-state
"""

from __future__ import annotations

import json
import os
import time

_UPDATE_DIR = "/run/ruby-update"
_STATE_DIR = "/home/michael/hud-state"
_VERSION_FILE = "/home/michael/hud/VERSION"

# Fallback VERSION next to the package (repo checkout on the build host).
_LOCAL_VERSION = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "VERSION")


def _update_dir() -> str:
    return os.environ.get("RUBYHUD_UPDATE_DIR", _UPDATE_DIR)


def _state_dir() -> str:
    return os.environ.get("RUBYHUD_STATE_DIR", _STATE_DIR)


# --------------------------------------------------------------------------- #
# tiny read cache (same shape as pages._cached; failures cache as None)
# --------------------------------------------------------------------------- #
_cache: dict = {}


def _cached(key, ttl, fn):
    now = time.monotonic()
    ent = _cache.get(key)
    if ent is not None and now - ent[0] < ttl:
        return ent[1]
    try:
        val = fn()
    except Exception:
        val = None
    _cache[key] = (now, val)
    return val


# --------------------------------------------------------------------------- #
# request queue (write side)
# --------------------------------------------------------------------------- #
def request(cmd: str, ref: str | None = None) -> bool:
    """Queue {"cmd": cmd[, "ref": ref]} for ruby-updated; atomic write.

    The temp name starts with '.' so the .path unit only fires once the
    finished file is renamed into place. Returns False on any failure
    failure (missing queue dir, PermissionError, full disk, ...)."""
    qdir = os.path.join(_update_dir(), "queue")
    try:
        payload = {"cmd": str(cmd)}
        if ref:
            payload["ref"] = str(ref)
        import tempfile
        fd, tmp = tempfile.mkstemp(prefix=".req-", dir=qdir)
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(json.dumps(payload) + "\n")
            dst = os.path.join(qdir, "%d-%d.req"
                               % (time.time_ns() // 1000000, os.getpid()))
            os.replace(tmp, dst)
            udir = _update_dir()
            _cache.pop(("status", udir), None)
            _cache.pop(("lines", udir), None)
        except Exception:
            try:
                os.unlink(tmp)
            except Exception:
                pass
            raise
        return True
    except Exception:
        return False


def queue_writable() -> bool:
    """True when the request queue exists and we may write to it."""
    qdir = os.path.join(_update_dir(), "queue")
    try:
        return os.path.isdir(qdir) and os.access(qdir, os.W_OK)
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# status (read side)
# --------------------------------------------------------------------------- #
def status() -> dict | None:
    """Last status.json snapshot as a dict, or None. Cached 0.5s."""
    udir = _update_dir()

    def read():
        with open(os.path.join(udir, "status.json")) as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else None
    return _cached(("status", udir), 0.5, read)


def status_lines(n: int = 8) -> list:
    """Tail of status.jsonl as a list of dicts (oldest first). Cached 0.5s."""
    udir = _update_dir()

    def read():
        path = os.path.join(udir, "status.jsonl")
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 8192))
            data = fh.read().decode("utf-8", "replace")
        out = []
        for line in data.splitlines()[-64:]:
            try:
                d = json.loads(line)
            except Exception:
                continue
            if isinstance(d, dict):
                out.append(d)
        return out
    lines = _cached(("lines", udir), 0.5, read)
    try:
        n = max(0, int(n))
    except Exception:
        n = 8
    return list(lines or [])[-n:]


def last_result() -> dict | None:
    """hud-state/last-update.json ({"ref","sha","ts","ok"}) or None."""
    sdir = _state_dir()

    def read():
        with open(os.path.join(sdir, "last-update.json")) as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else None
    return _cached(("last", sdir), 5.0, read)


def previous_version() -> str | None:
    """hud-state/previous (the rollback target tag) or None."""
    sdir = _state_dir()

    def read():
        with open(os.path.join(sdir, "previous")) as fh:
            val = fh.read().strip()
        return val or None
    return _cached(("previous", sdir), 5.0, read)


def current_version() -> str | None:
    """Deployed version as 'vX.Y.Z' (from /home/michael/hud/VERSION, repo
    VERSION as build-host fallback). ' (dev)' is appended when the last
    applied tag mismatches VERSION (hand-edited tree). Cached 5s."""
    sdir = _state_dir()

    def read():
        ver = None
        for path in (_VERSION_FILE, _LOCAL_VERSION):
            try:
                with open(path) as fh:
                    ver = fh.read().strip()
            except Exception:
                continue
            if ver:
                break
        if not ver:
            return None
        tag = ver if ver.startswith("v") else "v" + ver
        lr = last_result()
        ref = str((lr or {}).get("ref") or "")
        if ref and ref.lstrip("v") != ver.lstrip("v"):
            tag += " (dev)"
        return tag
    return _cached(("version", sdir), 5.0, read)
