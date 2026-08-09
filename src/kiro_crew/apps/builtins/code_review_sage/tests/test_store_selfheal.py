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


class TestReadConfigQuiet(unittest.TestCase):
    """read_config_quiet: side-effect-free AND no-follow. config.json sits in
    the worker-reachable data dir, and the allowlist resolution the adapters
    run on every pasted URL reads it — so a planted symlink must be refused,
    never dereferenced into whatever the gateway can read."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = Path(self.tmp) / "apps" / "code-review-sage"
        self.data = self.root / "data"
        self.data.mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_normal_config_reads(self):
        (self.data / "config.json").write_text(
            json.dumps({"github_hosts": ["github.com"]}), encoding="utf-8")
        self.assertEqual(store.read_config_quiet(self.root),
                         {"github_hosts": ["github.com"]})

    def test_missing_config_is_empty_and_creates_nothing(self):
        shutil.rmtree(self.data)
        self.assertEqual(store.read_config_quiet(self.root), {})
        self.assertFalse(self.data.exists())   # never self-heals the layout

    def test_non_dict_payload_is_empty(self):
        (self.data / "config.json").write_text("[1, 2, 3]", encoding="utf-8")
        self.assertEqual(store.read_config_quiet(self.root), {})

    def test_symlinked_config_is_refused_not_dereferenced(self):
        # A worker-planted link pointing OUTSIDE the data dir: the gate must
        # refuse it, so URL parsing can never make the gateway follow a link
        # to a blocked credential file.
        outside = Path(self.tmp) / "outside.json"
        outside.write_text(json.dumps({"github_hosts": ["evil.example"]}),
                           encoding="utf-8")
        try:
            (self.data / "config.json").symlink_to(outside)
        except (OSError, NotImplementedError):  # pragma: no cover
            self.skipTest("symlinks unavailable on this host")
        self.assertEqual(store.read_config_quiet(self.root), {})
