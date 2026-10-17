"""Which locks a dashboard turn holds, when, and that it always gives them back.

A turn takes the session-switch lock (``slot_switch_session_lock`` on its
session key) from binding capture through a COLD session claim, so a model switch
on another alias of the same session cannot interleave with the registration; it
drops the lock before a WARM claim (a registered session is already visible to
the switch handlers, and waiting for its lease under the lock would deadlock with
the turn that holds the lease) and before it streams (the refusal and access
fallbacks take the same non-reentrant lock mid-turn). The model-access fallback
takes the session lock, then the slot's ``_model_pick_lock``, the order every
switch path uses.

These drive real turns through ``turn_harness.run_turn`` and read the lock only on
the turn's own loop (the lock cache is per loop), from inside the session
manager's answers, the provider's methods and ``Do`` steps. They pin what the
turn does with the locks, not where the runner acquires them.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from turn_harness import (
    Do,
    Raise,
    ScriptedProvider,
    TurnContext,
    TurnRecord,
    TurnScript,
    run_turn,
)

from kiro_crew.acp.transport_errors import AcpError
from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_TEXT_CHUNK,
    EVENT_TOOL_CALL,
    STOP_REASON_END_TURN,
    AcpEvent,
)
from kiro_crew.dashboard.chat_utils import effective_session_key
from kiro_crew.llm_helpers import slot_switch_session_lock
from kiro_crew.session_allocation import SessionBusyError

_REPLY = AcpEvent(kind=EVENT_TEXT_CHUNK, text="the reply")
_DONE = AcpEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)
_TOOL = AcpEvent(kind=EVENT_TOOL_CALL, tool_call_id="t1", title="read_file", tool_kind="read")


class _Locks:
    """Reads of the session-switch lock at the points a turn passes through."""

    def __init__(self) -> None:
        self.lock: asyncio.Lock | None = None
        self.claims: list[tuple[bool, Any]] = []
        self.seen: list[tuple[str, bool]] = []
        self.picks: list[bool] = []

    def held(self) -> bool:
        assert self.lock is not None
        return self.lock.locked()

    def install(self, ctx: TurnContext, *, claim_error: BaseException | None = None) -> None:
        self.lock = slot_switch_session_lock(effective_session_key(ctx.slot))
        sessions = ctx.state.sessions
        claim = sessions.get_or_create.side_effect
        failures = [claim_error] if claim_error is not None else []

        async def _claim(*args: Any, **kwargs: Any) -> Any:
            self.claims.append((self.held(), kwargs.get("wait_if_busy")))
            if failures:
                raise failures.pop()
            return await claim(*args, **kwargs)

        sessions.get_or_create.side_effect = _claim
        begin = sessions.begin_turn.side_effect

        def _begin(*args: Any, **kwargs: Any) -> Any:
            self.seen.append(("begin_turn", self.held()))
            return begin(*args, **kwargs)

        sessions.begin_turn.side_effect = _begin
        frames = ctx.state.broadcast_ws

        def _frame(kind: str, payload: Any = None) -> Any:
            if kind == "chat_done":
                self.seen.append(("chat_done", self.held()))
            return frames(kind, payload)

        ctx.state.broadcast_ws = _frame

    def mid_stream(self, ctx: TurnContext) -> None:
        self.seen.append(("stream", self.held()))


class _PickLock(asyncio.Lock):
    """The slot's model-pick lock, noting whether the session lock was held first."""

    def __init__(self, locks: _Locks) -> None:
        super().__init__()
        self._locks = locks

    async def acquire(self) -> bool:
        self._locks.picks.append(self._locks.held())
        return await super().acquire()


class _SwappableProvider(ScriptedProvider):
    """A provider that can be moved to another model, noting the locks it saw."""

    async def set_model(self, model: str) -> bool:
        slot = self._ctx.slot
        held = slot_switch_session_lock(effective_session_key(slot)).locked()
        self.record("set_model", model, held, slot._model_pick_lock.locked())
        return True


def _access_rejection(model: str, advertised: list[str]) -> AcpError:
    """A prompt-time entitlement refusal, tagged as the ACP client tags it."""
    exc = AcpError(f"Your account does not have access to model '{model}'.", transient=False)
    exc.rejected_model = model  # type: ignore[attr-defined]
    exc.advertised = list(advertised)  # type: ignore[attr-defined]
    return exc


def _releases(record: TurnRecord) -> int:
    return [call.name for call in record.session_calls].count("release")


class TestTheDispatchLock:
    @pytest.mark.asyncio
    async def test_turn_locks_cold_claim_holds_the_lock_and_the_stream_never_does(
        self,
    ) -> None:
        locks = _Locks()
        record = await run_turn(
            TurnScript(
                events=[
                    Do(locks.mid_stream),
                    _REPLY,
                    Do(locks.mid_stream),
                    _TOOL,
                    Do(locks.mid_stream),
                    _REPLY,
                    _DONE,
                ],
                setup=locks.install,
            )
        )

        # Held across the cold claim, never waiting on a lease under it.
        assert locks.claims == [(True, False)]
        assert locks.seen == [
            ("begin_turn", False),
            ("stream", False),
            ("stream", False),
            ("stream", False),
            ("chat_done", False),
        ]
        assert locks.held() is False
        assert record.stop_reason == STOP_REASON_END_TURN
        assert _releases(record) == 1

    @pytest.mark.asyncio
    async def test_turn_locks_warm_claim_waits_for_the_lease_without_the_lock(self) -> None:
        locks = _Locks()

        def _registered(ctx: TurnContext) -> None:
            locks.install(ctx)
            ctx.state.sessions.has_session.side_effect = lambda _key: True

        record = await run_turn(
            TurnScript(events=[Do(locks.mid_stream), _REPLY, _DONE], setup=_registered)
        )

        assert locks.claims == [(False, True)]
        assert ("stream", False) in locks.seen
        assert locks.held() is False
        assert record.stop_reason == STOP_REASON_END_TURN

    @pytest.mark.asyncio
    async def test_turn_locks_busy_cold_claim_retries_with_the_lease_wait_unlocked(
        self,
    ) -> None:
        locks = _Locks()
        record = await run_turn(
            TurnScript(
                events=[Do(locks.mid_stream), _REPLY, _DONE],
                setup=lambda ctx: locks.install(ctx, claim_error=SessionBusyError("busy")),
            )
        )

        # The immediate claim under the lock found the session held; the lock is
        # dropped before the claim that waits for the lease.
        assert locks.claims == [(True, False), (False, True)]
        assert locks.seen[0] == ("begin_turn", False)
        assert locks.held() is False
        assert [row["content"] for row in record.rows("assistant")] == ["the reply"]
        assert record.stop_reason == STOP_REASON_END_TURN
        assert _releases(record) == 1

    @pytest.mark.asyncio
    async def test_turn_locks_claim_that_raises_still_frees_the_lock(self) -> None:
        locks = _Locks()
        record = await run_turn(
            TurnScript(
                events=[_REPLY, _DONE],
                setup=lambda ctx: locks.install(ctx, claim_error=RuntimeError("claim failed")),
            )
        )

        assert locks.claims == [(True, False)]
        # Free by the time the cycle ends, and after the run.
        assert locks.seen == [("chat_done", False)]
        assert locks.held() is False
        [card] = record.rows("error")
        assert "claim failed" in card["content"]
        assert record.calls("stream") == []
        assert _releases(record) == 0


class TestTheFallbackLockOrder:
    @pytest.mark.asyncio
    async def test_turn_locks_access_fallback_takes_session_lock_then_pick_lock(self) -> None:
        locks = _Locks()

        def _observed(ctx: TurnContext) -> None:
            locks.install(ctx)
            ctx.slot._model_pick_lock = _PickLock(locks)

        record = await run_turn(
            TurnScript(
                events=[Raise(_access_rejection("auto", ["model-b", "model-c"]))],
                provider=_SwappableProvider,
                setup=_observed,
            )
        )

        # The pick lock is entered once, with the session lock already held...
        assert locks.picks == [True]
        # ...and the swap, to one of the advertised models, runs under both.
        [swap] = record.calls("set_model")
        assert swap.args[0] in ("model-b", "model-c")
        assert swap.args[1:] == (True, True)
        assert locks.held() is False
        assert [row["role"] for row in record.window if row["role"] == "notice"] == ["notice"]
        [successor] = record.successors
        assert successor.args[0] == "hello"
        assert successor.kwargs.get("_replay")
        assert _releases(record) == 1
