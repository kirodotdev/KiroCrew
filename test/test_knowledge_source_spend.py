"""Ongoing per-source spend visibility on the knowledge sources list.

A folder source keeps drawing billed model calls sweep after sweep for as long as
files remain outstanding, and the add-source estimate is spent the moment the user
clicks through it. These tests pin the counters that make the *remaining* cost
visible on the sources list, and the invariant that an unrecognised file status
counts as outstanding work rather than as finished.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers.knowledge import list_sources
from kiro_crew.knowledge.folder_watcher import DEFAULT_MAX_FILES
from kiro_crew.knowledge.spend import source_spend
from kiro_crew.knowledge.store import KnowledgeStore


@pytest.fixture()
def store(tmp_path):
    s = KnowledgeStore(str(tmp_path / "spend.db"))
    yield s
    s.close()


def _file_state(store, source_id: str, file_path: str, status: str,
                *, mtime: float = 1.0) -> None:
    store.db.execute(
        "INSERT INTO folder_file_state (source_id, file_path, status, mtime, last_seen) "
        "VALUES (?, ?, ?, ?, '2026-01-01T00:00:00Z')",
        (source_id, file_path, status, mtime))
    store.db.commit()


def _rows(store, uri: str | None = None) -> list[dict]:
    """The source rows in the shape ``list_sources`` hands to the aggregation."""
    sql = ("SELECT s.*, COALESCE(c.cnt, 0) AS item_count FROM sources s "
           "LEFT JOIN (SELECT source_id, COUNT(*) AS cnt FROM items GROUP BY source_id) c "
           "ON s.id = c.source_id")
    if uri:
        return [dict(r) for r in store.db.execute(sql + " WHERE s.uri = ?", (uri,)).fetchall()]
    return [dict(r) for r in store.db.execute(sql).fetchall()]


class TestSourceSpendAggregation:
    def test_progress_counters_partition_the_files(self, store, tmp_path):
        sid = store.add_source(name="repo", source_type="local_folder",
                               uri=str(tmp_path), properties={})
        for name, status in (("a.md", "done"), ("b.md", "deduped"), ("c.md", "failed"),
                             ("d.md", "skipped"), ("e.md", "pending"), ("f.md", "scanning")):
            (tmp_path / name).write_text("body")
            _file_state(store, sid, str(tmp_path / name), status)

        spend = source_spend(store, _rows(store))[sid]
        assert spend["files_total"] == 6
        # 'deduped' is a file that was ingested, so it counts as done, not as failed.
        assert spend["files_done"] == 2
        assert spend["files_failed"] == 1
        assert spend["files_skipped"] == 1
        # 'scanning' is in flight, not finished.
        assert spend["files_pending"] == 2
        parts = (spend["files_done"] + spend["files_failed"]
                 + spend["files_skipped"] + spend["files_pending"])
        assert parts == spend["files_total"]

    def test_unknown_status_counts_as_outstanding_not_as_done(self, store, tmp_path):
        # A future writer's status must not be silently reported as finished work --
        # under-reporting remaining spend is the failure this whole view exists to fix.
        sid = store.add_source(name="repo", source_type="local_folder",
                               uri=str(tmp_path), properties={})
        (tmp_path / "a.md").write_text("body")
        _file_state(store, sid, str(tmp_path / "a.md"), "quarantined")

        spend = source_spend(store, _rows(store))[sid]
        assert spend["files_pending"] == 1
        assert spend["files_done"] == 0
        assert spend["estimated_llm_calls_remaining"] > 0

    def test_remaining_calls_cover_only_outstanding_files(self, store, tmp_path):
        sid = store.add_source(name="repo", source_type="local_folder",
                               uri=str(tmp_path), properties={})
        # Two chunks' worth of bytes each, so the estimate is not trivially the
        # file count.
        for name in ("done.md", "todo.md"):
            (tmp_path / name).write_text("word " * 2000)
        _file_state(store, sid, str(tmp_path / "done.md"), "done")
        _file_state(store, sid, str(tmp_path / "todo.md"), "pending")

        spend = source_spend(store, _rows(store))[sid]
        # One extraction call per chunk plus one summary call for the one file left.
        # An identically-sized finished file is not charged again.
        chunks = spend["estimated_llm_calls_remaining"] - 1
        assert chunks >= 2
        (tmp_path / "todo.md").write_text("word " * 4000)
        bigger = source_spend(store, _rows(store))[sid]
        assert bigger["estimated_llm_calls_remaining"] > spend["estimated_llm_calls_remaining"]

    def test_nothing_outstanding_owes_nothing(self, store, tmp_path):
        sid = store.add_source(name="repo", source_type="local_folder",
                               uri=str(tmp_path), properties={})
        (tmp_path / "a.md").write_text("word " * 2000)
        _file_state(store, sid, str(tmp_path / "a.md"), "done")

        spend = source_spend(store, _rows(store))[sid]
        assert spend["estimated_llm_calls_remaining"] == 0
        assert spend["files_pending"] == 0

    def test_a_source_at_its_file_cap_owes_nothing_more(self, store, tmp_path):
        # The cap bounds the source's whole ingestion. Charging for files past it
        # would report spend that is never going to happen.
        sid = store.add_source(name="repo", source_type="local_folder", uri=str(tmp_path),
                               properties={"max_files": 1})
        for name in ("done.md", "todo.md"):
            (tmp_path / name).write_text("word " * 2000)
        _file_state(store, sid, str(tmp_path / "done.md"), "done")
        _file_state(store, sid, str(tmp_path / "todo.md"), "pending")

        spend = source_spend(store, _rows(store))[sid]
        assert spend["files_pending"] == 1
        assert spend["estimated_llm_calls_remaining"] == 0

    def test_hostile_properties_do_not_raise_into_the_endpoint(self, store, tmp_path):
        # properties is user-editable JSON reachable from the add-source body, and a
        # raise here is a 500 on the list every dashboard visit makes.
        sid = store.add_source(name="repo", source_type="local_folder", uri=str(tmp_path),
                               properties={"max_files": 1e309})
        (tmp_path / "a.md").write_text("body")
        _file_state(store, sid, str(tmp_path / "a.md"), "pending")

        rows = _rows(store)
        rows[0]["properties"] = "{not json"
        spend = source_spend(store, rows)[sid]
        assert spend["estimated_llm_calls_remaining"] > 0

    def test_embedded_chunks_are_counted_and_unembedded_ones_are_not(self, store, tmp_path):
        sid = store.add_source(name="repo", source_type="local_folder",
                               uri=str(tmp_path), properties={})
        store.add_item(title="embedded", content="a", item_type="note",
                       source_id=sid, embedding=b"\x00\x01")
        store.add_item(title="bare", content="b", item_type="note", source_id=sid)

        spend = source_spend(store, _rows(store))[sid]
        assert spend["chunks_embedded"] == 1

    def test_a_source_with_no_file_state_reports_zeroes(self, store, tmp_path):
        # An uploaded file or an aggregate artifact source has no per-file rows.
        # Zero queued work is the truth for it, not a missing block.
        path = tmp_path / "note.md"
        path.write_text("body")
        sid = store.add_source(name="note", source_type="local_file", uri=str(path))

        spend = source_spend(store, _rows(store))[sid]
        assert spend["files_total"] == 0
        assert spend["estimated_llm_calls_remaining"] == 0

    def test_counters_do_not_bleed_between_sources(self, store, tmp_path):
        a_dir, b_dir = tmp_path / "a", tmp_path / "b"
        a_dir.mkdir()
        b_dir.mkdir()
        a = store.add_source(name="a", source_type="local_folder", uri=str(a_dir), properties={})
        b = store.add_source(name="b", source_type="local_folder", uri=str(b_dir), properties={})
        (a_dir / "x.md").write_text("word " * 2000)
        (b_dir / "y.md").write_text("body")
        _file_state(store, a, str(a_dir / "x.md"), "pending")
        _file_state(store, b, str(b_dir / "y.md"), "done")

        spend = source_spend(store, _rows(store))
        assert spend[a]["files_pending"] == 1
        assert spend[a]["estimated_llm_calls_remaining"] > 0
        assert spend[b]["files_pending"] == 0
        assert spend[b]["estimated_llm_calls_remaining"] == 0

    def test_no_sources_is_not_a_query_over_an_empty_in_list(self, store):
        # An empty IN () is a SQL syntax error, so the empty case has to short-circuit.
        assert source_spend(store, []) == {}

    def test_default_cap_leaves_headroom_for_a_small_folder(self, store, tmp_path):
        sid = store.add_source(name="repo", source_type="local_folder",
                               uri=str(tmp_path), properties={})
        (tmp_path / "a.md").write_text("body")
        _file_state(store, sid, str(tmp_path / "a.md"), "pending")
        assert DEFAULT_MAX_FILES > 1
        assert source_spend(store, _rows(store))[sid]["estimated_llm_calls_remaining"] >= 2


class TestListSourcesCarriesTheSpendBlock:
    def _app(self, store):
        app = web.Application()
        state = MagicMock()
        state.knowledge_store = store
        app["state"] = state
        app.router.add_get("/api/knowledge/sources", list_sources)
        return app

    @pytest.mark.asyncio
    async def test_every_source_carries_its_spend_counters(self, store, tmp_path):
        sid = store.add_source(name="repo", source_type="local_folder",
                               uri=str(tmp_path), properties={})
        for name, status in (("a.md", "done"), ("b.md", "pending")):
            (tmp_path / name).write_text("word " * 2000)
            _file_state(store, sid, str(tmp_path / name), status)

        async with TestClient(TestServer(self._app(store))) as client:
            resp = await client.get("/api/knowledge/sources")
            assert resp.status == 200
            data = await resp.json()

        assert len(data) == 1
        spend = data[0]["spend"]
        assert spend["files_total"] == 2
        assert spend["files_done"] == 1
        assert spend["files_pending"] == 1
        assert spend["estimated_llm_calls_remaining"] > 0

    @pytest.mark.asyncio
    async def test_the_uri_filtered_path_carries_it_too(self, store, tmp_path):
        folder = tmp_path / "repo"
        folder.mkdir()
        sid = store.add_source(name="repo", source_type="local_folder",
                               uri=str(folder), properties={})
        (folder / "a.md").write_text("body")
        _file_state(store, sid, str(folder / "a.md"), "pending")

        async with TestClient(TestServer(self._app(store))) as client:
            resp = await client.get("/api/knowledge/sources",
                                    params={"uri": str(folder)})
            data = await resp.json()

        assert len(data) == 1
        assert data[0]["spend"]["files_pending"] == 1

    @pytest.mark.asyncio
    async def test_the_block_survives_a_json_round_trip_for_every_source(self, store, tmp_path):
        # The frontend reads spend off every row, so a source missing the key would
        # be an undefined dereference rather than a zero.
        path = tmp_path / "note.md"
        path.write_text("body")
        store.add_source(name="note", source_type="local_file", uri=str(path))
        folder = tmp_path / "repo"
        folder.mkdir()
        store.add_source(name="repo", source_type="local_folder",
                         uri=str(folder), properties={})

        async with TestClient(TestServer(self._app(store))) as client:
            data = await (await client.get("/api/knowledge/sources")).json()

        assert len(data) == 2
        for row in data:
            assert set(json.loads(json.dumps(row["spend"]))) == {
                "files_total", "files_done", "files_failed", "files_skipped",
                "files_pending", "chunks_embedded", "estimated_llm_calls_remaining"}
