"""Guard tests for sync: skills linking and MCP union/merge on temp fixtures.

Run: python -m unittest test_sync -v
"""

import json
import os
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from perch import sync


def make_skill(root, name):
    d = os.path.join(root, name)
    os.makedirs(d)
    with open(os.path.join(d, "SKILL.md"), "w") as fh:
        fh.write(f"---\nname: {name}\ndescription: test\n---\n")
    return d


class SkillsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.patchers = [
            patch.object(sync, "MANIFEST", os.path.join(self.tmp.name, "manifest.json")),
            patch.object(sync, "PERCH_DIR", os.path.join(self.tmp.name, "state")),
        ]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_union_links(self):
        with tempfile.TemporaryDirectory() as tmp:
            claude = os.path.join(tmp, "claude")
            codex = os.path.join(tmp, "codex")
            os.makedirs(claude)
            os.makedirs(codex)
            make_skill(claude, "alpha")
            make_skill(claude, "beta")
            make_skill(codex, "gamma")

            roots = {"claude": claude, "codex": codex}
            report = sync.sync_skills(roots=roots, targets=("claude", "codex"))

            # codex gains claude's two; claude gains codex's one
            self.assertEqual(len(report["linked"]), 3)
            self.assertTrue(os.path.isfile(os.path.join(codex, "alpha", "SKILL.md")))
            self.assertTrue(os.path.isfile(os.path.join(codex, "beta", "SKILL.md")))
            self.assertTrue(os.path.isfile(os.path.join(claude, "gamma", "SKILL.md")))
            registry = Path(sync.PERCH_DIR) / "shared" / "skills"
            self.assertEqual(os.path.realpath(registry / "alpha"), os.path.realpath(os.path.join(claude, "alpha")))
            self.assertEqual(os.path.abspath(os.readlink(os.path.join(codex, "alpha"))), str(registry / "alpha"))

            # second run is idempotent: everything already present
            again = sync.sync_skills(roots=roots, targets=("claude", "codex"))
            self.assertEqual(again["linked"], [])
            self.assertEqual(len(again["present"]), 3)

    @unittest.skipIf(os.name == "nt", "junction target inspection is POSIX-specific")
    def test_managed_direct_link_migrates_to_registry(self):
        roots = {name: os.path.join(self.tmp.name, name) for name in ("claude", "codex")}
        for root in roots.values():
            os.makedirs(root)
        source = make_skill(roots["claude"], "review")
        direct = os.path.join(roots["codex"], "review")
        os.symlink(source, direct)
        Path(sync.MANIFEST).write_text(json.dumps({"created": [{"link": direct, "target": source}]}))

        sync.sync_skills(roots=roots, targets=("codex",))

        self.assertEqual(os.path.abspath(os.readlink(direct)), str(Path(sync.PERCH_DIR) / "shared" / "skills" / "review"))


class McpTest(unittest.TestCase):
    def test_toml_roundtrip_and_merge(self):
        with tempfile.TemporaryDirectory() as tmp:
            claude = os.path.join(tmp, "claude.json")
            codex = os.path.join(tmp, "config.toml")
            omp = os.path.join(tmp, "agent", "mcp.json")

            with open(claude, "w") as fh:
                json.dump({"mcpServers": {}, "other": "keep me"}, fh)
            with open(codex, "w") as fh:
                fh.write(
                    '[projects.foo]\ntrust_level = "trusted"\n\n'
                    '[mcp_servers.github]\ncommand = "docker"\nargs = ["run", "gh-mcp"]\n\n'
                    '[mcp_servers.github.env]\nTOKEN = "abc"\n\n'
                    '[windows]\nsandbox = "elevated"\n'
                )

            paths = {
                "claude": claude,
                "claude_mcpjson": os.path.join(tmp, "nope.json"),
                "codex": codex,
                "omp": omp,
            }
            report = sync.sync_mcp(paths=paths)

            self.assertEqual(report["found"], 1)
            self.assertEqual(report["added"].get("claude"), ["github"])
            self.assertEqual(report["added"].get("omp"), ["github"])
            self.assertNotIn("codex", report.get("added", {}))  # already had it

            # claude.json got the server, unrelated keys preserved
            doc = json.loads(Path(claude).read_text())
            self.assertEqual(doc["other"], "keep me")
            self.assertEqual(doc["mcpServers"]["github"]["command"], "docker")
            self.assertEqual(doc["mcpServers"]["github"]["env"]["TOKEN"], "abc")

            # codex toml still parses, non-mcp sections intact, no dup server
            import tomllib

            with open(codex, "rb") as fh:
                tdoc = tomllib.load(fh)
            self.assertEqual(tdoc["windows"]["sandbox"], "elevated")
            self.assertEqual(tdoc["projects"]["foo"]["trust_level"], "trusted")
            self.assertEqual(tdoc["mcp_servers"]["github"]["args"], ["run", "gh-mcp"])

            # omp file created with schema + server
            odoc = json.loads(Path(omp).read_text())
            self.assertIn("$schema", odoc)
            self.assertEqual(odoc["mcpServers"]["github"]["command"], "docker")

            # second run: nothing left to add anywhere
            again = sync.sync_mcp(paths=paths)
            self.assertEqual(again["added"], {})

            registry = json.loads(sync._mcp_registry(paths).read_text())
            self.assertEqual(registry["servers"]["github"]["command"], "docker")

    def test_registry_is_a_source_for_missing_harness_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = {name: os.path.join(tmp, name + ".json") for name in ("claude", "omp")}
            registry = sync._mcp_registry(paths)
            registry.parent.mkdir(parents=True)
            registry.write_text(json.dumps({"version": 1, "servers": {"shared": {"command": "fixture"}}}))

            report = sync.sync_mcp(paths=paths, targets=("claude", "omp"))

            self.assertEqual(report["added"], {"claude": ["shared"], "omp": ["shared"]})

    def test_invalid_registry_leaves_harness_files_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = {"claude": os.path.join(tmp, "claude.json")}
            registry = sync._mcp_registry(paths)
            registry.parent.mkdir(parents=True)
            registry.write_text('{"servers": []}')

            report = sync.sync_mcp(paths=paths, targets=("claude",))

            self.assertTrue(report["errors"])
            self.assertFalse(Path(paths["claude"]).exists())


if __name__ == "__main__":
    unittest.main()
