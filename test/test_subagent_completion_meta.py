"""Structured completion facts ride on the message meta, not the prose.

A finished sub-agent's completion is injected as the parent's next turn and
rendered by the dashboard as a card. The card used to recover its header facts
(outcome, tallies, which agent) by re-parsing the English prose the gateway
composed; a reword silently broke rendering with no failing test (#1792).

The gateway now stamps those facts as a structured dict on the injected row's
``meta[SUBAGENT_COMPLETION_META_KEY]``. These tests pin the helper shapes and
prove the queue-drain path carries the meta onto the row (and does not invent it
for a plain user message or a merged turn).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from chat_test_helpers import _make_state

from kiro_crew.constants import SUBAGENT_COMPLETION_META_KEY
from kiro_crew.dashboard.chat_runner import _start_next_queued_turn
from kiro_crew.dashboard.state import (
    SUBAGENT_BATCH_COMPLETION_PREFIX,
    SUBAGENT_COMPLETION_PREFIX,
)
from kiro_crew.subagent_completion_meta import (
    OUTCOME_FAILED,
    OUTCOME_INTERRUPTED,
    OUTCOME_OK,
    OUTCOME_STOPPED,
    single_completion_meta,
    wave_chunk_meta,
    wave_final_meta,
)

SINGLE = (
    f"{SUBAGENT_COMPLETION_PREFIX}\n"
    "Agent `a1` (kirocrew) completed ✅\n"
    "Task: add a label\n"
    "\n"
    "done."
)
WAVE = (
    f"{SUBAGENT_BATCH_COMPLETION_PREFIX}\n"
    "Batch results 1/1 — wave finished: 8 ✅ · 1 ❌ · 0 ⏹ of 9 agents. "
    "All results delivered.\n"
    "This run is complete.\n"
    "\n"
    "— `a1` ✅ first"
)


class TestMetaHelperShapes:
    """The dict shape is a wire contract with subagentCompletion.ts; keep the
    field names and the outcome tokens in lockstep with the ParsedSingleCompletion
    / ParsedBatchCompletion interfaces there."""

    def test_single_carries_outcome_agent_and_task(self) -> None:
        m = single_completion_meta(
            agent_id="a1", outcome=OUTCOME_OK, agent_name="kirocrew", task="add a label"
        )
        assert m == {
            "kind": "single",
            "agentId": "a1",
            "agentName": "kirocrew",
            "outcome": "ok",
            "task": "add a label",
            "note": "",
        }

    def test_single_note_carries_the_only_explanation_on_orphan_shapes(self) -> None:
        m = single_completion_meta(
            agent_id="a1", outcome=OUTCOME_INTERRUPTED, note="orphaned by gateway restart"
        )
        assert m["outcome"] == "interrupted"
        assert m["note"] == "orphaned by gateway restart"

    def test_outcome_tokens_match_the_frontend_union(self) -> None:
        assert (OUTCOME_OK, OUTCOME_FAILED, OUTCOME_STOPPED, OUTCOME_INTERRUPTED) == (
            "ok",
            "failed",
            "stopped",
            "interrupted",
        )

    def test_wave_final_carries_tallies_not_progress(self) -> None:
        m = wave_final_meta(chunk=1, chunks=1, ok=8, failed=1, stopped=0, total=9)
        assert m["kind"] == "batch"
        assert m["final"] is True
        assert (m["ok"], m["failed"], m["stopped"], m["total"]) == (8, 1, 0, 9)
        # delivered/running are implied by final and left for the frontend.
        assert "delivered" not in m and "running" not in m

    def test_wave_chunk_carries_progress_not_tallies(self) -> None:
        m = wave_chunk_meta(chunk=1, chunks=3, delivered=10, total=30, running=20)
        assert m["final"] is False
        assert (m["delivered"], m["total"], m["running"]) == (10, 30, 20)
        assert "ok" not in m and "failed" not in m


class TestDrainStampsMetaOntoRow:
    @pytest.mark.asyncio
    async def test_single_completion_meta_lands_on_the_subagent_row(self, tmp_path) -> None:
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("meta-single")
        slot.queue_append(
            SINGLE,
            meta={
                SUBAGENT_COMPLETION_META_KEY: single_completion_meta(
                    agent_id="a1", outcome=OUTCOME_OK, agent_name="kirocrew", task="add a label"
                )
            },
        )
        with patch("kiro_crew.dashboard.chat_runner.spawn_guarded_turn") as spawn:
            spawn.return_value = MagicMock()
            started = await _start_next_queued_turn(state, slot)

        assert started is True
        row = [m for m in slot.messages if m["role"] == "subagent"][0]
        stamped = row["meta"][SUBAGENT_COMPLETION_META_KEY]
        assert stamped["kind"] == "single"
        assert stamped["agentId"] == "a1"
        assert stamped["outcome"] == "ok"

    @pytest.mark.asyncio
    async def test_wave_digest_meta_lands_on_the_subagent_row(self, tmp_path) -> None:
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("meta-wave")
        slot.queue_append(
            WAVE,
            meta={
                SUBAGENT_COMPLETION_META_KEY: wave_final_meta(
                    chunk=1, chunks=1, ok=8, failed=1, stopped=0, total=9
                )
            },
        )
        with patch("kiro_crew.dashboard.chat_runner.spawn_guarded_turn") as spawn:
            spawn.return_value = MagicMock()
            await _start_next_queued_turn(state, slot)

        row = [m for m in slot.messages if m["role"] == "subagent"][0]
        stamped = row["meta"][SUBAGENT_COMPLETION_META_KEY]
        assert stamped["kind"] == "batch"
        assert stamped["final"] is True
        assert stamped["ok"] == 8 and stamped["failed"] == 1

    @pytest.mark.asyncio
    async def test_a_plain_user_message_gets_no_completion_meta(self, tmp_path) -> None:
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("meta-user")
        slot.queue_append("please fix the bug")
        with patch("kiro_crew.dashboard.chat_runner.spawn_guarded_turn") as spawn:
            spawn.return_value = MagicMock()
            await _start_next_queued_turn(state, slot)

        row = [m for m in slot.messages if m["role"] == "user"][0]
        meta = row.get("meta") or {}
        assert SUBAGENT_COMPLETION_META_KEY not in meta

    @pytest.mark.asyncio
    async def test_meta_is_dropped_when_a_completion_is_merged(self, tmp_path) -> None:
        """Per-entry facts are meaningless once several entries merge under one
        synthetic header, so meta is only attached to a single un-merged system
        injection. Subagent completions never merge in practice (they break a
        user-message merge and drain one at a time); this guards the invariant
        directly by staging a completion carrying meta behind a user message and
        confirming the drained (user) row is unstamped."""
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("meta-merge")
        # A user message drains first (a completion never merges with it), so the
        # first drained row is the user's and must carry no completion meta even
        # though a completion with meta sits behind it in the queue.
        slot.queue_append("do a thing")
        slot.queue_append(
            SINGLE,
            meta={
                SUBAGENT_COMPLETION_META_KEY: single_completion_meta(
                    agent_id="a1", outcome=OUTCOME_OK
                )
            },
        )
        with patch("kiro_crew.dashboard.chat_runner.spawn_guarded_turn") as spawn:
            spawn.return_value = MagicMock()
            await _start_next_queued_turn(state, slot)

        drained = [m for m in slot.messages if m["role"] in ("user", "subagent")][0]
        assert drained["role"] == "user"
        assert SUBAGENT_COMPLETION_META_KEY not in (drained.get("meta") or {})
