"""Managed edits, guarded removal, crash recovery, and additive-sync interaction."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from perch import capabilities, credentials, resources, sync
from test_runtime import Workspace


class ManagedResourcesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Workspace(self.temp.name)
        self.addCleanup(self.workspace.close)
        self.secrets = {}
        for target, name, value in (
            (capabilities, 'discover_plugins', lambda: ([], [])),
            (credentials, 'write_secret', lambda k, v: self.secrets.__setitem__(k, copy.deepcopy(v))),
            (credentials, 'read_secret', lambda k: copy.deepcopy(self.secrets.get(k))),
        ):
            p = patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)

    def apply(self, body):
        plan = resources.change(body)
        return resources.change({**body, 'revision': plan['revision'], 'apply': True})

    def skill(self, name='new-skill'):
        return self.apply({'kind': 'skill', 'name': name, 'targets': ['claude', 'codex'],
                           'files': {'SKILL.md': f'---\nname: {name}\ndescription: Test\n---\nFirst version'}})['id']

    def test_create_edit_remove_restore_and_no_automatic_resurrection(self):
        identifier = self.skill()
        dest = Path(sync.SKILL_ROOTS['codex'])/'new-skill'
        first = dest.resolve()
        doc = resources.inspect_resource(identifier)
        self.apply({'id': identifier, 'targets': ['claude', 'codex'], 'baseRevision': doc['revision'],
                    'files': {'SKILL.md': 'Second version', 'support.txt': 'Support'}})
        self.assertNotEqual(dest.resolve(), first)
        self.assertEqual((dest/'SKILL.md').read_text(), 'Second version')
        self.assertIn('First version', (first/'SKILL.md').read_text())
        self.apply({'id': identifier, 'action': 'remove'})
        self.assertFalse(dest.exists())
        sync.sync_all()
        self.assertFalse(dest.exists())
        self.assertNotIn(identifier, [r['id'] for r in capabilities.inventory()['resources']])
        self.assertEqual(resources.removed()[0]['id'], identifier)
        self.apply({'id': identifier, 'action': 'restore'})
        self.assertEqual((dest/'support.txt').read_text(), 'Support')

    def test_import_copies_source_and_preserves_external_repository(self):
        source = Path(self.temp.name)/'external-skill'
        source.mkdir()
        (source/'SKILL.md').write_text('Original')
        native = Path(sync.SKILL_ROOTS['claude'])/'outside'
        native.symlink_to(source, target_is_directory=True)
        identifier = capabilities._id('skill', 'outside')
        read = resources.inspect_resource(identifier)
        self.apply({'id': identifier, 'source': read['source'], 'targets': ['claude', 'codex'],
                    'files': {'SKILL.md': 'Edited in Perch'}})
        self.assertEqual((source/'SKILL.md').read_text(), 'Original')
        self.assertEqual((native/'SKILL.md').read_text(), 'Edited in Perch')

    def test_stale_edits_and_paths_are_rejected(self):
        identifier = self.skill()
        request = {'id': identifier, 'files': {'SKILL.md': 'Draft'}}
        preview = resources.change(request)
        self.apply({'id': identifier, 'files': {'SKILL.md': 'Other editor'}})
        with self.assertRaisesRegex(ValueError, 'changed'):
            resources.change({**request, 'revision': preview['revision'], 'apply': True})
        for filename in ('../escape', '/absolute', 'C:/outside', 'folder/../../outside'):
            with self.assertRaises(ValueError):
                resources.change({'id': identifier, 'files': {filename: 'no'}})

    def test_edit_failure_rolls_back_and_pending_transaction_recovers(self):
        identifier = self.skill()
        target = Path(sync.SKILL_ROOTS['codex'])/'new-skill'
        before = target.resolve()
        original = resources.atomic_write
        def fail(path, text, **kwargs):
            if Path(path) == resources.root()/'resources.json':
                raise OSError('Injected write failure')
            return original(path, text, **kwargs)
        with patch.object(resources, 'atomic_write', side_effect=fail), self.assertRaises(OSError):
            self.apply({'id': identifier, 'files': {'SKILL.md': 'Broken'}})
        self.assertEqual(target.resolve(), before)
        def interrupt(path, text, **kwargs):
            if Path(path) == resources.root()/'resources.json':
                raise KeyboardInterrupt()
            return original(path, text, **kwargs)
        with patch.object(resources, 'atomic_write', side_effect=interrupt), self.assertRaises(KeyboardInterrupt):
            self.apply({'id': identifier, 'files': {'SKILL.md': 'Interrupted'}})
        resources.recover()
        self.assertEqual(target.resolve(), before)

    def test_mcp_credentials_edits_and_unrelated_configuration_preserved(self):
        identifier = self.apply({'kind': 'mcp', 'name': 'managed-tool', 'targets': ['codex'],
                                 'config': {'command': 'server', 'env': {'TOKEN': 'very-private'}}})['id']
        self.assertNotIn('very-private', (resources.root()/'resources.json').read_text())
        config_path = Path(self.workspace.paths['codex'])
        self.assertNotIn('very-private', config_path.read_text())
        view = resources.inspect_resource(identifier)
        self.assertEqual(view['config']['env']['TOKEN'], resources.MASK)
        self.apply({'id': identifier, 'config': {**view['config'], 'args': ['--verbose']}, 'targets': ['codex']})
        self.assertEqual(resources._material(resources.catalog()['items'][identifier])['env']['TOKEN'], 'very-private')
        # An external edit loses Perch ownership and must survive removal.
        raw, doc, servers = sync._load(config_path, True)
        doc['mcp_servers']['managed-tool']['command'] = 'user-override'
        import tomlkit
        config_path.write_text(tomlkit.dumps(doc))
        self.apply({'id': identifier, 'action': 'remove'})
        self.assertEqual(sync._load(config_path, True)[2]['managed-tool']['command'], 'user-override')
        sync.sync_all()
        self.assertNotIn('managed-tool', sync._load(self.workspace.paths['claude'])[2])

    def test_desktop_routes_require_authorization_and_share_operation_logic(self):
        w = self.workspace
        status, preview = w.request('/api/resources/change', {'kind': 'skill', 'name': 'from-ui',
                                                            'targets': [], 'files': {'SKILL.md': 'Created'}})
        self.assertEqual(status, 200)
        status, result = w.request('/api/resources/change', {'kind': 'skill', 'name': 'from-ui', 'targets': [],
                                                           'files': {'SKILL.md': 'Created'}, 'revision': preview['revision'], 'apply': True})
        self.assertEqual(status, 200)
        self.assertEqual(w.request('/api/resources/read', {'id': result['id']})[1]['files']['SKILL.md'], 'Created')
        self.assertEqual(w.request('/api/resources/removed', {}, headers={'X-Perch-Token': 'wrong'})[0], 401)

    def test_external_registry_change_is_preserved_on_removal(self):
        identifier = self.skill()
        registry = sync._shared_skill_root() / 'new-skill'
        outside = Path(self.temp.name) / 'new-source'
        outside.mkdir()
        (outside / 'SKILL.md').write_text('Outside replacement')
        sync._remove_managed_link(registry)
        sync._link_dir(str(outside), str(registry))
        with self.assertRaisesRegex(ValueError, 'changed outside'):
            resources.change({'id': identifier, 'files': {'SKILL.md': 'Draft'}})
        self.apply({'id': identifier, 'action': 'remove'})
        self.assertEqual(registry.resolve(), outside.resolve())
        self.assertEqual((outside / 'SKILL.md').read_text(), 'Outside replacement')

    def test_adopted_plugin_component_remains_reachable_from_its_package(self):
        record = {'id': 'managed', 'sourceId': 'component', 'kind': 'mcp', 'name': 'shared-tool',
                  'targets': [], 'config': {'command': 'server'}}
        from perch.storage import write_json
        write_json(resources.root() / 'resources.json', {'version': 1, 'items': {'managed': record}})
        result = resources.overlay([
            {'id': 'package', 'kind': 'plugin', 'name': 'package', 'components': ['component']},
            {'id': 'component', 'plugin': 'package', 'kind': 'mcp', 'name': 'shared-tool'},
            {'id': 'unrelated', 'plugin': 'another-package', 'kind': 'mcp', 'name': 'shared-tool'},
        ], [])
        self.assertEqual(result[0]['components'], ['managed'])
        self.assertIn('unrelated', [r['id'] for r in result])
        self.assertNotIn('component', [r['id'] for r in result])
