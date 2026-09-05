"""The dispatch-sigil deny list exists on both surfaces and must not diverge.

The backend decides what a dispatched message means; the frontend refuses to strip a
recommendation marker off a label that would become one. A sigil added to one side and
not the other reopens the promotion the guard exists to prevent, silently.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from kiro_crew.constants import _RESERVED_DISPATCH_SIGILS, strip_recommended_marker

_ROOT = Path(__file__).resolve().parents[1]
_FRONTEND = _ROOT / "website" / "src" / "app-sdk" / "protocol" / "recommendation.ts"
_FRONTEND_TEST = _ROOT / "website" / "src" / "test" / "recommendation.test.ts"
_VECTOR_FILE = _ROOT / "test" / "fixtures" / "recommended_marker_grammar.json"
_VECTORS = json.loads(_VECTOR_FILE.read_text(encoding="utf-8"))["vectors"]
_DECL_RE = re.compile(
    r"const RESERVED_DISPATCH_SIGILS = \[(?P<body>[^\]]*)\] as const",
)


def _frontend_sigils() -> tuple[str, ...]:
    source = _FRONTEND.read_text(encoding="utf-8")
    match = _DECL_RE.search(source)
    assert match, "frontend sigil list not found -- was it renamed or reshaped?"
    return tuple(re.findall(r"'((?:[^'\\]|\\.)*)'", match.group("body")))


class TestDispatchSigilListsAgree:
    def test_the_frontend_declares_a_list_this_test_can_read(self):
        # Guards the guard: a silently unreadable list would make every comparison vacuous.
        assert _frontend_sigils(), "extracted an EMPTY frontend list"

    def test_neither_surface_carries_a_sigil_the_other_lacks(self):
        assert _frontend_sigils() == tuple(_RESERVED_DISPATCH_SIGILS)

    def test_the_backend_list_is_not_empty(self):
        assert _RESERVED_DISPATCH_SIGILS


class TestTheMarkerGrammarMatchesTheSharedVectors:
    """The list agreeing is not the grammar agreeing.

    Edges-only matching, case-insensitivity and the marker-only no-op are implemented
    twice and were pinned on neither side against the other. These vectors are the
    single source both surfaces answer to; the frontend half runs the same file from
    ``splitRecommendation``'s own suite.
    """

    def test_the_vector_file_is_readable_and_covers_both_outcomes(self):
        # Guards the guard: an empty or single-sided table would assert nothing.
        assert len(_VECTORS) >= 10
        assert any(v["marker"] for v in _VECTORS), "no marked vector"
        assert any(not v["marker"] for v in _VECTORS), "no unmarked vector"

    @pytest.mark.parametrize("vector", _VECTORS, ids=lambda v: v["why"])
    def test_the_backend_answers_each_vector(self, vector):
        assert strip_recommended_marker(vector["label"]) == vector["expected"]

    @pytest.mark.parametrize("vector", _VECTORS, ids=lambda v: v["why"])
    def test_an_unmarked_vector_is_a_true_no_op(self, vector):
        # Pins the shape the frontend relies on: no marker means the ORIGINAL text back,
        # so `expected` can never quietly encode a partial strip.
        if not vector["marker"]:
            assert vector["expected"] == vector["label"]


class TestTheFrontendRunsTheSameVectors:
    def test_the_frontend_suite_reads_the_shared_file(self):
        """Neither side may drop the shared table and still look green.

        A vector file asserted by only one implementation is not parity, and the
        omission is invisible from this side.
        """
        source = _FRONTEND_TEST.read_text(encoding="utf-8")
        assert _VECTOR_FILE.name in source, "frontend suite no longer reads the vectors"
        assert "splitRecommendation" in source
