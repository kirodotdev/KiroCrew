"""The active-workspace resolver is per-session.

``_get_active_workspace`` resolves the REQUESTING session's own workspace
through a lessons-local ``_lesson_caller_slot_key`` and falls back to
``'default'`` only -- never to another session's workspace, however busy that
session is.

That lessons-local resolver matches a slot's ``effective_session_key`` so a
channel-born slot still resolves to its own workspace, but it is SEPARATE from
``session_control.caller_slot_key``: widening the latter would also change the
``authorize_target`` self-target guard that every session-control verb consults,
which this bug does not touch. The guard-unchanged test below pins that.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from chat_test_helpers import _make_state

from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.dashboard.handlers import _shared, cron
from kiro_crew.learn import Lesson, LessonStore

SLACK_KEY = "slack:1760000000.000001"


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    return _make_state(tmp_path)


def _slot(state, name: str, workspace: str, total_messages: int = 1, **kw):
    slot = state.get_or_create_slot(name, **kw)
    slot.workspace = workspace
    slot.total_messages = total_messages
    return slot


def test_requesting_session_workspace_wins_over_busiest_slot(state):
    quiet = _slot(state, "chat-quiet", "client-a", 3)
    busy = _slot(state, "chat-busy", "client-b", 500)
    assert _shared._get_active_workspace(state, slot_history_key(quiet)) == "client-a"
    assert _shared._get_active_workspace(state, slot_history_key(busy)) == "client-b"


def test_default_session_never_bleeds_into_busier_workspace(state):
    mine = _slot(state, "chat-mine", "default", 1)
    _slot(state, "chat-other", "client-b", 500)
    assert _shared._get_active_workspace(state, slot_history_key(mine)) == "default"


def test_no_session_or_unknown_slot_falls_back_to_default(state):
    _slot(state, "chat-other", "client-b", 500)
    assert _shared._get_active_workspace(state) == "default"
    assert _shared._get_active_workspace(state, "") == "default"
    assert _shared._get_active_workspace(state, "dashboard:ui") == "default"
    assert _shared._get_active_workspace(state, "dashboard:chat-gone") == "default"


def test_a_channel_born_slot_resolves_through_its_session_key(state):
    """A ``slack:<ts>`` key names the ``slack_<ts>`` slot, not a prefix-stripped name."""
    _slot(
        state,
        SLACK_KEY.replace(":", "_"),
        "client-a",
        linked_session_key=SLACK_KEY,
        channel_origin=True,
    )
    _slot(state, "chat-busy", "client-b", 500)
    assert _shared._get_active_workspace(state, SLACK_KEY) == "client-a"


def test_an_unbound_channel_slot_resolves_through_its_effective_key(state):
    """Surfaced without a binding, a channel slot's turns run as ``dashboard:<slot>``."""
    from kiro_crew.dashboard.chat_utils import effective_session_key

    slot = _slot(state, SLACK_KEY.replace(":", "_"), "client-a", channel_origin=True)
    _slot(state, "chat-busy", "client-b", 500)
    key = effective_session_key(slot)
    assert key != slot_history_key(slot)
    assert _shared._get_active_workspace(state, key) == "client-a"


def test_session_control_guard_is_unchanged_only_the_lesson_path_matches_effective_key(state):
    """The ``effective_session_key`` match lives ONLY in the lesson path.

    ``session_control.caller_slot_key`` -- which ``authorize_target`` and every
    session-control verb consult -- must stay byte-identical to main: it does
    NOT resolve an unbound channel slot by its ``dashboard:<slot>`` effective
    key. The lessons-local ``_lesson_caller_slot_key`` is what adds that match,
    so the two diverge for exactly this key, keeping the self-target guard off
    the widened path.
    """
    from kiro_crew.dashboard.chat_utils import effective_session_key
    from kiro_crew.dashboard.session_control import caller_slot_key

    slot = _slot(state, SLACK_KEY.replace(":", "_"), "client-a", channel_origin=True)
    key = effective_session_key(slot)
    assert key != slot_history_key(slot)
    # session-control guard path: unchanged -- the effective key resolves to "".
    assert caller_slot_key(state, key) == ""
    # lessons-local path: resolves it to the slot, so the workspace is found.
    assert _shared._lesson_caller_slot_key(state, key) == slot.key
    mine = _slot(state, "chat-mine", "client-a", 1)
    _slot(state, "chat-other", "client-b", 500)
    ws_store = MagicMock(name="ws_store")
    state.context_builder = MagicMock()
    state.context_builder.get_lessons_for = MagicMock(return_value=ws_store)

    store = cron._lesson_jsonl_store(
        state, "", "workspace", None, session_key=slot_history_key(mine)
    )

    assert store is ws_store
    state.context_builder.get_lessons_for.assert_called_once_with("client-a")


def test_explicit_workspace_still_wins(state):
    mine = _slot(state, "chat-mine", "client-a", 1)
    state.context_builder = MagicMock()
    state.context_builder.get_lessons_for = MagicMock(return_value=MagicMock())

    cron._lesson_jsonl_store(state, "", "workspace", "client-c", session_key=slot_history_key(mine))

    state.context_builder.get_lessons_for.assert_called_once_with("client-c")


async def _list(
    state,
    tmp_path,
    session_key: str | None,
    *,
    internal: bool = False,
    is_dashboard_user: bool = False,
) -> list[tuple[str, str | None]]:
    """GET /api/lessons over a global file and two workspace files.

    The caller's credential class is set through ``request.get``:
    ``internal=True`` is a validated ``X-Internal-Secret`` (kiro-cli / MCP
    agent); ``is_dashboard_user=True`` is the operator's own cookie/session
    browser caller; neither set models an app token (a scoped cookie whose
    ``is_dashboard_user`` the middleware set to ``False``).
    """
    stores = {}
    for name in ("global", "client-a", "client-b"):
        store = LessonStore(base_dir=tmp_path / name)
        store.save(Lesson(rule=f"rule in {name}", category="knowledge", ts="2026-01-01T00:00:00"))
        stores[name] = store
    state.lessons = stores["global"]
    state.context_builder = MagicMock()
    state.context_builder.get_lessons_for = MagicMock(side_effect=stores.__getitem__)
    state.context_builder.memory.vector_store = None

    request = MagicMock()
    request.app = {"state": state}
    request.headers = {} if session_key is None else {"X-Session-Key": session_key}
    request.query = {}
    request_env = {}
    if internal:
        request_env["internal_auth"] = True
    if is_dashboard_user:
        request_env["is_dashboard_user"] = True
    request.get = request_env.get
    configured = MagicMock(workspaces={"default": None, "client-a": None, "client-b": None})
    with (
        patch.object(cron, "_blocks_reads_session", return_value=False),
        patch.object(cron, "resolve_lesson_memory_store", new=AsyncMock(return_value=(None, None))),
        patch.object(cron, "_prepare_member_lesson_store", new=AsyncMock(return_value=None)),
        patch.object(cron, "_get_memory", return_value=MagicMock(vector_store=None)),
        patch.object(cron.KiroCrewConfig, "load", return_value=configured),
    ):
        resp = await cron.api_lessons(request)
    return [(row["rule"], row.get("workspace")) for row in json.loads(resp.text)["lessons"]]


@pytest.mark.asyncio
async def test_an_agent_session_lists_only_its_own_workspace(state, tmp_path):
    mine = _slot(state, "chat-mine", "client-a", 1)
    _slot(state, "chat-busy", "client-b", 500)
    rows = await _list(state, tmp_path, slot_history_key(mine))
    assert rows == [("rule in global", None), ("rule in client-a", "client-a")]


@pytest.mark.asyncio
async def test_an_unresolvable_session_lists_only_global(state, tmp_path):
    _slot(state, "chat-busy", "client-b", 500)
    rows = await _list(state, tmp_path, "dashboard:chat-gone")
    assert rows == [("rule in global", None)]


@pytest.mark.asyncio
@pytest.mark.parametrize("session_key", [None, "dashboard:ui"])
async def test_the_operator_browser_surface_lists_every_configured_workspace(
    state, tmp_path, session_key
):
    """The all-workspaces union is for the operator's own browser Memory tab.

    That surface sends no ``X-Internal-Secret`` and names no slot, and the
    token-auth middleware marks its pure cookie/session credential
    ``is_dashboard_user=True`` -- the positive signal this gate requires -- so
    every workspace row stays visible and deletable from it.
    """
    _slot(state, "chat-busy", "client-b", 500)
    rows = await _list(state, tmp_path, session_key, is_dashboard_user=True)
    assert rows == [
        ("rule in global", None),
        ("rule in client-a", "client-a"),
        ("rule in client-b", "client-b"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("session_key", [None, "dashboard:ui"])
async def test_an_internal_auth_keyless_caller_lists_only_global(state, tmp_path, session_key):
    """An internal-secret (agent) caller whose key resolved to none is NOT the
    operator: it must not reach another workspace's rows, so it gets global rows
    only -- the data-isolation harm this fix removes."""
    _slot(state, "chat-busy", "client-b", 500)
    rows = await _list(state, tmp_path, session_key, internal=True)
    assert rows == [("rule in global", None)]


@pytest.mark.asyncio
@pytest.mark.parametrize("session_key", [None, "dashboard:ui"])
async def test_an_app_token_keyless_caller_lists_only_global(state, tmp_path, session_key):
    """An app-token caller is NOT internal-auth and NOT the operator: the
    middleware marks it ``is_dashboard_user=False``. With no key it must get
    the global rows only, never the all-workspaces union -- the fail-safe the
    positive-signal gate buys over a bare ``internal_auth is not True`` test."""
    _slot(state, "chat-busy", "client-b", 500)
    rows = await _list(state, tmp_path, session_key)  # neither internal nor dashboard-user
    assert rows == [("rule in global", None)]


async def _create(state, tmp_path, session_key: str, body: dict):
    """POST /api/lessons on the JSONL path; returns (response, stores)."""
    stores = {name: LessonStore(base_dir=tmp_path / name) for name in ("global", "client-a")}
    state.lessons = stores["global"]
    state.context_builder = MagicMock()
    state.context_builder.get_lessons_for = MagicMock(side_effect=stores.__getitem__)

    request = MagicMock()
    request.app = {"state": state}
    request.headers = {"X-Session-Key": session_key}
    configured = MagicMock()
    configured.memory.persistence_enabled = True
    with (
        patch.object(cron, "read_bounded_json", new=AsyncMock(return_value=(body, None))),
        patch.object(cron, "_recognize_session", new=AsyncMock(return_value=None)),
        patch.object(cron, "_is_restricted_session", return_value=False),
        patch.object(cron, "resolve_lesson_memory_store", new=AsyncMock(return_value=(None, None))),
        patch.object(cron, "_prepare_member_lesson_store", new=AsyncMock(return_value=None)),
        patch.object(cron, "_get_memory", return_value=MagicMock(vector_store=None)),
        patch.object(cron.KiroCrewConfig, "load", return_value=configured),
    ):
        resp = await cron.api_lessons_create(request)
    return resp, stores


@pytest.mark.asyncio
async def test_a_slotless_workspace_write_is_refused_not_made_global(state, tmp_path):
    _slot(state, "chat-busy", "client-b", 500)
    resp, stores = await _create(
        state, tmp_path, "cron:nightly-digest", {"rule": "keep it local", "scope": "workspace"}
    )
    assert resp.status == 400
    assert json.loads(resp.text)["code"] == "workspace_required"
    assert stores["global"].load_all() == []


@pytest.mark.asyncio
async def test_a_slotted_workspace_write_lands_in_its_own_workspace(state, tmp_path):
    mine = _slot(state, "chat-mine", "client-a", 1)
    resp, stores = await _create(
        state, tmp_path, slot_history_key(mine), {"rule": "keep it local", "scope": "workspace"}
    )
    assert resp.status == 200
    assert [lesson.rule for lesson in stores["client-a"].load_all()] == ["keep it local"]
    assert stores["global"].load_all() == []
