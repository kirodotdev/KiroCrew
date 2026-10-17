"""A crewmate's recap of the work it holds, for its cold-start welcome.

When the user opens a crewmate's chat after a long idle (or for the first
time), the Crewmates page greets them with what this crewmate was asked to do
before: goals it left open, then its most recent sessions. The page decides
WHEN to greet (``mateGreeting.ts``, the one greeting-on-open seam); this module
only says WHAT there is to recap.

Deterministic and cheap: ledger folds and the session catalog the Sessions list
already reads, no model call. The functions are pure over their inputs, so the
route that gathers those inputs is the only place that does IO.
"""

from __future__ import annotations

from typing import Any, Iterable

from kiro_crew.external_text import redact_external_text
from kiro_crew.members import is_member_session_key

#: Recent sessions a recap lists. A recap, not a history view.
MAX_RECENT = 3

#: Characters kept of one goal or title. The recap is one line per item; the
#: full text is in the session itself.
MAX_ITEM_CHARS = 120


def _clip(text: Any) -> str:
    """One display line: redacted, whitespace folded, redacted again, then capped.

    The first pass runs on the text as written: some detectors (a private key
    with no END line) key on its line breaks, which folding removes. The second
    pass catches a secret the fold joined into one token.
    """
    if not isinstance(text, str):
        return ""
    line = redact_external_text(" ".join(redact_external_text(text).split()))
    if len(line) > MAX_ITEM_CHARS:
        line = line[: MAX_ITEM_CHARS - 1].rstrip() + "…"
    return line


def open_goal(ledger_state: dict[str, Any]) -> dict[str, str] | None:
    """The ledger's goal when it is still open (set, and not done/abandoned)."""
    from kiro_crew.session_ledger import TERMINAL_PHASES

    goal = _clip(ledger_state.get("goal"))
    if not goal or ledger_state.get("phase") in TERMINAL_PHASES:
        return None
    return {"goal": goal, "next": _clip(ledger_state.get("next"))}


def recent_sessions(sessions: Iterable[dict[str, Any]], member: str) -> list[dict[str, Any]]:
    """The newest catalog rows that ran as *member*, member threads excluded.

    A row whose title is only its key says nothing a user would recognise, and
    an incognito row is not listed anywhere else either, so both are skipped.
    """
    rows = [
        s
        for s in sessions
        if s.get("agent") == member
        and not is_member_session_key(str(s.get("key", "")))
        and s.get("memory_mode", "persistent") == "persistent"
        and s.get("title")
        and s.get("title") != s.get("key")
    ]
    rows.sort(key=lambda s: float(s.get("modified") or 0), reverse=True)
    return rows[:MAX_RECENT]


def recap(
    thread_ledger: dict[str, Any],
    recent: list[dict[str, Any]],
    recent_ledgers: list[dict[str, Any]],
) -> dict[str, Any]:
    """Paused goals first, then recent sessions.

    *recent_ledgers* holds one ledger state per *recent* row, in order. A
    recent session whose own goal is still open is listed as paused (its goal
    names the task better than its title), so one task never appears twice.
    """
    paused: list[dict[str, str]] = []
    own = open_goal(thread_ledger)
    if own:
        paused.append(own)
    tasks: list[dict[str, Any]] = []
    for row, ledger in zip(recent, recent_ledgers):
        goal = open_goal(ledger)
        if goal:
            paused.append(goal)
            continue
        title = _clip(row.get("title"))
        if title:
            tasks.append({"title": title, "ts": float(row.get("modified") or 0)})
    return {"paused": paused, "recent": tasks}
