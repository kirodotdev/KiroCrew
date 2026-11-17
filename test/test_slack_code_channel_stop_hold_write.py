"""A held turn starts no work: the hold is asked where each piece of a turn's work begins
(``stop_hold``, r10).

The dispatch gate asks once, but a turn's work also begins before it (the AgentSpawn and
UserPromptSubmit hook commands) and after it (the prompt write, behind the transport's own
awaits). Each is asked again where it begins, synchronously: a hook command right before
its spawn, the prompt under the transport's write lock (``prompt_gate``).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew.dashboard import chat_runner
from kiro_crew.slack.code_channels import stop_hold

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _no_pending() -> Any:
    stop_hold._keys.clear()
    stop_hold._channels.clear()
    yield
    stop_hold._keys.clear()
    stop_hold._channels.clear()


def _slot(key: str) -> Any:
    return SimpleNamespace(key=key, _stop_held=False, _stop_hold_seq=0)


def _press(slot: Any) -> None:
    """A Stop on *slot*: a newer press than any stamp read before it."""
    stop_hold._last = next(stop_hold._presses)
    stop_hold._hold(slot, stop_hold._last)


async def test_a_stop_in_the_transports_wait_writes_no_prompt() -> None:
    """r9 GPT A: the gate passed while free, then the prompt waits behind another
    writer's frame (one of the transport's awaits). A Stop there is asked under the
    write lock: nothing is written and the turn is told it was held."""
    from acp_runtime_helpers import _make_runtime, _register

    from kiro_crew.acp.runtime import AcpSessionHandle
    from kiro_crew.testing.wait import await_until, until_parked

    rt, _, proc = _make_runtime()
    queues = _register(rt, "sA")
    handle = AcpSessionHandle("sA", queues["sA"], rt)
    slot = _slot("chat-held-1")
    ask = stop_hold.write_gate(_real_client(), slot, "")["prompt_gate"]
    lock = rt._stdin_write_lock()
    await lock.acquire()  # another session's frame holds the shared stdin
    first = asyncio.ensure_future(handle.prompt("hi", prompt_gate=ask).__anext__())
    await until_parked(lock)
    _press(slot)  # the Stop lands while the prompt waits
    lock.release()
    with pytest.raises(stop_hold.HeldAtWrite):
        await await_until(first, what="the held prompt's refusal")
    proc.stdin.write.assert_not_called()
    assert rt._routed_requests == {} and handle.is_turn_active is False


def _real_client() -> Any:
    from kiro_crew.providers.acp import AcpProvider

    return object.__new__(AcpProvider)


async def test_the_write_gate_goes_to_both_real_providers_and_admits_a_release() -> None:
    from kiro_crew.acp.session_provider import AcpSessionProvider

    slot = _slot("chat-h-2")
    assert stop_hold.write_gate(object(), slot, "") == {}, "a client without the keyword"
    shared = stop_hold.write_gate(object.__new__(AcpSessionProvider), slot, "")
    shared["prompt_gate"]()  # free: nothing raised
    _press(slot)
    person = stop_hold.stamp(stop_hold.seen(), True)  # a person's, after the Stop
    stop_hold.write_gate(_real_client(), slot, person)["prompt_gate"]()
    with pytest.raises(stop_hold.HeldAtWrite):
        shared["prompt_gate"]()


async def test_a_turn_held_at_the_write_ends_quietly_as_stopped() -> None:
    """The write's refusal ends the turn quietly: no error card, no reply, and the turn
    the gate opened closes once, as stopped (r10 Opus: not refused AND failed)."""
    from turn_harness import Raise, TurnScript, run_turn

    record = await run_turn(TurnScript(events=[Raise(stop_hold.HeldAtWrite("chat-1"))]))
    assert [row["role"] for row in record.history_rows] == ["user"]
    assert record.crew("on_turn_refused") == []
    assert record.stop_reason == "failed: channel_stopped"


async def test_a_hook_command_never_spawns_once_the_stop_lands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """r9 GPT B: a hook's vetting awaits (governance, the deny list, the sandbox argv).
    A Stop at any of them spawns no command: the ask is after the last await."""
    from test_slack_code_channel_stop_hold import _stepped

    import kiro_crew.hook_event_governance as governance
    import kiro_crew.sandbox as sandbox
    from kiro_crew import hooks
    from kiro_crew.hooks import HOOK_EVENT_USER_PROMPT_SUBMIT

    stopped = [False]
    spawned: list[bool] = []

    async def spawn(*_a: Any, **_k: Any) -> Any:
        spawned.append(stopped[0])
        raise OSError("not spawned in this test")

    monkeypatch.setattr(sandbox, "create_subprocess_limited", spawn)
    monkeypatch.setattr(sandbox, "wrap_argv", lambda argv, **k: (list(argv), None))
    monkeypatch.setattr(sandbox, "cgroup_scope_argv", lambda argv: list(argv))
    monkeypatch.setattr("kiro_crew.hooks._script_hooks_capability_denied", lambda _sk: None)
    monkeypatch.setattr(governance, "event_refusal", lambda *_a: None)
    monkeypatch.setattr(governance, "hook_refusal", lambda *_a: None)
    hook = hooks.ScriptHook(id="h", name="h", event=HOOK_EVENT_USER_PROMPT_SUBMIT, command="true")

    def run(at: int) -> Any:
        stopped[0] = False
        coro = hooks.run_script_hook(hook, "ctx", admit=lambda: not stopped[0])
        return _stepped(coro, at, lambda: stopped.__setitem__(0, True))

    _, total = await run(-1)
    assert spawned == [False] and total > 1, (spawned, total)
    for at in range(total):
        spawned.clear()
        result, _ = await run(at)
        assert True not in spawned, f"spawned after a Stop at await {at}"
        assert spawned or (result.refused and result.exit_code == 2), at


async def test_a_turn_held_as_it_starts_prepares_claims_and_announces_nothing() -> None:
    """r10 sweep: held before its first await, a turn claims no session (no provider
    starts), fires no hook, posts no status and writes no prompt."""
    from turn_harness import TurnScript, run_turn

    def held(ctx: Any) -> None:
        stop_hold._keys[ctx.slot.key] = next(stop_hold._presses)

    record = await run_turn(TurnScript(events=[], setup=held))
    assert not record.rows("error") and not record.rows("assistant")
    assert [call.args[2] for call in record.crew("on_turn_refused")] == ["channel_stopped"]
    assert record.provider_calls == () and record.allocations == [], "no claim, no provider"
    assert not [f for f in record.frames("chat_status") if "Thinking" in str(f)]


@pytest.mark.parametrize("person", [True, False])
async def test_a_persons_continue_on_a_paused_held_member_sends_its_restored_queue(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, person: bool
) -> None:
    """r10 GPT B 4: a helper held by both Pause all and the Stop, its queue restored
    from disk. The person's Continue releases the pause and drains the queue: its
    arrival stamp goes with that drain, so the Stop's hold is released too, and the
    handler never answers ok with nothing run: an internal caller's is refused, and the
    pause keeps its hand-back."""
    import json

    from aiohttp import web
    from aiohttp.test_utils import make_mocked_request
    from chat_test_helpers import _make_state, drain_background_tasks

    from kiro_crew import crew_mode, crew_pause
    from kiro_crew.dashboard import chat_handlers  # noqa: F401 -- binds the handlers' globals
    from kiro_crew.dashboard.chat_api import continue_turn
    from kiro_crew.dashboard.slot_queue_repository import RESTORED_QUEUE_KEY

    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    monkeypatch.setattr(crew_pause, "_path", lambda: tmp_path / "crew-pause.json")
    monkeypatch.setattr(crew_pause, "_loaded_from", None)
    monkeypatch.setattr(crew_pause, "_records", {})
    monkeypatch.setattr(crew_pause, "_resuming", {})
    monkeypatch.setattr(crew_pause, "_unsaved", False)
    monkeypatch.setattr(crew_mode, "_known_roots", set())
    monkeypatch.setattr(crew_mode, "persisted_parents", lambda: {})
    runs: list[dict[str, Any]] = []

    def run_chat(*_a: Any, **kw: Any) -> Any:
        runs.append(kw)
        return asyncio.sleep(0)

    monkeypatch.setattr("kiro_crew.dashboard.chat_runner._run_chat", run_chat)
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-w1")
    slot._queue = [{"id": "q-restored", "content": "next step", RESTORED_QUEUE_KEY: True}]
    crew_pause.pause("chat-root", ["chat-w1"], ["chat-w1"])
    _press(slot)  # the Stop holds it too
    app = web.Application()
    app["state"] = state
    req = make_mocked_request("POST", "/x", app=app, match_info={"slot": "chat-w1"})
    req["app"], req["is_dashboard_user"], req["internal_auth"] = "", person, not person
    req[stop_hold.STOP_SEEN] = stop_hold.seen()  # the Continue arrived after the Stop
    resp = await continue_turn.api_chat_slot_continue(req)
    await drain_background_tasks(state)
    if not person:
        assert resp.status == 409 and json.loads(resp.text)["code"] == "channel_stopped"
        assert runs == [] and crew_pause.holds_turn("chat-w1"), "still paused, nothing run"
        return
    assert resp.status == 200 and json.loads(resp.text)["ok"] is True
    stamps = [str(r.get("_stop_hold_stamp") or "") for r in runs]
    assert stamps and stop_hold.decide(slot, stamps[0]) != stop_hold.HELD, (stamps, runs)


# ── each ask, by what a turn does when a Stop lands right before it ─────────────
# A Stop is pressed inside the await just before an ask (the turn's own callee,
# wrapped on the runner's globals, as the composition reads it), and the turn's
# record shows what that ask kept from starting.


def _stop_now(key: str) -> None:
    stop_hold._last = next(stop_hold._presses)
    stop_hold._keys[key] = stop_hold._last


class _Hooks:
    """A hook store whose every event has one command: records each fire, and each
    spawn the turn's ``admit`` let through (``run_script_hook`` asks it at the spawn).
    *stop* runs once, at event *on*, before its spawn's ask or (*after*) after its spawn."""

    def __init__(self, stop: Any = None, on: str = "", after: bool = False) -> None:
        self.fired: list[str] = []
        self.spawned: list[str] = []
        self.stop, self.on, self.after = stop, on, after

    async def fire(self, event: str, *_a: Any, admit: Any = None, **_k: Any) -> list[Any]:
        self.fired.append(event)
        stop = self.stop if event == self.on else None
        if stop and not self.after:
            stop()
        if admit is None or admit():
            self.spawned.append(event)
            if stop and self.after:
                stop()
        return []


def _pressing(real: Any, key: list[str], pressed: list[int]) -> Any:
    """*real* (a runner callee) with a Stop landing inside its await, counted in *pressed*."""

    async def inside(*a: Any, **k: Any) -> Any:
        _stop_now(key[0])
        pressed.append(1)
        return await real(*a, **k)

    return inside


async def _turn(key: list[str], hooks: Any = None, held: bool = False, **run: Any) -> Any:
    """One harness turn in a fresh slot; *key* gets the slot's key at setup."""
    from turn_harness import TurnScript, run_turn

    from kiro_crew.acp.types import EVENT_TEXT_CHUNK, AcpEvent

    def setup(ctx: Any) -> None:
        key.append(ctx.slot.key)
        if hooks is not None:
            ctx.state._hook_store = hooks
        if held:
            _stop_now(ctx.slot.key)

    reply = AcpEvent(kind=EVENT_TEXT_CHUNK, text="the reply")
    return await run_turn(TurnScript(events=[reply], setup=setup, run_kwargs=run))


def _wrote_prompt(record: Any) -> bool:
    return bool(record.calls("stream")) or bool(record.rows("assistant"))


async def test_a_turn_held_as_it_starts_prepares_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ask before admission's first await: nothing of the turn's preparation runs."""
    from kiro_crew.slack.code_channels import binding

    prepared: list[Any] = []

    async def prepare(*a: Any) -> bool:
        prepared.append(a)
        return True

    monkeypatch.setattr(binding, "notify_turn_prepare", prepare)
    record = await _turn([], held=True)
    assert prepared == [] and not _wrote_prompt(record)


async def test_a_stop_before_the_claim_claims_no_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ask right before the session claim (no provider starts for a held turn)."""
    key: list[str] = []
    pressed: list[int] = []
    monkeypatch.setattr(
        chat_runner,
        "_slot_predecessor_store",
        _pressing(chat_runner._slot_predecessor_store, key, pressed),
    )
    record = await _turn(key)
    assert pressed and record.allocations == [] and not _wrote_prompt(record)


async def test_a_stop_after_the_claim_fires_no_hook(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ask before the AgentSpawn and UserPromptSubmit hooks."""

    hooks = _Hooks()
    real = chat_runner._turn_dispatch_prep

    async def inside(turn: Any) -> Any:
        _stop_now(turn.slot.key)
        return await real(turn)

    monkeypatch.setattr(chat_runner, "_turn_dispatch_prep", inside)
    record = await _turn([], hooks=hooks)
    assert hooks.fired == [] and not _wrote_prompt(record)


@pytest.mark.parametrize("event", ["AgentSpawn", "UserPromptSubmit"])
async def test_a_stop_while_a_hook_is_vetted_spawns_no_command(event: str) -> None:
    """Each hook command's ask at its spawn, after its vetting awaits: a Stop while one
    event's hook is vetted spawns that command and nothing after it."""
    key: list[str] = []
    hooks = _Hooks(stop=lambda: _stop_now(key[0]), on=event)
    from turn_harness import TurnScript, run_turn

    def setup(ctx: Any) -> None:
        key.append(ctx.slot.key)
        ctx.state._hook_store = hooks

    record = await run_turn(TurnScript(events=[], setup=setup))
    assert event in hooks.fired, hooks.fired
    assert hooks.spawned == hooks.fired[: hooks.fired.index(event)]
    assert not _wrote_prompt(record)


async def test_a_stop_during_the_hooks_starts_nothing_after_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ask after the hooks: a Stop during the last hook (after its spawn) ends the
    turn before the prompt's egress scrub and the model probes."""
    key: list[str] = []
    hooks = _Hooks(stop=lambda: _stop_now(key[0]), on="UserPromptSubmit", after=True)
    scrubbed: list[int] = []
    monkeypatch.setattr(
        chat_runner,
        "_scrub_dashboard_prefix",
        _pressing(chat_runner._scrub_dashboard_prefix, key, scrubbed),
    )
    from turn_harness import TurnScript, run_turn

    def setup(ctx: Any) -> None:
        key.append(ctx.slot.key)
        ctx.state._hook_store = hooks

    record = await run_turn(TurnScript(events=[], setup=setup))
    assert hooks.spawned == hooks.fired and hooks.fired[-1] == "UserPromptSubmit"
    assert scrubbed == [] and not _wrote_prompt(record)


async def test_a_stop_during_the_prompts_last_steps_announces_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ask before the first post: no "Thinking…" status, no echo."""
    key: list[str] = []
    pressed: list[int] = []
    monkeypatch.setattr(
        chat_runner,
        "_scrub_dashboard_prefix",
        _pressing(chat_runner._scrub_dashboard_prefix, key, pressed),
    )
    from turn_harness import TurnScript, run_turn

    def setup(ctx: Any) -> None:
        key.append(ctx.slot.key)

    record = await run_turn(TurnScript(events=[], setup=setup))
    thinking = [f for f in record.ws_frames if "Thinking" in str(f)]
    assert pressed and thinking == [] and not _wrote_prompt(record)


async def test_a_stop_during_the_echo_is_ended_at_the_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """The dispatch gate, the last refusal: a Stop after the first post writes no prompt
    and the turn is refused as stopped."""
    key: list[str] = []
    pressed: list[int] = []
    monkeypatch.setattr(
        chat_runner, "_mirror_echo_text", _pressing(chat_runner._mirror_echo_text, key, pressed)
    )
    monkeypatch.setattr(
        chat_runner,
        "_deliver_cross_surface_user_message",
        _pressing(chat_runner._deliver_cross_surface_user_message, key, pressed),
    )
    from turn_harness import TurnScript, run_turn

    def setup(ctx: Any) -> None:
        key.append(ctx.slot.key)

    record = await run_turn(TurnScript(events=[], setup=setup))
    assert pressed and not _wrote_prompt(record)
    assert [call.args[2] for call in record.crew("on_turn_refused")] == ["channel_stopped"]


async def test_a_persons_stamp_from_after_the_stop_releases_at_the_gate() -> None:
    """The gate is handed the turn's own stamp: a person's from after the press releases."""
    from turn_harness import TurnScript, run_turn

    from kiro_crew.acp.types import EVENT_TEXT_CHUNK, AcpEvent

    def setup(ctx: Any) -> None:
        _stop_now(ctx.slot.key)

    person = stop_hold.stamp(next(stop_hold._presses) + 1, True)  # read after the Stop
    reply = AcpEvent(kind=EVENT_TEXT_CHUNK, text="the reply")
    record = await run_turn(
        TurnScript(events=[reply], setup=setup, run_kwargs={"_stop_hold_stamp": person})
    )
    assert _wrote_prompt(record) and stop_hold._keys == {}


async def test_the_runner_hands_the_stream_the_write_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """The last ask, at the prompt write: a Stop in the transport's awaits writes nothing,
    and the turn closes once as stopped."""
    import turn_harness
    from turn_harness import TurnScript, run_turn

    import kiro_crew.providers.acp as acp_provider
    from kiro_crew.acp.types import EVENT_TEXT_CHUNK, AcpEvent

    key: list[str] = []

    class _Gated(turn_harness.ScriptedProvider):
        def stream(self, message: str, *, allow_image: bool = True, prompt_gate: Any = None):
            _stop_now(key[0])  # the Stop, during the transport's own awaits
            if prompt_gate is not None:
                prompt_gate()  # the transport asks it right before the write
            return super().stream(message, allow_image=allow_image)

    monkeypatch.setattr(acp_provider, "AcpProvider", _Gated)  # read by ``write_gate``

    def setup(ctx: Any) -> None:
        key.append(ctx.slot.key)

    reply = AcpEvent(kind=EVENT_TEXT_CHUNK, text="the reply")
    record = await run_turn(TurnScript(events=[reply], setup=setup, provider=_Gated))
    assert not record.rows("assistant") and record.stop_reason == "failed: channel_stopped"


async def test_a_stop_during_a_workflows_start_runs_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``/workflow`` hands its commit point the hold: a Stop while the run starts is asked
    there (the handler's own commit-point ask is
    ``test_a_workflow_started_across_a_stop_never_runs``)."""

    started: list[bool] = []

    async def start(state: Any, slot: Any, message: str, key: str, admit: Any = None) -> None:
        _stop_now(slot.key)  # the Stop, during the run's start
        started.append(admit is None or admit())

    monkeypatch.setattr(chat_runner, "_handle_workflow_command", start)
    from turn_harness import TurnScript, run_turn

    record = await run_turn(TurnScript(events=[], message="/workflow run nightly"))
    assert started == [False] and not _wrote_prompt(record)


async def test_a_stop_before_a_harness_local_command_runs_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A harness-local command asks the hold after its last await (the backend read)."""
    from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS

    answered: list[str] = []

    async def backend(state: Any, key: str) -> str:
        _stop_now(key.removeprefix("dashboard:"))  # the Stop, during the backend read
        return ACP_BACKEND_KAS

    def answer(state: Any, slot: Any, key: str, word: str, *_a: Any) -> Any:
        answered.append(word)
        return asyncio.sleep(0)

    monkeypatch.setattr(chat_runner, "_slash_command_backend", backend)
    monkeypatch.setattr(chat_runner, "_answer_slash_for_harness", answer)
    from turn_harness import TurnScript, run_turn

    key: list[str] = []

    def setup(ctx: Any) -> None:
        key.append(ctx.slot.key)

    await run_turn(TurnScript(events=[], message="/context show", setup=setup))
    assert answered == [] and key and key[0] in stop_hold._keys
