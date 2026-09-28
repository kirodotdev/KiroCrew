"""The ancestor CHAIN above a sage record, not just the record's own name.

``O_NOFOLLOW`` guards the FINAL component of a path and nothing above it, and
``mkdir(parents=True)`` creates THROUGH a link it meets, so a reviewer worker --
which runs prompt-injected model output and has a shell inside its own run tree
-- can plant a link at an intermediate directory and take the write with it. The
guard has two halves, and each test below names which half it exercises:

* :func:`store.refuse_linked_parents` refuses a link that is ALREADY on the
  chain, which is the shape that can be staged at leisure;
* :func:`store.pin_record_dir` walks the chain with one ``openat`` per component,
  which refuses a component swapped after the refusal ran.

The gap between the two halves turns on which NAMES the walk is handed, and gets
a test for each outcome: closed inside the trees Kiro Crew owns, and open outside
them, where a link cannot be told from the operator's own layout.
"""

import errno
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sage_lib import learning, store

from kiro_crew.apps.builtins.code_review_sage.tests.fixtures import SYMLINKS_OK


# The trust anchor is the runtime's own data roots, and a temp directory is
# outside all of them. Outside every owned root the refusal can only walk up to
# the first ancestor that already EXISTS -- above that, a link is the operator's
# layout and not something to judge -- so a test that plants a link inside a
# pre-existing temp tree would find it deliberately exempt. Pointing one owned
# root at the temp tree puts the plant BELOW the anchor, which is where a sage
# record actually lives.
def _anchor_at(path: Path):
    """Patch the owned-root resolvers so *path* is the trust anchor."""
    return mock.patch("kiro_crew.config.paths.data_home", lambda: str(path))


@unittest.skipUnless(SYMLINKS_OK, "planting the attack needs symlink creation")
class TestPlantedLinkRefusedBeforeTheTreeIsBuilt(unittest.TestCase):
    """``mkdir_refusing_links``: the refusal half, on the directory-create path."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_a_link_at_an_intermediate_directory_is_refused(self):
        """The component that is neither the leaf nor the immediate parent.

        ``O_NOFOLLOW`` on a single open would never see this one, and the bare
        ``mkdir(parents=True)`` it replaces would have built the whole tree
        inside the impostor and returned success.
        """
        impostor = self.tmp / "impostor"
        impostor.mkdir()
        (self.tmp / "data").symlink_to(impostor)
        target = self.tmp / "data" / "runs" / "r1" / "report"

        with _anchor_at(self.tmp):
            with self.assertRaises(OSError):
                store.mkdir_refusing_links(target)

        # The point of refusing BEFORE the mkdir: nothing exists under the link.
        self.assertEqual(list(impostor.iterdir()), [])

    def test_a_link_at_the_directory_itself_is_refused(self):
        """The last component counts too, because this call CREATES it.

        The refusal exempts a leaf, which is correct for a file being renamed
        over but wrong for a directory being created, so the helper joins a name
        below the directory before asking. Without that join this plant would
        pass.
        """
        impostor = self.tmp / "impostor"
        impostor.mkdir()
        (self.tmp / "reports").symlink_to(impostor)

        with _anchor_at(self.tmp):
            with self.assertRaises(OSError):
                store.mkdir_refusing_links(self.tmp / "reports")

        self.assertEqual(list(impostor.iterdir()), [])

    def test_a_clean_chain_is_created_and_is_idempotent(self):
        target = self.tmp / "data" / "runs" / "r1" / "report"

        with _anchor_at(self.tmp):
            first = store.mkdir_refusing_links(target)
            second = store.mkdir_refusing_links(target)

        self.assertTrue(target.is_dir())
        self.assertEqual(first, second)

    def test_a_root_outside_the_data_home_still_works(self):
        """Callers pass their own ``root``, and the tests use a temp directory.

        With no owned root above it the refusal walks up to the first existing
        ancestor and stops, so an out-of-tree root keeps working rather than
        being refused for being unrecognised.
        """
        target = self.tmp / "elsewhere" / "runs" / "r1"

        store.mkdir_refusing_links(target)

        self.assertTrue(target.is_dir())


@unittest.skipUnless(SYMLINKS_OK, "planting the attack needs symlink creation")
class TestPlantedLinkRefusedOnThePublishPath(unittest.TestCase):
    """``atomic_write_locked``: the same refusal, where the bytes land."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_a_link_above_the_parent_takes_no_bytes(self):
        impostor = self.tmp / "impostor"
        impostor.mkdir()
        (self.tmp / "data").symlink_to(impostor)
        parent = self.tmp / "data" / "runs"
        # Staged the way an attacker leaves the ground: the run directory already
        # exists through the link, so the publish is not saved by a missing parent.
        parent.mkdir(parents=True, exist_ok=True)

        with _anchor_at(self.tmp):
            with self.assertRaises(OSError):
                store.atomic_write_locked(parent / "record.json", b"payload")

        self.assertEqual([p.name for p in impostor.iterdir()], ["runs"])
        self.assertEqual(list((impostor / "runs").iterdir()), [])

    def test_a_record_still_publishes_through_a_clean_chain(self):
        parent = self.tmp / "data" / "runs" / "r1"
        parent.mkdir(parents=True)

        with _anchor_at(self.tmp):
            store.atomic_write_locked(parent / "record.json", b"payload")

        self.assertEqual((parent / "record.json").read_bytes(), b"payload")
        self.assertEqual([p.name for p in parent.iterdir()], ["record.json"])


@unittest.skipUnless(SYMLINKS_OK, "planting the attack needs symlink creation")
@unittest.skipUnless(
    hasattr(os, "O_NOFOLLOW") and os.open in os.supports_dir_fd,
    "the pinned walk needs openat with O_NOFOLLOW",
)
class TestComponentSwappedAfterTheChainWasResolved(unittest.TestCase):
    """``pin_record_dir``: the half the refusal cannot cover.

    A stat answers for the instant it ran. The walk is what makes the answer
    hold: each component is opened with ``O_NOFOLLOW`` relative to the previous
    one's descriptor, so a component that turns into a link after the path was
    resolved fails its own open instead of being followed.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_a_component_that_becomes_a_link_mid_walk_is_refused(self):
        impostor = self.tmp / "impostor"
        (impostor / "runs").mkdir(parents=True)
        mid = self.tmp / "data"
        (mid / "runs").mkdir(parents=True)
        target_dir = mid / "runs"
        real_anchored = store._runtime_anchored_parent
        if real_anchored is None:  # pragma: no cover - standalone fallback
            self.skipTest("no runtime anchor policy to wrap")
        state = {"planted": False}

        # The swap lands after the walk's names are fixed and before it opens
        # them, which is the instant the pin exists to survive.
        def anchor_then_plant(parent):
            out = real_anchored(parent)
            state["planted"] = True
            mid.rename(Path(str(mid) + ".moved"))
            mid.symlink_to(impostor)
            return out

        with mock.patch.object(store, "_runtime_anchored_parent", anchor_then_plant):
            with _anchor_at(self.tmp):
                with self.assertRaises(OSError) as caught:
                    store.pin_record_dir(target_dir)

        self.assertTrue(state["planted"], "the swap never ran, so this proved nothing")
        self.assertIsInstance(caught.exception, store.LinkedAncestorRefusal)
        self.assertEqual(caught.exception.errno, errno.ELOOP)

    def test_a_clean_chain_hands_back_a_usable_descriptor(self):
        target_dir = self.tmp / "data" / "runs"
        target_dir.mkdir(parents=True)

        fd = store.pin_record_dir(target_dir)
        try:
            pinned = os.fstat(fd)
        finally:
            os.close(fd)

        named = os.stat(str(target_dir))
        self.assertEqual((pinned.st_dev, pinned.st_ino), (named.st_dev, named.st_ino))


@unittest.skipUnless(SYMLINKS_OK, "planting the attack needs symlink creation")
class TestTheWindowBetweenTheRefusalAndTheWalk(unittest.TestCase):
    """The gap between the two halves, and where it is closed.

    The refusal is an ``lstat`` walk, so it answers for the instant it ran and a
    link planted just after it wins that race. What decides whether the pin then
    catches the link is which NAMES the pin is handed: names kept lexical below
    the trust anchor put the planted component back in the walk's path, where its
    own ``O_NOFOLLOW`` open refuses it.

    Both tests plant in exactly that window. They differ only in whether the tree
    has an owned anchor, which is what the two outcomes turn on.

    Catching the plant needs a pinned walk to exist at all, so the test that
    asserts the refusal carries the module's own :data:`store._CAN_PIN_DIR` probe
    as its gate. Where that probe is False -- Windows, which has no ``openat`` --
    the publish takes the by-name branch, the window is open on both sides of the
    anchor, and only the ``lstat`` refusal of an already-planted link is left.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _plant_after_the_refusal(self, mid: Path, impostor: Path, state: dict):
        """Wrap the refusal so the link lands the instant after it passes."""
        real_refuse = store._runtime_refuse_linked
        if real_refuse is None:  # pragma: no cover - standalone fallback
            self.skipTest("no runtime refusal to wrap")

        def refuse_then_plant(path):
            real_refuse(path)
            state["planted"] = True
            mid.rename(Path(str(mid) + ".moved"))
            mid.symlink_to(impostor)

        return mock.patch.object(store, "_runtime_refuse_linked", refuse_then_plant)

    @unittest.skipUnless(store._CAN_PIN_DIR, "the window is only closed by a pinned walk")
    def test_a_link_planted_inside_the_window_is_refused_below_the_anchor(self):
        """Where sage records actually live, the race is lost by the attacker."""
        impostor = self.tmp / "impostor"
        (impostor / "runs").mkdir(parents=True)
        mid = self.tmp / "data"
        (mid / "runs").mkdir(parents=True)
        state = {"planted": False}

        with self._plant_after_the_refusal(mid, impostor, state):
            with _anchor_at(self.tmp):
                with self.assertRaises(OSError):
                    store.atomic_write_locked(mid / "runs" / "record.json", b"payload")

        self.assertTrue(state["planted"], "the plant never ran, so this proved nothing")
        self.assertEqual(list((impostor / "runs").iterdir()), [])

    def test_a_link_planted_inside_the_window_is_followed_outside_every_root(self):
        """With no owned root above it, the chain is resolved whole and followed.

        Deliberate, and the same trade the refusal itself makes there: outside the
        trees Kiro Crew creates, a link is the operator's layout -- a data home on
        another disk, ``/tmp`` itself on macOS -- and refusing every one of them
        would break a caller that passes its own ``root``. Asserted so the
        boundary is a decision on the record rather than a belief.
        """
        impostor = self.tmp / "impostor"
        (impostor / "runs").mkdir(parents=True)
        mid = self.tmp / "data"
        (mid / "runs").mkdir(parents=True)
        state = {"planted": False}

        with self._plant_after_the_refusal(mid, impostor, state):
            store.atomic_write_locked(mid / "runs" / "record.json", b"payload")

        self.assertTrue(state["planted"], "the plant never ran, so this proved nothing")
        self.assertEqual((impostor / "runs" / "record.json").read_bytes(), b"payload")


@unittest.skipUnless(SYMLINKS_OK, "planting the attack needs symlink creation")
class TestLayoutHelpersRefuseAPlantedLink(unittest.TestCase):
    """``ensure_layout`` and ``ensure_run_layout`` build the tree, so they guard it."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_a_link_at_a_layout_child_is_refused(self):
        impostor = self.tmp / "impostor"
        impostor.mkdir()
        data = self.tmp / "data"
        data.mkdir()
        (data / "learnings").symlink_to(impostor)

        with _anchor_at(self.tmp):
            with self.assertRaises(OSError):
                store.ensure_layout(self.tmp)

        self.assertEqual(list(impostor.iterdir()), [])

    def test_a_link_at_a_run_subdirectory_is_refused(self):
        impostor = self.tmp / "impostor"
        impostor.mkdir()
        rd = self.tmp / "data" / "runs" / "r1"
        rd.mkdir(parents=True)
        (rd / "report").symlink_to(impostor)

        with _anchor_at(self.tmp):
            with self.assertRaises(OSError):
                store.ensure_run_layout("r1", self.tmp)

        self.assertEqual(list(impostor.iterdir()), [])

    def test_a_clean_layout_is_still_built_and_seeded(self):
        paths = store.ensure_layout(self.tmp)

        self.assertTrue(Path(paths["learningsCommon"]).is_file())
        self.assertTrue(Path(paths["reportsIndex"]).is_file())
        self.assertIn("resolved_paths", json.loads(Path(paths["configPath"]).read_text()))

    def test_a_link_at_the_config_name_contributes_no_keys(self):
        """Seeding READS ``config.json`` before publishing the merged document back.

        The publish lands on the NAME, so a planted link is replaced rather than
        written through -- which is exactly what makes the READ the dangerous leg
        here. A following read would merge the aliased document's keys into the
        document published under this name, turning the alias into a real file
        that ``load_config`` then serves as configuration. So the plant is
        refused, and the file is left as it was found.
        """
        victim = self.tmp / "foreign-settings.json"
        foreign = '{"borrowed_key": "not-from-this-file"}'
        victim.write_text(foreign, encoding="utf-8")
        data = self.tmp / "data"
        data.mkdir()
        cfg = data / "config.json"
        try:
            cfg.symlink_to(victim)
        except (OSError, NotImplementedError):
            self.skipTest("planting the attack needs symlink creation")

        with _anchor_at(self.tmp):
            store.ensure_layout(self.tmp)

        self.assertTrue(cfg.is_symlink(), "the plant was republished as a real file")
        self.assertEqual(
            victim.read_text(encoding="utf-8"), foreign, "the aliased document was written through"
        )
        self.assertNotIn(
            "borrowed_key",
            store.read_config_quiet(self.tmp),
            "the aliased document was served as configuration",
        )


class TestTheConsolidationsLogStaysAnAppend(unittest.TestCase):
    """The one write deliberately NOT moved onto the staged-replace helper.

    A staged replace would have to read the whole log, add a line and rename a
    fresh file over it. Two consolidations racing that sequence each publish a
    file missing the other's entry, so a completed consolidation vanishes from
    the log. ``O_APPEND`` keeps each record indivisible instead, and the
    directory above it -- the part a planted link redirects -- is guarded.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_two_records_both_survive(self):
        learning._record_consolidation(1, "alpha", self.tmp)
        learning._record_consolidation(2, "beta", self.tmp)

        log = learning._consolidations_log(self.tmp)
        lines = [json.loads(ln) for ln in log.read_text().splitlines() if ln.strip()]

        self.assertEqual([e["consolidated"] for e in lines], [1, 2])
        self.assertEqual([e["namespace"] for e in lines], ["alpha", "beta"])

    @unittest.skipUnless(SYMLINKS_OK, "planting the attack needs symlink creation")
    def test_a_link_at_the_log_name_takes_no_line(self):
        """An append FOLLOWS a link at the name it opens, so the name is checked.

        This is the one way the append is weaker than the staged replace it keeps
        out of: a rename over a link replaces the link, while an append writes
        THROUGH it into the target. The check is an ``lstat`` rather than
        ``O_NOFOLLOW`` alone, so it holds on the platform that has no such flag.
        """
        impostor = self.tmp / "impostor.jsonl"
        impostor.write_text("")
        log = learning._consolidations_log(self.tmp)
        store.mkdir_refusing_links(log.parent)
        log.symlink_to(impostor)

        with self.assertRaises(OSError):
            learning._record_consolidation(1, "alpha", self.tmp)

        self.assertEqual(impostor.read_text(), "")

    @unittest.skipUnless(SYMLINKS_OK, "planting the attack needs symlink creation")
    def test_a_link_above_the_log_takes_no_line(self):
        """The ancestor chain, not just the log's own name.

        The append opens ONE path, so every directory above the log is resolved
        on the way to it and a link at any of them redirects the whole write.
        """
        impostor = self.tmp / "impostor"
        impostor.mkdir()
        log = learning._consolidations_log(self.tmp)
        log.parent.parent.mkdir(parents=True, exist_ok=True)
        log.parent.symlink_to(impostor)

        with _anchor_at(self.tmp):
            with self.assertRaises(OSError):
                learning._record_consolidation(1, "alpha", self.tmp)

        self.assertEqual(list(impostor.iterdir()), [])

    @unittest.skipUnless(SYMLINKS_OK, "planting the attack needs symlink creation")
    def test_a_hardlink_at_the_log_name_takes_no_line(self):
        """An ALIAS to another inode passes every name-based test.

        A hardlink is a regular file and is not a symbolic link, so the `lstat`
        of the name accepts it and ``O_NOFOLLOW`` does too -- the append would
        land in the other file's bytes. The link COUNT on the opened descriptor is
        what refuses it, which is also why that check is on the descriptor and not
        on the name.
        """
        victim = self.tmp / "victim.txt"
        victim.write_text("keep me\n")
        log = learning._consolidations_log(self.tmp)
        store.mkdir_refusing_links(log.parent)
        os.link(victim, log)

        with self.assertRaises(OSError):
            learning._record_consolidation(1, "alpha", self.tmp)

        self.assertEqual(victim.read_text(), "keep me\n")

    @unittest.skipUnless(SYMLINKS_OK, "planting the attack needs symlink creation")
    @unittest.skipUnless(store._CAN_PIN_WALK, "opening relative to a pinned parent")
    def test_a_component_swapped_after_the_refusal_takes_no_line(self):
        """The leaf is opened RELATIVE to the pinned parent, not by name again.

        Resolving the full path a second time would undo the refusal: the guard
        passes, the attacker swaps a directory, and the by-name open lands inside
        it. Opening the leaf against the pinned descriptor is what makes the
        swapped component fail its own open instead.
        """
        impostor = self.tmp / "impostor"
        (impostor / "learnings").mkdir(parents=True)
        log = learning._consolidations_log(self.tmp)
        store.mkdir_refusing_links(log.parent)
        mid = log.parent.parent
        state = {"planted": False}

        # Planted after the helper's OWN last refusal, so what refuses the write
        # can only be the pinned open. Planting earlier would be caught by the
        # refusal and would prove nothing about the pin.
        real_leaf_check = store._refuse_unsafe_leaf

        def check_then_plant(target):
            real_leaf_check(target)
            state["planted"] = True
            mid.rename(Path(str(mid) + ".moved"))
            mid.symlink_to(impostor)

        with mock.patch.object(store, "_refuse_unsafe_leaf", check_then_plant):
            with _anchor_at(self.tmp):
                with self.assertRaises(OSError):
                    learning._record_consolidation(1, "alpha", self.tmp)

        self.assertTrue(state["planted"], "the plant never ran, so this proved nothing")
        self.assertEqual(list((impostor / "learnings").iterdir()), [])


if __name__ == "__main__":  # pragma: no cover - direct invocation
    unittest.main()
