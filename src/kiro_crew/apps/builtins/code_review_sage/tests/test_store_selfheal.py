"""Tests for the data-layout self-heal in store.py.

Locks in the fix for the "Initializing…" stuck state: when the generic app
config handler has already seeded an empty ``{}`` config.json, ensure_layout
must upgrade it to include ``resolved_paths`` so the UI can bootstrap."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from sage_lib import store


class TestSeedConfigUpgrade(unittest.TestCase):
    """_seed_config upgrade path must add resolved_paths if missing."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = Path(self.tmp) / "apps" / "code-review-sage"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_empty_config_gets_resolved_paths(self):
        """Simulates the scenario where the generic handler already wrote {}."""
        data = self.root / "data"
        data.mkdir(parents=True)
        (data / "config.json").write_text("{}\n", encoding="utf-8")

        store.ensure_layout(self.root)

        cfg = json.loads((data / "config.json").read_text(encoding="utf-8"))
        self.assertIn("resolved_paths", cfg)
        self.assertEqual(cfg["resolved_paths"]["reports"], str(data / "reports"))
        self.assertEqual(cfg["resolved_paths"]["results"], str(data / "results"))
        self.assertEqual(cfg["resolved_paths"]["learnings"], str(data / "learnings"))

    def test_existing_resolved_paths_not_overwritten(self):
        """User-edited resolved_paths must survive the upgrade."""
        data = self.root / "data"
        data.mkdir(parents=True)
        custom = {"resolved_paths": {"reports": "/custom/reports",
                                     "results": "/custom/results",
                                     "learnings": "/custom/learnings"}}
        (data / "config.json").write_text(json.dumps(custom), encoding="utf-8")

        store.ensure_layout(self.root)

        cfg = json.loads((data / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(cfg["resolved_paths"]["reports"], "/custom/reports")

    def test_fresh_install_has_resolved_paths(self):
        """Brand-new install (no config.json) should create one with resolved_paths."""
        store.ensure_layout(self.root)

        data = self.root / "data"
        cfg = json.loads((data / "config.json").read_text(encoding="utf-8"))
        self.assertIn("resolved_paths", cfg)
        self.assertEqual(cfg["resolved_paths"]["reports"], str(data / "reports"))

    def test_default_config_keys_merged_on_upgrade(self):
        """Existing config missing DEFAULT_CONFIG keys gets them added."""
        data = self.root / "data"
        data.mkdir(parents=True)
        (data / "config.json").write_text("{}\n", encoding="utf-8")

        store.ensure_layout(self.root)

        cfg = json.loads((data / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(cfg["schema"], "code-review-sage-config")
        self.assertIn("triage", cfg)
        self.assertIn("caps", cfg)


if __name__ == "__main__":
    unittest.main()
