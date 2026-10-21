"""Installing an app from a local directory, and updating it in place.

``install_app`` and ``update_app``, the per-app lifecycle lock they share
(``app_lifecycle_lock``) and the registry coordinate one operation is scoped to
(``registry_source_repository``).

Part of the ``kiro_crew.apps.manager`` family: callers and tests reach every name here
as ``kiro_crew.apps.manager.<name>``, and a patch there reaches this module too
(:mod:`kiro_crew._facade_mirror`). This module never imports the facade.
"""

from __future__ import annotations

import logging
import os
import shutil
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import replace
from pathlib import Path
from typing import Iterator

from kiro_crew.apps.admission import app_admission_denied
from kiro_crew.apps.execution import repository_bound_grant_denied
from kiro_crew.apps.manager_records import (
    APP_MANIFEST_FILENAME,
    AppResult,
    InstalledApp,
    _approved_grants_after_manifest_change,
    _approved_grants_path,
    _declared_grant_entries,
    _effective_approved_grants,
    _held_back_entries,
    _now_iso,
    _pending_session_approval_after_manifest_change,
    _read_installed,
    _tree_swap,
    _update_retired_tree_prefix,
    _UpdateMarker,
    _write_approved_grants,
    _write_installed,
    app_data_dir,
    app_dir,
    get_app_manifest,
    hold_app_record_lock,
    holds_app_record_locks,
    refresh_app_config_keys,
)
from kiro_crew.apps.manager_tree import (
    InstalledTreeRefused,
    _check_path_safety,
    _copy_app_tree,
    _owned_data_dir,
    _remove_any_shape,
    _reserved_name_code,
    _temp_data_name_obstruction,
    _validate_source_path,
    gateway_data_dir_obstruction,
)
from kiro_crew.apps.manifest import AppManifest
from kiro_crew.loop_lock import LoopBoundLock
from kiro_crew.sel import sel

logger = logging.getLogger("kiro_crew.apps.manager")


# Per-app lifecycle locks, shared by every async entry point (registry
# install, dashboard install/update/uninstall routes).  Once the blocking
# copy runs off-loop, two concurrent operations on the same app could
# otherwise race the installed-check against the copy — and update/uninstall
# use shared move-aside names (``.{name}-data-tmp``), so an interleaving can
# destroy preserved user data.  Different apps proceed in parallel.
_LIFECYCLE_LOCKS: dict[str, LoopBoundLock] = {}


# Registry installs call ``install_app(source)`` / ``update_app(source)`` with one
# positional argument. Keep that internal callable contract (tests and
# downstream integrations replace these functions), while carrying the server-
# resolved repository through ``asyncio.to_thread`` without putting it in the
# app-controlled manifest. Context variables are copied into to_thread workers
# and remain task-local when two registry installs run concurrently.
_REGISTRY_SOURCE_REPOSITORY: ContextVar[str | None] = ContextVar(
    "kirocrew_registry_source_repository", default=None
)


@contextmanager
def registry_source_repository(repository: str) -> Iterator[None]:
    """Scope a sanitized registry coordinate to one manager operation."""
    coordinate = repository.strip()
    if not coordinate:
        raise ValueError("registry source repository is required")
    token = _REGISTRY_SOURCE_REPOSITORY.set(coordinate)
    try:
        yield
    finally:
        _REGISTRY_SOURCE_REPOSITORY.reset(token)


def _effective_source_repository(explicit: str) -> str:
    """Resolve an explicit/local source against the scoped registry source."""
    contextual = _REGISTRY_SOURCE_REPOSITORY.get()
    return contextual if contextual is not None else explicit.strip()


def app_lifecycle_lock(name: str) -> LoopBoundLock:
    """Return the per-app lock guarding install/update/uninstall (loop-bound).

    Must be called from (and the lock used on) the event loop thread; the
    guarded blocking work itself runs off-loop via executor/``to_thread``.
    This async lock serializes route handlers only and does not imply exclusive
    backend-lifecycle ownership. New lifecycle paths must go through the public
    ``start_app_backend`` or ``stop_app_backend`` entry points, which take
    ``_health_reconcile_lock`` and ``_lock`` and call
    ``_advance_lifecycle_locked``; they never mutate ``_processes`` directly.
    """
    if name not in _LIFECYCLE_LOCKS:
        _LIFECYCLE_LOCKS[name] = LoopBoundLock()
    return _LIFECYCLE_LOCKS[name]


# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------


@holds_app_record_locks
def install_app(
    source: str | Path,
    *,
    expected_name: str | None = None,
    source_repository: str = "",
) -> AppResult:
    """Install an app from a local directory path.

    1. Validate manifest and any caller-pinned app identity
    2. Copy to ``~/.kiro/crew/apps/{name}/``
    3. Write ``installed.json``

    Resource registration (agents, skills, crons) is handled separately
    by the bridge module — this function only manages files.
    """
    source = Path(source).expanduser().resolve()
    if not source.is_dir():
        sel().log_api_access(
            caller="app_install",
            operation="install",
            outcome="failed",
            resources=f"source={source!s}",
            error="source is not a directory",
        )
        return AppResult(ok=False, error=f"source is not a directory: {source}")

    errors = _validate_source_path(source)
    if errors:
        sel().log_api_access(
            caller="app_install",
            operation="install",
            outcome="failed",
            resources=f"source={source!s}",
            error="; ".join(errors),
        )
        return AppResult(
            ok=False,
            error="; ".join(errors),
            error_code=_reserved_name_code(source),
        )

    manifest = AppManifest.from_json_file(source / APP_MANIFEST_FILENAME)
    name = manifest.name
    if expected_name is not None and name != expected_name:
        detail = (
            f"app identity changed during install: expected {expected_name!r}, " f"found {name!r}"
        )
        sel().log_api_access(
            caller="app_install",
            operation="install",
            outcome="failed",
            resources=f"source={source!s}",
            error=detail,
        )
        return AppResult(
            ok=False,
            name=name,
            error=detail,
            error_code="app_identity_changed",
        )
    dest = app_dir(name)

    # Guard against path traversal in manifest name
    if not _check_path_safety(name):
        sel().log_api_access(
            caller="app_install",
            operation="path_safety_check",
            outcome="rejected",
            resources=f"name={name!r}",
            error="unsafe app name (path traversal attempt)",
        )
        return AppResult(ok=False, name=name, error=f"unsafe app name: {name!r}")

    # Admission: the app allowlist/ban/signature gate INSTALL, not just
    # activation, so a banned / non-allowlisted app never lands on disk.
    denied = app_admission_denied(name, manifest=manifest, action="install")
    if denied:
        sel().log_api_access(
            caller="app_install",
            operation="admission",
            outcome="rejected",
            resources=f"name={name!r}",
            error=denied,
        )
        return AppResult(ok=False, name=name, error=f"blocked by admission policy: {denied}")

    # Check if already installed — reject, use update_app() or uninstall first
    hold_app_record_lock(name)
    existing = _read_installed(name)
    if existing:
        sel().log_api_access(
            caller="app_install",
            operation="install",
            outcome="failed",
            resources=f"name={name!r}",
            error=f"already installed (v{existing.version})",
        )
        return AppResult(
            ok=False,
            name=name,
            error=f"app {name!r} is already installed (v{existing.version}). "
            f"Uninstall first or use the update endpoint.",
        )

    source_repository = _effective_source_repository(source_repository)
    trust_denied = repository_bound_grant_denied(name, repository=source_repository)
    if trust_denied:
        sel().log_api_access(
            caller="app_install",
            operation="trust_repository",
            outcome="rejected",
            resources=f"name={name!r}",
            error=trust_denied,
        )
        return AppResult(
            ok=False,
            name=name,
            error=trust_denied,
            error_code="app_trust_repository_mismatch",
        )

    # Preserve existing data/ directory (left behind by a prior default uninstall)
    existing_data = dest / "data" if dest.exists() else None
    # Use same temp name as uninstall_app/update_app so data stranded by a
    # crashed sibling operation is reclaimable by whichever lifecycle runs next.
    tmp_data = dest.parent / f".{name}-data-tmp"

    # BEFORE anything moves: the temp name must be free or hold the gateway's own
    # stale copy. A link or a file planted there is refused whole -- moving
    # `data/` onto a directory link would deposit it inside the link's target and
    # the restore would rename the link, not the data (see
    # _temp_data_name_obstruction). Nothing has been touched yet.
    tmp_obstruction = _temp_data_name_obstruction(tmp_data)
    if tmp_obstruction:
        sel().log_api_access(
            caller="app_install",
            operation="data_tmp_name",
            outcome="rejected",
            resources=f"name={name!r}",
            error=tmp_obstruction,
        )
        return AppResult(ok=False, name=name, error=tmp_obstruction)

    # Clean stale tmp from a previous failed install/uninstall.
    # Only remove tmp_data if the original data/ also exists (proving tmp is
    # truly stale). If data/ is gone, tmp_data may be the sole surviving copy.
    try:
        if _owned_data_dir(tmp_data):
            if existing_data and _owned_data_dir(existing_data):
                shutil.rmtree(str(tmp_data))
    except OSError as cleanup_exc:
        logger.error(
            "Failed to clean stale temp dir %s for app %s: %s",
            tmp_data,
            name,
            cleanup_exc,
        )
        sel().log_api_access(
            caller="app_install",
            operation="install",
            outcome="failed",
            resources=f"name={name!r}",
            error=f"stale temp cleanup: {cleanup_exc}",
        )
        return AppResult(
            ok=False,
            name=name,
            error=f"cannot clean stale temp dir {tmp_data}: {cleanup_exc}",
        )

    # BEFORE the transaction opens, the same question the copied tree and the
    # preview answer, asked of an app directory already standing at `dest` with
    # no record (a prior default uninstall's leftover, or an orphaned partial
    # copy): a `data` there that is a link or a file is not an owned directory,
    # so the move-aside below would skip it and the orphan cleanup would unlink
    # it -- a pre-existing entry gone with no refusal, no log line and no error,
    # where update_app and uninstall_app refuse the identical shape before they
    # mutate anything. Refused here with the predicate's own sentence, and
    # nothing has been touched yet.
    if dest.exists():
        refusal = gateway_data_dir_obstruction(dest)
        if refusal:
            sel().log_api_access(
                caller="app_install",
                operation="install",
                outcome="failed",
                resources=f"name={name!r}",
                error=refusal,
            )
            return AppResult(ok=False, name=name, error=refusal)

    try:
        if existing_data and _owned_data_dir(existing_data):
            shutil.move(str(existing_data), str(tmp_data))
        elif _owned_data_dir(tmp_data):
            # tmp_data is the sole surviving copy from a prior crash —
            # keep it intact; it will be restored after copytree.
            pass

        if dest.exists():
            # No installed metadata for this app (checked above), yet the
            # dest dir exists — an orphaned partial copy from a prior crash
            # (e.g. hard kill mid-install). Remove and re-copy fresh.
            logger.warning("Removing orphaned partial install at %s", dest)
            shutil.rmtree(dest)
        _copy_app_tree(source, dest)
        # The gateway's own root entries -- ``.app_secret`` and, when a preserved
        # directory is put back below, ``data`` -- are never the source's: a
        # source entry so named reaches the app directory verbatim only when the
        # gateway has nothing of its own to put there. ``.app_secret`` goes first, and goes
        # WITHOUT being followed: the copy keeps an in-tree link as a link, and
        # write_app_secret below opens the path it is given, so a shipped
        # ``.app_secret -> ui/leak.js`` would otherwise have the secret written
        # into a file the unauthenticated UI route serves. This is the removal
        # the install-time preview (copy_app_tree_as_installed) applies, so the
        # gate's judgment and the install agree on what stands here.
        _remove_any_shape(dest / ".app_secret")

        # Restore preserved data/ over whatever the source shipped under that
        # name (an empty data/ from the package, a file, a link) -- the same
        # link-safe removal update_app makes, so a shipped link is unlinked,
        # never traversed, and never left for the move to fail on.
        if _owned_data_dir(tmp_data):
            restored = dest / "data"
            _remove_any_shape(restored)
            shutil.move(str(tmp_data), str(restored))
        # What now stands at `data` is what app_data_dir() below will meet: the
        # preserved directory just put back, the source's own `data/` on a first
        # install, or nothing. Anything else there -- a shipped FILE, a dangling
        # link -- is a name its mkdir cannot stand beside, so it is refused HERE,
        # before installed.json exists, instead of raising after the record is
        # written and leaving a half-installed app behind. The preview copy the
        # desktop gate judges asks the same predicate (copy_app_tree_as_installed).
        refusal = gateway_data_dir_obstruction(dest)
        if refusal:
            raise InstalledTreeRefused(refusal)
    except InstalledTreeRefused as exc:
        # Nothing preserved is inside `dest`: a restored `data/` IS a directory,
        # so the refusal only fires where none was put back.
        shutil.rmtree(dest, ignore_errors=True)
        sel().log_api_access(
            caller="app_install",
            operation="install",
            outcome="failed",
            resources=f"name={name!r}",
            error=str(exc),
        )
        return AppResult(ok=False, name=name, error=str(exc))
    except (OSError, shutil.Error, ValueError) as exc:
        # Clean up partial install first
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        # Restore preserved data to the clean dest
        try:
            if _owned_data_dir(tmp_data):
                dest.mkdir(parents=True, exist_ok=True)
                shutil.move(str(tmp_data), str(dest / "data"))
        except OSError as restore_exc:
            logger.error(
                "Failed to restore preserved data for app %s; " "data left at %s: %s",
                name,
                tmp_data,
                restore_exc,
            )
        sel().log_api_access(
            caller="app_install",
            operation="install",
            outcome="failed",
            resources=f"name={name!r}",
            error=f"copy failed: {exc}",
        )
        return AppResult(ok=False, name=name, error=f"failed to copy app files: {exc}")

    # Write installed metadata
    meta = InstalledApp(
        name=name,
        version=manifest.version,
        displayName=manifest.displayName,
        enabled=False,  # installed but not enabled until explicitly enabled
        sessionApprovalConsentPending=bool(manifest.permissions.sessionApproval),
        approvedGrantsStored=True,
        installedAt=_now_iso(),
        source=str(source),
        # Persist the server-resolved repository at the first durable metadata
        # write.  The registry's richer set_app_provenance bookkeeping happens
        # later and may fail after the copied app is already the live occupant;
        # runtime admission must still remain bound to what was installed.
        sourceUrl=source_repository.strip(),
    )
    # Installing is the consent moment for everything the manifest declares.
    # Written before the record, so no record ever sits beside a missing set.
    _write_approved_grants(name, _declared_grant_entries(manifest.permissions))
    _write_installed(name, meta)

    # Create data directory
    app_data_dir(name)

    # Generate and write app secret for token-based auth (App Kit §5.1)
    # circular import: token_auth -> dashboard -> bridges -> manager
    from kiro_crew.dashboard.token_auth import generate_app_secret, write_app_secret

    write_app_secret(name, generate_app_secret())

    # Audit successful install for all callers (CLI, registry, dashboard)
    sel().log_api_access(
        caller="app_install",
        operation="install",
        outcome="success",
        resources=f"name={name!r} version={manifest.version}",
    )

    refresh_app_config_keys()
    logger.info("Installed app %s v%s from %s", name, manifest.version, source)
    return AppResult(
        ok=True,
        name=name,
        message=f"installed {name} v{manifest.version}",
        notice=("session_approval_reconsent" if manifest.permissions.sessionApproval else ""),
    )


# ---------------------------------------------------------------------------
# Update (re-install in place)
# ---------------------------------------------------------------------------


@holds_app_record_locks
def update_app(
    source: str | Path,
    *,
    expected_name: str | None = None,
    source_repository: str = "",
) -> AppResult:
    """Update an already-installed app from a local directory path.

    1. Validate new manifest
    2. Preserve ``data/`` directory
    3. Replace app files
    4. Update ``installed.json``

    ``expected_name``: when given, reject the update unless the source
    manifest's ``name`` matches — callers that lock/route by app name must
    not let a mismatched source mutate a different app.
    """
    source = Path(source).expanduser().resolve()
    if not source.is_dir():
        return AppResult(ok=False, error=f"source is not a directory: {source}")

    errors = _validate_source_path(source)
    if errors:
        return AppResult(ok=False, error="; ".join(errors))

    manifest = AppManifest.from_json_file(source / APP_MANIFEST_FILENAME)
    name = manifest.name
    if expected_name is not None and name != expected_name:
        return AppResult(
            ok=False,
            name=expected_name,
            error=f"source manifest name {name!r} does not match app {expected_name!r}",
        )
    dest = app_dir(name)

    # Guard against path traversal in manifest name
    if not _check_path_safety(name):
        return AppResult(ok=False, error=f"unsafe app name: {name!r}")

    # Admission: re-gate on update so a policy that tightens after install
    # (e.g. an app is later banned) blocks a subsequent update in place.
    denied = app_admission_denied(name, manifest=manifest, action="update")
    if denied:
        sel().log_api_access(
            caller="app_update",
            operation="admission",
            outcome="rejected",
            resources=f"name={name!r}",
            error=denied,
        )
        return AppResult(ok=False, name=name, error=f"blocked by admission policy: {denied}")

    # Consent to session-approval control is captured at install/enable, but the
    # route guard reads the LIVE manifest. Without this check a routine update
    # that adds ``permissions.sessionApproval`` would gain control of the user's
    # sessions with no consent moment. Read the old manifest BEFORE the tree is
    # replaced so the old grant remains the consent boundary, and read the record
    # against that same manifest: the replacement record lands in a new tree, so
    # the record's pending state must not depend on a second manifest read.
    hold_app_record_lock(name)
    old_manifest = get_app_manifest(name)
    existing = _read_installed(name, manifest=old_manifest)
    if not existing:
        return AppResult(ok=False, name=name, error=f"app {name!r} is not installed")

    source_repository = _effective_source_repository(source_repository)
    trust_denied = repository_bound_grant_denied(name, repository=source_repository)
    if trust_denied:
        sel().log_api_access(
            caller="app_update",
            operation="trust_repository",
            outcome="rejected",
            resources=f"name={name!r}",
            error=trust_denied,
        )
        return AppResult(
            ok=False,
            name=name,
            error=trust_denied,
            error_code="app_trust_repository_mismatch",
        )

    old_version = existing.version
    requested_session_approval = bool(manifest.permissions.sessionApproval)
    widened_session_approval = bool(
        requested_session_approval
        and not (old_manifest and old_manifest.permissions.sessionApproval)
    )
    # ``permissions.api`` / ``permissions.events`` are read live too. An added
    # entry is held back instead of disabling the app: it keeps running on the
    # set the owner approved until they approve the new one.
    # Read before the tree swap: the file lives in the tree the swap retires.
    existing_approved = _effective_approved_grants(name, existing)
    approved_grants = _approved_grants_after_manifest_change(
        existing_approved=existing_approved,
        old_permissions=old_manifest.permissions if old_manifest else None,
        new_permissions=manifest.permissions,
    )
    widened_grants = any(_held_back_entries(approved_grants, manifest.permissions).values())

    # Carry every persisted field forward from ``existing``, overriding only
    # what the update changes. Keeping this metadata inside the file transaction
    # means any write failure restores the old tree and old metadata together.
    meta = replace(
        existing,
        version=manifest.version,
        displayName=manifest.displayName,
        updatedAt=_now_iso(),
        enabled=False if widened_session_approval else existing.enabled,
        sessionApprovalConsentPending=_pending_session_approval_after_manifest_change(
            existing_pending=existing.sessionApprovalConsentPending,
            requested_session_approval=requested_session_approval,
            widened_session_approval=widened_session_approval,
        ),
        source=str(source),
        sourceUrl=source_repository.strip(),
        sourceRegistry="",
        sourceCommit="",
        sourceSigner="",
        approvedGrantsStored=True,
    )
    # Preserve data directory and app secret
    data_dir = dest / "data"
    secret_file = dest / ".app_secret"
    tmp_data = dest.parent / f".{name}-data-tmp"
    tmp_secret = dest.parent / f".{name}-secret-tmp"
    retired = dest.parent / (
        f"{_update_retired_tree_prefix(name)}{os.getpid()}-{os.urandom(4).hex()}"
    )
    preserved_data = False
    preserved_secret = False

    # BEFORE the transaction opens: what stands at `data` must be something the
    # gateway can move aside and put back whole -- a real directory. A LINK is
    # not: the move below relocates the link itself to `.{name}-data-tmp` beside
    # the app directory, where a relative target does not resolve, so nothing
    # would be put back and the directory it named would be retired -- and
    # deleted -- with the old tree on this function's own success path. A FILE
    # is not either: `_owned_data_dir` is false for it, so the move skips it, the
    # old tree carrying it is retired, the post-copy ask below inspects only the
    # NEW tree, and the file is deleted with the retired tree while app_data_dir
    # creates an empty directory in its place -- silently, with ok=True. The
    # install refuses to create either shape (gateway_data_dir_obstruction); one
    # that predates the refusal, or that the app's own runtime made, is refused
    # here by that same predicate -- the one question every entry point asks
    # before it mutates -- whole, with the old tree and record untouched. The
    # predicate counts a Windows directory junction as a link, as the rest of
    # the app manager does.
    refusal = gateway_data_dir_obstruction(dest)
    if refusal:
        sel().log_api_access(
            caller="app_update",
            operation="update",
            outcome="failed",
            resources=f"name={name!r}",
            error=refusal,
        )
        return AppResult(ok=False, name=name, error=refusal)

    # BEFORE the transaction opens, too: the temp name must be free or the
    # gateway's own stale copy (see _temp_data_name_obstruction and install_app).
    tmp_obstruction = _temp_data_name_obstruction(tmp_data)
    if tmp_obstruction:
        sel().log_api_access(
            caller="app_update",
            operation="data_tmp_name",
            outcome="rejected",
            resources=f"name={name!r}",
            error=tmp_obstruction,
        )
        return AppResult(ok=False, name=name, error=tmp_obstruction)

    # Clean up stale tmp files from a previous failed update
    if _owned_data_dir(tmp_data) and _owned_data_dir(data_dir):
        shutil.rmtree(str(tmp_data))
    if tmp_secret.is_file() and secret_file.is_file():
        tmp_secret.unlink()

    try:
        update_marker = _UpdateMarker.hold(name)
    except OSError as exc:
        return AppResult(ok=False, name=name, error=f"failed to update app files: {exc}")
    with _tree_swap(name), update_marker:
        try:
            # The swap below takes the approved-set file with the old tree. A record
            # that stored a set must say so before then, or a reader meeting it
            # beside the missing file would approve everything declared.
            if not existing.approvedGrantsStored and _approved_grants_path(name).is_file():
                _write_installed(name, existing)
            if _owned_data_dir(data_dir):
                shutil.move(str(data_dir), str(tmp_data))
                preserved_data = True
            if secret_file.is_file():
                shutil.move(str(secret_file), str(tmp_secret))
                preserved_secret = True

            # Keep the complete old tree until the replacement and its metadata are
            # durable. Source-owned installed.json never reaches the live tree.
            os.replace(dest, retired)
            _copy_app_tree(source, dest)
            # ``.app_secret`` is the gateway's whether or not one is preserved: the
            # copied entry goes, unfollowed, before the preserved file moves back
            # (see install_app; the same removal the preview copy applies).
            _remove_any_shape(dest / ".app_secret")

            if _owned_data_dir(tmp_data):
                restored = dest / "data"
                _remove_any_shape(restored)
                shutil.move(str(tmp_data), str(restored))
            if tmp_secret.is_file():
                shutil.move(str(tmp_secret), str(dest / ".app_secret"))
            # Before the record: a root `data` that is not a directory (nothing
            # preserved was put back over it) would fail app_data_dir() below, after
            # the new metadata was durable -- refused here instead, and the rollback
            # restores the old tree and record (see install_app).
            refusal = gateway_data_dir_obstruction(dest)
            if refusal:
                raise InstalledTreeRefused(refusal)
            # The new tree carries no set (the copy skips the file); a rollback
            # restores the old tree's file with it.
            _write_approved_grants(name, approved_grants)
            _write_installed(name, meta)
        except (OSError, shutil.Error, ValueError, InstalledTreeRefused) as exc:
            rollback_error = ""
            try:
                if retired.is_dir():
                    restored_data = dest / "data"
                    restored_secret = dest / ".app_secret"
                    if preserved_data and not _owned_data_dir(tmp_data) and restored_data.is_dir():
                        shutil.move(str(restored_data), str(tmp_data))
                    if preserved_secret and not tmp_secret.is_file() and restored_secret.is_file():
                        shutil.move(str(restored_secret), str(tmp_secret))
                    _remove_any_shape(dest)
                    os.replace(retired, dest)
                if _owned_data_dir(tmp_data):
                    restored = dest / "data"
                    _remove_any_shape(restored)
                    shutil.move(str(tmp_data), str(restored))
                if tmp_secret.is_file():
                    restored_secret = dest / ".app_secret"
                    _remove_any_shape(restored_secret)
                    shutil.move(str(tmp_secret), str(restored_secret))
                _write_installed(name, existing)
            except (OSError, shutil.Error, ValueError) as rollback_exc:
                rollback_error = f"; rollback failed: {rollback_exc}"
                logger.error("Failed to restore app %s after update error", name, exc_info=True)
            return AppResult(
                ok=False,
                name=name,
                error=f"failed to update app files: {exc}{rollback_error}",
            )

        try:
            _remove_any_shape(retired)
        except OSError:
            logger.warning("Could not remove retired app tree for %s", name, exc_info=True)

    # Ensure data directory exists
    app_data_dir(name)
    refresh_app_config_keys()

    logger.info(
        "Updated app %s: v%s -> v%s from %s",
        name,
        old_version,
        manifest.version,
        source,
    )
    if widened_session_approval:
        sel().log_api_access(
            caller="app_update",
            operation="session_approval_widened",
            outcome="disabled",
            resources=f"name={name!r}",
            error="update added permissions.sessionApproval; re-enable to consent",
        )
        return AppResult(
            ok=True,
            name=name,
            message=(
                f"updated {name} v{old_version} -> v{manifest.version}; "
                "disabled because this version newly requests session approval "
                "control -- review it on the app page and enable again"
            ),
            notice="session_approval_reconsent",
        )
    if widened_grants:
        sel().log_api_access(
            caller="app_update",
            operation="grants_widened",
            outcome="staged",
            resources=f"name={name!r}",
            error="update added permissions.api/events entries; approve to grant them",
        )
        return AppResult(
            ok=True,
            name=name,
            message=(
                f"updated {name} v{old_version} -> v{manifest.version}; "
                "this version requests new permissions, which stay off until you "
                "approve them on the app page"
            ),
        )
    return AppResult(
        ok=True,
        name=name,
        message=f"updated {name} v{old_version} -> v{manifest.version}",
    )
