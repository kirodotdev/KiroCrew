"""The subagent panel's durable rebuild source.

The panel's live source is gateway memory, so a replacement gateway process has
nothing to replay for the runs it never tracked. These tests pin the persisted
fallback that answers for them, and -- just as importantly -- pin what it must
refuse to invent: a slot-tracked native card, a run whose memory mode is not
persistent, a second copy of a run the live manager still holds, and a failure
for a run the tombstone records as a user stop.
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import time
from types import SimpleNamespace

import pytest

from kiro_crew import subagent_persistence
from kiro_crew.dashboard import ws_event_scope as scope_module
from kiro_crew.dashboard.chat_utils import subagent_event_slot
from kiro_crew.dashboard.handlers import messaging
from kiro_crew.dashboard.state import (
    NATIVE_SUBAGENT_TERMINAL_TTL_SECS,
    PERSISTED_SUBAGENT_REPLAY_KEEP,
    PERSISTED_SUBAGENT_REPLAY_MAX_AGE_SECS,
)
from kiro_crew.dashboard.ws import build_persisted_subagent_frame
from kiro_crew.dashboard.ws_event_scope import (
    persisted_precap_denial_reason,
    persisted_precap_readings,
    persisted_replay_denial_reason,
    persisted_snapshot_denial_reason,
    slot_owner_snapshot,
    visible_subagent_slot_keys,
)
from kiro_crew.subagent_persistence import (
    _PANEL_AGENT_CAP,
    _PANEL_APP_CAP,
    _PANEL_CANDIDATE_MULTIPLE,
    _PANEL_ERROR_CAP,
    _PANEL_ID_CAP,
    _PANEL_PARENT_SESSION_CAP,
    _PANEL_RESULT_CAP,
    _PANEL_TASK_CAP,
    _PANEL_TRUNC_MARKER,
    PanelRecords,
    classify_persisted_ending,
    delete_agent_folder,
    dismissed_panel_ids,
    mark_delivered,
    prune_orphan_panel_dismissals,
    read_panel_records,
    read_state,
    record_panel_dismissal,
    settle_delivered_batch,
    write_tombstone,
)

DAY = 86_400.0


def messaging_crew_log_emit():
    """The emitter module the dismiss route imports inside its own helper.

    Patched there rather than on ``messaging``: the helper does a local import, so
    a name set on ``messaging`` is never read.
    """
    from kiro_crew.crew_log import emit

    return emit


CORRUPT = "__corrupt__"
TRUNC = _PANEL_TRUNC_MARKER


@pytest.fixture()
def agent_root(tmp_path, monkeypatch):
    """Point persistence at a registry below this test's temp directory."""
    root = tmp_path / "subagents"
    root.mkdir()
    monkeypatch.setattr("kiro_crew.subagent_persistence._SUBAGENTS_DIR", root)
    return root


def write_record(
    root,
    agent_id: str,
    *,
    task: str = "summarise the changelog",
    agent: str = "kirocrew",
    parent_session: str = "dashboard:chat-1",
    started: float | None = None,
    memory_mode: str = "persistent",
    app: str = "",
    tombstone_cause: str | None = "delivered",
    outcome: str | None = None,
    detail: str | None = None,
    died: float | None = None,
    result: str | None = None,
    mtime: float | None = None,
) -> None:
    """Write one run folder the way a real run leaves it behind.

    ``tombstone_cause=None`` writes no tombstone; ``CORRUPT`` writes one that is
    present and unparseable. Those are different states and the reader tells them
    apart, so the helper has to be able to produce both.
    """
    moment = time.time()
    folder = root / agent_id
    folder.mkdir(parents=True, exist_ok=True)
    state = {
        "id": agent_id,
        "task": task,
        "agent": agent,
        "parent_session": parent_session,
        "started": moment - 120 if started is None else started,
        "status": "running",
        "turns": 2,
        "memory_mode": memory_mode,
        "execution_context": {"memory_mode": memory_mode},
        "app": app,
        "updated_at": moment - 60,
    }
    (folder / "state.json").write_text(json.dumps(state), encoding="utf-8")
    if result is not None:
        (folder / "result.txt").write_text(result, encoding="utf-8")
    if tombstone_cause == CORRUPT:
        (folder / "tombstone.json").write_text("{not json", encoding="utf-8")
    elif tombstone_cause is not None:
        tombstone: dict = {
            "id": agent_id,
            "cause": tombstone_cause,
            "recovery_action": "delivered",
            "started": state["started"],
            "died": moment - 60 if died is None else died,
        }
        if outcome is not None:
            tombstone["outcome"] = outcome
        if detail is not None:
            tombstone["detail"] = detail
        (folder / "tombstone.json").write_text(json.dumps(tombstone), encoding="utf-8")
    if mtime is not None:
        os.utime(folder, (mtime, mtime))


def ids(records) -> list[str]:
    return [record["id"] for record in records]


def panel(**kwargs):
    """The records list alone, for cases that do not assert on the overflow."""
    return read_panel_records(**kwargs).records


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

    def test_an_empty_slot_key_denies(self):
        state = self.state({"chat-1": self.slot("")})
        assert persisted_replay_denial_reason(state, "", {"app": ""}) == "slot_missing"

    def test_an_empty_slot_key_denies_even_if_a_slot_is_registered_under_it(self):
        """The empty key is refused on its own, not merely by finding no slot.

        A record whose parent resolves to nothing carries an empty slot, and a
        lookup would otherwise hand it whatever sits under that key.
        """
        state = self.state({"": self.slot("")})
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

    def test_overflow_counts_exactly_what_the_cap_cut(self, agent_root):
        self.seed(agent_root, 7)
        result = read_panel_records(keep=4, max_age_secs=DAY)
        assert len(result.records) == 4
        assert result.overflow == 3

    def test_one_past_the_cap_is_reported(self, agent_root):
        """The case the finding named: 51 eligible, 50 shown."""
        self.seed(agent_root, 6)
        result = read_panel_records(keep=5, max_age_secs=DAY)
        assert len(result.records) == 5
        assert result.overflow == 1

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
        # Five offloads live in _impl methods: the delivery path, the two
        # gateway_restart writes, the digest-hold settlement, and the drained
        # queue's, which predates this change and is the pattern the others follow.
        assert checked == 5, f"expected 5 _impl offloads, found {checked}"

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


class TestAGrantIsAuditedNotOnlyARefusal:
    """Recording only refusals shows what was blocked and never what was released.

    Every refusal on this route leaves a SEL record, so a stream that omits the
    admissions cannot answer who was handed a persisted run's text -- which is the
    question an operator reviewing it has.
    """

    def decisions(self, monkeypatch) -> list[tuple[str, str, str]]:
        calls: list[tuple[str, str, str]] = []
        monkeypatch.setattr(
            messaging, "_audit_deny", lambda app, event, reason: calls.append((app, event, reason))
        )
        monkeypatch.setattr(
            messaging, "_audit_allow", lambda app, event: calls.append((app, event, "granted"))
        )
        return calls

    def request(self, state, caller: str, app: str = "", *, internal: bool = True):
        values: dict[str, object] = {"app": app, "internal_auth": internal}
        return SimpleNamespace(
            app={"state": state},
            headers={"X-Session-Key": caller},
            query={},
            get=lambda key, default=None: values.get(key, default),
        )

    def state(self, slot_app: str):
        slots = {"chat-1": SimpleNamespace(_app=slot_app)}
        return SimpleNamespace(
            subagents=SimpleNamespace(all_agents=[]), _slots=slots, get_slot=slots.get
        )

    def listing(
        self, monkeypatch, *, slot_app: str, caller: str, scope: object, internal: bool = True
    ):
        calls = self.decisions(monkeypatch)

        async def fake_scope(request, op):
            return scope, None

        monkeypatch.setattr(messaging, "internal_memory_scope", fake_scope)
        payload = asyncio.get_event_loop().run_until_complete(
            messaging.api_spawn_list(self.request(self.state(slot_app), caller, internal=internal))
        )
        return calls, json.loads(payload.text or "{}")

    def test_an_admitted_record_leaves_a_grant_record(self, agent_root, monkeypatch):
        write_record(agent_root, "grant11", app="", parent_session="dashboard:chat-1")
        calls, body = self.listing(
            monkeypatch, slot_app="", caller="dashboard:chat-1", scope=object()
        )
        assert [entry["id"] for entry in body["agents"]] == ["grant11"]
        assert [reason for _app, _event, reason in calls] == ["granted"]

    def test_a_refused_record_leaves_no_grant_record(self, agent_root, monkeypatch):
        """The two are exclusive, so a refusal must not also read as a release."""
        write_record(agent_root, "grant22", app="appA", parent_session="dashboard:chat-1")
        calls, body = self.listing(
            monkeypatch, slot_app="appB", caller="dashboard:chat-1", scope=object()
        )
        assert body["agents"] == []
        assert [reason for _app, _event, reason in calls] == ["persisted_owner_mismatch"]


class TestVisibilityBoundsTheCapNotOnlyOwnership:
    """Ownership and visibility are independent, and both belong before the cap.

    A record can name a slot the caller owns while carrying an event the caller
    never declared, and it can be visible under a declaration while belonging to
    another app. Sizing the cap on ownership alone lets a burst of records this
    socket may not see spend the slots its own visible runs need, and the cut count
    is then computed over records that were never its to receive.
    """

    def slot(self, app: str, origin: str = ""):
        return SimpleNamespace(_app=app, _origin=origin)

    def state(self, slots: dict):
        return SimpleNamespace(_slots=slots, get_slot=slots.get)

    def unrevoked(self, monkeypatch):
        """Pin the revocation answer, which is otherwise cache-warmth dependent.

        ``app_events_revoked`` reports NOT revoked on a cold cache and schedules a
        refresh, so an own-slot answer read twice in one process can legitimately
        differ. A test that does not pin it is measuring cache warmth.
        """
        monkeypatch.setattr(scope_module, "app_events_revoked", lambda _app: False)

    def test_the_dashboard_user_sees_every_live_slot(self, monkeypatch):
        self.unrevoked(monkeypatch)
        state = self.state({"chat-1": self.slot(""), "chat-2": self.slot("appA")})
        keys = visible_subagent_slot_keys(state, "", frozenset(), dashboard_user=True)
        assert keys == {"chat-1", "chat-2"}

    def test_an_app_sees_its_own_slot(self, monkeypatch):
        self.unrevoked(monkeypatch)
        state = self.state({"chat-1": self.slot("appA"), "chat-2": self.slot("appB")})
        keys = visible_subagent_slot_keys(state, "appA", frozenset(), dashboard_user=False)
        assert keys == {"chat-1"}

    def test_a_revoked_app_loses_even_its_own_slot(self, monkeypatch):
        """The own-slot branch is the one revocation has to reach."""
        monkeypatch.setattr(scope_module, "app_events_revoked", lambda _app: True)
        state = self.state({"chat-1": self.slot("appA")})
        keys = visible_subagent_slot_keys(state, "appA", frozenset(), dashboard_user=False)
        assert keys == set()

    def test_a_declaration_widens_it(self, monkeypatch):
        self.unrevoked(monkeypatch)
        state = self.state({"chat-1": self.slot("appA"), "chat-2": self.slot("appB")})
        keys = visible_subagent_slot_keys(
            state, "appA", frozenset({"subagent:all"}), dashboard_user=False
        )
        assert keys == {"chat-1", "chat-2"}

    def test_a_caller_that_is_neither_sees_nothing(self, monkeypatch):
        """Fail closed: no app claim and not the dashboard user admits no slot."""
        self.unrevoked(monkeypatch)
        state = self.state({"chat-1": self.slot("appA")})
        keys = visible_subagent_slot_keys(state, "", frozenset(), dashboard_user=False)
        assert keys == set()


class TestThePreCapDecisionIsBothBounds:
    """One function, two independent halves, so neither can be forgotten.

    A record can name a slot this client owns while being invisible to it, and it
    can be visible while belonging to another app. Sizing the cap on either half
    alone lets the other's records spend slots the client's own runs need.
    """

    def record(self, app: str, parent: str = "dashboard:chat-1") -> dict:
        return {"id": "pre111", "app": app, "parent_session": parent}

    def key(self, record: dict) -> str:
        return subagent_event_slot(str(record["parent_session"]))

    def test_both_bounds_satisfied_admits(self):
        rec = self.record("appA")
        assert (
            persisted_precap_denial_reason({"chat-1": "appA"}, {"chat-1"}, self.key(rec), rec) == ""
        )

    def test_invisible_is_refused_even_when_the_owner_matches(self):
        rec = self.record("appA")
        assert (
            persisted_precap_denial_reason({"chat-1": "appA"}, set(), self.key(rec), rec)
            == "persisted_not_visible"
        )

    def test_a_foreign_owner_is_refused_even_when_visible(self):
        rec = self.record("appB")
        assert (
            persisted_precap_denial_reason({"chat-1": "appA"}, {"chat-1"}, self.key(rec), rec)
            == "persisted_owner_mismatch"
        )

    def test_visibility_is_reported_first_when_both_fail(self):
        """A declaration gap must not enter the stream as a cross-app breach."""
        rec = self.record("appB")
        assert (
            persisted_precap_denial_reason({"chat-1": "appA"}, set(), self.key(rec), rec)
            == "persisted_not_visible"
        )

    def test_a_slot_no_snapshot_holds_is_slot_missing_when_visible(self):
        rec = self.record("appA", parent="dashboard:chat-9")
        assert (
            persisted_precap_denial_reason({"chat-1": "appA"}, {"chat-9"}, self.key(rec), rec)
            == "slot_missing"
        )


class TestThePreCapReadingsAreBothTaken:
    """The pairing is the tested unit, not two inline calls at an unreachable site.

    A wiring that passed the owner map where the visible set belongs would type-check
    and pass every test of the decision function, because both are keyed on slot
    keys. Only asserting what each reading CARRIES separates them.
    """

    def state(self, slots: dict):
        return SimpleNamespace(_slots=slots, get_slot=slots.get)

    def test_the_owner_map_carries_apps_and_the_visible_set_carries_keys(self, monkeypatch):
        monkeypatch.setattr(scope_module, "app_events_revoked", lambda _app: False)
        slots = {
            "chat-1": SimpleNamespace(_app="appA", _origin=""),
            "chat-2": SimpleNamespace(_app="appB", _origin=""),
        }
        owners, visible = persisted_precap_readings(
            self.state(slots), "appA", frozenset(), dashboard_user=False
        )
        # The owner map spans every live slot and its VALUES are apps.
        assert owners == {"chat-1": "appA", "chat-2": "appB"}
        # The visible set is narrower than the owner map here, which is what makes
        # the two distinguishable: passing one for the other changes the answer.
        assert visible == {"chat-1"}
        assert visible != set(owners)

    def test_the_dashboard_user_reads_every_slot_as_visible(self, monkeypatch):
        monkeypatch.setattr(scope_module, "app_events_revoked", lambda _app: False)
        slots = {"chat-1": SimpleNamespace(_app="appA", _origin="")}
        owners, visible = persisted_precap_readings(
            self.state(slots), "", frozenset(), dashboard_user=True
        )
        assert visible == set(owners) == {"chat-1"}


class TestPersistedListingDenialsAreAudited:
    """An authorization refusal that emits nothing leaves no artifact to review.

    The repo's own scope gate audits every decision through ``_audit_deny``, and
    these two refusals are decisions of the same kind, so they go through it too.
    Each carries its own reason: a truncation must not borrow an ownership
    refusal's reason, and an ownership refusal must not read as a scope one.
    """

    def audited(self, monkeypatch) -> list[tuple[str, str, str]]:
        calls: list[tuple[str, str, str]] = []
        monkeypatch.setattr(
            messaging,
            "_audit_deny",
            lambda app, event, reason: calls.append((app, event, reason)),
        )
        return calls

    def warnings(self, monkeypatch) -> list[str]:
        """Collect the handler's own WARNING lines.

        The package logger does not propagate to root, so caplog sees nothing;
        the call itself is what this pins anyway.
        """
        lines: list[str] = []
        monkeypatch.setattr(
            messaging.logger,
            "warning",
            lambda msg, *a, **k: lines.append(str(msg) % a if a else str(msg)),
        )
        return lines

    def request(self, state, caller: str, app: str = "", *, internal: bool = True):
        headers = {"X-Session-Key": caller}
        values: dict[str, object] = {"internal_auth": internal}
        if app:
            values["app"] = app
        return SimpleNamespace(
            app={"state": state},
            headers=headers,
            query={},
            get=lambda key, default=None: values.get(key, default),
        )

    def state(self, slot_app: str):
        manager = SimpleNamespace(all_agents=[])
        slots = {"chat-1": SimpleNamespace(_app=slot_app)}
        return SimpleNamespace(subagents=manager, _slots=slots, get_slot=slots.get)

    def run_listing(
        self,
        monkeypatch,
        agent_root,
        *,
        slot_app: str,
        caller: str,
        scope: object,
        app: str = "",
        internal: bool = True,
    ):
        """Drive the listing end to end over one persisted record."""
        calls = self.audited(monkeypatch)

        async def fake_scope(request, op):
            return scope, None

        monkeypatch.setattr(messaging, "internal_memory_scope", fake_scope)
        state = self.state(slot_app)
        payload = asyncio.get_event_loop().run_until_complete(
            messaging.api_spawn_list(self.request(state, caller, app, internal=internal))
        )
        return calls, json.loads(payload.text or "{}")

    def test_a_stale_snapshot_is_overruled_by_the_loop_recheck(self, agent_root, monkeypatch):
        """The REST listing decides ownership on the loop, not in the worker thread.

        The snapshot is taken before the scan and can be out of date by the time
        the records come back, because slot keys are caller-supplied and another
        app can reclaim one mid-scan. Here the snapshot still says the caller owns
        the slot while live state says another app does, which is the shape of that
        race; the record must be withheld and audited on the live answer.
        """
        write_record(agent_root, "stale11", app="appA", parent_session="dashboard:chat-1")
        monkeypatch.setattr(messaging, "slot_owner_snapshot", lambda _state: {"chat-1": "appA"})
        calls, body = self.run_listing(
            monkeypatch,
            agent_root,
            slot_app="appB",
            caller="dashboard:chat-1",
            scope=object(),
        )
        assert body["agents"] == []
        assert [reason for _app, _event, reason in calls] == ["persisted_owner_mismatch"]

    def test_the_audit_identity_is_the_app_never_the_session_key(self, agent_root, monkeypatch):
        """The dedup registry behind the audit is keyed on it and never evicted.

        A per-run session id would leave one permanent entry per subagent run, which
        is the unbounded growth the retention rule forbids.
        """
        write_record(agent_root, "aud111", app="appA", parent_session="dashboard:chat-1")
        calls, _body = self.run_listing(
            monkeypatch,
            agent_root,
            slot_app="appB",
            caller="subagent:ephemeral-run-id",
            scope=object(),
            app="owning-app",
        )
        assert [app for app, _event, _reason in calls] == ["owning-app"]
        assert "subagent:ephemeral-run-id" not in [app for app, _e, _r in calls]

    def test_an_absent_app_audits_under_a_fixed_literal_not_the_caller(
        self, agent_root, monkeypatch
    ):
        write_record(agent_root, "aud222", app="appA", parent_session="dashboard:chat-1")
        calls, _body = self.run_listing(
            monkeypatch,
            agent_root,
            slot_app="appB",
            caller="subagent:another-run-id",
            scope=object(),
        )
        assert [app for app, _event, _reason in calls] == ["<owner>"]

    def test_an_owner_mismatch_is_audited_and_the_record_withheld(self, agent_root, monkeypatch):
        write_record(agent_root, "own111", app="appA", parent_session="dashboard:chat-1")
        calls, body = self.run_listing(
            monkeypatch,
            agent_root,
            slot_app="appB",
            caller="dashboard:chat-1",
            scope=object(),
        )
        assert body["agents"] == []
        assert [reason for _app, _event, reason in calls] == ["persisted_owner_mismatch"]

    def test_a_scope_mismatch_carries_its_own_reason(self, agent_root, monkeypatch):
        write_record(agent_root, "scp111", parent_session="dashboard:chat-1")
        calls, body = self.run_listing(
            monkeypatch,
            agent_root,
            slot_app="",
            caller="dashboard:other",
            scope=object(),
        )
        assert body["agents"] == []
        assert [reason for _app, _event, reason in calls] == ["persisted_scope_mismatch"]

    def test_an_internal_caller_on_the_default_store_is_still_bound(self, agent_root, monkeypatch):
        """The empty store is the COMMON internal case, not an exemption.

        ``internal_memory_scope`` answers ``scope.store or None``, so a verified
        internal caller whose execution record names the default store resolves
        the same ``None`` the dashboard owner does. Gating the session bound on
        that value therefore lifted it for nearly every attested caller, handing a
        subagent another session's persisted ``task``, ``parent`` and full
        ``result`` text -- which the live half of the same response withholds.
        """
        write_record(agent_root, "dflt11", parent_session="dashboard:chat-1")
        calls, body = self.run_listing(
            monkeypatch,
            agent_root,
            slot_app="",
            caller="subagent:someone-elses-run",
            scope=None,
            internal=True,
        )
        assert body["agents"] == []
        assert [reason for _app, _event, reason in calls] == ["persisted_scope_mismatch"]

    def test_the_dashboard_owner_still_reads_every_record(self, agent_root, monkeypatch):
        """Control for the bound above: it must narrow agents, not the operator.

        The owner arrives with no ``internal_auth`` and no store either, so if the
        bound keyed on the store it would be indistinguishable from the internal
        caller above -- this is the case that proves the new predicate separates
        them.
        """
        write_record(agent_root, "ownr11", parent_session="dashboard:chat-1")
        calls, body = self.run_listing(
            monkeypatch,
            agent_root,
            slot_app="",
            caller="dashboard:some-other-tab",
            scope=None,
            internal=False,
        )
        assert [entry["id"] for entry in body["agents"]] == ["ownr11"]
        assert [reason for _app, _event, reason in calls] == []

    def test_an_admitted_record_is_listed_and_audits_nothing(self, agent_root, monkeypatch):
        write_record(agent_root, "ok1111", parent_session="dashboard:chat-1")
        calls, body = self.run_listing(
            monkeypatch,
            agent_root,
            slot_app="",
            caller="dashboard:chat-1",
            scope=object(),
        )
        assert [entry["id"] for entry in body["agents"]] == ["ok1111"]
        assert calls == []

    def test_truncation_is_reported_and_audited_under_its_own_reason(
        self, agent_root, monkeypatch, caplog
    ):
        moment = time.time()
        for index in range(3):
            write_record(
                agent_root,
                f"tr{index:04d}",
                parent_session="dashboard:chat-1",
                mtime=moment - index,
            )
        monkeypatch.setattr(messaging, "PERSISTED_SUBAGENT_REPLAY_KEEP", 1)
        warned = self.warnings(monkeypatch)
        calls, body = self.run_listing(
            monkeypatch,
            agent_root,
            slot_app="",
            caller="dashboard:chat-1",
            scope=None,
        )
        assert len(body["agents"]) == 1
        # Said out loud to the OPERATOR, once. The client gets no count, because
        # no panel acts on one; silence about a cut tail is what is forbidden.
        said = [line for line in warned if "truncated" in line]
        assert len(said) == 1
        # The COUNT is the payload of the report; a line that says only that
        # something was cut is the same silence in a longer sentence.
        assert "2 eligible" in said[0]
        assert "persisted_overflow" not in body
        # And the report is the WARNING alone. A truncation refuses nobody, so it
        # is not a permission decision: filing it into the deny stream would put
        # a routine cap event beside the ownership refusals an operator reads
        # that stream to find.
        assert calls == []

    def test_a_saturated_window_is_reported_even_at_a_zero_count(self, agent_root, monkeypatch):
        """The case the count itself cannot see.

        Every candidate is rejected, so the row cap never fires and its count
        stays zero -- while admissible older folders were never inspected.
        Gating the report on the count alone calls that "nothing was cut".
        """
        moment = time.time()
        for index in range(12):
            write_record(
                agent_root,
                f"sw{index:04d}",
                parent_session="dashboard:other",
                mtime=moment - index,
            )
        monkeypatch.setattr(messaging, "PERSISTED_SUBAGENT_REPLAY_KEEP", 2)
        warned = self.warnings(monkeypatch)
        calls, body = self.run_listing(
            monkeypatch, agent_root, slot_app="", caller="dashboard:chat-1", scope=object()
        )
        assert body["agents"] == []
        said = [line for line in warned if "truncated" in line]
        assert len(said) == 1
        assert "scan window saturated" in said[0]
        # The deny stream carries one row per record the scan actually INSPECTED
        # and no truncation row: every reason in it is a permission decision. The
        # count is 8 rather than the 12 written, because the candidate window is
        # itself bounded -- which is exactly why a saturated window has to report
        # even at a zero cut count: the four it never opened are invisible to it.
        assert [reason for _a, _e, reason in calls] == ["persisted_scope_mismatch"] * 8

    def test_no_report_at_all_when_nothing_was_cut(self, agent_root, monkeypatch):
        write_record(agent_root, "fit111", parent_session="dashboard:chat-1")
        warned = self.warnings(monkeypatch)
        calls, body = self.run_listing(
            monkeypatch, agent_root, slot_app="", caller="dashboard:chat-1", scope=None
        )
        assert "persisted_overflow" not in body
        assert calls == []
        assert warned == []


class TestRedactionRunsBeforeClamping:
    """Clamping first cuts a credential's tail off and defeats the match.

    A value cut at the cap loses the suffix its pattern needs, so the consumers'
    own redaction cannot match the fragment that survives into the record. The
    native card path already requires this order; the persisted path follows it.
    """

    #: A value the redactors do match, so the ordering is testable at all.
    SECRET = "AKIAIOSFODNN7EXAMPLE"

    def straddling_task(self, cap: int) -> str:
        """A task whose credential starts before the cap and ends after it."""
        return "a" * (cap - 12) + self.SECRET + "b" * 50

    def test_a_credential_straddling_the_task_cap_is_redacted(self, agent_root):
        write_record(agent_root, "sec111", task=self.straddling_task(_PANEL_TASK_CAP))
        (record,) = panel(keep=10, max_age_secs=DAY)
        assert self.SECRET not in record["task"]
        # A clamp-first ordering would leave this recognizable head behind.
        assert "AKIA" not in record["task"]

    def test_a_credential_straddling_the_result_cap_is_redacted(self, agent_root):
        write_record(agent_root, "sec222", result=self.straddling_task(_PANEL_RESULT_CAP))
        (record,) = panel(keep=10, max_age_secs=DAY, include_result=True)
        assert self.SECRET not in record["result"]
        assert "AKIA" not in record["result"]

    def test_a_credential_in_the_error_detail_is_redacted(self, agent_root):
        write_record(
            agent_root,
            "sec333",
            tombstone_cause="error",
            outcome="failed",
            detail=f"provider rejected {self.SECRET}",
        )
        (record,) = panel(keep=10, max_age_secs=DAY)
        assert self.SECRET not in record["error"]

    def test_a_credential_in_the_agent_name_is_redacted(self, agent_root):
        write_record(agent_root, "sec444", agent=self.SECRET)
        (record,) = panel(keep=10, max_age_secs=DAY)
        assert self.SECRET not in record["agent"]

    def test_ordinary_text_survives_redaction_unchanged(self, agent_root):
        write_record(agent_root, "sec555", task="summarise the changelog")
        (record,) = panel(keep=10, max_age_secs=DAY)
        assert record["task"] == "summarise the changelog"

    def test_the_result_read_stays_bounded_despite_the_margin(self, agent_root):
        """The margin is finite: a huge file is still not read whole."""
        write_record(agent_root, "sec666", result="z" * (_PANEL_RESULT_CAP * 20))
        (record,) = panel(keep=10, max_age_secs=DAY, include_result=True)
        assert len(record["result"]) <= _PANEL_RESULT_CAP + len(TRUNC)


class TestSlotAbsentIsNotAnOwnershipBreach:
    """A lazily hydrated slot is ordinary, and must not be filed as a breach.

    Slots hydrate from history, so a reconnect can arrive before the slot exists.
    The live gate calls that ``slot_missing``; reporting it as an ownership
    mismatch would file every cold-start reconnect as a security event and dilute
    the stream a real cross-owner refusal has to stand out in.
    """

    def test_a_missing_slot_reports_slot_missing(self):
        state = SimpleNamespace(_slots={}, get_slot=lambda name: None)
        assert persisted_replay_denial_reason(state, "chat-1", {"app": ""}) == "slot_missing"

    def test_an_owner_difference_reports_an_ownership_mismatch(self):
        slots = {"chat-1": SimpleNamespace(_app="appB")}
        state = SimpleNamespace(_slots=slots, get_slot=slots.get)
        reason = persisted_replay_denial_reason(state, "chat-1", {"app": "appA"})
        assert reason == "persisted_owner_mismatch"

    def test_the_two_reasons_are_distinct(self):
        """The whole point: an operator can tell them apart in the audit stream."""
        empty = SimpleNamespace(_slots={}, get_slot=lambda name: None)
        slots = {"chat-1": SimpleNamespace(_app="appB")}
        owned = SimpleNamespace(_slots=slots, get_slot=slots.get)
        assert persisted_replay_denial_reason(empty, "chat-1", {"app": "appA"}) != (
            persisted_replay_denial_reason(owned, "chat-1", {"app": "appA"})
        )


class TestTheCallersFilterRunsBeforeTheCap:
    """Filtering after the cap lets a foreign record spend the caller's budget.

    Two harms, not one: a record the caller may not see occupies a slot its own
    runs need, so the caller's newest run vanishes while a stranger's is counted;
    and the overflow figure then describes a population that is not the caller's,
    which tells them how many foreign runs exist.
    """

    def mine(self, record: dict) -> bool:
        return record["app"] == "mine"

    def test_a_foreign_record_does_not_occupy_a_cap_slot(self, agent_root):
        moment = time.time()
        write_record(agent_root, "theirs1", app="theirs", mtime=moment - 1)
        write_record(agent_root, "theirs2", app="theirs", mtime=moment - 2)
        write_record(agent_root, "mine001", app="mine", mtime=moment - 3)
        result = read_panel_records(keep=2, max_age_secs=DAY, admit=self.mine)
        # Without the filter running first, the two foreign records would fill
        # the cap and this caller would see nothing of its own.
        assert ids(result.records) == ["mine001"]

    def test_a_foreign_record_is_not_counted_as_overflow(self, agent_root):
        moment = time.time()
        write_record(agent_root, "mine001", app="mine", mtime=moment - 1)
        for index in range(4):
            write_record(agent_root, f"their{index}", app="theirs", mtime=moment - 2 - index)
        result = read_panel_records(keep=1, max_age_secs=DAY, admit=self.mine)
        assert ids(result.records) == ["mine001"]
        assert result.overflow == 0

    def test_overflow_counts_only_the_callers_own_withheld_runs(self, agent_root):
        moment = time.time()
        for index in range(3):
            write_record(agent_root, f"mine{index:03d}", app="mine", mtime=moment - index)
        write_record(agent_root, "theirs1", app="theirs", mtime=moment - 10)
        result = read_panel_records(keep=1, max_age_secs=DAY, admit=self.mine)
        assert len(result.records) == 1
        assert result.overflow == 2

    def test_no_admit_filter_admits_everything(self, agent_root):
        write_record(agent_root, "any001", app="theirs")
        result = read_panel_records(keep=10, max_age_secs=DAY)
        assert ids(result.records) == ["any001"]

    def test_the_result_read_is_still_skipped_past_the_cap(self, agent_root, monkeypatch):
        from kiro_crew import subagent_persistence

        moment = time.time()
        for index in range(3):
            write_record(
                agent_root, f"mine{index:03d}", app="mine", result="payload", mtime=moment - index
            )
        reads: list[str] = []
        real = subagent_persistence._panel_result_text
        monkeypatch.setattr(
            subagent_persistence,
            "_panel_result_text",
            lambda d: (reads.append(d.name), real(d))[1],
        )
        result = read_panel_records(keep=1, max_age_secs=DAY, include_result=True, admit=self.mine)
        assert result.overflow == 2
        assert reads == ["mine000"]


class TestASaturatedScanWindowIsReportedOnItsOwn:
    """The candidate window is a SECOND bound, and it closes before admission.

    It keeps the newest folders by mtime, then validity and the caller's filter
    reject some of them. So a window filled entirely by records that are then
    rejected leaves admissible older folders uninspected while the row-cap count
    stays zero. Reporting only on a nonzero count calls that "nothing was cut".
    """

    def write_many(self, agent_root, count: int, *, app: str = "mine") -> None:
        moment = time.time()
        for index in range(count):
            write_record(agent_root, f"sat{index:04d}", app=app, mtime=moment - index)

    def test_saturation_is_flagged_even_when_the_row_count_is_zero(self, agent_root):
        # Every candidate is rejected, so overflow cannot see what was left out.
        self.write_many(agent_root, 12, app="theirs")
        result = read_panel_records(
            keep=2, max_age_secs=DAY, admit=lambda record: record["app"] == "mine"
        )
        assert result.records == []
        assert result.overflow == 0
        assert result.overflow_is_lower_bound is True

    def test_an_unsaturated_window_is_not_flagged(self, agent_root):
        self.write_many(agent_root, 2)
        result = read_panel_records(keep=10, max_age_secs=DAY)
        assert len(result.records) == 2
        assert result.overflow == 0
        assert result.overflow_is_lower_bound is False

    def test_the_flag_survives_alongside_a_real_count(self, agent_root):
        self.write_many(agent_root, 12)
        result = read_panel_records(keep=2, max_age_secs=DAY)
        assert len(result.records) == 2
        assert result.overflow > 0
        assert result.overflow_is_lower_bound is True


class TestAnAppTokenCannotReadAnotherAppsRuns:
    """An app caller and the dashboard owner both arrive with ``scope is None``.

    ``internal_memory_scope`` answers that for a non-internal caller AND for a
    verified session whose execution record is empty, so scope alone cannot tell
    an app token from the owner. The app CLAIM can: every transport publishes it
    only for a positively resolved app and leaves it absent for the person, so
    its presence is the positive signal and the app bound hangs off that.

    A transport flag cannot carry this, which is what these cases pin. The
    internal-secret arm is one of four arms that publish an app claim; the other
    three are the ordinary cookie and token transports, which set the claim and
    no flag. An app bound keyed to the flag is therefore absent on exactly the
    transport an installed app normally arrives over.
    """

    def audited(self, monkeypatch) -> list[tuple[str, str, str]]:
        calls: list[tuple[str, str, str]] = []
        monkeypatch.setattr(
            messaging,
            "_audit_deny",
            lambda app, event, reason: calls.append((app, event, reason)),
        )
        return calls

    def request(self, state, *, internal: bool, app: str):
        return SimpleNamespace(
            app={"state": state},
            headers={"X-Session-Key": "dashboard:chat-1"},
            query={},
            get=lambda key, default=None: (
                True if key == "internal_auth" and internal else (app if key == "app" else default)
            ),
        )

    def state(self, slot_app: str = ""):
        manager = SimpleNamespace(all_agents=[])
        slots = {"chat-1": SimpleNamespace(_app=slot_app)}
        return SimpleNamespace(subagents=manager, _slots=slots, get_slot=slots.get)

    def listing(self, monkeypatch, *, internal: bool, app: str, slot_app: str = ""):
        calls = self.audited(monkeypatch)

        async def fake_scope(request, op):
            return None, None

        monkeypatch.setattr(messaging, "internal_memory_scope", fake_scope)
        payload = asyncio.get_event_loop().run_until_complete(
            messaging.api_spawn_list(self.request(self.state(slot_app), internal=internal, app=app))
        )
        return calls, json.loads(payload.text or "{}")

    def test_an_app_token_is_refused_another_apps_record(self, agent_root, monkeypatch):
        write_record(agent_root, "own111", app="theirs", parent_session="dashboard:chat-1")
        calls, body = self.listing(monkeypatch, internal=True, app="mine")
        assert body["agents"] == []
        assert [reason for _app, _event, reason in calls] == ["persisted_app_mismatch"]

    def test_an_app_on_the_cookie_transport_is_refused_too(self, agent_root, monkeypatch):
        """The transport an installed app actually arrives over.

        The cookie and token arms publish a validated app claim and set no
        internal flag, so a bound keyed to the flag never runs here and every
        record is admitted through ``scope is None`` -- another app's task, result
        and error text, to a caller holding only its own app's token.
        """
        write_record(agent_root, "own444", app="theirs", parent_session="dashboard:chat-1")
        calls, body = self.listing(monkeypatch, internal=False, app="mine")
        assert body["agents"] == []
        assert [reason for _app, _event, reason in calls] == ["persisted_app_mismatch"]

    def test_an_app_on_the_cookie_transport_still_sees_its_own_record(
        self, agent_root, monkeypatch
    ):
        """The bound narrows to the caller's own app, it does not deny the app."""
        write_record(agent_root, "own555", app="mine", parent_session="dashboard:chat-1")
        calls, body = self.listing(monkeypatch, internal=False, app="mine")
        assert [entry["id"] for entry in body["agents"]] == ["own555"]
        assert calls == []

    def test_an_app_token_still_sees_its_own_record(self, agent_root, monkeypatch):
        """The bound narrows to the caller's own app, it does not deny the app.

        The slot is owned by the same app here because an internal caller takes
        the session and slot-ownership bounds too, exactly as the live half does.
        A record whose app differs from its slot's present owner is the reused-key
        case those bounds exist for, not an app reading its own run.
        """
        write_record(agent_root, "own222", app="mine", parent_session="dashboard:chat-1")
        calls, body = self.listing(monkeypatch, internal=True, app="mine", slot_app="mine")
        assert [entry["id"] for entry in body["agents"]] == ["own222"]
        assert calls == []

    def test_the_owner_is_not_narrowed_by_the_app_bound(self, agent_root, monkeypatch):
        """The owner is not app-authenticated, so the bound does not apply.

        Narrowing the owner here would empty the panel on the cold start this
        whole change exists to fix: a record's app is whatever spawned the run,
        which need not match the app of the tab now reading it. An ABSENT claim
        is what marks the person, which is why the bound reads the claim's
        presence rather than treating an empty claim as an app named "".
        """
        write_record(agent_root, "own333", app="theirs", parent_session="dashboard:chat-1")
        calls, body = self.listing(monkeypatch, internal=False, app="")
        assert [entry["id"] for entry in body["agents"]] == ["own333"]
        assert calls == []


class TestOwnershipIsRecheckedOnTheLoop:
    """The snapshot sizes the cap; the loop decides delivery.

    Slot keys are caller-supplied and are not namespaced by app, so another app
    can reclaim one WHILE the off-loop scan runs. A decision taken inside the
    worker thread would then be read from state the socket does not describe,
    so the admission the scan uses is a snapshot taken on the loop beforehand and
    every surviving record is checked again on the loop before its frame is kept.

    This exercises the two real functions in the order the replay calls them,
    with the owner changing in between.
    """

    def snapshot(self, state) -> dict:
        return slot_owner_snapshot(state)

    def refusal_of(self, snapshot: dict, record: dict) -> str:
        return persisted_snapshot_denial_reason(
            snapshot, subagent_event_slot(str(record.get("parent_session") or "")), record
        )

    def state(self, app: str):
        slots = {"chat-1": SimpleNamespace(_app=app)}
        return SimpleNamespace(_slots=slots, get_slot=slots.get)

    def record(self, app: str) -> dict:
        return {"id": "race111", "app": app, "parent_session": "dashboard:chat-1"}

    def test_an_owner_flip_mid_scan_is_caught_by_the_loop_recheck(self):
        state = self.state("appA")
        record = self.record("appA")
        taken = self.snapshot(state)
        assert self.refusal_of(taken, record) == ""
        # App B reclaims the slot while the scan is still running.
        state._slots["chat-1"]._app = "appB"
        reason = persisted_replay_denial_reason(
            state, subagent_event_slot(str(record["parent_session"])), record
        )
        assert reason == "persisted_owner_mismatch"

    def test_a_stable_owner_survives_both_checks(self):
        state = self.state("appA")
        record = self.record("appA")
        taken = self.snapshot(state)
        assert self.refusal_of(taken, record) == ""
        assert (
            persisted_replay_denial_reason(
                state, subagent_event_slot(str(record["parent_session"])), record
            )
            == ""
        )

    def test_the_snapshot_refuses_a_slot_it_does_not_hold(self):
        taken = self.snapshot(self.state("appA"))
        other = {"id": "race222", "app": "appA", "parent_session": "dashboard:chat-9"}
        assert self.refusal_of(taken, other) == "slot_missing"

    def test_the_snapshot_refuses_a_foreign_app_so_the_cap_is_not_spent(self):
        taken = self.snapshot(self.state("appA"))
        assert self.refusal_of(taken, self.record("appB")) == "persisted_owner_mismatch"

    def test_both_halves_name_the_same_refusal_for_the_same_state(self):
        """One decision, so the two halves cannot disagree about which refusal.

        A bool here would have forced the caller to withhold a record with no SEL
        record of why, which is the case a snapshot rejection normally is: the two
        checks apply the same equality, so most refusals never reach the loop half
        at all.
        """
        for owner, app, expected in (
            ("appA", "appB", "persisted_owner_mismatch"),
            ("", "appB", "persisted_owner_mismatch"),
            ("appA", "appA", ""),
            ("", "", ""),
        ):
            state = self.state(owner)
            record = self.record(app)
            slot = subagent_event_slot(str(record["parent_session"]))
            assert self.refusal_of(self.snapshot(state), record) == expected
            assert persisted_replay_denial_reason(state, slot, record) == expected


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
            await started.wait()
            task.cancel()
            # The waiter is gone; the worker is not, so give it room to finish.
            for _ in range(200):
                if all((agent_root / a / "tombstone.json").exists() for a in ids):
                    break
                await asyncio.sleep(0.01)

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


class TestReplayFrame:
    """The frame shape the panel's reducers consume."""

    def plain(self, text: str) -> str:
        return text

    def test_frame_is_a_terminal_subagent_event(self):
        frame = build_persisted_subagent_frame(
            {
                "id": "abc123",
                "task": "audit the gate",
                "agent": "kirocrew-worker",
                "parent_session": "dashboard:chat-1",
                "started": 100.0,
                "elapsed": 42.0,
                "outcome": "completed",
                "error": "",
                "stopped": False,
            },
            redact=self.plain,
        )
        assert frame["type"] == "subagent_done"
        data = frame["data"]
        assert data["id"] == "abc123"
        assert data["slot"] == "chat-1"
        assert data["elapsed"] == 42.0
        assert data["outcome"] == "completed"
        assert data["task"] == "audit the gate"
        assert data["agent"] == "kirocrew-worker"
        assert data["stopped"] is False

    def test_a_recorded_stop_reaches_the_frame(self):
        frame = build_persisted_subagent_frame(
            {
                "id": "a",
                "parent_session": "dashboard:chat-1",
                "outcome": "stopped",
                "error": "",
                "stopped": True,
            },
            redact=self.plain,
        )
        assert frame["data"]["stopped"] is True
        assert frame["data"]["outcome"] == "stopped"

    def test_no_error_is_sent_as_null_not_an_empty_string(self):
        frame = build_persisted_subagent_frame(
            {"id": "a", "parent_session": "dashboard:chat-1", "outcome": "completed", "error": ""},
            redact=self.plain,
        )
        assert frame["data"]["error"] is None

    def test_an_error_travels_through_the_callers_redactor(self):
        frame = build_persisted_subagent_frame(
            {
                "id": "a",
                "parent_session": "dashboard:chat-1",
                "outcome": "failed",
                "error": "Orphaned: gateway_restart",
            },
            redact=lambda text: text.upper(),
        )
        assert frame["data"]["error"] == "ORPHANED: GATEWAY_RESTART"

    def test_task_and_agent_travel_through_the_callers_redactor(self):
        frame = build_persisted_subagent_frame(
            {
                "id": "a",
                "parent_session": "dashboard:chat-1",
                "outcome": "completed",
                "error": "",
                "task": "secret",
                "agent": "worker",
            },
            redact=lambda text: f"[{text}]",
        )
        assert frame["data"]["task"] == "[secret]"
        assert frame["data"]["agent"] == "[worker]"

    def test_a_record_with_no_parent_yields_an_ownerless_frame(self):
        """Which the replay's own owner filter then drops, as it does for live frames."""
        frame = build_persisted_subagent_frame(
            {"id": "a", "parent_session": "", "outcome": "completed", "error": ""},
            redact=self.plain,
        )
        assert frame["data"]["slot"] == ""


class TestADismissalOutlivesTheManager:
    """A dismissed card must not come back when the panel reads folders.

    The live half of a dismissal is ``settle_before_delete`` popping the run out
    of the manager, and before this it was the WHOLE dismissal: the durable half
    excludes only the ids the live manager still holds, so the next rebuild read
    the folder the delete left behind and put the card back. The dismissal is
    recorded durably instead, and the folder stays where it is -- ``spawn_continue``
    reseeds its session map from that folder's ``state.json``.
    """

    def test_a_dismissed_run_stays_out_of_the_panel(self, agent_root):
        write_record(agent_root, "keep-me")
        write_record(agent_root, "dismiss-me")
        assert sorted(ids(panel(keep=10, max_age_secs=DAY))) == ["dismiss-me", "keep-me"]

        assert record_panel_dismissal("dismiss-me") is True

        assert ids(panel(keep=10, max_age_secs=DAY)) == ["keep-me"]

    def test_the_run_folder_is_left_alone_so_a_conversation_survives(self, agent_root):
        """Removing the folder would hide the card AND break ``spawn_continue``."""
        write_record(agent_root, "dismiss-me")
        record_panel_dismissal("dismiss-me")
        assert (agent_root / "dismiss-me" / "state.json").exists()
        assert read_state("dismiss-me") is not None

    def test_the_record_lives_outside_the_agent_writable_run_folder(self, agent_root):
        """A run must not be able to hide its own card from the operator."""
        write_record(agent_root, "dismiss-me")
        record_panel_dismissal("dismiss-me")
        store = subagent_persistence._panel_dismissals_dir()
        written = [p for p in store.rglob("*") if p.is_file()]
        assert written, "the dismissal left no durable record"
        assert not any(agent_root in p.parents for p in written)
        assert agent_root not in store.parents and store != agent_root

    def test_a_run_with_no_folder_records_nothing(self, agent_root):
        """Nothing durable can resurrect that card, and prune would never reclaim it."""
        assert record_panel_dismissal("never-ran") is False
        assert dismissed_panel_ids() == frozenset()

    def test_an_unreadable_record_shows_the_run_rather_than_hiding_it(
        self, agent_root, monkeypatch
    ):
        """Fail OPEN: a fault may resurrect a card, never hide a run's history."""
        write_record(agent_root, "dismiss-me")
        record_panel_dismissal("dismiss-me")

        def boom(_path):
            raise OSError("registry unreadable")

        monkeypatch.setattr(subagent_persistence.os, "scandir", boom)
        assert dismissed_panel_ids() == frozenset()

    def test_a_dismissed_folder_does_not_spend_a_candidate_slot(self, agent_root):
        """Filtered before the heap, so a dismissal cannot push a visible run out."""
        now = time.time()
        write_record(agent_root, "older", mtime=now - 300)
        for index in range(4):
            write_record(agent_root, f"dismissed-{index}", mtime=now - index)
            record_panel_dismissal(f"dismissed-{index}")
        records = read_panel_records(keep=1, max_age_secs=DAY)
        assert ids(records.records) == ["older"]
        assert records.overflow == 0

    def test_deleting_the_folder_reclaims_the_record(self, agent_root):
        write_record(agent_root, "dismiss-me")
        record_panel_dismissal("dismiss-me")
        assert dismissed_panel_ids() == frozenset({"dismiss-me"})
        delete_agent_folder("dismiss-me")
        assert dismissed_panel_ids() == frozenset()

    def test_settle_before_delete_records_the_dismissal(self, agent_root, monkeypatch):
        """The pop and the durable record are one act, not two the route must pair."""
        import kiro_crew.subagent as subagent_module

        write_record(agent_root, "a1")
        recorded: list[str] = []

        def record(agent_id):
            recorded.append(agent_id)
            return subagent_persistence.DISMISSAL_RECORDED

        monkeypatch.setattr(subagent_module, "record_panel_dismissal_outcome", record)
        manager = SimpleNamespace(
            _agents={"a1": SimpleNamespace(id="a1", _ending_claimed=False, done=True)},
            _tasks={},
            _report_owners={},
        )
        asyncio.run(subagent_module.SubagentManager.settle_before_delete(manager, "a1"))
        assert recorded == ["a1"]
        assert "a1" not in manager._agents

    def test_the_record_is_written_off_the_event_loop(self, agent_root):
        """It is a synchronous file write, and its caller is a coroutine."""
        import inspect

        import kiro_crew.subagent as subagent_module

        source = inspect.getsource(subagent_module.SubagentManager.settle_before_delete)
        assert "asyncio.to_thread(record_panel_dismissal" in source

    def test_the_sweep_removes_a_record_whose_folder_is_gone(self, agent_root):
        import shutil

        write_record(agent_root, "swept")
        record_panel_dismissal("swept")
        shutil.rmtree(agent_root / "swept")
        assert prune_orphan_panel_dismissals() == 1
        assert dismissed_panel_ids() == frozenset()

    def test_the_sweep_keeps_a_record_whose_folder_remains(self, agent_root):
        write_record(agent_root, "kept")
        record_panel_dismissal("kept")
        assert prune_orphan_panel_dismissals() == 0
        assert dismissed_panel_ids() == frozenset({"kept"})

    def test_the_sweep_counts_only_what_it_removed(self, agent_root):
        """A name no run could carry is removed once, not counted every cycle."""
        write_record(agent_root, "kept")
        record_panel_dismissal("kept")
        stray = subagent_persistence._panel_dismissals_dir() / "not..an..id"
        stray.write_text("{}", encoding="utf-8")
        assert prune_orphan_panel_dismissals() == 1
        assert prune_orphan_panel_dismissals() == 0
        assert dismissed_panel_ids() == frozenset({"kept"})


class TestAFailedDismissalWriteIsNotPublished:
    """The pop is the publish, so it must not happen before the record is stored.

    With the write second and its result discarded, an unwritable store still
    popped the run and still answered ``"delivered"`` -- which the DELETE route
    reports as success -- and the card came back on the next reconnect with
    nothing to explain it. The user is told the dismissal worked and watches it
    undo itself.

    The two falsy cases are NOT interchangeable. A run with no folder has nothing
    durable to bring its card back, so its dismissal stands and the pop proceeds;
    only a FAILED write holds it. Collapsing them would make every folderless run
    permanently undismissable, which is the over-correction this pins against.
    """

    def manager(self):
        return SimpleNamespace(
            _agents={"a1": SimpleNamespace(id="a1", _ending_claimed=False, done=True)},
            _tasks={"a1": object()},
            _report_owners={},
        )

    def settle(self, monkeypatch, outcome: str):
        import kiro_crew.subagent as subagent_module

        monkeypatch.setattr(
            subagent_module, "record_panel_dismissal_outcome", lambda agent_id: outcome
        )
        manager = self.manager()
        result = asyncio.run(subagent_module.SubagentManager.settle_before_delete(manager, "a1"))
        return result, manager

    def test_a_failed_write_keeps_the_run_and_asks_for_a_retry(self, agent_root, monkeypatch):
        result, manager = self.settle(monkeypatch, subagent_persistence.DISMISSAL_FAILED)
        assert result == "pending", "a dismissal that was not stored must not report delivered"
        assert "a1" in manager._agents, (
            "the run was popped although its dismissal was never stored, so the card "
            "returns on the next rebuild and the route reported success"
        )
        assert "a1" in manager._tasks

    def test_a_stored_write_publishes_the_dismissal(self, agent_root, monkeypatch):
        result, manager = self.settle(monkeypatch, subagent_persistence.DISMISSAL_RECORDED)
        assert result == "delivered"
        assert "a1" not in manager._agents
        assert "a1" not in manager._tasks

    def test_a_run_with_no_folder_is_still_dismissable(self, agent_root, monkeypatch):
        """Control: the fix must not make a folderless run undismissable forever."""
        result, manager = self.settle(monkeypatch, subagent_persistence.DISMISSAL_NO_FOLDER)
        assert result == "delivered"
        assert "a1" not in manager._agents

    def test_the_outcome_separates_a_missing_folder_from_a_failed_write(self, agent_root):
        """Read off the real function, so the three answers are not just constants."""
        write_record(agent_root, "real11")
        assert (
            subagent_persistence.record_panel_dismissal_outcome("real11")
            == subagent_persistence.DISMISSAL_RECORDED
        )
        assert (
            subagent_persistence.record_panel_dismissal_outcome("never-ran")
            == subagent_persistence.DISMISSAL_NO_FOLDER
        )

    def test_an_unwritable_store_reports_failed_not_missing(self, agent_root, monkeypatch):
        write_record(agent_root, "real22")

        def boom(*_a, **_kw):
            raise OSError("store is read-only")

        monkeypatch.setattr(subagent_persistence, "atomic_write", boom)
        assert (
            subagent_persistence.record_panel_dismissal_outcome("real22")
            == subagent_persistence.DISMISSAL_FAILED
        )
        # The legacy bool keeps its meaning for callers that only need "stored".
        assert subagent_persistence.record_panel_dismissal("real22") is False


class TestTheDismissalCheckRetainsNothing:
    """The reader's working set is bounded, so the dismissal check must be too.

    ``read_panel_records`` caps its candidate heap on purpose. Answering "is this
    dismissed" by enumerating every record into a set put one entry per dismissal
    back inside that bound, so a registry with many retained dismissals set the
    memory the cap exists to fix. The question is per candidate, and so is the
    answer now.
    """

    def test_many_dismissals_do_not_change_what_the_reader_returns(self, agent_root):
        """Behavioural control: the cheaper check still filters every one of them."""
        write_record(agent_root, "visible")
        for index in range(40):
            name = f"gone{index:03d}"
            write_record(agent_root, name)
            assert record_panel_dismissal(name) is True
        assert ids(panel(keep=10, max_age_secs=DAY)) == ["visible"]

    def test_the_probe_answers_per_run(self, agent_root):
        write_record(agent_root, "one")
        write_record(agent_root, "two")
        record_panel_dismissal("one")
        assert subagent_persistence.panel_dismissal_recorded("one") is True
        assert subagent_persistence.panel_dismissal_recorded("two") is False

    def test_the_probe_fails_open(self, agent_root, monkeypatch):
        """A fault resurrects a card; it must never hide an undismissed run."""
        write_record(agent_root, "one")
        record_panel_dismissal("one")
        assert subagent_persistence.panel_dismissal_recorded("one") is True

        def boom(_self):
            raise OSError("registry unreadable")

        monkeypatch.setattr(pathlib.Path, "is_file", boom)
        assert subagent_persistence.panel_dismissal_recorded("one") is False


def _messaging_handler_files(root: pathlib.Path, *holders: str) -> list[pathlib.Path]:
    """``handlers/messaging.py`` and the ``messaging_api`` owners it composes routes from.

    The facade's routes run from those owners, so a source ratchet on the facade
    reads them all; each name in *holders* must live in one of the returned files.
    """
    import inspect

    dashboard = root / "src/kiro_crew/dashboard"
    owners = sorted((dashboard / "messaging_api").glob("[!_]*.py"))
    assert owners, "the messaging_api owners were not found"
    files = [dashboard / "handlers" / "messaging.py", *owners]
    for name in holders:
        held = pathlib.Path(inspect.unwrap(getattr(messaging, name)).__code__.co_filename)
        assert held.parts[-2:] in {path.parts[-2:] for path in files}, (name, held)
    return files


class TestADismissalHoldsOnBothReaders:
    """A dismissal has to hold on every durable reader, not just one.

    There were two folder readers and they were reported separately: the WS
    reconnect replay and the REST listing. Each excluded only the ids the LIVE
    manager held, so a delete that left the folder behind came back on the next
    reconnect. The filter's position -- inside the shared helper rather than at
    either call site -- is what let one store answer for both, and that position
    is pinned structurally below.

    One of those two readers is gone: the replay folds the crew log, where a
    dismissal is an ENTRY rather than a record in a store beside it. That is not a
    softening of this class's rule but the same rule applied one level up -- the
    folder store could only be keyed on the folder, so a dismissal in it did not
    outlive the thing it was suppressing. What remains here is the listing, which
    reads folders because it serves the run's own output text, and the ratchet that
    keeps the replay from quietly becoming a second folder reader again.
    """

    def request(self, state, caller: str, app: str = "", *, internal: bool = True):
        headers = {"X-Session-Key": caller}
        values: dict[str, object] = {"internal_auth": internal}
        if app:
            values["app"] = app
        return SimpleNamespace(
            app={"state": state},
            headers=headers,
            query={},
            get=lambda key, default=None: values.get(key, default),
        )

    def state(self, slot_app: str = ""):
        manager = SimpleNamespace(all_agents=[])
        slots = {"chat-1": SimpleNamespace(_app=slot_app)}
        return SimpleNamespace(subagents=manager, _slots=slots, get_slot=slots.get)

    def run_listing(self, monkeypatch, caller: str = "dashboard:chat-1"):
        """Drive ``GET /api/spawn`` end to end and return its agent ids."""

        async def fake_scope(request, op):
            return object(), None

        monkeypatch.setattr(messaging, "internal_memory_scope", fake_scope)
        payload = asyncio.get_event_loop().run_until_complete(
            messaging.api_spawn_list(self.request(self.state(), caller))
        )
        body = json.loads(payload.text or "{}")
        return [entry["id"] for entry in body["agents"]]

    def replay_records(self, seen: set[str] | None = None):
        """The LISTING's query with a durable rebuild's own bounds.

        The WS replay asks a different source entirely: it folds the crew log,
        where a dismissal is an entry rather than a record in a store beside it.
        These bounds are kept because the listing applies them too, and because
        the keep/age pair is what makes the question a durable rebuild's rather
        than a generic read.
        """
        return read_panel_records(
            keep=PERSISTED_SUBAGENT_REPLAY_KEEP,
            max_age_secs=PERSISTED_SUBAGENT_REPLAY_MAX_AGE_SECS,
            exclude_ids=seen or set(),
        ).records

    def test_a_dismissed_run_is_absent_from_the_rest_listing(self, agent_root, monkeypatch):
        write_record(agent_root, "keep11", parent_session="dashboard:chat-1")
        write_record(agent_root, "gone11", parent_session="dashboard:chat-1")
        # Control: both are listed before the dismissal, so absence below is the
        # dismissal doing it rather than the record never having been admitted.
        assert sorted(self.run_listing(monkeypatch)) == ["gone11", "keep11"]

        assert record_panel_dismissal("gone11") is True

        assert self.run_listing(monkeypatch) == ["keep11"]

    def test_a_dismissed_run_is_absent_from_the_durable_folder_read(self, agent_root):
        """The reader that actually resurrected the card, asked its own question.

        The WS replay's half of this moved with the reader: it folds the crew log
        now, and ``test_subagent_panel_fold_replay.py`` pins a dismissal there --
        including the case this store cannot answer at all, a run whose folder was
        already reclaimed.
        """
        write_record(agent_root, "keep22", parent_session="dashboard:chat-1")
        write_record(agent_root, "gone22", parent_session="dashboard:chat-1")
        assert sorted(ids(self.replay_records())) == ["gone22", "keep22"]

        assert record_panel_dismissal("gone22") is True

        surviving = self.replay_records()
        assert ids(surviving) == ["keep22"]
        # And nothing downstream can put it back: the frames the listing's records
        # would build are built from these alone.
        frames = [
            build_persisted_subagent_frame(record, redact=lambda text: text) for record in surviving
        ]
        assert [frame["data"]["id"] for frame in frames] == ["keep22"]

    def test_the_folder_read_still_honours_its_live_exclusions(self, agent_root):
        """Control on the shape: the dismissal filter is added to that set, not swapped in."""
        write_record(agent_root, "live33", parent_session="dashboard:chat-1")
        write_record(agent_root, "gone33", parent_session="dashboard:chat-1")
        write_record(agent_root, "keep33", parent_session="dashboard:chat-1")
        record_panel_dismissal("gone33")
        assert ids(self.replay_records(seen={"live33"})) == ["keep33"]

    def test_the_filter_lives_inside_the_shared_reader_not_at_a_call_site(self):
        """Why one store answers for both readers -- pinned, not assumed.

        Filtering at the two call sites would read the same on the day it is
        written and would leave the next reader uncovered. The filter is inside
        :func:`read_panel_records`, so a third reader inherits it by construction.
        """
        import ast

        root = pathlib.Path(__file__).resolve().parents[1]
        tree = ast.parse(
            (root / "src/kiro_crew/subagent_persistence.py").read_text(encoding="utf-8")
        )
        reader = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "read_panel_records"
        )
        called = {
            getattr(call.func, "id", "") or getattr(call.func, "attr", "")
            for call in ast.walk(reader)
            if isinstance(call, ast.Call)
        }
        assert "panel_dismissal_recorded" in called, (
            "read_panel_records does not consult the dismissal store, so each reader "
            "would have to filter for itself and the next one added would not"
        )
        # And it consults it per CANDIDATE, never by materialising the whole set:
        # this function's contract is a bounded working set, and enumerating every
        # record would let the registry's size decide its memory.
        assert "dismissed_panel_ids" not in called, (
            "read_panel_records materialises every dismissal id, so its retained set "
            "grows with the registry inside the one function bounded on purpose"
        )

    def test_each_durable_source_is_reached_through_the_reader_that_filters_it(self):
        """Neither reader may assemble records itself and skip the dismissal filter.

        Matched on a word boundary, because ``_panel_record`` is a SUBSTRING of
        ``read_panel_records`` -- a plain ``in`` test reports a correct reader as an
        offender, and the failure reads exactly like a real bypass.

        Both files read folders, and both must do it through the shared helper that
        consults the dismissal registry. The WS replay reads them on ONE path only,
        the one taken when the crew log is switched off -- where there is no fold,
        so the registry is the only dismissal record there is. With the log on it
        folds instead, and the dismissal is an entry in that same log; the gate
        between those two is pinned below, because a folder read reached with the
        log ON would honour a registry entry while ignoring the log entry that
        supersedes it.
        """
        import re

        builder = re.compile(r"(?<![A-Za-z0-9_])_panel_record(?![A-Za-z0-9_])")
        root = pathlib.Path(__file__).resolve().parents[1]
        for relative in (
            "src/kiro_crew/dashboard/handlers/messaging.py",
            "src/kiro_crew/dashboard/ws.py",
        ):
            source = (root / relative).read_text(encoding="utf-8")
            if relative.endswith("handlers/messaging.py"):
                # The listing reader runs from a messaging_api owner, so this reader
                # is the facade and every owner it composes, read together.
                source = "\n".join(
                    path.read_text(encoding="utf-8")
                    for path in _messaging_handler_files(root, "api_spawn_list")
                )
            # Control: this reader really is one, so the assertion below cannot pass
            # by matching a file that stopped reading records altogether.
            assert "read_panel_records" in source, f"{relative} no longer reads persisted records"
            assert not builder.search(source), (
                f"{relative} reaches the per-folder record builder directly, which "
                "skips the dismissal filter that read_panel_records applies"
            )
        # Control on the needle itself: it must match the real thing somewhere, or
        # the assertions above pass because the pattern is broken.
        owner = (root / "src/kiro_crew/subagent_persistence.py").read_text(encoding="utf-8")
        assert builder.search(owner), "the _panel_record needle matches nothing anywhere"

    def test_the_replay_folds_the_log_when_it_is_on_and_reads_folders_when_it_is_not(self):
        """The gate that keeps one dismissal record authoritative at a time.

        Asserted on the source because the alternative is a live socket: the branch
        sits inside the reconnect handler, which needs a real aiohttp WebSocket, and
        the two readers it chooses between are each pinned on their own elsewhere.

        What is pinned is which reader each branch REACHES, not how it is called.
        A reader handed to :func:`asyncio.to_thread` is NAMED rather than called,
        and the log-on branch names its own worker there, so a pattern expecting
        the reader as the thread's first argument describes one spelling of the
        call rather than the gate.
        """
        import ast

        root = pathlib.Path(__file__).resolve().parents[1]
        source = (root / "src/kiro_crew/dashboard/ws.py").read_text(encoding="utf-8")

        def _names(body: list[ast.stmt]) -> set[str]:
            """Every name this code reaches: bare, called, handed over, or attribute.

            Attributes count because a value read off a returned record -- the
            folder scan's own ``overflow_is_lower_bound`` -- is reached by
            attribute and not by name.
            """
            module = ast.Module(body=body, type_ignores=[])
            found = set()
            for sub in ast.walk(module):
                if isinstance(sub, ast.Name):
                    found.add(sub.id)
                elif isinstance(sub, ast.Attribute):
                    found.add(sub.attr)
            return found

        tree = ast.parse(source)
        gates = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.If)
            and isinstance(node.test, ast.Call)
            and isinstance(node.test.func, ast.Name)
            and node.test.func.id == "crew_log_enabled"
            and node.orelse
            and "read_panel_records" in _names(node.orelse)
        ]
        assert len(gates) == 1, (
            "the replay's folder read is no longer the crew-log-off branch of one "
            "crew_log_enabled() gate, so a folder record could be replayed while "
            "the log holds the dismissal"
        )
        gate = gates[0]
        on_branch = _names(gate.body)
        off_branch = _names(gate.orelse)
        assert (
            "read_fold_subagent_records" in on_branch
        ), "the replay no longer folds the crew log behind crew_log_enabled()"
        # Each branch reaches ONE reader. A branch reaching both would draw a card
        # from the folders while the log holds its dismissal, which is the whole
        # point of choosing between them.
        assert "read_panel_records" not in on_branch
        assert "read_fold_subagent_records" not in off_branch
        # The log-on branch corrects the legacy registry's dismissals into the log
        # before it folds. Without that a card dismissed before the log held
        # dismissals comes back, which is the failure this reader exists to end.
        assert "backfill_legacy_panel_dismissals" in on_branch
        # And what it corrected is excluded from THIS read too. The append is
        # queued, so the fold that follows it may not carry it yet -- a branch that
        # passed only the live ids would draw the cleared card once, on the very
        # reconnect that corrected it. Pinned through the NAME the backfill's
        # result is bound to, so dropping the union is caught however it is spelt.
        bound = [
            target.id
            for node in ast.walk(ast.Module(body=gate.body, type_ignores=[]))
            if isinstance(node, ast.Assign)
            and "backfill_legacy_panel_dismissals" in _names([ast.Expr(value=node.value)])
            for target in node.targets
            if isinstance(target, ast.Name)
        ]
        assert len(bound) == 1, "the backfill's result is no longer bound to one name"
        excluded = [
            node
            for node in ast.walk(ast.Module(body=gate.body, type_ignores=[]))
            if isinstance(node, ast.keyword)
            and node.arg == "exclude_ids"
            and bound[0] in _names([ast.Expr(value=node.value)])
        ]
        assert len(excluded) == 1, (
            "the fold read's exclude_ids no longer carries what the backfill "
            f"corrected ({bound[0]}), so a corrected card is drawn once"
        )

        # Every blocking read sits INSIDE the worker handed to asyncio.to_thread.
        # Each of these lists the crew-log root, stats it, and on a cold cache
        # opens a header file per unit, so one left in the branch body is a
        # filesystem walk per slot on the loop that carries chat and heartbeats.
        blocking = {
            "crew_log_panel_units",
            "backfill_legacy_panel_dismissals",
            "read_fold_subagent_records",
        }
        workers = [
            node
            for node in ast.walk(ast.Module(body=gate.body, type_ignores=[]))
            if isinstance(node, ast.FunctionDef)
        ]
        assert len(workers) == 1, "the log-on branch no longer has exactly one worker"
        inside = _names(workers[0].body)
        assert blocking <= inside, f"not every blocking read is in the worker: {blocking - inside}"
        handed = [
            node
            for node in ast.walk(ast.Module(body=gate.body, type_ignores=[]))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "to_thread"
            and workers[0].name in _names([ast.Expr(value=arg) for arg in node.args])
        ]
        assert len(handed) == 1, "the worker is no longer the one thing handed to to_thread"
        # And none of them is ALSO reached from the branch body outside it, which
        # is how the walk creeps back onto the loop while the worker still exists.
        on_loop = _names([stmt for stmt in gate.body if not isinstance(stmt, ast.FunctionDef)])
        assert not (blocking & on_loop), f"a blocking read runs on the loop: {blocking & on_loop}"

        # The folder scan has its own candidate window, and a window that FILLED
        # means admissible runs past it were never inspected. The count cannot
        # describe them -- a window full of records this socket then rejected
        # reports an overflow of zero while older eligible runs went unseen -- so
        # the flag has to travel with the records and reach the warning.
        assert "overflow_is_lower_bound" in off_branch, (
            "the crew-log-off fallback drops overflow_is_lower_bound, so a "
            "saturated scan reads as a complete replay"
        )
        enclosing = [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
            and any(child is gate for child in ast.walk(node))
        ]
        assert enclosing, "the gate has no enclosing function"
        handler = min(enclosing, key=lambda node: len(list(ast.walk(node))))
        warnings = [
            node
            for node in ast.walk(handler)
            if isinstance(node, ast.If)
            and isinstance(node.test, ast.BoolOp)
            and isinstance(node.test.op, ast.Or)
            and any(
                isinstance(call, ast.Attribute) and call.attr == "warning"
                for call in ast.walk(ast.Module(body=node.body, type_ignores=[]))
            )
        ]
        assert len(warnings) == 1, "the replay's truncation warning is no longer one or-gated if"
        # Gated on EITHER, so a saturated window reports even at a count of zero.
        assert len(_names(warnings[0].test.values)) >= 2
        # Control on the needles: both readers must be reached somewhere in the
        # module, or the assertions above pass because the names are wrong.
        whole = _names(tree.body)
        assert {"read_fold_subagent_records", "read_panel_records"} <= whole


class TestTheDismissalStoreIsRegisteredEverywhereItMustBe:
    """The store's protection is four registrations, and a rename breaks them silently.

    ``record_panel_dismissal`` writes an OWNER decision about what the panel hides,
    so no sandboxed process may forge or unlink one. ``trust/`` cannot host such a
    record: it is a declared sandbox READ-WRITE exception, so a record under it
    stays writable by a command that builds the path at runtime, which command
    matching cannot catch. The leaf therefore lives at the data-home root and is
    registered in four places. Nothing in the code fails when one is missing -- it
    just leaves the store unprotected -- so each is pinned by name here.
    """

    def test_the_leaf_is_not_under_trust(self):
        parts = subagent_persistence._panel_dismissals_dir().parts
        assert "trust" not in parts
        assert parts[-1] == subagent_persistence._PANEL_DISMISSAL_LEAF

    def test_the_leaf_is_a_direct_child_of_the_data_home(self):
        """A nested leaf would not be covered: every list keys on a top-level name."""
        from kiro_crew.subagent_persistence import _subagents_dir

        assert subagent_persistence._panel_dismissals_dir().parent == _subagents_dir().parent

    def test_the_leaf_is_on_the_file_gates_sensitive_floor(self):
        """Agent file tools must neither read nor write the store."""
        from kiro_crew.security import paths as security_paths

        floor = security_paths._CREW_SECRET_LEAVES
        # Control: an empty or renamed list would make the assertion below pass
        # for the wrong reason, so pin a sibling owner-authority leaf too.
        assert "crew-teams" in floor, "not the floor list this test means to read"
        assert subagent_persistence._PANEL_DISMISSAL_LEAF in floor

    def test_the_leaf_is_bind_masked_in_the_sandbox(self):
        from kiro_crew import sandbox

        assert subagent_persistence._PANEL_DISMISSAL_LEAF in sandbox._CREW_HIDDEN_LEAVES

    def test_the_leaf_is_precreated_so_the_mask_is_not_skipped(self):
        """Created on the first dismissal, so an absent name would skip the bind."""
        from kiro_crew import sandbox

        assert (
            subagent_persistence._PANEL_DISMISSAL_LEAF in sandbox._CREW_PRECREATE_HIDDEN_DIR_LEAVES
        )

    def test_a_symlink_at_the_leaf_is_refused(self):
        """A link attaches the mask to the target and leaves the name writable."""
        from kiro_crew import sandbox

        assert subagent_persistence._PANEL_DISMISSAL_LEAF in sandbox._CREW_NO_ALIAS_LEAVES


class TestThePersistedGrantIsRecordedNotJustTheRefusals:
    """The ownership decision leaves a record whichever way it goes.

    This replay writes to the socket directly, so ``ws_event_allowed`` never sees
    the frame, and a dashboard user short-circuits ``_ws_client_allowed``
    unconditionally -- the two places a grant would otherwise be recorded. With
    only the deny sites audited, the SEL stream cannot distinguish a rebuild that
    delivered a run from one that never considered it.

    Structural rather than behavioural: the decision sits inside the
    ``subscribe_subagents`` handler, and what can regress is the audit call being
    deleted, which reading the source catches exactly.
    """

    ROOT = pathlib.Path(__file__).resolve().parents[1]
    REL = "src/kiro_crew/dashboard/ws.py"

    def block(self) -> list:
        """The INNERMOST statement block enclosing the persisted-frame append.

        Innermost matters and is the whole reason this helper is not two lines:
        ``ws.py`` already calls ``_audit_grant_quietly`` at three unrelated sites,
        so any outer block -- the handler body, the module -- contains one of those
        and would satisfy this pin no matter what the append site does. The
        mutation matrix caught exactly that, so the block is chosen as the one
        whose own enclosing statement spans the fewest lines.
        """
        import ast

        tree = ast.parse((self.ROOT / self.REL).read_text(encoding="utf-8"))

        def holds_append(node) -> bool:
            return any(
                isinstance(c, ast.Call)
                and getattr(c.func, "id", "") == "build_persisted_subagent_frame"
                for c in ast.walk(node)
            )

        best: tuple[int, list] | None = None
        for node in ast.walk(tree):
            body = getattr(node, "body", None)
            if not isinstance(body, list):
                continue
            for stmt in body:
                if not holds_append(stmt):
                    continue
                span = getattr(stmt, "end_lineno", stmt.lineno) - stmt.lineno
                if best is None or span < best[0]:
                    best = (span, body)
        if best is None:
            raise AssertionError(
                f"{self.REL} no longer appends a persisted frame -- this pin names the wrong site"
            )
        return best[1]

    def names_called(self, body: list) -> list[str]:
        import ast

        out: list[str] = []
        for stmt in body:
            for call in ast.walk(stmt):
                if isinstance(call, ast.Call):
                    name = getattr(call.func, "id", "") or getattr(call.func, "attr", "")
                    if name:
                        out.append(name)
        return out

    def test_the_append_block_records_the_grant(self):
        called = self.names_called(self.block())
        # Control: the block really is the one that appends the frame, so an
        # empty or wrong match cannot pass this as a success.
        assert "build_persisted_subagent_frame" in called
        # Control: and it is the TIGHT block. ws.py holds three unrelated
        # ``_audit_grant_quietly`` calls, so a block wide enough to reach them
        # would pass this pin whatever the append site did.
        assert "persisted_replay_denial_reason" in called, called
        assert called.count("_audit_grant_quietly") <= 1, called
        assert "_audit_grant_quietly" in called, (
            "the persisted frame is appended with no grant recorded; its refusals "
            "are audited, so only the deny side would appear in the SEL stream"
        )

    def test_the_grant_is_recorded_before_the_append(self):
        called = self.names_called(self.block())
        assert called.index("_audit_grant_quietly") < called.index("build_persisted_subagent_frame")

    def test_the_grant_names_the_same_auditee_as_the_refusals(self):
        """A grant labelled differently from its own denies cannot be correlated.

        ``_audit_grant_quietly`` falls back to ``<unknown>`` while both refusals
        beside it record ``<dashboard>``, so passing a bare ``ws_app`` here would
        file the two halves of ONE ownership decision under two subjects.
        """
        import ast

        args: list[str] = []
        for stmt in self.block():
            for call in ast.walk(stmt):
                if not isinstance(call, ast.Call):
                    continue
                if getattr(call.func, "id", "") != "_audit_grant_quietly":
                    continue
                assert call.args, "the grant records no auditee at all"
                args.append(ast.unparse(call.args[0]).replace("'", '"'))
        # Control: exactly one grant in the tight block, so an empty list cannot
        # pass this as a success.
        assert len(args) == 1, args
        assert args[0] == 'ws_app or "<dashboard>"', args

    def test_both_refusal_sites_are_still_audited(self):
        """The grant must be added beside the denies, never instead of them."""
        source = (self.ROOT / self.REL).read_text(encoding="utf-8")
        assert source.count('_audit_deny(ws_app or "<dashboard>", "subagent_done"') == 2

    def test_the_rest_listing_records_the_same_grant(self):
        """One ownership decision, so the trail cannot depend on which reader asked."""
        source = "\n".join(
            path.read_text(encoding="utf-8")
            for path in _messaging_handler_files(self.ROOT, "api_spawn_list")
        )
        assert '_audit_allow(auditee, "api_spawn_list")' in source


class _DeleteRequest:
    """Request double for the delete route, with a real ``in`` for the app claim.

    ``app=None`` means the key is ABSENT, which is a different state from an empty
    claim and is the one the route refuses -- so the double has to be able to
    produce both, and an instance attribute cannot: ``in`` dispatches on the type.
    """

    def __init__(self, agent_id: str, app: object = "", slots: dict | None = None) -> None:
        self.app = {"state": SimpleNamespace(subagents=None, _slots=slots or {})}
        self.headers: dict[str, str] = {}
        self.match_info = {"agent_id": agent_id}
        self.query: dict[str, str] = {}
        self._extra = {} if app is None else {"app": app}

    def __contains__(self, key: str) -> bool:
        return key in self._extra

    def __getitem__(self, key: str):
        return self._extra[key]

    def get(self, key: str, default=None):
        return self._extra.get(key, default)


class TestACardRebuiltFromDiskCanBeDismissed:
    """After a restart every finished card has no manager entry.

    ``DELETE /api/spawn/{id}`` answered 404 for exactly those ids, so the one
    control the user has over a rebuilt card refused, showed the failure banner,
    and the next reconnect sent the card again. The route records the dismissal
    against the folder instead.
    """

    def request(self, agent_id: str, app: object = ""):
        return _DeleteRequest(agent_id, app)

    def delete(self, agent_id: str, app: object = ""):
        response = asyncio.get_event_loop().run_until_complete(
            messaging.api_spawn_delete(self.request(agent_id, app))
        )
        return response.status, json.loads(response.text or "{}")

    def test_a_persisted_run_is_dismissed_and_stays_dismissed(self, agent_root, monkeypatch):
        monkeypatch.setattr(
            messaging, "_sel", lambda: SimpleNamespace(log_api_access=lambda **_: None)
        )
        write_record(agent_root, "ondisk1")
        status, body = self.delete("ondisk1")
        assert (status, body["dismissed"], body["cancelled"]) == (200, True, False)
        assert panel(keep=10, max_age_secs=DAY) == []

    def test_an_unknown_id_is_still_not_found(self, agent_root, monkeypatch):
        monkeypatch.setattr(
            messaging, "_sel", lambda: SimpleNamespace(log_api_access=lambda **_: None)
        )
        assert self.delete("never-ran")[0] == 404
        assert dismissed_panel_ids() == frozenset()

    def test_an_app_token_cannot_dismiss_a_run(self, agent_root, monkeypatch):
        """No caller gains reach over a run it could not list."""
        denied: list[dict] = []
        monkeypatch.setattr(
            messaging,
            "_sel",
            lambda: SimpleNamespace(log_api_access=lambda **kw: denied.append(kw)),
        )
        write_record(agent_root, "ondisk2", app="appA")
        status, body = self.delete("ondisk2", app="appA")
        assert (status, body["code"]) == (403, "app_token_forbidden")
        assert dismissed_panel_ids() == frozenset()
        assert [entry["outcome"] for entry in denied] == ["denied"]

    def test_an_absent_app_claim_is_refused_rather_than_trusted(self, agent_root, monkeypatch):
        """Absent means the request never passed the auth middleware."""
        monkeypatch.setattr(
            messaging, "_sel", lambda: SimpleNamespace(log_api_access=lambda **_: None)
        )
        write_record(agent_root, "ondisk3")
        assert self.delete("ondisk3", app=None)[0] == 403
        assert dismissed_panel_ids() == frozenset()

    def test_the_dismissal_is_audited(self, agent_root, monkeypatch):
        recorded: list[dict] = []
        monkeypatch.setattr(
            messaging,
            "_sel",
            lambda: SimpleNamespace(log_api_access=lambda **kw: recorded.append(kw)),
        )
        write_record(agent_root, "ondisk4")
        self.delete("ondisk4")
        assert [(e["operation"], e["outcome"]) for e in recorded] == [("spawn.dismiss", "allowed")]


class TestTheRouteRecordsTheDismissalInTheOwningSessionsLog:
    """The record the PANEL reads, written by the route that accepts the dismissal.

    The folder registry below it is kept for ``GET /api/spawn``, which reads the
    folders for the run's own output text. This half is what keeps the card gone,
    because the panel's durable source is a fold of the session's crew log.
    """

    def delete(self, agent_id: str, slots: dict | None = None):
        response = asyncio.get_event_loop().run_until_complete(
            messaging.api_spawn_delete(_DeleteRequest(agent_id, "", slots))
        )
        return response.status, json.loads(response.text or "{}")

    @pytest.fixture(autouse=True)
    def _crew_log_on(self, monkeypatch):
        """The suite runs with the crew log OFF, so a test opts in.

        This class is about the entry the route writes, so the record has to be
        switched on for it. The opt-OUT is behaviour of its own and is pinned by
        the last test here rather than left to this fixture's absence.
        """
        monkeypatch.setenv("KIROCREW_CREW_LOG", "1")
        yield

    def _session_with_child(self, unit_id: str, agent_id: str, slot: str = "chat-1"):
        """One crew-log UNIT holding a finished child, with *slot* in its header.

        The unit id is an ACP session id and is deliberately unlike the slot and
        its session key: the route resolves the unit by searching the units whose
        headers name the slot, which a unit id that happened to BE the session key
        would not exercise.
        """
        from kiro_crew import crew_log as lg
        from kiro_crew.crew_log import CrewLog

        handle = CrewLog.create(
            lg.KIND_SESSION, unit_id, owner="raymond", agent="kirocrew", slot=slot
        )
        handle.append("subagent/spawned", {"agent_id": agent_id}, src="gateway")
        handle.append("subagent/completed", {"agent_id": agent_id, "ms": 5}, src="gateway")
        return handle

    def _drawn(self, unit_id: str) -> list[str]:
        from kiro_crew.crew_log import emit
        from kiro_crew.crew_log import projection as crew_log

        # The emitter QUEUES its appends, so the route's entry has to reach the file
        # before the fold is read: an unflushed write would leave the row drawn and
        # read as a dismissal that did not work, or -- worse, in the other
        # direction -- let a later flush make a failing assertion pass.
        emit.flush()
        value = crew_log.fold_session(unit_id, names=("subagents",)).projection("subagents").value
        return sorted(value["by_id"])

    def test_a_run_with_no_folder_is_dismissed_through_the_log(self, agent_root, monkeypatch):
        """The case the folder registry cannot answer at all.

        It refuses to record without a folder, which is the state every run reaches
        once its folder is reclaimed -- and the log still carries the child, so the
        card came back. The route writes the entry, and the fold stops drawing it.
        """
        monkeypatch.setattr(
            messaging, "_sel", lambda: SimpleNamespace(log_api_access=lambda **_: None)
        )
        self._session_with_child("acp-nf", "nofolder1")
        assert self._drawn("acp-nf") == ["nofolder1"]

        slots = {"chat-1": SimpleNamespace(key="chat-1", linked_session_key="")}
        status, body = self.delete("nofolder1", slots)
        assert (status, body["dismissed"]) == (200, True)
        assert self._drawn("acp-nf") == []

    def test_an_id_no_log_and_no_folder_knows_is_still_not_found(self, agent_root, monkeypatch):
        """Neither record can be written, so there is no run here to speak of."""
        monkeypatch.setattr(
            messaging, "_sel", lambda: SimpleNamespace(log_api_access=lambda **_: None)
        )
        self._session_with_child("acp-other", "other1")
        slots = {"chat-1": SimpleNamespace(key="chat-1", linked_session_key="")}
        assert self.delete("never-ran", slots)[0] == 404

    def test_a_child_of_no_live_slots_session_is_not_resolved(self, agent_root, monkeypatch):
        """The resolver is bounded by the live slots, like the panel read.

        A child whose session is not among them is not one the panel could be
        drawing from the log either, so there is no folded card to clear.
        """
        monkeypatch.setattr(
            messaging, "_sel", lambda: SimpleNamespace(log_api_access=lambda **_: None)
        )
        self._session_with_child("acp-offstage", "offstage1", slot="chat-9")
        slots = {"chat-1": SimpleNamespace(key="chat-1", linked_session_key="")}
        assert self.delete("offstage1", slots)[0] == 404
        assert self._drawn("acp-offstage") == ["offstage1"]

    def test_an_unwritable_folder_registry_is_reported_even_when_the_log_arm_wrote(
        self, agent_root, monkeypatch
    ):
        """One reader honours the dismissal and the other does not, so it is partial.

        ``GET /api/spawn`` reads the folders, so an unwritable registry leaves that
        listing offering the run however well the log arm did. Answering ok claims
        a dismissal one reader goes on contradicting, with nothing anywhere saying
        the write failed -- so the caller cannot retry the half that did not land.
        """
        denied: list[dict] = []
        monkeypatch.setattr(
            messaging,
            "_sel",
            lambda: SimpleNamespace(log_api_access=lambda **kw: denied.append(kw)),
        )
        self._session_with_child("acp-partial", "partial1")
        monkeypatch.setattr(
            messaging,
            "record_panel_dismissal_outcome",
            lambda agent_id: subagent_persistence.DISMISSAL_FAILED,
        )

        slots = {"chat-1": SimpleNamespace(key="chat-1", linked_session_key="")}
        status, body = self.delete("partial1", slots)
        assert (status, body["code"]) == (503, "dismissal_unwritable")
        assert [(e["operation"], e["outcome"]) for e in denied] == [("spawn.dismiss", "denied")]
        # And the log arm's own write still happened: the half that CAN land does,
        # so a retry has less to do rather than more.
        assert self._drawn("acp-partial") == []

    def test_an_append_that_never_commits_is_not_published_as_a_dismissal(
        self, agent_root, monkeypatch
    ):
        """The queue returns on handover, so handover is not the record.

        With the run's folder already reclaimed this entry is the ONLY record of
        the dismissal. Reporting success on the handover tells the user the card is
        gone and lets the next reconnect bring it back, with nothing left to
        explain it and nothing for a retry to act on.
        """
        monkeypatch.setattr(
            messaging, "_sel", lambda: SimpleNamespace(log_api_access=lambda **_: None)
        )
        self._session_with_child("acp-drop", "dropped1")

        def _never_commits(unit, *, agent_id, on_settled=None):
            """Queued, then given up on -- the writer's permanent-drop outcome."""
            if on_settled is not None:
                on_settled(False)

        monkeypatch.setattr(messaging_crew_log_emit(), "on_subagent_dismissed", _never_commits)
        monkeypatch.setattr(messaging_crew_log_emit(), "dismiss_child", lambda agent_id, **_: "")

        slots = {"chat-1": SimpleNamespace(key="chat-1", linked_session_key="")}
        status, body = self.delete("dropped1", slots)
        # 503, not 404: a unit holds this child, so the run exists. A 404 would say
        # it never did, which is the answer that makes the card undismissable.
        assert (status, body["code"]) == (503, "dismissal_unwritable")
        assert self._drawn("acp-drop") == ["dropped1"]

    def test_a_committed_append_is_published(self, agent_root, monkeypatch):
        """Control: the gate must not refuse the dismissal that did land."""
        monkeypatch.setattr(
            messaging, "_sel", lambda: SimpleNamespace(log_api_access=lambda **_: None)
        )
        self._session_with_child("acp-ok", "ok1")
        slots = {"chat-1": SimpleNamespace(key="chat-1", linked_session_key="")}

        status, body = self.delete("ok1", slots)
        assert (status, body["dismissed"]) == (200, True)
        assert self._drawn("acp-ok") == []

    def test_a_failed_unit_search_answers_retryable_not_not_found(self, agent_root, monkeypatch):
        """ABSENT would become a 404 or a plain success, both of them wrong.

        The run exists as far as anyone knows; the store simply would not say where
        its row is. A 404 tells the caller the run never existed, and a success
        tells it the card is cleared, so the only honest answer is retryable.
        """
        monkeypatch.setattr(
            messaging, "_sel", lambda: SimpleNamespace(log_api_access=lambda **_: None)
        )
        from kiro_crew.crew_log import resolve as crew_log_resolve

        self._session_with_child("acp-nosay", "nosay1")

        def _boom(*_a, **_kw):
            raise crew_log_resolve.UnitSearchFailed("the store is unreadable")

        monkeypatch.setattr(crew_log_resolve, "unit_holding_child", _boom)
        monkeypatch.setattr(messaging_crew_log_emit(), "dismiss_child", lambda agent_id, **_: "")

        slots = {"chat-1": SimpleNamespace(key="chat-1", linked_session_key="")}
        status, body = self.delete("nosay1", slots)
        assert (status, body["code"]) == (503, "dismissal_unwritable")

    def test_an_unexpected_fault_in_the_log_arm_answers_retryable(self, agent_root, monkeypatch):
        """The catch-all is a store fault too, so it cannot read as "nothing owed".

        Whatever reaches it is an emitter or store problem, and ABSENT would become
        a 404 for a run that exists or a plain success for a card still drawn. The
        one case that genuinely owes no record, a switched-off emitter, is decided
        before anything here can fail.
        """
        monkeypatch.setattr(
            messaging, "_sel", lambda: SimpleNamespace(log_api_access=lambda **_: None)
        )
        self._session_with_child("acp-fault", "fault1")

        def _boom(*_a, **_kw):
            raise ValueError("something nobody anticipated")

        monkeypatch.setattr(messaging_crew_log_emit(), "dismiss_child", _boom)

        slots = {"chat-1": SimpleNamespace(key="chat-1", linked_session_key="")}
        status, body = self.delete("fault1", slots)
        assert (status, body["code"]) == (503, "dismissal_unwritable")

    def test_with_the_crew_log_switched_off_the_folder_record_is_all_there_is(
        self, agent_root, monkeypatch
    ):
        """An install that opted out of the record has no record to write to.

        The folder registry still answers on its own terms, which is what the
        listing endpoint reads anyway -- so the opt-out costs the panel its durable
        half rather than breaking the route.
        """
        monkeypatch.setenv("KIROCREW_CREW_LOG", "0")
        monkeypatch.setattr(
            messaging, "_sel", lambda: SimpleNamespace(log_api_access=lambda **_: None)
        )
        write_record(agent_root, "withfolder1")
        slots = {"chat-1": SimpleNamespace(key="chat-1", linked_session_key="")}
        status, body = self.delete("withfolder1", slots)
        assert (status, body["dismissed"]) == (200, True)
        assert dismissed_panel_ids() == frozenset({"withfolder1"})


class TestTheLiveManagerPathRecordsTheDismissalToo:
    """The dismiss route has TWO arms, and both have to reach the same records.

    A finished child the manager still holds goes through
    ``settle_before_delete``; one the manager has already dropped goes through the
    route's own durable arm. The panel folds the crew log for both, so an arm that
    writes only the folder registry clears the card from live state and leaves the
    fold still
    offering it -- the card returns on the very next reconnect, inside the same
    process, with the user having been told the dismissal worked.
    """

    @pytest.fixture(autouse=True)
    def _crew_log_on(self, monkeypatch):
        """The crew log on, and the emitter's spawn pin clear.

        ``_child_origin`` is a module global that outlives one test, so a pin left
        by an earlier test would decide a later one -- and the two cases here
        differ by exactly whether a pin exists. Cleared both sides, so neither
        order hides a failure.
        """
        from kiro_crew.crew_log import emit

        monkeypatch.setenv("KIROCREW_CREW_LOG", "1")
        emit.forget_child_origin("live1")
        yield
        emit.forget_child_origin("live1")

    def manager(self):
        return SimpleNamespace(
            _agents={
                "live1": SimpleNamespace(
                    id="live1",
                    _ending_claimed=False,
                    done=True,
                    parent_session_key="dashboard:chat-1",
                )
            },
            _tasks={"live1": object()},
            _report_owners={},
        )

    def settle(self, manager):
        import kiro_crew.subagent as subagent_module

        return asyncio.run(subagent_module.SubagentManager.settle_before_delete(manager, "live1"))

    def _session_with_child(self, unit_id: str, agent_id: str, slot: str = "chat-1"):
        """A dispatched child, closed through the REAL terminal report.

        The terminal report is what releases the emitter's spawn pin, in a
        ``finally`` that always runs -- so by the time a finished child reaches
        ``settle_before_delete`` there is no pin, and a test that closed the child
        by calling ``on_subagent_completed`` directly leaves one standing and
        proves nothing about production. Driving the real reporter is what makes
        this the live arm's own case.
        """
        from kiro_crew import crew_log as lg
        from kiro_crew.crew_log import CrewLog, emit
        from kiro_crew.subagent_manager.terminal import TerminalCoordinator

        CrewLog.create(lg.KIND_SESSION, unit_id, owner="raymond", agent="kirocrew", slot=slot)
        emit.remember_child_origin(agent_id, unit_id, 1)
        sid, turn = emit.open_child_origin(agent_id)
        emit.on_subagent_spawned(sid, turn, agent_id=agent_id)
        info = SimpleNamespace(
            id=agent_id,
            outcome="completed",
            error=None,
            user_stopped=False,
            elapsed=0.005,
            credits=0.0,
        )
        TerminalCoordinator._record_crew_log_terminal(SimpleNamespace(), info)
        assert emit.flush()
        assert emit.child_origin(agent_id) == ("", 0), (
            "the terminal report left the spawn pin standing, so this test is not the "
            "state a finished child is actually in"
        )

    def _drawn(self, unit_id: str) -> list[str]:
        from kiro_crew.crew_log import emit
        from kiro_crew.crew_log import projection as crew_log

        assert emit.flush()
        value = crew_log.fold_session(unit_id, names=("subagents",)).projection("subagents").value
        return sorted(value["by_id"])

    def test_the_fold_stops_offering_a_card_the_live_arm_dismissed(self, agent_root, monkeypatch):
        self._session_with_child("acp-live", "live1")
        write_record(agent_root, "live1")
        assert self._drawn("acp-live") == ["live1"]

        manager = self.manager()
        assert self.settle(manager) == "delivered"
        assert "live1" not in manager._agents
        assert self._drawn("acp-live") == []
        # And the folder half is written too, so the listing endpoint agrees.
        assert dismissed_panel_ids() == frozenset({"live1"})

    def test_a_child_no_unit_holds_does_not_hold_the_pop(self, agent_root, monkeypatch):
        """Nothing to suppress is not a failure to suppress.

        A child whose dispatch this slot's logs never recorded has no row for the
        fold to offer, so there is no folded card to clear -- and holding the pop
        on it would answer 409 for every such dismissal, which is undismissable
        rather than safe. The folder record still answers for the listing.
        """
        write_record(agent_root, "live1")  # a folder, and no unit holding the row
        manager = self.manager()
        assert self.settle(manager) == "delivered"
        assert "live1" not in manager._agents
        assert dismissed_panel_ids() == frozenset({"live1"})

    def test_an_unstorable_folder_record_still_holds_the_pop(self, agent_root, monkeypatch):
        """The half that CAN fail still gates the publish.

        The folder write is a synchronous file write and can genuinely fail, and
        the listing reads that record -- so a pop on a failed write reports a
        dismissal the listing will not honour.
        """
        import kiro_crew.subagent as subagent_module

        self._session_with_child("acp-live", "live1")
        monkeypatch.setattr(
            subagent_module,
            "record_panel_dismissal_outcome",
            lambda agent_id: subagent_persistence.DISMISSAL_FAILED,
        )
        manager = self.manager()
        assert self.settle(manager) == "pending"
        assert "live1" in manager._agents

    def test_an_append_that_never_commits_holds_the_pop(self, agent_root, monkeypatch):
        """The pop is the publish, on this arm as much as on the route's.

        The writer can refuse the entry at its memory ceiling, and the queue has
        already returned by then. A pop on that reports a dismissal to the route as
        200 and leaves the fold still offering the card, so it comes back on the
        very next reconnect -- which is the same published-too-early failure the
        folder write above this is deliberately ordered to avoid.
        """
        self._session_with_child("acp-live-drop", "live1")
        write_record(agent_root, "live1")

        def _never_commits(unit, *, agent_id, on_settled=None):
            if on_settled is not None:
                on_settled(False)

        monkeypatch.setattr(messaging_crew_log_emit(), "on_subagent_dismissed", _never_commits)
        manager = self.manager()
        assert self.settle(manager) == "pending"
        assert "live1" in manager._agents
        assert "live1" in manager._tasks
        # And the folder half was never written either, so a retry starts clean
        # rather than from a half-recorded dismissal.
        assert dismissed_panel_ids() == frozenset()

    def test_a_failed_unit_search_holds_the_pop(self, agent_root, monkeypatch):
        """A store that would not say is not a store that said no.

        Popping on it reports a dismissal that was never looked for, and the card
        returns once the store recovers -- with the user having been told it worked.
        """
        from kiro_crew.crew_log import resolve as crew_log_resolve

        self._session_with_child("acp-live-search", "live1")
        write_record(agent_root, "live1")

        def _boom(*_a, **_kw):
            raise crew_log_resolve.UnitSearchFailed("the store is unreadable")

        monkeypatch.setattr(crew_log_resolve, "unit_holding_child", _boom)
        manager = self.manager()
        assert self.settle(manager) == "pending"
        assert "live1" in manager._agents
        assert dismissed_panel_ids() == frozenset()

    def test_with_the_crew_log_off_the_live_arm_still_dismisses(self, agent_root, monkeypatch):
        """The opt-out must not make every managed card undismissable."""
        monkeypatch.setenv("KIROCREW_CREW_LOG", "0")
        write_record(agent_root, "live1")
        manager = self.manager()
        assert self.settle(manager) == "delivered"
        assert "live1" not in manager._agents
        assert dismissed_panel_ids() == frozenset({"live1"})
