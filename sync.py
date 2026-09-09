"""Perch sync: make every harness see the same skills and MCP servers.

Skills: omp already aggregates every harness's skills on its own; Claude and
Codex do not. Sync closes the gap by linking each harness's skills into the
others' user skill directories (junction on Windows, symlink elsewhere), so
`<name>/SKILL.md` resolves identically everywhere. Links Perch creates are
recorded in ~/.perch/sync-manifest.json.

MCP: builds the union of stdio/http server definitions across
~/.claude.json, ~/.claude/mcp.json, ~/.codex/config.toml and
~/.omp/agent/mcp.json, then writes the missing entries back in each file's
own format. Files are backed up (<file>.perch-bak, once) before any write.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time

PERCH_DIR = os.path.expanduser("~/.perch")
MANIFEST = os.path.join(PERCH_DIR, "sync-manifest.json")

SKILL_ROOTS = {
    "claude": "~/.claude/skills",
    "codex": "~/.codex/skills",
    "omp": "~/.omp/skills",
    "agents": "~/.agents/skills",
}
SKILL_TARGETS = ("claude", "codex")  # omp/agents already aggregate the rest

MCP_CLAUDE_JSON = os.path.expanduser("~/.claude.json")
MCP_CLAUDE_MCPJSON = os.path.expanduser("~/.claude/mcp.json")
MCP_CODEX_TOML = os.path.expanduser("~/.codex/config.toml")
MCP_OMP_JSON = os.path.expanduser("~/.omp/agent/mcp.json")


# --------------------------------------------------------------------------
# skills

def _link_dir(target: str, link: str) -> str:
    try:
        os.symlink(target, link, target_is_directory=True)
        return "symlink"
    except OSError:
        if os.name != "nt":
            raise
        # cmd's mklink rejects mixed forward/backward separators
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", os.path.normpath(link), os.path.normpath(target)],
            check=True, capture_output=True, creationflags=0x08000000,
        )
        return "junction"


def _record_links(created: list[dict]) -> None:
    os.makedirs(PERCH_DIR, exist_ok=True)
    manifest = {"created": []}
    if os.path.isfile(MANIFEST):
        try:
            with open(MANIFEST, "r", encoding="utf-8") as fh:
                manifest = json.load(fh)
        except (OSError, json.JSONDecodeError):
            pass
    manifest.setdefault("created", []).extend(created)
    with open(MANIFEST, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=1)


def sync_skills(roots: dict | None = None, targets: tuple = SKILL_TARGETS) -> dict:
    roots = roots or {h: os.path.expanduser(p) for h, p in SKILL_ROOTS.items()}
    found: dict[str, tuple[str, str]] = {}
    conflicts = []

    for harness, root in roots.items():
        if not os.path.isdir(root):
            continue
        for entry in sorted(os.listdir(root)):
            full = os.path.join(root, entry)
            if entry.startswith(".") or not os.path.isdir(full):
                continue
            if not os.path.isfile(os.path.join(full, "SKILL.md")):
                continue
            if entry in found:
                if os.path.realpath(found[entry][1]) != os.path.realpath(full):
                    conflicts.append({"skill": entry, "kept": found[entry][0], "dropped": harness})
            else:
                found[entry] = (harness, full)

    linked, present, created = [], [], []
    for target_h in targets:
        root = roots[target_h]
        os.makedirs(root, exist_ok=True)
        for name, (origin, path) in found.items():
            if origin == target_h:
                continue
            link = os.path.join(root, name)
            if os.path.lexists(link):
                if os.path.realpath(link) == os.path.realpath(path):
                    present.append({"skill": name, "into": target_h})
                else:
                    conflicts.append({"skill": name, "kept": target_h, "dropped": origin})
                continue
            kind = _link_dir(path, link)
            linked.append({"skill": name, "into": target_h, "kind": kind})
            created.append({"link": link, "target": path, "kind": kind, "ts": time.time()})

    if created:
        _record_links(created)
    return {"linked": linked, "present": present, "conflicts": conflicts, "total": len(found)}


# --------------------------------------------------------------------------
# mcp

def _norm_server(cfg) -> dict | None:
    """Normalize one server definition to {command|url, ...} or None."""
    if not isinstance(cfg, dict) or cfg.get("enabled") is False:
        return None
    if cfg.get("command"):
        out = {"command": cfg["command"]}
        if isinstance(cfg.get("args"), list):
            out["args"] = [str(a) for a in cfg["args"]]
        if isinstance(cfg.get("env"), dict):
            out["env"] = {str(k): str(v) for k, v in cfg["env"].items()}
        if isinstance(cfg.get("cwd"), str):
            out["cwd"] = cfg["cwd"]
        return out
    if cfg.get("url"):
        out = {"url": cfg["url"]}
        if isinstance(cfg.get("headers"), dict):
            out["headers"] = {str(k): str(v) for k, v in cfg["headers"].items()}
        return out
    return None


def _backup(path: str) -> None:
    bak = path + ".perch-bak"
    if os.path.isfile(path) and not os.path.isfile(bak):
        shutil.copy2(path, bak)


def _write_json_atomic(path: str, doc: dict) -> None:
    _backup(path)
    tmp = path + ".perch-tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def _json_servers(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
        return doc.get("mcpServers") or {}
    except (OSError, json.JSONDecodeError):
        return {}


_MCP_SECTION = re.compile(r'^\s*\[\s*mcp_servers(\..*)?\]\s*$', re.IGNORECASE)


def _codex_servers(path: str) -> dict:
    import tomllib
    try:
        with open(path, "rb") as fh:
            doc = tomllib.load(fh)
        return doc.get("mcp_servers") or {}
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def _strip_mcp_sections(text: str) -> str:
    out, skipping = [], False
    for line in text.splitlines():
        if _MCP_SECTION.match(line):
            skipping = True
            continue
        if skipping and re.match(r"^\s*\[", line):
            skipping = False
        if not skipping:
            out.append(line)
    return "\n".join(out).rstrip() + "\n"


def _toml_str(s: str) -> str:
    return json.dumps(s)  # a JSON string is a valid TOML basic string


def _toml_key(name: str) -> str:
    return name if re.match(r"^[A-Za-z0-9_-]+$", name) else _toml_str(name)


def _render_codex(servers: dict) -> str:
    blocks = []
    for name, cfg in servers.items():
        lines = [f"[mcp_servers.{_toml_key(name)}]"]
        if "command" in cfg:
            lines.append(f"command = {_toml_str(cfg['command'])}")
            if cfg.get("args"):
                lines.append("args = [" + ", ".join(_toml_str(a) for a in cfg["args"]) + "]")
            if cfg.get("cwd"):
                lines.append(f"cwd = {_toml_str(cfg['cwd'])}")
            if cfg.get("env"):
                lines.append("")
                lines.append(f"[mcp_servers.{_toml_key(name)}.env]")
                for k, v in cfg["env"].items():
                    lines.append(f"{k} = {_toml_str(v)}")
        else:
            lines.append(f"url = {_toml_str(cfg['url'])}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def sync_mcp(paths: dict | None = None) -> dict:
    paths = paths or {}
    sources = [
        ("claude", paths.get("claude", MCP_CLAUDE_JSON), _json_servers),
        ("claude-mcpjson", paths.get("claude_mcpjson", MCP_CLAUDE_MCPJSON), _json_servers),
        ("codex", paths.get("codex", MCP_CODEX_TOML), _codex_servers),
        ("omp", paths.get("omp", MCP_OMP_JSON), _json_servers),
    ]

    union: dict[str, dict] = {}
    origin: dict[str, str] = {}
    conflicts = []
    counts = {}
    for name, path, reader in sources:
        raw = reader(path)
        counts[name] = len(raw)
        for srv_name, cfg in raw.items():
            norm = _norm_server(cfg)
            if norm is None:
                continue
            if srv_name in union:
                if union[srv_name] != norm:
                    conflicts.append({"server": srv_name, "kept": origin[srv_name], "dropped": name})
            else:
                union[srv_name] = norm
                origin[srv_name] = name

    report = {"found": len(union), "sources": counts, "added": {}, "conflicts": conflicts}
    if not union:
        return report

    def missing(existing: dict) -> dict:
        return {n: c for n, c in union.items() if n not in existing}

    # claude user scope: top-level mcpServers in the claude state file
    claude_path = paths.get("claude", MCP_CLAUDE_JSON)
    if os.path.isfile(claude_path):
        try:
            with open(claude_path, "r", encoding="utf-8") as fh:
                doc = json.load(fh)
        except json.JSONDecodeError:
            doc = None
        if doc is not None:
            servers = doc.setdefault("mcpServers", {})
            add = missing(servers)
            if add:
                for n, c in add.items():
                    servers[n] = ({"type": "http", **c} if "url" in c else c)
                _write_json_atomic(claude_path, doc)
                report["added"]["claude"] = sorted(add)

    # codex: config.toml [mcp_servers.*]
    codex_path = paths.get("codex", MCP_CODEX_TOML)
    if os.path.isfile(codex_path):
        existing = _codex_servers(codex_path)
        add = missing(existing)
        if add:
            with open(codex_path, "r", encoding="utf-8") as fh:
                text = _strip_mcp_sections(fh.read())
            merged = {**existing, **add}
            _backup(codex_path)
            with open(codex_path, "w", encoding="utf-8") as fh:
                fh.write(text + "\n" + _render_codex(merged))
            report["added"]["codex"] = sorted(add)

    # omp: agent/mcp.json (created if missing)
    omp_path = paths.get("omp", MCP_OMP_JSON)
    os.makedirs(os.path.dirname(omp_path), exist_ok=True)
    try:
        with open(omp_path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, json.JSONDecodeError):
        doc = {"$schema": "https://raw.githubusercontent.com/can1357/oh-my-pi/main/packages/coding-agent/src/config/mcp-schema.json"}
    servers = doc.setdefault("mcpServers", {})
    add = missing(servers)
    if add:
        for n, c in add.items():
            servers[n] = ({"type": "http", **c} if "url" in c else c)
        _write_json_atomic(omp_path, doc)
        report["added"]["omp"] = sorted(add)

    return report


def mcp_overview() -> list:
    """The MCP union as the settings UI shows it: name, transport, origin, reach."""
    sources = [
        ("claude", MCP_CLAUDE_JSON, _json_servers),
        ("claude-mcpjson", MCP_CLAUDE_MCPJSON, _json_servers),
        ("codex", MCP_CODEX_TOML, _codex_servers),
        ("omp", MCP_OMP_JSON, _json_servers),
    ]
    servers: dict[str, dict] = {}
    for src, path, reader in sources:
        for name, cfg in reader(path).items():
            norm = _norm_server(cfg)
            if norm is None:
                continue
            entry = servers.setdefault(name, {
                "name": name,
                "transport": "http" if "url" in norm else "stdio",
                "origin": src,
                "presentIn": [],
            })
            entry["presentIn"].append(src)
    return sorted(servers.values(), key=lambda e: e["name"])


def sync_all(skills: bool = True, mcp: bool = True, targets: tuple = SKILL_TARGETS) -> dict:
    report = {
        "skills": sync_skills(targets=targets) if skills else {"linked": [], "present": [], "conflicts": [], "total": 0},
        "mcp": sync_mcp() if mcp else {"found": 0, "sources": {}, "added": {}, "conflicts": []},
        "ts": time.time(),
    }
    try:
        os.makedirs(PERCH_DIR, exist_ok=True)
        with open(os.path.join(PERCH_DIR, "last-sync.json"), "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=1)
    except OSError:
        pass
    return report


if __name__ == "__main__":
    print(json.dumps(sync_all(), indent=1))
