"""End-to-end per-row ingest: SyncScheduler -> ingest_rows -> store -> query.

Drives a structured connector (fetch_rows) through the REAL SyncScheduler ->
IngestionPipeline.ingest_rows -> KnowledgeStore path, then queries via the REAL
HybridRetriever with a per-candidate binding resolver. Covers Root's required
scenarios: two rows with DIFFERENT permissions, partial failure (checkpoint not
advanced), update/duplicate, incremental keeps unchanged rows, full-snapshot
deletion, and checkpoint-only-on-full-success. No live embedding (embedder=None),
isolated tmp store.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.knowledge.acl import (
    AccessContext,
    LOCAL_PRINCIPAL,
    ProviderResourceRef,
    QueryPrincipal,
    RevalidationOutcome,
)
from kiro_crew.knowledge.connectors.base import BaseConnector
from kiro_crew.knowledge.ingestion import IngestionPipeline
from kiro_crew.knowledge.retrieval import HybridRetriever
from kiro_crew.knowledge.rows import SourceRow
from kiro_crew.knowledge.store import KnowledgeStore
from kiro_crew.knowledge.sync import SyncScheduler


@pytest.fixture()
def store(tmp_path):
    s = KnowledgeStore(str(tmp_path / "rows.db"))
    yield s
    s.close()


def _pipeline(store):
    extractor = MagicMock()
    extractor._pool = None
    # One chunk per row -> one extraction per chunk. chunk() returns a single
    # chunk carrying the whole row text, so total == 1 per row.
    extractor.extract_batch = AsyncMock(
        side_effect=lambda chunks: [
            {"category": "document", "summary": "s", "entities": []} for _ in chunks
        ]
    )
    chunker = MagicMock()
    chunker.chunk.side_effect = lambda text, **k: [
        {"content": text, "chunk_index": 0, "section_title": None}
    ]
    return IngestionPipeline(
        store=store, extractor=extractor, chunker=chunker,
        reader=MagicMock(), embedder=None,
    )


class _RowsConnector(BaseConnector):
    """A structured connector that returns pre-built (rows, snapshot, checkpoint)."""

    def __init__(self, plan):
        # plan: list of (rows, snapshot, checkpoint) returned on successive syncs
        self.plan = list(plan)
        self.i = 0

    def source_type(self):
        return "teststruct"

    def supports_rows(self):
        return True

    async def detect_changes(self, source):
        return True

    async def fetch(self, source):  # not used
        raise NotImplementedError

    def validate_config(self, config):
        return True, None

    async def fetch_rows(self, source):
        rows, snapshot, checkpoint = self.plan[min(self.i, len(self.plan) - 1)]
        self.i += 1
        return rows, snapshot, checkpoint


def _ref(provider, account, rid):
    return ProviderResourceRef(provider=provider, account=account, resource_id=rid)


async def _sync(store, connector, source_id):
    sched = SyncScheduler(store, _pipeline(store), {"teststruct": connector})
    return await sched.sync_source(source_id)


def _ids(res):
    return {r["id"] for r in res}


class _Resolver:
    def __init__(self, bindings):
        self.bindings = bindings

    def resolve(self, principal, provider, account):
        return self.bindings.get((provider, account))


class _Fresh:
    def revalidate(self, ctx, item_id, grant):
        return RevalidationOutcome.FRESH


@pytest.mark.asyncio
async def test_two_rows_different_permissions_each_isolated(store):
    src = store.add_source("Struct", "teststruct", "teststruct://s")
    rows = [
        SourceRow(key="r1", text="alpha content one", tenant="acme",
                  subjects=("sp-alice",), resource_ref=_ref("sharepoint", "tenA", "r1")),
        SourceRow(key="r2", text="alpha content two", tenant="acme",
                  subjects=("sp-bob",), resource_ref=_ref("sharepoint", "tenA", "r2")),
    ]
    conn = _RowsConnector([(rows, True, {"cursor": "c1"})])
    out = await _sync(store, conn, src)
    assert out["synced"] is True and out["items_created"] == 2

    # Each row landed as its OWN item group with its OWN grant + resource_ref.
    state = store.get_connector_row_state(src)
    assert set(state.keys()) == {"r1", "r2"}
    r1_item = state["r1"]["item_ids"][0]
    r2_item = state["r2"]["item_ids"][0]
    g1 = store.get_item_grants([r1_item])[r1_item]
    assert g1["managed"] is True
    assert ProviderResourceRef.from_json(g1["resource_ref"]).resource_id == "r1"

    resolver = _Resolver({("sharepoint", "tenA"):
                          AccessContext(subject="sp-alice", tenant="acme")})
    # alice sees r1 (granted to sp-alice) but NOT r2 (granted to sp-bob).
    res = _query_items(store, QueryPrincipal("alice"), resolver, [r1_item, r2_item], "alpha")
    assert r1_item in _ids(res)
    assert r2_item not in _ids(res)


def _query_items(store, principal, resolver, item_ids, term):
    r = HybridRetriever(store, revalidator=_Fresh(), binding_resolver=resolver)
    r._keyword_search = lambda *_a, **_k: [(iid, i + 1) for i, iid in enumerate(item_ids)]
    return r.search(term, limit=20, query_principal=principal)


@pytest.mark.asyncio
async def test_incremental_keeps_unchanged_rows(store):
    src = store.add_source("Struct", "teststruct", "teststruct://inc")
    r1 = SourceRow(key="r1", text="one body", tenant="t", subjects=("u1",),
                   resource_ref=_ref("sharepoint", "a", "r1"))
    r2 = SourceRow(key="r2", text="two body", tenant="t", subjects=("u2",),
                   resource_ref=_ref("sharepoint", "a", "r2"))
    # First full snapshot: both rows.
    # Second round INCREMENTAL: only r2 changes; r1 absent must NOT be deleted.
    r2b = SourceRow(key="r2", text="two body UPDATED", tenant="t", subjects=("u2",),
                    resource_ref=_ref("sharepoint", "a", "r2"))
    conn = _RowsConnector([([r1, r2], True, {"c": 1}), ([r2b], False, {"c": 2})])
    await _sync(store, conn, src)
    first = store.get_connector_row_state(src)
    assert set(first.keys()) == {"r1", "r2"}
    r1_item = first["r1"]["item_ids"][0]

    await _sync(store, conn, src)
    second = store.get_connector_row_state(src)
    # r1 (unchanged, absent from the incremental fetch) is STILL present.
    assert "r1" in second
    assert second["r1"]["item_ids"] == [r1_item]
    # r2 changed -> new content_hash.
    assert second["r2"]["content_hash"] != first["r2"]["content_hash"]


@pytest.mark.asyncio
async def test_full_snapshot_deletes_dropped_row(store):
    src = store.add_source("Struct", "teststruct", "teststruct://snap")
    r1 = SourceRow(key="r1", text="one body", tenant="t", subjects=("u1",),
                   resource_ref=_ref("sharepoint", "a", "r1"))
    r2 = SourceRow(key="r2", text="two body", tenant="t", subjects=("u2",),
                   resource_ref=_ref("sharepoint", "a", "r2"))
    # Round 2 is a FULL snapshot missing r2 -> r2 deleted.
    conn = _RowsConnector([([r1, r2], True, {"c": 1}), ([r1], True, {"c": 2})])
    await _sync(store, conn, src)
    assert set(store.get_connector_row_state(src).keys()) == {"r1", "r2"}
    r2_item = store.get_connector_row_state(src)["r2"]["item_ids"][0]
    out = await _sync(store, conn, src)
    assert out["rows_deleted"] == 1
    state = store.get_connector_row_state(src)
    assert set(state.keys()) == {"r1"}
    # r2's item AND its grant are gone.
    assert store.get_item(r2_item) is None
    assert store.get_item_grants([r2_item]) == {}


@pytest.mark.asyncio
async def test_partial_failure_does_not_advance_checkpoint(store):
    src = store.add_source("Struct", "teststruct", "teststruct://fail")
    r1 = SourceRow(key="r1", text="ok body", tenant="t", subjects=("u1",),
                   resource_ref=_ref("sharepoint", "a", "r1"))
    r2 = SourceRow(key="r2", text="bad body", tenant="t", subjects=("u2",),
                   resource_ref=_ref("sharepoint", "a", "r2"))
    conn = _RowsConnector([([r1, r2], True, {"cursor": "should-not-persist"})])

    # Make the SECOND row's ingest fail: patch the pipeline's ingest_text to raise
    # on r2's text.
    sched = SyncScheduler(store, _pipeline(store), {"teststruct": conn})
    real_ingest = sched.pipeline.ingest_text

    async def _flaky(text, *a, **k):
        if "bad body" in text:
            raise RuntimeError("boom")
        return await real_ingest(text, *a, **k)

    sched.pipeline.ingest_text = _flaky
    out = await sched.sync_source(src)
    assert out["synced"] is False  # not fully persisted
    assert out.get("checkpoint_advanced") is False
    # r1 persisted, r2 did not; checkpoint NOT written to properties.
    row = store.db.execute("SELECT properties FROM sources WHERE id = ?", (src,)).fetchone()
    import json as _json
    props = _json.loads(row["properties"] or "{}")
    assert "checkpoint" not in props
    state = store.get_connector_row_state(src)
    assert "r1" in state and "r2" not in state


@pytest.mark.asyncio
async def test_checkpoint_advances_on_full_success(store):
    src = store.add_source("Struct", "teststruct", "teststruct://ok")
    r1 = SourceRow(key="r1", text="fine body", tenant="t", subjects=("u1",),
                   resource_ref=_ref("sharepoint", "a", "r1"))
    conn = _RowsConnector([([r1], True, {"cursor": "cp-1"})])
    out = await _sync(store, conn, src)
    assert out["synced"] is True and out["checkpoint_advanced"] is True
    import json as _json
    row = store.db.execute("SELECT properties FROM sources WHERE id = ?", (src,)).fetchone()
    props = _json.loads(row["properties"] or "{}")
    assert props["checkpoint"] == {"cursor": "cp-1"}


@pytest.mark.asyncio
async def test_local_principal_cannot_see_ingested_managed_rows(store):
    src = store.add_source("Struct", "teststruct", "teststruct://loc")
    r1 = SourceRow(key="r1", text="secret body", tenant="t", subjects=("u1",),
                   resource_ref=_ref("sharepoint", "a", "r1"))
    conn = _RowsConnector([([r1], True, {"c": 1})])
    await _sync(store, conn, src)
    item = store.get_connector_row_state(src)["r1"]["item_ids"][0]
    resolver = _Resolver({("sharepoint", "a"): AccessContext(subject="u1", tenant="t")})
    # Local library principal: managed rows denied.
    res = _query_items(store, LOCAL_PRINCIPAL, resolver, [item], "secret")
    assert item not in _ids(res)


# --------------------------------------------------------------------------
# SourceRow guards: a missing grant must never become public, a cloud row must
# not escape managed provenance via a blank tenant or a managed=False opt-out.
# --------------------------------------------------------------------------

def test_sourcerow_subjects_required_no_public_default():
    # subjects has NO default: a connector that omits the grant gets a
    # constructor error, not a silently-public row.
    with pytest.raises(TypeError):
        SourceRow(key="r", text="t", tenant="acme",  # type: ignore[call-arg]
                  resource_ref=_ref("sharepoint", "a", "r"))


@pytest.mark.asyncio
async def test_sourcerow_empty_subjects_is_deny_all_not_public(store):
    # An EXPLICIT empty subject set is a valid deny-all; it must NOT read as
    # public. Ingest it and confirm nobody -- not even a matching-tenant subject,
    # not the local library -- can see it.
    src = store.add_source("Struct", "teststruct", "teststruct://deny")
    row = SourceRow(key="r", text="denied body", tenant="acme", subjects=(),
                    resource_ref=_ref("sharepoint", "a", "r"))
    conn = _RowsConnector([([row], True, {"c": 1})])
    await _sync(store, conn, src)
    item = store.get_connector_row_state(src)["r"]["item_ids"][0]
    resolver = _Resolver({("sharepoint", "a"): AccessContext(subject="u1", tenant="acme")})
    assert _query_items(store, QueryPrincipal("u1"), resolver, [item], "denied") == []
    assert _query_items(store, LOCAL_PRINCIPAL, resolver, [item], "denied") == []


def test_sourcerow_blank_tenant_rejected():
    with pytest.raises(ValueError):
        SourceRow(key="r", text="t", tenant="", subjects=("u1",),
                  resource_ref=_ref("sharepoint", "a", "r"))


def test_sourcerow_missing_resource_ref_rejected():
    with pytest.raises(ValueError):
        SourceRow(key="r", text="t", tenant="acme", subjects=("u1",))


def test_sourcerow_managed_is_always_true_no_bypass():
    row = SourceRow(key="r", text="t", tenant="acme", subjects=("u1",),
                    resource_ref=_ref("sharepoint", "a", "r"))
    assert row.managed is True
    # managed is a read-only property, not a settable field: a connector cannot
    # pass managed=False to make a cloud source bypass managed provenance.
    with pytest.raises(TypeError):
        SourceRow(key="r", text="t", tenant="acme", subjects=("u1",),  # type: ignore[call-arg]
                  resource_ref=_ref("sharepoint", "a", "r"), managed=False)
