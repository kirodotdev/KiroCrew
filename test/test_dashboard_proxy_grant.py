"""The owner's ``dashboard_proxy`` grant: never pre-granted, registry-verified, per install.

The grant is a gateway-only record under ``member-records/`` (sandbox-masked, never under
the agent-writable ``apps/<app>/``). It names the install it was given for, so a reinstall
or another package under the same name holds none. It counts only while the owner-enable
marker stands. Secret-shaped values here are described in words.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from kiro_crew.apps import dashboard_proxy
from kiro_crew.apps import dashboard_proxy_routes as routes

_APP = "tunnel-like"


@pytest.fixture
def records(tmp_path, monkeypatch):
    home = tmp_path / "kirocrew-home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    monkeypatch.setattr(
        "kiro_crew.member_records.records_dir", lambda kind: home / "member-records" / kind
    )
    return home


@pytest.fixture
def installed(monkeypatch):
    record = SimpleNamespace(
        installedAt="2026-11-22T10:00:00Z",
        version="0.9.4",
        origin="registry",
        sourceRegistry="crew-tunnels",
        sourceUrl="https://example.invalid/tunnel.git",
        sourceCommit="abc",
        sourceSigner="",
    )
    monkeypatch.setattr(dashboard_proxy, "_installed", lambda _name: record)
    monkeypatch.setattr(dashboard_proxy, "_marker_state", lambda _name: (True, True))
    monkeypatch.setattr(dashboard_proxy, "_namespace_backend", lambda: True)
    return record


def test_no_grant_or_offer_on_a_backend_that_cannot_bind_a_stand_in(
    records, installed, monkeypatch
):
    """Seatbelt and every other non-namespace backend: refuse, and keep every mask."""
    monkeypatch.setattr(dashboard_proxy, "_namespace_backend", lambda: False)
    assert dashboard_proxy.grant(_APP) == (False, "platform-unsupported")
    assert dashboard_proxy.prompt_text(_APP) is None
    assert dashboard_proxy.granted(_APP) is False


def test_a_granted_app_stays_masked_when_the_backend_cannot_bind_a_stand_in(
    records, installed, monkeypatch
):
    dashboard_proxy.set_gateway_secret("gateway-secret-in-words")
    assert dashboard_proxy.grant(_APP) == (True, "")
    masks = (str(records / ".local_secret"),)
    monkeypatch.setattr(dashboard_proxy, "_namespace_backend", lambda: False)
    assert dashboard_proxy.backend_masks(_APP, masks) == (masks, ())


def test_an_installed_app_is_never_pre_granted(records, installed):
    assert dashboard_proxy.granted(_APP) is False


def test_the_grant_is_recorded_for_this_install_only(records, installed):
    assert dashboard_proxy.grant(_APP) == (True, "")
    assert dashboard_proxy.granted(_APP) is True
    stored = json.loads(
        (records / "member-records/app-dashboard-proxy" / f"{_APP}.json").read_text()
    )
    assert stored["app"] == _APP and "identity" in stored
    installed.installedAt = "2026-11-22T11:00:00Z"  # a reinstall
    assert dashboard_proxy.granted(_APP) is False
    installed.installedAt = "2026-11-22T10:00:00Z"
    installed.sourceUrl = "https://example.invalid/another-package.git"  # another package
    assert dashboard_proxy.granted(_APP) is False


def test_only_a_registry_install_can_be_offered_the_grant(records, installed):
    installed.origin = "local"
    assert dashboard_proxy.prompt_text(_APP) is None
    assert dashboard_proxy.grant(_APP) == (False, "not-registry-verified")
    assert dashboard_proxy.granted(_APP) is False


def test_the_offer_names_publisher_and_package_from_the_install_record(records, installed):
    offer = dashboard_proxy.prompt_text(_APP)
    assert offer is not None
    assert offer["registry"] == "crew-tunnels"
    assert offer["source"] == "https://example.invalid/tunnel.git"
    for words in (
        "tunnel-like",
        "0.9.4",
        "crew-tunnels",
        dashboard_proxy.DASHBOARD_ACCESS_SCOPE,
        "show the dashboard page with your theme and a list of your chats",
        "stop a running chat turn",
        "cannot read chat messages, send them, read your preferences, change settings, credentials, apps",
    ):
        assert words in offer["text"]
    assert "read your dashboard and chats" not in offer["text"]
    assert "run agents as you" not in offer["text"]
    assert "full dashboard control" not in offer["text"]


def test_the_grant_counts_only_while_the_enable_marker_stands(records, installed, monkeypatch):
    dashboard_proxy.grant(_APP)
    monkeypatch.setattr(dashboard_proxy, "_marker_state", lambda _name: (False, True))
    assert dashboard_proxy.granted(_APP) is False
    monkeypatch.setattr(dashboard_proxy, "_marker_state", lambda _name: (True, False))
    assert dashboard_proxy.granted(_APP) is False


def test_no_grant_without_the_enable_marker(records, installed, monkeypatch):
    monkeypatch.setattr(dashboard_proxy, "_marker_state", lambda _name: (False, True))
    assert dashboard_proxy.grant(_APP) == (False, "not-enabled")


def test_revoke_removes_the_record_and_the_stand_in_source(records, installed):
    dashboard_proxy.set_gateway_secret("gateway-secret-in-words")
    dashboard_proxy.grant(_APP)
    pairs = dashboard_proxy.stand_in_pairs(_APP, (str(records / ".local_secret"),))
    assert pairs
    dashboard_proxy.revoke(_APP)
    assert not list((records / "member-records/app-dashboard-proxy").iterdir())


@pytest.fixture
def forged_apps(records, tmp_path, monkeypatch):
    """An app tree whose ``installed.json`` claims a registry install the gateway never made."""
    apps = tmp_path / "apps"
    (apps / _APP).mkdir(parents=True)
    forged = {
        "name": _APP,
        "version": "9.9.9",
        "installedAt": "2026-11-22T09:00:00Z",
        "origin": "registry",
        "source": f"registry:{_APP}",
        "sourceUrl": "https://example.invalid/forged.git",
        "sourceRegistry": "crew-tunnels",
        "sourceSigner": "someone",
    }
    (apps / _APP / "installed.json").write_text(json.dumps(forged), encoding="utf-8")
    monkeypatch.setattr("kiro_crew.apps.manager.apps_dir", lambda: apps)
    monkeypatch.setattr(dashboard_proxy, "_marker_state", lambda _name: (True, True))
    monkeypatch.setattr(dashboard_proxy, "_namespace_backend", lambda: True)
    return apps


def test_a_forged_installed_json_does_not_make_an_app_grantable(forged_apps):
    assert dashboard_proxy.registry_verified(_APP) is False
    assert dashboard_proxy.prompt_text(_APP) is None
    assert dashboard_proxy.grant(_APP) == (False, "not-registry-verified")
    assert dashboard_proxy.granted(_APP) is False


def test_the_gateway_install_record_is_what_makes_an_app_grantable(forged_apps):
    dashboard_proxy.record_install(
        _APP,
        version="0.9.4",
        registry="crew-tunnels",
        url="https://example.invalid/tunnel.git",
        commit="abc",
        signer="",
    )
    assert dashboard_proxy.registry_verified(_APP) is True
    offer = dashboard_proxy.prompt_text(_APP)
    assert offer is not None and offer["source"] == "https://example.invalid/tunnel.git"
    assert dashboard_proxy.grant(_APP) == (True, "")
    assert dashboard_proxy.granted(_APP) is True


def test_a_forged_installed_json_cannot_change_a_recorded_install(forged_apps):
    dashboard_proxy.record_install(
        _APP, version="0.9.4", registry="r", url="https://example.invalid/a.git"
    )
    assert dashboard_proxy.grant(_APP) == (True, "")
    (forged_apps / _APP / "installed.json").write_text(
        json.dumps({"name": _APP, "version": "6.6.6", "origin": "registry"}), encoding="utf-8"
    )
    assert dashboard_proxy.granted(_APP) is True
    assert dashboard_proxy.prompt_text(_APP)["version"] == "0.9.4"  # type: ignore[index]


def test_a_new_install_record_ends_the_grant_and_forget_removes_the_record(forged_apps):
    dashboard_proxy.record_install(_APP, version="1", registry="r", url="https://x.invalid/a.git")
    assert dashboard_proxy.grant(_APP) == (True, "")
    dashboard_proxy.record_install(_APP, version="1", registry="r", url="https://x.invalid/a.git")
    assert dashboard_proxy.granted(_APP) is False, "a reinstall is another install"
    dashboard_proxy.forget_install(_APP)
    assert dashboard_proxy.registry_verified(_APP) is False


def test_each_grant_gets_its_own_id_and_revoke_ends_it(records, installed):
    dashboard_proxy.set_gateway_secret("gateway-secret-in-words")
    assert dashboard_proxy.grant(_APP) == (True, "")
    first = dashboard_proxy.grant_claim(_APP)
    assert first and dashboard_proxy.grant_current(_APP, first)
    dashboard_proxy.revoke(_APP)
    assert dashboard_proxy.grant_claim(_APP) == ""
    assert not dashboard_proxy.grant_current(_APP, first)
    assert dashboard_proxy.grant(_APP) == (True, "")
    assert dashboard_proxy.grant_claim(_APP) != first
    assert not dashboard_proxy.grant_current(_APP, first)
    assert not dashboard_proxy.grant_current(_APP, "")


def test_process_state_leaks_in(monkeypatch):
    """Sets the module state the next test must not see (pairs with the one below)."""
    monkeypatch.setattr(dashboard_proxy, "granted", lambda _name: False)
    dashboard_proxy.set_gateway_secret("a-secret-an-earlier-test-left-behind")
    dashboard_proxy.note_refused_mint(_APP)
    assert dashboard_proxy._secret()


def test_process_state_is_reset_between_tests():
    assert dashboard_proxy._secret() == ""
    assert not dashboard_proxy._refused_mints


def _request(method: str, body: dict | None = None):
    async def _json():
        return body

    return SimpleNamespace(
        method=method,
        match_info={"name": _APP},
        get=lambda _k, default="": default,
        json=_json,
    )


@pytest.mark.asyncio
async def test_a_grant_needs_the_owner_and_the_confirm(records, installed, monkeypatch):
    async def _deny(_request, _operation):
        return routes.web.json_response({"code": "owner_only"}, status=403)

    monkeypatch.setattr(routes, "_owner_denial", _deny)
    assert (await routes.handle_dashboard_proxy(_request("PUT", {"confirm": True}))).status == 403
    assert dashboard_proxy.granted(_APP) is False


@pytest.mark.asyncio
async def test_a_grant_without_confirm_is_refused(records, installed, monkeypatch):
    async def _owner(_request, _operation):
        return None

    monkeypatch.setattr(routes, "_owner_denial", _owner)
    monkeypatch.setattr("kiro_crew.apps.manager.get_app", lambda _name: object())
    resp = await routes.handle_dashboard_proxy(_request("PUT", {}))
    assert resp.status == 400
    assert dashboard_proxy.granted(_APP) is False
    resp = await routes.handle_dashboard_proxy(_request("PUT", {"confirm": True}))
    assert resp.status == 200
    assert dashboard_proxy.granted(_APP) is True


def test_a_refused_mint_marks_the_app_until_it_holds_the_grant(records, installed):
    dashboard_proxy.set_gateway_secret("gateway-secret-in-words")
    assert dashboard_proxy.needs_access(_APP) is False
    dashboard_proxy.note_refused_mint(_APP)
    assert dashboard_proxy.needs_access(_APP) is True
    assert routes._view(_APP)["needs_access"] is True
    assert dashboard_proxy.grant(_APP) == (True, "")
    assert dashboard_proxy.needs_access(_APP) is False
    assert routes._view(_APP)["needs_access"] is False


def test_the_view_says_when_the_platform_cannot_bind_a_stand_in(records, installed, monkeypatch):
    assert routes._view(_APP)["platform_supported"] is True
    monkeypatch.setattr(dashboard_proxy, "_namespace_backend", lambda: False)
    view = routes._view(_APP)
    assert view["platform_supported"] is False
    assert view["offer"] is None


@pytest.mark.asyncio
async def test_the_restart_needs_the_owner_and_a_grant_then_restarts_the_backend(
    records, installed, monkeypatch
):
    calls: list[str] = []

    async def _owner(_request, _operation):
        return None

    def _stop(_name):
        calls.append("stop")
        return True

    def _start(_name):
        calls.append("start")
        return object()

    monkeypatch.setattr(routes, "_owner_denial", _owner)
    monkeypatch.setattr("kiro_crew.apps.manager.get_app", lambda _name: object())
    monkeypatch.setattr("kiro_crew.apps.backend.stop_app_backend", _stop)
    monkeypatch.setattr("kiro_crew.apps.backend.start_app_backend", _start)
    dashboard_proxy.set_gateway_secret("gateway-secret-in-words")
    resp = await routes.handle_dashboard_proxy_restart(_request("POST", None))
    assert resp.status == 409
    assert calls == []
    assert dashboard_proxy.grant(_APP) == (True, "")
    resp = await routes.handle_dashboard_proxy_restart(_request("POST", None))
    assert resp.status == 200
    assert json.loads(resp.text or "{}")["restarted"] is True
    assert calls == ["stop", "start"]
