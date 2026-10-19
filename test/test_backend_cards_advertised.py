"""A card line backed by an advertised field gains no new membership gate.

``ADVERTISED_FIELD_BY_LINE`` names the card lines the runtime also gates on a
handshake field. A runtime site that tests a deciding set of one of those lines
(``backend in ACP_BACKENDS_*``) is counted per file, and the count may only
shrink: a new site reads the field instead.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Dict, Tuple

from kiro_crew.agent_sdk.backend_cards import (
    ADVERTISED_FIELD_BY_LINE,
    DETAIL_LINES,
    USER_FACING_LINES,
)

SRC = Path(__file__).resolve().parents[1] / "src" / "kiro_crew"
SPECS = {spec.id: spec for spec in USER_FACING_LINES + DETAIL_LINES}

#: Runtime sites, per (file, set), that gate a field-backed line on a set today.
#: Shrink-only: convert a site to read the field, then lower its count here.
SET_GATE_ALLOWLIST: Dict[Tuple[str, str], int] = {
    ("acp/client_class.py", "ACP_BACKENDS_STEER"): 2,
    ("acp/client_class.py", "ACP_BACKENDS_STEERING_REQUEST"): 3,
    ("acp/client_class.py", "ACP_BACKENDS_STEER_CONCURRENT_PROMPT"): 2,
    ("acp/client_model_selection.py", "ACP_BACKENDS_MODEL_VIA_CONFIG_OPTION"): 2,
    ("acp/client_turn_control.py", "ACP_BACKENDS_STEERING_REQUEST"): 1,
    ("acp/client_turn_control.py", "ACP_BACKENDS_STEER_CONCURRENT_PROMPT"): 1,
    ("acp/session_handle_class.py", "ACP_BACKENDS_STEER"): 2,
    ("acp/session_handle_class.py", "ACP_BACKENDS_STEERING_REQUEST"): 2,
    ("acp/session_handle_model_selection.py", "ACP_BACKENDS_MODEL_VIA_CONFIG_OPTION"): 1,
    ("acp/session_handle_steering.py", "ACP_BACKENDS_STEERING_REQUEST"): 2,
    ("acp/session_provider.py", "ACP_BACKENDS_COMPACT"): 2,
    ("agent_sdk/capabilities.py", "ACP_BACKENDS_EFFORT_VIA_CONFIG_OPTION"): 2,
    ("agent_sdk/capabilities.py", "ACP_BACKENDS_KIRO_SLASH_COMMANDS"): 1,
    ("dashboard/chat_turn/slash_commands.py", "ACP_BACKENDS_COMPACT"): 1,
    ("providers/acp.py", "ACP_BACKENDS_COMPACT"): 2,
    ("providers/acp.py", "ACP_BACKENDS_KIRO_SLASH_COMMANDS"): 1,
    ("providers/acp_model_effort.py", "ACP_BACKENDS_KIRO_SLASH_COMMANDS"): 2,
    ("providers/acp_turns.py", "ACP_BACKENDS_KIRO_SLASH_COMMANDS"): 1,
    ("subagent_parts/model_choice.py", "ACP_BACKENDS_EFFORT_VIA_CONFIG_OPTION"): 1,
}


def _watched_sets() -> frozenset:
    return frozenset(name for line_id in ADVERTISED_FIELD_BY_LINE for name in SPECS[line_id].sets)


def _set_gates() -> Dict[Tuple[str, str], int]:
    """Count ``x in <watched set>`` / ``not in`` per (file, set) under ``src``."""
    watched = _watched_sets()
    counts: Dict[Tuple[str, str], int] = {}
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        if rel.startswith("agent_sdk/backends") or rel == "agent_sdk/backend_cards.py":
            continue
        text = path.read_text(encoding="utf-8")
        if not any(name in text for name in watched):
            continue
        for node in ast.walk(ast.parse(text)):
            if not isinstance(node, ast.Compare):
                continue
            for op, right in zip(node.ops, node.comparators):
                if not isinstance(op, (ast.In, ast.NotIn)):
                    continue
                name = getattr(right, "id", None) or getattr(right, "attr", None)
                if name in watched:
                    counts[(rel, name)] = counts.get((rel, name), 0) + 1
    return counts


def test_every_registered_line_is_a_card_line_with_a_field() -> None:
    for line_id, wire_path in ADVERTISED_FIELD_BY_LINE.items():
        assert line_id in SPECS, line_id
        assert wire_path.split(".")[0] in ("initialize", "session"), wire_path


def test_no_new_runtime_site_gates_a_field_backed_line_on_a_set() -> None:
    found = _set_gates()
    grown = {k: v for k, v in found.items() if v > SET_GATE_ALLOWLIST.get(k, 0)}
    assert not grown, (
        "a runtime site gates a line ADVERTISED_FIELD_BY_LINE backs with a field on "
        f"a membership set instead; read the field: {grown}"
    )
    shrunk = {k: v for k, v in SET_GATE_ALLOWLIST.items() if found.get(k, 0) < v}
    assert not shrunk, f"lower SET_GATE_ALLOWLIST to the new counts: {shrunk}"
