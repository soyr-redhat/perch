<p align="center">
  <img src="perch/src/perch/static/icon.svg" width="112" alt="A small bird perched on a branch">
</p>

# Perch

Perch is a native desktop workspace for local coding harnesses. It shows recorded Claude Code, Codex, and omp sessions in one place, opens compatible sessions in an embedded terminal, and uses the same sharing engine from both the desktop app and CLI for skills and MCP servers.

The bird is Perch’s application icon: a small bird on a branch, representing a compact place to watch and move between active sessions. The same SVG supplies the in-app mark and the generated macOS and Windows icons.

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

`perch` opens the application. `perch-cli` provides the same command-line actions without Python:

```sh
perch-cli --tools
perch-cli --dry-run
perch-cli --sync --targets claude codex omp
perch-cli --demo
```

`PERCH_DATA_DIR` changes Perch’s own local state directory (default `~/.perch`). Optional declarative session adapters belong in `~/.perch/sources.json`.

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
