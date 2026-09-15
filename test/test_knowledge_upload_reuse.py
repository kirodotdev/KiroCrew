"""The upload handler's re-used source row is owed an ingest from the response on.

An upload whose name matches an existing ``upload://`` row re-uses that row and
returns its id. The row is marked ``syncing`` BEFORE the background task is
scheduled, so an itemless row an earlier upload left in a terminal status is
outside the orphan sweep's predicate for the whole gap and the id the client
holds stays the row the ingest fills. Under the gate the task re-reads the row
and, when it is gone (a delete landed in the gap), skips the ingest instead of
re-creating a source behind the delete, returning the import admission.
"""

from __future__ import annotations

import asyncio
import sqlite3
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import FormData, web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers import knowledge as kn
from kiro_crew.knowledge.store import IngestionGate, KnowledgeStore


@pytest.fixture()
def store(tmp_path):
    s = KnowledgeStore(str(tmp_path / "k.db"))
    yield s
    s.close()


class _GateProbe:
    """Counts holds of a real IngestionGate so a test can assert that the
    ingest ran under a hold and that every hold was released. The handler and
    its hand-off helper both enter (the real pipeline folds those into one
    per-task hold; this probe just counts), then the task enters for itself."""

    def __init__(self) -> None:
        self.gate = IngestionGate()
        self.depth = 0
        self.events: list[str] = []

    def held(self) -> bool:
        return self.depth > 0


class _AsyncGate:
    def __init__(self, probe: _GateProbe) -> None:
        self._cm = probe.gate.ingestion_in_flight()
        self._probe = probe

    async def __aenter__(self):
        self._cm.__enter__()
        self._probe.depth += 1

    async def __aexit__(self, *exc):
        self._probe.depth -= 1
        self._cm.__exit__(None, None, None)


def _pipeline(store: KnowledgeStore, probe: _GateProbe) -> MagicMock:
    pipeline = MagicMock()
    pipeline.reserve_import_budget = AsyncMock(return_value=7)
    pipeline.release_import_budget = MagicMock()
    pipeline.ingestion_in_flight = MagicMock(side_effect=lambda: _AsyncGate(probe))

    async def _ingest_file(path, **kwargs):
        probe.events.append(
            f"ingest:{kwargs.get('source_id')}:{'held' if probe.held() else 'open'}"
        )
        return "job"

    pipeline.ingest_file = AsyncMock(side_effect=_ingest_file)
    return pipeline


def _make_app(store: KnowledgeStore, pipeline: MagicMock) -> web.Application:
    app = web.Application()
    state = MagicMock()
    state.knowledge_store = store
    app["state"] = state
    app["knowledge_pipeline"] = pipeline
    app.router.add_post("/api/knowledge/ingest", kn.ingest_file)
    return app


async def _upload(client: TestClient, name: str) -> dict:
    form = FormData()
    form.add_field("file", b"# hello\n", filename=name, content_type="text/markdown")
    resp = await client.post("/api/knowledge/ingest", data=form)
    assert resp.status == 200, await resp.text()
    return await resp.json()


async def _drain(app: web.Application) -> None:
    tasks = list(app.get("_bg_tasks", ()))
    if tasks:
        await asyncio.gather(*tasks)


def _status(store: KnowledgeStore, source_id: str) -> str | None:
    row = store.db.execute("SELECT sync_status FROM sources WHERE id = ?", (source_id,)).fetchone()
    return None if row is None else row["sync_status"]


@pytest.mark.asyncio
async def test_a_reused_row_is_marked_syncing_before_the_response(store, monkeypatch):
    monkeypatch.setattr(kn, "_sel_log", lambda *a, **k: None)
    probe = _GateProbe()
    pipeline = _pipeline(store, probe)
    existing = store.add_source("notes.md", "local_file", "upload://notes.md")
    store.db.execute("UPDATE sources SET sync_status = 'error' WHERE id = ?", (existing,))
    store.db.commit()
    app = _make_app(store, pipeline)
    async with TestClient(TestServer(app)) as client:
        # The status write happens on the handler's path, before the task runs.
        body = await _upload(client, "notes.md")
        assert body["source_id"] == existing
        assert _status(store, existing) == "syncing"
        await _drain(app)
    assert probe.events == [f"ingest:{existing}:held"]
    assert probe.depth == 0


@pytest.mark.asyncio
async def test_a_failed_syncing_stamp_does_not_discard_the_re_upload(store, monkeypatch):
    """The handler's 'syncing' stamp on a re-used row is a hint. When it raises
    (a contended writer lock), the upload is still handed off: the alternative is
    the outer except unlinking the only server-side copy and answering 500."""
    monkeypatch.setattr(kn, "_sel_log", lambda *a, **k: None)
    probe = _GateProbe()
    pipeline = _pipeline(store, probe)
    existing = store.add_source("notes.md", "local_file", "upload://notes.md")
    store.db.execute("UPDATE sources SET sync_status = 'error' WHERE id = ?", (existing,))
    store.db.commit()
    real = kn._set_sync_status
    calls = {"n": 0}

    def _flaky(store_, source_id, status):
        calls["n"] += 1
        if calls["n"] == 1:  # the handler's stamp, before the response
            raise sqlite3.OperationalError("database is locked")
        real(store_, source_id, status)

    monkeypatch.setattr(kn, "_set_sync_status", _flaky)
    app = _make_app(store, pipeline)
    async with TestClient(TestServer(app)) as client:
        body = await _upload(client, "notes.md")  # asserts 200, not 500
        assert body["source_id"] == existing
        await _drain(app)
    assert probe.events == [f"ingest:{existing}:held"]
    assert pipeline.release_import_budget.call_count == 0
    assert probe.depth == 0


@pytest.mark.asyncio
async def test_a_failed_re_read_releases_the_import_admission(store, monkeypatch):
    """The task's re-read runs between reserving the admission and handing it to
    ingest_file. When it raises, the row is stamped 'error' AND the reservation is
    released -- otherwise its placeholder stays open in the window until restart
    and refuses imports that should pass."""
    monkeypatch.setattr(kn, "_sel_log", lambda *a, **k: None)
    probe = _GateProbe()
    pipeline = _pipeline(store, probe)
    existing = store.add_source("notes.md", "local_file", "upload://notes.md")

    def _locked(store_, source_id):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(kn, "_source_row", _locked)
    app = _make_app(store, pipeline)
    async with TestClient(TestServer(app)) as client:
        body = await _upload(client, "notes.md")
        assert body["source_id"] == existing
        await _drain(app)
    assert probe.events == []
    pipeline.release_import_budget.assert_called_once_with(7)
    assert _status(store, existing) == "error"
    assert probe.depth == 0


@pytest.mark.asyncio
async def test_a_row_deleted_in_the_gap_is_not_recreated(store, monkeypatch):
    monkeypatch.setattr(kn, "_sel_log", lambda *a, **k: None)
    probe = _GateProbe()
    pipeline = _pipeline(store, probe)
    existing = store.add_source("notes.md", "local_file", "upload://notes.md")
    app = _make_app(store, pipeline)

    original_create_task = asyncio.create_task

    def _delete_then_schedule(coro, **kwargs):
        # A user delete lands after the response's lookup and before the task runs.
        store.db.execute("DELETE FROM sources WHERE id = ?", (existing,))
        store.db.commit()
        return original_create_task(coro, **kwargs)

    monkeypatch.setattr(kn.asyncio, "create_task", _delete_then_schedule)
    async with TestClient(TestServer(app)) as client:
        body = await _upload(client, "notes.md")
        assert body["source_id"] == existing
        await _drain(app)
    assert store.get_source_by_uri("upload://notes.md") is None
    pipeline.ingest_file.assert_not_awaited()
    pipeline.release_import_budget.assert_called_once_with(7)
    assert probe.events == []
    assert probe.depth == 0


@pytest.mark.asyncio
async def test_two_first_time_uploads_of_one_name_share_the_winning_row(store, monkeypatch):
    """Both claims miss the lookup; the UNIQUE uri lets one insert through and
    the other re-reads and shares the row instead of failing the upload."""
    monkeypatch.setattr(kn, "_sel_log", lambda *a, **k: None)
    probe = _GateProbe()
    pipeline = _pipeline(store, probe)
    real_add = store.add_source

    def _racing_add(**kwargs):
        # The other upload's insert lands between this claim's lookup and add.
        real_add(**kwargs)
        return real_add(**kwargs)

    monkeypatch.setattr(store, "add_source", _racing_add)
    app = _make_app(store, pipeline)
    async with TestClient(TestServer(app)) as client:
        body = await _upload(client, "notes.md")
        await _drain(app)
    row = store.get_source_by_uri("upload://notes.md")
    assert row is not None and body["source_id"] == row["id"]
    assert _status(store, row["id"]) in ("syncing", "synced")
    assert probe.events == [f"ingest:{row['id']}:held"]
    assert probe.depth == 0


@pytest.mark.asyncio
async def test_manual_resync_holds_the_gate_until_the_task_has_claimed(
    store, monkeypatch, tmp_path
):
    """`sync_source` reads the row before the task exists and the task claims
    'syncing' itself; an itemless row in a terminal status is reclaimable in
    between. The handler holds the ingestion gate across the claim, so a
    maintenance window cannot open while the claim is undecided, and the task's
    own ingest still takes its own hold (the creating hold is not inherited)."""
    monkeypatch.setattr(kn, "_sel_log", lambda *a, **k: None)
    from kiro_crew.knowledge.ingestion import IngestionPipeline

    path = tmp_path / "notes.md"
    path.write_text("# notes\n")
    sid = store.add_source(name="notes.md", source_type="local_file", uri=str(path), properties={})
    store.update_source(sid, sync_status="error")
    pipeline = IngestionPipeline.__new__(IngestionPipeline)
    pipeline.store = store
    seen: list[str] = []
    real_claim = kn._claim_sync

    def _claim(store_, source_id):
        with store.maintenance_window(timeout=0.05) as quiescent:
            seen.append(f"claim:{quiescent}")
        return real_claim(store_, source_id)

    async def _ingest(*_a, **_kw):
        # Let the handler leave its hold first, then take the task's own: the
        # task was created with a detached context, so this entry is a real
        # acquisition (a window cannot open), not a nested no-op.
        for _ in range(3):
            await asyncio.sleep(0)
        with store.maintenance_window(timeout=0.05) as quiescent:
            seen.append(f"before-hold:{quiescent}")
        async with pipeline._ingestion_in_flight():
            with store.maintenance_window(timeout=0.05) as quiescent:
                seen.append(f"ingest:{quiescent}")
        return "job"

    monkeypatch.setattr(kn, "_claim_sync", _claim)
    pipeline.ingest_file = _ingest  # type: ignore[method-assign]

    app = web.Application()
    state = MagicMock()
    state.knowledge_store = store
    app["state"] = state
    app["knowledge_pipeline"] = pipeline
    app["knowledge_sync"] = MagicMock(get_connector=MagicMock(return_value=None))
    app.router.add_post("/api/knowledge/sources/{id}/sync", kn.sync_source)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(f"/api/knowledge/sources/{sid}/sync")
        assert resp.status == 200, await resp.text()
        await _drain(app)
    assert seen == ["claim:False", "before-hold:True", "ingest:False"]
    with store.maintenance_window(timeout=0.05) as quiescent:
        assert quiescent is True, "a hold outlived the task"
    assert _status(store, sid) == "synced"
