"""Keep the cron jobs saved before ``local`` became the default zone firing when they did.

Before store version :data:`LOCAL_DEFAULT_STORE_VERSION`, a job with no zone of its
own read its wall clock in the config zone, and in UTC when the config named none.
From that version on, an empty zone with an empty config zone means the host's zone
(``local``). Without this step every such job would move by the host's UTC offset on
the first load after an upgrade.

So a store written before that version has each of those jobs stamped ``UTC`` when it
is loaded: the zone it was already firing in. A job whose zone came from a configured
zone is left alone, because that meaning did not change. Only a job whose wall clock
the zone decides is stamped: a cron expression, or ``skip_dates``. An interval never
reads a zone, and a one-shot's instant was fixed when it was created.

The stamp is applied in memory on every load of an older store and reaches disk with
the next save, which writes the current version; a store that is never saved again is
stamped again on each load, with the same result. When the config cannot be read, the
zone those jobs inherited is unknown: nothing is stamped, and saves keep the store's
older version, so a later load with a readable config settles it.

An older version cannot tell those jobs from an unzoned job created, or re-zoned,
while the upgrade waits, whose empty zone already has the new meaning. So such a save
lists those later jobs under :data:`FOLLOWS_DEFAULT_KEY`, and every later load leaves
them unstamped. An older build keeps an unknown store key as it is, and a job it adds
is not listed, so that job is stamped as an older job.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any

from kiro_crew.host_timezone import is_local_timezone

if TYPE_CHECKING:
    from kiro_crew.cron_service.model import CronJob

logger = logging.getLogger("kiro_crew.cron")

#: The first store version whose empty job zone (under an empty config zone) is the host's.
LOCAL_DEFAULT_STORE_VERSION = 3

#: The version a save keeps on a store whose upgrade had to wait for a readable config.
DEFERRED_STORE_VERSION = LOCAL_DEFAULT_STORE_VERSION - 1

#: The zone an older store's unzoned jobs fired in when the config named none.
_LEGACY_DEFAULT_ZONE = "UTC"

#: The attribute a waiting upgrade sets on a job it could not stamp. In memory only:
#: ``store.job_record`` writes named fields, never this one. Read by
#: ``schedule.job_zone_name``.
LEGACY_ZONE_ATTR = "_legacy_zone"

#: The store key a save of a waiting upgrade writes: the ids of the unzoned jobs whose
#: empty zone already means the default, which no load stamps.
FOLLOWS_DEFAULT_KEY = "unzoned_follow_default"


def predates_local_default(read_version: object) -> bool:
    """Whether a store document whose ``version`` was *read_version* predates ``local``."""
    return not (
        isinstance(read_version, int)
        and not isinstance(read_version, bool)
        and read_version >= LOCAL_DEFAULT_STORE_VERSION
    )


class ConfigZoneUnknown(Exception):
    """The config could not be read, so the zone older jobs inherited is unknown."""


def configured_default_zone() -> str:
    """The config ``timezone``, from a fresh load that read the config files.

    A fresh load rather than the published snapshot: the snapshot cannot say
    whether the load behind it parsed the files, and a config that did not parse
    loads as defaults, whose empty zone is not the zone the jobs inherited.
    Pinning on it would stamp UTC on a job that followed a configured zone, for
    good. Raises :class:`ConfigZoneUnknown` when ``config.json`` or
    ``config.local.json`` did not parse, and lets a failing load raise. The store
    is loaded off the event loop or in a loop-less process, so the load blocks
    nothing.
    """
    from kiro_crew.config import KiroCrewConfig

    cfg = KiroCrewConfig.load()
    if getattr(cfg, "_base_unreadable", False) or getattr(cfg, "_overlay_unreadable", False):
        raise ConfigZoneUnknown("config.json or config.local.json did not parse")
    return cfg.timezone or ""


def _fired_in_utc(timezone: object, kind: object, skip_dates: object, config_zone: str) -> bool:
    """The one rule: an unzoned job whose wall clock the zone decides, under no configured zone."""
    zone = (config_zone or "").strip()
    if zone and not is_local_timezone(zone):
        return False
    if isinstance(timezone, str) and timezone.strip():
        return False
    return kind == "cron" or bool(skip_dates)


def _log_pinned(pinned: list[str]) -> None:
    if pinned:
        logger.info(
            "Cron store upgrade: %d job(s) with no time zone keep firing in UTC "
            "(an unset zone now follows this computer): %s",
            len(pinned),
            ", ".join(pinned),
        )


def follows_default_ids(extra: Mapping[str, Any]) -> frozenset[str]:
    """The job ids a store's :data:`FOLLOWS_DEFAULT_KEY` lists; none when it is absent or malformed."""
    ids = extra.get(FOLLOWS_DEFAULT_KEY)
    if not isinstance(ids, list):
        return frozenset()
    return frozenset(job_id for job_id in ids if isinstance(job_id, str))


def pin_jobs_read_in_utc(
    jobs: Iterable[CronJob], config_zone: str, follows_default: frozenset[str] = frozenset()
) -> list[str]:
    """Stamp ``UTC`` on each job of an older store that fired in UTC. Returns their ids.

    Nothing is stamped when *config_zone* names a zone: those jobs inherited it then
    and still do. A ``local`` config zone counts as none, since an older build could
    not resolve it and fell back to UTC. A job in *follows_default* was saved with the
    new meaning while the upgrade waited, and is not stamped.
    """
    pinned: list[str] = []
    for job in jobs:
        if job.id in follows_default:
            continue
        if _fired_in_utc(job.timezone, job.schedule.kind, job.skip_dates, config_zone):
            job.timezone = _LEGACY_DEFAULT_ZONE
            pinned.append(job.id)
    _log_pinned(pinned)
    return pinned


def hold_jobs_on_utc_in_memory(
    jobs: Iterable[CronJob], follows_default: frozenset[str] = frozenset()
) -> list[str]:
    """While an upgrade waits for a readable config, run each older job it would stamp in UTC.

    An older build read a config that did not parse as defaults, so these jobs ran
    in UTC then too. Nothing is stamped: the zone they inherited is unknown until
    the config can be read, and a stamp would reach disk with the next save. A job
    in *follows_default* is not an older job, and is not held.
    """
    held: list[str] = []
    for job in jobs:
        if job.id in follows_default:
            continue
        if _fired_in_utc(job.timezone, job.schedule.kind, job.skip_dates, ""):
            setattr(job, LEGACY_ZONE_ATTR, _LEGACY_DEFAULT_ZONE)
            held.append(job.id)
    return held


def release_legacy_zone(job: CronJob) -> None:
    """Clear the in-memory hold on *job*: a write gave it a zone with the new meaning.

    Cleared to ``""``, never deleted: ``CronService._update_job_locked`` applies an
    update to a copy and copies back only the attributes it changed, so a deleted
    one would stay on the live job.
    """
    if getattr(job, LEGACY_ZONE_ATTR, ""):
        setattr(job, LEGACY_ZONE_ATTR, "")


def store_extra_for_save(
    extra: Mapping[str, Any],
    jobs: Iterable[CronJob],
    *,
    deferred: bool,
    skipped: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """The top-level keys a save writes besides ``version`` and ``jobs``.

    While the upgrade waits (*deferred*), :data:`FOLLOWS_DEFAULT_KEY` lists each
    unzoned job the upgrade would stamp that is not held: one created or re-zoned
    since the load, whose empty zone already means the default. A *skipped* record
    the save keeps stays listed when *extra* listed it, so the job it becomes once
    fixed by hand is not stamped either. Otherwise the key is dropped, since a
    current store has no older jobs left to tell apart.
    """
    kept = {key: value for key, value in extra.items() if key != FOLLOWS_DEFAULT_KEY}
    if deferred:
        listed = follows_default_ids(extra)
        ids = [
            job.id
            for job in jobs
            if not getattr(job, LEGACY_ZONE_ATTR, "")
            and _fired_in_utc(job.timezone, job.schedule.kind, job.skip_dates, "")
        ]
        ids += [
            job_id
            for record in skipped
            if isinstance(job_id := record.get("id"), str) and job_id in listed
        ]
        if ids:
            # A kept record can share a loaded job's id: list each id once.
            kept[FOLLOWS_DEFAULT_KEY] = list(dict.fromkeys(ids))
    return kept


def pin_records_read_in_utc(
    records: Iterable[object], config_zone: str, follows_default: frozenset[str] = frozenset()
) -> list[str]:
    """:func:`pin_jobs_read_in_utc` for raw store records (a document not yet decoded)."""
    pinned: list[str] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        # A record the loader skipped can carry any id; only a string can be listed.
        job_id = record.get("id")
        if isinstance(job_id, str) and job_id in follows_default:
            continue
        schedule = record.get("schedule")
        kind = schedule.get("kind") if isinstance(schedule, dict) else None
        if _fired_in_utc(record.get("timezone"), kind, record.get("skip_dates"), config_zone):
            record["timezone"] = _LEGACY_DEFAULT_ZONE
            pinned.append(str(record.get("id", "")))
    _log_pinned(pinned)
    return pinned


def settle_documents_before_merge(src: dict, dst: dict) -> bool:
    """Stamp each older document's unzoned jobs before records move between two stores.

    A merge folds *src*'s records into *dst* and writes them under *dst*'s version,
    so an older record would be read with the newer meaning of an empty zone, and a
    newer record folded into an older *dst* would be stamped on that store's next
    load. Each document older than :data:`LOCAL_DEFAULT_STORE_VERSION` has its own
    unzoned jobs stamped first, and *dst* is then marked current. ``False``, with
    nothing changed, when an older document is involved and the config cannot be
    read: the zone its jobs inherited is unknown, so the merge must wait.
    """
    older = [doc for doc in (src, dst) if predates_local_default(doc.get("version"))]
    if not older:
        return True
    try:
        zone = configured_default_zone()
    except Exception:
        logger.warning("Cron merge: config unreadable, older jobs cannot be settled", exc_info=True)
        return False
    for doc in older:
        jobs = doc.get("jobs")
        pin_records_read_in_utc(
            jobs if isinstance(jobs, list) else [], zone, follows_default_ids(doc)
        )
    if predates_local_default(dst.get("version")):
        dst["version"] = LOCAL_DEFAULT_STORE_VERSION
    dst.pop(FOLLOWS_DEFAULT_KEY, None)
    return True
