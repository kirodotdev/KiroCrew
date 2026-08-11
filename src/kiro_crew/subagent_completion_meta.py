"""Structured header facts for sub-agent completion cards.

A finished sub-agent's result is injected into the parent session as the next
turn's input (see ``gateway._subagent_done``). The dashboard renders that
injected text as a compact card instead of the machine-facing prompt it is; to
decide the outcome, tallies, and headline it needs the header FACTS — which
agent, success or failure, how much of a wave landed.

Historically the card recovered those facts by re-parsing the English header
prose with regexes on the frontend. Nothing pinned the two sides together, so a
reword of the prose here silently broke card rendering with no failing test
(issue #1792). These helpers stamp the same facts as a structured dict on the
injected message's ``meta[SUBAGENT_COMPLETION_META_KEY]`` at composition time;
the frontend reads that first and demotes the regexes to a legacy fallback.

The dict shape mirrors ``ParsedSingleCompletion`` / ``ParsedBatchCompletion`` in
``website/src/pages/chat/subagentCompletion.ts`` (minus ``body``, which the
frontend still derives from the message's blank-line split — a structural
boundary, not prose). Keep the two in lockstep.

Leaf module: imports only ``constants`` so any layer — the core ``subagent.py``
recovery paths and the dashboard gateway alike — can build the meta without a
dependency cycle.
"""

from __future__ import annotations

# Outcome tokens shared with the frontend ``SubagentOutcome`` union. The glyph
# the prose carries is derived FROM these, not the other way round, so this is
# the single source of truth for a completion's outcome.
OUTCOME_OK = "ok"
OUTCOME_FAILED = "failed"
OUTCOME_STOPPED = "stopped"
OUTCOME_INTERRUPTED = "interrupted"


def single_completion_meta(
    *,
    agent_id: str,
    outcome: str,
    agent_name: str = "",
    task: str = "",
    note: str = "",
) -> dict:
    """Structured facts for a per-agent completion card.

    ``outcome`` is one of the ``OUTCOME_*`` tokens. ``note`` carries the words
    that sit beside the glyph on the restart/timeout shapes (e.g. "orphaned by
    gateway restart") — the ONLY explanation those messages carry — so the card
    can fold it into the payload without re-reading the header line. Empty on the
    ordinary completion path, where the status word is redundant with the chip.
    """
    return {
        "kind": "single",
        "agentId": agent_id,
        "agentName": agent_name,
        "outcome": outcome,
        "task": task,
        "note": note,
    }


def wave_final_meta(
    *,
    chunk: int,
    chunks: int,
    ok: int,
    failed: int,
    stopped: int,
    total: int,
) -> dict:
    """Structured facts for the FINAL chunk of a wave digest — the only chunk
    that carries terminal tallies. ``delivered``/``running`` are implied
    (``delivered == total``, ``running == 0``) and left for the frontend to fill,
    exactly as the regex path does."""
    return {
        "kind": "batch",
        "final": True,
        "chunk": chunk,
        "chunks": chunks,
        "ok": ok,
        "failed": failed,
        "stopped": stopped,
        "total": total,
    }


def wave_chunk_meta(
    *,
    chunk: int,
    chunks: int,
    delivered: int,
    total: int,
    running: int,
) -> dict:
    """Structured facts for a MID-wave digest chunk — progress, no tallies."""
    return {
        "kind": "batch",
        "final": False,
        "chunk": chunk,
        "chunks": chunks,
        "delivered": delivered,
        "total": total,
        "running": running,
    }
