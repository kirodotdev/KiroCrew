"""A committed sub-slice receipt must stay replayable when a later orphan
transcript migration drops a display-only ``notice`` at the head.

The over-budget head path commits its V2 receipt before the durable marker
advances. If the gateway crashes in that window, the next startup can run a
one-time orphan-transcript migration that merges an older display-only ``notice``
in at the consolidation marker. The merge does not bump the rotation generation
or move the marker (the rendered head is unchanged), so the replay recomputes the
SAME ``source_id`` and finds the committed receipt -- then it must recognise it.

The receipt's digest covers exactly what the model read, which is the PROMPTED
rows (``_prompt_rows`` drops display-only rows). Hashing the raw ``chunk`` instead
folds the ``notice`` into the fingerprint, so the inserted notice makes the replay
digest diverge, ``_consolidate`` raises ``ValueError`` on every pass, and the head
and the tail behind it never consolidate again. These tests pin that an inserted
head notice is invisible to the receipt digest, so a committed sub-slice replays
cleanly across the migration.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew import history as history_mod
from kiro_crew import memory_stores
from kiro_crew.history import (
    _CONSOLIDATION_PROMPT_BUDGET_CHARS,
    ConversationLog,
)
from kiro_crew.vector_memory import consolidation_source_digest

KEY = "dashboard:chat-subslice-notice"


class _RecordingStore:
    """A faithful stand-in for the V2 member store's consolidation receipts.

    Records receipts by ``source_id`` the way ``apply_consolidation`` does -- a
    ``source_count``/``source_digest`` over the exact ``messages`` it was handed,
    plus the ``source_total`` -- and returns them on replay, so the replay digest
    comparison in ``_consolidate`` runs against a genuine committed fingerprint.
    Every other store method the structured-memory writes touch is a permissive
    auto-mock, as in the sibling receipt tests.
    """

    def __new__(cls):
        store = MagicMock()
        store.algorithm_version = "v2"
        receipts: dict[str, dict] = {}
        store.receipts = receipts

        def _receipt(source_id):  # noqa: ANN001
            return receipts.get(source_id)

        def _apply(
            *, source_id, session_key, source_total, result, snapshot, messages, facets
        ):  # noqa: ANN001
            receipts[source_id] = {
                "source_total": source_total,
                "source_count": len(messages),
                "source_digest": consolidation_source_digest(messages),
            }
            return {}

        store.consolidation_receipt = MagicMock(side_effect=_receipt)
        store.apply_consolidation = MagicMock(side_effect=_apply)
        return store


def _log_with_head(tmp_path, head: str) -> ConversationLog:
    log = ConversationLog(base_dir=tmp_path / "sessions")
    log.init()
    with history_mod.allow_on_loop_persist():
        log.append(KEY, "user", head)
    return log


def _inject_head_notice(log: ConversationLog, text: str) -> None:
    """Drop a display-only ``notice`` line in just before the head message, the
    way a one-time orphan-transcript migration merges an older notice at the
    consolidation marker -- without touching the metadata line (generation and
    the ``last_consolidated`` marker stay put, as they do when the rendered head
    is unchanged).
    """
    path = log._path(KEY)
    lines = path.read_text().splitlines()
    meta_line, body = lines[0], lines[1:]
    notice = json.dumps({"role": "notice", "content": text, "ts": "2026-09-09T00:00:00+00:00"})
    path.write_text("\n".join([meta_line, notice, *body]) + "\n")
    log._invalidate_cache(KEY)


def _make_consolidator(log, store):
    from kiro_crew.context import ContextBuilder

    memory = _dummy_memory()
    monkey = pytest.MonkeyPatch()
    monkey.setattr("kiro_crew.context.store_of_session", lambda *_: "alice-store")
    monkey.setattr(memory_stores, "memory_store_version", lambda *_: 2)
    monkey.setattr(ContextBuilder, "ensure_store", AsyncMock(return_value=store))
    monkey.setattr(ContextBuilder, "get_memory_for", lambda *a, **k: memory)
    c = history_mod.HistoryConsolidator(
        log=log,
        memory=memory,
        migrated=True,
        history_idle_secs=0,
        sessions=None,
        vector_store=store,
    )
    return c, monkey


def _dummy_memory():
    from unittest.mock import MagicMock

    memory = MagicMock()
    memory.read_preferences.return_value = ""
    memory.read_projects.return_value = ""
    return memory


class TestASubSliceReceiptSurvivesAHeadNoticeMigration:
    @pytest.mark.asyncio
    async def test_a_committed_sub_slice_replays_after_a_notice_lands_at_the_head(
        self, tmp_path
    ) -> None:
        """Commit a sub-slice receipt, then migrate a notice in at the head and
        replay. The replay must recognise the receipt and re-apply the durable
        advance instead of raising ``ValueError`` forever.
        """
        # A head three budgets wide so the first pass is a NON-FINAL sub-slice:
        # the message marker does not move and the durable sub-offset advances,
        # which is exactly the mid-slice window the migration can land in.
        log = _log_with_head(tmp_path, "y" * (_CONSOLIDATION_PROMPT_BUDGET_CHARS * 3))
        store = _RecordingStore()
        c, monkey = _make_consolidator(log, store)
        try:
            # Pass 1: commit a sub-slice receipt and advance the durable
            # sub-offset without moving the message marker.
            with patch.object(c, "_call_llm", AsyncMock(return_value={"history_entry": "e"})):
                await c._consolidate(KEY, include_history=True)

            meta = log.get_metadata(KEY)
            assert meta.get("last_consolidated", 0) == 0, "mid-slice: marker must not have moved"
            assert int(meta.get("consolidation_sub_offset", 0)) > 0, "a durable sub-offset remains"
            assert store.receipts, "the first slice committed a V2 receipt"

            # The crash is in the window AFTER the receipt commits but BEFORE the
            # durable sub-offset advance is made durable: reset the sub-offset to
            # the pre-advance snapshot value (0) so the next pass recomputes the
            # SAME source_id and finds the committed receipt -- the replay path.
            with history_mod.allow_on_loop_persist():
                log.update_metadata(
                    KEY,
                    {"consolidation_sub_offset": 0, "consolidation_retry_at": 0.0},
                )

            # A one-time orphan-transcript migration drops an older display-only
            # notice in at the marker. Generation and marker are unchanged.
            _inject_head_notice(log, "AutoNudge: earlier reminder")

            # Pass 2 is the replay. The digest the receipt stored covers the
            # PROMPTED rows, so the inserted display-only notice must be
            # invisible to it: _consolidate must not raise ``Committed
            # consolidation source changed`` and wedge every future pass. Drive
            # passes to completion -- none may raise, and the head must drain
            # rather than stall in environment-failure backoff forever.
            with patch.object(c, "_call_llm", AsyncMock(return_value={"history_entry": "e"})):
                passes = 0
                while log.unconsolidated_count(KEY) and passes < 10:
                    with history_mod.allow_on_loop_persist():
                        log.update_metadata(KEY, {"consolidation_retry_at": 0.0})
                    await c._consolidate(KEY, include_history=True)
                    passes += 1

            assert (
                log.unconsolidated_count(KEY) == 0
            ), "the head drains after the notice migration instead of wedging in backoff"
        finally:
            monkey.undo()

    @pytest.mark.asyncio
    async def test_a_final_slice_receipt_replays_after_a_head_notice(self, tmp_path) -> None:
        """The final slice of a drained head commits a receipt whose durable
        advance is the message marker. A notice merged in at the head before that
        advance is retried must not wedge the replay either.
        """
        # A head a little over one budget: it drains in two slices, and the
        # SECOND is final (advances the message marker).
        log = _log_with_head(
            tmp_path,
            "y" * (_CONSOLIDATION_PROMPT_BUDGET_CHARS + _CONSOLIDATION_PROMPT_BUDGET_CHARS // 2),
        )
        store = _RecordingStore()
        c, monkey = _make_consolidator(log, store)
        try:
            # Drain to completion once so the final-slice receipt is committed.
            passes = 0
            with patch.object(c, "_call_llm", AsyncMock(return_value={"history_entry": "e"})):
                while log.unconsolidated_count(KEY) and passes < 8:
                    with history_mod.allow_on_loop_persist():
                        log.update_metadata(KEY, {"consolidation_retry_at": 0.0})
                    await c._consolidate(KEY, include_history=True)
                    passes += 1
            assert log.unconsolidated_count(KEY) == 0, "the head drained"
            assert log.get_metadata(KEY).get("last_consolidated") == 1

            # Reset the marker/sub-offset to just before the final slice's
            # advance, as a crash in the commit->advance window would leave it,
            # then migrate a notice in and replay. (The committed final-slice
            # receipt is still in the store.)
            final_sub = store  # receipts kept
            with history_mod.allow_on_loop_persist():
                log.update_metadata(
                    KEY,
                    {"last_consolidated": 0, "consolidation_retry_at": 0.0},
                )
            _inject_head_notice(log, "AutoNudge: earlier reminder")

            with patch.object(c, "_call_llm", AsyncMock(return_value={"history_entry": "e"})):
                # Drive passes to completion; none may raise, and the head must
                # end consolidated.
                passes = 0
                while log.unconsolidated_count(KEY) and passes < 8:
                    with history_mod.allow_on_loop_persist():
                        log.update_metadata(KEY, {"consolidation_retry_at": 0.0})
                    await c._consolidate(KEY, include_history=True)
                    passes += 1
            assert log.unconsolidated_count(KEY) == 0, "the head re-drains without wedging"
            assert final_sub.receipts, "receipts were used"
        finally:
            monkey.undo()
