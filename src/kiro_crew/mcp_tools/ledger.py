"""Session work-ledger tools — durable loop state on disk, not in context.

``schemas()`` returns the advertisement half of each tool; ``HANDLERS`` maps
names to behavior (see ``learn.py`` — this module follows the same template,
including reaching shared plumbing as ``mcp_core`` attributes so tests that
rebind them still intercept the handlers).

The tools carry NO session/slot argument: the backend resolves the calling
session's identity from the request and each session can only touch its own
ledger. Raw HTTP without a recognized session identity is refused (403).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from kiro_crew import mcp_core
from kiro_crew.session_ledger import EVENT_KINDS, TERMINAL_PHASES


def schemas() -> list[dict[str, Any]]:
    """Descriptors for the session-ledger tools."""
    kinds = ", ".join(sorted(EVENT_KINDS))
    terminal = ", ".join(sorted(TERMINAL_PHASES))
    return [
        {
            "name": "session_ledger_read",
            "description": (
                "Read THIS session's durable work ledger: the state record "
                "(goal, phase, next step, tried/rejected approaches, artifact "
                "pointers) plus the recent event tail. The ledger lives on "
                "disk and survives context compaction — treat it as "
                "authoritative over your memory of prior cycles. Call it when "
                "resuming long-running work (a monitor loop cycle, a return "
                "after compaction) before re-deriving state from the "
                "conversation."
            ),
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "session_ledger_record",
            "description": (
                "Record one step of long-running work into THIS session's "
                "durable ledger, so the state survives context compaction and "
                "is re-injected into monitor-loop cycles. Write what a cold "
                "resume needs: `next` as a concrete intent (not a status "
                "word), approaches you tried and rejected, and artifact "
                "pointers (worktree, branch, PR). Fields you omit keep their "
                "stored values — partial updates are the norm. Changing "
                f"`phase` REQUIRES an `event` (+ `event_kind`); phases "
                f"{terminal} mark the workstream finished and stop the "
                "ledger's snapshot injection. Use it for genuinely "
                "long-horizon work (babysit loops, goal loops, multi-wake "
                "tasks), not for single-turn requests."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "goal": {
                        "type": "string",
                        "maxLength": 2000,
                        "description": "The binding objective of this workstream.",
                    },
                    "phase": {
                        "type": "string",
                        "maxLength": 128,
                        "description": (
                            "Current phase (free-form, e.g. 'implementing', "
                            f"'awaiting-ci'; {terminal} are terminal). "
                            "REQUIRES `event` AND a recognized `event_kind` "
                            "in the same call — a phase never moves without "
                            "a logged, classified reason."
                        ),
                    },
                    "next": {
                        "type": "string",
                        "maxLength": 2000,
                        "description": (
                            "The resumable intent — the concrete next step, " "not a status word."
                        ),
                    },
                    "tried_approach": {
                        "type": "string",
                        "maxLength": 2000,
                        "description": (
                            "An approach you tried and rejected — appended, so "
                            "a resumed cycle does not re-walk it."
                        ),
                    },
                    "tried_rejected_because": {
                        "type": "string",
                        "maxLength": 2000,
                        "description": "Why that approach was rejected.",
                    },
                    "artifacts": {
                        "type": "object",
                        "additionalProperties": {"type": "string"},
                        "description": (
                            "String-to-string pointers to where the work "
                            "lives (worktree, branch, pr, paths). Merged into "
                            "the stored map."
                        ),
                    },
                    "event": {
                        "type": "string",
                        "maxLength": 2000,
                        "description": (
                            "One-line progress note appended to the event log. "
                            "REQUIRED when `phase` changes."
                        ),
                    },
                    "event_kind": {
                        "type": "string",
                        "maxLength": 32,
                        "description": f"Kind of step this records: {kinds}.",
                    },
                },
            },
        },
    ]


def _strict_session_key() -> tuple[str, str]:
    """Resolve the calling session strictly, refusing PID-walked identities.

    Returns ``(key, "")`` or ``("", error)``. The lenient default resolver
    includes a ``/proc`` ancestor walk, and a subagent lives under its parent
    slot's process tree — the walk would silently resolve to the PARENT
    session, disclosing or overwriting the parent's ledger. Resolution is
    delegated to :func:`mcp_core.require_strict_session_key`, the shared
    fail-closed gate for reflexive tools (this helper is where that gate was
    promoted from), and the verified key is passed explicitly to the transport
    so the value that was checked is the value that is used.
    """
    return mcp_core.require_strict_session_key(
        "Error: this session's identity could not be verified strictly, "
        "so the ledger is not reachable from here. Subagents inherit no "
        "session identity of their own — record ledger updates from the "
        "parent session instead."
    )


def session_ledger_read(name: str, args: dict[str, Any]) -> str:
    sk, err = _strict_session_key()
    if err:
        return err
    d = mcp_core._get("/api/session-ledger", session_key=sk)
    api_err = d.get("error")
    if api_err:
        return f"Error: {api_err}"
    state = d.get("state") or {}
    events = d.get("events") or []
    if not any(state.get(k) for k in ("goal", "phase", "next", "tried", "artifacts")):
        return (
            "This session has no work ledger yet. Use session_ledger_record "
            "to start one when doing long-horizon work."
        )
    return json.dumps({"state": state, "events": events}, indent=2)


def session_ledger_record(name: str, args: dict[str, Any]) -> str:
    payload = {
        k: v
        for k, v in args.items()
        if k
        in (
            "goal",
            "phase",
            "next",
            "tried_approach",
            "tried_rejected_because",
            "artifacts",
            "event",
            "event_kind",
        )
        and v is not None
    }
    if not payload:
        return "Error: nothing to record — pass at least one field"
    sk, err = _strict_session_key()
    if err:
        return err
    d = mcp_core._post("/api/session-ledger/record", payload, session_key=sk)
    api_err = d.get("error")
    if api_err:
        return f"Error: {api_err}"
    state = d.get("state") or {}
    phase = state.get("phase") or "(unset)"
    return f"Recorded. phase={phase} next={state.get('next') or '(unset)'}"


HANDLERS: dict[str, Callable[[str, dict[str, Any]], str]] = {
    "session_ledger_read": session_ledger_read,
    "session_ledger_record": session_ledger_record,
}
