"""list_sessions caches the head-scan title of an untitled transcript.

The listing titles a session whose metadata line carries no title from the
first user message in its first 21 lines. That scan is memoized by the
file's cache identity and invalidation generation, so a listing does not
re-read every untitled transcript. These tests pin both halves: the memo
is used, and it never outlives a change to the file.
"""

from __future__ import annotations

import builtins
import json
from pathlib import Path

import pytest

from kiro_crew import history_listing_memo, history_search
from kiro_crew.history import ConversationLog


def _write(path: Path, *rows: dict) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


@pytest.fixture
def opens(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []
    real_open = builtins.open

    def counting_open(file, *args, **kwargs):  # type: ignore[no-untyped-def]
        seen.append(str(file))
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(history_search, "open", counting_open, raising=False)
    return seen


def _titles(log: ConversationLog) -> dict[str, str]:
    return {row["key"]: row["title"] for row in log.list_sessions()}


def test_untitled_head_is_scanned_once(tmp_path: Path, opens: list[str]) -> None:
    _write(tmp_path / "a.jsonl", {"_type": "metadata"}, {"role": "user", "content": "hello there"})
    log = ConversationLog(base_dir=tmp_path)
    assert _titles(log) == {"a": "hello there"}
    first = len(opens)
    assert first >= 1
    assert _titles(log) == {"a": "hello there"}
    assert len(opens) == first, "warm listing re-read the transcript head"


def test_head_without_a_user_message_is_cached_as_untitled(
    tmp_path: Path, opens: list[str]
) -> None:
    _write(tmp_path / "b.jsonl", {"_type": "metadata"}, {"role": "assistant", "content": "only me"})
    log = ConversationLog(base_dir=tmp_path)
    assert _titles(log) == {"b": "b"}
    first = len(opens)
    assert _titles(log) == {"b": "b"}
    assert len(opens) == first


def test_a_changed_file_is_rescanned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A rewrite made outside the log moves no memo key, so only the listing
    # memo's TTL bounds it; expire it at once to reach the per-file check.
    monkeypatch.setattr(history_listing_memo, "TTL_SECS", 0.0)
    path = tmp_path / "c.jsonl"
    _write(path, {"role": "assistant", "content": "x"})
    log = ConversationLog(base_dir=tmp_path)
    assert _titles(log) == {"c": "c"}
    _write(path, {"role": "assistant", "content": "x"}, {"role": "user", "content": "now titled"})
    assert _titles(log) == {"c": "now titled"}


def test_a_write_through_the_log_is_rescanned(tmp_path: Path) -> None:
    _write(tmp_path / "d.jsonl", {"role": "assistant", "content": "x"})
    log = ConversationLog(base_dir=tmp_path)
    assert _titles(log) == {"d": "d"}
    log.append("d", "user", "appended title")
    assert _titles(log) == {"d": "appended title"}


def test_an_unreadable_head_is_not_cached(tmp_path: Path, opens: list[str]) -> None:
    (tmp_path / "e.jsonl").write_bytes(b'{"role": "user", "content": "\xff"}\n')
    log = ConversationLog(base_dir=tmp_path)
    assert _titles(log) == {"e": "e"}
    first = len(opens)
    assert _titles(log) == {"e": "e"}
    assert len(opens) > first, "a head that failed to read must be retried, not memoized"
