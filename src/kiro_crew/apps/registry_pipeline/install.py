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
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.apps import install_receipt
from kiro_crew.apps.admission import app_admission_denied, verified_signer
from kiro_crew.apps.execution import (
    app_execution_denied,
    repository_bound_grant_denied,
    trusted_app_repository,
)
from kiro_crew.apps.manager import (
    get_app,
    install_app,
    registry_source_repository,
    set_app_provenance,
    update_app,
)
from kiro_crew.apps.manifest import (
    RESERVED_APP_NAME_CODE,
    AppManifest,
    app_name_error,
    is_reserved_app_name,
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
    _restorable_or_none,
    _restore_moved_aside,
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
    """
    declared = cloned_name or "<missing>"
    if not created_this_run:
        log_lines.append(
            "Preserving pre-existing source checkout (rolled back to its "
            "last-good state): the refused update installed nothing, and the "
            "workspace belongs to the already-installed app"
        )
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
        f"{declared!r} — refusing to install an app under an identity that differs "
        f"from its registry entry"
    )
    log_lines.append(f"Refusing install: {error}")
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
            pending_cleanup=pending_cleanup,
            restorable_stale=restorable_stale,
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
    pending_cleanup: list[Path],
    restorable_stale: list[Path] | None = None,
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
        )

    # ADMISSION GATE, second pass — on the CLONED manifest. The first pass ran
    # on the pre-clone prefetch, but the repository can advance between the two
    # reads: a signed preview can resolve to an unsigned (or newly banned)
    # manifest at clone time, and under a require-signature policy that content
    # must not build or install. Same fail-closed policy call, different
    # artifact.
    denied = app_admission_denied(
        app_name,
        manifest=AppManifest.from_dict(cloned_manifest),
        action="install_from_registry",
    )
    if denied:
        log_lines.append(f"Refusing install: blocked by admission policy: {denied}")
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
    # The build step stays in the facade, where the internal-Python isolation
    # guard reads it by path, so it is resolved there at call time.
    result = await _facade()._run_app_build(app_source, app_name, log_lines)
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
            log_lines.append(f"Refusing install: blocked by admission policy: {denied}")
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
                log_lines.append(f"Refusing install: blocked by admission policy: {denied}")
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
                # denial leaves it poisoned exactly like the earlier gates —
                # apply the same delete-fresh/roll-back cleanup so a retry
                # can pull a fixed remote instead of re-rejecting at prefetch.
                # _unpoison restores only the restorable subset; the
                # `finally`-owned reporter names any stranded non-restorable
                # move-aside from on-disk truth and re-stamps outcome["log"].
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
