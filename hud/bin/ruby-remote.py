#!/usr/bin/env python3
"""Remote rubysat control over the network.

The Pi runs rubysat as a TCP server on 0.0.0.0:7878 (see qualia/README.md).
Connect from any host on the LAN (or Tailscale) and send allowlisted control
verbs; the server replies with a transient "ack" key on STATE lines for ~3s.

Usage:
  ruby-remote.py check              queue a tag check (ruby_check)
  ruby-remote.py update             apply latest tag (ruby_update)
  ruby-remote.py rollback           roll back (ruby_rollback)
  ruby-remote.py restart-hud        restart HUD (ruby_restart_hud)
  ruby-remote.py switch-dash        console dash (ruby_switch_dash)
  ruby-remote.py listen [N]         print N STATE lines (default 5), no cmd

Env:
  RUBY_HOST   host to connect to (default: ruby.local, then 192.168.2.180)
  RUBY_PORT   TCP port (default 7878)
"""
from __future__ import annotations

import json
import os
import socket
import sys
import time

VERBS = {
    "check": "ruby_check",
    "update": "ruby_update",
    "rollback": "ruby_rollback",
    "restart-hud": "ruby_restart_hud",
    "switch-dash": "ruby_switch_dash",
}

DEFAULT_HOSTS = ("ruby.local", "192.168.2.180")
DEFAULT_PORT = 7878
CONNECT_S = 8.0
READ_S = 12.0


def _hosts() -> list[str]:
    env = os.environ.get("RUBY_HOST", "").strip()
    return [env] if env else list(DEFAULT_HOSTS)


def _port() -> int:
    try:
        return int(os.environ.get("RUBY_PORT", DEFAULT_PORT))
    except ValueError:
        return DEFAULT_PORT


def _connect(hosts: list[str], port: int) -> tuple[socket.socket, str]:
    last_err = None
    for host in hosts:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(CONNECT_S)
        try:
            s.connect((host, port))
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            return s, host
        except OSError as exc:
            last_err = exc
            try:
                s.close()
            except OSError:
                pass
    raise SystemExit("connect failed (%s:%d): %s"
                     % (hosts[-1], port, last_err))


def _iter_state(sock: socket.socket, deadline: float):
    buf = b""
    while time.time() < deadline:
        wait = max(0.0, min(1.0, deadline - time.time()))
        sock.settimeout(wait)
        try:
            chunk = sock.recv(8192)
        except socket.timeout:
            continue
        except OSError as exc:
            print("recv error: %s" % exc, file=sys.stderr)
            break
        if not chunk:
            print("connection closed by peer", file=sys.stderr)
            break
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            if not line.strip():
                continue
            try:
                yield json.loads(line.decode("utf-8", "replace"))
            except json.JSONDecodeError:
                print("bad json: %r" % line[:120], file=sys.stderr)


def _summarize(state: dict) -> str:
    bits = []
    for key in ("seq", "rpm", "mph", "gear", "bus", "canfps", "vsrc", "ack"):
        if key in state:
            bits.append("%s=%s" % (key, state[key]))
    return " ".join(bits) if bits else json.dumps(state, separators=(",", ":"))


def cmd_listen(count: int) -> int:
    sock, host = _connect(_hosts(), _port())
    print("connected to %s:%d" % (host, _port()))
    seen = 0
    for state in _iter_state(sock, time.time() + READ_S):
        print(_summarize(state))
        seen += 1
        if seen >= count:
            break
    sock.close()
    if seen == 0:
        print("no STATE lines received", file=sys.stderr)
        return 1
    return 0


def cmd_verb(verb: str) -> int:
    ruby_verb = VERBS[verb]
    sock, host = _connect(_hosts(), _port())
    print("connected to %s:%d" % (host, _port()))
    payload = json.dumps({"cmd": ruby_verb}, separators=(",", ":")) + "\n"
    try:
        sock.sendall(payload.encode("ascii"))
    except OSError as exc:
        print("send failed: %s" % exc, file=sys.stderr)
        return 1
    print("sent %s" % ruby_verb)
    ack = None
    for state in _iter_state(sock, time.time() + READ_S):
        if "ack" in state:
            ack = str(state["ack"])
            print("ack: %s" % ack)
            break
        # Keep the link warm while waiting for the handler to queue the verb.
    sock.close()
    if ack is None:
        print("no ack received (updater queue may still have been triggered)",
              file=sys.stderr)
        return 1
    if ack.endswith(":failed"):
        return 1
    return 0


def usage() -> None:
    print(__doc__.strip())


def main(argv: list[str] | None = None) -> int:
    args = list(argv or sys.argv[1:])
    if not args or args[0] in ("-h", "--help", "help"):
        usage()
        return 0
    cmd = args[0]
    if cmd == "listen":
        count = 5
        if len(args) > 1:
            try:
                count = max(1, int(args[1]))
            except ValueError:
                print("listen count must be an integer", file=sys.stderr)
                return 2
        return cmd_listen(count)
    if cmd not in VERBS:
        usage()
        print("\nunknown command: %s" % cmd, file=sys.stderr)
        return 2
    return cmd_verb(cmd)


if __name__ == "__main__":
    raise SystemExit(main())
