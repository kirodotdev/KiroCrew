"""`session_status`: the roster, the liveness, and the honesty of a short list.

The verb joins two sources that answer different halves, so the tests are grouped
by what each source contributes and by what happens when one of them is missing.

The LIVE slots supply the status, and the values are not decoration — a patrol
waits on `working` and `queued`, decides on `idle`, ignores `closed`, and
re-dispatches `lost` — so each one is pinned on its own.

The CREW LOG supplies the roster, and `closed`/`lost` are the rows only it can
produce. A worker that was closed, or lost with the process that ran it, is absent
from the live slots entirely: on a live-only list it is indistinguishable from a
worker that was never dispatched, and those states call for opposite actions. The
two durable fates are themselves told apart by the ``session/closed`` edge — a
worker that finished and closed its tab reads `closed` (with `closed_at`), one
gone without a close reads `lost`. So the tests below assert not only that the row
appears, but that a caller is TOLD which fate it was, and TOLD when the durable
read could not be made — a short list under `unreadable` is not evidence that
nothing was created.

The ownership fence applies to the ROWS and not merely to the verb, because the
rows carry other sessions' titles. That is asserted here rather than left to the
gate tests, since the gate cannot see it: a fenced caller passes the caller-side
gate and the leak would be in the listing.
"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from chat_test_helpers import _make_state

from kiro_crew import mcp_dashboard
from kiro_crew.crew_log import emit
from kiro_crew.crew_log import session_tree_projection as stp
from kiro_crew.crew_log.session_tree import OpenedRecord
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.dashboard.handlers import session_control as handlers_sc
from kiro_crew.mcp_tools.dashboard_client import InMemoryDashboardClient
from kiro_crew.mcp_tools.table import Caller, ToolContext


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setattr(sc, "session_control_enabled", lambda: True)


@pytest.fixture(autouse=True)
def _tree_on(tmp_path, monkeypatch):
    """A recorded tree folded in memory, with no write armed on the real pool.

    The projection is bound to one store, so it is dropped on both sides.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "crewhome"))
    monkeypatch.setenv(emit.CREW_LOG_ENV, "1")
    monkeypatch.setattr(
        "kiro_crew.executors.maintenance_executor",
        lambda: type("_NoPool", (), {"submit": staticmethod(lambda *a, **k: None)}),
    )
    stp.reset_for_tests()
    stp.projection().ensure_seeded()
    yield
    stp.reset_for_tests()


def _slot(state, name: str, **kwargs):
    return state.get_or_create_slot(name, **kwargs)


def _key(slot) -> str:
    return slot_history_key(slot)


def _child(state, name: str, creator):
    slot = state.get_or_create_slot(name)
    slot._created_by = creator.key
    return slot


def _recorded(slot: str, parent: str) -> None:
    """Put *slot* under *parent* in the fold, as its opening entry would have."""
    proj = stp.projection()
    proj.apply(OpenedRecord(sid=f"sid-{parent}", slot=parent, created_at=1))
    proj.apply(OpenedRecord(sid=f"sid-{slot}", slot=slot, created_at=2, parent_slot=parent))


def _emitted(
    slot: str,
    parent: str,
    *,
    closed: bool,
    reason: str = "removed",
    open_turn: bool = False,
    completed_stop_reason: str | None = None,
) -> None:
    """Write a REAL session log on disk under *parent*, optionally closed.

    Unlike :func:`_recorded`, which only applies the in-memory tree edge, this
    writes an announced unit through ``emit.on_session_opened`` -- the gateway's own
    path, which both advances the projection AND leaves a disk log -- so the
    slot-keyed status fold the `closed`/`lost` split reads has units to fold. A
    ``session/closed`` edge is appended when *closed*, which is the one fact that
    separates a worker that finished from one lost with its process.

    ``reason`` is the ``session/closed`` reason, so a test can write the DELIBERATE
    tab close (``"removed"``, the default -- what the ``_close_slot`` close path
    emits) versus a process recycle (``"reset"``) or a ``destroy`` (``"destroyed"``);
    only ``"removed"`` reads ``closed``. ``open_turn`` leaves a ``turn/started`` with
    no ``turn/completed`` before the close, which models a session torn down with a
    turn still in flight -- the projection keeps the open turn set on the close.
    ``completed_stop_reason`` instead closes that turn with a real
    ``turn/completed`` carrying that ``stop_reason`` before the ``session/closed``
    edge -- the shape a deliberate MID-TURN close actually leaves, where cancelling
    the running task makes its ``finally`` record a non-clean completion that clears
    ``turn_open``.
    """
    emit.on_session_opened(
        f"sid-{parent}", agent="kirocrew", slot=parent, model="m", cwd="/w", owner="o"
    )
    emit.on_session_opened(
        f"sid-{slot}",
        agent="kirocrew",
        slot=slot,
        model="m",
        cwd="/w",
        owner="o",
        parent_slot=parent,
    )
    if open_turn or completed_stop_reason is not None:
        emit.on_turn_started(f"sid-{slot}", turn=1, attempt=1, actor="agent")
    if completed_stop_reason is not None:
        emit.on_turn_completed(f"sid-{slot}", turn=1, stop_reason=completed_stop_reason)
    if closed:
        emit.on_session_closed(f"sid-{slot}", reason)


def _busy(slot):
    task = MagicMock()
    task.done.return_value = False
    slot.task = task
    return slot


def _status(state, caller, **kw):
    return asyncio.run(sc.created_session_status(state, caller_session_key=_key(caller), **kw))


def _rows(out) -> dict:
    return {r["target"]: r for r in out["sessions"]}


# ── What the live slots contribute ───────────────────────────────────────────


class TestLiveness:
    def test_an_open_doing_nothing_session_is_idle(self, tmp_path):
        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        _child(state, "chat-2", caller)
        row = _rows(_status(state, caller))["chat-2"]
        assert row["status"] == "idle" and row["running"] is False

    def test_a_session_with_a_turn_in_flight_is_working(self, tmp_path):
        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        _busy(_child(state, "chat-2", caller))
        row = _rows(_status(state, caller))["chat-2"]
        assert row["status"] == "working" and row["running"] is True

    def test_an_idle_session_with_messages_waiting_is_queued(self, tmp_path):
        """Distinct from `idle` because a steer would land on nothing, and distinct
        from `working` because nothing is running yet."""
        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        child = _child(state, "chat-2", caller)
        child.queue_append("do this next")
        row = _rows(_status(state, caller))["chat-2"]
        assert row["status"] == "queued" and row["queue_depth"] == 1

    def test_a_row_carries_the_title_the_person_sees(self, tmp_path):
        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        child = _child(state, "chat-2", caller)
        child.title = "Rebase the watchdog PR"
        assert _rows(_status(state, caller))["chat-2"]["title"] == "Rebase the watchdog PR"

    def test_a_session_the_caller_did_not_create_is_not_listed(self, tmp_path):
        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        _child(state, "chat-2", caller)
        _slot(state, "chat-9")  # the person's own tab
        assert set(_rows(_status(state, caller))) == {"chat-2"}

    def test_the_caller_does_not_list_itself(self, tmp_path):
        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        caller._created_by = caller.key
        _child(state, "chat-2", caller)
        assert caller.key not in _rows(_status(state, caller))


# ── What the crew log contributes ────────────────────────────────────────────


class TestTheDurableRoster:
    def test_a_session_the_dashboard_no_longer_holds_without_a_close_is_lost(self, tmp_path):
        """The row only the durable half can produce, and the reason this verb is
        not a filter over the live slots.

        A tree edge with no recorded ``session/closed`` -- the shape ``_recorded``
        leaves, and the shape a worker lost with its process leaves -- reads `lost`:
        the fail-safe reading a patrol re-dispatches.

        Mutation guard: drop the crew-log read and a worker that died vanishes from
        the list entirely, where it is indistinguishable from one that was never
        dispatched — and those call for opposite actions.
        """
        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        _recorded("chat-7", caller.key)  # opened, and since lost with its process
        out = _status(state, caller)
        assert out["tree"] == "readable"
        assert _rows(out)["chat-7"] == {
            "target": "chat-7",
            "status": "lost",
            "source": "crew_log",
        }

    def test_a_worker_that_closed_its_tab_is_reported_closed_with_its_stamp(self, tmp_path):
        """The distinction this verb exists for: a worker that FINISHED and closed
        its tab wrote a ``session/closed`` edge, so its gone row reads `closed` and
        carries ``closed_at`` — a conductor ignores it instead of re-dispatching.
        """
        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        _emitted("chat-7", caller.key, closed=True)
        out = _status(state, caller)
        assert out["tree"] == "readable"
        row = _rows(out)["chat-7"]
        assert row["target"] == "chat-7"
        assert row["status"] == "closed"
        assert row["source"] == "crew_log"
        assert row["closed_at"] is not None

    def test_a_worker_gone_with_a_live_log_but_no_close_is_lost(self, tmp_path):
        """A real on-disk log that never recorded a close is `lost`, not `closed`:
        the opposite action from the one above, read from the same edge."""
        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        _emitted("chat-7", caller.key, closed=False)
        row = _rows(_status(state, caller))["chat-7"]
        assert row["status"] == "lost"
        assert "closed_at" not in row

    def test_a_worker_reset_then_gone_is_lost_not_closed(self, tmp_path):
        """A process recycle (idle expiry, RSS recycle, model/provider switch, the
        watchdogs) writes a ``session/closed`` edge with reason ``reset``, but the
        tab and conversation survive it — the worker did NOT finish. Reading that
        close as `closed` is the fail-OPEN harm this split removes: it would tell a
        conductor a lost worker finished and stop it looking. So a reset-then-gone
        slot reads `lost`.

        Mutation guard: a reader keyed only on ``lifecycle == "closed"`` reads this
        `closed`; keying on the deliberate close reason keeps it `lost`.
        """
        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        _emitted("chat-7", caller.key, closed=True, reason="reset")
        row = _rows(_status(state, caller))["chat-7"]
        assert row["status"] == "lost"
        assert "closed_at" not in row

    def test_a_worker_destroyed_then_gone_is_lost_not_closed(self, tmp_path):
        """``destroy`` (including mid-turn) writes a ``session/closed`` edge with
        reason ``destroyed``; that is a teardown, not a finish, so it reads `lost`.
        """
        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        _emitted("chat-7", caller.key, closed=True, reason="destroyed")
        row = _rows(_status(state, caller))["chat-7"]
        assert row["status"] == "lost"
        assert "closed_at" not in row

    def test_a_worker_closed_with_an_open_turn_is_lost_not_closed(self, tmp_path):
        """A session cut off with a turn still in flight did not finish, however it
        was torn down. The projection keeps the open turn set on a close for exactly
        this reader, so even a deliberate-reason close with ``turn_open`` reads
        `lost`.

        Mutation guard: dropping the ``turn_open`` check reads this `closed`.
        """
        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        _emitted("chat-7", caller.key, closed=True, reason="removed", open_turn=True)
        row = _rows(_status(state, caller))["chat-7"]
        assert row["status"] == "lost"
        assert "closed_at" not in row

    def test_a_worker_closed_mid_turn_is_lost_not_closed(self, tmp_path):
        """A deliberate tab close WHILE a turn is in flight did not finish the work,
        so it must read `lost` -- the conductor has something to re-dispatch.

        The real path is subtle and is why the ``turn_open`` guard above is not
        enough on its own: ``_close_slot`` cancels ``slot.task`` first, and the
        cancelled turn's ``finally`` records a real ``turn/completed`` (here
        ``stop_reason="failed"``) that CLEARS ``turn_open`` before the deliberate
        ``removed`` close is written. So the fold sees ``lifecycle=closed``,
        ``turn_open=False`` and ``close_reason="removed"`` -- every earlier guard
        passes -- and only the newest turn's non-clean ``last_stop_reason`` reveals
        the work was cut off.

        Mutation guard: without the ``last_stop_reason`` check this reads `closed`
        and the conductor is told there is nothing to re-dispatch.
        """
        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        _emitted(
            "chat-7", caller.key, closed=True, reason="removed", completed_stop_reason="failed"
        )
        row = _rows(_status(state, caller))["chat-7"]
        assert row["status"] == "lost"
        assert "closed_at" not in row

    def test_a_worker_that_finished_a_clean_turn_then_closed_is_closed(self, tmp_path):
        """The counterpart: a worker whose newest turn ended cleanly (``end_turn``)
        and then had its tab closed DID finish, so it reads `closed`. This fences the
        mid-turn fix from over-reaching -- the ``last_stop_reason`` guard must let a
        genuine finish through, not fail every closed row with a completed turn.
        """
        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        _emitted(
            "chat-7", caller.key, closed=True, reason="removed", completed_stop_reason="end_turn"
        )
        row = _rows(_status(state, caller))["chat-7"]
        assert row["status"] == "closed"
        assert row["closed_at"] is not None

    def test_a_read_fault_folding_a_gone_slot_defaults_to_lost(self, tmp_path, monkeypatch):
        """Fail-safe default #1: a slot whose status fold raises reads `lost`, never
        `closed` — a read fault is not evidence the worker closed cleanly. Pinned
        directly on the fold since nothing else reddened when buluoray mutated the
        except branch to return `closed`.
        """

        def _boom(_slot, _name):
            raise RuntimeError("fold read fault")

        monkeypatch.setattr(sc, "read_slot_projection", _boom, raising=False)
        monkeypatch.setattr("kiro_crew.crew_log.projection.read_slot_projection", _boom)
        assert sc._gone_slot_lifecycles(["chat-9"]) == {"chat-9": {"status": "lost"}}

    def test_a_closed_lifecycle_missing_its_reason_defaults_to_lost(self, tmp_path):
        """Fail-safe default #2: a `closed` lifecycle whose ``close_reason`` is not a
        deliberate end (here ``None``, the missing-reason shape) reads `lost`, not
        `closed`. Pins ``_lifecycle_is_closed`` directly — the fail-safe branch
        buluoray noted no test reddened.
        """
        assert sc._lifecycle_is_closed({"lifecycle": "closed", "close_reason": None}) is False
        assert sc._lifecycle_is_closed({"lifecycle": "closed", "close_reason": "reset"}) is False
        assert sc._lifecycle_is_closed({"lifecycle": "open", "close_reason": "removed"}) is False
        assert (
            sc._lifecycle_is_closed(
                {"lifecycle": "closed", "close_reason": "removed", "turn_open": True}
            )
            is False
        )
        # A deliberate close whose newest turn ended non-cleanly (the mid-turn
        # cancel shape) reads lost even with a clean reason and turn_open already
        # cleared; a clean ``end_turn`` and the ``None`` of a never-worked close
        # both stay closed.
        assert (
            sc._lifecycle_is_closed(
                {"lifecycle": "closed", "close_reason": "removed", "last_stop_reason": "failed"}
            )
            is False
        )
        assert (
            sc._lifecycle_is_closed(
                {"lifecycle": "closed", "close_reason": "removed", "last_stop_reason": "cancelled"}
            )
            is False
        )
        assert (
            sc._lifecycle_is_closed(
                {"lifecycle": "closed", "close_reason": "removed", "last_stop_reason": "end_turn"}
            )
            is True
        )
        assert (
            sc._lifecycle_is_closed(
                {"lifecycle": "closed", "close_reason": "removed", "last_stop_reason": None}
            )
            is True
        )
        assert sc._lifecycle_is_closed({"lifecycle": "closed", "close_reason": "removed"}) is True

    def test_a_closed_or_lost_row_carries_no_title(self, tmp_path):
        """There is no slot to read one from, and a title recovered from anywhere
        else would be a claim about a session nobody is holding."""
        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        _recorded("chat-7", caller.key)
        assert "title" not in _rows(_status(state, caller))["chat-7"]

    def test_a_live_session_the_tree_also_knows_is_one_row_naming_both_sources(self, tmp_path):
        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        _child(state, "chat-2", caller)
        _recorded("chat-2", caller.key)
        rows = _rows(_status(state, caller))
        assert list(rows) == ["chat-2"]
        assert rows["chat-2"]["source"] == "crew_log+live"
        assert rows["chat-2"]["status"] == "idle"

    def test_a_live_session_the_tree_does_not_know_is_still_listed(self, tmp_path):
        """A session created before the log was on, or whose entry has not landed,
        must not disappear from the caller's own roster."""
        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        _child(state, "chat-2", caller)
        assert _rows(_status(state, caller))["chat-2"]["source"] == "live"

    def test_another_sessions_children_are_not_listed(self, tmp_path):
        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        _recorded("chat-7", caller.key)
        _recorded("chat-8", "chat-99")
        assert set(_rows(_status(state, caller))) == {"chat-7"}


class TestThePersistedBirthRoster:
    def test_a_caller_created_session_closed_before_its_first_turn_is_listed(
        self, tmp_path, monkeypatch
    ):
        """Persisted creator attribution covers the gap before a tree edge exists.

        Another caller's archived session is the negative fence control: history
        metadata carries titles, so creator equality must be checked before a row
        is exposed.
        """
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        other = _slot(state, "chat-8")
        monkeypatch.setattr(sc, "_workspace_name_for_dir", lambda cfg, ws_dir: caller.workspace)
        own_result = asyncio.run(
            sc.create_session(
                state,
                caller_session_key=_key(caller),
                title="worker lost before startup",
            )
        )
        foreign_result = asyncio.run(
            sc.create_session(
                state,
                caller_session_key=_key(other),
                title="another session's private work",
            )
        )
        own = state.get_slot(own_result["target"])
        foreign = state.get_slot(foreign_result["target"])
        for slot in (own, foreign):
            assert slot.messages == []
            assert _save_slot_to_history(state, slot, closed=True, force=True)
            state._slots.pop(slot.key)

        out = _status(state, caller)

        assert out["tree"] == "readable"
        assert out["history"] == "readable"
        assert _rows(out) == {
            own.key: {
                "target": own.key,
                "title": "worker lost before startup",
                "status": "unknown",
                "source": "history",
            }
        }


class TestTheDurableReadsQuality:
    def test_an_unreadable_tree_is_said_out_loud_and_the_answer_is_live_only(
        self, tmp_path, monkeypatch
    ):
        """A short list means opposite things under `readable` and `unreadable`, so
        the flag is the load-bearing field.

        Mutation guard: report `readable` unconditionally and a caller reads "you
        created one worker" from an answer that could not see the other seven.
        """
        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        _child(state, "chat-2", caller)
        _recorded("chat-7", caller.key)
        monkeypatch.setattr(
            "kiro_crew.crew_log.session_tree_projection.SessionTreeProjection."
            "seeded_for_current_store",
            property(lambda _self: False),
        )
        out = _status(state, caller)
        assert out["tree"] == "unreadable"
        assert set(_rows(out)) == {"chat-2"}, "an unreadable tree contributes no rows"

    def test_the_crew_log_being_off_is_unreadable_not_empty(self, tmp_path, monkeypatch):
        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        _child(state, "chat-2", caller)
        monkeypatch.setattr(emit, "enabled", lambda: False)
        out = _status(state, caller)
        assert out["tree"] == "unreadable"
        assert set(_rows(out)) == {"chat-2"}

    def test_an_incomplete_fold_is_still_served_and_flagged_as_a_floor(self, tmp_path, monkeypatch):
        """An incomplete fold is the one case the projection's own contract says a
        DISPLAYING reader may use: a missing row renders as an absent session, which
        is what a live-only list looked like before this verb existed. Discarding it
        would throw away every durable (`closed`/`lost`) row over one unreadable unit.
        """
        from kiro_crew.crew_log.session_tree import TreeReading

        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        _recorded("chat-7", caller.key)
        real = stp.SessionTreeProjection.reading
        monkeypatch.setattr(
            stp.SessionTreeProjection,
            "reading",
            lambda self: TreeReading(
                nodes=real(self).nodes, incomplete=True, records=real(self).records
            ),
        )
        out = _status(state, caller)
        assert out["tree"] == "incomplete"
        assert "chat-7" in _rows(out), "the rows it DID read are still the answer"

    def test_a_raising_projection_degrades_to_live_only(self, tmp_path, monkeypatch):
        """A read fault must not take the verb down: the live half is still a true
        answer to half the question, and it is the half a patrol acts on most."""
        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        _child(state, "chat-2", caller)

        def _boom(_self):
            raise RuntimeError("store went away")

        monkeypatch.setattr(stp.SessionTreeProjection, "reading", _boom)
        out = _status(state, caller)
        assert out["tree"] == "unreadable"
        assert set(_rows(out)) == {"chat-2"}


class TestTheEventLoopBoundary:
    def test_the_history_roster_scan_runs_off_the_event_loop(self, tmp_path, monkeypatch):
        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        event_loop_thread = threading.get_ident()
        scan_threads: list[int] = []

        def _history_scan(_state, _caller_key, _caller_workspace):
            scan_threads.append(threading.get_ident())
            return {}, "readable", 0

        monkeypatch.setattr(sc, "_created_history_roster", _history_scan)

        out = asyncio.run(sc.created_session_status(state, caller_session_key=_key(caller)))

        assert out["history"] == "readable"
        assert scan_threads
        assert scan_threads[0] != event_loop_thread


class TestTheMcpQualityCaveats:
    @pytest.mark.parametrize("history_state", ["incomplete", "unreadable"])
    def test_a_history_quality_gap_is_caveated_separately_from_the_tree(
        self, monkeypatch, history_state
    ):
        rendered = self._render(
            monkeypatch,
            {
                "tree": "readable",
                "history": history_state,
                "sessions": [
                    {
                        "target": "chat-2",
                        "title": "worker",
                        "status": "idle",
                        "queue_depth": 0,
                    }
                ],
            },
        )

        assert "transcript-metadata roster" in rendered.lower()
        assert history_state in rendered.lower()
        assert "crew-log roster" not in rendered.lower()

    def _render(self, monkeypatch, payload):
        """One ``session_status`` frame whose roster route answers ``payload``."""
        dash = InMemoryDashboardClient({"GET /api/session-control/status": payload})
        ctx = ToolContext(dash, Caller.strict("dashboard:chat-1"))
        return mcp_dashboard.TABLE.call("session_status", {}, ctx)

    def _one_row(self, status, **extra):
        row = {"target": "chat-2", "title": "worker", "status": status, "queue_depth": 0}
        row.update(extra)
        return {"tree": "readable", "history": "readable", "sessions": [row]}

    def test_an_unknown_row_does_not_render_as_idle(self, monkeypatch):
        """``unknown`` must not fall through the glyph map's default, which draws
        idle's sleep mark — the one status meaning "no decision needed" — for a
        session whose fate nothing accounts for. The two have to be
        distinguishable at a glance."""
        idle = self._render(monkeypatch, self._one_row("idle"))
        unknown = self._render(monkeypatch, self._one_row("unknown"))

        idle_mark = idle.splitlines()[1].strip().split()[0]
        unknown_mark = unknown.splitlines()[1].strip().split()[0]
        assert idle_mark == "\U0001f4a4", "the idle mark is the control for this comparison"
        assert unknown_mark != idle_mark
        assert "unknown" in unknown

    def test_a_union_cut_is_caveated_with_its_count_under_its_own_name(self, monkeypatch):
        payload = self._one_row("idle")
        payload["roster_omitted"] = 7

        rendered = self._render(monkeypatch, payload)

        assert "7 more session(s)" in rendered
        assert "transcript-metadata roster" not in rendered.lower()

    def test_a_retained_union_is_not_caveated(self, monkeypatch):
        payload = self._one_row("idle")
        payload["roster_omitted"] = 0

        assert "more session(s)" not in self._render(monkeypatch, payload)

    def test_an_absent_union_count_claims_no_cut(self, monkeypatch):
        """An older backend does not compute the field. Absent must read as "this
        answer does not say", never as a cut."""
        assert "more session(s)" not in self._render(monkeypatch, self._one_row("idle"))

    def test_a_published_but_unreadable_union_count_is_not_silently_a_no_cut(self, monkeypatch):
        """The third state. A cut whose size will not read as a number is still a
        cut, and collapsing it into the absent case hides missing rows."""
        payload = self._one_row("idle")
        payload["roster_omitted"] = {"rows": "many"}

        rendered = self._render(monkeypatch, payload)

        assert "may be missing" in rendered
        assert "without a readable count" in rendered


# ── The fence applies to the rows ────────────────────────────────────────────


class TestTheFenceOnRows:
    def test_a_fenced_caller_does_not_see_a_live_session_it_did_not_create(self, tmp_path):
        """The rows carry TITLES — the names of the user's private work — so this
        verb must not become a way to enumerate them.

        The tree can place a row the fence does not admit (an adoption), and the
        caller-side gate cannot catch it: a fenced caller passes that gate, and the
        leak would be in the listing.
        """
        state = _make_state(tmp_path)
        caller = _slot(state, "cron-abc123")
        state.crons.list_jobs.return_value = [SimpleNamespace(id="abc123", created_by="U0123ABCD")]
        theirs = _slot(state, "chat-9")
        theirs.title = "the tax return"
        _recorded("chat-9", caller.key)
        _child(state, "chat-2", caller)
        _recorded("chat-2", caller.key)
        rows = _rows(_status(state, caller))
        assert set(rows) == {"chat-2"}

    def test_an_unfenced_caller_sees_a_session_the_tree_places_under_it(self, tmp_path):
        """The other direction: the owner's own session is not fenced, so a session
        it adopted belongs on its roster."""
        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        adopted = _slot(state, "chat-9")
        adopted.title = "taken over"
        _recorded("chat-9", caller.key)
        rows = _rows(_status(state, caller))
        assert rows["chat-9"]["title"] == "taken over"

    def test_a_carried_fence_verdict_is_honoured_over_the_config_record(self, tmp_path):
        """The HTTP gate resolves a member's fence on its VERIFIED scope and passes
        it down; re-deriving it here would read a record an operator's writer can
        flip in between."""
        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        theirs = _slot(state, "chat-9")
        _recorded("chat-9", caller.key)
        assert "chat-9" in _rows(_status(state, caller, caller_fenced=False))
        assert "chat-9" not in _rows(_status(state, caller, caller_fenced=True))
        assert theirs.key == "chat-9"

    def test_a_session_in_another_workspace_is_not_listed(self, tmp_path):
        """Workspaces are the memory boundary, and it is the same boundary
        `authorize_target` refuses across — a row the caller could not then message
        would be a listing of work it cannot see."""
        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        elsewhere = _child(state, "chat-2", caller)
        elsewhere.workspace = "other"
        assert _rows(_status(state, caller)) == {}

    def test_a_worker_linked_to_a_channel_after_birth_is_not_listed(self, tmp_path):
        """`display_title` for a channel-linked session is derived from a
        conversation other people are in. The creator passes every caller-side gate
        and did create this session, so only a TARGET-side containment check keeps
        that title out of its roster."""
        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        worker = _child(state, "chat-2", caller)
        worker.title = "#incident-4417 payment outage"
        worker.linked_session_key = "slack:C0ABC:1790411186.442029"

        assert _rows(_status(state, caller)) == {}

    def test_a_worker_mirrored_to_a_channel_after_birth_is_not_listed(self, tmp_path, monkeypatch):
        """The mirror is the same exposure by the other mechanism, and it is bound
        with no idle-slot requirement, so it can land long after the creator's own
        gate passed."""
        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        worker = _child(state, "chat-2", caller)
        worker.title = "#incident-4417 payment outage"
        monkeypatch.setattr(sc, "_has_channel_mirror", lambda _state, slot: slot.key == "chat-2")

        assert _rows(_status(state, caller)) == {}

    def test_an_unlinked_worker_is_still_listed_with_its_title(self, tmp_path):
        """The containment drops a CHANGED target, not every row: the ordinary
        roster is unaffected."""
        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        worker = _child(state, "chat-2", caller)
        worker.title = "rebase the broadcast branch"

        assert _rows(_status(state, caller))["chat-2"]["title"] == "rebase the broadcast branch"


# ── The caller gate ──────────────────────────────────────────────────────────


class TestCallerGate:
    def test_a_disabled_surface_refuses(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sc, "session_control_enabled", lambda: False)
        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        with pytest.raises(sc.SessionControlError) as exc:
            _status(state, caller)
        assert exc.value.code == "session_control_disabled"

    def test_an_ephemeral_caller_cannot_list(self, tmp_path):
        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        caller.memory_mode = "temporary"
        with pytest.raises(sc.SessionControlError) as exc:
            _status(state, caller)
        assert exc.value.code == "ephemeral_caller"

    def test_an_app_scoped_caller_cannot_list(self, tmp_path):
        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        caller._app = "some-app"
        with pytest.raises(sc.SessionControlError) as exc:
            _status(state, caller)
        assert exc.value.code == "app_scoped_caller"

    def test_an_unidentifiable_caller_is_refused(self, tmp_path):
        state = _make_state(tmp_path)
        _slot(state, "chat-1")
        with pytest.raises(sc.SessionControlError) as exc:
            asyncio.run(sc.created_session_status(state, caller_session_key=""))
        assert exc.value.code == "caller_unidentified"

    def test_a_workflow_caller_cannot_list(self, tmp_path):
        state = _make_state(tmp_path)
        caller = _slot(state, f"{sc.WORKFLOW_SLOT_PREFIX}abc")
        with pytest.raises(sc.SessionControlError) as exc:
            _status(state, caller)
        assert exc.value.code == "unattended_caller"


def test_nothing_about_the_targets_changes(tmp_path):
    """READ-only. A listing that started a turn, drained a queue, or bumped a
    session's activity would make a patrol a participant in the work it watches."""
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    child = _child(state, "chat-2", caller)
    child.queue_append("waiting")
    before = (len(child.messages), len(child._queue), child.task)
    _status(state, caller)
    assert (len(child.messages), len(child._queue), child.task) == before


class TestTheRoute:
    def _request(self, state, caller, *, internal=True):
        request = MagicMock()
        request.app = {"state": state}
        request.path = "/api/session-control/status"
        request.method = "GET"
        request.headers = {"X-Session-Key": _key(caller)}
        request.query = {}
        request.get = lambda key, default=None: (
            True if (key in ("internal_auth", "peer_verified") and internal) else default
        )
        return request

    def _body(self, response):
        import json

        return json.loads(response.body.decode())

    def test_the_prewarm_stays_valid_until_the_sync_gate(self, tmp_path, monkeypatch):
        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        warm = {"valid": False}
        gate_observations: list[bool] = []

        async def _prewarm():
            warm["valid"] = True
            asyncio.get_running_loop().call_soon(warm.__setitem__, "valid", False)

        def _gate():
            gate_observations.append(warm["valid"])
            return True

        monkeypatch.setattr(sc, "prewarm_enabled_check", _prewarm)
        monkeypatch.setattr(sc, "session_control_enabled", _gate)

        resp = asyncio.run(handlers_sc.api_session_control_status(self._request(state, caller)))

        assert resp.status == 200
        assert gate_observations and gate_observations[0] is True

    def test_without_the_secret_it_is_forbidden(self, tmp_path):
        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        resp = asyncio.run(
            handlers_sc.api_session_control_status(self._request(state, caller, internal=False))
        )
        assert resp.status == 403
        assert self._body(resp)["code"] == "internal_secret_required"

    def test_it_returns_the_roster(self, tmp_path):
        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        _child(state, "chat-2", caller)
        _recorded("chat-7", caller.key)
        resp = asyncio.run(handlers_sc.api_session_control_status(self._request(state, caller)))
        assert resp.status == 200
        body = self._body(resp)
        assert body["caller"] == caller.key
        assert {r["target"] for r in body["sessions"]} == {"chat-2", "chat-7"}

    def test_a_refusal_keeps_its_status(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sc, "session_control_enabled", lambda: False)
        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        resp = asyncio.run(handlers_sc.api_session_control_status(self._request(state, caller)))
        assert resp.status == 403
        assert self._body(resp)["code"] == "session_control_disabled"


class TestRosterRetentionBounds:
    def test_a_persisted_roster_overflow_is_bounded_and_reported(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

        cap = 3
        monkeypatch.setattr(sc, "MAX_SESSION_STATUS_ROWS", cap, raising=False)
        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        monkeypatch.setattr(sc, "_workspace_name_for_dir", lambda cfg, ws_dir: caller.workspace)

        for index in range(cap + 1):
            result = asyncio.run(
                sc.create_session(
                    state,
                    caller_session_key=_key(caller),
                    title=f"worker {index}",
                )
            )
            child = state.get_slot(result["target"])
            assert _save_slot_to_history(state, child, closed=True, force=True)
            state._slots.pop(child.key)

        out = _status(state, caller)

        assert len(out["sessions"]) == cap
        assert out["history"] == "incomplete"
        assert out["history_omitted"] == 1

    def test_a_union_overflow_reports_its_own_count_and_not_a_transcript_fault(
        self, tmp_path, monkeypatch
    ):
        """The union cut is its own fact and must not borrow another source's.

        Signalling it as ``history: "incomplete"`` with ``history_omitted`` left
        alone says two false things at once: it tells a caller a transcript read it
        completed was faulty, and it reports nothing omitted while rows are dropped.
        Neither is actionable — re-reading history cannot recover a row the union
        bound cut.

        The roster here is built from LIVE children only, so the transcript read is
        genuinely complete and any ``incomplete`` on it is the borrowed reason.
        """
        cap = 3
        over = 2
        monkeypatch.setattr(sc, "MAX_SESSION_STATUS_ROWS", cap, raising=False)
        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        for index in range(cap + over):
            _child(state, f"chat-{index + 2}", caller)

        out = _status(state, caller)

        assert len(out["sessions"]) == cap
        assert out["roster_omitted"] == over
        assert out["history"] == "readable", "the transcript read completed; do not blame it"
        assert out["history_omitted"] == 0, "no transcript row was cut"

    def test_a_retained_union_reports_no_cut_rather_than_omitting_the_count(
        self, tmp_path, monkeypatch
    ):
        """Zero, not absent. A reader has to tell "the union was whole" from "this
        answer does not say", and only an always-present count does that."""
        monkeypatch.setattr(sc, "MAX_SESSION_STATUS_ROWS", 8, raising=False)
        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        _child(state, "chat-2", caller)

        out = _status(state, caller)

        assert out["roster_omitted"] == 0

    def test_the_union_cut_is_audited_with_its_own_count(self, tmp_path, monkeypatch):
        """The audit row carried the same borrowed reason, so the trail recorded a
        transcript fault for a cut that was not one."""
        cap = 2
        monkeypatch.setattr(sc, "MAX_SESSION_STATUS_ROWS", cap, raising=False)
        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        for index in range(cap + 1):
            _child(state, f"chat-{index + 2}", caller)
        audits: list[dict] = []

        with patch.object(sc, "_audit", side_effect=lambda **kw: audits.append(kw)):
            _status(state, caller)

        detail = next(a["detail"] for a in audits if a["operation"] == "status")
        assert detail["roster_omitted"] == 1
        assert detail["history"] == "readable"
        assert detail["history_omitted"] == 0

    def test_a_persisted_title_is_truncated_when_the_row_is_retained(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

        title_cap = 12
        monkeypatch.setattr(sc, "MAX_SESSION_STATUS_TITLE_CHARS", title_cap, raising=False)
        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        monkeypatch.setattr(sc, "_workspace_name_for_dir", lambda cfg, ws_dir: caller.workspace)
        result = asyncio.run(
            sc.create_session(
                state,
                caller_session_key=_key(caller),
                title="attacker-controlled title",
            )
        )
        child = state.get_slot(result["target"])
        assert _save_slot_to_history(state, child, closed=True, force=True)
        state._slots.pop(child.key)

        row = _status(state, caller)["sessions"][0]

        assert row["title"] == "attacker-con"
        assert len(row["title"]) == title_cap


class TestTheCallerSurfaceIsRecheckedAfterTheScan:
    """The roster's rows carry other sessions' TITLES -- the names of the user's
    private work -- and the caller-surface gate runs BEFORE a disk-bound history
    scan that suspends. A channel mirror can be bound onto an already-open
    dashboard session while that scan is on its worker thread, from the channel
    picker and from the Slack link route, neither of which requires an idle slot.
    Without a re-check the reply is published past a gate that passed.

    The gone-row lifecycle fold is a SECOND suspension, later still, and the
    workspace-boundary containment every live row is filtered on is built after
    it -- so a worker moved across the boundary during that fold is excluded by a
    containment read as the slots now stand, not admitted on its pre-move
    workspace. The last two tests pin that: the fold is monkeypatched to move a
    slot mid-await, the window the synchronous gate and the first re-check both
    predate.
    """

    def test_a_mirror_bound_during_the_scan_refuses_instead_of_replying(
        self, tmp_path, monkeypatch
    ):
        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        child = _child(state, "chat-2", caller)
        child.title = "acquisition terms Q4"
        real_scan = sc._created_history_roster

        def _scan_then_mirror(state_, caller_key_, workspace_):
            # The mirror lands while the scan is on its worker thread, which is
            # exactly the window the synchronous gate cannot see -- in the store the
            # re-check reads.
            out = real_scan(state_, caller_key_, workspace_)
            state.sessions.set_mirror_link(_key(caller), "C0FFEE", "1758.0004")
            return out

        monkeypatch.setattr(sc, "_created_history_roster", _scan_then_mirror)

        with pytest.raises(sc.SessionControlError) as excinfo:
            _status(state, caller)

        assert excinfo.value.code == "mirrored_caller"
        assert child.title not in str(excinfo.value)

    def test_a_caller_whose_surface_stays_clean_still_gets_its_roster(self, tmp_path):
        """The re-check refuses a CHANGE, not every caller: the ordinary path is
        one gate result confirmed by a second, not a new way to fail."""
        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        _child(state, "chat-2", caller)

        out = _status(state, caller)

        assert [row["target"] for row in out["sessions"]] == ["chat-2"]

    def test_a_workspace_that_moves_during_the_scan_refuses(self, tmp_path, monkeypatch):
        """A live slot's workspace IS reassigned elsewhere (a chat-handler commit,
        a channel slot adopting one from metadata), and it is the boundary every
        row is filtered on -- so a move mid-scan must not filter rows against a
        boundary the scan did not use.

        Slot identity is deliberately not asserted here: no live key is rebound to
        a new object, so a test for it could only pass by faking the registry."""
        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        _child(state, "chat-2", caller)
        real_scan = sc._created_history_roster

        def _scan_then_move(state_, caller_key_, workspace_):
            out = real_scan(state_, caller_key_, workspace_)
            caller.workspace = "somewhere-else"
            return out

        monkeypatch.setattr(sc, "_created_history_roster", _scan_then_move)

        with pytest.raises(sc.SessionControlError) as excinfo:
            _status(state, caller)

        assert excinfo.value.code == "caller_changed_mid_read"

    def test_a_worker_moved_across_the_boundary_during_the_gone_fold_is_not_leaked(
        self, tmp_path, monkeypatch
    ):
        """A live worker in the caller's workspace carries a title the caller may
        read -- until it switches workspace, which `api_chat_slot_workspace` does on
        a running slot with no idle requirement. If that move lands during the
        gone-row lifecycle fold (the verb's last suspension), a containment set
        built before the fold would still admit the worker and carry its title out
        of the caller's workspace. The row must be dropped: containment is rebuilt
        after the fold, so the moved worker falls outside the resolvable set."""
        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        worker = _child(state, "chat-2", caller)
        worker.title = "acquisition terms Q4"
        # A tree-attested, now-absent slot so `gone_slots` is non-empty and the
        # lifecycle fold -- the guarded suspension -- actually runs.
        _recorded("chat-9", caller.key)
        real_fold = sc._gone_slot_lifecycles

        def _fold_then_move(slots):
            out = real_fold(slots)
            worker.workspace = "other"
            return out

        monkeypatch.setattr(sc, "_gone_slot_lifecycles", _fold_then_move)

        rows = _rows(_status(state, caller))

        assert "chat-2" not in rows
        assert worker.title not in str(rows)
        # The gone row the fold was folding is unaffected -- it carries no title.
        assert rows["chat-9"]["status"] == "lost"

    def test_the_caller_moving_during_the_gone_fold_refuses(self, tmp_path, monkeypatch):
        """The symmetric guard for the SAME suspension: the caller's own workspace
        is the boundary every live row is filtered on, and a move of it during the
        gone fold must refuse rather than filter rows against a boundary the fold
        did not use -- the re-check after the fold fires, not only the one after
        the history scan."""
        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        _child(state, "chat-2", caller)
        _recorded("chat-9", caller.key)
        real_fold = sc._gone_slot_lifecycles

        def _fold_then_move(slots):
            out = real_fold(slots)
            caller.workspace = "somewhere-else"
            return out

        monkeypatch.setattr(sc, "_gone_slot_lifecycles", _fold_then_move)

        with pytest.raises(sc.SessionControlError) as excinfo:
            _status(state, caller)

        assert excinfo.value.code == "caller_changed_mid_read"

    def test_a_live_only_child_gone_during_the_gone_fold_is_dropped_not_a_keyerror(
        self, tmp_path, monkeypatch
    ):
        """Regression: the gone-row fold is the verb's last suspension, and the
        creator's live children were read into the roster BEFORE it. A worker the
        caller created that never took a first turn (no tree edge) and left no
        history row can have its tab closed WHILE the fold runs. The row loop then
        reaches it with ``slot is None`` and neither durable source present. Reading
        ``history_children[key]`` there raised ``KeyError`` and failed the whole
        roster; the row must simply be dropped instead.
        """
        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        # A live-only child: created-by the caller (so it enters the roster via the
        # live-children read), no tree edge, no history row.
        _child(state, "chat-2", caller)
        # A tree-attested, now-absent slot so the lifecycle fold -- the suspension
        # the live child must vanish across -- actually runs.
        _recorded("chat-9", caller.key)
        real_fold = sc._gone_slot_lifecycles

        def _fold_then_drop_child(slots):
            out = real_fold(slots)
            # The child's tab closes mid-fold: its slot leaves the live map after it
            # was already captured in the roster.
            state._slots.pop("chat-2", None)
            return out

        monkeypatch.setattr(sc, "_gone_slot_lifecycles", _fold_then_drop_child)

        rows = _rows(_status(state, caller))

        # No KeyError, the roster still returns, and the vanished live-only child is
        # simply absent -- there is nothing durable to report for it.
        assert "chat-2" not in rows
        # The gone tree slot the fold was folding is unaffected.
        assert rows["chat-9"]["status"] == "lost"
