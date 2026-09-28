"""Read-only views of ``crons.json`` for callers with no running scheduler.

``kirocrew doctor``, the skill lifecycle, the Agent templates delete guard, the
dashboard status count and the telemetry probe each read the store file
directly, so they work when the gateway is down or wedged. They share one
read-parse-shape prologue (:func:`~kiro_crew.cron_service.store._read_job_records`)
and the scheduler's own record predicates, so a record the scheduler would not
load is never reported, counted or matched.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from kiro_crew.cron_service.identity import agent_sequence_dispatches
from kiro_crew.cron_service.store import (
    _CRONS_FILE,
    CronStoreUnreadable,
    _is_loadable_record,
    _read_job_records,
)

# ``$skill`` token pattern (mirrors skills._DOLLAR_SKILL_PATTERN; duplicated
# here to avoid a cron<->skills import cycle).
_SKILL_TOKEN_RE = re.compile(r"(?<![\w$])\$([a-z0-9][a-z0-9/_-]*)")


def referenced_skill_names() -> set[str]:
    """Skill slugs referenced via ``$skill`` tokens in any cron job's message.

    Read-only + best-effort: reads ``crons.json`` directly (so it needs no
    running scheduler) and returns an empty set on any error. The skill
    lifecycle uses this to exempt cron-referenced skills from eviction — a job
    that says ``$deploy-helper`` keeps ``auto/deploy-helper`` from being
    archived out from under it. Returns both the raw token and its last path
    segment so callers can match either a full key or a bare slug.
    """
    from kiro_crew import cron as seams  # the facade holds the patched names; it imports us

    out: set[str] = set()
    try:
        for j in _read_job_records(seams.config_dir() / _CRONS_FILE)[0]:
            msg = j.get("message") or ""
            for m in _SKILL_TOKEN_RE.finditer(msg):
                tok = m.group(1)
                if any(c.isalpha() for c in tok):
                    out.add(tok)
                    out.add(tok.rpartition("/")[2])
    except Exception:
        return set()
    return out


def dispatched_agents_from_disk(*, loadable_only: bool) -> list[tuple[str, str, str]]:
    """``(job id, holder label, agent name)`` for every agent a stored job DISPATCHES.

    The ONE walk that encodes the dispatch-mirroring rule, so the two readers
    that need it -- ``kirocrew doctor`` through :func:`job_agent_names_from_disk`
    and the Agent templates delete guard -- cannot drift when a job kind is
    added. Reads ``crons.json`` directly (no running scheduler) and lets any
    read or parse error propagate; the doctor wrapper is the one that swallows.

    Mirrors dispatch, not storage: a ``script`` or ``command`` job bypasses
    agent dispatch entirely, so its agent fields are dormant and the record is
    skipped whole; otherwise, when :func:`agent_sequence_dispatches` the
    sequence entries are reported and ``agent_id`` is dormant, else the
    template the job actually runs is reported and the sequence (if any) is
    dormant. That template is the captured ``execution_context.template_id``
    when the record carries one (:func:`resolve_cron_memory` and the gateway
    dispatch both read it there -- a schedule created from a template chat
    with no ``agent`` argument names its template ONLY there, ``agent_id``
    staying empty), else ``agent_id`` for a legacy record. A record whose
    AGENT fields the scheduler's loader rejects (a non-list sequence, a
    non-string entry or ``agent_id``) dispatches nothing and contributes nothing.

    *loadable_only* is where the two readers legitimately differ. The delete
    guard passes ``True``: it counts only records the scheduler could build
    (:func:`_is_loadable_record`), because a record with no ``schedule`` never
    fires and must not pin a template forever. Doctor passes ``False``: it
    warns about every deprecated name written on disk, including a partial or
    legacy record the operator can still see and repoint.
    """
    from kiro_crew import cron as seams  # the facade holds the patched names; it imports us

    store = seams.config_dir() / _CRONS_FILE
    records, loadable = _read_job_records(store)
    if not records and not loadable:
        # Present but unreadable (permissions, bytes, JSON, shape): the
        # scheduler loads nothing from it NOW, but a repaired store brings its
        # jobs back with the agents they name -- so this is not "no
        # references", it is "the references cannot be read", and the caller
        # decides how loud to be. A store whose records parsed but none of
        # which the scheduler can build is NOT this case: those records are
        # returned and each reader applies its own loadability rule below.
        raise CronStoreUnreadable(str(store))
    out: list[tuple[str, str, str]] = []
    for j in records:
        if loadable_only and not _is_loadable_record(j):
            continue
        if j.get("script") or j.get("command"):
            continue  # runs with no LLM; agent fields are dormant
        seq = j.get("agent_sequence", [])
        if not isinstance(seq, list) or any(not isinstance(s, str) for s in seq):
            continue  # the scheduler's loader rejects this record whole
        agent_id = j.get("agent_id", "")
        if agent_id is not None and not isinstance(agent_id, str):
            continue  # same rejection class
        job_id = j.get("id")
        job_id = job_id if isinstance(job_id, str) else ""
        name = j.get("name")
        label = name if isinstance(name, str) and name else (job_id or "<unnamed job>")
        if agent_sequence_dispatches(seq):
            names = [s for s in seq if s]
        else:
            runs = _captured_template_id(j.get("execution_context")) or agent_id
            names = [runs] if runs else []
        out.extend((job_id, label, agent) for agent in dict.fromkeys(names))
    return out


def _captured_template_id(execution_context: Any) -> str:
    """The template a stored job's captured execution names, or ``""``.

    Read leniently on purpose: this is a reference scan over records on disk,
    not the loader, so a record whose context is missing or malformed simply
    contributes no captured name and falls back to ``agent_id`` -- the same
    order the dispatcher applies when it has no usable context.
    """
    if not isinstance(execution_context, dict):
        return ""
    template_id = execution_context.get("template_id")
    return template_id if isinstance(template_id, str) else ""


def job_agent_names_from_disk() -> list[tuple[str, str]]:
    """``(job name, agent name)`` for every agent a stored cron job dispatches.

    Read-only + best-effort like :func:`referenced_skill_names`: the doctor
    wrapper over :func:`dispatched_agents_from_disk` that returns an empty list
    on any error (an unreadable store included -- doctor reports that fault
    through its own check), so ``kirocrew doctor`` can warn about a job that
    still names a deprecated agent spec without failing over the store.
    """
    try:
        return [
            (label, agent)
            for _job_id, label, agent in dispatched_agents_from_disk(loadable_only=False)
        ]
    except Exception:
        return []


def enabled_count_from_disk(path: Path) -> tuple[int, bool]:
    """Return ``(enabled count, loadable)`` for the store at *path*.

    A sibling of :func:`unhealthy_jobs_from_disk`: read-only, non-raising, needs
    no running scheduler, and carries ``loadable`` on the SAME read for the same
    reason — a store the scheduler cannot load counts 0, which is
    indistinguishable from a healthy empty store in the number alone.

    Single owner of the enabled-count reduction. Two callers need it and want
    different halves: :meth:`CronService.count_enabled_from_disk` takes the count
    and degrades a fault to 0 (its caller is a status pusher that must keep
    running), while the telemetry probe needs ``loadable`` to report a fault as a
    fault rather than as a plausible number. One loop serves both, so the two
    readers cannot drift apart on which records count; sharing only the
    ``_is_loadable_record`` / ``_record_is_enabled`` predicates would cap that
    drift without removing it.
    """
    from kiro_crew import cron as seams  # the facade holds the patched names; it imports us

    count = 0
    records, loadable = _read_job_records(path)
    for j in records:
        # Same skip decision as _load: a record _job_from_record rejects is not
        # a schedulable job, so it must not be counted.
        if not _is_loadable_record(j):
            continue
        if seams._record_is_enabled(j):
            count += 1
    return (count, loadable)
