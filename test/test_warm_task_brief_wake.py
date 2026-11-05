"""The warm brief's wake turn (RFC ``rfc-delayed-task-brief.md`` sections 3.8
and 3.8.1): one read-only turn before the one-call brief, its
checks backed by the tool-call ids that ran, capped per chat and day, and the
one-call brief on any timeout, failure, cap or typed row.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_warm_task_brief import (
    _arm_model,
    _claimed,
    _foreground_slot,
    _posted,
    _snapshot,
    _State,
)

from kiro_crew.acp_backends import ACP_BACKEND_KIRO
from kiro_crew.dashboard import warm_task_brief
from kiro_crew.dashboard import warm_task_brief_facts as facts
from kiro_crew.dashboard import warm_task_brief_inputs as inputs
from kiro_crew.dashboard import warm_task_brief_wake as wake
from kiro_crew.testing.wait import DEFAULT_INTERVAL_SECS, async_wait_until

_CFG = SimpleNamespace(
    agent=SimpleNamespace(acp_backend=ACP_BACKEND_KIRO, resolve_model=lambda role: "background")
)
_BRIEF = {
    "headline": "Back.",
    "lines": [{"text": "The deploy is live", "kind": "deploy", "facts": ["c1"]}],
    "checks": [
        {"id": "c1", "kind": "deploy", "label": "prod deploy", "result": "live",
         "tool_calls": ["t2"]},
    ],
}  # fmt: skip


class _WakeState(_State):
    def __init__(self) -> None:
        super().__init__()
        self.context_builder = SimpleNamespace(hooks=object())
        self.background_turns = 0

    async def run_background_turn(self, slot, coro):
        self.background_turns += 1
        return await coro


@pytest.fixture(autouse=True)
def _fresh_wake_state(_floor_monkeypatch):
    _floor_monkeypatch.setattr(wake, "_wake_days", {})
    _floor_monkeypatch.setattr(wake, "_stops", {})
    # The member-aware selector reads agent files; each test names its backend.
    _floor_monkeypatch.setattr(wake, "_backend", lambda cfg, slot: cfg.agent.acp_backend)


def _fake_turn(monkeypatch, text, *, ran=("id-a", "id-b"), done=("id-a", "id-b"), block=False):
    async def turn(state, slot, cfg, backend, prompt, calls):
        assert "Do not use tools." not in prompt
        calls.order.extend(ran)
        calls.done.update(done)
        if block:
            await asyncio.Event().wait()  # never set: the turn runs until cancelled
        if isinstance(text, Exception):
            raise text
        return text

    monkeypatch.setattr(wake, "_turn", turn)


def test_the_wake_prompt_rewords_every_tool_ban_of_the_one_call_prompt():
    prompt = warm_task_brief._build_prompt(_snapshot())
    for old, _new in wake.WAKE_EDITS:
        assert old in prompt
    woken = wake.wake_prompt(prompt)
    assert "Do not use tools." not in woken and "nothing checks them yet" not in woken
    assert '"checks": [<your checks>]' in woken and "at most 15" in woken


def _brief(lines):
    return warm_task_brief.Brief("on_track", "h", "", "", (), (), (), (), lines=tuple(lines))


def test_a_proven_check_takes_the_next_id_and_backs_its_line():
    base = facts.checked_facts((), ({"item_id": "it_0000000a", "title": "Ship", "state": "open"},))
    lines = [
        {"text": "deployed", "kind": "deploy", "facts": ["c1"]},
        {"text": "file read", "kind": "file", "facts": ["c2"]},  # its call failed
        {"text": "a guess", "kind": "deploy", "facts": ["f2"]},  # f2 did not exist
        {"text": "ticket", "kind": "ticket", "facts": ["c3"]},  # wrong kind
    ]
    checks = [
        {"id": "c1", "kind": "deploy", "label": "prod", "result": "live", "tool_calls": ["t1"]},
        {"id": "c1", "kind": "deploy", "label": "dup", "result": "x", "tool_calls": ["t1"]},
        {"id": "c2", "kind": "file", "label": "a.py", "result": "has it", "tool_calls": ["t2"]},
        {"id": "c3", "kind": "pr", "label": "#1", "result": "merged", "tool_calls": ["t1"]},
        {"id": "c4", "kind": "file", "label": "b.py", "result": "x", "tool_calls": ["t9"]},
        {"id": "c5", "kind": ["file"], "label": "c.py", "result": "x", "tool_calls": ["t1"]},
    ]
    calls = wake.Calls(order=["id-a", "id-b"], done={"id-a"})
    brief, table = wake.with_checks(_brief(lines), json.dumps({"checks": checks}), calls, base)
    assert [f["id"] for f in table] == ["f1", "f2"]
    assert table[1] == {
        "id": "f2",
        "kind": "deploy",
        "label": "prod",
        "values": {"result": "live"},
        "source": "wake",
        "evidence": ["id-a"],
    }
    # Only the line citing the proven check stays; the old f2 cannot ride on it.
    assert facts.typed_lines(brief.lines, table) == (
        {"text": "deployed", "kind": "deploy", "facts": ["f2"]},
    )
    assert facts.card_facts(table)[1]["evidence"] == ["id-a"]


def test_a_check_result_is_shown_as_the_model_wrote_it():
    # Model text, not a gateway enum: never lower-cased, de-underscored or translated.
    assert facts.fact_words({"result": "Deployed build_42 to PROD"}) == [
        "Deployed build_42 to PROD"
    ]
    assert facts.fact_words({"result": "UNKNOWN"}) == ["UNKNOWN"]


def test_no_completed_call_means_no_check_and_no_file_line():
    lines = [{"text": "deployed", "kind": "deploy", "facts": ["c1"]}]
    brief, table = wake.with_checks(_brief(lines), json.dumps(_BRIEF), wake.Calls(), [])
    assert table == [] and facts.typed_lines(brief.lines, table) == ()
    assert wake.with_checks(_brief(lines), json.dumps(_BRIEF), None, [])[1] == []


@pytest.mark.asyncio
async def test_a_wake_returns_its_brief_and_keeps_its_calls(monkeypatch):
    _fake_turn(monkeypatch, json.dumps(_BRIEF))
    state, slot, calls = _WakeState(), _foreground_slot(_WakeState()), wake.Calls()
    assert await wake.run(state, slot, _CFG, "prompt", calls) == json.dumps(_BRIEF)
    assert calls.order == ["id-a", "id-b"] and state.background_turns == 1
    assert slot.key not in wake._stops


@pytest.mark.parametrize(
    "outcome", [RuntimeError("boom"), "not json", json.dumps({"lines": []})], ids=str
)
@pytest.mark.asyncio
async def test_a_failed_wake_or_one_with_no_brief_backs_nothing(monkeypatch, outcome):
    _fake_turn(monkeypatch, outcome)
    state, calls = _WakeState(), wake.Calls()
    assert await wake.run(state, _foreground_slot(state), _CFG, "prompt", calls) == ""
    assert calls.order == [] and calls.done == set()


@pytest.mark.asyncio
async def test_a_wake_past_the_deadline_is_cancelled(monkeypatch):
    monkeypatch.setattr(wake, "WARM_TASK_BRIEF_WAKE_TIMEOUT_SECS", DEFAULT_INTERVAL_SECS)
    _fake_turn(monkeypatch, json.dumps(_BRIEF), block=True)
    state, calls = _WakeState(), wake.Calls()
    assert await wake.run(state, _foreground_slot(state), _CFG, "prompt", calls) == ""
    assert calls.done == set()
    await async_wait_until(lambda: not wake._reaping)


@pytest.mark.asyncio
async def test_a_typed_row_stops_the_wake_and_no_one_call_brief_is_made(monkeypatch):
    _fake_turn(monkeypatch, json.dumps(_BRIEF), block=True)
    state = _WakeState()
    slot = _foreground_slot(state)
    task = asyncio.ensure_future(wake.run(state, slot, _CFG, "prompt", wake.Calls()))
    state._warm_task_brief_tasks[slot.key] = task
    state._warm_task_brief_model_active.add(slot.key)
    await async_wait_until(lambda: slot.key in wake._stops)
    warm_task_brief.cancel_for_live_user_row(state, slot.key, "hello")
    # None, not "": the caller makes no one-call brief that could never post.
    assert await task is None and not task.cancelled()
    assert wake.stop(slot.key) is False
    assert slot.key in state._warm_task_brief_abandoned


@pytest.mark.asyncio
async def test_a_reset_during_the_wake_stops_it_and_abandons_the_brief(monkeypatch):
    _fake_turn(monkeypatch, json.dumps(_BRIEF), block=True)
    state = _WakeState()
    slot = _foreground_slot(state)
    task = asyncio.ensure_future(wake.run(state, slot, _CFG, "prompt", wake.Calls()))
    state._warm_task_brief_tasks[slot.key] = task
    # The brief's model call holds the key while the wake runs (``_generate``).
    state._warm_task_brief_model_active.add(slot.key)
    await async_wait_until(lambda: slot.key in wake._stops)
    warm_task_brief.cancel_warm_task_brief(state, slot.key)
    assert await task is None
    assert slot.key in state._warm_task_brief_abandoned


@pytest.mark.asyncio
async def test_a_reset_while_the_harness_resolves_starts_no_wake(monkeypatch):
    _fake_turn(monkeypatch, json.dumps(_BRIEF))
    state = _WakeState()
    slot = _foreground_slot(state)

    def resolve(cfg, slot_):
        # The owner leaves before any stop event exists: only the abandoned set records it.
        state._warm_task_brief_abandoned.add(slot_.key)
        return cfg.agent.acp_backend

    monkeypatch.setattr(wake, "_backend", resolve)
    assert await wake.run(state, slot, _CFG, "prompt", wake.Calls()) is None
    assert state.background_turns == 0 and wake._wake_days == {}


def test_the_wake_runs_only_on_the_harness_its_session_starts_on():
    # A crewmate pinned or routed elsewhere than the default gets the one-call brief.
    state = _WakeState()
    assert wake._may_wake(state, _CFG, ACP_BACKEND_KIRO)
    assert not wake._may_wake(state, _CFG, "claude")
    assert not wake._may_wake(state, _CFG, None)
    # A dashboard surface for governance and the audit log, apart from the chat.
    from kiro_crew.sel import _infer_source

    assert wake.session_key("member-alice") == "thread:member-alice:warm-wake"
    assert _infer_source(wake.session_key("member-alice")) == "dashboard"


def test_words_a_wake_check_wrote_are_scanned_like_model_text():
    checked = (
        {"id": "f1", "kind": "task", "label": "Ship #12", "source": "tasks"},
        {"id": "f2", "kind": "deploy", "label": "prod", "values": {"result": "via #987654"},
         "source": "wake"},
    )  # fmt: skip
    brief = replace(_brief([]), checked=checked)
    written = warm_task_brief._posted_model_text(brief)
    assert "via #987654" in written and "Ship #12" not in written


@pytest.mark.asyncio
async def test_six_wakes_a_day_then_the_one_call_brief(monkeypatch):
    _fake_turn(monkeypatch, json.dumps(_BRIEF))
    # One fixed instant, so the seven wakes never straddle local midnight.
    clock = SimpleNamespace(
        time=lambda: 1_800_000_000.0, strftime=time.strftime, localtime=time.localtime
    )
    monkeypatch.setattr(wake, "time", clock)
    state = _WakeState()
    slot = _foreground_slot(state)
    texts = [await wake.run(state, slot, _CFG, "p", wake.Calls()) for _ in range(7)]
    assert texts.count("") == 1 and texts[-1] == "" and state.background_turns == 6
    # No wake on a retry, a harness with no read-only mode, or no hook gate.
    assert await wake.run(_WakeState(), slot, _CFG, "p", None) == ""
    other = SimpleNamespace(agent=SimpleNamespace(acp_backend="codex"))
    assert (
        await wake.run(_WakeState(), _foreground_slot(_WakeState()), other, "p", wake.Calls()) == ""
    )
    assert await wake.run(_State(), _foreground_slot(_State()), _CFG, "p", wake.Calls()) == ""


@pytest.mark.asyncio
async def test_the_turn_is_read_only_capped_and_its_session_destroyed(monkeypatch):
    from kiro_crew import agent_discovery, llm_helpers
    from kiro_crew.agent_sdk.drivers.acp_turn_vocab import EVENT_TOOL_CALL, EVENT_TOOL_RESULT
    from kiro_crew.config import loader
    from kiro_crew.dashboard import side_readonly_spec

    seen: dict = {}

    async def warm(*args, **kwargs):
        return None

    async def stream(provider, prompt, **kwargs):
        seen.update(kwargs)
        for event in (
            SimpleNamespace(kind=EVENT_TOOL_CALL, tool_call_id="id-a", tool_final=False),
            SimpleNamespace(kind=EVENT_TOOL_CALL, tool_call_id="id-b", tool_final=False),
            SimpleNamespace(kind=EVENT_TOOL_RESULT, tool_call_id="id-b", tool_final=True),
        ):
            await kwargs["on_event"](event)
        return "{}"

    class _Sessions:
        def __init__(self):
            self.log: list = []

        async def get_or_create(self, key, **kwargs):
            self.log.append(("create", key, kwargs["agent"]))
            return object(), True, False

        def release(self, key):
            self.log.append(("release", key))

        async def destroy(self, key):
            self.log.append(("destroy", key))

    monkeypatch.setattr(agent_discovery, "warm_project_agent_names", warm)
    monkeypatch.setattr(
        loader,
        "resolve_agent_bindings",
        lambda *a, **k: SimpleNamespace(kiro_agent="mate", requested_resolved=True),
    )
    monkeypatch.setattr(
        side_readonly_spec,
        "publish_readonly_spec",
        lambda name, project: SimpleNamespace(name=f"{name}-ro"),
    )
    monkeypatch.setattr(llm_helpers, "stream_and_collect", stream)
    state = _WakeState()
    state.sessions = _Sessions()
    slot = SimpleNamespace(key="thread-x", project="", agent="mate", _app="")
    calls = wake.Calls()
    assert await wake._turn(state, slot, _CFG, ACP_BACKEND_KIRO, "p", calls) == "{}"
    assert seen["approval_policy"] == llm_helpers.ToolApprovalPolicy.READ_ONLY
    assert (
        seen["max_turns"] == wake.WARM_TASK_BRIEF_WAKE_MAX_TOOL_CALLS == 15
        and seen["unattended"] is True
        and seen["retry_transient"] is False
    )
    assert calls.order == ["id-a", "id-b"] and calls.done == {"id-b"}
    key = "thread:thread-x:warm-wake"
    assert state.sessions.log == [("create", key, "mate-ro"), ("release", key), ("destroy", key)]
    # The backend the member route picked decides the confinement, not the global one.
    from kiro_crew.acp_backends import ACP_BACKEND_GOOSE
    from kiro_crew.agent_sdk.drivers import acp

    monkeypatch.setattr(acp, "goose_side_home_supported", lambda: True)
    state.sessions = _Sessions()
    created: dict = {}
    real_create = state.sessions.get_or_create

    async def create(key, **kwargs):
        created.update(kwargs)
        return await real_create(key, **kwargs)

    state.sessions.get_or_create = create
    assert await wake._turn(state, slot, _CFG, ACP_BACKEND_GOOSE, "p", wake.Calls()) == "{}"
    assert created["side_read_only"] is True and created["agent"] == "mate"


@pytest.mark.asyncio
async def test_a_return_posts_the_wakes_brief_with_its_deploy_line(monkeypatch):
    monkeypatch.setattr(inputs, "_live_cache", {})
    monkeypatch.setattr(inputs, "probe_github", lambda urls: {})
    one_call: list[str] = []

    async def run_model(*args, **kwargs):
        one_call.append("model")
        return json.dumps({"headline": "Fallback."})

    state = _WakeState()
    slot = _foreground_slot(state)
    base = _snapshot()
    _arm_model(monkeypatch, slot, [base, _claimed(base), _claimed(base)], run_model)
    monkeypatch.setattr(warm_task_brief.KiroCrewConfig, "load", lambda: _CFG)
    _fake_turn(monkeypatch, json.dumps(_BRIEF))
    await warm_task_brief._prepare_warm_task_brief(state, slot)
    (row,) = _posted(slot)
    brief = row["meta"]["brief"]
    assert one_call == [] and brief["headline"] == "Back."
    assert brief["lines"] == [{"text": "The deploy is live", "kind": "deploy", "facts": ["f1"]}]
    assert brief["checked"][0]["evidence"] == ["id-b"]

    # A failed wake falls back to the one-call brief once, and no deploy line stays.
    state2 = _WakeState()
    slot2 = _foreground_slot(state2)
    _arm_model(monkeypatch, slot2, [base, _claimed(base), _claimed(base)], run_model)
    monkeypatch.setattr(warm_task_brief.KiroCrewConfig, "load", lambda: _CFG)
    _fake_turn(monkeypatch, RuntimeError("down"))
    await warm_task_brief._prepare_warm_task_brief(state2, slot2)
    assert one_call == ["model"]
    assert [r["meta"]["brief"]["headline"] for r in _posted(slot2)] == ["Fallback."]
