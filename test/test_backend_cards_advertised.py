"""A card line backed by an advertised field gains no new membership gate.

``ADVERTISED_FIELD_BY_LINE`` names the card lines the runtime also gates on a
handshake field, and ``ADVERTISED_FIELD_BY_SET`` names the handshake fields with no
card line whose sets stand in for them today. A runtime site that tests one of
those sets is counted per file, and the count may only shrink: a new site reads
the field instead. A site is a bare ``backend in ACP_BACKENDS_*``, the same test
through a name bound to the set, a set operation on it (``.issubset``, ``&``, ...),
or a call to a one-line helper that returns such a test.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Dict, FrozenSet, Iterator, Mapping, Optional, Tuple

import pytest

from kiro_crew.agent_sdk.backend_cards import (
    ADVERTISED_FIELD_BY_LINE,
    DETAIL_LINES,
    USER_FACING_LINES,
)

SRC = Path(__file__).resolve().parents[1] / "src" / "kiro_crew"
SPECS = {spec.id: spec for spec in USER_FACING_LINES + DETAIL_LINES}

#: Handshake fields with no card line, and the sets that decide them in their place.
#: An empty tuple: no set stands in today, and a new one must be listed here.
ADVERTISED_FIELD_BY_SET: Mapping[str, Tuple[str, ...]] = {
    "initialize.agentCapabilities.mcpCapabilities": ("ACP_BACKENDS_SESSION_MCP_ARRAY",),
    "initialize.agentCapabilities.loadSession / sessionCapabilities.resume": (
        "ACP_BACKENDS_RESUME_WITHOUT_LOAD",
    ),
    "initialize.agentCapabilities.sessionCapabilities.fork": ("ACP_BACKENDS_FORK_AT_MESSAGE",),
    "initialize.agentCapabilities.promptCapabilities.image": (),
}

#: Runtime sites, per (file, set), that gate a field-backed line on a set today.
#: Shrink-only: convert a site to read the field, then lower its count here.
SET_GATE_ALLOWLIST: Dict[Tuple[str, str], int] = {
    ("acp/client_class.py", "ACP_BACKENDS_SESSION_MCP_ARRAY"): 1,
    ("acp/client_class.py", "ACP_BACKENDS_STEER"): 2,
    ("acp/client_class.py", "ACP_BACKENDS_STEERING_REQUEST"): 4,
    ("acp/client_class.py", "ACP_BACKENDS_STEER_CONCURRENT_PROMPT"): 2,
    ("acp/client_mcp_array.py", "ACP_BACKENDS_SESSION_MCP_ARRAY"): 2,
    ("acp/client_model_selection.py", "ACP_BACKENDS_MODEL_VIA_CONFIG_OPTION"): 2,
    ("acp/client_permissions.py", "ACP_BACKENDS_SESSION_MCP_ARRAY"): 1,
    ("acp/client_start.py", "ACP_BACKENDS_FORK_AT_MESSAGE"): 1,
    ("acp/client_start.py", "ACP_BACKENDS_RESUME_WITHOUT_LOAD"): 2,
    ("acp/client_start.py", "ACP_BACKENDS_SESSION_MCP_ARRAY"): 1,
    ("acp/client_turn_control.py", "ACP_BACKENDS_STEERING_REQUEST"): 2,
    ("acp/client_turn_control.py", "ACP_BACKENDS_STEER_CONCURRENT_PROMPT"): 1,
    ("acp/runtime_session_setup.py", "ACP_BACKENDS_SESSION_MCP_ARRAY"): 1,
    ("acp/session_handle_class.py", "ACP_BACKENDS_STEER"): 2,
    ("acp/session_handle_class.py", "ACP_BACKENDS_STEERING_REQUEST"): 2,
    ("acp/session_handle_model_selection.py", "ACP_BACKENDS_MODEL_VIA_CONFIG_OPTION"): 1,
    ("acp/session_handle_steering.py", "ACP_BACKENDS_STEERING_REQUEST"): 2,
    ("acp/session_provider.py", "ACP_BACKENDS_COMPACT"): 2,
    ("acp/session_provider.py", "ACP_BACKENDS_STEER"): 1,
    ("acp/session_provider.py", "ACP_BACKENDS_STEERING_REQUEST"): 1,
    ("agent_sdk/capabilities.py", "ACP_BACKENDS_EFFORT_VIA_CONFIG_OPTION"): 1,
    ("agent_sdk/capabilities.py", "ACP_BACKENDS_KIRO_SLASH_COMMANDS"): 1,
    ("dashboard/channel_handoff.py", "ACP_BACKENDS_STEERING_REQUEST"): 1,
    ("dashboard/chat_delivery.py", "ACP_BACKENDS_STEERING_REQUEST"): 1,
    ("dashboard/chat_turn/deny_chokepoints.py", "ACP_BACKENDS_STEER"): 2,
    ("dashboard/chat_turn/slash_commands.py", "ACP_BACKENDS_COMPACT"): 1,
    ("deny_notice.py", "ACP_BACKENDS_STEER"): 1,
    ("providers/acp.py", "ACP_BACKENDS_COMPACT"): 2,
    ("providers/acp.py", "ACP_BACKENDS_KIRO_FAMILY"): 1,
    ("providers/acp.py", "ACP_BACKENDS_STEER"): 1,
    ("providers/acp.py", "ACP_BACKENDS_STEERING_REQUEST"): 2,
    ("providers/acp_model_effort.py", "ACP_BACKENDS_KIRO_FAMILY"): 2,
    ("providers/acp_turns.py", "ACP_BACKENDS_KIRO_SLASH_COMMANDS"): 1,
    ("providers/acp_turns.py", "ACP_BACKENDS_STEERING_REQUEST"): 1,
    ("subagent_manager/continuation.py", "ACP_BACKENDS_STEERING_REQUEST"): 1,
    ("subagent_parts/model_choice.py", "ACP_BACKENDS_EFFORT_VIA_CONFIG_OPTION"): 1,
}

_SET_METHODS = frozenset(
    {"issubset", "issuperset", "isdisjoint", "intersection", "union", "difference"}
)
_SET_OPS = (ast.BitAnd, ast.BitOr, ast.Sub, ast.BitXor)
_ORDER_OPS = (ast.Lt, ast.LtE, ast.Gt, ast.GtE)


def _watched_sets() -> FrozenSet[str]:
    by_line = {name for line_id in ADVERTISED_FIELD_BY_LINE for name in SPECS[line_id].sets}
    return frozenset(by_line.union(*ADVERTISED_FIELD_BY_SET.values()))


def _set_of(node: ast.AST, names: Mapping[str, str]) -> Optional[str]:
    """The watched set *node* names, directly, by attribute or through an alias."""
    if isinstance(node, ast.Name):
        return names.get(node.id)
    if isinstance(node, ast.Attribute):
        return names.get(node.attr)
    return None


def _aliases(tree: ast.Module, watched: FrozenSet[str]) -> Dict[str, str]:
    names = {name: name for name in watched}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in watched and alias.asname:
                    names[alias.asname] = alias.name
    for node in tree.body:  # module-level ``S = ACP_BACKENDS_X`` / ``S: T = ...``
        targets = node.targets if isinstance(node, ast.Assign) else [getattr(node, "target", None)]
        value = getattr(node, "value", None)
        bound = _set_of(value, names) if value is not None else None
        for target in targets:
            if bound and isinstance(target, ast.Name):
                names[target.id] = bound
    return names


def _sites(tree: ast.AST, names: Mapping[str, str]) -> Iterator[str]:
    """Yield the watched set behind every membership site in *tree*."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            operands = [node.left, *node.comparators]
            for i, op in enumerate(node.ops):
                if isinstance(op, (ast.In, ast.NotIn)):
                    hit = _set_of(operands[i + 1], names)
                elif isinstance(op, _ORDER_OPS):
                    hit = _set_of(operands[i], names) or _set_of(operands[i + 1], names)
                else:
                    hit = None
                if hit:
                    yield hit
        elif isinstance(node, ast.BinOp) and isinstance(node.op, _SET_OPS):
            hit = _set_of(node.left, names) or _set_of(node.right, names)
            if hit:
                yield hit
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in _SET_METHODS:
                hit = _set_of(node.func.value, names)
                hit = hit or next(filter(None, (_set_of(a, names) for a in node.args)), None)
                if hit:
                    yield hit


def _helpers(trees: Mapping[str, ast.Module], watched: FrozenSet[str]) -> Dict[str, str]:
    """Functions whose body is one ``return`` of a single membership site."""
    found: Dict[str, str] = {}
    for tree in trees.values():
        names = _aliases(tree, watched)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            body = node.body[1:] if ast.get_docstring(node) is not None else node.body
            if len(body) == 1 and isinstance(body[0], ast.Return) and body[0].value is not None:
                hits = set(_sites(body[0].value, names))
                if len(hits) == 1:
                    found[node.name] = hits.pop()
    return found


def _count(sources: Mapping[str, str], skip=lambda rel: False) -> Dict[Tuple[str, str], int]:
    """Count membership sites per (file, set) over *sources* (rel path -> text)."""
    watched = _watched_sets()
    trees = {rel: ast.parse(text) for rel, text in sources.items() if "ACP_BACKENDS_" in text}
    helpers = _helpers(trees, watched)
    counts: Dict[Tuple[str, str], int] = {}
    for rel, text in sources.items():
        if skip(rel) or not any(name in text for name in (*watched, *helpers)):
            continue
        tree = trees.get(rel) or ast.parse(text)
        hits = list(_sites(tree, _aliases(tree, watched)))
        for node in ast.walk(tree):
            # ``helper(b)``, ``obj.helper(b)``, a property ``obj.helper``, or one
            # read by name with ``getattr(obj, "helper", ...)``: each is one site.
            if isinstance(node, ast.Name) and node.id in helpers and isinstance(node.ctx, ast.Load):
                hits.append(helpers[node.id])
            elif isinstance(node, ast.Attribute) and node.attr in helpers:
                hits.append(helpers[node.attr])
            elif isinstance(node, ast.Call) and getattr(node.func, "id", None) in (
                "getattr",
                "hasattr",
            ):
                key = getattr(node.args[1], "value", None) if len(node.args) > 1 else None
                if isinstance(key, str) and key in helpers:
                    hits.append(helpers[key])
        for name in hits:
            counts[(rel, name)] = counts.get((rel, name), 0) + 1
    return counts


def _is_set_home(rel: str) -> bool:
    # The sets' own modules define and compose them, and backend_cards.py projects
    # them onto card lines; neither is a runtime gate. A helper defined there is
    # still found, and every call to it from a runtime file is counted.
    return rel.startswith("agent_sdk/backends") or rel == "agent_sdk/backend_cards.py"


def _set_gates() -> Dict[Tuple[str, str], int]:
    sources = {
        p.relative_to(SRC).as_posix(): p.read_text(encoding="utf-8")
        for p in sorted(SRC.rglob("*.py"))
    }
    return _count(sources, skip=_is_set_home)


def test_every_registered_line_is_a_card_line_with_a_field() -> None:
    for line_id, wire_path in ADVERTISED_FIELD_BY_LINE.items():
        assert line_id in SPECS, line_id
        assert wire_path.split(".")[0] in ("initialize", "session"), wire_path
    for wire_path in ADVERTISED_FIELD_BY_SET:
        assert wire_path.startswith("initialize."), wire_path


def _grown(
    found: Mapping[Tuple[str, str], int], allowlist: Mapping[Tuple[str, str], int]
) -> Dict[Tuple[str, str], int]:
    return {k: v for k, v in found.items() if v > allowlist.get(k, 0)}


def test_no_new_runtime_site_gates_a_field_backed_line_on_a_set() -> None:
    found = _set_gates()
    grown = _grown(found, SET_GATE_ALLOWLIST)
    assert not grown, (
        "a runtime site gates a field-backed line or field on a membership set "
        f"instead; read the field: {grown}"
    )
    shrunk = {k: v for k, v in SET_GATE_ALLOWLIST.items() if found.get(k, 0) < v}
    assert not shrunk, f"lower SET_GATE_ALLOWLIST to the new counts: {shrunk}"


_HELPER_HOME = '''
from kiro_crew.agent_sdk.backends_turns import ACP_BACKENDS_STEER

def steers(backend):
    """One membership test."""
    return backend in ACP_BACKENDS_STEER
'''


@pytest.mark.parametrize(
    "planted",
    [
        "from x import ACP_BACKENDS_STEER as S\ndef f(b):\n    return b in S\n",
        "from x import ACP_BACKENDS_STEER\nS = ACP_BACKENDS_STEER\ndef f(b):\n    return b in S\n",
        "import x\ndef f(b):\n    return {b}.issubset(x.ACP_BACKENDS_STEER)\n",
        "import x\ndef f(bs):\n    return bool(bs & x.ACP_BACKENDS_STEER)\n",
        "from h import steers\ndef f(b):\n    return steers(b)\n",
        "def f(client):\n    return getattr(client, 'steers', False)\n",
    ],
    ids=["import-alias", "module-alias", "issubset", "and", "helper", "helper-by-name"],
)
def test_planted_gate_is_counted(planted: str) -> None:
    sources = {"agent_sdk/backends_h.py": _HELPER_HOME, "planted.py": planted}
    found = _count(sources, skip=_is_set_home)
    assert found == {("planted.py", "ACP_BACKENDS_STEER"): 1}
    assert _grown(found, {}), "an unlisted planted site must fail the ratchet"
    assert not _grown(found, {("planted.py", "ACP_BACKENDS_STEER"): 1})
