"""The Shared Crew reads list sessions from the listing index, so their cost is flat.

``GET /api/shared-crew/people``, ``/sessions`` and ``/access`` all list the catalog
through ``routes_identity._catalog_rows``. On a gateway that is writing transcripts,
the scan's memo misses after every write and re-reads one row per transcript on a
shared worker thread; several tabs opening these reads at once then fill the
default executor. The index re-reads only the transcript that changed.
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew.history import ConversationLog
from kiro_crew.history_listing_index import SessionListingIndex
from kiro_crew.history_search import SessionCatalogProjection
from kiro_crew.shared_crew import routes_identity

_BASE_NS = 1_700_000_000 * 10**9
_META = {"_type": "metadata", "created_at": "2026-01-01T00:00:00"}


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


def _request(log: ConversationLog) -> web.Request:
    app = web.Application()
    app["state"] = SimpleNamespace(conversation_log=log)
    return make_mocked_request("GET", routes_identity.PEOPLE_PATH, app=app)


@pytest.mark.asyncio
@pytest.mark.parametrize("files", [600, 1200, 2400, 4704])
async def test_a_catalog_read_after_an_in_process_write_reads_one_row_at_any_size(
    tmp_path, monkeypatch, files: int
) -> None:
    log = _log(tmp_path, files)
    request = _request(log)
    await routes_identity._catalog_rows(request)
    reads: list[object] = []
    real = SessionCatalogProjection._listing_row

    def counted(*args, **kwargs):
        reads.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(SessionCatalogProjection, "_listing_row", counted)

    log.append("dashboard:chat-0-0", "user", "one more line")
    rows = await routes_identity._catalog_rows(request)

    assert (len(rows), len(reads)) == (files, 1)
