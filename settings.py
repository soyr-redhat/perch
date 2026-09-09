"""Perch user settings, persisted at ~/.perch/settings.json.

Kept deliberately small — the harnesses own their own config; Perch only
needs knobs for what *it* does: watching, per-harness enable, and sharing.
"""

from __future__ import annotations

import json
import os

PATH = os.path.expanduser("~/.perch/settings.json")
LAST_SYNC = os.path.expanduser("~/.perch/last-sync.json")

DEFAULTS = {
    "watching": {"quietDays": 7},
    "harnesses": {"disabled": []},
    "sharing": {"skills": True, "mcp": True, "targets": ["claude", "codex"], "autoSync": False},
}


def load() -> dict:
    cfg = json.loads(json.dumps(DEFAULTS))
    try:
        with open(PATH, "r", encoding="utf-8") as fh:
            saved = json.load(fh)
        for key, val in saved.items():
            if isinstance(val, dict) and isinstance(cfg.get(key), dict):
                cfg[key].update(val)
    except (OSError, json.JSONDecodeError):
        pass
    return cfg


def save(cfg: dict) -> None:
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    tmp = PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=1)
        fh.write("\n")
    os.replace(tmp, PATH)


def patch(cfg: dict, body: dict) -> dict:
    """Validate + apply a partial update; returns the new full config."""
    watching = body.get("watching")
    if isinstance(watching, dict):
        days = watching.get("quietDays")
        if isinstance(days, (int, float)) and 0.1 <= days <= 90:
            cfg["watching"]["quietDays"] = float(days)
    harnesses = body.get("harnesses")
    if isinstance(harnesses, dict) and isinstance(harnesses.get("disabled"), list):
        cfg["harnesses"]["disabled"] = [str(x) for x in harnesses["disabled"]]
    sharing = body.get("sharing")
    if isinstance(sharing, dict):
        for key in ("skills", "mcp", "autoSync"):
            if isinstance(sharing.get(key), bool):
                cfg["sharing"][key] = sharing[key]
        if isinstance(sharing.get("targets"), list):
            cfg["sharing"]["targets"] = [str(x) for x in sharing["targets"] if str(x) in ("claude", "codex")]
    save(cfg)
    return cfg
