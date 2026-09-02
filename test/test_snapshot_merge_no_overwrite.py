"""A merge never overwrites, never follows a link into the destination, and a URL is
escaped before printing.

Retargeted for M1: `_copy_tree_no_overwrite` no longer stages a `.partial` file and
publishes it with `os.link` -- that design was proven exploitable and removed (see
`pinned_fs.copy_file_pinned`'s docstring). It now writes each file straight to its final
name with `O_CREAT|O_EXCL|O_NOFOLLOW`, so the properties below are asserted against that
primitive's observable behaviour rather than the deleted staging mechanism.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest
from test_snapshot import unpinnable_argv

from kiro_crew import pinned_fs
from kiro_crew import snapshot as snap


class TestAFailedMergeCopyLeavesNoTruncatedTarget:
    """The merge skips existing targets, so a target it wrote must be the real bytes."""

    def test_a_failed_write_does_not_masquerade_as_a_completed_merge(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If a per-file copy raises, the merge must not report success over the gap.

        The exclusive-create write has no publish step to interrupt, so the failure the
        old `.partial` design guarded against is gone; what still matters is that a copy
        error propagates rather than being swallowed, so nothing downstream treats the
        absent file as merged. Once the transient condition clears, a retry completes.
        """
        src = tmp_path / "src"
        src.mkdir()
        (src / "memory.md").write_text("the real content\n", encoding="utf-8")
        dst = tmp_path / "dst"
        dst.mkdir()

        real = pinned_fs.copy_file_pinned
        calls = {"n": 0}

        def flaky(*a, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError(28, "No space left on device")
            return real(*a, **kw)

        monkeypatch.setattr(snap.pinned_fs, "copy_file_pinned", flaky)
        with pytest.raises(OSError):
            snap._copy_tree_no_overwrite(src, dst, allow_unpinned=bool(unpinnable_argv()))

        # The retry, once the disk is fine, completes properly and the target holds the
        # real bytes -- not a truncated remnant of the failed attempt.
        monkeypatch.setattr(snap.pinned_fs, "copy_file_pinned", real)
        snap._copy_tree_no_overwrite(src, dst, allow_unpinned=bool(unpinnable_argv()))
        assert (dst / "memory.md").read_text(encoding="utf-8") == "the real content\n"

    def test_an_existing_target_is_still_not_overwritten(self, tmp_path: Path) -> None:
        """No-overwrite is the promise: a target already present is left as-is."""
        src = tmp_path / "src"
        src.mkdir()
        (src / "keep.md").write_text("from the bundle\n", encoding="utf-8")
        dst = tmp_path / "dst"
        dst.mkdir()
        (dst / "keep.md").write_text("mine, already here\n", encoding="utf-8")

        snap._copy_tree_no_overwrite(src, dst, allow_unpinned=bool(unpinnable_argv()))

        assert (dst / "keep.md").read_text(encoding="utf-8") == "mine, already here\n"

    def test_the_write_is_exclusive_create_not_an_unconditional_rename(self) -> None:
        """Atomicity must not be bought with an overwriting publish.

        An `os.replace` (or an `os.link` publish of a `.partial`) is atomic AND
        unconditional, so it would trade a truncated file for a destroyed one in a mode
        whose whole contract is that it adds only what is missing. The primitive instead
        opens the destination `O_CREAT|O_EXCL`, which refuses when the target exists, so
        this pins the exclusive create and the absence of an unconditional publish.
        """
        code = "\n".join(
            line
            for line in inspect.getsource(snap._copy_tree_no_overwrite).splitlines()
            if not line.lstrip().startswith("#")
        )
        assert "skip_existing=True" in code, "the merge write is not the no-overwrite form"
        assert "os.replace(" not in code, "an unconditional rename can destroy live data"
        assert "os.link(" not in code, "the exploitable link-publish design must not return"
        prim = inspect.getsource(pinned_fs.copy_file_pinned)
        assert "O_EXCL" in prim, "the destination is not created exclusively"


class TestTheMergeDoesNotWriteThroughALink:
    """Making the write refuse a present target must not open a way to escape the tree."""

    def test_it_does_not_write_through_a_link_planted_at_the_target_name(
        self, tmp_path: Path
    ) -> None:
        """A destination name pre-occupied by a LINK must not have bytes written through it.

        `O_NOFOLLOW` on the exclusive create refuses the link outright rather than
        following it to whatever it points at outside the tree.
        """
        outside = tmp_path / "outside.txt"
        outside.write_text("must not be overwritten\n", encoding="utf-8")
        src = tmp_path / "src"
        src.mkdir()
        (src / "note.md").write_text("from the bundle\n", encoding="utf-8")
        dst = tmp_path / "dst"
        dst.mkdir()
        (dst / "note.md").symlink_to(outside)

        snap._copy_tree_no_overwrite(src, dst, allow_unpinned=bool(unpinnable_argv()))

        assert (
            outside.read_text(encoding="utf-8") == "must not be overwritten\n"
        ), "the merge write followed a link and left the destination tree"

    def test_no_scratch_file_survives_a_successful_merge(self, tmp_path: Path) -> None:
        """The write is direct-to-final, so a completed merge leaves only the real files."""
        src = tmp_path / "src"
        src.mkdir()
        (src / "a.md").write_text("x\n", encoding="utf-8")
        dst = tmp_path / "dst"
        dst.mkdir()

        snap._copy_tree_no_overwrite(src, dst, allow_unpinned=bool(unpinnable_argv()))

        assert sorted(p.name for p in dst.iterdir()) == ["a.md"]


class TestTheDownloadBannerIsEscaped:
    def test_a_control_byte_in_the_url_does_not_reach_the_terminal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        """It is printed before the URL has been validated, so it is escaped first."""
        hostile = "s3://bucket/\x1b[2Jkey.tar.gz"

        monkeypatch.setattr(snap, "_default_snapshot_dir", lambda: str(tmp_path))
        rc = snap.restore_main([hostile, "--force"])

        out = capsys.readouterr().out
        assert "\x1b" not in out, "a raw escape sequence reached the terminal"
        assert rc != 0 or "Downloading" in out

    def test_the_banner_routes_through_the_escaper(self) -> None:
        src = inspect.getsource(snap.restore_main)
        assert (
            "_safe_name(str(args.snapshot))" in src
        ), "the caller-supplied snapshot argument is printed unescaped"
