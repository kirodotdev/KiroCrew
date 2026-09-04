"""The restore path: put the authority files in place before the backend starts.

One public seam, ``run_restore(settings)`` (see ``container/CONTRACT.md``). The
supervisor (Track S3) calls it and lets it run to completion BEFORE the backend
process starts. That ordering is a correctness requirement, not a preference:
the backend's periodic flush of ``open_slots.json`` will overwrite whatever is
in place with its in-memory slot table, so any file that lands after the backend
starts is at risk of being erased (design 9.1, ``dashboard_persistence.py``).

Restore downloads the ``config/`` namespace and NOTHING else. It used to
download every object under ``crews/<crew>/``, which put every conversation the
crew had ever had on one task's disk where the backend could read all of them:
isolation is per CREW via the IAM prefix, not per CUSTOMER, so for a crew
serving many customers that was the wrong shape (``EPHEMERAL-CONTRACT.md``).

The property this achieves, stated exactly: **a task only ever holds the
conversations it itself served, and loses them when it exits.** Not "the task
holds nothing" — the backend's session store IS a filesystem, so a served
conversation is on disk while it is being served. The disk is scratch, not a
store. A reader told nothing is kept, who then finds a jsonl file, has been
misled.

``session_map.json`` and ``open_slots.json`` are the exception, and cannot be
ephemeral: per ``container/common/config.py:120``, without ``session_map.json``
there is no resume at all, and ``open_slots.json`` is the authoritative record
of which conversations exist. They are Kiro Crew's files; we only name their
paths. Both are small, so the restore stays fast.

The backup unit is still ONE unit, so an incomplete set is still reported. A
missing authority file is a degraded boot -- no resume, or no conversation list
-- and restore says so in the returned result and at error level in the log.
Absent transcripts are NOT a degradation: they are absent by design, and the
front process fetches this slot's transcript on demand (Track B).
"""

from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from ..common import Settings
from . import layout
from .store import ObjectStore, S3ObjectStore

logger = logging.getLogger("smc.backup.restore")

#: The one machine-readable line restore always emits, exactly once per call.
#:
#: TREAT THIS AS AN INTERFACE, NOT A LOG MESSAGE. A deploy gate greps the boot
#: log for it to prove no transcript was restored, so the token and the field
#: names are a contract: add fields at the end, never rename or reorder one, and
#: never make the line conditional. It is emitted for every outcome (ok,
#: partial, empty, disabled) so an absent line means restore did not run at all,
#: which is a different failure from restore reporting zero.
SUMMARY_TOKEN = "restore: SUMMARY"

__all__ = ["run_restore", "RestoreResult", "SUMMARY_TOKEN"]


@dataclass
class RestoreResult:
    """What a restore actually put in place, and whether it is trustworthy.

    ``partial`` is the load-bearing field: True means an authoritative file
    (``session_map.json`` or ``open_slots.json``) was missing, so resume or the
    conversation list is degraded. ``empty`` (no objects at all under the crew's
    prefix) is a clean first boot, not a partial restore.

    ``transcripts_restored`` must be 0 and is COUNTED, never assumed: it is
    incremented at the write site from ``layout.is_transcript``, so reintroducing
    a bulk restore moves the number instead of leaving a hardcoded zero for the
    gate to read. ``transcripts_available`` is how many transcripts the bucket
    holds that were deliberately left there, which is what makes the zero
    meaningful -- zero restored out of zero available proves nothing.
    """

    restored: int = 0
    restored_bytes: int = 0
    skipped: int = 0
    disabled: bool = False
    empty: bool = False
    partial: bool = False
    missing: list[str] = field(default_factory=list)
    transcripts_restored: int = 0
    transcripts_available: int = 0

    @property
    def ok(self) -> bool:
        return not self.partial and not self.disabled

    @property
    def state(self) -> str:
        """One word for the summary line. Worst-news-first precedence."""
        if self.disabled:
            return "disabled"
        if self.partial:
            return "partial"
        if self.empty:
            return "empty"
        return "ok"


def _log_summary(result: RestoreResult) -> None:
    """Emit the gate's line. See :data:`SUMMARY_TOKEN` before changing it."""
    logger.info(
        "%s state=%s transcripts_restored=%d transcripts_available=%d "
        "config_restored=%d restored_bytes=%d skipped=%d missing=%s",
        SUMMARY_TOKEN,
        result.state,
        result.transcripts_restored,
        result.transcripts_available,
        result.restored,
        result.restored_bytes,
        result.skipped,
        ",".join(result.missing) if result.missing else "none",
    )


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".restore-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _build_store(settings: Settings) -> ObjectStore | None:
    if not settings.backup_bucket:
        return None
    return S3ObjectStore(settings.backup_bucket)


def run_restore(
    settings: Settings,
    *,
    store: ObjectStore | None = None,
) -> RestoreResult:
    """Download the ``config/`` namespace into place. Runs to completion.

    Returns a :class:`RestoreResult`. A partial restore is reported, not raised:
    the design tolerates a degraded restore (a lost recent turn is accepted),
    and blocking the container forever on a partial set would be worse than
    booting with a visible, reported degradation. The supervisor decides what
    to do with a ``partial`` result.
    """
    if store is None:
        store = _build_store(settings)
    if store is None:
        logger.warning(
            "restore: SMC_BACKUP_BUCKET is not set; nothing to restore. Starting "
            "with an empty data home."
        )
        result = RestoreResult(disabled=True)
        _log_summary(result)
        return result

    prefix = layout.object_prefix(settings)
    # One LIST of the whole crew prefix, deliberately not narrowed to config/.
    # It downloads no content -- keys and sizes only -- and it buys two things a
    # narrowed list cannot: a bucket holding transcripts but no authority file
    # is reported as PARTIAL rather than misread as a clean first boot, and the
    # summary line can say how many transcripts were left behind, without which
    # the gate's zero is unfalsifiable.
    listing = store.list(prefix)

    result = RestoreResult()
    if not listing:
        result.empty = True
        logger.info("restore: bucket has no objects under %r; clean first boot", prefix)
        _log_summary(result)
        return result

    restored_rel: set[str] = set()
    # The ONLY objects restore has any reason to write, named by the same function
    # the backup side uses so the two cannot drift. This is an enumerated set, not
    # the `config/` namespace predicate it used to be, and the difference is a
    # security boundary rather than a tidiness preference: the task role can WRITE
    # to this bucket in persistent mode, so a prompt injection that talks the agent
    # into uploading `config/security_policy.json` would, under a namespace test,
    # have that object restored on the next boot and read by the backend before it
    # loads -- letting the agent raise its own governance ceiling. A key that is not
    # one of these two is skipped no matter how plausible its name.
    #
    # Note this is the OPPOSITE choice from `is_fetchable_slot_id` in the front,
    # which deliberately uses a permissive shape test. The threat models are
    # mirrored: there the writer is trusted and being wrong restrictively would
    # serve empty history that the sidecar then makes permanent, while here the
    # writer is untrusted and being wrong permissively hands over the ceiling.
    allowed_config = set(layout.config_keys(settings).values())
    for full_key in sorted(listing):
        rel = layout.rel_from_full(settings, full_key)
        if rel is None:
            result.skipped += 1
            continue
        if layout.is_transcript(settings, rel):
            result.transcripts_available += 1
        if rel not in allowed_config:
            # Everything else stays in the bucket: transcripts because a task must
            # not hold a conversation it did not serve, artifacts because they are
            # one customer's content too and the backend's artifact store
            # enumerates its whole directory with no per-customer scope, and any
            # other `config/` object because nothing authorises it to exist.
            # Skipping is not deletion -- the sidecar only ever uploads, so the
            # object is still there for the on-demand fetch.
            result.skipped += 1
            continue
        tail = rel.split("/", 1)[1] if "/" in rel else rel
        if layout.is_excluded(tail):
            result.skipped += 1
            continue
        local = layout.local_path_for_key(settings, rel)
        if local is None:
            logger.warning("restore: dropping unroutable key %r", full_key)
            result.skipped += 1
            continue
        data = store.get(full_key)
        _atomic_write(local, data)
        restored_rel.add(rel)
        result.restored += 1
        result.restored_bytes += len(data)
        if layout.is_transcript(settings, rel):
            # Unreachable while the namespace gate above holds. Counted anyway,
            # because the gate reads this number: a hardcoded zero would stay
            # zero after someone reintroduces the bulk restore.
            result.transcripts_restored += 1

    # Completeness: the two authoritative files must be present.
    required = layout.config_keys(settings)
    missing = [role for role, key in required.items() if key not in restored_rel]
    if missing:
        result.partial = True
        result.missing = missing
        logger.error(
            "restore: PARTIAL restore — %d config objects restored but missing %s. "
            "Resume and/or the conversation list are degraded. session_map.json "
            "missing => no resume; open_slots.json missing => the conversation "
            "list is lost. Absent transcripts are NOT this: they are absent by "
            "design and fetched on demand.",
            result.restored,
            ", ".join(f"{r} ({required[r]})" for r in missing),
        )
    else:
        logger.info(
            "restore: complete — %d config objects (%d B) restored, %d objects "
            "left in the bucket (%d of them transcripts, fetched on demand)",
            result.restored,
            result.restored_bytes,
            result.skipped,
            result.transcripts_available,
        )
    _log_summary(result)
    return result
