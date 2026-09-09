"""Perch scanner: harness-agnostic detection of live coding agents.

An *adapter* knows where one harness keeps its session recordings and how to
read them. Adding a harness means either:

  1. dropping a declarative entry in sources.json (JSONL/JSON session files), or
  2. subclassing Adapter here for anything exotic.

The scanner polls session files every couple of seconds, parses them
*incrementally* (only bytes appended since the last pass), and folds everything
into one snapshot the UI renders.
"""

from __future__ import annotations

import glob
import json
import os
import platform
import shutil
import subprocess
import time
import threading
import re
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

WORKING_AGE_S = 8  # writes within this window => actively working
BUSY_HINT_GRACE_S = 120  # in-flight hint keeps "working" this long
WAITING_AGE_S = 15 * 60  # beyond this, session drops from LIVE to QUIET


# --------------------------------------------------------------------------
# model


@dataclass
class TailEvent:
    who: str  # user | assistant | tool
    text: str
    ts: Optional[str] = None


class PromptBuffer(deque):
    def __init__(self):
        super().__init__(maxlen=150)
        self.count = 0
        self.offset = 0

    def append(self, item):
        super().append({**item, "index": self.count, "offset": self.offset})
        self.count += 1


@dataclass
class FileState:
    """Incremental parse state for one session file."""

    path: str
    offset: int = 0
    session_id: Optional[str] = None
    cwd: Optional[str] = None
    started: Optional[str] = None
    title: Optional[str] = None
    model: Optional[str] = None
    tokens: Optional[int] = None
    last_ts: Optional[str] = None
    hint: str = "unknown"  # busy | done | unknown
    tail: deque = field(default_factory=lambda: deque(maxlen=60))
    prompts: PromptBuffer = field(default_factory=PromptBuffer)
    mtime: float = -1
    size: int = -1
    identity: tuple = ()
    errors: int = 0
    harness: str = ""


def read_history(
    adapter: "Adapter",
    path: str,
    pidx: int,
    span: int = 100,
    offset: int | None = None,
    total: int | None = None,
) -> dict:
    """Stream one prompt's events with bounded memory; use a known byte offset when available."""
    st = FileState(path=path)
    st.tail = deque(maxlen=span + 1)
    prompt = pidx - 1 if offset is not None else -1
    selected = []
    if adapter.format == "json":
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        records = dig(doc, adapter.records_path) if adapter.records_path else doc
        iterator = ((rec, 0) for rec in records if isinstance(rec, dict))
    else:
        iterator = _iter_json_lines(path, offset or 0)
    for rec, _ in iterator:
        st.tail.clear()
        try:
            adapter.consume(rec, st)
        except (ValueError, TypeError, AttributeError, KeyError):
            continue
        for event in st.tail:
            if event.who == "user":
                prompt += 1
                if prompt > pidx:
                    return {
                        "events": selected,
                        "prompt": pidx,
                        "of": total or prompt + 1,
                        "truncated": len(selected) >= span,
                    }
            if prompt == pidx and len(selected) < span:
                selected.append(vars(event))
        if len(selected) >= span:
            break
    return {"events": selected, "prompt": pidx, "of": total or prompt + 1, "truncated": len(selected) >= span}


@dataclass
class Agent:
    id: str
    harness: str
    title: str
    cwd: Optional[str]
    file: str
    started: Optional[str]
    updated: Optional[str]
    mtime: float
    model: Optional[str]
    tokens: Optional[int]
    state: str  # working | waiting | quiet
    tail: list
    prompts: list = field(default_factory=list)


# --------------------------------------------------------------------------
# helpers


def _iter_json_lines(path: str, offset: int) -> Iterable[tuple[dict, int]]:
    """Consume complete lines only. Malformed records advance rather than poisoning the scan."""
    with open(path, "rb") as fh:
        fh.seek(offset)
        while True:
            line = fh.readline(4 * 1024 * 1024)
            if not line:
                break
            if not line.endswith(b"\n"):
                if len(line) < 4 * 1024 * 1024:
                    break  # an unfinished append must be retried
                while line and not line.endswith(b"\n"):
                    line = fh.readline(4 * 1024 * 1024)
                yield {"_perch_error": "Record exceeds 4 MB"}, fh.tell()
                continue
            try:
                rec = json.loads(line)
                if not isinstance(rec, dict):
                    raise ValueError("Expected an object")
            except (ValueError, UnicodeDecodeError):
                rec = {"_perch_error": "Invalid JSON record"}
            yield rec, fh.tell()


def _max_ts(a: Optional[str], b: Optional[str]) -> Optional[str]:
    if a and b:
        return max(a, b)
    return a or b


def _first_text(content: Any, user=False) -> Optional[str]:
    """Collect one message's text blocks without splitting it into extra prompts."""
    blocks = [content] if isinstance(content, str) else [
        b.get("text") for b in content
        if isinstance(b, dict) and b.get("type") in ("text", "input_text", "output_text")
    ] if isinstance(content, list) else []
    texts = [_clean_user_text(t) if user else t for t in blocks if isinstance(t, str)]
    return "\n\n".join(t for t in texts if t) or None


def _clean_user_text(text: Optional[str]) -> Optional[str]:
    """Drop environment/context scaffolding that looks like a user message."""
    if not isinstance(text, str) or not text:
        return None
    # Strip only recognized harness scaffolding, preserving HTML/XML requests and
    # actual user text following a context block in the same message.
    tags = "environment_context|permissions instructions|collaboration_mode|in-app-browser-context"
    cleaned = re.sub(rf"<({tags})(?:\s[^>]*)?>.*?</\1>", "", text, flags=re.DOTALL)
    return cleaned.strip() or None


def _truncate(text: str, limit: int = 8000) -> str:
    text = " ".join(text.split()) if limit < 100 else text.strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def dig(obj: Any, path: str) -> Any:
    """Tiny dotted-path getter for declarative adapters: 'a.b.c', 'a.b[].c'."""
    if not path:
        return obj
    cur = obj
    for part in path.split("."):
        if cur is None:
            return None
        if part.endswith("[]"):
            key = part[:-2]
            seq = cur.get(key) if isinstance(cur, dict) else None
            if not isinstance(seq, list):
                return None
            rest = ".".join(path.split(".")[path.split(".").index(part) + 1 :])
            for item in seq:
                val = dig(item, rest) if rest else item
                if val:
                    return val
            return None
        cur = cur.get(part) if isinstance(cur, dict) else None
    return cur


# --------------------------------------------------------------------------
# adapters


def _which(name: str, *fallbacks: str) -> Optional[list[str]]:
    """Resolve an executable to an argv prefix; .cmd/.bat need cmd.exe to spawn."""
    path = shutil.which(name)
    if not path:
        for cand in fallbacks:
            cand = os.path.expanduser(cand)
            if os.path.isfile(cand):
                path = cand
                break
    if not path:
        return None
    if platform.system() == "Windows" and path.lower().endswith((".cmd", ".bat")):
        # npm shims are shell scripts. Resolve their JS entry point so prompts and
        # session IDs never pass through cmd.exe expansion.
        wrapper = Path(path)
        text = wrapper.read_text(encoding="utf-8", errors="replace")
        match = re.search(r'%dp0%[\\/]([^"\r\n]+\.(?:m?js))', text, re.IGNORECASE)
        node = shutil.which("node")
        if match and node:
            entry = wrapper.parent / match.group(1).replace("\\", os.sep)
            if entry.is_file():
                return [node, str(entry.resolve())]
        return None
    return [path]


class Adapter:
    id = "base"
    name = "Base"
    color = "#0a84ff"
    patterns: list[str] = []
    format = "jsonl"
    enabled = True
    cmd: Optional[list[str]] = None  # argv prefix to launch the harness CLI
    resume: Optional[list[str]] = None  # appended to cmd; "{session}" substituted
    message: Optional[list[str]] = None  # headless one-shot; "{session}"/"{text}" substituted

    def spawn_argv(self, session: Optional[str] = None) -> Optional[list[str]]:
        if not self.cmd or (session and session.startswith("-")):
            return None
        if session and self.resume:
            return self.cmd + [p.replace("{session}", session) for p in self.resume]
        return list(self.cmd)

    def message_argv(self, session: str, text: str) -> Optional[list[str]]:
        """Argv to deliver one message into an existing session, non-interactively."""
        if not self.cmd or not self.message or not session or session.startswith("-"):
            return None
        return self.cmd + [p.replace("{session}", session).replace("{text}", text) for p in self.message]

    def session_files(self) -> Iterable[str]:
        seen = set()
        for pat in self.patterns:
            for path in glob.glob(os.path.expanduser(pat), recursive=True):
                if path not in seen and os.path.isfile(path):
                    seen.add(path)
                    yield path

    def consume(self, rec: dict, st: FileState) -> None:
        raise NotImplementedError

    def fallback_id(self, path: str) -> str:
        return Path(path).stem


class OmpAdapter(Adapter):
    id = "omp"
    name = "omp"
    color = "#bf5af2"
    patterns = ["~/.omp/agent/sessions/*/*.jsonl"]

    def __init__(self):
        self.cmd = _which("omp", "~/AppData/Local/omp/omp.exe")
        self.resume = ["--resume", "{session}"]
        self.message = ["--print", "--resume", "{session}", "--", "{text}"]

    def consume(self, rec: dict, st: FileState) -> None:
        rtype = rec.get("type")
        ts = rec.get("timestamp") or rec.get("updatedAt")
        st.last_ts = _max_ts(st.last_ts, ts)

        if rtype == "title":
            if rec.get("title"):
                st.title = rec["title"]
        elif rtype == "session":
            st.session_id = rec.get("id") or st.session_id
            st.cwd = rec.get("cwd") or st.cwd
            st.started = st.started or rec.get("timestamp")
        elif rtype == "message":
            msg = rec.get("message") or {}
            role = msg.get("role")
            blocks = msg.get("content") or []
            if role == "user":
                text = _first_text(blocks, user=True)
                if text:
                    st.tail.append(TailEvent("user", _truncate(text), ts))
                    st.prompts.append({"text": _truncate(text, 70), "ts": ts})
                    if not st.title:
                        st.title = _truncate(text, 70)
                st.hint = "busy"
            elif role == "assistant":
                if msg.get("model"):
                    st.model = msg["model"]
                usage = msg.get("usage") or {}
                if usage.get("totalTokens"):
                    st.tokens = usage["totalTokens"]
                for block in blocks if isinstance(blocks, list) else []:
                    btype = block.get("type")
                    if btype == "text" and block.get("text"):
                        st.tail.append(TailEvent("assistant", _truncate(block["text"]), ts))
                    elif btype == "thinking" and block.get("thinking"):
                        st.tail.append(TailEvent("think", _truncate(block["thinking"], 260), ts))
                    elif btype == "toolCall":
                        st.tail.append(TailEvent("tool", block.get("name") or "tool", ts))
                st.hint = "busy" if msg.get("stopReason") == "toolUse" else "done"


class ClaudeAdapter(Adapter):
    id = "claude"
    name = "Claude Code"
    color = "#d97757"
    patterns = ["~/.claude/projects/*/*.jsonl"]

    def __init__(self):
        self.cmd = _which("claude", "~/AppData/Roaming/npm/claude.cmd", "~/.local/bin/claude")
        self.resume = ["--resume", "{session}"]
        self.message = ["-p", "--resume", "{session}", "--", "{text}"]

    def consume(self, rec: dict, st: FileState) -> None:
        rtype = rec.get("type")
        ts = rec.get("timestamp")
        st.last_ts = _max_ts(st.last_ts, ts)
        st.session_id = st.session_id or rec.get("sessionId")
        st.cwd = st.cwd or rec.get("cwd")
        st.started = st.started or ts

        if rtype == "summary" and rec.get("summary"):
            st.title = rec["summary"]
            return

        msg = rec.get("message")
        if not isinstance(msg, dict):
            return
        content = msg.get("content")

        if rtype == "user":
            text = _first_text(content, user=True)
            if text:
                st.tail.append(TailEvent("user", _truncate(text), ts))
                st.prompts.append({"text": _truncate(text, 70), "ts": ts})
                if not st.title:
                    st.title = _truncate(text, 70)
                st.hint = "busy"
        elif rtype == "assistant":
            if msg.get("model"):
                st.model = msg["model"]
            usage = msg.get("usage") or {}
            total = (usage.get("input_tokens") or 0) + (usage.get("output_tokens") or 0)
            if total:
                st.tokens = (st.tokens or 0) + total
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text" and block.get("text"):
                        st.tail.append(TailEvent("assistant", _truncate(block["text"]), ts))
                    elif block.get("type") == "thinking" and block.get("thinking"):
                        st.tail.append(TailEvent("think", _truncate(block["thinking"], 260), ts))
                    elif block.get("type") == "tool_use":
                        st.tail.append(TailEvent("tool", block.get("name") or "tool", ts))
            st.hint = "done"


class CodexAdapter(Adapter):
    id = "codex"
    name = "Codex"
    color = "#10a37f"
    patterns = ["~/.codex/sessions/**/*.jsonl"]

    def __init__(self):
        self.cmd = _which("codex", "~/AppData/Local/Programs/OpenAI/Codex/bin/codex.exe")
        self.patterns = [
            str(Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser() / "sessions/**/*.jsonl")
        ]
        self.resume = ["resume", "{session}"]
        self.message = ["exec", "resume", "{session}", "--", "{text}"]

    def consume(self, rec: dict, st: FileState) -> None:
        rtype = rec.get("type")
        ts = rec.get("timestamp")
        st.last_ts = _max_ts(st.last_ts, ts)
        payload = rec.get("payload") or {}

        if rtype == "session_meta":
            st.session_id = payload.get("session_id") or payload.get("id") or st.session_id
            st.cwd = payload.get("cwd") or st.cwd
            st.started = st.started or payload.get("timestamp")
        elif rtype == "turn_context":
            if payload.get("model"):
                st.model = payload["model"]
        elif rtype == "response_item":
            ptype = payload.get("type") or ""
            if ptype == "message":
                role = payload.get("role")
                if role in ("user", "assistant"):
                    text = _first_text(payload.get("content"), user=role == "user")
                    if text:
                        st.tail.append(TailEvent(role, _truncate(text), ts))
                        if role == "user":
                            st.hint = "busy"
                            st.prompts.append({"text": _truncate(text, 70), "ts": ts})
                            if not st.title:
                                st.title = _truncate(text, 70)
                        else:
                            st.hint = "done"
            elif ptype == "reasoning":
                for s in payload.get("summary") or []:
                    if isinstance(s, dict) and s.get("text"):
                        st.tail.append(TailEvent("think", _truncate(s["text"], 260), ts))
            elif "call" in ptype and not ptype.endswith("output"):
                name = payload.get("name") or ptype
                st.tail.append(TailEvent("tool", name, ts))
        elif rtype == "event_msg":
            ptype = payload.get("type")
            if ptype == "token_count":
                total = (payload.get("info") or {}).get("total_token_usage") or {}
                if total.get("total_tokens"):
                    st.tokens = total["total_tokens"]
            elif ptype == "task_complete":
                st.hint = "done"


class DeclarativeAdapter(Adapter):
    """A harness described entirely by a sources.json entry.

    Config shape:
      {"id","name","color", "patterns": [...], "format": "jsonl"|"json",
       "map": {"id","cwd","ts","role","text","model"},
       "records": "messages",                 # json format only
       "roles": {"user": ["user","human"], "assistant": ["assistant","ai"]}}
    """

    def __init__(self, cfg: dict):
        if not isinstance(cfg, dict) or not isinstance(cfg.get("id"), str) or not re.fullmatch(r"[a-zA-Z0-9_-]+", cfg["id"]):
            raise ValueError("Source needs a valid id")
        for key in ("patterns", "cmd", "resume", "message"):
            value = cfg.get(key)
            if value is not None and (not isinstance(value, list) or not all(isinstance(x, str) for x in value)):
                raise ValueError(f"{key} must be a list of strings")
        if cfg.get("format", "jsonl") not in ("json", "jsonl"):
            raise ValueError("Source format must be json or jsonl")
        if not isinstance(cfg.get("map", {}), dict) or not all(isinstance(v, str) for v in cfg.get("map", {}).values()):
            raise ValueError("Source map must contain paths")
        if not isinstance(cfg.get("roles", {}), dict):
            raise ValueError("Source roles must be an object")
        self.id = cfg["id"]
        self.name = cfg.get("name", self.id)
        self.color = cfg.get("color", "#0a84ff")
        self.patterns = cfg.get("patterns", [])
        self.format = cfg.get("format", "jsonl")
        self.records_path = cfg.get("records", "")
        self.map = cfg.get("map", {})
        self.roles = cfg.get("roles", {})
        self.cmd = cfg.get("cmd")
        self.resume = cfg.get("resume")
        self.message = cfg.get("message")

    def _role_of(self, value: Any) -> Optional[str]:
        value = str(value)
        for canonical, aliases in self.roles.items():
            if value in aliases:
                return canonical
        return value if value in ("user", "assistant") else None

    def consume(self, rec: dict, st: FileState) -> None:
        m = self.map
        ts = dig(rec, m.get("ts", ""))
        st.last_ts = _max_ts(st.last_ts, ts if isinstance(ts, str) else None)
        identifier = dig(rec, m["id"]) if m.get("id") else None
        cwd = dig(rec, m["cwd"]) if m.get("cwd") else None
        st.session_id = st.session_id or (identifier if isinstance(identifier, str) else None)
        st.cwd = st.cwd or (cwd if isinstance(cwd, str) else None)
        st.started = st.started or (ts if isinstance(ts, str) else None)
        model = dig(rec, m["model"]) if m.get("model") else None
        if isinstance(model, str) and model:
            st.model = model
        role = self._role_of(dig(rec, m.get("role", "")))
        text = dig(rec, m.get("text", ""))
        if role and isinstance(text, str) and text.strip():
            text = _clean_user_text(text)
            if text:
                st.tail.append(TailEvent(role, _truncate(text), ts if isinstance(ts, str) else None))
                if role == "user":
                    st.prompts.append(
                        {"text": _truncate(text, 70), "ts": ts if isinstance(ts, str) else None}
                    )
                if role == "user" and not st.title:
                    st.title = _truncate(text, 70)
                st.hint = "busy" if role == "user" else "done"


def load_declarative(config_path: str) -> list[DeclarativeAdapter]:
    try:
        with open(config_path, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(cfg, dict) or not isinstance(cfg.get("sources", []), list):
        return []
    adapters = []
    for entry in cfg.get("sources", []):
        try:
            adapters.append(DeclarativeAdapter(entry))
        except (KeyError, TypeError, ValueError):
            continue
    return adapters


# --------------------------------------------------------------------------
# process scan (best effort, corroborating signal)

_PROC_NAMES = {"omp": "omp", "codex": "codex", "claude": "claude"}


def scan_processes() -> dict[str, int]:
    try:
        if platform.system() == "Windows":
            script = (
                "Get-CimInstance Win32_Process | Where-Object { "
                "$_.Name -in @('omp.exe','codex.exe','claude.exe') -or "
                "($_.Name -eq 'node.exe' -and $_.CommandLine -match 'claude') } | "
                "Select-Object -ExpandProperty Name | ConvertTo-Json -Compress"
            )
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command", script],
                capture_output=True,
                text=True,
                timeout=15,
            ).stdout.strip()
            if not out:
                return {}
            data = json.loads(out)
            names = [data] if isinstance(data, str) else data
            counts: dict[str, int] = {}
            for name in names:
                base = name.lower().removesuffix(".exe")
                harness = "claude" if base == "node" else _PROC_NAMES.get(base)
                if harness:
                    counts[harness] = counts.get(harness, 0) + 1
            return counts
        out = subprocess.run(
            ["ps", "-eo", "comm=,args="],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout
        counts = {}
        for line in out.splitlines():
            parts = line.split(None, 1)
            if not parts:
                continue
            base = os.path.basename(parts[0])
            harness = _PROC_NAMES.get(base)
            if not harness and base == "node" and len(parts) > 1 and "claude" in parts[1]:
                harness = "claude"
            if harness:
                counts[harness] = counts.get(harness, 0) + 1
        return counts
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return {}


# --------------------------------------------------------------------------
# scanner


class Scanner:
    def __init__(self, quiet_days: float = 7.0, config_dir: Optional[str] = None):
        self.adapters: list[Adapter] = [OmpAdapter(), ClaudeAdapter(), CodexAdapter()]
        if config_dir:
            self.adapters += load_declarative(os.path.join(config_dir, "sources.json"))
        self.config_dir = config_dir
        self.quiet_s = quiet_days * 86400
        self._states: dict[str, FileState] = {}
        self._procs: dict[str, int] = {}
        self._procs_at = 0.0
        self._paths = {}
        self._discovered_at = 0.0
        self.changed = threading.Event()
        self._lock = threading.RLock()
        self._observer = None

    def apply_settings(self, cfg: dict) -> None:
        self.quiet_s = cfg.get("watching", {}).get("quietDays", 7) * 86400
        self.changed.set()
        disabled = set(cfg.get("harnesses", {}).get("disabled", []))
        for adapter in self.adapters:
            adapter.enabled = adapter.id not in disabled

    def start_watching(self):
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer
        from watchdog.observers.polling import PollingObserver

        scanner = self

        class Events(FileSystemEventHandler):
            def on_any_event(self, event):
                if event.event_type in ("opened", "closed_no_write", "closed"):
                    return
                scanner.changed.set()

        # FSEvents can abort the interpreter in restricted macOS app contexts.
        # Polling watches metadata only; transcript parsing remains incremental.
        observer = PollingObserver(timeout=2) if platform.system() == "Darwin" else Observer()
        roots = set()
        for adapter in self.adapters:
            for pattern in adapter.patterns:
                prefix = os.path.expanduser(pattern).split("*")[0]
                root = Path(prefix)
                if not root.is_dir():
                    root = root.parent
                if root.is_dir():
                    roots.add(str(root))
        for root in roots:
            observer.schedule(Events(), root, recursive=True)
        try:
            observer.start()
        except (OSError, RuntimeError):
            observer.stop()
            if observer.is_alive():
                observer.join(timeout=3)
            observer = PollingObserver(timeout=2)
            for root in roots:
                observer.schedule(Events(), root, recursive=True)
            observer.start()
        self._observer = observer

    def close(self):
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=3)

    def _parse_file(self, adapter: Adapter, path: str, mtime: float, size: int) -> FileState:
        st = self._states.get(path)
        if st and st.mtime == mtime and st.size == size:
            return st
        if (
            st is None
            or size < st.offset
            or (size == st.size and st.mtime != mtime)
            or adapter.format == "json"
        ):
            st = FileState(path=path)
            self._states[path] = st
        if adapter.format == "json":
            with open(path, "r", encoding="utf-8") as fh:
                doc = json.load(fh)
            records = dig(doc, adapter.records_path) if adapter.records_path else doc
            if not isinstance(records, list):
                raise ValueError("Session records must be a list")
            iterator = ((rec, size) for rec in records)
        else:
            iterator = _iter_json_lines(path, st.offset)
        for rec, end in iterator:
            st.prompts.offset = st.offset
            try:
                if not isinstance(rec, dict) or "_perch_error" in rec:
                    raise ValueError("Invalid record")
                adapter.consume(rec, st)
            except (ValueError, TypeError, AttributeError, KeyError):
                st.errors += 1
            st.offset = end
        st.harness = adapter.id
        st.mtime, st.size = mtime, size
        return st

    def history(self, agent_id, pidx):
        with self._lock:
            for adapter in self.adapters:
                for path, st in self._states.items():
                    if st.harness != adapter.id:
                        continue
                    if f"{adapter.id}:{st.session_id or adapter.fallback_id(path)}" != agent_id:
                        continue
                    if not 0 <= pidx < st.prompts.count:
                        raise ValueError("Prompt index is outside this session")
                    prompt = next((p for p in st.prompts if p["index"] == pidx), None)
                    return read_history(
                        adapter,
                        path,
                        pidx,
                        offset=prompt["offset"] if prompt and adapter.format != "json" else None,
                        total=st.prompts.count,
                    )
        raise ValueError("Session is no longer available")

    def scan(self) -> dict:
        with self._lock:
            return self._scan()

    def _scan(self) -> dict:
        now = time.time()
        if now - self._procs_at > 10:
            self._procs = scan_processes()
            self._procs_at = now

        agents: list[Agent] = []
        errors = []
        retained = set()
        if self.changed.is_set() or now - self._discovered_at > 30:
            self.changed.clear()
            self._paths = {a.id: list(a.session_files()) for a in self.adapters if a.enabled}
            self._discovered_at = now
        for adapter in self.adapters:
            if not adapter.enabled:
                continue
            for path in self._paths.get(adapter.id, []):
                try:
                    stat = os.stat(path)
                except OSError:
                    continue
                age = now - stat.st_mtime
                if age > self.quiet_s:
                    continue
                retained.add(path)
                previous = self._states.get(path)
                identity = (stat.st_dev, stat.st_ino)
                if previous and previous.identity and previous.identity != identity:
                    self._states.pop(path)
                try:
                    st = self._parse_file(adapter, path, stat.st_mtime, stat.st_size)
                    st.identity = identity
                    if st.errors:
                        errors.append(
                            {
                                "harness": adapter.id,
                                "file": Path(path).name,
                                "message": f"Skipped {st.errors} malformed records",
                            }
                        )
                except (OSError, ValueError, TypeError, AttributeError) as exc:
                    errors.append(
                        {
                            "harness": adapter.id,
                            "file": Path(path).name,
                            "message": f"Cannot read session: {type(exc).__name__}",
                        }
                    )
                    continue

                if age < WORKING_AGE_S or (st.hint == "busy" and age < BUSY_HINT_GRACE_S):
                    state = "working"
                elif age < WAITING_AGE_S:
                    state = "waiting"
                else:
                    state = "quiet"

                session_id = st.session_id or adapter.fallback_id(path)
                agents.append(
                    Agent(
                        id=f"{adapter.id}:{session_id}",
                        harness=adapter.id,
                        title=st.title or f"Session {session_id[:8]}",
                        cwd=st.cwd,
                        file=path,
                        started=st.started,
                        updated=st.last_ts,
                        mtime=stat.st_mtime,
                        model=st.model,
                        tokens=st.tokens,
                        state=state,
                        tail=[vars(e) for e in st.tail],
                        prompts=list(st.prompts),
                    )
                )

        self._states = {p: st for p, st in self._states.items() if p in retained}
        state_rank = {"working": 0, "waiting": 1, "quiet": 2}
        agents.sort(key=lambda a: (state_rank[a.state], -a.mtime))

        return {
            "errors": errors,
            "harnesses": [
                {
                    "id": a.id,
                    "name": a.name,
                    "color": a.color,
                    "canSpawn": bool(a.enabled and a.cmd),
                    "canResume": bool(a.enabled and a.cmd and a.resume),
                    "canMessage": bool(a.enabled and a.cmd and a.message),
                }
                for a in self.adapters
            ],
            "agents": [vars(a) for a in agents],
            "watching": [
                {
                    "id": a.id,
                    "name": a.name,
                    "color": a.color,
                    "processes": self._procs.get(a.id, 0),
                    "files": sum(1 for ag in agents if ag.harness == a.id),
                }
                for a in self.adapters
                if a.enabled
            ],
        }
