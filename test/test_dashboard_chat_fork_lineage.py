"""Fork ancestry at fork time: the chain a new fork records, and when a fork is refused.

``chat_fork.fork_slot`` materializes the new fork's ``fork_ancestors`` from its
source's chain (``dashboard/fork_lineage.py``). A source forked before chains
existed carries only ``forked_from``, so its chain is walked through the catalog.
A deleted ancestor ends that walk like a root: a pre-upgrade fork whose source was
deleted must keep forking as it did before chains existed.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state


def _legacy_fork(state, name: str, parent: str, text: str):
    """A pre-upgrade fork: ``forked_from`` set, no recorded chain."""
    slot = state.get_or_create_slot(name)
    slot.forked_from = parent
    slot.fork_ancestors = []
    slot.append("user", text, "msg msg-u")
    slot.drain()
    return slot


async def _fork(state, name: str):
    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post(f"/api/chat/slots/{name}/fork", json={})
        return resp.status, await resp.json()


@pytest.mark.asyncio
async def test_legacy_fork_whose_source_was_deleted_can_still_be_forked(tmp_path):
    """A fork with only `forked_from` whose root transcript is gone (never
    written here): the walk finds no record for the root, the fork succeeds,
    and the chain names every link that is known."""
    state = _make_state(tmp_path)
    _legacy_fork(state, "mid", "dashboard:root", "q2")
    assert not state.conversation_log.has_log("dashboard:root")

    status, body = await _fork(state, "mid")

    assert status == 200, body
    child = state._slots[body["key"]]
    assert child.forked_from == "dashboard:mid"
    assert child.fork_ancestors == ["dashboard:mid", "dashboard:root"]


@pytest.mark.asyncio
async def test_a_deeper_legacy_chain_with_a_deleted_root_records_every_known_link(tmp_path):
    """root (deleted) <- mid (legacy, saved) <- leaf (legacy). Forking leaf walks
    mid's saved record to root, finds root gone, and stops there: the chain is
    leaf, mid, root, nearest first."""
    from kiro_crew.dashboard.chat import _save_slot_to_history

    state = _make_state(tmp_path)
    mid = _legacy_fork(state, "mid", "dashboard:root", "q2")
    _save_slot_to_history(state, mid)
    assert state.conversation_log.get_metadata("dashboard:mid").get("forked_from") == (
        "dashboard:root"
    )
    _legacy_fork(state, "leaf", "dashboard:mid", "q3")

    status, body = await _fork(state, "leaf")

    assert status == 200, body
    child = state._slots[body["key"]]
    assert child.fork_ancestors == ["dashboard:leaf", "dashboard:mid", "dashboard:root"]


@pytest.mark.asyncio
async def test_a_deleted_intermediate_ends_the_chain_without_refusing(tmp_path):
    """root (alive) <- mid (deleted) <- leaf (legacy). mid's record is gone, so
    nothing past it is knowable; the fork still succeeds with the known links."""
    state = _make_state(tmp_path)
    root = state.get_or_create_slot("root")
    root.append("user", "q1", "msg msg-u")
    root.drain()
    _legacy_fork(state, "leaf", "dashboard:mid", "q3")
    assert not state.conversation_log.has_log("dashboard:mid")

    status, body = await _fork(state, "leaf")

    assert status == 200, body
    assert state._slots[body["key"]].fork_ancestors == ["dashboard:leaf", "dashboard:mid"]


@pytest.mark.asyncio
async def test_a_source_that_moves_during_the_ancestry_walk_is_refused(tmp_path):
    """The legacy walk runs in a thread after the source identity was frozen; a
    source whose workspace changes under it must be refused, and since the walk
    runs before the mint no child is ever born."""
    state = _make_state(tmp_path)
    root = state.get_or_create_slot("root")
    root.append("user", "q1", "msg msg-u")
    root.drain()
    mid = _legacy_fork(state, "mid", "dashboard:root", "q2")
    before = set(state._slots)

    from kiro_crew.dashboard import chat_fork as cf

    real_chain = cf.ancestry_chain

    def _drifting_chain(log, key):
        mid.workspace = "elsewhere"  # the source moves while the walk runs
        return real_chain(log, key)

    with patch.object(cf, "ancestry_chain", _drifting_chain):
        status, body = await _fork(state, "mid")
    assert status == 503
    assert body["code"] == "store_unavailable"
    assert set(state._slots) == before, "no child may be born for a refused fork"


@pytest.mark.asyncio
async def test_a_fork_whose_chain_would_sit_at_the_bound_is_refused(tmp_path):
    """A chain AT `MAX_FORK_ANCESTORS` reads back as unprovable to every lineage
    reader, so the fork is refused before any row is copied."""
    from kiro_crew.dashboard.fork_lineage import MAX_FORK_ANCESTORS

    state = _make_state(tmp_path)
    root = state.get_or_create_slot("root")
    root.append("user", "q1", "msg msg-u")
    root.drain()
    deep = state.get_or_create_slot("deep")
    deep.forked_from = "dashboard:root"
    # One short of the bound: adding the source itself lands exactly on it.
    deep.fork_ancestors = ["dashboard:root"] + [
        f"dashboard:a{i}" for i in range(MAX_FORK_ANCESTORS - 2)
    ]
    deep.append("user", "q2", "msg msg-u")
    deep.drain()
    before = set(state._slots)

    status, body = await _fork(state, "deep")
    assert status == 422
    assert body["code"] == "fork_ancestry_over_bound"
    assert set(state._slots) == before, "the child must be withdrawn, not persisted"


@pytest.mark.asyncio
async def test_a_fork_whose_chain_would_overflow_its_metadata_line_is_refused(tmp_path):
    """Under the count bound a chain of long keys can still serialize past
    MAX_FORK_ANCESTRY_BYTES; the fork is refused with the same code."""
    from kiro_crew.dashboard.fork_lineage import (
        MAX_ANCESTOR_KEY_CHARS,
        MAX_FORK_ANCESTORS,
        MAX_FORK_ANCESTRY_BYTES,
    )

    state = _make_state(tmp_path)
    root = state.get_or_create_slot("root")
    root.append("user", "q1", "msg msg-u")
    root.drain()
    wide = state.get_or_create_slot("wide")
    wide.forked_from = "dashboard:root"
    stem = "dashboard:" + "w" * (MAX_ANCESTOR_KEY_CHARS - len("dashboard:") - 4)
    chain = ["dashboard:root"] + [f"{stem}{i:04d}" for i in range(140)]
    assert len(chain) < MAX_FORK_ANCESTORS
    assert len(json.dumps(chain).encode()) > MAX_FORK_ANCESTRY_BYTES
    wide.fork_ancestors = chain
    wide.append("user", "q2", "msg msg-u")
    wide.drain()
    before = set(state._slots)

    status, body = await _fork(state, "wide")
    assert status == 422
    assert body["code"] == "fork_ancestry_over_bound"
    assert set(state._slots) == before


@pytest.mark.asyncio
async def test_a_fork_whose_legacy_chain_cannot_be_walked_is_refused(tmp_path):
    """An ancestor whose transcript EXISTS but cannot be read makes the walk
    unprovable: the fork is refused before the child is minted, so no slot is
    born and no user-session count is taken."""
    state = _make_state(tmp_path)
    root = state.get_or_create_slot("root")
    root.append("user", "q1", "msg msg-u")
    root.drain()
    _legacy_fork(state, "mid", "dashboard:root", "q2")
    before = set(state._slots)

    from kiro_crew.dashboard import chat_fork as cf

    counted = MagicMock()
    with (
        patch.object(cf, "ancestry_chain", lambda log, key: None),
        patch("kiro_crew.dashboard.state.increment_user_session_count_off_loop", counted),
    ):
        status, body = await _fork(state, "mid")
    assert status == 422
    assert body["code"] == "fork_ancestry_unprovable"
    assert set(state._slots) == before, "no child may be born for a refused fork"
    counted.assert_not_called()


@pytest.mark.asyncio
async def test_a_fork_whose_chain_cannot_be_recorded_whole_is_refused(tmp_path):
    """`materialize_ancestors` reports `None` when an entry cannot be retained;
    the fork refuses rather than persist a chain that omits its own source."""
    state = _make_state(tmp_path)
    src = state.get_or_create_slot("src")
    src.append("user", "q1", "msg msg-u")
    src.drain()
    before = set(state._slots)

    from kiro_crew.dashboard import chat_fork as cf

    with patch.object(cf, "materialize_ancestors", lambda *a, **kw: None):
        status, body = await _fork(state, "src")
    assert status == 422
    assert body["code"] == "fork_ancestry_over_bound"
    assert set(state._slots) == before


@pytest.mark.asyncio
async def test_a_raise_during_the_ancestry_walk_leaves_no_child_behind(tmp_path):
    """A raise in the walk finds no half-created fork to withdraw; the caller
    sees the store-unavailable refusal, not a 500."""
    state = _make_state(tmp_path)
    root = state.get_or_create_slot("root")
    root.append("user", "q1", "msg msg-u")
    root.drain()
    _legacy_fork(state, "mid", "dashboard:root", "q2")
    before = set(state._slots)

    from kiro_crew.dashboard import chat_fork as cf

    def _explodes(log, key):
        raise OSError("catalog vanished mid-walk")

    with patch.object(cf, "ancestry_chain", _explodes):
        status, body = await _fork(state, "mid")
    assert status == 503
    assert body["code"] == "store_unavailable"
    assert set(state._slots) == before
