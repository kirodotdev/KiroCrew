"""Tests for the Microsoft Teams transport layer.

Covers the declared capabilities, import purity (messaging must not import
teams), deny-by-default authorization, direct/personal-only scope gating,
unresolved-identity fail-closed, and inbound normalization -> dispatch.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.messaging.transport import InboundMessage
from kiro_crew.teams.client import TeamsInbound
from kiro_crew.teams.transport import TEAMS_CAPABILITIES, TeamsTransport


class _FakeSel:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def log_api_access(self, **kwargs: Any) -> None:
        self.events.append(kwargs)


class _FakeClient:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    async def send_message(self, conversation_id: str, content: str, service_url: str):
        self.sent.append((conversation_id, content, service_url))
        return "mid-1"


def _dm(text: str = "hi", email: str = "alice@example.com", aad: str = "aad-1") -> TeamsInbound:
    return TeamsInbound(
        conversation_id="conv-1",
        conversation_type="personal",
        service_url="https://smba.example.com/",
        text=text,
        user_email=email,
        aad_object_id=aad,
        activity_id="act-1",
    )


class TestCapabilities:
    def test_teams_shape(self) -> None:
        assert TEAMS_CAPABILITIES.streaming is False
        assert TEAMS_CAPABILITIES.max_buttons == 0
        assert TEAMS_CAPABILITIES.supports_proactive_send is True
        assert TEAMS_CAPABILITIES.max_message_chars > 0


class TestImportPurity:
    def test_messaging_does_not_import_teams(self) -> None:
        """The neutral messaging package must never import the teams channel."""
        import kiro_crew.messaging as messaging_pkg

        pkg_dir = Path(messaging_pkg.__file__).parent
        offenders: list[str] = []
        for py in pkg_dir.rglob("*.py"):
            tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.startswith("kiro_crew.teams"):
                            offenders.append(f"{py.name}: import {alias.name}")
                elif isinstance(node, ast.ImportFrom):
                    mod = node.module or ""
                    if mod.startswith("kiro_crew.teams"):
                        offenders.append(f"{py.name}: from {mod} import ...")
        assert not offenders, f"messaging imports teams: {offenders}"


class TestAuthorize:
    def test_allow_list_permits_member(self) -> None:
        t = TeamsTransport(_FakeClient(), allowed_emails=["Alice@example.com"])
        msg = InboundMessage(
            channel_type="teams", user_id="alice@example.com", conversation_id="c", text="x"
        )
        assert t.authorize(msg) is True

    def test_deny_by_default_and_audit(self, monkeypatch) -> None:
        fake = _FakeSel()
        monkeypatch.setattr("kiro_crew.teams.transport.sel", lambda: fake)
        t = TeamsTransport(_FakeClient(), allowed_emails=["alice@example.com"])
        msg = InboundMessage(
            channel_type="teams", user_id="mallory@evil.com", conversation_id="c", text="x"
        )
        assert t.authorize(msg) is False
        assert any(e["outcome"] == "denied" for e in fake.events)

    def test_empty_allow_list_denies_everyone(self) -> None:
        t = TeamsTransport(_FakeClient(), allowed_emails=[])
        msg = InboundMessage(
            channel_type="teams", user_id="alice@example.com", conversation_id="c", text="x"
        )
        assert t.authorize(msg) is False


class TestReceive:
    @pytest.mark.asyncio
    async def test_personal_message_dispatches(self, monkeypatch) -> None:
        dispatched: list[TeamsInbound] = []

        async def _dispatch(inb: TeamsInbound) -> None:
            dispatched.append(inb)

        t = TeamsTransport(
            _FakeClient(), allowed_emails=["alice@example.com"], dispatch=_dispatch
        )
        await t.receive(_dm())
        assert len(dispatched) == 1
        # serviceUrl learned for the conversation
        assert t.service_url_for("conv-1") == "https://smba.example.com/"

    @pytest.mark.asyncio
    async def test_channel_scope_denied_and_audited(self, monkeypatch) -> None:
        fake = _FakeSel()
        monkeypatch.setattr("kiro_crew.teams.transport.sel", lambda: fake)
        dispatched: list[TeamsInbound] = []

        async def _dispatch(inb: TeamsInbound) -> None:
            dispatched.append(inb)

        t = TeamsTransport(
            _FakeClient(), allowed_emails=["alice@example.com"], dispatch=_dispatch
        )
        inb = _dm()
        inb.conversation_type = "channel"
        await t.receive(inb)
        assert dispatched == []
        assert any(e["outcome"] == "denied_non_personal_scope" for e in fake.events)

    @pytest.mark.asyncio
    async def test_unresolved_identity_fails_closed(self, monkeypatch) -> None:
        fake = _FakeSel()
        monkeypatch.setattr("kiro_crew.teams.transport.sel", lambda: fake)
        dispatched: list[TeamsInbound] = []

        async def _dispatch(inb: TeamsInbound) -> None:
            dispatched.append(inb)

        t = TeamsTransport(
            _FakeClient(), allowed_emails=["alice@example.com"], dispatch=_dispatch
        )
        # Neither email nor AAD object id -> unresolved -> denied.
        await t.receive(_dm(email="", aad=""))
        assert dispatched == []
        assert any(e["outcome"] == "denied_unresolved_identity" for e in fake.events)

    @pytest.mark.asyncio
    async def test_aad_object_id_fallback_authorized(self) -> None:
        # Email absent (typical Teams activity) but the AAD object id is on the
        # allow-list -> dispatches (feature works out of the box).
        dispatched: list[TeamsInbound] = []

        async def _dispatch(inb: TeamsInbound) -> None:
            dispatched.append(inb)

        t = TeamsTransport(_FakeClient(), allowed_emails=["aad-42"], dispatch=_dispatch)
        await t.receive(_dm(email="", aad="aad-42"))
        assert len(dispatched) == 1
        # ...and an unlisted object id is denied.
        dispatched.clear()
        await t.receive(_dm(email="", aad="aad-99"))
        assert dispatched == []

    @pytest.mark.asyncio
    async def test_empty_text_no_turn(self) -> None:
        dispatched: list[TeamsInbound] = []

        async def _dispatch(inb: TeamsInbound) -> None:
            dispatched.append(inb)

        t = TeamsTransport(
            _FakeClient(), allowed_emails=["alice@example.com"], dispatch=_dispatch
        )
        await t.receive(_dm(text=""))
        assert dispatched == []

    @pytest.mark.asyncio
    async def test_non_teams_envelope_dropped(self) -> None:
        t = TeamsTransport(_FakeClient(), allowed_emails=["alice@example.com"])
        # Should not raise
        await t.receive({"not": "a TeamsInbound"})

    @pytest.mark.asyncio
    async def test_non_allowed_email_denied(self, monkeypatch) -> None:
        dispatched: list[TeamsInbound] = []

        async def _dispatch(inb: TeamsInbound) -> None:
            dispatched.append(inb)

        t = TeamsTransport(
            _FakeClient(), allowed_emails=["alice@example.com"], dispatch=_dispatch
        )
        await t.receive(_dm(email="mallory@evil.com"))
        assert dispatched == []
