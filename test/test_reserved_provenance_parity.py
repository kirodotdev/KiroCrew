"""Every reserved provenance opener a dispatcher byte-matches must be one the strip refuses.

The bracket guard refuses only the declared provenance openers, not every leading ``[``:
a legitimate label such as ``(recommended) [Draft] Reword it`` strips normally, while
``[SYSTEM]`` and its siblings decline. Mirroring the actual openers is what keeps a raw
marker out of both the chip and the dispatched string, and this test keeps the mirror
faithful in both directions: a new opener in the dashboard fails here, and a
reserved entry no dispatcher matches fails too.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from kiro_crew.constants import _RESERVED_PROVENANCE_PREFIXES, strip_recommended_marker

ROOT = Path(__file__).resolve().parents[1]
# Both modules that DEFINE a bracket opener. Scanning only the dashboard missed the two
# subagent markers, which live in constants itself and are matched in chat_runner.
OPENER_SOURCES = (
    ROOT / "src" / "kiro_crew" / "dashboard" / "state.py",
    ROOT / "src" / "kiro_crew" / "constants.py",
)
FRONTEND = ROOT / "website" / "src" / "app-sdk" / "protocol" / "recommendation.ts"


def _dashboard_openers() -> set[str]:
    """Bracket-opening string constants the runtime compares inbound text against."""
    found = set()
    for path in OPENER_SOURCES:
        text = path.read_text(encoding="utf-8")
        for m in re.finditer(
            r"^[A-Z][A-Z0-9_]*(?:_PREFIX|_PREFIXES)?\s*=\s*\"(\[[^\"]+)\"", text, re.MULTILINE
        ):
            found.add(m.group(1))
    return found


class TestTheProvenanceMirrorIsFaithful:
    def test_the_scan_finds_openers_at_all(self):
        # Vacuity guard: an empty scan would satisfy the subset assertion below.
        assert _dashboard_openers(), "no bracket openers found -- re-anchor this scan"

    def test_every_dashboard_opener_is_reserved(self):
        missing = {
            o for o in _dashboard_openers() if not o.startswith(_RESERVED_PROVENANCE_PREFIXES)
        }
        assert not missing, (
            f"the dashboard byte-matches {sorted(missing)} but the marker strip would promote a "
            "label into it -- add it to _RESERVED_PROVENANCE_PREFIXES"
        )

    def test_a_bracket_that_claims_no_origin_still_strips(self):
        assert strip_recommended_marker("(recommended) [Draft] Reword it") == "[Draft] Reword it"
        assert strip_recommended_marker("(recommended) [WIP] ship") == "[WIP] ship"

    @pytest.mark.parametrize("prefix", sorted(_RESERVED_PROVENANCE_PREFIXES))
    def test_a_marked_provenance_opener_is_left_verbatim(self, prefix):
        label = f"(recommended) {prefix} tail"
        assert strip_recommended_marker(label) == label

    def test_the_frontend_declares_the_same_openers(self):
        text = FRONTEND.read_text(encoding="utf-8")
        m = re.search(r"RESERVED_PROVENANCE_RES = \[(.*?)\] as const", text, re.DOTALL)
        assert m, "the frontend provenance list is missing -- the runtimes have drifted"
        # Regex literals, not strings: the i18n gate forbids a new untranslated literal
        # inside an ALL-CAPS constant, so each opener ships as an anchored pattern.
        literals = re.findall(r"/\^(.+?)/,", m.group(1))
        declared = {lit.replace("\\", "") for lit in literals}
        assert declared == set(_RESERVED_PROVENANCE_PREFIXES), sorted(
            declared ^ set(_RESERVED_PROVENANCE_PREFIXES)
        )

    def test_every_frontend_opener_is_anchored(self):
        # An unanchored pattern would match mid-label and guard far more than an opener.
        text = FRONTEND.read_text(encoding="utf-8")
        m = re.search(r"RESERVED_PROVENANCE_RES = \[(.*?)\] as const", text, re.DOTALL)
        assert m
        pats = re.findall(r"(/[^,\n]+/),", m.group(1))
        assert pats, "no patterns found -- the extraction is vacuous"
        unanchored = [p for p in pats if not p.startswith("/^")]
        assert unanchored == [], unanchored

    def test_the_bracket_is_no_longer_a_blanket_sigil(self):
        from kiro_crew.constants import _RESERVED_DISPATCH_SIGILS

        assert "[" not in _RESERVED_DISPATCH_SIGILS, "a blanket bracket sigil is back"
