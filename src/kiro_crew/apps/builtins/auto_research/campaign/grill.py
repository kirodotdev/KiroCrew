"""The question-tree planner behind the grill endpoint.

A campaign is scoped by growing a tree of clarifier (decision) and research
nodes before any agent time is spent. This module owns the tree arithmetic
(depth, node ids), the expand prompt with its untrusted-data fence, parsing the
LLM reply into child nodes and shaping them into stored nodes.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from kiro_crew.apps.builtins.auto_research.campaign import LOGGER_NAME, untrusted
from kiro_crew.llm_helpers import _extract_json_of_type

logger = logging.getLogger(LOGGER_NAME)

# Node JSON contract (see grill-question-tree-design.md):
#   { id, parent|null, kind: "root"|"clarifier"|"research", text,
#     recommended (clarifier only), answer (clarifier only),
#     origin: "grill"|"emergent" (research only), status }
_MAX_GRILL_DEPTH = 4  # a node at this depth cannot be expanded
_GRILL_CHILD_CAP = 5  # max children returned per expand


def _new_node_id() -> str:
    return "n" + uuid.uuid4().hex[:8]


def _node_depth(tree: list[dict], node_id: str) -> int:
    """Depth of node_id (root=0). Returns -1 if node_id is not in the tree."""
    by_id = {n["id"]: n for n in tree if isinstance(n, dict) and "id" in n}
    if node_id not in by_id:
        return -1
    depth = 0
    seen: set = set()
    cur: dict | None = by_id[node_id]
    while cur is not None and cur.get("parent") and cur["id"] not in seen:
        seen.add(cur["id"])
        depth += 1
        cur = by_id.get(cur["parent"])
    return depth


_GRILL_EXPAND_PROMPT = (
    "You are helping a user scope a research campaign by growing a question tree. "
    "Reason from FIRST PRINCIPLES. Given the main question, the tree so far, and the "
    "target node to expand, propose at most 5 children — the highest-value next nodes. "
    "Each child is either:\n"
    '  - "clarifier": a DECISION question to ask the user — something that narrows '
    "scope or surfaces an unknown they may not have considered. These must be genuine "
    "decisions only the user can make, NOT facts discoverable by exploring code/docs/"
    'tools. Include a "recommended" best-guess answer.\n'
    '  - "research": a well-formed, distinct sub-question the campaign should '
    "investigate (use only when it is already a concrete research target).\n"
    "Rules:\n"
    "- Distinct, non-overlapping angles; no generic restatements.\n"
    "- Never propose a clarifier for something the agent could look up itself "
    "(codebase structure, API signatures, existing config, prior decisions in the tree).\n"
    "- Each clarifier should be ONE focused question — asking multiple things in one "
    "node is bewildering and produces shallow answers.\n"
    "Output ONLY a JSON "
    'array like [{"kind":"clarifier","text":"...","recommended":"..."},'
    '{"kind":"research","text":"..."}].'
)


def _compact_tree(tree: list[dict]) -> str:
    """One line per node (id/kind/text + answer) as LLM context."""
    lines = []
    for n in tree:
        if not isinstance(n, dict):
            continue
        line = f"- [{n.get('id', '?')}] {n.get('kind', '?')}: {n.get('text', '')}"
        if n.get("answer"):
            line += f" → answered: {n['answer']}"
        lines.append(line)
    return "\n".join(lines) if lines else "(empty — this is the first round)"


def _grill_node_shaped(value: object) -> bool:
    """Prefer predicate: an array carrying at least one node-shaped record.

    Disambiguates the payload from stray bracketed PROSE that also parses as
    an array (a "see item [1]:" marker, a trailing "[12]." citation) — those
    decode to arrays of scalars and are never preferred."""
    return isinstance(value, list) and any(
        isinstance(item, dict) and "kind" in item and "text" in item for item in value
    )


def _parse_grill_nodes(raw: str) -> list[dict]:
    """Extract child node dicts {kind, text, recommended?} from an LLM reply.

    Extraction delegates to the shared ``llm_helpers._extract_json_of_type``
    scanner, so a stray bracket in surrounding prose cannot corrupt the span
    the way an outermost ``find('[') .. rfind(']')`` slice would.
    Returns [] on any parse failure, or when two DIFFERENT node-shaped arrays
    make the choice ambiguous (the shared contract refuses to guess)."""
    try:
        items = _extract_json_of_type(raw, list, prefer=_grill_node_shaped)
    except RecursionError:
        # The stdlib decoder recurses per nesting level, so a nesting bomb in
        # the untrusted reply overflows long before any structural bound. This
        # parser's callers are outside any exception envelope (the grill-expand
        # handler would surface it as HTTP 500), so degrade to no-nodes here.
        return []
    if not isinstance(items, list):
        return []
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        text = str(it.get("text", "")).strip()
        kind = it.get("kind")
        if not text or kind not in ("clarifier", "research"):
            continue
        node = {"kind": kind, "text": text}
        if kind == "clarifier":
            node["recommended"] = str(it.get("recommended", "")).strip()
        out.append(node)
    return out


async def _grill_expand_children(
    pool: Any, question: str, tree: list[dict], node_id: str | None
) -> list[dict]:
    """Return raw child dicts {kind, text, recommended?} for the target node.

    Uses the dedicated auto_research_llm_pool (CC worker is haiku-backed — the
    fast model the grill wants); empty-on-failure so the UI degrades gracefully.
    """
    if pool is None:
        return []
    target = "the root question (propose the first round of children)"
    if node_id is not None:
        node = next((n for n in tree if isinstance(n, dict) and n.get("id") == node_id), None)
        if node:
            target = f"[{node_id}] {node.get('kind')}: {node.get('text', '')}"
            ans = node.get("answer") or node.get("recommended")
            if ans:
                target += f" (answer: {ans})"
    prompt = (
        f"{_GRILL_EXPAND_PROMPT}\n\n{untrusted._UNTRUSTED_DATA_NOTICE}\n\n"
        f"Main question:\n{untrusted._fence_untrusted(question)}\n\n"
        f"Tree so far:\n{untrusted._fence_untrusted(_compact_tree(tree))}\n\n"
        f"Expand this node:\n{untrusted._fence_untrusted(target)}"
    )
    try:
        raw = await pool.send(prompt, timeout=18.0)
    except Exception as exc:
        logger.warning("auto_research grill expand failed: %s", exc)
        return []
    return _parse_grill_nodes(raw)


def _child_nodes(raw: list[dict], node_id: str | None) -> list[dict]:
    """Shape up to ``_GRILL_CHILD_CAP`` parsed children into stored tree nodes.

    An unknown ``kind`` becomes a research node and blank text is dropped;
    only clarifiers carry a ``recommended`` answer and only research nodes an
    ``origin``.
    """
    nodes = []
    for ch in raw[:_GRILL_CHILD_CAP]:
        kind = ch.get("kind") if ch.get("kind") in ("clarifier", "research") else "research"
        text = str(ch.get("text", "")).strip()
        if not text:
            continue
        nodes.append(
            {
                "id": _new_node_id(),
                "parent": node_id,
                "kind": kind,
                "text": text,
                "recommended": (
                    str(ch.get("recommended", "")).strip() if kind == "clarifier" else ""
                ),
                "answer": "",
                "origin": "grill" if kind == "research" else "",
                "status": "open",
            }
        )
    return nodes
