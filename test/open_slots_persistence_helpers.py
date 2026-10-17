"""The session seeding helper shared by the test_open_slots_persistence*.py modules."""

from __future__ import annotations

from kiro_crew.dashboard.chat_utils import _history_key_for


def _seed_session(state, slot_key: str, *, closed: bool = False) -> None:
    """Write a minimal session metadata + one user message so rehydrate succeeds."""
    history_key = _history_key_for(slot_key)
    log = state.conversation_log
    assert log is not None
    log.append(history_key, "user", "hello")
    if closed:
        # Use the canonical update_metadata helper rather than manually
        # rewriting the JSONL — depends only on the public API and is
        # resilient to format changes.
        log.update_metadata(history_key, {"closed": True})
