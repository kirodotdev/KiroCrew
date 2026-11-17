"""The channel backfill, resume picker and Webex listings read the listing index.

Each of these callers lists every session. On a gateway that is writing
transcripts, the scan's memo misses after every write and re-reads one row per
transcript on a shared worker thread; the index re-reads only the transcript that
changed, so each call costs the same at any history size.
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest
from webex_dispatch_helpers import (
    FakeClient,
    FakeCtx,
    FakeProvider,
    FakeSessions,
    _dispatcher,
    _inbound,
)

from kiro_crew.dashboard import channel_slots
from kiro_crew.history import ConversationLog
from kiro_crew.history_listing_index import SessionListingIndex
from kiro_crew.history_search import SessionCatalogProjection
from kiro_crew.messaging import session_resume

_SIZES = [600, 1200, 2400, 4704]
_BASE_NS = 1_700_000_000 * 10**9
_META = {"_type": "metadata", "created_at": "2026-01-01T00:00:00", "memory_mode": "persistent"}


def _stamp(seconds: int) -> int:
    return _BASE_NS + seconds * 10**9


def _log(tmp_path, files: int) -> ConversationLog:
    d = tmp_path / "sessions"
    d.mkdir()
    for i in range(files):
        path = d / f"dashboard_chat-{i}-{i}.jsonl"
        path.write_text(json.dumps({**_META, "title": f"t{i}"}) + "\n", encoding="utf-8")
        os.utime(path, ns=(_stamp(i), _stamp(i)))
    os.utime(d, ns=(_stamp(files + 1), _stamp(files + 1)))
    log = ConversationLog(base_dir=d)
    log._listing_index = SessionListingIndex(log, clock=lambda: 1000.0, submit=lambda _job: None)
    return log


def _count_row_reads(monkeypatch) -> list[object]:
    reads: list[object] = []
    real = SessionCatalogProjection._listing_row

    def counted(*args, **kwargs):
        reads.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(SessionCatalogProjection, "_listing_row", counted)
    return reads


async def _rows_read_by_second_call(tmp_path, monkeypatch, files: int, call) -> int:
    log = _log(tmp_path, files)
    await call(log)
    reads = _count_row_reads(monkeypatch)
    log.append("dashboard:chat-0-0", "user", "one more line")
    await call(log)
    return len(reads)


@pytest.mark.asyncio
@pytest.mark.parametrize("files", _SIZES)
async def test_a_channel_backfill_after_an_in_process_write_reads_one_row(
    tmp_path, monkeypatch, files: int
) -> None:
    monkeypatch.setattr(channel_slots, "configured_folder_name", lambda _ns: "Slack")

    async def folder_id(_state, _ns, _name):
        return "folder-1"

    monkeypatch.setattr(channel_slots, "folder_id_for_name", folder_id)

    async def backfill(log: ConversationLog) -> None:
        report = await channel_slots.backfill_channel_folder(
            SimpleNamespace(conversation_log=log), "slack"
        )
        assert report["reason"] == ""

    assert await _rows_read_by_second_call(tmp_path, monkeypatch, files, backfill) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("files", _SIZES)
async def test_a_resume_picker_after_an_in_process_write_reads_one_row(
    tmp_path, monkeypatch, files: int
) -> None:
    async def picker(log: ConversationLog) -> None:
        _, total, _ = await session_resume.resolve_session_choices(log, "", lambda t, _n: t)
        assert total == files

    assert await _rows_read_by_second_call(tmp_path, monkeypatch, files, picker) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("files", _SIZES)
async def test_a_resume_title_fallback_after_an_in_process_write_reads_one_row(
    tmp_path, monkeypatch, files: int
) -> None:
    async def fallback(log: ConversationLog) -> None:
        monkeypatch.setattr(log, "search_sessions", lambda _q, _limit: [])
        choices, _, _ = await session_resume.resolve_session_choices(log, "t1 t1", lambda t, _n: t)
        assert choices

    assert await _rows_read_by_second_call(tmp_path, monkeypatch, files, fallback) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("files", _SIZES)
async def test_a_webex_sessions_command_after_an_in_process_write_reads_one_row(
    tmp_path, monkeypatch, files: int
) -> None:
    d = _dispatcher(FakeSessions(FakeProvider([])), FakeCtx(), FakeClient())

    async def sessions_command(log: ConversationLog) -> None:
        d.conv_log = log
        await d.handle_message(_inbound("/sessions"))
        assert "No earlier conversations" in d.client.sent[-1][1]

    assert await _rows_read_by_second_call(tmp_path, monkeypatch, files, sessions_command) == 1
