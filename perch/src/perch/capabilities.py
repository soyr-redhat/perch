"""Read-only capability discovery and explicit, revision-checked component sharing."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
import os
from pathlib import Path
import tomllib

from . import sync
from .storage import sync_lock


@dataclass
class Capability:
    id: str
    kind: str
    name: str
    origins: dict
    presentIn: list = field(default_factory=list)
    compatibility: dict = field(default_factory=dict)
    plugin: str | None = None
    components: list = field(default_factory=list)
    discovery: str = "detected"


def _read(path):
    raw = path.read_bytes()
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError("Manifest exceeds 2 MB")
    value = tomllib.loads(raw.decode("utf-8")) if path.suffix == ".toml" else json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("Manifest must be an object")
    return value


def _within(root, value):
    if not isinstance(value, str):
        raise ValueError("Component path must be text")
    path = (root / value).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("Component path leaves the plugin directory")
    return path


def _id(kind, origin):
    return kind + ":" + hashlib.sha256(origin.encode()).hexdigest()[:24]


def _plugin_paths(value, root):
    if isinstance(value, dict):
        return {key: _plugin_paths(item, root) for key, item in value.items()}
    if isinstance(value, list):
        return [_plugin_paths(item, root) for item in value]
    if isinstance(value, str):
        tokens = ("${CLAUDE_PLUGIN_ROOT}", "${CODEX_PLUGIN_ROOT}")
        rooted = any(token in value for token in tokens)
        if rooted and not value.startswith(tokens):
            raise ValueError("Embedded plugin path variables require a specific adapter")
        value = value.replace("${CLAUDE_PLUGIN_ROOT}", str(root)).replace("${CODEX_PLUGIN_ROOT}", str(root))
        if rooted or value.startswith(("./", "../")):
            value = str(_within(root, value))
    return value


def discover_plugins(home=None):
    """Use Claude's install index; Codex cache entries are never called installed."""
    home = Path(home or Path.home())
    codex = Path(os.environ.get("CODEX_HOME", str(home / ".codex"))) if home == Path.home() else home / ".codex"
    candidates, errors = [], []
    index = home / ".claude/plugins/installed_plugins.json"
    if index.exists():
        try:
            plugins = _read(index).get("plugins", {})
            if not isinstance(plugins, dict):
                raise ValueError("Plugin index must contain an object")
            for name, installs in plugins.items():
                if not isinstance(installs, list):
                    errors.append({"source": name, "reason": "Invalid installation list"})
                    continue
                for entry in installs:
                    if isinstance(entry, dict) and isinstance(entry.get("installPath"), str):
                        candidates.append(("claude", Path(entry["installPath"]), "installed", name))
        except (OSError, ValueError) as exc:
            errors.append({"source": str(index), "reason": str(exc)})
    for format_name in ("codex", "claude"):
        for manifest in sorted((codex / "plugins/cache").glob(f"*/*/*/.{format_name}-plugin/plugin.json")):
            candidates.append(("codex", manifest.parent.parent, "cached", manifest.parent.parent.parent.name))
    items = []
    seen = set()
    for harness, root, discovery, name in candidates:
        try:
            root = root.resolve()
            if (harness, root) in seen:
                continue
            seen.add((harness, root))
            if not root.is_dir():
                raise ValueError("Installed plugin directory is missing")
            manifest_path = root / f".{harness}-plugin/plugin.json"
            if not manifest_path.exists():
                manifest_path = root / ".claude-plugin/plugin.json"
            manifest = _read(manifest_path) if manifest_path.exists() else {}
            name = manifest.get("name", name)
            if not isinstance(name, str) or not name:
                raise ValueError("Plugin name must be text")
            plugin_id = _id("plugin", harness + str(root))
            plugin = {"id": plugin_id, "kind": "plugin", "name": name, "harness": harness,
                      "path": str(root), "discovery": discovery, "components": []}
            items.append(plugin)
            components = plugin["components"]
            skill_roots = manifest.get("skills", ["./skills"])
            if isinstance(skill_roots, str):
                skill_roots = [skill_roots]
            if not isinstance(skill_roots, list):
                raise ValueError("Skills must be a path or list of paths")
            if (root / "SKILL.md").is_file() and "skills" not in manifest:
                skill_roots = ["."]
            for value in dict.fromkeys(["./skills", *skill_roots]):
                folder = _within(root, value)
                paths = [folder] if (folder / "SKILL.md").is_file() else sorted(folder.glob("*/SKILL.md"))
                for path in paths:
                    path = _within(root, str(path.parent if path.name == "SKILL.md" else path))
                    text = _within(root, str(path / "SKILL.md")).read_text(encoding="utf-8")
                    reason = "Requires a host plugin variable" if "${CLAUDE_PLUGIN_" in text or "${CODEX_PLUGIN_" in text else None
                    components.append({"kind": "skill", "name": path.name, "path": str(path), "reason": reason})
            configs = manifest.get("mcpServers", [])
            configs = [configs] if isinstance(configs, (str, dict)) else configs
            if not isinstance(configs, list):
                raise ValueError("MCP definitions must be an object, path, or list")
            if (root / ".mcp.json").is_file():
                configs = ["./.mcp.json", *configs]
            for cfg in configs:
                doc = _read(_within(root, cfg)) if isinstance(cfg, str) else cfg
                if not isinstance(doc, dict):
                    raise ValueError("MCP definitions must be an object")
                definitions = doc.get("mcpServers", doc)
                if not isinstance(definitions, dict):
                    raise ValueError("MCP servers must be an object")
                for server_name, definition in definitions.items():
                    components.append({"kind": "mcp", "name": server_name, "definition": _plugin_paths(definition, root)})
            for key, default in (("apps", ".app.json"), ("hooks", "hooks/hooks.json"), ("agents", "agents"),
                                 ("commands", "commands"), ("lspServers", ".lsp.json")):
                if key in manifest or (root / default).exists():
                    components.append({"kind": key, "name": key, "reason": "Requires a host-specific adapter"})
            plugin["components"] = list({json.dumps(c, sort_keys=True): c for c in components}.values())
        except (OSError, ValueError, TypeError) as exc:
            errors.append({"source": str(root), "reason": str(exc)})
            if items and items[-1].get("path") == str(root):
                for component in items[-1]["components"]:
                    component["reason"] = "Plugin discovery is incomplete; fix its manifest first"
    return items, errors


def _report(kind, name, target, component=None, dry_run=True):
    extra = None
    if component:
        extra = {name: component["path"] if kind == "skill" else component["definition"]}
    fn = sync.sync_skills if kind == "skill" else sync.sync_mcp
    return fn(targets=(target,), dry_run=dry_run, extra=extra, names={name})


def _status(report, kind, name, target, present):
    issues = [item for key in ("errors", "conflicts", "blocked") for item in report.get(key, [])
              if (item.get("skill") or item.get("server") or name) == name]
    if issues:
        return {"status": "blocked", "reason": issues[0].get("reason", "Needs review")}
    present = present or any(item.get("into") == target and item.get("skill") == name for item in report.get("present", []))
    additions = any(item.get("into") == target and item.get("skill") == name for item in report.get("linked", [])) if kind == "skill" else name in report.get("added", {}).get(target, [])
    if not present and not additions:
        return {"status": "blocked", "reason": "No enabled compatible source can be connected to this destination"}
    return {"status": "present" if present else "available", "reason": ""}


def inventory(*, base=None, plugins=None, errors=None):
    live = base is None
    if base is None:
        base = sync.overview()
    if plugins is None:
        plugins, errors = discover_plugins()
    resources = []
    native_mcp = {}
    if live and any(c["kind"] == "mcp" for p in plugins for c in p["components"]):
        for hid, path in sync._paths().items():
            try:
                native_mcp[hid] = sync._load(path, hid == "codex")[2]
            except (OSError, ValueError):
                continue
    for kind, key in (("skill", "skills"), ("mcp", "mcp")):
        for item in base[key]:
            resource = Capability(_id(kind, item["name"]), kind, item["name"], item.get("origins") or {item.get("origin", "unknown"): ""}, item["presentIn"])
            for target in base["targets"]:
                hid = target["id"]
                if kind == "skill" and not target["skills"]:
                    status = {"status": "unsupported", "reason": "No compatible skill directory"}
                elif hid in item.get("disabledIn", []):
                    status = {"status": "disabled", "reason": "Disabled in this harness"}
                else:
                    status = {"status": "present" if hid in item["presentIn"] else "review", "reason": "Review sharing to check compatibility"}
                resource.compatibility[hid] = status
            resources.append(asdict(resource))
    for plugin in plugins:
        parent = Capability(plugin["id"], "plugin", plugin["name"], {plugin["harness"]: plugin["path"]}, discovery=plugin["discovery"])
        for hid in sync.TARGET_NAMES:
            parent.compatibility[hid] = {"status": "components", "reason": "Review individual components"}
        resources.append(asdict(parent))
        seen = set()
        for component in plugin["components"]:
            identity = component["kind"] + ":" + component["name"] + ":" + component.get("path", "")
            if identity in seen:
                continue
            seen.add(identity)
            rid = _id(component["kind"], plugin["id"] + identity)
            resource = Capability(rid, component["kind"], component["name"], {plugin["harness"]: component.get("path", plugin["path"])}, plugin=plugin["id"], discovery=plugin["discovery"])
            if resource.kind == "skill":
                standalone = next((r for r in resources if r["kind"] == "skill" and not r["plugin"] and r["name"] == resource.name), None)
                if standalone:
                    resource.presentIn = [hid for hid, path in standalone["origins"].items() if Path(path).resolve() == Path(component["path"]).resolve()]
                    if len(resource.presentIn) == len(standalone["origins"]):
                        resources.remove(standalone)
            elif resource.kind == "mcp":
                for hid, definitions in native_mcp.items():
                    try:
                        normalized = sync._norm_server(component["definition"], "plugin")
                        if normalized is not None and sync._norm_server(definitions.get(resource.name), hid) == normalized:
                            resource.presentIn.append(hid)
                    except ValueError:
                        continue
            for hid in sync.TARGET_NAMES:
                reason = component.get("reason")
                if hid in resource.presentIn:
                    status = {"status": "present", "reason": ""}
                elif reason or resource.kind not in ("skill", "mcp") or (resource.kind == "skill" and hid not in sync.SKILL_TARGETS):
                    status = {"status": "unsupported", "reason": reason or "No compatible adapter"}
                else:
                    status = {"status": "review", "reason": "Review this component before linking"}
                resource.compatibility[hid] = status
            parent.components.append(rid)
            resources.append(asdict(resource))
        next(r for r in resources if r["id"] == parent.id)["components"] = parent.components
    return {"resources": resources, "targets": base["targets"], "errors": errors or []}


def link(resource_id, target, *, revision=None, apply=False):
    if target not in sync.TARGET_NAMES:
        raise ValueError("Unknown destination harness")
    with sync_lock(sync.PERCH_DIR):
        plugins, errors = discover_plugins()
        catalog = inventory(plugins=plugins, errors=errors)
        resource = next((r for r in catalog["resources"] if r["id"] == resource_id), None)
        if not resource or resource["kind"] not in ("skill", "mcp"):
            raise ValueError("Select a skill or MCP component")
        if resource["compatibility"][target]["status"] in ("unsupported", "disabled"):
            raise ValueError(resource["compatibility"][target]["reason"])
        component = None
        if resource["plugin"]:
            plugin = next(p for p in plugins if p["id"] == resource["plugin"])
            matches = [c for c in plugin["components"] if c["kind"] == resource["kind"] and c["name"] == resource["name"]]
            if len(matches) != 1:
                raise ValueError("Ambiguous plugin component")
            component = matches[0]
            if resource["kind"] == "mcp" and sync._norm_server(component["definition"], "plugin") is None:
                raise ValueError("This MCP component is disabled or invalid")
        content = (Path(component["path"]) / "SKILL.md").read_bytes() if component and resource["kind"] == "skill" else b""
        digest = hashlib.sha256((sync.fingerprint() + json.dumps([resource, target, component], sort_keys=True)).encode() + content).hexdigest()
        if apply and revision != digest:
            raise ValueError("Resources changed. Review this connection again.")
        report = _report(resource["kind"], resource["name"], target, component)
        status = _status(report, resource["kind"], resource["name"], target, target in resource["presentIn"])
        if apply and status["status"] != "blocked":
            report = _report(resource["kind"], resource["name"], target, component, dry_run=False)
            status = _status(report, resource["kind"], resource["name"], target, True)
        return {"id": resource_id, "target": target, "revision": digest, "applied": apply and status["status"] == "present", **status}
