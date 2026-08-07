"""A wave digest is a system injection, not something the user typed.

The batch digest's prefix (``[Subagent batch completion event]``) is a SIBLING of
the per-agent prefix, not an extension of it, so a `startswith` check written for
one silently misses the other. When that happened the digest was classified as a
plain user message: it merged with real user input, and the transcript recorded
it under the ``user`` role, which renders the machine-facing prompt as a chat
bubble.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from chat_test_helpers import _make_state

from kiro_crew.dashboard.chat import _dequeue_next_message
from kiro_crew.dashboard.chat_runner import _start_next_queued_turn
from kiro_crew.dashboard.chat_utils import is_system_injection
from kiro_crew.dashboard.state import (
    SUBAGENT_BATCH_COMPLETION_PREFIX,
    SUBAGENT_COMPLETION_PREFIX,
    SUBAGENT_COMPLETION_PREFIXES,
    _ChatSlot,
)

BATCH_DIGEST = (
    f"{SUBAGENT_BATCH_COMPLETION_PREFIX}\n"
    "Batch results 1/1 — wave finished: 2 ✅ · 0 ❌ · 0 ⏹ of 2 agents. "
    "All results delivered.\n"
    "This run is complete.\n"
    "\n"
    "— `a1` ✅ first task\n"
    "— `a2` ✅ second task"
)


class TestIsSystemInjection:
    def test_both_subagent_shapes_are_system_injections(self) -> None:
        assert is_system_injection(f"{SUBAGENT_COMPLETION_PREFIX}\nAgent `a1` completed ✅")
        assert is_system_injection(BATCH_DIGEST)

    def test_prefix_tuple_holds_every_shape_the_checks_must_cover(self) -> None:
        """The tuple is what both the drain and is_system_injection iterate; a new
        injected shape added to one but missing here reopens this bug class."""
        assert SUBAGENT_COMPLETION_PREFIX in SUBAGENT_COMPLETION_PREFIXES
        assert SUBAGENT_BATCH_COMPLETION_PREFIX in SUBAGENT_COMPLETION_PREFIXES

    def test_plain_user_message_is_not(self) -> None:
        assert not is_system_injection("also add tests")
        # A mention of the marker mid-message is not an injection.
        assert not is_system_injection("what is [Subagent batch completion event]?")


class TestBatchDigestQueueSemantics:
    def test_batch_digest_is_never_merged_with_user_input(self) -> None:
        """Merging would splice a 60 kB machine-facing digest into the user's own
        prompt, and the merged turn would render as one user bubble."""
        slot = _ChatSlot("s1")
        slot._queue = [
            {"id": "a", "content": "fix the bug"},
            {"id": "b", "content": BATCH_DIGEST},
        ]
        for item in slot._queue:
            slot.append("queued", item["content"], "msg msg-queued")

        next_msg, consumed = _dequeue_next_message(slot, merge_enabled=True)

        assert next_msg == "fix the bug"
        assert [c["content"] for c in consumed] == ["fix the bug"]
        assert [q["content"] for q in slot._queue] == [BATCH_DIGEST]

    @pytest.mark.asyncio
    async def test_drained_batch_digest_lands_under_the_subagent_role(self, tmp_path) -> None:
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("batch-role")
        slot.queue_append(BATCH_DIGEST)

        with patch("kiro_crew.dashboard.chat_runner.spawn_guarded_turn") as spawn:
            spawn.return_value = MagicMock()
            started = await _start_next_queued_turn(state, slot)

        assert started is True
        roles = [m["role"] for m in slot.messages if m["role"] != "queued"]
        assert roles == ["subagent"]
