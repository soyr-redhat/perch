"""Single-instance ownership and shell executable discovery for GUI launches."""

from __future__ import annotations
import json
import os
import subprocess
import sys
from pathlib import Path
import urllib.request
from ..storage import DATA_DIR, write_json


def _mac_open(*args):
    try:
        result = subprocess.run(["open", *args], capture_output=True, timeout=10)
    except subprocess.TimeoutExpired as exc:
        raise OSError("Opening the application timed out") from exc
    if result.returncode:
        raise OSError("Could not open the item. Check that its application is installed.")


def show_export(archive):
    if not archive.is_file():
        raise ValueError("Export not found")
    if sys.platform == "darwin":
        _mac_open("-R", str(archive))
    elif sys.platform == "win32":
        os.startfile(str(archive.parent))
    else:
        raise ValueError("File reveal is supported on macOS and Windows")


def open_codex_session(session_id):
    """Ask the OS to open a known session through Codex's registered deep link."""
    from uuid import UUID

    if not isinstance(session_id, str) or str(UUID(session_id)) != session_id:
        raise ValueError("Invalid Codex session ID")
    url = "codex://threads/" + session_id
    if sys.platform == "darwin":
        _mac_open(url)
    elif sys.platform == "win32":
        os.startfile(url)
    else:
        raise ValueError("Native session opening is supported on macOS and Windows")


def extend_path():
    # Finder does not inherit shell initialization. No shell startup scripts are executed.
    candidates = [
        Path("~/.local/bin").expanduser(),
        Path("/opt/homebrew/bin"),
        Path("/usr/local/bin"),
        Path("~/.cargo/bin").expanduser(),
        Path("~/AppData/Roaming/npm").expanduser(),
    ]
    nvm = Path("~/.nvm/versions/node").expanduser()
    if nvm.is_dir():
        candidates += sorted(nvm.glob("*/bin"), reverse=True)
    current = os.environ.get("PATH", "").split(os.pathsep)
    os.environ["PATH"] = os.pathsep.join(
        dict.fromkeys([*current, *(str(p) for p in candidates if p.is_dir())])
    )


class Instance:
    def __init__(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.handle = (DATA_DIR / "instance.lock").open("a+b")
        self.owned = False

    def acquire(self):
        try:
            if os.name == "nt":
                import msvcrt

                self.handle.seek(0)
                if not self.handle.read(1):
                    self.handle.write(b"0")
                    self.handle.flush()
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.owned = True
            return True
        except OSError:
            return False

    def publish(self, server):
        write_json(DATA_DIR / "instance.json", {"port": server.server_port, "token": server.token})

    def activate(self):
        info = json.loads((DATA_DIR / "instance.json").read_text())
        port = int(info["port"])
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/activate",
            data=b"{}",
            headers={"X-Perch-Token": info["token"], "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status == 200

    def close(self):
        if self.owned:
            (DATA_DIR / "instance.json").unlink(missing_ok=True)
        self.handle.close()
