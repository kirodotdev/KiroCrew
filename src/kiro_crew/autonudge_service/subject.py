"""Which pull request a loop is about, and a digest of one reading of it.

:func:`infer_subject` is the ONE resolver of a loop's watched subject -- its judge
brief's target list first, then its instruction -- and :func:`infer_monitor` builds the
monitor a gated loop is armed with from that answer. The arm, the retarget, the tick,
the post-poll rebinding check and the terminal revalidation all ask these, so the
monitor and the judge's collector cannot come to disagree about what is watched. The
reading helpers (:func:`_pr_observation_of`, :func:`_pr_facts_digest`) and the bounds
on the judged baseline sit beside them because the gate and the loader share them.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Mapping, Sequence

from kiro_crew.monitoring.models import MonitorCreationSurface, MonitorState
from kiro_crew.monitoring.registry import REVIEW_READY
from kiro_crew.probes import targets

# The service's own logger: callers and tests filter on it by name.
logger = logging.getLogger("kiro_crew.autonudge")


def _pr_observation_of(probe: Any) -> Any | None:
    """The reading a fetcher published on itself this tick, or ``None``.

    Duck-typed rather than imported, so this module names no probe class. A probe
    is asked only for an object that can say whether it reached its subject; a
    build whose probe publishes nothing answers ``None`` and every caller here
    treats that as "not read", which fires.
    """
    observation = getattr(probe, "observation", None)
    if observation is None:
        return None
    for attribute in ("reached", "is_terminal", "merged", "state"):
        if not hasattr(observation, attribute):
            return None
    return observation


#: Keys on a published reading that describe the TICK rather than the subject.
#: ``observed_at`` is the clock, a remark's ``age_s`` grows on every tick, and
#: ``first_seen_this_tick`` is the delta the core stamps for the judge. A digest
#: carrying any of them would call every reading a change, and the comparison it
#: exists for would never hold once.
_PR_FACTS_TICK_KEYS = ("observed_at", "age_s", "first_seen_this_tick")


#: How many remark ids the judged baseline retains. The reading itself is already
#: bounded to a fetch horizon, so this is the ceiling that keeps a long-lived watch
#: from growing one unbounded list in a persisted record. An id pushed out of it
#: reads as new again, which fires -- the safe direction.
_JUDGE_PR_SEEN_REMARKS = 200


#: Width the stored baseline digest is held to on LOAD. The value this build writes is
#: a 16-character hex digest; the bound is what holds when the row came off a store an
#: agent shell can write, where a longer string would otherwise be retained and
#: re-serialized on every persist. A clipped digest simply fails to match, which fires.
_JUDGE_PR_SEEN_DIGEST_CHARS = 64


def _pr_facts_digest(facts: Mapping[str, Any]) -> str:
    """A stable digest of WHAT a pull-request reading says, ignoring WHEN it was read.

    Two readings with the same digest describe the same subject state, so no criterion
    ABOUT the subject can have become true between them. ``observation_status`` is part
    of it, so a reading that went short of whole is a change rather than a match.

    Returns ``""`` when the facts cannot be rendered, which no caller may read as a
    match: an unanswerable comparison has to resolve toward spending the turn.
    """

    def strip(value: object) -> object:
        if isinstance(value, Mapping):
            return {
                key: strip(inner)
                for key, inner in sorted(value.items())
                if key not in _PR_FACTS_TICK_KEYS
            }
        if isinstance(value, (list, tuple)):
            return [strip(item) for item in value]
        return value

    try:
        rendered = json.dumps(strip(facts), sort_keys=True, default=str)
    except Exception:
        logger.debug("AutoNudge: could not digest a pull-request reading", exc_info=True)
        return ""
    return hashlib.sha256(rendered.encode("utf-8", "replace")).hexdigest()[:16]


def _transcript_key(slot_key: str) -> str:
    """The transcript key of the session a loop is bound to.

    The inverse of ``autonudge.binding_key_for``: a dashboard loop binds on the BARE
    slot key while its transcript is logged under ``dashboard:<slot>``; a channel
    loop binds on the session key itself.
    """
    from kiro_crew.autonudge_service.model import is_channel_key

    return slot_key if is_channel_key(slot_key) else f"dashboard:{slot_key}"


def _session_texts(slot_key: str) -> list[str]:
    """Every message body in *slot_key*'s own transcript. Never raises.

    Read through ``derive_messages``, the guarded read: the rows feed a decision
    rather than the transcript's own rendering, so a restricted transcript is
    withheld and the loop simply stays on its timer.
    """
    try:
        from kiro_crew.history import ConversationLog

        rows = ConversationLog().derive_messages(_transcript_key(slot_key))
    except Exception:
        logger.debug("AutoNudge: could not read the session log for %s", slot_key, exc_info=True)
        return []
    return [
        row["content"]
        for row in rows
        if isinstance(row, Mapping) and isinstance(row.get("content"), str)
    ]


def needs_session_texts(message: str, slot_key: str, watch: str = "") -> bool:
    """Whether deciding *message*'s subject needs its session's log at all.

    Only an instruction naming its pull request by number alone does, and only when
    there is a session to read and the watch is not the work ledger.
    """
    if str(watch or "").strip() == targets.WORK_LEDGER:
        return False
    if not str(slot_key or "").strip():
        return False
    return targets.bare_pull_request_number(message) is not None


def read_session_texts(message: str, slot_key: str, watch: str = "") -> list[str] | None:
    """The session-log bodies :func:`infer_subject` needs for *message*, or ``None``.

    BLOCKING: it reads the whole transcript. An async caller runs it with
    ``asyncio.to_thread`` and passes the result as ``session_texts``; inference itself
    never reads a transcript, so no path through it can block the event loop.
    """
    if not needs_session_texts(message, slot_key, watch):
        return None
    return _session_texts(str(slot_key).strip())


def _resolve_instruction(
    message: str,
    slot_key: str,
    watch: str,
    monitor_target: str | None,
    session_texts: Sequence[str] | None = None,
) -> "targets.Target | None":
    """:func:`targets.infer` for *message*, with a context for a bare ``PR <number>``.

    A loop naming its pull request only as ``PR <number>`` takes the repository from
    its OWN session's log -- a full URL or ``owner/name#<number>`` with that number --
    and when the log names none, or names two, no repository is guessed and the loop
    keeps its timer.

    The log is consulted only when the subject is being DECIDED: on arm and on
    retarget, where *monitor_target* is ``None``, and only as *session_texts* the
    caller already read off the event loop with :func:`read_session_texts`. Without
    them a bare number resolves nothing. A stored loop passes the target its monitor
    was bound to instead, which is the arm's own answer persisted, so the binding
    cannot drift as the log grows. A monitor naming a different number resolves
    nothing, which fires.
    """
    if targets.bare_pull_request_number(message) is None:
        return targets.infer(message, watch=watch, slot_key=slot_key)
    if monitor_target is not None:
        stored = [monitor_target] if monitor_target else []
        return targets.infer(message, watch=watch, slot_key=slot_key, context=lambda: stored)
    if session_texts is None:
        return targets.infer(message, watch=watch, slot_key=slot_key)
    texts = list(session_texts)
    resolved = targets.infer(message, watch=watch, slot_key=slot_key, context=lambda: texts)
    if resolved is not None:
        logger.info(
            "AutoNudge: a bare pull-request number resolved to %s from the session's own log",
            resolved.subject,
        )
    return resolved


def _judge_pr_targets(judge: Mapping[str, Any] | None) -> list[str]:
    """The pull-request targets a judge brief names. Never raises.

    Imported lazily and wrapped, for the reason every other judge call in this
    module is: the brief is an optional feature, and a loop must still arm on a
    build where reading it fails.
    """
    if not isinstance(judge, Mapping) or not judge:
        return []
    try:
        from kiro_crew import autonudge_judge as _judge

        return _judge.pr_targets_of(judge)
    except Exception:
        logger.debug("AutoNudge: could not read the judge brief's targets", exc_info=True)
        return []


def infer_subject(
    message: str,
    judge: Mapping[str, Any] | None = None,
    *,
    watch: str = "",
    slot_key: str = "",
    monitor_target: str | None = None,
    session_texts: Sequence[str] | None = None,
) -> "targets.Target | None":
    """WHICH pull request a loop is about: its judge brief first, then its instruction.

    One function for all five callers (arm, retarget, probe config, post-poll
    rebinding check, terminal revalidation), so the monitor and the reader never
    disagree about what is watched.

    The brief's ``targets`` list is read FIRST because
    :func:`~kiro_crew.autonudge_judge.parse_targets` does: a list narrows what the
    collector asks about, and a monitor on a different pull request has its reading
    dropped every tick, so the judge never sees the comment bodies.

    That is a precedence, not an override. A single-PR brief supplies the subject
    only when the instruction names NO pull request. Otherwise the instruction
    decides: its own pull request, or ``None`` for a shorthand-only or two-subject
    text. A brief naming several pull requests selects none, since a loop holds
    one monitor. Falling back rather than refusing keeps merge-retirement for the
    "blocked on #7" shape (instruction names its PR, brief names the blocker).

    Every candidate goes through :func:`targets.infer`, which refuses a shorthand
    wherever it comes from (``#123`` may be an issue; a slug has no host), so a brief
    naming ``owner/name#123`` selects nothing, as the collector drops it.

    For the INSTRUCTION only, a bare ``PR <number>`` takes its repository from the
    session log via :func:`targets.resolve_bare` -- see :func:`_resolve_instruction`,
    which also explains *monitor_target*.
    """
    listed: list["targets.Target"] = []
    if str(watch or "").strip() == targets.WORK_LEDGER:
        # A work-ledger watch is bound to the conductor's OWN session, resolved from
        # ``slot_key`` -- never from a pull request. The judge brief may still name a
        # PR as a collection target, but that must NOT retarget the watch: the block
        # below would read a one-entry brief as the subject and arm the loop on the PR
        # instead of the ledger, silently, with no error to the caller. Resolve the
        # watch's own subject here, before the brief is consulted at all.
        return targets.infer(message, watch=watch, slot_key=slot_key)
    for entry in _judge_pr_targets(judge):
        found = targets.infer(entry)
        if found is None:
            continue
        identity = (found.kind, found.subject, found.host_key)
        if all(identity != (other.kind, other.subject, other.host_key) for other in listed):
            listed.append(found)
    from_message = _resolve_instruction(message, slot_key, watch, monitor_target, session_texts)
    if len(listed) == 1:
        only = listed[0]
        if not targets.names_pull_request(message):
            return only
        if from_message is not None and (
            from_message.kind,
            from_message.subject,
            from_message.host_key,
        ) == (only.kind, only.subject, only.host_key):
            return only
        # The instruction names a pull request, so it decides (see the docstring).
        # ``from_message`` is ``None`` for a hostless shorthand or two subjects; the
        # brief must not resolve an ambiguity ``targets.infer`` refuses, or the
        # blocker would become the watched subject.
        logger.info(
            "AutoNudge: a judge brief and an instruction disagree about the pull "
            "request (brief names %s), so the instruction decides the watched subject",
            only.subject,
        )
        return from_message
    if listed:
        logger.info(
            "AutoNudge: a judge brief names %d pull requests, so the instruction decides "
            "the watched subject instead",
            len(listed),
        )
    return from_message


def loop_subject(loop: Any) -> "targets.Target | None":
    """The subject one stored loop is about, from that loop's own two strings."""
    from kiro_crew import autonudge_judge as _judge

    _monitor = getattr(loop, "monitor", None)
    _watch_kind = getattr(_monitor, "kind", "") if _monitor is not None else ""
    return infer_subject(
        str(getattr(loop, "message", "") or ""),
        _judge.spec_of(loop),
        watch=_watch_kind or "",
        slot_key=getattr(loop, "slot_key", "") or "",
        # A STORED loop: its subject was decided when it was armed, so a bare number
        # is resolved against the monitor that decision bound, never the log.
        monitor_target=_judge.watched_pr_subject(loop),
    )


def infer_monitor(
    message: str,
    now: float,
    *,
    creation_surface: MonitorCreationSurface = MonitorCreationSurface.UNKNOWN,
    judge: Mapping[str, Any] | None = None,
    watch: str = "",
    slot_key: str = "",
    session_texts: Sequence[str] | None = None,
) -> MonitorState | None:
    """Build a monitor for this loop's subject, or ``None`` to stay ungated.

    ``None`` is the common, safe answer: a loop watching something with no probe
    (a deployment, a ticket, a file) stays a plain timer. Only a loop naming ONE
    observable subject becomes a gated monitor.

    Public so the arming surface's acknowledgement reports this same decision; a
    subject that cannot form a valid monitor is a ``None`` reason outside
    :func:`infer_subject`, and an ack derived from the target alone could claim a
    gate the loop never got. *judge* is passed because on the arming path the loop
    does not exist yet.

    Budgets stay at their defaults and are NOT enforced here: the wake cap defaults
    to unlimited, and the runtime, token and provider-error bounds are enforced by
    the decision controller, which has the probe history. A bound applied here
    without it would stop working watches early.
    """
    target = infer_subject(
        message, judge, watch=watch, slot_key=slot_key, session_texts=session_texts
    )
    if target is None:
        return None
    try:
        return MonitorState(
            kind=target.kind,
            target=target.subject,
            objective=REVIEW_READY,
            created_ts=now,
            creation_surface=creation_surface,
        )
    except ValueError:
        # A subject that cannot form a valid monitor is not a reason to refuse
        # the loop the caller asked for. Arm it ungated.
        logger.warning("AutoNudge: inferred target %r rejected by MonitorState", target.subject)
        return None
