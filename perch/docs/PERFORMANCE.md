# Performance validation

Synthetic benchmark on the development Mac with Python 3.14.3, compared against the initial repository commit. Run `python scripts/benchmark.py` from the `perch` project directory to reproduce. No live agent files are read.

| Operation | Initial implementation | Updated implementation |
|---|---:|---:|
| First scan, 1,000 one-record session files | 31.91 ms | 27.84 ms |
| Median unchanged scan, same 1,000 files | 24.11 ms | 3.06 ms |
| Last-prompt history lookup, 100.2 MB JSONL after indexing | 1,401.27 ms | 0.21 ms |
| Peak Python allocation during that history lookup | 110.3 MB | 0.142 MB |
| Consecutive unchanged snapshots compare equal | No | Yes |

These are fixture timings, not end-to-end UI latency or a promise about every machine. History timings include tracemalloc instrumentation and exclude the initial index-building scan. The new lookup uses a recorded byte offset. Full-document JSON adapters still need to parse a changed document; JSONL supports incremental reads. Filesystem discovery is cached, file events trigger prompt refreshes, and periodic reconciliation catches missed events.

Verification also checks that unchanged files are not reopened, partial JSONL records are retried, replacement files reset cached state, invalid records do not freeze other sessions, and session drafts survive UI selection changes.

macOS uses a two-second polling observer after native FSEvents crashed during repeated runtime fixtures. Cached parsing and periodic reconciliation still apply. Observer startup failures on other platforms also fall back to polling.
