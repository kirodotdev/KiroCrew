"""An app token is not the owner on the run routes, and an app's job never fires as a Bot.

``POST /api/spawn`` holds an app token to what the App Kit SDK holds an app's run to
(``apps.spawn_sdk``): a named agent, which the gate then proves is the app's own
because the route hands it the app; no parent session but the app's own; and a run
recorded under the app. The per-run routes (status, steer, retry, release, delete,
continue) and the live half of the listing reach only a run recorded under the
caller's app, with the 404 a caller gets for a run it may not see. The owner's own
requests, and an internal caller's session-owned reach, are as they were.

At fire time a job an app created does not collapse a Bot's name onto the Bot's
agent, workspace or crew identity, in a single run or an ``agent_sequence`` step,
and a job that captured a Bot's execution is refused. A row on the shared store
still collapses, and the owner's own job still runs as the Bot.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from handlers_messaging_coverage_helpers import _Req, _state
from member_memory_helpers import write_member_home
from test_cron_sequence_member_alias import _cron_callback

import kiro_crew.dashboard.handlers.messaging as messaging
from kiro_crew.config import loader
from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig, config_dir
from kiro_crew.config.sections import WorkspaceConfig
from kiro_crew.cron import CronJob
from kiro_crew.dashboard import slot_ownership
from kiro_crew.execution_context import ExecutionContext, MemoryStoreRef

# The real-gate batch tests drive SubagentManager.spawn; a memory-pressured runner
# would queue the spawn instead of admitting it.
pytestmark = pytest.mark.usefixtures("healthy_host_memory")

APP = "shop"
OTHER_APP = "other"
OWN_AGENT = "shop--helper"


def _denials(sel_calls: MagicMock) -> list[tuple[str, str]]:
    return [
        (call.kwargs["caller"], call.kwargs["operation"])
        for call in sel_calls.log_api_access.call_args_list
        if call.kwargs.get("outcome") == "denied"
    ]


@pytest.fixture
def sel_calls(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    import kiro_crew.sel as sel_mod

    recorder = MagicMock()
    monkeypatch.setattr(sel_mod, "sel", lambda: recorder)
    # The app-isolation rows go through slot_ownership, which binds sel at import.
    monkeypatch.setattr(slot_ownership, "sel", lambda: recorder)
    monkeypatch.setattr(messaging, "_sel", lambda: recorder)
    return recorder


def _run(**fields) -> SimpleNamespace:
    """A live run record with the fields the routes read."""
    base = dict(
        id="r1",
        task="do it",
        done=False,
        error="",
        error_code="",
        result="",
        started=1_700_000_000.0,
        turns=1,
        last_tool="",
        parent_session_key="",
        agent=OWN_AGENT,
        crew="",
        app="",
        memory_store="",
        user_stopped=False,
        outcome="",
        include_memory=True,
        include_lessons=True,
        include_project=True,
    )
    base.update(fields)
    return SimpleNamespace(**base)


def _manager(*runs: SimpleNamespace) -> MagicMock:
    manager = MagicMock(max_concurrent=4, _tasks={})
    manager.all_agents = list(runs)
    manager._agents = {run.id: run for run in runs}
    manager.get.side_effect = manager._agents.get
    manager.spawn.return_value = _run(id="new")
    return manager


class _Request(_Req):
    """The request double, plus the per-request cache the scope check writes."""

    def __setitem__(self, key: str, value: object) -> None:
        self._extra[key] = value


def _request(manager, body=None, *, app: str, run_id: str = "", slots=None) -> _Request:
    state = _state(subagents=manager, _slots=slots or {})
    match = {"agent_id": run_id} if run_id else None
    return _Request(state, body, match_info=match, extra={"app": app, "internal_auth": False})


@pytest.fixture
def persistent_parent(monkeypatch: pytest.MonkeyPatch) -> None:
    """The parent session's memory mode, which a slot double does not carry."""
    monkeypatch.setattr(
        messaging, "_spawn_request_memory_mode", AsyncMock(return_value="persistent")
    )


def _call(handler, request: _Request) -> web.Response:
    return asyncio.run(handler(request))


# ── POST /api/spawn ──


def test_an_app_tokens_spawn_is_handed_to_the_gate_as_the_apps(sel_calls) -> None:
    manager = _manager()
    body = {"task": "summarise", "agent": OWN_AGENT, "approval_mode": "auto"}

    response = _call(messaging.api_spawn, _request(manager, body, app=APP))

    assert response.status == 200, response.text
    kwargs = manager.spawn.call_args.kwargs
    # The gate proves a named agent is the app's own only when it is told the app.
    assert (kwargs["app"], kwargs["agent"]) == (APP, OWN_AGENT)
    assert _denials(sel_calls) == []


def test_an_app_tokens_spawn_names_an_agent(sel_calls) -> None:
    manager = _manager()

    response = _call(messaging.api_spawn, _request(manager, {"task": "summarise"}, app=APP))

    assert response.status == 400
    assert json.loads(response.text)["code"] == "spawn_rejected"
    manager.spawn.assert_not_called()
    assert _denials(sel_calls) == [(APP, "spawn.create")]


@pytest.mark.parametrize(
    ("parent", "status"),
    [
        # A session the app owns, and none at all.
        ("dashboard:shop-1", 200),
        ("", 200),
        # The owner's session, another app's, and one nothing records.
        ("dashboard:mine", 409),
        ("dashboard:other-1", 409),
        ("dashboard:gone", 409),
    ],
)
def test_an_app_tokens_run_reports_only_into_a_session_of_the_apps(
    sel_calls, persistent_parent, parent, status
) -> None:
    manager = _manager()
    slots = {
        "shop-1": SimpleNamespace(key="shop-1", _app=APP, linked_session_key=""),
        "mine": SimpleNamespace(key="mine", _app="", linked_session_key=""),
        "other-1": SimpleNamespace(key="other-1", _app=OTHER_APP, linked_session_key=""),
    }
    body = {"task": "summarise", "agent": OWN_AGENT, "parent_session": parent}

    response = _call(messaging.api_spawn, _request(manager, body, app=APP, slots=slots))

    assert response.status == status, response.text
    if status == 200:
        assert manager.spawn.call_args.kwargs["parent_session_key"] == parent
    else:
        assert json.loads(response.text)["code"] == "member_identity_unavailable"
        manager.spawn.assert_not_called()
        assert _denials(sel_calls) == [(APP, "spawn.create")]


@pytest.mark.parametrize("parent", ["cron:deadbeef", "cron:deadbeef:run1", "subagent:deadbeef"])
@pytest.mark.parametrize("record_app", [None, "", OTHER_APP, APP])
@pytest.mark.parametrize("linked", [False, True])
def test_parent_namespace_uses_its_registry_not_a_colliding_slot(
    sel_calls, persistent_parent, parent, record_app, linked
) -> None:
    runs = [] if record_app is None else [_run(id="deadbeef", app=record_app)]
    manager = _manager(*runs)
    jobs = (
        []
        if record_app is None
        else [SimpleNamespace(id="deadbeef", created_by=f"app:{record_app}" if record_app else "")]
    )
    slot_key = "alias" if linked else parent.split(":", 1)[1]
    slots = {
        slot_key: SimpleNamespace(
            key=slot_key, _app=APP, linked_session_key=parent if linked else ""
        )
    }
    body = {"task": "summarise", "agent": OWN_AGENT, "parent_session": parent}
    request = _request(manager, body, app=APP, slots=slots)
    request.app["state"].crons = SimpleNamespace(_jobs=jobs)

    response = _call(messaging.api_spawn, request)

    if record_app == APP:
        assert response.status == 200, response.text
        assert manager.spawn.call_args.kwargs["parent_session_key"] == parent
        assert _denials(sel_calls) == []
    else:
        assert response.status == 409, response.text
        assert json.loads(response.text) == {
            "error": "The execution identity is unavailable; Global memory was not used.",
            "code": "member_identity_unavailable",
        }
        manager.spawn.assert_not_called()
        assert _denials(sel_calls) == [(APP, "spawn.create")]


@pytest.mark.parametrize(
    ("parent", "slot_app", "linked_key", "status"),
    [
        ("dashboard:shop-1", APP, "", 200),
        ("dashboard:shop-1", APP, "slack:channel:thread", 409),
        ("dashboard:missing", APP, "dashboard:missing", 409),
        ("slack:channel:thread", APP, "slack:channel:thread", 200),
        ("slack:channel:thread", OTHER_APP, "slack:channel:thread", 409),
        ("slack:channel:thread", APP, "slack:channel:other", 409),
        ("slack:shop-1", APP, "", 409),
    ],
)
def test_parent_slot_must_bind_the_exact_session_key(
    sel_calls, persistent_parent, parent, slot_app, linked_key, status
) -> None:
    manager = _manager()
    slots = {"shop-1": SimpleNamespace(key="shop-1", _app=slot_app, linked_session_key=linked_key)}
    body = {"task": "summarise", "agent": OWN_AGENT, "parent_session": parent}

    response = _call(messaging.api_spawn, _request(manager, body, app=APP, slots=slots))

    assert response.status == status, response.text
    if status == 200:
        assert manager.spawn.call_args.kwargs["parent_session_key"] == parent
        assert _denials(sel_calls) == []
    else:
        assert json.loads(response.text)["code"] == "member_identity_unavailable"
        manager.spawn.assert_not_called()
        assert _denials(sel_calls) == [(APP, "spawn.create")]


@pytest.mark.parametrize("route", ["continue", "lost", "mark_collected"])
def test_run_controls_refuse_a_parent_with_only_a_colliding_app_slot(sel_calls, route) -> None:
    manager = _manager(_run(id="r1", app=APP, done=True))
    slots = {"deadbeef": SimpleNamespace(key="deadbeef", _app=APP, linked_session_key="")}
    body = {
        "task": "again",
        "batch_id": "b1",
        "ids": ["r1"],
        "written": True,
        "parent_session": "cron:deadbeef",
    }
    request = _request(manager, body, app=APP, run_id="r1", slots=slots)
    request.app["state"].crons = SimpleNamespace(_jobs=[])

    response = _call(getattr(messaging, f"api_spawn_{route}"), request)

    assert response.status == 409, response.text
    assert json.loads(response.text)["code"] == "member_identity_unavailable"
    manager.continue_conversation.assert_not_called()
    manager.record_lost_submission.assert_not_called()
    manager.mark_collected.assert_not_called()
    operation = "spawn.continue" if route == "continue" else "spawn.batch"
    assert _denials(sel_calls) == [(APP, operation)]


def test_the_owners_spawn_is_not_an_apps(sel_calls) -> None:
    """The owner names no agent and is handed to the gate with no app."""
    manager = _manager()

    response = _call(messaging.api_spawn, _request(manager, {"task": "summarise"}, app=""))

    assert response.status == 200, response.text
    assert "app" not in manager.spawn.call_args.kwargs
    assert _denials(sel_calls) == []


_PARENT = "dashboard:shop-1"
# The owner the gate recorded for batch ``b1`` in each case, and what the
# registry holds: an app's own wave is the app's whatever its members' state.
_JOIN_CASES = {
    "owner_batch": ("", _PARENT),
    "other_app_batch": (OTHER_APP, _PARENT),
    "different_parent": (APP, "dashboard:shop-2"),
    "mixed_owners": None,
    "unrecorded_owner": "unrecorded",
    "runs_only": "runs_only",
    "own_batch": (APP, _PARENT),
    "own_batch_second_queued": (APP, _PARENT),
    "own_batch_all_finished_or_evicted": (APP, _PARENT),
    "new_batch": (APP, _PARENT),
    "owner_caller": ("", _PARENT),
    "internal_caller": ("", _PARENT),
}
_JOIN_ADMITTED = {
    "own_batch",
    "own_batch_second_queued",
    "own_batch_all_finished_or_evicted",
    "new_batch",
    "owner_caller",
    "internal_caller",
}


def _batch_manager(case: str) -> MagicMock:
    """A manager whose wave ``b1`` has the recorded owner of *case*.

    ``own_batch_second_queued`` is the wave of three that the stagger split: the
    first member runs, the second is queued (counted, but no run in ``_agents``),
    and the third is the one joining now.
    """
    first = _run(id="first", app=APP, parent_session_key=_PARENT, batch_id="b1")
    second = _run(id="second", app=APP, parent_session_key=_PARENT, batch_id="b1", done=True)
    runs = [first, second]
    if case == "own_batch_second_queued":
        runs = [first]
    if case == "own_batch_all_finished_or_evicted":
        runs = []
    manager = _manager(*runs)
    manager._batch_submitted = {"b1": [2, 3]}
    owner = _JOIN_CASES[case]
    manager._batch_owner = {} if isinstance(owner, str) else {"b1": owner}
    if owner == "runs_only":
        manager._batch_submitted = {}
    return manager


@pytest.mark.parametrize("case", list(_JOIN_CASES))
def test_spawn_joins_only_a_batch_the_gate_admitted_for_its_app_and_parent(
    monkeypatch, sel_calls, persistent_parent, case
):
    manager = _batch_manager(case)
    slots = {"shop-1": SimpleNamespace(key="shop-1", _app=APP, linked_session_key="")}
    batch = "newbatch" if case == "new_batch" else "b1"
    body = {
        "task": "summarise",
        "agent": OWN_AGENT,
        "parent_session": _PARENT,
        "batch_id": batch,
    }
    request = _request(manager, body, app="" if case == "owner_caller" else APP, slots=slots)
    if case == "internal_caller":
        request["internal_auth"] = True
        monkeypatch.setattr(
            messaging, "internal_memory_scope", AsyncMock(return_value=(None, None))
        )

    response = _call(messaging.api_spawn, request)

    if case in _JOIN_ADMITTED:
        assert response.status == 200, response.text
        assert manager.spawn.call_args.kwargs["batch_id"] == batch
        assert _denials(sel_calls) == []
    else:
        assert response.status == 409, response.text
        assert json.loads(response.text) == {
            "error": "The execution identity is unavailable; Global memory was not used.",
            "code": "member_identity_unavailable",
        }
        manager.spawn.assert_not_called()
        assert _denials(sel_calls) == [(APP, "spawn.create")]


@pytest.mark.asyncio
async def test_the_gate_records_the_owner_of_every_counted_submission(sel_calls) -> None:
    """The real gate: members that never register (a refused spawn, an execution
    that cannot be read) still record the wave's owner, so the app's next member
    joins; a member under another owner cannot change the wave's owner."""
    from unittest.mock import patch

    from kiro_crew import subagent as sa
    from kiro_crew.dashboard.messaging_api.run_control import _app_token_batch_refusal
    from kiro_crew.subagent import SubagentManager

    mgr = SubagentManager(sessions=MagicMock(), ctx_builder=None, on_done=AsyncMock())
    with patch.object(sa, "sel"):
        mgr.spawn("", parent_session_key=_PARENT, batch_id="w1", batch_total=3, app=APP)
        await mgr.spawn_async(
            "a task",
            parent_session_key=_PARENT,
            batch_id="w1",
            batch_total=3,
            app=APP,
            _memory_mode="not-a-memory-mode",
        )
    assert mgr._batch_submitted["w1"][0] == 2
    assert [run for run in mgr.all_agents if run.batch_id == "w1"] == []
    assert mgr._batch_owner == {"w1": (APP, _PARENT)}

    def _refused(app: str, parent: str = _PARENT) -> bool:
        request = _request(mgr, None, app=app)
        return (
            _app_token_batch_refusal(request, parent, batch_id="w1", allow_new_batch=True)
            is not None
        )

    assert not _refused(APP)
    assert _refused(OTHER_APP)
    assert _refused(APP, "dashboard:shop-2")

    with patch.object(sa, "sel"):
        mgr.spawn("", parent_session_key=_PARENT, batch_id="w1", batch_total=3)
    assert mgr._batch_owner == {"w1": (APP, _PARENT)}
    assert mgr._batch_submitted["w1"][0] == 2
    assert not _refused(APP)

    mgr.finalize_batch("w1")
    assert mgr._batch_owner == {}


# ── batch ownership at both counting sites ──


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["gate", "unreadable_execution"])
@pytest.mark.parametrize(
    ("first_app", "next_app", "next_parent", "mixed"),
    [
        ("", APP, _PARENT, False),
        (APP, "", _PARENT, False),
        (APP, OTHER_APP, _PARENT, False),
        (APP, APP, "dashboard:shop-2", False),
        ("", APP, _PARENT, True),
    ],
    ids=["owner-first", "app-first", "other-app", "other-parent", "mixed-owner-only"],
)
async def test_batch_owner_claim_refuses_a_racing_foreign_submission(
    monkeypatch, sel_calls, entry, first_app, next_app, next_parent, mixed
) -> None:
    from kiro_crew import subagent as sa
    from kiro_crew.dashboard.messaging_api.run_control import _app_token_batch_refusal

    monkeypatch.setattr(sa, "sel", lambda: sel_calls)
    on_done = AsyncMock()
    mgr = sa.SubagentManager(sessions=MagicMock(), ctx_builder=None, on_done=on_done)
    request = _request(mgr, None, app=next_app)
    # The request's early check runs before another caller claims the batch.
    assert (
        _app_token_batch_refusal(request, next_parent, batch_id="race", allow_new_batch=True)
        is None
    )
    total = 3 if mixed else 2
    first = mgr.spawn(
        "", parent_session_key=_PARENT, batch_id="race", batch_total=total, app=first_app
    )
    if mixed:
        mgr.spawn("", parent_session_key="dashboard:mine", batch_id="race", batch_total=total)
    await asyncio.gather(*list(mgr._tasks.values()))
    before_count = list(mgr._batch_submitted["race"])
    before_progress = mgr._batch_progress_ts["race"]
    before_reports = on_done.await_count
    owner = None if mixed else (first_app, _PARENT)
    assert mgr._batch_owner == {"race": owner}
    assert mgr.batch_members_pending("race")

    kwargs = dict(parent_session_key=next_parent, batch_id="race", batch_total=999, app=next_app)
    if entry == "gate":
        refused = mgr.spawn("", **kwargs)
    else:
        refused = await mgr.spawn_async("foreign task", **kwargs, _memory_mode="invalid")
    await asyncio.gather(*list(mgr._tasks.values()))

    assert refused.done
    assert refused.error == "spawn refused: batch belongs to another app or session"
    assert (refused.batch_id, refused.batch_total) == ("", 0)
    assert mgr._batch_owner == {"race": owner}
    assert mgr._batch_submitted["race"] == before_count
    assert mgr._batch_progress_ts["race"] == before_progress
    assert on_done.await_count == before_reports
    assert not mgr.batch_reports_in_flight("race")
    assert mgr.batch_members_pending("race")
    assert sel_calls.log_tool_invocation.call_args.kwargs["outcome"] == "rejected_batch_owner"

    # Only the original owner's remaining member completes the expected wave;
    # the foreign refusal neither fills its slot nor leaves an extra hold.
    last = mgr.spawn(
        "", parent_session_key=_PARENT, batch_id="race", batch_total=total, app=first_app
    )
    await asyncio.gather(*list(mgr._tasks.values()))
    assert first.batch_id == last.batch_id == "race"
    assert mgr._batch_submitted["race"] == [total, total]
    assert not mgr.batch_members_pending("race")
    assert not mgr.batch_reports_in_flight("race")
    assert on_done.await_count == total
    mgr.finalize_batch("race")
    assert mgr._batch_owner == mgr._batch_submitted == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["gate", "unreadable_execution"])
@pytest.mark.parametrize(
    ("app", "next_parent"),
    [(APP, _PARENT), ("", "dashboard:mine")],
    ids=["same-app-and-parent", "owner-only-cross-parent"],
)
async def test_batch_owner_claim_preserves_allowed_submissions(
    monkeypatch, sel_calls, entry, app, next_parent
) -> None:
    from kiro_crew import subagent as sa

    monkeypatch.setattr(sa, "sel", lambda: sel_calls)
    mgr = sa.SubagentManager(sessions=MagicMock(), ctx_builder=None, on_done=AsyncMock())
    mgr.spawn("", parent_session_key=_PARENT, batch_id="wave", batch_total=2, app=app)
    kwargs = dict(parent_session_key=next_parent, batch_id="wave", batch_total=2, app=app)
    if entry == "gate":
        second = mgr.spawn("", **kwargs)
        assert second.error == "spawn refused: task must be a non-empty string"
    else:
        second = await mgr.spawn_async("task", **kwargs, _memory_mode="invalid")
        assert second.error.startswith("memory_unavailable:")
    await asyncio.gather(*list(mgr._tasks.values()))
    assert second.batch_id == "wave"
    assert mgr._batch_submitted == {"wave": [2, 2]}
    assert mgr._batch_owner == {"wave": (app, _PARENT) if app else None}
    assert not mgr.batch_members_pending("wave")
    assert not mgr.batch_reports_in_flight("wave")


# ── the per-run routes ──


@pytest.mark.parametrize("route", ["status", "steer", "retry", "release", "delete", "continue"])
@pytest.mark.parametrize("run_app", ["", OTHER_APP])
def test_an_app_token_reaches_no_run_it_did_not_start(sel_calls, route, run_app) -> None:
    manager = _manager(_run(id="r1", app=run_app, done=route in ("retry", "continue")))
    handler = getattr(messaging, f"api_spawn_{route}")
    body = {"task": "again", "message": "stop"}

    response = _call(handler, _request(manager, body, app=APP, run_id="r1"))

    assert response.status == 404
    assert json.loads(response.text) == {"error": "not found", "code": "task_scope_denied"}
    assert _denials(sel_calls) == [(APP, "spawn.access")]
    manager.spawn.assert_not_called()
    manager.steer.assert_not_called()
    manager.cancel.assert_not_called()


@pytest.mark.parametrize(
    ("caller_app", "run_app"),
    [
        # An app's own run, and the owner on anyone's run.
        (APP, APP),
        ("", ""),
        ("", OTHER_APP),
    ],
)
def test_the_scope_check_admits_the_runs_app_and_the_owner(sel_calls, caller_app, run_app) -> None:
    manager = _manager(_run(id="r1", app=run_app))
    request = _request(manager, None, app=caller_app, run_id="r1")

    assert asyncio.run(messaging._spawn_scope_refusal(request)) is None
    assert _denials(sel_calls) == []


@pytest.mark.parametrize(("record_app", "admitted"), [(APP, True), (OTHER_APP, False), ("", False)])
def test_a_finished_run_is_the_apps_by_its_record(
    monkeypatch, sel_calls, record_app, admitted
) -> None:
    """A run this process does not hold: its own record names the app."""
    manager = _manager()
    manager._admission.queued_run = None
    monkeypatch.setattr(messaging, "_queued_run", AsyncMock(return_value=None))
    record = {"parent_session": "", "execution_context": {"app": record_app}}
    monkeypatch.setattr(messaging, "read_state", lambda run_id: record)
    request = _request(manager, None, app=APP, run_id="old")

    refusal = asyncio.run(messaging._spawn_scope_refusal(request))

    assert (refusal is None) is admitted
    if not admitted:
        assert refusal.status == 404


def test_an_app_token_lists_only_the_live_runs_of_its_app(monkeypatch, sel_calls) -> None:
    manager = _manager(
        _run(id="mine", app=APP), _run(id="owners", app=""), _run(id="theirs", app=OTHER_APP)
    )
    monkeypatch.setattr(
        messaging, "read_panel_records", lambda **kw: messaging.PanelRecords([], 0, False)
    )

    listed = {
        caller: [
            row["id"]
            for row in json.loads(
                _call(messaging.api_spawn_list, _request(manager, None, app=caller)).text
            )["agents"]
        ]
        for caller in (APP, "")
    }

    assert listed == {APP: ["mine"], "": ["mine", "owners", "theirs"]}


@pytest.mark.asyncio
@pytest.mark.parametrize("record_app", [APP, OTHER_APP, ""])
@pytest.mark.parametrize(
    "ending",
    [
        "depth",
        "memory_mode",
        "execution",
        "cwd",
        "governance",
        "allowlist",
        "floor_expiry",
        "pressure_expiry",
        "cancel",
        "queued_expiry",
    ],
)
async def test_queued_terminal_preserves_app_scope(
    monkeypatch, sel_calls, close_subagent_managers, ending, record_app
) -> None:
    """Every registered pre-start ending keeps the queue's owner, never a fallback."""
    from kiro_crew import subagent as sa
    from kiro_crew.subagent_manager.admission import SpawnAdmissionCoordinator
    from kiro_crew.subagent_wait_reasons import (
        MEMORY_PRESSURE_NEVER_STARTED,
        QUEUED_WAIT_EXPIRED_TEXT,
    )

    cfg = KiroCrewConfig()
    cfg.agent.task_queue_enabled = False
    monkeypatch.setattr(KiroCrewConfig, "load", lambda: cfg)
    monkeypatch.setattr(SpawnAdmissionCoordinator, "open_store_off_loop", False)
    monkeypatch.setattr(sa, "sel", lambda: sel_calls)
    monkeypatch.setattr(sa, "_vet_spawn_governance", lambda *args, **kw: None)
    monkeypatch.setattr(sa, "parent_spawn_policy", lambda *args: None)
    monkeypatch.setattr(sa, "_validate_app_agent_ownership", lambda *args: None)
    monkeypatch.setattr(sa, "_validate_agent", lambda *args: (OWN_AGENT, "", ""))
    mgr = sa.SubagentManager(sessions=MagicMock(), ctx_builder=None, on_done=AsyncMock())
    monkeypatch.setattr(mgr, "_emit_queue_depth", MagicMock())
    monkeypatch.setattr(mgr, "_should_stagger_queue", lambda now: (True, False))
    monkeypatch.setattr(mgr, "_memory_pressure_hold", lambda **kw: None)
    monkeypatch.setattr(
        messaging, "read_panel_records", lambda **kw: messaging.PanelRecords([], 0, False)
    )
    queued = mgr.spawn("work", agent=OWN_AGENT, app=record_app, _parent_project_dirs=[])
    assert queued.queued and not queued.done
    assert queued.app == record_app

    if ending == "cancel":
        assert await mgr.cancel(queued.id)
        await asyncio.gather(*list(mgr._report_tasks))
        terminal = mgr.get(queued.id)
        assert terminal.user_stopped
    else:
        params = mgr._queue.pop()
        if ending == "queued_expiry":
            report = mgr._report_queued_stop(params, error=QUEUED_WAIT_EXPIRED_TEXT)
            assert report is not None
            await report
            terminal = mgr.get(queued.id)
            assert terminal.error == QUEUED_WAIT_EXPIRED_TEXT
        else:
            if ending == "depth":
                params["_depth"] = sa._MAX_SPAWN_DEPTH + 1
            elif ending == "memory_mode":
                params["_memory_mode"] = "invalid"
            elif ending == "execution":
                params["_execution_context"] = {}
            elif ending == "cwd":
                params["cwd"] = "/refused"
                monkeypatch.setattr(sa, "validate_cwd", lambda *args: ("", "path denied"))
            elif ending == "governance":
                monkeypatch.setattr(sa, "_vet_spawn_governance", lambda *args, **kw: "denied")
            elif ending == "allowlist":
                params["_parent_spawn_policy"] = SimpleNamespace()
                monkeypatch.setattr(sa, "_vet_parent_available_agents", lambda *a, **kw: "denied")
            elif ending == "floor_expiry":
                monkeypatch.setattr(sa, "check_memory_available", lambda **kw: (False, 0.0))
                bound = mgr._admission.taskq_memory_wait_bound_secs()
                assert bound > 0
                mgr._floor_waits[queued.id] = (round(bound * 1e9), 0, 0)
            else:
                monkeypatch.setattr(mgr, "_memory_pressure_hold", lambda **kw: 2)
                monkeypatch.setattr(mgr, "_memory_pressure_holds", lambda *a, **kw: "expired")
            terminal = mgr.spawn(**params, _from_queue=True)
            assert terminal.error
            if ending in ("floor_expiry", "pressure_expiry"):
                assert terminal.error == (
                    QUEUED_WAIT_EXPIRED_TEXT
                    if ending == "floor_expiry"
                    else MEMORY_PRESSURE_NEVER_STARTED
                )

    assert terminal.done and mgr.get(queued.id) is terminal
    for caller in (APP, OTHER_APP):
        response = await messaging.api_spawn_status(_request(mgr, app=caller, run_id=queued.id))
        listed = await messaging.api_spawn_list(_request(mgr, app=caller))
        ids = [row["id"] for row in json.loads(listed.text)["agents"]]
        if caller == record_app:
            assert response.status == 200, response.text
            assert json.loads(response.text)["id"] == queued.id
            assert json.loads(response.text)["done"] is True
            assert queued.id in ids
        else:
            assert response.status == 404, response.text
            assert json.loads(response.text)["code"] == "task_scope_denied"
            assert queued.id not in ids
    assert terminal.app == record_app


@pytest.mark.parametrize("route", ["lost", "mark_collected"])
def test_an_app_token_reconciles_no_batch_of_another_session(sel_calls, route) -> None:
    manager = _manager()
    slots = {"mine": SimpleNamespace(_app="", linked_session_key="")}
    body = {"batch_id": "b1", "ids": ["r1"], "written": True, "parent_session": "dashboard:mine"}
    handler = getattr(messaging, f"api_spawn_{route}")

    response = _call(handler, _request(manager, body, app=APP, slots=slots))

    assert response.status == 409
    assert json.loads(response.text)["code"] == "member_identity_unavailable"
    manager.record_lost_submission.assert_not_called()
    assert _denials(sel_calls) == [(APP, "spawn.batch")]


_LOST_CASES = {
    "owner_batch": ("", "dashboard:mine"),
    "other_app_batch": (OTHER_APP, _PARENT),
    "unknown_batch": (APP, _PARENT),
    "different_parent": (APP, "dashboard:shop-2"),
    "mixed_owners": None,
    "unrecorded_owner": "unrecorded",
    "own_batch": (APP, _PARENT),
    "own_batch_second_queued": (APP, _PARENT),
    "owner_caller": ("", "dashboard:mine"),
    "internal_caller": (APP, _PARENT),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", list(_LOST_CASES))
async def test_lost_batch_requires_the_recorded_owner(monkeypatch, sel_calls, case):
    from kiro_crew.subagent import WaveDigestCoordinator
    from kiro_crew.testing.wait import await_until

    parent = _PARENT
    owner = _LOST_CASES[case]
    run_app, run_parent = owner if isinstance(owner, tuple) else (APP, parent)
    first = _run(id="first", app=run_app, parent_session_key=run_parent, batch_id="b1", done=True)
    second = _run(id="second", app=run_app, parent_session_key=run_parent, batch_id="b1")
    runs = [first] if case == "own_batch_second_queued" else [first, second]
    manager = _manager(*runs, _run(id="unrelated", app="", batch_id="b2"))
    manager._batch_submitted = {"b1": [2, 3]}
    manager._batch_owner = {} if isinstance(owner, str) else {"b1": owner}
    manager._batch_progress_ts = {"b1": 10.0}
    manager._mint_agent_id.return_value = "lost1"
    manager._safe_announce = AsyncMock()
    manager.record_lost_submission.side_effect = WaveDigestCoordinator(
        manager
    ).record_lost_submission_impl
    before = {key: list(counts) for key, counts in manager._batch_submitted.items()}
    request_parent = run_parent if case == "owner_caller" else parent
    slots = {"shop-1": SimpleNamespace(_app=APP, linked_session_key="")}
    body = {
        "parent_session": request_parent,
        "batch_id": "unknown" if case == "unknown_batch" else "b1",
        "batch_total": 3,
        "reason": "rejected before admission",
    }
    request = _request(manager, body, app="" if case == "owner_caller" else APP, slots=slots)
    if case == "internal_caller":
        request["internal_auth"] = True
        monkeypatch.setattr(
            messaging, "internal_memory_scope", AsyncMock(return_value=(None, None))
        )

    response = await messaging.api_spawn_lost(request)
    for task in list(manager._tasks.values()):
        await await_until(task, what="lost submission announcement")

    if case in ("own_batch", "own_batch_second_queued", "owner_caller", "internal_caller"):
        assert response.status == 200, response.text
        assert manager._batch_submitted == {"b1": [3, 3]}
        manager._safe_announce.assert_awaited_once()
        info = manager._safe_announce.call_args.args[0]
        assert (info.batch_id, info.parent_session_key, info.done) == ("b1", request_parent, True)
        assert _denials(sel_calls) == []
    else:
        assert response.status == 409, response.text
        assert json.loads(response.text)["code"] == "member_identity_unavailable"
        manager.record_lost_submission.assert_not_called()
        manager._safe_announce.assert_not_called()
        manager.arm_report_in_flight.assert_not_called()
        assert manager._batch_submitted == before
        assert manager._batch_progress_ts == {"b1": 10.0}
        assert _denials(sel_calls) == [(APP, "spawn.batch")]


@pytest.mark.parametrize(
    "case",
    ["owner_run", "other_app_run", "unknown_run", "different_parent", "own_runs", "owner_caller"],
)
def test_mark_collected_checks_all_owners_before_any_mark(sel_calls, case):
    from kiro_crew.parent_turns import ParentTurnLedger

    parent = "dashboard:shop-1"
    token = (parent, None, 1)
    first = _run(
        id="first",
        app=APP,
        parent_session_key=parent,
        _spawned_under=token,
        _seen_done_under=token,
        _collected_inline=False,
    )
    second = _run(
        id="second",
        app=APP,
        parent_session_key=parent,
        _spawned_under=token,
        _seen_done_under=token,
        _collected_inline=False,
    )
    if case in ("owner_run", "owner_caller"):
        second.app = ""
    if case == "other_app_run":
        second.app = OTHER_APP
    if case == "different_parent":
        second.parent_session_key = "dashboard:shop-2"
    manager = _manager(first, second)
    slots = {"shop-1": SimpleNamespace(_app=APP, linked_session_key="")}
    body = {
        "parent_session": parent,
        "ids": ["first", "unknown" if case == "unknown_run" else "second"],
        "written": True,
    }
    request = _request(manager, body, app="" if case == "owner_caller" else APP, slots=slots)
    ledger = ParentTurnLedger()
    request.app["state"].sessions = SimpleNamespace(
        parent_turns=ledger, turn_token=lambda *args, **kwargs: token
    )

    response = _call(messaging.api_spawn_mark_collected, request)

    if case in ("own_runs", "owner_caller"):
        assert response.status == 200, response.text
        assert first._collected_inline is True
        assert second._collected_inline is True
        assert ledger.pending(token)
        assert _denials(sel_calls) == []
    else:
        assert response.status == 409, response.text
        assert json.loads(response.text)["code"] == "member_identity_unavailable"
        assert first._collected_inline is False
        assert second._collected_inline is False
        assert not ledger.pending(token)
        assert _denials(sel_calls) == [(APP, "spawn.batch")]


# ── cron fire ──


async def _fire(
    monkeypatch, tmp_path, *, created_by: str, default_agent: str | None = None, **job_fields
):
    """Fire one job against a config with a Bot, ``beta``, and a shared-store row."""
    write_member_home(config_dir(), "alpha", "beta")
    loader._invalidate_config_cache()
    cfg = KiroCrewConfig.load()
    if default_agent is not None:
        cfg.agent.default_agent = default_agent
    beta_tree = tmp_path / "beta-tree"
    beta_tree.mkdir()
    cfg.workspaces["beta-ws"] = WorkspaceConfig(dir=str(beta_tree))
    cfg.agents["beta"].kiro_agent = "beta-mode"
    cfg.agents["beta"].workspace = "beta-ws"
    cfg.agents["plain"] = KiroCrewAgentConfig(kiro_agent="plain-mode")
    gw, callback = await _cron_callback(monkeypatch, cfg)
    gw._alert_cron_failure = AsyncMock()
    # A job stores the agent it names as its captured template; the fire
    # dispatches that name, not ``agent_id``.
    named = job_fields.get("agent_id") or "kirocrew"
    template = ExecutionContext(None, MemoryStoreRef("default"), "template", named)
    job = CronJob(
        id="job1",
        name="job",
        message="task",
        created_by=created_by,
        **{"execution_context": template.to_record(), **job_fields},
    )
    try:
        await callback(job)
    except RuntimeError as exc:
        # The harness stops every dispatch at the provider.
        assert "stop before the provider" in str(exc)
    return gw, job, str(beta_tree)


def _dispatches(gw) -> list[tuple[str | None, str | None, str | None]]:
    return [
        (call.kwargs.get("agent"), call.kwargs.get("crew_agent"), call.kwargs.get("cwd"))
        for call in gw.sessions.get_or_create.call_args_list
    ]


@pytest.mark.asyncio
async def test_an_apps_sequence_step_naming_a_bot_runs_as_a_template(monkeypatch, tmp_path):
    gw, _job, beta_tree = await _fire(
        monkeypatch, tmp_path, created_by=f"app:{APP}", agent_sequence=["beta", "plain"]
    )

    agent, crew, cwd = _dispatches(gw)[0]
    # The name as a template, no crew identity, and not the Bot's workspace.
    assert (agent, crew) == ("beta", "")
    assert cwd != beta_tree


@pytest.mark.asyncio
async def test_the_owners_sequence_step_still_runs_as_the_bot(monkeypatch, tmp_path):
    gw, _job, beta_tree = await _fire(
        monkeypatch, tmp_path, created_by="", agent_sequence=["beta", "plain"]
    )

    assert _dispatches(gw)[0] == ("beta-mode", "beta", beta_tree)


@pytest.mark.asyncio
async def test_an_apps_sequence_step_on_a_shared_store_row_still_collapses(monkeypatch, tmp_path):
    gw, _job, _tree = await _fire(
        monkeypatch, tmp_path, created_by=f"app:{APP}", agent_sequence=["plain", "beta"]
    )

    agent, crew, _cwd = _dispatches(gw)[0]
    assert (agent, crew) == ("plain-mode", "plain")


@pytest.mark.asyncio
async def test_an_apps_single_job_naming_a_bot_takes_nothing_of_the_bots(monkeypatch, tmp_path):
    gw, _job, beta_tree = await _fire(
        monkeypatch, tmp_path, created_by=f"app:{APP}", agent_id="beta"
    )

    agent, crew, cwd = _dispatches(gw)[0]
    # The name as a template, no crew identity, and not the Bot's workspace.
    assert (agent, crew) == ("beta", "")
    assert cwd != beta_tree


@pytest.mark.asyncio
async def test_the_owners_single_job_still_runs_as_the_bot(monkeypatch, tmp_path):
    gw, _job, beta_tree = await _fire(monkeypatch, tmp_path, created_by="", agent_id="beta")

    assert _dispatches(gw)[0] == ("beta-mode", "beta", beta_tree)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("created_by", "app", "refused"),
    [(f"app:{APP}", "", True), ("", APP, False), ("", "", False)],
)
async def test_an_apps_job_that_captured_a_bots_execution_is_refused(
    monkeypatch, tmp_path, created_by, app, refused
):
    captured = ExecutionContext(
        "beta",
        MemoryStoreRef("member-beta", "beta"),
        "member",
        "beta-mode",
        app=app,
        selection_name="beta",
    )

    gw, job, _tree = await _fire(
        monkeypatch,
        tmp_path,
        created_by=created_by,
        member_id="beta",
        execution_context=captured.to_record(),
    )

    from kiro_crew.slack import gateway

    if refused:
        gw.sessions.get_or_create.assert_not_awaited()
        assert (job.last_status, job.last_error) == ("error", gateway.APP_JOB_BOT_REFUSAL)
        assert job.fire_time_denied is True
        gw._alert_cron_failure.assert_awaited_once()
    else:
        assert _dispatches(gw)[0][:2] == ("beta-mode", "beta")
        assert job.fire_time_denied is False
        gw._alert_cron_failure.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "named",
    [{"agent_id": "beta"}, {"agent_sequence": ["plain", "beta"]}],
    ids=["agent_id", "agent_sequence"],
)
@pytest.mark.parametrize(
    ("created_by", "app", "refused"),
    [(f"app:{APP}", "", True), ("", APP, False), ("", "", False)],
)
async def test_an_apps_job_naming_a_bots_own_file_fails_before_dispatch(
    monkeypatch, tmp_path, named, created_by, app, refused
):
    """A name the job stores is dispatched as a template; a Bot's own file is the Bot's."""
    from kiro_crew import mate_worker
    from kiro_crew.slack import gateway

    monkeypatch.setattr(mate_worker, "is_mate_owned_agent", lambda name: name == "beta")
    captured = ExecutionContext(
        None, MemoryStoreRef("default"), "template", named.get("agent_id", "kirocrew"), app=app
    )
    gw, job, _tree = await _fire(
        monkeypatch,
        tmp_path,
        created_by=created_by,
        execution_context=captured.to_record(),
        **named,
    )

    if refused:
        gw.sessions.get_or_create.assert_not_awaited()
        assert (job.last_status, job.last_error) == ("error", gateway.APP_JOB_BOT_REFUSAL)
        assert job.fire_time_denied is True
    else:
        assert _dispatches(gw)
        assert job.fire_time_denied is False


@pytest.mark.parametrize(
    ("job_fields", "refused"),
    [({"script": "app_job.py:run"}, False), ({"command": "true"}, False), ({}, True)],
    ids=["script", "command", "message"],
)
def test_an_apps_default_bot_file_is_judged_only_for_agent_dispatch(
    monkeypatch, job_fields, refused
):
    from kiro_crew import mate_worker
    from kiro_crew.slack import gateway

    monkeypatch.setattr(mate_worker, "is_mate_owned_agent", lambda name: name == "beta")
    captured = ExecutionContext(None, MemoryStoreRef("default"), "template", "kirocrew")
    job = CronJob(id="job1", name="job", message="task", created_by=f"app:{APP}", **job_fields)

    assert gateway._app_job_runs_as_bot(job, captured, "beta", KiroCrewConfig()) is refused


@pytest.mark.parametrize("bot_source", ["agent_id", "agent_sequence", "capture"])
def test_an_apps_script_still_refuses_a_stored_bot_identity(monkeypatch, bot_source):
    from kiro_crew import mate_worker
    from kiro_crew.slack import gateway

    monkeypatch.setattr(mate_worker, "is_mate_owned_agent", lambda name: name == "beta")
    captured = ExecutionContext(
        None,
        MemoryStoreRef("default"),
        "template",
        "beta" if bot_source == "capture" else "kirocrew",
    )
    job = CronJob(
        id="job1",
        name="job",
        message="task",
        created_by=f"app:{APP}",
        script="app_job.py:run",
        agent_id="beta" if bot_source == "agent_id" else None,
        agent_sequence=["plain", "beta"] if bot_source == "agent_sequence" else [],
        execution_context=captured.to_record(),
    )

    assert gateway._app_job_runs_as_bot(job, captured, "kirocrew", KiroCrewConfig())


@pytest.mark.asyncio
@pytest.mark.parametrize("sequence", [[], ["plain"]], ids=["no_sequence", "dormant_sequence"])
@pytest.mark.parametrize(
    ("created_by", "app", "refused"),
    [(f"app:{APP}", "", True), ("", APP, False), ("", "", False)],
    ids=["app_creator", "app_capture", "owner"],
)
async def test_agentless_job_with_a_bots_own_file_as_default(
    monkeypatch, tmp_path, sequence, created_by, app, refused
):
    from kiro_crew import mate_worker, subagent
    from kiro_crew.slack import gateway

    monkeypatch.setattr(mate_worker, "is_mate_owned_agent", lambda name: name == "beta")
    allowlists = MagicMock(return_value=())
    monkeypatch.setattr(subagent, "parent_spawn_allowlists", allowlists)
    captured = ExecutionContext(None, MemoryStoreRef("default"), "template", "kirocrew", app=app)

    gw, job, beta_tree = await _fire(
        monkeypatch,
        tmp_path,
        created_by=created_by,
        default_agent="beta",
        agent_sequence=sequence,
        execution_context=captured.to_record(),
    )

    allowlists.assert_called_once_with("beta")
    assert not job.agent_id
    assert job.execution_context == captured.to_record()
    if refused:
        gw.sessions.get_or_create.assert_not_awaited()
        assert (job.last_status, job.last_error) == ("error", gateway.APP_JOB_BOT_REFUSAL)
        assert job.fire_time_denied is True
        gw._alert_cron_failure.assert_awaited_once_with(
            job, gateway.APP_JOB_BOT_REFUSAL, denied=True
        )
    else:
        agent, crew, cwd = _dispatches(gw)[0]
        assert (agent, crew) == ("beta", "")
        assert cwd != beta_tree
        assert job.fire_time_denied is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "named",
    [{"agent_id": "beta"}, {"agent_sequence": ["beta", "plain"]}],
    ids=["agent_id", "agent_sequence"],
)
@pytest.mark.parametrize("app", [APP, ""], ids=["app", "owner"])
async def test_a_jobs_captured_app_claim_does_not_change_bot_name_dispatch(
    monkeypatch, tmp_path, named, app
):
    captured = ExecutionContext(
        None, MemoryStoreRef("default"), "template", named.get("agent_id", "kirocrew"), app=app
    )

    gw, job, beta_tree = await _fire(
        monkeypatch, tmp_path, created_by="", execution_context=captured.to_record(), **named
    )

    assert _dispatches(gw)[0] == ("beta-mode", "beta", beta_tree)
    assert job.fire_time_denied is False


@pytest.mark.parametrize("captured", [None, {}, {"app": APP}, "invalid", []])
@pytest.mark.parametrize("created_by", ["", f"app:{APP}"])
def test_app_job_classification_ignores_missing_or_malformed_capture(captured, created_by):
    from kiro_crew.slack import gateway

    job = SimpleNamespace(created_by=created_by, execution_context=captured)

    assert gateway._job_is_an_apps(job) is bool(created_by)
