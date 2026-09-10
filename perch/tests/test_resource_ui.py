"""Run resource interaction regressions with Node's dependency-free test runner."""

from pathlib import Path
import shutil
import subprocess
import unittest


class ResourceInteractionTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node is required for UI interaction checks')
    def test_async_resource_interactions(self):
        result = subprocess.run(
            ['node', '--test', str(Path(__file__).with_name('resource_interactions.test.cjs'))],
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
