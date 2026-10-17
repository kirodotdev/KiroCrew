"""``GET /api/artifacts?chat_folder=<id>``: the artifacts a sidebar folder's chats touched.

The folder scope is the union of the ``touched_by`` involvement scope over every
session filed in one chat folder or a folder nested under it, open or archived. It resolves the sessions
server-side and only ever narrows the unscoped listing: an unknown, empty or
malformed folder id is an empty view, never the whole library.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew import artifacts as art_mod
from kiro_crew.artifacts import ArtifactStore
from kiro_crew.dashboard.chat_folders import (
    archived_session_names_in_folders,
    chat_folder_subtree_ids,
    live_slot_names_in_folders,
)
from kiro_crew.dashboard.handlers.artifacts import api_artifacts_list


class _Log:
    """A conversation log double whose catalog is a fixed session list."""

    def __init__(self, sessions: list[dict[str, Any]]) -> None:
        self._sessions = sessions

    def list_sessions(self) -> list[dict[str, Any]]:
        return [dict(s) for s in self._sessions]


def _state(
    *,
    folders: list[str],
    live: dict[str, str] | None = None,
    archived: list[dict[str, Any]] | None = None,
    parents: dict[str, str] | None = None,
) -> SimpleNamespace:
    """Dashboard state with *folders* (``parents`` maps a child id to its parent),
    open slots ``{name: folder_id}`` and a catalog."""
    return SimpleNamespace(
        _folders=[
            {"id": fid, "name": fid, "parent_id": (parents or {}).get(fid, "")} for fid in folders
        ],
        _slots={name: SimpleNamespace(folder_id=fid) for name, fid in (live or {}).items()},
        conversation_log=_Log(archived or []),
        get_slot=lambda _name: None,
    )


def _request(state: Any, query: dict[str, str] | None = None) -> MagicMock:
    req = MagicMock()
    req.headers = {"X-Session-Key": "dashboard:test"}
    req.match_info = {}
    req.query = query or {}
    req.read = AsyncMock(return_value=b"")
    req.app = {"state": state}
    return req


def _slugs(resp) -> set[str]:
    return {a["slug"] for a in json.loads(resp.body)["artifacts"]}


@pytest.fixture
def store(tmp_path: Path, monkeypatch) -> ArtifactStore:
    """A real store with artifacts touched by sessions in and out of folder ``f1``.

    * ``made-live``   -- originated by open slot ``chat-1`` (filed in ``f1``)
    * ``read-arch``   -- only READ by archived session ``chat-2`` (filed in ``f1``)
    * ``made-slack``  -- originated by archived channel session ``slack:1700.1`` (``f1``)
    * ``nested``      -- originated by open slot ``chat-5``, filed in ``f1a`` (under ``f1``)
    * ``other-chat``  -- originated by ``chat-3``, filed in folder ``f2``
    * ``unfiled``     -- originated by ``chat-4``, filed nowhere
    * ``orphan``      -- no session at all
    """
    s = ArtifactStore(root=tmp_path / "artifacts")
    monkeypatch.setattr(art_mod, "_default_store", s)
    s.create(name="made-live", slug="made-live", content="x", session_key="chat-1")
    s.create(name="read-arch", slug="read-arch", content="x")
    s.record_impression("read-arch", by="agent", session_id="dashboard:chat-2")
    s.create(name="made-slack", slug="made-slack", content="x", session_key="slack:1700.1")
    s.create(name="nested", slug="nested", content="x", session_key="chat-5")
    s.create(name="other-chat", slug="other-chat", content="x", session_key="chat-3", tags=["t"])
    s.create(name="unfiled", slug="unfiled", content="x", session_key="chat-4")
    s.create(name="orphan", slug="orphan", content="x", tags=["t"])
    return s


ALL = {"made-live", "read-arch", "made-slack", "nested", "other-chat", "unfiled", "orphan"}


@pytest.fixture
def state() -> SimpleNamespace:
    return _state(
        folders=["f1", "f1a", "f2", "empty"],
        parents={"f1a": "f1"},
        live={"chat-1": "f1", "chat-5": "f1a", "chat-3": "f2", "chat-4": ""},
        archived=[
            {"key": "dashboard_chat-2", "folder_id": "f1"},
            {"key": "slack_1700.1", "folder_id": "f1"},
            {"key": "dashboard_chat-9", "folder_id": "gone"},
            {"key": "dashboard_chat-4"},
        ],
    )


class TestUnscopedListUnchanged:
    @pytest.mark.asyncio
    async def test_no_parameter_lists_the_whole_library(self, store, state) -> None:
        resp = await api_artifacts_list(_request(state))
        assert _slugs(resp) == ALL
        assert _slugs(resp) == {a.slug for a in store.list()}

    @pytest.mark.asyncio
    async def test_unknown_touched_by_slot_lists_nothing(self, store, state) -> None:
        resp = await api_artifacts_list(_request(state, {"touched_by": "chat-nope"}))
        assert _slugs(resp) == set()


class TestChatFolderScope:
    @pytest.mark.asyncio
    async def test_union_of_open_and_archived_sessions_in_the_folder(self, store, state) -> None:
        resp = await api_artifacts_list(_request(state, {"chat_folder": "f1"}))
        assert _slugs(resp) == {"made-live", "read-arch", "made-slack", "nested"}

    @pytest.mark.asyncio
    async def test_sub_folder_scope_excludes_its_parent(self, store, state) -> None:
        resp = await api_artifacts_list(_request(state, {"chat_folder": "f1a"}))
        assert _slugs(resp) == {"nested"}

    @pytest.mark.asyncio
    async def test_other_folder_gets_only_its_own_sessions(self, store, state) -> None:
        resp = await api_artifacts_list(_request(state, {"chat_folder": "f2"}))
        assert _slugs(resp) == {"other-chat"}

    @pytest.mark.asyncio
    async def test_result_is_a_subset_of_the_unscoped_list(self, store, state) -> None:
        unscoped = _slugs(await api_artifacts_list(_request(state)))
        for fid in ("f1", "f2", "empty", "gone", ""):
            scoped = _slugs(await api_artifacts_list(_request(state, {"chat_folder": fid})))
            assert scoped <= unscoped
            assert "orphan" not in scoped
            assert "unfiled" not in scoped

    @pytest.mark.asyncio
    async def test_combines_with_other_filters_by_intersection(self, store, state) -> None:
        tagged = await api_artifacts_list(_request(state, {"chat_folder": "f2", "tag": "t"}))
        assert _slugs(tagged) == {"other-chat"}
        none = await api_artifacts_list(_request(state, {"chat_folder": "f1", "tag": "t"}))
        assert _slugs(none) == set()
        both = await api_artifacts_list(
            _request(state, {"chat_folder": "f1", "touched_by": "chat-2"})
        )
        assert _slugs(both) == {"read-arch"}


class TestChatFolderNeverWidens:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "folder_id",
        [
            "nope",  # no such folder
            "gone",  # sessions still name it, but the folder is deleted
            "empty",  # the folder exists and holds no session
            "",  # present but empty
            "f1" + "x" * 500,  # over-long
            "../f1",
        ],
    )
    async def test_unresolvable_folder_lists_nothing(self, store, state, folder_id) -> None:
        resp = await api_artifacts_list(_request(state, {"chat_folder": folder_id}))
        assert resp.status == 200
        assert _slugs(resp) == set()

    @pytest.mark.asyncio
    async def test_no_dashboard_state_lists_nothing(self, store) -> None:
        resp = await api_artifacts_list(_request(None, {"chat_folder": "f1"}))
        assert _slugs(resp) == set()


class TestFolderMemberResolution:
    def test_subtree_holds_the_folder_and_its_descendants(self, state) -> None:
        assert chat_folder_subtree_ids(state, "f1") == {"f1", "f1a"}
        assert chat_folder_subtree_ids(state, "f1a") == {"f1a"}
        assert chat_folder_subtree_ids(state, "nope") == frozenset()
        assert chat_folder_subtree_ids(state, "") == frozenset()

    def test_subtree_survives_a_parent_cycle(self) -> None:
        st = _state(folders=["a", "b"], parents={"a": "b", "b": "a"})
        assert chat_folder_subtree_ids(st, "a") == {"a", "b"}

    def test_live_slots_filed_in_the_folders(self, state) -> None:
        assert live_slot_names_in_folders(state, frozenset({"f1"})) == {"chat-1"}
        assert live_slot_names_in_folders(state, frozenset({"f1", "f1a"})) == {"chat-1", "chat-5"}
        assert live_slot_names_in_folders(state, frozenset()) == set()

    def test_archived_dashboard_stems_fold_to_slot_names(self) -> None:
        st = _state(
            folders=["f1"],
            archived=[
                {"key": "dashboard_dashboard_chat-7", "folder_id": "f1"},
                {"key": "slack_1700.1", "folder_id": "f1"},
                {"key": "dashboard_chat-8", "folder_id": "f2"},
            ],
        )
        assert archived_session_names_in_folders(st, frozenset({"f1"})) == {
            "chat-7",
            "slack_1700.1",
        }


class TestStoreTouchedByAny:
    def test_none_does_not_scope(self, store) -> None:
        assert {a.slug for a in store.list(touched_by_any=None)} == ALL

    def test_empty_collection_matches_nothing(self, store) -> None:
        assert store.list(touched_by_any=[]) == []
        assert store.list(touched_by_any=[""]) == []

    def test_matches_any_session_in_either_spelling(self, store) -> None:
        got = {a.slug for a in store.list(touched_by_any=["dashboard:chat-1", "chat-2"])}
        assert got == {"made-live", "read-arch"}
