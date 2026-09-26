"""Tests for the background session-title refresh.

The feature: instead of a ``set_session_title`` tool exposed to every chat, the
existing background auto-title flow is made flexible — an AUTO title is
re-examined at bounded user-turn milestones via the same ``_bg`` one-liner
path, and swapped when the model says the old name does not fit.

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

import asyncio
import json
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew.dashboard import chat_persistence, chat_title
from kiro_crew.dashboard.chat_title import (
    _TITLE_EARLY_REFRESH_MILESTONE,
    _TITLE_ORIGIN_AUTO,
    _TITLE_ORIGIN_USER,
    _TITLE_REFRESH_MILESTONES,
    _build_refresh_prompt,
    _rehydrated_refresh_mark,
    _title_refresh_due,
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

    async def _fake(_state, _messages, current_title, *, session_key: str = ""):
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

        async def _rename_mid_flight(_state, _messages, _current, *, session_key: str = ""):
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
        prompt = _build_refresh_prompt([{"role": "user", "content": "hello"}], "My current title")
        assert prompt is not None
        assert "My current title" in prompt
        assert "KEEP" in prompt
        assert "===== CONVERSATION TO NAME =====" in prompt

    def test_prompt_windows_the_recent_tail(self):
        messages = [{"role": "user", "content": f"topic-{i} discussion"} for i in range(30)]
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
        prompt = _build_refresh_prompt([{"role": "user", "content": "hello"}], "t" * 500)
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
        user reaches for the control when the current name does not fit, and
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
        slot.messages = [{"role": "user", "content": f"topic-{i} discussion"} for i in range(30)]
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


class TestManualRegenerateRaceGuard:
    """The manual regenerate endpoint stands down when a rename lands during
    its own await -- the same ``_title_epoch`` contract ``maybe_refresh_title``
    documents and enforces at its two re-check points. The name the user typed
    outranks a generated one that was already in flight."""

    @pytest.mark.asyncio
    async def test_rename_during_generation_wins(self, monkeypatch):
        slot = _ChatSlot("chat-1-1")
        slot.messages = [{"role": "user", "content": "hello world task"}]
        slot.title = "Old auto title"
        slot._titled = True
        slot._title_origin = _TITLE_ORIGIN_AUTO

        async def _rename_mid_flight(_sessions, _prompt, **_kw):
            # A manual rename landing mid-await, exactly as
            # api_chat_slot_rename writes it.
            slot.title = "User chosen name"
            slot._title_origin = _TITLE_ORIGIN_USER
            slot._title_epoch += 1
            return "Model suggestion"

        async def _noop(*_a, **_kw):
            return None

        monkeypatch.setattr(chat_title, "run_bg_oneliner", _rename_mid_flight)
        monkeypatch.setattr(chat_title, "_persist_title", _noop)
        monkeypatch.setattr(chat_title, "_ui_language", lambda: "")

        state = _fake_state()
        state._slots = {slot.key: slot}
        epoch_after_rename = slot._title_epoch + 1
        request = MagicMock()
        request.app = {"state": state}
        request.match_info = {"slot": slot.key}

        response = await chat_title.api_chat_slot_generate_title(request)

        assert response.status == 200
        payload = json.loads(response.body.decode())
        assert payload == {"ok": True, "title": ""}, (
            "the stand-down must answer with the endpoint's existing "
            "nothing-was-applied shape so the client keeps the user's name"
        )
        assert slot.title == "User chosen name"
        assert slot._title_origin == _TITLE_ORIGIN_USER
        assert slot._title_epoch == epoch_after_rename, "no epoch bump on stand-down"
        state.push_slot_title.assert_not_called()

    @pytest.mark.asyncio
    async def test_rename_during_persist_is_not_broadcast_over(self, monkeypatch):
        """Second re-check point: the rename lands while OUR persist awaits, so
        it has already pushed its own name, so broadcasting our own stale title
        would overwrite it in the sidebar."""
        slot = _ChatSlot("chat-1-1")
        slot.messages = [{"role": "user", "content": "hello world task"}]
        slot.title = "Old auto title"
        slot._titled = True
        slot._title_origin = _TITLE_ORIGIN_AUTO

        async def _generate(_sessions, _prompt, **_kw):
            return "Model suggestion"

        async def _rename_during_persist(_state, _slot):
            slot.title = "User chosen name"
            slot._title_origin = _TITLE_ORIGIN_USER
            slot._title_epoch += 1
            return True

        monkeypatch.setattr(chat_title, "run_bg_oneliner", _generate)
        monkeypatch.setattr(chat_title, "_persist_title", _rename_during_persist)
        monkeypatch.setattr(chat_title, "_ui_language", lambda: "")

        state = _fake_state()
        state._slots = {slot.key: slot}
        request = MagicMock()
        request.app = {"state": state}
        request.match_info = {"slot": slot.key}

        response = await chat_title.api_chat_slot_generate_title(request)

        assert response.status == 200
        assert json.loads(response.body.decode()) == {"ok": True, "title": ""}
        assert slot.title == "User chosen name"
        state.push_slot_title.assert_not_called()

    @pytest.mark.asyncio
    async def test_quiet_window_still_applies_the_generated_title(self, monkeypatch):
        """The guard must not break the ordinary path: with no rename in the
        window the generated title is written, persisted and pushed."""
        slot = _ChatSlot("chat-1-1")
        slot.messages = [{"role": "user", "content": "hello world task"}]
        slot.title = "Old auto title"
        slot._titled = True
        slot._title_origin = _TITLE_ORIGIN_AUTO

        async def _generate(_sessions, _prompt, **_kw):
            return "Model suggestion"

        async def _noop(*_a, **_kw):
            return None

        monkeypatch.setattr(chat_title, "run_bg_oneliner", _generate)
        monkeypatch.setattr(chat_title, "_persist_title", _noop)
        monkeypatch.setattr(chat_title, "_ui_language", lambda: "")

        state = _fake_state()
        state._slots = {slot.key: slot}
        epoch_before = slot._title_epoch
        request = MagicMock()
        request.app = {"state": state}
        request.match_info = {"slot": slot.key}

        response = await chat_title.api_chat_slot_generate_title(request)

        assert json.loads(response.body.decode()) == {
            "ok": True,
            "title": "Model suggestion",
        }
        assert slot.title == "Model suggestion"
        assert slot._title_epoch == epoch_before + 1
        state.push_slot_title.assert_called_once_with(slot.key, "Model suggestion")


# ── origin recording on the write paths ──────────────────────────────────────
class TestOriginRecording:
    @pytest.mark.asyncio
    async def test_auto_title_success_records_auto_origin(self, monkeypatch):
        async def _fake_generate(_state, _messages, *, session_key: str = ""):
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
        async def _fake_generate(_state, _messages, *, session_key: str = ""):
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
    async def test_auto_title_stands_down_when_rename_lands_mid_generation(self, monkeypatch):
        slot = _ChatSlot("chat-1-1")
        slot.messages = [{"role": "user", "content": "hello world task"}]

        async def _rename_mid_flight(_state, _messages, *, session_key: str = ""):
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
    def test_shared_slot_hydration_restores_complete_title_state(self, monkeypatch):
        calls: list[tuple[str, str]] = []

        def _redact_urls(value: str):
            calls.append(("urls", value))
            return f"url-safe:{value}", []

        def _redact_credentials(value: str):
            calls.append(("credentials", value))
            return f"credential-safe:{value}", []

        monkeypatch.setattr(chat_persistence, "redact_exfiltration_urls", _redact_urls)
        monkeypatch.setattr(chat_persistence, "redact_credentials", _redact_credentials)
        slot = _ChatSlot("chat-1-1")

        chat_persistence._rehydrate_slot_title(
            slot,
            "Model title",
            titled=True,
            metadata={"title_origin": "auto", "title_refresh_mark": 8},
        )

        assert calls == [
            ("urls", "Model title"),
            ("credentials", "url-safe:Model title"),
        ]
        assert slot.title == "credential-safe:url-safe:Model title"
        assert slot._titled is True
        assert slot._title_origin == "auto"
        assert slot._title_refresh_mark == 8

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


# ── rehydration: the mark is re-based against the rows a reload holds ────────
def _restore_state(tmp_path, monkeypatch):
    from kiro_crew.dashboard.state import DashboardState
    from kiro_crew.history import ConversationLog

    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    sessions = MagicMock(count=0)
    sessions.get_pid = MagicMock(return_value=None)
    return DashboardState(
        sessions=sessions,
        crons=MagicMock(list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})),
        lessons=MagicMock(load_all=MagicMock(return_value=[])),
        start_time=0.0,
        conversation_log=ConversationLog(base_dir=tmp_path),
    )


def _write_transcript(tmp_path, rows: list[dict], *, mark: int) -> None:
    """Write ``dashboard_chat1`` as the JSONL the loaders read: one metadata line,
    then the rows, with the refresh mark persisted the way _persist_title does."""
    meta = {
        "_type": "metadata",
        "created_at": "2026-03-23T10:00:00",
        "last_consolidated": 0,
        "title": "Auto name",
        "title_origin": _TITLE_ORIGIN_AUTO,
        "title_refresh_mark": mark,
    }
    lines = [json.dumps(meta), *(json.dumps(r) for r in rows)]
    (tmp_path / "dashboard_chat1.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _turn_rows(user_turns: int) -> list[dict]:
    rows: list[dict] = []
    for i in range(user_turns):
        rows.append({"role": "user", "content": f"turn {i}", "ts": "2026-03-23T10:00:00"})
        rows.append({"role": "assistant", "content": "ok", "ts": "2026-03-23T10:00:01"})
    return rows


#: The three loaders that restore a slot's message window from disk: the two
#: chat_persistence restart paths and the History resume endpoint, whose
#: ``chat_handlers._hydrate_slot_from_history`` import also shares.
_LOADERS = ("single", "recent", "resume")


async def _rehydrate(state, driver: str) -> _ChatSlot:
    """Load ``chat1`` through one of the three loaders (see ``_LOADERS``)."""
    if driver == "single":
        slot = chat_persistence._rehydrate_slot_from_history(state, "chat1")
        assert slot is not None
        return slot
    if driver == "resume":
        from aiohttp.test_utils import TestClient, TestServer
        from chat_test_helpers import _make_app

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/chat1/resume", json={"key": "dashboard:chat1"}
            )
            assert resp.status == 200, await resp.text()
        return state._slots["chat1"]
    from kiro_crew.dashboard.chat import restore_recent_sessions

    assert restore_recent_sessions(state, window_minutes=60) == 1
    return state._slots["chat1"]


class TestRehydratedMarkRebase:
    """A reload holds only the latest 500 rows, so the restored user count sits
    below the count the persisted mark was taken over. The mark is re-based at
    rehydrate so the opt-in cadence continues from the restored count, while a
    spent built-in milestone stays spent: when the reload keeps fewer user
    turns than the largest built-in milestone the session had already reached,
    the mark stays at that milestone and the cadence resumes after it, not
    after the restored count."""

    def test_rebase_never_changes_a_built_in_schedule_verdict(self):
        # With and without the early milestone, for every persisted mark,
        # restored count and later count, the re-based mark must answer exactly
        # as the persisted one does or a reload could re-spend a milestone.
        schedules = (
            _TITLE_REFRESH_MILESTONES,
            (_TITLE_EARLY_REFRESH_MILESTONE, *_TITLE_REFRESH_MILESTONES),
        )
        for mark in range(0, 61):
            for count in range(0, 61):
                rebased = _rehydrated_refresh_mark(mark, count)
                assert 0 <= rebased <= mark
                for milestones in schedules:
                    for later_count in range(count, 61):
                        assert _title_refresh_due(rebased, later_count, 0, milestones) == (
                            _title_refresh_due(mark, later_count, 0, milestones)
                        ), (mark, count, later_count, milestones)

    def test_rebase_rules(self):
        expected = {
            (20, 10): 10,
            (20, 5): 8,
            (5, 0): 1,
            (24, 20): 24,
            (30, 20): 24,
            (300, 250): 250,
            (10, 250): 10,
            (0, 250): 0,
        }
        for (mark, count), result in expected.items():
            assert _rehydrated_refresh_mark(mark, count) == result, (mark, count)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("driver", _LOADERS)
    async def test_cadence_continues_n_turns_after_a_restore(self, tmp_path, monkeypatch, driver):
        # 300 user turns on disk, the last cadence attempt spent at turn 300.
        # The loader keeps 500 rows = 250 user turns, so the mark comes back
        # as 250 and the N=10 cadence fires ten turns after the restore
        # instead of staying silent for fifty.
        _write_transcript(tmp_path, _turn_rows(300), mark=300)
        state = _restore_state(tmp_path, monkeypatch)
        slot = await _rehydrate(state, driver)
        restored = sum(1 for m in slot.messages if m.get("role") == "user")
        assert restored == 250
        assert slot._title_refresh_mark == 250
        assert slot._title_origin == _TITLE_ORIGIN_AUTO

        calls = _patch_generator(monkeypatch, "KEEP-unused")
        monkeypatch.setattr(chat_title, "_title_refresh_every", lambda: 10)
        fired_at: list[int] = []
        for i in range(15):
            slot.append("user", f"after restart {i}", broadcast=False)
            slot.append("assistant", "ok", broadcast=False)
            await maybe_refresh_title(state, slot)
            if len(calls) > len(fired_at):
                fired_at.append(restored + i + 1)
        assert fired_at == [restored + 10]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("driver", _LOADERS)
    async def test_cadence_mark_between_milestones_rebases_to_the_window(
        self, tmp_path, monkeypatch, driver
    ):
        # The 500-row reload window drops the first ten user/assistant pairs,
        # leaving ten user turns under a persisted cadence mark of twenty.
        rows = _turn_rows(20) + [{"role": "assistant", "content": "more"} for _ in range(480)]
        _write_transcript(tmp_path, rows, mark=20)
        state = _restore_state(tmp_path, monkeypatch)
        slot = await _rehydrate(state, driver)
        restored = sum(1 for message in slot.messages if message.get("role") == "user")
        assert restored == 10

        calls = _patch_generator(monkeypatch, "KEEP-unused")
        monkeypatch.setattr(chat_title, "_title_refresh_every", lambda: 4)
        fired_at: list[int] = []
        for i in range(3):
            slot.append("user", f"after reload {i}", broadcast=False)
            slot.append("assistant", "ok", broadcast=False)
            await maybe_refresh_title(state, slot)
            if len(calls) > len(fired_at):
                fired_at.append(restored + i + 1)
        assert fired_at == [12]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("driver", _LOADERS)
    async def test_spent_built_in_milestones_stay_spent(self, tmp_path, monkeypatch, driver):
        # Both built-in milestones were spent (mark 24). The window holds only
        # 20 user turns: 30 turns then 460 assistant rows, 520 rows in all, so
        # the loader drops the first 20 rows. The mark stays 24 and the
        # built-in schedule fires nothing more.
        rows = _turn_rows(30) + [{"role": "assistant", "content": "more"} for _ in range(460)]
        _write_transcript(tmp_path, rows, mark=max(_TITLE_REFRESH_MILESTONES))
        state = _restore_state(tmp_path, monkeypatch)
        slot = await _rehydrate(state, driver)
        assert sum(1 for m in slot.messages if m.get("role") == "user") == 20
        assert slot._title_refresh_mark == max(_TITLE_REFRESH_MILESTONES)

        calls = _patch_generator(monkeypatch, "New Title")
        monkeypatch.setattr(chat_title, "_title_refresh_every", lambda: 0)
        for i in range(10):
            slot.append("user", f"after restart {i}", broadcast=False)
            slot.append("assistant", "ok", broadcast=False)
            await maybe_refresh_title(state, slot)
        assert calls == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("driver", _LOADERS)
    async def test_floor_bound_mark_resumes_the_cadence_after_the_milestone(
        self, tmp_path, monkeypatch, driver
    ):
        # Same transcript as test_spent_built_in_milestones_stay_spent: mark 24
        # over 30 user turns, and the window keeps 20 of them. The floor holds
        # the mark at 24, above the restored count, so the N=4 cadence resumes
        # after turn 24: the first refresh lands at turn 28, eight turns after
        # the reload, not at 24.
        rows = _turn_rows(30) + [{"role": "assistant", "content": "more"} for _ in range(460)]
        _write_transcript(tmp_path, rows, mark=max(_TITLE_REFRESH_MILESTONES))
        state = _restore_state(tmp_path, monkeypatch)
        slot = await _rehydrate(state, driver)
        restored = sum(1 for m in slot.messages if m.get("role") == "user")
        assert restored == 20

        calls = _patch_generator(monkeypatch, "KEEP-unused")
        monkeypatch.setattr(chat_title, "_title_refresh_every", lambda: 4)
        fired_at: list[int] = []
        for i in range(8):
            slot.append("user", f"after reload {i}", broadcast=False)
            slot.append("assistant", "ok", broadcast=False)
            await maybe_refresh_title(state, slot)
            if len(calls) > len(fired_at):
                fired_at.append(restored + i + 1)
        assert fired_at == [28]

    @pytest.mark.asyncio
    async def test_a_transcript_that_fits_the_window_keeps_its_mark(self, tmp_path, monkeypatch):
        # 60 rows sit inside resume's 500-row window, so the restored user count
        # is the count the mark was taken over and the re-base moves nothing.
        _write_transcript(tmp_path, _turn_rows(30), mark=30)
        state = _restore_state(tmp_path, monkeypatch)
        slot = await _rehydrate(state, "resume")
        assert slot._disk_older_count == 0
        assert sum(1 for m in slot.messages if m.get("role") == "user") == 30
        assert slot._title_refresh_mark == 30

    @pytest.mark.asyncio
    async def test_import_surfaces_every_row_and_keeps_its_mark(self, tmp_path, monkeypatch):
        # Import routes through the same materialiser with ``window_limit=None``:
        # all 600 rows are held (no frozen prefix), so the user count is the
        # count the mark was taken over and the re-base moves nothing, where
        # resume's 500-row window would have pulled the mark down to 250.
        from kiro_crew.dashboard import chat_handlers

        _write_transcript(tmp_path, _turn_rows(300), mark=300)
        state = _restore_state(tmp_path, monkeypatch)
        log = state.conversation_log
        slot = chat_handlers._materialise_slot_from_history(
            state,
            name="chat1",
            history_key="dashboard:chat1",
            meta=log.get_metadata("dashboard:chat1"),
            all_messages=log.read_messages_chained("dashboard:chat1"),
            window_limit=None,
            disk_meta_observed=False,
            broadcast_rows=False,
            mint_missing_mids=True,
        )
        state.end_slot_construction(slot.key)
        assert slot._disk_older_count == 0
        assert sum(1 for m in slot.messages if m.get("role") == "user") == 300
        assert slot._title_refresh_mark == 300


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


# ── dashboard.title_refresh_every_turns: the opt-in cadence ─────────────────
def _drive(slot, turns: int):
    for i in range(turns):
        slot.messages.append({"role": "user", "content": f"turn {i}"})
        slot.messages.append({"role": "assistant", "content": "ok"})
        yield sum(1 for m in slot.messages if m.get("role") == "user")


class TestRefreshCadence:
    def test_unset_cadence_is_exactly_the_built_in_schedule(self):
        for mark in range(0, 30):
            for user_count in range(0, 40):
                expected = any(mark < m <= user_count for m in _TITLE_REFRESH_MILESTONES)
                assert (
                    _title_refresh_due(mark, user_count, 0, _TITLE_REFRESH_MILESTONES) == expected
                )

    def test_cadence_replaces_the_built_in_milestones(self):
        # N=10: due at 10, not at the built-in 8 or 24.
        assert not _title_refresh_due(0, 8, 10, _TITLE_REFRESH_MILESTONES)
        assert _title_refresh_due(0, 10, 10, _TITLE_REFRESH_MILESTONES)
        assert not _title_refresh_due(20, 24, 10, _TITLE_REFRESH_MILESTONES)
        assert _title_refresh_due(20, 30, 10, _TITLE_REFRESH_MILESTONES)

    def test_crossed_multiples_cost_one_refresh(self):
        # A rehydrated session at turn 35 with nothing consumed: one refresh now,
        # then nothing until the next multiple.
        assert _title_refresh_due(0, 35, 10, _TITLE_REFRESH_MILESTONES)
        assert not _title_refresh_due(35, 39, 10, _TITLE_REFRESH_MILESTONES)
        assert _title_refresh_due(35, 40, 10, _TITLE_REFRESH_MILESTONES)

    def test_low_signal_early_milestone_survives_the_cadence(self):
        milestones = (_TITLE_EARLY_REFRESH_MILESTONE, *_TITLE_REFRESH_MILESTONES)
        assert _title_refresh_due(0, _TITLE_EARLY_REFRESH_MILESTONE, 10, milestones)

    @pytest.mark.asyncio
    async def test_cadence_drives_one_call_per_multiple(self, monkeypatch):
        calls = _patch_generator(monkeypatch, "KEEP-unused")
        monkeypatch.setattr(chat_title, "_title_refresh_every", lambda: 10)
        slot = _titled_slot(0)
        state = _fake_state()
        fired_at: list[int] = []
        for user_count in _drive(slot, 45):
            before = len(calls)
            await maybe_refresh_title(state, slot)
            if len(calls) > before:
                fired_at.append(user_count)
        assert fired_at == [10, 20, 30, 40]

    @pytest.mark.asyncio
    async def test_cadence_never_touches_a_renamed_title(self, monkeypatch):
        calls = _patch_generator(monkeypatch, "New Title")
        monkeypatch.setattr(chat_title, "_title_refresh_every", lambda: 4)
        slot = _titled_slot(0, origin=_TITLE_ORIGIN_USER)
        state = _fake_state()
        for _ in _drive(slot, 20):
            await maybe_refresh_title(state, slot)
        assert calls == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("landing", ["rename", "refresh"])
    async def test_what_lands_during_the_config_hop_stands_the_attempt_down(
        self, monkeypatch, landing
    ):
        calls = _patch_generator(monkeypatch, "New Title")
        slot = _titled_slot(10)

        def _read_while_something_lands() -> int:
            # Runs in the worker thread, while the event loop is free to run a
            # rename or another turn's refresh.
            if landing == "rename":
                slot._title_origin = _TITLE_ORIGIN_USER
                slot._title_epoch += 1
            else:
                slot._title_in_flight = True
            return 10

        monkeypatch.setattr(chat_title, "_title_refresh_every", _read_while_something_lands)
        await maybe_refresh_title(_fake_state(), slot)
        assert calls == []
        assert slot._title_refresh_mark == 0
        if landing == "refresh":
            # The other attempt owns the flag; standing down must not clear it.
            assert slot._title_in_flight is True

    @pytest.mark.asyncio
    async def test_two_attempts_in_the_config_hop_at_once_spend_one_call(self, monkeypatch):
        # Two chat_done chains reach the same due slot and both sit in the
        # config thread hop at the same moment. After the hop, the re-check,
        # the due test and the in-flight claim run in one synchronous segment,
        # so whichever attempt resumes first claims the slot and the other
        # stands down: one generation call, the mark consumed once.
        calls = _patch_generator(monkeypatch, "New Title")
        both_in_hop = threading.Barrier(2, timeout=5)

        def _hold_both_in_the_hop() -> int:
            both_in_hop.wait()  # BrokenBarrierError if only one attempt arrives
            return 0

        monkeypatch.setattr(chat_title, "_title_refresh_every", _hold_both_in_the_hop)
        slot = _titled_slot(_TITLE_REFRESH_MILESTONES[0])
        state = _fake_state()
        await asyncio.gather(maybe_refresh_title(state, slot), maybe_refresh_title(state, slot))
        assert calls == ["Initial auto title"]
        assert slot._title_refresh_mark == _TITLE_REFRESH_MILESTONES[0]
        assert slot._title_in_flight is False
        assert slot.title == "New Title"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("every", "initial_turns"), [(0, 7), (10, 9)])
    async def test_follow_up_landing_during_config_hop_keeps_its_milestone(
        self, monkeypatch, every, initial_turns
    ):
        calls = _patch_generator(monkeypatch, "New Title")
        slot = _titled_slot(initial_turns)
        initial_mark = slot._title_refresh_mark
        landed = False

        def _read_while_follow_up_lands() -> int:
            nonlocal landed
            if not landed:
                slot.messages.append({"role": "user", "content": "queued follow-up"})
                landed = True
            return every

        monkeypatch.setattr(chat_title, "_title_refresh_every", _read_while_follow_up_lands)
        state = _fake_state()
        await maybe_refresh_title(state, slot)
        assert calls == []
        assert slot._title_refresh_mark == initial_mark

        await maybe_refresh_title(state, slot)
        assert calls == ["Initial auto title"]

    @pytest.mark.asyncio
    async def test_the_count_stops_rising_once_the_window_is_full(self, monkeypatch):
        # Once this full window drops one user row for each new user row, its
        # retained user count stays at four and later cadence marks are not due.
        from kiro_crew.dashboard import state as state_mod

        monkeypatch.setattr(state_mod, "_MAX_SLOT_MESSAGES", 8)
        calls = _patch_generator(monkeypatch, "KEEP-unused")
        monkeypatch.setattr(chat_title, "_title_refresh_every", lambda: 4)
        slot = _titled_slot(0)
        state = _fake_state()
        for i in range(12):
            slot.append("user", f"turn {i}", broadcast=False)
            slot.append("assistant", "ok", broadcast=False)
            await maybe_refresh_title(state, slot)
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_refresh_fires_while_a_full_window_drops_assistant_rows(self, monkeypatch):
        from kiro_crew.dashboard import state as state_mod

        monkeypatch.setattr(state_mod, "_MAX_SLOT_MESSAGES", 8)
        calls = _patch_generator(monkeypatch, "KEEP-unused")
        monkeypatch.setattr(chat_title, "_title_refresh_every", lambda: 4)
        slot = _titled_slot(0)
        slot.messages = [{"role": "assistant", "content": f"prefill {i}"} for i in range(8)]
        state = _fake_state()
        fired_at: list[int] = []
        for turn in range(1, 9):
            slot.append("user", f"turn {turn}", broadcast=False)
            assert len(slot.messages) == 8
            slot.append("assistant", "ok", broadcast=False)
            assert len(slot.messages) == 8
            await maybe_refresh_title(state, slot)
            if len(calls) > len(fired_at):
                fired_at.append(turn)
        assert fired_at == [4]

    def test_a_changed_value_applies_on_the_next_read(self, tmp_path):
        # "Takes effect on the next turn; no restart", measured through the real
        # config load rather than a patched reader.
        cfg = tmp_path / "config.json"
        with patch("kiro_crew.config.loader.config_path", return_value=cfg):
            for every in (10, 100):
                cfg.write_text(
                    json.dumps({"dashboard": {"title_refresh_every_turns": every}}),
                    encoding="utf-8",
                )
                assert chat_title._title_refresh_every() == every

    def test_every_copy_of_the_help_names_the_enforced_values(self):
        # The schema help, the operator docs row and the spec sentence each state
        # the floor, the ceiling, the built-in turns and the retained window, and
        # each value is read from the code that enforces it.
        from dataclasses import fields
        from pathlib import Path

        from kiro_crew.config import sections
        from kiro_crew.dashboard.state import _MAX_SLOT_MESSAGES

        floor = sections.TITLE_REFRESH_EVERY_TURNS_MIN
        ceiling = sections.TITLE_REFRESH_EVERY_TURNS_MAX
        first, second = _TITLE_REFRESH_MILESTONES
        window = f"{_MAX_SLOT_MESSAGES:,}"
        key = "`dashboard.title_refresh_every_turns`"
        help_text = next(
            f.metadata["help"]
            for f in fields(sections.DashboardConfig)
            if f.name == "title_refresh_every_turns"
        )
        root = Path(__file__).resolve().parents[1]
        docs = (root / "src/kiro_crew/docs/configuration.md").read_text(encoding="utf-8")
        doc_row = next(line for line in docs.splitlines() if key in line)
        spec = (root / "docs/system-specs/modules/learn-cron-dashboard.md").read_text(
            encoding="utf-8"
        )
        start = spec.index(key)
        spec_text = spec[start : spec.index("opt-in", start)]

        assert f"raised to {floor}" in help_text and f"ceiling is {ceiling}" in help_text
        assert f"raised to {floor}" in doc_row and f"ceiling {ceiling}" in doc_row
        assert f"`0` or {floor}-{ceiling}" in spec_text
        for text in (help_text, doc_row):
            assert f"turns {first} and {second}" in text
            assert "after the first turn" in text
        for text in (help_text, doc_row, spec_text):
            assert window in text
            # 500 is a literal in the loaders (``messages[-500:]``); no constant.
            assert "500" in text
            assert "no lifetime cap" not in text

    def test_failed_lookup_falls_back_to_the_built_in_schedule(self, monkeypatch):
        def _boom():
            raise OSError("config unreadable")

        monkeypatch.setattr(chat_title.KiroCrewConfig, "load", staticmethod(_boom))
        assert chat_title._title_refresh_every() == 0

    @pytest.mark.parametrize(
        ("raw", "parsed"),
        [
            (0, 0),
            (1, 4),
            (3, 4),
            (4, 4),
            (10, 10),
            ("12", 12),
            (5000, 1000),
            (-5, 0),
            (True, 0),
            ("nonsense", 0),
        ],
    )
    def test_loader_bounds(self, raw, parsed):
        from kiro_crew.config.loader import _build_dashboard_config

        config = _build_dashboard_config(set(), {"title_refresh_every_turns": raw})
        assert config.title_refresh_every_turns == parsed

    def test_default_is_off(self):
        from kiro_crew.config.loader import _build_dashboard_config

        assert _build_dashboard_config(set(), {}).title_refresh_every_turns == 0


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
            {"role": "user", "content": f"m{i}"} for i in range(_TITLE_REFRESH_MILESTONES[0])
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
            assert (
                slot._title_origin == _TITLE_ORIGIN_USER
            ), "legacy origin-less title must stay conservative (never refreshed)"

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

        async def _cancelled_generation(_state, _messages, _current, *, session_key: str = ""):
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
            assert (
                slot._title_origin == _TITLE_ORIGIN_AUTO
            ), "an echoed title must not be classified as a user pin"
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

        async def _spy_generate(_state, _messages, current, *, session_key: str = ""):
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
    async def test_rename_during_final_persist_is_not_overwritten_in_sidebar(self, monkeypatch):
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
        await maybe_refresh_title(_fake_state(), slot)
        assert calls == ["Initial auto title"]
        assert slot.title == "S3 bucket policy violation triage"

    @pytest.mark.asyncio
    async def test_ordinary_title_is_not_due_at_one_user_message(self, monkeypatch):
        calls = _patch_generator(monkeypatch, "New Title")
        slot = _titled_slot(1)
        assert slot._title_low_signal is False
        await maybe_refresh_title(_fake_state(), slot)
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
        await maybe_refresh_title(_fake_state(), slot)
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
        await chat_title._maybe_auto_title(_fake_state(), slot)
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
        await chat_title._maybe_auto_title(_fake_state(), slot)
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
        await chat_title._maybe_auto_title(_fake_state(), slot)
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
        state.conversation_log.update_metadata = lambda _k, fields: recorded.append(fields)
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

    Regression (review finding): when the on-send titling task was still
    awaiting its LLM call at chat_done, the chained ``_maybe_auto_title``
    bounced off the ``_title_in_flight`` guard and ``maybe_refresh_title``
    bounced off the not-titled guard; the low-signal title then locked AFTER
    both, and a one-message session — which gets no later chat_done — kept the
    URL echo indefinitely. ``title_then_refresh`` must wait the attempt out.
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
            await gate.wait()
            return "Research ticket T8412000027"  # URL echo → low-signal

        self._patch_title_path(monkeypatch, _slow_generate)
        refresh_calls = _patch_generator(monkeypatch, "S3 bucket policy triage")

        slot = self._one_message_url_slot()
        state = _fake_state()
        # The on-send attempt is still awaiting its LLM call at chat_done.
        slot._title_task = asyncio.create_task(chat_title._maybe_auto_title(state, slot))
        await asyncio.sleep(0)  # let it take the in-flight guard
        assert slot._title_in_flight is True

        chained = asyncio.create_task(chat_title.title_then_refresh(state, slot))
        await asyncio.sleep(0)
        assert not chained.done(), "chain must wait, not bounce off the guards"
        assert refresh_calls == []

        gate.set()  # LLM answers; the on-send attempt locks the low-signal title
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
        await chat_title.title_then_refresh(_fake_state(), slot)
        assert refresh_calls == ["Research ticket T8412000027"]
        assert slot.title == "S3 bucket policy triage"

    @pytest.mark.asyncio
    async def test_cancelled_attempt_leaves_the_retry_to_do_the_work(self, monkeypatch):
        gate = asyncio.Event()

        async def _slow_generate(_state, _messages, *, session_key: str = ""):
            await gate.wait()
            return "never returned"

        self._patch_title_path(monkeypatch, _slow_generate)
        refresh_calls = _patch_generator(monkeypatch, "S3 bucket policy triage")

        slot = self._one_message_url_slot()
        state = _fake_state()
        slot._title_task = asyncio.create_task(chat_title._maybe_auto_title(state, slot))
        await asyncio.sleep(0)
        slot._title_task.cancel()
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

        async def _slow_persist(_state, _slot):
            await gate.wait()
            return True  # honor the bool contract — the durable-mark gate reads it

        async def _fast_generate(_state, _messages, *, session_key: str = ""):
            return "Research ticket T8412000027"  # URL echo → low-signal

        self._patch_title_path(monkeypatch, _fast_generate)
        monkeypatch.setattr(chat_title, "_persist_title", _slow_persist)
        refresh_calls = _patch_generator(monkeypatch, "S3 bucket policy triage")

        slot = self._one_message_url_slot()
        state = _fake_state()
        slot._title_task = asyncio.create_task(chat_title._maybe_auto_title(state, slot))
        await asyncio.sleep(0)  # generate returns; attempt now awaits persist
        await asyncio.sleep(0)
        assert slot._titled is True, "attempt must have locked the title already"
        assert slot._title_in_flight is True, "guard must still be held (persist pending)"

        chained = asyncio.create_task(chat_title.title_then_refresh(state, slot))
        await asyncio.sleep(0)
        assert not chained.done(), "chain must wait, not bounce off the in-flight guard"
        assert refresh_calls == []

        gate.set()  # persist lands; the attempt releases the guard
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
