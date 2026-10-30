"""Low-signal first-message titles: detection, the early refresh milestone, where the
flag locks, its persistence, the chained trigger waiting out an on-send attempt, and
the slot saves writing the flag.
``test_title_refresh.py`` heads the suite and lists its parts.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from title_refresh_helpers import _fake_state, _patch_generator, _titled_slot

from kiro_crew.dashboard import chat_persistence, chat_title
from kiro_crew.dashboard.chat_title import (
    _TITLE_ORIGIN_AUTO,
    _TITLE_ORIGIN_USER,
    _TITLE_REFRESH_MILESTONES,
    maybe_refresh_title,
)
from kiro_crew.dashboard.state import _ChatSlot


# ── low-signal first-message titles: the early refresh milestone ─────────────
class TestLowSignalDetection:
    """_is_low_signal_title: deterministic, no LLM call."""

    @pytest.mark.parametrize(
        "title",
        [
            "https://tickets.example.com/T8412000019 can you investigate…",
            "Investigate www.example.com outage",
            "Research ticket T8412000027",
        ],
    )
    def test_url_and_opaque_id_titles_are_low_signal(self, title):
        messages = [{"role": "user", "content": "unrelated opener"}]
        assert chat_title._is_low_signal_title(title, messages) is True

    @pytest.mark.parametrize(
        "title",
        [
            "Login timeout triage",
            "Fix PR #7353 emoji icons",  # short numbers stay below the bar
            "Port 8080 already in use",
        ],
    )
    def test_topic_titles_are_not_low_signal(self, title):
        messages = [{"role": "user", "content": "something else entirely"}]
        assert chat_title._is_low_signal_title(title, messages) is False

    def test_first_message_echo_is_low_signal(self):
        messages = [{"role": "user", "content": "please look into the flaky build"}]
        echo = chat_title._fallback_title_from_messages(messages)
        assert chat_title._is_low_signal_title(echo, messages) is True


class TestEarlyRefreshGating:
    @pytest.mark.asyncio
    async def test_low_signal_title_is_due_at_one_user_message(self, monkeypatch):
        calls = _patch_generator(monkeypatch, "S3 bucket policy violation triage")
        slot = _titled_slot(1)
        slot._title_low_signal = True
        await maybe_refresh_title(_fake_state(slot), slot)
        assert calls == ["Initial auto title"]
        assert slot.title == "S3 bucket policy violation triage"

    @pytest.mark.asyncio
    async def test_ordinary_title_is_not_due_at_one_user_message(self, monkeypatch):
        calls = _patch_generator(monkeypatch, "New Title")
        slot = _titled_slot(1)
        assert slot._title_low_signal is False
        await maybe_refresh_title(_fake_state(slot), slot)
        assert calls == []
        assert slot.title == "Initial auto title"

    @pytest.mark.asyncio
    async def test_early_attempt_consumes_flag_and_milestone(self, monkeypatch):
        calls = _patch_generator(monkeypatch, "")  # KEEP
        slot = _titled_slot(1)
        slot._title_low_signal = True
        state = _fake_state()
        await maybe_refresh_title(state, slot)
        assert len(calls) == 1
        assert slot._title_low_signal is False, "spent with the attempt"
        assert slot._title_refresh_mark == 1
        await maybe_refresh_title(state, slot)
        assert len(calls) == 1, "the early milestone never re-arms"

    @pytest.mark.asyncio
    async def test_ordinary_milestones_survive_an_early_spend(self, monkeypatch):
        """The early refresh must not eat the turn-8/24 budget."""
        calls = _patch_generator(monkeypatch, "")
        slot = _titled_slot(1)
        slot._title_low_signal = True
        state = _fake_state()
        await maybe_refresh_title(state, slot)
        assert len(calls) == 1
        for i in range(_TITLE_REFRESH_MILESTONES[0] - 1):
            slot.messages.append({"role": "user", "content": f"more {i}"})
        await maybe_refresh_title(state, slot)
        assert len(calls) == 2, "first ordinary milestone still fires"

    @pytest.mark.asyncio
    async def test_low_signal_flag_never_overrides_a_manual_rename(self, monkeypatch):
        calls = _patch_generator(monkeypatch, "New Title")
        slot = _titled_slot(1, origin=_TITLE_ORIGIN_USER)
        slot._title_low_signal = True
        await maybe_refresh_title(_fake_state(slot), slot)
        assert calls == []
        assert slot.title == "Initial auto title"


class TestLowSignalLockSites:
    @pytest.mark.asyncio
    async def test_url_opener_title_locks_flagged(self, monkeypatch):
        async def _fake_generate(_state, _messages, *, session_key: str = ""):
            return "Research ticket T8412000027"

        async def _noop(*_a, **_kw):
            return None

        monkeypatch.setattr(chat_title, "_generate_title_via_kiro", _fake_generate)
        monkeypatch.setattr(chat_title, "_reveal_title", _noop)
        monkeypatch.setattr(chat_title, "_persist_title", _noop)
        monkeypatch.setattr(chat_title, "maybe_suggest_folder", _noop)
        slot = _ChatSlot("chat-1-1")
        slot.messages = [
            {"role": "user", "content": "https://tickets.example.com/T8412000027 investigate"}
        ]
        await chat_title._maybe_auto_title(_fake_state(slot), slot)
        assert slot._titled is True
        assert slot._title_low_signal is True

    @pytest.mark.asyncio
    async def test_topic_title_locks_unflagged(self, monkeypatch):
        async def _fake_generate(_state, _messages, *, session_key: str = ""):
            return "Login timeout triage"

        async def _noop(*_a, **_kw):
            return None

        monkeypatch.setattr(chat_title, "_generate_title_via_kiro", _fake_generate)
        monkeypatch.setattr(chat_title, "_reveal_title", _noop)
        monkeypatch.setattr(chat_title, "_persist_title", _noop)
        monkeypatch.setattr(chat_title, "maybe_suggest_folder", _noop)
        slot = _ChatSlot("chat-1-1")
        slot.messages = [{"role": "user", "content": "the login page times out on submit"}]
        await chat_title._maybe_auto_title(_fake_state(slot), slot)
        assert slot._titled is True
        assert slot._title_low_signal is False

    @pytest.mark.asyncio
    async def test_definitive_fallback_locks_flagged(self, monkeypatch):
        async def _fake_generate(_state, _messages, *, session_key: str = ""):
            return ""  # SKIP

        async def _noop(*_a, **_kw):
            return None

        monkeypatch.setattr(chat_title, "_generate_title_via_kiro", _fake_generate)
        monkeypatch.setattr(chat_title, "_persist_title", _noop)
        monkeypatch.setattr(chat_title, "maybe_suggest_folder", _noop)
        slot = _ChatSlot("chat-1-1")
        slot.messages = [
            {"role": "user", "content": "please look into the flaky build on main"},
            {"role": "assistant", "content": "done"},
        ]
        await chat_title._maybe_auto_title(_fake_state(slot), slot)
        assert slot._titled is True
        assert slot._title_low_signal is True, "a fallback is an echo of the opener"

    @pytest.mark.asyncio
    async def test_manual_regenerate_clears_the_flag(self, monkeypatch):
        """A regenerated title comes from the recent tail — not an echo."""

        async def _fake_generate(_state, _messages, *, session_key: str = ""):
            return "Real topic name"

        async def _noop(*_a, **_kw):
            return None

        monkeypatch.setattr(chat_title, "_generate_title_via_kiro", _fake_generate)
        monkeypatch.setattr(chat_title, "_persist_title", _noop)
        slot = _ChatSlot("chat-1-1")
        slot.messages = [
            {"role": "user", "content": "https://tickets.example.com/T123456789"},
            {"role": "assistant", "content": "investigated the bucket policy"},
        ]
        slot.title = "https://tickets.example.com/T123456789"
        slot._titled = True
        slot._title_origin = _TITLE_ORIGIN_AUTO
        slot._title_low_signal = True

        state = _fake_state()
        request = MagicMock()
        request.get.return_value = ""
        request.app = {"state": state}
        request.match_info = {"slot": "chat-1-1"}
        state._slots = {"chat-1-1": slot}
        await chat_title.api_chat_slot_generate_title(request)
        assert slot.title == "Real topic name"
        assert slot._title_low_signal is False


class TestLowSignalPersistence:
    @pytest.mark.asyncio
    async def test_persist_writes_the_flag_both_ways(self):
        """True -> False must reach disk: a stale True would re-arm the early
        milestone on every restart."""
        state = _fake_state()
        recorded: list[dict] = []

        def _record(_key, fields, guard, **_kwargs):
            assert guard({})
            recorded.append(dict(fields))
            return True

        state.conversation_log.update_metadata_if = _record
        slot = _titled_slot(1)
        slot._title_low_signal = True
        await chat_title._persist_title(state, slot)
        slot._title_low_signal = False
        await chat_title._persist_title(state, slot)
        assert [f["title_low_signal"] for f in recorded] == [True, False]

    @pytest.mark.parametrize(
        "stored,expected",
        [(True, True), (False, False), (None, False), ("true", False), (1, False)],
    )
    def test_rehydrate_mapping(self, stored, expected):
        assert chat_persistence._rehydrate_title_low_signal(stored) is expected

    def test_rehydrate_slot_title_restores_the_flag(self):
        slot = _ChatSlot("chat-1-1")
        chat_persistence._rehydrate_slot_title(
            slot,
            "https://tickets.example.com/T123456789",
            titled=True,
            metadata={"title_origin": "auto", "title_low_signal": True},
        )
        assert slot._title_low_signal is True


class TestChainedTriggerWaitsForOnSendAttempt:
    """chat_done's title→refresh chain vs a still-running ON-SEND attempt.

    When the on-send titling task is still awaiting its LLM call at
    chat_done, the chained ``_maybe_auto_title`` bounces off the
    ``_title_in_flight`` guard and ``maybe_refresh_title`` bounces off the
    not-titled guard; the low-signal title then locks AFTER both, and a
    one-message session — which gets no later chat_done — keeps the URL echo
    indefinitely. ``title_then_refresh`` must wait the attempt out.
    """

    @staticmethod
    def _patch_title_path(monkeypatch, generate):
        async def _noop(*_a, **_kw):
            return None

        monkeypatch.setattr(chat_title, "_generate_title_via_kiro", generate)
        monkeypatch.setattr(chat_title, "_reveal_title", _noop)
        # NOTE: _persist_title stays REAL (against the MagicMock state) — the
        # refresh's durable-mark gate skips generation when persistence fails.
        monkeypatch.setattr(chat_title, "maybe_suggest_folder", _noop)

    @staticmethod
    def _one_message_url_slot() -> _ChatSlot:
        slot = _ChatSlot("chat-1-1")
        slot.messages = [
            {"role": "user", "content": "https://tickets.example.com/T8412000027 investigate"},
            {"role": "assistant", "content": "found the offending bucket policy"},
        ]
        return slot

    @pytest.mark.asyncio
    async def test_waits_out_in_flight_attempt_then_refreshes(self, monkeypatch):
        gate = asyncio.Event()

        async def _slow_generate(_state, _messages, *, session_key: str = ""):
            # flake-ok: the fake parks on an event the test sets itself, pre-existing shape, moved verbatim; tracked for a de-flake pass
            await gate.wait()
            return "Research ticket T8412000027"  # URL echo → low-signal

        self._patch_title_path(monkeypatch, _slow_generate)
        refresh_calls = _patch_generator(monkeypatch, "S3 bucket policy triage")

        slot = self._one_message_url_slot()
        state = _fake_state(slot)
        # The on-send attempt is still awaiting its LLM call at chat_done.
        slot._title_task = asyncio.create_task(chat_title._maybe_auto_title(state, slot))
        # flake-ok: one loop yield so the created task reaches its await, pre-existing shape, moved verbatim; tracked for a de-flake pass
        await asyncio.sleep(0)  # let it take the in-flight guard
        assert slot._title_in_flight is True

        chained = asyncio.create_task(chat_title.title_then_refresh(state, slot))
        # flake-ok: one loop yield so the created task reaches its await, pre-existing shape, moved verbatim; tracked for a de-flake pass
        await asyncio.sleep(0)
        assert not chained.done(), "chain must wait, not bounce off the guards"
        assert refresh_calls == []

        gate.set()  # LLM answers; the on-send attempt locks the low-signal title
        # flake-ok: awaits the chain the test has just released, unbounded as at its origin, pre-existing shape, moved verbatim; tracked for a de-flake pass
        await chained
        assert refresh_calls == ["Research ticket T8412000027"]
        assert slot.title == "S3 bucket policy triage"

    @pytest.mark.asyncio
    async def test_no_pending_task_runs_straight_through(self, monkeypatch):
        async def _fast_generate(_state, _messages, *, session_key: str = ""):
            return "Research ticket T8412000027"

        self._patch_title_path(monkeypatch, _fast_generate)
        refresh_calls = _patch_generator(monkeypatch, "S3 bucket policy triage")

        slot = self._one_message_url_slot()
        assert slot._title_task is None
        await chat_title.title_then_refresh(_fake_state(slot), slot)
        assert refresh_calls == ["Research ticket T8412000027"]
        assert slot.title == "S3 bucket policy triage"

    @pytest.mark.asyncio
    async def test_cancelled_attempt_leaves_the_retry_to_do_the_work(self, monkeypatch):
        gate = asyncio.Event()

        async def _slow_generate(_state, _messages, *, session_key: str = ""):
            # flake-ok: the fake parks on an event the test never sets; the test cancels the task that waits on it, pre-existing shape, moved verbatim; tracked for a de-flake pass
            await gate.wait()
            return "never returned"

        self._patch_title_path(monkeypatch, _slow_generate)
        refresh_calls = _patch_generator(monkeypatch, "S3 bucket policy triage")

        slot = self._one_message_url_slot()
        state = _fake_state(slot)
        slot._title_task = asyncio.create_task(chat_title._maybe_auto_title(state, slot))
        # flake-ok: one loop yield so the created task reaches its await, pre-existing shape, moved verbatim; tracked for a de-flake pass
        await asyncio.sleep(0)
        slot._title_task.cancel()
        # flake-ok: one loop yield so the cancellation reaches the parked task and releases its guard, pre-existing shape, moved verbatim; tracked for a de-flake pass
        await asyncio.sleep(0)  # cancellation propagates; guard is released

        async def _fast_generate(_state, _messages, *, session_key: str = ""):
            return "Research ticket T8412000027"

        monkeypatch.setattr(chat_title, "_generate_title_via_kiro", _fast_generate)
        await chat_title.title_then_refresh(state, slot)  # must not raise
        assert slot.title == "S3 bucket policy triage"
        assert refresh_calls == ["Research ticket T8412000027"]

    @pytest.mark.asyncio
    async def test_titled_at_entry_with_guard_still_held_waits_then_refreshes(self, monkeypatch):
        """The already-titled chat_done branch must also wait out the attempt.

        An on-send attempt sets ``_titled``/``_title_low_signal`` and only then
        awaits its persist, releasing ``_title_in_flight`` after. A chat_done
        landing in that window sees a TITLED slot whose guard is still held —
        a direct ``maybe_refresh_title`` bounces off the guard, and a
        one-message session gets no later chat_done. Routing this branch
        through ``title_then_refresh`` waits the attempt out first.
        """
        gate = asyncio.Event()

        async def _slow_persist(_state, _slot, **_kw):
            # flake-ok: the fake parks on an event the test sets itself, pre-existing shape, moved verbatim; tracked for a de-flake pass
            await gate.wait()
            return True  # honor the bool contract — the durable-mark gate reads it

        async def _fast_generate(_state, _messages, *, session_key: str = ""):
            return "Research ticket T8412000027"  # URL echo → low-signal

        self._patch_title_path(monkeypatch, _fast_generate)
        monkeypatch.setattr(chat_title, "_persist_title", _slow_persist)
        refresh_calls = _patch_generator(monkeypatch, "S3 bucket policy triage")

        slot = self._one_message_url_slot()
        state = _fake_state(slot)
        slot._title_task = asyncio.create_task(chat_title._maybe_auto_title(state, slot))
        # flake-ok: one loop yield so the created task reaches its await, pre-existing shape, moved verbatim; tracked for a de-flake pass
        await asyncio.sleep(0)  # generate returns; attempt now awaits persist
        # flake-ok: a second loop yield so the attempt reaches its persist await, pre-existing shape, moved verbatim; tracked for a de-flake pass
        await asyncio.sleep(0)
        assert slot._titled is True, "attempt must have locked the title already"
        assert slot._title_in_flight is True, "guard must still be held (persist pending)"

        chained = asyncio.create_task(chat_title.title_then_refresh(state, slot))
        # flake-ok: one loop yield so the created task reaches its await, pre-existing shape, moved verbatim; tracked for a de-flake pass
        await asyncio.sleep(0)
        assert not chained.done(), "chain must wait, not bounce off the in-flight guard"
        assert refresh_calls == []

        gate.set()  # persist lands; the attempt releases the guard
        # flake-ok: awaits the chain the test has just released, unbounded as at its origin, pre-existing shape, moved verbatim; tracked for a de-flake pass
        await chained
        assert refresh_calls == ["Research ticket T8412000027"]
        assert slot.title == "S3 bucket policy triage"


class TestSlotSavePersistsLowSignalFlag:
    """Both ``_save_slot_to_history`` metadata builders persist the low-signal
    flag alongside the title (GPT review of e5ba1cf9f).

    ``_persist_title`` is the primary writer, but it returns False without
    retry on a transient failure. If a full slot save then lands the TITLE on
    disk without the flag, a restart rehydrates a titled slot whose low-signal
    marker is absent -- and the turn-one refresh is permanently skipped. The
    flag is title-coupled metadata: whoever persists the title persists it.
    """

    def _make_state(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.state import DashboardState
        from kiro_crew.history import ConversationLog

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        sessions = MagicMock(count=0)
        sessions.get_pid = MagicMock(return_value=None)
        return DashboardState(
            sessions=sessions,
            crons=MagicMock(
                list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})
            ),
            lessons=MagicMock(load_all=MagicMock(return_value=[])),
            start_time=0.0,
            conversation_log=ConversationLog(base_dir=tmp_path),
        )

    def test_full_save_persists_low_signal_flag(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history
        from kiro_crew.dashboard.chat_utils import _history_key_for
        from kiro_crew.history import ConversationLog

        state = self._make_state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("lowsig-full")
        slot.append("user", "https://tickets.example.com/T7300000042")
        slot.append("assistant", "investigating")
        slot.drain()
        slot.title = "https://tickets.example.com/T7300000042"
        slot._title_origin = "auto"
        slot._title_low_signal = True

        _save_slot_to_history(state, slot, force=True)

        stored = ConversationLog(base_dir=tmp_path).get_metadata(_history_key_for(slot.key))
        assert stored["title_low_signal"] is True

    def test_forced_empty_slot_save_persists_low_signal_flag(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history
        from kiro_crew.dashboard.chat_utils import _history_key_for
        from kiro_crew.history import ConversationLog

        state = self._make_state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("lowsig-empty")
        # No messages: the forced save routes through the ``_fresh_fields``
        # builder rather than the full ``meta_line`` construction. That branch
        # merges ONLY into an existing metadata line, so seed the birth line
        # exactly as ``session_create`` does.
        state.conversation_log.update_metadata(_history_key_for(slot.key), {"folder_id": ""})
        slot.title = "https://tickets.example.com/T7300000042"
        slot._title_origin = "auto"
        slot._title_low_signal = True

        _save_slot_to_history(state, slot, force=True)

        stored = ConversationLog(base_dir=tmp_path).get_metadata(_history_key_for(slot.key))
        assert stored["title_low_signal"] is True

    def test_cleared_flag_is_written_as_false(self, tmp_path, monkeypatch):
        """The CURRENT boolean is written both directions (GPT review of
        63893d4e4): a cleared flag (consumption or manual regenerate) whose
        ``_persist_title`` write failed transiently must not be resurrected
        as True from an earlier persist -- the full save overwrites it with
        the slot's live False."""
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history
        from kiro_crew.dashboard.chat_utils import _history_key_for
        from kiro_crew.history import ConversationLog

        state = self._make_state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("lowsig-off")
        slot.append("user", "hello")
        slot.append("assistant", "hi")
        slot.drain()
        slot.title = "Greeting chat"
        slot._title_origin = "auto"
        # An earlier persist landed True on disk...
        state.conversation_log.update_metadata(
            _history_key_for(slot.key), {"title_low_signal": True}
        )
        # ...then the flag was cleared in memory (consumed / regenerated).
        slot._title_low_signal = False

        _save_slot_to_history(state, slot, force=True)

        stored = ConversationLog(base_dir=tmp_path).get_metadata(_history_key_for(slot.key))
        assert stored["title_low_signal"] is False

    def test_low_signal_flag_defers_on_rows_only_saves(self):
        """``title_low_signal`` is title-coupled metadata, so a rows-only
        hand-over save (a popped slot draining onto a live same-key
        replacement's line) must defer it exactly like ``title_origin`` and
        ``title_refresh_mark`` -- otherwise the popped slot's stale flag
        overwrites the replacement's and a restart wrongly suppresses or
        re-arms the turn-one refresh (GPT review of 3518b8081)."""
        from kiro_crew.history import ROWS_ONLY_DEFERRED_META_KEYS

        assert "title_low_signal" in ROWS_ONLY_DEFERRED_META_KEYS
        # The two siblings it must travel with.
        assert "title_origin" in ROWS_ONLY_DEFERRED_META_KEYS
        assert "title_refresh_mark" in ROWS_ONLY_DEFERRED_META_KEYS
