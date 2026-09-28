"""Cross-language pin: the stop-card turn boundary mirrors the TS grouping.

``_orphan_in_current_turn`` (chat_handlers.py) re-implements a subset of the
frontend's turn grouping: ``_TURN_OPENER_ROLES`` mirrors ``TURN_OPENER_ROLES``
and its synthesis-injection check mirrors ``isSynthesisInjection`` — both in
``website/src/pages/chat/groupDisplayItems.ts``. Comment-only mirroring of
exactly this list has drifted before (the TS file's own doc comment records a
61-display-row gap from a hand-maintained copy), and a drift here re-arms a
stop card across what the client renders as a turn boundary — the wrong-turn
chip bug the same-turn scope exists to prevent — with no test going red on
either side. These tests are the automated pin the Design lane asked for: they
parse the TS source and assert equality, so EITHER side moving alone goes red.
"""

from __future__ import annotations

import re
from pathlib import Path

from kiro_crew.dashboard.chat_handlers import _TURN_OPENER_ROLES

_TS_GROUPING = (
    Path(__file__).resolve().parents[1]
    / "website"
    / "src"
    / "pages"
    / "chat"
    / "groupDisplayItems.ts"
)


class TestTurnBoundaryMirrorsFrontendGrouping:
    def test_turn_opener_roles_match_the_ts_set(self) -> None:
        source = _TS_GROUPING.read_text(encoding="utf-8")
        match = re.search(
            r"export const TURN_OPENER_ROLES = new Set\(\[(?P<body>[^\]]*)\]\)", source
        )
        assert match, "TURN_OPENER_ROLES set literal not found — update this parser with it"
        ts_roles = set(re.findall(r"'([^']+)'", match.group("body")))
        assert ts_roles, "parsed an empty TS role set — the literal's shape changed"
        assert ts_roles == set(_TURN_OPENER_ROLES)

    def test_synthesis_injection_contract_matches(self) -> None:
        """Both sides key the synthesis boundary on the same wire contract."""
        ts_source = _TS_GROUPING.read_text(encoding="utf-8")
        # The TS predicate: role === 'inject' plus meta.injectKind === 'synthesis'.
        assert re.search(r"msg\.role === 'inject'", ts_source)
        assert re.search(r"injectKind === 'synthesis'", ts_source)
        py_source = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "kiro_crew"
            / "dashboard"
            / "chat_handlers.py"
        ).read_text(encoding="utf-8")
        boundary = re.search(
            r"def _orphan_in_current_turn.*?(?=\ndef )", py_source, flags=re.DOTALL
        )
        assert boundary, "_orphan_in_current_turn not found"
        assert 'msg.get("role") == "inject"' in boundary.group(0)
        assert '.get("injectKind") == "synthesis"' in boundary.group(0)
