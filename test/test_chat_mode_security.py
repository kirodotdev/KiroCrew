"""Security contract of ``api_chat_mode``.

``api_chat_mode`` (``src/kiro_crew/dashboard/chat_handlers.py``) carried three
defects, all in the ordering between slot validation and global mutation:

1. ``trust_reads`` silently widened to EVERY slot when the named slot did not
   resolve — its ``trust``/``normal`` siblings answer ``400 unknown slot``.
2. A request rejected for an unknown slot had already revoked the
   process-global safety override (``safety_override().deactivate()`` ran
   before the ``400``). A refused request must leave the global grant and
   every slot untouched.
3. ``deactivate()`` — which writes a SEL event — ran inline on the gateway
   loop, unlike the sibling ``activate()`` which is offloaded with
   ``asyncio.to_thread``.

Every test drives the real handler through an aiohttp ``TestClient``; the auth
middleware is stood in by ``_dashboard_owner_request``, and ``safety_override``
is either the real singleton (happy paths) or a recording fake (the rejection
paths, where the contract is "never even called").
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import inspect
import json
import threading
from operator import attrgetter
from typing import NamedTuple
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TEXT_CHUNK
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard import chat_persistence, chat_runner, session_control
from kiro_crew.dashboard.chat_delivery import TURN_ACTOR_META_KEY, queue_for_next_turn
from kiro_crew.dashboard.chat_handlers import (
    api_chat,
    api_chat_mode,
    api_chat_slot_approve,
)
from kiro_crew.dashboard.chat_title import (
    _counts_as_user_turn,
    _maybe_auto_title,
    _rehydrated_refresh_mark,
    maybe_refresh_title,
)
from kiro_crew.dashboard.chat_utils import (
    CRON_NOTIFICATION_KIND,
    CRON_NOTIFY_PREFIX,
    MCP_APP_MESSAGE_KIND,
    SYNTHETIC_RECOVERY_KIND,
    TURN_END_WIRE_CLS,
    _dequeue_next_message,
)
from kiro_crew.dashboard.handlers.sessions import api_approval_resolve
from kiro_crew.dashboard.slot_ownership import app_may_control_session
from kiro_crew.dashboard.slot_queue_repository import (
    durable_queue_entries,
    sanitize_restored_queue,
)
from kiro_crew.dashboard.state import SlotOrigin, row_mid
from kiro_crew.providers.base import LLMEvent
from kiro_crew.safety_override import (
    reset_singleton,
)
from kiro_crew.safety_override import safety_override as real_safety_override


@web.middleware
async def _dashboard_owner_request(request: web.Request, handler):
    """Stand in for the auth middleware: a dashboard-owner request.

    ``deny_non_dashboard_caller`` accepts a caller matching the configured
    owner, or a local bootstrap subject when no owner is configured; these
    tests configure no owner, so ``local-app`` passes.
    """
    request["app"] = ""
    request["user"] = "local-app"
    return await handler(request)


def _make_mode_app(state) -> web.Application:
    app = web.Application(middlewares=[_dashboard_owner_request])
    app["state"] = state
    app.router.add_post("/api/chat/mode", api_chat_mode)
    return app


@web.middleware
async def _app_request(request: web.Request, handler):
    request["app"] = "crew-keyboard"
    request["user"] = ""
    return await handler(request)


def _make_app_token_mode_app(state) -> web.Application:
    app = web.Application(middlewares=[_app_request])
    app["state"] = state
    app.router.add_post("/api/chat", api_chat)
    app.router.add_post("/api/chat/mode", api_chat_mode)
    app.router.add_post("/api/chat/slots/{slot}/approve", api_chat_slot_approve)
    return app


@pytest.fixture(autouse=True)
def _hermetic_home(tmp_path, monkeypatch):
    """Redirect KIROCREW_HOME so SEL writes never touch the developer's home."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))


@pytest.fixture(autouse=True)
def _isolate_safety_override():
    reset_singleton()
    yield
    reset_singleton()


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    st = _make_state(tmp_path)
    st.broadcast_ws = MagicMock()
    st.push_slots_update = MagicMock()
    st.owner_id = ""
    return st


def _client(state) -> TestClient:
    return TestClient(TestServer(_make_mode_app(state)))


def _app_client(state) -> TestClient:
    return TestClient(TestServer(_make_app_token_mode_app(state)))


class _FakeOverride:
    """Recording stand-in for the SafetyOverride singleton.

    ``active`` starts True when the test wants a live global grant; a grant a
    rejected request must not have touched stays True. ``is_declared`` mirrors
    the real singleton's property — a declared grant is exempt from the
    slot-scoped narrowing, so the fake defaults to the common ad-hoc case.
    """

    def __init__(self, *, active: bool = False) -> None:
        self.active = active
        self.activate_calls: list[str] = []
        self.deactivate_calls: list[str] = []
        self.is_declared = False

    def activate(self, source: str) -> _FakeOverride:
        self.activate_calls.append(source)
        return self

    def deactivate(self, source: str) -> None:
        self.deactivate_calls.append(source)
        self.active = False

    def is_active(self) -> bool:
        return self.active


_APP_CONTROL_TARGETS = (
    ("user", True),
    ("cron", False),
    ("system", False),
    ("member", False),
    ("remote", False),
    ("cron-linked", False),
    ("channel-linked", False),
    ("other-app", False),
    ("own-app", True),
)


def _make_app_control_target(state, target: str):
    if target == "user":
        return state.get_or_create_slot("s1", origin=SlotOrigin.USER)
    if target == "cron":
        return state.get_or_create_slot("s1", origin=SlotOrigin.CRON)
    if target == "system":
        return state.get_or_create_slot("s1", origin=SlotOrigin.SYSTEM)
    if target == "member":
        return state.get_or_create_slot("s1", origin=SlotOrigin.USER, mode="member")
    if target == "remote":
        slot = state.get_or_create_slot("s1", origin=SlotOrigin.USER)
        slot.executor = "remote"
        slot.instance_id = "peer-1"
        slot.remote_slot = "remote-s1"
        return slot
    if target == "cron-linked":
        # A user-created slot that a cron injection re-bound: USER origin, but
        # its turns run on the cron session.
        slot = state.get_or_create_slot("s1", origin=SlotOrigin.USER)
        slot.linked_session_key = "cron:job-1"
        return slot
    if target == "channel-linked":
        slot = state.get_or_create_slot("s1", origin=SlotOrigin.USER)
        slot.linked_session_key = "slack:12345.678"
        return slot
    if target == "other-app":
        return state.get_or_create_slot("s1", app="other-app")
    if target == "own-app":
        return state.get_or_create_slot("s1", app="crew-keyboard")
    raise AssertionError(f"unknown target: {target}")


# ── app permission: explicit grant, live slot scope ──


@pytest.mark.parametrize(("target", "allowed"), _APP_CONTROL_TARGETS)
def test_app_send_target_boundary(state, target: str, allowed: bool) -> None:
    slot = _make_app_control_target(state, target)
    assert app_may_control_session("crew-keyboard", slot, True) is allowed


def test_app_without_grant_cannot_send_to_user_slot(state) -> None:
    slot = state.get_or_create_slot("s1", origin=SlotOrigin.USER)
    assert app_may_control_session("crew-keyboard", slot, False) is False


def test_app_cannot_send_to_another_apps_slot(state) -> None:
    """The grant never crosses into another app's session."""
    slot = state.get_or_create_slot("s1", app="other-app")
    assert app_may_control_session("crew-keyboard", slot, True) is False


def test_app_keeps_own_slot_send_without_session_grant(state) -> None:
    slot = state.get_or_create_slot("s1", app="crew-keyboard")
    assert app_may_control_session("crew-keyboard", slot, False) is True


@pytest.mark.asyncio
async def test_app_without_session_approval_grant_is_denied(state) -> None:
    state.get_or_create_slot("s1")
    with patch(
        "kiro_crew.apps.permissions.app_can_manage_session_approvals",
        return_value=False,
    ):
        async with _app_client(state) as client:
            resp = await client.post("/api/chat/mode", json={"mode": "trust", "slot": "s1"})
            assert resp.status == 403
            assert (await resp.json())["code"] == "session_approval_not_granted"
    assert state._slots["s1"]._trust is False


# ── app send to a user slot: a turn, never the slot's settings ──

_CONSENT_SHA = "a" * 64
_THEME_FIELDS = {
    "color_theme": "custom-pack",
    "theme_consent": True,
    "theme_consent_sha": _CONSENT_SHA,
}
_GRANT_CHECK = "kiro_crew.apps.permissions.app_can_manage_session_approvals"
#: The fields a send on a session the app does not own must leave as it found them.
_settings = attrgetter("agent", "color_theme", "theme_consent", "theme_consent_sha")


def _user_slot(state, *, agent: str = ""):
    slot = state.get_or_create_slot("s1", origin=SlotOrigin.USER)
    slot.agent = agent
    slot.color_theme = "custom-mine"
    slot.theme_consent = True
    slot.theme_consent_sha = "b" * 64
    return slot


def _own_slot(state):
    return state.get_or_create_slot("s1", app="crew-keyboard")


def _user_rows(slot) -> list[dict]:
    return [m for m in slot.messages if m.get("role") == "user"]


class _Sent(NamedTuple):
    status: int
    data: dict
    run_chat: AsyncMock
    title: AsyncMock
    grant_check: MagicMock


async def _app_send(state, body: dict, *, granted: bool = True) -> _Sent:
    """POST /api/chat?ws=1 as the app ``crew-keyboard``."""
    run_chat = AsyncMock()
    title = AsyncMock()
    grant_check = MagicMock(return_value=granted)
    with (
        patch(_GRANT_CHECK, grant_check),
        patch("kiro_crew.dashboard.chat_handlers._run_chat", new=run_chat),
        patch("kiro_crew.dashboard.chat_handlers._maybe_auto_title", new=title),
    ):
        async with _app_client(state) as client:
            resp = await client.post("/api/chat?ws=1", json={"message": "hi", **body})
            return _Sent(resp.status, await resp.json(), run_chat, title, grant_check)


def _assert_refused(audit: MagicMock, trigger: str) -> None:
    audit.log_api_access.assert_any_call(
        caller="crew-keyboard",
        operation="chat_send",
        outcome="denied",
        source="app_isolation",
        resources=f"slot=s1 {trigger}",
        error=ANY,
    )
    outcomes = [c.kwargs.get("outcome") for c in audit.log_api_access.call_args_list]
    assert "allowed" not in outcomes


@pytest.mark.parametrize(
    ("slot_agent", "body", "trigger"),
    [
        pytest.param("", {"agent": "researcher"}, "agent=researcher", id="agent"),
        # The configured default is a binding like any other, not a no-op.
        pytest.param("", {"agent": "default"}, "agent=default", id="default-agent"),
        pytest.param("", _THEME_FIELDS, "field=color_theme", id="theme"),
        pytest.param("", {"color_theme": ""}, "field=color_theme", id="theme-clear"),
        pytest.param("researcher", _THEME_FIELDS, "field=color_theme", id="theme-agent-bound"),
        pytest.param("", {"agent": "researcher", **_THEME_FIELDS}, "agent=researcher", id="both"),
    ],
)
@pytest.mark.asyncio
async def test_app_on_user_slot_cannot_change_its_settings(
    state, slot_agent: str, body: dict, trigger: str
) -> None:
    slot = _user_slot(state, agent=slot_agent)
    before = _settings(slot)
    audit = MagicMock()

    with patch("kiro_crew.dashboard.chat_handlers.sel", return_value=audit):
        sent = await _app_send(state, {"slot": "s1", **body})

    assert sent.status == 403
    assert sent.data["code"] == "app_session_settings_forbidden"
    sent.run_chat.assert_not_called()
    assert _settings(slot) == before
    assert _user_rows(slot) == []
    _assert_refused(audit, trigger)


@pytest.mark.parametrize(
    "body", [pytest.param({}, id="plain"), pytest.param({"agent": ""}, id="empty-agent")]
)
@pytest.mark.asyncio
async def test_app_on_agentless_user_slot_sends_a_turn(state, body: dict) -> None:
    slot = _user_slot(state)
    before = _settings(slot)

    sent = await _app_send(state, {"slot": "s1", **body})

    assert sent.status == 200, sent.data
    sent.run_chat.assert_called_once()
    assert _settings(slot) == before


@pytest.mark.parametrize(
    "body",
    [pytest.param({}, id="plain"), pytest.param({"agent": "researcher"}, id="same-agent")],
)
@pytest.mark.asyncio
async def test_app_on_agent_bound_user_slot_sends_a_turn(state, body: dict) -> None:
    slot = _user_slot(state, agent="researcher")
    before = _settings(slot)
    audit = MagicMock()

    with patch("kiro_crew.dashboard.chat_handlers.sel", return_value=audit):
        sent = await _app_send(state, {"slot": "s1", **body})

    assert sent.status == 200, sent.data
    sent.run_chat.assert_called_once()
    assert _settings(slot) == before
    audit.log_api_access.assert_any_call(
        caller="crew-keyboard",
        operation="chat_send",
        outcome="allowed",
        source="app_isolation",
        resources="permissions.sessionApproval|slot=s1",
    )


@pytest.mark.asyncio
async def test_app_on_its_own_slot_keeps_agent_and_theme_writes(state) -> None:
    slot = _own_slot(state)

    sent = await _app_send(
        state, {"slot": "s1", "agent": "researcher", **_THEME_FIELDS}, granted=False
    )

    assert sent.status == 200, sent.data
    sent.run_chat.assert_called_once()
    assert _settings(slot) == ("researcher", "custom-pack", True, _CONSENT_SHA)


#: Every request-body field ``api_chat`` reads, by what it can do to an EXISTING
#: user session the app reaches through the grant. A turn-only field shapes the
#: turn (``memory_mode`` / ``mode`` apply only to a slot the send creates, and an
#: app's send never creates a user slot). A settings-bearing field names a value
#: the slot keeps; the sample is one that would change ``_user_slot``.
_TURN_ONLY_BODY_KEYS = frozenset({"message", "slot", "meta", "steer", "memory_mode", "mode"})
_SETTINGS_BODY_KEYS: dict[str, object] = {
    "agent": "researcher",
    "color_theme": "custom-pack",
    "theme_consent": False,
    "theme_consent_sha": _CONSENT_SHA,
}


def _body_keys_read(func, name: str, functions: dict) -> set[str]:
    """Keys *func* reads off its parameter *name*, following module helpers it hands it to."""
    keys: set[str] = set()
    for node in ast.walk(func):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            target = node.func.value
            if isinstance(target, ast.Name) and target.id == name and node.func.attr == "get":
                first = node.args[0]
                assert isinstance(first, ast.Constant), ast.unparse(node)
                keys.add(first.value)
        elif isinstance(node, ast.Subscript):
            if isinstance(node.value, ast.Name) and node.value.id == name:
                assert isinstance(node.slice, ast.Constant), ast.unparse(node)
                keys.add(node.slice.value)
        elif isinstance(node, ast.Compare) and isinstance(node.left, ast.Constant):
            for op, right in zip(node.ops, node.comparators):
                if isinstance(op, (ast.In, ast.NotIn)) and isinstance(right, ast.Name):
                    if right.id == name:
                        keys.add(node.left.value)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            helper = functions.get(node.func.id)
            for index, arg in enumerate(node.args):
                if helper is not None and isinstance(arg, ast.Name) and arg.id == name:
                    keys |= _body_keys_read(helper, helper.args.args[index].arg, functions)
    return keys


def test_every_api_chat_body_key_is_classified() -> None:
    """A new body field fails here until someone says what it can change.

    Classifying it settings-bearing puts it under
    ``test_settings_bearing_body_keys_never_reach_a_user_slot``.
    """
    from kiro_crew.dashboard import chat_handlers

    tree = ast.parse(inspect.getsource(chat_handlers))
    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    read = _body_keys_read(functions["api_chat"], "body", functions)
    assert not _TURN_ONLY_BODY_KEYS & _SETTINGS_BODY_KEYS.keys()
    assert read == _TURN_ONLY_BODY_KEYS | _SETTINGS_BODY_KEYS.keys()


@pytest.mark.parametrize("key", sorted(_SETTINGS_BODY_KEYS))
@pytest.mark.asyncio
async def test_settings_bearing_body_keys_never_reach_a_user_slot(state, key: str) -> None:
    slot = _user_slot(state)
    before = _settings(slot)

    sent = await _app_send(state, {"slot": "s1", key: _SETTINGS_BODY_KEYS[key]})

    assert sent.status in (200, 403), sent.data
    assert _settings(slot) == before


# ── harness slash commands ──


@pytest.mark.parametrize("message", ["/agent researcher", "/clear", "/goal ship the release"])
@pytest.mark.asyncio
async def test_app_on_user_slot_cannot_run_a_harness_slash_command(state, message: str) -> None:
    slot = _user_slot(state, agent="researcher")
    audit = MagicMock()

    with patch("kiro_crew.dashboard.chat_handlers.sel", return_value=audit):
        sent = await _app_send(state, {"slot": "s1", "message": message})

    assert sent.status == 403
    assert sent.data["code"] == "app_session_settings_forbidden"
    sent.run_chat.assert_not_called()
    assert _user_rows(slot) == []
    _assert_refused(audit, f"command={message.split()[0]}")


@pytest.mark.asyncio
async def test_app_slash_command_is_refused_before_the_queue(state) -> None:
    slot = _user_slot(state, agent="researcher")
    busy = MagicMock()
    busy.done.return_value = False
    slot.task = busy

    sent = await _app_send(state, {"slot": "s1", "message": "/clear"})

    assert sent.status == 403
    assert slot.queue_depth == 0


@pytest.mark.parametrize("message", ["/agent researcher", "/clear", "/goal ship the release"])
@pytest.mark.asyncio
async def test_app_on_its_own_slot_runs_slash_commands(state, message: str) -> None:
    _own_slot(state)

    sent = await _app_send(state, {"slot": "s1", "message": message})

    assert sent.status == 200, sent.data
    sent.run_chat.assert_called_once()


@pytest.mark.parametrize("message", ["please /clear the board", "/tmp/notes.txt is empty"])
@pytest.mark.asyncio
async def test_app_text_that_is_not_a_harness_command_is_a_plain_send(state, message: str) -> None:
    _user_slot(state, agent="researcher")

    sent = await _app_send(state, {"slot": "s1", "message": message})

    assert sent.status == 200, sent.data
    sent.run_chat.assert_called_once()


# ── steer, row id, echo, title ──


@pytest.mark.asyncio
async def test_app_on_user_slot_cannot_steer_past_running_subagents(state) -> None:
    slot = _user_slot(state, agent="researcher")
    state.subagents = MagicMock()
    state.subagents.running_agents_for.return_value = ["child"]

    sent = await _app_send(state, {"slot": "s1", "steer": True})

    assert sent.status == 200, sent.data
    assert sent.data["queued"] is True
    sent.run_chat.assert_not_called()
    assert slot.queue_depth == 1


@pytest.mark.asyncio
async def test_app_on_its_own_slot_keeps_the_steer_opt_out(state) -> None:
    _own_slot(state)
    state.subagents = MagicMock()
    state.subagents.running_agents_for.return_value = ["child"]

    sent = await _app_send(state, {"slot": "s1", "steer": True})

    assert sent.status == 200, sent.data
    sent.run_chat.assert_called_once()


@pytest.mark.parametrize(
    "mid",
    [
        pytest.param("forged-mid", id="string"),
        pytest.param(123, id="int"),
        pytest.param(["forged"], id="list"),
    ],
)
@pytest.mark.asyncio
async def test_app_on_user_slot_cannot_choose_the_row_id(state, mid) -> None:
    slot = _user_slot(state, agent="researcher")

    sent = await _app_send(state, {"slot": "s1", "meta": {"mid": mid, "sendId": "s-1"}})

    assert sent.status == 200, sent.data
    [row] = _user_rows(slot)
    minted = row_mid(row)
    assert isinstance(minted, str) and minted and minted != mid
    assert sent.data["mid"] == minted


def _user_echoes(state) -> list[dict]:
    return [
        call.args[1]
        for call in state.broadcast_ws.call_args_list
        if call.args[0] == "chat_message" and call.args[1].get("role") == "user"
    ]


@pytest.mark.asyncio
async def test_app_turn_on_user_slot_reaches_the_users_open_tabs(state) -> None:
    _user_slot(state, agent="researcher")

    sent = await _app_send(state, {"slot": "s1", "message": "from the app"})

    assert sent.status == 200, sent.data
    assert [frame["content"] for frame in _user_echoes(state)] == ["from the app"]


@pytest.mark.asyncio
async def test_app_turn_on_its_own_slot_keeps_the_correlated_echo(state) -> None:
    _own_slot(state)

    sent = await _app_send(state, {"slot": "s1", "message": "from the app"})

    assert sent.status == 200, sent.data
    assert _user_echoes(state) == []


@pytest.mark.asyncio
async def test_app_turn_on_user_slot_starts_no_auto_title(state) -> None:
    slot = _user_slot(state, agent="researcher")

    sent = await _app_send(state, {"slot": "s1"})

    assert sent.status == 200, sent.data
    sent.title.assert_not_called()
    [row] = _user_rows(slot)
    assert row["meta"][TURN_ACTOR_META_KEY] == "app"


@pytest.mark.asyncio
async def test_app_turn_on_its_own_slot_still_titles(state) -> None:
    _own_slot(state)

    sent = await _app_send(state, {"slot": "s1"})

    assert sent.status == 200, sent.data
    sent.title.assert_called_once()


def test_title_counts_an_app_row_only_on_the_apps_own_slot(state) -> None:
    app_row = {"role": "user", "content": "x", "meta": {TURN_ACTOR_META_KEY: "app"}}
    user_row = {"role": "user", "content": "y"}
    user_slot = _user_slot(state)
    own = state.get_or_create_slot("s2", app="crew-keyboard")

    assert not _counts_as_user_turn(user_slot, app_row)
    assert _counts_as_user_turn(user_slot, user_row)
    assert _counts_as_user_turn(own, app_row)


@pytest.mark.asyncio
async def test_end_of_turn_title_ignores_an_apps_turn_on_a_user_slot(state) -> None:
    slot = _user_slot(state, agent="researcher")
    slot.append("user", "from the app", "msg msg-u", meta={TURN_ACTOR_META_KEY: "app"})
    slot.append("assistant", "reply")
    generate = AsyncMock(return_value="An App Title")

    with patch("kiro_crew.dashboard.chat_title._generate_title_via_kiro", new=generate):
        await _maybe_auto_title(state, slot)

    generate.assert_not_called()
    assert not slot._titled


@pytest.mark.parametrize(("actor", "refreshed"), [("app", False), ("", True)])
@pytest.mark.asyncio
async def test_title_refresh_counts_only_the_users_turns(state, actor: str, refreshed: bool):
    slot = _user_slot(state, agent="researcher")
    slot.title, slot._titled, slot._title_origin = "Old", True, "auto"
    slot._title_low_signal = True  # due at the first user turn
    slot.append("user", "x", "msg msg-u", meta={TURN_ACTOR_META_KEY: actor} if actor else None)
    generate = AsyncMock(return_value="")

    with (
        patch("kiro_crew.dashboard.chat_title._generate_refreshed_title", new=generate),
        patch("kiro_crew.dashboard.chat_title._title_refresh_every", return_value=0),
        patch("kiro_crew.dashboard.chat_title._persist_title", new=AsyncMock(return_value=True)),
    ):
        await maybe_refresh_title(state, slot)

    assert generate.called is refreshed


def _app_then_user_turns(slot) -> None:
    slot.append("user", "Summarize ticket ABC-1", "msg msg-u", meta={TURN_ACTOR_META_KEY: "app"})
    slot.append("assistant", "app reply")
    slot.append("user", "ok", "msg msg-u")
    slot.append("assistant", "sure")


def _prompt_lines(generate: AsyncMock) -> list[str]:
    return [m["content"] for m in generate.call_args.args[1]]


@pytest.mark.asyncio
async def test_the_title_fallback_never_takes_an_apps_text(state) -> None:
    slot = _user_slot(state, agent="researcher")
    _app_then_user_turns(slot)
    generate = AsyncMock(return_value="")  # the titler SKIPs

    with (
        patch("kiro_crew.dashboard.chat_title._generate_title_via_kiro", new=generate),
        patch("kiro_crew.dashboard.chat_title._persist_title", new=AsyncMock(return_value=True)),
        patch("kiro_crew.dashboard.chat_title.maybe_suggest_folder", new=AsyncMock()),
    ):
        await _maybe_auto_title(state, slot)

    assert _prompt_lines(generate) == ["ok", "sure"]
    assert slot.title == "ok"
    assert slot._titled


@pytest.mark.asyncio
async def test_the_reply_to_an_apps_turn_is_not_the_users_answered_turn(state) -> None:
    # The user's first turn has no reply yet, so a SKIP must leave the fallback
    # unlocked for the end-of-turn retry; the app turn's reply is not an answer.
    slot = _user_slot(state, agent="researcher")
    slot.append("user", "Reply with exactly: PLAN", "msg msg-u", meta={TURN_ACTOR_META_KEY: "app"})
    slot.append("assistant", "PLAN")
    slot.append("user", "hi", "msg msg-u")
    generate = AsyncMock(return_value="")

    with (
        patch("kiro_crew.dashboard.chat_title._generate_title_via_kiro", new=generate),
        patch("kiro_crew.dashboard.chat_title._persist_title", new=AsyncMock(return_value=True)),
        patch("kiro_crew.dashboard.chat_title.maybe_suggest_folder", new=AsyncMock()),
    ):
        await _maybe_auto_title(state, slot)

    assert _prompt_lines(generate) == ["hi"]
    assert slot.title == "hi"
    assert not slot._titled


@pytest.mark.asyncio
async def test_the_title_refresh_prompt_leaves_out_an_apps_turn(state) -> None:
    slot = _user_slot(state, agent="researcher")
    slot.title, slot._titled, slot._title_origin = "Old", True, "auto"
    slot._title_low_signal = True
    _app_then_user_turns(slot)
    generate = AsyncMock(return_value="")

    with (
        patch("kiro_crew.dashboard.chat_title._generate_refreshed_title", new=generate),
        patch("kiro_crew.dashboard.chat_title._title_refresh_every", return_value=0),
        patch("kiro_crew.dashboard.chat_title._persist_title", new=AsyncMock(return_value=True)),
    ):
        await maybe_refresh_title(state, slot)

    assert _prompt_lines(generate) == ["ok", "sure"]


def test_a_merge_run_stops_where_the_turn_actor_changes(state) -> None:
    slot = _user_slot(state)
    queue_for_next_turn(state, slot, "ping", turn_actor="app")
    slot.queue_append("Fix the login bug")
    slot.queue_append("and the logout one")

    first, first_items = _dequeue_next_message(slot, merge_enabled=True)
    second, second_items = _dequeue_next_message(slot, merge_enabled=True)

    assert (first, len(first_items)) == ("ping", 1)
    assert second == "[2 queued messages merged]\n\nFix the login bug\n\nand the logout one"
    assert len(second_items) == 2


def test_a_rehydrated_refresh_mark_counts_turns_like_the_refresh(state) -> None:
    slot = _user_slot(state, agent="researcher")
    for n in range(3):
        slot.append("user", f"mine {n}", "msg msg-u")
        slot.append("user", f"app {n}", "msg msg-u", meta={TURN_ACTOR_META_KEY: "app"})
    slot._title_refresh_mark = 6

    chat_persistence._rebase_rehydrated_refresh_mark(slot)

    assert slot._title_refresh_mark == _rehydrated_refresh_mark(6, 3) < 6


# ── the grant re-checks the slot after its awaits ──


@pytest.mark.parametrize("closes", ["begin-close", "removed"])
@pytest.mark.asyncio
async def test_app_send_refused_when_the_slot_closes_during_the_permission_read(
    state, closes: str
) -> None:
    slot = _user_slot(state, agent="researcher")
    reading = threading.Event()
    release = threading.Event()

    def gated_grant(_app: str) -> bool:
        reading.set()
        assert release.wait(5)
        return True

    run_chat = AsyncMock()
    with (
        patch(_GRANT_CHECK, gated_grant),
        patch("kiro_crew.dashboard.chat_handlers._run_chat", new=run_chat),
        patch("kiro_crew.dashboard.chat_handlers._maybe_auto_title", new=AsyncMock()),
    ):
        async with _app_client(state) as client:
            send = asyncio.create_task(
                client.post("/api/chat?ws=1", json={"slot": "s1", "message": "hi"})
            )
            assert await asyncio.to_thread(reading.wait, 5)
            if closes == "begin-close":
                slot.begin_close()
            else:
                state._slots.pop("s1")
            release.set()
            resp = await send
            data = await resp.json()

    assert _user_rows(slot) == []
    if closes == "begin-close":
        assert resp.status == 404
        assert data["code"] == "slot_not_found"
        run_chat.assert_not_called()
    else:
        # A slot removed before the lookup leaves a free name with no
        # transcript; the app may only open its own session under it.
        current = state._slots.get("s1")
        assert current is not slot
        assert current is None or current._app == "crew-keyboard"


@pytest.mark.asyncio
async def test_app_send_refused_when_the_slot_is_relinked_during_the_command_read(state) -> None:
    slot = _user_slot(state, agent="researcher")
    reading = threading.Event()
    release = threading.Event()
    real_load = KiroCrewConfig.load
    loads: list[None] = []

    def gated_load(*args, **kwargs):
        # Only the first read, `_send_harness_command`'s, is held.
        loads.append(None)
        if len(loads) == 1:
            reading.set()
            assert release.wait(5)
        return real_load(*args, **kwargs)

    run_chat = AsyncMock()
    audit = MagicMock()
    with (
        patch(_GRANT_CHECK, return_value=True),
        patch.object(KiroCrewConfig, "load", staticmethod(gated_load)),
        patch("kiro_crew.dashboard.chat_handlers._run_chat", new=run_chat),
        patch("kiro_crew.dashboard.chat_handlers._maybe_auto_title", new=AsyncMock()),
        patch("kiro_crew.dashboard.chat_handlers.sel", return_value=audit),
    ):
        async with _app_client(state) as client:
            send = asyncio.create_task(
                client.post("/api/chat?ws=1", json={"slot": "s1", "message": "/tmp/a read it"})
            )
            assert await asyncio.to_thread(reading.wait, 5)
            slot.linked_session_key = "cron:job-1"
            release.set()
            resp = await send
            data = await resp.json()

    assert resp.status == 404
    assert data["code"] == "slot_not_found"
    run_chat.assert_not_called()
    assert _user_rows(slot) == []
    outcomes = [c.kwargs.get("outcome") for c in audit.log_api_access.call_args_list]
    assert "allowed" not in outcomes


# ── an app's SSE stream on a user slot is its own turn ──


@pytest.mark.asyncio
async def test_app_sse_stream_stops_at_the_end_of_its_turn(state) -> None:
    slot = _user_slot(state, agent="researcher")
    state._broadcast = MagicMock()

    async def turn_then_queued_follow_up(_state, s, _message, **_kwargs):
        s.append("assistant", "reply to the app")
        s.push_wire_frame(TURN_END_WIRE_CLS, "")
        # What the queue drain does next: the user's follow-up and its reply.
        s.append("user", "the user's follow-up", "msg msg-u")
        s.append("assistant", "reply to the user")
        s.append("done", "", "done")

    with (
        patch(_GRANT_CHECK, return_value=True),
        patch("kiro_crew.dashboard.chat_handlers._run_chat", new=turn_then_queued_follow_up),
        patch("kiro_crew.dashboard.chat_handlers._maybe_auto_title", new=AsyncMock()),
    ):
        async with _app_client(state) as client:
            resp = await client.post("/api/chat", json={"slot": "s1", "message": "hi"})
            stream = await resp.text()

    assert resp.status == 200
    assert "reply to the app" in stream
    assert "follow-up" not in stream and "reply to the user" not in stream
    assert stream.rstrip().endswith("data: [DONE]")
    assert slot._has_reader is False
    dashboard = [
        call.args[0]["content"]
        for call in state._broadcast.call_args_list
        if call.args[0].get("_type") == "chat_message"
    ]
    assert dashboard == ["reply to the app", "reply to the user"]


@pytest.mark.asyncio
async def test_app_sse_stream_skips_a_row_queued_for_a_later_turn(state) -> None:
    _user_slot(state, agent="researcher")
    state._broadcast = MagicMock()
    cron = f'{CRON_NOTIFY_PREFIX}"job"]\nthe user\'s cron output\n[/Cron notification]'

    async def turn_with_a_cron_queued_meanwhile(_state, s, _message, **_kwargs):
        s.append("assistant", "reply to the app")
        # What a cron notifying a running slot does (handlers/messaging.py).
        qid = s.queue_append(cron, kind=CRON_NOTIFICATION_KIND)
        s.append("queued", cron, json.dumps({"cronLabel": "job", "queue_id": qid}))
        s.append("assistant", "more of the app's turn")
        s.append("done", "", "done")

    with (
        patch(_GRANT_CHECK, return_value=True),
        patch("kiro_crew.dashboard.chat_handlers._run_chat", new=turn_with_a_cron_queued_meanwhile),
        patch("kiro_crew.dashboard.chat_handlers._maybe_auto_title", new=AsyncMock()),
    ):
        async with _app_client(state) as client:
            resp = await client.post("/api/chat", json={"slot": "s1", "message": "hi"})
            stream = await resp.text()

    assert "reply to the app" in stream and "more of the app's turn" in stream
    assert "cron output" not in stream
    dashboard = [
        call.args[0]["content"]
        for call in state._broadcast.call_args_list
        if call.args[0].get("_type") == "chat_message"
    ]
    assert cron in dashboard


@pytest.mark.asyncio
async def test_a_slot_owners_sse_stream_skips_the_turn_boundary(state) -> None:
    _own_slot(state)

    async def two_turns(_state, s, _message, **_kwargs):
        s.append("assistant", "first reply")
        s.push_wire_frame(TURN_END_WIRE_CLS, "")
        s.append("assistant", "second reply")
        s.append("done", "", "done")

    with (
        patch("kiro_crew.dashboard.chat_handlers._run_chat", new=two_turns),
        patch("kiro_crew.dashboard.chat_handlers._maybe_auto_title", new=AsyncMock()),
    ):
        async with _app_client(state) as client:
            resp = await client.post("/api/chat", json={"slot": "s1", "message": "hi"})
            stream = await resp.text()

    assert "first reply" in stream and "second reply" in stream
    assert TURN_END_WIRE_CLS not in stream


class _PrepareFails(web.StreamResponse):
    async def prepare(self, request):
        raise ConnectionResetError


@pytest.mark.asyncio
async def test_a_failed_sse_prepare_releases_the_reader(state) -> None:
    slot = _own_slot(state)

    with (
        patch.object(web, "StreamResponse", _PrepareFails),
        patch("kiro_crew.dashboard.chat_handlers._run_chat", new=AsyncMock()),
        patch("kiro_crew.dashboard.chat_handlers._maybe_auto_title", new=AsyncMock()),
    ):
        async with _app_client(state) as client:
            with contextlib.suppress(aiohttp.ClientError):
                await client.post("/api/chat", json={"slot": "s1", "message": "hi"})

    assert slot._has_reader is False


def _scripted_turns(state, monkeypatch, *turns) -> None:
    """Drive the real ``_run_chat`` with one scripted provider stream per turn."""

    async def _events(items):
        for item in items:
            yield item

    monkeypatch.setattr(session_control, "session_control_enabled", lambda: True)
    monkeypatch.setattr(session_control, "sel", lambda: MagicMock())
    monkeypatch.setattr(session_control, "_sel_off_loop", lambda write, what: write())
    monkeypatch.setattr(chat_runner, "title_then_refresh", AsyncMock())
    monkeypatch.setattr(chat_runner, "generate_session_summary", AsyncMock())
    state.subagents = None
    state.sessions.get_or_create = AsyncMock(return_value=(MagicMock(), False, False))
    state.sessions.release = MagicMock()
    state.sessions.reset = AsyncMock()
    state.sessions.set_approval_policy = MagicMock()
    state.sessions.check_context_usage = MagicMock()
    state.sessions.get_slack_link = MagicMock(return_value=(None, None))
    state.sessions.record_failure = AsyncMock()
    state.is_yolo_active = MagicMock(return_value=False)
    client = state.sessions.get_or_create.return_value[0]
    client.shutdown = AsyncMock()
    client.context_usage_pct = MagicMock(return_value=0.0)
    client._client = client
    client.last_prompt_stats = None
    # A turn is its reply text; "" is a response that carries none.
    scripts = iter(
        [
            *([LLMEvent(kind=EVENT_TEXT_CHUNK, text=text)] if text else []),
            LLMEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
        ]
        for text in turns
    )
    client.stream = MagicMock(side_effect=lambda *_a, **_k: _events(next(scripts)))
    client.stream_command = client.stream


async def _run_app_turn_and_successors(state, slot) -> list[tuple[str, str]]:
    await chat_runner._run_chat(state, slot, "from the app", _turn_actor="app")
    for _ in range(5):
        successor = slot.task
        if successor is None or successor.done():
            break
        await asyncio.wait_for(successor, timeout=10)
    assert slot.task is None or slot.task.done()
    return [(m["role"], m["content"]) for m in slot.drain()]


@pytest.mark.asyncio
async def test_the_drain_marks_the_turn_end_before_a_successor_turn(state, monkeypatch) -> None:
    _scripted_turns(state, monkeypatch, "reply to the app", "reply to the user")
    slot = _user_slot(state)
    slot._titled = True
    slot.queue_append("the user's follow-up", directive_user_origin=True)

    pending = await _run_app_turn_and_successors(state, slot)

    boundary = pending.index((TURN_END_WIRE_CLS, ""))
    assert any("reply to the app" in content for _, content in pending[:boundary])
    assert ("user", "the user's follow-up") in pending[boundary:]
    assert all("reply to the user" not in content for _, content in pending[:boundary])


@pytest.mark.asyncio
async def test_an_empty_first_reply_is_retried_inside_the_apps_turn(state, monkeypatch) -> None:
    # The first response is empty, so the runner re-queues the app's message
    # (`_queue_recovery`); that retry is still the app's turn.
    _scripted_turns(state, monkeypatch, "", "the real reply", "reply to the user")
    slot = _user_slot(state)
    slot._titled = True
    slot.queue_append("the user's follow-up", directive_user_origin=True)

    pending = await _run_app_turn_and_successors(state, slot)

    # One where the app's turn ends, one where the user's follow-up ends the cycle.
    assert pending.count((TURN_END_WIRE_CLS, "")) == 2
    boundary = pending.index((TURN_END_WIRE_CLS, ""))
    assert any("the real reply" in content for _, content in pending[:boundary])
    assert ("user", "the user's follow-up") in pending[boundary:]


@pytest.mark.parametrize(
    ("stamped", "predecessor", "recovery_id", "ending_id", "marked"),
    [
        pytest.param("app", "app", "turn-a", "turn-a", False, id="own-retry"),
        pytest.param("user", "app", "turn-a", "turn-a", True, id="another-actor"),
        pytest.param("app", "", "turn-a", "turn-a", True, id="no-ending-actor"),
        pytest.param("app", "app", "turn-a", "turn-b", True, id="older-app-retry"),
        pytest.param("app", "app", "", "turn-a", True, id="missing-recovery-identity"),
        pytest.param("app", "app", "turn-a", "", True, id="missing-ending-identity"),
    ],
)
@pytest.mark.asyncio
async def test_only_the_ending_turns_own_recovery_continues_its_turn(
    state, stamped: str, predecessor: str, recovery_id: str, ending_id: str, marked: bool
) -> None:
    slot = _user_slot(state)
    meta = {TURN_ACTOR_META_KEY: stamped}
    if recovery_id:
        meta[chat_runner._RECOVERY_TURN_META_KEY] = recovery_id
    slot.queue_insert(0, "retry", kind=SYNTHETIC_RECOVERY_KIND, meta=meta)

    await _drain_once(state, slot, predecessor_actor=predecessor, predecessor_turn_id=ending_id)

    assert ((TURN_END_WIRE_CLS, "") in [(m["cls"], m["content"]) for m in slot.drain()]) is marked


def _held_note(slot, text: str) -> None:
    slot._deferred_notes.append(
        {"id": "n1", "content": text, "cls": "reconcile-note", "context": None, "session": None}
    )


@pytest.mark.asyncio
async def test_a_held_note_and_a_queued_follow_up_land_after_the_turn_end(state) -> None:
    slot = _user_slot(state)
    slot.drain()
    _held_note(slot, "a note for the user")
    slot.queue_append("the user's follow-up", directive_user_origin=True)

    await _drain_once(state, slot, predecessor_actor="app")

    pending = [(m["cls"], m["content"]) for m in slot.drain()]
    boundary = pending.index((TURN_END_WIRE_CLS, ""))
    assert boundary == 0
    assert ("reconcile-note", "a note for the user") in pending[boundary:]
    assert any(content == "the user's follow-up" for _, content in pending[boundary:])


@pytest.mark.asyncio
async def test_the_cycle_end_marks_the_turn_end_before_a_held_note(state) -> None:
    slot = _user_slot(state)
    slot.drain()
    _held_note(slot, "a note for the user")

    await chat_runner._finish_queue_cycle(state, slot)

    assert [(m["cls"], m["content"]) for m in slot.drain()] == [
        (TURN_END_WIRE_CLS, ""),
        ("reconcile-note", "a note for the user"),
        ("done", ""),
    ]


@pytest.mark.asyncio
async def test_a_synthesis_successor_is_a_new_turn(state) -> None:
    slot = _user_slot(state)
    slot._pending_synthesis = True
    # Every fire-gate probe set: a bare MagicMock answers an unset one with a
    # truthy mock, which reads as children still waiting.
    state.subagents = MagicMock(
        running_agents_for=MagicMock(return_value=[]),
        has_in_memory_pending_work_for=MagicMock(return_value=False),
        queued_count_for_async=AsyncMock(return_value=0),
        queued_count_or_none_async=AsyncMock(return_value=0),
    )

    with patch.object(chat_runner, "_run_pending_synthesis", new=AsyncMock()):
        await chat_runner._finish_queue_cycle(state, slot)
        await asyncio.wait_for(slot.task, timeout=10)

    assert [m["cls"] for m in slot.drain()] == [TURN_END_WIRE_CLS]


# ── a queued app entry is never a cron or sub-agent event ──

_CRON_LOOKALIKE = '[Cron notification from "Nightly backup"] all done'


def _spawn_closing_the_turn(_state, _slot, turn, *_args, **_kwargs) -> MagicMock:
    """A ``spawn_guarded_turn`` stand-in that closes the turn it is never going to run."""
    if inspect.iscoroutine(turn):
        turn.close()
    return MagicMock()


async def _drain_once(state, slot, **kwargs) -> dict:
    with (
        patch.object(chat_runner, "spawn_guarded_turn", side_effect=_spawn_closing_the_turn),
        patch.object(chat_runner, "_run_chat", new=MagicMock()),
    ):
        assert await chat_runner._start_next_queued_turn(state, slot, **kwargs) is True
    return slot.messages[-1]


@pytest.mark.asyncio
async def test_a_queued_mcp_app_message_is_an_app_inject_whatever_its_text(state) -> None:
    slot = state.get_or_create_slot("s1", origin=SlotOrigin.USER)
    slot.queue_append(_CRON_LOOKALIKE, kind=MCP_APP_MESSAGE_KIND, meta={"appLabel": "Keys"})

    row = await _drain_once(state, slot)

    assert row["role"] == "inject"
    assert row["meta"]["injectKind"] == "mcp_app"
    assert row["meta"]["appLabel"] == "Keys"
    assert "cronLabel" not in row["meta"]
    assert "cronLabel" not in row["cls"]


@pytest.mark.asyncio
async def test_a_queued_app_send_is_never_a_cron_notification(state) -> None:
    slot = state.get_or_create_slot("s1", origin=SlotOrigin.USER)
    queue_for_next_turn(state, slot, _CRON_LOOKALIKE, turn_actor="app")

    row = await _drain_once(state, slot)

    # The row an immediate app send writes: a user row, not a cron inject.
    assert row["role"] == "user"
    assert "injectKind" not in row["meta"]
    assert "cronLabel" not in row["meta"]
    assert "cronLabel" not in row["cls"]


@pytest.mark.asyncio
async def test_a_restored_queue_entry_is_never_a_cron_notification(state) -> None:
    # A restart drops the actor stamp, so a restored app send keeps only its text.
    live = state.get_or_create_slot("s0", origin=SlotOrigin.USER)
    queue_for_next_turn(state, live, _CRON_LOOKALIKE, turn_actor="app")
    slot = state.get_or_create_slot("s1", origin=SlotOrigin.USER)
    slot._queue = sanitize_restored_queue(durable_queue_entries(live._queue))
    assert slot._queue, "a plain queued send must survive the restart"

    row = await _drain_once(state, slot)

    assert row["role"] == "user"
    assert "injectKind" not in (row.get("meta") or {})
    assert "cronLabel" not in row["cls"]


@pytest.mark.parametrize(
    ("mode", "expected_trust", "expected_trust_reads"),
    [
        ("normal", False, False),
        ("trust_reads", False, True),
        ("trust", True, False),
    ],
)
@pytest.mark.asyncio
async def test_app_with_grant_can_set_user_slot_mode(
    state,
    mode: str,
    expected_trust: bool,
    expected_trust_reads: bool,
) -> None:
    slot = state.get_or_create_slot("s1", origin=SlotOrigin.USER)
    if mode == "normal":
        slot._trust = True
        slot._trust_reads = True
    with patch(
        "kiro_crew.apps.permissions.app_can_manage_session_approvals",
        return_value=True,
    ):
        async with _app_client(state) as client:
            resp = await client.post("/api/chat/mode", json={"mode": mode, "slot": "s1"})
            assert resp.status == 200
    assert slot._trust is expected_trust
    assert slot._trust_reads is expected_trust_reads


@pytest.mark.parametrize(("target", "allowed"), _APP_CONTROL_TARGETS)
@pytest.mark.asyncio
async def test_app_non_yolo_mode_target_boundary(state, target: str, allowed: bool) -> None:
    slot = _make_app_control_target(state, target)
    with patch(
        "kiro_crew.apps.permissions.app_can_manage_session_approvals",
        return_value=True,
    ):
        async with _app_client(state) as client:
            resp = await client.post("/api/chat/mode", json={"mode": "trust", "slot": "s1"})
            assert resp.status == (200 if allowed else 404)
    assert slot._trust is allowed


@pytest.mark.asyncio
async def test_app_cannot_arm_global_yolo_even_with_grant(state) -> None:
    state.get_or_create_slot("s1", origin=SlotOrigin.USER)
    override = _FakeOverride()
    with (
        patch(
            "kiro_crew.apps.permissions.app_can_manage_session_approvals",
            return_value=True,
        ),
        patch("kiro_crew.dashboard.chat_handlers.safety_override", return_value=override),
    ):
        async with _app_client(state) as client:
            resp = await client.post("/api/chat/mode", json={"mode": "yolo", "slot": "s1"})
            body = await resp.json()
    assert resp.status == 403
    assert body["code"] == "app_yolo_forbidden"
    assert override.activate_calls == []


@pytest.mark.asyncio
async def test_app_normal_does_not_revoke_global_yolo(state) -> None:
    # The override is process-global; an app's per-slot ``normal`` must not end
    # the operator's YOLO grant on every other session.
    slot = state.get_or_create_slot("s1", origin=SlotOrigin.USER)
    slot._trust = True
    override = _FakeOverride()
    override.active = True
    with (
        patch(
            "kiro_crew.apps.permissions.app_can_manage_session_approvals",
            return_value=True,
        ),
        patch("kiro_crew.dashboard.chat_handlers.safety_override", return_value=override),
    ):
        async with _app_client(state) as client:
            resp = await client.post("/api/chat/mode", json={"mode": "normal", "slot": "s1"})
    assert resp.status == 200
    assert slot._trust is False
    assert override.deactivate_calls == []
    assert override.active is True


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [{"mode": "trust"}, {"mode": "trust", "slot": ""}])
async def test_app_mode_change_requires_explicit_slot(state, body: dict) -> None:
    # A missing slot and an EMPTY slot both normalize to the all-slots path, so
    # both must be refused for an app caller -- ``""`` once slipped past an
    # ``is None`` check and trusted every session.
    state.get_or_create_slot("s1")
    state.get_or_create_slot("s2")
    audit = MagicMock()
    with (
        patch(
            "kiro_crew.apps.permissions.app_can_manage_session_approvals",
            return_value=True,
        ),
        patch("kiro_crew.dashboard.chat_handlers.sel", return_value=audit),
    ):
        async with _app_client(state) as client:
            resp = await client.post("/api/chat/mode", json=body)
            assert resp.status == 400
            assert (await resp.json())["code"] == "slot_required"
    audit.log_api_access.assert_any_call(
        caller="crew-keyboard",
        operation="chat_mode",
        outcome="allowed",
        source="app_isolation",
        resources="permissions.sessionApproval",
    )
    assert state._slots["s1"]._trust is False
    assert state._slots["s2"]._trust is False


@pytest.mark.asyncio
async def test_app_with_grant_can_resolve_user_slot_approval(state) -> None:
    slot = state.get_or_create_slot("s1", origin=SlotOrigin.USER)
    future = asyncio.get_running_loop().create_future()
    slot._approval_futures["req-1"] = future
    audit = MagicMock()
    with (
        patch(
            "kiro_crew.apps.permissions.app_can_manage_session_approvals",
            return_value=True,
        ),
        patch("kiro_crew.dashboard.chat_handlers.sel", return_value=audit),
    ):
        async with _app_client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/approve",
                json={"action": "approved", "request_id": "req-1"},
            )
            assert resp.status == 200
    assert future.result() == "approved"
    assert audit.log_api_access.call_args.kwargs["caller"] == "app:crew-keyboard"


@pytest.mark.parametrize(("target", "allowed"), _APP_CONTROL_TARGETS)
@pytest.mark.parametrize("action", ["approved", "rejected"])
@pytest.mark.asyncio
async def test_app_approval_target_boundary(state, target: str, allowed: bool, action: str) -> None:
    slot = _make_app_control_target(state, target)
    future = asyncio.get_running_loop().create_future()
    slot._approval_futures["req-1"] = future
    with patch(
        "kiro_crew.apps.permissions.app_can_manage_session_approvals",
        return_value=True,
    ):
        async with _app_client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/approve",
                json={"action": action, "request_id": "req-1"},
            )
            assert resp.status == (200 if allowed else 404)
    if allowed:
        assert future.result() == action
    else:
        assert future.done() is False


@pytest.mark.parametrize("target", [t for t, _allowed in _APP_CONTROL_TARGETS])
@pytest.mark.parametrize("action", ["approved", "rejected"])
@pytest.mark.asyncio
async def test_app_never_resolves_state_level_approvals(state, target: str, action: str) -> None:
    # State-level approvals are raised by background sources (cron, autonudge,
    # subagent, taskrunner) and only parked in a user's tab. The grant reaches
    # the user's own session -- whose prompts live on the slot future -- so an
    # app token gets 404 here whatever slot the approval is attributed to.
    state.get_or_create_slot("addressed", origin=SlotOrigin.USER)
    _make_app_control_target(state, target)
    future = asyncio.get_running_loop().create_future()
    state._approval_futures["req-state"] = future
    state._pending_approvals["req-state"] = {"id": "req-state", "slot": "s1", "source": "subagent"}
    with patch(
        "kiro_crew.apps.permissions.app_can_manage_session_approvals",
        return_value=True,
    ):
        async with _app_client(state) as client:
            resp = await client.post(
                "/api/chat/slots/addressed/approve",
                json={"action": action, "request_id": "req-state"},
            )
            body = await resp.json()
    assert resp.status == 404
    assert body["code"] == "slot_not_found"
    assert future.done() is False


@pytest.mark.parametrize("action", ["approved", "rejected", "rejected_once"])
@pytest.mark.asyncio
async def test_dashboard_still_resolves_state_level_approvals(state, action: str) -> None:
    # The dashboard owner keeps the pre-existing fallback: a parked background
    # approval is theirs to answer from the tab it appears in.
    state.get_or_create_slot("addressed", origin=SlotOrigin.USER)
    future = asyncio.get_running_loop().create_future()
    state._approval_futures["req-state"] = future
    state._pending_approvals["req-state"] = {"id": "req-state", "slot": "s1", "source": "cron"}

    @web.middleware
    async def _dashboard_request(request: web.Request, handler):
        request["app"] = ""
        request["user"] = "local-app"
        return await handler(request)

    app = web.Application(middlewares=[_dashboard_request])
    app["state"] = state
    app.router.add_post("/api/chat/slots/{slot}/approve", api_chat_slot_approve)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/api/chat/slots/addressed/approve",
            json={"action": action, "request_id": "req-state"},
        )
    assert resp.status == 200
    assert future.result() is (action == "approved")


@pytest.mark.parametrize("action", ["approved", "rejected", "rejected_once"])
@pytest.mark.asyncio
async def test_dashboard_decision_targets_slot_and_request_with_colliding_ids(
    state, action
) -> None:
    other = state.get_or_create_slot("other", origin=SlotOrigin.USER)
    selected = state.get_or_create_slot("selected", origin=SlotOrigin.USER)
    loop = asyncio.get_running_loop()
    other_future = loop.create_future()
    selected_future = loop.create_future()
    sibling_future = loop.create_future()
    other._approval_futures["same-id"] = other_future
    selected._approval_futures.update({"same-id": selected_future, "sibling": sibling_future})
    app = _make_mode_app(state)
    app.router.add_post("/api/chat/slots/{slot}/approve", api_chat_slot_approve)
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/api/chat/slots/selected/approve",
            json={"action": action, "request_id": "same-id"},
        )
        assert response.status == 200
        assert selected_future.result() == action
        assert not other_future.done()
        assert not sibling_future.done()
        state.broadcast_ws.assert_any_call(
            "approval_resolved",
            {"id": "same-id", "approved": action == "approved", "slot": "selected"},
        )
        # An expired request cannot fall through to the colliding other session.
        response = await client.post(
            "/api/chat/slots/selected/approve",
            json={"action": action, "request_id": "same-id"},
        )
        assert response.status == 404
        assert not other_future.done()
        assert not sibling_future.done()


@pytest.mark.parametrize("replacement_slot", [False, True])
@pytest.mark.asyncio
async def test_dashboard_native_stale_row_cannot_resolve_reused_id(state, replacement_slot):
    selected = state.get_or_create_slot("selected", origin=SlotOrigin.USER)
    old_row = selected.append("permission", "Old tool", json.dumps({"request_id": "reused"}))
    old = asyncio.get_running_loop().create_future()
    selected.register_approval("reused", old, old_row)
    old.set_result("rejected")
    if replacement_slot:
        state._slots.pop("selected")
        selected = state.get_or_create_slot("selected", origin=SlotOrigin.USER)
    current_row = selected.append(
        "permission", "Replacement tool", json.dumps({"request_id": "reused"})
    )
    current = asyncio.get_running_loop().create_future()
    selected.register_approval("reused", current, current_row)
    assert row_mid(old_row) != row_mid(current_row)
    app = _make_mode_app(state)
    app.router.add_post("/api/chat/slots/{slot}/approve", api_chat_slot_approve)
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/api/chat/slots/selected/approve",
            json={
                "action": "approved",
                "request_id": "reused",
                "origin": "native",
                "request_mid": row_mid(old_row),
            },
        )
    assert response.status == 404
    assert not current.done()
    assert not selected._trust and not selected._trust_reads


@pytest.mark.parametrize("action", ["approve", "reject", "reject_once"])
@pytest.mark.parametrize(
    "pending", ["live", "missing", "done", "record_missing", "wrong_slot", "wrong_instance"]
)
@pytest.mark.asyncio
async def test_dashboard_strict_coordinator_never_falls_into_native(state, action, pending):
    loop = asyncio.get_running_loop()
    selected = state.get_or_create_slot("selected", origin=SlotOrigin.USER)
    other = state.get_or_create_slot("other", origin=SlotOrigin.USER)
    native = [loop.create_future(), loop.create_future()]
    selected._approval_futures["same-id"] = native[0]
    other._approval_futures["same-id"] = native[1]
    coordinator = loop.create_future()
    if pending != "missing":
        state._approval_futures["same-id"] = coordinator
    if pending == "done":
        coordinator.set_result(False)
    if pending != "record_missing":
        state._pending_approvals["same-id"] = {
            "id": "same-id",
            "slot": "other" if pending == "wrong_slot" else "dashboard:selected",
            # The record a stale card was rendered from carries another instance:
            # the caller reused the request id, and this is the replacement.
            "instance": "replacement" if pending == "wrong_instance" else "shown",
        }
    app = _make_mode_app(state)
    app.router.add_post("/api/approvals/{id}/{action}", api_approval_resolve)
    target = {"origin": "coordinator", "slot": "dashboard:selected", "instance": "shown"}
    async with TestClient(TestServer(app)) as client:
        response = await client.post(f"/api/approvals/same-id/{action}", params=target, json={})
        assert response.status == (200 if pending == "live" else 404)
        if pending == "live":
            assert coordinator.result() is (action == "approve")
            # A repeated click after resolution is stale even before record cleanup.
            response = await client.post(f"/api/approvals/same-id/{action}", params=target, json={})
            assert response.status == 404
        elif pending != "done":
            assert not coordinator.done()
        assert all(not future.done() for future in native)


@pytest.mark.parametrize("action", ["approve", "reject"])
@pytest.mark.asyncio
async def test_dashboard_coordinator_target_without_an_instance_is_refused(state, action):
    """A coordinator target names the instance it saw, as a native target names its
    row; one that names none is malformed and mutates nothing, even for a live
    record whose id and slot it does name."""
    coordinator = asyncio.get_running_loop().create_future()
    state._approval_futures["same-id"] = coordinator
    state._pending_approvals["same-id"] = {
        "id": "same-id",
        "slot": "dashboard:selected",
        "instance": "shown",
    }
    app = _make_mode_app(state)
    app.router.add_post("/api/approvals/{id}/{action}", api_approval_resolve)
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            f"/api/approvals/same-id/{action}",
            params={"origin": "coordinator", "slot": "dashboard:selected"},
            json={},
        )
    assert response.status == 400
    assert not coordinator.done()


@pytest.mark.parametrize("action", ["approve", "reject"])
@pytest.mark.asyncio
async def test_a_slotless_coordinator_target_settles_only_the_instance_it_names(state, action):
    """An approval with no owning slot (a cron, autonudge or task-runner request)
    decides with an empty slot and its instance. A card left up from an earlier
    request under the recurring id names that request's instance and settles
    nothing; the card for the live request settles it; a target that omits the
    slot key altogether is malformed. Negative control: an empty slot never
    matches a record that has one."""
    coordinator = asyncio.get_running_loop().create_future()
    state._approval_futures["cron-id"] = coordinator
    state._pending_approvals["cron-id"] = {"id": "cron-id", "slot": "", "instance": "live"}
    slotted = asyncio.get_running_loop().create_future()
    state._approval_futures["slotted-id"] = slotted
    state._pending_approvals["slotted-id"] = {
        "id": "slotted-id",
        "slot": "dashboard:selected",
        "instance": "shown",
    }
    app = _make_mode_app(state)
    app.router.add_post("/api/approvals/{id}/{action}", api_approval_resolve)
    async with TestClient(TestServer(app)) as client:
        stale = {"origin": "coordinator", "slot": "", "instance": "earlier"}
        response = await client.post(f"/api/approvals/cron-id/{action}", params=stale, json={})
        assert response.status == 404
        assert not coordinator.done()

        no_slot_key = {"origin": "coordinator", "instance": "live"}
        response = await client.post(
            f"/api/approvals/cron-id/{action}", params=no_slot_key, json={}
        )
        assert response.status == 400
        assert not coordinator.done()

        unslotted = {"origin": "coordinator", "slot": "", "instance": "shown"}
        response = await client.post(
            f"/api/approvals/slotted-id/{action}", params=unslotted, json={}
        )
        assert response.status == 404
        assert not slotted.done()

        live = {"origin": "coordinator", "slot": "", "instance": "live"}
        response = await client.post(f"/api/approvals/cron-id/{action}", params=live, json={})
        assert response.status == 200
        assert coordinator.result() is (action == "approve")


def test_a_coordinator_resolution_names_the_instance_it_resolved():
    """Both coordinator exits -- a decision and an expiry -- broadcast the
    instance of the record they resolved, so a client can tell it from an
    earlier request's row under the same recurring id."""
    from kiro_crew.dashboard.interaction_coordinator import ApprovalCoordinator

    class _State:
        _log = MagicMock()

        def __init__(self) -> None:
            self.frames: list[dict] = []
            self._approval_futures: dict = {}
            self._pending_approvals = {
                "id": {"id": "id", "slot": "", "instance": "live"},
                "exp": {"id": "exp", "slot": "cron:job", "instance": "gone"},
            }

        def broadcast_ws(self, kind: str, payload: dict) -> None:
            self.frames.append({"kind": kind, **payload})

        def _audit_and_broadcast_approval(
            self, session_key, approval_id, approved, decision="", *, instance=""
        ):
            ApprovalCoordinator.audit_and_broadcast(
                self,
                session_key,
                approval_id,
                approved,
                decision,
                audit_provider=MagicMock(),
                instance=instance,
            )

    fake = _State()
    loop = asyncio.new_event_loop()
    try:
        fake._approval_futures["id"] = loop.create_future()
        assert ApprovalCoordinator.resolve_state(fake, "id", True)
    finally:
        loop.close()
    ApprovalCoordinator._retire_unresolved(fake, "exp", "cron:job")
    assert fake.frames == [
        {"kind": "approval_resolved", "id": "id", "approved": True, "instance": "live"},
        {
            "kind": "approval_resolved",
            "id": "exp",
            "approved": False,
            "slot": "cron:job",
            "decision": "expired",
            "instance": "gone",
        },
    ]


def test_coordinator_records_carry_a_distinct_instance_per_request():
    """The request id is the caller's and can recur; the instance is minted here,
    once per request, so two requests sharing an id are told apart."""
    from kiro_crew.dashboard.interaction_coordinator import ApprovalCoordinator

    class _State:
        _APPROVAL_TIMEOUT = 0.01
        _BACKGROUND_APPROVAL_TIMEOUT_SECS = 0.01

        def __init__(self):
            self._approval_futures = {}
            self._pending_approvals = {}
            self.records = []

        def broadcast_ws(self, kind, payload):
            if kind == "approval":
                self.records.append(dict(payload))

        def push_slots_update(self):
            pass

        def _audit_and_broadcast_approval(self, *args, **kwargs):
            pass

    async def run():
        st = _State()
        for _ in range(2):
            await ApprovalCoordinator.request(
                st,
                "same-id",
                "dashboard",
                "shell",
                tool_input="",
                tool_purpose="",
                slot="dashboard:selected",
                is_background=False,
                redact_url=lambda t: (t, []),
                redact_secret=lambda t: (t, []),
            )
        return st.records

    records = asyncio.run(run())
    assert len(records) == 2 and all(r["id"] == "same-id" for r in records)
    assert all(len(r["instance"]) == 32 for r in records)
    assert records[0]["instance"] != records[1]["instance"]


@pytest.mark.parametrize("proof", [None, "", [], {}, 1, "wrong-row"])
@pytest.mark.asyncio
async def test_dashboard_native_instance_proof_fails_closed(state, proof):
    selected = state.get_or_create_slot("selected", origin=SlotOrigin.USER)
    row = selected.append("permission", "Tool", json.dumps({"request_id": "id"}))
    current = asyncio.get_running_loop().create_future()
    selected.register_approval("id", current, row)
    app = _make_mode_app(state)
    app.router.add_post("/api/chat/slots/{slot}/approve", api_chat_slot_approve)
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/api/chat/slots/selected/approve",
            json={
                "origin": "native",
                "request_id": "id",
                "request_mid": proof,
                "action": "approved",
            },
        )
    assert response.status == (404 if proof == "wrong-row" else 400)
    assert not current.done()
    assert selected.approval_instance("id") == row_mid(row)
    assert not selected._trust and not selected._trust_reads


@pytest.mark.parametrize("action", ["approved", "rejected", "rejected_once"])
@pytest.mark.parametrize("pending", ["live", "missing", "done"])
@pytest.mark.parametrize("coordinator_slot", ["selected", "other"])
@pytest.mark.asyncio
async def test_dashboard_strict_native_never_falls_into_coordinator(
    state, action, pending, coordinator_slot
):
    loop = asyncio.get_running_loop()
    selected = state.get_or_create_slot("selected", origin=SlotOrigin.USER)
    other = state.get_or_create_slot("other", origin=SlotOrigin.USER)
    native = loop.create_future()
    permission = selected.append(
        "permission", "Current tool", json.dumps({"request_id": "same-id"})
    )
    if pending != "missing":
        selected.register_approval("same-id", native, permission)
    if pending == "done":
        native.set_result("rejected_once")
    other_future = loop.create_future()
    sibling = loop.create_future()
    other._approval_futures["same-id"] = other_future
    selected._approval_futures["sibling"] = sibling
    coordinator = loop.create_future()
    state._approval_futures["same-id"] = coordinator
    state._pending_approvals["same-id"] = {"id": "same-id", "slot": coordinator_slot}
    app = _make_mode_app(state)
    app.router.add_post("/api/chat/slots/{slot}/approve", api_chat_slot_approve)
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/api/chat/slots/selected/approve",
            json={
                "action": action,
                "request_id": "same-id",
                "origin": "native",
                "request_mid": row_mid(permission),
            },
        )
        assert response.status == (200 if pending == "live" else 404)
        if pending == "live":
            assert native.result() == action
        elif pending == "missing":
            assert not native.done()
        assert not coordinator.done()
        assert not other_future.done()
        assert not sibling.done()
        assert not selected._trust and not selected._trust_reads


@pytest.mark.parametrize("action", ["approve", "reject", "reject_once"])
@pytest.mark.asyncio
async def test_dashboard_legacy_coordinator_endpoint_keeps_native_fallback(state, action):
    selected = state.get_or_create_slot("selected", origin=SlotOrigin.USER)
    future = asyncio.get_running_loop().create_future()
    selected._approval_futures["legacy"] = future
    app = _make_mode_app(state)
    app.router.add_post("/api/approvals/{id}/{action}", api_approval_resolve)
    async with TestClient(TestServer(app)) as client:
        response = await client.post(f"/api/approvals/legacy/{action}", json={})
    assert response.status == 200
    assert (
        future.result()
        == {"approve": "approved", "reject": "rejected", "reject_once": "rejected_once"}[action]
    )


@pytest.mark.parametrize(
    "origin,request_id,action",
    [
        ("coordinator", "same-id", "approved"),
        (None, "same-id", "approved"),
        ("native", "", "approved"),
        ("native", [], "approved"),
        ("native", "same-id", "trust"),
        ("native", "same-id", "unknown"),
    ],
)
@pytest.mark.asyncio
async def test_dashboard_strict_native_invalid_target_refuses_without_mutation(
    state, origin, request_id, action
):
    selected = state.get_or_create_slot("selected", origin=SlotOrigin.USER)
    future = asyncio.get_running_loop().create_future()
    selected._approval_futures["same-id"] = future
    app = _make_mode_app(state)
    app.router.add_post("/api/chat/slots/{slot}/approve", api_chat_slot_approve)
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/api/chat/slots/selected/approve",
            json={"origin": origin, "request_id": request_id, "action": action},
        )
    assert response.status == 400
    assert not future.done()
    assert not selected._trust and not selected._trust_reads


@pytest.mark.parametrize(
    "origin,slot,action",
    [
        ("native", "selected", "approve"),
        ("", "selected", "approve"),
        ("coordinator", "", "approve"),
        ("coordinator", "selected", "unknown"),
    ],
)
@pytest.mark.asyncio
async def test_dashboard_strict_coordinator_invalid_target_refuses_without_mutation(
    state, origin, slot, action
):
    future = asyncio.get_running_loop().create_future()
    state._approval_futures["same-id"] = future
    state._pending_approvals["same-id"] = {"id": "same-id", "slot": "selected"}
    app = _make_mode_app(state)
    app.router.add_post("/api/approvals/{id}/{action}", api_approval_resolve)
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            f"/api/approvals/same-id/{action}",
            params={"origin": origin, "slot": slot},
            json={},
        )
    assert response.status == 400
    assert not future.done()


@pytest.mark.parametrize("strict", [True, False])
@pytest.mark.asyncio
async def test_dashboard_native_origin_does_not_adopt_replacement_slot_future(state, strict):
    selected = state.get_or_create_slot("selected", origin=SlotOrigin.USER)
    replacement = state.get_or_create_slot("replacement", origin=SlotOrigin.USER)
    selected.linked_session_key = replacement.linked_session_key = "slack:thread"
    future = asyncio.get_running_loop().create_future()
    replacement._approval_futures["same-id"] = future
    app = _make_mode_app(state)
    app.router.add_post("/api/chat/slots/{slot}/approve", api_chat_slot_approve)
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/api/chat/slots/selected/approve",
            json={
                "action": "approved",
                "request_id": "same-id",
                **({"origin": "native", "request_mid": "missing-instance"} if strict else {}),
            },
        )
    assert response.status == (404 if strict else 200)
    assert future.done() is not strict


@pytest.mark.asyncio
async def test_app_yolo_approval_is_refused_and_audited_as_the_app(state) -> None:
    slot = state.get_or_create_slot("s1", origin=SlotOrigin.USER)
    future = asyncio.get_running_loop().create_future()
    slot._approval_futures["req-1"] = future
    override = _FakeOverride()
    audit = MagicMock()
    with (
        patch(
            "kiro_crew.apps.permissions.app_can_manage_session_approvals",
            return_value=True,
        ),
        patch(
            "kiro_crew.dashboard.chat_handlers.safety_override",
            return_value=override,
        ),
        patch("kiro_crew.dashboard.chat_handlers.sel", return_value=audit),
    ):
        async with _app_client(state) as client:
            resp = await client.post(
                "/api/chat/slots/s1/approve",
                json={"action": "yolo", "request_id": "req-1"},
            )
            body = await resp.json()
    assert resp.status == 403
    assert body["code"] == "app_yolo_forbidden"
    assert override.activate_calls == []
    assert future.done() is False
    assert audit.log_api_access.call_args.kwargs["caller"] == "crew-keyboard"


@pytest.mark.asyncio
async def test_app_trust_does_not_persist_linked_channel_trust(state) -> None:
    slot = state.get_or_create_slot("s1", origin=SlotOrigin.USER)
    slot._slack_channel = "ch1"
    channel = MagicMock(trusted=False)
    state.channel_manager = MagicMock(_channels={"ch1": channel})
    with patch(
        "kiro_crew.apps.permissions.app_can_manage_session_approvals",
        return_value=True,
    ):
        async with _app_client(state) as client:
            resp = await client.post("/api/chat/mode", json={"mode": "trust", "slot": "s1"})
            assert resp.status == 200
    assert slot._trust is True
    assert channel.trusted is False
    channel._save.assert_not_called()


@pytest.mark.asyncio
async def test_app_normal_does_not_clear_linked_channel_trust(state) -> None:
    slot = state.get_or_create_slot("s1", origin=SlotOrigin.USER)
    slot._trust = True
    slot._slack_channel = "ch1"
    channel = MagicMock(trusted=True)
    state.channel_manager = MagicMock(_channels={"ch1": channel})
    with patch(
        "kiro_crew.apps.permissions.app_can_manage_session_approvals",
        return_value=True,
    ):
        async with _app_client(state) as client:
            resp = await client.post("/api/chat/mode", json={"mode": "normal", "slot": "s1"})
            assert resp.status == 200
    assert slot._trust is False
    assert channel.trusted is True
    channel._save.assert_not_called()


# ── defect 1: trust_reads must not widen on an unknown slot ──


@pytest.mark.asyncio
async def test_trust_reads_unknown_slot_is_400_and_revokes_nothing(state) -> None:
    """A slot-scoped request naming a missing slot widens to nothing.

    The live global grant must survive the refusal: ``deactivate`` is never
    even reached, and no slot's flags change.
    """
    state.get_or_create_slot("s1")
    state.get_or_create_slot("s2")
    override = _FakeOverride(active=True)
    with patch("kiro_crew.dashboard.chat_handlers.safety_override", return_value=override):
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/mode", json={"mode": "trust_reads", "slot": "ghost"}
            )
            assert resp.status == 400
            assert (await resp.json()) == {"ok": False, "error": "unknown slot"}
    assert override.active is True
    assert override.deactivate_calls == []
    assert all(not s._trust_reads and not s._trust for s in state._slots.values())


@pytest.mark.asyncio
async def test_trust_reads_non_string_slot_key_is_rejected(state) -> None:
    """A truthy non-string key is rejected, not routed to the all-slots branch."""
    state.get_or_create_slot("s1")
    async with _client(state) as client:
        resp = await client.post("/api/chat/mode", json={"mode": "trust_reads", "slot": 123})
        assert resp.status == 400
    assert state._slots["s1"]._trust_reads is False


@pytest.mark.asyncio
async def test_falsy_non_string_slot_key_is_rejected_for_trust(state) -> None:
    """Falsy non-strings (``[]``/``{}``/``0``/``False``) must not erase into the all-slots scope.

    ``body.get("slot") or None`` collapses an empty list -- and every other
    falsy non-string -- into ``None``, which is the documented "all slots"
    request; before this fix ``{"mode": "trust", "slot": []}`` trusted EVERY
    slot. The raw value must be refused before that normalization, and the
    live global grant must survive the refusal.
    """
    state.get_or_create_slot("s1")
    state.get_or_create_slot("s2")
    override = _FakeOverride(active=True)
    with patch("kiro_crew.dashboard.chat_handlers.safety_override", return_value=override):
        async with _client(state) as client:
            for bad in ([], {}, 0, False):
                resp = await client.post("/api/chat/mode", json={"mode": "trust", "slot": bad})
                assert resp.status == 400, bad
                assert (await resp.json()) == {"ok": False, "error": "unknown slot"}
    assert override.active is True
    assert override.deactivate_calls == []
    assert all(not s._trust_reads and not s._trust for s in state._slots.values())


@pytest.mark.asyncio
async def test_falsy_non_string_slot_key_is_rejected_for_trust_reads(state) -> None:
    state.get_or_create_slot("s1")
    override = _FakeOverride(active=True)
    with patch("kiro_crew.dashboard.chat_handlers.safety_override", return_value=override):
        async with _client(state) as client:
            resp = await client.post("/api/chat/mode", json={"mode": "trust_reads", "slot": []})
            assert resp.status == 400
            assert (await resp.json()) == {"ok": False, "error": "unknown slot"}
    assert override.active is True
    assert override.deactivate_calls == []
    assert state._slots["s1"]._trust_reads is False
    assert state._slots["s1"]._trust is False


# ── defect 2: a rejected request must leave the global grant untouched ──


@pytest.mark.asyncio
async def test_rejected_normal_request_leaves_the_global_grant_active(state) -> None:
    """'{"mode": "normal", "slot": " "}' must not revoke the grant.

    The unknown-slot 400 must be raised BEFORE the revocation, so a refused
    request cannot silently end YOLO mode.
    """
    state.get_or_create_slot("s1")
    override = _FakeOverride(active=True)
    with patch("kiro_crew.dashboard.chat_handlers.safety_override", return_value=override):
        async with _client(state) as client:
            resp = await client.post("/api/chat/mode", json={"mode": "normal", "slot": " "})
            assert resp.status == 400
            assert (await resp.json()) == {"ok": False, "error": "unknown slot"}
    assert override.active is True
    assert override.deactivate_calls == []


@pytest.mark.asyncio
async def test_rejected_trust_request_leaves_the_global_grant_active(state) -> None:
    state.get_or_create_slot("s1")
    override = _FakeOverride(active=True)
    with patch("kiro_crew.dashboard.chat_handlers.safety_override", return_value=override):
        async with _client(state) as client:
            resp = await client.post("/api/chat/mode", json={"mode": "trust", "slot": "ghost"})
            assert resp.status == 400
    assert override.active is True
    assert override.deactivate_calls == []


@pytest.mark.asyncio
async def test_rejected_trust_reads_does_not_touch_existing_slots(state) -> None:
    state.get_or_create_slot("s1")
    state.get_or_create_slot("s2")
    with patch(
        "kiro_crew.dashboard.chat_handlers.safety_override",
        return_value=_FakeOverride(active=True),
    ):
        async with _client(state) as client:
            resp = await client.post(
                "/api/chat/mode", json={"mode": "trust_reads", "slot": "ghost"}
            )
            assert resp.status == 400
    assert all(not s._trust_reads and not s._trust for s in state._slots.values())


# ── happy paths: the repaired scope semantics hold ──


@pytest.mark.asyncio
async def test_trust_reads_named_slot_only(state) -> None:
    """The named slot is the only one that trusts reads (widening regression)."""
    state.get_or_create_slot("s1")
    state.get_or_create_slot("s2")
    async with _client(state) as client:
        resp = await client.post("/api/chat/mode", json={"mode": "trust_reads", "slot": "s1"})
        assert resp.status == 200
        assert (await resp.json())["mode"] == "trust_reads"
    assert state._slots["s1"]._trust_reads is True
    assert state._slots["s2"]._trust_reads is False


@pytest.mark.asyncio
async def test_trust_named_slot_only(state) -> None:
    """The named slot is the only one trusted (mirrors the trust_reads case).

    Guards the same widening regression on the ``trust`` branch: a slot-scoped
    trust request must not flip its siblings, and the approval policy must be
    set to ``auto`` for the named slot's session only.
    """
    state.get_or_create_slot("s1")
    state.get_or_create_slot("s2")
    async with _client(state) as client:
        resp = await client.post("/api/chat/mode", json={"mode": "trust", "slot": "s1"})
        assert resp.status == 200
        assert (await resp.json())["mode"] == "trust"
    assert state._slots["s1"]._trust is True
    assert state._slots["s2"]._trust is False
    state.sessions.set_approval_policy.assert_any_call("dashboard:s1", "auto")


@pytest.mark.asyncio
async def test_trust_without_a_slot_is_still_global(state) -> None:
    """An absent slot key keeps the documented all-slots meaning for trust."""
    state.get_or_create_slot("s1")
    state.get_or_create_slot("s2")
    async with _client(state) as client:
        resp = await client.post("/api/chat/mode", json={"mode": "trust"})
        assert resp.status == 200
    assert all(s._trust for s in state._slots.values())


# ── interplay: a slot-scoped trust/trust_reads must not revoke
# ── the process-global YOLO grant (the grant is global, the mode is per-slot)


@pytest.mark.asyncio
async def test_named_slot_trust_reads_leaves_an_active_grant_live(state) -> None:
    """A named-slot trust_reads applies to that slot and does NOT revoke YOLO.

    The trust-grant narrowing and the slot isolation must hold
    together: only the named slot trusts reads, and the operator's live grant
    survives the request.
    """
    state.get_or_create_slot("s1")
    state.get_or_create_slot("s2")
    override = _FakeOverride(active=True)
    with patch("kiro_crew.dashboard.chat_handlers.safety_override", return_value=override):
        async with _client(state) as client:
            resp = await client.post("/api/chat/mode", json={"mode": "trust_reads", "slot": "s1"})
            assert resp.status == 200
    assert override.active is True
    assert override.deactivate_calls == []
    assert state._slots["s1"]._trust_reads is True
    assert state._slots["s2"]._trust_reads is False


@pytest.mark.asyncio
async def test_named_slot_trust_leaves_an_active_grant_live(state) -> None:
    """A named-slot trust applies to that slot and does NOT revoke YOLO."""
    state.get_or_create_slot("s1")
    state.get_or_create_slot("s2")
    override = _FakeOverride(active=True)
    with patch("kiro_crew.dashboard.chat_handlers.safety_override", return_value=override):
        async with _client(state) as client:
            resp = await client.post("/api/chat/mode", json={"mode": "trust", "slot": "s1"})
            assert resp.status == 200
    assert override.active is True
    assert override.deactivate_calls == []
    assert state._slots["s1"]._trust is True
    assert state._slots["s2"]._trust is False


@pytest.mark.asyncio
async def test_trust_reads_without_a_slot_is_still_global(state) -> None:
    """An absent slot key keeps its documented all-slots meaning."""
    state.get_or_create_slot("s1")
    state.get_or_create_slot("s2")
    async with _client(state) as client:
        resp = await client.post("/api/chat/mode", json={"mode": "trust_reads"})
        assert resp.status == 200
    assert all(s._trust_reads for s in state._slots.values())


@pytest.mark.asyncio
async def test_normal_mode_named_slot_only(state) -> None:
    """A named-slot normal request revokes that slot, not its siblings."""
    state.get_or_create_slot("s1")
    state.get_or_create_slot("s2")
    state._slots["s1"]._trust = True
    state._slots["s2"]._trust = True
    async with _client(state) as client:
        resp = await client.post("/api/chat/mode", json={"mode": "normal", "slot": "s1"})
        assert resp.status == 200
    assert state._slots["s1"]._trust is False
    assert state._slots["s2"]._trust is True


# ── defect 3: deactivate runs off the event loop ──


@pytest.mark.asyncio
async def test_deactivate_runs_off_the_event_loop(state) -> None:
    """deactivate() writes a SEL event and must not run on the gateway loop."""
    state.get_or_create_slot("s1")
    captured: dict[str, int] = {}

    class _TrackingOverride:
        is_declared = False

        def deactivate(self, source: str) -> None:
            captured["thread"] = threading.get_ident()

        def is_active(self) -> bool:
            return False

    with patch(
        "kiro_crew.dashboard.chat_handlers.safety_override",
        return_value=_TrackingOverride(),
    ):
        async with _client(state) as client:
            loop_thread = threading.get_ident()
            resp = await client.post("/api/chat/mode", json={"mode": "normal"})
            assert resp.status == 200
    assert captured["thread"] != loop_thread


@pytest.mark.asyncio
async def test_valid_normal_mode_touches_the_real_singleton(state) -> None:
    """The happy path exercises the real SafetyOverride, not just the fake."""
    state.get_or_create_slot("s1")
    async with _client(state) as client:
        resp = await client.post("/api/chat/mode", json={"mode": "normal"})
        assert resp.status == 200
    assert real_safety_override().is_active() is False


@pytest.mark.asyncio
async def test_a_held_app_recovery_cannot_extend_a_later_apps_stream(state, monkeypatch) -> None:
    _scripted_turns(state, monkeypatch, "", "later app reply", "older recovered reply")
    slot = _user_slot(state)
    slot._titled = True
    # The first turn queues a real recovery, but it cannot drain until later.
    with patch.object(chat_runner, "_start_next_queued_turn", new=AsyncMock(return_value=False)):
        await chat_runner._run_chat(state, slot, "older app request", _turn_actor="app")
    [recovery] = slot._queue
    assert recovery["meta"][TURN_ACTOR_META_KEY] == "app"
    assert recovery["meta"][chat_runner._RECOVERY_TURN_META_KEY]
    slot.drain()

    pending = await _run_app_turn_and_successors(state, slot)

    boundary = pending.index((TURN_END_WIRE_CLS, ""))
    assert any("later app reply" in content for _, content in pending[:boundary])
    assert any("older recovered reply" in content for _, content in pending[boundary:])
    assert all("older recovered reply" not in content for _, content in pending[:boundary])


@pytest.mark.asyncio
async def test_restored_recovery_identity_cannot_continue_a_live_stream(state) -> None:
    from kiro_crew.dashboard.slot_queue_repository import RESTORED_QUEUE_KEY

    slot = _user_slot(state)
    slot.queue_insert(
        0,
        "retry",
        kind=SYNTHETIC_RECOVERY_KIND,
        meta={TURN_ACTOR_META_KEY: "app", chat_runner._RECOVERY_TURN_META_KEY: "turn-a"},
    )
    slot._queue[0][RESTORED_QUEUE_KEY] = True

    await _drain_once(state, slot, predecessor_actor="app", predecessor_turn_id="turn-a")

    assert slot.drain()[0]["cls"] == TURN_END_WIRE_CLS


@pytest.mark.parametrize("owned", [False, True])
@pytest.mark.asyncio
async def test_restored_app_turn_titles_only_an_app_owned_slot(state, owned: bool) -> None:
    live = state.get_or_create_slot("s0", origin=SlotOrigin.USER)
    queue_for_next_turn(state, live, "restored app request", turn_actor="app")
    slot = _user_slot(state)
    slot._queue = sanitize_restored_queue(durable_queue_entries(live._queue))
    assert TURN_ACTOR_META_KEY not in slot._queue[0].get("meta", {})
    row = await _drain_once(state, slot)
    # Exercise the titler's owner exemption independently of queue admission:
    # an app-bound restore may itself be held by containment revalidation.
    slot._app = "crew-keyboard" if owned else ""
    slot.append("assistant", "restored app reply")
    assert TURN_ACTOR_META_KEY not in row["meta"]
    assert _counts_as_user_turn(slot, row) is owned
    generate = AsyncMock(return_value="")

    with (
        patch("kiro_crew.dashboard.chat_title._generate_title_via_kiro", new=generate),
        patch("kiro_crew.dashboard.chat_title._persist_title", new=AsyncMock(return_value=True)),
        patch("kiro_crew.dashboard.chat_title.maybe_suggest_folder", new=AsyncMock()),
    ):
        await _maybe_auto_title(state, slot)

    assert generate.called is owned
    assert slot._titled is owned
    if owned:
        assert _prompt_lines(generate) == ["restored app request", "restored app reply"]
        assert slot.title == "restored app request"
    else:
        assert slot.title != "restored app request"


@pytest.mark.parametrize("restored_first", [False, True])
@pytest.mark.asyncio
async def test_restored_and_fresh_queue_rows_never_merge(state, restored_first: bool) -> None:
    from kiro_crew.dashboard.chat_title import _titling_messages

    slot = _user_slot(state)
    restored = sanitize_restored_queue([{"id": "old", "content": "restored app request"}])
    slot.queue_append("fresh user request", directive_user_origin=True)
    slot._queue = restored + slot._queue if restored_first else slot._queue + restored
    config = KiroCrewConfig()
    config.dashboard.merge_queued_messages = True
    with patch.object(KiroCrewConfig, "load", return_value=config):
        first = await _drain_once(state, slot)
        slot.append("assistant", "first reply")
        assert slot.queue_depth == 1
        second = await _drain_once(state, slot)
        slot.append("assistant", "second reply")
    assert slot.queue_depth == 0
    fresh, restored_row = (second, first) if restored_first else (first, second)
    assert fresh["content"] == "fresh user request"
    assert restored_row["content"] == "restored app request"
    assert _counts_as_user_turn(slot, fresh)
    assert not _counts_as_user_turn(slot, restored_row)
    assert [row["content"] for row in _titling_messages(slot)] == [
        "fresh user request",
        "second reply" if restored_first else "first reply",
    ]
    # Keep the count above the fixed-milestone floor, so counting the restored
    # row incorrectly changes the rebase result instead of hiding under it.
    slot.append("user", "another user request", "msg msg-u")
    slot.append("user", "one more user request", "msg msg-u")
    # A transcript reload sees the same persisted meta and uses the same count.
    slot.messages = json.loads(json.dumps(slot.messages))
    slot._title_refresh_mark = 6
    chat_persistence._rebase_rehydrated_refresh_mark(slot)
    assert slot._title_refresh_mark == _rehydrated_refresh_mark(6, 3)


@pytest.mark.parametrize("successor", ["human", "app", "missing-mid"])
@pytest.mark.asyncio
async def test_app_sse_backstop_rejects_a_foreign_user_row_without_turn_end(
    state, successor: str
) -> None:
    slot = _user_slot(state)

    async def without_boundary(_state, s, _message, **_kwargs):
        # The handler normally drains its opening row before dispatch. Redeliver
        # that exact row to prove the reader recognizes its own minted mid.
        s._pending.append(_user_rows(s)[-1])
        s.append("assistant", "own reply")
        # No turn_end: the reader must defend its scope independently.
        meta = {TURN_ACTOR_META_KEY: "app"} if successor == "app" else None
        row = s.append("user", "private successor request", "msg msg-u", meta=meta)
        if successor == "missing-mid":
            row["meta"].pop("mid")
        s.append("assistant", "private successor reply")
        s.append("done", "", "done")

    with (
        patch(_GRANT_CHECK, return_value=True),
        patch("kiro_crew.dashboard.chat_handlers._run_chat", new=without_boundary),
        patch("kiro_crew.dashboard.chat_handlers._maybe_auto_title", new=AsyncMock()),
    ):
        async with _app_client(state) as client:
            resp = await client.post("/api/chat", json={"slot": "s1", "message": "own request"})
            stream = await asyncio.wait_for(resp.text(), timeout=5)

    assert resp.status == 200
    assert "own request" in stream and "own reply" in stream
    assert "private successor" not in stream
    assert stream.rstrip().endswith("data: [DONE]")
    assert slot._has_reader is False
    assert any(row["content"] == "private successor reply" for row in slot.messages)
