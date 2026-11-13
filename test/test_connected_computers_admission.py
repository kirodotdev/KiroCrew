"""Final connector admission observes a disable sweep before attaching."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp import web

from kiro_crew.config import live
from kiro_crew.connected_computers import broker as broker_mod
from kiro_crew.connected_computers import disable, ws_route
from kiro_crew.connected_computers.authenticated import SessionFrames
from kiro_crew.connected_computers.broker import ConnectionBroker, DeviceConnection
from kiro_crew.connected_computers.connector_token import CredentialRejected
from kiro_crew.connected_computers.devices import STATUS_ACTIVE, DeviceRecord, DeviceRegistry
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS, LoopHop

pytestmark = pytest.mark.asyncio


@pytest.fixture
def admission(monkeypatch):
    cfg = SimpleNamespace(dashboard=SimpleNamespace(feature_previews={"connected_computers": True}))
    monkeypatch.setattr(live, "snapshot", lambda: cfg)
    monkeypatch.setattr("kiro_crew.session_environment.session_environments_enabled", lambda: True)
    record = DeviceRecord(
        device_id="laptop",
        label="Laptop",
        owner="me",
        fingerprint="a" * 64,
        cgen=1,
        status=STATUS_ACTIVE,
        created=1.0,
        updated=1.0,
    )
    broker = ConnectionBroker()
    monkeypatch.setattr(broker_mod, "_BROKER", broker)
    monkeypatch.setattr(ws_route, "verify_standing", lambda *args, **kwargs: record)
    bound = ("gateway", record.device_id, "b" * 64, "c" * 64)
    frames = SessionFrames("d" * 64, bound, gateway=True)
    peer = SessionFrames("d" * 64, bound, gateway=False)
    socket = Mock(spec=web.WebSocketResponse)
    socket.send_str = AsyncMock()
    registry = Mock(spec=DeviceRegistry)
    conn = DeviceConnection(record, socket.send_str, root="/work")

    async def admit():
        return await asyncio.wait_for(
            ws_route._admit(
                broker,
                record.device_id,
                "test-token",
                "gateway",
                registry,
                socket,
                frames,
                lambda current: conn,
            ),
            FALLBACK_TIMEOUT_SECS,
        )

    return SimpleNamespace(
        cfg=cfg,
        record=record,
        broker=broker,
        peer=peer,
        socket=socket,
        conn=conn,
        admit=admit,
    )


@pytest.mark.parametrize("disable_at", ["ready", "standing"])
async def test_a_completed_disable_sweep_prevents_late_admission(
    admission, monkeypatch, disable_at
):
    hop = LoopHop(asyncio.get_running_loop())
    swept = []

    async def turn_off():
        admission.cfg.dashboard.feature_previews["connected_computers"] = False
        await asyncio.wait_for(disable.turn_off(), FALLBACK_TIMEOUT_SECS)
        swept.append(True)

    if disable_at == "ready":
        # Ready has arrived, but the connection has not entered final admission.
        await asyncio.wait_for(turn_off(), FALLBACK_TIMEOUT_SECS)
    else:

        def standing(*args, **kwargs):
            # The credential worker returns an active record AFTER the sweep.
            hop.run(turn_off(), "disable sweep during standing lookup")
            return admission.record

        monkeypatch.setattr(ws_route, "verify_standing", standing)

    result = await admission.admit()
    hop.check()
    assert swept == [True], "the disable sweep must finish before admission resumes"
    assert isinstance(result, CredentialRejected)
    assert result.code == "connected_computers_disabled"
    assert admission.broker.live("laptop") is None
    admission.socket.send_str.assert_not_awaited()


async def test_final_admission_requires_a_live_snapshot(admission, monkeypatch):
    monkeypatch.setattr(live, "snapshot", lambda: None)
    result = await admission.admit()
    assert isinstance(result, CredentialRejected)
    assert result.code == "connected_computers_disabled"
    assert admission.broker.live("laptop") is None
    admission.socket.send_str.assert_not_awaited()


async def test_enabled_preview_admits_and_authenticates_the_answer(admission):
    result = await admission.admit()
    assert result is admission.conn
    assert admission.broker.live("laptop") is result
    admission.socket.send_str.assert_awaited_once()
    text = admission.socket.send_str.await_args.args[0]
    assert admission.peer.decode(text, allowed=frozenset({"admitted"})) == {"op": "admitted"}
    admission.broker.detach(result, "test complete")


@pytest.mark.parametrize("code", ["revoked", "removed"])
async def test_standing_rejection_still_prevents_admission(admission, monkeypatch, code):
    def standing(*args, **kwargs):
        raise CredentialRejected(code, "access ended")

    monkeypatch.setattr(ws_route, "verify_standing", standing)
    result = await admission.admit()
    assert isinstance(result, CredentialRejected)
    assert result.code == code
    assert admission.broker.live("laptop") is None
    admission.socket.send_str.assert_not_awaited()
