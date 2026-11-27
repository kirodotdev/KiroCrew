"""A slot that holds a session-tree or creation record keeps its older crew logs.

The tree reads each unit's own ``session/adopted``, ``session/released``,
``session/spawned`` and parent-naming ``session/opened``, and the created-session roster,
the restart fence and an archived revive's ownership check read the same records. So
when such a slot moves to a new crew log the carry stands down for the whole slot
(``emit_session._holds_tree_record``): nothing is carried and none of its older crew logs
is closed ``superseded``, so the sweep keeps every one of them, as before the carry.
A plain chat, which holds none of them, still has its older crew logs collected.
"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from crew_log_core_helpers import _split_into_segments
from crew_log_drain import assert_drained, settle

from kiro_crew import crew_log as lg
from kiro_crew import session_ledger
from kiro_crew.crew_log import CrewLog, emit
from kiro_crew.crew_log import session_tree_projection as stp
from kiro_crew.crew_log import store
from kiro_crew.crew_log.session_tree_projection import CHECKPOINT_NAME, SessionTreeProjection
from kiro_crew.crew_log.store_segment_reads import _TAIL_WINDOW
from kiro_crew.session_ledger import _store_name
from kiro_crew.testing.wait import wait_until


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    yield


@pytest.fixture
def carrying(monkeypatch):
    monkeypatch.setenv(emit.CREW_LOG_ENV, "1")
    emit.reset_caches()
    stp.reset_for_tests()
    monkeypatch.setattr(emit, "_submit_carry_fold", lambda job: job())
    emit.set_session_key_probe(lambda session_id: None)
    try:
        yield
    finally:
        emit.set_session_key_probe(None)
        settle()
        tree = stp.projection()
        stp.reset_for_tests()
        wait_until(
            lambda: not tree._write_scheduled,
            describe=lambda: "the session tree's debounced checkpoint is still sleeping",
        )


def _parent(slot: str):
    node = stp.projection().nodes().get(slot)
    return None if node is None else node.parent_slot


@pytest.mark.usefixtures("carrying")
@pytest.mark.parametrize("decision", ["adopted", "released"])
def test_an_adopt_or_release_survives_the_reset_and_the_sweep(decision):
    w1, w2 = f"{decision}-w1", f"{decision}-w2"
    emit.on_session_opened(
        w1,
        agent="kirocrew",
        slot="worker-1",
        parent_slot="chat-a",
        session_key="dashboard:worker-1",
    )
    assert_drained()
    if decision == "adopted":
        emit.on_session_adopted(w1, slot="worker-1", parent_slot="chat-b")
        want = "chat-b"
    else:
        emit.on_session_released(w1, slot="worker-1")
        want = None
    assert_drained()
    assert _parent("worker-1") == want
    session_ledger.record_update("worker-1", session_id=w1, goal="g", event="e1")
    assert_drained()
    emit.on_session_opened(
        w2,
        agent="kirocrew",
        slot="worker-1",
        parent_slot="chat-a",
        previous_sid=w1,
        session_key="dashboard:worker-1",
    )
    assert_drained()
    assert not store.closed_for_good(store.unit_dir_for(lg.KIND_SESSION, w1))
    store.sweep_expired(0, now=time.time() + 60)
    assert CrewLog.exists(lg.KIND_SESSION, w1)
    assert _parent("worker-1") == want


@pytest.mark.usefixtures("carrying")
def test_a_creators_only_spawn_survives_the_reset_and_the_sweep():
    c1, c2 = "creator-c1", "creator-c2"
    emit.on_session_opened(c1, agent="kirocrew", slot="creator", session_key="dashboard:creator")
    assert_drained()
    emit.on_session_spawned(c1, creator_slot="creator", child_slot="kid")
    assert_drained()
    assert stp.projection().spawned_creator("kid") == "creator"
    session_ledger.record_update("creator", session_id=c1, goal="g", event="e1")
    assert_drained()
    emit.on_session_opened(
        c2,
        agent="kirocrew",
        slot="creator",
        previous_sid=c1,
        session_key="dashboard:creator",
    )
    assert_drained()
    assert not store.closed_for_good(store.unit_dir_for(lg.KIND_SESSION, c1))
    store.sweep_expired(0, now=time.time() + 60)
    assert CrewLog.exists(lg.KIND_SESSION, c1)
    assert stp.projection().spawned_creator("kid") == "creator"
    assert "kid" in [
        slot for slot, (by, _at) in stp.projection().created().items() if by == "creator"
    ]
    assert _parent("kid") == "creator"


@pytest.mark.usefixtures("carrying")
def test_a_sweep_keeps_the_succession_link_to_a_unit_that_stays(monkeypatch):
    """Removing ``b`` must not leave ``c`` ordered before the ``a`` it follows.

    ``a`` stays (a session key still maps it), ``b`` is superseded and expired, and the
    clock stepped back so ``c``'s header is stamped before ``a``'s.
    """
    stamp = [1_700_000_000_000]
    monkeypatch.setattr(store, "now_ms", lambda: stamp[0])  # flake-ok: fixed clock pin
    emit.set_session_key_probe(
        lambda session_id: "dashboard:link" if session_id == "link-a" else None
    )
    a, b, c = "link-a", "link-b", "link-c"
    emit.on_session_opened(a, agent="kirocrew", slot="link", session_key="dashboard:link")
    assert_drained()
    emit.on_session_opened(
        b, agent="kirocrew", slot="link", previous_sid=a, session_key="dashboard:link"
    )
    assert_drained()
    stamp[0] = 1_650_000_000_000
    emit.on_session_opened(
        c, agent="kirocrew", slot="link", previous_sid=b, session_key="dashboard:link"
    )
    assert_drained()
    store.sweep_expired(0, now=time.time() + 60)
    assert CrewLog.exists(lg.KIND_SESSION, a), "the control: a stays"
    from kiro_crew.crew_log import projection_readers

    order = projection_readers.slot_succession("link")
    assert order.index(c) > order.index(a), order


# -- the stand-down, at depth ---------------------------------------------------


def _probe_none() -> None:
    """No session-map key maps any unit, so nothing but the stand-down keeps one open."""
    emit.set_session_key_probe(lambda session_id: None)


def _restart() -> SessionTreeProjection:
    """A gateway restart: no in-memory state, no tree checkpoint, only the logs."""
    settle()
    emit.reset_caches()
    _probe_none()
    stp.reset_for_tests()
    checkpoint = lg.crew_log_root(lg.KIND_SESSION) / "projections" / CHECKPOINT_NAME
    checkpoint.unlink(missing_ok=True)
    tree = stp.projection()
    tree.ensure_seeded()
    return tree


def _entries(unit_id: str) -> "list[dict]":
    directory = lg.crew_log_root(lg.KIND_SESSION) / _store_name(unit_id)
    return [
        json.loads(line)
        for segment in sorted(directory.glob("log*.jsonl"), key=lambda p: (len(p.name), p.name))
        for line in segment.read_text(encoding="utf-8").splitlines()[1:]
    ]


def _bury(unit_id: str, after_type: str) -> None:
    """Push *unit_id*'s last *after_type* entry past the tail window, across a rotation.

    More than the tail window of the slot's own entries follow it, and they are moved
    into a second segment, so a read of the tail window, or of the newest segment, never
    meets it: only a read of every segment of the unit does.
    """
    for turn in range(1, 31):
        emit.on_message_received(unit_id, turn, text=f"turn {turn} " + "x" * 3000)
    assert_drained()
    entries = _entries(unit_id)
    last = max(e["seq"] for e in entries if e["type"] == after_type)
    settle()
    emit.reset_caches()
    _probe_none()
    _split_into_segments(lg.KIND_SESSION, unit_id, last + 1)
    directory = lg.crew_log_root(lg.KIND_SESSION) / _store_name(unit_id)
    rotated = directory / f"log.{last + 1}.jsonl"
    assert rotated.stat().st_size > _TAIL_WINDOW, "the control: the record is past the window"


def _reset(slot: str, previous: str, sid: str, key: str) -> None:
    emit.on_session_opened(sid, agent="kirocrew", slot=slot, previous_sid=previous, session_key=key)
    assert_drained()


def _none_closed_or_removed(*units: str) -> None:
    for unit in units:
        assert CrewLog.exists(lg.KIND_SESSION, unit), f"{unit} was collected"
        assert not store.closed_for_good(store.unit_dir_for(lg.KIND_SESSION, unit)), unit


def _fenced_revive_refusal(child: str, creator: str):
    """What an archived revive's ownership fence answers for *creator* reviving *child*."""
    from kiro_crew.dashboard.session_control_parts.session_revive import (
        _archived_fence_refusal,
    )

    return _archived_fence_refusal(
        child, {"created_by": creator}, caller_key=creator, ownership_fenced=True
    )


@pytest.mark.usefixtures("carrying")
def test_a_creators_older_crew_logs_are_kept_with_its_children_at_depth():
    """7(iii): a creator that created a live and an ended child keeps every older crew log.

    The spawns go through ``session_create``'s own spawn write (``_record_spawn``, the
    create's call with the values stamped at mint). The live child has no log yet; the
    ended one opened naming its creator and closed. The creator's own entries then push
    both spawns past the tail window across a segment rotation, and the creator resets
    twice, so the spawns sit in the older of its two older units. Swept past retention,
    none of them is closed or removed, the roster's creation records are unchanged, and
    the ended child's fenced revive by its creator is not refused, live and after a
    restart.
    """
    from kiro_crew.dashboard import session_control as sc

    creator, key = "chat-boss", "dashboard:chat-boss"
    emit.on_session_opened("boss-1", agent="kirocrew", slot=creator, session_key=key)
    assert_drained()
    for child in ("chat-live", "chat-done"):
        minted = SimpleNamespace(
            key=child, _lineage_minted=True, _created_by=creator, _created_by_sid="boss-1"
        )
        assert asyncio.run(sc._record_spawn(creator, minted)) is True
    emit.on_session_opened(
        "done-1",
        agent="kirocrew",
        slot="chat-done",
        parent_slot=creator,
        session_key="dashboard:chat-done",
    )
    assert_drained()
    emit.on_session_closed("done-1", "removed")
    assert_drained()
    _bury("boss-1", "session/spawned")
    tree = _restart()
    before = {slot: tree.created()[slot] for slot in ("chat-live", "chat-done")}
    assert before == {
        "chat-live": (creator, before["chat-live"][1]),
        "chat-done": (creator, before["chat-done"][1]),
    }

    _reset(creator, "boss-1", "boss-2", key)
    session_ledger.record_update(creator, session_id="boss-2", goal="g", event="e1")
    assert_drained()
    _reset(creator, "boss-2", "boss-3", key)
    store.sweep_expired(0, now=time.time() + 60)

    _none_closed_or_removed("boss-1", "boss-2")
    live = stp.projection()
    assert {slot: live.created()[slot] for slot in before} == before
    assert _fenced_revive_refusal("chat-done", creator) is None
    restarted = _restart()
    assert {slot: restarted.created()[slot] for slot in before} == before
    assert _fenced_revive_refusal("chat-done", creator) is None


@pytest.mark.usefixtures("carrying")
def test_a_childs_creator_claim_stays_verified_when_its_newest_opening_names_no_parent(
    tmp_path, monkeypatch
):
    """7(iv): a child whose older crew log alone names its creator keeps it, and the claim.

    Its first crew log opens naming the creator and grows past the tail window; after a
    restart the inherited-parent read fails, so the newer openings name no parent. That
    is the slot's only creation record, so the reset stands the carry down: nothing of
    the slot is closed or swept, and after a second restart the boot restore still
    verifies the transcript's creator claim against it.
    """
    from chat_test_helpers import _make_state

    from kiro_crew.config.paths import config_dir
    from kiro_crew.dashboard.chat import restore_open_slots
    from kiro_crew.dashboard.session_control import UNVERIFIED_CREATOR

    creator, child, key = "chat-boss", "chat-kid", "dashboard:chat-kid"
    emit.on_session_opened(
        "kid-1", agent="kirocrew", slot=child, parent_slot=creator, session_key=key
    )
    assert_drained()
    _bury("kid-1", "session/opened")
    _restart()
    # The turn's opening cites what ``_crew_log_inherited_parent`` answers; a failed read
    # answers ``""``, and the opening names no parent.
    emit.on_session_opened(
        "kid-2", agent="kirocrew", slot=child, previous_sid="kid-1", session_key=key
    )
    assert_drained()
    assert "parent" not in _entries("kid-2")[0]["data"], "the control: no inherited parent"
    session_ledger.record_update(child, session_id="kid-2", goal="g", event="e1")
    assert_drained()
    _reset(child, "kid-2", "kid-3", key)
    store.sweep_expired(0, now=time.time() + 60)

    _none_closed_or_removed("kid-1", "kid-2")
    _restart()
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    history = tmp_path / "history"
    history.mkdir()
    (history / f"dashboard_{child}.jsonl").write_text(
        json.dumps({"_type": "metadata", "title": child, "created_by": creator})
        + "\n"
        + json.dumps({"role": "user", "content": "task"})
        + "\n",
        encoding="utf-8",
    )
    (config_dir() / "open_slots.json").write_text(
        json.dumps({"keys": [child], "ts": 0.0}), encoding="utf-8"
    )
    state = _make_state(history)
    assert restore_open_slots(state) == 1
    assert state._slots[child]._created_by != UNVERIFIED_CREATOR
    assert state._slots[child]._created_by == creator


@pytest.mark.usefixtures("carrying")
def test_an_adopted_root_chat_keeps_its_older_crew_logs_and_its_adopter():
    """7(v): a root chat another session adopted keeps every older crew log.

    It opens with no parent and records no spawn; the adoption is its only tree record,
    and its own entries push it past the tail window across a rotation. Reset twice,
    swept past retention and restarted, none of its older crew logs is closed or
    removed, and the tree still hangs it under the adopter.
    """
    root, key = "chat-root", "dashboard:chat-root"
    emit.on_session_opened("root-1", agent="kirocrew", slot=root, session_key=key)
    assert_drained()
    emit.on_session_adopted("root-1", slot=root, parent_slot="chat-adopter")
    assert_drained()
    _bury("root-1", "session/adopted")
    _restart()
    assert _parent(root) == "chat-adopter", "the control"

    _reset(root, "root-1", "root-2", key)
    session_ledger.record_update(root, session_id="root-2", goal="g", event="e1")
    assert_drained()
    _reset(root, "root-2", "root-3", key)
    store.sweep_expired(0, now=time.time() + 60)

    _none_closed_or_removed("root-1", "root-2")
    assert _parent(root) == "chat-adopter"
    _restart()
    assert _parent(root) == "chat-adopter"
