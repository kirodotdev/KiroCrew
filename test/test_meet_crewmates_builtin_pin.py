"""The Meet CrewMates flow's name hint agrees with the backend's agent-name grammar.

``POST /api/agents`` refuses a crew name that fails ``validation._AGENT_NAME_RE``
(``invalid_agent_name``), because ``GET /api/members`` skips such a row. The
flow previews that rule under the name field as the user types, from a copy of
the regex; this test keeps the copy honest on the cases that matter.
"""

from __future__ import annotations

import re
from pathlib import Path

from kiro_crew.validation import _AGENT_NAME_RE

_REPO = Path(__file__).resolve().parents[1]
_FLOW = _REPO / "website" / "src" / "components" / "MeetCrewmatesFlow.tsx"
_NAME_RE_LITERAL = re.compile(r"export const AGENT_NAME_RE = /(?P<body>[^/]+)/")


def test_frontend_name_grammar_agrees_with_backend_on_the_cases_that_matter() -> None:
    # The flow refuses, before Next, every name `GET /api/members` would skip:
    # the two regexes must agree on the whole probe set, not just on spaces.
    m = _NAME_RE_LITERAL.search(_FLOW.read_text(encoding="utf-8"))
    assert m, f"AGENT_NAME_RE literal not found in {_FLOW}"
    frontend = re.compile(m.group("body"))
    probes = [
        "Radar",
        "R",
        "issue-radar_2",
        "a-b",
        "a_b",
        "A1",
        "Issue Radar",
        "-radar",
        "radar-",
        "_x",
        "x_",
        "",
        " ",
        "雷达",
        "ra.dar",
        "ra/dar",
        "a" * 64,
        "a" * 63,
        "a" * 2,
    ]
    for name in probes:
        assert bool(frontend.fullmatch(name)) == bool(_AGENT_NAME_RE.match(name)), name
