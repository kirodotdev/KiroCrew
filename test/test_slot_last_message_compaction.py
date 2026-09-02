"""Tests that the sidebar `last_message` preview skips auto-compaction notices.

When a dashboard session crosses `session.autocompact_pct`, the auto-compact
path appends an assistant-role notice ("🔄 Auto-compacted at 93%.", see
`_AUTO_COMPACT_NOTICE` / `DashboardState`) tagged `meta.kind == "compaction"`.
The `/compact` slash command and its result banner
(`chat_utils._append_compaction_notice`) use the same tag. The session sidebar
reads `to_dict()["last_message"]`, so without a skip the notice would shadow the
real last turn. `to_dict` must surface the last real conversational message
instead. The skip keys on `meta.kind`, not the message text, so it covers every
compaction notice variant. Mirrors the frontend's `deriveFollowUpOptions` skip.
"""

from __future__ import annotations

from kiro_crew.dashboard.state import _ChatSlot

_COMPACT_META = {"kind": "compaction"}
_AUTO_COMPACT_MSG = "🔄 Auto-compacted at 93%."


class TestSlotLastMessageCompaction:
    def _slot_with(self, *messages) -> _ChatSlot:
        s = _ChatSlot("chat-1")
        for role, content, meta in messages:
            s.append(role, content, "msg msg-a", broadcast=False, meta=meta)
        return s

    def test_last_message_skips_auto_compaction_notice(self):
        s = self._slot_with(
            ("user", "do the thing", None),
            ("assistant", "here is the result of the thing", None),
            ("assistant", _AUTO_COMPACT_MSG, _COMPACT_META),
        )
        assert s.to_dict()["last_message"] == "here is the result of the thing"

    def test_last_message_skips_stacked_compaction_notices(self):
        s = self._slot_with(
            ("assistant", "the real last message", None),
            ("assistant", _AUTO_COMPACT_MSG, _COMPACT_META),
            ("assistant", "✅ Conversation compacted.", _COMPACT_META),
        )
        assert s.to_dict()["last_message"] == "the real last message"

    def test_last_message_unchanged_without_compaction(self):
        s = self._slot_with(
            ("user", "hi", None),
            ("assistant", "hello there", None),
        )
        assert s.to_dict()["last_message"] == "hello there"

    def test_last_message_options_survive_trailing_compaction(self):
        # A real turn ending in [OPTIONS:] followed by a compaction notice must
        # still report has_options (the preview/options come from the real turn).
        s = self._slot_with(
            ("assistant", "pick one [OPTIONS: A | B]", None),
            ("assistant", _AUTO_COMPACT_MSG, _COMPACT_META),
        )
        d = s.to_dict()
        assert d["has_options"] is True
        assert d["options"] == ["A", "B"]
