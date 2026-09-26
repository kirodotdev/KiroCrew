"""``LocalMemoryFiles`` reproduces what ``MemoryStore``'s private methods did.

The ``MemoryFiles`` seam moved memory's admission gates, hardened reader and
atomic writer out of ``MemoryStore`` and behind an extension point. The claim
that came with that move is that the PUBLIC edition's behaviour is unchanged --
and a claim like that is only worth something if something fails when it stops
being true.

The suites that already cover this surface (``test_memory.py``,
``test_memory_markdown_read.py``) exercise it THROUGH ``MemoryStore``, so they
would also pass if the local implementation quietly relaxed a guarantee that
``MemoryStore`` happened to re-establish on its own. These tests therefore go
straight at ``LocalMemoryFiles``, one class of guarantee at a time, and assert
the observable artefacts -- the bytes on disk, the permission bits, the absence
of a temp file, the target of a planted link being untouched -- rather than that
a call returned without raising.

Each test names the property it pins, because a future implementation of this
protocol has to satisfy the PROPERTY, and the local specifics (an ``O_NOFOLLOW``
open, an inode's link count) are only how this one implementation achieves it.
"""

from __future__ import annotations

import os
import stat as _stat
from pathlib import Path

import pytest

from kiro_crew.memory_files import LocalMemoryFiles
from kiro_crew.platform.interfaces import MemoryRoots


def _files(tmp_path: Path) -> LocalMemoryFiles:
    """A ``LocalMemoryFiles`` over a real, link-free tree, roots created."""
    memory_dir = tmp_path / "ws" / "memory"
    history_dir = memory_dir / "history"
    history_dir.mkdir(parents=True)
    return LocalMemoryFiles(
        MemoryRoots(
            workspace=tmp_path / "ws",
            memory_dir=memory_dir,
            history_dir=history_dir,
        )
    )


def _link_or_skip(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):  # pragma: no cover - Windows CI
        pytest.skip("symlinks not available on this platform")


class TestAtomicWriteParity:
    """A reader observes committed versions only, and a mode is never widened."""

    def test_write_publishes_exact_bytes_and_leaves_no_temp_file(self, tmp_path: Path) -> None:
        files = _files(tmp_path)
        target = files._roots.memory_dir / "preferences.md"

        # newline="" so the assertion is byte-exact on every platform: the
        # default (None) applies universal-newline translation, which the
        # write path preserves, rewriting \n to \r\n on Windows.
        files.write(target, "# User Preferences\n\n- prefers pytest\n", newline="")

        assert target.read_bytes() == b"# User Preferences\n\n- prefers pytest\n"
        # The staging file is a sibling, so a leftover would land here and would
        # also be picked up by the history "*.md" glob if it were named badly.
        siblings = {p.name for p in files._roots.memory_dir.iterdir()}
        assert siblings == {"history", "preferences.md"}

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
    def test_replacing_an_existing_file_preserves_its_permission_bits(self, tmp_path: Path) -> None:
        """The regression a rename-based publish introduces if nobody carries the mode.

        ``write_text`` truncated in place and never touched the mode; a staged
        temp + rename installs the TEMP file's mode, so an owner-only memory file
        would silently widen to the umask default on its next write.
        """
        files = _files(tmp_path)
        target = files._roots.memory_dir / "preferences.md"
        target.write_text("original\n", encoding="utf-8")
        os.chmod(target, 0o600)

        files.write(target, "replaced\n")

        assert target.read_text(encoding="utf-8") == "replaced\n"
        assert _stat.S_IMODE(os.lstat(target).st_mode) == 0o600

    def test_newline_argument_is_honoured_verbatim(self, tmp_path: Path) -> None:
        """``newline=""`` must not translate, or a CRLF document gains a CR per save."""
        files = _files(tmp_path)
        target = files._roots.memory_dir / "projects.md"

        files.write(target, "a\r\nb\r\n", newline="")

        assert target.read_bytes() == b"a\r\nb\r\n"

    def test_a_link_at_the_write_target_is_refused_and_its_target_untouched(
        self, tmp_path: Path
    ) -> None:
        files = _files(tmp_path)
        outside = tmp_path / "evidence.txt"
        outside.write_text("belongs to someone else\n", encoding="utf-8")
        target = files._roots.memory_dir / "preferences.md"
        _link_or_skip(target, outside)

        with pytest.raises(OSError):
            files.write(target, "- attack\n")

        assert outside.read_text(encoding="utf-8") == "belongs to someone else\n"


class TestLockParity:
    """The lock file is opened without following a link, and never truncates one."""

    def test_a_planted_link_at_the_lock_path_is_refused(self, tmp_path: Path) -> None:
        """A bare ``open(path, "w")`` would truncate the link's TARGET.

        The lock carries no content, so the open must not truncate; and it must
        not follow a link, or the next memory write destroys whatever the link
        points at.
        """
        files = _files(tmp_path)
        outside = tmp_path / "evidence.txt"
        outside.write_text("belongs to someone else\n", encoding="utf-8")
        _link_or_skip(files._roots.memory_dir / ".write.lock", outside)

        with pytest.raises(OSError):
            with files.lock(files._roots.memory_dir):
                pass  # pragma: no cover - the lock open must refuse first

        assert outside.read_text(encoding="utf-8") == "belongs to someone else\n"

    def test_a_hardlinked_lock_file_is_refused(self, tmp_path: Path) -> None:
        """A hardlink passes a link check -- it is a regular inode -- so the

        implementation additionally requires a LONE inode.
        """
        files = _files(tmp_path)
        outside = tmp_path / "evidence.txt"
        outside.write_text("belongs to someone else\n", encoding="utf-8")
        try:
            os.link(outside, files._roots.memory_dir / ".write.lock")
        except (OSError, NotImplementedError):  # pragma: no cover
            pytest.skip("hard links not available on this platform")

        with pytest.raises(OSError):
            with files.lock(files._roots.memory_dir):
                pass  # pragma: no cover - the lock open must refuse first

        assert outside.read_text(encoding="utf-8") == "belongs to someone else\n"

    def test_the_lock_is_released_when_the_body_raises(self, tmp_path: Path) -> None:
        """A leaked fd or held lock would wedge every later writer in the process."""
        files = _files(tmp_path)

        with pytest.raises(ValueError):
            with files.lock(files._roots.memory_dir):
                raise ValueError("boom")

        # Re-entering proves the previous tenure was released, not merely that
        # the exception propagated.
        with files.lock(files._roots.memory_dir):
            pass


class TestLinkFreeRootParity:
    """No write syscall touches a tree whose root is a link, and refusal is LOUD."""

    @pytest.mark.parametrize("linked", ["memory", "history"])
    def test_a_linked_root_refuses_writers_without_touching_the_target(
        self, tmp_path: Path, linked: str
    ) -> None:
        outside = tmp_path / "outside-tree"
        outside.mkdir()
        memory_dir = tmp_path / "ws" / "memory"
        history_dir = memory_dir / "history"
        if linked == "memory":
            memory_dir.parent.mkdir(parents=True)
            _link_or_skip(memory_dir, outside)
        else:
            memory_dir.mkdir(parents=True)
            _link_or_skip(history_dir, outside)
        files = LocalMemoryFiles(
            MemoryRoots(workspace=tmp_path / "ws", memory_dir=memory_dir, history_dir=history_dir)
        )

        # Every writer, including the ones whose first act is structural: a gate
        # applied after mkdir is not a gate, it is a cleanup problem.
        with pytest.raises(OSError):
            files.mkdir(history_dir)
        with pytest.raises(OSError):
            files.write(memory_dir / "preferences.md", "- attack\n")
        with pytest.raises(OSError):
            files.replace_if(memory_dir / "preferences.md", "- attack\n", base=None)
        with pytest.raises(OSError):
            files.remove(memory_dir / "history" / "2026-01-01.md")

        assert list(outside.iterdir()) == []

    def test_a_refused_read_degrades_to_empty_rather_than_raising(self, tmp_path: Path) -> None:
        """Reads and writes fail DIFFERENTLY, on purpose.

        A refused read degrades to an empty entry so a session still starts; a
        refused write must not look like success.
        """
        outside = tmp_path / "outside-tree"
        outside.mkdir()
        (outside / "preferences.md").write_text("planted\n", encoding="utf-8")
        memory_dir = tmp_path / "ws" / "memory"
        memory_dir.parent.mkdir(parents=True)
        _link_or_skip(memory_dir, outside)
        files = LocalMemoryFiles(
            MemoryRoots(
                workspace=tmp_path / "ws",
                memory_dir=memory_dir,
                history_dir=memory_dir / "history",
            )
        )

        entry = files.read_entry(memory_dir / "preferences.md")

        assert entry.content == ""
        assert entry.updated_at is None

    def test_glob_answers_nothing_for_a_refused_root(self, tmp_path: Path) -> None:
        """Listing is a syscall too, so it is gated like the rest.

        Before the seam the gate was applied by each caller and the pruning path
        did not apply it, so a linked root was enumerated -- and pruned -- THROUGH
        the link.
        """
        outside = tmp_path / "outside-tree"
        outside.mkdir()
        (outside / "2026-01-01.md").write_text("planted\n", encoding="utf-8")
        memory_dir = tmp_path / "ws" / "memory"
        memory_dir.parent.mkdir(parents=True)
        _link_or_skip(memory_dir, outside)
        files = LocalMemoryFiles(
            MemoryRoots(
                workspace=tmp_path / "ws",
                memory_dir=memory_dir,
                history_dir=memory_dir,
            )
        )

        assert files.glob(memory_dir, "*.md") == []
        assert (outside / "2026-01-01.md").exists()


class TestReadParity:
    """What the two readers promise, and why they differ."""

    def test_read_text_raises_on_undecodable_bytes(self, tmp_path: Path) -> None:
        """The asymmetry that protects a read-modify-write caller.

        This value is what the next whole-file write is computed from, so
        answering ``""`` for a file that merely failed to decode would let that
        write persist over the original bytes.
        """
        files = _files(tmp_path)
        target = files._roots.memory_dir / "preferences.md"
        target.write_bytes(b"\xff\xfe not utf-8")

        with pytest.raises(UnicodeDecodeError):
            files.read_text(target)
        assert target.read_bytes() == b"\xff\xfe not utf-8"  # left intact

    def test_read_entry_degrades_on_undecodable_bytes(self, tmp_path: Path) -> None:
        """The guarded reader serves readers, who prefer a gap to a traceback."""
        files = _files(tmp_path)
        target = files._roots.memory_dir / "preferences.md"
        target.write_bytes(b"\xff\xfe not utf-8")

        assert files.read_entry(target).content == ""

    def test_read_text_answers_empty_for_a_missing_file(self, tmp_path: Path) -> None:
        files = _files(tmp_path)
        assert files.read_text(files._roots.memory_dir / "nope.md") == ""

    def test_read_entry_reports_metadata_only_with_content(self, tmp_path: Path) -> None:
        """The documented empty-state contract: empty content carries null metadata.

        Consumers key incremental sync on ``updated_at``, and an "updated" empty
        file has nothing to sync.
        """
        files = _files(tmp_path)
        target = files._roots.memory_dir / "preferences.md"
        target.write_text("", encoding="utf-8")

        empty = files.read_entry(target)
        assert (empty.content, empty.updated_at) == ("", None)

        files.write(target, "real\n", newline="")
        filled = files.read_entry(target)
        assert filled.content == "real\n"
        assert filled.updated_at is not None

    def test_read_entry_refuses_a_file_over_the_size_cap(self, tmp_path: Path) -> None:
        """The cap bounds one read so a planted huge file cannot exhaust memory.

        Exercised on the MEMBER/named-store path deliberately: that is the branch
        whose reader is the descriptor-checked one governed by
        ``_HISTORY_SNAPSHOT_MAX_BYTES``. The default store's branch delegates to
        ``hooks.safe_read_file_bytes_nolink``, which carries its own cap -- so
        pinning the constant there would assert against a number that does not
        govern the path.
        """
        memory_dir = tmp_path / "ws" / "memory"
        memory_dir.mkdir(parents=True)
        files = LocalMemoryFiles(
            MemoryRoots(
                workspace=tmp_path / "ws",
                memory_dir=memory_dir,
                history_dir=memory_dir / "history",
                memory_version=2,
            )
        )
        target = memory_dir / "preferences.md"
        target.write_text("x" * 200, encoding="utf-8")
        files._HISTORY_SNAPSHOT_MAX_BYTES = 128

        assert files.read_entry(target).content == ""
        with pytest.raises(OSError, match="size cap"):
            files.read_entry(target, require_readable=True)

    def test_read_entry_refuses_a_non_regular_file_without_blocking(self, tmp_path: Path) -> None:
        """A planted FIFO opened read-only blocks forever waiting for a writer.

        So the kind check has to come BEFORE the open, not inside the reader.
        """
        files = _files(tmp_path)
        target = files._roots.memory_dir / "preferences.md"
        try:
            os.mkfifo(target)
        except (AttributeError, OSError, NotImplementedError):  # pragma: no cover
            pytest.skip("FIFOs not available on this platform")

        assert files.read_entry(target).content == ""

    def test_read_text_for_rewrite_refuses_a_link_where_read_text_would_follow_it(
        self, tmp_path: Path
    ) -> None:
        """The distinction between the two reads, stated as behaviour.

        A plain read that follows a link leaks the target to the reader; a read
        whose caller is about to rewrite the file REPUBLISHES the target into
        memory, where it is then served in context and included in exports.
        """
        files = _files(tmp_path)
        outside = tmp_path / "evidence.txt"
        outside.write_text("belongs to someone else\n", encoding="utf-8")
        target = files._roots.history_dir / "2026-01-01.md"
        _link_or_skip(target, outside)

        with pytest.raises(OSError):
            files.read_text_for_rewrite(target)

    def test_read_text_for_rewrite_answers_empty_for_a_fresh_day(self, tmp_path: Path) -> None:
        files = _files(tmp_path)
        assert files.read_text_for_rewrite(files._roots.history_dir / "2026-01-01.md") == ""


class TestReplaceIfParity:
    """The compare-and-set contract: refuse on drift, never retry against a new base."""

    def test_a_matching_base_writes(self, tmp_path: Path) -> None:
        files = _files(tmp_path)
        target = files._roots.memory_dir / "preferences.md"
        files.write(target, "first\n")

        assert files.replace_if(target, "second\n", base="first\n") is True
        assert target.read_text(encoding="utf-8") == "second\n"

    def test_a_moved_base_is_refused_and_the_file_is_left_alone(self, tmp_path: Path) -> None:
        """The silent-overwrite this guard exists to prevent.

        A refusal returns ``False`` rather than raising, because losing a race is
        a normal outcome the caller retries; and it does NOT re-read and write
        anyway, which would be the overwrite itself.
        """
        files = _files(tmp_path)
        target = files._roots.memory_dir / "preferences.md"
        files.write(target, "written by the human\n")

        assert files.replace_if(target, "merged\n", base="what the merge read\n") is False
        assert target.read_text(encoding="utf-8") == "written by the human\n"

    def test_a_whitespace_only_difference_is_still_drift(self, tmp_path: Path) -> None:
        """Byte-for-byte: a merge computed from different bytes is stale whatever
        the difference was."""
        files = _files(tmp_path)
        target = files._roots.memory_dir / "preferences.md"
        files.write(target, "a\n\n")

        assert files.replace_if(target, "merged\n", base="a\n") is False
        assert target.read_text(encoding="utf-8") == "a\n\n"

    def test_base_none_writes_unconditionally(self, tmp_path: Path) -> None:
        """Direct user intent wins by design."""
        files = _files(tmp_path)
        target = files._roots.memory_dir / "preferences.md"
        files.write(target, "whatever\n")

        assert files.replace_if(target, "user typed this\n", base=None) is True
        assert target.read_text(encoding="utf-8") == "user typed this\n"

    def test_an_absent_file_matches_an_empty_base(self, tmp_path: Path) -> None:
        """``read_text`` answers ``""`` for a missing file, so seeding compares
        against ``""`` -- the pre-seam behaviour of the CAS callers."""
        files = _files(tmp_path)
        target = files._roots.memory_dir / "preferences.md"

        assert files.replace_if(target, "seeded\n", base="") is True
        assert target.read_text(encoding="utf-8") == "seeded\n"


class TestStructureParity:
    """Structure is explicit, and removal is survivable."""

    def test_mkdir_creates_parents_and_is_idempotent(self, tmp_path: Path) -> None:
        files = _files(tmp_path)
        nested = files._roots.memory_dir / "a" / "b"

        files.mkdir(nested)
        files.mkdir(nested)  # exist_ok: seeding runs on every boot

        assert nested.is_dir()

    def test_remove_tolerates_a_missing_file(self, tmp_path: Path) -> None:
        """Pruning races itself across processes; a missing file is a success."""
        files = _files(tmp_path)
        files.remove(files._roots.history_dir / "2000-01-01.md")

    def test_glob_lists_one_level_and_answers_empty_for_a_missing_dir(self, tmp_path: Path) -> None:
        files = _files(tmp_path)
        (files._roots.history_dir / "2026-01-01.md").write_text("a\n", encoding="utf-8")
        (files._roots.history_dir / "notes.txt").write_text("b\n", encoding="utf-8")
        nested = files._roots.history_dir / "deeper"
        nested.mkdir()
        (nested / "2026-02-02.md").write_text("c\n", encoding="utf-8")

        names = {p.name for p in files.glob(files._roots.history_dir, "*.md")}

        assert names == {"2026-01-01.md"}
        assert files.glob(files._roots.memory_dir / "absent", "*.md") == []
