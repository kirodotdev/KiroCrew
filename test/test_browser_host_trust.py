"""Host trust: the ``browser`` tool trusted on one site, for reading or for acting.

The key a call gets, the pattern a grant stores, the card a chat's own browser
call shows, the trusted-pattern rung that approves the next call on that site
and asks for another, and the op route's check that the tab is still there.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from kiro_crew import browser_host_trust as bht
from kiro_crew.agent_sdk.backends import ACP_BACKEND_CLAUDE, ACP_BACKEND_KAS
from kiro_crew.agent_sdk.capabilities import capabilities_for
from kiro_crew.browser_harness.op_classes import host_trust_class, reads_only
from kiro_crew.testing.wait import finished
from kiro_crew.trust_patterns import (
    approval_command,
    approval_display_command,
    matches_trusted_pattern,
)


def _call(op: str, **args: object) -> dict:
    return {"op": op, "args": args}


# ── The classifier ──


@pytest.mark.parametrize(
    ("op", "args", "cls"),
    [
        ("snapshot", {}, "read"),
        ("read", {}, "read"),
        ("screenshot", {}, "read"),
        ("console", {}, "read"),
        ("wait_for", {}, "read"),
        ("navigate", {"url": "https://a.example/"}, "read"),
        ("scroll", {}, "read"),
        ("tabs", {}, ""),
        ("tabs", {"action": "new", "url": "https://a.example/"}, "read"),
        ("tabs", {"action": "switch"}, "act"),
        ("click", {}, "act"),
        ("type", {}, "act"),
        ("paste", {}, "act"),
        ("press_key", {}, "act"),
        ("select_option", {}, "act"),
        ("dialog", {}, "act"),
        ("back", {}, "act"),
        ("ask", {}, ""),
        ("reflex", {}, ""),
        ("upload", {}, ""),
        ("some_future_op", {}, ""),
    ],
)
def test_each_op_has_one_class(op, args, cls):
    assert host_trust_class(op, args) == cls


def test_reads_only_is_the_handback_rule():
    assert reads_only("read", {}) and reads_only("tabs", {"action": "list"})
    assert not reads_only("navigate", {"url": "https://a.example/"})
    assert not reads_only("tabs", {"action": "new"})


# ── The host and the key ──


@pytest.mark.parametrize(
    ("url", "host"),
    [
        ("https://WWW.Example.com./path?q=1", "www.example.com"),
        ("http://localhost:5173/", "localhost:5173"),
        ("https://[::1]:8443/", "[::1]:8443"),
        ("https://user@a.example/", ""),
        ("about:blank", ""),
        ("data:text/html,hi", ""),
        ("file:///etc/passwd", ""),
        ("https://a.example:bad/", ""),
        # A Unicode host is named as Chromium reports it, so a look-alike shows as such.
        ("https://ex\u00e4mple.com/", "xn--exmple-cua.com"),
        ("https://\u0430pple.com/", "xn--pple-43d.com"),
        ("https://" + "\u00fc" * 70 + ".com/", ""),
        ("https://" + "a" * 250 + ".com/", ""),
        ("", ""),
        (None, ""),
    ],
)
def test_page_host(url, host):
    assert bht.page_host(url) == host


def test_a_page_opening_op_is_keyed_on_the_page_it_opens():
    assert bht.call_scope(_call("navigate", url="https://b.example/x"), "https://a.example/") == (
        "b.example",
        "read",
    )
    assert bht.call_scope(_call("tabs", action="new", url="about:blank"), "https://a.example/") == (
        "",
        "",
    )


def test_any_other_op_is_keyed_on_the_current_page():
    assert bht.call_scope(_call("click", ref="e1"), "https://a.example/p") == ("a.example", "act")
    assert bht.call_scope(_call("click", ref="e1"), "") == ("", "")
    assert bht.call_scope(_call("ask", kind="confirm"), "https://a.example/") == ("", "")
    assert bht.call_scope({"op": 3}, "https://a.example/") == ("", "")


def test_approval_command_names_the_host_only_for_a_proven_browser_call():
    params = _call("click", ref="e1")
    key = approval_command(
        json.dumps(params),
        is_shell=False,
        tool_name="browser",
        mcp_server_name="kirocrew-core",
        raw_tool_params=params,
        browser_page_url="https://a.example/",
    )
    assert key == bht.trust_key("a.example", "act")
    shown = approval_display_command(
        json.dumps(params),
        is_shell=False,
        tool_name="browser",
        mcp_server_name="kirocrew-core",
        raw_tool_params=params,
        browser_page_url="https://a.example/",
    )
    assert shown == "a.example"
    # Without the proof the call is what it was on main: no command grant.
    assert (
        approval_command(
            json.dumps(params),
            is_shell=False,
            tool_name="browser",
            mcp_server_name="kirocrew-core",
            raw_tool_params=params,
        )
        == ""
    )


def test_a_command_spelled_as_a_host_key_gets_no_key():
    spelled = bht.trust_key("a.example", "act")
    for command in (spelled, spelled.upper(), f"ls; {spelled}"):
        assert approval_command(json.dumps({"command": command}), is_shell=True) == ""
    assert approval_command(json.dumps({"command": "ls"}), is_shell=True) == "ls"
    # Naming the prefix in an argument is a search, not a host key: it keeps its key.
    search = "rg -n 'browser-trust:v1:' src"
    assert approval_command(json.dumps({"command": search}), is_shell=True) == search


def test_host_keys_and_other_keys_never_match_each_others_patterns():
    key = bht.trust_key("a.example", "act")
    site = bht.grant_pattern(key)
    # ``*`` (an app worker's trust-everything) is not a site grant.
    assert matches_trusted_pattern(key, {"*"}) is None
    assert matches_trusted_pattern(key, {bht.KEY_PREFIX + "*"}) == bht.KEY_PREFIX + "*"
    # A site grant covers no command.
    assert matches_trusted_pattern("ls", {site}) is None
    assert matches_trusted_pattern("ls", {"*", site}) == "*"


def test_an_act_grant_covers_reading_and_acting_on_its_host_only():
    act = {bht.grant_pattern(bht.trust_key("a.example", "act"))}
    assert matches_trusted_pattern(bht.trust_key("a.example", "read"), act)
    assert matches_trusted_pattern(bht.trust_key("a.example", "act"), act)
    assert not matches_trusted_pattern(bht.trust_key("b.example", "read"), act)
    # A host whose hex starts with the granted one is another host.
    assert not matches_trusted_pattern(bht.trust_key("a.example.evil", "read"), act)


def test_a_read_grant_covers_reading_only():
    read = {bht.grant_pattern(bht.trust_key("a.example", "read"))}
    assert matches_trusted_pattern(bht.trust_key("a.example", "read"), read)
    assert not matches_trusted_pattern(bht.trust_key("a.example", "act"), read)


def test_any_other_key_is_stored_exactly():
    assert bht.grant_pattern("rm *.tmp") == "rm [*].tmp"


# ── The op route's re-check ──


def _tab(url: object):
    async def helper(name: str) -> object:
        assert name == "current_tab"
        if isinstance(url, Exception):
            raise url
        return {"url": url, "targetId": "t1"}

    return helper


def _note(host: str, cls: str, op: str, session: str = "dashboard:chat-1", **args: object):
    bht.note_host_approval(session, bht.trust_key(host, cls), _call(op, **args))


async def _recheck(op: str, args: dict, helper) -> dict | None:
    """What the op route does: take the call's own note, then check it."""
    return await bht.recheck(bht.take_host_approval("chat-1", op, args), op, args, helper)


@pytest.mark.asyncio
async def test_no_host_approval_runs_unchecked():
    assert await _recheck("click", {}, _tab(RuntimeError("unused"))) is None


@pytest.mark.asyncio
async def test_a_host_approval_runs_on_its_host_once():
    _note("a.example", "act", "click")
    assert await _recheck("click", {}, _tab("https://a.example/x")) is None
    assert bht.take_host_approval("chat-1", "click", {}) is None


@pytest.mark.asyncio
async def test_a_host_approval_is_refused_once_the_tab_moved_off_its_host():
    _note("a.example", "act", "click")
    refused = await _recheck("click", {}, _tab("https://b.example/"))
    assert refused is not None and refused["code"] == bht.SCOPE_CHANGED_CODE
    assert "a.example" in refused["error"]


@pytest.mark.asyncio
async def test_an_unreadable_tab_refuses():
    _note("a.example", "read", "snapshot")
    refused = await _recheck("snapshot", {}, _tab(RuntimeError("gone")))
    assert refused is not None and refused["code"] == bht.SCOPE_CHANGED_CODE


@pytest.mark.asyncio
async def test_a_navigate_is_checked_against_the_page_it_opens():
    _note("a.example", "read", "navigate", url="https://a.example/y")
    assert (
        await _recheck("navigate", {"url": "https://a.example/y"}, _tab(RuntimeError("unused")))
        is None
    )


def test_a_key_that_is_not_a_host_key_notes_nothing():
    bht.note_host_approval("dashboard:chat-1", "path-trust:v1:aa:bb", _call("snapshot"))
    assert bht.take_host_approval("chat-1", "snapshot", {}) is None


@pytest.mark.asyncio
async def test_a_note_checks_only_the_call_it_approved():
    # A host-approved click whose op never reached the route (held, refused busy).
    _note("a.example", "act", "click", ref="e3")
    # The next op, approved on its own card, runs on another host unchecked.
    assert await _recheck("snapshot", {}, _tab("https://b.example/")) is None
    assert await _recheck("click", {"ref": "e9"}, _tab("https://b.example/")) is None


@pytest.mark.asyncio
async def test_two_pending_approvals_are_each_checked_against_their_own_host():
    _note("a.example", "act", "click", ref="e1")
    _note("b.example", "read", "navigate", url="https://b.example/")
    # The navigate reaches the route first; it takes its own note, not the click's.
    assert (
        await _recheck("navigate", {"url": "https://b.example/"}, _tab(RuntimeError("unused")))
        is None
    )
    refused = await _recheck("click", {"ref": "e1"}, _tab("https://b.example/"))
    assert refused is not None and "a.example" in refused["error"]


def test_a_note_keeps_a_digest_of_the_call_not_its_arguments():
    _note("a.example", "act", "type", ref="e1", text="x" * 100_000)
    ((digest, host),) = bht._pending["chat-1"]
    assert len(digest) == 32 and host == "a.example"
    assert bht.take_host_approval("chat-1", "type", {"ref": "e1", "text": "x" * 100_000})


def test_a_tool_search_envelope_is_read_by_its_inner_call():
    inner = _call("click", ref="e1")
    for tool_id in ("kirocrew-core::browser", "tool::kirocrew-core::browser"):
        envelope = {"tool_id": tool_id, "arguments": inner}
        assert bht.call_scope(envelope, "https://a.example/") == ("a.example", "act")
        bht.note_host_approval("dashboard:chat-1", bht.trust_key("a.example", "act"), envelope)
        assert bht.take_host_approval("chat-1", "click", {"ref": "e1"}) == "a.example"
    # An envelope for another tool is not this call.
    other = {"tool_id": "kirocrew-core::send_message", "arguments": inner}
    assert bht.call_scope(other, "https://a.example/") == ("", "")


# ── Through a dashboard turn ──


def _turn_harness():
    import dashboard_approval_helpers as h

    from kiro_crew.dashboard.chat import _run_chat

    return h, _run_chat


def _context_builder(h):
    cb = h._context_builder()
    # The gate's confirm list: empty, read as such (a MagicMock would read as unknown).
    cb.hooks._config = SimpleNamespace(confirm_tools=[], confirm_tools_unknown=False)
    return cb


def _browser(
    h, request_id: str, params: dict, *, sub_session: str = "", native_child: bool = False
):
    event = h._permission_event(title="Drive browser")
    event.request_id = request_id
    event.tool_call_id = f"tc-{request_id}"
    event.tool_name = "browser"
    event.mcp_server_name = "kirocrew-core"
    event.raw_tool_params = params
    event.tool_input = json.dumps(params)
    event.raw_params_trusted = event.mcp_identity_trusted = event.shell_classified = True
    event.sub_session_id = sub_session
    event.native_child_call = native_child
    return event


@pytest.fixture
def page(monkeypatch):
    """The live hub's view of the chat's page; records the keys it was asked for."""
    seen: list[str] = []
    current = {"url": "https://a.example/start"}

    class _Hub:
        def get(self, key: str):
            seen.append(key)
            return SimpleNamespace(url=current["url"])

    monkeypatch.setattr("kiro_crew.browser_harness.live_hub.get_live_hub", lambda: _Hub())
    return SimpleNamespace(seen=seen, current=current)


async def _run_answered(h, run_chat, state, slot, answerer) -> None:
    try:
        with h._patch_stats():
            await finished(run_chat(state, slot, "hello"), "the chat turn completing")
    finally:
        await h._drain(answerer)


def _card_meta(slot) -> dict:
    from kiro_crew.dashboard.state import parse_cls_meta

    card = next(m for m in slot.messages if m.get("role") == "permission")
    return parse_cls_meta(card["cls"]) or {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("params", "cls"),
    [(_call("snapshot"), "read"), (_call("click", ref="e3"), "act")],
)
async def test_the_card_offers_the_site_for_the_calls_class(tmp_path, page, params, cls):
    h, run_chat = _turn_harness()
    state, client = h._make_state(tmp_path, context_builder=_context_builder(h))
    client.capabilities = capabilities_for(ACP_BACKEND_KAS)
    slot = h._make_slot()
    h._set_stream(client, [_browser(h, "req-1", params), h._complete_event()])
    answerer = asyncio.get_event_loop().create_task(
        h._answer_approval(slot, "req-1", "rejected_once")
    )
    await _run_answered(h, run_chat, state, slot, answerer)
    meta = _card_meta(slot)
    assert meta.get("trust_command_grantable") == "1"
    assert meta.get("full_command") == "a.example"
    assert meta.get("trust_command_key") == bht.trust_key("a.example", cls)
    assert meta.get("browser_trust_class") == cls
    # "Trust all tools" stays on the card.
    assert meta.get("trust_grantable") == "1"
    assert page.seen and all(k == slot.key for k in page.seen)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("backend", "sub_session", "native_child"),
    [
        (ACP_BACKEND_CLAUDE, "", False),
        (ACP_BACKEND_KAS, "child-1", False),
        (ACP_BACKEND_KAS, "", True),
    ],
)
async def test_no_site_grant_where_the_page_cannot_be_known(
    tmp_path, page, backend, sub_session, native_child
):
    h, run_chat = _turn_harness()
    state, client = h._make_state(tmp_path, context_builder=_context_builder(h))
    client.capabilities = capabilities_for(backend)
    slot = h._make_slot()
    event = _browser(
        h, "req-1", _call("click", ref="e3"), sub_session=sub_session, native_child=native_child
    )
    h._set_stream(client, [event, h._complete_event()])
    answerer = asyncio.get_event_loop().create_task(
        h._answer_approval(slot, "req-1", "rejected_once")
    )
    await _run_answered(h, run_chat, state, slot, answerer)
    meta = _card_meta(slot)
    assert "browser_trust_class" not in meta
    assert meta.get("trust_command_grantable") != "1"


@pytest.mark.asyncio
async def test_a_confirm_tools_pattern_withholds_the_site_grant(tmp_path, page):
    h, run_chat = _turn_harness()
    cb = _context_builder(h)
    cb.hooks._config = SimpleNamespace(
        confirm_tools=["@kirocrew-core/browser"], confirm_tools_unknown=False
    )
    state, client = h._make_state(tmp_path, context_builder=cb)
    client.capabilities = capabilities_for(ACP_BACKEND_KAS)
    slot = h._make_slot()
    h._set_stream(client, [_browser(h, "req-1", _call("snapshot")), h._complete_event()])
    answerer = asyncio.get_event_loop().create_task(
        h._answer_approval(slot, "req-1", "rejected_once")
    )
    await _run_answered(h, run_chat, state, slot, answerer)
    assert "browser_trust_class" not in _card_meta(slot)


@pytest.mark.asyncio
async def test_a_site_grant_approves_that_site_and_asks_for_another(tmp_path, page):
    h, run_chat = _turn_harness()
    state, client = h._make_state(tmp_path, context_builder=_context_builder(h))
    client.capabilities = capabilities_for(ACP_BACKEND_KAS)
    slot = h._make_slot()
    slot._trusted_patterns.add(bht.grant_pattern(bht.trust_key("a.example", "act")))
    on_site = _browser(h, "req-2", _call("click", ref="e1"))
    read_on_site = _browser(h, "req-3", _call("snapshot"))
    off_site = _browser(h, "req-4", _call("navigate", url="https://b.example/"))
    h._set_stream(client, [on_site, read_on_site, off_site, h._complete_event()])
    answerer = asyncio.get_event_loop().create_task(
        h._answer_approval(slot, "req-4", "rejected_once")
    )
    await _run_answered(h, run_chat, state, slot, answerer)
    client.approve_tool.assert_any_call("req-2")
    client.approve_tool.assert_any_call("req-3")
    client.reject_tool.assert_any_call("req-4")
    asked = [m for m in slot.messages if m.get("role") == "permission"]
    assert len(asked) == 1  # only the other site asked
    # Each approval is on the op route's books for this chat's browser session.
    assert bht.take_host_approval(slot.key, "click", {"ref": "e1"}) == "a.example"
    assert bht.take_host_approval(slot.key, "snapshot", {}) == "a.example"
    assert bht.take_host_approval(slot.key, "navigate", {"url": "https://b.example/"}) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("grants", [True, False])
async def test_the_call_that_makes_the_site_grant_is_checked_too(tmp_path, page, grants):
    h, run_chat = _turn_harness()
    state, client = h._make_state(tmp_path, context_builder=_context_builder(h))
    client.capabilities = capabilities_for(ACP_BACKEND_KAS)
    slot = h._make_slot()
    h._set_stream(client, [_browser(h, "req-1", _call("click", ref="e3")), h._complete_event()])

    def _site_trust_click() -> None:
        # What the approve route's trust_command branch stores before it answers.
        if grants:
            slot._trusted_patterns.add(bht.grant_pattern(bht.trust_key("a.example", "act")))

    answerer = asyncio.get_event_loop().create_task(
        h._answer_approval(slot, "req-1", "approved", before_answer=_site_trust_click)
    )
    await _run_answered(h, run_chat, state, slot, answerer)
    client.approve_tool.assert_any_call("req-1")
    # A site-trust click is noted for the route; a plain approval is not.
    noted = bht.take_host_approval(slot.key, "click", {"ref": "e3"})
    assert noted == ("a.example" if grants else None)


@pytest.mark.asyncio
async def test_a_plain_approval_is_not_checked_though_a_grant_covers_the_call(tmp_path, page):
    h, run_chat = _turn_harness()
    state, client = h._make_state(tmp_path, context_builder=_context_builder(h))
    client.capabilities = capabilities_for(ACP_BACKEND_KAS)
    slot = h._make_slot()
    slot._trusted_patterns.add(bht.grant_pattern(bht.trust_key("a.example", "act")))
    event = _browser(h, "req-1", _call("click", ref="e3"))
    # The trusted-pattern rung does not read a redacted call, so it meets a card.
    event.tool_input_redacted = True
    h._set_stream(client, [event, h._complete_event()])
    answerer = asyncio.get_event_loop().create_task(h._answer_approval(slot, "req-1", "approved"))
    await _run_answered(h, run_chat, state, slot, answerer)
    client.approve_tool.assert_any_call("req-1")
    assert bht.take_host_approval(slot.key, "click", {"ref": "e3"}) is None


@pytest.mark.asyncio
async def test_trust_everything_does_not_approve_a_browser_call(tmp_path, page):
    h, run_chat = _turn_harness()
    state, client = h._make_state(tmp_path, context_builder=_context_builder(h))
    client.capabilities = capabilities_for(ACP_BACKEND_KAS)
    slot = h._make_slot()
    slot._trusted_patterns.add("*")
    h._set_stream(client, [_browser(h, "req-1", _call("click", ref="e3")), h._complete_event()])
    answerer = asyncio.get_event_loop().create_task(
        h._answer_approval(slot, "req-1", "rejected_once")
    )
    await _run_answered(h, run_chat, state, slot, answerer)
    client.reject_tool.assert_any_call("req-1")
    assert [m for m in slot.messages if m.get("role") == "permission"]


@pytest.mark.asyncio
async def test_a_read_grant_still_asks_to_act(tmp_path, page):
    h, run_chat = _turn_harness()
    state, client = h._make_state(tmp_path, context_builder=_context_builder(h))
    client.capabilities = capabilities_for(ACP_BACKEND_KAS)
    slot = h._make_slot()
    slot._trusted_patterns.add(bht.grant_pattern(bht.trust_key("a.example", "read")))
    h._set_stream(
        client,
        [
            _browser(h, "req-2", _call("read")),
            _browser(h, "req-3", _call("type", ref="e1", text="x")),
            h._complete_event(),
        ],
    )
    answerer = asyncio.get_event_loop().create_task(
        h._answer_approval(slot, "req-3", "rejected_once")
    )
    await _run_answered(h, run_chat, state, slot, answerer)
    client.approve_tool.assert_any_call("req-2")
    client.reject_tool.assert_any_call("req-3")


# ── The approve route ──


@pytest.mark.asyncio
@pytest.mark.parametrize("cls", ["read", "act"])
@pytest.mark.parametrize("ttl", [None, 3600])
async def test_a_site_click_stores_the_class_pattern_for_the_session(
    tmp_path, monkeypatch, _floor_monkeypatch, cls, ttl
):
    from unittest.mock import MagicMock

    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from chat_test_helpers import _make_state

    from kiro_crew.dashboard.chat_handlers import api_chat_slot_approve
    from kiro_crew.dashboard.state import SlotOrigin

    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    state.owner_id = ""

    @web.middleware
    async def _dashboard_user(request: web.Request, handler):
        request["app"] = ""
        request["user"] = "local-app"
        return await handler(request)

    app = web.Application(middlewares=[_dashboard_user])
    app["state"] = state
    app.router.add_post("/api/chat/slots/{slot}/approve", api_chat_slot_approve)

    key = bht.trust_key("a.example", cls)
    slot = state.get_or_create_slot("selected", origin=SlotOrigin.USER)
    fut = asyncio.get_running_loop().create_future()
    meta = {
        "request_id": "r1",
        "full_command": "a.example",
        "trust_command_key": key,
        "trust_command_grantable": "1",
        "browser_trust_class": cls,
    }
    row = slot.append("permission", "Drive browser", json.dumps(meta))
    slot.register_approval("r1", fut, row)
    mid = slot.approval_instance("r1")
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/api/chat/slots/selected/approve",
            json={
                "action": "trust_command",
                "pattern": "a.example",
                "request_id": "r1",
                "origin": "native",
                "request_mid": mid,
                **({} if ttl is None else {"ttl_seconds": ttl}),
            },
        )
        body = await resp.json()
    if ttl is not None:
        # A site grant has no lifetime: one asked for is refused, and nothing is granted.
        assert resp.status == 400 and body["code"] == "ttl_unsupported"
        assert not fut.done() and not slot._trusted_patterns
        assert not slot._trusted_pattern_deadlines
        return
    assert resp.status == 200
    assert fut.result() == "approved"
    stored = set(slot._trusted_patterns)
    assert stored == {bht.grant_pattern(key)}
    assert not slot._trusted_pattern_deadlines  # a site grant has no lifetime
    assert bool(matches_trusted_pattern(bht.trust_key("a.example", "act"), stored)) is (
        cls == "act"
    )


# ── The op route ──


class _Supervisor:
    """The daemon, as the op route sees it: one tab, on *url*."""

    def __init__(self, url: str) -> None:
        from unittest.mock import MagicMock

        self.calls: list[str] = []
        self.relay = MagicMock()
        self.relay.agent_targets.return_value = ["T1"]
        self.relay.context_of.return_value = ""
        self.tab = {"targetId": "T1", "url": url, "title": "t"}

    def status(self) -> dict:
        return {"browser": {"kind": "kiro_chromium", "headless": True}, "connection": "connected"}

    async def call(self, helper: str, **kwargs: object) -> object:
        self.calls.append(helper)
        if helper in ("js", "js_isolated"):
            return {"url": self.tab["url"], "title": "t", "nodes": []}
        if helper == "current_tab":
            return self.tab
        return {}


@pytest.fixture
def route(monkeypatch, _floor_monkeypatch):
    from unittest.mock import MagicMock

    from kiro_crew.browser_harness import live_hub as hub_mod
    from kiro_crew.dashboard.handlers import browser_ops as mod

    _floor_monkeypatch.setattr(mod, "_sel", lambda: MagicMock())
    hub_mod.reset_live_hub()

    async def _start(self, key, relay, target, **kw):
        sess = self._session(key)
        sess.url, sess.title = kw.get("url", ""), kw.get("title", "")

    monkeypatch.setattr(hub_mod.LiveHub, "start_stream", _start)

    def install(sup: _Supervisor) -> None:
        async def _sup(hub, key):
            return sup

        monkeypatch.setattr(mod, "_supervisor", _sup)

    yield SimpleNamespace(mod=mod, install=install)
    hub_mod.reset_live_hub()


@pytest.mark.asyncio
async def test_the_route_refuses_a_host_approved_op_on_another_host(route):
    sup = _Supervisor("https://b.example/")
    route.install(sup)
    _note("a.example", "read", "snapshot")
    answer, status = await route.mod.run_browser_op("dashboard:chat-1", "snapshot", {})
    assert status == 200
    assert answer["ok"] is False and answer["code"] == bht.SCOPE_CHANGED_CODE
    assert "js" not in sup.calls and "js_isolated" not in sup.calls  # the op never ran


@pytest.mark.asyncio
async def test_the_route_runs_a_host_approved_op_on_its_host(route):
    sup = _Supervisor("https://a.example/p")
    route.install(sup)
    _note("a.example", "read", "snapshot")
    answer, _status = await route.mod.run_browser_op("dashboard:chat-1", "snapshot", {})
    assert answer["ok"] is True
    assert bht.take_host_approval("chat-1", "snapshot", {}) is None


@pytest.mark.asyncio
async def test_a_call_refused_for_a_tab_move_takes_its_own_note(route):
    from kiro_crew.browser_harness.live_hub import get_live_hub

    sup = _Supervisor("https://a.example/p")
    route.install(sup)
    _note("a.example", "act", "click", ref="e1")
    get_live_hub().note_tab_moved("chat-1", {"popup": {"target_id": "P1"}})
    answer, _status = await route.mod.run_browser_op("dashboard:chat-1", "click", {"ref": "e1"})
    assert answer["ok"] is False and answer["code"] == "popup_opened"
    # Gone, so a later identical call approved on its own card is not refused for it.
    assert bht.take_host_approval("chat-1", "click", {"ref": "e1"}) is None


@pytest.mark.parametrize(
    ("op", "args"),
    [
        ("cdp", {"method": "Storage.getCookies"}),
        ("js", {"expression": "1"}),
        ("http_get", {"url": "https://example.com/"}),
        ("upload", {"ref": "e3", "path": "/tmp/a.txt"}),
    ],
)
def test_a_call_that_reaches_past_one_site_gets_no_site_key(op: str, args: dict) -> None:
    # cdp can reach every tab and cookie; js naming a tab or frame runs on a host no
    # grant names: an act grant on the current page never covers them.
    from kiro_crew.browser_harness import op_classes

    assert op_classes.host_trust_class(op, args) == ""
    # tabs close names a tab whose host no grant names: it always asks too.
    assert op_classes.host_trust_class("tabs", {"action": "close", "target_id": "T"}) == ""
    assert op_classes.host_trust_class("tabs", {"action": "switch"}) == op_classes.CLASS_ACT


def test_a_full_session_refuses_a_new_note_and_keeps_the_old_ones(monkeypatch) -> None:
    # A dropped note would let its call run unchecked on another host.
    monkeypatch.setattr(bht, "_pending", {})
    key = bht.trust_key("a.example", "act")
    for i in range(bht._PER_SESSION_CAP):
        assert bht.note_host_approval("dashboard:chat-cap", key, _call("click", ref=f"e{i}"))
    assert not bht.note_host_approval("dashboard:chat-cap", key, _call("click", ref="extra"))
    live = bht.browser_session_key("dashboard:chat-cap")
    assert bht.take_host_approval(live, "click", {"ref": "e0"}) == "a.example"
