"""Test that injected context is excluded from the Slack mirror.

Regression test for the fix in chat_runner.py that saves the raw user
message before context/persona prepend and uses _prepare_mirror_msg()
for the Slack mirror. Without the fix, [Background context from ...]
blocks leak into the linked Slack thread.
"""

from __future__ import annotations

from kiro_crew.dashboard.chat_runner import _prepare_mirror_msg


class TestPrepareMirrorMsg:
    """Tests for _prepare_mirror_msg — the helper that truncates and
    redacts user messages before posting to the Slack mirror."""

    def test_truncates_to_500(self):
        """Messages longer than 500 chars are truncated."""
        long_msg = "x" * 1000
        result = _prepare_mirror_msg(long_msg)
        assert len(result) <= 500

    def test_short_message_unchanged(self):
        """Short messages pass through without modification."""
        msg = "Hello, please investigate this ticket"
        result = _prepare_mirror_msg(msg)
        assert result == msg

    def test_context_not_in_mirror_when_saved_before_prepend(self):
        """Simulate the chat_runner logic: _user_msg_for_mirror is captured
        BEFORE _pending_context is prepended to message, so context never
        reaches _prepare_mirror_msg.

        The context block comes from the REAL ``drain_pending_context`` (not a
        hand-built literal), so a frame change — e.g. the #4780 silent-
        consumption contract line — flows through this assertion instead of
        leaving it green against a stale copy. (The snapshot ORDER itself is
        replicated here, not exercised; that would need a _run_chat-level
        test.)
        """
        from types import SimpleNamespace

        from kiro_crew.dashboard.chat_runner import drain_pending_context

        user_msg = "Hello, please investigate"

        # --- Replicate chat_runner.py logic ---
        message = user_msg

        # Save raw message for mirror BEFORE context prepend
        _user_msg_for_mirror = message

        # Prepend pending context to message (for LLM) — real drain output.
        slot = SimpleNamespace(
            _pending_context=[
                {
                    "content": "SECRET BRIEFING: investigate ticket V123",
                    "source": "ticket-dispatch",
                }
            ],
            # The drain also asks the slot to shed note content authorized
            # against a session it has since left. Nothing here is note content,
            # so the stand-in reports zero dropped — matching the real
            # ``_ChatSlot.drop_foreign_authorized_notes`` return type. Satisfied
            # here rather than guarded in the drain: that call is a fail-closed
            # authorization filter, so making it tolerate a slot without the
            # method would silently skip it for every such caller.
            drop_foreign_authorized_notes=lambda: 0,
            # The drain BUMPS this on consumption, so a slot save that exported
            # the queue before the drain discards its now-stale copy instead of
            # persisting entries already given to the model. Supplied here for
            # the same reason as the filter above: making the drain tolerate a
            # slot without the counter would silently disable that protection
            # for every such caller.
            _pending_context_gen=0,
        )
        context_block = drain_pending_context(slot)
        message = context_block + "\n" + message

        # Prepare mirror from saved raw message
        mirror_msg = _prepare_mirror_msg(_user_msg_for_mirror)

        # The LLM message includes context
        assert "SECRET BRIEFING" in message
        assert "Background context from" in message

        # The mirror message does NOT include context
        assert "SECRET BRIEFING" not in mirror_msg
        assert "Background context from" not in mirror_msg
        assert mirror_msg == "Hello, please investigate"

    def test_subagent_failures_included(self):
        """Subagent failure prepend happens BEFORE the mirror save point,
        so failures ARE visible in the mirror (intentional)."""
        message = "Continue investigating"

        # Lines 629-632: prepend failures BEFORE mirror save
        failures = ["[Subagent X failed: timeout]"]
        message = "\n\n".join(failures) + "\n\n" + message

        # Line 635: save for mirror (AFTER failure prepend)
        _user_msg_for_mirror = message

        mirror_msg = _prepare_mirror_msg(_user_msg_for_mirror)
        assert "[Subagent X failed: timeout]" in mirror_msg
