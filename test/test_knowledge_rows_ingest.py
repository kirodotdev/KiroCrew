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
    LOCAL_PRINCIPAL,
    AccessContext,
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
        store=store,
        extractor=extractor,
        chunker=chunker,
        reader=MagicMock(),
        embedder=None,
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
        SourceRow(
            key="r1",
            text="alpha content one",
            tenant="acme",
            subjects=("sp-alice",),
            resource_ref=_ref("sharepoint", "tenA", "r1"),
        ),
        SourceRow(
            key="r2",
            text="alpha content two",
            tenant="acme",
            subjects=("sp-bob",),
            resource_ref=_ref("sharepoint", "tenA", "r2"),
        ),
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

    resolver = _Resolver({("sharepoint", "tenA"): AccessContext(subject="sp-alice", tenant="acme")})
    # alice sees r1 (granted to sp-alice) but NOT r2 (granted to sp-bob).
    res = _query_items(store, QueryPrincipal("alice"), resolver, [r1_item, r2_item], "alpha")
    assert r1_item in _ids(res)
    assert r2_item not in _ids(res)


def _query_items(store, principal, resolver, item_ids, term):
    r = HybridRetriever(store, revalidator=_Fresh(), binding_resolver=resolver)
    r._keyword_search = lambda *_a, **_k: [(iid, i + 1) for i, iid in enumerate(item_ids)]
    return r.search(term, limit=20, query_principal=principal)


@pytest.mark.asyncio
async def test_handler_injected_runner_drives_real_chain(store, monkeypatch):
    """The shared handler's registration seam constructs a connector WITH an
    injected runner (the real vendor construction contract), and the SAME
    registered instance drives the real SyncScheduler -> fetch_rows ->
    ingest_rows -> store -> ACL query chain.

    This is the end-to-end proof that the injection wiring produces a working
    connector, not just an app[] presence flag. The live-transport leaf (W01
    controlled TLS) is the injected runner's job; here it is a scripted runner
    (the connectors' own documented offline verification path), because the real
    W01 control-plane executor and the vendor transport modules are not on this
    tree yet.
    """
    import sys
    import types

    from kiro_crew.dashboard.handlers.knowledge import _register_optional_connector

    # A connector whose ctor takes the injected runner_factory and whose
    # fetch_rows uses it to produce rows -- the shape of the real vendors.
    class _InjectedConnector(BaseConnector):
        def __init__(self, operations_factory):
            assert operations_factory is not None  # refuses without a runner
            self._ops = operations_factory

        def source_type(self):
            return "teststruct"

        def supports_rows(self):
            return True

        async def detect_changes(self, source):
            return True

        async def fetch(self, source):
            raise NotImplementedError

        def validate_config(self, config):
            return True, None

        async def fetch_rows(self, source):
            # Drive the injected runner exactly as a real connector would:
            # operations_factory(source) -> ops; ops() -> the raw records.
            ops = self._ops(source)
            rows = [
                SourceRow(
                    key=k,
                    text=t,
                    tenant="acme",
                    subjects=(subj,),
                    resource_ref=_ref("sharepoint", "tenA", k),
                )
                for (k, t, subj) in ops()
            ]
            return rows, True, {"cursor": "c1"}

    mod = types.ModuleType("kiro_crew._injected_vendor_mod")
    mod._InjectedConnector = _InjectedConnector  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "kiro_crew._injected_vendor_mod", mod)

    # The host-installed runner factory: given a source, returns a scripted
    # "ops" that yields two records (stands in for the W01-backed DriveOperations
    # whose calls run through the control-plane executor's controlled TLS).
    def _runner_factory(source):
        return lambda: [("k1", "alpha one", "sp-alice"), ("k2", "alpha two", "sp-bob")]

    connectors: dict = {}
    ok = _register_optional_connector(
        connectors,
        "kiro_crew._injected_vendor_mod",
        "_InjectedConnector",
        runner_factory=_runner_factory,
        inject_kw="operations_factory",
    )
    assert ok is True
    conn = connectors["teststruct"]

    # Drive the REGISTERED instance through the real scheduler -> store chain.
    src = store.add_source("Struct", "teststruct", "teststruct://inj")
    out = await _sync(store, conn, src)
    assert out["synced"] is True and out["items_created"] == 2

    state = store.get_connector_row_state(src)
    assert set(state.keys()) == {"k1", "k2"}
    k1 = state["k1"]["item_ids"][0]
    k2 = state["k2"]["item_ids"][0]
    g1 = store.get_item_grants([k1])[k1]
    assert g1["managed"] is True
    assert ProviderResourceRef.from_json(g1["resource_ref"]).resource_id == "k1"

    # ACL query: alice (bound to sharepoint/tenA) sees k1, not k2 (bob's).
    resolver = _Resolver({("sharepoint", "tenA"): AccessContext(subject="sp-alice", tenant="acme")})
    res = _query_items(store, QueryPrincipal("alice"), resolver, [k1, k2], "alpha")
    assert k1 in _ids(res)
    assert k2 not in _ids(res)


@pytest.mark.asyncio
async def test_incremental_keeps_unchanged_rows(store):
    src = store.add_source("Struct", "teststruct", "teststruct://inc")
    r1 = SourceRow(
        key="r1",
        text="one body",
        tenant="t",
        subjects=("u1",),
        resource_ref=_ref("sharepoint", "a", "r1"),
    )
    r2 = SourceRow(
        key="r2",
        text="two body",
        tenant="t",
        subjects=("u2",),
        resource_ref=_ref("sharepoint", "a", "r2"),
    )
    # First full snapshot: both rows.
    # Second round INCREMENTAL: only r2 changes; r1 absent must NOT be deleted.
    r2b = SourceRow(
        key="r2",
        text="two body UPDATED",
        tenant="t",
        subjects=("u2",),
        resource_ref=_ref("sharepoint", "a", "r2"),
    )
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
    r1 = SourceRow(
        key="r1",
        text="one body",
        tenant="t",
        subjects=("u1",),
        resource_ref=_ref("sharepoint", "a", "r1"),
    )
    r2 = SourceRow(
        key="r2",
        text="two body",
        tenant="t",
        subjects=("u2",),
        resource_ref=_ref("sharepoint", "a", "r2"),
    )
    # The second sync is a FULL snapshot missing r2 -> r2 deleted.
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
    r1 = SourceRow(
        key="r1",
        text="ok body",
        tenant="t",
        subjects=("u1",),
        resource_ref=_ref("sharepoint", "a", "r1"),
    )
    r2 = SourceRow(
        key="r2",
        text="bad body",
        tenant="t",
        subjects=("u2",),
        resource_ref=_ref("sharepoint", "a", "r2"),
    )
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
    r1 = SourceRow(
        key="r1",
        text="fine body",
        tenant="t",
        subjects=("u1",),
        resource_ref=_ref("sharepoint", "a", "r1"),
    )
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
    r1 = SourceRow(
        key="r1",
        text="secret body",
        tenant="t",
        subjects=("u1",),
        resource_ref=_ref("sharepoint", "a", "r1"),
    )
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
        SourceRow(
            key="r",
            text="t",
            tenant="acme",  # type: ignore[call-arg]
            resource_ref=_ref("sharepoint", "a", "r"),
        )


@pytest.mark.asyncio
async def test_sourcerow_empty_subjects_is_deny_all_not_public(store):
    # An EXPLICIT empty subject set is a valid deny-all; it must NOT read as
    # public. Ingest it and confirm nobody -- not even a matching-tenant subject,
    # not the local library -- can see it.
    src = store.add_source("Struct", "teststruct", "teststruct://deny")
    row = SourceRow(
        key="r",
        text="denied body",
        tenant="acme",
        subjects=(),
        resource_ref=_ref("sharepoint", "a", "r"),
    )
    conn = _RowsConnector([([row], True, {"c": 1})])
    await _sync(store, conn, src)
    item = store.get_connector_row_state(src)["r"]["item_ids"][0]
    resolver = _Resolver({("sharepoint", "a"): AccessContext(subject="u1", tenant="acme")})
    assert _query_items(store, QueryPrincipal("u1"), resolver, [item], "denied") == []
    assert _query_items(store, LOCAL_PRINCIPAL, resolver, [item], "denied") == []


def test_sourcerow_blank_tenant_rejected():
    with pytest.raises(ValueError):
        SourceRow(
            key="r",
            text="t",
            tenant="",
            subjects=("u1",),
            resource_ref=_ref("sharepoint", "a", "r"),
        )


def test_sourcerow_missing_resource_ref_rejected():
    with pytest.raises(ValueError):
        SourceRow(key="r", text="t", tenant="acme", subjects=("u1",))


def test_sourcerow_managed_is_always_true_no_bypass():
    row = SourceRow(
        key="r",
        text="t",
        tenant="acme",
        subjects=("u1",),
        resource_ref=_ref("sharepoint", "a", "r"),
    )
    assert row.managed is True
    # managed is a read-only property, not a settable field: a connector cannot
    # pass managed=False to make a cloud source bypass managed provenance.
    with pytest.raises(TypeError):
        SourceRow(
            key="r",
            text="t",
            tenant="acme",
            subjects=("u1",),  # type: ignore[call-arg]
            resource_ref=_ref("sharepoint", "a", "r"),
            managed=False,
        )


# --------------------------------------------------------------------------
# ingest_rows data-integrity guards: duplicate keys, ACL-only regrant, a
# swallowed chunk failure, and grant+ledger atomicity.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_duplicate_row_key_in_batch_does_not_orphan_first_group(store):
    # Two rows sharing a key (an object appearing on two overlapping fetch
    # pages) must NOT both ingest: the second's state write would replace the
    # first's item_ids and orphan the first group. Every duplicate fails closed,
    # the checkpoint does not advance, nothing is ingested for that key.
    src = store.add_source("Struct", "teststruct", "teststruct://dup")
    r1 = SourceRow(
        key="dup",
        text="first body",
        tenant="t",
        subjects=("u1",),
        resource_ref=_ref("sharepoint", "a", "dup"),
    )
    r1b = SourceRow(
        key="dup",
        text="second body",
        tenant="t",
        subjects=("u2",),
        resource_ref=_ref("sharepoint", "a", "dup"),
    )
    conn = _RowsConnector([([r1, r1b], True, {"cursor": "must-not-persist"})])
    out = await _sync(store, conn, src)
    assert out["synced"] is False
    assert out.get("checkpoint_advanced") is False
    # The duplicated key was NOT ingested (no orphaned group left behind).
    assert store.get_connector_row_state(src) == {}


@pytest.mark.asyncio
async def test_acl_only_change_regrants_existing_items_not_skipped(store):
    # Same text, DIFFERENT subjects: the text-hash shortcut must NOT skip it and
    # leave the old grant in force. The row is re-granted (same items) and the
    # new subject can see it while the old subject cannot.
    src = store.add_source("Struct", "teststruct", "teststruct://aclonly")
    r_v1 = SourceRow(
        key="k",
        text="stable body",
        tenant="acme",
        subjects=("sp-alice",),
        resource_ref=_ref("sharepoint", "tenA", "k"),
    )
    r_v2 = SourceRow(
        key="k",
        text="stable body",
        tenant="acme",
        subjects=("sp-bob",),
        resource_ref=_ref("sharepoint", "tenA", "k"),
    )
    conn = _RowsConnector([([r_v1], True, {"c": 1}), ([r_v2], True, {"c": 2})])
    await _sync(store, conn, src)
    item = store.get_connector_row_state(src)["k"]["item_ids"][0]
    g1 = store.get_item_grants([item])[item]
    v1 = g1["acl_version"]

    out = await _sync(store, conn, src)
    assert out["synced"] is True
    # SAME item id kept (regrant, not re-ingest).
    assert store.get_connector_row_state(src)["k"]["item_ids"] == [item]
    g2 = store.get_item_grants([item])[item]
    # Grant was rewritten: acl_version bumped, subjects now bob's.
    assert g2["acl_version"] > v1
    resolver = _Resolver({("sharepoint", "tenA"): AccessContext(subject="sp-bob", tenant="acme")})
    assert item in _ids(_query_items(store, QueryPrincipal("bob"), resolver, [item], "stable"))
    resolver_alice = _Resolver(
        {("sharepoint", "tenA"): AccessContext(subject="sp-alice", tenant="acme")}
    )
    assert item not in _ids(
        _query_items(store, QueryPrincipal("alice"), resolver_alice, [item], "stable")
    )


@pytest.mark.asyncio
async def test_chunk_failure_without_exception_does_not_advance(store):
    # A chunk failure inside ingest_text is swallowed per-chunk (processed <
    # total) so ingest_text returns WITHOUT firing on_items and WITHOUT raising.
    # The row must still be treated as failed: no grant, no ledger entry, and the
    # checkpoint does not advance.
    src = store.add_source("Struct", "teststruct", "teststruct://chunkfail")
    r1 = SourceRow(
        key="r1",
        text="body that will lose its chunk",
        tenant="t",
        subjects=("u1",),
        resource_ref=_ref("sharepoint", "a", "r1"),
    )
    conn = _RowsConnector([([r1], True, {"cursor": "must-not-persist"})])
    sched = SyncScheduler(store, _pipeline(store), {"teststruct": conn})

    # Force every chunk write to fail so processed stays 0 < total, with NO
    # exception escaping ingest_text (mirrors the per-chunk except in the impl).
    def _boom(*a, **k):
        raise RuntimeError("chunk write failed")

    sched.pipeline.store.add_item = _boom  # type: ignore[assignment]
    out = await sched.sync_source(src)
    assert out["synced"] is False
    assert out.get("checkpoint_advanced") is False
    # No row-state written, no grant leaked.
    assert store.get_connector_row_state(src) == {}


@pytest.mark.asyncio
async def test_grant_failure_rolls_back_row_state_atomically(store):
    # If a grant write fails while finalizing a row, the row-state write must NOT
    # commit -- otherwise a retry creates untracked duplicate items. Force
    # finalize_connector_row to fail and assert neither the grant nor the ledger
    # entry landed, and the checkpoint did not advance.
    src = store.add_source("Struct", "teststruct", "teststruct://atomic")
    r1 = SourceRow(
        key="r1",
        text="atomic body",
        tenant="t",
        subjects=("u1",),
        resource_ref=_ref("sharepoint", "a", "r1"),
    )
    conn = _RowsConnector([([r1], True, {"cursor": "must-not-persist"})])
    sched = SyncScheduler(store, _pipeline(store), {"teststruct": conn})

    def _boom(*a, **k):
        raise RuntimeError("grant+state transaction failed")

    sched.pipeline.store.finalize_connector_row = _boom  # type: ignore[assignment]
    out = await sched.sync_source(src)
    assert out["synced"] is False
    assert out.get("checkpoint_advanced") is False
    # Neither the ledger row nor any grant for this source was left behind.
    assert store.get_connector_row_state(src) == {}


def test_sourcerow_acl_hash_tracks_grant_facts_only():
    # acl_hash changes when subjects/tenant/ref change, and is stable across
    # text changes (text is tracked by content_hash, not acl_hash).
    base = SourceRow(
        key="k",
        text="body one",
        tenant="acme",
        subjects=("a",),
        resource_ref=_ref("sharepoint", "tenA", "k"),
    )
    same_grant_new_text = SourceRow(
        key="k",
        text="body TWO",
        tenant="acme",
        subjects=("a",),
        resource_ref=_ref("sharepoint", "tenA", "k"),
    )
    new_subjects = SourceRow(
        key="k",
        text="body one",
        tenant="acme",
        subjects=("b",),
        resource_ref=_ref("sharepoint", "tenA", "k"),
    )
    assert base.acl_hash == same_grant_new_text.acl_hash
    assert base.acl_hash != new_subjects.acl_hash


@pytest.mark.asyncio
async def test_optional_connector_registration_is_deferred(monkeypatch):
    # Registering with source_type must NOT import the vendor module or run its
    # constructor on the (boot) call path: only a lightweight lazy stand-in is
    # placed, answering source_type() from the known string. The real connector
    # is imported + constructed on FIRST actual use.
    import sys
    import types

    from kiro_crew.dashboard.handlers.knowledge import (
        _LazyConnector,
        _register_optional_connector,
    )
    from kiro_crew.knowledge.connectors.base import BaseConnector

    built: list[str] = []

    class _Vendor(BaseConnector):
        def __init__(self, **kw):
            built.append("constructed")

        def source_type(self):
            return "teststruct"

        def supports_rows(self):
            return True

        async def detect_changes(self, source):
            return True

        async def fetch(self, source):
            raise NotImplementedError

        def validate_config(self, config):
            return True, None

        async def fetch_rows(self, source):
            return [], True, {}

    mod = types.ModuleType("kiro_crew._lazy_vendor_mod")
    mod._Vendor = _Vendor  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "kiro_crew._lazy_vendor_mod", mod)

    connectors: dict = {}
    ok = _register_optional_connector(
        connectors,
        "kiro_crew._lazy_vendor_mod",
        "_Vendor",
        source_type="teststruct",
    )
    assert ok is True
    conn = connectors["teststruct"]
    # Deferred: a lazy stand-in, NOT constructed yet, but source_type known.
    assert isinstance(conn, _LazyConnector)
    assert conn.source_type() == "teststruct"
    assert built == []
    # First real use constructs it exactly once.
    assert conn.supports_rows() is True
    assert built == ["constructed"]
    await conn.fetch_rows({})
    assert built == ["constructed"]  # not rebuilt


# --------------------------------------------------------------------------
# Bundle import + connector-row delete integrity guards.
# --------------------------------------------------------------------------


def test_bundle_import_cannot_forge_local_provenance(store):
    # The forgery to block: a bundle smuggling MANAGED content in as local (which
    # would un-gate it). A source with a MANAGED grant claiming a local
    # source_type/trust_class must stay MANAGED on import.
    from kiro_crew.knowledge.store import TRUST_LOCAL, TRUST_MANAGED

    bundle = {
        "sources": [
            {
                "id": "forged-src",
                "name": "Forged",
                "source_type": "doc",
                "uri": "doc://forged",
                "properties": "{}",
                "trust_class": TRUST_LOCAL,
                "created_at": "2020-01-01T00:00:00",
            }
        ],
        "items": [
            {
                "id": "forged-item",
                "title": "t",
                "content": "c",
                "item_type": "document",
                "source_id": "forged-src",
            }
        ],
        "entities": [],
        "relations": [],
        "source_locations": [],
        "mentions": [],
        # A MANAGED grant on the item -> the source is managed-bearing -> the
        # local claim must NOT be honoured.
        "item_acls": [
            {"item_id": "forged-item", "subjects": '["sf-a"]', "tenant": "t-A", "managed": 1}
        ],
    }
    store.import_bundle(bundle)
    row = store.db.execute(
        "SELECT trust_class FROM sources WHERE id = ?", ("forged-src",)
    ).fetchone()
    assert row is not None
    assert row["trust_class"] == TRUST_MANAGED, "managed content forged as local"


def test_bundle_import_into_fresh_store_is_managed_not_local(store):
    # A bundle claiming a local source_type/trust_class, imported into a store
    # that has NO such source yet, must land MANAGED -- the bundle's own claim is
    # not verifiable provenance. (A one-step local restore would need a named,
    # authenticated re-admission step, which does not exist yet.)
    from kiro_crew.knowledge.store import TRUST_LOCAL, TRUST_MANAGED

    bundle = {
        "sources": [
            {
                "id": "vault-src",
                "name": "MyVault",
                "source_type": "local_folder",
                "uri": "file:///vault",
                "properties": "{}",
                "trust_class": TRUST_LOCAL,
                "created_at": "2020-01-01T00:00:00",
            }
        ],
        "items": [
            {
                "id": "vault-item",
                "title": "note",
                "content": "body",
                "item_type": "document",
                "source_id": "vault-src",
            }
        ],
        "entities": [],
        "relations": [],
        "source_locations": [],
        "mentions": [],
        "item_acls": [],
    }
    store.import_bundle(bundle)
    row = store.db.execute(
        "SELECT trust_class FROM sources WHERE id = ?", ("vault-src",)
    ).fetchone()
    assert row is not None and row["trust_class"] == TRUST_MANAGED


def test_bundle_import_preserves_local_only_for_preexisting_local_source(store):
    # The ONLY verifiable local signal: a source with this id ALREADY exists in
    # this store as local (independently admitted by the real local creator). Its
    # re-import keeps local trust; a bundle can neither create nor flip it.
    from kiro_crew.knowledge.store import TRUST_LOCAL

    sid = store.add_source("MyVault", "local_folder", "file:///vault")
    row0 = store.db.execute("SELECT trust_class FROM sources WHERE id = ?", (sid,)).fetchone()
    assert row0["trust_class"] == TRUST_LOCAL
    bundle = {
        "sources": [
            {
                "id": sid,
                "name": "MyVault",
                "source_type": "local_folder",
                "uri": "file:///vault",
                "properties": "{}",
                "trust_class": TRUST_LOCAL,
                "created_at": "2020-01-01T00:00:00",
            }
        ],
        "items": [],
        "entities": [],
        "relations": [],
        "source_locations": [],
        "mentions": [],
        "item_acls": [],
    }
    store.import_bundle(bundle)
    row = store.db.execute("SELECT trust_class FROM sources WHERE id = ?", (sid,)).fetchone()
    assert row["trust_class"] == TRUST_LOCAL


def test_bundle_import_omitted_managed_defaults_to_managed(store):
    # An item_acl entry omitting `managed`, for an item THIS bundle inserts, must
    # import as managed=1 (fail-closed) -- an untrusted bundle cannot un-gate an
    # item by leaving the flag out. (Grants are only restored for newly-inserted
    # items; the item therefore arrives via the bundle's own `items`.)
    bundle = {
        "sources": [],
        "items": [
            {
                "id": "it-x",
                "title": "t",
                "content": "c",
                "item_type": "document",
                "source_id": None,
            }
        ],
        "entities": [],
        "relations": [],
        "source_locations": [],
        "mentions": [],
        "item_acls": [{"item_id": "it-x", "subjects": '["u1"]', "tenant": "acme"}],
    }
    store.import_bundle(bundle)
    row = store.db.execute("SELECT managed FROM item_acl WHERE item_id = 'it-x'").fetchone()
    assert row is not None and row["managed"] == 1


def test_bundle_import_malformed_item_acls_does_not_crash(store):
    # A bundle whose item_acls contains a non-object entry must be skipped, not
    # raise and 500 the whole import.
    bundle = {
        "sources": [],
        "items": [],
        "entities": [],
        "relations": [],
        "source_locations": [],
        "mentions": [],
        "item_acls": [1, "nope", {"item_id": None}, {"no_item_id": True}],
    }
    # Must not raise.
    store.import_bundle(bundle)


@pytest.mark.asyncio
async def test_dropped_row_delete_is_atomic(store):
    # A dropped connector row's item delete + ledger delete run in one
    # transaction: if the item delete succeeds but a fault would strand the
    # ledger, both roll back together. Verify the happy path removes both, and a
    # forced fault inside the atomic seam leaves BOTH intact.
    src = store.add_source("Struct", "teststruct", "teststruct://del")
    r1 = SourceRow(
        key="r1",
        text="keep body",
        tenant="t",
        subjects=("u1",),
        resource_ref=_ref("sharepoint", "a", "r1"),
    )
    r2 = SourceRow(
        key="r2",
        text="drop body",
        tenant="t",
        subjects=("u2",),
        resource_ref=_ref("sharepoint", "a", "r2"),
    )
    conn = _RowsConnector([([r1, r2], True, {"c": 1}), ([r1], True, {"c": 2})])
    await _sync(store, conn, src)
    r2_item = store.get_connector_row_state(src)["r2"]["item_ids"][0]

    # Fault the atomic delete: neither the item nor the ledger row may change.
    orig = store.delete_items_batch_in_txn

    def _boom(*a, **k):
        raise RuntimeError("delete seam failed")

    store.delete_items_batch_in_txn = _boom  # type: ignore[assignment]
    try:
        await _sync(store, conn, src)
    finally:
        store.delete_items_batch_in_txn = orig  # type: ignore[assignment]
    # The dropped-row delete failed atomically: item AND ledger row both survive.
    assert store.get_item(r2_item) is not None
    assert "r2" in store.get_connector_row_state(src)

    # Now let it succeed: both are gone together.
    await _sync(store, conn, src)
    assert store.get_item(r2_item) is None
    assert "r2" not in store.get_connector_row_state(src)


def test_bundle_import_malformed_numeric_fields_do_not_crash(store):
    # A non-numeric fresh_as_of / acl_version must be skipped, not raise
    # ValueError and 500 the import.
    store.db.execute(
        "INSERT OR IGNORE INTO items (id, title, content, item_type, created_at, updated_at) "
        "VALUES ('it-n','t','c','document','2020-01-01','2020-01-01')"
    )
    store.db.commit()
    bundle = {
        "sources": [],
        "items": [],
        "entities": [],
        "relations": [],
        "source_locations": [],
        "mentions": [],
        "item_acls": [
            {
                "item_id": "it-n",
                "subjects": "[]",
                "tenant": "t",
                "managed": 0,
                "fresh_as_of": "not-a-float",
            },
            {"item_id": "it-n", "subjects": "[]", "tenant": "t", "acl_version": "NaN-ish"},
        ],
    }
    store.import_bundle(bundle)  # must not raise


@pytest.mark.asyncio
async def test_row_replacement_delete_is_atomic_with_finalize(store):
    # A row whose text CHANGES replaces its prior item group. The old-item
    # delete must be in the SAME transaction as the new grants + ledger: if
    # finalize fails, the OLD items survive (not deleted-then-grantless).
    src = store.add_source("Struct", "teststruct", "teststruct://repl")
    r_v1 = SourceRow(
        key="k",
        text="first body",
        tenant="t",
        subjects=("u1",),
        resource_ref=_ref("sharepoint", "a", "k"),
    )
    r_v2 = SourceRow(
        key="k",
        text="second body CHANGED",
        tenant="t",
        subjects=("u1",),
        resource_ref=_ref("sharepoint", "a", "k"),
    )
    conn = _RowsConnector([([r_v1], True, {"c": 1}), ([r_v2], True, {"c": 2})])
    await _sync(store, conn, src)
    old_item = store.get_connector_row_state(src)["k"]["item_ids"][0]

    # Fault finalize: the old item must NOT be deleted (atomic rollback).
    orig = store.finalize_connector_row

    def _boom(*a, **k):
        raise RuntimeError("finalize failed")

    store.finalize_connector_row = _boom  # type: ignore[assignment]
    try:
        out = await _sync(store, conn, src)
    finally:
        store.finalize_connector_row = orig  # type: ignore[assignment]
    assert out["synced"] is False
    # Old item survives (replacement delete rolled back with the failed finalize).
    assert store.get_item(old_item) is not None
    assert store.get_connector_row_state(src)["k"]["item_ids"] == [old_item]

    # Now succeed: old item replaced by the new group, atomically.
    out = await _sync(store, conn, src)
    assert out["synced"] is True
    new_ids = store.get_connector_row_state(src)["k"]["item_ids"]
    assert old_item not in new_ids
    assert store.get_item(old_item) is None


def test_lazy_connector_absent_module_fails_closed_not_crash():
    # An absent vendor module must make the lazy connector REFUSE (validate_config
    # -> (False, reason)) rather than raise an uncaught ImportError that 500s
    # source creation. detect_changes/supports_rows degrade to False; a live read
    # raises a clear RuntimeError only if actually invoked.
    from kiro_crew.dashboard.handlers.knowledge import _LazyConnector

    lc = _LazyConnector("github", "kiro_crew.knowledge.connectors.__absent__", "X")
    assert lc.source_type() == "github"  # no import needed
    ok, msg = lc.validate_config({})
    assert ok is False and "not available" in msg  # refused, not crashed
    assert lc.supports_rows() is False


def test_install_binding_resolver_is_noop_when_already_present():
    # If a host (or test) already installed a resolver, _install_binding_resolver
    # must leave it untouched.
    from aiohttp import web

    from kiro_crew.dashboard.handlers.knowledge import _install_binding_resolver

    app = web.Application()
    sentinel = object()
    app["knowledge_binding_resolver"] = sentinel
    _install_binding_resolver(app)
    assert app["knowledge_binding_resolver"] is sentinel


def test_install_binding_resolver_fail_closed_when_unavailable(monkeypatch):
    # When the control-plane package cannot be imported, the resolver is NOT
    # installed (managed items then fail closed) -- never a synthesized stand-in.
    import builtins

    from aiohttp import web

    from kiro_crew.dashboard.handlers.knowledge import _install_binding_resolver

    real_import = builtins.__import__

    def _blocked(name, *a, **k):
        if name.startswith("kiro_crew.connections.control_plane"):
            raise ImportError("control-plane unavailable (test)")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _blocked)
    app = web.Application()
    _install_binding_resolver(app)
    assert app.get("knowledge_binding_resolver") is None


@pytest.mark.asyncio
async def test_new_row_finalize_failure_cleans_up_orphan_chunks(store):
    # A brand-new row (no prior items) whose finalize (grants+ledger) fails must
    # NOT leave the committed chunks behind grantless/untracked -- they are
    # deleted before the error propagates, so a retry does not accumulate
    # orphaned duplicates and the checkpoint does not advance.
    src = store.add_source("Struct", "teststruct", "teststruct://orphan")
    r1 = SourceRow(
        key="k",
        text="fresh body",
        tenant="t",
        subjects=("u1",),
        resource_ref=_ref("sharepoint", "a", "k"),
    )
    conn = _RowsConnector([([r1], True, {"cursor": "must-not-persist"})])
    sched = SyncScheduler(store, _pipeline(store), {"teststruct": conn})

    def _boom(*a, **k):
        raise RuntimeError("finalize failed")

    sched.pipeline.store.finalize_connector_row = _boom  # type: ignore[assignment]
    out = await sched.sync_source(src)
    assert out["synced"] is False
    assert out.get("checkpoint_advanced") is False
    # No ledger row, and NO orphaned items left behind for this source.
    assert store.get_connector_row_state(src) == {}
    leftover = store.db.execute(
        "SELECT COUNT(*) AS n FROM items WHERE source_id = ?", (src,)
    ).fetchone()["n"]
    assert leftover == 0


# --------------------------------------------------------------------------
# cycle8 unified trust-boundary invariant: an untrusted bundle's claim, missing
# field, or naming an existing source id never yields local trust or a new
# non-managed authorization. Proven via synthetic import -> query.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_import_cannot_bypass_gate_via_existing_local_source(store):
    # A genuine local source is established locally first; its OWN item is
    # visible. A bundle then attaches a NEW item to that same source id -> the
    # new item must NOT inherit local trust (it is managed, denied without a
    # binding), while the pre-existing local item stays visible.
    local_src = store.add_source("Vault", "local_folder", "file:///v")
    genuine = store.add_item("Genuine", "genuine local body", "doc", source_id=local_src)
    resolver = _Resolver({})  # no bindings -> managed items denied
    # Pre-existing genuine local item is visible (trusted-local, no grant needed).
    vis = _query_items(store, LOCAL_PRINCIPAL, resolver, [genuine], "genuine")
    assert genuine in _ids(vis)
    # Bundle attaches a NEW item to the SAME (local) source id, no grant.
    store.import_bundle(
        {
            "sources": [],
            "entities": [],
            "relations": [],
            "source_locations": [],
            "mentions": [],
            "item_acls": [],
            "items": [
                {
                    "id": "smuggled",
                    "title": "Smuggled",
                    "content": "smuggled body",
                    "item_type": "document",
                    "source_id": local_src,
                }
            ],
        }
    )
    # The smuggled item is MANAGED (forced grant) -> denied without a binding,
    # even though it names a local source; the genuine item is still visible.
    both = _query_items(store, LOCAL_PRINCIPAL, resolver, [genuine, "smuggled"], "body")
    assert genuine in _ids(both)
    assert "smuggled" not in _ids(both)


@pytest.mark.asyncio
async def test_import_managed_false_cannot_ungate(store):
    # A bundle item with an explicit item_acl managed=false must NOT become
    # trusted-local: the grant is forced managed, so it is denied without a
    # binding.
    src = store.add_source("SF", "salesforce", "salesforce://s")
    store.import_bundle(
        {
            "sources": [],
            "entities": [],
            "relations": [],
            "source_locations": [],
            "mentions": [],
            "items": [
                {
                    "id": "mf",
                    "title": "MF",
                    "content": "managed false body",
                    "item_type": "record",
                    "source_id": src,
                }
            ],
            "item_acls": [
                {"item_id": "mf", "subjects": '["anyone"]', "tenant": "t", "managed": False}
            ],
        }
    )
    grant = store.get_item_grants(["mf"]).get("mf")
    assert grant is not None and grant["managed"] == 1  # forced managed
    resolver = _Resolver({})
    assert "mf" not in _ids(
        _query_items(store, QueryPrincipal("anyone"), resolver, ["mf"], "managed")
    )


@pytest.mark.asyncio
async def test_import_preserves_access_to_genuine_local_content(store):
    # A genuine local item that ALREADY exists here (idempotent re-import: the
    # bundle carries the same id) keeps its local trust and stays accessible --
    # the invariant fences NEW content, it does not damage existing local
    # content.
    local_src = store.add_source("Vault", "local_folder", "file:///v")
    existing = store.add_item("Kept", "kept local body", "doc", source_id=local_src)
    # Re-import a bundle naming the SAME item id (INSERT OR IGNORE no-op).
    store.import_bundle(
        {
            "sources": [],
            "entities": [],
            "relations": [],
            "source_locations": [],
            "mentions": [],
            "item_acls": [],
            "items": [
                {
                    "id": existing,
                    "title": "Kept",
                    "content": "kept local body",
                    "item_type": "doc",
                    "source_id": local_src,
                }
            ],
        }
    )
    # No managed grant was forced onto the pre-existing item; still visible.
    assert store.get_item_grants([existing]).get(existing) is None
    resolver = _Resolver({})
    assert existing in _ids(_query_items(store, LOCAL_PRINCIPAL, resolver, [existing], "kept"))


@pytest.mark.asyncio
async def test_bundle_does_not_regrant_preexisting_item(store):
    # A bundle that names a PRE-EXISTING local item (already in the store, no
    # grant row) must NOT attach a managed grant to it -- doing so would make a
    # genuine local item inaccessible. Only items THIS import newly inserts get
    # a restored/forced grant.
    local_src = store.add_source("Vault", "local_folder", "file:///v")
    existing = store.add_item("Kept", "kept body", "doc", source_id=local_src)
    assert store.get_item_grants([existing]).get(existing) is None
    # Bundle references the SAME existing id with a managed item_acl.
    store.import_bundle(
        {
            "sources": [],
            "items": [],  # not re-inserting it (INSERT OR IGNORE no-op anyway)
            "entities": [],
            "relations": [],
            "source_locations": [],
            "mentions": [],
            "item_acls": [{"item_id": existing, "subjects": "[]", "tenant": "x", "managed": 1}],
        }
    )
    # No grant was written onto the pre-existing item; it stays trusted-local.
    assert store.get_item_grants([existing]).get(existing) is None
    resolver = _Resolver({})
    assert existing in _ids(_query_items(store, LOCAL_PRINCIPAL, resolver, [existing], "kept"))


@pytest.mark.asyncio
async def test_revalidation_only_fresh_admits_managed(store):
    # When a revalidation hook is consulted, ONLY an explicit FRESH admits a
    # managed item; any other outcome (STALE, UNVERIFIABLE, or an unrecognized
    # value) is a hard deny (fail-closed).
    src = store.add_source("SF", "salesforce", "salesforce://s")
    item = store.add_item("Deal", "alpha managed body", "record", source_id=src)
    store.set_item_acl(
        item,
        ["sf-a"],
        tenant="t-A",
        managed=True,
        fresh_as_of=0.0,
        resource_ref=_ref("salesforce", "acct", "Deal"),
    )
    ctx = AccessContext(subject="sf-a", tenant="t-A")
    resolver = _Resolver({("salesforce", "acct"): ctx})

    class _Reval:
        def __init__(self, outcome):
            self._o = outcome

        def revalidate(self, ctx, item_id, grant):
            return self._o

    def _query(reval):
        r = HybridRetriever(store, revalidator=reval, binding_resolver=resolver)
        r._keyword_search = lambda *_a, **_k: [(item, 1)]
        return {
            x["id"] for x in r.search("alpha", limit=20, query_principal=QueryPrincipal("sf-a"))
        }

    # FRESH -> visible.
    assert item in _query(_Reval(RevalidationOutcome.FRESH))
    # REVOKED -> denied.
    assert item not in _query(_Reval(RevalidationOutcome.REVOKED))
    # An unrecognized outcome -> denied (fail-closed, not fail-open).
    assert item not in _query(_Reval("some-unrecognized-value"))
