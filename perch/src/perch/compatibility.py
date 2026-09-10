"""Local transfer checks. These inspect files; they never execute discovered tools."""

from pathlib import Path
import os
import re
import shutil


def skill_requirement(path):
    """Return the unresolved requirement, or None for instruction-only skills.

    This is a conservative transfer check, not a claim that a harness can perform
    every task described in prose. Unknown bundled programs stay reviewable.
    """
    root = Path(path).resolve()
    try:
        if not (root / 'SKILL.md').is_file():
            return 'Skill is missing SKILL.md'
        texts, total, bundled = [], 0, False
        for index, item in enumerate(root.rglob('*')):
            if index >= 10000:
                return 'Skill exceeds 10,000 entries; review its dependencies'
            if item.is_symlink():
                return 'Linked skill contents require dependency review'
            if not item.is_file():
                continue
            if item.suffix.lower() in ('.md', '.txt') or item.name in ('LICENSE', 'NOTICE'):
                total += item.stat().st_size
                if total > 2 * 1024 * 1024:
                    return 'Skill instructions exceed 2 MB; review its dependencies'
                texts.append(item.read_text(encoding='utf-8'))
            else:
                bundled = True
        text = '\n'.join(texts)
        checks = (
            (r'\$\{(?:CLAUDE|CODEX)_PLUGIN_', 'Requires host plugin variables'),
            (r'mcp__\w+|\b(?:search_plugins|suggest_plugins|get_app_permissions|get_plugin_dependencies)\b',
             'Requires host connector tools; destination support is not verified'),
            (r'container_tools/|load_workspace_dependencies|@oai/artifact-tool|codex-runtimes',
             'Requires the host workspace runtime'),
            (r'window\.openai|:codex-(?:file-citation|visualization)|visualization://',
             'Requires the host renderer'),
            (r'plugin://|preinstalled (?:document|spreadsheet|presentation).*capability',
             'Requires another plugin or artifact capability'),
            (r'\bSites (?:tool|connector)|\bsites-preview\b', 'Requires the Sites connector and preview runtime'),
        )
        for pattern, reason in checks:
            if re.search(pattern, text, flags=re.IGNORECASE):
                return reason
        if bundled:
            return 'Bundled files require dependency review before automatic sharing'
        return None
    except (OSError, UnicodeError) as exc:
        return f'Cannot inspect skill dependencies: {type(exc).__name__}'


def header_requirement(cfg):
    for key in ('headers', 'http_headers'):
        values = cfg.get(key, {})
        if isinstance(values, dict) and any(isinstance(value, str) and '$(' in value for value in values.values()):
            return 'Shell expressions in HTTP headers are sent literally; set the header value in Perch'
    return None


def command_requirement(cfg):
    if not isinstance(cfg, dict):
        return 'MCP definition must be an object'
    if cfg.get('enabled') is False or cfg.get('disabled') is True:
        return 'Source MCP server is disabled'
    if reason := header_requirement(cfg):
        return reason
    command = cfg.get('command')
    if not command:
        return None
    if not isinstance(command, str):
        return 'MCP command must be text'
    # Absolute commands must be executable; bare commands use Perch's PATH.
    if not shutil.which(command):
        return f'Executable not found: {Path(command).name}'
    if cfg.get('cwd') and (not isinstance(cfg['cwd'], str) or not os.path.isdir(cfg['cwd'])):
        return 'MCP working directory is missing'
    return None
