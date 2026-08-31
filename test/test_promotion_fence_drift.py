"""Neither promotion fence may gain a reserved form the other lacks.

The fence is implemented twice -- ``strip_recommended_marker`` in Python and
``splitRecommendation`` in TypeScript -- because the two runtimes dispatch a clicked
label independently. The shared vector fixture pins the forms that exist TODAY, which
is a statement about a list rather than about the invariant: a new slash command added
to one grammar and forgotten in the other lets a marker strip promote a model-authored
label into a real command on the surface that was missed, and every existing test still
passes.

So this module asserts the INVARIANT instead. Every reserved list in ``constants`` must
be either PAIRED with a frontend symbol whose entries match, or explicitly declared
backend-only with a reason. A list that is neither fails the completeness test, so
adding a seventh list cannot silently skip the pairing.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from kiro_crew.constants import (
    _INJECTED_PROVENANCE_PREFIXES,
    _RESERVED_DISPATCH_SIGILS,
    _RESERVED_PLAN_ACTIONS,
    _RESERVED_PROVENANCE_PREFIXES,
)
from kiro_crew.dashboard.chat_handlers import _RESERVED_STOP_WORDS

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "src" / "kiro_crew" / "constants.py"
STOP_WORD_HOME = ROOT / "src" / "kiro_crew" / "dashboard" / "chat_handlers.py"
FRONTEND = ROOT / "website" / "src" / "app-sdk" / "protocol" / "recommendation.ts"

# `set` and `alternation` compare entries exactly; `count` compares only how many, which
# is what an anchored regex array supports -- and it still fails when either side gains one.
PAIRS = {
    "_RESERVED_DISPATCH_SIGILS": ("RESERVED_DISPATCH_SIGILS", "set"),
    "_RESERVED_PROVENANCE_PREFIXES": ("RESERVED_PROVENANCE_RES", "count"),
    "_INJECTED_PROVENANCE_PREFIXES": ("INJECTED_PROVENANCE_RES", "count"),
    "_RESERVED_PLAN_ACTIONS": ("RESERVED_PLAN_ACTION_RE", "alternation"),
    "_RESERVED_STOP_WORDS": ("RESERVED_STOP_WORD_RE", "alternation"),
}

# Declared backend-only, with the reason the frontend has no twin. A row here is a
# claim that no dashboard dispatch path reads the form, and it is reviewed as such.
BACKEND_ONLY: dict[str, str] = {}

# Declared frontend-only, with the reason the backend has no twin. Same bar as
# BACKEND_ONLY: a claim that no channel dispatch path reads the form.
FRONTEND_ONLY: dict[str, str] = {}

PY_VALUES = {
    "_RESERVED_DISPATCH_SIGILS": _RESERVED_DISPATCH_SIGILS,
    "_RESERVED_PROVENANCE_PREFIXES": _RESERVED_PROVENANCE_PREFIXES,
    "_INJECTED_PROVENANCE_PREFIXES": _INJECTED_PROVENANCE_PREFIXES,
    "_RESERVED_PLAN_ACTIONS": _RESERVED_PLAN_ACTIONS,
    "_RESERVED_STOP_WORDS": _RESERVED_STOP_WORDS,
}


def _declared_backend_lists() -> set[str]:
    """Every reserved/injected list the backend assigns at module level.

    Two sources, not one: a list with a single production consumer lives beside that
    consumer, so scanning only `constants` would let it drift from its frontend twin
    unnoticed -- which is the exact hole this gate exists to close.
    """
    found: set[str] = set()
    for path in (BACKEND, STOP_WORD_HOME):
        text = path.read_text(encoding="utf-8")
        found |= set(re.findall(r"^(_(?:RESERVED|INJECTED)_[A-Z0-9_]+)\s*=", text, re.MULTILINE))
    return found


def _frontend_source() -> str:
    return FRONTEND.read_text(encoding="utf-8")


def _declared_frontend_symbols() -> set[str]:
    """Every reserved/injected form the frontend fence declares at module level."""
    return set(
        re.findall(
            r"^const ((?:RESERVED|INJECTED)_[A-Z0-9_]+)\s*=",
            _frontend_source(),
            re.MULTILINE,
        )
    )


def _frontend_string_array(symbol: str) -> set[str]:
    src = _frontend_source()
    m = re.search(rf"const {symbol} = \[(.*?)\]", src, re.DOTALL)
    if not m:
        return set()
    return set(re.findall(r"'([^']*)'", m.group(1)))


def _frontend_regex_array_count(symbol: str) -> int:
    src = _frontend_source()
    m = re.search(rf"const {symbol} = \[(.*?)^\]", src, re.DOTALL | re.MULTILINE)
    if not m:
        return -1
    return len(re.findall(r"^\s*/\^", m.group(1), re.MULTILINE))


def _frontend_alternation(symbol: str) -> set[str]:
    src = _frontend_source()
    m = re.search(rf"const {symbol} = /\^\(\?:([^)]*)\)", src)
    if not m:
        return set()
    return set(m.group(1).split("|"))


class TestEveryReservedListIsAccountedFor:
    def test_the_scan_finds_the_backend_lists_at_all(self):
        # Vacuity guard: an empty scan would satisfy the completeness test below.
        found = _declared_backend_lists()
        assert len(found) >= 5, f"only found {sorted(found)} -- re-anchor this scan"

    def test_the_frontend_source_is_readable(self):
        assert "splitRecommendation" in _frontend_source(), "re-anchor FRONTEND"

    def test_no_backend_list_is_unclassified(self):
        """A NEW list must be paired or declared backend-only -- not silently skipped."""
        classified = set(PAIRS) | set(BACKEND_ONLY)
        unclassified = _declared_backend_lists() - classified
        assert not unclassified, (
            f"{sorted(unclassified)} is a reserved list with no frontend pairing and no "
            "backend-only declaration, so the two promotion fences can drift on it -- add "
            "it to PAIRS with its frontend symbol, or to BACKEND_ONLY with the reason no "
            "dashboard dispatch path reads it"
        )

    def test_the_pairing_table_names_only_real_lists(self):
        declared = _declared_backend_lists()
        stale = (set(PAIRS) | set(BACKEND_ONLY)) - declared
        assert not stale, f"{sorted(stale)} is in the table but no longer exists in constants"

    def test_the_scan_finds_the_frontend_symbols_at_all(self):
        found = _declared_frontend_symbols()
        assert len(found) >= 5, f"only found {sorted(found)} -- re-anchor this scan"

    def test_no_frontend_symbol_is_unclassified(self):
        """Drift is symmetric: a form added only to the TS fence must be declared too."""
        paired = {symbol for symbol, _ in PAIRS.values()}
        unclassified = _declared_frontend_symbols() - paired - set(FRONTEND_ONLY)
        assert not unclassified, (
            f"{sorted(unclassified)} is a frontend reserved form with no backend pairing and "
            "no frontend-only declaration, so the two fences can drift on it -- pair it in "
            "PAIRS, or declare it in FRONTEND_ONLY with the reason no channel reads it"
        )

    def test_the_frontend_only_table_names_only_real_symbols(self):
        stale = set(FRONTEND_ONLY) - _declared_frontend_symbols()
        assert not stale, f"{sorted(stale)} is in FRONTEND_ONLY but no longer exists in the guard"


class TestThePairedListsHaveNotDrifted:
    @pytest.mark.parametrize("py_name", sorted(n for n, (_, k) in PAIRS.items() if k == "set"))
    def test_entries_match_exactly(self, py_name):
        symbol, _ = PAIRS[py_name]
        frontend = _frontend_string_array(symbol)
        assert frontend, f"extracted nothing from {symbol} -- re-anchor the extractor"
        assert frontend == set(PY_VALUES[py_name]), (
            f"{py_name} and {symbol} disagree: only in Python "
            f"{sorted(set(PY_VALUES[py_name]) - frontend)}, only in TS "
            f"{sorted(frontend - set(PY_VALUES[py_name]))}"
        )

    @pytest.mark.parametrize("py_name", sorted(n for n, (_, k) in PAIRS.items() if k == "count"))
    def test_entry_counts_match(self, py_name):
        symbol, _ = PAIRS[py_name]
        frontend = _frontend_regex_array_count(symbol)
        assert frontend > 0, f"extracted no patterns from {symbol} -- re-anchor the extractor"
        assert frontend == len(PY_VALUES[py_name]), (
            f"{py_name} has {len(PY_VALUES[py_name])} entries but {symbol} has {frontend}; "
            "one runtime gained a reserved form the other cannot refuse"
        )

    @pytest.mark.parametrize(
        "py_name", sorted(n for n, (_, k) in PAIRS.items() if k == "alternation")
    )
    def test_alternation_branches_match(self, py_name):
        symbol, _ = PAIRS[py_name]
        frontend = _frontend_alternation(symbol)
        assert frontend, f"extracted no branches from {symbol} -- re-anchor the extractor"
        assert frontend == set(PY_VALUES[py_name]), (
            f"{py_name} and {symbol} disagree: only in Python "
            f"{sorted(set(PY_VALUES[py_name]) - frontend)}, only in TS "
            f"{sorted(frontend - set(PY_VALUES[py_name]))}"
        )

    def test_every_backend_only_row_carries_a_reason(self):
        thin = {n for n, why in BACKEND_ONLY.items() if len(why) < 40}
        assert not thin, f"{sorted(thin)} is declared backend-only without a real reason"

    def test_every_frontend_only_row_carries_a_reason(self):
        thin = {n for n, why in FRONTEND_ONLY.items() if len(why) < 40}
        assert not thin, f"{sorted(thin)} is declared frontend-only without a real reason"
