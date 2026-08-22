"""A merge that dies partway must leave nothing, and a URL is escaped before printing."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from kiro_crew import snapshot as snap


class TestAFailedMergeCopyLeavesNoTruncatedTarget:
    """The merge skips existing targets, so a partial one would never be retried."""

    def test_a_copy_that_fails_partway_leaves_the_target_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        src = tmp_path / "src"
        src.mkdir()
        (src / "memory.md").write_text("the real content\n", encoding="utf-8")
        dst = tmp_path / "dst"
        dst.mkdir()

        real_copy = shutil.copy2

        def dies_partway(a, b, *args, **kw):
            Path(b).write_text("trunc", encoding="utf-8")  # a partial write landed
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(snap.shutil, "copy2", dies_partway)

        with pytest.raises(OSError):
            snap._copy_tree_no_overwrite(src, dst, tmp_path)

        assert not (dst / "memory.md").exists(), (
            "a truncated target survived, and the merge skips existing targets so no "
            "retry would ever replace it"
        )
        assert list(dst.iterdir()) == [], f"scratch left behind: {list(dst.iterdir())}"

        # And the retry, once the disk is fine, completes properly.
        monkeypatch.setattr(snap.shutil, "copy2", real_copy)
        snap._copy_tree_no_overwrite(src, dst, tmp_path)
        assert (dst / "memory.md").read_text(encoding="utf-8") == "the real content\n"

    def test_an_existing_target_is_still_not_overwritten(self, tmp_path: Path) -> None:
        """Atomicity must not turn a no-overwrite merge into an overwriting one."""
        src = tmp_path / "src"
        src.mkdir()
        (src / "keep.md").write_text("from the bundle\n", encoding="utf-8")
        dst = tmp_path / "dst"
        dst.mkdir()
        (dst / "keep.md").write_text("mine, already here\n", encoding="utf-8")

        snap._copy_tree_no_overwrite(src, dst, tmp_path)

        assert (dst / "keep.md").read_text(encoding="utf-8") == "mine, already here\n"

    def test_the_copy_is_staged_then_linked_into_place(self) -> None:
        """Atomicity was only half of what this write owes.

        A rename is atomic and also unconditional, so staging plus `os.replace` traded a
        truncated file for a destroyed one: anything appearing between the existence test
        and the rename is overwritten by a mode whose whole contract is that it adds only
        what is missing. Linking into the same directory is atomic AND refuses when the
        target exists, so this asserts the link rather than the rename.
        """
        import inspect

        src = inspect.getsource(snap._copy_tree_no_overwrite)
        code = "\n".join(line for line in src.splitlines() if not line.lstrip().startswith("#"))
        assert "os.link(" in code, "the merge write is not atomic"
        assert "os.replace(" not in code, "an unconditional rename can destroy live data"
        copy_at = code.index("shutil.copy2(")
        assert code.index("os.link(") > copy_at
        assert "mkstemp(" in code, "a derived staging name can collide with the operator's"


class TestTheStagingFileCannotClobberOperatorData:
    """Making the write atomic must not introduce a second way to lose a file."""

    def test_a_file_the_operator_already_owns_is_untouched(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        (src / "note.md").write_text("from the bundle\n", encoding="utf-8")
        dst = tmp_path / "dst"
        dst.mkdir()
        squatter = dst / ".note.md.partial"
        squatter.write_text("MINE -- do not touch\n", encoding="utf-8")

        snap._copy_tree_no_overwrite(src, dst, tmp_path)

        assert squatter.read_text(encoding="utf-8") == "MINE -- do not touch\n"
        assert (dst / "note.md").read_text(encoding="utf-8") == "from the bundle\n"

    def test_it_does_not_write_through_a_link_of_a_guessable_name(self, tmp_path: Path) -> None:
        """A derived staging name the operator owns as a LINK escapes the tree entirely."""
        outside = tmp_path / "outside.txt"
        outside.write_text("must not be overwritten\n", encoding="utf-8")
        src = tmp_path / "src"
        src.mkdir()
        (src / "note.md").write_text("from the bundle\n", encoding="utf-8")
        dst = tmp_path / "dst"
        dst.mkdir()
        (dst / ".note.md.partial").symlink_to(outside)

        snap._copy_tree_no_overwrite(src, dst, tmp_path)

        assert (
            outside.read_text(encoding="utf-8") == "must not be overwritten\n"
        ), "the staging write followed a link and left the destination tree"

    def test_no_staging_file_survives_a_successful_merge(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        (src / "a.md").write_text("x\n", encoding="utf-8")
        dst = tmp_path / "dst"
        dst.mkdir()

        snap._copy_tree_no_overwrite(src, dst, tmp_path)

        assert sorted(p.name for p in dst.iterdir()) == ["a.md"]

    def test_staging_happens_in_the_destination_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The rename is only atomic within one filesystem.

        Staging in the system temp dir works on a host where it shares a device with the
        data home and raises a cross-device error where it does not — so the decision is
        observed directly rather than through a side effect this host cannot reproduce.
        """
        src = tmp_path / "src"
        src.mkdir()
        (src / "a.md").write_text("x\n", encoding="utf-8")
        dst = tmp_path / "dst"
        dst.mkdir()

        seen: list[str | None] = []
        real_mkstemp = snap.tempfile.mkstemp

        def recording(*args, **kw):
            seen.append(kw.get("dir"))
            return real_mkstemp(*args, **kw)

        monkeypatch.setattr(snap.tempfile, "mkstemp", recording)
        snap._copy_tree_no_overwrite(src, dst, tmp_path)

        assert seen == [
            str(dst)
        ], f"staging must sit beside the target for the rename to be atomic: {seen}"


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
        import inspect

        src = inspect.getsource(snap.restore_main)
        assert (
            "_safe_name(str(args.snapshot))" in src
        ), "the caller-supplied snapshot argument is printed unescaped"
