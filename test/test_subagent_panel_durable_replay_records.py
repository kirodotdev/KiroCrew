"""What one run folder rebuilds into on the subagent panel.

The persisted fallback answers for the runs a replacement gateway never tracked. These
tests pin what a rebuilt record carries and what it must refuse to invent: live state wins
over disk, native and non-persistent runs get no card, the tombstone's own outcome
classifies the ending, every retained field and the row count are bounded and a cut tail is
counted, equality keys refuse the record rather than clamp it, ownership follows the app a
run was recorded under, the carry-forward read stays off the loop, a later write keeps the
recorded outcome, elapsed measures the run rather than the delivery wait, and a detached
settle batch survives a cancelled waiter. The shared helpers live in
``subagent_panel_durable_replay_helpers.py``.
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import time
from types import SimpleNamespace

import pytest
from subagent_panel_durable_replay_helpers import (  # noqa: F401 -- the fixture is requested by name
    CORRUPT,
    DAY,
    TRUNC,
    agent_root_fixture,
    ids,
    panel,
    write_record,
)

from kiro_crew.dashboard.state import (
    NATIVE_SUBAGENT_TERMINAL_TTL_SECS,
    PERSISTED_SUBAGENT_REPLAY_KEEP,
    PERSISTED_SUBAGENT_REPLAY_MAX_AGE_SECS,
)
from kiro_crew.dashboard.ws_event_scope import persisted_replay_denial_reason
from kiro_crew.subagent_persistence import (
    _PANEL_AGENT_CAP,
    _PANEL_APP_CAP,
    _PANEL_CANDIDATE_MULTIPLE,
    _PANEL_ERROR_CAP,
    _PANEL_ID_CAP,
    _PANEL_PARENT_SESSION_CAP,
    _PANEL_RESULT_CAP,
    _PANEL_TASK_CAP,
    PanelRecords,
    classify_persisted_ending,
    mark_delivered,
    read_panel_records,
    settle_delivered_batch,
    write_tombstone,
)
from kiro_crew.testing.wait import async_wait_until


class TestLiveStateWins:
    """The safety boundary: a disk record never displaces a tracked run.

    This is the property the whole fallback rests on. If disk could speak for an
    id the manager holds, a stale folder would overwrite the live card of a run
    still streaming -- turning a rebuild aid into a source of wrong answers.
    """

    def test_excluded_id_is_not_returned(self, agent_root):
        write_record(agent_root, "liveone")
        write_record(agent_root, "deadone")
        records = panel(
            keep=PERSISTED_SUBAGENT_REPLAY_KEEP,
            max_age_secs=DAY,
            exclude_ids={"liveone"},
        )
        assert ids(records) == ["deadone"]

    def test_every_id_excluded_yields_nothing(self, agent_root):
        write_record(agent_root, "aaa111")
        write_record(agent_root, "bbb222")
        records = panel(
            keep=PERSISTED_SUBAGENT_REPLAY_KEEP,
            max_age_secs=DAY,
            exclude_ids={"aaa111", "bbb222"},
        )
        assert records == []

    def test_exclusion_is_by_id_not_by_folder_order(self, agent_root):
        """A newer excluded folder must not shadow an older admissible one."""
        moment = time.time()
        write_record(agent_root, "newlive", mtime=moment - 10)
        write_record(agent_root, "olddead", mtime=moment - 1000)
        records = panel(
            keep=PERSISTED_SUBAGENT_REPLAY_KEEP,
            max_age_secs=DAY,
            exclude_ids={"newlive"},
        )
        assert ids(records) == ["olddead"]

    def test_an_excluded_folder_is_never_even_read(self, agent_root, monkeypatch):
        """Exclusion happens during the walk, so a live run costs no state read."""
        from kiro_crew import subagent_persistence

        write_record(agent_root, "liveone")
        reads: list[str] = []
        real = subagent_persistence.read_state
        monkeypatch.setattr(
            subagent_persistence,
            "read_state",
            lambda aid: (reads.append(aid), real(aid))[1],
        )
        assert panel(keep=10, max_age_secs=DAY, exclude_ids={"liveone"}) == []
        assert reads == []


class TestNativeCardsAreNotInvented:
    """Native slot-tracked cards have no durable record anywhere.

    ``create_agent_folder`` is reached only from the manager's admission pump, so
    a native run writes no folder. The fallback discovers records by walking
    folders, and these pin that the walk cannot conjure one: a native card that
    vanished with its gateway stays gone rather than reappearing as a card the
    panel cannot address.
    """

    def test_empty_registry_yields_nothing(self, agent_root):
        records = panel(keep=PERSISTED_SUBAGENT_REPLAY_KEEP, max_age_secs=DAY)
        assert records == []

    def test_manager_record_is_found_while_native_id_is_absent(self, agent_root):
        """A folder-backed run appears; a native id with no folder never does."""
        write_record(agent_root, "manager1")
        records = panel(keep=PERSISTED_SUBAGENT_REPLAY_KEEP, max_age_secs=DAY)
        assert ids(records) == ["manager1"]
        assert not any(record["id"].startswith("native:") for record in records)

    def test_a_folder_holding_only_a_result_is_not_a_record(self, agent_root):
        """No ``state.json`` means no identity, so there is nothing to replay."""
        folder = agent_root / "resultonly"
        folder.mkdir()
        (folder / "result.txt").write_text("orphan output", encoding="utf-8")
        records = panel(keep=PERSISTED_SUBAGENT_REPLAY_KEEP, max_age_secs=DAY)
        assert records == []


class TestNonPersistentRunsAreNotInvented:
    """A non-persistent run keeps its state in memory and writes no folder.

    The mode check is belt and braces on top of that: a folder whose record
    spells ``incognito`` or ``temporary`` is skipped explicitly, so the exclusion
    holds however the folder came to exist. Failing closed costs one card;
    failing open would put a private run's task text on screen.
    """

    @pytest.mark.parametrize("mode", ["incognito", "temporary", "ephemeral", ""])
    def test_non_persistent_mode_is_skipped(self, agent_root, mode):
        write_record(agent_root, "private1", memory_mode=mode)
        records = panel(keep=PERSISTED_SUBAGENT_REPLAY_KEEP, max_age_secs=DAY)
        assert records == []

    def test_mode_missing_from_the_record_is_skipped(self, agent_root):
        """An unstated mode is not assumed persistent."""
        folder = agent_root / "nomode"
        folder.mkdir()
        (folder / "state.json").write_text(
            json.dumps(
                {
                    "id": "nomode",
                    "task": "t",
                    "parent_session": "dashboard:chat-1",
                    "started": time.time() - 60,
                }
            ),
            encoding="utf-8",
        )
        records = panel(keep=PERSISTED_SUBAGENT_REPLAY_KEEP, max_age_secs=DAY)
        assert records == []

    def test_nested_execution_mode_alone_is_honoured(self, agent_root):
        """The mode is read from the execution record when the top level omits it."""
        folder = agent_root / "nested"
        folder.mkdir()
        (folder / "state.json").write_text(
            json.dumps(
                {
                    "id": "nested",
                    "task": "t",
                    "parent_session": "dashboard:chat-1",
                    "started": time.time() - 60,
                    "execution_context": {"memory_mode": "incognito"},
                }
            ),
            encoding="utf-8",
        )
        records = panel(keep=PERSISTED_SUBAGENT_REPLAY_KEEP, max_age_secs=DAY)
        assert records == []


class TestEndingClassification:
    """One classifier answers for both the list and the single-card read.

    The tombstone carries the run's own ``outcome``, so reading only ``cause``
    reports a routine user stop as a failure -- and the run's specific reason
    lives in ``detail`` while ``cause`` is a coarse bucket.
    """

    def test_a_recorded_user_stop_stays_a_stop(self, agent_root):
        write_record(agent_root, "stop111", tombstone_cause="user_stop", outcome="stopped")
        (record,) = panel(keep=10, max_age_secs=DAY)
        assert record["outcome"] == "stopped"
        assert record["stopped"] is True
        assert record["error"] == ""

    def test_a_recorded_outcome_outranks_the_cause_bucket(self, agent_root):
        """``cause`` says reaped; the outcome the run recorded says stopped."""
        write_record(agent_root, "rank111", tombstone_cause="reaped", outcome="stopped")
        (record,) = panel(keep=10, max_age_secs=DAY)
        assert record["outcome"] == "stopped"

    def test_a_recorded_failure_prefers_its_detail_over_the_bucket(self, agent_root):
        write_record(
            agent_root,
            "det111",
            tombstone_cause="error",
            outcome="failed",
            detail="provider returned 503",
        )
        (record,) = panel(keep=10, max_age_secs=DAY)
        assert record["outcome"] == "failed"
        assert record["error"] == "provider returned 503"

    def test_a_failure_without_detail_names_its_cause(self, agent_root):
        write_record(agent_root, "orph111", tombstone_cause="gateway_restart")
        (record,) = panel(keep=10, max_age_secs=DAY)
        assert record["outcome"] == "failed"
        assert record["error"] == "Orphaned: gateway_restart"

    def test_delivered_reads_as_completed(self, agent_root):
        write_record(agent_root, "done111", tombstone_cause="delivered")
        (record,) = panel(keep=10, max_age_secs=DAY)
        assert record["outcome"] == "completed"
        assert record["error"] == ""
        assert record["stopped"] is False

    def test_an_unreadable_tombstone_is_an_unknown_cause(self, agent_root):
        """An ending WAS recorded and cannot be read -- not the same as none."""
        write_record(agent_root, "corr111", tombstone_cause=CORRUPT)
        (record,) = panel(keep=10, max_age_secs=DAY)
        assert record["outcome"] == "failed"
        assert record["error"] == "Orphaned (unknown cause)"

    def test_no_tombstone_yields_no_card_rather_than_an_invented_outcome(self, agent_root):
        """A run with no recorded ending has no outcome to show.

        Calling it ``completed`` would put a green terminal card on the panel for
        a run the restart killed, contradicting the orphan notice injected for
        that same run. The reconciler yields between folders, so a tab
        reconnecting mid-scan observes this state rather than racing past it.
        """
        write_record(agent_root, "none111", tombstone_cause=None)
        assert panel(keep=10, max_age_secs=DAY) == []
        assert classify_persisted_ending(agent_root / "none111") == ("", "", False)

    def test_a_card_appears_once_the_reconciler_writes_the_ending(self, agent_root):
        """The folder is not lost -- it is waited for, then read normally."""
        write_record(agent_root, "none222", tombstone_cause=None)
        assert panel(keep=10, max_age_secs=DAY) == []
        write_record(agent_root, "none222", tombstone_cause="gateway_restart")
        (record,) = panel(keep=10, max_age_secs=DAY)
        assert record["outcome"] == "failed"
        assert record["error"] == "Orphaned: gateway_restart"

    def test_an_unrecognised_outcome_falls_back_to_the_cause(self, agent_root):
        write_record(agent_root, "junk111", tombstone_cause="gateway_restart", outcome="banana")
        (record,) = panel(keep=10, max_age_secs=DAY)
        assert record["outcome"] == "failed"
        assert record["error"] == "Orphaned: gateway_restart"

    def test_classifier_is_callable_on_its_own_for_the_single_card_read(self, agent_root):
        """The single-card endpoint shares this exact function, not a copy."""
        write_record(agent_root, "shar111", tombstone_cause="user_stop", outcome="stopped")
        assert classify_persisted_ending(agent_root / "shar111") == ("stopped", "", True)

    def test_classifier_takes_the_folder_so_one_response_resolves_it_once(self, agent_root):
        """Taking a path, not an id, is what lets a caller share its resolution."""
        write_record(agent_root, "path111", tombstone_cause="error", detail="boom")
        assert classify_persisted_ending(agent_root / "path111") == ("failed", "boom", False)
        assert classify_persisted_ending(agent_root / "absent") == ("", "", False)

    def test_elapsed_spans_start_to_death(self, agent_root):
        moment = time.time()
        write_record(agent_root, "span111", started=moment - 300, died=moment - 60)
        (record,) = panel(keep=10, max_age_secs=DAY)
        assert record["elapsed"] == pytest.approx(240.0, abs=1.0)

    def test_death_before_start_yields_no_negative_elapsed(self, agent_root):
        moment = time.time()
        write_record(agent_root, "skew111", started=moment - 60, died=moment - 300)
        (record,) = panel(keep=10, max_age_secs=DAY)
        assert record["elapsed"] == 0.0

    def test_task_and_agent_travel_with_the_record(self, agent_root):
        write_record(agent_root, "idy111", task="audit the gate", agent="kirocrew-worker")
        (record,) = panel(keep=10, max_age_secs=DAY)
        assert record["task"] == "audit the gate"
        assert record["agent"] == "kirocrew-worker"


class TestOwnership:
    def test_record_without_a_parent_is_dropped(self, agent_root):
        """An empty slot routes nowhere, and older clients read it as the active tab."""
        write_record(agent_root, "noown1", parent_session="")
        records = panel(keep=10, max_age_secs=DAY)
        assert records == []


class TestRetainedFieldsAreBounded:
    """A row count is not a memory bound: 50 rows of an unbounded task is unbounded.

    Every retained string carries its own named cap, and a value that was cut ends
    in the marker -- which travels inside the value to every reader, so no separate
    flag has to be carried and kept in step with it.
    """

    def test_task_is_clamped_and_says_it_was_cut(self, agent_root):
        write_record(agent_root, "big111", task="t" * (_PANEL_TASK_CAP + 500))
        (record,) = panel(keep=10, max_age_secs=DAY)
        assert len(record["task"]) < _PANEL_TASK_CAP + 100
        assert record["task"].startswith("t" * 100)
        assert record["task"].endswith(TRUNC)

    def test_agent_is_clamped(self, agent_root):
        write_record(agent_root, "big222", agent="a" * (_PANEL_AGENT_CAP + 500))
        (record,) = panel(keep=10, max_age_secs=DAY)
        assert len(record["agent"]) < _PANEL_AGENT_CAP + 100
        assert record["agent"].endswith(TRUNC)

    def test_error_detail_is_clamped(self, agent_root):
        write_record(
            agent_root,
            "big333",
            tombstone_cause="error",
            outcome="failed",
            detail="e" * (_PANEL_ERROR_CAP + 500),
        )
        (record,) = panel(keep=10, max_age_secs=DAY)
        assert len(record["error"]) < _PANEL_ERROR_CAP + 100
        assert record["error"].endswith(TRUNC)

    def test_result_is_clamped_and_says_it_was_cut(self, agent_root):
        write_record(agent_root, "big444", result="x" * (_PANEL_RESULT_CAP + 5000))
        (record,) = panel(keep=10, max_age_secs=DAY, include_result=True)
        assert len(record["result"]) < _PANEL_RESULT_CAP + 100
        assert record["result"].endswith(TRUNC)

    def test_a_result_exactly_at_the_cap_is_not_marked_cut(self, agent_root):
        write_record(agent_root, "exact1", result="x" * _PANEL_RESULT_CAP)
        (record,) = panel(keep=10, max_age_secs=DAY, include_result=True)
        assert record["result"] == "x" * _PANEL_RESULT_CAP
        assert not record["result"].endswith(TRUNC)

    def test_ordinary_fields_carry_no_marker(self, agent_root):
        write_record(agent_root, "small1", task="short", result="also short")
        (record,) = panel(keep=10, max_age_secs=DAY, include_result=True)
        assert record["task"] == "short"
        assert record["result"] == "also short"

    def test_the_scan_working_set_is_bounded_by_keep_not_folder_count(
        self, agent_root, monkeypatch
    ):
        """Unreadable candidates must not let the walk grow with the registry."""
        from kiro_crew import subagent_persistence

        moment = time.time()
        for index in range(60):
            write_record(
                agent_root,
                f"skip{index:04d}",
                memory_mode="incognito",
                mtime=moment - index,
            )
        reads: list[str] = []
        real = subagent_persistence.read_state
        monkeypatch.setattr(
            subagent_persistence,
            "read_state",
            lambda aid: (reads.append(aid), real(aid))[1],
        )
        records = panel(keep=2, max_age_secs=DAY)
        assert records == []
        assert len(reads) <= 2 * _PANEL_CANDIDATE_MULTIPLE


class TestBounds:
    def test_keep_caps_the_burst(self, agent_root):
        moment = time.time()
        for index in range(8):
            write_record(agent_root, f"agent{index:03d}", mtime=moment - index)
        records = panel(keep=3, max_age_secs=DAY)
        assert len(records) == 3

    def test_keep_of_zero_reads_nothing(self, agent_root):
        write_record(agent_root, "any111")
        assert panel(keep=0, max_age_secs=DAY) == []

    def test_newest_folders_are_preferred_when_the_cap_bites(self, agent_root):
        moment = time.time()
        write_record(agent_root, "newest", mtime=moment - 5)
        write_record(agent_root, "middle", mtime=moment - 500)
        write_record(agent_root, "oldest", mtime=moment - 5000)
        records = panel(keep=2, max_age_secs=DAY)
        assert ids(records) == ["newest", "middle"]

    def test_records_past_the_age_bound_are_dropped(self, agent_root):
        stale = time.time() - (3 * DAY)
        write_record(agent_root, "stale1", started=stale, died=stale, mtime=stale)
        records = panel(keep=10, max_age_secs=DAY)
        assert records == []

    def test_a_recently_touched_folder_is_still_judged_on_its_recorded_times(self, agent_root):
        """The folder's mtime orders the walk; the run's own times decide the window.

        A late write inside a folder -- a result chunk, a tombstone -- moves its
        mtime without moving the run. Folder mtime therefore cannot be the age
        decision, and this pins the check that is.
        """
        stale = time.time() - (3 * DAY)
        write_record(agent_root, "touched", started=stale, died=stale, mtime=time.time() - 5)
        records = panel(keep=10, max_age_secs=DAY)
        assert records == []

    def test_folders_outside_the_window_are_never_opened(self, agent_root, monkeypatch):
        """The age filter runs during the walk, so a stale folder costs no read."""
        from kiro_crew import subagent_persistence

        moment = time.time()
        write_record(agent_root, "fresh01", started=moment - 30, died=moment - 10, mtime=moment - 1)
        stale = moment - (5 * DAY)
        for index in range(30):
            write_record(
                agent_root,
                f"old{index:04d}",
                started=stale,
                died=stale,
                mtime=stale - index,
            )
        reads: list[str] = []
        real = subagent_persistence.read_state
        monkeypatch.setattr(
            subagent_persistence,
            "read_state",
            lambda aid: (reads.append(aid), real(aid))[1],
        )
        records = panel(keep=10, max_age_secs=DAY)
        assert ids(records) == ["fresh01"]
        assert reads == ["fresh01"]

    def test_a_record_inside_the_day_survives_the_native_hour(self, agent_root):
        """The whole reason this bound is its own number rather than the native TTL.

        A run that ended two hours before the gateway was replaced is outside the
        native terminal TTL and still inside this one. Sharing the hour-long
        constant would leave the panel empty in exactly the case the fallback
        exists to serve.
        """
        two_hours_ago = time.time() - 7200
        assert 7200 > NATIVE_SUBAGENT_TERMINAL_TTL_SECS
        write_record(
            agent_root, "twohr1", started=two_hours_ago, died=two_hours_ago, mtime=two_hours_ago
        )
        records = panel(
            keep=PERSISTED_SUBAGENT_REPLAY_KEEP,
            max_age_secs=PERSISTED_SUBAGENT_REPLAY_MAX_AGE_SECS,
        )
        assert ids(records) == ["twohr1"]

    def test_configured_bounds_are_the_approved_policy(self):
        assert PERSISTED_SUBAGENT_REPLAY_KEEP == 50
        assert PERSISTED_SUBAGENT_REPLAY_MAX_AGE_SECS == DAY


class TestResultText:
    def test_result_is_withheld_unless_asked_for(self, agent_root):
        write_record(agent_root, "res111", result="the answer")
        (record,) = panel(keep=10, max_age_secs=DAY)
        assert "result" not in record

    def test_result_is_read_when_asked_for(self, agent_root):
        write_record(agent_root, "res222", result="the answer")
        (record,) = panel(keep=10, max_age_secs=DAY, include_result=True)
        assert record["result"] == "the answer"

    def test_missing_result_file_is_empty_not_an_error(self, agent_root):
        write_record(agent_root, "res333")
        (record,) = panel(keep=10, max_age_secs=DAY, include_result=True)
        assert record["result"] == ""

    def test_a_sensitive_result_path_is_not_read(self, agent_root, monkeypatch):
        """The same guard the single-agent read applies, at the one place a file opens."""
        write_record(agent_root, "res555", result="secret material")
        monkeypatch.setattr("kiro_crew.subagent_persistence.is_sensitive_path", lambda path: True)
        (record,) = panel(keep=10, max_age_secs=DAY, include_result=True)
        assert record["result"] == ""


class TestCorruptFolders:
    def test_unreadable_state_is_skipped_without_losing_the_rest(self, agent_root):
        moment = time.time()
        broken = agent_root / "broken"
        broken.mkdir()
        (broken / "state.json").write_text("{not json", encoding="utf-8")
        os.utime(broken, (moment - 1, moment - 1))
        write_record(agent_root, "intact", mtime=moment - 2)
        records = panel(keep=10, max_age_secs=DAY)
        assert ids(records) == ["intact"]

    def test_a_file_in_the_registry_is_not_a_record(self, agent_root):
        (agent_root / "stray.json").write_text("{}", encoding="utf-8")
        write_record(agent_root, "intact")
        records = panel(keep=10, max_age_secs=DAY)
        assert ids(records) == ["intact"]

    def test_absent_registry_reads_as_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "kiro_crew.subagent_persistence._SUBAGENTS_DIR", tmp_path / "never-created"
        )
        assert panel(keep=10, max_age_secs=DAY) == []


class TestReusedSlotKeyOwnership:
    """A slot key an app's run was recorded under can later belong to another app.

    Slot keys are caller-supplied and are not namespaced by app, and the per-frame
    scope gate authorizes against the slot's CURRENT owner -- which for a reused
    key is not the owner the run belonged to. Within the replay window that hands
    the new owner the previous owner's task, agent name and error text, delivered
    once with no recall. So the run's own recorded app is compared here.
    """

    def slot(self, app: str):
        return SimpleNamespace(_app=app)

    def state(self, slots: dict):
        """A state whose ``get_slot`` behaves like the real one, plus a raw map."""
        return SimpleNamespace(_slots=slots, get_slot=slots.get)

    def test_a_slot_under_construction_denies(self):
        """``get_slot`` answers None for one, and admission must not use it."""
        state = SimpleNamespace(_slots={"chat-1": self.slot("")}, get_slot=lambda name: None)
        assert persisted_replay_denial_reason(state, "chat-1", {"app": ""}) == "slot_missing"

    @pytest.mark.parametrize(
        "record_app, slot_app, allowed",
        [
            ("", "", True),
            ("appA", "appA", True),
            ("appA", "appB", False),
            ("appA", "", False),
            ("", "appB", False),
        ],
    )
    def test_the_recorded_app_must_equal_the_slots_current_owner(
        self, record_app, slot_app, allowed
    ):
        state = self.state({"chat-1": self.slot(slot_app)})
        record = {"id": "a", "app": record_app, "parent_session": "dashboard:chat-1"}
        assert (persisted_replay_denial_reason(state, "chat-1", record) == "") is allowed

    def test_a_missing_slot_denies_because_there_is_no_owner_to_compare(self):
        state = self.state({})
        assert persisted_replay_denial_reason(state, "chat-1", {"app": ""}) == "slot_missing"

    @pytest.mark.parametrize(
        "registered_key",
        [
            pytest.param("chat-1", id="test_an_empty_slot_key_denies"),
            # The empty key is refused on its own, not merely by finding no slot.
            # A record whose parent resolves to nothing carries an empty slot, and
            # a lookup would otherwise hand it whatever sits under that key.
            pytest.param(
                "", id="test_an_empty_slot_key_denies_even_if_a_slot_is_registered_under_it"
            ),
        ],
    )
    def test_an_empty_slot_key_is_refused(self, registered_key):
        state = self.state({registered_key: self.slot("")})
        assert persisted_replay_denial_reason(state, "", {"app": ""}) == "slot_missing"

    def test_a_missing_app_key_on_the_record_reads_as_no_app(self):
        """An older record with no app field is a run no app owns."""
        state = self.state({"chat-1": self.slot("")})
        assert persisted_replay_denial_reason(state, "chat-1", {"id": "a"}) == ""
        state_app = self.state({"chat-1": self.slot("appB")})
        assert (
            persisted_replay_denial_reason(state_app, "chat-1", {"id": "a"})
            == "persisted_owner_mismatch"
        )

    def test_a_slot_without_the_attribute_reads_as_no_app(self):
        state = self.state({"chat-1": SimpleNamespace()})
        assert persisted_replay_denial_reason(state, "chat-1", {"app": ""}) == ""
        assert (
            persisted_replay_denial_reason(state, "chat-1", {"app": "appA"})
            == "persisted_owner_mismatch"
        )


class TestRecordedApp:
    def test_the_records_app_comes_from_the_run_state(self, agent_root):
        write_record(agent_root, "app111", app="my-app")
        (record,) = panel(keep=10, max_age_secs=DAY)
        assert record["app"] == "my-app"

    def test_a_run_no_app_owns_carries_an_empty_app(self, agent_root):
        write_record(agent_root, "app222")
        (record,) = panel(keep=10, max_age_secs=DAY)
        assert record["app"] == ""

    def test_the_nested_execution_app_is_used_when_the_top_level_omits_it(self, agent_root):
        folder = agent_root / "app333"
        folder.mkdir()
        (folder / "state.json").write_text(
            json.dumps(
                {
                    "id": "app333",
                    "task": "t",
                    "parent_session": "dashboard:chat-1",
                    "started": time.time() - 60,
                    "memory_mode": "persistent",
                    "execution_context": {"memory_mode": "persistent", "app": "nested-app"},
                }
            ),
            encoding="utf-8",
        )
        (folder / "tombstone.json").write_text(
            json.dumps({"cause": "delivered", "died": time.time()}), encoding="utf-8"
        )
        (record,) = panel(keep=10, max_age_secs=DAY)
        assert record["app"] == "nested-app"

    def test_an_implausible_app_id_drops_the_record_rather_than_clamping_it(self, agent_root):
        """A clamped id would compare unequal while looking like a value."""
        write_record(agent_root, "app444", app="a" * (_PANEL_APP_CAP + 1))
        assert panel(keep=10, max_age_secs=DAY) == []

    def test_an_app_id_exactly_at_the_cap_is_kept_whole(self, agent_root):
        write_record(agent_root, "app555", app="a" * _PANEL_APP_CAP)
        (record,) = panel(keep=10, max_age_secs=DAY)
        assert record["app"] == "a" * _PANEL_APP_CAP


class TestEqualityKeysAreRefusedNotClamped:
    """Three keys are compared for equality, so an oversized one refuses the record.

    ``id`` addresses the folder, ``app`` is matched against a slot's current
    owner, and ``parent_session`` is matched against a caller and resolved into a
    slot key. Clamping any of them would produce a value that still reads as a
    key while comparing unequal, so each is checked and the record dropped.
    """

    def test_an_oversized_parent_session_drops_the_record(self, agent_root):
        write_record(
            agent_root, "ps111", parent_session="dashboard:" + "k" * _PANEL_PARENT_SESSION_CAP
        )
        assert panel(keep=10, max_age_secs=DAY) == []

    def test_a_parent_session_exactly_at_the_cap_is_kept_whole(self, agent_root):
        key = "d" * _PANEL_PARENT_SESSION_CAP
        write_record(agent_root, "ps222", parent_session=key)
        (record,) = panel(keep=10, max_age_secs=DAY)
        assert record["parent_session"] == key

    def test_an_oversized_id_drops_the_record(self, agent_root, monkeypatch):
        """The id is bounded by the filesystem in practice; the check does not rely on that."""
        from kiro_crew import subagent_persistence

        write_record(agent_root, "id111")
        monkeypatch.setattr(subagent_persistence, "_PANEL_ID_CAP", 3)
        assert panel(keep=10, max_age_secs=DAY) == []

    def test_an_oversized_app_drops_the_record(self, agent_root):
        write_record(agent_root, "ap111", app="a" * (_PANEL_APP_CAP + 1))
        assert panel(keep=10, max_age_secs=DAY) == []

    def test_every_key_within_its_cap_is_admitted(self, agent_root):
        write_record(agent_root, "ok111", app="app", parent_session="dashboard:chat-1")
        (record,) = panel(keep=10, max_age_secs=DAY)
        assert record["id"] == "ok111"
        assert len(record["id"]) <= _PANEL_ID_CAP


class TestOverflowIsCountedNotHidden:
    """A cut tail must not read as a population that never held those runs.

    A rebuild showing 50 of 51 eligible runs is indistinguishable from one showing
    all 50 there were, so what the row cap left out is counted and returned with
    the records, and each consumer says it once per rebuild.
    """

    def seed(self, root, count: int) -> None:
        moment = time.time()
        for index in range(count):
            write_record(root, f"ov{index:04d}", mtime=moment - index)

    def test_no_overflow_when_everything_fits(self, agent_root):
        self.seed(agent_root, 3)
        result = read_panel_records(keep=10, max_age_secs=DAY)
        assert len(result.records) == 3
        assert result.overflow == 0
        assert result.overflow_is_lower_bound is False

    @pytest.mark.parametrize(
        ("seeded", "keep", "overflow"),
        [
            pytest.param(7, 4, 3, id="test_overflow_counts_exactly_what_the_cap_cut"),
            # One past the cap, the shape of 51 eligible with 50 shown.
            pytest.param(6, 5, 1, id="test_one_past_the_cap_is_reported"),
        ],
    )
    def test_overflow_is_counted(self, agent_root, seeded, keep, overflow):
        self.seed(agent_root, seeded)
        result = read_panel_records(keep=keep, max_age_secs=DAY)
        assert len(result.records) == keep
        assert result.overflow == overflow

    def test_overflow_counts_only_admissible_records(self, agent_root):
        """A folder past the cap that would have been skipped is not overflow."""
        moment = time.time()
        write_record(agent_root, "keep01", mtime=moment - 1)
        write_record(agent_root, "keep02", mtime=moment - 2)
        write_record(agent_root, "private", memory_mode="incognito", mtime=moment - 3)
        write_record(agent_root, "noowner", parent_session="", mtime=moment - 4)
        result = read_panel_records(keep=2, max_age_secs=DAY)
        assert ids(result.records) == ["keep01", "keep02"]
        assert result.overflow == 0

    def test_an_excluded_id_past_the_cap_is_not_overflow(self, agent_root):
        """A run the live manager holds is not something the cap withheld."""
        moment = time.time()
        write_record(agent_root, "shown1", mtime=moment - 1)
        write_record(agent_root, "livee1", mtime=moment - 2)
        result = read_panel_records(keep=1, max_age_secs=DAY, exclude_ids={"livee1"})
        assert ids(result.records) == ["shown1"]
        assert result.overflow == 0

    def test_a_record_outside_the_age_window_is_not_overflow(self, agent_root):
        moment = time.time()
        write_record(agent_root, "fresh1", mtime=moment - 1)
        stale = moment - (3 * DAY)
        write_record(agent_root, "stale1", started=stale, died=stale, mtime=stale)
        result = read_panel_records(keep=1, max_age_secs=DAY)
        assert ids(result.records) == ["fresh1"]
        assert result.overflow == 0

    def test_keep_of_zero_reports_no_overflow_rather_than_guessing(self, agent_root):
        self.seed(agent_root, 3)
        result = read_panel_records(keep=0, max_age_secs=DAY)
        assert result == PanelRecords([], 0, False)

    def test_overflow_is_flagged_as_a_floor_when_the_candidate_window_fills(
        self, agent_root, monkeypatch
    ):
        """Past the scan's own window the count can only be a lower bound."""
        from kiro_crew import subagent_persistence

        monkeypatch.setattr(subagent_persistence, "_PANEL_CANDIDATE_MULTIPLE", 1)
        self.seed(agent_root, 6)
        result = read_panel_records(keep=2, max_age_secs=DAY)
        assert len(result.records) == 2
        assert result.overflow > 0
        assert result.overflow_is_lower_bound is True

    def test_a_full_window_without_overflow_is_not_called_a_floor(self, agent_root):
        """The floor flag tracks the overflow, not the window on its own."""
        self.seed(agent_root, 3)
        result = read_panel_records(keep=10, max_age_secs=DAY)
        assert result.overflow == 0
        assert result.overflow_is_lower_bound is False

    def test_the_result_read_is_skipped_for_records_past_the_cap(self, agent_root, monkeypatch):
        """Counting overflow must not pay for output nobody will see."""
        from kiro_crew import subagent_persistence

        moment = time.time()
        for index in range(4):
            write_record(agent_root, f"rs{index:04d}", result="payload", mtime=moment - index)
        reads: list[str] = []
        real = subagent_persistence._panel_result_text
        monkeypatch.setattr(
            subagent_persistence,
            "_panel_result_text",
            lambda d: (reads.append(d.name), real(d))[1],
        )
        result = read_panel_records(keep=2, max_age_secs=DAY, include_result=True)
        assert len(result.records) == 2
        assert result.overflow == 2
        assert reads == ["rs0000", "rs0001"]


class TestTheCarryForwardReadIsNeverOnTheLoop:
    """The carry-forward added a disk READ to a write that already wrote.

    The invariant is REACHABILITY, not enclosure: a SYNC function called from a
    coroutine still runs on the loop, so checking only whether the immediate
    enclosing ``def`` is ``async`` passes a hole one level down. That is exactly
    how a settlement helper -- sync itself, awaited from two coroutines -- kept a
    synchronous write on the loop after the directly-async sites were fixed.

    So the property asserted here is: from every coroutine in the manager, no
    call path reaches the tombstone write without crossing an offload. Checked
    structurally over the call graph, because what regresses is a call site losing
    its offload or a new one arriving without one -- a property of the source, and
    a new site is the case a runtime test on today's set could not see.
    """

    MODULES = (
        "src/kiro_crew/subagent_manager/terminal.py",
        "src/kiro_crew/subagent_manager/monitoring.py",
        "src/kiro_crew/subagent_manager/waves.py",
    )
    # Resolved from this file, not from the process working directory: a worker
    # whose cwd is not the invocation root would otherwise read these paths from
    # the wrong place and the structural checks would raise ``FileNotFoundError``
    # instead of reporting on the tree. The same idiom as the other source-reading
    # tests in this directory.
    ROOT = pathlib.Path(__file__).resolve().parents[1]
    # The write itself, the helper that reaches it with no explicit outcome, and
    # the batch form a detached caller hands over whole.
    WRITERS = frozenset({"mark_delivered", "settle_delivered_batch", "write_tombstone"})

    def graph(self) -> tuple[dict, dict, dict]:
        """Per-module: who calls whom, which callees are offloaded, who is async."""
        import ast

        calls: dict[str, set[str]] = {}
        offloaded: dict[str, set[str]] = {}
        is_async: dict[str, bool] = {}
        for rel in self.MODULES:
            tree = ast.parse((self.ROOT / rel).read_text(encoding="utf-8"))
            for fn in ast.walk(tree):
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                key = f"{rel}::{fn.name}"
                is_async[key] = isinstance(fn, ast.AsyncFunctionDef)
                named: set[str] = set()
                off: set[str] = set()
                for node in ast.walk(fn):
                    if not isinstance(node, ast.Call):
                        continue
                    if getattr(node.func, "attr", "") == "to_thread" and node.args:
                        first = node.args[0]
                        nm = getattr(first, "id", None) or getattr(first, "attr", None)
                        if nm:
                            off.add(nm)
                        continue
                    nm = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
                    if nm:
                        named.add(nm)
                calls[key] = named
                offloaded[key] = off
        return calls, offloaded, is_async

    def test_no_coroutine_reaches_the_write_without_an_offload(self):
        calls, offloaded, is_async = self.graph()
        # Control: the graph must actually contain the writers somewhere, or an
        # empty result reads as a clean tree rather than a broken probe. They are
        # looked for in BOTH maps, because an offloaded writer is recorded only as
        # an offload and never as a direct call -- checking `calls` alone reported
        # a fully-offloaded tree as one the probe could not see at all.
        seen_writers = {w for names in calls.values() for w in names & self.WRITERS}
        seen_writers |= {w for names in offloaded.values() for w in names & self.WRITERS}
        assert seen_writers == self.WRITERS, f"probe found only {sorted(seen_writers)}"

        by_name: dict[str, list[str]] = {}
        for key in calls:
            by_name.setdefault(key.split("::")[1], []).append(key)

        offenders: list[str] = []
        for start, started_async in is_async.items():
            if not started_async:
                continue
            seen: set[str] = set()
            stack = [start]
            while stack:
                cur = stack.pop()
                if cur in seen:
                    continue
                seen.add(cur)
                direct = calls.get(cur, set())
                for writer in direct & self.WRITERS:
                    if writer not in offloaded.get(cur, set()):
                        offenders.append(f"{cur} -> {writer}")
                for callee in direct:
                    if callee in self.WRITERS:
                        continue
                    for key in by_name.get(callee, []):
                        # A callee whose body is offloaded here is crossed safely.
                        if callee not in offloaded.get(cur, set()):
                            stack.append(key)
        assert not offenders, "on-loop tombstone write reachable: " + "; ".join(sorted(offenders))

    def test_the_offload_uses_the_name_an_impl_method_can_resolve(self):
        """Shape is not enough: the module the offload names has to be in scope.

        A method whose name ends in ``_impl`` runs with the manager facade's
        globals, not its own module's, so a module-private alias such as
        ``_asyncio`` is NOT defined inside one -- the call raises ``NameError`` at
        runtime and a surrounding ``except Exception`` swallows it, leaving the
        write silently undone. The reachability assertion above passes either way,
        which is why this one exists beside it.
        """
        import ast

        checked = 0
        for rel in self.MODULES:
            tree = ast.parse((self.ROOT / rel).read_text(encoding="utf-8"))
            for fn in ast.walk(tree):
                if not isinstance(fn, ast.AsyncFunctionDef) or not fn.name.endswith("_impl"):
                    continue
                for node in ast.walk(fn):
                    if not isinstance(node, ast.Call):
                        continue
                    if getattr(node.func, "attr", "") != "to_thread" or not node.args:
                        continue
                    first = node.args[0]
                    nm = getattr(first, "id", None) or getattr(first, "attr", None)
                    if nm not in self.WRITERS:
                        continue
                    module = getattr(node.func.value, "id", "")
                    assert module == "asyncio", (
                        f"{rel}:{node.lineno} offloads {nm} via {module!r};"
                        " an _impl method resolves 'asyncio' only"
                    )
                    checked += 1
        # Seven offloads live in _impl methods: the delivery path, the leaked
        # delivery's mark, the two gateway_restart writes, the digest-hold
        # settlement, and the drained queue's two (the delivered mark, and a
        # restart orphan's carried record), the pattern the others follow.
        assert checked == 7, f"expected 7 _impl offloads, found {checked}"

    def test_the_source_reads_do_not_depend_on_the_working_directory(self, tmp_path, monkeypatch):
        """The modules are read from this file's own tree, not from the cwd.

        A worker whose working directory is not the invocation root would read
        these relative paths from somewhere else, and the structural checks above
        would raise ``FileNotFoundError`` instead of reporting on the tree -- a
        pass or a fail decided by where pytest happened to be standing.
        """
        monkeypatch.chdir(tmp_path)
        # Control: from here the relative spellings resolve to nothing, so a graph
        # that still comes back full can only have used the anchored root.
        assert not (tmp_path / self.MODULES[0]).exists()
        calls, _offloaded, is_async = self.graph()
        assert calls, "the call graph came back empty from a foreign cwd"
        assert any(is_async.values()), "no coroutine seen from a foreign cwd"


class TestATerminalOutcomeSurvivesALaterWrite:
    """A recorded outcome is not erased by a write that carries none.

    ``write_tombstone`` REPLACES the file and only ``extra`` can supply
    ``outcome``, so a caller with no opinion about the outcome -- a delivery
    acknowledgement is the ordinary one -- would drop what an earlier write
    recorded. The reader then derives an outcome from ``cause``, and
    ``cause="delivered"`` derives ``completed``, so a user stop reads as a
    success. The run side reaching its delivery claim after the stop is recorded
    is the ordinary sequence, not an exotic interleaving.
    """

    def folder(self, agent_root, agent_id: str):
        write_record(agent_root, agent_id, tombstone_cause=None)
        return agent_root / agent_id

    def tombstone(self, agent_root, agent_id: str) -> dict:
        return json.loads((agent_root / agent_id / "tombstone.json").read_text())

    def test_a_delivered_write_after_a_stop_keeps_the_stop(self, agent_root):
        self.folder(agent_root, "keep111")
        write_tombstone("keep111", cause="reaped", recovery_action="none", outcome="stopped")
        assert self.tombstone(agent_root, "keep111")["outcome"] == "stopped"
        # The delivery acknowledgement lands second and names no outcome.
        mark_delivered("keep111")
        after = self.tombstone(agent_root, "keep111")
        assert after["cause"] == "delivered"
        assert after["outcome"] == "stopped"
        outcome, detail, stopped = classify_persisted_ending(agent_root / "keep111")
        assert (outcome, detail, stopped) == ("stopped", "", True)

    def test_an_explicit_outcome_still_wins_over_the_carried_one(self, agent_root):
        """Carrying forward is a default, not a lock: a caller may correct it."""
        self.folder(agent_root, "keep222")
        write_tombstone("keep222", cause="reaped", recovery_action="none", outcome="stopped")
        write_tombstone("keep222", cause="error", recovery_action="none", outcome="failed")
        assert self.tombstone(agent_root, "keep222")["outcome"] == "failed"

    def test_a_delivered_write_with_no_prior_outcome_still_reads_completed(self, agent_root):
        """The ordinary success path is unchanged: nothing to carry, cause decides."""
        self.folder(agent_root, "keep333")
        mark_delivered("keep333")
        after = self.tombstone(agent_root, "keep333")
        assert "outcome" not in after
        outcome, _detail, stopped = classify_persisted_ending(agent_root / "keep333")
        assert (outcome, stopped) == ("completed", False)


class TestElapsedMeasuresTheRunNotTheDeliveryWait:
    """A card's duration is the run's, not the wait before its result was read.

    ``died`` is stamped at every write, and a delivery acknowledgement lands
    whenever the parent got round to the result -- a busy parent queues the
    announce and the drain settles it later. Two things keep the duration honest:
    a write at the ending has its ``died`` carried across later writes, and where
    no write happened at the ending at all, the run's own output file is the
    completion evidence that remains on disk.
    """

    def tombstone(self, agent_root, agent_id: str) -> dict:
        return json.loads((agent_root / agent_id / "tombstone.json").read_text())

    def test_a_later_write_does_not_move_a_recorded_ending(self, agent_root):
        moment = time.time()
        write_record(agent_root, "wait111", started=moment - 300, tombstone_cause=None)
        write_tombstone("wait111", cause="reaped", recovery_action="none", died=moment - 240)
        assert self.tombstone(agent_root, "wait111")["died"] == pytest.approx(moment - 240)
        # The acknowledgement arrives four minutes after the run ended.
        mark_delivered("wait111")
        after = self.tombstone(agent_root, "wait111")
        assert after["cause"] == "delivered"
        assert after["died"] == pytest.approx(moment - 240)
        (record,) = panel(keep=10, max_age_secs=DAY)
        assert record["elapsed"] == pytest.approx(60.0, abs=2.0)

    def test_an_explicit_ending_still_wins_over_the_carried_one(self, agent_root):
        """Carrying forward is a default, not a lock: a caller may correct it."""
        moment = time.time()
        write_record(agent_root, "wait222", started=moment - 300, tombstone_cause=None)
        write_tombstone("wait222", cause="reaped", recovery_action="none", died=moment - 240)
        write_tombstone("wait222", cause="error", recovery_action="none", died=moment - 120)
        assert self.tombstone(agent_root, "wait222")["died"] == pytest.approx(moment - 120)

    def test_a_queued_delivery_measures_to_the_runs_own_output(self, agent_root):
        """The delivery write is the FIRST tombstone here, so it carries nothing.

        This is the queued and digest-held shape: nothing is written at
        completion, so without the output file the panel would report the whole
        delivery wait as runtime.
        """
        moment = time.time()
        write_record(
            agent_root,
            "wait333",
            started=moment - 300,
            tombstone_cause=None,
            result="the answer",
        )
        os.utime(agent_root / "wait333" / "result.txt", (moment - 240, moment - 240))
        mark_delivered("wait333")
        # The only tombstone is the acknowledgement, stamped now.
        assert self.tombstone(agent_root, "wait333")["died"] == pytest.approx(moment, abs=5.0)
        (record,) = panel(keep=10, max_age_secs=DAY)
        assert record["elapsed"] == pytest.approx(60.0, abs=2.0)

    def test_a_run_with_no_output_falls_back_to_the_tombstone(self, agent_root):
        """No output file is no evidence, and the tombstone is then all there is."""
        moment = time.time()
        write_record(agent_root, "wait444", started=moment - 300, died=moment - 60)
        assert not (agent_root / "wait444" / "result.txt").exists()
        (record,) = panel(keep=10, max_age_secs=DAY)
        assert record["elapsed"] == pytest.approx(240.0, abs=2.0)

    def test_a_delivered_run_stays_in_the_panel_by_its_latest_timestamp(self, agent_root):
        """The recency window is not narrowed by the duration correction.

        A run that finished long ago but whose result reached its parent moments
        ago belongs in the panel: the window reads the latest timestamp while the
        duration reads the run's own.
        """
        moment = time.time()
        write_record(
            agent_root,
            "wait555",
            started=moment - 8000,
            tombstone_cause=None,
            result="the answer",
        )
        os.utime(agent_root / "wait555" / "result.txt", (moment - 7000, moment - 7000))
        mark_delivered("wait555")
        (record,) = panel(keep=10, max_age_secs=3600)
        assert record["id"] == "wait555"
        assert record["elapsed"] == pytest.approx(1000.0, abs=2.0)

    def test_only_a_write_with_no_outcome_reads_the_prior_tombstone(self, agent_root, monkeypatch):
        """The carry costs a disk read, so it is taken on one condition only.

        This function is synchronous and is reached from coroutines through sync
        helpers, so a read it takes lands on the event loop unless that caller
        offloads it. A caller supplying an outcome is recording an ending -- its
        own stamp IS the ending -- so taking the read there would put file I/O on
        paths that never had it, for a value that is not wanted. Counted in both
        directions, because the whole point is which callers pay it.
        """
        import kiro_crew.subagent_persistence as persistence

        write_record(agent_root, "read111", tombstone_cause=None)
        reads: list[str] = []
        real = persistence._read_tombstone_at

        def counting(path):
            reads.append(str(path))
            return real(path)

        monkeypatch.setattr(persistence, "_read_tombstone_at", counting)

        # Records an ending of its own: no read.
        write_tombstone("read111", cause="error", recovery_action="none", outcome="failed")
        assert reads == [], f"a write supplying an outcome read the prior tombstone: {reads}"

        # Pure bookkeeping, no ending of its own: the read is what preserves one.
        mark_delivered("read111")
        assert len(reads) == 1, f"the acknowledgement did not take the carry read: {reads}"
        assert (
            json.loads((agent_root / "read111" / "tombstone.json").read_text())["outcome"]
            == "failed"
        )


class TestADetachedSettleBatchSurvivesCancellation:
    """A batch taken off its owner must not be lost when the waiter is cancelled.

    The settle detaches its held deliveries irrevocably before suspending, so
    awaiting the write once per delivery would make every one after the first a
    cancellation point -- and ``CancelledError`` is not an ``Exception``, so the
    usual per-delivery guard does not catch it. Each one not reached keeps no
    ``delivered`` tombstone, which is the marker restart reconciliation uses to
    EXCLUDE a folder, so it replays as a duplicate completion. Handing the batch to
    one worker operation is what makes the loss impossible rather than unlikely.

    The batch carries the DELIVERIES, not bare ids, because the tombstone records
    the run's terminal usage: settling by id would write a delivered tombstone with
    no usage, so a held wave member would show none while its siblings carry theirs.
    """

    def delivery(self, agent_id: str, elapsed: float = 1.5, credits: float = 0.25):
        """The real frozen dataclass, so these pin the shipped contract."""
        from kiro_crew.subagent import SubagentDelivery

        return SubagentDelivery(agent_id, elapsed, credits)

    def test_every_id_in_the_batch_is_settled(self, agent_root):
        ids = ["batch111", "batch222", "batch333"]
        for agent_id in ids:
            write_record(agent_root, agent_id, tombstone_cause=None)
        settle_delivered_batch(tuple(self.delivery(agent_id) for agent_id in ids))
        for agent_id in ids:
            data = json.loads((agent_root / agent_id / "tombstone.json").read_text())
            assert data["cause"] == "delivered", f"{agent_id} was not settled"

    def test_the_batch_carries_each_runs_terminal_usage(self, agent_root):
        """Offloading the write must not cost the tombstone its elapsed and credits."""
        write_record(agent_root, "usage11", tombstone_cause=None)
        settle_delivered_batch((self.delivery("usage11", elapsed=12.5, credits=3.25),))
        data = json.loads((agent_root / "usage11" / "tombstone.json").read_text())
        assert data["cause"] == "delivered"
        assert data["elapsed"] == 12.5, "the held member's runtime did not reach its tombstone"
        assert data["credits"] == 3.25, "the held member's credits did not reach its tombstone"

    def test_one_unwritable_folder_does_not_strand_the_rest(self, agent_root):
        """The batch keeps the per-run isolation the single write promised."""
        write_record(agent_root, "batch444", tombstone_cause=None)
        write_record(agent_root, "batch555", tombstone_cause=None)
        settle_delivered_batch(
            (
                self.delivery("batch444"),
                self.delivery("no-such-run-at-all"),
                self.delivery("batch555"),
            )
        )
        for agent_id in ("batch444", "batch555"):
            data = json.loads((agent_root / agent_id / "tombstone.json").read_text())
            assert data["cause"] == "delivered", f"{agent_id} was stranded"

    def test_a_cancelled_waiter_still_leaves_the_whole_batch_written(self, agent_root):
        """The offload is one unit, so cancelling the await cannot split it.

        Cancelling a ``to_thread`` await abandons the waiting, not the thread, so
        the batch completes. Driven through the real settle so the property holds
        of the call site and not merely of the helper.
        """
        ids = ["cncl111", "cncl222", "cncl333"]
        for agent_id in ids:
            write_record(agent_root, agent_id, tombstone_cause=None)
        deliveries = tuple(self.delivery(agent_id) for agent_id in ids)

        started = asyncio.Event()

        def slow_batch(batch):
            started.set()
            settle_delivered_batch(batch)

        async def drive() -> None:
            task = asyncio.create_task(asyncio.to_thread(slow_batch, deliveries))
            # flake-ok: the worker thread sets this event as its first statement, so the wait returns; pre-existing, moved verbatim
            await started.wait()
            task.cancel()
            # The waiter is gone; the worker is not, so wait for it to finish.
            await async_wait_until(
                lambda: all((agent_root / a / "tombstone.json").exists() for a in ids),
                off_loop=True,
                describe=lambda: f"tombstones written: {[a for a in ids if (agent_root / a / 'tombstone.json').exists()]}",
            )

        asyncio.run(drive())
        for agent_id in ids:
            data = json.loads((agent_root / agent_id / "tombstone.json").read_text())
            assert data["cause"] == "delivered", (
                f"{agent_id} lost its delivered tombstone when the waiter was cancelled, "
                "so restart reconciliation would replay it as a duplicate completion"
            )

    def test_the_settle_hands_the_batch_over_whole(self):
        """Structural: one offload of the batch, never a per-id await.

        A per-id await is the defect itself, and it reads as innocuous -- a loop
        with a guard inside it -- so the shape is asserted rather than left to a
        reader.
        """
        import ast

        root = pathlib.Path(__file__).resolve().parents[1]
        tree = ast.parse(
            (root / "src/kiro_crew/subagent_manager/waves.py").read_text(encoding="utf-8")
        )
        settle = next(
            fn
            for fn in ast.walk(tree)
            if isinstance(fn, ast.AsyncFunctionDef) and fn.name == "_settle_digest_holds_impl"
        )
        offloads = [
            node
            for node in ast.walk(settle)
            if isinstance(node, ast.Call)
            and getattr(node.func, "attr", "") == "to_thread"
            and node.args
        ]
        assert len(offloads) == 1, f"expected one offload, found {len(offloads)}"
        (offload,) = offloads
        assert getattr(offload.args[0], "id", "") == "settle_delivered_batch", (
            "the settle offloads something other than the batch helper, so a"
            " cancellation could abandon part of a detached batch"
        )
        # The batch must write through the name the MANAGER resolved, not the one
        # the helper's own module holds: the manager's modules take their names
        # from the facade, which is the single point anything substituting the
        # write replaces, and a batch reaching past it would be the one delivery
        # path that ignored the substitution.
        writers = [kw for kw in offload.keywords if kw.arg == "writer"]
        assert writers, "the settle does not pass its own writer into the batch"
        assert getattr(writers[0].value, "id", "") == "mark_delivered"
        # Control: the enclosing method must still be reachable and awaited, or an
        # empty offload list would read as a pass.
        assert any(isinstance(n, ast.Await) for n in ast.walk(settle))
