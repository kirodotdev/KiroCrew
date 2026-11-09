"""LTS build: every Feature Preview reads off on the gateway (RFC ``rfc-crew-lts-edition`` D12).

A stored ``dashboard.feature_previews.<name>: true`` (written by hand, by the CLI or by an
older build) turns nothing on in an LTS build: each server reader takes the switches
through ``edition_locks.feature_previews``, the config PATCH refuses a write, a change
card refuses to offer one, and the config read reports them all off with the locked
controls listed. The ``oss`` edition is unchanged.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew import edition_locks
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.config.sections import FEATURE_PREVIEW_FLAGS

ALL_ON = {name: True for name in FEATURE_PREVIEW_FLAGS}


@pytest.fixture(autouse=True)
def _fresh_lock_log():
    edition_locks.reset_logged()
    yield
    edition_locks.reset_logged()


@pytest.fixture
def lts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("kiro_crew.edition.EDITION", "lts")


@pytest.fixture
def all_on(tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"dashboard": {"feature_previews": ALL_ON}}), encoding="utf-8")
    with patch("kiro_crew.config.loader.config_path", return_value=cfg_path):
        yield cfg_path


def _readers() -> dict[str, bool]:
    from kiro_crew import crew_mode, dashboard_agentic, session_harness_preview
    from kiro_crew.connections import crew_oauth
    from kiro_crew.dashboard import stale_wake

    return {
        "crew_oauth": crew_oauth.enabled(),
        "mate_sessions": crew_mode.mate_sessions_hidden(KiroCrewConfig.load()),
        "dashboard_stale_fields": dashboard_agentic.stale_fields_enabled(),
        "dashboard_stale_wake": stale_wake.enabled(),
        "session_harness": session_harness_preview.session_harness_enabled(),
    }


def test_lts_server_readers_read_every_preview_off(lts, all_on):
    assert KiroCrewConfig.load().dashboard.feature_previews == ALL_ON  # stored value kept
    readers = _readers()
    assert not any(readers.values()), readers


def test_oss_server_readers_still_follow_the_switch(all_on):
    readers = _readers()
    assert all(readers.values()), readers


def test_feature_previews_helper():
    assert edition_locks.feature_previews(ALL_ON) is ALL_ON


def test_feature_previews_helper_lts(lts):
    assert edition_locks.feature_previews(ALL_ON) == {}
    assert edition_locks.config_path_locked("dashboard.feature_previews.explore")
    assert edition_locks.config_path_locked("dashboard.feature_previews")
    assert not edition_locks.config_path_locked("dashboard.feature_previews_other")


@web.middleware
async def _owner_identity(request, handler):
    request["user"] = "local-app"
    request["app"] = ""
    state = request.app.get("state")
    if state is not None:
        state.owner_id = ""
    return await handler(request)


def _patch_app() -> web.Application:
    from kiro_crew.dashboard.handlers import api_kirocrew_config_patch

    app = web.Application(middlewares=[_owner_identity])
    app["state"] = MagicMock()
    app.router.add_patch("/api/config/kirocrew", api_kirocrew_config_patch)
    return app


@pytest.mark.asyncio
@pytest.mark.parametrize("name", FEATURE_PREVIEW_FLAGS)
async def test_lts_patch_refuses_every_preview_switch(lts, all_on, name):
    before = all_on.read_bytes()
    async with TestClient(TestServer(_patch_app())) as c:
        resp = await c.patch(
            "/api/config/kirocrew",
            json={"path": f"dashboard.feature_previews.{name}", "value": False},
        )
        assert resp.status == 409, await resp.text()
        assert (await resp.json())["code"] == "locked_by_policy"
    assert all_on.read_bytes() == before


def _get_config() -> dict:
    from kiro_crew.dashboard.handlers import api_kirocrew_config

    req = MagicMock()
    req.method = "GET"
    return json.loads(asyncio.run(api_kirocrew_config(req)).body)


def test_lts_config_read_reports_previews_off_and_the_locks(lts, all_on, monkeypatch):
    # The masking pass reads the platform context, which an LTS boot installs and
    # this test does not; it is not what is under test here.
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.core_config_view._masked_config_dict",
        lambda cfg: cfg.to_dict(),
    )
    body = _get_config()
    assert body["dashboard"]["feature_previews"] == {}
    # The preview lock reaches the dashboard through the schema (lockedByPolicy);
    # this field carries only the browser-only control.
    assert body["locked_controls"] == ["debug_tools"]


def test_oss_config_read_is_unchanged(all_on):
    body = _get_config()
    assert body["dashboard"]["feature_previews"] == ALL_ON
    assert "locked_controls" not in body


@pytest.mark.asyncio
async def test_lts_patch_reply_keeps_the_edition_view(lts, all_on, monkeypatch):
    # The dashboard caches a PATCH reply in place of the GET body, so it must carry
    # the same lock; the masking pass needs a booted context (see above).
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.core_config_view._masked_config_dict",
        lambda cfg: cfg.to_dict(),
    )
    async with TestClient(TestServer(_patch_app())) as c:
        resp = await c.patch(
            "/api/config/kirocrew", json={"path": "dashboard.folder_sort", "value": "name"}
        )
        assert resp.status == 200, await resp.text()
        body = await resp.json()
    assert body["dashboard"]["feature_previews"] == {}
    # The preview lock reaches the dashboard through the schema (lockedByPolicy);
    # this field carries only the browser-only control.
    assert body["locked_controls"] == ["debug_tools"]


def test_lts_change_card_proposal_refuses_a_preview_switch(lts):
    from kiro_crew import change_card_catalog as catalog
    from kiro_crew.dashboard.change_cards_settings import resolve_setting

    with pytest.raises(catalog.CardCatalogError):
        resolve_setting({"path": "dashboard.feature_previews.explore", "value": True})


def test_oss_change_card_proposal_resolves_a_preview_switch():
    from kiro_crew.dashboard.change_cards_settings import resolve_setting

    assert resolve_setting({"path": "dashboard.feature_previews.explore"})["key"] == (
        "dashboard.feature_previews.explore"
    )


def test_lts_change_card_refuses_a_preview_switch(lts):
    from kiro_crew import change_card_catalog as catalog
    from kiro_crew.dashboard.change_cards_settings import setting_spec

    with pytest.raises(catalog.CardCatalogError):
        setting_spec("dashboard.feature_previews.explore")


def test_oss_change_card_offers_a_preview_switch():
    from kiro_crew.dashboard.change_cards_settings import setting_spec

    assert setting_spec("dashboard.feature_previews.explore") == {"type": "bool"}
