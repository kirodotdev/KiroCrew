"""Tests for the session-summary generator.

No real model is ever called: ``run_bg_oneliner`` is patched at the module
boundary, so these assert the gating, the caching, the failure containment and
the trap wiring rather than any model behavior.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from chat_test_helpers import move_transcript_past

from kiro_crew.config.loader import KiroCrewConfig, SessionSummaryConfig
from kiro_crew.dashboard import chat_summary
from kiro_crew.dashboard.chat_persistence import _save_slot_to_history
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.dashboard.state import _ChatSlot
from kiro_crew.history import ConversationLog
from kiro_crew.session_summary import (
    count_user_turns,
    count_user_turns_in_records,
    extract_turns,
)


def _make_slot(messages=None, memory_mode="default"):
    """A real _ChatSlot: the generator resolves its transcript key, which a
    stand-in object cannot answer for."""
    slot = _ChatSlot("s1")
    slot.messages = messages if messages is not None else []
    slot.memory_mode = memory_mode
    slot._last_stop_reason = "end_turn"
    return slot


def _write_transcript(log, hkey, messages):
    """Persist *messages* to the on-disk transcript the generator reads.

    The generator deliberately reads the full chained history from disk (a
    restored slot keeps only the most recent 500 messages in memory), so tests
    must stage their fixture turns in the log, not on ``slot.messages``.
    """
    for m in messages:
        log.append(hkey, m["role"], m["content"])


class _FakeState:
    def __init__(self, log):
        self.conversation_log = log
        self.sessions = object()
        self.pushed: list[str] = []
        self.hkey = ""

    def push_session_summary(self, key):
        self.pushed.append(key)

    def flush_slot_now(self, slot):
        """Mirror DashboardState.flush_slot_now: write, then clear the bit.

        Clearing matters to what the pass under test relies on — a flush that
        left the slot dirty would be re-saved by the 5s loop and move the mtime
        again, so a stub that only wrote would hide the very bug this exercises.
        """
        if not slot._dirty or not slot.messages:
            return
        gen = slot._dirty_gen
        _save_slot_to_history(self, slot)
        if slot._dirty_gen == gen:
            slot._dirty = False


def _cfg(**overrides):
    cfg = KiroCrewConfig()
    cfg.session_summary = SessionSummaryConfig(**{"enabled": True, **overrides})
    return cfg


def _turns(n=3):
    out = []
    for i in range(n):
        out.append({"role": "user", "content": f"request {i}"})
        out.append({"role": "assistant", "content": f"reply {i}"})
    return out


_GOOD_REPLY = json.dumps(
    {
        "intents": [
            {
                "title": "set up auth",
                "ranges": [[1, 3]],
                "status": "completed",
                "verified": False,
                "initial_intent": "wire up login",
                "progress": ["login works locally"],
                "next_steps": [{"what": "try it in staging", "why": "never run there"}],
            }
        ],
        "constraints": ["restart the worker after a config change"],
    }
)


@pytest.fixture
def env(tmp_path):
    log = ConversationLog(base_dir=tmp_path)
    slot = _make_slot(_turns())
    state = _FakeState(log)
    state.hkey = slot_history_key(slot)
    _write_transcript(log, state.hkey, _turns())
    return state, slot


def _stub_llm(monkeypatch, reply, calls=None):
    async def fake(*args, **kwargs):
        if calls is not None:
            calls.append(kwargs.get("model"))
        return reply

    monkeypatch.setattr(chat_summary, "run_bg_oneliner", fake)


class TestGating:
    pytestmark = pytest.mark.asyncio

    async def test_disabled_returns_false_without_calling_the_model(self, env, monkeypatch):
        state, slot = env
        called = []
        _stub_llm(monkeypatch, _GOOD_REPLY, called)
        ok = await chat_summary.generate_session_summary(state, slot, cfg=KiroCrewConfig())
        assert ok is False
        assert called == []

    async def test_enabled_generates_and_caches(self, env, monkeypatch):
        state, slot = env
        _stub_llm(monkeypatch, _GOOD_REPLY)
        assert await chat_summary.generate_session_summary(state, slot, cfg=_cfg()) is True
        cached = state.conversation_log.get_cached_intent_summary(state.hkey)
        assert cached["intents"][0]["title"] == "set up auth"
        assert cached["constraints"] == ["restart the worker after a config change"]

    async def test_incognito_is_refused_before_any_model_call(self, env, monkeypatch):
        state, slot = env
        slot.memory_mode = "incognito"
        called = []
        _stub_llm(monkeypatch, _GOOD_REPLY, called)
        assert await chat_summary.generate_session_summary(state, slot, cfg=_cfg()) is False
        assert called == []

    async def test_mixed_case_incognito_is_refused_too(self, env, monkeypatch):
        """A hand-edited transcript header is not bound by the API's
        validation, so the privacy refusal must be case-insensitive."""
        state, slot = env
        slot.memory_mode = "Incognito"
        called = []
        _stub_llm(monkeypatch, _GOOD_REPLY, called)
        assert await chat_summary.generate_session_summary(state, slot, cfg=_cfg()) is False
        assert called == []

    async def test_temporary_is_refused_before_any_model_call(self, env, monkeypatch):
        """A temporary session's transcript is discarded; persisting a summary
        of it would leave conversation content on disk after the conversation
        itself is gone (mirrors history.INCOGNITO_MEMORY_MODES)."""
        state, slot = env
        slot.memory_mode = "temporary"
        called = []
        _stub_llm(monkeypatch, _GOOD_REPLY, called)
        assert await chat_summary.generate_session_summary(state, slot, cfg=_cfg()) is False
        assert called == []
        assert state.conversation_log.get_cached_intent_summary(state.hkey) is None

    async def test_an_unclean_stop_reason_skips(self, env, monkeypatch):
        """A turn cut short by a timeout or stall did not really finish."""
        state, slot = env
        called = []
        _stub_llm(monkeypatch, _GOOD_REPLY, called)
        for reason in ("timeout", "tool_stall", "error: cancel unacked", "stale_recover"):
            slot._last_stop_reason = reason
            assert await chat_summary.generate_session_summary(state, slot, cfg=_cfg()) is False
        assert called == []

    async def test_a_missing_stop_reason_skips(self, env, monkeypatch):
        """The marker is cleared at turn start, so empty means the turn never
        reached EVENT_COMPLETE (ACP failure, transport drop) -- summarizing
        would cache an incomplete turn as if it finished cleanly."""
        state, slot = env
        slot._last_stop_reason = ""
        called = []
        _stub_llm(monkeypatch, _GOOD_REPLY, called)
        assert await chat_summary.generate_session_summary(state, slot, cfg=_cfg()) is False
        assert called == []

    async def test_summarizes_the_full_disk_transcript_not_the_memory_tail(
        self, env, monkeypatch
    ):
        """A restored slot keeps only the most recent messages in memory;
        generation must read the full transcript from disk or earlier intents
        vanish from the regenerated summary."""
        state, slot = env
        # Disk carries an early request the in-memory tail no longer holds.
        slot.messages = [{"role": "user", "content": "request 2"}]
        prompts: list[str] = []

        async def fake(_sessions, prompt, **kwargs):
            prompts.append(prompt)
            return _GOOD_REPLY

        monkeypatch.setattr(chat_summary, "run_bg_oneliner", fake)
        assert await chat_summary.generate_session_summary(state, slot, cfg=_cfg()) is True
        assert "request 0" in prompts[0]  # only on disk, not in slot.messages

    async def test_the_turn_being_summarised_is_flushed_before_the_signature(
        self, env, monkeypatch
    ):
        """The reason the panel stayed empty on every real session.

        ``_ChatSlot.append`` marks the slot dirty; the bytes reach disk on the 5s
        flush loop. This pass is dispatched from ``_finish_queue_cycle`` in the
        same block that appended the turn's final assistant message, so the write
        lands AFTER dispatch. A signature captured before it is stale the moment
        the flush fires, and ``set_cached_intent_summary`` then refuses the
        payload — on every turn, forever, since the next turn repeats it.

        Here the slot carries a turn its transcript does not, which is exactly
        the dirty-slot state at dispatch. The pass must flush it, then stamp the
        post-flush mtime.
        """
        state, slot = env
        log = state.conversation_log
        # A turn the transcript does not have yet: dirty, unflushed. Assigning
        # .messages bypasses _ChatSlot.append, which is what sets the bit in
        # production, so set it explicitly — otherwise the flush is skipped and
        # this test cannot distinguish the two behaviours.
        slot.messages = _turns() + [
            {"role": "user", "content": "the newest ask"},
            {"role": "assistant", "content": "the reply that has not reached disk"},
        ]
        slot._dirty = True
        _stub_llm(monkeypatch, _GOOD_REPLY)

        assert await chat_summary.generate_session_summary(state, slot, cfg=_cfg()) is True
        # Stored AND readable as current: the signature matches the transcript
        # that exists after the flush, so the strict reader serves it.
        assert log.get_cached_intent_summary(state.hkey) is not None
        # The unflushed turn reached disk, so the summary describes the whole
        # session rather than everything except the turn that triggered it.
        assert "the newest ask" in "".join(
            m.get("content", "") for m in log.read_messages_chained(state.hkey)
        )

    async def test_the_flush_loop_firing_mid_model_call_does_not_lose_the_summary(
        self, env, monkeypatch
    ):
        """The production symptom, reproduced.

        The 5s flush loop is what actually invalidated the signature: it fires
        while the model call is in flight, writes the dirty slot, and advances
        the mtime past a signature captured before dispatch. The write guard then
        refuses the payload and the panel never fills.

        The stub below flushes the slot mid-call, standing in for that loop.
        Flushing first makes it a no-op — the slot is already clean — so the
        signature still matches at write time.
        """
        state, slot = env
        log = state.conversation_log
        slot.messages = _turns() + [
            {"role": "user", "content": "the newest ask"},
            {"role": "assistant", "content": "the reply that has not reached disk"},
        ]
        # Assigning .messages bypasses _ChatSlot.append, which is what sets the
        # dirty bit in production. Set it explicitly or the flush stand-in below
        # is inert and the test passes whether or not the fix is present.
        slot._dirty = True

        async def flush_then_reply(*args, **kwargs):
            # Stand-in for _flush_dirty_slots firing during the model call. It
            # mirrors that loop's guard: a CLEAN slot is skipped. So once the
            # pass has flushed, this writes nothing and the signature holds;
            # without the flush the slot is still dirty here and the write lands
            # mid-call, moving the mtime out from under the captured signature.
            if slot._dirty:
                sig_before = log.session_mtime(state.hkey)
                _save_slot_to_history(state, slot, force=True)
                move_transcript_past(log, state.hkey, sig_before)
            return _GOOD_REPLY

        monkeypatch.setattr(chat_summary, "run_bg_oneliner", flush_then_reply)

        assert await chat_summary.generate_session_summary(state, slot, cfg=_cfg()) is True
        assert log.get_cached_intent_summary(state.hkey) is not None

    async def test_an_append_during_the_transcript_read_refuses_the_write(
        self, env, monkeypatch
    ):
        """The signature is captured BEFORE the transcript read, so a message
        landing during the read advances the mtime past the captured sig and
        the write guard refuses the payload -- an incomplete summary must never
        be stored and served as fresh."""
        state, slot = env
        log = state.conversation_log
        real_read = log.read_messages_chained
        # Production captures the signature at generation start; the racing
        # append below must provably land PAST it, not merely after it in time.
        pre_read_sig = log.session_mtime(state.hkey)

        def racing_read(key):
            records = real_read(key)
            # A concurrent turn appends while generation is reading.
            log.append(key, "user", "landed mid-read")
            move_transcript_past(log, key, pre_read_sig)  # don't rely on the OS tick (#2981)
            return records

        monkeypatch.setattr(log, "read_messages_chained", racing_read)
        _stub_llm(monkeypatch, _GOOD_REPLY)
        assert await chat_summary.generate_session_summary(state, slot, cfg=_cfg()) is False
        assert log.get_cached_intent_summary(state.hkey) is None

    async def test_a_turn_starting_during_the_model_call_refuses_the_forced_write(
        self, env, monkeypatch
    ):
        """The liveness gate runs before the model call, so a turn that starts
        DURING it cannot be caught by that gate -- the write guard is what closes
        the window, and it closes it for the forced path too.

        This is the longest unguarded window in a pass (a model call runs for tens
        of seconds while no lock is held), and it is the one a reader assumes the
        `running` check covers. It does not: the signature captured at generation
        start no longer matches once the racing turn's message is on disk, so
        `set_cached_intent_summary` refuses the payload instead of publishing a
        summary that predates the turn under a current signature. The guard is
        strictly stronger than re-checking `slot.running` would be, because it
        catches an append from ANY writer, not just this slot's own turn.
        """
        state, slot = env
        log = state.conversation_log
        sig_at_start = log.session_mtime(state.hkey)
        raced: list[str] = []

        async def racing_model_call(*args, **kwargs):
            # Another client starts a turn while the model is thinking. `running`
            # is a derived property (`task is not None and not task.done()`), so
            # the turn is staged the way production stages it -- assigning to
            # `running` would raise, and the generator's broad handler would
            # swallow that into the same `False` this test asserts.
            slot.task = asyncio.get_running_loop().create_future()
            log.append(state.hkey, "user", "a turn that started mid-generation")
            move_transcript_past(log, state.hkey, sig_at_start)
            raced.append("yes")
            return _GOOD_REPLY

        monkeypatch.setattr(chat_summary, "run_bg_oneliner", racing_model_call)
        assert (
            await chat_summary.generate_session_summary(state, slot, cfg=_cfg(), force=True)
            is False
        )
        # The race must have actually happened, the slot must really read as
        # running, and the transcript must really have moved past the captured
        # signature -- otherwise this test would pass for a reason unrelated to
        # the write guard.
        assert raced == ["yes"]
        assert slot.running is True
        assert log.session_mtime(state.hkey) != sig_at_start
        assert log.get_cached_intent_summary(state.hkey) is None

    async def test_an_unflushed_turn_during_the_model_call_refuses_the_write(
        self, env, monkeypatch
    ):
        """The mtime guard only sees DISK. A turn that starts during the model
        call and has not reached the 5s flush leaves the transcript's mtime
        untouched, so the signature still matches and the write would be accepted
        -- then BROADCAST as current, showing a summary that omits that turn until
        the flush finally moves the mtime.

        So the in-memory state is re-validated before publishing: `_dirty` means
        an append exists that disk has not seen yet. This is the half of the race
        the signature cannot cover, and it is the case a signature-only argument
        wrongly calls benign.
        """
        state, slot = env
        log = state.conversation_log
        sig_at_start = log.session_mtime(state.hkey)
        raced: list[str] = []

        async def racing_model_call(*args, **kwargs):
            # An append that is only in memory: the slot goes dirty and the
            # transcript on disk is deliberately NOT touched.
            slot.messages = list(slot.messages) + [
                {"role": "user", "content": "typed while the model was thinking"}
            ]
            slot._dirty = True
            raced.append("yes")
            return _GOOD_REPLY

        monkeypatch.setattr(chat_summary, "run_bg_oneliner", racing_model_call)
        assert (
            await chat_summary.generate_session_summary(state, slot, cfg=_cfg(), force=True)
            is False
        )
        assert raced == ["yes"]
        # The point of the test: the signature is STILL VALID, so the mtime guard
        # would have let this through. Only the in-memory check refuses it.
        assert log.session_mtime(state.hkey) == sig_at_start
        assert slot._dirty is True
        assert log.get_cached_intent_summary(state.hkey) is None

    async def test_two_concurrent_passes_spend_only_one_model_call(self, env, monkeypatch):
        """The in-flight guard is taken before the first await, so a second pass
        cannot slip past it while the first is still awaiting the flush, the mtime
        or the transcript read.

        On-demand generation is what makes this reachable: two clicks from two
        clients (or a click racing a turn-end pass) are concurrent callers of a
        function that previously set its marker only after four awaits. The
        signature guard made the outcome safe but not free -- the second pass had
        already paid for a model call by the time its write was refused.
        """
        state, slot = env
        calls: list[str] = []
        release = asyncio.Event()

        async def slow_model_call(*args, **kwargs):
            calls.append("call")
            await release.wait()
            return _GOOD_REPLY

        monkeypatch.setattr(chat_summary, "run_bg_oneliner", slow_model_call)
        first = asyncio.create_task(
            chat_summary.generate_session_summary(state, slot, cfg=_cfg(), force=True)
        )
        # Hand control to the first pass so it reaches the model call and is
        # holding the guard, which is the state a second click arrives in.
        while not calls:
            await asyncio.sleep(0)
        second = await chat_summary.generate_session_summary(state, slot, cfg=_cfg(), force=True)
        release.set()

        assert second is False
        assert await first is True
        assert calls == ["call"]

    async def test_too_few_user_turns_skips(self, env, monkeypatch):
        state, slot = env
        log = state.conversation_log
        log.delete_session(state.hkey)
        one_turn = [{"role": "user", "content": "just one"}]
        _write_transcript(log, state.hkey, one_turn)
        # The slot's in-memory window is the same conversation as its transcript:
        # the pass flushes the window to disk before counting, so a fixture that
        # left a longer window here would be counting a different session.
        slot.messages = list(one_turn)
        called = []
        _stub_llm(monkeypatch, _GOOD_REPLY, called)
        ok = await chat_summary.generate_session_summary(state, slot, cfg=_cfg(min_user_turns=2))
        assert ok is False
        assert called == []

    async def test_in_flight_guard_blocks_a_second_pass(self, env, monkeypatch):
        state, slot = env
        slot._summary_in_flight = True
        called = []
        _stub_llm(monkeypatch, _GOOD_REPLY, called)
        assert await chat_summary.generate_session_summary(state, slot, cfg=_cfg()) is False
        assert called == []

    async def test_cadence_skips_until_enough_turns_pass(self, env, monkeypatch):
        state, slot = env
        slot._summary_turn_mark = 3
        called = []
        _stub_llm(monkeypatch, _GOOD_REPLY, called)
        ok = await chat_summary.generate_session_summary(
            state, slot, cfg=_cfg(regenerate_after_turns=5)
        )
        assert ok is False
        assert called == []

    async def test_automation_rows_do_not_count_toward_the_turn_minimum(self, env, monkeypatch):
        state, slot = env
        log = state.conversation_log
        log.delete_session(state.hkey)
        rows = [
            {"role": "user", "content": "real ask"},
            {"role": "user", "content": "[Subagent completion event] done"},
            {"role": "user", "content": '[Cron notification from "x"] fired'},
        ]
        _write_transcript(log, state.hkey, rows)
        # Window and transcript are one conversation — see the note above.
        slot.messages = list(rows)
        called = []
        _stub_llm(monkeypatch, _GOOD_REPLY, called)
        ok = await chat_summary.generate_session_summary(state, slot, cfg=_cfg(min_user_turns=2))
        assert ok is False
        assert called == []


class TestCaching:
    pytestmark = pytest.mark.asyncio

    async def test_an_unchanged_transcript_is_not_regenerated(self, env, monkeypatch):
        state, slot = env
        called = []
        _stub_llm(monkeypatch, _GOOD_REPLY, called)
        assert await chat_summary.generate_session_summary(state, slot, cfg=_cfg()) is True
        assert len(called) == 1
        # Second pass: the sidecar signature still matches, so no model call.
        assert await chat_summary.generate_session_summary(state, slot, cfg=_cfg()) is False
        assert len(called) == 1

    async def test_a_new_message_makes_it_regenerate(self, env, monkeypatch):
        state, slot = env
        called = []
        _stub_llm(monkeypatch, _GOOD_REPLY, called)
        await chat_summary.generate_session_summary(state, slot, cfg=_cfg())
        log = state.conversation_log
        sig = log.session_mtime(state.hkey)  # what pass 1 stamped the sidecar with
        log.append(state.hkey, "user", "another")
        move_transcript_past(log, state.hkey, sig)  # don't rely on the OS tick (#2981)
        slot._summary_turn_mark = 0
        assert await chat_summary.generate_session_summary(state, slot, cfg=_cfg()) is True
        assert len(called) == 2

    async def test_a_successful_pass_pushes_to_the_client(self, env, monkeypatch):
        state, slot = env
        _stub_llm(monkeypatch, _GOOD_REPLY)
        await chat_summary.generate_session_summary(state, slot, cfg=_cfg())
        # The SLOT key, not the transcript key the sidecar is stored under: the
        # dashboard addresses slots by slot key, so broadcasting the transcript
        # key would name an id no client holds a cache entry for.
        assert state.pushed == [slot.key]
        assert state.pushed != [state.hkey]

    async def test_stored_payload_carries_display_metadata(self, env, monkeypatch):
        state, slot = env
        _stub_llm(monkeypatch, _GOOD_REPLY)
        await chat_summary.generate_session_summary(state, slot, cfg=_cfg())
        cached = state.conversation_log.get_cached_intent_summary(state.hkey)
        assert cached["generated_at"] > 0
        assert cached["user_turns"] == 3


class TestFailureContainment:
    pytestmark = pytest.mark.asyncio

    async def test_a_model_error_is_swallowed(self, env, monkeypatch):
        state, slot = env

        async def boom(*a, **k):
            raise RuntimeError("provider exploded")

        monkeypatch.setattr(chat_summary, "run_bg_oneliner", boom)
        assert await chat_summary.generate_session_summary(state, slot, cfg=_cfg()) is False
        assert slot._summary_in_flight is False

    async def test_unparseable_reply_leaves_no_cache_entry(self, env, monkeypatch):
        state, slot = env
        _stub_llm(monkeypatch, "I'm afraid I can't do that")
        assert await chat_summary.generate_session_summary(state, slot, cfg=_cfg()) is False
        assert state.conversation_log.get_cached_intent_summary(state.hkey) is None

    async def test_empty_intents_do_not_replace_a_good_summary(self, env, monkeypatch):
        state, slot = env
        _stub_llm(monkeypatch, _GOOD_REPLY)
        await chat_summary.generate_session_summary(state, slot, cfg=_cfg())
        state.conversation_log.append(state.hkey, "user", "more")
        slot._summary_turn_mark = 0
        _stub_llm(monkeypatch, json.dumps({"intents": []}))
        assert await chat_summary.generate_session_summary(state, slot, cfg=_cfg()) is False

    async def test_the_guard_is_released_on_every_path(self, env, monkeypatch):
        state, slot = env
        _stub_llm(monkeypatch, "garbage")
        await chat_summary.generate_session_summary(state, slot, cfg=_cfg())
        assert slot._summary_in_flight is False


class TestReplyParsing:
    pytestmark = pytest.mark.asyncio

    async def test_a_fenced_json_block_is_accepted(self, env, monkeypatch):
        state, slot = env
        _stub_llm(monkeypatch, f"```json\n{_GOOD_REPLY}\n```")
        assert await chat_summary.generate_session_summary(state, slot, cfg=_cfg()) is True

    async def test_prose_around_the_object_is_tolerated(self, env, monkeypatch):
        state, slot = env
        _stub_llm(monkeypatch, f"Here you go:\n{_GOOD_REPLY}\nHope that helps.")
        assert await chat_summary.generate_session_summary(state, slot, cfg=_cfg()) is True

    async def test_caps_from_config_are_applied_to_the_reply(self, env, monkeypatch):
        state, slot = env
        many = {
            "intents": [
                {"title": f"i{n}", "ranges": [[n, n]], "status": "active"} for n in range(1, 10)
            ],
            "constraints": ["a", "b", "c", "d", "e", "f"],
        }
        _stub_llm(monkeypatch, json.dumps(many))
        await chat_summary.generate_session_summary(
            state, slot, cfg=_cfg(max_intents=2, max_constraints=1)
        )
        cached = state.conversation_log.get_cached_intent_summary(state.hkey)
        assert len(cached["intents"]) == 2
        assert len(cached["constraints"]) == 1


class TestPromptCarriesTheTraps:
    """The prompt is the only place the judgement traps are enforced."""

    def test_every_judgement_trap_is_named(self):
        p = chat_summary._PROMPT
        for phrase in (
            "automation, not the user",
            "resend",
            "RETRACT",
            "DECLINED",
            "merged is not work being verified",
            "not a work order",
            "stale",
            "compaction",
            "title",
        ):
            assert phrase.lower() in p.lower(), f"prompt does not mention {phrase!r}"

    def test_boundary_signals_are_named(self):
        p = chat_summary._PROMPT.lower()
        assert "commit" in p and "merge" in p
        assert "timestamp" in p
        assert "correction" in p

    def test_progress_is_specified_as_a_runbook(self):
        assert "RUNBOOK" in chat_summary._PROMPT

    def test_next_steps_are_specified_as_inferences(self):
        assert "inferences" in chat_summary._PROMPT

    def test_the_two_status_axes_are_explained(self):
        p = chat_summary._PROMPT
        assert '"verified"' in p and "independent of status" in p


class _GateSlot:
    """The attributes ``_should_summarize`` reads, and nothing else.

    The gate function is pure and reads its slot through ``getattr``, so the
    force matrix below can be asserted without a transcript, a log or an event
    loop -- which is the point: these are the decisions, isolated from the IO
    the generator wraps around them.
    """

    def __init__(
        self,
        *,
        stop="end_turn",
        in_flight=False,
        memory_mode="default",
        mark=0,
        running=False,
    ):
        self._last_stop_reason = stop
        self._summary_in_flight = in_flight
        self.memory_mode = memory_mode
        self._summary_turn_mark = mark
        self.running = running


class TestShouldSummarizeForceMatrix:
    """``force`` lifts EXACTLY two gates. Which two is the whole contract."""

    def test_force_lifts_an_unclean_stop_reason(self):
        for reason in ("timeout", "tool_stall", "error: cancel unacked", "stale_recover"):
            slot = _GateSlot(stop=reason)
            assert chat_summary._should_summarize(_cfg(), slot, 3) == f"stop_reason:{reason}"
            assert chat_summary._should_summarize(_cfg(), slot, 3, force=True) == ""

    def test_force_lifts_a_missing_stop_reason(self):
        """The case the on-demand button exists for: an idle session restored in
        a later process carries no stop reason at all, so every session a person
        opens to catch up on is refused by the turn-end gate."""
        slot = _GateSlot(stop="")
        assert chat_summary._should_summarize(_cfg(), slot, 3) == "stop_reason:missing"
        assert chat_summary._should_summarize(_cfg(), slot, 3, force=True) == ""

    def test_force_lifts_the_cadence_gate(self):
        slot = _GateSlot(mark=3)
        cfg = _cfg(regenerate_after_turns=5)
        assert chat_summary._should_summarize(cfg, slot, 3) == "cadence"
        assert chat_summary._should_summarize(cfg, slot, 3, force=True) == ""

    def test_force_does_not_lift_disabled(self):
        """The feature's off switch is not a spending gate, so consent cannot
        override it."""
        cfg = KiroCrewConfig()  # session_summary.enabled defaults to False
        slot = _GateSlot()
        assert chat_summary._should_summarize(cfg, slot, 3, force=True) == "disabled"

    def test_force_does_not_lift_in_flight(self):
        slot = _GateSlot(in_flight=True)
        assert chat_summary._should_summarize(_cfg(), slot, 3, force=True) == "in_flight"

    def test_force_does_not_lift_a_turn_in_flight(self):
        """A streaming turn has no boundary worth caching. Consent to spend is
        not consent to store a partial transcript as the whole session -- and the
        sidecar's mtime signature would then serve that partial summary as
        CURRENT until the next append, so the gate holds under force."""
        slot = _GateSlot(running=True)
        assert chat_summary._should_summarize(_cfg(), slot, 3) == "running"
        assert chat_summary._should_summarize(_cfg(), slot, 3, force=True) == "running"
        # Also on the cheap slot-level call, before any transcript read.
        assert chat_summary._should_summarize(_cfg(), slot, None, force=True) == "running"

    def test_the_running_gate_reads_liveness_not_the_stop_reason(self):
        """Why liveness is consulted directly: the stop marker is cleared at turn
        start, so an EMPTY stop reason describes both a turn streaming right now
        and the idle restored session the on-demand pass exists to serve. Only
        ``running`` separates them, and force must keep the first refused while
        letting the second through."""
        streaming = _GateSlot(stop="", running=True)
        assert chat_summary._should_summarize(_cfg(), streaming, 3, force=True) == "running"
        idle = _GateSlot(stop="", running=False)
        assert chat_summary._should_summarize(_cfg(), idle, 3, force=True) == ""

    def test_force_does_not_lift_incognito_or_temporary(self):
        for mode in ("incognito", "Incognito", "temporary"):
            slot = _GateSlot(memory_mode=mode)
            assert chat_summary._should_summarize(_cfg(), slot, 3, force=True) == "memory_mode"

    def test_force_does_not_lift_too_few_turns(self):
        slot = _GateSlot()
        cfg = _cfg(min_user_turns=2)
        assert chat_summary._should_summarize(cfg, slot, 1, force=True) == "too_few_turns"

    def test_the_disabled_gate_precedes_everything_else(self):
        """Order matters for the reason string the panel is told: a disabled
        feature must not report itself as in-flight or as too short."""
        cfg = KiroCrewConfig()
        slot = _GateSlot(stop="", in_flight=True, memory_mode="incognito")
        assert chat_summary._should_summarize(cfg, slot, 0, force=True) == "disabled"

    def test_a_slot_level_only_call_defers_the_turn_gates(self):
        """``user_turns=None`` runs only the gates that cost no IO."""
        slot = _GateSlot(mark=3)
        cfg = _cfg(min_user_turns=99, regenerate_after_turns=99)
        assert chat_summary._should_summarize(cfg, slot, None) == ""
        assert chat_summary._should_summarize(cfg, slot, None, force=True) == ""


class TestCountUserTurnsInRecords:
    """The cheap count the panel's affordance state is derived from.

    It must agree with ``extract_turns`` + ``count_user_turns`` on what a user
    turn IS, or the button offered by the panel and the gate enforced by the
    generator would disagree about the same session.
    """

    def test_counts_only_user_rows(self):
        records = [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "reply"},
            {"role": "user", "content": "second"},
            {"role": "assistant", "content": "reply"},
        ]
        assert count_user_turns_in_records(records) == 2

    def test_ignores_rows_that_are_not_dicts(self):
        records = ["a string", None, 7, ["user", "hi"], {"role": "user", "content": "real"}]
        assert count_user_turns_in_records(records) == 1

    def test_ignores_blank_and_non_string_content(self):
        records = [
            {"role": "user", "content": ""},
            {"role": "user", "content": "   \n\t "},
            {"role": "user", "content": None},
            {"role": "user", "content": 42},
            {"role": "user"},
            {"role": "user", "content": "real"},
        ]
        assert count_user_turns_in_records(records) == 1

    def test_ignores_injected_automation_rows(self):
        """Automation posts under role "user". Counting one invents a goal the
        person never had -- and would offer a summary button on a session whose
        only "turns" were cron notifications."""
        records = [
            {"role": "user", "content": '[Cron notification from "morning digest"] fired'},
            {"role": "user", "content": "[Subagent completion event] Agent a1 completed"},
            {"role": "user", "content": "[Subagent batch completion event] 3 agents finished"},
            {"role": "user", "content": "[auto-nudge cycle 4] keep going"},
            {"role": "user", "content": "[Tool refusal] denied command"},
            {"role": "user", "content": "[Tool stall] no output for 300s"},
            {"role": "user", "content": "=== Restored Context (from prior session) ==="},
            {"role": "user", "content": "[system] context was compacted"},
        ]
        assert count_user_turns_in_records(records) == 0

    def test_injection_detection_ignores_case_and_leading_whitespace(self):
        records = [
            {"role": "user", "content": '\n  [cron notification from "x"] fired'},
            {"role": "user", "content": "  [SUBAGENT COMPLETION EVENT] done"},
        ]
        assert count_user_turns_in_records(records) == 0

    def test_a_mixed_transcript_counts_only_the_person(self):
        records = [
            {"role": "user", "content": "real ask"},
            {"role": "assistant", "content": "on it"},
            {"role": "user", "content": "[Subagent completion event] done"},
            {"role": "tool", "content": "tool payload"},
            {"role": "user", "content": "follow-up ask"},
            {"role": "user", "content": ""},
        ]
        assert count_user_turns_in_records(records) == 2

    def test_an_empty_transcript_is_zero(self):
        assert count_user_turns_in_records([]) == 0

    def test_it_agrees_with_the_extract_turns_path(self):
        """Two implementations of one rule; a divergence would show up as a
        button that appears and then refuses."""
        records = [
            {"role": "user", "content": "one"},
            {"role": "user", "content": '[Cron notification from "x"] fired'},
            {"role": "assistant", "content": "reply"},
            {"role": "user", "content": "two"},
            {"role": "user", "content": "  "},
        ]
        assert count_user_turns_in_records(records) == count_user_turns(extract_turns(records))


class TestForcedGeneration:
    """The on-demand pass, end to end through the generator."""

    pytestmark = pytest.mark.asyncio

    async def test_force_generates_despite_an_unclean_stop_reason(self, env, monkeypatch):
        state, slot = env
        slot._last_stop_reason = "timeout"
        called = []
        _stub_llm(monkeypatch, _GOOD_REPLY, called)
        assert (
            await chat_summary.generate_session_summary(state, slot, cfg=_cfg(), force=True) is True
        )
        assert len(called) == 1
        assert state.conversation_log.get_cached_intent_summary(state.hkey) is not None

    async def test_force_generates_for_an_idle_restored_slot(self, env, monkeypatch):
        """No stop reason at all -- the ordinary state of a session opened in a
        later process, and the reason the button exists."""
        state, slot = env
        slot._last_stop_reason = ""
        _stub_llm(monkeypatch, _GOOD_REPLY)
        assert (
            await chat_summary.generate_session_summary(state, slot, cfg=_cfg(), force=True) is True
        )
        assert state.conversation_log.get_cached_intent_summary(state.hkey) is not None

    async def test_force_generates_inside_the_cadence_window(self, env, monkeypatch):
        state, slot = env
        slot._summary_turn_mark = 3
        cfg = _cfg(regenerate_after_turns=5)
        called = []
        _stub_llm(monkeypatch, _GOOD_REPLY, called)
        assert await chat_summary.generate_session_summary(state, slot, cfg=cfg) is False
        assert called == []
        assert await chat_summary.generate_session_summary(state, slot, cfg=cfg, force=True) is True
        assert len(called) == 1

    async def test_force_does_not_override_the_off_switch(self, env, monkeypatch):
        state, slot = env
        called = []
        _stub_llm(monkeypatch, _GOOD_REPLY, called)
        assert (
            await chat_summary.generate_session_summary(
                state, slot, cfg=KiroCrewConfig(), force=True
            )
            is False
        )
        assert called == []
        assert state.conversation_log.get_cached_intent_summary(state.hkey) is None

    async def test_force_does_not_override_the_in_flight_guard(self, env, monkeypatch):
        """Two passes over one transcript would race the same sidecar."""
        state, slot = env
        slot._summary_in_flight = True
        called = []
        _stub_llm(monkeypatch, _GOOD_REPLY, called)
        assert (
            await chat_summary.generate_session_summary(state, slot, cfg=_cfg(), force=True)
            is False
        )
        assert called == []
        # The guard belongs to the pass that took it; a refused caller must not
        # clear it on the way out.
        assert slot._summary_in_flight is True

    async def test_force_does_not_override_incognito(self, env, monkeypatch):
        """An incognito transcript is discarded, so a forced summary would leave
        conversation content on disk after the conversation is gone -- consent to
        spend tokens is not consent to persist."""
        state, slot = env
        called = []
        _stub_llm(monkeypatch, _GOOD_REPLY, called)
        for mode in ("incognito", "temporary"):
            slot.memory_mode = mode
            assert (
                await chat_summary.generate_session_summary(state, slot, cfg=_cfg(), force=True)
                is False
            )
        assert called == []
        assert state.conversation_log.get_cached_intent_summary(state.hkey) is None

    async def test_force_does_not_override_the_turn_minimum(self, env, monkeypatch):
        state, slot = env
        log = state.conversation_log
        log.delete_session(state.hkey)
        one_turn = [{"role": "user", "content": "just one"}]
        _write_transcript(log, state.hkey, one_turn)
        # Window and transcript are one conversation: the pass flushes the window
        # to disk before counting.
        slot.messages = list(one_turn)
        called = []
        _stub_llm(monkeypatch, _GOOD_REPLY, called)
        assert (
            await chat_summary.generate_session_summary(
                state, slot, cfg=_cfg(min_user_turns=2), force=True
            )
            is False
        )
        assert called == []
        assert log.get_cached_intent_summary(state.hkey) is None

    async def test_a_forced_pass_over_an_unchanged_transcript_is_still_free(self, env, monkeypatch):
        """The cache check is deliberately NOT lifted, so a second click cannot
        buy an identical answer."""
        state, slot = env
        called = []
        _stub_llm(monkeypatch, _GOOD_REPLY, called)
        assert (
            await chat_summary.generate_session_summary(state, slot, cfg=_cfg(), force=True) is True
        )
        assert (
            await chat_summary.generate_session_summary(state, slot, cfg=_cfg(), force=True)
            is False
        )
        assert len(called) == 1
