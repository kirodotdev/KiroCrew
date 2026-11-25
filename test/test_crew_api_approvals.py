"""The owner's approval routes: only the host owner lists, reads and answers tool
approvals, each under one ``apr_`` id that names one request in this gateway process."""

from __future__ import annotations

import asyncio
import gc
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

from conftest import requires_symlinks
from kiro_crew.dashboard import crew_api_approval_records as records
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


def _record_file(home: Path) -> Path:
    """Where a gateway whose data home is *home* keeps the approval records."""
    return home / "member-records" / records.APPROVALS_KIND / "state.json"


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
async def test_after_a_restart_old_ids_read_expired_and_waiting_requests_get_new_ids(
    client, tmp_path, monkeypatch
):
    task = await _background(client.state, "rid", slot=SLOT)
    _native(client.state, "rid", tool_input="ls /secret-place")
    answered_future = _native(client.state, "answered", slot_key="other")
    before = await _listed(client)
    assert [r["conversation"] for r in before] == [SLOT, SLOT, "other"]
    done = await client.post(
        _decision(before[2]["id"]), json={"decision": "allow"}, headers=AS_OWNER
    )
    assert done.status == 200 and answered_future.done()
    # The gateway closes its sessions for good, then the turns are cut.
    client.state.sessions.final_drain_started = True
    await _cancel(task)
    assert (await _listed(client))[0]["id"] == before[0]["id"]

    # Only ids, states, times and sessions are in the file: no tool, no argument.
    text = _record_file(tmp_path).read_text(encoding="utf-8")
    stored = json.loads(text)["approvals"]
    assert [set(row) for row in stored] == [set(records.FIELDS)] * 3
    assert [row["state"] for row in stored] == ["pending", "pending", "allowed"]
    for hidden in ("secret-place", "shell", "fs_write", "a.txt", OWNER):
        assert hidden not in text

    # A new process has a new state and index. The caller ids can repeat.
    restarted = _state(tmp_path / "restarted", monkeypatch)
    async with _serve(restarted) as second:
        assert await _listed(second) == []
        old = await _listed(second, state="expired")
        assert [r["id"] for r in old] == [r["id"] for r in before[:2]]
        for row in old:
            _valid("OwnerApproval", row)
            assert row["expired_reason"] == "gateway_restart"
            assert (row["tool"], row["reason"], row["arguments"]) == ("", "", {"input": ""})
            assert (row["conversation"], row["client_id"], row["decided_by"]) == (SLOT, None, None)
            assert row["decided_at"] is not None
            answered = await second.post(
                _decision(row["id"]), json={"decision": "allow"}, headers=AS_OWNER
            )
            assert (answered.status, (await answered.json())["error"]["code"]) == (
                409,
                "approval_already_resolved",
            )
        kept = await second.get(f"{LIST}/{before[2]['id']}", headers=AS_OWNER)
        assert (await kept.json())["state"] == "allowed"

        # A turn resumed in the session asks again under new ids; the old records
        # stay expired as they were.
        again = await _background(restarted, "rid", slot=SLOT)
        future = _native(restarted, "rid")
        waiting = await _listed(second)
        assert not again.done() and not future.done()
        assert len(waiting) == 2 and not {r["id"] for r in waiting} & {r["id"] for r in before}
        assert await _listed(second, state="expired") == old
        await _cancel(again)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    [
        "not json",
        '{"approvals": [{"id": "apr_x"}]}',
        '{"approvals": "none"}',
        json.dumps({"approvals": [dict.fromkeys(records.FIELDS, "x")]}),
        json.dumps(
            {
                "approvals": [
                    {
                        **dict.fromkeys(records.FIELDS),
                        "id": "apr_" + "a" * 32,
                        "state": "pending",
                        "created_at": 1,
                        "slot": "s",
                        "extra": None,
                    }
                ]
            }
        ),
        "[" * 100_000,
    ],
    ids=[
        "not-json",
        "missing-fields",
        "not-a-list",
        "wrong-values",
        "an-extra-field",
        "nested-too-deep",
    ],
)
async def test_a_record_file_that_cannot_be_believed_is_set_aside_and_live_approvals_go_on(
    client, tmp_path, content, caplog
):
    path = _record_file(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    future = _native(client.state, "rid")
    # The nested case ends in RecursionError on purpose, so the cyclic collector is
    # held off for the read (testing-conventions: no sweep at the recursion limit).
    gc.collect()
    gc.disable()
    try:
        with caplog.at_level("WARNING", logger=approvals.logger.name):
            (row,) = await _listed(client)
    finally:
        gc.enable()
        gc.collect()
    # The damaged file is kept under a dated name, and a new one holds the live id.
    (aside,) = [p for p in path.parent.iterdir() if p.name.startswith("state.json.damaged-")]
    assert aside.read_text(encoding="utf-8") == content
    assert str(aside) in caplog.text and content not in caplog.text
    assert [r["id"] for r in json.loads(path.read_text())["approvals"]] == [row["id"]]
    # An id from before the damage is unknown; the live request is answered.
    old = await client.get(f"{LIST}/apr_{'0' * 32}", headers=AS_OWNER)
    assert (old.status, (await old.json())["error"]["code"]) == (404, "not_found")
    answered = await client.post(_decision(row["id"]), json={"decision": "allow"}, headers=AS_OWNER)
    assert answered.status == 200 and future.result() == "approved"
    assert row["id"].startswith("apr_") and len(row["id"]) == 36


@pytest.mark.asyncio
async def test_a_record_file_that_is_not_valid_and_cannot_be_moved_is_503_and_gives_no_id(
    client, tmp_path, monkeypatch
):
    content = "not json"
    path = _record_file(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    monkeypatch.setattr(records, "set_aside", Mock(side_effect=OSError("read-only")))
    future = _native(client.state, "rid")
    for response in (
        await client.get(LIST, headers=AS_OWNER),
        await client.get(f"{LIST}/apr_{'0' * 32}", headers=AS_OWNER),
        await client.post(
            _decision(f"apr_{'0' * 32}"), json={"decision": "allow"}, headers=AS_OWNER
        ),
    ):
        body = await response.json()
        assert (response.status, body["error"]["code"]) == (503, "instance_unavailable")
        _valid("Error", body)
    assert not future.done() and path.read_text(encoding="utf-8") == content


@pytest.mark.asyncio
async def test_a_record_file_that_cannot_be_read_is_left_in_place_and_read_once_it_can_be(
    client, tmp_path, monkeypatch, caplog
):
    path = _record_file(tmp_path)
    old = {
        **dict.fromkeys(records.FIELDS),
        "id": "apr_" + "a" * 32,
        "state": "allowed",
        "created_at": 1,
        "decided_at": 2,
        "slot": "s",
    }
    records.save(path, [old])
    content = path.read_bytes()
    future = _native(client.state, "rid")
    with monkeypatch.context() as broken, caplog.at_level("WARNING", logger=approvals.logger.name):
        # A read that fails says nothing about the content: it may be a good record.
        broken.setattr(records, "load", Mock(side_effect=PermissionError("held open")))
        for response in (
            await client.get(LIST, headers=AS_OWNER),
            await client.get(f"{LIST}/{old['id']}", headers=AS_OWNER),
            await client.post(_decision(old["id"]), json={"decision": "deny"}, headers=AS_OWNER),
        ):
            body = await response.json()
            assert (response.status, body["error"]["code"]) == (503, "instance_unavailable")
            _valid("Error", body)
    assert path.read_bytes() == content
    assert not [p for p in path.parent.iterdir() if p.name.startswith("state.json.damaged-")]
    assert str(path) in caplog.text and old["id"] not in caplog.text
    assert content.decode("utf-8").strip() not in caplog.text
    assert not future.done()
    # Once the file can be read, the next call reads it and the old id is back.
    kept = await client.get(f"{LIST}/{old['id']}", headers=AS_OWNER)
    assert (kept.status, (await kept.json())["state"]) == (200, "allowed")
    late = await client.post(_decision(old["id"]), json={"decision": "deny"}, headers=AS_OWNER)
    assert (late.status, (await late.json())["error"]["code"]) == (409, "approval_already_resolved")


@pytest.mark.asyncio
async def test_an_id_that_cannot_be_written_is_not_announced_and_an_answer_still_lands(
    client, monkeypatch
):
    future = _native(client.state, "rid")
    with monkeypatch.context() as broken:
        broken.setattr(records, "save", Mock(side_effect=OSError("disk")))
        listed = await client.get(LIST, headers=AS_OWNER)
        assert (listed.status, (await listed.json())["error"]["code"]) == (
            503,
            "instance_unavailable",
        )
    row = (await _listed(client))[0]
    monkeypatch.setattr(records, "save", Mock(side_effect=OSError("disk")))
    answered = await client.post(_decision(row["id"]), json={"decision": "allow"}, headers=AS_OWNER)
    assert answered.status == 200 and (await answered.json())["state"] == "allowed"
    assert future.result() == "approved"


def _stored_states(home: Path) -> list[str]:
    # The gateway's own reader: off the loop it rides out the Windows window in which
    # a concurrent save's replace denies the open.
    return [row["state"] for row in records.load(_record_file(home))]


def _stopping(state: Any, monkeypatch) -> Any:
    """A gateway about to run its graceful stop's teardown over *state*."""
    from slack_gateway_helpers import _make_orchestrator, _mock_sessions

    from kiro_crew.slack import gateway

    async def unwound(_state: Any) -> None:
        return None

    monkeypatch.setattr(gateway, "wait_for_unwinding_turns", unwound)
    orch = _make_orchestrator(test_mode=True)
    orch.sessions = _mock_sessions()
    orch.dashboard_state = state
    return orch


async def _states_after_restart(home: Path, monkeypatch, ids: list[str]) -> list[tuple[str, Any]]:
    """What the next gateway process over *home*'s record reads for *ids*."""
    async with _serve(_state(home / "restarted", monkeypatch)) as second:
        read = [await (await second.get(f"{LIST}/{i}", headers=AS_OWNER)).json() for i in ids]
    return [(row["state"], row["expired_reason"]) for row in read]


@pytest.mark.asyncio
async def test_an_ending_made_on_another_surface_is_written_when_it_happens(
    client, tmp_path, monkeypatch
):
    task = await _background(client.state, "bg", slot=SLOT)
    approved = _native(client.state, "approved")
    rejected = _native(client.state, "rejected")
    unanswered = await _background(client.state, "unanswered")
    before = await _listed(client)
    assert _stored_states(tmp_path) == ["pending"] * 4
    # The dashboard's resolvers settle the same futures; no Owner route runs afterwards.
    client.state._approval_futures["bg"].set_result(True)
    approved.set_result("approved")
    rejected.set_result("rejected_once")
    await _cancel(unanswered)
    await async_wait_until(
        lambda: _stored_states(tmp_path) == ["allowed", "expired", "allowed", "denied"],
        describe=lambda: f"the file holds {_stored_states(tmp_path)}",
        off_loop=True,
    )
    assert await task is True

    restarted = _state(tmp_path / "restarted", monkeypatch)
    async with _serve(restarted) as second:
        after = {}
        for row in before:
            read = await (await second.get(f"{LIST}/{row['id']}", headers=AS_OWNER)).json()
            after[row["id"]] = (read["state"], read["expired_reason"])
        assert list(after.values()) == [
            ("allowed", None),
            ("expired", "timeout"),
            ("allowed", None),
            ("denied", None),
        ]


@pytest.mark.asyncio
async def test_a_timeout_while_the_gateway_is_not_closing_is_written_as_timeout(
    client, tmp_path, monkeypatch
):
    # No session is closing. The wait's own timer is not run here: when it runs out,
    # ``wait_for`` cancels the request's future, and the test does that at a moment
    # it chooses, after the owner has listed the request.
    task = await _background(client.state, "slow", slot=SLOT)
    (row,) = await _listed(client)
    client.state._approval_futures["slow"].cancel()
    assert await await_until(task, what="the approval wait to end") is False
    await async_wait_until(
        lambda: _stored_states(tmp_path) == ["expired"],
        describe=lambda: f"the file holds {_stored_states(tmp_path)}",
        off_loop=True,
    )
    restarted = _state(tmp_path / "restarted", monkeypatch)
    async with _serve(restarted) as second:
        _native(restarted, "new")
        assert len(await _listed(second)) == 1
        read = await (await second.get(f"{LIST}/{row['id']}", headers=AS_OWNER)).json()
        assert (read["state"], read["expired_reason"]) == ("expired", "timeout")


@pytest.mark.asyncio
async def test_an_update_pause_is_not_the_gateway_closing(client, tmp_path):
    # An update pauses turn admission and can resume it: no session ends for good.
    client.state.sessions.admission_closed = True
    client.state.sessions.final_drain_started = False
    task = await _background(client.state, "paused", slot=SLOT)
    (row,) = await _listed(client)
    await _cancel(task)
    await async_wait_until(
        lambda: _stored_states(tmp_path) == ["expired"],
        describe=lambda: f"the file holds {_stored_states(tmp_path)}",
        off_loop=True,
    )
    read = await (await client.get(f"{LIST}/{row['id']}", headers=AS_OWNER)).json()
    assert (read["state"], read["expired_reason"]) == ("expired", "timeout")


@pytest.mark.asyncio
async def test_a_loaded_index_does_not_wait_for_a_blocked_store(client, monkeypatch):
    index = approvals.gateway_approval_index(client.state)
    await index.load()
    save, entered, release = records.save, threading.Event(), threading.Event()

    def slow_save(path, rows):
        entered.set()
        if not release.wait(60):
            raise TimeoutError("the test never released the write")
        save(path, rows)

    monkeypatch.setattr(records, "save", slow_save)
    loop = asyncio.get_running_loop()
    writing = loop.create_task(index.store())
    loading = None
    try:
        await async_wait_until(entered.is_set, describe=lambda: "the write did not start")
        assert index._loaded and index._file_lock.locked()
        loading = loop.create_task(index.load())
        progressed = asyncio.Event()
        # Queued after the load task: it has returned or yielded on the held lock.
        loop.call_soon(progressed.set)
        await await_until(progressed, what="the loaded index to get its loop turn")
        assert loading.done(), "an already loaded index waited for the blocked write"
        await loading
        assert not release.is_set() and not writing.done() and index._file_lock.locked()
    finally:
        release.set()
        await await_until(writing, what="the blocked store to finish")
        if loading is not None:
            await await_until(loading, what="the index load to finish")


async def _cancel_delivered(task: asyncio.Task) -> None:
    task.cancel()
    # A loop callback queued after cancel observes its delivery, not elapsed time.
    delivered = asyncio.Event()
    asyncio.get_running_loop().call_soon(delivered.set)
    await await_until(delivered, what="the cancellation to be delivered")


@pytest.mark.asyncio
async def test_a_cancelled_first_load_keeps_the_file_lock_until_its_read_ends(client, monkeypatch):
    index = approvals.gateway_approval_index(client.state)
    load, entered, release = records.load, threading.Event(), threading.Event()

    def slow_load(path):
        entered.set()
        if not release.wait(60):
            raise TimeoutError("the test never released the read")
        return load(path)

    monkeypatch.setattr(records, "load", slow_load)
    loading = asyncio.get_running_loop().create_task(index.load())
    try:
        await async_wait_until(entered.is_set, describe=lambda: "the read did not start")
        await _cancel_delivered(loading)
        # The worker still runs, so a later load or store must still wait for it.
        assert index._file_lock.locked() and not loading.done()
    finally:
        release.set()
        await await_until(
            asyncio.gather(loading, return_exceptions=True), what="the cancelled load to end"
        )
    assert loading.cancelled() and not index._file_lock.locked() and not index._loaded


@pytest.mark.asyncio
async def test_a_cancelled_store_whose_write_fails_still_reports_the_cancellation(
    client, monkeypatch, caplog
):
    index = approvals.gateway_approval_index(client.state)
    await index.load()
    entered, release = threading.Event(), threading.Event()

    def failing_save(path, rows):
        entered.set()
        if not release.wait(60):
            raise TimeoutError("the test never released the write")
        raise OSError("no space left on device")

    monkeypatch.setattr(records, "save", failing_save)
    writing = asyncio.get_running_loop().create_task(index.store())
    try:
        await async_wait_until(entered.is_set, describe=lambda: "the write did not start")
        await _cancel_delivered(writing)
        assert index._file_lock.locked() and not writing.done()
    finally:
        with caplog.at_level("WARNING", logger=approvals.logger.name):
            release.set()
            await await_until(
                asyncio.gather(writing, return_exceptions=True), what="the cancelled store to end"
            )
    assert writing.cancelled(), "the write's error replaced the cancellation"
    assert "write failed after its wait was cancelled" in caplog.text
    assert not index._file_lock.locked()


@pytest.mark.asyncio
async def test_a_cancelled_store_whose_write_landed_writes_the_same_rows_once_more(
    client, monkeypatch
):
    _native(client.state, "rid")
    (row,) = await _listed(client)
    index = approvals.gateway_approval_index(client.state)
    save, entered, release = records.save, threading.Event(), threading.Event()
    calls: list[list[dict[str, Any]]] = []

    def counted_save(path, rows):
        calls.append([dict(r) for r in rows])
        if len(calls) == 1:
            entered.set()
            if not release.wait(60):
                raise TimeoutError("the test never released the write")
        save(path, rows)

    monkeypatch.setattr(records, "save", counted_save)
    # The listing wrote the row; a changed record makes the next store write.
    index._records[row["id"]]["state"] = "denied"
    writing = asyncio.get_running_loop().create_task(index.store())
    try:
        await async_wait_until(entered.is_set, describe=lambda: "the write did not start")
        await _cancel_delivered(writing)
    finally:
        release.set()
        await await_until(
            asyncio.gather(writing, return_exceptions=True), what="the cancelled store to end"
        )
    assert writing.cancelled() and len(calls) == 1
    (first,) = calls
    await index.store()
    assert len(calls) == 2 and calls[1] == first
    stored = records.load(records.record_path())
    assert stored == first and [r["id"] for r in stored] == [row["id"]]
    assert index._saved == first
    await index.store()
    assert len(calls) == 2, "an unchanged record was written a third time"


@pytest.mark.asyncio
async def test_a_planned_restart_waits_for_a_record_still_being_written(
    client, tmp_path, monkeypatch
):
    from kiro_crew.dashboard import restart_resume

    native = _native(client.state, "rid")
    # A turn is running, so the restart has a record of its own to write.
    client.state._slots[SLOT]._turn_in_flight_generation = 1
    await _listed(client)
    save, release = records.save, threading.Event()

    def slow_save(path, rows):
        if not release.wait(60):
            raise TimeoutError("the test never released the write")
        save(path, rows)

    monkeypatch.setattr(records, "save", slow_save)
    # No bound here: this test is about the wait, and the bound has its own test below.
    monkeypatch.setattr(approvals, "RESTART_WRITE_WAIT_SECS", None)
    native.set_result("approved")
    # The restart reaches its record step while the ending's write is still running.
    index = approvals.gateway_approval_index(client.state)
    await async_wait_until(lambda: bool(index._writes), describe=lambda: "no write started")
    finish_writes, waiting = index.finish_writes, asyncio.Event()

    async def finish_writes_seen():
        waiting.set()
        await finish_writes()

    monkeypatch.setattr(index, "finish_writes", finish_writes_seen)
    recording = asyncio.get_running_loop().create_task(
        restart_resume.record_running_slots(client.state)
    )
    try:
        # The restart is past its own record and inside the wait for this one.
        await await_until(waiting, what="the restart to reach the wait")
        assert _stored_states(tmp_path) == ["pending"] and not recording.done()
    finally:
        release.set()
    await await_until(recording, what="the restart's record step")
    assert _stored_states(tmp_path) == ["allowed"]


@pytest.mark.asyncio
async def test_a_planned_restart_waits_for_the_write_of_an_answer_a_route_gave(
    client, tmp_path, monkeypatch
):
    native = _native(client.state, "rid")
    (row,) = await _listed(client)
    save, release, entered = records.save, threading.Event(), threading.Event()

    def slow_save(path, rows):
        entered.set()
        if not release.wait(60):
            raise TimeoutError("the test never released the write")
        save(path, rows)

    monkeypatch.setattr(records, "save", slow_save)
    monkeypatch.setattr(approvals, "RESTART_WRITE_WAIT_SECS", None)
    loop = asyncio.get_running_loop()
    answering = loop.create_task(
        client.post(_decision(row["id"]), json={"decision": "allow"}, headers=AS_OWNER)
    )
    try:
        # The tool has its answer; the route's own write of it is still running.
        await async_wait_until(entered.is_set, describe=lambda: "the route's write did not start")
        assert native.result() == "approved"
        index = approvals.gateway_approval_index(client.state)
        store, waiting = index.store, asyncio.Event()

        async def store_seen():
            waiting.set()
            await store()

        monkeypatch.setattr(index, "store", store_seen)
        draining = loop.create_task(approvals.finish_writes_before_restart(client.state))
        await await_until(waiting, what="the restart to reach the write")
        assert _stored_states(tmp_path) == ["pending"] and not draining.done()
    finally:
        release.set()
    await await_until(draining, what="the restart's wait")
    assert _stored_states(tmp_path) == ["allowed"]
    assert (await await_until(answering, what="the owner's answer")).status == 200


@pytest.mark.asyncio
async def test_a_write_that_never_ends_does_not_hold_the_restart(client, monkeypatch, caplog):
    native = _native(client.state, "rid")
    await _listed(client)
    release = threading.Event()
    monkeypatch.setattr(records, "save", lambda path, rows: release.wait(10))
    # flake-ok: a zero bound runs out at once whatever the host's speed; no race is timed.
    monkeypatch.setattr(approvals, "RESTART_WRITE_WAIT_SECS", 0)  # flake-ok: see above
    native.set_result("approved")
    index = approvals.gateway_approval_index(client.state)
    await async_wait_until(lambda: bool(index._writes), describe=lambda: "no write started")
    with caplog.at_level("WARNING", logger=approvals.logger.name):
        await approvals.finish_writes_before_restart(client.state)
    release.set()
    # The warning counts the rows the file lacks, and names nothing else.
    assert "were not all written before the restart: 1 not written" in caplog.text


@pytest.mark.asyncio
async def test_a_graceful_stop_waits_for_answers_made_just_before_it(client, tmp_path, monkeypatch):
    from kiro_crew.slack import gateway

    allowed = _native(client.state, "yes")
    denied = _native(client.state, "no", slot_key="other")
    ids = [row["id"] for row in await _listed(client)]
    save, release = records.save, threading.Event()

    def slow_save(path, rows):
        if not release.wait(60):
            raise TimeoutError("the test never released the write")
        save(path, rows)

    monkeypatch.setattr(records, "save", slow_save)
    # No bound here: this test is about the wait, and the bound has its own test below.
    monkeypatch.setattr(gateway, "PERSISTENCE_DRAIN_GRACE_SECS", None)
    # Answered on another surface: the write is a task nothing but the stop waits for.
    allowed.set_result("approved")
    denied.set_result("rejected_once")
    index = approvals.gateway_approval_index(client.state)
    await async_wait_until(lambda: bool(index._writes), describe=lambda: "no write started")
    finish_writes, waiting = index.finish_writes, asyncio.Event()

    async def finish_writes_seen():
        waiting.set()
        await finish_writes()

    monkeypatch.setattr(index, "finish_writes", finish_writes_seen)
    orch = _stopping(client.state, monkeypatch)
    stopping = asyncio.get_running_loop().create_task(orch._stop_services_and_sessions())
    try:
        await await_until(waiting, what="the stop to reach the wait")
        # The sessions are closed by now, so no later answer can arrive.
        orch.sessions.close_all.assert_awaited_once()
        assert _stored_states(tmp_path) == ["pending", "pending"] and not stopping.done()
    finally:
        release.set()
    await await_until(stopping, what="the stop's teardown")
    assert await _states_after_restart(tmp_path, monkeypatch, ids) == [
        ("allowed", None),
        ("denied", None),
    ]


@pytest.mark.asyncio
async def test_a_write_that_never_ends_does_not_hold_the_stop_and_its_row_reads_expired(
    client, tmp_path, monkeypatch, caplog
):
    from kiro_crew.slack import gateway

    native = _native(client.state, "rid")
    (row,) = await _listed(client)
    save, release, stuck = records.save, threading.Event(), iter([True])

    def save_stuck_once(path, rows):
        # Only this process's write never ends; the next process writes as usual.
        if next(stuck, False):
            release.wait(10)
        else:
            save(path, rows)

    monkeypatch.setattr(records, "save", save_stuck_once)
    # flake-ok: a zero bound runs out at once whatever the host's speed; no race is timed.
    monkeypatch.setattr(gateway, "PERSISTENCE_DRAIN_GRACE_SECS", 0)  # flake-ok: see above
    native.set_result("approved")
    index = approvals.gateway_approval_index(client.state)
    await async_wait_until(lambda: bool(index._writes), describe=lambda: "no write started")
    orch = _stopping(client.state, monkeypatch)
    try:
        with caplog.at_level("WARNING", logger=approvals.logger.name):
            await await_until(
                asyncio.create_task(orch._stop_services_and_sessions()),
                what="the stop's teardown",
            )
        assert "were not all written before the stop: 1 not written" in caplog.text
        # The answer never reached the file, so the next process reads the row as cut.
        assert await _states_after_restart(tmp_path, monkeypatch, [row["id"]]) == [
            ("expired", "gateway_restart")
        ]
    finally:
        release.set()
        await await_until(index._exit_write, what="the drain the stop left running")


@pytest.mark.asyncio
async def test_a_graceful_stop_with_nothing_to_write_leaves_the_record_alone(
    client, tmp_path, monkeypatch
):
    _native(client.state, "rid")
    await _listed(client)
    save = Mock()
    monkeypatch.setattr(records, "save", save)
    # A gateway whose record is written, and one whose Owner routes were never called.
    for state in (client.state, _state(tmp_path / "unused", monkeypatch)):
        orch = _stopping(state, monkeypatch)
        await await_until(
            asyncio.create_task(orch._stop_services_and_sessions()), what="the stop's teardown"
        )
    save.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", ["restart", "caller"])
async def test_a_cancelled_wait_never_lets_an_older_write_replace_a_later_answer(
    client, tmp_path, monkeypatch, caplog, cancel
):
    first = _native(client.state, "first")
    later = _native(client.state, "later", slot_key="later")
    _, row = await _listed(client)
    index = approvals.gateway_approval_index(client.state)
    save = records.save
    entered, release, finished, later_written = (threading.Event() for _ in range(4))

    def slow_save(path, rows):
        if rows[1]["state"] == "pending":
            entered.set()
            if not release.wait(60):
                raise TimeoutError("the test never released the older write")
            try:
                save(path, rows)
            finally:
                finished.set()
        else:
            save(path, rows)
            later_written.set()

    monkeypatch.setattr(records, "save", slow_save)
    # flake-ok: zero expires immediately; the worker-entry event establishes the order.
    monkeypatch.setattr(approvals, "RESTART_WRITE_WAIT_SECS", 0)
    first.set_result("approved")
    answering = None
    writing = None
    try:
        await async_wait_until(entered.is_set, describe=lambda: "the older write did not start")
        (writing,) = index._writes
        if cancel == "restart":
            finish_writes, draining_seen = index.finish_writes, asyncio.Event()

            async def drain_started():
                draining_seen.set()
                await finish_writes()

            draining = asyncio.create_task(drain_started())
            await await_until(draining_seen, what="the drain to enter its write wait")
            monkeypatch.setattr(index, "finish_writes", lambda: draining)
        if cancel == "caller":
            writing.cancel()
            # A loop callback queued after cancel observes its delivery, not elapsed time.
            delivered = asyncio.Event()
            asyncio.get_running_loop().call_soon(delivered.set)
            await await_until(delivered, what="the store caller's cancellation to be delivered")
        with caplog.at_level("WARNING", logger=approvals.logger.name):
            await await_until(
                asyncio.create_task(approvals.finish_writes_before_restart(client.state)),
                what="the restart's bound",
            )
        assert "Owner approval records were not all written before the restart" in caplog.text
        assert not release.is_set()
        store, store_entered = index.store, asyncio.Event()

        async def later_store_seen():
            store_entered.set()
            await store()

        monkeypatch.setattr(index, "store", later_store_seen)
        answering = asyncio.get_running_loop().create_task(
            client.post(_decision(row["id"]), json={"decision": "allow"}, headers=AS_OWNER)
        )
        await await_until(store_entered, what="the later answer to reach its write")
        assert later.result() == "approved"
        # The wrapper enters store before yielding back to the test. Fixed code
        # still holds the lock; broken code can finish the later write first.
        if not writing.done():
            assert index._file_lock.locked() and not later_written.is_set()
        else:
            await async_wait_until(
                later_written.is_set, describe=lambda: "the later write did not finish"
            )
            assert (await await_until(answering, what="the later owner's response")).status == 200
            assert _stored_states(tmp_path) == ["allowed", "allowed"]
    finally:
        release.set()
        if writing is not None:
            await await_until(
                asyncio.gather(writing, return_exceptions=True), what="the older store to end"
            )
        await async_wait_until(finished.is_set, describe=lambda: "the older worker did not finish")
        if answering is not None:
            assert (await await_until(answering, what="the later owner's response")).status == 200
        if draining := getattr(index, "_exit_write", None):
            await await_until(draining, what="the retained restart drain")
    assert _stored_states(tmp_path) == ["allowed", "allowed"]
    assert [record["state"] for record in index._saved] == ["allowed", "allowed"]
    await index.store()
    assert _stored_states(tmp_path) == ["allowed", "allowed"]


@pytest.mark.asyncio
@pytest.mark.parametrize("broken", ["the record", "the write"])
async def test_a_failing_record_never_changes_what_the_tool_is_told(
    client, monkeypatch, caplog, broken
):
    task = await _background(client.state, "bg")
    native = _native(client.state, "rid")
    rows = await _listed(client)
    index = approvals.gateway_approval_index(client.state)
    if broken == "the record":
        monkeypatch.setattr(index, "_clock", Mock(side_effect=RuntimeError("clock")))
    else:
        monkeypatch.setattr(records, "save", Mock(side_effect=OSError("disk")))
    with caplog.at_level("WARNING", logger=approvals.logger.name):
        client.state._approval_futures["bg"].set_result(True)
        native.set_result("approved")
        assert await task is True and native.result() == "approved"
        await async_wait_until(
            lambda: all(row["id"] in caplog.text for row in rows),
            describe=lambda: f"logged: {caplog.text!r}",
        )
    assert "disk" not in caplog.text and "clock" not in caplog.text


@pytest.mark.asyncio
async def test_a_decision_whose_write_fails_is_200_and_the_warning_names_the_id(
    client, monkeypatch, caplog
):
    future = _native(client.state, "rid")
    row = (await _listed(client))[0]
    monkeypatch.setattr(records, "save", Mock(side_effect=OSError("disk")))
    with caplog.at_level("WARNING", logger=approvals.logger.name):
        answered = await client.post(
            _decision(row["id"]), json={"decision": "deny"}, headers=AS_OWNER
        )
    assert answered.status == 200 and (await answered.json())["state"] == "denied"
    assert future.result() == "rejected_once"
    assert f"write for {row['id']} failed" in caplog.text


def test_the_record_file_round_trips_and_refuses_a_repeated_id(tmp_path):
    path = tmp_path / "kind" / "state.json"
    assert records.load(path) == []
    row = dict.fromkeys(records.FIELDS)
    row.update(id="apr_" + "a" * 32, state="pending", created_at=7, slot="s")
    records.save(path, [{**row, "tool": "never stored"}])
    assert records.load(path) == [row]
    assert "never stored" not in path.read_text(encoding="utf-8")
    records.save(path, [row, row])
    with pytest.raises(ValueError):
        records.load(path)
    # Two files set aside in the same second are both kept.
    first = records.set_aside(path, 0)
    path.write_text("second", encoding="utf-8")
    second = records.set_aside(path, 0)
    assert first != second and not path.exists()
    assert (first.name, second.read_text(encoding="utf-8")) == (
        "state.json.damaged-19700101T000000Z",
        "second",
    )


def test_a_record_too_large_to_be_read_back_is_not_written(tmp_path, monkeypatch):
    path = tmp_path / "kind" / "state.json"
    row = dict.fromkeys(records.FIELDS)
    row.update(id="apr_" + "a" * 32, state="pending", created_at=7, slot="s")
    records.save(path, [row])
    kept = path.read_bytes()
    # The reader's ceiling: a file of exactly that size is still written and read.
    monkeypatch.setattr(records, "_MAX_BYTES", len(kept))
    records.save(path, [row])
    with pytest.raises(ValueError):
        records.save(path, [row, {**row, "id": "apr_" + "b" * 32}])
    assert path.read_bytes() == kept and records.load(path) == [row]


@requires_symlinks
def test_a_dangling_symlink_takes_the_name_of_a_record_set_aside(tmp_path):
    path = tmp_path / "state.json"
    # A name held by a link that points nowhere is taken too.
    taken = path.with_name("state.json.damaged-19700101T000001Z")
    taken.symlink_to(tmp_path / "nowhere")
    path.write_text("third", encoding="utf-8")
    assert records.set_aside(path, 1).name == taken.name + "-2" and taken.is_symlink()


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
        return approvals.LiveApproval(("native", "s", name), name, Mock(), "t", "", "", None)

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
    live = [approvals.LiveApproval(("native", "s", "a"), "rid", Mock(), "t", "", "", None)]
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
    live = [approvals.LiveApproval(("native", "s", "a"), "rid", Mock(), "t", "", "", None)]
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
