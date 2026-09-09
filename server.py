"""Perch server: stdlib HTTP + SSE + a minimal RFC 6455 WebSocket bridge.

Routes:
  GET  /                     static app
  GET  /api/snapshot         current scanner snapshot (+ live terminals)
  GET  /api/events           SSE stream of snapshot changes
  POST /api/spawn            {harness, cwd, session?} -> {term}
  POST /api/kill             {id} -> {}
  GET  /ws/term/<id>         WebSocket <-> PTY pipe (binary out, text in)
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import queue
import struct
import subprocess
import threading
import time
import secrets
import socket
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, urlparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

STATIC_DIR = Path(__file__).parent / "static"
MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
}
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


# --------------------------------------------------------------------------
# websocket primitives


def _ws_accept(key: str) -> str:
    digest = hashlib.sha1((key + WS_GUID).encode()).digest()
    return base64.b64encode(digest).decode()


def _ws_send(sock, payload: bytes, opcode: int = 2) -> None:
    header = bytearray([0x80 | opcode])
    n = len(payload)
    if n < 126:
        header.append(n)
    elif n < 65536:
        header += struct.pack("!BH", 126, n)
    else:
        header += struct.pack("!BQ", 127, n)
    sock.sendall(bytes(header) + payload)


def _ws_recv(rfile, on_ping=None):
    """Read one complete message -> (kind, payload). kind: msg|ping|close."""
    fragments = bytearray()
    started = False
    while True:
        head = rfile.read(2)
        if len(head) < 2:
            return ("close", None)
        fin = head[0] & 0x80
        opcode = head[0] & 0x0F
        if head[0] & 0x70 or opcode not in (0, 1, 2, 8, 9, 10):
            return ("close", None)
        masked = head[1] & 0x80
        length = head[1] & 0x7F
        if length == 126:
            extended = rfile.read(2)
            if len(extended) != 2:
                return ("close", None)
            length = struct.unpack("!H", extended)[0]
        elif length == 127:
            extended = rfile.read(8)
            if len(extended) != 8:
                return ("close", None)
            length = struct.unpack("!Q", extended)[0]
        if opcode >= 8 and (not fin or length > 125):
            return ("close", None)
        if not masked or length > 1024 * 1024 or len(fragments) + length > 1024 * 1024:
            return ("close", None)
        mask = rfile.read(4)
        if len(mask) != 4:
            return ("close", None)
        data = rfile.read(length) if length else b""
        if len(data) < length:
            return ("close", None)
        if masked:
            data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        if opcode == 8:
            return ("close", None)
        if opcode == 9:
            if on_ping:
                on_ping(data)
                continue
            if not started:
                return ("ping", data)
            continue
        if opcode == 10:
            continue
        if (opcode == 0 and not started) or (opcode in (1, 2) and started):
            return ("close", None)
        started = True
        fragments += data
        if fin:
            return ("msg", bytes(fragments))


# --------------------------------------------------------------------------
# sse hub


class EventHub:
    def __init__(self):
        self._clients: list[queue.Queue] = []
        self._lock = threading.Lock()

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=8)
        with self._lock:
            self._clients.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._clients:
                self._clients.remove(q)

    def publish(self, payload: str) -> None:
        with self._lock:
            clients = list(self._clients)
        for q in clients:
            try:
                q.put_nowait(payload)
            except queue.Full:
                try:
                    q.get_nowait()
                    q.put_nowait(payload)
                except (queue.Empty, queue.Full):
                    pass


# --------------------------------------------------------------------------
# replies in flight


class Pending:
    """Messages delivered to agents, shown dimmed until the session file moves."""

    def __init__(self):
        self._items: dict[str, list[dict]] = {}
        self._lock = threading.Lock()

    def add(self, agent: str, text: str) -> dict:
        entry = {"text": text, "ts": time.time(), "error": None}
        with self._lock:
            self._items.setdefault(agent, []).append(entry)
        return entry

    def fail(self, agent: str, entry: dict, error: str) -> None:
        with self._lock:
            entry["error"] = error

    def snapshot(self, agents: list[dict]) -> dict:
        now = time.time()
        with self._lock:
            for agent in list(self._items):
                keep = []
                for e in self._items[agent]:
                    landed = e.get("completed", False)
                    if e["error"]:
                        if now - e["ts"] < 90:
                            keep.append(e)
                    elif not landed and now - e["ts"] < 900:
                        keep.append(e)
                if keep:
                    self._items[agent] = keep
                else:
                    del self._items[agent]
            return {a: list(es) for a, es in self._items.items()}


# --------------------------------------------------------------------------
# http handler


class Handler(BaseHTTPRequestHandler):
    server_version = "Perch/1.0"
    protocol_version = "HTTP/1.1"
    hub: EventHub
    get_snapshot = None  # callable -> str
    scanner = None
    terms = None  # TermRegistry
    pending = None  # Pending

    def log_message(self, *args):
        pass

    def _send(self, code: int, body: bytes, content_type: str):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'" + (" 'unsafe-eval'" if getattr(self.server, "native_bridge", False) else "") + "; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self' ws://127.0.0.1:*; frame-ancestors 'none'",
        )
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj):
        self._send(code, json.dumps(obj).encode(), "application/json")

    def _authorized(self, required=True):
        expected = f"127.0.0.1:{self.server.server_port}"
        if self.headers.get("Host") != expected:
            self._json(403, {"error": "Invalid host"})
            return False
        origin = self.headers.get("Origin")
        if origin and origin != "http://" + expected:
            self._json(403, {"error": "Invalid origin"})
            return False
        if not required:
            return True
        cookies = SimpleCookie()
        try:
            cookies.load(self.headers.get("Cookie", ""))
        except Exception:
            pass
        token = self.headers.get("X-Perch-Token", "")
        cookie_name = f"perch_session_{self.server.server_port}"
        if not token and cookie_name in cookies:
            token = cookies[cookie_name].value
        if not token.isascii() or not secrets.compare_digest(token, self.server.token):
            self._json(401, {"error": "Open Perch again to reconnect securely"})
            return False
        return True

    # -- GET --

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if not self._authorized(required=path.startswith(("/api/", "/ws/"))):
            return
        query = parse_qs(urlparse(self.path).query)
        token = (query.get("token") or [""])[0]
        if path == "/" and token and token.isascii() and secrets.compare_digest(token, self.server.token):
            self.send_response(303)
            self.send_header("Set-Cookie", f"perch_session_{self.server.server_port}={token}; HttpOnly; SameSite=Strict; Path=/")
            self.send_header("Location", "/")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        path = self.path.split("?", 1)[0]
        if path == "/api/snapshot":
            self._send(200, self.get_snapshot().encode(), "application/json")
        elif path == "/api/events":
            self._events()
        elif path == "/api/history":
            self._history()
        elif path == "/api/tools":
            if self.server.demo:
                import demo

                self._json(200, demo.tools())
                return
            import sync

            try:
                self._json(200, sync.overview())
            except (OSError, ValueError) as exc:
                self._json(400, {"error": str(exc)})
        elif path == "/api/settings":
            self._settings()
        elif path.startswith("/ws/term/"):
            self._term_ws(path.rsplit("/", 1)[1])
        else:
            self._static(path)

    def _history(self):
        from urllib.parse import parse_qs, urlparse

        query = parse_qs(urlparse(self.path).query)
        agent_id = (query.get("agent") or [""])[0]
        try:
            pidx = int((query.get("prompt") or ["0"])[0])
        except ValueError:
            self._json(400, {"error": "bad prompt index"})
            return
        snap = json.loads(self.get_snapshot())
        agent = next((a for a in snap.get("agents", []) if a["id"] == agent_id), None)
        adapter = agent and next((a for a in self.scanner.adapters if a.id == agent["harness"]), None)
        if not agent or not adapter:
            self._json(404, {"error": "agent not found"})
            return
        try:
            self._json(200, self.scanner.history(agent_id, pidx))
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
        except OSError as exc:
            self._json(500, {"error": str(exc)})

    def _settings(self):
        if self.server.demo:
            import demo

            self._json(200, demo.config(self.scanner))
            return
        import settings as settings_mod
        import sync as sync_mod

        harnesses = [
            {
                "id": a.id,
                "name": a.name,
                "color": a.color,
                "installed": bool(a.cmd),
                "enabled": a.enabled,
                "patterns": a.patterns,
                "canMessage": bool(a.cmd and a.message),
            }
            for a in self.scanner.adapters
        ]
        last_sync = None
        try:
            with open(settings_mod.LAST_SYNC, "r", encoding="utf-8") as fh:
                last_sync = json.load(fh)
        except (OSError, json.JSONDecodeError):
            pass
        sources = os.path.join(self.scanner.config_dir or "", "sources.json")
        self._json(
            200,
            {
                "settings": settings_mod.load(),
                "harnesses": harnesses,
                "mcp": sync_mod.mcp_overview(),
                "lastSync": last_sync,
                "files": {"settings": settings_mod.PATH, "sources": sources},
            },
        )

    def _static(self, path: str):
        rel = "index.html" if path == "/" else path.lstrip("/")
        target = (STATIC_DIR / rel).resolve()
        if not target.is_relative_to(STATIC_DIR.resolve()) or not target.is_file():
            self._send(404, b"not found", "text/plain")
            return
        self._send(200, target.read_bytes(), MIME.get(target.suffix, "application/octet-stream"))

    def _events(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        q = self.hub.subscribe()
        try:
            self.wfile.write(f"data: {self.get_snapshot()}\n\n".encode())
            self.wfile.flush()
            while not self.server.stop_event.is_set():
                try:
                    payload = q.get(timeout=2)
                    self.wfile.write(f"data: {payload}\n\n".encode())
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        finally:
            self.hub.unsubscribe(q)

    # -- POST --

    def do_POST(self):
        if not self._authorized():
            self.close_connection = True
            return
        if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            self._json(415, {"error": "Expected application/json"})
            self.close_connection = True
            return
        path = self.path.split("?", 1)[0]
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 <= length <= 1024 * 1024:
                raise ValueError("Request too large")
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("Expected an object")
            for key in ("agent", "text", "harness", "cwd", "session", "id", "revision"):
                if key in body and not isinstance(body[key], str):
                    raise ValueError(f"{key} must be text")
        except (ValueError, json.JSONDecodeError):
            self._json(400, {"error": "bad json"})
            return

        if self.server.demo and path != "/api/activate":
            self._json(403, {"error": "Demo mode is read-only"})
            return
        if path == "/api/activate":
            if self.server.activate:
                self.server.activate()
            self._json(200, {"ok": True})
        elif path == "/api/spawn":
            self._spawn(body)
        elif path == "/api/kill":
            self._kill(body)
        elif path == "/api/message":
            self._message(body)
        elif path == "/api/sync":
            self._sync(body)
        elif path == "/api/tools":
            import sync

            try:
                self._json(200, sync.overview())
            except (OSError, ValueError) as exc:
                self._json(400, {"error": str(exc)})
        elif path == "/api/settings":
            self._save_settings(body)
        else:
            self._json(404, {"error": "unknown route"})

    def _save_settings(self, body: dict):
        import settings as settings_mod

        try:
            cfg = settings_mod.patch(settings_mod.load(), body)
        except (ValueError, OSError) as exc:
            self._json(400, {"error": str(exc)})
            return
        self.scanner.apply_settings(cfg)
        self._json(200, {"ok": True, "settings": cfg})

    def _message(self, body: dict):
        agent_id = (body.get("agent") or "").strip()
        text = (body.get("text") or "").strip()
        if not agent_id or not text:
            self._json(400, {"error": "agent and text required"})
            return
        harness_id, _, session = agent_id.partition(":")
        adapter = next((a for a in self.scanner.adapters if a.id == harness_id), None)
        argv = adapter.message_argv(session, text) if adapter else None
        if not argv:
            self._json(400, {"error": "this harness can't receive messages"})
            return
        snap = json.loads(self.get_snapshot())
        agent = next((a for a in snap.get("agents", []) if a["id"] == agent_id), None)
        cwd = (agent or {}).get("cwd") or os.path.expanduser("~")
        if not agent or not adapter.enabled:
            self._json(404, {"error": "Session is not available"})
            return
        with self.server.delivery_lock:
            if self.server.stop_event.is_set():
                self._json(503, {"error": "Perch is shutting down"})
                return
            if agent_id in self.server.delivering:
                self._json(409, {"error": "A reply is already running for this session"})
                return
            if any(t["harness"] == harness_id and t.get("session") == session and t["alive"] for t in self.terms.list()):
                self._json(409, {"error": "This session is open in a terminal. Send the message there."})
                return
            self.server.delivering.add(agent_id)
        entry = self.pending.add(agent_id, text)
        threading.Thread(target=self._deliver, args=(agent_id, entry, argv, cwd), daemon=True).start()
        self._json(200, {"ok": True})

    def _deliver(self, agent_id: str, entry: dict, argv: list[str], cwd: str):
        proc = None
        try:
            from term import external_process_env

            with self.server.delivery_lock:
                if self.server.stop_event.is_set():
                    raise OSError("Perch is shutting down")
                with external_process_env() as env:
                    proc = subprocess.Popen(
                        argv, cwd=cwd, env=env,
                        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                        start_new_session=os.name != "nt",
                        creationflags=0x08000000 if os.name == "nt" else 0,
                    )
                self.server.deliveries.add(proc)
            _, err = proc.communicate(timeout=900)
            if proc.returncode:
                detail = err.decode("utf-8", "replace")[-400:].strip()
                self.pending.fail(agent_id, entry, detail or f"exit code {proc.returncode}")
            else:
                entry["completed"] = True
        except (OSError, subprocess.TimeoutExpired) as exc:
            if proc:
                from term import terminate_process

                terminate_process(proc)
            self.pending.fail(agent_id, entry, str(exc)[:300])
        finally:
            with self.server.delivery_lock:
                self.server.delivering.discard(agent_id)
                if proc:
                    self.server.deliveries.discard(proc)

    def _sync(self, body):
        try:
            import settings as settings_mod
            import sync as sync_mod

            sharing = settings_mod.load()["sharing"]
            dry_run = body.get("preview", True) is not False
            if not dry_run and not body.get("revision"):
                raise ValueError("Preview sharing changes before applying")
            self._json(
                200,
                sync_mod.sync_all(
                    skills=sharing["skills"],
                    mcp=sharing["mcp"],
                    targets=tuple(sharing["targets"]),
                    dry_run=dry_run,
                    revision=body.get("revision"),
                ),
            )
        except (OSError, ValueError) as exc:
            self._json(400, {"error": str(exc)})

    def _spawn(self, body: dict):
        adapter = next((a for a in self.scanner.adapters if a.id == body.get("harness")), None)
        if not adapter or not adapter.enabled:
            self._json(400, {"error": "unknown harness"})
            return
        session = (body.get("session") or "").strip() or None
        argv = adapter.spawn_argv(session)
        if not argv:
            self._json(400, {"error": f"{adapter.name} is not installed or not spawnable"})
            return
        cwd = os.path.expanduser((body.get("cwd") or "").strip() or "~")
        if not os.path.isdir(cwd):
            self._json(400, {"error": f"no such folder: {cwd}"})
            return
        try:
            with self.server.delivery_lock:
                if session and f"{adapter.id}:{session}" in self.server.delivering:
                    self._json(409, {"error": "A reply is running for this session. Wait for it to finish."})
                    return
                term = self.terms.spawn(adapter.id, adapter.name, adapter.color, cwd, argv, session=session)
            self._json(200, {"term": term.info(), "snapshot": json.loads(self.get_snapshot())})
        except (OSError, ImportError, ValueError) as exc:
            self._json(400, {"error": f"Could not start {adapter.name}: {exc}"})

    def _kill(self, body: dict):
        self._json(200, {"removed": self.terms.remove(body.get("id") or "")})

    # -- websocket bridge --

    def _term_ws(self, term_id: str):
        term = self.terms.get(term_id)
        key = self.headers.get("Sec-WebSocket-Key")
        if not term or not key:
            self._send(404, b"no such terminal", "text/plain")
            return
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", _ws_accept(key))
        self.end_headers()
        self.close_connection = True

        sock = self.connection
        q = term.subscribe()
        send_lock = threading.Lock()
        stop = threading.Event()

        def safe_send(payload: bytes, opcode: int = 2) -> bool:
            with send_lock:
                try:
                    _ws_send(sock, payload, opcode)
                    return True
                except OSError:
                    return False

        def writer():
            try:
                while not stop.is_set():
                    try:
                        chunk = q.get(timeout=0.5)
                    except queue.Empty:
                        continue
                    if chunk is None:  # PTY EOF
                        safe_send(json.dumps({"type": "reconnect" if term.alive() else "exit"}).encode(), opcode=1)
                        return
                    if not safe_send(chunk, opcode=2):
                        return
            finally:
                stop.set()
                try:
                    sock.shutdown(socket.SHUT_RD)
                except OSError:
                    pass

        threading.Thread(target=writer, daemon=True).start()
        try:
            while not stop.is_set():
                kind, data = _ws_recv(self.rfile, on_ping=lambda payload: safe_send(payload, opcode=0xA))
                if kind == "close":
                    break
                if kind == "ping":
                    safe_send(data, opcode=0xA)
                    continue
                if data[:1] == b"{":
                    try:
                        ctl = json.loads(data)
                        if ctl.get("type") == "resize":
                            term.resize(int(ctl["cols"]), int(ctl["rows"]))
                            continue
                        if ctl.get("type") == "input" and isinstance(ctl.get("data"), str):
                            term.write(ctl["data"].encode())
                            continue
                    except (ValueError, KeyError, TypeError):
                        pass
                term.write(data)
        except (OSError, ValueError, struct.error):
            pass
        finally:
            stop.set()
            term.unsubscribe(q)


# --------------------------------------------------------------------------
# wiring


def scan_loop(scanner, publish, stop, interval=2.0):
    while not stop.is_set():
        try:
            snap = scanner.scan()
        except Exception as exc:
            snap = json.loads(publish())
            snap["errors"] = [{"message": f"Scanner unavailable: {type(exc).__name__}"}]
        publish(snap)
        # File events refresh quickly. Status ages still reconcile every two seconds.
        deadline = time.monotonic() + interval
        while not stop.is_set() and time.monotonic() < deadline:
            if scanner.changed.wait(0.2):
                stop.wait(0.15)
                break


class PerchServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def close(self):
        self.stop_event.set()
        self.shutdown()
        self.server_close()
        self.scanner.close()
        self.terms.shutdown()
        from term import terminate_process

        with self.delivery_lock:
            procs = list(self.deliveries)
        for proc in procs:
            terminate_process(proc)
        self.scan_thread.join(timeout=5)


def serve(scanner, terms, port=0, demo=False):
    hub = EventHub()
    pending = Pending()
    # First scan is asynchronous, so large directories do not block the first window.
    state = {
        "snapshot": json.dumps(
            {
                "agents": [],
                "harnesses": [],
                "watching": [],
                "terms": [],
                "pending": {},
                "errors": [],
                "loading": True,
            }
        )
    }
    snapshot_lock = threading.Lock()
    base = json.loads(state["snapshot"])
    revision, previous = 0, None
    instance = secrets.token_hex(8)

    def publish(snapshot=None):
        nonlocal base, revision, previous
        with snapshot_lock:
            if snapshot is not None:
                base = {k: v for k, v in snapshot.items() if k not in ("revision", "instance", "terms", "pending")}
            current = {**base, "terms": terms.list(), "pending": pending.snapshot(base["agents"])}
            semantic = json.dumps(current)
            if semantic != previous:
                previous = semantic
                revision += 1
                state["snapshot"] = json.dumps({**current, "revision": revision, "instance": instance})
                hub.publish(state["snapshot"])
            return state["snapshot"]

    publish()
    handler = type(
        "BoundHandler",
        (Handler,),
        {
            "hub": hub,
            "get_snapshot": staticmethod(publish),
            "scanner": scanner,
            "terms": terms,
            "pending": pending,
        },
    )
    httpd = PerchServer(("127.0.0.1", port), handler)
    httpd.token = secrets.token_urlsafe(32)
    httpd.stop_event = threading.Event()
    httpd.terms, httpd.scanner = terms, scanner
    httpd.demo, httpd.activate = demo, None
    httpd.deliveries, httpd.delivering = set(), set()
    httpd.delivery_lock = threading.Lock()
    httpd.scan_thread = threading.Thread(
        target=scan_loop, args=(scanner, publish, httpd.stop_event), daemon=True
    )
    httpd.scan_thread.start()
    return httpd
