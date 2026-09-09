"""Validated settings shared by Perch's CLI and desktop window."""

from __future__ import annotations
import copy
import json
import math
from storage import DATA_DIR, sync_lock, write_json

PATH = str(DATA_DIR / "settings.json")
LAST_SYNC = str(DATA_DIR / "last-sync.json")
DEFAULTS = {
    "watching": {"quietDays": 7},
    "harnesses": {"disabled": []},
    "sharing": {"skills": True, "mcp": True, "targets": ["claude", "codex", "omp"], "autoSync": False},
    "window": {"width": 1280, "height": 820},
    "appearance": {"theme": "system"},
}
TARGETS = ("claude", "codex", "omp", "claude-desktop")


def validate(base: dict, body: dict) -> dict:
    cfg = copy.deepcopy(base)
    if not isinstance(body, dict):
        raise ValueError("Settings must be an object")
    for group in DEFAULTS:
        section = body.get(group, {})
        if not isinstance(section, dict):
            continue
        if group == "watching":
            days = section.get("quietDays")
            if type(days) in (int, float) and math.isfinite(days) and 0.1 <= days <= 90:
                cfg[group]["quietDays"] = days
        elif group == "harnesses":
            disabled = section.get("disabled")
            if isinstance(disabled, list):
                cfg[group]["disabled"] = sorted({x for x in disabled if isinstance(x, str)})
        elif group == "sharing":
            for key in ("skills", "mcp", "autoSync"):
                if isinstance(section.get(key), bool):
                    cfg[group][key] = section[key]
            if isinstance(section.get("targets"), list):
                cfg[group]["targets"] = [x for x in TARGETS if x in section["targets"]]
        elif group == "window":
            for key, low, high in [("width", 860, 5000), ("height", 580, 3000)]:
                value = section.get(key)
                if type(value) is int and low <= value <= high:
                    cfg[group][key] = value
        elif group == "appearance" and section.get("theme") in ("system", "light", "dark"):
            cfg[group]["theme"] = section["theme"]
    return cfg


def load() -> dict:
    try:
        with open(PATH, encoding="utf-8") as fh:
            return validate(DEFAULTS, json.load(fh))
    except (OSError, ValueError):
        return copy.deepcopy(DEFAULTS)


def save(cfg: dict) -> None:
    write_json(PATH, validate(DEFAULTS, cfg))


def patch(cfg: dict, body: dict) -> dict:
    # Reload while holding the same lock used by other Perch processes.
    with sync_lock():
        cfg = validate(load(), body)
        save(cfg)
        return cfg
