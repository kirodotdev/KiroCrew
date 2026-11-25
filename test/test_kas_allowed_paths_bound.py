"""What the KAS session-start notice says about a read or write ``allowedPaths`` list
longer than the 64 entries Kiro Crew applies.

Kiro Crew answers those lists at its own gate, so the bound is its own. Past it the
first 64 entries still apply, so the notice names each entry that does not, by its
position and text, and never says the whole key has no effect. The bare key is still
named whole for a list the session answers none of.
"""

from __future__ import annotations

from typing import Any

import pytest
from test_kas_allowed_commands import _no_profiles  # noqa: F401

from kiro_crew.acp import kas_agents
from kiro_crew.acp.kas_permissions import (
    _MAX_ALLOWED_PATHS,
    crew_answered_paths,
    crew_answered_read_paths,
    spec_write_allowlist,
)
from kiro_crew.dashboard.chat_turn.tool_approval import _spec_keys_notice

LIMIT = _MAX_ALLOWED_PATHS


def _entries(count: int) -> list[str]:
    """``/work/dir0/**`` to ``/work/dir<count - 1>/**``: entry *n* sits at position *n + 1*."""
    return [f"/work/dir{n}/**" for n in range(count)]


def _lost(tools_settings: dict[str, Any]) -> list[str]:
    spec = {"name": "a", "prompt": "p", "tools": ["*"], "toolsSettings": tools_settings}
    return kas_agents.spec_keys_without_carrier(spec, translates_tools_settings=True)


@pytest.mark.parametrize(
    ("tool", "answered"), [("write", crew_answered_paths), ("read", crew_answered_read_paths)]
)
def test_an_entry_past_the_bound_is_named_and_the_first_ones_apply(tool, answered) -> None:
    """65 usable entries: the first 64 are answered, and the notice names entry 65
    by position and text instead of calling the whole key ineffective."""
    settings = {tool: {"allowedPaths": _entries(LIMIT + 1)}}
    assert LIMIT == 64
    assert answered(settings) == _entries(LIMIT)
    key = f"toolsSettings.{tool}.allowedPaths"
    lost = _lost(settings)
    assert lost == [f"{key} past the limit: entry {LIMIT + 1}: /work/dir{LIMIT}/**"]
    notice = _spec_keys_notice("a", lost)
    assert f"sets more {key} entries than the {LIMIT} Kiro Crew applies" in notice
    assert f"Its first {LIMIT} readable entries apply" in notice
    assert f"These do not: entry {LIMIT + 1}: /work/dir{LIMIT}/**." in notice
    assert "no effect" not in notice and "does not receive" not in notice
    assert _lost({tool: {"allowedPaths": _entries(LIMIT)}}) == []


def test_a_deny_fallback_list_names_the_entry_its_allowlist_cuts() -> None:
    """With ``fallbackAction: "deny"`` the first 64 readable entries are the write
    allowlist the gate enforces, so the cut entry is named even when a
    ``deniedPaths`` withholds the list's grants and names it whole."""
    settings = {
        "write": {
            "allowedPaths": _entries(LIMIT + 1),
            "deniedPaths": ["/etc"],
            "fallbackAction": "deny",
        }
    }
    assert spec_write_allowlist(settings) == _entries(LIMIT)
    lost = _lost(settings)
    assert "toolsSettings.write.allowedPaths (read by Crew's gate)" in lost
    notice = _spec_keys_notice("a", lost)
    assert "Kiro Crew's own approval gate still enforces them" in notice
    assert f"These do not: entry {LIMIT + 1}: /work/dir{LIMIT}/**." in notice
    assert "no effect" not in notice


def test_an_unreadable_entry_and_the_cut_ones_are_each_named() -> None:
    """An entry Kiro Crew cannot read is named on its own beside the entries past
    the bound, so the key is never said to have no effect."""
    settings = {"write": {"allowedPaths": ["/work/[a]/**", *_entries(LIMIT + 1)]}}
    lost = _lost(settings)
    assert "toolsSettings.write.allowedPaths" not in lost
    notice = _spec_keys_notice("a", lost)
    assert "cannot read: toolsSettings.write.allowedPaths: /work/[a]/** (the [ character)" in notice
    assert f"These do not: entry {LIMIT + 2}: /work/dir{LIMIT}/**." in notice
    assert "no effect" not in notice and "other entries still apply" not in notice


def test_each_list_past_the_bound_is_named_in_its_own_sentence() -> None:
    """Both lists past the bound get one sentence each, naming their own list. When
    the read list is named whole as withheld, the write list's cut entry is still named."""
    both = {
        "read": {"allowedPaths": _entries(LIMIT + 1)},
        "write": {"allowedPaths": _entries(LIMIT + 1)},
    }
    notice = _spec_keys_notice("a", _lost(both))
    for tool in ("read", "write"):
        assert (
            f"sets more toolsSettings.{tool}.allowedPaths entries than the {LIMIT} Kiro Crew "
            f"applies. Its first {LIMIT} readable entries apply. These do not: entry {LIMIT + 1}: "
            f"/work/dir{LIMIT}/**."
        ) in notice
    both["read"]["deniedPaths"] = ["/etc"]
    notice = _spec_keys_notice("a", _lost(both))
    assert "sets more toolsSettings.write.allowedPaths entries" in notice
    assert f"These do not: entry {LIMIT + 1}: /work/dir{LIMIT}/**." in notice
    assert "sets more toolsSettings.read.allowedPaths entries" not in notice


def test_a_list_the_session_answers_none_of_is_not_said_to_apply_in_part() -> None:
    """A ``deniedPaths`` beside the list withholds every entry, so the notice names
    the key whole and not the first entries as applying."""
    settings = {"write": {"allowedPaths": _entries(LIMIT + 1), "deniedPaths": ["/etc"]}}
    lost = _lost(settings)
    assert "toolsSettings.write.allowedPaths" in lost
    notice = _spec_keys_notice("a", lost)
    assert "readable entries apply" not in notice and "past the limit" not in notice
