"""A code channel's Stop also stops its helper agents and their sub-agents
(RFC ``rfc-slack-code-channels`` §3 "Stop"), and says how many it stopped.

The brake only: nothing is held afterwards (the hold is a follow-up change), so
these tests pin what the Stop itself does, its races included: a second press, a
helper that finishes while being stopped, a helper born during the Stop, and the
stop fence set before any await.

Not here: a shared user's Stop being refused needs shared code-channel access,
which does not exist yet; only the owner may press Stop at all
(``test_native_stop_from_a_stranger_is_denied``). How a stopped approval's Slack
card settles is pinned in ``test_slack_code_channel_worker_approvals``.
"""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from slack_code_channels_helpers import OWNER, Env, make_env

from kiro_crew.slack import events as _slack_events  # noqa: F401  (see the routing tests)
from kiro_crew.slack.code_channels import routing, surface
from kiro_crew.slack.code_channels.routing import Gate

pytestmark = pytest.mark.asyncio

CHANNEL_SESSION = "chat-9-1"


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Iterator[Env]:
    yield from make_env(monkeypatch, tmp_path)


class _Subagents:
    def __init__(self, counts: dict[str, tuple[int, int]] | None = None) -> None:
        self.counts = counts or {}
        self.parents: list[str] = []

    async def cancel_for_parent(self, parent: str) -> tuple[int, int]:
        self.parents.append(parent)
        return self.counts.pop(parent, (0, 0))  # a cancelled run is not cancelled again


def _slot(key: str, running: bool = False) -> SimpleNamespace:
    return SimpleNamespace(key=key, running=running)


def _world(env: Env, monkeypatch: pytest.MonkeyPatch, **helpers: bool) -> dict[str, Any]:
    """Bind the channel, record each of *helpers* (key -> running) as its helper agent."""
    from kiro_crew.slack.code_channels.binding import Binding

    binding = Binding(
        channel_id="C0CODE",
        team_id="T1",
        session_key=CHANNEL_SESSION,
        anchor_ts="300.000001",
        origin_channel_id="C0ORIGIN",
        origin_ts="1700000000.000100",
    )
    env.store.put_binding(binding)
    env.info("C0CODE")
    assert env.state is not None
    channel = _slot(CHANNEL_SESSION)
    env.state.linked[binding.anchor_ts] = channel
    slots: dict[str, Any] = {CHANNEL_SESSION: channel}
    for key, running in helpers.items():
        slots[key] = _slot(key, running)
        env.store.record_worker(key, CHANNEL_SESSION)
    env.state.get_slot = slots.get  # type: ignore[attr-defined]
    env.state.subagents = _Subagents()  # type: ignore[attr-defined]
    stopped: list[tuple[str, dict[str, Any]]] = []
    answers: dict[str, dict[str, Any]] = {}

    async def stop_slot_turn(state: Any, slot: Any, **kwargs: Any) -> dict[str, Any]:
        stopped.append((slot.key, kwargs))
        return answers.get(slot.key, {"ok": True, "info": "stopping"})

    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.stop_slot_turn", stop_slot_turn)
    monkeypatch.setattr("kiro_crew.slack.handler.is_allowed_user", lambda u: u == OWNER)
    return {"slots": slots, "stopped": stopped, "answers": answers, "binding": binding}


async def _bang_stop(env: Env, ts: str = "301.0") -> None:
    event = {"type": "message", "user": OWNER, "channel": "C0CODE", "ts": ts, "text": "!stop"}
    result = await routing.inbound_gate(
        env.orch,
        event,
        text="!stop",
        sender_id=OWNER,
        authorized=True,
        from_trusted_bot=False,
    )
    assert result.gate is Gate.DROP


def _posts(env: Env) -> list[str]:
    return [text for _channel, text, _ts in env.slack.posts]


async def test_an_idle_channels_stop_stops_each_running_helper_and_says_how_many(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = _world(env, monkeypatch, **{"chat-h1": True, "chat-h2": True})
    await _bang_stop(env)
    assert w["stopped"] == [
        ("chat-h1", {"source": "slack", "escalate": True}),
        ("chat-h2", {"source": "slack", "escalate": True}),
    ]
    assert _posts(env) == ["Stopped 2: 2 helper agents."]
    rows = [r["resources"] for r in env.sel.ops("slack.code_channel.stop_command")]
    assert sum("slot=chat-h1 result=stopping" in r for r in rows) == 1
    assert sum("slot=chat-h2 result=stopping" in r for r in rows) == 1


async def test_a_running_channel_session_and_its_helpers_all_stop(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = _world(env, monkeypatch, **{"chat-h1": True})
    w["slots"][CHANNEL_SESSION].running = True
    await _bang_stop(env)
    assert [k for k, _ in w["stopped"]] == [CHANNEL_SESSION, "chat-h1"]
    assert _posts(env) == ["Stopped 2: this channel's session and 1 helper agent."]


async def test_only_the_stores_own_helpers_are_stopped(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A child known only from the crew log, and another channel's helper, are left alone."""
    w = _world(env, monkeypatch, **{"chat-h1": True})
    w["slots"]["chat-adopted"] = SimpleNamespace(
        key="chat-adopted", running=True, _created_by=CHANNEL_SESSION
    )
    w["slots"]["chat-other"] = _slot("chat-other", True)
    env.store.record_worker("chat-other", "chat-elsewhere-1")
    await _bang_stop(env)
    assert [k for k, _ in w["stopped"]] == ["chat-h1"]


async def test_a_helper_that_finished_while_being_stopped_is_not_counted(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = _world(env, monkeypatch, **{"chat-h1": True, "chat-h2": True})

    async def stop_slot_turn(state: Any, slot: Any, **kwargs: Any) -> dict[str, Any]:
        w["stopped"].append((slot.key, kwargs))
        if slot.key == "chat-h1":
            slot.running = False  # its turn ended just before the stop call
            return {"ok": True, "info": "not running"}
        return {"ok": True, "info": "stopping"}

    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.stop_slot_turn", stop_slot_turn)
    await _bang_stop(env)
    assert [k for k, _ in w["stopped"]] == ["chat-h1", "chat-h2"]
    assert _posts(env) == ["Stopped 1: 1 helper agent."]


async def test_a_helper_born_during_the_stop_is_stopped_too(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = _world(env, monkeypatch, **{"chat-h1": True})
    w["slots"][CHANNEL_SESSION].running = True
    late = _slot("chat-late", True)

    async def stop_slot_turn(state: Any, slot: Any, **kwargs: Any) -> dict[str, Any]:
        w["stopped"].append((slot.key, kwargs))
        if slot.key == "chat-h1":
            # The session's in-flight session_create lands while the helpers stop.
            w["slots"]["chat-late"] = late
            env.store.record_worker("chat-late", CHANNEL_SESSION)
        return {"ok": True, "info": "stopping"}

    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.stop_slot_turn", stop_slot_turn)
    await _bang_stop(env)
    assert [k for k, _ in w["stopped"]] == [CHANNEL_SESSION, "chat-h1", "chat-late"]
    assert _posts(env) == ["Stopped 3: this channel's session and 2 helper agents."]


async def test_a_second_press_escalates_the_helpers_too(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = _world(env, monkeypatch, **{"chat-h1": True})
    await _bang_stop(env, "301.0")
    await _bang_stop(env, "302.0")
    assert w["stopped"] == [("chat-h1", {"source": "slack", "escalate": True})] * 2


async def test_the_stop_fence_is_set_before_anything_is_awaited(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Work admitted before the press writes nothing more, from the first await on:
    the fence is up when the helpers' stops and the status settle run."""
    w = _world(env, monkeypatch, **{"chat-h1": True})
    fenced: list[tuple[str, bool]] = []

    async def stop_slot_turn(state: Any, slot: Any, **kwargs: Any) -> dict[str, Any]:
        fenced.append((slot.key, surface._stopped_at.get("C0CODE", 0) > 0))
        w["stopped"].append((slot.key, kwargs))
        return {"ok": True, "info": "stopping"}

    async def settle(channel: str) -> None:
        fenced.append(("settle", surface._stopped_at.get(channel, 0) > 0))

    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.stop_slot_turn", stop_slot_turn)
    monkeypatch.setattr("kiro_crew.slack.code_channels.status.settle_after_stop", settle)
    surface._stopped_at.pop("C0CODE", None)
    await _bang_stop(env)
    assert fenced == [("chat-h1", True), ("settle", True)]


async def test_a_helper_waiting_on_an_approval_is_stopped_like_any_running_turn(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Its turn is running while it waits, so the person's press stops it;
    ``stop_slot_turn`` rejects the pending approval (Pause all would skip it)."""
    w = _world(env, monkeypatch, **{"chat-h1": True})
    w["slots"]["chat-h1"]._pending_approvals = {"req-1": object()}
    await _bang_stop(env)
    assert w["stopped"] == [("chat-h1", {"source": "slack", "escalate": True})]


async def test_with_the_feature_off_a_running_channels_stop_still_stops_its_helpers(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = _world(env, monkeypatch, **{"chat-h1": True})
    w["slots"][CHANNEL_SESSION].running = True
    env.on = False
    event = {"type": "agent_session_stopped", "channel": "C0CODE", "user": OWNER}
    assert await routing.handle_session_stopped(env.orch, event, OWNER) == "stopped"
    assert [k for k, _ in w["stopped"]] == [CHANNEL_SESSION, "chat-h1"]
    assert _posts(env) == ["Stopped 2: this channel's session and 1 helper agent."]


async def test_the_native_stop_with_nothing_running_still_says_nothing(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = _world(env, monkeypatch, **{"chat-h1": False})
    event = {"type": "agent_session_stopped", "channel": "C0CODE", "user": OWNER}
    assert await routing.handle_session_stopped(env.orch, event, OWNER) == "stopped"
    assert w["stopped"] == [] and _posts(env) == []


async def test_the_sub_agents_of_the_session_and_each_helper_are_stopped_and_counted(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = _world(env, monkeypatch, **{"chat-h1": True})
    assert env.state is not None
    subagents = _Subagents({"dashboard_chat-h1": (1, 1), f"dashboard_{CHANNEL_SESSION}": (1, 0)})
    env.state.subagents = subagents  # type: ignore[attr-defined]
    monkeypatch.setattr(
        "kiro_crew.dashboard.chat_utils.effective_session_key", lambda s: f"dashboard_{s.key}"
    )
    await _bang_stop(env)
    assert subagents.parents == [f"dashboard_{CHANNEL_SESSION}", "dashboard_chat-h1"] * 2
    assert [k for k, _ in w["stopped"]] == ["chat-h1"]
    assert _posts(env) == ["Stopped 4: 1 helper agent and 3 sub-agents."]


async def test_sub_agents_a_parents_hard_stop_takes_itself_are_still_counted(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hard Stop of a helper stops the sub-agents sharing its process itself, so
    they are cancelled, and counted, before the helper's turn is stopped."""
    w = _world(env, monkeypatch, **{"chat-h1": True})
    assert env.state is not None
    subagents = _Subagents({"dashboard_chat-h1": (1, 0)})
    env.state.subagents = subagents  # type: ignore[attr-defined]
    monkeypatch.setattr(
        "kiro_crew.dashboard.chat_utils.effective_session_key", lambda s: f"dashboard_{s.key}"
    )

    async def stop_slot_turn(state: Any, slot: Any, **kwargs: Any) -> dict[str, Any]:
        w["stopped"].append((slot.key, kwargs))
        subagents.counts.pop(f"dashboard_{slot.key}", None)  # the reset's own stop
        return {"ok": True, "info": "stopping"}

    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.stop_slot_turn", stop_slot_turn)
    await _bang_stop(env)
    assert _posts(env) == ["Stopped 2: 1 helper agent and 1 sub-agent."]


async def test_the_session_and_its_helpers_are_stopped_together(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No helper waits out another's soft-stop budget: every stop starts before any ends."""
    import asyncio

    w = _world(env, monkeypatch, **{"chat-h1": True, "chat-h2": True})
    w["slots"][CHANNEL_SESSION].running = True
    started_by_the_end: list[int] = []

    async def stop_slot_turn(state: Any, slot: Any, **kwargs: Any) -> dict[str, Any]:
        w["stopped"].append((slot.key, kwargs))
        await asyncio.sleep(0)  # one loop turn: stops made together all start first
        started_by_the_end.append(len(w["stopped"]))
        return {"ok": True, "info": "stopping"}

    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.stop_slot_turn", stop_slot_turn)
    await _bang_stop(env)
    assert started_by_the_end == [3, 3, 3]
    assert _posts(env) == ["Stopped 3: this channel's session and 2 helper agents."]


async def test_a_helper_that_starts_a_turn_during_the_stop_is_stopped_too(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Idle when the Stop first read the helpers, running by the second read."""
    w = _world(env, monkeypatch, **{"chat-h0": False, "chat-h1": True})

    async def stop_slot_turn(state: Any, slot: Any, **kwargs: Any) -> dict[str, Any]:
        w["stopped"].append((slot.key, kwargs))
        if slot.key == "chat-h1":
            w["slots"]["chat-h0"].running = True  # a message from h1 woke it
        return {"ok": True, "info": "stopping"}

    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.stop_slot_turn", stop_slot_turn)
    await _bang_stop(env)
    assert [k for k, _ in w["stopped"]] == ["chat-h1", "chat-h0"]
    assert _posts(env) == ["Stopped 2: 2 helper agents."]


async def test_the_reply_counts_the_stopped_helpers_and_names_none(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """How many it stopped, as the RFC says: no helper's title, even one the board
    shows; the reply never claims nothing starts again (nothing is held yet)."""
    from kiro_crew.slack.code_channels.workspace import core as workspace

    _world(env, monkeypatch, **{"chat-h1": True, "chat-h2": True})
    board = SimpleNamespace(rows=[SimpleNamespace(session_key="dashboard_chat-h1", title="Fix")])
    service = SimpleNamespace(
        _board_still_admitted=lambda binding: True,
        board=lambda team, channel, session: board,
        watched_sessions=lambda binding: set(),
        _forget_worker_status=lambda binding: None,
    )
    monkeypatch.setattr(workspace, "service", lambda: service)
    await _bang_stop(env)
    assert _posts(env) == ["Stopped 2: 2 helper agents."]


async def test_a_helper_whose_compaction_declines_the_stop_is_said_so_on_either_stop(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not "Nothing running.", and not silence on Slack's own Stop: a second press forces it."""
    w = _world(env, monkeypatch, **{"chat-h1": True})
    w["answers"]["chat-h1"] = {"ok": True, "info": "compacting", "compacting": True}
    await _bang_stop(env)
    declined = (
        "1 helper agent is compacting context, so Stop was not applied to it. "
        "Stop again to force it."
    )
    assert _posts(env) == [declined]
    event = {"type": "agent_session_stopped", "channel": "C0CODE", "user": OWNER}
    assert await routing.handle_session_stopped(env.orch, event, OWNER) == "stopped"
    assert _posts(env) == [declined, declined]


async def test_a_helper_stop_that_is_refused_or_fails_is_not_counted_and_is_said_so(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = _world(env, monkeypatch, **{"chat-h1": True, "chat-h2": True, "chat-h3": True})
    w["answers"]["chat-h1"] = {
        "ok": False,
        "error": "only the dashboard owner can stop a scheduled run",
        "code": "owner_only",
    }

    real = w["answers"].get

    async def stop_slot_turn(state: Any, slot: Any, **kwargs: Any) -> dict[str, Any]:
        w["stopped"].append((slot.key, kwargs))
        if slot.key == "chat-h2":
            raise RuntimeError("provider gone")
        return real(slot.key, {"ok": True, "info": "stopping"})

    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.stop_slot_turn", stop_slot_turn)
    await _bang_stop(env)
    assert _posts(env) == ["Stopped 1: 1 helper agent. 2 helper agents could not be stopped."]
    rows = [r["resources"] for r in env.sel.ops("slack.code_channel.stop_command")]
    assert any("slot=chat-h1 result=error" in r for r in rows)
    assert any("slot=chat-h2 result=error" in r for r in rows)


async def test_a_lone_helper_that_could_not_be_stopped_is_not_nothing_running(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = _world(env, monkeypatch, **{"chat-h1": True})
    w["answers"]["chat-h1"] = {"ok": False, "error": "refused", "code": "owner_only"}
    await _bang_stop(env)
    event = {"type": "agent_session_stopped", "channel": "C0CODE", "user": OWNER}
    assert await routing.handle_session_stopped(env.orch, event, OWNER) == "stopped"
    assert _posts(env) == ["1 helper agent could not be stopped."] * 2


async def test_a_channel_session_stop_that_raises_still_stops_and_reports_the_helpers(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = _world(env, monkeypatch, **{"chat-h1": True})
    w["slots"][CHANNEL_SESSION].running = True

    async def stop_slot_turn(state: Any, slot: Any, **kwargs: Any) -> dict[str, Any]:
        w["stopped"].append((slot.key, kwargs))
        if slot.key == CHANNEL_SESSION:
            raise RuntimeError("provider gone")
        return {"ok": True, "info": "stopping"}

    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.stop_slot_turn", stop_slot_turn)
    await _bang_stop(env)
    assert [k for k, _ in w["stopped"]] == [CHANNEL_SESSION, "chat-h1"]
    assert _posts(env) == [
        "Stopped 1: 1 helper agent. This channel's session could not be stopped."
    ]
    rows = [r["resources"] for r in env.sel.ops("slack.code_channel.stop_command")]
    assert any("slot=chat-h1 result=stopping" in r for r in rows)
    assert any("result=error" in r and "slot=" not in r for r in rows)


async def test_a_failed_sub_agent_sweep_is_said_so_not_nothing_running(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An idle helper's sub-agents could not be cancelled (the task store was busy):
    they may run on, so neither Stop answers as if nothing were running."""
    _world(env, monkeypatch, **{"chat-h1": False})
    assert env.state is not None

    class _Busy(_Subagents):
        async def cancel_for_parent(self, parent: str) -> tuple[int, int]:
            self.parents.append(parent)
            if parent == "chat-h1":
                raise RuntimeError("task store busy")
            return (0, 0)

    env.state.subagents = _Busy()  # type: ignore[attr-defined]
    monkeypatch.setattr("kiro_crew.dashboard.chat_utils.effective_session_key", lambda s: s.key)
    await _bang_stop(env)
    event = {"type": "agent_session_stopped", "channel": "C0CODE", "user": OWNER}
    assert await routing.handle_session_stopped(env.orch, event, OWNER) == "stopped"
    assert (
        _posts(env)
        == ["Stopping the sub-agents of 1 session failed partway, so some may still be running."]
        * 2
    )


async def test_a_turn_stopped_before_it_dispatched_is_counted(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``stop_slot_turn``'s "no active turn": no provider turn to cancel yet (a cold
    start), but the press ends the turn at its dispatch gate, so it was stopped."""
    w = _world(env, monkeypatch, **{"chat-h1": True, "chat-h2": True})
    w["slots"][CHANNEL_SESSION].running = True
    w["answers"][CHANNEL_SESSION] = {"ok": True, "info": "no active turn"}
    w["answers"]["chat-h1"] = {"ok": True, "info": "no active turn"}
    await _bang_stop(env)
    assert [k for k, _ in w["stopped"]] == [CHANNEL_SESSION, "chat-h1", "chat-h2"]
    assert _posts(env) == ["Stopped 3: this channel's session and 2 helper agents."]


async def test_a_helper_whose_turn_ended_then_restarted_during_the_stop_is_stopped(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Its stop call found no turn, and a new one started before the second read."""
    w = _world(env, monkeypatch, **{"chat-h1": True})
    calls: list[str] = []

    async def stop_slot_turn(state: Any, slot: Any, **kwargs: Any) -> dict[str, Any]:
        w["stopped"].append((slot.key, kwargs))
        calls.append(slot.key)
        if calls.count("chat-h1") == 1:
            # Idle at the call; a queued wake starts a new turn right after.
            return {"ok": True, "info": "not running"}
        return {"ok": True, "info": "stopping"}

    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.stop_slot_turn", stop_slot_turn)
    await _bang_stop(env)
    assert calls == ["chat-h1", "chat-h1"]
    assert _posts(env) == ["Stopped 1: 1 helper agent."]


async def test_a_helper_stop_call_cancelled_midway_is_said_and_the_rest_still_stop(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stop call that ends in CancelledError (its provider call was cancelled) is
    not a stop; the others stop, are audited, and the reply goes out."""
    import asyncio

    w = _world(env, monkeypatch, **{"chat-h1": True, "chat-h2": True})

    async def stop_slot_turn(state: Any, slot: Any, **kwargs: Any) -> dict[str, Any]:
        w["stopped"].append((slot.key, kwargs))
        if slot.key == "chat-h1":
            raise asyncio.CancelledError
        return {"ok": True, "info": "stopping"}

    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.stop_slot_turn", stop_slot_turn)
    await _bang_stop(env)
    assert _posts(env) == ["Stopped 1: 1 helper agent. 1 helper agent could not be stopped."]
    rows = [r["resources"] for r in env.sel.ops("slack.code_channel.stop_command")]
    assert any("slot=chat-h1 result=error" in r for r in rows)
    assert any("slot=chat-h2 result=stopping" in r for r in rows)


async def test_a_sub_agent_sweep_cancelled_midway_is_said(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    _world(env, monkeypatch, **{"chat-h1": True})
    assert env.state is not None

    class _Cancelled(_Subagents):
        async def cancel_for_parent(self, parent: str) -> tuple[int, int]:
            self.parents.append(parent)
            if parent == "chat-h1":
                raise asyncio.CancelledError
            return (0, 0)

    env.state.subagents = _Cancelled()  # type: ignore[attr-defined]
    monkeypatch.setattr("kiro_crew.dashboard.chat_utils.effective_session_key", lambda s: s.key)
    await _bang_stop(env)
    assert _posts(env) == [
        "Stopped 1: 1 helper agent. Stopping the sub-agents of 1 session failed partway, "
        "so some may still be running."
    ]


async def test_a_sweep_that_failed_partway_is_said_even_when_a_later_sweep_works(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first sweep cancelled a queued sub-agent and then raised, so its count
    is lost; the final sweep finds nothing left. Neither "Nothing running." nor
    silence: the person is told the sweep failed partway."""
    _world(env, monkeypatch, **{"chat-h1": False})
    assert env.state is not None

    class _FailsOnce(_Subagents):
        async def cancel_for_parent(self, parent: str) -> tuple[int, int]:
            self.parents.append(parent)
            if parent == "chat-h1" and self.parents.count(parent) == 1:
                raise RuntimeError("task store busy after the first row")
            return (0, 0)

    env.state.subagents = _FailsOnce()  # type: ignore[attr-defined]
    monkeypatch.setattr("kiro_crew.dashboard.chat_utils.effective_session_key", lambda s: s.key)
    await _bang_stop(env)
    assert _posts(env) == [
        "Stopping the sub-agents of 1 session failed partway, so some may still be running."
    ]


async def test_a_retried_stop_that_is_cancelled_is_audited_as_an_error(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first call found the helper idle; it started again, and the retry was
    cancelled midway. Its audit row says error, not the first call's "not running"."""
    import asyncio

    _world(env, monkeypatch, **{"chat-h1": True})
    calls: list[str] = []

    async def stop_slot_turn(state: Any, slot: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(slot.key)
        if len(calls) == 1:
            return {"ok": True, "info": "not running"}
        raise asyncio.CancelledError

    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.stop_slot_turn", stop_slot_turn)
    await _bang_stop(env)
    assert _posts(env) == ["1 helper agent could not be stopped."]
    rows = [r["resources"] for r in env.sel.ops("slack.code_channel.stop_command")]
    assert any("slot=chat-h1 result=error" in r for r in rows)


async def test_a_refused_channel_session_stop_is_said_not_counted(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = _world(env, monkeypatch, **{"chat-h1": True})
    w["slots"][CHANNEL_SESSION].running = True
    w["answers"][CHANNEL_SESSION] = {"ok": False, "error": "refused", "code": "owner_only"}
    await _bang_stop(env)
    assert _posts(env) == [
        "Stopped 1: 1 helper agent. This channel's session could not be stopped."
    ]
    rows = [r["resources"] for r in env.sel.ops("slack.code_channel.stop_command")]
    assert any(f"slot={CHANNEL_SESSION} result=refused" in r for r in rows)


async def test_a_running_session_the_board_shows_but_the_stop_does_not_reach_is_said(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A chat filed in the channel's folder is on the board but is not one of its
    helper agents, so the Stop leaves it running and says so, never "Nothing running."
    Its own idle helper, the channel's session and a board the scope refuses add none."""
    from kiro_crew.slack.code_channels.workspace import core as workspace

    w = _world(env, monkeypatch, **{"chat-h1": False})
    w["slots"]["chat-filed"] = _slot("chat-filed", True)
    w["slots"]["chat-filed-idle"] = _slot("chat-filed-idle", False)
    admitted = {"on": True}

    def watched(binding: Any) -> set[str]:
        return {"chat-h1", "chat-filed", "chat-filed-idle"} if admitted["on"] else set()

    service = SimpleNamespace(
        watched_sessions=watched,
        _board_still_admitted=lambda binding: admitted["on"],
        board=lambda team, channel, session: SimpleNamespace(rows=[]),
        _forget_worker_status=lambda binding: None,
    )
    monkeypatch.setattr(workspace, "service", lambda: service)
    left = (
        "1 other session on the work board is still running: Stop reaches only this "
        "channel's session and its helper agents."
    )
    await _bang_stop(env, "301.0")
    event = {"type": "agent_session_stopped", "channel": "C0CODE", "user": OWNER}
    assert await routing.handle_session_stopped(env.orch, event, OWNER) == "stopped"
    assert _posts(env) == [left, left]
    assert w["stopped"] == []  # not the Stop's to stop
    admitted["on"] = False
    await _bang_stop(env, "302.0")
    assert _posts(env)[-1] == "Nothing running."


async def _stop_while_the_first_sweep_waits(
    env: Env, monkeypatch: pytest.MonkeyPatch, w: dict[str, Any]
) -> None:
    """``!stop``, with the channel's session starting a turn while the first sub-agent
    sweep is awaited (the person's next message), released only after that."""
    import asyncio

    from kiro_crew.testing.wait import await_until

    assert env.state is not None
    swept, release = asyncio.Event(), asyncio.Event()

    class _Held(_Subagents):
        async def cancel_for_parent(self, parent: str) -> tuple[int, int]:
            if not swept.is_set():
                swept.set()
                await release.wait()
            return await super().cancel_for_parent(parent)

    env.state.subagents = _Held()  # type: ignore[attr-defined]
    monkeypatch.setattr("kiro_crew.dashboard.chat_utils.effective_session_key", lambda s: s.key)
    stop = asyncio.ensure_future(_bang_stop(env))
    try:
        await await_until(swept, what="the Stop's first sub-agent sweep")
        w["slots"][CHANNEL_SESSION].running = True  # a turn runs now, whatever it was at the press
    finally:
        release.set()
    await await_until(stop, what="the Stop")


async def test_a_turn_the_channel_starts_during_the_first_sweep_is_not_stopped(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Idle at the press: the turn the next message starts while the sweep is awaited
    began after the Stop, so it runs on and the reply does not count it."""
    w = _world(env, monkeypatch, **{"chat-h1": True})
    await _stop_while_the_first_sweep_waits(env, monkeypatch, w)
    assert [k for k, _ in w["stopped"]] == ["chat-h1"]
    assert _posts(env) == ["Stopped 1: 1 helper agent."]


async def test_a_channel_session_running_at_the_press_is_stopped_after_the_first_sweep(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = _world(env, monkeypatch, **{"chat-h1": True})
    w["slots"][CHANNEL_SESSION].running = True
    await _stop_while_the_first_sweep_waits(env, monkeypatch, w)
    assert [k for k, _ in w["stopped"]] == [CHANNEL_SESSION, "chat-h1"]
    assert _posts(env) == ["Stopped 2: this channel's session and 1 helper agent."]


async def test_a_turn_the_channel_starts_during_the_stop_keeps_its_status(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The channel's session was idle at the press; the person's next message
    started a turn while a helper's stop waited out its budget. The settle back
    to ``active`` would hide that turn's loader and Stop button, so it is skipped."""
    w = _world(env, monkeypatch, **{"chat-h1": True})
    settled: list[str] = []

    async def stop_slot_turn(state: Any, slot: Any, **kwargs: Any) -> dict[str, Any]:
        w["stopped"].append((slot.key, kwargs))
        w["slots"][CHANNEL_SESSION].running = True  # the next message's turn starts
        return {"ok": True, "info": "stopping"}

    async def settle(channel: str) -> None:
        settled.append(channel)

    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.stop_slot_turn", stop_slot_turn)
    monkeypatch.setattr("kiro_crew.slack.code_channels.status.settle_after_stop", settle)
    await _bang_stop(env)
    assert settled == []
    assert _posts(env) == ["Stopped 1: 1 helper agent."]
