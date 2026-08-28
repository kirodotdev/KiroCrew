"""Tests for _disk_older_durable_count: the durable-only frozen-prefix counter.

Verifies that absolute cursor positions are exact even after transient rows
(chunk, done, streaming, queued, permission) have been trimmed into the frozen
prefix. The counter is maintained alongside _disk_older_count at every set and
increment site.
"""

from __future__ import annotations

import pytest
from chat_test_helpers import _make_state

from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.chat_persistence import _TRANSIENT_ROLES
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.dashboard.state import _ChatSlot, _MAX_SLOT_MESSAGES, _TRANSIENT_ROLES_FOR_TRIM


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _slot(state, name: str = "test", **kwargs):
    return state.get_or_create_slot(name, **kwargs)


def _key(slot) -> str:
    return slot_history_key(slot)


def _append_messages(slot, messages):
    """Append a list of (role, content) pairs to the slot."""
    for role, content in messages:
        slot.append(role, content, "msg msg-u" if role == "user" else "msg msg-a")


# ---------------------------------------------------------------------------
# Test: _disk_older_durable_count initialization
# ---------------------------------------------------------------------------


class TestDurableCountInit:
    """_disk_older_durable_count is initialized to 0 on a fresh slot."""

    def test_fresh_slot_has_zero_durable_count(self, tmp_path):
        state = _make_state(tmp_path)
        slot = _slot(state)
        assert slot._disk_older_durable_count == 0
        assert slot._disk_older_count == 0


# ---------------------------------------------------------------------------
# Test: trim path increments _disk_older_durable_count correctly
# ---------------------------------------------------------------------------


class TestDurableCountTrim:
    """The trim path counts only non-transient roles among trimmed messages."""

    def test_trim_all_durable(self, tmp_path):
        """All trimmed messages are durable: both counters advance equally."""
        state = _make_state(tmp_path)
        slot = _slot(state)
        # Fill to just under the cap with durable messages
        for i in range(_MAX_SLOT_MESSAGES):
            slot.append("assistant", f"msg {i}", "msg msg-a", broadcast=False)
        # Mark entire window as persisted (simulates a flush)
        slot._disk_window_len = len(slot.messages)
        assert slot._disk_older_count == 0
        assert slot._disk_older_durable_count == 0
        # Append one more to trigger trim of 1 message
        slot.append("user", "trigger trim", "msg msg-u", broadcast=False)
        assert slot._disk_older_count == 1
        assert slot._disk_older_durable_count == 1

    def test_trim_all_transient(self, tmp_path):
        """All trimmed messages are transient: durable count stays 0."""
        state = _make_state(tmp_path)
        slot = _slot(state)
        # Fill with transient roles first, then durable to reach the cap
        transient_count = 5
        durable_count = _MAX_SLOT_MESSAGES - transient_count
        for role in ["chunk", "done", "streaming", "queued", "permission"]:
            slot.append(role, "transient", "msg msg-a", broadcast=False)
        for i in range(durable_count):
            slot.append("assistant", f"msg {i}", "msg msg-a", broadcast=False)
        slot._disk_window_len = len(slot.messages)
        # Append enough to trim exactly the 5 transient messages
        for i in range(transient_count):
            slot.append("user", f"trigger {i}", "msg msg-u", broadcast=False)
        assert slot._disk_older_count == transient_count
        assert slot._disk_older_durable_count == 0

    def test_trim_mixed(self, tmp_path):
        """Mixed transient and durable trimmed: durable count is exact."""
        state = _make_state(tmp_path)
        slot = _slot(state)
        # Place 3 transient + 2 durable at the front, then fill the rest durable
        mixed_front = [
            ("chunk", "t1"),
            ("user", "d1"),
            ("streaming", "t2"),
            ("assistant", "d2"),
            ("queued", "t3"),
        ]
        for role, content in mixed_front:
            slot.append(role, content, "msg msg-a", broadcast=False)
        remaining = _MAX_SLOT_MESSAGES - len(mixed_front)
        for i in range(remaining):
            slot.append("assistant", f"fill {i}", "msg msg-a", broadcast=False)
        slot._disk_window_len = len(slot.messages)
        # Trigger trim of 5 (the mixed front)
        for i in range(5):
            slot.append("user", f"new {i}", "msg msg-u", broadcast=False)
        assert slot._disk_older_count == 5
        # Only 2 of the 5 trimmed are durable (user, assistant)
        assert slot._disk_older_durable_count == 2

    def test_trim_unpersisted_not_counted(self, tmp_path):
        """Unpersisted overflow does not credit durable count."""
        state = _make_state(tmp_path)
        slot = _slot(state)
        for i in range(_MAX_SLOT_MESSAGES):
            slot.append("assistant", f"msg {i}", "msg msg-a", broadcast=False)
        # Simulate that NO messages have been flushed to disk
        slot._disk_window_len = 0
        slot.append("user", "overflow", "msg msg-u", broadcast=False)
        # Nothing credited because persisted_trim = min(1, 0) = 0
        assert slot._disk_older_count == 0
        assert slot._disk_older_durable_count == 0


# ---------------------------------------------------------------------------
# Test: restore sites compute _disk_older_durable_count correctly
# ---------------------------------------------------------------------------


class TestDurableCountRestore:
    """Restore paths count only non-transient roles in the frozen prefix."""

    def test_restore_with_mixed_prefix(self, tmp_path):
        """Simulates a restore where the frozen prefix has mixed roles."""
        state = _make_state(tmp_path)
        slot = _slot(state)
        # Simulate what chat_persistence does at restore time:
        # messages list with 510 items, 500 loaded into window, 10 in prefix
        messages = []
        # 3 transient in the prefix
        messages.append({"role": "chunk", "content": "c1"})
        messages.append({"role": "done", "content": ""})
        messages.append({"role": "streaming", "content": ""})
        # 7 durable in the prefix
        for i in range(7):
            messages.append({"role": "assistant", "content": f"old {i}"})
        # 500 in the window (all durable)
        for i in range(500):
            messages.append({"role": "user", "content": f"recent {i}"})

        # Apply the same logic as chat_persistence restore
        prefix_count = max(0, len(messages) - 500)
        slot._disk_older_count = prefix_count
        slot._disk_older_durable_count = sum(
            1
            for m in messages[:prefix_count]
            if m.get("role") not in _TRANSIENT_ROLES
        )

        assert slot._disk_older_count == 10
        assert slot._disk_older_durable_count == 7

    def test_restore_all_transient_prefix(self, tmp_path):
        """A prefix of entirely transient rows yields durable count 0."""
        state = _make_state(tmp_path)
        slot = _slot(state)
        messages = []
        for role in ["chunk", "done", "streaming", "queued", "permission"]:
            messages.append({"role": role, "content": ""})
        for i in range(500):
            messages.append({"role": "assistant", "content": f"msg {i}"})

        prefix_count = max(0, len(messages) - 500)
        slot._disk_older_count = prefix_count
        slot._disk_older_durable_count = sum(
            1
            for m in messages[:prefix_count]
            if m.get("role") not in _TRANSIENT_ROLES
        )

        assert slot._disk_older_count == 5
        assert slot._disk_older_durable_count == 0


# ---------------------------------------------------------------------------
# Test: session_control.read_messages uses durable count for cursors
# ---------------------------------------------------------------------------


@pytest.fixture
def _sc_enabled(monkeypatch):
    monkeypatch.setattr(sc, "session_control_enabled", lambda: True)


class TestReadMessagesDurableCursor:
    """read_messages uses _disk_older_durable_count for exact cursor positions."""

    @pytest.fixture(autouse=True)
    def setup(self, tmp_path, _sc_enabled):
        self.state = _make_state(tmp_path)

    def _make_target_with_prefix(self, durable_prefix, transient_prefix, window_msgs):
        """Create a target slot with the given prefix counts and window messages.

        Returns the slot so callers can inspect it; read_messages addresses it
        by name ("target") through the state, matching the authorize_target
        resolution path.
        """
        slot = _slot(self.state, "target")
        for role, content in window_msgs:
            slot.append(role, content, "msg msg-u" if role == "user" else "msg msg-a",
                        broadcast=False)
        slot._disk_older_count = durable_prefix + transient_prefix
        slot._disk_older_durable_count = durable_prefix
        return slot

    def test_next_since_emitted_on_trimmed_session(self):
        """next_since is present even when the session has trimmed rows."""
        self._make_target_with_prefix(
            durable_prefix=50, transient_prefix=10,
            window_msgs=[("assistant", f"msg {i}") for i in range(5)],
        )
        caller = _slot(self.state, "caller")
        result = sc.read_messages(
            self.state,
            caller_session_key=_key(caller),
            target="target",
            limit=100,
        )
        assert "next_since" in result
        assert result["next_since"] == 50 + 5  # durable_prefix + window durable count

    def test_since_read_works_on_trimmed_session(self):
        """A since-read no longer raises cursor_unavailable on trimmed sessions."""
        self._make_target_with_prefix(
            durable_prefix=50, transient_prefix=10,
            window_msgs=[("assistant", f"msg {i}") for i in range(5)],
        )
        caller = _slot(self.state, "caller")
        # This would have raised SessionControlError("cursor_unavailable") before
        result = sc.read_messages(
            self.state,
            caller_session_key=_key(caller),
            target="target",
            since=50,
            limit=100,
        )
        assert result["ok"] is True
        assert result["total"] == 55  # 50 + 5
        assert result["next_since"] == 55

    def test_since_at_zero_on_trimmed_session(self):
        """since=0 on a trimmed session starts from the window beginning."""
        self._make_target_with_prefix(
            durable_prefix=50, transient_prefix=10,
            window_msgs=[("user", f"msg {i}") for i in range(3)],
        )
        caller = _slot(self.state, "caller")
        result = sc.read_messages(
            self.state,
            caller_session_key=_key(caller),
            target="target",
            since=0,
            limit=100,
        )
        assert result["ok"] is True
        # since=0 < base=50, so it clamps to start of window
        assert len(result["messages"]) == 3
        assert result["messages"][0]["index"] == 50  # starts at durable base

    def test_since_past_end_raises(self):
        """A cursor past the end still raises for rewind/regenerate detection."""
        self._make_target_with_prefix(
            durable_prefix=10, transient_prefix=2,
            window_msgs=[("assistant", "only one")],
        )
        caller = _slot(self.state, "caller")
        with pytest.raises(sc.SessionControlError, match="shorter than your cursor"):
            sc.read_messages(
                self.state,
                caller_session_key=_key(caller),
                target="target",
                since=999,
                limit=100,
            )

    def test_transient_roles_constant_matches_persistence(self):
        """The trim constant in state.py matches chat_persistence._TRANSIENT_ROLES."""
        assert _TRANSIENT_ROLES_FOR_TRIM == _TRANSIENT_ROLES

    def test_total_uses_durable_count(self):
        """total in the response is base + durable window rows."""
        self._make_target_with_prefix(
            durable_prefix=100, transient_prefix=20,
            window_msgs=[
                ("assistant", "d1"),
                ("chunk", "transient"),  # filtered out of durable view
                ("user", "d2"),
            ],
        )
        caller = _slot(self.state, "caller")
        result = sc.read_messages(
            self.state,
            caller_session_key=_key(caller),
            target="target",
            limit=100,
        )
        # chunk is transient, so only 2 durable in window
        assert result["total"] == 100 + 2
