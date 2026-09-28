"""A slot's ``SafetyOverride`` scoped grant reaches its spawn gate, its children and its badge.

An unattended app crew is trusted through ``slot._trust_scope`` plus a live scoped
grant, never through ``slot._trust``. Its own tool approvals honour that grant via
``chat_runner._slot_is_trusted``. The spawn prompt and every tool call of a spawned
child go through the gateway's ``_interactive_approval("subagent")`` callback, so
that callback must take the same verdict, audit which grant it rode, keep the
low-fidelity-child block, and never renew the grant.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.chat_utils import effective_session_key
from kiro_crew.dashboard.slot_projection import live_trust_scope
from kiro_crew.safety_override import safety_override
from kiro_crew.slack import gateway as gw

_SCOPE = "crew:slack-radar:autoapprove"
_SO = type(safety_override())


def _slot(*, trust: bool = False, scope: str = "") -> SimpleNamespace:
    """Exactly the two trust attributes named; a MagicMock would invent a scope."""
    return SimpleNamespace(key="crew-slot", _trust=trust, _trust_scope=scope, running=True)


def _orchestrator(slot: SimpleNamespace) -> gw.GatewayOrchestrator:
    cfg = KiroCrewConfig()
    with patch.object(cfg, "load_credentials", return_value={}):
        orch = gw.GatewayOrchestrator(cfg)
    orch.slack = None
    ds = MagicMock()
    ds._yolo = False
    ds._slots = {"crew-slot": slot}
    ds.request_approval = AsyncMock(return_value=False)
    ds.dashboard_user_ws_count.return_value = 1
    orch.dashboard_state = ds
    return orch


def _event(*, low_fidelity: bool = False) -> MagicMock:
    event = MagicMock()
    event.request_id = "spawn:abc123"
    event.title = "spawn_run(watch the channel)"
    event.tool_input = ""
    event.tool_purpose = ""
    event.child_low_fidelity = low_fidelity
    event.child_unconditional_grant_eligible = False
    return event


async def _decide(slot: SimpleNamespace, *, scope_live: bool, low_fidelity: bool = False):
    orch = _orchestrator(slot)
    callback = orch._interactive_approval("subagent", slot_resolver=lambda _rid: "crew-slot")
    log = MagicMock()
    with (
        patch.object(_SO, "is_scope_active", return_value=scope_live),
        patch.object(_SO, "is_active", return_value=False),
        patch.object(_SO, "renew_scoped") as renew,
        patch("kiro_crew.slack.handler.is_yolo_mode", return_value=False),
        patch.object(gw, "sel") as sel_factory,
    ):
        sel_factory.return_value.log_api_access = log
        approved = await callback(_event(low_fidelity=low_fidelity), "")
    ops = [c.kwargs.get("operation") for c in log.call_args_list]
    return approved, ops, log, orch.dashboard_state.request_approval, renew


class TestSlotIsTrusted:
    def test_session_flag(self) -> None:
        assert chat_runner._slot_is_trusted(_slot(trust=True)) is True

    def test_live_scope(self) -> None:
        with patch.object(_SO, "is_scope_active", return_value=True):
            assert chat_runner._slot_is_trusted(_slot(scope=_SCOPE)) is True

    def test_lapsed_scope(self) -> None:
        with patch.object(_SO, "is_scope_active", return_value=False):
            assert chat_runner._slot_is_trusted(_slot(scope=_SCOPE)) is False

    def test_no_attributes(self) -> None:
        assert chat_runner._slot_is_trusted(SimpleNamespace()) is False


class TestSubagentApprovalHonoursScope:
    @pytest.mark.asyncio
    async def test_live_scope_auto_approves_with_its_own_audit(self) -> None:
        approved, ops, log, prompt, renew = await _decide(_slot(scope=_SCOPE), scope_live=True)
        assert approved is True
        prompt.assert_not_awaited()
        assert ops == ["subagent.trust_scope_auto_approve"]
        assert log.call_args.kwargs["resources"].startswith(f"scope:{_SCOPE} ")
        renew.assert_not_called()

    @pytest.mark.asyncio
    async def test_lapsed_scope_prompts(self) -> None:
        approved, ops, _log, prompt, _renew = await _decide(_slot(scope=_SCOPE), scope_live=False)
        assert approved is False
        prompt.assert_awaited_once()
        assert "subagent.scoped_trust_not_trusted" in ops
        assert "subagent.trust_scope_auto_approve" not in ops

    @pytest.mark.asyncio
    async def test_live_scope_still_blocks_a_low_fidelity_child(self) -> None:
        approved, ops, _log, prompt, _renew = await _decide(
            _slot(scope=_SCOPE), scope_live=True, low_fidelity=True
        )
        assert approved is False
        prompt.assert_awaited_once()
        assert "subagent.scoped_trust_blocked_low_fidelity_child" in ops

    @pytest.mark.asyncio
    async def test_session_flag_keeps_its_audit_name(self) -> None:
        approved, ops, _log, prompt, _renew = await _decide(_slot(trust=True), scope_live=False)
        assert approved is True
        prompt.assert_not_awaited()
        assert ops == ["subagent.scoped_trust_auto_approve"]


class TestProjection:
    def test_live_scope_is_projected(self) -> None:
        with patch.object(_SO, "scope_remaining_secs", return_value=120):
            assert live_trust_scope(_slot(scope=_SCOPE)) == _SCOPE

    def test_lapsed_scope_is_blank(self) -> None:
        with patch.object(_SO, "scope_remaining_secs", return_value=0):
            assert live_trust_scope(_slot(scope=_SCOPE)) == ""

    def test_policy_denied_scope_is_blank(self) -> None:
        with (
            patch.object(_SO, "scope_remaining_secs", return_value=120),
            patch("kiro_crew.dashboard.slot_projection.yolo_policy_permits", return_value=False),
        ):
            assert live_trust_scope(_slot(scope=_SCOPE)) == ""

    def test_no_scope_is_blank_without_a_lookup(self) -> None:
        with patch.object(_SO, "scope_remaining_secs") as remaining:
            assert live_trust_scope(_slot()) == ""
        remaining.assert_not_called()

    def test_projection_never_expires_the_grant(self) -> None:
        with (
            patch.object(_SO, "scope_remaining_secs", return_value=5),
            patch.object(_SO, "is_scope_active") as enforce,
        ):
            live_trust_scope(_slot(scope=_SCOPE))
        enforce.assert_not_called()

    def test_slot_dict_carries_the_field(self) -> None:
        from kiro_crew.dashboard.state import _ChatSlot

        slot = _ChatSlot("s1")
        slot._trust_scope = _SCOPE
        with patch.object(_SO, "scope_remaining_secs", return_value=60):
            d = slot.to_dict()
        assert d["trust_scope"] == _SCOPE
        assert d["trust"] is False


def _dashboard_state(tmp_path):
    from kiro_crew.dashboard.state import DashboardState
    from kiro_crew.history import ConversationLog

    sessions = MagicMock(count=0)
    sessions.get_pid = MagicMock(return_value=None)
    sessions.remove = AsyncMock()
    return DashboardState(
        sessions=sessions,
        crons=MagicMock(list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})),
        lessons=MagicMock(load_all=MagicMock(return_value=[])),
        start_time=0.0,
        conversation_log=ConversationLog(base_dir=tmp_path),
    )


async def _choose_mode(state, body: dict) -> web.Response:
    from kiro_crew.dashboard import chat_handlers

    app = web.Application()
    app["state"] = state
    req = make_mocked_request("POST", "/api/chat/mode", app=app)
    req["internal_auth"] = True

    async def _read(_request, **_kw):
        return body, None

    with patch.object(chat_handlers, "read_bounded_json", _read):
        return await chat_handlers.api_chat_mode(req)


class TestNormalEndsTheScopedGrant:
    """Normal is the off-switch for every state the header shows as Trust."""

    @pytest.mark.asyncio
    async def test_normal_on_the_slot_ends_its_live_scope(self, tmp_path) -> None:
        state = _dashboard_state(tmp_path)
        slot = state.get_or_create_slot("crew-slot")
        so = safety_override()
        assert so.activate_scoped(_SCOPE, source="test", ttl=600).active
        slot._trust_scope = _SCOPE
        try:
            with patch.object(gw, "sel"), patch("kiro_crew.dashboard.chat_handlers.sel") as hsel:
                resp = await _choose_mode(state, {"mode": "normal", "slot": "crew-slot"})
            assert resp.status == 200
            assert so.scope_remaining_secs(_SCOPE) == 0
            assert slot._trust_scope == ""
            assert slot.to_dict()["trust_scope"] == ""
            assert slot.to_dict()["trust"] is False
            ops = [
                c.kwargs.get("operation") for c in hsel.return_value.log_api_access.call_args_list
            ]
            assert "approval_mode.scope_cleared_by_user" in ops
            cleared = [
                c
                for c in hsel.return_value.log_api_access.call_args_list
                if c.kwargs.get("operation") == "approval_mode.scope_cleared_by_user"
            ]
            assert cleared[0].kwargs["resources"] == f"scope:{_SCOPE}"
        finally:
            so.deactivate_scope(_SCOPE)

    @pytest.mark.asyncio
    async def test_normal_without_a_scope_writes_no_scope_record(self, tmp_path) -> None:
        state = _dashboard_state(tmp_path)
        state.get_or_create_slot("plain-slot")
        with patch("kiro_crew.dashboard.chat_handlers.sel") as hsel:
            resp = await _choose_mode(state, {"mode": "normal", "slot": "plain-slot"})
        assert resp.status == 200
        ops = [c.kwargs.get("operation") for c in hsel.return_value.log_api_access.call_args_list]
        assert "approval_mode.scope_cleared_by_user" not in ops

    @pytest.mark.parametrize(
        "body", [{"mode": "trust_reads", "slot": "crew-slot"}, {"mode": "trust_reads"}]
    )
    @pytest.mark.asyncio
    async def test_reads_ends_the_live_scope_too(self, tmp_path, body) -> None:
        state = _dashboard_state(tmp_path)
        slot = state.get_or_create_slot("crew-slot")
        so = safety_override()
        assert so.activate_scoped(_SCOPE, source="test", ttl=600).active
        slot._trust_scope = _SCOPE
        try:
            with patch("kiro_crew.dashboard.chat_handlers.sel") as hsel:
                resp = await _choose_mode(state, body)
            assert resp.status == 200
            assert so.scope_remaining_secs(_SCOPE) == 0
            assert slot._trust_scope == ""
            assert slot._trust_reads is True
            assert slot.to_dict()["trust"] is False
            assert slot.to_dict()["trust_scope"] == ""
            ops = [
                c.kwargs.get("operation") for c in hsel.return_value.log_api_access.call_args_list
            ]
            assert "approval_mode.scope_cleared_by_user" in ops
        finally:
            so.deactivate_scope(_SCOPE)

    @pytest.mark.asyncio
    async def test_reads_clears_trust_on_every_slot_sharing_the_session(self, tmp_path) -> None:
        """A sibling's own ``_trust`` cannot rewrite the shared session back to auto."""
        state = _dashboard_state(tmp_path)
        slot = state.get_or_create_slot("crew-slot")
        sibling = state.get_or_create_slot("alias-slot")
        slot.linked_session_key = "slack:1700000000.000100"
        sibling.linked_session_key = "slack:1700000000.000100"
        key = effective_session_key(slot)
        assert effective_session_key(sibling) == key
        sibling._trust = True
        with patch("kiro_crew.dashboard.chat_handlers.sel"):
            resp = await _choose_mode(state, {"mode": "trust_reads", "slot": "crew-slot"})
        assert resp.status == 200
        assert sibling._trust is False
        assert slot._trust_reads is True
        policies = [
            c.args[1] for c in state.sessions.set_approval_policy.call_args_list if c.args[0] == key
        ]
        assert policies and "auto" not in policies

    @pytest.mark.asyncio
    async def test_trust_leaves_a_live_scope_alone(self, tmp_path) -> None:
        state = _dashboard_state(tmp_path)
        slot = state.get_or_create_slot("crew-slot")
        so = safety_override()
        assert so.activate_scoped(_SCOPE, source="test", ttl=600).active
        slot._trust_scope = _SCOPE
        try:
            with patch("kiro_crew.dashboard.chat_handlers.sel"):
                resp = await _choose_mode(state, {"mode": "trust", "slot": "crew-slot"})
            assert resp.status == 200
            assert so.scope_remaining_secs(_SCOPE) > 0
            assert slot._trust_scope == _SCOPE
        finally:
            so.deactivate_scope(_SCOPE)

    @pytest.mark.parametrize("body", [{"mode": "normal", "slot": "crew-slot"}, {"mode": "normal"}])
    @pytest.mark.asyncio
    async def test_a_slot_added_while_the_clear_awaits_does_not_abort_it(
        self, tmp_path, body
    ) -> None:
        state = _dashboard_state(tmp_path)
        slot = state.get_or_create_slot("crew-slot")
        so = safety_override()
        real_deactivate = so.deactivate_scope

        def _deactivate_and_grow(scope: str) -> None:
            real_deactivate(scope)
            state._slots["late-slot"] = state._slots["crew-slot"]

        assert so.activate_scoped(_SCOPE, source="test", ttl=600).active
        slot._trust_scope = _SCOPE
        try:
            with (
                patch.object(so, "deactivate_scope", _deactivate_and_grow),
                patch("kiro_crew.dashboard.chat_handlers.sel"),
            ):
                resp = await _choose_mode(state, body)
            assert resp.status == 200
            assert slot._trust_scope == ""
            state.sessions.set_approval_policy.assert_called()
        finally:
            so.deactivate_scope(_SCOPE)

    @pytest.mark.parametrize("mode", ["normal", "trust_reads"])
    @pytest.mark.asyncio
    async def test_a_slot_recreated_while_the_grant_ends_keeps_its_trust(
        self, tmp_path, mode
    ) -> None:
        """Only the slots the request resolved are revoked, not a replacement on the key."""
        state = _dashboard_state(tmp_path)
        slot = state.get_or_create_slot("crew-slot")
        so = safety_override()
        real_deactivate = so.deactivate_scope
        replacement: list = []

        def _deactivate_and_recreate(scope: str) -> None:
            real_deactivate(scope)
            del state._slots["crew-slot"]
            fresh = state.get_or_create_slot("crew-slot")
            fresh._trust = True
            replacement.append(fresh)

        assert so.activate_scoped(_SCOPE, source="test", ttl=600).active
        slot._trust_scope = _SCOPE
        try:
            with (
                patch.object(so, "deactivate_scope", _deactivate_and_recreate),
                patch("kiro_crew.dashboard.chat_handlers.sel"),
            ):
                resp = await _choose_mode(state, {"mode": mode, "slot": "crew-slot"})
            assert resp.status == 200
            assert replacement and replacement[0] is not slot
            assert replacement[0]._trust is True
            revokes = [
                c for c in state.sessions.set_approval_policy.call_args_list if c.args[1] == ""
            ]
            assert revokes == []
            assert so.scope_remaining_secs(_SCOPE) == 0
        finally:
            so.deactivate_scope(_SCOPE)

    @pytest.mark.asyncio
    async def test_a_slot_recreated_while_the_grant_ends_keeps_its_channel_trust(
        self, tmp_path
    ) -> None:
        """The stale slot's linked channel is not untrusted once a replacement owns the key."""
        state = _dashboard_state(tmp_path)
        slot = state.get_or_create_slot("crew-slot")
        channel = MagicMock(trusted=True)
        state.channel_manager = MagicMock(_channels={"C1": channel})
        slot._slack_channel = "C1"
        so = safety_override()
        real_deactivate = so.deactivate_scope

        def _deactivate_and_recreate(scope: str) -> None:
            real_deactivate(scope)
            del state._slots["crew-slot"]
            state.get_or_create_slot("crew-slot")._trust = True

        assert so.activate_scoped(_SCOPE, source="test", ttl=600).active
        slot._trust_scope = _SCOPE
        try:
            with (
                patch.object(so, "deactivate_scope", _deactivate_and_recreate),
                patch("kiro_crew.dashboard.chat_handlers.sel"),
            ):
                resp = await _choose_mode(state, {"mode": "normal", "slot": "crew-slot"})
            assert resp.status == 200
            assert channel.trusted is True
            channel._save.assert_not_called()
        finally:
            so.deactivate_scope(_SCOPE)

    @pytest.mark.parametrize("body", [{"mode": "normal", "slot": "crew-slot"}, {"mode": "normal"}])
    @pytest.mark.asyncio
    async def test_a_trust_landing_while_the_grant_ends_is_still_revoked(
        self, tmp_path, body
    ) -> None:
        """Flags and stored policy are cleared together, after every await."""
        state = _dashboard_state(tmp_path)
        slot = state.get_or_create_slot("crew-slot")
        so = safety_override()
        real_deactivate = so.deactivate_scope

        def _deactivate_while_trust_lands(scope: str) -> None:
            real_deactivate(scope)
            slot._trust = True

        assert so.activate_scoped(_SCOPE, source="test", ttl=600).active
        slot._trust_scope = _SCOPE
        try:
            with (
                patch.object(so, "deactivate_scope", _deactivate_while_trust_lands),
                patch("kiro_crew.dashboard.chat_handlers.sel"),
            ):
                resp = await _choose_mode(state, body)
            assert resp.status == 200
            assert slot._trust is False
            assert slot._trust_scope == ""
            assert so.scope_remaining_secs(_SCOPE) == 0
        finally:
            so.deactivate_scope(_SCOPE)
