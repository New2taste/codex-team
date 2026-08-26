import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import sync_plugin


ROOT = Path(__file__).resolve().parents[1]


class SyncPluginTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "config").mkdir()
        (self.root / "scripts").mkdir()
        (self.root / "plugins" / "ai-workflow" / "config").mkdir(parents=True)
        (self.root / "plugins" / "ai-workflow" / "runtime").mkdir()
        (self.root / "config" / "sample.toml").write_text("root config\n")
        (self.root / "scripts" / "sample.py").write_text("ROOT = True\n")
        (self.root / "plugins" / "ai-workflow" / "config" / "sample.toml").write_text(
            "stale config\n"
        )
        (self.root / "plugins" / "ai-workflow" / "runtime" / "sample.py").write_text(
            "STALE = True\n"
        )
        self.manifest = mock.patch.multiple(
            sync_plugin,
            CONFIG_FILES=("sample.toml",),
            RUNTIME_FILES=("sample.py",),
        )
        self.manifest.start()

    def tearDown(self):
        self.manifest.stop()
        self.temporary.cleanup()

    def test_check_reports_drift_without_writing(self):
        with self.assertRaisesRegex(sync_plugin.SyncError, "differs"):
            sync_plugin.synchronize(self.root, write=False)

        self.assertEqual(
            "stale config\n",
            (
                self.root
                / "plugins"
                / "ai-workflow"
                / "config"
                / "sample.toml"
            ).read_text(),
        )

    def test_write_copies_only_the_fixed_manifest_then_check_passes(self):
        changed = sync_plugin.synchronize(self.root, write=True)

        self.assertEqual(
            (
                "plugins/ai-workflow/config/sample.toml",
                "plugins/ai-workflow/runtime/sample.py",
            ),
            changed,
        )
        self.assertEqual((), sync_plugin.synchronize(self.root, write=False))
        self.assertEqual(
            (self.root / "config" / "sample.toml").read_bytes(),
            (
                self.root
                / "plugins"
                / "ai-workflow"
                / "config"
                / "sample.toml"
            ).read_bytes(),
        )
        self.assertEqual(
            (self.root / "scripts" / "sample.py").read_bytes(),
            (
                self.root
                / "plugins"
                / "ai-workflow"
                / "runtime"
                / "sample.py"
            ).read_bytes(),
        )

    def test_verify_all_is_one_deterministic_zero_model_entrypoint(self):
        script = (ROOT / "scripts" / "verify_all.sh").read_text(encoding="utf-8")

        self.assertIn("-m unittest discover -s tests", script)
        self.assertIn("-m compileall -q", script)
        self.assertIn("scripts/sync_plugin.py --check", script)
        self.assertIn("plugins/ai-workflow/scripts/verify.sh", script)
        self.assertIn("git diff --check", script)
        self.assertNotIn("codex exec", script)


if __name__ == "__main__":
    unittest.main()
