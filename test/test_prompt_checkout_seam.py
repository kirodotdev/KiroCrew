"""The branch switch's busy check sees every agent prompt, on both ACP transports.

Every harness sends a turn as a ``session/prompt`` request: through
``AcpClient._send_request`` (the adapter harnesses) or ``AcpRuntime.send_request``
(the shared kiro / KAS runtime). Each prompt is recorded from before its write
until the backend answers it or the process tree is confirmed gone
(``kiro_crew.repo_checkout_guard``), whatever drives the turn and whether or not
anyone still waits on the answer.
"""

from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from acp_runtime_helpers import _feed, _make_runtime, _register

from kiro_crew import repo_checkout_guard as guard
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.types import METHOD_COMMANDS_EXECUTE, METHOD_PROMPT
from kiro_crew.dashboard.handlers import git_branches as gb
from kiro_crew.testing.wait import async_wait_until, await_until, default_timeout

SID = "sess-1"


@pytest.fixture(autouse=True)
def _no_reservations():
    guard._reserved.clear()
    yield
    guard._reserved.clear()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "sub").mkdir(parents=True)
    (tmp_path / "other").mkdir()
    return root


def _no_slots():
    return SimpleNamespace(_slots={}, subagents=None)


async def _busy(root) -> bool:
    return await gb._repo_has_running_work(_no_slots(), os.path.realpath(root))


class _SeenEvent(asyncio.Event):
    """A reservation event that says when something starts waiting on it."""

    def __init__(self) -> None:
        super().__init__()
        self.waiting = asyncio.Event()

    async def wait(self) -> bool:
        self.waiting.set()
        return await super().wait()


def _reserve_seen(root: str) -> _SeenEvent:
    """Reserve ``root`` with an event that reports the first waiter on it.

    A test asserting that work has NOT been admitted waits on ``waiting``
    first, so the assertion is about a prompt parked on this reservation and
    never about one still matching its folder in a worker thread.
    """
    assert root not in guard._reserved
    event = _SeenEvent()
    guard._reserved[root] = event
    return event


async def _until_reader_saw_noop(rt) -> None:
    """Wait until the reader has handled the trailing ``noop`` broadcast.

    Frames are read in order, so once the notification fed after the frames
    under test reaches the session queue, every earlier frame has been handled
    and an assertion that one of them changed nothing is about the reader.
    """
    queue = rt._session_queues[SID]

    async def _drain() -> None:
        while True:
            msg = await queue.get()
            if msg is not None and getattr(msg, "method", None) == "noop":
                return

    await asyncio.wait_for(_drain(), default_timeout())


def test_the_prompt_methods_are_the_wire_constants():
    from kiro_crew.acp.client_turn_control import _GOOSE_SESSION_STEER
    from kiro_crew.acp.types import METHOD_SESSION_STEERING, STEERING_STARTED_NEW_TURN

    assert guard.TURN_METHODS == {METHOD_PROMPT, METHOD_COMMANDS_EXECUTE}
    assert guard.STEER_METHODS == {"_session/steer", METHOD_SESSION_STEERING, _GOOSE_SESSION_STEER}
    assert guard.PROMPT_METHODS == guard.TURN_METHODS | guard.STEER_METHODS
    assert guard.STARTED_NEW_TURN == STEERING_STARTED_NEW_TURN


def test_no_checkout_in_flight_means_no_suspension():
    # A coroutine that completes on its first step never yields to the loop, so a
    # prompt sent with no switch in progress is not delayed or reordered.
    for folder in ("/some/where", ""):
        step = guard.before_prompt(folder)
        with pytest.raises(StopIteration):
            step.send(None)


@pytest.mark.asyncio
async def test_a_switch_reserved_during_the_off_loop_match_is_waited_out(
    repo, tmp_path, monkeypatch
):
    other = os.path.realpath(tmp_path / "other")
    root = os.path.realpath(repo)
    real_to_thread = asyncio.to_thread
    raced: list[str] = []

    async def _match_then_race(fn, *args):
        result = await real_to_thread(fn, *args)
        if not raced:
            # A switch of the prompt's own repo reserves while the match that
            # found nothing covering it ran off the loop.
            raced.append(_reserve_seen(root))
        return result

    monkeypatch.setattr(asyncio, "to_thread", _match_then_race)
    assert guard.reserve(other)
    admitted = asyncio.ensure_future(guard.before_prompt(str(repo / "sub")))
    await async_wait_until(lambda: bool(raced))
    guard.release(other)
    await await_until(raced[0].waiting)
    assert not admitted.done()
    guard.release(root)
    await await_until(admitted)


@pytest.mark.parametrize(
    "answer, counted",
    [
        ({"result": {"outcome": "injected"}}, False),
        ({"result": {"queued": True}}, False),
        ({"error": {"code": -32601}}, False),
        # An adapter-owned turn nobody will report the end of.
        ({"result": {"outcome": "startedNewTurn"}}, True),
    ],
    ids=["injected", "queued", "refused", "started-new-turn"],
)
@pytest.mark.parametrize("method", sorted(guard.STEER_METHODS))
def test_a_steer_stays_counted_only_when_it_started_a_turn_of_its_own(method, answer, counted):
    guard.prompt_sent("inst", 3, "/w", method=method)
    guard.prompt_answered("inst", 3, answer.get("result"))
    assert (guard.tracked_work_folders() == (["/w"], True)) is counted
    guard.prompt_process_gone("inst")
    assert guard.tracked_work_folders() == ([], True)


@pytest.mark.parametrize("wire_id", [1.0, 1.9, "1", True, None, [1]])
def test_only_the_issued_integer_id_answers_a_prompt(wire_id):
    guard.prompt_sent("inst", 1, "/w", method=METHOD_PROMPT)
    guard.prompt_answered("inst", wire_id, {})
    assert guard.tracked_work_folders() == (["/w"], True)
    guard.prompt_answered("inst", 1, {})
    assert guard.tracked_work_folders() == ([], True)


# ── The shared runtime (kiro, KAS) ──


def _runtime(folder: str):
    rt, reader, proc = _make_runtime()
    rt._process_instance = "inst-a"
    _register(rt, SID)
    rt._session_cwds[SID] = folder
    return rt, reader, proc


@pytest.mark.asyncio
async def test_a_runtime_prompt_is_busy_in_its_folder_until_the_backend_answers(repo, tmp_path):
    rt, reader, _proc = _runtime(str(repo / "sub"))
    req_id = await rt.send_request(METHOD_PROMPT, {"sessionId": SID, "prompt": []})
    assert await _busy(repo) is True
    assert await _busy(tmp_path / "other") is False
    # Nobody waits on it (the caller stopped reading); the backend's answer still
    # ends the record when the reader sees it.
    reader_task = asyncio.ensure_future(rt._reader_loop())
    try:
        _feed(reader, {"jsonrpc": "2.0", "id": req_id, "result": {"stopReason": "end_turn"}})
        await async_wait_until(lambda: guard.tracked_work_folders() == ([], True))
        assert await _busy(repo) is False
    finally:
        reader_task.cancel()
        await asyncio.gather(reader_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_a_runtime_prompt_whose_session_folder_is_unknown_is_busy_everywhere(tmp_path):
    rt, _reader, _proc = _make_runtime()
    rt._process_instance = "inst-a"
    _register(rt, SID)
    await rt.send_request(METHOD_PROMPT, {"sessionId": SID, "prompt": []})
    assert guard.tracked_work_folders() == ([], False)
    assert await _busy(tmp_path / "other") is True


@pytest.mark.asyncio
async def test_other_runtime_requests_are_not_recorded(repo):
    rt, _reader, _proc = _runtime(str(repo))
    await rt.send_request("session/set_mode", {"sessionId": SID, "modeId": "x"})
    assert guard.tracked_work_folders() == ([], True)


@pytest.mark.asyncio
async def test_a_runtime_prompt_waits_out_a_checkout_of_its_folder_before_it_is_written(repo):
    rt, _reader, proc = _runtime(str(repo / "sub"))
    root = os.path.realpath(repo)
    seen = _reserve_seen(root)
    send = asyncio.ensure_future(rt.send_request(METHOD_PROMPT, {"sessionId": SID, "prompt": []}))
    await await_until(seen.waiting)
    assert not send.done()
    proc.stdin.write.assert_not_called()
    assert guard.tracked_work_folders() == ([], True)
    guard.release(root)
    await await_until(send)
    proc.stdin.write.assert_called()
    assert await _busy(repo) is True


class _ObservedWriteLock(asyncio.Lock):
    """A write lock that signals when a second acquirer is parked on it."""

    def __init__(self) -> None:
        super().__init__()
        self.waiting = asyncio.Event()

    async def acquire(self) -> bool:
        if self.locked():
            self.waiting.set()
        return await super().acquire()


def _runtime_prompt_task(rt, writer: str) -> asyncio.Task:
    params = {"sessionId": SID, "prompt": []}
    if writer == "send_request":
        coro = rt.send_request(METHOD_PROMPT, params)
    elif writer == "send_request_for_answer":
        coro = rt.send_request_for_answer(METHOD_PROMPT, params)
    else:
        coro = rt._send_and_await(METHOD_PROMPT, params, timeout=60)
    return asyncio.create_task(coro)


@pytest.mark.asyncio
@pytest.mark.parametrize("writer", ["send_request", "send_request_for_answer", "send_and_await"])
async def test_a_runtime_prompt_cancelled_while_waiting_for_stdin_is_not_busy(repo, writer):
    rt, _reader, proc = _runtime(str(repo))
    lock = _ObservedWriteLock()
    await lock.acquire()
    rt._stdin_lock = lock
    send = _runtime_prompt_task(rt, writer)
    try:
        await await_until(lock.waiting)
        assert proc.stdin.write.call_count == 0
        assert await _busy(repo) is True
        send.cancel()
        with pytest.raises(asyncio.CancelledError):
            await await_until(send)
        assert await _busy(repo) is False
    finally:
        if lock.locked():
            lock.release()
        if not send.done():
            send.cancel()
            await asyncio.gather(send, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("writer", ["send_request", "send_request_for_answer", "send_and_await"])
async def test_a_runtime_prompt_cancelled_after_stdin_write_stays_busy(repo, writer):
    rt, _reader, proc = _runtime(str(repo))
    drain_entered = asyncio.Event()
    release_drain = asyncio.Event()

    async def _drain() -> None:
        drain_entered.set()
        await release_drain.wait()

    proc.stdin.drain = _drain
    send = _runtime_prompt_task(rt, writer)
    try:
        await await_until(drain_entered)
        proc.stdin.write.assert_called_once()
        send.cancel()
        with pytest.raises(asyncio.CancelledError):
            await await_until(send)
        assert await _busy(repo) is True
    finally:
        release_drain.set()
        if not send.done():
            send.cancel()
            await asyncio.gather(send, return_exceptions=True)
        guard.prompt_process_gone("inst-a")


@pytest.mark.asyncio
async def test_an_answer_from_a_respawned_process_does_not_end_the_old_prompt(repo):
    rt, reader, _proc = _runtime(str(repo))
    req_id = await rt.send_request(METHOD_PROMPT, {"sessionId": SID, "prompt": []})
    # The process was replaced without its tree confirmed gone; the new one
    # reuses the request id.
    rt._process_instance = "inst-b"
    reader_task = asyncio.ensure_future(rt._reader_loop())
    try:
        _feed(reader, {"jsonrpc": "2.0", "id": req_id, "result": {}})
        _feed(reader, {"jsonrpc": "2.0", "method": "noop", "params": {}})
        await _until_reader_saw_noop(rt)
        assert await _busy(repo) is True
    finally:
        reader_task.cancel()
        await asyncio.gather(reader_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_a_teardown_survivor_keeps_the_prompt_counted_across_a_respawn(repo, monkeypatch):
    """A kill that leaves a descendant running keeps that process's unanswered
    prompt counted, past a respawn that mints a new instance, until the busy
    check sees the descendant gone."""
    from test_acp_runtime_death import _neuter_kill_side_effects

    from kiro_crew.acp import runtime as acp_runtime
    from kiro_crew.acp import runtime_tree_kill

    if runtime_tree_kill.platform_compat.IS_WINDOWS:
        pytest.skip("the Windows owned drain confirms the whole tree itself")
    rt, _reader, proc = _runtime(str(repo))
    _neuter_kill_side_effects(monkeypatch, proc)
    await rt.send_request(METHOD_PROMPT, {"sessionId": SID, "prompt": []})
    proc.returncode = 0
    rt._child_pids = {9999: object()}
    alive = {9999}
    monkeypatch.setattr(
        acp_runtime, "_prune_dead_descendants", lambda saved: [p for p in saved if p in alive]
    )
    await rt.kill(expected=True)
    assert rt.process_tree_confirmed_dead is False
    # A respawn names a new process; nothing about the old one is reset by it.
    rt._process_instance = "inst-b"
    assert await _busy(repo) is True
    alive.clear()
    assert await _busy(repo) is False
    assert not guard.has_lingering()


@pytest.mark.asyncio
async def test_a_windows_drain_that_raises_keeps_the_prompt_until_its_root_is_gone(
    repo, monkeypatch
):
    from test_acp_runtime_death import _neuter_kill_side_effects

    from kiro_crew.acp import runtime as acp_runtime
    from kiro_crew.acp import runtime_tree_kill

    rt, _reader, proc = _runtime(str(repo))
    _neuter_kill_side_effects(monkeypatch, proc)
    monkeypatch.setattr(runtime_tree_kill.platform_compat, "IS_WINDOWS", True)

    async def _drain_fails(process):
        raise OSError("Windows tree tracking retirement did not complete")

    monkeypatch.setattr(
        runtime_tree_kill.platform_compat, "terminate_windows_asyncio_tree", _drain_fails
    )
    root_gone = False
    monkeypatch.setattr(acp_runtime, "_pid_gone_or_unmanaged", lambda pid: root_gone)
    await rt.send_request(METHOD_PROMPT, {"sessionId": SID, "prompt": []})
    with pytest.raises(OSError):
        await rt.kill(expected=True)
    assert guard.has_lingering()
    assert await _busy(repo) is True
    root_gone = True
    assert await _busy(repo) is False


@pytest.mark.asyncio
async def test_a_windows_pending_tree_keeps_a_dead_root_busy_until_cleanup_finishes(
    repo, monkeypatch
):
    from kiro_crew.acp import runtime_tree_kill

    cleanup_pending = True
    probes: list[tuple[int, str | None]] = []

    def _pending(pid: int, start: str | None) -> bool:
        probes.append((pid, start))
        return cleanup_pending

    monkeypatch.setattr(runtime_tree_kill.platform_compat, "windows_tree_cleanup_pending", _pending)
    guard.prompt_sent("inst-a", 1, str(repo), method=METHOD_PROMPT)
    runtime_tree_kill.end_prompts_after_teardown("inst-a", True, 4242, "start-1", {})
    assert await _busy(repo) is True
    cleanup_pending = False
    assert await _busy(repo) is False
    assert probes and set(probes) == {(4242, "start-1")}
    assert not guard.has_lingering()


def test_a_lingering_probe_that_raises_stays_busy(repo):
    guard.prompt_sent("inst-a", 1, str(repo), method=METHOD_PROMPT)

    def _boom() -> bool:
        raise OSError("proc unreadable")

    guard.prompt_process_lingering("inst-a", _boom)
    assert guard.lingering_gone() == []
    # A process with no unanswered prompt is not recorded at all.
    guard.prompt_process_lingering("inst-z", lambda: True)
    assert set(guard._lingering) == {"inst-a"}


@pytest.mark.asyncio
@pytest.mark.parametrize("confirmed", [True, False])
async def test_a_runtime_kill_ends_its_prompts_only_when_the_tree_is_confirmed_gone(
    repo, monkeypatch, confirmed
):
    from test_acp_runtime_death import _neuter_kill_side_effects

    from kiro_crew.acp import runtime as acp_runtime

    rt, _reader, proc = _runtime(str(repo))
    _neuter_kill_side_effects(monkeypatch, proc)
    await rt.send_request(METHOD_PROMPT, {"sessionId": SID, "prompt": []})
    proc.returncode = 0 if confirmed else None
    root_gone = False
    monkeypatch.setattr(acp_runtime, "_pid_gone_or_unmanaged", lambda pid: root_gone)
    await rt.kill(expected=True)
    assert rt.process_tree_confirmed_dead is confirmed
    assert await _busy(repo) is (not confirmed)
    # A root the kill could not prove dead stays counted until it is gone.
    root_gone = True
    assert await _busy(repo) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["create", "load"])
async def test_a_session_the_runtime_opens_is_placed_on_its_folder(
    repo, tmp_path, monkeypatch, path
):
    from kiro_crew.acp.types import METHOD_SESSION_LOAD, METHOD_SESSION_NEW

    rt, _reader, _proc = _make_runtime()
    rt._process_instance = "inst-a"
    rt._can_load_session = True
    work = str(repo / "sub")

    async def _fake_send(method, params, timeout=None):
        if method == METHOD_SESSION_NEW:
            return {"sessionId": "sid-new", "modes": {"currentModeId": "kirocrew"}}
        if method == METHOD_SESSION_LOAD:
            return {"modes": {"currentModeId": "kirocrew"}, "models": []}
        return {}

    monkeypatch.setattr(rt, "_send_and_await", _fake_send)
    if path == "create":
        handle = await rt.create_session(cwd=work, agent="kirocrew")
    else:
        handle = await rt.load_session(
            str(tmp_path / "sid-123.json"), "sid-123", cwd=work, agent="kirocrew"
        )
    await rt.send_request(METHOD_PROMPT, {"sessionId": handle._session_id, "prompt": []})
    folders, placed = guard.tracked_work_folders()
    assert placed and folders and await _busy(repo) is True
    assert await _busy(tmp_path / "other") is False
    # A destroyed session's folder goes with it; its unanswered prompt does not.
    rt.unregister_session(handle._session_id)
    assert handle._session_id not in rt._session_cwds
    assert await _busy(repo) is True


# ── The adapter client (claude, codex, goose, opencode, ...) ──


def _client(folder: str) -> tuple[AcpClient, MagicMock]:
    client = AcpClient()
    proc = MagicMock()
    proc.stdin = MagicMock()
    proc.stdin.write = MagicMock()
    proc.stdin.drain = AsyncMock()
    proc.stdout = asyncio.StreamReader()
    proc.returncode = None
    client._process = proc
    client._process_instance = "cli-a"
    client._spawn_work_dir = folder
    return client, proc


def _line(obj: dict) -> bytes:
    return (json.dumps(obj) + "\n").encode()


@pytest.mark.asyncio
@pytest.mark.parametrize("method", sorted(guard.PROMPT_METHODS))
async def test_a_client_prompt_is_busy_until_its_answer_is_read(repo, tmp_path, method):
    client, proc = _client(str(repo / "sub"))
    req_id = await client._send_request(method, {"sessionId": "s"})
    assert await _busy(repo) is True
    assert await _busy(tmp_path / "other") is False
    # Unrelated frames, including another request's answer, change nothing.
    proc.stdout.feed_data(_line({"jsonrpc": "2.0", "method": "session/update", "params": {}}))
    proc.stdout.feed_data(_line({"jsonrpc": "2.0", "id": req_id + 7, "result": {}}))
    await client._read_message(timeout=1)
    await client._read_message(timeout=1)
    assert await _busy(repo) is True
    # The answer is read whenever some loop reads it -- here long after any
    # caller stopped waiting -- and only then does the folder go quiet.
    proc.stdout.feed_data(_line({"jsonrpc": "2.0", "id": req_id, "error": {"code": -1}}))
    await client._read_message(timeout=1)
    assert await _busy(repo) is False


@pytest.mark.asyncio
async def test_other_client_requests_are_not_recorded(repo):
    client, _proc = _client(str(repo))
    await client._send_request("session/set_model", {"sessionId": "s", "modelId": "m"})
    assert guard.tracked_work_folders() == ([], True)


@pytest.mark.asyncio
async def test_a_client_prompt_waits_out_a_checkout_of_its_folder(repo):
    client, proc = _client(str(repo))
    root = os.path.realpath(repo)
    seen = _reserve_seen(root)
    send = asyncio.ensure_future(client._send_request(METHOD_PROMPT, {"sessionId": "s"}))
    await await_until(seen.waiting)
    assert not send.done()
    proc.stdin.write.assert_not_called()
    guard.release(root)
    await await_until(send)
    proc.stdin.write.assert_called()
    assert await _busy(repo) is True


@pytest.mark.asyncio
async def test_a_client_prompt_cancelled_while_waiting_for_stdin_is_not_busy(repo):
    client, proc = _client(str(repo))
    lock = _ObservedWriteLock()
    await lock.acquire()
    client._stdin_lock = lock
    send = asyncio.create_task(client._send_request(METHOD_PROMPT, {"sessionId": "s"}))
    try:
        await await_until(lock.waiting)
        assert proc.stdin.write.call_count == 0
        assert await _busy(repo) is True
        send.cancel()
        with pytest.raises(asyncio.CancelledError):
            await await_until(send)
        assert await _busy(repo) is False
    finally:
        if lock.locked():
            lock.release()
        if not send.done():
            send.cancel()
            await asyncio.gather(send, return_exceptions=True)


@pytest.mark.asyncio
async def test_a_client_prompt_cancelled_after_stdin_write_stays_busy(repo):
    client, proc = _client(str(repo))
    drain_entered = asyncio.Event()
    release_drain = asyncio.Event()

    async def _drain() -> None:
        drain_entered.set()
        await release_drain.wait()

    proc.stdin.drain = _drain
    send = asyncio.create_task(client._send_request(METHOD_PROMPT, {"sessionId": "s"}))
    try:
        await await_until(drain_entered)
        proc.stdin.write.assert_called_once()
        send.cancel()
        with pytest.raises(asyncio.CancelledError):
            await await_until(send)
        assert await _busy(repo) is True
    finally:
        release_drain.set()
        if not send.done():
            send.cancel()
            await asyncio.gather(send, return_exceptions=True)
        guard.prompt_process_gone("cli-a")


@pytest.mark.asyncio
async def test_a_client_prompt_with_no_folder_is_busy_everywhere(tmp_path):
    client, _proc = _client("")
    await client._send_request(METHOD_PROMPT, {"sessionId": "s"})
    assert await _busy(tmp_path / "other") is True


@pytest.mark.asyncio
@pytest.mark.parametrize("confirmed", [True, False])
async def test_a_client_teardown_ends_its_prompts_only_when_the_tree_is_confirmed_gone(
    repo, monkeypatch, confirmed
):
    client, proc = _client(str(repo))
    await client._send_request(METHOD_PROMPT, {"sessionId": "s"})
    proc.returncode = 0 if confirmed else None
    client._pid = None
    client._child_pids = {}
    client._reset_state()
    assert client.process_tree_confirmed_dead is confirmed
    assert await _busy(repo) is (not confirmed)


# ── Every writer, every reader ──


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome, counted",
    [("injected", False), ("startedNewTurn", True)],
    ids=["injected", "new-turn"],
)
async def test_a_runtime_steering_request_is_counted_through_its_own_writer(repo, outcome, counted):
    from kiro_crew.acp.types import METHOD_SESSION_STEERING

    rt, reader, proc = _runtime(str(repo))
    answer = await rt.send_request_for_answer(
        METHOD_SESSION_STEERING, {"sessionId": SID, "prompt": []}
    )
    assert await _busy(repo) is True
    sent = json.loads(proc.stdin.write.call_args.args[0])
    reader_task = asyncio.ensure_future(rt._reader_loop())
    try:
        _feed(reader, {"jsonrpc": "2.0", "id": sent["id"], "result": {"outcome": outcome}})
        await await_until(answer)
        assert await _busy(repo) is counted
    finally:
        reader_task.cancel()
        await asyncio.gather(reader_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_a_runtime_control_request_writer_counts_a_prompt_too(repo, monkeypatch):
    rt, _reader, _proc = _runtime(str(repo))
    sent = asyncio.ensure_future(
        rt._send_and_await(METHOD_PROMPT, {"sessionId": SID, "prompt": []}, timeout=60)
    )
    await async_wait_until(lambda: guard.tracked_work_folders()[0] != [])
    assert await _busy(repo) is True
    sent.cancel()
    await asyncio.gather(sent, return_exceptions=True)


@pytest.mark.asyncio
async def test_a_runtime_answer_with_a_non_integer_id_ends_nothing(repo):
    rt, reader, _proc = _runtime(str(repo))
    req_id = await rt.send_request(METHOD_PROMPT, {"sessionId": SID, "prompt": []})
    reader_task = asyncio.ensure_future(rt._reader_loop())
    try:
        _feed(reader, {"jsonrpc": "2.0", "id": req_id + 0.9, "result": {}})
        _feed(reader, {"jsonrpc": "2.0", "id": str(req_id), "result": {}})
        _feed(reader, {"jsonrpc": "2.0", "method": "noop", "params": {}})
        await _until_reader_saw_noop(rt)
        assert await _busy(repo) is True
    finally:
        reader_task.cancel()
        await asyncio.gather(reader_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_a_runtime_answer_whose_result_is_null_still_ends_the_prompt(repo):
    # {"result": null} is a valid JSON-RPC answer; AcpClient ends the prompt on
    # it, so the runtime must too or the repository reads busy until a kill.
    rt, reader, _proc = _runtime(str(repo))
    req_id = await rt.send_request(METHOD_PROMPT, {"sessionId": SID, "prompt": []})
    assert await _busy(repo) is True
    reader_task = asyncio.ensure_future(rt._reader_loop())
    try:
        _feed(reader, {"jsonrpc": "2.0", "id": req_id, "result": None})
        await async_wait_until(lambda: guard.tracked_work_folders() == ([], True))
        assert await _busy(repo) is False
    finally:
        reader_task.cancel()
        await asyncio.gather(reader_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_a_runtime_that_dies_on_its_own_ends_its_prompts(repo, monkeypatch):
    # An OOM kill or a crash reaches no kill(), so the exit path itself must end
    # the unanswered prompt once the root is confirmed gone.
    from kiro_crew import platform_compat

    rt, _reader, proc = _runtime(str(repo))
    await rt.send_request(METHOD_PROMPT, {"sessionId": SID, "prompt": []})
    assert await _busy(repo) is True
    proc.returncode = -9
    monkeypatch.setattr(platform_compat, "pid_exists", lambda pid: False)
    await rt._retire_tracking_after_exit()
    assert await _busy(repo) is False


@pytest.mark.asyncio
async def test_a_runtime_whose_exit_is_not_confirmed_keeps_its_prompts(repo, monkeypatch):
    from kiro_crew import platform_compat

    rt, _reader, proc = _runtime(str(repo))
    await rt.send_request(METHOD_PROMPT, {"sessionId": SID, "prompt": []})
    proc.returncode = -9
    # The pid still answers: the exit is not proven, so nothing is released.
    monkeypatch.setattr(platform_compat, "pid_exists", lambda pid: True)
    await rt._retire_tracking_after_exit()
    assert await _busy(repo) is True


@pytest.mark.asyncio
async def test_a_client_answer_with_a_non_integer_id_ends_nothing(repo):
    client, proc = _client(str(repo))
    req_id = await client._send_request(METHOD_PROMPT, {"sessionId": "s"})
    proc.stdout.feed_data(_line({"jsonrpc": "2.0", "id": req_id + 0.9, "result": {}}))
    proc.stdout.feed_data(_line({"jsonrpc": "2.0", "id": True, "result": {}}))
    await client._read_message(timeout=1)
    await client._read_message(timeout=1)
    assert await _busy(repo) is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome, counted",
    [("promptRequired", False), ("startedNewTurn", True)],
    ids=["idle", "new-turn"],
)
async def test_a_client_steer_that_started_a_turn_stays_counted(repo, outcome, counted):
    from kiro_crew.acp.types import METHOD_SESSION_STEERING

    client, proc = _client(str(repo))
    req_id = await client._send_request(METHOD_SESSION_STEERING, {"sessionId": "s"})
    proc.stdout.feed_data(_line({"jsonrpc": "2.0", "id": req_id, "result": {"outcome": outcome}}))
    await client._read_message(timeout=1)
    assert await _busy(repo) is counted


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "later, released",
    [
        ({"session": "s", "answer": {"result": {"stopReason": "end_turn"}}}, True),
        ({"session": "t", "answer": {"result": {"stopReason": "end_turn"}}}, False),
        ({"session": "s", "answer": {"error": {"code": -32000}}}, False),
    ],
    ids=["same-session", "other-session", "error-answer"],
)
async def test_a_later_turn_on_the_same_session_ends_a_turn_the_adapter_started(
    repo, later, released
):
    """An adapter runs one turn at a time per session, so a later turn prompt on
    that session that the backend FINISHED ends the turn it started from a
    steer; a turn on another session, or an error answer (the adapter refusing
    a turn while its own runs), does not."""
    from kiro_crew.acp.types import METHOD_SESSION_STEERING

    client, proc = _client(str(repo))
    steer_id = await client._send_request(METHOD_SESSION_STEERING, {"sessionId": "s"})
    proc.stdout.feed_data(
        _line({"jsonrpc": "2.0", "id": steer_id, "result": {"outcome": "startedNewTurn"}})
    )
    await client._read_message(timeout=1)
    assert await _busy(repo) is True
    turn_id = await client._send_request(METHOD_PROMPT, {"sessionId": later["session"]})
    proc.stdout.feed_data(_line({"jsonrpc": "2.0", "id": turn_id, **later["answer"]}))
    await client._read_message(timeout=1)
    assert await _busy(repo) is (not released)


@pytest.mark.asyncio
async def test_a_turn_sent_before_the_steer_never_ends_the_adapter_turn(repo):
    from kiro_crew.acp.types import METHOD_SESSION_STEERING

    client, proc = _client(str(repo))
    earlier_id = await client._send_request(METHOD_PROMPT, {"sessionId": "s"})
    steer_id = await client._send_request(METHOD_SESSION_STEERING, {"sessionId": "s"})
    proc.stdout.feed_data(
        _line({"jsonrpc": "2.0", "id": steer_id, "result": {"outcome": "startedNewTurn"}})
    )
    proc.stdout.feed_data(
        _line({"jsonrpc": "2.0", "id": earlier_id, "result": {"stopReason": "end_turn"}})
    )
    await client._read_message(timeout=1)
    await client._read_message(timeout=1)
    assert await _busy(repo) is True


@pytest.mark.asyncio
async def test_a_duplicate_answer_never_releases_a_turn_the_runtime_adapter_started(repo):
    from kiro_crew.acp.types import METHOD_SESSION_STEERING

    rt, reader, proc = _runtime(str(repo))
    await rt.send_request_for_answer(METHOD_SESSION_STEERING, {"sessionId": SID, "prompt": []})
    sent = json.loads(proc.stdin.write.call_args.args[0])
    reader_task = asyncio.ensure_future(rt._reader_loop())
    try:
        for result in ({"outcome": "startedNewTurn"}, {"outcome": "injected"}, {}):
            _feed(reader, {"jsonrpc": "2.0", "id": sent["id"], "result": result})
        _feed(reader, {"jsonrpc": "2.0", "id": sent["id"], "error": {"code": -1}})
        _feed(reader, {"jsonrpc": "2.0", "method": "noop", "params": {}})
        await _until_reader_saw_noop(rt)
        assert await _busy(repo) is True
    finally:
        reader_task.cancel()
        await asyncio.gather(reader_task, return_exceptions=True)
    # Only the process tree's confirmed exit ends it.
    guard.prompt_process_gone("inst-a")
    assert await _busy(repo) is False


@pytest.mark.asyncio
async def test_a_duplicate_answer_never_releases_a_turn_the_client_adapter_started(repo):
    from kiro_crew.acp.types import METHOD_SESSION_STEERING

    client, proc = _client(str(repo))
    req_id = await client._send_request(METHOD_SESSION_STEERING, {"sessionId": "s"})
    for result in ({"outcome": "startedNewTurn"}, {"outcome": "injected"}):
        proc.stdout.feed_data(_line({"jsonrpc": "2.0", "id": req_id, "result": result}))
        await client._read_message(timeout=1)
    assert await _busy(repo) is True


def test_every_checkpoint_verb_is_tracked():
    from kiro_crew.acp.types import METHOD_KAS_CHECKPOINT_REVERT

    assert guard.is_tracked(METHOD_KAS_CHECKPOINT_REVERT)
    assert guard.is_tracked("_kiro/checkpoint/revert")
    for method in ("session/set_mode", "_kiro/session/delete", "_kiro/mcp/resetServer"):
        assert not guard.is_tracked(method)


@pytest.mark.asyncio
async def test_a_checkpoint_restore_counts_until_its_answer_whoever_stopped_waiting(repo):
    from kiro_crew.acp.types import METHOD_KAS_CHECKPOINT_REVERT

    rt, reader, _proc = _runtime(str(repo / "sub"))
    root = os.path.realpath(repo)
    # It waits out a switch of its folder before it is written...
    seen = _reserve_seen(root)
    send = asyncio.ensure_future(
        rt.send_request(METHOD_KAS_CHECKPOINT_REVERT, {"sessionId": SID, "messageId": "m"})
    )
    await await_until(seen.waiting)
    assert not send.done()
    guard.release(root)
    req_id = await await_until(send)
    # ...and counts, though its caller gave up, until the backend answers it.
    assert await _busy(repo) is True
    reader_task = asyncio.ensure_future(rt._reader_loop())
    try:
        _feed(reader, {"jsonrpc": "2.0", "id": req_id, "result": {"success": True}})
        await async_wait_until(lambda: guard.tracked_work_folders() == ([], True))
    finally:
        reader_task.cancel()
        await asyncio.gather(reader_task, return_exceptions=True)


# ── The knowledge pool's legacy stream-json worker ──


@pytest.mark.asyncio
async def test_a_cc_worker_prompt_never_marks_a_repo_busy(repo, monkeypatch):
    """It summarizes text in the gateway's own working directory and never works
    on a project, so its prompts, finished or abandoned, are not counted."""
    from kiro_crew.knowledge.llm_pool import CCWorker

    monkeypatch.chdir(repo)
    worker = CCWorker(sandbox_mode="off")
    proc = MagicMock()
    proc.returncode = None
    proc.stdin = MagicMock()
    proc.stdin.write = MagicMock()
    worker._proc = proc
    worker.shutdown = AsyncMock()
    seen: list[bool] = []

    async def _drain():
        seen.append(await _busy(repo))

    proc.stdin.drain = _drain
    with pytest.raises(asyncio.TimeoutError):
        await worker.send_message("hi", timeout=0.05)  # flake-ok: the timeout is the case
    assert seen == [False]
    assert await _busy(repo) is False
