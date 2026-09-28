"""``reset_conversation`` — the stateless directive that gives a session a
clean model context at its next turn boundary.

The effect is deferred rather than applied inline because the producer runs
INSIDE the turn it would tear down: a conversation discard shuts the provider
down, and the immediate route (``POST /api/chat/slots/{slot}/reset-conversation``)
refuses a busy slot for exactly that reason. These tests pin the tool's payload,
the applier's queuing, the confirmation text a model reads, and the two
provenance gates that keep a borrowed slot safe from a headless caller.

The consumer half — that a queued discard is actually applied at the turn
boundary — lives in ``test_chat_runner_coverage.py::TestConsumePendingReset``.
"""

import pytest

from kiro_crew import mcp_core, session_directive
from kiro_crew.dashboard.session_directive_apply import apply_session_directive

# ───────────────────────────── the tool ──────────────────────────────────────


class TestResetConversationTool:
    """Stateless: the tool validates its arguments and returns a directive. It
    resolves no session identity and makes no HTTP call."""

    def test_takes_no_arguments_and_never_replays(self):
        """A caller asking for a clean context must GET one. Replaying the
        transcript into the fresh conversation returns most of what the reset was
        meant to reclaim, so it is not offered as a choice — the HTTP route
        carries the flag for the rare caller that wants it."""
        result = mcp_core._call_tool_inner("reset_conversation", {})
        assert session_directive.decode(result, "reset_conversation") == {}

    def test_a_caller_passing_replay_is_rejected(self):
        from kiro_crew.validation import ValidationError

        with pytest.raises(ValidationError):
            mcp_core._call_tool_inner("reset_conversation", {"replay": True})

    def test_listed_in_tools_with_no_parameters(self):
        descriptor = next(t for t in mcp_core._list_tools() if t["name"] == "reset_conversation")
        schema = descriptor["inputSchema"]
        assert schema["type"] == "object"
        assert schema["properties"] == {}
        assert not schema.get("required")

    def test_confirmation_warns_the_transcript_survives(self):
        """The model must not read this as "the record is gone" — the user can
        still see every earlier message, so a summary written for the user's
        benefit would be wrong about what they lost."""
        result = mcp_core._call_tool_inner("reset_conversation", {})
        human = session_directive.strip_marker(result)
        assert "transcript is not deleted" in human.lower()

    def test_is_a_recognized_directive_tool(self):
        """The forgery gate honours a marker only under a canonical directive
        tool name; omitting the tool here would make every marker it emits
        inert."""
        assert "reset_conversation" in session_directive.DIRECTIVE_TOOLS


# ───────────────────────────── the applier ───────────────────────────────────


class _FakeSlot:
    """Minimal slot: the applier touches only ``key`` and the two pending-discard
    fields. ``linked_session_key`` exists so a test can rebind the slot the way a
    cron or workflow injection does."""

    def __init__(self, key: str = "dashboard:test-slot", linked: str = ""):
        self.key = key
        self.linked_session_key = linked
        self._pending_discard_conversation_key = None


class TestResetConversationApplier:
    @pytest.mark.asyncio
    async def test_queues_the_session_key_the_caller_captured(self):
        slot = _FakeSlot()
        result = await apply_session_directive(
            None,
            slot,
            slot.key,
            "reset_conversation",
            {},
            producer_is_user_facing=True,
        )
        assert slot._pending_discard_conversation_key == slot.key
        assert "queued" in result

    @pytest.mark.asyncio
    async def test_a_rebound_slot_cannot_redirect_the_discard(self):
        """``linked_session_key`` is MUTABLE: a cron or workflow injection can
        rebind the live slot between the turn that asked for the reset and the
        consume that applies it. Resolving the key from the slot would discard
        whatever it points at by then — session B — and leave A, the conversation
        the caller actually meant, untouched. The authoritative key is the one
        the turn ran on, which the caller passes in."""
        slot = _FakeSlot(linked="slack:B")
        session_key = "slack:A"

        await apply_session_directive(
            None,
            slot,
            session_key,
            "reset_conversation",
            {},
            producer_is_user_facing=True,
        )

        assert slot._pending_discard_conversation_key == "slack:A"
        assert slot._pending_discard_conversation_key != slot.linked_session_key

    @pytest.mark.asyncio
    async def test_confirmation_says_the_transcript_stays(self):
        slot = _FakeSlot()
        result = await apply_session_directive(
            None,
            slot,
            slot.key,
            "reset_conversation",
            {},
            producer_is_user_facing=True,
        )
        assert "transcript is untouched" in result

    @pytest.mark.asyncio
    async def test_headless_producer_refused_without_arming_the_slot(self):
        """A cron turn can run on a user's slot and a sub-agent shares its
        parent's. Either one wiping that conversation is the failure this gate
        exists to prevent, so the refusal must leave nothing queued."""
        slot = _FakeSlot()
        result = await apply_session_directive(
            None,
            slot,
            slot.key,
            "reset_conversation",
            {},
            producer_is_user_facing=False,
        )
        assert result.startswith("Error:")
        assert slot._pending_discard_conversation_key is None

    @pytest.mark.asyncio
    async def test_slotless_caller_refused(self):
        """A channel transport's TurnDriver holds no slot for the effect to land
        on. Refused as a decision here rather than left to crash the applier."""
        result = await apply_session_directive(
            None,
            None,
            "slack:C123:456",
            "reset_conversation",
            {},
            producer_is_user_facing=True,
        )
        assert result.startswith("Error:")
        assert "holds none" in result
