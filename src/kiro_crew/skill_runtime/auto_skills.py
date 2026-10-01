"""The ``auto/`` namespace: generated skills, their lifecycle and the pending queue.

Creates and refines generated skills, archives and restores them, and bounds the
set by inactivity and count. The live tree and the pending queue are one slug
space with one allocator: ``create_auto_skill``, ``stage_skill_candidate`` and
``restore_auto_skill`` each test a name with ``_auto_slug_available`` under
``_auto_slug_claim_lock``. Also stages candidates and removes them from the queue.

Reading, judging and approving a queued candidate stays in the facade, which is
the registered redaction sink for pending candidate content.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Iterator, Literal

if TYPE_CHECKING:
    from kiro_crew.skills import AutoSkillProvenance, ClaimRefusal, SkillsLoader

logger = logging.getLogger("kiro_crew.skills")


def is_auto_generated(loader: SkillsLoader, name: str) -> bool:
    """Return True if *name* refers to a skill in the auto namespace.

    Cheap filesystem check (no frontmatter parse) based on the
    directory prefix.  Used for filtering and safety guards (e.g.
    refusing to overwrite a hand-authored skill from an auto-update
    path).
    """
    from kiro_crew import skills as sk  # circular import: the facade imports this module

    if not loader._safe_name(name):
        return False
    return name.startswith(f"{sk.AUTO_SKILL_NAMESPACE}/")


def find_similar(
    loader: SkillsLoader,
    description: str,
    threshold: float = 0.85,
    *,
    exclude: str = "",
) -> str | None:
    """Return the name of an existing skill whose description overlaps with *description*.

    Uses case-insensitive word-set Jaccard-like overlap against every
    loaded skill's ``description`` frontmatter value:

        score = |words(a) ∩ words(b)| / |words(a) ∪ words(b)|

    Intended for deduplication of auto-generated skills — we don't
    want the agent producing a near-duplicate of an existing skill.
    Returns the first skill whose score ≥ *threshold*, or ``None``
    if nothing matches.

    *exclude* lets callers suppress self-matches during refinement.
    """
    if not description:
        return None
    query_words = set(re.findall(r"\w+", description.lower()))
    if not query_words:
        return None
    best_name: str | None = None
    best_score: float = 0.0
    for name, skill_file, _within in loader._iter():
        if exclude and name == exclude:
            continue
        meta = loader._cached_frontmatter(skill_file, within=_within)
        existing = meta.get("description", "")
        if not existing:
            continue
        existing_words = set(re.findall(r"\w+", existing.lower()))
        if not existing_words:
            continue
        intersection = query_words & existing_words
        union = query_words | existing_words
        score = len(intersection) / len(union) if union else 0.0
        if score > best_score:
            best_score = score
            best_name = name
    if best_score >= threshold:
        return best_name
    return None


def create_auto_skill(
    loader: SkillsLoader,
    slug: str,
    *,
    description: str,
    triggers: str,
    procedure_md: str,
    provenance: AutoSkillProvenance,
    refusal: ClaimRefusal | None = None,
) -> str | None:
    """Write a new auto-generated skill under ``auto/<slug>/SKILL.md``.

    Returns the full skill name (``auto/<slug>``) on success, or ``None`` if
    the slug is invalid, a live skill of that name exists, or a NEW candidate
    of that slug is awaiting review in the pending queue (publishing over a
    queued candidate's promotion destination would strand it). A pending
    UPDATE candidate does not hold the slug, since it is promoted over the
    live target named in its metadata.

    Caller is responsible for:
    - Running ``find_similar()`` first to avoid near-duplicates.
    - Passing already-redacted ``procedure_md`` (sensitive data is
      the caller's responsibility — this method is pure I/O).
    - Enforcing the ``skills.auto_create_from_sessions`` config flag.
    """
    from kiro_crew import skills as sk  # circular import: the facade imports this module

    if not sk._AUTO_NAME_PATTERN.match(slug):
        logger.warning("Rejected auto skill: slug %r failed validation", slug)
        return None
    if len(procedure_md) > sk.AUTO_SKILL_MAX_PROCEDURE_CHARS:
        logger.warning(
            "Rejected auto skill %s: procedure %d chars exceeds cap %d",
            slug,
            len(procedure_md),
            sk.AUTO_SKILL_MAX_PROCEDURE_CHARS,
        )
        return None
    name = f"{sk.AUTO_SKILL_NAMESPACE}/{slug}"
    skill_dir = loader._dir / name
    content = sk._build_auto_skill_content(
        slug=slug,
        description=description,
        triggers=triggers,
        procedure_md=procedure_md,
        provenance=provenance,
    )
    # Test and claim under ONE lock, shared with ``stage_skill_candidate``:
    # the two paths allocate in different directories, so nothing an atomic
    # mkdir can do makes the cross-namespace pair safe on its own.
    with loader._auto_slug_claim_lock() as locked:
        if not locked:
            if refusal is not None:
                refusal.retryable = True
            logger.info("Auto skill %s not created: the slug claim lock is unavailable", name)
            return None
        if skill_dir.exists():
            logger.info("Auto skill %s already exists, skipping", name)
            return None
        if not loader._auto_slug_available(slug, claim="live"):
            # The pending queue holds this slug for a NEW candidate, so
            # ``auto/<slug>`` is that candidate's promotion destination.
            # Publishing over it strands it for good: ``approve_pending_skill``
            # refuses it while the live directory stands, and TTL pruning then
            # deletes it unreviewed. One side of the collision has to lose, and
            # the publish is the cheaper loss: it goes back to the caller as a
            # ``None`` the caller audits as a rejection, whereas the queued
            # candidate is immutable, human-gated work that would disappear with
            # no record at all. Consolidation advances its message offset
            # whatever one candidate's outcome, so neither side is retried from
            # the same sessions; the choice is which loss leaves a trail.
            logger.info(
                "Auto skill %s not created: that slug is awaiting review in the pending queue",
                name,
            )
            return None
        try:
            skill_dir.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            # A concurrent writer holding no lock owns this directory (the
            # lock is advisory, so the mkdir is the real claim). Refusing is
            # the safe side: overwriting destroys their content.
            logger.info("Auto skill %s claimed concurrently, skipping", name)
            return None
    (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
    loader._invalidate_iter_cache()  # new skill visible to trigger matching now
    logger.info("Created auto skill: %s", name)
    return name


def update_auto_skill(
    loader: SkillsLoader,
    name: str,
    *,
    description: str,
    triggers: str,
    procedure_md: str,
    provenance: AutoSkillProvenance,
) -> bool:
    """Update an existing auto-generated skill with a refined procedure.

    Refuses to overwrite skills NOT in the auto namespace — protects
    hand-authored skills from being clobbered by the refine path.
    Returns True on success.

    Caller is responsible for passing already-redacted ``procedure_md``.
    """
    from kiro_crew import skills as sk  # circular import: the facade imports this module

    if not loader.is_auto_generated(name):
        logger.warning(
            "Refusing to auto-refine non-auto skill: %s (not in %s/)",
            name,
            sk.AUTO_SKILL_NAMESPACE,
        )
        return False
    skill_file = loader._dir / name / "SKILL.md"
    if not skill_file.exists():
        return False
    if len(procedure_md) > sk.AUTO_SKILL_MAX_PROCEDURE_CHARS:
        logger.warning(
            "Refusing to refine %s: procedure %d chars exceeds cap %d",
            name,
            len(procedure_md),
            sk.AUTO_SKILL_MAX_PROCEDURE_CHARS,
        )
        return False
    # Preserve the original creation timestamp — refinement must not
    # clobber provenance history.  Callers typically pass a fresh
    # provenance with created_at=now; we override from the existing
    # frontmatter here so the write path is authoritative.  Uses
    # ``dataclasses.replace`` because AutoSkillProvenance is frozen.
    existing_meta = loader._cached_frontmatter(skill_file, within=None)
    original_created_at = existing_meta.get("created_at")
    if original_created_at:
        provenance = replace(provenance, created_at=original_created_at)
    slug = name.split("/", 1)[1]
    content = sk._build_auto_skill_content(
        slug=slug,
        description=description,
        triggers=triggers,
        procedure_md=procedure_md,
        provenance=provenance,
    )
    # Re-emit the lifecycle lines ``_build_auto_skill_content`` does not know
    # about. Dropping ``version`` would make the next update-approval read the
    # skill as v1 and overwrite an existing ``.versions/v1-SKILL.md`` snapshot;
    # dropping ``pinned`` would silently remove the skill's archival exemption;
    # dropping ``inject_on_trigger`` would turn full-body injection back on for
    # a skill the user had made pointer-only — a setting undoing itself behind
    # an unrelated refine.
    _carry: list[str] = []
    _ver = existing_meta.get("version", "")
    try:
        _vn = int(_ver)
    except (TypeError, ValueError):
        _vn = 0
    if _vn > 1:
        _carry.append(f"version: {_vn}")
    if str(existing_meta.get("pinned", "")).strip().lower() in ("true", "1", "yes"):
        _carry.append("pinned: true")
    if str(existing_meta.get("inject_on_trigger", "")).strip().lower() == "false":
        _carry.append("inject_on_trigger: false")
    if _carry:
        content = content.replace("\n---\n", "\n" + "\n".join(_carry) + "\n---\n", 1)
    skill_file.write_text(content, encoding="utf-8")
    loader._invalidate_iter_cache()  # so the refined triggers/description apply now
    logger.info("Refined auto skill: %s", name)
    return True


def list_auto_skills(loader: SkillsLoader) -> list[dict]:
    """Return metadata dicts for all skills under the auto namespace.

    Dashboard / CLI consumers use this to display provenance to
    users.  Hand-authored skills are excluded.
    """
    from kiro_crew import skills as sk  # circular import: the facade imports this module

    return [s for s in loader.list_skills() if s["key"].startswith(f"{sk.AUTO_SKILL_NAMESPACE}/")]


def _cron_referenced_skills() -> set[str]:
    """Skill keys referenced by any cron job (best-effort, never raises).

    A skill a cron job depends on must never be archived out from under it.
    Any import/read failure yields an empty set (no protection, no crash).
    """
    from kiro_crew import skills as sk  # circular import: the facade imports this module

    try:  # pragma: no cover - cron reference API is environment-dependent
        return set(sk.referenced_skill_names())
    except Exception:
        return set()


def _auto_created_ts(loader: SkillsLoader, meta: dict) -> float:
    """Parse ``created_at`` frontmatter to a unix timestamp, else 0.0."""
    raw = meta.get("created_at", "")
    if not raw:
        return 0.0
    try:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (TypeError, ValueError):
        return 0.0


def _auto_activity(loader: SkillsLoader, key: str, path_str: str, meta: dict) -> tuple[int, float]:
    """Return ``(hits, anchor_ts)`` for an auto-skill.

    ``anchor_ts`` is the most recent evidence of relevance: last recorded
    use, else the created_at frontmatter, else the file mtime — so a
    never-used-but-freshly-created skill is not treated as ancient.
    """
    hits = 0
    last_seen = 0.0
    if loader._usage is not None:
        try:
            hits_f, last_seen = loader._usage.score(key)
            hits = int(hits_f)
        except Exception:
            hits, last_seen = 0, 0.0
    anchor = last_seen or loader._auto_created_ts(meta)
    if not anchor:
        try:
            anchor = Path(path_str).stat().st_mtime
        except OSError:
            anchor = 0.0
    return hits, anchor


def _archive_root(loader: SkillsLoader) -> Path:
    from kiro_crew import skills as sk  # circular import: the facade imports this module

    return loader._dir / sk.AUTO_SKILL_NAMESPACE / sk.AUTO_ARCHIVE_DIRNAME


def _is_pending_slug_safe(slug: str) -> bool:
    """Strict guard for a single-segment auto-skill slug.

    Rejects empty, ``.``/``..``, leading-dot, and any separator/traversal —
    so e.g. ``dismiss_pending_skill(".")`` can't collapse to the pending
    root and wipe the whole queue.
    """
    return (
        bool(slug)
        and slug not in (".", "..")
        and not slug.startswith(".")
        and "/" not in slug
        and "\\" not in slug
        and ".." not in slug
    )


def archive_auto_skill(loader: SkillsLoader, name: str) -> bool:
    """Move an auto-skill into the archive (recoverable, never deleted).

    Refuses non-auto skills. Returns True on success.
    """
    if not loader.is_auto_generated(name):
        return False
    slug = name.split("/", 1)[1]
    src = loader._dir / name
    if not src.is_dir():
        return False
    dest = loader._archive_root() / slug
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        # Never destroy a recoverable archive: a same-slug skill was
        # archived before. Version the destination so the prior copy
        # survives (archive-not-delete contract).
        i = 2
        while (loader._archive_root() / f"{slug}-{i}").exists():
            i += 1
        dest = loader._archive_root() / f"{slug}-{i}"
    shutil.move(str(src), str(dest))
    loader._invalidate_iter_cache()
    logger.info("Archived auto skill: %s", name)
    return True


def restore_auto_skill(loader: SkillsLoader, slug: str) -> str | None:
    """Restore an archived auto-skill back to ``auto/<slug>``.

    Returns the restored skill name, or None if not found / name clash.

    A restore is a THIRD claim on the live half of the slug space, so it runs
    the same availability test under the same lock as a publish and a staging
    walk: moving an archived skill onto a queued NEW candidate's promotion
    destination strands that candidate exactly as a publish would, and an
    unacquired lock is a refusal rather than a licence to claim uncoordinated.
    """
    from kiro_crew import skills as sk  # circular import: the facade imports this module

    if not loader._is_pending_slug_safe(slug):
        return None
    src = loader._archive_root() / slug
    if not src.is_dir():
        return None
    name = f"{sk.AUTO_SKILL_NAMESPACE}/{slug}"
    dest = loader._dir / name
    with loader._auto_slug_claim_lock() as locked:
        if not locked:
            logger.warning("Cannot restore %s: the slug claim lock is unavailable", name)
            return None
        if dest.exists():
            logger.warning("Cannot restore %s: a live skill already exists", name)
            return None
        if not loader._auto_slug_available(slug, claim="live"):
            logger.warning(
                "Cannot restore %s: that slug is awaiting review in the pending queue", name
            )
            return None
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dest))
    loader._invalidate_iter_cache()
    logger.info("Restored auto skill: %s", name)
    return name


def list_archived_auto_skills(loader: SkillsLoader) -> list[dict]:
    """Return ``{slug, path}`` for every archived auto-skill."""
    root = loader._archive_root()
    out: list[dict] = []
    if not root.is_dir():
        return out
    for child in sorted(root.iterdir()):
        if child.is_dir() and (child / "SKILL.md").exists():
            out.append({"slug": child.name, "path": str(child / "SKILL.md")})
    return out


def run_skill_lifecycle(
    loader: SkillsLoader,
    *,
    max_auto_skills: int,
    stale_after_days: int,
    archive_after_days: int,
    cron_referenced: set[str] | None = None,
    exempt: set[str] | None = None,
    now: float | None = None,
) -> dict:
    """Age + bound the auto-skill set. Archives (never deletes).

    Two passes:
    1. **Inactivity**: archive any auto-skill whose anchor is older than
       ``archive_after_days``. Pinned and cron-referenced skills are exempt.
       Never-used (hits==0) skills younger than ``stale_after_days`` are
       exempt (grace floor).
    2. **Max-N backstop**: if more than ``max_auto_skills`` remain live,
       archive the lowest-ranked (by hits, then recency) down to the cap,
       again skipping pinned / cron-referenced skills.

    Returns a counts dict: ``{checked, marked_stale, archived, capped}``.
    """
    if now is None:
        now = time.time()
    if cron_referenced is None:
        cron_referenced = loader._cron_referenced_skills()
    extra_exempt = exempt or set()
    stale_cutoff = now - stale_after_days * 86400
    archive_cutoff = now - archive_after_days * 86400
    counts = {"checked": 0, "marked_stale": 0, "archived": 0, "capped": 0}

    # Snapshot live auto-skills with their activity + exemption status.
    rows: list[dict] = []
    for s in loader.list_auto_skills():
        key = s["key"]
        # A listed row can be a project skill, so reuse the root the listing
        # recorded rather than reading it unconfined for a ranking signal.
        meta = loader._cached_frontmatter(Path(s["path"]), within=s.get("confine_root"))
        hits, anchor = loader._auto_activity(key, s["path"], meta)
        pinned = str(meta.get("pinned", "")).strip().lower() == "true"
        slug = key.split("/")[-1]
        exempt_row = (
            pinned
            or key in cron_referenced
            or slug in cron_referenced
            or key in extra_exempt
            or slug in extra_exempt
        )
        rows.append({"key": key, "hits": hits, "anchor": anchor, "exempt": exempt_row})
        counts["checked"] += 1

    # Pass 1 — inactivity archival.
    survivors: list[dict] = []
    for r in rows:
        if r["exempt"]:
            survivors.append(r)
            continue
        never_used_grace = r["hits"] == 0 and r["anchor"] > stale_cutoff
        if not never_used_grace and r["anchor"] <= archive_cutoff:
            if loader.archive_auto_skill(r["key"]):
                counts["archived"] += 1
                continue
        if r["hits"] == 0 and r["anchor"] <= stale_cutoff:
            counts["marked_stale"] += 1
        elif r["anchor"] <= stale_cutoff:
            counts["marked_stale"] += 1
        survivors.append(r)

    # Pass 2 — max-N backstop over what survived pass 1.
    evictable = [r for r in survivors if not r["exempt"]]
    overflow = len(survivors) - max_auto_skills
    if overflow > 0 and evictable:
        evictable.sort(key=lambda r: (r["hits"], r["anchor"]))
        for r in evictable[:overflow]:
            if loader.archive_auto_skill(r["key"]):
                counts["archived"] += 1
                counts["capped"] += 1
    return counts


def _pending_root(loader: SkillsLoader) -> Path:
    from kiro_crew import skills as sk  # circular import: the facade imports this module

    return loader._dir / sk.AUTO_SKILL_NAMESPACE / sk.AUTO_PENDING_DIRNAME


def _auto_slug_claim_lock(loader: SkillsLoader) -> Iterator[bool]:
    """Hold one exclusive lock across an availability test and its claim.

    Yields whether the lock was ACQUIRED. Every claim path must refuse when it
    was not: the two paths allocate in DIFFERENT directories, ``auto/<slug>``
    and ``auto/.pending/<slug>``, so an atomic ``mkdir`` makes each half safe
    on its own but cannot make the CROSS-namespace pair safe. Two processes
    sharing one skills home can each pass the other half's test and then create
    their own directory, which strands the queued candidate:
    ``approve_pending_skill`` refuses it while the live directory stands and TTL
    pruning then deletes it unreviewed. Proceeding unlocked would reopen exactly
    that window, so an unacquired lock is a refusal, not a licence.

    A refusal is cheap and leaves a record, which is why this never raises:
    both claim paths return ``None``, which their callers audit as a rejection,
    and the next consolidation pass reaches the same code with the lock free.
    An escaping error would instead abort the caller mid-pass. Acquisition fails
    in two ways, and both yield ``False``: the lock file cannot be opened (a
    read-only or full home, which would fail the skill write too) and the
    acquire does not win within the ceiling. The body's own errors still
    propagate, so a failed write is never mistaken for a failed acquire.

    That "next pass" is not automatic, and the ``None`` alone does not deliver
    it: skill detection records a ``(rotation_generation, message_count)``
    marker per session and skips a pass whose pair is unchanged, so a session
    that goes quiet right after a stall would not be re-judged until a further
    message or a gateway restart. A claim path therefore reports a lock refusal
    through :class:`ClaimRefusal`, and the detection pass RETRACTS its marker
    when it sees one -- which is what makes the retry above real. Only the
    marker is retracted; the consolidation offset that history, semantic and
    lesson extraction share is left advanced on purpose, because holding it
    back would re-summarize an already-consolidated tail into duplicate
    entries.
    """
    from kiro_crew import skills as sk  # circular import: the facade imports this module

    lock_path = loader._dir / sk.AUTO_SLUG_CLAIM_LOCK_NAME
    fd: int | None = None
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    except OSError:
        fd = None
    if fd is None:
        logger.warning("Auto slug claim lock cannot be opened at %s", lock_path)
        yield False
        return
    try:
        # ``file_lock`` owns the platform matrix, the bounded wait and the
        # single-shot refusal for an on-loop caller, and it fails closed by
        # raising. Enter it EXPLICITLY so the acquire is the only failure that
        # means "unlocked": wrapping the whole block in one ``except OSError``
        # would read a failed write as a failed acquire and run the body twice.
        guard = None
        try:
            guard = sk.file_lock(fd, exclusive=True, timeout=sk.AUTO_SLUG_CLAIM_LOCK_TIMEOUT_SECS)
            guard.__enter__()
        except OSError:
            logger.warning(
                "Auto slug claim lock not acquired within %.0fs; refusing the claim",
                sk.AUTO_SLUG_CLAIM_LOCK_TIMEOUT_SECS,
            )
            yield False
            return
        try:
            yield True
        finally:
            guard.__exit__(None, None, None)
    finally:
        os.close(fd)


def _auto_slug_available(
    loader: SkillsLoader,
    slug: str,
    *,
    claim: Literal["live", "pending-new", "pending-update"] = "live",
) -> bool:
    """True when ``slug`` is free for the allocation named by ``claim``.

    The live tree and the pending queue are two halves of ONE slug space: a
    queued NEW candidate's promotion destination is ``auto/<slug>``, so a
    claim that consults only its own half can take a name the other half
    depends on, and the losing side goes silently. ``approve_pending_skill``
    refuses a candidate whose live name is occupied (``live_exists``) for as
    long as that directory stands, and TTL pruning then deletes the candidate
    unreviewed. Both claim paths test a name here, under
    :meth:`_auto_slug_claim_lock`, before allocating it.

    The halves are not symmetric, so ``claim`` names which allocation is
    under test:

    - ``"live"`` — creating ``auto/<slug>``. Free when no live directory
      stands there and no pending NEW candidate is queued under that slug.
    - ``"pending-new"`` — queueing a new candidate at
      ``auto/.pending/<slug>``. Free when that pending directory is absent
      and ``auto/<slug>`` is unoccupied, since a queued candidate whose live
      name is taken is unapprovable.
    - ``"pending-update"`` — queueing an UPDATE candidate. Free when the
      pending directory is absent; the live tree does not constrain it,
      because ``approve_pending_update`` promotes over the live ``target``
      named in the candidate's metadata and never consults
      ``auto/<candidate-slug>``.

    A pending UPDATE candidate reserves NOTHING in the live tree, which is
    why the ``"live"`` test reads the queued candidate's ``kind``. An update
    is queued under a name derived from its target (``<target-slug>-update``)
    and promotes to ``auto/<target-slug>``, so treating it as a reservation
    would hold ``auto/<target-slug>-update`` against an unrelated skill that
    slugifies to that name and drop it for good, since consolidation advances
    its message offset whatever one candidate's outcome. The kind test FAILS
    CLOSED: a pending directory whose metadata is missing, unreadable, or
    silent about ``kind`` keeps its slug reserved.

    The slug pattern is re-checked because a caller may DERIVE the name by
    suffixing (``<slug>-2``), which can push a long slug past the length the
    pattern allows. ``list_pending_skills`` skips a pending directory whose
    name is not canonical and ``prune_pending`` walks that same list, so a
    non-canonical claim is invisible to the queue, to the dashboard, and to
    pruning alike — a candidate written there can never be reviewed and is
    never cleaned up.
    """
    from kiro_crew import skills as sk  # circular import: the facade imports this module

    if not sk._AUTO_NAME_PATTERN.match(slug):
        return False
    pending_dir = loader._pending_root() / slug
    live_dir = loader._dir / sk.AUTO_SKILL_NAMESPACE / slug
    if claim != "live":
        if pending_dir.exists():
            return False
        return claim == "pending-update" or not live_dir.exists()
    if live_dir.exists():
        return False
    return not pending_dir.exists() or loader._read_pending_meta(slug).get("kind") == "update"


def stage_skill_candidate(
    loader: SkillsLoader,
    slug: str,
    *,
    description: str,
    triggers: str,
    procedure_md: str,
    provenance: AutoSkillProvenance,
    scripts: list[dict] | None = None,
    source: str = "consolidation",
    kind: str = "new",
    target: str | None = None,
    base_version: int | None = None,
    refusal: ClaimRefusal | None = None,
) -> str | None:
    """Write a skill candidate to the pending queue (not live).

    Layout: ``auto/.pending/<slug>/{SKILL.md, scripts/*, .meta.json}``.
    Scripts are written **non-executable** — the executable bit is only set
    on approval. Returns the queued name on success, which is ``auto/<slug>``
    or a sibling ``auto/<slug>-N`` when the natural slug is taken. Returns
    ``None`` when nothing is queued: an invalid slug, an oversized procedure,
    or no free name across the live and pending namespaces. A ``None`` means
    the candidate is NOT staged and the caller must take its rejection
    branch. Caller passes already-redacted content.

    ``kind`` distinguishes a brand-new candidate (``"new"``, the default,
    approved via ``approve_pending_skill``) from an UPDATE proposal against
    an existing live auto-skill (``"update"``, approved via
    ``approve_pending_update``). For an update, ``target`` names the live
    auto-skill (``auto/<slug>``) and ``base_version`` records the live
    version the merge was based on. These are written into ``.meta.json``
    (``kind`` always; ``target`` / ``base_version`` only when provided) so
    existing new-candidate callers are unaffected.
    """
    from kiro_crew import skills as sk  # circular import: the facade imports this module

    if not sk._AUTO_NAME_PATTERN.match(slug):
        logger.warning("Rejected pending skill: slug %r failed validation", slug)
        return None
    if len(procedure_md) > sk.AUTO_SKILL_MAX_PROCEDURE_CHARS:
        logger.warning("Rejected pending skill %s: procedure too long", slug)
        return None
    name = f"{sk.AUTO_SKILL_NAMESPACE}/{slug}"
    root = loader._pending_root()
    root.mkdir(parents=True, exist_ok=True)
    # Atomically CLAIM a pending dir. mkdir(exist_ok=False) closes the TOCTOU
    # between the availability test and the create. If the natural slug is
    # already awaiting review we must NOT overwrite it (the queued candidate is
    # immutable until approved/dismissed) — but we also must NOT silently drop
    # THIS candidate: consolidation advances its message offset regardless of
    # per-candidate outcome, so a distinct skill that merely slugifies the
    # same as a pending one would be lost forever. Allocate a unique sibling
    # slug (<slug>-2, -3, …) so it still gets queued. Genuine re-detections of
    # the SAME skill are suppressed upstream by the metadata dedupe before
    # staging, so this does not flood the queue with duplicates.
    #
    # Every name on the walk is tested against BOTH namespaces and the slug
    # pattern, so the queue only ever accepts a claim it can serve: a NEW
    # candidate whose live name is occupied is unapprovable, and an over-long
    # suffixed name is dropped from ``list_pending_skills`` and with it from
    # the dashboard and from pruning.
    _claim: Literal["pending-new", "pending-update"] = (
        "pending-update" if (kind or "new") == "update" else "pending-new"
    )
    pdir: Path | None = None
    # Same lock the live publish takes, for the same reason: the pending
    # mkdir is atomic within this namespace, but only a shared lock keeps a
    # live create from claiming the name this walk just accepted. The lock is
    # held until ``.meta.json`` is committed, because ``kind`` is the field a
    # live publish reads to decide whether this claim reserves its name: a
    # publish seeing the claimed directory without that file fails closed and
    # refuses a name an UPDATE candidate never reserves.
    with loader._auto_slug_claim_lock() as locked:
        if not locked:
            if refusal is not None:
                refusal.retryable = True
            logger.warning("Pending skill %s not staged: the slug claim lock is unavailable", slug)
            return None
        for _cand in (slug, *(f"{slug}-{_n}" for _n in range(2, 51))):
            if not loader._auto_slug_available(_cand, claim=_claim):
                continue
            _cand_dir = root / _cand
            try:
                _cand_dir.mkdir(exist_ok=False)
            except FileExistsError:
                continue
            pdir = _cand_dir
            break
        if pdir is None:
            # Nothing is written, so the caller MUST take its rejection branch: a
            # name returned from here is recorded as a staged candidate that does
            # not exist, and consolidation's offset advance makes that loss
            # permanent and invisible.
            logger.warning("No free pending slug for %s; candidate not staged", slug)
            return None
        if pdir.name != slug:
            slug = pdir.name
            name = f"{sk.AUTO_SKILL_NAMESPACE}/{slug}"
            logger.info("Slug in use; staging distinct candidate as %s", name)
        try:
            content = sk._build_auto_skill_content(
                slug=slug,
                description=description,
                triggers=triggers,
                procedure_md=procedure_md,
                provenance=provenance,
            )
            (pdir / "SKILL.md").write_text(content, encoding="utf-8")
            script_names: list[str] = []
            clean_scripts = [s for s in (scripts or []) if isinstance(s, dict)]
            if clean_scripts:
                sdir = pdir / "scripts"
                sdir.mkdir(exist_ok=True)
                for s in clean_scripts:
                    fn = str(s.get("filename", "")).strip()
                    # Guard the script filename against traversal / nesting.
                    if not fn or "/" in fn or "\\" in fn or ".." in fn:
                        continue
                    (sdir / fn).write_text(str(s.get("content", "")), encoding="utf-8")
                    script_names.append(fn)
            meta = {
                "slug": slug,
                "name": name,
                "source": source,
                "created_at": provenance.created_at or sk.AutoSkillProvenance.now_iso(),
                "description": description,
                "triggers": triggers,
                "has_scripts": bool(script_names),
                "scripts": script_names,
                "kind": kind or "new",
            }
            if target is not None:
                meta["target"] = target
            if base_version is not None:
                meta["base_version"] = base_version
            (pdir / ".meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
        except Exception:
            # A partial write (e.g. disk full) must not leave a CLAIMED but empty
            # dir behind: a later stage would see it exists and report the slug as
            # "already awaiting review" while no reviewable candidate exists.
            # Roll back the atomic claim so the slug can be re-staged cleanly.
            shutil.rmtree(pdir, ignore_errors=True)
            raise
    logger.info("Staged pending skill candidate: %s (scripts=%d)", name, len(script_names))
    # Notify any registered observer (the gateway wires a bell-feed
    # notification + a ``skills.pending_changed`` WS event) so a candidate
    # awaiting review surfaces instead of sitting unseen in the queue. Fired
    # for BOTH new and update candidates, from every producer that stages
    # through this choke point. Best-effort: an observer failure must never
    # fail the staging that already succeeded on disk.
    #
    # ``description``/``triggers`` ride along because the observer's only
    # other option is to re-read ``.meta.json`` off disk (a second read of
    # what was just written, on the staging path) -- and without them a
    # notification can only say THAT a skill was generated, never what it
    # does, which is the one fact a reviewer needs to decide whether to open
    # the queue at all.
    sk._emit_pending_staged(
        {
            "name": name,
            "slug": slug,
            "kind": kind or "new",
            "target": target,
            "source": source,
            "has_scripts": bool(script_names),
            "description": description,
            "triggers": triggers,
        }
    )
    return name


def pending_candidate_is_staged(loader: SkillsLoader, slug: str) -> bool:
    """Whether a candidate is still staged at *slug*, for CHOOSING A MESSAGE.

    ``get_pending_skill`` answers None for two different situations -- there is no
    such candidate, and there is one whose tree the pinned read refuses -- and a
    caller that renders both as "approved or dismissed elsewhere" tells the user
    their candidate is gone while it sits in the list. This separates them.

    Deliberately a by-name probe, and deliberately not a security check: the pinned
    read is the authority on whether anything may be READ, and this runs only after
    that read already refused. Losing the race changes which refusal message a user
    sees and can never turn a refusal into a read, which is why a second traversal
    would be cost without a property.
    """
    if not loader._is_pending_slug_safe(slug):
        return False
    return (loader._pending_root() / slug / "SKILL.md").exists()


def dismiss_pending_skill(loader: SkillsLoader, slug: str) -> bool:
    """Delete a pending candidate. Returns True if it existed."""
    from kiro_crew import skills as sk  # circular import: the facade imports this module

    if not loader._is_pending_slug_safe(slug):
        return False
    pdir = loader._pending_root() / slug
    if not pdir.is_dir():
        return False
    # Captured BEFORE the removal so a same-slug replacement staged after
    # this instant keeps its notification (see approve_pending_skill).
    consumed_at = datetime.now(tz=timezone.utc).isoformat()
    shutil.rmtree(pdir)
    logger.info("Dismissed pending skill: %s", slug)
    sk._emit_pending_consumed({"slug": slug, "outcome": "dismissed", "consumed_at": consumed_at})
    return True


def dismiss_all_pending(loader: SkillsLoader) -> int:
    """Delete all pending candidates. Returns count dismissed."""
    pending = loader.list_pending_skills()
    count = 0
    for entry in pending:
        if loader.dismiss_pending_skill(entry["slug"]):
            count += 1
    if count:
        logger.info("Dismissed all %d pending skills", count)
    return count


def dismiss_pending_slugs(loader: SkillsLoader, slugs: list[str]) -> int:
    """Delete only the specified pending candidates. Returns count dismissed."""
    count = 0
    for slug in slugs:
        if loader.dismiss_pending_skill(slug):
            count += 1
    if count:
        logger.info("Dismissed %d of %d requested pending skills", count, len(slugs))
    return count


def prune_pending(loader: SkillsLoader, ttl_days: int, *, now: float | None = None) -> int:
    """Remove pending candidates older than ``ttl_days``. Returns count pruned.

    Age is measured from the candidate directory's filesystem mtime (set when
    the queue writes it), NOT the LLM-supplied ``created_at`` metadata: a
    ``crystallize`` direct-write could stamp an arbitrarily old ``created_at``
    and trick pruning into ``rmtree``-ing fresh, unreviewed work.
    """
    if now is None:
        now = time.time()
    cutoff = now - ttl_days * 86400
    pruned = 0
    root = loader._pending_root()
    for entry in loader.list_pending_skills():
        pdir = root / entry["slug"]
        try:
            ts = pdir.stat().st_mtime
        except OSError:
            continue
        if ts <= cutoff and loader.dismiss_pending_skill(entry["slug"]):
            pruned += 1
    return pruned
