"""`session_status`: the roster, the liveness, and the honesty of a short list.

The verb joins two sources that answer different halves, so the tests are grouped
by what each source contributes and by what happens when one of them is missing.

The LIVE slots supply the status, and the four values are not decoration — a patrol
waits on `working` and `queued`, decides on `idle`, and re-dispatches or drops
`gone` — so each one is pinned on its own.

The CREW LOG supplies the roster, and `gone` is the row only it can produce. A
worker that was closed, or lost with the process that ran it, is absent from the
live slots entirely: on a live-only list it is indistinguishable from a worker
that was never dispatched, and those two states call for opposite actions. So the
tests below assert not only that a `gone` row appears, but that a caller is TOLD
when the durable read could not be made — a short list under `unreadable` is not
evidence that nothing was created.

The ownership fence applies to the ROWS and not merely to the verb, because the
rows carry other sessions' titles. That is asserted here rather than left to the
gate tests, since the gate cannot see it: a fenced caller passes the caller-side
gate and the leak would be in the listing.
"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from chat_test_helpers import _make_state

from kiro_crew import mcp_dashboard
from kiro_crew.crew_log import emit
from kiro_crew.crew_log import session_tree_projection as stp
from kiro_crew.crew_log.session_tree import OpenedRecord
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.dashboard.handlers import session_control as handlers_sc


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

    def test_a_session_between_a_plans_stages_still_reads_as_working(self, tmp_path):
        """`running` alone is not busy: a multi-stage plan closes each stage's own
        turn, so it reads False in the gap while the plan is live.

        Mutation guard: drop `_in_stage_execution` and a patrol concludes a worker
        mid-plan is idle and needs a decision, then steers into a plan that is
        about to open its next stage.
        """
        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        child = _child(state, "chat-2", caller)
        child._in_stage_execution = True
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
    def test_a_session_the_dashboard_no_longer_holds_is_reported_gone(self, tmp_path):
        """The row only the durable half can produce, and the reason this verb is
        not a filter over the live slots.

        Mutation guard: drop the crew-log read and a worker that died vanishes from
        the list entirely, where it is indistinguishable from one that was never
        dispatched — and those call for opposite actions.
        """
        state = _make_state(tmp_path)
        caller = _slot(state, "chat-1")
        _recorded("chat-7", caller.key)  # opened, and since closed or lost
        out = _status(state, caller)
        assert out["tree"] == "readable"
        assert _rows(out)["chat-7"] == {
            "target": "chat-7",
            "status": "gone",
            "source": "crew_log",
        }

    def test_a_gone_row_carries_no_title(self, tmp_path):
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
        would throw away every `gone` row over one unreadable unit.
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

        def _history_scan(_state, _caller_key):
            scan_threads.append(threading.get_ident())
            return {}, "readable"

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
        monkeypatch.setattr(
            mcp_dashboard,
            "require_strict_session_key",
            lambda *_args, **_kwargs: ("dashboard:chat-1", None),
        )
        monkeypatch.setattr(
            mcp_dashboard,
            "_get",
            lambda *_args, **_kwargs: {
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

        rendered = mcp_dashboard._call_tool_inner("session_status", {})

        assert "transcript-metadata roster" in rendered.lower()
        assert history_state in rendered.lower()
        assert "crew-log roster" not in rendered.lower()


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
