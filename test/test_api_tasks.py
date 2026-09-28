"""``/api/tasks*``: rows, detail, cancel-through-the-cascade, summary, auth.

A real ``TaskStore`` on a temp file with an injected clock, a fake dashboard
state whose ``subagents`` carries the store and a recording ``cancel``, and the
REAL token middleware for the auth case. No manager, no sockets, no sleeps.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from overload_fakes import Clock, open_task_store

from kiro_crew.dashboard import session_health
from kiro_crew.dashboard.handlers import tasks as tasks_mod
from kiro_crew.taskq import model
from kiro_crew.taskq.adapters import runner as taskq_runner
from kiro_crew.taskq.store import TaskStore
from kiro_crew.taskq.waits import WaitRecord


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store(tmp_path: Path, clock: Clock):
    yield from open_task_store(tmp_path, clock)


@pytest.fixture(autouse=True)
def _fresh_monitor(monkeypatch: pytest.MonkeyPatch):
    """A private health monitor: the default one carries progress memos and
    registered cap sources from other tests in the same process."""
    mon = session_health.SessionHealthMonitor(include_log_scan=False)
    monkeypatch.setattr(session_health, "_default_monitor", mon)
    yield mon


class FakeManager:
    """Just enough of ``SubagentManager`` for the handlers: the store and ``cancel``.

    ``cancel`` records the call and, like the real cascade, ends the row
    through the STORE's own cancel (the manager owns that write); the handler
    under test must never touch the store directly.
    """

    def __init__(self, store: TaskStore, *, honours: bool = True) -> None:
        self._taskq = store
        self.cancel_calls: list[str] = []
        self._honours = honours
        self.max_concurrent = 4
        self.running_count = 0
        self._queue: list[dict[str, Any]] = []
        self.running: list[Any] = []

    async def cancel(self, agent_id: str) -> bool:
        self.cancel_calls.append(agent_id)
        if not self._honours:
            return False
        return self._taskq.cancel(agent_id, reason="user_stop") is not None


def _state(store: TaskStore, **kw: Any) -> SimpleNamespace:
    return SimpleNamespace(subagents=FakeManager(store, **kw), _slots={})


def _req(
    method: str,
    path: str,
    state: Any,
    *,
    query: str = "",
    match: dict | None = None,
    body: Any = None,
):
    # A REAL Application: make_mocked_request's default app is a Mock whose
    # ``["state"]`` answers a fresh MagicMock, which reads as a phantom store.
    app = web.Application()
    app["state"] = state
    req = make_mocked_request(
        method, path + (f"?{query}" if query else ""), match_info=match or {}, app=app
    )
    if body is not None:
        fut: asyncio.Future[Any] = asyncio.get_event_loop().create_future()
        fut.set_result(body)
        req.json = lambda: fut  # type: ignore[method-assign]
    return req


def _body(resp: web.Response) -> dict[str, Any]:
    assert resp.text is not None
    return json.loads(resp.text)


def _seed(store: TaskStore, clock: Clock) -> dict[str, str]:
    """A queue with one row per interesting state; returns ids by role."""
    rows = [
        model.TaskRecord(
            id="q-1", kind=model.KIND_SUBAGENT, session_key="web-a", params={"task": "a"}
        ),
        model.TaskRecord(
            id="q-2", kind=model.KIND_SUBAGENT, session_key="web-b", params={"task": "b"}
        ),
        model.TaskRecord(id="sys-1", kind=model.KIND_CRON, session_key="", params={"job": "j"}),
        model.TaskRecord(id="run-1", kind=model.KIND_SUBAGENT, session_key="web-a", params={}),
        model.TaskRecord(id="dep-1", kind=model.KIND_SUBAGENT, session_key="web-a", params={}),
        model.TaskRecord(id="inp-1", kind=model.KIND_SUBAGENT, session_key="web-b", params={}),
        model.TaskRecord(id="rec-1", kind=model.KIND_SUBAGENT, session_key="web-b", params={}),
        model.TaskRecord(id="done-1", kind=model.KIND_SUBAGENT, session_key="web-a", params={}),
    ]
    store.accept(rows)
    clock.t += 30.0
    # run-1: claimed and running.
    assert store.claim("run-1") is not None
    assert store.transition("run-1", model.STARTING)
    assert store.transition("run-1", model.RUNNING)
    # dep-1: running → waiting_dependency (github:api, retry in 90s).
    assert store.claim("dep-1") is not None
    assert store.transition("dep-1", model.STARTING)
    assert store.transition("dep-1", model.RUNNING)
    dep = WaitRecord.dependency("github:api", since=clock.t, retry_at=clock.t + 90.0)
    assert store.enter_wait("dep-1", dep.to_dict())
    # inp-1: running → waiting_input.
    assert store.claim("inp-1") is not None
    assert store.transition("inp-1", model.STARTING)
    assert store.transition("inp-1", model.RUNNING)
    inp = WaitRecord.input("call-7", since=clock.t, reason="sudo wants a password")
    assert store.enter_wait("inp-1", inp.to_dict())
    # rec-1: dispatched once, now recovering with a backoff.
    assert store.claim("rec-1") is not None
    assert store.transition("rec-1", model.STARTING)
    assert store.transition("rec-1", model.RECOVERING, next_run_at=clock.t + 4.0)
    # q-2: a transient in-run failure parked it in retry_wait (no runtime held).
    assert store.claim("q-2") is not None
    assert store.transition("q-2", model.STARTING)
    assert store.transition("q-2", model.RETRY_WAIT, next_run_at=clock.t + 9.0)
    # done-1: finished.
    assert store.claim("done-1") is not None
    assert store.transition("done-1", model.STARTING)
    assert store.transition("done-1", model.RUNNING)
    assert store.transition("done-1", model.DONE)
    clock.t += 60.0
    return {r.id: r.id for r in rows}


# ── GET /api/tasks ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_returns_rows_with_lane_and_wait_fields(store: TaskStore, clock: Clock) -> None:
    _seed(store, clock)
    resp = await tasks_mod.api_tasks_list(_req("GET", "/api/tasks", _state(store)))
    assert resp.status == 200
    body = _body(resp)
    assert body["available"] is True
    assert body["count"] == 8
    by_id = {t["id"]: t for t in body["tasks"]}
    assert by_id["sys-1"]["lane"] == "system"
    assert by_id["q-1"]["lane"] == "web-a"
    dep = by_id["dep-1"]
    assert dep["state"] == "waiting_dependency"
    assert dep["wait"]["resume_kind"] == "at_time"
    assert dep["wait"]["dependency_scope"] == "github:api"
    assert dep["wait_since"] == pytest.approx(1_030.0)
    assert dep["wait_reason"]
    assert by_id["inp-1"]["wait_reason"] == "sudo wants a password"
    assert by_id["rec-1"]["attempts"] == 1
    assert by_id["rec-1"]["next_run_at"] == pytest.approx(1_034.0)
    assert by_id["run-1"]["lease_owner"] == store.incarnation
    assert by_id["done-1"]["terminal"] is True
    assert by_id["q-1"]["parent_id"] is None and by_id["q-1"]["root_id"] == "q-1"


@pytest.mark.asyncio
async def test_list_filters_by_state_lane_and_limit(store: TaskStore, clock: Clock) -> None:
    _seed(store, clock)
    st = _state(store)
    body = _body(
        await tasks_mod.api_tasks_list(_req("GET", "/api/tasks", st, query="state=queued"))
    )
    assert sorted(t["id"] for t in body["tasks"]) == ["q-1", "sys-1"]
    body = _body(await tasks_mod.api_tasks_list(_req("GET", "/api/tasks", st, query="lane=system")))
    assert [t["id"] for t in body["tasks"]] == ["sys-1"]
    body = _body(await tasks_mod.api_tasks_list(_req("GET", "/api/tasks", st, query="lane=web-b")))
    assert sorted(t["id"] for t in body["tasks"]) == ["inp-1", "q-2", "rec-1"]
    body = _body(
        await tasks_mod.api_tasks_list(
            _req("GET", "/api/tasks", st, query="lane=web-b&state=queued")
        )
    )
    assert [t["id"] for t in body["tasks"]] == []
    body = _body(
        await tasks_mod.api_tasks_list(
            _req("GET", "/api/tasks", st, query="lane=web-b&state=retry_wait")
        )
    )
    assert [t["id"] for t in body["tasks"]] == ["q-2"]
    body = _body(await tasks_mod.api_tasks_list(_req("GET", "/api/tasks", st, query="limit=2")))
    assert body["count"] == 2 and body["limit"] == 2
    # Ceiling, not an error, for an oversized limit.
    body = _body(
        await tasks_mod.api_tasks_list(_req("GET", "/api/tasks", st, query="limit=999999"))
    )
    assert body["limit"] == tasks_mod._MAX_LIMIT


@pytest.mark.asyncio
async def test_nested_row_reports_its_roots_lane(store: TaskStore, clock: Clock) -> None:
    """The stored ``lane`` column (resolved at accept) wins over derivation."""
    _seed(store, clock)
    store.accept(
        [
            model.TaskRecord(
                id="child-1",
                kind=model.KIND_SUBAGENT,
                session_key="subagent:run-1",
                parent_id="run-1",
                params={},
            )
        ]
    )
    body = _body(
        await tasks_mod.api_tasks_list(_req("GET", "/api/tasks", _state(store), query="lane=web-a"))
    )
    by_id = {t["id"]: t for t in body["tasks"]}
    assert by_id["child-1"]["lane"] == "web-a"
    assert by_id["child-1"]["parent_id"] == "run-1" and by_id["child-1"]["root_id"] == "run-1"


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["state=bogus", "limit=0", "limit=x", "limit=-3"])
async def test_list_rejects_bad_filters(store: TaskStore, query: str) -> None:
    resp = await tasks_mod.api_tasks_list(_req("GET", "/api/tasks", _state(store), query=query))
    assert resp.status == 400
    assert _body(resp)["code"] in ("bad_state", "bad_limit")


@pytest.mark.asyncio
async def test_list_without_a_store_reports_unavailable() -> None:
    state = SimpleNamespace(subagents=SimpleNamespace(_taskq=None), _slots={})
    body = _body(await tasks_mod.api_tasks_list(_req("GET", "/api/tasks", state)))
    assert body == {"available": False, "tasks": [], "count": 0}


# ── GET /api/tasks/{id} ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_detail_returns_row_and_events_tail(store: TaskStore, clock: Clock) -> None:
    _seed(store, clock)
    resp = await tasks_mod.api_task_detail(
        _req("GET", "/api/tasks/dep-1", _state(store), match={"task_id": "dep-1"})
    )
    assert resp.status == 200
    body = _body(resp)
    assert body["task"]["id"] == "dep-1"
    kinds = [e["kind"] for e in body["events"]]
    assert kinds[0] == "accepted"
    assert "claimed" in kinds
    assert kinds[-1] == "transition"
    assert body["events"][-1]["data"].get("wait") is True
    # Oldest first, seq ascending.
    assert [e["seq"] for e in body["events"]] == sorted(e["seq"] for e in body["events"])


@pytest.mark.asyncio
async def test_detail_404_for_unknown_id(store: TaskStore) -> None:
    resp = await tasks_mod.api_task_detail(
        _req("GET", "/api/tasks/nope", _state(store), match={"task_id": "nope"})
    )
    assert resp.status == 404
    assert _body(resp)["code"] == "not_found"


# ── redaction: store prose on the wire ───────────────────────────────────────

#: A credential the redaction table matches, and an exfiltration URL whose
#: payload is that same credential. Real shapes, not placeholders: a pin over
#: a string the redactor does not match would pass on an unredacted surface.
_CRED = "AKIAIOSFODNN7EXAMPLE"
_EXFIL_HOST = "collector.example.invalid"
_EXFIL_URL = f"https://{_EXFIL_HOST}/ingest?token={_CRED}"


def _leaky_row(store: TaskStore, clock: Clock) -> None:
    """One row whose EVERY store-sourced string carries both shapes.

    Three writers, one per surface the routes serve: the terminal ``error``
    detail (``TaskStore.finish``), a nested event payload (an adapter's signal
    dict), and a ``WaitRecord`` reason built from a dependency adapter's
    ``detail`` (``dependency.py``'s ``f"{kind} from {source}: {detail}"``).
    """
    store.accept(
        [
            model.TaskRecord(id="leak-1", kind=model.KIND_SUBAGENT, session_key="web-a", params={}),
            model.TaskRecord(id="leak-2", kind=model.KIND_SUBAGENT, session_key="web-a", params={}),
        ]
    )
    for task_id in ("leak-1", "leak-2"):
        assert store.claim(task_id) is not None
        assert store.transition(task_id, model.STARTING)
        assert store.transition(task_id, model.RUNNING)
    # leak-1: a live wait whose reason came from an adapter, plus a nested
    # event payload -- a credential inside a list inside a dict is the same leak.
    wait = WaitRecord.dependency(
        "github:api",
        since=clock.t,
        retry_at=clock.t + 90.0,
        reason=f"http_error from gh: POST {_EXFIL_URL} rejected ({_CRED})",
    )
    assert store.enter_wait("leak-1", wait.to_dict())
    store.append_event(
        "leak-1",
        "progress",
        {"signal": {"detail": [_EXFIL_URL], "attempts": 2}, "note": f"key {_CRED}"},
    )
    # leak-2: the terminal error a subagent/tool failure stores verbatim.
    assert store.finish("leak-2", model.FAILED, error=f"boom: {_CRED} posted to {_EXFIL_URL}")


def _assert_scrubbed(raw: str | None) -> None:
    """No credential and no live exfiltration URL anywhere in the wire body.

    The URL rewriter's substitution NAMES the domain it removed
    (``[REDACTED: suspicious URL to <domain>]``, by design -- the operator has
    to see where the agent was posting), so the host alone is not the leak. The
    leak is the URL: its scheme, path and the credential-bearing query.
    """
    assert raw is not None
    assert _CRED not in raw, "a credential reached the wire"
    assert _EXFIL_URL not in raw, "a live exfiltration URL reached the wire"
    assert f"https://{_EXFIL_HOST}" not in raw, "a fetchable exfiltration URL reached the wire"


@pytest.mark.asyncio
async def test_task_routes_redact_store_prose(
    store: TaskStore, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every route that serves a store string scrubs it first.

    The store keeps a subagent/tool error and an adapter's wait reason
    VERBATIM (that is what makes the row diagnosable offline); the redaction
    belongs on the way out, on every surface, the way ``subagent.py`` and
    ``taskrunner.py`` already scrub the same strings for the WS frame and the
    run error.
    """
    _leaky_row(store, clock)
    monkeypatch.setattr(tasks_mod.resource_status, "adaptive_state", lambda: None)
    st = _state(store)

    detail = await tasks_mod.api_task_detail(
        _req("GET", "/api/tasks/leak-1", st, match={"task_id": "leak-1"})
    )
    _assert_scrubbed(detail.text)
    failed = await tasks_mod.api_task_detail(
        _req("GET", "/api/tasks/leak-2", st, match={"task_id": "leak-2"})
    )
    _assert_scrubbed(failed.text)
    listing = await tasks_mod.api_tasks_list(_req("GET", "/api/tasks", st))
    _assert_scrubbed(listing.text)
    summary = await tasks_mod.api_tasks_summary(_req("GET", "/api/tasks/summary", st))
    _assert_scrubbed(summary.text)
    cancel = await tasks_mod.api_task_cancel(
        _req("POST", "/api/tasks/leak-1/cancel", st, match={"task_id": "leak-1"})
    )
    _assert_scrubbed(cancel.text)

    # The strings still ARRIVE -- redacted, not dropped. A pin that only tested
    # for absence would pass on a route that stopped serving events at all.
    events = {e["kind"]: e for e in _body(detail)["events"]}
    nested = events["progress"]["data"]
    assert nested["signal"]["detail"][0].startswith("[REDACTED: suspicious URL to ")
    assert nested["signal"]["attempts"] == 2, "a non-string leaf survives unchanged"
    assert "[REDACTED: credential]" in nested["note"]
    assert _body(failed)["events"][-1]["data"]["error"].startswith("boom: [REDACTED: credential]")
    reason = _body(detail)["task"]["wait_reason"]
    assert reason.startswith("http_error from gh: POST [REDACTED: suspicious URL to ")
    assert "[REDACTED: credential]" in reason
    assert _body(detail)["task"]["wait"]["reason"] == reason


def test_redaction_runs_exfil_before_credentials() -> None:
    """The composition order, pinned on the case that separates the two.

    A URL whose query IS a credential is what the exfiltration rewriter exists
    for: it replaces the whole URL, DESTINATION included. Redacting the
    credential first leaves ``?token=[REDACTED: credential]`` -- a URL with
    nothing suspicious left in it, so the host the agent was posting to
    survives on the wire. ``subagent.py``'s ``_redact`` and
    ``security.redact_and_truncate`` both order it this way.
    """
    scrubbed = tasks_mod._scrub_prose(_EXFIL_URL)
    assert scrubbed == f"[REDACTED: suspicious URL to {_EXFIL_HOST}]"
    # Credentials-first would have produced this instead: the credential gone,
    # the destination and the whole fetchable URL still on the wire.
    from kiro_crew.security import redact_credentials, redact_exfiltration_urls

    creds_first, _ = redact_credentials(_EXFIL_URL)
    assert redact_exfiltration_urls(creds_first)[0] == creds_first
    assert f"https://{_EXFIL_HOST}" in creds_first


# ── POST /api/tasks/{id}/cancel ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cancel_goes_through_the_manager_not_the_store(
    store: TaskStore, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(store, clock)
    st = _state(store)
    # The handler must not call the store's own writers: guard every one.
    for name in ("cancel", "transition", "finish", "defer", "wake_wait", "enter_wait"):
        real = getattr(store, name)

        def _trip(*a: Any, _n: str = name, _real: Any = real, **kw: Any) -> Any:
            if st.subagents.cancel_calls and _n == "cancel":
                # The MANAGER's cascade is allowed to write (it did the call).
                return _real(*a, **kw)
            raise AssertionError(f"handler wrote the store directly via {_n}")

        monkeypatch.setattr(store, name, _trip)
    resp = await tasks_mod.api_task_cancel(
        _req("POST", "/api/tasks/q-1/cancel", st, match={"task_id": "q-1"})
    )
    assert resp.status == 200, resp.text
    body = _body(resp)
    assert body["ok"] is True and body["cancelled"] is True
    assert body["task"]["state"] == "cancelled"
    assert st.subagents.cancel_calls == ["q-1"]
    assert store.state_of("q-1") == "cancelled"


@pytest.mark.asyncio
async def test_cancel_of_a_live_wait_also_routes_through_the_manager(
    store: TaskStore, clock: Clock
) -> None:
    _seed(store, clock)
    st = _state(store)
    resp = await tasks_mod.api_task_cancel(
        _req("POST", "/api/tasks/inp-1/cancel", st, match={"task_id": "inp-1"})
    )
    assert resp.status == 200
    assert st.subagents.cancel_calls == ["inp-1"]
    assert store.state_of("inp-1") == "cancelled"


@pytest.mark.asyncio
async def test_cancel_terminal_row_is_409_and_never_calls_the_manager(
    store: TaskStore, clock: Clock
) -> None:
    _seed(store, clock)
    st = _state(store)
    resp = await tasks_mod.api_task_cancel(
        _req("POST", "/api/tasks/done-1/cancel", st, match={"task_id": "done-1"})
    )
    assert resp.status == 409
    assert _body(resp)["code"] == "terminal"
    assert st.subagents.cancel_calls == []
    assert store.state_of("done-1") == "done"


@pytest.mark.asyncio
async def test_cancel_without_an_adapter_is_409_and_leaves_the_row(
    store: TaskStore, clock: Clock
) -> None:
    _seed(store, clock)
    st = _state(store, honours=False)
    resp = await tasks_mod.api_task_cancel(
        _req("POST", "/api/tasks/run-1/cancel", st, match={"task_id": "run-1"})
    )
    assert resp.status == 409
    body = _body(resp)
    assert body["code"] == "no_cancel_adapter"
    assert body["task"]["state"] == "running"
    assert store.state_of("run-1") == "running"


@pytest.mark.asyncio
async def test_cancel_unknown_id_404(store: TaskStore) -> None:
    st = _state(store)
    resp = await tasks_mod.api_task_cancel(
        _req("POST", "/api/tasks/nope/cancel", st, match={"task_id": "nope"})
    )
    assert resp.status == 404
    assert st.subagents.cancel_calls == []


# ── POST /api/tasks/{id}: answer_input ───────────────────────────────────────


def _repark_on_a_dependency(store: TaskStore, task_id: str, clock: Clock) -> None:
    """The step's question ended elsewhere and the step parked on a dependency.

    Every write is the store's own: the wake takes the row to ``retry_wait``
    (generation+1), the re-dispatch claims it, and the new wait is entered from
    ``running`` -- which is the state the row is really in by the time an
    operator's answer to the OLD question reaches the store.
    """
    assert store.wake_wait(task_id, reason="answered on another gateway") is not None
    assert store.claim(task_id) is not None
    assert store.transition(task_id, model.STARTING)
    assert store.transition(task_id, model.RUNNING)
    dep = WaitRecord.dependency("github:api", since=clock.t, retry_at=clock.t + 30.0)
    assert store.enter_wait(task_id, dep.to_dict())


@pytest.mark.asyncio
async def test_answer_input_is_refused_when_the_row_moved_to_another_wait(
    store: TaskStore, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The route's read ROUTES the answer; the store's own fence decides it.

    The row is ``waiting_input`` when the handler reads it and in a DEPENDENCY
    wait by the time the write lands. An answer delivered there would end a wait
    it does not answer, report 200, and leave the row claimable with its
    dependency unmet -- so the handler hands the read's generation to
    ``answer_input``, which fences on ``waiting_input`` alone, and the operator
    gets the same 409 a later read would have earned.
    """
    row_id = "taskrunner:r:task1"
    store.accept([model.TaskRecord(id=row_id, kind=model.KIND_TASKRUNNER_STEP)])
    assert store.claim(row_id) is not None
    assert store.transition(row_id, model.STARTING)
    assert store.transition(row_id, model.RUNNING)
    inp = WaitRecord.input("call-9", since=clock.t, reason="a passphrase?")
    assert store.enter_wait(row_id, inp.to_dict())

    admission = taskq_runner.RunnerAdmission(store, lane=taskq_runner.RunnerLane(2), clock=clock)
    st = _state(store)
    st.task_runner = SimpleNamespace(task_admission=admission)
    real_get = store.get
    reads: list[str] = []

    def _move_under_the_read(task_id: str):
        record = real_get(task_id)
        reads.append(task_id)
        if len(reads) == 1:
            _repark_on_a_dependency(store, task_id, clock)
        return record

    monkeypatch.setattr(store, "get", _move_under_the_read)
    request = _req(
        "POST",
        f"/api/tasks/{row_id}",
        st,
        match={"task_id": row_id},
        body={"action": "answer_input", "answer": "the passphrase"},
    )
    resp = await tasks_mod.api_task_action(request)
    monkeypatch.setattr(store, "get", real_get)

    assert reads[0] == row_id, "the interleaving never ran, so this pin measured nothing"
    assert resp.status == 409
    body = _body(resp)
    assert body["code"] == "not_waiting_input"
    assert body["task"]["state"] == model.WAITING_DEPENDENCY
    # The wait the row is really in is untouched, wait record and all.
    live = real_get(row_id)
    assert live is not None and live.wait is not None
    assert live.wait["dependency_scope"] == "github:api"
    assert store.events(row_id)[-1].kind in {"rejected_transition", "stale_result"}


def _answer_elsewhere_and_ask_again(store: TaskStore, task_id: str, clock: Clock) -> None:
    """The question was answered on another surface and the step asked a NEW one.

    Every write is the store's own. The row ends where it started -- parked in
    ``waiting_input`` -- so the state fence sees nothing wrong; what moved is the
    GENERATION, because the wake took the row through ``retry_wait`` and a
    re-dispatch claimed it.
    """
    assert store.wake_wait(task_id, reason="answered on another gateway") is not None
    assert store.claim(task_id) is not None
    assert store.transition(task_id, model.STARTING)
    assert store.transition(task_id, model.RUNNING)
    again = WaitRecord.input("call-10", since=clock.t, reason="which environment?")
    assert store.enter_wait(task_id, again.to_dict())


@pytest.mark.asyncio
async def test_answer_input_is_fenced_by_the_generation_the_route_read(
    store: TaskStore, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An answer answers ONE question, and only the generation can say which.

    The row is ``waiting_input`` at the read AND at the write, so every state
    fence on this path -- the handler's pre-check and ``answer_input``'s own
    ``only_from`` -- is satisfied by a row that asks a DIFFERENT question from
    the one the operator answered. The passphrase would be delivered to "which
    environment?", reported 200, and the real question would be gone with no
    trace that it was never answered. Only ``generation=record.generation``
    refuses it, which is why deleting that one kwarg must not be a silent green.
    """
    row_id = "taskrunner:r:task1"
    store.accept([model.TaskRecord(id=row_id, kind=model.KIND_TASKRUNNER_STEP)])
    assert store.claim(row_id) is not None
    assert store.transition(row_id, model.STARTING)
    assert store.transition(row_id, model.RUNNING)
    inp = WaitRecord.input("call-9", since=clock.t, reason="a passphrase?")
    assert store.enter_wait(row_id, inp.to_dict())

    admission = taskq_runner.RunnerAdmission(store, lane=taskq_runner.RunnerLane(2), clock=clock)
    st = _state(store)
    st.task_runner = SimpleNamespace(task_admission=admission)
    real_get = store.get
    reads: list[str] = []

    def _move_under_the_read(task_id: str):
        record = real_get(task_id)
        reads.append(task_id)
        if len(reads) == 1:
            _answer_elsewhere_and_ask_again(store, task_id, clock)
        return record

    monkeypatch.setattr(store, "get", _move_under_the_read)
    request = _req(
        "POST",
        f"/api/tasks/{row_id}",
        st,
        match={"task_id": row_id},
        body={"action": "answer_input", "answer": "the passphrase"},
    )
    resp = await tasks_mod.api_task_action(request)
    monkeypatch.setattr(store, "get", real_get)

    assert reads[0] == row_id, "the interleaving never ran, so this pin measured nothing"
    live = real_get(row_id)
    assert live is not None and live.wait is not None
    assert (
        live.state == model.WAITING_INPUT
    ), "the state fence must be SATISFIED here or this pin measures the wrong fence"
    assert resp.status == 409
    body = _body(resp)
    # ``action_failed`` is the code ``_refused_code`` reserves for a refusal the
    # state does not explain -- which is exactly this one.
    assert body["code"] == "action_failed"
    # The question the row is really asking is untouched, and unanswered.
    assert live.wait["tool_call_id"] == "call-10"
    assert live.wait["reason"] == "which environment?"
    assert "the passphrase" not in json.dumps(live.wait)


@pytest.mark.asyncio
async def test_cancel_wait_is_fenced_by_the_generation_the_route_read(
    store: TaskStore, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The SIBLING fence on the same handler, and it is unpinned for the same
    reason: ``only_from=PARKED`` is satisfied by a row that got there twice.

    The row is parked at the read and parked again at the write, so the state
    fence sees a cancellable row both times -- but it is a DIFFERENT wait,
    entered by a re-dispatch the operator never saw. Without the read's
    generation the cancel commits: a wait nobody asked to cancel ends, and
    ``on_wake`` is delivered to a live executor that will then find its own row
    cancelled underneath it.
    """
    row_id = "taskrunner:r:task1"
    store.accept([model.TaskRecord(id=row_id, kind=model.KIND_TASKRUNNER_STEP)])
    assert store.claim(row_id) is not None
    assert store.transition(row_id, model.STARTING)
    assert store.transition(row_id, model.RUNNING)
    inp = WaitRecord.input("call-9", since=clock.t, reason="a passphrase?")
    assert store.enter_wait(row_id, inp.to_dict())

    admission = taskq_runner.RunnerAdmission(store, lane=taskq_runner.RunnerLane(2), clock=clock)
    st = _state(store)
    st.task_runner = SimpleNamespace(task_admission=admission)
    real_get = store.get
    reads: list[str] = []

    def _move_under_the_read(task_id: str):
        record = real_get(task_id)
        reads.append(task_id)
        if len(reads) == 1:
            _answer_elsewhere_and_ask_again(store, task_id, clock)
        return record

    monkeypatch.setattr(store, "get", _move_under_the_read)
    request = _req(
        "POST",
        f"/api/tasks/{row_id}",
        st,
        match={"task_id": row_id},
        body={"action": "cancel_wait"},
    )
    resp = await tasks_mod.api_task_action(request)
    monkeypatch.setattr(store, "get", real_get)

    assert reads[0] == row_id, "the interleaving never ran, so this pin measured nothing"
    live = real_get(row_id)
    assert live is not None
    assert (
        live.state in model.PARKED
    ), "the state fence must be SATISFIED here or this pin measures the wrong fence"
    assert resp.status == 409
    assert _body(resp)["code"] == "action_failed"
    # The wait the re-dispatch entered is still there, uncancelled.
    assert live.wait is not None and live.wait["tool_call_id"] == "call-10"


# ── the queued set: one constant, two surfaces ───────────────────────────────


def test_queued_set_is_one_constant_spelled_from_the_model() -> None:
    """The depth summary and ``session_health`` share the SET, not a copy of it.

    Identity, not equality: two lists that happen to match today are how one
    surface starts counting a state the other does not. The spelling is checked
    against ``taskq.model`` here because ``session_health`` writes it in
    literals on purpose -- it duck-types the store and must not import
    ``kiro_crew.taskq`` (``test_dashboard_handlers_lazy_tasks.py``).
    """
    # The handler must RESOLVE that object, not hold an equal one: `is` against the
    # attribute the handler itself reaches is the only spelling a module-level copy
    # or an inline literal cannot satisfy.
    assert tasks_mod.session_health.TASK_QUEUED_STATES is session_health.TASK_QUEUED_STATES
    assert "session_health.TASK_QUEUED_STATES" in inspect.getsource(tasks_mod._summary_sync)
    assert not hasattr(tasks_mod, "_QUEUED_STATES"), "a second queued set came back"
    assert session_health.TASK_QUEUED_STATES == {
        model.QUEUED,
        model.ADMITTED,
        model.RETRY_WAIT,
        model.WAITING_INFRA,
    }
    assert session_health.TASK_QUEUED_STATES <= model.STATES
    # A row waiting on infra holds no runtime, so it is queued-for-capacity and
    # never a live wait; the two sets must not overlap.
    assert not session_health.TASK_QUEUED_STATES & set(session_health._TASK_WAIT_STATES)
    assert set(session_health._TASK_WAIT_STATES) == set(model.WAITING)


@pytest.mark.asyncio
async def test_waiting_infra_is_queued_on_both_surfaces(
    store: TaskStore, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end: one ``waiting_infra`` row, both counts, one number.

    Drives the real handler and the real health monitor over the same store, so
    the pin fails if either surface stops counting the state -- which is what
    made the depth chip and ``/api/sessions/health`` disagree.
    """
    store.accept(
        [model.TaskRecord(id="infra-1", kind=model.KIND_SUBAGENT, session_key="web-a", params={})]
    )
    assert store.transition("infra-1", model.WAITING_INFRA)
    monkeypatch.setattr(tasks_mod.resource_status, "adaptive_state", lambda: None)
    st = _state(store)
    summary = _body(await tasks_mod.api_tasks_summary(_req("GET", "/api/tasks/summary", st)))
    assert summary["depth"]["by_state"] == {"waiting_infra": 1}
    assert summary["depth"]["queued"] == 1
    # Not a live wait: no runtime is held, so it is not in the waiting list.
    assert summary["waiting"] == [] and summary["depth"]["waiting"] == 0

    health = session_health.compute_session_health(state=st, taskq=store)
    assert health["queued"]["count"] == summary["depth"]["queued"]
    assert health["waiting"] == []


# ── GET /api/tasks/summary ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_summary_shape_depth_oldest_wait_and_waits(
    store: TaskStore, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(store, clock)
    monkeypatch.setattr(tasks_mod.resource_status, "adaptive_state", lambda: None)
    resp = await tasks_mod.api_tasks_summary(_req("GET", "/api/tasks/summary", _state(store)))
    assert resp.status == 200
    body = _body(resp)
    assert body["available"] is True
    assert set(body) >= {
        "generated_at",
        "depth",
        "oldest_wait_secs",
        "lanes",
        "degrade_reason",
        "adaptive",
        "slots",
        "waiting",
        "recovering",
        "stalled",
        "counts",
    }
    depth = body["depth"]
    assert depth["by_state"] == {
        "queued": 2,
        "retry_wait": 1,
        "running": 1,
        "waiting_dependency": 1,
        "waiting_input": 1,
        "recovering": 1,
        "done": 1,
    }
    assert depth["queued"] == 3  # queued + admitted + retry_wait + waiting_infra
    assert depth["waiting"] == 2
    # recovering + retry_wait: both are rows being retried (retry_wait also
    # counts as queued above -- it waits for capacity too).
    assert depth["recovering"] == 2
    assert depth["running"] == 1
    assert depth["total"] == 8
    # Oldest claimable row was accepted at t=1000 and the clock is at 1090.
    assert body["oldest_wait_secs"] == pytest.approx(90.0)
    waits = {w["id"]: w for w in body["waiting"]}
    assert (
        waits["dep-1"]["wait_reason"] and waits["dep-1"]["wait"]["dependency_scope"] == "github:api"
    )
    assert waits["inp-1"]["state"] == "waiting_input"
    assert waits["inp-1"]["age_secs"] == pytest.approx(60.0)
    rec = body["recovering"]
    assert sorted(r["id"] for r in rec["tasks"]) == ["q-2", "rec-1"]
    assert rec["task_attempts"] == 2
    assert rec["slots"] == []
    assert {row["layer"] for row in rec["ladder"]} >= {"L1_tool_call", "L4_gatewayd"}
    # The manager's live cap lands as the subagents lane even without a controller.
    assert body["lanes"]["subagents"]["effective"] == 4
    assert body["degrade_reason"] is None
    assert body["adaptive"] is None


@pytest.mark.asyncio
async def test_every_depth_number_counts_the_same_rows_as_by_state(
    store: TaskStore, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No two numbers in one response may disagree about how many rows there are.

    The row LIST is a page: ``retry_wait`` rows hold no runtime, so they are read
    by a bounded ``list_rows`` rather than by ``active_rows``. Counting
    ``depth.recovering`` off that page would make the page size a silent cap on a
    COUNT -- and the card computes "waiting for a slot" as ``depth.queued`` less
    the uncapped ``by_state.retry_wait``, so every row past the page would appear
    in neither number. The page size is shrunk here so the boundary is what is
    measured rather than the volume.
    """
    _seed(store, clock)
    monkeypatch.setattr(tasks_mod, "_LANE_SCAN_LIMIT", 1)
    monkeypatch.setattr(tasks_mod.resource_status, "adaptive_state", lambda: None)
    # A second retry_wait row, so the page cannot hold them both.
    store.accept([model.TaskRecord(id="q-3", kind=model.KIND_SUBAGENT, session_key="web-a")])
    assert store.claim("q-3") is not None
    assert store.transition("q-3", model.STARTING)
    assert store.transition("q-3", model.RETRY_WAIT, next_run_at=clock.t + 9.0)

    resp = await tasks_mod.api_tasks_summary(_req("GET", "/api/tasks/summary", _state(store)))
    body = _body(resp)
    depth = body["depth"]
    assert depth["by_state"]["retry_wait"] == 2 and depth["by_state"]["recovering"] == 1
    assert depth["recovering"] == 3, "a page size capped a count"
    # The list stays a page, and the card's own subtraction still reaches every
    # claimable row: "waiting for a slot" (queued LESS retry_wait) plus this count.
    assert len(body["recovering"]["tasks"]) == 2
    assert depth["queued"] - depth["by_state"]["retry_wait"] + depth["recovering"] == sum(
        depth["by_state"].get(s, 0) for s in model.CLAIMABLE
    )


@pytest.mark.asyncio
async def test_summary_merges_adaptive_caps_and_degrade_reason(
    store: TaskStore, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(store, clock)
    fake_adaptive = {
        "enabled": True,
        "mode": "aimd",
        "effective_exec_cap": 2,
        "exec_ceiling": 8,
        "spawn_gate_capacity": 3,
        "gate_ceiling": 8,
        "paused": False,
        "probing": False,
        "last": {"action": "decrease", "reason": "loop_lag=410ms,timeouts=0.4"},
    }
    monkeypatch.setattr(tasks_mod.resource_status, "adaptive_state", lambda: fake_adaptive)
    body = _body(
        await tasks_mod.api_tasks_summary(_req("GET", "/api/tasks/summary", _state(store)))
    )
    lanes = body["lanes"]
    # The health monitor's own reading (the live manager cap) wins for `effective`;
    # the controller supplies the ceiling and the gate lane.
    assert lanes["subagents"]["effective"] == 4
    assert lanes["subagents"]["user_max"] == 8
    assert lanes["spawn_gate"] == {"effective": 3, "user_max": 8}
    assert body["degrade_reason"] == "loop_lag=410ms,timeouts=0.4"
    assert body["adaptive"]["effective_exec_cap"] == 2


@pytest.mark.asyncio
async def test_summary_prefers_a_registered_pressure_source(
    store: TaskStore, clock: Clock, monkeypatch: pytest.MonkeyPatch, _fresh_monitor
) -> None:
    _seed(store, clock)
    _fresh_monitor.register_pressure_source(lambda: "memory_critical")
    _fresh_monitor.register_cap_source("spawn_gate", lambda: {"effective": 1, "user_max": 8})
    monkeypatch.setattr(
        tasks_mod.resource_status, "adaptive_state", lambda: {"paused": True, "last": None}
    )
    body = _body(
        await tasks_mod.api_tasks_summary(_req("GET", "/api/tasks/summary", _state(store)))
    )
    assert body["degrade_reason"] == "memory_critical"
    assert body["lanes"]["spawn_gate"]["effective"] == 1


@pytest.mark.asyncio
async def test_summary_paused_controller_reads_as_degraded(
    store: TaskStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        tasks_mod.resource_status,
        "adaptive_state",
        lambda: {"paused": True, "last": {"action": "pause", "reason": "memory<2GB"}},
    )
    body = _body(
        await tasks_mod.api_tasks_summary(_req("GET", "/api/tasks/summary", _state(store)))
    )
    assert body["degrade_reason"] == "memory<2GB"
    assert body["depth"]["total"] == 0 and body["available"] is True


@pytest.mark.asyncio
async def test_summary_without_a_store_is_empty_not_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tasks_mod.resource_status, "adaptive_state", lambda: None)
    state = SimpleNamespace(subagents=SimpleNamespace(_taskq=None), _slots={})
    resp = await tasks_mod.api_tasks_summary(_req("GET", "/api/tasks/summary", state))
    assert resp.status == 200
    body = _body(resp)
    assert body["available"] is False
    assert body["depth"]["total"] == 0
    assert body["waiting"] == [] and body["recovering"]["tasks"] == []


@pytest.mark.asyncio
async def test_summary_survives_a_mock_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """A MagicMock state (much of the suite) must read as unavailable, not 500."""
    monkeypatch.setattr(tasks_mod.resource_status, "adaptive_state", lambda: None)
    app = web.Application()
    app["state"] = MagicMock()
    resp = await tasks_mod.api_tasks_summary(
        make_mocked_request("GET", "/api/tasks/summary", app=app)
    )
    assert resp.status == 200
    assert _body(resp)["available"] is False


# ── auth ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/api/tasks"),
        ("GET", "/api/tasks/summary"),
        ("GET", "/api/tasks/abc"),
        ("POST", "/api/tasks/abc/cancel"),
    ],
)
async def test_routes_are_refused_without_a_token(method: str, path: str) -> None:
    """Drive the REAL token middleware: no cookie, no token, no internal secret."""
    from kiro_crew.dashboard.server import _MIXED_INTERNAL_API_PATHS, _STRICT_INTERNAL_API_PATHS
    from kiro_crew.dashboard.token_auth import token_auth_middleware

    # Not internal routes: an MCP caller with only the loopback secret is
    # refused too, exactly like /api/sessions/health.
    assert path not in _STRICT_INTERNAL_API_PATHS and path not in _MIXED_INTERNAL_API_PATHS

    mw = token_auth_middleware(
        internal_paths=_STRICT_INTERNAL_API_PATHS,
        mixed_internal_paths=_MIXED_INTERNAL_API_PATHS,
        internal_secret="s3cret",
    )
    reached: list[str] = []

    async def _handler(_request: web.Request) -> web.Response:
        reached.append(_request.path)
        return web.json_response({"ok": True})

    for headers in ({}, {"X-Internal-Secret": "s3cret"}):
        req = MagicMock(spec=web.Request)
        req.path = path
        req.query = {}
        req.cookies = {}
        req.remote = "127.0.0.1"
        req.headers = headers
        req.method = method
        resp = await mw(req, _handler)
        assert resp.status in (401, 403), f"{method} {path} admitted with {headers}: {resp.status}"
    assert reached == []


def test_routes_are_registered_in_order() -> None:
    """``/summary`` must be registered before ``/{task_id}`` or it is swallowed."""
    from kiro_crew.dashboard.routes import system as system_routes

    app = web.Application()
    system_routes.register(app)
    paths = [
        r.resource.canonical
        for r in app.router.routes()
        if getattr(r.resource, "canonical", "").startswith("/api/tasks")
    ]
    assert paths.index("/api/tasks/summary") < paths.index("/api/tasks/{task_id}")
    assert "/api/tasks/{task_id}/cancel" in paths
    assert "/api/tasks" in paths
