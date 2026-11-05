"""Cross-language pin: the stop-card turn boundary mirrors the TS grouping.

``_orphan_in_current_turn`` (chat_handlers.py) re-implements a subset of the
frontend's turn grouping: ``_TURN_OPENER_ROLES`` mirrors ``TURN_OPENER_ROLES``
and its turn-opening inject check mirrors the ``injectOpensTurn`` branch of
``opensDisplayTurn`` — both in ``website/src/pages/chat/groupDisplayItems.ts``. Comment-only mirroring of
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
from kiro_crew.dashboard.state import TURN_OPENING_INJECT_KINDS

_TS_CHAT = Path(__file__).resolve().parents[1] / "website" / "src" / "pages" / "chat"
_TS_GROUPING = _TS_CHAT / "groupDisplayItems.ts"
_TS_RECOVERY_CARD = _TS_CHAT / "RecoveryCard.tsx"


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

    def test_turn_opening_inject_contract_matches(self) -> None:
        """Both sides open a turn at the same ``inject`` kinds.

        The TS grouping opens a turn at every inject ``injectOpensTurn`` accepts,
        which reads ``INJECT_KIND_OPENS_TURN`` in RecoveryCard.tsx. The stop-card
        walk stops at every kind in ``TURN_OPENING_INJECT_KINDS``. The two kind
        sets must be equal.
        """
        ts_source = _TS_GROUPING.read_text(encoding="utf-8")
        assert re.search(
            r"opensDisplayTurn = \(msg: ChatMessage\): boolean =>\s*"
            r"TURN_OPENER_ROLES\.has\(msg\.role\) \|\| injectOpensTurn\(msg\)",
            ts_source,
        ), "opensDisplayTurn no longer reads injectOpensTurn — update this pin with it"
        assert "opensDisplayTurn(item.msg)" in ts_source
        table = re.search(
            r"INJECT_KIND_OPENS_TURN: Readonly<Record<InjectKind, boolean>> = \{(?P<body>.*?)\n\}",
            _TS_RECOVERY_CARD.read_text(encoding="utf-8"),
            flags=re.DOTALL,
        )
        assert table, "INJECT_KIND_OPENS_TURN literal not found — update this parser with it"
        entries = dict(re.findall(r"^\s*(\w+): (true|false),", table.group("body"), flags=re.M))
        assert entries, "parsed an empty INJECT_KIND_OPENS_TURN table — its shape changed"
        ts_kinds = {kind for kind, opens in entries.items() if opens == "true"}
        assert ts_kinds == set(TURN_OPENING_INJECT_KINDS)
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
        assert '.get("injectKind") in TURN_OPENING_INJECT_KINDS' in boundary.group(0)
