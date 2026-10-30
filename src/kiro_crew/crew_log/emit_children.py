"""Child facts: a subagent this session dispatched was spawned, steered, dismissed or closed.

Each entry lands on the dispatching session's log. The turn that asked is captured where the
spawn was accepted and passed in, because the child starts, steers and finishes after that turn
has ended; :func:`dismiss_child` finds the dispatching session through the child-origin pin
:mod:`kiro_crew.crew_log.emit_runtime` keeps.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any

from kiro_crew.crew_log.emit_runtime import _write, child_origin
from kiro_crew.crew_log.emit_state import _SRC_GATEWAY, enabled
from kiro_crew.crew_log.emit_text import _MAX_SHORT_TEXT, _clip, _safe_text


def on_subagent_spawned(
    session_id: str,
    turn: int,
    *,
    agent_id: str,
    attempt: int = 0,
    agent: str = "",
    model: str = "",
    task: str = "",
    scope: Any = None,
) -> None:
    """Record a child this session dispatched.

    ``turn`` is the turn that ASKED, captured where the spawn was accepted --
    which runs inside the parent's turn, since a spawn arrives as one of its tool
    calls. It is passed in rather than read here on purpose: the child starts,
    steers and finishes long after that turn has ended, and every later entry
    about this child reuses the captured ordinal instead of asking what turn the
    parent is on now.

    No ``ref`` into the child's log. The schema describes one, and a child that
    had a crew log would deserve it, but no subagent code path opens one: the only
    site that creates a session's crew log is the dashboard turn path, and a subagent
    run does not go through it. A ``ref`` written now would cite a file that does
    not exist, which a reader cannot distinguish from one that was deleted. It
    becomes writable, unchanged, the day subagent sessions get crew logs of their
    own.

    ``turn`` is ABSENT when no turn asked, the same way :func:`on_model_selected`
    omits its own. A spawn does not always arrive inside a model turn -- a slash
    command, a cron and a hook all dispatch children of a session with nothing
    running -- and turns are numbered from one, so a literal ``0`` would name a
    turn that never existed and match no ``turn/started``. The child is still
    recorded: it is a real child of that session, and losing it to keep a field
    populated would be the worse trade.

    ``attempt`` is which try at ``turn`` asked: a regenerate or a rewind reruns the
    same ordinal, and ``(turn, attempt)`` is what ``turn/started`` is grouped on. It is
    written only beside a turn and only when observed, so ABSENT means unknown. It is
    not the "attempt 1" that an absent ``turn/started`` attempt means, because the
    attempt is derived on the writer thread and a dispatch early in a turn can be
    pinned before it is.

    ``task`` is what the child was asked to do, redacted and clipped on the same
    terms as ``plan/updated``'s item text -- the other place this module records
    text a person wrote. It is the one thing a reader needs to tell two children
    apart that is not derivable from anything else in the entry, and a surface
    rebuilding a child's card after the dispatching process is gone has nowhere
    else to read it from.

    It is written only when non-empty, so a dispatch that carried no task text
    leaves the field ABSENT rather than present-and-empty. The two are different
    facts to a reader: absent is "this log does not say", which is also what every
    log written before the field reads as, and a surface draws no task line for
    it. An empty string would claim the dispatch asked for nothing.
    """
    data: dict[str, Any] = {"agent_id": agent_id}
    if turn:
        data["turn"] = int(turn)
        # Which try at that ordinal asked. Written even at 1, unlike turn/started,
        # because absent here means the attempt was not observed.
        if attempt > 0:
            data["attempt"] = int(attempt)
    if agent:
        data["agent"] = agent
    if model:
        data["model"] = model
    asked = _clip(_safe_text(task), _MAX_SHORT_TEXT)
    if asked:
        data["task"] = asked
    if isinstance(scope, dict):
        data["scope"] = {
            "memory": bool(scope.get("memory")),
            "lessons": bool(scope.get("lessons")),
            "project": bool(scope.get("project")),
        }
    _write(session_id, "subagent/spawned", data, src=_SRC_GATEWAY)


def on_subagent_steered(session_id: str, *, agent_id: str, mode: str = "") -> None:
    """Record a correction sent into a running child.

    Written into the PARENT's log: the parent is what sent it, and the child has
    no crew log to receive it.
    """
    data: dict[str, Any] = {"agent_id": agent_id}
    if mode:
        data["mode"] = mode
    _write(session_id, "subagent/steered", data, src=_SRC_GATEWAY)


def on_subagent_dismissed(
    session_id: str,
    *,
    agent_id: str,
    on_settled: "Callable[[bool], None] | None" = None,
) -> None:
    """Record that the user cleared a child's card from the panel.

    Written into the PARENT's log, like a steer, and for the same reason: the
    child has no crew log of its own and the act belongs to the session the panel
    was showing.

    It lives in the log rather than in a registry beside it because the panel's
    durable half is a FOLD of this log. A dismissal held anywhere else is a second
    record of a fact about this session, and the two are reclaimed on different
    schedules -- which is not hypothetical: the registry that held it was keyed on
    the run's folder at both ends, so a dismissed card came back the moment that
    folder was pruned while the log still carried the child.

    ``on_settled`` is handed True only once the append has COMMITTED. A caller that
    will tell the user the card is gone needs that, because this is the only record
    of the dismissal when the run's folder has already been reclaimed: the queue
    returns as soon as the entry is handed over, so publishing on the handover
    reports a dismissal the next reconnect can undo, with nothing left to explain
    it and nothing for a retry to act on.

    Opens and closes nothing. A dismissal is not an ending, and the child keeps
    whatever outcome its own closer recorded; a user may also clear a card while
    the child is still running, so this can precede any closer.
    """
    _write(
        session_id,
        "subagent/dismissed",
        {"agent_id": agent_id},
        src=_SRC_GATEWAY,
        on_settled=on_settled,
    )


def dismiss_child(agent_id: str, *, on_settled: "Callable[[bool], None] | None" = None) -> str:
    """Record a dismissal against the session this process dispatched *agent_id* from.

    Returns that session's id, or ``""`` when this process cannot name one -- the
    emitter is off, or the spawn pin for this child is gone, which is what a
    gateway restart leaves behind. A caller that must record the dismissal some
    other way reads the empty answer as "nothing was written here".

    The pin rather than a lookup, for the reason it exists: it is the session the
    child's own ``subagent/spawned`` was written to, so the dismissal lands in the
    log that holds the row it is about. :func:`child_origin` is gated on that entry
    having been opened, so a child whose spawn was never recorded answers ``""``
    rather than putting a dismissal in a log with no dispatch to match it.

    ``on_settled`` is passed to :func:`on_subagent_dismissed` and so reports
    whether the append COMMITTED. The returned session id says only that one was
    queued: a caller publishing a dismissal to the user needs the callback, since
    the empty-string answer and a queued-then-dropped append are the same outcome
    from the user's side and only one of them is visible in the return value.
    """
    if not enabled():
        return ""
    session_id, _turn = child_origin(agent_id)
    if not session_id:
        return ""
    on_subagent_dismissed(session_id, agent_id=agent_id, on_settled=on_settled)
    return session_id


def on_subagent_completed(
    session_id: str, *, agent_id: str, duration_ms: int = 0, credits: float = 0.0
) -> None:
    """Close a child that finished its work, and what it cost.

    Only for the ``completed`` outcome. A stopped or failed child closes through
    :func:`on_subagent_failed`, because the runtime's own three-way outcome exists
    precisely to stop consumers reading "no error" as success.

    ``credits`` is the run's own accumulator, cumulative across every attempted
    turn including billed retries that failed before the last one. It is written
    only when positive: a provider that does not bill in credits reports zero
    through the shared ``TurnUsage`` contract, which is indistinguishable at this
    seam from a run that was genuinely free, so writing the zero would present the
    absence of a measurement as a measurement of zero. ``tokens`` stays absent
    throughout -- nothing in the subagent runtime measures them.
    """
    data: dict[str, Any] = {"agent_id": agent_id}
    if duration_ms > 0:
        data["ms"] = int(duration_ms)
    if credits > 0 and math.isfinite(credits):
        data["credits"] = float(credits)
    _write(session_id, "subagent/completed", data, src=_SRC_GATEWAY)


def on_subagent_failed(
    session_id: str,
    *,
    agent_id: str,
    reason: str = "",
    outcome: str = "failed",
    duration_ms: int = 0,
    credits: float = 0.0,
) -> None:
    """Close a child that did NOT finish its work, and what it cost anyway.

    Covers both non-success outcomes, and says which in ``outcome``: a run the
    user stopped is not a failure and must not read as one, but it is also not a
    completion, and the schema offers no third closer. Carrying the runtime's own
    outcome verbatim keeps the two distinguishable without renaming a frozen type
    or leaving the ``subagent/spawned`` entry open forever.

    ``credits`` follows :func:`on_subagent_completed`: positive only, because a
    zero cannot be told apart from an unbilled provider. A run that did not finish
    still billed for the turns it attempted, so this is the one place that charge
    would otherwise be lost. The crash-repair closer passes nothing, which is
    correct -- it knows only that the writer is gone.
    """
    data: dict[str, Any] = {"agent_id": agent_id}
    shown = _clip(_safe_text(reason), _MAX_SHORT_TEXT)
    if shown:
        data["reason"] = shown
    if outcome:
        data["outcome"] = outcome
    if duration_ms > 0:
        data["ms"] = int(duration_ms)
    if credits > 0 and math.isfinite(credits):
        data["credits"] = float(credits)
    _write(session_id, "subagent/failed", data, src=_SRC_GATEWAY)
