"""Tests for the shared channel-neutral ConversationState + disk-seeded generation.

Covers: the shared generation/awaiting counter, SessionMap.max_generation, and
the restart regression where /new must advance past a stale generation left on
disk instead of colliding with it and resuming the old session.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from kiro_crew.messaging.conversation import ConversationState
from kiro_crew.session_map import SessionMap


@pytest.fixture()
def session_map(tmp_path):
    with patch("kiro_crew.session_map.config_dir", return_value=tmp_path):
        yield SessionMap()


class TestConversationStateBasics:
    def test_default_starts_at_zero(self):
        c: ConversationState[int] = ConversationState()
        assert c.current_gen(7) == 0

    def test_bump_increments(self):
        c: ConversationState[int] = ConversationState()
        assert c.bump_gen(7) == 1
        assert c.bump_gen(7) == 2
        assert c.current_gen(7) == 2

    def test_keys_isolated(self):
        c: ConversationState[str] = ConversationState()
        c.bump_gen("a")
        assert c.current_gen("a") == 1
        assert c.current_gen("b") == 0

    def test_awaiting_flag(self):
        c: ConversationState[int] = ConversationState()
        assert c.is_awaiting(1) is False
        c.set_awaiting(1)
        assert c.is_awaiting(1) is True
        c.clear_awaiting(1)
        assert c.is_awaiting(1) is False

    def test_bump_clears_awaiting(self):
        c: ConversationState[int] = ConversationState()
        c.set_awaiting(1)
        c.bump_gen(1)
        assert c.is_awaiting(1) is False

    def test_first_message_never_rotates(self):
        c: ConversationState[int] = ConversationState()
        # last_active == 0 → nothing to roll over yet.
        assert c.maybe_rotate(1, now=1000.0, idle_minutes=1) is False
        assert c.current_gen(1) == 0

    def test_idle_rotate_bumps(self):
        c: ConversationState[int] = ConversationState()
        c.maybe_rotate(1, now=1000.0, idle_minutes=10)  # seed last_active
        assert c.maybe_rotate(1, now=1000.0 + 11 * 60, idle_minutes=10) is True
        assert c.current_gen(1) == 1


class TestSeeding:
    def test_seed_sets_initial_gen(self):
        c: ConversationState[str] = ConversationState(seed_fn=lambda k: 2)
        assert c.current_gen("x") == 2
        assert c.bump_gen("x") == 3

    def test_seed_negative_floors_to_zero(self):
        c: ConversationState[str] = ConversationState(seed_fn=lambda k: -1)
        assert c.current_gen("x") == 0

    def test_seed_called_once_per_key(self):
        calls: list[str] = []

        def seed(k: str) -> int:
            calls.append(k)
            return 1

        c: ConversationState[str] = ConversationState(seed_fn=seed)
        c.current_gen("x")
        c.bump_gen("x")
        c.current_gen("x")
        assert calls == ["x"]  # seeded exactly once


class TestMaxGeneration:
    def test_none_returns_minus_one(self, session_map):
        assert session_map.max_generation("telegram:kirocrew:direct:U") == -1

    def test_base_only_returns_zero(self, session_map):
        session_map.set("telegram:kirocrew:direct:U", "sid0", provider="claude_code")
        assert session_map.max_generation("telegram:kirocrew:direct:U") == 0

    def test_highest_generation(self, session_map):
        b = "telegram:kirocrew:direct:U"
        session_map.set(b, "sid0", provider="claude_code")
        session_map.set(f"{b}:gen1", "sid1", provider="claude_code")
        session_map.set(f"{b}:gen2", "sid2", provider="claude_code")
        assert session_map.max_generation(b) == 2

    def test_ignores_other_buckets(self, session_map):
        session_map.set("telegram:kirocrew:direct:U:gen5", "sid", provider="claude_code")
        # A different (prefix-lookalike) user must not leak in.
        assert session_map.max_generation("telegram:kirocrew:direct:U2") == -1
        assert session_map.max_generation("telegram:kirocrew:direct:U") == 5


class TestRestartRegression:
    def test_new_after_restart_advances_past_stale_gen(self, session_map):
        """The bug: after a gateway restart the in-memory counter reset to 0 and
        /new bumped 0→1, colliding with a stale :gen1 on disk and resuming it.
        Seeding from disk makes current resume the latest and /new go fresh."""
        b = "telegram:kirocrew:direct:U"
        session_map.set(b, "sid0", provider="claude_code")
        session_map.set(f"{b}:gen1", "sid1", provider="claude_code")
        session_map.set(f"{b}:gen2", "sid2", provider="claude_code")

        # Fresh state after restart, seeded from the persisted map.
        conv: ConversationState[str] = ConversationState(
            seed_fn=lambda k: session_map.max_generation(b)
        )
        assert conv.current_gen("U") == 2   # resume the latest, not stale gen0
        assert conv.bump_gen("U") == 3      # /new → fresh, no collision with gen1/gen2
