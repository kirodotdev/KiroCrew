"""Regression guard for issue #1050: module-scope ``kiro_crew.agent`` imports.

The four modules below historically used function-local
``from kiro_crew.agent import ...`` statements, several justified by
``# circular import`` comments that misstated the real import graph
(``kiro_crew.agent`` imports nothing from ``kiro_crew.dashboard.*`` or
``kiro_crew.session``).  The imports were hoisted to module scope; these
tests keep them there and prove no cycle exists in either load order.

Order-dependent cycles only surface in a fresh interpreter, not under a
bare import in an already-warm test process — hence the subprocess runs.
"""

import re
import subprocess
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"

_HOISTED_MODULES = (
    "kiro_crew.dashboard.handlers.agents",
    "kiro_crew.dashboard.handlers.mcp",
    "kiro_crew.dashboard.handlers.hooks",
    "kiro_crew.session",
)

_HOISTED_FILES = (
    "kiro_crew/dashboard/handlers/agents.py",
    "kiro_crew/dashboard/handlers/mcp.py",
    "kiro_crew/dashboard/handlers/hooks.py",
    "kiro_crew/session.py",
)


def _fresh_import(statements: str) -> None:
    """Run import statements in a fresh child interpreter; fail on any error."""
    res = subprocess.run(
        [sys.executable, "-c", statements],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert res.returncode == 0, (
        f"fresh-interpreter import failed (a hoisted import created a cycle?):\n"
        f"{res.stderr}"
    )


def test_hoisted_modules_import_together_fresh() -> None:
    """All four hoisted modules import together in a cold interpreter."""
    _fresh_import("; ".join(f"import {m}" for m in _HOISTED_MODULES))


def test_hoisted_modules_import_agent_first_fresh() -> None:
    """Loading kiro_crew.agent BEFORE the handlers must also be cycle-free.

    A cycle between ``agent`` and these modules would be order-dependent:
    it can pass in one load order and raise ImportError in the other.
    """
    _fresh_import(
        "; ".join(["import kiro_crew.agent"] + [f"import {m}" for m in _HOISTED_MODULES])
    )


def test_no_function_local_agent_imports_remain() -> None:
    """Ratchet: no function-local ``from kiro_crew.agent import`` in the four files.

    A reintroduced local import would silently undo the hoist and eventually
    re-grow the false ``# circular import`` folklore this fixed.  All three
    repo spellings are covered: ``from kiro_crew.agent import X``,
    ``import kiro_crew.agent``, and ``from kiro_crew import agent`` (the
    last matched on the bare ``agent`` name so sibling imports like
    ``agent_state`` don't trip it; comments are excluded from the match).
    """
    local_import = re.compile(
        r"^[ \t]+(?:"
        r"from kiro_crew\.agent import"
        r"|import kiro_crew\.agent\b"
        r"|from kiro_crew import [^#\n]*\bagent\b"
        r")",
        re.MULTILINE,
    )
    offenders = {}
    for rel in _HOISTED_FILES:
        text = (_SRC / rel).read_text(encoding="utf-8")
        hits = local_import.findall(text)
        if hits:
            offenders[rel] = len(hits)
    assert not offenders, (
        f"function-local kiro_crew.agent imports reintroduced: {offenders}; "
        f"import at module scope instead (no cycle exists — see issue #1050)"
    )
