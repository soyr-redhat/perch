"""Desktop installation repair, with ordinary launchers and recoverable backups."""

import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
from uuid import uuid4

from ..storage import atomic_write, sync_lock

MARKER = "# Perch managed launcher"


def bundle():
    if not getattr(sys, "frozen", False):
        return None
    executable = Path(sys.executable).resolve()
    return executable.parents[2] if sys.platform == "darwin" else executable.parent


def launcher(app):
    return f'#!/bin/sh\n{MARKER}\nexec {shlex.quote(str(app / "Contents/MacOS/perch-cli"))} "$@"\n'


def owned(path, app):
    if path.is_symlink():
        return Path(os.path.realpath(path)).name == "perch-cli" and ".app/Contents/MacOS/" in os.path.realpath(path)
    if not path.is_file() or path.stat().st_size > 8192:
        return False
    text = path.read_text(encoding="utf-8", errors="replace")
    return MARKER in text or text == launcher(app) or text == launcher(app).replace(MARKER + "\n", "")


def status(app=None, home=None):
    app, home = app or bundle(), Path(home or Path.home())
    if app is None:
        return {"available": False, "reason": "Installation controls are available in the packaged desktop app."}
    app = Path(app)
    if sys.platform == "darwin":
        target = home / "Applications/Perch.app"
        shortcuts = []
        for name in ("perch", "perch-cli"):
            path = home / ".local/bin" / name
            exists = os.path.lexists(path)
            ready = exists and not path.is_symlink() and path.is_file() and path.stat().st_size <= 8192 and path.read_bytes() == launcher(target).encode() and os.access(path, os.X_OK)
            shortcuts.append({"name": name, "status": "ready" if ready else "repair" if not exists or owned(path, target) else "conflict"})
        return {"available": True, "app": str(app), "target": str(target), "installed": (target / "Contents/MacOS/perch-cli").is_file(), "shortcuts": shortcuts}
    if sys.platform == "win32":
        return {"available": True, "app": str(app), "target": str(app), "installed": (app / "Perch.exe").is_file(),
                "shortcuts": [{"name": "perch-cli.exe", "status": "ready" if (app / "perch-cli.exe").is_file() else "conflict"}]}
    return {"available": False, "reason": "Installation repair supports macOS and Windows."}


def repair(app=None, home=None, cli=True):
    app, home = app or bundle(), Path(home or Path.home())
    info = status(app, home)
    if not info["available"]:
        raise ValueError(info["reason"])
    if cli and any(s["status"] == "conflict" for s in info["shortcuts"]):
        raise ValueError("An unrelated or missing executable uses a Perch shortcut name. It has been left unchanged.")
    if sys.platform == "win32":
        if not info["installed"]:
            raise ValueError("The installed application is incomplete. Reinstall using the Windows installer.")
        if cli:
            import winreg

            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                try:
                    current, kind = winreg.QueryValueEx(key, "Path")
                except FileNotFoundError:
                    current, kind = "", winreg.REG_EXPAND_SZ
                parts = [p for p in current.split(";") if p]
                if os.path.normcase(str(app)) not in [os.path.normcase(p) for p in parts]:
                    winreg.SetValueEx(key, "Path", 0, kind, ";".join([*parts, str(app)]))
        return {**info, "repaired": True, "backups": []}
    app, target = Path(app), Path(info["target"])
    backups, changed, stage = [], [], None
    with sync_lock(home / ".perch/install"):
        try:
            if app.resolve() != target.resolve():
                target.parent.mkdir(parents=True, exist_ok=True)
                stage = target.with_name(".Perch.install-" + uuid4().hex + ".app")
                shutil.copytree(app, stage, symlinks=True)
                # A bundle that cannot start its CLI must not replace the installed app.
                from ..term import external_process_env

                with external_process_env() as env:
                    subprocess.run([str(stage / "Contents/MacOS/perch-cli"), "--version"], check=True, capture_output=True, timeout=15, env=env)
                old = target.with_name(".Perch.previous-" + uuid4().hex + ".app") if target.exists() else None
                if old:
                    target.rename(old)
                    backups.append(str(old))
                changed.append((target, old))
                stage.rename(target)
            if cli:
                folder = home / ".local/bin"
                folder.mkdir(parents=True, exist_ok=True)
                for name in ("perch", "perch-cli"):
                    path = folder / name
                    if os.path.lexists(path) and not owned(path, target):
                        raise ValueError(f"{name} changed during repair; left unchanged")
                    if path.is_file() and not path.is_symlink() and path.read_bytes() == launcher(target).encode() and os.access(path, os.X_OK):
                        continue
                    old = folder / ("." + name + ".previous-" + uuid4().hex) if os.path.lexists(path) else None
                    if old:
                        path.rename(old)
                        backups.append(str(old))
                    changed.append((path, old))
                    atomic_write(path, launcher(target))
                    path.chmod(0o755)
        except Exception:
            for path, old in reversed(changed):
                if path.is_symlink() or path.is_file():
                    path.unlink()
                elif path.is_dir():
                    shutil.rmtree(path)
                if old:
                    old.rename(path)
            raise
        finally:
            if stage and stage.exists():
                shutil.rmtree(stage)
    return {**status(target, home), "repaired": True, "backups": backups}
