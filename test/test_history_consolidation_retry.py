"""Persistent retry accounting for history consolidation.

A consolidation pass spends a billed LLM turn before it can write the durable
``last_consolidated`` marker, so a failure in between leaves the span
unconsolidated. Without durable accounting every entry point then re-spends that
turn indefinitely — the 60s idle sweep on every tick, session-expiry sweeps on
every expiry, and every gateway restart (the in-memory throttle is memory-only).
These tests pin the attempt counter, the exponential backoff, the abandon cap,
and that no entry point bypasses them.

The failure accounting (attempts, backoff, the cap, restarts, hostile metadata) is in
``test_history_consolidation_retry_accounting.py``, and the cases tying the cap to its span
(rotations, foreign writers, edited and grown transcripts) are in
``test_history_consolidation_retry_span.py``; the helpers they share are in
``history_consolidation_retry_helpers.py``.
"""

import asyncio
import contextlib
import json
import threading
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from history_consolidation_retry_helpers import (
    KEY,
    _FakeRequest,
    _make_consolidator,
    _seed_log,
    _total,
)

from kiro_crew import history as history_mod
from kiro_crew.history import ConversationLog
from kiro_crew.testing.wait import await_until, finished


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["persistent", "incognito", "temporary"])
async def test_consolidation_captures_execution_off_loop(tmp_path, monkeypatch, mode):
    from kiro_crew import execution_context
    from kiro_crew.history_consolidation import _CONSOLIDATION_BUSY, _CONSOLIDATION_REFUSED

    captured = execution_context.ExecutionContext(
        None, execution_context.MemoryStoreRef("default"), "template", "kirocrew", mode
    )
    loop_thread = threading.get_ident()
    reads = []

    def read(key):
        reads.append((threading.get_ident(), key))
        return captured

    monkeypatch.setattr(execution_context, "read_session_execution", read)
    consolidator = _make_consolidator(_seed_log(tmp_path, count=0))
    consolidator._call_llm = AsyncMock()
    result = await finished(consolidator._consolidate(KEY), "consolidation")
    # count=0 leaves no transcript on disk, so the lease (which requires an
    # existing session) is refused before any snapshot.
    assert result is (_CONSOLIDATION_BUSY if mode == "persistent" else _CONSOLIDATION_REFUSED)
    consolidator._call_llm.assert_not_awaited()
    assert len(reads) == 1
    assert reads[0][0] != loop_thread
    assert reads[0][1] == KEY


class TestTheLineIsValidatedWithTheRows:
    """The consolidator's privacy check reads the line, then snapshots rows.

    A writer can tighten the line in between -- a same-key hand-over landing a
    restricted tab's rows under a line that was persistent a moment ago. The
    snapshot therefore re-reads the line under the SAME lock as the rows and
    refuses them together, at both the pre-turn snapshot and the write-boundary
    one, so no rows a restricted line governs reach the model or memory.
    """

    @staticmethod
    def _tighten_before(log, method_name, monkeypatch, *, mode="incognito"):
        """Tighten KEY's line the first time the seam takes its lock set on ``log``.

        The gated snapshot acquires through ``locked_stems`` (the seam's hold), so
        that is the seam the tightening writer is modelled as beating to the lock.
        """
        real = type(log).locked_stems
        fired = {"done": False}

        def _tighten_then_call(self, stems):
            if not fired["done"]:
                fired["done"] = True
                with history_mod.allow_on_loop_persist():
                    self.update_metadata(KEY, {"memory_mode": mode})
            return real(self, stems)

        monkeypatch.setattr(type(log), "locked_stems", _tighten_then_call)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", ["incognito", "temporary", "Incognito"])
    async def test_a_line_tightened_before_the_snapshot_refuses_without_a_model_turn(
        self, tmp_path, monkeypatch, mode
    ):
        from kiro_crew.history_consolidation import _CONSOLIDATION_REFUSED

        log = _seed_log(tmp_path)
        c = _make_consolidator(log)
        c._call_llm = AsyncMock(return_value={"history_entry": "leaked"})
        # ``_locked`` is the snapshot's own lock acquisition: the line flips just
        # before the rows are read under it, after the privacy pre-checks ran.
        self._tighten_before(log, "_locked", monkeypatch, mode=mode)

        outcome = await finished(c._consolidate(KEY, include_history=True), "consolidation")

        assert outcome is _CONSOLIDATION_REFUSED
        c._call_llm.assert_not_awaited()
        c._memory.append_history.assert_not_called()
        assert log.consolidation_counts(KEY)[1] == 3, "the span was marked consolidated"
        assert log.get_metadata(KEY).get("consolidation_attempts", 0) in (
            0,
            None,
        ), "a refusal was charged as a failed attempt"

    @pytest.mark.asyncio
    async def test_a_line_tightened_during_the_model_turn_discards_the_result(
        self, tmp_path, monkeypatch
    ):
        from kiro_crew.history_consolidation import _CONSOLIDATION_REFUSED

        log = _seed_log(tmp_path)
        c = _make_consolidator(log)

        async def response(_prompt, **_kwargs):
            with history_mod.allow_on_loop_persist():
                log.update_metadata(KEY, {"memory_mode": "incognito"})
            return {"history_entry": "derived from rows now under a restricted line"}

        with patch.object(c, "_call_llm", AsyncMock(side_effect=response)):
            outcome = await finished(c._consolidate(KEY, include_history=True), "consolidation")

        assert outcome is _CONSOLIDATION_REFUSED
        c._memory.append_history.assert_not_called()
        assert log.consolidation_counts(KEY)[1] == 3, "the span was marked consolidated"

    @staticmethod
    def _tighten_at_publication(log, monkeypatch):
        """Tighten the line immediately before the first publication hold enters."""
        real_hold = log.publication_hold
        fired = {"done": False}

        @contextlib.contextmanager
        def tighten_then_hold(key):
            if not fired["done"]:
                fired["done"] = True
                with history_mod.allow_on_loop_persist():
                    log.update_metadata(KEY, {"memory_mode": "incognito"})
            with real_hold(key):
                yield

        monkeypatch.setattr(log, "publication_hold", tighten_then_hold)
        return fired

    @pytest.mark.asyncio
    async def test_a_line_tightened_before_history_publication_refuses_the_write(
        self, tmp_path, monkeypatch
    ):
        from kiro_crew.history_consolidation import _CONSOLIDATION_REFUSED

        log = _seed_log(tmp_path)
        c = _make_consolidator(log)
        c._call_llm = AsyncMock(return_value={"history_entry": "must not persist"})
        fired = self._tighten_at_publication(log, monkeypatch)

        outcome = await finished(c._consolidate(KEY, include_history=True), "consolidation")

        assert fired["done"], "the history publication interleaving did not occur"
        assert outcome is _CONSOLIDATION_REFUSED
        c._memory.append_history.assert_not_called()
        assert int(log.get_metadata(KEY).get("last_consolidated", 0)) == 0

    @pytest.mark.asyncio
    async def test_a_line_tightened_before_member_publication_refuses_the_atomic_write(
        self, tmp_path, monkeypatch
    ):
        from kiro_crew.context import ContextBuilder
        from kiro_crew.execution_context import ExecutionContext, MemoryStoreRef
        from kiro_crew.history_consolidation import _CONSOLIDATION_REFUSED

        log = _seed_log(tmp_path)
        vectors = MagicMock()
        vectors.algorithm_version = "v2"
        vectors.consolidation_receipt.return_value = None
        vectors.get_all_semantic.return_value = []
        vectors.with_record_metadata.return_value = []
        execution = ExecutionContext(
            "member-id",
            MemoryStoreRef("member-store", "member-id"),
            "member",
            "kirocrew",
            "persistent",
        )
        monkeypatch.setattr(
            "kiro_crew.execution_context.read_session_execution", lambda _key: execution
        )
        monkeypatch.setattr("kiro_crew.memory_stores.memory_store_version", lambda _store: 2)
        monkeypatch.setattr(ContextBuilder, "ensure_store", AsyncMock(return_value=vectors))
        c = _make_consolidator(log)
        monkeypatch.setattr(ContextBuilder, "get_memory_for", lambda **_kwargs: c._memory)
        c._call_llm = AsyncMock(return_value={"history_entry": "must not persist"})
        fired = self._tighten_at_publication(log, monkeypatch)

        outcome = await finished(c._consolidate(KEY, include_history=True), "consolidation")

        assert fired["done"], "the member publication interleaving did not occur"
        assert outcome is _CONSOLIDATION_REFUSED
        vectors.apply_consolidation.assert_not_called()
        assert int(log.get_metadata(KEY).get("last_consolidated", 0)) == 0

    @pytest.mark.asyncio
    async def test_an_unreadable_line_under_the_snapshot_lock_refuses(self, tmp_path, monkeypatch):
        """Fail closed: rows whose contract cannot be read are not derived from."""
        from kiro_crew.history_consolidation import _CONSOLIDATION_REFUSED

        log = _seed_log(tmp_path)
        c = _make_consolidator(log)
        c._call_llm = AsyncMock(return_value={"history_entry": "leaked"})
        # Unreadable only from the snapshot's own lock hold onward, so the
        # pre-checks (which read the line too) pass and the snapshot is the gate.
        # Hooked on the snapshot itself: the lease check takes the same lock
        # first and must still read the line.
        real_snapshot = type(log).snapshot_for_consolidation
        real_status = type(log)._read_metadata_status
        inside = {"lock": False}

        def _snapshot_then_unreadable(self, key, **kwargs):
            inside["lock"] = True
            return real_snapshot(self, key, **kwargs)

        def _status(self, key):
            if inside["lock"] and key == KEY:
                return {}, False
            return real_status(self, key)

        monkeypatch.setattr(type(log), "snapshot_for_consolidation", _snapshot_then_unreadable)
        monkeypatch.setattr(type(log), "_read_metadata_status", _status)

        outcome = await finished(c._consolidate(KEY, include_history=True), "consolidation")

        assert outcome is _CONSOLIDATION_REFUSED
        c._call_llm.assert_not_awaited()

    def test_the_snapshot_is_ungated_unless_asked(self, tmp_path):
        """Other callers keep the plain three-tuple; only the consolidator asks."""
        log = _seed_log(tmp_path)
        with history_mod.allow_on_loop_persist():
            log.update_metadata(KEY, {"memory_mode": "incognito"})
        rows, total, generation, _sub = log.snapshot_for_consolidation(KEY)
        assert (len(rows), total) == (3, 3)
        with pytest.raises(history_mod.TranscriptWithheld):
            log.snapshot_for_consolidation(KEY, withhold_restricted=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result",
    (
        {"history_entry": "must not persist"},
        {"preferences_update": "# User Preferences\n- dark mode"},
        {"projects_update": "# Active Projects\n- Kiro Crew"},
    ),
    ids=("history", "preferences", "projects"),
)
async def test_persistence_disabled_after_extraction_refuses_before_marking(
    tmp_path, monkeypatch, result
):
    from kiro_crew import history_consolidation
    from kiro_crew.history_consolidation import _CONSOLIDATION_REFUSED

    log = _seed_log(tmp_path)
    c = _make_consolidator(log)
    c._migrated = False
    disabled = False

    async def response(_prompt, **_kwargs):
        nonlocal disabled
        disabled = True
        return result

    monkeypatch.setattr(history_consolidation, "_persistence_disabled", lambda: disabled)
    c._call_llm = AsyncMock(side_effect=response)
    mark_consolidated = MagicMock(wraps=log.mark_consolidated)
    monkeypatch.setattr(log, "mark_consolidated", mark_consolidated)

    outcome = await finished(c._consolidate(KEY, include_history=True), "consolidation")

    assert outcome is _CONSOLIDATION_REFUSED
    mark_consolidated.assert_not_called()
    c._memory.append_history.assert_not_called()
    c._memory.write_preferences.assert_not_called()
    c._memory.write_projects.assert_not_called()
    assert log.consolidation_counts(KEY)[1] == 3


@pytest.mark.asyncio
async def test_persistence_flip_after_first_commit_finishes_the_run(tmp_path, monkeypatch):
    from kiro_crew import history_consolidation

    log = _seed_log(tmp_path)
    c = _make_consolidator(log)
    c._migrated = False
    disabled = False

    def append_history(_entry):
        nonlocal disabled
        disabled = True

    monkeypatch.setattr(history_consolidation, "_persistence_disabled", lambda: disabled)
    c._call_llm = AsyncMock(
        return_value={
            "history_entry": "committed history",
            "preferences_update": "# User Preferences\n- dark mode",
        }
    )
    c._memory.append_history.side_effect = append_history
    c._memory.write_preferences.return_value = True
    mark_consolidated = MagicMock(wraps=log.mark_consolidated)
    monkeypatch.setattr(log, "mark_consolidated", mark_consolidated)

    outcome = await finished(c._consolidate(KEY, include_history=True), "consolidation")

    assert outcome is None
    c._memory.append_history.assert_called_once_with("committed history")
    c._memory.write_preferences.assert_called_once_with(
        "# User Preferences\n- dark mode", expected_baseline=""
    )
    mark_consolidated.assert_called_once()
    assert log.consolidation_counts(KEY)[1] == 0


@pytest.mark.asyncio
async def test_cas_refused_first_publication_does_not_arm_commit_latch(tmp_path, monkeypatch):
    from kiro_crew import history_consolidation
    from kiro_crew.history_consolidation import _CONSOLIDATION_REFUSED

    log = _seed_log(tmp_path)
    c = _make_consolidator(log)
    c._migrated = False
    disabled = False

    def refuse_preferences(*_args, **_kwargs):
        nonlocal disabled
        disabled = True
        return False

    monkeypatch.setattr(history_consolidation, "_persistence_disabled", lambda: disabled)
    c._call_llm = AsyncMock(
        return_value={
            "preferences_update": "# User Preferences\n- dark mode",
            "projects_update": "# Active Projects\n- Kiro Crew",
        }
    )
    c._memory.write_preferences.side_effect = refuse_preferences
    c._memory.write_projects.return_value = True
    mark_consolidated = MagicMock(wraps=log.mark_consolidated)
    monkeypatch.setattr(log, "mark_consolidated", mark_consolidated)

    outcome = await finished(c._consolidate(KEY, include_history=True), "consolidation")

    assert outcome is _CONSOLIDATION_REFUSED
    c._memory.write_preferences.assert_called_once_with(
        "# User Preferences\n- dark mode", expected_baseline=""
    )
    c._memory.write_projects.assert_not_called()
    c._memory.append_history.assert_not_called()
    mark_consolidated.assert_not_called()
    assert log.consolidation_counts(KEY)[1] == 3


def _refuse_holds_after(log, monkeypatch, first_refused: int, exc: Exception) -> dict:
    """Let the first ``first_refused - 1`` publication holds enter; refuse the rest.

    The refusal is raised where ``publication_hold`` raises its own: on entry,
    before the guarded body runs, so the write under a refused hold never happens.
    """
    real_hold = log.publication_hold
    calls = {"n": 0}

    @contextlib.contextmanager
    def counted_hold(key, **kwargs):
        calls["n"] += 1
        if calls["n"] >= first_refused:
            raise exc
        with real_hold(key, **kwargs):
            yield

    monkeypatch.setattr(log, "publication_hold", counted_hold)
    return calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "refusal",
    (
        history_mod.TranscriptBusy("transcript lock not acquired in time"),
        history_mod.TranscriptWithheld("transcript is restricted"),
    ),
    ids=("busy", "withheld"),
)
async def test_a_refused_hold_after_the_first_commit_marks_the_span(
    tmp_path, monkeypatch, caplog, refusal
):
    """History committed, then a later hold is refused: the span is still marked.

    Refusing the run at that point leaves the span pending, the idle sweep runs
    it again and ``append_history`` -- which carries no receipt -- appends the
    same entry twice. The later outputs are skipped with a warning instead.
    """
    log = _seed_log(tmp_path)
    c = _make_consolidator(log)
    c._migrated = False
    c._call_llm = AsyncMock(
        return_value={
            "history_entry": "committed history",
            "preferences_update": "# User Preferences\n- dark mode",
            "projects_update": "# Active Projects\n- Kiro Crew",
        }
    )
    c._memory.write_preferences.return_value = True
    c._memory.write_projects.return_value = True
    calls = _refuse_holds_after(log, monkeypatch, first_refused=2, exc=refusal)
    mark_consolidated = MagicMock(wraps=log.mark_consolidated)
    monkeypatch.setattr(log, "mark_consolidated", mark_consolidated)

    with caplog.at_level("WARNING", logger="kiro_crew.history"):
        outcome = await finished(c._consolidate(KEY, include_history=True), "consolidation")

    assert outcome is None
    assert calls["n"] == 2, "publication continued past the refused hold"
    c._memory.append_history.assert_called_once_with("committed history")
    c._memory.write_preferences.assert_not_called()
    c._memory.write_projects.assert_not_called()
    mark_consolidated.assert_called_once_with(KEY, 3, 0)
    assert log.consolidation_counts(KEY)[1] == 0, "the span was left pending"
    assert log.get_metadata(KEY).get("consolidation_attempts", 0) in (0, None)
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "stopped at the preferences stage" in warnings[0].getMessage()
    assert str(refusal) in warnings[0].getMessage()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "refusal",
    (
        history_mod.TranscriptBusy("transcript lock not acquired in time"),
        history_mod.TranscriptWithheld("transcript is restricted"),
    ),
    ids=("busy", "withheld"),
)
async def test_a_refused_hold_before_the_first_commit_still_refuses_the_run(
    tmp_path, monkeypatch, refusal
):
    """Nothing committed, so nothing can be duplicated: refuse, mark nothing."""
    log = _seed_log(tmp_path)
    c = _make_consolidator(log)
    c._migrated = False
    c._call_llm = AsyncMock(
        return_value={
            "history_entry": "must not persist",
            "preferences_update": "# User Preferences\n- dark mode",
        }
    )
    calls = _refuse_holds_after(log, monkeypatch, first_refused=1, exc=refusal)
    mark_consolidated = MagicMock(wraps=log.mark_consolidated)
    monkeypatch.setattr(log, "mark_consolidated", mark_consolidated)

    from kiro_crew.history_consolidation import _CONSOLIDATION_REFUSED

    outcome = await finished(c._consolidate(KEY, include_history=True), "consolidation")

    assert outcome is _CONSOLIDATION_REFUSED
    assert calls["n"] == 1
    c._memory.append_history.assert_not_called()
    c._memory.write_preferences.assert_not_called()
    mark_consolidated.assert_not_called()
    assert log.consolidation_counts(KEY)[1] == 3


@pytest.mark.asyncio
async def test_a_marker_failure_after_a_refused_later_hold_is_charged(tmp_path, monkeypatch):
    """The busy stem can also fail the marker write: it is charged like any post-LLM failure."""
    log = _seed_log(tmp_path)
    c = _make_consolidator(log)
    c._migrated = False
    c._call_llm = AsyncMock(
        return_value={
            "history_entry": "committed history",
            "preferences_update": "# User Preferences\n- dark mode",
        }
    )
    _refuse_holds_after(
        log, monkeypatch, first_refused=2, exc=history_mod.TranscriptBusy("held elsewhere")
    )
    monkeypatch.setattr(
        log, "mark_consolidated", MagicMock(side_effect=history_mod.HistoryLockTimeout("held"))
    )

    with pytest.raises(history_mod.HistoryLockTimeout):
        await finished(c._consolidate(KEY, include_history=True), "consolidation")

    c._memory.append_history.assert_called_once_with("committed history")
    assert int(log.get_metadata(KEY).get("consolidation_attempts", 0)) == 1


@pytest.mark.asyncio
async def test_persistence_rechecked_inside_member_publication_hold(tmp_path, monkeypatch):
    from kiro_crew import history_consolidation
    from kiro_crew.context import ContextBuilder
    from kiro_crew.execution_context import ExecutionContext, MemoryStoreRef
    from kiro_crew.history_consolidation import _CONSOLIDATION_REFUSED

    log = _seed_log(tmp_path)
    vectors = MagicMock()
    vectors.algorithm_version = "v2"
    vectors.consolidation_receipt.return_value = None
    vectors.get_all_semantic.return_value = []
    vectors.with_record_metadata.return_value = []
    execution = ExecutionContext(
        "member-id",
        MemoryStoreRef("member-store", "member-id"),
        "member",
        "kirocrew",
        "persistent",
    )
    monkeypatch.setattr(
        "kiro_crew.execution_context.read_session_execution", lambda _key: execution
    )
    monkeypatch.setattr("kiro_crew.memory_stores.memory_store_version", lambda _store: 2)
    monkeypatch.setattr(ContextBuilder, "ensure_store", AsyncMock(return_value=vectors))
    c = _make_consolidator(log)
    monkeypatch.setattr(ContextBuilder, "get_memory_for", lambda **_kwargs: c._memory)
    disabled = False

    async def response(_prompt, **_kwargs):
        nonlocal disabled
        disabled = True
        return {"history_entry": "must not persist"}

    monkeypatch.setattr(history_consolidation, "_persistence_disabled", lambda: disabled)
    c._call_llm = AsyncMock(side_effect=response)
    mark_consolidated = MagicMock(wraps=log.mark_consolidated)
    monkeypatch.setattr(log, "mark_consolidated", mark_consolidated)

    outcome = await finished(c._consolidate(KEY, include_history=True), "consolidation")

    assert outcome is _CONSOLIDATION_REFUSED
    vectors.apply_consolidation.assert_not_called()
    mark_consolidated.assert_not_called()
    assert log.consolidation_counts(KEY)[1] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("result", "blocked_writer"),
    (
        (
            {"lessons": [{"rule": "Always verify the durable write", "category": "knowledge"}]},
            "write_lesson",
        ),
        (
            {
                "episodic": [
                    {"text": "The durable write was verified before publication.", "tags": []}
                ]
            },
            "write_episodic",
        ),
    ),
    ids=("lesson", "episodic"),
)
async def test_persistence_rechecked_inside_v1_vector_publication_hold(
    tmp_path, monkeypatch, result, blocked_writer
):
    from kiro_crew import history_consolidation
    from kiro_crew.history_consolidation import _CONSOLIDATION_REFUSED

    log = _seed_log(tmp_path)
    vectors = MagicMock()
    vectors.algorithm_version = "v1"
    vectors.get_all_semantic.return_value = []
    vectors.space_generation = 1
    vectors.embed_lesson.return_value = [0.1]
    vectors.embed_episodic.return_value = [0.1]
    c = _make_consolidator(log, vector_store=vectors)
    disabled = False

    async def response(_prompt, **_kwargs):
        nonlocal disabled
        disabled = True
        return result

    monkeypatch.setattr(history_consolidation, "_persistence_disabled", lambda: disabled)
    c._call_llm = AsyncMock(side_effect=response)
    mark_consolidated = MagicMock(wraps=log.mark_consolidated)
    monkeypatch.setattr(log, "mark_consolidated", mark_consolidated)

    outcome = await finished(c._consolidate(KEY, include_history=True), "consolidation")

    assert outcome is _CONSOLIDATION_REFUSED
    getattr(vectors, blocked_writer).assert_not_called()
    vectors.write_lesson.assert_not_called()
    vectors.write_episodic.assert_not_called()
    mark_consolidated.assert_not_called()
    assert log.consolidation_counts(KEY)[1] == 3


@pytest.mark.asyncio
async def test_persistence_rechecked_inside_skill_publication_hold(tmp_path, monkeypatch):
    from kiro_crew import history_consolidation
    from kiro_crew.history_consolidation import _CONSOLIDATION_REFUSED

    log = _seed_log(tmp_path)
    loader = MagicMock()
    loader.list_auto_skills.return_value = []
    loader.list_pending_skills.return_value = []
    loader.find_similar.return_value = None
    c = _make_consolidator(
        log,
        skills_loader=loader,
        auto_skills_enabled=True,
        auto_min_tool_calls=0,
        approval_required=True,
    )
    disabled = False
    calls = 0

    async def response(_prompt, **_kwargs):
        nonlocal calls, disabled
        calls += 1
        if calls == 1:
            return {"lessons": []}
        disabled = True
        return {
            "new_skill": {
                "slug": "checked-publication-hold",
                "description": "Verify a publication hold before writing",
                "triggers": "publication, persistence",
                "procedure_md": "## When to use\nBefore a durable write.\n\n## Steps\nVerify the switch.\n\n## Gotchas\nDo not bypass the hold.",
            }
        }

    monkeypatch.setattr(history_consolidation, "_persistence_disabled", lambda: disabled)
    c._call_llm = AsyncMock(side_effect=response)
    mark_consolidated = MagicMock(wraps=log.mark_consolidated)
    monkeypatch.setattr(log, "mark_consolidated", mark_consolidated)

    outcome = await finished(c._consolidate(KEY, include_history=True), "consolidation")

    assert calls == 2, "the skill-candidate publication interleaving did not occur"
    assert outcome is _CONSOLIDATION_REFUSED
    loader.stage_skill_candidate.assert_not_called()
    loader.create_auto_skill.assert_not_called()
    loader.update_auto_skill.assert_not_called()
    mark_consolidated.assert_not_called()
    assert log.consolidation_counts(KEY)[1] == 3


@pytest.mark.asyncio
async def test_consolidate_refuses_a_channel_thread_whose_map_entry_was_pruned(
    tmp_path, monkeypatch
):
    """The transcript header is the record a memory reader consults.

    ``privacy_mode.apply_mode`` stamps ``memory_mode`` into the header, the record
    ``_consolidate`` refuses on, so a channel thread's mode survives the pruning
    of its session-map entry. Driven the way production runs it: the real
    modifier path over a real ``SessionMap``, then a consolidator that holds NO
    session map at all (a header-only reader), with the process-local trackers
    emptied (the restart), through the session-end hook's entry point.
    """
    from types import SimpleNamespace

    from kiro_crew.history_consolidation import _CONSOLIDATION_REFUSED
    from kiro_crew.messaging import privacy_mode
    from kiro_crew.session_map import SessionMap

    monkeypatch.setattr("kiro_crew.session_map.config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.session_map._KIRO_SESSIONS_DIR", tmp_path / "kiro")
    live_key = "telegram:kirocrew:direct:4242"
    log = _seed_default_log(live_key)  # three turns before the modifier
    sm = SessionMap()
    sessions = SimpleNamespace(_session_map=sm, channel_key_for_stem=sm.channel_key_for_stem)
    privacy_mode.reset()
    try:
        await privacy_mode.apply_mode(
            privacy_mode.MODE_INCOGNITO,
            live_key,
            source="telegram",
            sessions=sessions,
        )
        sm.flush()
        assert log.get_metadata(live_key).get("memory_mode") == "incognito", "premise: stamped"
        privacy_mode.reset()  # the fresh gateway's trackers start empty
        c = _make_consolidator(log, sessions=None)  # no map: the header must answer
        c._call_llm = AsyncMock(return_value={"history_entry": "x"})
        real_consolidate = c._consolidate
        outcomes: list[object] = []

        async def _recording(*args, **kwargs):
            outcomes.append(await real_consolidate(*args, **kwargs))
            return outcomes[-1]

        c._consolidate = _recording
        c.consolidate_session(live_key)
        tasks = list(c._tasks)
        assert len(tasks) == 1, "premise: the session-end hook scheduled one pass"
        await await_until(tasks[0], what="consolidation task")
        assert len(outcomes) == 1, "premise: the session-end hook ran one pass"
        result = outcomes[0]
    finally:
        privacy_mode.reset()

    assert (
        c._call_llm.await_count == 0
    ), "a channel thread marked incognito was consolidated without its session map"
    assert result is _CONSOLIDATION_REFUSED
    assert log.unconsolidated_count(live_key) == 3
    assert log.get_metadata(live_key).get("last_consolidated", 0) == 0


@pytest.mark.asyncio
async def test_a_legacy_privacy_flag_is_kept_by_prune_and_stamped_by_the_startup_sweep(
    tmp_path, monkeypatch
):
    """A thread flagged before the header carried the mode: the map entry is all it has.

    Pre-upgrade installs hold private threads whose flag was written straight to
    the session map, with no header stamp, and ``apply_mode`` returns early for a
    mode already marked, so nothing re-stamps them. Two facts pinned in one
    lifecycle: the startup ``prune()`` keeps such an entry (clearing only its
    dead ``sid``) and never reads a transcript for it; and the startup path's
    off-loop step then copies the mode into the existing transcript's header
    while the row STAYS (it is what the channel gate hydrates from), after which
    the consolidator -- a header reader, holding no map -- refuses the thread.
    """
    import threading

    from kiro_crew.history import ConversationLog
    from kiro_crew.history_consolidation import _CONSOLIDATION_REFUSED
    from kiro_crew.messaging import privacy_mode
    from kiro_crew.session_map import SessionMap

    monkeypatch.setattr("kiro_crew.session_map.config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.session_map._KIRO_SESSIONS_DIR", tmp_path / "kiro")
    live_key = "telegram:kirocrew:direct:4242"
    log = _seed_default_log(live_key)
    assert "memory_mode" not in log.get_metadata(live_key), "premise: legacy header, no mode"
    sm = SessionMap()
    sm.set(live_key, "sid-reclaimed-by-kiro-cli")  # the provider session, since reclaimed
    sm.set_flag(live_key, "incognito", True)  # the legacy flag: map only, no header stamp
    sm.flush()
    privacy_mode.reset()
    loop_thread = threading.current_thread()
    header_reads: list[threading.Thread] = []
    real_get_metadata = ConversationLog.get_metadata

    def _recording_get_metadata(self, key):
        header_reads.append(threading.current_thread())
        return real_get_metadata(self, key)

    monkeypatch.setattr(ConversationLog, "get_metadata", _recording_get_metadata)
    try:
        # The restart: startup prunes stale entries on the loop -- no transcript read.
        pruned = sm.prune()
        sm.flush()
        assert pruned == 0
        assert sm.get_flag(live_key, "incognito") is True
        assert header_reads == [], "prune read a transcript header on the loop"

        # The startup path's off-loop step: the mode moves into the header, the
        # row stays, and neither happened on the loop thread.
        assert await sm.stamp_privacy_headers() == 1
        assert header_reads, "premise: the header was probed"
        assert [t for t in header_reads if t is loop_thread] == []
        assert sm.get_flag(live_key, "incognito") is True
        assert real_get_metadata(log, live_key).get("memory_mode") == "incognito"
        assert log.unconsolidated_count(live_key) == 3

        # From here the header answers: a consolidator with no map refuses.
        privacy_mode.reset()
        header_only = _make_consolidator(log, sessions=None)
        header_only._call_llm = AsyncMock(return_value={"history_entry": "x"})
        assert (
            await finished(header_only._consolidate(live_key), "consolidation")
            is _CONSOLIDATION_REFUSED
        )
        header_only._call_llm.assert_not_awaited()
    finally:
        privacy_mode.reset()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("version", "seed_source", "assistant_value"),
    (
        (1, "user_explicit", "old@example.com"),
        (1, f"consolidation:{KEY}", "new@example.com"),
        (2, "user_explicit", "new@example.com"),
    ),
    ids=("v1-owner", "v1-automatic", "v2-verified-correction"),
)
@pytest.mark.parametrize("mutation", ("edit", "delete", "withdraw", "assistant"))
async def test_pending_extraction_rechecks_its_actual_transcript(
    tmp_path, monkeypatch, version, seed_source, assistant_value, mutation
):
    from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
    from kiro_crew.context import ContextBuilder
    from kiro_crew.history_consolidation import _CONSOLIDATION_REFUSED
    from kiro_crew.memory_stores import memory_store_dir_for, provision_member_memory
    from kiro_crew.vector_memory import VectorMemoryStore, open_member_database

    store_name = None
    directory = tmp_path / "global"
    if version == 2:
        cfg = KiroCrewConfig.load()
        cfg.agents["writer"] = KiroCrewAgentConfig()
        store_name = provision_member_memory(cfg, "writer")
        cfg.save()
        directory = memory_store_dir_for(store_name)
    if version == 2:
        vectors = open_member_database(
            directory / "memory.db", member_id=cfg.agents["writer"].member_id, store_id=store_name
        )
    else:
        vectors = VectorMemoryStore(db_path=directory / "memory.db")
        vectors.init()
    try:
        assert vectors.set_semantic("user.email", "old@example.com", 1, seed_source) is None
        log = _seed_log(tmp_path, count=0)
        quote = "Please replace old@example.com with new@example.com."
        with history_mod.allow_on_loop_persist():
            log.append(KEY, "user", quote)
        c = _make_consolidator(log, vector_store=vectors)
        monkeypatch.setattr("kiro_crew.context.store_of_session", lambda *_: store_name)
        monkeypatch.setattr(ContextBuilder, "ensure_store", AsyncMock(return_value=vectors))
        monkeypatch.setattr(ContextBuilder, "get_memory_for", lambda *a, **kw: c._memory)
        monkeypatch.setattr(ContextBuilder, "get_lessons_for", lambda *a, **kw: None)

        async def response(_prompt, **_kwargs):
            with history_mod.allow_on_loop_persist():
                if mutation == "edit":
                    rows = log.read_messages(KEY)
                    rows[-1]["content"] = "That replacement was only a hypothetical example."
                    log.rewrite_session(KEY, rows)
                elif mutation == "delete":
                    assert log.delete_session(KEY)
                elif mutation == "withdraw":
                    log.append(KEY, "user", "Do not change my email after all.")
                else:
                    log.append(KEY, "assistant", "Understood.")
            return {
                "history_entry": "The user changed their email.",
                "semantic": [
                    {
                        "key": "user.email",
                        "value": "new@example.com",
                        "confidence": 1,
                        "correction_quote": quote,
                    }
                ],
            }

        with patch.object(c, "_call_llm", AsyncMock(side_effect=response)):
            outcome = await c._consolidate(KEY, include_history=True)
        row = vectors.get_semantic("user.email")
        assert row is not None
        if mutation == "assistant":
            assert outcome is None
            # An assistant append preserves eligibility, not extra write authority.
            assert json.loads(row["value_json"]) == assistant_value
            if version == 2:
                assert "changed their email" in vectors.read_editable_history()
                c._memory.append_history.assert_not_called()
            else:
                c._memory.append_history.assert_called_once()
            assert log.consolidation_counts(KEY)[1] == 1
        else:
            assert outcome is _CONSOLIDATION_REFUSED
            assert json.loads(row["value_json"]) == "old@example.com"
            c._memory.append_history.assert_not_called()
            assert int(log.get_metadata(KEY).get("last_consolidated", 0)) == 0
    finally:
        vectors.close()


def _seed_default_log(key: str, count: int = 3) -> ConversationLog:
    """Like :func:`_seed_log`, in the DEFAULT sessions directory.

    For a test that drives ``privacy_mode.apply_mode``: its header stamp goes
    through a default ``ConversationLog``, so the transcript under test has to
    live where that instance writes (the conftest pins the directory per test).
    """
    log = ConversationLog()
    log.init()
    with history_mod.allow_on_loop_persist():
        for i in range(count):
            log.append(key, "user", f"m{i}")
    return log


class TestEveryEntryPointRespectsTheAccounting:
    @pytest.mark.asyncio
    async def test_consolidate_session_honors_the_backoff(self, tmp_path):
        """The expiry path consults no time throttle of its own at all."""
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)
        with history_mod.allow_on_loop_persist():
            log.update_metadata(
                KEY,
                {
                    "consolidation_attempts": 1,
                    "consolidation_retry_at": time.time() + 3600,
                },
            )

        c.consolidate_session(KEY)
        assert not c._tasks, "session expiry re-fired a backed-off consolidation"

        # Past the deadline the same call proceeds, so backoff delays rather than
        # disables the path.
        with history_mod.allow_on_loop_persist():
            log.update_metadata(KEY, {"consolidation_retry_at": time.time() - 1})
        with patch.object(c, "_consolidate", new_callable=AsyncMock):
            c.consolidate_session(KEY)
            assert c._tasks
            await asyncio.gather(*list(c._tasks), return_exceptions=True)

    @pytest.mark.asyncio
    async def test_manual_dashboard_trigger_refuses_inside_the_backoff(self, tmp_path):
        from kiro_crew.dashboard.handlers.memory import api_memory_consolidate

        log = _seed_log(tmp_path)
        c = _make_consolidator(log)
        with history_mod.allow_on_loop_persist():
            log.update_metadata(
                KEY,
                {
                    "consolidation_attempts": 2,
                    "consolidation_retry_at": time.time() + 3600,
                },
            )

        state = MagicMock()
        state.consolidator = c
        state.conversation_log = log
        state._restricted_keys = set()
        state._slots = {}
        request = _FakeRequest(state, {"key": KEY})

        resp = await api_memory_consolidate(request)  # type: ignore[arg-type]
        assert resp.status == 429
        assert not c._tasks, "manual trigger bypassed the backoff"

        with history_mod.allow_on_loop_persist():
            log.update_metadata(KEY, {"consolidation_retry_at": time.time() - 1})
        with patch.object(c, "_consolidate", new_callable=AsyncMock):
            resp = await api_memory_consolidate(request)  # type: ignore[arg-type]
            assert resp.status == 200
            await asyncio.gather(*list(c._tasks), return_exceptions=True)


class TestTheManualTriggerClaimsAtomically:
    """Two concurrent POSTs must not both dispatch a billed turn.

    The eligibility probe awaits an off-loop transcript read, so a membership
    test taken before that yield and acted on after it is a check-then-act race.
    """

    @pytest.mark.asyncio
    async def test_concurrent_triggers_dispatch_only_once(self, tmp_path):
        from kiro_crew.dashboard.handlers.memory import api_memory_consolidate

        log = _seed_log(tmp_path)
        c = _make_consolidator(log)

        state = MagicMock()
        state.consolidator = c
        state.conversation_log = log
        state._restricted_keys = set()
        state._slots = {}

        with patch.object(c, "_consolidate", new_callable=AsyncMock) as spy:
            # Both requests are in flight across the handler's await, which is
            # exactly the window a check-then-act guard leaves open.
            responses = await asyncio.gather(
                api_memory_consolidate(_FakeRequest(state, {"key": KEY})),  # type: ignore[arg-type]
                api_memory_consolidate(_FakeRequest(state, {"key": KEY})),  # type: ignore[arg-type]
            )
            await asyncio.gather(*list(c._tasks), return_exceptions=True)

        statuses = sorted(r.status for r in responses)
        assert statuses == [200, 409], f"expected one winner and one refusal, got {statuses}"
        assert spy.await_count == 1, (
            f"consolidation dispatched {spy.await_count} times for one span — "
            "the claim is not atomic across the handler's await"
        )

    @pytest.mark.asyncio
    async def test_a_refused_trigger_releases_its_claim(self, tmp_path):
        """A 429 must not leave the key claimed, or the span is wedged forever."""
        from kiro_crew.dashboard.handlers.memory import api_memory_consolidate

        log = _seed_log(tmp_path)
        c = _make_consolidator(log)
        with history_mod.allow_on_loop_persist():
            log.update_metadata(
                KEY,
                {
                    "consolidation_attempts": 2,
                    "consolidation_retry_at": time.time() + 3600,
                },
            )

        state = MagicMock()
        state.consolidator = c
        state.conversation_log = log
        state._restricted_keys = set()
        state._slots = {}
        request = _FakeRequest(state, {"key": KEY})

        resp = await api_memory_consolidate(request)  # type: ignore[arg-type]
        assert resp.status == 429
        assert KEY not in c._running, "the backoff refusal leaked its claim"

        # Once the backoff expires the same key is dispatchable again, which it
        # would not be if the refusal above had left the claim in place.
        with history_mod.allow_on_loop_persist():
            log.update_metadata(KEY, {"consolidation_retry_at": time.time() - 1})
        with patch.object(c, "_consolidate", new_callable=AsyncMock):
            resp = await api_memory_consolidate(request)  # type: ignore[arg-type]
            assert resp.status == 200
            await asyncio.gather(*list(c._tasks), return_exceptions=True)


class TestConsolidateIsTheEligibilityChokePoint:
    """_consolidate() enforces retry eligibility itself.

    The caller pre-checks are scheduling short-circuits and UX; the gate inside
    the operation is what guarantees an entry point without one (the
    preferences/projects path, or a future caller) cannot re-bill a backed-off
    span.
    """

    def _arm_backoff(self, log: ConversationLog, attempts: int = 1) -> None:
        with history_mod.allow_on_loop_persist():
            log.update_metadata(
                KEY,
                {
                    "consolidation_attempts": attempts,
                    "consolidation_retry_at": time.time() + 3600,
                },
            )

    @pytest.mark.asyncio
    async def test_a_direct_call_inside_the_backoff_bills_nothing(self, tmp_path):
        """No caller pre-check at all — the inner gate alone must refuse."""
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)
        self._arm_backoff(log)

        with patch.object(c, "_call_llm", new_callable=AsyncMock) as spy:
            await c._consolidate(KEY, include_history=True)

        spy.assert_not_awaited()
        # The refusal is free: nothing reached the provider, so nothing is
        # charged and the span's accounting is untouched.
        assert log.consolidation_retry_state(KEY, _total(log))[0] == 1
        assert log.unconsolidated_count(KEY) == 3

    @pytest.mark.asyncio
    async def test_a_direct_call_on_a_fresh_span_still_consolidates(self, tmp_path):
        """The eval runner calls _consolidate directly to bypass the message
        threshold; a fresh span carries no backoff, so the gate lets it pass."""
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)

        with patch.object(c, "_call_llm", AsyncMock(return_value={"history_entry": "x"})) as spy:
            await c._consolidate(KEY, include_history=True)

        spy.assert_awaited_once()
        assert log.unconsolidated_count(KEY) == 0

    @pytest.mark.asyncio
    async def test_maybe_consolidate_is_refused_inside_the_backoff(self, tmp_path):
        """The prefs/projects path carries the same cheap pre-check as the
        other automatic entry points, so a backed-off span never even
        schedules a task (this path runs on every user turn)."""
        log = _seed_log(tmp_path, count=history_mod._CONSOLIDATION_THRESHOLD)
        c = _make_consolidator(log)
        self._arm_backoff(log)

        with patch.object(c, "_call_llm", new_callable=AsyncMock) as spy:
            c.maybe_consolidate(KEY)
            assert not c._tasks, "a backed-off span still scheduled a task"

        spy.assert_not_awaited()
        assert KEY not in c._running
        assert log.consolidation_retry_state(KEY, _total(log))[0] == 1

    @pytest.mark.asyncio
    async def test_the_inner_gate_backstops_a_caller_without_a_pre_check(self, tmp_path):
        """A caller whose pre-check misses — a future entry point without one,
        or a pre-check that raced the backoff being recorded — still cannot
        bill the span: the gate inside _consolidate is the enforcement, the
        pre-checks are scheduling short-circuits. The deadline short-circuit
        is bypassed and the first retry_eligible answer (the pre-check) lies
        True; the second (the inner gate) tells the truth."""
        log = _seed_log(tmp_path, count=history_mod._CONSOLIDATION_THRESHOLD)
        c = _make_consolidator(log)
        self._arm_backoff(log)

        with (
            patch.object(c, "_call_llm", new_callable=AsyncMock) as spy,
            patch.object(c, "retry_eligible", side_effect=[True, False]) as eligible,
            patch.object(c, "_inside_retry_deadline", return_value=False),
        ):
            c.maybe_consolidate(KEY)
            assert c._tasks, "premise broken: the bypassed pre-check did not schedule"
            await asyncio.gather(*list(c._tasks), return_exceptions=True)
        assert eligible.call_count == 2, "premise broken: the inner gate was never reached"

        spy.assert_not_awaited()
        assert KEY not in c._running, "the refusal leaked the running claim"
        assert KEY not in c._prefs_offset, "a refused pass advanced the prefs offset"

    @pytest.mark.asyncio
    async def test_a_refusal_does_not_strand_the_key_in_running(self, tmp_path):
        """Callers add the key to _running before the task and rely on
        done-callbacks to release it; a refusal path that skipped the release
        would wedge consolidation for the session permanently. The pre-checks
        are bypassed (no deadline, first retry_eligible answer lies True) so the inner
        gate's refusal path actually runs."""
        log = _seed_log(tmp_path, count=history_mod._CONSOLIDATION_THRESHOLD)
        c = _make_consolidator(log)
        self._arm_backoff(log)

        with (
            patch.object(c, "_call_llm", new_callable=AsyncMock),
            patch.object(c, "retry_eligible", side_effect=[True, False]) as eligible,
            patch.object(c, "_inside_retry_deadline", return_value=False),
        ):
            c.maybe_consolidate(KEY)
            assert c._tasks, "premise broken: the bypassed pre-check did not schedule"
            await asyncio.gather(*list(c._tasks), return_exceptions=True)
        assert eligible.call_count == 2, "premise broken: the inner gate was never reached"

        assert KEY not in c._running, "the refusal leaked the running claim"

        # Once the backoff expires the same key is dispatchable again, which
        # it would not be if the refusal above had left the claim in place.
        with history_mod.allow_on_loop_persist():
            log.update_metadata(KEY, {"consolidation_retry_at": time.time() - 1})
        with patch.object(c, "_call_llm", AsyncMock(return_value={"history_entry": "x"})) as spy:
            c.consolidate_session(KEY)
            assert c._tasks, "a released key was still refused after the backoff"
            await asyncio.gather(*list(c._tasks), return_exceptions=True)
        spy.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_maybe_consolidate_proceeds_outside_the_backoff(self, tmp_path):
        """The gate delays rather than disables the prefs/projects path."""
        log = _seed_log(tmp_path, count=history_mod._CONSOLIDATION_THRESHOLD)
        c = _make_consolidator(log)

        with patch.object(c, "_call_llm", AsyncMock(return_value={"noop": True})) as spy:
            c.maybe_consolidate(KEY)
            assert c._tasks, "premise broken: the threshold did not schedule a task"
            await asyncio.gather(*list(c._tasks), return_exceptions=True)

        spy.assert_awaited_once()
        assert c._prefs_offset.get(KEY) == history_mod._CONSOLIDATION_THRESHOLD
        assert KEY not in c._running

    @pytest.mark.asyncio
    async def test_a_refusal_does_not_advance_the_prefs_offset(self, tmp_path):
        """Advancing the offset on a refusal marks the window consolidated with
        no pass run — once the backoff expires the threshold test would skip it
        until a whole new threshold of messages accumulates, silently dropping
        its preference/project extraction. The pre-checks are bypassed (no
        deadline, first retry_eligible answer lies True) so the inner gate's refusal path
        actually runs."""
        log = _seed_log(tmp_path, count=history_mod._CONSOLIDATION_THRESHOLD)
        c = _make_consolidator(log)
        self._arm_backoff(log)

        with (
            patch.object(c, "_call_llm", new_callable=AsyncMock) as spy,
            patch.object(c, "retry_eligible", side_effect=[True, False]) as eligible,
            patch.object(c, "_inside_retry_deadline", return_value=False),
        ):
            c.maybe_consolidate(KEY)
            assert c._tasks, "premise broken: the bypassed pre-check did not schedule"
            await asyncio.gather(*list(c._tasks), return_exceptions=True)
        assert eligible.call_count == 2, "premise broken: the inner gate was never reached"

        spy.assert_not_awaited()
        assert KEY not in c._prefs_offset, "a refused pass advanced the prefs offset"

        # Once the backoff expires the SAME window is retried — the refusal
        # delayed the extraction rather than dropping it.
        with history_mod.allow_on_loop_persist():
            log.update_metadata(KEY, {"consolidation_retry_at": time.time() - 1})
        with patch.object(c, "_call_llm", AsyncMock(return_value={"noop": True})) as spy:
            c.maybe_consolidate(KEY)
            assert c._tasks, "the un-advanced offset did not re-arm the threshold"
            await asyncio.gather(*list(c._tasks), return_exceptions=True)

        spy.assert_awaited_once()
        assert c._prefs_offset.get(KEY) == history_mod._CONSOLIDATION_THRESHOLD

    @pytest.mark.asyncio
    async def test_consolidate_now_reports_a_refusal(self, tmp_path):
        """consolidate_now's only caller is the CLI, which would otherwise print
        'done ✓' unconditionally; the returned False is what lets it report
        the backoff skip instead of a false success."""
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)
        self._arm_backoff(log)

        with patch.object(c, "_call_llm", new_callable=AsyncMock) as spy:
            assert await c.consolidate_now(KEY) is False

        spy.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_consolidate_now_reports_success_outside_the_backoff(self, tmp_path):
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)

        with patch.object(c, "_call_llm", AsyncMock(return_value={"history_entry": "x"})) as spy:
            assert await c.consolidate_now(KEY) is True

        spy.assert_awaited_once()
        assert log.unconsolidated_count(KEY) == 0
