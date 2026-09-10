<p align="center">
  <img src="perch/src/perch/static/icon.svg" width="112" alt="A small bird perched on a branch">
</p>

# Perch

Perch is a native desktop workspace for local coding harnesses. It shows recorded Claude Code, Codex, and omp sessions in one place, opens compatible sessions in an embedded terminal, and uses the same sharing engine from both the desktop app and CLI for skills and MCP servers.

## Install

```sh
# Apple silicon macOS
curl -fsSL https://raw.githubusercontent.com/soyr-redhat/perch/main/perch/install.sh | sh
```

```powershell
# Windows PowerShell
irm https://raw.githubusercontent.com/soyr-redhat/perch/main/perch/install.ps1 | iex
```

Installers download a GitHub Release asset and verify its SHA-256 checksum. macOS installs `~/Applications/Perch.app` and creates `perch` and `perch-cli` links in `~/.local/bin`. Windows installs to `%LOCALAPPDATA%\Programs\Perch` and adds it to the user PATH.

The macOS release targets Apple silicon. Gatekeeper may require confirmation until signed and notarized releases are available.

## What it does

- Reads local session recordings without moving them or exposing them to a remote service.
- Shows session activity and prompt history in a desktop window.
- Opens supported harness sessions in an embedded terminal.
- Lets you continue a compatible recorded session without leaving the desktop app.
- Inspects and shares compatible skills and MCP server definitions across installed harnesses.
- Exports a session recording, transcript, and inline attachments for use in another harness.
- Opens recorded Codex sessions in its desktop app through a native link.

`perch` opens the application. `perch-cli` provides the same command-line actions without Python:

```sh
perch-cli --tools
perch-cli --dry-run
perch-cli --sync --targets claude codex omp
perch-cli --sessions
perch-cli --export-session 'claude:SESSION_ID'
perch-cli --demo
```

`PERCH_DATA_DIR` changes Perch’s own local state directory (default `~/.perch`). Optional declarative session adapters belong in `~/.perch/sources.json`.

Shared tool state lives alongside it:

- `~/.perch/shared/skills/<name>` links each compatible skill to its detected source. Perch-managed harness skill directories link through this location, so an edit is immediately shared.
- `~/.perch/shared/mcp/servers.json` stores portable MCP definitions. Perch writes compatible entries into each harness’s native configuration when you apply sharing.

Existing user-managed links and configuration remain in place. Plugin installations, hooks, custom agents, and sign-in state stay with their own harnesses.

### Conversation exports

Select **Export…** in a session, or use `--export-session` with an ID from `--sessions`. Both save to `~/.perch/conversations/<export-id>/`. **Copy reference** copies a local file reference to give another harness; **Show files** opens the export folder. Browser development mode offers a ZIP download.

Each export includes the original recording, a transcript in source record order, `manifest.json` with source hashes and attachment availability, and a portable ZIP. It preserves the whole source file, including branches and tool events, without the activity view’s display limits. Malformed or unusually large records remain in the original source and are identified in the manifest. Inline images and documents are extracted where recognized; external file and URL references are listed without fetching them. Other session files and live runtime state are not included.

Exporting does not send a message or merge project files. A local reference works only where the receiving harness can read that folder; use the ZIP when moving it elsewhere. Automated delivery and drag-to-transfer are tracked in [RFC #2](https://github.com/soyr-redhat/perch/issues/2).

## Repository layout

```text
perch/
  src/perch/        application, desktop lifecycle, and interface assets
  tests/            unit, runtime, installer, and harness fixtures
  packaging/        PyInstaller and Windows installer definitions
  scripts/          packaged-app and performance checks
  docs/             interoperability, validation, and performance details
  install.sh        macOS installer
  install.ps1       Windows installer
```

## Develop

Python 3.11 or newer is required for development. Packaged applications include their runtime.

```sh
python -m venv .venv
.venv/bin/python -m pip install -e './perch[dev]'
.venv/bin/python -m perch
```

On Windows PowerShell, use `.venv\Scripts\python` in place of `.venv/bin/python`.

```sh
.venv/bin/python -m unittest discover -s perch/tests -v
.venv/bin/ruff check perch
node --check perch/src/perch/static/app.js
.venv/bin/python -m PyInstaller perch/packaging/perch.spec --noconfirm
```

## Details

- [Interoperability contract](perch/docs/INTEROPERABILITY.md)
- [Validation coverage and release boundaries](perch/docs/VALIDATION.md)
- [Performance measurements](perch/docs/PERFORMANCE.md)
