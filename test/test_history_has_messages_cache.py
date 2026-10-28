"""``ConversationLog.listing_has_messages`` memoizes ``has_messages`` per file stamp.

The Older Sessions listing asks it of every untitled transcript on each history
refresh. These tests pin that an unchanged file is answered without a read,
that every kind of write still changes the answer, and that the callers gating
memory on the answer keep the uncached, locked ``has_messages``.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew.dashboard.handlers.sessions_parts.history_reads import _is_user_listed_row
from kiro_crew.history import ConversationLog
from kiro_crew.history_cache import _LRUCache

_SRC = Path(__file__).resolve().parents[1] / "src" / "kiro_crew"


def _line(record: dict) -> str:
    return json.dumps(record) + "\n"


def _metadata_only(log: ConversationLog, key: str) -> None:
    log._path(key).write_text(_line({"_type": "metadata", "title": key}), encoding="utf-8")


def _listed(log: ConversationLog) -> list[str]:
    return [row["key"] for row in log.list_sessions() if _is_user_listed_row(log, row)]


class _RewriteOnFirstStore:
    """Cache stand-in that runs *hook* once, just before the first store."""

    def __init__(self, inner, hook) -> None:
        self._inner = inner
        self._hook = hook
        self.fired = False

    def __setitem__(self, key, value) -> None:
        hook, self._hook = self._hook, None
        if hook is not None:
            self.fired = True
            hook()
        self._inner[key] = value

    def __getattr__(self, name):
        return getattr(self._inner, name)


def test_unchanged_file_is_answered_without_a_read(tmp_path):
    log = ConversationLog(base_dir=tmp_path)
    _metadata_only(log, "k1")
    assert log.listing_has_messages("k1") is False
    with patch.object(log, "has_messages", side_effect=AssertionError("re-read")):
        assert log.listing_has_messages("k1") is False


def test_append_flips_a_cached_false(tmp_path):
    log = ConversationLog(base_dir=tmp_path)
    log.set_title("k1", "only metadata")
    assert log.listing_has_messages("k1") is False
    log.append("k1", "user", "hello")
    assert log.listing_has_messages("k1") is True


def test_metadata_write_keeps_the_answer_and_reads_again(tmp_path):
    log = ConversationLog(base_dir=tmp_path)
    log.append("k1", "user", "hello")
    assert log.listing_has_messages("k1") is True
    log.set_title("k1", "renamed")
    with patch.object(log, "has_messages", wraps=log.has_messages) as read:
        assert log.listing_has_messages("k1") is True
    assert read.call_count == 1


def test_external_rewrite_with_new_size_is_seen(tmp_path):
    log = ConversationLog(base_dir=tmp_path)
    _metadata_only(log, "k1")
    assert log.listing_has_messages("k1") is False
    with log._path("k1").open("a", encoding="utf-8") as fh:
        fh.write(_line({"role": "user", "content": "hi"}))
    assert log.listing_has_messages("k1") is True


def test_absent_file_is_false_and_not_cached(tmp_path):
    log = ConversationLog(base_dir=tmp_path)
    assert log.listing_has_messages("missing") is False
    assert log._has_messages_cache.get("missing") is None
    log.append("missing", "user", "now here")
    assert log.listing_has_messages("missing") is True


def test_generation_bump_alone_invalidates_a_same_stamp_answer(tmp_path):
    # The writer is a second log, so its invalidation cannot pop this log's
    # entry: only the process-wide generation can retire the answer.
    log = ConversationLog(base_dir=tmp_path)
    writer = ConversationLog(base_dir=tmp_path)
    log.set_title("dashboard:chat-3-1", "t")
    path = log._path("dashboard:chat-3-1")
    listed_key = path.stem
    original = path.read_text(encoding="utf-8")
    with patch.object(ConversationLog, "_cache_identity", staticmethod(lambda st: (1, 1, 1, 1))):
        assert log.listing_has_messages(listed_key) is False
        # Same length, so nothing in a real stamp but the bytes would differ.
        row = _line({"role": "user", "content": ""})
        path.write_text(row.ljust(len(original) - 1) + "\n", encoding="utf-8")
        assert log.listing_has_messages(listed_key) is False
        writer._invalidate_cache("dashboard:chat-3-1")
        assert log.listing_has_messages(listed_key) is True


def test_a_rewrite_during_the_read_publishes_nothing(tmp_path):
    log = ConversationLog(base_dir=tmp_path)
    log.set_title("k1", "t")

    def append_a_row() -> None:
        log.append("k1", "user", "landed mid-read")

    wrapper = _RewriteOnFirstStore(log._has_messages_cache, append_a_row)
    log._has_messages_cache = wrapper  # type: ignore[assignment]
    assert log.listing_has_messages("k1") is False
    assert wrapper.fired
    assert wrapper._inner.get("k1") is None
    assert log.listing_has_messages("k1") is True


def test_a_second_log_over_the_same_dir_sees_the_other_log_s_write(tmp_path):
    log_a = ConversationLog(base_dir=tmp_path)
    log_b = ConversationLog(base_dir=tmp_path)
    log_a.set_title("k1", "t")
    assert log_a.listing_has_messages("k1") is False
    log_b.append("k1", "user", "from the other log")
    with patch.object(log_a, "has_messages", wraps=log_a.has_messages) as read:
        assert log_a.listing_has_messages("k1") is True
    assert read.call_count == 1


def test_unlink_after_a_cached_true_answers_false(tmp_path):
    log = ConversationLog(base_dir=tmp_path)
    log.append("k1", "user", "hello")
    assert log.listing_has_messages("k1") is True
    log._path("k1").unlink()
    assert log.listing_has_messages("k1") is False


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
@pytest.mark.skipif(sys.platform != "win32" and os.geteuid() == 0, reason="root reads a 0o000 file")
def test_unreadable_file_raises_and_publishes_nothing(tmp_path):
    log = ConversationLog(base_dir=tmp_path)
    log.set_title("k1", "t")
    path = log._path("k1")
    path.chmod(0)
    try:
        with pytest.raises(PermissionError):
            log.listing_has_messages("k1")
        assert log._has_messages_cache.get("k1") is None
    finally:
        path.chmod(0o600)


def test_second_user_only_listing_reads_no_heads(tmp_path):
    log = ConversationLog(base_dir=tmp_path)
    for i in range(64):
        _metadata_only(log, f"m{i:02d}")
    for i in range(4):
        log.append(f"c{i}", "user", "hello")
    with patch.object(log, "has_messages", wraps=log.has_messages) as read:
        first = _listed(log)
        assert read.call_count == 64
        second = _listed(log)
        assert read.call_count == 64
    assert first == second


def test_has_messages_lru_cliff_is_the_documented_limit(tmp_path):
    log = ConversationLog(base_dir=tmp_path)
    log._has_messages_cache = _LRUCache(4)
    for i in range(8):
        _metadata_only(log, f"m{i}")
    with patch.object(log, "has_messages", wraps=log.has_messages) as read:
        _listed(log)
        assert read.call_count == 8
        _listed(log)
        assert read.call_count == 16


def test_memory_gates_read_has_messages_uncached(tmp_path):
    log = ConversationLog(base_dir=tmp_path)
    log.set_title("k1", "t")
    assert log.listing_has_messages("k1") is False
    with patch.object(log, "_locked", wraps=log._locked) as locked:
        assert log.has_messages("k1") is False
        assert log.has_messages("k1") is False
    assert locked.call_count == 2


def test_only_the_listing_calls_the_memoized_reader():
    """The memory gates must not drift onto the cached answer."""
    callers = {
        path.relative_to(_SRC).as_posix()
        for path in _SRC.rglob("*.py")
        if ".listing_has_messages(" in path.read_text(encoding="utf-8")
    }
    assert callers == {"dashboard/handlers/sessions_parts/history_reads.py"}
