import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import tomllib
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

import scanner
import server
import settings
import sync
from term import TermRegistry
from demo import DemoScanner


class ScannerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "session.jsonl"
        self.adapter = scanner.DeclarativeAdapter(
            {"id": "fixture", "patterns": [str(self.path)], "map": {"role": "role", "text": "text"}}
        )
        self.scanner = scanner.Scanner()
        self.scanner.adapters = [self.adapter]
        self.patcher = patch.object(scanner, "scan_processes", return_value={})
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def parse(self):
        stat = self.path.stat()
        return self.scanner._parse_file(self.adapter, str(self.path), stat.st_mtime_ns, stat.st_size)

    def test_unchanged_json_not_folded_twice(self):
        self.adapter.format = "json"
        self.path.write_text(json.dumps([{"role": "user", "text": "hello"}]))
        self.assertEqual(len(self.parse().tail), 1)
        self.assertEqual(len(self.parse().tail), 1)

    def test_partial_append_retried(self):
        self.path.write_bytes(b'{"role":"user","text":"he')
        self.assertEqual(self.parse().offset, 0)
        with self.path.open("ab") as f:
            f.write(b'llo"}\n')
        self.assertEqual(self.parse().tail[0].text, "hello")

    def test_long_history_indices_and_seek(self):
        self.path.write_text(
            "".join(json.dumps({"role": "user", "text": f"prompt {i}"}) + "\n" for i in range(160))
        )
        st = self.parse()
        self.assertEqual(st.prompts[0]["index"], 10)
        result = scanner.read_history(
            self.adapter, str(self.path), 10, offset=st.prompts[0]["offset"], total=160
        )
        self.assertEqual(result["events"][0]["text"], "prompt 10")
        self.assertEqual(result["of"], 160)

    def test_malformed_record_isolated(self):
        self.adapter = scanner.ClaudeAdapter()
        self.adapter.patterns = [str(self.path)]
        self.scanner.adapters = [self.adapter]
        self.path.write_text('[]\n{"type":"summary","summary":"still readable"}\n')
        snap = self.scanner.scan()
        self.assertEqual(snap["agents"][0]["title"], "still readable")
        self.assertEqual(len(snap["errors"]), 1)

    def test_idle_snapshot_equal_without_opening_files(self):
        self.path.write_text('{"role":"user","text":"hello"}\n')
        first = self.scanner.scan()
        with patch("builtins.open", side_effect=AssertionError("Unchanged file reopened")):
            second = self.scanner.scan()
        self.assertEqual(first, second)

    def test_multiline_text_preserved(self):
        self.assertEqual(scanner._truncate("a\n  b"), "a\n  b")

    def test_replaced_file_resets_state(self):
        self.path.write_text('{"role":"user","text":"old"}\n')
        self.scanner.scan()
        replacement = self.path.with_suffix(".tmp")
        replacement.write_text('{"role":"user","text":"new"}\n')
        os.replace(replacement, self.path)
        snap = self.scanner.scan()
        self.assertEqual(snap["agents"][0]["title"], "new")

    def test_windows_npm_shim_bypasses_shell(self):
        wrapper = Path(self.tmp.name) / "cli.cmd"
        entry = Path(self.tmp.name) / "node_modules" / "tool" / "cli.js"
        entry.parent.mkdir(parents=True)
        entry.write_text("// fixture")
        wrapper.write_text('"%_prog%" "%dp0%\\node_modules\\tool\\cli.js" %*')

        def which(name):
            return str(wrapper) if name == "fixture-cli" else "node.exe"

        with (
            patch.object(scanner.platform, "system", return_value="Windows"),
            patch.object(scanner.shutil, "which", side_effect=which),
        ):
            argv = scanner._which("fixture-cli")
        self.assertEqual(argv, ["node.exe", str(entry.resolve())])


class SharingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.paths = {
            k: str(self.root / (k + (".toml" if k == "codex" else ".json")))
            for k in ["claude", "codex", "omp", "claude-desktop", "claude_mcpjson"]
        }

    def seed(self):
        Path(self.paths["claude"]).write_text(
            json.dumps({"mcpServers": {"new": {"command": "example", "args": ["--stdio"]}}})
        )
        Path(self.paths["codex"]).write_text(
            '# Preserve this comment\n[projects.demo]\ntrust_level="trusted"\n\n[mcp_servers.existing]\ncommand="existing"\nenabled=false\nstartup_timeout_sec=90\n'
        )

    def test_preserve_existing_fields_and_comments(self):
        self.seed()
        sync.sync_mcp(self.paths, targets=("codex",))
        text = Path(self.paths["codex"]).read_text()
        doc = tomllib.loads(text)
        self.assertFalse(doc["mcp_servers"]["existing"]["enabled"])
        self.assertEqual(doc["mcp_servers"]["existing"]["startup_timeout_sec"], 90)
        self.assertIn("# Preserve this comment", text)
        self.assertIn("new", doc["mcp_servers"])
        self.assertFalse(Path(self.paths["omp"]).exists())

    def test_preview_does_not_write(self):
        self.seed()
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        result = sync.sync_mcp(self.paths, targets=("codex", "omp"), dry_run=True)
        self.assertEqual(result["added"]["codex"], ["new"])
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir()})

    def test_header_translation(self):
        Path(self.paths["claude"]).write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "remote": {
                            "type": "http",
                            "url": "https://example.test/mcp",
                            "headers": {"X-Region": "us"},
                        }
                    }
                }
            )
        )
        sync.sync_mcp(self.paths, targets=("codex",))
        doc = tomllib.loads(Path(self.paths["codex"]).read_text())
        self.assertEqual(doc["mcp_servers"]["remote"]["http_headers"], {"X-Region": "us"})

    def test_conflicts_not_propagated(self):
        self.seed()
        Path(self.paths["codex"]).write_text('[mcp_servers.new]\ncommand="different"\n')
        result = sync.sync_mcp(self.paths)
        self.assertTrue(result["conflicts"])
        self.assertFalse(Path(self.paths["omp"]).exists())

    def test_unknown_fields_block_lossy_conversion(self):
        Path(self.paths["codex"]).write_text(
            '[mcp_servers.remote]\nurl="https://example.test"\nbearer_token_env_var="MY_TOKEN"\n'
        )
        result = sync.sync_mcp(self.paths, targets=("claude",))
        self.assertTrue(result["blocked"])
        self.assertFalse(Path(self.paths["claude"]).exists())

    def test_invalid_destination_not_overwritten(self):
        self.seed()
        Path(self.paths["codex"]).write_text("not valid toml [")
        result = sync.sync_mcp(self.paths, targets=("codex",))
        self.assertTrue(result["errors"])
        self.assertEqual(Path(self.paths["codex"]).read_text(), "not valid toml [")

    def test_remote_desktop_reported_unsupported(self):
        Path(self.paths["claude"]).write_text(
            json.dumps({"mcpServers": {"remote": {"type": "http", "url": "https://example.test"}}})
        )
        result = sync.sync_mcp(self.paths, targets=("claude-desktop",))
        self.assertTrue(result["blocked"])
        self.assertFalse(Path(self.paths["claude-desktop"]).exists())

    def test_idempotent(self):
        self.seed()
        sync.sync_mcp(self.paths)
        self.assertEqual(sync.sync_mcp(self.paths)["added"], {})

    def test_skill_conflict_not_spread(self):
        roots = {k: str(self.root / k) for k in ["claude", "codex", "omp"]}
        for k in ["claude", "codex"]:
            p = Path(roots[k]) / "skill"
            p.mkdir(parents=True)
            (p / "SKILL.md").write_text(k)
        result = sync.sync_skills(roots, targets=("omp",), dry_run=True)
        self.assertTrue(result["conflicts"])
        self.assertEqual(result["linked"], [])

    def test_stale_plan_rejected(self):
        with (
            patch.object(sync, "PERCH_DIR", str(self.root)),
            patch.object(sync, "fingerprint", return_value="new"),
        ):
            with self.assertRaisesRegex(ValueError, "changed"):
                sync.sync_all(revision="stale", dry_run=False)


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.server = server.serve(DemoScanner(), TermRegistry(), 0, demo=True)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.close)
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def request(self, path, body=None, auth=True, headers=None):
        h = {"X-Perch-Token": self.server.token} if auth else {}
        if body is not None:
            h["Content-Type"] = "application/json"
        h.update(headers or {})
        req = urllib.request.Request(
            self.url + path, data=json.dumps(body).encode() if body is not None else None, headers=h
        )
        try:
            with urllib.request.urlopen(req, timeout=3) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            data = e.read()
            code = e.code
            e.close()
            return code, data

    def test_auth_required(self):
        self.assertEqual(self.request("/api/snapshot", auth=False)[0], 401)
        self.assertEqual(self.request("/api/snapshot")[0], 200)

    def test_origin_rejected_even_with_token(self):
        self.assertEqual(self.request("/api/sync", {}, headers={"Origin": "https://evil.invalid"})[0], 403)

    def test_non_json_rejected(self):
        self.assertEqual(self.request("/api/sync", {}, headers={"Content-Type": "text/plain"})[0], 415)

    def test_malformed_body_rejected(self):
        self.assertEqual(self.request("/api/spawn", [])[0], 400)
        self.assertEqual(self.request("/api/spawn", {"cwd": 42})[0], 400)

    def test_static_path_containment(self):
        self.assertEqual(self.request("/../perch.py")[0], 404)

    def test_demo_cannot_mutate(self):
        self.assertEqual(self.request("/api/sync", {})[0], 403)

    def test_latest_event_replaces_old_backlog(self):
        hub = server.EventHub()
        q = hub.subscribe()
        for i in range(20):
            hub.publish(str(i))
        items = []
        while not q.empty():
            items.append(q.get_nowait())
        self.assertEqual(items[-1], "19")

    def test_oversize_websocket_rejected(self):
        import struct

        data = b"\x82\xff" + struct.pack("!Q", 2**32)
        self.assertEqual(server._ws_recv(io.BytesIO(data))[0], "close")


class TerminalTests(unittest.TestCase):
    def test_terminal_output_and_cleanup(self):
        import sys

        registry = TermRegistry()
        self.addCleanup(registry.shutdown)
        term = registry.spawn(
            "fixture",
            "Python",
            "#fff",
            tempfile.gettempdir(),
            [sys.executable, "-u", "-c", 'import time; print("ready", flush=True); time.sleep(60)'],
        )
        q = term.subscribe()
        # ConPTY emits control sequences before process output; stream boundaries
        # are not message boundaries on either platform.
        data = b""
        deadline = time.monotonic() + 10
        while b"ready" not in data and time.monotonic() < deadline:
            chunk = q.get(timeout=max(0.1, deadline - time.monotonic()))
            if chunk is None:
                break
            data += chunk
        self.assertIn(b"ready", data)
        term.resize(100, 30)
        registry.remove(term.id)
        self.assertFalse(term.alive())
        if os.name != "nt":
            self.assertIsNone(term.pty._m)


class SettingsTests(unittest.TestCase):
    def test_invalid_saved_types_do_not_escape(self):
        cfg = settings.validate(
            settings.DEFAULTS, {"watching": {"quietDays": float("nan")}, "sharing": {"targets": ["nope"]}}
        )
        self.assertEqual(cfg["watching"]["quietDays"], 7)
        self.assertEqual(cfg["sharing"]["targets"], [])
