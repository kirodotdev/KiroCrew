"""The injected-origin list is implemented twice, so nothing but this holds it in step.

The backend tuple and the frontend pattern list guard the same forgery on the two dispatch
paths: a turn opening with one of these is read as automation rather than as something the
user said, so a label stripping into one would forge that origin. A prefix added to one
runtime and not the other reopens the forgery on the runtime that missed it.
"""

import re
from pathlib import Path

import pytest

from kiro_crew.constants import _INJECTED_PROVENANCE_PREFIXES

FRONTEND = (
    Path(__file__).resolve().parents[1]
    / "website"
    / "src"
    / "app-sdk"
    / "protocol"
    / "recommendation.ts"
)
_BLOCK_RE = re.compile(r"INJECTED_PROVENANCE_RES = \[(.*?)\] as const", re.DOTALL)


def _frontend_block() -> str:
    m = _BLOCK_RE.search(FRONTEND.read_text(encoding="utf-8"))
    assert m, "the frontend injected-origin list is missing -- the runtimes have drifted"
    return m.group(1)


def _declared() -> set[str]:
    # Regex literals, not strings: the i18n gate forbids a new untranslated literal
    # inside an ALL-CAPS constant, so each origin ships as an anchored pattern.
    literals = re.findall(r"/\^(.+?)/i,", _frontend_block())
    return {lit.replace("\\", "") for lit in literals}


class TestTheRuntimesDeclareTheSameOrigins:
    def test_the_scan_finds_patterns_at_all(self):
        # Vacuity guard: an empty scan would satisfy the equality below.
        assert _declared(), "no anchored patterns found -- re-anchor this scan"

    def test_the_frontend_declares_the_same_origins(self):
        assert _declared() == set(_INJECTED_PROVENANCE_PREFIXES), sorted(
            _declared() ^ set(_INJECTED_PROVENANCE_PREFIXES)
        )

    @pytest.mark.parametrize("pattern", re.findall(r"(/[^,\n]+/i),", _frontend_block()))
    def test_every_frontend_pattern_is_anchored_and_case_insensitive(self, pattern):
        # Unanchored would match mid-label; case-sensitive would miss the forgery.
        assert pattern.startswith("/^")
        assert pattern.endswith("/i")
