"""Tests for the session-title behavior:

- ``_ChatSlot.display_title`` shows "New Session…" for untitled slots (brand-new
  empty sessions and the pre-LLM window), never the bare chat-N key.
- ``_fallback_title_from_messages`` truncates the first user message with an
  ellipsis when the LLM can't title the chat.
- ``_maybe_auto_title`` SKIP fallback, in-flight guard, and mode-independence.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from kiro_crew.dashboard.chat_title import (
    _fallback_title_from_messages,
)
from kiro_crew.dashboard.state import NEW_SESSION_TITLE, _ChatSlot


def _fake_state():
    state = MagicMock()
    # conversation_log must be truthy for _persist_title to attempt a write.
    state.conversation_log = MagicMock()
    return state


class TestDisplayTitle:
    def test_untitled_default_shows_new_session(self):
        slot = _ChatSlot("chat-4-1783603256")  # title defaults to key
        assert slot.title == slot.key
        assert slot._titled is False
        assert slot.display_title == NEW_SESSION_TITLE

    def test_label_value(self):
        assert NEW_SESSION_TITLE == "New Session…"

    def test_titled_slot_shows_real_title(self):
        slot = _ChatSlot("chat-4-1783603256", title="Debug flaky test")
        slot._titled = True
        assert slot.display_title == "Debug flaky test"

    def test_non_key_title_shows_through_even_if_untitled(self):
        # e.g. a plan/cron/slack session that set a title without _titled.
        slot = _ChatSlot("chat-4-1783603256", title="Plan: task-7")
        assert slot._titled is False
        assert slot.display_title == "Plan: task-7"

    def test_empty_title_shows_new_session(self):
        slot = _ChatSlot("chat-4-1783603256", title="")
        # title defaults to key when empty is passed, but force-empty to simulate
        # a resume path that stored no title.
        slot.title = ""
        assert slot.display_title == NEW_SESSION_TITLE

    def test_resumed_dashboard_key_form_shows_new_session(self):
        # Resume can set title to the dashboard_-prefixed key form while the
        # slot key is stripped — still an identifier, not a name.
        slot = _ChatSlot("chat-4-1783603256")
        slot.title = "dashboard_chat-4-1783603256"
        assert slot._titled is False
        assert slot.display_title == NEW_SESSION_TITLE


class TestFallbackTitle:
    def test_short_message_returned_whole(self):
        msgs = [{"role": "user", "content": "hi there"}]
        assert _fallback_title_from_messages(msgs) == "hi there"

    def test_long_message_truncated_with_ellipsis_on_word_boundary(self):
        long = "help me write a dockerfile for a go service with a multi stage build and caching"
        out = _fallback_title_from_messages([{"role": "user", "content": long}])
        assert out.endswith("…")
        assert len(out) <= 61  # <=60 chars + ellipsis
        # trimmed on a word boundary — no dangling space before the ellipsis
        assert not out[:-1].endswith(" ")
        assert long.startswith(out[:-1])

    def test_strips_image_attachment_and_keeps_user_text(self):
        attachment = f"![image](/Users/example/.kirocrew/uploads/{'b' * 240}.jpg)"
        msgs = [{"role": "user", "content": f"{attachment}\n\nsubagents seem to be failing"}]
        assert _fallback_title_from_messages(msgs) == "subagents seem to be failing"

    def test_attachment_only_returns_new_session_label(self):
        msgs = [{"role": "user", "content": "![image](/tmp/screenshot.jpg)"}]
        assert _fallback_title_from_messages(msgs) == NEW_SESSION_TITLE

    def test_strips_non_image_attachment_and_keeps_user_text(self):
        msgs = [
            {
                "role": "user",
                "content": "[attached_file 1] /tmp/report.txt\nreview report findings",
            }
        ]
        assert _fallback_title_from_messages(msgs) == "review report findings"

    def test_non_image_attachment_only_returns_new_session_label(self):
        msgs = [{"role": "user", "content": "[attached_file 1] /tmp/report.txt"}]
        assert _fallback_title_from_messages(msgs) == NEW_SESSION_TITLE

    def test_skips_attachment_only_message_for_later_user_text(self):
        msgs = [
            {"role": "user", "content": "![image](/tmp/screenshot.jpg)"},
            {"role": "user", "content": "fix title generation"},
        ]
        assert _fallback_title_from_messages(msgs) == "fix title generation"

    def test_no_user_text_returns_label(self):
        assert _fallback_title_from_messages([]) == NEW_SESSION_TITLE


class TestManualTitleFallback:
    def test_generation_failure_uses_sanitized_fallback(self):
        import asyncio

        from kiro_crew.dashboard import chat_title

        state = _fake_state()
        slot = _ChatSlot("chat-4-1783603256")
        slot.messages.append(
            {
                "role": "user",
                "content": (
                    "![image](/tmp/screenshot.png)\n\n"
                    "fix title generation\n"
                    "[attached_file 1] /tmp/debug.log"
                ),
            }
        )
        state._slots = {slot.key: slot}
        request = MagicMock()
        request.app = {"state": state}
        request.match_info = {"slot": slot.key}

        async def _fail(*_args, **_kwargs):
            raise RuntimeError("title service unavailable")

        original = chat_title._generate_title_via_kiro
        chat_title._generate_title_via_kiro = _fail  # type: ignore[assignment]
        try:
            response = asyncio.run(chat_title.api_chat_slot_generate_title(request))
        finally:
            chat_title._generate_title_via_kiro = original  # type: ignore[assignment]

        assert response.status == 200
        assert slot.title == "fix title generation"
        assert slot._titled is True

    def test_attachment_only_failure_keeps_auto_title_unlocked(self):
        import asyncio

        from kiro_crew.dashboard import chat_title

        state = _fake_state()
        slot = _ChatSlot("chat-4-1783603257")
        slot.messages.append({"role": "user", "content": "![image](/tmp/screenshot.png)"})
        state._slots = {slot.key: slot}
        request = MagicMock()
        request.app = {"state": state}
        request.match_info = {"slot": slot.key}

        async def _fail(*_args, **_kwargs):
            raise RuntimeError("title service unavailable")

        original = chat_title._generate_title_via_kiro
        chat_title._generate_title_via_kiro = _fail  # type: ignore[assignment]
        try:
            response = asyncio.run(chat_title.api_chat_slot_generate_title(request))
        finally:
            chat_title._generate_title_via_kiro = original  # type: ignore[assignment]

        assert response.status == 200
        assert response.text == '{"ok": true, "title": ""}'
        assert slot.display_title == NEW_SESSION_TITLE
        assert slot._titled is False
        state.push_slot_title.assert_not_called()


class TestAutoTitleInFlightGuard:
    """The on-send trigger and the end-of-turn trigger must not both hit the LLM."""

    def test_in_flight_guard_short_circuits(self):
        import asyncio

        from kiro_crew.dashboard import chat_title

        state = _fake_state()
        slot = _ChatSlot("chat-4-1783603256")
        slot.messages.append({"role": "user", "content": "debug my flaky test"})
        slot._title_in_flight = True  # simulate an attempt already running

        called = False

        async def _boom(*_a, **_k):
            nonlocal called
            called = True
            return "should not happen"

        orig = chat_title._generate_title_via_kiro
        chat_title._generate_title_via_kiro = _boom  # type: ignore[assignment]
        try:
            asyncio.run(chat_title._maybe_auto_title(state, slot))
        finally:
            chat_title._generate_title_via_kiro = orig  # type: ignore[assignment]

        assert called is False  # guard prevented the LLM call
        assert slot._titled is False

    def test_end_of_turn_retry_waits_for_send_time_attempt(self):
        import asyncio

        from kiro_crew.dashboard import chat_title

        async def _scenario():
            state = _fake_state()
            slot = _ChatSlot("chat-4-1783603256")
            slot.messages.append({"role": "user", "content": "debug my flaky test"})
            first_started = asyncio.Event()
            release_first = asyncio.Event()
            attempts = []

            async def _generate(_state, messages):
                attempts.append(list(messages))
                if len(attempts) == 1:
                    first_started.set()
                    await release_first.wait()
                    return ""
                return "Debug flaky test"

            original = chat_title._generate_title_via_kiro
            chat_title._generate_title_via_kiro = _generate  # type: ignore[assignment]
            try:
                send_attempt = asyncio.create_task(chat_title._maybe_auto_title(state, slot))
                await first_started.wait()
                slot.messages.append({"role": "assistant", "content": "I found the race."})

                # Simulate chat_done arriving while the on-send attempt is active.
                await chat_title._maybe_auto_title(state, slot)
                assert slot._title_retry_pending is True

                release_first.set()
                await send_attempt
            finally:
                chat_title._generate_title_via_kiro = original  # type: ignore[assignment]

            assert len(attempts) == 2
            assert all(m["role"] != "assistant" for m in attempts[0])
            assert any(m["role"] == "assistant" for m in attempts[1])
            assert slot.title == "Debug flaky test"
            assert slot._titled is True
            assert slot._title_retry_pending is False

        asyncio.run(_scenario())


class TestAutoTitleCancellation:
    def test_cancellation_does_not_start_pending_retry(self):
        import asyncio

        from kiro_crew.dashboard import chat_title

        async def _scenario():
            state = _fake_state()
            slot = _ChatSlot("chat-4-1783603258")
            slot.messages.extend(
                [
                    {"role": "user", "content": "name this session"},
                    {"role": "assistant", "content": "working on it"},
                ]
            )
            slot._title_retry_pending = True
            attempts = 0

            async def _cancel(*_args, **_kwargs):
                nonlocal attempts
                attempts += 1
                raise asyncio.CancelledError

            original = chat_title._generate_title_via_kiro
            chat_title._generate_title_via_kiro = _cancel  # type: ignore[assignment]
            try:
                try:
                    await chat_title._maybe_auto_title(state, slot)
                except asyncio.CancelledError:
                    pass
                else:
                    raise AssertionError("cancellation should propagate")
            finally:
                chat_title._generate_title_via_kiro = original  # type: ignore[assignment]

            assert attempts == 1
            assert slot._title_in_flight is False
            assert slot._title_retry_pending is False
            assert slot._titled is False

        asyncio.run(_scenario())


class TestSkipFallbackBranch:
    """On LLM SKIP: keep pending on the on-send attempt, fall back once the
    assistant has responded (definitive failure)."""

    def _run_with_skip(self, slot):
        import asyncio

        from kiro_crew.dashboard import chat_title

        state = _fake_state()

        async def _skip(*_a, **_k):
            return ""  # simulate SKIP/empty

        orig = chat_title._generate_title_via_kiro
        chat_title._generate_title_via_kiro = _skip  # type: ignore[assignment]
        try:
            asyncio.run(chat_title._maybe_auto_title(state, slot))
        finally:
            chat_title._generate_title_via_kiro = orig  # type: ignore[assignment]
        return state

    def test_on_send_skip_falls_back_but_stays_unlocked(self):
        # Only the user message present (on-send attempt). SKIP now shows the
        # truncated fallback immediately (fast), but leaves _titled False so the
        # end-of-turn attempt can still upgrade it to a real LLM title.
        slot = _ChatSlot("chat-9-1")
        slot.messages.append({"role": "user", "content": "something vague"})

        self._run_with_skip(slot)

        assert slot._titled is False
        assert slot.title == "something vague"  # short enough, no ellipsis

    def test_skip_after_response_falls_back_to_truncation(self):
        # Assistant has responded and the LLM still SKIP'd — definitive failure.
        slot = _ChatSlot("chat-9-2")
        slot.messages.append({"role": "user", "content": "a fairly vague opening question here"})
        slot.messages.append({"role": "assistant", "content": "some reply"})

        self._run_with_skip(slot)

        assert slot._titled is True
        assert slot.title == "a fairly vague opening question here"  # short enough, no ellipsis


class TestAutoTitleRunsForEveryMemoryMode:
    """Titling is not gated on ``memory_mode``.

    It used to bail on ``slot.blocks_reads`` (true only for ``temporary``),
    which left temporary tabs showing "New Session…" for their whole life.
    Titling reads only the slot's own messages, so no memory-privacy rule
    applies; the manual generate-title endpoint never had the guard either.
    """

    def _run(self, slot, generated="Debug flaky test"):
        import asyncio

        from kiro_crew.dashboard import chat_title

        state = _fake_state()
        attempts = []

        async def _generate(_state, messages):
            attempts.append(list(messages))
            return generated

        orig = chat_title._generate_title_via_kiro
        chat_title._generate_title_via_kiro = _generate  # type: ignore[assignment]
        try:
            asyncio.run(chat_title._maybe_auto_title(state, slot))
        finally:
            chat_title._generate_title_via_kiro = orig  # type: ignore[assignment]
        return state, attempts

    def test_temporary_slot_is_titled(self):
        slot = _ChatSlot("chat-10-1", memory_mode="temporary")
        assert slot.blocks_reads is True  # the mode the old guard rejected
        slot.messages.append({"role": "user", "content": "debug my flaky test"})

        state, attempts = self._run(slot)

        assert len(attempts) == 1  # the LLM was actually called
        assert slot.title == "Debug flaky test"
        assert slot._titled is True
        assert slot.display_title == "Debug flaky test"
        state.push_slot_title.assert_called_with(slot.key, "Debug flaky test")

    def test_temporary_slot_skip_still_falls_back(self):
        # SKIP handling must be identical for temporary slots — no early return
        # can short-circuit the truncated fallback.
        slot = _ChatSlot("chat-10-2", memory_mode="temporary")
        slot.messages.append({"role": "user", "content": "something vague"})
        slot.messages.append({"role": "assistant", "content": "some reply"})

        self._run(slot, generated="")

        assert slot.title == "something vague"
        assert slot._titled is True

    def test_incognito_slot_is_titled(self):
        # Incognito was never blocked (blocks_reads is temporary-only); assert it
        # so a future re-broadening of the gate to is_restricted is caught.
        slot = _ChatSlot("chat-10-3", memory_mode="incognito")
        assert slot.is_restricted is True
        assert slot.blocks_reads is False
        slot.messages.append({"role": "user", "content": "debug my flaky test"})

        _state, attempts = self._run(slot)

        assert len(attempts) == 1
        assert slot._titled is True
