"""Previewable, additive sharing of skill directories and MCP definitions.

Existing definitions are never rewritten. Ambiguous or lossy translations are
blocked, not guessed. CLI and desktop use exactly the same plan/apply engine.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
import tomlkit

from storage import DATA_DIR, atomic_write, sync_lock, write_json

PERCH_DIR = str(DATA_DIR)
MANIFEST = str(DATA_DIR / "sync-manifest.json")
SKILL_ROOTS = {
    "claude": "~/.claude/skills",
    "codex": "~/.agents/skills",
    "codex-legacy": "~/.codex/skills",
    "omp": "~/.omp/agent/skills",
    "omp-legacy": "~/.omp/skills",
}
SKILL_TARGETS = ("claude", "codex", "omp")
MCP_CLAUDE_JSON = os.path.expanduser("~/.claude.json")
MCP_CLAUDE_MCPJSON = os.path.expanduser("~/.claude/mcp.json")
MCP_CODEX_TOML = str(Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser() / "config.toml")
MCP_OMP_JSON = os.path.expanduser("~/.omp/agent/mcp.json")
MCP_DESKTOP_JSON = str(
    (
        Path(os.environ.get("APPDATA", "~")).expanduser() / "Claude"
        if os.name == "nt"
        else Path("~/Library/Application Support/Claude").expanduser()
    )
    / "claude_desktop_config.json"
)
TARGET_NAMES = {
    "claude": "Claude Code",
    "codex": "Codex CLI & desktop",
    "omp": "omp",
    "claude-desktop": "Claude Desktop",
}


def _link_dir(target: str, link: str) -> str:
    try:
        os.symlink(os.path.realpath(target), link, target_is_directory=True)
        return "symlink"
    except OSError:
        if os.name != "nt":
            raise
        link_path, source_path = os.path.normpath(link), os.path.realpath(target)
        if any(char in link_path + source_path for char in ('"', "%", "\r", "\n")):
            raise ValueError("Windows junction paths cannot contain quotes or environment substitutions")
        command = f'cmd.exe /d /c mklink /J "{link_path}" "{source_path}"'
        subprocess.run(command, check=True, capture_output=True, creationflags=0x08000000)
        return "junction"


def _record_links(created: list[dict]) -> None:
    doc = {"created": []}
    if Path(MANIFEST).exists():
        doc = json.loads(Path(MANIFEST).read_text(encoding="utf-8"))
        if not isinstance(doc, dict) or not isinstance(doc.get("created"), list):
            raise ValueError("Invalid Perch sync manifest")
    doc["created"].extend(created)
    write_json(MANIFEST, doc)


def skill_inventory(roots=None) -> list:
    roots = roots if roots is not None else {k: os.path.expanduser(v) for k, v in SKILL_ROOTS.items()}
    skills = {}
    for harness, root in roots.items():
        if not Path(root).is_dir():
            continue
        for folder in sorted(Path(root).iterdir()):
            if folder.name.startswith(".") or not (folder / "SKILL.md").is_file():
                continue
            item = skills.setdefault(folder.name, {"name": folder.name, "origins": {}, "presentIn": []})
            item["origins"][harness] = str(folder.resolve())
            item["presentIn"].append(harness)
    return sorted(skills.values(), key=lambda x: x["name"].casefold())


def sync_skills(roots=None, targets=SKILL_TARGETS, dry_run=False) -> dict:
    roots = roots if roots is not None else {k: os.path.expanduser(v) for k, v in SKILL_ROOTS.items()}
    inventory = skill_inventory(roots)
    report = {"linked": [], "present": [], "conflicts": [], "errors": [], "total": len(inventory)}
    created = []
    for skill in inventory:
        origins = skill["origins"]
        variants = set(origins.values())
        if len(variants) > 1:
            report["conflicts"].append(
                {"skill": skill["name"], "reason": "Different source directories; choose one before sharing"}
            )
            continue
        source = next(iter(variants))
        for target in targets:
            if target not in roots:  # Claude Desktop does not have this skills-directory contract.
                continue
            dest = Path(roots[target]) / skill["name"]
            if os.path.lexists(dest):
                if os.path.realpath(dest) == source:
                    if os.path.join(os.path.realpath(dest.parent), dest.name) != source:
                        report["present"].append({"skill": skill["name"], "into": target})
                else:
                    report["conflicts"].append(
                        {"skill": skill["name"], "into": target, "reason": "Destination already exists"}
                    )
                continue
            entry = {"skill": skill["name"], "into": target, "kind": "link"}
            try:
                if not dry_run:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    entry["kind"] = _link_dir(source, str(dest))
                    created.append(
                        {"link": str(dest), "target": source, "kind": entry["kind"], "ts": time.time()}
                    )
                report["linked"].append(entry)
            except (OSError, ValueError) as exc:
                report["errors"].append({"skill": skill["name"], "into": target, "reason": str(exc)})
    if created:
        _record_links(created)
    return report


def _paths(paths=None):
    result = {
        "claude": MCP_CLAUDE_JSON,
        "claude_mcpjson": MCP_CLAUDE_MCPJSON,
        "codex": MCP_CODEX_TOML,
        "omp": MCP_OMP_JSON,
        "claude-desktop": MCP_DESKTOP_JSON,
    }
    if paths is not None:
        root = Path(next(iter(paths.values()))).parent if paths else Path(PERCH_DIR)
        result = {key: str(root / (".missing-" + key)) for key in result}
    result.update(paths or {})
    return result


def _load(path, toml=False):
    p = Path(path)
    raw = p.read_bytes() if p.exists() else b""
    doc = (
        (tomlkit.parse(raw.decode("utf-8")) if toml else json.loads(raw))
        if raw.strip()
        else (tomlkit.document() if toml else {})
    )
    if not isinstance(doc, dict):
        raise ValueError("Configuration must be an object")
    key = "mcp_servers" if toml else "mcpServers"
    servers = doc.get(key, {})
    if not isinstance(servers, dict):
        raise ValueError(f"{key} must be an object")
    return raw, doc, servers


def _norm_server(cfg, source=""):
    if not isinstance(cfg, dict) or cfg.get("enabled") is False or cfg.get("disabled") is True:
        return None
    if bool(cfg.get("command")) == bool(cfg.get("url")):
        raise ValueError("Expected exactly one command or URL")
    allowed = {"command", "args", "env", "url", "type", "headers", "http_headers", "enabled", "disabled"}
    unsupported = sorted(set(cfg) - allowed)
    if unsupported:
        raise ValueError("Client-specific fields need review: " + ", ".join(unsupported))
    if cfg.get("command"):
        if not isinstance(cfg["command"], str):
            raise ValueError("Command must be text")
        out = {"command": cfg["command"]}
        if "args" in cfg:
            if not isinstance(cfg["args"], list) or not all(isinstance(a, str) for a in cfg["args"]):
                raise ValueError("Arguments must be a list of strings")
            out["args"] = list(cfg["args"])
        if "env" in cfg:
            if not isinstance(cfg["env"], dict) or not all(isinstance(v, str) for v in cfg["env"].values()):
                raise ValueError("Environment must contain string values")
            out["env"] = dict(cfg["env"])
        if cfg.get("type", "stdio") != "stdio":
            raise ValueError("Command transport must be stdio")
    else:
        if not isinstance(cfg["url"], str) or not cfg["url"].startswith(("https://", "http://")):
            raise ValueError("Expected an HTTP or HTTPS URL")
        transport = cfg.get("type", "http")
        if transport not in ("http", "streamable-http"):
            raise ValueError(f"{transport} transport is not portable to every target")
        out = {"url": cfg["url"]}
        headers = cfg.get("http_headers", cfg.get("headers"))
        if headers is not None:
            if not isinstance(headers, dict) or not all(isinstance(v, str) for v in headers.values()):
                raise ValueError("Headers must contain string values")
            out["headers"] = dict(headers)
    # Expansion conventions are not the same between clients. Never resolve secrets here.
    if "${" in json.dumps(out):
        raise ValueError("Environment substitutions need a target-specific configuration")
    return out


def _encode(cfg, target):
    result = dict(cfg)
    if "url" in result:
        if target == "claude-desktop":
            raise ValueError("Remote servers use Claude Desktop connectors; sign in within that application")
        if target == "codex":
            if "headers" in result:
                result["http_headers"] = result.pop("headers")
        else:
            result["type"] = "http"
    return result


def _backup(path):
    p = Path(path)
    if p.exists():
        backup = p.with_name(p.name + f".perch-{time.time_ns()}.bak")
        atomic_write(backup, p.read_text(encoding="utf-8"))


def sync_mcp(paths=None, targets=("claude", "codex", "omp"), dry_run=False) -> dict:
    paths = _paths(paths)
    report = {"found": 0, "sources": {}, "added": {}, "conflicts": [], "blocked": [], "errors": []}
    loaded, union, ambiguous, unavailable = {}, {}, set(), set()
    for source, path in paths.items():
        # Custom fixture callers can omit desktop without accessing real desktop configuration.
        try:
            raw, doc, servers = _load(path, source == "codex")
            loaded[source] = (raw, doc, servers)
            report["sources"][source] = len(servers)
            for name, cfg in servers.items():
                try:
                    norm = _norm_server(cfg, source)
                    if norm is None:
                        continue
                except ValueError as exc:
                    unavailable.add(name)
                    report["blocked"].append({"server": name, "source": source, "reason": str(exc)})
                    continue
                if name in union and union[name] != norm:
                    ambiguous.add(name)
                else:
                    union[name] = norm
        except (ValueError, OSError) as exc:
            report["errors"].append(
                {"source": source, "reason": f"Cannot read {Path(path).name}: {type(exc).__name__}"}
            )
    for name in sorted(ambiguous):
        report["conflicts"].append(
            {"server": name, "reason": "Different definitions; existing versions preserved"}
        )
    report["found"] = len(union)
    for target in targets:
        if target not in loaded or target not in TARGET_NAMES:
            continue
        raw, doc, existing = loaded[target]
        additions = {}
        for name, cfg in union.items():
            if name in existing or name in ambiguous or name in unavailable:
                continue
            try:
                additions[name] = _encode(cfg, target)
            except ValueError as exc:
                report["blocked"].append({"server": name, "into": target, "reason": str(exc)})
        if not additions:
            continue
        if not dry_run:
            try:
                if target == "omp" and not raw:
                    doc["$schema"] = (
                        "https://raw.githubusercontent.com/can1357/oh-my-pi/main/packages/coding-agent/src/config/mcp-schema.json"
                    )
                key = "mcp_servers" if target == "codex" else "mcpServers"
                if key not in doc:
                    doc[key] = tomlkit.table() if target == "codex" else {}
                for name, cfg in additions.items():
                    doc[key][name] = cfg
                text = tomlkit.dumps(doc) if target == "codex" else json.dumps(doc, indent=2) + "\n"
                # Validate the final document before touching disk.
                (tomlkit.parse if target == "codex" else json.loads)(text)
                _backup(paths[target])
                atomic_write(paths[target], text, expected=raw)
            except (ValueError, OSError) as exc:
                report["errors"].append({"source": target, "reason": str(exc)})
                continue
        report["added"][target] = sorted(additions)
    return report


def mcp_overview(paths=None):
    servers = {}
    for source, path in _paths(paths).items():
        try:
            _, _, definitions = _load(path, source == "codex")
        except (OSError, ValueError):
            continue
        for name, cfg in definitions.items():
            if not isinstance(cfg, dict):
                continue
            item = servers.setdefault(
                name,
                {
                    "name": name,
                    "transport": "HTTP" if cfg.get("url") else "stdio",
                    "origin": source,
                    "presentIn": [],
                    "disabledIn": [],
                },
            )
            item["presentIn"].append(source)
            if cfg.get("enabled") is False or cfg.get("disabled") is True:
                item["disabledIn"].append(source)
    return sorted(servers.values(), key=lambda x: x["name"].casefold())


def fingerprint():
    digest = hashlib.sha256()
    for path in _paths().values():
        p = Path(path)
        digest.update(str(p).encode())
        digest.update(p.read_bytes() if p.is_file() else b"")
    for item in skill_inventory():
        digest.update(json.dumps(item, sort_keys=True).encode())
        for root in set(item["origins"].values()):
            p = Path(root) / "SKILL.md"
            digest.update(p.read_bytes())
    return digest.hexdigest()


def sync_all(skills=True, mcp=True, targets=SKILL_TARGETS, dry_run=False, revision=None):
    targets = tuple(t for t in targets if t in TARGET_NAMES)
    with sync_lock(PERCH_DIR):
        before = fingerprint()
        plan_id = hashlib.sha256((before + json.dumps([skills, mcp, targets])).encode()).hexdigest()
        if revision is not None and revision != plan_id:
            raise ValueError("Tools or sharing preferences changed. Preview again before applying.")
        report = {
            "skills": sync_skills(targets=targets, dry_run=dry_run)
            if skills
            else {"linked": [], "conflicts": [], "errors": [], "total": 0},
            "mcp": sync_mcp(targets=targets, dry_run=dry_run)
            if mcp
            else {"found": 0, "added": {}, "conflicts": [], "blocked": [], "errors": []},
            "dryRun": dry_run,
            "revision": plan_id,
            "ts": time.time(),
        }
        if not dry_run:
            write_json(Path(PERCH_DIR) / "last-sync.json", report)
        return report


def overview():
    return {
        "skills": skill_inventory(),
        "mcp": mcp_overview(),
        "targets": [{"id": k, "name": v, "skills": k != "claude-desktop"} for k, v in TARGET_NAMES.items()],
        "notes": [
            "Codex CLI and desktop share their user configuration.",
            "Skill links share edits immediately. Restart a harness if it does not refresh tools.",
            "OAuth sign-ins, plugin installations, hooks and custom agents remain owned by each harness.",
            "Custom tool commands are transferable when packaged as stdio MCP servers.",
        ],
    }


def _strip_mcp_sections(text):
    """Legacy helper retained for callers; never used by the sharing engine."""
    out, skipping = [], False
    for line in text.splitlines():
        if re.match(r"^\s*\[\s*mcp_servers(?:\..*)?\]\s*$", line):
            skipping = True
            continue
        if skipping and re.match(r"^\s*\[", line):
            skipping = False
        if not skipping:
            out.append(line)
    return "\n".join(out).rstrip() + "\n"
