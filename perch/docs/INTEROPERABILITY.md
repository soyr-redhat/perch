# Interoperability contract

Perch connects context and compatible capabilities across harnesses. Harnesses remain responsible for their own conversation interface, execution, accounts, and permissions. Perch's reader supports inspecting and transferring context; its target workflow routes work into native harness tools.

| Harness | Sessions | Skills | MCP |
|---|---|---|---|
| Claude Code | `~/.claude/projects/*/*.jsonl` | `~/.claude/skills` | `~/.claude.json` |
| Codex CLI and desktop | `~/.codex/sessions/**/*.jsonl` | `~/.agents/skills`, legacy sources in `~/.codex/skills` | `$CODEX_HOME/config.toml` or `~/.codex/config.toml` |
| omp | `~/.omp/agent/sessions/*/*.jsonl` | `~/.omp/agent/skills` | `~/.omp/agent/mcp.json` |
| Claude Desktop | No private transcript adapter | No compatible user-directory contract | platform Claude `claude_desktop_config.json`; stdio only |

Claude Desktop's JSON is under `~/Library/Application Support/Claude` on macOS and `%APPDATA%/Claude` on Windows. Perch also discovers legacy Claude MCP definitions in `~/.claude/mcp.json`.

Perch keeps portable shared state in `~/.perch/shared`. Each `skills/<name>` entry is a link to the detected canonical skill directory, and Perch-managed entries in native skill roots link through it. `mcp/servers.json` stores normalized portable MCP definitions; applying sharing adds compatible definitions to the destination harness’s native configuration. Identical skill copies and existing skill links can be consolidated through the registry, with sibling backups for replaced entries. Differing content requires an explicit source choice in Perch. The physical authoritative source and unrelated configuration stay in place; Perch does not normalize harness-specific paths inside skill instructions.

MCP conversion supports the portable core: `command`, string `args`, string-valued `env`, HTTP `url`, and static headers. Codex `http_headers` maps to JSON clients' `headers`. HTTP types are explicit for JSON clients. Disabled definitions remain disabled in their existing client and are not used as sources. Different definitions with the same name block propagation of that name. Unknown fields, SSE/WebSocket transports, client-specific OAuth settings, and environment interpolation syntax block automatic conversion.

Static values deliberately configured in an MCP definition, including headers and environment values, are part of the selected sharing operation. They are never displayed in tool inventory or reports. Existing harness OAuth credential stores are never read or copied. A copied remote server may still require sign-in in the destination client.

Plugins can contain platform-specific code, commands, environment assumptions, and approval rules. Copying their installation directories across harnesses is not a supported interchange format. Current sharing covers compatible skills and MCP configuration; plugin installations, hooks, agent definitions, and sign-in state remain harness-owned.

The resource inventory reads Claude's user installation index and enablement settings, plus Codex's plugin cache and configured plugin enablement. A unique cached version matching an enabled Codex package is counted as supplied natively; multiple versions remain unconfirmed. Explicitly disabled and project-scoped packages are excluded from global automatic sharing. Unconfirmed cache entries are labelled cached. It identifies skills, MCP definitions, apps, hooks, agents, commands, and LSP components. Selected skill and MCP connections use the existing sharing engine; package installation and host-specific components are not converted. Plugin-relative MCP paths are resolved within the plugin directory, and malformed manifests produce individual discovery errors. omp package/plugin discovery is not implemented yet.

Automatic sharing includes enabled plugin components with compatible MCP definitions and instruction-only skills. It preserves native plugin delivery instead of creating duplicate skill entries. Identical full skill trees are grouped; differing enabled sources are withheld. Skill instructions and Markdown references are checked for known host tool, renderer, plugin, and runtime dependencies. Non-text bundled files and unknown programs require dependency review. These are conservative local checks, not runtime capability attestation. Existing skill connections are preserved, and explicit shared-source review still supports selecting a whole folder with supporting files. MCP commands are checked on Perch's PATH without executing them. Presence and authentication are separate: native client-specific settings can work in their own harness even when transfer is blocked. Sync revisions include eligible plugin content and enablement; changes invalidate previewed additions.

Context transfers preserve the source snapshot and send a reference into a compatible recorded destination using its message adapter. Preparation does not run a harness. Sending explicitly starts a destination turn; it is not native role-history import. Receipts prevent repeat delivery of the same transfer ID, reject busy destinations, and preserve uncertain outcomes after interruptions. Real authenticated harness acceptance, native forks/imports, range/multi-source transfers, and external app dragging remain tracked in [RFC #2](https://github.com/soyr-redhat/perch/issues/2).

## Perch-managed editing and authorization

Editing a detected resource creates a Perch-owned version and records ownership of each selected native connection. Skill roots continue to link through the shared registry. MCP connections use a local stdio bridge for stdio or Streamable HTTP upstream servers, including Claude Desktop's stdio configuration. Tools, resources, prompts, notifications, and protocol requests pass through the bridge; server implementation code remains external.

The managed catalog is `~/.perch/managed/resources.json`. Skill versions are immutable directories under `managed/skills`. Revision checks cover the catalog and discovered configuration; journaled replacement backs up old entries and restores interrupted changes. Deleted records suppress automatic re-import and can be restored. Unrelated entries or definitions changed outside Perch are not removed. External programs do not participate in Perch's lock, so detected races fail for review.

Perch-owned OAuth uses the pinned MCP Python SDK's discovery, dynamic client registration, authorization-code and PKCE flow. Tokens, client metadata, headers, and environment values use OS credential storage, with bounded entries for Windows. Loopback callbacks validate state; authorization endpoints require HTTPS. Auth state is scoped to the Perch profile, resource ID, and server URL. Token refresh is serialized across bridge processes. Sign-out clears Perch tokens; it does not revoke provider sessions or close another application's account.

The bridge starts on demand and does not depend on the desktop server. Existing upstream processes must reconnect for changed server configuration. Compatible clients share one Perch authorization, not a copied harness credential. Providers requiring pre-registered clients, nonstandard auth, or legacy transports are not yet supported. No real provider or Windows GUI acceptance is claimed by fixture tests.

## References checked during implementation

- [Codex MCP](https://developers.openai.com/codex/mcp/): shared CLI/desktop configuration, transport and header fields.
- [Codex skills](https://developers.openai.com/codex/skills/): user `.agents/skills`, symlink support.
- [Claude Code MCP](https://code.claude.com/docs/en/mcp): user scope, HTTP transport types, desktop import distinctions.
- [pywebview API](https://pywebview.flowrl.com/api/): persistent browser storage, native window events and menus.

Harness formats evolve. The adapter tests use representative local records; unknown records must be isolated and visible rather than blocking all session updates.

- [MCP authorization specification](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization): OAuth discovery, PKCE, resource audience, and client registration.
