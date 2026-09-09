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


def _ws_recv(rfile):
    """Read one complete message -> (kind, payload). kind: msg|ping|close."""
    fragments = bytearray()
    while True:
        head = rfile.read(2)
        if len(head) < 2:
            return ("close", None)
        fin = head[0] & 0x80
        opcode = head[0] & 0x0F
        masked = head[1] & 0x80
        length = head[1] & 0x7F
        if length == 126:
            length = struct.unpack("!H", rfile.read(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", rfile.read(8))[0]
        mask = rfile.read(4) if masked else b""
        data = rfile.read(length) if length else b""
        if len(data) < length:
            return ("close", None)
        if masked:
            data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        if opcode == 8:
            return ("close", None)
        if opcode == 9:
            return ("ping", data)
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
                pass  # slow client skips a beat; next snapshot resyncs


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
        entry["error"] = error

    def snapshot(self, agents: list[dict]) -> dict:
        now = time.time()
        mtime = {a["id"]: a["mtime"] for a in agents}
        with self._lock:
            for agent in list(self._items):
                keep = []
                for e in self._items[agent]:
                    landed = mtime.get(agent, 0) > e["ts"] + 2
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
    get_snapshot = None   # callable -> str
    scanner = None
    terms = None          # TermRegistry
    pending = None        # Pending

    def log_message(self, *args):
        pass

    def _send(self, code: int, body: bytes, content_type: str):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj):
        self._send(code, json.dumps(obj).encode(), "application/json")

    # -- GET --

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/snapshot":
            self._send(200, self.get_snapshot().encode(), "application/json")
        elif path == "/api/events":
            self._events()
        elif path == "/api/history":
            self._history()
        elif path == "/api/settings":
            self._settings()
        elif path.startswith("/ws/term/"):
            self._term_ws(path.rsplit("/", 1)[1])
        else:
            self._static(path)

    def _history(self):
        from urllib.parse import parse_qs, urlparse
        from scanner import read_history
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
            self._json(200, read_history(adapter, agent["file"], pidx))
        except OSError as exc:
            self._json(500, {"error": str(exc)})

    def _settings(self):
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
        self._json(200, {
            "settings": settings_mod.load(),
            "harnesses": harnesses,
            "mcp": sync_mod.mcp_overview(),
            "lastSync": last_sync,
            "files": {"settings": settings_mod.PATH, "sources": sources},
        })

    def _static(self, path: str):
        rel = "index.html" if path == "/" else path.lstrip("/")
        target = (STATIC_DIR / rel).resolve()
        if not str(target).startswith(str(STATIC_DIR.resolve())) or not target.is_file():
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
            self.wfile.write(b": hello\n\n")
            self.wfile.flush()
            while True:
                try:
                    payload = q.get(timeout=15)
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
        path = self.path.split("?", 1)[0]
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            self._json(400, {"error": "bad json"})
            return

        if path == "/api/spawn":
            self._spawn(body)
        elif path == "/api/kill":
            self._kill(body)
        elif path == "/api/message":
            self._message(body)
        elif path == "/api/sync":
            self._sync()
        elif path == "/api/settings":
            self._save_settings(body)
        else:
            self._json(404, {"error": "unknown route"})

    def _save_settings(self, body: dict):
        import settings as settings_mod
        cfg = settings_mod.patch(settings_mod.load(), body)
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
        entry = self.pending.add(agent_id, text)
        threading.Thread(target=self._deliver, args=(agent_id, entry, argv, cwd), daemon=True).start()
        self._json(200, {"ok": True})

    def _deliver(self, agent_id: str, entry: dict, argv: list[str], cwd: str):
        try:
            proc = subprocess.Popen(
                argv, cwd=cwd,
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                creationflags=0x08000000 if os.name == "nt" else 0,  # CREATE_NO_WINDOW
            )
            _, err = proc.communicate(timeout=900)
            if proc.returncode:
                detail = err.decode("utf-8", "replace")[-400:].strip()
                self.pending.fail(agent_id, entry, detail or f"exit code {proc.returncode}")
        except (OSError, subprocess.TimeoutExpired) as exc:
            self.pending.fail(agent_id, entry, str(exc)[:300])

    def _sync(self):
        try:
            import settings as settings_mod
            import sync as sync_mod
            sharing = settings_mod.load()["sharing"]
            self._json(200, sync_mod.sync_all(
                skills=sharing["skills"], mcp=sharing["mcp"], targets=tuple(sharing["targets"])))
        except Exception as exc:
            self._json(500, {"error": str(exc)})

    def _spawn(self, body: dict):
        adapter = next((a for a in self.scanner.adapters if a.id == body.get("harness")), None)
        if not adapter:
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
        term = self.terms.spawn(adapter.id, adapter.name, adapter.color, cwd, argv)
        self._json(200, {"term": term.info()})

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
                    chunk = q.get()
                    if chunk is None:  # PTY EOF
                        safe_send(json.dumps({"type": "exit"}).encode(), opcode=1)
                        return
                    if not safe_send(chunk, opcode=2):
                        return
            finally:
                stop.set()

        threading.Thread(target=writer, daemon=True).start()
        try:
            while not stop.is_set():
                kind, data = _ws_recv(self.rfile)
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
                    except (ValueError, KeyError, TypeError):
                        pass
                term.write(data)
        except OSError:
            pass
        finally:
            stop.set()
            term.unsubscribe(q)


# --------------------------------------------------------------------------
# wiring

def scan_loop(scanner, terms, pending: Pending, hub: EventHub, state: dict, interval: float = 2.0):
    last = ""
    while True:
        try:
            snap = scanner.scan()
            snap["terms"] = terms.list()
            snap["pending"] = pending.snapshot(snap["agents"])
            payload = json.dumps(snap)
        except Exception:
            time.sleep(interval)
            continue
        state["snapshot"] = payload
        if payload != last:
            last = payload
            hub.publish(payload)
        time.sleep(interval)


def serve(scanner, terms, port: int):
    hub = EventHub()
    pending = Pending()
    state: dict = {}
    snap = scanner.scan()
    snap["terms"] = terms.list()
    snap["pending"] = {}
    state["snapshot"] = json.dumps(snap)

    handler = type("BoundHandler", (Handler,), {
        "hub": hub,
        "get_snapshot": staticmethod(lambda: state["snapshot"]),
        "scanner": scanner,
        "terms": terms,
        "pending": pending,
    })

    threading.Thread(target=scan_loop, args=(scanner, terms, pending, hub, state), daemon=True).start()

    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    httpd.daemon_threads = True
    return httpd
