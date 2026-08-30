"""Tests for the data-layout self-heal in store.py.

Locks in the fix for the "Initializing…" stuck state: when the generic app
config handler has already seeded an empty ``{}`` config.json, ensure_layout
must upgrade it to include ``resolved_paths`` so the UI can bootstrap."""
import errno
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sage_lib import store

from kiro_crew.apps.builtins.code_review_sage.tests.fixtures import SYMLINKS_OK


class TestPinnedAtomicWrite(unittest.TestCase):
    """``atomic_write_locked`` resolves the parent directory ONCE.

    The staging temp and the rename that publishes it used to resolve the parent
    by NAME three times over (``mkstemp(dir=...)`` plus both halves of
    ``os.replace``). The review worker runs prompt-injected model output and has
    a shell inside its own run tree, so it could swap a directory for a symlink
    between those resolutions and steer the write out of the sandbox.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        # Registered at creation, not in tearDown: an exception later in setUp
        # skips tearDown entirely, and the residue is a directory tree.
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_a_plain_write_lands_owner_only_and_leaves_no_temp(self):
        # The primitive does not create the parent -- every production caller
        # does, and an exist_ok mkdir inside it would resurrect a namespace mid
        # deletion. So the test creates it too.
        target = self.tmp / "nested" / "record.json"
        target.parent.mkdir(parents=True)

        store.atomic_write_locked(target, b"payload")

        self.assertEqual(target.read_bytes(), b"payload")
        # Owner-only from the creation mode, so no follow-up chmod by name.
        if os.name == "posix":
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertEqual([p.name for p in target.parent.iterdir()], ["record.json"])

    def test_a_target_name_near_the_filename_limit_still_publishes(self):
        """`safe_change_id` does not cap length, so a change id built from a long
        GHE host, owner and repo produces a stem that already approaches
        NAME_MAX. A temp name derived from the target would exceed it and fail
        with ENAMETOOLONG, writing no record at all; the staging name is fixed
        length for exactly that reason.
        """
        target = self.tmp / ("x" * 240 + ".json")

        store.atomic_write_locked(target, b"payload")

        self.assertEqual(target.read_bytes(), b"payload")

    def test_a_short_write_still_publishes_the_whole_record(self):
        """``os.write`` may accept fewer bytes than it is given, and near a full
        disk it does. A one-shot call would publish a truncated record -- and
        ``results.adopt_into_run`` deletes its source once the publish returns,
        so a truncation there loses the only valid copy.
        """
        target = self.tmp / "record.json"
        payload = b'{"change_id": "abc", "verdict": "ok"}'
        real_write = os.write
        calls = {"n": 0}

        def dribbling_write(fd, data):
            calls["n"] += 1
            return real_write(fd, bytes(data)[:1])  # one byte per call

        with mock.patch.object(os, "write", dribbling_write):
            store.atomic_write_locked(target, payload)

        self.assertEqual(target.read_bytes(), payload)
        self.assertEqual(calls["n"], len(payload), "the write was not looped")

    @unittest.skipUnless(
        SYMLINKS_OK and store._CAN_PIN_DIR, "needs symlinks and dir_fd support"
    )
    def test_a_parent_swapped_mid_write_cannot_redirect_the_write(self):
        """The swap lands in the window the finding names, and is defeated.

        Mid-write the parent is renamed aside and a symlink to an attacker-owned
        directory takes its NAME. Two things must hold, and the second is why
        this raises rather than returning quietly:

        * nothing goes through the link -- the bytes land in the inode that was
          pinned, which resolving the name again would not have done;
        * the call REFUSES, because that pinned inode is now detached from the
          caller's path. Publishing into a directory nobody can reach and
          reporting success is how ``results.adopt_into_run`` would delete its
          only reachable copy.
        """
        real = self.tmp / "real"
        real.mkdir()
        impostor = self.tmp / "impostor"
        impostor.mkdir()
        target = real / "record.json"
        real_write = os.write
        state = {"swapped": False}

        def swapping_write(fd, data):
            if not state["swapped"]:
                state["swapped"] = True
                real.rename(self.tmp / "moved")
                (self.tmp / "real").symlink_to(impostor)
            return real_write(fd, data)

        with mock.patch.object(os, "write", swapping_write):
            with self.assertRaises(OSError) as caught:
                store.atomic_write_locked(target, b"payload")

        self.assertTrue(state["swapped"], "the swap never ran, so this proved nothing")
        self.assertEqual(caught.exception.errno, errno.ESTALE)
        # Landed in the inode that was pinned, never through the planted link.
        self.assertEqual((self.tmp / "moved" / "record.json").read_bytes(), b"payload")
        self.assertEqual(list(impostor.iterdir()), [])


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
