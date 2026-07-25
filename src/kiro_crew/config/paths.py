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
import shlex
import shutil
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
# is deleted outright — no rollback copy is kept.
LEGACY_CONFIG_DIR_NAME = ".kirocrew"

# Names an EARLIER release of this migration (since retired) could have left on
# disk: ``~/.kirocrew.archived`` (a full rollback copy of the pre-move home) and
# ``~/.kiro/crew.pre-migration/<timestamp>`` (a sidelined divergent-home backup).
# Neither is created by the current migration, and neither is on the security
# keystone anymore (nothing creates them, so gating them was dead weight) — which
# means a leftover one from that earlier release is now UNGATED: its frozen
# ``.env`` / ``token_signing.key`` / ``security_policy.json`` etc. would be
# agent-readable indefinitely, with nothing to ever prompt a cleanup. See
# ``_sweep_ungated_archive_leftovers``.
_ARCHIVED_LEGACY_DIR_NAME = ".kirocrew.archived"
_PRE_MIGRATION_BACKUP_DIR_NAME = "crew.pre-migration"

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
# ``~/.kiro/`` would take KiroCrew's data with it, and there is no rollback copy
# anywhere to recover from. This tiny, non-secret pointer survives such a
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
# aborted, migrate_home() returns the home this process must join — the LIVE
# GATEWAY's home when one is running (legacy or new, whichever holds
# gateway.lock, so .local_secret matches for internal IPC), or the still-intact
# legacy ``~/.kirocrew`` on a copy/verify failure — and every subsequent call
# must return that SAME home, not the empty new home that was never populated.
# A bare boolean guard would let call #1 return one home while call #2+ returned
# the untouched ~/.kiro/crew, splitting the process across two data roots.
# ``None`` means "not yet resolved this process".
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
    Normally that is the new default home. If a migration is needed but is
    skipped because a gateway is already live, the caller joins whichever home
    that live gateway holds (its new home when it booted post-migration, else
    the still-intact legacy home) so IPC stays coherent; if a migration is
    needed but fails, it falls back to the still-intact legacy home so a botched
    copy never surfaces as data loss. Either way the cache pins that same home
    for every later call (no mid-process home switch).

    Fail-safe contract: force-copy-then-verify-then-delete, so an interruption
    before the delete leaves the original ``~/.kirocrew`` fully intact. Import is
    deferred to keep this module a stdlib-only leaf.

    The short-circuit is gated on a COMPLETION MARKER, not on bare directory
    existence: an empty or partial ``~/.kiro/crew`` (created by another Kiro
    tool, a user ``mkdir``, or an interrupted copy) must NOT be mistaken for a
    finished migration — otherwise a legacy ``~/.kirocrew`` full of real data
    would be silently stranded and every caller pinned to the empty home. When
    no marker is present, migration runs (legacy files OVERWRITE anything already
    at the new home) and the marker is written only after a verified copy; a
    fresh install with no legacy writes the marker immediately (nothing to do).
    """
    global _resolved_home
    if _resolved_home is not None:
        return _resolved_home
    new_home = _default_home()
    marker = new_home / MIGRATION_MARKER_NAME
    legacy = _legacy_home()

    # The completion marker is AUTHORITATIVE: once written (after a verified
    # copy), the new home won and the legacy home was force-deleted — this
    # design has NO downgrade/rollback path (see security.py `_CREW_HOME_PREFIXES`
    # note + config.md "No rollback"). So a legacy ``~/.kirocrew`` present
    # ALONGSIDE the marker can only be resurrection DEBRIS: stale files an old
    # or legacy-pinned process wrote back after the migration completed. It is
    # NEVER authoritative, so we must NOT re-migrate it over the new home —
    # doing so would revert same-named files (``sel_hmac.key``, logs,
    # ``workspace/``) to stale versions (the split-brain data-loss GPT 5.6
    # flagged). marker present (with or without a legacy dir) => trust the new
    # home. The debris is left in place and RETAINED (not auto-swept — the
    # leftover sweep only removes ``.kirocrew.archived`` / ``.kiro/crew.pre-
    # migration``, not ``.kirocrew`` itself); it stays under the ``.kirocrew``
    # sensitive-path prefix (credential-protected) for manual cleanup. A legacy
    # dir RE-created later is likewise never promoted, so the recreate/TOCTOU
    # race is benign.
    #
    # INVARIANT — marker-authoritative, no downgrade (please do NOT "fix" this
    # by re-adding a legacy-wins branch): the pre-move guard `and not
    # legacy.is_dir()` was removed ON PURPOSE. Re-adding it — or any variant
    # ("prefer a confirmed-live legacy", "treat marker + non-empty legacy as an
    # unresolved conflict needing reconciliation", "auto-recover newer legacy
    # data after a downgrade") — reintroduces exactly the split-brain / stale-
    # overwrite this change fixes. Downgrade/rollback is UNSUPPORTED BY DESIGN:
    # `~/.kiro/crew` is the only home; legacy is read once at the first-launch
    # copy, then never. A user who genuinely needs pre-move state restores it
    # themselves; the conflict is surfaced (WARNING + `kirocrew doctor`), never
    # silently reconciled.
    if marker.exists():
        # Observability (GPT 5.6): the new home is authoritative, but a
        # non-empty legacy dir alongside the marker is a conflicted state
        # (resurrection debris / a recreated legacy) that we proceed past
        # silently. Surface it loudly ONCE so an operator can investigate /
        # clean up, without changing the authoritative decision. Cheap check,
        # gated on legacy actually having content (the healthy post-migration
        # case has no legacy dir and never warns).
        try:
            if legacy.is_dir() and any(legacy.iterdir()):
                logger.warning(
                    "data-home conflict: completion marker present at %s but a "
                    "non-empty legacy home %s also exists — the new home is "
                    "authoritative and the legacy dir is NOT used (treated as "
                    "debris). Any data written there is ignored. Investigate and "
                    "remove it manually once confirmed stale (kirocrew doctor "
                    "surfaces this).",
                    new_home, legacy,
                )
        except OSError:
            pass  # best-effort probe — never block resolution
        _resolved_home = new_home
        return new_home

    # No marker yet → the migration has never completed. If a legacy home
    # exists it is the REAL pre-move data root and must be migrated.
    # No legacy data to migrate → this is a fresh install (the new home may or
    # may not exist yet). Create it, drop the marker, and use it.
    if not legacy.is_dir():
        _resolved_home = _finalize_fresh_home(new_home, marker)
        return _resolved_home

    # A legacy home exists and migration has never completed (no marker) — this
    # is a genuine pre-move install. Migrate: copy legacy into the new home,
    # verify, mark, delete legacy. (A live gateway on either home defers the
    # copy and joins that gateway's home; see migrate_home.)
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


def _sweep_ungated_archive_leftovers() -> None:
    """Delete any leftover archive/backup dir an EARLIER release created (best-effort).

    A release between the original ``~/.kirocrew`` -> ``~/.kiro/crew`` move and
    this one could have left ``~/.kirocrew.archived`` (a full rollback copy) or
    ``~/.kiro/crew.pre-migration/<timestamp>`` (a sidelined divergent-home
    backup) on disk. Neither is created by the current migration, and neither
    is on the security keystone anymore (see ``_ARCHIVED_LEGACY_DIR_NAME`` /
    ``_PRE_MIGRATION_BACKUP_DIR_NAME`` above) — so a leftover one is now
    UNGATED: its frozen credentials would otherwise be agent-readable
    indefinitely, with nothing to ever prompt a cleanup. This matches the rest
    of this migration's no-retention design: delete outright rather than shred
    just the credential leaves, so nothing ungated is left partially behind.

    Runs on every default-path ``config_dir()`` resolution (idempotent — a
    no-op once both are gone) rather than gating on a one-shot marker, so a
    leftover created between two starts (or one this sweep failed to remove)
    is still caught on the next start. Never raises and never blocks startup —
    a failure here is logged and left for the next start to retry.
    """
    archived = Path.home() / _ARCHIVED_LEGACY_DIR_NAME
    if archived.is_dir() and not archived.is_symlink():
        try:
            shutil.rmtree(archived)
            logger.warning(
                "removed ungated leftover data-home archive %s (from an earlier "
                "release; the current migration keeps no rollback copy)",
                archived,
            )
        except OSError:
            logger.warning("could not remove leftover archive %s", archived, exc_info=True)

    pre_migration_root = _default_home().parent / _PRE_MIGRATION_BACKUP_DIR_NAME
    if pre_migration_root.is_dir() and not pre_migration_root.is_symlink():
        try:
            shutil.rmtree(pre_migration_root)
            logger.warning(
                "removed ungated leftover divergent-home backup %s (from an earlier "
                "release; the current migration keeps no rollback copy)",
                pre_migration_root,
            )
        except OSError:
            logger.warning("could not remove leftover backup %s", pre_migration_root, exc_info=True)


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


def _valid_override_home() -> Path | None:
    """Return the resolved ``KIROCREW_HOME`` override iff it is set AND valid.

    A filesystem/drive root (``/`` on POSIX, ``C:\\`` on Windows) or a known
    POSIX system directory (``/usr``, ``/System``, ``/etc``) is refused —
    ``config_dir()`` ignores it and falls back to the default home, so the
    migration/conflict logic still applies. Shared by ``config_dir()`` and
    ``detect_data_home_conflict()`` so both agree on when an override is
    actually selected (GPT 5.6 MEDIUM: an invalid override must NOT suppress
    conflict detection).
    """
    override = os.environ.get("KIROCREW_HOME")
    if not override:
        return None
    p = Path(override).expanduser().resolve()
    # ``p == p.parent`` is the portable "is a root" test: a filesystem root or
    # Windows drive root is its own parent (``/`` → ``/``, ``C:\`` → ``C:\``),
    # so this refuses a bare "/" on every OS (not just POSIX).
    if p == p.parent or p.parts[:2] in (("/", "usr"), ("/", "System"), ("/", "etc")):
        return None
    return p


def config_dir() -> Path:
    p = _valid_override_home()
    if p is not None:
        p.mkdir(parents=True, exist_ok=True)
        return p
    if os.environ.get("KIROCREW_HOME"):
        logger.warning(
            "KIROCREW_HOME=%s is a system directory, ignoring",
            os.environ.get("KIROCREW_HOME"),
        )
    d = _maybe_migrate_legacy_home()
    d.mkdir(parents=True, exist_ok=True)
    # Drop the recovery-pointer breadcrumb outside ~/.kiro/ (default path only).
    # Best-effort + idempotent; guarded so a breadcrumb failure never blocks the
    # data-home resolution the whole app depends on.
    _write_recovery_breadcrumb(d)
    # One-shot (but re-checked every call, so a leftover created or missed
    # between starts is still caught) removal of an ungated archive/backup an
    # earlier release of this migration could have left behind. Default path
    # only — see _sweep_ungated_archive_leftovers.
    _sweep_ungated_archive_leftovers()
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


def detect_data_home_conflict() -> str | None:
    """Return a human-readable description of a conflicted data-home state, or
    ``None`` when the home is clean.

    Conflicted = the completion marker exists at the new home AND a **non-empty**
    legacy ``~/.kirocrew`` exists alongside it. Post-migration that legacy dir
    can only be resurrection debris (a stale/old or legacy-pinned process wrote
    back after the move completed); the new home is authoritative and the debris
    is never used, so its presence is a silent signal worth surfacing — both
    ``config_dir()`` (a one-time WARNING) and ``kirocrew doctor`` (a Data Home
    line) call this. Non-destructive, best-effort: any probe error, or a VALID
    ``KIROCREW_HOME`` override (which never migrates; an INVALID system-dir
    override falls back to the default home so the check still runs), yields
    ``None``.
    """
    if _valid_override_home() is not None:
        return None
    new_home = _default_home()
    legacy = _legacy_home()
    marker = new_home / MIGRATION_MARKER_NAME
    try:
        if marker.exists() and legacy.is_dir() and any(legacy.iterdir()):
            return (
                f"A legacy data home {legacy} still exists alongside the migrated, "
                f"authoritative home {new_home}. The legacy dir is NOT used (treated "
                f"as debris) — any data written there is ignored. Once you have "
                f"confirmed it holds nothing you need, delete the directory "
                f"{legacy} (POSIX: rm -rf {shlex.quote(str(legacy))} · Windows: "
                f"rmdir /s /q the same path)."
            )
    except OSError:
        return None
    return None


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
