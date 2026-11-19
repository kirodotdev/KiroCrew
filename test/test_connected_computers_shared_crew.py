"""Personal computer gates use the running Shared Crew preview, never observed people."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web

from kiro_crew import edition
from kiro_crew.config import live
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.connected_computers import flag, remote_routes, ws_route
from kiro_crew.dashboard.handlers import session_environments as handlers
from kiro_crew.dashboard.token_auth_paths import CONNECTOR_CONNECT_PATH, _exact_method_bypass
from kiro_crew.shared_crew import preview
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS


@pytest.fixture
def computer_config(monkeypatch):
    cfg = SimpleNamespace(
        dashboard=SimpleNamespace(feature_previews={flag.PREVIEW_NAME: True}),
        session_environments=SimpleNamespace(
            coder_url="", profiles=[], sprites_profiles=[], boxd_profiles=[], kiro_cloud_profiles=[]
        ),
    )
    monkeypatch.setattr(edition, "EDITION", "oss")
    monkeypatch.setattr(live, "snapshot", lambda: cfg)
    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr("kiro_crew.session_environment.session_environments_enabled", lambda: True)
    monkeypatch.delenv(preview.PREVIEW_ENV, raising=False)
    monkeypatch.setattr(preview, "_boot_stored", None)
    monkeypatch.setattr(preview, "read_stored", lambda: False)
    return cfg


@pytest.fixture(params=["environment", "stored", "unreadable"])
def shared_active(request, computer_config, monkeypatch):
    if request.param == "environment":
        monkeypatch.setenv(preview.PREVIEW_ENV, "1")
    elif request.param == "stored":
        monkeypatch.setattr(preview, "read_stored", lambda: True)
        assert preview.snapshot_at_start() is True
        # A pending Off must not open computers before the gateway restarts.
        monkeypatch.setattr(preview, "read_stored", lambda: False)
    else:

        def unreadable():
            raise RuntimeError("Shared Crew state unavailable")

        monkeypatch.setattr(preview, "preview_enabled", unreadable)
    return computer_config


@pytest.mark.parametrize("source", ["explicit", "live", "file"])
def test_shared_crew_closes_every_switch_reader(shared_active, monkeypatch, source):
    cfg = shared_active
    if source == "file":
        monkeypatch.setattr(live, "snapshot", lambda: None)
    passed = cfg if source == "explicit" else None
    assert flag.preview_on(passed) is False
    assert flag.remote_enabled(passed) is False
    assert flag.remote_refusal(passed) == flag.SHARED_CREW_REASON
    assert flag.local_enabled(passed) is False
    assert flag.local_switch(passed) is False
    assert flag.live_preview_on() is False
    with pytest.raises(flag.ConnectedComputersDisabled) as refusal:
        flag.require_remote(passed)
    assert str(refusal.value) == flag.SHARED_CREW_REASON
    with pytest.raises(flag.ConnectedComputersDisabled):
        flag.require_local(passed)
    assert cfg.dashboard.feature_previews == {flag.PREVIEW_NAME: True}


def test_shared_reason_precedes_disabled_and_developer_reasons(shared_active, monkeypatch):
    shared_active.dashboard.feature_previews.clear()
    monkeypatch.setattr("kiro_crew.session_environment.session_environments_enabled", lambda: False)
    assert flag.remote_refusal(shared_active) == flag.SHARED_CREW_REASON


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["GET", "HEAD", "POST"])
async def test_shared_connect_is_unserved_and_has_no_token_exemption(
    shared_active, monkeypatch, method
):
    connect = AsyncMock(side_effect=AssertionError("the connector must not be reached"))
    monkeypatch.setattr(ws_route, "api_connector_connect", connect)
    assert not _exact_method_bypass(CONNECTOR_CONNECT_PATH, method)
    with pytest.raises(web.HTTPNotFound):
        await asyncio.wait_for(
            remote_routes.api_connector_connect(SimpleNamespace(method=method)),
            FALLBACK_TIMEOUT_SECS,
        )
    connect.assert_not_awaited()


@pytest.mark.asyncio
async def test_shared_device_list_reports_why_without_reading_devices(shared_active, monkeypatch):
    monkeypatch.setattr(remote_routes, "_owner", AsyncMock(return_value=None))

    def no_devices():
        raise AssertionError("a shared Crew must not read personal computers")

    monkeypatch.setattr(remote_routes, "device_rows", no_devices)
    response = await asyncio.wait_for(
        remote_routes.api_devices(SimpleNamespace()), FALLBACK_TIMEOUT_SECS
    )
    assert response.status == 403
    assert json.loads(response.text) == {
        "code": "connected_computers_disabled",
        "error": flag.SHARED_CREW_REASON,
    }


def test_shared_picker_offers_no_computer(shared_active, monkeypatch):
    def no_devices():
        raise AssertionError("a shared Crew must not enumerate personal computers")

    monkeypatch.setattr(remote_routes, "device_rows", no_devices)
    assert handlers._profiles() == []


@pytest.mark.parametrize("stored", [False, True])
def test_inactive_shared_crew_keeps_the_computer_switch(computer_config, monkeypatch, stored):
    assert preview.snapshot_at_start() is False
    # A pending On is not the running Shared Crew state either.
    monkeypatch.setattr(preview, "read_stored", lambda: True)
    computer_config.dashboard.feature_previews[flag.PREVIEW_NAME] = stored
    assert flag.preview_on(computer_config) is stored
    assert flag.local_enabled(computer_config) is stored
    assert flag.local_switch(computer_config) is stored
    assert flag.live_preview_on() is stored
    assert flag.remote_enabled(computer_config) is stored
    assert flag.remote_refusal(computer_config) == (None if stored else flag.DISABLED_REASON)
    assert _exact_method_bypass(CONNECTOR_CONNECT_PATH, "GET") is stored


def test_inactive_shared_crew_keeps_the_edition_lock(computer_config, monkeypatch):
    monkeypatch.setattr(edition, "EDITION", "lts")
    monkeypatch.setattr("kiro_crew.edition_locks._logged", set())
    assert flag.preview_on(computer_config) is False
    assert flag.remote_refusal(computer_config) == flag.DISABLED_REASON


def test_inactive_shared_crew_keeps_computers_in_picker(computer_config, monkeypatch):
    monkeypatch.setattr(
        remote_routes,
        "device_rows",
        lambda: [
            {
                "device_id": "laptop",
                "label": "Laptop",
                "root": "/work",
                "connected": True,
                "label_is_shared": False,
                "status": "active",
            }
        ],
    )
    assert handlers._profiles() == [
        {
            "id": "laptop",
            "name": "Laptop",
            "provider": "connected_computer",
            "workdir": "/work",
            "connected": True,
            "label_is_shared": False,
        }
    ]
