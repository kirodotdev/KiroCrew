"""Fargate lifecycle authorization and off-loop discovery, with no AWS calls."""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_fargate_session import sync

from kiro_crew import platform_compat
from kiro_crew.cloud import ssm
from kiro_crew.fargate_session import forward
from kiro_crew.session_environment import SessionEnvironmentUnavailable


@pytest.mark.parametrize("plugin_available", [False, True])
@sync
async def test_forward_discovery_runs_off_loop(monkeypatch, plugin_available):
    loop_thread = threading.get_ident()
    calls = []
    process = SimpleNamespace(pid=90000000001, poll=lambda: None)

    def probe():
        assert threading.get_ident() != loop_thread
        calls.append("probe")
        return plugin_available

    def argv(target, remote_port, port, profile, region):
        assert threading.get_ident() != loop_thread
        assert (target, remote_port, port, profile, region) == (
            "ecs:dev_task_runtime",
            47821,
            54321,
            "sandbox",
            "us-east-1",
        )
        calls.append("argv")
        return ["aws", "ssm", "start-session"]

    spawn = Mock(return_value=process)
    kill = Mock()
    monkeypatch.setattr(ssm, "session_manager_plugin_installed", probe)
    monkeypatch.setattr(ssm, "build_port_forward_argv", argv)
    monkeypatch.setattr(ssm, "wait_for_local_port", lambda *a, **k: True)
    monkeypatch.setattr(ssm, "kill_port_forward", kill)
    monkeypatch.setattr(platform_compat, "process_start_time", lambda pid: "pinned")
    monkeypatch.setattr(forward, "_free_port", lambda: 54321)
    tunnel = forward.PortForward(
        "ecs:dev_task_runtime", 47821, profile="sandbox", region="us-east-1", spawn=spawn
    )
    try:
        if plugin_available:
            assert await tunnel.ensure() == 54321
            assert tunnel._start == "pinned"
            assert await tunnel.ensure() == 54321
            assert calls == ["probe", "argv"]
            spawn.assert_called_once_with(["aws", "ssm", "start-session"])
        else:
            with pytest.raises(SessionEnvironmentUnavailable, match="plugin is not installed"):
                await tunnel.ensure()
            spawn.assert_not_called()
            assert calls == ["probe"]
    finally:
        await tunnel.stop()
    if plugin_available:
        kill.assert_called_once_with(process)
    else:
        kill.assert_not_called()


@pytest.fixture
def owned_task(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock

    from test_fargate_session import PROFILE, SECRET

    from kiro_crew.fargate_session import launch, tasks

    config = SimpleNamespace(fargate_profiles=[dict(PROFILE)])
    envs = launch.FargateSessionEnvironments(config, tmp_path)
    row = envs._record_task(
        launch.SessionTask(
            "dashboard:owned",
            PROFILE["id"],
            "arn:aws:ecs:us-east-1:123456789012:task/dev/task123",
            "task123",
            "dev",
            "runtime1",
        ),
        PROFILE,
    )
    envs._save_secret(row.task_id, SECRET)
    info = {
        "taskArn": row.arn,
        "lastStatus": "RUNNING",
        "tags": [{"key": tasks.SESSION_TAG, "value": tasks.session_tag(row.session_key)}],
    }
    describe = AsyncMock(return_value=info)
    stop = AsyncMock()
    launch_task = AsyncMock(side_effect=AssertionError("unexpected launch"))
    connect = AsyncMock()
    forward_task = Mock(return_value=SimpleNamespace(connect=connect))
    monkeypatch.setattr(tasks, "describe", describe)
    monkeypatch.setattr(tasks, "stop_task", stop)
    monkeypatch.setattr(envs, "_drop_forward", AsyncMock())
    monkeypatch.setattr(envs, "_launch", launch_task)
    monkeypatch.setattr(envs, "forward", forward_task)
    return SimpleNamespace(
        envs=envs,
        row=row,
        info=info,
        describe=describe,
        stop=stop,
        launch=launch_task,
        forward=forward_task,
        config=config,
    )


@pytest.mark.parametrize("operation", ["archive", "prepare", "retry"])
@pytest.mark.parametrize(
    "field",
    [
        "arn",
        "task_id",
        "cluster",
        "runtime_id",
        "session_key",
        "profile_id",
        "aws_profile",
        "binding_mac",
        "stop_reason",
    ],
)
@sync
async def test_tampered_binding_never_authorizes_stop(owned_task, operation, field):
    import json

    from test_fargate_session import SECRET

    envs, row = owned_task.envs, owned_task.row
    stored = json.loads(envs.registry.path.read_text(encoding="utf-8"))
    stored["tasks"][row.session_key][field] = "foreign"
    if operation == "retry":
        stored["tasks"][row.session_key]["stop_reason"] = "archived"
    envs.registry.path.write_text(json.dumps(stored), encoding="utf-8")
    if operation == "retry" and field != "session_key":
        assert await envs.retry_stops() == 0
    else:
        with pytest.raises(SessionEnvironmentUnavailable, match="not authenticated|unreadable"):
            if operation == "prepare":
                await envs.prepare(row.session_key, row.profile_id)
            elif operation == "archive":
                await envs.archive_session(row.session_key)
            else:
                await envs.retry_stops()
    owned_task.stop.assert_not_awaited()
    owned_task.describe.assert_not_awaited()
    owned_task.launch.assert_not_awaited()
    assert envs.load_secret(row.task_id) == SECRET


@pytest.mark.parametrize("forgery", ["unsigned", "guest_key", "other_gateway"])
@sync
async def test_binding_requires_the_original_gateway_only_key(owned_task, tmp_path, forgery):
    import hashlib
    import hmac
    import json
    from dataclasses import replace

    from test_fargate_session import SECRET

    from kiro_crew.fargate_session.launch import FargateSessionEnvironments

    envs, row = owned_task.envs, owned_task.row
    if forgery == "guest_key":
        payload = json.dumps(
            [
                row.session_key,
                row.profile_id,
                row.arn,
                row.task_id,
                row.cluster,
                row.runtime_id,
                row.aws_profile,
            ],
            separators=(",", ":"),
        ).encode()
        row = replace(row, binding_mac=hmac.new(SECRET, payload, hashlib.sha256).hexdigest())
    elif forgery == "unsigned":
        row = replace(row, binding_mac="")
    else:
        other = FargateSessionEnvironments(owned_task.config, tmp_path / "other")
        row = replace(row, binding_mac=other._binding_mac(row))
    envs.registry.put(row)
    with pytest.raises(SessionEnvironmentUnavailable, match="not authenticated"):
        await envs.archive_session(row.session_key)
    owned_task.stop.assert_not_awaited()
    assert envs.registry.get(row.session_key) == row


@sync
async def test_signed_binding_survives_gateway_restart_and_allows_its_own_stop(owned_task):
    from kiro_crew.fargate_session.launch import FargateSessionEnvironments

    row = owned_task.row
    envs = FargateSessionEnvironments(owned_task.config, owned_task.envs.data_home)
    await asyncio.to_thread(envs._verify_binding, envs.registry.get(row.session_key))
    assert await envs.archive_session(row.session_key)
    owned_task.stop.assert_awaited_once_with(
        {"aws_profile": "sandbox", "region": "us-east-1"}, row.task, "the session was archived"
    )
    assert envs.registry.get(row.session_key) is None
    assert envs.load_secret(row.task_id) is None


@pytest.mark.parametrize("operation", ["archive", "prepare", "retry"])
@pytest.mark.parametrize("identity", ["foreign_arn", "foreign_session", "whole_crew"])
@sync
async def test_foreign_task_is_forgotten_without_stop_and_prepare_launches_fresh(
    owned_task, operation, identity, caplog
):
    from dataclasses import replace

    from test_fargate_session import SECRET

    envs, row, info = owned_task.envs, owned_task.row, owned_task.info
    if identity == "foreign_arn":
        info["taskArn"] = "arn:aws:ecs:us-east-1:123456789012:task/dev/foreign"
    elif identity == "whole_crew":
        info["tags"] = [{"key": "kirocrew:managed", "value": "true"}]
    else:
        info["tags"][0]["value"] = "another-session"
    if operation == "retry":
        assert envs._record_stop(row, "archived")
        assert await envs.retry_stops() == 1
        assert envs.registry.get(row.session_key) is None
    elif operation == "prepare":

        async def fresh(key, profile):
            new = replace(row, arn=row.arn.replace("task123", "task456"), task_id="task456")
            return await asyncio.to_thread(envs._record_task, new, profile), SECRET

        owned_task.launch.side_effect = fresh
        prepared = await envs.prepare(row.session_key, row.profile_id)
        assert prepared.created
        owned_task.launch.assert_awaited_once()
        assert envs.registry.get(row.session_key).task_id == "task456"
    else:
        assert await envs.archive_session(row.session_key)
        assert envs.registry.get(row.session_key) is None
    owned_task.stop.assert_not_awaited()
    if operation != "prepare":
        owned_task.launch.assert_not_awaited()
    assert envs.load_secret(row.task_id) is None
    envs._drop_forward.assert_awaited_once_with(row.task_id)
    assert any(
        rec.name == "kiro_crew.fargate_session.launch"
        and row.arn in rec.message
        and "without stopping it" in rec.message
        for rec in caplog.records
    )


@sync
async def test_browsing_refuses_a_tampered_task_binding(owned_task):
    from dataclasses import replace

    envs, row = owned_task.envs, owned_task.row
    envs.registry.put(replace(row, cluster="foreign"))
    with pytest.raises(SessionEnvironmentUnavailable, match="not authenticated"):
        await envs.shell_for(row.session_key, row.profile_id)
    owned_task.forward.assert_not_called()
    owned_task.describe.assert_not_awaited()


@pytest.mark.parametrize("operation", ["archive", "prepare", "retry"])
@sync
async def test_purged_task_is_forgotten_without_stopping_and_prepare_launches_fresh(
    owned_task, operation
):
    from dataclasses import replace

    from test_fargate_session import SECRET

    envs, row = owned_task.envs, owned_task.row
    owned_task.describe.return_value = None
    owned_task.stop.side_effect = SessionEnvironmentUnavailable("The referenced task was not found")
    if operation == "prepare":

        async def fresh(key, profile):
            new = replace(row, arn=row.arn.replace("task123", "task456"), task_id="task456")
            return await asyncio.to_thread(envs._record_task, new, profile), SECRET

        owned_task.launch.side_effect = fresh
        prepared = await envs.prepare(row.session_key, row.profile_id)
        assert prepared.created
        owned_task.launch.assert_awaited_once()
        assert envs.registry.get(row.session_key).task_id == "task456"
    elif operation == "retry":
        assert envs._record_stop(row, "archived")
        assert await envs.retry_stops() == 1
        assert envs.registry.get(row.session_key) is None
    else:
        assert await envs.archive_session(row.session_key)
        assert envs.registry.get(row.session_key) is None
    owned_task.stop.assert_not_awaited()
    assert envs.load_secret(row.task_id) is None


@pytest.mark.parametrize("still_present", [False, True])
@sync
async def test_stop_failure_only_clears_the_row_after_confirmed_absence(owned_task, still_present):
    envs, row = owned_task.envs, owned_task.row
    owned_task.stop.side_effect = SessionEnvironmentUnavailable("AWS refused ecs:StopTask")
    owned_task.describe.side_effect = [owned_task.info, owned_task.info if still_present else None]
    if still_present:
        with pytest.raises(SessionEnvironmentUnavailable, match="AWS refused ecs:StopTask"):
            await envs.archive_session(row.session_key)
        assert envs.registry.get(row.session_key).stop_reason == "the session was archived"
        assert envs.load_secret(row.task_id) is not None
    else:
        assert await envs.archive_session(row.session_key)
        assert envs.registry.get(row.session_key) is None
        assert envs.load_secret(row.task_id) is None
    owned_task.stop.assert_awaited_once()
    assert owned_task.describe.await_count == 2


@pytest.mark.parametrize("operation", ["archive", "prepare", "browse"])
@sync
async def test_recorded_tasks_keep_the_launch_profile_and_region(owned_task, operation):
    envs, row = owned_task.envs, owned_task.row
    profile = {
        **owned_task.config.fargate_profiles[0],
        "aws_profile": "other",
        "region": "eu-west-2",
    }
    owned_task.config.fargate_profiles = [profile]
    if operation == "archive":
        assert await envs.archive_session(row.session_key)
        used = owned_task.stop.call_args.args[0]
    elif operation == "prepare":
        prepared = await envs.prepare(row.session_key, row.profile_id)
        assert not prepared.created
        used = owned_task.describe.call_args.args[0]
        assert owned_task.forward.call_args.args[0]["aws_profile"] == "sandbox"
    else:
        assert await envs.shell_for(row.session_key, row.profile_id) is not None
        used = owned_task.forward.call_args.args[0]
    assert (used["aws_profile"], used["region"]) == ("sandbox", "us-east-1")


@pytest.mark.parametrize("reason", ["MISSING", "ACCESS_DENIED", "UNKNOWN"])
@sync
async def test_describe_failures_only_mean_absent_for_the_named_missing_task(monkeypatch, reason):
    from kiro_crew.cloud import aws
    from kiro_crew.fargate_session import tasks

    task = tasks.FargateTask(
        "arn:aws:ecs:us-east-1:123456789012:task/dev/task123", "task123", "dev"
    )
    checked = Mock(return_value={"tasks": [], "failures": [{"arn": task.arn, "reason": reason}]})
    monkeypatch.setattr(aws, "checked_json", checked)
    if reason == "MISSING":
        assert await tasks.describe({}, task) is None
    else:
        with pytest.raises(SessionEnvironmentUnavailable, match="could not describe"):
            await tasks.describe({}, task)
    checked.assert_called_once()
    assert checked.call_args.kwargs["action"] == "ecs:DescribeTasks"


@sync
async def test_a_failed_describe_never_forgets_or_stops_the_task(owned_task):
    owned_task.describe.side_effect = SessionEnvironmentUnavailable("ECS could not describe")
    with pytest.raises(SessionEnvironmentUnavailable, match="could not describe"):
        await owned_task.envs.archive_session(owned_task.row.session_key)
    owned_task.stop.assert_not_awaited()
    assert owned_task.envs.registry.get(owned_task.row.session_key).stop_reason
    assert owned_task.envs.load_secret(owned_task.row.task_id) is not None


@pytest.mark.parametrize("mutation", ["change_reason", "clear_reason", "guest_key", "other_task"])
@sync
async def test_stop_intent_is_bound_to_the_gateway_task_and_exact_reason(owned_task, mutation):
    import hashlib
    import hmac
    import json
    from dataclasses import replace

    from test_fargate_session import SECRET

    envs, row = owned_task.envs, owned_task.row
    assert envs._record_stop(row, "archived by owner")
    stopped = envs.registry.get(row.session_key)
    assert stopped.binding_mac != row.binding_mac
    envs._verify_binding(stopped)
    if mutation == "change_reason":
        forged = replace(stopped, stop_reason="injected reason")
    elif mutation == "clear_reason":
        forged = replace(stopped, stop_reason="")
    elif mutation == "other_task":
        forged = replace(stopped, task_id="other123")
    else:
        payload = json.dumps(
            [
                stopped.session_key,
                stopped.profile_id,
                stopped.arn,
                stopped.task_id,
                stopped.cluster,
                stopped.runtime_id,
                stopped.aws_profile,
                stopped.stop_reason,
            ],
            separators=(",", ":"),
        ).encode()
        forged = replace(stopped, binding_mac=hmac.new(SECRET, payload, hashlib.sha256).hexdigest())
    envs.registry.put(forged)
    assert await envs.retry_stops() == 0
    with pytest.raises(SessionEnvironmentUnavailable, match="not authenticated"):
        envs._record_stop(forged, "archive")
    assert envs.registry.get(row.session_key) == forged
    owned_task.stop.assert_not_awaited()
    owned_task.describe.assert_not_awaited()
    assert envs.load_secret(row.task_id) == SECRET


@pytest.mark.parametrize("field", ["stop_reason", "runtime_id", "binding_mac"])
@sync
async def test_stop_transition_never_signs_a_registry_edit_after_verification(
    owned_task, monkeypatch, field
):
    from dataclasses import replace

    envs, row = owned_task.envs, owned_task.row
    forged = replace(row, **{field: "injected"})
    mark_stopping = envs.registry.mark_stopping

    def edit_then_mark(snapshot, stopping):
        envs.registry.put(forged)
        return mark_stopping(snapshot, stopping)

    monkeypatch.setattr(envs.registry, "mark_stopping", edit_then_mark)
    await envs.archive_session(row.session_key)
    assert envs.registry.get(row.session_key) == forged
    owned_task.describe.assert_not_awaited()
    owned_task.stop.assert_not_awaited()


@sync
async def test_signed_stop_survives_restart_and_retries_with_the_authorized_reason(owned_task):
    from kiro_crew.fargate_session.launch import FargateSessionEnvironments

    envs, row = owned_task.envs, owned_task.row
    owned_task.stop.side_effect = SessionEnvironmentUnavailable("temporarily unavailable")
    with pytest.raises(SessionEnvironmentUnavailable):
        await envs.archive_session(row.session_key, "archived by owner")
    pending = envs.registry.get(row.session_key)
    envs._verify_binding(pending)
    assert pending.stop_reason == "archived by owner"
    assert pending.binding_mac != row.binding_mac
    restarted = FargateSessionEnvironments(owned_task.config, envs.data_home)
    owned_task.stop.reset_mock(side_effect=True)
    assert await restarted.retry_stops() == 1
    owned_task.stop.assert_awaited_once_with(
        {"aws_profile": "sandbox", "region": "us-east-1"}, row.task, "archived by owner"
    )
    assert restarted.registry.get(row.session_key) is None
    assert restarted.load_secret(row.task_id) is None
