"""The Subagents panel's durable half, read out of the CREW LOG.

A panel's live source is gateway memory, so a replacement process has nothing to
show for the runs it never tracked. The durable answer is a fold of the log --
the record itself -- rather than a second store kept in step with it by hand, so
a card's outcome, duration and cost cannot disagree with the record without the
record being wrong.

What is pinned here is what a reader would otherwise have to trust:

* only a CLOSED child is replayed, because a card for an open one would wear a
  running pill nothing in this process will ever advance;
* an outcome the client cannot draw never resolves to a success;
* the cross-app guard still refuses a slot key another app has re-minted, which
  is the one thing a slot-keyed durable read cannot get wrong;
* a dismissed card does not come back, and is skipped before the cap;
* the newest-first cap is ordered across SESSIONS, which seqs cannot do.
"""

from __future__ import annotations

import contextlib
import os
import pathlib
import stat

import pytest

from kiro_crew import crew_log as lg
from kiro_crew.crew_log import CrewLog
from kiro_crew.dashboard.ws import (
    build_persisted_subagent_frame,
    fold_subagent_outcome,
    read_fold_subagent_records,
)

GATEWAY = "gateway"
DAY = 86_400.0


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Every test writes into its own data home, never the live one."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    yield


def _log(unit_id: str, *, slot: str = "chat-1", app: str = "") -> CrewLog:
    """One crew-log UNIT, with *slot* in its header.

    The unit id and the slot are deliberately unlike each other. A unit is the ACP
    session id the log was opened under, which is neither the slot nor the slot's
    session key -- and a test whose unit id happened to BE the session key passed
    while production folded an empty log.
    """
    handle = CrewLog.create(lg.KIND_SESSION, unit_id, owner="raymond", agent="kirocrew", slot=slot)
    recorded: dict = {"memory": "persistent"}
    if app:
        recorded["app"] = app
    handle.append(
        "session/opened",
        {
            "agent": "kirocrew",
            "slot": slot,
            "model": "opus",
            "cwd": "/w",
            "owner": "raymond",
            "resumed": False,
            "class": recorded,
        },
        src=GATEWAY,
    )
    return handle


def _spawn(handle: CrewLog, agent_id: str, **data) -> None:
    handle.append("subagent/spawned", {"agent_id": agent_id, **data}, src=GATEWAY)


def _done(handle: CrewLog, agent_id: str, **data) -> None:
    handle.append("subagent/completed", {"agent_id": agent_id, **data}, src=GATEWAY)


def _failed(handle: CrewLog, agent_id: str, **data) -> None:
    handle.append("subagent/failed", {"agent_id": agent_id, **data}, src=GATEWAY)


def _dismissed(handle: CrewLog, agent_id: str) -> None:
    handle.append("subagent/dismissed", {"agent_id": agent_id}, src=GATEWAY)


def _read(units, *, keep: int = 50, max_age_secs: float = DAY, seen=(), admit=None):
    return read_fold_subagent_records(
        list(units),
        keep=keep,
        max_age_secs=max_age_secs,
        exclude_ids=seen,
        admit=admit if admit is not None else (lambda _record: True),
    )


def _ids(result) -> list[str]:
    return [record["id"] for record in result[0]]


def _stamps_and_seqs(units) -> set[tuple[float, int]]:
    """The ``(started_ms, seq_spawned)`` pairs the fold offers for *units*.

    Read back so a tie test asserts its own precondition instead of assuming it. A
    tie that did not happen makes every assertion below it pass for the wrong
    reason, and that is indistinguishable from the code being right.
    """
    from kiro_crew.crew_log import projection as crew_log

    pairs = set()
    for unit in units:
        rows = crew_log.fold_session(unit, ("subagents",)).projection("subagents").value
        for row in (rows.get("by_id") or {}).values():
            pairs.add((float(row["started_ms"]), int(row["seq_spawned"])))
    return pairs


@contextlib.contextmanager
def _frozen_clock():
    """Append entries that genuinely SHARE one envelope stamp.

    What a host whose clock granularity is coarser than a millisecond does on its
    own: every child of one burst gets the same ``time``, so the rows tie on the
    only key the fold keeps for dating them. Frozen at the store's own clock
    because that is where the stamp comes from -- a row's value cannot be patched
    afterwards, since the reader asks the bundle for its own projection and the
    render is computed from the entries again.

    Frozen at ONE reading of the real clock rather than a fixed past instant, so
    the rows tie AND stay inside the replay's age window; a fixed stamp in the
    past is dropped by the cutoff and every assertion then passes on an empty
    read.
    """
    from kiro_crew.crew_log import store

    real = store.now_ms
    stamp_ms = real()
    store.now_ms = lambda: stamp_ms
    try:
        yield stamp_ms
    finally:
        store.now_ms = real


class TestWhatTheFoldGivesACard:
    def test_a_finished_child_rebuilds_with_its_task_outcome_duration_and_cost(self):
        handle = _log("acp-1")
        _spawn(handle, "a-1", agent="kirocrew-worker", model="opus", task="audit the gate")
        _done(handle, "a-1", ms=4200, credits=1.5)

        records, overflow = _read([("chat-1", "acp-1")])
        assert overflow == 0
        assert len(records) == 1
        record = records[0]
        assert record["id"] == "a-1"
        assert record["task"] == "audit the gate"
        assert record["agent"] == "kirocrew-worker"
        assert record["model"] == "opus"
        assert record["outcome"] == "completed"
        assert record["elapsed"] == 4.2
        assert record["credits"] == 1.5
        assert record["slot"] == "chat-1"

    def test_a_child_that_reported_no_charge_carries_no_credits_key(self):
        """Absent is not zero: only one of the two is a measurement."""
        handle = _log("acp-1")
        _spawn(handle, "a-1")
        _done(handle, "a-1", ms=10)

        records, _ = _read([("chat-1", "acp-1")])
        assert "credits" not in records[0]

    def test_a_child_still_in_flight_is_not_replayed_as_a_card(self):
        """This process is not tracking it, so the pill would never advance.

        The fold's own ``running`` count is what states those, and the panel
        section draws it.
        """
        handle = _log("acp-1")
        _spawn(handle, "open-1")
        _spawn(handle, "closed-1")
        _done(handle, "closed-1", ms=5)

        assert _ids(_read([("chat-1", "acp-1")])) == ["closed-1"]

    def test_a_live_id_is_skipped_so_memory_is_never_displaced_by_the_log(self):
        handle = _log("acp-1")
        _spawn(handle, "live-1")
        _done(handle, "live-1", ms=5)
        _spawn(handle, "cold-1")
        _done(handle, "cold-1", ms=5)

        assert _ids(_read([("chat-1", "acp-1")], seen={"live-1"})) == ["cold-1"]

    def test_a_unit_with_no_log_at_all_contributes_nothing_and_does_not_raise(self):
        assert _read([("chat-9", "acp-never-written")]) == ([], 0)

    def test_a_slot_with_no_session_and_a_session_with_no_slot_are_both_skipped(self):
        """No slot is no card to route to; no session is no log to fold."""
        handle = _log("acp-1")
        _spawn(handle, "a-1")
        _done(handle, "a-1", ms=5)

        assert _read([("", "acp-1"), ("chat-1", "")]) == ([], 0)


class TestAnEndingACardCannotDrawIsNeverASuccess:
    """The client's terminal precedence falls through to ``done`` for an outcome
    it does not recognise, and the fold keeps the runtime's OWN open enum -- so
    the mapping has to happen before a frame is built, not in the reducer."""

    def test_a_completion_a_stop_and_a_failure_keep_their_own_meaning(self):
        assert fold_subagent_outcome("completed", "") == ("completed", "", False)
        assert fold_subagent_outcome("stopped", "boom") == ("stopped", "", True)
        assert fold_subagent_outcome("failed", "boom") == ("failed", "boom", False)

    @pytest.mark.parametrize("recorded", ["unknown", "evicted", "", "Completed"])
    def test_anything_else_is_a_failure_and_says_it_was_orphaned(self, recorded):
        ending, error, stopped = fold_subagent_outcome(recorded, "")
        assert ending == "failed"
        assert error == "Orphaned (unknown cause)"
        assert stopped is False

    def test_a_crash_repaired_child_reaches_the_card_as_a_failure(self):
        """The whole chain: crash-repair's closer writes ``unknown`` and no reason."""
        handle = _log("acp-1")
        _spawn(handle, "a-1", task="t")
        _failed(handle, "a-1", outcome="unknown")

        records, _ = _read([("chat-1", "acp-1")])
        assert records[0]["outcome"] == "failed"
        frame = build_persisted_subagent_frame(records[0], redact=lambda text: text)
        assert frame["data"]["outcome"] == "failed"
        assert frame["data"]["error"] == "Orphaned (unknown cause)"

    def test_a_stop_carries_no_error_text(self):
        """A stop drawn with error text reads as a failure, which it is not."""
        handle = _log("acp-1")
        _spawn(handle, "a-1")
        _failed(handle, "a-1", outcome="stopped", reason="user stopped it")

        records, _ = _read([("chat-1", "acp-1")])
        assert records[0]["stopped"] is True
        assert records[0]["error"] == ""


class TestALogWrittenBeforeTheTaskFieldDrawsNoTaskLine:
    def test_the_record_and_the_frame_both_decline_to_claim_a_task(self):
        """``""`` on the record is "this log does not say", and the frame OMITS it.

        A blank task line is the failure this avoids: the client's reducer reads an
        absent key as no claim and keeps whatever it already held, where an empty
        string would overwrite it and the card would draw an empty block.
        """
        handle = _log("acp-1")
        _spawn(handle, "old-1", agent="kirocrew-worker")  # no `task`, as older writers wrote
        _done(handle, "old-1", ms=5)

        records, _ = _read([("chat-1", "acp-1")])
        assert records[0]["task"] == ""
        frame = build_persisted_subagent_frame(records[0], redact=lambda text: text)
        assert "task" not in frame["data"]
        # The rest of the card is whole, so the absence costs it only that line.
        assert frame["data"]["outcome"] == "completed"
        assert frame["data"]["agent"] == "kirocrew-worker"

    def test_a_recorded_task_is_present_on_the_frame(self):
        """Control: the assertion above fails for the right reason."""
        handle = _log("acp-1")
        _spawn(handle, "new-1", task="audit the gate")
        _done(handle, "new-1", ms=5)

        records, _ = _read([("chat-1", "acp-1")])
        frame = build_persisted_subagent_frame(records[0], redact=lambda text: text)
        assert frame["data"]["task"] == "audit the gate"


class TestTheCrossAppGuardStillRefusesAReMintedSlotKey:
    """A slot key is caller-supplied and not namespaced by app, so the key one
    app's run was recorded under can later be minted by a DIFFERENT app. The
    folded path answers the guard from the ``class`` fold's latched ``app``, and
    this is the test that it answers the same way the folder reader did."""

    def test_the_record_carries_the_logs_own_recorded_owner(self):
        handle = _log("acp-1", app="mochi-pet")
        _spawn(handle, "a-1")
        _done(handle, "a-1", ms=5)

        records, _ = _read([("chat-1", "acp-1")])
        assert records[0]["app"] == "mochi-pet"

    def test_a_session_no_app_owns_carries_an_empty_owner(self):
        handle = _log("acp-1")
        _spawn(handle, "a-1")
        _done(handle, "a-1", ms=5)

        records, _ = _read([("chat-1", "acp-1")])
        assert records[0]["app"] == ""

    def test_another_app_holding_the_slot_now_is_refused_the_old_run(self):
        """The hostile case, end to end.

        ``mochi-pet`` dispatched the child and the log records that. By the time a
        socket reconnects, ``other-app`` holds a slot under the same key. The
        guard compares the live owner against the LOG's recorded owner and
        refuses, so the old app's run is never handed to the new one.
        """
        from kiro_crew.dashboard.ws_event_scope import persisted_replay_denial_reason

        handle = _log("acp-1", app="mochi-pet")
        _spawn(handle, "a-1", task="the first app's work")
        _done(handle, "a-1", ms=5)
        records, _ = _read([("chat-1", "acp-1")])

        reclaimed = _FakeState({"chat-1": "other-app"})
        assert persisted_replay_denial_reason(reclaimed, "chat-1", records[0]) == (
            "persisted_owner_mismatch"
        )
        # Control: the SAME record is admitted to the owner the log recorded, so
        # the refusal above is the app comparison and not a broken record.
        still_owned = _FakeState({"chat-1": "mochi-pet"})
        assert persisted_replay_denial_reason(still_owned, "chat-1", records[0]) == ""

    def test_a_person_never_receives_an_apps_run_and_an_app_never_a_persons(self):
        """Equality both ways, which is what fail-closed means here."""
        from kiro_crew.dashboard.ws_event_scope import persisted_replay_denial_reason

        owned = _log("acp-app", app="mochi-pet")
        _spawn(owned, "app-1")
        _done(owned, "app-1", ms=5)
        plain = _log("acp-user", slot="chat-2")
        _spawn(plain, "user-1")
        _done(plain, "user-1", ms=5)

        app_record = _read([("chat-1", "acp-app")])[0][0]
        user_record = _read([("chat-2", "acp-user")])[0][0]
        unowned_slot = _FakeState({"chat-1": "", "chat-2": ""})
        app_slot = _FakeState({"chat-1": "mochi-pet", "chat-2": "mochi-pet"})
        assert persisted_replay_denial_reason(unowned_slot, "chat-1", app_record) == (
            "persisted_owner_mismatch"
        )
        assert persisted_replay_denial_reason(app_slot, "chat-2", user_record) == (
            "persisted_owner_mismatch"
        )
        # And each is admitted to the owner that matches it.
        assert persisted_replay_denial_reason(unowned_slot, "chat-2", user_record) == ""
        assert persisted_replay_denial_reason(app_slot, "chat-1", app_record) == ""


class _FakeState:
    """A dashboard state that answers only what the guard asks: a slot's owner."""

    def __init__(self, owners: dict[str, str]) -> None:
        self._slots = {key: _FakeSlot(app) for key, app in owners.items()}

    def get_slot(self, key: str):
        return self._slots.get(key)


class _FakeSlot:
    def __init__(self, app: str) -> None:
        self._app = app


class TestADismissedCardDoesNotComeBack:
    """The user clearing a card is a fact about the SESSION, so it is in the log.

    A store beside the log cannot hold it. The folder registry is keyed on the
    run's FOLDER at both ends -- the write refuses without one, and the sweep drops
    a record whose folder has gone -- so it is reclaimed on a different schedule
    from the thing it suppresses: a delivered folder goes within the hour while the
    log keeps the child for the replay's whole day-long window. The log does not
    forget, which is the property these pin.
    """

    def test_a_dismissed_run_is_not_replayed(self):
        handle = _log("acp-1")
        _spawn(handle, "gone-1")
        _done(handle, "gone-1", ms=5)
        _spawn(handle, "keep-1")
        _done(handle, "keep-1", ms=5)
        _dismissed(handle, "gone-1")

        assert _ids(_read([("chat-1", "acp-1")])) == ["keep-1"]

    def test_no_run_folder_is_needed_for_the_dismissal_to_hold(self):
        """The defect this moved to fix, stated directly.

        These runs exist only in the log -- no folder was ever created for them,
        which is also the state every run reaches once its folder is pruned. The
        dismissal still holds, because the record is the log entry.
        """
        from kiro_crew.subagent_persistence import (
            DISMISSAL_NO_FOLDER,
            record_panel_dismissal_outcome,
        )

        handle = _log("acp-1")
        _spawn(handle, "gone-1")
        _done(handle, "gone-1", ms=5)
        # Control: the folder-keyed registry genuinely cannot record this one, so
        # the assertion below is about the log and not about a second store.
        assert record_panel_dismissal_outcome("gone-1") == DISMISSAL_NO_FOLDER

        _dismissed(handle, "gone-1")
        assert _ids(_read([("chat-1", "acp-1")])) == []

    def test_a_dismissed_run_does_not_spend_a_cap_slot_a_visible_run_needs(self):
        """The fold stops offering the row, so the cap never sees it."""
        handle = _log("acp-1")
        for index in range(4):
            _spawn(handle, f"a-{index}")
            _done(handle, f"a-{index}", ms=5)
        _dismissed(handle, "a-3")
        _dismissed(handle, "a-2")

        records, overflow = _read([("chat-1", "acp-1")], keep=2)
        assert _ids((records, overflow)) == ["a-1", "a-0"]
        assert overflow == 0

    def test_a_child_dismissed_while_still_running_is_cleared_too(self):
        """A dismissal is not an ending, so it can precede any closer."""
        handle = _log("acp-1")
        _spawn(handle, "open-1")
        _dismissed(handle, "open-1")
        _done(handle, "open-1", ms=5)

        assert _ids(_read([("chat-1", "acp-1")])) == []

    def test_what_the_session_spent_is_unchanged_by_a_dismissal(self):
        """Tidying the panel must not make the session look cheaper.

        The row leaves the drawn set; the totals are facts about the session and
        stay where they are, reconciled by the published ``dismissed`` count.
        """
        from kiro_crew.crew_log import projection as crew_log

        handle = _log("acp-1")
        _spawn(handle, "a-1")
        _done(handle, "a-1", ms=5, credits=2.0)
        _dismissed(handle, "a-1")

        value = crew_log.fold_session("acp-1", names=("subagents",)).projection("subagents").value
        assert value["totals"]["spawned"] == 1
        assert value["totals"]["completed"] == 1
        assert value["by_id"] == {}
        assert value["dismissed"] == 1
        # The identity a reader can check, with its third term.
        assert value["totals"]["spawned"] == (
            len(value["by_id"]) + value["omitted"] + value["dismissed"]
        )


class TestTheCapAndTheAgeWindow:
    def test_the_newest_survive_the_cap_and_the_rest_are_counted(self):
        handle = _log("acp-1")
        for index in range(5):
            _spawn(handle, f"a-{index}")
            _done(handle, f"a-{index}", ms=5)

        records, overflow = _read([("chat-1", "acp-1")], keep=2)
        assert _ids((records, overflow)) == ["a-4", "a-3"]
        assert overflow == 3

    def test_a_cap_of_zero_reads_nothing_at_all(self):
        handle = _log("acp-1")
        _spawn(handle, "a-1")
        _done(handle, "a-1", ms=5)

        assert _read([("chat-1", "acp-1")], keep=0) == ([], 0)

    def test_the_cap_orders_across_sessions_which_seqs_cannot(self):
        """Why the row keeps the spawn entry's own stamp.

        Each log numbers its seqs from 1, so the first child of session two ties
        with the first child of session one. Ordered by seq, the cap would keep an
        arbitrary one; ordered by the stamp it keeps the newest.
        """
        first = _log("acp-one", slot="chat-1")
        _spawn(first, "one-1")
        _done(first, "one-1", ms=5)
        second = _log("acp-two", slot="chat-2")
        _spawn(second, "two-1")
        _done(second, "two-1", ms=5)

        units = [("chat-1", "acp-one"), ("chat-2", "acp-two")]
        assert _ids(_read(units, keep=1)) == ["two-1"]
        # Both are returned when the cap admits them, newest first.
        assert _ids(_read(units)) == ["two-1", "one-1"]

    def test_a_run_older_than_the_window_is_not_reached(self):
        handle = _log("acp-1")
        _spawn(handle, "a-1")
        _done(handle, "a-1", ms=5)

        assert _read([("chat-1", "acp-1")], max_age_secs=0.000_001) == ([], 0)

    def test_a_window_of_zero_means_no_window_rather_than_no_runs(self):
        """Parity with the folder reader: a non-positive age reaches all the way back."""
        handle = _log("acp-1")
        _spawn(handle, "a-1")
        _done(handle, "a-1", ms=5)

        assert _ids(_read([("chat-1", "acp-1")], max_age_secs=0.0)) == ["a-1"]


class TestTheCallersOwnBoundsRunBeforeTheCap:
    def test_a_record_the_caller_may_not_see_does_not_spend_a_cap_slot(self):
        """Filtering after the cap would let a foreign run displace the caller's
        own, and would make the cut count describe a population that is not the
        caller's -- which also discloses how many foreign runs exist."""
        handle = _log("acp-1")
        for index in range(4):
            _spawn(handle, f"a-{index}")
            _done(handle, f"a-{index}", ms=5)

        records, overflow = _read(
            [("chat-1", "acp-1")],
            keep=2,
            admit=lambda record: record["id"] in {"a-0", "a-1"},
        )
        assert _ids((records, overflow)) == ["a-1", "a-0"]
        assert overflow == 0

    def test_the_bound_sees_the_whole_record_it_is_deciding_about(self):
        """It decides on ownership and visibility, so it needs the parent and app."""
        handle = _log("acp-1", app="mochi-pet")
        _spawn(handle, "a-1")
        _done(handle, "a-1", ms=5)
        seen: list[dict] = []

        _read([("chat-1", "acp-1")], admit=lambda record: seen.append(record) or True)
        assert seen[0]["slot"] == "chat-1"
        assert seen[0]["app"] == "mochi-pet"


class TestASlotsUnitsAreFoundByItsLogHeadersNotItsSessionKey:
    """The resolution the replay's call site does, which the reader cannot check.

    A unit is the ACP session id its log was opened under. A slot's session key is
    not that id, so folding the key reads a log that does not exist and the panel
    rebuilds nothing -- the whole feature, failing silently. A slot also owns one
    id AT A TIME: a reset, a model switch or a provider swap cold-starts a new one,
    so one slot's finished children are spread over a unit per id it ran under and
    a reader that looks at only the current id misses the rest.
    """

    def test_the_slots_units_come_from_the_headers_that_name_it(self):
        from kiro_crew.dashboard.ws import crew_log_panel_units

        _log("acp-live", slot="chat-1")
        assert crew_log_panel_units(["chat-1"]) == [("chat-1", "acp-live")]
        # And the slot's SESSION KEY names no unit, which is the mistake this pins
        # against: folding it would read nothing and report an empty panel.
        assert crew_log_panel_units(["dashboard:chat-1"]) == []

    def test_a_slot_that_has_run_under_several_ids_offers_every_one(self):
        """A child dispatched before a reset is in a unit the current id does not name."""
        from kiro_crew.dashboard.ws import crew_log_panel_units

        retired = _log("acp-before-reset", slot="chat-1")
        _spawn(retired, "old-1", task="dispatched before the reset")
        _done(retired, "old-1", ms=5)
        current = _log("acp-after-reset", slot="chat-1")
        _spawn(current, "new-1")
        _done(current, "new-1", ms=5)

        units = crew_log_panel_units(["chat-1"])
        assert {unit for _slot, unit in units} == {"acp-before-reset", "acp-after-reset"}
        assert sorted(_ids(_read(units))) == ["new-1", "old-1"]

    def test_every_slots_units_come_back_from_one_scan(self):
        """The caller serves every slot at once, so it asks once rather than per slot."""
        from kiro_crew.dashboard.ws import crew_log_panel_units

        _log("acp-a", slot="chat-1")
        _log("acp-b", slot="chat-2")

        assert sorted(crew_log_panel_units(["chat-1", "chat-2"])) == [
            ("chat-1", "acp-a"),
            ("chat-2", "acp-b"),
        ]
        # A name with no log of its own contributes no pair, rather than a pair
        # with an empty unit that the reader would then have to refuse.
        assert crew_log_panel_units(["chat-1", "chat-nothing"]) == [("chat-1", "acp-a")]

    def test_the_record_carries_the_slot_because_a_unit_id_cannot_yield_one(self):
        """What the frame routes by and the ownership gate asks about.

        A unit id is an ACP session id, so the slot cannot be derived back from it
        the way it can from a session key -- it is carried from the pair instead.
        """
        handle = _log("acp-1", slot="chat-7")
        _spawn(handle, "a-1")
        _done(handle, "a-1", ms=5)

        records, _ = _read([("chat-7", "acp-1")])
        assert records[0]["slot"] == "chat-7"
        assert records[0]["unit"] == "acp-1"
        frame = build_persisted_subagent_frame(records[0], redact=lambda text: text)
        assert frame["data"]["slot"] == "chat-7"

    def test_a_folder_record_still_derives_its_slot_from_the_session_key(self):
        """The other producer of these frames carries a session key, not a slot.

        ``GET /api/spawn``'s records and the crew-log-off replay come from the run
        folders, where the field is the parent's session key. The frame has to
        serve both, so the slot is read when present and derived when it is not.
        """
        frame = build_persisted_subagent_frame(
            {"id": "a", "parent_session": "dashboard:chat-3", "outcome": "completed", "error": ""},
            redact=lambda text: text,
        )
        assert frame["data"]["slot"] == "chat-3"


class TestFindingTheUnitThatHoldsAChild:
    """``unit_holding_child``: where a LATER fact about a child has to be written.

    The two cheaper answers both name the wrong unit. The emitter's spawn pin is
    released by the terminal report, so it answers nothing once a run has finished;
    the slot's current session id is the unit work is landing in NOW, which the
    resolver's own contract says is not enough to find the unit an earlier fact
    went to.
    """

    def test_the_unit_holding_the_row_is_the_one_returned(self):
        from kiro_crew.crew_log.resolve import unit_holding_child

        _log("acp-other", slot="chat-1")
        held = _log("acp-holder", slot="chat-1")
        _spawn(held, "a-1")
        _done(held, "a-1", ms=5)

        assert unit_holding_child("chat-1", "a-1") == "acp-holder"

    def test_a_child_dispatched_before_a_reset_is_found_in_the_retired_unit(self):
        from kiro_crew.crew_log.resolve import unit_holding_child

        retired = _log("acp-before", slot="chat-1")
        _spawn(retired, "old-1")
        _done(retired, "old-1", ms=5)
        _log("acp-after", slot="chat-1")  # the id the slot is landing work in now

        assert unit_holding_child("chat-1", "old-1") == "acp-before"

    def test_an_already_dismissed_row_answers_nothing(self):
        """The render stopped offering it, so there is no card left to clear."""
        from kiro_crew.crew_log.resolve import unit_holding_child

        handle = _log("acp-1", slot="chat-1")
        _spawn(handle, "a-1")
        _done(handle, "a-1", ms=5)
        assert unit_holding_child("chat-1", "a-1") == "acp-1"

        _dismissed(handle, "a-1")
        assert unit_holding_child("chat-1", "a-1") == ""

    def test_an_unknown_child_and_an_unknown_slot_both_answer_nothing(self):
        from kiro_crew.crew_log.resolve import unit_holding_child

        handle = _log("acp-1", slot="chat-1")
        _spawn(handle, "a-1")
        _done(handle, "a-1", ms=5)
        assert unit_holding_child("chat-1", "never-ran") == ""
        assert unit_holding_child("chat-nope", "a-1") == ""
        assert unit_holding_child("", "a-1") == ""
        assert unit_holding_child("chat-1", "") == ""


class TestTheCapsOrderIsTotalSoTiedRowsCannotReachTheRecord:
    """A heap tuple whose keys all tie falls through to the dict and raises.

    ``started_ms`` is a millisecond clock, so a host whose granularity is coarser
    hands every child of one burst the same stamp. ``seq_spawned`` is per log, and
    each log numbers its own from 1, so two units tie on both keys. The failure is
    not a wrong order: ``TypeError`` propagates out of the read and the reconnect
    handler discards the whole durable replay, so the panel rebuilds nothing.

    The stamps are tied at the SOURCE, by freezing the store's clock while the
    entries are appended. A row's value cannot be patched after the fact: the
    reader asks the bundle for its own projection, whose render is computed from
    the entries again, so a patched row is simply not the row it reads.
    """

    def test_rows_tied_on_stamp_and_seq_across_units_still_read(self):
        """Two units, both keys equal. Without a unique key this raises TypeError."""
        with _frozen_clock():
            first = _log("acp-1", slot="chat-1")
            _spawn(first, "a-1")
            _done(first, "a-1", ms=5)
            second = _log("acp-2", slot="chat-1")
            _spawn(second, "b-1")
            _done(second, "b-1", ms=5)
        # The precondition this test exists for, asserted rather than assumed.
        assert (
            len(_stamps_and_seqs(["acp-1", "acp-2"])) == 1
        ), "both units' rows must tie on BOTH keys, or the dict is never reached"

        records, overflow = _read([("chat-1", "acp-1"), ("chat-1", "acp-2")])
        assert sorted(record["id"] for record in records) == ["a-1", "b-1"]
        assert overflow == 0

    def test_the_cap_itself_holds_when_every_key_ties(self):
        """``keep`` displaces through the same comparison, so it raises there too."""
        with _frozen_clock():
            first = _log("acp-1", slot="chat-1")
            second = _log("acp-2", slot="chat-1")
            for index in range(3):
                _spawn(first, f"a-{index}")
                _done(first, f"a-{index}", ms=5)
                _spawn(second, f"b-{index}")
                _done(second, f"b-{index}", ms=5)
        # Three rows per unit, and the two units' seqs are the same three numbers.
        assert len(_stamps_and_seqs(["acp-1", "acp-2"])) == 3

        records, overflow = _read([("chat-1", "acp-1"), ("chat-1", "acp-2")], keep=2)
        assert len(records) == 2
        assert overflow == 4

    def test_tied_stamps_are_ordered_by_dispatch_not_by_heap_accident(self):
        """The stamps tie but the seqs do not, which is one unit on a coarse clock.

        Sorting on the stamp alone leaves these rows in whatever order the heap
        array held them, and a stable sort then publishes that order as the answer.
        On a clock fine enough to separate them the same code looks right, which is
        how this passes on one platform and fails on another for identical logs.
        """
        with _frozen_clock():
            handle = _log("acp-1", slot="chat-1")
            for index in range(4):
                _spawn(handle, f"a-{index}")
                _done(handle, f"a-{index}", ms=5)
        assert len({stamp for stamp, _seq in _stamps_and_seqs(["acp-1"])}) == 1
        assert len(_stamps_and_seqs(["acp-1"])) == 4

        # Newest first is HIGHEST seq first, which is the dispatch order reversed.
        assert _ids(_read([("chat-1", "acp-1")])) == ["a-3", "a-2", "a-1", "a-0"]

    def test_the_same_logs_read_twice_give_the_same_order(self):
        """Nothing in a read may depend on how the heap happened to come out."""
        with _frozen_clock():
            handle = _log("acp-1", slot="chat-1")
            for index in range(4):
                _spawn(handle, f"a-{index}")
                _done(handle, f"a-{index}", ms=5)

        once = _ids(_read([("chat-1", "acp-1")]))
        twice = _ids(_read([("chat-1", "acp-1")]))
        assert once == twice
        assert sorted(once) == ["a-0", "a-1", "a-2", "a-3"]
        # And the cap keeps a PREFIX of that same order rather than a fresh one.
        assert _ids(_read([("chat-1", "acp-1")], keep=2)) == once[:2]


class TestADismissalMadeBeforeTheLogHeldOneIsCorrectedIntoTheLog:
    """The record is fixed, not the reader.

    A dismissal made before this entry type existed sits only in the folder
    registry. A reader consulting that registry alongside the fold would keep two
    records of one fact about the session, which is the arrangement this panel path
    exists to end -- and the registry is keyed on the run's folder at both ends, so
    a prune takes the dismissal away and the card returns. Appending the missing
    entry to the unit that holds the child ends both: the log carries the fact, and
    the registry is never asked about that child again.
    """

    def _legacy_dismissal(self, monkeypatch, dismissed: set[str], asked: list[str]):
        """The legacy registry, as a read-only input that counts its own reads."""
        import kiro_crew.subagent_persistence as persistence

        def _recorded(agent_id: str) -> bool:
            asked.append(agent_id)
            return agent_id in dismissed

        monkeypatch.setattr(persistence, "panel_dismissal_recorded", _recorded)

    def test_a_pre_upgrade_dismissal_stays_hidden_across_two_reconnects(self, monkeypatch):
        from kiro_crew.crew_log import emit as crew_log_emit
        from kiro_crew.dashboard.ws import backfill_legacy_panel_dismissals

        monkeypatch.setenv("KIROCREW_CREW_LOG", "1")
        handle = _log("acp-1", slot="chat-1")
        for agent_id in ("kept-1", "cleared-1"):
            _spawn(handle, agent_id)
            _done(handle, agent_id, ms=5)
        asked: list[str] = []
        self._legacy_dismissal(monkeypatch, {"cleared-1"}, asked)
        units = [("chat-1", "acp-1")]

        # First reconnect: the registry is read, the log is corrected, and the
        # card is excluded from THIS read too -- the append is queued, so a read
        # that waited for the fold to carry it would draw the card once.
        corrected = backfill_legacy_panel_dismissals(units)
        assert corrected == frozenset({"cleared-1"})
        assert _ids(_read(units, seen=corrected)) == ["kept-1"]
        assert sorted(asked) == ["cleared-1", "kept-1"]

        # Second reconnect, reading the log the first one wrote. The card stays
        # hidden with no exclusion passed in, which is the fold carrying the
        # dismissal -- and the registry is not asked about that child again.
        assert crew_log_emit.flush(timeout=10.0) is True
        asked.clear()
        assert backfill_legacy_panel_dismissals(units) == frozenset()
        assert _ids(_read(units)) == ["kept-1"]
        assert "cleared-1" not in asked

    def test_a_child_the_registry_does_not_mark_is_left_alone(self, monkeypatch):
        """Reading the registry must not itself clear anything."""
        from kiro_crew.dashboard.ws import backfill_legacy_panel_dismissals

        handle = _log("acp-1", slot="chat-1")
        _spawn(handle, "a-1")
        _done(handle, "a-1", ms=5)
        self._legacy_dismissal(monkeypatch, set(), [])

        assert backfill_legacy_panel_dismissals([("chat-1", "acp-1")]) == frozenset()
        assert _ids(_read([("chat-1", "acp-1")])) == ["a-1"]

    def test_a_child_still_running_is_not_corrected(self, monkeypatch):
        """Its card is live, so no row this reads was ever offered for dismissal."""
        from kiro_crew.dashboard.ws import backfill_legacy_panel_dismissals

        handle = _log("acp-1", slot="chat-1")
        _spawn(handle, "a-1")
        self._legacy_dismissal(monkeypatch, {"a-1"}, [])

        assert backfill_legacy_panel_dismissals([("chat-1", "acp-1")]) == frozenset()


class TestTheGatesReadASlotFromWhicheverKeyTheRecordCarries:
    """The two durable sources name the owning conversation differently.

    A folded record carries ``slot``; a folder record carries ``parent_session``.
    A gate that reads only ``slot`` refuses every folder record for naming no
    conversation, which is the opt-out install's whole rebuild.
    """

    def test_a_folder_records_session_key_maps_to_its_slot(self):
        from kiro_crew.dashboard.ws import _replay_record_slot

        assert _replay_record_slot({"parent_session": "dashboard:chat-1"}) == "chat-1"

    def test_a_folded_records_own_slot_is_taken_as_it_stands(self):
        """Its unit id is an ACP session id, which yields no slot."""
        from kiro_crew.dashboard.ws import _replay_record_slot

        assert _replay_record_slot({"slot": "chat-7", "unit": "acp-1"}) == "chat-7"

    def test_a_record_naming_no_conversation_answers_nothing(self):
        """Which both gates refuse, rather than routing to the active tab."""
        from kiro_crew.dashboard.ws import _replay_record_slot

        assert _replay_record_slot({}) == ""
        assert _replay_record_slot({"slot": "", "parent_session": ""}) == ""


class TestTheDismissalEmitterReportsWhetherItsAppendCommitted:
    """Handover is not the record, so the emitter has to say which it is.

    ``_write`` queues and returns. A caller that will tell the user the card is
    gone needs the append's own outcome, and the absence of a permanent drop is
    not that outcome: an entry rejected at the buffer's memory ceiling finishes
    with no drop hook at all, which is precisely the wedged-writer condition the
    ceiling exists for.
    """

    @pytest.fixture(autouse=True)
    def _crew_log_on(self, monkeypatch):
        monkeypatch.setenv("KIROCREW_CREW_LOG", "1")
        yield

    def test_a_committed_append_reports_true(self):
        from kiro_crew.crew_log import emit

        _log("acp-commit", slot="chat-1")
        seen: list[bool] = []
        emit.on_subagent_dismissed("acp-commit", agent_id="a-1", on_settled=seen.append)
        assert emit.flush(timeout=10.0) is True
        assert seen == [True]

    def test_a_job_that_finds_no_log_reports_false(self, monkeypatch):
        """One of the two cases that never reach the append at all."""
        from kiro_crew.crew_log import emit

        _log("acp-nolog", slot="chat-1")
        monkeypatch.setattr(emit, "_handle", lambda session_id: None)
        seen: list[bool] = []
        emit.on_subagent_dismissed("acp-nolog", agent_id="a-1", on_settled=seen.append)
        assert emit.flush(timeout=10.0) is True
        assert seen == [False]

    def test_a_switched_off_emitter_reports_false_rather_than_staying_silent(self, monkeypatch):
        """No record was asked for, so none is owed -- and the caller must hear so.

        Staying silent would leave a caller that waits for this answer waiting out
        its whole bound for a callback that is never coming.
        """
        from kiro_crew.crew_log import emit

        monkeypatch.setenv("KIROCREW_CREW_LOG", "0")
        seen: list[bool] = []
        emit.on_subagent_dismissed("acp-off", agent_id="a-1", on_settled=seen.append)
        assert seen == [False]

    def test_the_pinned_arm_carries_the_same_answer(self, monkeypatch):
        """``dismiss_child``'s return says a unit was NAMED, not that it landed."""
        from kiro_crew.crew_log import emit

        _log("acp-pin", slot="chat-1")
        emit.forget_child_origin("a-1")
        monkeypatch.setattr(emit, "_child_origin", dict(emit._child_origin))
        # Pinned and opened through the real emitter calls, so the pin under test
        # is the one production writes rather than a value poked into the map. The
        # pin is gated on OPENED: a dismissal of a child whose spawn was never
        # recorded would be a fact with no cause.
        emit.remember_child_origin("a-1", "acp-pin", 1)
        emit.on_subagent_spawned("acp-pin", 1, agent_id="a-1", agent="w")
        emit.open_child_origin("a-1")
        assert emit.flush(timeout=10.0) is True
        assert emit.child_origin("a-1")[0] == "acp-pin"

        seen: list[bool] = []
        assert emit.dismiss_child("a-1", on_settled=seen.append) == "acp-pin"
        assert emit.flush(timeout=10.0) is True
        assert seen == [True]

    def test_an_unpinned_child_queues_nothing_so_nothing_settles(self):
        """The caller reads the empty return and goes on to search the units.

        Resolving it as a failure here would spend the caller's whole commit bound
        before the search that is the real answer.
        """
        from kiro_crew.crew_log import emit

        emit.forget_child_origin("never-pinned")
        seen: list[bool] = []
        assert emit.dismiss_child("never-pinned", on_settled=seen.append) == ""
        assert seen == []


class TestTheCapKeepsTheNewestTiedRowsNotTheFirstToArrive:
    """The displacement decides WHICH rows survive the cap, so it needs the order too.

    Comparing the stamp alone refuses to displace on a tie, so a burst of children
    sharing one millisecond keeps whichever arrived first and then reports them
    newest-first: a list that is ordered and is not the newest runs. The sort can
    only order what the heap kept.
    """

    def _tied_burst(self, count: int, unit: str = "acp-1"):
        handle = _log(unit, slot="chat-1")
        with _frozen_clock():
            for index in range(count):
                _spawn(handle, f"a-{index}")
                _done(handle, f"a-{index}", ms=5)

    def test_the_two_newest_of_four_tied_children_are_the_ones_kept(self):
        self._tied_burst(4)
        assert len({stamp for stamp, _seq in _stamps_and_seqs(["acp-1"])}) == 1

        records, overflow = _read([("chat-1", "acp-1")], keep=2)
        assert _ids((records, overflow)) == ["a-3", "a-2"]
        assert overflow == 2

    def test_what_the_cap_keeps_is_the_prefix_of_what_it_reports(self):
        """The property a reader relies on: the cap cuts the tail, not the head."""
        self._tied_burst(5)
        assert len({stamp for stamp, _seq in _stamps_and_seqs(["acp-1"])}) == 1

        whole = _ids(_read([("chat-1", "acp-1")]))
        assert _ids(_read([("chat-1", "acp-1")], keep=3)) == whole[:3]
        assert _ids(_read([("chat-1", "acp-1")], keep=1)) == whole[:1]


class TestAPermanentlyDeletedConversationsUnitsAreNotFolded:
    """A slot key is reused, so its units are not automatically its holder's.

    A permanent delete records the units it excluded. The raw store index does not
    apply that list, so folding through it hands whoever holds the slot key now the
    deleted conversation's finished cards -- task text included -- for the whole
    retention window.
    """

    def test_an_excluded_unit_contributes_no_pair(self, monkeypatch):
        from kiro_crew.dashboard.ws import crew_log_panel_units

        _log("acp-kept", slot="chat-1")
        _log("acp-deleted", slot="chat-1")
        # Control first: both units are the slot's before anything is excluded.
        assert {unit for _s, unit in crew_log_panel_units(["chat-1"])} == {
            "acp-kept",
            "acp-deleted",
        }

        import kiro_crew.session_ledger as ledger

        monkeypatch.setattr(ledger, "_excluded_units", lambda slot_key: frozenset({"acp-deleted"}))
        assert crew_log_panel_units(["chat-1"]) == [("chat-1", "acp-kept")]

    def test_an_excluded_units_cards_do_not_reach_the_panel(self, monkeypatch):
        """The whole point: the read is bounded by the pairs, so exclusion is enough."""
        import kiro_crew.session_ledger as ledger
        from kiro_crew.dashboard.ws import crew_log_panel_units

        kept = _log("acp-kept", slot="chat-1")
        _spawn(kept, "mine-1")
        _done(kept, "mine-1", ms=5)
        gone = _log("acp-deleted", slot="chat-1")
        _spawn(gone, "theirs-1", task="the deleted conversation's work")
        _done(gone, "theirs-1", ms=5)

        monkeypatch.setattr(ledger, "_excluded_units", lambda slot_key: frozenset({"acp-deleted"}))
        assert _ids(_read(crew_log_panel_units(["chat-1"]))) == ["mine-1"]

    def test_an_unreadable_exclusion_list_costs_one_slots_cards_not_every_slots(self, monkeypatch):
        """Fail CLOSED, and per slot.

        Answering with the units anyway would serve a deleted conversation's cards.
        Answering with nothing for EVERY slot would blank a panel because one
        unrelated slot's control file could not be read.
        """
        import kiro_crew.session_ledger as ledger
        from kiro_crew.dashboard.ws import crew_log_panel_units

        _log("acp-a", slot="chat-1")
        _log("acp-b", slot="chat-2")

        def _raise_for_one(slot_key: str):
            if slot_key == "chat-1":
                raise ledger.LedgerExclusionError("unreadable")
            return frozenset()

        monkeypatch.setattr(ledger, "_excluded_units", _raise_for_one)
        assert crew_log_panel_units(["chat-1", "chat-2"]) == [("chat-2", "acp-b")]


class TestAUnitWhoseOwnerCannotBeReadIsSkippedNotPublishedAsUnowned:
    """An absent owner and an unowned session are different facts.

    The cross-app guard compares the record's ``app`` with the slot's present
    owner, so publishing an unreadable owner as ``""`` makes it compare EQUAL to a
    slot that has no owning app -- and the old app's runs, task text included,
    reach whoever holds the slot key now. ``crew_log.read.recorded_class`` owns the
    three refusals (a hole in the history, a trimmed front, a class never recorded)
    and answers ``None`` for each, so the unit is skipped instead.
    """

    def _unclassed_log(self, unit_id: str, slot: str = "chat-1"):
        """A log that never records a class, which is one of the three refusals."""
        handle = CrewLog.create(
            lg.KIND_SESSION, unit_id, owner="raymond", agent="kirocrew", slot=slot
        )
        # No ``session/opened``, so nothing ever states this unit's class.
        return handle

    def test_a_unit_that_never_recorded_a_class_contributes_no_cards(self):
        from kiro_crew.crew_log import read as crew_log_read

        handle = self._unclassed_log("acp-unclassed")
        _spawn(handle, "theirs-1", task="an app-owned run")
        _done(handle, "theirs-1", ms=5)
        # The precondition, read off the function that owns the refusal.
        assert crew_log_read.recorded_class("acp-unclassed") is None

        assert _ids(_read([("chat-1", "acp-unclassed")])) == []

    def test_a_unit_that_did_record_its_class_still_contributes(self):
        """Control: the refusal must not swallow every unit."""
        from kiro_crew.crew_log import read as crew_log_read

        handle = _log("acp-classed", slot="chat-1", app="mochi-pet")
        _spawn(handle, "mine-1")
        _done(handle, "mine-1", ms=5)
        recorded = crew_log_read.recorded_class("acp-classed")
        assert recorded is not None and recorded.get("app") == "mochi-pet"

        records, _ = _read([("chat-1", "acp-classed")])
        assert [record["id"] for record in records] == ["mine-1"]
        # And the owner on the record is the one the log recorded, which is what
        # the cross-app guard compares.
        assert records[0]["app"] == "mochi-pet"

    def test_an_unreadable_owner_does_not_blank_the_other_units(self):
        """Per unit, like the exclusion list: one bad log costs its own cards."""
        good = _log("acp-good", slot="chat-1")
        _spawn(good, "mine-1")
        _done(good, "mine-1", ms=5)
        bad = self._unclassed_log("acp-bad", slot="chat-1")
        _spawn(bad, "theirs-1")
        _done(bad, "theirs-1", ms=5)

        assert _ids(_read([("chat-1", "acp-good"), ("chat-1", "acp-bad")])) == ["mine-1"]


class TestAFailedUnitSearchIsNotReadAsNoObligation:
    """ "No unit holds this child" and "the store would not say" are opposite answers.

    The search decides whether a later fact about a child has a unit to go into. A
    store fault answered as ``""`` tells the caller there is nothing to record, so
    the dismissal is published, the run is popped, and the card returns once the
    store recovers -- a store failure reported as success. The search raises
    :class:`UnitSearchFailed` for that case so neither arm can confuse the two.
    """

    def test_an_unlistable_slot_raises_rather_than_answering_nothing(self, monkeypatch):
        import kiro_crew.session_ledger as ledger
        from kiro_crew.crew_log.resolve import UnitSearchFailed, unit_holding_child

        handle = _log("acp-1", slot="chat-1")
        _spawn(handle, "a-1")
        _done(handle, "a-1", ms=5)
        # Control: the search finds it while the store answers.
        assert unit_holding_child("chat-1", "a-1") == "acp-1"

        def _boom(*_a, **_kw):
            raise OSError("the store is unreadable")

        monkeypatch.setattr(ledger, "_excluded_units", _boom)
        with pytest.raises(UnitSearchFailed):
            unit_holding_child("chat-1", "a-1")

    def test_a_unit_whose_fold_will_not_read_raises_rather_than_being_skipped(self, monkeypatch):
        """Skipping it would answer "no unit holds it" from a search that did not finish."""
        from kiro_crew.crew_log import projection as crew_log
        from kiro_crew.crew_log.resolve import UnitSearchFailed, unit_holding_child

        handle = _log("acp-1", slot="chat-1")
        _spawn(handle, "a-1")
        _done(handle, "a-1", ms=5)

        def _boom(*_a, **_kw):
            raise OSError("the fold is unreadable")

        monkeypatch.setattr(crew_log, "fold_session", _boom)
        with pytest.raises(UnitSearchFailed):
            unit_holding_child("chat-1", "a-1")

    def test_a_store_that_cannot_be_SCANNED_raises_through_the_whole_chain(self, monkeypatch):
        """The leaf scan is where the failure starts, so strict has to reach it.

        A flag re-raised only from the top function never fires: the store swallows
        its own scan failure and answers empty, so nothing is left to re-raise and
        the search reports "no unit holds this child" from a scan that never ran.

        The failure is INJECTED at the store's own root check rather than produced
        with ``chmod``, so this asserts the chain and not the host's permission
        semantics -- a directory mode of 0 does not deny reads on Windows, where the
        chmod version of this test passes while proving nothing. That the OS really
        does produce this condition is the next test's job.
        """
        from kiro_crew.crew_log import store
        from kiro_crew.crew_log.resolve import UnitSearchFailed, unit_holding_child
        from kiro_crew.session_ledger import crew_log_units

        handle = _log("acp-1", slot="chat-1")
        _spawn(handle, "a-1")
        _done(handle, "a-1", ms=5)
        assert unit_holding_child("chat-1", "a-1") == "acp-1"

        def _unscannable(kind: str):
            raise PermissionError(13, "the session root could not be read")

        monkeypatch.setattr(store, "_checked_crew_log_root", _unscannable)
        store._slot_index = None
        with pytest.raises(OSError):
            crew_log_units("chat-1", strict=True)
        # And a plain read still takes the empty listing, which is the right answer
        # for a reader: it says nothing false about what it could see.
        assert crew_log_units("chat-1") == ()
        with pytest.raises(UnitSearchFailed):
            unit_holding_child("chat-1", "a-1")

    @pytest.mark.skipif(
        os.name == "nt",
        reason="a directory mode of 0 does not deny reads on Windows, so the real "
        "unreadable-directory condition cannot be produced there",
    )
    def test_a_real_unreadable_directory_produces_that_failure(self):
        """The injected condition above is one a real host actually creates.

        Without this the chain is only ever tested against a stub, and a store that
        answered some other way on a real unreadable directory would go unnoticed.
        """
        from kiro_crew.crew_log import store
        from kiro_crew.crew_log.resolve import UnitSearchFailed, unit_holding_child

        handle = _log("acp-1", slot="chat-1")
        _spawn(handle, "a-1")
        _done(handle, "a-1", ms=5)
        assert unit_holding_child("chat-1", "a-1") == "acp-1"

        root = pathlib.Path(os.environ["KIROCREW_HOME"]) / "crew-log" / "sessions"
        # The directory's OWN mode is restored, not a literal: a hard-coded
        # permissive mode would widen whatever the store actually created, and the
        # restore has to put back what was there rather than what a test assumed.
        was = stat.S_IMODE(root.stat().st_mode)
        os.chmod(root, 0o000)
        try:
            store._slot_index = None
            with pytest.raises(UnitSearchFailed):
                unit_holding_child("chat-1", "a-1")
        finally:
            os.chmod(root, was)

    def test_a_store_nothing_has_written_answers_empty_even_under_strict(self):
        """A fresh install holds no unit for any slot, which is not indeterminate.

        Raising here would make every dismissal on a new install permanently
        retryable, since the condition never clears on its own.
        """
        from kiro_crew.crew_log.resolve import unit_holding_child
        from kiro_crew.session_ledger import crew_log_units

        # No log is created in this test, so the session root does not exist.
        assert crew_log_units("chat-1", strict=True) == ()
        assert unit_holding_child("chat-1", "a-1") == ""

    def test_a_child_no_unit_holds_still_answers_nothing(self):
        """Control: the raise must not swallow the genuine absence.

        A child whose dispatch this slot's logs never recorded has no row for a
        later fact to be about, and the caller must be free to proceed.
        """
        from kiro_crew.crew_log.resolve import unit_holding_child

        handle = _log("acp-1", slot="chat-1")
        _spawn(handle, "a-1")
        _done(handle, "a-1", ms=5)
        assert unit_holding_child("chat-1", "never-ran") == ""

    def test_an_excluded_units_child_is_not_written_into(self, monkeypatch):
        """The search resolves through the exclusion-applying resolver too.

        Writing a later fact into a deleted conversation's log would put it back in
        a record a permanent delete removed from this slot.
        """
        import kiro_crew.session_ledger as ledger
        from kiro_crew.crew_log.resolve import unit_holding_child

        handle = _log("acp-deleted", slot="chat-1")
        _spawn(handle, "a-1")
        _done(handle, "a-1", ms=5)
        assert unit_holding_child("chat-1", "a-1") == "acp-deleted"

        monkeypatch.setattr(ledger, "_excluded_units", lambda slot_key: frozenset({"acp-deleted"}))
        assert unit_holding_child("chat-1", "a-1") == ""
