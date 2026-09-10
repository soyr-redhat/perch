"""Perch-owned resource versions, revision-checked edits, and recoverable removal."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sys
from uuid import uuid4
from urllib.parse import urlparse

import tomlkit

from . import skill_sharing, sync
from .storage import atomic_write, sync_lock, write_json

MASK = '••••••••'


def root():
    return Path(sync.PERCH_DIR) / 'managed'


def catalog():
    path = root() / 'resources.json'
    doc = json.loads(path.read_text()) if path.exists() else {'version': 1, 'items': {}}
    if doc.get('version') != 1 or not isinstance(doc.get('items'), dict):
        raise ValueError('Invalid managed resource catalog')
    return doc


def managed_names(kind):
    return {r['name'] for r in catalog()['items'].values() if r['kind'] == kind}


def _json(value):
    return json.dumps(value, indent=2, ensure_ascii=False) + '\n'


def _state(path):
    path = Path(path)
    if skill_sharing.is_link(path):
        target = os.readlink(path)
        return 'link:' + os.path.normcase(os.path.abspath(target if os.path.isabs(target) else path.parent / target)).removeprefix('\\\\?\\')
    if path.is_dir():
        return 'tree:' + skill_sharing.signature(path)
    return 'file:' + hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else 'absent'


def _new_state(op):
    if op.get('link'):
        return 'link:' + os.path.normcase(os.path.abspath(op['link'])).removeprefix('\\\\?\\')
    if 'text' in op:
        return 'file:' + hashlib.sha256(op['text'].encode()).hexdigest()
    return 'absent'


def _remove(path):
    if skill_sharing.is_link(path):
        sync._remove_managed_link(path)
    elif path.is_dir():
        raise ValueError('Refusing to remove an unexpected directory')
    else:
        path.unlink(missing_ok=True)


def _rollback(job):
    for op in reversed(job['operations']):
        path, backup = Path(op['path']), Path(op['backup'])
        if not op.get('started', True):
            continue
        if os.path.lexists(backup):
            if _state(path) not in ('absent', _new_state(op)):
                raise ValueError('A resource changed during recovery; its backup was preserved')
            _remove(path)
            backup.rename(path)
        elif op['before'] == 'absent':
            if _state(path) not in ('absent', _new_state(op)):
                raise ValueError('A new resource changed during recovery; it was preserved')
            _remove(path)
        elif _state(path) != op['before']:
            raise ValueError('A resource changed during recovery; manual review is required')


def recover():
    for path in sorted((root() / 'transactions').glob('*.json')):
        job = json.loads(path.read_text())
        if job['status'] == 'pending':
            _rollback(job)
            job['status'] = 'restored'
            write_json(path, job)


def _commit(operations):
    identifier = uuid4().hex
    for op in operations:
        p = Path(op['path'])
        op['before'] = _state(p)
        op['started'] = False
        op['backup'] = str(p.with_name(f'.{p.name}.perch-edit-{identifier}'))
    job = {'status': 'pending', 'operations': operations}
    journal = root() / 'transactions' / (identifier + '.json')
    write_json(journal, job)
    try:
        for op in operations:
            p, backup = Path(op['path']), Path(op['backup'])
            p.parent.mkdir(parents=True, exist_ok=True)
            op['started'] = True
            write_json(journal, job)
            if _state(p) != op['before']:
                raise ValueError('Resource changed before saving; review again')
            if os.path.lexists(p):
                p.rename(backup)
                if _state(backup) != op['before']:
                    raise ValueError('Resource changed while saving; review again')
            if op.get('link'):
                sync._link_dir(op['link'], str(p), resolve=False)
            elif 'text' in op:
                atomic_write(p, op['text'])
        job['status'] = 'complete'
        write_json(journal, job)
    except Exception:
        _rollback(job)
        job['status'] = 'restored'
        write_json(journal, job)
        raise


def _file_name(value):
    if not isinstance(value, str) or not value or '\\' in value or ':' in value:
        raise ValueError('Use a relative file name inside the skill')
    p = PurePosixPath(value)
    if p.is_absolute() or any(x in ('.', '..') or x.startswith('.') for x in p.parts):
        raise ValueError('File must stay inside the skill')
    return value


def _name(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,99}', value):
        raise ValueError('Use a name containing letters, numbers, dots, underscores, or hyphens')
    return value


def _revision():
    return hashlib.sha256((sync.fingerprint() + _json(catalog())).encode()).hexdigest()


def _find(identifier):
    from .capabilities import inventory
    item = next((r for r in inventory()['resources'] if r['id'] == identifier), None)
    if not item or item['kind'] not in ('skill', 'mcp'):
        raise ValueError('Choose a skill or MCP server')
    return item


def _source(item, choice):
    choices = [{'id': hashlib.sha256((h + ':' + p).encode()).hexdigest(), 'harness': h, 'path': p}
               for h, p in item['origins'].items()]
    selected = next((s for s in choices if s['id'] == choice), None) if choice else choices[0] if choices else None
    if not selected:
        raise ValueError('Choose a detected source')
    return selected, choices


def _configuration(item, source):
    if item.get('plugin'):
        from .capabilities import discover_plugins
        plugins, _ = discover_plugins()
        plugin = next(p for p in plugins if p['id'] == item['plugin'])
        component = next(c for c in plugin['components'] if c['kind'] == 'mcp' and c['name'] == item['name'])
        return copy.deepcopy(component['definition'])
    if source['harness'] == 'perch':
        return sync._load_mcp_registry(sync._mcp_registry())[1][item['name']]
    path = sync._paths().get(source['harness'])
    if not path:
        raise ValueError('This server has no editable configuration source')
    return copy.deepcopy(sync._load(path, source['harness'] == 'codex')[2][item['name']])


def _redact(config):
    value = copy.deepcopy(config)
    for group in ('env', 'headers', 'http_headers'):
        if isinstance(value.get(group), dict):
            value[group] = {k: MASK for k in value[group]}
    for field in set(value) - {'command', 'args', 'url', 'type', 'enabled', 'disabled', 'env', 'headers', 'http_headers'}:
        value[field] = MASK
    return value


def _material(record):
    from .credentials import read_secret
    config = copy.deepcopy(record['config'])
    if record.get('secrets'):
        secrets = read_secret(record['secrets']) or {}
        for group in ('env', 'headers'):
            if group in config:
                if group not in secrets:
                    raise ValueError('Saved credentials are unavailable; update the server in Perch')
                config[group] = secrets[group]
    return config


def inspect_resource(identifier, source=None):
    with sync_lock(sync.PERCH_DIR):
        skill_sharing.recover()
        recover()
        record = catalog()['items'].get(identifier)
        if record and record.get('deleted'):
            raise ValueError('Restore this resource before editing')
        item = _find(identifier)
        selected, choices = _source(item, source)
        data = {'id': identifier, 'name': item['name'], 'kind': item['kind'], 'managed': bool(record),
                'source': selected['id'], 'sources': choices, 'revision': _revision(),
                'targets': record['targets'] if record else list(item['presentIn'])}
        if item['kind'] == 'skill':
            path = Path(record['path'] if record else selected['path'])
            tree = skill_sharing.tree(path)
            files = {}
            budget = 700_000
            readonly = []
            for relative in sorted(tree, key=lambda name: (name.casefold() != 'skill.md', name)):
                p = path / relative
                if p.is_file() and not p.is_symlink() and p.stat().st_size <= min(256_000, budget) and len(files) < 100:
                    try:
                        text = p.read_text(encoding='utf-8')
                        if '\x00' in text:
                            raise ValueError()
                        files[relative] = text
                        budget -= len(text.encode())
                        continue
                    except (UnicodeError, ValueError):
                        pass
                readonly.append(relative)
            if not any(f.casefold() == 'skill.md' for f in files):
                raise ValueError('SKILL.md must be UTF-8 text smaller than 256 KB')
            data.update(files=files, readonly=readonly)
        else:
            config = _material(record) if record else _configuration(item, selected)
            data.update(config=_redact(config), authentication=record.get('authentication', 'none') if record else 'none')
        return data


def bridge_definition(identifier):
    if getattr(sys, 'frozen', False):
        executable = Path(sys.executable).with_name('perch-cli.exe' if os.name == 'nt' else 'perch-cli')
        command = [str(executable)]
    else:
        command = [sys.executable, '-m', 'perch']
    return {'command': command[0], 'args': command[1:] + ['--mcp-bridge', identifier],
            'env': {'PERCH_DATA_DIR': str(Path(sync.PERCH_DIR).resolve())}}


def _operations(record, old):
    """Only replace entries we created or that match the explicitly adopted source."""
    operations = []
    deleted = record.get('deleted', False)
    name = record['name']
    if record['kind'] == 'skill':
        registry = sync._shared_skill_root() / name
        roots = {h: Path(p).expanduser() for h, p in sync.SKILL_ROOTS.items() if h in sync.SKILL_TARGETS}
        entries = copy.deepcopy(sync._load_manifest())
        managed = {x['link']: x for x in entries['created'] if isinstance(x, dict) and 'link' in x}
        for h, parent in roots.items():
            dest = parent / name
            wanted = not deleted and h in record['targets']
            exists = os.path.lexists(dest)
            known = str(dest) in managed and skill_sharing.points_to(dest, managed[str(dest)]['target'])
            adopted = not old and str(dest.resolve()) in record.get('adopted', [])
            if exists and not known and not adopted:
                if wanted:
                    raise ValueError(f'{h}: this entry is not managed by Perch; choose a different name')
                continue
            if wanted and not skill_sharing.points_to(dest, registry):
                operations.append({'path': str(dest), 'link': str(registry)})
            elif not wanted and known:
                operations.append({'path': str(dest)})
            if wanted:
                managed[str(dest)] = {'link': str(dest), 'target': str(registry), 'kind': 'registry'}
            elif known:
                managed.pop(str(dest), None)
        registry_known = str(registry) in managed and skill_sharing.points_to(registry, managed[str(registry)]['target'])
        if os.path.lexists(registry) and not registry_known:
            if not deleted:
                raise ValueError('The shared registry entry changed outside Perch; preserved')
        else:
            operations.insert(0, {'path': str(registry), **({'link': record['path']} if not deleted else {})})
        if deleted:
            managed.pop(str(registry), None)
        else:
            managed[str(registry)] = {'link': str(registry), 'target': record['path'], 'kind': 'registry'}
        entries['created'] = list(managed.values())
        operations.append({'path': sync.MANIFEST, 'text': _json(entries)})
    else:
        connection = bridge_definition(record['id'])
        recorded = (old or {}).get('connections', {})
        record['connections'] = {}
        for h, path in sync._paths().items():
            if h not in sync.TARGET_NAMES:
                continue
            raw, doc, servers = sync._load(path, h == 'codex')
            existing = servers.get(name)
            owned = h in recorded and existing == recorded[h]
            adoptable = not old and existing == record.get('adopted', {}).get(h) and existing is not None
            wanted = not deleted and h in record['targets']
            if existing is not None and not owned and not adoptable:
                if wanted:
                    raise ValueError(f'{h}: configuration changed or belongs to another source; preserved')
                continue
            key = 'mcp_servers' if h == 'codex' else 'mcpServers'
            if key not in doc:
                doc[key] = tomlkit.table() if h == 'codex' else {}
            if wanted:
                doc[key][name] = connection
                record['connections'][h] = connection
            elif owned:
                doc[key].pop(name, None)
            else:
                continue
            text = tomlkit.dumps(doc) if h == 'codex' else _json(doc)
            if text.encode() != raw:
                operations.append({'path': path, 'text': text})
        registry = sync._mcp_registry()
        _, servers = sync._load_mcp_registry(registry)
        servers.pop(name, None)
        if registry.exists():
            operations.append({'path': str(registry), 'text': _json({'version': 1, 'servers': servers})})
    return operations


def change(body):
    from .capabilities import _id
    from .credentials import write_secret
    action = body.get('action', 'save')
    if action not in ('save', 'remove', 'restore') or type(body.get('apply', False)) is not bool:
        raise ValueError('Choose save, remove, or restore')
    with sync_lock(sync.PERCH_DIR):
        skill_sharing.recover()
        recover()
        document = catalog()
        if body.get('baseRevision') and body['baseRevision'] != _revision():
            raise ValueError('Resources changed while you were editing; reopen the editor')
        old = document['items'].get(body.get('id'))
        if action != 'save' and not old:
            raise ValueError('Only Perch-managed resources can be removed or restored')
        item = _find(body['id']) if body.get('id') and not old else None
        kind = old['kind'] if old else item['kind'] if item else body.get('kind')
        if kind not in ('skill', 'mcp'):
            raise ValueError('Choose a skill or MCP server')
        name = _name(old['name'] if old else item['name'] if item else body.get('name'))
        identifier = old['id'] if old else _id(kind, name)
        if not old and identifier in document['items']:
            raise ValueError('An item with this name already exists; edit or restore it')
        if not old and not item:
            from .capabilities import inventory
            if any(r['kind'] == kind and r['name'] == name for r in inventory()['resources']):
                raise ValueError('This resource already exists; edit it instead')
        targets = body.get('targets', old['targets'] if old else [])
        allowed = sync.SKILL_TARGETS if kind == 'skill' else sync.TARGET_NAMES
        if not isinstance(targets, list) or any(t not in allowed for t in targets):
            raise ValueError('Choose compatible destination harnesses')
        record = copy.deepcopy(old) if old else {'id': identifier, 'kind': kind, 'name': name}
        record['targets'] = list(dict.fromkeys(targets))
        record['deleted'] = action == 'remove'
        source = choices = None
        if item:
            record['sourceId'] = item['id']
            source, choices = _source(item, body.get('source'))
        files = body.get('files', {})
        material = None
        if action == 'save':
            if old and old.get('deleted'):
                raise ValueError('Restore this resource before editing')
            if kind == 'skill':
                if not isinstance(files, dict) or len(files) > 100 or sum(len(v.encode()) for v in files.values() if isinstance(v, str)) > 700_000:
                    raise ValueError('Edit up to 100 text files and 700 KB at a time')
                for relative, text in files.items():
                    _file_name(relative)
                    if text is not None and (not isinstance(text, str) or '\x00' in text or len(text.encode()) > 256_000):
                        raise ValueError('Skill files must be UTF-8 text, at most 256 KB each')
                record['adopted'] = list(item['origins'].values()) if item else record.get('adopted', [])
                base = Path(old['path']) if old else Path(source['path']) if source else None
                if base:
                    skill_sharing.tree(base)
                content = files.get('SKILL.md', (base / 'SKILL.md').read_text() if base else None)
                if not isinstance(content, str) or not content.strip():
                    raise ValueError('A nonempty SKILL.md is required')
            else:
                config = copy.deepcopy(body.get('config'))
                if not isinstance(config, dict):
                    raise ValueError('MCP configuration must be a JSON object')
                original = _material(old) if old else _configuration(item, source) if item else {}
                for group in ('env', 'headers', 'http_headers'):
                    if isinstance(config.get(group), dict):
                        for k, v in config[group].items():
                            if v == MASK:
                                if k not in original.get(group, {}):
                                    raise ValueError('Enter a value for the new credential field')
                                config[group][k] = original[group][k]
                material = sync._norm_server(config)
                if not material:
                    raise ValueError('A disabled configuration cannot be shared')
                authentication = body.get('authentication', 'none')
                endpoint = urlparse(material.get('url', ''))
                if endpoint.username or endpoint.password:
                    raise ValueError('Use server headers for credentials, not credentials in the URL')
                if authentication not in ('none', 'oauth') or (authentication == 'oauth' and endpoint.scheme != 'https'):
                    raise ValueError('OAuth requires an HTTPS MCP server')
                record.update(config=_redact(material), authentication=authentication)
                if not old:
                    record['adopted'] = {}
                    for h, p in sync._paths().items():
                        _, _, servers = sync._load(p, h == 'codex')
                        existing = servers.get(name)
                        if existing is not None:
                            try:
                                if sync._norm_server(existing) == sync._norm_server(original):
                                    record['adopted'][h] = existing
                            except ValueError:
                                if h == source['harness']:
                                    record['adopted'][h] = existing
        revision = hashlib.sha256((_revision() + _json({k: v for k, v in body.items() if k not in ('revision', 'apply')})).encode()).hexdigest()
        if body.get('apply') and body.get('revision') != revision:
            raise ValueError('Resources changed; review your changes again')
        if not body.get('apply'):
            preview_record = copy.deepcopy(record)
            if kind == 'skill' and action == 'save':
                preview_record['path'] = str(base or root() / 'skills' / 'preview')
            _operations(preview_record, old)
            return {'revision': revision, 'id': identifier, 'action': action, 'targets': record['targets'],
                    'name': name, 'adopting': bool(item), 'applied': False}
        if action == 'save' and kind == 'skill':
            version = root() / 'skills' / identifier.replace(':', '-') / uuid4().hex
            version.parent.mkdir(parents=True, exist_ok=True)
            if base:
                shutil.copytree(base, version, symlinks=True)
                for relative in skill_sharing.tree(base):
                    original, copied = base / relative, version / relative
                    if original.is_symlink() and os.path.isabs(os.readlink(original)):
                        target = version / original.resolve().relative_to(base.resolve())
                        copied.unlink()
                        os.symlink(str(target), copied, target_is_directory=original.is_dir())
            else:
                version.mkdir()
            for relative, text in files.items():
                p = version / relative
                if p.is_symlink() or not p.resolve().is_relative_to(version.resolve()):
                    raise ValueError('Edit the actual file instead of a symlink')
                if text is None:
                    p.unlink(missing_ok=True)
                else:
                    atomic_write(p, text)
            skill_sharing.tree(version)
            record['path'] = str(version)
        elif action == 'save' and kind == 'mcp':
            secrets = {k: material[k] for k in ('env', 'headers') if material.get(k)}
            record['secrets'] = uuid4().hex if secrets else None
            if secrets:
                write_secret(record['secrets'], secrets)
        operations = _operations(record, old)
        record.pop('adopted', None)
        document['items'][identifier] = record
        operations.append({'path': str(root() / 'resources.json'), 'text': _json(document)})
        _commit(operations)
        return {'id': identifier, 'name': name, 'action': action, 'applied': True}


def overlay(resources, targets):
    items = catalog()['items']
    covered = {(r['kind'], r['name']) for r in items.values()}
    adopted = {r.get('sourceId'): r['id'] for r in items.values() if r.get('sourceId')}
    result = [copy.deepcopy(r) for r in resources if r['id'] not in adopted and (r.get('plugin') or (r['kind'], r['name']) not in covered)]
    for resource in result:
        resource['components'] = [adopted.get(identifier, identifier) for identifier in resource.get('components', [])
                                  if not items.get(adopted.get(identifier, identifier), {}).get('deleted')]
    for record in items.values():
        if record.get('deleted'):
            continue
        compatibility = {}
        for t in targets:
            h = t['id']
            wanted = h in record['targets']
            if record['kind'] == 'skill':
                registry = sync._shared_skill_root() / record['name']
                native = Path(sync.SKILL_ROOTS.get(h, '/nonexistent')).expanduser() / record['name']
                present = wanted and skill_sharing.points_to(native, registry) and skill_sharing.points_to(registry, record['path'])
            else:
                try:
                    present = wanted and sync._load(sync._paths()[h], h == 'codex')[2].get(record['name']) == record.get('connections', {}).get(h)
                except (OSError, ValueError, KeyError):
                    present = False
            compatibility[h] = {'status': 'present' if present else 'blocked' if wanted else 'managed',
                                'reason': 'Connection changed outside Perch; edit to review' if wanted and not present else ''}
        result.append({'id': record['id'], 'kind': record['kind'], 'name': record['name'], 'managed': True,
                       'origins': {'perch': record.get('path', 'Perch')}, 'presentIn': [h for h, value in compatibility.items() if value['status'] == 'present'],
                       'compatibility': compatibility, 'plugin': None, 'components': [], 'discovery': 'managed',
                       'authentication': record.get('authentication', 'none')})
    return result


def removed():
    return [{'id': r['id'], 'name': r['name'], 'kind': r['kind'], 'targets': r['targets']}
            for r in catalog()['items'].values() if r.get('deleted')]
