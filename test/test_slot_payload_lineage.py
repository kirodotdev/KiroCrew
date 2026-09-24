"""``parent`` on the slots payload -- the edge the chat sidebar nests on.

The sidebar already receives the slots broadcast, so the creator edge rides a frame it
gets anyway. Two properties decide whether that works, and they pull in opposite
directions, which is why both are pinned here:

* the SHAPE is byte-identical to the Sessions table's ``parent``, so one moved
  ``nestsUnder`` serves both views;
* the KEY SPACE is this payload's own -- bare slot keys here, ``dashboard:`` session
  keys there -- because ``nestsUnder`` resolves ``parent.key`` against the keys of the
  payload it was handed, and a session key here would name no row at all.
"""

from __future__ import annotations

import time

import pytest

from kiro_crew import crew_log as lg
from kiro_crew.crew_log import CrewLog, emit
from kiro_crew.crew_log import session_tree_projection as stp
from kiro_crew.dashboard import state as st
from kiro_crew.dashboard.session_memory import lineage_parents
from kiro_crew.dashboard.state import _attach_slot_parents

GATEWAY = "gateway"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    # The payload carries lineage only when the crew log is on: with it off there are no
    # records, and the builder answers None for every row without touching storage.
    monkeypatch.setenv(emit.CREW_LOG_ENV, "1")
    # The in-flight guard is module state, so a test that leaves it raised would make
    # every later test see "a seed is already running" and request none.
    monkeypatch.setattr(st, "_lineage_seed_in_flight", False)
    # No seed OUTLIVES its test. `_attach_slot_parents` asks for one whenever the
    # projection is unseeded, and on the real pool that fold runs on a background thread:
    # it is still scanning when a later file monkeypatches the scanner, where it reads as
    # that test's own second scan. Submitting runs it INLINE instead -- the seed still
    # happens, which the frame-after-the-seed test depends on, but it is finished before
    # the test that asked for it returns. The one test that counts submits installs its
    # own stub over this.
    monkeypatch.setattr(
        "kiro_crew.executors.maintenance_executor",
        lambda: type("_Inline", (), {"submit": staticmethod(lambda fn, *a, **k: fn(*a, **k))}),
    )
    stp.reset_for_tests()
    yield
    stp.reset_for_tests()


def _unit(sid: str, slot: str, *, parent: str | None = None) -> None:
    """One announced session log, written the way the emitter writes one."""
    handle = CrewLog.create(lg.KIND_SESSION, sid, owner="raymond", agent="kirocrew", slot=slot)
    data = {
        "agent": "kirocrew",
        "slot": slot,
        "model": "opus",
        "cwd": "/w",
        "owner": "raymond",
        "resumed": False,
    }
    if parent:
        data["parent"] = {"slot": parent}
    handle.append("session/opened", data, src=GATEWAY)
    del handle


def _rows(*slot_keys: str) -> list[dict]:
    """Slot rows as the payload spells them: identity is the BARE slot key."""
    return [{"key": key} for key in slot_keys]


def _seeded() -> None:
    """Seed the process projection, as the off-loop seed does before a later frame.

    ``_attach_slot_parents`` never seeds: it runs on the event loop and seeding touches
    the disk. So a test that wants edges establishes the state first, which is the state
    the SECOND frame after a cold start sees.
    """
    stp.projection().ensure_seeded()


# ── the provisional frame ──────────────────────────────────────────────────


def test_an_unseeded_projection_marks_the_frame_provisional():
    """A cold frame says its answer is not final, so a reader can come back for it.

    The seed does not broadcast when it lands, so without this an idle gateway -- nothing
    running, no further frame coming -- serves an unnested sidebar until the user happens
    to act. The flag turns that recovery into a read the client repeats.
    """
    _unit("s-1", "chat-1")
    _unit("s-2", "chat-2", parent="chat-1")
    # Deliberately NOT seeded: this is the first frame after a cold start.
    rows = _rows("chat-1", "chat-2")
    _attach_slot_parents(rows)

    assert all(row["parent"] is None for row in rows)
    assert all(row["lineage_pending"] is True for row in rows)


def test_a_seeded_projection_marks_nothing_provisional():
    """The steady state carries no flag at all, so nothing is added to every frame."""
    _unit("s-1", "chat-1")
    _unit("s-2", "chat-2", parent="chat-1")
    _seeded()

    rows = _rows("chat-1", "chat-2")
    _attach_slot_parents(rows)

    assert rows[1]["parent"] == {"slot": "chat-1", "key": "chat-1"}
    assert all("lineage_pending" not in row for row in rows)


# ── the edge ───────────────────────────────────────────────────────────────


def test_a_created_slot_carries_its_creator_in_the_payloads_own_key_space():
    _unit("s-1", "chat-1")
    _unit("s-2", "chat-2", parent="chat-1")
    _seeded()

    rows = _rows("chat-1", "chat-2")
    _attach_slot_parents(rows)

    assert rows[0]["parent"] is None
    assert rows[1]["parent"] == {"slot": "chat-1", "key": "chat-1"}


def test_the_shape_is_the_same_two_keys_the_memory_payload_uses():
    """Byte-identical shape, so one ``nestsUnder`` reads both payloads."""
    _unit("s-1", "chat-1")
    _unit("s-2", "chat-2", parent="chat-1")
    _seeded()

    slot_rows = _rows("chat-1", "chat-2")
    _attach_slot_parents(slot_rows)

    proj = stp.projection()
    proj.ensure_seeded()
    memory_rows = [{"key": "dashboard:chat-1"}, {"key": "dashboard:chat-2"}]
    memory_parents = lineage_parents(memory_rows, proj.nodes())

    slot_child = slot_rows[1]["parent"]
    memory_child = memory_parents["dashboard:chat-2"]
    assert set(slot_child) == set(memory_child) == {"slot", "key"}
    # The citation is the same fact on both -- it is the child's own crew log entry.
    assert slot_child["slot"] == memory_child["slot"] == "chat-1"
    # The resolved key follows the payload, which is the whole point.
    assert slot_child["key"] == "chat-1"
    assert memory_child["key"] == "dashboard:chat-1"


def test_a_creator_that_is_not_running_leaves_the_citation_but_no_key():
    """The child stays a root and still says who opened it."""
    _unit("s-1", "chat-1")
    _unit("s-2", "chat-2", parent="chat-1")
    _seeded()

    rows = _rows("chat-2")  # chat-1 is closed: not in this payload
    _attach_slot_parents(rows)

    assert rows[0]["parent"] == {"slot": "chat-1", "key": None}


def test_a_slot_nobody_created_carries_an_explicit_null():
    _unit("s-1", "chat-1")
    _seeded()
    rows = _rows("chat-1")
    _attach_slot_parents(rows)
    assert rows[0]["parent"] is None


def test_every_row_gets_the_key_even_with_no_lineage_at_all():
    """``parent`` absent and ``parent`` null must not be two different states for the
    frontend to tell apart."""
    rows = _rows("chat-1", "chat-2")
    _attach_slot_parents(rows)
    assert all("parent" in row for row in rows)
    assert all(row["parent"] is None for row in rows)


def test_a_cycle_detaches_the_edge_but_keeps_the_citation():
    """Reachable only through damaged records. Neither slot may nest."""
    _unit("s-1", "chat-1", parent="chat-2")
    _unit("s-2", "chat-2", parent="chat-1")
    _seeded()

    rows = _rows("chat-1", "chat-2")
    _attach_slot_parents(rows)

    assert rows[0]["parent"] == {"slot": "chat-2", "key": None}
    assert rows[1]["parent"] == {"slot": "chat-1", "key": None}


def test_a_chain_nests_to_whatever_depth_the_creating_went():
    _unit("s-1", "chat-1")
    _unit("s-2", "chat-2", parent="chat-1")
    _unit("s-3", "chat-3", parent="chat-2")
    _seeded()

    rows = _rows("chat-1", "chat-2", "chat-3")
    _attach_slot_parents(rows)

    assert rows[1]["parent"]["key"] == "chat-1"
    assert rows[2]["parent"]["key"] == "chat-2"


# ── the guard ──────────────────────────────────────────────────────────────


def test_a_lineage_failure_still_ships_every_slot(monkeypatch):
    """A sidebar that does not nest beats a sidebar that does not paint."""

    def boom(*_a, **_k):
        raise RuntimeError("projection is broken")

    monkeypatch.setattr(stp, "projection", boom)

    rows = _rows("chat-1", "chat-2")
    _attach_slot_parents(rows)  # must not raise

    assert [row["parent"] for row in rows] == [None, None]


def test_a_lineage_failure_says_so_once_at_warning(monkeypatch, caplog):
    """The failure is the one outcome the payload cannot express, so it is logged.

    ``parent``, an explicit ``None`` and ``lineage_pending`` are all on the wire, so
    every other answer this gives is readable from the payload. A swallowed exception
    is not: the rows it produces are byte-identical to a store that genuinely holds no
    lineage, which is why the report has to carry the traceback.

    Once per process, because this runs on every slots frame and the likely failures
    are persistent -- an unlatched warning would be one line per broadcast.
    """
    monkeypatch.setattr(st, "_lineage_failure_warned", False)

    def boom(*_a, **_k):
        raise RuntimeError("projection is broken")

    monkeypatch.setattr(stp, "projection", boom)

    with caplog.at_level("DEBUG", logger=st.logger.name):
        for _ in range(3):
            _attach_slot_parents(_rows("chat-1", "chat-2"))

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "conductor lane" in warnings[0].getMessage()
    # The traceback is the payload of the report: without it the line says only that
    # something failed, which is what the rows already implied.
    assert warnings[0].exc_info is not None


def test_a_healthy_read_says_nothing(caplog):
    """No warning on the ordinary path -- the latch must not be armed by success."""
    _unit("s-1", "chat-1")
    _unit("s-2", "chat-2", parent="chat-1")
    _seeded()

    with caplog.at_level("WARNING", logger=st.logger.name):
        _attach_slot_parents(_rows("chat-1", "chat-2"))

    assert [r for r in caplog.records if r.levelname == "WARNING"] == []


# ── the wire ───────────────────────────────────────────────────────────────


def test_a_failed_seed_is_re_requested_from_the_slots_path(monkeypatch):
    """The sidebar must reach the projection's retry on its own.

    A seed that failed leaves a readable but EMPTY state, so ``seeded_for_current_store``
    is satisfied and this path would take the healthy branch forever. The only caller
    that seeds is the System page's sampler, so on a gateway nobody opens that page on
    the sidebar would stay unnested for the life of the process -- the projection's retry
    would exist and never be reached.
    """
    _unit("s-1", "chat-1")
    _unit("s-2", "chat-2", parent="chat-1")

    proj = stp.projection()
    monkeypatch.setattr(
        type(proj), "seeded_for_current_store", property(lambda self: True), raising=False
    )
    monkeypatch.setattr(type(proj), "seed_retry_due", property(lambda self: True))

    asked: list = []
    monkeypatch.setattr(st, "_request_lineage_seed", lambda: asked.append(1))

    rows = _rows("chat-1", "chat-2")
    _attach_slot_parents(rows)

    assert asked == [1], "the slots path never asked for the retry"
    # Provisional, because a retry is a promise the answer will change -- which is what
    # makes the flag correct here rather than the thing it is forbidden for.
    assert all(row["lineage_pending"] is True for row in rows)


def test_an_established_seed_asks_for_nothing(monkeypatch):
    """The healthy path must not request a seed, or every frame would queue one."""
    _unit("s-1", "chat-1")
    _unit("s-2", "chat-2", parent="chat-1")
    _seeded()

    asked: list = []
    monkeypatch.setattr(st, "_request_lineage_seed", lambda: asked.append(1))

    rows = _rows("chat-1", "chat-2")
    _attach_slot_parents(rows)

    assert asked == []
    assert rows[1]["parent"] == {"slot": "chat-1", "key": "chat-1"}
    assert all("lineage_pending" not in row for row in rows)


def test_the_serialized_payload_carries_parent_for_a_created_child(tmp_path, monkeypatch):
    """``serialize_slots`` is what ``GET /api/chat/slots`` dumps, so pin IT.

    The tests above call ``_attach_slot_parents`` directly, which proves the helper and
    not the payload: the handler builds its body from ``serialize_slots()`` alone, so a
    serializer that dropped or overwrote the key would pass every one of them and still
    ship a sidebar with no conductor lane.
    """
    from chat_test_helpers import _make_state

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "wire-home"))
    stp.reset_for_tests()
    _unit("s-1", "chat-1-parent")
    _unit("s-2", "chat-2-child", parent="chat-1-parent")
    _seeded()

    state = _make_state(tmp_path)
    state.get_or_create_slot("chat-1-parent")
    state.get_or_create_slot("chat-2-child")

    payloads = state.serialize_slots()
    by_key = {p["key"]: p for p in payloads}

    assert by_key["chat-2-child"]["parent"] == {
        "slot": "chat-1-parent",
        "key": "chat-1-parent",
    }
    assert by_key["chat-1-parent"]["parent"] is None
    # The frontend branches on `parent === undefined`, so every row carries the key.
    assert all("parent" in p for p in payloads)


def test_an_empty_payload_is_left_alone():
    rows: list[dict] = []
    _attach_slot_parents(rows)
    assert rows == []


def test_a_row_without_a_usable_key_gets_a_null_rather_than_a_lookup():
    _unit("s-1", "chat-1")
    _seeded()
    rows: list[dict] = [{"key": "chat-1"}, {"key": None}, {}]
    _attach_slot_parents(rows)
    assert rows[1]["parent"] is None
    assert rows[2]["parent"] is None


def test_reads_after_the_first_do_not_touch_the_store(monkeypatch):
    """The seed is once per process; a broadcast is not a scan."""
    _unit("s-1", "chat-1")
    _unit("s-2", "chat-2", parent="chat-1")
    _seeded()

    from kiro_crew.crew_log import store as crew_store

    reads = {"n": 0}
    for name in ("unit_dirs", "oldest_segment", "read_head"):
        real = getattr(crew_store, name)

        def spy(*a, _real=real, **k):
            reads["n"] += 1
            return _real(*a, **k)

        monkeypatch.setattr(crew_store, name, spy)

    for _ in range(20):
        rows = _rows("chat-1", "chat-2")
        _attach_slot_parents(rows)
        assert rows[1]["parent"]["key"] == "chat-1"

    assert reads["n"] == 0


# ── the event loop ─────────────────────────────────────────────────────────


def test_an_unseeded_projection_ships_nulls_instead_of_seeding_inline(monkeypatch):
    """This runs on the event loop, so it must not be the thing that pays for a seed.

    The store HAS an edge here and the payload still reports none: before the seed lands
    the honest answer is "no creator known", and one frame of that is the price of never
    stalling the loop.
    """
    _unit("s-1", "chat-1")
    _unit("s-2", "chat-2", parent="chat-1")

    submitted: list = []
    monkeypatch.setattr(st, "_request_lineage_seed", lambda: submitted.append(1))

    rows = _rows("chat-1", "chat-2")
    _attach_slot_parents(rows)

    assert [row["parent"] for row in rows] == [None, None]
    assert stp.projection().seeded is False
    assert len(submitted) == 1


def test_the_seed_is_requested_once_however_many_frames_arrive(monkeypatch):
    """A burst of broadcasts before the seed lands must cost one seed, not one each."""
    _unit("s-1", "chat-1")

    seeds = {"n": 0}
    monkeypatch.setattr(st, "_lineage_seed_in_flight", False)

    def fake_submit(fn):
        seeds["n"] += 1  # never runs fn, so the flag stays raised

    monkeypatch.setattr(
        "kiro_crew.executors.maintenance_executor",
        lambda: type("P", (), {"submit": staticmethod(fake_submit)}),
    )

    for _ in range(5):
        _attach_slot_parents(_rows("chat-1"))

    assert seeds["n"] == 1


def test_the_frame_after_the_seed_carries_the_edge():
    """Nothing broadcasts when the seed lands, so the NEXT ordinary frame nests.

    That is why the seed starts at boot rather than on a frame: every way of pushing a
    frame of its own breaks something load-bearing. ``push_slots_update`` and the
    trailing timer both WRITE the coalescer's clock, and a request inside
    ``suspend_slots_push`` needs that window open so its own flush broadcasts inline and
    a broadcast failure reaches its caller; a frame outside the accounting avoids the
    clock but the create path pins exactly one coalesced frame per change.
    """
    _unit("s-1", "chat-1")
    _unit("s-2", "chat-2", parent="chat-1")

    st._request_lineage_seed()
    deadline = time.monotonic() + 10
    while not stp.projection().seeded_for_current_store and time.monotonic() < deadline:
        time.sleep(0.02)
    assert stp.projection().seeded_for_current_store is True

    rows = _rows("chat-1", "chat-2")
    _attach_slot_parents(rows)
    assert rows[1]["parent"] == {"slot": "chat-1", "key": "chat-1"}


def test_binding_the_serving_loop_does_not_seed(monkeypatch):
    """Binding the loop must queue NO pool work.

    Seeding is bound to one store and re-runs when the data home changes, so a process
    that binds several states over several homes -- a test run, a pod host -- would put
    one full cold scan per bind on the shared maintenance pool and starve whatever else
    is waiting on it. The seed is requested lazily by the first cold frame instead.
    """
    import asyncio

    started: list = []
    monkeypatch.setattr(st, "_request_lineage_seed", lambda: started.append(1))

    state = st.DashboardState.__new__(st.DashboardState)
    loop = asyncio.new_event_loop()
    try:
        state.bind_serving_loop(loop)
    finally:
        loop.close()

    assert started == []
