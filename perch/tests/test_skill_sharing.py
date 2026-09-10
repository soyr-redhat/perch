"""Whole-folder comparison, explicit choices, and interrupted migration recovery."""

import json
import contextlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from perch import skill_sharing, sync
from test_runtime import Workspace
from test_sync import make_skill


class ConsolidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for key, value in (("MANIFEST", str(Path(self.tmp.name)/"manifest.json")), ("PERCH_DIR", str(Path(self.tmp.name)/"state"))):
            patcher = patch.object(sync, key, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.roots = {h: str(Path(self.tmp.name) / h) for h in ('claude', 'codex', 'omp')}
        for root in self.roots.values():
            Path(root).mkdir()
        for h in ('claude', 'codex'):
            make_skill(self.roots[h], 'shared')
            (Path(self.roots[h]) / 'shared/helper.py').write_text('print("hello")\n')

    def run_sync(self, **kwargs):
        return sync.sync_skills(roots=self.roots, **kwargs)

    def test_identical_copies_consolidate_with_backup_and_repeat_safely(self):
        plan = self.run_sync(dry_run=True)
        self.assertFalse(plan['conflicts'])
        self.assertEqual(len(plan['consolidated']), 1)
        source = Path(plan['consolidated'][0]['source'])
        self.assertFalse((Path(sync.PERCH_DIR)/'shared').exists())
        report = self.run_sync()
        self.assertFalse(report['errors'])
        self.assertTrue(report['backups'])
        self.assertTrue((Path(report['backups'][0])/'helper.py').is_file())
        self.assertTrue(source.is_dir())
        for root in self.roots.values():
            self.assertEqual((Path(root)/'shared').resolve(), source.resolve())
        again = self.run_sync()
        self.assertEqual(again['linked'], [])
        self.assertEqual(again['backups'], [])

    def test_support_file_differences_require_source_and_unknown_destination_is_preserved(self):
        other = Path(self.roots['codex'])/'shared/helper.py'
        other.write_text('different')
        self.assertTrue(self.run_sync()['conflicts'][0]['resolvable'])
        self.assertFalse((Path(sync.PERCH_DIR)/'shared').exists())
        source = str((Path(self.roots['claude'])/'shared').resolve())
        destination = Path(self.roots['omp'])/'shared'
        destination.mkdir()
        (destination/'user-file').write_text('keep')
        report = self.run_sync(sources={'shared': source})
        self.assertTrue(report['errors'])
        self.assertEqual(other.read_text(), 'different')
        self.assertEqual((destination/'user-file').read_text(), 'keep')
        destination.rename(destination.with_name('unrelated'))
        report = self.run_sync(sources={'shared': source})
        self.assertFalse(report['errors'])
        self.assertEqual(other.read_text(), 'print("hello")\n')
        self.assertTrue(any((Path(p)/'helper.py').read_text() == 'different' for p in report['backups']))

    def test_skill_entrypoint_uses_filesystem_case_rules(self):
        for h in ('claude','codex'):
            original=Path(self.roots[h])/'shared/SKILL.md'
            original.rename(original.with_name('skill.md'))
        if not (Path(self.roots['claude'])/'shared/SKILL.md').is_file():
            self.skipTest('Case-sensitive filesystem')
        self.assertFalse(self.run_sync()['errors'])

    def test_link_failure_restores_copies_and_manifest(self):
        original = sync._link_dir
        def failure(target, link, resolve=True):
            if Path(link).parent == Path(self.roots['omp']):
                raise OSError('fixture link failure')
            return original(target, link, resolve)
        with patch.object(sync, '_link_dir', side_effect=failure):
            result = self.run_sync()
        self.assertTrue(result['errors'])
        self.assertFalse(Path(sync.MANIFEST).exists())
        for h in ('claude','codex'):
            path=Path(self.roots[h])/'shared'
            self.assertTrue(path.is_dir())
            self.assertFalse(skill_sharing.is_link(path))
        self.assertFalse((Path(self.roots['omp'])/'shared').exists())
        self.assertFalse(self.run_sync()['errors'])

    def test_hard_interruption_is_recovered_before_retry(self):
        original = sync._link_dir
        def interrupt(target, link, resolve=True):
            if Path(link).parent == Path(self.roots['codex']):
                raise KeyboardInterrupt()
            return original(target, link, resolve)
        with patch.object(sync, '_link_dir', side_effect=interrupt), self.assertRaises(KeyboardInterrupt):
            self.run_sync()
        journals=list((Path(sync.PERCH_DIR)/'skill-migrations').glob('*.json'))
        self.assertEqual(json.loads(journals[0].read_text())['status'],'pending')
        skill_sharing.recover()
        self.assertTrue((Path(self.roots['codex'])/'shared/helper.py').is_file())
        self.assertFalse(skill_sharing.is_link(Path(self.roots['codex'])/'shared'))
        self.assertFalse(self.run_sync()['errors'])

    @unittest.skipIf(os.name == 'nt', 'POSIX permissions and symlink fixture')
    def test_executable_bits_and_external_links_are_not_silently_equal(self):
        path=Path(self.roots['codex'])/'shared/helper.py'
        path.chmod(0o755)
        self.assertTrue(self.run_sync()['conflicts'])
        path.unlink()
        path.symlink_to('/etc/hosts')
        self.assertTrue(self.run_sync()['errors'])


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.w=Workspace(self.temp.name)
        self.addCleanup(self.w.close)
        source=Path(sync.SKILL_ROOTS['codex'])/'fixture-review'
        source.mkdir(parents=True)
        (source/'SKILL.md').write_text('A different review.\n')
        self.source=source

    def test_cli_uses_the_desktop_source_review(self):
        from perch import app

        with patch('sys.argv', ['perch-cli', '--resolve-skill', 'fixture-review']), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(app.main(), 1)
        cli=json.loads(out.getvalue())
        desktop=self.w.request('/api/skills/review', {'name':'fixture-review'})[1]
        self.assertEqual(cli['sources'], desktop['sources'])
        self.assertEqual(cli['report']['revision'], desktop['report']['revision'])

    def test_desktop_source_choice_is_revision_checked_and_idempotent(self):
        code, data=self.w.request('/api/skills/review', {'name':'fixture-review'})
        self.assertEqual(code,200,data)
        self.assertTrue(data['report']['skills']['conflicts'])
        self.assertTrue(data['differences'][0]['diff'])
        selected=data['sources'][0]['id']
        body={'name':'fixture-review','source':selected}
        _,plan=self.w.request('/api/skills/review',body)
        self.assertFalse(plan['report']['skills']['conflicts'])
        (self.source/'new-file').write_text('new')
        code,_=self.w.request('/api/skills/review',{**body,'apply':True,'revision':plan['report']['revision']})
        self.assertEqual(code,400)
        _,plan=self.w.request('/api/skills/review',body)
        code,result=self.w.request('/api/skills/review',{**body,'apply':True,'revision':plan['report']['revision']})
        self.assertEqual(code,200,result)
        self.assertFalse(result['report']['skills']['errors'])
        self.assertTrue(result['report']['skills']['backups'])
        self.assertEqual(self.w.request('/api/skills/review',{'name':'fixture-review','source':'/etc/passwd'})[0],400)
        self.assertEqual(self.w.request('/api/skills/review',body,headers={'X-Perch-Token':''})[0],401)
