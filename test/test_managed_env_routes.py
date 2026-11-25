"""The lane's routes: owner check first, 404 while the preview is off, own ids only."""

from __future__ import annotations

import asyncio
import logging
import stat
import sys
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from managed_env_fake_box import FakeBox, FakeRegistry
from managed_env_fake_service import FakeService

from kiro_crew.managed_env.client import Credential, SandboxClient
from kiro_crew.managed_env.engine import SandboxLaunchEngine
from kiro_crew.managed_env.guard import SandboxGuard
from kiro_crew.managed_env.remote import ssh_host_for
from kiro_crew.managed_env.routes import SandboxRoutes, register_sandbox_routes
from kiro_crew.managed_env.settings import LaneSettings

ROUTES = [
    ("GET", "/api/managed-environments/list"),
    ("POST", "/api/managed-environments/env-001/stop"),
    ("POST", "/api/managed-environments/env-001/resume"),
    ("POST", "/api/managed-environments/env-001/delete"),
    ("POST", "/api/managed-environments/env-001/ssh-session"),
    ("GET", "/api/managed-environments/ssh-config"),
    ("POST", "/api/managed-environments/ssh-config"),
]


@pytest.fixture
def svc():
    f = FakeService()
    yield f
    f.close()


@pytest.fixture
def lane(svc, tmp_path, monkeypatch):
    from kiro_crew.dashboard.handlers import _shared

    state = {"on": True, "owner": True}

    async def owner(request, operation):
        return None if state["owner"] else web.json_response({"error": "forbidden"}, status=403)

    monkeypatch.setattr(_shared, "require_owner_dashboard_request", owner)
    settings = LaneSettings(endpoint=svc.endpoint, ssh_region="ex-test-1")
    client = SandboxClient(
        lambda: Credential("tok-secret", "a"),
        SandboxGuard(tmp_path / "s"),
        settings=lambda: settings,
    )
    engine = SandboxLaunchEngine(
        client=client, registry_factory=FakeRegistry, run=FakeBox(), settings=lambda: settings
    )
    routes = SandboxRoutes(lambda: engine, lane_enabled=lambda: state["on"])
    return routes, client, state


def _dashboard(reg=None, manager=None):
    """The dashboard state the core Remove reads: its registry and tunnel manager."""
    return SimpleNamespace(instances_registry=reg or FakeRegistry(), instances_manager=manager)


def call(routes: SandboxRoutes, method: str, path: str, state=None):
    async def go():
        app = web.Application()
        app["state"] = state or _dashboard()
        register_sandbox_routes(app, routes)
        async with TestClient(TestServer(app)) as c:
            resp = await c.request(method, path)
            return resp.status, await resp.json()

    return asyncio.run(go())


def _own(client, svc) -> str:
    sid = client.create_sandbox("crew-a-kc-0a1b2c")["execEnvironmentId"]
    svc.sandboxes[sid]["status"] = "RUNNING"
    return sid


@pytest.mark.parametrize("method, path", ROUTES)
def test_a_real_teammate_is_refused_on_every_lane_route_and_the_owner_passes(
    monkeypatch, method, path
):
    """The real owner gate, no stub: the teammate seam names this prefix."""
    from test_shared_crew_teammate_owner_access import OWNER, _request

    from kiro_crew.shared_crew import owner
    from kiro_crew.shared_crew.teammate_access import is_shared_crew_teammate

    monkeypatch.setenv("KIROCREW_SHARED_CREW_PREVIEW", "1")
    monkeypatch.setattr(owner, "_owner_subject", OWNER)
    routes = SandboxRoutes(
        lambda: pytest.fail("no engine for a teammate"), lane_enabled=lambda: True
    )
    handler = {
        "list": routes.list_handler,
        "ssh-session": routes.ssh_session,
        "ssh-config": routes.ssh_config_handler,
    }.get(path.rsplit("/", 1)[1], routes.act)
    teammate = _request("ssm", path, method)
    assert is_shared_crew_teammate(teammate)
    resp = asyncio.run(handler(teammate))
    assert resp.status == 403 and b"owner_only" in resp.body
    assert asyncio.run(routes._gate(_request("owner", path, method), "x")) is None


@pytest.mark.parametrize("method, path", ROUTES)
def test_every_route_checks_the_owner_first_then_answers_404_while_off(lane, svc, method, path):
    routes, _, state = lane
    state["owner"] = False
    assert call(routes, method, path)[0] == 403
    state["owner"], state["on"] = True, False
    assert call(routes, method, path)[0] == 404
    assert svc.requests == []


def test_the_list_marks_own_environments(lane, svc):
    routes, client, _ = lane
    sid = _own(client, svc)
    other = svc.add("RUNNING", "elsewhere")
    status, body = call(routes, "GET", "/api/managed-environments/list")
    assert status == 200
    items = {i["sandbox_id"]: i for i in body["sandboxes"]}
    assert items[sid]["own"] is True and items[sid]["ssh_host"] == f"crew-sbx-{sid}"
    assert items[other]["own"] is False and "ssh_host" not in items[other]


@pytest.mark.parametrize("action", ["stop", "resume", "delete", "ssh-session"])
def test_another_clients_environment_takes_no_action(lane, svc, action):
    routes, *_ = lane
    other = svc.add("RUNNING", "elsewhere")
    status, body = call(routes, "POST", f"/api/managed-environments/{other}/{action}")
    assert status == 409 and body["code"] == "not_own_id"
    assert svc.ops() == []


def test_stop_resume_and_delete_an_own_environment(lane, svc):
    routes, client, _ = lane
    sid = _own(client, svc)
    assert call(routes, "POST", f"/api/managed-environments/{sid}/stop") == (
        200,
        {"status": "STOPPING"},
    )
    assert call(routes, "POST", f"/api/managed-environments/{sid}/resume")[0] == 200
    assert call(routes, "POST", f"/api/managed-environments/{sid}/delete") == (
        200,
        {"status": "deleting"},
    )
    assert call(routes, "POST", "/api/managed-environments/env-x/reboot")[0] == 404


def test_delete_sends_one_delete_sandbox_for_the_ledger_id_and_removes_no_remote_crew_row(
    lane, svc
):
    routes, client, _ = lane
    reg = FakeRegistry()
    disconnected: list[str] = []

    class Manager:
        async def disconnect(self, crew_id, keep_intent=False):
            disconnected.append(crew_id)
            return True

    dash = _dashboard(reg, Manager())
    sid = _own(client, svc)
    row = reg.add(name="crew-a", ssh_host=ssh_host_for(sid))
    path = f"/api/managed-environments/{sid}/delete"
    assert call(routes, "POST", path, dash) == (200, {"status": "deleting"})
    assert svc.ops() == ["CreateSandbox", "DeleteSandbox"]
    assert svc.requests[-1]["body"]["execEnvironmentId"] == sid
    assert "deletedAt" in client._guard.ledger()[0]
    assert reg.list() == [row] and disconnected == []
    # Pressing Delete again sends DeleteSandbox again; the service's 404 reads as gone.
    assert call(routes, "POST", path, dash) == (200, {"status": "deleting"})
    assert svc.ops() == ["CreateSandbox", "DeleteSandbox", "DeleteSandbox"]
    assert reg.list() == [row] and disconnected == []


def test_a_deleted_id_the_list_no_longer_shows_is_listed_as_gone(lane, svc):
    routes, client, _ = lane
    sid = _own(client, svc)
    kept = _own(client, svc)
    assert call(routes, "POST", f"/api/managed-environments/{sid}/delete")[0] == 200
    # While listed, a deleted own id reads as being deleted, with its alias.
    svc.answer(
        "ListSandboxes",
        200,
        {"sandboxes": [{"execEnvironmentId": sid, "status": "DELETING"}]},
    )
    _, body = call(routes, "GET", "/api/managed-environments/list")
    assert body["sandboxes"] == [
        {"sandbox_id": sid, "status": "DELETING", "own": True, "ssh_host": ssh_host_for(sid)}
    ]
    # Once the List omits it, it reads gone: no alias, so no action is offered.
    _, body = call(routes, "GET", "/api/managed-environments/list")
    items = {i["sandbox_id"]: i for i in body["sandboxes"]}
    assert items[sid] == {"sandbox_id": sid, "status": "GONE", "own": True}
    assert items[kept]["status"] == "RUNNING" and items[kept]["own"] is True
    assert svc.ops().count("DeleteSandbox") == 1  # the List sent no Delete


@pytest.mark.parametrize("deleted", [False, True])
@pytest.mark.parametrize("listed", ["absent", "DELETED", "DELETING", "DELETE_FAILED", "RUNNING"])
def test_list_never_removes_remote_crew_rows(lane, svc, deleted, listed):
    routes, client, _ = lane
    reg = FakeRegistry()
    sid = _own(client, svc)
    row = reg.add(name="crew-a", ssh_host=ssh_host_for(sid))
    if deleted:
        client._guard.record_deleted(sid)
    # A sign-in at the same start URL can list another account's environments.
    client._credential = lambda: Credential("other-account-token", "a")
    entries = [] if listed == "absent" else [{"execEnvironmentId": sid, "status": listed}]
    svc.answer("ListSandboxes", 200, {"sandboxes": entries})
    status, body = call(routes, "GET", "/api/managed-environments/list", _dashboard(reg))
    assert status == 200
    gone = 1 if deleted and listed == "absent" else 0
    assert len(body["sandboxes"]) == len(entries) + gone
    assert reg.list() == [row]
    assert svc.sandboxes[sid]["status"] == "RUNNING"
    assert svc.ops() == ["CreateSandbox", "ListSandboxes"]
    assert svc.sandboxes[sid]["status"] == "RUNNING"
    assert svc.ops() == ["CreateSandbox", "ListSandboxes"]


def test_the_lane_routes_are_404_on_an_lts_build(tmp_path, monkeypatch):
    """The real preview switch, not a stub: an LTS build reads ``crew_client`` off."""
    import json
    from unittest.mock import patch

    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(
        json.dumps({"dashboard": {"feature_previews": {"crew_client": True}}}), encoding="utf-8"
    )
    monkeypatch.setattr("kiro_crew.config.live.snapshot", lambda: None)
    from kiro_crew.dashboard.handlers import _shared

    async def owner(request, operation):
        return None

    monkeypatch.setattr(_shared, "require_owner_dashboard_request", owner)
    routes = SandboxRoutes(lambda: pytest.fail("no engine while off"))
    with patch("kiro_crew.config.loader.config_path", return_value=cfg_path):
        from kiro_crew.managed_env import settings as lane_settings

        assert lane_settings.lane_enabled() is True
        monkeypatch.setattr("kiro_crew.edition.EDITION", "lts")
        for method, path in ROUTES:
            assert call(routes, method, path) == (
                404,
                {"code": "not_found", "error": "not found"},
            )


def test_the_ssh_session_is_own_running_only_and_never_logged(lane, svc, caplog):
    routes, client, _ = lane
    caplog.set_level(logging.DEBUG)
    sid = _own(client, svc)
    call(routes, "GET", "/api/managed-environments/list")
    status, body = call(routes, "POST", f"/api/managed-environments/{sid}/ssh-session")
    assert status == 200 and body["region"] == "ex-test-1" and body["tokenValue"] == "tv"
    assert "tv" not in caplog.text and "wss://x" not in caplog.text
    # a List that reads it STOPPED: refused with no call
    svc.sandboxes[sid]["status"] = "STOPPED"
    call(routes, "GET", "/api/managed-environments/list")
    asked = svc.ops().count("StartSshSession")
    status, body = call(routes, "POST", f"/api/managed-environments/{sid}/ssh-session")
    assert status == 409 and body["code"] == "not_ready"
    assert svc.ops().count("StartSshSession") == asked
    # Resume, then Connect with no List between: the mark is read again, not trusted
    assert call(routes, "POST", f"/api/managed-environments/{sid}/resume")[0] == 200
    svc.sandboxes[sid]["status"] = "RUNNING"
    assert call(routes, "POST", f"/api/managed-environments/{sid}/ssh-session")[0] == 200
    # a stop marks it; a session asked while it still reads STOPPED is refused after one read
    call(routes, "POST", f"/api/managed-environments/{sid}/stop")
    svc.sandboxes[sid]["status"] = "STOPPED"
    assert call(routes, "POST", f"/api/managed-environments/{sid}/ssh-session")[0] == 409
    svc.sandboxes[sid]["status"] = "RUNNING"
    assert call(routes, "POST", f"/api/managed-environments/{sid}/ssh-session")[0] == 200


def test_a_signed_out_lane_says_so(lane, svc):
    routes, client, _ = lane
    client._credential = lambda: Credential("")
    status, body = call(routes, "GET", "/api/managed-environments/list")
    assert status == 409 and body["code"] == "not_signed_in"


def test_an_expired_lane_sign_in_answers_the_one_sign_in_line(lane, svc):
    from kiro_crew.managed_env import core_signin

    routes, client, _ = lane

    def expired():
        raise core_signin.NotLaneSignIn(core_signin.NOT_SIGNED_IN)

    client._credential = expired
    status, body = call(routes, "GET", "/api/managed-environments/list")
    assert status == 409 and body == {
        "code": "not_signed_in",
        "error": core_signin.NOT_SIGNED_IN,
    }


def test_the_ssh_settings_are_read_and_written_with_the_gateways_python(
    lane, tmp_path, monkeypatch
):
    routes, _, _ = lane
    cfg = tmp_path / "ssh" / "config"
    monkeypatch.setattr("kiro_crew.managed_env.ssh_config.default_config", lambda: str(cfg))
    from kiro_crew.managed_env import ssh_config

    block = ssh_config.render_block(ssh_config.gateway_python())
    assert call(routes, "GET", "/api/managed-environments/ssh-config") == (
        200,
        {"present": False, "path": str(cfg), "block": block},
    )
    assert not cfg.exists()  # a read writes nothing
    assert call(routes, "POST", "/api/managed-environments/ssh-config") == (
        200,
        {"present": True, "path": str(cfg), "block": block},
    )
    assert cfg.read_text(encoding="utf-8").startswith(block)
    assert sys.executable in cfg.read_text(encoding="utf-8")

    cfg.write_text(ssh_config.BEGIN + "\n", encoding="utf-8")  # a damaged block
    status, body = call(routes, "POST", "/api/managed-environments/ssh-config")
    assert status == 409 and body["code"] == "ssh_config_unwritable"


def test_the_ssh_settings_read_returns_only_crews_block_and_changes_nothing(
    lane, tmp_path, monkeypatch
):
    """The preview GET answers the block a write would add, never the person's own lines."""
    from kiro_crew.managed_env import ssh_config

    routes, _, _ = lane
    cfg = tmp_path / "config"
    mine = "Host work\n  HostName work.example\n  IdentityFile ~/.ssh/work_key\n"
    cfg.write_text(mine, encoding="utf-8")
    monkeypatch.setattr("kiro_crew.managed_env.ssh_config.default_config", lambda: str(cfg))
    status, body = call(routes, "GET", "/api/managed-environments/ssh-config")
    assert status == 200 and set(body) == {"present", "path", "block"}
    assert body["block"] == ssh_config.render_block(ssh_config.gateway_python())
    assert "work.example" not in repr(body) and "work_key" not in repr(body)
    assert cfg.read_text(encoding="utf-8") == mine


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
def test_the_ssh_settings_write_keeps_other_entries_and_mode_0600(lane, tmp_path, monkeypatch):
    from kiro_crew.managed_env import ssh_config

    routes, _, _ = lane
    (tmp_path / "home").mkdir()
    cfg = tmp_path / "home" / "config"
    mine = "Host work\n  HostName work.example\n  User me\n\nHost *\n  ServerAliveInterval 30\n"
    cfg.write_text(mine, encoding="utf-8")
    cfg.chmod(0o600)
    monkeypatch.setattr("kiro_crew.managed_env.ssh_config.default_config", lambda: str(cfg))
    assert call(routes, "POST", "/api/managed-environments/ssh-config")[0] == 200
    text = cfg.read_text(encoding="utf-8")
    assert text.count(ssh_config.BEGIN) == 1 and text.count(ssh_config.END) == 1
    # outside the Crew-marked block the file is the user's, byte for byte
    start, end = text.index(ssh_config.BEGIN), text.index(ssh_config.END) + len(ssh_config.END) + 1
    assert (text[:start] + text[end:]).lstrip("\n") == mine
    assert stat.S_IMODE(cfg.stat().st_mode) == 0o600
    # a second write changes nothing
    assert call(routes, "POST", "/api/managed-environments/ssh-config")[0] == 200
    assert cfg.read_text(encoding="utf-8") == text
    assert [p.name for p in cfg.parent.iterdir()] == ["config"]  # no temp file left
