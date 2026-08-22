"""Tests for the data-layout self-heal in store.py.

Locks in the fix for the "Initializing…" stuck state: when the generic app
config handler has already seeded an empty ``{}`` config.json, ensure_layout
must upgrade it to include ``resolved_paths`` so the UI can bootstrap."""
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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


class TestRestrictToOwner(unittest.TestCase):
    """Every record, report and cache this app stages is locked to its owner before
    it takes its final name. The lockdown must go through the runtime helper, not a
    raw ``os.chmod``: on Windows a chmod only toggles the read-only attribute,
    leaves the inherited DACL intact, and SUCCEEDS — so the file would stay
    readable by other local accounts with nothing raised to notice."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        # Registered before the write below: a failure there skips tearDown, and
        # the directory would survive the run.
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = Path(self.tmp) / "record.json"
        self.path.write_text("{}", encoding="utf-8")

    def test_delegates_to_the_runtime_helper(self):
        seen: list[object] = []
        with mock.patch.object(store, "_runtime_restrict", seen.append):
            store.restrict_to_owner(self.path)
        self.assertEqual(seen, [self.path])

    def test_lockdown_failure_propagates(self):
        # The callers stage into a private temp file and unlink it in a ``finally``;
        # they rely on the OSError to reach that cleanup instead of renaming a
        # file whose permissions were never applied.
        def boom(_path):
            raise OSError("icacls failed")

        with mock.patch.object(store, "_runtime_restrict", boom):
            with self.assertRaises(OSError):
                store.restrict_to_owner(self.path)

    def test_applies_owner_only_permissions(self):
        store.restrict_to_owner(self.path)
        if os.name == "posix":
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_the_temp_file_is_locked_before_any_payload_byte_is_written(self):
        """A new file inherits the directory's DACL on Windows, so restricting it
        only after the payload is written leaves a window in which the content is
        readable by everyone the parent grants -- and nothing tightens this app's
        data directories. The fd must therefore come back already restricted.

        Asserted by ORDER, not by permissions: the mode bits cannot express this on
        Windows, and the whole point is that the lockdown precedes the write.
        """
        order: list[str] = []
        real = store.restrict_to_owner

        def _tracked(path):
            order.append("lock")
            return real(path)

        with mock.patch.object(store, "restrict_to_owner", _tracked):
            fd, tmp = store.open_locked_temp(self.tmp)
            try:
                order.append("write")
                os.write(fd, b"sensitive")
            finally:
                os.close(fd)
        self.assertEqual(order, ["lock", "write"])
        self.assertTrue(Path(tmp).is_file())

    def test_a_failed_lockdown_leaves_no_descriptor_and_no_temp_file(self):
        # The caller never receives the fd or the path on this path, so nothing
        # else can close or remove them: a leak here would orphan one descriptor
        # and one stray temp file per failed write.
        def boom(_path):
            raise OSError("icacls failed")

        before = set(os.listdir(self.tmp))
        with mock.patch.object(store, "restrict_to_owner", boom):
            with self.assertRaises(OSError):
                store.open_locked_temp(self.tmp)
        self.assertEqual(set(os.listdir(self.tmp)), before,
                         "a temp file was left behind by the failed lockdown")
