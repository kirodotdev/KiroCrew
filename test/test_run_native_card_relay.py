"""The gateway draws a Crew run's own child cards from the run's ``run_native_card`` events.

A run on claude reconciles its harness-native children's cards itself
(``subagent_manager.native_child_cards``) and hands each frame to the manager's
``on_event`` as one ``run_native_card`` event. The gateway's ``_subagent_event`` hands
that to ``dashboard.run_native_card_relay``, which draws the frame in the tab the run
tree started from, with no ``batch_id``, keeps the open card in ``_native_cards`` with
the run's id for its Stop, sends chunks to subagent subscribers only, and passes every
frame through the scale coalescer. The event itself is never broadcast.

Driven through the real ``on_event`` the gateway wires (``test_gateway_subagent_wiring``'s
harness, whose ``subagent_event_slot`` names a frame's tab ``evt:<session key>``).
"""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from unittest import mock
from unittest.mock import ANY, MagicMock

import pytest
from member_memory_helpers import env as _member_env
from member_memory_helpers import request
from test_gateway_subagent_wiring import _Done, harness, make_info

from kiro_crew.dashboard.run_native_card_relay import RELAYED_FRAMES, relay_run_native_card
from kiro_crew.subagent import SubagentInfo
from kiro_crew.subagent_manager.native_child_cards import RELAY_EVENT

RUN = "0123456789abcdef"
CARD = f"native:{RUN}:child-1"
ROOT_TAB = "evt:dashboard:main"


env = _member_env


@pytest.fixture
def done(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Done]:
    yield from harness(monkeypatch)


def _run(*, batch_id: str = "w1") -> SubagentInfo:
    """A nested run: spawned by another run, in a tree the main tab started."""
    info = make_info(RUN, parent="subagent:fedcba9876543210", batch_id=batch_id, batch_total=2)
    info.root_session_key = "dashboard:main"
    return info


async def _relay(done: _Done, etype: str, frame: dict[str, Any], child: str = "child-1") -> None:
    await done.manager_kwargs["on_event"](
        RELAY_EVENT, _run(), {"etype": etype, "frame": frame, "child": child}
    )


def _subscriber_frames(done: _Done, kind: str) -> list[dict[str, Any]]:
    calls = done.state.broadcast_ws_subagent_subscribers.call_args_list
    return [c.args[1] for c in calls if c.args[0] == kind]


def _every_kind(done: _Done) -> set[str]:
    calls = [
        *done.state.broadcast_ws.call_args_list,
        *done.state.broadcast_ws_subagent_subscribers.call_args_list,
    ]
    return {c.args[0] for c in calls}


def test_the_relay_draws_the_four_card_frames() -> None:
    assert RELAYED_FRAMES == {"subagent_spawn", "subagent_tool", "subagent_chunk", "subagent_done"}


@pytest.mark.asyncio
async def test_a_child_card_draws_in_the_root_tab_in_no_wave_and_is_registered(
    done: _Done,
) -> None:
    await _relay(done, "subagent_spawn", {"id": CARD, "task": "say pong", "agent": "gp"})
    assert done.frames("subagent_spawn") == [
        {"id": CARD, "task": "say pong", "agent": "gp", "slot": ROOT_TAB}
    ]
    assert done.state._native_cards == {
        CARD: {"slot": ROOT_TAB, "session_id": "child-1", "started": ANY, "run_id": RUN}
    }
    assert RELAY_EVENT not in _every_kind(done)


@pytest.mark.asyncio
async def test_a_childs_chunks_reach_subscribers_only(done: _Done) -> None:
    await _relay(done, "subagent_spawn", {"id": CARD, "task": "t", "agent": "a"})
    await _relay(done, "subagent_chunk", {"id": CARD, "text": "PONG"})
    assert _subscriber_frames(done, "subagent_chunk") == [
        {"id": CARD, "text": "PONG", "slot": ROOT_TAB}
    ]
    assert done.frames("subagent_chunk") == []


@pytest.mark.asyncio
async def test_a_childs_end_unregisters_its_card(done: _Done) -> None:
    await _relay(done, "subagent_spawn", {"id": CARD, "task": "t", "agent": "a"})
    await _relay(done, "subagent_tool", {"id": CARD, "tool": "Bash"})
    await _relay(done, "subagent_done", {"id": CARD, "error": None, "result": "PONG"})
    assert done.frames("subagent_tool") == [{"id": CARD, "tool": "Bash", "slot": ROOT_TAB}]
    assert done.frames("subagent_done") == [
        {"id": CARD, "error": None, "result": "PONG", "slot": ROOT_TAB}
    ]
    assert done.state._native_cards == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("etype", "frame"),
    [
        ("subagent_stalled", {"id": CARD, "stalled": True}),
        ("run_native_card", {"id": CARD}),
        ("subagent_spawn", {"id": "native:aaaabbbbccccdddd:child-1", "task": "t"}),
        ("subagent_spawn", {"id": RUN, "task": "t"}),
        ("subagent_spawn", "not a frame"),
    ],
    ids=["other-kind", "itself", "another-run", "the-run-itself", "not-a-dict"],
)
async def test_a_frame_that_is_not_this_runs_child_card_draws_nothing(
    done: _Done, etype: str, frame: Any
) -> None:
    done.state._native_cards = {}
    await done.manager_kwargs["on_event"](
        RELAY_EVENT, _run(), {"etype": etype, "frame": frame, "child": "child-1"}
    )
    assert _every_kind(done) == set()
    assert done.state._native_cards == {}


@pytest.mark.asyncio
async def test_above_the_scale_threshold_the_childs_deltas_coalesce(done: _Done) -> None:
    done.mgr.running_count = 100
    try:
        await _relay(done, "subagent_spawn", {"id": CARD, "task": "t", "agent": "a"})
        await _relay(done, "subagent_chunk", {"id": CARD, "text": "PO"})
        await _relay(done, "subagent_chunk", {"id": CARD, "text": "NG"})
        await _relay(done, "subagent_tool", {"id": CARD, "tool": "Bash"})
        assert _subscriber_frames(done, "subagent_chunk") == []
        assert done.frames("subagent_tool") == []
        # The end flushes what the coalescer holds before it goes out.
        await _relay(done, "subagent_done", {"id": CARD, "error": None, "result": "PONG"})
        [batch] = _subscriber_frames(done, "subagent_batch_chunks")
        assert batch == {"chunks": [{"id": CARD, "slot": ROOT_TAB, "text": "PONG"}]}
        [update] = done.frames("subagent_batch_update")
        assert update == {"updates": [{"id": CARD, "slot": ROOT_TAB, "tool": "Bash"}]}
        assert [f["id"] for f in done.frames("subagent_done")] == [CARD]
    finally:
        done.orch._subagent_coalescer().close()


@pytest.mark.asyncio
async def test_a_child_card_is_owned_by_the_tab_it_draws_in(env: Any) -> None:
    """The ownership check of a run control is unchanged: an internal caller reaches a
    native card through the tab its registry entry names, and nothing else does."""
    from kiro_crew.dashboard.handlers import messaging

    env.state.subagents = SimpleNamespace(get=lambda _id: None, all_agents=[])
    env.state.broadcast_ws = MagicMock()
    coalescer = MagicMock()
    coalescer.handle.return_value = False
    frame = {"id": CARD, "task": "t", "agent": "a"}
    extra = {"etype": "subagent_spawn", "frame": frame, "child": "child-1"}
    relay_run_native_card(env.state, coalescer, "owner-slot", RUN, extra)
    with mock.patch("kiro_crew.dashboard.handlers.messaging.read_state", return_value=None):
        for caller, admitted in (
            ("dashboard:owner-slot", True),
            ("dashboard:another-slot", False),
            (f"subagent:{RUN}", False),
            ("", False),
        ):
            req = request(env, body={"message": "x"}, internal=True, session=caller)
            req.match_info["agent_id"] = CARD
            refusal = await messaging._spawn_scope_refusal(req)
            assert (refusal is None) is admitted, caller
            if refusal is not None:
                assert refusal.status == 404
