"""Coverage for ``kiro_crew.emoji_list`` — the standard Slack shortcode table.

The list is merged with custom workspace emojis fetched from Slack at runtime
and fed to the composer's autocomplete, so the invariants that matter are:
bare shortcodes (no surrounding colons, no whitespace), no duplicates, and
non-empty. A colon-wrapped or duplicated entry silently corrupts autocomplete.
"""

from __future__ import annotations

from kiro_crew.emoji_list import STANDARD_EMOJIS


def test_table_is_populated_and_all_strings() -> None:
    assert len(STANDARD_EMOJIS) > 100
    assert all(isinstance(name, str) and name for name in STANDARD_EMOJIS)


def test_shortcodes_are_bare_names() -> None:
    """Entries are merged with Slack's custom-emoji names, which are also bare —
    a stray ``:`` or space here would never match a typed prefix."""
    offenders = [n for n in STANDARD_EMOJIS if ":" in n or n.strip() != n or " " in n]
    assert offenders == []


def test_no_duplicate_shortcodes() -> None:
    seen: set[str] = set()
    dupes: list[str] = []
    for name in STANDARD_EMOJIS:
        if name in seen:
            dupes.append(name)
        seen.add(name)
    assert dupes == [], f"duplicate shortcodes would appear twice in autocomplete: {dupes}"
