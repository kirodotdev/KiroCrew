"""``store.delete_item`` and ``store.import_bundle`` must never run on the event loop.

Both mutations take the write lock with ``BEGIN IMMEDIATE`` (a concurrent writer
can park them for the connection's 10s busy_timeout) and both end in a full
rebuild of the entity graph -- ``_load_graph`` scans ``entities`` and
``entity_relations`` in their entirety, which grows linearly with the library.
Called straight from a coroutine body, either one freezes every task on the
gateway's single loop, and on a large profile outlasts the stall watchdog: the
process is killed and respawned into the same state.

Same two-layer shape as ``test_knowledge_delete_off_loop.py``, whose AST
helpers this module reuses:

* the ratchet pins every call site at once and fails if a future edit calls
  either mutation straight from a coroutine body anywhere in the package. It
  matches on the method NAME alone, so a same-named method on another object
  (``pack_transfer.import_bundle``, ``appearance_store.import_bundle``) called
  from a coroutine would also trip it -- a conservative false failure whose
  message names this store's remedy; read the flagged line before applying it;
* the behavioural tests drive the real HTTP handlers and assert the THREAD the
  store call lands on, so a refactor that keeps ``asyncio.to_thread`` in the
  source but hands it something already-invoked still fails. A third proves an
  exception raised inside the worker still reaches the handler's except arms as
  the same typed error the synchronous call raised.
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from test_knowledge_delete_off_loop import _on_loop_call_sites

from kiro_crew.dashboard.handlers import knowledge as kh
from kiro_crew.knowledge.store import KnowledgeBundleError, KnowledgeStore


def test_delete_item_is_never_called_on_the_event_loop():
    """Ratchet: hand it to a worker, never call it from a coroutine body."""
    offenders = _on_loop_call_sites("delete_item")
    assert offenders == [], (
        "store.delete_item runs on the event loop at:\n  "
        + "\n  ".join(offenders)
        + "\nOffload it: await asyncio.to_thread(store.delete_item, item_id). "
        "It takes the write lock with BEGIN IMMEDIATE and rebuilds the whole "
        "entity graph before returning, so on a large library it blocks the "
        "loop past the stall watchdog."
    )


def test_import_bundle_is_never_called_on_the_event_loop():
    """Ratchet: same rule for the bundle import, which writes even more rows."""
    offenders = _on_loop_call_sites("import_bundle")
    assert offenders == [], (
        "import_bundle runs on the event loop at:\n  "
        + "\n  ".join(offenders)
        + "\nOffload it: await asyncio.to_thread(store.import_bundle, body). "
        "A bundle inserts every item, entity and relation it carries inside "
        "one BEGIN IMMEDIATE and then rebuilds the entity graph, so the call "
        "scales with the bundle AND the library."
    )


def _make_app(store: KnowledgeStore) -> web.Application:
    app = web.Application()
    state = MagicMock()
    state.knowledge_store = store
    app["state"] = state
    app.router.add_delete("/api/knowledge/items/{id}", kh.delete_item)
    app.router.add_post("/api/knowledge/import", kh.import_bundle)
    return app


@pytest.mark.asyncio
async def test_delete_item_handler_runs_the_delete_off_the_loop_thread(tmp_path, monkeypatch):
    """The offload is real, and the SEL audit travels with the commit.

    The audit must run on the same worker as the mutation: a client disconnect
    cancels the coroutine at the ``await``, so an audit line placed after it is
    skippable by the caller -- a committed mutation with no SEL record.
    """
    store = KnowledgeStore(str(tmp_path / "knowledge.db"))
    try:
        source_id = store.add_source("src", "local_folder", str(tmp_path))
        item_id = store.add_item("title", "body", "doc", source_id=source_id)

        seen_threads: list[int] = []
        real = store.delete_item

        def recording(*args, **kwargs):
            seen_threads.append(threading.get_ident())
            return real(*args, **kwargs)

        monkeypatch.setattr(store, "delete_item", recording)

        sel_threads: list[int] = []
        monkeypatch.setattr(
            kh, "_sel_log", lambda tool, **kw: sel_threads.append(threading.get_ident())
        )

        async with TestClient(TestServer(_make_app(store))) as client:
            resp = await client.delete(f"/api/knowledge/items/{item_id}")
            assert resp.status == 200

        assert seen_threads, (
            "store.delete_item was never called -- this test no longer "
            "exercises the delete path and would pass vacuously"
        )
        assert threading.get_ident() not in seen_threads, (
            "store.delete_item ran on the event-loop thread; it must be handed "
            "to asyncio.to_thread"
        )
        assert sel_threads == seen_threads, (
            "the SEL audit did not run on the worker alongside the commit; an "
            "audit line after the await is skipped when the client disconnects "
            "and the cancellation lands post-commit"
        )
        assert store.get_item(item_id) is None, "the handler reported ok but the item survived"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_import_bundle_handler_runs_the_import_off_the_loop_thread(tmp_path, monkeypatch):
    """The offload is real for the import path, audit riding with the commit."""
    store = KnowledgeStore(str(tmp_path / "knowledge.db"))
    try:
        seen_threads: list[int] = []
        real = store.import_bundle

        def recording(*args, **kwargs):
            seen_threads.append(threading.get_ident())
            return real(*args, **kwargs)

        monkeypatch.setattr(store, "import_bundle", recording)

        sel_threads: list[int] = []
        monkeypatch.setattr(
            kh, "_sel_log", lambda tool, **kw: sel_threads.append(threading.get_ident())
        )

        bundle = {
            "items": [
                {
                    "id": "i1",
                    "title": "Imported",
                    "content": "hello",
                    "summary": "s",
                    "item_type": "note",
                }
            ]
        }
        async with TestClient(TestServer(_make_app(store))) as client:
            resp = await client.post("/api/knowledge/import", json=bundle)
            assert resp.status == 200
            assert (await resp.json())["items_imported"] == 1

        assert seen_threads, (
            "store.import_bundle was never called -- the import path was not "
            "reached and this test would pass vacuously"
        )
        assert threading.get_ident() not in seen_threads, (
            "store.import_bundle ran on the event-loop thread; it must be "
            "handed to asyncio.to_thread"
        )
        assert sel_threads == seen_threads, (
            "the success SEL audit did not run on the worker alongside the "
            "commit; an audit line after the await is skipped when the client "
            "disconnects and the cancellation lands post-commit"
        )
    finally:
        store.close()


@pytest.mark.asyncio
async def test_import_bundle_worker_error_still_reaches_the_typed_400(tmp_path, monkeypatch):
    """An error raised inside the worker propagates to the handler's arms.

    The handler's whole error contract lives in the ``except`` arms around the
    store call; moving the call to a worker thread must not change what they
    catch. ``KnowledgeBundleError`` is the store's own typed rejection, whose
    message the handler renders verbatim -- the arm most sensitive to the
    exception arriving as itself rather than wrapped.
    """
    store = KnowledgeStore(str(tmp_path / "knowledge.db"))
    try:

        def rejecting(*args, **kwargs):
            raise KnowledgeBundleError("'sources.properties' must be a JSON object")

        monkeypatch.setattr(store, "import_bundle", rejecting)

        bundle = {
            "items": [
                {"id": "i1", "title": "t", "content": "c", "summary": "s", "item_type": "note"}
            ]
        }
        async with TestClient(TestServer(_make_app(store))) as client:
            resp = await client.post("/api/knowledge/import", json=bundle)
            assert resp.status == 400
            body = await resp.json()
        assert body["code"] == "malformed_knowledge_bundle"
        assert "sources.properties" in body["error"]
    finally:
        store.close()
