"""Embedding-model apply endpoint + re-embed progress.

Covers the three things the dashboard control needs: validating a path WITHOUT
persisting it, applying a change live (no gateway restart), and observable
progress while the background re-embed runs.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import kiro_crew.embeddings as embeddings_mod
from kiro_crew.embeddings import ReembedProgress, reembed_progress, validate_custom_model_path
from kiro_crew.vector_memory import VectorMemoryStore

# Wider than the production floor so the file is not read as a truncated
# placeholder.
_MODEL_SIZE = embeddings_mod._GGUF_MIN_BYTES + 100_000


def _write_model(path: Path) -> Path:
    """Write a stand-in model file of _MODEL_SIZE bytes.

    Sparse rather than a bytes literal: nothing here reads the content, only
    ``stat().st_size``, and a literal this size would be allocated while the
    module is IMPORTED -- charging every xdist worker for it at collection.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as fh:
        fh.truncate(_MODEL_SIZE)
    return path


class TestGatedCandidateLifecycle:
    """A live model swap is safe only if the candidate is gated end-to-end.

    Each test here pins one failure mode that review found in earlier revisions:
    dual residency, embedder resurrection after close, a serving window before
    reconciliation, an unknown adopted width, and a wedged load pinning progress.
    """

    def test_candidate_is_built_gated_and_adopts_its_width(self, tmp_path) -> None:
        from kiro_crew.embeddings import build_gated_candidate

        model = _write_model(tmp_path / "bge.gguf")
        cand = build_gated_candidate(model)
        assert cand.dim == 0, "width is unknown until the model loads"
        assert cand._serving is False, "a candidate must not serve before activation"
        assert cand.model_id, "an empty id would collapse distinct spaces together"

    def test_a_gated_candidate_hands_out_no_vectors(self, tmp_path) -> None:
        """The window between load and reconcile is the corruption window."""
        from kiro_crew.embeddings import build_gated_candidate

        cand = build_gated_candidate(_write_model(tmp_path / "m.gguf"))
        cand._llm = object()  # pretend the load landed
        assert cand.embed_batch(["hello"]) is None
        assert cand.embed("hello") is None

    def test_activation_opens_the_gate(self, tmp_path, monkeypatch) -> None:
        from kiro_crew.embeddings import build_gated_candidate

        cand = build_gated_candidate(_write_model(tmp_path / "m.gguf"))
        cand.activate()
        assert cand._serving is True

    def test_is_ready_reports_load_state_not_serving_state(self, tmp_path) -> None:
        """Reconcile must be able to confirm the model loaded while still gated."""
        from kiro_crew.embeddings import build_gated_candidate

        cand = build_gated_candidate(_write_model(tmp_path / "m.gguf"))
        cand._llm = object()
        assert cand.is_ready() is True
        assert cand.embed_batch(["x"]) is None

    def test_retire_is_terminal_so_a_stale_holder_cannot_resurrect_it(self, tmp_path) -> None:
        """A swapped-out embedder must not reload ~700MB from a stale caller.

        close() is deliberately REUSABLE (it clears the failure cooldown), so the
        terminal form is retire() — used only when the embedder leaves the
        singleton.
        """
        from kiro_crew.embeddings import LlamaCppEmbedder

        emb = LlamaCppEmbedder(model_path=_write_model(tmp_path / "m.gguf"), dim=8)
        emb.retire()
        assert emb.embed_batch(["hello"]) is None
        assert emb._closed is True

    def test_close_stays_reusable_for_every_other_caller(self, tmp_path) -> None:
        """Terminality must not leak into plain close(): tests + callers rely on it."""
        from kiro_crew.embeddings import LlamaCppEmbedder

        emb = LlamaCppEmbedder(model_path=_write_model(tmp_path / "m.gguf"), dim=8)
        emb.close()
        assert emb._closed is False, "close() is an unload, not a retirement"

    def test_adopt_mode_refuses_an_unreadable_width(self, tmp_path, monkeypatch) -> None:
        """dim=0 must never be published: every produced vector would be rejected."""
        import kiro_crew.embeddings as emb

        class _NoWidth:
            def __init__(self, **kwargs: object) -> None:
                pass

            def n_embd(self) -> int:
                return 0

        monkeypatch.setattr(emb, "_load_llama_class", lambda: _NoWidth)
        cand = emb.LlamaCppEmbedder(
            model_path=_write_model(tmp_path / "m.gguf"), dim=0, serving=False
        )
        cand._load_model()
        assert cand.is_ready() is False, "an unknown width is a failed load"
        assert cand.dim == 0

    def test_adopt_mode_accepts_a_positive_width(self, tmp_path, monkeypatch) -> None:
        import kiro_crew.embeddings as emb

        class _Wide:
            def __init__(self, **kwargs: object) -> None:
                pass

            def n_embd(self) -> int:
                return 768

        monkeypatch.setattr(emb, "_load_llama_class", lambda: _Wide)
        cand = emb.LlamaCppEmbedder(
            model_path=_write_model(tmp_path / "m.gguf"), dim=0, serving=False
        )
        cand._load_model()
        assert cand.dim == 768
        assert cand.is_ready() is True

    def test_boot_mismatch_refusal_is_unchanged_by_adopt_mode(
        self, tmp_path, monkeypatch
    ) -> None:
        """#961's loud refusal must still fire for a concrete configured dim."""
        import kiro_crew.embeddings as emb

        class _Wide:
            def __init__(self, **kwargs: object) -> None:
                pass

            def n_embd(self) -> int:
                return 768

        monkeypatch.setattr(emb, "_load_llama_class", lambda: _Wide)
        boot = emb.LlamaCppEmbedder(model_path=_write_model(tmp_path / "m.gguf"), dim=1024)
        boot._load_model()
        assert boot.is_ready() is False, "a 768-wide model must not load as 1024"
        assert boot.dim == 1024, "the configured width must not be silently adopted"

    def test_a_late_publish_into_a_retired_candidate_is_dropped(
        self, tmp_path, monkeypatch
    ) -> None:
        """A timed-out apply retires the candidate; its loader must not publish."""
        import kiro_crew.embeddings as emb

        freed: list[bool] = []

        class _Slow:
            def __init__(self, **kwargs: object) -> None:
                pass

            def n_embd(self) -> int:
                return 768

            def close(self) -> None:
                freed.append(True)

        monkeypatch.setattr(emb, "_load_llama_class", lambda: _Slow)
        cand = emb.LlamaCppEmbedder(
            model_path=_write_model(tmp_path / "m.gguf"), dim=0, serving=False
        )
        cand.retire()         # the apply timed out and rolled back
        cand._load_model()    # the abandoned loader finishes afterwards
        assert cand.is_ready() is False, "a retired candidate must stay unloaded"
        assert freed == [True], "the abandoned model must be freed, not published"

    def test_install_closes_the_outgoing_model(self, monkeypatch) -> None:
        """Closing the outgoing model is what keeps peak residency at one."""
        import kiro_crew.embeddings as emb

        closed: list[str] = []

        class _Fake:
            def __init__(self, name: str) -> None:
                self.name = name

            def close(self) -> None:
                closed.append(self.name)

        old, new = _Fake("old"), _Fake("new")
        monkeypatch.setattr(emb, "_shared_embedder", old, raising=False)
        emb.install_shared_embedder(new)  # type: ignore[arg-type]
        assert closed == ["old"]
        assert emb.get_shared_embedder() is new

    def test_install_is_idempotent_for_the_same_object(self, monkeypatch) -> None:
        import kiro_crew.embeddings as emb

        closed: list[str] = []

        class _Fake:
            def close(self) -> None:
                closed.append("self")

        same = _Fake()
        monkeypatch.setattr(emb, "_shared_embedder", same, raising=False)
        emb.install_shared_embedder(same)  # type: ignore[arg-type]
        assert closed == []

    def test_serving_accessor_distinguishes_candidate_from_live(self, monkeypatch) -> None:
        """The rollback branch needs to tell pre- from post-activation failure."""
        import kiro_crew.embeddings as emb

        class _Cand:
            _serving = False

        monkeypatch.setattr(emb, "_shared_embedder", _Cand(), raising=False)
        assert emb.embedding_backend_serving() is False
        monkeypatch.setattr(emb, "_shared_embedder", None, raising=False)
        assert emb.embedding_backend_serving() is False


class TestEnvOverrideRefusal:
    """With KIROCREW_EMBED_MODEL_PATH set, applying from the dashboard is refused.

    ``resolve_custom_model`` takes the PATH from the env var but always reads
    ``memory.embedding_dim`` from CONFIG. Persisting a model's width here while the
    env pins a different path produces a pair ``_load_model`` refuses on the width
    check — so the previously-working env-pinned model becomes unloadable on every
    restart until config.json is hand-edited. A config write cannot take effect
    under the override anyway, so the only safe answer is to refuse.
    """

    def test_handler_checks_the_env_var_before_arming_progress(self) -> None:
        import inspect

        from kiro_crew.dashboard.handlers import memory as mem

        src = inspect.getsource(mem.api_memory_embedding_model)
        env_at = src.find("KIROCREW_EMBED_MODEL_PATH")
        arm_at = src.find("prog.begin_apply()")
        assert -1 not in (env_at, arm_at)
        assert env_at < arm_at, "refuse before arming, or the tracker wedges at applying"
        assert "env_override_active" in src

    def test_resolve_takes_path_from_env_but_dim_from_config(self, monkeypatch, tmp_path) -> None:
        """Pin the precedence asymmetry that makes the write destructive."""
        import kiro_crew.embeddings as emb

        model = _write_model(tmp_path / "env-model.gguf")
        monkeypatch.setenv("KIROCREW_EMBED_MODEL_PATH", str(model))
        monkeypatch.setattr(
            emb, "_read_memory_config",
            lambda: {"embed_model_path": "/models/config-model.gguf", "embedding_dim": 768},
        )
        spec = emb.resolve_custom_model()
        assert spec is not None
        assert spec.path == model, "the env var wins for the path"
        assert spec.dim == 768, "but the width still comes from config — the mismatch source"


class TestSpaceGenerationGuard:
    """A vector produced in the OLD space must never commit after the swap.

    The gate stops the NEW model serving early, but it cannot un-produce a vector
    the OLD model already returned. Such a vector commits behind the reconcile,
    which has already swept that row, and backfill only ever revisits NULLs — so
    it persists in the wrong space forever. A width comparison cannot catch it:
    two different models of the same width are different spaces.
    """

    def _store(self, tmp_path):
        return VectorMemoryStore(db_path=tmp_path / "m.db", embedding_dim=4)

    def test_generation_starts_at_zero_and_bumps(self, tmp_path) -> None:
        store = self._store(tmp_path)
        assert store._space_generation == 0
        store.begin_space_change()
        assert store._space_generation == 1

    def test_a_swap_during_embedding_discards_the_stale_vector(self, tmp_path) -> None:
        store = self._store(tmp_path)

        def _embed_then_swap(text: str) -> list[float]:
            # Simulates the model change landing while this call is in flight.
            store.begin_space_change()
            return [0.1, 0.2, 0.3, 0.4]

        store.embed_fn = _embed_then_swap
        assert store._try_embed("hello") is None, (
            "a vector produced across a space change must be dropped, not committed"
        )

    def test_a_normal_embed_is_unaffected(self, tmp_path) -> None:
        store = self._store(tmp_path)
        store.embed_fn = lambda text: [0.1, 0.2, 0.3, 0.4]
        assert store._try_embed("hello") == [0.1, 0.2, 0.3, 0.4]

    def test_guard_catches_a_same_width_swap(self, tmp_path) -> None:
        """The case a dim check misses: two different models, identical width."""
        store = self._store(tmp_path)
        before_dim = store._embedding_dim

        def _embed_then_same_width_swap(text: str) -> list[float]:
            store.begin_space_change()
            store.set_embedding_dim(before_dim)  # no width change at all
            return [1.0, 2.0, 3.0, 4.0]

        store.embed_fn = _embed_then_same_width_swap
        assert store._try_embed("hello") is None
        assert store._embedding_dim == before_dim

    def test_apply_bumps_the_generation_before_waiting_for_the_load(self) -> None:
        """The bump must precede the load, or the in-flight window is unguarded."""
        import inspect

        from kiro_crew.dashboard.handlers import memory as mem

        src = inspect.getsource(mem._apply_embedding_model)
        bump_at = src.find("begin_space_change()")
        wait_at = src.find("wait_ready(")
        assert -1 not in (bump_at, wait_at)
        assert bump_at < wait_at


class TestKnowledgeIngestSignatureBinding:
    """An in-flight knowledge embed must be stamped with ITS OWN model's signature.

    `InProcessEmbedder.model` resolves the LIVE shared singleton
    (`.model` -> `_get_embedder()` -> `get_shared_embedder()`), so evaluating
    `embedder_signature()` at UPDATE time reads whichever model is current THEN,
    not the one that produced the vector. A live swap landing in that gap would
    stamp an old-model vector with the NEW signature — and the re-embed sweep is
    sig-gated (`embedding_sig IS NULL OR embedding_sig != ?`), so the row would be
    skipped forever instead of self-healing on the next watcher pass.

    Before this PR the only way to change models was config + restart, and a
    restart cannot swap mid-ingestion — the live swap is what makes it reachable,
    which is why the fix belongs here.
    """

    def test_signature_is_captured_before_the_embed(self) -> None:
        import inspect

        from kiro_crew.knowledge.ingestion import IngestionPipeline

        src = inspect.getsource(IngestionPipeline._embed_item)
        cap = src.find("sig = embedder_signature(self.embedder)")
        embed = src.find("embed_for_item")
        update = src.find("UPDATE items SET embedding")
        assert cap != -1, "the signature must be captured into a local"
        assert cap < embed, (
            "capture the signature BEFORE the embed — capturing after lets a swap "
            "stamp an old-model vector with the new signature"
        )
        assert cap < update
        assert "embedder_signature(self.embedder)," not in src, (
            "the UPDATE must bind the captured sig, not re-evaluate it inline"
        )

    def test_the_batch_path_still_binds_sig_explicitly(self) -> None:
        """_write_item_embedding already took sig as a parameter — keep it that way."""
        import inspect

        from kiro_crew.knowledge.ingestion import _write_item_embedding

        assert "sig" in inspect.signature(_write_item_embedding).parameters


class TestNonObjectJsonBody:
    """`[]` / `"s"` / `5` are VALID JSON, so request.json() returns them.

    Only the subsequent `.get()` fails — and it sits outside the try that wraps
    request.json(), so the AttributeError escaped as a 500 for what is really
    malformed client input. Must be the same 400 contract as unparseable bytes.
    """

    def _request(self, payload):
        class _Req:
            headers: dict = {}

            def __init__(self, p):
                self._p = p
                self.app = {"state": object()}

            async def json(self):
                return self._p

        return _Req(payload)

    @pytest.mark.parametrize("payload", [[], "a string", 5, 1.5, True, None])
    def test_non_object_bodies_return_400_not_500(self, payload, monkeypatch) -> None:
        import asyncio as _asyncio

        from kiro_crew.dashboard.handlers import memory as mem

        # The restricted-session gate runs first; let it through so the body
        # check is what we exercise.
        monkeypatch.setattr(mem, "_is_restricted_session", lambda state, req: False)
        resp = _asyncio.new_event_loop().run_until_complete(
            mem.api_memory_embedding_model(self._request(payload))
        )
        assert resp.status == 400, f"{payload!r} must be a 400, not a crash"
        assert b"invalid_json" in resp.body

    def test_the_guard_is_an_isinstance_check_on_dict(self) -> None:
        """Guard against a refactor that only special-cases lists."""
        import inspect

        from kiro_crew.dashboard.handlers.memory import api_memory_embedding_model

        src = inspect.getsource(api_memory_embedding_model)
        assert "isinstance(body, dict)" in src
        assert src.index("isinstance(body, dict)") < src.index('body.get("path"')


class TestApplyIsAudited:
    """A model change must emit an SEL event on the ALLOWED path, not only denials.

    Applying a model mutates config AND reshapes the whole vector store (reconcile
    NULLs every embedding, the backfill re-embeds it). Auditing only the
    restricted-session denial would leave an operator's SEL log showing blocked
    attempts while hiding the applies that actually happened.
    """

    def test_handler_audits_both_outcomes(self) -> None:
        import inspect

        from kiro_crew.dashboard.handlers.memory import api_memory_embedding_model

        src = inspect.getsource(api_memory_embedding_model)
        assert 'outcome="denied"' in src, "the restricted-session denial must stay audited"
        assert 'outcome="allowed"' in src, (
            "an allowed model change must be audited too — denial-only auditing "
            "hides the mutation that actually reshaped the vector store"
        )
        assert src.index('outcome="allowed"') < src.index("prog.begin_apply()"), (
            "audit the intent BEFORE the worker starts, so a mid-apply crash still "
            "leaves a record"
        )

    def test_the_allowed_audit_names_the_same_operation(self) -> None:
        """Both outcomes must share the operation name so the log filters cleanly."""
        import inspect

        from kiro_crew.dashboard.handlers.memory import api_memory_embedding_model

        src = inspect.getsource(api_memory_embedding_model)
        assert src.count('operation="memory.embedding_model"') >= 2


class TestWidthIsRestoredOnRollback:
    """Every rollback must put the store's width back.

    The apply retargets the store to the candidate's width before reconciling.
    If a rollback restores the PREVIOUS model but leaves the store on the NEW
    width, backfill's per-row shape check and build_faiss_index' width check
    reject every vector — and reconcile has already NULLed the corpus — so memory
    stays keyword-only for the rest of the process lifetime.
    """

    def test_every_rollback_branch_restores_the_width(self) -> None:
        import inspect

        from kiro_crew.dashboard.handlers.memory import _apply_embedding_model

        src = inspect.getsource(_apply_embedding_model)
        resets = src.count("reset_shared_embedder()")
        restores = src.count("_restore_dim()") - src.count("def _restore_dim()")
        # The pre-retarget branch resets without needing a restore; every branch
        # AFTER the retarget must pair the two.
        assert restores >= resets - 1, (
            f"{resets} rollback(s) but only {restores} width restore(s) — a rollback "
            "that leaves the store on the new width strands memory keyword-only"
        )

    def test_restore_helper_is_defined_before_the_try(self) -> None:
        """The catch-all handler calls it, so it must exist on every failure path."""
        import inspect

        from kiro_crew.dashboard.handlers.memory import _apply_embedding_model

        src = inspect.getsource(_apply_embedding_model)
        assert src.index("def _restore_dim()") < src.index("    try:"), (
            "_restore_dim must be hoisted above the try, or a failure before the "
            "retarget makes the exception handler itself raise NameError"
        )

    def test_restore_is_a_no_op_when_the_width_never_changed(self, tmp_path) -> None:
        """A same-width swap must not thrash the store or drop its index."""
        store = VectorMemoryStore(db_path=tmp_path / "m.db", embedding_dim=4)
        store.init()
        try:
            assert store.set_embedding_dim(4) is False, (
                "set_embedding_dim must report False when the width is unchanged, "
                "which is what makes the restore a no-op"
            )
        finally:
            store.close()


class TestCommitTimeGenerationCheck:
    """A swap landing between the embed and the DB lock must not commit the vector.

    `_try_embed` discards a vector produced ACROSS a space change, but it returns
    before the caller takes `_db_lock`. A swap can land in that gap — most
    plausibly while the write queues behind reconcile's own lock hold — and the
    committed vector would then be in the previous space, invisible to reconcile
    (already swept) and to backfill (only refills NULLs).

    Only two paths persist a vector concurrently with a swap: `write_episodic` and
    `write_lesson`. Query paths embed but persist nothing, and the backfill paths
    run after activation under the new model.
    """

    def _store(self, tmp_path):
        store = VectorMemoryStore(db_path=tmp_path / "m.db", embedding_dim=4)
        store.init()
        return store

    def test_episodic_stores_null_when_a_swap_lands_before_the_lock(self, tmp_path) -> None:
        store = self._store(tmp_path)
        try:
            # Return a vector, then bump the generation so the swap lands in the gap
            # between _try_embed returning and the write acquiring _db_lock.
            def _embed_then_swap(text: str) -> list[float]:
                vec = [0.1, 0.2, 0.3, 0.4]
                store.begin_space_change()
                return vec

            store.embed_fn = _embed_then_swap
            assert store.write_episodic("a memory written across a model swap") is True
            row = store.db.execute(
                "SELECT text, embedding FROM episodic_memories WHERE is_deleted = 0"
            ).fetchone()
            assert row is not None, "the text must still be written — nothing is lost"
            assert row["embedding"] is None, (
                "a vector from the previous space must not be committed"
            )
        finally:
            store.close()

    def test_episodic_stores_the_vector_when_no_swap_happens(self, tmp_path) -> None:
        """The guard must not cost the common case its embedding."""
        store = self._store(tmp_path)
        try:
            store.embed_fn = lambda text: [0.1, 0.2, 0.3, 0.4]
            assert store.write_episodic("an ordinary memory") is True
            row = store.db.execute(
                "SELECT embedding FROM episodic_memories WHERE is_deleted = 0"
            ).fetchone()
            assert row["embedding"] is not None
        finally:
            store.close()

    def test_lesson_leaves_the_vector_null_when_a_swap_lands_before_the_lock(
        self, tmp_path
    ) -> None:
        store = self._store(tmp_path)
        try:
            def _embed_then_swap(text: str) -> list[float]:
                vec = [0.5, 0.5, 0.5, 0.5]
                store.begin_space_change()
                return vec

            store.embed_fn = _embed_then_swap
            store.write_lesson("always carry the generation to the write")
            row = store.db.execute(
                "SELECT embedding FROM semantic_memory WHERE embedding IS NOT NULL"
            ).fetchone()
            assert row is None, "no lesson vector from the previous space may persist"
        finally:
            store.close()

    def test_lesson_stores_the_vector_when_no_swap_happens(self, tmp_path) -> None:
        store = self._store(tmp_path)
        try:
            store.embed_fn = lambda text: [0.5, 0.5, 0.5, 0.5]
            store.write_lesson("an ordinary lesson")
            row = store.db.execute(
                "SELECT embedding FROM semantic_memory WHERE embedding IS NOT NULL"
            ).fetchone()
            assert row is not None
        finally:
            store.close()

    def test_lazy_lesson_backfill_is_dropped_when_a_swap_lands_after_its_embed(
        self, tmp_path
    ) -> None:
        """write_lesson also flushes LAZY backfills of legacy lessons.

        Those embed inside the dedup scan, after the function captured its own
        generation, so each pending blob carries the generation it was embedded in
        and the flush skips any that a swap has since invalidated.
        """
        store = self._store(tmp_path)
        try:
            # A legacy lesson with no vector, so the dedup scan lazily backfills it.
            store.embed_fn = None
            store.write_lesson("a legacy lesson stored without a vector")
            assert (
                store.db.execute(
                    "SELECT 1 FROM semantic_memory WHERE embedding IS NOT NULL"
                ).fetchone()
                is None
            ), "precondition: the legacy lesson must start with no vector"

            # Now the lazy backfill embeds it, and a swap lands before the flush.
            def _embed_then_swap(text: str) -> list[float]:
                vec = [0.25, 0.25, 0.25, 0.25]
                store.begin_space_change()
                return vec

            store.embed_fn = _embed_then_swap
            store.write_lesson("an unrelated new lesson that triggers the scan")
            rows = store.db.execute(
                "SELECT COUNT(*) AS n FROM semantic_memory WHERE embedding IS NOT NULL"
            ).fetchone()
            assert rows["n"] == 0, (
                "a lazily backfilled vector from the previous space must not persist"
            )
        finally:
            store.close()

    def test_lazy_lesson_backfill_persists_when_no_swap_happens(self, tmp_path) -> None:
        """The guard must not break the ordinary lazy-backfill path."""
        store = self._store(tmp_path)
        try:
            store.embed_fn = None
            store.write_lesson("a legacy lesson stored without a vector")
            store.embed_fn = lambda text: [0.25, 0.25, 0.25, 0.25]
            store.write_lesson("an unrelated new lesson that triggers the scan")
            rows = store.db.execute(
                "SELECT COUNT(*) AS n FROM semantic_memory WHERE embedding IS NOT NULL"
            ).fetchone()
            assert rows["n"] >= 1, "the legacy lesson should have been backfilled"
        finally:
            store.close()

    def test_a_precomputed_vector_is_dropped_when_a_swap_follows_its_embed(
        self, tmp_path
    ) -> None:
        """A caller-supplied vector carries provenance the store cannot infer.

        The dashboard's lesson handler embeds once off the loop and reuses the
        vector, so `write_lesson` capturing the generation at ITS entry would
        compare the post-swap value against itself. The caller passes the
        generation it read BEFORE embedding instead.
        """
        store = self._store(tmp_path)
        try:
            store.embed_fn = lambda text: [0.75, 0.75, 0.75, 0.75]
            # Caller-side: read the generation, embed, THEN a swap lands.
            gen_before = store.space_generation
            precomputed = store.embed_lesson("a lesson embedded by the caller")
            assert precomputed, "precondition: the caller got a vector"
            store.begin_space_change()

            store.write_lesson(
                "a lesson embedded by the caller",
                "knowledge",
                None,
                "user_explicit",
                precomputed,
                gen_before,
            )
            row = store.db.execute(
                "SELECT embedding FROM semantic_memory WHERE embedding IS NOT NULL"
            ).fetchone()
            assert row is None, (
                "a caller-supplied vector from the previous space must not persist"
            )
        finally:
            store.close()

    def test_a_precomputed_vector_persists_when_no_swap_follows(self, tmp_path) -> None:
        store = self._store(tmp_path)
        try:
            store.embed_fn = lambda text: [0.75, 0.75, 0.75, 0.75]
            gen_before = store.space_generation
            precomputed = store.embed_lesson("an ordinary caller-embedded lesson")
            store.write_lesson(
                "an ordinary caller-embedded lesson",
                "knowledge",
                None,
                "user_explicit",
                precomputed,
                gen_before,
            )
            row = store.db.execute(
                "SELECT embedding FROM semantic_memory WHERE embedding IS NOT NULL"
            ).fetchone()
            assert row is not None
        finally:
            store.close()

    def test_lazy_backfill_generation_is_sampled_before_the_embed(self) -> None:
        """Sampling AFTER _try_embed returns would tag an old blob as current.

        _try_embed returns None when a swap spans its own call, so the only value
        that identifies the blob's space is the one read BEFORE the embed. This
        asserts the source order, since the post-return gap is a few instructions
        wide and cannot be driven deterministically from a test.
        """
        import inspect

        src = inspect.getsource(VectorMemoryStore.write_lesson)
        sample = src.find("backfill_generation = self._space_generation")
        embed = src.find("existing_emb = self._try_embed(")
        assert -1 not in (sample, embed), "lazy-backfill sampling not found"
        assert sample < embed, (
            "the generation must be sampled BEFORE the embed, or a swap in the "
            "post-return gap tags a previous-space blob with the new generation"
        )

    def test_both_persisting_paths_recheck_under_the_lock(self) -> None:
        """Guard against a refactor dropping the re-check from either path."""
        import inspect

        for fn in (VectorMemoryStore.write_episodic, VectorMemoryStore.write_lesson):
            src = inspect.getsource(fn)
            assert "_space_generation" in src, f"{fn.__name__} lost its generation check"
            lock_at = src.find("with self._db_lock:")
            check_at = src.find("self._space_generation !=")
            assert -1 not in (lock_at, check_at), fn.__name__
            assert check_at > lock_at, (
                f"{fn.__name__} must re-check INSIDE the lock, not before it"
            )


class TestRevertToBundledIsGatedToo:
    """Reverting must prove the bundled model loads BEFORE persisting the revert.

    The bundled GGUF is a download and can be absent; persisting the revert first
    would discard a working custom configuration with nothing to fall back to.
    Gating it also closes the width window: a bundled-width query vector could
    otherwise reach a FAISS index still built at the custom width.
    """

    def test_bundled_candidate_is_gated_with_a_known_width(self) -> None:
        from kiro_crew.embeddings import build_gated_bundled, bundled_embedding_dim

        cand = build_gated_bundled()
        assert cand._serving is False
        assert cand.dim == bundled_embedding_dim() > 0

    def test_bundled_candidate_targets_the_BUNDLED_file_not_the_custom_one(
        self, tmp_path, monkeypatch
    ) -> None:
        """The constructor defaults model_path to active_model_path().

        active_model_path() resolves the STILL-CONFIGURED custom path, so leaving
        model_path implicit would load the CUSTOM model while labelling it with the
        bundled model_id — stamping custom vectors as bundled and keeping them
        across restarts. Every argument must be explicit.
        """
        import kiro_crew.embeddings as emb

        custom = _write_model(tmp_path / "custom.gguf")
        monkeypatch.setattr(
            emb, "_read_memory_config",
            lambda: {"embed_model_path": str(custom), "embedding_dim": 1024},
        )
        # Precondition: the implicit default really would resolve the custom path.
        assert emb.active_model_path() == custom

        cand = emb.build_gated_bundled()
        assert cand.model_path == emb.default_model_path(), (
            "reverting to bundled must load the bundled file, not the custom one"
        )
        assert cand.model_path != custom
        assert cand.model_id != emb._custom_model_id(custom, ""), (
            "and must not carry the custom space identity"
        )

    def test_both_branches_install_a_gated_candidate(self) -> None:
        import inspect

        from kiro_crew.dashboard.handlers import memory as mem

        src = inspect.getsource(mem._apply_embedding_model)
        assert "build_gated_candidate(candidate)" in src
        assert "build_gated_bundled()" in src

    def test_config_is_never_written_before_the_load_proves_out(self) -> None:
        """Neither branch may persist ahead of readiness (bundled GGUF can be absent)."""
        import inspect

        from kiro_crew.dashboard.handlers import memory as mem

        src = inspect.getsource(mem._apply_embedding_model)
        first_write = src.find("_write_embed_model_config(")
        wait_at = src.find("wait_ready(")
        assert -1 not in (first_write, wait_at)
        assert wait_at < first_write, (
            "persisting before the model loads can strand an unloadable config"
        )


class TestReconcileOutcomeIsChecked:
    """A reconcile that could not remove the stale index must fail the apply.

    ``reconcile_embedding_space`` deliberately does NOT stamp the signature when it
    cannot unlink the stale FAISS pair (read-only memory dir; Windows while the
    index is mapped). Ignoring that return would report "Re-embedding complete"
    for a store that was never reconciled, and the next start's
    ``load_faiss_index()`` prefers the surviving OLD-space pair.
    """

    def test_apply_compares_recorded_space_before_activating(self) -> None:
        import inspect

        from kiro_crew.dashboard.handlers import memory as mem

        src = inspect.getsource(mem._apply_embedding_model)
        check_at = src.find("recorded_embedding_space()")
        activate_at = src.find("activate_shared_embedder()")
        write_at = src.find("_write_embed_model_config(raw, embedder.dim)")
        assert -1 not in (check_at, activate_at, write_at)
        assert check_at < activate_at, "must not activate against an unreconciled store"
        assert check_at < write_at, "must not persist against an unreconciled store"

    def test_an_unstamped_store_cannot_match_the_active_space(self, tmp_path) -> None:
        """Pin the store-side observable the apply now relies on.

        When reconcile leaves the signature unstamped, `recorded_embedding_space()`
        does not equal the active signature — which is exactly the comparison the
        apply uses to refuse activation.
        """
        store = VectorMemoryStore(db_path=tmp_path / "m.db", embedding_dim=4)
        store.init()
        try:
            recorded = store.recorded_embedding_space()
            assert recorded != embeddings_mod.active_embedding_space_signature(), (
                "an unstamped store must not compare equal to the active space"
            )
        finally:
            store.close()


class TestApplyOrdering:
    """The apply's step order is the safety property; pin it against drift."""

    def _src(self) -> str:
        import inspect

        from kiro_crew.dashboard.handlers import memory as mem

        return inspect.getsource(mem._apply_embedding_model)

    def test_install_precedes_the_load_wait(self) -> None:
        """An empty slot lets a ~2s status poll rebuild the outgoing model."""
        src = self._src()
        assert src.find("install_shared_embedder(") < src.find("wait_ready(")

    def test_activation_comes_after_config_write_and_reconcile(self) -> None:
        src = self._src()
        write_at = src.find("_write_embed_model_config(raw, embedder.dim)")
        reconcile_at = src.find("reconcile_store_embedding_space(store)")
        activate_at = src.find("activate_shared_embedder()")
        assert -1 not in (write_at, reconcile_at, activate_at)
        assert write_at < activate_at, "activating before persistence exposes the new space"
        assert reconcile_at < activate_at, "activating before reconcile mixes spaces"

    def test_config_is_written_after_reconcile_so_failure_rolls_back(self) -> None:
        """Config must name the PREVIOUS model while reconcile can still fail.

        Written earlier, a reconcile failure would leave config naming the new
        model — and the rollback would then rebuild THAT model, ungated, against a
        store that was never reconciled. Reconcile reads the live backend rather
        than config, so deferring the write costs nothing.
        """
        src = self._src()
        reconcile_at = src.find("reconcile_store_embedding_space(store)")
        write_at = src.find("_write_embed_model_config(raw, embedder.dim)")
        assert -1 not in (reconcile_at, write_at)
        assert reconcile_at < write_at

    def test_backfill_runs_after_activation(self) -> None:
        src = self._src()
        assert src.find("activate_shared_embedder()") < src.find("backfill_missing_embeddings")

    def test_every_pre_activation_failure_rolls_back(self) -> None:
        """A gated candidate left installed would serve nobody forever."""
        src = self._src()
        assert src.count("reset_shared_embedder()") >= 3, (
            "load failure, config-write failure and the unexpected-exception path "
            "must each drop the candidate"
        )

    def test_load_wait_is_bounded(self) -> None:
        """Unbounded, a wedged native load pins progress at `applying` forever."""
        src = self._src()
        assert "_MODEL_LOAD_TIMEOUT_SECS" in src

    def test_reembed_runs_on_the_embed_pool_not_the_maintenance_pool(self) -> None:
        """maintenance_executor is 4 workers reserved for the fast sweeps."""
        import inspect

        from kiro_crew.dashboard.handlers import memory as mem

        src = inspect.getsource(mem.api_memory_embedding_model)
        assert "embed_executor()" in src
        assert "maintenance_executor()" not in src


class TestConfigWriteHasNoOrphanWindow:
    """The apply must never report failure while a config write is still pending.

    A `fut.result(timeout=N)` does NOT cancel the coroutine it is waiting on, so
    a slow config lock would let the worker mark progress failed and return while
    the write later lands — persisting the new path/dim WITHOUT the embedder reset
    that belongs with it. Runtime and config diverge, and the next restart
    activates the model the user was told had failed.
    """

    def test_no_timeout_is_used_when_waiting_for_the_config_write(self) -> None:
        """A bounded wait here is the bug, so assert the unbounded form is kept."""
        import inspect

        from kiro_crew.dashboard.handlers import memory as mem

        src = inspect.getsource(mem._apply_embedding_model)
        assert "fut.result()" in src, "the config write must be awaited unbounded"
        assert "timeout=" not in src.split("fut.result")[0].split("run_coroutine_threadsafe")[-1], (
            "a timeout on the config-write future reintroduces the orphan-write window"
        )

    @pytest.mark.asyncio
    async def test_a_slow_lock_still_produces_a_consistent_pair(
        self, tmp_path, monkeypatch
    ) -> None:
        """Even when the write is delayed, path and dim land together or not at all."""
        from kiro_crew.dashboard.handlers.memory import _write_embed_model_config

        cfg = tmp_path / "config.json"
        cfg.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.memory.config_path", lambda: cfg, raising=False
        )
        await _write_embed_model_config("/models/bge.gguf", 1024)
        data = json.loads(cfg.read_text(encoding="utf-8"))
        mem_cfg = data["memory"]
        # Never a path without its width — that pair is what the loader validates.
        assert mem_cfg["embed_model_path"] == "/models/bge.gguf"
        assert mem_cfg["embedding_dim"] == 1024


class TestStaleModelIdIsCleared:
    """A pinned ``embed_model_id`` must never survive a model change.

    ``_custom_model_id`` gives an explicit ``memory.embed_model_id`` precedence
    over the derived name+size identity, so it is pinned to the model the
    operator set it for. Keeping it across a swap holds the OLD vector-space
    signature: a change to a different model of the SAME dimension reconciles as
    "space unchanged", so vectors built by the previous model are retained and
    compared against new-model vectors — silent semantic corruption.
    """

    @pytest.mark.asyncio
    async def test_changing_the_path_drops_a_pinned_model_id(
        self, tmp_path, monkeypatch
    ) -> None:
        from kiro_crew.dashboard.handlers.memory import _write_embed_model_config

        cfg = tmp_path / "config.json"
        cfg.write_text(
            json.dumps({"memory": {
                "embed_model_path": "/models/old.gguf",
                "embed_model_id": "pinned-to-old-model",
                "embedding_dim": 1024,
            }}),
            encoding="utf-8",
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.memory.config_path", lambda: cfg, raising=False
        )
        await _write_embed_model_config("/models/new-same-dim.gguf", 1024)
        mem_cfg = json.loads(cfg.read_text(encoding="utf-8"))["memory"]
        assert mem_cfg["embed_model_path"] == "/models/new-same-dim.gguf"
        assert "embed_model_id" not in mem_cfg, (
            "a pinned id would keep the old space signature and retain stale vectors"
        )

    @pytest.mark.asyncio
    async def test_reverting_to_bundled_also_drops_the_pinned_id(
        self, tmp_path, monkeypatch
    ) -> None:
        from kiro_crew.dashboard.handlers.memory import _write_embed_model_config

        cfg = tmp_path / "config.json"
        cfg.write_text(
            json.dumps({"memory": {
                "embed_model_path": "/models/old.gguf",
                "embed_model_id": "pinned-to-old-model",
            }}),
            encoding="utf-8",
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.memory.config_path", lambda: cfg, raising=False
        )
        await _write_embed_model_config("", 1024)
        mem_cfg = json.loads(cfg.read_text(encoding="utf-8"))["memory"]
        assert "embed_model_path" not in mem_cfg
        assert "embed_model_id" not in mem_cfg


class TestPathIsValidatedAtPointOfUse:
    """The sensitive-path gate must be enforced where the file is opened.

    The worker runs a thread hop away from the request handler, so a path
    validated only at the boundary and re-derived in the worker leaves the gate
    and the actual native-library file access in different scopes.
    """

    def test_worker_revalidates_before_loading(self) -> None:
        import inspect

        from kiro_crew.dashboard.handlers import memory as mem

        src = inspect.getsource(mem._apply_embedding_model)
        assert "validate_custom_model_path(" in src, (
            "the worker must re-apply the sensitive-path gate before opening the file"
        )
        # The backend must be built from the VALIDATED path, never from a
        # re-derived raw string.
        assert "build_gated_candidate(candidate)" in src
        assert "build_gated_candidate(Path(raw)" not in src


class TestValidateCustomModelPath:
    """The dashboard validates before persisting, so a typo never reaches config.

    Each failure returns a stable ``code`` as well as prose: the dashboard renders
    a LOCALIZED message keyed on the code, so prose alone would be untranslatable.
    """

    def test_accepts_a_real_model(self, tmp_path: Path) -> None:
        path, error, code = validate_custom_model_path(str(_write_model(tmp_path / "m.gguf")))
        assert error == ""
        assert code == ""
        assert path == tmp_path / "m.gguf"

    def test_rejects_relative(self) -> None:
        _, error, code = validate_custom_model_path("models/m.gguf")
        assert "absolute path" in error
        assert code == "model_path_not_absolute"

    def test_rejects_missing(self, tmp_path: Path) -> None:
        _, error, code = validate_custom_model_path(str(tmp_path / "nope.gguf"))
        assert "does not exist" in error
        assert code == "model_path_not_found"

    def test_rejects_directory(self, tmp_path: Path) -> None:
        d = tmp_path / "adir"
        d.mkdir()
        _, error, code = validate_custom_model_path(str(d))
        assert "not a regular file" in error
        assert code == "model_path_not_a_file"

    def test_rejects_truncated(self, tmp_path: Path) -> None:
        stub = tmp_path / "stub.gguf"
        stub.write_bytes(b"tiny")
        _, error, code = validate_custom_model_path(str(stub))
        assert "too small" in error
        assert code == "model_path_too_small"

    def test_rejects_protected_location(self, tmp_path: Path, monkeypatch) -> None:
        secret = _write_model(tmp_path / "credentials")
        monkeypatch.setattr(
            "kiro_crew.embeddings.is_sensitive_path", lambda p, base_dir=None: True
        )
        _, error, code = validate_custom_model_path(str(secret))
        assert "protected location" in error
        assert code == "model_path_protected"

    def test_every_failure_carries_a_code(self, tmp_path: Path) -> None:
        """No failure may return prose without a code — that is the repo contract."""
        cases = ["relative/x.gguf", str(tmp_path / "missing.gguf"), str(tmp_path)]
        for raw in cases:
            _, error, code = validate_custom_model_path(raw)
            assert error, f"expected a failure for {raw!r}"
            assert code, f"failure for {raw!r} returned prose with no code"

    def test_error_names_the_origin_for_a_readable_message(self, tmp_path: Path) -> None:
        """The prose is advisory but still shown, so it must not say 'env var'."""
        _, error, _ = validate_custom_model_path(str(tmp_path / "nope.gguf"), "The model path")
        assert error.startswith("The model path")


class TestReembedProgress:
    def test_starts_idle(self) -> None:
        p = ReembedProgress()
        assert p.snapshot() == {"step": "idle", "done": 0, "total": 0, "error": ""}
        assert p.is_active() is False

    def test_apply_then_run_then_done(self) -> None:
        p = ReembedProgress()
        p.begin_apply()
        assert p.snapshot()["step"] == "applying"
        assert p.is_active() is True, "an apply in flight must block a second apply"
        p.begin_run(10)
        p.advance(4, 10)
        snap = p.snapshot()
        assert (snap["step"], snap["done"], snap["total"]) == ("running", 4, 10)
        p.finish(10)
        assert p.snapshot()["step"] == "done"
        assert p.is_active() is False

    def test_applying_is_distinguishable_from_running(self) -> None:
        """The indicator must not show 0/0 as if it were 0 % of a known total."""
        p = ReembedProgress()
        p.begin_apply()
        assert p.snapshot()["total"] == 0, "no denominator is known while loading"
        p.begin_run(7)
        assert p.snapshot()["total"] == 7

    def test_failure_retains_counts_and_surfaces_the_reason(self) -> None:
        p = ReembedProgress()
        p.begin_run(9)
        p.advance(3, 9)
        p.fail("model did not load")
        snap = p.snapshot()
        assert snap["step"] == "failed"
        assert snap["done"] == 3, "keep the counts so the UI can say where it stopped"
        assert snap["error"] == "model did not load"
        assert p.is_active() is False

    def test_finish_never_reports_more_done_than_total(self) -> None:
        p = ReembedProgress()
        p.begin_run(2)
        p.finish(5)
        snap = p.snapshot()
        assert snap["total"] >= snap["done"], "a >100% bar is a visible bug"

    def test_singleton_is_shared(self) -> None:
        assert reembed_progress() is reembed_progress()


def _store(tmp_path: Path, dim: int = 8) -> VectorMemoryStore:
    s = VectorMemoryStore(db_path=tmp_path / "mem.db", embedding_dim=dim)
    s.init()
    return s


def _basis_embed(dim: int = 8):
    """One stable vector per text, orthogonal across texts.

    A text-independent constant makes every write after the first score as a
    duplicate against the indexed vector, so a loop seeding N memories leaves
    one row behind and the progress denominator below reads 1 instead of N.
    """
    slots: dict[str, int] = {}

    def embed(text: str) -> list[float]:
        slot = slots.setdefault(text, len(slots) % dim)
        return [1.0 if i == slot else 0.0 for i in range(dim)]

    return embed


class TestBackfillProgressReporting:
    """Without a denominator the indicator can only spin."""

    def test_reports_total_up_front(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.embed_fn = _basis_embed()
        for i in range(3):
            store.write_episodic(f"memory number {i} long enough to be stored here")
        store.db.execute("UPDATE episodic_memories SET embedding = NULL")
        store.db.commit()

        seen: list = []
        store.backfill_missing_embeddings(progress=lambda d, t: seen.append((d, t)))

        assert seen, "progress must be reported at least once"
        assert seen[0] == (0, 3), "the denominator is known before any work"
        assert seen[-1] == (3, 3)

    def test_progress_is_monotonic(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.embed_fn = _basis_embed()
        for i in range(4):
            store.write_episodic(f"memory number {i} long enough to be stored here")
        store.db.execute("UPDATE episodic_memories SET embedding = NULL")
        store.db.commit()

        seen: list = []
        store.backfill_missing_embeddings(progress=lambda d, t: seen.append(d))
        assert seen == sorted(seen), "a bar that goes backwards is a visible bug"

    def test_no_progress_callback_still_works(self, tmp_path: Path) -> None:
        """The gateway boot sweep calls this with no callback."""
        store = _store(tmp_path)
        store.embed_fn = lambda t: [0.5] * 8
        store.write_episodic("a memory long enough to survive the length check")
        store.db.execute("UPDATE episodic_memories SET embedding = NULL")
        store.db.commit()
        assert store.backfill_missing_embeddings() == 1


class TestSetEmbeddingDim:
    """The dim-swap gap: _embedding_dim gates the index width AND the shape check."""

    def test_changing_dim_reports_true_and_drops_the_index(self, tmp_path: Path) -> None:
        store = _store(tmp_path, dim=1024)
        store._faiss_id_map = ["stale"]
        assert store.set_embedding_dim(768) is True
        assert store._embedding_dim == 768
        assert store._faiss_index is None
        assert store._faiss_id_map == [], "an index at the old width must not survive"

    def test_same_dim_is_a_noop(self, tmp_path: Path) -> None:
        store = _store(tmp_path, dim=1024)
        assert store.set_embedding_dim(1024) is False

    def test_nonsense_dim_is_refused(self, tmp_path: Path) -> None:
        store = _store(tmp_path, dim=1024)
        assert store.set_embedding_dim(0) is False
        assert store.set_embedding_dim(-5) is False
        assert store._embedding_dim == 1024

    def test_backfill_accepts_the_new_width_after_a_swap(self, tmp_path: Path) -> None:
        """The regression this exists to prevent: vectors stuck NULL forever.

        Before `set_embedding_dim`, a swap to a narrower model left every
        re-embedded vector failing the shape check, so nothing was ever stored.
        """
        store = _store(tmp_path, dim=8)
        store.embed_fn = lambda t: [0.5] * 8
        store.write_episodic("a memory long enough to survive the length check")
        store.db.execute("UPDATE episodic_memories SET embedding = NULL")
        store.db.commit()

        # New model produces 4-d vectors.
        store.set_embedding_dim(4)
        store.embed_fn = lambda t: [0.25] * 4
        assert store.backfill_missing_embeddings() == 1

        row = store.db.execute(
            "SELECT embedding FROM episodic_memories WHERE is_deleted = 0"
        ).fetchone()
        assert row["embedding"] is not None
        assert len(row["embedding"]) // 4 == 4, "stored at the NEW width"

    def test_without_the_dim_update_the_swap_would_store_nothing(self, tmp_path: Path) -> None:
        """Pins the failure mode so a revert of set_embedding_dim is caught."""
        store = _store(tmp_path, dim=8)
        store.embed_fn = lambda t: [0.5] * 8
        store.write_episodic("a memory long enough to survive the length check")
        store.db.execute("UPDATE episodic_memories SET embedding = NULL")
        store.db.commit()

        store.embed_fn = lambda t: [0.25] * 4  # narrower model, dim NOT updated
        assert store.backfill_missing_embeddings() == 0
        row = store.db.execute(
            "SELECT embedding FROM episodic_memories WHERE is_deleted = 0"
        ).fetchone()
        assert row["embedding"] is None


class TestConfigWrite:
    """A malformed config must not be clobbered by a model-path write."""

    @pytest.mark.asyncio
    async def test_writes_path_into_the_memory_section(self, tmp_path: Path, monkeypatch) -> None:
        from kiro_crew.dashboard.handlers.memory import _write_embed_model_config

        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({"memory": {"episodic_max_results": 8}}), encoding="utf-8")
        monkeypatch.setattr("kiro_crew.dashboard.handlers.memory.config_path", lambda: cfg)

        await _write_embed_model_config("/models/m.gguf", 0)
        data = json.loads(cfg.read_text(encoding="utf-8"))
        assert data["memory"]["embed_model_path"] == "/models/m.gguf"
        assert data["memory"]["episodic_max_results"] == 8, "other keys preserved"

    @pytest.mark.asyncio
    async def test_empty_path_reverts_to_the_bundled_model(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        from kiro_crew.dashboard.handlers.memory import _write_embed_model_config

        cfg = tmp_path / "config.json"
        cfg.write_text(
            json.dumps({"memory": {"embed_model_path": "/models/old.gguf"}}), encoding="utf-8"
        )
        monkeypatch.setattr("kiro_crew.dashboard.handlers.memory.config_path", lambda: cfg)

        await _write_embed_model_config("", 0)
        data = json.loads(cfg.read_text(encoding="utf-8"))
        assert "embed_model_path" not in data["memory"]

    @pytest.mark.asyncio
    async def test_unparseable_config_is_not_clobbered(self, tmp_path: Path, monkeypatch) -> None:
        from kiro_crew.dashboard.handlers.memory import _write_embed_model_config

        cfg = tmp_path / "config.json"
        cfg.write_text("{ this is not json", encoding="utf-8")
        monkeypatch.setattr("kiro_crew.dashboard.handlers.memory.config_path", lambda: cfg)

        with pytest.raises(ValueError):
            await _write_embed_model_config("/models/m.gguf", 0)
        assert cfg.read_text(encoding="utf-8") == "{ this is not json"


class TestStatusEndpointCarriesProgress:
    def test_snapshot_shape_matches_what_the_card_reads(self) -> None:
        """The frontend destructures these four keys; renaming one breaks the bar."""
        assert set(reembed_progress().snapshot()) == {"step", "done", "total", "error"}

    def test_embeddings_module_exposes_the_singleton_accessor(self) -> None:
        assert callable(embeddings_mod.reembed_progress)
