"""Preparation never executes a harness; sending has a durable idempotency record."""

import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from perch import app, transfers
from perch.desktop import lifecycle
from test_runtime import Workspace


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Workspace(self.temp.name)
        self.addCleanup(self.workspace.close)
        self.scanner = self.workspace.scanner
        for path in (self.workspace.root / "sessions").glob("*"):
            os.utime(path, (time.time() - 60, time.time() - 60))
        self.workspace.wait_for(lambda: all(a["state"] != "working" for a in self.workspace.request('/api/snapshot')[1]["agents"]))
        self.transfer_id = str(uuid4())

    def prepare(self):
        return transfers.prepare(self.scanner, "fixture:session-one", "fixture:session-two", self.transfer_id)

    def test_prepare_is_repeatable_and_preserves_context_after_source_deletion(self):
        receipt = self.prepare()
        original = (self.workspace.root / "sessions/session-two.jsonl").read_bytes()
        (self.workspace.root / "sessions/session-one.jsonl").unlink()
        self.assertEqual(self.prepare(), receipt)
        self.assertEqual((self.workspace.root / "sessions/session-two.jsonl").read_bytes(), original)
        self.assertIn("Prompt 1", Path(receipt["transcript"]).read_text())
        self.assertIn("Reply 160", Path(receipt["transcript"]).read_text())
        with self.assertRaisesRegex(ValueError, "different selection"):
            transfers.prepare(self.scanner, "fixture:session-two", "fixture:session-one", self.transfer_id)

    def test_delivery_sends_once_through_existing_harness_engine(self):
        self.prepare()
        (self.workspace.root / "instance.json").write_text(json.dumps({"port": self.workspace.port, "token": self.workspace.server.token}))
        result = transfers.send_to_app(self.scanner, self.transfer_id)
        self.assertIn(result["status"], ("running", "delivered"))
        status, result = self.workspace.request('/api/transfers/send', {"id": self.transfer_id})
        self.assertEqual(status, 200, result)
        self.workspace.wait_for(lambda: transfers.read(self.scanner, self.transfer_id)["status"] == "delivered")
        self.assertEqual(self.workspace.request('/api/transfers/send', {"id": self.transfer_id})[0], 200)
        rows = [json.loads(row) for row in (self.workspace.root/'sessions/session-two.jsonl').read_text().splitlines()]
        self.assertEqual(sum(r.get("role") == "user" and "Read the conversation context at" in r.get("text", "") for r in rows), 1)

    def test_missing_context_busy_destination_and_restart_are_explicit(self):
        receipt = self.prepare()
        agent = next(a for a in self.scanner.scan()["agents"] if a["id"] == receipt["target"])
        os.utime(agent["file"], None)
        self.workspace.wait_for(lambda: any(a["id"] == receipt["target"] and a["state"] == "working" for a in self.workspace.request('/api/snapshot')[1]["agents"]))
        self.assertEqual(self.workspace.request('/api/transfers/send', {"id": self.transfer_id})[0], 409)
        self.assertEqual(transfers.read(self.scanner,self.transfer_id)["status"],"prepared")
        self.assertTrue(transfers.claim(self.scanner, self.transfer_id))
        self.assertFalse(transfers.claim(self.scanner, self.transfer_id))
        transfers.recover(self.scanner)
        self.assertEqual(transfers.read(self.scanner,self.transfer_id)["status"],"uncertain")
        self.assertFalse(transfers.claim(self.scanner, self.transfer_id))
        other = transfers.prepare(self.scanner, "fixture:session-one", "fixture:session-two", str(uuid4()))
        Path(other["transcript"]).unlink()
        with self.assertRaisesRegex(ValueError, "missing"):
            transfers.claim(self.scanner,other["id"])

    def test_cli_preparation_matches_desktop_and_receipt_routes_are_authenticated(self):
        with patch.object(app,"Scanner",return_value=self.scanner), patch("sys.argv",["perch-cli","--transfer","fixture:session-one","--to","fixture:session-two","--transfer-id",self.transfer_id]), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(app.main(),0)
        receipt=json.loads(output.getvalue())
        self.assertEqual(self.workspace.request('/api/transfers/'+self.transfer_id)[1],receipt)
        self.assertEqual(self.workspace.request('/api/transfers/'+self.transfer_id,headers={"X-Perch-Token":""})[0],401)
        self.assertEqual(self.workspace.request('/api/transfers/../settings.json')[0],404)

    def test_native_cli_launcher_quotes_arguments_and_uses_external_console(self):
        with patch.object(lifecycle.sys,"platform","darwin"),patch.object(lifecycle,"_mac_open") as opening:
            lifecycle.open_cli_session(self.scanner,"fixture:session-two")
            script=Path(opening.call_args.args[-1])
            self.assertIn("--resume session-two",script.read_text())
            self.assertIn("exec ",script.read_text())
            if os.name != "nt":
                self.assertEqual(script.stat().st_mode & 0o777,0o700)
        with patch.object(lifecycle.sys,"platform","win32"),patch.object(lifecycle.subprocess,"Popen") as opening:
            lifecycle.open_cli_session(self.scanner,"fixture:session-two")
            self.assertEqual(opening.call_args.kwargs["creationflags"],0x10)
            self.assertIsInstance(opening.call_args.kwargs["env"],dict)
        with self.assertRaises(ValueError):
            lifecycle.open_cli_session(self.scanner,"fixture:missing")
