"""Pre-merge regressions using temporary configurations and owned fixture processes."""

import contextlib
import io
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from perch import app, scanner, server, sync
from perch.term import Pty, TermRegistry


class TranscriptContractTests(unittest.TestCase):
    def test_windows_temporary_empty_read_is_not_eof(self):
        pty = Pty.__new__(Pty)
        pty._kind = "win"
        pty._p = Mock()
        pty._p.read.side_effect = ["", "ready"]
        pty._p.isalive.return_value = True
        self.assertEqual(pty.read(), b"ready")

    def test_session_id_cannot_be_interpreted_as_cli_flags(self):
        for kind in (scanner.CodexAdapter, scanner.ClaudeAdapter, scanner.OmpAdapter):
            adapter = kind()
            adapter.cmd = ["fixture"]
            self.assertIsNone(adapter.spawn_argv("--dangerously-bypass-approvals-and-sandbox"))
            self.assertIsNone(adapter.message_argv("--last", "hello"))
            self.assertEqual(adapter.message_argv("fixture-id", "--help")[-2:], ["--", "--help"])

    def test_optional_declarative_metadata_is_absent(self):
        adapter = scanner.DeclarativeAdapter({"id": "fixture", "map": {"role": "role", "text": "text"}})
        state = scanner.FileState("fixture")
        adapter.consume({"role": "user", "text": "hello"}, state)
        self.assertIsNone(state.session_id)
        self.assertIsNone(state.cwd)
        self.assertIsNone(state.model)

    def test_one_claude_message_is_one_prompt_with_all_text_blocks(self):
        adapter = scanner.ClaudeAdapter()
        state = scanner.FileState("fixture")
        adapter.consume({"type": "user", "message": {"content": [
            {"type": "text", "text": "First paragraph"},
            {"type": "text", "text": "Second paragraph"},
        ]}}, state)
        self.assertEqual(state.prompts.count, 1)
        self.assertEqual(state.tail[0].text, "First paragraph\n\nSecond paragraph")

    def test_codex_preserves_request_after_context_block(self):
        adapter = scanner.CodexAdapter()
        state = scanner.FileState("fixture")
        adapter.consume({"type": "response_item", "payload": {"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "<environment_context>fixture</environment_context>"},
            {"type": "input_text", "text": "Fix the test"},
        ]}}, state)
        self.assertEqual(state.prompts.count, 1)
        self.assertEqual(state.tail[0].text, "Fix the test")

    def test_html_prompt_is_not_discarded_as_scaffolding(self):
        self.assertEqual(scanner._clean_user_text("<div>Hello</div>"), "<div>Hello</div>")

    def test_malformed_source_document_is_isolated(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "sources.json"
            for document in ([], {"sources": [None, {"id": "bad", "patterns": 42}]}):
                path.write_text(json.dumps(document))
                self.assertEqual(scanner.load_declarative(str(path)), [])


class SharingContractTests(unittest.TestCase):
    def test_automatic_sharing_retries_reported_io_failure(self):
        stop = Mock()
        stop.wait.side_effect = [False, False, True]
        cfg = {"sharing": {"autoSync": True, "skills": True, "mcp": True, "targets": ["codex"]}}
        with patch("perch.settings.load", return_value=cfg), patch("perch.sync.fingerprint", return_value="same"), patch("perch.sync.sync_all", return_value={"skills": {"errors": ["Temporary I/O failure"]}, "mcp": {"errors": []}}) as apply:
            app.auto_share(stop)
        self.assertEqual(apply.call_count, 2)

    def test_backup_preserves_windows_line_endings(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.json"
            original = b'{\r\n  "mcpServers": {}\r\n}\r\n'
            path.write_bytes(original)
            sync._backup(path)
            self.assertEqual(next(Path(temp).glob('*.bak')).read_bytes(), original)

    def test_transport_specific_fields_are_not_silently_dropped(self):
        for definition in (
            {"command": "fixture", "headers": {"X-Test": "value"}},
            {"url": "https://example.test/mcp", "env": {"REGION": "local"}},
            {"url": "https://example.test/mcp", "args": ["--flag"]},
            {"url": "https://example.test/mcp", "headers": {"X": "one"}, "http_headers": {"X": "two"}},
        ):
            with self.subTest(definition=definition), self.assertRaises(ValueError):
                sync._norm_server(definition)

    def test_corrupt_manifest_does_not_create_unrecorded_links(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = root / "manifest.json"
            manifest.write_text("invalid")
            roots = {key: str(root / key) for key in ("claude", "codex")}
            source = Path(roots["claude"]) / "review"
            source.mkdir(parents=True)
            (source / "SKILL.md").write_text("Review code")
            with patch.object(sync, "MANIFEST", str(manifest)):
                result = sync.sync_skills(roots, targets=("codex",))
            self.assertTrue(result["errors"])
            self.assertFalse((Path(roots["codex"]) / "review").exists())

    def test_cli_signals_partial_failure(self):
        report = {"skills": {"errors": []}, "mcp": {"errors": [{"reason": "write failed"}]}}
        with patch.object(sys, "argv", ["perch", "--sync"]), patch.object(sync, "sync_all", return_value=report), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(app.main(), 1)


class TerminalContractTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "POSIX controlling terminal")
    def test_child_has_a_controlling_terminal(self):
        registry = TermRegistry()
        self.addCleanup(registry.shutdown)
        term = registry.spawn("fixture", "Fixture", "#fff", tempfile.gettempdir(), [
            sys.executable, "-u", "-c", "import os; fd=os.open('/dev/tty',os.O_RDWR); print('CONTROLLING_TTY_OK'); os.close(fd)",
        ])
        output = b""
        queue = term.subscribe()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            chunk = queue.get(timeout=max(.1, deadline-time.monotonic()))
            if chunk is None:
                break
            output += chunk
        self.assertIn(b"CONTROLLING_TTY_OK", output)

    def test_fragmented_websocket_message_survives_ping(self):
        def frame(opcode, data, fin=True):
            key = b"abcd"
            return bytes([(128 if fin else 0) | opcode, 128 | len(data)]) + key + bytes(c ^ key[i % 4] for i, c in enumerate(data))
        wire = io.BytesIO(frame(1,b"hel",False)+frame(9,b"ping")+frame(0,b"lo"))
        pings = []
        kind, payload = server._ws_recv(wire, on_ping=pings.append)
        self.assertEqual((kind,payload), ("msg", b"hello"))
        self.assertEqual(pings, [b"ping"])

    def test_truncated_websocket_header_closes_cleanly(self):
        self.assertEqual(server._ws_recv(io.BytesIO(b"\x82\xfe\x00")), ("close", None))

    def test_control_frames_cannot_be_fragmented(self):
        self.assertEqual(server._ws_recv(io.BytesIO(b"\x09\x80abcd")), ("close", None))

    def test_unexpected_continuation_is_rejected(self):
        self.assertEqual(server._ws_recv(io.BytesIO(b"\x80\x80abcd")), ("close", None))

    def test_large_frame_limit(self):
        self.assertEqual(server._ws_recv(io.BytesIO(b"\x82\xff" + struct.pack("!Q", 2**32))), ("close", None))
