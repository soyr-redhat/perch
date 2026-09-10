"""Interactive QA fixture: python perch/tests/serve_fixture.py DIRECTORY."""

import json
from pathlib import Path
import sys
import time
from test_runtime import Workspace


def main():
    root = Path(sys.argv[1]).resolve()
    root.mkdir(parents=True, exist_ok=True)
    workspace = Workspace(root)
    print(f"http://127.0.0.1:{workspace.port}/?token={workspace.server.token}", flush=True)
    try:
        for command in sys.stdin:
            if command.strip() == "stop":
                break
            if command.strip() == "disconnect":
                for info in workspace.server.terms.list():
                    term = workspace.server.terms.get(info["id"])
                    with term._lock:
                        for queue in term._subs:
                            queue.put_nowait(None)
                print("Disconnected terminal streams; fixture processes remain running", flush=True)
            if command.strip() == "burst":
                for index in range(20):
                    with (root / "sessions" / "session-one.jsonl").open("a") as out:
                        out.write(json.dumps({"id": "session-one", "cwd": str(root), "role": "assistant", "text": f"Burst update {index}"}) + "\n")
                    workspace.server.RequestHandlerClass.get_snapshot(workspace.scanner.scan())
                    time.sleep(.1)
                print("Published 20 snapshots", flush=True)
    finally:
        workspace.close()


if __name__ == "__main__":
    main()
