"""The embedding rebuild background job and the embedding-space identity.

The rebuild job, the watcher's self-heal, the shared embed signature, failure
accounting, the lost-update race, single flight, the dashboard cancel and the
large-rebuild warning.
The ``store`` fixture comes from ``knowledge_helpers.py``.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from knowledge_helpers import store_fixture  # noqa: F401

from kiro_crew.embeddings import PRIORITY_NORMAL

# ---------------------------------------------------------------------------
# Embedding rebuild background job
# ---------------------------------------------------------------------------


class _FakeEmbedder:
    """Embedder stub: returns a fixed vector and records which items it embedded.

    Records the scheduling class of every ``embed_for_item`` call so a caller's
    priority can be asserted; the shape mirrors ``InProcessEmbedder``, whose
    signature inputs are ``model`` + ``dim`` + ``content_budget``.
    """

    model = "fake-embed"
    dim = 4  # width of the vector below; feeds embed_signature like the real one
    base_url = ""
    content_budget = 10_000  # mirrors the real _EMBED_CONTENT_BUDGET default

    def __init__(self):
        self.embedded_titles: list[str] = []
        self.priorities: list[int] = []

    def is_available(self) -> bool:
        return True

    async def is_available_async(self) -> bool:
        return self.is_available()

    def embed_for_item(self, title, summary, content=None, *, priority=PRIORITY_NORMAL):
        self.embedded_titles.append(title)
        self.priorities.append(priority)
        return [0.1, 0.2, 0.3, 0.4]


@pytest.mark.asyncio
class TestRebuildEmbeddingsJob:
    async def _run(self, store, embedder, n_items):
        from kiro_crew.dashboard.handlers.knowledge import _rebuild_embeddings_job

        # Seed active items, each with a stale (single-element) embedding so we can
        # prove the rebuild overwrites in place rather than only filling NULLs.
        from kiro_crew.knowledge.embedder import floats_to_bytes

        for i in range(n_items):
            store.add_item(
                f"Item {i:03d}", f"body {i}", "document", embedding=floats_to_bytes([9.9])
            )
        job_id = "rebuildjob01"
        now = "2026-06-16T00:00:00"
        store.db.execute(
            "INSERT INTO ingestion_jobs (id, source_id, status, created_at, updated_at) "
            "VALUES (?, NULL, 'processing', ?, ?)",
            (job_id, now, now),
        )
        store.db.commit()
        await _rebuild_embeddings_job(None, store, embedder, job_id)
        return job_id

    async def test_rebuild_reembeds_all_items_across_batches(self, store):
        # More than one _REBUILD_BATCH_SIZE page to exercise the id-cursor loop.
        from kiro_crew.knowledge.embedder import embed_signature, floats_to_bytes
        from kiro_crew.knowledge.ingestion import _REBUILD_BATCH_SIZE

        n = _REBUILD_BATCH_SIZE + 5
        embedder = _FakeEmbedder()
        job_id = await self._run(store, embedder, n)

        # Every active item was embedded exactly once (cursor: no skips, no repeats).
        assert len(embedder.embedded_titles) == n
        assert len(set(embedder.embedded_titles)) == n

        job = store.db.execute("SELECT * FROM ingestion_jobs WHERE id = ?", (job_id,)).fetchone()
        assert job["status"] == "completed"
        assert job["items_total"] == n
        assert job["items_processed"] == n

        # Stale [9.9] vectors were overwritten in place with the new embedding, and
        # every item is stamped with the current signature + an embedded_at timestamp.
        row = store.db.execute(
            "SELECT embedding, embedding_sig, embedded_at FROM items "
            "WHERE status = 'active' LIMIT 1"
        ).fetchone()
        assert row["embedding"] == floats_to_bytes([0.1, 0.2, 0.3, 0.4])
        assert row["embedding_sig"] == embed_signature(embedder.model, embedder.dim)
        assert row["embedded_at"]

    async def test_rebuild_is_idempotent_skips_current_sig(self, store):
        # First rebuild stamps every item with the current sig.
        embedder = _FakeEmbedder()
        await self._run(store, embedder, 3)
        assert len(embedder.embedded_titles) == 3

        # A second rebuild on an unchanged setup finds nothing stale -> no-op.
        from kiro_crew.knowledge.ingestion import rebuild_embeddings

        second = _FakeEmbedder()
        processed = await rebuild_embeddings(store, second)
        assert processed == 0
        assert second.embedded_titles == []

    async def test_rebuild_partial_retry_resumes_only_stale(self, store):
        # One item already carries the current sig; the rest are stale (NULL sig).
        from kiro_crew.knowledge.embedder import embed_signature, floats_to_bytes
        from kiro_crew.knowledge.ingestion import rebuild_embeddings

        embedder = _FakeEmbedder()
        sig = embed_signature(embedder.model, embedder.dim)
        done = store.add_item(
            "done", "body", "document", embedding=floats_to_bytes([0.1, 0.2, 0.3, 0.4])
        )
        store.db.execute("UPDATE items SET embedding_sig = ? WHERE id = ?", (sig, done))
        for i in range(2):
            store.add_item(f"stale {i}", "body", "document")
        store.db.commit()

        processed = await rebuild_embeddings(store, embedder)
        # Only the two stale items re-embed; the already-current one is skipped.
        assert processed == 2
        assert sorted(embedder.embedded_titles) == ["stale 0", "stale 1"]

    async def test_rebuild_force_reembeds_current_sig(self, store):
        # All items already current; force=True ignores the sig and re-embeds all.
        embedder = _FakeEmbedder()
        await self._run(store, embedder, 3)

        from kiro_crew.knowledge.ingestion import rebuild_embeddings

        forced = _FakeEmbedder()
        processed = await rebuild_embeddings(store, forced, force=True)
        assert processed == 3
        assert len(forced.embedded_titles) == 3

    async def test_rebuild_marks_job_failed_on_error(self, store):
        class _BoomEmbedder(_FakeEmbedder):
            def embed_for_item(self, title, summary, content=None, *, priority=PRIORITY_NORMAL):
                raise RuntimeError("ollama down mid-rebuild")

        job_id = await self._run(store, _BoomEmbedder(), 3)
        job = store.db.execute("SELECT * FROM ingestion_jobs WHERE id = ?", (job_id,)).fetchone()
        assert job["status"] == "failed"
        assert "ollama down" in (job["error"] or "")

    async def test_rebuild_heartbeats_updated_at_per_item_not_per_batch(self, store):
        """A slow item must not let the single-flight claimer judge the live job
        abandoned mid-batch: updated_at is committed AFTER EACH item, so it
        advances within a batch rather than only at end-of-batch. A heartbeat that
        only lands at end-of-batch lets a second claimer duplicate the rebuild."""
        from kiro_crew.knowledge.ingestion import rebuild_embeddings

        # Capture the job row's COMMITTED updated_at as each item is embedded (the
        # embedder runs between the prior item's heartbeat commit and this one).
        seen_updated_at: list[str] = []

        class _RecordingEmbedder(_FakeEmbedder):
            def embed_for_item(self, title, summary, content=None, *, priority=PRIORITY_NORMAL):
                row = store.db.execute(
                    "SELECT updated_at FROM ingestion_jobs WHERE id = 'hbjob0000001'"
                ).fetchone()
                seen_updated_at.append(row["updated_at"] if row else "")
                return super().embed_for_item(title, summary, content)

        for i in range(3):
            store.add_item(f"hb {i}", "body", "document")
        job_id = "hbjob0000001"
        base = "2026-06-16T00:00:00"
        store.db.execute(
            "INSERT INTO ingestion_jobs (id, source_id, status, created_at, updated_at) "
            "VALUES (?, NULL, 'processing', ?, ?)",
            (job_id, base, base),
        )
        store.db.commit()

        await rebuild_embeddings(store, _RecordingEmbedder(), job_id=job_id)

        # By the 2nd/3rd item, the committed updated_at has advanced past the
        # batch-start value — proving a per-item heartbeat, not a per-batch one.
        assert seen_updated_at, "embedder never ran"
        assert any(
            ts > base for ts in seen_updated_at[1:]
        ), f"updated_at never advanced mid-batch: {seen_updated_at}"


@pytest.mark.asyncio
class TestWatcherSelfHeal:
    def _watcher(self, store, embedder):
        from kiro_crew.knowledge.watcher import KnowledgeWatcher

        class _Pipe:
            pass

        pipe = _Pipe()
        pipe.embedder = embedder
        return KnowledgeWatcher(store, pipe)

    async def test_stale_items_trigger_rebuild_job(self, store):
        # Items with NULL sig are stale -> watcher fires a tracked rebuild job.
        from kiro_crew.knowledge.embedder import embed_signature

        embedder = _FakeEmbedder()
        for i in range(3):
            store.add_item(f"Item {i}", "body", "document")
        watcher = self._watcher(store, embedder)

        await watcher._maybe_reembed_stale()
        assert watcher._reembed_task is not None
        await watcher._reembed_task

        job = store.db.execute(
            "SELECT * FROM ingestion_jobs WHERE source_id IS NULL "
            "ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        assert job["status"] == "completed"
        assert job["items_processed"] == 3
        assert len(embedder.embedded_titles) == 3
        sig = embed_signature(embedder.model, embedder.dim)
        stale = store.db.execute(
            "SELECT COUNT(*) AS c FROM items WHERE embedding_sig IS NULL OR embedding_sig != ?",
            (sig,),
        ).fetchone()["c"]
        assert stale == 0

    async def test_the_stale_count_reads_only_the_index(self, store):
        # The count ran at every boot beside memory preparation and read every
        # item row. With the covering index it never touches the table.
        from kiro_crew.knowledge.ingestion import count_stale_items

        assert store.has_embed_state_index()
        plan = " ".join(
            str(row[3])
            for row in store.db.execute(
                "EXPLAIN QUERY PLAN SELECT COUNT(*) AS c FROM items WHERE status = 'active' "
                "AND (embedding_sig IS NULL OR embedding_sig != ?) "
                "AND (embedded_at IS NULL OR embedded_at < ?)",
                ("sig", "2000-01-01"),
            ).fetchall()
        )
        assert "COVERING INDEX idx_items_embed_state" in plan
        store.add_item("one", "body", "document")
        assert count_stale_items(store, "sig") == 1

    async def test_no_scan_until_the_covering_index_exists(self, store, monkeypatch):
        # Before the post-bind build has made the index (first boot after an
        # upgrade) the count would read the whole table, so it waits a sweep.
        from kiro_crew.knowledge import watcher as watcher_mod

        embedder = _FakeEmbedder()
        for i in range(3):
            store.add_item(f"Item {i}", "body", "document")
        store.db.execute("DROP INDEX idx_items_embed_state")
        store.db.commit()
        counted: list[int] = []
        real_count = watcher_mod.count_stale_items

        def _count(*args, **kwargs):
            counted.append(1)
            return real_count(*args, **kwargs)

        monkeypatch.setattr(watcher_mod, "count_stale_items", _count)
        watcher = self._watcher(store, embedder)
        await watcher._maybe_reembed_stale()
        assert counted == []
        assert watcher._reembed_task is None
        store.ensure_search_indexes()
        await watcher._maybe_reembed_stale()
        assert counted == [1]
        assert watcher._reembed_task is not None
        await watcher._reembed_task

    async def test_a_missing_index_falls_back_to_the_full_count(self, store, monkeypatch, caplog):
        # A failed post-bind build leaves the index absent for the whole
        # process; the self-heal must not stop for good.
        from kiro_crew.knowledge import watcher as watcher_mod

        embedder = _FakeEmbedder()
        for i in range(3):
            store.add_item(f"Item {i}", "body", "document")
        store.db.execute("DROP INDEX idx_items_embed_state")
        store.db.commit()
        counted: list[int] = []
        real_count = watcher_mod.count_stale_items

        def _count(*args, **kwargs):
            counted.append(1)
            return real_count(*args, **kwargs)

        monkeypatch.setattr(watcher_mod, "count_stale_items", _count)
        watcher = self._watcher(store, embedder)
        for _ in range(watcher_mod._EMBED_INDEX_GRACE_SWEEPS):
            await watcher._maybe_reembed_stale()
        assert counted == []
        with caplog.at_level("WARNING", logger=watcher_mod.logger.name):
            await watcher._maybe_reembed_stale()
        assert counted == [1]
        assert "idx_items_embed_state is missing" in caplog.text
        assert watcher._reembed_task is not None
        await watcher._reembed_task

    async def test_no_stale_items_is_noop(self, store):
        # Everything already current -> no job created.
        from kiro_crew.knowledge.embedder import embed_signature, floats_to_bytes

        embedder = _FakeEmbedder()
        sig = embed_signature(embedder.model, embedder.dim)
        item_id = store.add_item(
            "current", "body", "document", embedding=floats_to_bytes([0.1, 0.2, 0.3, 0.4])
        )
        store.db.execute("UPDATE items SET embedding_sig = ? WHERE id = ?", (sig, item_id))
        store.db.commit()
        watcher = self._watcher(store, embedder)

        await watcher._maybe_reembed_stale()
        assert watcher._reembed_task is None
        assert embedder.embedded_titles == []

    async def test_single_flight_skips_when_job_processing(self, store):
        # A dashboard rebuild already in flight (fresh updated_at) -> watcher does
        # not stack a second.
        embedder = _FakeEmbedder()
        store.add_item("stale", "body", "document")
        # flake-ok: pre-existing naive local stamp written into the test's own rows, moved verbatim; tracked for a de-flake pass
        # nosemgrep: kirocrew.test-naive-local-time -- pre-existing naive stamp for the test's own rows, moved verbatim; tracked for a de-flake pass
        now = datetime.now().isoformat()
        store.db.execute(
            "INSERT INTO ingestion_jobs (id, source_id, status, created_at, updated_at) "
            "VALUES ('inflight0001', NULL, 'processing', ?, ?)",
            (now, now),
        )
        store.db.commit()
        watcher = self._watcher(store, embedder)

        await watcher._maybe_reembed_stale()
        assert watcher._reembed_task is None
        assert embedder.embedded_titles == []

    async def test_stale_processing_row_does_not_block(self, store):
        # A 'processing' row whose updated_at is older than the staleness window is
        # from a crash that bypassed cleanup -> the guard ignores it and the watcher
        # starts a fresh rebuild rather than being permanently blocked.
        from kiro_crew.knowledge.ingestion import _REBUILD_STALE_AFTER

        embedder = _FakeEmbedder()
        store.add_item("stale", "body", "document")
        # flake-ok: pre-existing naive local stamp written into the test's own rows, moved verbatim; tracked for a de-flake pass
        # nosemgrep: kirocrew.test-naive-local-time -- pre-existing naive stamp for the test's own rows, moved verbatim; tracked for a de-flake pass
        old = (datetime.now() - _REBUILD_STALE_AFTER - timedelta(minutes=1)).isoformat()
        store.db.execute(
            "INSERT INTO ingestion_jobs (id, source_id, status, created_at, updated_at) "
            "VALUES ('dead00000001', NULL, 'processing', ?, ?)",
            (old, old),
        )
        store.db.commit()
        watcher = self._watcher(store, embedder)

        await watcher._maybe_reembed_stale()
        assert watcher._reembed_task is not None
        await watcher._reembed_task
        assert embedder.embedded_titles == ["stale"]

    async def test_cancelled_job_row_is_finalized_not_left_processing(self, store):
        # If the rebuild task is cancelled (e.g. app shutdown), the job row must be
        # finalized to 'cancelled' and the CancelledError re-raised -- otherwise the
        # row stays 'processing' and permanently blocks the single-flight guard.
        embedder = _FakeEmbedder()
        store.add_item("item", "body", "document")
        watcher = self._watcher(store, embedder)
        # flake-ok: pre-existing naive local stamp written into the test's own rows, moved verbatim; tracked for a de-flake pass
        # nosemgrep: kirocrew.test-naive-local-time -- pre-existing naive stamp for the test's own rows, moved verbatim; tracked for a de-flake pass
        now = datetime.now().isoformat()
        store.db.execute(
            "INSERT INTO ingestion_jobs (id, source_id, status, created_at, updated_at) "
            "VALUES ('cancel000001', NULL, 'processing', ?, ?)",
            (now, now),
        )
        store.db.commit()

        async def _boom(*a, **k):
            raise asyncio.CancelledError()

        with patch("kiro_crew.knowledge.watcher.rebuild_embeddings", _boom):
            with pytest.raises(asyncio.CancelledError):
                await watcher._run_reembed_job(embedder, "cancel000001")

        row = store.db.execute(
            "SELECT status FROM ingestion_jobs WHERE id = 'cancel000001'"
        ).fetchone()
        assert row["status"] == "cancelled"


class TestEmbedSignature:
    def test_base_url_ignored_by_signature(self):
        # Embeddings run in-process (no external inference endpoint), so the sig
        # has no base_url input. Same model + width = stable signature.
        from kiro_crew.knowledge.embedder import embed_signature

        a = embed_signature("m", 1024)
        b = embed_signature("m", 1024)
        assert a == b

    def test_model_changes_signature(self):
        from kiro_crew.knowledge.embedder import embed_signature

        assert embed_signature("m1", 1024) != embed_signature("m2", 1024)

    def test_content_budget_changes_signature(self):
        # Changing the budget must change the embed signature, else items
        # truncated under the old budget would never be re-embedded.
        from kiro_crew.knowledge.embedder import embed_signature

        assert embed_signature("m", 1024) != embed_signature("m", 1024, content_budget=42)

    def test_dim_change_moves_both_identities(self):
        """A width change at a CONSTANT model id must move BOTH space identities.

        The ratchet on the fold. ``dim`` is exactly the axis a KB signature over
        ``model`` alone cannot see — a custom GGUF re-quantized to a different
        projection, a backend adopting the model's own width — and an identity
        that misses it leaves the KB serving old-width vectors as if nothing had
        changed. Asserting BOTH here is what makes dropping the input from either
        half fail a test rather than ship.
        """
        from kiro_crew.embeddings import embedding_space_signature
        from kiro_crew.knowledge.embedder import embed_signature

        assert embedding_space_signature("m", 1024) != embedding_space_signature("m", 768)
        assert embed_signature("m", 1024) != embed_signature("m", 768)

    def test_the_two_identities_partition_spaces_identically(self):
        """Whatever the inputs, the two identities agree on "same vector space?".

        For every pair of ``(model, dim)`` combinations, memory's signature and
        the knowledge library's must be equal for exactly the same pairs. This is
        the property that matters, but on its own it is satisfied by any injective
        hash over the same fields -- see the derivation test below for the half it
        cannot reach.
        """
        from kiro_crew.embeddings import embedding_space_signature
        from kiro_crew.knowledge.embedder import embed_signature

        spaces = [("m", 1024), ("m", 768), ("other", 1024), ("other", 768)]
        memory = [embedding_space_signature(model, dim) for model, dim in spaces]
        knowledge = [embed_signature(model, dim) for model, dim in spaces]
        for i, left in enumerate(spaces):
            for j, right in enumerate(spaces):
                assert (memory[i] == memory[j]) == (knowledge[i] == knowledge[j]), (
                    f"{left} vs {right}: memory and knowledge disagree about whether "
                    "these are the same vector space"
                )

    def test_the_kb_identity_is_derived_from_memorys_not_reassembled(self, monkeypatch):
        """The KB's space identity is a FUNCTION of memory's, not a twin of it.

        Agreement on ``(model, dim)`` is not derivation: an independently
        assembled hash over the same two fields agrees on every input and is
        still a second definition of "same vector space", so the next field added
        to ``embedding_space_signature`` reaches memory alone.

        Observed by DISPLACING the shared function and watching the KB's value
        follow. Substituting one that ignores ``dim`` must collapse the KB's two
        widths onto one signature and keep its two models apart -- which holds
        only if every ``(model, dim)`` input reaches ``embed_signature`` through
        that one function.
        """
        from kiro_crew import embeddings as embeddings_mod
        from kiro_crew.knowledge.embedder import embed_signature

        assert embed_signature("m", 1024) != embed_signature("m", 768)

        monkeypatch.setattr(
            embeddings_mod, "embedding_space_signature", lambda model, dim: f"space::{model}"
        )
        assert embed_signature("m", 1024) == embed_signature("m", 768)
        assert embed_signature("m", 1024) != embed_signature("other", 1024)
        # The KB's own input is unaffected: it is folded on outside the space half.
        assert embed_signature("m", 1024) != embed_signature("m", 1024, content_budget=42)

    def test_embedder_signature_matches_model_signature(self):
        from kiro_crew.knowledge.embedder import (
            _EMBED_CONTENT_BUDGET,
            embed_signature,
            embedder_signature,
        )

        class _E:
            model = "m"
            dim = 1024
            content_budget = _EMBED_CONTENT_BUDGET

        assert embedder_signature(_E()) == embed_signature("m", 1024)

    def test_embedder_signature_reads_the_width_from_the_embedder(self):
        """``embedder_signature`` must not pin the width to a literal.

        It is the single call sites use, so a width it did not read from the
        embedder would make every per-item sig claim a space the vectors are not
        in — and no other test would notice, because the value would still be
        internally consistent.
        """
        from kiro_crew.knowledge.embedder import (
            _EMBED_CONTENT_BUDGET,
            embed_signature,
            embedder_signature,
        )

        class _E:
            model = "m"
            dim = 1024
            content_budget = _EMBED_CONTENT_BUDGET

        narrow = _E()
        narrow.dim = 768
        assert embedder_signature(narrow) == embed_signature("m", 768)
        assert embedder_signature(narrow) != embedder_signature(_E())


class _FlakyEmbedder(_FakeEmbedder):
    """Returns None for items whose title is in ``fail_titles`` (transient failure)."""

    def __init__(self, fail_titles):
        super().__init__()
        self.fail_titles = set(fail_titles)

    def embed_for_item(self, title, summary, content=None, *, priority=PRIORITY_NORMAL):
        self.embedded_titles.append(title)
        self.priorities.append(priority)
        if title in self.fail_titles:
            return None
        return [0.1, 0.2, 0.3, 0.4]


@pytest.mark.asyncio
class TestRebuildFailureAccounting:
    async def test_none_vec_not_counted_as_processed(self, store):
        # When embed returns None, the item is NOT counted as processed, its sig
        # stays stale (so it retries), but items_failed reflects the miss.
        from kiro_crew.knowledge.ingestion import rebuild_embeddings

        ok = store.add_item("ok", "body", "document")
        bad = store.add_item("bad", "body", "document")
        store.db.commit()
        embedder = _FlakyEmbedder(fail_titles=["bad"])

        processed = await rebuild_embeddings(store, embedder)
        assert processed == 1  # only the successful one
        # Failed item kept a NULL sig (retryable) but got an embedded_at attempt stamp.
        row = store.db.execute(
            "SELECT embedding_sig, embedded_at FROM items WHERE id = ?", (bad,)
        ).fetchone()
        assert row["embedding_sig"] is None
        assert row["embedded_at"]
        ok_row = store.db.execute("SELECT embedding_sig FROM items WHERE id = ?", (ok,)).fetchone()
        assert ok_row["embedding_sig"] is not None

    async def test_job_row_tracks_items_failed(self, store):
        from kiro_crew.knowledge.ingestion import rebuild_embeddings

        for t in ("a", "b", "c"):
            store.add_item(t, "body", "document")
        store.db.commit()
        # flake-ok: pre-existing naive local stamp written into the test's own rows, moved verbatim; tracked for a de-flake pass
        # nosemgrep: kirocrew.test-naive-local-time -- pre-existing naive stamp for the test's own rows, moved verbatim; tracked for a de-flake pass
        now = datetime.now().isoformat()
        store.db.execute(
            "INSERT INTO ingestion_jobs (id, source_id, status, created_at, updated_at) "
            "VALUES ('jobfail00001', NULL, 'processing', ?, ?)",
            (now, now),
        )
        store.db.commit()
        embedder = _FlakyEmbedder(fail_titles=["b"])

        await rebuild_embeddings(store, embedder, job_id="jobfail00001")
        job = store.db.execute(
            "SELECT items_processed, items_failed FROM ingestion_jobs WHERE id = 'jobfail00001'"
        ).fetchone()
        assert job["items_processed"] == 2
        assert job["items_failed"] == 1

    async def test_watcher_backs_off_recently_failed_item(self, store):
        # A perpetually-failing item keeps a stale sig; once it has a recent
        # embedded_at attempt stamp, the watcher's stale count excludes it so it
        # doesn't re-trigger a rebuild every scan (post-merge retrigger-loop fix).
        from kiro_crew.knowledge.embedder import embedder_signature
        from kiro_crew.knowledge.ingestion import count_stale_items

        embedder = _FakeEmbedder()
        sig = embedder_signature(embedder)
        item = store.add_item("perma-fail", "body", "document")
        # Stale sig but attempted just now -> in backoff window -> not counted.
        store.db.execute(
            "UPDATE items SET embedded_at = ? WHERE id = ?",
            # flake-ok: pre-existing naive local stamp written into the test's own rows, moved verbatim; tracked for a de-flake pass
            # nosemgrep: kirocrew.test-naive-local-time -- pre-existing naive stamp for the test's own rows, moved verbatim; tracked for a de-flake pass
            (datetime.now().isoformat(), item),
        )
        store.db.commit()
        assert count_stale_items(store, sig) == 0
        # An item never attempted (NULL embedded_at) IS counted.
        store.add_item("never-tried", "body", "document")
        store.db.commit()
        assert count_stale_items(store, sig) == 1


@pytest.mark.asyncio
class TestRebuildLostUpdateRace:
    async def test_concurrent_write_skips_stale_vector_update(self, store):
        # If a concurrent writer (file re-ingest) bumps the row's updated_at past the
        # rebuild's read snapshot while the embedder is working, the rebuild's UPDATE
        # must not land -- otherwise a new-content row gets an old-content vector
        # stamped "current". The guarded UPDATE drops on the contended row.
        import sqlite3

        from kiro_crew.knowledge.ingestion import rebuild_embeddings

        item = store.add_item("racey", "old body", "document")
        store.db.commit()
        # embed_for_item runs in a worker thread (run_in_executor), so it cannot
        # touch the main-thread sqlite connection. The concurrent writer opens its
        # OWN connection to the same db file -- which is exactly the real race
        # (ingestion writing while a rebuild embeds).
        db_path = store.db.execute("PRAGMA database_list").fetchall()[0]["file"]

        class _RacingEmbedder(_FakeEmbedder):
            def __init__(self, path, iid):
                super().__init__()
                self._path = path
                self._iid = iid

            def embed_for_item(self, title, summary, content=None, *, priority=PRIORITY_NORMAL):
                # Simulate a concurrent ingestion write landing mid-embed: bump
                # updated_at into the future relative to the rebuild's snapshot.
                conn = sqlite3.connect(self._path, timeout=30, isolation_level=None)
                try:
                    conn.execute(
                        "UPDATE items SET updated_at = ? WHERE id = ?",
                        # flake-ok: pre-existing naive local stamp written into the test's own rows, moved verbatim; tracked for a de-flake pass
                        # nosemgrep: kirocrew.test-naive-local-time -- pre-existing naive stamp for the test's own rows, moved verbatim; tracked for a de-flake pass
                        ((datetime.now() + timedelta(minutes=1)).isoformat(), self._iid),
                    )
                finally:
                    conn.close()
                return super().embed_for_item(title, summary, content)

        embedder = _RacingEmbedder(db_path, item)
        processed = await rebuild_embeddings(store, embedder)
        assert processed == 0  # lost the race -> not stamped
        row = store.db.execute("SELECT embedding_sig FROM items WHERE id = ?", (item,)).fetchone()
        assert row["embedding_sig"] is None  # left stale for its own re-embed


@pytest.mark.asyncio
class TestStartRebuildJobSingleFlight:
    async def test_concurrent_claims_create_one_job(self, store):
        # The atomic claim must let only ONE of two racing callers (watcher tick vs
        # dashboard click) create a job -- the other gets None.
        from kiro_crew.knowledge.ingestion import start_rebuild_job

        first = start_rebuild_job(store)
        second = start_rebuild_job(store)
        assert first is not None
        assert second is None
        n = store.db.execute(
            "SELECT COUNT(*) AS c FROM ingestion_jobs "
            "WHERE source_id IS NULL AND status = 'processing'"
        ).fetchone()["c"]
        assert n == 1

    async def test_stale_processing_rows_swept_to_abandoned(self, store):
        # Crashed leftovers (stale 'processing' rows) are finalized to 'abandoned'
        # when a new rebuild claims the slot, so they don't accumulate forever.
        from kiro_crew.knowledge.ingestion import _REBUILD_STALE_AFTER, start_rebuild_job

        # flake-ok: pre-existing naive local stamp written into the test's own rows, moved verbatim; tracked for a de-flake pass
        # nosemgrep: kirocrew.test-naive-local-time -- pre-existing naive stamp for the test's own rows, moved verbatim; tracked for a de-flake pass
        old = (datetime.now() - _REBUILD_STALE_AFTER - timedelta(minutes=1)).isoformat()
        for i in range(3):
            store.db.execute(
                "INSERT INTO ingestion_jobs (id, source_id, status, created_at, updated_at) "
                "VALUES (?, NULL, 'processing', ?, ?)",
                (f"ghost{i:08d}", old, old),
            )
        store.db.commit()

        job_id = start_rebuild_job(store)
        assert job_id is not None
        abandoned = store.db.execute(
            "SELECT COUNT(*) AS c FROM ingestion_jobs WHERE status = 'abandoned'"
        ).fetchone()["c"]
        assert abandoned == 3
        # No stale 'processing' rows survived; only the fresh claim is processing.
        processing = store.db.execute(
            "SELECT id FROM ingestion_jobs WHERE status = 'processing'"
        ).fetchall()
        assert [r["id"] for r in processing] == [job_id]


@pytest.mark.asyncio
class TestDashboardRebuildCancel:
    async def test_dashboard_job_cancel_finalizes_row(self, store):
        # Same cancel-finalization contract as the watcher, in the dashboard wrapper.
        from kiro_crew.dashboard.handlers.knowledge import _rebuild_embeddings_job

        embedder = _FakeEmbedder()
        store.add_item("item", "body", "document")
        # flake-ok: pre-existing naive local stamp written into the test's own rows, moved verbatim; tracked for a de-flake pass
        # nosemgrep: kirocrew.test-naive-local-time -- pre-existing naive stamp for the test's own rows, moved verbatim; tracked for a de-flake pass
        now = datetime.now().isoformat()
        store.db.execute(
            "INSERT INTO ingestion_jobs (id, source_id, status, created_at, updated_at) "
            "VALUES ('dashcancel01', NULL, 'processing', ?, ?)",
            (now, now),
        )
        store.db.commit()

        async def _boom(*a, **k):
            raise asyncio.CancelledError()

        with patch("kiro_crew.dashboard.handlers.knowledge.rebuild_embeddings", _boom):
            with pytest.raises(asyncio.CancelledError):
                await _rebuild_embeddings_job(None, store, embedder, "dashcancel01")

        row = store.db.execute(
            "SELECT status FROM ingestion_jobs WHERE id = 'dashcancel01'"
        ).fetchone()
        assert row["status"] == "cancelled"


@pytest.mark.asyncio
class TestWatcherLargeRebuildWarning:
    """A stale count at/over _LARGE_REBUILD_WARN_THRESHOLD logs a prominent WARNING."""

    def _watcher(self, store):
        from kiro_crew.knowledge.watcher import KnowledgeWatcher

        class _Pipe:
            pass

        pipe = _Pipe()
        pipe.embedder = _FakeEmbedder()
        return KnowledgeWatcher(store, pipe)

    async def test_large_stale_count_logs_warning(self, store, monkeypatch, caplog):
        import kiro_crew.knowledge.watcher as watcher_mod

        monkeypatch.setattr(watcher_mod, "_LARGE_REBUILD_WARN_THRESHOLD", 3)
        for i in range(3):
            store.add_item(f"Item {i}", "body", "document")
        watcher = self._watcher(store)

        with caplog.at_level(logging.WARNING, logger="kiro_crew.knowledge.watcher"):
            await watcher._maybe_reembed_stale()
        assert watcher._reembed_task is not None
        await watcher._reembed_task

        warnings = [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING and "full background re-embed" in r.getMessage()
        ]
        assert len(warnings) == 1
        assert "3 items" in warnings[0].getMessage()

    async def test_small_stale_count_no_warning(self, store, monkeypatch, caplog):
        import kiro_crew.knowledge.watcher as watcher_mod

        monkeypatch.setattr(watcher_mod, "_LARGE_REBUILD_WARN_THRESHOLD", 100)
        store.add_item("Only item", "body", "document")
        watcher = self._watcher(store)

        with caplog.at_level(logging.WARNING, logger="kiro_crew.knowledge.watcher"):
            await watcher._maybe_reembed_stale()
        assert watcher._reembed_task is not None
        await watcher._reembed_task

        assert not [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING and "full background re-embed" in r.getMessage()
        ]
