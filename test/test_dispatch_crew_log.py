"""How a channel conversation's crew log is opened ahead of its turn.

The opener and the facts it reads live in :mod:`kiro_crew.messaging.dispatch_crew_log` and are
reached, as every caller reaches them, through :mod:`kiro_crew.messaging.dispatch`. Each read
is best-effort: a stand-in without the reader, or one that raises, states nothing rather than
failing the turn, and the opener itself never raises.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew.crew_log import emit as crew_log_emit
from kiro_crew.history import transcript_stem
from kiro_crew.messaging import dispatch


class _Raises:
    def __call__(self, *_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("reader failed")


@pytest.mark.parametrize(
    "reader_name, helper",
    [
        ("allocation_predecessor", dispatch.predecessor_sid),
        ("allocation_requested_model", dispatch.requested_model_sid),
    ],
    ids=["predecessor", "requested-model"],
)
class TestAllocationReads:
    def test_the_allocations_capture_is_read_for_the_key(
        self, reader_name: str, helper: Any
    ) -> None:
        asked: list[str] = []

        def reader(key: str) -> str:
            asked.append(key)
            return "value-1"

        assert helper(SimpleNamespace(**{reader_name: reader}), "k") == "value-1"
        assert asked == ["k"]

    @pytest.mark.parametrize(
        "sessions_for",
        [
            lambda name: SimpleNamespace(),
            lambda name: SimpleNamespace(**{name: _Raises()}),
            lambda name: SimpleNamespace(**{name: lambda key: None}),
        ],
        ids=["no-reader", "raises", "answers-none"],
    )
    def test_an_unreadable_capture_states_nothing(
        self, reader_name: str, helper: Any, sessions_for: Any
    ) -> None:
        assert helper(sessions_for(reader_name), "k") == ""


class TestSlotWorkspace:
    def test_the_workspace_is_read_off_the_conversations_slot(self) -> None:
        asked: list[str] = []

        def get_slot(name: str) -> Any:
            asked.append(name)
            return SimpleNamespace(workspace="/srv/work")

        state = SimpleNamespace(get_slot=get_slot)
        assert dispatch.slot_workspace(state, "weixin:agent:direct:u1") == "/srv/work"
        assert asked == [transcript_stem("weixin:agent:direct:u1")]

    @pytest.mark.parametrize(
        "state",
        [
            None,
            SimpleNamespace(),
            SimpleNamespace(get_slot=_Raises()),
            SimpleNamespace(get_slot=lambda name: None),
            SimpleNamespace(get_slot=lambda name: SimpleNamespace(workspace=object())),
        ],
        ids=["no-state", "no-getter", "raises", "no-slot", "not-a-string"],
    )
    def test_a_workspace_that_is_not_stated_reads_empty(self, state: Any) -> None:
        assert dispatch.slot_workspace(state, "k") == ""


class TestStorePredecessor:
    _KEY = "weixin:agent:direct:u1"

    def _store(self, monkeypatch: pytest.MonkeyPatch, answer: Any) -> list[str]:
        asked: list[str] = []

        def slot_previous_store(slot: str) -> Any:
            asked.append(slot)
            if isinstance(answer, BaseException):
                raise answer
            return answer

        monkeypatch.setattr(crew_log_emit, "enabled", lambda: True)
        monkeypatch.setattr(crew_log_emit, "slot_previous_store", slot_previous_store)
        return asked

    def test_the_slots_store_is_read_only_when_no_live_session_holds_the_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        asked = self._store(monkeypatch, ("sid-1", True, True))
        live = SimpleNamespace(has_session=lambda key: True)
        cold = SimpleNamespace(has_session=lambda key: False)
        assert asyncio.run(dispatch.store_predecessor(live, self._KEY)) is None
        assert asyncio.run(dispatch.store_predecessor(cold, self._KEY)) == ("sid-1", True, True)
        assert asked == [transcript_stem(self._KEY)]

    def test_a_store_read_that_fails_is_undecided_not_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._store(monkeypatch, OSError("listing failed"))
        cold = SimpleNamespace(has_session=lambda key: False)
        assert asyncio.run(dispatch.store_predecessor(cold, self._KEY)) == ("", False, False)


@pytest.mark.parametrize(
    "capture, replay, store, edge",
    [
        ("sid-cap", False, ("sid-store", True, True), ("sid-cap", False)),
        ("sid-cap", False, None, ("sid-cap", False)),
        ("", False, None, ("", None)),
        ("", False, ("sid-store", True, True), ("sid-store", False)),
        ("", False, ("", True, True), ("", False)),
        ("", False, ("", True, False), ("", None)),
        ("", False, ("", False, False), ("", True)),
        ("sid-cap", True, ("sid-store", True, True), ("sid-store", False)),
        ("sid-cap", True, ("", True, True), ("", True)),
        ("sid-cap", True, None, ("sid-cap", False)),
    ],
    ids=[
        "capture-ahead-of-store",
        "capture-store-unread",
        "nothing-read",
        "restart-store-names-it",
        "first-store-stated",
        "unrankable-units-state-nothing",
        "unreadable-store-breaks",
        "replay-window-store-decides",
        "replay-window-store-empty-breaks",
        "replay-window-store-unread",
    ],
)
def test_the_edge_takes_each_tier_in_order(
    capture: str, replay: bool, store: Any, edge: tuple[str, Any]
) -> None:
    sessions = SimpleNamespace(
        allocation_predecessor=lambda key: capture,
        provider_switch_replay_pending=lambda key: replay,
    )
    assert dispatch.predecessor_edge(sessions, "k", store) == edge


class _Opened:
    """Records ``on_session_opened`` calls."""

    def __init__(self, *, raises: bool = False) -> None:
        self.raises = raises
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, session_id: str, **fields: Any) -> None:
        self.calls.append((session_id, fields))
        if self.raises:
            raise RuntimeError("emitter failed")


class TestOpenTurnCrewLog:
    def _open(
        self, monkeypatch: pytest.MonkeyPatch, *, sid: str, ctx_builder: Any, opened: _Opened
    ) -> None:
        monkeypatch.setattr(crew_log_emit, "session_id_of", lambda provider: sid)
        monkeypatch.setattr(crew_log_emit, "on_session_opened", opened)
        provider = SimpleNamespace(served_model="model-a", cwd="/srv/work")
        dispatch.open_turn_crew_log(
            provider,
            session_key="weixin:agent:direct:u1",
            agent="agent",
            resumed=False,
            ctx_builder=ctx_builder,
            sessions=SimpleNamespace(allocation_predecessor=lambda key: "sid-0"),
            model_requested="model-r",
            workspace="/srv/work",
        )

    def test_the_log_is_opened_with_the_facts_the_dispatcher_holds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        opened = _Opened()
        builder = SimpleNamespace(live_memory_mode_for_session=lambda key: "persistent")
        self._open(monkeypatch, sid="sid-1", ctx_builder=builder, opened=opened)
        assert len(opened.calls) == 1
        session_id, fields = opened.calls[0]
        assert session_id == "sid-1"
        assert fields["memory"] == "persistent"
        assert fields["slot"] == transcript_stem("weixin:agent:direct:u1")
        assert fields["previous_sid"] == "sid-0"
        assert fields["previous_undecided"] is False
        assert fields["model"] == "model-a"
        assert fields["channel"] is True

    def test_a_provider_without_a_session_id_opens_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        opened = _Opened()
        self._open(monkeypatch, sid="", ctx_builder=None, opened=opened)
        assert opened.calls == []

    def test_an_unreadable_memory_mode_states_no_class(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        opened = _Opened()
        builder = SimpleNamespace(live_memory_mode_for_session=_Raises())
        self._open(monkeypatch, sid="sid-1", ctx_builder=builder, opened=opened)
        assert opened.calls[0][1]["memory"] == ""

    def test_a_failing_emitter_never_raises_into_the_turn(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        opened = _Opened(raises=True)
        self._open(monkeypatch, sid="sid-1", ctx_builder=None, opened=opened)
        assert len(opened.calls) == 1
