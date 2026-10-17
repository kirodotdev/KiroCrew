"""How the channel turn pipeline reads its recovery state and charges a failed turn.

The helpers live in :mod:`kiro_crew.messaging.dispatch_recovery` and are reached, as every
caller reaches them, through :mod:`kiro_crew.messaging.dispatch`. Each reader of the session
manager is defensive: a stand-in lacking the method, or answering with the wrong shape, reads
as "nothing to report" rather than raising into a turn. ``charge_turn_failure`` exempts the
death of a runtime other sessions shared, up to the breaker's own threshold, and at the
threshold performs the reset the breaker would.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew import runtime_death
from kiro_crew.acp.client import AcpProcessDied
from kiro_crew.messaging import dispatch
from kiro_crew.messaging.link import build_dm_session_key

#: A DM key with a generation, and the bucket the generation reader is asked for.
_DM_KEY = build_dm_session_key("weixin", "agent", "u1", gen=2)
_DM_BUCKET = build_dm_session_key("weixin", "agent", "u1")


@pytest.fixture
def deaths() -> Iterator[None]:
    """A clean death table on both sides of the test: it is process-global state."""
    runtime_death._reset_for_tests()
    yield
    runtime_death._reset_for_tests()


class _Raises:
    def __call__(self, *_args: Any) -> Any:
        raise RuntimeError("reader failed")


class _Sessions:
    """The slice of ``SessionManager`` the breaker accounting reads, recorded."""

    def __init__(self, threshold: object = 3, *, reset_raises: bool = False) -> None:
        self.threshold = threshold
        self.reset_raises = reset_raises
        self.failures: list[str] = []
        self.resets: list[str] = []
        self.successes: list[str] = []

    def _allocation_deps(self) -> Any:
        return SimpleNamespace(constants=SimpleNamespace(circuit_breaker_threshold=self.threshold))

    async def record_failure(self, key: str) -> None:
        self.failures.append(key)

    async def reset(self, key: str) -> None:
        if self.reset_raises:
            raise RuntimeError("reset failed")
        self.resets.append(key)

    def record_success(self, key: str) -> None:
        self.successes.append(key)


class _Runtime:
    """A runtime the death table can key on (weak-referenceable)."""


def _shared_runtime() -> _Runtime:
    runtime = _Runtime()
    runtime_death.announce(runtime, reason="probe", expected=False, leases=2, acp_sessions=2)
    return runtime


class TestGenerationReaders:
    def test_the_stop_count_reads_the_managers_answer(self) -> None:
        sessions = SimpleNamespace(stop_generation=lambda key: 4)
        assert dispatch.session_stop_generation(sessions, "k") == 4

    @pytest.mark.parametrize(
        "sessions",
        [
            SimpleNamespace(),
            SimpleNamespace(stop_generation=_Raises()),
            SimpleNamespace(stop_generation=lambda key: "4"),
        ],
        ids=["no-reader", "raises", "not-an-int"],
    )
    def test_a_stop_count_that_cannot_be_read_is_zero(self, sessions: Any) -> None:
        assert dispatch.session_stop_generation(sessions, "k") == 0

    def test_the_conversation_generation_reads_the_bucket(self) -> None:
        asked: list[str] = []

        def reader(bucket: str) -> int:
            asked.append(bucket)
            return 2

        sessions = SimpleNamespace(max_generation=reader)
        assert dispatch.session_conversation_generation(sessions, _DM_KEY) == 2
        assert asked == [_DM_BUCKET]

    @pytest.mark.parametrize(
        "reader",
        [_Raises(), lambda bucket: True, lambda bucket: "2"],
        ids=["raises", "a-bool", "not-an-int"],
    )
    def test_a_generation_that_cannot_be_read_is_zero(self, reader: Any) -> None:
        sessions = SimpleNamespace(max_generation=reader)
        assert dispatch.session_conversation_generation(sessions, _DM_KEY) == 0

    def test_a_key_without_a_generation_grammar_is_zero(self) -> None:
        sessions = SimpleNamespace(max_generation=lambda bucket: 9)
        assert dispatch.session_conversation_generation(sessions, "slack:C1:1700000000.1") == 0


class TestReplayGap:
    def test_the_gap_is_awaited_when_the_manager_has_one(self) -> None:
        waited: list[str] = []

        async def await_replay_gap(key: str) -> None:
            waited.append(key)

        asyncio.run(
            dispatch.await_replay_gap(SimpleNamespace(await_replay_gap=await_replay_gap), "k")
        )
        assert waited == ["k"]

    def test_a_stand_in_without_a_gap_waits_for_nothing(self) -> None:
        asyncio.run(dispatch.await_replay_gap(SimpleNamespace(), "k"))

    def test_the_gap_opens_and_closes_through_the_matching_method(self) -> None:
        calls: list[tuple[str, str]] = []
        sessions = SimpleNamespace(
            open_replay_gap=lambda key: calls.append(("open", key)),
            close_replay_gap=lambda key: calls.append(("close", key)),
        )
        dispatch._set_replay_gap(sessions, "k", opened=True)
        dispatch._set_replay_gap(sessions, "k", opened=False)
        assert calls == [("open", "k"), ("close", "k")]

    def test_a_failing_gap_method_is_swallowed(self) -> None:
        sessions = SimpleNamespace(open_replay_gap=_Raises(), close_replay_gap=_Raises())
        dispatch._set_replay_gap(sessions, "k", opened=True)
        dispatch._set_replay_gap(sessions, "k", opened=False)


class TestBreakerThreshold:
    def test_the_threshold_is_the_managers_own(self) -> None:
        assert dispatch._breaker_threshold(_Sessions(threshold=5)) == 5

    @pytest.mark.parametrize("threshold", [0, -1, True, "3", None], ids=repr)
    def test_a_threshold_that_is_not_a_positive_int_is_unknown(self, threshold: object) -> None:
        assert dispatch._breaker_threshold(_Sessions(threshold=threshold)) is None

    def test_a_manager_without_allocation_deps_has_no_known_threshold(self) -> None:
        assert dispatch._breaker_threshold(SimpleNamespace()) is None

    def test_allocation_deps_that_raise_leave_the_threshold_unknown(self) -> None:
        assert dispatch._breaker_threshold(SimpleNamespace(_allocation_deps=_Raises())) is None


class TestChargeTurnFailure:
    def _charge(self, sessions: _Sessions, exc: BaseException, provider: object) -> None:
        asyncio.run(
            dispatch.charge_turn_failure(
                sessions, "k", exc=exc, provider=provider, channel_type="weixin"
            )
        )

    def test_an_ordinary_failure_is_charged(self, deaths: None) -> None:
        sessions = _Sessions()
        self._charge(sessions, RuntimeError("boom"), _shared_runtime())
        assert sessions.failures == ["k"]
        assert runtime_death.shared_deaths("k") == 0

    def test_a_death_of_a_runtime_this_session_was_alone_on_is_charged(self, deaths: None) -> None:
        sessions = _Sessions()
        self._charge(sessions, AcpProcessDied("died"), _Runtime())
        assert sessions.failures == ["k"]

    def test_without_a_known_threshold_a_shared_death_is_charged(self, deaths: None) -> None:
        sessions = _Sessions(threshold=None)
        self._charge(sessions, AcpProcessDied("died"), _shared_runtime())
        assert sessions.failures == ["k"]
        assert runtime_death.shared_deaths("k") == 0

    def test_a_shared_death_below_the_threshold_is_counted_not_charged(self, deaths: None) -> None:
        sessions = _Sessions(threshold=3)
        runtime = _shared_runtime()
        self._charge(sessions, AcpProcessDied("died"), runtime)
        self._charge(sessions, AcpProcessDied("died"), runtime)
        assert sessions.failures == []
        assert sessions.resets == []
        assert runtime_death.shared_deaths("k") == 2

    def test_at_the_threshold_the_runtime_is_reset_and_the_streak_spent(self, deaths: None) -> None:
        sessions = _Sessions(threshold=2)
        runtime = _shared_runtime()
        self._charge(sessions, AcpProcessDied("died"), runtime)
        self._charge(sessions, AcpProcessDied("died"), runtime)
        assert sessions.failures == []
        assert sessions.resets == ["k"]
        assert runtime_death.shared_deaths("k") == 0

    def test_a_failed_reset_keeps_the_streak_for_the_next_death(self, deaths: None) -> None:
        sessions = _Sessions(threshold=1, reset_raises=True)
        self._charge(sessions, AcpProcessDied("died"), _shared_runtime())
        assert sessions.failures == []
        assert runtime_death.shared_deaths("k") == 1


class TestRecordSuccess:
    def test_a_delivered_turn_counts_and_clears_the_streak(self, deaths: None) -> None:
        runtime_death.note_shared_death("k")
        sessions = _Sessions()
        dispatch._record_success(sessions, "k", "weixin")
        assert sessions.successes == ["k"]
        assert runtime_death.shared_deaths("k") == 0

    def test_a_failing_success_record_is_swallowed(self, deaths: None) -> None:
        runtime_death.note_shared_death("k")
        dispatch._record_success(SimpleNamespace(record_success=_Raises()), "k", "weixin")
        assert runtime_death.shared_deaths("k") == 1
