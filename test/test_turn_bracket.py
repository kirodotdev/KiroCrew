"""The turn bracket: one turn's re-injection and skill-body settle, through its interface.

``TurnBracket`` consumes the post-compaction re-injection flag per attempt, records
whether the turn landed from whatever evidence the engine holds, and settles once:
re-arm the flag when it was taken and the turn did not land, THEN commit or roll back
the skill-body dedup writes. These tests observe that through the two collaborators
it is handed, which share one ordered ledger.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from channel_turn_fakes import FakeSessions, RecordingCtxBuilder

from kiro_crew.messaging.turn_bracket import TurnBracket, stop_reason_landed, turn_bracket

KEY = "weixin:agentA:direct:userA"


def _bracket(*, armed: bool) -> tuple[TurnBracket, FakeSessions, RecordingCtxBuilder]:
    sessions = FakeSessions(reinjection=armed)
    ctx = RecordingCtxBuilder(ledger=sessions.ledger)
    return TurnBracket(sessions, ctx, KEY), sessions, ctx


def test_stop_reason_landed_is_a_success_allowlist() -> None:
    assert stop_reason_landed("end_turn") is True
    assert stop_reason_landed("") is True, "a completion from a provider that never sets the field"
    assert stop_reason_landed(None) is False, "no completion observed at all"
    for reason in ("cancelled", "stale_recover", "error: tool stall", "refusal", "error: boom"):
        assert stop_reason_landed(reason) is False, reason


def test_taking_the_flag_reads_and_clears_it_now() -> None:
    bracket, sessions, _ctx = _bracket(armed=True)
    assert bracket.take_reinjection() is True
    assert sessions.reinjection_armed is False
    assert bracket.take_reinjection() is False, "a second attempt finds it consumed"


@pytest.mark.parametrize(
    ("evidence", "landed"),
    [
        pytest.param(True, True, id="bool-true"),
        pytest.param(False, False, id="bool-false"),
        pytest.param("end_turn", True, id="succeeded-stop-reason"),
        pytest.param("cancelled", False, id="cancelled-stop-reason"),
        pytest.param(None, False, id="no-completion"),
        pytest.param(
            SimpleNamespace(completion_observed=True, last_stop_reason="end_turn"),
            True,
            id="completed-driver",
        ),
        pytest.param(
            SimpleNamespace(completion_observed=False, last_stop_reason="end_turn"),
            False,
            id="driver-without-completion",
        ),
        pytest.param(object(), False, id="driver-stand-in-without-fields"),
    ],
)
def test_landed_reads_every_kind_of_evidence(evidence: Any, landed: bool) -> None:
    bracket, _sessions, _ctx = _bracket(armed=False)
    assert bracket.landed(evidence) is landed


def test_a_taken_flag_that_never_landed_is_re_armed_before_the_rollback() -> None:
    bracket, sessions, ctx = _bracket(armed=True)
    bracket.take_reinjection()

    bracket.settle()

    assert sessions.reinjection_armed is True
    assert sessions.names() == [
        "consume_reinjection",
        "mark_reinjection",
        "rollback_skill_bodies",
    ]
    assert ctx.settles == [("rollback", KEY)]


def test_a_landed_turn_keeps_the_flag_consumed_and_commits() -> None:
    bracket, sessions, ctx = _bracket(armed=True)
    bracket.take_reinjection()
    bracket.landed(True)

    bracket.settle()

    assert sessions.reinjection_armed is False and sessions.count("mark_reinjection") == 0
    assert ctx.settles == [("commit", KEY)]


def test_a_turn_that_took_nothing_arms_nothing() -> None:
    bracket, sessions, ctx = _bracket(armed=False)
    bracket.take_reinjection()
    bracket.settle()
    assert sessions.count("mark_reinjection") == 0
    assert ctx.settles == [("rollback", KEY)]


def test_settle_uses_the_latest_attempts_take() -> None:
    """A replayed attempt consumes again; the settle re-arms on what IT took."""
    bracket, sessions, _ctx = _bracket(armed=True)
    bracket.take_reinjection()  # attempt 1 consumed the flag
    bracket.take_reinjection()  # the replay found it consumed
    bracket.settle()
    assert sessions.count("mark_reinjection") == 0


def test_only_the_first_settle_acts() -> None:
    """An engine that hands a turn over early settles at the hand-off; its later
    ``finally`` must then change nothing."""
    bracket, sessions, ctx = _bracket(armed=True)
    bracket.take_reinjection()
    bracket.settle()
    bracket.landed(True)
    bracket.settle()
    assert sessions.count("mark_reinjection") == 1
    assert ctx.settles == [("rollback", KEY)]


def test_settle_never_raises_and_still_settles_both_halves() -> None:
    class _Broken(FakeSessions):
        def mark_needs_reinjection(self, key: str) -> None:
            raise RuntimeError("session map unavailable")

    class _BrokenCtx(RecordingCtxBuilder):
        def rollback_skill_bodies(self, session_key: str) -> None:
            super().rollback_skill_bodies(session_key)
            raise RuntimeError("index unavailable")

    sessions = _Broken(reinjection=True)
    ctx = _BrokenCtx()
    bracket = TurnBracket(sessions, ctx, KEY)
    bracket.take_reinjection()

    bracket.settle()

    assert ctx.settles == [("rollback", KEY)], "a failed re-arm does not skip the rollback"


def test_stand_ins_without_the_methods_are_a_safe_no_op() -> None:
    bracket = TurnBracket(object(), object(), KEY)
    assert bracket.take_reinjection() is False
    bracket.settle()


@pytest.mark.parametrize("raises", [False, True])
def test_the_context_manager_settles_on_every_exit(raises: bool) -> None:
    sessions = FakeSessions(reinjection=True)
    ctx = RecordingCtxBuilder(ledger=sessions.ledger)

    async def scenario() -> None:
        async with turn_bracket(sessions, ctx, KEY) as bracket:
            bracket.take_reinjection()
            if raises:
                raise RuntimeError("the turn died")
            bracket.landed("end_turn")

    if raises:
        with pytest.raises(RuntimeError, match="the turn died"):
            asyncio.run(scenario())
        assert sessions.reinjection_armed is True and ctx.settles == [("rollback", KEY)]
    else:
        asyncio.run(scenario())
        assert sessions.reinjection_armed is False and ctx.settles == [("commit", KEY)]
