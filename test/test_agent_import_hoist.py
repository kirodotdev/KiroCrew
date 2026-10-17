"""Module-scope ``kiro_crew.agent`` imports stay at module scope.

The four modules below import ``from kiro_crew.agent import ...`` at module
scope. ``kiro_crew.agent`` imports nothing from ``kiro_crew.dashboard.*`` or
``kiro_crew.session``, so no import cycle exists; these tests keep the imports
at module scope and prove no cycle exists in either load order.

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
    "kiro_crew/session_parts/claims.py",
    "kiro_crew/session_parts/config_apply.py",
    "kiro_crew/session_parts/links.py",
    "kiro_crew/session_parts/policy.py",
    "kiro_crew/session_parts/provider_probes.py",
    "kiro_crew/session_parts/record.py",
    "kiro_crew/session_parts/runtimes.py",
    "kiro_crew/session_parts/state_views.py",
    "kiro_crew/session_parts/sweeps.py",
    "kiro_crew/session_parts/teardown.py",
    "kiro_crew/session_parts/turn_context.py",
)


#: ``handlers/agents.py`` runs the functions its ``dashboard/agent_admin`` owners
#: define on its own globals, so the ratchet reads those owners as part of it.
_COMPOSED_OWNERS = {
    "kiro_crew/dashboard/handlers/agents.py": "kiro_crew/dashboard/agent_admin",
}


def _hoisted_text(rel: str) -> str:
    """*rel*'s source, followed by the owner modules it composes."""
    text = (_SRC / rel).read_text(encoding="utf-8")
    owner_dir = _COMPOSED_OWNERS.get(rel)
    if owner_dir is None:
        return text
    owners = sorted((_SRC / owner_dir).glob("[!_]*.py"))
    assert len(owners) >= 13, f"{owner_dir}: the composed owners were not found"
    return "\n".join([text, *(owner.read_text(encoding="utf-8") for owner in owners)])


def _fresh_import(statements: str) -> None:
    """Run import statements in a fresh child interpreter; fail on any error."""
    res = subprocess.run(
        [sys.executable, "-c", statements],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    assert res.returncode == 0, (
        f"fresh-interpreter import failed (a hoisted import created a cycle?):\n" f"{res.stderr}"
    )


def test_hoisted_modules_import_together_fresh() -> None:
    """All four hoisted modules import together in a cold interpreter."""
    _fresh_import("; ".join(f"import {m}" for m in _HOISTED_MODULES))


def test_hoisted_modules_import_agent_first_fresh() -> None:
    """Loading kiro_crew.agent BEFORE the handlers must also be cycle-free.

    A cycle between ``agent`` and these modules would be order-dependent:
    it can pass in one load order and raise ImportError in the other.
    """
    _fresh_import("; ".join(["import kiro_crew.agent"] + [f"import {m}" for m in _HOISTED_MODULES]))


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
        text = _hoisted_text(rel)
        hits = local_import.findall(text)
        if hits:
            offenders[rel] = len(hits)
    assert not offenders, (
        f"function-local kiro_crew.agent imports reintroduced: {offenders}; "
        f"import at module scope instead (no cycle exists — see issue #1050)"
    )


def test_members_and_artifacts_import_first_fresh() -> None:
    """``kiro_crew.members`` (and so ``memory_stores.provision_member_memory``)
    must load in a process whose FIRST ``kiro_crew`` import reaches ``artifacts``.

    ``artifacts`` imports ``hooks`` at module scope, and ``hooks`` ->
    ``webhooks`` -> ``validation``; when ``validation`` read its content cap
    from ``artifacts`` that edge closed a cycle which raised ImportError in
    exactly this load order (the gateway and CLI dodged it only by importing
    ``validation`` first). The cap now lives in the ``constants`` leaf.
    """
    _fresh_import("import kiro_crew.members; import kiro_crew.artifacts")
    _fresh_import("import kiro_crew.artifacts; import kiro_crew.members")


def test_validation_is_a_leaf_that_never_imports_artifacts() -> None:
    """The closing edge stays deleted: loading ``validation`` must not pull
    ``artifacts`` in, or the cycle above is one hoisted import from returning."""
    _fresh_import(
        "import sys; import kiro_crew.validation; "
        "assert 'kiro_crew.artifacts' not in sys.modules, 'validation imports artifacts'"
    )
