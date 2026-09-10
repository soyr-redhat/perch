"""Conversation snapshots preserve evidence without executing or importing it."""

import base64
import contextlib
import hashlib
import http.client
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from perch import app, conversations
from perch.desktop import lifecycle
from test_runtime import Workspace


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "session.jsonl"
        self.agent = {"id": "claude:example", "harness": "claude", "title": "Example", "cwd": str(self.root), "file": str(self.source)}

    def export(self, rows):
        self.source.write_bytes(b"".join(json.dumps(row).encode() + b"\n" for row in rows))
        return conversations.export_session(self.agent, data_dir=self.root)

    def test_all_records_and_untruncated_text_survive_source_deletion(self):
        rows = [{"role": "user", "text": f"Prompt {i}: " + "x" * 9000} for i in range(200)]
        rows.append({"type": "unknown_future_event", "parent": "branch-2", "data": {"preserved": True}})
        result = self.export(rows)
        original = self.source.read_bytes()
        self.source.unlink()
        manifest = result["manifest"]
        self.assertEqual(manifest["transcript"]["records"], 201)
        self.assertEqual(manifest["recording"]["sha256"], hashlib.sha256(original).hexdigest())
        with zipfile.ZipFile(result["archive"]) as bundle:
            self.assertEqual(bundle.read("source.jsonl"), original)
            transcript = bundle.read("transcript.txt").decode()
            self.assertIn(rows[0]["text"], transcript)
            self.assertIn(rows[199]["text"], transcript)
            self.assertIn("unknown_future_event", transcript)
            self.assertEqual(json.loads(bundle.read("manifest.json")), manifest)

    def test_media_is_extracted_but_external_paths_and_urls_are_never_followed(self):
        secret = self.root / "outside.txt"
        secret.write_text("DO NOT COPY")
        encoded = base64.b64encode(b"inline image bytes").decode()
        result = self.export([{"content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": encoded}},
            {"type": "input_image", "image_url": "data:image/png;base64," + encoded},
            {"type": "local_image", "path": str(secret)},
            {"type": "image", "source": {"type": "url", "url": "https://example.com/private.png"}},
            {"type": "document", "source": {"type": "base64", "data": "invalid??"}},
            {"type": "image", "mimeType": "image/png", "data": encoded},
        ]}])
        attachments = result["manifest"]["attachments"]
        self.assertEqual([item["status"] for item in attachments], ["included", "included", "reference-only", "reference-only", "source-only", "included"])
        with zipfile.ZipFile(result["archive"]) as bundle:
            self.assertEqual(bundle.read(attachments[0]["path"]), b"inline image bytes")
            self.assertEqual(len([name for name in bundle.namelist() if name.startswith("attachments/")]), 1)
            self.assertNotIn(b"DO NOT COPY", bundle.read("transcript.txt"))

    def test_malformed_and_partial_records_preserved_with_explicit_warnings(self):
        original = b'{"role":"user","text":"valid"}\nnot json\n{"unfinished":\xff'
        self.source.write_bytes(original)
        result = conversations.export_session(self.agent, data_dir=self.root)
        self.assertEqual(len(result["manifest"]["transcript"]["warnings"]), 2)
        self.assertEqual((Path(result["directory"]) / "source.jsonl").read_bytes(), original)

    def test_oversized_record_does_not_hide_following_records(self):
        self.source.write_text(json.dumps({"text": "x" * 200}) + '\n{"text":"last"}\n')
        with patch.object(conversations, "MAX_RECORD", 64):
            result = conversations.export_session(self.agent, data_dir=self.root)
        self.assertEqual(result["manifest"]["transcript"]["records"], 2)
        self.assertEqual(len(result["manifest"]["transcript"]["warnings"]), 1)
        self.assertIn('"last"', Path(result["transcript"]).read_text())

    def test_json_recording(self):
        self.source.write_text(json.dumps({"messages": [{"role": "user", "text": "hello"}]}))
        result = conversations.export_session(self.agent, data_dir=self.root, format="json")
        self.assertEqual(result["manifest"]["recording"]["format"], "json")
        self.assertEqual(result["manifest"]["transcript"]["records"], 1)

    def test_changed_recording_aborts_without_publishing(self):
        self.source.write_text('{"text":"hello"}\n')
        real_stamp = conversations._stamp
        calls = 0

        def changed(stat):
            nonlocal calls
            calls += 1
            stamp = real_stamp(stat)
            return stamp if calls == 1 else (*stamp[:-1], stamp[-1] + 1)

        with patch.object(conversations, "_stamp", side_effect=changed):
            with self.assertRaisesRegex(ValueError, "changed"):
                conversations.export_session(self.agent, data_dir=self.root)
        self.assertEqual(list((self.root / "conversations").iterdir()), [])

    def test_failed_archive_is_not_published(self):
        with patch.object(zipfile.ZipFile, "write", side_effect=OSError("Disk full")):
            with self.assertRaisesRegex(OSError, "Disk full"):
                self.export([{"text": "hello"}])
        self.assertEqual(list((self.root / "conversations").iterdir()), [])

    def test_download_paths_cannot_escape_snapshot_directory(self):
        for value in ("../private", "abc", "00000000-0000-0000-0000-000000000000/../source", None):
            with self.assertRaises(ValueError):
                conversations.archive_path(value, data_dir=self.root)


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Workspace(self.temp.name)
        self.addCleanup(self.workspace.close)

    def test_http_export_full_history_authenticated_download_and_unknown_session(self):
        workspace = self.workspace
        status, result = workspace.request("/api/conversations/export", {"agent": "fixture:session-one"})
        self.assertEqual(status, 200)
        self.assertEqual(result["manifest"]["transcript"]["records"], 320)
        self.assertTrue(Path(result["directory"]).is_relative_to(workspace.root))
        connection = http.client.HTTPConnection("127.0.0.1", workspace.port, timeout=5)
        self.addCleanup(connection.close)
        url = "/api/conversations/" + result["id"]
        connection.request("GET", url)
        response = connection.getresponse()
        self.assertEqual(response.status, 401)
        response.read()
        connection.request("GET", url, headers={"X-Perch-Token": workspace.server.token})
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        with zipfile.ZipFile(io.BytesIO(response.read())) as archive:
            self.assertIn(b"Prompt 1", archive.read("transcript.txt"))
            self.assertIn(b"Reply 160", archive.read("transcript.txt"))
        self.assertEqual(workspace.request("/api/conversations/../../settings.json")[0], 404)
        self.assertEqual(workspace.request("/api/conversations/export", {"agent": "fixture:missing"})[0], 400)

    def test_cli_uses_same_engine_without_gui_or_server(self):
        workspace = self.workspace
        with patch.object(app, "Scanner", return_value=workspace.scanner), patch.object(app, "serve") as serve:
            output = io.StringIO()
            with patch("sys.argv", ["perch-cli", "--export-session", "fixture:session-one"]), contextlib.redirect_stdout(output):
                self.assertEqual(app.main(), 0)
            self.assertEqual(json.loads(output.getvalue())["manifest"]["transcript"]["records"], 320)
            output = io.StringIO()
            with patch("sys.argv", ["perch-cli", "--sessions"]), contextlib.redirect_stdout(output):
                self.assertEqual(app.main(), 0)
            self.assertIn("fixture:session-one", [a["id"] for a in json.loads(output.getvalue())])
            with patch("sys.argv", ["perch-cli", "--export-session", "fixture:missing"]), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(app.main(), 1)
            serve.assert_not_called()


class NativeOpeningTests(unittest.TestCase):
    def test_codex_link_uses_os_dispatch_without_shell_or_model_run(self):
        session_id = "12345678-1234-1234-1234-123456789abc"
        with patch.object(lifecycle.sys, "platform", "darwin"), patch.object(lifecycle.subprocess, "run") as run:
            run.return_value.returncode = 0
            lifecycle.open_codex_session(session_id)
            run.assert_called_once_with(["open", "codex://threads/" + session_id], capture_output=True, timeout=10)
        with patch.object(lifecycle.sys, "platform", "win32"), patch.object(lifecycle.os, "startfile", create=True) as start:
            lifecycle.open_codex_session(session_id)
            start.assert_called_once_with("codex://threads/" + session_id)
        for invalid in ("--help", "../secrets", "id?prompt=execute", None):
            with self.assertRaises(ValueError):
                lifecycle.open_codex_session(invalid)

    def test_failed_and_timed_out_open_are_reported(self):
        with patch.object(lifecycle.sys, "platform", "darwin"), patch.object(lifecycle.subprocess, "run") as run:
            run.return_value.returncode = 1
            with self.assertRaises(OSError):
                lifecycle.open_codex_session("12345678-1234-1234-1234-123456789abc")
            run.side_effect = subprocess.TimeoutExpired("open", 10)
            with self.assertRaisesRegex(OSError, "timed out"):
                lifecycle.open_codex_session("12345678-1234-1234-1234-123456789abc")
