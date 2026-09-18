"""The turn actor must be bound before anything that can queue a recovery.

``_queue_recovery`` stamps the turn's actor onto the entry it requeues, and the
handlers that call it also serve failures raised during PRE-TURN setup -- before
the dispatch block that refines the actor has run. A local read before its
assignment raises ``UnboundLocalError`` from inside the handler, which buries the
failure the handler exists to recover from.

These tests drive the real ``_run_chat`` and make setup fail, so the recovery
path runs for real rather than being reasoned about.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state

from kiro_crew.acp.client import AcpError, AcpProcessDied
from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.chat_runner import TURN_ACTOR_META_KEY, _run_chat


def _requeued(slot) -> list[dict]:
    """The rows the recovery path put back, still carrying their stamp.

    A requeue at index 0 of an idle slot is consumed straight into the transcript
    as an ``inject`` row rather than sitting on the queue, and the row keeps the
    ``meta`` it was inserted with -- which is the stamp under test.
    """
    return [m for m in slot.messages if m.get("role") == "inject"]


def _state_and_slot(tmp_path: Path, name: str = "early-recovery-slot"):
    state = _make_state(tmp_path)
    state.sessions.get_or_create = AsyncMock(return_value=(MagicMock(), False, False))
    state.sessions.release = MagicMock()
    state.sessions.reset = AsyncMock()
    state.sessions.set_approval_policy = MagicMock()
    state.sessions.check_context_usage = MagicMock()
    state.sessions.get_slack_link = MagicMock(return_value=(None, None))
    state.sessions.record_failure = AsyncMock()
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    state.is_yolo_active = MagicMock(return_value=False)
    state._background_tasks = set()
    slot = state.get_or_create_slot(name)
    slot.append("user", "hello", "msg msg-u")
    client = state.sessions.get_or_create.return_value[0]
    client.shutdown = AsyncMock()
    return state, slot, client


class TestSetupFailureReachesRecoveryWithoutCrashing:
    """The failures that reach a requeue are ACP-specific, so these raise those.

    ``get_or_create`` runs inside the guarded region and well before the dispatch
    block, so a backend that dies there is the real shape of "the turn failed
    before its actor was refined".
    """

    @pytest.mark.asyncio
    async def test_a_backend_that_dies_during_setup_still_requeues(self, tmp_path, caplog) -> None:
        state, slot, _client = _state_and_slot(tmp_path)
        state.sessions.get_or_create = AsyncMock(
            side_effect=AcpProcessDied("backend died during setup")
        )
        with caplog.at_level("DEBUG"):
            await _run_chat(state, slot, "test message")

        queued = _requeued(slot)
        assert "UnboundLocalError" not in caplog.text
        assert queued, "the failure never reached a requeue -- the test proves nothing"
        assert all(
            isinstance(item.get("meta"), dict) for item in queued
        ), "a requeued entry carries no meta to stamp"
        assert all(TURN_ACTOR_META_KEY in item["meta"] for item in queued)

    @pytest.mark.asyncio
    async def test_a_prompt_error_during_setup_reports_itself_not_a_name_error(
        self, tmp_path, caplog
    ) -> None:
        """This handler reports rather than requeues, so the property is narrower:
        what surfaces is the ACP error, not a crash inside the handler that
        replaced it."""
        state, slot, _client = _state_and_slot(tmp_path)
        state.sessions.get_or_create = AsyncMock(side_effect=AcpError("prompt refused"))
        with caplog.at_level("DEBUG"):
            await _run_chat(state, slot, "test message")

        assert "UnboundLocalError" not in caplog.text
        assert "prompt refused" in caplog.text

    @pytest.mark.asyncio
    async def test_the_requeued_entry_carries_the_actor_bound_at_entry(
        self, tmp_path, caplog
    ) -> None:
        """A cron turn that dies in setup is retried AS a cron turn."""
        state, slot, _client = _state_and_slot(tmp_path)
        state.sessions.get_or_create = AsyncMock(side_effect=AcpProcessDied("died"))
        with caplog.at_level("DEBUG"):
            await _run_chat(state, slot, "test message", _turn_actor="cron")

        queued = _requeued(slot)
        assert "UnboundLocalError" not in caplog.text
        assert queued, "the failure never reached a requeue -- the test proves nothing"
        assert queued[0].get("meta", {}).get(TURN_ACTOR_META_KEY) == "cron"

    @pytest.mark.asyncio
    async def test_a_self_wake_whose_cold_start_dies_is_not_retried_as_a_user_turn(
        self, tmp_path, caplog
    ) -> None:
        """The narrow case a late refinement gets wrong.

        A self-wake turn names no ``_turn_actor`` -- the flag is what identifies it
        -- and the ACP cold start it can die in sits between the binding and the
        dispatch block far below. So the actor has to be complete AT the binding, or
        this recovery is stamped ``user`` and an autonudge's retry is recorded as a
        person's message.
        """
        state, slot, _client = _state_and_slot(tmp_path)
        state.sessions.get_or_create = AsyncMock(side_effect=AcpProcessDied("cold start died"))

        with caplog.at_level("DEBUG"):
            await _run_chat(state, slot, "nudge", _directive_self_wake=True)

        queued = _requeued(slot)
        assert "UnboundLocalError" not in caplog.text
        assert queued, "the failure never reached a requeue -- the test proves nothing"
        assert queued[0].get("meta", {}).get(TURN_ACTOR_META_KEY) == "autonudge"

    @pytest.mark.asyncio
    async def test_a_named_dispatch_actor_outranks_the_self_wake_flag(
        self, tmp_path, caplog
    ) -> None:
        # The flag says how the turn was woken; the argument says who it belongs to.
        # A dispatch that named an actor and also set the flag is that actor's turn.
        state, slot, _client = _state_and_slot(tmp_path)
        state.sessions.get_or_create = AsyncMock(side_effect=AcpProcessDied("died"))

        with caplog.at_level("DEBUG"):
            await _run_chat(state, slot, "msg", _turn_actor="cron", _directive_self_wake=True)

        queued = _requeued(slot)
        assert queued
        assert queued[0].get("meta", {}).get(TURN_ACTOR_META_KEY) == "cron"


class TestTheBindingIsStructural:
    """A dynamic test covers the paths it drives; this covers every other one.

    The recovery handlers sit thousands of lines below the assignment, so the
    invariant that matters is positional: the actor is bound before the try whose
    handlers requeue.
    """

    def test_the_actor_is_bound_before_any_try_that_queues_a_recovery(self) -> None:
        fn = next(
            node
            for node in ast.walk(ast.parse(inspect.getsource(chat_runner)))
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_run_chat"
        )
        bound_at = min(
            target.lineno
            for stmt in ast.walk(fn)
            if isinstance(stmt, ast.Assign)
            for target in stmt.targets
            if isinstance(target, ast.Name) and target.id == "_ledger_actor"
        )

        def _queues_recovery(node: ast.AST) -> bool:
            return any(
                isinstance(call, ast.Call) and getattr(call.func, "id", "") == "_queue_recovery"
                for call in ast.walk(node)
            )

        guarded: list[int] = []
        for node in ast.walk(fn):
            if not isinstance(node, ast.Try):
                continue
            handlers: list[ast.AST] = list(node.handlers)
            if node.finalbody:
                handlers.append(ast.Module(body=node.finalbody, type_ignores=[]))
            if any(_queues_recovery(h) for h in handlers):
                guarded.append(node.body[0].lineno)

        assert guarded, "no try block queues a recovery -- has the path moved?"
        assert bound_at < min(guarded), (
            f"_ledger_actor is bound at line {bound_at}, after a recovery-queuing "
            f"try that starts at line {min(guarded)}: a failure before the "
            f"assignment would read it unbound"
        )
