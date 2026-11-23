"""A Bot's scheduled job keeps the approval mode the Bot's Profile gave it.

A Bot's own chats and workers take their mode from the Bot's Profile Permissions
(``agents.<name>.approval_mode``, see ``dashboard.crewmate_approval``), which is on
disk, so it holds across a gateway restart. A job the Bot makes with ``cron_add``
runs on the Bot's worker too, and its own ``approval_mode`` is never ``auto``
(a tool may not set it). Without a grant of its own, such a job auto-approves
only while an in-memory grant (an ad-hoc YOLO, a slot's Trust) is live, and a
restart ends those, so the Bot's chats would run unasked while its jobs ask.

This module gives the job that grant with two reads:

* :func:`creator_grant` runs once, when ``cron_add`` makes the job. When the
  calling session is a Bot's chat or worker, it returns that Bot's member id and
  the Profile mode it holds now (clamped by policy). The job stores both
  (``CronJob.owner_member_id`` / ``owner_approval_mode``).
* :func:`owner_grant_holds` runs at each fire and on each tool request. The job
  auto-approves only when the stored mode is ``trust``, the fire runs on that
  same Bot's worker, AND the Bot's live Profile mode is still ``trust`` under
  policy. So the job is never above the mode its creator held, and lowering the
  Bot's Profile lowers the job at its next request. The grant covers the turn's
  own tool calls only: the session's stored policy and the mode a ``spawn_run``
  child inherits stay the job's own, so no child keeps a grant the Profile
  change cannot reach.

The job's own Approval field stays the owner's switch, which no tool can turn on.
The mode carried here is the Bot's Profile Permissions, which only the owner sets.

A job a Bot made before this build has no stored grant. :func:`migrate_owner_grants`
runs once at start and stamps such a job only when its record proves the Bot made
it, with the Bot's Profile mode at that moment.

Anything missing, unreadable or different answers "no grant", which is today's
behaviour: the call goes to the approval prompt. A held Bot limit still reaches
the prompt (``fields.auto_job_approver``). ``crons.json`` is a protected file an
agent cannot write, and a forged stored mode could only lift a job to what the
Bot's live Profile already gives its worker.

shortcut: only ``trust`` carries. A ``trust_reads`` Profile stores its mode but
the job keeps asking, because the cron turn has no read-only auto-approve rung.
Upgrade when the cron approval path gains one.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

# The cron channel every cron_service owner logs on.
logger = logging.getLogger("kiro_crew.cron")

#: The only stored mode that lets a job auto-approve (see the shortcut above).
_CARRIED_MODE = "trust"

#: Written beside ``crons.json`` once :func:`migrate_owner_grants` has run, so a
#: later start never stamps again (a job made after it gets its grant from
#: ``cron_add``, or none).
MIGRATION_MARKER = "cron_owner_grants_migrated"


def _profile_mode(config: Any, name: str) -> str:
    """Bot *name*'s Profile mode as policy lets a session hold it now."""
    from kiro_crew.approval_mode_policy import clamp_session_mode
    from kiro_crew.config.sections_agent_fields import (
        MEMBER_APPROVAL_MODE_UNSET_EFFECTIVE,
        coerce_member_approval_mode,
    )

    record = getattr(config, "agents", {}).get(name)
    if record is None:
        return "normal"
    raw = coerce_member_approval_mode(getattr(record, "approval_mode", ""))
    return clamp_session_mode(raw or MEMBER_APPROVAL_MODE_UNSET_EFFECTIVE)


def creator_grant(session_key: str, config: Any = None) -> tuple[str, str]:
    """``(member_id, mode)`` of the Bot whose session *session_key* is, else ``("", "")``.

    Reads the session's execution record and, when *config* is None, the
    configuration from disk, so an ``async`` caller runs it in a thread.
    """
    if not session_key:
        return "", ""
    try:
        from kiro_crew.dashboard.crewmate_approval import crewmate_name_for
        from kiro_crew.execution_context import read_session_execution

        if config is None:
            from kiro_crew.config.loader import KiroCrewConfig

            config = KiroCrewConfig.load()
        execution = read_session_execution(session_key)
        member_id = getattr(execution, "member_id", None)
        name = crewmate_name_for(config, execution)
        if not name or not isinstance(member_id, str) or not member_id:
            return "", ""
        return member_id, _profile_mode(config, name)
    except Exception:  # noqa: BLE001 - an unreadable creator grants nothing
        logger.debug("cron owner grant: creator %r unreadable", session_key, exc_info=True)
        return "", ""


def owner_grant_holds(job: Any, execution: Any, config: Any) -> bool:
    """Whether *job*'s stored Bot grant lets this fire auto-approve. From memory.

    *execution* is the record this fire runs (after the swap to the Bot's
    worker) and *config* the live configuration.
    """
    owner = getattr(job, "owner_member_id", "") or ""
    if not owner or getattr(job, "owner_approval_mode", "") != _CARRIED_MODE:
        return False
    if execution is None or config is None:
        return False
    try:
        from kiro_crew.dashboard.crewmate_approval import crewmate_name_for
        from kiro_crew.mate_worker import MATE_WORKER_AGENT

        # Only the Bot's own worker turn: a sequence step or another agent's
        # template is not what the Bot's Profile covers.
        if getattr(execution, "template_id", "") != MATE_WORKER_AGENT:
            return False
        if getattr(execution, "member_id", None) != owner:
            return False
        name = crewmate_name_for(config, execution)
        return bool(name) and _profile_mode(config, name) == _CARRIED_MODE
    except Exception:  # noqa: BLE001 - an unreadable grant asks
        logger.debug("cron owner grant: check failed for %s", getattr(job, "id", ""), exc_info=True)
        return False


def fire_approval_mode(job: Any, execution: Any, config: Any) -> str:
    """The mode a single-agent fire of *job* runs with: ``"auto"`` or ``""``.

    The job's own ``auto`` (the owner's switch, re-checked against policy), else
    its Bot grant when :func:`owner_grant_holds`.
    """
    from kiro_crew.cron_service.fields import effective_job_approval_mode

    if effective_job_approval_mode(getattr(job, "approval_mode", "")) == "auto":
        return "auto"
    return "auto" if owner_grant_holds(job, execution, config) else ""


def _proven_owner(record: dict[str, Any], config: Any) -> tuple[str, str]:
    """``(member_id, mode)`` when *record*'s maker provably is the Bot it runs as.

    All of these must agree, else ``("", "")``: the job's ``member_id``, the
    member its captured ``execution_context`` names, and the Bot whose chat or
    worker the job's ``session_key`` (the session that made it) is.
    """
    member = record.get("member_id")
    capture = record.get("execution_context")
    if not isinstance(member, str) or not member or not isinstance(capture, dict):
        return "", ""
    if capture.get("member_id") != member:
        return "", ""
    if str(record.get("created_by") or "").startswith("app:"):
        return "", ""
    session_key = record.get("session_key")
    if not isinstance(session_key, str) or not session_key:
        return "", ""
    owner, mode = creator_grant(session_key, config)
    return (owner, mode) if owner == member else ("", "")


def migrate_owner_grants(store_dir: Path, config: Any = None) -> list[str]:
    """Give each job a Bot made before ``cron_add`` stamped grants its Bot's grant, once.

    Never raises. Runs at start, before the scheduler arms, and only while
    :data:`MIGRATION_MARKER` is absent; the marker is written after a pass that
    read the store, so a second start is a no-op. A job is stamped only when
    :func:`_proven_owner` proves its maker is the Bot it runs as, with that
    Bot's Profile mode now; a job with a grant already, an unknown or a
    different maker gets nothing and keeps asking. Each fire still checks the
    Bot's live Profile (:func:`owner_grant_holds`). *config* is read from disk
    when None. Returns the stamped ids.
    """
    from kiro_crew.atomic_write import atomic_write
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.cron_service.store import _CRONS_FILE, cron_store_lock

    marker = store_dir / MIGRATION_MARKER
    path = store_dir / _CRONS_FILE
    try:
        if marker.exists():
            return []
        if not path.is_file():
            atomic_write(marker, "1\n")
            return []
        if config is None:
            config = KiroCrewConfig.load()
        with cron_store_lock(store_dir):
            data = json.loads(path.read_bytes())
            records = data.get("jobs") if isinstance(data, dict) else None
            if not isinstance(records, list):
                return []
            stamped: list[str] = []
            for record in records:
                if not isinstance(record, dict) or record.get("owner_member_id"):
                    continue
                owner, mode = _proven_owner(record, config)
                if not owner:
                    continue
                record["owner_member_id"] = owner
                record["owner_approval_mode"] = mode
                stamped.append(str(record.get("id") or ""))
            if stamped:
                atomic_write(path, json.dumps(data, indent=2))
            atomic_write(marker, "1\n")
        return stamped
    except Exception:
        logger.warning("Bot jobs made before owner grants were not stamped", exc_info=True)
        return []
