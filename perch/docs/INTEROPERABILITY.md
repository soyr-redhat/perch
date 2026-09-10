# Interoperability contract

Perch connects context and compatible capabilities across harnesses. Harnesses remain responsible for their own conversation interface, execution, accounts, and permissions. Perch's reader supports inspecting and transferring context; its target workflow routes work into native harness tools.

| Harness | Sessions | Skills | MCP |
|---|---|---|---|
| Claude Code | `~/.claude/projects/*/*.jsonl` | `~/.claude/skills` | `~/.claude.json` |
| Codex CLI and desktop | `~/.codex/sessions/**/*.jsonl` | `~/.agents/skills`, legacy sources in `~/.codex/skills` | `$CODEX_HOME/config.toml` or `~/.codex/config.toml` |
| omp | `~/.omp/agent/sessions/*/*.jsonl` | `~/.omp/agent/skills` | `~/.omp/agent/mcp.json` |
| Claude Desktop | No private transcript adapter | No compatible user-directory contract | platform Claude `claude_desktop_config.json`; stdio only |

Claude Desktop's JSON is under `~/Library/Application Support/Claude` on macOS and `%APPDATA%/Claude` on Windows. Perch also discovers legacy Claude MCP definitions in `~/.claude/mcp.json`.

Perch keeps portable shared state in `~/.perch/shared`. Each `skills/<name>` entry is a link to the detected canonical skill directory, and Perch-managed entries in native skill roots link through it. `mcp/servers.json` stores normalized portable MCP definitions; applying sharing adds compatible definitions to the destination harness’s native configuration. A previous Perch-created direct skill link is migrated to the registry. User-created links and existing configuration are left alone.

MCP conversion supports the portable core: `command`, string `args`, string-valued `env`, HTTP `url`, and static headers. Codex `http_headers` maps to JSON clients' `headers`. HTTP types are explicit for JSON clients. Disabled definitions remain disabled in their existing client and are not used as sources. Different definitions with the same name block propagation of that name. Unknown fields, SSE/WebSocket transports, client-specific OAuth settings, and environment interpolation syntax block automatic conversion.

Static values deliberately configured in an MCP definition, including headers and environment values, are part of the selected sharing operation. They are never displayed in tool inventory or reports. OAuth credential stores are never read or copied. A copied remote server may still require sign-in in the destination client.

Plugins can contain platform-specific code, commands, environment assumptions, and approval rules. Copying their installation directories across harnesses is not a supported interchange format. Current sharing covers explicit skills and MCP configuration; plugin installations, hooks, agent definitions, and sign-in state remain harness-owned.

Planned plugin adapters will inventory those components, map compatible skills and tool definitions into destination formats, retain source provenance, and identify host-specific features. Context delivery will use the saved recording snapshots with destination-specific import or attachment routes. These adapters and automatic delivery are not implemented yet; see [RFC #2](https://github.com/soyr-redhat/perch/issues/2).

## References checked during implementation

- [Codex MCP](https://developers.openai.com/codex/mcp/): shared CLI/desktop configuration, transport and header fields.
- [Codex skills](https://developers.openai.com/codex/skills/): user `.agents/skills`, symlink support.
- [Claude Code MCP](https://code.claude.com/docs/en/mcp): user scope, HTTP transport types, desktop import distinctions.
- [pywebview API](https://pywebview.flowrl.com/api/): persistent browser storage, native window events and menus.

Harness formats evolve. The adapter tests use representative local records; unknown records must be isolated and visible rather than blocking all session updates.
