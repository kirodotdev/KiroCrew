"""The descriptor-pinned staging primitive, and snapshot/restore's use of it.

Written as an A/B suite rather than a list of assertions. The point of this change is
that a pinned traversal is strictly stronger than a by-name one, and the only honest
way to show that is to run the SAME attack against both paths and watch one leak while
the other refuses -- an exception-only assertion cannot tell "pinned" from "got lucky".

The mid-walk swap is performed inside the ``ignore`` callback, which both paths invoke
once per directory with that directory's contents. That is the check-to-use window
made deterministic: the screen has just looked at the entry and declared it fine, and
the copy of that entry has not happened yet.

This file pins the differential and the sections after it up to the window between
creating a directory and opening it. The skips that precede a delete and the validations
that get discarded live in ``test_pinned_staging_skips_before_delete.py``; the staging
helper's own contract, the home of ``fd_real_path`` and the identity a copy reports
before it writes in ``test_pinned_staging_helper_contract.py``; and the platform mark,
the cross-platform keyword and the symlink helper both staging files read in
``pinned_staging_helpers.py``.
"""

from __future__ import annotations

import json
import os
import tarfile
from collections.abc import Callable
from pathlib import Path

import pytest
from pinned_staging_helpers import (
    UNPINNED_OK,
    _symlink_or_skip,
    pinned_only,
)

from kiro_crew import pinned_fs, snapshot


def _tree_with_victim(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A source tree to stage, plus a victim directory nothing should reach.

    Returns ``(src_root, mid_dir, victim_dir)``. ``mid_dir`` is the ancestor that gets
    swapped: it is a real directory when the screen looks at it and a link to
    ``victim_dir`` by the time the copy of its contents happens.
    """
    src = tmp_path / "source"
    mid = src / "mid"
    mid.mkdir(parents=True)
    (mid / "ordinary.txt").write_text("ordinary\n", encoding="utf-8")

    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "stolen.txt").write_text("CREDENTIAL\n", encoding="utf-8")
    return src, mid, victim


def _swap_on_screen(mid: Path, victim: Path):
    """An ``ignore`` callback that swaps *mid* for a link to *victim*, once.

    Fires when the walk screens the directory that CONTAINS *mid*, i.e. after that
    directory has been listed and before its entries are copied.
    """
    state = {"done": False}

    def _ignore(directory: str, contents: list[str]) -> set[str]:
        if not state["done"] and mid.name in contents:
            state["done"] = True
            mid.rename(mid.parent / "mid-moved")
            _symlink_or_skip(mid, victim)
        return set()

    return _ignore


# ── The differential: pinned refuses where by-name follows ────────────────────


@pinned_only
def test_an_ancestor_swapped_after_the_screen_leaks_by_name_and_not_when_pinned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The whole reason this module exists, asserted on WHERE THE BYTES LANDED.

    Run twice against identical trees. The by-name traversal is expected to copy the
    victim's contents, because ``shutil.copytree`` captures its entries with one
    ``scandir`` and then descends into ``mid`` BY NAME -- the cached ``DirEntry`` still
    says "directory", and a fresh listing of that name now lands in the victim. The
    pinned traversal re-stats through the descriptor it holds and skips it.

    The control half has to force the by-name branch by patching the platform probe.
    ``allow_unpinned=True`` alone does not: it is a permission for a platform that
    cannot pin, not a switch that turns pinning off where it works -- which is itself
    worth pinning, since a flag that silently downgraded a capable platform would be a
    far worse bug than the one this change fixes.

    If the by-name half ever stops leaking, this test has stopped testing anything and
    the assertion below says so rather than passing quietly.
    """
    src_a, mid_a, victim_a = _tree_with_victim(tmp_path / "by_name")
    dst_a = tmp_path / "staged_by_name"
    with monkeypatch.context() as no_pinning:
        no_pinning.setattr(pinned_fs, "supports_pinned_tree_walk", lambda: False)
        snapshot._copytree_safe(
            src_a, dst_a, allow_unpinned=True, ignore=_swap_on_screen(mid_a, victim_a)
        )
    leaked = (dst_a / "mid" / "stolen.txt").exists()

    src_b, mid_b, victim_b = _tree_with_victim(tmp_path / "pinned")
    dst_b = tmp_path / "staged_pinned"
    snapshot._copytree_safe(src_b, dst_b, ignore=_swap_on_screen(mid_b, victim_b))

    assert leaked, (
        "the by-name control did not leak, so this test no longer demonstrates the "
        "difference the pinned walk exists to make -- fix the control, do not delete "
        "the assertion"
    )
    assert not (
        dst_b / "mid" / "stolen.txt"
    ).exists(), "the pinned walk followed an ancestor swapped after the screen approved it"
    assert "Skipping symlink in source tree" in capsys.readouterr().out


@pinned_only
def test_the_opt_in_flag_does_not_turn_pinning_off_where_it_works(tmp_path: Path) -> None:
    """``--allow-unpinned-staging`` permits a fallback; it never requests one.

    Stated as its own test because the distinction is load-bearing: if the flag were a
    mode switch, anyone passing it once (to get past an unrelated failure, or in a
    script copied from a Windows runbook) would silently give up the pinning on every
    platform.
    """
    src, mid, victim = _tree_with_victim(tmp_path)
    dst = tmp_path / "staged"
    snapshot._copytree_safe(src, dst, allow_unpinned=True, ignore=_swap_on_screen(mid, victim))
    assert not (dst / "mid" / "stolen.txt").exists()


@pinned_only
def test_open_dir_pinned_refuses_a_parent_that_became_a_link(tmp_path: Path) -> None:
    """The root's OWN ancestor chain is pinned, which the preserved branches did not do.

    ``os.open(root, O_DIRECTORY | O_NOFOLLOW)`` refuses a link AT the root's name but
    walks every ancestor by name to get there. Here the parent is captured before the
    swap -- the state a caller is in when it loses the resolve-to-open race -- and the
    pinned chain has to refuse it.
    """
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "inside").mkdir()

    parent = tmp_path / "holder"
    parent.mkdir()
    (parent / "inside").mkdir()
    resolved_parent = os.path.realpath(parent)

    parent.rename(tmp_path / "holder-moved")
    _symlink_or_skip(parent, victim)

    with pytest.raises(pinned_fs.PinnedPathRefusal) as excinfo:
        pinned_fs.open_in_pinned_parent(
            resolved_parent,
            "inside",
            flags=os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            mode=0o700,
            what="staging root",
        )
    assert "became a symbolic link" in str(excinfo.value)


@pinned_only
def test_a_refused_link_names_both_causes_rather_than_asserting_a_swap(tmp_path: Path) -> None:
    """The refusal must not report an attack it cannot see.

    A link met on the walk has two causes: swapped after the caller resolved
    the path, or there all along because the caller never resolved it. Nothing
    reachable from here tells them apart - the filesystem is read after the
    swap either way - so the message names both and asserts neither. Claiming
    a swap sends a reader hunting an attacker who was never there; claiming a
    caller error denies one who was.
    """
    real = tmp_path / "real"
    (real / "inside").mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(real)

    with pytest.raises(pinned_fs.PinnedPathRefusal) as excinfo:
        pinned_fs.pin_parent(str(link / "inside"), what="staging root")
    message = str(excinfo.value)
    assert "became a symbolic link after the path was checked" in message
    assert "was one all along because this path was handed in unresolved" in message
    assert "is not a directory at all" in message


# ── Hardlink aliases ─────────────────────────────────────────────────────────


def test_a_hardlinked_source_is_refused_rather_than_dereferenced(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``shutil.copy2`` would have shipped a credential alias as ordinary bytes.

    A hardlink shares its target's inode, so ``realpath`` yields the alias's own name,
    ``is_symlink()`` is False, and ``O_NOFOLLOW`` has no link to refuse. The check has
    to happen on the open descriptor, which is what this pins.
    """
    secret = tmp_path / "credential"
    secret.write_text("AKIA-not-real\n", encoding="utf-8")
    alias = tmp_path / "config.json"
    try:
        os.link(secret, alias)
    except (OSError, NotImplementedError):  # pragma: no cover - platform dependent
        pytest.skip("hard links are unavailable on this host")

    dst = tmp_path / "staged.json"
    copied = pinned_fs.copy_file_pinned(str(alias), str(dst), on_skip=snapshot._report_skip)

    assert copied is False
    assert not dst.exists()
    assert "hardlinked or non-regular" in capsys.readouterr().out


def test_a_single_link_regular_file_still_copies_with_mode_and_mtime(tmp_path: Path) -> None:
    """The refusal above must not be a blanket one: the ordinary case still works."""
    src = tmp_path / "plain.txt"
    src.write_text("content\n", encoding="utf-8")
    os.chmod(src, 0o640)
    dst = tmp_path / "copied.txt"

    assert pinned_fs.copy_file_pinned(str(src), str(dst)) is True
    assert dst.read_text(encoding="utf-8") == "content\n"
    if os.name == "posix":
        assert (dst.stat().st_mode & 0o777) == 0o640
    assert dst.stat().st_mtime_ns == src.stat().st_mtime_ns


def test_an_oversize_source_is_refused_before_the_destination_exists(tmp_path: Path) -> None:
    """GPT review r12: a ceiling checked only AFTER a copy lets the copy itself
    exhaust the destination volume. The fstat pre-check refuses first -- and
    fstat reports a sparse file's LOGICAL size, so a swapped-in sparse giant is
    refused with zero bytes written and no destination entry at all."""
    src = tmp_path / "huge.bin"
    src.write_bytes(b"x" * 64)
    dst = tmp_path / "copied.bin"
    reasons: list[tuple[str, str]] = []

    copied = pinned_fs.copy_file_pinned(
        str(src), str(dst), max_bytes=16, on_skip=lambda r, p: reasons.append((r, p))
    )

    assert copied is False
    assert reasons and reasons[0][0] == pinned_fs.SKIP_TOO_LARGE
    assert not dst.exists(), "the pre-check must fire before the destination is created"


def test_a_source_that_is_not_the_vetted_inode_is_refused(tmp_path: Path) -> None:
    """GPT review r29 (meetings import): the descriptor pin proves the copied
    inode is the OPENED inode, not that it is the inode the caller's validation
    judged. ``expected_src_ident`` closes that validate->copy window: a regular
    single-link file swapped in at the name fstats to a different identity and
    is refused with zero bytes written."""
    src = tmp_path / "vetted.bin"
    src.write_bytes(b"vetted")
    st = os.stat(src)
    vetted = (st.st_dev, st.st_ino)
    # Allocate the replacement while the original still exists: unlink+recreate
    # lets the filesystem hand the new file the SAME inode number (observed on
    # CI), which would make this test flaky rather than a swap replay.
    swapped = tmp_path / "swapped.bin"
    swapped.write_bytes(b"swapped-in")
    assert (os.stat(swapped).st_dev, os.stat(swapped).st_ino) != vetted
    os.replace(swapped, src)
    dst = tmp_path / "copied.bin"
    reasons: list[tuple[str, str]] = []

    copied = pinned_fs.copy_file_pinned(
        str(src), str(dst), expected_src_ident=vetted, on_skip=lambda r, p: reasons.append((r, p))
    )

    assert copied is False
    assert reasons and reasons[0][0] == pinned_fs.SKIP_IDENTITY_CHANGED
    assert not dst.exists(), "no byte of an unvetted inode may land"


def test_a_source_matching_the_vetted_inode_is_copied(tmp_path: Path) -> None:
    src = tmp_path / "vetted.bin"
    src.write_bytes(b"vetted")
    st = os.stat(src)
    dst = tmp_path / "copied.bin"

    assert (
        pinned_fs.copy_file_pinned(str(src), str(dst), expected_src_ident=(st.st_dev, st.st_ino))
        is True
    )
    assert dst.read_bytes() == b"vetted"


def test_a_source_grown_after_fstat_aborts_at_the_first_excess_byte(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one case the fstat pre-check cannot see: bytes appended between the
    fstat and the read. The copy loop is bounded, so the excess aborts the copy
    and empties what was written through the descriptor."""
    src = tmp_path / "growing.bin"
    src.write_bytes(b"x" * 64)
    dst = tmp_path / "copied.bin"
    reasons: list[tuple[str, str]] = []

    real_fstat = os.fstat

    def lying_fstat(fd: int) -> os.stat_result:
        st = real_fstat(fd)
        if st.st_size == 64:  # only our source; every other caller sees the truth
            return os.stat_result(
                (
                    st.st_mode,
                    st.st_ino,
                    st.st_dev,
                    st.st_nlink,
                    st.st_uid,
                    st.st_gid,
                    8,
                    st.st_atime,
                    st.st_mtime,
                    st.st_ctime,
                )
            )
        return st

    monkeypatch.setattr(os, "fstat", lying_fstat)
    copied = pinned_fs.copy_file_pinned(
        str(src), str(dst), max_bytes=16, on_skip=lambda r, p: reasons.append((r, p))
    )
    monkeypatch.undo()

    assert copied is False
    assert reasons and reasons[0][0] == pinned_fs.SKIP_TOO_LARGE
    # The destination entry exists (created before the loop) but holds no bytes:
    # the partial content was emptied through the descriptor.
    assert dst.stat().st_size == 0


def test_the_ceiling_does_not_refuse_a_source_at_exactly_the_limit(tmp_path: Path) -> None:
    """Boundary: max_bytes is a ceiling, not an exclusive bound."""
    src = tmp_path / "exact.bin"
    src.write_bytes(b"x" * 16)
    dst = tmp_path / "copied.bin"

    assert pinned_fs.copy_file_pinned(str(src), str(dst), max_bytes=16) is True
    assert dst.read_bytes() == b"x" * 16


# ── The restore side ─────────────────────────────────────────────────────────


@pinned_only
def test_restore_moves_a_symlinked_core_file_aside_instead_of_writing_through_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The silent partial restore, and the failure hiding behind it.

    Before this change the name-based screen declined to MOVE a symlinked core file to
    the backup and then ``shutil.copy2`` wrote through the very link it had just
    declined to move. Skipping the whole entry instead would have been no better: the
    archive's version of that file would silently never be restored, and the command
    would still report success.

    Both are asserted here -- the victim keeps its bytes, AND the snapshot's version
    actually lands.
    """
    snap = tmp_path / "payload"
    snap.mkdir()
    (snap / "crons.json").write_text("[]\n", encoding="utf-8")

    victim = tmp_path / "victim.json"
    victim.write_text("PRECIOUS\n", encoding="utf-8")

    mc = tmp_path / "home"
    mc.mkdir()
    _symlink_or_skip(mc / "crons.json", victim)

    backup = mc / "backup"
    backup.mkdir()

    snapshot._backup_and_copy(mc, backup, snap, "crons")

    assert (
        victim.read_text(encoding="utf-8") == "PRECIOUS\n"
    ), "the restore wrote through the symlink it was supposed to move aside"
    assert (mc / "crons.json").read_text(
        encoding="utf-8"
    ) == "[]\n", "the archive's version was not restored -- a silent partial restore"
    assert not (mc / "crons.json").is_symlink()
    assert (backup / "crons.json").is_symlink(), "the link itself belongs in the backup"
    assert "Moving symlinked core file aside" in capsys.readouterr().out


@pinned_only
def test_restore_refuses_when_the_name_is_still_occupied_after_the_backup_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The backstop for the case the move above cannot resolve.

    The move is neutralised rather than made to fail, because the invariant being
    pinned is about the STATE the copy runs in ("this name is still taken"), not about
    any particular reason the move did not happen. Exclusive creation is what turns
    that state into a refusal instead of a write into whatever now sits there -- and
    the live bytes have to survive it.
    """
    snap = tmp_path / "payload"
    snap.mkdir()
    (snap / "crons.json").write_text("[]\n", encoding="utf-8")

    mc = tmp_path / "home"
    mc.mkdir()
    (mc / "crons.json").write_text("occupied\n", encoding="utf-8")
    backup = mc / "backup"
    backup.mkdir()

    monkeypatch.setattr(snapshot.os, "rename", lambda *a, **k: None)

    with pytest.raises(pinned_fs.PinnedPathRefusal) as excinfo:
        snapshot._backup_and_copy(mc, backup, snap, "crons")

    assert "still occupies that name" in str(excinfo.value)
    assert (mc / "crons.json").read_text(
        encoding="utf-8"
    ) == "occupied\n", "the live file was overwritten even though it was never moved aside"


@pinned_only
def test_merge_does_not_overwrite_and_skips_a_symlinked_source(tmp_path: Path) -> None:
    """The no-overwrite promise is now exclusive creation rather than a prior exists()."""
    src = tmp_path / "from_snapshot"
    (src / "nested").mkdir(parents=True)
    (src / "nested" / "new.txt").write_text("new\n", encoding="utf-8")
    (src / "nested" / "kept.txt").write_text("from snapshot\n", encoding="utf-8")
    _symlink_or_skip(src / "nested" / "link.txt", tmp_path / "anything")

    dst = tmp_path / "live"
    (dst / "nested").mkdir(parents=True)
    (dst / "nested" / "kept.txt").write_text("LOCAL WINS\n", encoding="utf-8")

    snapshot._copy_tree_no_overwrite(src, dst)

    assert (dst / "nested" / "new.txt").read_text(encoding="utf-8") == "new\n"
    assert (dst / "nested" / "kept.txt").read_text(encoding="utf-8") == "LOCAL WINS\n"
    assert not (dst / "nested" / "link.txt").exists()


# ── Platforms that cannot pin ────────────────────────────────────────────────


def test_a_platform_that_cannot_pin_refuses_until_the_operator_says_otherwise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refuse rather than fall back by name -- and name the flag that permits it.

    Patched on ``pinned_fs`` because that is where the capability question lives; the
    snapshot module reads it through the module rather than binding it at import.
    """
    monkeypatch.setattr(pinned_fs, "supports_pinned_tree_walk", lambda: False)
    src = tmp_path / "source"
    src.mkdir()
    (src / "file.txt").write_text("x\n", encoding="utf-8")

    with pytest.raises(pinned_fs.PinnedPathRefusal) as excinfo:
        snapshot._copytree_safe(src, tmp_path / "refused")
    assert "--allow-unpinned-staging" in str(excinfo.value)
    assert not (tmp_path / "refused").exists()

    snapshot._copytree_safe(src, tmp_path / "permitted", allow_unpinned=True)
    assert (tmp_path / "permitted" / "file.txt").read_text(encoding="utf-8") == "x\n"


def test_the_manifest_records_which_traversal_built_the_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reader deciding whether to trust an archive needs the mode on the record.

    Asserted against the platform's own capability rather than a hardcoded "pinned",
    because on a platform that cannot pin the honest value IS "unpinned" -- and a test
    that hardcoded the POSIX answer would fail the Windows shard for being right.
    """
    mc = tmp_path / "home"
    (mc / "workspace").mkdir(parents=True)
    (mc / "workspace" / "note.md").write_text("hi\n", encoding="utf-8")
    out = tmp_path / "snapshots"

    expected = "pinned" if pinned_fs.supports_pinned_tree_walk() else "unpinned"
    native = snapshot._build_snapshot(mc, out, "native-archive", **UNPINNED_OK)
    with tarfile.open(str(native)) as tar:
        member = tar.extractfile("native-archive/MANIFEST.json")
        assert member is not None
        assert json.load(member)["staging"] == expected

    monkeypatch.setattr(pinned_fs, "supports_pinned_tree_walk", lambda: False)
    unpinned = snapshot._build_snapshot(mc, out, "unpinned-archive", allow_unpinned=True)
    with tarfile.open(str(unpinned)) as tar:
        member = tar.extractfile("unpinned-archive/MANIFEST.json")
        assert member is not None
        assert json.load(member)["staging"] == "unpinned"


def test_the_snapshot_cli_reports_a_refusal_instead_of_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A deliberate refusal must read as a decision, with a non-zero exit code."""
    mc = tmp_path / "home"
    (mc / "workspace").mkdir(parents=True)
    (mc / "workspace" / "note.md").write_text("hi\n", encoding="utf-8")
    monkeypatch.setenv("KIROCREW_HOME", str(mc))
    monkeypatch.setattr(pinned_fs, "supports_pinned_tree_walk", lambda: False)

    rc = snapshot.snapshot_main([str(tmp_path / "out")])

    assert rc == 1
    assert "--allow-unpinned-staging" in capsys.readouterr().out
    assert not list((tmp_path / "out").glob("*.tar.gz"))


# ── The destination side is pinned too ───────────────────────────────────────


@pinned_only
def test_the_destination_root_cannot_be_repointed_once_the_walk_holds_it(
    tmp_path: Path,
) -> None:
    """Asserted on where the bytes landed, because an exception cannot prove pinning.

    The first revision of `stage_tree_pinned` pinned only the SOURCE. That was
    defensible while the only destination was a private temporary directory, and wrong
    the moment a restore used it: then the destination IS the live data home, and an
    ancestor swapped there lands the archive's bytes outside it. Review caught it.

    Here the destination root is renamed away and a link to a victim directory is put
    at its old name, from inside the `ignore` callback -- i.e. after the walk has
    opened the destination and before it writes. A pinned destination keeps writing
    into the directory it opened; a by-name one would follow the link.
    """
    src = tmp_path / "source"
    src.mkdir()
    (src / "payload.txt").write_text("REAL\n", encoding="utf-8")

    victim = tmp_path / "victim"
    victim.mkdir()

    dst = tmp_path / "live"
    state = {"done": False}

    def _swap_destination(directory: str, contents: list[str]) -> set[str]:
        if not state["done"]:
            state["done"] = True
            dst.rename(tmp_path / "live-moved")
            _symlink_or_skip(dst, victim)
        return set()

    pinned_fs.stage_tree_pinned(src, dst, what="tree", ignore=_swap_destination)

    assert (tmp_path / "live-moved" / "payload.txt").read_text(
        encoding="utf-8"
    ) == "REAL\n", "the write did not land in the directory the walk had already opened"
    assert not (
        victim / "payload.txt"
    ).exists(), "the write followed the destination link planted after the walk opened it"


# ── The gate runs before anything is staged ──────────────────────────────────


def test_core_files_alone_still_consult_the_platform_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A data home with core files and no trees must not stage unpinned unasked.

    The first revision gated inside `_copytree_safe` only, so the opt-in was consulted
    exactly when a tree happened to exist. A data home holding `crons.json` and no
    `workspace/` staged its core files on a platform that cannot pin without ever
    asking -- the gate was reachable only through a path that might not run. Raised in
    review; the gate now runs once, up front, which is also what makes the manifest's
    `staging` value true of the whole archive.
    """
    mc = tmp_path / "home"
    mc.mkdir()
    (mc / "crons.json").write_text("[]\n", encoding="utf-8")
    monkeypatch.setattr(pinned_fs, "supports_pinned_tree_walk", lambda: False)

    with pytest.raises(pinned_fs.PinnedPathRefusal) as excinfo:
        snapshot._build_snapshot(mc, tmp_path / "out", "archive")
    assert "--allow-unpinned-staging" in str(excinfo.value)
    assert not list((tmp_path / "out").glob("*.tar.gz"))


# ── An incomplete archive says so ────────────────────────────────────────────


def test_the_manifest_records_what_was_omitted(tmp_path: Path) -> None:
    """A skipped file must be recorded, not left as only a console warning.

    That is the same "silent partial" shape this PR fixes on the restore side: exit 0,
    a success message, and an archive quietly missing a file. Raised in review. The
    omission is now in `MANIFEST.json`, and `_print_manifest` shows it so the record
    has a reader that is not "untar the archive by hand".
    """
    mc = tmp_path / "home"
    (mc / "workspace").mkdir(parents=True)
    (mc / "workspace" / "kept.txt").write_text("kept\n", encoding="utf-8")

    secret = tmp_path / "credential"
    secret.write_text("AKIA-not-real\n", encoding="utf-8")
    try:
        os.link(secret, mc / "workspace" / "alias.json")
    except (OSError, NotImplementedError):  # pragma: no cover - platform dependent
        pytest.skip("hard links are unavailable on this host")

    archive = snapshot._build_snapshot(mc, tmp_path / "out", "archive", **UNPINNED_OK)
    with tarfile.open(str(archive)) as tar:
        member = tar.extractfile("archive/MANIFEST.json")
        assert member is not None
        manifest = json.load(member)
        assert tar.getnames().count("archive/workspace/kept.txt") == 1
        assert "archive/workspace/alias.json" not in tar.getnames()

    omitted = {e["path"]: e["reason"] for e in manifest["skipped"]}
    assert omitted == {os.path.join("workspace", "alias.json"): pinned_fs.SKIP_NOT_REGULAR}


# ── The opt-in has to be reachable from the shipped command ──────────────────


def test_the_shipped_cli_accepts_the_opt_in_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """The refusal names a flag, so the flag must exist on the real parser.

    Review found it defined only on the fallback parsers INSIDE `snapshot_main` /
    `restore_main`, which the console script never reaches -- it builds its own
    subparsers in `cli.py` and passes `parsed=`. So a user on a platform that cannot
    pin was refused with a message naming a flag argparse would then reject: a dead
    end, and exactly the "withdraws the command from the platform" outcome the design
    note said it was avoiding.

    Asserted against the real `cli.main` with a control, because a test that only
    checked the flag parses could pass on a parser nobody runs.
    """
    from kiro_crew import cli

    def _run(argv: list[str]) -> object:
        monkeypatch.setattr("sys.argv", ["kirocrew"] + argv)
        try:
            cli.main()
            return "accepted"
        except SystemExit as exc:
            return exc.code

    assert _run(["snapshot", "--list", "--allow-unpinned-staging"]) == "accepted"
    assert _run(["restore", "--list-components", "--allow-unpinned-staging"]) == "accepted"
    assert (
        _run(["snapshot", "--list", "--no-such-flag"]) == 2
    ), "the control passed, so this test would accept a parser that ignores unknown flags"


def test_the_import_path_records_unpinned_staging_instead_of_removing_the_feature(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An import on a platform that cannot pin PROCEEDS, and says so in its summary.

    This test asserted the opposite until CI told me otherwise, and the correction is worth
    keeping: I had the gate refuse here on the reasoning -- written into the code -- that
    "there is no flag to pass, an import is a UI action". The observation was right and the
    conclusion was wrong. Refuse-by-default means "ask the user" only where a consent surface
    EXISTS; where none does it means deleting the feature on that platform, which is not a
    security decision anyone made. Twenty-one pre-existing `test_portability.py` tests failed
    on the Windows shard to make the point.

    So snapshot and restore still refuse -- they have `--allow-unpinned-staging` and can ask --
    and this path records `staging: unpinned` instead. The per-entry screens still apply
    either way: what is given up is ancestor-swap resistance, not link resistance.
    """
    import zipfile

    from kiro_crew import portability

    mc = tmp_path / "home"
    mc.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(mc))
    monkeypatch.setattr(pinned_fs, "supports_pinned_tree_walk", lambda: False)

    payload = tmp_path / "import.zip"
    with zipfile.ZipFile(payload, "w") as zf:
        # One top-level directory is the format's requirement -- a flat zip is rejected
        # before staging is reached, which made an earlier version of this test fail for a
        # reason that had nothing to do with what it was asserting.
        zf.writestr("kirocrew-export/crons.json", "[]\n")
        zf.writestr("kirocrew-export/workspace/note.md", "hi\n")

    summary = portability.apply_import_zip(payload, mode="merge")

    assert summary["staging"] == "unpinned", (
        "an unpinnable import must record that it was staged by name, or the weaker mode "
        "becomes invisible to whoever reads the summary"
    )
    assert (mc / "crons.json").exists(), "the import was refused instead of proceeding"


def test_a_pinnable_import_records_that_it_was_pinned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half: where pinning IS available the summary must say what really happened.

    This asserted `pinned` for a merge until review pointed out that a merge's core files
    (including the out-of-scope databases) and its skills copy are still `shutil` by name --
    so `pinned` was true of the platform and false of the operation. The field exists to tell
    a reader what happened, so a merge on a pinnable host reports `mixed`.

    Without this the field could read one constant everywhere and nothing would notice.
    """
    import zipfile

    from kiro_crew import portability

    if not pinned_fs.supports_pinned_tree_walk():
        pytest.skip("host cannot pin, so there is no pinned case to assert")

    mc = tmp_path / "home"
    mc.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(mc))

    payload = tmp_path / "import.zip"
    with zipfile.ZipFile(payload, "w") as zf:
        zf.writestr("kirocrew-export/crons.json", "[]\n")

    summary = portability.apply_import_zip(payload, mode="merge")
    assert summary["staging"] == "mixed", (
        "a merge routes only its tree copy through the primitive, so claiming a fully "
        "pinned staging overstates what happened"
    )


def test_the_import_path_passes_its_staging_decision_to_both_branches() -> None:
    """The entry decision has to reach the merge AND replace branches, or it is decorative.

    Both inner helpers gate independently, so a permission decided once at entry and then not
    passed down means the inner gate re-asks and refuses -- the same platform outage by a
    longer route, and one that only shows up on the platform that cannot pin. That is exactly
    how the Windows shard failed: I fixed the merge branch first and CI came back red on the
    replace branch.

    Pinned on the source because the failure is invisible on a host that can pin: every
    assertion about it passes locally whether or not the argument is threaded through.
    """
    import inspect

    from kiro_crew import portability

    source = inspect.getsource(portability.apply_import_zip)
    assert (
        "except PinnedPathRefusal" not in source
    ), "the refusal is being swallowed again; a returned summary reads as success"
    assert "rejected_replace" not in source
    assert "_do_replace(snap, mc, None, allow_unpinned=not staging_pinned)" in source, (
        "the replace branch does not receive the entry decision, so it will re-gate and "
        "refuse on a platform that cannot pin"
    )
    assert (
        "_copy_tree_no_overwrite(sd, dd, allow_unpinned=not staging_pinned)" in source
    ), "the merge branch does not receive the entry decision"


# ── A failed copy leaves nothing behind ──────────────────────────────────────


def test_mode_and_mtime_are_applied_through_the_written_descriptor(tmp_path: Path) -> None:
    """`chmod(name, dir_fd=...)` re-resolves the name; `fchmod(fd)` cannot be redirected.

    The metadata calls must not address the destination by name under the pinned
    directory, which leaves a window where the final component is swapped between the
    write and the chmod and the mode lands on the replacement. Asserted here as the
    ordinary-case behaviour the descriptor form has to preserve.
    """
    src = tmp_path / "source.txt"
    src.write_text("payload\n", encoding="utf-8")
    os.chmod(src, 0o640)
    dst = tmp_path / "dest.txt"

    assert pinned_fs.copy_file_pinned(str(src), str(dst)) is True
    if os.name == "posix":
        assert (dst.stat().st_mode & 0o777) == 0o640
    assert dst.stat().st_mtime_ns == src.stat().st_mtime_ns


@pinned_only
def test_a_planted_name_in_a_fresh_destination_tree_is_refused_not_skipped(
    tmp_path: Path,
) -> None:
    """Suppressing `FileExistsError` on mkdir turned a planted link into a silent gap.

    In a destination tree this operation created, a name already occupying a
    subdirectory is a link or a file planted there. The suppressed error let the pinned
    open below refuse the subtree and the restore then reported success with the
    archive's subtree missing -- the same silent-partial shape fixed elsewhere in this
    change. A merge legitimately meets existing directories, so `skip_existing` still
    tolerates them.
    """
    src = tmp_path / "source"
    (src / "sub").mkdir(parents=True)
    (src / "sub" / "file.txt").write_text("payload\n", encoding="utf-8")

    dst = tmp_path / "dest"
    dst.mkdir()
    _symlink_or_skip(dst / "sub", tmp_path / "elsewhere")

    with pytest.raises(pinned_fs.PinnedPathRefusal) as excinfo:
        pinned_fs.stage_tree_pinned(src, dst, what="tree")
    assert "already occupies" in str(excinfo.value)

    # The merge path must still tolerate a real existing directory.
    dst2 = tmp_path / "dest2"
    (dst2 / "sub").mkdir(parents=True)
    pinned_fs.stage_tree_pinned(src, dst2, what="tree", skip_existing=True)
    assert (dst2 / "sub" / "file.txt").read_text(encoding="utf-8") == "payload\n"


@pinned_only
def test_the_destination_root_is_created_through_a_pinned_parent(tmp_path: Path) -> None:
    """Only the final component is created, and only relative to a pinned parent.

    `Path(dst).mkdir(parents=True)` created every missing component BY NAME. Two
    separate properties come out of replacing it, and it is worth being exact about
    which one this buys, because my first version of this test claimed the stronger one
    and passed for the wrong reason:

    1. A missing ancestor is not silently materialised through whatever the path
       resolves to. The parent must already exist, so a caller cannot accidentally
       write a tree into a linked directory it never validated. Asserted below.
    2. An ancestor swapped for a link AFTER the parent was resolved is refused, by
       `pin_parent`'s per-component `O_NOFOLLOW`. That is covered by
       `test_open_dir_pinned_refuses_a_parent_that_became_a_link`.

    What this does NOT buy is refusing an ancestor that was ALREADY a link when the
    parent was resolved: `realpath` follows it, deliberately, because refusing every
    symlinked ancestor would break a destination under `/tmp` on macOS. That residual is
    documented on `pin_parent` and asserted at the end of this test so the limit is on
    the record rather than assumed away.
    """
    src = tmp_path / "source"
    src.mkdir()
    (src / "file.txt").write_text("payload\n", encoding="utf-8")

    # (1) A missing parent is not created by name.
    with pytest.raises((pinned_fs.PinnedPathRefusal, OSError)):
        pinned_fs.stage_tree_pinned(src, tmp_path / "absent" / "deep" / "dest", what="tree")
    assert not (tmp_path / "absent").exists(), "a missing ancestor chain was materialised by name"

    # The documented residual, stated rather than implied: a parent that is already a
    # link is followed by the resolution, so the tree lands in its target.
    victim = tmp_path / "victim"
    victim.mkdir()
    holder = tmp_path / "holder"
    holder.mkdir()
    _symlink_or_skip(holder / "linked", victim)

    pinned_fs.stage_tree_pinned(src, holder / "linked" / "dest", what="tree")
    assert (victim / "dest" / "file.txt").read_text(encoding="utf-8") == "payload\n", (
        "if this now refuses, the pre-existing-link residual has been closed and this "
        "assertion should become the refusal it documents"
    )


# ── The alias hazard reaches the databases too ──────────────────────


def test_merge_consults_the_gate_before_writing_any_core_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The core copies run before any tree call, so the gate has to be at entry.

    Gating inside the tree helpers meant a merge on a platform that cannot pin wrote
    `memory.db`, `crons.json` and the security files FIRST and only then met the refusal
    -- either redirecting those writes through a planted link, or aborting with the
    restore already half applied. Raised in review; same gate-placement defect as the
    snapshot side, one path over.
    """
    snap = tmp_path / "payload"
    snap.mkdir()
    (snap / "crons.json").write_text("[]\n", encoding="utf-8")
    mc = tmp_path / "home"
    mc.mkdir()
    monkeypatch.setattr(pinned_fs, "supports_pinned_tree_walk", lambda: False)

    with pytest.raises(pinned_fs.PinnedPathRefusal):
        snapshot._do_merge(snap, mc, None)

    assert not (
        mc / "crons.json"
    ).exists(), "a core file was written before the platform gate was consulted"


@pinned_only
def test_a_linked_destination_root_raises_the_refusal_not_a_raw_oserror(
    tmp_path: Path,
) -> None:
    """`O_NOFOLLOW` refuses it, and the refusal must not escape as a traceback.

    Every other refusal on this surface is one type the CLI boundary contains. A bare
    `ELOOP`/`ENOTDIR` would end a restore in a stack trace instead of the sentence
    explaining what to remove.
    """
    src = tmp_path / "source"
    src.mkdir()
    (src / "file.txt").write_text("payload\n", encoding="utf-8")

    victim = tmp_path / "victim"
    victim.mkdir()
    holder = tmp_path / "holder"
    holder.mkdir()
    _symlink_or_skip(holder / "dest", victim)

    with pytest.raises(pinned_fs.PinnedPathRefusal) as excinfo:
        pinned_fs.stage_tree_pinned(src, holder / "dest", what="tree")
    assert "symbolic link or not a directory" in str(excinfo.value)


# ── The window between creating a directory and opening it ──────────


def _vanishing_mkdir(
    target: Path, *, once: bool, counter: list[int], plant: Path | None = None
) -> Callable[..., None]:
    """A ``mkdir`` that makes *target* and then removes it again, as a remover would.

    The removal lands exactly where a concurrent one would: after the directory
    exists and before anything has opened it. Deterministic, so the window is a
    test fixture rather than something to reproduce under load. With *plant*, a
    link to that path is left at the name instead of nothing, which is the other
    thing an actor who won the race can do.
    """
    real_mkdir = os.mkdir

    def vanishing(name: object, mode: int = 0o777, *, dir_fd: int | None = None) -> None:
        real_mkdir(name, mode, dir_fd=dir_fd)  # type: ignore[arg-type]
        if name != target.name or (once and counter):
            return
        counter.append(1)
        os.rmdir(name, dir_fd=dir_fd)  # type: ignore[arg-type]
        if plant is not None:
            _symlink_or_skip(target, plant)

    return vanishing


@pinned_only
def test_a_destination_removed_between_its_mkdir_and_its_open_is_re_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Creating a directory and opening it are two syscalls (GH-12043).

    A removal between them is an interleaving no ordering closes, and tolerating
    only the `FileExistsError` half handled a concurrent writer while a concurrent
    remover ended the operation. The pair is re-run, so the caller gets the
    descriptor it asked for.
    """
    parent = tmp_path / "parent"
    parent.mkdir()
    target = parent / "dest"
    removals: list[int] = []
    monkeypatch.setattr(os, "mkdir", _vanishing_mkdir(target, once=True, counter=removals))

    fd = pinned_fs.create_and_open_dir_pinned(target, what="destination")
    try:
        assert removals == [1], "the race did not happen, so the retry is not what passed"
        assert os.fstat(fd).st_ino == target.stat().st_ino
    finally:
        os.close(fd)


@pinned_only
def test_a_destination_removed_on_every_attempt_names_the_whole_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exhaustion reports the PATH, not the bare leaf `openat` was handed.

    `'dest'` on its own reads as a working-directory bug and is not one -- that
    unreadable filename is half of what GH-12034 cost, and it is asserted as the
    contract rather than left to whichever `openat` lost. The attempt count is
    asserted too: this must report rather than spin.
    """
    parent = tmp_path / "parent"
    parent.mkdir()
    target = parent / "dest"
    removals: list[int] = []
    monkeypatch.setattr(os, "mkdir", _vanishing_mkdir(target, once=False, counter=removals))

    with pytest.raises(FileNotFoundError) as excinfo:
        pinned_fs.create_and_open_dir_pinned(target, what="destination")
    assert excinfo.value.filename == str(target)
    assert str(target) in str(excinfo.value)
    assert "was removed" in str(excinfo.value)
    # The exact ceiling, not merely "more than one": an assertion that only read
    # the constant would stay green if the bound were raised to fifty, and a
    # fifty-deep retry on a directory something is deleting is the failure mode
    # the bound exists to prevent.
    assert len(removals) == pinned_fs._CREATE_ATTEMPTS == 3
    assert not target.exists()


@pinned_only
def test_the_retry_does_not_weaken_the_must_create_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An occupied name is still refused, and refused on the FIRST attempt.

    `must_create` exists so that meeting an existing directory is a refusal to
    report, not a condition to work around. A retry that re-asked `mkdir` without
    re-asking this would paper over exactly what the flag is for, so both halves
    are pinned: the refusal type, and that no second attempt is made.
    """
    parent = tmp_path / "parent"
    parent.mkdir()
    target = parent / "dest"
    target.mkdir()
    attempts: list[int] = []
    real_mkdir = os.mkdir

    def counting(name: object, mode: int = 0o777, *, dir_fd: int | None = None) -> None:
        if name == target.name:
            attempts.append(1)
        real_mkdir(name, mode, dir_fd=dir_fd)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "mkdir", counting)

    with pytest.raises(pinned_fs.PinnedPathRefusal) as excinfo:
        pinned_fs.create_and_open_dir_pinned(target, what="destination", must_create=True)
    assert "already exists" in str(excinfo.value)
    assert attempts == [1], "must_create was re-attempted instead of refusing"


@pinned_only
def test_a_link_planted_at_the_name_mid_sequence_is_refused_not_re_attempted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a REMOVAL is re-run. A link that replaces the directory is refused, once.

    This is the property that makes the retry safe: it is reached from
    `FileNotFoundError` alone, so an actor who removes the directory and plants a
    link where it was meets `O_NOFOLLOW` on the first attempt and gets the module's
    refusal, with nothing created inside what the link points at.
    """
    victim = tmp_path / "victim"
    victim.mkdir()
    parent = tmp_path / "parent"
    parent.mkdir()
    target = parent / "dest"
    removals: list[int] = []
    monkeypatch.setattr(
        os, "mkdir", _vanishing_mkdir(target, once=True, counter=removals, plant=victim)
    )

    with pytest.raises(pinned_fs.PinnedPathRefusal) as excinfo:
        pinned_fs.create_and_open_dir_pinned(target, what="destination")
    assert "symbolic link or not a directory" in str(excinfo.value)
    assert removals == [1]
    assert list(victim.iterdir()) == [], "the retry wrote through the planted link"


@pinned_only
def test_a_parent_removed_under_its_pin_is_reported_rather_than_re_attempted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing can be created inside an unlinked directory, so this reports at once.

    The retry is for a directory that was removed after it existed; a pinned PARENT
    that is gone makes every further attempt fail identically, and spending two more
    on it would only delay the same answer.
    """
    parent = tmp_path / "parent"
    parent.mkdir()
    target = parent / "dest"
    attempts: list[int] = []
    real_mkdir = os.mkdir

    def removing_the_parent(name: object, mode: int = 0o777, *, dir_fd: int | None = None) -> None:
        if name == target.name:
            attempts.append(1)
            os.rmdir(parent)
        real_mkdir(name, mode, dir_fd=dir_fd)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "mkdir", removing_the_parent)

    with pytest.raises(FileNotFoundError) as excinfo:
        pinned_fs.create_and_open_dir_pinned(target, what="destination")
    assert excinfo.value.filename == str(target)
    assert attempts == [1]


@pinned_only
def test_a_parent_that_never_existed_is_not_reported_as_a_removal(tmp_path: Path) -> None:
    """The removal report is a claim about what happened, so it is not made loosely.

    A parent that simply is not there fails in `pin_parent`, above the sequence, and
    keeps its own error -- otherwise every ordinary mistyped path would read as a
    concurrent removal.
    """
    with pytest.raises(FileNotFoundError) as excinfo:
        pinned_fs.create_and_open_dir_pinned(tmp_path / "absent" / "dest", what="destination")
    assert "was removed" not in str(excinfo.value)


@pinned_only
def test_a_destination_this_call_only_MET_is_never_re_created_when_it_vanishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The retry covers a directory this call made. A met one is reported instead.

    Found by review. When `mkdir` raises `FileExistsError` the destination was already
    there holding the caller's files, so re-creating it after a removal hands back an
    EMPTY directory -- and a merge (`must_create=False`) then stages the archive into
    it and reports success while the files it was merging with are gone for good. The
    contents are unrecoverable either way; what this pins is that the loss is reported
    rather than hidden behind a fresh directory.
    """
    parent = tmp_path / "parent"
    parent.mkdir()
    target = parent / "dest"
    target.mkdir()
    (target / "pre-existing.txt").write_text("the user's file\n", encoding="utf-8")
    attempts: list[int] = []
    real_mkdir = os.mkdir

    def removing_the_met_directory(
        name: object, mode: int = 0o777, *, dir_fd: int | None = None
    ) -> None:
        if name == target.name:
            attempts.append(1)
            try:
                real_mkdir(name, mode, dir_fd=dir_fd)
            finally:
                # Whatever mkdir answered, the directory is gone by the time the open
                # runs: the removal lands in the window, taking the file with it.
                if target.exists():
                    (target / "pre-existing.txt").unlink()
                    target.rmdir()
            return
        real_mkdir(name, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "mkdir", removing_the_met_directory)

    with pytest.raises(FileNotFoundError) as excinfo:
        pinned_fs.create_and_open_dir_pinned(target, what="destination")
    assert excinfo.value.filename == str(target)
    assert "already existed when this call met it" in str(excinfo.value)
    assert attempts == [1], "a met destination's removal was re-attempted"
    assert not target.exists(), "the met destination was re-created empty"


@pinned_only
def test_a_merge_whose_destination_vanishes_mid_create_fails_instead_of_reporting_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The product-level shape of the case above, through `stage_tree_pinned`.

    A merge passes `must_create=False` because meeting an existing destination is what
    merging IS. If that destination is removed inside the create-then-open window, the
    files the merge was adding to are gone; the one thing that must not happen is the
    archive landing in a fresh empty directory and the merge returning normally, which
    would report success over unrecoverable loss.
    """
    src = tmp_path / "source"
    src.mkdir()
    (src / "from-archive.txt").write_text("archive\n", encoding="utf-8")

    dst = tmp_path / "live"
    dst.mkdir()
    (dst / "only-on-disk.txt").write_text("not in the archive\n", encoding="utf-8")
    real_mkdir = os.mkdir
    removals: list[int] = []

    def removing_the_live_tree(
        name: object, mode: int = 0o777, *, dir_fd: int | None = None
    ) -> None:
        # ONCE, and only for the destination this merge MET. A wrapper that removed the
        # directory on every attempt would make the call fail by exhaustion whatever the
        # code does, and would assert nothing: the discriminating run is the one where a
        # retry WOULD succeed, into a directory recreated after the live tree was lost.
        if name == dst.name and not removals:
            removals.append(1)
            try:
                real_mkdir(name, mode, dir_fd=dir_fd)
            finally:
                if dst.exists():
                    (dst / "only-on-disk.txt").unlink()
                    dst.rmdir()
            return
        real_mkdir(name, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "mkdir", removing_the_live_tree)

    with pytest.raises(OSError):
        pinned_fs.stage_tree_pinned(src, dst, what="tree", skip_existing=True)
    assert removals == [1]
    assert not (dst / "from-archive.txt").exists(), (
        "the merge staged the archive into a directory recreated after the live tree "
        "was removed, which reports success over the loss"
    )


def test_metadata_falls_back_to_a_path_where_fd_operations_do_not_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`os.fchmod` does not exist on Windows, and calling it unconditionally crashed.

    An earlier revision applied metadata only by descriptor, so a Windows snapshot using
    the declared by-name traversal raised `AttributeError` the moment it reached a core
    file -- a crash introduced by a hardening change, which is the worst kind. Simulated
    by deleting the attribute, the way the bench suite simulates a missing `O_NOFOLLOW`.
    """
    monkeypatch.delattr(pinned_fs.os, "fchmod", raising=False)
    monkeypatch.setattr(pinned_fs.os, "supports_fd", set())

    src = tmp_path / "source.txt"
    src.write_text("payload\n", encoding="utf-8")
    os.chmod(src, 0o640)
    dst = tmp_path / "dest.txt"

    assert pinned_fs.copy_file_pinned(str(src), str(dst)) is True
    assert dst.read_text(encoding="utf-8") == "payload\n"
    if os.name == "posix":
        assert (dst.stat().st_mode & 0o777) == 0o640


def test_a_restored_security_file_is_locked_down_regardless_of_the_archive_mode(
    tmp_path: Path,
) -> None:
    """The archive is untrusted input, so its recorded mode cannot decide the result.

    The reviewer's suggested fix for the by-name lockdown was to drop it because "the
    copy already applies mode through the descriptor" -- but that applies the ARCHIVE's
    mode, and a tarball can be built by hand with any mode it likes. So the mode is
    forced through the descriptor instead of inherited or re-applied by name.

    The payload is `0o644` rather than something wider: group- and world-readable is
    already a real leak for a secret, so it proves the point, and it avoids asking a SAST
    rule to tell a fixture apart from a mistake.
    """
    snap = tmp_path / "payload"
    snap.mkdir()
    salt = snap / "telemetry_salt"
    salt.write_text("salt\n", encoding="utf-8")
    os.chmod(salt, 0o644)

    mc = tmp_path / "home"
    mc.mkdir()
    backup = mc / "backup"
    backup.mkdir()

    snapshot._backup_and_copy(mc, backup, snap, "security", **UNPINNED_OK)

    restored = mc / "telemetry_salt"
    assert restored.read_text(encoding="utf-8") == "salt\n"
    if os.name == "posix":
        assert (
            restored.stat().st_mode & 0o777
        ) == 0o600, "the archive's permissive mode was inherited onto a restored secret"
