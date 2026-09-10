"""Resource inventory and selected connections stay local and preserve existing settings."""

import contextlib
import io
import json
from pathlib import Path
import tempfile
import sys
import unittest
from unittest.mock import patch

from perch import app, capabilities, sync
from test_runtime import Workspace


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)

    def plugin(self, name="example", manifest=None):
        root = self.home / ".claude/plugins/cache/local" / name / "v1"
        (root / ".claude-plugin").mkdir(parents=True)
        (root / ".claude-plugin/plugin.json").write_text(json.dumps(manifest or {"name": name}))
        (self.home / ".claude/plugins/installed_plugins.json").write_text(json.dumps({"plugins": {name: [{"installPath": str(root)}]}}))
        return root

    def test_default_and_custom_components_no_secret_values_in_inventory(self):
        root = self.plugin(manifest={"name": "example", "skills": "./skills/", "hooks": {"run": "never execute"}})
        skill = root / "skills/review"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("---\nname: review\n---\nReview code.")
        (root / ".mcp.json").write_text(json.dumps({"mcpServers": {"tools": {"command": sys.executable, "env": {"TOKEN": "private-value"}}}}))
        plugins, errors = capabilities.discover_plugins(self.home)
        self.assertFalse(errors)
        self.assertEqual([c["kind"] for c in plugins[0]["components"]], ["skill", "mcp", "hooks"])
        base = {"skills": [], "mcp": [], "targets": []}
        catalog = capabilities.inventory(base=base, plugins=plugins)
        self.assertNotIn("private-value", json.dumps(catalog))
        self.assertEqual(len(catalog["resources"][0]["components"]), 3)
        self.assertEqual(catalog["resources"][-1]["compatibility"]["codex"]["status"], "unsupported")

    def test_missing_optional_manifest_and_cached_versions(self):
        root = self.plugin()
        (root / ".claude-plugin/plugin.json").unlink()
        (root / ".lsp.json").write_text("{}")
        cache = self.home / ".codex/plugins/cache/local/cached/v1/.codex-plugin"
        cache.mkdir(parents=True)
        (cache / "plugin.json").write_text('{"name":"cached"}')
        plugins, errors = capabilities.discover_plugins(self.home)
        self.assertFalse(errors)
        self.assertEqual({p["discovery"] for p in plugins}, {"enabled", "cached"})
        self.assertEqual(plugins[0]["components"][0]["kind"], "lspServers")

    def test_component_traversal_is_reported_without_hiding_other_plugins(self):
        self.plugin(manifest={"name": "bad", "skills": "../../../../../outside"})
        cache = self.home / ".codex/plugins/cache/local/good/v1/.codex-plugin"
        cache.mkdir(parents=True)
        (cache / "plugin.json").write_text('{"name":"good"}')
        plugins, errors = capabilities.discover_plugins(self.home)
        self.assertIn("good", [p["name"] for p in plugins])
        self.assertTrue(errors)
        self.assertIn("leaves", errors[0]["reason"])

    def test_malformed_manifest_does_not_abort_inventory(self):
        root = self.plugin()
        (root / ".claude-plugin/plugin.json").write_text('{"bad"')
        plugins, errors = capabilities.discover_plugins(self.home)
        self.assertEqual(plugins, [])
        self.assertEqual(len(errors), 1)

    def test_plugin_relative_paths_are_mapped_without_expanding_environment_secrets(self):
        root = self.plugin(manifest={"name": "paths", "mcpServers": {"tools": {"command": "node", "args": ["${CLAUDE_PLUGIN_ROOT}/server.js", "./config.json"], "env": {"TOKEN": "${TOKEN}"}}}})
        plugins, errors = capabilities.discover_plugins(self.home)
        self.assertFalse(errors)
        cfg = plugins[0]["components"][0]["definition"]
        self.assertEqual(cfg["args"], [str(root.resolve()/"server.js"), str(root.resolve()/"config.json")])
        self.assertEqual(cfg["env"]["TOKEN"], "${TOKEN}")


class ConnectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Workspace(self.temp.name)
        self.addCleanup(self.workspace.close)
        root = self.workspace.root / "plugin"
        skill = root / "skills/plugin-review"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("Review code.")
        self.plugins = [{"id": "plugin:test", "kind": "plugin", "name": "Example", "harness": "claude", "path": str(root), "discovery": "installed", "components": [
            {"kind": "skill", "name": "plugin-review", "path": str(skill)},
            {"kind": "mcp", "name": "plugin-tools", "definition": {"command": sys.executable, "env": {"TOKEN": "private-value"}}},
        ]}]
        self.patcher = patch.object(capabilities, "discover_plugins", side_effect=lambda: (self.plugins, []))
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def resource(self, kind):
        return next(r for r in capabilities.inventory()["resources"] if r["plugin"] and r["kind"] == kind)

    def test_selected_skill_only_preview_apply_and_retry(self):
        resource = self.resource("skill")
        target = Path(sync.SKILL_ROOTS["codex"]) / "plugin-review"
        plan = capabilities.link(resource["id"], "codex")
        self.assertFalse(target.exists())
        result = capabilities.link(resource["id"], "codex", revision=plan["revision"], apply=True)
        self.assertTrue(result["applied"])
        self.assertEqual(target.resolve(), Path(self.plugins[0]["components"][0]["path"]).resolve())
        self.assertFalse((target.parent / "fixture-review").exists())
        catalog = capabilities.inventory()["resources"]
        self.assertEqual(sum(r["name"] == "plugin-review" for r in catalog), 1)
        self.assertEqual(self.resource("skill")["compatibility"]["codex"]["status"], "present")
        plan = capabilities.link(resource["id"], "codex")
        self.assertTrue(capabilities.link(resource["id"], "codex", revision=plan["revision"], apply=True)["applied"])

    def test_mcp_component_preserves_other_settings_and_secrets_stay_out_of_results(self):
        resource = self.resource("mcp")
        plan = capabilities.link(resource["id"], "omp")
        result = capabilities.link(resource["id"], "omp", revision=plan["revision"], apply=True)
        self.assertTrue(result["applied"])
        self.assertNotIn("private-value", json.dumps(result))
        doc = json.loads(Path(self.workspace.paths["omp"]).read_text())
        self.assertEqual(list(doc["mcpServers"]), ["plugin-tools"])
        self.assertEqual(doc["mcpServers"]["plugin-tools"]["env"]["TOKEN"], "private-value")

    def test_stale_plan_conflict_and_disabled_source_do_not_write(self):
        resource = self.resource("mcp")
        plan = capabilities.link(resource["id"], "codex")
        self.plugins[0]["components"][1]["definition"]["args"] = ["changed"]
        with self.assertRaisesRegex(ValueError, "changed"):
            capabilities.link(resource["id"], "codex", revision=plan["revision"], apply=True)
        self.assertFalse(Path(self.workspace.paths["codex"]).exists())
        self.plugins[0]["components"][1]["definition"]["disabled"] = True
        with self.assertRaisesRegex(ValueError, "disabled"):
            capabilities.link(resource["id"], "codex")

    def test_cli_http_inventory_share_engine_and_unknown_ids_are_rejected(self):
        status, data = self.workspace.request("/api/capabilities")
        self.assertEqual(status, 200)
        with patch("sys.argv", ["perch-cli", "--capabilities"]), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(app.main(), 0)
        self.assertEqual(json.loads(output.getvalue()), data)
        self.assertEqual(self.workspace.request("/api/capabilities/link", {"id": "unknown", "target": "codex"})[0], 400)
        resource = self.resource("skill")
        status, plan = self.workspace.request("/api/capabilities/link", {"id": resource["id"], "target": "codex"})
        self.assertEqual(status, 200)
        status, result = self.workspace.request("/api/capabilities/link", {"id": resource["id"], "target": "codex", "revision": plan["revision"], "apply": True})
        self.assertEqual(status, 200)
        self.assertTrue(result["applied"])


class EnablementTests(unittest.TestCase):
    resource = ConnectionTests.resource

    def setUp(self):
        ConnectionTests.setUp(self)
        self.plugins[0]['active'] = True
        self.plugins[0]['enabled'] = True

    def test_auto_share_skips_native_plugin_and_is_idempotent(self):
        preview = sync.sync_all(dry_run=True)
        additions = [x['into'] for x in preview['skills']['linked'] if x['skill'] == 'plugin-review']
        self.assertEqual(additions, ['codex', 'omp'])
        sync.sync_all(revision=preview['revision'])
        self.assertFalse((Path(sync.SKILL_ROOTS['claude']) / 'plugin-review').exists())
        self.assertTrue((Path(sync.SKILL_ROOTS['omp']) / 'plugin-review').is_dir())
        again = sync.sync_all(dry_run=True)
        self.assertEqual(again['skills']['linked'], [])
        self.assertEqual(again['mcp']['added'], {})

    def test_disabled_plugin_is_not_reexported_through_existing_shared_links(self):
        sync.sync_all(targets=('omp',))
        self.plugins[0].update(active=False, disabled=True)
        report = sync.sync_all(dry_run=True)
        self.assertFalse(any(x['skill'] == 'plugin-review' for x in report['skills']['linked']))
        self.assertFalse(any('plugin-tools' in names for names in report['mcp']['added'].values()))
        self.assertTrue((Path(sync.SKILL_ROOTS['omp']) / 'plugin-review').is_dir())
        self.assertFalse((Path(sync.SKILL_ROOTS['codex']) / 'plugin-review').exists())

    def test_dependency_in_reference_blocks_auto_and_manual_transfer(self):
        skill = Path(self.plugins[0]['components'][0]['path'])
        (skill / 'reference.md').write_text('Call mcp__codex_apps__example to use this skill.')
        resource = self.resource('skill')
        self.assertEqual(resource['compatibility']['claude']['status'], 'present')
        self.assertIn('connector', resource['compatibility']['omp']['reason'])
        with self.assertRaisesRegex(ValueError, 'connector'):
            capabilities.link(resource['id'], 'omp')
        report = sync.sync_all(dry_run=True)
        self.assertNotIn('plugin-review', [x['skill'] for x in report['skills']['linked']])

    def test_cached_disabled_and_project_plugins_are_not_auto_sources(self):
        for field, value in [('active', False)]:
            self.plugins[0][field] = value
            self.assertFalse(capabilities.sharing_sources()['skills'])
        self.plugins[0]['disabled'] = True
        self.assertIn('disabled', self.resource('skill')['compatibility']['omp']['reason'])
        self.plugins[0]['disabled'] = False
        self.plugins[0]['scope'] = 'project'
        self.assertIn('Project', self.resource('skill')['compatibility']['omp']['reason'])

    def test_duplicate_native_skills_collapse_but_differing_versions_block_sync(self):
        import copy
        import shutil
        clone = copy.deepcopy(self.plugins[0])
        clone.update(id='plugin:codex', harness='codex')
        path = self.workspace.root / 'second-plugin/skills/plugin-review'
        shutil.copytree(self.plugins[0]['components'][0]['path'], path)
        clone['components'] = [{'kind': 'skill', 'name': 'plugin-review', 'path': str(path)}]
        self.plugins.append(clone)
        resources = capabilities.inventory()['resources']
        skills = [r for r in resources if r['name'] == 'plugin-review']
        self.assertEqual(len(skills), 1)
        self.assertEqual(skills[0]['presentIn'], ['claude', 'codex'])
        parents = [r for r in resources if r['kind'] == 'plugin']
        self.assertTrue(all(skills[0]['id'] in r['components'] for r in parents))
        (path / 'SKILL.md').write_text('Different instructions.')
        sources = capabilities.sharing_sources()
        self.assertNotIn('plugin-review', sources['skills'])
        self.assertTrue(any('differ' in x['reason'] for x in sources['skipped']))

    def test_missing_executable_and_native_auth_fields_have_distinct_states(self):
        self.plugins[0]['components'][1]['definition']['command'] = '/missing/perch-fixture-executable'
        resource = self.resource('mcp')
        self.assertIn('Executable not found', resource['compatibility']['omp']['reason'])
        native = Path(self.workspace.paths['codex'])
        native.write_text('[mcp_servers.signin]\nurl="https://example.invalid/mcp"\nauth="native"\n')
        resource = next(r for r in capabilities.inventory()['resources'] if r['name'] == 'signin')
        self.assertEqual(resource['compatibility']['codex']['status'], 'present')
        self.assertIn('auth', resource['compatibility']['omp']['reason'])

    def test_reference_change_invalidates_auto_sync_preview(self):
        preview = sync.sync_all(dry_run=True)
        path = Path(self.plugins[0]['components'][0]['path']) / 'reference.md'
        path.write_text('A new instruction.')
        with self.assertRaisesRegex(ValueError, 'changed'):
            sync.sync_all(revision=preview['revision'])
        self.assertFalse((Path(sync.SKILL_ROOTS['omp']) / 'plugin-review').exists())

    def test_failed_apply_is_not_reported_as_connected(self):
        resource = self.resource('skill')
        plan = capabilities.link(resource['id'], 'omp')
        with patch('perch.skill_sharing.apply', side_effect=OSError('write failed')):
            result = capabilities.link(resource['id'], 'omp', revision=plan['revision'], apply=True)
        self.assertFalse(result['applied'])
        self.assertEqual(result['status'], 'blocked')


class PluginActivationTests(unittest.TestCase):
    setUp = DiscoveryTests.setUp
    plugin = DiscoveryTests.plugin

    def test_explicit_disable_and_ambiguous_cached_versions(self):
        self.plugin()
        (self.home / '.claude/settings.json').write_text('{"enabledPlugins":{"example":false}}')
        for version in ('v1', 'v2'):
            root = self.home / '.codex/plugins/cache/local/cached' / version / '.codex-plugin'
            root.mkdir(parents=True)
            (root / 'plugin.json').write_text('{"name":"cached"}')
        (self.home / '.codex/config.toml').write_text('[plugins."cached@local"]\nenabled=true\n')
        plugins, errors = capabilities.discover_plugins(self.home)
        self.assertFalse(errors)
        self.assertTrue(plugins[0]['disabled'])
        self.assertTrue(all(not p['active'] for p in plugins))
        self.assertFalse(capabilities.sharing_sources(plugins)['skills'])


class HeaderValidationTests(unittest.TestCase):
    def test_shell_expression_in_header_is_not_treated_as_a_credential(self):
        from perch.compatibility import command_requirement

        reason = command_requirement({'url': 'https://example.invalid/mcp', 'headers': {'Authorization': '$(secret-command)'}})
        self.assertIn('sent literally', reason)
        self.assertNotIn('secret-command', reason)
