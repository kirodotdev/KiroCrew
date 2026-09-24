"""The install transaction: gates, clone, identity, build, ``onInstall``, register.

``install_from_registry`` runs every consent and admission gate before repository
bytes run, clones and builds through ``_clone_build_app`` with the identity and
admission gates between clone and build, checks again after the build and after
``onInstall``, records provenance from the final state, and restores or reports
every moved-aside checkout from its single ``finally``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal

from kiro_crew import platform_compat
from kiro_crew.apps import install_receipt
from kiro_crew.apps.admission import app_admission_denied, verified_signer
from kiro_crew.apps.execution import (
    app_execution_denied,
    repository_bound_grant_denied,
    trusted_app_repository,
)
from kiro_crew.apps.manager import (
    InstalledTreeRefused,
    copy_app_tree_as_installed,
    get_app,
    install_app,
    preserved_data_awaits,
    registry_source_repository,
    set_app_provenance,
    update_app,
)
from kiro_crew.apps.manifest import (
    RESERVED_APP_NAME_CODE,
    AppManifest,
    app_name_error,
    is_module_style_entry_point,
    is_reserved_app_name,
    requirements_in_tree,
    runtime_provisions_requirements,
    spawn_launches_entry_point_as_python,
)
from kiro_crew.apps.registry_pipeline import _FACADE, _facade
from kiro_crew.apps.registry_pipeline.catalog import (
    SOURCE_REGISTRY_PREFIX,
    _is_catalog_row,
    _resolve_install_entry,
)
from kiro_crew.apps.registry_pipeline.checkout import (
    _COMMIT_SHA_RE,
    _communicate_with_timeout,
    _git_clone_or_pull,
    _kill_process_group,
    _resolved_clone_commit,
)
from kiro_crew.apps.registry_pipeline.git_targets import (
    _entry_git_url,
    _git_target_is_unsupported,
    _looks_like_git_url,
    _strip_git_target_userinfo,
)
from kiro_crew.apps.registry_pipeline.indexes import _owner_tier_confirmed
from kiro_crew.apps.registry_pipeline.manifests import _contained_join, _fetch_app_manifest
from kiro_crew.apps.registry_pipeline.recovery import (
    _STALE_CHECKOUT_RETENTION_DAYS,
    _app_sources_dir,
    _restorable_or_none,
    _restore_moved_aside,
    _stale_sibling,
    _sweep_stale_checkouts,
    app_source_dir,
)
from kiro_crew.apps.registry_pipeline.sources import (
    _owner_designated_repo_target,
    _sel_credential_grant,
)
from kiro_crew.apps.registry_pipeline.subprocess_env import _detect_probe_env, minimal_env
from kiro_crew.sandbox import (
    cgroup_scope_argv,
    create_subprocess_limited,
    sandboxed_spawn_argv,
    sandboxed_spawn_argv_async,
    wrap_argv,
    wrap_argv_async,
)
from kiro_crew.sel import sel

logger = logging.getLogger(_FACADE)


class StreamingLogLines(list):
    """Drop-in replacement for ``list[str]`` that also pushes to an asyncio.Queue.

    Used by the streaming install endpoint to forward log lines in real-time
    without changing the signature of ``install_from_registry`` or any of its
    callees.  All existing ``log_lines.append()`` / ``.extend()`` calls work
    unchanged — the queue receives each line as it's added.
    """

    def __init__(self, queue: asyncio.Queue[str | None]) -> None:
        super().__init__()
        self._queue = queue

    def append(self, line: str) -> None:  # type: ignore[override]
        super().append(line)
        try:
            self._queue.put_nowait(line)
        except asyncio.QueueFull:
            pass  # drop if consumer is too slow

    def extend(self, lines) -> None:  # type: ignore[override]
        for line in lines:
            self.append(line)


_SCRIPT_TIMEOUT = 300


def _remote_controlled_url(entry: dict[str, Any]) -> bool:
    """Whether *entry*'s clone URL came from content we do not control.

    Drives the CREDENTIAL posture: a True answer means the clone runs
    credential-free and strict-sandboxed (:func:`anonymous_git_env`), because the
    URL is not one the owner typed.

    Both markers qualify. ``_registry`` is an external index's row. ``_catalog`` is
    the official catalog's, whose URL arrives in a document fetched over the network
    whose signature this client does not yet verify -- so it is remote-controlled in
    exactly the same way. Reading "no ``_registry``" as "owner-designated" held only
    while the sole marker-less rows came from the wheel's bundled seed, which the
    owner installed deliberately.
    """
    return bool(entry.get("_registry")) or bool(entry.get("_catalog"))


def _official_entry(entry: dict[str, Any]) -> bool:
    """Whether *entry* is an app WE list, which decides install-receipt eligibility.

    Deliberately NOT the negation of :func:`_remote_controlled_url`. A catalog row
    is remote-controlled (credential-free) AND official (receipt fires); collapsing
    both onto one boolean is what made the catalog row take owner credentials.
    """
    return not entry.get("_registry")


# The verb of the action a registry install is performing: "update" when an
# installed record exists for the app, "install" otherwise. The page titles its
# log panel "Install log" or "Update log" by that record, and
# `install_from_registry` chooses `update_app` or `install_app` by it, so every
# line this module streams about a refusal reads the same fact -- never a second
# fact (a pre-existing checkout is not an installed app) and never a guess.
_InstallVerb = Literal["install", "update"]

# One whole string per verb. Nothing composes a sentence from a verb word, so a
# reader (or a test) finds each line as it is streamed. `{reason}` is the
# sentence the same refusal returns as `error`.
_REFUSAL_LINES: dict[str, str] = {
    "install": "Refusing install: {reason}",
    "update": "Refusing update: {reason}",
}


def _refusal_line(verb: _InstallVerb, reason: object) -> str:
    """The streamed line that refuses a *verb* attempt for *reason*."""
    return _REFUSAL_LINES[verb].format(reason=reason)


async def _refuse_identity_mismatch(
    entry_name: str,
    cloned_name: str,
    repo: str,
    clone_root: Path,
    log_lines: list[str],
    *,
    created_this_run: bool,
    pre_pull_commit: str = "",
    manifest_relpath: str = "app.json",
    manifest_snapshot: bytes | None = None,
    restore_from: Path | None = None,
    appeared_layout: tuple[Path, frozenset[str]] | None = None,
    verb: _InstallVerb = "install",
) -> dict[str, Any]:
    """Abort an install whose cloned repo claims a different app name.

    A checkout **created by this run** is deleted so the squatting source (and
    any build output) leaves no residue in the entry's ``app-sources/`` slot — a
    leftover would also be preferred by :func:`_fetch_app_manifest` on the next
    listing, letting a refused repo keep answering as this app.  Nothing has
    been written under ``~/.kiro/crew/apps/`` at this point, so removing the
    fresh clone leaves the machine exactly as it was before the install.

    A checkout that **pre-existed** (the update path — ``git pull`` brought in a
    commit whose manifest renamed itself, or a build/script rewrote it in the
    working tree) is the installed app's source workspace, so it is preserved —
    but rolled back to its last-good state (``git reset --keep`` to the
    pre-pull commit plus a manifest restore from HEAD, both edit-preserving):
    left at the renamed manifest, the prefetch would re-read it and re-reject
    every retry before a fixed remote could ever be pulled.

    *appeared_layout* is given by the POST-SCRIPT caller only -- the package
    directory and the layout snapshot taken before ``setup.onInstall`` -- and
    routes the rollback through :func:`_roll_back_post_script_refusal`, so a
    layout file the script created is removed before the checkout is rolled back
    exactly as it is for the other gates after the script; the pre-script callers
    have no script window to clean up after and roll back directly.

    *verb* is the action this run performs ("install" or "update", read from the
    installed record by :func:`install_from_registry`); the streamed refusal
    line names it, the way the page's log panel is titled. It defaults to the
    fresh-install reading every caller had before the two were told apart.
    """
    declared = cloned_name or "<missing>"
    if not created_this_run:
        log_lines.append(
            "Preserving pre-existing source checkout (rolled back to its "
            "last-good state): the refused update installed nothing, and the "
            "workspace belongs to the already-installed app"
        )
    if appeared_layout is not None:
        app_source, layout_before = appeared_layout
        await _roll_back_post_script_refusal(
            entry_name,
            app_source,
            clone_root,
            log_lines,
            layout_before=layout_before,
            checkout_preexisted=not created_this_run,
            pre_pull_commit=pre_pull_commit,
            manifest_relpath=manifest_relpath,
            manifest_snapshot=manifest_snapshot,
            restore_from=restore_from,
        )
    else:
        await _unpoison_rejected_checkout(
            entry_name,
            clone_root,
            log_lines,
            checkout_preexisted=not created_this_run,
            pre_pull_commit=pre_pull_commit,
            manifest_relpath=manifest_relpath,
            manifest_snapshot=manifest_snapshot,
            restore_from=restore_from,
        )
    error = (
        f"registry entry {entry_name!r} resolves to a repo whose app.json declares "
        f"{declared!r} — refusing to {verb} an app under an identity that differs "
        f"from its registry entry"
    )
    log_lines.append(_refusal_line(verb, error))
    try:
        sel().log_api_access(
            caller="app_install_from_registry",
            operation="identity_mismatch",
            outcome="rejected",
            resources=(
                f"name={entry_name!r} declared={declared!r} "
                f"repo={_strip_git_target_userinfo(repo)}"
            ),
            error="cloned manifest name does not match registry entry name",
        )
    except Exception as exc:  # an audit failure must never mask the refusal
        logger.debug("SEL audit failed for %s identity mismatch: %s", entry_name, exc)
    return {"ok": False, "name": entry_name, "error": error, "log": "\n".join(log_lines)}


async def _clone_build_app(
    git_url: str,
    app_name: str,
    log_lines: list[str],
    branch: str = "main",
    *,
    index_originated: bool = False,
    subdirectory: str = "",
    entry_repo: str = "",
    commit: str = "",
    self_managed: bool = False,
    verb: _InstallVerb = "install",
) -> dict[str, Any]:
    """Clone an app repo, gate its identity, then run its build.

    Source is cloned to ``~/.kiro/crew/app-sources/{app_name}/`` (persistent;
    survives reboots and is reused for updates).  **The identity gate runs
    BETWEEN clone and build**: the cloned ``app.json`` (under *subdirectory*
    when set) must declare *app_name* before :func:`_run_app_build` executes —
    build ecosystems run repo-authored lifecycle scripts (an npm ``preinstall``,
    a ``setup.py``), so validating only after the build would let a mismatched
    repo execute code despite the refusal.

    *index_originated* is forwarded to :func:`_git_clone_or_pull` to pick the
    credential posture (credential-free + strict sandbox for repos whose URL
    came from an external registry index — see that function's docstring).

    *self_managed* is the registry entry's resource ownership (``resources:
    "app"``), forwarded to :func:`_run_app_build` for the desktop gate, where it
    is required; ``install_from_registry`` -- the one caller that reads the
    entry -- always passes it, and the default names the gateway-managed
    install the way *index_originated*'s names the catalog posture.

    *verb* is the action the run performs -- "update" when an installed record
    exists, "install" otherwise -- and only names the streamed refusal lines,
    the way :func:`_refuse_identity_mismatch` describes.

    Returns ``{"ok": True, "pkg_dir": <Path>}`` on success or
    ``{"ok": False, "error": ...}`` on failure/refusal.
    """
    # Lock-free: the caller (route handler) holds app_lifecycle_lock(name)
    # across the complete lifecycle transaction — clone/build, copy,
    # registration, and backend startup — so nested acquisition here would
    # deadlock (asyncio.Lock is not reentrant).
    # The restoration state is collected HERE, at the single return, rather than
    # stamped onto the result inside `_clone_build_app_locked`. That function has
    # several exits and the state was only attached on the successful one, so a
    # post-fetch failure -- a subdirectory that escapes containment, an identity
    # mismatch, a rejected admission -- dropped it: the caller's `finally` had
    # nothing to restore from AND `_report_retained_stale_checkouts` iterated an
    # empty list, so a non-restorable (origin-mismatch) checkout was stranded as a
    # `.stale-*` sibling, unreported, until the retention sweep deleted it. Two
    # lists the callee fills and this one exit reads cannot be forgotten by a new
    # exit: `pending_cleanup` is every move-aside this run, `restorable_stale` the
    # same-origin subset a failure-path restore may put back.
    pending_cleanup: list[Path] = []
    restorable_stale: list[Path] = []
    try:
        result = await _clone_build_app_locked(
            git_url,
            app_name,
            log_lines,
            branch=branch,
            index_originated=index_originated,
            subdirectory=subdirectory,
            entry_repo=entry_repo,
            commit=commit,
            self_managed=self_managed,
            pending_cleanup=pending_cleanup,
            restorable_stale=restorable_stale,
            verb=verb,
        )
    except BaseException:
        # Cancellation and exceptions never reach the stamping line below, so the
        # caller's `finally` would see no state and the user's moved-aside checkout
        # would go to the retention sweep. There is no result dict to carry it on
        # this path, so restore HERE, where both the state and the destination are
        # known. `BaseException` on purpose: `CancelledError` is the reported case.
        #
        # SYNCHRONOUS, and that is the point: `await` during cancellation re-enters a
        # loop that is being torn down, which surfaces as `RuntimeError: Event loop is
        # closed` -- a failure this handler caused on three CI platforms at once. The
        # work is a rmtree plus a rename, so it never needed the loop.
        if restorable_stale:
            _restore_moved_aside(
                restorable_stale[0],
                app_source_dir(app_name),
                log_lines,
                "the build was interrupted",
            )
        # The restore above puts back the same-origin subset; the NON-restorable
        # move-asides (origin-mismatch tree-asides, deliberately kept) are left on
        # disk as `.stale-*` siblings. On this exception path there is no result
        # dict, so the caller's `finally`-owned reporter never learns of them and
        # the age-based sweep would delete a checkout the user was never told
        # about. Report them through the SHARED reporter -- the one owner of the
        # "Previous checkout retained at" wording -- so this path and the finally
        # can never drift apart. `filter_restorable=True` skips the same-origin
        # subset the restore above just put back, matching the finally's
        # post-restore call. Synchronous: the reporter only appends to a list and
        # logs, so it needs no loop (awaiting during cancellation re-enters a
        # closing loop -- see the SYNCHRONOUS note above).
        _report_retained_stale_checkouts(
            {
                "_pending_stale_cleanup": pending_cleanup,
                "_restorable_stale": restorable_stale,
            },
            log_lines,
            filter_restorable=True,
        )
        raise
    if isinstance(result, dict):
        # Stamp the FULL move-aside state on EVERY dict result crossing this
        # single exit -- refusals included -- so the caller's
        # `_report_retained_stale_checkouts` names a retained non-restorable
        # checkout instead of dropping it. This is report/restore metadata only:
        # it changes no path that gets restored or deleted.
        if pending_cleanup:
            result["_pending_stale_cleanup"] = list(pending_cleanup)
        if restorable_stale:
            result["_restorable_stale"] = list(restorable_stale)
    return result


async def _unpoison_rejected_checkout(
    app_name: str,
    pkg_dir: Path,
    log_lines: list[str],
    *,
    checkout_preexisted: bool,
    pre_pull_commit: str,
    manifest_relpath: str = "app.json",
    manifest_snapshot: bytes | None = None,
    restore_from: Path | None = None,
) -> None:
    """Un-poison a checkout after an identity/admission rejection.

    The prefetch prefers the local checkout, so a checkout left sitting at a
    rejected state makes every retry re-reject at prefetch before it could
    ever pull a fixed remote — a permanently stuck app.

    A checkout created THIS RUN is deleted (no residue) and, when the run
    replaced a moved-aside previous checkout (*restore_from*), that previous
    checkout is renamed back into the slot — otherwise the rejection would
    leave the slot empty and strand the user's old workspace as a
    sweeper-doomed ``.stale-*`` sibling.

    A pre-existing workspace is rolled back to its pre-pull commit with
    ``git reset --keep`` (preserves uncommitted local edits; aborts on
    conflict), then the manifest is restored to its exact pre-update
    working-tree bytes (*manifest_snapshot*) — undoing whatever the pull,
    build, or ``onInstall`` script did to ``app.json`` WITHOUT discarding the
    user's own uncommitted manifest edits. Only when no snapshot exists does
    it fall back to ``git --literal-pathspecs checkout --`` from HEAD
    (literal pathspecs keep an index-controlled subdirectory from being
    parsed as pathspec magic). Best-effort throughout: a cleanup failure is
    logged, never raised — the refusal it follows must stand regardless.

    *manifest_relpath* is the untrusted registry-declared manifest path the
    caller built (``f"{subdirectory}/app.json"``, or plain ``app.json`` when
    no subdirectory was declared). A build step or ``onInstall`` script runs
    with write access to the checkout BEFORE some callers reach this cleanup,
    and can plant a symlink at the manifest path — the subdirectory OR the
    leaf — after an earlier containment check already passed; this restore
    then runs unsandboxed as the Kiro Crew process, so it must not trust that
    earlier check. Containment of the FULL manifest path is re-verified HERE,
    at the point of the write, against the CURRENT on-disk state: on a
    failure the manifest restore (both the raw-write and the git-checkout
    fallback) is skipped so neither can be redirected outside *pkg_dir*
    through a symlink planted after the caller's check. The pre-pull
    ``git reset`` above is unaffected — it targets the whole checkout, not
    the manifest path.
    """
    if not checkout_preexisted:
        await asyncio.to_thread(shutil.rmtree, pkg_dir, ignore_errors=True)
        if restore_from is not None:
            try:
                await asyncio.to_thread(restore_from.rename, pkg_dir)
                log_lines.append(
                    "Restored the previous checkout after rejecting the replacement clone"
                )
            except OSError as exc:
                log_lines.append(
                    f"WARNING: could not restore the previous checkout from "
                    f"{restore_from.name}: {exc}; it is retained there for manual recovery"
                )
        return

    async def _run_git(argv: list[str]) -> int:
        cmd, _cleanup = await wrap_argv_async(argv, mode="standard", _prepare=wrap_argv)
        cmd = cgroup_scope_argv(cmd)
        proc = await create_subprocess_limited(
            *cmd,
            cwd=str(pkg_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=platform_compat.IS_POSIX,
            creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
            env=minimal_env(),
        )
        # Tree-killing timeout: a bare wait_for would abandon a slow git
        # process still running, letting it race (and overwrite) the manifest
        # restore that follows.
        await _communicate_with_timeout(proc, timeout=15)
        return proc.returncode or 0

    try:
        if pre_pull_commit:
            rc = await _run_git(["git", "reset", "--keep", pre_pull_commit])
            if rc == 0:
                log_lines.append(
                    f"Rolled checkout back to pre-update commit {pre_pull_commit[:12]}"
                )
            else:
                log_lines.append(
                    "WARNING: could not roll the checkout back; "
                    "a retry may keep rejecting until the source is repaired"
                )
    except (asyncio.TimeoutError, OSError, RuntimeError) as exc:
        # RuntimeError covers SandboxUnavailableError from wrap_argv — cleanup
        # is best-effort and must never mask the refusal it follows.
        logger.debug("post-rejection rollback failed for %s: %s", app_name, exc)
    if _contained_join(pkg_dir, manifest_relpath) is None:
        # manifest_relpath (the FULL path, e.g. "sub/app.json") does not
        # resolve inside pkg_dir RIGHT NOW — some callers reach this point
        # after a build step or onInstall script ran with write access to the
        # checkout, so a containment check the caller made earlier cannot be
        # trusted here. Checking only `subdirectory` (the directory, and only
        # when non-empty) misses a symlink planted at the manifest LEAF itself
        # -- `subdirectory/app.json`, or plain `app.json` when there is no
        # subdirectory -- which is exactly what the raw write and the
        # git-checkout fallback below target; either would follow such a
        # symlink and write outside pkg_dir as this unsandboxed process. Skip
        # the manifest restore entirely rather than risk that write; the
        # rollback above already ran and stands. Unconditional (no
        # `if subdirectory` gate): the same leaf-symlink attack works with an
        # empty subdirectory too, where manifest_relpath is just "app.json".
        log_lines.append(
            f"WARNING: {manifest_relpath!r} no longer resolves inside "
            "the checkout; skipping manifest restore to avoid writing through "
            "a symlink escape. A retry may keep rejecting until the source is "
            "repaired."
        )
        return
    try:
        # Restore the manifest regardless — in its OWN guarded block so a
        # reset failure above cannot skip it: a build step or install script
        # rewriting app.json is a WORKING-TREE edit the reset cannot undo
        # (HEAD never moved), and app.json is the poison vector the next
        # prefetch reads.
        if manifest_snapshot is not None:
            await asyncio.to_thread((pkg_dir / manifest_relpath).write_bytes, manifest_snapshot)
            log_lines.append(f"Restored {manifest_relpath} to its exact pre-update contents")
        else:
            rc = await _run_git(["git", "--literal-pathspecs", "checkout", "--", manifest_relpath])
            if rc != 0:
                log_lines.append(
                    f"WARNING: could not restore {manifest_relpath}; "
                    "a retry may keep rejecting until the source is repaired"
                )
    except (asyncio.TimeoutError, OSError, RuntimeError) as exc:
        logger.debug("post-rejection manifest restore failed for %s: %s", app_name, exc)


async def _clone_build_app_locked(
    git_url: str,
    app_name: str,
    log_lines: list[str],
    branch: str = "main",
    *,
    index_originated: bool = False,
    subdirectory: str = "",
    entry_repo: str = "",
    commit: str = "",
    self_managed: bool = False,
    pending_cleanup: list[Path],
    restorable_stale: list[Path] | None = None,
    verb: _InstallVerb = "install",
) -> dict[str, Any]:
    """Inner implementation of _clone_build_app, called under per-app lock.

    *pending_cleanup* and *restorable_stale* are caller-owned mutable lists
    (see :func:`_clone_build_app`): this function fills them so the wrapper's
    single return can stamp the full move-aside state onto EVERY dict result,
    refusals included. *pending_cleanup* is REQUIRED — the sole production
    caller always threads its own list through so the wrapper's single exit
    can read the move-aside state, and every test constructs one too; an
    optional-with-``None`` shape would only invite a caller to drop the list
    and silently lose that state, so there is no default to fall back to.
    *self_managed* is the registry entry's resource ownership, forwarded to the
    desktop gate in :func:`_run_app_build` (see :func:`_clone_build_app`).
    """
    credential_target = git_url
    if _git_target_is_unsupported(credential_target):
        return {
            "ok": False,
            "name": app_name,
            "error": (
                "git clone target contains an unsupported query or fragment or an "
                "ambiguous Git transport identity"
            ),
        }
    git_url = _strip_git_target_userinfo(credential_target)
    if not _looks_like_git_url(git_url):
        return {
            "ok": False,
            "name": app_name,
            "error": (f"{_strip_git_target_userinfo(git_url)!r} is not a cloneable git URL"),
        }

    pkg_dir = app_source_dir(app_name)
    if restorable_stale is None:
        restorable_stale = []
    # Captured BEFORE the clone so a refusal below can tell a checkout this run
    # created (delete: no residue) from a pre-existing app workspace (preserve).
    checkout_preexisted = (pkg_dir / ".git").is_dir()
    # And the pre-pull commit, so an admission rejection can ROLL BACK a
    # pre-existing checkout: the prefetch prefers the local checkout, so a
    # checkout left sitting at a policy-rejected commit would make every retry
    # reject at prefetch before the pull could ever fetch a fixed remote.
    pre_pull_commit = (
        await asyncio.to_thread(_resolved_clone_commit, pkg_dir) if checkout_preexisted else ""
    )
    # And the manifest's exact pre-update WORKING-TREE bytes (which may carry
    # the user's uncommitted local edits): a rejection restores THIS snapshot,
    # so cleanup undoes whatever the pull/build/script did to app.json without
    # discarding the user's own edits the way a checkout-from-HEAD would.
    manifest_rel = f"{subdirectory}/app.json" if subdirectory else "app.json"
    pre_update_manifest: bytes | None = None
    if checkout_preexisted:
        try:
            pre_update_manifest = await asyncio.to_thread((pkg_dir / manifest_rel).read_bytes)
        except OSError:
            pre_update_manifest = None
    clone_err = await _git_clone_or_pull(
        git_url,
        branch,
        pkg_dir,
        log_lines,
        credential_target=credential_target,
        index_originated=index_originated,
        pending_cleanup=pending_cleanup,
        restorable_stale=restorable_stale,
        commit=commit,
    )
    if clone_err is not None:
        return clone_err
    if pending_cleanup:
        # The origin-mismatch gate moved the old checkout aside and FRESH-CLONED
        # into pkg_dir: whatever pre-existed is now a .stale-* sibling, and the
        # directory at pkg_dir was created THIS RUN. The pre-clone snapshot
        # above describes the moved-aside (different-origin) history — using it
        # would make a later rejection try to reset the new clone to a commit
        # from another repository, or preserve a squatting clone as if it were
        # the user's workspace. Cleanup state must describe the ACTIVE checkout;
        # the moved-aside path is kept so a rejection can put the previous
        # checkout BACK instead of leaving the slot empty and the old workspace
        # stranded as a sweeper-doomed .stale-* sibling.
        checkout_preexisted = False
        pre_pull_commit = ""
        pre_update_manifest = None

    # IDENTITY GATE — before the build, so a repo whose app.json declares a
    # different name never gets to run npm/pip lifecycle scripts. Fail-closed:
    # a missing or unparseable app.json (or name) is a mismatch, not a pass.
    app_source = pkg_dir
    if subdirectory:
        contained = _contained_join(pkg_dir, subdirectory)
        if contained is None:
            return {
                "ok": False,
                "name": app_name,
                "error": f"unsafe subdirectory {subdirectory!r} escapes the app source root",
            }
        app_source = contained
    cloned_manifest: dict[str, Any] | None = None
    try:
        parsed = json.loads(await asyncio.to_thread((app_source / "app.json").read_text, "utf-8"))
        if isinstance(parsed, dict):
            cloned_manifest = parsed
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        logger.debug("cloned app.json for %s is unreadable pre-build: %s", app_name, exc)
    cloned_name = str((cloned_manifest or {}).get("name", "") or "")
    if cloned_manifest is None or cloned_name != app_name:
        return await _refuse_identity_mismatch(
            app_name,
            cloned_name,
            entry_repo or git_url,
            pkg_dir,
            log_lines,
            created_this_run=not checkout_preexisted,
            pre_pull_commit=pre_pull_commit,
            manifest_relpath=manifest_rel,
            manifest_snapshot=pre_update_manifest,
            restore_from=_restorable_or_none(pending_cleanup, restorable_stale),
            verb=verb,
        )

    # ADMISSION GATE, second pass — on the CLONED manifest. The first pass ran
    # on the pre-clone prefetch, but the repository can advance between the two
    # reads: a signed preview can resolve to an unsigned (or newly banned)
    # manifest at clone time, and under a require-signature policy that content
    # must not build or install. Same fail-closed policy call, different
    # artifact.
    # The typed view, built ONCE: the admission gate below and the build's
    # desktop gate must judge the same normalized manifest the runtime later
    # loads from (`manager.py` hands the hook loaders
    # `AppManifest.from_json_file(...).to_dict()`), so a second, differently
    # normalized reading of these bytes is exactly the disagreement to avoid.
    cloned_app_manifest = AppManifest.from_dict(cloned_manifest)
    denied = app_admission_denied(
        app_name,
        manifest=cloned_app_manifest,
        action="install_from_registry",
    )
    if denied:
        log_lines.append(_refusal_line(verb, f"blocked by admission policy: {denied}"))
        try:
            sel().log_api_access(
                caller="app_install_from_registry",
                operation="admission_cloned",
                outcome="rejected",
                resources=f"name={app_name!r}",
                error=denied,
            )
        except Exception as exc:  # an audit failure must never mask the refusal
            logger.debug("SEL audit failed for %s cloned admission: %s", app_name, exc)
        # Un-poison the checkout so the rejection is retryable (see helper).
        await _unpoison_rejected_checkout(
            app_name,
            pkg_dir,
            log_lines,
            checkout_preexisted=checkout_preexisted,
            pre_pull_commit=pre_pull_commit,
            manifest_relpath=manifest_rel,
            manifest_snapshot=pre_update_manifest,
            restore_from=_restorable_or_none(pending_cleanup, restorable_stale),
        )
        return {
            "ok": False,
            "name": app_name,
            "error": f"blocked by admission policy: {denied}",
        }

    # Build in the directory that actually HOLDS the package, not the clone root.
    #
    # A monorepo registry entry declares `subdirectory`, and joining it only AFTER
    # this build ran would leave `_run_app_build` looking for
    # pyproject.toml/package.json at the clone root, finding none, logging "No build
    # step detected — using source as-is", and returning ok=True having installed
    # nothing — the app's own pyproject.toml never seen. A silent success is the
    # worst shape for this: `setup.onInstall` does get `cwd=app_source`, so an app
    # could paper over it with a script, which is how such a break stays hidden.
    #
    # `app_source` is already the containment-checked join of `subdirectory`
    # under the clone root (the identity gate above fails closed on an escaping
    # value), so it is safe to run the build command there.
    #
    # `cloned_app_manifest` is the manifest the identity and admission gates just
    # judged — passed rather than re-read so the build decides from the bytes
    # those gates accepted, normalized once, the way the runtime's loaders see it.
    #
    # The build step stays in the facade, where the internal-Python isolation
    # guard reads it by path, so it is resolved there at call time.
    result = await _facade()._run_app_build(
        app_source,
        app_name,
        log_lines,
        manifest=cloned_app_manifest,
        self_managed=self_managed,
        verb=verb,
    )
    if result["ok"]:
        result["pkg_dir"] = pkg_dir
        # Surface the pre-clone checkout state so the caller's LATER gates
        # (post-build / post-script admission) can un-poison the checkout with
        # the same delete-fresh / roll-back-pre-existing semantics this
        # function applies at the cloned-admission gate above.
        result["_checkout_preexisted"] = checkout_preexisted
        result["_pre_pull_commit"] = pre_pull_commit
        result["_pre_update_manifest"] = pre_update_manifest
        # Do NOT delete moved-aside checkouts — even after a successful
        # install transaction the user may want to recover local edits from
        # the old checkout. The paths are surfaced to the caller by
        # `_clone_build_app`'s single-exit stamp (every dict result carries
        # `_pending_stale_cleanup`), so no explicit stamping is needed here.
        # The dirs are harmless siblings swept by _sweep_stale_checkouts()
        # after _STALE_CHECKOUT_RETENTION_DAYS (best-effort, runs at the
        # start of the next install_from_registry call).
        pass
    else:
        # Build failed — restore the old checkout so the user's local edits
        # survive. Remove the (successfully cloned but unbuildable) new dest
        # and rename the moved-aside dir back.
        #
        # But ONLY for RESTORABLE move-asides. `pending_cleanup` carries every
        # move-aside this run made — both same-origin/branch-drift asides
        # (restorable: restoring them is the point) AND origin-mismatch asides
        # the identity gate deliberately refused to serve. Restoring the latter
        # would re-seat a repository the gate just rejected into the active
        # source slot the instant its replacement's build fails — the exact
        # confused-deputy residue the restorable/pending split exists to close.
        # Membership is tested against `restorable_stale`, the caller-owned list
        # populated at the same move-aside site (identity `in`, comparing the
        # Path objects both lists share — never a re-derived string that path
        # aliasing could spoof). A non-restorable aside stays in
        # `pending_cleanup` untouched so the single-exit stamp carries it and
        # the finally-owned `_report_retained_stale_checkouts` names it.
        # `restorable_stale or []`: a missing list means NOTHING is restorable,
        # so every move-aside is retained rather than restored — the fail-closed
        # default (the production caller always threads a real list; this only
        # guards a caller that omits it from re-seating a checkout by accident).
        restorable_set = set(restorable_stale or [])
        restored_paths: list[Path] = []
        for stale_path in pending_cleanup:
            if stale_path not in restorable_set:
                # Refused-origin checkout: never restored into the active slot.
                # Left in pending_cleanup so it is reported retained, not swept
                # silently and not re-seated as the live app source.
                log_lines.append(
                    "Build failed; origin-mismatched checkout NOT restored, "
                    f"retained at: {stale_path}"
                )
                continue
            if stale_path.exists():
                await asyncio.to_thread(shutil.rmtree, pkg_dir, True)
                try:
                    await asyncio.to_thread(stale_path.rename, pkg_dir)
                    log_lines.append(
                        "Build failed; previous checkout restored from " f"{stale_path.name}"
                    )
                    restored_paths.append(stale_path)
                except OSError as exc:
                    log_lines.append(
                        f"Build failed; could not restore previous checkout "
                        f"from {stale_path}: {exc}. Recover your files from "
                        f"{stale_path}"
                    )
        # Drop the checkouts actually put back from the caller-owned pending
        # list: a restored checkout is not a retained `.stale-*` sibling,
        # so `_clone_build_app`'s single-exit stamp must not carry it and the
        # caller's `_report_retained_stale_checkouts` must not name it. A rename
        # that FAILED above stays in the list so it is still reported stranded.
        for restored in restored_paths:
            pending_cleanup.remove(restored)
    return result


def _provisioning_declared(manifest: AppManifest, *, self_managed: bool) -> bool:
    """The half of :func:`_requirements_owned_by_the_runtime` that reads the
    manifest and the registry entry alone: False for a self-managed entry
    (nothing of ours provisions or spawns it) and for an app declaring
    ``backend.hooks`` (imported INTO the gateway process, which the deps tree never
    reaches). Spelled once, here, and asked by :func:`_desktop_build_refusal`
    BEFORE it copies the tree: a refusal these two decide needs no preview.
    """
    if self_managed:
        return False
    # `hooks.to_dict()` omits every blank field, so an empty dict IS "declares no
    # in-gateway hook", in the same emission the loaders are fed.
    return not manifest.backend.hooks.to_dict()


def _requirements_owned_by_the_runtime(
    manifest: AppManifest,
    installed: Path,
    *,
    source: Path,
    self_managed: bool,
    final: bool,
) -> bool:
    """True when the runtime provisions this app's root ``requirements.txt`` out
    of process and nothing of the app's Python imports into the gateway.

    This is the RUNTIME'S OWN condition, not an approximation of it: the
    out-of-process half is :func:`runtime_provisions_requirements`, the predicate
    both provisioners themselves call (``apps/backend.py`` at the spawn of a
    FILE-style ``backend.entryPoint``, ``apps/bridges.py`` at the registration of
    a stdio ``mcpServers`` entry), so a shape they refuse -- a module-style,
    dotted entry point, which executes trusted package code and never has an
    app-dir requirements file installed for it -- fails here too instead of
    passing an install whose dependencies land nowhere.

    The in-process half is this gate's own: a declared ``backend.hooks`` field is
    imported INTO the gateway process (``lifecycle.py``, ``route_registry.py``),
    which the deps tree never reaches, so an app declaring one has Python that
    must import from the gateway's interpreter after all -- waiving for it would
    install the app "successfully" with its hook imports broken and its routes
    degraded, the silent-broken install the loud refusal exists to prevent.

    *self_managed* is the registry entry's resource ownership (``resources:
    "app"``), which no manifest field carries and which decides whether either
    provisioner ever RUNS for this app: ``install_from_registry`` registers a
    self-managed app from its manifest alone -- no source is copied into the app
    directory, ``bridges.py`` skips every registration for it, and the app
    launches itself -- so the runtime never sees its ``requirements.txt``. On a
    source install the build step's ``pip install -r`` was the only thing that
    installed that file, and that step is exactly what the bundled interpreter
    cannot run: waiving for a self-managed app would report a successful install
    whose dependencies landed nowhere, so it keeps the refusal regardless of what
    its manifest declares. Required rather than defaulted, for the same reason
    *manifest* is on :func:`_run_app_build`: the permissive value must be stated
    by a caller that knows the entry, never inherited.

    *final* says which pass is asking. The union is the provisioners' exact
    condition, and the backend half requires the entry FILE to exist -- but the
    build pass runs before ``setup.onInstall``, whose window is exactly where an
    app may generate that file, so with ``final=False`` a declared Python-script
    entry that is merely absent still counts (nothing is pip-installed either
    way); with ``final=True``, on the post-script checkout, the union is applied
    as is and an entry that never appeared is refused there.

    Answers from the TYPED manifest, which is what decides what actually loads:
    ``manager.py`` hands the loaders ``AppManifest.from_json_file(...).to_dict()``,
    so a declaration ``BackendConfig.from_dict`` normalizes away is a hook the
    loaders never see either, and ``bridges.py`` reads ``manifest.mcpServers`` from
    the same typed object. *installed* is the tree the entry-point shape is
    judged against -- the copy ``install_app`` would produce from *source*, the
    checkout, which the spawn later resolves the same name under; *source* is
    read by the BUILD pass alone, to tell a declared entry file that is merely
    absent from the checkout (the script's window may still create it) from a
    layout no script window lifts.
    """
    if not _provisioning_declared(manifest, self_managed=self_managed):
        return False
    # Asked of the INSTALLED tree, not the checkout: *installed* is what
    # `install_app`'s own copy produced from it (see `_installed_tree_preview`),
    # so an entry the copy does not carry into the app directory -- under a
    # dropped build-input dir, under `data/` (which an update replaces with the
    # preserved previous data), a link escaping the root, a link whose text
    # stops resolving once the tree stands elsewhere -- is missing HERE exactly
    # as it is missing where the spawn looks, and the predicate answers as the
    # spawn would: no file-style entry, so only a stdio server, which
    # `bridges.py` provisions for on its own, can be the reason to waive.
    if runtime_provisions_requirements(manifest, installed):
        return True
    if final:
        return False
    # The BUILD pass runs before `setup.onInstall`, whose documented window is
    # exactly where an app may generate its entry file. A declared PYTHON entry
    # that is merely ABSENT from the checkout -- nothing at the path, or a link
    # whose target is not there yet -- is therefore provisionable for now:
    # nothing is pip-installed either way, and the final pass -- on the
    # post-script checkout, with `final=True` -- refuses if the file never
    # appeared. A declared shell or node entry is not: the suffix is the
    # manifest's, no script window changes it, and the deps tree never reaches
    # that child (`spawn_launches_entry_point_as_python`). Judged on the SOURCE:
    # anything standing at the path there (a directory, a link escaping the
    # root, a file under a name the copy drops) is a refusal no script window
    # lifts, and stands here.
    entry_point = manifest.backend.entryPoint
    if (
        not entry_point
        or is_module_style_entry_point(entry_point, source)
        or not spawn_launches_entry_point_as_python(manifest, source)
    ):
        return False
    entry = source / entry_point
    return _absent(entry) or (os.path.islink(entry) and not entry.exists())


def _absent(path: Path) -> bool:
    """True when nothing stands at *path* (``ENOENT``) -- the one shape a script's
    window can fill. Any other failure to stat it (a symlink budget exceeded, a
    file where a directory should be) is a layout no script fixes, so it is not
    "absent" and the build pass refuses it as the spawn would."""
    try:
        os.lstat(path)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return False


#: Machine code carried beside the desktop refusal on the install result. The
#: dashboard keys its plain-language copy on it: the condition is permanent for
#: this gateway (retrying cannot change what the bundled interpreter can install),
#: so the consent modal drops its retry instruction for exactly this code.
DESKTOP_BUILD_STEP_UNSUPPORTED = "desktop_build_step_unsupported"

_DESKTOP_BUILD_REFUSAL = (
    "Python apps that require a build step are not supported in Kiro Crew's desktop "
    "build: its bundled interpreter is inside the signed application bundle and cannot "
    "install packages"
)


def _desktop_build_refusal(
    build_dir: Path, manifest: AppManifest, *, self_managed: bool, final: bool
) -> str:
    """The desktop app's build-step refusal for this checkout, or ``""`` when it may install.

    The ONE owner of that verdict. :func:`_run_app_build` asks it before planning
    any Python build command (``final=False``), and :func:`install_from_registry`
    asks it again on the FINAL checkout (``final=True``) -- after the build and
    ``setup.onInstall`` have run with write access -- so the waiver the build
    granted is re-derived from what actually registers rather than trusted
    across a script that can add ``backend.hooks`` or drop the out-of-process
    consumer it was granted for. The two passes differ in one respect only:
    ABSENCE. The script's window is where an app may generate its entry file or
    the target of its ``requirements.txt`` link, so the build pass lets a
    declared entry that is merely missing, or a link that is merely dangling,
    through (nothing is pip-installed either way) and the final pass refuses
    them if they never appeared; every other refusal is the same in both.
    Both callers are coroutines and run it through ``asyncio.to_thread``: the
    layout probes below are filesystem stats and one small copy, and
    ``KIROCREW_HOME`` can sit on a network mount that stalls, which on the event
    loop would freeze the gateway and its liveness heartbeat with it.

    Judges the same layout the build detects, in the same order: nothing on a
    gateway that is not the bundled interpreter; nothing when ``package.json``
    selects the JavaScript build (the Python files are not examined then); the
    refusal for ``pyproject.toml`` / ``setup.py``, which install INTO this
    interpreter; then, for a ``requirements.txt`` that is present, the refusal
    for one the runtime does not provision (:func:`_requirements_owned_by_the_runtime`,
    which *self_managed* -- the registry entry's resource ownership -- feeds) or
    will not READ (:func:`~kiro_crew.apps.manifest.requirements_in_tree`, the
    provisioner's own acceptance rule: a link escaping the app root, a dangling
    one, a non-file, an oversized one); ``""`` for one it does, and for a
    checkout with no Python build files at all.

    Those two checks are asked of the tree the install PRODUCES, not of this
    checkout: :func:`_installed_tree_preview` runs ``install_app``'s own copy
    (:func:`~kiro_crew.apps.manager.copy_app_tree_as_installed`) into a temporary
    directory and the predicates read that. The provisioner and the spawn read
    the APP DIRECTORY, and the copy drops build-input names at any depth, omits a
    link that escapes the root, keeps an in-tree link as a link with its text as
    written, rewrites an absolute in-tree link, and -- on an update -- meets
    ``data/`` as the preserved previous directory; a layout that resolves in the
    checkout and not after all of that (a link into ``node_modules``, an entry
    under ``data/``, a link whose text climbs above the root and re-enters by
    naming the checkout) is missing in the preview exactly as it would be missing
    there, and the runtime's own predicates refuse it with no rule of this
    function's predicting what the copy does. A preview the copy itself cannot
    produce is an ordinary install error, retryable and without this code: the
    install would fail on the same copy, and the app's author has nothing to fix.
    A preview the copy produces and the INSTALL would refuse -- a root ``data``
    that is not a directory, which ``install_app`` turns away before writing its
    record -- raises that refusal (:class:`~kiro_crew.apps.manager.InstalledTreeRefused`)
    through this gate unchanged, so the transaction refuses before the copy into
    the app directory is ever made, and without this code: the layout is the
    author's to fix on every host.

    Presence is ``lexists``, the provisioner's own notion: a dangling
    ``requirements.txt`` link is "present but not a readable regular file" to
    ``provision_app_deps``, which records a provisioning FAILURE and lets the
    backend spawn without its deps (the failure written at the top of its log,
    the import error following) -- so with a consumer declared it is refused
    here rather than waived into an install whose backend cannot run. With NO
    consumer nothing of ours would
    ever read it, and the build's own detection (``is_file``, on every host)
    sees no file: it passes, as it does on a source install.
    """
    if not platform_compat.is_bundled_interpreter():
        return ""
    if (build_dir / "package.json").is_file():
        return ""
    if (build_dir / "pyproject.toml").is_file() or (build_dir / "setup.py").is_file():
        return _DESKTOP_BUILD_REFUSAL
    requirements = build_dir / "requirements.txt"
    if not os.path.lexists(requirements):
        return ""
    if not _provisioning_declared(manifest, self_managed=self_managed):
        # Decided by the manifest and the registry entry alone -- nothing here
        # reads the tree, so the preview copy is not produced for it. A readable
        # requirements.txt with nothing of ours to provision it is the
        # silent-broken install; a dangling link with no consumer is what the
        # build's detection already calls "no file".
        return _DESKTOP_BUILD_REFUSAL if requirements.is_file() else ""
    with _installed_tree_preview(build_dir, manifest.name) as installed:
        if not _requirements_owned_by_the_runtime(
            manifest, installed, source=build_dir, self_managed=self_managed, final=final
        ):
            # The tree-reading half of the same union: no consumer the copy carries.
            return _DESKTOP_BUILD_REFUSAL if requirements.is_file() else ""
        if requirements_in_tree(installed, installed / "requirements.txt") is None:
            if not final and os.path.islink(requirements) and not requirements.exists():
                # Dangling NOW, in the checkout, before `setup.onInstall` has had
                # its window to create the target; the final pass judges the
                # post-script checkout and refuses if it is still dangling there.
                return ""
            return _DESKTOP_BUILD_REFUSAL
    return ""


@contextmanager
def _installed_tree_preview(source: Path, name: str) -> Iterator[Path]:
    """The tree an install of *source* would leave for the runtime, produced by
    the install's own copy into a temporary directory and removed on exit.

    :func:`~kiro_crew.apps.manager.copy_app_tree_as_installed` is the same call
    ``install_app`` makes plus the gateway's own post-copy part -- told, as the
    install will find out, whether a preserved ``data/`` awaits
    (:func:`~kiro_crew.apps.manager.preserved_data_awaits`) -- so the verdict
    derived from this tree cannot drift from the copy: the copy IS the model.

    WHERE the copy lands is part of the model too. The runtime resolves the
    installed tree's links from ``app_dir(name)`` -- ``<data home>/apps/<name>``
    -- and a relative link text that climbs out of the tree and re-enters it by
    NAME resolves by the names actually around it: ``requirements.txt ->
    ../app/requirements/prod.txt`` is in-tree in a checkout whose leaf directory
    is ``app`` (a registry ``subdirectory`` of that name) and dangles, or lands in
    another app's directory, once installed under ``apps/<name>``. So the preview
    is written under the destination's own leaf name, *name* -- the manifest's
    validated app name, the single directory component ``app_dir`` joins -- and a
    text that re-enters by that name resolves here exactly as it will there. The
    leaf is what decides: a text that climbs further re-enters by the names ABOVE
    the leaf, and the copy judges those texts where it runs, in the checkout --
    one that names the checkout's own ancestors resolves outside the root from
    the app directory and from here alike, and one that names anything else
    escapes the checkout and is omitted by the copy before either place sees it.

    The holder is a ``<name>.partial-<8 hex>`` sibling of the checkout under
    app-sources: the filesystem the install writes to (the one the copy's
    case-folding probe must answer for), and the one name the retention sweep
    already retires, so a copy an unclean gateway exit leaves behind mid-install
    goes the way of any other abandoned partial tree after
    :data:`_STALE_CHECKOUT_RETENTION_DAYS` days instead of accumulating.

    A copy that fails (disk full, a permission) raises an ordinary error carrying
    the cause -- the transaction reports it as any other install failure, without
    the permanent ``desktop_build_step_unsupported`` code, because a retry may
    well succeed and the app's author has nothing to fix. A copy that SUCCEEDS
    into a tree the install refuses -- a root ``data`` that is not a directory,
    which ``install_app`` turns away before writing its record -- raises the
    install's own :class:`~kiro_crew.apps.manager.InstalledTreeRefused` through
    here unchanged: the author's to fix, permanent, and reported by the
    transaction as the refusal it is (not this gate's desktop code, which would
    tell the reader a browser install could succeed). Blocking filesystem
    work, for a worker thread: app trees are small (the copy drops ``.git``,
    ``node_modules``, ``.venv`` and the other build-input names), and the caller
    already runs off-loop.
    """
    holder = _app_sources_dir() / f"{name}.partial-{uuid.uuid4().hex[:8]}"
    try:
        holder.mkdir(parents=True)
        preview = holder / name
        copy_app_tree_as_installed(source, preview, data_preserved=preserved_data_awaits(name))
    except OSError as exc:
        shutil.rmtree(holder, ignore_errors=True)
        raise RuntimeError(
            f"could not preview the installed tree of {source.name} for the desktop gate: {exc}"
        ) from exc
    except BaseException:
        shutil.rmtree(holder, ignore_errors=True)
        raise
    try:
        yield preview
    finally:
        shutil.rmtree(holder, ignore_errors=True)


def _desktop_gate_probe(
    build_dir: Path, manifest: AppManifest, self_managed: bool
) -> tuple[str, bool]:
    """The build-time desktop verdict plus whether a readable ``requirements.txt``
    is present -- one worker-thread call, so neither stat runs on the event loop.

    The second value only selects the install-log line the build writes when the
    verdict is ``""``: a present requirements.txt is the runtime's to provision,
    an absent one (or an entry nothing reads) is "no build step".
    """
    refusal = _desktop_build_refusal(build_dir, manifest, self_managed=self_managed, final=False)
    return refusal, (build_dir / "requirements.txt").is_file()


#: The files whose presence decides the desktop gate's verdict and the build's
#: detection, in the order both read them.
_DESKTOP_LAYOUT_FILES = ("package.json", "pyproject.toml", "setup.py", "requirements.txt")


def _desktop_layout_present(app_source: Path) -> frozenset[str]:
    """The desktop gate's layout inputs present under *app_source* right now.

    Presence is ``lexists``: a dangling link is a present entry to the gate and
    to the provisioner alike. Taken before ``setup.onInstall`` runs, so a
    refusal after it can tell what the script created from what was there.
    """
    return frozenset(name for name in _DESKTOP_LAYOUT_FILES if os.path.lexists(app_source / name))


def _layout_cleanup_escaped(app_source: Path, clone_root: Path) -> str:
    """Why the post-script layout cleanup must NOT write under *app_source* right
    now, or ``""`` when it may.

    ``setup.onInstall`` ran with write access to the checkout before the
    cleanup, so the containment the caller established when it computed
    *app_source* (``clone_root / subdirectory``) cannot be trusted at the write:
    the script can replace the subdirectory -- or the checkout root itself --
    with a link to a directory this unsandboxed process can write to, and an
    ``unlink``/``rename`` of ``<app_source>/setup.py`` would then reach an
    operator's file. The same TOCTOU :func:`_unpoison_rejected_checkout`
    re-checks before its manifest write, in the same shape: resolve both ends
    NOW and require *app_source* to still resolve inside *clone_root*, and
    *clone_root* to still be a real directory (a root swapped for a link would
    make the resolved pair agree while both point elsewhere). The sentence is the
    log line the caller appends when it skips.
    """
    if platform_compat.is_link_or_junction(clone_root):
        return (
            f"WARNING: the checkout {clone_root.name!r} is now a link; skipping the layout "
            "cleanup to avoid writing through a symlink escape. A retry may keep refusing "
            "until the source is repaired."
        )
    try:
        root = clone_root.resolve()
        target = app_source.resolve()
    except (OSError, RuntimeError) as exc:
        # Exactly what ``Path.resolve`` raises: ``OSError`` for a path it cannot
        # walk, and ``RuntimeError`` for a symlink LOOP -- non-strict resolution
        # re-raises ELOOP that way -- which a script can plant as easily as an
        # escape (``rm -rf pkg && ln -s pkg pkg``). Either way the cleanup skips
        # and the rollback that follows still runs; an escaping exception here
        # would leave the poisoned checkout in the live slot for every retry.
        return (
            f"WARNING: the checkout no longer resolves ({exc}); skipping the layout "
            "cleanup rather than write through a symlink escape. A retry may keep "
            "refusing until the source is repaired."
        )
    if not target.is_relative_to(root):
        return (
            f"WARNING: {app_source.name!r} no longer resolves inside the checkout; "
            "skipping the layout cleanup to avoid writing through a symlink escape. "
            "A retry may keep refusing until the source is repaired."
        )
    return ""


def _remove_new_layout_files(
    app_source: Path, present_before: frozenset[str], *, clone_root: Path
) -> list[str]:
    """Remove the layout files that appeared during ``setup.onInstall`` after the
    final desktop refusal. Returns the log lines to append.

    Companion to :func:`_unpoison_rejected_checkout`, and run BEFORE it. On a
    pre-existing checkout, ``git reset --keep`` restores tracked files, but a
    file that APPEARED during the script's window is untracked and survives it,
    and a surviving ``pyproject.toml`` / ``setup.py`` makes every retry refuse at
    the build gate -- before the script a fixed remote would have corrected ever
    runs again. That retry poisoning is the harm this removal cures. Only the
    entries in :data:`_DESKTOP_LAYOUT_FILES` that were absent in *present_before*
    and are present now are touched, by fixed leaf name under the
    containment-checked *app_source*, with ``unlink`` -- a link goes as a link,
    its target untouched, and a directory so named is refused by ``unlink`` and
    reported rather than removed.

    Removed, not moved: this is the FRESH-clone half of the post-script rollback
    (:func:`_roll_back_post_script_refusal`), whose next step deletes the whole
    clone -- nothing a person wrote can be in a checkout created by this run and
    torn down by it, so setting its files aside would only strand them. A
    pre-existing checkout takes :func:`_set_aside_new_layout_files` instead. The
    order above keeps the gateway from touching a tracked file the rollback puts
    back. The install log names each file removed. Best-effort, like the rollback
    it precedes: a failure is reported, and the refusal stands regardless.

    Runs in a worker thread, so it RETURNS its log lines instead of appending
    them: the caller's ``log_lines`` may be a :class:`StreamingLogLines`, whose
    ``append`` feeds a loop-owned ``asyncio.Queue`` and must only ever be called
    on the event-loop thread.

    *clone_root* is the checkout the containment is re-verified against AT THE
    WRITE (:func:`_layout_cleanup_escaped`): the script ran with write access
    and can have swapped a directory component for a link, so nothing is
    unlinked unless *app_source* still resolves inside it.
    """
    escaped = _layout_cleanup_escaped(app_source, clone_root)
    if escaped:
        return [escaped]
    messages: list[str] = []
    for name in _DESKTOP_LAYOUT_FILES:
        if name in present_before or not os.path.lexists(app_source / name):
            continue
        try:
            os.unlink(app_source / name)
        except OSError as exc:
            messages.append(
                f"WARNING: could not remove {name}, which appeared during the install "
                f"script: {exc}; a retry may keep refusing until the source is repaired"
            )
        else:
            messages.append(f"Removed {name}, which appeared during the install script")
    return messages


def _set_aside_new_layout_files(
    app_source: Path, present_before: frozenset[str], *, clone_root: Path
) -> list[str]:
    """Set aside the layout files that appeared during ``setup.onInstall`` after a
    post-script refusal of a PRE-EXISTING checkout. Returns the log lines to append.

    Same window and same entries as :func:`_remove_new_layout_files` -- the
    :data:`_DESKTOP_LAYOUT_FILES` absent in *present_before* and present now,
    judged by fixed leaf name under the containment-checked *app_source* -- and
    the same cure for the same harm: left in place, an untracked ``pyproject.toml``
    or ``setup.py`` survives ``git reset --keep`` and makes every retry refuse at
    the build gate. Set aside, not removed, because the pre-existing checkout is
    the one place a person's uncommitted work is otherwise preserved (the
    rollback is ``git reset --keep`` for exactly that reason), so a file written
    there by hand inside the script's window is not the gateway's to destroy.

    Where: :func:`_stale_sibling` of the checkout -- ``<clone_root>.stale-<8
    hex>/<name>`` under ``app-sources``, the same move-aside idiom and the same
    ``.stale-*`` name :func:`_move_checkout_aside` gives a whole checkout, which
    the retention sweep already retires after
    :data:`_STALE_CHECKOUT_RETENTION_DAYS` days
    (:func:`_sweep_stale_checkouts_sync`) -- no new artifact class and no new
    sweep. One sibling per refusal, created only when there is something to set
    aside; it is created fresh, so its retention clock starts at the refusal. A
    link moves as a link, its target untouched; a directory so named is
    reported and left, as the removal leaves it. The install log names each file
    and where it went. Best-effort, like the rollback it precedes: a failure is
    reported, and the refusal stands regardless.

    Runs in a worker thread, so it RETURNS its log lines instead of appending
    them, for the reason :func:`_remove_new_layout_files` gives -- and, like it,
    re-verifies at the write that *app_source* still resolves inside
    *clone_root* (:func:`_layout_cleanup_escaped`) before it renames anything.
    """
    escaped = _layout_cleanup_escaped(app_source, clone_root)
    if escaped:
        return [escaped]
    messages: list[str] = []
    aside: Path | None = None
    for name in _DESKTOP_LAYOUT_FILES:
        entry = app_source / name
        if name in present_before or not os.path.lexists(entry):
            continue
        if os.path.isdir(entry) and not platform_compat.is_link_or_junction(entry):
            messages.append(
                f"WARNING: could not set aside {name}, which appeared during the install "
                f"script: it is a directory; a retry may keep refusing until the source is repaired"
            )
            continue
        try:
            if aside is None:
                # Bound only once it exists: a holder whose mkdir the parent
                # refused must not count as "the holder", or every later file
                # would skip the mkdir and report the rename's missing-directory
                # error in place of the parent's refusal.
                holder = _stale_sibling(clone_root)
                holder.mkdir()
                aside = holder
            os.rename(entry, aside / name)
        except OSError as exc:
            messages.append(
                f"WARNING: could not set aside {name}, which appeared during the install "
                f"script: {exc}; a retry may keep refusing until the source is repaired"
            )
        else:
            messages.append(
                f"Set aside {name}, which appeared during the install script, at "
                f"{aside.name}/{name}; the retention sweep removes it after "
                f"{_STALE_CHECKOUT_RETENTION_DAYS} days"
            )
    return messages


async def _roll_back_post_script_refusal(
    name: str,
    app_source: Path,
    clone_root: Path,
    log_lines: list[str],
    *,
    layout_before: frozenset[str],
    checkout_preexisted: bool,
    pre_pull_commit: str,
    manifest_relpath: str,
    manifest_snapshot: bytes | None,
    restore_from: Path | None,
) -> None:
    """Undo what ``setup.onInstall`` left in the checkout when a gate AFTER it refuses.

    The one call every post-script refusal makes -- the identity re-check, the
    admission re-check and the final desktop pass alike -- because the harm is the
    script's, not the gate's: the script ran with write access, and whichever gate
    then refuses, a layout input it created (``pyproject.toml``, ``setup.py``,
    ...) is untracked and would survive the rollback to make every retry refuse
    at the BUILD gate, before a fixed remote's script ever runs again. So the
    appeared layout files go FIRST (:func:`_remove_new_layout_files`, judged
    against *layout_before*, the snapshot taken before the script), then the
    checkout is un-poisoned (:func:`_unpoison_rejected_checkout`: a fresh clone
    deleted whole, a pre-existing one rolled back). The order matters: ``git
    reset --keep`` can RESTORE a tracked layout file the pull had removed --
    absent in the snapshot, present after the reset -- and a scan run after it
    would mistake that restored file for one that appeared.

    The removal runs in a worker thread and RETURNS its lines: *log_lines* may be
    a :class:`StreamingLogLines` feeding a loop-owned queue, appended to here, on
    the loop thread, never from the worker. *app_source* is the containment-checked
    package directory the layout is judged under; *clone_root* is the checkout the
    rollback acts on.

    A PRE-EXISTING checkout has its appeared files set aside
    (:func:`_set_aside_new_layout_files`: a ``.stale-*`` sibling the retention
    sweep retires), because that checkout is where a person's uncommitted work is
    otherwise preserved; a fresh clone is deleted whole by the rollback that
    follows, so its appeared files are removed with it as before.
    """
    if checkout_preexisted:
        log_lines.extend(
            await asyncio.to_thread(
                _set_aside_new_layout_files, app_source, layout_before, clone_root=clone_root
            )
        )
    else:
        log_lines.extend(
            await asyncio.to_thread(
                _remove_new_layout_files, app_source, layout_before, clone_root=clone_root
            )
        )
    await _unpoison_rejected_checkout(
        name,
        clone_root,
        log_lines,
        checkout_preexisted=checkout_preexisted,
        pre_pull_commit=pre_pull_commit,
        manifest_relpath=manifest_relpath,
        manifest_snapshot=manifest_snapshot,
        restore_from=restore_from,
    )


def _report_retained_stale_checkouts(
    build_result: dict[str, Any] | None,
    log_lines: list[str],
    *,
    filter_restorable: bool,
) -> None:
    """Log a "Previous checkout retained at" line for each moved-aside
    checkout that will actually stay retained after this call.

    CONTRACT: this is the ONLY owner of the "Previous checkout retained at"
    wording — no exit re-implements the string. It has exactly TWO call sites,
    and neither is a per-exit copy of the other:

    - the ``finally`` of :func:`install_from_registry`, AFTER that ``finally``
      has run its restore block. This is the ordinary path: it reaches EVERY
      normal exit (success and refusal alike) via the single ``finally``,
      passing the returned ``build_result`` and ``filter_restorable=not
      durable_success`` so the flag is derived once, never hand-mirrored.
    - the exception handler in :func:`_clone_build_app` (the ``except`` that
      re-raises a build error). That path produces NO result dict — the
      exception propagates instead of returning — so the ``finally`` above
      never sees the move-aside state. This second call synthesises a minimal
      dict from that scope's ``pending_cleanup``/``restorable_stale`` and
      passes ``filter_restorable=True`` (the handler restored the same-origin
      subset just above it), so a non-restorable ``.stale-*`` on the
      exception path is still named instead of being silently swept.

    Both routes funnel the wording through here precisely so they can never
    drift: hand-replicating the reporter across every exit, with a
    ``filter_restorable`` flag manually mirrored to ``durable_success`` at each
    one, is the scattered-per-exit stranding class the caller's move-aside
    bookkeeping exists to avoid — a new exit could forget the call or pass the
    wrong flag and silently strand or double-report a checkout. Every normal
    exit reaches the single ``finally`` call and derives the flag once; the only
    other caller is the exception path that no ``finally`` return can cover.

    ``_pending_stale_cleanup`` collects every move-aside regardless of
    reason, but ``_restorable_stale`` (a subset) is put back by the
    enclosing ``finally`` — and ONLY when the exit leaves ``durable_success``
    False. The single call passes ``filter_restorable=not durable_success``,
    exactly the restore condition, so the flag can never drift from it:

    - On a failure exit (``durable_success`` False) the ``finally`` restored
      the restorable stale just before this call, so ``filter_restorable`` is
      True and that path — now back in place on disk — is filtered out rather
      than misreported as retained.
    - On a durable-success exit (``durable_success`` True) the ``finally``
      restores nothing, so ``filter_restorable`` is False and a restorable
      stale genuinely retained at ``.stale-*`` is reported instead of sitting
      unlogged until the age-based sweep — the possible-data-loss case this
      covers. A durable-success exit includes one where provenance
      persistence raised AFTER ``durable_success`` was set: the generic
      ``except`` catches it, the ``finally`` still sees ``durable_success``
      True, and this reporter names the retained stale.
    """
    if build_result is None:
        return
    restorable = set(build_result.get("_restorable_stale") or []) if filter_restorable else set()
    for stale in build_result.get("_pending_stale_cleanup") or []:
        if stale in restorable:
            continue
        log_lines.append(f"Previous checkout retained at: {stale}")
        logger.info("Retained stale checkout: %s", stale)


async def _retained_startup_refusal(name: str, log_lines: list[str]) -> dict[str, Any] | None:
    """Return a retryable refusal while old-version startup code remains live."""
    # Deferred to avoid registry -> hooks_integration -> manager import cycles at
    # module load. The dispatcher exists only in the gateway process; without it
    # there is no in-process retained startup task to own.
    from kiro_crew.apps.hooks_integration import stop_retained_startup_hooks

    if await stop_retained_startup_hooks(name, bounded=True):
        return None
    message = (
        f"cannot reinstall {name!r} while its timed-out startup hook is still "
        "running; retry after it exits"
    )
    log_lines.append(message)
    return {
        "ok": False,
        "name": name,
        "error": message,
        "code": "startup_hook_still_running",
        "retryable": True,
    }


async def install_from_registry(
    name: str,
    log_lines: list[str] | None = None,
) -> dict[str, Any]:
    """Clone an app from its git repo and install it.

    Source code is cloned to ``~/.kiro/crew/app-sources/{name}/`` (persistent,
    survives reboots, used by app update scripts).

    For self-managed apps (``managed: "self"`` in registry), only the clone +
    install script is run — Kiro Crew does NOT copy files to ``~/.kiro/crew/apps/``
    or register resources via bridges.  The app registers itself at runtime.

    For kirocrew-managed apps, files are copied to ``~/.kiro/crew/apps/{name}/``
    and resources are registered via bridges.py as usual.

    Args:
        name: Registry app name.
        log_lines: Optional list to collect log output.  Pass a
            :class:`StreamingLogLines` instance to stream logs in real-time
            via the SSE install endpoint.  If *None*, a plain ``list`` is used
            (original behaviour).

    Steps:
    1. Validate the app exists in the trusted registry JSON
    2. Clone the repo to ~/.kiro/crew/app-sources/{name}/ (timeout: 60s)
    3. Build it (npm/pip, auto-detected) then run the install script from
       app.json if any (timeout: 300s)
    4. For kirocrew-managed: call install_app() or update_app()
    5. Store ``registry:<name>`` plus structured provenance (source URL,
       originating registry, resolved commit, verified signer) for future updates

    Returns a dict with ok, name, message/error, and log output.
    """
    # An already-installed app that carries provenance may only be re-installed
    # (updated) from the source it came from; fresh installs and legacy records
    # keep the historical bare-name lookup. Blocking reads → off the loop.
    # Reject an inadmissible name BEFORE the registry lookup and any
    # clone/build/onInstall work. The manifest/self-registration gates repeat
    # this check, but for a self-managed app they only fire at runtime
    # self-registration — without this early refusal the install would clone,
    # build, and run onInstall, then report success while leaving an
    # unregisterable checkout behind. Name admissibility is independent of
    # registry contents, so this precedes _resolve_install_entry.
    name_error = app_name_error(name)
    if name_error:
        outcome_early: dict[str, Any] = {
            "ok": False,
            "name": name,
            "error": name_error,
            "log": "",
        }
        # `code` only for the reserved-name refusals — same contract as the
        # register_external_app path (is_reserved_app_name gates the code there).
        if is_reserved_app_name(name):
            outcome_early["code"] = RESERVED_APP_NAME_CODE
        return outcome_early

    entry, pin_error = await asyncio.to_thread(_resolve_install_entry, name)
    if pin_error:
        try:
            sel().log_api_access(
                caller="app_install_from_registry",
                operation="provenance_mismatch",
                outcome="rejected",
                resources=f"name={name!r}",
                error=pin_error,
            )
        except Exception as exc:  # an audit failure must never mask the refusal
            logger.debug("SEL audit failed for %s provenance mismatch: %s", name, exc)
        return {"ok": False, "name": name, "error": pin_error}
    if not entry:
        return {"ok": False, "error": f"app {name!r} not found in registry"}

    git_url = _entry_git_url(entry)
    if not git_url:
        return {"ok": False, "error": f"app {name!r} has no git URL configured"}
    if _git_target_is_unsupported(git_url):
        return {
            "ok": False,
            "name": name,
            "error": (
                "app registry clone URL contains an unsupported query or fragment or "
                "an ambiguous Git transport identity"
            ),
            "code": "invalid_registry_source",
        }
    persisted_git_url = _strip_git_target_userinfo(git_url)

    # A per-app execution grant is consent to the repository the operator saw,
    # not to whichever repository later claims the same app name. New grants
    # record that coordinate; a legacy name-only grant needs one-time re-consent
    # before repository-backed bytes can be fetched or executed. This gate runs
    # before manifest fetch, credential selection, clone, build, or setup code.
    granted_repository = trusted_app_repository(name)
    trust_denied = repository_bound_grant_denied(name, repository=git_url)
    if trust_denied:
        # The exact coordinates are comparison inputs, not log/API data. Clone
        # URLs can contain userinfo credentials; every copy of this reason is
        # audited or returned to the dashboard, so keep it credential-free.
        reason = trust_denied
        # A bound mismatch must first be revoked. A legacy unbound grant instead
        # needs the normal consent dialog, whose stable trigger is the execution
        # denial code. Keep both existing wire behaviours explicit.
        code = "app_trust_repository_mismatch" if granted_repository else "app_execution_denied"
        audit_operation = (
            "trust_repository_mismatch"
            if granted_repository
            else "trust_repository_binding_required"
        )
        try:
            sel().log_api_access(
                caller="app_install_from_registry",
                operation=audit_operation,
                outcome="rejected",
                resources=f"name={name!r}",
                error=reason,
            )
        except Exception as exc:  # an audit failure must never mask the refusal
            logger.debug("SEL audit failed for %s trust repository mismatch: %s", name, exc)
        return {
            "ok": False,
            "name": name,
            "error": reason,
            "code": code,
        }

    raw_repo = entry.get("repo", "")
    repo = _strip_git_target_userinfo(raw_repo) if isinstance(raw_repo, str) else ""
    branch = entry.get("branch", "main")
    subdirectory = entry.get("subdirectory", "")
    # Pinning is a CATALOG mechanism, so the pin is read only for a catalog row.
    #
    # `commit` is on the row-projection allowlist (`_REGISTRY_ROW_KEYS`), and that
    # projection also builds rows from an external registry's index -- untrusted,
    # index-controlled JSON. Reading it unconditionally would hand that index a
    # capability its `branch` field cannot express: a fetch BY SHA reaches objects
    # no branch contains (a commit force-pushed away, or one that only ever existed
    # on a side ref), while a branch clone can only ever reach what a ref points at.
    # The owner-configured `branch` would then stop bounding which code gets built
    # and runs `onInstall`.
    #
    # `_is_catalog_row` is the right test rather than a bare `_catalog` check,
    # because `_catalog` is index-settable while `_registry` is attached server-side
    # per configured registry and cannot be forged.
    if _is_catalog_row(entry):
        commit = str(entry.get("commit", "") or "")
    else:
        commit = ""
        if entry.get("commit"):
            # Not a refusal: `branch` is exactly the coordinate such a row is
            # entitled to, so honouring it is correct. But an index author who
            # believes they pinned deserves to see that they did not.
            logger.warning(
                "ignoring commit pin on non-catalog row %r: pinning is a catalog "
                "mechanism; installing from branch %r instead",
                name,
                branch,
            )

    # The pin is honoured or the install is refused -- there is no third option.
    #
    # `branch` above defaults to "main", and that default is what makes a quiet
    # failure possible: a catalog row carries a commit and no branch, so a path
    # that ignored `commit` would clone the tip of "main", SUCCEED, and record the
    # tip's commit as this app's provenance. The store would then look like it
    # installs pinned bytes while installing whatever the app's default branch
    # holds today. Refusing a malformed pin is the only safe answer, because the
    # alternative is inventing coordinates nobody signed.
    if commit and not _COMMIT_SHA_RE.match(commit):
        return {
            "ok": False,
            "name": name,
            "error": (
                f"app {name!r} carries a malformed pinned commit; refusing to "
                f"install rather than fall back to a branch"
            ),
        }

    # Confused-deputy defense on the INSTALL path (companion to the automatic
    # browse/refresh defense in ``anonymous_git_env``). An entry that came from
    # an owner-configured *external* registry index carries ``_registry`` (set
    # when the index is fetched/cached); its ``repo`` URL is index-controlled
    # content, not a repo the owner typed — the owner clicked Install on an
    # index-authored name/description. Because ``is_clone_host_trusted`` is
    # host-granular, such an entry can point at a private *sibling* repo on the
    # owner's own trusted forge; cloning it with the gateway's ambient git/ssh
    # identity would read that private repo as a confused deputy. So an
    # index-originated install clones credential-free + strict-sandboxed too.
    # Bundled (curated, shipped with Kiro Crew) entries have no ``_registry`` marker
    # and remain owner-designated → full credentials.
    #
    # Same-repo credential carve-out: when the entry's effective clone URL is
    # byte-identical to the owner-configured registry repo URL, the
    # confused-deputy argument does not apply — the owner explicitly designated
    # exactly that URL by adding the registry. The carve-out flips BOTH env
    # AND sandbox mode together (the strict sandbox hiding ~/.ssh is the
    # load-bearing enforcement on credential-helper setups, not the env alone).
    # Sibling repos on the same host remain anonymous+strict.
    # Credential posture and OFFICIALNESS are two different questions, and a
    # catalog row is the case that separates them: its URL arrives in a document
    # fetched over the network whose signature this client does not yet verify, so
    # it is remote-controlled content exactly like an external index's URL -- but
    # it IS an app we list, so its install receipt must still fire.
    #
    # Treating "no `_registry`" as "the owner designated this repo" was true while
    # the only marker-less rows came from the wheel's bundled seed, which the owner
    # installed deliberately. A catalog row is not that: nobody typed its URL, and
    # a repointed row on a trusted forge would otherwise be cloned with the
    # gateway's ambient git/ssh identity -- the confused-deputy read this posture
    # exists to prevent.
    index_originated = _remote_controlled_url(
        entry
    )  # OFFICIALNESS is decided from `_registry` ALONE, and BEFORE the
    # owner-designated carve-out below: that carve-out flips index_originated as a
    # CREDENTIAL decision (owner explicitly designated the repo), but an
    # external-index entry never becomes an official-catalog entry — install
    # receipts must not fire for it. A catalog row has no `_registry`, so it stays
    # official even though it takes the credential-free posture above.
    official_entry = _official_entry(entry)
    # The originating external registry id, recorded as provenance. Empty means
    # the bundled catalog shipped with Kiro Crew, which is itself a distinct source.
    # Captured BEFORE the owner-designated carve-out (same reasoning as above):
    # the entry still came from that external registry, and provenance must say so.
    source_registry = _strip_git_target_userinfo(str(entry.get("_registry", "") or ""))
    owner_designated_target = (
        await asyncio.to_thread(_owner_designated_repo_target, entry) if index_originated else ""
    )
    if index_originated and owner_designated_target:
        index_originated = False
        _sel_credential_grant("install_from_registry", _entry_git_url(entry) or "")
    elif index_originated and await _owner_tier_confirmed(entry):
        # An ``owner``-tier registry re-confirmed this exact clone URL in a fresh
        # fetch of its index. Install-only and never from the cache — see
        # `_owner_tier_confirmed`.
        index_originated = False
        _sel_credential_grant("install_from_registry_owner_tier", _entry_git_url(entry) or "")
    # Capture event kind before clone/build/install scripts can register or
    # otherwise change app state. The receipt describes this call's starting
    # state, not an intermediate side effect.
    was_installed = get_app(name) is not None

    # Fetch the app's manifest for platform info and install script. This is a
    # read-only metadata fetch (git archive of app.json), safe to do before the
    # admission gate so a correctly-signed manifest can be passed to it.
    # Same-repo carve-out: if the entry is from an external index but its clone
    # URL matches the owner-configured registry repo (index_originated was
    # flipped to False above), use owner credentials for the manifest fetch too.
    manifest_owner_designated = bool(entry.get("_registry")) and not index_originated
    manifest = await _fetch_app_manifest(
        repo,
        branch,
        subdirectory,
        app_name=name,
        git_url=owner_designated_target or git_url,
        owner_designated=manifest_owner_designated,
        commit=commit,
    )

    # Admission: gate AFTER the manifest fetch (so a signed manifest is verified)
    # but BEFORE the repo is cloned and setup.onInstall runs, so a banned /
    # non-allowlisted / unsigned app is never cloned nor its install script run.
    admission_manifest = AppManifest.from_dict(manifest) if manifest else None
    denied = app_admission_denied(name, manifest=admission_manifest, action="install_from_registry")
    if denied:
        sel().log_api_access(
            caller="app_install_from_registry",
            operation="admission",
            outcome="rejected",
            resources=f"name={name!r}",
            error=denied,
        )
        return {"ok": False, "name": name, "error": f"blocked by admission policy: {denied}"}

    # NOTE: the provenance signer is computed LATER, from the identity-checked
    # CLONED manifest — not from this pre-clone prefetch. An update can pull a
    # commit whose manifest is not signed (or is signed by someone else);
    # provenance must record the artifact actually installed, not the preview.

    # Platform compatibility check — if the app requires a specific OS and
    # Kiro Crew is running on an incompatible platform, return client install
    # instructions instead of attempting a server-side install.
    manifest_platform = (manifest or {}).get("platform", {})
    required_os = manifest_platform.get("os", ["macos", "linux"])
    install_mode = manifest_platform.get("installMode", "server")

    from kiro_crew.apps.manifest import PlatformConfig

    if install_mode == "client" and not PlatformConfig(os=required_os).supports_platform(
        sys.platform
    ):
        client_install = manifest_platform.get("clientInstall", {})
        os_label = ", ".join(o.capitalize() if o != "macos" else "macOS" for o in required_os)
        return {
            "ok": False,
            "needsClientInstall": True,
            "name": name,
            "clientInstall": client_install,
            "platform": {"required": required_os, "current": PlatformConfig.current_os()},
            "error": f"This app requires {os_label} and must be installed on your local machine.",
        }

    is_self_managed = entry.get("resources") == "app"
    if log_lines is None:
        log_lines = []

    startup_refusal = await _retained_startup_refusal(name, log_lines)
    if startup_refusal is not None:
        return startup_refusal

    # Validate minKiroCrewVersion if declared
    min_version = (manifest or {}).get("minKiroCrewVersion", "")
    if min_version:
        from kiro_crew.apps.version import check_min_version

        ver_err = check_min_version(min_version)
        if ver_err:
            return {
                "ok": False,
                "name": name,
                "error": ver_err,
            }

    # detectInstalled, clone/build, dependency setup, and onInstall are all
    # executable third-party surfaces and share the same explicit admission.
    execution_denied = app_execution_denied(
        name,
        action="registry_install",
        caller="registry",
        repository=git_url,
    )
    if execution_denied:
        return {
            "ok": False,
            "name": name,
            "error": f"blocked by execution policy: {execution_denied}",
            # Same wire contract as the openCommand denial in routes.py: the
            # frontend keys its affordance off `code`, never off this prose.
            # Without it the App Store cannot tell "needs a trust grant" from
            # any other install failure and the consent modal never opens.
            "code": "app_execution_denied",
            "log": "\n".join(log_lines),
        }

    # Guard: check if already installed externally (e.g. user ran setup.sh manually)
    detect_cmd = entry.get("detectInstalled", "")
    if detect_cmd:
        try:

            base_cmd = ["/bin/sh", "-c", detect_cmd]
            # Through the single sandboxed-spawn chokepoint, not a hand-rolled
            # wrap + cgroup pair: it applies the strict launcher, the credential
            # scrub and the cgroup DoS ceiling, AND it forwards the systemd bus
            # locators that the ceiling's own `systemd-run --user` wrapper needs
            # to reach the user bus, dropping them again with an `env -u` shim
            # inside the scope so the probe itself never sees a live bus address.
            # A caller-built env that omits those locators makes `systemd-run`
            # exit 1 before the command runs, which with DEVNULL stderr reads as
            # "not installed" for every app on a cgroup-delegated host.
            #
            # `_detect_probe_env` is the credential-free base it scrubs on top of:
            # no agent socket, no git credential helper, no prompt, no toolchain
            # variable. The command string comes from a registry manifest, which
            # is untrusted content, and `strict` mode's own scrub only runs when
            # the launcher does -- not on Windows, and not on a host with no
            # sandbox backend plus agent.sandbox_allow_unsandboxed_exec -- so the
            # env handed over here is the only control left on those hosts.
            sandboxed_cmd, probe_env, _cleanup = await sandboxed_spawn_argv_async(
                base_cmd,
                mode="strict",
                env=_detect_probe_env(),
                _prepare=sandboxed_spawn_argv,
            )
            proc = await create_subprocess_limited(
                *sandboxed_cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                env=probe_env,
                start_new_session=platform_compat.IS_POSIX,
                creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
            )
            await _communicate_with_timeout(proc, timeout=5)
            if proc.returncode == 0:
                return {
                    "ok": False,
                    "name": name,
                    "error": f"{name} is already installed on this machine. "
                    f"Launch it to register with Kiro Crew automatically.",
                }
        except (asyncio.TimeoutError, OSError):
            pass

    build_result: dict[str, Any] = {}
    # Cleared only after the transaction durably succeeds; the `finally` below reads it.
    durable_success = False
    # Every `return` below assigns here first (named `outcome`, not `result` —
    # the kirocrew-managed path below already uses `result` for the
    # install_app/update_app return value). `"log"` is stamped from
    # `log_lines` at assignment time, but the `finally` backstop can append to
    # `log_lines` (a restore confirmation, or the restore-failed WARNING) AFTER
    # that value is already computed — a `return`'s expression is evaluated
    # before `finally` runs, and `str.join` produces an immutable copy, so a
    # later append never reaches an already-built "log" string. Because
    # dicts ARE mutable, holding the same object here and re-stamping
    # `outcome["log"]` at the end of `finally` (below) closes that gap instead
    # of the WARNING silently never reaching the log the user sees.
    outcome: dict[str, Any] | None = None
    try:
        # Best-effort sweep of aged .stale-* / .partial-* dirs before the
        # install — prevents unbounded accumulation without blocking.
        await _sweep_stale_checkouts()

        # The verb every refusal line of this run carries. The page titles its
        # log panel "Install log" or "Update log" by whether this app has an
        # installed record, and the copy step below chooses `update_app` or
        # `install_app` by that same record, so the streamed lines read it too
        # -- one fact, three readers. Blocking read -> off the loop. The route
        # handler holds app_lifecycle_lock(name) across the whole transaction,
        # so the answer cannot change between here and the copy step.
        verb: _InstallVerb = "update" if await asyncio.to_thread(get_app, name) else "install"

        # Step 1: Clone the app repo and build it (npm/pip auto-detected).
        # `git clone` handles fetch + branch checkout; a subsequent install
        # run fast-forwards the existing clone instead of re-cloning. The
        # cleanup state for later gates (_checkout_preexisted /
        # _pre_pull_commit) rides on build_result — it describes the ACTIVE
        # checkout, accounting for a move-aside re-clone.
        build_result = await _clone_build_app(
            owner_designated_target or git_url,
            name,
            log_lines,
            branch=branch,
            index_originated=index_originated,
            # Passed so the BUILD runs where the package is. The containment check
            # below is still authoritative for choosing app.json's directory.
            subdirectory=subdirectory,
            entry_repo=repo,
            commit=commit,
            # The registry entry's resource ownership feeds the desktop gate: the
            # runtime provisions only the apps it installs and spawns itself.
            self_managed=is_self_managed,
            verb=verb,
        )
        if not build_result["ok"]:
            # A pre-build refusal (identity/admission gate inside
            # _clone_build_app), a failed clone, or a failed build may have left
            # a non-restorable origin-mismatch checkout moved aside. Retained-stale
            # reporting and restorable-stale restoration are both owned by the
            # single `finally` below: it runs on every exit, knows durable_success,
            # and re-stamps outcome["log"], so no per-exit report or log join is
            # needed here.
            outcome = {**build_result}
            return outcome

        app_source = build_result["pkg_dir"]
        clone_root = app_source
        if subdirectory:
            # ``subdirectory`` is untrusted index-controlled content. Join it
            # under the cloned source root with symlink-resolving containment so
            # an absolute/``..``/symlink value cannot point app.json (and thus
            # setup.onInstall) at an attacker-selected path outside the clone.
            contained = _contained_join(app_source, subdirectory)
            if contained is None:
                # subdirectory FAILED containment here — by definition it is
                # an escaping value (absolute, "..", or a symlink pointing
                # outside app_source). It must never be joined onto pkg_dir
                # for a filesystem write; that is exactly what
                # _contained_join guards against. The manifest-restore step
                # of _unpoison_rejected_checkout writes to
                # ``pkg_dir / manifest_relpath`` when checkout_preexisted is
                # True, so passing the raw subdirectory as manifest_relpath
                # there would let a symlinked subdirectory redirect that
                # write outside the sandboxed checkout.
                #
                # A successful clone+build already ran (build_result["ok"] is
                # True), so any moved-aside checkout from a branch/origin
                # re-convergence must not be silently stranded by this
                # refusal — but only the delete-this-run's-checkout /
                # restore-previous-checkout branch of the helper (taken when
                # checkout_preexisted is False) is safe here: it never
                # touches manifest_relpath. When the checkout PRE-existed,
                # skip cleanup entirely and return the refusal as-is rather
                # than risk that write.
                if not build_result.get("_checkout_preexisted"):
                    # Restoring a moved-aside checkout here means giving the
                    # rejected clone's own pkg_dir back to the CALLER as the
                    # active checkout, even though the containment gate just
                    # refused it. That is only safe for a restorable
                    # (same-origin, branch-drift) stale, never for a
                    # non-restorable (origin-mismatch, different repository)
                    # one — restoring an origin-mismatched stale here is
                    # exactly the "hand the build the tree the gate refused"
                    # case _restorable_stale exists to prevent, so it must be
                    # filtered out the same way every other restoration site
                    # in this module filters it.
                    await _unpoison_rejected_checkout(
                        name,
                        app_source_dir(name),
                        log_lines,
                        checkout_preexisted=False,
                        pre_pull_commit="",
                        restore_from=_restorable_or_none(
                            build_result.get("_pending_stale_cleanup"),
                            build_result.get("_restorable_stale"),
                        ),
                    )
                # The _unpoison above restores only a restorable stale, so an
                # origin-mismatch one is stranded here — the `finally`-owned
                # reporter names it and re-stamps outcome["log"].
                outcome = {
                    "ok": False,
                    "name": name,
                    "error": f"unsafe subdirectory {subdirectory!r} escapes the app source root",
                }
                return outcome
            app_source = contained

        # NOTE: a missing app.json is handled by the identity gate below
        # (fail-closed: unreadable manifest == mismatch), so a build step that
        # DELETES the manifest still goes through the refusal path and its
        # checkout cleanup rather than returning early with a poisoned tree.

        # Read the cloned repo's app.json once: it decides both the app's
        # IDENTITY and its install script.
        # Trust model: curated registry entry → cloned repo → app.json
        # (maintained by the app author).  The install script has the same
        # trust level as any code you clone and build locally.
        manifest_data: dict[str, Any] | None = None
        try:
            manifest_raw = await asyncio.to_thread(
                (app_source / "app.json").read_text,
                "utf-8",
            )
            parsed = json.loads(manifest_raw)
            if isinstance(parsed, dict):
                manifest_data = parsed
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
            logger.debug("cloned app.json for %s is unreadable: %s", name, exc)

        # IDENTITY GATE, second pass: the primary gate already ran inside
        # _clone_build_app BEFORE the build (so a mismatched repo never executes
        # npm/pip lifecycle scripts). This re-check catches the remaining
        # window — a build step that REWRITES app.json to a different name —
        # and stays fail-closed: a missing or unparseable name is a mismatch,
        # not a pass. ``install_app``/``update_app`` derive the installed
        # identity from this manifest, so it must still match the entry here.
        if manifest_data is None or str(manifest_data.get("name", "") or "") != name:
            outcome = await _refuse_identity_mismatch(
                name,
                str((manifest_data or {}).get("name", "") or ""),
                _strip_git_target_userinfo(repo),
                clone_root,
                log_lines,
                created_this_run=not bool(build_result.get("_checkout_preexisted")),
                pre_pull_commit=str(build_result.get("_pre_pull_commit", "") or ""),
                manifest_relpath=(f"{subdirectory}/app.json" if subdirectory else "app.json"),
                manifest_snapshot=build_result.get("_pre_update_manifest"),
                restore_from=_restorable_or_none(
                    build_result.get("_pending_stale_cleanup"),
                    build_result.get("_restorable_stale"),
                ),
                verb=verb,
            )
            # Retained-stale reporting for this refusal is owned by the
            # `finally` below (it re-stamps outcome["log"] on every exit).
            return outcome

        # ADMISSION GATE, third pass — the post-build manifest is what
        # install_app/update_app will actually register, and a build step can
        # rewrite app.json; a manifest that does not satisfy the admission
        # policy (e.g. signature required but absent) must not install.
        denied = app_admission_denied(
            name,
            manifest=AppManifest.from_dict(manifest_data),
            action="install_from_registry",
        )
        if denied:
            log_lines.append(_refusal_line(verb, f"blocked by admission policy: {denied}"))
            try:
                sel().log_api_access(
                    caller="app_install_from_registry",
                    operation="admission_postbuild",
                    outcome="rejected",
                    resources=f"name={name!r}",
                    error=denied,
                )
            except Exception as exc:  # audit failure must never mask the refusal
                logger.debug("SEL audit failed for %s post-build admission: %s", name, exc)
            # Same retry-poisoning hazard as the cloned-admission gate: the
            # checkout sits at the rejected commit and the prefetch prefers it,
            # so clean up with the same delete-fresh/roll-back semantics.
            # _unpoison restores only the restorable subset; the `finally`-owned
            # reporter names any stranded non-restorable move-aside from
            # on-disk truth after this restore and re-stamps outcome["log"].
            await _unpoison_rejected_checkout(
                name,
                app_source_dir(name),
                log_lines,
                checkout_preexisted=bool(build_result.get("_checkout_preexisted")),
                pre_pull_commit=str(build_result.get("_pre_pull_commit", "") or ""),
                manifest_relpath=(f"{subdirectory}/app.json" if subdirectory else "app.json"),
                manifest_snapshot=build_result.get("_pre_update_manifest"),
                restore_from=_restorable_or_none(
                    build_result.get("_pending_stale_cleanup"),
                    build_result.get("_restorable_stale"),
                ),
            )
            outcome = {
                "ok": False,
                "name": name,
                "error": f"blocked by admission policy: {denied}",
            }
            return outcome

        # NOTE: the provenance commit AND signer are both resolved AFTER the
        # install-script block below — onInstall runs with write access to the
        # checkout and can advance it to another commit or swap the manifest;
        # provenance must record the state that actually registers.

        install_script = (manifest_data.get("setup") or {}).get("onInstall", "")

        # Which of the desktop gate's layout inputs exist BEFORE the install
        # script runs. The script has write access to the checkout, and a file it
        # creates is untracked, so the post-refusal `git reset --keep` leaves it in
        # a pre-existing checkout -- where a created `pyproject.toml` would make
        # every retry refuse at the BUILD gate, before the script that could be
        # fixed ever runs again. The refusal below removes what the script created
        # and nothing that was already there.
        layout_before = await asyncio.to_thread(_desktop_layout_present, app_source)

        # Step 2: Run install script
        if install_script:
            log_lines.append(f"Running install script: {install_script}")
            # Sandboxed via wrap_argv(); consider migrating to AcpClient._spawn() for full OS-level isolation.
            # SEL audit event emitted below for traceability.
            logger.info(
                "Executing sandboxed install script for app %s from repo %s",
                name,
                _strip_git_target_userinfo(repo),
            )

            def _audit_script(result: str, exit_code: object = None) -> None:
                resources = f"{name} repo={_strip_git_target_userinfo(repo)}"
                if exit_code is not None:
                    resources += f" exit={exit_code}"
                try:
                    sel().log_api_access(
                        caller="registry",
                        operation="app_install_script",
                        outcome=result,
                        resources=resources,
                    )
                except Exception as exc:
                    logger.debug("SEL audit failed for app %s install: %s", name, exc)

            _audit_script("started")
            # Wrap with safe defaults:
            #   set -e  — exit on first error
            #   set -u  — treat unset variables as errors (prevents rm -rf $EMPTY/)
            #   set -o pipefail — propagate pipe failures
            safe_script = f"set -euo pipefail\n{install_script}"

            base_cmd = ["/bin/bash", "-c", safe_script]
            sandboxed_cmd, _cleanup = await wrap_argv_async(
                base_cmd, mode="standard", _prepare=wrap_argv
            )
            sandboxed_cmd = cgroup_scope_argv(sandboxed_cmd)  # cgroup DoS ceiling
            proc = await create_subprocess_limited(
                *sandboxed_cmd,
                cwd=str(app_source),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=minimal_env(NONINTERACTIVE="1"),
                start_new_session=platform_compat.IS_POSIX,
                creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
            )
            try:
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=_SCRIPT_TIMEOUT)
            except asyncio.TimeoutError:
                # Kill the entire process group (shell + children), reap the
                # child, and escalate SIGTERM -> SIGKILL if it ignores the term.
                await _kill_process_group(proc)
                _audit_script("timed_out", proc.returncode)
                # Retained-stale reporting and restorable-stale restoration are
                # owned by the `finally` below (it re-stamps outcome["log"]).
                outcome = {
                    "ok": False,
                    "name": name,
                    "error": f"install script timed out after {_SCRIPT_TIMEOUT}s",
                }
                return outcome

            lines = stdout.decode(errors="replace").strip().split("\n")
            if len(lines) > 50:
                log_lines.append(f"... ({len(lines) - 50} lines truncated)")
                log_lines.extend(lines[-50:])
            else:
                log_lines.extend(lines)

            _audit_script("completed" if proc.returncode == 0 else "failed", proc.returncode)
            if proc.returncode != 0:
                # Retained-stale reporting and restorable-stale restoration are
                # owned by the `finally` below (it re-stamps outcome["log"]).
                outcome = {
                    "ok": False,
                    "name": name,
                    "error": f"install script failed (exit {proc.returncode})",
                }
                return outcome

            # Reap any SURVIVING descendants of the script's process group
            # before the final gates re-read app.json: a backgrounded child
            # (`nohup evil &`) outlives the shell's clean exit and could
            # rewrite the manifest AFTER the re-read below but before
            # install_app registers it — the exact TOCTOU the final pass
            # exists to close. The shell itself already exited, so anything
            # still in the group is a detached straggler with no legitimate
            # claim to keep running.
            #
            # POSIX: signal the KNOWN group id directly — the script was
            # spawned with start_new_session, so its pgid equals proc.pid by
            # construction, and the group outlives its (already-reaped)
            # leader. Resolving the group via getpgid(proc.pid) would raise
            # ProcessLookupError once the leader is reaped, silently skipping
            # the very stragglers this exists to kill. The pid>1 guard keeps
            # the killpg broadcast-safe (never signal group 0/1/self).
            # Windows: taskkill /T on the root pid via the platform shim.
            try:
                if platform_compat.IS_POSIX:
                    if type(proc.pid) is int and proc.pid > 1:
                        await asyncio.to_thread(os.killpg, proc.pid, platform_compat.SIGKILL)
                else:
                    await platform_compat.kill_process_tree_async(proc.pid, platform_compat.SIGKILL)
            except OSError:
                # Empty group (no stragglers) — the common case.
                pass

            # IDENTITY + ADMISSION, final pass — the install script just ran
            # with write access to the checkout and can rewrite app.json, and
            # install_app/update_app/register_external_app re-read that file
            # from disk. Whatever is on disk NOW is what gets registered, so it
            # must pass the same fail-closed gates as the post-build read.
            manifest_data = None
            try:
                parsed = json.loads(
                    await asyncio.to_thread((app_source / "app.json").read_text, "utf-8")
                )
                if isinstance(parsed, dict):
                    manifest_data = parsed
            except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
                logger.debug("post-script app.json for %s is unreadable: %s", name, exc)
            if manifest_data is None or str(manifest_data.get("name", "") or "") != name:
                outcome = await _refuse_identity_mismatch(
                    name,
                    str((manifest_data or {}).get("name", "") or ""),
                    _strip_git_target_userinfo(repo),
                    clone_root,
                    log_lines,
                    created_this_run=not bool(build_result.get("_checkout_preexisted")),
                    pre_pull_commit=str(build_result.get("_pre_pull_commit", "") or ""),
                    manifest_relpath=(f"{subdirectory}/app.json" if subdirectory else "app.json"),
                    manifest_snapshot=build_result.get("_pre_update_manifest"),
                    restore_from=_restorable_or_none(
                        build_result.get("_pending_stale_cleanup"),
                        build_result.get("_restorable_stale"),
                    ),
                    # The script ran: what it created by the layout names goes
                    # before the rollback, as on every gate after the script.
                    appeared_layout=(app_source, layout_before),
                    verb=verb,
                )
                # Retained-stale reporting for this post-script refusal is owned
                # by the `finally` below (it re-stamps outcome["log"]).
                return outcome
            denied = app_admission_denied(
                name,
                manifest=AppManifest.from_dict(manifest_data),
                action="install_from_registry",
            )
            if denied:
                log_lines.append(_refusal_line(verb, f"blocked by admission policy: {denied}"))
                try:
                    sel().log_api_access(
                        caller="app_install_from_registry",
                        operation="admission_postscript",
                        outcome="rejected",
                        resources=f"name={name!r}",
                        error=denied,
                    )
                except Exception as exc:  # audit failure must never mask the refusal
                    logger.debug("SEL audit failed for %s post-script admission: %s", name, exc)
                # onInstall ran with write access to the checkout, so this
                # denial leaves it poisoned exactly like the earlier gates --
                # the layout files it created go first, then the same
                # delete-fresh/roll-back cleanup, so a retry can pull a fixed
                # remote instead of re-rejecting at prefetch or at the build gate.
                # _unpoison restores only the restorable subset; the
                # `finally`-owned reporter names any stranded non-restorable
                # move-aside from on-disk truth and re-stamps outcome["log"].
                await _roll_back_post_script_refusal(
                    name,
                    app_source,
                    app_source_dir(name),
                    log_lines,
                    layout_before=layout_before,
                    checkout_preexisted=bool(build_result.get("_checkout_preexisted")),
                    pre_pull_commit=str(build_result.get("_pre_pull_commit", "") or ""),
                    manifest_relpath=(f"{subdirectory}/app.json" if subdirectory else "app.json"),
                    manifest_snapshot=build_result.get("_pre_update_manifest"),
                    restore_from=_restorable_or_none(
                        build_result.get("_pending_stale_cleanup"),
                        build_result.get("_restorable_stale"),
                    ),
                )
                outcome = {
                    "ok": False,
                    "name": name,
                    "error": f"blocked by admission policy: {denied}",
                }
                return outcome

        # DESKTOP GATE, final pass — the build judged the requirements waiver on
        # the manifest that ENTERED the install script, and the script ran with
        # write access to the checkout: it can add `backend.hooks` (imported into
        # this process, which the app deps tree never reaches) or drop the entry
        # point / stdio server the waiver was granted for. Whatever is on disk
        # NOW is what registers and what the runtime later loads from, so the
        # same verdict is re-derived from it, by the same owner, with the same
        # ownership input (`is_self_managed`: a self-managed entry is registered
        # from its manifest alone and is never provisioned, so it keeps the
        # refusal) — and, being the FINAL pass, strictly: an entry file the
        # script was expected to create but did not, or a `requirements.txt`
        # link still dangling, is refused here (the build pass let absence
        # through because the script had not yet had its window). A refused
        # checkout is rolled back exactly like the post-script admission denial
        # above — nothing is enabled. Off-loop like the app.json read above:
        # the verdict stats the checkout.
        try:
            desktop_refusal = await asyncio.to_thread(
                _desktop_build_refusal,
                app_source,
                AppManifest.from_dict(manifest_data),
                self_managed=is_self_managed,
                final=True,
            )
            refusal_code = DESKTOP_BUILD_STEP_UNSUPPORTED
        except InstalledTreeRefused as exc:
            # The preview copy produced a tree `install_app` would refuse (a root
            # `data` that is not a directory): the install's own refusal, before
            # any transaction touched the app directory. Rolled back below like
            # the desktop refusal, reported WITHOUT its code -- a browser install
            # refuses the same tree, so the reader is not sent there.
            desktop_refusal, refusal_code = str(exc), ""
        except RuntimeError as exc:
            # The preview COPY itself failed (`_installed_tree_preview` wraps the
            # copy's `OSError`): not a verdict but a checkout the copy cannot read
            # -- and the script just ran with write access to it, so an unreadable
            # leaf, a FIFO or a symlink loop it left is the same third-party-script
            # write access every post-script gate rolls back. Left in the live
            # slot, a fresh clone so poisoned makes every retry fail at the build
            # pass before any script runs again, and nothing else removes it (the
            # `finally` restores a moved-aside checkout, which a first install
            # never has). So the failure takes the one post-script rollback too:
            # a fresh clone is deleted whole and the slot is empty for a clean
            # re-clone, a pre-existing checkout has its appeared layout files set
            # aside and is reset to its last-good state. Reported as the ordinary
            # error it is -- the cause sentence, no desktop code, retryable.
            preview_failure = str(exc)
            logger.warning("final desktop gate could not preview %s: %s", name, preview_failure)
            log_lines.append(preview_failure)
            await _roll_back_post_script_refusal(
                name,
                app_source,
                app_source_dir(name),
                log_lines,
                layout_before=layout_before,
                checkout_preexisted=bool(build_result.get("_checkout_preexisted")),
                pre_pull_commit=str(build_result.get("_pre_pull_commit", "") or ""),
                manifest_relpath=(f"{subdirectory}/app.json" if subdirectory else "app.json"),
                manifest_snapshot=build_result.get("_pre_update_manifest"),
                restore_from=_restorable_or_none(
                    build_result.get("_pending_stale_cleanup"),
                    build_result.get("_restorable_stale"),
                ),
            )
            # Through `outcome`, like every post-clone exit: the `finally` stamps
            # the log (the rollback's own lines included) onto this same dict.
            outcome = {"ok": False, "name": name, "error": preview_failure}
            return outcome
        if desktop_refusal:
            log_lines.append(_refusal_line(verb, desktop_refusal))
            try:
                sel().log_api_access(
                    caller="app_install_from_registry",
                    operation="desktop_build_gate_final",
                    outcome="rejected",
                    resources=f"name={name!r}",
                    error=desktop_refusal,
                )
            except Exception as exc:  # audit failure must never mask the refusal
                logger.debug("SEL audit failed for %s final desktop gate: %s", name, exc)
            # The layout inputs the script created go first, then the checkout is
            # rolled back -- the one post-script rollback every gate after the
            # script shares (why the order matters is on the helper).
            await _roll_back_post_script_refusal(
                name,
                app_source,
                app_source_dir(name),
                log_lines,
                layout_before=layout_before,
                checkout_preexisted=bool(build_result.get("_checkout_preexisted")),
                pre_pull_commit=str(build_result.get("_pre_pull_commit", "") or ""),
                manifest_relpath=(f"{subdirectory}/app.json" if subdirectory else "app.json"),
                manifest_snapshot=build_result.get("_pre_update_manifest"),
                restore_from=_restorable_or_none(
                    build_result.get("_pending_stale_cleanup"),
                    build_result.get("_restorable_stale"),
                ),
            )
            outcome = {"ok": False, "name": name, "error": desktop_refusal}
            if refusal_code:
                outcome["code"] = refusal_code
            return outcome

        # Provenance is pinned from the FINAL state — after the build, the
        # install script, and the last identity/admission gates: the exact
        # commit the checkout sits at, and whoever signed the manifest that
        # actually registers. Resolving either any earlier would let onInstall
        # advance the checkout or swap the manifest and have provenance record
        # a predecessor. Purely observational: never denies; unsigned yields "".
        source_commit = await asyncio.to_thread(_resolved_clone_commit, clone_root)
        source_signer = await asyncio.to_thread(
            verified_signer, AppManifest.from_dict(manifest_data)
        )

        # Step 3: Resolve dependencies (if declared in manifest)
        deps_data = manifest_data.get("dependencies")
        if deps_data and isinstance(deps_data, dict):
            from kiro_crew.apps.dependencies import resolve_dependencies as _resolve_deps
            from kiro_crew.apps.manifest import Dependencies as _Deps

            deps = _Deps.from_dict(deps_data)
            dep_result = await _resolve_deps(name, deps)
            if dep_result.installed:
                log_lines.append(f"Installed {len(dep_result.installed)} dependency(ies)")
            if dep_result.failed:
                log_lines.append(
                    f"Failed to install {len(dep_result.failed)} dependency(ies): {', '.join(dep_result.failed)}"
                )
            if dep_result.missing:
                log_lines.append(f"Missing commands: {', '.join(dep_result.missing)}")

        # A clone/build/install script can take minutes. Recheck at the shared
        # replacement boundary so startup execution that became retained during
        # that work cannot overlap either managed file replacement or
        # self-managed metadata replacement.
        startup_refusal = await _retained_startup_refusal(name, log_lines)
        if startup_refusal is not None:
            outcome = startup_refusal
            return outcome

        # Step 4: Register with Kiro Crew
        if is_self_managed:
            # Pre-register with manifest from the cloned repo so the app
            # appears in Installed tab immediately (with openCommand, icon, etc.)
            # The app will update its own registration on next launch.
            # ``manifest_data`` is the identity-checked read from above — reusing
            # it avoids a second read that could see different bytes.
            from kiro_crew.apps.manager import register_external_app

            display = manifest_data.get("displayName", name)
            version = manifest_data.get("version", "0.0.0")
            # Set BEFORE any of the fallible bookkeeping below, because this branch
            # returns ok=True regardless of how the registration and provenance writes
            # go: the clone is in place and the app will register itself on next
            # launch. Leaving it False would report success to the caller while the
            # `finally` rolled the source checkout back underneath it.
            durable_success = True
            reg_result = register_external_app(
                name=name,
                version=version,
                display_name=display,
                source=f"{SOURCE_REGISTRY_PREFIX}{name}",
                manifest_data=manifest_data,
                origin="registry",
                source_repository=persisted_git_url,
            )
            if reg_result.ok:
                set_app_provenance(
                    name,
                    source=f"{SOURCE_REGISTRY_PREFIX}{name}",
                    url=persisted_git_url,
                    registry=source_registry,
                    commit=source_commit,
                    signer=source_signer,
                )

            log_lines.append("Pre-registered from cloned manifest (self-managed)")
            log_lines.append("App will update its own registration on next launch")
            # Retained moved-aside checkouts (the user can recover local edits;
            # swept after _STALE_CHECKOUT_RETENTION_DAYS) are reported by the
            # `finally`-owned reporter: durable_success is True, so it runs with
            # filter_restorable=False and names the genuinely-retained restorable
            # stale rather than letting it sit unlogged until the sweep.
            if official_entry:
                await install_receipt.dispatch_async(
                    name,
                    official=True,
                    kind=(
                        install_receipt.KIND_UPDATE if was_installed else install_receipt.KIND_FRESH
                    ),
                )
            outcome = {
                "ok": True,
                "name": name,
                "message": (
                    f"installed {name} from {_strip_git_target_userinfo(repo)} " "(self-managed)"
                ),
            }
            notice = getattr(reg_result, "notice", "")
            if isinstance(notice, str) and notice:
                outcome["notice"] = notice
            return outcome

        # Managed by Kiro Crew: copy to ~/.kiro/crew/apps/ and register resources
        log_lines.append("Installing app...")
        # Lock-free: the route handler holds app_lifecycle_lock(name) across
        # the whole transaction (clone/build → copy → register → backend
        # start); asyncio.Lock is not reentrant, so no acquisition here.
        existing = get_app(name)
        # Off-loop: install_app/update_app do a blocking filesystem copy
        # that can take minutes on large source trees — on the loop it
        # would trip the loop-stall watchdog and kill the gateway.
        # Preserve the long-standing one-positional-argument manager contract.
        # The scoped coordinate is copied into asyncio.to_thread's context, so
        # the manager still performs its final repository-binding check and
        # writes safe provisional provenance without trusting app.json.
        with registry_source_repository(persisted_git_url):
            if existing:
                result = await asyncio.to_thread(update_app, str(app_source))
            else:
                result = await asyncio.to_thread(install_app, str(app_source))
        log_lines.append(result.message or result.error or "done")

        # Record the source marker plus structured provenance, so a later update
        # resolves the source this install actually came from rather than
        # whichever entry happens to answer to the bare name. This is also what
        # self-heals a legacy record: its next successful update writes the full
        # provenance it was missing.
        if result.ok:
            # BEFORE the bookkeeping below, not after. `install_app`/`update_app` has
            # already copied the files into place, so the installed app IS updated. If
            # provenance persistence then raises, deciding "not durable" and rolling
            # the SOURCE checkout back would leave installed files from the new
            # version beside a source tree from the old one -- a torn state worse than
            # either outcome. A failed receipt is a bookkeeping problem to log; it does
            # not un-install what is installed.
            durable_success = True
            set_app_provenance(
                result.name,
                source=f"{SOURCE_REGISTRY_PREFIX}{name}",
                url=persisted_git_url,
                registry=source_registry,
                commit=source_commit,
                signer=source_signer,
            )
            # Retained moved-aside checkouts are reported by the `finally`-owned
            # reporter (durable_success is True, filter_restorable=False), so a
            # genuinely-retained restorable stale is named rather than sitting
            # unlogged at `.stale-*` until the sweep. NOTE set_app_provenance
            # above runs while durable_success is already True: if it raises, the
            # generic `except` catches it, the `finally` does NOT restore (durable
            # success), and it reports with filter_restorable=not durable_success
            # = False — so the restorable stale is reported, not stranded.
            if official_entry:
                # Detached best-effort telemetry runs only after durable success.
                await install_receipt.dispatch_async(
                    name,
                    official=True,
                    kind=(
                        install_receipt.KIND_UPDATE if was_installed else install_receipt.KIND_FRESH
                    ),
                )
        # Install/update failed AFTER a successful clone+build: durable_success
        # stays False, so the `finally` restores the restorable stale and its
        # reporter filters it out — no per-exit report is needed here.

        outcome = {
            "ok": result.ok,
            "name": name,
            "message": result.message,
            "error": result.error,
        }
        if result.notice:
            # e.g. ``session_approval_reconsent``: the app was left disabled on
            # purpose and the routes must neither start it nor report plain success.
            outcome["notice"] = result.notice
        return outcome

    except Exception as exc:
        logger.exception("Failed to install %s from registry", name)
        # Retained-stale reporting and restorable-stale restoration are owned by
        # the `finally` below. It reports with filter_restorable=not
        # durable_success, which is precisely why an exception raised AFTER
        # durable_success was set (e.g. set_app_provenance) still names the
        # genuinely-retained restorable stale instead of stranding it.
        outcome = {"ok": False, "name": name, "error": str(exc)}
        return outcome
    finally:
        # RESTORATION BELONGS TO THE LIFETIME, NOT TO THE LIST OF FAILURES.
        #
        # A pinned install moves the previous checkout aside on every reinstall, and
        # this function has seven post-clone exits (containment, identity mismatch,
        # two admission gates, onInstall, the install step, the happy path) plus an
        # exception path and cancellation. Restoring on the branches instead meant an
        # `onInstall` that exited non-zero returned early and left the user's only
        # edited copy as a `.stale-*` sibling for the retention sweep to delete.
        #
        # One site, reached by every exit. It is a no-op unless a moved-aside
        # checkout exists AND the transaction did not durably succeed, so the
        # pre-clone exits and the happy path both pass through untouched.
        if not durable_success:
            try:
                pending = build_result.get("_restorable_stale") or []
                if pending:
                    _restore_moved_aside(
                        Path(pending[0]),
                        # `app_source_dir(name)`, NOT `build_result["pkg_dir"]`: every
                        # post-clone FAILURE dict omits `pkg_dir`, so reading it raised a
                        # KeyError that the broad catch below swallowed -- the
                        # restoration silently did nothing on exactly the exits it
                        # exists for. The destination is a function of the app name, so
                        # derive it instead of depending on a key the failure paths do
                        # not carry. It is the clone ROOT either way: a `subdirectory`
                        # entry points `app_source` inside the tree, while the
                        # moved-aside sibling replaces the whole checkout.
                        app_source_dir(name),
                        log_lines,
                        "the install did not complete",
                    )
            except Exception:  # noqa: BLE001 - never mask the outcome being returned
                # WARNING, not debug: this catch is what hid the KeyError above for
                # four review rounds. A restoration that could not run is a possible
                # data loss, so it has to be visible in the log the user sees.
                logger.warning(
                    "could not restore the moved-aside checkout for %r", name, exc_info=True
                )
                log_lines.append(
                    "WARNING: the previous checkout could not be restored; recover it "
                    "from the .stale-* sibling directory"
                )

        # THE reporter, owned by this `finally` and nowhere else. Placed AFTER
        # the restore block above so it reports on-disk truth: a restorable stale
        # the restore just put back must not then be named as retained. The flag
        # is derived, not hand-mirrored at each exit — `not durable_success` is
        # exactly the restore condition above, so a failure exit (restored) files
        # its restorable stale out and a durable-success exit (never restored)
        # keeps it. This is the whole point of the consolidation: a new exit
        # added to this function cannot forget the report or pass the wrong flag,
        # because there are no per-exit reports left to forget. A no-op unless a
        # move-aside exists (pre-clone exits and the happy-path-with-no-stale
        # pass through untouched).
        _report_retained_stale_checkouts(
            build_result, log_lines, filter_restorable=not durable_success
        )

        # Re-stamp AFTER the restore and the report above: each `return` built its
        # `outcome` dict WITHOUT a "log" key, deferring it to here so the restore
        # confirmation, the restore-failed WARNING, and the retained-stale lines
        # just produced all reach the caller. `outcome` is the SAME dict object
        # being returned (dicts are mutable), so setting its "log" key here is
        # what the caller receives. Pre-clone exits return bare dicts that already
        # carry their own "log" and never set `outcome`, so they skip this
        # backstop and keep their join.
        if outcome is not None:
            outcome["log"] = "\n".join(log_lines)
            # Scrub the internal move-aside/transaction bookkeeping keys from
            # the dict that leaves this function. They are consumed ABOVE (the
            # restore block and the reporter both read them off `build_result`,
            # never off `outcome`), so removing them here deprives no consumer.
            # Two of them -- `_pending_stale_cleanup` and `_restorable_stale` --
            # are `list[Path]`, which is not JSON-serializable, so a build
            # refusal that spreads `{**build_result}` into `outcome` would make
            # the API/SSE layer raise `TypeError` when it serialized the refusal.
            # Scrubbing the CLASS (every `_`-prefixed key) rather than those two
            # names closes it at the single seam: `_checkout_preexisted`,
            # `_pre_pull_commit`, and `_pre_update_manifest` are internal gate
            # state too, and no current or future exit can leak any of them once
            # they are stripped here. Underscore keys are internal by
            # convention; a response field the caller needs is never named `_x`.
            for _internal_key in [k for k in outcome if k.startswith("_")]:
                outcome.pop(_internal_key, None)
