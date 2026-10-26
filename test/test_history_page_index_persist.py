"""A history page frames the page, not the transcript.

Each case counts the records framed by one page read over a large transcript -- in a
new process, and after the metadata-line rewrites (metadata update, consolidation
marker) that replace the file's inode -- and checks the page against the full read
after appends, a body rewrite and a damaged sidecar.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    yield


ROWS = 3_000


@pytest.fixture
def transcript(tmp_path, monkeypatch) -> tuple[Any, str]:
    from kiro_crew import history

    monkeypatch.setattr(history, "_SESSION_MAX_BYTES", 10**12)
    key = "dashboard:bounded-history"
    log = history.ConversationLog(base_dir=tmp_path / "hist")
    path = log._path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({"_type": "metadata", "created_at": "2026-10-01T00:00:00+00:00"})]
    lines.extend(
        json.dumps({"role": "tool", "content": f"row-{row} " + "x" * 200, "ts": f"t{row:08d}"})
        for row in range(ROWS)
    )
    # Bytes, not text: a text write on Windows would turn every newline into CRLF,
    # and the metadata writers' text read normalizes that away, which is a body
    # change no carry may survive. Transcripts the gateway writes are LF.
    path.write_bytes(("\n".join(lines) + "\n").encode("utf-8"))
    return tmp_path / "hist", key


class _FrameMeter:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from kiro_crew import history_projection

        self.framed = 0
        real = history_projection.strict_raw_records_with_offsets

        def metered(*args: Any, **kwargs: Any) -> Iterator[Any]:
            for record in real(*args, **kwargs):
                self.framed += 1
                yield record

        # Through the facade: its mirror reaches every owner that reads the name.
        monkeypatch.setattr(history_projection, "strict_raw_records_with_offsets", metered)


def _bound() -> int:
    from kiro_crew.history_projection_contract import _TRANSCRIPT_PAGE_INDEX_STRIDE

    return 50 + 2 * _TRANSCRIPT_PAGE_INDEX_STRIDE + 8


def _contents(page: Any) -> list[str]:
    return [m["content"] for m in page.messages]


def test_a_first_history_page_in_a_new_process_frames_the_page_not_the_file(
    transcript, monkeypatch
):
    from kiro_crew.history import ConversationLog

    base_dir, key = transcript
    meter = _FrameMeter(monkeypatch)
    ConversationLog(base_dir=base_dir).read_messages_chained_page(key, limit=50)
    assert meter.framed >= ROWS  # the one cold build, and proof the meter counts
    meter.framed = 0
    page = ConversationLog(base_dir=base_dir).read_messages_chained_page(key, limit=50)
    assert page.total == ROWS
    assert _contents(page)[-1].startswith(f"row-{ROWS - 1} ")
    assert len(page.messages) == 50
    assert meter.framed <= _bound(), meter.framed


def test_a_metadata_rewrite_does_not_reframe_the_transcript(transcript, monkeypatch):
    from kiro_crew.history import ConversationLog

    base_dir, key = transcript
    log = ConversationLog(base_dir=base_dir)
    log.read_messages_chained_page(key, limit=50)
    log.update_metadata(key, {"title": "renamed " + "y" * 40})
    meter = _FrameMeter(monkeypatch)
    page = log.read_messages_chained_page(key, limit=50, before=1_000)
    assert page.total == ROWS
    assert _contents(page)[0].startswith("row-950 ")
    assert meter.framed <= _bound(), meter.framed
    # And in a new process after the rewrite, from the persisted base.
    meter.framed = 0
    page = ConversationLog(base_dir=base_dir).read_messages_chained_page(key, limit=50)
    assert page.total == ROWS
    assert meter.framed <= _bound(), meter.framed


def test_a_consolidation_marker_rewrite_does_not_reframe_the_transcript(transcript, monkeypatch):
    from kiro_crew.history import ConversationLog

    base_dir, key = transcript
    log = ConversationLog(base_dir=base_dir)
    log.read_messages_chained_page(key, limit=50)
    log.mark_consolidated(key, 10)
    meter = _FrameMeter(monkeypatch)
    page = log.read_messages_chained_page(key, limit=50)
    assert page.total == ROWS
    assert meter.framed <= _bound(), meter.framed


def test_a_bounded_page_matches_the_full_read_after_appends_and_rewrites(transcript):
    from kiro_crew.history import ConversationLog

    base_dir, key = transcript
    log = ConversationLog(base_dir=base_dir)
    log.read_messages_chained_page(key, limit=50)
    log.update_metadata(key, {"title": "t"})
    log.append(key, "user", "appended after the rewrite")
    restarted = ConversationLog(base_dir=base_dir)
    page = restarted.read_messages_chained_page(key, limit=40, before=2_000)
    full = restarted.read_messages_chained(key)
    assert page.total == len(full) == ROWS + 1
    assert _contents(page) == [m["content"] for m in full[1_960:2_000]]
    tail = restarted.read_messages_chained_page(key, limit=1)
    assert _contents(tail) == ["appended after the rewrite"]


def test_a_body_rewrite_is_never_served_from_the_carried_index(transcript):
    """A rewrite that changes rows (not only the first line) must not reuse offsets:
    the persisted base does not match the new file and the page is rebuilt."""
    from kiro_crew.history import ConversationLog

    base_dir, key = transcript
    log = ConversationLog(base_dir=base_dir)
    log.read_messages_chained_page(key, limit=50)
    kept = log.read_messages_chained(key)[100:]
    log.rewrite_session(key, kept)
    restarted = ConversationLog(base_dir=base_dir)
    page = restarted.read_messages_chained_page(key, limit=10, before=10)
    assert page.total == len(kept)
    assert _contents(page) == [m["content"] for m in kept[:10]]


def test_a_damaged_sidecar_is_ignored(transcript):
    from kiro_crew.history import ConversationLog
    from kiro_crew.history_page_index import sidecar_path

    base_dir, key = transcript
    log = ConversationLog(base_dir=base_dir)
    log.read_messages_chained_page(key, limit=50)
    sidecar = sidecar_path(log._path(key))
    assert sidecar.is_file()
    payload = json.loads(sidecar.read_text())
    payload["cps"] = [[0, 10**9]]
    sidecar.write_text(json.dumps(payload))
    page = ConversationLog(base_dir=base_dir).read_messages_chained_page(key, limit=5)
    assert page.total == ROWS
    assert _contents(page)[-1].startswith(f"row-{ROWS - 1} ")


def test_deleting_a_transcript_removes_its_sidecar(transcript):
    from kiro_crew.history import ConversationLog
    from kiro_crew.history_page_index import sidecar_path

    base_dir, key = transcript
    log = ConversationLog(base_dir=base_dir)
    log.read_messages_chained_page(key, limit=5)
    sidecar = sidecar_path(log._path(key))
    assert sidecar.is_file()
    assert log.delete_session(key)
    assert not sidecar.exists()


def test_a_rewrite_in_a_process_that_never_paged_carries_the_persisted_index(
    transcript, monkeypatch
):
    """A restart, then a metadata rewrite by a process holding no in-memory base (a
    resume that clears the close), then a page read: the sidecar is carried."""
    from kiro_crew.history import ConversationLog

    base_dir, key = transcript
    ConversationLog(base_dir=base_dir).read_messages_chained_page(key, limit=50)
    ConversationLog(base_dir=base_dir).update_metadata(key, {"title": "cold rewrite"})
    meter = _FrameMeter(monkeypatch)
    page = ConversationLog(base_dir=base_dir).read_messages_chained_page(key, limit=50)
    assert page.total == ROWS
    assert _contents(page)[-1].startswith(f"row-{ROWS - 1} ")
    assert meter.framed <= _bound(), meter.framed
