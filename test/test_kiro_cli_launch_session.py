"""Opening a chat in kiro-cli: the session kiro-cli resumes, and the chat's own
start racing the open.

kiro-cli 2.21.1's ``chat --resume-id`` runs the agent the session's envelope
(``~/.kiro/sessions/cli/<sid>.json``) names, whatever ``--agent`` says, and keeps
the tools the envelope trusts trusted. Both were checked live on a pty: a resume
with ``--agent`` naming another agent ran the envelope's agent, and one whose
envelope trusted ``executeCmd`` ran a shell command without asking, then asked
once the envelope trusted nothing. So the open points the envelope at its launch
copy, empties its grants for the hold, and puts both back on release.

Split from ``test_kiro_cli_launch.py`` (the file-line cap); the fixtures are
that module's.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import pathlib
import threading
from unittest.mock import MagicMock

import pytest
from test_kiro_cli_launch import (  # noqa: F401 -- the fixtures are bound for pytest
    _ENVELOPE,
    KEY,
    SID,
    _isolate,
    _log_line,
    _request,
    _slot,
    _state,
    switch_fixture,
    wired_fixture,
)

from kiro_crew import kiro_cli_handoff
from kiro_crew.dashboard import chat_handlers
from kiro_crew.dashboard.handlers import kiro_cli_launch as kcl
from kiro_crew.testing.wait import async_wait_until

_SAVED_AGENT = _ENVELOPE["session_state"]["agent_name"]
_SAVED_PERMISSIONS = _ENVELOPE["session_state"]["permissions"]


def _envelope(wired) -> dict:
    return json.loads((wired.sessions / f"{SID}.json").read_text())


def _rewrite(wired, change) -> None:
    envelope = _envelope(wired)
    change(envelope)
    (wired.sessions / f"{SID}.json").write_text(json.dumps(envelope))


async def _open(wired, terminal_id="t"):
    (wired.sessions / f"{SID}.jsonl").touch()
    return await kcl.prepare_kiro_cli_launch(
        _request(_state(_slot(wired.project))), "main", terminal_id
    )


# ── the session kiro-cli resumes ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_terminal_resumes_on_the_launch_copy_with_the_sessions_grants_emptied(
    wired, switch
):
    launch, code = await _open(wired)
    assert code == "" and launch is not None
    state = _envelope(wired)["session_state"]
    assert state["agent_name"] == pathlib.Path(launch.launch_spec).stem
    assert state["permissions"] == {
        "filesystem": {
            "allowed_read_paths": ["/w"],  # the conversation's own folder only
            "allowed_write_paths": [],
            "denied_read_paths": ["/w/secret"],
            "denied_write_paths": [],
        },
        "trusted_tools": [],
        "denied_tools": [{"BuiltIn": "webFetch"}],
        "allowed_commands": [],
    }
    [record] = [json.loads(p.read_text()) for p in wired.records.iterdir()]
    assert record["session_agent"] == {
        "agent_name": _SAVED_AGENT,
        "permissions": _SAVED_PERMISSIONS,
    }, "a restart can put the envelope back"
    launch.announce()

    # kiro-cli rewrites the envelope as it runs, and the owner may trust a tool.
    def in_kiro_cli(envelope):
        envelope["title"] = "renamed in kiro-cli"
        envelope["session_state"]["permissions"]["trusted_tools"] = [{"BuiltIn": "fsWrite"}]

    _rewrite(wired, in_kiro_cli)
    assert await kiro_cli_handoff.settle_and_release(KEY, "t")
    after = _envelope(wired)
    assert after["session_state"]["agent_name"] == _SAVED_AGENT
    assert after["session_state"]["permissions"] == _SAVED_PERMISSIONS, "the terminal's trust goes"
    assert after["title"] == "renamed in kiro-cli", "the rest is kiro-cli's"
    assert list(wired.records.iterdir()) == []


@pytest.mark.asyncio
async def test_the_record_names_what_the_open_writes_before_it_writes_it(
    wired, switch, monkeypatch
):
    """A gateway killed between any two of the open's writes leaves its restart
    a record that can undo what was written: the record goes first."""
    seen = []
    real_write, real_point = kcl._write_launch_spec, kcl._point_session_agent

    def on_disk():
        [record] = [json.loads(p.read_text()) for p in wired.records.iterdir()]
        return record

    def write(agent, target):
        record = on_disk()
        seen.append(("copy", record["launch_spec"] == target))
        real_write(agent, target)

    def point(sid, agent):
        record = on_disk()
        seen.append(("envelope", record["session_agent"]["agent_name"] == _SAVED_AGENT))
        real_point(sid, agent)

    monkeypatch.setattr(kcl, "_write_launch_spec", write)
    monkeypatch.setattr(kcl, "_point_session_agent", point)
    launch, code = await _open(wired)
    assert code == ""
    assert seen == [("copy", True), ("envelope", True)]
    # Killed right after the envelope write: the restart undoes both.
    kiro_cli_handoff.reset_holds()
    slot = _slot(wired.project)
    assert await kcl.recover_left_open(_state(slot), slot, KEY)
    assert _envelope(wired)["session_state"] == _ENVELOPE["session_state"]
    assert [p.name for p in wired.agents.iterdir()] == ["myagent.json"]


@pytest.mark.asyncio
async def test_a_cancelled_open_rolls_back_only_after_its_envelope_write_finishes(
    wired, switch, monkeypatch
):
    """A cancel landing while the envelope write runs in its worker must not
    restore the envelope under it: the worker would then write the pointed
    envelope after the rollback, naming a launch copy that is gone."""
    in_worker = threading.Event()
    go = threading.Event()
    real_point = kcl._point_session_agent

    def slow_point(sid, agent):
        in_worker.set()
        go.wait(10)
        real_point(sid, agent)

    monkeypatch.setattr(kcl, "_point_session_agent", slow_point)
    task = asyncio.ensure_future(_open(wired))
    await async_wait_until(in_worker.is_set, timeout=10)
    task.cancel()
    go.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert _envelope(wired)["session_state"] == _ENVELOPE["session_state"]
    assert [p.name for p in wired.agents.iterdir()] == ["myagent.json"]
    assert list(wired.records.iterdir()) == []
    assert not kiro_cli_handoff.is_held(KEY)


@pytest.mark.asyncio
async def test_a_release_that_times_out_while_restoring_keeps_the_record(
    wired, switch, monkeypatch
):
    """The import is owed from the moment the release starts: a settle cut off
    at its deadline while the envelope restore runs must not read as imported."""
    launch, code = await _open(wired)
    assert code == ""
    launch.announce()
    real_restore = kcl._restore_session_agent
    in_restore = threading.Event()
    go = threading.Event()

    def slow_restore(sid, saved):
        if not in_restore.is_set():
            in_restore.set()
            go.wait(10)
        real_restore(sid, saved)

    async def deadline_hits(step, timeout):
        # The settle's deadline, reached while the restore is in its worker.
        task = asyncio.ensure_future(step)
        await async_wait_until(in_restore.is_set, timeout=10)
        task.cancel()
        go.set()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        raise TimeoutError

    class _DeadlineAsyncio:
        def __getattr__(self, name):
            return deadline_hits if name == "wait_for" else getattr(asyncio, name)

    monkeypatch.setattr(kcl, "_restore_session_agent", slow_restore)
    monkeypatch.setattr(kiro_cli_handoff, "asyncio", _DeadlineAsyncio())
    with (wired.sessions / f"{SID}.jsonl").open("a") as fh:
        fh.write(_log_line("Prompt", "asked in kiro-cli") + "\n")
    await kiro_cli_handoff.settle_and_release(KEY, "t")
    assert not kiro_cli_handoff.is_held(KEY)
    assert len(list(wired.records.iterdir())) == 1, "kept: the turns were not imported"
    assert _envelope(wired)["session_state"] == _ENVELOPE["session_state"]


@pytest.mark.asyncio
async def test_an_open_whose_terminal_never_starts_puts_the_envelope_back(wired, switch):
    launch, code = await _open(wired)
    assert code == ""
    launch.release()
    assert _envelope(wired)["session_state"] == _ENVELOPE["session_state"]
    assert not kiro_cli_handoff.is_held(KEY)


@pytest.mark.asyncio
async def test_an_agent_without_a_spec_file_resumes_on_kiro_clis_default_not_the_saved_one(
    wired, switch, monkeypatch
):
    """With no ``--agent``, kiro-cli would run the envelope's agent: a Crew agent
    with its pre-approvals. A null name leaves kiro-cli its own default."""
    monkeypatch.setattr(kcl, "_agent_spec_where", lambda agent, cwd: (False, False))
    launch, code = await _open(wired)
    assert code == "" and launch.launch_spec == ""
    assert "--agent" not in launch.argv
    assert _envelope(wired)["session_state"]["agent_name"] is None
    launch.release()
    assert _envelope(wired)["session_state"]["agent_name"] == _SAVED_AGENT


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["missing", "directory", "not json", "no session_state"])
async def test_an_envelope_the_open_cannot_rewrite_refuses_it(wired, switch, damage):
    path = wired.sessions / f"{SID}.json"
    path.unlink()
    if damage == "directory":
        path.mkdir()
    elif damage == "not json":
        path.write_text("{")
    elif damage == "no session_state":
        path.write_text(json.dumps({"session_id": SID}))
    launch, code = await _open(wired)
    assert (launch, code) == (None, kcl.CODE_SESSION_FILE)
    assert code in kcl.KIRO_CLI_REFUSAL_CODES
    assert "~/.kiro/sessions/cli" in kcl.refusal_message(code)
    assert not kiro_cli_handoff.is_held(KEY)
    assert [p.name for p in wired.agents.iterdir()] == ["myagent.json"], "no launch copy left"
    assert not wired.records.exists() or list(wired.records.iterdir()) == []


@pytest.mark.asyncio
async def test_after_a_restart_the_chats_next_turn_puts_the_envelope_back(wired, switch):
    launch, code = await _open(wired)
    assert code == ""
    launch.announce()
    kiro_cli_handoff.reset_holds()  # the gateway stopped: no release ran
    slot = _slot(wired.project)
    assert await kcl.recover_left_open(_state(slot), slot, KEY)
    assert _envelope(wired)["session_state"] == _ENVELOPE["session_state"]
    assert [p.name for p in wired.agents.iterdir()] == ["myagent.json"]
    assert list(wired.records.iterdir()) == []


async def _loop_turns(n: int) -> None:
    """Let the event loop run *n* times round, so a delivered cancel is acted on."""
    loop = asyncio.get_running_loop()
    for _ in range(n):
        turn = loop.create_future()
        loop.call_soon(turn.set_result, None)
        await turn


@pytest.mark.asyncio
async def test_a_cancelled_recovery_holds_its_lock_until_its_envelope_restore_finishes(
    wired, switch, monkeypatch
):
    """A recovery cancelled (the chat closed) while its worker restores the
    envelope must not free the session for a new open: that open's envelope
    would then be overwritten by the old worker, original grants and all."""
    from kiro_crew.messaging.link import canonical_key

    launch, code = await _open(wired)
    assert code == ""
    launch.announce()
    kiro_cli_handoff.reset_holds()  # the gateway stopped: the record is left
    real_restore = kcl._restore_session_agent
    in_worker = threading.Event()
    go = threading.Event()

    def slow_restore(sid, saved):
        if not in_worker.is_set():
            in_worker.set()
            go.wait(10)
        real_restore(sid, saved)

    monkeypatch.setattr(kcl, "_restore_session_agent", slow_restore)
    slot = _slot(wired.project)
    task = asyncio.ensure_future(kcl.recover_left_open(_state(slot), slot, KEY))
    await async_wait_until(in_worker.is_set, timeout=10)
    task.cancel()
    await _loop_turns(5)
    lock = kcl._recovery_locks[canonical_key(KEY)]
    assert lock.locked() and not task.done(), "held while the worker still writes"
    go.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not lock.locked()
    assert _envelope(wired)["session_state"] == _ENVELOPE["session_state"]


@pytest.mark.asyncio
async def test_an_envelope_that_cannot_be_put_back_keeps_the_record_for_the_next_turn(
    wired, switch, monkeypatch
):
    launch, code = await _open(wired)
    assert code == ""
    launch.announce()
    real = kcl._write_envelope

    def read_only(*_a, **_k):
        raise OSError("read-only")

    monkeypatch.setattr(kcl, "_write_envelope", read_only)
    await kiro_cli_handoff.settle_and_release(KEY, "t")
    assert _envelope(wired)["session_state"]["agent_name"] != _SAVED_AGENT
    assert len(list(wired.records.iterdir())) == 1, "kept: the envelope still names the copy"
    slot = _slot(wired.project)
    assert await kcl.recover_left_open(_state(slot), slot, KEY)
    assert len(list(wired.records.iterdir())) == 1, "still failing: kept again"
    monkeypatch.setattr(kcl, "_write_envelope", real)
    assert await kcl.recover_left_open(_state(slot), slot, KEY)
    assert _envelope(wired)["session_state"] == _ENVELOPE["session_state"]
    assert list(wired.records.iterdir()) == []


def test_open_records_live_where_sandboxed_agents_can_only_read():
    """A record names a launch copy the gateway removes and the envelope state
    it writes back, so a sandboxed agent that could plant one (at its chat's
    predictable key) could steer those writes. Records sit in the data home's
    ``run`` subtree, which the sandbox mounts read-only."""
    import os

    from kiro_crew.sandbox_workspace import _voice_runtime_parent_paths

    parent = kiro_cli_handoff._records_dir().parent
    sealed = {os.path.normpath(p) for p in _voice_runtime_parent_paths()}
    assert {os.path.normpath(str(parent)), os.path.realpath(parent)} & sealed


def test_a_crewmate_pinned_to_a_harness_this_gateway_cannot_start_does_not_open(monkeypatch):
    """The backend read passes the crewmate's harness pin like every other
    reader of the selection gate; an unstartable pin means no kiro-cli session."""
    from kiro_crew import crewmate_harness

    def unstartable(cfg, key):
        raise crewmate_harness.CrewmateHarnessUnavailable("mate", "codex")

    monkeypatch.setattr(crewmate_harness, "crewmate_backend_pin", unstartable)
    assert kcl._configured_backend_is_kiro(KEY) is False


# ── nothing taken in while held that cannot run ─────────────────────────────


@pytest.mark.asyncio
async def test_a_responsive_send_to_a_held_chat_is_refused_before_it_is_accepted(
    tmp_path, monkeypatch
):
    """A crewmate composer sends ``responsive: true``. That path accepts and
    queues the message; while held its turn would be refused after the user row
    was written, losing the message. The send is refused before that branch."""
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from chat_test_helpers import _make_state

    from kiro_crew.dashboard import chat_utils, responsive_chat
    from kiro_crew.dashboard.chat import api_chat
    from kiro_crew.dashboard.state import _ChatSlot

    accepted = []
    monkeypatch.setattr(responsive_chat, "eligible", lambda *a, **k: True)

    async def accept(*a, **k):
        accepted.append(a)
        return {}

    monkeypatch.setattr(responsive_chat, "accept", accept)
    state = _make_state(tmp_path)
    slot = _ChatSlot("chat-held")
    state._slots[slot.key] = slot
    kiro_cli_handoff.hold(chat_utils.effective_session_key(slot), "t")

    @web.middleware
    async def _auth(request, handler):
        request["app"] = ""
        request["user"] = "local-app"
        return await handler(request)

    app = web.Application(middlewares=[_auth])
    app["state"] = state
    app.router.add_post("/api/chat/send", api_chat)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/api/chat/send", json={"slot": slot.key, "message": "hi", "responsive": True}
        )
        assert resp.status == 409
        assert (await resp.json())["code"] == "kiro_cli_open"
    assert accepted == [] and slot.messages == [] and not slot._queue


@pytest.mark.asyncio
async def test_the_queue_drain_leaves_a_held_chats_entries_in_place(tmp_path):
    """Something queued while held (a monitor wake, a delivery) would be popped,
    its row written, then refused at admission: the drain leaves it instead."""
    from chat_test_helpers import _make_state

    from kiro_crew.dashboard import chat_utils
    from kiro_crew.dashboard.chat_turn import queue_drain
    from kiro_crew.dashboard.state import _ChatSlot

    state = _make_state(tmp_path)
    slot = _ChatSlot("chat-held")
    state._slots[slot.key] = slot
    slot._queue.append({"message": "wake up", "id": "q1"})
    kiro_cli_handoff.hold(chat_utils.effective_session_key(slot), "t")
    assert await queue_drain._start_next_queued_turn(state, slot) is False
    assert [e["id"] for e in slot._queue] == ["q1"] and slot.messages == []


@pytest.mark.asyncio
async def test_the_hold_s_release_starts_what_was_queued_meanwhile(wired, switch, monkeypatch):
    from kiro_crew.dashboard import chat_runner

    drained = []

    async def drain(state_, keys):
        drained.append((keys, kiro_cli_handoff.is_held(KEY)))

    monkeypatch.setattr(chat_runner, "_drain_parked_queues", drain)
    slot = _slot(wired.project, _queue=[])
    (wired.sessions / f"{SID}.jsonl").touch()
    launch, code = await kcl.prepare_kiro_cli_launch(_request(_state(slot)), "main", "t")
    assert code == ""
    slot._queue.append({"message": "wake up"})
    launch.release()
    await async_wait_until(lambda: drained, timeout=10)
    assert drained == [(["main"], False)], "drained once the hold is gone"


# ── the chat's own start racing the open ────────────────────────────────────


@pytest.mark.asyncio
async def test_a_turn_that_slipped_in_declines_the_reset_and_gives_the_hold_back(
    wired, switch, monkeypatch
):
    provider = object()
    slot = _slot(wired.project)
    state = _state(slot)
    # No provider at the busy read; one registered by the time the reset runs.
    state.sessions.get_provider = MagicMock(side_effect=[None, provider, provider])

    async def declined(*_a, **_k):
        slot.running = True  # the send that slipped in dispatched its turn
        return False

    monkeypatch.setattr(chat_handlers, "_reset_slot_session", declined)
    assert await kcl.prepare_kiro_cli_launch(_request(state), "main", "t") == (None, kcl.CODE_BUSY)
    assert not kiro_cli_handoff.is_held(KEY)


@pytest.mark.asyncio
async def test_a_pre_spawn_slower_to_unwind_than_five_seconds_still_lets_an_idle_chat_open(
    wired, switch, monkeypatch
):
    """A pre-spawn cancelled mid-spawn kills its process before it ends, which
    can take over 5 s. An open that stops waiting sooner finds the process it
    registered and calls the idle chat busy ("turn running")."""
    # The open's waits at 1/100 scale, so 6 s of unwinding takes 60 ms here.
    scale = 0.01
    real_wait = asyncio.wait

    async def scaled_wait(aws, *, timeout=None, **kw):
        return await real_wait(aws, timeout=None if timeout is None else timeout * scale, **kw)

    class _ScaledAsyncio:
        def __getattr__(self, name):
            return scaled_wait if name == "wait" else getattr(asyncio, name)

    monkeypatch.setattr(kcl, "asyncio", _ScaledAsyncio())
    provider = object()
    state = _state(_slot(wired.project))
    registered: list[object] = []
    state.sessions.get_provider = MagicMock(
        side_effect=lambda key: registered[-1] if registered else None
    )
    started = asyncio.Event()

    async def pre_spawn():
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            # It unwinds for 6 s at the scale above, then its process registers.
            await asyncio.sleep(6.0 * scale)
            registered.append(provider)
            raise

    async def reset(state_, slot, key, *, skip_if_busy=False):
        switch.reset.append((key, skip_if_busy))
        registered.clear()  # the reset tears down what is registered now
        await asyncio.sleep(2.0 * scale)  # and its teardown takes a moment
        return True

    monkeypatch.setattr(chat_handlers, "_reset_slot_session", reset)
    task = asyncio.ensure_future(pre_spawn())
    await started.wait()
    state._slots["main"]._eager_spawn_task = task
    launch, code = await kcl.prepare_kiro_cli_launch(_request(state), "main", "t")
    assert code == "" and launch is not None
    assert task.done() and switch.reset == [(KEY, True)]
    launch.release()


@pytest.mark.asyncio
async def test_a_pre_spawn_that_will_not_stop_refuses_as_starting_not_as_a_running_turn(
    wired, switch, monkeypatch
):
    monkeypatch.setattr(kcl, "_EAGER_SPAWN_CANCEL_WAIT_S", 0.05)
    started = asyncio.Event()
    stop = asyncio.Event()

    async def pre_spawn():
        started.set()
        while not stop.is_set():
            try:
                await stop.wait()
            except asyncio.CancelledError:
                continue  # still killing the process it started

    task = asyncio.ensure_future(pre_spawn())
    await started.wait()
    state = _state(_slot(wired.project, _eager_spawn_task=task))
    try:
        assert await kcl.prepare_kiro_cli_launch(_request(state), "main", "t") == (
            None,
            kcl.CODE_STARTING,
        )
    finally:
        stop.set()
        await task
    assert not kiro_cli_handoff.is_held(KEY)
    assert kcl.CODE_STARTING in kcl.KIRO_CLI_REFUSAL_CODES


@pytest.mark.asyncio
async def test_a_process_registered_with_no_turn_running_refuses_as_starting(wired, switch):
    state = _state(_slot(wired.project))
    state.sessions.get_provider = MagicMock(side_effect=[None, None, object()])
    assert await kcl.prepare_kiro_cli_launch(_request(state), "main", "t") == (
        None,
        kcl.CODE_STARTING,
    )
    assert not kiro_cli_handoff.is_held(KEY)


@pytest.mark.asyncio
async def test_a_pending_pre_spawn_is_cancelled_before_the_reset(wired, switch):
    started = asyncio.Event()

    async def pre_spawn():
        started.set()
        await asyncio.Event().wait()

    task = asyncio.ensure_future(pre_spawn())
    await started.wait()
    state = _state(_slot(wired.project, _eager_spawn_task=task))
    launch, _ = await kcl.prepare_kiro_cli_launch(_request(state), "main", "t")
    assert launch is not None
    assert task.cancelled()
