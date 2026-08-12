"""Tests for the cycle-7 review findings.

What survives here is the restore-side behaviour the off-host removal did not touch: the
merge pass refusing to write through a broken destination link, and the archive-name
escaping that keeps a hostile bundle from driving the operator's terminal.
"""

from __future__ import annotations

import os

import pytest
from test_snapshot import unpinnable_argv

from kiro_crew import snapshot as snap


class TestABrokenDestinationLinkDoesNotAbortAMerge:
    @pytest.mark.skipif(
        os.name == "nt",
        reason="'refused not climbed past' is the descriptor-pinned destination walk "
        "opening each ancestor by fd; the by-name fallback this platform is forced onto "
        "runs mkdir(parents=True) straight over the dangling link and aborts with "
        "FileExistsError, so the not-climbed-past guarantee does not exist without dir_fd",
    )
    def test_a_dangling_link_target_is_refused_not_climbed_past(self, tmp_path):
        """`exists()` FOLLOWS links, so a broken symlink answers False and an ancestor
        climb steps straight past it — then mkdir(parents=True) meets the dangling link
        and raises FileExistsError, after the databases have already been replaced."""
        home = tmp_path / "home"
        dst = home / "workspace" / "memory"
        dst.mkdir(parents=True)
        try:
            (dst / "history").symlink_to(tmp_path / "does-not-exist", target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("cannot create a symlink on this platform")

        src = tmp_path / "snap" / "workspace" / "memory"
        (src / "history").mkdir(parents=True)
        (src / "history" / "note.md").write_text("incoming")
        (src / "top.md").write_text("fine")

        # Must not raise. `_copy_tree_no_overwrite` no longer takes a `home` argument:
        # the destination chain is pinned descriptor-by-descriptor by the shared
        # primitive, so containment is enforced by the pinned walk rather than by a
        # home passed in here.
        snap._copy_tree_no_overwrite(src, dst)
        assert (dst / "top.md").is_file(), "the merge aborted instead of skipping"
        assert not (tmp_path / "does-not-exist").exists(), "it wrote through the link"

    def test_a_healthy_nested_directory_still_merges(self, tmp_path):
        home = tmp_path / "home"
        dst = home / "workspace" / "memory"
        (dst / "history").mkdir(parents=True)
        src = tmp_path / "snap" / "workspace" / "memory"
        (src / "history").mkdir(parents=True)
        (src / "history" / "note.md").write_text("incoming")
        snap._copy_tree_no_overwrite(src, dst, allow_unpinned=bool(unpinnable_argv()))
        assert (dst / "history" / "note.md").is_file()


class TestArchiveNamesAreSanitizedBeforeReachingATerminal:
    """A restore prints names that came out of an untrusted archive, so the escaping
    that used to guard S3 object keys now guards manifest fields and member names, as
    the body of ``snap._safe_name``."""

    def test_control_bytes_are_escaped(self):
        raw = "backups/host/\x1b[2Jsnap.tar.gz"
        out = snap._safe_name(raw)
        assert "\x1b" not in out
        assert "\\x1b" in out
        assert "snap.tar.gz" in out, "the value should stay recognisable"

    def test_a_long_name_is_capped(self):
        out = snap._safe_name("a" * 5000)
        assert len(out) < 400
        assert "truncated" in out

    def test_ordinary_names_are_untouched(self):
        key = "backups/workstation/kirocrew-snapshot-20260811T000000Z.tar.gz"
        assert snap._safe_name(key) == key

    def test_bidi_overrides_are_escaped(self):
        """A glyphless reordering control survives a control-BYTE filter untouched.

        U+202E renders the tail that follows it reversed, so the name the operator reads
        is not the name they would be restoring.
        """
        raw = "backups/host/\u202egz.rat.pans/live"
        out = snap._safe_name(raw)
        assert "\u202e" not in out
        assert "\\u202e" in out
        assert "backups/host/" in out, "the value should stay recognisable"

    def test_every_embedding_override_and_isolate_is_escaped(self):
        for ch in "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069":
            out = snap._safe_name(f"backups/host/{ch}snap.tar.gz")
            assert ch not in out, f"U+{ord(ch):04X} reached the terminal"

    def test_a_code_point_above_ff_is_not_spelled_as_a_byte(self):
        """`\\x202e` would read as `\\x20` plus a literal `2e` -- ambiguous output."""
        out = snap._safe_name("\u202e")
        assert out == "\\u202e"

    def test_right_to_left_marks_are_kept(self):
        """LRM/RLM appear in genuine right-to-left names and cannot rewrite a line alone."""
        key = "backups/host/\u200fnotes.tar.gz"
        assert snap._safe_name(key) == key
