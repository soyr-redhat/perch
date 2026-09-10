"""Read-only capability discovery and explicit, revision-checked component sharing."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
import os
from pathlib import Path
import tomllib

from . import sync
from .compatibility import command_requirement, skill_requirement
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
    enabled = {}
    for harness, settings_path in (("claude", home / ".claude/settings.json"), ("codex", codex / "config.toml")):
        try:
            settings = _read(settings_path) if settings_path.exists() else {}
            enabled[harness] = settings.get("enabledPlugins" if harness == "claude" else "plugins", {})
            if not isinstance(enabled[harness], dict):
                raise ValueError("Plugin settings must be an object")
        except (OSError, ValueError) as exc:
            enabled[harness] = None
            errors.append({"source": str(settings_path), "reason": str(exc)})
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
                        candidates.append(("claude", Path(entry["installPath"]), "installed", name, entry.get("scope", "user")))
        except (OSError, ValueError) as exc:
            errors.append({"source": str(index), "reason": str(exc)})
    for format_name in ("codex", "claude"):
        for manifest in sorted((codex / "plugins/cache").glob(f"*/*/*/.{format_name}-plugin/plugin.json")):
            root = manifest.parent.parent
            key = root.parent.name + "@" + root.parent.parent.name
            candidates.append(("codex", root, "cached", key, "user"))
    items = []
    seen = set()
    for harness, root, discovery, name, scope in candidates:
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
            key = name
            config = (enabled.get(harness) or {}).get(key)
            active = (config is not False) if harness == "claude" else isinstance(config, dict) and config.get("enabled") is True
            if enabled.get(harness) is None or scope != "user":
                active = False
            disabled = config is False or (isinstance(config, dict) and config.get("enabled") is False)
            name = manifest.get("name", name.split("@")[0])
            if not isinstance(name, str) or not name:
                raise ValueError("Plugin name must be text")
            plugin_id = _id("plugin", harness + str(root))
            plugin = {"id": plugin_id, "kind": "plugin", "name": name, "harness": harness,
                      "path": str(root), "discovery": discovery, "components": [],
                      "key": key, "enabled": active, "disabled": disabled, "scope": scope}
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
                    _within(root, str(path / "SKILL.md"))
                    reason = skill_requirement(path)
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
    # An enabled package name does not identify which of several cached versions is active.
    for plugin in items:
        versions = [p for p in items if (p["harness"], p["key"]) == (plugin["harness"], plugin["key"])]
        plugin["active"] = plugin["enabled"] and len(versions) == 1
        if plugin["active"]:
            plugin["discovery"] = "enabled"
    return items, errors


def _report(kind, name, target, component=None, dry_run=True):
    extra = None
    if component:
        extra = {name: component["path"] if kind == "skill" else component["definition"]}
    fn = sync.sync_skills if kind == "skill" else sync.sync_mcp
    return fn(targets=(target,), dry_run=dry_run, extra=extra, names={name}, **({"check_dependencies": True} if kind == "skill" else {"check_runtime": True}))


def _status(report, kind, name, target, present):
    issues = [item for key in ("errors", "conflicts", "blocked", "skipped") for item in report.get(key, [])
              if (item.get("skill") or item.get("server") or name) == name]
    if issues:
        return {"status": "blocked", "reason": issues[0].get("reason", "Needs review")}
    present = present or any(item.get("into") == target and item.get("skill") == name for item in report.get("present", []))
    additions = any(item.get("into") == target and item.get("skill") == name for item in report.get("linked", [])) if kind == "skill" else name in report.get("added", {}).get(target, [])
    if not present and not additions:
        return {"status": "blocked", "reason": "No enabled source is available; check the source harness configuration"}
    return {"status": "present" if present else "available", "reason": ""}


def inventory(*, base=None, plugins=None, errors=None):
    live = base is None
    if base is None:
        base = sync.overview()
    if plugins is None:
        plugins, errors = discover_plugins()
    resources = []
    native_mcp = {}
    if live:
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
                    reason = None
                    if kind == "mcp":
                        cfg = native_mcp.get(hid, {}).get(item["name"])
                        reason = command_requirement(cfg) if cfg is not None else None
                        if hid not in item["presentIn"] and live:
                            status = _status(_report(kind, item["name"], hid), kind, item["name"], hid, False)
                        else:
                            status = {"status": "blocked" if reason else ("present" if hid in item["presentIn"] else "review"), "reason": reason or "Configured; authentication is not verified"}
                    else:
                        status = {"status": "present" if hid in item["presentIn"] else "review", "reason": "Review sharing to check compatibility"}
                resource.compatibility[hid] = status
            resources.append(asdict(resource))
    for plugin in sorted(plugins, key=lambda p: not p.get("active", False)):
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
            native = plugin.get("active", False)
            if native:
                resource.presentIn.append(plugin["harness"])
            if resource.kind == "skill":
                standalone = next((r for r in resources if r["kind"] == "skill" and not r["plugin"] and r["name"] == resource.name), None)
                if standalone:
                    linked = [hid for hid, path in standalone["origins"].items() if Path(path).resolve() == Path(component["path"]).resolve()]
                    resource.presentIn = sorted(set(resource.presentIn + linked))
                    if len(linked) == len(standalone["origins"]):
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
                if resource.kind == "skill":
                    reason = reason or skill_requirement(component["path"])
                elif resource.kind == "mcp":
                    reason = reason or command_requirement(component["definition"])
                    if not reason:
                        try:
                            sync._norm_server(component["definition"], "plugin")
                        except ValueError as exc:
                            reason = str(exc)
                if plugin.get("disabled"):
                    reason = "Source plugin is disabled"
                elif plugin.get("scope", "user") != "user":
                    reason = "Project plugin; not shared into global harness settings"
                runtime_problem = command_requirement(component["definition"]) if resource.kind == "mcp" else None
                if hid == plugin["harness"] and native and runtime_problem:
                    status = {"status": "blocked", "reason": runtime_problem}
                elif hid in resource.presentIn:
                    status = {"status": "present", "reason": "Provided by the enabled plugin" if native and hid == plugin["harness"] else ""}
                elif reason or resource.kind not in ("skill", "mcp") or (resource.kind == "skill" and hid not in sync.SKILL_TARGETS):
                    status = {"status": "unsupported", "reason": reason or "No compatible adapter"}
                else:
                    status = {"status": "review", "reason": "Review this component before linking"}
                resource.compatibility[hid] = status
            parent.components.append(rid)
            resources.append(asdict(resource))
        next(r for r in resources if r["id"] == parent.id)["components"] = parent.components
    # Merge identical skill versions without merging different content or losing parent links.
    from .skill_sharing import signature

    identical, aliases, merged = {}, {}, []
    for resource in resources:
        key = None
        if resource["kind"] == "skill" and resource["plugin"]:
            try:
                key = (resource["name"], signature(next(iter(resource["origins"].values()))))
            except (OSError, ValueError):
                pass
        if key and key in identical:
            existing = identical[key]
            aliases[resource["id"]] = existing["id"]
            existing["origins"].update(resource["origins"])
            existing["presentIn"] = sorted(set(existing["presentIn"] + resource["presentIn"]))
            for hid, status in resource["compatibility"].items():
                if status["status"] == "present":
                    existing["compatibility"][hid] = status
        else:
            merged.append(resource)
            if key:
                identical[key] = resource
    resources = merged
    for resource in resources:
        resource["components"] = list(dict.fromkeys(aliases.get(rid, rid) for rid in resource["components"]))
    from .resources import overlay

    return {"resources": overlay(resources, base["targets"]) if live else resources, "targets": base["targets"], "errors": errors or []}


def link(resource_id, target, *, revision=None, apply=False):
    if target not in sync.TARGET_NAMES:
        raise ValueError("Unknown destination harness")
    with sync_lock(sync.PERCH_DIR):
        plugins, errors = discover_plugins()
        catalog = inventory(plugins=plugins, errors=errors)
        resource = next((r for r in catalog["resources"] if r["id"] == resource_id), None)
        if resource and resource.get("managed"):
            raise ValueError("Edit this resource in Perch to change its harnesses")
        if not resource or resource["kind"] not in ("skill", "mcp"):
            raise ValueError("Select a skill or MCP component")
        if resource["compatibility"][target]["status"] in ("unsupported", "disabled"):
            raise ValueError(resource["compatibility"][target]["reason"])
        if resource["compatibility"][target]["status"] == "blocked":
            return {"id": resource_id, "target": target, "revision": sync.fingerprint(), "applied": False,
                    **resource["compatibility"][target]}
        if target in resource["presentIn"]:
            return {"id": resource_id, "target": target, "revision": sync.fingerprint(), "applied": apply,
                    "status": "present", "reason": "Already supplied in this harness"}
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


def sharing_sources(plugins=None):
    """Collect active portable components once, retaining native skill destinations."""
    from .skill_sharing import signature

    if plugins is None:
        plugins, errors = discover_plugins()
    else:
        errors = []
    groups, skipped = {}, []
    native, native_mcp, withheld = {}, {}, {"skill": [], "mcp": []}
    linked_sources = {item["name"]: set(item["origins"].values()) for item in sync.skill_inventory()}
    for plugin in plugins:
        if not plugin.get("active", False) or plugin.get("disabled") or plugin.get("scope", "user") != "user":
            for component in plugin["components"]:
                kind, name = component["kind"], component["name"]
                if kind not in withheld:
                    continue
                linked = kind == "skill" and str(Path(component["path"]).resolve()) in linked_sources.get(name, set())
                if plugin.get("disabled") or linked:
                    withheld[kind].append(name)
                    skipped.append({"skill" if kind == "skill" else "server": name,
                                    "reason": "Source plugin is disabled or no longer confirmed enabled; existing connections are preserved"})
            continue
        for component in plugin["components"]:
            kind, name = component["kind"], component["name"]
            if kind not in ("skill", "mcp"):
                continue
            reason = component.get("reason")
            try:
                if kind == "skill":
                    reason = reason or skill_requirement(component["path"])
                    identity = signature(component["path"]) if not reason else None
                    native.setdefault(name, set()).add(plugin["harness"])
                else:
                    native_mcp.setdefault(name, set()).add(plugin["harness"])
                    reason = reason or command_requirement(component["definition"])
                    normalized = sync._norm_server(component["definition"], "plugin") if not reason else None
                    identity = json.dumps(normalized, sort_keys=True)
            except (OSError, ValueError) as exc:
                reason = str(exc)
            if reason:
                skipped.append({"skill" if kind == "skill" else "server": name, "reason": reason})
                continue
            groups.setdefault((kind, name), []).append((identity, component))
    skills, mcp = {}, {}
    for (kind, name), entries in groups.items():
        if len({identity for identity, _ in entries}) > 1:
            withheld[kind].append(name)
            skipped.append({"skill" if kind == "skill" else "server": name, "reason": "Enabled plugin versions differ; choose a source before sharing"})
            continue
        component = entries[0][1]
        (skills if kind == "skill" else mcp)[name] = component["path"] if kind == "skill" else component["definition"]
    return {"skills": skills, "mcp": mcp, "native": {k: sorted(v) for k, v in native.items()}, "native_mcp": {k: sorted(v) for k, v in native_mcp.items()}, "withheld": withheld, "skipped": skipped, "errors": errors}
