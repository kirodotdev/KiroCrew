"""The activity pointers a member's sessions leave, and their read.

Which sessions may be recorded at all, the append of one pointer entry, and the read of a
member's entries oldest first, with whether the read saw all of them.

:mod:`kiro_crew.members` stays the import path and the patch surface; see
:mod:`kiro_crew.members_parts`.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from kiro_crew.members_parts.member_space import MemberSlugError, member_slug, validate_slug

# Logs under the facade's name, which operators and tests filter on.
logger = logging.getLogger("kiro_crew.members")


#: The ONLY mode whose sessions may be recorded. An allowlist, not a denylist of
#: no-trace modes: a mode that is missing, empty (metadata not yet flushed for a
#: brand-new session) or simply unrecognized would pass a denylist and durably
#: record a private session key in a log that outlives session pruning. Failing
#: closed costs at most a missing entry in an advisory log.
_TRACEABLE_MEMORY_MODES = frozenset({"persistent"})


def record_activity(
    member: str,
    session_key: str,
    memory_mode: str,
    *,
    project: str = "",
    via: str = "",
    dedupe_session: bool = False,
) -> bool:
    """Append one pointer entry to a member's activity log.

    Takes the member's NAME and derives the slug internally, so callers need no
    try/except: every failure path — a name that yields no usable slug, a
    read-only home, a torn write — is handled here and reported ``False``. This
    is best-effort by contract; a logging failure must never break the turn that
    triggered it, and one call site (``mcp_core``) has no logger of its own.

    ``memory_mode`` is REQUIRED and positional, not an opt-in keyword: it gates
    whether the session may be recorded at all, and a caller that simply forgot
    it would durably log a private session. It is matched against an allowlist
    (:data:`_TRACEABLE_MEMORY_MODES`), so an absent, empty or unrecognized mode
    skips the write rather than passing through.

    ``dedupe_session`` suppresses a repeat entry for a member/session pair. The
    chat site needs it because its ``is_new`` flag tracks the PROVIDER session,
    not the conversation: a dead provider cold-starts the same conversation with
    ``is_new=True`` again, which would append the same pointer twice and inflate
    the counts this log exists to feed. Routing decisions are NOT deduped — each
    ``select_crew`` bind is a distinct event even for one session.

    ``via`` records HOW the member was chosen, because the call sites mean
    different things and a mixed log cannot be read apart afterwards:

    * ``"chat"`` — the human picked this member for the session.
    * ``"select_crew"`` — the orchestrator judged this member fits the task.
    * ``"spawn"`` — the member ran a delegated ``spawn_run(crew=...)``.
    * ``"auto-improvement"`` — the auto-improvement runner ran the member.

    A ``select_crew`` entry records the routing *decision*, not an execution:
    binding a crew does not oblige the model to delegate to it. That is the
    useful signal for trigger generation (what the router believes belongs to
    whom), but it means these counts are intent, not runs.

    Blocking file IO: call via ``asyncio.to_thread`` from async code.
    """
    if not member or not session_key:
        return False
    if memory_mode.strip().lower() not in _TRACEABLE_MEMORY_MODES:
        return False
    # The exact member name travels IN the record rather than being implied by
    # the directory. Slugification is lossy, so two distinct member names can
    # map to one slug ("Review_Agent" and "review-agent") and share a log;
    # carrying the name keeps per-member attribution recoverable in that case,
    # which the frequency signal downstream depends on.
    #
    # The session pointer is named for what it MEANS, not just what it holds.
    # A routing decision is recorded in the session that made it — the parent —
    # while the member itself runs in a different (sub-agent) session that does
    # not exist yet at bind time. Filing both under one `session` key would let
    # a consumer counting "sessions this member took part in" count a session
    # the member never ran in. Distinct keys make that misread impossible
    # instead of leaving it to the consumer to notice `via`.
    entry = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "member": member,
    }
    if via == "select_crew":
        entry["decided_in"] = session_key
    else:
        entry["session"] = session_key
    if project:
        entry["project"] = project
    if via:
        entry["via"] = via
    # Imported BEFORE the try, not beside the sibling imports inside it: the
    # handler below reads this name, so an import that failed inside the block it
    # guards would raise NameError from the handler itself.
    from kiro_crew.crew_log.errors import CODE_ALREADY_OWNED

    try:
        # main's resolver, not the bare fold: a member may carry an explicit
        # `member_id` in config, and member_slug honours it before falling back to
        # slug_for_name. The log has to be keyed by the same id the rest of the
        # roster uses, or a member with an explicit id reads an empty projection.
        slug = member_slug(member)
        from kiro_crew.eventlog.service import get_service
        from kiro_crew.eventlog.types import ACTIVITY_RECORD

        svc = get_service()
        # ENSURE FIRST, then dedupe. ``ensure`` is what folds a pre-upgrade
        # ``activity.jsonl`` into the log, so a scan running before it reads a
        # log the legacy rows have not reached yet -- and the first
        # post-upgrade call for a session already recorded in that file would
        # dedupe against nothing and append a permanent duplicate. Both steps
        # are idempotent, so paying ensure before a scan that may return early
        # costs a caller nothing.
        svc.ensure(slug, member)
        if dedupe_session:
            # Check the member's own ACTIVITY_RECORD events for this session
            # pair instead of scanning a file. Matched on BOTH fields: a
            # colliding slug means one log can hold two members, so session
            # alone would suppress the wrong entry. Only participation entries
            # carry `session`, which is also the only kind deduped — routing
            # decisions are distinct events.
            #
            # The scan is UNBOUNDED, and a bound is not an optimisation here: the
            # log is already materialised in memory by the read below, so a limit
            # only truncates a filter over a list that was loaded either way. A
            # 200-event cap therefore bought no I/O and did buy a miss — a
            # session resumed after 200 later events deduped against nothing and
            # wrote a second participation record, inflating the activity
            # projection it feeds.
            recent = svc.history(slug, before=None, limit=None)
            for ev in recent:
                if ev.get("type") != ACTIVITY_RECORD:
                    continue
                rec = ev.get("data") or {}
                if rec.get("session") == session_key and rec.get("member") == member:
                    return False
        svc.append(slug, ACTIVITY_RECORD, entry)
        return True
    except Exception as exc:
        # An ``already_owned`` refusal is a LOSS, and it is reported rather than
        # hidden. The store's write lease is taken non-blocking, so a second
        # process appending at the same instant is refused outright instead of
        # being serialized behind the per-append lock -- and this entry is then
        # never written. The two writers are ordinary rather than pathological:
        # ``kirocrew-core`` runs as its own subprocess and records activity
        # through this function, which :meth:`MemberLog.refresh_if_changed`
        # already names as a second writer on the read side. ``crew_log.lease``
        # states the caller's duty for this code -- it reports the loss rather
        # than retrying it -- and a debug line does not discharge that: it leaves
        # a dropped routing decision indistinguishable from a member that was
        # never routed, which is the one thing this record exists to tell apart.
        if getattr(exc, "code", "") == CODE_ALREADY_OWNED:
            logger.warning(
                "member activity entry DROPPED for %r (session %r, via %r): another "
                "process owns writes to this member's log, so the entry is lost and "
                "is not retried: %s",
                member,
                session_key,
                via,
                exc,
            )
        else:
            logger.debug("member activity log write failed for %r", member, exc_info=True)
        return False


def read_activity(slug: str, limit: int = 0) -> list[dict]:
    """Return a member's activity entries, oldest first.

    The degrading view of :func:`_read_activity_checked`: it drops the
    completeness flag. Backed by the per-member append-only event log — the
    entries are the record dicts carried by ``ACTIVITY_RECORD`` events, in
    append order (oldest first). ``limit`` > 0 returns only the most recent N.
    Every failure reads as an empty list, so a caller that only displays or
    counts entries needs no try/except.
    """
    rows, _complete = _read_activity_checked(slug, limit)
    return rows


def _read_activity_checked(slug: str, limit: int = 0) -> tuple[list[dict], bool]:
    """Return a member's activity entries oldest first, and whether they are ALL of them.

    Backed by the per-member append-only event log: reads ``ACTIVITY_RECORD``
    events (newest first from the service), unwraps each to the record dict it
    carries, and reverses to oldest-first — the order the former file-backed
    reader returned. ``limit`` > 0 returns the most recent N. A bad slug or any
    read failure yields ``([], True)``.

    The second element is retained for callers that fail closed on a partial
    read of the agent-writable file. The event log commits one line
    per append under fsync and the reader repairs a torn trailing line at load,
    so a partial-read completeness gap does not arise here — the flag is
    always ``True`` on a successful read. It stays in the signature so the
    ``record_activity`` dedupe probe and its tests keep their shape.
    """
    try:
        validate_slug(slug)
    except MemberSlugError:
        return [], True
    try:
        from kiro_crew.eventlog.service import get_service
        from kiro_crew.eventlog.types import ACTIVITY_RECORD

        svc = get_service()
        events = svc.history(slug, before=None, limit=None)
        records = [
            ev.get("data") or {} for ev in reversed(events) if ev.get("type") == ACTIVITY_RECORD
        ]
        rows = [r for r in records if isinstance(r, dict)]
    except Exception:
        logger.debug("member activity log read failed for %r", slug, exc_info=True)
        return [], True
    return (rows[-limit:] if limit > 0 else rows), True
