"""Tests for the background session-title refresh (#1846, reworked per review).

The feature: instead of a ``set_session_title`` tool exposed to every chat, the
existing background auto-title flow is made flexible — an AUTO title is
re-examined at bounded user-turn milestones via the same ``_bg`` one-liner
path, and swapped when the model says the old name no longer fits.

Locked-in invariants:

- Token budget: at most one refresh per milestone in
  ``_TITLE_REFRESH_MILESTONES``, attempt-counted (KEEP/SKIP/prose/error all
  consume the milestone), and the consumed mark is persisted so restarts
  cannot re-spend it.
- A manual rename is FINAL: origin "user" locks the refresh out, a rename
  landing mid-generation stands the refresh down (epoch guard), and a legacy
  title with no stored origin rehydrates as "user".
- The reveal animation is cosmetic-only: it never mutates ``slot.title``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from kiro_crew.dashboard import chat_persistence, chat_title
from kiro_crew.dashboard.chat_title import (
    _TITLE_ORIGIN_AUTO,
    _TITLE_ORIGIN_USER,
    _TITLE_REFRESH_MILESTONES,
    _build_refresh_prompt,
    maybe_refresh_title,
)
from kiro_crew.dashboard.state import _ChatSlot


def _fake_state():
    state = MagicMock()
    # conversation_log must be truthy for _persist_title to attempt a write.
    state.conversation_log = MagicMock()
    return state


def _titled_slot(user_turns: int, *, origin: str = _TITLE_ORIGIN_AUTO) -> _ChatSlot:
    slot = _ChatSlot("chat-1-1")
    slot.messages = []
    for i in range(user_turns):
        slot.messages.append({"role": "user", "content": f"user message {i}"})
        slot.messages.append({"role": "assistant", "content": f"assistant reply {i}"})
    slot.title = "Initial auto title"
    slot._titled = True
    slot._title_origin = origin
    return slot


def _patch_generator(monkeypatch, reply: str | Exception):
    """Replace the refresh generator; returns the list of recorded calls."""
    calls: list[str] = []

    async def _fake(_state, _messages, current_title):
        calls.append(current_title)
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(chat_title, "_generate_refreshed_title", _fake)
    return calls


# ── milestone gating: the token budget ───────────────────────────────────────
class TestRefreshGating:
    @pytest.mark.asyncio
    async def test_not_due_before_first_milestone(self, monkeypatch):
        calls = _patch_generator(monkeypatch, "New Title")
        slot = _titled_slot(_TITLE_REFRESH_MILESTONES[0] - 1)
        await maybe_refresh_title(_fake_state(), slot)
        assert calls == []
        assert slot.title == "Initial auto title"

    @pytest.mark.asyncio
    async def test_due_at_first_milestone(self, monkeypatch):
        calls = _patch_generator(monkeypatch, "New Title")
        slot = _titled_slot(_TITLE_REFRESH_MILESTONES[0])
        await maybe_refresh_title(_fake_state(), slot)
        assert calls == ["Initial auto title"]
        assert slot.title == "New Title"

    @pytest.mark.asyncio
    async def test_milestone_fires_at_most_once(self, monkeypatch):
        calls = _patch_generator(monkeypatch, "New Title")
        slot = _titled_slot(_TITLE_REFRESH_MILESTONES[0])
        state = _fake_state()
        await maybe_refresh_title(state, slot)
        await maybe_refresh_title(state, slot)
        await maybe_refresh_title(state, slot)
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_second_milestone_fires_after_first_consumed(self, monkeypatch):
        calls = _patch_generator(monkeypatch, "KEEP-not-used")
        state = _fake_state()
        slot = _titled_slot(_TITLE_REFRESH_MILESTONES[0])
        await maybe_refresh_title(state, slot)
        assert len(calls) == 1
        # Conversation grows past the second milestone.
        for i in range(_TITLE_REFRESH_MILESTONES[1] - _TITLE_REFRESH_MILESTONES[0]):
            slot.messages.append({"role": "user", "content": f"more {i}"})
        await maybe_refresh_title(state, slot)
        assert len(calls) == 2
        await maybe_refresh_title(state, slot)
        assert len(calls) == 2, "budget is exhausted after the last milestone"

    @pytest.mark.asyncio
    async def test_failed_attempt_consumes_the_milestone(self, monkeypatch):
        calls = _patch_generator(monkeypatch, RuntimeError("bg session down"))
        slot = _titled_slot(_TITLE_REFRESH_MILESTONES[0])
        state = _fake_state()
        await maybe_refresh_title(state, slot)  # must not raise
        assert len(calls) == 1
        assert slot._title_refresh_mark == _TITLE_REFRESH_MILESTONES[0]
        await maybe_refresh_title(state, slot)
        assert len(calls) == 1, "a failed attempt is never retried"
        assert slot._title_in_flight is False

    @pytest.mark.asyncio
    async def test_user_origin_is_never_refreshed(self, monkeypatch):
        calls = _patch_generator(monkeypatch, "New Title")
        slot = _titled_slot(_TITLE_REFRESH_MILESTONES[1], origin=_TITLE_ORIGIN_USER)
        await maybe_refresh_title(_fake_state(), slot)
        assert calls == []
        assert slot.title == "Initial auto title"

    @pytest.mark.asyncio
    async def test_untitled_slot_is_not_refreshed(self, monkeypatch):
        calls = _patch_generator(monkeypatch, "New Title")
        slot = _titled_slot(_TITLE_REFRESH_MILESTONES[0])
        slot._titled = False
        slot._title_origin = ""
        await maybe_refresh_title(_fake_state(), slot)
        assert calls == []

    @pytest.mark.asyncio
    async def test_in_flight_guard_excludes_concurrent_attempts(self, monkeypatch):
        calls = _patch_generator(monkeypatch, "New Title")
        slot = _titled_slot(_TITLE_REFRESH_MILESTONES[0])
        slot._title_in_flight = True
        await maybe_refresh_title(_fake_state(), slot)
        assert calls == []

    @pytest.mark.asyncio
    async def test_rehydrated_mark_is_not_respent(self, monkeypatch):
        """A restart must not re-spend a consumed milestone: with mark=8 already
        persisted, only the SECOND milestone remains."""
        calls = _patch_generator(monkeypatch, "New Title")
        slot = _titled_slot(_TITLE_REFRESH_MILESTONES[0] + 1)
        slot._title_refresh_mark = _TITLE_REFRESH_MILESTONES[0]
        await maybe_refresh_title(_fake_state(), slot)
        assert calls == [], "first milestone already consumed pre-restart"


# ── refresh outcomes ─────────────────────────────────────────────────────────
class TestRefreshOutcomes:
    @pytest.mark.asyncio
    async def test_keep_leaves_title_untouched_but_persists_mark(self, monkeypatch):
        _patch_generator(monkeypatch, "")  # KEEP/SKIP surfaces as ""
        persisted: list[str] = []

        async def _fake_persist(_state, s):
            persisted.append(s.title)
            return True

        monkeypatch.setattr(chat_title, "_persist_title", _fake_persist)
        slot = _titled_slot(_TITLE_REFRESH_MILESTONES[0])
        state = _fake_state()
        await maybe_refresh_title(state, slot)
        assert slot.title == "Initial auto title"
        assert slot._title_refresh_mark == _TITLE_REFRESH_MILESTONES[0]
        assert persisted, "consumed mark must be persisted even on KEEP"
        state.push_slot_title.assert_not_called()

    @pytest.mark.asyncio
    async def test_new_title_is_applied_pushed_and_stays_auto(self, monkeypatch):
        _patch_generator(monkeypatch, "Debug flaky auth test")
        slot = _titled_slot(_TITLE_REFRESH_MILESTONES[0])
        state = _fake_state()
        await maybe_refresh_title(state, slot)
        assert slot.title == "Debug flaky auth test"
        assert slot._title_origin == _TITLE_ORIGIN_AUTO, "stays refreshable"
        assert slot._titled is True
        state.push_slot_title.assert_called_with(slot.key, "Debug flaky auth test")

    @pytest.mark.asyncio
    async def test_identical_title_is_not_repushed(self, monkeypatch):
        _patch_generator(monkeypatch, "Initial auto title")
        slot = _titled_slot(_TITLE_REFRESH_MILESTONES[0])
        state = _fake_state()
        await maybe_refresh_title(state, slot)
        state.push_slot_title.assert_not_called()

    @pytest.mark.asyncio
    async def test_rename_during_generation_wins(self, monkeypatch):
        """A manual rename landing mid-generation bumps the epoch; the refresh
        must stand down instead of clobbering the user's name."""

        async def _rename_mid_flight(_state, _messages, _current):
            slot.title = "User chosen name"
            slot._title_origin = _TITLE_ORIGIN_USER
            slot._title_epoch += 1
            return "Model suggestion"

        monkeypatch.setattr(chat_title, "_generate_refreshed_title", _rename_mid_flight)
        slot = _titled_slot(_TITLE_REFRESH_MILESTONES[0])
        state = _fake_state()
        await maybe_refresh_title(state, slot)
        assert slot.title == "User chosen name"
        state.push_slot_title.assert_not_called()


# ── the refresh reply path (real validation, fake wire) ──────────────────────
class TestRefreshReplyValidation:
    @pytest.mark.asyncio
    async def test_keep_reply_means_no_title(self, monkeypatch):
        async def _fake_oneliner(*_a, **_kw):
            return "KEEP"

        monkeypatch.setattr(chat_title, "run_bg_oneliner", _fake_oneliner)
        monkeypatch.setattr(chat_title, "_ui_language", lambda: "")
        title = await chat_title._generate_refreshed_title(
            SimpleNamespace(sessions=SimpleNamespace()),
            [{"role": "user", "content": "still the same task"}],
            "Current title",
        )
        assert title == ""

    @pytest.mark.asyncio
    async def test_prose_reply_means_no_title(self, monkeypatch):
        async def _fake_oneliner(*_a, **_kw):
            return "I cannot access external URLs like Quip documents."

        monkeypatch.setattr(chat_title, "run_bg_oneliner", _fake_oneliner)
        monkeypatch.setattr(chat_title, "_ui_language", lambda: "")
        title = await chat_title._generate_refreshed_title(
            SimpleNamespace(sessions=SimpleNamespace()),
            [{"role": "user", "content": "look at https://example.com/doc"}],
            "Current title",
        )
        assert title == ""

    @pytest.mark.asyncio
    async def test_real_reply_is_cleaned_and_returned(self, monkeypatch):
        async def _fake_oneliner(*_a, **_kw):
            return '"Fix login token refresh"'

        monkeypatch.setattr(chat_title, "run_bg_oneliner", _fake_oneliner)
        monkeypatch.setattr(chat_title, "_ui_language", lambda: "")
        title = await chat_title._generate_refreshed_title(
            SimpleNamespace(sessions=SimpleNamespace()),
            [{"role": "user", "content": "the login token expires too early"}],
            "Current title",
        )
        assert title == "Fix login token refresh"


# ── the refresh prompt: bounded, recent-windowed, KEEP-escaped ────────────────
class TestRefreshPrompt:
    def test_prompt_carries_current_title_and_keep_instruction(self):
        prompt = _build_refresh_prompt(
            [{"role": "user", "content": "hello"}], "My current title"
        )
        assert prompt is not None
        assert "My current title" in prompt
        assert "KEEP" in prompt
        assert "===== CONVERSATION TO NAME =====" in prompt

    def test_prompt_windows_the_recent_tail(self):
        messages = [
            {"role": "user", "content": f"topic-{i} discussion"} for i in range(30)
        ]
        prompt = _build_refresh_prompt(messages, "T")
        assert prompt is not None
        assert "topic-29" in prompt
        assert "topic-20" in prompt
        assert "topic-0 " not in prompt, "old head must be windowed out"

    def test_prompt_lines_are_bounded(self):
        messages = [{"role": "user", "content": "x" * 5000}]
        prompt = _build_refresh_prompt(messages, "T")
        assert prompt is not None
        transcript = prompt.split("===== CONVERSATION TO NAME =====")[1]
        assert max(len(line) for line in transcript.splitlines() if line) <= 210

    def test_current_title_is_bounded(self):
        prompt = _build_refresh_prompt(
            [{"role": "user", "content": "hello"}], "t" * 500
        )
        assert prompt is not None
        assert "t" * 81 not in prompt

    def test_prompt_none_without_usable_messages(self):
        assert _build_refresh_prompt([], "T") is None

    def test_language_directive_is_included_when_set(self):
        prompt = _build_refresh_prompt(
            [{"role": "user", "content": "hello"}], "T", ui_language="ja"
        )
        assert prompt is not None
        assert "BCP-47 tag ja" in prompt


# ── the manual regenerate endpoint windows the recent tail ────────────────────
class TestManualRegenerateWindow:
    @pytest.mark.asyncio
    async def test_manual_regenerate_prompts_from_the_recent_tail(self, monkeypatch):
        """Regenerating the title of a long session must build the prompt from
        the LAST conversational messages, mirroring the refresh window: the
        user reaches for the control when the current name no longer fits, and
        the recent tail is where the current topic lives. The trailing run of
        tool/status rows a tool-heavy turn appends must not starve the window
        — the slice is taken over conversational rows, not raw rows. Without
        the endpoint-side tail slice the prompt builder's head window rebuilds
        the opening-topic title."""
        captured: dict[str, str] = {}

        async def _capture_oneliner(_sessions, prompt, **_kw):
            captured["prompt"] = prompt
            return "Tail topic title"

        async def _noop(*_a, **_kw):
            return None

        monkeypatch.setattr(chat_title, "run_bg_oneliner", _capture_oneliner)
        monkeypatch.setattr(chat_title, "_persist_title", _noop)
        monkeypatch.setattr(chat_title, "_ui_language", lambda: "")

        slot = _ChatSlot("chat-1-1")
        slot.messages = [
            {"role": "user", "content": f"topic-{i} discussion"} for i in range(30)
        ]
        # A tool-heavy final turn: the raw tail is entirely non-conversational
        # rows, which the prompt builder filters out.
        slot.messages += [{"role": "tool", "content": f"tool-row-{i}"} for i in range(12)]
        state = _fake_state()
        state._slots = {slot.key: slot}
        request = MagicMock()
        request.app = {"state": state}
        request.match_info = {"slot": slot.key}

        response = await chat_title.api_chat_slot_generate_title(request)

        assert response.status == 200
        prompt = captured["prompt"]
        assert "topic-29" in prompt
        assert "topic-20" in prompt
        assert "topic-0 " not in prompt, "old head must be windowed out"
        assert "tool-row" not in prompt
        assert slot.title == "Tail topic title"


# ── origin recording on the write paths ──────────────────────────────────────
class TestOriginRecording:
    @pytest.mark.asyncio
    async def test_auto_title_success_records_auto_origin(self, monkeypatch):
        async def _fake_generate(_state, _messages):
            return "Generated title"

        async def _noop(*_a, **_kw):
            return None

        monkeypatch.setattr(chat_title, "_generate_title_via_kiro", _fake_generate)
        monkeypatch.setattr(chat_title, "_reveal_title", _noop)
        monkeypatch.setattr(chat_title, "_persist_title", _noop)
        monkeypatch.setattr(chat_title, "maybe_suggest_folder", _noop)
        slot = _ChatSlot("chat-1-1")
        slot.messages = [{"role": "user", "content": "hello world task"}]
        await chat_title._maybe_auto_title(_fake_state(), slot)
        assert slot._titled is True
        assert slot._title_origin == _TITLE_ORIGIN_AUTO

    @pytest.mark.asyncio
    async def test_definitive_fallback_records_auto_origin(self, monkeypatch):
        async def _fake_generate(_state, _messages):
            return ""  # SKIP

        async def _noop(*_a, **_kw):
            return None

        monkeypatch.setattr(chat_title, "_generate_title_via_kiro", _fake_generate)
        monkeypatch.setattr(chat_title, "_persist_title", _noop)
        monkeypatch.setattr(chat_title, "maybe_suggest_folder", _noop)
        slot = _ChatSlot("chat-1-1")
        slot.messages = [
            {"role": "user", "content": "hello world task"},
            {"role": "assistant", "content": "done"},
        ]
        await chat_title._maybe_auto_title(_fake_state(), slot)
        assert slot._titled is True
        assert slot._title_origin == _TITLE_ORIGIN_AUTO, (
            "the truncated fallback is auto-generated, so the refresh may "
            "upgrade it to a real LLM title later"
        )

    @pytest.mark.asyncio
    async def test_auto_title_stands_down_when_rename_lands_mid_generation(
        self, monkeypatch
    ):
        slot = _ChatSlot("chat-1-1")
        slot.messages = [{"role": "user", "content": "hello world task"}]

        async def _rename_mid_flight(_state, _messages):
            slot.title = "User chosen name"
            slot._titled = True
            slot._title_origin = _TITLE_ORIGIN_USER
            slot._title_epoch += 1
            return "Model suggestion"

        async def _noop(*_a, **_kw):
            return None

        monkeypatch.setattr(chat_title, "_generate_title_via_kiro", _rename_mid_flight)
        monkeypatch.setattr(chat_title, "_persist_title", _noop)
        monkeypatch.setattr(chat_title, "maybe_suggest_folder", _noop)
        state = _fake_state()
        await chat_title._maybe_auto_title(state, slot)
        assert slot.title == "User chosen name"
        assert slot._title_origin == _TITLE_ORIGIN_USER


# ── the reveal is cosmetic ───────────────────────────────────────────────────
class TestRevealIsCosmetic:
    @pytest.mark.asyncio
    async def test_reveal_never_mutates_slot_title(self, monkeypatch):
        monkeypatch.setattr(chat_title, "_TITLE_REVEAL_STEP_SECS", 0)
        slot = _ChatSlot("chat-1-1")
        slot.title = "existing"
        state = _fake_state()
        await chat_title._reveal_title(state, slot, "three word title")
        assert slot.title == "existing", "animation frames must not touch slot.title"
        assert state.push_slot_title.call_count == 2  # prefixes, not the full title

    @pytest.mark.asyncio
    async def test_reveal_stops_when_epoch_moves(self, monkeypatch):
        monkeypatch.setattr(chat_title, "_TITLE_REVEAL_STEP_SECS", 0)
        slot = _ChatSlot("chat-1-1")
        state = _fake_state()

        def _bump_epoch(*_a, **_kw):
            slot._title_epoch += 1

        state.push_slot_title.side_effect = _bump_epoch
        await chat_title._reveal_title(
            state, slot, "one two three four five", epoch=slot._title_epoch
        )
        assert state.push_slot_title.call_count == 1, "reveal must stop on epoch move"


# ── rehydration: provenance and budget survive a reload ─────────────────────
class TestRehydration:
    @pytest.mark.parametrize(
        "titled,stored,expected",
        [
            (True, "auto", "auto"),
            (True, "user", "user"),
            (True, None, "user"),  # legacy: conservative, never refreshed
            (True, "agent", "user"),  # unrecognized value: conservative
            (True, 7, "user"),
            (False, "auto", ""),
            (False, None, ""),
        ],
    )
    def test_origin_mapping(self, titled, stored, expected):
        assert chat_persistence._rehydrate_title_origin(titled, stored) == expected

    @pytest.mark.parametrize(
        "stored,expected",
        [(8, 8), (24, 24), (None, 0), (0, 0), (-3, 0), (True, 0), ("8", 0)],
    )
    def test_refresh_mark_mapping(self, stored, expected):
        assert chat_persistence._rehydrate_title_refresh_mark(stored) == expected


# ── rename handler finality ──────────────────────────────────────────────────
class TestRenameIsFinal:
    @pytest.mark.asyncio
    async def test_rename_sets_user_origin_and_bumps_epoch(self, monkeypatch):
        async def _noop(*_a, **_kw):
            return None

        monkeypatch.setattr(chat_title, "_persist_title", _noop)
        monkeypatch.setattr(chat_title, "sel", MagicMock())
        slot = _ChatSlot("chat-1-1")
        state = _fake_state()
        state._slots = {"chat-1-1": slot}
        request = MagicMock()
        request.app = {"state": state}
        request.match_info = {"slot": "chat-1-1"}

        async def _json():
            return {"title": "My manual name"}

        request.json = _json
        epoch_before = slot._title_epoch
        resp = await chat_title.api_chat_slot_rename(request)
        assert resp.status == 200
        assert slot.title == "My manual name"
        assert slot._title_origin == _TITLE_ORIGIN_USER
        assert slot._title_epoch == epoch_before + 1

        # And the background refresh now refuses to touch it, forever.
        calls = _patch_generator(monkeypatch, "Model idea")
        slot.messages = [
            {"role": "user", "content": f"m{i}"} for i in range(_TITLE_REFRESH_MILESTONES[1])
        ]
        await maybe_refresh_title(state, slot)
        assert calls == []
        assert slot.title == "My manual name"


# ── task-budget contract: the whole feature is at most N one-liner calls ─────
class TestTokenBudgetContract:
    def test_two_milestones(self):
        """The refresh budget is a deliberate contract reviewed for token cost:
        widening it must be a conscious decision, not a drive-by edit."""
        assert _TITLE_REFRESH_MILESTONES == (8, 24)

    @pytest.mark.asyncio
    async def test_lifetime_call_count_is_bounded(self, monkeypatch):
        """Drive a session through 40 turns of chat_done refreshes: the
        generator must run exactly len(_TITLE_REFRESH_MILESTONES) times."""
        calls = _patch_generator(monkeypatch, "KEEP-unused")
        slot = _titled_slot(0)
        state = _fake_state()
        for i in range(40):
            slot.messages.append({"role": "user", "content": f"turn {i}"})
            slot.messages.append({"role": "assistant", "content": "ok"})
            await maybe_refresh_title(state, slot)
        assert len(calls) == len(_TITLE_REFRESH_MILESTONES)


# ── local-review regressions: write ordering, error-path budget, pin origin ──
class TestPersistWriteOrdering:
    @pytest.mark.asyncio
    async def test_rename_landing_mid_write_is_repersisted(self, tmp_path):
        """A rename landing while the background persist's off-thread write is
        in flight bumps the epoch; the persist loop must then write AGAIN with
        the current (user) values, so the disk can never end up on the stale
        auto title regardless of flock acquisition order."""
        from kiro_crew.history import ConversationLog

        log = ConversationLog(base_dir=tmp_path)
        log.append("dashboard:chat-1-1", "user", "seed")

        slot = _ChatSlot("chat-1-1")
        slot.title = "Auto title"
        slot._titled = True
        slot._title_origin = _TITLE_ORIGIN_AUTO

        state = MagicMock()
        state.conversation_log = log

        writes: list[dict] = []
        real_update = log.update_metadata

        def _racing_update(key, fields):
            writes.append(dict(fields))
            if len(writes) == 1:
                # Simulate the rename winning the race while this (stale)
                # write is on the worker thread: by the time the awaiting
                # coroutine resumes, the epoch has moved.
                slot.title = "User chosen name"
                slot._title_origin = _TITLE_ORIGIN_USER
                slot._title_epoch += 1
            real_update(key, fields)

        log.update_metadata = _racing_update  # type: ignore[method-assign]

        await chat_title._persist_title(state, slot)

        assert len(writes) == 2, "epoch move during the write must trigger a re-persist"
        assert writes[-1]["title"] == "User chosen name"
        assert writes[-1]["title_origin"] == _TITLE_ORIGIN_USER
        persisted = ConversationLog(base_dir=tmp_path).get_metadata("dashboard:chat-1-1")
        assert persisted["title"] == "User chosen name"
        assert persisted["title_origin"] == _TITLE_ORIGIN_USER

    @pytest.mark.asyncio
    async def test_stable_epoch_writes_exactly_once(self, tmp_path):
        from kiro_crew.history import ConversationLog

        log = ConversationLog(base_dir=tmp_path)
        log.append("dashboard:chat-1-1", "user", "seed")
        slot = _ChatSlot("chat-1-1")
        slot.title = "Auto title"
        slot._titled = True
        slot._title_origin = _TITLE_ORIGIN_AUTO
        state = MagicMock()
        state.conversation_log = log

        count = 0
        real_update = log.update_metadata

        def _counting(key, fields):
            nonlocal count
            count += 1
            real_update(key, fields)

        log.update_metadata = _counting  # type: ignore[method-assign]
        await chat_title._persist_title(state, slot)
        assert count == 1


class TestRefreshErrorPathPersistsBudget:
    @pytest.mark.asyncio
    async def test_error_path_persists_consumed_mark(self, monkeypatch):
        """A propagated generation error must still persist the consumed
        milestone — otherwise a restart reloads the old mark and re-spends the
        refresh budget."""
        _patch_generator(monkeypatch, RuntimeError("bg session down"))
        persisted: list[int] = []

        async def _spy_persist(_state, s):
            persisted.append(s._title_refresh_mark)
            return True

        monkeypatch.setattr(chat_title, "_persist_title", _spy_persist)
        slot = _titled_slot(_TITLE_REFRESH_MILESTONES[0])
        await maybe_refresh_title(_fake_state(), slot)
        assert persisted == [_TITLE_REFRESH_MILESTONES[0]]


class TestSlotCreatePinIsFinal:
    @pytest.mark.asyncio
    async def test_pinned_title_records_user_origin(self, tmp_path, monkeypatch):
        """POST /api/chat/slots with an explicit title on an already-auto-titled
        slot must flip the origin to "user" (and bump the epoch), so the
        background refresh can never rewrite a pinned name."""
        import json as _json  # noqa: F401 — parity with sibling create tests
        from unittest.mock import AsyncMock

        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer
        from chat_test_helpers import _make_ready_kiro_prerequisite

        from kiro_crew.dashboard.chat import api_chat_slot_create
        from kiro_crew.dashboard.state import DashboardState
        from kiro_crew.history import ConversationLog

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        sessions = MagicMock(count=0)
        sessions.remove = AsyncMock()
        sessions.recycle_background = AsyncMock()
        sessions.get_pid = MagicMock(return_value=None)
        state = DashboardState(
            sessions=sessions,
            crons=MagicMock(
                list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})
            ),
            lessons=MagicMock(load_all=MagicMock(return_value=[])),
            start_time=0.0,
            conversation_log=ConversationLog(base_dir=tmp_path),
        )
        state.kiro_prerequisite_service = _make_ready_kiro_prerequisite()

        app = web.Application()
        app["state"] = state
        app.router.add_post("/api/chat/slots", api_chat_slot_create)

        async with TestClient(TestServer(app)) as client:
            # First create the slot, then simulate it having been auto-titled.
            resp = await client.post("/api/chat/slots", json={"name": "s1"})
            assert resp.status == 200
            slot = state._slots["s1"]
            slot.title = "Auto generated"
            slot._titled = True
            slot._title_origin = _TITLE_ORIGIN_AUTO
            epoch_before = slot._title_epoch

            # Re-address the SAME slot with an explicit pinned title.
            resp = await client.post(
                "/api/chat/slots", json={"name": "s1", "title": "Pinned by caller"}
            )
            assert resp.status == 200
            assert slot.title == "Pinned by caller"
            assert slot._title_origin == _TITLE_ORIGIN_USER
            assert slot._title_epoch == epoch_before + 1

        # And the refresh refuses to touch the pinned name.
        calls = _patch_generator(monkeypatch, "Model idea")
        slot.messages = [
            {"role": "user", "content": f"m{i}"}
            for i in range(_TITLE_REFRESH_MILESTONES[0])
        ]
        await maybe_refresh_title(_fake_state(), slot)
        assert calls == []
        assert slot.title == "Pinned by caller"


# ── server-review regressions: resume hydration, pin persistence, cancel path ─
class TestResumeRehydratesProvenance:
    """The HTTP resume path is the THIRD slot-hydration path; it must restore
    title provenance + the consumed refresh budget like the persistence
    loaders, or the refresh is silently disabled after resume-from-History."""

    @pytest.mark.asyncio
    async def test_resume_restores_auto_origin_and_mark(self, tmp_path, monkeypatch):
        from aiohttp.test_utils import TestClient, TestServer
        from chat_test_helpers import _make_app, _make_state

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        log = state.conversation_log
        log.append("dashboard:s1", "user", "hello")
        log.update_metadata(
            "dashboard:s1",
            {"title": "Auto name", "title_origin": "auto", "title_refresh_mark": 8},
        )

        async with TestClient(TestServer(_make_app(state))) as client:
            # ``key`` uses the filename-stem spelling list_sessions() returns,
            # matching what resume deep links actually carry.
            resp = await client.post("/api/chat/slots/s1/resume", json={"key": "dashboard_s1"})
            assert resp.status == 200
            slot = state._slots["s1"]
            assert slot._titled is True
            assert slot._title_origin == _TITLE_ORIGIN_AUTO, "refresh stays enabled"
            assert slot._title_refresh_mark == 8, "consumed budget not re-spendable"

    @pytest.mark.asyncio
    async def test_resume_legacy_title_maps_to_user(self, tmp_path, monkeypatch):
        from aiohttp.test_utils import TestClient, TestServer
        from chat_test_helpers import _make_app, _make_state

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        log = state.conversation_log
        log.append("dashboard:s1", "user", "hello")
        log.update_metadata("dashboard:s1", {"title": "Pre-existing name"})

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/resume", json={"key": "dashboard_s1"})
            assert resp.status == 200
            slot = state._slots["s1"]
            assert slot._title_origin == _TITLE_ORIGIN_USER, (
                "legacy origin-less title must stay conservative (never refreshed)"
            )

    @pytest.mark.asyncio
    async def test_resume_with_caller_title_is_a_pin(self, tmp_path, monkeypatch):
        from aiohttp.test_utils import TestClient, TestServer
        from chat_test_helpers import _make_app, _make_state

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.conversation_log.append("dashboard:s1", "user", "hello")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/resume",
                json={"key": "dashboard:s1", "title": "Pinned on resume"},
            )
            assert resp.status == 200
            slot = state._slots["s1"]
            assert slot.title == "Pinned on resume"
            assert slot._title_origin == _TITLE_ORIGIN_USER
            assert slot._title_epoch == 1


class TestCreatePinPersists:
    @pytest.mark.asyncio
    async def test_title_only_pin_is_persisted(self, tmp_path, monkeypatch):
        """A pinned title WITHOUT a folder must still persist the slot —
        otherwise a restart rehydrates the previous title with a refreshable
        "auto" origin and the background refresh may rewrite the pin."""
        from unittest.mock import AsyncMock

        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer
        from chat_test_helpers import _make_state

        from kiro_crew.dashboard import chat_handlers
        from kiro_crew.dashboard.chat import api_chat_slot_create

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        save_spy = AsyncMock()
        monkeypatch.setattr(chat_handlers, "save_slot_off_loop", save_spy)
        state = _make_state(tmp_path)
        app = web.Application()
        app["state"] = state
        app.router.add_post("/api/chat/slots", api_chat_slot_create)

        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/slots", json={"name": "s1", "title": "Pinned, no folder"}
            )
            assert resp.status == 200
            assert save_spy.await_count == 1, "pin without folder must persist"
            saved_slot = save_spy.await_args.args[1]
            assert saved_slot.title == "Pinned, no folder"
            assert saved_slot._title_origin == _TITLE_ORIGIN_USER


class TestRefreshCancelSafety:
    @pytest.mark.asyncio
    async def test_mark_is_persisted_before_generation(self, monkeypatch):
        """The consumed milestone is persisted BEFORE the generation await, so
        a task cancellation mid-generation (gateway shutdown) can never leave
        the disk on the old mark and re-spend the budget after restart."""
        import asyncio

        order: list[str] = []

        async def _spy_persist(_state, s):
            order.append(f"persist:{s._title_refresh_mark}")
            return True

        async def _cancelled_generation(_state, _messages, _current):
            order.append("generate")
            raise asyncio.CancelledError()

        monkeypatch.setattr(chat_title, "_persist_title", _spy_persist)
        monkeypatch.setattr(chat_title, "_generate_refreshed_title", _cancelled_generation)
        slot = _titled_slot(_TITLE_REFRESH_MILESTONES[0])
        with pytest.raises(asyncio.CancelledError):
            await maybe_refresh_title(_fake_state(), slot)
        assert order[0] == f"persist:{_TITLE_REFRESH_MILESTONES[0]}"
        assert order[1] == "generate"
        assert slot._title_in_flight is False


class TestMilestoneUnderSpend:
    @pytest.mark.asyncio
    async def test_late_first_attempt_consumes_all_lower_milestones(self, monkeypatch):
        """DELIBERATE under-spend: one attempt at turn >= the last milestone
        consumes every milestone at or below it — a late-eligible session gets
        ONE refresh, never a catch-up burst. The budget is a ceiling."""
        calls = _patch_generator(monkeypatch, "New Title")
        slot = _titled_slot(_TITLE_REFRESH_MILESTONES[-1] + 5)
        state = _fake_state()
        await maybe_refresh_title(state, slot)
        assert len(calls) == 1
        await maybe_refresh_title(state, slot)
        assert len(calls) == 1, "no catch-up second call for the skipped milestone"


class TestResumeTitleEchoIsNotAPin:
    """The sidebar's resume call ALWAYS sends a title (``title || key``), and
    that value can be a STALE echo of an older name (notification deep link,
    sidebar row rendered before a background refresh landed). Persisted
    metadata is therefore AUTHORITATIVE on resume: the request title applies
    only when no persisted title exists."""

    @pytest.mark.asyncio
    async def test_echo_of_persisted_title_rehydrates_provenance(self, tmp_path, monkeypatch):
        from aiohttp.test_utils import TestClient, TestServer
        from chat_test_helpers import _make_app, _make_state

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        log = state.conversation_log
        log.append("dashboard:s1", "user", "hello")
        log.update_metadata(
            "dashboard:s1",
            {"title": "Auto name", "title_origin": "auto", "title_refresh_mark": 8},
        )

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/resume",
                json={"key": "dashboard_s1", "title": "Auto name"},  # echo
            )
            assert resp.status == 200
            slot = state._slots["s1"]
            assert slot.title == "Auto name"
            assert slot._title_origin == _TITLE_ORIGIN_AUTO, (
                "an echoed title must not be classified as a user pin"
            )
            assert slot._title_refresh_mark == 8

    @pytest.mark.asyncio
    async def test_key_placeholder_echo_is_not_a_pin(self, tmp_path, monkeypatch):
        from aiohttp.test_utils import TestClient, TestServer
        from chat_test_helpers import _make_app, _make_state

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        log = state.conversation_log
        log.append("dashboard:s1", "user", "hello")
        log.update_metadata("dashboard:s1", {"title": "Auto name", "title_origin": "auto"})

        async with TestClient(TestServer(_make_app(state))) as client:
            # resumeChatSlot sends `title: title || key` — the key placeholder.
            resp = await client.post(
                "/api/chat/slots/s1/resume",
                json={"key": "dashboard_s1", "title": "dashboard_s1"},
            )
            assert resp.status == 200
            slot = state._slots["s1"]
            assert slot.title == "Auto name", "persisted title wins over placeholder"
            assert slot._title_origin == _TITLE_ORIGIN_AUTO

    @pytest.mark.asyncio
    async def test_stale_request_title_never_reverts_refreshed_name(self, tmp_path, monkeypatch):
        """The blocking scenario: the background refresh renamed the session
        on disk, then a resume arrives carrying the PRE-refresh title (stale
        client cache). The stale title must neither revert the refreshed name
        nor lock the session as user-origin."""
        from aiohttp.test_utils import TestClient, TestServer
        from chat_test_helpers import _make_app, _make_state

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        log = state.conversation_log
        log.append("dashboard:s1", "user", "hello")
        log.update_metadata(
            "dashboard:s1",
            {"title": "Refreshed name", "title_origin": "auto", "title_refresh_mark": 8},
        )

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/resume",
                json={"key": "dashboard_s1", "title": "Old pre-refresh name"},  # stale
            )
            assert resp.status == 200
            slot = state._slots["s1"]
            assert slot.title == "Refreshed name", "stale echo must not revert the refresh"
            assert slot._title_origin == _TITLE_ORIGIN_AUTO, "must not lock as user-origin"
            assert slot._title_refresh_mark == 8


# ── round-5 regressions: durable-mark gate, post-persist push guard ──────────
class TestRefreshDurableMarkGate:
    @pytest.mark.asyncio
    async def test_failed_mark_persist_aborts_before_generation(self, monkeypatch):
        """If the consumed milestone cannot be made durable (history write
        failure), the refresh must NOT spend the LLM call — a restart would
        reload the old mark and repeat the milestone, breaking the budget."""
        generated: list[str] = []

        async def _failing_persist(_state, _slot):
            return False

        async def _spy_generate(_state, _messages, current):
            generated.append(current)
            return "New Title"

        monkeypatch.setattr(chat_title, "_persist_title", _failing_persist)
        monkeypatch.setattr(chat_title, "_generate_refreshed_title", _spy_generate)
        slot = _titled_slot(_TITLE_REFRESH_MILESTONES[0])
        state = _fake_state()
        await maybe_refresh_title(state, slot)
        assert generated == [], "no LLM spend on a non-durable mark"
        assert slot.title == "Initial auto title"
        assert slot._title_in_flight is False
        state.push_slot_title.assert_not_called()

    @pytest.mark.asyncio
    async def test_persist_returns_false_on_write_failure(self, tmp_path):
        from kiro_crew.history import ConversationLog

        log = ConversationLog(base_dir=tmp_path)
        log.append("dashboard:chat-1-1", "user", "seed")

        def _boom(_key, _fields):
            raise OSError("disk full")

        log.update_metadata = _boom  # type: ignore[method-assign]
        slot = _ChatSlot("chat-1-1")
        slot.title = "T"
        slot._titled = True
        slot._title_origin = _TITLE_ORIGIN_AUTO
        state = MagicMock()
        state.conversation_log = log
        assert await chat_title._persist_title(state, slot) is False

    @pytest.mark.asyncio
    async def test_persist_returns_true_without_log(self):
        state = MagicMock()
        state.conversation_log = None
        slot = _ChatSlot("chat-1-1")
        assert await chat_title._persist_title(state, slot) is True


class TestRefreshPushGuard:
    @pytest.mark.asyncio
    async def test_rename_during_final_persist_is_not_overwritten_in_sidebar(
        self, monkeypatch
    ):
        """A rename landing during the refresh's final persist await has
        already broadcast its name; the refresh must not push its stale title
        over it. The push (if any) must carry the slot's CURRENT title."""
        slot = _titled_slot(_TITLE_REFRESH_MILESTONES[0])
        persist_calls = {"n": 0}

        async def _persist_with_rename(_state, s):
            persist_calls["n"] += 1
            if persist_calls["n"] == 2:
                # The FINAL persist (after the refresh assigned its title):
                # simulate a manual rename landing during this await.
                s.title = "User chosen name"
                s._title_origin = _TITLE_ORIGIN_USER
                s._title_epoch += 1
            return True

        _patch_generator(monkeypatch, "Refreshed title")
        monkeypatch.setattr(chat_title, "_persist_title", _persist_with_rename)
        state = _fake_state()
        await maybe_refresh_title(state, slot)
        assert slot.title == "User chosen name"
        state.push_slot_title.assert_not_called(), (
            "stale refresh title must not be broadcast over the rename's push"
        )


class TestResumeCorruptedTitleMetadata:
    @pytest.mark.asyncio
    async def test_non_string_persisted_title_does_not_crash_resume(self, tmp_path, monkeypatch):
        """A non-string ``title`` in a corrupted/legacy session JSONL must be
        treated as absent — not redacted (TypeError → HTTP 500)."""
        from aiohttp.test_utils import TestClient, TestServer
        from chat_test_helpers import _make_app, _make_state

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        log = state.conversation_log
        log.append("dashboard:s1", "user", "hello")
        log.update_metadata("dashboard:s1", {"title": 12345})  # corrupted: non-string

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/resume",
                json={"key": "dashboard_s1", "title": "Caller name"},
            )
            assert resp.status == 200, "corrupted title metadata must not 500 the resume"
            slot = state._slots["s1"]
            # Non-string persisted title == absent → the caller-supplied name
            # applies via the never-titled pin branch.
            assert slot.title == "Caller name"
            assert slot._title_origin == _TITLE_ORIGIN_USER
