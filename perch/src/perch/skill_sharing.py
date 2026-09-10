"""Content comparison and recoverable changes to harness skill entries."""

import hashlib
import json
import os
from pathlib import Path
import stat
from uuid import uuid4

from .storage import atomic_write, write_json


def tree(path):
    root = Path(path).resolve()
    entries, total = {}, 0
    for base, dirs, files in os.walk(root, followlinks=False):
        for name in sorted(dirs + files):
            item = Path(base) / name
            key = item.relative_to(root).as_posix()
            if len(entries) >= 10000:
                raise ValueError("Skill exceeds 10,000 entries; review it separately")
            before = item.lstat()
            if item.is_symlink() or getattr(item, "is_junction", lambda: False)():
                if not item.resolve().is_relative_to(root):
                    raise ValueError(f"{key}: linked content leaves the skill folder")
                entries[key] = ["link", os.readlink(item)]
            elif stat.S_ISREG(before.st_mode):
                total += before.st_size
                if total > 100 * 1024 * 1024:
                    raise ValueError("Skill exceeds 100 MB; review it separately")
                with item.open("rb") as stream:
                    digest = hashlib.file_digest(stream, "sha256").hexdigest()
                after = item.stat()
                if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
                    raise ValueError("Skill changed while comparing; review again")
                entries[key] = ["file", digest, bool(before.st_mode & 0o111)]
            elif stat.S_ISDIR(before.st_mode):
                entries[key] = ["directory"]
            else:
                raise ValueError(f"{key}: unsupported file type")
        dirs[:] = [d for d in dirs if not is_link(Path(base) / d)]
    if not (root / "SKILL.md").is_file():
        raise ValueError("Skill is missing SKILL.md")
    return entries


def signature(path):
    return hashlib.sha256(json.dumps(tree(path), sort_keys=True).encode()).hexdigest()


def compare(origins):
    return {path: tree(path) for path in sorted(set(origins.values()))}


def choose(origins, trees, registry, roots, selected=None):
    variants = set(origins.values())
    if selected is not None:
        if selected not in variants:
            raise ValueError("Choose a detected source")
        return selected
    if len({json.dumps(t, sort_keys=True) for t in trees.values()}) > 1:
        return None
    existing = os.path.realpath(registry)
    if existing in variants:
        return existing
    # Keep an external source repository authoritative instead of one of its copies.
    def rank(path):
        inside = any(Path(path).is_relative_to(Path(r).resolve()) for r in roots.values())
        return inside, path
    return min(variants, key=rank)


def is_link(path):
    return path.is_symlink() or getattr(path, "is_junction", lambda: False)()


def points_to(path, target):
    if not is_link(path):
        return False
    raw = Path(os.readlink(path))
    actual = os.path.normcase(os.path.abspath(raw if raw.is_absolute() else path.parent / raw)).removeprefix("\\\\?\\")
    return actual == os.path.normcase(os.path.abspath(target)).removeprefix("\\\\?\\")


def _rollback(journal):
    from . import sync

    for op in reversed(journal["operations"]):
        dest, target = Path(op["path"]), Path(op["target"])
        backup = Path(op["backup"]) if op.get("backup") else None
        if backup and not os.path.lexists(backup):
            continue  # The rename never started, or was already restored.
        if os.path.lexists(dest):
            if not points_to(dest, target):
                raise ValueError(f"{dest}: changed after sharing; preserved for manual review")
            sync._remove_managed_link(dest)
        if backup:
            backup.rename(dest)
    manifest = Path(sync.MANIFEST)
    current = manifest.read_text(encoding="utf-8") if manifest.exists() else None
    if current not in (journal["manifestBefore"], journal["manifestAfter"]):
        raise ValueError("Sharing manifest changed during recovery; preserved for review")
    if journal["manifestBefore"] is None:
        manifest.unlink(missing_ok=True)
    else:
        atomic_write(manifest, journal["manifestBefore"])


def recover():
    from . import sync

    for path in sorted((Path(sync.PERCH_DIR) / "skill-migrations").glob("*.json")):
        journal = json.loads(path.read_text(encoding="utf-8"))
        if journal["status"] == "pending":
            _rollback(journal)
            journal["status"] = "restored"
            write_json(path, journal)


def apply(operations):
    """Journal before renaming. Backups stay beside their original entries."""
    from . import sync

    identifier = uuid4().hex
    path = Path(sync.PERCH_DIR) / "skill-migrations" / (identifier + ".json")
    before = Path(sync.MANIFEST).read_text(encoding="utf-8") if Path(sync.MANIFEST).exists() else None
    doc = sync._load_manifest()
    created = {entry.get("link"): entry for entry in doc["created"] if isinstance(entry, dict)}
    for op in operations:
        dest = Path(op["path"])
        op["backup"] = str(dest.with_name(f".{dest.name}.perch-backup-{identifier}")) if os.path.lexists(dest) else None
        created[str(dest)] = {"link": str(dest), "target": op["target"], "kind": "registry"}
    doc["created"] = list(created.values())
    after = json.dumps(doc, indent=2, ensure_ascii=False) + "\n"
    journal = {"status": "pending", "operations": operations, "manifestBefore": before, "manifestAfter": after}
    write_json(path, journal)
    try:
        for op in operations:
            dest = Path(op["path"])
            dest.parent.mkdir(parents=True, exist_ok=True)
            if op["backup"]:
                dest.rename(op["backup"])
            sync._link_dir(op["target"], str(dest), resolve=False)
        atomic_write(sync.MANIFEST, after, expected=(before or "").encode())
        journal["status"] = "complete"
        write_json(path, journal)
    except Exception as exc:
        # Also handles platform-specific link errors. A hard process interruption
        # leaves the pending journal for recover() on the next sharing attempt.
        try:
            _rollback(journal)
            journal["status"] = "restored"
            write_json(path, journal)
        except (OSError, ValueError) as recovery:
            raise ValueError(f"Sharing failed; recovery requires attention: {recovery}") from exc
        raise
    return [op["backup"] for op in operations if op["backup"]]


def review(name, source=None, revision=None, apply_change=False):
    """Only detected paths are selectable; the HTTP client cannot supply a path."""
    import difflib
    from . import settings, sync

    item = next((item for item in sync.skill_inventory() if item["name"] == name), None)
    if not item:
        raise ValueError("Skill is no longer available")
    trees = compare(item["origins"])
    choices = [{"id": hashlib.sha256(path.encode()).hexdigest(), "path": path,
                "harnesses": [h for h, p in item["origins"].items() if p == path]} for path in trees]
    selected = next((c["path"] for c in choices if c["id"] == source), None)
    if source is not None and selected is None:
        raise ValueError("Choose a detected source")
    baseline = selected or choices[0]["path"]
    differences = []
    for path, entries in trees.items():
        if path == baseline:
            continue
        files = sorted(key for key in set(entries) | set(trees[baseline]) if entries.get(key) != trees[baseline].get(key))
        diff = ""
        if any(f.casefold() == "skill.md" for f in files):
            left, right = Path(baseline) / "SKILL.md", Path(path) / "SKILL.md"
            if max(left.stat().st_size, right.stat().st_size) <= 65536:
                diff = "\n".join(difflib.unified_diff(left.read_text(encoding="utf-8", errors="replace").splitlines(), right.read_text(encoding="utf-8", errors="replace").splitlines(), fromfile=baseline, tofile=path, lineterm=""))[:16000]
        differences.append({"path": path, "files": files[:100], "count": len(files), "diff": diff})
    if apply_change and not revision:
        raise ValueError("Review the source choice before applying")
    cfg = settings.load()["sharing"]
    if not cfg["skills"]:
        raise ValueError("Skill sharing is disabled in Settings")
    report = sync.sync_all(skills=True, mcp=False, targets=tuple(cfg["targets"]), dry_run=not apply_change,
                           revision=revision, skill_names={name}, skill_sources={name: selected} if selected else None)
    return {"name": name, "sources": choices, "selected": source, "differences": differences, "report": report}
