"""A Bot's own thread linked to the owner's Discord DM keeps conducting its workers.

The thread (a ``member-*`` slot) is dashboard-born, so a ``!sessions`` pick gives it
an outbound mirror to the DM and the owner-DM predicate's dashboard-born shape
decides whether session control and the work ledger still admit it.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from chat_test_helpers import _make_state
from member_memory_helpers import forget_declared_stores, write_member_home

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard import create_rate_limit
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.dashboard.handlers import work_ledger as ledger_routes
from kiro_crew.members import DM_SLOT_KEY_PREFIX
from kiro_crew.messaging.link import ChannelLink
from kiro_crew.messaging.transport import ConfiguredChannelTarget
from kiro_crew.mirror_admission import sign_mirror_admission
from kiro_crew.session_map import SessionMap

OWNER = "123456789012345678"
DM_CHANNEL = "dm-channel-4242"
_MEMBER = "radar"


@pytest.fixture(autouse=True)
def _fresh_create_budget():
    create_rate_limit.reset_for_tests()
    yield
    create_rate_limit.reset_for_tests()


class _Discord:
    """The transport surfaces the predicate and the reply leg read."""

    channel_type = "discord"

    def __init__(self, *, paired: bool) -> None:
        self.capabilities = SimpleNamespace(
            supports_proactive_send=True, max_message_chars=2000, max_message_bytes=0
        )
        self._pairings = {DM_CHANNEL: OWNER} if paired else {}
        self.send_message = AsyncMock(return_value="mid-1")

    def configured_targets(self) -> list[ConfiguredChannelTarget]:
        return [ConfiguredChannelTarget(f"user:{OWNER}", f"DM · {OWNER}")]

    def direct_peer_of(self, conversation_id: str) -> str:
        return self._pairings.get(conversation_id, "")

    def may_send_to(self, conversation_id: str, thread_id=None, principal: str = "") -> bool:
        return principal == OWNER


def _bot_thread(tmp_path: Path, monkeypatch, *, paired: bool):
    """A Bot's own thread, with its execution bound, beside a Discord channel."""
    from kiro_crew.config.sections import ResolvedBindings
    from kiro_crew.execution_context import bind_session_execution, resolve_member_execution
    from kiro_crew.history import ConversationLog

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    config = write_member_home(tmp_path, _MEMBER)
    (tmp_path / "config.json").write_text(json.dumps(config), encoding="utf-8")
    forget_declared_stores(monkeypatch)
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.session_map.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    state.conversation_log = ConversationLog()
    store = SessionMap()
    for name in (
        "set_slack_link",
        "get_slack_link",
        "set_mirror_link",
        "get_mirror_link",
        "clear_mirror_link",
        "find_mirror_sessions",
        "mirror_accepts_inbound",
        "batched_save",
    ):
        setattr(state.sessions, name, getattr(store, name))
    state.sessions.get_origin_link = MagicMock(return_value=None)
    state.push_slots_update = MagicMock()
    transport = _Discord(paired=paired)
    state.register_channel_transport(transport)

    caller = state.get_or_create_slot(DM_SLOT_KEY_PREFIX + _MEMBER, mode="member")
    execution = resolve_member_execution(KiroCrewConfig.load(), _MEMBER)
    caller.agent = _MEMBER
    caller.memory_store = execution.store.legacy_name
    bind_session_execution(slot_history_key(caller), execution, vouch=True)

    def resolve(_cfg, name, *_args, **_kwargs):
        selected = resolve_member_execution(_cfg, name or _MEMBER)
        return ResolvedBindings(
            workspace_dir=Path("workspace"),
            memory_store_name=selected.store.legacy_name,
            effective_memory_config={},
            kiro_agent=selected.template_id,
            selection_kind="member",
            resolved_alias=name or _MEMBER,
            execution_context=selected,
        )

    monkeypatch.setattr(sc, "resolve_agent_bindings", resolve)
    monkeypatch.setattr(sc, "_workspace_name_for_dir", lambda *_: caller.workspace)
    monkeypatch.setattr(sc, "session_control_enabled", lambda: True)
    monkeypatch.setattr(sc, "member_dispatch_enabled", lambda: True)
    return state, caller, transport


def _link_from_discord(state, caller) -> None:
    """The row a ``!sessions`` pick commits: the presser as peer, gateway-signed."""
    key = slot_history_key(caller)
    link = ChannelLink("discord", channel_id=DM_CHANNEL, principal=OWNER)
    admitted = dataclasses.replace(link, admission=sign_mirror_admission(key, link) or None)
    state.sessions.set_mirror_link(key, admitted, accepts_inbound=True)


def _ledger_gate(state, sk: str):
    app = web.Application()
    app["state"] = state
    request = make_mocked_request("GET", "/api/work-ledger", app=app, headers={"X-Session-Key": sk})
    request["internal_auth"] = True
    return asyncio.run(ledger_routes._caller_key(request, "work_ledger_read"))


@pytest.fixture
def open_ledger_gate(monkeypatch):
    async def _recognized(*a, **k):
        return None

    monkeypatch.setattr(ledger_routes, "_recognize_session", _recognized)
    monkeypatch.setattr(ledger_routes, "_is_restricted_session", lambda *a: False)


def test_a_bot_thread_linked_to_the_owners_dm_still_conducts(
    tmp_path, monkeypatch, open_ledger_gate
):
    state, caller, _ = _bot_thread(tmp_path, monkeypatch, paired=True)
    key = slot_history_key(caller)
    worker = asyncio.run(sc.create_session(state, caller_session_key=key))["target"]
    _link_from_discord(state, caller)

    assert sc.owner_dm_refusal(state, caller) == ""
    asyncio.run(sc.create_session(state, caller_session_key=key))
    for op in ("send", "read", "stop"):
        assert sc.authorize_target(state, caller_session_key=key, target=worker, operation=op)
    _, refusal = _ledger_gate(state, key)
    assert refusal is None


def test_after_a_restart_the_linked_bot_thread_is_cut_off_until_the_dm_speaks(
    tmp_path, monkeypatch, open_ledger_gate
):
    state, caller, _ = _bot_thread(tmp_path, monkeypatch, paired=False)
    key = slot_history_key(caller)
    worker = asyncio.run(sc.create_session(state, caller_session_key=key))["target"]
    _link_from_discord(state, caller)

    assert sc.owner_dm_refusal(state, caller) == sc.MIRROR_PEER_NOT_ON_RECORD
    with pytest.raises(sc.SessionControlError) as exc:
        sc.authorize_target(state, caller_session_key=key, target=worker, operation="send")
    assert exc.value.code == "mirrored_caller"
    _, refusal = _ledger_gate(state, key)
    assert refusal is not None and refusal.status == 403


def test_a_reply_admitted_before_the_link_is_withheld_from_the_dm(
    tmp_path, monkeypatch, open_ledger_gate
):
    from kiro_crew.dashboard.chat_runner import _deliver_cross_surface_reply

    state, caller, transport = _bot_thread(tmp_path, monkeypatch, paired=True)
    key = slot_history_key(caller)
    caller._active_turn_session_key = key
    _, refusal = _ledger_gate(state, key)
    assert refusal is None
    _link_from_discord(state, caller)

    asyncio.run(_deliver_cross_surface_reply(state, key, "the reply", slot=caller))
    assert transport.send_message.await_count == 0

    caller._steer_audience_fences.clear()
    asyncio.run(_deliver_cross_surface_reply(state, key, "the next reply", slot=caller))
    assert transport.send_message.await_count == 1


def test_a_turn_from_discord_still_cannot_use_the_dashboard_only_tools(tmp_path, monkeypatch):
    """Cards, guides and the Bot's own-record tools gate on a dashboard turn of the
    slot; a Discord turn on the thread runs in the messaging driver, so none runs."""
    from kiro_crew.dashboard.guide_runs import GuideError
    from kiro_crew.dashboard.handlers import guide

    state, caller, _ = _bot_thread(tmp_path, monkeypatch, paired=True)
    _link_from_discord(state, caller)
    monkeypatch.setattr("kiro_crew.member_memory_auth.session_key_is_attested", lambda *a: True)
    app = web.Application()
    app["state"] = state
    request = make_mocked_request(
        "POST",
        "/api/guide/agent/start",
        app=app,
        headers={"X-Session-Key": slot_history_key(caller)},
    )
    request["internal_auth"] = True

    with pytest.raises(GuideError) as exc:
        asyncio.run(guide._resolve_agent_caller(request, "guide.start"))
    assert exc.value.code == "no_dashboard_turn"
