"""Clone and build under the per-app lock, with the identity and admission gates between.

``_clone_build_app`` is the one exit that stamps every move-aside this run made onto
its result, refusals included, and restores the same-origin checkout itself when the
build is interrupted. ``_clone_build_app_locked`` clones or pulls, refuses a checkout
whose ``app.json`` names another app (``_refuse_identity_mismatch``) or that the
admission policy rejects, runs the facade's build step, and puts a same-origin
previous checkout back when the build fails. The streamed refusal lines name the
run's verb (``_refusal_line``), and ``_report_retained_stale_checkouts`` reports the
move-asides the install's exits leave retained.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Literal

from kiro_crew.apps.admission import app_admission_denied
from kiro_crew.apps.manifest import AppManifest
from kiro_crew.apps.registry_pipeline import _FACADE, _facade
from kiro_crew.apps.registry_pipeline.checkout import (
    _git_clone_or_pull,
    _resolved_clone_commit,
    _rmtree_force_settled,
)
from kiro_crew.apps.registry_pipeline.git_targets import (
    _git_target_is_unsupported,
    _looks_like_git_url,
    _strip_git_target_userinfo,
)
from kiro_crew.apps.registry_pipeline.install_rollback import (
    _roll_back_post_script_refusal,
    _unpoison_rejected_checkout,
)
from kiro_crew.apps.registry_pipeline.manifests import _contained_join
from kiro_crew.apps.registry_pipeline.recovery import (
    _restorable_or_none,
    _restore_moved_aside,
    app_source_dir,
)
from kiro_crew.sel import sel

logger = logging.getLogger(_FACADE)


# The verb of the action a registry install is performing: "update" when an
# installed record exists for the app, "install" otherwise. The page titles its
# log panel "Install log" or "Update log" by that record, and
# `install_from_registry` chooses `update_app` or `install_app` by it, so every
# refusal line the registry install streams reads the same fact -- never a second
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
    checkout_preexisted = await asyncio.to_thread((pkg_dir / ".git").is_dir)
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
    # loads from (`manager_lookup.py` hands the hook loaders
    # `AppManifest.from_json_file(...).to_dict()`), so a second, differently
    # normalized reading of these bytes is exactly the disagreement to avoid.
    cloned_app_manifest = AppManifest.from_dict(cloned_manifest)
    denied = await asyncio.to_thread(
        app_admission_denied,
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
                await _rmtree_force_settled(pkg_dir)
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
