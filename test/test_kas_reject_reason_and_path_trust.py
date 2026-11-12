"""A rejection's reason reaches KAS, and a file grant names one tool on one file.

P1-26: the reason a person types with a rejection rides KAS's one-time Deny as
``_meta.kiro.rejectionReason``; no other backend is sent one. P1-25: "trust" on a
file-write built-in can be scoped to that one file, never wider than the path the
card showed, and falls back to asking whenever the path cannot be named exactly.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from acp_runtime_helpers import _make_runtime, _register
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew.acp._dispatch import build_permission_event
from kiro_crew.acp.runtime import AcpSessionHandle
from kiro_crew.acp.types import (
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    AcpEvent,
    JsonRpcMessage,
)
from kiro_crew.dashboard.chat_handlers import api_chat_slot_approve
from kiro_crew.dashboard.chat_turn.deny_chokepoints import _reject_attributed
from kiro_crew.dashboard.state import SlotOrigin
from kiro_crew.rejection_reason import REJECTION_REASON_MAX_CHARS, rejection_reason_text
from kiro_crew.testing.links import make_dir_link
from kiro_crew.testing.wait import finished
from kiro_crew.trust_patterns import (
    approval_command,
    approval_display_command,
    exact_trust_pattern,
    matches_trusted_pattern,
    path_trust_target,
    path_trust_unresolved,
)

# ── P1-26: the reason ──


def test_reason_text_is_cleaned_and_bounded():
    assert rejection_reason_text("  use the other file \x07\n") == "use the other file"
    assert rejection_reason_text("") == ""
    assert (
        rejection_reason_text("x" * REJECTION_REASON_MAX_CHARS) == "x" * REJECTION_REASON_MAX_CHARS
    )
    # Too long is refused, never cut; not text is refused.
    assert rejection_reason_text("x" * (REJECTION_REASON_MAX_CHARS + 1)) is None
    assert rejection_reason_text(["no"]) is None


def _kas_request(options: list[dict]) -> JsonRpcMessage:
    return JsonRpcMessage(
        id=5,
        method="session/request_permission",
        params={"toolCall": {"toolCallId": "t1", "title": "Write"}, "options": options},
    )


_KAS_OPTIONS = [
    {"optionId": "accept", "name": "Allow", "kind": "allow_once"},
    {"optionId": "reject", "name": "Deny", "kind": "reject_once"},
    {"optionId": "always-reject", "name": "Always deny", "kind": "reject_always"},
]


def test_only_a_kas_request_with_a_one_time_deny_takes_a_reason():
    event, _ = build_permission_event(_kas_request(_KAS_OPTIONS), kas_consent_meta=True)
    assert event is not None and event.takes_rejection_reason
    # Another backend: KAS's meta is never sent, so no box is offered.
    event, _ = build_permission_event(_kas_request(_KAS_OPTIONS))
    assert event is not None and not event.takes_rejection_reason
    # Only "Always deny" offered: KAS ignores a reason on it.
    only_always = [_KAS_OPTIONS[0], _KAS_OPTIONS[2]]
    event, _ = build_permission_event(_kas_request(only_always), kas_consent_meta=True)
    assert event is not None and not event.takes_rejection_reason


@pytest.mark.asyncio
@pytest.mark.parametrize("backend,meta", [(ACP_BACKEND_KAS, True), (ACP_BACKEND_KIRO, False)])
async def test_session_handle_sends_reason_on_kas_only(backend, meta):
    rt, _, proc = _make_runtime()
    rt._acp_backend = backend
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    handle._permission_options[7] = {"once": "accept", "reject": "reject"}
    await handle.reject_tool(7, reason="  edit b.py instead ")
    sent = json.loads(proc.stdin.write.call_args.args[0].decode())["result"]
    assert sent["outcome"] == {"outcome": "selected", "optionId": "reject"}
    if meta:
        assert sent["_meta"] == {"kiro": {"rejectionReason": "edit b.py instead"}}
    else:
        assert "_meta" not in sent


@pytest.mark.asyncio
async def test_session_handle_sends_no_reason_without_a_deny_option():
    rt, _, proc = _make_runtime()
    rt._acp_backend = ACP_BACKEND_KAS
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    await handle.reject_tool(8, reason="why")
    sent = json.loads(proc.stdin.write.call_args.args[0].decode())["result"]
    assert sent == {"outcome": {"outcome": "cancelled"}}


@pytest.mark.asyncio
async def test_reject_attributed_passes_a_reason_only_when_given():
    client = MagicMock()
    client.reject_tool = AsyncMock()
    await _reject_attributed(client, 3, attribution="exempt: interactive user denial")
    client.reject_tool.assert_awaited_once_with(3)
    client.reject_tool.reset_mock()
    await _reject_attributed(client, 3, attribution="exempt: x", reason="because")
    client.reject_tool.assert_awaited_once_with(3, reason="because")


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


def _app(state) -> web.Application:
    app = web.Application(middlewares=[_dashboard_user])
    app["state"] = state
    app.router.add_post("/api/chat/slots/{slot}/approve", api_chat_slot_approve)
    return app


def _pending(state, request_id: str):
    slot = state.get_or_create_slot("selected", origin=SlotOrigin.USER)
    fut = asyncio.get_running_loop().create_future()
    row = slot.append("permission", "Tool", json.dumps({"request_id": request_id}))
    slot.register_approval(request_id, fut, row)
    return slot, fut, slot.approval_instance(request_id)


@pytest.mark.asyncio
async def test_slot_approve_keeps_the_reason_for_the_runner(state):
    slot, fut, mid = _pending(state, "r1")
    async with TestClient(TestServer(_app(state))) as client:
        resp = await client.post(
            "/api/chat/slots/selected/approve",
            json={
                "action": "rejected_once",
                "request_id": "r1",
                "origin": "native",
                "request_mid": mid,
                "reason": " wrong file ",
            },
        )
    assert resp.status == 200
    assert fut.result() == "rejected_once"
    # Bound to the future it answered: another future under the id gets nothing.
    assert slot.take_rejection_reason("r1", asyncio.get_running_loop().create_future()) == ""
    assert slot.take_rejection_reason("r1", fut) == "wrong file"
    assert slot.take_rejection_reason("r1", fut) == ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "extra",
    [
        {"action": "approved", "reason": "ok"},
        {"action": "rejected_once", "reason": "x" * (REJECTION_REASON_MAX_CHARS + 1)},
        {"action": "rejected_once", "reason": 5},
    ],
)
async def test_slot_approve_refuses_a_reason_it_cannot_send(state, extra):
    slot, fut, mid = _pending(state, "r2")
    async with TestClient(TestServer(_app(state))) as client:
        resp = await client.post(
            "/api/chat/slots/selected/approve",
            json={"request_id": "r2", "origin": "native", "request_mid": mid, **extra},
        )
        body = await resp.json()
    assert resp.status == 400
    assert body["code"] == "invalid_rejection_reason"
    assert not fut.done()
    assert slot.take_rejection_reason("r2", fut) == ""


# ── P1-25: the file grant ──


def _key(tool: str, params: dict, *, trusted: bool = True, server: str = "") -> str:
    return approval_command(
        json.dumps(params),
        is_shell=False,
        tool_name=tool,
        mcp_server_name=server,
        raw_tool_params=params,
        path_scope_trusted=trusted,
    )


def test_a_file_grant_covers_that_tool_on_that_file_only(tmp_path):
    # Native absolute paths, so the normal-form check passes on every host.
    base = os.path.realpath(tmp_path)
    a, b, upper = (os.path.join(base, name) for name in ("a.txt", "b.txt", "A.txt"))
    key = _key("fs_write", {"path": a, "text": "one"})
    assert key.startswith("path-trust:v1:")
    grants = {exact_trust_pattern(key)}
    # The same file with other content: covered.
    assert matches_trusted_pattern(_key("fs_write", {"path": a, "text": "two"}), grants)
    # Another file, another case, another tool: not covered.
    for other in (
        _key("fs_write", {"path": b, "text": "one"}),
        _key("fs_write", {"path": upper, "text": "one"}),
        _key("delete_file", {"targetFile": a}),
    ):
        assert other and not matches_trusted_pattern(other, grants)
    label = approval_display_command(
        "{}",
        is_shell=False,
        tool_name="fs_write",
        raw_tool_params={"path": a},
        path_scope_trusted=True,
    )
    assert label == f"fs_write {a}"


@pytest.mark.parametrize(
    "tool,params,trusted,server",
    [
        ("fs_write", {"path": "w/a.txt"}, True, ""),  # relative
        ("fs_write", {"path": "/w/../etc/x"}, True, ""),  # not in normal form
        ("fs_write", {"path": "/w//a"}, True, ""),
        ("fs_write", {"path": "/w/a\nb"}, True, ""),  # control character
        ("str_replace", {"path": "/w/a", "filePath": "/w/b"}, True, ""),  # two paths
        ("read_file", {"path": "/w/a"}, True, ""),  # not a write tool
        ("fs_write", {"path": "/w/a"}, False, ""),  # params not from the harness's frame
        ("fs_write", {"path": "/w/a"}, True, "srv"),  # an MCP tool of that name
    ],
)
def test_no_file_grant_when_the_path_cannot_be_named_exactly(tool, params, trusted, server):
    assert _key(tool, params, trusted=trusted, server=server) == ""


def test_a_link_at_or_above_the_path_withholds_the_grant(tmp_path):
    real = os.path.realpath(tmp_path)
    target = os.path.join(real, "f.txt")
    assert path_trust_target("fs_write", "", {"path": target}) == target
    assert not path_trust_unresolved(target)
    link_dir = Path(real) / "link"
    make_dir_link(link_dir, Path(real))
    assert path_trust_unresolved(os.path.join(link_dir, "f.txt"))


def test_path_scope_trust_needs_harness_provenance():
    base = dict(
        kind="permission_request",
        raw_tool_params={"path": "/w/a"},
        raw_params_trusted=True,
        mcp_identity_trusted=True,
        shell_classified=True,
        tool_name="fs_write",
    )
    assert AcpEvent(**base).path_scope_trusted
    for flag in ("raw_params_trusted", "mcp_identity_trusted", "shell_classified"):
        assert not AcpEvent(**{**base, flag: False}).path_scope_trusted
    assert not AcpEvent(**{**base, "is_shell": True}).path_scope_trusted
    assert not AcpEvent(**{**base, "mcp_server_name": "srv"}).path_scope_trusted


# ── Through a dashboard turn ──


def _turn_harness():
    import dashboard_approval_helpers as h

    from kiro_crew.dashboard.chat import _run_chat

    return h, _run_chat


async def _run_answered(h, run_chat, state, slot, answerer) -> None:
    """Run one chat turn under the lost-run bound, always draining *answerer*."""
    try:
        with h._patch_stats():
            await finished(run_chat(state, slot, "hello"), "the chat turn completing")
    finally:
        await h._drain(answerer)


@pytest.mark.asyncio
@pytest.mark.parametrize("takes", [True, False])
async def test_a_turn_sends_the_person_reason_only_where_it_is_taken(tmp_path, takes):
    h, run_chat = _turn_harness()
    state, client = h._make_state(tmp_path, context_builder=h._context_builder())
    slot = h._make_slot()
    event = h._permission_event(title="fs_write")
    event.takes_rejection_reason = takes
    h._set_stream(client, [event, h._complete_event()])

    def keep_reason() -> None:
        slot.set_rejection_reason("req-1", slot._approval_futures["req-1"], "edit b.py instead")

    answerer = asyncio.get_event_loop().create_task(
        h._answer_approval(slot, "req-1", "rejected_once", before_answer=keep_reason)
    )
    await _run_answered(h, run_chat, state, slot, answerer)

    if takes:
        client.reject_tool.assert_any_call("req-1", reason="edit b.py instead")
    else:
        client.reject_tool.assert_any_call("req-1")
    # Taken on the way out, whichever way the reason went.
    assert slot._rejection_reasons == {}
    card = next(m for m in slot.messages if m.get("role") == "permission")
    assert ("rejection_reason_accepted" in card["cls"]) is takes


def _fs_write(h, request_id: str, path: str) -> AcpEvent:
    event = h._permission_event(title="Write a file")
    event.request_id = request_id
    event.tool_call_id = f"tc-{request_id}"
    event.tool_name = "fs_write"
    event.raw_tool_params = {"path": path, "text": "x"}
    event.tool_input = json.dumps(event.raw_tool_params)
    event.raw_params_trusted = event.mcp_identity_trusted = event.shell_classified = True
    return event


@pytest.mark.asyncio
async def test_the_card_offers_a_grant_on_that_file(tmp_path):
    from kiro_crew.dashboard.state import parse_cls_meta

    h, run_chat = _turn_harness()
    target = os.path.join(os.path.realpath(tmp_path), "a.txt")
    state, client = h._make_state(tmp_path, context_builder=h._context_builder())
    slot = h._make_slot()
    h._set_stream(client, [_fs_write(h, "req-1", target), h._complete_event()])
    answerer = asyncio.get_event_loop().create_task(
        h._answer_approval(slot, "req-1", "rejected_once")
    )
    await _run_answered(h, run_chat, state, slot, answerer)
    card = next(m for m in slot.messages if m.get("role") == "permission")
    meta = parse_cls_meta(card["cls"]) or {}
    assert meta.get("trust_command_grantable") == "1"
    assert meta.get("full_command") == f"fs_write {target}"
    assert meta.get("trust_command_key", "").startswith("path-trust:v1:")


@pytest.mark.asyncio
async def test_a_file_grant_approves_that_file_and_asks_for_another(tmp_path):
    h, run_chat = _turn_harness()
    target = os.path.join(os.path.realpath(tmp_path), "a.txt")
    state, client = h._make_state(tmp_path, context_builder=h._context_builder())
    slot = h._make_slot()
    granted = _fs_write(h, "req-2", target)
    key = approval_command(
        granted.tool_input,
        is_shell=False,
        tool_name="fs_write",
        raw_tool_params=granted.raw_tool_params,
        path_scope_trusted=True,
    )
    slot._trusted_patterns.add(exact_trust_pattern(key))
    other = _fs_write(h, "req-3", target + ".bak")
    h._set_stream(client, [granted, other, h._complete_event()])
    answerer = asyncio.get_event_loop().create_task(
        h._answer_approval(slot, "req-3", "rejected_once")
    )
    await _run_answered(h, run_chat, state, slot, answerer)
    client.approve_tool.assert_any_call("req-2")
    client.reject_tool.assert_any_call("req-3")
    asked = [m for m in slot.messages if m.get("role") == "permission"]
    assert len(asked) == 1  # only the other file asked
