"""Resolving a calling session to its project knowledge namespace.

Pins `dashboard.project_knowledge.resolve_session_project_namespace`: the
server-side step that turns a verified session key into the namespace of the
project the session is filed in. Covers a live tab, an archived/revived session
with no open tab, a sub-agent inheriting its parent's project, a session filed
in no folder, and the corrupt-roster degrade path.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kiro_crew.dashboard.project_knowledge import resolve_session_project_namespace


class _FakeLog:
    """A keyed session log that mirrors the real one's row shape.

    ``list_sessions(keys=[k])`` returns the row for k with its ``key`` field set
    to the file STEM (``dashboard_abc`` for ``dashboard:abc``), not the original
    key -- the behaviour the resolver must take positionally rather than match
    on, so an archived session is not silently dropped.
    """

    def __init__(self, sessions):
        # sessions: {session_key: folder_id}
        self._sessions = sessions

    def list_sessions(self, *, keys=None):
        want = list(self._sessions) if keys is None else [k for k in keys if k in self._sessions]
        out = []
        for k in want:
            stem = k.replace(":", "_")  # how the real log keys the returned row
            out.append({"key": stem, "folder_id": self._sessions[k]})
        return out


class _FakeSubagents:
    """Minimal roster: maps a run key to its parent session key."""

    def __init__(self, parents):
        self._parents = parents  # {child_session_key: parent_session_key}

    @property
    def all_agents(self):
        out = []
        for child, parent in self._parents.items():
            agent_id = child.removeprefix("subagent:")
            out.append(SimpleNamespace(id=agent_id, conversation_key="", parent_session_key=parent))
        return out


class _FakeState:
    def __init__(self, *, slots=None, folders=None, sessions=None, parents=None):
        self._slots = slots or {}
        self._folders = folders or []
        self.conversation_log = _FakeLog(sessions or {})
        self.subagents = _FakeSubagents(parents or {}) if parents is not None else None

    def get_slot(self, name):
        return self._slots.get(name)


_FOLDERS = [
    {"id": "proj"},
    {"id": "subteam", "parent_id": "proj"},
    {"id": "other"},
]


@pytest.mark.asyncio
async def test_live_slot_in_a_folder_resolves_to_its_project_namespace():
    state = _FakeState(slots={"abc": SimpleNamespace(folder_id="proj")}, folders=_FOLDERS)
    assert await resolve_session_project_namespace(state, "dashboard:abc") == "project:proj"


@pytest.mark.asyncio
async def test_nested_live_slot_inherits_the_root_project():
    state = _FakeState(slots={"abc": SimpleNamespace(folder_id="subteam")}, folders=_FOLDERS)
    assert await resolve_session_project_namespace(state, "dashboard:abc") == "project:proj"


@pytest.mark.asyncio
async def test_session_filed_in_no_folder_has_no_project_namespace():
    state = _FakeState(slots={"abc": SimpleNamespace(folder_id="")}, folders=_FOLDERS)
    assert await resolve_session_project_namespace(state, "dashboard:abc") == ""


@pytest.mark.asyncio
async def test_archived_session_without_an_open_tab_uses_the_on_disk_record():
    # No live slot; the folder_id lives on the durable session record, whose
    # returned row is keyed by the file STEM. The resolver must still find it --
    # the revived-session path, and the archived-key regression Opus flagged.
    state = _FakeState(slots={}, folders=_FOLDERS, sessions={"dashboard:abc": "proj"})
    assert await resolve_session_project_namespace(state, "dashboard:abc") == "project:proj"


@pytest.mark.asyncio
async def test_subagent_inherits_its_parents_project():
    state = _FakeState(
        slots={"abc": SimpleNamespace(folder_id="proj")},
        folders=_FOLDERS,
        parents={"subagent:run1": "dashboard:abc"},
    )
    assert await resolve_session_project_namespace(state, "subagent:run1") == "project:proj"


@pytest.mark.asyncio
async def test_unknown_subagent_run_resolves_to_no_project():
    state = _FakeState(slots={}, folders=_FOLDERS, parents={})
    assert await resolve_session_project_namespace(state, "subagent:ghost") == ""


@pytest.mark.asyncio
async def test_empty_session_key_resolves_to_no_project():
    state = _FakeState(folders=_FOLDERS)
    assert await resolve_session_project_namespace(state, "") == ""


@pytest.mark.asyncio
async def test_folder_id_pointing_at_a_deleted_folder_still_resolves_stably():
    # The slot names a folder absent from the tree: the session still gets a
    # stable namespace rather than silently falling to global scope.
    state = _FakeState(slots={"abc": SimpleNamespace(folder_id="ghost")}, folders=_FOLDERS)
    assert await resolve_session_project_namespace(state, "dashboard:abc") == "project:ghost"


@pytest.mark.asyncio
async def test_different_sessions_in_different_projects_do_not_share_a_namespace():
    state = _FakeState(
        slots={
            "a": SimpleNamespace(folder_id="proj"),
            "b": SimpleNamespace(folder_id="other"),
        },
        folders=_FOLDERS,
    )
    ns_a = await resolve_session_project_namespace(state, "dashboard:a")
    ns_b = await resolve_session_project_namespace(state, "dashboard:b")
    assert ns_a == "project:proj"
    assert ns_b == "project:other"
    assert ns_a != ns_b


class _FakeRequest:
    def __init__(self, *, internal_auth, session_key, state):
        self._vals = {"internal_auth": internal_auth}
        self.headers = {"X-Session-Key": session_key}
        self.app = {"state": state}

    def get(self, key, default=None):
        return self._vals.get(key, default)


@pytest.mark.asyncio
async def test_route_refuses_a_caller_without_internal_auth():
    # A local_only=False deployment reclassifies strict paths as mixed, so a
    # cookie-authed browser could reach the handler and name any session as
    # itself. The handler re-asserts internal_auth and refuses otherwise.
    from kiro_crew.dashboard.project_knowledge import session_project_namespace_route

    state = _FakeState(slots={"abc": SimpleNamespace(folder_id="proj")}, folders=_FOLDERS)
    resp = await session_project_namespace_route(
        _FakeRequest(internal_auth=False, session_key="dashboard:abc", state=state)
    )
    assert resp.status == 403


@pytest.mark.asyncio
async def test_route_returns_only_the_callers_own_namespace():
    import json

    from kiro_crew.dashboard.project_knowledge import session_project_namespace_route

    state = _FakeState(slots={"abc": SimpleNamespace(folder_id="proj")}, folders=_FOLDERS)
    resp = await session_project_namespace_route(
        _FakeRequest(internal_auth=True, session_key="dashboard:abc", state=state)
    )
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body == {"namespace": "project:proj"}
