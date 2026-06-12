"""TcpStateServer -- threaded broadcast TCP server for the rubysat STATE channel.

Ruby is the SERVER (0.0.0.0:7878). The Qualia satellite(s) are CLIENTS that
auto-reconnect. The server:

  * accepts multiple simultaneous clients and broadcast()s each STATE line to
    all of them, pruning sockets that error on write;
  * runs one reader thread per client that parses inbound newline-delimited CMD
    JSON and pushes parsed dicts onto a thread-safe queue (drain via commands());
  * NEVER raises out of the accept loop, a reader thread, or broadcast() -- a
    misbehaving client must not take the publish loop down;
  * logs (throttled) to /tmp/rubysat.log.

The publish loop calls broadcast() from the main thread; accept + per-client
readers run on daemon threads. All shared state (the client set) is guarded by
a lock. broadcast() uses sendall under a per-client send lock and a short
timeout so one wedged client cannot block the publish loop indefinitely.
"""

from __future__ import annotations

import json
import queue
import socket
import threading
import time

_LOG = "/tmp/rubysat.log"

# A single STATE/heartbeat line is well under 1 KiB; cap inbound CMD buffering
# so a chatty or malicious client cannot grow our memory without bound.
_MAX_CMD_BUF = 64 * 1024
# Bound the command queue so a flood of taps that nobody drains cannot grow
# without limit; oldest commands are dropped when full.
_CMD_QUEUE_MAX = 256
# Per-client send timeout: a Qualia on Wi-Fi should drain a sub-KiB line almost
# instantly; if it stalls past this we treat the socket as dead and prune it
# rather than letting it wedge the broadcast.
#
# broadcast() runs on the single publish thread and calls sock.sendall() per
# client, so a wedged reader (full TCP send window) blocks ALL clients for up to
# this long. Keep it small: 0.2s is ample for a ~200-byte line on a LAN, and a
# Qualia that can't drain that in 200ms is dead -- prune it. At 15 Hz this bounds
# the worst-case stall to ~3 missed frames instead of ~15.
_SEND_TIMEOUT_S = 0.2


class _Client:
    """One connected satellite: its socket, a send lock, and a recv buffer."""

    __slots__ = ("sock", "addr", "send_lock", "buf")

    def __init__(self, sock: socket.socket, addr):
        self.sock = sock
        self.addr = addr
        self.send_lock = threading.Lock()
        self.buf = b""


class TcpStateServer:
    def __init__(self, host: str = "0.0.0.0", port: int = 7878):
        self.host = host
        self.port = port
        self._clients: set[_Client] = set()
        self._lock = threading.Lock()
        self._cmd_q: "queue.Queue[dict]" = queue.Queue(maxsize=_CMD_QUEUE_MAX)
        self._srv: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._last_log = 0.0

    # -- logging (throttled; never raises) -------------------------------- #
    def _log(self, msg: str) -> None:
        now = time.monotonic()
        if now - self._last_log < 1.0:
            return
        self._last_log = now
        try:
            with open(_LOG, "a") as fh:
                fh.write("%s %s\n" % (time.strftime("%H:%M:%S"), msg))
        except Exception:
            pass

    # -- lifecycle -------------------------------------------------------- #
    def start(self) -> None:
        if self._accept_thread is not None:
            return
        self._stop.clear()
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((self.host, self.port))
        srv.listen(8)
        # Wake the accept loop periodically so stop() is responsive.
        srv.settimeout(0.5)
        self._srv = srv
        self._accept_thread = threading.Thread(
            target=self._accept_loop, name="rubysat-accept", daemon=True
        )
        self._accept_thread.start()

    def stop(self) -> None:
        self._stop.set()
        t = self._accept_thread
        if t is not None:
            t.join(timeout=3.0)
        self._accept_thread = None
        srv = self._srv
        self._srv = None
        if srv is not None:
            try:
                srv.close()
            except Exception:
                pass
        with self._lock:
            clients = list(self._clients)
            self._clients.clear()
        for c in clients:
            try:
                c.sock.close()
            except Exception:
                pass

    # -- accept loop (own thread) ----------------------------------------- #
    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            srv = self._srv
            if srv is None:
                break
            try:
                sock, addr = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                # Server socket closed under us (stop) or transient accept
                # error; loop will re-check _stop and exit if needed.
                if self._stop.is_set():
                    break
                self._log("accept error; continuing")
                time.sleep(0.05)
                continue
            except Exception as exc:
                self._log("accept unexpected: %s" % exc)
                time.sleep(0.05)
                continue

            try:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                sock.settimeout(_SEND_TIMEOUT_S)
            except Exception:
                pass
            client = _Client(sock, addr)
            with self._lock:
                self._clients.add(client)
            self._log("client connected: %s" % (addr,))
            reader = threading.Thread(
                target=self._reader_loop, args=(client,),
                name="rubysat-reader", daemon=True,
            )
            reader.start()

    # -- per-client reader (own thread) ----------------------------------- #
    def _reader_loop(self, client: _Client) -> None:
        sock = client.sock
        try:
            while not self._stop.is_set():
                try:
                    chunk = sock.recv(4096)
                except socket.timeout:
                    # No inbound CMD data within the timeout: that's fine, the
                    # client may simply not be touching the screen. Keep waiting.
                    continue
                except OSError:
                    break
                except Exception as exc:
                    self._log("recv error %s: %s" % (client.addr, exc))
                    break
                if not chunk:
                    break  # clean EOF
                client.buf += chunk
                if len(client.buf) > _MAX_CMD_BUF:
                    # Runaway / non-newline-terminated junk: keep only the tail
                    # so we stay bounded but can still recover on the next NL.
                    client.buf = client.buf[-_MAX_CMD_BUF:]
                self._consume_lines(client)
        finally:
            self._drop(client)

    def _consume_lines(self, client: _Client) -> None:
        """Split the recv buffer on newlines, parse each complete CMD line, and
        enqueue valid dicts. Tolerates partial trailing lines (kept in buf)."""
        while True:
            nl = client.buf.find(b"\n")
            if nl < 0:
                break
            raw = client.buf[:nl]
            client.buf = client.buf[nl + 1:]
            line = raw.strip()
            if not line:
                continue
            try:
                obj = json.loads(line.decode("utf-8", "replace"))
            except Exception:
                self._log("bad CMD json from %s" % (client.addr,))
                continue
            if not isinstance(obj, dict):
                continue
            try:
                self._cmd_q.put_nowait(obj)
            except queue.Full:
                # Drop the oldest to make room: a stale tap matters less than a
                # fresh one, and we must never block the reader thread.
                try:
                    self._cmd_q.get_nowait()
                    self._cmd_q.put_nowait(obj)
                except Exception:
                    pass

    def _drop(self, client: _Client) -> None:
        with self._lock:
            self._clients.discard(client)
        try:
            client.sock.close()
        except Exception:
            pass
        self._log("client gone: %s" % (client.addr,))

    # -- broadcast (called from publish loop) ----------------------------- #
    def broadcast(self, line: str) -> int:
        """Send `line` (a single JSON record, newline appended if absent) to all
        connected clients. Prunes any that error. Returns the number of clients
        the line was delivered to. NEVER raises."""
        if not line.endswith("\n"):
            line = line + "\n"
        try:
            data = line.encode("utf-8")
        except Exception:
            # Should never happen (ASCII JSON), but a publish loop must not die.
            self._log("broadcast encode failed")
            return 0
        with self._lock:
            clients = list(self._clients)
        delivered = 0
        dead: list[_Client] = []
        for c in clients:
            try:
                with c.send_lock:
                    c.sock.sendall(data)
                delivered += 1
            except Exception:
                dead.append(c)
        for c in dead:
            self._log("pruning dead client %s" % (c.addr,))
            self._drop(c)
        return delivered

    def client_count(self) -> int:
        with self._lock:
            return len(self._clients)

    # -- inbound commands ------------------------------------------------- #
    def commands(self) -> list[dict]:
        """Drain and return all queued inbound CMD dicts (possibly empty)."""
        out: list[dict] = []
        while True:
            try:
                out.append(self._cmd_q.get_nowait())
            except queue.Empty:
                break
        return out
