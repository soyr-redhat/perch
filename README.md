# Perch

A desktop workspace for local coding agents. Follow sessions started in your CLI or desktop harness, resume work in an embedded terminal, and share compatible skills and MCP servers across tools.

## Development

Python 3.11 or newer is required. A packaged application bundles Python.

```sh
python -m venv .venv
# macOS
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python perch.py
# Windows (PowerShell)
.venv\Scripts\python -m pip install -e '.[dev]'
.venv\Scripts\python perch.py
```

Desktop mode opens an application window. It never silently falls back to a browser. `--browser` is an explicit development mode. Windows uses WebView2 and ConPTY; macOS uses WKWebView and POSIX PTYs.

```sh
perch --tools                 # inventory, without credentials in the output
perch --dry-run               # review compatible sharing additions
perch --sync                  # apply compatible additions
perch --sync --targets claude codex omp
perch --demo                  # synthetic sessions; no harness configuration writes
```

The installed packages also include `perch-cli`: on macOS it is inside
`Perch.app/Contents/MacOS/perch-cli`; on Windows it is `perch-cli.exe` in the
installation directory. It accepts the same flags and can be added to your shell's
PATH without installing Python. Running it without flags opens or activates Perch.

CLI and desktop sharing use the same settings and synchronization engine. `PERCH_DATA_DIR` overrides Perch's own storage (default `~/.perch`); it does not relocate the harnesses. Declarative session adapters belong in `~/.perch/sources.json`.

## Sharing behavior

Use **Shared tools → Review sharing → Apply additions**. Choose receiving harnesses in Settings. Optional automatic sharing applies compatible additions every 30 seconds while Perch runs.

- Skills are linked to their original directory, so edits stay shared. Different same-name sources are reported as conflicts.
- MCP servers are added only when absent. Existing definitions, comments, disabled flags, timeouts, and unrelated configuration are preserved.
- Each MCP file gets a backup before an atomic replacement. Perch serializes its own writes and rejects a destination changed during preparation. Multi-file changes are not a transaction; failures are reported per destination.
- Unsupported transports, environment substitution syntax, and client-specific fields are reported for review rather than silently discarded.
- Codex CLI and desktop use their existing shared configuration. Claude Desktop has a separate local stdio MCP adapter.
- OAuth logins, plugin installations, custom agents, and hooks remain harness-owned. Perch does not copy login tokens or pretend these formats are interchangeable. Custom tool commands are portable when exposed as stdio MCP servers.
- Handoff copies reviewed recent session context for use in another harness. It does not transfer a model's hidden state or merge conversation IDs.

See [the interoperability contract](docs/INTEROPERABILITY.md) for source paths and boundaries.

## Build and test

```sh
python -m unittest discover -v
ruff check .
node --check static/app.js
python -m PyInstaller packaging/perch.spec --noconfirm
```

Build on the target OS. macOS produces `dist/Perch.app`. Windows produces `dist/Perch/Perch.exe`; compile `packaging/windows.iss` with Inno Setup to make the per-user installer. Public macOS distribution needs Developer ID signing and notarization; Windows release signing is likewise separate from the development build.

See [pre-merge verification](docs/VALIDATION.md) for exercised workflows, audit fixes, and remaining platform acceptance checks.

The local transport requires a per-launch token, checks Host and Origin, and binds only to loopback. The desktop bootstrap handles authentication automatically. Perch owns and stops only the agent processes it starts. Session status is inferred from recorded activity, not a guaranteed real-time view of an external process's internal state.
