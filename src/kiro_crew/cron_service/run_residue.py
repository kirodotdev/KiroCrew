"""Remove old per-run transcripts a stateless cron job leaves behind.

A job with ``persistent_session=False`` runs each fire under a fresh key,
``cron:<job_id>:<run_id>``, so every fire writes its own
``cron_<job_id>_<run_id>.jsonl`` (plus a ``.lock`` sidecar and sometimes an
``.attachments`` directory) at the top of the sessions directory. Nothing reads
them once the run ends: the job's chat tab shows only the stable ``cron:<job_id>``
transcript and run results live in the cron history store. The exception is a
``new_chat_per_run`` run that opened a tab of its own: its transcript is that
tab's, so it is re-tagged ``tab`` and passed by. Archive retention only
covers ``sessions/archive``, so without this prune the files accumulate without
bound, and every full session listing pays one stat per file.

An age limit alone does not bound the pile: a job that fires every minute
leaves about 10,000 files inside seven days, and tens of thousands of them make
a gateway's session listing too slow to answer. So each job also keeps only its
:data:`RUN_TRANSCRIPT_KEEP_PER_JOB` newest run files, which leaves recent runs
to debug however often the job fires.

A file is removed only when all of these hold:

* its name is exactly ``cron_<8 hex>_<8 hex>.jsonl``;
* its metadata carries ``cron_run="ephemeral"``, recorded when the run starts
  (after the fire-time gate and admission, once the key is active), OR it carries
  no ``cron_run`` tag and holds nothing besides its metadata line. Builds before
  the tag wrote one such file per fire and nothing ever drained them; a durable
  sequence transcript, or any untagged file someone wrote a message into, holds
  rows and is retained, as is a ``tab``-tagged one;
* no live or retained run holds the key (``CronService.active_session_keys``);
* it has not been written for :data:`RUN_TRANSCRIPT_TTL_SECS`, or the same job
  has at least :data:`RUN_TRANSCRIPT_KEEP_PER_JOB` newer run files;
* it is not pinned.

Deletion goes through :meth:`ConversationLog.delete_session`, which takes the
transcript lock, drops the attachments and invalidates the caches; the staging
sweep then reclaims the orphaned ``.lock``.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Container
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.history import ConversationLog

logger = logging.getLogger("kiro_crew.cron")

#: How long a finished run's transcript is kept for debugging.
RUN_TRANSCRIPT_TTL_SECS = 7 * 86400
#: How many of each job's newest run transcripts are kept whatever their age.
RUN_TRANSCRIPT_KEEP_PER_JOB = 20
#: Files removed per pass, so a large backlog drains over several passes
#: instead of holding a worker thread for minutes.
RUN_TRANSCRIPT_PRUNE_CAP = 2000
_RUN_TRANSCRIPT = re.compile(r"^cron_([0-9a-f]{8})_([0-9a-f]{8})\.jsonl$")


def prune_stale_run_transcripts(
    *,
    active_keys: Container[str],
    now: float,
    log: ConversationLog | None = None,
    ttl_secs: float = RUN_TRANSCRIPT_TTL_SECS,
    cap: int = RUN_TRANSCRIPT_PRUNE_CAP,
) -> int:
    """Delete stale per-run cron transcripts; return how many were removed.

    A return equal to *cap* means more may be waiting.
    Blocking file I/O: call it off the event loop.
    """
    if log is None:
        from kiro_crew.history import ConversationLog

        log = ConversationLog()
    removed = 0
    # job id -> [(mtime, run id)], so each job's files can be ranked newest first.
    by_job: dict[str, list[tuple[float, str]]] = {}
    try:
        with os.scandir(os.fspath(log._dir)) as entries:
            for entry in entries:
                match = _RUN_TRANSCRIPT.match(entry.name)
                if match is None:
                    continue
                try:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    mtime = entry.stat(follow_symlinks=False).st_mtime
                except OSError:
                    continue
                job_id, run_id = match.groups()
                by_job.setdefault(job_id, []).append((mtime, run_id))
    except OSError:
        return 0
    for job_id, runs in by_job.items():
        runs.sort(reverse=True)
        for rank, (mtime, run_id) in enumerate(runs):
            if removed >= cap:
                break
            if rank < RUN_TRANSCRIPT_KEEP_PER_JOB and now - mtime < ttl_secs:
                continue
            key = f"cron:{job_id}:{run_id}"
            if key in active_keys:
                continue
            removed += _remove_if_finished_run(log, key, active_keys)
    if removed:
        logger.info("Pruned %d finished cron run transcript(s)", removed)
    return removed


def _remove_if_finished_run(log: ConversationLog, key: str, active_keys: Container[str]) -> int:
    """Delete one finished, unpinned run transcript; return 1 when it was removed.

    The verdict and the delete share one hold of the transcript lock, so a row
    written between them cannot be deleted with the file. *active_keys* is asked
    again inside that hold: a sequence step can register its stable key while the
    pass runs, and it writes its execution record under the same lock.
    """
    try:
        with log._locked(key):
            if key in active_keys or not _is_finished_run(log, key):
                return 0
            return 1 if log.delete_session(key, skip_pinned=True) else 0
    except Exception:
        logger.debug("cron run transcript prune failed for %s", key, exc_info=True)
        return 0


def _is_finished_run(log: ConversationLog, key: str) -> bool:
    """Whether *key*'s transcript is a run file nobody reads: tagged, or legacy and empty."""
    tag = log.get_metadata(key).get("cron_run")
    if tag == "ephemeral":
        return True
    return tag is None and not log.has_messages(key)
