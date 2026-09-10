"""Single-instance ownership and shell executable discovery for GUI launches."""

from __future__ import annotations
import json
import os
import subprocess
import sys
import shlex
from pathlib import Path
import urllib.request
from ..storage import DATA_DIR, write_json


def open_cli_session(scanner, agent_id):
    """Resume a known recording in an external terminal owned by the harness."""
    from uuid import uuid4
    from ..term import external_process_env

    agent = next((a for a in scanner.scan()["agents"] if a["id"] == agent_id), None)
    adapter = agent and next((a for a in scanner.adapters if a.id == agent["harness"]), None)
    if not adapter or not adapter.enabled or not adapter.resume:
        raise ValueError("This session has no native CLI resume route")
    argv = adapter.spawn_argv(agent_id.partition(":")[2])
    if not argv:
        raise ValueError("The harness CLI is unavailable")
    cwd = agent.get("cwd") or str(Path.home())
    if not Path(cwd).is_dir():
        raise ValueError("The recorded project folder is unavailable")
    if sys.platform == "darwin":
        folder = Path(scanner.config_dir or DATA_DIR) / "launchers"
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        script = folder / (str(uuid4()) + ".command")
        content = "#!/bin/sh\n" + "rm -f -- " + shlex.quote(str(script)) + "\n"
        content += "export PATH=" + shlex.quote(os.environ.get("PATH", "/usr/bin:/bin")) + "\n"
        content += "cd -- " + shlex.quote(cwd) + " || exit 1\nexec " + shlex.join(argv) + "\n"
        script.write_text(content, encoding="utf-8")
        script.chmod(0o700)
        try:
            _mac_open("-a", "Terminal", str(script))
        except OSError:
            script.unlink(missing_ok=True)
            raise
    elif sys.platform == "win32":
        with external_process_env() as env:
            subprocess.Popen(argv, cwd=cwd, env=env, creationflags=0x00000010)
    else:
        raise ValueError("Native CLI opening is supported on macOS and Windows")


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
