"""Exercise the macOS installer against a local release-shaped HTTP server."""

import hashlib
import http.server
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
import zipfile


@unittest.skipUnless(sys.platform == "darwin", "macOS installer test")
class MacInstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.release = self.root / "releases" / "latest" / "download"
        self.release.mkdir(parents=True)
        self.make_release("a" * 64)
        def handler(*args, **kwargs):
            return http.server.SimpleHTTPRequestHandler(*args, directory=self.root, **kwargs)

        self.http = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.http.server_close)
        self.addCleanup(self.http.shutdown)

    def make_release(self, checksum):
        app = self.root / "app" / "Perch.app" / "Contents" / "MacOS"
        app.mkdir(parents=True, exist_ok=True)
        cli = app / "perch-cli"
        cli.write_text("#!/bin/sh\necho Perch\n")
        cli.chmod(0o755)
        archive = self.release / "Perch-macOS-arm64.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            for file in (self.root / "app").rglob("*"):
                if file.is_file():
                    bundle.write(file, file.relative_to(self.root / "app"))
        if checksum == "a" * 64:
            checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
        (self.release / "SHA256SUMS").write_text(f"{checksum}  {archive.name}\n")

    def run_installer(self):
        install_dir = self.root / "Applications"
        bin_dir = self.root / "bin"
        env = {
            **os.environ,
            "PERCH_DOWNLOAD_BASE": f"http://127.0.0.1:{self.http.server_port}/releases",
            "PERCH_INSTALL_DIR": str(install_dir),
            "PERCH_BIN_DIR": str(bin_dir),
        }
        script = Path(__file__).parents[1] / "install.sh"
        return subprocess.run(["sh", str(script)], text=True, capture_output=True, env=env), install_dir, bin_dir

    def test_installs_verified_release_and_cli_links(self):
        result, install_dir, bin_dir = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((install_dir / "Perch.app" / "Contents" / "MacOS" / "perch-cli").is_file())
        self.assertFalse((bin_dir / "perch").is_symlink())
        self.assertEqual(subprocess.check_output([str(bin_dir / "perch-cli"), "--version"], text=True).strip(), "Perch")

    def test_bad_checksum_preserves_existing_install(self):
        existing = self.root / "Applications" / "Perch.app"
        existing.mkdir(parents=True)
        (existing / "previous").write_text("keep")
        self.make_release("0" * 64)
        result, install_dir, _ = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((install_dir / "Perch.app" / "previous").read_text(), "keep")

    def test_upgrade_migrates_symlinks_and_preserves_old_app(self):
        result, app, bins = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        for name in ('perch','perch-cli'):
            (bins/name).unlink()
            (bins/name).symlink_to(app/'Perch.app/Contents/MacOS/perch-cli')
        (app/'Perch.app/old-marker').write_text('previous')
        result, _, _ = self.run_installer()
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertFalse((bins/'perch').is_symlink())
        self.assertTrue(any((p/'old-marker').exists() for p in app.glob('.Perch.app.previous.*')))

    def test_unrelated_shortcut_preserves_installed_app(self):
        result, app, bins = self.run_installer()
        self.assertEqual(result.returncode,0,result.stderr)
        (app/'Perch.app/old-marker').write_text('keep')
        (bins/'perch').write_text('unrelated')
        result, _, _ = self.run_installer()
        self.assertNotEqual(result.returncode,0)
        self.assertEqual((bins/'perch').read_text(),'unrelated')
        self.assertEqual((app/'Perch.app/old-marker').read_text(),'keep')

    def test_launcher_quotes_folder_metacharacters(self):
        # Exercise the actual installed launcher without evaluating folder text.
        self.root=self.root/'''spaces ' $HOME `echo unsafe`'''
        self.root.mkdir()
        self.release=self.root/'releases/latest/download'
        self.release.mkdir(parents=True)
        self.make_release('a'*64)
        # The server handler follows self.root, so the same download URL is valid.
        result, _, bins=self.run_installer()
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(subprocess.check_output([str(bins/'perch'),'--version'],text=True).strip(),'Perch')

    def test_failed_new_executable_rolls_back_app_and_launchers(self):
        result, app, bins=self.run_installer()
        self.assertEqual(result.returncode,0,result.stderr)
        (app/'Perch.app/previous').write_text('keep')
        previous=(bins/'perch').read_bytes()
        archive=self.release/'Perch-macOS-arm64.zip'
        entry=zipfile.ZipInfo('Perch.app/Contents/MacOS/perch-cli')
        entry.external_attr=0o100755 << 16
        with zipfile.ZipFile(archive,'w') as bundle:
            bundle.writestr(entry,'#!/bin/sh\nexit 7\n')
        (self.release/'SHA256SUMS').write_text(hashlib.sha256(archive.read_bytes()).hexdigest()+'  '+archive.name+'\n')
        result, _, _=self.run_installer()
        self.assertNotEqual(result.returncode,0)
        self.assertEqual((app/'Perch.app/previous').read_text(),'keep')
        self.assertEqual((bins/'perch').read_bytes(),previous)
