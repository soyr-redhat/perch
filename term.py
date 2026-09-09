"""Perch terminal sessions: harness CLIs running on real PTYs.

Windows uses ConPTY via pywinpty; POSIX uses the stdlib pty module. Each
session pumps PTY output into subscriber queues; late attachers get a capped
backlog replay so a reloaded browser redraws the screen.
"""

from __future__ import annotations

import os
import platform
import queue
import subprocess
import threading
import time
import uuid
import signal

IS_WIN = platform.system() == "Windows"
BACKLOG_CAP = 96 * 1024


def terminate_process(proc, timeout=2):
    """Stop a Perch-owned process tree and reap its leader."""
    if proc.poll() is not None:
        return
    try:
        if IS_WIN:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
                timeout=timeout,
                creationflags=0x08000000,
            )
        else:
            os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        try:
            if IS_WIN:
                proc.kill()
            else:
                os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=timeout)
        except (OSError, subprocess.TimeoutExpired):
            pass


class Pty:
    def __init__(self, argv: list[str], cwd: str | None, cols: int = 120, rows: int = 38):
        env = dict(os.environ, TERM="xterm-256color", COLORTERM="truecolor")
        if IS_WIN:
            from winpty import PtyProcess

            self._p = PtyProcess.spawn(argv, cwd=cwd or None, dimensions=(rows, cols), env=env)
            self._kind = "win"
        else:
            import fcntl
            import pty
            import struct
            import termios

            master, slave = pty.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
            self._p = subprocess.Popen(
                argv,
                cwd=cwd or None,
                env=env,
                stdin=slave,
                stdout=slave,
                stderr=slave,
                close_fds=True,
                start_new_session=True,
            )
            os.close(slave)
            self._m = master
            self._kind = "posix"

    def read(self) -> bytes:
        if self._kind == "win":
            return self._p.read().encode("utf-8", "replace")
        return os.read(self._m, 16384)

    def write(self, data: bytes) -> None:
        if self._kind == "win":
            self._p.write(data.decode("utf-8", "replace"))
        else:
            while data:
                written = os.write(self._m, data)
                data = data[written:]

    def resize(self, cols: int, rows: int) -> None:
        cols, rows = max(2, min(cols, 500)), max(2, min(rows, 300))
        try:
            if self._kind == "win":
                self._p.setwinsize(rows, cols)
            else:
                import fcntl
                import struct
                import termios

                fcntl.ioctl(self._m, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        except Exception:
            pass

    def alive(self) -> bool:
        return self._p.isalive() if self._kind == "win" else self._p.poll() is None

    def exit_code(self):
        if self._kind == "win":
            return None if self.alive() else getattr(self._p, "exitstatus", None)
        return self._p.poll()

    def close(self):
        if self._kind == "posix" and self._m is not None:
            try:
                os.close(self._m)
            except OSError:
                pass
            self._m = None

    def kill(self) -> None:
        if self._kind == "win":
            try:
                self._p.terminate(force=True)
            except OSError:
                pass
        else:
            terminate_process(self._p)
        self.close()


class TermSession:
    def __init__(self, harness: str, name: str, color: str, cwd: str, argv: list[str]):
        self.id = uuid.uuid4().hex[:10]
        self.harness = harness
        self.name = name
        self.color = color
        self.cwd = cwd
        self.argv = argv
        self.started = time.time()
        self.pty = Pty(argv, cwd)
        self._subs: list[queue.Queue] = []
        self._backlog = bytearray()
        self._lock = threading.Lock()
        self.died_at: float | None = None
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        try:
            while True:
                chunk = self.pty.read()
                if not chunk:
                    break
                with self._lock:
                    self._backlog.extend(chunk)
                    if len(self._backlog) > BACKLOG_CAP:
                        del self._backlog[:-BACKLOG_CAP]
                    subs = list(self._subs)
                for q in subs:
                    try:
                        q.put_nowait(chunk)
                    except queue.Full:
                        # Terminal output cannot be dropped: explicitly end the slow stream.
                        self.unsubscribe(q)
                        while not q.empty():
                            try:
                                q.get_nowait()
                            except queue.Empty:
                                break
                        q.put_nowait(None)
        except (EOFError, OSError):
            pass
        finally:
            self.died_at = time.time()
            self.pty.close()
            with self._lock:
                subs = list(self._subs)
            for q in subs:
                try:
                    q.put_nowait(None)  # EOF sentinel
                except queue.Full:
                    q.get_nowait()
                    q.put_nowait(None)

    def subscribe(self) -> queue.Queue:
        """New subscriber: backlog replay is queued before live chunks."""
        q: queue.Queue = queue.Queue(maxsize=512)
        with self._lock:
            if self._backlog:
                q.put(bytes(self._backlog))
            self._subs.append(q)
            if self.died_at is not None:
                q.put(None)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def write(self, data: bytes) -> None:
        if self.alive():
            try:
                self.pty.write(data)
            except Exception:
                pass

    def kill(self) -> None:
        self.pty.kill()

    def resize(self, cols: int, rows: int) -> None:
        self.pty.resize(cols, rows)

    def alive(self) -> bool:
        return self.pty.alive()

    def info(self) -> dict:
        alive = self.alive()
        return {
            "id": self.id,
            "harness": self.harness,
            "name": self.name,
            "color": self.color,
            "cwd": self.cwd,
            "alive": alive,
            "started": self.started,
            "exit": None if alive else self.pty.exit_code(),
        }


class TermRegistry:
    def __init__(self):
        self._terms: dict[str, TermSession] = {}
        self._lock = threading.Lock()

    def spawn(self, harness: str, name: str, color: str, cwd: str, argv: list[str]) -> TermSession:
        term = TermSession(harness, name, color, cwd, argv)
        with self._lock:
            self._terms[term.id] = term
        return term

    def get(self, term_id: str) -> TermSession | None:
        with self._lock:
            return self._terms.get(term_id)

    def remove(self, term_id: str) -> bool:
        with self._lock:
            term = self._terms.pop(term_id, None)
        if term:
            term.kill()
            return True
        return False

    def list(self) -> list[dict]:
        with self._lock:
            terms = list(self._terms.values())
        return [t.info() for t in terms]

    def shutdown(self):
        with self._lock:
            terms = list(self._terms.values())
            self._terms.clear()
        for term in terms:
            term.kill()
