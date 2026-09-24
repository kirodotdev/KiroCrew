"""Tests for the data-layout self-heal in store.py.

Locks in the fix for the "Initializing…" stuck state: when the generic app
config handler has already seeded an empty ``{}`` config.json, ensure_layout
must upgrade it to include ``resolved_paths`` so the UI can bootstrap."""
import collections
import contextlib
import errno
import json
import os
import shutil
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from sage_lib import store

from kiro_crew.apps.builtins.code_review_sage.tests.fixtures import SYMLINKS_OK


class TestPinnedAtomicWrite(unittest.TestCase):
    """``atomic_write_locked`` resolves the parent directory ONCE.

    Resolving the parent by NAME for the staging temp and again for the rename
    that publishes it (``mkstemp(dir=...)`` plus both halves of ``os.replace``) is
    three resolutions. The review worker runs prompt-injected model output and has
    a shell inside its own run tree, so it can swap a directory for a symlink
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
        # The subject is the staging NAME. A 240-char leaf under any temp dir is a
        # PATH past Windows' 260-character cap, which the OS refuses outright
        # (WinError 3) unless long paths are enabled -- true on the CI runners,
        # false on a stock developer box. Probe the capability rather than the OS:
        # a host that can hold the path keeps the coverage.
        try:
            with open(target, "wb"):
                pass
            target.unlink()
        except OSError as exc:
            self.skipTest(f"host cannot address a {len(str(target))}-char path: {exc}")

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


class TestLayoutSeedingIsSerialized(unittest.TestCase):
    """Concurrent entrants must not each publish the same seeded file.

    ``ensure_layout`` runs on every action and reviews run as separate PROCESSES,
    so several can each find one seed absent and each publish it. On POSIX the
    duplicate renames are harmless; on Windows ``os.replace`` raises
    ``PermissionError`` when a handle is open on the destination or another rename
    is landing on it, and the loser raises out of ``atomic_write_locked`` and
    fails its whole action. The exclusion is what these pin, on every platform,
    because the race is platform-independent even though only one platform
    punishes it.
    """

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def test_every_seeding_publish_happens_while_the_layout_lock_is_held(self):
        """A publish outside the lock is a publish another entrant can duplicate."""
        real_lock = store.layout_lock
        real_write = store.atomic_write_text
        held: list[bool] = []
        published: list[tuple[str, bool]] = []

        @contextlib.contextmanager
        def tracking_lock(root=None):
            with real_lock(root):
                held.append(True)
                try:
                    yield
                finally:
                    held.pop()

        def spy(path, text):
            published.append((Path(path).name, bool(held)))
            return real_write(path, text)

        with mock.patch.object(store, "layout_lock", tracking_lock), \
                mock.patch.object(store, "atomic_write_text", spy):
            store.ensure_layout(self.root)

        self.assertEqual(
            sorted(name for name, _ in published),
            ["config.json", "index.json", "learned-patterns.md"],
            f"unexpected set of seeding publishes: {published}")
        self.assertEqual([name for name, was_held in published if not was_held], [],
                         f"a seed was published outside the lock: {published}")

    def test_a_second_entrant_does_not_republish_a_seed_being_published(self):
        """The test of presence is re-read INSIDE the lock, so the loser skips.

        Taking the lock and then acting on an answer read before it would leave
        the duplicate publish in place: both entrants saw the seed absent.
        """
        real_lock = store.layout_lock
        real_write = store.atomic_write_text
        reached = []
        entered = threading.Event()
        release = threading.Event()
        published: collections.Counter = collections.Counter()
        count_guard = threading.Lock()

        @contextlib.contextmanager
        def counting_lock(root=None):
            # Appended BEFORE the acquire, so the main thread can tell "the second
            # entrant has reached the lock" from "it has taken it" -- which is
            # what makes this handshake a wait rather than a sleep.
            reached.append(True)
            with real_lock(root):
                yield

        def spy(path, text):
            with count_guard:
                published[Path(path).name] += 1
                first = not entered.is_set()
                if first:
                    entered.set()
            if first:
                # Hold the first publish open so the other entrant is inside
                # ``ensure_layout`` while this seed is still absent on disk.
                self.assertTrue(release.wait(60), "the handshake never released")
            return real_write(path, text)

        errors: list[BaseException] = []

        def run():
            try:
                store.ensure_layout(self.root)
            except BaseException as exc:  # pragma: no cover - reported below
                errors.append(exc)

        entrants: list[threading.Thread] = []

        with mock.patch.object(store, "layout_lock", counting_lock), \
                mock.patch.object(store, "atomic_write_text", spy):
            try:
                first = threading.Thread(target=run)
                entrants.append(first)
                first.start()
                self.assertTrue(entered.wait(60), "the first publish never started")
                second = threading.Thread(target=run)
                entrants.append(second)
                second.start()
                deadline = time.monotonic() + 60
                while len(reached) < 2 and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertGreaterEqual(len(reached), 2,
                                        "the second entrant never reached the lock")
            finally:
                # A failed assertion above leaves an entrant parked in
                # ``release.wait``, and an entrant that outlives this test writes
                # into a tree the fixture has already removed -- so the release
                # and the joins run whether or not the handshake held.
                release.set()
                for entrant in entrants:
                    entrant.join(60)

        stranded = [entrant.name for entrant in entrants if entrant.is_alive()]
        self.assertEqual(stranded, [], f"an entrant outlived the test: {stranded}")
        self.assertEqual(errors, [], f"an entrant raised: {errors}")
        self.assertEqual(published["learned-patterns.md"], 1,
                         f"the seed was published more than once: {published}")


class TestLayoutLockFileIsGuarded(unittest.TestCase):
    """The lock file is opened, so it is also an attack surface.

    It lives in the worker-reachable data dir, so the same guards the candidate
    lock carries apply: a link planted at the name must be refused rather than
    written through, and a hardlink to a sensitive inode passes ``O_NOFOLLOW`` but
    not the link count.
    """

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.data = store.data_dir(self.root)
        self.data.mkdir(parents=True, exist_ok=True)
        self.lock_path = self.data / store._LAYOUT_LOCK_NAME
        self.victim = self.root / "precious.txt"
        self.victim.write_text("KEEP-ME\n", encoding="utf-8")

    @unittest.skipUnless(SYMLINKS_OK, "platform forbids unprivileged symlinks")
    def test_a_symlinked_lock_file_does_not_write_through_to_its_target(self):
        self.lock_path.symlink_to(self.victim)
        with self.assertRaises(OSError):
            with store.layout_lock(self.root):
                pass
        self.assertEqual(self.victim.read_text(encoding="utf-8"), "KEEP-ME\n")

    @unittest.skipUnless(SYMLINKS_OK, "platform forbids unprivileged symlinks")
    def test_a_planted_link_is_refused_where_the_platform_lacks_the_flag(self):
        """Without O_NOFOLLOW the open follows the link, so the lstat is the leg.

        On a platform that has the flag, the flag refuses a planted link and the
        name check never decides anything -- which is exactly why it needs its own
        case, with the flag masked to reach the condition. Windows does not define
        the attribute at all, so there the condition is already live and there is
        nothing to mask; patching it there raises instead. The descriptor check
        cannot cover for the name check either: a followed link yields a
        descriptor on a target that is itself a lone regular file and passes.
        """
        masked = (mock.patch.object(os, "O_NOFOLLOW", 0)
                  if hasattr(os, "O_NOFOLLOW") else contextlib.nullcontext())
        self.lock_path.symlink_to(self.victim)
        with masked:
            with self.assertRaises(OSError):
                with store.layout_lock(self.root):
                    pass
        self.assertEqual(self.victim.read_text(encoding="utf-8"), "KEEP-ME\n")

    def test_a_hardlinked_lock_file_is_refused(self):
        os.link(self.victim, self.lock_path)
        with self.assertRaises(OSError):
            with store.layout_lock(self.root):
                pass
        self.assertEqual(self.victim.read_text(encoding="utf-8"), "KEEP-ME\n")

    def test_the_ordinary_path_still_takes_the_lock(self):
        """The guard must not wedge the self-heal shut."""
        with store.layout_lock(self.root):
            pass
        st = self.lock_path.stat()
        self.assertTrue(stat.S_ISREG(st.st_mode))
        self.assertEqual(st.st_nlink, 1)

    @unittest.skipUnless(store._CAN_PIN_WALK,
                         "platform cannot open a leaf relative to a pinned directory")
    def test_the_chain_above_the_lock_is_refused_before_it_is_opened(self):
        """A link at `data` redirects a by-name open, which O_NOFOLLOW cannot see.

        The flag guards the FINAL component only, so the ancestor chain needs its
        own two legs: the refusal for a link already planted there, and the pin
        for one swapped after that refusal. Both must run before the leaf is
        opened, or the lock file lands wherever the link points -- outside the
        tree the review worker is confined to, and the worker is who plants it.
        """
        order: list[str] = []
        real_refuse = store.refuse_linked_parents
        real_pin = store.pin_record_dir
        real_open = os.open
        opened: list[bool] = []

        def refuse(path):
            order.append("refuse")
            return real_refuse(path)

        def pin(directory):
            order.append("pin")
            return real_pin(directory)

        def spy_open(path, *args, **kwargs):
            if str(path) == store._LAYOUT_LOCK_NAME or str(path) == str(self.lock_path):
                order.append("open")
                opened.append(kwargs.get("dir_fd") is not None)
            return real_open(path, *args, **kwargs)

        with mock.patch.object(store, "refuse_linked_parents", refuse), \
                mock.patch.object(store, "pin_record_dir", pin), \
                mock.patch.object(os, "open", spy_open):
            with store.layout_lock(self.root):
                pass

        self.assertEqual(order[:2], ["refuse", "pin"],
                         f"the chain guards must precede the open: {order}")
        self.assertEqual(order[-1], "open", f"the leaf opened too early: {order}")
        self.assertEqual(opened, [True],
                         "the lock leaf must be opened relative to the pinned parent")

    @unittest.skipIf(store._CAN_PIN_WALK,
                     "this is the fallback the pinning platforms do not take")
    def test_the_chain_is_still_refused_where_the_platform_cannot_pin(self):
        """Windows has no dir_fd verbs, so the leaf is opened by name there.

        That leaves the ancestor chain covered by the refusal alone, which is a
        weaker story than the pin and is stated as such on ``layout_lock``. What
        must not happen is the refusal being skipped as well: it is the only leg
        left. Runs ONLY on the platform that takes this branch, because a
        simulated capability cannot show a real host reaching it.
        """
        order: list[str] = []
        real_refuse = store.refuse_linked_parents
        real_open = os.open
        opened: list[bool] = []

        def refuse(path):
            order.append("refuse")
            return real_refuse(path)

        def spy_open(path, *args, **kwargs):
            if str(path) == store._LAYOUT_LOCK_NAME or str(path) == str(self.lock_path):
                order.append("open")
                opened.append(kwargs.get("dir_fd") is not None)
            return real_open(path, *args, **kwargs)

        with mock.patch.object(store, "refuse_linked_parents", refuse), \
                mock.patch.object(os, "open", spy_open):
            with store.layout_lock(self.root):
                pass

        self.assertEqual(order, ["refuse", "open"],
                         f"the refusal must still precede the open: {order}")
        self.assertEqual(opened, [False],
                         "this platform has no pinned open to make")
