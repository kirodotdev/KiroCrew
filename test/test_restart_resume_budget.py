"""The automatic-resume budget's durable half: on the line before the resume, or no resume.

Sibling of ``test_restart_resume.py`` (which holds the shared slot/state helpers
and the ``quiet_dispatch`` fixture this module imports): the stamp a resume
spends is committed before the resume is queued, a write that did not complete
refuses the resume, and a person's reset survives a write that was already in
flight when it landed.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest
from test_restart_resume import (  # noqa: F401 -- the fixture is requested by name
    _cut_off,
    _mock_state,
    _slot,
    quiet_dispatch_fixture,
)

from kiro_crew import history as history_mod
from kiro_crew.dashboard import restart_resume
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.dashboard.state import _ChatSlot
from kiro_crew.history import ConversationLog


class _FakeLog:
    """A conversation log whose metadata merge records its calls and answers as told."""

    def __init__(self, answer: object = True):
        self.answer = answer
        self.calls: list[dict] = []

    def update_metadata_if(
        self,
        key,
        fields,
        guard,
        *,
        require_existing=False,
        require_write=False,
        after_commit_under_lock=None,
    ):
        self.calls.append(
            {
                "key": key,
                "fields": fields,
                "require_existing": require_existing,
                "require_write": require_write,
                "guard": guard({}),
            }
        )
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


class _ResetBeforeTheLock(_FakeLog):
    """A merge whose guard runs after a person's reset landed on the slot.

    Stands in for the lock ordering ``_commit_budget`` cannot see: the reset
    emptied the list between the write's snapshot and the guard's turn under the
    line's lock. Answers what the guard decides, as the real merge does.
    """

    def __init__(self, slot: _ChatSlot):
        super().__init__(True)
        self.slot = slot

    def update_metadata_if(
        self,
        key,
        fields,
        guard,
        *,
        require_existing=False,
        require_write=False,
        after_commit_under_lock=None,
    ):
        restart_resume.reset_budget(self.slot)
        verdict = guard({})
        self.calls.append(
            {
                "key": key,
                "fields": fields,
                "require_existing": require_existing,
                "require_write": require_write,
                "guard": verdict,
            }
        )
        return verdict


def _person_resets_as_the_write_lands(slot: _ChatSlot):
    """A ``run_to_completion`` whose awaited write lands, then a person's send resets the budget.

    The other half of the race: the write took its lock first, so the spent
    stamps are on the line and the reset that followed must stay pending.
    """
    real = restart_resume.run_to_completion

    async def _land_then_reset(aw):
        result = await real(aw)
        restart_resume.reset_budget(slot)
        return result

    return _land_then_reset


class TestDurableBudget:
    """The stamp is on the metadata line before the resume is queued, or there is no resume."""

    @pytest.mark.asyncio
    async def test_the_stamp_is_written_before_the_resume_is_queued(self, quiet_dispatch):
        slot = _cut_off(_slot())
        state = _mock_state(slot)
        state.conversation_log = log = _FakeLog(True)
        order: list[str] = []
        real_update = log.update_metadata_if

        def recording_update(*args, **kwargs):
            order.append("write")
            return real_update(*args, **kwargs)

        log.update_metadata_if = recording_update
        quiet = quiet_dispatch

        ran = await restart_resume._dispatch(
            state, slot, cause="restart", limit=5, window_secs=3600
        )

        assert ran is True
        assert len(quiet) == 1
        # The write happened, with the stamp in it, before the queue insert.
        assert order == ["write"]
        [call] = log.calls
        assert call["fields"]["auto_resume_stamps"] == slot._auto_resume_stamps
        assert len(slot._auto_resume_stamps) == 1
        assert call["require_existing"] is True
        # A skipped write (legacy row-first transcript) must read as not written,
        # or the resume runs on a stamp that exists nowhere on disk.
        assert call["require_write"] is True
        assert call["guard"] is True
        assert slot._dirty is True

    def test_the_write_is_fenced_to_this_slot_incarnation(self):
        slot = _cut_off(_slot())
        state = _mock_state(slot)
        state.conversation_log = log = _FakeLog(True)
        # The name now maps to a replacement: the guard refuses the merge.
        state._slots[slot.key] = _slot()
        slot._auto_resume_stamps.append(time.time())

        written = asyncio.run(restart_resume._commit_budget(state, slot))

        assert written is True  # the fake answers True; what matters is the guard it was handed
        assert log.calls[0]["guard"] is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "answer",
        [False, OSError("disk full"), RuntimeError("lock lost")],
        ids=["no_line_or_guard_refused", "oserror", "other_error"],
    )
    async def test_a_budget_that_could_not_be_written_refuses_the_resume(
        self, quiet_dispatch, answer
    ):
        slot = _cut_off(_slot())
        state = _mock_state(slot)
        state.conversation_log = _FakeLog(answer)

        ran = await restart_resume._dispatch(
            state, slot, cause="restart", limit=5, window_secs=3600
        )

        assert ran is False
        assert quiet_dispatch == []
        # Nothing ran, so nothing was spent -- in memory either.
        assert slot._auto_resume_stamps == []

    @pytest.mark.asyncio
    async def test_no_conversation_log_means_no_automatic_resume(self, quiet_dispatch):
        slot = _cut_off(_slot())
        state = _mock_state(slot)
        state.conversation_log = None

        ran = await restart_resume._dispatch(state, slot, cause="error", limit=5, window_secs=3600)

        assert ran is False
        assert quiet_dispatch == []
        assert slot._auto_resume_stamps == []

    @pytest.mark.asyncio
    async def test_a_refusal_under_the_lock_takes_the_stamp_back_in_memory(self, monkeypatch):
        slot = _cut_off(_slot())
        state = _mock_state(slot)
        state.conversation_log = log = _FakeLog(True)

        async def person_acted_then_continue(state, slot, **kwargs):
            # A person acts between the committed write and the queue insert.
            slot._auto_resume_epoch += 1
            kwargs["before_dispatch"]()
            raise AssertionError("the refusal must stop the insert")

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.continue_slot_turn", person_acted_then_continue
        )
        monkeypatch.setattr(restart_resume, "_audit", lambda *a, **k: None)

        ran = await restart_resume._dispatch(
            state, slot, cause="restart", limit=5, window_secs=3600
        )

        assert ran is False
        # Written once (the committed stamp stands on the line until the next
        # save -- fail closed), taken back in memory at once.
        assert len(log.calls) == 1
        assert slot._auto_resume_stamps == []

    def test_a_reset_is_pending_until_a_save_carries_the_empty_list(self):
        slot = _slot()
        slot._auto_resume_stamps = [time.time()]

        restart_resume.reset_budget(slot)

        assert slot._auto_resume_stamps == []
        assert slot._auto_resume_reset_pending is True
        # A chat with no stamps to reset has nothing pending.
        fresh = _slot()
        restart_resume.reset_budget(fresh)
        assert fresh._auto_resume_reset_pending is False

    @pytest.mark.asyncio
    async def test_the_next_turn_marker_writes_the_empty_list_after_a_reset(self):
        from kiro_crew.dashboard import chat_runner

        slot = _slot()
        slot.append("user", "do the thing", "msg msg-u")
        state = _mock_state(slot)
        state.conversation_log = log = _FakeLog(True)
        slot._auto_resume_stamps = [time.time()]
        restart_resume.reset_budget(slot)

        await chat_runner._begin_local_turn_marker(state, slot, 1)

        [call] = log.calls
        assert call["fields"]["auto_resume_stamps"] == []
        assert slot._auto_resume_reset_pending is False

        # A chat that never had an automatic resume writes the same line as before.
        never = _slot()
        never.append("user", "hello", "msg msg-u")
        state2 = _mock_state(never)
        state2.conversation_log = log2 = _FakeLog(True)
        await chat_runner._begin_local_turn_marker(state2, never, 1)
        assert "auto_resume_stamps" not in log2.calls[0]["fields"]

    def test_a_reset_that_landed_before_the_lock_refuses_the_write(self):
        slot = _cut_off(_slot())
        state = _mock_state(slot)
        state.conversation_log = log = _ResetBeforeTheLock(slot)
        slot._auto_resume_stamps.append(time.time())

        written = asyncio.run(restart_resume._commit_budget(state, slot))

        # The guard saw the emptied list under the lock: the spent stamp this
        # write carried never reached the line, and the reset is still pending.
        assert written is False
        assert log.calls[0]["guard"] is False
        assert slot._auto_resume_stamps == []
        assert slot._auto_resume_reset_pending is True

    @pytest.mark.asyncio
    async def test_a_reset_that_landed_after_the_write_stays_pending(self, monkeypatch):
        slot = _cut_off(_slot())
        state = _mock_state(slot)
        state.conversation_log = log = _FakeLog(True)
        slot._auto_resume_stamps.append(time.time())
        monkeypatch.setattr(
            restart_resume, "run_to_completion", _person_resets_as_the_write_lands(slot)
        )

        written = await restart_resume._commit_budget(state, slot)

        # The write landed with the spent stamp; the person's reset came after it.
        assert written is True
        assert len(log.calls[0]["fields"]["auto_resume_stamps"]) == 1
        assert slot._auto_resume_stamps == []
        # So the person's own turn still has the empty list to carry.
        assert slot._auto_resume_reset_pending is True

    @pytest.mark.asyncio
    async def test_a_person_who_acts_during_the_commit_keeps_their_reset(
        self, quiet_dispatch, monkeypatch
    ):
        slot = _cut_off(_slot())
        state = _mock_state(slot)
        state.conversation_log = _FakeLog(True)
        monkeypatch.setattr(
            restart_resume, "run_to_completion", _person_resets_as_the_write_lands(slot)
        )

        ran = await restart_resume._dispatch(
            state, slot, cause="restart", limit=5, window_secs=3600
        )

        # The person's action moved the epoch, so the queue insert refused; the
        # budget they reset is empty in memory and still marked for their turn.
        assert ran is False
        assert not any(m.get("role") == "inject" for m in slot.messages)
        assert slot._auto_resume_stamps == []
        assert slot._auto_resume_reset_pending is True

    @pytest.mark.asyncio
    async def test_the_switch_turned_off_during_the_commit_refuses_the_insert(
        self, quiet_dispatch, monkeypatch
    ):
        # Both passes read the switch before entering the dispatcher, then await
        # the budget write. A setting turned off in that window moves no epoch,
        # so the pre-insert hook has to read the switch itself (GPT review, rev
        # 45): refused under the lock, nothing queued, the stamp taken back.
        switch = [True]
        monkeypatch.setattr(restart_resume, "_switch_on_now", lambda: switch[0])
        slot = _cut_off(_slot())
        state = _mock_state(slot)
        state.conversation_log = _FakeLog(True)
        real = restart_resume.run_to_completion

        async def _land_then_switch_off(aw):
            result = await real(aw)
            switch[0] = False
            return result

        monkeypatch.setattr(restart_resume, "run_to_completion", _land_then_switch_off)

        ran = await restart_resume._dispatch(state, slot, cause="error", limit=5, window_secs=3600)

        assert ran is False
        assert not any(m.get("role") == "inject" for m in slot.messages)
        assert slot._auto_resume_stamps == []

    @pytest.mark.asyncio
    async def test_the_marker_keeps_a_reset_pending_that_landed_under_its_write(self, monkeypatch):
        from kiro_crew.dashboard import chat_runner

        slot = _slot()
        slot.append("user", "do the thing", "msg msg-u")
        state = _mock_state(slot)
        state.conversation_log = log = _FakeLog(True)
        slot._auto_resume_stamps = [time.time()]
        # The marker's names resolve in the facade's namespace.
        monkeypatch.setattr(
            chat_runner, "run_to_completion", _person_resets_as_the_write_lands(slot)
        )

        await chat_runner._begin_local_turn_marker(state, slot, 1)

        # This marker carried the spent stamp, not the list the person left.
        [call] = log.calls
        assert len(call["fields"]["auto_resume_stamps"]) == 1
        assert slot._auto_resume_stamps == []
        assert slot._auto_resume_reset_pending is True


def _row_first_log(tmp_path, slot: _ChatSlot) -> tuple[ConversationLog, Path]:
    """A real conversation log whose transcript for *slot* is a legacy row-first file.

    Its first line is JSON but not a metadata record, so ``update_metadata_if``
    leaves it alone: the merge is a no-op the log reports as success unless the
    caller requires a write. Returns the log and the transcript's path.
    """
    log = ConversationLog(base_dir=tmp_path / "sessions")
    log.init()
    key = slot_history_key(slot)
    with history_mod.allow_on_loop_persist():
        log.append(key, "user", "hi")
    path = log._path(key)
    lines = path.read_text(encoding="utf-8").splitlines()
    lines[0] = json.dumps(["legacy-row"])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log._meta_cache.pop(key, None)
    return log, path


class TestSkippedWriteIsNotAWrite:
    """A merge the log skipped is not a persisted stamp, so it is not a resume.

    ``update_metadata_if`` answers True to a passing guard even when it wrote
    nothing -- a legacy row-first transcript keeps its first line -- unless the
    caller passes ``require_write``. The budget commit must, or the resume
    dispatches on a stamp that exists nowhere on disk and the next boot cannot
    count it against the budget.
    """

    def test_a_row_first_transcript_refuses_the_budget_commit(self, tmp_path):
        slot = _cut_off(_slot())
        state = _mock_state(slot)
        state.conversation_log, path = _row_first_log(tmp_path, slot)
        slot._auto_resume_stamps.append(time.time())
        slot._auto_resume_reset_pending = True

        written = asyncio.run(restart_resume._commit_budget(state, slot))

        assert written is False
        # Nothing landed, so the person's reset is still owed to the line.
        assert slot._auto_resume_reset_pending is True
        # The legacy first line is still there, untouched.
        assert json.loads(path.read_text(encoding="utf-8").splitlines()[0]) == ["legacy-row"]

    @pytest.mark.asyncio
    async def test_a_row_first_transcript_refuses_the_resume(self, quiet_dispatch, tmp_path):
        slot = _cut_off(_slot())
        state = _mock_state(slot)
        state.conversation_log, _path = _row_first_log(tmp_path, slot)

        ran = await restart_resume._dispatch(state, slot, cause="error", limit=5, window_secs=3600)

        assert ran is False
        assert quiet_dispatch == []
        # Nothing ran, so nothing was spent -- in memory either.
        assert slot._auto_resume_stamps == []


def _forced_save_answering(answer: object, calls: list[dict]):
    """A ``save_slot_off_loop`` double with the real function's best-effort contract.

    Records its keyword arguments. Returns *answer* when it is a bool; when it
    is an exception, raises it to a non-best-effort caller and -- as the real
    save does -- swallows it for a best-effort one, marks the slot dirty and
    answers ``True``, which is the answer the old marker took for a landed write.
    """

    async def _save(state, slot, *args, **kwargs):
        calls.append(kwargs)
        if isinstance(answer, BaseException):
            if kwargs.get("best_effort", True):
                slot._dirty = True
                return True
            raise answer
        return answer

    return _save


class TestMarkerFallbackSave:
    """The reset clears only after the forced-save fallback confirms its write."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "answer",
        [OSError("disk full"), False],
        ids=["save_failed", "save_refused"],
    )
    async def test_an_unconfirmed_fallback_keeps_the_reset_pending(self, monkeypatch, answer):
        from kiro_crew.dashboard import chat_runner

        slot = _slot()
        slot.append("user", "do the thing", "msg msg-u")
        state = _mock_state(slot)
        # No metadata line yet: the merge finds nothing and the forced save is the fallback.
        state.conversation_log = _FakeLog(False)
        slot._auto_resume_stamps = [time.time()]
        restart_resume.reset_budget(slot)
        saves: list[dict] = []
        monkeypatch.setattr(
            chat_runner, "save_slot_off_loop", _forced_save_answering(answer, saves)
        )

        await chat_runner._begin_local_turn_marker(state, slot, 1)

        # The turn is admitted either way; the empty list is not on disk, so the
        # next marker must still carry it.
        assert slot._auto_resume_reset_pending is True
        assert slot._dirty is True
        [save] = saves
        assert save["best_effort"] is False
        assert save["force"] is True

    @pytest.mark.asyncio
    async def test_a_confirmed_fallback_clears_the_reset(self, monkeypatch):
        from kiro_crew.dashboard import chat_runner

        slot = _slot()
        slot.append("user", "do the thing", "msg msg-u")
        state = _mock_state(slot)
        state.conversation_log = _FakeLog(False)
        slot._auto_resume_stamps = [time.time()]
        restart_resume.reset_budget(slot)
        saves: list[dict] = []
        monkeypatch.setattr(chat_runner, "save_slot_off_loop", _forced_save_answering(True, saves))

        await chat_runner._begin_local_turn_marker(state, slot, 1)

        assert len(saves) == 1
        assert slot._auto_resume_reset_pending is False
