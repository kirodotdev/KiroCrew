"""The memory graph's live activity frame: ids only, to owner sockets only."""

import asyncio
import json
import threading
from contextvars import ContextVar
from types import SimpleNamespace

import pytest
from member_memory_helpers import env as _member_env
from member_memory_helpers import request

from kiro_crew import memory_graph_activity as activity
from kiro_crew.dashboard.handlers import memory_member

env = _member_env


class _Socket:
    def __init__(self, closed: bool = False) -> None:
        self.closed = closed


class _Sockets:
    """An owner socket set and the frames ``broadcast_ws_owners`` was handed."""

    def __init__(self, *, open_sockets: int = 1) -> None:
        self.clients = {_Socket() for _ in range(open_sockets)}
        self.frames: list[tuple[str, dict]] = []
        self.sent = asyncio.Event()

    def attach(self, state) -> None:
        state._owner_ws_clients = self.clients

        def send(kind, data):
            self.frames.append((kind, data))
            self.sent.set()

        state.broadcast_ws_owners = send


def test_a_frame_names_ids_kind_store_and_time_and_nothing_else():
    frame = activity.activity_frame(
        "member-alice", activity.RECALL, ["user.a", "", None, "user.a", "lesson.b"], now=12.5
    )
    assert frame == {
        "store": "member-alice",
        "kind": "recall",
        "ids": ["user.a", "lesson.b"],
        "ts": 12.5,
    }
    assert activity.activity_frame("", activity.SESSION_START, []) is None
    assert activity.activity_frame("default", activity.RECALL, ["user.a"])["store"] == ""
    with pytest.raises(ValueError):
        activity.activity_frame("", "search", ["user.a"])


def test_a_frame_is_capped():
    frame = activity.activity_frame("", activity.RECALL, [f"user.n{i}" for i in range(500)])
    assert frame is not None and len(frame["ids"]) == activity.IDS_CAP


def test_nothing_is_sent_without_an_open_owner_socket():
    state = SimpleNamespace()
    sockets = _Sockets(open_sockets=0)
    sockets.attach(state)
    state._owner_ws_clients = {_Socket(closed=True)}
    activity.announce(state, "", activity.RECALL, ["user.a"])
    assert sockets.frames == []
    # A state with no owner socket set at all is the same.
    activity.announce(SimpleNamespace(), "", activity.RECALL, ["user.a"])


def test_recalled_ids_take_lessons_facts_and_experiences_by_their_graph_ids():
    payload = {
        "retrieval": {
            "facts": [{"key": "user.a", "snippet": "x"}, {"id": "markdown:notes.md"}],
            "episodes": [{"id": "ep-1", "text": "y"}],
        }
    }
    assert activity.recalled_ids(payload, ["lesson.z"]) == ["lesson.z", "user.a", "ep-1"]
    assert activity.recalled_ids(None) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["", "member-alice"])
async def test_a_recall_tells_the_owner_graph_which_memories_it_read_and_no_text(env, name):
    tier = env.tiers[name]
    secret = "PostgreSQL database decision PRIVATE_TEXT_SENTINEL"
    assert tier.write_episodic(secret, importance=1.0, defer_embedding=True)
    episode = tier.get_episodic_list()[0]["id"]
    sockets = _Sockets()
    sockets.attach(env.state)

    response = await memory_member.api_memory_recall(
        request(env, query={"q": "PostgreSQL database", "store": name or "default"}, owner=True)
    )

    assert response.status == 200
    await asyncio.wait_for(sockets.sent.wait(), timeout=30)
    assert [kind for kind, _ in sockets.frames] == [activity.FRAME]
    frame = sockets.frames[0][1]
    assert frame["kind"] == "recall" and frame["store"] == name
    assert episode in frame["ids"]
    # Only ids and numeric layout identities are added, never memory text.
    assert set(frame) <= {"store", "kind", "ids", "ts", "layout", "clusters"}
    assert {"store", "kind", "ids", "ts"} <= set(frame)
    assert "PRIVATE_TEXT_SENTINEL" not in json.dumps(frame)


@pytest.mark.asyncio
async def test_a_recall_with_no_owner_dashboard_open_sends_nothing(env):
    tier = env.tiers[""]
    assert tier.write_episodic("PostgreSQL database decision", importance=1.0, defer_embedding=True)
    sockets = _Sockets(open_sockets=0)
    sockets.attach(env.state)

    response = await memory_member.api_memory_recall(
        request(env, query={"q": "PostgreSQL database", "store": "default"}, owner=True)
    )

    assert response.status == 200
    assert sockets.frames == []


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", activity.KINDS)
async def test_announcement_locates_both_read_kinds_without_node_tiles(monkeypatch, tmp_path, kind):
    from kiro_crew import memory_graph_service, memory_stores

    loop_thread = threading.get_ident()
    context = ContextVar("activity_request", default=False)
    context.set(True)
    queried = []
    path = tmp_path / "memory.db"
    monkeypatch.setattr(memory_stores, "resolve_store_path", lambda store: path)

    def placed(ids):
        assert threading.get_ident() != loop_thread
        assert context.get() is True
        queried.append(ids)
        return {ids[0]: (0.0, 0.0, 7), ids[-1]: (1.0, 1.0, 9)}

    sidecar = SimpleNamespace(placed=placed, revision=lambda: "L1")
    monkeypatch.setattr(memory_graph_service, "graph_for", lambda db: SimpleNamespace(sidecar=sidecar))
    state = SimpleNamespace()
    sockets = _Sockets()
    sockets.attach(state)
    activity.announce(state, "member-alice", kind, [f"user.n{i}" for i in range(500)])
    await asyncio.wait_for(sockets.sent.wait(), timeout=30)
    frame = sockets.frames[0][1]
    assert queried == [[f"user.n{i}" for i in range(activity.IDS_CAP)]]
    assert frame["layout"] == "L1"
    assert frame["clusters"] == {"user.n0": 7, "user.n63": 9}
    assert set(frame) == {"store", "kind", "ids", "ts", "layout", "clusters"}
    assert frame["kind"] == kind and frame["store"] == "member-alice"


def test_activity_clusters_use_redacted_ids_and_detect_a_layout_change(monkeypatch, tmp_path):
    from kiro_crew import memory_graph_service, memory_stores
    from kiro_crew.dashboard.handlers import memory_graph_layout_api

    revisions = iter(["L1", "L2"])
    sidecar = SimpleNamespace(
        placed=lambda ids: {ids[0]: (0.0, 0.0, 7)}, revision=lambda: next(revisions)
    )
    monkeypatch.setattr(memory_stores, "resolve_store_path", lambda store: tmp_path / "memory.db")
    monkeypatch.setattr(memory_graph_service, "graph_for", lambda db: SimpleNamespace(sidecar=sidecar))
    monkeypatch.setattr(memory_graph_layout_api, "_redacted", lambda value: "redacted-id")
    frame = activity._located_frame("", activity.RECALL, ["private-id"])
    assert frame["ids"] == ["redacted-id"]
    assert frame["clusters"] == {"redacted-id": 7}
    assert frame["layout"] == memory_graph_layout_api.LAYOUT_CHANGED
    assert "private-id" not in json.dumps(frame)


def test_activity_still_names_nodes_if_layout_lookup_fails(monkeypatch):
    from kiro_crew import memory_stores

    def unavailable(store):
        raise OSError("layout unavailable")

    monkeypatch.setattr(memory_stores, "resolve_store_path", unavailable)
    frame = activity._located_frame("", activity.RECALL, ["user.a"])
    assert frame["ids"] == ["user.a"]
    assert "clusters" not in frame and "layout" not in frame
