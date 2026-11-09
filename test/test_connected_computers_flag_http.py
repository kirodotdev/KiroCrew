"""Connected computers: crafted HTTP and CLI requests while the switch is off.

The switch is read the way the gateway reads it (``config.live.snapshot()``),
not by patching the predicate, so these pin what a crafted request meets:

* every route that enrolls, connects, lists or consents refuses with the typed
  ``connected_computers_disabled`` code before it reads any state;
* the chat environment picker offers no computer, so a crafted choice is
  refused as an unknown profile;
* the routes that only narrow access (revoke, remove, local Off, the grant list)
  still work while it is off;
* the CLI siblings refuse too.

Authored for CI; this host runs no tests locally.
"""

from __future__ import annotations

import io
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.connected_computers import flag, local_routes, protocol, remote_routes, ws_route
from kiro_crew.connected_computers.devices import DeviceRegistry
from kiro_crew.connected_computers.identity import install_fingerprint
from kiro_crew.secrets.vault import SecretVault

pytestmark = pytest.mark.asyncio

DISABLED = "connected_computers_disabled"


def _explode(*_a: Any, **_kw: Any) -> Any:
    raise AssertionError("no state may be read while the feature is off")


def _config(on: bool) -> Any:
    previews = {flag.PREVIEW_NAME: True} if on else {}
    return SimpleNamespace(dashboard=SimpleNamespace(feature_previews=previews))


@pytest.fixture
def switch(monkeypatch):
    """Sets the live config the flag reads; the developer gate stays on."""
    from kiro_crew.config import live
    from kiro_crew.session_environment import DEVELOPER_FLAG

    monkeypatch.setenv(DEVELOPER_FLAG, "1")
    state = {"on": False}
    monkeypatch.setattr(live, "snapshot", lambda: _config(state["on"]))
    return state


@web.middleware
async def _owner(request, handler):
    request["user"] = "local-app"
    request["app"] = ""
    request.app["state"].owner_id = ""
    if request.headers.get("X-Test-Internal"):
        request["internal_auth"] = True
    return await handler(request)


@pytest_asyncio.fixture
async def client(switch):
    local_routes.clear_consents()
    app = web.Application(middlewares=[_owner])
    app["state"] = MagicMock()
    remote_routes.register_all(app)
    async with TestClient(TestServer(app)) as c:
        yield c


async def _refused(resp) -> None:
    assert resp.status == 403, await resp.text()
    assert (await resp.json())["code"] == DISABLED


async def test_flag_crafted_device_list_is_refused_while_off(client, monkeypatch):
    monkeypatch.setattr(remote_routes, "_registry", _explode)
    await _refused(await client.get(remote_routes.DEVICES_PATH))


async def test_flag_crafted_connector_connect_is_refused_before_any_read(client, monkeypatch):
    monkeypatch.setattr(ws_route, "_registry", _explode)
    resp = await client.get(
        ws_route.CONNECT_PATH,
        headers={protocol.DEVICE_HEADER: "laptop", protocol.NONCE_HEADER: "a" * 64},
    )
    # 503, not 403: the gateway is off, the computer is not refused, so its
    # local grant waits instead of ending (controller.REMOTE_OFF_STATUS).
    assert resp.status == 503, await resp.text()
    body = await resp.json()
    assert body["code"] == DISABLED
    assert body["error"] == flag.DISABLED_REASON


async def test_flag_connect_without_the_developer_gate_names_it(client, switch, monkeypatch):
    from kiro_crew.session_environment import DEVELOPER_FLAG

    switch["on"] = True
    monkeypatch.delenv(DEVELOPER_FLAG, raising=False)
    monkeypatch.setattr(ws_route, "_registry", _explode)
    resp = await client.get(
        ws_route.CONNECT_PATH,
        headers={protocol.DEVICE_HEADER: "laptop", protocol.NONCE_HEADER: "a" * 64},
    )
    assert resp.status == 503, await resp.text()
    body = await resp.json()
    assert body["code"] == DISABLED
    assert DEVELOPER_FLAG in body["error"]


async def test_flag_crafted_consent_is_refused_while_off(client, monkeypatch):
    monkeypatch.setattr(local_routes, "_instance", _explode)
    await _refused(await client.post(local_routes.CONSENT_PATH, json={"instance_id": "i"}))


@pytest.mark.parametrize("path", [local_routes.DESCRIBE_PATH, local_routes.REDEEM_PATH])
async def test_flag_crafted_native_calls_are_refused_while_off(client, monkeypatch, path):
    monkeypatch.setattr(local_routes, "_instance", _explode)
    resp = await client.post(
        path,
        json={"consent": "x" * 32, "root": "/tmp", "mcp": []},
        headers={"X-Test-Internal": "1"},
    )
    await _refused(resp)


async def test_flag_the_same_requests_pass_the_switch_once_it_is_on(client, switch, monkeypatch):
    switch["on"] = True
    monkeypatch.setattr(remote_routes, "device_rows", lambda: [])
    resp = await client.get(remote_routes.DEVICES_PATH)
    assert resp.status == 200
    assert await resp.json() == {"devices": []}


@pytest.mark.parametrize("action", ["revoke", "remove"])
async def test_flag_narrowing_still_works_while_off(client, monkeypatch, tmp_path, action):
    registry = DeviceRegistry(SecretVault(tmp_path / "vault"))
    rec = registry.enroll(
        base="laptop", fingerprint=install_fingerprint(b"\x01" * 32), label="Laptop", owner="me"
    )
    monkeypatch.setattr(remote_routes, "_registry", lambda: registry)
    resp = await client.post(f"{remote_routes.DEVICES_PATH}/{rec.device_id}/{action}")
    assert resp.status == 200, await resp.text()
    assert registry.get(rec.device_id).active is False


async def test_flag_local_grant_list_and_off_work_while_off(client, monkeypatch):
    from kiro_crew.connected_computers import controller as controller_mod

    async def off() -> list:
        return []

    fake = SimpleNamespace(status=lambda: [], off=off)
    monkeypatch.setattr(controller_mod, "get_controller", lambda: fake)
    resp = await client.get(local_routes.GRANTS_PATH)
    assert resp.status == 200
    assert await resp.json() == {"grants": [], "enabled": False}
    assert (await client.post(local_routes.OFF_PATH)).status == 200


# -- the chat environment picker ---------------------------------------------------------


def test_flag_the_picker_offers_no_computer_and_reads_nothing_while_off(switch, monkeypatch):
    from kiro_crew.dashboard.handlers.session_environments import _connected_computer_choices

    monkeypatch.setattr(remote_routes, "device_rows", _explode)
    assert _connected_computer_choices(set()) == []


def test_flag_the_picker_offers_active_untaken_computers_once_on(switch, monkeypatch):
    from kiro_crew.dashboard.handlers.session_environments import _connected_computer_choices

    switch["on"] = True
    row = {"root": "/w", "connected": True, "label_is_shared": False}
    monkeypatch.setattr(
        remote_routes,
        "device_rows",
        lambda: [
            {**row, "device_id": "laptop", "label": "Laptop", "status": "active"},
            {**row, "device_id": "old", "label": "Old", "status": "revoked"},
            {**row, "device_id": "dev-box", "label": "Taken", "status": "active"},
        ],
    )
    choices = _connected_computer_choices({"dev-box"})
    assert [c["id"] for c in choices] == ["laptop"]
    assert choices[0]["provider"] == "connected_computer"


# -- CLI siblings ----------------------------------------------------------------------


class _NoStdin(io.StringIO):
    def read(self, *_a: Any) -> str:
        raise AssertionError("the request body must not be read while the feature is off")


def test_flag_cli_enroll_is_refused_before_reading_the_request(switch, monkeypatch, capsys):
    from kiro_crew.connected_computers import cli

    monkeypatch.setattr("sys.stdin", _NoStdin())
    assert cli._remote_enroll() == 2
    assert flag.DISABLED_REASON in capsys.readouterr().out


@pytest.mark.parametrize("verb", ["allow", "share", "grant"])
def test_flag_cli_has_no_verb_that_shares_a_folder(verb, capsys):
    """Sharing needs native consent (cli.py's docstring), so no switch state lets a
    terminal grant a root: the verb does not parse at all."""
    import argparse

    from kiro_crew.connected_computers import cli

    parser = argparse.ArgumentParser(prog="kirocrew")
    cli.register_parser(parser.add_subparsers(dest="command"))
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["connected-computer", verb, "inst-1", "/tmp"])
    assert exc.value.code == 2
    assert "invalid choice" in capsys.readouterr().err
