"""Previewable, additive sharing of skill directories and MCP definitions.

Existing definitions are never rewritten. Ambiguous or lossy translations are
blocked, not guessed. CLI and desktop use exactly the same plan/apply engine.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import tomlkit

from .compatibility import command_requirement, skill_requirement
from .storage import DATA_DIR, atomic_write, sync_lock, write_json

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


def _link_dir(target: str, link: str, resolve=True) -> str:
    target_path = os.path.realpath(target) if resolve else os.path.abspath(target)
    try:
        os.symlink(target_path, link, target_is_directory=True)
        return "symlink"
    except OSError:
        if os.name != "nt":
            raise
        link_path, source_path = os.path.normpath(link), target_path
        if any(char in link_path + source_path for char in ('"', "%", "\r", "\n")):
            raise ValueError("Windows junction paths cannot contain quotes or environment substitutions")
        command = f'cmd.exe /d /c mklink /J "{link_path}" "{source_path}"'
        subprocess.run(command, check=True, capture_output=True, creationflags=0x08000000)
        return "junction"


def _load_manifest():
    doc = {"created": []}
    if Path(MANIFEST).exists():
        doc = json.loads(Path(MANIFEST).read_text(encoding="utf-8"))
        if not isinstance(doc, dict) or not isinstance(doc.get("created"), list):
            raise ValueError("Invalid Perch sync manifest")
    return doc


def _shared_skill_root() -> Path:
    return Path(PERCH_DIR) / "shared" / "skills"


def _managed_links(doc: dict) -> set[str]:
    return {
        entry["link"]
        for entry in doc.get("created", [])
        if isinstance(entry, dict) and isinstance(entry.get("link"), str)
    }


def _remove_managed_link(link: Path) -> None:
    if link.is_symlink():
        link.unlink()
    else:  # A Windows junction is a directory even though Perch created it as a link.
        link.rmdir()


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


def sync_skills(roots=None, targets=SKILL_TARGETS, dry_run=False, *, extra=None, names=None, sources=None, check_dependencies=False, native=None, withheld=()) -> dict:
    from . import skill_sharing

    roots = roots if roots is not None else {k: os.path.expanduser(v) for k, v in SKILL_ROOTS.items()}
    try:
        _load_manifest()
        if not dry_run:
            skill_sharing.recover()
    except (OSError, ValueError, KeyError) as exc:
        return {"linked": [], "registered": [], "present": [], "consolidated": [], "backups": [], "conflicts": [], "errors": [{"source": "Perch", "reason": f"Cannot recover sharing state: {exc}"}], "total": 0}
    from .resources import managed_names

    excluded = managed_names('skill') | set(withheld)
    inventory = [r for r in skill_inventory(roots) if r['name'] not in excluded]
    for name, source in (extra or {}).items():
        item = next((item for item in inventory if item["name"] == name), None)
        if item is None:
            item = {"name": name, "origins": {}, "presentIn": []}
            inventory.append(item)
        item["origins"]["plugin"] = str(Path(source).resolve())
    inventory = [r for r in inventory if r['name'] not in excluded]
    if names is not None:
        inventory = [item for item in inventory if item["name"] in names]
    report = {"linked": [], "registered": [], "present": [], "consolidated": [], "backups": [], "conflicts": [], "errors": [], "total": len(inventory)}
    for skill in inventory:
        name, origins = skill["name"], skill["origins"]
        registry = _shared_skill_root() / name
        try:
            if check_dependencies and name not in (sources or {}):
                reason = next((reason for path in origins.values() if (reason := skill_requirement(path))), None)
                if reason:
                    missing = [target for target in targets if target in roots
                               and not (Path(roots[target]) / name / "SKILL.md").is_file()
                               and target not in (native or {}).get(name, [])]
                    if missing:
                        report.setdefault("skipped", []).append({"skill": name, "reason": reason})
                    report["present"].extend({"skill": name, "into": target} for target in targets if target not in missing and target in roots)
                    continue
            trees = skill_sharing.compare(origins)
            source = skill_sharing.choose(origins, trees, registry, roots, (sources or {}).get(name))
            if source is None:
                report["conflicts"].append({"skill": name, "reason": "Skill contents differ; choose a shared source", "resolvable": True})
                continue
            if os.path.lexists(registry) and os.path.realpath(registry) != source:
                if not skill_sharing.is_link(registry) or str(registry) not in _managed_links(_load_manifest()):
                    report["conflicts"].append({"skill": name, "reason": "Perch registry contains an unmanaged source"})
                    continue
            operations, linked, present = [], [], []
            registered = not os.path.lexists(registry)
            if registered or os.path.realpath(registry) != source:
                operations.append({"path": str(registry), "target": source})
            for target in targets:
                if target not in roots:
                    continue
                dest = Path(roots[target]) / name
                if target in (native or {}).get(name, []) and not os.path.lexists(dest):
                    present.append({"skill": name, "into": target, "kind": "plugin"})
                    continue
                if not skill_sharing.is_link(dest) and os.path.join(os.path.realpath(dest.parent), dest.name) == source:
                    continue  # Never replace the authoritative physical source.
                if skill_sharing.points_to(dest, registry):
                    present.append({"skill": name, "into": target})
                    continue
                if os.path.lexists(dest) and os.path.realpath(dest) not in origins.values():
                    raise ValueError(f"{target}: destination contains an unrecognized entry; left unchanged")
                operations.append({"path": str(dest), "target": str(registry)})
                linked.append({"skill": name, "into": target, "kind": "registry"})
            if operations and not dry_run:
                if skill_sharing.compare(origins) != trees:
                    raise ValueError("Skill changed before sharing; review again")
                report["backups"].extend(skill_sharing.apply(operations))
            if registered:
                report["registered"].append({"skill": name, "kind": "registry"})
            if len(trees) > 1 and linked:
                report["consolidated"].append({"skill": name, "source": source, "identical": len({json.dumps(t, sort_keys=True) for t in trees.values()}) == 1})
            report["linked"].extend(linked)
            report["present"].extend(present)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            report["errors"].append({"skill": name, "reason": str(exc)})
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


def _mcp_registry(paths=None) -> Path:
    if paths is None:
        return Path(PERCH_DIR) / "shared" / "mcp" / "servers.json"
    root = Path(next(iter(paths.values()))).parent if paths else Path(PERCH_DIR)
    return root / ".perch" / "shared" / "mcp" / "servers.json"


def _load_mcp_registry(path: Path) -> tuple[bytes, dict]:
    raw = path.read_bytes() if path.exists() else b""
    if not raw.strip():
        return raw, {}
    doc = json.loads(raw)
    servers = doc.get("servers") if isinstance(doc, dict) else None
    if not isinstance(servers, dict):
        raise ValueError("Perch MCP registry must contain a servers object")
    normalized = {}
    for name, cfg in servers.items():
        if not isinstance(name, str):
            raise ValueError("Perch MCP registry server names must be text")
        normalized[name] = _norm_server(cfg, "perch")
    return raw, normalized


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
        if any(key in cfg for key in ("headers", "http_headers")):
            raise ValueError("HTTP headers cannot be applied to a stdio server")
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
        if any(key in cfg for key in ("args", "env")):
            raise ValueError("Command arguments and environment cannot be applied to an HTTP server")
        if "headers" in cfg and "http_headers" in cfg and cfg["headers"] != cfg["http_headers"]:
            raise ValueError("Conflicting HTTP header definitions")
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
        atomic_write(backup, p.read_bytes().decode("utf-8"))


def sync_mcp(paths=None, targets=("claude", "codex", "omp"), dry_run=False, *, extra=None, names=None, check_runtime=False, native=None, withheld=()) -> dict:
    from .resources import managed_names

    excluded = managed_names('mcp') | set(withheld)
    registry_path = _mcp_registry(paths)
    paths = _paths(paths)
    report = {"found": 0, "sources": {}, "registered": [], "added": {}, "conflicts": [], "blocked": [], "errors": []}
    loaded, union, ambiguous, unavailable = {}, {}, set(), set()
    disabled = set()
    inputs = [(source, path) for source, path in paths.items()]
    if extra:
        inputs.append(("plugin", None))
    for source, path in inputs:
        # Custom fixture callers can omit desktop without accessing real desktop configuration.
        try:
            raw, doc, servers = _load(path, source == "codex") if path else (b"", {}, extra)
            loaded[source] = (raw, doc, servers)
            report["sources"][source] = len(servers)
            for name, cfg in servers.items():
                if name in excluded:
                    continue
                try:
                    norm = _norm_server(cfg, source)
                    if norm is None:
                        disabled.add(name)
                        continue
                    if check_runtime and (reason := command_requirement(cfg)):
                        raise ValueError(reason)
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
    try:
        registry_raw, registry_servers = _load_mcp_registry(registry_path)
    except (ValueError, OSError) as exc:
        report["errors"].append({"source": "Perch", "reason": f"Cannot read MCP registry: {type(exc).__name__}"})
        return report
    for name, cfg in registry_servers.items():
        if check_runtime and (reason := command_requirement(cfg)):
            unavailable.add(name)
            report["blocked"].append({"server": name, "source": "Perch", "reason": reason})
        if name in excluded:
            continue
        if name in union and union[name] != cfg:
            ambiguous.add(name)
        else:
            union[name] = cfg
    for name in sorted(disabled - union.keys()):
        report["blocked"].append({"server": name, "reason": "All native source entries are disabled"})
    if names is not None:
        union = {name: cfg for name, cfg in union.items() if name in names}
        ambiguous.intersection_update(names)
    for name in sorted(ambiguous):
        report["conflicts"].append(
            {"server": name, "reason": "Different definitions; existing versions preserved"}
        )
    report["found"] = len(union)
    registry = dict(registry_servers)
    for name, cfg in union.items():
        if name not in ambiguous and name not in unavailable and name not in registry:
            registry[name] = cfg
            report["registered"].append(name)
    if report["registered"] and not dry_run:
        try:
            text = json.dumps({"version": 1, "servers": registry}, indent=2, sort_keys=True) + "\n"
            atomic_write(registry_path, text, expected=registry_raw)
        except (ValueError, OSError) as exc:
            report["errors"].append({"source": "Perch", "reason": str(exc)})
            return report
    for target in targets:
        if target not in loaded or target not in TARGET_NAMES:
            continue
        raw, doc, existing = loaded[target]
        additions = {}
        for name, cfg in union.items():
            if name in existing or name in ambiguous or name in unavailable or target in (native or {}).get(name, []):
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
    registry_path = _mcp_registry(paths)
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
    try:
        _, registry = _load_mcp_registry(registry_path)
    except (OSError, ValueError):
        registry = {}
    for name, cfg in registry.items():
        servers.setdefault(
            name,
            {
                "name": name,
                "transport": "HTTP" if cfg.get("url") else "stdio",
                "origin": "perch",
                "presentIn": [],
                "disabledIn": [],
            },
        )
    return sorted(servers.values(), key=lambda x: x["name"].casefold())


def fingerprint(plugin_sources=None):
    from .resources import catalog

    digest = hashlib.sha256(json.dumps(catalog(), sort_keys=True).encode())
    manifest = Path(MANIFEST)
    digest.update(str(manifest).encode())
    digest.update(manifest.read_bytes() if manifest.is_file() else b"")
    for path in _paths().values():
        p = Path(path)
        digest.update(str(p).encode())
        digest.update(p.read_bytes() if p.is_file() else b"")
    registry = _mcp_registry()
    digest.update(str(registry).encode())
    digest.update(registry.read_bytes() if registry.is_file() else b"")
    for item in skill_inventory():
        digest.update(json.dumps(item, sort_keys=True).encode())
        for root in set(item["origins"].values()):
            from .skill_sharing import signature

            try:
                digest.update(signature(root).encode())
            except (OSError, ValueError) as exc:
                digest.update(str(exc).encode())
    from .capabilities import sharing_sources

    plugin_sources = sharing_sources() if plugin_sources is None else plugin_sources
    digest.update(json.dumps(plugin_sources, sort_keys=True).encode())
    for path in plugin_sources["skills"].values():
        from .skill_sharing import signature

        digest.update(signature(path).encode())
    return digest.hexdigest()


def sync_all(skills=True, mcp=True, targets=SKILL_TARGETS, dry_run=False, revision=None, *, skill_sources=None, skill_names=None):
    targets = tuple(t for t in targets if t in TARGET_NAMES)
    with sync_lock(PERCH_DIR):
        if not dry_run:
            from .skill_sharing import recover

            recover()
            from .resources import recover as recover_resources

            recover_resources()
        from .capabilities import sharing_sources

        plugin_sources = sharing_sources()
        before = fingerprint(plugin_sources)
        plan_id = hashlib.sha256((before + json.dumps([skills, mcp, targets, skill_sources, sorted(skill_names) if skill_names else None])).encode()).hexdigest()
        if revision is not None and revision != plan_id:
            raise ValueError("Tools or sharing preferences changed. Preview again before applying.")
        report = {
            "skills": sync_skills(targets=targets, dry_run=dry_run, sources=skill_sources, names=skill_names,
                                  extra=plugin_sources["skills"], native=plugin_sources["native"], check_dependencies=True,
                                  withheld=set(plugin_sources["withheld"]["skill"]) - set(skill_sources or {}))
            if skills
            else {"linked": [], "registered": [], "present": [], "conflicts": [], "errors": [], "total": 0},
            "mcp": sync_mcp(targets=targets, dry_run=dry_run, extra=plugin_sources["mcp"], check_runtime=True,
                             native=plugin_sources["native_mcp"], withheld=plugin_sources["withheld"]["mcp"])
            if mcp
            else {"found": 0, "registered": [], "added": {}, "conflicts": [], "blocked": [], "errors": []},
            "dryRun": dry_run,
            "revision": plan_id,
            "ts": time.time(),
        }
        report["skipped"] = [item for item in plugin_sources["skipped"]
                             if (skills and "skill" in item and (skill_names is None or item["skill"] in skill_names))
                             or (mcp and "server" in item)]
        report["discoveryErrors"] = plugin_sources["errors"]
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
            "Skills and portable MCP servers are managed through Perch's shared registry.",
            "Perch-managed MCP servers can share a Perch sign-in. Native plugin accounts, hooks and custom agents remain harness-specific.",
            "Custom tool commands are transferable when packaged as stdio MCP servers.",
        ],
    }
