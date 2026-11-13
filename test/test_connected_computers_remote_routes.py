"""Connected computers: the dashboard's device list, Disconnect and Remove routes.

Served on a loopback test server (port 0) from ``register_all`` with a real
device registry in a temporary vault. The owner gate is replaced at the module
the handlers import it from; everything else is the product's own code.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.config import live
from kiro_crew.connected_computers import broker as broker_mod
from kiro_crew.connected_computers import flag, protocol, remote_routes, vault_doc
from kiro_crew.connected_computers.broker import ConnectionBroker, DeviceConnection
from kiro_crew.connected_computers.devices import STATUS_REMOVED, STATUS_REVOKED, DeviceRegistry
from kiro_crew.connected_computers.identity import install_fingerprint
from kiro_crew.secrets.vault import SecretVault
from kiro_crew.testing.wait import finished

pytestmark = pytest.mark.asyncio

DEVICES = remote_routes.DEVICES_PATH


def _fp(seed: int) -> str:
    return install_fingerprint(bytes([seed]) * 32)


@pytest.fixture
def gw(tmp_path, monkeypatch):
    previews: dict[str, Any] = {flag.PREVIEW_NAME: True}
    cfg = SimpleNamespace(dashboard=SimpleNamespace(feature_previews=previews))
    monkeypatch.setattr(live, "snapshot", lambda: cfg)
    monkeypatch.setattr("kiro_crew.session_environment.session_environments_enabled", lambda: True)
    vault = SecretVault(tmp_path / "vault-home")
    monkeypatch.setattr(vault_doc, "open_vault", lambda: vault)
    broker = ConnectionBroker()
    monkeypatch.setattr(broker_mod, "_BROKER", broker)
    # Recorded here, never written to the real event log.
    audits: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "kiro_crew.sel.sel", lambda: SimpleNamespace(log_api_access=lambda **kw: audits.append(kw))
    )
    state = SimpleNamespace(
        previews=previews,
        registry=DeviceRegistry(vault),
        broker=broker,
        operations=[],
        owner_refusal=None,
        audits=audits,
    )

    async def owner(request: web.Request, operation: str) -> Any:
        state.operations.append(operation)
        return state.owner_refusal

    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers._shared.require_owner_dashboard_request", owner
    )
    return state


def _app() -> web.Application:
    app = web.Application()
    remote_routes.register_all(app)
    return app


async def _call(method: str, path: str) -> tuple[int, dict[str, Any]]:
    async def go() -> tuple[int, dict[str, Any]]:
        async with TestClient(TestServer(_app())) as client:
            resp = await client.request(method, path)
            return resp.status, await resp.json()

    return await finished(go(), f"{method} {path}")


def _live(broker: ConnectionBroker, record: Any, sent: list[dict[str, Any]]) -> DeviceConnection:
    async def sender(text: str) -> None:
        sent.append(json.loads(text))

    conn = DeviceConnection(record, sender, root="/work", confinement="landlock")
    broker.attach(conn)
    return conn


async def _enroll(gw: Any, seed: int, label: str = "Laptop") -> Any:
    # Vault reads and writes run off the loop, as the routes run them.
    return await asyncio.to_thread(
        gw.registry.enroll, base="laptop", fingerprint=_fp(seed), label=label, owner="me"
    )


async def _stored(gw: Any, device_id: str) -> Any:
    return await asyncio.to_thread(gw.registry.get, device_id)


class TestDeviceList:
    async def test_refused_while_the_preview_is_off(self, gw):
        gw.previews.clear()
        status, body = await _call("GET", DEVICES)
        assert status == 403
        assert body == {"error": flag.DISABLED_REASON, "code": "connected_computers_disabled"}

    async def test_the_owner_gate_answers_first(self, gw):
        gw.owner_refusal = web.json_response({"error": "owner only"}, status=401)
        status, body = await _call("GET", DEVICES)
        assert (status, body) == (401, {"error": "owner only"})
        assert gw.operations == ["connected_computers.devices"]

    async def test_no_computers(self, gw):
        assert await _call("GET", DEVICES) == (200, {"devices": []})

    async def test_rows_carry_live_state_and_shared_labels(self, gw):
        first = await _enroll(gw, 1)
        second = await _enroll(gw, 2)
        third = await _enroll(gw, 3, label="Desktop")
        sent: list[dict[str, Any]] = []
        conn = _live(gw.broker, first, sent)
        status, body = await _call("GET", DEVICES)
        assert status == 200
        rows = {row["device_id"]: row for row in body["devices"]}
        assert set(rows) == {first.device_id, second.device_id, third.device_id}
        assert rows[first.device_id]["connected"] is True
        assert rows[first.device_id]["root"] == "/work"
        assert rows[first.device_id]["confinement"] == "landlock"
        assert rows[second.device_id]["connected"] is False
        assert rows[second.device_id]["root"] == "" and rows[second.device_id]["confinement"] == ""
        assert rows[first.device_id]["label_is_shared"] is True
        assert rows[second.device_id]["label_is_shared"] is True
        assert rows[third.device_id]["label_is_shared"] is False
        assert all("fingerprint" not in row for row in rows.values())
        gw.broker.detach(conn, "test complete")


class TestDisconnectAndRemove:
    @pytest.mark.parametrize("action", ["revoke", "remove"])
    async def test_an_unknown_computer_is_404(self, gw, action):
        status, body = await _call("POST", f"{DEVICES}/ghost/{action}")
        assert (status, body["code"]) == (404, "unknown_device")

    async def test_a_malformed_device_id_is_404(self, gw):
        status, body = await _call("POST", f"{DEVICES}/Not-An-Id!/revoke")
        assert (status, body["code"]) == (404, "unknown_device")

    @pytest.mark.parametrize(
        "action,status_after",
        [("revoke", STATUS_REVOKED), ("remove", STATUS_REMOVED)],
    )
    async def test_ending_a_live_computer_refuses_it_by_name(self, gw, action, status_after):
        rec = await _enroll(gw, 1)
        sent: list[dict[str, Any]] = []
        conn = _live(gw.broker, rec, sent)
        status, body = await _call("POST", f"{DEVICES}/{rec.device_id}/{action}")
        assert status == 200
        assert body["device"]["device_id"] == rec.device_id
        assert body["device"]["status"] == status_after
        assert sent == [{"op": "refusal", "code": status_after}]
        assert conn.alive is False and gw.broker.live(rec.device_id) is None
        assert (await _stored(gw, rec.device_id)).status == status_after
        assert gw.operations == [f"connected_computers.{action}"]

    @pytest.mark.parametrize("action", ["revoke", "remove"])
    async def test_ending_a_computer_is_audited(self, gw, action):
        rec = await _enroll(gw, 1)
        status, _body = await _call("POST", f"{DEVICES}/{rec.device_id}/{action}")
        assert status == 200
        assert gw.audits == [
            {
                "caller": "dashboard",
                "operation": f"connected_computer_{action}",
                "outcome": "ok",
                "source": "connected_computers",
                "resources": rec.device_id,
            }
        ]

    @pytest.mark.parametrize("action", ["revoke", "remove"])
    async def test_a_failed_audit_does_not_undo_the_end(self, gw, action, monkeypatch):
        def broken() -> Any:
            raise OSError("event log unwritable")

        monkeypatch.setattr("kiro_crew.sel.sel", broken)
        rec = await _enroll(gw, 1)
        status, _body = await _call("POST", f"{DEVICES}/{rec.device_id}/{action}")
        assert status == 200
        assert not (await _stored(gw, rec.device_id)).active

    async def test_nothing_is_audited_when_nothing_ended(self, gw):
        rec = await _enroll(gw, 1)
        status, _body = await _call("POST", f"{DEVICES}/ghost/revoke")
        assert status == 404
        gw.owner_refusal = web.json_response({"error": "owner only"}, status=403)
        status, _body = await _call("POST", f"{DEVICES}/{rec.device_id}/remove")
        assert status == 403
        assert gw.audits == []

    async def test_disconnect_still_works_with_the_preview_off(self, gw):
        rec = await _enroll(gw, 1)
        gw.previews.clear()
        status, body = await _call("POST", f"{DEVICES}/{rec.device_id}/revoke")
        assert (status, body["device"]["status"]) == (200, STATUS_REVOKED)

    async def test_the_owner_gate_refuses_before_any_write(self, gw):
        rec = await _enroll(gw, 1)
        gw.owner_refusal = web.json_response({"error": "owner only"}, status=403)
        status, _body = await _call("POST", f"{DEVICES}/{rec.device_id}/remove")
        assert status == 403
        assert (await _stored(gw, rec.device_id)).active


async def test_the_lazy_connect_route_reaches_the_socket_handler(gw, monkeypatch):
    # Preview on, Session Environments off: the socket handler's own 503.
    monkeypatch.setattr("kiro_crew.session_environment.session_environments_enabled", lambda: False)
    status, body = await _call("GET", "/api/connected-computers/connect")
    assert status == protocol.REMOTE_OFF_STATUS
    assert body["code"] == "connected_computers_disabled"


async def _raw(app: web.Application, method: str, path: str) -> tuple[int, bytes, str, str]:
    async def go() -> tuple[int, bytes, str, str]:
        async with TestClient(TestServer(app)) as client:
            resp = await client.request(method, path)
            body = await resp.read()
            return resp.status, body, resp.content_type, resp.headers.get("Allow", "")

    return await finished(go(), f"{method} {path}")


@pytest.mark.parametrize("method", ["GET", "HEAD", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
async def test_with_the_preview_off_connect_answers_as_an_unserved_path(gw, method):
    # The router's answer for a path no route serves, with this gateway's
    # routes registered (so the prefix is shared) and the preview off.
    gw.previews.clear()
    unserved = await _raw(_app(), method, "/api/connected-computers/not-a-route")
    assert unserved[0] == 404
    # Main: no route at this path at all.
    assert await _raw(web.Application(), method, "/api/connected-computers/connect") == unserved
    assert await _raw(_app(), method, "/api/connected-computers/connect") == unserved


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE"])
async def test_with_the_preview_on_connect_takes_get_and_head_only(gw, method):
    status, _body, _ctype, allow = await _raw(_app(), method, "/api/connected-computers/connect")
    assert (status, allow) == (405, "GET,HEAD")


async def test_without_a_live_configuration_connect_is_unserved(gw, monkeypatch):
    monkeypatch.setattr(live, "snapshot", lambda: None)
    status, *_ = await _raw(_app(), "GET", "/api/connected-computers/connect")
    assert status == 404
