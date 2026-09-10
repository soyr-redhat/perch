"""Private local storage, atomic replacement and cross-process sync exclusion."""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import tempfile
import threading

DATA_DIR = Path(os.environ.get("PERCH_DATA_DIR", "~/.perch")).expanduser()
_LOCK = threading.RLock()


def atomic_write(path: str | Path, text: str, expected: bytes | None = None) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as out:
            out.write(text)
            out.flush()
            os.fsync(out.fileno())
        if expected is not None:
            current = path.read_bytes() if path.exists() else b""
            if current != expected:
                raise ValueError(f"{path.name} changed during sync; preview again")
        if path.exists():
            os.chmod(tmp, path.stat().st_mode & 0o777)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def write_json(path: str | Path, data: dict) -> None:
    atomic_write(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


@contextlib.contextmanager
def sync_lock(directory: str | Path = DATA_DIR):
    with _LOCK:
        root = Path(directory)
        root.mkdir(parents=True, exist_ok=True)
        with (root / "sync.lock").open("a+b") as handle:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                if not handle.read(1):
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError as exc:
                    raise ValueError("Another Perch sync is running") from exc
            else:
                import fcntl

                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    raise ValueError("Another Perch sync is running") from exc
            try:
                yield
            finally:
                if os.name == "nt":
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle, fcntl.LOCK_UN)
