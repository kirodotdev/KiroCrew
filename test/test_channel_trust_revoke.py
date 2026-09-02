"""A dashboard trust/YOLO grant on a channel-surfaced slot must be revocable.

A channel-born slot carries the CHANNEL session key in ``linked_session_key``
(``surface_channel_session``), and that key is what
``messaging.approval.TextApprovalDecider.trusted()`` reads to decide whether the
channel's next tool call skips its prompt. So every grant and every revoke has to
address the same key: a revoke written to ``dashboard:{slot.key}`` leaves the live
channel session auto-approving with no way to switch it off.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.state import DashboardState
from kiro_crew.history import ConversationLog

CHANNEL_KEY = "whatsapp:default:dm:15551234567"


def _policy_recorder() -> MagicMock:
    """A ``SessionManager`` stand-in whose approval policies are a REAL map.

    Everything else stays a mock (the endpoints touch a wide surface of it), but
    the policy map has to be real: the defect is which KEY a write lands on, and a
    mock that records calls without a keyed store cannot tell a grant from its
    revoke.
    """
    sessions = MagicMock(count=0)
    sessions.get_pid = MagicMock(return_value=None)
    sessions.remove = AsyncMock()
    policies: dict[str, str] = {}
    sessions.policies = policies
    sessions.set_approval_policy = lambda key, policy: policies.__setitem__(key, policy)
    sessions.get_approval_policy = lambda key: policies.get(key, "")
    return sessions


def _make_state(tmp_path) -> DashboardState:
    return DashboardState(
        sessions=_policy_recorder(),
        crons=MagicMock(list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})),
        lessons=MagicMock(load_all=MagicMock(return_value=[])),
        start_time=0.0,
        conversation_log=ConversationLog(base_dir=tmp_path),
    )


def _make_app(state: DashboardState) -> web.Application:
    from kiro_crew.dashboard.chat import api_chat_mode, api_chat_slot_approve

    @web.middleware
    async def _test_auth(request: web.Request, handler):
        if "app" not in request:
            request["app"] = ""
        if "user" not in request:
            request["user"] = "local-app"
        return await handler(request)

    app = web.Application(middlewares=[_test_auth])
    app["state"] = state
    app.router.add_post("/api/chat/mode", api_chat_mode)
    app.router.add_post("/api/chat/slots/{slot}/approve", api_chat_slot_approve)
    return app


def _surfaced_slot(state: DashboardState, name: str = "whatsapp_15551234567"):
    """A slot shaped like one ``surface_channel_session`` created."""
    slot = state.get_or_create_slot(name)
    slot.linked_session_key = CHANNEL_KEY
    return slot


@pytest.fixture(autouse=True)
def _pin_config_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)


class TestModeEndpointSymmetry:
    """``/api/chat/mode`` grants and revokes must land on the channel key."""

    @pytest.mark.asyncio
    async def test_slot_scoped_trust_grants_the_channel_session(self, tmp_path):
        state = _make_state(tmp_path)
        slot = _surfaced_slot(state)

        async with TestClient(TestServer(_make_app(state))) as client:
            await client.post("/api/chat/mode", json={"mode": "trust", "slot": slot.key})

        assert state.sessions.get_approval_policy(CHANNEL_KEY) == "auto"

    @pytest.mark.asyncio
    async def test_slot_scoped_normal_revokes_the_channel_session(self, tmp_path):
        state = _make_state(tmp_path)
        slot = _surfaced_slot(state)
        state.sessions.set_approval_policy(CHANNEL_KEY, "auto")
        slot._trust = True

        async with TestClient(TestServer(_make_app(state))) as client:
            await client.post("/api/chat/mode", json={"mode": "normal", "slot": slot.key})

        assert state.sessions.get_approval_policy(CHANNEL_KEY) == ""

    @pytest.mark.asyncio
    async def test_granting_one_slot_grants_every_slot_sharing_the_session(self, tmp_path):
        """The mirror of the revoke, and the reason propagation stays per slot.

        If a grant set only the selected slot, two slots addressing one session
        would disagree about it, and the propagation pass would be decided by slot
        iteration order rather than by what the operator asked for.
        """
        state = _make_state(tmp_path)
        addressed = _surfaced_slot(state, "whatsapp_15551234567")
        owner = _surfaced_slot(state, "whatsapp_owner_alias")
        assert owner.linked_session_key == addressed.linked_session_key

        async with TestClient(TestServer(_make_app(state))) as client:
            await client.post("/api/chat/mode", json={"mode": "trust", "slot": addressed.key})

        assert state.sessions.get_approval_policy(CHANNEL_KEY) == "auto"
        assert owner._trust is True, "the sharing slot disagrees about its own session"

    @pytest.mark.asyncio
    async def test_revoking_one_slot_revokes_every_slot_sharing_the_session(self, tmp_path):
        """Two slots can address ONE session, and the policy is per session.

        A rehydrated owner slot and the alias its turns run under both resolve to
        the same effective key. Revoking on one used to leave the other holding a
        stale `_trust`, and the propagation pass then rewrote the shared session
        back to "auto" from it, so the revoke was undone by the same request that
        performed it.
        """
        state = _make_state(tmp_path)
        addressed = _surfaced_slot(state, "whatsapp_15551234567")
        owner = _surfaced_slot(state, "whatsapp_owner_alias")
        assert owner.linked_session_key == addressed.linked_session_key
        state.sessions.set_approval_policy(CHANNEL_KEY, "auto")
        addressed._trust = True
        owner._trust = True

        async with TestClient(TestServer(_make_app(state))) as client:
            await client.post("/api/chat/mode", json={"mode": "normal", "slot": addressed.key})

        assert state.sessions.get_approval_policy(CHANNEL_KEY) == ""
        assert owner._trust is False, "the sharing slot kept a grant the operator revoked"

    @pytest.mark.asyncio
    async def test_global_normal_revokes_the_channel_session(self, tmp_path):
        state = _make_state(tmp_path)
        slot = _surfaced_slot(state)
        state.sessions.set_approval_policy(CHANNEL_KEY, "auto")
        slot._trust = True

        async with TestClient(TestServer(_make_app(state))) as client:
            await client.post("/api/chat/mode", json={"mode": "normal"})

        assert state.sessions.get_approval_policy(CHANNEL_KEY) == ""

    @pytest.mark.asyncio
    async def test_trust_reads_revokes_the_channel_session(self, tmp_path):
        state = _make_state(tmp_path)
        slot = _surfaced_slot(state)
        state.sessions.set_approval_policy(CHANNEL_KEY, "auto")
        slot._trust = True

        async with TestClient(TestServer(_make_app(state))) as client:
            await client.post("/api/chat/mode", json={"mode": "trust_reads", "slot": slot.key})

        assert state.sessions.get_approval_policy(CHANNEL_KEY) == ""

    @pytest.mark.asyncio
    async def test_normal_is_the_off_switch_for_a_slot_it_was_not_scoped_to(self, tmp_path):
        """``normal`` clears every slot's effective key, not just the named one.

        A grant can arrive on the channel session without the dashboard slot
        knowing (the channel's own Trust reply calls
        ``TextApprovalDecider._grant_session_trust``, which writes the session
        policy and no slot flag). ``normal`` is documented as the off-switch at any
        scope, and the trailing propagation loop is the only path that reaches such
        a slot, so it has to address the effective key too.
        """
        state = _make_state(tmp_path)
        state.get_or_create_slot("s1")
        untouched = _surfaced_slot(state)
        # Granted channel-side: the session policy is set, the slot flag is not.
        state.sessions.set_approval_policy(CHANNEL_KEY, "auto")

        async with TestClient(TestServer(_make_app(state))) as client:
            await client.post("/api/chat/mode", json={"mode": "normal", "slot": "s1"})

        assert untouched._trust is False
        assert state.sessions.get_approval_policy(CHANNEL_KEY) == ""

    @pytest.mark.asyncio
    async def test_plain_dashboard_slot_keeps_its_dashboard_key(self, tmp_path):
        """An unlinked slot still resolves to ``dashboard:{key}`` — no regression."""
        state = _make_state(tmp_path)
        state.get_or_create_slot("s1")

        async with TestClient(TestServer(_make_app(state))) as client:
            await client.post("/api/chat/mode", json={"mode": "trust", "slot": "s1"})
            assert state.sessions.get_approval_policy("dashboard:s1") == "auto"
            await client.post("/api/chat/mode", json={"mode": "normal", "slot": "s1"})
            assert state.sessions.get_approval_policy("dashboard:s1") == ""


class TestOneDerivationForGrantAndRevoke:
    """Pin the SHAPE, because behaviour alone cannot pin symmetry.

    ``api_chat_mode``'s trailing propagation loop rewrites every slot's policy, so
    it masks a per-mode branch that still keys by slot name: the branch write is
    defense-in-depth and a behaviour test cannot see it drift. An asymmetry written
    twice drifts again, so what is pinned is that every approval-policy write in
    these paths goes through the one shared derivation.

    The TTL expiry handler is included deliberately: it is the third writer and the
    worst one to get wrong, because the operator was told the grant was
    time-bounded. Keying it by slot name means the TTL clears something nothing on
    the channel path reads, so the grant it handed out never expires at all.
    """

    def _handler_sources(self) -> str:
        import inspect

        from kiro_crew.dashboard import chat_handlers, server

        # The expiry handler is nested inside `setup_routes`, so its own source is
        # not reachable by name; the enclosing function is sliced to it instead.
        setup_src = inspect.getsource(server.start_dashboard)
        start = setup_src.index("def _on_override_expired(")
        expiry_src = setup_src[start : setup_src.index("safety_override().on_expired", start)]
        return (
            inspect.getsource(chat_handlers.api_chat_mode)
            + inspect.getsource(chat_handlers.api_chat_slot_approve)
            + expiry_src
        )

    def test_every_policy_write_uses_the_shared_derivation(self):
        import re

        # Whitespace-stripped so a call broken across lines by the formatter reads
        # the same as a one-liner; black's wrapping must not open a hole here.
        src = self._handler_sources()
        compact = re.sub(r"\s+", "", src)

        # A write may pass the derivation inline OR a local the same source
        # assigned from it, because the propagation loop has to aggregate per KEY
        # before writing (two slots can resolve to one session, so writing inside
        # the loop lets iteration order pick the winner). What must never appear is
        # a key from anywhere else, so the allowed names are collected from actual
        # `<name> = effective_session_key(...)` assignments rather than listed.
        derived = set(re.findall(r"(\w+)\s*=\s*effective_session_key\(", src))
        derived |= {name for name, _ in re.findall(r"for\s+(\w+),\s*(\w+)\s+in\s+_granted", src)}
        allowed = tuple(f"{n}," for n in derived) + ("effective_session_key(",)
        writes = compact.split("set_approval_policy(")[1:]
        assert writes, "the approval-policy writes moved; retarget this guard"
        for tail in writes:
            assert tail.startswith(allowed), (
                "an approval-policy write uses a key that is not the shared "
                f"derivation nor a local assigned from it: set_approval_policy({tail[:60]}"
            )
        assert "linked_session_keyor_history_key_for" not in compact, (
            "the effective-key derivation is inlined again instead of shared; "
            "that is the asymmetry that made a revoke miss a grant"
        )


class TestApprovalCardGrantIsRevocable:
    """The card's Trust/YOLO grants and ``/mode normal`` must agree on the key."""

    @pytest.mark.asyncio
    async def test_card_trust_then_normal_clears_the_channel_session(self, tmp_path):
        import asyncio

        state = _make_state(tmp_path)
        slot = _surfaced_slot(state)
        fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        slot._approval_futures["req-1"] = fut
        slot.messages.append(
            {
                "role": "permission",
                "content": "Running: ls",
                "cls": json.dumps(
                    {
                        "request_id": "req-1",
                        "full_command": "ls",
                        "trust_grantable": "1",
                    }
                ),
            }
        )

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                f"/api/chat/slots/{slot.key}/approve",
                json={"action": "trust", "request_id": "req-1"},
            )
            assert (await resp.json())["ok"] is True
            assert state.sessions.get_approval_policy(CHANNEL_KEY) == "auto"

            await client.post("/api/chat/mode", json={"mode": "normal"})

        assert state.sessions.get_approval_policy(CHANNEL_KEY) == ""

    @pytest.mark.asyncio
    async def test_card_yolo_then_normal_clears_the_channel_session(self, tmp_path):
        import asyncio

        state = _make_state(tmp_path)
        slot = _surfaced_slot(state)
        fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        slot._approval_futures["req-1"] = fut

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                f"/api/chat/slots/{slot.key}/approve",
                json={"action": "yolo", "request_id": "req-1"},
            )
            assert (await resp.json())["ok"] is True
            assert state.sessions.get_approval_policy(CHANNEL_KEY) == "auto"

            await client.post("/api/chat/mode", json={"mode": "normal"})

        assert state.sessions.get_approval_policy(CHANNEL_KEY) == ""
