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
    return record


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
    for words in ("full dashboard control", "running agents", "credential and admin", "locally"):
        assert words in offer["text"]


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
