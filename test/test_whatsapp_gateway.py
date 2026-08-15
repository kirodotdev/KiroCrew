"""WhatsApp gateway startup tests: enable gate, missing-extra, wiring, errors.

``maybe_start_whatsapp`` is the only public surface; it is driven with a
SimpleNamespace orchestrator and monkeypatched module symbols so neonize is
never imported and no real socket is opened.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import kiro_crew.whatsapp.gateway as gw
from kiro_crew.messaging.driver import APPROVAL_AUTO, APPROVAL_INTERACTIVE
from kiro_crew.whatsapp.gateway import _resolve_approval_mode, maybe_start_whatsapp


class FakeState:
    def __init__(self) -> None:
        self.whatsapp_connected: bool | None = None
        self.whatsapp_connect_error: str | None = None
        self.registered: list[Any] = []

    def register_channel_transport(self, transport: Any) -> None:
        self.registered.append(transport)


def _cfg(**wa):
    whatsapp = SimpleNamespace(
        db_path=wa.get("db_path", ""),
        dm_policy=wa.get("dm_policy", "self"),
        allowed_wa_ids=wa.get("allowed_wa_ids", []),
        groups=wa.get("groups", []),
    )
    agent = SimpleNamespace(default_agent="kirocrew", approval_mode="auto")
    messaging = SimpleNamespace(idle_reset_minutes=0, daily_reset_hour=-1, dm_scope="user")
    return SimpleNamespace(whatsapp=whatsapp, agent=agent, messaging=messaging)


def _orch(*, enabled=True, state=None, approval_mode=None):
    return SimpleNamespace(
        _whatsapp_enabled=enabled,
        dashboard_state=state,
        sessions=SimpleNamespace(),
        ctx_builder=SimpleNamespace(),
        _cfg=_cfg(),
        _approval_mode=approval_mode,
        conv_log="log-sentinel",
    )


# ── approval mode resolution ────────────────────────────────────────────────
def test_resolve_approval_mode_yolo_is_auto():
    orch = SimpleNamespace(_approval_mode="yolo", _cfg=_cfg())
    assert _resolve_approval_mode(orch) == APPROVAL_AUTO


def test_resolve_approval_mode_explicit_auto():
    orch = SimpleNamespace(_approval_mode="auto", _cfg=_cfg())
    assert _resolve_approval_mode(orch) == APPROVAL_AUTO


def test_resolve_approval_mode_falls_back_to_cfg_interactive():
    cfg = _cfg()
    cfg.agent.approval_mode = "interactive"
    orch = SimpleNamespace(_approval_mode=None, _cfg=cfg)
    assert _resolve_approval_mode(orch) == APPROVAL_INTERACTIVE


# ── enable gate ─────────────────────────────────────────────────────────────
def test_disabled_channel_is_a_noop():
    orch = _orch(enabled=False)
    assert asyncio.run(maybe_start_whatsapp(orch)) is None


def test_missing_enabled_attr_defaults_off():
    orch = SimpleNamespace()  # no _whatsapp_enabled
    assert asyncio.run(maybe_start_whatsapp(orch)) is None


# ── missing optional extra ──────────────────────────────────────────────────
def test_missing_extra_reports_error_and_returns_none(monkeypatch):
    monkeypatch.setattr(gw, "neonize_available", lambda: False)
    state = FakeState()
    orch = _orch(state=state)
    assert asyncio.run(maybe_start_whatsapp(orch)) is None
    assert state.whatsapp_connected is False
    assert state.whatsapp_connect_error  # hint recorded


def test_missing_extra_without_state_is_still_a_noop(monkeypatch):
    monkeypatch.setattr(gw, "neonize_available", lambda: False)
    orch = _orch(state=None)
    assert asyncio.run(maybe_start_whatsapp(orch)) is None


# ── happy path wiring ───────────────────────────────────────────────────────
def _patch_success(monkeypatch):
    """Stub client + transport so connect() never touches neonize."""
    monkeypatch.setattr(gw, "neonize_available", lambda: True)
    monkeypatch.setattr(gw, "default_db_path", lambda home: "/tmp/wa/session.db")

    created: dict[str, Any] = {}

    class StubClient:
        def __init__(self, db_path):
            self.db_path = db_path
            self.on_state_change = None
            self.state = "connected"
            created["client"] = self

    class StubTransport:
        def __init__(self, client, dispatch, *, dm_policy, allowed_wa_ids, groups):
            self.client = client
            self.dispatch = dispatch
            self.dm_policy = dm_policy
            self.allowed_wa_ids = allowed_wa_ids
            self.groups = groups
            self.connected = False
            created["transport"] = self

        async def connect(self):
            self.connected = True

    monkeypatch.setattr(gw, "WhatsAppClient", StubClient)
    monkeypatch.setattr(gw, "WhatsAppTransport", StubTransport)
    return created


def test_start_wires_client_transport_dispatcher_and_connects(monkeypatch):
    created = _patch_success(monkeypatch)
    state = FakeState()
    orch = _orch(state=state)
    client = asyncio.run(maybe_start_whatsapp(orch))

    assert client is created["client"]
    transport = created["transport"]
    assert transport.connected is True
    assert transport.dm_policy == "self"
    assert state.registered == [transport]
    # A state observer was installed and toggles on "connected".
    assert callable(client.on_state_change)
    client.on_state_change("connected", "")
    assert state.whatsapp_connected is True
    client.on_state_change("logged_out", "unlinked")
    assert state.whatsapp_connected is False
    assert "logged_out" in (state.whatsapp_connect_error or "")


def test_start_honours_configured_db_path(monkeypatch):
    created = _patch_success(monkeypatch)
    orch = _orch(state=FakeState())
    orch._cfg.whatsapp.db_path = "/custom/wa.db"
    asyncio.run(maybe_start_whatsapp(orch))
    assert created["client"].db_path == "/custom/wa.db"


def test_start_without_state_still_connects(monkeypatch):
    created = _patch_success(monkeypatch)
    orch = _orch(state=None)
    client = asyncio.run(maybe_start_whatsapp(orch))
    assert client is created["client"]
    assert created["transport"].connected is True


def test_start_passes_allowed_ids_and_groups(monkeypatch):
    created = _patch_success(monkeypatch)
    orch = _orch(state=FakeState())
    orch._cfg.whatsapp.allowed_wa_ids = ["447700900000"]
    orch._cfg.whatsapp.groups = [{"jid": "g1@g.us", "mode": "rules"}]
    asyncio.run(maybe_start_whatsapp(orch))
    transport = created["transport"]
    assert transport.allowed_wa_ids == ["447700900000"]
    assert transport.groups == [{"jid": "g1@g.us", "mode": "rules"}]


# ── failure path ────────────────────────────────────────────────────────────
def test_connect_failure_is_swallowed_and_recorded(monkeypatch):
    monkeypatch.setattr(gw, "neonize_available", lambda: True)
    monkeypatch.setattr(gw, "default_db_path", lambda home: "/tmp/wa/session.db")

    class StubClient:
        def __init__(self, db_path):
            self.on_state_change = None

    class BoomTransport:
        def __init__(self, *a, **kw):
            pass

        async def connect(self):
            raise RuntimeError("pairing socket refused")

    monkeypatch.setattr(gw, "WhatsAppClient", StubClient)
    monkeypatch.setattr(gw, "WhatsAppTransport", BoomTransport)

    state = FakeState()
    orch = _orch(state=state)
    assert asyncio.run(maybe_start_whatsapp(orch)) is None
    assert state.whatsapp_connected is False
    assert "pairing socket refused" in (state.whatsapp_connect_error or "")


def test_connect_failure_without_state_returns_none(monkeypatch):
    monkeypatch.setattr(gw, "neonize_available", lambda: True)
    monkeypatch.setattr(gw, "default_db_path", lambda home: "/tmp/wa/session.db")

    class StubClient:
        def __init__(self, db_path):
            self.on_state_change = None

    class BoomTransport:
        def __init__(self, *a, **kw):
            pass

        async def connect(self):
            raise RuntimeError("boom")

    monkeypatch.setattr(gw, "WhatsAppClient", StubClient)
    monkeypatch.setattr(gw, "WhatsAppTransport", BoomTransport)
    orch = _orch(state=None)
    assert asyncio.run(maybe_start_whatsapp(orch)) is None
