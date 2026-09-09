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
        (root / "settings.json").write_text(json.dumps({"harnesses": {"disabled": ["claude", "codex", "omp"]}}))
        (root / "sources.json").write_text(json.dumps({"sources": [{
            "id": "fixture", "patterns": [str(root / "sessions" / "*.jsonl")],
            "cmd": [sys.executable, "-u", fixture, str(root)],
            "map": {"id": "id", "cwd": "cwd", "role": "role", "text": "text"},
        }]}))
        proc = subprocess.Popen([executable, "--no-browser", "--port", "0"], env={**os.environ, "PERCH_DATA_DIR": str(root)}, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
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
            term = request("/api/spawn", {"harness": "fixture", "cwd": str(root)})["term"]
            term_id = term["id"]
            assert term["alive"]
            if os.name == "nt":
                diagnostic = subprocess.run(["powershell", "-NoProfile", "-Command", "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and $_.CommandLine.Contains($env:PERCH_FIXTURE_PATH) } | Select-Object ProcessId,ParentProcessId,CommandLine | ConvertTo-Json -Compress"], env={**os.environ, "PERCH_FIXTURE_PATH": fixture}, capture_output=True, text=True, timeout=10)
                print("Fixture processes:", diagnostic.stdout, diagnostic.stderr, flush=True)
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
            print("Packaged backend, WebSocket input, terminal child, and cleanup passed")
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
