"""Pressing Resume on the person's behalf (``dashboard.resume_interrupted_turns``).

The boot pass resumes every restored chat whose metadata line said the previous
process died with a turn admitted and whose tail reads as interrupted; the error
pass arms one backed-off retry for a turn that ended in a retryable error. Both go through ``continue_slot_turn``, both spend a
per-chat budget that is persisted on the metadata line and reset only by a
person, and neither restores any approval mode: the chat is TOLD its mode
changed instead (rfc-restart-resume).
"""

from __future__ import annotations

import asyncio
import inspect
import json
import time
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state
from dashboard_owner_helpers import as_owner

from kiro_crew.dashboard import restart_resume
from kiro_crew.dashboard.chat_handlers import SlotContinueRefusal, continue_slot_turn
from kiro_crew.dashboard.chat_utils import (
    _AUTO_RESUME_ERROR_MSG,
    _AUTO_RESUME_RESTART_MSG,
    _MANUAL_RESUME_MSG,
    APP_AGENT_REFUSED_KIND,
    AUTH_REQUIRED_KIND,
    CONTEXT_OVERFLOW_KIND,
    MODEL_UNENTITLED_KIND,
    RETRY_INVITED_KIND,
    SESSION_START_FAILED_KIND,
    SYNTHETIC_RECOVERY_KIND,
    TRANSIENT_NOTICE_GIVE_UP,
    TRANSIENT_NOTICE_META_KEY,
    TRANSIENT_RETRY_KIND,
    USAGE_LIMIT_KIND,
)
from kiro_crew.dashboard.restart_resume import TURN_FAILED_META
from kiro_crew.dashboard.slot_persistence import metadata_codec as codec
from kiro_crew.dashboard.slot_persistence.turn_marker import (
    _CLOSE_INTERRUPTION_KIND,
    _RESTART_INTERRUPTION_KIND,
)
from kiro_crew.dashboard.state import _ChatSlot, is_turn_interrupted
from kiro_crew.dashboard.system_notices import (
    APPROVAL_MODE_DROPPED_KIND,
    AUTO_RESUME_PAUSED_KIND,
    SYSTEM_NOTICE_KINDS,
    is_system_notice,
)
from kiro_crew.dashboard.turn_dispatch import (
    TURN_TIMEOUT_KIND,
    format_approval_no_budget_card,
    format_approval_timeout_card,
)
from kiro_crew.history import SLOT_OWNED_META_KEYS
from kiro_crew.members import DM_SLOT_MODE

_RESTART_ROW = {"kind": _RESTART_INTERRUPTION_KIND}
#: The runner's own terminal "please retry" row -- the generic retryable error.
_RETRY_INVITED_ROW = {"kind": RETRY_INVITED_KIND}


def _cfg(enabled: bool = True, limit: int = 5, window: int = 60) -> SimpleNamespace:
    return SimpleNamespace(
        dashboard=SimpleNamespace(
            resume_interrupted_turns=enabled,
            resume_retry_limit=limit,
            resume_retry_window_minutes=window,
        )
    )


def _slot(name: str = "s") -> _ChatSlot:
    return _ChatSlot(name)


def _cut_off(
    slot: _ChatSlot, *, kind: dict | None = _RESTART_ROW, marker: bool = True
) -> _ChatSlot:
    """A turn the restart cut: a user row, a partial reply, the interruption row.

    *marker* is what the restore reads off the metadata line (the previous process
    admitted a turn it never finished) -- set for the boot pass's candidates, and
    deliberately NOT set for a chat whose row an earlier boot left behind.
    """
    slot.append("user", "do the thing", "msg msg-u")
    slot.append("assistant", "Starting on it…", "msg msg-a")
    slot.append(
        "error", "This turn was interrupted when the app restarted.", "msg msg-err", meta=kind
    )
    slot._restart_cut_turn = marker
    return slot


def _stop_card() -> str:
    return json.dumps({"kind": "stop_event"})


def _mock_state(*slots: _ChatSlot) -> MagicMock:
    state = MagicMock()
    state._slots = {s.key: s for s in slots}
    state._background_tasks = set()
    state.push_slots_update = MagicMock()
    state.notify = MagicMock()
    state.subagents = None
    return state


@pytest.fixture(name="quiet_dispatch")
def quiet_dispatch_fixture(monkeypatch):
    """Stub the real dispatcher: record each continue_slot_turn call, start nothing.

    Declared with an explicit ``name=`` so ``test_restart_resume_budget.py`` can
    import ``quiet_dispatch_fixture`` and its tests request ``quiet_dispatch``
    without the import colliding with the parameter (flake8 F811).
    """
    calls: list[dict] = []

    async def fake_continue(state, slot, **kwargs):
        calls.append({"slot": slot.key, **kwargs})
        # continue_slot_turn's contract: ``before_dispatch`` runs under the lock
        # right before the queue insert, and the drain lands the body as an
        # ``inject`` row carrying the entry's meta plus its ``injectKind``.
        if kwargs.get("before_dispatch") is not None:
            kwargs["before_dispatch"]()
        slot.append(
            "inject",
            kwargs["body"],
            "",
            meta={**(kwargs.get("extra_meta") or {}), "injectKind": "recovery"},
        )
        return "resumed"

    async def no_pause(group):
        return None

    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.continue_slot_turn", fake_continue)
    monkeypatch.setattr("kiro_crew.dashboard.cautious_boot.pause_before", no_pause)
    monkeypatch.setattr(restart_resume, "BOOT_SETTLE_SECS", 0.0)
    monkeypatch.setattr(restart_resume, "BOOT_STAGGER_SECS", 0.0)
    monkeypatch.setattr(restart_resume, "_audit", lambda *a, **k: None)
    # No process config watcher in a test: the error pass reads the patched loader.
    monkeypatch.setattr(restart_resume, "_switch_on_now", lambda: None)
    return calls


# ── Which tails qualify ─────────────────────────────────────────────────────────


class TestClassifier:
    def test_restart_row_is_a_restart_candidate_and_not_an_error_one(self):
        slot = _cut_off(_slot())

        assert restart_resume.restart_candidate(slot) is True
        assert restart_resume.error_retry_candidate(slot.messages) is False

    def test_a_cut_turn_the_marker_names_qualifies_whatever_its_tail(self):
        # The restore appends the interruption row only when the tail does not
        # already prove the interruption: an unanswered user row, a cut recovery
        # or nudge turn, a turn cut before any reply. The marker is the signal.
        opener_only = _slot("opener")
        opener_only.append("user", "do the thing", "msg msg-u")
        opener_only._restart_cut_turn = True
        tool_tail = _slot("tool")
        tool_tail.append("user", "do the thing", "msg msg-u")
        tool_tail.append("tool", "ran", "msg msg-tool")
        tool_tail._restart_cut_turn = True
        recovery_tail = _slot("recovery")
        recovery_tail.append("user", "do the thing", "msg msg-u")
        recovery_tail.append("assistant", "Starting…", "msg msg-a")
        recovery_tail.append(
            "inject",
            "[Interrupted turn — automatic recovery]\nbody",
            "",
            meta={"injectKind": "recovery"},
        )
        recovery_tail._restart_cut_turn = True

        for slot in (opener_only, tool_tail, recovery_tail):
            assert restart_resume.restart_candidate(slot) is True, slot.key

    def test_an_interruption_row_an_earlier_boot_left_is_not_this_boots(self):
        # Same transcript, no marker on the line: nothing was cut THIS restart.
        slot = _cut_off(_slot(), marker=False)

        assert restart_resume.restart_candidate(slot) is False

    def test_a_marked_turn_that_ended_in_another_error_was_failing_before_the_restart(self):
        slot = _cut_off(_slot(), kind={"kind": AUTH_REQUIRED_KIND})

        assert restart_resume.restart_candidate(slot) is False

    @pytest.mark.parametrize(
        "meta",
        [
            {TRANSIENT_NOTICE_META_KEY: TRANSIENT_NOTICE_GIVE_UP},
            {"kind": SESSION_START_FAILED_KIND},
            {"kind": RETRY_INVITED_KIND},
        ],
        ids=["transient_give_up", "session_start_failed", "retry_invited"],
    )
    def test_retryable_error_tails(self, meta):
        slot = _cut_off(_slot(), kind=meta, marker=False)

        assert restart_resume.error_retry_candidate(slot.messages) is True
        assert restart_resume.restart_candidate(slot) is False

    @pytest.mark.parametrize(
        "kind",
        [
            TURN_TIMEOUT_KIND,
            AUTH_REQUIRED_KIND,
            MODEL_UNENTITLED_KIND,
            USAGE_LIMIT_KIND,
            CONTEXT_OVERFLOW_KIND,
            APP_AGENT_REFUSED_KIND,
            _CLOSE_INTERRUPTION_KIND,
            "some_kind_added_next_year",
        ],
    )
    def test_never_retried_kinds(self, kind):
        slot = _cut_off(_slot(), kind={"kind": kind})

        assert restart_resume.error_retry_candidate(slot.messages) is False
        assert restart_resume.restart_candidate(slot) is False

    @pytest.mark.parametrize(
        ("content", "meta"),
        [
            (format_approval_timeout_card(600), None),
            (format_approval_no_budget_card(), None),
            ("Session stuck — please start a new chat.", None),
            ("⚠️ This message failed before it started.", {TURN_FAILED_META: True}),
            # The words invite a retry but no writer of this gateway tagged the
            # row: a row an older gateway persisted, or a writer added without
            # the tag. The list is closed on the KIND, never on the wording.
            ("⟳ Connection lost — please retry.", None),
            ("⟳ Connection lost — please retry.", {"kind": "some_kind_added_next_year"}),
            ("An error nobody classified.", None),
        ],
        ids=[
            "approval_timeout",
            "approval_no_budget",
            "session_stuck",
            "turn_failed",
            "untagged_please_retry",
            "unknown_kind_please_retry",
            "untagged_generic",
        ],
    )
    def test_rows_off_the_retryable_list_are_never_retried(self, content, meta):
        slot = _slot()
        slot.append("user", "do the thing", "msg msg-u")
        slot.append("error", content, "msg msg-err", meta=meta)

        assert restart_resume.error_retry_candidate(slot.messages) is False

    def test_the_retryable_list_names_only_structural_kinds(self):
        # The closed list the description promises: every member is a kind a
        # writer stamps on purpose, so there is no "everything else" entry and no
        # wording to match.
        assert restart_resume.RETRYABLE_ERROR_KINDS == {
            SESSION_START_FAILED_KIND,
            RETRY_INVITED_KIND,
        }
        assert None not in restart_resume.RETRYABLE_ERROR_KINDS
        assert not hasattr(restart_resume, "NEVER_RETRY_TEXT_PREFIXES")

    def test_a_clean_completion_is_neither(self):
        slot = _slot()
        slot.append("user", "hi", "msg msg-u")
        slot.append("assistant", "done.", "msg msg-a")
        slot._restart_cut_turn = True

        assert restart_resume.restart_candidate(slot) is False
        assert restart_resume.error_retry_candidate(slot.messages) is False

    def test_a_stop_card_ends_the_turn_on_purpose(self):
        slot = _slot()
        slot.append("user", "hi", "msg msg-u")
        slot.append("system", _stop_card(), _stop_card())
        slot._restart_cut_turn = True

        assert restart_resume.restart_candidate(slot) is False
        assert restart_resume.error_retry_candidate(slot.messages) is False

    def test_a_give_up_row_written_after_a_stop_is_the_persons_stop(self):
        # The retry ladder writes its give-up row when a Stop lands during the
        # backoff; the Stop card is older than the row but in the same turn.
        slot = _slot()
        slot.append("user", "hi", "msg msg-u")
        slot.append("system", _stop_card(), _stop_card())
        slot.append(
            "error",
            "⟳ Connection unstable — please try again.",
            "msg msg-err",
            meta={TRANSIENT_NOTICE_META_KEY: TRANSIENT_NOTICE_GIVE_UP},
        )

        assert restart_resume.error_retry_candidate(slot.messages) is False

    def test_a_held_note_flushed_after_the_error_does_not_hide_it(self):
        # The runner flushes held notes (untagged inject rows) at turn end, before
        # the error pass reads the tail. The note is not a turn: the error is
        # still how the newest turn ended.
        slot = _cut_off(_slot(), kind=_RETRY_INVITED_ROW, marker=False)
        slot.append("inject", "a note the person left for the next turn", "", meta={})

        assert restart_resume.error_retry_candidate(slot.messages) is True

    def test_an_untagged_inject_after_an_old_error_does_not_make_a_restart_candidate(self):
        # Same shape on the boot pass: an untagged inject row is not a turn the
        # restart could have cut, so an older unrelated error stays what it is.
        slot = _cut_off(_slot(), kind={"kind": AUTH_REQUIRED_KIND})
        slot.append("inject", "a note", "", meta={})

        assert restart_resume.restart_candidate(slot) is False

    def test_a_dispatching_inject_opens_a_turn(self):
        # ...whereas an inject that dispatched a turn (a cron delivery) is one.
        slot = _cut_off(_slot(), kind=_RETRY_INVITED_ROW, marker=False)
        slot.append("inject", "cron: do the nightly thing", "", meta={"injectKind": "cron"})

        assert restart_resume.error_retry_candidate(slot.messages) is False

    def test_an_older_error_behind_a_finished_turn_does_not_count(self):
        slot = _cut_off(_slot(), kind={TRANSIENT_NOTICE_META_KEY: TRANSIENT_NOTICE_GIVE_UP})
        slot.append("user", "try again", "msg msg-u")
        slot.append("assistant", "done.", "msg msg-a")

        assert restart_resume.error_retry_candidate(slot.messages) is False

    def test_a_restart_row_behind_an_unanswered_follow_up_is_still_this_restarts(self):
        # The previous process admitted a follow-up after an older interruption row
        # and died on it: the newest error is still that older restart row, but the
        # newest turn is the follow-up, and the marker says it was admitted.
        slot = _cut_off(_slot())
        slot.append("user", "are you there?", "msg msg-u")

        assert restart_resume.restart_candidate(slot) is True  # cut, by the marker
        # ...which is right: that follow-up IS a turn the restart cut, so it is
        # resumed, not dropped.

    def test_a_restart_during_the_ladders_wait_is_this_restarts(self):
        # The in-turn retry ladder writes a PENDING error row (transient_retry) and
        # waits for its backoff with the turn marker still set. A restart during
        # that wait appends no restart row (the tail already reads as interrupted),
        # so the pending row is the newest error -- and it is not how the turn
        # ended. The boot pass resumes it.
        slot = _slot()
        slot.append("user", "do the thing", "msg msg-u")
        slot.append(
            "error",
            "⟳ Connection unstable — retrying…",
            "msg msg-err",
            meta={"kind": TRANSIENT_RETRY_KIND, TRANSIENT_NOTICE_META_KEY: "transient_retrying"},
        )
        slot._restart_cut_turn = True

        assert restart_resume.restart_candidate(slot) is True
        # The same shape after the ladder's own replay of the user's words.
        slot.append(
            "inject", "do the thing", "", meta={"injectKind": "user_replay", "recovery": True}
        )
        slot.append(
            "error",
            "⟳ Connection unstable — retrying…",
            "msg msg-err",
            meta={"kind": TRANSIENT_RETRY_KIND},
        )
        assert restart_resume.restart_candidate(slot) is True
        # The error pass never owns a pending row: the ladder does, or the boot pass.
        assert restart_resume.error_retry_candidate(slot.messages) is False

    def test_a_restricted_chat_is_never_eligible(self):
        for mode in ("incognito", "temporary"):
            slot = _cut_off(_slot(mode))
            slot.memory_mode = mode
            assert restart_resume._eligible_slot(slot) is False, mode
        assert restart_resume._eligible_slot(_cut_off(_slot("plain"))) is True

    def test_an_app_owned_chat_is_the_apps_to_continue(self):
        owned = _cut_off(_slot("owned"))
        owned._app = "some-app"

        assert restart_resume._eligible_slot(owned) is False


# ── The persisted budget ────────────────────────────────────────────────────────


class TestBudget:
    def test_stamps_are_written_to_the_line_and_read_back(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        source = state.get_or_create_slot("src")
        source._auto_resume_stamps = [1700000000.0, 1700000100.0]

        line = codec.encode(source, folds=codec.SaveFolds(memory_mode=source.memory_mode))
        assert line["auto_resume_stamps"] == [1700000000.0, 1700000100.0]
        assert "auto_resume_stamps" in SLOT_OWNED_META_KEYS

        target = state.get_or_create_slot("dst")
        codec.apply(
            state, target, {"_type": "metadata", **line}, codec.Restore(name="dst", member=None)
        )
        assert target._auto_resume_stamps == [1700000000.0, 1700000100.0]

    @pytest.mark.parametrize(
        "raw",
        [
            "garbage",
            [1.0] * (restart_resume.MAX_STAMPS + 1),
            [1.0, "x"],
            [float("inf")],
            [1e12],
            [True],
        ],
        ids=["string", "oversize", "mixed", "inf", "future", "bool"],
    )
    def test_a_malformed_list_reads_as_full_and_invalid(self, raw):
        now = 1_700_000_000.0

        stamps, valid = restart_resume.read_persisted_stamps(raw, now=now)

        assert valid is False
        assert len(stamps) == restart_resume.MAX_STAMPS
        assert all(s == now for s in stamps)

    def test_an_integer_too_large_for_a_float_reads_as_full_not_as_a_crash(self):
        # float() refuses an int past the double range with OverflowError. The read
        # runs inside the slot restore, where a raise makes the chat impossible to
        # open; a tampered line must fail CLOSED for the automation instead.
        now = 1_700_000_000.0

        stamps, valid = restart_resume.read_persisted_stamps([10**400], now=now)

        assert valid is False
        assert stamps == [now] * restart_resume.MAX_STAMPS

    def test_a_tampered_line_still_lets_the_chat_open(self, tmp_path, monkeypatch):
        # Through the real codec read: the restore completes, the manual Resume is
        # untouched, only the automation is refused.
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("tampered")
        meta = {"_type": "metadata", "auto_resume_stamps": [10**400]}

        codec.apply(state, slot, meta, codec.Restore(name="tampered", member=None))

        assert len(slot._auto_resume_stamps) == restart_resume.MAX_STAMPS

    def test_a_good_list_reads_back_and_none_reads_empty(self):
        now = 1_700_000_000.0

        assert restart_resume.read_persisted_stamps([now - 5, now - 1], now=now) == (
            [now - 5, now - 1],
            True,
        )
        assert restart_resume.read_persisted_stamps(None, now=now) == ([], True)

    def test_a_malformed_line_is_named_in_the_log(self, tmp_path, monkeypatch, caplog):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("odd")

        with caplog.at_level("WARNING"):
            codec.apply(
                state,
                slot,
                {"_type": "metadata", "auto_resume_stamps": "garbage"},
                codec.Restore(name="odd", member=None),
            )

        assert len(slot._auto_resume_stamps) == restart_resume.MAX_STAMPS
        assert any(
            "odd" in r.getMessage() and "auto_resume_stamps" in r.getMessage()
            for r in caplog.records
        )

    def test_window_prunes_old_stamps(self):
        slot = _slot()
        now = 1_700_000_000.0
        slot._auto_resume_stamps = [now - 4000, now - 100, now - 10]

        used = restart_resume.budget_used(slot, window_secs=3600, now=now)

        assert used == 2
        assert slot._auto_resume_stamps == [now - 100, now - 10]

    def test_a_stamp_exactly_one_window_old_still_counts(self):
        slot = _slot()
        now = 1_700_000_000.0
        slot._auto_resume_stamps = [now - 3600.0, now - 3600.01]

        assert restart_resume.budget_used(slot, window_secs=3600, now=now) == 1
        assert slot._auto_resume_stamps == [now - 3600.0]

    def test_reset_clears_stamps_cancels_the_armed_retry_and_moves_the_epoch(self):
        slot = _cut_off(_slot())
        slot._auto_resume_stamps = [time.time()]
        task = MagicMock()
        task.done.return_value = False
        slot._auto_resume_task = task
        epoch = slot._auto_resume_epoch

        restart_resume.reset_budget(slot)

        assert slot._auto_resume_stamps == []
        assert slot._auto_resume_task is None
        assert slot._restart_cut_turn is False
        assert slot._auto_resume_epoch == epoch + 1
        task.cancel.assert_called_once()

    def test_backoff_curve(self):
        rng = MagicMock()
        rng.random.return_value = 0.0

        delays = [restart_resume.backoff_secs(n, rng=rng) for n in range(1, 7)]

        assert delays == [30.0, 60.0, 120.0, 240.0, 480.0, 600.0]

    def test_settings_floor_at_the_config_bounds(self):
        enabled, limit, window = restart_resume._settings(_cfg(limit=0, window=1))

        assert (enabled, limit, window) == (True, 1, 5 * 60)


# ── The boot pass ──────────────────────────────────────────────────────────────


class TestBootPass:
    @pytest.mark.asyncio
    async def test_off_by_default_dispatches_nothing(self, quiet_dispatch):
        slot = _cut_off(_slot())
        state = _mock_state(slot)

        dispatched = await restart_resume.resume_after_restart(state, _cfg(enabled=False))

        assert dispatched == 0
        assert quiet_dispatch == []
        # The boot task still runs (the notice pass is always on); it dispatches nothing.
        assert await restart_resume.start_boot_pass(state, _cfg(enabled=False)) == 0
        assert quiet_dispatch == []

    @pytest.mark.asyncio
    async def test_resumes_only_the_chats_the_restart_cut_off(self, monkeypatch, quiet_dispatch):
        cut = _cut_off(_slot("cut"))
        clean = _slot("clean")
        clean.append("user", "hi", "msg msg-u")
        clean.append("assistant", "done.", "msg msg-a")
        failing = _cut_off(_slot("failing"), kind={"kind": AUTH_REQUIRED_KIND})
        stale = _cut_off(_slot("stale"), marker=False)  # an earlier boot's row
        loaded: list[str] = []

        async def fake_load(slot):
            loaded.append(slot.key)

        monkeypatch.setattr(restart_resume, "load_window_off_loop", fake_load)
        state = _mock_state(cut, clean, failing, stale)

        dispatched = await restart_resume.resume_after_restart(state, _cfg())

        assert dispatched == 1
        assert [c["slot"] for c in quiet_dispatch] == ["cut"]
        # Only chats the line flagged have their window loaded; a clean chat and
        # a stale row are never touched.
        assert loaded == ["cut", "failing"]
        call = quiet_dispatch[0]
        assert call["via"] == restart_resume.VIA_RESTART
        assert call["directive_user_origin"] is False
        assert call["require_interrupted"] is True  # a turn that finished meanwhile is refused
        assert call["body"] == _AUTO_RESUME_RESTART_MSG
        assert call["extra_meta"] == {"autoResume": {"cause": "restart", "attempt": 1, "limit": 5}}
        assert len(cut._auto_resume_stamps) == 1
        assert cut._dirty is True
        # This boot's reading is spent: a second pass in the same process re-reads nothing.
        assert cut._restart_cut_turn is False and failing._restart_cut_turn is False
        assert await restart_resume.resume_after_restart(state, _cfg()) == 0

    @pytest.mark.asyncio
    async def test_the_body_is_not_the_manual_one(self, quiet_dispatch):
        state = _mock_state(_cut_off(_slot()))

        await restart_resume.resume_after_restart(state, _cfg())

        assert quiet_dispatch[0]["body"] != _MANUAL_RESUME_MSG
        assert quiet_dispatch[0]["body"].splitlines()[0] == "[Continue — automatic after a restart]"

    @pytest.mark.asyncio
    async def test_a_refusal_costs_nothing(self, monkeypatch, quiet_dispatch):
        async def refusing(state, slot, **kwargs):
            raise SlotContinueRefusal("slot is running", "slot_running")

        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.continue_slot_turn", refusing)
        slot = _cut_off(_slot())
        state = _mock_state(slot)

        dispatched = await restart_resume.resume_after_restart(state, _cfg())

        assert dispatched == 0
        assert slot._auto_resume_stamps == []

    @pytest.mark.asyncio
    async def test_one_chats_failure_does_not_end_the_pass(self, monkeypatch, quiet_dispatch):
        calls: list[str] = []

        async def flaky(state, slot, **kwargs):
            calls.append(slot.key)
            if slot.key == "first":
                raise RuntimeError("boom")
            return "resumed"

        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.continue_slot_turn", flaky)
        state = _mock_state(_cut_off(_slot("first")), _cut_off(_slot("second")))

        assert await restart_resume.resume_after_restart(state, _cfg()) == 1
        assert calls == ["first", "second"]

    @pytest.mark.asyncio
    async def test_the_budget_survives_a_restart_and_pauses_at_the_limit(self, quiet_dispatch):
        slot = _cut_off(_slot())
        now = time.time()
        # Four earlier boots inside the window, as read back from the line.
        slot._auto_resume_stamps, _ = restart_resume.read_persisted_stamps(
            [now - 1800, now - 1200, now - 600, now - 60], now=now
        )
        state = _mock_state(slot)

        assert await restart_resume.resume_after_restart(state, _cfg(limit=5)) == 1
        assert len(slot._auto_resume_stamps) == 5
        # The fifth boot (a fresh process reads the marker again): spent. One
        # notice row, one notification, no dispatch.
        quiet_dispatch.clear()
        slot._restart_cut_turn = True
        assert await restart_resume.resume_after_restart(state, _cfg(limit=5)) == 0
        assert quiet_dispatch == []
        tail = slot.messages[-1]
        assert tail["role"] == "assistant"
        assert tail["meta"]["kind"] == AUTO_RESUME_PAUSED_KIND
        assert "its limit of 5 resumes in 1h is spent" in tail["content"]
        assert "Press Resume" in tail["content"]
        assert is_system_notice(tail["role"], tail["meta"])
        state.notify.assert_called_once()
        assert state.notify.call_args.args[0] == "agent"
        # A second boot at the limit does not stack a second notice.
        slot._restart_cut_turn = True
        assert await restart_resume.resume_after_restart(state, _cfg(limit=5)) == 0
        assert (
            sum(
                1
                for m in slot.messages
                if (m.get("meta") or {}).get("kind") == AUTO_RESUME_PAUSED_KIND
            )
            == 1
        )

    @pytest.mark.asyncio
    async def test_a_lowered_limit_is_quoted_not_mistaken_for_a_broken_record(self, quiet_dispatch):
        slot = _cut_off(_slot())
        now = time.time()
        slot._auto_resume_stamps = [now - 300, now - 200, now - 100]  # written under limit 5
        state = _mock_state(slot)

        assert await restart_resume.resume_after_restart(state, _cfg(limit=2)) == 0

        tail = slot.messages[-1]
        assert tail["meta"]["kind"] == AUTO_RESUME_PAUSED_KIND
        assert "3 resumes in the last 1h, over its limit of 2" in tail["content"]
        assert "could not be read" not in tail["content"]

    @pytest.mark.asyncio
    async def test_a_malformed_persisted_list_refuses_the_automation_only(self, quiet_dispatch):
        slot = _cut_off(_slot())
        slot._auto_resume_stamps, _ = restart_resume.read_persisted_stamps("garbage")
        state = _mock_state(slot)

        assert await restart_resume.resume_after_restart(state, _cfg()) == 0
        assert quiet_dispatch == []
        # The notice says why, and does not quote the sentinel count.
        tail = slot.messages[-1]
        assert tail["meta"]["kind"] == AUTO_RESUME_PAUSED_KIND
        assert "could not be read" in tail["content"]
        # The refusal is one window long (the sentinel stamps age out), and the
        # notice says so rather than promising a refusal that outlasts it.
        assert "for the next 1h" in tail["content"]
        assert "64 attempts" not in tail["content"]
        # The manual path is untouched: a person's Resume still goes through.
        assert is_turn_interrupted(slot.messages) is True

    @pytest.mark.asyncio
    async def test_the_pause_notice_is_one_per_budget_not_one_per_failed_turn(
        self, monkeypatch, quiet_dispatch
    ):
        monkeypatch.setattr(restart_resume, "_load_settings", lambda: (True, 1, 3600))

        async def no_backoff(delay):
            return None

        monkeypatch.setattr(restart_resume, "_backoff_sleep", no_backoff)
        slot = _cut_off(_slot(), kind=_RETRY_INVITED_ROW)
        state = _mock_state(slot)
        # The one resume the budget allows, then the budget is spent.
        assert restart_resume.schedule_after_error(state, slot) is True
        await asyncio.wait_for(slot._auto_resume_task, 30)
        assert len(quiet_dispatch) == 1

        # Unattended turns keep failing: a cron delivery, then its error, twice.
        for _ in range(2):
            slot.append("inject", "cron: nightly", "", meta={"injectKind": "cron"})
            slot.append("error", "it broke", "msg msg-err", meta=_RETRY_INVITED_ROW)
            assert restart_resume.schedule_after_error(state, slot) is True
            await asyncio.wait_for(slot._auto_resume_task, 30)

        pauses = [
            m for m in slot.messages if (m.get("meta") or {}).get("kind") == AUTO_RESUME_PAUSED_KIND
        ]
        assert len(pauses) == 1
        assert state.notify.call_count == 1
        # A person's Resume resets the budget; once the automation has resumed
        # again and the budget is spent again, a new notice is warranted.
        restart_resume.reset_budget(slot)
        slot.append("error", "it broke again", "msg msg-err", meta=_RETRY_INVITED_ROW)
        assert restart_resume.schedule_after_error(state, slot) is True
        await asyncio.wait_for(slot._auto_resume_task, 30)
        assert len(quiet_dispatch) == 2
        slot.append("error", "and again", "msg msg-err", meta=_RETRY_INVITED_ROW)
        assert restart_resume.schedule_after_error(state, slot) is True
        await asyncio.wait_for(slot._auto_resume_task, 30)
        assert (
            sum(
                1
                for m in slot.messages
                if (m.get("meta") or {}).get("kind") == AUTO_RESUME_PAUSED_KIND
            )
            == 2
        )

    @pytest.mark.asyncio
    async def test_remote_closing_and_restricted_slots_are_skipped(self, quiet_dispatch):
        remote = _cut_off(_slot("remote"))
        remote.executor = "remote"
        closing = _cut_off(_slot("closing"))
        closing.begin_close()
        private = _cut_off(_slot("private"))
        private.memory_mode = "incognito"
        state = _mock_state(remote, closing, private)

        assert await restart_resume.resume_after_restart(state, _cfg()) == 0
        assert quiet_dispatch == []

    @pytest.mark.asyncio
    async def test_a_chat_the_person_touched_first_is_skipped(self, monkeypatch, quiet_dispatch):
        slot = _cut_off(_slot())
        state = _mock_state(slot)
        held = _HeldPause()
        monkeypatch.setattr("kiro_crew.dashboard.cautious_boot.pause_before", held)

        task = asyncio.ensure_future(restart_resume.resume_after_restart(state, _cfg()))
        await asyncio.wait_for(held.entered.wait(), 30)  # the pass is inside its pause
        slot.append("user", "never mind", "msg msg-u")  # the person got there first
        restart_resume.reset_budget(slot)  # what their send does
        held.release.set()

        assert await asyncio.wait_for(task, 30) == 0
        assert quiet_dispatch == []

    @pytest.mark.asyncio
    async def test_turning_the_switch_off_during_the_pause_ends_the_pass(
        self, monkeypatch, quiet_dispatch
    ):
        first, second = _cut_off(_slot("first")), _cut_off(_slot("second"))
        state = _mock_state(first, second)
        held = _HeldPause()
        monkeypatch.setattr("kiro_crew.dashboard.cautious_boot.pause_before", held)
        switch = [None]
        monkeypatch.setattr(restart_resume, "_switch_on_now", lambda: switch[0])

        task = asyncio.ensure_future(restart_resume.resume_after_restart(state, _cfg()))
        await asyncio.wait_for(held.entered.wait(), 30)
        switch[0] = False  # the person turns it off while the pass waits
        held.release.set()

        assert await asyncio.wait_for(task, 30) == 0
        assert quiet_dispatch == []
        # This boot's reading is spent for both: the reconcile consumed the marker,
        # and a later pass in this process must not re-read it.
        assert first._restart_cut_turn is False and second._restart_cut_turn is False


# ── The error pass ─────────────────────────────────────────────────────────────


class _HeldPause:
    """A cautious-boot pause the test holds: ``entered`` fires when the pass is inside it."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self, group: str) -> None:
        self.entered.set()
        await self.release.wait()


class _HeldSleep:
    """A backoff the test holds: ``entered`` fires when the task is inside it."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        self.entered.set()
        await self.release.wait()


class TestErrorPass:
    @pytest.mark.asyncio
    async def test_arms_one_backed_off_retry_and_dispatches_it(self, monkeypatch, quiet_dispatch):
        monkeypatch.setattr(restart_resume, "_load_settings", lambda: (True, 5, 3600))
        held = _HeldSleep()
        monkeypatch.setattr(restart_resume, "_backoff_sleep", held)
        slot = _cut_off(_slot(), kind={TRANSIENT_NOTICE_META_KEY: TRANSIENT_NOTICE_GIVE_UP})
        state = _mock_state(slot)

        assert restart_resume.schedule_after_error(state, slot) is True
        assert restart_resume.schedule_after_error(state, slot) is False  # never twice
        task = slot._auto_resume_task
        await asyncio.wait_for(held.entered.wait(), 30)
        assert 30.0 <= held.delays[0] <= 30.0 * 1.25  # first retry in the window
        held.release.set()
        await asyncio.wait_for(task, 30)

        assert [c["via"] for c in quiet_dispatch] == [restart_resume.VIA_ERROR]
        assert quiet_dispatch[0]["body"] == _AUTO_RESUME_ERROR_MSG
        assert quiet_dispatch[0]["extra_meta"]["autoResume"]["cause"] == "error"
        assert slot._auto_resume_task is None

    @pytest.mark.asyncio
    async def test_switch_off_in_the_snapshot_arms_nothing(self, monkeypatch, quiet_dispatch):
        monkeypatch.setattr(restart_resume, "_switch_on_now", lambda: False)
        slot = _cut_off(_slot(), kind=_RETRY_INVITED_ROW)

        assert restart_resume.schedule_after_error(_mock_state(slot), slot) is False
        assert slot._auto_resume_task is None

    @pytest.mark.asyncio
    async def test_switch_off_on_disk_arms_but_dispatches_nothing(
        self, monkeypatch, quiet_dispatch
    ):
        # No snapshot yet (a gateway still booting): the task reads the disk config.
        monkeypatch.setattr(restart_resume, "_load_settings", lambda: (False, 5, 3600))
        slot = _cut_off(_slot(), kind=_RETRY_INVITED_ROW)
        state = _mock_state(slot)

        assert restart_resume.schedule_after_error(state, slot) is True
        await asyncio.wait_for(slot._auto_resume_task, 30)

        assert quiet_dispatch == []
        assert slot._auto_resume_stamps == []

    @pytest.mark.asyncio
    async def test_switching_off_during_the_backoff_stops_the_retry(
        self, monkeypatch, quiet_dispatch
    ):
        settings = [(True, 5, 3600), (False, 5, 3600)]  # before the sleep, after it
        monkeypatch.setattr(restart_resume, "_load_settings", lambda: settings.pop(0))
        held = _HeldSleep()
        monkeypatch.setattr(restart_resume, "_backoff_sleep", held)
        slot = _cut_off(_slot(), kind=_RETRY_INVITED_ROW)
        state = _mock_state(slot)

        assert restart_resume.schedule_after_error(state, slot) is True
        await asyncio.wait_for(held.entered.wait(), 30)
        held.release.set()
        await asyncio.wait_for(slot._auto_resume_task, 30)

        assert quiet_dispatch == []

    def test_never_retry_kind_arms_nothing(self):
        slot = _cut_off(_slot(), kind={"kind": TURN_TIMEOUT_KIND})

        assert restart_resume.schedule_after_error(_mock_state(slot), slot) is False
        assert slot._auto_resume_task is None

    @pytest.mark.asyncio
    async def test_a_send_during_the_backoff_cancels_the_retry(self, monkeypatch, quiet_dispatch):
        monkeypatch.setattr(restart_resume, "_load_settings", lambda: (True, 5, 3600))
        held = _HeldSleep()
        monkeypatch.setattr(restart_resume, "_backoff_sleep", held)
        slot = _cut_off(_slot(), kind=_RETRY_INVITED_ROW)
        state = _mock_state(slot)
        assert restart_resume.schedule_after_error(state, slot) is True
        task = slot._auto_resume_task
        await asyncio.wait_for(held.entered.wait(), 30)  # the cancel lands INSIDE the backoff

        restart_resume.reset_budget(slot)  # what a person's send does
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 30)

        assert quiet_dispatch == []
        assert slot._auto_resume_task is None

    @pytest.mark.asyncio
    async def test_a_persons_action_after_the_backoff_refuses_the_dispatch(self, monkeypatch):
        # The retry woke and reached continue_slot_turn; the person acted before
        # the queue insert. The hook runs under the slot lock and refuses.
        monkeypatch.setattr(restart_resume, "_load_settings", lambda: (True, 5, 3600))
        monkeypatch.setattr(restart_resume, "_switch_on_now", lambda: None)
        monkeypatch.setattr(restart_resume, "_audit", lambda *a, **k: None)
        held = _HeldSleep()
        monkeypatch.setattr(restart_resume, "_backoff_sleep", held)
        slot = _cut_off(_slot(), kind=_RETRY_INVITED_ROW)
        state = _mock_state(slot)
        inserted: list[str] = []

        async def continue_with_hook(state, slot, **kwargs):
            # continue_slot_turn's contract: before_dispatch runs right before the insert.
            restart_resume.cancel_armed_retry(slot)  # a Stop lands here
            kwargs["before_dispatch"]()
            inserted.append(slot.key)
            return "resumed"

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.continue_slot_turn", continue_with_hook
        )
        assert restart_resume.schedule_after_error(state, slot) is True
        task = slot._auto_resume_task
        await asyncio.wait_for(held.entered.wait(), 30)
        held.release.set()
        await asyncio.wait_for(task, 30)

        assert inserted == []  # refused under the lock, nothing queued
        assert slot._auto_resume_stamps == []  # and nothing spent

    @pytest.mark.asyncio
    async def test_the_stamp_is_spent_under_the_lock_before_the_insert(self, monkeypatch):
        # The stamp must be on the slot when the turn-start save runs, which is
        # the first thing after the insert: it is appended by the hook that runs
        # right before the insert, not after continue_slot_turn returns.
        monkeypatch.setattr(restart_resume, "_audit", lambda *a, **k: None)
        slot = _cut_off(_slot())
        state = _mock_state(slot)
        seen: list[int] = []

        async def continue_with_hook(state, slot, **kwargs):
            kwargs["before_dispatch"]()
            seen.append(len(slot._auto_resume_stamps))  # what the insert's save sees
            return "resumed"

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.continue_slot_turn", continue_with_hook
        )

        assert await restart_resume._dispatch(
            state, slot, cause=restart_resume.CAUSE_RESTART, limit=5, window_secs=3600
        )

        assert seen == [1]
        assert len(slot._auto_resume_stamps) == 1 and slot._dirty is True

    @pytest.mark.asyncio
    async def test_at_the_limit_the_error_pass_pauses_instead(self, monkeypatch, quiet_dispatch):
        monkeypatch.setattr(restart_resume, "_load_settings", lambda: (True, 2, 3600))
        slot = _cut_off(_slot(), kind=_RETRY_INVITED_ROW)
        slot._auto_resume_stamps = [time.time() - 10, time.time() - 5]
        state = _mock_state(slot)

        assert restart_resume.schedule_after_error(state, slot) is True
        await asyncio.wait_for(slot._auto_resume_task, 30)

        assert quiet_dispatch == []
        assert slot.messages[-1]["meta"]["kind"] == AUTO_RESUME_PAUSED_KIND


# ── What a natively resumed session is shown ───────────────────────────────────


def _global_names(code: types.CodeType) -> set[str]:
    names = set(code.co_names)
    for const in code.co_consts:
        if isinstance(const, types.CodeType):
            names |= _global_names(const)
    return names


class TestBootTask:
    """The two restore-time passes run behind the listen, notice first."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("enabled", [True, False], ids=["switch_on", "switch_off"])
    async def test_the_boot_task_tells_first_and_resumes_second(self, monkeypatch, enabled):
        order: list[str] = []

        async def notice(state, cfg):
            order.append("notice")
            return 1

        async def resume(state, cfg):
            order.append("resume")
            return 1

        monkeypatch.setattr(restart_resume, "notice_dropped_approval_modes", notice)
        monkeypatch.setattr(restart_resume, "resume_after_restart", resume)
        state = _mock_state(_slot())

        task = restart_resume.start_boot_pass(state, _cfg(enabled=enabled))

        # Nothing has run yet: the caller is not made to wait on either pass.
        assert order == []
        assert task in state._background_tasks
        assert await task == 1
        # The notice is always told, and before any resume could dispatch.
        assert order == ["notice", "resume"]

    def test_the_restore_hands_the_notice_pass_to_the_boot_task(self):
        # The restore is what ``KIROCREW_READY`` waits on; the notice pass loads
        # one deferred transcript per chat that was on a grant, so the restore
        # must schedule it, never await it.
        import inspect

        from kiro_crew.dashboard.server_runtime import session_restore

        src = inspect.getsource(session_restore)
        assert "await restart_resume.notice_dropped_approval_modes" not in src
        assert "restart_resume.start_boot_pass(state, cfg)" in src


class TestInterruptedTurnRestore:
    def test_both_automatic_bodies_re_inject_the_lost_turn(self):
        # kiro-cli logs a prompt only once it is answered, so on a session this
        # turn natively resumed the interrupted request is not in the model's
        # history. The runner re-injects it for every body in this tuple -- the
        # automatic resumes included, or the model is told to finish a request it
        # cannot see.
        from kiro_crew.dashboard.chat_runner import _run_chat
        from kiro_crew.dashboard.chat_utils import _INTERRUPTED_TURN_RESUME_MSGS

        assert {_AUTO_RESUME_RESTART_MSG, _AUTO_RESUME_ERROR_MSG, _MANUAL_RESUME_MSG} <= set(
            _INTERRUPTED_TURN_RESUME_MSGS
        )
        # The gate reads the shared tuple rather than a literal of its own
        # (``_run_chat`` is published behind a guard decorator; the body is unwrapped).
        body = inspect.unwrap(_run_chat).__code__
        assert "_INTERRUPTED_TURN_RESUME_MSGS" in _global_names(body)

    def test_the_gateways_notices_are_kept_out_of_the_restored_turn(self):
        # The restore drops the permissions notice exactly into the span the
        # builder quotes as the model's partial answer. ``interrupted_turn_rows``
        # is what the runner hands the builder: the notice is gone, the opener and
        # the partial answer stay, the same row objects, in order.
        from kiro_crew.context import build_interrupted_turn_preamble
        from kiro_crew.dashboard.chat_runner import _run_chat
        from kiro_crew.dashboard.chat_utils import interrupted_turn_rows

        rows = [
            {"role": "user", "content": "refactor the module", "meta": {}},
            {"role": "assistant", "content": "Working through the files…", "meta": {}},
            {"role": "error", "content": "interrupted", "meta": _RESTART_ROW},
            {
                "role": "assistant",
                "content": "🔒 This chat's permissions changed when the app restarted…",
                "meta": {"kind": APPROVAL_MODE_DROPPED_KIND},
            },
        ]
        current = {"role": "inject", "content": _AUTO_RESUME_RESTART_MSG, "meta": {}}

        handed = interrupted_turn_rows(rows)
        with_notice = build_interrupted_turn_preamble(rows, current=current)
        without = build_interrupted_turn_preamble(handed, current=current)

        assert [r is o for r, o in zip(handed, rows[:3])] == [True, True, True]
        assert len(handed) == 3
        assert "permissions changed" in with_notice  # the hazard, unfiltered
        assert "permissions changed" not in without
        assert "Working through the files" in without
        assert "interrupted_turn_rows" in _global_names(inspect.unwrap(_run_chat).__code__)


# ── continue_slot_turn: the body argument and the human reset ──────────────────


class TestContinueBody:
    @pytest.fixture
    def _patched(self, monkeypatch):
        mock_sel = MagicMock()
        started = AsyncMock(return_value=True)
        with (
            patch("kiro_crew.dashboard.chat_handlers.sel", return_value=mock_sel),
            patch("kiro_crew.dashboard.chat_handlers._start_next_queued_turn", started),
        ):
            yield mock_sel

    @pytest.mark.asyncio
    async def test_an_automatic_body_is_queued_verbatim_and_keeps_the_budget(self, _patched):
        slot = _cut_off(_slot())
        slot._auto_resume_stamps = [time.time()]
        state = _mock_state(slot)

        result = await continue_slot_turn(
            state,
            slot,
            directive_user_origin=False,
            via=restart_resume.VIA_RESTART,
            body=_AUTO_RESUME_RESTART_MSG,
            extra_meta={"autoResume": {"cause": "restart", "attempt": 1, "limit": 5}},
        )

        assert result == "resumed"
        entry = slot._queue[0]
        assert entry["content"] == _AUTO_RESUME_RESTART_MSG
        assert entry["kind"] == SYNTHETIC_RECOVERY_KIND
        assert entry["meta"]["autoResume"]["attempt"] == 1
        assert slot._auto_resume_stamps  # the automation never resets the budget
        audit = _patched.log_tool_invocation.call_args.kwargs
        assert audit["tool_name"] == "dashboard_continue"
        assert audit["metadata"]["via"] == restart_resume.VIA_RESTART

    @pytest.mark.asyncio
    async def test_a_persons_resume_resets_the_budget(self, _patched):
        slot = _cut_off(_slot())
        slot._auto_resume_stamps = [time.time()]
        state = _mock_state(slot)

        await continue_slot_turn(state, slot, directive_user_origin=True)

        assert slot._queue[0]["content"] == _MANUAL_RESUME_MSG
        assert slot._auto_resume_stamps == []

    @pytest.mark.asyncio
    async def test_an_agents_continue_leaves_the_budget_alone(self, _patched):
        # session_retry / session_continue and an app's Continue carry no human
        # origin: a conductor continuing a worker must not refill its limit.
        slot = _cut_off(_slot())
        slot._auto_resume_stamps = [time.time()]
        state = _mock_state(slot)

        await continue_slot_turn(state, slot, directive_user_origin=False, via="session_control")

        assert slot._queue[0]["content"] == _MANUAL_RESUME_MSG
        assert slot._auto_resume_stamps  # untouched

    @pytest.mark.asyncio
    async def test_a_dispatch_a_person_superseded_is_refused_under_the_lock(self, _patched):
        # The epoch moved (a Stop, a send) after the retry woke: the hook runs
        # right before the insert and the entry never lands.
        slot = _cut_off(_slot(), kind=_RETRY_INVITED_ROW)
        state = _mock_state(slot)
        epoch = slot._auto_resume_epoch
        restart_resume.cancel_armed_retry(slot)  # the person's Stop

        def hook() -> None:
            if slot._auto_resume_epoch != epoch:
                raise SlotContinueRefusal("a person acted on the chat", "auto_resume_superseded")

        with pytest.raises(SlotContinueRefusal) as exc:
            await continue_slot_turn(
                state,
                slot,
                directive_user_origin=False,
                body=_AUTO_RESUME_ERROR_MSG,
                before_dispatch=hook,
            )

        assert exc.value.code == "auto_resume_superseded"
        assert slot._queue == []


# ── What a person's actions do to an armed resume ──────────────────────────────


class TestPersonsActions:
    def _armed(self, slot: _ChatSlot) -> MagicMock:
        slot._auto_resume_stamps = [time.time()]
        task = MagicMock()
        task.done.return_value = False
        slot._auto_resume_task = task
        return task

    @pytest.mark.asyncio
    async def test_a_persons_send_resets_the_budget_an_apps_send_does_not(
        self, tmp_path, monkeypatch
    ):
        from chat_test_helpers import _make_app

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)

        async def fake_run_chat(st, sl, msg, **kwargs):
            sl.append("chunk", "ack", "chunk")

        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._run_chat", fake_run_chat)
        state = _make_state(tmp_path)
        person = _cut_off(state.get_or_create_slot("person"), kind=_RETRY_INVITED_ROW)
        person_task = self._armed(person)
        epoch = person._auto_resume_epoch
        app_slot = _cut_off(
            state.get_or_create_slot("app-owned", app="some-app"), kind=_RETRY_INVITED_ROW
        )
        app_task = self._armed(app_slot)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat", json={"message": "hello", "slot": "person"}, timeout=None
            )
            assert resp.status == 200  # the reset ran before any branch of the send
            resp.close()

        assert person._auto_resume_stamps == []
        assert person._auto_resume_task is None
        assert person._auto_resume_epoch == epoch + 1
        person_task.cancel.assert_called_once()

        # The same send from an APP (its token names it): no human origin, so the
        # budget of the chat it owns is left alone.
        @web.middleware
        async def _as_app(request, handler):
            request["app"] = "some-app"
            request["user"] = "some-app"
            return await handler(request)

        app = _make_app(state)
        app.middlewares.insert(0, _as_app)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat", json={"message": "hello", "slot": "app-owned"}, timeout=None
            )
            assert resp.status == 200, await resp.text()
            resp.close()

        assert app_slot._auto_resume_stamps and app_slot._auto_resume_task is app_task
        app_task.cancel.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "refusal",
        ["member_pin", "agent_mismatch", "message_required"],
    )
    async def test_a_refused_send_leaves_the_budget_as_it_found_it(
        self, tmp_path, monkeypatch, refusal
    ):
        """A denied request leaves the slot exactly as it found it -- budget included."""
        from chat_test_helpers import _make_app

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        if refusal == "member_pin":
            slot = state.get_or_create_slot("member-mate", agent="mate", mode=DM_SLOT_MODE)
            body = {"slot": slot.key, "agent": "someone-else", "message": "hello"}
            expected = (409, "member_thread_agent_pinned")
        elif refusal == "agent_mismatch":
            slot = state.get_or_create_slot("pinned", agent="mate")
            # An unknown requested name resolves to nothing: the mismatch 409.
            body = {"slot": slot.key, "agent": "no-such-agent", "message": "hello"}
            expected = (409, None)
        else:
            slot = state.get_or_create_slot("person")
            body = {"slot": slot.key, "message": ""}
            expected = (400, "message_required")
        _cut_off(slot, kind=_RETRY_INVITED_ROW)
        task = self._armed(slot)
        epoch = slot._auto_resume_epoch
        stamps = list(slot._auto_resume_stamps)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat", json=body, timeout=None)
            assert resp.status == expected[0], await resp.text()
            if expected[1]:
                assert (await resp.json())["code"] == expected[1]
            resp.close()

        assert slot._auto_resume_stamps == stamps
        assert slot._auto_resume_task is task
        assert slot._auto_resume_epoch == epoch
        task.cancel.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_send_queued_behind_a_running_turn_still_resets(self, tmp_path, monkeypatch):
        from chat_test_helpers import _make_app

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = _cut_off(state.get_or_create_slot("busy"), kind=_RETRY_INVITED_ROW)
        task = self._armed(slot)
        epoch = slot._auto_resume_epoch
        # A turn owns the slot: the send takes the busy branch and is queued.
        running = asyncio.get_running_loop().create_future()
        slot.task = running
        try:
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat", json={"slot": slot.key, "message": "hello"}, timeout=None
                )
                assert resp.status == 200, await resp.text()
                assert (await resp.json()).get("queued") is True
                resp.close()
        finally:
            running.cancel()

        assert slot._auto_resume_stamps == []
        assert slot._auto_resume_task is None
        assert slot._auto_resume_epoch == epoch + 1
        task.cancel.assert_called_once()

    @pytest.mark.asyncio
    async def test_a_stop_drops_the_armed_retry(self, tmp_path, monkeypatch):
        from chat_test_helpers import _make_app

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.sessions.reset = AsyncMock()
        slot = _cut_off(state.get_or_create_slot("s1"), kind=_RETRY_INVITED_ROW)
        task = self._armed(slot)
        epoch = slot._auto_resume_epoch

        async with TestClient(TestServer(_make_app(state))) as client:
            assert (await client.post("/api/chat/slots/s1/stop")).status == 200

        task.cancel.assert_called_once()
        assert slot._auto_resume_task is None
        assert slot._auto_resume_epoch == epoch + 1
        assert slot._auto_resume_stamps  # a Stop is not a reset: the count stands

    @pytest.mark.asyncio
    async def test_a_close_drops_the_armed_retry(self, tmp_path, monkeypatch):
        from chat_test_helpers import _make_app

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = _cut_off(state.get_or_create_slot("s1"), kind=_RETRY_INVITED_ROW)
        task = self._armed(slot)

        async with TestClient(TestServer(_make_app(state))) as client:
            assert (await client.delete("/api/chat/slots/s1")).status == 200

        task.cancel.assert_called_once()
        assert slot._auto_resume_task is None


# ── The approval-mode notice ───────────────────────────────────────────────────


class TestApprovalModeNotice:
    def test_the_recorded_mode_is_never_applied_to_the_flags(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("dst")
        meta = {"_type": "metadata", "approval_mode_seen": "trust"}

        codec.apply(state, slot, meta, codec.Restore(name="dst", member=None))

        assert slot._trust is False and slot._trust_reads is False
        assert slot._approval_mode_recorded == "trust"

    def test_the_save_writes_a_grant_and_omits_the_default(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        folds = codec.SaveFolds(memory_mode=slot.memory_mode)

        assert "approval_mode_seen" not in codec.encode(slot, folds=folds)
        assert codec.encode(slot, merge=True, folds=folds)["approval_mode_seen"] == "normal"
        slot._trust_reads = True
        assert codec.encode(slot, folds=folds)["approval_mode_seen"] == "trust_reads"
        assert "approval_mode_seen" in SLOT_OWNED_META_KEYS
        # The writers are pure: the record stays what the restore read.
        assert slot._approval_mode_recorded == ""

    def test_a_dropped_trust_gets_one_notice(self):
        slot = _cut_off(_slot())
        slot._approval_mode_recorded = "trust"

        assert restart_resume.approval_mode_dropped_notice(slot) is True
        assert restart_resume.approval_mode_dropped_notice(slot) is False  # idempotent

        row = slot.messages[-1]
        assert row["role"] == "assistant"
        assert {k: v for k, v in row["meta"].items() if k != "mid"} == {
            "kind": APPROVAL_MODE_DROPPED_KIND,
            "from": "trust",
            "to": "normal",
        }
        assert "permissions changed when the app restarted" in row["content"]
        assert "Trust (tools run without asking)" in row["content"]
        assert is_system_notice(row["role"], row["meta"])
        assert slot._trust is False  # the notice restores nothing

    @pytest.mark.parametrize("recorded", ["", "normal"])
    def test_nothing_to_say_when_the_person_saw_ask_every_time(self, recorded):
        slot = _cut_off(_slot())
        slot._approval_mode_recorded = recorded

        assert restart_resume.approval_mode_dropped_notice(slot) is False

    def test_no_notice_when_the_mode_came_back(self):
        # The declared crewmate seed, or crash-only trust, put it back before us.
        slot = _cut_off(_slot())
        slot._approval_mode_recorded = "trust"
        slot._trust = True

        assert restart_resume.approval_mode_dropped_notice(slot) is False

    def test_a_crewmate_thread_whose_declared_mode_comes_back_is_not_told(self, monkeypatch):
        # A crewmate thread opens in the mode its profile declares (unset reads as
        # Trust): ``_seed_member_approval`` re-applies it when the thread is opened,
        # so a record that matches it describes nothing the person has to redo.
        audit = MagicMock()
        monkeypatch.setattr(restart_resume, "_audit", audit)
        thread = _cut_off(_ChatSlot("dm", agent="mate", mode=DM_SLOT_MODE))
        thread._approval_mode_recorded = "trust"
        unset = SimpleNamespace(agents={"mate": SimpleNamespace(approval_mode="")})
        declared = SimpleNamespace(agents={"mate": SimpleNamespace(approval_mode="trust")})

        for cfg in (unset, declared, None):
            assert restart_resume.approval_mode_dropped_notice(thread, cfg=cfg) is False, cfg

        assert all(
            (m.get("meta") or {}).get("kind") != APPROVAL_MODE_DROPPED_KIND for m in thread.messages
        )
        audit.assert_not_called()

    def test_a_crewmate_thread_the_boot_pass_may_resume_is_told_first(self):
        # The seed runs when the thread is OPENED, and the boot pass runs before
        # anyone opens it: with the switch on, the cut turn would be resumed under
        # Ask every time, a mode the person has not been told about. So this one
        # thread is told, with the way back that fits a crewmate thread.
        thread = _cut_off(_ChatSlot("dm", agent="mate", mode=DM_SLOT_MODE))
        thread._approval_mode_recorded = "trust"
        off = SimpleNamespace(
            dashboard=_cfg(enabled=False).dashboard,
            agents={"mate": SimpleNamespace(approval_mode="trust")},
        )
        on = SimpleNamespace(dashboard=_cfg().dashboard, agents=off.agents)

        assert restart_resume.approval_mode_dropped_notice(thread, cfg=off) is False
        assert restart_resume.approval_mode_dropped_notice(thread, cfg=on) is True

        row = thread.messages[-1]
        assert row["meta"]["from"] == "trust" and row["meta"]["to"] == "normal"
        assert "Crewmates page" in row["content"] and "composer" not in row["content"]

    def test_a_crewmate_thread_whose_record_is_gone_is_told(self):
        # A config that was read and holds no record for the crewmate (renamed,
        # removed) seeds nothing on open, so no mode comes back.
        thread = _cut_off(_ChatSlot("dm", agent="gone", mode=DM_SLOT_MODE))
        thread._approval_mode_recorded = "trust"
        cfg = SimpleNamespace(agents={"mate": SimpleNamespace(approval_mode="")})

        assert restart_resume.approval_mode_dropped_notice(thread, cfg=cfg) is True

    def test_a_crewmate_thread_that_held_a_grant_its_profile_does_not_declare_is_told(self):
        # The profile says Ask every time; the thread held Trust (an approval
        # card's "trust this session"). That grant does not come back on open.
        thread = _cut_off(_ChatSlot("dm", agent="mate", mode=DM_SLOT_MODE))
        thread._approval_mode_recorded = "trust"
        cfg = SimpleNamespace(agents={"mate": SimpleNamespace(approval_mode="normal")})

        assert restart_resume.approval_mode_dropped_notice(thread, cfg=cfg) is True

        row = thread.messages[-1]
        assert row["meta"]["from"] == "trust" and row["meta"]["to"] == "normal"
        # The Crewmates composer has no approval picker: the remedy names what does
        # bring the grant back.
        assert "profile" in row["content"]
        assert "composer" not in row["content"]

    def test_an_app_owned_chat_is_not_told(self):
        # An app re-grants the trust on the chats it owns itself; nobody picks it
        # from a composer, so the notice would ask the person to redo what the
        # app will do.
        owned = _cut_off(_slot("owned"))
        owned._app = "some-app"
        owned._approval_mode_recorded = "trust"

        assert restart_resume.approval_mode_dropped_notice(owned) is False

    def test_the_two_notices_write_their_audit_lines(self, monkeypatch):
        sel = MagicMock()
        monkeypatch.setattr("kiro_crew.sel.sel", lambda: sel)
        dropped = _cut_off(_slot("dropped"))
        dropped._approval_mode_recorded = "trust_reads"
        spent = _cut_off(_slot("spent"))

        assert restart_resume.approval_mode_dropped_notice(dropped) is True
        restart_resume._notice_budget_exhausted(
            _mock_state(spent), spent, used=5, limit=5, window_secs=3600
        )

        lines = [c.kwargs for c in sel.log_api_access.call_args_list]
        assert lines == [
            {
                "caller": "dashboard:restart_resume",
                "operation": "restart_resume:approval_mode_dropped",
                "outcome": "trust_reads->normal",
                "resources": "dropped",
            },
            {
                "caller": "dashboard:restart_resume",
                "operation": "restart_resume:budget_exhausted",
                "outcome": "5/5",
                "resources": "spent",
            },
        ]

    def test_no_notice_under_the_global_override(self, monkeypatch):
        # agent.dangerously_skip_permissions is live: nothing asks, so "now on
        # Ask every time" would be false.
        override = MagicMock()
        override.is_active.return_value = True
        monkeypatch.setattr("kiro_crew.safety_override.safety_override", lambda: override)
        slot = _cut_off(_slot())
        slot._approval_mode_recorded = "trust"

        assert restart_resume.approval_mode_dropped_notice(slot) is False

    @pytest.mark.asyncio
    async def test_the_restore_wrapper_tells_only_the_chats_that_changed(self, monkeypatch):
        dropped = _cut_off(_slot("dropped"))
        dropped._approval_mode_recorded = "trust"
        kept = _cut_off(_slot("kept"))
        kept._approval_mode_recorded = "trust_reads"
        kept._trust_reads = True
        plain = _cut_off(_slot("plain"))
        loaded: list[str] = []

        async def fake_load(slot):
            loaded.append(slot.key)

        monkeypatch.setattr(restart_resume, "load_window_off_loop", fake_load)
        state = _mock_state(dropped, kept, plain)

        told = await restart_resume.notice_dropped_approval_modes(state, None)

        assert told == 1
        # Only the chat with something to say has its window loaded: a chat whose
        # mode came back and a plain chat are never touched.
        assert loaded == ["dropped"]
        assert dropped.messages[-1]["meta"]["kind"] == APPROVAL_MODE_DROPPED_KIND
        assert all(
            (m.get("meta") or {}).get("kind") != APPROVAL_MODE_DROPPED_KIND for m in kept.messages
        )

    def test_a_pick_dirties_the_chats_whose_mode_moved(self):
        # The flush skips a non-dirty slot and a pick appends no row.
        picked, other = _cut_off(_slot("picked")), _cut_off(_slot("other"))
        picked._dirty = other._dirty = False
        before = codec.approval_modes([picked, other])

        picked._trust = True
        changed = codec.mark_changed_approval_modes([picked, other], before)

        assert changed == 1
        assert picked._dirty is True and other._dirty is False

    @pytest.mark.asyncio
    async def test_the_picker_route_dirties_the_chat_so_the_record_follows(
        self, tmp_path, monkeypatch
    ):
        from kiro_crew.dashboard.chat import api_chat_mode

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("idle")
        slot.append("user", "hello", "msg msg-u")
        slot.append("assistant", "hi", "msg msg-a")
        slot._dirty = False

        @web.middleware
        async def _auth(request, handler):
            request.setdefault("app", "")
            request.setdefault("user", "local-app")
            return await handler(request)

        app = web.Application(middlewares=[_auth])
        app["state"] = state
        app.router.add_post("/api/chat/mode", api_chat_mode)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/mode", json={"mode": "trust", "slot": slot.key})
            assert resp.status == 200, await resp.text()

        assert slot._trust is True
        assert slot._dirty is True, "an idle chat's pick must reach the next flush"
        assert (
            codec.encode(slot, folds=codec.SaveFolds(memory_mode=slot.memory_mode))[
                "approval_mode_seen"
            ]
            == "trust"
        )

    def test_both_notice_kinds_are_system_notices(self):
        assert {APPROVAL_MODE_DROPPED_KIND, AUTO_RESUME_PAUSED_KIND} <= SYSTEM_NOTICE_KINDS


# ── Settings surface ───────────────────────────────────────────────────────────


class TestSettings:
    def test_loader_defaults_and_clamps(self):
        from kiro_crew.config.loader import _build_dashboard_config

        default = _build_dashboard_config(set(), {})
        assert (
            default.resume_interrupted_turns,
            default.resume_retry_limit,
            default.resume_retry_window_minutes,
        ) == (False, 5, 60)
        clamped = _build_dashboard_config(
            set(),
            {
                "resume_interrupted_turns": True,
                "resume_retry_limit": 99,
                "resume_retry_window_minutes": 1,
            },
        )
        assert (clamped.resume_retry_limit, clamped.resume_retry_window_minutes) == (20, 5)

    @pytest.mark.asyncio
    async def test_dashboard_config_endpoint_round_trips(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.handlers import api_dashboard_config

        monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: tmp_path / "config.json")
        monkeypatch.setattr(
            "kiro_crew.config.loader.config_local_path", lambda: tmp_path / "config.local.json"
        )
        monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = web.Application()
        app["state"] = state
        app.router.add_get("/api/dashboard/config", api_dashboard_config)
        app.router.add_put("/api/dashboard/config", api_dashboard_config)
        app = as_owner(app)
        async with TestClient(TestServer(app)) as client:
            got = await (await client.get("/api/dashboard/config")).json()
            assert (
                got["resume_interrupted_turns"],
                got["resume_retry_limit"],
                got["resume_retry_window_minutes"],
            ) == (False, 5, 60)

            put = await client.put(
                "/api/dashboard/config",
                json={
                    "resume_interrupted_turns": True,
                    "resume_retry_limit": 50,
                    "resume_retry_window_minutes": "240",
                },
            )
            assert put.status == 200, await put.text()
            got = await (await client.get("/api/dashboard/config")).json()
            assert (
                got["resume_interrupted_turns"],
                got["resume_retry_limit"],
                got["resume_retry_window_minutes"],
            ) == (True, 20, 240)

            bad = await client.put(
                "/api/dashboard/config", json={"resume_interrupted_turns": "yes"}
            )
            assert bad.status == 400
            assert (await bad.json())["code"] == "invalid_resume_interrupted_turns"
            bad = await client.put("/api/dashboard/config", json={"resume_retry_limit": "many"})
            assert bad.status == 400
            assert (await bad.json())["code"] == "invalid_resume_retry_limit"
            bad = await client.put(
                "/api/dashboard/config", json={"resume_retry_window_minutes": "soon"}
            )
            assert bad.status == 400
            assert (await bad.json())["code"] == "invalid_resume_retry_window_minutes"
            # A JSON Infinity or a fraction is refused, not a 500 and not truncated.
            for odd in ("Infinity", "NaN", "2.5"):
                bad = await client.put(
                    "/api/dashboard/config",
                    data='{"resume_retry_limit": ' + odd + "}",
                    headers={"Content-Type": "application/json"},
                )
                assert bad.status == 400, (odd, bad.status)
                assert (await bad.json())["code"] == "invalid_resume_retry_limit"


# ── End to end through the real persistence path ────────────────────────────────


def _cut_mid_turn_on_disk(state, tmp_path, monkeypatch, *, trust: bool = True) -> None:
    """Write a chat the previous process died in: Trust picked, a turn admitted."""
    from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    slot = state.get_or_create_slot("chat-1")
    slot._trust = trust  # the person picked Trust in the composer
    slot.append("user", "refactor the module", "msg msg-u")
    slot.append("assistant", "Working through the files…", "msg msg-a")
    slot._turn_in_flight_generation = 3
    assert _save_slot_to_history(state, slot, force=True)
    del state._slots["chat-1"]


class TestThroughTheRestore:
    @pytest.mark.asyncio
    async def test_a_trusted_chat_cut_mid_turn_comes_back_told_and_resumed(
        self, tmp_path, monkeypatch, quiet_dispatch
    ):
        from kiro_crew.dashboard.chat import restore_recent_sessions
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

        state = _make_state(tmp_path)
        state._background_tasks = set()
        state.notify = MagicMock()
        _cut_mid_turn_on_disk(state, tmp_path, monkeypatch)

        # The previous process is gone. The next boot restores the chat...
        assert restore_recent_sessions(state, window_minutes=30) == 1
        restored = state.get_slot("chat-1")
        assert restored is not None
        assert restored._trust is False  # ...untrusted, by construction...
        assert restored._approval_mode_recorded == "trust"
        assert restored._restart_cut_turn is True  # ...flagged by the marker...
        assert restored.messages[-1]["meta"]["kind"] == _RESTART_INTERRUPTION_KIND

        # ...and the restore hook tells the person...
        assert await restart_resume.notice_dropped_approval_modes(state, _cfg()) == 1
        # The notice is a status row: the chat still reads as interrupted, so the
        # manual Resume stays available had the automation been off...
        assert is_turn_interrupted(restored.messages) is True
        # ...then the boot pass resumes it.
        assert await restart_resume.resume_after_restart(state, _cfg()) == 1

        kinds = [(m.get("meta") or {}).get("kind") for m in restored.messages]
        assert kinds[-3:] == [_RESTART_INTERRUPTION_KIND, APPROVAL_MODE_DROPPED_KIND, None]
        assert [c["slot"] for c in quiet_dispatch] == ["chat-1"]
        resumed_row = restored.messages[-1]  # the body the drain landed, stamped by the dispatcher
        assert resumed_row["role"] == "inject"
        assert resumed_row["meta"]["autoResume"] == {"cause": "restart", "attempt": 1, "limit": 5}
        # The resume stamp is persisted with the next save, so a second boot counts
        # it; the interruption row is on disk but no marker is, so the second boot
        # resumes nothing on its own account.
        assert _save_slot_to_history(state, restored, force=True)
        del state._slots["chat-1"]
        assert restore_recent_sessions(state, window_minutes=30) == 1
        again = state.get_slot("chat-1")
        assert len(again._auto_resume_stamps) == 1
        assert again._restart_cut_turn is False
        # The default is not written to the line; its absence reads as Ask every time.
        assert again._approval_mode_recorded == ""
        assert await restart_resume.notice_dropped_approval_modes(state, _cfg()) == 0
        quiet_dispatch.clear()
        assert await restart_resume.resume_after_restart(state, _cfg()) == 0
        assert quiet_dispatch == []

    @pytest.mark.asyncio
    async def test_a_flush_landing_mid_restore_cannot_erase_what_the_person_saw(
        self, tmp_path, monkeypatch
    ):
        # The interruption row makes the restored chat dirty, so the 5 s flush can
        # save it before the notice hook runs. The save re-derives the line from
        # the live flags (now Ask every time) -- and must leave the restore's
        # reading alone, or the notice compares against the wrong mode.
        from kiro_crew.dashboard.chat import restore_recent_sessions
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

        state = _make_state(tmp_path)
        _cut_mid_turn_on_disk(state, tmp_path, monkeypatch)
        assert restore_recent_sessions(state, window_minutes=30) == 1
        restored = state.get_slot("chat-1")
        assert restored._dirty is True

        assert _save_slot_to_history(state, restored, force=True)  # the flush wins the race

        assert restored._approval_mode_recorded == "trust"  # the reading survived the save
        assert await restart_resume.notice_dropped_approval_modes(state, _cfg()) == 1
        assert restored.messages[-1]["meta"]["from"] == "trust"

    @pytest.mark.asyncio
    async def test_the_stamp_rides_the_turn_start_save(self, tmp_path, monkeypatch):
        # An automatic resume stamps the slot before its turn starts; the turn's
        # marker save carries the stamp so a gateway that dies on that turn
        # before the next flush still counts it on the next boot.
        from kiro_crew.dashboard.chat import restore_recent_sessions
        from kiro_crew.dashboard.chat_turn.turn_marker import _begin_local_turn_marker

        state = _make_state(tmp_path)
        _cut_mid_turn_on_disk(state, tmp_path, monkeypatch, trust=False)
        assert restore_recent_sessions(state, window_minutes=30) == 1
        restored = state.get_slot("chat-1")
        restored._auto_resume_stamps = [time.time()]

        await _begin_local_turn_marker(state, restored, 4)

        del state._slots["chat-1"]
        assert restore_recent_sessions(state, window_minutes=30) == 1
        assert len(state.get_slot("chat-1")._auto_resume_stamps) == 1
