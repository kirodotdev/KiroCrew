"""Gateway sweeps keep enrolled Fargate tasks alive without a worker pipe."""

from __future__ import annotations

import asyncio
import socket
import threading
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from test_fargate_session import PROFILE, SECRET, sync

from kiro_crew.coder import retention
from kiro_crew.fargate_session import launch
from kiro_crew.remote_session import fargate_bootstrap as boot
from kiro_crew.session_environment import SessionEnvironmentUnavailable


@pytest.fixture
def environments(tmp_path, monkeypatch):
    envs = launch.FargateSessionEnvironments(SimpleNamespace(fargate_profiles=[PROFILE]), tmp_path)
    monkeypatch.setattr(retention, "_fargate_environments", AsyncMock(return_value=envs))
    monkeypatch.setattr(retention, "_environments", AsyncMock(return_value=None))
    return envs


def record(envs, key="live123", *, runtime="r1"):
    row = envs._record_task(
        launch.SessionTask(
            f"dashboard:{key}", str(PROFILE["id"]), f"arn-task-{key}", key, "dev", runtime
        ),
        PROFILE,
    )
    envs._save_secret(row.task_id, SECRET)
    return row


@pytest.mark.parametrize("authenticated", [False, True])
@sync
async def test_sweeps_touch_the_authenticated_bootstrap_without_any_worker_pipe(
    environments, monkeypatch, authenticated
):
    envs = environments
    row = record(envs)
    now = [1000.0]
    monkeypatch.setattr(boot, "time", SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(boot, "_activity", {"last": now[0], "pipes": 0})
    monkeypatch.setattr(boot, "_running", set())
    monkeypatch.setattr(boot, "_state", {"secret": SECRET if authenticated else b"x" * 32})
    run = Mock(return_value={"code": 0, "stdout": "", "stderr": ""})
    monkeypatch.setattr(boot, "_run", run)
    servers = []

    async def connect():
        client, server = socket.socketpair()
        client.setblocking(False)
        servers.append(asyncio.create_task(asyncio.to_thread(boot._serve, server)))
        return await asyncio.open_connection(sock=client)

    monkeypatch.setattr(envs, "forward", lambda *_: SimpleNamespace(connect=connect))
    try:
        for _ in range(3):
            now[0] += 600.0
            assert await retention.sweep_once() == 0
            assert not boot._running and boot._activity["pipes"] == 0
            assert boot._activity["last"] == (now[0] if authenticated else 1000.0)
            assert envs.registry.get(row.session_key) == row
        if authenticated:
            assert run.call_count == 3
            for call in run.call_args_list:
                assert call.args[0] == {"op": "run", "command": "true", "stdin": ""}
            assert boot._idle_for(now[0]) == 0
        else:
            run.assert_not_called()
            assert boot._idle_for(now[0]) == 1800.0

        exited = []
        last_touch = boot._activity["last"]

        def advance(_seconds):
            now[0] += 60.0

        boot._idle_watch(
            900.0, sleep=advance, clock=lambda: now[0], exit_fn=lambda: exited.append(now[0])
        )
        assert len(exited) == 1 and exited[0] > last_touch + 900.0
    finally:
        await asyncio.wait_for(asyncio.gather(*servers), 30)


@sync
async def test_keepalive_skips_unenrolled_and_stopping_tasks_and_reads_off_loop(
    environments, monkeypatch
):
    envs = environments
    active = record(envs)
    record(envs, "starting123", runtime="")
    stopped = record(envs, "stopped123")
    assert envs._record_stop(stopped, "archived")
    loop_thread = threading.get_ident()
    all_rows, get_row, verify, load = (
        envs.registry.all,
        envs.registry.get,
        envs._verify_binding,
        envs.load_secret,
    )
    seen = []

    def off_loop(name, function):
        def check(*args):
            assert threading.get_ident() != loop_thread
            seen.append(name)
            return function(*args)

        return check

    monkeypatch.setattr(envs.registry, "all", off_loop("all", all_rows))
    monkeypatch.setattr(envs.registry, "get", off_loop("get", get_row))
    monkeypatch.setattr(envs, "_verify_binding", off_loop("verify", verify))
    monkeypatch.setattr(envs, "load_secret", off_loop("secret", load))
    run = AsyncMock(return_value=(0, b"", ""))
    connect = AsyncMock()
    forward = Mock(return_value=SimpleNamespace(connect=connect))
    monkeypatch.setattr(envs, "forward", forward)
    monkeypatch.setattr(launch.bootstrap, "run", run)
    assert await envs.keep_alive() == 1
    assert set(seen) == {"all", "get", "verify", "secret"}
    run.assert_awaited_once_with(connect, SECRET, "true", b"")
    assert forward.call_args.args[1] == active.task


@pytest.mark.parametrize("failure", ["connection", "command", "tampered", "no_secret"])
@sync
async def test_keepalive_failure_does_not_stop_other_tasks_or_coder_sweep(
    environments, monkeypatch, failure
):
    envs = environments
    bad, good = record(envs, "a123"), record(envs, "b123")
    run = AsyncMock(return_value=(0, b"", ""))
    if failure == "tampered":
        envs.registry.put(replace(bad, cluster="injected"))
    elif failure == "no_secret":
        envs._forget_secret(bad.task_id)

    def forward(profile, task):
        if task.task_id == bad.task_id:
            if failure == "connection":
                raise OSError("connection lost")
            return SimpleNamespace(connect=AsyncMock())
        return SimpleNamespace(connect=good_connect)

    good_connect = AsyncMock()

    async def response(connect, secret, command, stdin):
        if connect is not good_connect:
            assert failure == "command"
            return 1, b"", "command refused"
        return 0, b"", ""

    run.side_effect = response
    monkeypatch.setattr(envs, "forward", forward)
    monkeypatch.setattr(launch.bootstrap, "run", run)
    coder = object()
    sweep = AsyncMock(return_value=2)
    monkeypatch.setattr(retention, "_environments", AsyncMock(return_value=coder))
    monkeypatch.setattr(retention, "sweep_due", sweep)
    assert await retention.sweep_once() == 2
    assert any(call.args == (good_connect, SECRET, "true", b"") for call in run.await_args_list)
    sweep.assert_awaited_once_with(coder)
    assert envs.registry.get(good.session_key) == good


@sync
async def test_keepalive_limits_concurrent_probes_and_cancels_cleanly(environments, monkeypatch):
    envs = environments
    capacity = launch._KEEPALIVE_CONCURRENCY
    for n in range(capacity + 2):
        record(envs, f"task{n}")
    entered = asyncio.Event()
    release = asyncio.Event()
    started, finished = [], []

    async def shell_for(session_key, profile_id):
        async def run(command):
            assert command == "true"
            started.append(session_key)
            if len(started) == capacity:
                entered.set()
            try:
                await asyncio.wait_for(release.wait(), 30)
            finally:
                finished.append(session_key)

        return SimpleNamespace(run=run)

    monkeypatch.setattr(envs, "shell_for", shell_for)
    task = asyncio.create_task(envs.keep_alive())
    try:
        await asyncio.wait_for(entered.wait(), 30)
        assert len(started) == capacity
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 30)
        assert sorted(finished) == sorted(started)
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert await envs.keep_alive() == capacity + 2


@sync
async def test_keepalive_timeout_is_bounded_and_next_pass_can_retry(environments, monkeypatch):
    envs = environments
    row = record(envs)
    run = AsyncMock(return_value=b"")
    monkeypatch.setattr(envs, "shell_for", AsyncMock(return_value=SimpleNamespace(run=run)))
    with monkeypatch.context() as patched:
        patched.setattr(launch, "_KEEPALIVE_TIMEOUT_SECS", 0)
        assert await envs.keep_alive() == 0
    run.assert_not_awaited()
    assert envs.registry.get(row.session_key) == row
    assert await envs.keep_alive() == 1
    run.assert_awaited_once_with("true")


@sync
async def test_keepalive_rereads_stop_intent_after_waiting_for_lifecycle_lock(
    environments, monkeypatch
):
    envs = environments
    row = record(envs)
    run = AsyncMock(return_value=(0, b"", ""))
    monkeypatch.setattr(launch.bootstrap, "run", run)
    monkeypatch.setattr(envs, "forward", Mock(side_effect=AssertionError("stopping task")))
    all_rows = envs.registry.all

    def stopping_rows():
        rows = all_rows()
        assert envs._record_stop(row, "archived")
        return rows

    monkeypatch.setattr(envs.registry, "all", stopping_rows)
    assert await envs.keep_alive() == 0
    run.assert_not_awaited()
    assert envs.registry.get(row.session_key).stop_reason == "archived"


@sync
async def test_no_registry_rows_means_no_keepalive_connections(environments, monkeypatch):
    shell_for = AsyncMock(side_effect=SessionEnvironmentUnavailable("no tasks"))
    monkeypatch.setattr(environments, "shell_for", shell_for)
    assert await retention.sweep_once() == 0
    shell_for.assert_not_awaited()


@pytest.mark.parametrize("change", ["injected_stop", "removed_profile"])
@sync
async def test_live_task_keeps_its_heartbeat_without_trusting_an_unsigned_stop_or_config(
    environments, monkeypatch, change
):
    envs = environments
    row = record(envs)
    if change == "injected_stop":
        envs.registry.put(replace(row, stop_reason="injected archive"))
    else:
        envs._config.fargate_profiles = []
    connect = AsyncMock()
    forward = Mock(return_value=SimpleNamespace(connect=connect))
    run = AsyncMock(return_value=(0, b"", ""))
    describe = AsyncMock()
    stop = AsyncMock()
    monkeypatch.setattr(envs, "forward", forward)
    monkeypatch.setattr(launch.bootstrap, "run", run)
    monkeypatch.setattr(launch.tasks, "describe", describe)
    monkeypatch.setattr(launch.tasks, "stop_task", stop)
    before = envs.registry.path.read_bytes()
    assert await retention.sweep_once() == 0
    run.assert_awaited_once_with(connect, SECRET, "true", b"")
    describe.assert_not_awaited()
    stop.assert_not_awaited()
    assert envs.registry.path.read_bytes() == before
    assert envs.load_secret(row.task_id) == SECRET
