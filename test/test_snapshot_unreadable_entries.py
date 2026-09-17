"""A data-home entry the process cannot stat must not abort ``kirocrew snapshot``.

Two independent halves, and only the first one is what a user hits:

* the pre-flight size estimate walked the WHOLE data home before the component
  selection was applied, so a protected file the process cannot ``stat`` -- one
  that no component even declares -- raised out of the generator and the command
  died with a traceback. ``--components`` could not work around it, because the
  walk ran first.
* the staging pass reads the selected trees themselves, so an unreadable entry
  INSIDE one of them ended the snapshot too. There the file is genuinely wanted,
  so it is skipped, warned about, and recorded in ``MANIFEST.json`` rather than
  dropped silently.

The refusal is simulated rather than produced with a mode bit: ``chmod 000`` does
nothing for root, is not honoured the same way on Windows, and the class of file
this reproduces (a platform-protected path) cannot be created by a test at all.
Denying the three syscalls for ONE uniquely-named entry is deterministic on every
platform the suite runs on.
"""

from __future__ import annotations

import errno
import json
import os
import shutil
import tarfile

import pytest
from test_snapshot import _setup_fake_kirocrew, unpinnable_argv

from conftest import make_dir_link
from kiro_crew import pinned_fs
from kiro_crew import snapshot as snap

# Unique enough that the interception below can key on the base name alone, which is
# what makes it work for a bare name opened relative to a directory descriptor as
# well as for a full path.
VICTIM = "protected-entry.md"
#: A DIRECTORY the process may not list. Separate from VICTIM because the two are
#: refused at different syscalls and reach different branches of the walk.
DIR_VICTIM = "protected-dir"
#: A `.db` the process may not read. Named for its SUFFIX: the pass that re-copies
#: databases screens on that before it asks the filesystem anything.
DB_VICTIM = "protected.db"

#: The platform's REAL answer, taken at import before any fixture patches ``os``.
#: ``supports_pinned_tree_walk`` asks whether ``os.listdir``/``os.stat`` are members
#: of ``os.supports_fd``/``os.supports_dir_fd``, and a wrapper installed over either
#: one is not -- so a denial fixture silently flipped the probe to False and sent
#: every test here down the by-name branch, leaving the pinned branch that actually
#: ships on Linux and macOS untested. Each fixture pins this value back.
PINNED_TREE_WALK = pinned_fs.supports_pinned_tree_walk()


def _is_denied(path, name: str, home) -> bool:
    """Whether *path* names the live *name* under *home*, or names it bare.

    A BARE name is how the pinned walks ask -- through a directory descriptor -- so it has
    to be denied for the refusal to be visible to them. An absolute path is denied only
    inside the data home: the staged copy in the bundle has the same base name, and
    refusing that would fake a failure the product never produces (a staged path that is
    not there raises ENOENT) and would refuse reads of the staging tree these tests do not
    target.
    """
    if isinstance(path, int):
        return False
    try:
        text = os.fsdecode(path)
    except (TypeError, ValueError):
        return False
    if os.path.basename(text) != name:
        return False
    if not os.path.dirname(text):
        return True
    return text.startswith(str(home)) or text.startswith(os.path.realpath(home))


@pytest.fixture
def home(tmp_path, monkeypatch):
    d = tmp_path / "home"
    d.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(d))
    monkeypatch.setenv("KIROCREW_ASSUME_GATEWAY_RUNNING", "0")
    _setup_fake_kirocrew(d)
    return d


@pytest.fixture
def deny_victim(home, monkeypatch):
    """Make every access to a file named :data:`VICTIM` fail with ``EPERM``.

    ``stat``, ``lstat`` and ``open`` together are what "the process cannot touch
    this file" means: a walk that only stats is stopped by the first two, and the
    by-name staging fallback reaches the third.
    """
    real = {"stat": os.stat, "lstat": os.lstat, "open": os.open}
    monkeypatch.setattr(pinned_fs, "supports_pinned_tree_walk", lambda: PINNED_TREE_WALK)

    def _deny(key):
        def _fn(path, *args, **kwargs):
            if _is_denied(path, VICTIM, home):
                raise PermissionError(errno.EPERM, "Operation not permitted", str(path))
            return real[key](path, *args, **kwargs)

        return _fn

    for key in real:
        monkeypatch.setattr(os, key, _deny(key))


@pytest.fixture
def deny_open_only(home, monkeypatch):
    """Readable metadata, refused bytes -- mode 000 inside a readable directory.

    Distinct from :func:`deny_victim` on purpose: when the stat is refused too, the
    walk classifies the entry and skips it before it ever tries to copy, so the copy's
    own handler is never reached and a mutation to it survives.
    """
    real = os.open
    monkeypatch.setattr(pinned_fs, "supports_pinned_tree_walk", lambda: PINNED_TREE_WALK)

    def _fn(path, *args, **kwargs):
        if _is_denied(path, VICTIM, home):
            raise PermissionError(errno.EACCES, "Permission denied", str(path))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", _fn)


@pytest.fixture
def unpinned(monkeypatch):
    """Take the by-name traversal a platform without directory descriptors takes."""
    monkeypatch.setattr(snap.pinned_fs, "supports_pinned_tree_walk", lambda: False)


def _deny_listing(monkeypatch, name: str, home) -> None:
    """Refuse ``scandir``/``listdir``/``open`` for the live directory called *name*."""
    real = {"scandir": os.scandir, "listdir": os.listdir, "open": os.open}
    monkeypatch.setattr(pinned_fs, "supports_pinned_tree_walk", lambda: PINNED_TREE_WALK)

    def _deny(key):
        def _fn(path=".", *args, **kwargs):
            if _is_denied(path, name, home):
                raise PermissionError(errno.EPERM, "Operation not permitted", str(path))
            return real[key](path, *args, **kwargs)

        return _fn

    for key in real:
        monkeypatch.setattr(os, key, _deny(key))


@pytest.fixture
def deny_dir(home, monkeypatch):
    """Make :data:`DIR_VICTIM` a directory this process may not list."""
    _deny_listing(monkeypatch, DIR_VICTIM, home)


@pytest.fixture
def deny_victim_db(home, monkeypatch):
    """Refuse every access to the live :data:`DB_VICTIM`."""
    real = {"stat": os.stat, "lstat": os.lstat, "open": os.open}
    monkeypatch.setattr(pinned_fs, "supports_pinned_tree_walk", lambda: PINNED_TREE_WALK)

    def _deny(key):
        def _fn(path, *args, **kwargs):
            if _is_denied(path, DB_VICTIM, home):
                raise PermissionError(errno.EPERM, "Operation not permitted", str(path))
            return real[key](path, *args, **kwargs)

        return _fn

    for key in real:
        monkeypatch.setattr(os, key, _deny(key))


@pytest.fixture
def deny_dir_stat(home, monkeypatch):
    """Make :data:`DIR_VICTIM` a directory whose METADATA cannot be read either.

    The harder case, and the reported one: when the stat itself is refused,
    ``os.path.isdir`` and ``os.path.islink`` both answer False rather than raising, so
    a screen that asks either of them first reads the refusal as an ordinary file.
    """
    _deny_listing(monkeypatch, DIR_VICTIM, home)
    real = {"stat": os.stat, "lstat": os.lstat}

    def _deny(key):
        def _fn(path, *args, **kwargs):
            if _is_denied(path, DIR_VICTIM, home):
                raise PermissionError(errno.EPERM, "Operation not permitted", str(path))
            return real[key](path, *args, **kwargs)

        return _fn

    for key in real:
        monkeypatch.setattr(os, key, _deny(key))


def _manifest_of(out, tmp_path) -> dict:
    tarballs = sorted(out.glob("kirocrew-snapshot-*.tar.gz"))
    assert tarballs, "no bundle was written"
    extract = tmp_path / "extract"
    extract.mkdir()
    with tarfile.open(str(tarballs[0])) as tar:
        tar.extractall(extract, filter=lambda t, _d="": t)
    roots = [d for d in extract.iterdir() if d.is_dir()]
    assert len(roots) == 1
    return json.loads((roots[0] / "MANIFEST.json").read_text(encoding="utf-8"))


class TestAProtectedFileNoComponentWantsIsNeverTouched:
    """The reported crash: the file is not in any component, and still killed the run."""

    def test_snapshot_succeeds(self, home, deny_victim, tmp_path, capsys):
        (home / VICTIM).write_text("a protected file at the data home root\n")
        out = tmp_path / "out"
        rc = snap.snapshot_main([str(out), *unpinnable_argv()])
        assert rc == 0, capsys.readouterr()
        assert sorted(out.glob("kirocrew-snapshot-*.tar.gz")), "no bundle was written"

    def test_a_scoped_snapshot_succeeds(self, home, deny_victim, tmp_path, capsys):
        """``--components config,memory`` failed identically before the fix."""
        (home / VICTIM).write_text("a protected file at the data home root\n")
        out = tmp_path / "out"
        rc = snap.snapshot_main([str(out), "--components", "config,memory", *unpinnable_argv()])
        assert rc == 0, capsys.readouterr()
        assert sorted(out.glob("kirocrew-snapshot-*.tar.gz")), "no bundle was written"

    def test_the_estimate_does_not_report_it_as_skipped(self, home, deny_victim, tmp_path, capsys):
        """It is outside every component, so the estimate never looks at it at all."""
        (home / VICTIM).write_text("a protected file at the data home root\n")
        out = tmp_path / "out"
        assert snap.snapshot_main([str(out), *unpinnable_argv()]) == 0
        assert "unreadable" not in capsys.readouterr().err


class TestABundleWithOmissionsDoesNotPrune:
    """The hazard: `--keep 1` deleting the last complete backup for an incomplete one.

    Retention ranks by mtime alone, so a bundle missing a file it was asked for counts as
    the newest backup exactly like a whole one. Skipping the prune is the conservative
    direction -- it costs disk, where pruning costs the backup.
    """

    def _seed_complete(self, out, tmp_path, stamp="20260101T000000Z"):
        out.mkdir(exist_ok=True)
        existing = out / f"kirocrew-snapshot-{stamp}.tar.gz"
        with tarfile.open(existing, "w:gz") as tf:
            payload = tmp_path / f"payload-{stamp}"
            payload.mkdir()
            tf.add(str(payload), arcname=f"kirocrew-snapshot-{stamp}")
        return existing

    def test_a_prior_complete_archive_survives_keep_1(self, home, deny_victim, tmp_path, capsys):
        existing = self._seed_complete(tmp_path / "out", tmp_path)
        (home / "workspace" / VICTIM).write_text("wanted, but unreadable\n")
        out = tmp_path / "out"
        rc = snap.snapshot_main(
            [str(out), "--components", "workspace", "--keep", "1", *unpinnable_argv()]
        )
        assert rc == 0
        assert existing.is_file(), "an incomplete bundle pruned the last complete backup"
        assert "Not pruning" in capsys.readouterr().out

    def test_a_clean_run_still_prunes(self, home, tmp_path):
        """The guard must not disable retention generally."""
        existing = self._seed_complete(tmp_path / "out", tmp_path)
        out = tmp_path / "out"
        rc = snap.snapshot_main([str(out), "--keep", "1", *unpinnable_argv()])
        assert rc == 0
        assert not existing.exists(), "retention stopped working for an ordinary run"
        assert len(sorted(out.glob("kirocrew-snapshot-*.tar.gz"))) == 1


class TestTheEstimateIsScopedToTheSelectedComponents:
    def test_it_counts_only_what_the_selection_stages(self, home):
        big = b"x" * 4096
        (home / "workspace" / "big.bin").write_bytes(big)
        (home / "crons.json").write_bytes(b"y" * 32)
        crons_only, unreadable = snap._estimate_selected_bytes(home, ["crons"])
        assert crons_only == 32
        assert unreadable == 0
        with_workspace, _ = snap._estimate_selected_bytes(home, ["crons", "workspace"])
        assert with_workspace >= 32 + len(big)

    def test_an_overlapping_tree_is_counted_once(self, home):
        """`memory` names workspace/memory, which lives inside `workspace`."""
        (home / "workspace" / "memory" / "note.md").write_bytes(b"z" * 100)
        both, _ = snap._estimate_selected_bytes(home, ["memory", "workspace"])
        workspace_only, _ = snap._estimate_selected_bytes(home, ["workspace"])
        own_files = sum(
            (home / f).stat().st_size
            for f in snap.COMPONENTS["memory"].files
            if (home / f).is_file()
        )
        assert own_files > 0, "fixture no longer has the memory component's own files"
        assert both == workspace_only + own_files


class TestTheEstimateAbsorbsOnlyThePermissionClass:
    """A number cannot say "the storage is failing"."""

    def test_a_failing_stat_is_raised_not_counted(self, home, monkeypatch):
        real = os.lstat
        target = home / "workspace" / "doc.md"

        def _fn(path, *args, **kwargs):
            if not isinstance(path, int):
                try:
                    hit = os.fspath(path) == str(target)
                except TypeError:
                    hit = False
                if hit:
                    raise OSError(errno.EIO, "Input/output error", str(path))
            return real(path, *args, **kwargs)

        monkeypatch.setattr(os, "lstat", _fn)
        with pytest.raises(OSError) as caught:
            snap._estimate_selected_bytes(home, ["workspace"])
        assert caught.value.errno == errno.EIO

    def test_a_failing_directory_listing_is_raised_not_counted(self, home, monkeypatch):
        real = os.scandir
        target = home / "workspace" / "memory"

        def _fn(path=".", *args, **kwargs):
            if not isinstance(path, int):
                try:
                    hit = os.fspath(path) == str(target)
                except TypeError:
                    hit = False
                if hit:
                    raise OSError(errno.EIO, "Input/output error", str(path))
            return real(path, *args, **kwargs)

        monkeypatch.setattr(os, "scandir", _fn)
        with pytest.raises(OSError) as caught:
            snap._estimate_selected_bytes(home, ["workspace"])
        assert caught.value.errno == errno.EIO


class TestARefusalInAnOverlappingSelectionIsCountedOnce:
    """`memory` names workspace/memory, which sits under `workspace`."""

    def test_one_refused_directory_is_reported_once(self, home, deny_dir, tmp_path):
        (home / "workspace" / "memory" / DIR_VICTIM).mkdir()
        _, unreadable = snap._estimate_selected_bytes(home, ["memory", "workspace"])
        assert unreadable == 1

    def test_the_stderr_line_says_one_not_two(self, home, deny_dir, tmp_path, capsys):
        (home / "workspace" / "memory" / DIR_VICTIM).mkdir()
        out = tmp_path / "out"
        rc = snap.snapshot_main([str(out), "--components", "memory,workspace", *unpinnable_argv()])
        assert rc == 0
        assert "skipped 1 unreadable entry while estimating the size" in capsys.readouterr().err


class TestAnUnreadableEntryInsideASelectedTree:
    def test_the_estimate_counts_it_instead_of_raising(self, home, deny_victim):
        (home / "workspace" / VICTIM).write_text("wanted, but unreadable\n")
        total, unreadable = snap._estimate_selected_bytes(home, ["workspace"])
        assert unreadable >= 1
        assert total >= 0

    def test_it_says_how_many_on_stderr(self, home, deny_victim, tmp_path, capsys):
        (home / "workspace" / VICTIM).write_text("wanted, but unreadable\n")
        out = tmp_path / "out"
        assert snap.snapshot_main([str(out), "--components", "workspace", *unpinnable_argv()]) == 0
        assert "skipped 1 unreadable entry while estimating the size" in capsys.readouterr().err

    def test_a_file_whose_bytes_alone_are_refused_is_recorded(self, home, deny_open_only, tmp_path):
        """Reaches the refusal at the copy rather than at the classification."""
        (home / "workspace" / VICTIM).write_text("readable metadata, refused bytes\n")
        out = tmp_path / "out"
        assert snap.snapshot_main([str(out), "--components", "workspace", *unpinnable_argv()]) == 0
        manifest = _manifest_of(out, tmp_path)
        omitted = [s for s in manifest.get("skipped", []) if s["path"].endswith(VICTIM)]
        assert omitted, f"manifest does not record the omission: {manifest.get('skipped')}"
        assert omitted[0]["reason"] == "unreadable_entry"

    def test_the_snapshot_still_completes_and_says_how_many(self, home, deny_victim, tmp_path):
        (home / "workspace" / VICTIM).write_text("wanted, but unreadable\n")
        out = tmp_path / "out"
        rc = snap.snapshot_main([str(out), "--components", "workspace", *unpinnable_argv()])
        assert rc == 0
        assert sorted(out.glob("kirocrew-snapshot-*.tar.gz")), "no bundle was written"

    def test_the_bundle_records_the_omission(self, home, deny_victim, tmp_path):
        """A backup that could not read a file must SAY so, not omit it silently."""
        (home / "workspace" / VICTIM).write_text("wanted, but unreadable\n")
        out = tmp_path / "out"
        assert snap.snapshot_main([str(out), "--components", "workspace", *unpinnable_argv()]) == 0
        manifest = _manifest_of(out, tmp_path)
        omitted = [s for s in manifest.get("skipped", []) if s["path"].endswith(VICTIM)]
        assert omitted, f"manifest does not record the omission: {manifest.get('skipped')}"
        assert omitted[0]["reason"] == "unreadable_entry"


class TestAFileWhoseBytesAloneAreRefusedOnTheByNameWalk:
    """The by-name copy has its own tolerance, and only this reaches it.

    Every other by-name case here denies the stat as well, so the ignore screen
    classifies the entry and drops it before any copy is attempted.
    """

    def test_it_is_skipped_and_recorded(self, home, deny_open_only, unpinned, tmp_path):
        (home / "workspace" / VICTIM).write_text("readable metadata, refused bytes\n")
        out = tmp_path / "out"
        rc = snap.snapshot_main([str(out), "--components", "workspace", "--allow-unpinned-staging"])
        assert rc == 0
        manifest = _manifest_of(out, tmp_path)
        omitted = [s for s in manifest.get("skipped", []) if s["path"].endswith(VICTIM)]
        assert omitted, f"manifest does not record the omission: {manifest.get('skipped')}"
        assert omitted[0]["reason"] == "unreadable_entry"


class TestARefusedDIRECTORYIsTheSameOmission:
    """A tolerance that covered only files still ended the snapshot on a directory."""

    def test_a_refused_subdirectory_is_skipped_and_recorded(self, home, deny_dir, tmp_path):
        (home / "workspace" / DIR_VICTIM).mkdir()
        (home / "workspace" / DIR_VICTIM / "inside.md").write_text("unreachable\n")
        out = tmp_path / "out"
        assert snap.snapshot_main([str(out), "--components", "workspace", *unpinnable_argv()]) == 0
        manifest = _manifest_of(out, tmp_path)
        omitted = [s for s in manifest.get("skipped", []) if s["path"].endswith(DIR_VICTIM)]
        assert omitted, f"manifest does not record the omission: {manifest.get('skipped')}"
        assert omitted[0]["reason"] == "unreadable_entry"

    def test_a_refused_subdirectory_is_skipped_on_the_by_name_walk(
        self, home, deny_dir, unpinned, tmp_path
    ):
        (home / "workspace" / DIR_VICTIM).mkdir()
        (home / "workspace" / DIR_VICTIM / "inside.md").write_text("unreachable\n")
        out = tmp_path / "out"
        rc = snap.snapshot_main([str(out), "--components", "workspace", "--allow-unpinned-staging"])
        assert rc == 0
        manifest = _manifest_of(out, tmp_path)
        omitted = [s for s in manifest.get("skipped", []) if s["path"].endswith(DIR_VICTIM)]
        assert omitted, f"manifest does not record the omission: {manifest.get('skipped')}"
        assert omitted[0]["reason"] == "unreadable_entry"

    def test_a_refused_component_ROOT_is_skipped_and_recorded(self, home, monkeypatch, tmp_path):
        """The root is listed before any per-entry screen runs, on both traversals."""
        _deny_listing(monkeypatch, "plan_memory", home)
        out = tmp_path / "out"
        assert snap.snapshot_main([str(out), "--components", "workspace", *unpinnable_argv()]) == 0
        manifest = _manifest_of(out, tmp_path)
        omitted = [s for s in manifest.get("skipped", []) if s["path"].endswith("plan_memory")]
        assert omitted, f"manifest does not record the omission: {manifest.get('skipped')}"
        assert omitted[0]["reason"] == "unreadable_entry"


class TestTheByNameRootsOwnRefusal:
    """`copytree` lists the tree's own directory before any per-entry screen runs."""

    def test_it_is_reported_and_the_tree_omitted(self, home, tmp_path, monkeypatch):
        _deny_listing(monkeypatch, "plan_memory", home)
        monkeypatch.setattr(snap.pinned_fs, "supports_pinned_tree_walk", lambda: False)
        seen: list[tuple[str, str]] = []
        snap._copytree_safe(
            home / "plan_memory",
            tmp_path / "dst",
            allow_unpinned=True,
            on_skip=lambda reason, path: seen.append((reason, path)),
            skip_unreadable=True,
        )
        assert [p for r, p in seen if r == "unreadable_entry" and p.endswith("plan_memory")], seen

    def test_it_still_raises_when_the_tolerance_is_off(self, home, tmp_path, monkeypatch):
        _deny_listing(monkeypatch, "plan_memory", home)
        monkeypatch.setattr(snap.pinned_fs, "supports_pinned_tree_walk", lambda: False)
        with pytest.raises((PermissionError, shutil.Error)):
            snap._copytree_safe(home / "plan_memory", tmp_path / "dst", allow_unpinned=True)


class TestADirectoryWhoseMetadataIsRefusedToo:
    """`os.path.isdir` answers False for a path it cannot stat, so it cannot screen."""

    def test_the_pinned_walk_skips_and_records_it(self, home, deny_dir_stat, tmp_path):
        (home / "workspace" / DIR_VICTIM).mkdir()
        (home / "workspace" / DIR_VICTIM / "inside.md").write_text("unreachable\n")
        out = tmp_path / "out"
        assert snap.snapshot_main([str(out), "--components", "workspace", *unpinnable_argv()]) == 0
        manifest = _manifest_of(out, tmp_path)
        omitted = [s for s in manifest.get("skipped", []) if s["path"].endswith(DIR_VICTIM)]
        assert omitted, f"manifest does not record the omission: {manifest.get('skipped')}"
        assert omitted[0]["reason"] == "unreadable_entry"

    def test_the_by_name_walk_skips_and_records_it(self, home, deny_dir_stat, unpinned, tmp_path):
        (home / "workspace" / DIR_VICTIM).mkdir()
        (home / "workspace" / DIR_VICTIM / "inside.md").write_text("unreachable\n")
        out = tmp_path / "out"
        rc = snap.snapshot_main([str(out), "--components", "workspace", "--allow-unpinned-staging"])
        assert rc == 0
        manifest = _manifest_of(out, tmp_path)
        omitted = [s for s in manifest.get("skipped", []) if s["path"].endswith(DIR_VICTIM)]
        assert omitted, f"manifest does not record the omission: {manifest.get('skipped')}"
        assert omitted[0]["reason"] == "unreadable_entry"


class TestARefusedDatabaseNeverReachesTheRestagePass:
    """The destination is asked about first, and that is what keeps this safe.

    A statically unreadable `.db` is skipped by the tree walk, so no byte copy exists at
    the destination and the restage pass ends the iteration before it stats the source.
    Asking the SOURCE first -- which is what the pre-change `src.is_file()` did -- raises
    the refusal out of the loop and ends the whole snapshot.
    """

    def test_the_snapshot_completes_and_records_it(self, home, deny_victim_db, tmp_path):
        (home / "workspace" / DB_VICTIM).write_bytes(b"SQLite format 3\x00stub")
        out = tmp_path / "out"
        assert snap.snapshot_main([str(out), "--components", "workspace", *unpinnable_argv()]) == 0
        manifest = _manifest_of(out, tmp_path)
        omitted = [s for s in manifest.get("skipped", []) if s["path"].endswith(DB_VICTIM)]
        assert omitted, f"manifest does not record the omission: {manifest.get('skipped')}"
        assert omitted[0]["reason"] == "unreadable_entry"


class TestTheRestagePassFailsClosedOnAnUnreadableDestination(object):
    """An `EIO` reading the staged copy is not "there is nothing staged"."""

    def test_it_propagates_rather_than_keeping_the_byte_copy(self, home, tmp_path, monkeypatch):
        (home / "workspace" / "wal.db").write_bytes(b"SQLite format 3\x00stub")
        staged_name = "wal.db"
        real = os.stat
        fired = {"n": 0}

        def _fn(path, *args, **kwargs):
            if not isinstance(path, int) and fired["n"] == 0:
                try:
                    text = os.fsdecode(path)
                except (TypeError, ValueError):
                    text = ""
                # The STAGED copy only: under the bundle, not under the data home. Fired
                # ONCE, so a swallowed error lets the run finish rather than resurfacing at
                # the next pass that stats the same file -- which is what makes this able
                # to tell the narrow catch from the broad one.
                staged = (
                    os.path.basename(text) == staged_name
                    # A real directory component: the walks ask through a descriptor with
                    # a BARE name, and matching that fired on the live tree's own
                    # classification instead of on the staged copy.
                    and os.path.dirname(text)
                    and not text.startswith(str(home))
                    and not text.startswith(os.path.realpath(home))
                )
                if staged:
                    fired["n"] += 1
                    raise OSError(errno.EIO, "Input/output error", text)
            return real(path, *args, **kwargs)

        monkeypatch.setattr(os, "stat", _fn)
        monkeypatch.setattr(pinned_fs, "supports_pinned_tree_walk", lambda: PINNED_TREE_WALK)
        out = tmp_path / "out"
        with pytest.raises(OSError) as caught:
            snap.snapshot_main([str(out), "--components", "workspace", *unpinnable_argv()])
        assert caught.value.errno == errno.EIO


class TestTheEstimateDoesNotFollowASymlinkedTreeRoot:
    """Staging refuses such a root outright, but only after the estimate has run."""

    def test_its_bytes_are_not_counted(self, home, tmp_path):
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        (outside / "big.bin").write_bytes(b"x" * 8192)
        shutil.rmtree(home / "plan_memory")
        # A junction on Windows, a symlink elsewhere. Both are reparse points and both
        # are followed by the same machinery, so the screen under test stays exercised
        # on a host that cannot create a symbolic link -- where a `pytest.skip` would
        # have dropped this coverage silently.
        make_dir_link(home / "plan_memory", outside)
        total, _ = snap._estimate_selected_bytes(home, ["workspace"])
        assert total < 8192, "the estimate followed a symlinked component root"

    def test_a_nested_link_is_not_followed_either(self, home, tmp_path):
        """A junction INSIDE a selected tree.

        ``os.walk`` declines a symlink directory by itself, so on POSIX this holds with
        no code at all; a Windows junction is not a symlink to it and is descended. The
        same helper builds a junction there and a symlink here, so this assertion is
        the one that fails on Windows without the prune and passes everywhere with it.
        """
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        (outside / "big.bin").write_bytes(b"x" * 8192)
        make_dir_link(home / "workspace" / "a-link", outside)
        total, _ = snap._estimate_selected_bytes(home, ["workspace"])
        assert total < 8192, "the estimate followed a link nested inside a selected tree"

    def test_the_prune_runs_on_every_platform(self, home, tmp_path, monkeypatch):
        """Pin the mechanism, not only the outcome, so POSIX cannot pass by accident.

        `is_reparse_point` is what decides; the nested link must be ASKED about. On a
        platform whose ``os.walk`` already declines the link this is the only assertion
        that turns red when the prune is removed.
        """
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        link = home / "workspace" / "a-link"
        make_dir_link(link, outside)
        asked: list[str] = []
        real = pinned_fs.is_reparse_point

        def _fn(path):
            asked.append(os.fsdecode(path))
            return real(path)

        monkeypatch.setattr(snap.pinned_fs, "is_reparse_point", _fn)
        snap._estimate_selected_bytes(home, ["workspace"])
        assert str(link) in asked, asked


class TestADestinationRootThatCannotBeCreated:
    """Reported as an unreadable SOURCE, a bundle shipped without the whole tree."""

    def test_it_propagates_instead_of_being_recorded(self, home, tmp_path, monkeypatch):
        monkeypatch.setattr(snap.pinned_fs, "supports_pinned_tree_walk", lambda: False)
        dst = tmp_path / "dst-root"
        real = os.makedirs

        def _fn(path, *args, **kwargs):
            if str(path).startswith(str(dst)):
                raise PermissionError(errno.EACCES, "Permission denied", str(path))
            return real(path, *args, **kwargs)

        monkeypatch.setattr(os, "makedirs", _fn)
        seen: list[tuple[str, str]] = []
        with pytest.raises(PermissionError):
            snap._copytree_safe(
                home / "workspace",
                dst,
                allow_unpinned=True,
                on_skip=lambda reason, path: seen.append((reason, path)),
                skip_unreadable=True,
            )
        assert not [r for r, _ in seen if r == "unreadable_entry"], seen


class TestOnlyTheSOURCEReadIsTolerated:
    """A refusal to WRITE is not the operator's file being unreadable.

    Recording it as one omits data from the bundle, blames the source for it, and
    reports success -- so retention can then prune a bundle that was complete.
    """

    def _deny_open_of(self, monkeypatch, target):
        real = os.open

        def _fn(path, *args, **kwargs):
            if not isinstance(path, int):
                try:
                    hit = os.fspath(path) == str(target)
                except TypeError:
                    hit = False
                if hit:
                    raise PermissionError(errno.EACCES, "Permission denied", str(path))
            return real(path, *args, **kwargs)

        monkeypatch.setattr(os, "open", _fn)

    def test_a_destination_refusal_propagates(self, tmp_path, monkeypatch):
        src = tmp_path / "src.txt"
        src.write_text("payload\n")
        dst = tmp_path / "out" / "dst.txt"
        dst.parent.mkdir()
        self._deny_open_of(monkeypatch, dst)
        seen: list[tuple[str, str]] = []
        with pytest.raises(PermissionError):
            pinned_fs.copy_file_pinned(
                str(src),
                str(dst),
                skip_unreadable=True,
                on_skip=lambda reason, path: seen.append((reason, path)),
            )
        assert not [r for r, _ in seen if r == "unreadable_entry"], seen

    def test_a_source_refusal_is_recorded_and_skipped(self, tmp_path, monkeypatch):
        src = tmp_path / "src.txt"
        src.write_text("payload\n")
        dst = tmp_path / "out" / "dst.txt"
        dst.parent.mkdir()
        self._deny_open_of(monkeypatch, src)
        seen: list[tuple[str, str]] = []
        copied = pinned_fs.copy_file_pinned(
            str(src),
            str(dst),
            skip_unreadable=True,
            on_skip=lambda reason, path: seen.append((reason, path)),
        )
        assert copied is False
        assert [r for r, _ in seen if r == "unreadable_entry"], seen
        assert not dst.exists()

    def test_a_source_refusal_still_propagates_by_default(self, tmp_path, monkeypatch):
        src = tmp_path / "src.txt"
        src.write_text("payload\n")
        dst = tmp_path / "out" / "dst.txt"
        dst.parent.mkdir()
        self._deny_open_of(monkeypatch, src)
        with pytest.raises(PermissionError):
            pinned_fs.copy_file_pinned(str(src), str(dst))


class TestTheToleranceIsOffUnlessAskedFor:
    """Restore and merge must keep ending on a refusal, so the OFF side needs pinning.

    Without these, deleting every ``if not skip_unreadable: raise`` guard reds no test
    and restore silently becomes skip-and-drop -- the archive's own content, dropped
    from what the operator asked to have put back.
    """

    @pytest.mark.skipif(not PINNED_TREE_WALK, reason="no directory descriptors on this platform")
    def test_the_pinned_walk_raises_by_default(self, home, deny_victim, tmp_path):
        (home / "workspace" / VICTIM).write_text("wanted, but unreadable\n")
        with pytest.raises(PermissionError):
            pinned_fs.stage_tree_pinned(
                home / "workspace", tmp_path / "dst", what="tree 'workspace'"
            )

    def test_the_by_name_walk_raises_by_default(self, home, deny_victim, unpinned, tmp_path):
        (home / "workspace" / VICTIM).write_text("wanted, but unreadable\n")
        with pytest.raises((PermissionError, shutil.Error)):
            snap._copytree_safe(home / "workspace", tmp_path / "dst", allow_unpinned=True)

    def test_a_refused_directory_still_ends_a_default_walk(self, home, deny_dir, tmp_path):
        (home / "workspace" / DIR_VICTIM).mkdir()
        with pytest.raises((PermissionError, shutil.Error)):
            snap._copytree_safe(
                home / "workspace", tmp_path / "dst", allow_unpinned=True, skip_unreadable=False
            )

    def test_nothing_is_recorded_when_the_tolerance_is_off(self, home, deny_victim, tmp_path):
        (home / "workspace" / VICTIM).write_text("wanted, but unreadable\n")
        seen: list[tuple[str, str]] = []
        with pytest.raises((PermissionError, shutil.Error)):
            snap._copytree_safe(
                home / "workspace",
                tmp_path / "dst",
                allow_unpinned=True,
                on_skip=lambda reason, path: seen.append((reason, path)),
            )
        assert not [r for r, _ in seen if r == "unreadable_entry"]


class TestTheByNameStagingFallbackToleratesItToo:
    """The branch a platform without directory descriptors takes.

    Forced rather than skipped-unless-Windows: the branch ships on a real platform, and
    its screens reach the refusal at a different syscall -- ``os.path.islink`` answers
    False for a path it cannot ``lstat`` instead of raising, so the entry arrives as a
    copy that fails rather than as a classification that does.
    """

    def test_it_completes_and_records_the_omission(self, home, deny_victim, unpinned, tmp_path):
        (home / "workspace" / VICTIM).write_text("wanted, but unreadable\n")
        out = tmp_path / "out"
        rc = snap.snapshot_main([str(out), "--components", "workspace", "--allow-unpinned-staging"])
        assert rc == 0
        manifest = _manifest_of(out, tmp_path)
        omitted = [s for s in manifest.get("skipped", []) if s["path"].endswith(VICTIM)]
        assert omitted, f"manifest does not record the omission: {manifest.get('skipped')}"
        assert omitted[0]["reason"] == "unreadable_entry"


class TestATreeROOTThatCannotBeStatted:
    """The loop's own root probe, which the per-entry screens never see.

    ``Path.is_dir()`` answers False for a path it cannot stat, so a tree root the
    process may not reach took the SAME branch as a root that is simply absent: the
    whole selected tree left the bundle, nothing entered ``skipped``, the manifest
    still declared the component present, and ``--keep`` pruned the last complete
    archive. Both halves are pinned here, because a fix that records every skip would
    pass the first test and break retention for every fresh data home.
    """

    def _deny_stat_of(self, monkeypatch, target):
        """Refuse ``stat``/``lstat`` for exactly *target*, by path, and nothing else.

        Matched on the resolved path rather than on the base name: the staged copy in
        the bundle carries the same base name, and refusing that would fake a failure
        the product does not produce.
        """
        real = {"stat": os.stat, "lstat": os.lstat}
        monkeypatch.setattr(pinned_fs, "supports_pinned_tree_walk", lambda: PINNED_TREE_WALK)
        wanted = {str(target), os.path.realpath(target)}

        def _deny(key):
            def _fn(path, *args, **kwargs):
                if not isinstance(path, int) and os.fsdecode(path) in wanted:
                    raise PermissionError(errno.EPERM, "Operation not permitted", str(path))
                return real[key](path, *args, **kwargs)

            return _fn

        for key in real:
            monkeypatch.setattr(os, key, _deny(key))

    def test_the_manifest_records_the_whole_tree_as_unreadable(
        self, home, monkeypatch, tmp_path, capsys
    ):
        self._deny_stat_of(monkeypatch, home / "plan_memory")
        out = tmp_path / "out"
        rc = snap.snapshot_main([str(out), "--components", "workspace", *unpinnable_argv()])
        assert rc == 0, capsys.readouterr()
        manifest = _manifest_of(out, tmp_path)
        omitted = [
            s
            for s in manifest.get("skipped", [])
            if s["path"].endswith("plan_memory") and s["reason"] == "unreadable_entry"
        ]
        assert omitted, f"the refused tree root is not recorded: {manifest.get('skipped')}"

    def test_a_prior_complete_archive_survives_keep_1(self, home, monkeypatch, tmp_path, capsys):
        out = tmp_path / "out"
        out.mkdir()
        existing = out / "kirocrew-snapshot-20260101T000000Z.tar.gz"
        with tarfile.open(existing, "w:gz") as tf:
            payload = tmp_path / "payload"
            payload.mkdir()
            tf.add(str(payload), arcname="kirocrew-snapshot-20260101T000000Z")
        self._deny_stat_of(monkeypatch, home / "plan_memory")
        rc = snap.snapshot_main(
            [str(out), "--components", "workspace", "--keep", "1", *unpinnable_argv()]
        )
        assert rc == 0
        assert existing.is_file(), "a refused tree root pruned the last complete backup"
        assert "Not pruning" in capsys.readouterr().out

    def test_an_IO_error_on_the_root_is_not_absorbed(self, home, monkeypatch, tmp_path):
        """`EIO` is a failing disk, not a missing tree, and must not be skipped quietly.

        Absorbing it omitted the whole selected tree with nothing in `skipped`, so the
        manifest still declared the component present and `--keep` pruned the last
        complete archive. A backup target is exactly where this errno happens.
        """
        real = os.stat
        target = {str(home / "plan_memory"), os.path.realpath(home / "plan_memory")}

        def _fn(path, *args, **kwargs):
            if not isinstance(path, int) and os.fsdecode(path) in target:
                raise OSError(errno.EIO, "Input/output error", str(path))
            return real(path, *args, **kwargs)

        monkeypatch.setattr(pinned_fs, "supports_pinned_tree_walk", lambda: PINNED_TREE_WALK)
        monkeypatch.setattr(os, "stat", _fn)
        out = tmp_path / "out"
        with pytest.raises(OSError) as caught:
            snap.snapshot_main([str(out), "--components", "workspace", *unpinnable_argv()])
        assert caught.value.errno == errno.EIO

    def test_a_tree_that_is_merely_ABSENT_is_not_an_omission(self, home, tmp_path, capsys):
        """The discriminating half: absent must stay a silent skip, and still prune.

        Recording every skip would satisfy the two tests above and turn every fresh
        data home into a bundle that refuses to prune forever.
        """
        shutil.rmtree(home / "plan_memory")
        out = tmp_path / "out"
        out.mkdir()
        existing = out / "kirocrew-snapshot-20260101T000000Z.tar.gz"
        with tarfile.open(existing, "w:gz") as tf:
            payload = tmp_path / "payload"
            payload.mkdir()
            tf.add(str(payload), arcname="kirocrew-snapshot-20260101T000000Z")
        rc = snap.snapshot_main(
            [str(out), "--components", "workspace", "--keep", "1", *unpinnable_argv()]
        )
        assert rc == 0, capsys.readouterr()
        manifest = _manifest_of(out, tmp_path)
        assert not [
            s for s in manifest.get("skipped", []) if s["path"].endswith("plan_memory")
        ], "an absent tree was recorded as an omission"
        assert not existing.exists(), "retention stopped working for an ordinary run"


class TestEveryOmissionReasonIsClassified:
    """Retention must ask a CLASS, not name a reason code.

    A guard that names ``unreadable_entry`` lets `too_large`, `vanished` and
    `identity_changed` walk past it into a prune that deletes the last complete
    backup. A guard that enumerates codes is stale the moment the next code is added
    and goes on answering the old way silently, so the split lives next to the codes
    and this class pins it there.
    """

    def test_a_screen_is_not_an_omission(self):
        """Screened on every run by design, so a bundle that screened one is whole."""
        assert not pinned_fs.omits_wanted_data(pinned_fs.SKIP_SYMLINK)
        assert not pinned_fs.omits_wanted_data(pinned_fs.SKIP_NOT_REGULAR)

    def test_wanted_data_missing_is_an_omission(self):
        for reason in (
            pinned_fs.SKIP_UNREADABLE_ENTRY,
            pinned_fs.SKIP_TOO_LARGE,
            pinned_fs.SKIP_VANISHED,
            pinned_fs.SKIP_IDENTITY_CHANGED,
        ):
            assert pinned_fs.omits_wanted_data(reason), reason

    def test_a_reason_nobody_classified_counts_as_an_omission(self):
        """The asymmetry is the point: a surplus bundle costs disk, a prune costs the backup."""
        assert pinned_fs.omits_wanted_data("some_reason_added_next_year")

    def test_the_split_covers_every_reason_code_in_the_module(self):
        """A code added later cannot skip being classified without turning this red."""
        declared = {
            value
            for name, value in vars(pinned_fs).items()
            if name.startswith("SKIP_") and isinstance(value, str)
        }
        classified = pinned_fs._NEVER_ARCHIVED | pinned_fs._OMITS_WANTED_DATA
        assert declared == classified, (
            "a reason code is in neither _NEVER_ARCHIVED nor _OMITS_WANTED_DATA: "
            f"{sorted(declared ^ classified)}"
        )

    def test_the_two_halves_do_not_overlap(self):
        assert not (pinned_fs._NEVER_ARCHIVED & pinned_fs._OMITS_WANTED_DATA)


class TestRetentionActsOnTheClass:
    """The guard's behaviour for each class, driven through the real command.

    The reason vocabulary is injected at the seam the guard reads -- the list
    ``_build_snapshot`` fills -- rather than by producing each errno in the
    filesystem. Two reasons are why: ``too_large`` is not reachable from snapshot at
    all today (no ``max_bytes`` is passed), and an unclassified reason cannot be
    produced by definition, so a filesystem-only test could never cover the two cases
    that matter most. The traversal's own reason-for-errno mapping is covered by the
    real ``unreadable_entry`` cases above.
    """

    def _inject(self, monkeypatch, reason):
        real = snap._build_snapshot

        def _fn(*args, skipped_out=None, **kwargs):
            out = real(*args, skipped_out=skipped_out, **kwargs)
            if skipped_out is not None:
                skipped_out.append({"reason": reason, "path": "workspace/an-entry"})
            return out

        monkeypatch.setattr(snap, "_build_snapshot", _fn)

    def _seed_complete(self, out, tmp_path):
        out.mkdir(exist_ok=True)
        existing = out / "kirocrew-snapshot-20260101T000000Z.tar.gz"
        with tarfile.open(existing, "w:gz") as tf:
            payload = tmp_path / "payload"
            payload.mkdir(exist_ok=True)
            tf.add(str(payload), arcname="kirocrew-snapshot-20260101T000000Z")
        return existing

    @pytest.mark.parametrize(
        "reason",
        ["unreadable_entry", "too_large", "vanished", "identity_changed", "not_yet_classified"],
    )
    def test_an_omission_keeps_the_older_complete_bundle(
        self, home, monkeypatch, tmp_path, capsys, reason
    ):
        out = tmp_path / "out"
        existing = self._seed_complete(out, tmp_path)
        self._inject(monkeypatch, reason)
        assert snap.snapshot_main([str(out), "--keep", "1", *unpinnable_argv()]) == 0
        assert existing.is_file(), f"{reason} pruned the last complete backup"
        printed = capsys.readouterr().out
        assert "Not pruning" in printed
        # The message names WHICH reason, so an operator does not have to open the
        # manifest to find out why their bundle count is growing.
        assert reason in printed

    @pytest.mark.parametrize("reason", ["symlink", "not_regular"])
    def test_a_screened_entry_still_prunes(self, home, monkeypatch, tmp_path, capsys, reason):
        """Otherwise retention turns off permanently for any data home holding a symlink."""
        out = tmp_path / "out"
        existing = self._seed_complete(out, tmp_path)
        self._inject(monkeypatch, reason)
        assert snap.snapshot_main([str(out), "--keep", "1", *unpinnable_argv()]) == 0
        assert not existing.exists(), f"{reason} stopped retention for an ordinary run"
        assert "Not pruning" not in capsys.readouterr().out

    def test_a_screen_beside_a_real_omission_still_holds_the_prune(
        self, home, monkeypatch, tmp_path, capsys
    ):
        """The screen must not dilute the omission when a run records both."""
        real = snap._build_snapshot

        def _fn(*args, skipped_out=None, **kwargs):
            built = real(*args, skipped_out=skipped_out, **kwargs)
            if skipped_out is not None:
                skipped_out.append({"reason": "symlink", "path": "workspace/a-link"})
                skipped_out.append({"reason": "vanished", "path": "workspace/gone"})
            return built

        monkeypatch.setattr(snap, "_build_snapshot", _fn)
        out = tmp_path / "out"
        existing = self._seed_complete(out, tmp_path)
        assert snap.snapshot_main([str(out), "--keep", "1", *unpinnable_argv()]) == 0
        assert existing.is_file()
        printed = capsys.readouterr().out
        assert "omits 1 entry" in printed, printed
        assert "symlink" not in printed


class TestAPostStatSwapIsAnOmissionNotAScreen:
    """`symlink` and `not_regular` are FIRST-LOOK codes; a swap after the look is not one.

    The walk stats an entry, judges it regular, and hands it to the copy. If a link or a
    FIFO sits at that name by the time the copy opens it, the file the bundle was asked
    to carry is gone and something else is there -- the same-UID swap this module names
    as its threat. Reporting that under a screen code put it on the "complete" side of
    the retention split, so the bundle that omitted a wanted file pruned the last whole
    one. Every site that can see the swap reports `identity_changed` instead.
    """

    def _swap_open_to(self, monkeypatch, victim_name: str, *, errno_code=None, decoy=None):
        """Make the pinned open of *victim_name* fail with *errno_code*, or open *decoy*.

        Only the open THROUGH a directory descriptor is redirected -- that is the one the
        walk performs after its own stat, so the stat sees the real regular file and the
        open sees the swap. A by-name open (no ``dir_fd``) is left alone.
        """
        real = os.open
        # A wrapper over `os.open` is not a member of `os.supports_dir_fd`, so without
        # this the capability probe reads False and the walk under test is by-name.
        monkeypatch.setattr(pinned_fs, "supports_pinned_tree_walk", lambda: PINNED_TREE_WALK)

        def _fn(path, flags, *args, dir_fd=None, **kwargs):
            if (
                dir_fd is not None
                and not isinstance(path, int)
                and os.fsdecode(path) == victim_name
            ):
                if errno_code is not None:
                    raise OSError(errno_code, os.strerror(errno_code), str(path))
                return real(decoy, flags, *args, dir_fd=dir_fd, **kwargs)
            return real(path, flags, *args, dir_fd=dir_fd, **kwargs)

        monkeypatch.setattr(os, "open", _fn)

    @pytest.fixture
    def pinned_only(self):
        if not PINNED_TREE_WALK:
            pytest.skip("the swap is observable only on the descriptor-pinned walk")

    def test_a_file_that_becomes_a_symlink_is_identity_changed(
        self, home, pinned_only, monkeypatch, tmp_path
    ):
        (home / "workspace" / VICTIM).write_text("wanted\n")
        self._swap_open_to(monkeypatch, VICTIM, errno_code=errno.ELOOP)
        seen: list[tuple[str, str]] = []
        snap._copytree_safe(
            home / "workspace", tmp_path / "dst", on_skip=lambda r, p: seen.append((r, p))
        )
        reasons = {r for r, p in seen if p.endswith(VICTIM)}
        assert reasons == {"identity_changed"}, seen

    def test_a_file_that_becomes_another_inode_is_identity_changed(
        self, home, pinned_only, monkeypatch, tmp_path
    ):
        (home / "workspace" / VICTIM).write_text("wanted\n")
        (home / "workspace" / "decoy.txt").write_text("planted\n")
        self._swap_open_to(monkeypatch, VICTIM, decoy="decoy.txt")
        seen: list[tuple[str, str]] = []
        snap._copytree_safe(
            home / "workspace", tmp_path / "dst", on_skip=lambda r, p: seen.append((r, p))
        )
        reasons = {r for r, p in seen if p.endswith(VICTIM)}
        assert reasons == {"identity_changed"}, seen
        assert not (tmp_path / "dst" / VICTIM).exists(), "the decoy's bytes were copied"

    def test_a_file_that_becomes_a_fifo_is_identity_changed_not_not_regular(
        self, home, pinned_only, monkeypatch, tmp_path
    ):
        """The discriminating case: the swapped-in inode is ALSO non-regular.

        A copy that asks the type before the identity reports this as `not_regular` --
        a screen code -- and the omission prunes. Identity has to be asked first.
        """
        if not hasattr(os, "mkfifo"):
            pytest.skip("mkfifo is unavailable on this host")
        (home / "workspace" / VICTIM).write_text("wanted\n")
        os.mkfifo(home / "workspace" / "decoy-pipe")
        self._swap_open_to(monkeypatch, VICTIM, decoy="decoy-pipe")
        seen: list[tuple[str, str]] = []
        snap._copytree_safe(
            home / "workspace", tmp_path / "dst", on_skip=lambda r, p: seen.append((r, p))
        )
        reasons = {r for r, p in seen if p.endswith(VICTIM)}
        assert reasons == {"identity_changed"}, seen

    def test_a_directory_that_becomes_a_symlink_is_identity_changed(
        self, home, pinned_only, monkeypatch, tmp_path
    ):
        (home / "workspace" / DIR_VICTIM).mkdir()
        (home / "workspace" / DIR_VICTIM / "inner.txt").write_text("wanted\n")
        self._swap_open_to(monkeypatch, DIR_VICTIM, errno_code=errno.ELOOP)
        seen: list[tuple[str, str]] = []
        snap._copytree_safe(
            home / "workspace", tmp_path / "dst", on_skip=lambda r, p: seen.append((r, p))
        )
        reasons = {r for r, p in seen if p.endswith(DIR_VICTIM)}
        assert reasons == {"identity_changed"}, seen

    def test_a_link_on_first_look_is_still_the_screen(self, home, tmp_path):
        """No prior stat, so a link here is the ordinary screen and prunes normally.

        The screen under test is the open's own ``O_NOFOLLOW`` refusing a link. A
        platform without that flag opens THROUGH the link, which is the documented
        weakness of its by-name mode and is screened earlier by the caller's
        ``is_reparse_point`` -- so the expectation is derived from the flag the code
        consults rather than asserted the same way everywhere.
        """
        if not hasattr(os, "O_NOFOLLOW"):
            pytest.skip("the first-look screen is O_NOFOLLOW, which this platform lacks")
        target = tmp_path / "elsewhere.txt"
        target.write_text("outside\n")
        link = home / "workspace" / "a-link"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            pytest.skip("this platform cannot create a file symlink")
        seen: list[tuple[str, str]] = []
        copied = pinned_fs.copy_file_pinned(
            str(link), str(tmp_path / "dst-file"), on_skip=lambda r, p: seen.append((r, p))
        )
        assert copied is False
        assert seen and seen[0][0] == "symlink"

    def test_the_swap_holds_the_prune(self, home, pinned_only, monkeypatch, tmp_path, capsys):
        """End to end: a swapped entry is an omission, so the older whole bundle survives."""
        out = tmp_path / "out"
        out.mkdir()
        existing = out / "kirocrew-snapshot-20260101T000000Z.tar.gz"
        with tarfile.open(existing, "w:gz") as tf:
            payload = tmp_path / "payload"
            payload.mkdir()
            tf.add(str(payload), arcname="kirocrew-snapshot-20260101T000000Z")
        (home / "workspace" / VICTIM).write_text("wanted\n")
        self._swap_open_to(monkeypatch, VICTIM, errno_code=errno.ELOOP)
        rc = snap.snapshot_main([str(out), "--components", "workspace", "--keep", "1"])
        assert rc == 0
        assert existing.is_file(), "a swapped entry was read as a screen and pruned the last backup"
        assert "identity_changed" in capsys.readouterr().out


class TestADirectoryWhoseListingAloneIsRefused:
    """The open succeeds and the LISTING is refused -- the one seam left uncovered.

    No static POSIX mode produces this on a local filesystem: an ``O_RDONLY`` open
    already requires read. A filesystem that re-validates each call can, when the
    grant changes between the open and the read. The refusal is injected at exactly
    that call so the test does not depend on a filesystem that behaves that way.
    """

    def _deny_listing_of_fd_for(self, monkeypatch, victim_dir):
        """Refuse ``os.listdir`` on a DESCRIPTOR whose target is *victim_dir*.

        Matched by comparing the fd's identity to the directory's, so the walk's
        other listings -- the root, siblings, the staging tree -- are untouched, and so
        the by-name ``os.listdir(path)`` the fallback walk performs is untouched too.
        """
        real = os.listdir
        want = os.stat(victim_dir)
        ident = (want.st_dev, want.st_ino)
        monkeypatch.setattr(pinned_fs, "supports_pinned_tree_walk", lambda: PINNED_TREE_WALK)

        def _fn(path="."):
            if isinstance(path, int):
                st = os.fstat(path)
                if (st.st_dev, st.st_ino) == ident:
                    raise PermissionError(errno.EACCES, "Permission denied", str(victim_dir))
            return real(path)

        monkeypatch.setattr(os, "listdir", _fn)

    @pytest.fixture
    def pinned_only(self):
        if not PINNED_TREE_WALK:
            pytest.skip("the descriptor listing exists only on the pinned walk")

    def test_a_nested_directory_is_recorded_and_the_rest_is_staged(
        self, home, pinned_only, monkeypatch, tmp_path
    ):
        victim = home / "workspace" / DIR_VICTIM
        victim.mkdir()
        (victim / "inner.txt").write_text("wanted\n")
        (home / "workspace" / "kept.txt").write_text("kept\n")
        self._deny_listing_of_fd_for(monkeypatch, victim)
        seen: list[tuple[str, str]] = []
        snap._copytree_safe(
            home / "workspace",
            tmp_path / "dst",
            on_skip=lambda r, p: seen.append((r, p)),
            skip_unreadable=True,
        )
        assert (tmp_path / "dst" / "kept.txt").is_file()
        assert not (tmp_path / "dst" / DIR_VICTIM / "inner.txt").exists()
        assert [(r, p) for r, p in seen if p.endswith(DIR_VICTIM)] == [
            ("unreadable_entry", str(victim))
        ], seen

    def test_the_tree_root_itself_is_recorded(self, home, pinned_only, monkeypatch, tmp_path):
        root = home / "workspace"
        self._deny_listing_of_fd_for(monkeypatch, root)
        seen: list[tuple[str, str]] = []
        snap._copytree_safe(
            root, tmp_path / "dst", on_skip=lambda r, p: seen.append((r, p)), skip_unreadable=True
        )
        assert seen == [("unreadable_entry", str(root))], seen

    def test_the_default_still_raises(self, home, pinned_only, monkeypatch, tmp_path):
        """Restore and merge never opt in, so for them this must stay a hard stop."""
        victim = home / "workspace" / DIR_VICTIM
        victim.mkdir()
        self._deny_listing_of_fd_for(monkeypatch, victim)
        seen: list[tuple[str, str]] = []
        with pytest.raises(PermissionError):
            snap._copytree_safe(
                home / "workspace", tmp_path / "dst", on_skip=lambda r, p: seen.append((r, p))
            )
        assert not [r for r, _ in seen if r == "unreadable_entry"]

    def test_a_failing_disk_at_the_listing_is_not_absorbed(
        self, home, pinned_only, monkeypatch, tmp_path
    ):
        """Only the permission class is tolerated; EIO at the listing still ends the walk."""
        victim = home / "workspace" / DIR_VICTIM
        victim.mkdir()
        real = os.listdir
        want = os.stat(victim)
        ident = (want.st_dev, want.st_ino)
        monkeypatch.setattr(pinned_fs, "supports_pinned_tree_walk", lambda: PINNED_TREE_WALK)

        def _fn(path="."):
            if isinstance(path, int):
                st = os.fstat(path)
                if (st.st_dev, st.st_ino) == ident:
                    raise OSError(errno.EIO, "Input/output error", str(victim))
            return real(path)

        monkeypatch.setattr(os, "listdir", _fn)
        with pytest.raises(OSError) as caught:
            snap._copytree_safe(
                home / "workspace",
                tmp_path / "dst",
                on_skip=lambda r, p: None,
                skip_unreadable=True,
            )
        assert caught.value.errno == errno.EIO

    def test_the_snapshot_completes_and_holds_the_prune(
        self, home, pinned_only, monkeypatch, tmp_path, capsys
    ):
        out = tmp_path / "out"
        out.mkdir()
        existing = out / "kirocrew-snapshot-20260101T000000Z.tar.gz"
        with tarfile.open(existing, "w:gz") as tf:
            payload = tmp_path / "payload"
            payload.mkdir()
            tf.add(str(payload), arcname="kirocrew-snapshot-20260101T000000Z")
        victim = home / "workspace" / DIR_VICTIM
        victim.mkdir()
        (victim / "inner.txt").write_text("wanted\n")
        self._deny_listing_of_fd_for(monkeypatch, victim)
        rc = snap.snapshot_main([str(out), "--components", "workspace", "--keep", "1"])
        assert rc == 0
        assert existing.is_file(), "a listing refusal pruned the last complete backup"
        # Two bundles now: the seeded decoy and the one just written. The manifest is
        # read from the NEW one, so the decoy is removed first rather than sorted past.
        existing.unlink()
        manifest = _manifest_of(out, tmp_path)
        omitted = [s for s in manifest["skipped"] if s["path"].endswith(DIR_VICTIM)]
        assert omitted and omitted[0]["reason"] == "unreadable_entry", manifest["skipped"]
        assert "Not pruning" in capsys.readouterr().out
