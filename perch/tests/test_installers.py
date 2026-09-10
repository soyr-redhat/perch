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
        self.assertEqual(os.readlink(bin_dir / "perch"), str(install_dir / "Perch.app" / "Contents" / "MacOS" / "perch-cli"))
        self.assertEqual(os.readlink(bin_dir / "perch-cli"), str(install_dir / "Perch.app" / "Contents" / "MacOS" / "perch-cli"))

    def test_bad_checksum_preserves_existing_install(self):
        existing = self.root / "Applications" / "Perch.app"
        existing.mkdir(parents=True)
        (existing / "previous").write_text("keep")
        self.make_release("0" * 64)
        result, install_dir, _ = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((install_dir / "Perch.app" / "previous").read_text(), "keep")
