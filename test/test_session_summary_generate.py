"""Tests for the session-summary generator.

No real model is ever called: ``run_bg_oneliner`` is patched at the module
boundary, so these assert the gating, the caching, the failure containment and
the trap wiring rather than any model behavior.
"""

from __future__ import annotations

import json

import pytest
from chat_test_helpers import move_transcript_past

from kiro_crew.config.loader import KiroCrewConfig, SessionSummaryConfig
from kiro_crew.dashboard import chat_summary
from kiro_crew.dashboard.chat_persistence import _save_slot_to_history
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.dashboard.state import _ChatSlot
from kiro_crew.history import ConversationLog


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
