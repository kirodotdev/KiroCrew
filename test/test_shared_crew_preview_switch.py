"""The owner's Shared Crew switch: a keystone file, an owner-only route, a start-time snapshot."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew import sandbox, security
from kiro_crew.shared_crew import preview, routes_invites, routes_preview
from kiro_crew.shared_crew.contracts import ACTOR_REQUEST_KEY, ActorContext, Transport
from kiro_crew.shared_crew.identity import local_operator_principal

LEAF = "shared_crew_preview.json"


@pytest.fixture(autouse=True)
def _home(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
    _floor_monkeypatch.delenv(preview.PREVIEW_ENV, raising=False)
    preview.reset_for_tests()
    yield tmp_path
    preview.reset_for_tests()


# -- the stored choice and the start-time snapshot ---------------------------- #


def test_the_default_is_off_and_only_a_json_true_turns_it_on(_home):
    path = _home / LEAF
    assert preview.read_stored() is False
    for raw, want in (
        ('{"enabled": true}', True),
        ('{"enabled": false}', False),
        ('{"enabled": "true"}', False),
        ('{"enabled": 1}', False),
        ("{}", False),
        ("[]", False),
        ("not json", False),
    ):
        path.write_text(raw, encoding="utf-8")
        assert preview.read_stored() is want, raw


def test_the_gate_answers_from_the_choice_read_at_start_not_the_file_now(_home):
    preview.write_stored(True)
    assert preview.snapshot_at_start() is True
    assert preview.preview_enabled() is True
    preview.write_stored(False)
    assert preview.preview_enabled() is True  # the running process keeps its start-time answer
    preview.reset_for_tests()
    assert preview.preview_enabled() is False


def test_a_save_rewrites_the_existing_record_in_place(_home):
    # A running Linux sandbox bind-mounts this inode read-only; a rename over it
    # would detach that mount and leave the new file writable inside the sandbox.
    path = _home / LEAF
    path.write_text("{}", encoding="utf-8")
    inode = path.stat().st_ino
    preview.write_stored(True)
    assert path.stat().st_ino == inode
    assert preview.read_stored() is True
    preview.write_stored(False)
    assert path.stat().st_ino == inode
    assert preview.read_stored() is False
    assert json.loads(path.read_text(encoding="utf-8")) == {"enabled": False}


def test_a_save_creates_an_absent_record_owner_only(_home):
    import os
    import stat

    preview.write_stored(True)
    assert preview.read_stored() is True
    if os.name == "posix":
        assert stat.S_IMODE((_home / LEAF).stat().st_mode) == 0o600


def test_a_record_pre_created_while_saving_is_rewritten_not_replaced(_home, monkeypatch):
    # A sandbox spawn pre-creates and seals the name between this writer's
    # "absent" and its create; the writer must then open that file, not rename.
    import os

    path = _home / LEAF
    real_open = os.open
    seen: dict[str, int] = {}

    def racing_open(target, flags, *args, **kwargs):
        if target == path and flags & os.O_EXCL and "inode" not in seen:
            path.write_text("{}", encoding="utf-8")
            seen["inode"] = path.stat().st_ino
        return real_open(target, flags, *args, **kwargs)

    monkeypatch.setattr(preview.os, "open", racing_open)
    preview.write_stored(True)
    assert path.stat().st_ino == seen["inode"]
    assert preview.read_stored() is True


@pytest.mark.skipif(not hasattr(__import__("os"), "symlink"), reason="needs symlinks")
def test_a_save_refuses_a_symlink_or_hard_linked_record(_home, tmp_path_factory):
    import os

    elsewhere = tmp_path_factory.mktemp("elsewhere") / "target.json"
    elsewhere.write_text("{}", encoding="utf-8")
    path = _home / LEAF
    os.link(elsewhere, path)
    with pytest.raises(OSError):
        preview.write_stored(True)
    assert elsewhere.read_text(encoding="utf-8") == "{}"
    path.unlink()
    try:
        os.symlink(elsewhere, path)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(OSError):
        preview.write_stored(True)
    assert elsewhere.read_text(encoding="utf-8") == "{}"


def test_the_environment_override_turns_it_on_only_for_exactly_one(monkeypatch):
    for raw, want in (("1", True), ("true", False), ("0", False), ("", False)):
        monkeypatch.setenv(preview.PREVIEW_ENV, raw)
        assert preview.preview_enabled() is want, raw


def test_the_setting_reports_when_a_restart_changes_the_answer(monkeypatch):
    assert preview.current_setting().to_dict() == {
        "enabled": False,
        "active": False,
        "env_override": False,
        "restart_required": False,
    }
    preview.write_stored(True)
    assert preview.current_setting().to_dict() == {
        "enabled": True,
        "active": False,
        "env_override": False,
        "restart_required": True,
    }
    preview.reset_for_tests()
    assert preview.current_setting().restart_required is False
    # Forced on by the environment: the stored choice does not decide a restart.
    monkeypatch.setenv(preview.PREVIEW_ENV, "1")
    preview.write_stored(False)
    forced = preview.current_setting()
    assert forced.active is True and forced.env_override is True and forced.enabled is False
    assert forced.restart_required is False


# -- the route ---------------------------------------------------------------- #


def _actor():
    return ActorContext(
        principal=local_operator_principal("local-app"), request_id="r", transport=Transport.HTTP
    )


async def _client(*, dashboard_user: bool = True, app_name: str = "") -> TestClient:
    @web.middleware
    async def claims(request: web.Request, handler: Any) -> web.StreamResponse:
        request["user"] = "local-app"
        request["is_dashboard_user"] = dashboard_user
        request["app"] = app_name
        request[ACTOR_REQUEST_KEY] = _actor()
        return await handler(request)

    app = web.Application(middlewares=[claims])
    app["state"] = SimpleNamespace(owner_id="")
    routes_preview.register(app)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


@pytest.mark.asyncio
async def test_the_owner_reads_and_writes_it_while_the_preview_is_off(_home, monkeypatch):
    audits: list = []
    monkeypatch.setattr(routes_preview, "_audit", lambda *a: audits.append(a[1:]))
    assert preview.preview_enabled() is False
    client = await _client()
    try:
        got = await client.get(routes_preview.PREVIEW_PATH)
        assert got.status == 200
        assert await got.json() == {
            "enabled": False,
            "active": False,
            "env_override": False,
            "restart_required": False,
        }
        put = await client.put(routes_preview.PREVIEW_PATH, json={"enabled": True})
        assert put.status == 200
        assert await put.json() == {
            "enabled": True,
            "active": False,
            "env_override": False,
            "restart_required": True,
        }
    finally:
        await client.close()
    assert json.loads((_home / LEAF).read_text(encoding="utf-8")) == {"enabled": True}
    assert audits == [(True, "enabled")]
    # The running process is unchanged until the next start.
    assert preview.preview_enabled() is False


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [{}, {"enabled": "yes"}, {"enabled": 1}, [], "x"])
async def test_a_body_that_is_not_a_boolean_is_refused_and_writes_nothing(_home, body):
    client = await _client()
    try:
        resp = await client.put(routes_preview.PREVIEW_PATH, json=body)
        assert resp.status == 400
        assert (await resp.json())["code"] == "invalid_body"
    finally:
        await client.close()
    assert not (_home / LEAF).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "dashboard_user,app_name", [(False, ""), (True, "notes")], ids=["not-dashboard", "app-token"]
)
async def test_anyone_but_the_owner_is_refused_with_403_not_404(_home, dashboard_user, app_name):
    client = await _client(dashboard_user=dashboard_user, app_name=app_name)
    try:
        assert (await client.get(routes_preview.PREVIEW_PATH)).status == 403
        resp = await client.put(routes_preview.PREVIEW_PATH, json={"enabled": True})
        assert resp.status == 403
    finally:
        await client.close()
    assert not (_home / LEAF).exists()


@pytest.mark.asyncio
async def test_a_refusal_from_the_shared_owner_gate_is_returned_unchanged(_home, monkeypatch):
    async def refuse(_request):
        return web.json_response({"code": "owner_only"}, status=403)

    monkeypatch.setattr(routes_invites, "_owner_refusal", refuse)
    client = await _client()
    try:
        resp = await client.put(routes_preview.PREVIEW_PATH, json={"enabled": True})
        assert resp.status == 403 and (await resp.json())["code"] == "owner_only"
    finally:
        await client.close()
    assert not (_home / LEAF).exists()


@pytest.mark.asyncio
async def test_an_oversized_or_unreadable_body_is_refused_and_writes_nothing(_home):
    client = await _client()
    try:
        big = await client.put(
            routes_preview.PREVIEW_PATH,
            data=b'{"enabled": true, "pad": "' + b"x" * 2048 + b'"}',
            headers={"Content-Type": "application/json"},
        )
        assert big.status == 413 and (await big.json())["code"] == "invalid_body"
        bad = await client.put(
            routes_preview.PREVIEW_PATH,
            data=b"{not json",
            headers={"Content-Type": "application/json"},
        )
        assert bad.status == 400 and (await bad.json())["code"] == "invalid_body"
    finally:
        await client.close()
    assert not (_home / LEAF).exists()


@pytest.mark.asyncio
async def test_a_failed_write_is_a_500_and_is_audited_as_failed(_home, monkeypatch):
    audits: list = []
    monkeypatch.setattr(routes_preview, "_audit", lambda *a: audits.append(a[1:]))

    def fail(_enabled: bool) -> None:
        raise OSError("read-only file system")

    monkeypatch.setattr(preview, "write_stored", fail)
    client = await _client()
    try:
        resp = await client.put(routes_preview.PREVIEW_PATH, json={"enabled": True})
        assert resp.status == 500 and (await resp.json())["code"] == "preview_write_failed"
    finally:
        await client.close()
    assert audits == [(True, "write_failed")]


def test_the_audit_record_is_critical_and_never_raises(monkeypatch):
    import kiro_crew.sel as sel_module

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        sel_module, "sel", lambda: SimpleNamespace(log_api_access=lambda **kw: calls.append(kw))
    )
    request = {"user": "local-app"}
    routes_preview._audit(request, True, "enabled")  # type: ignore[arg-type]
    assert calls == [
        {
            "caller": "local-app",
            "operation": "shared_crew.preview.set",
            "outcome": "enabled",
            "source": "shared_crew_preview",
            "resources": "enabled=True",
            "critical": True,
        }
    ]

    def broken() -> Any:
        raise RuntimeError("sel unavailable")

    monkeypatch.setattr(sel_module, "sel", broken)
    routes_preview._audit(request, False, "disabled")  # type: ignore[arg-type]


def test_the_routes_are_registered_by_the_shared_crew_slice_whatever_the_preview():
    from kiro_crew.dashboard.routes import shared_crew

    app = web.Application()
    shared_crew.register(app)
    routes = {(r.method, r.resource.canonical) for r in app.router.routes() if r.resource}
    assert ("GET", routes_preview.PREVIEW_PATH) in routes
    assert ("PUT", routes_preview.PREVIEW_PATH) in routes
    assert preview.preview_enabled() is False


# -- the keystone seal -------------------------------------------------------- #


def test_the_leaf_is_sealed_like_the_other_owner_consent_records():
    assert LEAF in security._CREW_SECRET_LEAVES
    assert security.is_sensitive_path(f"~/.kiro/crew/{LEAF}") is True
    assert LEAF in sandbox._CREW_READONLY_LEAVES
    assert LEAF in sandbox._CREW_PRECREATE_READONLY_FILE_LEAVES
    assert LEAF in sandbox._CREW_CHILD_WITHHELD_LEAVES


def test_the_location_follows_the_data_home(_home):
    from kiro_crew.config import loader

    assert loader.shared_crew_preview_path() == _home / LEAF


def test_the_preview_switch_is_not_a_config_field_or_a_change_card():
    from kiro_crew.config.sections import FEATURE_PREVIEW_FLAGS

    assert not any("shared" in flag for flag in FEATURE_PREVIEW_FLAGS)
