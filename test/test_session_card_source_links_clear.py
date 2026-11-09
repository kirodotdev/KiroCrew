"""A session card's source-link chips start over at ``/clear``.

The chips are derived from the transcript, and ``/clear`` keeps the transcript:
the KAS path leaves every row visible, and a native clear empties only the
in-memory window, so a restore reads the earlier rows back. Without a boundary
the card kept showing the pull requests of a conversation the person had
already dropped.
"""

from __future__ import annotations

import pytest

from kiro_crew.dashboard.state import _ChatSlot

OLD = "https://github.com/acme/widgets/pull/11"
NEW = "https://github.com/acme/widgets/pull/12"
OLD_ISSUE = "https://github.com/acme/widgets/issues/21"


def _urls(slot: _ChatSlot) -> list[str]:
    return [link["url"] for link in slot.source_links_payload()["links"]]


class TestClearBoundary:
    def test_links_named_before_a_clear_are_dropped(self):
        slot = _ChatSlot("s1")
        slot.append("assistant", f"opened {OLD} and {OLD_ISSUE}", ts="t1")
        slot.append("user", "/clear", ts="t2")
        slot.append("assistant", "🗑 Conversation cleared.", ts="t3")

        assert _urls(slot) == []
        summary = slot.to_dict()
        assert summary["source_links"] == []
        assert summary["source_links_total"] == 0

    def test_links_named_after_the_clear_are_kept(self):
        slot = _ChatSlot("s1")
        slot.append("assistant", f"opened {OLD}", ts="t1")
        slot.append("user", "/clear", ts="t2")
        slot.append("assistant", f"now on {NEW}", ts="t3")

        assert _urls(slot) == [NEW]
        assert slot.to_dict()["source_links_total"] == 1

    def test_only_the_newest_clear_is_the_boundary(self):
        slot = _ChatSlot("s1")
        slot.append("assistant", f"opened {OLD}", ts="t1")
        slot.append("user", "/clear", ts="t2")
        slot.append("assistant", f"now on {NEW}", ts="t3")
        slot.append("user", "/CLEAR", ts="t4")

        assert _urls(slot) == []

    def test_a_link_re_mentioned_after_the_clear_comes_back(self):
        slot = _ChatSlot("s1")
        slot.append("assistant", f"opened {OLD}", ts="t1")
        slot.append("user", "/clear", ts="t2")
        assert _urls(slot) == []

        slot.append("user", f"look at {OLD} again", ts="t3")
        assert _urls(slot) == [OLD]

    def test_a_session_with_no_clear_is_unchanged(self):
        slot = _ChatSlot("s1")
        slot.append("assistant", f"opened {OLD}", ts="t1")
        slot.append("user", "please clear the cache", ts="t2")
        slot.append("assistant", f"and {NEW}", ts="t3")

        assert _urls(slot) == [NEW, OLD]


class TestIsClearCommandRow:
    @pytest.mark.parametrize(
        "content",
        ["/clear", "/Clear", "  /clear  ", "/clear now please"],
    )
    def test_user_clear_commands_match(self, content):
        from kiro_crew.dashboard.slot_projection import is_clear_command_row

        assert is_clear_command_row({"role": "user", "content": content})

    @pytest.mark.parametrize(
        "msg",
        [
            {"role": "assistant", "content": "/clear"},
            {"role": "user", "content": "/clearly not"},
            {"role": "user", "content": "please /clear"},
            {"role": "user", "content": ""},
            {"role": "user", "content": None},
            {"role": "user"},
            "/clear",
            None,
        ],
    )
    def test_everything_else_does_not(self, msg):
        from kiro_crew.dashboard.slot_projection import is_clear_command_row

        assert not is_clear_command_row(msg)
