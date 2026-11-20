"""The owner's approval routes: only the host owner lists, reads and answers tool
approvals, each under one ``apr_`` id that names one request in this gateway process."""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, Mock

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state
from jsonschema import Draft202012Validator
from test_shared_crew_teammate_owner_access import _publish

from kiro_crew.dashboard import crew_api_approvals as approvals
from kiro_crew.dashboard.deny_override import DENY_OVERRIDE_META_KEY
from kiro_crew.dashboard.routes import crew_api
from kiro_crew.dashboard.state import SlotOrigin
from kiro_crew.testing.wait import async_wait_until, await_until

CONTRACT = json.loads(
    (Path(__file__).parent / "fixtures" / "crew_api_owner_openapi.json").read_text(encoding="utf-8")
)
OWNER = "U_OWNER"
AS_OWNER = {"X-Test-User": OWNER}
AS_APP = {"X-Test-User": "notely", "X-Test-App": "notely"}
LIST = "/v1/crew/owner/approvals"
SLOT = "selected"


def _valid(name: str, body: Any) -> None:
    schema = {"$ref": f"#/components/schemas/{name}", "components": CONTRACT["components"]}
    Draft202012Validator(schema).validate(body)


def _decision(approval_id: str) -> str:
    return f"{LIST}/{approval_id}/decision"


@web.middleware
async def _identity(request: web.Request, handler: Any) -> web.StreamResponse:
    """Stands in for token auth: who the caller is, as token auth would have set it."""
    if request.headers.get("X-Test-Internal"):
        # A loopback X-Internal-Secret caller: no app claim at all.
        request["internal_auth"] = True
        request["user"] = OWNER
        return await handler(request)
    request["user"] = request.headers.get("X-Test-User", "")
    request["app"] = request.headers.get("X-Test-App", "")
    if teammate := request.headers.get("X-Test-Teammate"):
        _publish(request, teammate)
    return await handler(request)


@pytest.fixture(autouse=True)
def _hermetic_home(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))


def _state(tmp_path, monkeypatch) -> Any:
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    state.owner_id = OWNER
    return state


def _serve(state: Any) -> TestClient:
    """A gateway process over *state*, with its own in-memory index."""
    app = web.Application(middlewares=[_identity])
    app["state"] = state
    crew_api.register(app)
    return TestClient(TestServer(app))


@pytest_asyncio.fixture
async def client(tmp_path, monkeypatch):
    audit = Mock(spec=["log_api_access"])
    monkeypatch.setattr("kiro_crew.sel.sel", lambda: audit)
    state = _state(tmp_path, monkeypatch)
    async with _serve(state) as test_client:
        test_client.state = state
        test_client.audit = audit
        yield test_client


async def _background(state: Any, request_id: str, slot: str = "") -> asyncio.Task:
    """A background approval (a cron job's, a subagent's) waiting under *request_id*."""
    record = state._pending_approvals.get(request_id)
    task = asyncio.get_running_loop().create_task(
        state.request_approval(request_id, "subagent", "fs_write", tool_input="a.txt", slot=slot)
    )
    await async_wait_until(
        lambda: state._pending_approvals.get(request_id) not in (None, record),
        describe=lambda: f"approval {request_id!r} never registered",
    )
    return task


def _native(state: Any, request_id: str, slot_key: str = SLOT, **meta: str) -> asyncio.Future:
    """A chat turn's own tool prompt waiting on *slot_key* under *request_id*."""
    slot = state.get_or_create_slot(slot_key, origin=SlotOrigin.USER)
    future = asyncio.get_running_loop().create_future()
    fields = {"request_id": request_id, "tool_name": "shell", "tool_input": "ls", **meta}
    row = slot.append("permission", "Tool", json.dumps(fields))
    slot.register_approval(request_id, future, row)
    return future


async def _cancel(*tasks: asyncio.Task) -> None:
    for task in tasks:
        task.cancel()
    await await_until(asyncio.gather(*tasks, return_exceptions=True), what="approval cleanup")


async def _listed(client, **params: str) -> list[dict[str, Any]]:
    response = await client.get(LIST, params=params, headers=AS_OWNER)
    body = await response.json()
    assert response.status == 200, body
    _valid("OwnerApprovalList", body)
    return body["data"]


CALLERS = [
    AS_APP,
    {"X-Test-User": "123456789"},
    {},
    {"X-Test-Internal": "1"},
    {"X-Test-Teammate": "relay"},
    {"X-Test-Teammate": "ssm"},
    {"X-Test-Teammate": "tailscale"},
]


@pytest.mark.asyncio
@pytest.mark.parametrize("caller", CALLERS)
async def test_only_the_host_owner_reaches_the_routes(client, monkeypatch, caller):
    monkeypatch.setenv("KIROCREW_SHARED_CREW_PREVIEW", "1")
    task = await _background(client.state, "rid")
    future = _native(client.state, "rid")
    try:
        ids = [row["id"] for row in await _listed(client)]
        for approval_id in ids:
            refusals = [
                await client.get(LIST, headers=caller),
                await client.get(f"{LIST}/{approval_id}", headers=caller),
                await client.post(
                    _decision(approval_id), json={"decision": "allow"}, headers=caller
                ),
            ]
            for refused in refusals:
                body = await refused.json()
                assert refused.status == 403
                _valid("Error", body)
                assert body["error"]["code"] == "owner_only"
                assert body["error"]["request_id"] == refused.headers["X-Request-Id"]
        assert len(ids) == 2 and not task.done() and not future.done()
        assert [row["id"] for row in await _listed(client)] == ids
    finally:
        await _cancel(task)


@pytest.mark.asyncio
async def test_the_owner_lists_reads_and_filters_what_is_waiting(client):
    state = client.state
    task = await _background(state, "rid-bg", slot="cron-slot")
    _native(state, "rid", tool_purpose="List the files")
    _native(state, "rid", slot_key="app-slot")
    state._slots["app-slot"]._app = "notely"
    # An MCP server's question shares the registry and is not a tool approval.
    slot = state._slots[SLOT]
    asked = slot.append("elicitation", "Q", json.dumps({"request_id": "elicit"}))
    slot.register_approval("elicit", asyncio.get_running_loop().create_future(), asked)
    try:
        rows = await _listed(client)
        assert [(r["conversation"], r["client_id"], r["tool"]) for r in rows] == [
            ("cron-slot", None, "fs_write"),
            (SLOT, None, "shell"),
            ("app-slot", "notely", "shell"),
        ]
        assert rows[1]["reason"] == "List the files"
        assert rows[1]["arguments"] == {"input": "ls"}
        assert all(r["state"] == "pending" and r["decided_by"] is None for r in rows)
        assert len({r["id"] for r in rows}) == 3
        # The ids are stable: a second read gives the same ones.
        assert await _listed(client) == rows
        assert await _listed(client, conversation=SLOT) == [rows[1]]
        assert await _listed(client, client_id="notely") == [rows[2]]
        assert await _listed(client, state="allowed") == []
        first = await client.get(LIST, params={"limit": "2"}, headers=AS_OWNER)
        page = await first.json()
        assert (page["data"], page["has_more"], page["last_id"]) == (rows[:2], True, rows[1]["id"])
        assert await _listed(client, after=rows[1]["id"], limit="2") == [rows[2]]
        read = await client.get(f"{LIST}/{rows[0]['id']}", headers=AS_OWNER)
        assert (read.status, await read.json()) == (200, rows[0])
        _valid("OwnerApproval", rows[0])
        missing = await client.get(f"{LIST}/apr_{'0' * 32}", headers=AS_OWNER)
        assert (missing.status, (await missing.json())["error"]["code"]) == (404, "not_found")
        for bad in (
            {"state": "open"},
            {"limit": "0"},
            {"limit": "101"},
            {"limit": "9" * 5000},
            {"after": "apr_x"},
        ):
            refused = await client.get(LIST, params=bad, headers=AS_OWNER)
            assert (refused.status, (await refused.json())["error"]["code"]) == (
                400,
                "invalid_request",
            )
    finally:
        await _cancel(task)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("decision", "ran", "state_name"), [("allow", True, "allowed"), ("deny", False, "denied")]
)
async def test_an_answer_settles_a_background_approval_once(client, decision, ran, state_name):
    task = await _background(client.state, "rid")
    try:
        (row,) = await _listed(client)
        answered = await client.post(
            _decision(row["id"]), json={"decision": decision}, headers=AS_OWNER
        )
        body = await answered.json()
        assert answered.status == 200, body
        _valid("OwnerApproval", body)
        assert body["state"] == state_name
        assert body["decided_by"] == {"kind": "owner", "principal": OWNER, "via": "api"}
        assert await await_until(task, what="the approval request resolving") is ran
        client.audit.log_api_access.assert_any_call(
            caller=OWNER,
            operation="crew_api.approvals.decide",
            outcome=state_name,
            source="crew-api",
            resources=f"approval={row['id']}",
            error="",
        )
        again = await client.post(
            _decision(row["id"]), json={"decision": "allow"}, headers=AS_OWNER
        )
        assert (again.status, (await again.json())["error"]["code"]) == (
            409,
            "approval_already_resolved",
        )
        read = await client.get(f"{LIST}/{row['id']}", headers=AS_OWNER)
        assert await read.json() == body
        assert await _listed(client) == [] and await _listed(client, state=state_name) == [body]
    finally:
        await _cancel(task)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("decision", "result", "state_name"),
    [("allow", "approved", "allowed"), ("deny", "rejected_once", "denied")],
)
async def test_an_answer_settles_a_chat_turns_own_prompt(client, decision, result, state_name):
    future = _native(client.state, "rid")
    other = _native(client.state, "rid", slot_key="other")
    rows = await _listed(client)
    answered = await client.post(
        _decision(rows[0]["id"]), json={"decision": decision}, headers=AS_OWNER
    )
    assert answered.status == 200 and (await answered.json())["state"] == state_name
    # The same caller id on another session is another request, and stays open.
    assert future.result() == result and not other.done()
    assert [r["id"] for r in await _listed(client)] == [rows[1]["id"]]


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["coordinator", "native"])
async def test_an_id_never_answers_a_later_request_under_the_same_caller_id(client, origin):
    state = client.state
    if origin == "coordinator":
        first = await _background(state, "rid", slot=SLOT)
    else:
        first = _native(state, "rid")
    (row,) = await _listed(client)
    # The first request ends and a second one takes its caller id; no route ran between.
    if origin == "coordinator":
        await _cancel(first)
        second = await _background(state, "rid", slot=SLOT)
        still_open = lambda: not second.done()  # noqa: E731
    else:
        first.cancel()
        later = _native(state, "rid")
        still_open = lambda: not later.done()  # noqa: E731
    try:
        answered = await client.post(
            _decision(row["id"]), json={"decision": "allow"}, headers=AS_OWNER
        )
        assert (answered.status, (await answered.json())["error"]["code"]) == (
            409,
            "approval_already_resolved",
        )
        assert still_open()
        (replacement,) = await _listed(client)
        assert replacement["id"] != row["id"]
        (old,) = await _listed(client, state="expired")
        assert (old["id"], old["expired_reason"], old["decided_by"]) == (row["id"], "timeout", None)
    finally:
        if origin == "coordinator":
            await _cancel(second)


@pytest.mark.asyncio
async def test_an_ending_made_elsewhere_is_read_from_the_request_itself(client):
    state = client.state
    task = await _background(state, "rid-bg")
    stopped = _native(state, "rid")
    rows = await _listed(client)
    assert state.resolve_state_approval("rid-bg", True)  # the dashboard's answer
    stopped.set_result("rejected")  # a Stop
    assert await await_until(task, what="the approval request resolving") is True
    ended = [await (await client.get(f"{LIST}/{r['id']}", headers=AS_OWNER)).json() for r in rows]
    assert [(r["state"], r["decided_by"]) for r in ended] == [("allowed", None), ("denied", None)]
    late = await client.post(_decision(rows[0]["id"]), json={"decision": "deny"}, headers=AS_OWNER)
    assert late.status == 409


@pytest.mark.asyncio
async def test_after_a_restart_old_ids_are_unknown_and_waiting_requests_get_new_ids(
    client, tmp_path, monkeypatch
):
    task = await _background(client.state, "rid", slot=SLOT)
    _native(client.state, "rid")
    before = await _listed(client)
    await _cancel(task)
    # A new process has a new state and index. The caller ids can repeat.
    restarted = _state(tmp_path / "restarted", monkeypatch)
    again = await _background(restarted, "rid", slot=SLOT)
    future = _native(restarted, "rid")
    try:
        async with _serve(restarted) as second:
            assert await _listed(second, state="expired") == []
            for row in before:
                read = await second.get(f"{LIST}/{row['id']}", headers=AS_OWNER)
                assert (read.status, (await read.json())["error"]["code"]) == (404, "not_found")
                answered = await second.post(
                    _decision(row["id"]), json={"decision": "allow"}, headers=AS_OWNER
                )
                assert (answered.status, (await answered.json())["error"]["code"]) == (
                    404,
                    "not_found",
                )
            assert not again.done() and not future.done()
            waiting = await _listed(second)
            assert len(waiting) == 2 and not {r["id"] for r in waiting} & {r["id"] for r in before}
    finally:
        await _cancel(again)


@pytest.mark.asyncio
async def test_a_security_rule_override_card_is_denied_here_and_allowed_only_on_the_dashboard(
    client,
):
    future = _native(client.state, "rid", **{DENY_OVERRIDE_META_KEY: "1"})
    (row,) = await _listed(client)
    allowed = await client.post(_decision(row["id"]), json={"decision": "allow"}, headers=AS_OWNER)
    assert (allowed.status, (await allowed.json())["error"]["code"]) == (
        403,
        "approval_requires_dashboard",
    )
    assert not future.done()
    denied = await client.post(_decision(row["id"]), json={"decision": "deny"}, headers=AS_OWNER)
    assert denied.status == 200 and future.result() == "rejected_once"


@pytest.mark.asyncio
async def test_a_slot_with_nothing_waiting_never_has_its_rows_read(client, monkeypatch):
    """Reading a restored session's rows loads its transcript, so an idle slot's stay unread."""
    future = _native(client.state, "rid")
    client.state.get_or_create_slot("idle", origin=SlotOrigin.USER)
    unread = {"idle"}
    rows = type(client.state._slots[SLOT]).messages

    def read(slot: Any) -> list[dict[str, Any]]:
        assert slot.key not in unread, f"read the rows of {slot.key!r}"
        return rows.fget(slot)

    monkeypatch.setattr(type(client.state._slots[SLOT]), "messages", property(read, rows.fset))
    (row,) = await _listed(client)
    future.set_result(True)
    unread.add(SLOT)
    assert (await _listed(client, state="allowed"))[0]["id"] == row["id"]
    late = await client.post(_decision(row["id"]), json={"decision": "allow"}, headers=AS_OWNER)
    assert late.status == 409


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [{"decision": "allow_always"}, {"decision": "allow", "scope": "once"}, {}, ["allow"], None],
)
async def test_a_body_that_is_not_allow_or_deny_answers_nothing(client, body):
    future = _native(client.state, "rid")
    (row,) = await _listed(client)
    refused = await client.post(_decision(row["id"]), json=body, headers=AS_OWNER)
    assert (refused.status, (await refused.json())["error"]["code"]) == (400, "invalid_request")
    assert not future.done()


def test_the_record_keeps_its_pending_rows_and_drops_the_oldest_ended_ones(tmp_path, monkeypatch):
    monkeypatch.setattr(approvals, "MAX_RECORDS", 2)
    index = approvals.ApprovalIndex()

    def live(name: str) -> approvals.LiveApproval:
        return approvals.LiveApproval(("native", "s", name), name, None, "t", "", "", None)

    index.sync([live("a"), live("b")], {})
    index.sync([live("b"), live("c")], {})
    records = index.sync([live("b"), live("c"), live("d")], {})
    assert [(r["instance"], r["state"]) for r in records.values()] == [
        ("b", "pending"),
        ("c", "pending"),
        ("d", "pending"),
    ]
    assert all(r["namespace"] == "default" for r in records.values())


@pytest.mark.parametrize("outcome", ["allowed", "denied", "expired"])
def test_a_stale_snapshot_never_mints_an_id_for_an_ended_target(tmp_path, outcome):
    index = approvals.ApprovalIndex(clock=lambda: 42)
    live = [approvals.LiveApproval(("native", "s", "a"), "rid", None, "t", "", "", None)]
    (approval_id,) = index.sync(live, {})
    index.settle(approval_id, outcome, OWNER)
    records = index.sync(live, {})
    assert list(records) == [approval_id]
    assert records[approval_id]["state"] == outcome
    assert records[approval_id]["decided_by"] == {"kind": "owner", "principal": OWNER, "via": "api"}


@pytest.mark.asyncio
async def test_a_full_index_decision_is_settled_before_a_queued_read(client, monkeypatch):
    monkeypatch.setattr(approvals, "MAX_RECORDS", 2)
    _native(client.state, "oldest")
    _native(client.state, "second")
    oldest, _ = await _listed(client)
    read_done = asyncio.get_running_loop().create_future()
    settled_before_yield = asyncio.get_running_loop().create_future()
    index = client.state.__dict__["_crew_api_approval_index"]
    settle = Mock(wraps=index.settle)
    monkeypatch.setattr(index, "settle", settle)
    answer = approvals._answer

    async def read_after_answer():
        try:
            response = await client.get(f"{LIST}/{oldest['id']}", headers=AS_OWNER)
            read_done.set_result((response.status, await response.json()))
        except Exception as exc:
            read_done.set_exception(exc)

    def answer_and_queue(*args):
        result = answer(*args)
        asyncio.get_running_loop().call_soon(lambda: settled_before_yield.set_result(settle.called))
        asyncio.get_running_loop().create_task(read_after_answer())
        return result

    monkeypatch.setattr(approvals, "_answer", answer_and_queue)
    response = await client.post(
        _decision(oldest["id"]), json={"decision": "allow"}, headers=AS_OWNER
    )
    body = await response.json()
    assert response.status == 200
    assert body["state"] == "allowed"
    assert body["decided_by"] == {"kind": "owner", "principal": OWNER, "via": "api"}
    assert await await_until(read_done, what="the queued read") == (200, body)
    assert await await_until(settled_before_yield, what="the settlement before yielding")
    read = await client.get(f"{LIST}/{oldest['id']}", headers=AS_OWNER)
    assert (read.status, await read.json()) == (200, body)


@pytest.mark.asyncio
async def test_an_override_refusal_writes_one_denied_audit_row(client):
    future = _native(client.state, "rid", **{DENY_OVERRIDE_META_KEY: "1"})
    (row,) = await _listed(client)
    client.audit.reset_mock()
    response = await client.post(_decision(row["id"]), json={"decision": "allow"}, headers=AS_OWNER)
    assert response.status == 403 and not future.done()
    client.audit.log_api_access.assert_called_once_with(
        caller=OWNER,
        operation="crew_api.approvals.decide",
        outcome="denied",
        error="approval_requires_dashboard",
        source="crew-api",
        resources=f"approval={row['id']}",
    )


@pytest.mark.asyncio
async def test_a_resolved_refusal_writes_one_denied_audit_row(client):
    future = _native(client.state, "rid")
    (row,) = await _listed(client)
    future.set_result("approved")
    client.audit.reset_mock()
    response = await client.post(_decision(row["id"]), json={"decision": "allow"}, headers=AS_OWNER)
    assert response.status == 409
    client.audit.log_api_access.assert_called_once_with(
        caller=OWNER,
        operation="crew_api.approvals.decide",
        outcome="denied",
        error="approval_already_resolved",
        source="crew-api",
        resources=f"approval={row['id']}",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 403, 409])
async def test_a_failed_audit_does_not_change_the_decision_response(client, status, caplog):
    meta = {DENY_OVERRIDE_META_KEY: "1"} if status == 403 else {}
    future = _native(client.state, "rid", **meta)
    (row,) = await _listed(client)
    if status == 409:
        future.set_result("rejected_once")
    loop_thread = threading.get_ident()
    audit_threads = []

    def fail_audit(**fields):
        audit_threads.append(threading.get_ident())
        raise OSError("private audit failure detail")

    client.audit.log_api_access.side_effect = fail_audit
    response = await client.post(_decision(row["id"]), json={"decision": "allow"}, headers=AS_OWNER)
    assert response.status == status
    client.audit.log_api_access.assert_called_once()
    assert len(audit_threads) == 1 and audit_threads[0] != loop_thread
    assert "Owner approval API audit failed" in caplog.text
    assert "private audit failure detail" not in caplog.text
    if status == 200:
        assert future.result() == "approved"
        body = await response.json()
        assert body["state"] == "allowed"
        assert body["decided_by"] == {"kind": "owner", "principal": OWNER, "via": "api"}
        read = await client.get(f"{LIST}/{row['id']}", headers=AS_OWNER)
        assert (read.status, await read.json()) == (200, body)
    elif status == 403:
        assert not future.done()


def test_a_missing_watched_ending_expires_as_timeout():
    index = approvals.ApprovalIndex(clock=lambda: 42)
    live = [approvals.LiveApproval(("native", "s", "a"), "rid", None, "t", "", "", None)]
    (approval_id,) = index.sync(live, {})
    record = index.sync([], {})[approval_id]
    assert (record["state"], record["expired_reason"]) == ("expired", "timeout")
    assert record["decided_by"] is None


# -- The answer layer, called directly ---------------------------------------
#
# ``decide_approval`` syncs the index before it answers, so over HTTP an ended
# request is already refused by its record's state. These tests call ``_answer``
# on a record the sync has not caught up with, so each of its own checks decides.


def _stale_record(origin: str, **fields: str) -> dict[str, Any]:
    return {"origin": origin, "slot": SLOT, "instance": "inst", "request_id": "rid", **fields}


def _done_future() -> asyncio.Future:
    future = asyncio.get_running_loop().create_future()
    future.set_result(True)
    return future


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("pending", "open_future"),
    [
        # Settled, and its waiting task has not removed the record yet.
        ({"instance": "inst", "slot": SLOT}, False),
        # The future was dropped while the record is still there.
        ({"instance": "inst", "slot": SLOT}, None),
        # Another request holds the caller id now.
        ({"instance": "other", "slot": SLOT}, True),
        ({"instance": "inst", "slot": "elsewhere"}, True),
        (None, True),
    ],
)
async def test_the_answer_layer_refuses_a_background_request_that_is_not_the_one_waiting(
    monkeypatch, pending, open_future
):
    from kiro_crew.shared_crew import decisions

    resolver = Mock(return_value=(200, {"ok": True}))
    monkeypatch.setattr(decisions, "resolve_state_approval", resolver)
    loop = asyncio.get_running_loop()
    future = (
        None if open_future is None else loop.create_future() if open_future else _done_future()
    )
    state = Mock(spec=["_pending_approvals", "_approval_futures"])
    state._pending_approvals = {"rid": pending} if pending is not None else {}
    state._approval_futures = {"rid": future} if future is not None else {}
    assert approvals._answer(state, Mock(), _stale_record("coordinator"), True) is False
    resolver.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["settled", "no future", "another instance", "no slot"])
async def test_the_answer_layer_refuses_a_chat_prompt_that_is_not_the_one_waiting(
    monkeypatch, case
):
    from kiro_crew.shared_crew import decisions

    resolver = Mock(return_value=(200, {"ok": True}))
    monkeypatch.setattr(decisions, "resolve_pinned_slot_approval", resolver)
    slot = Mock(spec=["_approval_futures", "approval_instance"])
    # The slot reports the instance in every case but one, so the future's own
    # state is what refuses a settled or missing one.
    slot.approval_instance = Mock(return_value="other" if case == "another instance" else "inst")
    future = _done_future() if case == "settled" else asyncio.get_running_loop().create_future()
    slot._approval_futures = {} if case == "no future" else {"rid": future}
    state = Mock(spec=["_slots"])
    state._slots = {} if case == "no slot" else {SLOT: slot}
    assert approvals._answer(state, Mock(), _stale_record("native"), True) is False
    resolver.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["coordinator", "native"])
@pytest.mark.parametrize("decision", ["allow", "deny"])
async def test_a_record_that_is_not_pending_is_refused_before_the_answer_layer(
    client, monkeypatch, origin, decision
):
    """Neither reader of the live registries runs for a record that is not waiting."""
    state = client.state
    if origin == "coordinator":
        task = await _background(state, "rid")
        (row,) = await _listed(client)
        await _cancel(task)
    else:
        future = _native(state, "rid")
        (row,) = await _listed(client)
        future.set_result("rejected")  # ended elsewhere: a Stop
        _native(state, "rid")  # a later request waits under the same caller id
    answer = Mock(return_value=True)
    card = Mock(return_value=True)
    monkeypatch.setattr(approvals, "_answer", answer)
    monkeypatch.setattr("kiro_crew.dashboard.deny_override.is_deny_override_card", card)
    late = await client.post(_decision(row["id"]), json={"decision": decision}, headers=AS_OWNER)
    assert (late.status, (await late.json())["error"]["code"]) == (409, "approval_already_resolved")
    answer.assert_not_called()
    card.assert_not_called()


@pytest.mark.asyncio
async def test_an_ended_id_is_409_when_an_override_card_took_its_caller_id(client):
    first = _native(client.state, "rid")
    (row,) = await _listed(client)
    first.cancel()
    later = _native(client.state, "rid", **{DENY_OVERRIDE_META_KEY: "1"})
    answered = await client.post(_decision(row["id"]), json={"decision": "allow"}, headers=AS_OWNER)
    assert (answered.status, (await answered.json())["error"]["code"]) == (
        409,
        "approval_already_resolved",
    )
    assert not later.done()


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["coordinator", "native"])
async def test_two_answers_at_once_settle_the_request_once(client, origin):
    state = client.state
    task = await _background(state, "rid") if origin == "coordinator" else None
    future = None if origin == "coordinator" else _native(state, "rid")
    try:
        (row,) = await _listed(client)
        allow, deny = await asyncio.gather(
            client.post(_decision(row["id"]), json={"decision": "allow"}, headers=AS_OWNER),
            client.post(_decision(row["id"]), json={"decision": "deny"}, headers=AS_OWNER),
        )
        assert sorted([allow.status, deny.status]) == [200, 409]
        winner = allow if allow.status == 200 else deny
        settled = await winner.json()
        loser = await (deny if winner is allow else allow).json()
        assert loser["error"]["code"] == "approval_already_resolved"
        read = await client.get(f"{LIST}/{row['id']}", headers=AS_OWNER)
        assert await read.json() == settled
        ran = settled["state"] == "allowed"
        if task is not None:
            assert await await_until(task, what="the approval request resolving") is ran
        else:
            assert future.result() == ("approved" if ran else "rejected_once")
    finally:
        if task is not None:
            await _cancel(task)


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["coordinator", "native"])
async def test_an_answer_after_the_turn_ended_runs_nothing(client, origin):
    state = client.state
    if origin == "coordinator":
        task = await _background(state, "rid")
        (row,) = await _listed(client)
        await _cancel(task)  # the turn that waited was cancelled
    else:
        future = _native(state, "rid")
        (row,) = await _listed(client)
        future.cancel()
    late = await client.post(_decision(row["id"]), json={"decision": "allow"}, headers=AS_OWNER)
    assert (late.status, (await late.json())["error"]["code"]) == (409, "approval_already_resolved")
    read = await (await client.get(f"{LIST}/{row['id']}", headers=AS_OWNER)).json()
    assert (read["state"], read["expired_reason"], read["decided_by"]) == (
        "expired",
        "timeout",
        None,
    )
    if origin == "coordinator":
        assert task.done() and "rid" not in state._approval_futures


@pytest.mark.asyncio
async def test_an_answer_the_layer_refused_is_409_and_settles_nothing(client, monkeypatch):
    future = _native(client.state, "rid")
    (row,) = await _listed(client)
    monkeypatch.setattr(approvals, "_answer", Mock(return_value=False))
    refused = await client.post(_decision(row["id"]), json={"decision": "allow"}, headers=AS_OWNER)
    assert (refused.status, (await refused.json())["error"]["code"]) == (
        409,
        "approval_already_resolved",
    )
    assert not future.done()
    assert await _listed(client) == [row]
