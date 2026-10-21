"""History consolidation's retry cap stays tied to the span it charges.

Among the cases: a rotation that keeps a capped span's budget or releases it for new
content, a rotation during the turn, foreign writers that rebuild the metadata line (a
compaction, the dashboard slot save), an edited transcript earning a fresh budget, and a
cap that does not outlive its span as the transcript grows. The shared helpers come from
``history_consolidation_retry_helpers.py``.
"""

import asyncio
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from history_consolidation_retry_helpers import (
    KEY,
    _make_consolidator,
    _off_loop,
    _seed_log,
    _span,
    _total,
)

from kiro_crew import history as history_mod
from kiro_crew.dashboard.chat_persistence import (
    _rehydrate_slot_from_history,
    _save_slot_to_history,
)
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.history import (
    _CONSOLIDATION_MAX_ATTEMPTS,
    _CONSOLIDATION_PROMPT_BUDGET_CHARS,
    _SESSION_MAX_BYTES,
    ConversationLog,
    HistoryConsolidator,
)

# A row big enough that five of them exceed the rotation byte budget whatever it
# is set to. Rotation on a handful of huge rows is driven by the byte-shrink loop,
# not the line cap, so the workload has to be sized off the budget: a hardcoded
# byte figure silently stops rotating when the budget is raised, and these tests
# assert on a rotation having fired.
_OVER_CAP_ROW_CHARS = _SESSION_MAX_BYTES // 3


def _eligible(c: HistoryConsolidator, log: ConversationLog, now=None) -> bool:
    """retry_eligible with the count its callers already hold."""
    return c.retry_eligible(KEY, now, message_count=_total(log))


def _dashboard_state(log: ConversationLog) -> DashboardState:
    """A DashboardState wired to *log* — enough for the slot rehydrate/save paths."""
    sessions = MagicMock(count=0)
    sessions.get_pid = MagicMock(return_value=None)
    sessions.channel_key_for_stem = MagicMock(return_value=None)
    return DashboardState(
        sessions=sessions,
        crons=MagicMock(list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})),
        lessons=MagicMock(load_all=MagicMock(return_value=[])),
        start_time=0.0,
        conversation_log=log,
    )


class TestRotationDoesNotClearACappedBudget:
    """mark_consolidated is also the abandon path, and a rotation resets to 0.

    When the offset is not applied the span stays unconsolidated, so the write
    must not drop the accounting: clearing it there would hand the span a fresh
    budget every time a rewrite raced the marker, and the cap would never hold.
    Whether a LATER read still counts those attempts is a separate question,
    answered by span identity (see TestRotationReleasesTheBudgetForNewContent) —
    these tests pin the durable write.
    """

    @pytest.mark.asyncio
    async def test_a_generation_change_retains_the_capped_state(self, tmp_path):
        log = _seed_log(tmp_path)

        def _write() -> None:
            log.update_metadata(
                KEY,
                {
                    "consolidation_attempts": _CONSOLIDATION_MAX_ATTEMPTS,
                    "consolidation_retry_at": time.time() + 3600,
                    "rotation_generation": 4,
                },
            )
            # The caller's snapshot generation (2) does not match, so
            # mark_consolidated resets the offset to 0 instead of applying it.
            log.mark_consolidated(KEY, 3, 2)

        await _off_loop(_write)

        meta = log.get_metadata(KEY)
        assert meta["last_consolidated"] == 0
        assert meta.get("consolidation_attempts") == _CONSOLIDATION_MAX_ATTEMPTS, (
            "an unapplied offset cleared the cap on an unmarked span, buying "
            "another billed attempt"
        )
        assert meta.get("consolidation_retry_at")

    @pytest.mark.asyncio
    async def test_an_offset_beyond_the_message_count_retains_the_capped_state(self, tmp_path):
        """The count fallback also resets to 0 without advancing the marker."""
        log = _seed_log(tmp_path)

        def _write() -> None:
            log.update_metadata(
                KEY,
                {
                    "consolidation_attempts": _CONSOLIDATION_MAX_ATTEMPTS,
                    "consolidation_retry_at": time.time() + 3600,
                },
            )
            log.mark_consolidated(KEY, 999, 0)

        await _off_loop(_write)

        assert log.get_metadata(KEY)["last_consolidated"] == 0
        assert log.consolidation_retry_state(KEY)[0] == _CONSOLIDATION_MAX_ATTEMPTS

    @pytest.mark.asyncio
    async def test_an_applied_offset_still_releases_the_budget(self, tmp_path):
        """The success path is unchanged: a marked span drops its accounting."""
        log = _seed_log(tmp_path)

        def _write() -> None:
            log.update_metadata(
                KEY,
                {
                    "consolidation_attempts": 2,
                    "consolidation_retry_at": time.time() + 3600,
                    "consolidation_env_failures": 4,
                    "consolidation_attempts_generation": 0,
                    "consolidation_attempts_offset": 0,
                },
            )
            log.mark_consolidated(KEY, 3, 0)

        await _off_loop(_write)

        meta = log.get_metadata(KEY)
        assert meta["last_consolidated"] == 3
        assert log.consolidation_retry_state(KEY) == (0, 0.0)
        for stale in (
            "consolidation_env_failures",
            "consolidation_attempts_generation",
            "consolidation_attempts_offset",
        ):
            assert stale not in meta, f"{stale} outlived the span it described"


class TestRotationReleasesTheBudgetForNewContent:
    """The cap abandons ONE span, so it must not outlive that span.

    A rotation archives the messages the failures were charged against and resets
    the marker to 0. Carrying a capped count onto the retained tail would silence
    consolidation for the session permanently — every message written afterwards
    stays ineligible forever. The counter is therefore bound to the
    ``(rotation_generation, last_consolidated)`` pair it was charged against: the
    same span keeps its cap, a genuinely new one gets a fresh bounded budget.
    """

    @pytest.mark.asyncio
    async def test_a_charged_attempt_records_the_span_it_belongs_to(self, tmp_path):
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)

        with patch.object(c, "_call_llm", AsyncMock(return_value=None)):
            await c._consolidate(KEY, include_history=True)

        meta = log.get_metadata(KEY)
        assert meta["consolidation_attempts"] == 1
        assert meta["consolidation_attempts_generation"] == 0
        assert meta["consolidation_attempts_offset"] == 0

    @pytest.mark.asyncio
    async def test_a_new_generation_gets_a_fresh_budget(self, tmp_path):
        log = _seed_log(tmp_path)
        await _off_loop(
            lambda: log.update_metadata(
                KEY,
                {
                    "consolidation_attempts": _CONSOLIDATION_MAX_ATTEMPTS,
                    "consolidation_retry_at": time.time() - 1,
                    "consolidation_attempts_generation": 0,
                    "consolidation_attempts_offset": 0,
                    "rotation_generation": 1,
                },
            )
        )

        assert log.consolidation_retry_state(KEY)[0] == 0, (
            "a capped count from an archived span disabled consolidation for "
            "every message written after the rotation"
        )
        assert _make_consolidator(log).retry_eligible(KEY) is True

    @pytest.mark.asyncio
    async def test_the_same_span_stays_capped(self, tmp_path):
        """No free attempt while the span is unchanged."""
        log = _seed_log(tmp_path)
        await _off_loop(
            lambda: log.update_metadata(
                KEY,
                {
                    "consolidation_attempts": _CONSOLIDATION_MAX_ATTEMPTS,
                    "consolidation_retry_at": time.time() - 1,
                    "consolidation_attempts_generation": 0,
                    "consolidation_attempts_offset": 0,
                },
            )
        )

        assert log.consolidation_retry_state(KEY)[0] == _CONSOLIDATION_MAX_ATTEMPTS
        assert (
            _make_consolidator(log).retry_eligible(KEY) is False
        ), "the same failing span bought another billed attempt"

    @pytest.mark.asyncio
    async def test_a_fresh_budget_still_waits_out_the_backoff(self, tmp_path):
        """A new span is not a free immediate turn on a host that keeps failing."""
        log = _seed_log(tmp_path)
        await _off_loop(
            lambda: log.update_metadata(
                KEY,
                {
                    "consolidation_attempts": _CONSOLIDATION_MAX_ATTEMPTS,
                    "consolidation_retry_at": time.time() + 3600,
                    "consolidation_attempts_generation": 0,
                    "consolidation_attempts_offset": 0,
                    "rotation_generation": 1,
                },
            )
        )

        assert log.consolidation_retry_state(KEY)[0] == 0
        assert _make_consolidator(log).retry_eligible(KEY) is False

    @pytest.mark.asyncio
    async def test_unstamped_accounting_keeps_the_cap(self, tmp_path):
        """Unknown provenance must fail closed, not grant unbounded retries."""
        log = _seed_log(tmp_path)
        await _off_loop(
            lambda: log.update_metadata(
                KEY,
                {
                    "consolidation_attempts": _CONSOLIDATION_MAX_ATTEMPTS,
                    "consolidation_retry_at": time.time() - 1,
                    "rotation_generation": 7,
                },
            )
        )

        assert log.consolidation_retry_state(KEY)[0] == _CONSOLIDATION_MAX_ATTEMPTS

    @pytest.mark.asyncio
    async def test_content_written_after_a_real_rotation_consolidates(self, tmp_path):
        """End to end through the real rotation path, not a planted generation.

        The capped state is planted rather than accrued: when the abandon path's
        marker write SUCCEEDS it clears the accounting itself, so the state that
        survives a rotation is the one whose marker write was refused.
        """
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)

        def _plant_and_rotate() -> None:
            log.update_metadata(
                KEY,
                {
                    "consolidation_attempts": _CONSOLIDATION_MAX_ATTEMPTS,
                    "consolidation_retry_at": time.time() + 3600,
                    "consolidation_attempts_generation": 0,
                    "consolidation_attempts_offset": 0,
                },
            )
            assert c.retry_eligible(KEY) is False

            # Blow the FILE byte budget so _maybe_rotate archives the failing
            # messages and bumps the generation itself. Each row stays within one
            # prompt budget (so a pass consolidates a whole message, not a
            # sub-chunk — sub-chunking of an over-budget head is covered in
            # test_history_consolidation_bounds.py); it is their NUMBER that
            # overflows the session file and triggers rotation.
            row = "r" * (_CONSOLIDATION_PROMPT_BUDGET_CHARS // 2)
            rows_to_rotate = (_SESSION_MAX_BYTES // len(row)) + 4
            for i in range(rows_to_rotate):
                log.append(KEY, "user", f"{i}:{row}")

        await _off_loop(_plant_and_rotate)
        assert log.get_metadata(KEY)["rotation_generation"] >= 1, "no rotation fired"

        await _off_loop(lambda: log.update_metadata(KEY, {"consolidation_retry_at": 0.0}))
        assert (
            c.retry_eligible(KEY) is True
        ), "a rotation left the session permanently unable to consolidate"

        # The prompt budget puts a bounded number of messages in front of the
        # model per pass. Drive the passes to a fixed point rather than asserting
        # on one: what the rotation released is that the tail CAN be consolidated,
        # and a bounded pass reaches that over successive turns. The pass cap is
        # what proves it terminates. Progress is the durable position advancing —
        # the message marker, or (for an over-budget head) the sub-offset.
        def _position() -> tuple:
            m = log.get_metadata(KEY)
            return (
                int(m.get("last_consolidated", 0) or 0),
                int(m.get("consolidation_sub_offset", 0) or 0),
            )

        passes = 0
        with patch.object(
            c, "_call_llm", AsyncMock(return_value={"history_entry": "after rotation"})
        ):
            while log.unconsolidated_count(KEY) and passes < 400:
                before = _position()
                await c._consolidate(KEY, include_history=True)
                assert _position() > before, "a pass advanced nothing"
                passes += 1

        assert log.unconsolidated_count(KEY) == 0, "post-rotation content never consolidated"
        assert "consolidation_attempts" not in log.get_metadata(KEY)


class TestARotationDuringTheTurnStampsTheAttemptedSpan:
    """The charge must describe what the turn attempted, not what it returns to.

    The billed call is the whole point of the accounting, and the transcript is
    live while it is in flight — a rotation can land between the pre-turn
    snapshot and the failure charge. Re-reading the metadata line at charge time
    stamps the counter with the NEW generation, so the counter claims to have
    measured content no LLM has seen. At the cap that content is refused with its
    own identity already on the stamp, and nothing later can release it.
    """

    @staticmethod
    def _rotating_failure(log):
        """An LLM turn that rotates the transcript, then fails."""

        async def _turn(*_a, **_kw):
            def _rotate() -> None:
                # Blow the byte budget so the real _maybe_rotate archives the
                # attempted messages and bumps the generation mid-turn.
                for i in range(5):
                    log.append(KEY, "user", f"{i}" * _OVER_CAP_ROW_CHARS)

            await _off_loop(_rotate)
            assert (
                log.get_metadata(KEY)["rotation_generation"] >= 1
            ), "premise broken: no rotation fired during the turn"
            return None

        return AsyncMock(side_effect=_turn)

    @pytest.mark.asyncio
    async def test_the_charge_carries_the_pre_turn_generation(self, tmp_path):
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)

        with patch.object(c, "_call_llm", self._rotating_failure(log)):
            await c._consolidate(KEY, include_history=True)

        meta = log.get_metadata(KEY)
        assert meta["consolidation_attempts"] == 1, "the failed turn was not charged"
        assert meta["rotation_generation"] >= 1, "premise broken: no rotation landed"
        assert meta["consolidation_attempts_generation"] == 0, (
            "the charge was stamped with the generation the rotation produced, "
            "so it claims to have measured content the turn never sent"
        )

    @pytest.mark.asyncio
    async def test_the_rotated_span_does_not_inherit_the_charge(self, tmp_path):
        """The consequence: retained messages must start with their own budget.

        A charge stamped with the post-rotation identity reads back as belonging
        to the retained content, so that content is short a turn of budget before
        it has been attempted even once — and the cap abandons it early.
        """
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)

        with patch.object(c, "_call_llm", self._rotating_failure(log)):
            await c._consolidate(KEY, include_history=True)

        total = _total(log)
        meta = log.get_metadata(KEY)
        assert meta["consolidation_attempts"] == 1, "the failed turn was not charged"
        assert log.unconsolidated_count(KEY) > 0, (
            "premise broken: nothing is left pending after the rotation, so "
            "there is no span to strand"
        )
        assert total <= meta["consolidation_attempts_count"], (
            "premise broken: the transcript grew past the attempted extent, so "
            "the growth test would release the charge on its own and this would "
            "pass with the generation stamp wrong"
        )
        assert log.consolidation_retry_state(KEY, total)[0] == 0, (
            "the post-rotation messages carry a charge that was never spent on "
            "them, so their own budget is short and the cap abandons them early"
        )


class TestForeignWritersCannotEraseTheAccounting:
    """The accounting shares the metadata line with other layers' writers.

    A writer that REBUILDS that line from its own state deletes every field it
    does not enumerate. Two already did, and each silently reset the backoff so
    billed retries resumed. Preservation is now the default: a rebuilder names the
    keys it owns and carries the rest through.
    """

    @pytest.mark.asyncio
    async def test_a_compaction_preserves_the_retry_accounting(self, tmp_path):
        log = _seed_log(tmp_path)
        deadline = time.time() + 3600

        def _write() -> None:
            log.update_metadata(
                KEY,
                {
                    "consolidation_attempts": 3,
                    "consolidation_retry_at": deadline,
                    "consolidation_attempts_generation": 0,
                    "consolidation_attempts_offset": 0,
                    "title": "kept",
                },
            )
            log.rewrite_session(KEY, log._read_messages(KEY)[-1:])

        await _off_loop(_write)

        attempts, retry_at = log.consolidation_retry_state(KEY)
        assert attempts == 3, "a compaction reset the backoff and re-billed the span"
        assert retry_at == pytest.approx(deadline, abs=1)
        assert log.get_metadata(KEY)["title"] == "kept"

    def test_a_dashboard_slot_save_preserves_the_retry_accounting(self, tmp_path, monkeypatch):
        """The save rebuilds the whole metadata line from the slot's own state."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        log = ConversationLog(base_dir=tmp_path)
        log.init()
        for i in range(3):
            log.append("dashboard:chat1", "user", f"m{i}")
        deadline = time.time() + 3600
        log.update_metadata(
            "dashboard:chat1",
            {
                "consolidation_attempts": 4,
                "consolidation_retry_at": deadline,
                "consolidation_attempts_generation": 0,
                "consolidation_attempts_offset": 0,
                "rotation_generation": 2,
            },
        )

        state = _dashboard_state(log)
        slot = _rehydrate_slot_from_history(state, "chat1")
        assert slot is not None
        slot._dirty = True
        _save_slot_to_history(state, slot)

        meta = log.get_metadata("dashboard:chat1")
        assert meta.get("consolidation_attempts") == 4, (
            "a dashboard slot save erased the retry accounting, resuming billed "
            "retries on a span that already spent its budget"
        )
        assert meta.get("consolidation_retry_at") == pytest.approx(deadline, abs=1)
        assert meta.get("consolidation_attempts_generation") == 0
        assert meta.get("rotation_generation") == 2

    def test_a_slot_owned_field_is_still_cleared_by_omission(self, tmp_path, monkeypatch):
        """Preserving unowned keys must not make the slot's own state unclearable."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        log = ConversationLog(base_dir=tmp_path)
        log.init()
        log.append("dashboard:chat1", "user", "m0")
        log.update_metadata("dashboard:chat1", {"pinned": True, "consolidation_attempts": 1})

        state = _dashboard_state(log)
        slot = _rehydrate_slot_from_history(state, "chat1")
        assert slot is not None
        slot.pinned = False
        slot._dirty = True
        _save_slot_to_history(state, slot)

        meta = log.get_metadata("dashboard:chat1")
        assert "pinned" not in meta, "an un-pinned slot could not clear its pin"
        assert meta.get("consolidation_attempts") == 1

    def test_the_helper_never_shadows_an_owned_key(self):
        rebuilt = {"title": "new"}
        existing = {"title": "old", "consolidation_attempts": 2, "rotation_generation": 1}
        out = history_mod.carry_unowned_metadata(rebuilt, existing, frozenset({"title"}))
        assert out == {
            "title": "new",
            "consolidation_attempts": 2,
            "rotation_generation": 1,
        }


class TestAnEditedTranscriptEarnsAFreshBudget:
    """An edit swaps in content no consolidation turn read, so it advances the
    session's content identity — and that ONE counter carries both guarantees.

    Preserving the accounting across a REBUILD is right; letting it (or a marker
    written by a turn already in flight) apply to EDITED content is not. A
    regenerate replaces the assistant tail with a reply the failing turns never
    saw, and it lands at the same message count, the same marker and the same
    extent — so nothing but the rotation generation distinguishes it, and without
    the bump a capped span would strand brand-new content forever while an
    in-flight attempt would mark it consolidated unread.
    """

    def _plant_capped_slot(
        self, tmp_path, monkeypatch, retry_in: float = 3600.0
    ) -> ConversationLog:
        """A two-message dashboard transcript whose span sits at the cap.

        *retry_in* places the armed backoff deadline relative to now — negative
        for a span whose wait has already elapsed (so eligibility turns purely on
        the budget), positive for one still serving it.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        log = ConversationLog(base_dir=tmp_path)
        log.init()
        log.append("dashboard:chat1", "user", "ask")
        log.append("dashboard:chat1", "assistant", "first answer")
        log.update_metadata(
            "dashboard:chat1",
            {
                "consolidation_attempts": _CONSOLIDATION_MAX_ATTEMPTS,
                "consolidation_retry_at": time.time() + retry_in,
                "consolidation_attempts_generation": 0,
                "consolidation_attempts_offset": 0,
                "consolidation_attempts_count": log.consolidation_counts("dashboard:chat1")[0],
            },
        )
        return log

    def _regenerate(self, state, slot) -> None:
        """What the regenerate path does: swap the assistant tail for a different
        reply and persist that window as an explicit snapshot (a rewrite)."""
        snapshot = list(slot.messages)
        snapshot[-1] = {**snapshot[-1], "content": "regenerated answer"}
        slot.messages[:] = snapshot
        slot._dirty = True
        _save_slot_to_history(state, slot, snapshot)

    def test_a_regenerated_reply_is_not_held_by_the_old_spans_cap(self, tmp_path, monkeypatch):
        log = self._plant_capped_slot(tmp_path, monkeypatch, retry_in=-1.0)
        before = log.consolidation_counts("dashboard:chat1")[0]
        c = _make_consolidator(log)
        assert not c.retry_eligible("dashboard:chat1", message_count=before), (
            "premise broken: the span is not capped, so the edit has nothing to "
            "release and this would pass without the fix"
        )

        state = _dashboard_state(log)
        slot = _rehydrate_slot_from_history(state, "chat1")
        assert slot is not None
        self._regenerate(state, slot)

        after = log.consolidation_counts("dashboard:chat1")[0]
        meta = log.get_metadata("dashboard:chat1")
        assert after == before, (
            "premise broken: the rewrite changed the message total, so the growth "
            "test would release the charge on its own"
        )
        assert int(meta.get("last_consolidated", 0) or 0) == 0, (
            "premise broken: the rewrite moved the marker, which releases the " "charge on its own"
        )
        assert meta.get("consolidation_attempts") == _CONSOLIDATION_MAX_ATTEMPTS, (
            "premise broken: the accounting was dropped outright, so this passes "
            "without the span identity having moved"
        )
        assert "regenerated answer" in log._path("dashboard:chat1").read_text(
            encoding="utf-8"
        ), "premise broken: the replacement reply never reached disk"

        assert log.consolidation_retry_state("dashboard:chat1", after)[0] == 0, (
            "the replacement reply inherited the exhausted budget of the span it " "replaced"
        )
        assert c.retry_eligible("dashboard:chat1", message_count=after), (
            "a reply no consolidation turn has ever read is permanently "
            "ineligible for consolidation"
        )

    def test_the_edit_advances_the_sessions_content_identity(self, tmp_path, monkeypatch):
        """The release above is the span-identity semantics, not a special case."""
        log = self._plant_capped_slot(tmp_path, monkeypatch)
        state = _dashboard_state(log)
        slot = _rehydrate_slot_from_history(state, "chat1")
        assert slot is not None
        before = log.rotation_generation("dashboard:chat1")
        self._regenerate(state, slot)
        assert log.rotation_generation("dashboard:chat1") == before + 1, (
            "the edit left the content identity untouched, so a consolidation "
            "holding the pre-edit generation cannot tell its span was replaced"
        )

    def test_an_edit_does_not_buy_a_free_billed_turn(self, tmp_path, monkeypatch):
        """A fresh budget is not an immediate turn — same as a rotation.

        Releasing the deadline too would let a user hammering regenerate re-bill a
        failing consolidation on every gesture, which is exactly what the backoff
        exists to stop.
        """
        log = self._plant_capped_slot(tmp_path, monkeypatch, retry_in=3600.0)
        armed = log.get_metadata("dashboard:chat1")["consolidation_retry_at"]
        state = _dashboard_state(log)
        slot = _rehydrate_slot_from_history(state, "chat1")
        assert slot is not None
        self._regenerate(state, slot)

        count = log.consolidation_counts("dashboard:chat1")[0]
        attempts, retry_at = log.consolidation_retry_state("dashboard:chat1", count)
        assert attempts == 0, "the edit did not release the exhausted budget"
        assert retry_at == pytest.approx(
            armed, abs=1
        ), "the edit discarded the armed backoff deadline"
        assert not _make_consolidator(log).retry_eligible(
            "dashboard:chat1", message_count=count
        ), "an edit let the session skip a backoff it had not served"

    def test_an_edit_invalidates_an_attempt_already_in_flight(self, tmp_path, monkeypatch):
        """The completion write of a turn that snapshotted the PRE-edit span must
        not mark the replacement tail consolidated.

        The turn read the original reply, the user regenerated it, and only then
        did the turn finish. Its offset still fits the file — the count, the
        marker and the extent are all unchanged — so nothing but the advanced
        generation stops ``mark_consolidated`` from marking a reply no LLM has
        ever seen as already extracted.
        """
        log = self._plant_capped_slot(tmp_path, monkeypatch)
        key = "dashboard:chat1"
        # The consolidation turn starts: one atomic pre-turn snapshot.
        _msgs, total, generation, _sub = log.snapshot_for_consolidation(key)
        assert total == 2 and generation == 0

        state = _dashboard_state(log)
        slot = _rehydrate_slot_from_history(state, "chat1")
        assert slot is not None
        self._regenerate(state, slot)
        assert log.consolidation_counts(key)[0] == total, (
            "premise broken: the edit changed the message count, so the offset "
            "fallback in mark_consolidated would catch this without the fix"
        )

        # The turn now completes and writes the marker for the span it read.
        log.mark_consolidated(key, total, generation)

        meta = log.get_metadata(key)
        assert int(meta.get("last_consolidated", 0) or 0) == 0, (
            "the regenerated reply was marked consolidated without ever being "
            "extracted — silent memory loss"
        )
        assert (
            log.consolidation_counts(key)[1] == total
        ), "the replacement content is not queued for consolidation"

    def test_a_steady_flush_leaves_the_content_identity_alone(self, tmp_path, monkeypatch):
        """Only an EDIT advances it; a re-serialization of the same window is not
        evidence about content, or every flush would release the cap."""
        log = self._plant_capped_slot(tmp_path, monkeypatch, retry_in=-1.0)
        state = _dashboard_state(log)
        slot = _rehydrate_slot_from_history(state, "chat1")
        assert slot is not None
        slot._dirty = True
        _save_slot_to_history(state, slot)

        count = log.consolidation_counts("dashboard:chat1")[0]
        assert log.rotation_generation("dashboard:chat1") == 0
        assert (
            log.consolidation_retry_state("dashboard:chat1", count)[0]
            == _CONSOLIDATION_MAX_ATTEMPTS
        ), "a steady flush released the cap, so a failing span retries forever"

    def test_the_helper_preserves_a_foreign_field_unconditionally(self):
        existing = {
            "consolidation_attempts": 3,
            "consolidation_retry_at": 123.0,
            "rotation_generation": 1,
            "title": "kept",
        }
        assert history_mod.carry_unowned_metadata({}, existing, frozenset()) == existing


class TestTheCapDoesNotOutliveTheSpanItMeasured:
    """The cap is reachable only when the abandon-marker write itself failed.

    Refusing that span is correct; refusing the SESSION is not. Appended messages
    leave the generation and the marker untouched, so without the span's extent
    one transient write failure would reject the transcript forever and the
    session's history would never be consolidated again.
    """

    async def _plant_capped(self, log, count: int | None = None, retry_at: float = -1.0):
        """A span charged to the cap whose abandon-marker write did not land."""
        await _off_loop(
            lambda: log.update_metadata(
                KEY,
                {
                    "consolidation_attempts": _CONSOLIDATION_MAX_ATTEMPTS,
                    "consolidation_retry_at": (
                        time.time() + retry_at if retry_at > 0 else time.time() - 1
                    ),
                    "consolidation_attempts_generation": 0,
                    "consolidation_attempts_offset": 0,
                    "consolidation_attempts_count": (_total(log) if count is None else count),
                },
            )
        )

    @pytest.mark.asyncio
    async def test_a_charged_attempt_records_the_spans_extent(self, tmp_path):
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)

        with patch.object(c, "_call_llm", AsyncMock(return_value=None)):
            await c._consolidate(KEY, include_history=True)

        assert log.get_metadata(KEY)["consolidation_attempts_count"] == 3

    @pytest.mark.asyncio
    async def test_the_extent_is_the_attempted_count_not_the_current_size(self, tmp_path):
        """A message arriving DURING the failing turn must not be swallowed.

        It was never sent to the provider, so recording the post-turn size would
        bury it inside the charged extent and the growth test would never fire for
        it — at the cap it would be excluded from consolidation permanently.
        """
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)

        async def _append_then_fail(_prompt, *, memory_store: str = "", session_key: str = ""):
            await _off_loop(lambda: log.append(KEY, "user", "arrived mid-turn"))
            return None

        with patch.object(c, "_call_llm", AsyncMock(side_effect=_append_then_fail)):
            await c._consolidate(KEY, include_history=True)

        assert log.get_metadata(KEY)["consolidation_attempts_count"] == 3, (
            "the mid-turn message was recorded as attempted, so growth can never " "release it"
        )
        # It reads as growth, so the counter does not describe this span. (The
        # armed deadline still applies — a fresh budget is not a free turn.)
        assert log.consolidation_retry_state(KEY, _total(log))[0] == 0
        assert _total(log) == 4

    @pytest.mark.asyncio
    async def test_a_capped_span_with_no_growth_stays_ineligible(self, tmp_path):
        """An unchanged failing span cannot burn forever."""
        log = _seed_log(tmp_path)
        await self._plant_capped(log)

        assert log.consolidation_retry_state(KEY, _total(log))[0] == (_CONSOLIDATION_MAX_ATTEMPTS)
        assert _eligible(_make_consolidator(log), log) is False

    @pytest.mark.asyncio
    async def test_transcript_growth_releases_the_cap(self, tmp_path):
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)
        await self._plant_capped(log)
        assert _eligible(c, log) is False

        await _off_loop(lambda: log.append(KEY, "user", "arrived after the cap"))

        assert log.consolidation_retry_state(KEY, _total(log))[0] == 0, (
            "one failed abandon write refused every message the session will " "ever write again"
        )
        assert _eligible(c, log) is True

    @pytest.mark.asyncio
    async def test_growth_still_waits_out_the_armed_backoff(self, tmp_path):
        """A fresh budget is not a free immediate turn."""
        log = _seed_log(tmp_path)
        await self._plant_capped(log, retry_at=3600.0)
        await _off_loop(lambda: log.append(KEY, "user", "arrived after the cap"))

        assert log.consolidation_retry_state(KEY, _total(log))[0] == 0
        assert _eligible(_make_consolidator(log), log) is False

    @pytest.mark.asyncio
    async def test_the_grown_span_re_arms_the_cap(self, tmp_path):
        """Growth buys ONE bounded budget, not an escape from the cap."""
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)
        await self._plant_capped(log)
        await _off_loop(lambda: log.append(KEY, "user", "arrived after the cap"))
        assert _eligible(c, log) is True

        def _rearm() -> None:
            for _ in range(_CONSOLIDATION_MAX_ATTEMPTS):
                log.record_consolidation_failure(KEY, 900.0, 86400.0, _span(log))
                log.update_metadata(KEY, {"consolidation_retry_at": time.time() - 1})

        await _off_loop(_rearm)

        meta = log.get_metadata(KEY)
        assert meta["consolidation_attempts"] == _CONSOLIDATION_MAX_ATTEMPTS
        assert meta["consolidation_attempts_count"] == 4, (
            "the cap re-armed against the OLD extent, so the same growth would " "release it again"
        )
        assert log.consolidation_retry_state(KEY, _total(log))[0] == (_CONSOLIDATION_MAX_ATTEMPTS)
        assert _eligible(c, log) is False, "the original failing prefix can re-bill indefinitely"

    @pytest.mark.asyncio
    async def test_a_shrink_alone_does_not_release_the_cap(self, tmp_path):
        """Growth is `>`, not `!=` — a rewrite that only drops content adds none."""
        log = _seed_log(tmp_path)
        await self._plant_capped(log, count=99)

        assert log.consolidation_retry_state(KEY, _total(log))[0] == (_CONSOLIDATION_MAX_ATTEMPTS)
        assert _eligible(_make_consolidator(log), log) is False

    @pytest.mark.asyncio
    async def test_eligibility_never_reads_the_transcript(self, tmp_path):
        """It runs on the gateway loop; a full-file read there stalls everything."""
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)
        await self._plant_capped(log)
        total = _total(log)

        reads: list[str] = []
        real_read_text = Path.read_text

        def _counting_read_text(self_path, *a, **kw):
            if self_path == log._path(KEY):
                reads.append(str(self_path))
            return real_read_text(self_path, *a, **kw)

        with patch.object(Path, "read_text", _counting_read_text):
            c.retry_eligible(KEY, message_count=total)
            log.consolidation_retry_state(KEY, total)

        assert reads == [], "the eligibility check read the whole transcript on the event loop"

    @pytest.mark.asyncio
    async def test_content_written_after_a_failed_abandon_consolidates(self, tmp_path):
        """End to end through a real entry point, which is what consults the gate.

        Driving ``_consolidate`` directly would prove nothing here: the cap lives
        in ``retry_eligible``, so only a caller that passes through it can show the
        session recovering.
        """
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)
        await self._plant_capped(log)

        with patch.object(c, "_call_llm", AsyncMock(return_value={"history_entry": "x"})) as spy:
            c.consolidate_session(KEY)
            # flake-ok: pre-existing unbounded gather of scheduled tasks, moved verbatim
            await asyncio.gather(*list(c._tasks), return_exceptions=True)
        assert spy.await_count == 0, "a capped span with no growth still fired a turn"

        await _off_loop(lambda: log.append(KEY, "user", "arrived after the cap"))

        with patch.object(c, "_call_llm", AsyncMock(return_value={"history_entry": "recovered"})):
            c.consolidate_session(KEY)
            # flake-ok: pre-existing unbounded gather of scheduled tasks, moved verbatim
            await asyncio.gather(*list(c._tasks), return_exceptions=True)

        assert log.unconsolidated_count(KEY) == 0, (
            "one failed abandon write left the session permanently unable to "
            "consolidate anything it writes from now on"
        )
        assert "consolidation_attempts" not in log.get_metadata(KEY)
