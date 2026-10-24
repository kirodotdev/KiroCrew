"""The dashboard half of MCP elicitation: the card's row, the answer endpoint and the
mapping from a resolved future back to the action the driver sends.

``POST /api/chat/slots/{slot}/elicitation`` is driven through the real handler on an
aiohttp ``TestClient``; the auth middleware is stood in by the middlewares below.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew.dashboard.chat_handlers import api_chat_slot_approve
from kiro_crew.dashboard.chat_runner import (
    _redact_fields_for_display,
    mark_elicitation_row_resolved,
)
from kiro_crew.dashboard.chat_turn.elicitation_prompt import read_elicitation_answer
from kiro_crew.dashboard.chat_utils_slots import effective_session_key
from kiro_crew.dashboard.handlers.chat_elicitation import api_chat_slot_elicitation
from kiro_crew.dashboard.state import SlotOrigin, row_mid
from kiro_crew.testing.wait import async_wait_until, await_until

_FIELDS = [
    {"name": "confirm", "kind": "boolean", "required": True, "title": "Confirm"},
    {"name": "count", "kind": "integer", "required": False, "minimum": 1, "maximum": 5},
    {
        "name": "colour",
        "kind": "enum",
        "required": False,
        "options": [{"value": "r", "label": "Red"}],
    },
]


@pytest.fixture(autouse=True)
def _hermetic_home(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    st = _make_state(tmp_path)
    st.broadcast_ws = MagicMock()
    st.push_slots_update = MagicMock()
    st.owner_id = ""
    return st


@web.middleware
async def _dashboard_user(request: web.Request, handler):
    request["app"] = ""
    request["user"] = "local-app"
    return await handler(request)


@web.middleware
async def _internal_caller(request: web.Request, handler):
    request["internal_auth"] = True
    return await handler(request)


@web.middleware
async def _app_caller(request: web.Request, handler):
    request["app"] = "some-app"
    return await handler(request)


def _app(state, middleware) -> web.Application:
    app = web.Application(middlewares=[middleware])
    app["state"] = state
    app.router.add_post("/api/chat/slots/{slot}/elicitation", api_chat_slot_elicitation)
    return app


def _target(row: dict) -> dict:
    """The two keys the card posts to name the ask it drew: the id and the row."""
    return {"request_id": json.loads(row["cls"])["request_id"], "request_mid": row_mid(row)}


def _slot_with_pending(state, request_id: str, *, mode: str = "form"):
    slot = state.get_or_create_slot("selected", origin=SlotOrigin.USER)
    fut = asyncio.get_running_loop().create_future()
    meta = {"request_id": request_id, "mode": mode, "message": "Really?"}
    if mode == "form":
        meta["fields"] = _FIELDS
    else:
        meta["url"] = "https://example.com/go"
    row = slot.append("elicitation", "Really?", json.dumps(meta))
    slot.register_approval(request_id, fut, row)
    return slot, fut, row


@pytest.mark.asyncio
async def test_a_valid_form_answer_is_validated_written_to_the_row_and_resolves(state):
    _slot, fut, row = _slot_with_pending(state, "rid-1")
    async with TestClient(TestServer(_app(state, _dashboard_user))) as client:
        resp = await client.post(
            "/api/chat/slots/selected/elicitation",
            json={**_target(row), "action": "accept", "content": {"confirm": True, "count": 3.0}},
        )
        assert resp.status == 200, await resp.text()
    assert fut.result() == "accept"
    assert json.loads(row["cls"])["answer"] == {"confirm": True, "count": 3}
    assert read_elicitation_answer(fut.result(), json.loads(row["cls"])) == (
        "accept",
        {"confirm": True, "count": 3},
    )


@pytest.mark.asyncio
async def test_content_the_schema_refuses_is_a_400_and_the_ask_stays_open(state):
    _slot, fut, row = _slot_with_pending(state, "rid-2")
    async with TestClient(TestServer(_app(state, _dashboard_user))) as client:
        resp = await client.post(
            "/api/chat/slots/selected/elicitation",
            json={**_target(row), "action": "accept", "content": {"confirm": True, "count": 9}},
        )
        assert resp.status == 400
        body = await resp.json()
        assert body["code"] == "invalid_content" and "count" in body["error"]
        resp = await client.post(
            "/api/chat/slots/selected/elicitation",
            json={**_target(row), "action": "accept", "content": {}},
        )
        assert resp.status == 400
    assert not fut.done()
    assert "answer" not in json.loads(row["cls"])


@pytest.mark.asyncio
async def test_decline_needs_no_content_and_a_second_answer_is_404(state):
    _slot, fut, row = _slot_with_pending(state, "rid-3")
    async with TestClient(TestServer(_app(state, _dashboard_user))) as client:
        resp = await client.post(
            "/api/chat/slots/selected/elicitation",
            json={**_target(row), "action": "decline"},
        )
        assert resp.status == 200
        assert fut.result() == "decline"
        resp = await client.post(
            "/api/chat/slots/selected/elicitation",
            json={**_target(row), "action": "accept"},
        )
        assert resp.status == 404


@pytest.mark.asyncio
async def test_a_url_ask_accepts_without_content(state):
    _slot, fut, row = _slot_with_pending(state, "rid-4", mode="url")
    async with TestClient(TestServer(_app(state, _dashboard_user))) as client:
        resp = await client.post(
            "/api/chat/slots/selected/elicitation",
            json={**_target(row), "action": "accept"},
        )
        assert resp.status == 200
    assert fut.result() == "accept"


@pytest.mark.asyncio
async def test_a_permission_id_cannot_be_answered_through_the_elicitation_route(state):
    """The route answers elicitation rows only: a permission future under the same
    registry is not an ask a form can accept."""
    slot = state.get_or_create_slot("selected", origin=SlotOrigin.USER)
    fut = asyncio.get_running_loop().create_future()
    row = slot.append("permission", "Tool", json.dumps({"request_id": "perm-1"}))
    slot.register_approval("perm-1", fut, row)
    async with TestClient(TestServer(_app(state, _dashboard_user))) as client:
        resp = await client.post(
            "/api/chat/slots/selected/elicitation",
            json={"request_id": "perm-1", "request_mid": row_mid(row), "action": "accept"},
        )
        assert resp.status == 404
    assert not fut.done()


@pytest.mark.asyncio
@pytest.mark.parametrize("middleware", [_internal_caller, _app_caller])
async def test_only_the_dashboard_user_may_answer(state, middleware):
    """The server asked for a PERSON; an agent on the internal secret or an app
    token answering would be the automation the ask exists to exclude."""
    _slot, fut, row = _slot_with_pending(state, "rid-5")
    async with TestClient(TestServer(_app(state, middleware))) as client:
        resp = await client.post(
            "/api/chat/slots/selected/elicitation",
            json={**_target(row), "action": "accept", "content": {"confirm": True}},
        )
        assert resp.status == 403
    assert not fut.done()


@web.middleware
async def _allowlisted_non_owner(request: web.Request, handler):
    # A Telegram/Teams/Webex allowlist link: a dashboard session with the same
    # empty app claim as the owner's, for another subject.
    request["app"] = ""
    request["user"] = "telegram:friend"
    return await handler(request)


@pytest.mark.asyncio
async def test_a_non_owner_dashboard_session_cannot_answer_the_owners_ask(state):
    """An answer is the owner's consent to the server's operation, so the route
    checks the caller IS the owner, not only that it carries an empty app claim."""
    state.owner_id = "owner-1"
    _slot, fut, row = _slot_with_pending(state, "rid-own")
    async with TestClient(TestServer(_app(state, _allowlisted_non_owner))) as client:
        resp = await client.post(
            "/api/chat/slots/selected/elicitation",
            json={**_target(row), "action": "accept", "content": {"confirm": True}},
        )
        assert resp.status == 403
    assert not fut.done()


@pytest.mark.asyncio
async def test_bad_bodies_are_400(state):
    _slot, fut, row = _slot_with_pending(state, "rid-6")
    async with TestClient(TestServer(_app(state, _dashboard_user))) as client:
        resp = await client.post(
            "/api/chat/slots/selected/elicitation", json={**_target(row), "action": "maybe"}
        )
        assert resp.status == 400
        resp = await client.post("/api/chat/slots/selected/elicitation", json={"action": "accept"})
        assert resp.status == 400
        # The row the card drew is part of the target: an answer that names the
        # id alone cannot be bound to an instance, so it is refused outright.
        resp = await client.post(
            "/api/chat/slots/selected/elicitation",
            json={"request_id": "rid-6", "action": "decline"},
        )
        assert resp.status == 400
        assert (await resp.json())["code"] == "invalid_request_mid"
    assert not fut.done()


@pytest.mark.asyncio
async def test_a_stale_card_cannot_answer_a_later_ask_that_reused_its_id(state):
    """ACP request ids are connection-scoped and a backend may reuse one once the
    earlier ask expired. A delayed POST from the OLD card names the old row, so
    it must not accept the question the person never saw; only the card drawn
    from the live row resolves it."""
    slot, old_fut, old_row = _slot_with_pending(state, "rid-8", mode="url")
    old_fut.set_result("cancel")
    mark_elicitation_row_resolved(old_row, "cancel", None)
    new_fut = asyncio.get_running_loop().create_future()
    new_row = slot.append(
        "elicitation",
        "Again?",
        json.dumps({"request_id": "rid-8", "mode": "url", "url": "https://example.com/2"}),
    )
    slot.register_approval("rid-8", new_fut, new_row)
    assert row_mid(old_row) != row_mid(new_row)
    async with TestClient(TestServer(_app(state, _dashboard_user))) as client:
        resp = await client.post(
            "/api/chat/slots/selected/elicitation",
            json={**_target(old_row), "action": "accept"},
        )
        assert resp.status == 404
        assert not new_fut.done()
        resp = await client.post(
            "/api/chat/slots/selected/elicitation",
            json={**_target(new_row), "action": "accept"},
        )
        assert resp.status == 200
    assert new_fut.result() == "accept"


def test_a_resolved_future_maps_to_the_action_the_driver_sends() -> None:
    """The permission registry's own outcomes (a Stop, the generic approve, the
    trust sweep) are read as elicitation actions: a rejection declines, and an
    approval CANCELS -- URL ask or form -- because no permission-vocabulary word
    is a person answering the server's question. Only the elicitation route's own
    ``accept`` accepts."""
    form = {"mode": "form"}
    url = {"mode": "url"}
    assert read_elicitation_answer("decline", form) == ("decline", None)
    assert read_elicitation_answer("rejected", form) == ("decline", None)
    assert read_elicitation_answer("rejected_once", url) == ("decline", None)
    assert read_elicitation_answer("approved", url) == ("cancel", None)
    assert read_elicitation_answer("approved_trust_reads", url) == ("cancel", None)
    assert read_elicitation_answer("approved", form) == ("cancel", None)
    assert read_elicitation_answer("accept", {"mode": "form", "answer": {"a": 1}}) == (
        "accept",
        {"a": 1},
    )
    assert read_elicitation_answer("accept", form) == ("accept", None)
    assert read_elicitation_answer("whatever", form) == ("cancel", None)


@pytest.mark.asyncio
async def test_the_generic_approve_route_refuses_an_elicitation_id(state):
    """An app with the session-approval grant (or the owner's own Allow click)
    cannot turn a pending elicitation into an MCP ``accept`` through the tool
    approve route: the id is refused there, and the future stays pending for the
    elicitation route."""
    _slot, fut, _row = _slot_with_pending(state, "rid-7", mode="url")
    app = web.Application(middlewares=[_dashboard_user])
    app["state"] = state
    app.router.add_post("/api/chat/slots/{slot}/approve", api_chat_slot_approve)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/api/chat/slots/selected/approve", json={"action": "approved", "request_id": "rid-7"}
        )
        assert resp.status == 409
        assert (await resp.json())["code"] == "elicitation_not_an_approval"
    assert not fut.done()


def _approve_app(state) -> web.Application:
    app = web.Application(middlewares=[_dashboard_user])
    app["state"] = state
    app.router.add_post("/api/chat/slots/{slot}/approve", api_chat_slot_approve)
    return app


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["approved", "rejected"])
async def test_a_bare_approve_never_picks_the_sole_pending_elicitation(state, action):
    """An approve with NO request id falls back to the slot's one pending future.
    When that future is an elicitation it is not a tool approval the caller may
    be deciding, so the implicit pick leaves it out: the request finds nothing
    to resolve and the ask stays open for the elicitation route."""
    _slot, fut, _row = _slot_with_pending(state, "rid-9", mode="url")
    async with TestClient(TestServer(_approve_app(state))) as client:
        resp = await client.post("/api/chat/slots/selected/approve", json={"action": action})
        assert resp.status == 404
    assert not fut.done()


@pytest.mark.asyncio
async def test_the_owner_scan_refuses_an_elicitation_held_by_a_shared_slot(state):
    """Under session-sharing the future an id names can live on ANOTHER slot
    object with the same effective session. The elicitation check runs on the
    slot the scan resolved, not only on the addressed one, so the shared slot's
    open question is refused the same way."""
    addressed = state.get_or_create_slot("selected", origin=SlotOrigin.USER)
    holder = state.get_or_create_slot("other", origin=SlotOrigin.USER)
    holder.linked_session_key = effective_session_key(addressed)
    fut = asyncio.get_running_loop().create_future()
    row = holder.append(
        "elicitation",
        "Go?",
        json.dumps({"request_id": "rid-10", "mode": "url", "url": "https://example.com"}),
    )
    holder.register_approval("rid-10", fut, row)
    assert "rid-10" not in addressed._approval_futures
    async with TestClient(TestServer(_approve_app(state))) as client:
        resp = await client.post(
            "/api/chat/slots/selected/approve", json={"action": "approved", "request_id": "rid-10"}
        )
        assert resp.status == 409
        assert (await resp.json())["code"] == "elicitation_not_an_approval"
    assert not fut.done()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["approve", "reject", "reject_once"])
async def test_the_app_approvals_route_cannot_settle_an_elicitation(state, action):
    """``/api/approvals/{id}/{action}`` is the app token's route: an app holding
    the session-approval grant resolves a tool approval it may control. An
    elicitation shares the slot registry but is the server's question to a
    PERSON, so the id reads as not found and the future stays pending."""
    from unittest.mock import AsyncMock, patch

    from kiro_crew.dashboard.handlers.sessions import api_approval_resolve

    _slot, fut, _row = _slot_with_pending(state, "rid-30", mode="url")
    app = web.Application(middlewares=[_app_caller])
    app["state"] = state
    app.router.add_post("/api/approvals/{id}/{action}", api_approval_resolve)
    with (
        patch("kiro_crew.apps.permissions.app_can_manage_session_approvals", return_value=True),
        patch(
            "kiro_crew.dashboard.chat_handlers._app_may_send_to_slot",
            new=AsyncMock(return_value=True),
        ),
    ):
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(f"/api/approvals/rid-30/{action}")
            assert resp.status == 404, await resp.text()
            # Control: the same grant DOES settle a tool approval on the slot, so
            # the 404 above is the elicitation refusal, not a harness that grants
            # nothing.
            perm_fut = asyncio.get_running_loop().create_future()
            perm_row = _slot.append("permission", "Tool", json.dumps({"request_id": "perm-30"}))
            _slot.register_approval("perm-30", perm_fut, perm_row)
            resp = await client.post(f"/api/approvals/perm-30/{action}")
            assert resp.status == 200, await resp.text()
            assert perm_fut.done()
    assert not fut.done()


@pytest.mark.asyncio
async def test_the_servers_opaque_url_token_never_reaches_the_dashboard_row(state, audited):
    """``elicitation_id`` is wire detail the card never reads and the route never
    sends back; it is also the one server-authored string no display redaction
    touches, so it is dropped before the row is appended."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from kiro_crew.dashboard.chat_turn.elicitation_prompt import _on_elicitation_request

    state.approval_timeout_for = lambda slot: 0.0
    slot = state.get_or_create_slot("selected", origin=SlotOrigin.USER)
    event = SimpleNamespace(
        request_id="rid-40",
        tool_call_id="tool-1",
        title="",
        elicitation={
            "mode": "url",
            "message": "Sign in",
            "url": "https://example.com/go",
            "elicitation_id": "AKIAIOSFODNN7EXAMPLE",
        },
    )
    await await_until(
        asyncio.ensure_future(
            _on_elicitation_request(_runner_turn(state, slot, AsyncMock()), event)
        )
    )
    row = next(m for m in slot.messages if m.get("role") == "elicitation")
    meta = json.loads(row["cls"])
    assert "elicitation_id" not in meta
    assert meta["url"] == "https://example.com/go" and meta["mode"] == "url"


def test_display_redaction_touches_labels_and_taints_values() -> None:
    """Titles, descriptions and option labels are redacted for display; a
    credential in an option value or a default cannot be rewritten (the answer
    would then fail the schema), so the request is reported tainted."""
    secret = "AKIAIOSFODNN7EXAMPLE1234"
    fields = [
        {
            "name": "a",
            "kind": "string",
            "required": False,
            "title": f"key {secret}",
            "description": "d",
        },
        {
            "name": "b",
            "kind": "enum",
            "required": False,
            "options": [{"value": "x", "label": f"lbl {secret}"}],
        },
    ]
    safe, tainted = _redact_fields_for_display(fields)
    assert not tainted
    assert secret not in safe[0]["title"]
    assert secret not in safe[1]["options"][0]["label"]
    assert safe[1]["options"][0]["value"] == "x"
    _safe, tainted = _redact_fields_for_display(
        [
            {
                "name": "c",
                "kind": "enum",
                "required": False,
                "options": [{"value": secret, "label": "k"}],
            }
        ]
    )
    assert tainted
    _safe, tainted = _redact_fields_for_display(
        [{"name": "d", "kind": "string", "required": False, "default": secret}]
    )
    assert tainted
    # The property NAME is the key the answer is sent under and the label the
    # card falls back to without a title: a credential there taints too.
    _safe, tainted = _redact_fields_for_display(
        [{"name": secret, "kind": "string", "required": False}]
    )
    assert tainted


def test_pending_elicitation_rows_survive_a_slot_detail_read() -> None:
    """A pending ask is actionable and the client reads it out of the transcript,
    so the bounded reader must hand it back like a pending permission; an
    answered one is history and drops out of the owed tail the same way."""
    from kiro_crew.dashboard.chat_api.slot_detail import _is_answered_permission
    from kiro_crew.dashboard.chat_handlers import _UNOWED_WINDOW_ROLES

    assert "elicitation" not in _UNOWED_WINDOW_ROLES
    assert "permission" not in _UNOWED_WINDOW_ROLES
    pending = {"role": "elicitation", "cls": json.dumps({"request_id": "r", "mode": "url"})}
    answered = {"role": "elicitation", "cls": json.dumps({"request_id": "r", "resolved": "accept"})}
    assert not _is_answered_permission(pending)
    assert _is_answered_permission(answered)


def test_an_elicitation_row_keeps_its_mid_over_http() -> None:
    """The resolution frame names the row's ``mid``; a reload that dropped it
    would leave the client matching by a reusable request id again."""
    from kiro_crew.dashboard.chat_utils_display import _prepare_messages

    rows = [
        {
            "role": "elicitation",
            "content": "Really?",
            "cls": json.dumps({"request_id": "9002", "mode": "url"}),
            "ts": "t1",
            "meta": {"mid": "m-elicit-1"},
        }
    ]
    out = _prepare_messages(rows, running=True, live_child="")
    assert out[0]["meta"]["mid"] == "m-elicit-1"
    assert out[0]["meta"]["approval_id"] == "9002"


def test_the_row_in_hand_is_stamped_not_the_oldest_with_the_id() -> None:
    older = {"role": "elicitation", "cls": json.dumps({"request_id": "9002", "mode": "url"})}
    newer = {"role": "elicitation", "cls": json.dumps({"request_id": "9002", "mode": "url"})}
    mark_elicitation_row_resolved(newer, "accept", None)
    assert "resolved" not in json.loads(older["cls"])
    assert json.loads(newer["cls"])["resolved"] == "accept"


# ── the trust tiers: a sweep of pending approvals never answers an ask ───────


@pytest.fixture
def _isolate_safety_override():
    from kiro_crew.safety_override import reset_singleton

    reset_singleton()
    yield
    reset_singleton()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["trust", "yolo"])
async def test_switching_to_a_trust_tier_leaves_a_pending_ask_open(
    state, _isolate_safety_override, mode
):
    """``trust`` and ``yolo`` sweep every pending tool approval to ``approved``. An
    elicitation in the same registry is a server's own question to the person, so
    the sweep steps over it: the sibling permission resolves, the ask stays open
    for the elicitation route, and its row is not marked resolved."""
    from kiro_crew.dashboard.chat_api.slot_approval import apply_approval_mode

    slot, ask, row = _slot_with_pending(state, "rid-ask")
    perm = asyncio.get_running_loop().create_future()
    slot.append("permission", "Running: ls", json.dumps({"request_id": "perm-1"}))
    slot._approval_futures["perm-1"] = perm

    resp = await apply_approval_mode(
        state,
        mode=mode,
        slot=slot,
        slot_key=slot.key,
        request_app="",
        cron_creator="",
        audit_caller=lambda tag: tag,
    )
    assert resp.status == 200, resp.text
    assert perm.done() and perm.result() == "approved"
    assert not ask.done()
    assert "resolved" not in json.loads(row["cls"])
    assert slot._approval_futures["rid-ask"] is ask
    resolved = [
        c.args[1]["id"]
        for c in state.broadcast_ws.call_args_list
        if c.args[0] == "approval_resolved"
    ]
    assert resolved == ["perm-1"]


# ── the runner: what the audit says and what the resolution frame carries ─────


def _runner_turn(state, slot, answer_elicitation):
    from types import SimpleNamespace

    client = SimpleNamespace(answer_elicitation=answer_elicitation)
    return SimpleNamespace(
        client=client,
        session_key="dashboard:selected",
        slot=slot,
        state=state,
        _seen_tool_identity={"tool-1": ("probe", "delete_file")},
    )


def _form_event(request_id: str):
    from types import SimpleNamespace

    return SimpleNamespace(
        request_id=request_id,
        tool_call_id="tool-1",
        title="",
        elicitation={"mode": "form", "message": "Really?", "fields": _FIELDS},
    )


@pytest.fixture
def audited(monkeypatch):
    """The SEL records the runner writes, through the facade name every owner reads."""
    records: list[dict] = []
    fake = MagicMock()
    fake.log_tool_invocation.side_effect = lambda **kw: records.append(kw)
    monkeypatch.setattr("kiro_crew.dashboard.chat_runner.sel", lambda: fake)
    return records


@pytest.mark.asyncio
async def test_an_accepted_answer_rides_the_resolution_frame_and_audits_as_resolved(state, audited):
    """The live card fills its summary from the frame, not from a transcript reload,
    and the SEL record says a person decided."""
    from unittest.mock import AsyncMock

    from kiro_crew.dashboard.chat_turn.elicitation_prompt import _on_elicitation_request

    slot = state.get_or_create_slot("selected", origin=SlotOrigin.USER)
    sent = AsyncMock()
    task = asyncio.ensure_future(
        _on_elicitation_request(_runner_turn(state, slot, sent), _form_event("rid-20"))
    )
    await async_wait_until(lambda: "rid-20" in slot._approval_futures)
    fut = slot._approval_futures["rid-20"]
    row = next(m for m in slot.messages if m.get("role") == "elicitation")
    meta = json.loads(row["cls"])
    # Who is asking rides the row, from the turn's trusted identity for the call.
    assert (meta["mcp_server"], meta["tool_name"]) == ("probe", "delete_file")
    meta["answer"] = {"confirm": True, "count": 2}
    row["cls"] = json.dumps(meta)
    fut.set_result("accept")
    await await_until(task)
    sent.assert_awaited_once_with("rid-20", "accept", {"confirm": True, "count": 2})
    frames = [
        c.args for c in state.broadcast_ws.call_args_list if c.args[0] == "elicitation_resolved"
    ]
    assert len(frames) == 1
    frame = frames[0][1]
    assert frame["action"] == "accept" and frame["answer"] == {"confirm": True, "count": 2}
    assert frame["mid"] == row_mid(row)
    assert [r["metadata"]["reason"] for r in audited] == ["resolved"]
    assert audited[0]["outcome"] == "accept"


@pytest.mark.asyncio
async def test_an_expired_window_audits_as_window_expired_not_resolved(state, audited):
    """``wait_for`` cancels the future on timeout, so ``fut.done()`` is true on every
    exit; the reason is tracked where the exit happens. The window is the slot's
    approval window, set here to a millisecond nobody answers in."""
    from unittest.mock import AsyncMock

    from kiro_crew.dashboard.chat_turn.elicitation_prompt import _on_elicitation_request

    state.approval_timeout_for = lambda slot: 0.001
    slot = state.get_or_create_slot("selected", origin=SlotOrigin.USER)
    sent = AsyncMock()
    await await_until(
        asyncio.ensure_future(
            _on_elicitation_request(_runner_turn(state, slot, sent), _form_event("rid-21"))
        )
    )
    sent.assert_awaited_once_with("rid-21", "cancel", None)
    assert [r["metadata"]["reason"] for r in audited] == ["window_expired"]
    frames = [
        c.args[1] for c in state.broadcast_ws.call_args_list if c.args[0] == "elicitation_resolved"
    ]
    assert frames and frames[0]["action"] == "cancel" and "answer" not in frames[0]


@pytest.mark.asyncio
async def test_a_stop_rejection_clears_the_stopped_mark_and_audits_as_stopped(state, audited):
    """A Stop rejects the future and records the id in ``_approval_stopped`` for the
    permission path to read. The elicitation path must consume its own entry too:
    ACP request ids are reused, and a stale mark would later brand a person's
    permission answer as the host's."""
    from unittest.mock import AsyncMock

    from kiro_crew.dashboard.chat_turn.elicitation_prompt import _on_elicitation_request

    slot = state.get_or_create_slot("selected", origin=SlotOrigin.USER)
    sent = AsyncMock()
    task = asyncio.ensure_future(
        _on_elicitation_request(_runner_turn(state, slot, sent), _form_event("rid-22"))
    )
    await async_wait_until(lambda: "rid-22" in slot._approval_futures)
    # What turn_stop's reject sweep does to every pending future on the slot.
    slot._approval_stopped.add("rid-22")
    slot._approval_futures["rid-22"].set_result("rejected")
    await await_until(task)
    # A Stop is nobody refusing the server: MCP's ``cancel``, never ``decline``.
    sent.assert_awaited_once_with("rid-22", "cancel", None)
    assert "rid-22" not in slot._approval_stopped
    assert [r["metadata"]["reason"] for r in audited] == ["stopped"]
    assert [r["outcome"] for r in audited] == ["cancel"]


@pytest.mark.asyncio
async def test_a_failed_audit_or_fallback_write_still_settles_the_card(state, monkeypatch):
    """The audit and the decline fallback can each raise. Neither may skip the row
    stamp and the resolution frame: an ask left pending on the row would keep
    its buttons and hold its request id against a later permission."""
    from unittest.mock import AsyncMock

    from kiro_crew.acp.elicitation import ElicitationContentError
    from kiro_crew.dashboard.chat_turn.elicitation_prompt import _on_elicitation_request

    broken = MagicMock()
    broken.log_tool_invocation.side_effect = RuntimeError("sel down")
    monkeypatch.setattr("kiro_crew.dashboard.chat_runner.sel", lambda: broken)
    slot = state.get_or_create_slot("selected", origin=SlotOrigin.USER)
    # The answer does not fit the form, and the decline that replaces it fails too.
    sent = AsyncMock(side_effect=[ElicitationContentError("bad"), RuntimeError("pipe gone")])
    task = asyncio.ensure_future(
        _on_elicitation_request(_runner_turn(state, slot, sent), _form_event("rid-23"))
    )
    await async_wait_until(lambda: "rid-23" in slot._approval_futures)
    slot._approval_futures["rid-23"].set_result("accept")
    await await_until(task)
    row = next(m for m in slot.messages if m.get("role") == "elicitation")
    assert json.loads(row["cls"]).get("resolved") == "decline"
    frames = [
        c.args[1] for c in state.broadcast_ws.call_args_list if c.args[0] == "elicitation_resolved"
    ]
    assert [f["action"] for f in frames] == ["decline"]
