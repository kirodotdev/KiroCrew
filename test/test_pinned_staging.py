"""The descriptor-pinned staging primitive, and snapshot/restore's use of it.

Written as an A/B suite rather than a list of assertions. The point of this change is
that a pinned traversal is strictly stronger than a by-name one, and the only honest
way to show that is to run the SAME attack against both paths and watch one leak while
the other refuses -- an exception-only assertion cannot tell "pinned" from "got lucky".

The mid-walk swap is performed inside the ``ignore`` callback, which both paths invoke
once per directory with that directory's contents. That is the check-to-use window
made deterministic: the screen has just looked at the entry and declared it fine, and
the copy of that entry has not happened yet.
"""

from __future__ import annotations

import errno
import json
import os
import tarfile
from pathlib import Path

import pytest

from kiro_crew import pinned_fs, snapshot

pinned_only = pytest.mark.skipif(
    not pinned_fs.supports_pinned_tree_walk(),
    reason="requires O_DIRECTORY, O_NOFOLLOW, dir_fd and fd-listdir support (POSIX)",
)

#: Windows implements only the read-only bit, so an exact permission-bit assertion is a
#: POSIX statement. Everything else in these tests (content, mtime, which file exists)
#: is platform-neutral and stays asserted everywhere.
posix_modes_only = pytest.mark.skipif(
    os.name != "posix", reason="permission bits are POSIX; Windows has only a read-only flag"
)

#: Staging refuses by default where a directory cannot be pinned, so a test that drives
#: a real snapshot or restore has to say which platform contract it is asserting. Tests
#: that assert product behaviour true on BOTH platforms pass this and exercise the
#: declared by-name path on Windows; tests that assert the pinning itself use
#: `pinned_only` instead.
UNPINNED_OK = {"allow_unpinned": True}


def _symlink_or_skip(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):  # pragma: no cover - platform dependent
        pytest.skip("creating a symlink needs privilege on this host")


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


# ── The restore side ─────────────────────────────────────────────────────────


@pinned_only
def test_restore_moves_a_symlinked_core_file_aside_instead_of_writing_through_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """#3797's third finding, plus the silent partial restore hiding behind it.

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
    """A skipped file used to be a console warning and nothing else.

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

    The metadata calls used to address the destination by name under the pinned
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

    1. A missing ancestor is no longer silently materialised through whatever the path
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


# ── Round 3: the alias hazard reached the databases too ──────────────────────


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
    """`O_NOFOLLOW` already refused it; the gap was that it escaped as a traceback.

    Every other refusal on this surface is one type the CLI boundary contains, and this
    one arrived as a bare `ELOOP`/`ENOTDIR`, so a restore ended in a stack trace instead
    of the sentence explaining what to remove. Found by my own probe of a review finding,
    then named by the reviewer.
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


# ── Round 5: skips that precede a delete, and a validation that gets discarded ──


def test_replace_refuses_rather_than_deleting_a_tree_its_backup_could_not_copy(
    tmp_path: Path,
) -> None:
    """The data-loss finding that closed the earlier attempt (#2446), carried forward here.

    Replace mode's next step after the backup is `rmtree` on the LIVE tree. The staging
    walk legitimately skips a hardlink alias -- right when producing an archive, and
    catastrophic here, because the skip is followed by a delete rather than by an
    omission, so the only copy of that file goes with it. Refusing before the delete is
    the only ordering that cannot lose data.
    """
    mc = tmp_path / "home"
    live = mc / "workspace"
    live.mkdir(parents=True)
    (live / "ordinary.txt").write_text("kept\n", encoding="utf-8")

    secret = tmp_path / "credential"
    secret.write_text("token\n", encoding="utf-8")
    try:
        os.link(secret, live / "alias.json")
    except (OSError, NotImplementedError):  # pragma: no cover - platform dependent
        pytest.skip("hard links are unavailable on this host")

    snap = tmp_path / "payload"
    (snap / "workspace").mkdir(parents=True)
    (snap / "workspace" / "from-archive.txt").write_text("new\n", encoding="utf-8")

    with pytest.raises(pinned_fs.PinnedPathRefusal) as excinfo:
        snapshot._do_replace(snap, mc, ["workspace"], **UNPINNED_OK)

    assert "already moved or removed" in str(excinfo.value)
    assert (live / "alias.json").exists(), "the live tree was deleted despite a skipped file"
    assert (live / "ordinary.txt").read_text(encoding="utf-8") == "kept\n"


@pinned_only
def test_the_replace_path_refuses_a_root_recreated_after_its_own_rmtree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The WIRING, not just the primitive: replace must pass the contract down.

    A unit test on `stage_tree_pinned(must_create=True)` does not prove the replace path
    asks for it. It did not: dropping `must_create=True` at the call site left every test
    green, so the flag could be removed and nothing would notice -- the same untested-wiring
    gap that lets a fix silently rot. This drives the real `_do_replace`.

    The race is reproduced at its outcome rather than by timing: `rmtree` is replaced with
    one that removes the tree and immediately recreates the bare root, which is exactly the
    state a gateway recreating its workspace leaves behind.
    """
    mc = tmp_path / "home"
    live = mc / "workspace"
    live.mkdir(parents=True)
    (live / "stale.txt").write_text("not in the archive\n", encoding="utf-8")

    snap = tmp_path / "payload"
    (snap / "workspace").mkdir(parents=True)
    (snap / "workspace" / "from-archive.txt").write_text("new\n", encoding="utf-8")

    real_rmtree = snapshot.shutil.rmtree

    def _rmtree_then_recreate(path, *a, **k):
        real_rmtree(path, *a, **k)
        os.mkdir(path)  # the gateway comes back before the copy starts

    monkeypatch.setattr(snapshot.shutil, "rmtree", _rmtree_then_recreate)

    # The refusal must SURFACE with its message intact -- that message is what proves
    # `must_create=True` reached the call site, which is this test's whole point.
    #
    # It may arrive wrapped. A refusal mid-mutation now triggers the phase-two rollback, and
    # this test's own `rmtree` patch sabotages the recovery leg too (it recreates the root the
    # recovery is about to refill), so recovery legitimately cannot complete and the refusal
    # is re-raised as its `cause`. Accepting either form keeps the wiring assertion exactly as
    # strong -- the message is still required -- without asserting that a mid-mutation refusal
    # must SKIP the rollback, which would be the opposite of correct.
    with pytest.raises((pinned_fs.PinnedPathRefusal, snapshot.RollbackIncomplete)) as excinfo:
        snapshot._do_replace(snap, mc, ["workspace"], **UNPINNED_OK)

    refusal = getattr(excinfo.value, "cause", excinfo.value)
    assert isinstance(
        refusal, pinned_fs.PinnedPathRefusal
    ), f"not a pin refusal: {type(refusal).__name__}: {refusal}"
    assert "already exists" in str(refusal), f"unclear refusal: {refusal}"
    assert not (live / "from-archive.txt").exists(), (
        "the archive was staged into a root somebody else recreated, so a replace would "
        "have reported success with foreign files still in place"
    )


@pinned_only
def test_a_destination_subtree_that_cannot_be_opened_refuses_instead_of_vanishing(
    tmp_path: Path,
) -> None:
    """A source entry that stops being a directory is skipped; a destination is not.

    The archive's subtree still exists and now has nowhere to go, so continuing reported
    success with that subtree missing -- the silent-partial shape again, on the one path
    where `_open_child_dir` returning None was being treated as benign. A merge is the
    one caller that may legitimately meet a foreign destination tree, so it keeps the
    skip.
    """
    src = tmp_path / "source"
    (src / "sub").mkdir(parents=True)
    (src / "sub" / "file.txt").write_text("payload\n", encoding="utf-8")

    dst = tmp_path / "dest"
    dst.mkdir()
    # A plain FILE where the walk needs a directory: mkdir raises FileExistsError and the
    # pinned open then fails with ENOTDIR, which is the state under test.
    (dst / "sub").write_text("not a directory\n", encoding="utf-8")

    with pytest.raises(pinned_fs.PinnedPathRefusal):
        pinned_fs.stage_tree_pinned(src, dst, what="tree")


@pinned_only
def test_a_restore_source_that_cannot_be_copied_refuses_after_the_live_file_moved(
    tmp_path: Path,
) -> None:
    """Third instance of one rule, which is why it became a reporter rather than a check.

    The live file is moved aside first, so a skipped restore SOURCE finishes with the
    original gone and the archive's version never written -- active data missing, reported
    as success. Same shape as the backup-then-rmtree case and the destination-subtree
    case; all three now come from `pinned_fs.fatal_skip_reporter`, passed by every
    mutating call site and deliberately not by any archive-producing one.
    """
    snap = tmp_path / "payload"
    snap.mkdir()
    secret = tmp_path / "credential"
    secret.write_text("token\n", encoding="utf-8")
    try:
        os.link(secret, snap / "crons.json")
    except (OSError, NotImplementedError):  # pragma: no cover - platform dependent
        pytest.skip("hard links are unavailable on this host")

    mc = tmp_path / "home"
    mc.mkdir()
    (mc / "crons.json").write_text("live\n", encoding="utf-8")
    backup = mc / "backup"
    backup.mkdir()

    with pytest.raises(pinned_fs.PinnedPathRefusal) as excinfo:
        snapshot._backup_and_copy(mc, backup, snap, "crons")

    assert "already moved or removed" in str(excinfo.value)
    assert (backup / "crons.json").read_text(
        encoding="utf-8"
    ) == "live\n", "the live file must still be recoverable from the backup after the refusal"


def test_a_crafted_archive_cannot_drive_the_terminal_through_the_manifest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A restore reads an arbitrary tarball, so its manifest strings are untrusted input.

    ANSI and OSC sequences are executed by a terminal rather than displayed, so a crafted
    archive could rewrite what the operator appears to be reading -- including hiding the
    very omission notice this change added in order to make an incomplete archive visible.
    Raised in review.

    Control characters are escaped rather than stripped so the value stays diagnosable:
    the operator sees that the name really does contain an escape, instead of reading a
    different, innocuous name.
    """
    snap = tmp_path / "extracted"
    snap.mkdir()
    (snap / "MANIFEST.json").write_text(
        json.dumps(
            {
                "version": 2,
                "created_at": "2026-01-01T00:00:00Z\x1b[2J",
                "user": "someone\x1b]0;pwned\x07",
                "hostname": "host",
                "contents": {},
                "skipped": [{"reason": "not_regular\x1b[31m", "path": "a\x1b[1;31mb.txt"}],
            }
        ),
        encoding="utf-8",
    )

    snapshot._print_manifest(snap)
    out = capsys.readouterr().out

    assert "\x1b" not in out, "an escape sequence from the archive reached the terminal"
    assert "\\x1b" in out, "the escape was stripped rather than shown, hiding the tampering"
    assert "b.txt" in out, "the ordinary part of the name is still readable"


@pinned_only
def test_an_unusable_archive_member_does_not_cost_the_live_file(tmp_path: Path) -> None:
    """The archive is untrusted, so a member that is not a regular file is a real case.

    The old order moved the live file aside FIRST and only then found the source unusable,
    so the original ended up in the backup, nothing was restored, and the command reported
    success. A FIFO at a core filename is the cheapest way to produce that; a directory or
    a device node does the same.
    """
    snap = tmp_path / "payload"
    snap.mkdir()
    try:
        os.mkfifo(snap / "crons.json")
    except (AttributeError, OSError, NotImplementedError):  # pragma: no cover - platform
        pytest.skip("mkfifo is unavailable on this host")

    mc = tmp_path / "home"
    mc.mkdir()
    (mc / "crons.json").write_text("live\n", encoding="utf-8")
    backup = mc / "backup"
    backup.mkdir()

    snapshot._backup_and_copy(mc, backup, snap, "crons")

    assert (mc / "crons.json").read_text(
        encoding="utf-8"
    ) == "live\n", (
        "the live file was moved aside for an archive member that could never be restored"
    )
    assert not (backup / "crons.json").exists()


def test_a_fifo_source_is_refused_rather_than_blocking_forever(tmp_path: Path) -> None:
    """Opening a FIFO for reading blocks until a writer appears -- with no timeout.

    So a single named pipe in an extracted archive, or planted in a staged tree, could hang
    a whole snapshot or restore indefinitely, with no message. This was found the honest
    way: a mutation probe removed a caller's own `is_file()` guard and the test run stalled
    until a watchdog killed it, which is exactly how an operator would have met it.

    A caller-side pre-check is not enough on its own -- every caller would have to remember
    it -- so `O_NONBLOCK` makes reaching the `fstat` refusal guaranteed rather than
    dependent on the call site. Asserted with a real FIFO and no writer: if this ever
    regresses, the test hangs rather than fails, which is why the primitive and not just
    the caller has to hold the property.
    """
    if not hasattr(os, "mkfifo"):  # pragma: no cover - platform dependent
        pytest.skip("mkfifo is unavailable on this host")
    fifo = tmp_path / "pipe"
    try:
        os.mkfifo(fifo)
    except (OSError, NotImplementedError):  # pragma: no cover - platform dependent
        pytest.skip("mkfifo is unavailable on this host")

    seen: list[tuple[str, str]] = []
    copied = pinned_fs.copy_file_pinned(
        str(fifo), str(tmp_path / "dest"), on_skip=lambda r, p: seen.append((r, p))
    )

    assert copied is False
    assert seen and seen[0][0] == pinned_fs.SKIP_NOT_REGULAR
    assert not (tmp_path / "dest").exists()


def test_the_declared_by_name_path_still_refuses_a_hardlinked_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Opting into a by-name TRAVERSAL is not opting into dereferencing an alias.

    The operator accepts one specific weakness on a platform that cannot pin: an ancestor
    swapped mid-walk can redirect the copy, and nothing can prevent that without the
    descriptor support the platform lacks. Review pointed out the per-file screens had been
    left behind with it -- plain `copytree` dereferences a hardlink into ordinary bytes --
    so the fallback would have archived a credential the pinned path refuses. That is a
    wider concession than the documented one.
    """
    monkeypatch.setattr(pinned_fs, "supports_pinned_tree_walk", lambda: False)

    src = tmp_path / "workspace"
    src.mkdir()
    (src / "ordinary.txt").write_text("kept\n", encoding="utf-8")
    secret = tmp_path / "credential"
    secret.write_text("AKIA-not-real\n", encoding="utf-8")
    try:
        os.link(secret, src / "innocuous.json")
    except (OSError, NotImplementedError):  # pragma: no cover - platform dependent
        pytest.skip("hard links are unavailable on this host")

    dst = tmp_path / "staged"
    snapshot._copytree_safe(src, dst, allow_unpinned=True)

    assert (dst / "ordinary.txt").read_text(encoding="utf-8") == "kept\n"
    assert not (
        dst / "innocuous.json"
    ).exists(), "the by-name fallback dereferenced a hardlinked credential into the archive"


def test_the_by_name_core_path_refuses_a_linked_core_filename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`is_file()` and an O_NOFOLLOW-less open both FOLLOW a link, so neither screens one."""
    monkeypatch.setattr(pinned_fs, "supports_pinned_tree_walk", lambda: False)

    mc = tmp_path / "home"
    mc.mkdir()
    secret = tmp_path / "credential"
    secret.write_text("AKIA-not-real\n", encoding="utf-8")
    _symlink_or_skip(mc / "crons.json", secret)

    archive = snapshot._build_snapshot(mc, tmp_path / "out", "archive", **UNPINNED_OK)
    with tarfile.open(str(archive)) as tar:
        assert (
            "archive/crons.json" not in tar.getnames()
        ), "the by-name core path followed a link and archived the credential"
        member = tar.extractfile("archive/MANIFEST.json")
        assert member is not None
        assert any(e["path"] == "crons.json" for e in json.load(member)["skipped"])


def test_the_by_name_restore_applies_the_archive_over_a_symlinked_core_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The round-1 fix reached the pinned branch and not this one.

    The by-name branch still skipped both the backup AND the replacement for a symlinked
    core name, so the archive's file was never applied while the command reported success --
    the same silent partial restore fixed on the pinned path much earlier. Review caught the
    branch that had been left behind.
    """
    monkeypatch.setattr(pinned_fs, "supports_pinned_tree_walk", lambda: False)

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

    snapshot._backup_and_copy(mc, backup, snap, "crons", **UNPINNED_OK)

    assert (mc / "crons.json").read_text(
        encoding="utf-8"
    ) == "[]\n", "the archive's version was not applied -- a silent partial restore"
    assert victim.read_text(encoding="utf-8") == "PRECIOUS\n"
    assert (backup / "crons.json").is_symlink()


@pinned_only
def test_staged_directories_do_not_inherit_the_sources_mode_or_mtime(tmp_path: Path) -> None:
    """Directory metadata is deliberately NOT carried across, and this pins that choice.

    The earlier contract was the opposite: `shutil.copytree` preserved directory mode and
    mtime, the walk that replaced it did not, and that lost fidelity was restored after review
    found it. It has now been given up again, on purpose, for a reason that outranks fidelity.

    Applying it required knowing that WE created the directory. That flag came from the
    `mkdir` while the descriptor came from a separate `open`, so a directory replaced between
    the two left the flag true for an object we no longer held, and the archive's mode and
    mtime were then written onto a directory belonging to somebody else. There is no sound
    repair: a stat taken after the mkdir observes the replacement just as happily as our own
    directory, POSIX has no atomic create-and-open for a directory, and under a same-user
    threat model no mode trick closes the gap.

    So a staged tree gets the walk's own 0700 and a current mtime. Contents and file metadata
    are unaffected -- those are applied through a descriptor that IS the file.
    """
    src = tmp_path / "source"
    nested = src / "nested"
    nested.mkdir(parents=True)
    (nested / "file.txt").write_text("payload\n", encoding="utf-8")
    # Non-default DIRECTORY modes, so "not inherited" is distinguishable from "coincidence".
    # Semgrep's advice here is for files and would make a directory non-traversable, so it is
    # suppressed by rule id rather than widened.
    # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
    os.chmod(nested, 0o750)
    # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
    os.chmod(src, 0o755)
    old_mtime = 1600000000_000000000
    os.utime(nested, ns=(old_mtime, old_mtime))
    os.utime(src, ns=(old_mtime, old_mtime))

    dst = tmp_path / "staged"
    pinned_fs.stage_tree_pinned(src, dst, what="tree")

    # The payload and its own metadata still cross -- only directories are affected.
    assert (dst / "nested" / "file.txt").read_text(encoding="utf-8") == "payload\n"
    assert (
        dst / "nested"
    ).stat().st_mode & 0o777 == 0o700, (
        "a directory inherited the source mode, which review showed cannot be done safely"
    )
    assert dst.stat().st_mode & 0o777 == 0o700, "the root inherited the source mode"
    assert (
        dst / "nested"
    ).stat().st_mtime_ns != old_mtime, "a directory inherited the source mtime"


@pinned_only
@posix_modes_only
def test_a_merge_does_not_restamp_an_existing_directory(tmp_path: Path) -> None:
    """The archive's directory mode must not land on a directory the user already owns.

    Preserving directory metadata (previous round) was right for directories this walk
    CREATES and wrong for one a merge finds already there: a live 0700 directory had the
    archive's 0755 stamped onto it, which clobbers the user's metadata and loosens
    permissions from an untrusted source. Review caught it one round after the fix that
    introduced it.

    Not a new rule -- it is the same reason a security file is forced to 0600 instead of
    taking the archive's mode. That rule was applied to files and not carried to directories.
    """
    src = tmp_path / "source"
    shared = src / "shared"
    shared.mkdir(parents=True)
    (shared / "from-archive.txt").write_text("new\n", encoding="utf-8")
    # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
    os.chmod(shared, 0o755)

    dst = tmp_path / "live"
    live_shared = dst / "shared"
    live_shared.mkdir(parents=True)
    (live_shared / "already-here.txt").write_text("mine\n", encoding="utf-8")
    # 0o700 is OWNER-ONLY -- the tightest mode a directory can have and still be entered.
    # The rule fires on the execute bit, which every directory needs, so it flags even this;
    # its suggested 0o644 would grant world-read AND make the directory non-traversable.
    # Same suppression and same reasoning as the existing sites in `mcp_gateway/rewriter.py`.
    # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
    os.chmod(live_shared, 0o700)
    # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
    os.chmod(dst, 0o700)

    pinned_fs.stage_tree_pinned(src, dst, what="merge", skip_existing=True)

    assert (live_shared / "already-here.txt").read_text(encoding="utf-8") == "mine\n"
    assert (live_shared / "from-archive.txt").read_text(encoding="utf-8") == "new\n"
    assert (
        live_shared.stat().st_mode & 0o777 == 0o700
    ), "the archive's mode was stamped onto a live directory, loosening it to 0755"
    assert dst.stat().st_mode & 0o777 == 0o700, "the merge restamped the destination root"


@pinned_only
@posix_modes_only
def test_a_root_created_between_the_check_and_the_mkdir_is_not_restamped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The "is this root mine?" answer has to come from the kernel, not from a name.

    `not dst.exists()` was a check with a window after it: a forced restore removes the
    workspace, the gateway recreates it before the mkdir runs, and the live directory is then
    treated as newly created and stamped with the archive's mode and timestamps. Review caught
    it. `FileExistsError` on our own mkdir cannot race -- the kernel decided it.

    The fixture recreates the root inside that window, which is the one thing a name-based
    check cannot survive and a kernel-reported one shrugs off.
    """
    src = tmp_path / "source"
    src.mkdir()
    (src / "file.txt").write_text("payload\n", encoding="utf-8")
    # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
    os.chmod(src, 0o755)

    dst = tmp_path / "live"
    real_pin = pinned_fs.pin_parent

    def _recreate_the_root_first(*a, **k):
        fd = real_pin(*a, **k)
        # Only in the DESTINATION's window. An earlier version fired on the SOURCE root's
        # pin, which happens first, so the root already existed by the time the old
        # name-based check sampled it -- and the mutation that restored the bug passed.
        # A fixture that fires outside the window under test proves nothing.
        if "destination" in str(k.get("what", "")) and not dst.exists():
            dst.mkdir()  # the gateway, in the window a name check leaves open
            # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
            os.chmod(dst, 0o700)
        return fd

    monkeypatch.setattr(pinned_fs, "pin_parent", _recreate_the_root_first)
    pinned_fs.stage_tree_pinned(src, dst, what="tree")

    assert (dst / "file.txt").read_text(encoding="utf-8") == "payload\n"
    assert (
        dst.stat().st_mode & 0o777 == 0o700
    ), "a root recreated inside the window was stamped with the archive's mode"


def test_no_name_based_filesystem_question_where_a_descriptor_is_held() -> None:
    """The ratchet for this change's first invariant, enforced instead of just asserted.

    Five separate review findings across thirteen rounds were the same mistake: a filesystem
    question asked through a PATH NAME by code already holding a descriptor for the object. A
    name re-resolves, so the guard can inspect a different object than the one the next line
    acts on -- and every instance reported success while omitting or misplacing data. Point-
    fixing a sixth time is the wrong move, so this walks the AST and fails on the pattern.

    Scoped to `pinned_fs` ON PURPOSE. There the invariant is absolute: the module exists to be
    the descriptor-pinned path and has no by-name fallback, so any name-based question inside a
    descriptor-holding function is a defect.

    `snapshot.py` is deliberately NOT covered, and the first draft of this test that did cover
    it is why. It flagged twelve sites, and on inspection nearly all were the UNPINNED
    fallback branches -- where a by-name check is the only thing available and is correct.
    Whole-function scoping cannot tell those from the real thing, so covering `snapshot.py`
    needs a per-line marker convention for the legitimate by-name sites. That is a wider change
    than the two fixes this test guards, and it belongs in the follow-up gate I proposed on this
    PR rather than being smuggled in here.
    """
    import ast

    repo_root = Path(snapshot.__file__).resolve().parents[2]
    # Questions, plus the PUBLICATION writes. The question set alone is what shipped, and it
    # is why a publish that re-resolved its temporary by `os.link` went unflagged for three
    # rounds while satisfying this very ratchet -- the invariant covers writes derived from a
    # name, and only the branch-controlling half was ever enforced. `link`/`rename`/`replace`
    # are added because a name-resolved publish is precisely the hole that shipped.
    #
    # `chmod`/`utime`/`unlink` are deliberately NOT here: those have legitimate by-name
    # fallbacks for platforms without `os.fchmod`, and separating them needs the helper-split
    # refactor applied to the metadata appliers. Measured, that is 5 sites -- worth doing, and
    # not worth dragging into this change. They stay outside the ratchet, stated rather than
    # silently omitted.
    banned_attrs = {
        "is_file",
        "is_dir",
        "exists",
        "is_symlink",
        "stat",
        "lstat",
        "link",
        "rename",
        "replace",
    }
    offenders: list[str] = []

    for module in ("src/kiro_crew/pinned_fs.py",):
        path = repo_root / module
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for func in ast.walk(tree):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            # Does this function bind a directory descriptor at all?
            names = {
                n.id
                for n in ast.walk(func)
                if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Param))
            }
            names |= {a.arg for a in func.args.args + func.args.kwonlyargs}
            holds_fd = any(n.endswith("_fd") for n in names)
            if not holds_fd:
                continue
            for node in ast.walk(func):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                if not isinstance(fn, ast.Attribute) or fn.attr not in banned_attrs:
                    continue
                # os.stat(name, dir_fd=...) is the CORRECT form -- that is the fix, not
                # the defect. Only flag the descriptor-less question. `rename`/`link` name
                # their descriptors differently, so all three spellings are exempt or the
                # ratchet would flag the very form it exists to require.
                if any(kw.arg in ("dir_fd", "src_dir_fd", "dst_dir_fd") for kw in node.keywords):
                    continue
                # Reading a descriptor's own metadata is by definition not name-based.
                if isinstance(fn.value, ast.Name) and fn.value.id.endswith("_fd"):
                    continue
                # An `os.DirEntry` yielded by `os.scandir(<fd>)` is also not a name-based
                # question, and this is the second naming convention the ratchet reads
                # (`_fd` is the first): a receiver called `entry`, or ending `_entry`, must
                # be one. CPython keeps the iterator's DESCRIPTOR on each entry and stats
                # through it -- `entry.path` is the bare name and the stat still answers
                # from a working directory the name does not exist in, which
                # `test_a_direntry_from_a_descriptor_scan_stats_through_it` pins rather
                # than leaving as a comment. The alternative form the ratchet would accept,
                # `os.stat(entry.name, dir_fd=fd)`, asks the kernel the same question and
                # costs one extra syscall per entry on trees with six figures of them.
                if isinstance(fn.value, ast.Name) and (
                    fn.value.id == "entry" or fn.value.id.endswith("_entry")
                ):
                    continue
                offenders.append(f"{module}:{node.lineno} -> .{fn.attr}()")

    assert not offenders, (
        "a filesystem question asked by NAME inside a function holding a descriptor -- "
        "use pinned_fs.stat_at / is_regular_at, or pass dir_fd=:\n  " + "\n  ".join(offenders)
    )


@pinned_only
def test_the_pinned_core_loop_screens_through_the_descriptor(tmp_path: Path) -> None:
    """A core filename pointing at a credential is refused and recorded, not archived.

    NOTE ON WHAT THIS TEST DOES AND DOES NOT PROVE. It asserts the behaviour, but it does NOT
    discriminate the descriptor-relative guard it was written for: removing that guard leaves
    this test green, because `copy_file_pinned` opens with O_NOFOLLOW and refuses the link on
    its own. I verified that with a mutation and am recording it rather than presenting the
    test as evidence it is not.

    The guard is still right -- with `mc_fd` in hand, asking a NAME is a second source of
    truth that can disagree with the descriptor, and the disagreement direction that matters
    is a name-based guard rejecting a healthy file and silently dropping it from a successful
    archive. That direction needs a swap injected between guard and copy through a symlinked
    ancestor alias, which I have not built. So: hardening with a behavioural test, not a
    regression test, and labelled as such.

    Also worth recording how the defect was found: my own AST ratchet flagged this exact line,
    and I dismissed it as one of the legitimate by-name fallback sites without checking. Review
    then found it for real. The ratchet was right; the generalization I applied to its output
    was not.
    """
    mc = tmp_path / "home"
    mc.mkdir()
    secret = tmp_path / "credential"
    secret.write_text("AKIA-not-real\n", encoding="utf-8")
    _symlink_or_skip(mc / "crons.json", secret)
    (mc / "config.json").write_text("{}\n", encoding="utf-8")

    archive = snapshot._build_snapshot(mc, tmp_path / "out", "archive")

    with tarfile.open(str(archive)) as tar:
        names = tar.getnames()
        assert "archive/crons.json" not in names, "a linked core name reached the archive"
        assert "archive/config.json" in names, "the healthy core file was not archived"
        member = tar.extractfile("archive/MANIFEST.json")
        assert member is not None
        manifest = json.load(member)

    assert any(
        e["path"] == "crons.json" for e in manifest["skipped"]
    ), "the refusal was not recorded in the manifest"
    # An ABSENT core file is not an omission and must not appear -- most components ship
    # only a subset, and recording every missing name would make the list meaningless.
    assert not any(e["path"] == "session_map.json" for e in manifest["skipped"])


def test_a_staging_refusal_is_recorded_as_a_rejected_permission_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refusing to stage is a permission decision, so the SEL log has to show it.

    The refusal path this change introduced printed a message and returned, leaving no audit
    record -- so the one outcome a reviewer most wants evidence of, staging declined on an
    unsupported platform, was invisible after the fact. Review caught it. The restore side
    reuses the `state_restore_rejected` event already established in this module rather than
    inventing a second name for the same decision.
    """
    events: list[tuple[str, str]] = []
    monkeypatch.setattr(snapshot, "_audit", lambda ev, res: events.append((ev, res)))
    # Force the platform gate to refuse without the opt-in, which is the real refusal path.
    monkeypatch.setattr(pinned_fs, "supports_pinned_tree_walk", lambda: False)
    monkeypatch.setattr(pinned_fs, "supports_pinned_walk", lambda: False)

    mc = tmp_path / "home"
    mc.mkdir()
    (mc / "config.json").write_text("{}\n", encoding="utf-8")

    monkeypatch.setattr(snapshot, "_mc_dir", lambda: mc)

    rc = snapshot.snapshot_main([str(tmp_path / "out")])

    assert rc == 1, "the refusal did not reach the caller as a failure"
    assert any(
        ev == "snapshot_rejected" for ev, _ in events
    ), f"the refusal left no SEL record: {events}"
    assert any(
        "unpinnable_staging" in res for _, res in events
    ), f"the audit record did not say WHY it was refused: {events}"


def test_a_skipped_by_name_replacement_fails_loudly_instead_of_losing_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once the live file is moved aside, a SKIP is data loss, not an omission.

    The by-name restore branch moves the live core file into the backup and then copies the
    archive's version over the name. If that copy is refused -- a hardlinked source, say --
    a non-fatal reporter records the skip and the restore reports success with the live file
    gone and nothing put back. Review caught this call site still using the non-fatal
    reporter.

    This is the third site to need `fatal_skip_reporter`, and the rule behind all three is
    the same: a skip is CORRECT while producing an archive and is data loss on any path that
    has already moved or deleted the original.
    """
    monkeypatch.setattr(pinned_fs, "supports_pinned_tree_walk", lambda: False)

    snap = tmp_path / "payload"
    snap.mkdir()
    (snap / "crons.json").write_text("[]\n", encoding="utf-8")
    # A hardlink alias on the SOURCE is what the copy primitive refuses.
    alias_target = tmp_path / "other.json"
    alias_target.write_text("[]\n", encoding="utf-8")
    (snap / "crons.json").unlink()
    try:
        os.link(alias_target, snap / "crons.json")
    except OSError:  # pragma: no cover - platform dependent
        pytest.skip("host cannot create a hard link")

    mc = tmp_path / "home"
    mc.mkdir()
    (mc / "crons.json").write_text("LIVE\n", encoding="utf-8")
    backup = mc / "backup"
    backup.mkdir()

    with pytest.raises(Exception) as caught:
        snapshot._backup_and_copy(mc, backup, snap, "crons", **UNPINNED_OK)

    assert "restore of" in str(
        caught.value
    ), f"the refusal did not name the operation it aborted: {caught.value}"
    # The live copy must still be recoverable from the backup rather than simply gone.
    assert (backup / "crons.json").read_text(
        encoding="utf-8"
    ) == "LIVE\n", "the live file was neither restored nor left recoverable in the backup"


@pinned_only
def test_failed_copy_cleanup_spares_a_concurrently_replaced_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed copy must remove OUR partial, never whatever the name means by then.

    The cleanup unlinked by name unconditionally. A name is not a file: a concurrent atomic
    save (write temp, rename over the name) landing between our exclusive create and the
    cleanup meant we deleted the REPLACEMENT. On the restore path the destination is the
    live data home, so that other writer is the gateway losing a committed file. Review
    found it.

    The fixture performs exactly that replacement at the moment the copy fails, and asserts
    the replacement survives with its own bytes.
    """
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "f.txt").write_text("source\n", encoding="utf-8")
    dst_dir = tmp_path / "dst"
    dst_dir.mkdir()

    replacement = tmp_path / "committed.txt"
    replacement.write_text("ANOTHER WRITER\n", encoding="utf-8")

    real_copyfileobj = pinned_fs.shutil.copyfileobj

    def _fail_after_replacing(fsrc, fdst, *a, **k):
        # Someone else's atomic save lands on the destination name, then our copy fails.
        os.replace(str(replacement), str(dst_dir / "f.txt"))
        raise OSError("copy failed")

    monkeypatch.setattr(pinned_fs.shutil, "copyfileobj", _fail_after_replacing)
    with pytest.raises(OSError):
        pinned_fs.copy_file_pinned(str(src_dir / "f.txt"), str(dst_dir / "f.txt"))
    monkeypatch.setattr(pinned_fs.shutil, "copyfileobj", real_copyfileobj)

    survivor = dst_dir / "f.txt"
    assert survivor.exists(), "cleanup deleted the other writer's committed file"
    assert survivor.read_text(encoding="utf-8") == "ANOTHER WRITER\n"


@pinned_only
def test_failed_copy_cleanup_spares_a_replaced_destination_in_descriptor_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same contract on the PINNED path, which is a separate helper.

    The two modes are deliberately separate functions so the descriptor one never names a
    path, so each needs its own coverage -- a mutation to one leaves the other's test green,
    which is how I found this test missing.
    """
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "f.txt").write_text("source\n", encoding="utf-8")
    dst_dir = tmp_path / "dst"
    dst_dir.mkdir()

    replacement = tmp_path / "committed.txt"
    replacement.write_text("ANOTHER WRITER\n", encoding="utf-8")

    src_fd = os.open(str(src_dir), os.O_RDONLY)
    dst_fd = os.open(str(dst_dir), os.O_RDONLY)
    try:

        def _fail_after_replacing(fsrc, fdst, *a, **k):
            os.replace(str(replacement), str(dst_dir / "f.txt"))
            raise OSError("copy failed")

        monkeypatch.setattr(pinned_fs.shutil, "copyfileobj", _fail_after_replacing)
        with pytest.raises(OSError):
            pinned_fs.copy_file_pinned(
                str(src_dir / "f.txt"),
                str(dst_dir / "f.txt"),
                dir_fd=src_fd,
                name="f.txt",
                dst_dir_fd=dst_fd,
                dst_name="f.txt",
            )
    finally:
        os.close(src_fd)
        os.close(dst_fd)

    survivor = dst_dir / "f.txt"
    assert survivor.exists(), "pinned cleanup deleted the other writer's committed file"
    assert survivor.read_text(encoding="utf-8") == "ANOTHER WRITER\n"


@pinned_only
def test_a_published_file_has_exactly_one_link(tmp_path: Path) -> None:
    """A published file has exactly one link, which the next pass depends on.

    This began as a check that the publish removed its temporary. There is no temporary now,
    but the assertion it ended on is still load-bearing and for a reason worth keeping: the
    hardlink screen refuses a source with `st_nlink != 1`, so a destination left with two
    links would be refused by a LATER snapshot of the same tree. Exactly one link is the
    property, however it is achieved.
    """
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "f.txt").write_text("payload\n", encoding="utf-8")
    dst_dir = tmp_path / "dst"
    dst_dir.mkdir()

    assert pinned_fs.copy_file_pinned(str(src_dir / "f.txt"), str(dst_dir / "f.txt")) is True
    names = sorted(p.name for p in dst_dir.iterdir())
    assert names == ["f.txt"], f"unexpected residue: {names}"
    assert (dst_dir / "f.txt").read_text(encoding="utf-8") == "payload\n"
    assert (
        dst_dir / "f.txt"
    ).stat().st_nlink == 1, "a second link would make a later snapshot refuse this file"


@pinned_only
def test_a_destination_collision_surfaces_as_a_refusal_not_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A name taken mid-walk is a real condition, but it must not escape as a traceback.

    `restore --force` while the gateway recreates a tree file made the publish raise
    `FileExistsError` straight out of the walk, so the command died with a stack trace
    instead of the sentence saying what to do -- the same escape ELOOP/ENOTDIR were already
    translated for on this surface. Review found it.
    """
    src = tmp_path / "source"
    src.mkdir()
    (src / "f.txt").write_text("payload\n", encoding="utf-8")
    dst = tmp_path / "dest"
    dst.mkdir()
    # The name is taken BEFORE the walk reaches it. This was previously reproduced by
    # creating the name mid-copy, in the window between the copy and the publish -- that
    # window no longer exists now that the destination itself is opened O_EXCL up front, so
    # occupying the name first is the only way the test still proves anything.
    (dst / "f.txt").write_text("SOMEONE ELSE\n", encoding="utf-8")

    with pytest.raises(pinned_fs.PinnedPathRefusal) as caught:
        pinned_fs.stage_tree_pinned(src, dst, what="tree")

    assert "created by something else" in str(
        caught.value
    ), f"the refusal does not explain what happened: {caught.value}"
    assert (dst / "f.txt").read_text(
        encoding="utf-8"
    ) == "SOMEONE ELSE\n", "the colliding file was overwritten instead of refused"


@pinned_only
def test_a_failed_copy_empties_its_destination_and_never_unlinks_a_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed copy leaves an EMPTIED destination -- it must not unlink the name.

    This contract changed deliberately and the reason is the point. Unlinking the destination
    on failure is what review rejected twice: the name can change between deciding to remove
    it and removing it, POSIX has no unlink-by-inode, and the loser of that race is a file
    another writer published. Publishing through a temporary avoided the unlink but moved the
    problem into the publish, where the temporary's NAME is re-resolved -- proven exploitable.

    So the failure path empties the file through the descriptor it already holds, the one
    operation that cannot be redirected at somebody else's file, and leaves the entry. The
    caller's reporter is what makes it visible; on a restore that reporter is fatal, so the
    operation stops with the previous version still in the backup.
    """
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "f.txt").write_text("this will not survive the copy\n", encoding="utf-8")
    dst_dir = tmp_path / "dst"
    dst_dir.mkdir()

    unlinked: list[str] = []
    real_unlink = pinned_fs.os.unlink

    def _record_unlink(path, *a, **k):
        unlinked.append(str(path))
        return real_unlink(path, *a, **k)

    def _fail_midway(fsrc, fdst, *a, **k):
        fdst.write(b"partial")
        raise OSError(errno.EIO, "input/output error")

    monkeypatch.setattr(pinned_fs.os, "unlink", _record_unlink)
    monkeypatch.setattr(pinned_fs.shutil, "copyfileobj", _fail_midway)
    with pytest.raises(OSError):
        pinned_fs.copy_file_pinned(str(src_dir / "f.txt"), str(dst_dir / "f.txt"))

    target = dst_dir / "f.txt"
    assert target.exists(), "the destination entry was removed, which is the rejected design"
    assert (
        target.stat().st_size == 0
    ), f"the partial content survived: {target.stat().st_size} bytes"
    assert not unlinked, f"the failure path unlinked a name: {unlinked}"


@pinned_only
def test_nothing_names_the_destination_again_once_it_is_open(tmp_path: Path) -> None:
    """The point of the redesign: there is no second name resolution left to attack.

    The temp-and-publish design copied correctly and then re-resolved the temporary by name in
    order to publish it, so swapping that entry after the copy installed attacker bytes and
    reported the core file as restored. Rather than assert the absence of one exploit, this
    asserts the property that makes it impossible: exactly one entry is created, it is the
    destination, and no intermediate sibling ever appears.
    """
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "f.txt").write_text("ours\n", encoding="utf-8")
    dst_dir = tmp_path / "dst"
    dst_dir.mkdir()

    copied = pinned_fs.copy_file_pinned(str(src_dir / "f.txt"), str(dst_dir / "f.txt"))

    assert copied is True
    names = sorted(p.name for p in dst_dir.iterdir())
    assert names == ["f.txt"], f"an intermediate name was created: {names}"
    assert (dst_dir / "f.txt").read_text(encoding="utf-8") == "ours\n"


@pinned_only
def test_a_replace_refuses_a_destination_root_that_came_back(tmp_path: Path) -> None:
    """A replace that finds its root already there must refuse, not merge into it.

    The replace path removes the live tree and then stages the archive into its place. The
    walk used to accept a root that existed again, so if the gateway recreated it in that
    window, files the archive does not contain survived a REPLACE that reported success --
    the silent-partial shape this change exists to remove, in the one mode that promises the
    destination will be exactly the archive. Review found it. The per-child names already
    refused; only the root was exempt.

    The flag is passed by the caller and deliberately NOT derived from `skip_existing`: the
    snapshot's own staging destination already exists and has no `skip_existing` either, so
    deriving it refused every snapshot -- caught by the pre-existing snapshot suite.
    """
    src = tmp_path / "archive"
    src.mkdir()
    (src / "kept.txt").write_text("from the archive\n", encoding="utf-8")

    dst = tmp_path / "live"
    dst.mkdir()
    (dst / "stale.txt").write_text("not in the archive\n", encoding="utf-8")

    with pytest.raises(pinned_fs.PinnedPathRefusal) as caught:
        pinned_fs.stage_tree_pinned(src, dst, what="replace", must_create=True)

    assert "already exists" in str(caught.value), f"unclear refusal: {caught.value}"
    assert (dst / "stale.txt").exists(), "the refusal should not have touched anything"
    assert not (dst / "kept.txt").exists(), "it staged despite refusing"

    # The merge and staging callers must be unaffected -- they meet an existing root by
    # design, and refusing there would break every snapshot.
    pinned_fs.stage_tree_pinned(src, dst, what="merge", skip_existing=True)
    assert (dst / "kept.txt").read_text(encoding="utf-8") == "from the archive\n"
    assert (dst / "stale.txt").exists(), "a merge must not remove what it did not bring"


def test_the_unpinned_replace_also_refuses_a_recreated_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The by-name branch must honour the replace contract too -- this is the Windows path.

    Deliberately NOT `pinned_only`: it exercises the fallback that runs where a descriptor
    cannot pin a directory, which is exactly where the gap was. `must_create` reached the
    pinned walk and stopped there, so `shutil.copytree(dirs_exist_ok=True)` still merged into
    a root recreated after the rmtree and stale files survived a successful replace.

    This is the second time in this PR a guard was added to the pinned path and not carried
    to the by-name one -- the first cost 21 pre-existing Windows tests -- so the assertion
    here is that both platforms refuse with the same type and the same explanation.
    """
    src = tmp_path / "archive"
    src.mkdir()
    (src / "kept.txt").write_text("from the archive\n", encoding="utf-8")

    dst = tmp_path / "live"
    dst.mkdir()
    (dst / "stale.txt").write_text("not in the archive\n", encoding="utf-8")

    # Force the by-name branch regardless of what this host can do.
    monkeypatch.setattr(snapshot.pinned_fs, "supports_pinned_tree_walk", lambda: False)

    with pytest.raises(pinned_fs.PinnedPathRefusal) as caught:
        snapshot._copytree_safe(src, dst, must_create=True, **UNPINNED_OK)

    assert "already exists" in str(caught.value), f"unclear refusal: {caught.value}"
    assert not (dst / "kept.txt").exists(), "the unpinned branch staged despite refusing"
    assert (dst / "stale.txt").exists(), "the refusal should not have touched anything"

    # And without the flag the same branch still merges, which is what snapshot staging and
    # merge callers rely on.
    snapshot._copytree_safe(src, dst, **UNPINNED_OK)
    assert (dst / "kept.txt").read_text(encoding="utf-8") == "from the archive\n"


def test_the_import_audit_line_records_the_staging_mode() -> None:
    """The staging mode must reach the audit trail, not only the JSON response.

    Review found that nothing renders `summary["staging"]`, so the field added to stop the
    import overstating itself was visible to no one. Whether an import was pinned, mixed or
    unpinned is a security property of the operation, so it is recorded where security
    properties live. Asserted on the source because the handler needs a full dashboard
    request to run, and what matters here is that the value is passed at all.
    """
    import inspect

    from kiro_crew.dashboard.handlers import portability as handler

    source = inspect.getsource(handler)
    assert "staging={summary.get('staging'" in source, (
        "the import audit line does not record the staging mode, so the field has no "
        "consumer and can drift without anyone noticing"
    )


# ── The helper's own contract ────────────────────────────────────────────────


def test_an_unknown_copytree_keyword_is_rejected_rather_than_dropped(tmp_path: Path) -> None:
    """``dirs_exist_ok`` is absorbed on purpose; anything else is a caller bug.

    Silently swallowing ``**kwargs`` is how a staging call would keep compiling after
    the flag it depends on stopped being honoured.
    """
    src = tmp_path / "source"
    src.mkdir()
    with pytest.raises(TypeError, match="unexpected keyword"):
        snapshot._copytree_safe(src, tmp_path / "dst", symlinks=True)


def test_the_tree_walk_capability_probe_names_a_function_that_supports_dir_fd() -> None:
    """Pins the bug that made every POSIX snapshot refuse before it was caught.

    ``os.lstat`` is NOT a member of ``os.supports_dir_fd`` even where the pinned walk
    works perfectly -- the capability belongs to ``os.stat``. Probing the wrong one
    reports False on a fully capable platform, which turns the deliberate refusal into
    a blanket outage. Asserted as a property of the stdlib so the probe cannot drift
    back.

    The membership assertions are guarded because `os.supports_dir_fd` is EMPTY on Windows,
    where "which function carries the capability" is not a meaningful question -- nothing
    does. Asserting it unguarded made this test fail on the Windows shard, which is the
    platform whose refusal path the rest of this change is careful about. Caught by CI, not
    locally: this was the first run in which every Windows shard concluded before I pushed
    a new head over it.
    """
    if os.name != "posix":
        assert os.supports_dir_fd == set() or os.stat not in os.supports_dir_fd
        assert pinned_fs.supports_pinned_tree_walk() is False
        return

    assert os.stat in os.supports_dir_fd
    assert os.lstat not in os.supports_dir_fd
    if pinned_fs.supports_pinned_walk():
        assert pinned_fs.supports_pinned_tree_walk() is True


class TestFdRealPathHome:
    """``fd_real_path`` is defined in pinned_fs and every consumer binds THAT object.

    The primitive has cross-module consumers (hooks, sandbox, the app UI route,
    spec_builder), and its containment guarantees — including the Windows
    fail-closed branch — belong with the other descriptor-pinned mechanisms.
    Pinning the location keeps a refactor from quietly forking the
    implementation into a consumer module.
    """

    def test_defined_in_pinned_fs_and_exported(self):
        assert pinned_fs.fd_real_path.__module__ == "kiro_crew.pinned_fs"
        assert "fd_real_path" in pinned_fs.__all__

    def test_every_consumer_binds_the_pinned_fs_object(self):
        from kiro_crew import hooks, sandbox
        from kiro_crew.apps import routes as app_routes
        from kiro_crew.apps.builtins.spec_builder.backend import repository as spec_repository

        assert hooks._fd_real_path is pinned_fs.fd_real_path
        assert sandbox.fd_real_path is pinned_fs.fd_real_path
        assert app_routes.fd_real_path is pinned_fs.fd_real_path
        # spec_builder's descriptor-pinned reads live in repository.py (the
        # handlers refactor moved them out of routes.py).
        assert spec_repository.fd_real_path is pinned_fs.fd_real_path

    def test_resolves_an_open_descriptor_to_its_real_path(self, tmp_path: Path):
        target = tmp_path / "witness.txt"
        target.write_text("x")
        fd = os.open(target, os.O_RDONLY)
        try:
            got = pinned_fs.fd_real_path(fd)
        finally:
            os.close(fd)
        assert got is not None
        assert os.path.normcase(os.path.normpath(got)) == os.path.normcase(os.path.realpath(target))

    def test_fails_closed_on_a_dead_descriptor(self):
        # A descriptor number that cannot be open (far above any real fd
        # table): every platform route must answer None, never a guess. A
        # freshly-closed real fd would race reuse by another thread in the
        # test process, so the impossible number is the deterministic probe.
        assert pinned_fs.fd_real_path(2**30) is None
