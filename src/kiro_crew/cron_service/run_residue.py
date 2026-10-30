"""Remove old per-run transcripts a stateless cron job leaves behind.

A job with ``persistent_session=False`` runs each fire under a fresh key,
``cron:<job_id>:<run_id>``, so every fire writes its own
``cron_<job_id>_<run_id>.jsonl`` (plus a ``.lock`` sidecar and sometimes an
``.attachments`` directory) at the top of the sessions directory. Nothing reads
them once the run ends: the job's chat tab shows only the stable ``cron:<job_id>``
transcript and run results live in the cron history store. Archive retention only
covers ``sessions/archive``, so without this prune the files accumulate without
bound, and every full session listing pays one stat per file.

A file is removed only when all of these hold:

* its name is exactly ``cron_<8 hex>_<8 hex>.jsonl``;
* its job exists and the job's key is not stable
  (:func:`~kiro_crew.cron_service.identity.cron_session_key_is_stable`), because a
  multi-agent ``agent_sequence`` job uses a durable three-part key of the same shape;
* no live or retained run holds the key (``CronService.active_session_keys``);
* it has not been written for :data:`RUN_TRANSCRIPT_TTL_SECS`;
* it is not pinned.

Deletion goes through :meth:`ConversationLog.delete_session`, which takes the
transcript lock, drops the attachments and invalidates the caches; the staging
sweep then reclaims the orphaned ``.lock``.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Container, Mapping
from typing import TYPE_CHECKING

from kiro_crew.cron_service.identity import cron_session_key_is_stable

if TYPE_CHECKING:
    from kiro_crew.cron_service.model import CronJob
    from kiro_crew.history import ConversationLog

logger = logging.getLogger("kiro_crew.cron")

#: How long a finished run's transcript is kept for debugging.
RUN_TRANSCRIPT_TTL_SECS = 7 * 86400
#: Files removed per pass, so a large backlog drains over several passes
#: instead of holding a worker thread for minutes.
RUN_TRANSCRIPT_PRUNE_CAP = 200
_RUN_TRANSCRIPT = re.compile(r"^cron_([0-9a-f]{8})_([0-9a-f]{8})\.jsonl$")


def prune_stale_run_transcripts(
    *,
    jobs: Mapping[str, CronJob],
    active_keys: Container[str],
    now: float,
    log: ConversationLog | None = None,
    ttl_secs: float = RUN_TRANSCRIPT_TTL_SECS,
    cap: int = RUN_TRANSCRIPT_PRUNE_CAP,
) -> int:
    """Delete stale per-run cron transcripts; return how many were removed.

    Blocking file I/O: call it off the event loop.
    """
    if log is None:
        from kiro_crew.history import ConversationLog

        log = ConversationLog()
    removed = 0
    try:
        with os.scandir(os.fspath(log._dir)) as entries:
            candidates = [e for e in entries if _RUN_TRANSCRIPT.match(e.name)]
    except OSError:
        return 0
    for entry in candidates:
        if removed >= cap:
            break
        match = _RUN_TRANSCRIPT.match(entry.name)
        if match is None:  # pragma: no cover - filtered above
            continue
        job_id, run_id = match.groups()
        key = f"cron:{job_id}:{run_id}"
        if key in active_keys:
            continue
        job = jobs.get(job_id)
        if job is None or cron_session_key_is_stable(job):
            continue
        try:
            if not entry.is_file(follow_symlinks=False):
                continue
            if now - entry.stat(follow_symlinks=False).st_mtime < ttl_secs:
                continue
        except OSError:
            continue
        try:
            if log.delete_session(key, skip_pinned=True):
                removed += 1
        except Exception:
            logger.debug("cron run transcript prune failed for %s", key, exc_info=True)
    if removed:
        logger.info("Pruned %d finished cron run transcript(s) older than 7 days", removed)
    return removed
