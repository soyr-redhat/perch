<p align="center">
  <img src="perch/src/perch/static/icon.svg" width="112" alt="A small bird perched on a branch">
</p>

# Perch

Perch is a desktop integration layer for coding harnesses. Its purpose is to connect context, tools, plugins, and skills across the harnesses you use. Work happens in each harness’s own CLI or desktop app; Perch manages the connections between them.

The intended workflow is to discover a resource once, make it available wherever compatible, and carry context between conversations without rebuilding the setup by hand. Perch opens on Resources. Its conversation reader supports context selection and transfer; execution stays in the harness. An embedded terminal remains available as a compatibility fallback.

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

## Current support

The current source supports:

- Discovering compatible skills and MCP server definitions across installed harnesses.
- Reading Claude plugin installations and Codex plugin caches alongside enablement settings. Enabled native components count as present; unconfirmed cached versions remain separate.
- Linking skills through a shared local registry and adding compatible MCP definitions to native harness configurations.
- Reviewing and connecting individual skills and MCP components to selected harnesses through the same desktop and CLI engine.
- Creating and editing skills and MCP server configurations inside Perch, with selected harness connections and recoverable removal.
- Signing in to compatible OAuth MCP servers in Perch and using the same authorization through its local bridge.
- Browsing recorded Claude Code, Codex, and omp conversations to inspect their context.
- Exporting a complete recording file, transcript, and recognized inline attachments for use in another harness.
- Opening recorded Codex sessions through a native desktop link; destination visibility still needs acceptance testing.
- Opening recorded sessions in external macOS or Windows CLI windows, with platform dispatch covered by tests and native window acceptance still pending.
- Preparing context by choosing a destination or dragging one conversation onto another, then explicitly sending it with a durable transfer receipt.

Transfers attach recorded context through a file reference that the destination harness must be able to read. They start a destination turn when sent; they do not import foreign history as native past turns. Multi-source transfers, selected message ranges, external app dragging, and additional plugin formats remain in [RFC #2](https://github.com/soyr-redhat/perch/issues/2). Published installers may lag the source features described here.

`perch` opens the application. `perch-cli` provides the same command-line actions without Python:

```sh
perch-cli --tools
perch-cli --capabilities
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

Identical skill folders can be consolidated through the shared registry. Perch keeps the authoritative source in place and backs up replaced harness entries. Different contents get an in-app source choice; unrelated entries and MCP configuration remain in place. Plugin packages, hooks, custom agents, and existing harness sign-in state stay with their own harnesses. Automatic sharing includes enabled plugins with instruction-only skills and compatible MCP definitions, without duplicating native plugin skills. Identical plugin skill versions appear once. Skills requiring host connectors, renderers, runtimes, or unreviewed bundled programs remain available in their native plugin but are excluded from automatic transfer. This dependency check is conservative; it does not prove every task described by a skill will work. Missing MCP executables and disabled sources are reported separately, and a configured connection does not certify authentication. Supported components can also be connected individually. Relative MCP paths are resolved within the plugin; unresolved environment variables and host-specific components require an adapter. Harness accounts and execution permissions remain with each harness.

Review one connection with `perch-cli --link RESOURCE_ID --target codex`, using an ID from `--capabilities`. Apply that reviewed change with the same arguments plus `--apply --revision REVISION`. Changes to the source or existing configuration invalidate the review.

### Editing in Perch

Select a skill or MCP server and choose **Edit in Perch**. The first save creates a Perch-owned resource; later edits update it directly. Skills support `SKILL.md` and supporting UTF-8 text files. The editor wraps long lines and highlights Markdown, YAML frontmatter, YAML, and JSON. Tab indents with spaces; Shift+Tab outdents. Escape followed by Tab moves focus out of the editor. **Format** (Option+Shift+F on macOS, Alt+Shift+F on Windows) formats the current document locally and supports Undo. Original external source folders are preserved. Choose the harnesses that should receive the shared version in the same editor.

The **+** menu adds a skill or MCP server. **Remove…** disconnects Perch-managed entries and hides the item from automatic sharing. **+ → Removed items** restores it. Unrelated or externally changed harness entries are preserved. Edits check for concurrent changes and use a recovery journal; interrupted saves are restored before the next mutation. Owned skill versions and backups remain in `~/.perch/managed` and hidden sibling backups.

MCP edits change the server configuration, not its implementation. Managed connections run through the bundled `perch-cli --mcp-bridge` process; the desktop can be closed. Saved environment and header values are masked in the editor and stored in macOS Keychain or Windows Credential Manager. Reconnect existing MCP connections after configuration changes.

For an HTTPS Streamable HTTP server supporting MCP OAuth discovery and dynamic client registration, select **Sign in with OAuth**, save, then choose **Sign in**. Perch opens the provider in your browser and keeps its own authorization in the OS credential store. Selected harnesses use that authorization through the bridge; Perch never copies their existing login sessions. Providers requiring a pre-registered client, legacy SSE, and host-specific connector APIs require further adapters. Real-provider and Windows desktop sign-in acceptance remain outstanding.

### Installation and skill repair

Open **Settings → Installation** in the desktop app to install or repair the local application and optional command-line launchers. macOS launchers are regular files; existing Perch symlinks are migrated without touching their executable. Windows installation repair can restore the installed CLI directory to the user PATH. Unrelated commands are left unchanged. App upgrades preserve the previous macOS bundle; release downloads and automatic in-app updates are not part of this repair control.

**Review sharing** compares complete skill folders, including supporting files and executable permissions. Identical copies use the existing registry source when possible, otherwise an external source folder is preferred. **Choose source…** displays differing files and a bounded `SKILL.md` diff. Selecting a source applies to the destinations enabled in Settings. The optional CLI equivalent is `perch-cli --resolve-skill NAME`, followed by `--skill-source SOURCE_ID` to review that choice and `--apply --revision REVISION` to apply it.

Replaced entries are renamed to hidden sibling backups (`.<name>.perch-backup-<id>`). Migration records live in `~/.perch/skill-migrations`; interrupted changes are restored before another sharing attempt. Backups are retained. Perch does not rewrite embedded harness-specific paths in skill instructions. Trees larger than 100 MB or 10,000 entries, unreadable files, and links outside a skill folder require separate review.

### Context transfers

Choose **Transfer…** in the conversation reader, or drag a conversation onto its destination. Preparing saves the source recording without running a harness. **Send context** starts a turn in the destination; busy destinations are rejected. **Last transfer** restores the most recent receipt after reloading the interface.

The CLI provides the same flow:

```sh
perch-cli --transfer 'claude:SOURCE_ID' --to 'codex:TARGET_ID'
perch-cli --send-transfer TRANSFER_UUID
perch-cli --transfer-status TRANSFER_UUID
```

Sending from the CLI uses the running Perch app. Repeated sends of the same transfer ID do not start another turn. If delivery is interrupted or its outcome cannot be established, Perch records it as uncertain; check the destination before preparing another transfer. The receiving harness keeps its own permissions and may ask to read the saved context.

### Conversation exports

Select **Export…** in a session, or use `--export-session` with an ID from `--sessions`. Both save to `~/.perch/conversations/<export-id>/`. **Copy reference** copies a local file reference to give another harness; **Show files** opens the export folder. Browser development mode offers a ZIP download.

Each export includes the original recording, a transcript in source record order, `manifest.json` with source hashes and attachment availability, and a portable ZIP. It preserves the whole source file, including branches and tool events, without the activity view’s display limits. Malformed or unusually large records remain in the original source and are identified in the manifest. Inline images and documents are extracted where recognized; external file and URL references are listed without fetching them. Other session files and live runtime state are not included.

Exporting does not send a message or merge project files. A local reference works only where the receiving harness can read that folder; use the ZIP when moving it elsewhere.

## Repository layout

```text
perch/
  src/perch/        application, desktop lifecycle, and interface assets
  tests/            unit, runtime, installer, and harness fixtures
  frontend/         editor source, pinned dependencies, and reproducible asset build
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

Editor assets are bundled in the source tree; installed applications need neither Node nor network access for editing. To change the editor or formatter, rebuild its assets and run its tests:

```sh
npm ci --prefix perch/frontend
npm run --prefix perch/frontend build
npm run --prefix perch/frontend test
```

CI checks that generated assets match their source. Formatting loads on demand in a worker; invalid input and interrupted formatting preserve the draft.

## Details

- [Interoperability contract](perch/docs/INTEROPERABILITY.md)
- [Validation coverage and release boundaries](perch/docs/VALIDATION.md)
- [Performance measurements](perch/docs/PERFORMANCE.md)
