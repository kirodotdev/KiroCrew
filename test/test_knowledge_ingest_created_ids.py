"""Created-item ids come from the write, never a before/after source diff (#4431).

Three ingestion sites used to learn "what this call wrote" by snapshotting the
source's full item-id set before the work and diffing a second snapshot
afterwards. Each snapshot was a ``SELECT id FROM items WHERE source_id = ?``
materializing one row per item in the SOURCE (~20k rows on a large library,
over a second of blocking SQLite), and the "before" read ran ON the asyncio
event loop once per ingested file -- the same per-file loop stall PR #3397
removed from the folder-scan caller.

The diff was also WRONG, not just slow: ``import_bundle`` writes into the same
aggregate source in its own transaction under no shared lock, so anything it
committed while an ingest was awaiting got attributed to that ingest -- handing
a document delete authority over knowledge it never created. Ids appended at
``add_item`` (or reported through the pipeline's ``on_committed`` callback,
which fires inside the finalize hop that commits them) cannot misattribute
that way.

This file ratchets the conversion for the three sites:
  1. ``ingestion.py::_ingest_file_body`` -- partial-failure rollback uses
     ``created_item_ids``.
  2. ``ingestion.py::ingest_text`` -- same, with the collection newly added.
  3. ``artifact_ingest.py::_ingest_artifact`` -- ``on_committed`` replaces the
     before/after diff feeding ``_set_state``.

The ``_old_item_ids`` reads that share the query TEXT but mean "the
pre-existing item group this call must delete" are deliberately out of scope
(no callback can supply them); they live in ``_resolve_old_item_ids`` and run
inside the off-loop duplicate-gate hop (#4441). The ratchet pins the literal
to that helper alone so a re-introduced snapshot still fails the test.
"""

from __future__ import annotations

import ast
import hashlib
import pathlib
import threading
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.artifacts import ArtifactStore
from kiro_crew.knowledge import artifact_ingest
from kiro_crew.knowledge.artifact_ingest import (
    ensure_artifact_source,
    ingest_artifact,
)
from kiro_crew.knowledge.ingestion import IngestionPipeline
from kiro_crew.knowledge.readers import FileReader
from kiro_crew.knowledge.store import KnowledgeStore

_KNOWLEDGE_SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "kiro_crew" / "knowledge"

# The retired snapshot's query text. The one occurrence pinned below is the
# _old_item_ids resolver: same text, different meaning (the PRIOR group this
# call must delete -- not what it created), tracked separately from #4431 and
# run off-loop inside the duplicate-gate hop since #4441.
_SNAPSHOT_QUERY = "SELECT id FROM items WHERE source_id"


def _two_chunks(text, **kw):
    half = max(1, len(text) // 2)
    return [
        {
            "content": text[:half],
            "chunk_index": 0,
            "section_title": None,
            "line_start": 0,
            "line_end": 0,
        },
        {
            "content": text[half:],
            "chunk_index": 1,
            "section_title": None,
            "line_start": 1,
            "line_end": 1,
        },
    ]


@pytest.fixture()
def kstore(tmp_path):
    s = KnowledgeStore(str(tmp_path / "knowledge.db"))
    yield s
    s.close()


def _extraction():
    return {"category": "document", "summary": "s", "entities": []}


@pytest.fixture()
def pipeline(kstore):
    """Pipeline with a two-chunk chunker so a partial failure is reachable."""
    extractor = MagicMock()
    extractor._pool = None
    extractor.extract_batch = AsyncMock(return_value=[_extraction(), _extraction()])
    chunker = MagicMock()
    chunker.chunk.side_effect = _two_chunks
    chunker.chunk_markdown.side_effect = _two_chunks
    chunker.chunk_code.side_effect = _two_chunks
    chunker.chunk_slides.side_effect = _two_chunks
    return IngestionPipeline(
        store=kstore,
        extractor=extractor,
        chunker=chunker,
        reader=FileReader(),
        embedder=None,
    )


def _item_ids(kstore, source_id):
    return {
        r["id"]
        for r in kstore.db.execute(
            "SELECT id FROM items WHERE source_id = ?", (source_id,)
        ).fetchall()
    }


def _fail_second_embed(pipeline):
    """Make the SECOND chunk's embed raise: both items are created (the append
    happens right after add_item), processed stays 1 < total 2 -> the
    partial-failure branch of _finalize runs."""
    calls = {"n": 0}

    async def _embed(item_id, *a, **kw):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise RuntimeError("boom: embed failed for chunk 2")

    pipeline._embed_item = _embed


def _inject_foreign_item_during_extract(pipeline, kstore, source_id):
    """Land a foreign item in the same source WHILE the ingest is in flight.

    The retired ``_before_ids`` snapshot was taken just before
    ``extract_batch``, so an item committed inside it (as a concurrent
    ``import_bundle`` would) landed strictly after the "before" read and was
    misattributed to the ingest by the after-diff. With ids collected at the
    write, it must survive the rollback.
    """
    foreign: dict[str, str] = {}

    async def _extract(contents):
        foreign["id"] = kstore.add_item(
            "foreign", "committed by a concurrent writer", "document", source_id=source_id
        )
        return [_extraction() for _ in contents]

    pipeline.extractor.extract_batch = AsyncMock(side_effect=_extract)
    return foreign


class TestIngestFilePartialFailureRollback:
    """Site 1: _ingest_file_body's rollback deletes exactly what it created."""

    @pytest.mark.asyncio
    async def test_rollback_spares_foreign_and_preexisting_items(self, pipeline, kstore, tmp_path):
        source_id = kstore.add_source("agg", "local_folder", str(tmp_path))
        preexisting = kstore.add_item(
            "other group", "another document's item", "document", source_id=source_id
        )
        foreign = _inject_foreign_item_during_extract(pipeline, kstore, source_id)
        _fail_second_embed(pipeline)

        f = tmp_path / "doc.md"
        f.write_text("# heading\nbody text long enough to split", encoding="utf-8")
        job_id = await pipeline.ingest_file(str(f), source_id=source_id, old_item_ids=[])

        assert job_id is not None
        status = (pipeline.get_job_status(job_id) or {}).get("status")
        assert status != "completed"
        remaining = _item_ids(kstore, source_id)
        # Both survivors: the pre-existing sibling group AND the item a
        # concurrent writer committed mid-ingest (the case the diff got wrong).
        assert preexisting in remaining
        assert foreign["id"] in remaining
        # ... and nothing else: everything THIS call created was rolled back.
        assert remaining == {preexisting, foreign["id"]}
        row = kstore.db.execute(
            "SELECT sync_status FROM sources WHERE id = ?", (source_id,)
        ).fetchone()
        assert row["sync_status"] == "error"


class TestIngestTextPartialFailureRollback:
    """Site 2: ingest_text now collects its ids and rolls back exactly those."""

    @pytest.mark.asyncio
    async def test_rollback_spares_foreign_and_preexisting_items(self, pipeline, kstore, tmp_path):
        source_id = kstore.add_source("agg", "artifacts", "kc://artifacts")
        preexisting = kstore.add_item(
            "other group", "another artifact's item", "document", source_id=source_id
        )
        foreign = _inject_foreign_item_during_extract(pipeline, kstore, source_id)
        _fail_second_embed(pipeline)

        job_id = await pipeline.ingest_text(
            "body text long enough to split into two chunks",
            "Doc",
            source_id=source_id,
            old_item_ids=[],
        )

        assert job_id is not None
        status = (pipeline.get_job_status(job_id) or {}).get("status")
        assert status != "completed"
        remaining = _item_ids(kstore, source_id)
        assert preexisting in remaining
        assert foreign["id"] in remaining
        assert remaining == {preexisting, foreign["id"]}
        row = kstore.db.execute(
            "SELECT sync_status FROM sources WHERE id = ?", (source_id,)
        ).fetchone()
        assert row["sync_status"] == "error"


class TestArtifactIngestRecordsPipelineIds:
    """Site 3: _set_state records the ids the pipeline reported, non-empty."""

    @pytest.fixture()
    def one_chunk_pipeline(self, kstore):
        extractor = MagicMock()
        extractor._pool = None
        extractor.extract_batch = AsyncMock(return_value=[_extraction()])

        def _one(text, **kw):
            return [
                {
                    "content": text,
                    "chunk_index": 0,
                    "section_title": None,
                    "line_start": 0,
                    "line_end": 0,
                }
            ]

        chunker = MagicMock()
        chunker.chunk.side_effect = _one
        chunker.chunk_markdown.side_effect = _one
        chunker.chunk_code.side_effect = _one
        chunker.chunk_slides.side_effect = _one
        return IngestionPipeline(
            store=kstore,
            extractor=extractor,
            chunker=chunker,
            reader=FileReader(),
            embedder=None,
        )

    @pytest.mark.asyncio
    async def test_completed_path_pins_nonempty_callback_ids(
        self, one_chunk_pipeline, kstore, tmp_path, monkeypatch
    ):
        pipeline = one_chunk_pipeline
        art_store = ArtifactStore(root=tmp_path / "artifacts")
        sid, _ = ensure_artifact_source(kstore)
        art = art_store.create(name="Notes", content="# H\nbody", kind="markdown")

        # Spy on the callback wiring: record what the pipeline actually reports
        # through on_committed, so the assertion pins "state == reported", not
        # merely "state == whatever is in the table now".
        reported: list[str] = []
        real_ingest_file = pipeline.ingest_file

        async def _spy(path, **kw):
            inner = kw.get("on_committed")
            assert inner is not None, (
                "_ingest_artifact no longer passes on_committed to "
                "ingest_file -- a completed ingest whose callback never fires "
                "would write an EMPTY item list into _set_state, losing the "
                "slug's claim/ownership record"
            )

            def _wrap(ids):
                reported.extend(ids)
                inner(ids)

            kw["on_committed"] = _wrap
            result = await real_ingest_file(path, **kw)
            # Durability pin: the ownership row must already be persisted when
            # ingest_file returns -- i.e. it was written INSIDE the finalize
            # hop by the callback, not by the caller's post-ingest write. The
            # awaits after this point (temp-file cleanup, job-status read) are
            # cancellation points; a shutdown landing there must not leave
            # committed items that no state row names.
            _, hop_ids = artifact_ingest._get_state(kstore, sid, art.slug)
            assert hop_ids == reported and hop_ids != [], (
                "artifact ownership was not persisted inside the pipeline's "
                "finalize hop -- a cancellation between ingest_file returning "
                "and the caller's _set_state would orphan the committed group"
            )
            return result

        pipeline.ingest_file = _spy

        # Count _set_state calls: when the hop write succeeds it must be the
        # ONLY write. An unconditional post-ingest re-write would race a
        # concurrent dedup sweep in the awaits after the hop and resurrect an
        # 'active' row with stale ids over the sweep's result.
        set_state_calls: list[tuple] = []
        real_set_state = artifact_ingest._set_state

        def _counting_set_state(*a, **kw):
            set_state_calls.append((a, kw))
            return real_set_state(*a, **kw)

        monkeypatch.setattr(artifact_ingest, "_set_state", _counting_set_state)

        job = await ingest_artifact(
            pipeline, art_store, art.slug, sid, {"markdown", "text", "html", "json"}
        )

        assert job is not None
        assert (pipeline.get_job_status(job) or {}).get("status") == "completed"
        # The completed path must never record an empty group: an empty list in
        # _set_state loses the slug's ownership record while its items remain.
        assert reported != []
        _, state_ids = artifact_ingest._get_state(kstore, sid, art.slug)
        assert sorted(state_ids) == sorted(reported)
        assert set(state_ids) <= _item_ids(kstore, sid)
        assert len(set_state_calls) == 1, (
            "the post-ingest _set_state ran even though the finalize-hop write "
            "succeeded -- that unconditional re-write can overwrite a concurrent "
            "dedup's state row with stale ids"
        )


def _count_snapshot_literals(fn: ast.AST) -> list[int]:
    """Line numbers of every string literal in *fn* (nested scopes included)
    containing the retired snapshot query text."""
    out = []
    for node in ast.walk(fn):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and _SNAPSHOT_QUERY in node.value
        ):
            out.append(node.lineno)
    return sorted(out)


def _find_def(tree: ast.Module, name: str) -> ast.AST:
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name:
            return node
    raise AssertionError(
        f"def {name} no longer exists -- this ratchet is guarding nothing and "
        "must be repointed at whatever replaced it"
    )


class TestSnapshotQueryRatchet:
    """The retired full-source id snapshot must not creep back in (#4431).

    Counted per function, nested scopes included, so re-adding the read either
    on the loop (function body) or inside the off-loop ``_finalize`` closure
    trips the same assertion. Pinned as exact line lists, not booleans, so a
    failure names the offending line.
    """

    def test_ingest_file_body_has_no_source_snapshot(self):
        tree = ast.parse((_KNOWLEDGE_SRC / "ingestion.py").read_text(errors="replace"))
        hits = _count_snapshot_literals(_find_def(tree, "_ingest_file_body"))
        assert hits == [], (
            f"_ingest_file_body reads the source's full item-id set again at "
            f"lines {hits}. It must not: created_item_ids is collected at "
            "add_item, and a before/after diff both blocks the loop (~20k rows "
            "per read on a large source) and misattributes concurrent "
            "import_bundle writes."
        )

    def test_ingest_text_has_no_on_loop_group_read(self):
        tree = ast.parse((_KNOWLEDGE_SRC / "ingestion.py").read_text(errors="replace"))
        hits = _count_snapshot_literals(_find_def(tree, "ingest_text"))
        assert hits == [], (
            f"ingest_text contains '{_SNAPSHOT_QUERY}' literals at lines "
            f"{hits}; it must contain none. Both the created-ids snapshot "
            "(#4431) and the inline _old_item_ids resolution (#4441) are "
            "retired here: the old-group read lives in _resolve_old_item_ids "
            "and runs inside the off-loop duplicate-gate hop."
        )

    def test_ingest_file_has_no_on_loop_group_read(self):
        tree = ast.parse((_KNOWLEDGE_SRC / "ingestion.py").read_text(errors="replace"))
        hits = _count_snapshot_literals(_find_def(tree, "ingest_file"))
        assert hits == [], (
            f"ingest_file contains '{_SNAPSHOT_QUERY}' literals at lines "
            f"{hits}; it must contain none. The _old_item_ids resolution "
            "(#4441) lives in _resolve_old_item_ids and runs inside the "
            "off-loop duplicate-gate hop -- an inline read here lands on the "
            "asyncio event loop (~20k rows per read on a large source)."
        )

    def test_resolver_is_the_single_group_read(self):
        tree = ast.parse((_KNOWLEDGE_SRC / "ingestion.py").read_text(errors="replace"))
        hits = _count_snapshot_literals(_find_def(tree, "_resolve_old_item_ids"))
        assert len(hits) == 1, (
            f"_resolve_old_item_ids contains {len(hits)} '{_SNAPSHOT_QUERY}' "
            f"literals at lines {hits}; exactly 1 is expected. Zero means the "
            "old-group read moved and every ratchet in this class must be "
            "repointed; more means the query was duplicated inside the helper."
        )

    def test_ingest_artifact_has_no_source_snapshot(self):
        tree = ast.parse((_KNOWLEDGE_SRC / "artifact_ingest.py").read_text(errors="replace"))
        hits = _count_snapshot_literals(_find_def(tree, "ingest_artifact"))
        assert hits == [], (
            f"ingest_artifact reads the source's full item-id set again at "
            f"lines {hits}. It must not: the pipeline reports the created ids "
            "through on_committed from inside the finalize hop that commits "
            "them -- the same contract agent_source.py consumes."
        )


class TestOldGroupReadRunsOffLoop:
    """The _old_item_ids full-source reads run on a worker thread (#4441).

    One test per original on-loop read site: ingest_file's replace-all and
    existing-local-file paths, ingest_text's changed-existing (URI hit, hash
    mismatch) and replace-all paths. Each spies on the resolver to record the
    thread it ran on, and pins the semantics (the resolved group still drives
    the replace) so the off-loop move cannot silently drop the read.
    """

    @staticmethod
    def _spy_resolver(pipeline):
        calls: list[int] = []
        real = pipeline._resolve_old_item_ids

        def _spy(source_id):
            calls.append(threading.get_ident())
            return real(source_id)

        pipeline._resolve_old_item_ids = _spy
        return calls

    @pytest.mark.asyncio
    async def test_ingest_file_replace_all_resolves_off_loop(self, pipeline, kstore, tmp_path):
        source_id = kstore.add_source("remote", "url", "https://example.test/doc")
        stale = kstore.add_item("old", "superseded", "document", source_id=source_id)
        calls = self._spy_resolver(pipeline)
        loop_thread = threading.get_ident()

        f = tmp_path / "doc.md"
        f.write_text("# heading\nbody text long enough to split", encoding="utf-8")
        job_id = await pipeline.ingest_file(str(f), source_id=source_id)

        assert job_id is not None
        assert calls, "the replace-all group read never ran"
        assert all(t != loop_thread for t in calls), (
            "_resolve_old_item_ids ran on the event-loop thread -- the "
            "full-source read blocks the loop for >1s on a large library"
        )
        # The resolved group actually drove the replace: the stale item is gone.
        assert stale not in _item_ids(kstore, source_id)

    @pytest.mark.asyncio
    async def test_ingest_file_changed_local_file_resolves_off_loop(
        self, pipeline, kstore, tmp_path
    ):
        f = tmp_path / "doc.md"
        f.write_text("# heading\nfirst version body text", encoding="utf-8")
        assert await pipeline.ingest_file(str(f)) is not None
        source = kstore.get_source_by_uri(str(f.resolve()))
        assert source is not None
        before = _item_ids(kstore, source["id"])
        assert before != set()

        calls = self._spy_resolver(pipeline)
        loop_thread = threading.get_ident()
        f.write_text("# heading\nsecond version body text", encoding="utf-8")
        assert await pipeline.ingest_file(str(f)) is not None

        assert calls, "the existing-local-file group read never ran"
        assert all(t != loop_thread for t in calls)
        # The prior version's group was replaced, not accumulated.
        assert _item_ids(kstore, source["id"]).isdisjoint(before)

    @pytest.mark.asyncio
    async def test_ingest_text_changed_existing_resolves_off_loop(self, pipeline, kstore):
        text = "body text long enough to split"
        content_hash = hashlib.sha256(text.encode()).hexdigest()
        # A source at the hash-derived URI whose recorded hash DIFFERS forces
        # the changed-content branch of the no-source_id path.
        source_id = kstore.add_source(
            "Doc",
            "manual",
            f"manual://{content_hash[:16]}",
            properties={"content_hash": "stale"},
        )
        stale = kstore.add_item("old", "superseded", "document", source_id=source_id)
        calls = self._spy_resolver(pipeline)
        loop_thread = threading.get_ident()

        job_id = await pipeline.ingest_text(text, "Doc")

        assert job_id is not None
        assert calls, "the changed-existing group read never ran"
        assert all(t != loop_thread for t in calls)
        assert stale not in _item_ids(kstore, source_id)

    @pytest.mark.asyncio
    async def test_ingest_text_replace_all_resolves_off_loop(self, pipeline, kstore):
        source_id = kstore.add_source("notes", "manual", "manual://fixed-uri")
        stale = kstore.add_item("old", "superseded", "document", source_id=source_id)
        calls = self._spy_resolver(pipeline)
        loop_thread = threading.get_ident()

        job_id = await pipeline.ingest_text(
            "body text long enough to split", "Doc", source_id=source_id
        )

        assert job_id is not None
        assert calls, "the replace-all group read never ran"
        assert all(t != loop_thread for t in calls)
        assert stale not in _item_ids(kstore, source_id)
