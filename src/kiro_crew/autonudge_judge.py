"""Evidence for the ``nudge.wake`` judge, and the one call an auto-nudge tick makes.

The point module (``decisions.points.nudge_wake``) owns the questions, the bounded
state and the mapping. This module owns WHERE the evidence comes from: a watched
session's new transcript rows, a watched pull request's observation, and (later)
work-ledger events. It sits beside ``autonudge`` rather than under ``decisions``
because collecting is a gateway concern -- it reads live slot state -- while the
point stays a pure adapter that a test can drive with literals.

Why the reads arrive as CALLABLES
---------------------------------
``AutoNudgeService`` holds no ``DashboardState``: it is constructed with a data
directory and two callbacks, and the gateway injects closures that reach the rest
(``on_fire``, ``on_monitor_tick``, ``owner_session_id``). The session read is the
same shape, and it has to be, because authorizing it needs state the service
cannot see. So :func:`collect_evidence` takes readers and this module imports no
dashboard module at all -- which is also what lets every function here be tested
without a gateway.

Creator-only, enforced by reuse
-------------------------------
A ``session`` target is read through ``session_control.read_messages``, which
authorizes with ``authorize_target`` before returning a row: deny-by-default, and
SEL-audited on refusal. The judge therefore cannot read a session its owning loop
could not read by hand, and a target that refuses is DROPPED and counted rather
than fetched. Nothing here re-implements that check, because a second copy of an
authorization rule is a second place for it to be wrong.

Assistant rows only
-------------------
``decisions.points.HISTORY_ROLES`` excludes tool output from every other point on
the grounds that it is the largest and least selective text in a transcript and
routinely quotes files nobody mentioned. The same reasoning holds here, and the
evidence a watcher actually needs -- a worker's ``RULING:`` or ``BLOCKED:`` line,
a reviewer's verdict -- is an assistant row. Tool rows are skipped.
"""

from __future__ import annotations

import logging
import math
import re
import secrets
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Mapping, Sequence

from kiro_crew import validation as _validation
from kiro_crew.decisions.log import MAX_VERDICT_ID_CHARS as _MAX_VERDICT_ID_CHARS
from kiro_crew.decisions.points import nudge_wake as point

logger = logging.getLogger(__name__)

#: A dashboard chat-slot key, the spelling a `judge.targets` entry uses for a
#: session. Anchored and bounded: the value is owner-supplied and becomes a
#: `read_messages` target, so it is matched rather than trusted.
SESSION_TARGET_RE = re.compile(r"\Achat-[A-Za-z0-9][A-Za-z0-9-]{0,127}\Z")

#: The same shape, found INSIDE prose, so a loop whose instruction names the
#: sessions it watches needs no second spelling of them in ``judge.targets``. The
#: anchored pattern above still screens every hit, so this only decides where to
#: look, never what counts.
SESSION_TARGET_IN_TEXT_RE = re.compile(r"\bchat-[A-Za-z0-9][A-Za-z0-9-]{0,127}\b")

#: How many transcript rows one target contributes to a single tick. The char
#: budget in the point is what bounds egress; this bounds the READ, so a session
#: that produced a hundred rows between ticks cannot turn one decision into a
#: whole-transcript scan.
MAX_ROWS_PER_TARGET = 12

#: How many targets one loop may name. Bounds the number of authorizations and
#: reads a single tick performs. Spelled once, in ``validation``, where the arming
#: surface refuses an oversized brief: a copy here would be a second number to keep
#: in step, and a disagreement would silently drop targets the arming call accepted.
MAX_TARGETS = _validation.MAX_JUDGE_TARGETS

#: Transcript roles whose text is evidence. Assistant only -- see the module
#: docstring for why tool rows are excluded.
EVIDENCE_ROLES = frozenset({"assistant"})


def parse_targets(spec: Mapping[str, Any] | None, message: str = "") -> list[str]:
    """The targets to collect from: the spec's own list, else what *message* names.

    An explicit ``targets`` list adds or narrows; without one the loop's
    instruction is read the way the PR probe already reads it, so arming a judge
    on a loop that already names a pull request needs no second spelling of the
    subject.

    Only recognised shapes survive: a ``chat-*`` key matching
    :data:`SESSION_TARGET_RE`, or a string a pull-request target can be inferred
    from. Anything else is dropped rather than passed to a reader, because these
    strings come from the owner's tool call.
    """
    raw: list[str] = []
    narrowed = False
    if isinstance(spec, Mapping):
        listed = spec.get("targets")
        if isinstance(listed, (list, tuple)):
            # The owner NARROWED the watch by naming a list, and that stands even if
            # nothing in it survives the filter below.
            narrowed = True
            raw = [str(item) for item in listed if isinstance(item, str)]
    out: list[str] = []
    for item in raw:
        value = item.strip()
        if not value or value in out:
            continue
        if SESSION_TARGET_RE.fullmatch(value) or _pr_subject(value):
            out.append(value)
        if len(out) >= MAX_TARGETS:
            break
    if out or narrowed:
        # ``narrowed`` alone is enough. An owner who named targets and had every one
        # dropped -- a typo, a shape this build does not read -- must not be handed the
        # message's subjects instead: that is a WIDER watch than the one they asked for,
        # reading evidence from a session they never named. Answering no targets costs a
        # turn every interval, because a spec naming none fires rather than suppressing,
        # and spending a turn is the right direction to be wrong in.
        return out
    # No list at all, so read the instruction the way the design says: the targets
    # default to what the MESSAGE names. Both shapes, not just pull requests -- a
    # conductor's instruction names the sessions it patrols, and requiring those to be
    # repeated in ``targets`` would mean a brief that looks armed and watches nothing.
    for match in SESSION_TARGET_IN_TEXT_RE.findall(message or ""):
        if match not in out and SESSION_TARGET_RE.fullmatch(match):
            out.append(match)
        if len(out) >= MAX_TARGETS:
            break
    if _pr_subject(message):
        # The whole message, because pull-request inference reads the original
        # spelling (it carries the host a URL-armed watch is entitled to) rather
        # than a token lifted out of it.
        stripped = message.strip()
        if stripped not in out:
            out.append(stripped)
    return out


def is_session_target(value: str) -> bool:
    """Whether *value* names a dashboard chat slot rather than a pull request."""
    return SESSION_TARGET_RE.fullmatch(value or "") is not None


def session_evidence(
    rows: Sequence[Mapping[str, Any]],
    target: str,
    *,
    now_ts: float | None = None,
) -> list[dict[str, Any]]:
    """New assistant rows from one watched session, newest first, as evidence items.

    Each row's ``ts`` becomes the item's age, so the point can order and drop by
    recency. A row without a usable timestamp reads as age 0 -- treating it as the
    newest thing available, which keeps it in the request under a tight budget
    rather than silently dropping evidence because a row lacked a field.
    """
    clock = point.now() if now_ts is None else now_ts
    items: list[dict[str, Any]] = []
    for row in list(rows)[-MAX_ROWS_PER_TARGET:]:
        if not isinstance(row, Mapping):
            continue
        if str(row.get("role", "") or "") not in EVIDENCE_ROLES:
            continue
        text = row.get("content", "")
        if not isinstance(text, str) or not text.strip():
            continue
        items.append(
            {
                "source": f"session:{target}",
                "kind": point.KIND_TRANSCRIPT_TAIL,
                "age_s": _age_from_ts(row.get("ts"), clock),
                "text": text,
            }
        )
    return items


#: How many check identities one rendered bucket names before it reports a
#: remainder. The canonical object carries up to
#: ``MAX_MONITOR_CHECK_IDENTITIES_PER_BUCKET`` of them, which would spend the whole
#: item budget on lane names; a criterion asks WHETHER a lane is red and usually
#: which one, so a handful plus a count answers it and leaves room for the rest of
#: the reading.
MAX_RENDERED_CHECK_IDENTITIES = 8


def _render_check_bucket(checks: Mapping[str, Any], state: str) -> str:
    """``failed 3 (lint, tests-2, e2e)`` for one bucket, or ``""`` when empty."""
    values = checks.get(state)
    if not isinstance(values, (list, tuple)) or not values:
        return ""
    names = [str(value) for value in values if isinstance(value, str) and value.strip()]
    if not names:
        return f"{state} {len(values)}"
    shown = names[:MAX_RENDERED_CHECK_IDENTITIES]
    remainder = len(names) - len(shown)
    listed = ", ".join(shown)
    if remainder > 0:
        listed = f"{listed}, +{remainder} more"
    return f"{state} {len(names)} ({listed})"


def render_pr_summary(observation: Mapping[str, Any]) -> str:
    """The canonical pull-request facts as one line of prose, or ``""``.

    The rendering lives HERE, in the judge's own collector, because this is its only
    consumer. The monitor's canonical fact object is pinned by full-dict equality
    tests and hashed into the wake fingerprint, so a field added there to serve one
    reader changes what every provider must emit; a reading assembled from the keys
    the probe already published costs nothing and stays local to the reader.

    Only keys the object declares are read, and a key whose value has the wrong type
    is skipped rather than coerced: the judge is better served by a shorter true
    reading than by a field it cannot trust.
    """
    parts: list[str] = []
    for key in ("state", "mergeability", "review_decision", "blocking_review"):
        value = observation.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(f"{key}={value.strip()}")
    draft = observation.get("draft")
    if isinstance(draft, bool):
        parts.append(f"draft={'yes' if draft else 'no'}")
    threads = observation.get("unresolved_review_threads")
    if isinstance(threads, int) and not isinstance(threads, bool):
        parts.append(f"unresolved_review_threads={threads}")
    checks = observation.get("checks")
    if isinstance(checks, Mapping):
        # Failed and pending first, and named: those are the buckets an owner's
        # criterion asks about. Passed and unknown are counted only.
        for state in ("failed", "pending"):
            rendered = _render_check_bucket(checks, state)
            if rendered:
                parts.append(f"checks {rendered}")
        for state in ("passed", "unknown"):
            values = checks.get(state)
            if isinstance(values, (list, tuple)) and values:
                parts.append(f"checks {state} {len(values)}")
    # A partial reading must say so: a criterion about red checks means something
    # different when the check list itself is incomplete, and the judge cannot see
    # that from the tallies.
    for key in ("checks_complete", "review_threads_complete"):
        value = observation.get(key)
        if value is False:
            parts.append(f"{key}=no")
    head = observation.get("head_revision")
    if isinstance(head, str) and head.strip():
        parts.append(f"head={head.strip()[:12]}")
    digest = observation.get("pr_comment_body_digest")
    if isinstance(digest, str) and digest.strip():
        # Opportunistic, and a FINGERPRINT rather than text: the reader that sees
        # PR-level comment bodies reduces each to a fixed width and retains no body,
        # so this says discussion exists and carries none of it.
        parts.append(f"pr_comments_fingerprint={digest.strip()[:12]}")
    if not parts:
        return ""
    return "; ".join(parts)


def _pr_identity(value: str) -> tuple[str, str, str] | None:
    """*value*'s subject as ``(kind, subject, host)``, or ``None``. Never raises.

    The host is part of the identity: the same repository slug on two servers is
    two different pull requests, which is the case a slug comparison would miss.
    """
    if not value or not value.strip():
        return None
    try:
        from kiro_crew.probes import targets as _targets

        inferred = _targets.infer(value)
    except Exception:
        logger.debug("nudge.wake: target inference unavailable", exc_info=True)
        return None
    if inferred is None:
        return None
    return (inferred.kind, inferred.subject, inferred.host_key)


#: Keys a reader ADDS to the observation it hands over, which are not facts about
#: the subject. :func:`pr_observation_has_facts` discounts them, so an empty
#: canonical stays empty after the reader stamps its age onto the copy.
_PR_OBSERVATION_SIBLINGS = frozenset({"observed_at"})


def pr_observation_has_facts(observation: Mapping[str, Any] | None) -> bool:
    """Whether *observation* is a reading of a pull request at all.

    An empty canonical is the shape a monitor record carries before any provider
    writes one -- and the shape every GATED loop carries for the life of the watch,
    because the canonical field has one writer, the structured controller's
    provider, while a gated loop observes through the raise-based kernel whose
    verdict carries an outcome and prose and no facts.

    That has to read as NOT READ rather than as a subject with nothing to report.
    The two are indistinguishable downstream: :func:`render_pr_summary` returns
    ``""`` for both, :func:`pr_evidence` then yields no rows for both, and a tick
    holding no evidence and no dropped target is the one shape the point reads as
    every target having been read and found calm -- so an owner's criterion about
    their pull request would suppress every tick up to the streak floor. Answering
    ``False`` here makes it a dropped target instead, which fires.
    """
    if not isinstance(observation, Mapping):
        return False
    return any(str(key) not in _PR_OBSERVATION_SIBLINGS for key in observation)


def pr_target_is_unread(
    observation: Mapping[str, Any] | None,
    *,
    probe_covers_subject: bool,
) -> bool:
    """Whether this tick read the pull request at all, which is what a DROP means.

    A drop says "this target might have mattered and nobody looked", and the tick
    fires on it. So the question is not whether the judge got facts -- it is whether
    anything read the subject.

    *probe_covers_subject* is true when the typed probe observed this very subject on
    this tick, which the gated path always has by the time the judge is asked: the
    judge is consulted there only after the probe's own verdict came back quiet. A
    factless observation of that subject is then not an unread target. It was read,
    by the reader whose reading the judge was called to supplement, and counting it as
    unread fires a turn the probe had already settled -- every interval, for the life
    of the watch, which inverts what the screen is for.

    With no probe behind it -- a loop carrying no monitor, a spent watch, a brief
    naming some other pull request, or a build whose collector cannot reach one -- a
    factless observation is exactly an unread target, and firing is right.
    """
    if not isinstance(observation, Mapping):
        return True
    if pr_observation_has_facts(observation):
        return False
    return not probe_covers_subject


def pr_observation_is_about(
    target: str,
    *,
    monitor_kind: str,
    monitor_target: str,
    observation: Mapping[str, Any] | None = None,
) -> bool:
    """Whether *target* names the same pull request the observation is a reading of.

    A loop holds ONE monitor, so its reader answers with that monitor's single
    observation whatever subject it is asked for. The brief's ``targets`` are the
    owner's own strings, so a brief may name a SECOND pull request -- and the row
    would then be labelled with the name it asked for while carrying the watched
    subject's state. A judge could rule quiet on facts about a different pull
    request, which is the one way this path can suppress a turn that was owed.

    Three ways to agree, because the two sides are spelled by different writers and
    a monitor is stored in one of two shapes. An identical string is unambiguous.
    Otherwise *target* is put through the same inference :func:`parse_targets`
    already admits it by, and either the watched side infers to the same identity --
    host included, since one slug on two servers is two pull requests -- or the
    inferred ``kind`` and ``subject`` equal the pair the monitor is bound to.

    That third way is what a GATED loop needs. ``monitor_watch`` stores the caller's
    own canonical URL, so the first two ways cover it; ``infer_monitor`` stores the
    CANONICAL subject (``owner/name#123``) with the inferred kind, and that shorthand
    carries no host by design, so inference declines it. Putting the watched side
    through inference therefore answered ``None`` for every message-armed watch and
    dropped its target on every tick. Comparing the stored pair directly needs no
    host of its own: the inference pins exactly one host and refuses any other, so
    for a subject it admits at all, kind and subject ARE the whole identity.

    The observation's own ``target`` and ``kind`` labels are checked too when it
    carries them: those are what the reading says about itself, and a reading
    disagreeing with the record it came from is not a case to guess at.

    ``False`` on every doubtful case, which drops the target: the collector counts a
    drop, and a tick with no evidence answers FALLBACK, which fires.
    """
    kind = (monitor_kind or "").strip()
    watched = (monitor_target or "").strip()
    requested = (target or "").strip()
    if not kind or not watched or not requested:
        return False
    if isinstance(observation, Mapping):
        labelled = observation.get("target")
        if isinstance(labelled, str) and labelled.strip() and labelled.strip() != watched:
            return False
        observed_kind = observation.get("kind")
        if (
            isinstance(observed_kind, str)
            and observed_kind.strip()
            and observed_kind.strip() != kind
        ):
            return False
    if requested == watched:
        return True
    identity = _pr_identity(requested)
    if identity is None:
        return False
    if identity == _pr_identity(watched):
        return True
    return (identity[0], identity[1]) == (kind, watched)


def pr_evidence(
    observation: Mapping[str, Any] | None,
    target: str,
    *,
    now_ts: float | None = None,
) -> list[dict[str, Any]]:
    """One watched pull request's probe reading, as a single evidence row.

    The probe's own observation is carried rather than re-derived: it is the typed
    half of this decision and it has already run this tick, so asking the forge a
    second question would cost a subprocess to learn what the caller already holds.
    :func:`render_pr_summary` turns its typed keys into the prose a judge reads.

    What the judge adds over the probe is not a second reading of these facts. It is
    the OWNER'S criterion -- up to 500 characters of their own prose -- evaluated
    against them, which is the one thing a typed probe has no way to do.

    Comment body TEXT is not part of this evidence. The reader that sees PR-level
    comments reduces each body to a fixed-width fingerprint and retains none of
    them, so the fingerprint's presence is reported and nothing more.
    """
    if not isinstance(observation, Mapping):
        return []
    clock = point.now() if now_ts is None else now_ts
    summary = render_pr_summary(observation)
    if not summary:
        return []
    return [
        {
            "source": f"pr:{target}",
            "kind": point.KIND_PR_CHECKS,
            "age_s": _age_from_ts(observation.get("observed_at"), clock),
            "text": summary,
        }
    ]


async def collect_evidence(
    targets: Sequence[str],
    *,
    read_session: (
        Callable[[str, int], Awaitable[tuple[Sequence[Mapping[str, Any]], int]]] | None
    ) = None,
    read_pr: Callable[[str], Awaitable[Mapping[str, Any] | None]] | None = None,
    cursors: dict[str, int] | None = None,
    now_ts: float | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Evidence for one tick, and how many targets were dropped. Never raises.

    *read_session* is given a target and its cursor and returns ``(rows,
    next_cursor)``; *read_pr* is given a target and returns the probe observation.
    Either may be ``None``, which simply means that collector is unavailable on
    this build or this loop -- not an error, because a judge watching only
    sessions needs no pull-request reader.

    A reader that RAISES counts as a dropped target rather than a failed tick. The
    refusal a creator-only check produces arrives exactly that way, which is what
    makes "a target the owner may not read is dropped and noted" true by
    construction instead of by a second check here.

    *cursors* is updated in place for the targets that were read, so the next tick
    sees only what arrived in between. It is only advanced on a SUCCESSFUL read: a
    target that refused or raised keeps its old cursor, so a transient failure
    cannot silently skip the rows it would have returned.
    """
    clock = point.now() if now_ts is None else now_ts
    evidence: list[dict[str, Any]] = []
    wanted = list(targets)
    # Targets past the cap are NOT read, so they start the drop count rather than
    # vanishing from it. The cap bounds how many authorizations and reads one tick
    # performs, which is its job; what it must not do is make an incomplete reading
    # look like a complete one. With this at zero, a message naming one target more
    # than the cap allows would let a confident QUIET about the ones that fit suppress
    # a turn the unread one might have needed -- the same fault as a target the
    # collector could not read, arriving through a bound instead of a failure.
    dropped = max(0, len(wanted) - MAX_TARGETS)
    for target in wanted[:MAX_TARGETS]:
        try:
            if is_session_target(target):
                if read_session is None:
                    dropped += 1
                    continue
                rows, next_cursor = await read_session(target, int((cursors or {}).get(target, 0)))
                evidence.extend(session_evidence(rows, target, now_ts=clock))
                if cursors is not None and isinstance(next_cursor, int) and next_cursor >= 0:
                    cursors[target] = next_cursor
            else:
                if read_pr is None:
                    dropped += 1
                    continue
                observation = await read_pr(target)
                if observation is None:
                    # The reader answers ``None`` for every target nothing read this
                    # tick: a loop carrying no monitor -- which is every loop armed
                    # through ``POST /api/autonudge`` -- a brief naming a pull request
                    # this loop does not watch, a spent watch, and a factless
                    # observation with no typed probe behind it. It decides that,
                    # because the monitor and the probe's schedule are in its reach and
                    # not in this function's. Without the count the tick would hold no
                    # evidence and no drop, which is the one shape the point reads as
                    # "every target was calm".
                    dropped += 1
                    continue
                evidence.extend(pr_evidence(observation, target, now_ts=clock))
        except Exception as exc:
            # Includes the creator-only refusal. The class is not logged at
            # warning: a loop naming a session it may not read is an owner
            # mistake that would otherwise repeat every interval.
            dropped += 1
            if cursors is not None and getattr(exc, "code", "") == "cursor_unavailable":
                # The stored cursor does not address this session's transcript: it
                # was rewound, regenerated or trimmed under the loop. Counting that
                # as an ordinary drop keeps the same unusable cursor, so every later
                # tick re-sends it, is refused again, and the judge never reads this
                # target while the loop stays armed. Clearing the entry is what makes
                # the next tick a tail read, which is the recovery the reader
                # documents. Matched on the refusal's code rather than its class so
                # the collector stays independent of which reader was injected.
                cursors.pop(target, None)
                logger.debug(
                    "nudge.wake: clearing an unusable read cursor for one target",
                    exc_info=True,
                )
                continue
            logger.debug("nudge.wake: dropping a target this loop could not read", exc_info=True)
    return evidence, dropped


def spec_of(loop: Any) -> dict[str, Any]:
    """One loop's stored judge spec as a mapping, or ``{}``. Never raises.

    ``{}`` is "no judge on this loop", which is what a record written before the
    field existed decodes to and what an unreadable value resolves to -- the tick
    then behaves exactly as it does today.
    """
    try:
        raw = getattr(loop, "judge", None)
    except Exception:
        return {}
    return dict(raw) if isinstance(raw, Mapping) else {}


def criteria_of(spec: Mapping[str, Any] | None) -> tuple[str, str]:
    """The owner's ``(wake_when, quiet_when)``, each clipped, each possibly empty."""
    if not isinstance(spec, Mapping):
        return "", ""
    wake = spec.get("wake_when", "")
    quiet = spec.get("quiet_when", "")
    return (
        wake[: point.MAX_CRITERION_CHARS] if isinstance(wake, str) else "",
        quiet[: point.MAX_CRITERION_CHARS] if isinstance(quiet, str) else "",
    )


#: The brief a gated loop is screened under when its owner named none. Generic on
#: purpose: it asks the one question every patrol loop shares -- does the subject need
#: its owner this tick -- rather than anything about a particular subject, which is
#: what the owner's own criterion is for.
#:
#: "the loop message's own exit condition" is readable because the instruction is
#: passed to the judge as the tick's context, so a loop that says "stop when the PR is
#: merged" has its exit condition in front of the judge without restating it here.
DEFAULT_WAKE_WHEN = (
    "the subject needs its owner: a blocker, a question or ruling addressed to it, "
    "a terminal state, or the loop message's own exit condition"
)
DEFAULT_QUIET_WHEN = "nothing new for the owner since the last tick"

#: Which brief a tick ran under, for the transcript notice. A reader has to be able to
#: tell a verdict reached under their own criterion from one reached under the shipped
#: default, because only the first is evidence that their criterion works.
BRIEF_DEFAULT = "default"
BRIEF_CUSTOM = "custom"


def default_spec() -> dict[str, Any]:
    """The default brief, fresh each call so a caller cannot mutate the shipped one.

    No ``targets``: :func:`parse_targets` reads the loop's own instruction when a brief
    names none, so the default inherits whatever subject the loop already names. A loop
    whose message names no readable subject has nothing to observe, and its tick fires
    as it does today rather than being screened against an empty reading.
    """
    return {"wake_when": DEFAULT_WAKE_WHEN, "quiet_when": DEFAULT_QUIET_WHEN}


def verdict_record(verdict: Any, evidence_items: int) -> dict[str, Any]:
    """The previous-verdict summary carried into the NEXT tick's state.

    Deliberately small and text-free: the outcome, how much it was based on, and
    when. A judge seeing this knows it already passed on comparable evidence
    without being handed that evidence a second time.
    """
    try:
        outcome = verdict.outcome.value
    except Exception:
        outcome = "unknown"
    return {"outcome": outcome, "evidence_items": int(evidence_items), "at": time.time()}


#: How many labelled verdicts a loop's record keeps. Sized from the QUIET-STREAK
#: FLOOR, not from the window the judge reads: a full streak is the floor's worth of
#: suppressed verdicts followed by the delivery that labels them, and a store smaller
#: than that evicts the earliest suppressions before their label arrives. The floor is
#: configurable but clamped to ``autonudge._MAX_QUIET_STREAK``, so this covers every
#: streak the service can produce. ``test_wake_judge_feedback`` pins the two together.
MAX_STORED_VERDICTS = 11

#: A reply at or under this length, from a turn that called no tool, is the quiet-cycle
#: shape: the loop woke, the owner looked, there was nothing to do, and the turn said
#: so. Calibrated against what such a reply actually is -- one or two sentences -- and
#: deliberately generous, because the direction to be wrong in is calling a real turn
#: quiet rather than the reverse. A false ``owner_acted`` teaches the judge that a wake
#: was warranted, which costs turns; a false quiet teaches it to suppress.
QUIET_REPLY_MAX_CHARS = 280

#: Longest verdict id a stored row keeps. One spelling, held in
#: :mod:`kiro_crew.decisions.log`, so the stored row's ``id`` and the ``verdict_id`` on
#: the label row joining to it clip to the same length.
MAX_VERDICT_ID_CHARS = _MAX_VERDICT_ID_CHARS


def owner_action_reading(
    tool_calls: object,
    reply_text: object,
    *,
    reply_flushed: bool = False,
) -> tuple[bool, int | None, int]:
    """The action label and the text-free inputs from which it is derived.

    The tool-call count is ``None`` when it is unknown. The reply measurement is the
    stripped character count, never the reply or a fragment of it. Returning all three
    from one reading keeps the durable calibration row aligned with the boolean label.
    """
    tool_count = (
        tool_calls
        if isinstance(tool_calls, int) and not isinstance(tool_calls, bool) and tool_calls >= 0
        else None
    )
    text = reply_text if isinstance(reply_text, str) else ""
    stripped = text.strip()
    reply_chars = len(stripped)
    acted = owner_acted(tool_calls, reply_text, reply_flushed=reply_flushed)
    return acted, tool_count, reply_chars


def owner_acted(tool_calls: object, reply_text: object, *, reply_flushed: bool = False) -> bool:
    """Whether the woken turn DID anything. The whole rule, in one function.

    Deterministic and model-free, which is what makes it usable as a label: two
    readings of the same turn agree, and a curve built from these labels measures the
    judge rather than a second judge's opinion of it.

    ``True`` when the turn called at least one tool, or when its reply is longer than
    the quiet-cycle shape (:data:`QUIET_REPLY_MAX_CHARS`) or carries a link. ``False``
    only for a turn that called nothing and answered short -- which is exactly the
    reply a loop produces when it wakes, looks, and finds nothing for its owner.

    A turn that CHANGED the loop needs no separate signal: ``monitor_update`` and
    ``autonudge_stop`` are tool calls, so the count already carries them. Reading them
    a second way would be a second rule that can disagree with this one.

    An UNKNOWN tool-call count -- no count passed, an older caller, a turn whose runner
    never reached the hook -- reads as acted. The count is the strong half of the rule,
    so without it the honest answer is that this turn cannot be shown to have been
    idle, and the cheap direction to be wrong in is the one that does not teach the
    judge to stay quiet.

    When *reply_flushed* is true, an earlier segment already left the screen, so the
    text in hand is not the whole reply and cannot be judged short. Erring toward
    acted is the safe direction under this function's contract.

    *reply_text* is read and not retained: the answer is one boolean, and nothing
    downstream of this function stores or logs the reply.
    """
    tool_count = (
        tool_calls
        if isinstance(tool_calls, int) and not isinstance(tool_calls, bool) and tool_calls >= 0
        else None
    )
    text = reply_text if isinstance(reply_text, str) else ""
    stripped = text.strip()
    return (
        reply_flushed
        or tool_count is None
        or tool_count > 0
        or len(stripped) > QUIET_REPLY_MAX_CHARS
        or "http://" in stripped
        or "https://" in stripped
    )


def new_verdict_id() -> str:
    """An opaque key joining one verdict's decision row to its later label row.

    Random rather than a counter: the two writers are a tick and a turn-complete hook,
    neither holds a lock over the other, and a restart between them must not hand a
    second verdict the same key. It never reaches the judge -- the point rebuilds every
    row it sends and carries no id.
    """
    return secrets.token_hex(8)


def verdict_entry(
    verdict: Any,
    evidence_items: int,
    *,
    suppressed: bool,
    answered: bool,
    verdict_id: str = "",
    at: float | None = None,
) -> dict[str, Any]:
    """One row for the loop's labelled verdict history, as the TICK knows it.

    A tick knows two things a label pass cannot recover later, and neither is the
    outcome name. ``suppressed`` says this verdict withheld the turn, which is what
    makes it eligible for a ``missed`` label. ``answered`` says the judge actually
    produced it: a tick that read nothing new, or could not read a target, returns a
    verdict without asking the judge at all, and such a verdict scores nothing --
    it sat on no evidence and no decision row exists to join a label to.

    ``delivered`` is deliberately NOT set here. A verdict that decided to wake has
    not delivered anything yet: the fire can be refused because the slot is busy,
    the timer can be cancelled by the owner typing, or the process can stop in
    between. Only the fire path knows delivery happened, and it stamps the row
    there (:func:`confirm_delivery`). So a fresh wake row is neither suppressed nor
    delivered, and that third state is what keeps an unrelated turn's actions off
    it.

    ``verdict_id`` keys the calibration log row that carries this row's label, so
    the two join without rewriting the line the decision already wrote.
    """
    row = verdict_record(verdict, evidence_items)
    row["answered"] = bool(answered)
    if suppressed:
        row["suppressed"] = True
    if at is not None:
        row["at"] = float(at)
    if verdict_id:
        row["id"] = str(verdict_id)[:MAX_VERDICT_ID_CHARS]
    return row


def append_verdict(
    history: Sequence[Mapping[str, Any]] | None, row: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """*history* with *row* appended, oldest first, bounded to the stored window."""
    out = [dict(item) for item in list(history or []) if isinstance(item, Mapping)]
    out.append(dict(row))
    del out[:-MAX_STORED_VERDICTS]
    return out


def confirm_delivery(
    history: Sequence[Mapping[str, Any]] | None,
) -> tuple[list[dict[str, Any]], bool]:
    """*history* with the newest undecided row stamped delivered, and whether one was.

    The undecided row is the one a wake verdict left behind: neither suppressed nor
    yet delivered. Stamping it HERE, from the fire path, is what makes ``delivered``
    mean "a turn really went out" rather than "a tick meant to send one" -- and that
    is the difference between labelling the woken turn and labelling whatever turn
    happened to finish next.

    ``False`` when there is no such row, which is the ordinary case for a tick the
    judge suppressed and for a fire no judge verdict asked for.

    A FORFEITED row is a permanent boundary: its delivery went out but its turn was
    never seen, so no later turn can supply its label. A re-owed delivery has its own
    newer undecided row, which this walk stamps without changing the boundary.
    """
    rows = [dict(item) for item in list(history or []) if isinstance(item, Mapping)]
    for position in range(len(rows) - 1, -1, -1):
        row = rows[position]
        if row.get("forfeited") is True:
            return rows, False
        if row.get("delivered") is True or row.get("suppressed") is True:
            # The newest row already knows what it is, so no verdict is awaiting a
            # delivery stamp. Stopping at the first decided row rather than scanning
            # past it keeps an older undecided row -- a wake whose fire was lost --
            # from being credited to this delivery.
            return rows, False
        rows[position]["delivered"] = True
        return rows, True
    return rows, False


def mark_fired(history: Sequence[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    """*history* with the newest row's suppression withdrawn.

    For the one tick that judged quiet and then fired anyway because its state did
    not persist. The row claimed to withhold the turn and the turn is going out, so
    the claim is wrong; clearing it returns the row to the undecided state, where the
    fire path's own stamp can confirm it like any other delivery.
    """
    rows = [dict(item) for item in list(history or []) if isinstance(item, Mapping)]
    if rows:
        rows[-1].pop("suppressed", None)
    return rows


def label_latest_delivery(
    history: Sequence[Mapping[str, Any]] | None,
    *,
    acted: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """*history* with one delivery labelled, and the rows whose label changed.

    The newest row marked DELIVERED and carrying no label yet is labelled
    ``owner_acted``. Every unlabelled suppressed row between it and the delivery
    before it is then labelled ``missed`` with the same value: the owner had
    something to do and the judge sat on it for those ticks, or the owner had
    nothing and the judge was right to.

    Two kinds of row are refused rather than labelled, and both would bias the curve
    the thresholds are read from. A row the judge did not answer sat on nothing -- the
    point returned it without asking, because the tick read nothing new or could not
    read a target -- so a label on it counts a decision that never happened, whether it
    is the delivery's own ``owner_acted`` or a ``missed`` on a suppression. A FORFEITED
    delivery belongs to a turn this process never saw, so no ``owner_acted`` for it can
    be read truthfully.

    A row that is neither suppressed nor delivered is a wake whose fire was lost. It
    withheld nothing, so it takes no ``missed`` -- and it ENDS the retroactive walk,
    because the suppressions behind it belong to its own cycle rather than to the
    delivery this pass is labelling.

    The walk stops at the newest UNLABELLED delivery rather than the newest delivery
    outright, so a second turn-complete for one delivery -- a retry, a duplicated
    hook -- relabels nothing. The returned change list is what the caller writes to
    the calibration log, so a pass that changed nothing logs nothing either.
    """
    rows = [dict(item) for item in list(history or []) if isinstance(item, Mapping)]
    changed: list[dict[str, Any]] = []
    index: int | None = None
    for position in range(len(rows) - 1, -1, -1):
        row = rows[position]
        if row.get("delivered") is not True:
            continue
        if isinstance(row.get("owner_acted"), bool):
            # This delivery is already judged, and so is everything before it.
            break
        if row.get("forfeited") is True or row.get("answered") is not True:
            # A delivery no label can be read for. It still closes its own cycle, so
            # the pass ends here rather than reaching back to an older delivery whose
            # suppressions this one's label does not judge.
            break
        index = position
        break
    if index is None:
        return rows, changed
    rows[index]["owner_acted"] = bool(acted)
    changed.append(dict(rows[index]))
    delivery_at = rows[index].get("at")
    delivery_clock = (
        float(delivery_at)
        if isinstance(delivery_at, (int, float))
        and not isinstance(delivery_at, bool)
        and math.isfinite(float(delivery_at))
        else point.now()
    )
    for position in range(index - 1, -1, -1):
        row = rows[position]
        if row.get("delivered") is True:
            break
        if row.get("suppressed") is not True:
            break
        if row.get("answered") is not True:
            continue
        if isinstance(row.get("missed"), bool):
            break
        row["missed"] = bool(acted)
        changed_row = dict(row)
        changed_row["position_back"] = index - position
        changed_row["age_s"] = _age_from_ts(row.get("at"), delivery_clock)
        changed.append(changed_row)
    return rows, changed


def recent_for_state(
    history: Sequence[Mapping[str, Any]] | None,
    *,
    now_ts: float | None = None,
) -> list[dict[str, Any]]:
    """The labelled history with ages in seconds, for the point to screen and bound.

    The stored rows carry an absolute ``at``; the request carries an AGE, because a
    wall-clock timestamp would tell the judge what day it is and nothing it needs.
    Screening and the window bound belong to the point and are applied there -- this
    only turns stored times into elapsed ones.
    """
    clock = point.now() if now_ts is None else now_ts
    out: list[dict[str, Any]] = []
    for raw in list(history or []):
        if not isinstance(raw, Mapping):
            continue
        row = dict(raw)
        row["age_s"] = _age_from_ts(row.get("at"), clock)
        out.append(row)
    return out


def since_last_wake_s(last_fire_ts: object, *, now_ts: float | None = None) -> float | None:
    """Seconds since this loop last delivered a turn, or ``None`` when it never has.

    ``None`` is the honest answer for a loop on its first tick, and the point omits the
    field rather than sending a zero that would read as a delivery this instant.
    """
    if isinstance(last_fire_ts, bool) or not isinstance(last_fire_ts, (int, float)):
        return None
    stamp = float(last_fire_ts)
    if not math.isfinite(stamp) or stamp <= 0:
        return None
    clock = point.now() if now_ts is None else now_ts
    elapsed = clock - stamp
    return elapsed if elapsed > 0 else 0.0


def _pr_subject(value: str) -> bool:
    """Whether a pull-request target can be inferred from *value*. Never raises."""
    if not value or not value.strip():
        return False
    try:
        from kiro_crew.probes import targets as _targets

        return _targets.infer(value) is not None
    except Exception:
        logger.debug("nudge.wake: target inference unavailable", exc_info=True)
        return False


def _age_from_ts(raw: object, clock: float) -> float:
    """Seconds between *raw* and *clock*, or 0.0 when *raw* is not a usable time.

    Both shapes are parsed because the two callers genuinely differ: a probe
    observation carries an epoch float, while a transcript row carries an ISO 8601
    STRING (``'2026-09-22T07:25:47.670891+00:00'``). Reading only the float made every
    session row age 0.0, which quietly broke the drop-oldest-first bound -- with all
    ages equal, what got discarded when the cap was reached was arbitrary, so a newer
    actionable row could lose its place to an older one.

    A naive string -- no offset -- is read as UTC, matching how the rest of the
    gateway stores times. Anything unparseable is 0.0, which keeps the item rather
    than dropping it: an unreadable clock is a reason to let the judge see the
    evidence, not to hide it.
    """
    if isinstance(raw, bool):
        return 0.0
    stamp: float | None = None
    if isinstance(raw, (int, float)):
        stamp = float(raw)
    elif isinstance(raw, str) and raw.strip():
        text = raw.strip()
        # ``fromisoformat`` handles the trailing 'Z' only from 3.11, and the gateway
        # supports older readers of the same rows, so normalise it here.
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return 0.0
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        stamp = parsed.timestamp()
    if stamp is None or not math.isfinite(stamp):
        return 0.0
    age = clock - stamp
    return age if age > 0 else 0.0
