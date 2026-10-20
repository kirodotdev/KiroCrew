"""Uninstalling an app, and the execution grant it drops and puts back.

``uninstall_app`` and the ``agent.apps_trusted`` grant in ``config.json`` it removes
(``_drop_trust_grant``), checks (``trust_grant_removal_blocked``) and restores when the
uninstall fails with the app still installed (``_restore_trust_grant``).

Part of the ``kiro_crew.apps.manager`` family: callers and tests reach every name here
as ``kiro_crew.apps.manager.<name>``, and a patch there reaches this module too
(:mod:`kiro_crew._facade_mirror`). This module never imports the facade.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
from pathlib import Path

from kiro_crew import platform_compat
from kiro_crew.apps.manager_builtins import (
    invalidate_orphan_cache,
    migrated_builtin_cleanup_applies,
)
from kiro_crew.apps.manager_records import (
    AppResult,
    InstalledApp,
    _read_installed,
    app_dir,
    refresh_app_config_keys,
)
from kiro_crew.apps.manager_tree import (
    _DEPS_STAGING_SWEEP_RE,
    _check_path_safety,
    _is_generated_deps_artifact_name,
    _remove_any_shape,
)
from kiro_crew.config.loader import (
    ConfigReadError,
    config_local_path,
    config_path,
    read_config_text,
    update_config_locked,
)
from kiro_crew.sel import sel

logger = logging.getLogger("kiro_crew.apps.manager")


# ---------------------------------------------------------------------------
# Uninstall
# ---------------------------------------------------------------------------


def uninstall_app(name: str, *, keep_data: bool = True, retired_builtin: bool = False) -> AppResult:
    """Uninstall an app while preserving its ``data/`` directory by default.

    Passing ``keep_data=False`` is the explicit purge action. Resource
    deregistration should be done before calling this.
    Built-in apps stay locked unless explicitly cleaning up an eligible retired builtin.
    """
    if not _check_path_safety(name):
        return AppResult(ok=False, name=name, error=f"unsafe app name: {name!r}")
    meta = _read_installed(name)
    if not meta:
        return AppResult(ok=False, name=name, error=f"app {name!r} is not installed")
    if retired_builtin and not migrated_builtin_cleanup_applies(name):
        return AppResult(
            ok=False, name=name, error="not a migrated builtin", error_code="not_orphaned"
        )
    if meta.lifecycle == "locked" and not retired_builtin:
        return AppResult(
            ok=False,
            name=name,
            error=f"app {name!r} cannot be uninstalled (lifecycle=locked) — use disable instead",
        )
    dest = app_dir(name)
    if not dest.is_dir():
        return AppResult(ok=False, name=name, error=f"app {name!r} is not installed")

    # Withdraw the execution grant FIRST, and abort the whole uninstall if it
    # cannot be withdrawn.
    #
    # Runtime admission is keyed on the app NAME, so one left behind can admit a
    # DIFFERENT app later installed under this name — in-process code execution
    # with no consent prompt, because the gate just sees a name it was told to
    # trust. New registry grants additionally bind their install repository, but
    # that does not make an orphaned runtime grant safe. Doing this AFTER the files
    # were deleted (as this did) produced a state
    # the user could not recover from: the app is gone, so there is nothing left to
    # uninstall and no retry that would clear the grant, while the name stays
    # armed. Ordering it first makes the failure retryable — nothing has been
    # destroyed, the user fixes the cause (typically an overlay-owned setting) and
    # runs uninstall again. Same reasoning as the revoke path, which runs teardown
    # before its config write for exactly this reason.
    try:
        # Recorded BEFORE the withdrawal so a failed delete below can put back
        # exactly what was there — and only when there WAS something. Restoring a
        # grant the app never held would be granting, not restoring.
        had_grant = _has_trust_grant(name)
        granted_repository = _trust_grant_repository(name)
        granted_local = _trust_grant_local(name)
        _drop_trust_grant(name)
    except Exception as exc:  # noqa: BLE001 - refuse rather than half-uninstall
        logger.warning("trust-grant cleanup on uninstall of %r failed", name, exc_info=True)
        return AppResult(
            ok=False,
            name=name,
            error=(
                f"not uninstalling {name!r}: its third-party execution grant could "
                f"not be removed ({exc}). The grant is keyed on the name, so removing "
                f"the app while it stands would let any future app installed under "
                f"this name run code without asking. Clear the cause and retry."
            ),
            error_code="trust_grant_not_removed",
        )

    from kiro_crew.apps.backend import _pinned_ancestors  # deferred: see below

    quarantined: list[tuple[Path, Path]] = []
    _data_pin = None
    _deps_lock: contextlib.ExitStack | None = None
    try:
        if keep_data:
            # ONE string for the pin below and every path-based step after it,
            # or verify() guards a path the renames and deletes do not use.
            dest = _pinned_ancestors(dest)
            data = dest / "data"
            # Move data to temp, remove app dir, move data back
            tmp_data = dest.parent / f".{name}-data-tmp"
            if platform_compat.is_link_or_junction(data):
                # A LINKED data dir would make every operation below act on
                # the link's TARGET - an app pointing data at another app's
                # tree (or anywhere else) would have this uninstall rename
                # and delete a foreign deps tree, and "preserve" the victim's
                # data as its own. Refuse: the gateway creates data/ as a
                # real directory, so a link here is never legitimate.
                raise OSError(
                    f"app {name!r} data directory is a symlink/junction; "
                    f"refusing to operate through it"
                )
            if data.is_dir():
                # The check above is a TOCTOU window against a RUNNING
                # backend (CLI uninstall does not stop it first): pin the
                # directory for the whole quarantine transaction - the
                # enumeration and every rename below go through the pin, so
                # a data/ swapped for a link after validation cannot
                # redirect them into another app's tree. Deferred import:
                # backend imports this module at load, so the reverse import
                # must not run at module level (same pattern as bridges).
                from kiro_crew.apps.backend import _PinnedDir

                _data_pin = _PinnedDir(data)
            if data.is_dir():
                # data/ preservation exists for USER data. The gateway's own
                # generated dependency trees (data/.kirocrew-deps*) must NOT
                # ride through an uninstall: a compromised app could plant
                # code there (sitecustomize.py), and a later reinstall under
                # the same name would prepend it to PYTHONPATH - revoked code
                # executing in a fresh install. Updates still keep the trees
                # (update never passes through here). QUARANTINE-RENAME, not
                # delete: the trees are renamed out of data/ (cheap, same
                # filesystem) so a later failure in THIS uninstall can put
                # them back - deleting first would leave a failed uninstall
                # (app still installed) stripped of its working dependencies.
                # Deletion happens only after every destructive step
                # committed. Links are unlinked directly (nothing to restore:
                # the link's target is untouched); rmtree would refuse them.
                assert _data_pin is not None  # bound by the pin block above
                _data_pin.verify()  # enumeration reads through the path
                # Serialize against ACTIVE provisioning: without the same
                # per-app lock the provision transaction holds, a pip run
                # racing this uninstall can create staging (or swap a tree
                # live) AFTER the enumeration below - the tree then survives
                # in preserved data and executes on a same-name reinstall.
                # The lock file is opened through the pin (dir_fd), same as
                # the provisioner's own open.
                _lflags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
                _lock_name = (
                    ".kirocrew-deps.lock"
                    if _data_pin.fd is not None
                    else str(data / ".kirocrew-deps.lock")
                )
                # Same creator election as the provisioner: uninstall can race
                # its first open before either caller holds the dependency lock.
                _lfd = platform_compat.open_create_or_existing(
                    _lock_name,
                    _lflags,
                    0o600,
                    dir_fd=_data_pin.fd,
                )
                _deps_lock = contextlib.ExitStack()
                _lf = _deps_lock.enter_context(os.fdopen(_lfd, "r+"))
                _deps_lock.enter_context(platform_compat.file_lock(_lf.fileno(), exclusive=True))
                # NOT the lock file here: we HOLD it - on Windows renaming
                # or deleting an open file fails with WinError 32, which
                # took every uninstall down. It is handled after release.
                _gen_names = [".kirocrew-deps", ".kirocrew-deps-prior"]
                # Staging names are suffixed per transaction; purge every one
                # that matches the STRICT generated pattern. A loose prefix
                # glob here quarantined app-owned same-prefix entries into
                # the doomed set, which the success path deletes at commit -
                # permanent loss of preserved data (same defect the post-move
                # sweep already guards against with the strict matcher).
                _gen_names.extend(
                    p.name
                    for p in data.glob(".kirocrew-deps-staging*")
                    if _DEPS_STAGING_SWEEP_RE.fullmatch(p.name) is not None
                )
                for gen in _gen_names:
                    gen_path = data / gen
                    if platform_compat.is_link_or_junction(gen_path):
                        platform_compat.unlink_link_or_junction(gen_path)
                    elif gen_path.exists():
                        doomed = dest.parent / f".{name}-deps-doomed{gen}"
                        # A stale crash leftover at the doomed name can be
                        # ANY shape (a file-shaped artifact quarantined by a
                        # prior run - rmtree refuses files, so a plain rmtree
                        # here would leave it and the rename below would
                        # fail forever after). Shape-aware, best-effort.
                        try:
                            _remove_any_shape(doomed)
                        except OSError:
                            pass
                        # Pinned move OUT of data/: the source entry is
                        # resolved against the held descriptor, so a swapped
                        # data/ cannot make this quarantine a foreign tree.
                        _data_pin.rename_out(gen, doomed)
                        quarantined.append((doomed, gen_path))
                _deps_lock.close()
                # The lock ARTIFACT rides in preserved data only when it is
                # a regular file (harmless: the next provisioning reopens
                # it without creation flags). Any OTHER shape - a directory or link an app
                # planted at the name - would poison the next transaction's
                # lock open, so purge those now that nothing holds the name.
                _lock_artifact = data / ".kirocrew-deps.lock"
                try:
                    if platform_compat.is_link_or_junction(_lock_artifact):
                        platform_compat.unlink_link_or_junction(_lock_artifact)
                    elif _lock_artifact.is_dir():
                        _data_pin.verify()
                        shutil.rmtree(str(_lock_artifact), ignore_errors=True)
                except OSError:
                    pass
                _data_pin.verify()
                shutil.move(str(data), str(tmp_data))
                # POST-MOVE sweep: the lock cannot be held across the move
                # (the open lock file lives INSIDE data/ and Windows refuses
                # to move a tree holding an open file), so a fast concurrent
                # provisioning could land a tree in the close-to-move
                # window. The moved tree is PRIVATE now - provisioners
                # target data/, which does not exist at this point - so purging here has
                # no race to lose: any deps tree that slipped in dies before
                # preservation.
                for _late in list(tmp_data.glob(".kirocrew-deps*")):
                    if not _is_generated_deps_artifact_name(_late.name):
                        continue  # app-owned name sharing the prefix: not ours
                    if _late.name == ".kirocrew-deps.lock" and _late.is_file():
                        continue  # regular lock file is harmless
                    try:
                        if platform_compat.is_link_or_junction(_late):
                            platform_compat.unlink_link_or_junction(_late)
                        elif _late.is_dir():
                            shutil.rmtree(str(_late))
                        else:
                            _late.unlink(missing_ok=True)
                    except OSError:
                        pass
                # FAIL LOUD on survivors: a running app still holds open
                # descriptors into the moved tree and can recreate or wedge
                # entries after the sweep - letting one ride into preserved
                # data hands a same-name reinstall revoked .pth code, the
                # exact property this purge exists for. Aborting keeps the
                # app installed and its trees restorable (the except arm
                # below restores the quarantined ones).
                _survivors = [
                    p.name
                    for p in tmp_data.glob(".kirocrew-deps*")
                    if _is_generated_deps_artifact_name(p.name)
                    and not (p.name == ".kirocrew-deps.lock" and p.is_file())
                ]
                if _survivors:
                    raise OSError(
                        f"app {name!r}: generated dependency artifacts resisted the "
                        f"uninstall purge ({', '.join(sorted(_survivors)[:3])}); "
                        f"refusing to preserve them into reinstallable data"
                    )
            if _data_pin is not None:
                _data_pin.close()
            shutil.rmtree(dest)
            if tmp_data.is_dir():
                dest.mkdir(parents=True, exist_ok=True)
                shutil.move(str(tmp_data), str(data))
        else:
            shutil.rmtree(dest)
        # Point of commit: every destructive step succeeded, the app is
        # uninstalled - NOW the quarantined trees die. A tree that resists
        # deletion here is logged, not fatal: under its doomed name it is
        # unreachable by any reinstall or PYTHONPATH (the security property
        # the purge exists for), unlike the silently-preserved live tree the
        # fail-loud rule targets.
        for doomed, _orig in quarantined:
            try:
                _remove_any_shape(doomed)
            except OSError as exc:
                logger.warning(
                    "Could not delete quarantined deps tree %s after uninstalling %s: %s",
                    doomed,
                    name,
                    exc,
                )
        quarantined = []
    except OSError as exc:
        if _deps_lock is not None:
            try:
                _deps_lock.close()
            except OSError:
                pass
        if _data_pin is not None:
            try:
                _data_pin.close()
            except OSError:
                pass
        # The delete failed, so the app is STILL INSTALLED. FIRST move the
        # preserved data back home if the failure struck mid-move: a raise
        # after ``data`` was renamed to its temp name would otherwise orphan
        # the user's entire data directory under a hidden dot-name. Restoring
        # it first also gives the quarantined-tree restore below its original
        # parent back.
        if keep_data:
            try:
                _tmp_restore = dest.parent / f".{name}-data-tmp"
                _data_restore = dest / "data"
                if _tmp_restore.is_dir() and not _data_restore.exists():
                    dest.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(_tmp_restore), str(_data_restore))
            except OSError as restore_exc:
                logger.warning(
                    "Could not restore preserved data for app %s after a " "failed uninstall: %s",
                    name,
                    restore_exc,
                )
        # ... then put the quarantined deps trees back (best-effort; if data
        # could not be restored it may still sit at its temp name, in which
        # case restore beside it there): a failed uninstall must not leave a
        # working app stripped of its provisioned dependencies.
        for doomed, orig in quarantined:
            try:
                target = orig
                if not orig.parent.exists():
                    alt = dest.parent / f".{name}-data-tmp" / orig.name
                    if alt.parent.exists():
                        target = alt
                if doomed.exists() and not target.exists():
                    doomed.rename(target)
            except OSError as restore_exc:
                logger.warning(
                    "Could not restore quarantined deps tree %s for app %s: %s",
                    doomed,
                    name,
                    restore_exc,
                )
        # ... and its grant was
        # withdrawn above, which would leave a trusted app silently stripped of the
        # permission the operator gave it, from an operation that did not even
        # succeed. Put it back.
        #
        # Restoring is not widening: this re-adds the grant the operator had already
        # made, to an app that is still on disk, returning the exact state that
        # existed before this call. The alternative shapes are both worse. Deferring
        # the withdrawal until after a successful delete re-opens the hole the
        # pre-delete ordering exists to close — a withdrawal that then fails leaves
        # the app GONE with its name still armed, and no app left to uninstall means
        # no retry can ever clear it. Leaving the grant withdrawn here is fail-safe
        # but silently punitive. Restoring keeps the withdrawal-first ordering (so a
        # withdrawal failure stays retryable with nothing destroyed) AND leaves a
        # failed uninstall with no side effect on trust.
        restore_note = ""
        try:
            _restore_trust_grant(
                name,
                had_grant,
                granted_repository,
                local=granted_local,
                expected_app=meta,
            )
        except Exception as restore_exc:  # noqa: BLE001 - report, never mask the real error
            logger.warning(
                "could not restore %r's execution grant after a failed uninstall",
                name,
                exc_info=True,
            )
            restore_note = (
                f" Its third-party execution grant could not be safely restored "
                f"({restore_exc}). Review the current installed app, then re-grant "
                f"it in Settings only if you still trust that occupant."
            )
        return AppResult(ok=False, name=name, error=f"failed to remove app: {exc}{restore_note}")

    refresh_app_config_keys()
    logger.info("Uninstalled app %s (keep_data=%s)", name, keep_data)

    # Withdraw the grant a SECOND time, now that the files are actually gone.
    #
    # The first withdrawal above deliberately runs BEFORE the delete so that a
    # failure is retryable with nothing destroyed. That ordering, though, leaves a
    # cross-process window a dashboard grant can land in — no in-process lock helps,
    # because this runs under `kirocrew app uninstall` in a DIFFERENT process:
    #
    #   this process: drop grant (no-op, none yet) ................ then ... rmtree
    #   dashboard:      app exists? yes -> write grant -> app still exists? yes -> 200
    #
    # Every check on both sides passes, and the grant is left standing over a name
    # no app occupies — the exact orphan both sides exist to prevent, and one that
    # would let a DIFFERENT app later installed under this name execute with no
    # consent prompt.
    #
    # Closing it needs no cross-process lock, only this ordering argument. The grant
    # is orphaned only if the write happened, the delete happened, AND the handler's
    # post-write existence check still saw the app. That check seeing the app means
    # it ran before this `rmtree` finished — so this second withdrawal, which runs
    # after the delete, necessarily runs after that write and therefore SEES the
    # grant. The handler's post-write check covers the opposite interleaving (delete
    # completes first, so the check finds nothing and rolls its own write back).
    # Between them the two guards leave no window, without either side blocking on
    # the other.
    residual = ""
    try:
        _drop_trust_grant(name)
    except Exception as exc:  # noqa: BLE001 - the app is already gone; report, never hide
        # Refusing the uninstall is not available here and would be a lie: the
        # files are deleted. So report it. A live grant over a name with no app is
        # precisely the state that must not stay quiet — it is invisible in the app
        # list (there is no app to show) and only surfaces when something new takes
        # the name.
        logger.warning(
            "app %r was uninstalled but its execution grant could not be withdrawn "
            "afterwards; the grant is still standing",
            name,
            exc_info=True,
        )
        residual = (
            f" WARNING: a third-party execution grant for {name!r} is still in "
            f"agent.apps_trusted and could not be removed ({exc}). Remove it in "
            f"Settings -> Security before installing anything under this name."
        )

    # Drop any dev-mode sentinel entry so an app later reinstalled under this
    # name does not inherit stale dev-mode serving/watching. Lazy import avoids
    # a module-level cycle (dev_mode imports from manager).
    try:
        from kiro_crew.apps.dev_mode import remove_dev_app

        remove_dev_app(name)
    except Exception:
        logger.debug("dev-mode cleanup on uninstall of %r failed", name, exc_info=True)
    # Deregistration unlinked the app's agent specs, but the per-agent lock file
    # beside each and the MCP gateway's overlay copy of it are named after the
    # same spec and would otherwise outlive the app. Lazy import: bridges imports
    # this module at load.
    try:
        from kiro_crew.apps.bridges import purge_uninstalled_agent_residue

        purge_uninstalled_agent_residue(name)
    except Exception:
        logger.debug("agent residue cleanup on uninstall of %r failed", name, exc_info=True)
    # The orphan set may have named this app; a successor installed under the
    # same name must not inherit its stale `orphaned` flag.
    invalidate_orphan_cache()
    return AppResult(ok=True, name=name, message=f"uninstalled {name}{residual}")


def trust_grant_removal_blocked(name: str) -> str | None:
    """Return why *name*'s execution grant could not be dropped, or ``None``.

    A read-only PRECONDITION. Both uninstall entry points run destructive,
    non-idempotent work (cron deregistration, the app's own ``onUninstall``
    script, backend stop, dependency cleanup) before they reach
    :func:`uninstall_app`, so a refusal discovered inside ``uninstall_app`` is
    not the retryable "nothing has been destroyed" case it was written as: it
    strands a half-removed app and re-runs ``onUninstall`` on every retry. Callers
    therefore ask this FIRST and abort while it is still free to abort — the same
    reason the cron cleanup is ordered ahead of the script.
    """
    # An overlay-owned grant cannot be dropped by writing config.json: the loader
    # deep-merges config.local.json OVER it and save() strips overlay-owned values
    # from the output, so the write is ineffective in both directions.
    #
    # Scoped to a grant this app actually holds. An overlay that pins
    # `apps_trusted` for OTHER apps says nothing about THIS uninstall, and gating
    # on the key's mere presence made every app un-uninstallable for any operator
    # who set it at all — a blanket refusal, not a grant-specific one.
    local = config_local_path()
    if local.is_file():
        try:
            raw_local = json.loads(read_config_text(local))
        except (OSError, UnicodeError, json.JSONDecodeError):
            raw_local = {}  # the loader ignores an unreadable overlay, so do we
        agent_local = raw_local.get("agent") if isinstance(raw_local, dict) else None
        if isinstance(agent_local, dict):
            overlay_grants = agent_local.get("apps_trusted")
            # A non-list overlay value cannot express a grant for this app, so
            # there is nothing here that a write would have to survive.
            if isinstance(overlay_grants, list) and name in overlay_grants:
                return f"apps_trusted is set in {local}, which overrides config.json"

    path = config_path()
    if path.is_file():
        try:
            json.loads(read_config_text(path))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            # Report rather than stay silent: a quiet bail here is precisely the
            # "uninstalled but still trusted" state the caller must not reach. The
            # write is still refused (it would erase everything else the file
            # holds) — it just is not refused quietly.
            return f"{path} is unreadable: {exc}"
    return None


def _drop_trust_grant(name: str) -> None:
    """Remove *name* from ``agent.apps_trusted``, if present.

    A no-op when the app held no grant, which is the common case. Refuses to write
    over an unparseable ``config.json`` for the same reason the trusted-apps
    endpoints do: ``KiroCrewConfig.load()`` degrades a corrupt file to defaults, so
    a blind load/save would erase everything else the file holds.
    """
    blocked = trust_grant_removal_blocked(name)
    if blocked:
        raise RuntimeError(blocked)

    # Operate on the BASE file's own list, not the merged view.
    #
    # `KiroCrewConfig.load()` deep-merges `config.local.json` OVER `config.json`,
    # and a list MERGE REPLACES rather than unions — so with base
    # `apps_trusted: ["foo"]` and overlay `["bar"]`, the merged value is `["bar"]`
    # and a merged-view check concludes `foo` holds no grant and removes nothing.
    # The base entry then survives the uninstall: inert while the overlay stands,
    # but live again the moment the operator edits or drops that overlay key, at
    # which point a DIFFERENT app installed under the name `foo` inherits a grant
    # nobody made for it. Reading merged state to decide a base-file write is the
    # bug; the two layers have to be reasoned about separately.
    #
    # Writing through `cfg.save()` cannot fix it either: save() deliberately
    # strips overlay-owned keys from its output, so the one key we need to rewrite
    # is exactly the one it will not emit. Hence a targeted edit of the raw base
    # document, which also keeps the blast radius to a single key instead of
    # re-serialising the whole config from the model.
    path = config_path()
    if not path.is_file():
        return
    try:
        raw = json.loads(read_config_text(path))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        # RAISE rather than return: a silent bail here is precisely the
        # "uninstalled but still trusted" state the caller must not reach. The
        # write is still refused — it would erase everything else the file holds.
        raise RuntimeError(f"{path} is unreadable: {exc}") from exc
    if not isinstance(raw, dict):
        return
    agent_raw = raw.get("agent")
    if not isinstance(agent_raw, dict):
        return
    base_grants = agent_raw.get("apps_trusted")
    repositories = agent_raw.get("apps_trusted_repositories")
    local_grants = agent_raw.get("apps_trusted_local")
    has_name = isinstance(base_grants, list) and name in base_grants
    has_repository = isinstance(repositories, dict) and name in repositories
    has_local = isinstance(local_grants, list) and name in local_grants
    # Orphaned kind metadata is inert without the name grant, but uninstall still
    # clears it so a later hand edit cannot unexpectedly reactivate old consent.
    # Preserve the no-grant fast path: ordinary uninstalls perform no config write.
    if not (has_name or has_repository or has_local):
        return

    def _revoke(raw_locked: dict) -> dict | None:
        # Re-derived under the advisory lock. The read above decided WHETHER a
        # grant exists (and the fast path for the ordinary no-grant uninstall);
        # this is the read the write is derived from, so a settings write that
        # landed in between is carried forward instead of being reverted.
        agent_locked = raw_locked.get("agent")
        if not isinstance(agent_locked, dict):
            return None
        base_locked = agent_locked.get("apps_trusted")
        repos_locked = agent_locked.get("apps_trusted_repositories")
        local_locked = agent_locked.get("apps_trusted_local")
        if not (
            (isinstance(base_locked, list) and name in base_locked)
            or (isinstance(repos_locked, dict) and name in repos_locked)
            or (isinstance(local_locked, list) and name in local_locked)
        ):
            # Another writer already revoked it. Skip the write rather than
            # rewriting the document with identical bytes.
            return None
        agent_locked["apps_trusted"] = [
            a for a in (base_locked if isinstance(base_locked, list) else []) if a != name
        ]
        if isinstance(repos_locked, dict):
            repos_copy = dict(repos_locked)
            repos_copy.pop(name, None)
            agent_locked["apps_trusted_repositories"] = repos_copy
        if isinstance(local_locked, list):
            agent_locked["apps_trusted_local"] = [a for a in local_locked if a != name]
        return raw_locked

    # Concurrency: this is the repo's standard config read-modify-write, and it
    # inherits that model exactly — no cross-process lock, atomic (tmp+rename) on
    # the way out so no reader can see a torn file. `read_config_for_update`'s own
    # docstring describes the same shape and the same residual exposure, and the
    # base branch has two dozen writers in it, `kirocrew config set` among them, so
    # a CLI write racing a dashboard write can drop the loser's settings today
    # regardless of this function. Closing that properly means locking at the config
    # layer for every writer at once, which is its own change; doing it for this one
    # writer would serialize it against nothing.
    #
    # What is in scope here is not adding exposure: the early returns above mean the
    # ordinary uninstall (no grant on the name) reaches no write at all, and a write
    # happens only when there really is a grant to withdraw — locked by
    # `test_uninstall_writes_no_config_at_all_when_there_is_no_grant`. The write is
    # also a single-key edit of the raw document rather than a re-serialisation of
    # the whole config, so what it can clobber is bounded to a concurrent edit that
    # lands inside the same read-to-write window.
    try:
        update_config_locked(path, mutate=_revoke, stamp_meta=False)
    except ConfigReadError as exc:
        # RAISE rather than return, for the same reason as the read above: a
        # silent bail is the "uninstalled but still trusted" state the caller
        # must not reach. Fails closed, so nothing was written.
        raise RuntimeError(f"{path} is unreadable: {exc}") from exc
    logger.info("Dropped third-party trust grant for uninstalled app %s", name)
    # Audited, because this REVOKES an execution permission. The dashboard's revoke
    # endpoint emits its own SEL event, but this path runs from `kirocrew app
    # uninstall` — so a grant could be withdrawn with nothing in the security event
    # log to show it, and the log is what an operator reconstructs a trust timeline
    # from. A permission boundary that moves silently is exactly what SEL exists to
    # make visible; the log records the transition, not merely the request that
    # caused it. Emitted AFTER the write so it attests something that actually
    # happened, and never allowed to fail the uninstall: losing the audit line is
    # bad, refusing to complete a withdrawal because the audit sink is unavailable
    # is worse.
    try:
        sel().log_api_access(
            caller="cli",
            operation="app_trust_revoke",
            outcome="allowed",
            resources=f"{name}=grant_removed_on_uninstall",
        )
    except Exception:  # noqa: BLE001 - the withdrawal already happened
        logger.warning("could not audit the trust withdrawal for %r", name, exc_info=True)


def _has_trust_grant(name: str) -> bool:
    """Whether the BASE ``config.json`` currently grants *name* execution.

    Reads the base document, not the merged view, for the same reason
    :func:`_drop_trust_grant` writes to it: an overlay list REPLACES rather than
    unions, so the merged value answers a different question than "is there a base
    entry here to put back".
    """
    path = config_path()
    if not path.is_file():
        return False
    try:
        raw = json.loads(read_config_text(path))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    if not isinstance(raw, dict):
        return False
    agent_raw = raw.get("agent")
    if not isinstance(agent_raw, dict):
        return False
    grants = agent_raw.get("apps_trusted")
    return isinstance(grants, list) and name in grants


def _trust_grant_repository(name: str) -> str:
    """Repository binding for *name* in the BASE config, or ``""``."""
    path = config_path()
    if not path.is_file():
        return ""
    try:
        raw = json.loads(read_config_text(path))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return ""
    agent_raw = raw.get("agent") if isinstance(raw, dict) else None
    if not isinstance(agent_raw, dict):
        return ""
    repositories = agent_raw.get("apps_trusted_repositories")
    repository = repositories.get(name) if isinstance(repositories, dict) else None
    return repository if isinstance(repository, str) else ""


def _trust_grant_local(name: str) -> bool:
    """Whether *name* has an explicit local grant marker in the BASE config."""
    path = config_path()
    if not path.is_file():
        return False
    try:
        raw = json.loads(read_config_text(path))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    agent_raw = raw.get("agent") if isinstance(raw, dict) else None
    if not isinstance(agent_raw, dict):
        return False
    local_grants = agent_raw.get("apps_trusted_local")
    return isinstance(local_grants, list) and name in local_grants


def _restore_trust_grant(
    name: str,
    had_grant: bool,
    repository: str = "",
    *,
    local: bool = False,
    expected_app: InstalledApp,
) -> None:
    """Put *name*'s grant back after an uninstall failed with the app still installed.

    A no-op when the app held no grant to begin with — restoring one it never had
    would be GRANTING execution permission as a side effect of a failed uninstall,
    which is the one thing this must never do. The durable installed record must
    match *expected_app* both before and after the config write, so a partial delete
    or same-name replacement cannot inherit the old occupant's consent. Also a
    no-op if a grant is already present, so a concurrent re-grant is not duplicated.
    """
    if not had_grant:
        return
    if _read_installed(name) != expected_app:
        raise RuntimeError(
            "the original installed app metadata is missing or changed; "
            "leaving its execution grant withdrawn"
        )
    if _has_trust_grant(name):
        return
    path = config_path()

    def _restore(raw: dict) -> dict:
        # Read and write inside one hold of the ``<config>.json.lock`` sidecar,
        # so the restore cannot republish a document that predates a concurrent
        # settings write. The CLI runs this in its own process, which is exactly
        # the writer an in-process asyncio lock cannot serialize against.
        agent_raw = raw.setdefault("agent", {})
        if not isinstance(agent_raw, dict):
            raise RuntimeError(f"{path} has a non-object agent section")
        # Append only when absent. The pre-lock ``_has_trust_grant`` check above
        # answered "should this restore run at all"; it is not the read this write
        # is derived from, so a dashboard re-grant landing between it and the
        # acquire would otherwise be duplicated into the persisted list. Same
        # guarded shape as the ``apps_trusted_local`` branch below, and the same
        # re-derive-under-the-lock rule ``_drop_trust_grant`` follows.
        grants = agent_raw.get("apps_trusted")
        granted = list(grants) if isinstance(grants, list) else []
        if name not in granted:
            granted.append(name)
        agent_raw["apps_trusted"] = granted
        if repository:
            repositories = agent_raw.get("apps_trusted_repositories")
            bindings = dict(repositories) if isinstance(repositories, dict) else {}
            bindings[name] = repository
            agent_raw["apps_trusted_repositories"] = bindings
        if local:
            local_grants = agent_raw.get("apps_trusted_local")
            local_names = list(local_grants) if isinstance(local_grants, list) else []
            if name not in local_names:
                local_names.append(name)
            agent_raw["apps_trusted_local"] = local_names
        return raw

    try:
        update_config_locked(path, mutate=_restore, stamp_meta=False)
    except ConfigReadError as exc:
        # Unchanged shape: a document that is not a readable JSON object refuses
        # the restore, and ``uninstall_app`` folds it into ``restore_note``.
        raise RuntimeError(f"{path} does not hold a JSON object: {exc}") from exc

    # The CLI and dashboard run in different processes, so a same-name
    # replacement can land after the pre-write check.  Recheck the exact durable
    # occupant after the config write; if it changed, remove every kind of grant
    # we just restored rather than arming replacement code with old consent.
    if _read_installed(name) != expected_app:
        try:
            _drop_trust_grant(name)
        except Exception as rollback_exc:  # noqa: BLE001 - report an armed name
            raise RuntimeError(
                "the installed app changed while its grant was restored and the "
                f"unsafe grant could not be withdrawn ({rollback_exc}); remove it "
                "in Settings before installing or running this name"
            ) from rollback_exc
        raise RuntimeError(
            "the installed app changed while its grant was restored; the grant " "was withdrawn"
        )
    logger.info("Restored %s's trust grant after a failed uninstall", name)
    try:
        sel().log_api_access(
            caller="cli",
            operation="app_trust_restore",
            outcome="allowed",
            resources=f"{name}=grant_restored_after_failed_uninstall",
        )
    except Exception:  # noqa: BLE001
        logger.warning("could not audit the trust restore for %r", name, exc_info=True)
