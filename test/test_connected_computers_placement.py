"""Computer placement falls back locally while either placement switch is off."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew import session_environment as se
from kiro_crew.coder.session_launch import (
    CONNECTED_COMPUTER_PROVIDER_ID,
    SessionEnvironmentChoices,
)
from kiro_crew.config import live, paths
from kiro_crew.dashboard import chat_utils
from kiro_crew.dashboard.chat_turn import eager_spawn
from kiro_crew.dashboard.handlers import session_environments as handlers
from kiro_crew.dashboard.handlers import terminal_placement
from kiro_crew.providers import acp_lifecycle
from kiro_crew.providers.acp_runtime_start import _choices_file_may_exist
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

KEY = "dashboard:computer"
CHILD = "subagent:computer-child"


@pytest.fixture
def placement(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "data_home", lambda: tmp_path)
    monkeypatch.setenv("KIROCREW_DEV_MODE", "1")
    config = SimpleNamespace(
        dashboard=SimpleNamespace(feature_previews={"connected_computers": True})
    )
    monkeypatch.setattr(live, "snapshot", lambda: config)
    store = SessionEnvironmentChoices(se.choices_path())
    store.set(KEY, "laptop", CONNECTED_COMPUTER_PROVIDER_ID)
    return store, config


def _switches(monkeypatch, config, developer, preview):
    monkeypatch.setenv("KIROCREW_DEV_MODE", "1" if developer else "0")
    config.dashboard.feature_previews["connected_computers"] = preview


@pytest.mark.parametrize("developer,preview", [(False, True), (True, False), (False, False)])
@pytest.mark.parametrize("choice_state", ["readable", "missing", "unreadable"])
def test_disabled_computer_resolves_locally(
    placement, monkeypatch, developer, preview, choice_state
):
    store, config = placement
    if choice_state == "missing":
        store.path.unlink()
    elif choice_state == "unreadable":
        store.path.write_text("{not json", encoding="utf-8")
    _switches(monkeypatch, config, developer, preview)
    read = MagicMock(side_effect=AssertionError("disabled computer choice was read"))
    monkeypatch.setattr(SessionEnvironmentChoices, "_read", read)
    assert store.resolve_choice(KEY) is None
    assert not se.session_runs_remotely(KEY)
    assert not se.session_attached_remotely(KEY)
    assert not acp_lifecycle._bound_remotely(KEY)
    assert se.refuse_local_only_tool(KEY, "kirocrew-core", "file_send") is None
    if not developer:
        assert not _choices_file_may_exist(KEY)
    read.assert_not_called()


@pytest.mark.parametrize("developer,preview", [(False, True), (True, False), (False, False)])
@pytest.mark.parametrize("continuation", [False, True])
def test_disabled_computer_inheritance_is_local_without_rewriting(
    placement, monkeypatch, developer, preview, continuation
):
    store, config = placement
    if continuation:
        assert store.inherit(CHILD, KEY) == "laptop"
        store.set(KEY, None)
    before = store.path.read_bytes()
    _switches(monkeypatch, config, developer, preview)
    assert store.inherit(CHILD, KEY) is None
    assert store.path.read_bytes() == before
    assert store.may_be_bound(CHILD) is continuation


@pytest.mark.asyncio
@pytest.mark.parametrize("developer,preview", [(False, True), (True, False), (False, False)])
async def test_disabled_computer_allows_local_prewarm(placement, monkeypatch, developer, preview):
    _, config = placement
    _switches(monkeypatch, config, developer, preview)
    assert not await asyncio.wait_for(
        eager_spawn._runs_on_a_session_environment(KEY), FALLBACK_TIMEOUT_SECS
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("developer,preview", [(False, True), (True, False), (False, False)])
async def test_disabled_computer_placement_get_reports_local(
    placement, monkeypatch, developer, preview
):
    _, config = placement
    _switches(monkeypatch, config, developer, preview)
    monkeypatch.setattr(handlers, "require_owner_dashboard_request", AsyncMock(return_value=None))
    monkeypatch.setattr(handlers, "effective_session_key", lambda slot: KEY)
    monkeypatch.setattr(handlers, "_profiles", lambda: [])
    monkeypatch.setattr(handlers, "_choice_locked", AsyncMock(return_value=False))
    request = make_mocked_request("GET", "/environment", match_info={"slot": "computer"})
    request.app["state"] = SimpleNamespace(_slots={"computer": SimpleNamespace(key="computer")})
    response = await asyncio.wait_for(
        handlers.api_chat_slot_environment_get(request), FALLBACK_TIMEOUT_SECS
    )
    assert response.status == 200
    body = json.loads(response.body)
    assert body["profile"] is None and body["provider"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "developer,preview", [(False, True), (True, False), (False, False), (True, True)]
)
async def test_local_terminal_follows_computer_placement_switches(
    placement, monkeypatch, developer, preview
):
    _, config = placement
    _switches(monkeypatch, config, developer, preview)
    monkeypatch.setattr(chat_utils, "effective_session_key", lambda slot: KEY)
    request = make_mocked_request("GET", "/terminal", app=web.Application())
    request.app["state"] = SimpleNamespace(_slots={"computer": SimpleNamespace(key="computer")})
    why, code = await asyncio.wait_for(
        terminal_placement._local_terminal_refusal(request, "computer"), FALLBACK_TIMEOUT_SECS
    )
    if developer and preview:
        assert code == "terminal_wrong_machine" and "wrong machine" in why
    else:
        assert (why, code) == ("", "")


@pytest.mark.parametrize("choice_state", ["missing", "missing_row", "unreadable"])
def test_enabled_computer_marker_refuses_lost_choice(placement, choice_state):
    store, _ = placement
    if choice_state == "missing":
        store.path.unlink()
    elif choice_state == "missing_row":
        store.path.write_text(json.dumps({"version": 1, "sessions": {}}), encoding="utf-8")
    else:
        store.path.write_text("{not json", encoding="utf-8")
    assert store._computer_marker(KEY).is_file()
    with pytest.raises(se.SessionEnvironmentUnavailable):
        store.resolve_choice(KEY)
    assert se.session_runs_remotely(KEY)
    assert not se.session_attached_remotely(KEY)


def test_reenabling_computer_restores_saved_placement(placement, monkeypatch):
    store, config = placement
    _switches(monkeypatch, config, True, False)
    assert store.resolve_choice(KEY) is None
    _switches(monkeypatch, config, True, True)
    assert store.resolve_choice(KEY) == ("laptop", KEY, CONNECTED_COMPUTER_PROVIDER_ID)


def test_enabled_typed_child_missing_row_cannot_be_reinherited(placement):
    store, _ = placement
    assert store.inherit(CHILD, KEY) == "laptop"
    rows = json.loads(store.path.read_text(encoding="utf-8"))
    del rows["sessions"][CHILD]
    store.path.write_text(json.dumps(rows), encoding="utf-8")
    before = store.path.read_bytes()
    with pytest.raises(se.SessionEnvironmentUnavailable):
        store.inherit(CHILD, KEY)
    assert store.path.read_bytes() == before


def test_clear_computer_choice_removes_both_markers(placement):
    store, _ = placement
    store.set(KEY, None)
    assert not store._marker(KEY).exists()
    assert not store._computer_marker(KEY).exists()
    assert store.resolve_choice(KEY) is None


def test_pruning_ended_computer_removes_both_markers(placement, monkeypatch, manual_clock):
    from kiro_crew.coder import session_launch

    manual_clock.install(monkeypatch, session_launch)
    store, _ = placement
    assert store.inherit(CHILD, KEY) == "laptop"
    assert store.forget_inherited(CHILD)
    manual_clock.advance(session_launch.ENDED_ROW_TTL_SECS + 1)
    assert store.inherit("subagent:next-computer", KEY) == "laptop"
    assert not store._computer_marker(CHILD).exists()
    assert not store._marker(CHILD).exists()
