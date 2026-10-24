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
from kiro_crew.testing.wait import await_until


@pytest.fixture(autouse=True)
def _developer_flag_on(_floor_monkeypatch):
    # Session Environments are reachable only with the developer flag.
    _floor_monkeypatch.setenv("KIROCREW_DEV_MODE", "1")


@pytest.fixture(autouse=True)
def _ecs_reports_tasks_running(_floor_monkeypatch):
    # A failed probe asks ECS whether the task is gone; no test reaches real AWS.
    from kiro_crew.fargate_session import tasks

    _floor_monkeypatch.setattr(launch, "_RECONCILED_AT", {}, raising=False)
    _floor_monkeypatch.setattr(tasks, "describe", AsyncMock(return_value={"lastStatus": "RUNNING"}))


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

    monkeypatch.setattr(
        envs, "forward", lambda *_: SimpleNamespace(connect=connect, verify_peer=AsyncMock())
    )
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
    forward = Mock(return_value=SimpleNamespace(connect=connect, verify_peer=AsyncMock()))
    monkeypatch.setattr(envs, "forward", forward)
    monkeypatch.setattr(launch.bootstrap, "run", run)
    assert await envs.keep_alive() == 1
    assert set(seen) == {"all", "get", "verify", "secret"}
    run.assert_awaited_once_with(
        connect, SECRET, "true", b"", verify_peer=forward.return_value.verify_peer
    )
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
            return SimpleNamespace(connect=AsyncMock(), verify_peer=AsyncMock())
        return SimpleNamespace(connect=good_connect, verify_peer=AsyncMock())

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
    assert envs.registry.get(bad.session_key) is not None  # unreachable, not gone


@pytest.mark.parametrize("ecs_state", ["STOPPED", "purged", "RUNNING"])
@sync
async def test_a_task_that_stopped_on_its_own_is_forgotten_after_one_failed_probe(
    environments, monkeypatch, ecs_state
):
    """The gateway was down past the idle exit: ECS stopped the task meanwhile."""
    from kiro_crew.fargate_session import tasks

    envs = environments
    row = record(envs, "gone123")
    monkeypatch.setattr(launch, "_RECONCILED_AT", {}, raising=False)

    def forward(profile, task):
        async def connect():
            raise OSError("ssm: target not connected")

        return SimpleNamespace(connect=connect, verify_peer=AsyncMock())

    info = {
        "taskArn": row.arn,
        "lastStatus": ecs_state,
        "tags": [{"key": tasks.SESSION_TAG, "value": tasks.session_tag(row.session_key)}],
    }
    describe = AsyncMock(return_value=None if ecs_state == "purged" else info)
    stop_task = AsyncMock()
    dropped = AsyncMock()
    monkeypatch.setattr(envs, "forward", forward)
    monkeypatch.setattr(envs, "_drop_forward", dropped)
    monkeypatch.setattr(tasks, "describe", describe)
    monkeypatch.setattr(tasks, "stop_task", stop_task)

    assert await envs.keep_alive() == 0
    stop_task.assert_not_awaited()  # a stopped or purged task needs no StopTask
    if ecs_state == "RUNNING":
        # Up but unreachable: kept, and asked about again only after the gap.
        assert envs.registry.get(row.session_key) == row
        assert envs.load_secret(row.task_id) == SECRET
        assert await envs.keep_alive() == 0
        assert describe.await_count == 1
    else:
        assert envs.registry.get(row.session_key) is None
        assert envs.load_secret(row.task_id) is None
        dropped.assert_awaited_with(row.task_id)
        assert await envs.keep_alive() == 0  # nothing left to re-dial
        assert describe.await_count == 2  # the reconcile read plus _stop's own


@sync
async def test_a_forged_row_never_reaches_ecs_from_a_failed_probe(environments, monkeypatch):
    from kiro_crew.fargate_session import tasks

    envs = environments
    row = record(envs, "forged123")
    envs.registry.put(replace(row, cluster="injected"))
    monkeypatch.setattr(launch, "_RECONCILED_AT", {}, raising=False)
    describe = AsyncMock(return_value=None)
    monkeypatch.setattr(tasks, "describe", describe)
    assert await envs.keep_alive() == 0
    describe.assert_not_awaited()
    assert envs.registry.get(row.session_key) is not None


@sync
async def test_keepalive_probes_every_task_at_once_and_cancels_cleanly(environments, monkeypatch):
    """Stalled probes never queue a reachable task behind them past its idle exit."""
    envs = environments
    stalled = 6  # more than any fixed worker pool would run at once
    for n in range(stalled):
        record(envs, f"task{n}")
    record(envs, "healthy")
    entered = asyncio.Event()
    release = asyncio.Event()
    started, finished, healthy = [], [], []

    async def shell_for(session_key, profile_id):
        async def run(command):
            assert command == "true"
            if session_key == "dashboard:healthy":
                healthy.append(session_key)
                return b""
            started.append(session_key)
            if len(started) == stalled:
                entered.set()
            try:
                await release.wait()  # a stalled forward: never answers on its own
            finally:
                finished.append(session_key)

        return SimpleNamespace(run=run)

    monkeypatch.setattr(envs, "shell_for", shell_for)
    task = asyncio.create_task(envs.keep_alive())
    try:
        # Every stalled probe is in flight together, and the reachable task was
        # touched without waiting for any of them.
        await await_until(entered, what="all stalled probes in flight at once")
        # None of them had to time out first to make room for the rest.
        assert not finished
        assert len(started) == stalled and healthy == ["dashboard:healthy"]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert sorted(finished) == sorted(started)
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert await envs.keep_alive() == stalled + 1


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
    forward = Mock(return_value=SimpleNamespace(connect=connect, verify_peer=AsyncMock()))
    run = AsyncMock(return_value=(0, b"", ""))
    describe = AsyncMock()
    stop = AsyncMock()
    monkeypatch.setattr(envs, "forward", forward)
    monkeypatch.setattr(launch.bootstrap, "run", run)
    monkeypatch.setattr(launch.tasks, "describe", describe)
    monkeypatch.setattr(launch.tasks, "stop_task", stop)
    before = envs.registry.path.read_bytes()
    assert await retention.sweep_once() == 0
    run.assert_awaited_once_with(
        connect, SECRET, "true", b"", verify_peer=forward.return_value.verify_peer
    )
    describe.assert_not_awaited()
    stop.assert_not_awaited()
    assert envs.registry.path.read_bytes() == before
    assert envs.load_secret(row.task_id) == SECRET


@pytest.mark.parametrize("pending_stop", [False, True])
@sync
async def test_restart_with_no_profiles_maintains_persisted_tasks(
    environments, monkeypatch, pending_stop
):
    from pathlib import Path

    from kiro_crew.config import live, paths
    from kiro_crew.slack import gateway

    row = record(environments)
    if pending_stop:
        assert environments._record_stop(row, "archived")
    config = SimpleNamespace(coder_url="", profiles=[], fargate_profiles=[])
    restored = launch.FargateSessionEnvironments(config, environments.data_home)
    monkeypatch.setattr(live, "snapshot", lambda: SimpleNamespace(session_environments=config))
    monkeypatch.setattr(paths, "data_home", lambda: environments.data_home)
    monkeypatch.setattr(retention, "_fargate_environments", AsyncMock(return_value=restored))
    monkeypatch.setattr(gateway, "_CODER_RETENTION_TASK", None)
    run = AsyncMock(return_value=(0, b"", ""))
    connect = AsyncMock()
    verify_peer = AsyncMock()
    monkeypatch.setattr(
        restored,
        "forward",
        Mock(return_value=SimpleNamespace(connect=connect, verify_peer=verify_peer)),
    )
    monkeypatch.setattr(launch.bootstrap, "run", run)
    describe = AsyncMock(return_value=None)
    monkeypatch.setattr(launch.tasks, "describe", describe)
    loop_thread = threading.get_ident()
    stat = Path.stat

    def checked_stat(path, *args, **kwargs):
        if path == restored.registry.path:
            assert threading.get_ident() != loop_thread
        return stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", checked_stat)

    async def sweep():
        assert await retention.sweep_once() == int(pending_stop)

    monkeypatch.setattr(retention, "run_sweeper", sweep)
    gateway._start_coder_retention(SimpleNamespace(session_environments=config))
    task = gateway._CODER_RETENTION_TASK
    assert task is not None
    await asyncio.wait_for(task, 30)
    if pending_stop:
        describe.assert_awaited_once()
        run.assert_not_awaited()
        assert restored.registry.get(row.session_key) is None
        assert restored.load_secret(row.task_id) is None
    else:
        run.assert_awaited_once_with(connect, SECRET, "true", b"", verify_peer=verify_peer)
        describe.assert_not_awaited()
        assert restored.registry.get(row.session_key) == row
        assert restored.load_secret(row.task_id) == SECRET


@sync
async def test_a_slow_stop_retry_never_delays_the_live_tasks_heartbeat(monkeypatch):
    """A pending stop stuck on ECS must not hold back keep-alives past the idle exit."""
    beats = []
    enough = asyncio.Event()
    stuck = asyncio.Event()  # never set: the retried stop hangs, as on a dead ECS endpoint

    async def keep_alive():
        beats.append(1)
        if len(beats) >= 3:
            enough.set()
        return 1

    async def retry_stops():
        await stuck.wait()
        return 0

    fake = SimpleNamespace(keep_alive=keep_alive, retry_stops=retry_stops)
    monkeypatch.setattr(retention, "_fargate_environments", AsyncMock(return_value=fake))
    monkeypatch.setattr(retention, "_environments", AsyncMock(return_value=None))
    sweeper = asyncio.create_task(retention.run_sweeper(interval=0.01, initial_delay=0))
    try:
        await await_until(enough, what="three heartbeats while a stop retry hangs")
    finally:
        sweeper.cancel()
        with pytest.raises(asyncio.CancelledError):
            await sweeper
    assert len(beats) >= 3


def test_the_idle_exit_cannot_be_turned_off_from_the_task_environment(monkeypatch):
    """The idle exit is the only stop once the gateway is gone, so no env var moves it."""
    import importlib.util

    monkeypatch.setenv("KIROCREW_BOOTSTRAP_IDLE_SECS", "0")
    # A private copy under its own name: the shared module and its state stay untouched.
    spec = importlib.util.spec_from_file_location("_fargate_bootstrap_idle_probe", boot.__file__)
    assert spec is not None and spec.loader is not None
    private = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(private)
    assert private.IDLE_SECS == 900.0
    assert private is not boot
