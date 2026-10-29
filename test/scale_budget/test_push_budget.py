"""Dashboard push budgets (F3, F7, F13, F19): frames, bytes and row work per push.

Every case runs on :func:`conftest.budget_state`, a ``DashboardState`` over a real
``SessionMap`` on a fake loop and clock, with sockets that record each frame
synchronously. Slots are seeded with one broadcast at the end.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from .conftest import FakeWs, add_socket, budget_state, harness_fail, optional_module, seed_slots

pytestmark = pytest.mark.scale_budget

SLOTS = 400
ROUNDS = 25


def _window_s() -> float:
    """The refresh coalescing window, from the module that owns it when it exists."""
    module = optional_module("kiro_crew.dashboard.state_parts.refresh_coalesce")
    return float(getattr(module, "REFRESH_COALESCE_WINDOW_S", 1.5))


def _refresh_kinds(ws: FakeWs) -> list[set[str]]:
    """The kinds each ``refresh`` frame on *ws* carried, in send order."""
    out = []
    for frame in ws.frames:
        msg = json.loads(frame)
        if msg.get("type") != "refresh":
            continue
        raw = msg.get("data", msg.get("kinds", ""))
        if isinstance(raw, dict):
            raw = raw.get("kinds", "")
        parts = raw if isinstance(raw, list) else str(raw).split(",")
        out.append({str(k) for k in parts if k})
    return out


def _count_kind(ws: FakeWs, kind: str) -> int:
    return sum(1 for kinds in _refresh_kinds(ws) if kind in kinds)


@pytest.fixture
def state(monkeypatch, tmp_path, fake_clock):
    return budget_state(monkeypatch, tmp_path, fake_clock)


def test_refresh_frames_carry_their_kind(state) -> None:
    """Ratchet on main: a ``refresh`` frame names the kinds it was pushed with.

    The F3 budget below counts frames per kind; if the frame shape moves, this
    fails instead of the budget reading every frame as kind-less.
    """
    ws = add_socket(state, FakeWs(owner=True))
    state.push_refresh("crons")
    assert _refresh_kinds(ws) == [{"crons"}], f"refresh frames read as {_refresh_kinds(ws)}"


def test_turn_end_burst_coalesces_and_history_goes_at_once(state, fake_clock) -> None:
    """3,000 ``turn_end`` pushes 20 ms apart cost at most span/window + 1 frames.

    A ``history`` push (slot close, session delete) made while a ``turn_end``
    window is open is still sent at once: 50 such pushes, each in the middle of
    an open window, each produce their frame before the next tick.
    """
    ws = add_socket(state, FakeWs(owner=True))
    pushes, gap, every = 3000, 0.02, 60
    window = _window_s()
    fired = 0
    late_history = 0
    history_pushes = 0
    for i in range(pushes):
        state.push_refresh("turn_end")
        if i % every == every // 2:
            history_pushes += 1
            before = _count_kind(ws, "history")
            state.push_refresh("history")
            if _count_kind(ws, "history") != before + 1:
                late_history += 1
        fake_clock.advance(gap)
        fired += state.fake_loop.run_due()
    fake_clock.advance(window)
    fired += state.fake_loop.run_due()
    budget = int(pushes * gap / window) + 1
    sent = _count_kind(ws, "turn_end")
    assert fired >= 1 and 1 <= sent <= budget, (
        f"F3: {pushes} turn_end pushes over {pushes * gap:.0f} s sent {sent} frames carrying "
        f"turn_end (budget {budget}); the fake loop fired {fired} coalescing timers (at least 1)"
    )
    assert late_history == 0, (
        f"F3: {late_history} of {history_pushes} history pushes made inside an open "
        "turn_end window waited for the trailing frame (budget 0: history is sent at once)"
    )


def test_one_flush_reprojects_once_per_view_and_serializes_once_per_slot(state, spy) -> None:
    """Ratchet on main: an owner tab plus a user tab cost one reprojection per view.

    One flush at 400 slots runs at most 2 reprojections (owner and user view) and
    at most N slot row builds: one pass, the count measured on main.
    """
    from kiro_crew.dashboard.state_parts import slots_frame

    slots = seed_slots(state, SLOTS)
    add_socket(state, FakeWs(owner=True))
    add_socket(state, FakeWs(owner=False))
    state._do_slots_broadcast()
    spy.wrap(type(state), "_reproject_slots")
    spy.wrap(slots_frame, "serialize_slot")
    spy.wrap(type(state), "serialize_slot")
    slots[7].append("assistant", "one more row", "msg")
    state._do_slots_broadcast()
    reprojections = spy.counts["_reproject_slots"]
    rows = spy.counts["serialize_slot"]
    if rows == 0:
        harness_fail("one flush built 0 slot rows; the serialize_slot spy missed")
    assert (
        reprojections <= 2
    ), f"F13: one flush for an owner and a user tab ran {reprojections} reprojections (budget 2)"
    assert rows <= SLOTS, (
        f"F13: one flush built {rows} slot rows (budget {SLOTS}, one pass per flush at "
        f"{SLOTS} slots on main)"
    )


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "scale-F13: a delta carries each changed row whole, about 2,300 chars for one row "
        "at this seed"
    ),
)
def test_diff_tab_frames_stay_under_2kb(state) -> None:
    """At 400 slots, every frame a diff-capable tab gets after its first is under 2,000 chars."""
    slots = seed_slots(state, SLOTS)
    diff = add_socket(state, FakeWs(owner=True, slot_diff=True))
    add_socket(state, FakeWs(owner=True))
    state._do_slots_broadcast()
    first = len(diff.frames)
    for i in range(ROUNDS):
        slots[i].append("assistant", f"round {i}", "msg")
        state._do_slots_broadcast()
    later = [f for f in diff.frames[first:] if '"slots' in f[:40]]
    largest = max((len(f) for f in later), default=0)
    assert later and largest < 2000, (
        f"F13: over {ROUNDS} one-slot flushes at {SLOTS} slots the diff tab got "
        f"{len(later)} slot frames, the largest {largest} chars (budget 2,000)"
    )


def _count_row_encodings(monkeypatch: pytest.MonkeyPatch, prefix: str) -> dict[str, int]:
    """Count slot rows passed through ``json.dumps``, one per row dict per call.

    A row is a dict whose ``key`` starts with *prefix*; a call that encodes a list
    of 400 rows counts 400. Modules call ``json.dumps`` through the ``json``
    module, so patching the attribute reaches every call site.
    """
    counter = {"rows": 0}
    real = json.dumps

    def rows_in(value: Any, depth: int = 0) -> int:
        if depth > 3:
            return 0
        if isinstance(value, dict):
            key = value.get("key")
            if isinstance(key, str) and key.startswith(prefix):
                return 1
            return sum(rows_in(v, depth + 1) for v in value.values())
        if isinstance(value, (list, tuple)):
            return sum(rows_in(v, depth + 1) for v in value)
        return 0

    def counted(obj: Any, *args: Any, **kwargs: Any) -> str:
        counter["rows"] += rows_in(obj)
        return real(obj, *args, **kwargs)

    monkeypatch.setattr(json, "dumps", counted)
    return counter


def test_mixed_audience_serializes_each_row_once(state, monkeypatch) -> None:
    """One diff owner tab and one legacy owner tab: each slot row is JSON-encoded once per flush.

    Both tabs read the owner view, so one serialization pass covers them (item
    2.15): the diff is taken from the row texts the frame is built from.
    """
    slots = seed_slots(state, SLOTS)
    diff = add_socket(state, FakeWs(owner=True, slot_diff=True))
    legacy = add_socket(state, FakeWs(owner=True))
    state._do_slots_broadcast()
    sent_before = (len(diff.frames), len(legacy.frames))
    encodings = _count_row_encodings(monkeypatch, "chat-")
    slots[5].append("assistant", "changed", "msg")
    state._do_slots_broadcast()
    if (len(diff.frames), len(legacy.frames)) == sent_before:
        harness_fail("the mixed-audience flush sent no frame to either tab")
    if encodings["rows"] == 0:
        harness_fail("the mixed-audience flush encoded 0 slot rows; the json.dumps spy missed")
    assert encodings["rows"] <= SLOTS, (
        f"F13: one flush to a diff tab and a legacy tab JSON-encoded {encodings['rows']} slot "
        f"rows (budget {SLOTS}: one pass)"
    )


def test_warm_flush_builds_no_slot_links(state, spy) -> None:
    """A second flush with nothing changed: 0 link sets, 0 row summaries, 0 redactions.

    Positive control: one appended row then costs exactly 1 row summary, so the
    summary spy is on the name the projection calls.
    """
    from kiro_crew.dashboard.state_parts import slot_transcript, slots_frame

    slots = seed_slots(state, SLOTS)
    add_socket(state, FakeWs(owner=True))
    state._do_slots_broadcast()

    # The uncached link body: ``_build_slot_links`` where a memo sits in front of
    # it, else ``_slot_links`` itself.
    if hasattr(slots_frame, "_build_slot_links"):
        spy.wrap(slots_frame, "_build_slot_links", "links")
    else:
        spy.wrap(type(state), "_slot_links", "links")
    projection = optional_module("kiro_crew.dashboard.slot_projection")
    summarize = projection is not None and hasattr(projection, "summarize_rows")
    if summarize:
        # The projection binds ``summarize_rows`` by name at import, so the spy
        # wraps that binding, not ``slot_row_memo.summarize_rows``.
        spy.wrap(projection, "summarize_rows")
    spy.wrap(slot_transcript, "_redact", "redactions")
    state._do_slots_broadcast()
    measured = (spy.counts["links"], spy.counts["summarize_rows"], spy.counts["redactions"])
    assert summarize and measured == (0, 0, 0), (
        f"F7: a warm flush at {SLOTS} slots ran {measured[0]} link builds, {measured[1]} row "
        f"summaries and {measured[2]} redactions (budget 0, 0, 0; row summaries counted: "
        f"{summarize})"
    )
    spy.reset()
    slots[11].append("assistant", "one more row", "msg")
    state._do_slots_broadcast()
    assert (
        spy.counts["summarize_rows"] == 1
    ), f"F7: one appended row cost {spy.counts['summarize_rows']} row summaries (expected 1)"


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="scale-F19: one dirty slot still re-serializes every row (no per-slot dirty set)",
)
def test_one_dirty_slot_serializes_at_most_10_rows(state, spy) -> None:
    """With one slot changed, a flush builds at most 10 slot rows."""
    from kiro_crew.dashboard.state_parts import slots_frame

    slots = seed_slots(state, SLOTS)
    add_socket(state, FakeWs(owner=True))
    state._do_slots_broadcast()
    spy.wrap(slots_frame, "serialize_slot")
    spy.wrap(type(state), "serialize_slot")
    slots[3].append("assistant", "changed", "msg")
    state._do_slots_broadcast()
    rows = spy.counts["serialize_slot"]
    assert rows <= 10, f"F19: one dirty slot cost {rows} slot row builds (budget 10)"
