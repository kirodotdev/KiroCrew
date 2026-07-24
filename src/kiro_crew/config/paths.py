"""Pure filesystem path primitives for KiroCrew configuration.

This is a **leaf module**: it depends only on the standard library
(``os``, ``sys``, ``pathlib``, ``logging``) and imports nothing from
``kiro_crew``. Modules that only need to locate ``~/.kirocrew/`` should import
from here directly::

    from kiro_crew.config.paths import config_dir

so they don't transitively pull in the full config loader (DTOs, schema
validation, the process-global cache, and the lazily-imported provider
factory) the way ``from kiro_crew.config.loader import config_dir`` does.

Only the genuinely pure primitives live here. The *dir-derived* helpers
(``config_path``, ``config_local_path``, ``workspace_root``, ``workspace_dir_for``,
``outbox_dir``, ``env_path``, …) remain in :mod:`kiro_crew.config.loader` so that
their ``config_dir()`` lookups resolve in the loader namespace — preserving the
``patch("kiro_crew.config.loader.config_dir", ...)`` test seam used across the
suite.

All names here are also re-exported from ``kiro_crew.config.loader`` for
backward compatibility, so existing callers continue to work unchanged.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# KiroCrew's data root nests UNDER kiro-cli's own home ``~/.kiro/`` (Labs product
# decision: all Kiro-family apps share the ``~/.kiro/`` base so a user has a
# single place to secure). ``config_dir()`` therefore resolves to
# ``~/.kiro/crew`` by default. ``CONFIG_DIR_NAME`` is the segment(s) appended to
# ``~/`` — kept as a POSIX-style relative literal so downstream string checks
# (e.g. the security keystone) can match it uniformly.
KIRO_BASE_DIR_NAME = ".kiro"
CONFIG_DIR_LEAF = "crew"
CONFIG_DIR_NAME = f"{KIRO_BASE_DIR_NAME}/{CONFIG_DIR_LEAF}"  # ".kiro/crew"

# The pre-move top-level home. Retained as a constant (not an inline literal) so
# the one-time migration and the security keystone reference the same source of
# truth. Data here is copied into the new root at first run, then the directory
# is renamed to ``ARCHIVED_LEGACY_DIR_NAME`` (rollback copy — never deleted).
LEGACY_CONFIG_DIR_NAME = ".kirocrew"
ARCHIVED_LEGACY_DIR_NAME = ".kirocrew.archived"

# Marker file written INTO the new home once migration (or a fresh-install
# no-op) has fully completed and been verified. Its presence — NOT the bare
# existence of the ~/.kiro/crew directory — is what tells a later start "this
# home is authoritative, do not migrate". An empty/partial ~/.kiro/crew (created
# by another Kiro tool, a user ``mkdir``, or an interrupted copy) has NO marker,
# so migration still runs and the real legacy data is never stranded.
MIGRATION_MARKER_NAME = ".data-home-ready"

# Recovery-pointer breadcrumb written at the TOP-LEVEL home (``~/.kirocrew.breadcrumb``),
# deliberately OUTSIDE ``~/.kiro/``. The data home now nests under kiro-cli's
# ``~/.kiro/`` base, so a hypothetical Kiro-family uninstaller that wipes
# ``~/.kiro/`` would take KiroCrew's data with it — and a fresh install has no
# ``~/.kirocrew.archived`` fallback. This tiny, non-secret pointer survives such a
# wipe (it lives beside ``~/.kiro``, not inside it) and records where the data
# home is, so a user/support script can find any surviving data or understand
# what was lost. It is NOT a backup — just a durable signpost (the reviewer's
# "cheap technical hedge" for the one-way-door). Only written on the default
# (non-override) path; a ``KIROCREW_HOME`` override is the user's own chosen
# location and carries no ``~/.kiro/`` wipe risk.
RECOVERY_BREADCRUMB_NAME = ".kirocrew.breadcrumb"

OUTBOX_DIR_NAME = "outbox"

# Cross-platform workspace root for LLM working directories.
# Override: KIROCREW_WORKSPACE env var or <config_dir>/workspace_dir
# macOS: /Volumes/workplace/kirocrew-workspace (fallback ~/workplace)
# Linux: ~/workplace/kirocrew-workspace
_WORKSPACE_DIR_NAME = "kirocrew-workspace"

# Once-per-process cache of the RESOLVED data home so the lazy first-run
# migration runs at most once and every later config_dir() call returns the same
# directory with no extra filesystem probing. We cache the resolved Path itself
# (not merely a "did we try" boolean): when a migration is needed but skipped or
# aborted (a live gateway, or a copy/verify failure), migrate_home() falls back
# to the still-intact legacy ``~/.kirocrew`` for THIS process — and every
# subsequent call must return that SAME legacy home, not the empty new home that
# was never populated. A bare boolean guard would let call #1 return the legacy
# home while call #2+ returned the untouched ~/.kiro/crew, splitting the process
# across two data roots. ``None`` means "not yet resolved this process".
_resolved_home: Path | None = None


def _default_home() -> Path:
    """Resolve the default (non-override) data root: ``~/.kiro/crew``."""
    return Path.home() / KIRO_BASE_DIR_NAME / CONFIG_DIR_LEAF


def _legacy_home() -> Path:
    """Resolve the pre-move top-level home: ``~/.kirocrew``."""
    return Path.home() / LEGACY_CONFIG_DIR_NAME


def _maybe_migrate_legacy_home() -> Path:
    """Relocate a pre-move ``~/.kirocrew`` into ``~/.kiro/crew`` exactly once.

    Returns the directory the caller should use as the data root for THIS
    process, caching it so the result is stable for the process lifetime.
    Normally that is the new default home; if a migration is needed but fails or
    is skipped, we fall back to the still-intact legacy home so a botched copy
    never surfaces as data loss — and the cache pins that same legacy home for
    every later call (no mid-process home switch).

    Fail-safe contract (mirrors KITCHEN-111): copy-then-verify-then-archive, so
    an interruption at any point leaves the original ``~/.kirocrew`` fully
    intact. Import is deferred to keep this module a stdlib-only leaf.

    The short-circuit is gated on a COMPLETION MARKER, not on bare directory
    existence: an empty or partial ``~/.kiro/crew`` (created by another Kiro
    tool, a user ``mkdir``, or an interrupted copy) must NOT be mistaken for a
    finished migration — otherwise a legacy ``~/.kirocrew`` full of real data
    would be silently stranded and every caller pinned to the empty home. When
    no marker is present, migration runs (merging legacy data without
    overwriting) and the marker is written only after a verified copy; a
    fresh install with no legacy writes the marker immediately (nothing to do).
    """
    global _resolved_home
    if _resolved_home is not None:
        return _resolved_home
    new_home = _default_home()
    marker = new_home / MIGRATION_MARKER_NAME
    legacy = _legacy_home()

    # Trust the completion marker ONLY when there is no legacy dir alongside it.
    # A marker means "this new home was authoritative at some point", but if a
    # legacy ``~/.kirocrew`` ALSO exists now, the new home is not unconditionally
    # authoritative: e.g. the user migrated (marker written), then DOWNGRADED to
    # an old release that wrote fresh state back to ~/.kirocrew, then upgraded
    # again. Trusting the marker blindly would ignore that now-active legacy data.
    # So: marker + no legacy → trust the new home; marker + legacy present → fall
    # through to migrate(), whose merge + byte-identical divergence guard
    # reconciles (archive legacy only if the new home already holds every legacy
    # file identically; otherwise retain legacy and don't mark). This single
    # invariant — "never treat the new home as authoritative while a legacy home
    # with non-identical content exists" — closes the whole family of edge cases.
    if marker.exists() and not legacy.is_dir():
        _resolved_home = new_home
        # Migration is durably complete. Opportunistically expire the credential
        # half of the archived rollback copy once its grace window has elapsed, so
        # a frozen ~/.kirocrew.archived/.env & keys don't linger indefinitely
        # (best-effort; never blocks startup). Non-secret rollback data is kept.
        _maybe_expire_archive_secrets()
        return new_home

    # No legacy data to migrate → this is a fresh install (the new home may or
    # may not exist yet). Create it, drop the marker, and use it.
    if not legacy.is_dir():
        _resolved_home = _finalize_fresh_home(new_home, marker)
        return _resolved_home

    # A legacy home exists (a pre-move install, OR a post-downgrade write-back
    # even though the new home is marked). Migrate — merging legacy data into
    # whatever is already there without overwriting, reconciling via the
    # byte-identical divergence guard — then mark.
    try:
        from kiro_crew.home_migration import migrate_home

        _resolved_home = migrate_home(legacy=legacy, new_home=new_home, marker=marker)
    except Exception:  # pragma: no cover - defensive: never block startup
        logger.warning(
            "legacy home migration to %s failed; using %s for this run",
            new_home,
            legacy,
            exc_info=True,
        )
        _resolved_home = legacy
    return _resolved_home


def _maybe_expire_archive_secrets() -> None:
    """Shred stale secret leaves from ``~/.kirocrew.archived`` (best-effort).

    Delegates to :func:`kiro_crew.home_migration.shred_archive_secrets_if_stale`,
    which is a no-op unless the archive exists and the completion marker is older
    than the grace window. Any error is swallowed so this never blocks startup.
    Import is deferred to keep this module a stdlib-only leaf.
    """
    try:
        from kiro_crew.home_migration import (
            _ARCHIVE_SECRET_GRACE_SECONDS,
            shred_archive_secrets_if_stale,
        )

        archived = Path.home() / ARCHIVED_LEGACY_DIR_NAME
        marker = _default_home() / MIGRATION_MARKER_NAME
        shred_archive_secrets_if_stale(
            archived, marker, min_age_seconds=_ARCHIVE_SECRET_GRACE_SECONDS
        )
        # Divergent-home reconciliation leaves timestamped backups under
        # ``~/.kiro/crew.pre-migration/`` that also hold frozen credential leaves.
        # Give each the SAME marker-aged end-of-life as the archive so they are not
        # a standing, ever-growing secret store (the live secrets stay in the new
        # home; non-secret backup data is kept).
        default_home = _default_home()
        pre_migration_root = default_home.parent / f"{default_home.name}.pre-migration"
        if pre_migration_root.is_dir():
            for child in sorted(pre_migration_root.iterdir()):
                if child.is_dir():
                    shred_archive_secrets_if_stale(
                        child, marker, min_age_seconds=_ARCHIVE_SECRET_GRACE_SECONDS
                    )
    except Exception:  # pragma: no cover - defensive: never block startup
        logger.debug("archive-secret expiry sweep failed (non-fatal)", exc_info=True)


def _write_recovery_breadcrumb(data_home: Path) -> None:
    """Drop a recovery-pointer breadcrumb at ``~/.kirocrew.breadcrumb`` (best effort).

    Lives OUTSIDE ``~/.kiro/`` so it survives a ``~/.kiro/``-wide uninstaller wipe
    and records where the data home is (see ``RECOVERY_BREADCRUMB_NAME``). Written
    once (skipped if already present and already points at *data_home*), never
    raises, never blocks startup, and contains NO secrets — only the path. Only
    called on the default (non-override) resolution path.
    """
    try:
        crumb = Path.home() / RECOVERY_BREADCRUMB_NAME
        content = (
            "KiroCrew data-home location pointer (safe to delete).\n"
            "\n"
            "KiroCrew stores its data (config, credentials, history, DBs) at:\n"
            f"    {data_home}\n"
            "\n"
            "This pointer lives outside ~/.kiro/ on purpose: if a Kiro-family\n"
            "uninstaller ever removes ~/.kiro/, this file survives so you can find\n"
            "any surviving data or know where it had been. It is NOT a backup.\n"
        )
        # Idempotent: only (re)write when absent or the recorded path changed, so
        # we don't churn the file on every process start.
        if crumb.is_file():
            try:
                if str(data_home) in crumb.read_text(encoding="utf-8"):
                    return
            except OSError:
                pass
        crumb.write_text(content, encoding="utf-8")
    except OSError:  # pragma: no cover - defensive: a breadcrumb is best-effort
        logger.debug("could not write recovery breadcrumb", exc_info=True)


def _finalize_fresh_home(new_home: Path, marker: Path) -> Path:
    """Create *new_home* and stamp the completion marker (fresh-install path).

    Falls back to *new_home* uncreated on any error — config_dir()'s own
    ``mkdir`` still runs, so the process is never blocked; the marker simply
    isn't written this run and a later start retries (idempotent).
    """
    try:
        new_home.mkdir(parents=True, exist_ok=True)
        marker.write_text("fresh-install\n", encoding="utf-8")
    except OSError:  # pragma: no cover - defensive
        logger.debug("could not stamp fresh-install marker at %s", marker, exc_info=True)
    return new_home


def config_dir() -> Path:
    override = os.environ.get("KIROCREW_HOME")
    if override:
        p = Path(override).expanduser().resolve()
        # Refuse root or system directories as config home
        if p == Path("/") or p.parts[:2] in (("/", "usr"), ("/", "System"), ("/", "etc")):
            logger.warning("KIROCREW_HOME=%s is a system directory, ignoring", override)
        else:
            p.mkdir(parents=True, exist_ok=True)
            return p
    d = _maybe_migrate_legacy_home()
    d.mkdir(parents=True, exist_ok=True)
    # Drop the recovery-pointer breadcrumb outside ~/.kiro/ (default path only).
    # Best-effort + idempotent; guarded so a breadcrumb failure never blocks the
    # data-home resolution the whole app depends on.
    _write_recovery_breadcrumb(d)
    return d


def ensure_data_home() -> Path:
    """Eagerly resolve (and, if needed, migrate) the data home — call BEFORE the loop.

    ``config_dir()`` performs the one-time legacy→new-home migration lazily on its
    first call, and that migration can BLOCK (a ``copytree`` + ``os.walk`` +
    byte-compare over the whole legacy home, behind a cross-process file lock).
    If the first ``config_dir()`` of the process happens on the asyncio event loop
    (e.g. inside an async-facing constructor), the loop freezes for the full
    migration and the stall watchdog may kill the gateway
    (``no-blocking-call-on-event-loop``).

    Every real entrypoint therefore calls this ONCE from its synchronous prologue,
    before ``asyncio.run``: it forces the resolution+migration to complete on the
    main thread and caches the result, so every later on-loop ``config_dir()`` is
    a cheap cached lookup. Idempotent (the process-lifetime cache makes a second
    call a no-op) and safe to call unconditionally — a fresh install with no legacy
    home just creates the directory. Returns the resolved data home.
    """
    return config_dir()


def config_package_dir() -> Path:
    """Return the installed ``kiro_crew/config/`` directory.

    This is the source of truth for bundled config data files (``defaults.json``,
    ``prompt.md``, persona/orchestrator prompts). ``paths.py`` lives directly in
    the config package, so this is simply its parent directory.
    """
    return Path(__file__).resolve().parent


def kiro_agents_dir() -> Path:
    """Return the kiro agents directory (``~/.kiro/agents``).

    Lives in this leaf module so :mod:`kiro_crew.config.loader` can locate
    installed agent JSONs without importing :mod:`kiro_crew.agent` — which
    imports ``config.loader`` at module load and would create an import cycle.
    """
    return Path.home() / ".kiro" / "agents"


def _default_workspace_base() -> Path:
    """Return the platform-specific default base for the workspace."""
    if sys.platform == "darwin":
        vol = Path("/Volumes/workplace")
        return vol if vol.is_dir() else Path.home() / "workplace"
    return Path.home() / "workplace"


def _safe_dir_name(key: str) -> str:
    """Sanitize a session key into a safe directory name."""
    return key.replace("/", "_").replace("\\", "_").replace(":", "_").replace(" ", "_")
