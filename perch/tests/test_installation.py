"""Desktop installation repair does not overwrite unrelated commands."""

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from perch.desktop import installation


class InstallationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home=Path(self.temp.name)
        self.app=self.home/'Applications/Perch.app'
        (self.app/'Contents/MacOS').mkdir(parents=True)
        (self.app/'Contents/MacOS/perch-cli').write_text('fixture executable')
        self.bin=self.home/'.local/bin'
        self.bin.mkdir(parents=True)
        platform=patch.object(installation.sys,'platform','darwin')
        platform.start()
        self.addCleanup(platform.stop)

    def test_creates_regular_launchers_and_second_repair_is_noop(self):
        result=installation.repair(self.app,self.home)
        self.assertTrue(result['repaired'])
        for name in ('perch','perch-cli'):
            path=self.bin/name
            self.assertFalse(path.is_symlink())
            self.assertIn('"$@"',path.read_text())
        self.assertTrue(all(s['status']=='ready' for s in result['shortcuts']))
        self.assertEqual(installation.repair(self.app,self.home)['backups'],[])

    def test_migrates_old_symlink_without_touching_its_executable(self):
        path=self.bin/'perch'
        try:
            path.symlink_to(self.app/'Contents/MacOS/perch-cli')
        except OSError:
            self.skipTest('Symbolic links unavailable')
        result=installation.repair(self.app,self.home)
        self.assertFalse(path.is_symlink())
        self.assertTrue(Path(result['backups'][0]).is_symlink())
        self.assertEqual((self.app/'Contents/MacOS/perch-cli').read_text(),'fixture executable')

    def test_unrelated_shortcut_is_preserved_and_cli_is_optional(self):
        path=self.bin/'perch'
        path.write_text('unrelated command')
        self.assertEqual(installation.status(self.app,self.home)['shortcuts'][0]['status'],'conflict')
        with self.assertRaises(ValueError):
            installation.repair(self.app,self.home)
        self.assertTrue(installation.repair(self.app,self.home,cli=False)['repaired'])
        self.assertEqual(path.read_text(),'unrelated command')

    def test_failed_launcher_write_restores_previous_shortcut(self):
        path=self.bin/'perch'
        previous=installation.launcher(self.app).replace(installation.MARKER+'\n','')
        path.write_text(previous)
        original=installation.atomic_write
        def fail(path,text,**kwargs):
            if Path(path).name=='perch-cli':
                raise OSError('fixture write failure')
            return original(path,text,**kwargs)
        with patch.object(installation,'atomic_write',side_effect=fail), self.assertRaises(OSError):
            installation.repair(self.app,self.home)
        self.assertEqual(path.read_text(),previous)
        self.assertFalse((self.bin/'perch-cli').exists())

    def test_bundle_install_validates_then_preserves_previous_app(self):
        source=self.home/'Downloads/Perch.app'
        (source/'Contents/MacOS').mkdir(parents=True)
        (source/'Contents/MacOS/perch-cli').write_text('new executable')
        with patch.object(installation.subprocess,'run') as run:
            result=installation.repair(source,self.home,cli=False)
        self.assertIn('--version',run.call_args.args[0])
        self.assertEqual((self.app/'Contents/MacOS/perch-cli').read_text(),'new executable')
        self.assertEqual((Path(result['backups'][0])/'Contents/MacOS/perch-cli').read_text(),'fixture executable')

    @unittest.skipUnless(os.name=='nt','Windows registry integration')
    def test_windows_path_preserves_existing_entries(self):
        import winreg
        from unittest.mock import MagicMock

        with patch.object(installation.sys,'platform','win32'), patch.object(winreg,'CreateKey',return_value=MagicMock()), patch.object(winreg,'QueryValueEx',return_value=('C:\\Existing',winreg.REG_EXPAND_SZ)), patch.object(winreg,'SetValueEx') as write:
            (self.app/'Perch.exe').write_text('fixture')
            (self.app/'perch-cli.exe').write_text('fixture')
            installation.repair(self.app,self.home)
            self.assertEqual(write.call_args.args[-1],'C:\\Existing;'+str(self.app))
