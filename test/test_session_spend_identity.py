"""Credits/Turns must survive the slot-vs-session identity split.

Per-turn usage is filed under ``slot.key``; a session is addressed by
``effective_session_key``. For a slot bound to a channel or cron conversation
those are unrelated strings, so a join on the session key finds nothing and the
Sessions table reports credits as unknown for a session that really did spend.
"""

from __future__ import annotations

import json as _json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from kiro_crew.dashboard import session_memory as sm


def _shard(tmp: Path, slot: str, credits: float) -> None:
    now = datetime.now(timezone.utc)
    shard = tmp / now.strftime("%Y-%m-%d.jsonl")
    row = {"_type": "tokens", "ts": now.isoformat(), "slot": slot, "credits": credits}
    with shard.open("a") as fh:
        fh.write(_json.dumps(row) + "\n")


def test_spend_key_for_slot_is_the_single_owner_of_the_prefix_rule() -> None:
    """Bare dashboard slot keys gain the prefix; every other shape passes through."""
    from kiro_crew.dashboard.handlers.usage import spend_key_for_slot

    assert spend_key_for_slot("chat-69-1785905004") == "dashboard:chat-69-1785905004"
    # Already-qualified and non-dashboard keys must not be double-prefixed.
    assert spend_key_for_slot("dashboard:chat-69-1785905004") == (
        "dashboard:chat-69-1785905004"
    )
    assert spend_key_for_slot("slack:C123") == "slack:C123"
    assert spend_key_for_slot("cron:nightly") == "cron:nightly"


def test_direct_join_still_wins_for_an_ordinary_dashboard_session() -> None:
    """The common path must not depend on the alias map at all."""
    spend = {"dashboard:chat-1-9": {"credits": 4.0, "turns": 2.0}}
    row = sm._spend_for_session(spend, "dashboard:chat-1-9", None)
    assert row is not None
    assert row["credits"] == pytest.approx(4.0)


def test_linked_session_resolves_spend_through_the_slot_alias() -> None:
    """A cron/channel-bound slot: session key and spend key are unrelated.

    Without the alias the direct lookup misses and credits render as unknown even
    though the spend exists under the dashboard slot that produced it.
    """
    spend = {"dashboard:chat-7-42": {"credits": 9.5, "turns": 3.0}}
    aliases = {"cron:nightly-digest": "chat-7-42"}

    # The bug: joining on the session key alone finds nothing.
    assert sm._spend_for_session(spend, "cron:nightly-digest", None) is None

    row = sm._spend_for_session(spend, "cron:nightly-digest", aliases)
    assert row is not None, "linked session must resolve its spend via the slot alias"
    assert row["credits"] == pytest.approx(9.5)
    assert row["turns"] == pytest.approx(3.0)


def test_unknown_session_stays_unknown() -> None:
    """No shard and no alias means None -- which the payload documents as null."""
    assert sm._spend_for_session({}, "cron:never-ran", {"cron:never-ran": "chat-9-1"}) is None
    assert sm._spend_for_session({}, "dashboard:chat-9-1", None) is None
    # A non-string key (defensive: runtime_pids returns object values).
    assert sm._spend_for_session({"x": {"credits": 1.0, "turns": 1.0}}, None, None) is None


def test_a_mock_alias_map_cannot_reach_the_regex() -> None:
    """A MagicMock alias map must be refused, not fed to the shard-key regex.

    Much of the suite builds DashboardState as a MagicMock, so
    ``state.spend_slot_by_session()`` is a mock and its ``.get()`` returns another
    mock -- which is TRUTHY. A bare ``if not slot_key`` admitted it and
    ``spend_key_for_slot`` raised ``TypeError: expected string or bytes-like
    object, got 'MagicMock'`` across every Backend Tests shard 2 on all three
    platforms. Type-check, do not truth-check, values crossing in from a caller.
    """
    from unittest.mock import MagicMock

    mock_aliases = MagicMock()
    spend = {"dashboard:chat-1-9": {"credits": 4.0, "turns": 2.0}}

    # Must return None rather than raising, and must not consult the mock's value.
    assert sm._spend_for_session(spend, "cron:whatever", mock_aliases) is None
    # A mock standing in for the whole spend dict is equally refused.
    assert sm._spend_for_session(MagicMock(), "cron:whatever", {"cron:whatever": "chat-1-9"}) is None
    # An alias whose value is a mock (not a str) is refused too.
    assert sm._spend_for_session(spend, "cron:whatever", {"cron:whatever": MagicMock()}) is None


def test_dashboard_state_maps_a_linked_slot_session_to_its_slot_key(
    tmp_path: object,
) -> None:
    """Built against the REAL DashboardState, not a stand-in.

    DashboardState exposes only ``get_slot`` publicly, so a hand-rolled double
    with a ``slots`` attribute would assert against a shape the class does not
    have. This drives the actual registry via the project's own helper.
    """
    from chat_test_helpers import _make_state

    state = _make_state(tmp_path)

    plain = state.get_or_create_slot(name="chat-1-100")
    state.get_or_create_slot(
        name="chat-2-200", linked_session_key="cron:nightly-digest",
    )

    aliases = state.spend_slot_by_session()

    # An ordinary slot is addressed by its dashboard session key.
    assert aliases.get("dashboard:chat-1-100") == plain.key
    # A linked slot is addressed by the conversation it runs on, and its spend
    # still lives under the dashboard slot key.
    assert aliases.get("cron:nightly-digest") == "chat-2-200"
    # And the dashboard form must NOT also be present for the linked slot, or a
    # consumer could join it twice under two identities.
    assert "dashboard:chat-2-200" not in aliases


def test_end_to_end_linked_slot_credits_reach_the_payload(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object,
) -> None:
    """The whole chain: shard keyed by slot -> alias -> credits on the session row."""
    from chat_test_helpers import _make_state

    from kiro_crew.dashboard.handlers import usage

    tmp = Path(str(tmp_path))
    shards = tmp / "shards"
    shards.mkdir()
    _shard(shards, "chat-7-42", 9.5)

    monkeypatch.setattr(usage, "_TOKEN_USAGE_DIR", shards)
    monkeypatch.setattr(usage, "is_session_slot", lambda s: True)
    monkeypatch.setattr(usage, "_SLOT_SPEND_CACHE", {})
    monkeypatch.setattr(usage, "_SLOT_SPEND_CACHE_SIG", ())
    monkeypatch.setattr(usage, "_SLOT_SPEND_CACHE_AT", 0.0)

    state = _make_state(tmp_path)
    state.get_or_create_slot(
        name="chat-7-42", linked_session_key="cron:nightly-digest",
    )

    spend = usage.slot_spend()
    aliases = state.spend_slot_by_session()

    row = sm._spend_for_session(spend, "cron:nightly-digest", aliases)
    assert row is not None, "spend recorded under the slot key must reach the session row"
    assert row["credits"] == pytest.approx(9.5)
