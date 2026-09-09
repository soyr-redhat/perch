"""Deterministic, synthetic content for desktop and browser smoke tests."""

import copy
import threading
import time
from types import SimpleNamespace
import settings


class DemoScanner:
    def __init__(self):
        self.changed = threading.Event()
        self.config_dir = "(demo)"
        self.adapters = [
            SimpleNamespace(id=i, name=n, color=c, cmd=["demo"], message=["demo"], enabled=True, patterns=[])
            for i, n, c in [
                ("claude", "Claude Code", "#b67453"),
                ("codex", "Codex", "#4689a8"),
                ("omp", "omp", "#8d77ba"),
            ]
        ]
        now = time.time()
        self.agents = []
        rows = [
            ("Polish the session workspace", "working", "claude", "perch"),
            ("Review the sharing adapters", "waiting", "codex", "perch"),
            ("Add terminal lifecycle tests", "quiet", "omp", "perch"),
            ("Investigate inference latency", "working", "codex", "model-serving"),
            ("Update deployment recipes", "waiting", "claude", "model-serving"),
            ("Prepare the release notes", "quiet", "omp", "developer-tools"),
        ]
        for i, (title, status, hid, project) in enumerate(rows):
            age = {"working": 3, "waiting": 140, "quiet": 7200}[status]
            self.agents.append(
                {
                    "id": f"{hid}:demo-{i}",
                    "harness": hid,
                    "title": title,
                    "state": status,
                    "cwd": f"/projects/{project}",
                    "file": "demo",
                    "started": now - 1800,
                    "updated": None,
                    "mtime": now - age,
                    "model": "example-model",
                    "tokens": 48200 + i * 1370,
                    "prompts": [
                        {"index": 0, "text": "Make the workspace feel at home on the desktop", "ts": None},
                        {"index": 1, "text": "Preserve the session when I switch tools", "ts": None},
                    ],
                    "tail": [
                        {
                            "who": "user",
                            "text": "Make the workspace feel at home on the desktop. I want to move between agents without losing track of the work.",
                            "ts": None,
                        },
                        {
                            "who": "assistant",
                            "text": "I’m simplifying the workspace around the project and the conversation. Sessions will stay in the sidebar, with terminals one click away.\n\nThe shared tools view will show exactly where each skill and MCP server is available.",
                            "ts": None,
                        },
                        {"who": "tool", "text": "Read · static/app.js, scanner.py, sync.py", "ts": None},
                        {
                            "who": "assistant",
                            "text": "The session list now keeps its selection as updates arrive. Your draft and scroll position stay where you left them.\n\nNext I’m checking terminal reconnects and the sharing preview.",
                            "ts": None,
                        },
                    ],
                }
            )

    def scan(self):
        return {
            "demo": True,
            "agents": copy.deepcopy(self.agents),
            "harnesses": [
                {
                    "id": a.id,
                    "name": a.name,
                    "color": a.color,
                    "canSpawn": True,
                    "canResume": True,
                    "canMessage": True,
                }
                for a in self.adapters
            ],
            "watching": [{"id": a.id, "name": a.name, "files": 2, "processes": 1} for a in self.adapters],
            "errors": [],
        }

    def history(self, agent_id, pidx):
        return {"events": self.agents[0]["tail"], "prompt": pidx, "of": 2}

    def close(self):
        pass

    def apply_settings(self, cfg):
        pass


def tools():
    return {
        "skills": [
            {"name": n, "presentIn": p}
            for n, p in [
                ("code-review", ["claude", "codex", "omp"]),
                ("deployment-recipes", ["claude", "codex"]),
                ("project-conventions", ["codex"]),
                ("release-notes", ["claude", "omp"]),
            ]
        ],
        "mcp": [
            {"name": n, "transport": t, "presentIn": p, "disabledIn": []}
            for n, t, p in [
                ("github", "stdio", ["claude", "codex", "omp"]),
                ("documentation", "HTTP", ["codex"]),
                ("project-files", "stdio", ["claude", "omp", "claude-desktop"]),
            ]
        ],
        "targets": [
            {"id": i, "name": n, "skills": i != "claude-desktop"}
            for i, n in [
                ("claude", "Claude Code"),
                ("codex", "Codex CLI & desktop"),
                ("omp", "omp"),
                ("claude-desktop", "Claude Desktop"),
            ]
        ],
        "notes": [
            "Codex CLI and desktop share their user configuration.",
            "Skill links share edits immediately. Restart a harness if it does not refresh tools.",
            "OAuth sign-ins, plugin installations, hooks and custom agents remain owned by each harness.",
            "This preview contains synthetic data. No agent configurations are read or modified.",
        ],
    }


def config(scanner):
    return {
        "settings": copy.deepcopy(settings.DEFAULTS),
        "harnesses": [
            {"id": a.id, "name": a.name, "installed": True, "enabled": True} for a in scanner.adapters
        ],
        "mcp": tools()["mcp"],
        "lastSync": None,
        "files": {},
    }
