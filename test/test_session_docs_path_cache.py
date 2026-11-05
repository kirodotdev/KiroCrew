"""The session-docs file-change cache holds changed paths only, for every session.

``GET /api/artifacts/session-docs`` walks every session through
``ConversationLog.read_file_change_messages``. A cache smaller than the store
is evicted before reuse by that walk, and a cache that keeps ``before``/``after``
bodies is too large to cover the store.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from kiro_crew import history_projection
from kiro_crew.dashboard.handlers import artifacts as h
from kiro_crew.history import ConversationLog
from kiro_crew.history_cache import _TRANSCRIPT_CACHE_MAX

#: More sessions than the general transcript cache bound.
_SESSIONS = _TRANSCRIPT_CACHE_MAX + 44


def _write(path: Path, rows: list[object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8", newline="\n")


def _rows(i: int, doc: str) -> list[object]:
    return [
        {"_type": "metadata", "title": f"s{i}"},
        {"role": "user", "content": "write it", "ts": f"2026-11-03T00:00:{i % 60:02d}"},
        {
            "role": "assistant",
            "content": "done",
            "ts": f"2026-11-03T00:01:{i % 60:02d}",
            "meta": {
                "file_changes": [
                    {"path": doc, "before": "a" * 4096, "after": "b" * 4096, "kind": "edit"},
                    {"path": f"/tmp/code_{i}.py", "before": "", "after": "x = 1\n"},
                    "not-a-dict",
                    {"path": 123},
                    {"before": "no path"},
                ]
            },
        },
        {
            "role": "assistant",
            "content": "nothing usable",
            "ts": "2026-11-03T00:02:00",
            "meta": {"file_changes": ["bad", {"path": None}]},
        },
    ]


@pytest.fixture
def store(tmp_path: Path) -> ConversationLog:
    log = ConversationLog(base_dir=tmp_path)
    for i in range(_SESSIONS):
        _write(log._path(f"sess{i}"), _rows(i, f"/tmp/docs/report_{i}.md"))
    return log


class _FullTranscriptLog:
    """The same store read through full transcripts: every change kept whole."""

    def __init__(self, log: ConversationLog):
        self._log = log

    def list_sessions(self):
        return self._log.list_sessions()

    def read_messages(self, key):
        return self._log.read_messages(key)


def _count_file_reads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    opened: list[str] = []
    real = history_projection.bounded_raw_records

    def counting(handle, path, *args, **kwargs):
        opened.append(str(path))
        return real(handle, path, *args, **kwargs)

    monkeypatch.setattr(history_projection, "bounded_raw_records", counting)
    return opened


def test_output_matches_full_transcript_scan_and_cache_holds_paths_only(store):
    sessions = store.list_sessions()
    assert len(sessions) == _SESSIONS

    projected = h._collect_session_docs(store, {"/tmp/docs/report_3.md": "saved-slug"})
    reference = h._collect_session_docs(
        _FullTranscriptLog(store), {"/tmp/docs/report_3.md": "saved-slug"}
    )
    assert len(projected) == _SESSIONS
    assert json.dumps(projected, sort_keys=False) == json.dumps(reference, sort_keys=False)

    assert len(store._file_change_cache) == _SESSIONS
    for i in range(_SESSIONS):
        entry = store._file_change_cache.get(f"sess{i}")
        assert entry is not None
        assert entry.messages == [
            {
                "ts": f"2026-11-03T00:01:{i % 60:02d}",
                "role": "assistant",
                "meta": {
                    "file_changes": [
                        {"path": f"/tmp/docs/report_{i}.md"},
                        {"path": f"/tmp/code_{i}.py"},
                    ]
                },
            }
        ]


def test_second_scan_over_more_than_256_sessions_reads_no_file(store, monkeypatch):
    h._collect_session_docs(store, {})
    opened = _count_file_reads(monkeypatch)

    second = h._collect_session_docs(store, {})

    assert opened == []
    assert len(second) == _SESSIONS


def test_changed_session_file_is_read_again_and_picked_up(store, monkeypatch):
    h._collect_session_docs(store, {})
    _write(store._path("sess7"), _rows(7, "/tmp/docs/renamed.md"))
    opened = _count_file_reads(monkeypatch)

    out = h._collect_session_docs(store, {})

    assert opened == [str(store._path("sess7"))]
    paths = {e["path"] for e in out}
    assert "/tmp/docs/renamed.md" in paths
    assert "/tmp/docs/report_7.md" not in paths


def test_cache_memory_is_paths_only(store):
    h._collect_session_docs(store, {})
    entries = [store._file_change_cache.get(f"sess{i}") for i in range(_SESSIONS)]
    sizes = [sys.getsizeof(json.dumps(e.messages)) for e in entries if e is not None]
    # Two short paths and one timestamp per session; the 8 KiB of before/after
    # bodies per session are not kept.
    assert sizes and max(sizes) < 400
