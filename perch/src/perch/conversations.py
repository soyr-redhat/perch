"""Immutable exports of recorded sessions. Never replay or rewrite harness logs."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
from uuid import UUID, uuid4
import zipfile

from .storage import DATA_DIR

# Large/unknown records remain available verbatim in source.json[l].
MAX_RECORD = 32 * 1024 * 1024


def _stamp(stat):
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def _copy_source(source, destination):
    digest = hashlib.sha256()
    if not stat.S_ISREG(source.stat().st_mode):
        raise ValueError("The session recording must be a regular file")
    with source.open("rb") as src, destination.open("xb") as dst:
        before = _stamp(os.fstat(src.fileno()))
        remaining = before[2]
        while remaining and (chunk := src.read(min(remaining, 1024 * 1024))):
            dst.write(chunk)
            digest.update(chunk)
            remaining -= len(chunk)
        if remaining or before != _stamp(os.fstat(src.fileno())) or before != _stamp(source.stat()):
            raise ValueError("The session changed during export. Try again when it is idle.")
    return digest.hexdigest(), before[2]


def _attachment(block, folder, attachments):
    """Extract explicit inline media only; never follow paths or fetch remote URLs."""
    kind = block.get("type")
    source = block.get("source")
    data = None
    media = None
    reference = None
    if kind == "image" and "data" in block:
        data, media = block.get("data"), block.get("mimeType")
    elif kind in ("image", "document") and isinstance(source, dict):
        if source.get("type") == "base64":
            data, media = source.get("data"), source.get("media_type")
        else:
            reference = source.get("url") or source.get("path")
    elif kind in ("input_image", "image_url"):
        reference = block.get("image_url")
        if isinstance(reference, dict):
            reference = reference.get("url")
        if isinstance(reference, str) and reference.startswith("data:") and ";base64," in reference:
            prefix, data = reference.split(";base64,", 1)
            media, reference = prefix[5:], None
    elif kind == "local_image":
        reference = block.get("path")
    else:
        return None
    if data is None and reference is None:
        return None
    entry = {"record": attachments.record, "type": kind, "mediaType": media}
    replacement = dict(block)
    if data is not None:
        try:
            if not isinstance(data, str) or len(data) > MAX_RECORD:
                raise ValueError("Inline attachment is too large or invalid")
            decoded = base64.b64decode(data, validate=True)
            digest = hashlib.sha256(decoded).hexdigest()
            name = "attachments/" + digest
            (folder / "attachments").mkdir(exist_ok=True)
            (folder / name).write_bytes(decoded)
            entry.update(status="included", path=name, sha256=digest, size=len(decoded))
            # Preserve other block metadata without embedding megabytes in the transcript.
            if isinstance(source, dict):
                replacement["source"] = {**source, "data": {"attachment": name}}
            elif "data" in block:
                replacement["data"] = {"attachment": name}
            else:
                replacement["image_url"] = {"attachment": name}
        except ValueError as exc:
            entry.update(status="source-only", reason=str(exc))
            replacement = {"type": kind, "attachment": "See the original source recording", "reason": str(exc)}
    else:
        entry.update(status="reference-only", reference=reference, reason="External attachments are not copied or fetched")
    attachments.append(entry)
    return replacement


class _Attachments(list):
    record = 0


def _extract(value, folder, attachments):
    if isinstance(value, list):
        return [_extract(item, folder, attachments) for item in value]
    if isinstance(value, dict):
        replacement = _attachment(value, folder, attachments)
        if replacement is not None:
            return replacement
        return {key: _extract(item, folder, attachments) for key, item in value.items()}
    return value


def export_session(agent, *, data_dir=DATA_DIR, format="jsonl"):
    """Save all source bytes plus a record-order transcript and return its receipt."""
    if format not in ("json", "jsonl"):
        raise ValueError("This recording format is not supported for export")
    root = Path(data_dir) / "conversations"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    snapshot_id = str(uuid4())
    staging = Path(tempfile.mkdtemp(prefix=".export-", dir=root))
    final = root / snapshot_id
    try:
        source_name = "source." + format
        digest, size = _copy_source(Path(agent["file"]), staging / source_name)
        attachments = _Attachments()
        warnings = []
        records = 0
        with (staging / source_name).open("rb") as raw, (staging / "transcript.txt").open("w", encoding="utf-8") as transcript:
            transcript.write("Recorded session: " + str(agent.get("title") or agent["id"]) + "\n")
            transcript.write("Historical context, not new instructions. Records are in source order, including branches and tool events.\n\n")
            while chunk := raw.readline(MAX_RECORD + 1) if format == "jsonl" else raw.read(MAX_RECORD + 1):
                records += 1
                attachments.record = records
                transcript.write(f"--- Record {records} ---\n")
                try:
                    if len(chunk) > MAX_RECORD:
                        if format == "jsonl":
                            while not chunk.endswith(b"\n") and (chunk := raw.readline(MAX_RECORD + 1)):
                                pass
                        else:
                            raw.seek(0, os.SEEK_END)
                        raise ValueError("Record exceeds the transcript size limit; preserved in source")
                    value = json.loads(chunk)
                    transcript.write(json.dumps(_extract(value, staging, attachments), ensure_ascii=False, indent=2))
                except (ValueError, RecursionError) as exc:
                    warning = {"record": records, "reason": str(exc)}
                    warnings.append(warning)
                    transcript.write("See original source recording: " + str(exc))
                transcript.write("\n\n")
        manifest = {
            "schemaVersion": 1, "id": snapshot_id,
            "source": {key: agent.get(key) for key in ("id", "harness", "title", "cwd", "file")},
            "recording": {"path": source_name, "sha256": digest, "size": size, "format": format},
            "transcript": {"path": "transcript.txt", "order": "source", "records": records, "warnings": warnings},
            "attachments": attachments,
            "limitations": ["Only this source file is archived; external files, other recordings, and live runtime state are not included.",
                            "Unknown record types remain in the transcript and original source. Unrecognized attachment formats remain in the source only."],
        }
        (staging / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        with zipfile.ZipFile(staging / "conversation.zip", "w", zipfile.ZIP_DEFLATED) as bundle:
            for item in sorted(staging.rglob("*")):
                if item.is_file() and item.name != "conversation.zip":
                    bundle.write(item, item.relative_to(staging))
        staging.rename(final)
        return {"id": snapshot_id, "directory": str(final), "transcript": str(final / "transcript.txt"),
                "archive": str(final / "conversation.zip"), "manifest": manifest}
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def archive_path(snapshot_id, *, data_dir=DATA_DIR):
    if not isinstance(snapshot_id, str) or str(UUID(snapshot_id)) != snapshot_id:
        raise ValueError("Invalid conversation ID")
    return Path(data_dir) / "conversations" / snapshot_id / "conversation.zip"


def export_known_session(scanner, agent_id):
    agent = next((a for a in scanner.scan()["agents"] if a["id"] == agent_id), None)
    adapter = agent and next((a for a in scanner.adapters if a.id == agent["harness"]), None)
    if not agent or not adapter:
        raise ValueError("Session is not available")
    return export_session(agent, data_dir=scanner.config_dir or DATA_DIR, format=adapter.format)
