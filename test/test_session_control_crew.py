"""``crew=`` on the five session tools (rfc-conductor-remote-dispatch.md, wave 1).

Drives the real ``/api/session-control/*`` handlers with a stubbed request, a real
dashboard state and a stub peer that records every call the hub makes over the
tunnel carrier. Pinned here:

* switch off (the default): the advertised schemas are unchanged, and a call
  carrying ``crew`` is refused ``remote_dispatch_disabled`` with no peer call;
* switch on: each verb lands on the ``api/chat`` route RFC §1 names, with
  ``agent``/``model`` passed through, ``memory_mode`` always on the create, and no
  ``agent`` sent when none was given;
* a path outside the proxy allowlist is refused server-side, and a hostile peer
  slot key never becomes a path;
* a credential planted in a peer reply reaches the caller redacted;
* a non-owner caller and a peer slot this caller did not dispatch are refused;
* nearby inputs: unknown, ambiguous, disconnected and version-skewed crews.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import aiohttp
import pytest
from chat_test_helpers import _make_state
from session_control_helpers import (  # noqa: F401 -- the autouse fixtures register by import
    _busy,
    _enabled_fixture,
    _fresh_create_budget,
    _fresh_stop_windows,
    _key,
    _slot,
)

import kiro_crew
from kiro_crew import mcp_dashboard
from kiro_crew.config.section_builders import _build_work_ledger_config
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.handlers import remote_dispatch_routes as crew
from kiro_crew.dashboard.handlers import session_control as handlers_sc
from kiro_crew.dashboard.peer_redaction import redact_peer_text
from kiro_crew.instances.ssh_tunnel_manager import ProxyRequestError
from kiro_crew.mcp_session_crew import CREW_TOOLS, with_crew_schema
from kiro_crew.remote_dispatch import REMOTE_DISPATCH_DISABLED

#: A GitHub-style personal access credential, built at runtime so no scanner
#: reads this file as holding one.
PLANTED = "ghp_" + "Ab3x9" * 7 + "Z"

INSTANCE = "inst-lab"

#: The real owner check, kept before any test stubs it.
_REAL_OWNER_CHECK = crew.owner_unverifiable_reason


class _Content:
    def __init__(self, raw: bytes, fail: bool = False) -> None:
        self._raw = raw
        self._fail = fail

    async def iter_any(self):
        if self._fail:
            raise aiohttp.ClientPayloadError("cut")
        yield self._raw


class StubPeer:
    """The two carrier methods the hub calls, recording every call."""

    def __init__(self, version: str = kiro_crew.__version__) -> None:
        self.version = version
        self.calls: list[dict[str, Any]] = []
        self.replies: dict[tuple[str, str], tuple[int, Any]] = {}
        self.not_connected = False
        self.cut_body = False
        self.content_type = "application/json"

    def reply(self, method: str, path: str, status: int, body: Any) -> None:
        self.replies[(method, path)] = (status, body)

    async def peer_capability(self, instance_id: str, path: str):
        if self.not_connected:
            # The real carrier's spelling (``ssh_tunnel_manager_peer.peer_capability``).
            return False, {"error": "down", "code": "capability_peer_not_connected"}
        return True, {"version": self.version}

    @asynccontextmanager
    async def proxy_request(
        self, instance_id, method, path, *, params=None, data=None, content_type=""
    ):
        if self.not_connected:
            raise ProxyRequestError("proxy_peer_not_connected", "down", http_status=503)
        self.calls.append(
            {
                "instance": instance_id,
                "method": method,
                "path": path,
                "params": dict(params or {}),
                "body": json.loads(data) if data else None,
            }
        )
        status, body = self.replies.get((method, path), (200, {"ok": True}))
        yield SimpleNamespace(
            status=status,
            headers={"Content-Type": self.content_type},
            content=_Content(json.dumps(body).encode(), fail=self.cut_body),
        )


class _Registry:
    def __init__(self, rows) -> None:
        self._rows = rows

    def list(self):
        return list(self._rows)


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A real state with one connected crew, the switch on, and the caller's tab."""
    state = _make_state(tmp_path)
    peer = StubPeer()
    state.instances_manager = peer
    state.instances_registry = _Registry(
        [SimpleNamespace(id=INSTANCE, name="lab"), SimpleNamespace(id="inst-b", name="other")]
    )
    caller = _busy(_slot(state, "chat-1"))
    monkeypatch.setattr(crew, "remote_dispatch_enabled", lambda: True)
    # The verb tests stand in an admitting owner check so the rest of the path
    # can be driven; the real one is pinned by the ``*_owner_*`` tests below.
    monkeypatch.setattr(crew, "owner_unverifiable_reason", lambda state: "")
    return SimpleNamespace(state=state, peer=peer, caller=caller)


def _request(state, key: str, *, body: dict | None = None, query: dict | None = None, path=""):
    request = MagicMock()
    request.app = {"state": state}
    request.path = path
    request.method = "GET" if query is not None else "POST"
    request.headers = {"X-Session-Key": key}
    request.query = query or {}
    marks = {"internal_auth": True, "peer_verified": True, "app": ""}
    request.get = lambda k, default=None: marks.get(k, default)

    async def _json():
        return body or {}

    request.json = _json
    return request


_ROUTES = {
    "create": handlers_sc.api_session_control_create,
    "send": handlers_sc.api_session_control_send,
    "stop": handlers_sc.api_session_control_stop,
    "close": handlers_sc.api_session_control_close,
}


def _call(env, verb: str, key: str | None = None, **fields) -> tuple[int, dict]:
    key = key or _key(env.caller)
    if verb == "read":
        req = _request(env.state, key, query={k: str(v) for k, v in fields.items()})
        resp = asyncio.run(handlers_sc.api_session_control_read(req))
    else:
        req = _request(env.state, key, body=fields)
        resp = asyncio.run(_ROUTES[verb](req))
    return resp.status, json.loads(resp.body.decode())


def _create(env, **fields) -> str:
    env.peer.reply("POST", "api/chat/slots", 200, {"key": "chat-77", "agent": "kirocrew-worker"})
    status, body = _call(env, "create", crew="lab", **fields)
    assert status == 200, body
    return body["target"]


# ── switch off: nothing changes ──────────────────────────────────────────────


def test_the_switch_defaults_off_and_reads_only_a_real_bool():
    assert _build_work_ledger_config({}).remote_dispatch is False
    assert _build_work_ledger_config({"remote_dispatch": True}).remote_dispatch is True
    assert _build_work_ledger_config({"remote_dispatch": "true"}).remote_dispatch is False
    assert _build_work_ledger_config({"remote_dispatch": True, "max_depth": 3}).max_depth == 3


def test_switch_off_advertises_exactly_todays_schemas(monkeypatch):
    monkeypatch.setattr("kiro_crew.remote_dispatch.remote_dispatch_enabled", lambda: False)
    assert mcp_dashboard._list_tools() == mcp_dashboard.TABLE.list()
    for tool in mcp_dashboard._list_tools():
        assert "crew" not in tool["inputSchema"].get("properties", {})


def test_switch_on_adds_crew_to_the_five_tools_only(monkeypatch):
    monkeypatch.setattr("kiro_crew.remote_dispatch.remote_dispatch_enabled", lambda: True)
    listed = {t["name"]: t for t in mcp_dashboard._list_tools()}
    base = {t["name"]: t for t in mcp_dashboard.TABLE.list()}
    for name, tool in listed.items():
        has = "crew" in tool["inputSchema"].get("properties", {})
        assert has is (name in CREW_TOOLS), name
        if name not in CREW_TOOLS:
            assert tool == base[name]
    # The table's own descriptors are not mutated by the overlay.
    assert all(
        "crew" not in t["inputSchema"].get("properties", {}) for t in mcp_dashboard.TABLE.list()
    )
    assert with_crew_schema([{"name": "x"}], False) == [{"name": "x"}]


@pytest.mark.parametrize(
    "verb, fields",
    [
        ("create", {"crew": "lab"}),
        ("send", {"crew": "lab", "target": "chat-77", "message": "hi"}),
        ("read", {"crew": "lab", "target": "chat-77"}),
        ("stop", {"crew": "lab", "target": "chat-77"}),
        ("close", {"crew": "lab", "target": "chat-77"}),
        ("send", {"crew": None, "target": "chat-77", "message": "hi"}),
    ],
)
def test_switch_off_refuses_any_crew_call_typed_with_no_peer_call(env, monkeypatch, verb, fields):
    monkeypatch.setattr(crew, "remote_dispatch_enabled", lambda: False)
    status, body = _call(env, verb, **fields)
    assert (status, body["code"]) == (403, REMOTE_DISPATCH_DISABLED)
    assert env.peer.calls == []


def test_a_call_without_crew_never_enters_the_crew_path(env, monkeypatch):
    async def _boom(*a, **k):
        raise AssertionError("local call routed to the crew handler")

    monkeypatch.setattr(crew, "handle_crew_call", _boom)
    target = _slot(env.state, "chat-2")
    status, body = _call(env, "stop", target=target.key)
    # The local verb answered (an idle target has nothing to stop).
    assert status == 200 and "crew" not in body, body
    assert env.peer.calls == []


def test_the_schema_accepts_crew_so_the_gateway_can_answer_it():
    cleaned = mcp_dashboard.TABLE.validate(
        "session_send", {"target": "t", "message": "m", "crew": "lab"}
    )
    assert cleaned["crew"] == "lab"
    assert "crew" not in mcp_dashboard.TABLE.validate(
        "session_send", {"target": "t", "message": "m"}
    )


# ── switch on: each verb lands on its api/chat route ─────────────────────────


def test_create_posts_slots_with_memory_mode_then_renames(env):
    target = _create(env, title="worker A", agent="kirocrew-worker", model="sonnet")
    assert target == "chat-77"
    first, second = env.peer.calls
    assert (first["instance"], first["method"], first["path"]) == (
        INSTANCE,
        "POST",
        "api/chat/slots",
    )
    assert first["body"]["agent"] == "kirocrew-worker" and first["body"]["model"] == "sonnet"
    assert "memory_mode" in first["body"]
    assert (second["method"], second["path"], second["body"]) == (
        "PATCH",
        "api/chat/slots/chat-77/title",
        {"title": "worker A"},
    )


def test_create_without_agent_sends_no_agent(env):
    _create(env)
    (only,) = env.peer.calls
    assert set(only["body"]) == {"memory_mode"}


def test_send_read_stop_close_each_land_on_their_route(env):
    target = _create(env)
    env.peer.calls.clear()
    env.peer.reply("GET", f"api/chat/slots/{target}", 200, {"title": "w", "messages": []})
    assert _call(env, "send", crew="lab", target=target, message="go")[0] == 200
    assert _call(env, "read", crew="lab", target=target, limit=5)[0] == 200
    assert _call(env, "stop", crew="lab", target=target)[0] == 200
    assert _call(env, "close", crew="lab", target=target)[0] == 200
    seen = [(c["method"], c["path"], c["params"]) for c in env.peer.calls]
    assert seen == [
        ("POST", "api/chat", {"ws": "1"}),
        ("GET", f"api/chat/slots/{target}", {"limit": "5"}),
        ("POST", f"api/chat/slots/{target}/stop", {}),
        ("DELETE", f"api/chat/slots/{target}", {}),
    ]
    assert env.peer.calls[0]["body"] == {"message": "go", "slot": target, "turn_origin": "agent"}


def test_close_forgets_the_slot(env):
    target = _create(env)
    assert _call(env, "close", crew="lab", target=target)[0] == 200
    status, body = _call(env, "send", crew="lab", target=target, message="again")
    assert (status, body["code"]) == (403, "crew_target_not_dispatched")


def test_a_create_with_work_item_is_born_bound_on_the_callers_ledger(env, monkeypatch):
    """Wave 4: ``work_item`` routes the create through the bound path, on the ledger
    the caller's own key names, and the reply says which item it bound."""
    from kiro_crew import session_ledger
    from kiro_crew.dashboard.handlers import remote_ingest

    seen: dict = {}

    async def _bound(state, mgr, board, instance_id, item_id, create):
        seen.update(board=board, instance=instance_id, item=item_id, create=create)
        return "chat-77", {"key": "chat-77"}, SimpleNamespace(item_id=item_id, title="item one")

    monkeypatch.setattr(remote_ingest, "create_bound", _bound)
    status, body = _call(env, "create", crew="lab", work_item="it_0000abcd")
    assert status == 200, body
    assert body["work_item_id"] == "it_0000abcd" and body["title"] == "item one"
    assert seen["board"] == session_ledger.ledger_key(_key(env.caller))
    assert seen["instance"] == INSTANCE and "memory_mode" in seen["create"]


def test_after_a_restart_the_item_pair_still_fences_the_slot(env, monkeypatch):
    """The in-memory record is gone after a hub restart; an open item bound to the
    slot on that crew still admits the verbs, and nothing else does."""
    from kiro_crew.dashboard.handlers import remote_ingest

    target = _create(env)
    crew._DISPATCHED.clear()

    async def never(board, inst, slot):
        return False

    monkeypatch.setattr(remote_ingest, "holds_peer_slot", never)
    status, body = _call(env, "send", crew="lab", target=target, message="again")
    assert (status, body["code"]) == (403, "crew_target_not_dispatched")

    async def only_target(board, inst, slot):
        return (inst, slot) == (INSTANCE, target)

    monkeypatch.setattr(remote_ingest, "holds_peer_slot", only_target)
    assert _call(env, "send", crew="lab", target=target, message="again")[0] == 200


def test_a_crew_create_refuses_local_only_fields(env):
    status, body = _call(env, "create", crew="lab", folder_id="f1")
    assert (status, body["code"]) == (400, "crew_field_unsupported")
    assert env.peer.calls == []


# ── the path fence ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "path",
    [
        "api/instances/x/proxy/api/chat",
        "api/chat/../instances",
        "api/%252e%252e/api/instances",
        "api/agents",
    ],
)
def test_a_path_outside_the_allowlist_is_refused_server_side(path):
    peer = StubPeer()
    with pytest.raises(crew.CrewCallError) as err:
        asyncio.run(crew.peer_call(peer, INSTANCE, "GET", path))
    assert err.value.code == "proxy_path_denied"
    assert peer.calls == []


@pytest.mark.parametrize("key", ["../../api/instances", "a/b", "..", "", 7])
def test_a_hostile_create_key_never_becomes_a_path(env, key):
    env.peer.reply("POST", "api/chat/slots", 200, {"key": key})
    status, body = _call(env, "create", crew="lab")
    assert (status, body["code"]) == (502, "crew_reply_malformed")
    assert len(env.peer.calls) == 1
    assert crew._record(env.state) == {}


def test_a_non_json_peer_reply_is_refused(env):
    env.peer.content_type = "text/html"
    status, body = _call(env, "create", crew="lab")
    assert (status, body["code"]) == (502, "proxy_content_type_refused")


# ── redaction ────────────────────────────────────────────────────────────────


def test_the_planted_credential_is_one_the_peer_chain_masks():
    assert PLANTED not in redact_peer_text(f"see {PLANTED}")


def test_a_credential_in_a_peer_reply_reads_redacted(env):
    target = _create(env, title="w")
    env.peer.reply(
        "GET",
        f"api/chat/slots/{target}",
        200,
        {"title": f"t {PLANTED}", "messages": [{"role": "assistant", "content": f"key {PLANTED}"}]},
    )
    status, body = _call(env, "read", crew="lab", target=target)
    assert status == 200
    assert PLANTED not in json.dumps(body)
    assert body["messages"][0]["role"] == "assistant"


def test_a_credential_in_a_peer_refusal_reads_redacted(env):
    env.peer.reply("POST", "api/chat/slots", 400, {"error": f"bad {PLANTED}"})
    status, body = _call(env, "create", crew="lab")
    assert status == 502
    assert PLANTED not in json.dumps(body)


# ── the caller rule ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "key", ["slack:C1:1700000000.000100", "cron:job-1", "subagent:abc", "side:chat-1", "workflow:w"]
)
def test_a_non_dashboard_caller_is_refused(env, key):
    status, body = _call(env, "create", key=key, crew="lab")
    assert (status, body["code"]) == (403, "owner_only")
    assert env.peer.calls == []


def test_a_worker_opened_for_another_session_is_refused(env):
    worker = _busy(_slot(env.state, "chat-w"))
    worker._created_by = _key(env.caller)
    status, body = _call(env, "create", key=_key(worker), crew="lab")
    assert (status, body["code"]) == (403, "owner_only")
    assert env.peer.calls == []


def test_a_channel_turn_is_refused(env):
    env.caller._turn_channel_origin = True
    status, body = _call(env, "create", crew="lab")
    assert (status, body["code"]) == (403, "channel_caller")


def test_a_tab_with_no_running_turn_is_refused(env):
    env.caller.task = None
    status, body = _call(env, "create", crew="lab")
    assert (status, body["code"]) == (409, "no_dashboard_turn")


# ── the target fence ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("verb", ["send", "read", "stop", "close"])
def test_a_peer_slot_this_caller_did_not_dispatch_is_refused(env, verb):
    fields = {"crew": "lab", "target": "chat-person"}
    if verb == "send":
        fields["message"] = "hi"
    status, body = _call(env, verb, **fields)
    assert (status, body["code"]) == (403, "crew_target_not_dispatched")
    assert env.peer.calls == []


def test_another_callers_dispatch_is_refused(env):
    target = _create(env)
    other = _busy(_slot(env.state, "chat-9"))
    env.peer.calls.clear()
    status, body = _call(env, "stop", key=_key(other), crew="lab", target=target)
    assert (status, body["code"]) == (403, "crew_target_not_dispatched")
    assert env.peer.calls == []


def test_the_same_slot_on_another_crew_is_refused(env):
    target = _create(env)
    env.peer.calls.clear()
    status, body = _call(env, "stop", crew="other", target=target)
    assert (status, body["code"]) == (403, "crew_target_not_dispatched")


# ── nearby inputs ────────────────────────────────────────────────────────────


def test_an_unknown_crew_is_refused(env):
    status, body = _call(env, "create", crew="nope")
    assert (status, body["code"]) == (404, "crew_unknown")


def test_an_ambiguous_name_is_refused(env):
    env.state.instances_registry = _Registry(
        [SimpleNamespace(id="a", name="lab"), SimpleNamespace(id="b", name="lab")]
    )
    status, body = _call(env, "create", crew="lab")
    assert (status, body["code"]) == (409, "crew_ambiguous")


def test_an_id_resolves_and_wins_over_a_name(env):
    env.state.instances_registry = _Registry(
        [SimpleNamespace(id="x", name=INSTANCE), SimpleNamespace(id=INSTANCE, name="lab")]
    )
    _call(env, "create", crew=INSTANCE)
    assert env.peer.calls[0]["instance"] == INSTANCE


@pytest.mark.parametrize("value", ["", "   ", 5, None])
def test_an_empty_or_non_string_crew_is_refused(env, value):
    status, body = _call(env, "create", crew=value)
    assert (status, body["code"]) == (400, "crew_invalid")


def test_a_disconnected_crew_is_refused_typed(env):
    env.peer.not_connected = True
    status, body = _call(env, "create", crew="lab")
    assert (status, body["code"]) == (503, "proxy_peer_not_connected")


def test_a_version_skewed_crew_is_refused_before_any_create(env):
    env.peer.version = "0.0.1"
    status, body = _call(env, "create", crew="lab")
    assert (status, body["code"]) == (409, "crew_version_mismatch")
    assert env.peer.calls == []


def test_the_dispatch_cap_refuses_one_more(env, monkeypatch):
    monkeypatch.setattr(crew, "MAX_CREW_SLOTS_PER_CALLER", 1)
    _create(env)
    status, body = _call(env, "create", crew="lab")
    assert (status, body["code"]) == (409, "crew_dispatch_limit")


def test_the_session_control_switch_withdraws_crew_calls_too(env, monkeypatch):
    monkeypatch.setattr(sc, "session_control_enabled", lambda: False)
    status, body = _call(env, "create", crew="lab")
    assert (status, body["code"]) == (403, "session_control_disabled")
    assert env.peer.calls == []


def test_a_reply_cut_off_mid_body_is_a_typed_refusal(env):
    env.peer.cut_body = True
    status, body = _call(env, "create", crew="lab")
    assert (status, body["code"]) == (502, "crew_peer_interrupted")
    assert crew._record(env.state) == {}


def test_a_failed_rename_keeps_the_created_key(env):
    env.peer.reply("POST", "api/chat/slots", 200, {"key": "chat-77"})
    env.peer.reply("PATCH", "api/chat/slots/chat-77/title", 500, {"error": "no"})
    status, body = _call(env, "create", crew="lab", title="w")
    assert status == 200 and body["target"] == "chat-77" and body["title_error"]
    assert crew.dispatched_by(env.state, env.caller.key, INSTANCE, "chat-77")


@pytest.mark.parametrize(
    "reply, steered", [({"ok": True, "steered": True}, True), ({"ok": True, "queued": True}, False)]
)
def test_the_steer_receipt_reports_what_the_crew_did(env, reply, steered):
    target = _create(env)
    env.peer.reply("POST", "api/chat", 200, reply)
    status, body = _call(env, "send", crew="lab", target=target, message="now", steer=True)
    assert status == 200 and body["steered"] is steered
    assert env.peer.calls[-1]["body"]["steer"] is True


def test_a_bound_worker_cannot_dispatch_to_a_crew(env, monkeypatch):
    from kiro_crew import session_ledger, work_ledger

    worker_key = session_ledger.ledger_key(_key(env.caller))
    real = work_ledger.read_binding
    monkeypatch.setattr(
        work_ledger,
        "read_binding",
        lambda key, **kw: (
            ("chat-conductor", "it_00000001") if key == worker_key else real(key, **kw)
        ),
    )
    status, body = _call(env, "create", crew="lab")
    assert (status, body["code"]) == (403, crew.CALLER_IS_WORKER)
    assert env.peer.calls == []


def test_an_unreadable_binding_fails_closed(env, monkeypatch):
    from kiro_crew import work_ledger

    def _boom(key, **kw):
        raise OSError("busy")

    monkeypatch.setattr(work_ledger, "read_binding", _boom)
    status, body = _call(env, "create", crew="lab")
    assert (status, body["code"]) == (403, crew.CALLER_IS_WORKER)
    assert env.peer.calls == []


def test_an_unbound_owner_tab_is_admitted(env):
    assert crew.caller_is_bound_worker(_key(env.caller)) is False
    assert _create(env) == "chat-77"


def test_an_incognito_tab_cannot_use_crew(env):
    env.caller.memory_mode = "incognito"
    status, body = _call(env, "create", crew="lab")
    assert (status, body["code"]) == (403, "ephemeral_caller")
    assert env.peer.calls == []


def test_outbound_text_is_scrubbed_before_it_leaves(env):
    target = _create(env, title=f"t {PLANTED}")
    assert PLANTED not in json.dumps(env.peer.calls)
    assert _call(env, "send", crew="lab", target=target, message=f"use {PLANTED}")[0] == 200
    assert PLANTED not in json.dumps(env.peer.calls[-1]["body"])


@pytest.mark.parametrize("field", ["agent", "model"])
def test_a_selection_carrying_a_credential_is_refused_unsent(env, field):
    status, body = _call(env, "create", crew="lab", **{field: PLANTED})
    assert (status, body["code"]) == (400, "crew_field_invalid")
    assert env.peer.calls == []


def test_a_declined_stop_is_reported_as_declined(env):
    target = _create(env)
    env.peer.reply("POST", f"api/chat/slots/{target}/stop", 200, {"ok": True, "compacting": True})
    status, body = _call(env, "stop", crew="lab", target=target)
    assert status == 200 and body["stopped"] is False and body["compacting"] is True


def test_a_turn_the_owner_did_not_send_is_refused(env, monkeypatch):
    """The real owner check: a turn not sent by the owner (a wake, a session_send)."""
    monkeypatch.setattr(crew, "owner_unverifiable_reason", _REAL_OWNER_CHECK)
    env.caller._turn_owner_sent = False
    status, body = _call(env, "create", crew="lab")
    assert (status, body["code"]) == (403, crew.OWNER_UNVERIFIABLE)
    assert env.peer.calls == []


def test_a_turn_the_owner_sent_is_admitted(env, monkeypatch):
    """The real owner check admits the owner's own turn in their own tab."""
    monkeypatch.setattr(crew, "owner_unverifiable_reason", _REAL_OWNER_CHECK)
    env.caller._turn_owner_sent = True
    assert _REAL_OWNER_CHECK(env.caller) == ""
    assert _create(env) == "chat-77"


def test_the_owner_check_is_the_crew_read_gate_check():
    from kiro_crew.dashboard.handlers_instances_crew_reads import _slot_refusal
    from kiro_crew.dashboard.state import _ChatSlot

    slot = _ChatSlot("chat-x")
    assert _slot_refusal(slot) is not None and _REAL_OWNER_CHECK(slot)
    slot._turn_owner_sent = True
    assert _slot_refusal(slot) is None and _REAL_OWNER_CHECK(slot) == ""
    slot._created_by = "dashboard:chat-1"
    assert _REAL_OWNER_CHECK(slot)


@pytest.mark.parametrize("steer", [False, True])
def test_every_crew_send_is_marked_agent_origin(env, steer):
    """A conductor's message must reach the crew as an agent's, never a person's turn."""
    from kiro_crew.dashboard.agent_origin import send_is_agent_origin

    target = _create(env)
    fields = {"crew": "lab", "target": target, "message": "go"}
    if steer:
        fields["steer"] = True
    assert _call(env, "send", **fields)[0] == 200
    assert send_is_agent_origin(env.peer.calls[-1]["body"])
