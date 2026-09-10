"""Smoke-test an installed CLI's backend and embedded terminal without real accounts."""

import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from uuid import uuid4


def wait_for(fn, proc, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"Packaged CLI exited with {proc.returncode}")
        result = fn()
        if result:
            return result
        time.sleep(.1)
    raise AssertionError("Packaged operation timed out")


def main():
    executable = str(Path(sys.argv[1]).resolve())
    fixture = str(Path(__file__).resolve().parents[1] / "tests" / "fixture_harness.py")
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        target = root / "sessions/context-target.jsonl"
        target.parent.mkdir()
        target.write_text("".join(json.dumps({"id": "context-target", "cwd": str(root), "role": role, "text": text}) + "\n"
                                  for role, text in (("user", "Destination"), ("assistant", "Ready"))))
        os.utime(target, (time.time() - 60, time.time() - 60))
        (root / "settings.json").write_text(json.dumps({"harnesses": {"disabled": ["claude", "codex", "omp"]}}))
        (root / "sources.json").write_text(json.dumps({"sources": [{
            "id": "fixture", "patterns": [str(root / "sessions" / "*.jsonl")],
            "cmd": [sys.executable, "-u", fixture, str(root)],
            "message": ["--message", "{session}", "{text}"],
            "map": {"id": "id", "cwd": "cwd", "role": "role", "text": "text"},
        }]}))
        proc = subprocess.Popen([executable, "--no-browser", "--port", "0"], env={**os.environ, "PERCH_DATA_DIR": str(root)}, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, creationflags=0x08000000 if os.name == "nt" else 0)
        info, term_id = None, None

        def request(path, body=None):
            headers = {"X-Perch-Token": info["token"]}
            if body is not None:
                headers["Content-Type"] = "application/json"
            req = urllib.request.Request(f'http://127.0.0.1:{info["port"]}{path}', data=json.dumps(body).encode() if body is not None else None, headers=headers)
            with urllib.request.urlopen(req, timeout=5) as response:
                return json.load(response)

        try:
            print("Packaged application:", executable, "External fixture Python:", sys.executable, flush=True)
            wait_for(lambda: (root / "instance.json").is_file(), proc)
            info = json.loads((root / "instance.json").read_text())
            wait_for(lambda: not request("/api/snapshot").get("loading"), proc)
            second = subprocess.run([executable, "--no-browser", "--port", "0"], env={**os.environ, "PERCH_DATA_DIR": str(root)}, capture_output=True, timeout=15)
            assert second.returncode == 0, second.stderr
            assert json.loads((root / "instance.json").read_text()) == info
            term = request("/api/spawn", {"harness": "fixture", "cwd": str(root)})["term"]
            term_id = term["id"]
            assert term["alive"]
            with socket.create_connection(("127.0.0.1", info["port"]), timeout=20) as connection:
                connection.sendall((f'GET /ws/term/{term_id} HTTP/1.1\r\nHost: 127.0.0.1:{info["port"]}\r\nX-Perch-Token: {info["token"]}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n\r\n').encode())
                with connection.makefile("rb") as stream:
                    assert b"101" in stream.readline()
                    while stream.readline() != b"\r\n":
                        pass
                    output = b""
                    while b"FIXTURE_READY" not in output:
                        try:
                            header = stream.read(2)
                        except TimeoutError as error:
                            raise AssertionError("Terminal did not become ready: " + repr(output)) from error
                        assert len(header) == 2, repr(output)
                        length = header[1] & 127
                        if length == 126:
                            length = struct.unpack("!H", stream.read(2))[0]
                        elif length == 127:
                            length = struct.unpack("!Q", stream.read(8))[0]
                        output += stream.read(length)
                        if header[0] & 15 == 1:
                            raise AssertionError("Terminal exited before ready: " + repr(output))
                    payload = json.dumps({"type": "input", "data": "packaged smoke\r"}).encode()
                    mask = os.urandom(4)
                    connection.sendall(bytes([129, 128 | len(payload)]) + mask + bytes(c ^ mask[i % 4] for i, c in enumerate(payload)))
                    transcript = root / "sessions" / "new-session.jsonl"
                    wait_for(lambda: transcript.exists() and "Fixture reply: packaged smoke" in transcript.read_text(), proc)
            request("/api/kill", {"id": term_id})
            term_id = None
            assert request("/api/snapshot")["terms"] == []
            wait_for(lambda: any(a["id"] == "fixture:new-session" for a in request("/api/snapshot")["agents"]), proc)
            request("/api/message", {"agent": "fixture:new-session", "text": "packaged reply"})
            wait_for(lambda: "Fixture reply: packaged reply" in transcript.read_text(), proc)
            wait_for(lambda: not request("/api/snapshot").get("pending"), proc)
            exported = request("/api/conversations/export", {"agent": "fixture:new-session"})
            with zipfile.ZipFile(exported["archive"]) as archive:
                assert archive.read("source.jsonl") == transcript.read_bytes()
                assert b"Fixture reply: packaged reply" in archive.read("transcript.txt")
            cli_export = subprocess.run([executable, "--export-session", "fixture:new-session"], env={**os.environ, "PERCH_DATA_DIR": str(root)}, capture_output=True, timeout=15)
            assert cli_export.returncode == 0, cli_export.stderr
            assert json.loads(cli_export.stdout)["manifest"]["recording"]["sha256"] == exported["manifest"]["recording"]["sha256"]
            catalog = subprocess.run([executable, "--capabilities"], env={**os.environ, "PERCH_DATA_DIR": str(root)}, capture_output=True, timeout=15)
            assert catalog.returncode in (0, 1), catalog.stderr
            assert isinstance(json.loads(catalog.stdout)["resources"], list)
            wait_for(lambda: any(a["id"] == "fixture:context-target" and a["state"] != "working" for a in request("/api/snapshot")["agents"]), proc)
            transfer_id = str(uuid4())
            prepared = subprocess.run([executable, "--transfer", "fixture:new-session", "--to", "fixture:context-target", "--transfer-id", transfer_id], env={**os.environ, "PERCH_DATA_DIR": str(root)}, capture_output=True, timeout=15)
            assert prepared.returncode == 0, prepared.stderr
            assert json.loads(prepared.stdout)["status"] == "prepared"
            sent = subprocess.run([executable, "--send-transfer", transfer_id], env={**os.environ, "PERCH_DATA_DIR": str(root)}, capture_output=True, timeout=15)
            assert sent.returncode == 0, sent.stderr
            wait_for(lambda: request("/api/transfers/" + transfer_id)["status"] == "delivered", proc)
            assert "Read the conversation context at" in target.read_text()
            print("Packaged single instance, backend, terminal input/output, headless reply, export, resource inventory, context transfer, and cleanup passed")
        finally:
            marker = root / "fixture-started.json"
            print("Fixture startup:", marker.read_text() if marker.exists() else "never entered fixture", flush=True)
            try:
                if term_id and info:
                    print("Terminal state:", request("/api/snapshot").get("terms"))
                    request("/api/kill", {"id": term_id})
            finally:
                proc.terminate()
            try:
                _, errors = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                _, errors = proc.communicate(timeout=5)
            if errors:
                print(errors.decode("utf-8", "replace"), file=sys.stderr)


if __name__ == "__main__":
    main()
