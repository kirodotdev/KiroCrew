"""Ratchet: every place that starts an agent goes through a start gate.

A fork's ``allowedTools`` and the main spec's both bypass the PreToolUse gate, so
the one check that their grants are current is
:func:`kiro_crew.agent.require_fork_governance`. It only protects the starts that
call it. An agent starts in one of two ways, and both are scanned here:

* a backend launched with ``--agent <name>`` (an argv list holding ``"--agent"``);
* a running backend switched to an agent with ``session/set_mode`` (a call passing
  ``METHOD_SET_MODE``).

The function holding a launch must reference ``require_fork_governance``. A switch
may reference it or only its main-spec part, ``require_main_spec_projected``: fork
gating at ``session/set_mode`` is out of this check's scope, and the main-spec
part is bounded to the unreadable-spec episode. A site that references
neither must be listed in :data:`EXEMPT` with the reason it starts no governed
agent. The exempt
set must match exactly, so a site that is routed later leaves a stale entry that
fails here too. The scan is :mod:`ast`, so a docstring or comment naming either
form is not a site, and paths are compared as POSIX relative paths so the result
is the same on every OS.
"""

from __future__ import annotations

import ast
import functools
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "kiro_crew"

GATE = "require_fork_governance"
#: The gates a ``session/set_mode`` switch may use; never neither.
SWITCH_GATES = frozenset({GATE, "require_main_spec_projected"})
_AGENT_FLAG = "--agent"
_SET_MODE = "METHOD_SET_MODE"

#: One worker parses the tree once for the whole module.
pytestmark = pytest.mark.xdist_group("tree_scan_agent_start_gate_ratchet")

#: ``<posix path under src/kiro_crew>::<qualified function>`` -> why it starts no
#: governed agent.
EXEMPT: dict[str, str] = {
    "mcp_gateway/rewriter.py::_build_stub_entry": (
        "MCP stub argv: ``--agent`` names the session's agent for the pool key; the "
        "stub launches an MCP server, not an agent"
    ),
    "dashboard/handlers/sessions.py::_usage_scrape_argv": (
        "the usage scrape runs Crew's own ``kirocrew-lite`` service agent, which is "
        "neither a fork nor the main spec or a mirror of it"
    ),
    "acp/client.py::AcpClient._pin_claude_starting_mode": (
        "sends a fixed Claude permission mode id, not an agent name"
    ),
    "acp/client.py::AcpClient._initialize_session": (
        "activates ``self._agent``, the agent this process was launched as, which "
        "``AcpClient._spawn`` gated before the launch"
    ),
}


def _is_agent_argv(node: ast.AST) -> bool:
    return isinstance(node, (ast.List, ast.Tuple)) and any(
        isinstance(elt, ast.Constant) and elt.value == _AGENT_FLAG for elt in node.elts
    )


def _is_set_mode_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    for arg in node.args:
        if isinstance(arg, ast.Name) and arg.id == _SET_MODE:
            return True
        if isinstance(arg, ast.Attribute) and arg.attr == _SET_MODE:
            return True
    return False


def _references_gate(func: ast.AST, gates: frozenset[str] = frozenset({GATE})) -> bool:
    for node in ast.walk(func):
        if isinstance(node, ast.Name) and node.id in gates:
            return True
        if isinstance(node, ast.Attribute) and node.attr in gates:
            return True
    return False


def start_sites(source: str, module: str) -> dict[str, bool]:
    """``{"<module>::<function>": gated}`` for every agent-start site in *source*."""
    tree = ast.parse(source)
    sites: dict[str, bool] = {}

    def visit(node: ast.AST, scope: list[str], func: ast.AST | None) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                visit(child, [*scope, child.name], child)
                continue
            if isinstance(child, ast.ClassDef):
                visit(child, [*scope, child.name], func)
                continue
            launch = _is_agent_argv(child)
            if launch or _is_set_mode_call(child):
                key = f"{module}::{'.'.join(scope) or '<module>'}"
                gates = frozenset({GATE}) if launch else SWITCH_GATES
                gated = func is not None and _references_gate(func, gates)
                sites[key] = sites.get(key, True) and gated
            visit(child, scope, func)

    visit(tree, [], None)
    return sites


@functools.lru_cache(maxsize=1)
def _all_sites() -> dict[str, bool]:
    sites: dict[str, bool] = {}
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        if "/tests/" in f"/{rel}" or rel.startswith("testing/"):
            continue
        sites.update(start_sites(path.read_text(encoding="utf-8"), rel))
    return sites


def test_every_agent_start_goes_through_the_gate():
    ungated = {key for key, gated in _all_sites().items() if not gated}
    assert ungated == set(EXEMPT), (
        f"ungated agent starts not in EXEMPT: {sorted(ungated - set(EXEMPT))}; "
        f"EXEMPT entries that are no longer ungated sites: {sorted(set(EXEMPT) - ungated)}. "
        f"Route a new start through {GATE}, or exempt it with the reason it starts no "
        "governed agent."
    )


def test_the_scan_finds_the_known_gated_starts():
    """The scan is not passing because it matches nothing."""
    sites = _all_sites()
    for key in (
        "acp/harness/kiro.py::KiroHarness.resolve_spawn",
        "acp/runtime.py::AcpRuntime._activate_mode_bracketed",
        "acp/session_handle.py::AcpSessionHandle.set_mode",
    ):
        assert sites.get(key) is True, key


def test_the_wire_registered_switch_is_gated_where_its_payload_is_built():
    """``_activate_mode_bracketed`` skips the gate for a wire-registered host.

    There the agent was consumed at ``session/new``, in the payload
    ``KasHarness.session_extras`` builds, so that is where the gate must run.
    """
    tree = ast.parse((SRC / "acp" / "harness" / "kas.py").read_text(encoding="utf-8"))
    extras = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "session_extras"
    ]
    assert len(extras) == 1 and _references_gate(extras[0])


def test_a_start_that_skips_the_gate_is_reported():
    """Mutation pin: a launch or a switch with no gate call goes red."""
    source = (
        "async def launch(agent):\n"
        "    return ['kiro', 'acp', '--agent', agent]\n"
        "async def switch(handle, agent):\n"
        "    await handle.send(METHOD_SET_MODE, agent)\n"
        "async def gated(agent):\n"
        "    await asyncio.to_thread(require_fork_governance, agent, None)\n"
        "    return ['kiro', 'acp', '--agent', agent]\n"
        "async def switch_main_only(handle, agent):\n"
        "    await asyncio.to_thread(require_main_spec_projected, agent, None)\n"
        "    await handle.send(METHOD_SET_MODE, agent)\n"
        "async def launch_main_only(agent):\n"
        "    await asyncio.to_thread(require_main_spec_projected, agent, None)\n"
        "    return ['kiro', 'acp', '--agent', agent]\n"
    )
    assert start_sites(source, "m.py") == {
        "m.py::launch": False,
        "m.py::switch": False,
        "m.py::gated": True,
        "m.py::switch_main_only": True,
        "m.py::launch_main_only": False,
    }
