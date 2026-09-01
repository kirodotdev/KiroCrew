"""Cross-language drift guard for the chat transcript's recovery rows.

Every synthetic continuation the runner injects is rendered by
``website/src/pages/chat/RecoveryCard.tsx``, which classifies a row by matching
the bracketed marker line at the top of the injected prompt. The two sides are
in different languages with no shared build step, so a new marker added on the
Python side is invisible to the card and its machine-facing prose falls through
to a full-width chat bubble -- the regression this module exists to catch.

The assertions are deliberately source-level: they compare the constants in
``state.py`` against the literal prefix table in the TSX rather than exercising
a render, because the failure mode is a MISSING table entry, which no render of
the existing kinds can reveal.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_STATE = _ROOT / "src/kiro_crew/dashboard/state.py"
_CARD = _ROOT / "website/src/pages/chat/RecoveryCard.tsx"
_EN = _ROOT / "website/src/i18n/locales/en.json"
_SECURITY = _ROOT / "src/kiro_crew/security.py"
_DENY_UTIL = _ROOT / "website/src/utils/denyReason.ts"

#: ``NAME = "[Something — automatic recovery]"`` at module level in state.py.
_PREFIX_DECL = re.compile(
    r"^(?P<name>[A-Z][A-Z0-9_]*_RECOVERY_PREFIX)\s*=\s*\"(?P<value>\[[^\"]+\])\"",
    re.MULTILINE,
)
#: A ``['kind', '[Marker]'],`` row of the card's PREFIXES table.
_CARD_ROW = re.compile(r"\[\s*'(?P<kind>[a-z_]+)'\s*,\s*'(?P<value>\[[^']+\])'\s*\]")


#: TS/JS comments -- block (incl. the ``/** */`` doc form) and line.
_TS_COMMENT = re.compile(r"/\*.*?\*/|//[^\n]*", re.DOTALL)


def _code_only(src: str) -> str:
    """*src* with comments removed, so a guard counts declarations not prose."""
    return _TS_COMMENT.sub("", src)


def _state_prefixes() -> dict[str, str]:
    found = {
        m.group("name"): m.group("value")
        for m in _PREFIX_DECL.finditer(_STATE.read_text(encoding="utf-8"))
    }
    assert found, "no *_RECOVERY_PREFIX constants found in state.py -- regex drift"
    return found


def _card_prefixes() -> dict[str, str]:
    src = _CARD.read_text(encoding="utf-8")
    table = src.split("const PREFIXES", 1)
    assert len(table) == 2, "RecoveryCard.tsx no longer declares a PREFIXES table"
    body = table[1].split("]\n", 1)[0]
    found = {m.group("kind"): m.group("value") for m in _CARD_ROW.finditer(body)}
    assert found, "could not parse the PREFIXES table -- regex drift"
    return found


def test_every_recovery_prefix_is_rendered_as_a_card() -> None:
    """A marker with no card entry renders as raw machine prose in the chat."""
    missing = {
        name: value
        for name, value in _state_prefixes().items()
        if value not in _card_prefixes().values()
    }
    assert not missing, (
        "these recovery markers have no RecoveryCard.tsx entry, so the injected "
        f"prompt would render as a full-width bubble: {missing}"
    )


def test_card_prefixes_all_exist_in_python() -> None:
    """The reverse direction: a card entry matching nothing is dead code."""
    values = set(_state_prefixes().values())
    stale = {kind: value for kind, value in _card_prefixes().items() if value not in values}
    assert not stale, f"RecoveryCard.tsx matches markers no longer emitted: {stale}"


def test_synthetic_recovery_messages_carry_a_known_marker() -> None:
    """Every synthetic prompt the runner injects must open with a card-known marker.

    They are built from the prefixes rather than hardcoding the marker, so this
    guards the composition (a lost f-string prefix) as well as the marker set.
    """
    from kiro_crew.dashboard.chat_utils import _SYNTHETIC_RECOVERY_MSGS

    known = set(_card_prefixes().values())
    for msg in _SYNTHETIC_RECOVERY_MSGS:
        marker = msg.split("\n", 1)[0]
        assert (
            marker in known
        ), f"synthetic recovery prompt opens with {marker!r}, which no card matches"
        assert msg.split("\n", 1)[1].strip(), "marker line is not followed by a body"


@pytest.mark.parametrize(
    "key",
    [
        "turn_interrupted",
        "backend_error_continuing",
        "no_response_returned",
        "empty_output_continuing",
        # The user-pressed Continue card. Its copy must NOT read as an automatic
        # recovery, which is why it has its own pair rather than reusing the
        # posttoken labels above.
        "continued_by_you",
        "resuming_the_interrupted_turn",
        # The Stop-hook continuation card. Its own pair because the turn
        # completed: reusing an interruption label would report an event that
        # did not happen.
        "continued_by_a_hook",
        "hook_requested_continuing",
        # The nudge-cap halt card. Surfaced when a Stop-hook run hits
        # agent.max_stop_hook_nudges; its own pair because no continuation
        # happened — the loop was force-stopped.
        "hook_loop_halted",
        "nudge_cap_reached",
        # The post-compaction continuation card. Its own pair because nothing
        # errored: reusing the backend-error labels would send the reader looking
        # for a fault, when the earlier messages were deliberately summarized.
        "context_compacted",
        "summarized_mid_turn_continuing",
    ],
)
def test_new_card_labels_are_in_the_english_catalog(key: str) -> None:
    """The card resolves its labels through i18n; a missing key renders the key."""
    catalog = json.loads(_EN.read_text(encoding="utf-8"))
    assert key in catalog["pages"]["chat"]["recoveryCard"]


def test_deny_marker_has_exactly_one_frontend_copy_and_it_matches_python() -> None:
    """``DENY_REASON_PREFIX`` is a wire value; the frontend must carry it verbatim, once.

    Three readers key on this string -- ``RecoveryCard`` extracts the pattern with an
    end-anchored per-line match, ``denyReason.ts`` slices the reason out of the blocked
    row, and the test suite's ``_denied_by`` partitions on it -- so a change on the
    Python side that the frontend does not follow silently stops every deny reason from
    being found, with nothing going red.

    The PREFIXES-table guard above does NOT cover this literal (it parses only that
    table), which is why it needs its own check. Asserting a single copy is the other
    half: a second declaration is free to drift from the first while the first keeps
    matching, so the failure would surface as a card that still renders beside a
    suppression that silently stopped working.
    """
    py = _SECURITY.read_text(encoding="utf-8")
    decl = re.search(r'^DENY_REASON_PREFIX = "(?P<value>[^"]+)"', py, re.MULTILINE)
    assert decl, "security.py no longer declares DENY_REASON_PREFIX -- regex drift"
    # The trailing space is a joiner, not part of the marker the frontend matches.
    marker = decl.group("value").rstrip()

    util = _DENY_UTIL.read_text(encoding="utf-8")
    assert marker in util, (
        f"denyReason.ts does not carry the Python marker {marker!r}; every deny reason "
        "would fall back to the localized placeholder and name no rule"
    )

    # Counted across BOTH frontend readers, and over CODE only: the prose in a doc
    # comment explaining the wire format is not a second declaration, and forbidding
    # it would push the explanation out of the file that needs it most. RecoveryCard
    # must build its matcher from the exported source rather than re-declaring.
    card = _CARD.read_text(encoding="utf-8")
    copies = _code_only(util).count(marker) + _code_only(card).count(marker)
    assert copies == 1, (
        f"the wire marker {marker!r} is declared {copies} times in code across "
        "denyReason.ts and RecoveryCard.tsx; it must be declared exactly once and "
        "shared, or one copy can drift while the other keeps matching"
    )
