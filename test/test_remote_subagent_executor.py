"""Remote sub-agent placement: spawn, monitor, delivery, restart and dismissal."""

from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from remote_subagent_helpers import (
    _auto_manager,
    _Instances,
    _Manager,
    _manager_with_remote,
    _settle_monitor,
    _spawn_kwargs,
    _spawn_placed,
)

import kiro_crew
from kiro_crew import mcp_shared
from kiro_crew.dashboard import remote_subagents
from kiro_crew.dashboard.remote_subagents import RemoteSubagentError, RemoteSubagentService
from kiro_crew.mcp_tools import spawn as spawn_tools
from kiro_crew.subagent import SubagentInfo, SubagentManager
from kiro_crew.testing.wait import async_wait_until, await_until, finished
from kiro_crew.validation import SPAWN_RUN_SCHEMA, validate_tool_args

# The local-path cases drive SubagentManager.spawn; pin the host-memory reading
# so a memory-pressured runner does not queue them.
pytestmark = pytest.mark.usefixtures("healthy_host_memory")


@pytest.mark.asyncio
async def test_remote_spawn_picks_least_loaded_peer_and_relays_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents._POLL_SECONDS", 0.0)
    instances = _Instances(
        [
            (200, {"id": "peer01"}),
            (200, {"done": False, "turns": 2, "last_tool": "Reading src"}),
            (
                200,
                {
                    "done": True,
                    "result": "remote-result",
                    "error": "",
                    "outcome": "completed",
                    "partial": False,
                },
            ),
        ]
    )
    manager = _Manager()
    manager.external_agents.append(
        SubagentInfo(
            id="occupied",
            task="existing",
            executor="remote",
            instance_id="crew-a",
            remote_id="peer00",
        )
    )
    state = SimpleNamespace(instances_manager=instances)
    service = RemoteSubagentService(state, manager)  # type: ignore[arg-type]

    info = await _spawn_placed(
        service,
        task="review exact refactor SHA",
        parent_session="dashboard:chat-1",
        agent="kirocrew",
        max_turns=50,
        cwd="/home/kirocrew/workplace/repo",
        model="",
        reasoning_effort="",
        include_memory=False,
        include_lessons=False,
        include_project=True,
        memory_mode="temporary",
        batch_id="batch01",
        batch_total=1,
    )
    await _settle_monitor(service, info.id)

    assert info.instance_id == "crew-b"
    assert info.remote_id == "peer01"
    assert info.done is True
    assert info.result == "rem"
    assert info.result_truncated is True
    assert manager.reported == [info]
    assert manager.events[0][0] == "subagent_spawn"
    post = instances.calls[0]
    assert post[:3] == ("crew-b", "POST", "api/spawn")
    # Named by the hub, in the peer's own minted shape, before it is sent.
    assert re.fullmatch(r"[0-9a-f]{16}", post[3].pop("run_id"))
    assert post[3] == {
        "task": "review exact refactor SHA",
        "parent_session": "",
        "silent": True,
        "include_memory": False,
        "include_lessons": False,
        "include_project": True,
        "memory_mode": "temporary",
        "agent": "kirocrew",
        "max_turns": 50,
        "cwd": "/home/kirocrew/workplace/repo",
        # A dashboard parent in Normal mode: the crew must not auto-approve.
        "approval_floor": "interactive",
    }
    await service.close()


@pytest.mark.asyncio
async def test_a_credential_quoted_in_the_task_never_reaches_the_peer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents._POLL_SECONDS", 0.0)
    instances = _Instances(
        [
            (200, {"id": "peer01"}),
            (200, {"done": True, "result": "ok", "error": "", "outcome": "completed"}),
        ]
    )
    manager = _Manager()
    service = RemoteSubagentService(  # type: ignore[arg-type]
        SimpleNamespace(instances_manager=instances), manager
    )
    secret = "AKIAIOSFODNN7EXAMPLE"

    info = await _spawn_placed(
        service,
        task=f"deploy with key {secret} please",
        parent_session="",
        agent="",
        max_turns=0,
        cwd="/home/kirocrew/workplace/repo",
        model="",
        reasoning_effort="",
        include_memory=False,
        include_lessons=False,
        include_project=False,
        memory_mode="persistent",
        batch_id="",
        batch_total=0,
    )
    await _settle_monitor(service, info.id)

    sent = instances.calls[0]
    assert sent[1:3] == ("POST", "api/spawn")
    assert secret not in json.dumps(sent[3])
    assert "deploy with key" in str(sent[3]["task"])
    assert secret not in info.task
    await service.close()


@pytest.mark.asyncio
async def test_remote_spawn_refuses_disconnected_explicit_peer() -> None:
    manager = _Manager()
    service = RemoteSubagentService(
        cast(
            Any,
            SimpleNamespace(instances_manager=_Instances([], connected=("crew-a",))),
        ),
        cast(Any, manager),
    )

    with pytest.raises(RemoteSubagentError) as raised:
        await _spawn_placed(
            service,
            task="x",
            parent_session="dashboard:chat-1",
            agent="",
            max_turns=0,
            cwd="",
            model="",
            reasoning_effort="",
            include_memory=False,
            include_lessons=False,
            include_project=False,
            memory_mode="temporary",
            batch_id="",
            batch_total=0,
            instance_id="crew-z",
        )

    assert raised.value.code == "remote_instance_not_connected"
    assert manager.external_agents == []


@pytest.mark.asyncio
async def test_remote_cancel_targets_peer_run() -> None:
    instances = _Instances([(200, {"ok": True, "cancelled": True})])
    service = RemoteSubagentService(
        SimpleNamespace(instances_manager=instances), _Manager()  # type: ignore[arg-type]
    )
    info = SubagentInfo(
        id="local01",
        task="x",
        executor="remote",
        instance_id="crew-a",
        remote_id="peer01",
    )

    assert await service.cancel(info) is True
    assert instances.calls == [("crew-a", "DELETE", "api/spawn/peer01", None)]


@pytest.mark.asyncio
async def test_an_unconfirmed_cancel_keeps_stopping_the_peer_run(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Opus: a failed DELETE raised and was swallowed by teardown and Stop-all,
    so nothing re-sent the stop. The stop is committed and handed to the
    monitor, which re-sends it until the peer confirms."""
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents.data_home", lambda: tmp_path)
    instances = _Instances([(503, {"error": "busy"})])
    service = RemoteSubagentService(
        SimpleNamespace(instances_manager=instances), _Manager()  # type: ignore[arg-type]
    )
    info = SubagentInfo(
        id="a1b2c3d4",
        task="x",
        executor="remote",
        instance_id="crew-a",
        remote_id="peer01",
    )

    with pytest.raises(RemoteSubagentError):
        await service.cancel(info)

    assert info.id in service._unconfirmed_stops
    [(restored, _delivered)] = RemoteSubagentService._load_records()
    assert restored.user_stopped is True and restored.done is False


@pytest.mark.asyncio
async def test_an_accepted_remote_spawn_and_its_cancel_are_audited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A remote child leaves this gateway's approval ceiling, so its placement
    # and its stop must reach the SEL trail just like a local run's.
    events: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "kiro_crew.sel.sel",
        lambda: SimpleNamespace(log_tool_invocation=lambda **kw: events.append(kw)),
    )
    instances = _Instances([(200, {"id": "peer01"}), (200, {"ok": True, "cancelled": True})])
    service = RemoteSubagentService(
        cast(Any, SimpleNamespace(instances_manager=instances)), cast(Any, _auto_manager())
    )
    info = await _spawn_placed(
        service,
        task="x",
        parent_session="dashboard:chat-1",
        agent="reviewer",
        max_turns=0,
        cwd="/home/kirocrew/workplace/repo",
        model="",
        reasoning_effort="",
        include_memory=False,
        include_lessons=False,
        include_project=False,
        memory_mode="persistent",
        batch_id="",
        batch_total=0,
    )
    await service.close()  # the monitor must not consume the cancel reply
    assert await service.cancel(info) is True

    assert [e["outcome"] for e in events] == ["spawned", "cancelled"]
    for event in events:
        assert event["tool_name"] == "spawn_run"
        assert event["session_key"] == "dashboard:chat-1"
        assert event["metadata"] == {
            "subagent_id": info.id,
            "executor": "remote",
            "instance_id": info.instance_id,
            "remote_id": "peer01",
            "agent": "reviewer",
        }


def test_manager_external_inventory_does_not_consume_local_running_capacity() -> None:
    manager = SubagentManager.__new__(SubagentManager)
    manager._agents = {
        "local01": SubagentInfo(id="local01", task="local"),
    }
    manager._external_agents = {}
    manager._batch_submitted = {}
    manager._batch_progress_ts = {}
    remote = SubagentInfo(
        id="remote01",
        task="remote",
        parent_session_key="dashboard:chat-1",
        executor="remote",
        instance_id="crew-a",
        remote_id="peer01",
        batch_id="batch01",
        batch_total=1,
    )

    manager.register_external(remote)

    assert manager.get("remote01") is remote
    assert {info.id for info in manager.all_agents} == {"local01", "remote01"}
    assert [info.id for info in manager.running] == ["local01"]
    assert manager._batch_submitted["batch01"] == [1, 1]


def test_a_remote_wave_with_a_lost_submission_reaches_the_stuck_wave_sweep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reviewer: ``register_external`` stamped wave progress with the wall clock,
    which the sweep subtracts from a monotonic ``now``, so a remote wave whose
    other submission was lost read as inside its grace window for ever."""
    from kiro_crew import subagent as subagent_mod
    from kiro_crew.subagent_manager.waves import WaveDigestCoordinator

    manager = SubagentManager.__new__(SubagentManager)
    manager._agents = {}
    manager._external_agents = {}
    manager._batch_submitted = {}
    manager._batch_progress_ts = {}
    remote = SubagentInfo(
        id="remote01",
        task="remote",
        parent_session_key="dashboard:chat-1",
        executor="remote",
        instance_id="crew-a",
        remote_id="peer01",
        batch_id="batch01",
        batch_total=2,
    )
    manager.register_external(remote)
    remote.done = True
    waves = WaveDigestCoordinator(manager)
    monkeypatch.setattr(WaveDigestCoordinator, "_unstarted_entries", lambda self: [])

    registered = time.monotonic()
    assert waves._stuck_wave_candidates(registered) == []
    later = registered + subagent_mod._WAVE_STUCK_SECS + 1
    assert waves._stuck_wave_candidates(later) == [("batch01", "dashboard:chat-1")]


def test_spawn_schema_accepts_remote_pool_selection() -> None:
    cleaned = validate_tool_args(
        {"task": "review", "executor": "remote", "instance_id": "crew-a"},
        SPAWN_RUN_SCHEMA,
    )
    assert cleaned["executor"] == "remote"
    assert cleaned["instance_id"] == "crew-a"


def test_spawn_tool_forwards_remote_placement_and_reports_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(spawn_tools.mcp_core, "_resolve_session_key", lambda: "dashboard:chat-1")

    def post(path: str, body: dict[str, object]) -> dict[str, object]:
        assert path == "/api/spawn"
        calls.append(body)
        return {
            "id": f"run{len(calls)}",
            "executor": "remote",
            "instance_id": "crew-a",
        }

    monkeypatch.setattr(spawn_tools.mcp_core, "_post", post)
    deferred = spawn_tools.spawn_run(
        "spawn_run",
        {
            "tasks": ["review a", "review b"],
            "executor": "remote",
            "instance_id": "crew-a",
            "include_memory": False,
            "include_lessons": False,
        },
    )
    # spawn_run returns a DeferredTool that posts one task per step; drive it
    # to its receipt the way the stdio loop does.
    if isinstance(deferred, mcp_shared.DeferredTool):
        result = mcp_shared.drive_deferred(deferred, clock=spawn_tools.mcp_core.time)
    else:
        result = deferred

    assert len(calls) == 2
    assert all(call["executor"] == "remote" for call in calls)
    assert all(call["instance_id"] == "crew-a" for call in calls)
    assert "[remote:crew-a]" in result


@pytest.mark.asyncio
async def test_restart_restores_terminal_mapping_and_redelivers_once(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents.data_home", lambda: tmp_path)
    run_dir = tmp_path / "subagents" / "remote" / "abcdef12"
    run_dir.mkdir(parents=True)
    result_path = run_dir / "result.txt"
    result_path.write_text("restored-result", encoding="utf-8")
    original = SubagentInfo(
        id="abcdef12",
        task="review",
        parent_session_key="dashboard:chat-1",
        agent="kirocrew",
        done=True,
        result="restored-result",
        result_path=str(result_path),
        executor="remote",
        instance_id="crew-a",
        remote_id="peer01",
    )
    RemoteSubagentService._persist_info(original, delivered=False)

    manager = _Manager()
    service = RemoteSubagentService(SimpleNamespace(instances_manager=None), manager)  # type: ignore[arg-type]
    await service.ensure_restored()
    # Redelivery is its own task, which no route awaits (see the next test).
    assert service._redeliver_task is not None
    await service._redeliver_task

    restored = manager.get("abcdef12")
    assert restored is not None
    assert restored.executor == "remote"
    assert restored.result == "res"
    assert restored.result_truncated is True
    assert manager.reported == [restored]
    state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    assert state["delivered"] is True
    await service.close()


@pytest.mark.asyncio
async def test_the_restore_does_not_wait_for_a_parent_turn_to_take_a_redelivery(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Opus 5.5 on ed42dea447: a redelivery can wait out the parent's turn, and
    that turn may call spawn_status, which awaits the restore. Every spawn route
    awaits the restore, so it must finish without the redelivery."""
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents.data_home", lambda: tmp_path)
    for run_id in ("abcdef12", "abcdef13"):
        RemoteSubagentService._persist_info(
            SubagentInfo(
                id=run_id,
                task="review",
                parent_session_key="dashboard:chat-1",
                agent="kirocrew",
                done=True,
                result="done",
                executor="remote",
                instance_id="crew-a",
                remote_id=f"peer-{run_id}",
            ),
            delivered=False,
        )
    manager = _Manager()
    turn_over = asyncio.Event()

    async def slow_report(info: SubagentInfo) -> bool:
        manager.reported.append(info)
        await turn_over.wait()
        return True

    manager.report_external = slow_report  # type: ignore[method-assign]
    service = RemoteSubagentService(SimpleNamespace(instances_manager=None), manager)  # type: ignore[arg-type]
    await finished(service.ensure_restored(), "the restore, with a redelivery held")
    assert {info.id for info in manager.external_agents} == {"abcdef12", "abcdef13"}
    # A second route call is not held up either.
    await finished(service.ensure_restored(), "a second restore call")
    await asyncio.sleep(0)
    assert len(manager.reported) == 1
    turn_over.set()
    assert service._redeliver_task is not None
    await await_until(service._redeliver_task, what="the redelivery task")
    assert len(manager.reported) == 2
    await service.close()


def test_a_reconnect_replays_the_card_of_a_live_remote_run() -> None:
    """GPT 6.1 on ed42dea447: ``running`` is local-only and the done replay skips
    unfinished records, so a refresh during a remote run lost its card."""
    import inspect

    from kiro_crew.dashboard import ws

    def remote(run_id: str, *, done: bool) -> SubagentInfo:
        return SubagentInfo(
            id=run_id,
            task="review",
            parent_session_key="dashboard:chat-1",
            agent="kirocrew",
            done=done,
            executor="remote",
            instance_id="crew-a",
            remote_id=f"peer-{run_id}",
        )

    frames = ws.live_remote_snapshot_frames(
        SimpleNamespace(
            external_agents=[remote("abcdef12", done=False), remote("abcdef13", done=True)]
        )
    )
    assert [f["type"] for f in frames] == ["subagent_snapshot"]
    data = frames[0]["data"]
    assert data["id"] == "abcdef12"
    assert data["slot"] == ws.subagent_event_slot("dashboard:chat-1")
    assert data["child_session"] == "remote:crew-a:peer-abcdef12"
    assert ws.live_remote_snapshot_frames(SimpleNamespace()) == []
    # The subscribe handler puts them into the same replay list as local runs.
    assert "_replay.extend(live_remote_snapshot_frames(state.subagents))" in inspect.getsource(ws)


@pytest.mark.asyncio
async def test_remote_monitor_recovers_from_disconnect_and_terminal_404(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents.data_home", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents._POLL_SECONDS", 0.0)
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents._RETRY_SECONDS", 0.0)

    reconnecting = _Instances(
        [
            (200, {"id": "peer01"}),
            OSError("tunnel dropped"),
            (200, {"done": True, "turns": {}, "result": "reconnected", "error": ""}),
        ],
        connected=("crew-a",),
    )
    manager = _Manager()
    service = RemoteSubagentService(
        SimpleNamespace(instances_manager=reconnecting), manager  # type: ignore[arg-type]
    )
    info = await _spawn_placed(
        service,
        task="review",
        parent_session="dashboard:chat-1",
        agent="",
        max_turns=0,
        cwd="/remote/project",
        model="",
        reasoning_effort="",
        include_memory=False,
        include_lessons=False,
        include_project=True,
        memory_mode="temporary",
        batch_id="",
        batch_total=0,
    )
    await _settle_monitor(service, info.id)
    assert info.done is True
    assert info.error == ""
    assert manager.reported == [info]
    await service.close()

    missing = _Instances(
        [(200, {"id": "peer02"}), (404, {}), (404, {}), (404, {})],
        connected=("crew-a",),
    )
    missing_manager = _Manager()
    missing_service = RemoteSubagentService(
        SimpleNamespace(instances_manager=missing), missing_manager  # type: ignore[arg-type]
    )
    missing_info = await _spawn_placed(
        missing_service,
        task="review",
        parent_session="dashboard:chat-1",
        agent="",
        max_turns=0,
        cwd="/remote/project",
        model="",
        reasoning_effort="",
        include_memory=False,
        include_lessons=False,
        include_project=True,
        memory_mode="temporary",
        batch_id="",
        batch_total=0,
    )
    await _settle_monitor(missing_service, missing_info.id)
    assert missing_info.done is True
    assert "no longer available" in missing_info.error
    assert missing_manager.reported == [missing_info]
    await missing_service.close()


@pytest.mark.asyncio
async def test_corrupt_persistence_is_ignored_fail_closed(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents.data_home", lambda: tmp_path)
    state_path = tmp_path / "subagents" / "remote" / "abcdef12" / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "id": "abcdef12",
                "executor": "remote",
                "instance_id": "crew-a",
                "remote_id": "../peer",
            }
        ),
        encoding="utf-8",
    )
    malformed_path = tmp_path / "subagents" / "remote" / "abcdef13" / "state.json"
    malformed_path.parent.mkdir(parents=True)
    malformed_path.write_text(
        json.dumps(
            {
                "version": 1,
                "id": "abcdef13",
                "executor": "remote",
                "instance_id": "crew-a",
                "remote_id": "peer02",
                "max_turns": {"not": "an integer"},
            }
        ),
        encoding="utf-8",
    )
    manager = _Manager()
    service = RemoteSubagentService(SimpleNamespace(instances_manager=None), manager)  # type: ignore[arg-type]

    await service.ensure_restored()

    assert manager.external_agents == []
    assert manager.reported == []
    assert service._monitors == {}
    await service.close()


@pytest.mark.asyncio
async def test_remote_terminal_enters_normal_batch_digest_reporter() -> None:
    manager = SubagentManager.__new__(SubagentManager)
    info = SubagentInfo(
        id="remote01",
        task="remote",
        done=True,
        executor="remote",
        instance_id="crew-a",
        remote_id="peer01",
        batch_id="batch01",
        batch_total=2,
    )
    manager._external_agents = {info.id: info}
    manager._claim_finalize = lambda candidate: candidate is info  # type: ignore[method-assign]
    reporter = AsyncMock(return_value=True)
    manager._run_terminal_report = reporter  # type: ignore[method-assign]

    assert await manager.report_external(info) is True
    reporter.assert_awaited_once_with(
        info,
        source="Remote subagent",
        injection_timeout_reason="remote completion delivery timed out",
        mark_delivered_on_success=False,
        settle_digest=True,
    )


@pytest.mark.asyncio
async def test_stop_all_and_parent_teardown_cancel_remote_children() -> None:
    stop_info = SubagentInfo(
        id="remote01",
        task="remote",
        parent_session_key="dashboard:chat-1",
        executor="remote",
        instance_id="crew-a",
        remote_id="peer01",
    )
    stop_manager, stop_canceller = _manager_with_remote(stop_info)

    assert await stop_manager.cancel_for_parent("dashboard:chat-1") == (1, 0)
    stop_canceller.assert_awaited_once_with(stop_info)
    assert stop_info.user_stopped is True

    teardown_info = SubagentInfo(
        id="remote02",
        task="remote",
        parent_session_key="dashboard:chat-2",
        executor="remote",
        instance_id="crew-b",
        remote_id="peer02",
    )
    teardown_manager, teardown_canceller = _manager_with_remote(teardown_info)

    selected = teardown_manager.snapshot_teardown_children("dashboard:chat-2")
    assert selected == ("remote02",)
    assert "remote02" in teardown_manager._teardown_cancelled_ids
    assert (
        await teardown_manager.cancel_for_teardown(
            selected,
            parent_session_key="dashboard:chat-2",
            verb="remove",
        )
        == 1
    )
    teardown_canceller.assert_awaited_once_with(teardown_info)


@pytest.mark.asyncio
async def test_delivered_remote_records_expire_at_configured_ttl(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents.data_home", lambda: tmp_path)
    info = SubagentInfo(
        id="abcdef12",
        task="done",
        done=True,
        executor="remote",
        instance_id="crew-a",
        remote_id="peer01",
    )
    RemoteSubagentService._persist_info(info, delivered=True)
    manager = _Manager()
    manager._result_ttl_secs = 0
    service = RemoteSubagentService(SimpleNamespace(instances_manager=None), manager)  # type: ignore[arg-type]

    await service.ensure_restored()

    assert manager.get("abcdef12") is None
    assert not (tmp_path / "subagents" / "remote" / "abcdef12").exists()
    await service.close()


@pytest.mark.asyncio
async def test_restart_resumes_polling_an_unfinished_remote_mapping(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents.data_home", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents._POLL_SECONDS", 0.0)
    original = SubagentInfo(
        id="abcdef12",
        task="review",
        parent_session_key="dashboard:chat-1",
        executor="remote",
        instance_id="crew-a",
        remote_id="peer01",
    )
    RemoteSubagentService._persist_info(original, delivered=False)
    instances = _Instances(
        [(200, {"done": True, "result": "after-restart", "error": ""})],
        connected=("crew-a",),
    )
    manager = _Manager()
    service = RemoteSubagentService(
        SimpleNamespace(instances_manager=instances), manager  # type: ignore[arg-type]
    )

    await service.ensure_restored()
    await _settle_monitor(service, "abcdef12")

    restored = manager.get("abcdef12")
    assert restored is not None and restored.done is True
    assert manager.reported == [restored]
    assert instances.calls == [("crew-a", "GET", "api/spawn/peer01", None)]
    await service.close()


@pytest.mark.asyncio
async def test_a_failed_terminal_write_publishes_nothing_and_retries(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Persist before publish: no completion reaches the parent, and ``done``
    stays False, until the terminal mapping is on disk; the monitor re-polls
    the peer and retries instead of logging the write failure and reporting."""
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents.data_home", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents._POLL_SECONDS", 0.0)
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents._RETRY_SECONDS", 0.0)
    original = SubagentInfo(
        id="abcdef12",
        task="review",
        parent_session_key="dashboard:chat-1",
        executor="remote",
        instance_id="crew-a",
        remote_id="peer01",
    )
    RemoteSubagentService._persist_info(original, delivered=False)
    instances = _Instances(
        [
            (200, {"done": True, "result": "first", "error": ""}),
            (200, {"done": True, "result": "second", "error": ""}),
        ],
        connected=("crew-a",),
    )
    manager = _Manager()
    service = RemoteSubagentService(
        SimpleNamespace(instances_manager=instances), manager  # type: ignore[arg-type]
    )
    real_persist = RemoteSubagentService._persist_info.__func__
    seen: list[tuple[bool, bool, int]] = []

    def flaky(cls, info, *, delivered, done=None):
        committed = info.done if done is None else done
        seen.append((committed, info.done, len(manager.reported)))
        if committed and len([s for s in seen if s[0]]) == 1:
            raise OSError("disk full")
        return real_persist(cls, info, delivered=delivered, done=done)

    monkeypatch.setattr(RemoteSubagentService, "_persist_info", classmethod(flaky))

    await service.ensure_restored()
    await _settle_monitor(service, "abcdef12")

    restored = manager.get("abcdef12")
    assert restored is not None and restored.done is True
    # The failed commit happened with nothing published and done still False.
    first_commit = next(entry for entry in seen if entry[0])
    assert first_commit[1:] == (False, 0)
    assert manager.reported == [restored]
    assert len(instances.calls) == 2
    state = json.loads((tmp_path / "subagents" / "remote" / "abcdef12" / "state.json").read_text())
    assert state["done"] is True
    await service.close()


@pytest.mark.asyncio
async def test_unpersisted_mapping_cancels_the_peer_run_instead_of_publishing(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents.data_home", lambda: tmp_path)
    instances = _Instances([(200, {"id": "peer01"}), (200, {"cancelled": True})])
    manager = _auto_manager()
    service = RemoteSubagentService(
        SimpleNamespace(instances_manager=instances), manager  # type: ignore[arg-type]
    )
    real_persist = service._persist_info
    writes: list[bool] = []

    def confirmed_write_fails(info: object, **kwargs: object) -> None:
        writes.append(bool(kwargs.get("accepted", True)))
        if kwargs.get("accepted", True):
            raise OSError(28, "No space left on device")
        real_persist(info, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(service, "_persist_info", confirmed_write_fails)

    with pytest.raises(RemoteSubagentError) as caught:
        await _spawn_placed(service, **_spawn_kwargs())  # type: ignore[arg-type]

    assert caught.value.code == "remote_mapping_unpersisted"
    assert manager.external_agents == []
    assert writes == [False, True]
    assert [call[1:3] for call in instances.calls] == [
        ("POST", "api/spawn"),
        ("DELETE", "api/spawn/peer01"),
    ]
    # The peer confirmed the stop, so the unconfirmed record goes too.
    assert not any((tmp_path / "subagents" / "remote").glob("*/state.json"))
    assert not service._monitors
    await service.close()


@pytest.mark.asyncio
async def test_a_run_that_cannot_be_recorded_first_is_never_sent(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The orphan window: the record is written before the peer is asked, so a
    gateway that cannot write it sends nothing."""
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents.data_home", lambda: tmp_path)
    instances = _Instances([])
    service = RemoteSubagentService(
        SimpleNamespace(instances_manager=instances), _Manager()  # type: ignore[arg-type]
    )

    def disk_full(*_args: object, **_kwargs: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(service, "_persist_info", disk_full)
    with pytest.raises(RemoteSubagentError) as caught:
        await _spawn_placed(service, **_spawn_kwargs())  # type: ignore[arg-type]
    assert caught.value.code == "remote_mapping_unpersisted"
    assert instances.calls == []
    await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("withdraw", "kept"),
    [((404, {}), False), ((200, {"cancelled": True}), False), (OSError("down"), True)],
    ids=["never-started", "stopped", "unreachable"],
)
async def test_a_lost_spawn_answer_withdraws_the_named_run(
    tmp_path, monkeypatch: pytest.MonkeyPatch, withdraw: object, kept: bool
) -> None:
    """The peer may have accepted a run whose answer was lost. It was sent under a
    hub-chosen name, so the hub stops that name. A confirmed stop refuses the
    spawn and drops the record; an unconfirmed one keeps the record and the run,
    which the monitor then reconciles (here: the peer never started it)."""
    from kiro_crew.dashboard import remote_subagents

    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents.data_home", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents._POLL_SECONDS", 0.0)
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents._RETRY_SECONDS", 0.0)
    polls = [(404, {})] * remote_subagents._MAX_NOT_FOUND_POLLS if kept else []
    instances = _Instances([OSError("answer lost"), withdraw, *polls])  # type: ignore[list-item]
    service = RemoteSubagentService(
        SimpleNamespace(instances_manager=instances), _auto_manager()  # type: ignore[arg-type]
    )
    if kept:
        info = await _spawn_placed(service, **_spawn_kwargs())  # type: ignore[arg-type]
    else:
        with pytest.raises(RemoteSubagentError):
            await _spawn_placed(service, **_spawn_kwargs())  # type: ignore[arg-type]

    minted = instances.calls[0][3]["run_id"]  # type: ignore[index]
    assert instances.calls[1][1:3] == ("DELETE", f"api/spawn/{minted}")
    states = list((tmp_path / "subagents" / "remote").glob("*/state.json"))
    assert bool(states) is kept
    if kept:
        state = json.loads(states[0].read_text(encoding="utf-8"))
        assert state["accepted"] is False and state["remote_id"] == minted
        await async_wait_until(lambda: info.done, describe=lambda: instances.calls)
        assert instances.calls[2][1:3] == ("GET", f"api/spawn/{minted}")
        assert info.error == remote_subagents._NEVER_STARTED
    await service.close()


@pytest.mark.asyncio
async def test_a_restart_inside_the_window_reports_a_never_started_run_plainly(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents.data_home", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents._POLL_SECONDS", 0.0)
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents._RETRY_SECONDS", 0.0)
    from kiro_crew.dashboard import remote_subagents

    pending = SubagentInfo(
        id="abcdef12",
        task="review",
        parent_session_key="dashboard:chat-1",
        executor="remote",
        instance_id="crew-a",
        remote_id="0123456789abcdef",
    )
    RemoteSubagentService._persist_info(pending, delivered=False, accepted=False)
    polls = [(404, {})] * remote_subagents._MAX_NOT_FOUND_POLLS
    manager = _Manager()
    service = RemoteSubagentService(
        SimpleNamespace(instances_manager=_Instances(polls, connected=("crew-a",))),  # type: ignore[arg-type]
        manager,  # type: ignore[arg-type]
    )
    await service.ensure_restored()
    await _settle_monitor(service, "abcdef12")
    restored = manager.get("abcdef12")
    assert restored is not None and restored.done is True
    assert restored.error == remote_subagents._NEVER_STARTED
    await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("overrides", "refused"),
    [
        ({"memory_mode": "incognito", "approval_mode": "auto", "include_project": False}, True),
        ({"memory_mode": "temporary", "approval_mode": "auto", "include_project": False}, True),
        ({"approval_mode": "", "include_project": False}, True),
        # Nothing to tighten still needs the crew to keep the run's name: a crew
        # that advertises nothing could run it under another id and lose it.
        ({"approval_mode": "auto", "include_project": False}, True),
    ],
    ids=["incognito", "temporary", "floor", "no-run-id"],
)
async def test_a_crew_that_does_not_advertise_the_tightening_never_gets_the_task(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, object],
    refused: bool,
) -> None:
    """A crew that does not advertise the fields is refused before it sees the task."""
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents.data_home", lambda: tmp_path)
    instances = _Instances(
        [(200, {"id": "peer01"})],
        connected=("crew-a",),
        advertises=False,
    )
    manager = _Manager()
    service = RemoteSubagentService(
        cast(Any, SimpleNamespace(instances_manager=instances)), cast(Any, manager)
    )
    monkeypatch.setattr(service, "_monitor", AsyncMock())

    if refused:
        with pytest.raises(RemoteSubagentError) as caught:
            await _spawn_placed(service, **_spawn_kwargs(**overrides))  # type: ignore[arg-type]
        assert caught.value.code == "remote_peer_unenforced"
        assert caught.value.status == 409
        # Not a single request reached the crew, so no task text left the hub.
        assert instances.calls == []
        assert manager.external_agents == []
    else:
        info = await _spawn_placed(service, **_spawn_kwargs(**overrides))  # type: ignore[arg-type]
        assert info.remote_id == "peer01"
        assert [call[1:3] for call in instances.calls] == [("POST", "api/spawn")]
    await service.close()
    await service.close()


@pytest.mark.asyncio
async def test_privacy_mode_runs_leave_no_task_or_result_on_disk(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents.data_home", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents._POLL_SECONDS", 0.0)
    instances = _Instances(
        [(200, {"id": "peer01"}), (200, {"done": True, "result": "private answer"})]
    )
    manager = _Manager()
    service = RemoteSubagentService(
        SimpleNamespace(instances_manager=instances), manager  # type: ignore[arg-type]
    )

    info = await _spawn_placed(service, **_spawn_kwargs(memory_mode="incognito"))  # type: ignore[arg-type]
    await _settle_monitor(service, info.id)

    run_dir = tmp_path / "subagents" / "remote" / info.id
    state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    assert state["task"] == ""
    assert state["remote_id"] == "peer01"
    assert not (run_dir / "result.txt").exists()
    assert info.result_path == ""
    assert "secret plan" not in (run_dir / "state.json").read_text(encoding="utf-8")
    await service.close()


@pytest.mark.asyncio
async def test_a_permanently_unreachable_peer_ends_the_shadow_run(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents.data_home", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents._RETRY_SECONDS", 0.0)
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents._UNREACHABLE_DEADLINE_SECONDS", 0.0)
    instances = _Instances(
        [(200, {"id": "peer01"}), ConnectionError("gone"), (200, {"cancelled": True})]
    )
    manager = _Manager()
    service = RemoteSubagentService(
        SimpleNamespace(instances_manager=instances), manager  # type: ignore[arg-type]
    )

    info = await _spawn_placed(service, **_spawn_kwargs())  # type: ignore[arg-type]
    await _settle_monitor(service, info.id)

    assert info.done is True
    assert "unreachable" in info.error
    assert manager.reported == [info]
    # The peer is then owed a stop, sent once it answers.
    await await_until(service._owed_stops[info.id], what="the owed stop")
    assert instances.calls[-1][1:3] == ("DELETE", "api/spawn/peer01")
    await service.close()


@pytest.mark.asyncio
async def test_a_digest_held_completion_is_not_acknowledged(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents.data_home", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents._POLL_SECONDS", 0.0)
    instances = _Instances([(200, {"id": "peer01"}), (200, {"done": True, "result": "ok"})])
    manager = _Manager()

    async def held(info: SubagentInfo) -> bool:
        # The gateway parked this wave member in the digest: the reporter
        # returns True, but nothing has reached the parent yet.
        info._digest_held = True
        manager.reported.append(info)
        return True

    manager.report_external = held  # type: ignore[method-assign]
    service = RemoteSubagentService(
        SimpleNamespace(instances_manager=instances), manager  # type: ignore[arg-type]
    )

    info = await _spawn_placed(service, **_spawn_kwargs(batch_id="w1", batch_total=2))  # type: ignore[arg-type]
    await _settle_monitor(service, info.id)

    state = json.loads(
        (tmp_path / "subagents" / "remote" / info.id / "state.json").read_text(encoding="utf-8")
    )
    assert state["done"] is True
    assert state["delivered"] is False

    # The digest later reaches the parent; its settle acknowledges the run, so
    # a restart does not deliver the same result a second time.
    assert manager.external_placement is service
    await service.acknowledge_delivered([info.id, "notmine1"])
    state = json.loads(
        (tmp_path / "subagents" / "remote" / info.id / "state.json").read_text(encoding="utf-8")
    )
    assert state["delivered"] is True
    assert not (tmp_path / "subagents" / "remote" / "notmine1").exists()
    await service.close()


@pytest.mark.asyncio
async def test_the_service_binds_once_and_close_unbinds_every_duty(tmp_path, monkeypatch) -> None:
    """One reference carries cancel, dismiss and delivery acknowledgement.

    Closing drops all three together: before, ``close`` unbound the canceller
    and the dismisser but left the delivery listener pointing at a closed service.
    """
    from kiro_crew.subagent import ExternalPlacement

    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents.data_home", lambda: tmp_path)
    manager = SubagentManager(sessions=SimpleNamespace(), ctx_builder=None)
    service = RemoteSubagentService(
        cast(Any, SimpleNamespace(instances_manager=_Instances([]))), manager
    )
    placement: ExternalPlacement = service
    assert cast(Any, manager)._external_placement is placement

    await service.close()

    assert cast(Any, manager)._external_placement is None
    info = SubagentInfo(
        id="remote01",
        task="t",
        parent_session_key="dashboard:chat-1",
        executor="remote",
        instance_id="crew-a",
        remote_id="peer01",
    )
    manager.register_external(info)
    with pytest.raises(RuntimeError, match="unavailable"):
        await manager.cancel_external("remote01")


@pytest.mark.asyncio
async def test_settles_hand_only_delivered_external_ids_to_the_listener() -> None:
    from kiro_crew.subagent import SubagentDelivery

    info = SubagentInfo(
        id="remote01", task="t", parent_session_key="dashboard:chat-1", executor="remote"
    )
    manager, _canceller = _manager_with_remote(info)
    raw = cast(Any, manager)
    raw._waves = SimpleNamespace(
        settle_queued_delivery_impl=AsyncMock(), _settle_digest_holds_impl=AsyncMock()
    )
    listener = cast(Any, manager)._external_placement.acknowledge_delivered
    deliveries = [
        SubagentDelivery("remote01", 1.0, 0.0),
        SubagentDelivery("local001", 1.0, 0.0),
        SubagentDelivery("remote01", 0.0, 0.0, report_owed=True),
    ]

    await manager.settle_queued_delivery(deliveries)
    listener.assert_awaited_once_with(["remote01"])

    listener.reset_mock()
    holder = SubagentInfo(id="member01", task="t", parent_session_key="dashboard:chat-1")
    holder._digest_settle_deliveries = [SubagentDelivery("remote01", 1.0, 0.0)]
    await manager._settle_digest_holds(holder)
    listener.assert_awaited_once_with(["remote01"])

    # A digest whose injection the gateway gave up on did not reach the parent.
    listener.reset_mock()
    holder._digest_settle_deliveries = [SubagentDelivery("remote01", 1.0, 0.0)]
    holder._report_undelivered = True
    await manager._settle_digest_holds(holder)
    listener.assert_not_awaited()

    # A failing acknowledgement never fails the settle itself.
    listener.side_effect = RuntimeError("disk full")
    await manager.settle_queued_delivery([SubagentDelivery("remote01", 1.0, 0.0)])


@pytest.mark.asyncio
async def test_version_skew_is_a_typed_refusal_not_a_bare_500(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unreachable(*_args: object, **_kwargs: object) -> None:
        raise ConnectionError("version read failed")

    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents._ensure_version_parity", unreachable)
    instances = _Instances([])
    service = RemoteSubagentService(
        SimpleNamespace(instances_manager=instances), _Manager()  # type: ignore[arg-type]
    )

    with pytest.raises(RemoteSubagentError) as caught:
        await _spawn_placed(service, **_spawn_kwargs())  # type: ignore[arg-type]

    assert caught.value.status == 409
    assert instances.calls == []
    await service.close()


def test_a_swallowed_injection_failure_is_not_read_as_delivered() -> None:
    from kiro_crew.dashboard.remote_subagents import _reached_parent

    info = SubagentInfo(id="remote01", task="t", parent_session_key="cron:job-1")
    assert _reached_parent(info) is True
    # A channel or cron parent whose injection attempts all failed: the
    # reporter returned True, but the parent never saw the result.
    info._report_undelivered = True
    assert _reached_parent(info) is False


@pytest.mark.asyncio
async def test_a_failed_redelivery_does_not_strand_live_runs_on_restore(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents.data_home", lambda: tmp_path)
    finished = SubagentInfo(
        id="aaaaaaa1",
        task="t",
        parent_session_key="dashboard:chat-1",
        done=True,
        result="r",
        executor="remote",
        instance_id="crew-a",
        remote_id="peer01",
    )
    live = SubagentInfo(
        id="bbbbbbb2",
        task="t",
        parent_session_key="dashboard:chat-1",
        executor="remote",
        instance_id="crew-a",
        remote_id="peer02",
    )
    RemoteSubagentService._persist_info(finished, delivered=False)
    RemoteSubagentService._persist_info(live, delivered=False)

    manager = _Manager()

    async def broken(_info: SubagentInfo) -> bool:
        raise OSError("disk full")

    manager.report_external = broken  # type: ignore[method-assign]
    service = RemoteSubagentService(SimpleNamespace(instances_manager=None), manager)  # type: ignore[arg-type]
    started: list[str] = []

    async def monitor(info: SubagentInfo) -> None:
        started.append(info.id)

    monkeypatch.setattr(service, "_monitor", monitor)
    await service.ensure_restored()
    await asyncio.sleep(0)

    assert started == ["bbbbbbb2"]
    state = json.loads(
        (tmp_path / "subagents" / "remote" / "aaaaaaa1" / "state.json").read_text(encoding="utf-8")
    )
    assert state["delivered"] is False  # retried on the next start
    await service.close()


@pytest.mark.asyncio
async def test_one_bad_record_does_not_fail_the_whole_restore(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A record with an empty crew binding, or one the manager refuses, is
    skipped; the other runs still restore and the restore itself succeeds,
    so the spawn routes that await it do not error."""
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents.data_home", lambda: tmp_path)
    runs = {
        run_id: SubagentInfo(
            id=run_id,
            task="t",
            parent_session_key="dashboard:chat-1",
            executor="remote",
            instance_id="crew-a",
            remote_id=f"peer0{index}",
        )
        for index, run_id in enumerate(("aaaaaaa1", "bbbbbbb2", "ccccccc3"), start=1)
    }
    for info in runs.values():
        RemoteSubagentService._persist_info(info, delivered=False)
    blank = tmp_path / "subagents" / "remote" / "aaaaaaa1" / "state.json"
    state = json.loads(blank.read_text(encoding="utf-8"))
    state["instance_id"] = ""
    blank.write_text(json.dumps(state), encoding="utf-8")

    manager = _Manager()
    accept = manager.register_external

    def picky(info: SubagentInfo) -> None:
        if info.id == "bbbbbbb2":
            raise ValueError("subagent id already registered: bbbbbbb2")
        accept(info)

    manager.register_external = picky  # type: ignore[method-assign]
    service = RemoteSubagentService(SimpleNamespace(instances_manager=None), manager)  # type: ignore[arg-type]
    started: list[str] = []

    async def monitor(info: SubagentInfo) -> None:
        started.append(info.id)

    monkeypatch.setattr(service, "_monitor", monitor)
    await service.ensure_restored()
    await asyncio.sleep(0)

    assert [info.id for info in manager.external_agents] == ["ccccccc3"]
    assert started == ["ccccccc3"]
    await service.close()


@pytest.mark.asyncio
async def test_a_failed_restore_is_retried_on_the_next_call(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents.data_home", lambda: tmp_path)
    info = SubagentInfo(
        id="ddddddd4",
        task="t",
        parent_session_key="dashboard:chat-1",
        done=True,
        result="r",
        executor="remote",
        instance_id="crew-a",
        remote_id="peer04",
    )
    RemoteSubagentService._persist_info(info, delivered=True)
    real_load = RemoteSubagentService._load_records.__func__
    calls: list[int] = []

    def flaky(cls):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("transient")
        return real_load(cls)

    monkeypatch.setattr(RemoteSubagentService, "_load_records", classmethod(flaky))
    manager = _Manager()
    service = RemoteSubagentService(SimpleNamespace(instances_manager=None), manager)  # type: ignore[arg-type]

    with pytest.raises(RuntimeError):
        await service.ensure_restored()
    await service.ensure_restored()

    assert len(calls) == 2
    assert [restored.id for restored in manager.external_agents] == ["ddddddd4"]
    await service.close()


@pytest.mark.asyncio
async def test_automatic_placement_skips_a_version_skewed_crew(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents.data_home", lambda: tmp_path)
    instances = _Instances([(200, {"id": "peer01"})], connected=("crew-a", "crew-b"))

    async def version(instance_id: str) -> tuple[bool, str]:
        # crew-a is the least-loaded (alphabetical tie winner) but a feature
        # release ahead; crew-b matches.
        return True, ("99.0.0" if instance_id == "crew-a" else kiro_crew.__version__)

    instances.peer_version = version  # type: ignore[method-assign]
    manager = _Manager()
    service = RemoteSubagentService(SimpleNamespace(instances_manager=instances), manager)  # type: ignore[arg-type]
    monkeypatch.setattr(service, "_monitor", AsyncMock())

    info = await _spawn_placed(service, **_spawn_kwargs())  # type: ignore[arg-type]

    assert info.instance_id == "crew-b"
    assert [call[0] for call in instances.calls] == ["crew-b"]

    # An explicitly requested skewed crew is still a typed refusal.
    with pytest.raises(RemoteSubagentError) as caught:
        await _spawn_placed(service, **_spawn_kwargs(instance_id="crew-a"))  # type: ignore[arg-type]
    assert caught.value.status == 409
    await service.close()


@pytest.mark.asyncio
async def test_dismissing_a_finished_remote_run_waits_for_its_delivery() -> None:
    info = SubagentInfo(
        id="remote01",
        task="t",
        parent_session_key="dashboard:chat-1",
        executor="remote",
        done=True,
    )
    manager, _canceller = _manager_with_remote(info)

    info._delivery_queued = True
    assert await manager.settle_before_delete("remote01") == "pending"
    assert manager.get("remote01") is info

    info._delivery_queued = False
    assert await manager.settle_before_delete("remote01") == "delivered"
    # Gone from the inventory, so the spawn list does not show it.
    assert cast(Any, manager)._external_agents == {}


@pytest.mark.asyncio
async def test_a_dismissed_remote_run_stays_dismissed_across_a_restart(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dismissal is committed to the mapping before the pop, so a restart
    does not restore it; a failed commit leaves the run listed and retryable."""
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents.data_home", lambda: tmp_path)
    info = SubagentInfo(
        id="abcdef12",
        task="t",
        parent_session_key="dashboard:chat-1",
        executor="remote",
        instance_id="crew-a",
        remote_id="peer01",
        done=True,
    )
    RemoteSubagentService._persist_info(info, delivered=True)
    manager, _canceller = _manager_with_remote(info)
    service = RemoteSubagentService(
        SimpleNamespace(instances_manager=_Instances([], connected=("crew-a",))),
        manager,  # type: ignore[arg-type]
    )
    real_persist = RemoteSubagentService._persist_info.__func__

    def unwritable(cls, *args, **kwargs):
        raise OSError("read-only")

    monkeypatch.setattr(RemoteSubagentService, "_persist_info", classmethod(unwritable))
    assert await manager.settle_before_delete("abcdef12") == "pending"
    assert manager.get("abcdef12") is info

    monkeypatch.setattr(RemoteSubagentService, "_persist_info", classmethod(real_persist))
    assert await manager.settle_before_delete("abcdef12") == "delivered"
    assert manager.get("abcdef12") is None
    assert RemoteSubagentService._load_records() == []
    await service.close()


def test_a_late_acknowledgement_cannot_undo_a_committed_dismissal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A delivery acknowledgement already in flight when the operator dismisses
    the run must not write ``dismissed: false`` back: mapping writes for one run
    are serialized and a committed dismissal is sticky."""
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents.data_home", lambda: tmp_path)
    info = SubagentInfo(
        id="abcdef12",
        task="t",
        parent_session_key="dashboard:chat-1",
        executor="remote",
        instance_id="crew-a",
        remote_id="peer01",
        done=True,
    )
    RemoteSubagentService._persist_info(info, delivered=False)

    # Sequential: an acknowledgement after the dismissal keeps it.
    RemoteSubagentService._persist_info(info, delivered=True, dismissed=True)
    RemoteSubagentService._persist_info(info, delivered=True)
    assert RemoteSubagentService._load_records() == []

    # Interleaved: the acknowledgement has read the mapping (not yet dismissed)
    # and is about to write when the dismissal arrives on another thread.
    # Start from a fresh, undismissed mapping (the dismissal above is sticky).
    (tmp_path / "subagents" / "remote" / "abcdef12" / "state.json").unlink()
    RemoteSubagentService._persist_info(info, delivered=False)
    assert len(RemoteSubagentService._load_records()) == 1
    real_stored = RemoteSubagentService._stored_true
    ack_has_read = threading.Event()
    release_ack = threading.Event()

    def paused_read(run_dir: Path, key: str) -> bool:
        value = real_stored(run_dir, key)
        if threading.current_thread().name == "ack":
            ack_has_read.set()
            release_ack.wait(timeout=5)
        return value

    monkeypatch.setattr(RemoteSubagentService, "_stored_true", staticmethod(paused_read))
    real_lock = remote_subagents._mapping_lock
    dismiss_at_lock = threading.Event()

    class _Announcing:
        """The run's real mapping lock, announcing the dismissal's arrival at it."""

        def __init__(self, run_id: str) -> None:
            self._lock = real_lock(run_id)

        def __enter__(self) -> None:
            if threading.current_thread().name == "dismiss":
                dismiss_at_lock.set()
            self._lock.acquire()

        def __exit__(self, *_exc: object) -> None:
            self._lock.release()

    monkeypatch.setattr(remote_subagents, "_mapping_lock", _Announcing)
    ack = threading.Thread(
        target=RemoteSubagentService._persist_info,
        args=(info,),
        kwargs={"delivered": True},
        name="ack",
    )
    ack.start()
    assert ack_has_read.wait(timeout=5)
    dismiss = threading.Thread(
        target=RemoteSubagentService._persist_info,
        args=(info,),
        kwargs={"delivered": True, "dismissed": True},
        name="dismiss",
    )
    dismiss.start()
    # The dismissal has reached the lock the paused acknowledgement holds.
    assert dismiss_at_lock.wait(timeout=5)
    release_ack.set()
    ack.join(timeout=5)
    dismiss.join(timeout=5)
    assert not ack.is_alive() and not dismiss.is_alive()

    state = json.loads(
        (tmp_path / "subagents" / "remote" / "abcdef12" / "state.json").read_text(encoding="utf-8")
    )
    assert state["dismissed"] is True
    assert RemoteSubagentService._load_records() == []


@pytest.mark.parametrize("record", ("abcdef12/state.json", "remote/abcdef12/state.json"))
def test_remote_run_mappings_are_write_protected_like_run_records(record: str) -> None:
    """A mapping names the parent session a restart restores, which decides who
    may read or cancel the run and where its result lands. It lives inside the
    ``subagents`` registry, so the same seal holds it: agent file edits are
    refused, reads stay open."""
    from kiro_crew import security

    path = os.path.expanduser(f"~/.kiro/crew/subagents/{record}")
    assert security.is_sensitive_write_path(path) is True
    assert security.is_sensitive_path(path) is False


def test_remote_records_live_inside_the_sealed_run_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The records root must stay under ``subagents/``: a sibling leaf would need
    its own seal, and an unsealed one lets an agent forge a run's parent session."""
    from kiro_crew.dashboard import remote_subagents

    monkeypatch.setattr(remote_subagents, "data_home", lambda: tmp_path)
    root = remote_subagents._records_root()
    assert root.parent == tmp_path / "subagents"
    assert not re.fullmatch(r"[a-f0-9]+", root.name), "the root must never look like a run id"


def test_local_registry_walks_skip_the_remote_records_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every local walk of ``subagents/`` must pass over ``remote/``: it has no
    ``state.json`` of its own, so it is neither an orphan to recover, a panel
    record, a legacy queue row, nor a tombstoned folder to prune."""
    from kiro_crew import subagent_persistence
    from kiro_crew.taskq.migrate import legacy_subagent_records

    registry = tmp_path / "subagents"
    record = registry / "remote" / "abcdef12"
    record.mkdir(parents=True)
    (record / "state.json").write_text(
        json.dumps({"version": 1, "id": "abcdef12", "executor": "remote"}), encoding="utf-8"
    )
    monkeypatch.setattr(subagent_persistence, "_SUBAGENTS_DIR", registry)

    assert subagent_persistence.list_orphans() == []
    assert subagent_persistence.prune_stale_tombstones(max_age_days=0, delivered_ttl_secs=0) == 0
    assert subagent_persistence.read_panel_records(keep=10, max_age_secs=86400).records == []
    assert legacy_subagent_records(registry, now=time.time()) == []
    assert (record / "state.json").is_file()
