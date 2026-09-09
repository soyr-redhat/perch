# Pre-merge verification

This audit covers the desktop rebuild on `feat/desktop-workspace`, including the vertical prompt rail. It does not approve merging or claim that every external harness feature is portable.

## Reproducible checks

Run `python -m unittest discover -v`, `ruff check .`, and `node --check static/app.js`. The suite currently contains 64 tests. Runtime tests use temporary configurations and a local fixture CLI that never contacts a model. The same tests run on macOS and Windows in GitHub Actions.

`scripts/check_packaged.py <packaged-cli>` exercises the built executable, second-launch activation, authenticated backend, WebSocket input, a real terminal child, recorded output, headless replies, and terminal cleanup. This catches runtime failures that an import or `--version` check misses.

| Area | Coverage |
|---|---|
| Session parsing | Claude/Codex/omp representative records; multiple text blocks; context scaffolding; malformed records; partial JSONL appends; file replacement; declarative source validation |
| Performance contracts | Unchanged files are not reopened; unchanged snapshots compare equal; indexed history reaches first, middle, and latest prompts, including beyond the old history limit |
| Sharing | CLI preview to desktop apply; additive writes; TOML comments and unrelated fields; byte-preserving backups; conflicting names; unsupported transport fields; invalid destinations; stale previews; idempotence; corrupt manifests; retry after reported I/O failure |
| Process lifecycle | Real POSIX PTY/Windows ConPTY fixture processes; controlling terminal on POSIX; duplicate resume reuse; no simultaneous headless reply and resumed terminal for one owned session; stop/cleanup |
| Transport | Authentication, Host/Origin restrictions, malformed input, native-only bridge policy, bounded frame size, fragmentation with ping, input framing, reconnection/backlog replay |
| Settings | Validation, persisted changes, disabled harness behavior |

## Interactive verification

An isolated production-server fixture was operated through the UI. Verified: first/latest prompt navigation across 160 prompts; drafts across session changes; typing and focus during 20 snapshots at 10 Hz; terminal input/output; forced socket disconnect followed by reconnection to the same PID; search focus during terminal updates; stop confirmation and removal; sharing preview/apply and a second preview with no additions; settings persistence; theme changes.

A rebuilt macOS demo `.app` was also operated as a native application. Verified: application window, Workspace menu dispatch, native folder dialog and returned path, and copying handoff context then pasting it through the macOS Edit menu. Demo mode cannot write harness configurations or launch model sessions.

## Defects found and corrected

- Multi-block Claude/Codex messages could lose text or shift prompt indexes. Parsing now preserves the full user request as one prompt.
- Malformed declarative metadata could become whole record objects. Invalid optional fields are isolated.
- A corrupt sharing manifest could allow links before bookkeeping failed. Preflight now prevents that partial operation; later failures are reported explicitly.
- MCP transport-specific fields could be silently discarded. Incompatible conversion is blocked.
- Windows backup text could change CRLF line endings. Backups preserve original bytes for valid UTF-8 configuration.
- CLI sync failures could still return success. Partial failures, conflicts, and blocked mappings return a nonzero status.
- Option-like session identifiers could become harness flags; option-like prompts could be interpreted as options. Identifiers are rejected and built-in prompt arguments use an option terminator.
- Terminal disconnects could leave a frozen pane; fragmented messages could lose data around pings. Reconnection and framing were corrected.
- Concurrent snapshots could remove a newly spawned terminal or restore stale state. Snapshots now carry instance-scoped revisions and current process state.
- Updates could steal terminal/search focus or discard a draft edited while sending. UI ownership and draft clearing were corrected.
- POSIX children lacked a controlling terminal, and shutdown could leave descendants after the leader exited. The spawn/termination paths were corrected.
- Reopening a session could launch competing harness processes. Existing owned terminals are reused and conflicting headless delivery is rejected.
- macOS filesystem event handling crashed during repeated integration fixtures. macOS uses polling observation with cached parsing; startup failure elsewhere also falls back to polling.
- Packaged external processes could inherit bundled library settings. External launches now sanitize those settings; Windows process discovery runs without an interactive console.
- Windows PTY temporary empty reads could end the output pump. They are retried while the process remains alive.
- pywinpty 3.0.5 passed source tests but failed terminal input/output after freezing, including a build that explicitly bundled its console helper. The Windows dependency is pinned to 2.0.15, which passed the packaged test. A 3.x upgrade must pass that same gate before adoption; its underlying frozen-runtime failure has not been fully diagnosed.
- Automatic sharing stopped retrying after a reported I/O failure. Failed operations now retry on the next scheduled pass.
- Malformed non-ASCII authentication tokens could abort the HTTP request. They now receive a normal rejection.
- The desktop content policy blocked pywebview's dynamic bridge, breaking menus and the folder picker. Desktop mode now permits the evaluation required by the installed bridge while browser mode keeps the stricter script policy.
- Inferred inactivity was labeled as a request for input. It now reads as Idle; actual approval/question states require harness events.

## Boundaries and remaining release checks

- Windows installer interaction, WebView2 integration, clipboard, and native dialogs need a Windows desktop acceptance run; CI process checks do not cover those GUI behaviors.
- Windows development/build verification uses Python 3.12. Packaged users do not need to install Python. The compatible 2.x PTY runtime has slower reads than 3.x; Windows throughput/latency benchmarking remains outstanding.
- Packaged Windows terminal verification is a merge gate. See the latest Actions result and the dated user-facing report for its current status.
- Builds have no publisher signing or macOS notarization. The macOS CI artifact targets Apple Silicon; Intel coverage is not established.
- Real authenticated Claude/Codex/omp conversations were not sent during this audit. Installed CLI help and representative recordings were checked; provider authentication, approval prompts, and changes in harness versions still need acceptance coverage.
- Session state remains inferred for externally launched harnesses. Process disappearance or inactivity does not prove success, failure, or a pending approval.
- Bounded replay is not an exact serialized terminal screen. Sustained producer flow control and slow network-client stress testing are follow-up work.
- Headless reply stderr is captured until completion; sustained excessive stderr remains a resource-hardening follow-up.
- MCP sharing is additive per destination, not one transaction across all files. The preview and per-file checks cannot lock out edits by unrelated tools for the entire operation.
- Skills/MCP compatibility does not imply transferable OAuth sessions, plugins, hooks, agent definitions, or private conversation state. See `INTEROPERABILITY.md`.

The performance results in `PERFORMANCE.md` are synthetic fixture measurements, not measured end-to-end latency across every user's machine.
