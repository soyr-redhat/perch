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
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

WORKING_AGE_S = 8          # writes within this window => actively working
BUSY_HINT_GRACE_S = 120    # in-flight hint keeps "working" this long
WAITING_AGE_S = 15 * 60    # beyond this, session drops from LIVE to QUIET


# --------------------------------------------------------------------------
# model

@dataclass
class TailEvent:
    who: str   # user | assistant | tool
    text: str
    ts: Optional[str] = None


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
    hint: str = "unknown"          # busy | done | unknown
    tail: deque = field(default_factory=lambda: deque(maxlen=15))
    prompts: deque = field(default_factory=lambda: deque(maxlen=150))


def read_history(adapter: "Adapter", path: str, pidx: int, span: int = 40) -> dict:
    """Extract the slice of a session starting at its pidx-th user prompt."""
    st = FileState(path=path)
    st.tail = deque()  # unbounded: walk the whole file
    for rec, _ in _iter_json_lines(path, 0):
        adapter.consume(rec, st)
    events = [vars(e) for e in st.tail]
    starts = [i for i, e in enumerate(events) if e["who"] == "user"]
    if not starts:
        return {"events": events[-span:], "prompt": -1, "of": 0}
    pidx = max(0, min(pidx, len(starts) - 1))
    start = starts[pidx]
    end = starts[pidx + 1] if pidx + 1 < len(starts) else len(events)
    return {"events": events[start:end][:span], "prompt": pidx, "of": len(starts)}


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
    state: str           # working | waiting | quiet
    tail: list
    prompts: list = field(default_factory=list)


# --------------------------------------------------------------------------
# helpers

def _iter_json_lines(path: str, offset: int) -> Iterable[tuple[dict, int]]:
    """Yield (record, end_offset) for each complete JSON line after offset."""
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        fh.seek(offset)
        while True:
            line = fh.readline()
            if not line:
                break
            end = fh.tell()
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line), end
            except json.JSONDecodeError:
                continue


def _max_ts(a: Optional[str], b: Optional[str]) -> Optional[str]:
    if a and b:
        return max(a, b)
    return a or b


def _first_text(content: Any) -> Optional[str]:
    """Pull the first meaningful text out of a message content field."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") in ("text", "input_text", "output_text"):
                t = block.get("text")
                if t:
                    return t
    return None


def _clean_user_text(text: Optional[str]) -> Optional[str]:
    """Drop environment/context scaffolding that looks like a user message."""
    if not text:
        return None
    stripped = text.lstrip()
    if stripped.startswith("<"):
        return None
    return text


def _truncate(text: str, limit: int = 400) -> str:
    text = " ".join(text.split())
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
            rest = ".".join(path.split(".")[path.split(".").index(part) + 1:])
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
        return ["cmd.exe", "/c", path]
    return [path]


class Adapter:
    id = "base"
    name = "Base"
    color = "#0a84ff"
    patterns: list[str] = []
    format = "jsonl"
    enabled = True
    cmd: Optional[list[str]] = None      # argv prefix to launch the harness CLI
    resume: Optional[list[str]] = None   # appended to cmd; "{session}" substituted
    message: Optional[list[str]] = None  # headless one-shot; "{session}"/"{text}" substituted

    def spawn_argv(self, session: Optional[str] = None) -> Optional[list[str]]:
        if not self.cmd:
            return None
        if session and self.resume:
            return self.cmd + [p.replace("{session}", session) for p in self.resume]
        return list(self.cmd)

    def message_argv(self, session: str, text: str) -> Optional[list[str]]:
        """Argv to deliver one message into an existing session, non-interactively."""
        if not self.cmd or not self.message:
            return None
        return self.cmd + [
            p.replace("{session}", session).replace("{text}", text) for p in self.message
        ]

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
        self.message = ["--print", "--resume", "{session}", "{text}"]

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
                text = _clean_user_text(_first_text(blocks))
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
        self.message = ["-p", "--resume", "{session}", "{text}"]

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
            if isinstance(content, str):
                text = _clean_user_text(content)
                if text:
                    st.tail.append(TailEvent("user", _truncate(text), ts))
                    st.prompts.append({"text": _truncate(text, 70), "ts": ts})
                    if not st.title:
                        st.title = _truncate(text, 70)
                st.hint = "busy"
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text = _clean_user_text(block.get("text"))
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
        self.resume = ["resume", "{session}"]
        self.message = ["exec", "resume", "{session}", "{text}"]

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
                    text = _clean_user_text(_first_text(payload.get("content")))
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
        st.session_id = st.session_id or dig(rec, m.get("id", ""))
        st.cwd = st.cwd or dig(rec, m.get("cwd", ""))
        st.started = st.started or (ts if isinstance(ts, str) else None)
        model = dig(rec, m.get("model", ""))
        if model:
            st.model = model
        role = self._role_of(dig(rec, m.get("role", "")))
        text = dig(rec, m.get("text", ""))
        if role and isinstance(text, str) and text.strip():
            text = _clean_user_text(text)
            if text:
                st.tail.append(TailEvent(role, _truncate(text), ts if isinstance(ts, str) else None))
                if role == "user":
                    st.prompts.append({"text": _truncate(text, 70), "ts": ts if isinstance(ts, str) else None})
                if role == "user" and not st.title:
                    st.title = _truncate(text, 70)
                st.hint = "busy" if role == "user" else "done"


def load_declarative(config_path: str) -> list[DeclarativeAdapter]:
    try:
        with open(config_path, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return []
    adapters = []
    for entry in cfg.get("sources", []):
        try:
            adapters.append(DeclarativeAdapter(entry))
        except (KeyError, TypeError):
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
                capture_output=True, text=True, timeout=15,
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
            ["ps", "-eo", "comm=,args="], capture_output=True, text=True, timeout=10,
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

    def apply_settings(self, cfg: dict) -> None:
        self.quiet_s = cfg.get("watching", {}).get("quietDays", 7) * 86400
        disabled = set(cfg.get("harnesses", {}).get("disabled", []))
        for adapter in self.adapters:
            adapter.enabled = adapter.id not in disabled

    def _parse_file(self, adapter: Adapter, path: str, mtime: float, size: int) -> FileState:
        st = self._states.get(path)
        if st is None or size < st.offset:  # new or truncated file
            st = FileState(path=path)
            self._states[path] = st
        if adapter.format == "json":
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                doc = json.load(fh)
            records = dig(doc, adapter.records_path) if adapter.records_path else doc
            for rec in records if isinstance(records, list) else []:
                if isinstance(rec, dict):
                    adapter.consume(rec, st)
            st.offset = size
        else:
            for rec, end in _iter_json_lines(path, st.offset):
                adapter.consume(rec, st)
                st.offset = end
        return st

    def scan(self) -> dict:
        now = time.time()
        if now - self._procs_at > 10:
            self._procs = scan_processes()
            self._procs_at = now

        agents: list[Agent] = []
        adapter_by_id = {a.id: a for a in self.adapters}
        for adapter in self.adapters:
            if not adapter.enabled:
                continue
            for path in adapter.session_files():
                try:
                    stat = os.stat(path)
                except OSError:
                    continue
                age = now - stat.st_mtime
                if age > self.quiet_s:
                    continue
                try:
                    st = self._parse_file(adapter, path, stat.st_mtime, stat.st_size)
                except (OSError, json.JSONDecodeError):
                    continue

                if age < WORKING_AGE_S or (st.hint == "busy" and age < BUSY_HINT_GRACE_S):
                    state = "working"
                elif age < WAITING_AGE_S:
                    state = "waiting"
                else:
                    state = "quiet"

                session_id = st.session_id or adapter.fallback_id(path)
                agents.append(Agent(
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
                ))

        state_rank = {"working": 0, "waiting": 1, "quiet": 2}
        agents.sort(key=lambda a: (state_rank[a.state], -a.mtime))

        return {
            "generated": now,
            "harnesses": [
                {
                    "id": a.id,
                    "name": a.name,
                    "color": a.color,
                    "canSpawn": bool(a.cmd),
                    "canResume": bool(a.cmd and a.resume),
                    "canMessage": bool(a.cmd and a.message),
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
