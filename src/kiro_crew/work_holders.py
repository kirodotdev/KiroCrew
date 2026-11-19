"""What a restart is waiting on, by name.

The in-flight census (``GatewayOrchestrator._in_flight_work_census``) and the
queue and approval probes of :mod:`kiro_crew.quiescence` describe every item
they count as a :class:`Holder`; the counts a restart is judged on are sums over
those same holders, so a number and the names behind it cannot disagree.

A holder's ``name`` is an identifier the gateway already keys work by: a
dashboard slot key, a channel session key, a cron job name, a workflow run id,
a channel name. It is never message text, a session title or a path, and it is
cleaned for a terminal and redacted where it is made (:func:`clean_name`). Names
identify other sessions' work, so they are for the host only: see
``Quiescence.named_holders``. Work that is
counted but has no identifier (sub-agents, callbacks) is a holder with an empty
name and a ``count``.

This module imports nothing from the gateway, so the CLI renders the same
sentence from the payload a gateway answered.
"""

from __future__ import annotations

import time
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from kiro_crew.security import redact
from kiro_crew.terminal_safe import safe_terminal_line

TURN = "turn"
SCRIPT = "script"
WORKFLOW = "workflow"
SUBAGENT = "subagent"
CHANNEL_HANDLER = "channel_handler"
QUEUED = "queued"
APPROVAL = "approval"
#: A source that could not be read; ``name`` says which source, never more.
UNREADABLE = "unreadable"
CALLBACK = "callback"
REFUSAL_WRITE = "refusal_write"
UPDATE_BUILD = "update_build"
TASK_RUNNER = "task_runner"

#: How many holders a payload or a sentence lists before ``+N more``.
LIST_CAP = 5

#: The longest name a holder carries.
NAME_MAX_CHARS = 64

# The wall clock a cron run's age is read on, named so a test can drive it.
_now = time.time


@dataclass(frozen=True)
class Holder:
    """One thing a restart would cut: what kind, which one, and for how long."""

    kind: str
    name: str = ""
    #: Seconds it has been running, or ``None`` where no start was recorded.
    since_secs: float | None = None
    #: Census items behind this holder (more than one for unnamed work).
    count: int = 1
    #: Whether the census counts it as a turn (``False``: background).
    turn: bool = False

    def __post_init__(self) -> None:
        # The one place a holder's text is made: every census site, and so the
        # log, the refusal sentence and the HTTP answers, gets it cleaned.
        object.__setattr__(self, "kind", clean_name(self.kind))
        object.__setattr__(self, "name", clean_name(self.name))


def clean_name(value: object) -> str:
    """*value* as one printable line of at most :data:`NAME_MAX_CHARS` characters.

    A name can be text an agent or a remote party chose (a cron job name, a
    session key), and it is printed to a terminal and written to the log: escape
    sequences, control characters and format characters (the bidi overrides
    among them) are dropped, any run of whitespace is one space, and a credential
    or a credential inside a URL is masked as in any other surfaced text.
    """
    if value is None:
        return ""
    text = safe_terminal_line(" ".join(str(value).split()))
    if not text.isascii():
        text = "".join(c for c in text if unicodedata.category(c) != "Cf")
    # Before the cut, so a credential is judged whole and never shown in part.
    text = " ".join(redact(text).split())
    if len(text) > NAME_MAX_CHARS:
        text = text[: NAME_MAX_CHARS - 3] + "..."
    return text


def total(holders: Iterable[Holder]) -> int:
    """The census items behind *holders*."""
    return sum(h.count for h in holders)


def counts(holders: Iterable[Holder]) -> tuple[int, int]:
    """``(turns, background)`` over *holders*."""
    turns = 0
    background = 0
    for holder in holders:
        if holder.turn:
            turns += holder.count
        else:
            background += holder.count
    return turns, background


def summarize(holders: Iterable[Holder]) -> tuple[Holder, ...]:
    """One holder per ``(kind, name)``, longest running first.

    A chat turn is counted twice by the census (its provider and its task), and
    unnamed work arrives as several counts of one kind: both read as one line.
    Holders with no recorded start follow those with one, in census order.
    """
    merged: dict[tuple[str, str], Holder] = {}
    for holder in holders:
        key = (holder.kind, holder.name)
        seen = merged.get(key)
        if seen is None:
            merged[key] = holder
            continue
        ages = [a for a in (seen.since_secs, holder.since_secs) if a is not None]
        merged[key] = Holder(
            kind=holder.kind,
            name=holder.name,
            since_secs=max(ages) if ages else None,
            count=seen.count + holder.count,
            turn=seen.turn or holder.turn,
        )
    timed = sorted(
        (h for h in merged.values() if h.since_secs is not None),
        key=lambda h: -(h.since_secs or 0.0),
    )
    return (*timed, *(h for h in merged.values() if h.since_secs is None))


def payload(holders: Iterable[Holder]) -> tuple[list[dict[str, object]], int]:
    """The first :data:`LIST_CAP` holders as JSON entries, and how many were left out."""
    listed = list(holders)
    entries: list[dict[str, object]] = [
        {
            "kind": h.kind,
            "name": h.name,
            "since_secs": None if h.since_secs is None else int(h.since_secs),
            "count": h.count,
        }
        for h in listed[:LIST_CAP]
    ]
    return entries, max(0, len(listed) - LIST_CAP)


def _age(secs: object) -> str:
    if isinstance(secs, bool) or not isinstance(secs, (int, float)) or secs < 0:
        return ""
    if secs < 60:
        return f"{int(secs)} s"
    if secs < 3600:
        return f"{int(secs // 60)} min"
    return f"{int(secs // 3600)} h"


def describe(entries: object, more: object = 0) -> str:
    """``chat-95 turn (4 min), nightly script (21 s), subagent x3, +3 more``.

    Reads the entries of :func:`payload`, as a gateway answered them: anything
    that is not an entry is skipped, so an older or foreign answer reads as "".
    """
    parts: list[str] = []
    for entry in entries if isinstance(entries, (list, tuple)) else ():
        if not isinstance(entry, dict) or not isinstance(entry.get("kind"), str):
            continue
        label = clean_name(entry["kind"]).replace("_", " ")
        name = clean_name(entry.get("name"))
        count = entry.get("count")
        text = f"{name} {label}" if name else label
        if not name and isinstance(count, int) and not isinstance(count, bool) and count > 1:
            text += f" x{count}"
        age = _age(entry.get("since_secs"))
        parts.append(f"{text} ({age})" if age else text)
    if not parts:
        return ""
    if isinstance(more, int) and not isinstance(more, bool) and more > 0:
        parts.append(f"+{more} more")
    return ", ".join(parts)


def script_holder(cron_svc: Any, job_id: str) -> Holder:
    """The holder for running script or command job *job_id*.

    Its name and start are the cron service's own (``get_job``, cache-only, and
    ``running_since``). A run the service cannot describe (no service, a job
    removed mid-run, a claim already taken) is still a holder, by kind only.
    """
    name = ""
    age: float | None = None
    try:
        job_name = getattr(cron_svc.get_job(job_id), "name", "")
        name = job_name if isinstance(job_name, str) else ""
        started = cron_svc.running_since(job_id)
        if isinstance(started, (int, float)) and not isinstance(started, bool):
            age = max(0.0, _now() - started)
    except Exception:
        # No cron service, or one that cannot answer: the run still counts.
        pass
    return Holder(SCRIPT, name, age)


def slot_age(slot: Any) -> float | None:
    """Seconds the dashboard slot's current task has run, if it was stamped."""
    started = getattr(slot, "_task_started_at", None)
    if isinstance(started, bool) or not isinstance(started, (int, float)):
        return None
    # circular import: the dashboard package imports the gateway at load.
    from kiro_crew.dashboard import run_clock

    return max(0.0, run_clock.now() - started)


def slot_names(state: Any) -> dict[str, str]:
    """``session key -> slot key`` for every dashboard slot, or ``{}``.

    A slot's turns run on the session ``effective_session_key`` names
    (``dashboard:<slot>``, or the channel's own key for a channel-born slot),
    so work the gateway files under that session key is the slot's work and is
    named by the slot.
    """
    slots = getattr(state, "_slots", None)
    if not isinstance(slots, dict):
        return {}
    # circular import: the dashboard package imports the gateway at load.
    from kiro_crew.dashboard.chat_utils import effective_session_key

    names: dict[str, str] = {}
    for slot_key, slot in list(slots.items()):
        try:
            names[effective_session_key(slot)] = str(slot_key)
        except Exception:
            # A slot with no key to derive from keeps its own name only.
            continue
    return names


def session_name(session_key: object, slots: dict[str, str]) -> str:
    """The name a session's work is listed under: its slot's, else its own key."""
    key = str(session_key)
    return slots.get(key, key)


def provider_names(sessions: Any, slots: dict[str, str]) -> dict[int, str]:
    """``id(provider) -> name`` from the allocation table, or ``{}``."""
    try:
        table = sessions._allocation_boundary()._sessions
        return {id(s.provider): session_name(k, slots) for k, s in list(table.items())}
    except Exception:
        return {}
