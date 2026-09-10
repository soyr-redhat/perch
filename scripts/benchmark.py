"""Reproducible fixture benchmark. Never reads real harness directories."""

import gc
import importlib.util
import json
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time
import tracemalloc
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import scanner  # noqa: E402


def measure(fn, repeat=5):
    samples = []
    for _ in range(repeat):
        start = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - start) * 1000)
    return round(statistics.median(samples), 2)


def main():
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        original = subprocess.check_output(["git", "show", "6c1499e:scanner.py"], cwd=ROOT, text=True)
        module_path = root / "baseline.py"
        module_path.write_text(original)
        spec = importlib.util.spec_from_file_location("baseline_scanner", module_path)
        baseline = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = baseline
        spec.loader.exec_module(baseline)
        directory = root / "sessions"
        directory.mkdir()
        for i in range(1000):
            (directory / f"{i}.jsonl").write_text(json.dumps({"role": "user", "text": f"Session {i}"}) + "\n")
        result = {"fixtureSessions": 1000, "python": sys.version.split()[0], "scan": {}}
        for label, mod in [("before", baseline), ("after", scanner)]:
            adapter = mod.DeclarativeAdapter(
                {
                    "id": "fixture",
                    "patterns": [str(directory / "*.jsonl")],
                    "map": {"role": "role", "text": "text"},
                }
            )
            engine = mod.Scanner()
            engine.adapters = [adapter]
            with patch.object(mod, "scan_processes", return_value={}):
                cold = measure(engine.scan, 1)
                warm = measure(engine.scan)
                identical = engine.scan() == engine.scan()
            result["scan"][label] = {
                "coldMs": cold,
                "warmMedianMs": warm,
                "unchangedSnapshotsEqual": identical,
            }
        transcript = root / "large.jsonl"
        with transcript.open("w") as out:
            for i in range(50000):
                out.write(json.dumps({"role": "user", "text": f"Prompt {i} " + ("x" * 960)}) + "\n")
                out.write(json.dumps({"role": "assistant", "text": "Response " + ("y" * 960)}) + "\n")
        result["transcriptBytes"] = transcript.stat().st_size
        result["history"] = {}
        for label, mod in [("before", baseline), ("after", scanner)]:
            adapter = mod.DeclarativeAdapter({"id": "fixture", "map": {"role": "role", "text": "text"}})
            engine = mod.Scanner()
            stat = transcript.stat()
            parsed = engine._parse_file(adapter, str(transcript), stat.st_mtime, stat.st_size)
            kwargs = {"offset": parsed.prompts[-1]["offset"], "total": 50000} if label == "after" else {}
            gc.collect()
            tracemalloc.start()
            start = time.perf_counter()
            history = mod.read_history(adapter, str(transcript), 49999, **kwargs)
            elapsed = (time.perf_counter() - start) * 1000
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            result["history"][label] = {
                "elapsedMs": round(elapsed, 2),
                "peakAllocatedBytes": peak,
                "returnedEvents": len(history["events"]),
            }
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
