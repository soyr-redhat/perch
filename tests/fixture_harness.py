"""Local fixture CLI. Never contacts a model or uses a real harness configuration."""

import json
import os
from pathlib import Path
import sys
import time


def main():
    root = Path(sys.argv[1])
    (root / "fixture-started.json").write_text(json.dumps({"pid": os.getpid(), "argv": sys.argv}))
    args = sys.argv[2:]
    session = args[1] if args and args[0] in ("--resume", "--message") else "new-session"
    path = root / "sessions" / (session + ".jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)

    def record(role, text):
        with path.open("a", encoding="utf-8") as out:
            out.write(json.dumps({"id": session, "cwd": str(root), "role": role, "text": text}) + "\n")

    if args and args[0] == "--message":
        text = args[2]
        if text == "__FAIL__":
            print("Fixture delivery failed", file=sys.stderr)
            return 7
        if text == "__SLOW__":
            time.sleep(1)
        record("user", text)
        record("assistant", "Fixture reply: " + text)
        return 0
    print("FIXTURE_READY", os.getpid(), flush=True)
    for line in sys.stdin:
        text = line.strip()
        if text == "exit":
            break
        record("user", text)
        record("assistant", "Fixture reply: " + text)
        print("Fixture reply: " + text, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
