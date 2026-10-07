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
from kiro_crew.testing.wait import async_wait_until, await_until

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
            raced.append(root)
            assert guard.reserve(root)
        return result

    monkeypatch.setattr(asyncio, "to_thread", _match_then_race)
    assert guard.reserve(other)
    admitted = asyncio.ensure_future(guard.before_prompt(str(repo / "sub")))
    await async_wait_until(lambda: bool(raced))
    guard.release(other)
    for _ in range(20):
        await asyncio.sleep(0)
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
    assert guard.reserve(root)
    send = asyncio.ensure_future(rt.send_request(METHOD_PROMPT, {"sessionId": SID, "prompt": []}))
    for _ in range(20):
        await asyncio.sleep(0)
    assert not send.done()
    proc.stdin.write.assert_not_called()
    assert guard.tracked_work_folders() == ([], True)
    guard.release(root)
    await await_until(send)
    proc.stdin.write.assert_called()
    assert await _busy(repo) is True


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
        for _ in range(50):
            await asyncio.sleep(0)
        assert await _busy(repo) is True
    finally:
        reader_task.cancel()
        await asyncio.gather(reader_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_a_confirmed_teardown_ends_the_runtime_prompts(repo, monkeypatch):
    rt, _reader, proc = _runtime(str(repo))
    await rt.send_request(METHOD_PROMPT, {"sessionId": SID, "prompt": []})
    # A teardown that left a survivor keeps the record...
    rt._teardown_instance = "inst-a"
    rt._teardown_survivors = {9999: object()}
    monkeypatch.setattr(
        "kiro_crew.acp.runtime._prune_dead_descendants", lambda children: list(children)
    )
    await rt._recheck_teardown_survivors()
    assert await _busy(repo) is True
    # ...until a re-check confirms the whole tree gone.
    monkeypatch.setattr("kiro_crew.acp.runtime._prune_dead_descendants", lambda children: [])
    await rt._recheck_teardown_survivors()
    assert rt.process_tree_confirmed_dead
    assert await _busy(repo) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("confirmed", [True, False])
async def test_a_runtime_kill_ends_its_prompts_only_when_the_tree_is_confirmed_gone(
    repo, monkeypatch, confirmed
):
    from test_acp_runtime_death import _neuter_kill_side_effects

    rt, _reader, proc = _runtime(str(repo))
    _neuter_kill_side_effects(monkeypatch, proc)
    await rt.send_request(METHOD_PROMPT, {"sessionId": SID, "prompt": []})
    proc.returncode = 0 if confirmed else None
    await rt.kill(expected=True)
    assert rt.process_tree_confirmed_dead is confirmed
    assert await _busy(repo) is (not confirmed)


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
    assert guard.reserve(root)
    send = asyncio.ensure_future(client._send_request(METHOD_PROMPT, {"sessionId": "s"}))
    for _ in range(20):
        await asyncio.sleep(0)
    assert not send.done()
    proc.stdin.write.assert_not_called()
    guard.release(root)
    await await_until(send)
    proc.stdin.write.assert_called()
    assert await _busy(repo) is True


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
        for _ in range(50):
            await asyncio.sleep(0)
        assert await _busy(repo) is True
    finally:
        reader_task.cancel()
        await asyncio.gather(reader_task, return_exceptions=True)


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
        for _ in range(50):
            await asyncio.sleep(0)
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
    assert guard.reserve(root)
    send = asyncio.ensure_future(
        rt.send_request(METHOD_KAS_CHECKPOINT_REVERT, {"sessionId": SID, "messageId": "m"})
    )
    for _ in range(20):
        await asyncio.sleep(0)
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


def _cc_worker(folder: str):
    from kiro_crew.knowledge.llm_pool import CCWorker

    worker = CCWorker(sandbox_mode="off")
    proc = MagicMock()
    proc.returncode = None
    proc.stdin = MagicMock()
    proc.stdin.write = MagicMock()
    worker._proc = proc
    worker._instance = "cc-a"
    worker._spawn_cwd = folder
    worker.shutdown = AsyncMock()
    return worker, proc


@pytest.mark.asyncio
async def test_a_cc_worker_prompt_is_counted_until_its_result_is_read(repo):
    worker, proc = _cc_worker(str(repo))
    seen: list[bool] = []

    async def _drain():
        seen.append(await _busy(repo))
        await worker._event_queue.put({"type": "result", "result": "ok"})

    proc.stdin.drain = _drain
    assert await worker.send_message("hi", timeout=60) == "ok"
    assert seen == [True]
    assert await _busy(repo) is False


@pytest.mark.asyncio
async def test_a_cc_worker_reply_that_never_completes_stays_counted(repo):
    worker, proc = _cc_worker(str(repo))
    proc.stdin.drain = AsyncMock()
    with pytest.raises(asyncio.TimeoutError):
        await worker.send_message("hi", timeout=0.05)  # flake-ok: the timeout is the case
    worker.shutdown.assert_awaited()
    assert await _busy(repo) is True
