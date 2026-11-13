"""A persisted computer binding cannot become local when either switch is off."""

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
def test_disabled_computer_still_counts_as_remote(placement, monkeypatch, developer, preview):
    store, config = placement
    _switches(monkeypatch, config, developer, preview)
    assert store.resolve_choice(KEY) == ("laptop", KEY, CONNECTED_COMPUTER_PROVIDER_ID)
    assert se.session_runs_remotely(KEY)
    assert se.session_attached_remotely(KEY)
    assert acp_lifecycle._bound_remotely(KEY)
    assert _choices_file_may_exist(KEY)
    assert se.refuse_local_only_tool(KEY, "kirocrew-core", "file_send") is not None


@pytest.mark.parametrize("developer,preview", [(False, True), (True, False), (False, False)])
@pytest.mark.parametrize("continuation", [False, True])
def test_disabled_computer_inheritance_refuses_without_rewriting(
    placement, monkeypatch, developer, preview, continuation
):
    store, config = placement
    if continuation:
        assert store.inherit(CHILD, KEY) == "laptop"
        store.set(KEY, None)
    before = store.path.read_bytes()
    _switches(monkeypatch, config, developer, preview)
    with pytest.raises(se.SessionEnvironmentUnavailable, match="turned off|Session Environments"):
        store.inherit(CHILD, KEY)
    assert store.path.read_bytes() == before
    assert store.may_be_bound(CHILD) is continuation


@pytest.mark.parametrize("provider", ["coder", "sprites", "boxd", "kiro_cloud"])
@pytest.mark.parametrize("unreadable", [False, True])
def test_legacy_providers_keep_developer_off_local_behavior(
    placement, monkeypatch, provider, unreadable
):
    store, config = placement
    store.set(KEY, "dev", provider)
    if unreadable:
        store.path.write_text("{not json", encoding="utf-8")
    _switches(monkeypatch, config, False, False)
    assert store.resolve_choice(KEY) is None
    assert store.inherit(CHILD, KEY) is None
    assert not se.session_runs_remotely(KEY)
    assert not se.session_attached_remotely(KEY)
    assert not acp_lifecycle._bound_remotely(KEY)
    assert not _choices_file_may_exist(KEY)


def test_never_bound_developer_off_reads_no_choices(placement, monkeypatch):
    store, config = placement
    _switches(monkeypatch, config, False, False)
    read = MagicMock(side_effect=AssertionError("unbound chat read the store"))
    monkeypatch.setattr(SessionEnvironmentChoices, "_read", read)
    local = "dashboard:local"
    assert store.resolve_choice(local) is None
    assert store.inherit("subagent:local", local) is None
    assert not se.session_runs_remotely(local)
    assert not se.session_attached_remotely(local)
    assert not _choices_file_may_exist(local)
    assert not _choices_file_may_exist()
    read.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("developer,preview", [(False, True), (True, False), (False, False)])
async def test_disabled_computer_skips_prewarm(placement, monkeypatch, developer, preview):
    _, config = placement
    _switches(monkeypatch, config, developer, preview)
    assert await asyncio.wait_for(
        eager_spawn._runs_on_a_session_environment(KEY), FALLBACK_TIMEOUT_SECS
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("developer,preview", [(False, True), (True, False), (False, False)])
async def test_disabled_computer_refuses_legacy_engine(placement, monkeypatch, developer, preview):
    _, config = placement
    _switches(monkeypatch, config, developer, preview)
    provider = MagicMock(memory_mode="persistent", _disposable_work_dir=False)
    provider.is_acp_runtime_backend = False
    provider._client._session_key = KEY
    provider._client.ensure_ready = AsyncMock(side_effect=AssertionError("local engine started"))
    provider._recover_effort_overlay = AsyncMock()
    from kiro_crew.acp.runtime import AcpRuntimeError

    with pytest.raises(AcpRuntimeError, match="remote session environments need"):
        await asyncio.wait_for(acp_lifecycle.start(provider), FALLBACK_TIMEOUT_SECS)
    provider._client.ensure_ready.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("developer,preview", [(False, True), (True, False), (False, False)])
async def test_disabled_computer_placement_get_refuses_local_fallback(
    placement, monkeypatch, developer, preview
):
    _, config = placement
    _switches(monkeypatch, config, developer, preview)
    monkeypatch.setattr(handlers, "require_owner_dashboard_request", AsyncMock(return_value=None))
    monkeypatch.setattr(handlers, "effective_session_key", lambda slot: KEY)
    no_profiles = MagicMock(side_effect=AssertionError("disabled placement looked up profiles"))
    monkeypatch.setattr(handlers, "_profiles", no_profiles)
    request = make_mocked_request("GET", "/environment", match_info={"slot": "computer"})
    request.app["state"] = SimpleNamespace(_slots={"computer": SimpleNamespace(key="computer")})
    response = await asyncio.wait_for(
        handlers.api_chat_slot_environment_get(request), FALLBACK_TIMEOUT_SECS
    )
    assert response.status == 409
    body = json.loads(response.body)
    assert body["code"] == "connected_computer"
    assert "Connected computers" in body["error"]
    no_profiles.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "developer,preview", [(False, True), (True, False), (False, False), (True, True)]
)
async def test_disabled_computer_local_terminal_refuses(placement, monkeypatch, developer, preview):
    _, config = placement
    _switches(monkeypatch, config, developer, preview)
    monkeypatch.setattr(chat_utils, "effective_session_key", lambda slot: KEY)
    request = make_mocked_request("GET", "/terminal", app=web.Application())
    request.app["state"] = SimpleNamespace(_slots={"computer": SimpleNamespace(key="computer")})
    why, code = await asyncio.wait_for(
        terminal_placement._local_terminal_refusal(request, "computer"), FALLBACK_TIMEOUT_SECS
    )
    assert code == "terminal_wrong_machine"
    assert "wrong machine" in why


@pytest.mark.asyncio
async def test_unknown_chat_keeps_developer_off_terminal_behavior(placement, monkeypatch):
    _, config = placement
    _switches(monkeypatch, config, False, False)
    request = make_mocked_request("GET", "/terminal", app=web.Application())
    request.app["state"] = SimpleNamespace(_slots={})
    assert await terminal_placement._local_terminal_refusal(request, "unknown") == ("", "")


@pytest.fixture(params=["missing", "missing_row", "invalid_json", "unreadable"])
def lost_computer_choice(placement, monkeypatch, request):
    store, config = placement
    if request.param == "missing":
        store.path.unlink()
    elif request.param == "missing_row":
        store.path.write_text(json.dumps({"version": 1, "sessions": {}}), encoding="utf-8")
    elif request.param == "invalid_json":
        store.path.write_text("{not json", encoding="utf-8")
    else:
        path_type = type(store.path)
        original = path_type.read_text

        def read(path, *args, **kwargs):
            if path == store.path:
                raise PermissionError("choices unreadable")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(path_type, "read_text", read)
    _switches(monkeypatch, config, False, False)
    return store, config


def test_typed_computer_marker_refuses_lost_choice(lost_computer_choice):
    store, _ = lost_computer_choice
    assert store._computer_marker(KEY).is_file()
    assert store._marker(KEY).read_bytes() == b""
    with pytest.raises(se.SessionEnvironmentUnavailable):
        store.resolve_choice(KEY)
    with pytest.raises(se.SessionEnvironmentUnavailable):
        store.inherit(CHILD, KEY)
    assert se.session_runs_remotely(KEY)
    assert acp_lifecycle._bound_remotely(KEY)
    assert _choices_file_may_exist(KEY)
    assert se.refuse_local_only_tool(KEY, "kirocrew-core", "file_send")
    # Unknown attachment must not grant remote-server privileges or a cheaper spawn floor.
    assert not se.session_attached_remotely(KEY)
    with se.known_placement(KEY, se.session_runs_remotely(KEY)):
        assert se.refuse_local_only_tool(KEY, "kirocrew-core", "file_send")


@pytest.mark.asyncio
async def test_lost_computer_choice_refuses_local_surfaces(lost_computer_choice):
    from kiro_crew.coder.session_browse import _resolve
    from kiro_crew.coder.session_launch import prepare_for_session

    assert await asyncio.wait_for(
        eager_spawn._runs_on_a_session_environment(KEY), FALLBACK_TIMEOUT_SECS
    )
    with pytest.raises(se.SessionEnvironmentUnavailable):
        await asyncio.wait_for(prepare_for_session(KEY), FALLBACK_TIMEOUT_SECS)
    with pytest.raises(se.SessionEnvironmentUnavailable):
        await asyncio.wait_for(_resolve(KEY), FALLBACK_TIMEOUT_SECS)
    app = web.Application()
    app["state"] = SimpleNamespace(
        _slots={"computer": SimpleNamespace(key="computer", linked_session_key=KEY)}
    )
    request = make_mocked_request("GET", "/terminal", app=app)
    why, code = await asyncio.wait_for(
        terminal_placement._local_terminal_refusal(request, "computer"), FALLBACK_TIMEOUT_SECS
    )
    assert why
    assert code == "terminal_placement_unavailable"


@pytest.mark.parametrize("provider", ["coder", "sprites", "boxd", "kiro_cloud"])
def test_legacy_marker_missing_store_stays_local(placement, monkeypatch, provider):
    store, config = placement
    store.set(KEY, "dev", provider)
    assert store._marker(KEY).read_bytes() == b""
    assert not store._computer_marker(KEY).exists()
    store.path.unlink()
    _switches(monkeypatch, config, False, False)
    assert store.resolve_choice(KEY) is None
    assert store.inherit(CHILD, KEY) is None
    assert not se.session_runs_remotely(KEY)
    assert not _choices_file_may_exist(KEY)


@pytest.mark.parametrize("missing_row", [False, True])
def test_clear_computer_choice_removes_both_markers(placement, monkeypatch, missing_row):
    store, config = placement
    if missing_row:
        store.path.unlink()
    _switches(monkeypatch, config, False, False)
    store.set(KEY, None)
    assert not store._marker(KEY).exists()
    assert not store._computer_marker(KEY).exists()
    assert not store.may_be_bound(KEY)
    assert store.resolve_choice(KEY) is None


def test_inherited_computer_marker_survives_parent_clear(placement, monkeypatch):
    store, config = placement
    assert store.inherit(CHILD, KEY) == "laptop"
    store.set(KEY, None)
    assert not store._computer_marker(KEY).exists()
    assert store._computer_marker(CHILD).is_file()
    store.path.unlink()
    _switches(monkeypatch, config, False, False)
    with pytest.raises(se.SessionEnvironmentUnavailable):
        store.inherit(CHILD, KEY)


def test_typed_child_missing_row_cannot_be_reinherited(placement):
    store, _ = placement
    assert store.inherit(CHILD, KEY) == "laptop"
    rows = json.loads(store.path.read_text(encoding="utf-8"))
    del rows["sessions"][CHILD]
    store.path.write_text(json.dumps(rows), encoding="utf-8")
    before = store.path.read_bytes()
    with pytest.raises(se.SessionEnvironmentUnavailable):
        store.inherit(CHILD, KEY)
    assert store.path.read_bytes() == before


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


def test_interrupted_computer_write_keeps_typed_witness(placement, monkeypatch):
    store, config = placement
    store.set(KEY, None)
    mark = MagicMock(side_effect=OSError("legacy marker write interrupted"))
    monkeypatch.setattr(store, "_mark_bound", mark)
    with pytest.raises(OSError, match="interrupted"):
        store.set(KEY, "laptop", CONNECTED_COMPUTER_PROVIDER_ID)
    assert store._computer_marker(KEY).is_file()
    assert not store._marker(KEY).exists()
    assert store.may_be_bound(KEY)
    _switches(monkeypatch, config, False, False)
    with pytest.raises(se.SessionEnvironmentUnavailable):
        store.resolve_choice(KEY)
    # A developer-on startup also sees the witness when the choices file is lost.
    store.path.unlink()
    _switches(monkeypatch, config, True, False)
    assert _choices_file_may_exist(KEY)


def test_failed_computer_clear_preserves_witness(placement, monkeypatch):
    from kiro_crew.coder import session_launch

    store, config = placement
    original = session_launch.atomic_write

    def write(path, *args, **kwargs):
        if path == store.path:
            raise OSError("choices write interrupted")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(session_launch, "atomic_write", write)
    with pytest.raises(OSError, match="interrupted"):
        store.set(KEY, None)
    assert store._computer_marker(KEY).is_file()
    _switches(monkeypatch, config, False, False)
    assert store.resolve_choice(KEY) == ("laptop", KEY, CONNECTED_COMPUTER_PROVIDER_ID)
