"""Tool declarations of the ``ledger`` domain of ``kirocrew-core``.

The handlers are in :mod:`kiro_crew.mcp_tools.ledger`, which reads every name here as
its own. This module imports none of them (see the package docstring).
"""

from __future__ import annotations

from typing import Any

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
                "is re-injected into monitor-loop cycles and into the first "
                "turn after a context compaction. Write what a cold "
                "resume needs: `next` as a concrete intent (not a status "
                "word), approaches you tried and rejected, and artifact "
                "pointers (worktree, branch, PR). Fields you omit keep their "
                "stored values — partial updates are the norm. Changing "
                f"`phase` REQUIRES an `event` (+ `event_kind`); phases "
                f"{terminal} mark the workstream finished and stop the "
                "ledger's snapshot injection. Use it for genuinely "
                "long-horizon work (babysit loops, goal loops, multi-wake "
                "tasks), not for single-turn requests. Record from the parent "
                "session: a subagent has no session identity of its own and is "
                "refused."
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
