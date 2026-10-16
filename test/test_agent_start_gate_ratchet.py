"""Ratchet: every ``session/set_mode`` switch goes through a start gate, and the hold flag has one reader.

A fork's ``allowedTools`` and the main spec's both bypass the PreToolUse gate, so
the checks that their grants are current are
:func:`kiro_crew.agent.require_fork_governance` and, for the main spec alone,
:func:`kiro_crew.agent.require_main_spec_projected`. A backend launched with
``--agent <name>`` is pinned by ``test_agent_admission_paths_ratchet.py``. That
table does not see the other way an agent starts, a running backend switched to
an agent with ``session/set_mode`` (a call passing ``METHOD_SET_MODE``), and that is
scanned here.

The function holding a switch must reference ``require_fork_governance`` or only its
main-spec part, ``require_main_spec_projected``: fork gating at ``session/set_mode``
is out of this check's scope, and the main-spec part is bounded to the
unreadable-spec episode. A switch that references neither must be listed in
:data:`EXEMPT` with the reason it starts no governed agent. The exempt set must
match exactly, so a site that is routed later leaves a stale entry that fails here
too. The scan is :mod:`ast`, so a docstring or comment naming either form is not a
site, and paths are compared as POSIX relative paths so the result is the same on
every OS.

The second check keeps the rebuild's hold in one reader. A held rebuild is two
facts, ``_conductor_spec_held`` and an open unreadable-spec episode, and a reader
that looks at the first alone skips the retry or seeds the ceiling baseline during
an episode, with nothing turning red. Only ``_spec_hold_open`` and the template-only
probe ``_held_rebuild_would_repeat`` (which needs the flag by itself, and says so)
may read it; everything else asks ``_spec_hold_open``.
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
_SET_MODE = "METHOD_SET_MODE"

#: The raw hold flag, and the only functions that may read it.
_HOLD_FLAG = "_conductor_spec_held"
HOLD_READERS = frozenset(
    {
        "agent.py::_spec_hold_open",
        "agent.py::_held_rebuild_would_repeat",
    }
)

#: One worker parses the tree once for the whole module.
pytestmark = pytest.mark.xdist_group("tree_scan_agent_start_gate_ratchet")

#: ``<posix path under src/kiro_crew>::<qualified function>`` -> why it starts no
#: governed agent.
EXEMPT: dict[str, str] = {
    "acp/client_claude_session.py::_pin_claude_starting_mode": (
        "sends a fixed Claude permission mode id, not an agent name"
    ),
    "acp/client.py::AcpClient._initialize_session": (
        "activates ``self._agent``, the agent this process was launched as, which "
        "``AcpClient._spawn`` gated before the launch"
    ),
}


def _is_set_mode_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    for arg in node.args:
        if isinstance(arg, ast.Name) and arg.id == _SET_MODE:
            return True
        if isinstance(arg, ast.Attribute) and arg.attr == _SET_MODE:
            return True
    return False


def _references_gate(func: ast.AST, gates: frozenset[str] = SWITCH_GATES) -> bool:
    for node in ast.walk(func):
        if isinstance(node, ast.Name) and node.id in gates:
            return True
        if isinstance(node, ast.Attribute) and node.attr in gates:
            return True
    return False


def start_sites(source: str, module: str) -> dict[str, bool]:
    """``{"<module>::<function>": gated}`` for every ``session/set_mode`` call in *source*."""
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
            if _is_set_mode_call(child):
                key = f"{module}::{'.'.join(scope) or '<module>'}"
                gated = func is not None and _references_gate(func)
                sites[key] = sites.get(key, True) and gated
            visit(child, scope, func)

    visit(tree, [], None)
    return sites


def hold_flag_readers(source: str, module: str) -> set[str]:
    """``{"<module>::<function>"}`` for every function in *source* that reads the hold flag."""
    tree = ast.parse(source)
    readers: set[str] = set()

    def visit(node: ast.AST, scope: list[str]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                visit(child, [*scope, child.name])
                continue
            loaded = (
                isinstance(child, ast.Name)
                and child.id == _HOLD_FLAG
                and isinstance(child.ctx, ast.Load)
            ) or (
                isinstance(child, ast.Attribute)
                and child.attr == _HOLD_FLAG
                and isinstance(child.ctx, ast.Load)
            )
            if loaded:
                readers.add(f"{module}::{'.'.join(scope) or '<module>'}")
            visit(child, scope)

    visit(tree, [])
    return readers


def _source_files() -> list[tuple[str, str]]:
    files: list[tuple[str, str]] = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        if "/tests/" in f"/{rel}" or rel.startswith("testing/"):
            continue
        files.append((rel, path.read_text(encoding="utf-8")))
    return files


@functools.lru_cache(maxsize=1)
def _all_sites() -> dict[str, bool]:
    sites: dict[str, bool] = {}
    for rel, source in _source_files():
        sites.update(start_sites(source, rel))
    return sites


@functools.lru_cache(maxsize=1)
def _all_hold_readers() -> frozenset[str]:
    readers: set[str] = set()
    for rel, source in _source_files():
        readers |= hold_flag_readers(source, rel)
    return frozenset(readers)


def test_every_agent_switch_goes_through_a_gate():
    ungated = {key for key, gated in _all_sites().items() if not gated}
    assert ungated == set(EXEMPT), (
        f"ungated session/set_mode switches not in EXEMPT: {sorted(ungated - set(EXEMPT))}; "
        f"EXEMPT entries that are no longer ungated sites: {sorted(set(EXEMPT) - ungated)}. "
        f"Route a new switch through {GATE} or require_main_spec_projected, or exempt it "
        "with the reason it starts no governed agent."
    )


def test_the_scan_finds_the_known_gated_switches():
    """The scan is not passing because it matches nothing."""
    sites = _all_sites()
    for key in (
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
    assert len(extras) == 1 and _references_gate(extras[0], frozenset({GATE}))


def test_a_switch_that_skips_the_gate_is_reported():
    """Mutation pin: a switch with no gate call goes red."""
    source = (
        "async def switch(handle, agent):\n"
        "    await handle.send(METHOD_SET_MODE, agent)\n"
        "async def gated(handle, agent):\n"
        "    await asyncio.to_thread(require_fork_governance, agent, None)\n"
        "    await handle.send(METHOD_SET_MODE, agent)\n"
        "async def switch_main_only(handle, agent):\n"
        "    await asyncio.to_thread(require_main_spec_projected, agent, None)\n"
        "    await handle.send(METHOD_SET_MODE, agent)\n"
    )
    assert start_sites(source, "m.py") == {
        "m.py::switch": False,
        "m.py::gated": True,
        "m.py::switch_main_only": True,
    }


def test_the_hold_flag_is_read_only_through_the_episode_predicate():
    """Only ``_spec_hold_open`` and the template-only probe read the raw hold flag."""
    readers = _all_hold_readers()
    assert readers == HOLD_READERS, (
        f"readers of {_HOLD_FLAG} outside the allowed set: {sorted(readers - HOLD_READERS)}; "
        f"allowed readers that no longer read it: {sorted(HOLD_READERS - readers)}. "
        "Ask _spec_hold_open() instead, so an open unreadable-spec episode counts as a hold."
    )


def test_a_raw_hold_read_is_reported():
    """Mutation pin: a reader of the raw flag outside the allowed set goes red."""
    source = (
        "_conductor_spec_held = False\n"
        "def settle():\n"
        "    global _conductor_spec_held\n"
        "    _conductor_spec_held = True\n"
        "def retry():\n"
        "    if not _conductor_spec_held:\n"
        "        return\n"
        "def via_module(agent_mod):\n"
        "    return agent_mod._conductor_spec_held\n"
        "def asks_the_predicate():\n"
        "    return _spec_hold_open()\n"
    )
    assert hold_flag_readers(source, "m.py") == {"m.py::retry", "m.py::via_module"}
