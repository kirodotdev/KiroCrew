"""Putting a refused checkout back the way the install found it.

``_unpoison_rejected_checkout`` deletes a checkout this run created and restores the
one it replaced, or rolls a pre-existing workspace back to its pre-pull commit and
its exact pre-update manifest. After ``setup.onInstall`` has run,
``_roll_back_post_script_refusal`` first removes (fresh clone) or sets aside
(pre-existing checkout) the desktop layout files the script created, judged against
the ``_desktop_layout_present`` snapshot taken before it, and then un-poisons the
checkout. Each layout cleanup first re-checks (``_layout_cleanup_escaped``) that the
package directory still resolves inside a checkout that is not itself a link.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from kiro_crew import platform_compat
from kiro_crew.apps.registry_pipeline import _FACADE
from kiro_crew.apps.registry_pipeline.checkout import (
    _communicate_with_timeout,
    _rmtree_force_settled,
)
from kiro_crew.apps.registry_pipeline.manifests import _contained_join
from kiro_crew.apps.registry_pipeline.recovery import (
    _STALE_CHECKOUT_RETENTION_DAYS,
    _restore_moved_aside,
    _stale_sibling,
)
from kiro_crew.apps.registry_pipeline.subprocess_env import minimal_env
from kiro_crew.sandbox import (
    cgroup_scope_argv,
    create_subprocess_limited,
    wrap_argv,
    wrap_argv_async,
)

logger = logging.getLogger(_FACADE)


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
        # The rejected tree is a git checkout, so `rmtree_force`: `ignore_errors` leaves
        # its read-only packs on Windows. `_restore_moved_aside` then sets aside whatever
        # still survives (a file another process holds open) before it renames, so the
        # previous checkout lands instead of colliding with it (WinError 183).
        await _rmtree_force_settled(pkg_dir)
        _restore_moved_aside(restore_from, pkg_dir, log_lines, "rejecting the replacement clone")
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
