"""Fargate enrollment, stop-retry and profile-dispatch boundaries, without AWS."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_fargate_session import PROFILE, SECRET, _start_bootstrap, _stop, posix_guest, sync

from kiro_crew import platform_compat, sandbox, sandbox_plan, security
from kiro_crew.coder import retention, session_launch
from kiro_crew.config import live, loader, paths
from kiro_crew.config.sections import SessionEnvironmentsConfig
from kiro_crew.dashboard.handlers import session_environments as handlers
from kiro_crew.fargate_session import bootstrap, fargate_profile, tasks
from kiro_crew.fargate_session.forward import PortForward
from kiro_crew.fargate_session.launch import FargateSessionEnvironments, SessionTask
from kiro_crew.session_environment import SessionEnvironmentUnavailable


@pytest.mark.parametrize("relocated", [False, True])
@posix_guest
def test_bootstrap_key_uses_the_gateway_only_vault(tmp_path, monkeypatch, relocated):
    home = tmp_path / "home"
    home.mkdir()
    crew = tmp_path / "relocated" if relocated else home / ".kiro" / "crew"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("KIROCREW_HOME", str(crew))
    monkeypatch.delenv("KIROCREW_POD", raising=False)
    monkeypatch.setattr(sandbox, "_ssh_supports_accept_new", lambda: True)
    envs = FargateSessionEnvironments(SimpleNamespace(), crew)
    envs._save_secret("task123", SECRET)
    fresh = FargateSessionEnvironments(SimpleNamespace(), crew)
    assert fresh.load_secret("task123") == SECRET
    stored = crew / ".vault" / "secrets.enc"
    assert stored.exists() and SECRET.hex() not in stored.read_text(encoding="utf-8")
    assert not (crew / "fargate" / "secrets").exists()
    for leaf in (stored, crew / ".vault" / ".vault_key"):
        assert security.is_sensitive_path(str(leaf))
    assert not security.is_sensitive_path(str(crew / "notes" / "ordinary.txt"))
    for backend in (sandbox_plan.BACKEND_NAMESPACE, sandbox_plan.BACKEND_SEATBELT):
        for mode in ("standard", "cc", "strict"):
            assert str(stored.parent) in sandbox._spawn_plan(backend, mode).sensitive_dirs
    fresh._forget_secret("task123")
    assert envs.load_secret("task123") is None


@pytest.mark.parametrize("failure", ["eof", "timeout", "cancelled", "malformed"])
@sync
async def test_startup_failure_reaps_the_child_and_uses_port_zero(tmp_path, monkeypatch, failure):
    seen = {}

    async def readline():
        if failure == "timeout":
            raise asyncio.TimeoutError
        if failure == "cancelled":
            raise asyncio.CancelledError
        if failure == "malformed":
            return b"crew-bootstrap: listening on 127.0.0.1:invalid\n"
        return b""

    process = SimpleNamespace(
        returncode=None,
        stderr=SimpleNamespace(readline=readline),
        kill=lambda: seen.update(killed=True),
        wait=AsyncMock(return_value=0),
    )

    async def spawn(*args, **kwargs):
        seen.update(kwargs)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    error = {
        "timeout": asyncio.TimeoutError,
        "cancelled": asyncio.CancelledError,
        "malformed": ValueError,
        "eof": RuntimeError,
    }[failure]
    with pytest.raises(error):
        await _start_bootstrap(tmp_path)
    assert seen["env"]["KIROCREW_BOOTSTRAP_PORT"] == "0"
    runtime = tmp_path / "guest" / "runtime"
    assert seen["env"]["XDG_RUNTIME_DIR"] == str(runtime)
    assert seen["env"]["DBUS_SESSION_BUS_ADDRESS"] == f"unix:path={runtime / 'bus'}"
    assert runtime.is_dir() and not list(runtime.iterdir())
    assert seen["killed"]
    process.wait.assert_awaited_once()


@posix_guest
@sync
async def test_foreign_listener_gets_no_enrollment_key_even_with_an_alive_child(tmp_path):
    process, connect, _ = await _start_bootstrap(tmp_path)
    received = asyncio.get_running_loop().create_future()
    peers = set()

    async def rogue(reader, writer):
        peers.add(writer)
        try:
            writer.write(b"KCB1 attacker-nonce\n")
            await writer.drain()
            data = await reader.readline()
            if not received.done():
                received.set_result(data)
            if data:
                stolen = bytes.fromhex(data.split()[1].decode())
                writer.write(
                    b"ENROLLED " + bootstrap._mac(stolen, b"attacker-nonce", b"enrolled") + b"\n"
                )
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            peers.discard(writer)

    server = await asyncio.start_server(rogue, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    async def hijacked():
        return await asyncio.open_connection("127.0.0.1", port)

    try:
        assert process.returncode is None
        with pytest.raises(SessionEnvironmentUnavailable, match="not the task's SSM"):
            await bootstrap.enroll(hijacked, SECRET, verify_peer=connect.verify_peer)
        assert await asyncio.wait_for(received, 30) == b""
        # Positive control: the same attestor permits the real child.
        await bootstrap.enroll(connect, SECRET, verify_peer=connect.verify_peer)
        assert (await bootstrap.run(connect, SECRET, "echo verified"))[:2] == (0, b"verified\n")
    finally:
        server.close()
        await server.wait_closed()
        for writer in tuple(peers):
            writer.close()
            await writer.wait_closed()
        await _stop(process)


@pytest.mark.parametrize("case", ["descendant", "foreign", "unknown", "recycled", "dead"])
@sync
async def test_peer_proof_pins_the_aws_process_lifetime(monkeypatch, case):
    root, child = 90000000001, 90000000002
    forward = PortForward("ecs:target", bootstrap.BOOTSTRAP_PORT)
    forward._proc = SimpleNamespace(pid=root, poll=lambda: 0 if case == "dead" else None)
    forward._start = "original"
    endpoints = {"sockname": ("127.0.0.1", 123), "peername": ("127.0.0.1", 456)}
    writer = SimpleNamespace(get_extra_info=endpoints.get)
    monkeypatch.setattr(
        platform_compat, "get_tcp_peer_pid", lambda *_: None if case == "unknown" else child
    )
    monkeypatch.setattr(
        platform_compat, "get_ppid", lambda pid: root if case != "foreign" and pid == child else 0
    )
    monkeypatch.setattr(
        platform_compat,
        "process_start_time",
        lambda pid: "different" if case == "recycled" and pid == root else "original",
    )
    if case == "descendant":
        await forward.verify_peer(writer)
    else:
        with pytest.raises(SessionEnvironmentUnavailable, match="refusing bootstrap enrollment"):
            await forward.verify_peer(writer)


@sync
async def test_failed_stop_is_durable_and_the_next_sweep_retries(tmp_path, monkeypatch):
    envs = FargateSessionEnvironments(SimpleNamespace(fargate_profiles=[PROFILE]), tmp_path)
    row = SessionTask("dashboard:stop", PROFILE["id"], "arn-task", "task123", "dev", "r1")
    row = envs._record_task(row, PROFILE)
    envs._save_secret(row.task_id, SECRET)
    monkeypatch.setattr(
        tasks,
        "describe",
        AsyncMock(
            return_value={
                "taskArn": row.arn,
                "tags": [{"key": tasks.SESSION_TAG, "value": tasks.session_tag(row.session_key)}],
            }
        ),
    )
    stopping = AsyncMock(side_effect=SessionEnvironmentUnavailable("denied"))
    monkeypatch.setattr(tasks, "stop_task", stopping)
    monkeypatch.setattr(envs, "_drop_forward", AsyncMock())
    with pytest.raises(SessionEnvironmentUnavailable, match="denied"):
        await envs.archive_session(row.session_key)
    stored = envs.registry.get(row.session_key)
    assert stored and stored.stop_reason == "the session was archived"
    assert envs.load_secret(row.task_id) == SECRET
    assert await envs.shell_for(row.session_key, row.profile_id) is None
    assert await envs._reusable(PROFILE, stored) is None
    # Resume cannot replace a still-billed task when stopping it keeps failing.
    launch = AsyncMock()
    monkeypatch.setattr(envs, "_launch", launch)
    with pytest.raises(SessionEnvironmentUnavailable, match="denied"):
        await envs.prepare(row.session_key, row.profile_id)
    launch.assert_not_awaited()
    assert envs.registry.get(row.session_key).arn == row.arn
    assert await envs.retry_stops() == 0
    monkeypatch.setattr(retention, "_fargate_environments", AsyncMock(return_value=envs))
    monkeypatch.setattr(retention, "_environments", AsyncMock(return_value=None))
    stopping.side_effect = None
    assert await retention.sweep_once() == 1
    assert envs.registry.get(row.session_key) is None
    assert envs.load_secret(row.task_id) is None
    assert stopping.await_count == 4
    assert await retention.sweep_once() == 0


def test_stop_intent_cannot_overwrite_a_newer_task(tmp_path):
    envs = FargateSessionEnvironments(SimpleNamespace(), tmp_path)
    row = SessionTask("session", "p", "new-arn", "new123", "dev")
    envs.registry.put(row)
    assert not envs.registry.mark_stopping(
        SessionTask("session", "p", "old-arn", "old123", "dev"),
        SessionTask("session", "p", "old-arn", "old123", "dev", stop_reason="stop old"),
    )
    assert envs.registry.get("session") == row


@pytest.mark.parametrize("coder_url", ["", "https://coder.example.com"])
@sync
async def test_ambiguous_id_is_neither_offered_nor_launched(tmp_path, monkeypatch, coder_url):
    section = SessionEnvironmentsConfig.from_raw(
        {
            "coder_url": coder_url,
            "profiles": [{"id": PROFILE["id"], "name": "Coder", "template": "t", "workdir": "/w"}],
            "fargate_profiles": [PROFILE],
        }
    )
    config = SimpleNamespace(session_environments=section)
    monkeypatch.setattr(live, "snapshot", lambda: config)
    monkeypatch.setattr(loader.KiroCrewConfig, "load", lambda: config)
    monkeypatch.setattr(paths, "data_home", lambda: tmp_path)
    assert handlers._profiles() == []
    with pytest.raises(SessionEnvironmentUnavailable, match="ambiguous"):
        fargate_profile(section, PROFILE["id"])
    choices = session_launch.SessionEnvironmentChoices(tmp_path / "coder" / "session-choices.json")
    choices._write({"session": {"provider": "coder", "configuration": PROFILE["id"]}})
    with pytest.raises(SessionEnvironmentUnavailable, match="ambiguous"):
        await session_launch.prepare_for_session("session")


def test_choice_rows_record_the_resolved_provider_and_inherit_it(tmp_path, monkeypatch):
    section = SessionEnvironmentsConfig.from_raw({"fargate_profiles": [PROFILE]})
    config = SimpleNamespace(session_environments=section)
    monkeypatch.setattr(live, "snapshot", lambda: config)
    path = tmp_path / "choices.json"
    choices = session_launch.SessionEnvironmentChoices(path)
    choices.set("parent", PROFILE["id"])
    assert choices.inherit("child", "parent") == PROFILE["id"]
    assert choices.resolve("child") == (PROFILE["id"], "parent")
    rows = json.loads(path.read_text(encoding="utf-8"))["sessions"]
    assert {v["provider"] for v in rows.values()} == {"fargate"}
    # Provider metadata is descriptive: an older row is still resolved by id.
    rows["parent"]["provider"] = "coder"
    choices._write(rows)
    assert choices.resolve("parent") == (PROFILE["id"], "parent")
    assert fargate_profile(section, choices.get("parent")) == PROFILE


@sync
async def test_shutdown_preserves_fargate_rows_and_secrets(tmp_path, monkeypatch):
    envs = FargateSessionEnvironments(SimpleNamespace(fargate_profiles=[PROFILE]), tmp_path)
    row = envs._record_task(
        SessionTask("dashboard:live", PROFILE["id"], "arn-live", "live123", "dev", "r1"),
        PROFILE,
    )
    envs._save_secret(row.task_id, SECRET)
    describe = AsyncMock(side_effect=AssertionError("shutdown must not contact ECS"))
    stop = AsyncMock(side_effect=AssertionError("shutdown must not destroy task files"))
    monkeypatch.setattr(tasks, "describe", describe)
    monkeypatch.setattr(tasks, "stop_task", stop)
    monkeypatch.setattr(retention, "_fargate_environments", AsyncMock(return_value=envs))
    monkeypatch.setattr(retention, "_environments", AsyncMock(return_value=None))
    assert await retention.stop_for_shutdown() == {"coder": 0, "fargate": 0, "skipped": 1}
    assert envs.registry.get(row.session_key) == row
    assert envs.load_secret(row.task_id) == SECRET
    describe.assert_not_awaited()
    stop.assert_not_awaited()


@pytest.mark.parametrize("failure", ["describe", "stop"])
@sync
async def test_sweep_keeps_failed_task_and_continues_with_later_tasks(
    tmp_path, monkeypatch, failure
):
    envs = FargateSessionEnvironments(SimpleNamespace(fargate_profiles=[PROFILE]), tmp_path)
    rows = [
        envs._record_task(
            SessionTask(f"dashboard:{key}", PROFILE["id"], f"arn-{key}", key, "dev", "r1"),
            PROFILE,
        )
        for key in ("a123", "b123")
    ]
    for row in rows:
        envs._save_secret(row.task_id, SECRET)
        assert envs._record_stop(row, "archived")
    fail = True

    async def describe(profile, task):
        if fail and failure == "describe" and task.arn == rows[0].arn:
            raise SessionEnvironmentUnavailable("describe denied")
        row = next(row for row in rows if row.arn == task.arn)
        return {
            "taskArn": row.arn,
            "tags": [{"key": tasks.SESSION_TAG, "value": tasks.session_tag(row.session_key)}],
        }

    async def stop(profile, task, reason):
        if fail and failure == "stop" and task.arn == rows[0].arn:
            raise SessionEnvironmentUnavailable("stop denied")

    stopping = AsyncMock(side_effect=stop)
    monkeypatch.setattr(tasks, "describe", AsyncMock(side_effect=describe))
    monkeypatch.setattr(tasks, "stop_task", stopping)
    monkeypatch.setattr(envs, "_drop_forward", AsyncMock())
    monkeypatch.setattr(retention, "_fargate_environments", AsyncMock(return_value=envs))
    monkeypatch.setattr(retention, "_environments", AsyncMock(return_value=None))
    assert await retention.sweep_once() == 1
    assert envs.registry.get(rows[0].session_key).stop_reason == "archived"
    assert envs.load_secret(rows[0].task_id) == SECRET
    assert envs.registry.get(rows[1].session_key) is None
    assert envs.load_secret(rows[1].task_id) is None
    assert stopping.await_args_list[-1].args[1] == rows[1].task
    fail = False
    assert await retention.sweep_once() == 1
    assert envs.registry.all() == []
    assert envs.load_secret(rows[0].task_id) is None


@pytest.mark.parametrize("operation", ["run", "dial", "link"])
@sync
async def test_forward_restart_failure_reaches_the_worker_error_contract(
    tmp_path, monkeypatch, operation
):
    from kiro_crew.coder.remote_worker import RemoteWorkerError
    from kiro_crew.fargate_session.shell import FargateShell, FargateWorkerLaunch
    from kiro_crew.remote_session.link import LOST, RemoteLink

    now = [1000.0]
    attempts = []

    async def unavailable():
        attempts.append(None)
        if len(attempts) == 2:
            now[0] += 301.0
        raise SessionEnvironmentUnavailable("could not open the SSM port forward")

    forward = PortForward("ecs:task", bootstrap.BOOTSTRAP_PORT)
    monkeypatch.setattr(forward, "ensure", unavailable)
    shell = FargateShell(forward.connect, SECRET)
    launch = FargateWorkerLaunch("fg-retry", shell, tmp_path / "secrets")
    if operation == "run":
        with pytest.raises(RemoteWorkerError, match="could not open the SSM"):
            await shell.run("true")
    elif operation == "dial":
        with pytest.raises(ConnectionError, match="could not open the SSM"):
            await launch.dial(12345)()
    else:
        link = RemoteLink(
            "fg-retry",
            SECRET,
            launch.dial(12345),
            clock=lambda: now[0],
            backoff_initial=0,
            backoff_max=0,
        )
        try:
            link.start()
            await asyncio.wait_for(link._task, 30)
            assert link.state == LOST
            assert len(attempts) == 2
            assert "could not open the SSM" in link.error
        finally:
            await link.aclose()
