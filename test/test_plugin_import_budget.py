"""The plugin import is bounded in BREADTH as well as depth and per-file size.

``MAX_RESOURCE_BYTES`` bounds one file and ``MAX_SKILL_TREE_DEPTH`` bounds how
deep the walk goes. Neither bounds how MANY files a package may hand over, so a
plugin of individually legal files could copy without end -- the module's own
contract asks every loop over untrusted input for a ceiling, and this loop had
none.
"""

from __future__ import annotations

import os

import pytest

from conftest import make_dir_link
from kiro_crew.apps import plugin_import


@pytest.fixture
def tree(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    return src


def _files(root, n, size=1):
    for i in range(n):
        (root / f"f{i}.txt").write_text("x" * size, encoding="utf-8")


def test_the_item_ceiling_stops_the_copy_and_records_why(tree, tmp_path, monkeypatch):
    """The ceiling counts ITEMS: the destination directory is one of them."""
    monkeypatch.setattr(plugin_import, "MAX_IMPORT_FILES", 5)
    _files(tree, 12)
    copied, skipped = plugin_import._copy_tree_without_symlinks(tree, tmp_path / "out")
    assert copied == 4
    assert len(list((tmp_path / "out").iterdir())) == 4
    assert any("import budget spent" in s for s in skipped)


def test_empty_directories_are_charged_too(tree, tmp_path, monkeypatch):
    """Breadth of EMPTY directories is inodes spent, so it cannot be free."""
    monkeypatch.setattr(plugin_import, "MAX_IMPORT_FILES", 4)
    for i in range(20):
        (tree / f"d{i}").mkdir()
    copied, skipped = plugin_import._copy_tree_without_symlinks(tree, tmp_path / "out")
    assert copied == 0
    made = sum(1 for p in (tmp_path / "out").rglob("*") if p.is_dir())
    assert made == 3, "root + 3 charged children, then the budget is spent"
    assert any("directory skipped" in s for s in skipped)


def test_the_byte_ceiling_stops_the_copy_too(tree, tmp_path, monkeypatch):
    monkeypatch.setattr(plugin_import, "MAX_IMPORT_BYTES", 30)
    _files(tree, 10, size=10)
    copied, skipped = plugin_import._copy_tree_without_symlinks(tree, tmp_path / "out")
    assert copied == 3
    assert any("import budget spent" in s for s in skipped)


def test_the_budget_is_shared_across_subdirectories(tree, tmp_path, monkeypatch):
    """Breadth is the unbounded axis, so a per-directory counter fixes nothing."""
    monkeypatch.setattr(plugin_import, "MAX_IMPORT_FILES", 6)
    for d in ("a", "b", "c"):
        sub = tree / d
        sub.mkdir()
        _files(sub, 3)
    copied, _ = plugin_import._copy_tree_without_symlinks(tree, tmp_path / "out")
    # root dir + subdir a + its 3 files + subdir b = 6 items, so 3 files land.
    assert copied == 3


def test_a_package_inside_the_ceilings_copies_whole(tree, tmp_path):
    _files(tree, 7)
    copied, skipped = plugin_import._copy_tree_without_symlinks(tree, tmp_path / "out")
    assert copied == 7
    assert not [s for s in skipped if "budget" in s]


def test_one_budget_spans_every_skill_of_one_conversion(tmp_path, monkeypatch):
    """``_convert_skills`` walks up to MAX_SKILLS trees: the cap must hold across them."""
    monkeypatch.setattr(plugin_import, "MAX_IMPORT_FILES", 6)
    root = tmp_path / "pkg"
    skills = root / plugin_import.DEFAULT_SKILLS_DIR
    for name in ("alpha", "beta", "gamma"):
        d = skills / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text("# skill", encoding="utf-8")
        _files(d, 4)

    report = plugin_import.ImportReport(
        source_root=str(root), manifest_path="", source_format="test", app_name="pkg"
    )
    out = tmp_path / "out"
    emitted = plugin_import._convert_skills(root, None, out, report)

    assert emitted, "the skills were still discovered"
    copied = sum(1 for p in out.rglob("*") if p.is_file())
    assert copied == 5, "one item of the six is the skill's own directory"
    assert any("import budget spent after" in w for w in report.warnings)


def test_the_spent_budget_is_reported_once_at_the_top_level(tmp_path, monkeypatch):
    monkeypatch.setattr(plugin_import, "MAX_IMPORT_FILES", 2)
    root = tmp_path / "pkg"
    d = root / plugin_import.DEFAULT_SKILLS_DIR / "alpha"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("# skill", encoding="utf-8")
    _files(d, 6)

    report = plugin_import.ImportReport(
        source_root=str(root), manifest_path="", source_format="test", app_name="pkg"
    )
    plugin_import._convert_skills(root, None, tmp_path / "out", report)
    summaries = [w for w in report.warnings if "import budget spent after" in w]
    assert len(summaries) == 1


def test_a_file_swapped_for_a_symlink_mid_walk_is_not_followed(tmp_path, monkeypatch):
    """The TOCTOU the no-follow copy closes: judged and copied must be one object.

    The swap is performed between the walk's symlink check and the copy by
    patching the check itself to do it -- the only way to hit that window
    deterministically rather than by racing a real agent.
    """
    src = tmp_path / "src"
    src.mkdir()
    secret = tmp_path / "outside.txt"
    secret.write_text("SECRET", encoding="utf-8")
    victim = src / "f.txt"
    victim.write_text("fine", encoding="utf-8")

    # POSIX only, and the reason is the INJECTION rather than the property. The
    # copy refuses a swapped entry through ``O_NOFOLLOW``, which is absent on
    # Windows and degrades to 0, so there the swap cannot be refused and these
    # assertions describe nothing. The no-follow property itself is platform
    # independent and is pinned without a seam by
    # ``test_a_directory_link_inside_a_resource_is_never_followed``.
    if os.name != "posix":
        pytest.skip("the mid-walk swap is refused by O_NOFOLLOW, which Windows lacks")

    real_check = plugin_import._is_link_or_reparse

    def _swap(path):
        # The seam is the walk's OWN boundary check, not a stdlib call: patching
        # ``Path.is_symlink`` or ``os.path.islink`` arms nothing once the check
        # delegates elsewhere, and patching them process-wide also reaches the
        # temporary-directory teardown. Matched by NAME because the walk builds
        # its own Path per entry, which need not compare equal to this one.
        answer = real_check(path)
        if path.name == victim.name and path.exists() and not answer:
            path.unlink()
            path.symlink_to(secret)
        return answer

    monkeypatch.setattr(plugin_import, "_is_link_or_reparse", _swap)
    copied, skipped = plugin_import._copy_tree_without_symlinks(src, tmp_path / "out")
    monkeypatch.undo()

    assert copied == 0
    assert not (tmp_path / "out" / "f.txt").exists()
    assert skipped, "the refusal is recorded rather than silent"


def test_a_directory_link_inside_a_resource_is_never_followed(tmp_path):
    """The no-follow property, pinned on every platform and without a seam.

    ``make_dir_link`` plants a junction on Windows and a directory symlink on
    POSIX. A junction needs no privilege, and both are traversed by the same
    reparse machinery, so the refusal is exercised on both platforms rather than
    asserted on one and skipped on the other. The swap test above needs
    ``O_NOFOLLOW`` and so stays POSIX-only; this one needs nothing but a link.
    """
    src = tmp_path / "src"
    src.mkdir()
    (src / "real.txt").write_text("fine", encoding="utf-8")

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("SECRET", encoding="utf-8")

    make_dir_link(src / "escape", outside)

    out = tmp_path / "out"
    copied, skipped = plugin_import._copy_tree_without_symlinks(src, out)

    assert copied == 1, "the one real file is copied"
    assert (out / "real.txt").read_text(encoding="utf-8") == "fine"
    assert not (out / "escape").exists(), "the link itself is not recreated"
    assert not (out / "secret.txt").exists(), "nothing from outside the root arrives"
    assert any("escape" in note for note in skipped), "the refusal is recorded"


def test_the_boundary_check_answers_true_for_an_unjudgeable_entry(tmp_path):
    """A stat that fails is a refusal, where ``is_link_or_junction`` answers False.

    This is one of the two properties the boundary check adds on top of
    ``platform_compat``, so it needs its own pin: an entry whose kind cannot be
    read is not one to descend into.
    """
    missing = tmp_path / "gone"
    assert plugin_import._is_link_or_reparse(missing) is True


def test_the_boundary_check_accepts_a_plain_file_and_directory(tmp_path):
    """The complement, so the refusal above is not vacuously true of everything."""
    plain = tmp_path / "plain.txt"
    plain.write_text("x", encoding="utf-8")
    folder = tmp_path / "folder"
    folder.mkdir()

    assert plugin_import._is_link_or_reparse(plain) is False
    assert plugin_import._is_link_or_reparse(folder) is False


class _StatReportingSize:
    """The real stat values with ``st_size`` replaced.

    Only the four fields the copy reads. Constructing an ``os.stat_result``
    would need all ten in the right order, and the extra nine carry no meaning
    for this test.
    """

    def __init__(self, real, size: int) -> None:
        self.st_mode = real.st_mode
        self.st_size = size
        self.st_atime = real.st_atime
        self.st_mtime = real.st_mtime


class _OsReportingSize:
    """A stand-in for the module's ``os`` whose ``fstat`` reports one size.

    Scoped to the module under test rather than to the real ``os``: patching the
    global module routes every other descriptor stat in the process -- pytest's
    own, the coverage writer's -- through this substitution, and the walk's fstat
    is the only one this test is about.

    Keying the substitution on ``st_ino`` is the other way to narrow it, and it is
    not portable: a path stat and a descriptor stat need not report the same inode
    on every platform, so the lie would either miss its target or match
    everything. Confining it by MODULE needs no such agreement, and the copy makes
    exactly one ``fstat`` call, so scope alone is precise enough.
    """

    def __init__(self, real, size: int) -> None:
        self._real = real
        self._size = size

    def __getattr__(self, name):
        # open, utime, close and the O_* flags all pass straight through.
        return getattr(self._real, name)

    def fstat(self, fd):
        return _StatReportingSize(self._real.fstat(fd), self._size)


def _fstat_reports(monkeypatch, size: int) -> None:
    """Make the walk's own ``fstat`` report *size* for whatever it opens.

    The walk decides the ceiling, the budget charge and the number of bytes to
    read from one ``fstat``, so a source whose length changes after it is measured
    can only be simulated by moving that measurement away from the truth.
    """
    monkeypatch.setattr(plugin_import, "os", _OsReportingSize(os, size))


def test_a_source_longer_than_it_measured_is_copied_only_to_the_measured_size(
    tmp_path, monkeypatch
):
    """The copy stops at the size the budget was charged for, not at EOF.

    A file still being written grows after the walk measures it. Copying to EOF
    puts more bytes in the output than the declared ceiling admitted and more
    than the budget recorded, so the two numbers that are supposed to describe
    the same copy stop agreeing.
    """
    src = tmp_path / "src"
    src.mkdir()
    entry = src / "grows.bin"
    entry.write_bytes(b"A" * 40 + b"B" * 60)
    _fstat_reports(monkeypatch, 40)

    target = tmp_path / "out.bin"
    budget = plugin_import._ImportBudget()
    note = plugin_import._copy_regular_file_nofollow(entry, target, budget)

    assert note is None, "a longer source is a successful copy, not a refusal"
    assert target.stat().st_size == 40, "only the measured bytes are written"
    assert target.read_bytes() == b"A" * 40, "and they are the leading ones"
    assert budget.nbytes == 40, "the charge and the output are the same number"


def test_a_source_shorter_than_it_measured_is_refused_rather_than_truncated(tmp_path, monkeypatch):
    """A source that shrank is skipped, not published short.

    The alternative is worse than a refusal: the output would look like a
    complete file, be charged for the full measured size, and differ from the
    package the manifest describes, with nothing recording that it is partial.
    """
    src = tmp_path / "src"
    src.mkdir()
    entry = src / "shrinks.bin"
    entry.write_bytes(b"C" * 10)
    _fstat_reports(monkeypatch, 500)

    target = tmp_path / "out.bin"
    budget = plugin_import._ImportBudget()
    note = plugin_import._copy_regular_file_nofollow(entry, target, budget)

    assert note is not None, "a short read is reported, never silent"
    assert "shrank" in note, f"and it names the cause: {note}"
    assert str(entry) in note, "and the entry, so the skip is attributable"


def test_an_unchanged_source_copies_whole(tmp_path):
    """The complement, with no patched stat: the bound does not truncate honest files."""
    src = tmp_path / "src"
    src.mkdir()
    entry = src / "plain.bin"
    body = b"D" * (plugin_import._COPY_CHUNK_BYTES + 7)
    entry.write_bytes(body)

    target = tmp_path / "out.bin"
    budget = plugin_import._ImportBudget()
    note = plugin_import._copy_regular_file_nofollow(entry, target, budget)

    assert note is None
    assert target.read_bytes() == body, "a file spanning several chunks arrives intact"
    assert budget.nbytes == len(body)


class TestAFailedCopyLeavesNothingBehind:
    """A skip note promises nothing was emitted, and the budget agrees.

    The charge is taken from the measured size before the copy runs, so a copy
    that fails has both written bytes to the target and spent allowance. Left
    alone the target converts as a valid, shorter resource, and the spent
    allowance shrinks the budget for every later file.
    """

    def test_a_shrinking_source_leaves_no_output(self, tmp_path, monkeypatch):
        from kiro_crew.apps import plugin_import as pi

        src = tmp_path / "a.txt"
        src.write_bytes(b"x" * 10)
        target = tmp_path / "out.txt"

        real_fstat = pi.os.fstat

        # Report a bigger size than the file holds, so the copy runs out of
        # bytes and takes the shrank path.
        class _Stat:
            def __init__(self, st):
                self._st = st
                self.st_size = 500

            def __getattr__(self, n):
                return getattr(self._st, n)

        target_ino = src.stat().st_ino

        def fake_fstat(fd):
            st = real_fstat(fd)
            return _Stat(st) if st.st_ino == target_ino else st

        monkeypatch.setattr(pi.os, "fstat", fake_fstat)

        budget = pi._ImportBudget()
        note = pi._copy_regular_file_nofollow(src, target, budget)

        assert note is not None and "shrank" in note, note
        assert not target.exists(), "a failed copy left an installable partial file"
        assert budget.files == 0, f"the charge was not refunded: files={budget.files}"
        assert budget.nbytes == 0, f"the charge was not refunded: nbytes={budget.nbytes}"

    def test_the_sink_refuses_an_existing_target(self, tmp_path):
        """O_EXCL: an object already at the output path is never written through.

        A path the walk did not create is not this import's to truncate, whether
        it is a planted symlink pointing outside the tree or a plain file.
        """
        from kiro_crew.apps import plugin_import as pi

        src = tmp_path / "a.txt"
        src.write_bytes(b"hello")
        outside = tmp_path / "precious.txt"
        outside.write_bytes(b"do not touch")
        target = tmp_path / "out.txt"
        target.write_bytes(b"squatter")

        budget = pi._ImportBudget()
        note = pi._copy_regular_file_nofollow(src, target, budget)

        assert note is not None, "an existing target was written through"
        assert target.read_bytes() == b"squatter", "the existing file was overwritten"
        assert outside.read_bytes() == b"do not touch"
        assert budget.files == 0 and budget.nbytes == 0, "the refused copy kept its charge"


class TestRollbackSparesFilesItDidNotCreate:
    """A failed conversion clears its own output, not the directory's contents.

    The rollback ran on a single "was it empty when I started" flag, so any
    output directory that was empty at the start had everything in it removed on
    failure -- including files another writer put there. Entries present before
    the conversion are now spared.
    """

    def test_a_preexisting_file_survives_a_failed_conversion(self, tmp_path, monkeypatch):
        from kiro_crew.apps import plugin_import as pi

        out = tmp_path / "out"
        out.mkdir()
        keeper = out / "someone-elses.txt"
        keeper.write_text("not mine", encoding="utf-8")

        def _boom(*a, **kw):
            # Write something of our own first, so the test distinguishes
            # "removed nothing" from "removed only ours".
            (out / "ours.json").write_text("{}", encoding="utf-8")
            raise RuntimeError("conversion failed")

        monkeypatch.setattr(pi, "_convert_plugin_package", _boom)

        import pytest as _pytest

        with _pytest.raises(RuntimeError):
            pi.convert_plugin_package(tmp_path / "src", out)

        assert keeper.exists(), "the rollback deleted a file it did not create"
        assert keeper.read_text(encoding="utf-8") == "not mine"
        assert not (out / "ours.json").exists(), "the rollback left its own partial output"
        assert out.exists(), "a directory it did not create was removed"
