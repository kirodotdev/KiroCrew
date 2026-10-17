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

This file holds milestone gating, refresh outcomes, reply validation, the prompt, the
manual regenerate endpoint, origin recording, the reveal, rehydration and its mark
rebase, rename finality, the token budget and the opt-in cadence. The rest of the suite
sits beside it: ``test_title_refresh_persistence.py`` (persistence, pins and resume)
and ``test_title_refresh_low_signal.py`` (low-signal first-message titles). Shared
builders are in ``title_refresh_helpers.py``.
"""

from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from title_refresh_helpers import _fake_state, _patch_generator, _titled_slot

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


# ── milestone gating: the token budget ───────────────────────────────────────
class TestRefreshGating:
    @pytest.mark.asyncio
    async def test_not_due_before_first_milestone(self, monkeypatch):
        calls = _patch_generator(monkeypatch, "New Title")
        slot = _titled_slot(_TITLE_REFRESH_MILESTONES[0] - 1)
        await maybe_refresh_title(_fake_state(slot), slot)
        assert calls == []
        assert slot.title == "Initial auto title"

    @pytest.mark.asyncio
    async def test_due_at_first_milestone(self, monkeypatch):
        calls = _patch_generator(monkeypatch, "New Title")
        slot = _titled_slot(_TITLE_REFRESH_MILESTONES[0])
        await maybe_refresh_title(_fake_state(slot), slot)
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
        await maybe_refresh_title(_fake_state(slot), slot)
        assert calls == []
        assert slot.title == "Initial auto title"

    @pytest.mark.asyncio
    async def test_untitled_slot_is_not_refreshed(self, monkeypatch):
        calls = _patch_generator(monkeypatch, "New Title")
        slot = _titled_slot(_TITLE_REFRESH_MILESTONES[0])
        slot._titled = False
        slot._title_origin = ""
        await maybe_refresh_title(_fake_state(slot), slot)
        assert calls == []

    @pytest.mark.asyncio
    async def test_in_flight_guard_excludes_concurrent_attempts(self, monkeypatch):
        calls = _patch_generator(monkeypatch, "New Title")
        slot = _titled_slot(_TITLE_REFRESH_MILESTONES[0])
        slot._title_in_flight = True
        await maybe_refresh_title(_fake_state(slot), slot)
        assert calls == []

    @pytest.mark.asyncio
    async def test_rehydrated_mark_is_not_respent(self, monkeypatch):
        """A restart must not re-spend a consumed milestone: with mark=8 already
        persisted, only the SECOND milestone remains."""
        calls = _patch_generator(monkeypatch, "New Title")
        slot = _titled_slot(_TITLE_REFRESH_MILESTONES[0] + 1)
        slot._title_refresh_mark = _TITLE_REFRESH_MILESTONES[0]
        await maybe_refresh_title(_fake_state(slot), slot)
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
        state = _fake_state(slot)
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
        request.get.return_value = ""
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
        request.get.return_value = ""
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

        async def _rename_during_persist(_state, _slot, **_kw):
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
        request.get.return_value = ""
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
        request.get.return_value = ""
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
        await chat_title._maybe_auto_title(_fake_state(slot), slot)
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
        await chat_title._maybe_auto_title(_fake_state(slot), slot)
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
        state = _fake_state(slot)

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


def _write_transcript(
    tmp_path, rows: list[dict], *, mark: int, extra_meta: dict | None = None
) -> None:
    """Write ``dashboard_chat1`` as the JSONL the loaders read: one metadata line,
    then the rows, with the refresh mark persisted the way _persist_title does."""
    meta = {
        "_type": "metadata",
        "created_at": "2026-03-23T10:00:00",
        "last_consolidated": 0,
        "title": "Auto name",
        "title_origin": _TITLE_ORIGIN_AUTO,
        "title_refresh_mark": mark,
        **(extra_meta or {}),
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
    async def test_rebase_counts_the_opening_row_the_turn_marker_restores(
        self, tmp_path, monkeypatch, driver
    ):
        # 300 flushed user turns with the N=4 attempt spent at turn 300, then
        # the process died inside turn 301 before the periodic flush wrote its
        # opening row. Six trailing assistant rows push three more pairs out of
        # the 500-row window, so it holds 247 user turns, and the local-turn
        # marker re-appends turn 301's opener as the 248th. The re-base must
        # count that row: counting 247 leaves multiple 248 unspent, and the
        # cadence refreshes on the first turn after the restart instead of four
        # turns later.
        opener = {
            "role": "user",
            "content": "turn 300",
            "ts": "2026-03-23T10:05:00",
            "meta": {"mid": "m-unflushed"},
        }
        rows = _turn_rows(300) + [{"role": "assistant", "content": "more"} for _ in range(6)]
        _write_transcript(
            tmp_path,
            rows,
            mark=300,
            extra_meta={"turn_in_flight_generation": 3, "turn_in_flight_prompt": opener},
        )
        state = _restore_state(tmp_path, monkeypatch)
        slot = await _rehydrate(state, driver)
        restored = sum(1 for m in slot.messages if m.get("role") == "user")
        assert restored == 248
        assert slot.messages[-1]["content"] == "turn 300"
        assert slot._title_refresh_mark == 248

        calls = _patch_generator(monkeypatch, "KEEP-unused")
        monkeypatch.setattr(chat_title, "_title_refresh_every", lambda: 4)
        fired_at: list[int] = []
        for i in range(6):
            slot.append("user", f"after restart {i}", broadcast=False)
            slot.append("assistant", "ok", broadcast=False)
            await maybe_refresh_title(state, slot)
            if len(calls) > len(fired_at):
                fired_at.append(restored + i + 1)
        assert fired_at == [252]

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
        async def _saved(*_a, **_kw):
            # ``_persist_title`` returns True when the write landed. The sidebar
            # route ignores the result (best-effort save, as on main).
            return True

        monkeypatch.setattr(chat_title, "_persist_title", _saved)
        monkeypatch.setattr(chat_title, "sel", MagicMock())
        slot = _ChatSlot("chat-1-1")
        state = _fake_state()
        state._slots = {"chat-1-1": slot}
        request = MagicMock()
        request.get.return_value = ""
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
        await maybe_refresh_title(_fake_state(slot), slot)
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
        state = _fake_state(slot)
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
