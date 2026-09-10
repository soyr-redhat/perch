"""Exercise the production HTTP/SSE/WebSocket and process paths on a local fixture."""

import contextlib
import http.client
import json
import os
from pathlib import Path
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from perch import scanner, server, settings, storage, sync
from perch.term import TermRegistry


class Workspace:
    def __init__(self, root):
        self.root = Path(root)
        self.stack = contextlib.ExitStack()
        self.paths = {key: str(self.root / (key + (".toml" if key == "codex" else ".json"))) for key in ("claude", "claude_mcpjson", "codex", "omp", "claude-desktop")}
        roots = {key: str(self.root / "skills" / key) for key in ("claude", "codex", "omp")}
        for obj, name, value in (
            (sync, "PERCH_DIR", str(self.root / "state")),
            (sync, "MANIFEST", str(self.root / "state" / "manifest.json")),
            (sync, "SKILL_ROOTS", roots),
            (settings, "PATH", str(self.root / "state" / "settings.json")),
            (settings, "LAST_SYNC", str(self.root / "state" / "last-sync.json")),
            (settings, "sync_lock", lambda: storage.sync_lock(self.root / "state")),
        ):
            self.stack.enter_context(patch.object(obj, name, value))
        self.stack.enter_context(patch.object(sync, "_paths", return_value=self.paths))
        self.stack.enter_context(patch.object(scanner, "scan_processes", return_value={}))
        adapter = scanner.DeclarativeAdapter({
            "id": "fixture", "name": "Fixture CLI", "patterns": [str(self.root / "sessions" / "*.jsonl")],
            "cmd": [sys.executable, "-u", str(Path(__file__).with_name("fixture_harness.py")), str(self.root)],
            "resume": ["--resume", "{session}"], "message": ["--message", "{session}", "{text}"],
            "map": {"id": "id", "cwd": "cwd", "role": "role", "text": "text"},
        })
        self.scanner = scanner.Scanner(config_dir=str(self.root))
        self.scanner.adapters = [adapter]
        self.seed("session-one", 160)
        self.seed("session-two", 1)
        skill = Path(roots["claude"]) / "fixture-review"
        skill.mkdir(parents=True, exist_ok=True)
        (skill / "SKILL.md").write_text("---\nname: fixture-review\ndescription: Test fixture\n---\nReview fixture code.")
        Path(self.paths["claude"]).write_text(json.dumps({"mcpServers": {"fixture-tool": {"command": "fixture-command"}}}))
        self.scanner.start_watching()
        self.server = server.serve(self.scanner, TermRegistry(), 0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.port = self.server.server_port
        self.wait_for(lambda: self.request("/api/snapshot")[1].get("agents"))

    def seed(self, session, count):
        path = self.root / "sessions" / (session + ".jsonl")
        path.parent.mkdir(exist_ok=True)
        with path.open("w") as out:
            for index in range(count):
                for role, text in (("user", f"Prompt {index+1}"), ("assistant", f"Reply {index+1}")):
                    out.write(json.dumps({"id": session, "cwd": str(self.root), "role": role, "text": text}) + "\n")

    def request(self, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            h = {"X-Perch-Token": self.server.token, **(headers or {})}
            if body is not None:
                h["Content-Type"] = "application/json"
            connection.request("POST" if body is not None else "GET", path, json.dumps(body) if body is not None else None, h)
            response = connection.getresponse()
            data = response.read()
            return response.status, json.loads(data) if data else None
        finally:
            connection.close()

    @staticmethod
    def wait_for(fn, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = fn()
            if result:
                return result
            time.sleep(.025)
        raise AssertionError("Fixture operation did not complete")

    def close(self):
        self.server.close()
        self.stack.close()


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Workspace(self.temp.name)
        self.addCleanup(self.workspace.close)
        self.request = self.workspace.request

    def test_terminal_mutations_immediately_update_snapshot(self):
        code, result = self.request("/api/spawn", {"harness": "fixture", "cwd": self.temp.name})
        self.assertEqual(code, 200)
        term_id = result["term"]["id"]
        self.assertIn(term_id, [t["id"] for t in self.request("/api/snapshot")[1]["terms"]])
        self.assertIn(term_id, [t["id"] for t in result["snapshot"]["terms"]])
        self.request("/api/kill", {"id": term_id})
        self.assertEqual(self.request("/api/snapshot")[1]["terms"], [])

    def test_reopening_owned_session_reuses_terminal_and_blocks_parallel_reply(self):
        body = {"harness": "fixture", "cwd": self.temp.name, "session": "session-two"}
        first = self.request("/api/spawn", body)[1]["term"]["id"]
        second = self.request("/api/spawn", body)[1]["term"]["id"]
        self.assertEqual(first, second)
        self.assertEqual(self.request("/api/message", {"agent": "fixture:session-two", "text": "hello"})[0], 409)

    def test_headless_reply_blocks_parallel_resume(self):
        self.request("/api/message", {"agent": "fixture:session-two", "text": "__SLOW__"})
        self.assertEqual(self.request("/api/spawn", {"harness": "fixture", "cwd": self.temp.name, "session": "session-two"})[0], 409)

    def test_history_first_middle_latest_and_out_of_range(self):
        for index in (0, 80, 159):
            code, data = self.request(f"/api/history?agent=fixture:session-one&prompt={index}")
            self.assertEqual(code, 200)
            self.assertEqual([e["text"] for e in data["events"]], [f"Prompt {index+1}", f"Reply {index+1}"])
            self.assertEqual(data["of"], 160)
        for index in (-1, 160):
            self.assertEqual(self.request(f"/api/history?agent=fixture:session-one&prompt={index}")[0], 400)

    def test_settings_persist_and_disabling_harness_updates_sessions(self):
        code, _ = self.request("/api/settings", {"appearance": {"theme": "light"}, "harnesses": {"disabled": ["fixture"]}})
        self.assertEqual(code, 200)
        self.assertEqual(self.request("/api/settings")[1]["settings"]["appearance"]["theme"], "light")
        self.workspace.wait_for(lambda: not self.request("/api/snapshot")[1]["agents"])
        self.assertEqual(self.request("/api/spawn", {"harness": "fixture"})[0], 400)

    def test_delivery_records_real_output_and_rejects_duplicates(self):
        body = {"agent": "fixture:session-two", "text": "__SLOW__"}
        self.assertEqual(self.request("/api/message", body)[0], 200)
        self.assertEqual(self.request("/api/message", body)[0], 409)
        self.workspace.wait_for(lambda: not self.workspace.server.delivering)
        self.workspace.wait_for(lambda: any(e["text"] == "Fixture reply: __SLOW__" for a in self.request("/api/snapshot")[1]["agents"] for e in a["tail"]))

    def test_recorded_reply_replaces_pending_message(self):
        self.assertEqual(self.request("/api/message", {"agent": "fixture:session-two", "text": "deduplicate me"})[0], 200)
        self.workspace.wait_for(
            lambda: any(
                event["who"] == "user" and event["text"] == "deduplicate me"
                for agent in self.request("/api/snapshot")[1]["agents"]
                if agent["id"] == "fixture:session-two"
                for event in agent["tail"]
            )
        )
        self.assertNotIn("fixture:session-two", self.request("/api/snapshot")[1]["pending"])

    def test_agent_snapshot_carries_harness_name(self):
        agents = self.request("/api/snapshot")[1]["agents"]
        self.assertTrue(all(agent["harness_name"] == "Fixture CLI" for agent in agents))

    def test_failed_delivery_is_visible(self):
        self.request("/api/message", {"agent": "fixture:session-two", "text": "__FAIL__"})
        self.workspace.wait_for(lambda: not self.workspace.server.delivering)
        pending = self.request("/api/snapshot")[1]["pending"]
        self.assertIn("Fixture delivery failed", pending["fixture:session-two"][0]["error"])

    def test_shared_tools_preview_apply_and_idempotence(self):
        _, preview = self.request("/api/sync", {"preview": True})
        self.assertFalse(Path(self.workspace.paths["codex"]).exists())
        code, result = self.request("/api/sync", {"preview": False, "revision": preview["revision"]})
        self.assertEqual(code, 200)
        self.assertEqual(result["mcp"]["added"], preview["mcp"]["added"])
        self.assertEqual(self.request("/api/sync", {"preview": True})[1]["mcp"]["added"], {})
        self.assertEqual(self.request("/api/sync", {"preview": False, "revision": preview["revision"]})[0], 400)

    def test_websocket_input_and_reconnection_replay(self):
        _, data = self.request("/api/spawn", {"harness": "fixture", "cwd": self.temp.name})
        term_id = data["term"]["id"]
        def connect():
            connection = socket.create_connection(("127.0.0.1", self.workspace.port), timeout=5)
            self.addCleanup(connection.close)
            stream = connection.makefile("rb")
            self.addCleanup(stream.close)
            connection.sendall((f"GET /ws/term/{term_id} HTTP/1.1\r\nHost: 127.0.0.1:{self.workspace.port}\r\nX-Perch-Token: {self.workspace.server.token}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n\r\n").encode())
            self.assertIn(b"101", stream.readline())
            while stream.readline() != b"\r\n":
                pass
            return connection, stream
        def receive_until(stream, expected):
            output = b""
            while expected not in output:
                head = stream.read(2)
                self.assertEqual(len(head), 2)
                size = head[1] & 127
                if size == 126:
                    size = struct.unpack("!H", stream.read(2))[0]
                elif size == 127:
                    size = struct.unpack("!Q", stream.read(8))[0]
                output += stream.read(size)
            return output
        connection, stream = connect()
        receive_until(stream, b"FIXTURE_READY")
        message, mask = b"hello fixture\r", os.urandom(4)
        connection.sendall(bytes([129, 128 | len(message)]) + mask + bytes(c ^ mask[i % 4] for i, c in enumerate(message)))
        receive_until(stream, b"Fixture reply: hello fixture")
        connection.shutdown(socket.SHUT_RDWR)
        connection.close()
        _, replay = connect()
        receive_until(replay, b"Fixture reply: hello fixture")
