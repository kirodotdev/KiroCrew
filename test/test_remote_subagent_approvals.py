"""Remote sub-agent approval floor, the hub approval relay and placement stops."""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, cast
from unittest.mock import AsyncMock

import pytest
from remote_subagent_helpers import (
    _AnswerRequest,
    _approved_spawn_held_at,
    _failing_terminal_write,
    _floored_info,
    _Instances,
    _Manager,
    _no_snapshot,
    _raising_sessions,
    _remote_record,
    _Sessions,
    _settle_monitor,
    _spawn_kwargs,
    _spawn_placed,
)

import kiro_crew
from kiro_crew.dashboard import remote_subagents
from kiro_crew.dashboard.remote_subagents import RemoteSubagentError, RemoteSubagentService
from kiro_crew.subagent import SubagentInfo
from kiro_crew.testing.wait import async_wait_until, await_until

# The local-path cases drive SubagentManager.spawn; pin the host-memory reading
# so a memory-pressured runner does not queue them.
pytestmark = pytest.mark.usefixtures("healthy_host_memory")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("configure", "approval_mode", "expected"),
    [
        (lambda m: None, "", "interactive"),
        (lambda m: setattr(m, "_sessions", _Sessions(policy="auto")), "", ""),
        (lambda m: None, "auto", ""),
        (lambda m: setattr(m, "_is_yolo", lambda: True), "", ""),
        # Global approval_mode=auto counts only for a parent that is gone ...
        (
            lambda m: (
                setattr(m, "_global_approval_mode", "auto"),
                setattr(m, "_sessions", _Sessions(present=False)),
            ),
            "",
            "",
        ),
        # ... never for a live parent that is intentionally not on auto.
        (lambda m: setattr(m, "_global_approval_mode", "auto"), "", "interactive"),
        # An unreadable posture must not loosen the child.
        (lambda m: setattr(m, "_sessions", _raising_sessions()), "auto", "interactive"),
        # Opus 5.5 on ed42dea447: the run step's last rung, the hook that
        # auto-approves tools inside this gateway's subagents. Only ``True`` counts.
        (
            lambda m: setattr(
                m,
                "_ctx_builder",
                SimpleNamespace(hooks=SimpleNamespace(auto_approve_subagent_tools=True)),
            ),
            "",
            "",
        ),
        (
            lambda m: setattr(
                m,
                "_ctx_builder",
                SimpleNamespace(hooks=SimpleNamespace(auto_approve_subagent_tools=False)),
            ),
            "",
            "interactive",
        ),
    ],
    ids=[
        "normal-parent",
        "auto-parent",
        "sdk-auto",
        "hub-yolo",
        "global-auto-parent-gone",
        "global-auto-parent-alive",
        "store-unreadable",
        "subagent-tools-hook-on",
        "subagent-tools-hook-off",
    ],
)
async def test_remote_spawn_forwards_the_parents_approval_floor(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    configure: Callable[[Any], object],
    approval_mode: str,
    expected: str,
) -> None:
    """The crew gets the posture the same child would have here, as a floor."""
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents.data_home", lambda: tmp_path)
    instances = _Instances([(200, {"id": "peer01"})], connected=("crew-a",))
    manager = _Manager()
    configure(manager)
    service = RemoteSubagentService(
        cast(Any, SimpleNamespace(instances_manager=instances)), cast(Any, manager)
    )
    monkeypatch.setattr(service, "_monitor", AsyncMock())

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
        memory_mode="persistent",
        batch_id="",
        batch_total=0,
        approval_mode=approval_mode,
    )

    body = instances.calls[0][3]
    assert isinstance(body, dict)
    if expected:
        assert body["approval_floor"] == expected
    else:
        assert "approval_floor" not in body
    assert "approval_mode" not in body
    assert info.approval_floor == expected
    state = json.loads(
        (tmp_path / "subagents" / "remote" / info.id / "state.json").read_text(encoding="utf-8")
    )
    assert state["approval_floor"] == expected
    await service.close()


@pytest.mark.asyncio
async def test_restored_remote_run_keeps_its_approval_floor(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents.data_home", lambda: tmp_path)
    for run_id, floor in (("abcdef12", "interactive"), ("abcdef13", "bogus")):
        RemoteSubagentService._persist_info(
            SubagentInfo(
                id=run_id,
                task="review",
                parent_session_key="dashboard:chat-1",
                done=True,
                executor="remote",
                instance_id="crew-a",
                remote_id="peer01",
                approval_floor=floor,
            ),
            delivered=True,
        )
    manager = _Manager()
    service = RemoteSubagentService(
        cast(Any, SimpleNamespace(instances_manager=None)), cast(Any, manager)
    )
    await service.ensure_restored()

    kept = manager.get("abcdef12")
    unknown = manager.get("abcdef13")
    assert kept is not None and kept.approval_floor == "interactive"
    # Only the one known value survives a reload; anything else reads as none.
    assert unknown is not None and unknown.approval_floor == ""
    await service.close()


@pytest.mark.asyncio
async def test_a_lost_private_result_is_redelivered_as_an_error_not_an_empty_success(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An incognito/temporary result is never written to disk, so a run that
    # finished but was not delivered before a restart has no result left.
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents.data_home", lambda: tmp_path)
    cases = (
        ("abcdef21", "incognito", False),
        ("abcdef22", "temporary", False),
        ("abcdef23", "incognito", True),
        ("abcdef24", "persistent", False),
    )
    for run_id, mode, delivered in cases:
        RemoteSubagentService._persist_info(
            SubagentInfo(
                id=run_id,
                task="review",
                parent_session_key="dashboard:chat-1",
                done=True,
                executor="remote",
                instance_id="crew-a",
                remote_id=f"peer-{run_id}",
                memory_mode=mode,
            ),
            delivered=delivered,
        )
    manager = _Manager()
    service = RemoteSubagentService(
        cast(Any, SimpleNamespace(instances_manager=None)), cast(Any, manager)
    )
    await service.ensure_restored()

    errors = {}
    for run_id, *_ in cases:
        restored = manager.get(run_id)
        assert restored is not None
        errors[run_id] = restored.error
    assert "did not survive a gateway restart" in errors["abcdef21"]
    assert "did not survive a gateway restart" in errors["abcdef22"]
    assert errors["abcdef23"] == ""  # already delivered: nothing to redeliver
    assert errors["abcdef24"] == ""  # persistent: its result is on disk when it exists
    await service.close()


@pytest.mark.asyncio
async def test_a_parked_hub_request_is_listed_answered_and_cleared() -> None:
    from kiro_crew.subagent_manager import hub_approvals

    event = SimpleNamespace(
        title="execute_bash: ls", tool_input='{"command": "ls"}', tool_purpose=""
    )
    waiting = asyncio.create_task(hub_approvals.ask_hub("run-p1", event))
    for _ in range(100):
        listed = hub_approvals.pending_for("run-p1")
        if listed:
            break
        await asyncio.sleep(0)
    assert [item["title"] for item in listed] == ["execute_bash: ls"]
    assert hub_approvals.resolve("run-p1", "f" * 16, True) is False
    assert hub_approvals.resolve("run-p1", listed[0]["id"], True) is True
    assert await waiting is True
    assert hub_approvals.pending_for("run-p1") == []
    # A request nobody answers is a rejection once its wait runs out.
    assert await hub_approvals.ask_hub("run-p2", event, timeout=0.01) is False
    assert hub_approvals.pending_for("run-p2") == []


@pytest.mark.asyncio
async def test_only_the_owner_answers_a_hub_request(monkeypatch: pytest.MonkeyPatch) -> None:
    from kiro_crew.dashboard.handlers.remote_approvals import api_spawn_approval_answer
    from kiro_crew.subagent_manager import hub_approvals

    audited: list[dict[str, object]] = []
    monkeypatch.setattr(
        "kiro_crew.sel.sel", lambda: SimpleNamespace(log_api_access=lambda **kw: audited.append(kw))
    )
    resolved: list[tuple[str, str, bool]] = []
    monkeypatch.setattr(
        hub_approvals, "resolve", lambda run, aid, ok: resolved.append((run, aid, ok)) or True
    )
    # An agent on this gateway (internal MCP caller, an app) or another user never answers.
    for request in (
        _AnswerRequest({"approved": True}, user="owner", internal_auth=True),
        _AnswerRequest({"approved": True}, user="owner", app="notes"),
        _AnswerRequest({"approved": True}, user="other"),
    ):
        assert (await api_spawn_approval_answer(cast(Any, request))).status == 403
    for request in (
        _AnswerRequest({"approved": True}, user="owner", approval_id="../x"),
        _AnswerRequest({"approved": "yes"}, user="owner"),
    ):
        assert (await api_spawn_approval_answer(cast(Any, request))).status == 400
    assert resolved == [] and audited == []

    response = await api_spawn_approval_answer(
        cast(Any, _AnswerRequest({"approved": False}, user="owner"))
    )
    assert response.status == 200
    assert resolved == [("run-q1", "a" * 16, False)]
    assert audited[0]["operation"] == "subagent.hub_approval"
    assert audited[0]["outcome"] == "rejected"

    monkeypatch.setattr(hub_approvals, "resolve", lambda *_a: False)
    response = await api_spawn_approval_answer(
        cast(Any, _AnswerRequest({"approved": True}, user="owner"))
    )
    assert response.status == 404


@pytest.mark.asyncio
async def test_registered_approval_route_reaches_the_real_handler(monkeypatch) -> None:
    from aiohttp import web

    from kiro_crew.dashboard import server
    from kiro_crew.dashboard.handlers import remote_approvals

    sentinel = web.json_response({"reached": True})

    async def _stub(_request: Any) -> web.Response:
        return sentinel

    monkeypatch.setattr(remote_approvals, "api_spawn_approval_answer", _stub)
    app = web.Application()
    server._register_mcp_routes(app)
    route = next(
        r
        for r in app.router.routes()
        if r.method == "POST"
        and r.resource is not None
        and r.resource.canonical == "/api/spawn/{agent_id}/approvals/{approval_id}"
    )
    assert await route.handler(cast(Any, object())) is sentinel


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", [True, False])
async def test_the_hub_asks_its_person_and_posts_the_answer_back(answer: bool) -> None:
    from kiro_crew.dashboard.chat_utils import dashboard_slot_key

    asked: list[tuple[tuple[object, ...], dict[str, object]]] = []

    async def request_approval(*args: object, **kwargs: object) -> bool:
        asked.append((args, kwargs))
        return answer

    instances = _Instances([(200, {"approved": answer})])
    state = SimpleNamespace(instances_manager=instances, request_approval=request_approval)
    service = RemoteSubagentService(state, _Manager())  # type: ignore[arg-type]
    info = _floored_info()
    listed = [{"id": "a" * 16, "title": "execute_bash: ls", "tool_input": "ls", "tool_purpose": ""}]

    service._relay_approvals(info, listed)
    service._relay_approvals(info, listed)  # the next poll does not ask twice
    assert len(service._relays) == 1
    await await_until(asyncio.gather(*service._relays.values()), what="the approval relays")

    assert len(asked) == 1
    args, kwargs = asked[0]
    assert args[1] == "subagent" and "execute_bash: ls" in str(args[2])
    assert kwargs["slot"] == dashboard_slot_key("dashboard:chat-1")
    assert kwargs["is_background"] is False
    assert instances.calls == [
        ("crew-a", "POST", f"api/spawn/peer01/approvals/{'a' * 16}", {"approved": answer})
    ]
    assert service._relays == {}
    await service.close()


@pytest.mark.asyncio
async def test_a_request_the_peer_settled_is_withdrawn_on_the_hub() -> None:
    gate = asyncio.Event()
    cancelled: list[bool] = []

    async def request_approval(*_args: object, **_kwargs: object) -> bool:
        try:
            await gate.wait()
        except asyncio.CancelledError:
            cancelled.append(True)
            raise
        return True

    instances = _Instances([])
    state = SimpleNamespace(instances_manager=instances, request_approval=request_approval)
    service = RemoteSubagentService(state, _Manager())  # type: ignore[arg-type]
    info = _floored_info()
    service._relay_approvals(info, [{"id": "b" * 16, "title": "write"}])
    await asyncio.sleep(0)
    (task,) = service._relays.values()

    service._relay_approvals(info, [])
    with pytest.raises(asyncio.CancelledError):
        await await_until(task, what="the cancelled relay")
    assert cancelled == [True]
    assert instances.calls == []
    await service.close()


@pytest.mark.asyncio
async def test_only_a_floored_run_relays_and_only_well_formed_ids() -> None:
    asked: list[object] = []

    async def request_approval(*args: object, **_kwargs: object) -> bool:
        asked.append(args)
        return True

    state = SimpleNamespace(instances_manager=_Instances([]), request_approval=request_approval)
    service = RemoteSubagentService(state, _Manager())  # type: ignore[arg-type]
    service._relay_approvals(_floored_info(approval_floor=""), [{"id": "c" * 16, "title": "x"}])
    service._relay_approvals(
        _floored_info(), [{"id": "../../steer", "title": "x"}, "junk", {"title": "no id"}]
    )
    assert service._relays == {} and asked == []
    await service.close()


@pytest.mark.asyncio
async def test_a_floored_run_needs_a_crew_that_relays_approvals() -> None:
    instances = _Instances([])

    async def older_peer(_instance_id: str, _path: str) -> tuple[bool, object]:
        return True, {
            "version": kiro_crew.__version__,
            "spawn_enforces": ["approval_floor", "memory_mode", "run_id"],
        }

    instances.peer_capability = older_peer  # type: ignore[method-assign]
    with pytest.raises(RemoteSubagentError) as refused:
        await remote_subagents._ensure_peer_supports(
            instances, "crew-a", memory_mode="", approval_floor="interactive"
        )
    assert refused.value.code == "remote_peer_unenforced"
    # A run without a floor still goes to that crew.
    await remote_subagents._ensure_peer_supports(
        instances, "crew-a", memory_mode="temporary", approval_floor=""
    )


@pytest.mark.asyncio
async def test_a_floored_runs_status_lists_its_parked_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew.dashboard.messaging_api import run_views
    from kiro_crew.subagent_manager import hub_approvals

    runs = {
        "run-s1": SubagentInfo(id="run-s1", task="t", approval_floor="interactive"),
        "run-s2": SubagentInfo(id="run-s2", task="t"),
    }

    async def _restored() -> None:
        return None

    monkeypatch.setattr(
        "kiro_crew.dashboard.remote_subagents.get_remote_subagent_service",
        lambda _state: SimpleNamespace(ensure_restored=_restored),
    )
    state = SimpleNamespace(subagents=SimpleNamespace(get=runs.get))
    event = SimpleNamespace(title="execute_bash: ls", tool_input="ls", tool_purpose="")
    waits = [asyncio.create_task(hub_approvals.ask_hub(run, event)) for run in runs]
    await asyncio.sleep(0)

    async def _status(run_id: str) -> dict[str, object]:
        request = SimpleNamespace(app={"state": state}, match_info={"agent_id": run_id}, query={})
        response = await run_views.api_spawn_status(cast(Any, request))
        return json.loads(response.body)

    floored, normal = await _status("run-s1"), await _status("run-s2")
    assert [item["title"] for item in floored["approvals"]] == ["execute_bash: ls"]
    assert "approvals" not in normal
    for run in runs:
        for item in hub_approvals.pending_for(run):
            hub_approvals.resolve(run, str(item["id"]), False)
    answers = await await_until(asyncio.gather(*waits), what="the two approval waits")
    assert answers == [False, False]


@pytest.mark.parametrize(
    ("backend", "enforceable"),
    # KAS takes the derived spec over the wire and raised a permission request
    # for every tool under it (measured for ACP_BACKENDS_SIDE_READONLY), so it
    # carries the floor; the floor reads that set rather than its own list.
    [("", True), ("claude", False), ("codex", False), ("kas", True), ("opencode", False)],
)
def test_only_a_harness_the_derived_spec_controls_carries_the_floor(
    monkeypatch: pytest.MonkeyPatch, backend: str, enforceable: bool
) -> None:
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.subagent_manager import hub_approvals

    monkeypatch.setattr(
        KiroCrewConfig,
        "load",
        classmethod(lambda _cls: SimpleNamespace(agent=SimpleNamespace(acp_backend=backend))),
    )
    assert hub_approvals.floor_enforceable() is enforceable


def test_an_unreadable_config_cannot_carry_the_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.subagent_manager import hub_approvals

    def _broken(_cls):
        raise OSError("config.json unreadable")

    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(_broken))
    assert hub_approvals.floor_enforceable() is False


@pytest.mark.asyncio
@pytest.mark.parametrize("enforceable", [True, False])
async def test_the_version_route_advertises_the_floor_only_where_it_holds(
    monkeypatch: pytest.MonkeyPatch, enforceable: bool
) -> None:
    from kiro_crew.dashboard.handlers.core import api_version

    monkeypatch.setattr(
        "kiro_crew.subagent_manager.hub_approvals.floor_enforceable", lambda: enforceable
    )
    payload = json.loads((await api_version(cast(Any, object()))).body)
    listed = set(payload["spawn_enforces"])
    assert {"memory_mode", "run_id"} <= listed
    assert ({"approval_floor", "approval_relay"} <= listed) is enforceable
    assert not ({"approval_floor", "approval_relay"} & listed) or enforceable


@pytest.mark.asyncio
async def test_the_version_route_reads_the_floor_off_the_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The route needs no login; the config load behind the floor check must not block the loop."""
    import threading

    from kiro_crew.dashboard.handlers.core import api_version

    loop_thread = threading.get_ident()
    seen: list[int] = []

    def _probe() -> bool:
        seen.append(threading.get_ident())
        return True

    monkeypatch.setattr("kiro_crew.subagent_manager.hub_approvals.floor_enforceable", _probe)
    await api_version(cast(Any, object()))
    assert seen and loop_thread not in seen


@pytest.mark.asyncio
async def test_a_floored_run_keeps_only_its_templates_pre_tool_hooks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew.agent_sdk import spec_hooks
    from kiro_crew.hooks import HOOK_EVENT_POST_TOOL_USE, HOOK_EVENT_PRE_TOOL_USE, ScriptHook

    deny = ScriptHook(name="deny-rm", event=HOOK_EVENT_PRE_TOOL_USE, command="exit 2")
    audit = ScriptHook(name="audit", event=HOOK_EVENT_POST_TOOL_USE, command="true")
    source = {"name": "kirocrew", "hooks": {"preToolUse": [{"command": "exit 2"}]}}
    seen: list[tuple[str, object]] = []

    def convert(agent_id: str, spec: object) -> tuple[tuple[ScriptHook, ...], int]:
        seen.append((agent_id, spec))
        return (deny, audit), 0

    def re_resolved(*_a: object) -> object:
        raise AssertionError("the floored hooks must come from the derivation's source")

    monkeypatch.setattr(spec_hooks, "_convert", convert)
    monkeypatch.setattr(spec_hooks, "crew_fired_spec_hooks", re_resolved)
    gated = await spec_hooks.floored_spec_hooks("kirocrew", "/work/repo", source)
    assert seen == [("kirocrew", source)]
    assert [hook.name for hook in gated.hooks] == ["deny-rm"]
    assert gated.gated is True and gated.unreadable is False

    def unconvertible(*_a: object) -> object:
        raise ValueError("hooks did not parse")

    monkeypatch.setattr(spec_hooks, "_convert", unconvertible)
    blocked = await spec_hooks.floored_spec_hooks("kirocrew", None, source)
    assert blocked.unreadable is True and blocked.gated is True
    sourceless = await spec_hooks.floored_spec_hooks("kirocrew", None, None)
    assert sourceless.unreadable is True and sourceless.gated is True


@pytest.mark.asyncio
async def test_a_floored_project_agents_hooks_follow_the_trust_verdict(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """The derivation may read a PROJECT spec. Its hooks are commands from the
    checkout, so an untrusted project refuses every request (no hook runs, and the
    floor does not weaken to "no hooks"); a trusted one fires its PreToolUse deny."""
    from kiro_crew.acp import session_mcp
    from kiro_crew.agent_sdk import spec_hooks
    from kiro_crew.hooks import HOOK_EVENT_PRE_TOOL_USE

    project_spec = {
        "name": "kirocrew",
        "hooks": {"preToolUse": [{"matcher": "*", "command": "exit 2"}]},
    }
    converted: list[object] = []
    real_convert = spec_hooks._convert

    def tracking_convert(agent_id: str, spec: object) -> object:
        converted.append(spec)
        return real_convert(agent_id, spec)

    monkeypatch.setattr(spec_hooks, "_convert", tracking_convert)
    monkeypatch.setattr(session_mcp, "trusted_project_agent_spec", lambda *_a: (False, None))
    refused = await spec_hooks.floored_spec_hooks(
        "kirocrew", str(tmp_path), project_spec, project_source=str(tmp_path)
    )
    assert refused.hooks == [] and refused.unreadable is True and refused.gated is True
    assert converted == []

    monkeypatch.setattr(session_mcp, "trusted_project_agent_spec", lambda *_a: (True, project_spec))
    gated = await spec_hooks.floored_spec_hooks(
        "kirocrew", str(tmp_path), project_spec, project_source=str(tmp_path)
    )
    assert gated.unreadable is False
    assert [hook.event for hook in gated.hooks] == [HOOK_EVENT_PRE_TOOL_USE]
    assert gated.hooks[0].command == "exit 2"


@pytest.mark.asyncio
async def test_a_rejected_remote_spawn_sends_nothing(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents.data_home", lambda: tmp_path)
    events: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "kiro_crew.sel.sel",
        lambda: SimpleNamespace(log_tool_invocation=lambda **kw: events.append(kw)),
    )
    instances = _Instances([])
    manager = _Manager()
    manager.approve_spawns = False
    service = RemoteSubagentService(
        SimpleNamespace(instances_manager=instances), manager  # type: ignore[arg-type]
    )
    monkeypatch.setattr(service, "_build_project_archive", _no_snapshot)

    with pytest.raises(RemoteSubagentError) as caught:
        await _spawn_placed(service, **_spawn_kwargs(cwd=""))

    assert caught.value.code == "remote_spawn_rejected"
    assert instances.calls == []  # no snapshot upload, no peer spawn
    [(request_id, label, parent)] = manager.spawn_approvals
    assert request_id.startswith("spawn:") and parent == "dashboard:chat-1"
    assert "remote crew crew-a" in label and "secret plan" in label
    [info] = manager.external_agents
    assert info.done is True and manager.reported == [info]
    assert [e["outcome"] for e in events] == ["rejected_spawn"]
    await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("hooks", "sources", "asked", "floor"),
    [
        # GPT 6.1 on 0a4dfc61a8: auto-approved child TOOLS loosen the floor only;
        # the spawn still waits for the person local admission would ask.
        (SimpleNamespace(auto_approve_subagent_tools=True), (), True, ""),
        (SimpleNamespace(), ("subagent",), True, ""),
        # The spawn grant local admission reads skips the question, and the
        # child's tools stay floored.
        (SimpleNamespace(auto_approve_subagent_spawn=True), (), False, "interactive"),
    ],
    ids=["tools-hook-only", "subagent-source-only", "spawn-hook"],
)
async def test_only_a_spawn_grant_skips_the_remote_spawn_approval(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    hooks: object,
    sources: tuple[str, ...],
    asked: bool,
    floor: str,
) -> None:
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents.data_home", lambda: tmp_path)
    instances = _Instances([])
    manager = _Manager()
    manager.approve_spawns = False
    setattr(manager, "_ctx_builder", SimpleNamespace(hooks=hooks))
    service = RemoteSubagentService(
        SimpleNamespace(instances_manager=instances), manager  # type: ignore[arg-type]
    )
    monkeypatch.setattr(service, "_build_project_archive", _no_snapshot)
    placed = AsyncMock()
    monkeypatch.setattr(service, "_place", placed)

    kwargs = {**_spawn_kwargs(cwd=""), "auto_approve_sources": sources}
    if asked:
        with pytest.raises(RemoteSubagentError) as caught:
            await _spawn_placed(service, **kwargs)
        assert caught.value.code == "remote_spawn_rejected"
        assert len(manager.spawn_approvals) == 1
        placed.assert_not_awaited()  # neither the snapshot nor the task left
        [info] = manager.external_agents
    else:
        info = await _spawn_placed(service, **kwargs)
        assert manager.spawn_approvals == []
        placed.assert_awaited_once()
    assert info.approval_floor == floor
    await service.close()


@pytest.mark.asyncio
async def test_the_receipt_does_not_wait_for_the_person(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """spawn_run's HTTP call is bounded, so the run is listed as awaiting approval
    at once, as a local spawn is, and placed only after the answer."""
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents.data_home", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents._POLL_SECONDS", 0.0)
    instances = _Instances(
        [(200, {"id": "peer01"}), (200, {"done": True, "turns": 1, "result": "ok"})]
    )
    manager = _Manager()
    answer = asyncio.Event()

    async def person(request_id: str, label: str, parent: str) -> bool:
        await answer.wait()
        return True

    manager._on_spawn_approval = person  # type: ignore[method-assign]
    service = RemoteSubagentService(
        SimpleNamespace(instances_manager=instances), manager  # type: ignore[arg-type]
    )
    info = await service.spawn(**_spawn_kwargs())
    assert info._awaiting_approval is True and instances.calls == []
    assert manager.external_agents == [info]
    state = json.loads(next((tmp_path / "subagents" / "remote").glob("*/state.json")).read_text())
    assert state["approved"] is False

    answer.set()
    await await_until(service._approvals[info.id], what=f"placement of {info.id}")
    assert instances.calls[0][1:3] == ("POST", "api/spawn")
    await _settle_monitor(service, info.id)
    assert info.done is True and info.error == ""
    await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("peer_ran_it", [True, False], ids=["peer-ran-it", "never-started"])
async def test_an_approved_spawn_whose_answer_and_withdrawal_are_lost_is_reconciled(
    tmp_path, monkeypatch: pytest.MonkeyPatch, peer_ran_it: bool
) -> None:
    """After approval, the peer may accept the run while its answer and the
    withdrawing DELETE are both lost. The record was kept for reconciliation, so
    the run must stay live under the monitor, not be persisted as done: a done
    record is never reconciled and the peer's result would never arrive."""
    from kiro_crew.dashboard import remote_subagents

    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents.data_home", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents._POLL_SECONDS", 0.0)
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents._RETRY_SECONDS", 0.0)
    polls: list[object] = (
        [(200, {"done": True, "turns": 1, "result": "ok"})]
        if peer_ran_it
        else [(404, {})] * remote_subagents._MAX_NOT_FOUND_POLLS
    )
    instances = _Instances([OSError("answer lost"), OSError("down"), *polls])  # type: ignore[list-item]
    manager = _Manager()
    service = RemoteSubagentService(
        SimpleNamespace(instances_manager=instances), manager  # type: ignore[arg-type]
    )
    info = await service.spawn(**_spawn_kwargs())
    runner = service._approvals.get(info.id)
    if runner is not None:
        await await_until(runner, what=f"placement of {info.id}")

    minted = instances.calls[0][3]["run_id"]  # type: ignore[index]
    assert instances.calls[1][1:3] == ("DELETE", f"api/spawn/{minted}")
    # The monitor, not the placement failure, ends the run: wait on its outcome.
    await async_wait_until(lambda: info.done, describe=lambda: instances.calls)
    assert len(instances.calls) > 2, "the kept record must be reconciled by polling the peer"
    assert instances.calls[2][1:3] == ("GET", f"api/spawn/{minted}")
    if peer_ran_it:
        assert info.error == ""
    else:
        assert info.error == remote_subagents._NEVER_STARTED
    await service.close()


@pytest.mark.asyncio
async def test_a_hub_without_an_approval_surface_refuses_a_normal_parents_remote_spawn(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents.data_home", lambda: tmp_path)
    instances = _Instances([])
    manager = _Manager()
    manager._on_spawn_approval = None  # type: ignore[assignment]
    service = RemoteSubagentService(
        SimpleNamespace(instances_manager=instances), manager  # type: ignore[arg-type]
    )
    with pytest.raises(RemoteSubagentError) as caught:
        await service.spawn(**_spawn_kwargs())
    assert caught.value.code == "remote_spawn_unapproved" and caught.value.status == 403
    assert instances.calls == [] and manager.external_agents == []
    await service.close()


@pytest.mark.asyncio
async def test_a_stop_during_the_approval_wait_is_local(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents.data_home", lambda: tmp_path)
    instances = _Instances([])
    manager = _Manager()

    async def never(*_args: object) -> bool:
        await asyncio.Event().wait()
        return True

    manager._on_spawn_approval = never  # type: ignore[method-assign]
    service = RemoteSubagentService(
        SimpleNamespace(instances_manager=instances), manager  # type: ignore[arg-type]
    )
    info = await service.spawn(**_spawn_kwargs())
    assert await service.cancel(info) is True
    assert info.done is True and info.user_stopped is True
    assert "nothing was sent" in info.error
    assert instances.calls == [] and not service._approvals
    await service.close()


@pytest.mark.asyncio
async def test_a_rejected_spawns_failed_terminal_write_is_retried_until_committed(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Persist before publish: a rejection whose terminal write fails must not
    leave the run pending forever (its parent and batch wait on it)."""
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents.data_home", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents._RETRY_SECONDS", 0.0)
    monkeypatch.setattr(
        "kiro_crew.sel.sel", lambda: SimpleNamespace(log_tool_invocation=lambda **kw: None)
    )
    instances = _Instances([])
    manager = _Manager()
    manager.approve_spawns = False
    service = RemoteSubagentService(
        SimpleNamespace(instances_manager=instances), manager  # type: ignore[arg-type]
    )
    attempts = _failing_terminal_write(service, failures=1)
    info = await service.spawn(**_spawn_kwargs())
    await await_until(service._approvals[info.id], what=f"placement of {info.id}")
    # The retry, not the rejection itself, commits the ending.
    await async_wait_until(lambda: info.done, describe=lambda: attempts)
    assert attempts == [False, True]
    assert info.done is True and info.error_code == "remote_spawn_rejected"
    assert manager.reported == [info] and instances.calls == []
    await service.close()


@pytest.mark.asyncio
async def test_a_stop_during_the_approval_wait_is_acknowledged_once_committed(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents.data_home", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents._RETRY_SECONDS", 0.0)
    instances = _Instances([])
    manager = _Manager()

    async def never(*_args: object) -> bool:
        await asyncio.Event().wait()
        return True

    manager._on_spawn_approval = never  # type: ignore[method-assign]
    service = RemoteSubagentService(
        SimpleNamespace(instances_manager=instances), manager  # type: ignore[arg-type]
    )
    attempts = _failing_terminal_write(service, failures=1)
    info = await service.spawn(**_spawn_kwargs())
    assert await service.cancel(info) is False, "an uncommitted stop is not acknowledged"
    assert info.done is False
    await async_wait_until(lambda: info.done, describe=lambda: attempts)
    assert attempts == [False, True]
    assert info.done is True and info.user_stopped is True
    assert instances.calls == []
    await service.close()


@pytest.mark.asyncio
async def test_a_stop_while_the_run_is_placed_never_starts_it_on_the_peer(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Opus: a stop between approval and the POST sent a DELETE for a name the peer
    had never seen (404, an error to the user) and the POST then started the run."""
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents.data_home", lambda: tmp_path)
    instances = _Instances([])
    manager = _Manager()
    service = RemoteSubagentService(
        SimpleNamespace(instances_manager=instances), manager  # type: ignore[arg-type]
    )
    info, release = await _approved_spawn_held_at(
        service, monkeypatch, {"delivered": False, "accepted": False}
    )
    assert await service.cancel(info) is True
    release.set()
    await await_until(service._approvals[info.id], what=f"placement of {info.id}")
    assert instances.calls == [], "neither a POST nor a DELETE may reach the peer"
    assert info.done is True and info.user_stopped is True
    assert info.error_code == "remote_spawn_stopped"
    await service.close()


@pytest.mark.asyncio
async def test_a_stop_after_the_post_left_stops_the_run_on_the_peer(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents.data_home", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents._POLL_SECONDS", 0.0)
    instances = _Instances(
        [
            (200, {"id": "peer01"}),
            (200, {"cancelled": True}),
            (200, {"done": True, "stopped": True, "result": "stopped"}),
        ]
    )
    manager = _Manager()
    service = RemoteSubagentService(
        SimpleNamespace(instances_manager=instances), manager  # type: ignore[arg-type]
    )
    info, release = await _approved_spawn_held_at(service, monkeypatch, {"delivered": False})
    assert await service.cancel(info) is True
    # Committed before it was acknowledged, so a restart still knows to stop it.
    state = json.loads((tmp_path / "subagents" / "remote" / info.id / "state.json").read_text())
    assert state["stopped"] is True
    release.set()
    await await_until(service._approvals[info.id], what=f"placement of {info.id}")
    assert [call[1:3] for call in instances.calls[:2]] == [
        ("POST", "api/spawn"),
        ("DELETE", "api/spawn/peer01"),
    ]
    await _settle_monitor(service, info.id)
    assert info.done is True and info.user_stopped is True
    await service.close()


class _HeldPost(_Instances):
    """Holds the spawn POST until released, so a stop lands while it is out."""

    def __init__(self, responses: list[Any]) -> None:
        super().__init__(responses)
        self.post_out = asyncio.Event()
        self.release_post = asyncio.Event()

    @asynccontextmanager
    async def proxy_request(self, instance_id: str, method: str, path: str, **kwargs: Any):
        if method == "POST" and path == "api/spawn" and not self.post_out.is_set():
            self.post_out.set()
            await self.release_post.wait()
        async with super().proxy_request(instance_id, method, path, **kwargs) as response:
            yield response


@pytest.mark.asyncio
async def test_a_stop_during_a_placement_whose_answer_is_lost_is_sent_again(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Opus: a stop acknowledged while the POST was out, when the POST's answer and
    the withdrawing DELETE are both lost, left the kept run under a monitor that
    never sent the stop again, so a run the peer did start kept running."""
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents.data_home", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents._POLL_SECONDS", 0.0)
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents._RETRY_SECONDS", 0.0)
    instances = _HeldPost(
        [
            OSError("answer lost"),
            OSError("down"),
            (200, {"cancelled": True}),
            (200, {"done": True, "stopped": True, "result": "stopped"}),
        ]  # type: ignore[list-item]
    )
    manager = _Manager()
    service = RemoteSubagentService(
        SimpleNamespace(instances_manager=instances), manager  # type: ignore[arg-type]
    )
    info = await service.spawn(**_spawn_kwargs())
    await async_wait_until(instances.post_out.is_set, describe=lambda: instances.calls)
    assert await service.cancel(info) is True
    instances.release_post.set()
    await await_until(service._approvals[info.id], what=f"placement of {info.id}")
    await _settle_monitor(service, info.id)
    minted = instances.calls[0][3]["run_id"]  # type: ignore[index]
    assert [call[1:3] for call in instances.calls[:4]] == [
        ("POST", "api/spawn"),
        ("DELETE", f"api/spawn/{minted}"),
        ("DELETE", f"api/spawn/{minted}"),
        ("GET", f"api/spawn/{minted}"),
    ]
    assert info.done is True and info.user_stopped is True
    await service.close()


@pytest.mark.asyncio
async def test_a_pending_admission_does_not_count_toward_a_never_started_run(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GPT: a peer still admitting the run answered 404 to the hub's polls, and three
    of them ended a run the peer was about to start. Its 409 resets the count."""
    from kiro_crew.dashboard import remote_subagents

    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents.data_home", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents._POLL_SECONDS", 0.0)
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents._RETRY_SECONDS", 0.0)
    pending = (409, {"code": "spawn_admission_pending"})
    polls: list[object] = [
        *[(404, {})] * (remote_subagents._MAX_NOT_FOUND_POLLS - 1),
        pending,
        *[(404, {})] * (remote_subagents._MAX_NOT_FOUND_POLLS - 1),
        (200, {"done": True, "turns": 1, "result": "ok"}),
    ]
    instances = _Instances([OSError("answer lost"), OSError("down"), *polls])  # type: ignore[list-item]
    manager = _Manager()
    service = RemoteSubagentService(
        SimpleNamespace(instances_manager=instances), manager  # type: ignore[arg-type]
    )
    info = await service.spawn(**_spawn_kwargs())
    runner = service._approvals.get(info.id)
    if runner is not None:
        await await_until(runner, what=f"placement of {info.id}")
    await async_wait_until(lambda: info.done, describe=lambda: instances.calls)
    assert info.error == "" and info.result == "ok"
    await service.close()


def test_a_floored_runs_session_identity_write_records_its_floor() -> None:
    """GPT: the relaunch guard read only the in-memory record, which a restart
    drops. The floor rides in the same state write as the session id a cold
    continuation resumes from, so a resumable run always carries it."""
    import inspect

    # The spawn-time state writes live in turn_session.persist_spawn_records.
    from kiro_crew.subagent_manager import turn_session

    source = inspect.getsource(turn_session.persist_spawn_records)
    _, after = source.split('"session_id": str(getattr(info, "_session_id", "")),', 1)
    write = after.split("_write_state_off_loop", 1)[0]
    assert 'state_update["approval_floor"] = info.approval_floor' in write


@pytest.mark.asyncio
async def test_a_hub_request_too_long_to_show_whole_is_refused_not_cut() -> None:
    """GPT: a command padded past the relay bound showed the person only its
    head, so the approval covered a suffix nobody saw. It is refused instead,
    and anything within the bound is relayed whole."""
    from kiro_crew.subagent_manager import hub_approvals

    padded = "ls" + " " * hub_approvals._MAX_INPUT_CHARS + "; : > src/main.py"
    titled = "x" * (hub_approvals._MAX_TITLE_CHARS + 1)
    for title, tool_input in (("execute_bash", padded), (titled, "")):
        event = SimpleNamespace(title=title, tool_input=tool_input, tool_purpose="")
        # Refused at once, never parked: a parked request would wait the full
        # hub window here instead of answering.
        refusal = asyncio.create_task(hub_approvals.ask_hub("run-p9", event))
        assert await await_until(refusal, what="the refusal") is False
        assert hub_approvals.pending_for("run-p9") == []

    whole = "y" * 5000
    event = SimpleNamespace(title="execute_bash", tool_input=whole, tool_purpose="")
    waiting = asyncio.create_task(hub_approvals.ask_hub("run-p10", event))
    await async_wait_until(
        lambda: hub_approvals.pending_for("run-p10"), describe=lambda: "the parked request"
    )
    [listed] = hub_approvals.pending_for("run-p10")
    assert listed["tool_input"] == whole
    assert hub_approvals.resolve("run-p10", listed["id"], False) is True
    assert await waiting is False


def test_an_unreadable_result_is_not_restored_as_an_empty_success(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GPT: an OSError reading a finished run's result.txt became "", which the
    restore published as an empty success and acknowledged, losing the result."""
    from kiro_crew.dashboard import remote_subagents

    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents.data_home", lambda: tmp_path)
    info = _remote_record(done=True)
    RemoteSubagentService._persist_info(info, delivered=False, done=True)
    result = remote_subagents._records_root() / info.id / "result.txt"
    result.write_text("the answer")
    real_read = Path.read_text

    def unreadable(self: Path, *args: Any, **kwargs: Any) -> str:
        if self.name == "result.txt":
            raise OSError("bad sector")
        return real_read(self, *args, **kwargs)

    with monkeypatch.context() as patched:
        patched.setattr(Path, "read_text", unreadable)
        assert RemoteSubagentService._load_records() == []
    [(restored, delivered)] = RemoteSubagentService._load_records()
    assert restored.result == "the answer" and delivered is False


@pytest.mark.asyncio
async def test_a_lost_approval_is_committed_before_it_is_published(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GPT: a restart during the approval wait derived a terminal at load and
    published it before writing it; a failed write left a published ending that
    the record did not hold. The write now comes first."""
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents.data_home", lambda: tmp_path)
    waiting = _remote_record()
    RemoteSubagentService._persist_info(waiting, delivered=False, accepted=False, approved=False)

    manager = _Manager()
    service = RemoteSubagentService(
        SimpleNamespace(instances_manager=_Instances([])), manager  # type: ignore[arg-type]
    )

    def refuse_terminal(info: SubagentInfo, **kw: Any) -> None:
        if kw.get("done") is True:
            raise OSError("disk full")

    service._persist_info = refuse_terminal  # type: ignore[method-assign]
    await service.ensure_restored()
    assert manager.external_agents == [] and manager.reported == []
    await service.close()

    manager = _Manager()
    service = RemoteSubagentService(
        SimpleNamespace(instances_manager=_Instances([])), manager  # type: ignore[arg-type]
    )
    await service.ensure_restored()
    [published] = manager.reported
    assert published.error_code == "remote_spawn_approval_lost"
    state = json.loads((tmp_path / "subagents" / "remote" / waiting.id / "state.json").read_text())
    assert state["done"] is True
    await service.close()


@pytest.mark.asyncio
async def test_an_unconfirmed_stop_after_placement_is_sent_again(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Opus: a stop accepted during placement whose peer DELETE failed was never
    retried, so the run went on while the stop read as done. The monitor re-sends
    it until the peer confirms."""
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents.data_home", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents._POLL_SECONDS", 0.0)
    instances = _Instances(
        [
            (200, {"id": "peer01"}),
            (503, {}),
            (200, {"cancelled": True}),
            (200, {"done": True, "stopped": True, "result": "stopped"}),
        ]
    )
    manager = _Manager()
    service = RemoteSubagentService(
        SimpleNamespace(instances_manager=instances), manager  # type: ignore[arg-type]
    )
    info, release = await _approved_spawn_held_at(service, monkeypatch, {"delivered": False})
    assert await service.cancel(info) is True
    release.set()
    await await_until(service._approvals[info.id], what=f"placement of {info.id}")
    await _settle_monitor(service, info.id)
    assert [call[1:3] for call in instances.calls] == [
        ("POST", "api/spawn"),
        ("DELETE", "api/spawn/peer01"),
        ("DELETE", "api/spawn/peer01"),
        ("GET", "api/spawn/peer01"),
    ]
    assert info.done is True and not service._unconfirmed_stops
    await service.close()


@pytest.mark.asyncio
async def test_a_silent_remote_run_stays_silent_across_a_restart(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Opus: the remote branch dropped ``silent``, so a silent spawn still notified."""
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents.data_home", lambda: tmp_path)
    service = RemoteSubagentService(
        SimpleNamespace(instances_manager=_Instances([])), _Manager()  # type: ignore[arg-type]
    )

    async def never(*_args: object) -> bool:
        await asyncio.Event().wait()
        return True

    service._manager._on_spawn_approval = never  # type: ignore[attr-defined]
    info = await service.spawn(**_spawn_kwargs(), silent=True)
    assert info.silent is True
    [(restored, _delivered)] = RemoteSubagentService._load_records()
    assert restored.silent is True
    await service.close()


@pytest.mark.asyncio
async def test_a_remote_run_waiting_for_its_spawn_approval_is_parked_not_live(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A parent end and Stop-all leave a run parked on its spawn approval alone, so
    the person's pending decision is not answered for them. A remote run must read
    as parked the same way while it waits, and as executing once approved."""
    from kiro_crew.subagent import _parked_at_spawn_approval

    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents.data_home", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents._POLL_SECONDS", 0.0)
    instances = _Instances(
        [(200, {"id": "peer01"}), (200, {"done": True, "turns": 1, "result": "ok"})]
    )
    manager = _Manager()
    answer = asyncio.Event()

    async def person(request_id: str, label: str, parent: str) -> bool:
        await answer.wait()
        return True

    manager._on_spawn_approval = person  # type: ignore[method-assign]
    service = RemoteSubagentService(
        SimpleNamespace(instances_manager=instances), manager  # type: ignore[arg-type]
    )
    info = await service.spawn(**_spawn_kwargs())
    assert _parked_at_spawn_approval(info) is True

    answer.set()
    await await_until(service._approvals[info.id], what=f"placement of {info.id}")
    assert _parked_at_spawn_approval(info) is False
    await _settle_monitor(service, info.id)
    await service.close()


@pytest.mark.asyncio
async def test_a_restart_during_the_approval_wait_ends_the_run_as_never_sent(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.dashboard import remote_subagents

    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents.data_home", lambda: tmp_path)
    waiting = SubagentInfo(
        id="abcdef12",
        task="review",
        parent_session_key="dashboard:chat-1",
        executor="remote",
        instance_id="crew-a",
        remote_id="0123456789abcdef",
    )
    RemoteSubagentService._persist_info(waiting, delivered=False, accepted=False, approved=False)
    instances = _Instances([])
    manager = _Manager()
    service = RemoteSubagentService(
        SimpleNamespace(instances_manager=instances), manager  # type: ignore[arg-type]
    )
    await service.ensure_restored()
    restored = manager.get("abcdef12")
    assert restored is not None and restored.done is True
    assert restored.error == remote_subagents._APPROVAL_LOST
    assert manager.reported == [restored] and instances.calls == []
    await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("advertised", "sent"),
    [
        (["memory_mode"], False),
        (["run_id"], True),
    ],
    ids=["ignores-run-id", "keeps-run-id"],
)
async def test_every_remote_run_needs_a_crew_that_keeps_its_name(
    advertised: list[str], sent: bool
) -> None:
    """A run with nothing to tighten is still refused by a crew without ``run_id``."""
    instances = _Instances([])

    async def peer(_instance_id: str, _path: str) -> tuple[bool, object]:
        return True, {"version": kiro_crew.__version__, "spawn_enforces": advertised}

    instances.peer_capability = peer  # type: ignore[method-assign]
    if sent:
        await remote_subagents._ensure_peer_supports(
            instances, "crew-a", memory_mode="", approval_floor=""
        )
        return
    with pytest.raises(RemoteSubagentError) as refused:
        await remote_subagents._ensure_peer_supports(
            instances, "crew-a", memory_mode="", approval_floor=""
        )
    assert refused.value.code == "remote_peer_unenforced"


@pytest.mark.parametrize(
    ("default", "run_backend", "enforceable"),
    [
        ("", "claude", False),
        ("", "codex", False),
        ("claude", "", True),
        ("claude", None, False),
        ("", None, True),
    ],
)
def test_the_floor_is_checked_against_the_runs_own_backend(
    monkeypatch: pytest.MonkeyPatch, default: str, run_backend: str | None, enforceable: bool
) -> None:
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.subagent_manager import hub_approvals

    loaded = SimpleNamespace(agent=SimpleNamespace(acp_backend=default))
    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda _cls: loaded))
    assert hub_approvals.floor_enforceable(run_backend) is enforceable


@pytest.mark.asyncio
async def test_a_committed_stop_is_sent_again_after_a_restart(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GPT: the unconfirmed-stop retry lived only in memory, so a hub restart
    resumed polling a run it had acknowledged as stopped without stopping it."""
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents.data_home", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents._POLL_SECONDS", 0.0)
    stopped = _remote_record(user_stopped=True)
    RemoteSubagentService._persist_info(stopped, delivered=False)
    instances = _Instances(
        [(200, {"cancelled": True}), (200, {"done": True, "stopped": True, "result": "stopped"})]
    )
    service = RemoteSubagentService(
        SimpleNamespace(instances_manager=instances), _Manager()  # type: ignore[arg-type]
    )
    await service.ensure_restored()
    await _settle_monitor(service, stopped.id)
    assert [call[1:3] for call in instances.calls] == [
        ("DELETE", f"api/spawn/{stopped.remote_id}"),
        ("GET", f"api/spawn/{stopped.remote_id}"),
    ]
    await service.close()


@pytest.mark.asyncio
async def test_an_unconfirmed_stop_outlives_the_unreachable_deadline(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GPT 6.1: the 30-minute unreachable deadline closed a run whose stop the
    crew had not confirmed. The run became terminal, so neither the monitor nor a
    restart sent the stop again, and the crew could keep running it."""
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents.data_home", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents._POLL_SECONDS", 0.0)
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents._RETRY_SECONDS", 0.0)
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents._UNREACHABLE_DEADLINE_SECONDS", 0.0)
    stopped = _remote_record(user_stopped=True)
    RemoteSubagentService._persist_info(stopped, delivered=False)
    outage: list[tuple[int, dict[str, object]] | BaseException] = [
        ConnectionError("tunnel down")
    ] * 4
    instances = _Instances(
        [
            *outage,
            (200, {"cancelled": True}),
            (200, {"done": True, "stopped": True, "result": "stopped"}),
        ]
    )
    manager = _Manager()
    service = RemoteSubagentService(
        SimpleNamespace(instances_manager=instances), manager  # type: ignore[arg-type]
    )
    await service.ensure_restored()
    await _settle_monitor(service, stopped.id)
    info = manager._external_agents[stopped.id]
    assert "unreachable" not in (info.error or "")
    assert info.done is True and info.user_stopped is True
    assert [call[1] for call in instances.calls] == ["DELETE", "GET"] * 3
    await service.close()


@pytest.mark.asyncio
async def test_a_run_closed_at_the_unreachable_deadline_is_stopped_once_its_crew_is_back(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GPT 6.1: the deadline closed an accepted run with no stop pending. The
    terminal shadow left monitoring, restore, the prune's keep-list and every
    stop path, so a worker still running on the crew was never stopped. The run
    now owes its peer a stop, kept across a restart and a dismissal, until the
    peer confirms it."""
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents.data_home", lambda: tmp_path)
    # A long retry: the crew misses the first stop, and the restart sends the next.
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents._RETRY_SECONDS", 60.0)
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents._UNREACHABLE_DEADLINE_SECONDS", 0.0)
    live = _remote_record()
    RemoteSubagentService._persist_info(live, delivered=False)
    marker = tmp_path / "subagents" / "remote" / live.id / remote_subagents._STOP_OWED
    instances = _Instances([ConnectionError("tunnel down")] * 2)
    manager = _Manager()
    service = RemoteSubagentService(
        SimpleNamespace(instances_manager=instances), manager  # type: ignore[arg-type]
    )
    await service.ensure_restored()
    await _settle_monitor(service, live.id)
    info = manager._external_agents[live.id]
    assert info.done is True and "unreachable" in info.error
    # The stop is owed: the closing GET is followed by a DELETE the crew misses.
    await async_wait_until(lambda: len(instances.calls) == 2, describe=lambda: instances.calls)
    await service.close()
    assert [call[1] for call in instances.calls] == ["GET", "DELETE"]
    assert marker.is_file()
    await asyncio.to_thread(service._persist_info, info, delivered=True, dismissed=True)
    assert RemoteSubagentService._prune_records(0, time.time() + 10) == []

    # A restart resumes the owed stop, dismissed or not, and the crew confirms it.
    back = _Instances([(404, {})])
    restarted = RemoteSubagentService(
        SimpleNamespace(instances_manager=back), _Manager()  # type: ignore[arg-type]
    )
    await restarted.ensure_restored()
    # The owed-stop task drops itself from ``_owed_stops`` once it finishes, and a
    # 404 can finish it before this line runs, so wait on what it leaves behind.
    await async_wait_until(
        lambda: not marker.exists() and live.id not in restarted._owed_stops,
        describe=lambda: (back.calls, marker.exists()),
    )
    assert [call[1:3] for call in back.calls] == [("DELETE", f"api/spawn/{live.remote_id}")]
    assert not marker.exists()
    assert RemoteSubagentService._prune_records(0, time.time() + 10) == [live.id]
    await restarted.close()


@pytest.mark.asyncio
async def test_a_stop_owed_to_a_crew_removed_from_the_registry_is_settled_as_moot(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Opus 5.5: a stop owed to a crew the operator deleted was retried every
    5 s forever, logging each time, and its record could never be pruned."""
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents.data_home", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents._RETRY_SECONDS", 0.0)
    owed = _remote_record(done=True)
    RemoteSubagentService._persist_info(owed, delivered=True)
    RemoteSubagentService._owe_stop(owed.id)
    marker = tmp_path / "subagents" / "remote" / owed.id / remote_subagents._STOP_OWED
    instances = _Instances([])
    registry = SimpleNamespace(get=lambda instance_id: None)
    service = RemoteSubagentService(
        SimpleNamespace(instances_manager=instances, instances_registry=registry),  # type: ignore[arg-type]
        _Manager(),  # type: ignore[arg-type]
    )
    await service.ensure_restored()
    await async_wait_until(
        lambda: not marker.exists() and owed.id not in service._owed_stops,
        describe=lambda: (instances.calls, marker.exists()),
    )
    assert instances.calls == []  # nothing is sent to a crew that is gone
    assert RemoteSubagentService._prune_records(0, time.time() + 10) == [owed.id]
    await service.close()


def test_a_private_runs_error_text_stays_off_disk(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GPT 6.1: a restricted run's timeout error can quote its last tool call,
    which is task text; it was written to ``state.json`` while the task was not."""
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents.data_home", lambda: tmp_path)
    error = "Timed out after 600s; last tool: execute_bash: grep merger-plan.txt"
    for run_id, mode in (
        ("abcdef31", "incognito"),
        ("abcdef32", "temporary"),
        ("abcdef33", "persistent"),
    ):
        info = _remote_record(
            id=run_id, task="merger-plan", memory_mode=mode, done=True, error=error
        )
        RemoteSubagentService._persist_info(info, delivered=False)

    def saved(run_id: str) -> dict:
        return json.loads((tmp_path / "subagents" / "remote" / run_id / "state.json").read_text())

    for run_id in ("abcdef31", "abcdef32"):
        assert "merger-plan" not in json.dumps(saved(run_id))
        assert saved(run_id)["error"] == remote_subagents._ERROR_NOT_RETAINED
    assert saved("abcdef33")["error"] == error


@pytest.mark.asyncio
async def test_the_peers_unexecuted_tool_warning_reaches_the_shadow_and_survives_a_restart(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GPT 6.1: the peer reports ``leaked_tool_call`` for a run that ended on a
    tool call written as prose; the hub dropped it, so the parent's completion
    report had no unexecuted-operation warning."""
    from kiro_crew.dashboard.remote_subagents_usage import _take_peer_usage

    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents.data_home", lambda: tmp_path)
    info = _remote_record(id="abcdef41")
    _take_peer_usage(info, {"done": True, "leaked_tool_call": True})
    assert info.leaked_tool_call is True
    info.done = True
    RemoteSubagentService._persist_info(info, delivered=True)
    manager = _Manager()
    service = RemoteSubagentService(
        cast(Any, SimpleNamespace(instances_manager=None)), cast(Any, manager)
    )
    await service.ensure_restored()
    restored = manager.get("abcdef41")
    assert restored is not None and restored.leaked_tool_call is True
    await service.close()


@pytest.mark.asyncio
async def test_a_finished_runs_duration_survives_a_restart(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GPT 6.1: the mapping kept no duration, so a restored terminal shadow
    reported zero in spawn status and in a replayed completion."""
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents.data_home", lambda: tmp_path)
    finished = _remote_record(done=True)
    finished.elapsed = 42.5
    RemoteSubagentService._persist_info(finished, delivered=True)
    ((restored, _delivered),) = RemoteSubagentService._load_records()
    assert restored.elapsed == 42.5


@pytest.mark.asyncio
async def test_an_auto_placed_run_is_listed_and_stoppable_while_its_post_is_out(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GPT 6.1: an auto-approved spawn joined the inventory only after the peer
    answered, so a Stop-all during a delayed POST found no remote target and the
    peer kept the run. It is listed while placed, and a stop then reaches the peer."""
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents.data_home", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents._POLL_SECONDS", 0.0)
    instances = _HeldPost(
        [
            (200, {"id": "peer01"}),
            (200, {"cancelled": True}),
            (200, {"done": True, "stopped": True, "result": "stopped"}),
        ]
    )
    manager = _Manager()
    manager._sessions = _Sessions(policy="auto")
    service = RemoteSubagentService(
        SimpleNamespace(instances_manager=instances), manager  # type: ignore[arg-type]
    )
    spawn = asyncio.create_task(service.spawn(**_spawn_kwargs()))
    await async_wait_until(instances.post_out.is_set, describe=lambda: instances.calls)
    (info,) = manager._external_agents.values()
    assert info.parent_session_key == "dashboard:chat-1" and not info.done
    assert await service.cancel(info) is True
    instances.release_post.set()
    assert await await_until(spawn, what="the auto spawn") is info
    assert [call[1:3] for call in instances.calls[:2]] == [
        ("POST", "api/spawn"),
        ("DELETE", "api/spawn/peer01"),
    ]
    await _settle_monitor(service, info.id)
    assert info.done is True and info.user_stopped is True
    await service.close()


@pytest.mark.asyncio
async def test_a_refused_auto_spawn_leaves_the_inventory_it_was_listed_in(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents.data_home", lambda: tmp_path)
    instances = _Instances([(409, {"error": "busy", "code": "remote_spawn_refused"})])
    manager = _Manager()
    manager._sessions = _Sessions(policy="auto")
    service = RemoteSubagentService(
        SimpleNamespace(instances_manager=instances), manager  # type: ignore[arg-type]
    )
    with pytest.raises(RemoteSubagentError) as refused:
        await service.spawn(**_spawn_kwargs())
    assert refused.value.code == "remote_spawn_refused"
    assert manager._external_agents == {}
    assert service._placing == set() and service._stops == set()
    await service.close()


@pytest.mark.asyncio
async def test_a_stop_during_the_upload_of_a_refused_spawn_leaves_no_record(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Opus: a stop during the project upload committed a stopped, not-done record
    before the first persist; when the upload then failed, the record stayed and a
    restart revived the refused spawn as a live run that later 'vanished'."""
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents.data_home", lambda: tmp_path)
    manager = _Manager()
    manager._sessions = _Sessions(policy="auto")
    service = RemoteSubagentService(
        SimpleNamespace(instances_manager=_Instances([])), manager  # type: ignore[arg-type]
    )
    uploading, fail_upload = asyncio.Event(), asyncio.Event()

    async def upload(*_args: object, **_kwargs: object) -> str:
        uploading.set()
        await fail_upload.wait()
        raise RemoteSubagentError("snapshot failed", code="remote_project_snapshot_failed")

    monkeypatch.setattr(service, "_sync_parent_project", upload)
    spawn = asyncio.create_task(service.spawn(**_spawn_kwargs(cwd="")))
    await await_until(uploading, what="the project upload")
    (info,) = manager._external_agents.values()
    assert await service.cancel(info) is True
    assert len(RemoteSubagentService._load_records()) == 1
    fail_upload.set()
    with pytest.raises(RemoteSubagentError) as refused:
        await await_until(spawn, what="the refused spawn")
    assert refused.value.code == "remote_project_snapshot_failed"
    assert RemoteSubagentService._load_records() == []
    assert manager._external_agents == {}
    await service.close()


@pytest.mark.asyncio
async def test_a_late_stop_commit_keeps_a_completion_delivered(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GPT 6.1: a stop whose DELETE failed after the monitor had acknowledged the
    completion committed ``delivered=False``, so a restart delivered it again."""
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents.data_home", lambda: tmp_path)
    service = RemoteSubagentService(
        SimpleNamespace(instances_manager=_Instances([])), _Manager()  # type: ignore[arg-type]
    )
    finished = _remote_record(done=True)
    RemoteSubagentService._persist_info(finished, delivered=True)
    assert await service._commit_stop(finished) is True
    ((restored, delivered),) = RemoteSubagentService._load_records()
    assert delivered is True and restored.user_stopped is True
    await service.close()


@pytest.mark.asyncio
async def test_a_relayed_request_reaches_the_parents_channel_with_the_floor() -> None:
    """GPT 6.1: relayed requests went only to the dashboard coordinator, so a Slack
    parent never saw them and the peer's wait ran out. They go through the
    gateway's tool-approval callback, the one a local run of that parent uses,
    marked with the interactive floor so only a person can approve them."""
    asked: list[tuple[object, str]] = []

    async def on_tool_approval(event: object, parent_session_key: str = "") -> bool:
        asked.append((event, parent_session_key))
        return True

    async def request_approval(*_args: object, **_kwargs: object) -> bool:
        raise AssertionError("the dashboard-only prompt is the fallback, not the first ask")

    instances = _Instances([(200, {"approved": True})])
    manager = _Manager()
    manager._on_tool_approval = on_tool_approval  # type: ignore[attr-defined]
    state = SimpleNamespace(instances_manager=instances, request_approval=request_approval)
    service = RemoteSubagentService(state, manager)  # type: ignore[arg-type]
    info = _floored_info()
    listed = [
        {"id": "b" * 16, "title": "execute_bash: ls", "tool_input": "ls", "tool_purpose": "list"}
    ]

    service._relay_approvals(info, listed)
    await await_until(asyncio.gather(*service._relays.values()), what="the approval relay")

    ((event, parent),) = asked
    assert parent == "dashboard:chat-1"
    assert getattr(event, "kind") == "permission_request"
    assert getattr(event, "request_id") == f"remote:{info.id}:{'b' * 16}"
    assert "execute_bash: ls" in getattr(event, "title")
    assert (getattr(event, "tool_input"), getattr(event, "tool_purpose")) == ("ls", "list")
    assert getattr(event, "approval_floor") == "interactive"
    assert instances.calls == [
        ("crew-a", "POST", f"api/spawn/peer01/approvals/{'b' * 16}", {"approved": True})
    ]
    await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(("sources", "expected"), [(("subagent",), ""), (("cron",), "interactive")])
async def test_a_hub_that_auto_approves_subagents_sends_no_floor(
    tmp_path, monkeypatch: pytest.MonkeyPatch, sources: tuple[str, ...], expected: str
) -> None:
    """Opus: with ``"subagent"`` in ``hooks.auto_approve_sources`` the hub's own
    tool callback fast-denies every floored request without asking anybody, so
    the floor must follow that posture, as a local child of the parent would."""
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents.data_home", lambda: tmp_path)
    instances = _Instances([(200, {"id": "peer01"})], connected=("crew-a",))
    service = RemoteSubagentService(
        cast(Any, SimpleNamespace(instances_manager=instances)), cast(Any, _Manager())
    )
    monkeypatch.setattr(service, "_monitor", AsyncMock())
    info = await _spawn_placed(service, **_spawn_kwargs(), auto_approve_sources=sources)
    body = instances.calls[0][3]
    assert isinstance(body, dict)
    assert body.get("approval_floor", "") == expected
    assert info.approval_floor == expected
    await service.close()


def test_the_spawn_route_hands_the_hubs_auto_approve_sources_to_the_floor() -> None:
    import inspect

    from kiro_crew.dashboard.messaging_api import spawn as spawn_route

    assert "auto_approve_sources=auto_approve_sources(remote_cfg)" in inspect.getsource(
        spawn_route._admit_spawn
    )
    read = remote_subagents.auto_approve_sources
    assert read(SimpleNamespace(hooks={"auto_approve_sources": ["subagent"]})) == ("subagent",)
    assert read(SimpleNamespace(hooks={"auto_approve_sources": "subagent"})) == ()
    assert read(SimpleNamespace(hooks=None)) == ()


@pytest.mark.asyncio
async def test_the_peers_usage_and_tool_evidence_survive_the_shadow_record(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GPT: ``_finish`` dropped the peer's ``credits`` and ``made_no_tool_calls``,
    so a run whose tools were pre-approved on the peer read as zero cost and as
    a run that called no tool."""
    monkeypatch.setattr("kiro_crew.dashboard.remote_subagents.data_home", lambda: tmp_path)
    service = RemoteSubagentService(
        cast(Any, SimpleNamespace(instances_manager=None)), cast(Any, _Manager())
    )
    info = _remote_record(id="abcdef12")
    assert info.made_no_tool_calls is True
    payload: dict[str, object] = {"done": True, "result": "ok", "outcome": "completed"}
    payload.update(credits=2.5, made_no_tool_calls=False)
    assert await service._finish(info, payload)
    assert info.credits == 2.5 and info.made_no_tool_calls is False
    [(restored, _delivered)] = RemoteSubagentService._load_records()
    assert restored.credits == 2.5 and restored.made_no_tool_calls is False

    # A value that is not a finite, non-negative number is not taken. 10**400
    # (GPT): an int no float holds must not raise out of the monitor's _finish.
    for bad in (True, -1, float("nan"), "3", 10**400):
        other = _remote_record(id="abcdef13")
        assert await service._finish(other, {"done": True, "result": "ok", "credits": bad})
        assert other.credits == 0.0 and other.made_no_tool_calls is True
    await service.close()
