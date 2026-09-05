"""Pod, worktree, sync, rebase, and prune operations for Dev Fleet."""

from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from kiro_crew import dep_sync, frontend, hooks, platform_compat
from kiro_crew.apps.builtins.dev_fleet import fleet_state, live, npm_preflight, repository, runtime
from kiro_crew.executors import subprocess_executor
from kiro_crew.loop_lock import LoopBoundLock
from kiro_crew.sandbox import sandboxed_spawn_argv, shielded_prepare_off_loop


def _sync_base_ref() -> str:
    """The ref this process pins the revision it is about to merge to.

    ``<remote>/<base branch>`` cannot serve for this: it is a MUTABLE name, and
    the status refresher re-fetches it every ``_NET_REFRESH_S`` seconds in this
    same process. Resolving that name once in the probe and again in the merge --
    with a real install in between -- lets the two land on different commits, so
    the probe would certify a revision the merge does not install. Fetching the
    tip into a ref of our own closes the window instead of narrowing it: the
    refresher's fetch writes only the remote-tracking refs, so nothing can move
    this one for the life of the run.

    The PID is part of the name because ``_SYNC_LOCK`` is a lock in ONE process,
    which makes syncs single-flight only within a single gateway. Two gateways
    configured against the same checkout would otherwise share this ref, and the
    second one's fetch would move it between the first one's probe and merge --
    reopening exactly the window the ref exists to close. That configuration is
    already destructive for a bigger reason (two syncs running ``git merge`` and
    ``npm ci`` against one working tree), so this does not make concurrent syncs
    safe; it only stops one gateway from invalidating another's probe.

    Safe to force and safe to reuse: the fetch step rewrites it before any step
    reads it, so a value left by an earlier run -- or by an earlier process that
    happened to hold this PID -- can never be consumed.
    """
    return f"refs/kirocrew/sync-base-{os.getpid()}"


async def _prune_dead_sync_base_refs(repo: str) -> None:
    """Delete pinned base refs left behind by gateway processes that are gone.

    The PID in the ref name is what stops two gateways on one checkout from
    moving each other's pin, but nothing deletes the ref on the way out -- an
    ordinary restart strands it, and so does a killed run. Without this every
    gateway generation would leave one behind in the OPERATOR's own checkout,
    bounded only by ``pid_max`` and visible in ``git for-each-ref`` forever.

    A ref whose PID is still alive is LEFT ALONE. It may be a second gateway's
    live pin, and deleting that would reopen exactly the window the PID suffix
    exists to close -- so the prune is bounded by the number of live gateways,
    which is the real floor. Liveness goes through
    ``platform_compat.pid_exists``: a raw ``os.kill(pid, 0)`` is a liveness probe
    only on POSIX, and on Windows it TERMINATES the process it is asked about --
    which here would mean killing the very live gateway this is meant to spare.
    ``pid_exists`` also collapses "exists but we cannot signal it" into alive,
    which is the answer we want: unsure means do not touch.

    Best-effort throughout: this is housekeeping, and a sync must not fail
    because a stale ref could not be removed.
    """
    listed = await repository._git(repo, "for-each-ref", "--format=%(refname)", "refs/kirocrew/")
    if not listed:
        return
    mine = _sync_base_ref()
    prefix = "refs/kirocrew/sync-base-"
    for ref in listed.splitlines():
        ref = ref.strip()
        if not ref.startswith(prefix) or ref == mine:
            continue
        try:
            pid = int(ref[len(prefix) :])
        except ValueError:
            continue  # not one of ours to reason about
        if pid <= 0:
            # 0 and negatives are not PIDs: on POSIX they address the caller's
            # process group and every process respectively, so they must never
            # reach a liveness probe even a harmless one.
            continue
        if platform_compat.pid_exists(pid):
            continue  # owner still running -- may be another gateway's live pin
        await repository._git(repo, "update-ref", "-d", ref)


def _pod_env() -> dict:
    """Environment for pod CLI subprocesses (allowlisted base + pod repo)."""
    return {**runtime._build_env(), "KIROCREW_POD_REPO": repository._repo()}


def _read_pin_strict(cfg: Any, name: str) -> tuple[bool, str | None]:
    """Read the pod's pinned CHECKOUT with failures PROPAGATED.

    Returns ``(env_file_exists, checkout_or_none)``. Unlike
    ``rt.read_env_file`` (which swallows OSError and returns ``{}``), a read
    failure raises — the caller must treat "file exists but cannot be
    positively read" as deny, never as "unpinned". The pin file must be a
    regular non-symlink file resolving inside the pods dir and must not be a
    sensitive path (the pods dir is agent-writable; a symlinked ``.env``
    must never pull a protected file into the gateway). Runs on the executor.
    """
    env_path = cfg.env_file(name)
    if not env_path.exists():
        return False, None
    # TOCTOU-safe: O_NOFOLLOW open + fstat validation of the DESCRIPTOR
    # (symlink/regular-file/containment/sensitivity checked atomically
    # against the opened inode, not a raceable path). Raises -> caller denies.
    data = hooks.safe_read_file_bytes_nolink(str(env_path), within_root=str(cfg.pods_dir))
    if data is None:
        # hooks gate refused (symlink/hardlink/containment/sensitive/IO):
        # "exists but cannot be positively read" is a DENY, never "unpinned".
        raise OSError(f"pin file refused by hooks read gate: {env_path}")
    text = data.decode("utf-8", errors="replace")
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#") or "=" not in ln:
            continue
        key, val = ln.split("=", 1)
        if key.strip() != "CHECKOUT":
            continue
        raw = val.strip()
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
            raw = raw[1:-1]
        return True, raw or None
    return True, None


async def _pod_checkout_guard(name: str) -> str | None:
    """Pod identities are global basenames while Dev Fleet scopes worktrees to
    MAIN_REPO. Before ANY pod operation, verify the pod's pinned ``CHECKOUT``
    matches THIS repo's worktree of that name — otherwise the operation would
    land on an unrelated repository's pod (stop it, delete its isolated HOME,
    or provision the wrong checkout). Returns an error string to refuse, or
    None to proceed. Fail closed on any uncertainty."""
    target, ferr = await repository._find_worktree(name)
    if target is None:
        return ferr or f"unknown worktree: {name!r}"
    cfg = runtime._load_cfg()
    if cfg is None:
        if not runtime._POD_AVAILABLE:
            # Pod subsystem entirely absent -> nothing to collide with; the
            # pod op itself will fail with its own clear error.
            return None
        # Pods exist on this host but config cannot be loaded -> we cannot
        # verify pod identity; fail closed.
        return "cannot load pod configuration to verify pod identity"
    loop = asyncio.get_running_loop()
    try:
        env_exists, pinned = await loop.run_in_executor(
            subprocess_executor(), _read_pin_strict, cfg, name
        )
    except Exception as exc:  # noqa: BLE001
        # Pin state exists but cannot be positively read -> deny, never
        # treat as "unpinned" (that ambiguity is exactly the cross-repo hole).
        return f"cannot verify pod checkout pin: {runtime._redact(str(exc))}"
    if not env_exists:
        # No pin file: only safe when no pod under this global name is
        # ACTIVE — an active unit with a missing pin is a foreign pod we
        # cannot attribute; acting on it would stop/expose another repo's
        # gateway. Fail closed on active or unverifiable.
        try:
            active = await loop.run_in_executor(subprocess_executor(), runtime.rt.active_names, cfg)
        except Exception as exc:  # noqa: BLE001
            return f"cannot verify active pods: {runtime._redact(str(exc))}"
        if name in active:
            return (
                f"pod {name!r} is active but has no checkout pin — refusing "
                "pod operation (unattributable pod identity)"
            )
        return None
    if not pinned:
        # Pin file EXISTS but carries no verifiable CHECKOUT -> ambiguous
        # pod identity; refuse rather than risk acting on a foreign pod.
        return (
            f"pod {name!r} has a pin file without a verifiable CHECKOUT — "
            "refusing pod operation (ambiguous pod identity)"
        )
    try:
        if Path(pinned).resolve() != Path(target["path"]).resolve():
            return (
                f"pod {name!r} is pinned to a different checkout — refusing "
                "cross-repository pod operation (basename collision)"
            )
    except OSError as exc:
        return f"cannot resolve checkout paths for pod guard: {runtime._redact(str(exc))}"
    return None


def _reclaim_pod_locked(cfg, name: str, expected_checkout: str) -> tuple[str, str]:
    """Attribute pod *name* and tear it down as ONE locked transaction.

    Runs entirely inside ``rt.pod_name_mutex``, which is the cross-process flock
    every mutating pod path cooperates on. That is the whole point of this
    helper: checking the checkout pin in one process and then tearing down in
    another leaves a window where a concurrent ``pod up`` from a DIFFERENT
    checkout claims the same global basename, so the teardown would stop that
    pod and delete its isolated HOME. Reading the pin under the same lock the
    teardown holds closes it.

    Necessarily in-process rather than via the ``pod down`` CLI: the lock is held
    per open-file-description and ``stop_pod`` re-acquires it, so a caller that
    held it around a shell-out would block the very child it waits on.
    ``pod_name_mutex`` is reentrant WITHIN A THREAD, and this function is
    submitted to the executor as one callable, so ``stop_pod``'s own acquisition
    nests instead of deadlocking.

    *expected_checkout* is this repo's worktree path for *name*, resolved by the
    caller before the lock -- it is not the racy half, since a foreign ``up``
    moves the PIN, never our own worktree.

    Mirrors :func:`_pod_checkout_guard`'s attribution rules, and the CLI's
    post-teardown env-file clear, so behaviour matches the paths it replaces.

    Returns ``(outcome, detail)`` with outcome one of:
      ``reclaimed``  -- ours, torn down, HOME verified gone
      ``handed_over`` -- a new pod claimed the name mid-teardown; its state (and
                        its pin) is deliberately left untouched. Callers treat
                        this as a REFUSAL, not a success: a live pod may now be
                        running out of the very checkout they are about to
                        delete, and which pod holds the name is unknowable here.
      ``foreign``    -- not attributable to this checkout; nothing was touched
      ``failed``     -- attribution or teardown could not be completed
    """
    with runtime.rt.pod_name_mutex(cfg, name):
        try:
            env_exists, pinned = _read_pin_strict(cfg, name)
        except Exception as exc:  # noqa: BLE001
            # Pin state exists but cannot be positively read -> never treat as
            # "unpinned"; that ambiguity is the cross-repo hole itself.
            return "failed", f"cannot verify pod checkout pin: {runtime._redact(str(exc))}"
        if not env_exists:
            # No pin means the HOME cannot be attributed to ANY checkout, and
            # this path deletes it. ``_pod_checkout_guard`` allows an unpinned
            # name when no unit is live, which is right for OPERATING on a pod
            # the caller located; deletion is the stricter case and demands
            # POSITIVE attribution, because a same-basename leftover from
            # another checkout looks identical from here. Refusing costs an
            # unpinned orphan an automatic reclaim -- `pod prune` still takes
            # it -- while allowing it risks deleting another repo's data.
            return "foreign", (
                f"pod {name!r} has no checkout pin, so its HOME cannot be "
                "attributed to this checkout (unattributable pod identity)"
            )
        if not pinned:
            return "foreign", (
                f"pod {name!r} has a pin file without a verifiable CHECKOUT "
                "(ambiguous pod identity)"
            )
        try:
            mine = Path(pinned).resolve() == Path(expected_checkout).resolve()
        except OSError as exc:
            return "failed", f"cannot resolve checkout paths: {runtime._redact(str(exc))}"
        if not mine:
            return "foreign", (
                f"pod {name!r} is pinned to a different checkout " "(basename collision)"
            )
        cp = runtime.rt.stop_pod(cfg, name)
        if cp.returncode != 0:
            # Redacted like every other detail this helper returns: it reaches the
            # worktree-remove response and therefore the dashboard, and teardown
            # stderr can carry a path or credential from the pod's own output.
            err = runtime._redact((cp.stderr or "").strip())
            return "failed", err or f"stop rc={cp.returncode}"
        if runtime.rt.RECLAIMED_MARKER in (cp.stdout or ""):
            # The env file now pins the NEW pod's checkout -- clearing it would
            # strip that pod's identity.
            return "handed_over", f"pod {name!r} was reclaimed by a new pod mid-teardown"
        # Clear the pinned CHECKOUT=/SEED= so a later `up` re-resolves cleanly,
        # the same post-reclaim step the CLI performs.
        cfg.env_file(name).unlink(missing_ok=True)
        return "reclaimed", ""


async def _pod_up(name: str) -> dict:
    guard = await _pod_checkout_guard(name)
    if guard:
        return {"ok": False, "error": guard}
    # Resolve the node toolchain off the loop before building the pod env:
    # `pod up` runs the provision chain (npm ci + vite) when asked to.
    await runtime._warm_build_path()
    cmd = runtime._find_cli() + ["pod", "up", name, "--json"]
    rc, stdout, stderr = await runtime._run_cmd(
        cmd, cwd=repository._repo(), env=_pod_env(), timeout=180
    )
    if rc != 0:
        return {"ok": False, "error": runtime._redact(stderr or stdout)}
    # Post-start verification (symmetry with _pod_down): rc==0 is not proof the
    # pod is up. Confirm the unit is actually active, else fail closed rather
    # than flash a false "started" — the same false-success class as a false
    # "stopped", in the opposite direction.
    cfg = runtime._load_cfg()
    if runtime._POD_AVAILABLE and cfg:
        try:
            loop = asyncio.get_running_loop()
            active = await loop.run_in_executor(subprocess_executor(), runtime.rt.active_names, cfg)
            if name not in active:
                return {"ok": False, "error": "pod not active after start"}
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "error": f"cannot verify pod start: {runtime._redact(str(exc))}",
            }
    try:
        return {"ok": True, **json.loads(stdout)}
    except ValueError:
        return {"ok": True, "output": stdout}


async def _pod_down(name: str) -> dict:
    guard = await _pod_checkout_guard(name)
    if guard:
        return {"ok": False, "error": guard}
    await runtime._warm_build_path()
    cmd = runtime._find_cli() + ["pod", "down", name]
    rc, stdout, stderr = await runtime._run_cmd(
        cmd, cwd=repository._repo(), env=_pod_env(), timeout=30
    )
    if rc != 0:
        return {"ok": False, "error": runtime._redact(stderr or stdout)}
    # Post-stop verification: a CLI exit 0 is NOT proof the unit stopped (a
    # broken `-m` entry point can no-op with rc 0, and a real stop
    # can still fail or time out). Re-check the live unit state and fail CLOSED
    # if the pod is still active — mirrors the post-shutdown recheck in
    # _worktree_remove so "Stopped" is never reported for a pod still running.
    cfg = runtime._load_cfg()
    if runtime._POD_AVAILABLE and cfg:
        try:
            loop = asyncio.get_running_loop()
            active = await loop.run_in_executor(subprocess_executor(), runtime.rt.active_names, cfg)
            if name in active:
                return {"ok": False, "error": "pod still active after shutdown"}
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "error": f"cannot verify pod shutdown: {runtime._redact(str(exc))}",
            }
    return {"ok": True, "error": None}


async def _pod_restart(name: str) -> dict:
    """Restart a pod: down, then up only after a successful shutdown."""
    r = await _pod_down(name)
    if not r.get("ok"):
        return {"ok": False, "error": f"pod shutdown failed: {r.get('error')}"}
    return await _pod_up(name)


async def _pod_token(name: str) -> dict:
    guard = await _pod_checkout_guard(name)
    if guard:
        return {"ok": False, "error": guard}
    cfg = runtime._load_cfg()
    if cfg is None:
        return {"ok": False, "error": "PodConfig unavailable"}
    try:
        loop = asyncio.get_running_loop()
        token = await loop.run_in_executor(
            subprocess_executor(), runtime.rt.mint_token, cfg, name, "2h"
        )
        port = await loop.run_in_executor(subprocess_executor(), runtime.rt.derive_port, cfg, name)
        return {"ok": True, "token": token, "url": f"http://127.0.0.1:{port}/?token={token}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


async def _pod_logs(name: str, n: int = 120) -> dict:
    guard = await _pod_checkout_guard(name)
    if guard:
        return {"ok": False, "error": guard}
    cfg = runtime._load_cfg()
    if cfg is None:
        return {"ok": False, "error": "PodConfig unavailable"}
    loop = asyncio.get_running_loop()
    raw = await loop.run_in_executor(subprocess_executor(), runtime.rt.recent_journal, cfg, name, n)
    return {"ok": True, "logs": runtime._redact(raw)}


async def _pod_provision(name: str) -> dict:
    guard = await _pod_checkout_guard(name)
    if guard:
        return {"ok": False, "error": guard}
    # Check, start, and record under ONE lock — releasing between the check
    # and the record lets two queued requests both observe "no active run".
    async with fleet_state._PROVISION_LOCK:
        prev = fleet_state._PROVISION_INFLIGHT.get(name)
        if prev:
            async with runtime._RUNS_LOCK:
                running = runtime._RUNS.get(prev, {}).get("status") == "running"
            if running:
                return {"ok": False, "error": "provision already running", "run_id": prev}
        await runtime._warm_build_path()
        p_argv, p_env, p_cleanup = await shielded_prepare_off_loop(
            functools.partial(
                sandboxed_spawn_argv,
                runtime._find_cli() + ["pod", "provision", name],
                "strict",
                env=_pod_env(),
            ),
            executor=subprocess_executor(),
        )
        rid = await runtime._start_run(
            "provision " + name,
            p_argv,
            cwd=repository._repo(),
            env=p_env,
            cleanup_paths=[p_cleanup] if p_cleanup else None,
        )
        fleet_state._PROVISION_INFLIGHT[name] = rid
    return {"ok": True, "run_id": rid}


async def _pod_provision_dismiss(name: str, run_id: str) -> dict:
    """Forget one terminal provision run without racing a replacement run."""
    async with fleet_state._PROVISION_LOCK:
        if fleet_state._PROVISION_INFLIGHT.get(name) != run_id:
            return {"ok": True, "dismissed": False}
        async with runtime._RUNS_LOCK:
            if runtime._RUNS.get(run_id, {}).get("status") == "running":
                return {"ok": False, "error": "cannot dismiss a running provision"}
        fleet_state._PROVISION_INFLIGHT.pop(name, None)
    return {"ok": True, "dismissed": True}


# --- worktree remove ---
async def _worktree_remove(
    name: str,
    force: bool = False,
    progress: Callable[[str], None] | None = None,
    _caller: str = "handler",
    discard_untracked_paths: list[str] | None = None,
) -> dict:
    """Remove a feature worktree without racing its rebase lifecycle.

    The unlocked check and acquisition are adjacent with no intervening await.
    On asyncio's single event loop, acquiring a free lock completes without
    yielding, so a rebase cannot enter between the fail-fast check and removal.
    """
    worktree_lock = _wt_lock(name)
    if worktree_lock.locked():
        return {
            "ok": False,
            "error": (
                "refusing: a rebase is in progress for this worktree -- "
                "wait for it to finish or abort it first"
            ),
        }
    async with worktree_lock:
        return await _worktree_remove_locked(
            name, force, progress, _caller, discard_untracked_paths
        )


async def _worktree_remove_locked(
    name: str,
    force: bool = False,
    progress: Callable[[str], None] | None = None,
    _caller: str = "handler",
    discard_untracked_paths: list[str] | None = None,
) -> dict:
    """Remove a feature worktree while its per-worktree lock is held.

    Non-forced removal of merged PRs uses a SQUASH-SAFE race guard: fetches
    the PR's headRefOid via `gh` and requires the worktree branch's current
    OID == the PR's merged headRefOid. Commits pushed after merge cause OID
    divergence and refuse the removal (unlike git cherry which never works
    for squash merges).

    ``discard_untracked_paths`` is a NARROWER request than ``force``, not a
    synonym: it authorizes destroying exactly the untracked files the caller
    LISTED (session scratch -- probe scripts, capture harnesses, notes) and
    nothing else, so it is honoured only while NO tracked file is modified AND
    the listed set still matches what is on disk. Carrying the set rather than a
    boolean is what makes "you consented to what you were shown" enforceable
    instead of merely asserted. It does not speak to whether the branch's
    commits are shipped, so an unmerged branch still needs ``force`` in
    addition. Both together are still refused if a tracked file is modified.

    Lock order (must never be reversed to prevent deadlock):
      _wt_lock(name)  →  _MAKE_LIVE_LOCK  →  _GIT_MUTATION_LOCK
    The wrapper owns _wt_lock(name) for this entire function. The make-live
    lock is held from the protection re-check through destructive deletion,
    so neither rebase nor a live cutover can claim the target concurrently.
    """
    target, err = await repository._find_worktree(name)
    if target is None:
        return {"ok": False, "error": err}
    # _find_worktree ran discovery, so the checkout is resolved from here on;
    # the accessor keeps the removal git calls off the bare global.
    repo = repository._repo()
    if target.get("is_main"):
        return {"ok": False, "error": "refusing: cannot remove the main checkout"}
    path = target["path"]
    branch = target.get("branch")

    live_path = await live._live_worktree_path(fresh=True)
    if live_path is not None and repository._same_path(path, live_path):
        return {
            "ok": False,
            "error": (
                "refusing: this worktree is running the live gateway -- "
                "switch the gateway to another checkout first"
            ),
        }
    # The systemd unit only covers service-managed gateways. A gateway (and
    # this backend, its subprocess) launched directly from a feature worktree
    # is invisible to it -- so also refuse when the target IS the checkout our
    # own running code was imported from.
    own_checkout = live._own_checkout_path()
    if own_checkout is not None and repository._same_path(path, own_checkout):
        return {
            "ok": False,
            "error": (
                "refusing: this worktree is the checkout the current gateway "
                "process is running from -- switch checkouts first"
            ),
        }

    # A locked worktree is one git will refuse to remove, and it refuses at the
    # END -- after any pre-removal cleanup has run. Recognising the lock here,
    # before a discard deletes anything, is what keeps a doomed request from
    # taking the scratch with it. Checked for every removal, not just discards:
    # the alternative is git's raw stderr arriving after the fact.
    if target.get("locked"):
        return {
            "ok": False,
            "error": (
                "refusing: this worktree is locked ("
                + runtime._redact(str(target["locked"]))
                + ") -- unlock it with `git worktree unlock` first"
            ),
        }

    # Approve an untracked-only discard, but do NOT execute it here. Deleting
    # now would let a gate further down refuse the removal AFTER the files were
    # destroyed -- a refused request that still took something away. The
    # approved list is carried to the one point where the removal is certain
    # (just before `git worktree remove`) and executed there.
    #
    # The caller submits the EXACT list it displayed, and it must still equal the
    # set on disk. Re-enumerating and trusting the fresh result would destroy a
    # file created between the moment the user was shown the list and this
    # check -- consent for a set the user never saw. On any mismatch the removal
    # is refused and the caller has to look again.
    #
    # The comparison only happens when ``_redact`` is the IDENTITY on every
    # fresh path, so the set the caller echoes back is the set that gets
    # deleted. Redaction is lossy: two distinct filenames can share one redacted
    # rendering, and comparing in that space would let a file swapped in after
    # display satisfy the equality check and be destroyed unapproved. When any
    # path would be altered on the way out we refuse instead of guessing -- the
    # tree is still removable by hand, and no unapproved file can be lost.
    pending_discard: list[str] | None = None
    if discard_untracked_paths is not None:
        _tracked_dirty, _fresh = await repository._dirty_split(path)
        if _tracked_dirty is not False or not _fresh:
            pass  # not untracked-only: the gates below refuse on their own terms
        elif len(_fresh) > repository._DIRTY_PATH_SAMPLE:
            # The caller was handed a truncated list, so it cannot have
            # consented to the whole set. Refuse rather than delete the tail
            # nobody ever saw.
            return {
                "ok": False,
                **repository._dirt_fields(_tracked_dirty, _fresh),
                "error": (
                    f"too many untracked files to confirm individually "
                    f"({len(_fresh)}, listed at most {repository._DIRTY_PATH_SAMPLE}) -- "
                    "clean the worktree manually, then remove it"
                ),
            }
        elif any(runtime._redact(p) != p for p in _fresh):
            return {
                "ok": False,
                **repository._dirt_fields(_tracked_dirty, _fresh),
                "error": (
                    "an untracked filename cannot be confirmed safely (it is "
                    "rewritten when displayed, so a different file could match the "
                    "same confirmation) -- clean the worktree manually, then "
                    "remove it"
                ),
            }
        elif set(discard_untracked_paths) != set(_fresh):
            return {
                "ok": False,
                **repository._dirt_fields(_tracked_dirty, _fresh),
                "error": (
                    "the worktree's untracked files changed since they were listed "
                    "-- nothing was discarded; re-check the file list and retry"
                ),
            }
        else:
            pending_discard = _fresh

    async def _dirty_now() -> bool | None:
        """The dirty state the gates below must decide on.

        With an approved untracked-only discard pending the tree counts as
        clean, because the discard runs unconditionally before the removal --
        no gate is passed on a promise that is not kept. Delegates to
        _real_dirty otherwise, which keeps the gate semantics (and every test
        that stubs _real_dirty) unchanged when no discard was requested.
        """
        if pending_discard is not None:
            return False
        return await repository._real_dirty(path)

    if not force:
        dirty = await _dirty_now()
        if dirty is not False:
            fields, detail = ({}, "") if dirty is None else await repository._dirt_report(path)
            return {
                "ok": False,
                **fields,
                "error": (
                    "worktree has uncommitted changes"
                    " (force is allowed only when the PR is merged)"
                    if dirty
                    else "cannot verify worktree state (git status failed)"
                )
                + detail,
            }

    _rm_head_oid = (await repository._git(path, "rev-parse", "HEAD")) if branch else None
    pr = (await fleet_state._pr_status_cached(branch, _rm_head_oid)) if branch else None
    own = await repository._own_commits_count(path)

    if not force and not fleet_state._is_pr_merged(pr):
        if own is None or own > 0:
            return {
                "ok": False,
                "error": f"PR not merged (state: {(pr or {}).get('state', 'no PR')})",
                "pr": runtime._redact_pr(pr),
            }

    # Teardown guard: even with force=True, refuse to destroy a dirty worktree
    # whose PR is not merged — that combination means unrecoverable data loss
    # (uncommitted edits on an unmerged branch). Force retains its meaning for
    # merged-dirty and diverged-OID overrides where the work IS already shipped.
    #
    # force_use_git_force tracks whether the removal command should use --force.
    # When the unmerged tree verified CLEAN here, we intentionally omit --force
    # at removal time so git's own dirty check acts as the atomic last-line
    # guard against edits made in the window between this check and the actual
    # removal (TOCTOU mitigation).
    force_use_git_force = force  # default: honour caller's force flag
    if force and not fleet_state._is_pr_merged(pr):
        dirty = await _dirty_now()
        if dirty is True:
            fields, detail = await repository._dirt_report(path)
            runtime.logger.info(
                "worktree_removal_audit: worktree=%s branch=%s caller=%s force=%s "
                "dirty=True tracked_dirty=%s untracked=%s own=%s pr_state=%s "
                "verdict_oid=n/a action=refused_dirty_unmerged",
                name,
                branch,
                _caller,
                force,
                fields.get("dirty_tracked"),
                fields.get("dirty_untracked"),
                own,
                (pr or {}).get("state", "none"),
            )
            return {
                "ok": False,
                **fields,
                "error": (
                    "refusing forced removal: worktree has uncommitted changes "
                    "and PR is not merged — this would cause unrecoverable data "
                    f"loss (PR state: {(pr or {}).get('state', 'no PR')})"
                )
                + detail,
                "pr": runtime._redact_pr(pr),
            }
        elif dirty is None:
            runtime.logger.info(
                "worktree_removal_audit: worktree=%s branch=%s caller=%s force=%s "
                "dirty=unknown own=%s pr_state=%s verdict_oid=n/a "
                "action=refused_unverifiable",
                name,
                branch,
                _caller,
                force,
                own,
                (pr or {}).get("state", "none"),
            )
            return {
                "ok": False,
                "error": (
                    "cannot verify worktree cleanliness (git status failed) — "
                    "refusing forced removal"
                ),
                "pr": runtime._redact_pr(pr),
            }
        # Tree verified clean + unmerged: force override allowed, but do NOT
        # pass --force to git so a late dirty edit is caught at removal time.
        force_use_git_force = False
        runtime.logger.info(
            "worktree_removal_audit: worktree=%s branch=%s caller=%s "
            "force=%s dirty=False own=%s pr_state=%s "
            "action=unmerged_clean_no_git_force",
            name,
            branch,
            _caller,
            force,
            own,
            (pr or {}).get("state", "none"),
        )

    # Pin the branch ref NOW — the same OID the safety verdict below evaluates
    # is the expected-old-OID for the atomic delete. A commit landing at any
    # point after this pin moves the ref, update-ref -d fails, branch retained.
    # Hoisted before the fresh-MERGED gate so containment uses the SAME pinned
    # OID (closing a two-reads inconsistency where the first could fail while
    # the second succeeds on retry).
    verdict_oid = (
        (await repository._git(repo, "rev-parse", f"refs/heads/{branch}")) if branch else None
    )
    if branch and branch != repository.BASE_BRANCH and not verdict_oid:
        runtime.logger.info(
            "worktree_removal_audit: worktree=%s branch=%s caller=%s force=%s "
            "dirty=n/a own=%s pr_state=%s verdict_oid=None "
            "action=refused_unpinnable",
            name,
            branch,
            _caller,
            force,
            own,
            (pr or {}).get("state", "none"),
        )
        return {
            "ok": False,
            "error": ("cannot pin branch OID (git rev-parse failed) — refusing removal"),
        }

    # Fresh-MERGED gate: when force=True and the CACHED verdict says MERGED,
    # confirm with a live gh query before allowing destruction of a dirty or
    # unverifiable worktree. A permanently cached MERGED verdict (reused branch
    # name whose old PR merged) would otherwise skip the unmerged guard above.
    #
    # CRITICAL: even when fresh verification CONFIRMS the PR is merged,
    # a dirty or unverifiable worktree must NOT be removed with --force.
    # Containment proves the branch's COMMITS are shipped; it says nothing
    # about working-tree edits. `git worktree remove --force` bypasses git's
    # own dirty check and would irrecoverably destroy uncommitted edits.
    # Contract: NO path from this gate ever passes --force to git:
    #   - dirty is not False → refuse outright (round 5)
    #   - dirty is False → drop --force so git's own dirty check is the
    #     atomic last line against edits arriving in the check-to-removal
    #     window (round 6, mirrors the unmerged-clean TOCTOU pattern)
    if force and fleet_state._is_pr_merged(pr) and branch:
        dirty = await _dirty_now()
        if dirty is not False:
            fresh_head = await fleet_state._fetch_pr_head_oid(branch, repo=(pr or {}).get("_repo"))
            if fresh_head is None:
                runtime.logger.info(
                    "worktree_removal_audit: worktree=%s branch=%s caller=%s "
                    "force=%s dirty=%s own=%s pr_state=MERGED(cached) "
                    "fresh_merged=False verdict_oid=%s "
                    "action=refused_stale_merged",
                    name,
                    branch,
                    _caller,
                    force,
                    "unknown" if dirty is None else "True",
                    own,
                    (verdict_oid or "").strip()[:12] if verdict_oid else "none",
                )
                return {
                    "ok": False,
                    "error": (
                        "refusing forced removal: cached PR state is MERGED but "
                        "fresh verification failed (stale cache or reused branch "
                        "name) — cannot confirm work is shipped"
                    ),
                    "pr": runtime._redact_pr(pr),
                }
            # Containment check: the branch's pinned OID must be contained in
            # the freshly verified PR head. A reused branch name whose OLD PR
            # merged returns a valid fresh_head, but the current branch OID
            # (carrying new unmerged commits) won't be contained in it.
            # Uses the single verdict_oid pin (hoisted above) — no second
            # rev-parse that could diverge from the first.
            assert verdict_oid  # guaranteed by the refused_unpinnable guard above
            if not await fleet_state._head_contained_in_pr(
                repo, verdict_oid.strip(), fresh_head.strip()
            ):
                runtime.logger.info(
                    "worktree_removal_audit: worktree=%s branch=%s caller=%s "
                    "force=%s dirty=%s own=%s pr_state=MERGED(cached) "
                    "fresh_head=%s verdict_oid=%s "
                    "action=refused_uncontained_fresh_head",
                    name,
                    branch,
                    _caller,
                    force,
                    "unknown" if dirty is None else "True",
                    own,
                    fresh_head.strip()[:12],
                    verdict_oid.strip()[:12],
                )
                return {
                    "ok": False,
                    "error": (
                        "refusing forced removal: branch has commits not "
                        "contained in the verified PR head (possible reused "
                        "branch name with new unmerged work)"
                    ),
                    "pr": runtime._redact_pr(pr),
                }
            # Fresh verification confirms the PR is merged, but the worktree
            # has uncommitted edits (dirty=True) or unverifiable state
            # (dirty=None). Refuse: --force would bypass git's dirty check
            # and destroy working-tree edits that containment cannot vouch for.
            action = "refused_dirty_merged" if dirty is True else "refused_unverifiable_merged"
            fields, detail = ({}, "") if dirty is None else await repository._dirt_report(path)
            runtime.logger.info(
                "worktree_removal_audit: worktree=%s branch=%s caller=%s "
                "force=%s dirty=%s own=%s pr_state=MERGED(fresh) "
                "fresh_merged=True verdict_oid=%s "
                "action=%s",
                name,
                branch,
                _caller,
                force,
                "unknown" if dirty is None else "True",
                own,
                (verdict_oid or "").strip()[:12] if verdict_oid else "none",
                action,
            )
            return {
                "ok": False,
                **fields,
                "error": (
                    "refusing forced removal: PR is merged but worktree has "
                    + (
                        "uncommitted changes"
                        if dirty is True
                        else "unverifiable state (git status failed)"
                    )
                    + " — commit, stash, or clean the working tree first"
                )
                + detail,
                "pr": runtime._redact_pr(pr),
            }
        else:
            # dirty is False: tree is clean NOW, but an editor save during the
            # check-to-removal window could dirty it. Drop --force so git's own
            # dirty check is the atomic last line of defence (mirrors the
            # unmerged-clean TOCTOU pattern). No path from the fresh-MERGED gate
            # ever passes --force to git.
            force_use_git_force = False
            runtime.logger.info(
                "worktree_removal_audit: worktree=%s branch=%s caller=%s "
                "force=%s dirty=False own=%s pr_state=MERGED(cached) "
                "action=merged_clean_no_git_force",
                name,
                branch,
                _caller,
                force,
                own,
            )

    # Squash-safe race guard: for merged PRs, verify the branch tip matches
    # the PR's merged headRefOid. A commit pushed after merge moves the OID.
    # The pr_head_oid is reused later in the ref-delete gate for squash-safe
    # containment — hoist to this scope so both code paths see it.
    pr_head_oid: str | None = None
    if not force and fleet_state._is_pr_merged(pr) and branch:
        branch_oid = verdict_oid
        if branch_oid is None:
            return {
                "ok": False,
                "error": (
                    "cannot verify branch OID (git rev-parse failed) — "
                    "refusing non-forced removal; retry or use force"
                ),
            }
        pr_head_oid = await fleet_state._fetch_pr_head_oid(branch, repo=(pr or {}).get("_repo"))
        if pr_head_oid is None:
            return {
                "ok": False,
                "error": (
                    "cannot verify PR head OID (gh query failed) — "
                    "refusing non-forced removal; retry or use force"
                ),
            }
        if not await fleet_state._head_contained_in_pr(path, branch_oid, pr_head_oid):
            return {
                "ok": False,
                "error": (
                    "branch has commits after merge (OID diverged from PR head) — "
                    "refusing non-forced removal; use force to override"
                ),
                "pr": runtime._redact_pr(pr),
            }

    # Hold _MAKE_LIVE_LOCK from the protection re-check through deletion.
    # A concurrent /make-live can stage this worktree between the
    # eager live-path check above and ``git worktree remove``; every removal
    # caller delegates this ownership to the same internal critical section.
    async with live._MAKE_LIVE_LOCK:
        # Protection re-check under the lock closes the TOCTOU window between
        # the eager checks at function entry and the actual deletion.
        _live2 = await live._live_worktree_path(fresh=True)
        if _live2 is not None and repository._same_path(path, _live2):
            return {
                "ok": False,
                "error": (
                    "refusing: this worktree became the live gateway -- "
                    "switch the gateway to another checkout first"
                ),
            }
        _staged2 = live._staged_target()
        if _staged2 is not None and repository._same_path(path, _staged2):
            return {
                "ok": False,
                "error": (
                    "refusing: this worktree is a staged live-gateway cutover "
                    "target -- cancel the staged cutover before removing"
                ),
            }

        # stop pod if running
        # Verification (dirty/PR/OID guards above) is the "verifying" phase;
        # from here we enter pod shutdown, then the serialized git mutation.
        # These phase signals drive the per-item prune checklist (no-op for
        # other callers).
        if progress is not None:
            progress("stopping_pod")
        cfg = runtime._load_cfg()
        stopped_pod = False
        # Distinct from ``stopped_pod``: nothing was running, an already-stopped
        # pod's isolated HOME was reclaimed. Conflating the two would report a
        # shutdown that never happened.
        reclaimed_pod_home = False
        if runtime._POD_AVAILABLE and cfg is None:
            return {"ok": False, "error": "cannot load pod configuration to verify pod state"}
        if runtime._POD_AVAILABLE and cfg:
            # Pre-gate: verify the pod backend is reachable. If absent
            # (PodBackendAbsent), pods cannot be supervised — a systemd --user
            # unit requires a reachable session bus for its lifecycle. Even if
            # the socket were removed under a running pod, that pod is now
            # uncontrollable and will terminate on its next watchdog cycle.
            # As a defense-in-depth measure, we also probe the unit file directly.
            try:
                runtime.rt.require_backend()
            except runtime.rt.PodBackendAbsent:
                # Defense-in-depth: attempt a direct unit-state query. If this
                # somehow succeeds (bus re-appeared between require_backend and
                # here), we fall through to the normal active-names path.
                try:
                    loop = asyncio.get_running_loop()
                    unit_state = await loop.run_in_executor(
                        subprocess_executor(), runtime.rt.unit_state, cfg, name
                    )
                    if unit_state[0] == "active":
                        return {
                            "ok": False,
                            "error": "pod backend reported absent but unit is active — refusing",
                        }
                except Exception:
                    pass  # unit_state also fails → backend truly gone
                # Name the residue instead of hiding the skip at debug level. The
                # HOME cannot be reclaimed here — without a backend the pod's
                # liveness is unprovable, and deleting a HOME that may belong to a
                # live gateway is the one outcome teardown must never risk — but an
                # operator who is told the path can reclaim it with `pod down`.
                # Resolving the path is itself best-effort: a diagnostic must never
                # be the reason a removal fails.
                try:
                    residue: object = runtime.rt.pod_home(cfg, name)
                except Exception:  # noqa: BLE001
                    residue = "its isolated HOME under the pod root"
                runtime.logger.warning(
                    "dev-fleet worktree_remove: pod backend absent, so %r's pod state "
                    "cannot be verified and %s is left in place; reclaim it with "
                    "`kirocrew pod down %s` once the backend is back",
                    name,
                    residue,
                    name,
                )
            else:
                try:
                    loop = asyncio.get_running_loop()
                    active = await loop.run_in_executor(
                        subprocess_executor(), runtime.rt.active_names, cfg
                    )
                    if name in active:
                        outcome, detail = await loop.run_in_executor(
                            subprocess_executor(), _reclaim_pod_locked, cfg, name, path
                        )
                        if outcome == "foreign":
                            return {"ok": False, "error": f"refusing pod shutdown: {detail}"}
                        if outcome == "handed_over":
                            # A new pod holds this name. Which checkout it belongs to
                            # is unknowable from here, and it may be running out of
                            # THIS worktree -- removing the files under a live pod is
                            # exactly what the liveness gate exists to prevent, and
                            # the post-stop recheck below cannot be relied on to see
                            # a unit that is still bootstrapping.
                            return {"ok": False, "error": f"refusing removal: {detail}"}
                        if outcome == "failed":
                            return {"ok": False, "error": f"pod shutdown failed: {detail}"}
                        stopped_pod = True
                        # ``reclaimed_pod_home`` deliberately stays False here: the
                        # teardown did reclaim the HOME, but the flag's job is to
                        # distinguish a leftover reclaimed with NOTHING running from a
                        # real shutdown, and ``stopped_pod`` already reports this one.
                        try:
                            active2 = await loop.run_in_executor(
                                subprocess_executor(), runtime.rt.active_names, cfg
                            )
                            if name in active2:
                                return {"ok": False, "error": "pod still active after shutdown"}
                        except Exception as exc:
                            return {
                                "ok": False,
                                "error": f"cannot verify pod shutdown: {runtime._redact(str(exc))}",
                            }
                    else:
                        # A STOPPED pod still owns its isolated HOME, and removing the
                        # worktree is the last moment anything can attribute that
                        # directory to this checkout: afterwards the pin naming it is
                        # gone and only a bulk sweep could find it. Real usage stops
                        # the pod when testing ends and prunes days later once the PR
                        # merges, so gating reclamation on a LIVE unit meant the common
                        # path never reclaimed anything — each removal stranded a full
                        # isolated HOME (a per-instance model copy dominates it) for
                        # good.
                        #
                        # ``orphan_homes`` is the authoritative predicate rather than a
                        # bare directory probe, so this agrees with `pod ls` / `pod
                        # prune` by construction: it skips symlinks, and on macOS it
                        # treats a per-pod plist as "installed, not orphaned" so a name
                        # mid-``up`` is never reclaimed underneath itself. It keys on
                        # the pod root, liveness and plist and never on the checkout
                        # pin, so attribution is the locked helper's job, not its.
                        #
                        # Two different fail directions, so two different scopes. The
                        # ENUMERATION is best-effort cleanup: an orphan scan says
                        # nothing about liveness, so its failure degrades to a named
                        # leftover rather than turning a lost directory into a lost
                        # removal. The RECLAIM is teardown, so it fails CLOSED -- a
                        # returned failure refuses the removal, and a raised one is
                        # deliberately left to the liveness handler below rather than
                        # swallowed here, because a teardown that died mid-flight
                        # (a stop that timed out against a still-activating unit) is
                        # exactly the state in which removing the checkout is unsafe.
                        try:
                            # Probe the pod root FIRST: ``orphan_homes`` swallows an
                            # enumeration OSError and answers ``[]``, which is
                            # indistinguishable from "nothing to reclaim" -- so an
                            # unreadable pod root would silently skip the HOME without
                            # the warning this block promises. Reading it here puts the
                            # error on a path that reaches that warning.
                            await loop.run_in_executor(
                                subprocess_executor(), lambda: list(cfg.pod_root.iterdir())
                            )
                            orphans = await loop.run_in_executor(
                                subprocess_executor(), runtime.rt.orphan_homes, cfg
                            )
                        except Exception as exc:  # noqa: BLE001
                            runtime.logger.warning(
                                "dev-fleet worktree_remove: could not look for %r's pod "
                                "HOME (%s); the worktree is still removed — sweep the "
                                "leftover with `kirocrew pod prune`",
                                name,
                                runtime._redact(str(exc)),
                            )
                            orphans = []
                        if name in orphans:
                            outcome, detail = await loop.run_in_executor(
                                subprocess_executor(), _reclaim_pod_locked, cfg, name, path
                            )
                            if outcome == "reclaimed":
                                reclaimed_pod_home = True
                            elif outcome == "foreign":
                                # Not ours to delete, which is a reason to leave it --
                                # never a reason to refuse this checkout's own removal,
                                # since nothing of ours is at risk.
                                runtime.logger.warning(
                                    "dev-fleet worktree_remove: left a pod HOME named "
                                    "%r in place (%s); continuing the removal",
                                    name,
                                    runtime._redact(detail),
                                )
                            else:
                                # failed, or handed_over -- a new pod now holds this
                                # name and may be running out of this worktree.
                                return {
                                    "ok": False,
                                    "error": f"pod home reclaim failed: {detail}",
                                }
                except Exception as exc:
                    return {
                        "ok": False,
                        "error": f"cannot verify pod state: {runtime._redact(str(exc))}",
                    }

        if progress is not None:
            progress("removing")
        # Serialize the destructive git mutations. Concurrent `git worktree remove`
        # / `update-ref -d` against the shared MAIN_REPO would race on the worktree
        # admin dir and packed-refs locks, so only one worker mutates at a time.
        async with _GIT_MUTATION_LOCK:
            # TOCTOU recheck: the pod-inactive verification above happened BEFORE
            # this lock was acquired. Under parallel prune a worker can queue here
            # behind other removals — long enough for another session to restart
            # the pod. Removing the checkout under a live pod would leave its
            # gateway running from deleted files, so re-verify inactivity now.
            if runtime._POD_AVAILABLE and cfg:
                try:
                    runtime.rt.require_backend()
                except runtime.rt.PodBackendAbsent:
                    pass  # backend provably absent — no pods can exist
                else:
                    try:
                        loop = asyncio.get_running_loop()
                        active3 = await loop.run_in_executor(
                            subprocess_executor(), runtime.rt.active_names, cfg
                        )
                        if name in active3:
                            return {
                                "ok": False,
                                "error": ("pod became active again before removal — refusing"),
                            }
                    except Exception as exc:
                        return {
                            "ok": False,
                            "error": f"cannot re-verify pod state before removal: {runtime._redact(str(exc))}",
                        }
            # Execute the approved untracked-only discard. This is the LAST
            # point before the removal, so a gate that refused above never got
            # here and never destroyed anything.
            #
            # Deletion is per-file `os.unlink`, not `git clean`: it removes only
            # the enumerated names, cannot recurse into a path whose type changed
            # since consent, and has no pathspec for a filename to hide magic in.
            # See _discard_untracked_files for why git clean is unsuitable.
            #
            # Ignored paths are untouched because they were never enumerated --
            # `_dirty_split` excludes them, so node_modules and .venv are outside
            # this list by construction rather than by a flag.
            #
            # The tree must then verify CLEAN before the removal runs WITHOUT
            # --force, so git's own dirty check stays the atomic last line
            # against a tracked edit that landed in the meantime -- the same
            # TOCTOU contract every other path here honours.
            if pending_discard is not None:
                if force_use_git_force:  # pragma: no cover - defensive
                    return {
                        "ok": False,
                        "error": (
                            "internal: refusing to combine an untracked discard "
                            "with a forced git removal"
                        ),
                    }
                loop = asyncio.get_running_loop()
                discard_err = await loop.run_in_executor(
                    subprocess_executor(),
                    repository._discard_untracked_files,
                    path,
                    pending_discard,
                )
                post_tracked, post_untracked = await repository._dirty_split(path)
                if discard_err or post_tracked is not False or post_untracked:
                    runtime.logger.info(
                        "worktree_removal_audit: worktree=%s branch=%s caller=%s "
                        "approved_discard=%s discard_err=%s tracked_after=%s "
                        "untracked_after=%s action=refused_discard_incomplete",
                        name,
                        branch,
                        _caller,
                        len(pending_discard),
                        bool(discard_err),
                        post_tracked,
                        len(post_untracked),
                    )
                    return {
                        "ok": False,
                        "error": (
                            discard_err
                            or (
                                "could not discard the worktree's untracked files "
                                "(the tree is not clean afterwards -- a file may "
                                "have appeared after the discard was approved)"
                            )
                        )
                        + " -- removal aborted",
                    }
                runtime.logger.info(
                    "worktree_removal_audit: worktree=%s branch=%s caller=%s "
                    "discarded_untracked=%s action=discarded_untracked",
                    name,
                    branch,
                    _caller,
                    len(pending_discard),
                )

            cmd = ["git", "-C", repo, "worktree", "remove", path]
            if force_use_git_force:
                cmd.append("--force")
            # Run the destructive mutation uninterruptibly. On gateway
            # shutdown dev_fleet_cleanup cancels the prune worker; a naive
            # cancel would either SIGKILL the child (via _run_cmd's handler) or,
            # with a bare shield, unwind this frame and release
            # _GIT_MUTATION_LOCK / _MAKE_LIVE_LOCK while the detached child is
            # still writing. _run_uninterruptible holds this frame -- and the
            # locks -- until the timeout-bounded `git worktree remove` has
            # finished, then lets the cancellation propagate at that safe point.
            rc, stdout, stderr = await runtime._run_uninterruptible(
                runtime._run_cmd(cmd, timeout=60)
            )
            if rc != 0:
                # When the removal runs without --force (TOCTOU guard for
                # clean-unmerged override), a git refusal means the tree became
                # dirty in the window — surface it as a specific audit event.
                if force and not force_use_git_force:
                    runtime.logger.info(
                        "worktree_removal_audit: worktree=%s branch=%s caller=%s "
                        "force=%s dirty_at_removal=True verdict_oid=%s "
                        "action=refused_dirty_at_removal",
                        name,
                        branch,
                        _caller,
                        force,
                        (verdict_oid or "").strip()[:12] if verdict_oid else "none",
                    )
                # Redact the FULL text, then bound: slicing first can cut a
                # credential into fragments no redaction regex matches.
                _err = runtime._redact((stderr or stdout).strip())[:300]
                if pending_discard is not None:
                    # The discard already ran. Every failure mode we can name in
                    # advance is refused earlier (lock, protection, dirt), but a
                    # removal can still fail for reasons we cannot enumerate --
                    # a permission change, a file held open. Say so, rather than
                    # returning git's bare stderr and letting the caller assume
                    # the request was a no-op when files are in fact gone.
                    _err = (
                        f"discarded {len(pending_discard)} untracked file(s), but "
                        f"then could not remove the worktree: {_err}"
                    )
                    runtime.logger.info(
                        "worktree_removal_audit: worktree=%s branch=%s caller=%s "
                        "discarded_untracked=%s action=removal_failed_after_discard",
                        name,
                        branch,
                        _caller,
                        len(pending_discard),
                    )
                return {"ok": False, "error": _err}
            # Delete branch ref only when the PR is MERGED — atomically against
            # the pinned OID. Unmerged branch refs are always retained, even when
            # own == 0 (every commit already reachable from the upstream base, so
            # no unique commits exist): keying deletion to PR state alone is a
            # deliberately simpler, fail-closed policy (recoverable > irrecoverable).
            # Known cost: removing an unmerged empty worktree leaves refs/heads/
            # <branch> behind, which blocks re-creating a worktree under the same
            # branch name until the ref is deleted manually.
            # Fail-closed ancestry gate: even when the cached PR status says MERGED,
            # verify the branch OID is actually contained in the base branch. A stale
            # or wrong merged verdict cannot delete the only local pointer to unmerged
            # commits — leaving a dangling ref is recoverable; deleting one is not.
            # OR: squash-safe containment — a squash-merged branch head is never an
            # ancestor of the base, but IS contained in the PR head (the squash
            # commit). When ancestry fails, verify containment via _head_contained_in_pr
            # using the pr_head_oid already fetched above (or fresh if needed).
            if branch and branch != repository.BASE_BRANCH and verdict_oid:
                should_delete = False
                if fleet_state._is_pr_merged(pr):
                    remote = await repository._upstream_remote()
                    rc_anc, _, _ = await runtime._run_cmd(
                        [
                            "git",
                            "-C",
                            repo,
                            "merge-base",
                            "--is-ancestor",
                            verdict_oid.strip(),
                            f"{remote}/{repository.BASE_BRANCH}",
                        ],
                        timeout=10,
                    )
                    should_delete = rc_anc == 0
                    # Squash-safe fallback: ancestry fails for squash/rebase merges.
                    # Use the containment check (branch OID is ancestor of PR head).
                    if not should_delete:
                        head_oid = pr_head_oid or await fleet_state._fetch_pr_head_oid(
                            branch, repo=(pr or {}).get("_repo")
                        )
                        if head_oid:
                            should_delete = await fleet_state._head_contained_in_pr(
                                repo, verdict_oid.strip(), head_oid.strip()
                            )
                if should_delete:
                    # Uninterruptible like `worktree remove` above: this ref
                    # delete is the second destructive mutation; hold the frame
                    # (and the git-mutation lock) until it finishes so a
                    # shutdown cancel cannot tear it mid-write and corrupt
                    # packed-refs. A cancel landing BETWEEN the two mutations
                    # lands on the recoverable side (a dangling refs/heads/<branch>,
                    # per the fail-closed policy above), never on a torn write.
                    await runtime._run_uninterruptible(
                        repository._git(
                            repo,
                            "update-ref",
                            "-d",
                            f"refs/heads/{branch}",
                            verdict_oid.strip(),
                            timeout=10,
                        )
                    )

        # Every removal path lands here — the single-worktree handler, each parallel
        # prune worker, and the auto-prune reaper — so this is the one place the
        # cached snapshot has to be told the row is gone.
        runtime.logger.info(
            "worktree_removal_audit: worktree=%s branch=%s caller=%s force=%s "
            "dirty=%s own=%s pr_state=%s verdict_oid=%s action=removed",
            name,
            branch,
            _caller,
            force,
            "unknown",
            own,
            (pr or {}).get("state", "none"),
            (verdict_oid or "").strip()[:12] if verdict_oid else "none",
        )
        fleet_state._fleet_forget(name)
        return {
            "ok": True,
            "removed": True,
            "stopped_pod": stopped_pod,
            "reclaimed_pod_home": reclaimed_pod_home,
            "pr": runtime._redact_pr(pr),
        }


# --- sync (pull + build) ---
_SYNC_RID: str | None = None


async def _sync() -> dict:
    """Pull upstream main + rebuild. Single-flight via _SYNC_LOCK."""
    async with runtime._SYNC_LOCK:
        if _SYNC_RID is not None:
            async with runtime._RUNS_LOCK:
                run = runtime._RUNS.get(_SYNC_RID)
            if run and run["status"] == "running":
                # Guard against a stale "running" status: the worker task may
                # have exited (process reaped) but the status update has not yet
                # landed because the event loop has not yielded back to the
                # worker's finally block.  The correct liveness signal is the
                # subprocess handle: _ACTIVE_RUNS[rid] is (task, proc), and
                # proc.returncode is not None means the process has exited even
                # if the task's finally (cleanup_paths unlinking, status write)
                # has not completed.  task.done() is strictly LATER than the
                # status write, so checking it would miss the exact window.
                active = runtime._ACTIVE_RUNS.get(_SYNC_RID)
                if active is not None:
                    _task, proc = active
                    if proc is None or proc.returncode is None:
                        # Process still running (or not yet spawned) — genuine.
                        return {"ok": False, "error": "sync already running", "run_id": _SYNC_RID}
                    # Process exited but worker hasn't written status yet.
                    # Wait briefly for the task's finally block to land the
                    # status write + cleanup, rather than starting a new sync
                    # while the old worker is still unlinking temp files in the
                    # same worktree.
                    try:
                        await asyncio.wait_for(asyncio.shield(_task), timeout=2.0)
                    except asyncio.TimeoutError:
                        # Worker still cleaning up after 2s — refuse the new
                        # sync to avoid concurrent worktree writes.
                        return {"ok": False, "error": "sync already running", "run_id": _SYNC_RID}
                    except Exception:
                        pass  # task raised during cleanup; safe to proceed
                # Re-read status after giving the worker a chance to land it.
                async with runtime._RUNS_LOCK:
                    run = runtime._RUNS.get(_SYNC_RID)
                if run and run["status"] == "running":
                    # Still stale after the wait — reconcile defensively.
                    run["status"] = "done"
                    run["exit_code"] = run.get("exit_code") or -1
        return await _sync_start_locked()


def _venv_python(repo: str) -> Path | None:
    """Resolve the repo's own venv interpreter cross-platform (POSIX bin/,
    Windows Scripts/). Returns None when the venv is not provisioned."""
    for rel in ("bin/python", "Scripts/python.exe"):
        cand = Path(repo) / ".venv" / rel
        if cand.is_file():
            return cand
    return None


#: Trusted loader for the sync-runner snapshot. A FIXED literal — nothing is
#: ever interpolated into it — so unlike the pre-extraction inline script it
#: carries no program logic beyond verify-and-exec. It exists because a file
#: on disk is mutable between staging and exec while argv is not: a sandboxed
#: same-UID process could rewrite the staged snapshot in that window and have
#: its code run UNSANDBOXED as the runner. The bootstrap travels in argv
#: (immutable once this process exists) together with the snapshot's expected
#: sha256, reads the file ONCE, hashes and compiles the SAME buffer (no
#: re-read between check and use), and refuses on mismatch. The runner's own
#: argv follows and is re-exposed as ``sys.argv``.
_SYNC_RUNNER_BOOTSTRAP = (
    "import hashlib, sys\n"
    "path, digest = sys.argv[1], sys.argv[2]\n"
    "with open(path, 'rb') as fh:\n"
    "    raw = fh.read()\n"
    "if hashlib.sha256(raw).hexdigest() != digest:\n"
    "    print('sync runner: snapshot does not match the staged source '\n"
    "          '(refusing to execute)', flush=True)\n"
    "    sys.exit(1)\n"
    "code = compile(raw, path, 'exec')\n"
    "sys.argv = [path] + sys.argv[3:]\n"
    "exec(code, {'__name__': '__main__', '__file__': path})\n"
)


async def _sync_start_locked() -> dict:
    """Start the sync run. Caller holds _SYNC_LOCK."""
    global _SYNC_RID  # noqa: F824 (assigned below after await)

    try:
        repo = repository._repo()
    except repository.RepoUnavailable as exc:
        # Sync answers a UI action, so the unresolved state degrades to the
        # same {"ok": False} shape every other refusal here uses.
        return {"ok": False, "error": str(exc)}
    await runtime._warm_build_path()
    head = await repository._git(repo, "symbolic-ref", "--short", "HEAD")
    if head is None:
        return {"ok": False, "error": "cannot determine checked-out branch (git failed)"}
    if head.strip() != repository.BASE_BRANCH:
        return {
            "ok": False,
            "error": (
                f"refusing to sync: primary checkout is on {head.strip()!r}, not {repository.BASE_BRANCH!r}"
            ),
        }

    remote = await repository._upstream_remote()

    # CRITICAL: pip must run with the TARGET repo's own venv interpreter.
    # ``sys.executable`` here is the app backend's venv (a feature worktree's)
    # — `pip install -e .` with it would re-point that venv's editable install
    # at MAIN_REPO, hijacking the running gateway's code identity on its next
    # restart (observed live: gateway silently became the main repo's code).
    target_py = _venv_python(repo)
    if target_py is None:
        return {
            "ok": False,
            "error": (
                "main checkout has no .venv — provision it first "
                f"(expected under {Path(repo) / '.venv'})"
            ),
        }
    # Both binary lookups stat the filesystem (`_trusted_bin` walks the trusted
    # dirs; `_toolchain_bin` adds a `shutil.which` over the node bin dirs, which
    # may be NFS-backed). The console-script probe opens files in the target
    # venv, and the origin probe RUNS that interpreter. Resolve them together on
    # the executor so /api/sync cannot stall the gateway's requests and liveness
    # behind a directory scan or a subprocess.
    loop = asyncio.get_running_loop()
    git_bin, npm_bin, locked_scripts, venv_origin = await loop.run_in_executor(
        subprocess_executor(),
        lambda: (
            runtime._trusted_bin("git"),
            runtime._toolchain_bin("npm"),
            dep_sync.locked_console_scripts(target_py),
            dep_sync.installed_package_origin(target_py),
        ),
    )
    if git_bin is None:
        # Same failure class as the discovery path: name the remedy in the
        # response; the log records the event. The searched dirs are the
        # static _TRUSTED_BIN_DIRS constant (and the discovery-path warning
        # already prints them when it fires) — repeating them here trips
        # CodeQL's name-based secret heuristic on _TRUSTED_PATH for no
        # diagnostic gain.
        runtime.logger.warning(
            "dev-fleet: %s'git' — no trusted-dir candidate passed vetting "
            "and no override is set",
            runtime._UNRESOLVED_TOOL_PREFIX,
        )
        return {"ok": False, "error": runtime._unresolved_tool_message("git")}
    if npm_bin is None:
        # Drop the memoized resolution so the remedy this message advertises
        # actually works. `node_bin_dirs()` is lru_cached and `_BUILD_PATH_CACHE`
        # is set once per process, so in a long-lived gateway a user who ran
        # ensure-node.sh and hit Pull+build again would get this SAME error --
        # the marker file is never re-read. Invalidating on the failure path
        # makes the retry fresh. Only on failure: a successful resolution is
        # worth keeping cached, and this path is user-initiated, not a loop.
        runtime._invalidate_toolchain_cache()
        return {
            "ok": False,
            "error": (
                "npm not found. Kiro Crew looks for a Node toolchain in "
                "<data-home>/node-bin-dir (written by ensure-node.sh), then in "
                "mise / asdf / nvm / fnm / volta install dirs, then in "
                f"{runtime._TRUSTED_PATH}. Fix: run `bash ensure-node.sh` in the main "
                "checkout and press Pull + Build again — no restart needed. To point "
                "at a toolchain by hand instead, set "
                "KIROCREW_NODE_BIN_DIR=/abs/path/to/node/bin in the gateway's "
                "service environment; that one does need a restart, because a "
                "running process cannot see a new environment variable."
            ),
        }
    # A locked console script does not have to mean the sync cannot happen.
    #
    # pip cannot replace a running executable on Windows, and its uninstall is
    # not atomic: it renames the dist-info aside and deletes the editable `.pth`
    # before it reaches the locked script, rolling back neither. So
    # `pip install -e .` must not run against a venv serving this gateway.
    #
    # It does not have to run at all, though. An editable install needs no
    # reinstall for a source change -- `src` is already on `sys.path`, so merged
    # code is live the moment the merge lands. The only thing the reinstall was
    # still buying is a dependency a new revision added, and installing a
    # dependency never touches the project's own console script.
    #
    # The substitute keeps the SAME shape as the reinstall it stands in for:
    # `fetch -> merge -> install the project's requirements`, in that order, with
    # the project itself left out. Deviating from that shape -- installing before
    # the merge so a failure cannot strand a merged checkout -- was tried and
    # abandoned: it requires proving in advance that the merge cannot fail, which
    # needs a growing set of preconditions that still cannot be complete. A failed
    # install after a landed merge is what every other platform already does when
    # its reinstall step fails.
    #
    # The substitute step runs with THIS backend's interpreter for the same
    # reason the build+stage step does: the logic is revision-independent, so
    # resolving it from the target would make the step's very EXISTENCE
    # contingent on the pulled revision carrying dep_sync.
    #
    # It runs a pre-merge SNAPSHOT of that module, by path, and neither half of
    # that is incidental. `-m kiro_crew...dep_sync` would import the module from
    # the working tree AFTER the merge has landed, dragging in the whole package
    # __init__ chain with it -- so a revision that raises the `requires-python`
    # floor and uses newer syntax anywhere in that chain would die with a
    # SyntaxError while being parsed, and the floor refusal that exists for
    # exactly that revision could never run. Copying the module first and
    # executing the copy keeps the only parsed file one this interpreter has
    # already imported, which makes the refusal reachable in the case it is
    # written for. Running it by path rather than by module name is what keeps
    # the package chain out of it; the module imports nothing but the standard
    # library, so it needs no package context.
    #
    # This mirrors pip rather than deviating from it: the INSTALLED pip reads the
    # merged project's metadata, and an old pip refusing a too-new project is the
    # behaviour being stood in for here.
    # The fetch pins the tip it brought into this process's own base ref as well
    # as updating the remote-tracking ref, so the probe below and the merge here
    # consume ONE commit even though the refresher keeps fetching underneath
    # them. Resolved once here so every step of this run names the same ref, and
    # refs stranded by gateway processes that are gone are cleared first so they
    # do not accumulate in the operator's checkout.
    sync_base_ref = _sync_base_ref()
    await _prune_dead_sync_base_refs(str(repo))
    fetch_step = (
        [
            git_bin,
            "fetch",
            remote,
            repository.BASE_BRANCH,
            f"+refs/heads/{repository.BASE_BRANCH}:{sync_base_ref}",
        ],
        "standard",
        runtime._build_env(with_credentials=True),
        "Pull",
    )
    # Labelled distinctly from the fetch: with the preflight between them, two
    # steps both called "Pull" would render as Pull -> Preflight -> Pull and read
    # like the run had restarted.
    merge_step = (
        [git_bin, "merge", "--ff-only", sync_base_ref],
        "strict",
        runtime._build_env(),
        "Merge",
    )
    # The venv must be an install OF this checkout before either install step
    # runs, and that is true of the reinstall just as much as the substitute.
    #
    # `<repo>/.venv` is only where the interpreter was FOUND; it says nothing
    # about what it is an install of. When it serves a different checkout,
    # `pip install -e .` silently repoints its editable install at this repo and
    # the other checkout's gateway becomes this code on its next restart -- the
    # same hijack the target_py resolution above exists to prevent, arriving by
    # the other direction. The substitute has always refused this (dep_sync's own
    # first check); the reinstall branch did not, so the safer path was the only
    # guarded one. Checking here covers both, on every platform.
    foreign = dep_sync.venv_not_mapped_to(venv_origin, Path(repo))
    if foreign:
        return {
            "ok": False,
            "error": (
                f"refusing to sync: {foreign}. Give this checkout its own editable "
                "install, or run the sync from the checkout that venv serves."
            ),
        }
    dep_sync_snapshot: Path | None = None
    # Whether the frontend half runs at all is decided BEFORE the step list is
    # assembled, because the preflight belongs between fetch and merge and there
    # is nothing to preflight when the frontend half is skipped (see the edition
    # rationale where the build steps are appended).
    frontend_half = not frontend.edition_configured()
    # The preflight goes AFTER fetch and BEFORE merge, and that position is the
    # whole point rather than a detail.
    #
    # `npm ci` deletes website/node_modules before installing, so a registry
    # that refuses one package does not merely fail the sync — it empties the
    # tree and leaves the checkout with new source, a new lockfile and no
    # frontend dependencies. The lockfile that will be installed is knowable as
    # soon as fetch lands (it is in the fetched ref), and fetch moves only
    # remote refs, so between the two is the one moment where the question can
    # be asked while a refusal still costs nothing.
    #
    # The runner is fail-fast, so no extra refusal plumbing is needed: a failing
    # preflight step stops the run before the merge step is reached.
    #
    # Only ONE preflight is built, and it runs in the credential-free build tier
    # like every other npm invocation here — `_run_cmd` would have been the
    # obvious host for it but it overwrites PATH with _TRUSTED_PATH
    # unconditionally, and npm's wrapper needs `node` to resolve BY NAME, which
    # only _build_env()'s node-augmented PATH provides.
    preflight_steps: list[tuple[list[str], str, dict, str]] = []
    preflight_cleanup: list[str] = []
    if frontend_half:
        # Executed as a SNAPSHOT by path, never imported from the checkout.
        #
        # `-I` alone was not enough. It drops the cwd from sys.path, which closes
        # the untracked-`kiro_crew/`-at-the-checkout-root shadow -- but the
        # install is EDITABLE, so `import kiro_crew...` still resolves into the
        # checkout's own `src/` tree. This step is trusted to assert a failure
        # cause precisely BECAUSE its binary is ours, so importing a tree that is
        # itself the thing being synced put the boundary's own key under the mat.
        #
        # Copying the module out and running that copy is the same move this
        # function already makes for dep_sync a few lines below, and it works
        # here for a reason worth stating: npm_preflight imports nothing but the
        # stdlib, so a by-path snapshot needs no package context at all.
        #
        # mkdtemp rather than a fixed path, for the reason the dep_sync snapshot
        # gives: executing a script by path would put its DIRECTORY on sys.path,
        # so a predictable one would let anything dropped beside the snapshot
        # shadow a stdlib module it imports. mkdtemp is unguessable and 0o700 --
        # and `-I` removes that directory from sys.path as well, so neither the
        # checkout nor the snapshot's own neighbours can reach the interpreter.
        if runtime._PREFLIGHT_SOURCE is None:
            return {
                "ok": False,
                "error": (
                    "the dependency preflight's own source could not be read at "
                    "startup, so there is nothing trustworthy to run it from — "
                    "reinstall the gateway from a source checkout"
                ),
            }
        snap_dir: Path | None = None
        try:
            snap_dir = Path(tempfile.mkdtemp(prefix="kirocrew-npm-preflight-"))
            snap = snap_dir / "npm_preflight.py"
            # The BYTES captured at import, not a copy of the file as it is now:
            # copying at sync time would execute whatever had been written to the
            # checkout since this gateway started.
            snap.write_bytes(runtime._PREFLIGHT_SOURCE)
        except OSError as exc:
            # mkdtemp can succeed and the COPY still fail, and nothing has
            # registered the directory for the run's cleanup yet at this point --
            # so remove it here or a failed sync leaks one temp directory every
            # time, which is the same litter the pinned base refs were just
            # taught to avoid.
            if snap_dir is not None:
                shutil.rmtree(snap_dir, ignore_errors=True)
            # A full or unwritable TMPDIR must not escape as a 500: the sync
            # answers a UI action, so it degrades to the same {"ok": False} shape
            # every other refusal here uses. Refusing is also the SAFE outcome --
            # without a probe there is nothing to trust, and the alternative
            # (proceed unprobed) is exactly the destructive path this change
            # exists to prevent.
            return {
                "ok": False,
                "error": (
                    "could not stage the dependency preflight: "
                    f"{exc.strerror or exc} — free space in the temporary "
                    "directory and press Pull + Build again"
                ),
            }
        # File before directory: the run's cleanup unlinks each entry and falls
        # back to rmdir, which only succeeds on an empty one.
        preflight_cleanup = [str(snap), str(snap_dir)]
        preflight_steps = [
            (
                [
                    sys.executable,
                    "-I",
                    "-X",
                    "utf8",
                    str(snap),
                    "--git",
                    git_bin,
                    "--npm",
                    npm_bin,
                    "--repo",
                    str(repo),
                    "--ref",
                    sync_base_ref,
                ],
                "strict",
                runtime._build_env(),
                runtime._PREFLIGHT_LABEL,
            )
        ]
    if locked_scripts:
        runtime.logger.info(
            "dev-fleet: %s locked by a running process; substituting a "
            "dependency-only sync for the editable reinstall",
            ", ".join(locked_scripts),
        )
        # mkdtemp, not a fixed path under the system temp dir: executing a script
        # by path puts its DIRECTORY on sys.path, so a shared or predictable
        # directory would let anything dropped beside the snapshot shadow a
        # stdlib module the snapshot imports. mkdtemp is unguessable and 0o700.
        snapshot_dir = Path(tempfile.mkdtemp(prefix="kirocrew-dep-sync-"))
        dep_sync_snapshot = snapshot_dir / "dep_sync.py"
        shutil.copyfile(dep_sync.__file__, dep_sync_snapshot)
        steps = [
            fetch_step,
            *preflight_steps,
            merge_step,
            (
                [sys.executable, str(dep_sync_snapshot), str(repo), str(target_py)],
                "strict",
                runtime._build_env(),
                "pip install",
            ),
        ]
    else:
        steps = [
            fetch_step,
            *preflight_steps,
            merge_step,
            (
                [str(target_py), "-m", "pip", "install", "-e", "."],
                "strict",
                runtime._build_env(),
                "pip install",
            ),
        ]
    raw_steps: list[tuple[list[str], str, dict, str]] = steps
    # The whole FRONTEND half of the sync is skipped on an edition checkout.
    #
    # The build runs under _build_env(), whose allowlist (_SAFE_ENV_KEYS) drops
    # KIROCREW_EDITION_DIR and KIROCREW_ALLOW_EDITION, so on an edition
    # composition root `npm run build` can only compile the STOCK SPA -- and vite
    # builds with emptyOutDir, so it OVERWRITES website/dist. On a source-tree
    # install frontend.ensure_dev_dist_symlink() has pointed static/dist at
    # website/dist, which means the build alone replaces the served edition
    # dashboard with upstream's, with or without a staging step. Skipping the
    # build is therefore the only way to make this safe, and it costs an edition
    # nothing: the only artifact this path could produce for it is a stock SPA it
    # must never serve. It is the same call frontend's own
    # edition_sources_missing() guard already makes -- leave the shipped bundle
    # alone rather than degrade it.
    #
    # This backend is a SEPARATE process started with apps.registry.minimal_env(),
    # which strips KIROCREW_EDITION_DIR, so the guard would read "stock" on every
    # install and never fire. apps/backend.py therefore propagates that one var
    # explicitly, the same way it already propagates KIROCREW_PROJECT_DIR.
    if not frontend_half:
        runtime.logger.info(
            "dev-fleet: skipping the frontend build and dist staging -- this is "
            "an edition checkout and the sync build cannot recompose the "
            "edition; the shipped bundle is left in place"
        )
    else:
        raw_steps += [
            ([npm_bin, "ci", "--prefix", "website"], "strict", runtime._build_env(), "npm ci"),
            # Build and stage as ONE step, holding the staging lock across both.
            # `npm run build` empties website/dist, so a peer flow (the
            # dashboard's own update, pod provisioning) staging concurrently
            # would copy a partially written tree — and a bundle's lazy chunks
            # are not reachable from index.html, so no post-hoc inspection of
            # the copy detects that reliably. Covering only the copy is not
            # enough; the holder has to span the build.
            #
            # Run with THIS backend's interpreter, not the target checkout's, for
            # the same reason the staging step does: the logic is
            # revision-independent, while resolving it from the target would make
            # the step's very EXISTENCE contingent on the pulled revision
            # carrying build_and_stage, turning an older target into an
            # ImportError that fails the whole Pull+Build. The repo to build and
            # npm's resolved trusted path are passed in rather than re-resolved.
            (
                [
                    sys.executable,
                    "-c",
                    "import sys;from kiro_crew.frontend import build_and_stage;"
                    "sys.exit(0 if build_and_stage(sys.argv[1], npm=sys.argv[2]) else 1)",
                    repo,
                    npm_bin,
                ],
                "strict",
                runtime._build_env(),
                "npm build + stage",
            ),
        ]
    cleanups: list[str] = []
    # The preflight's snapshot is removed with the run's other temporaries. It is
    # registered here rather than left behind: a leaked mkdtemp per sync is how
    # the dependency-only path already accumulates one, and repeating that would
    # litter the operator's temp directory on every Pull + Build.
    cleanups.extend(preflight_cleanup)
    if dep_sync_snapshot is not None:
        # File first, then its directory: the cleanup loop removes entries in
        # order, and a directory cannot go until it is empty.
        cleanups += [str(dep_sync_snapshot), str(dep_sync_snapshot.parent)]
    wrapped_steps: list[dict] = []
    for argv, mode, base_env, label in raw_steps:
        w_argv, w_env, cleanup = await shielded_prepare_off_loop(
            functools.partial(sandboxed_spawn_argv, argv, mode, env=base_env),
            executor=subprocess_executor(),
        )
        if cleanup:
            cleanups.append(cleanup)
        wrapped_steps.append({"argv": w_argv, "env": w_env, "label": label})
    for step in wrapped_steps:
        if step["label"] == "npm ci":
            # `npm ci` deletes node_modules BEFORE it installs, and a tree it
            # emptied is the one artifact of a failed sync that cannot be
            # rebuilt without the registry — which is exactly what is unavailable
            # when this step fails. So the runner moves it aside first and puts it
            # back if the step does not succeed, making the failure a no-op
            # instead of damage. A separate "restore" step could not do this: the
            # runner is fail-fast, so anything after a failed step never runs.
            step["stash"] = str(Path(repo) / "website" / "node_modules")
    # The runner is a SNAPSHOT of :mod:`sync_runner`, staged into a private
    # mkdtemp and run BY PATH with `-I`. Running `-m kiro_crew...sync_runner`
    # would import it from the working tree AFTER the merge has landed (and drag
    # the package __init__ chain in with it), and interpolating source into a
    # `-c` string is what this module replaced. mkdtemp is unguessable and
    # 0o700; the steps JSON lives in the SAME dir and is passed as a PATH
    # argument, never interpolated into source.
    if runtime._SYNC_RUNNER_SOURCE is None:
        return {
            "ok": False,
            "error": (
                "the sync runner's own source could not be read at startup, so "
                "there is nothing trustworthy to run it from — reinstall the "
                "gateway from a source checkout"
            ),
        }
    runner_dir: Path | None = None
    try:
        runner_dir = Path(tempfile.mkdtemp(prefix="kirocrew-sync-runner-"))
        runner_snap = runner_dir / "sync_runner.py"
        # The BYTES captured at import, not a copy of the file as it is now.
        runner_snap.write_bytes(runtime._SYNC_RUNNER_SOURCE)
        # Pinned in argv below (same contract as the steps digest): the staged
        # file is mutable between now and exec, argv is not, so the bootstrap
        # can refuse a snapshot rewritten in that window.
        runner_sha256 = hashlib.sha256(runtime._SYNC_RUNNER_SOURCE).hexdigest()
        steps_json_path = runner_dir / "steps.json"
        # The step list travels as a FILE, not embedded in argv or source: a
        # non-ASCII checkout path in a step's argv or env then cannot break the
        # program that reads it, and nothing worktree-controlled is interpolated
        # into code.
        steps_payload = json.dumps(wrapped_steps).encode("utf-8")
        steps_json_path.write_bytes(steps_payload)
        # Pinned in argv below: argv is immutable once the runner process
        # exists, so the runner can verify the file it reads is the manifest
        # THIS gateway staged -- a rewrite of steps.json between staging and
        # startup fails the digest check and nothing runs.
        steps_sha256 = hashlib.sha256(steps_payload).hexdigest()
    except OSError as exc:
        # A full or unwritable TMPDIR must not escape as a 500, and nothing has
        # registered this dir for the run's cleanup yet -- remove it here or a
        # failed sync leaks one temp dir every time. Refusing is also the SAFE
        # outcome: without a runner there is nothing to drive the steps.
        if runner_dir is not None:
            shutil.rmtree(runner_dir, ignore_errors=True)
        return {
            "ok": False,
            "error": (
                "could not stage the sync runner: "
                f"{exc.strerror or exc} — free space in the temporary directory "
                "and press Pull + Build again"
            ),
        }
    # Files before their directory: the run's cleanup unlinks each entry and
    # falls back to rmdir, which only succeeds on an empty one.
    cleanups += [str(runner_snap), str(steps_json_path), str(runner_dir)]
    # `-I` isolates the interpreter (drops cwd from sys.path and ignores env
    # vars that alter it), the same hardening the preflight snapshot uses; the
    # snapshot dir itself is removed from sys.path by `-I` too. The runner is
    # executed THROUGH the fixed bootstrap above, which verifies the staged
    # snapshot's bytes against the argv-pinned digest before compiling them —
    # closing the staging→exec mutation window. The reserved set and the one
    # trusted label are passed IN so the runner imports nothing from
    # kiro_crew -- what it does cannot change with the revision merged under it.
    cmd = [
        sys.executable,
        "-I",
        "-c",
        _SYNC_RUNNER_BOOTSTRAP,
        str(runner_snap),
        runner_sha256,
        str(steps_json_path),
        str(repo),
        "--reserved",
        ",".join(str(c) for c in sorted(npm_preflight.RESERVED_EXIT_CODES)),
        "--preflight-label",
        runtime._PREFLIGHT_LABEL,
        "--exit-tree-ambiguous",
        str(npm_preflight.EXIT_TREE_AMBIGUOUS),
        "--exit-restore-failed",
        str(npm_preflight.EXIT_RESTORE_FAILED),
        "--steps-sha256",
        steps_sha256,
    ]
    rid = await runtime._start_run(
        runtime._SYNC_RUN_LABEL, cmd, env=runtime._build_env(), cleanup_paths=cleanups
    )
    _SYNC_RID = rid
    return {"ok": True, "run_id": rid}


# --- rebase ---
# Per-worktree mutation locks: two concurrent /rebase requests for the same
# checkout could both pass the clean-state check, then one's failure path
# would `rebase --abort` the OTHER's in-flight rebase.
_WT_LOCKS: dict[str, LoopBoundLock] = {}


def _wt_lock(name: str) -> LoopBoundLock:
    return _WT_LOCKS.setdefault(name, LoopBoundLock())


async def _rebase(name: str) -> dict:
    """Rebase worktree onto latest base branch. Aborts on conflict."""
    target, err = await repository._find_worktree(name)
    if target is None:
        return {"ok": False, "error": err}
    if target.get("is_main"):
        return {"ok": False, "error": "refusing to rebase the main checkout"}
    lock = _wt_lock(name)
    if lock.locked():
        return {"ok": False, "error": "rebase already running for this worktree"}
    async with lock:
        return await _rebase_locked(target)


async def _rebase_locked(target: dict) -> dict:
    path = target["path"]
    st = await repository._git(path, "status", "--porcelain")
    if st is None:
        return {"ok": False, "error": "cannot verify worktree state (git status failed)"}
    if st:
        # Same fileless refusal the removal path used to give. Name the dirt so
        # the user can act on it. The GATE is deliberately unchanged: an
        # untracked file cannot conflict semantically, but it can still block
        # the rebase's checkout when it collides with a path a replayed commit
        # creates, so loosening this to tracked-only is a separate decision with
        # its own failure mode (a rebase stopped halfway), not a rename of this
        # message.
        _fields, _detail = await repository._dirt_report(path)
        return {
            "ok": False,
            **_fields,
            "error": "worktree has uncommitted changes" + _detail,
        }
    remote = await repository._upstream_remote()
    if await repository._git(path, "fetch", remote, repository.BASE_BRANCH, timeout=90) is None:
        return {"ok": False, "error": f"git fetch {remote} {repository.BASE_BRANCH} failed"}
    rc, stdout, stderr = await runtime._run_cmd(
        ["git", "-C", path, "rebase", f"{remote}/{repository.BASE_BRANCH}"],
        timeout=180,
        mode="strict",
    )
    if rc == 0:
        g = await repository._git_info(path)
        return {"ok": True, "rebased": True, "head": g["head"], "behind": g["behind"]}
    abort_res = await repository._git(path, "rebase", "--abort", timeout=30, mode="strict")
    # Redact the FULL text, then take the tail: a tail cut of already-redacted
    # text can at worst split a redaction marker, never a secret.
    tail = runtime._redact((stdout + stderr).strip())[-200:]
    if abort_res is None:
        # Abort itself failed/timed out — the worktree is still mid-rebase.
        # Never report "aborted" when it is not; manual recovery required.
        return {
            "ok": False,
            "conflict": True,
            "error": (
                "rebase conflict AND `git rebase --abort` failed — worktree "
                f"is still mid-rebase; manual recovery required. {tail}"
            ),
        }
    return {"ok": False, "conflict": True, "error": f"rebase conflict (aborted). {tail}"}


# --- prune ---
# Per-item state machine (``items``) drives the frontend checklist, while the
# top-level ``running``/``total``/``done``/``current``/``results`` fields are
# kept for backward compatibility (auto-prune reaper + any existing consumers).
_PRUNE_STATE: dict = {
    "running": False,
    "total": 0,
    "done": 0,
    "current": None,
    "results": [],
    "items": {},
}
_PRUNE_LOCK = LoopBoundLock()
# Cap on concurrent per-item prune phases (fresh gh verdict + pod shutdown).
_PRUNE_CONCURRENCY = 4
# Serializes the destructive git mutations (`git worktree remove` +
# `update-ref -d`) across ALL removal paths — concurrent prune workers, the
# single-worktree remove handler, and the auto-prune reaper — because they all
# mutate the shared MAIN_REPO ``.git`` state (worktree admin dir + packed-refs).
# Uncontended in the sequential paths; only the parallel prune workers ever
# queue on it.
# LoopBoundLock excludes within one loop only. That covers every contender
# here: all acquirers are aiohttp handlers and tasks on the app's single
# gateway loop — no worker thread runs its own loop against this .git.
_GIT_MUTATION_LOCK = LoopBoundLock()


# Prune verdicts an untracked-discard approval is allowed to override. Both are
# verdicts where the DIRT is what withheld the candidate, which is exactly what
# the caller consented to clear. Deliberately excludes `dirty_check_failed` (the
# tree is unverifiable, so nothing was enumerated to consent to) and the
# merged_* verdicts about commit divergence, which a discard says nothing about.
# `active` stays in: dirt is one of its two causes, and when the cause is
# unmerged commits instead, the removal's own PR gate refuses it a moment later.
# `closed_dirty` joins them: like `merged_dirty` the DIRT is what withheld the
# candidate, so a discard of exactly the untracked files the operator was shown
# is the consent that unblocks it. Force is still required for a tracked-file
# modification; a discard alone never destroys tracked work.
_DISCARD_OVERRIDABLE_CODES = frozenset({"merged_dirty", "closed_dirty", "active"})


async def _prunable(path: str, branch: str | None) -> dict:
    """Structured prune verdict. Squash-merge safe: PR merged + clean -> ok.

    Does NOT require ahead==0 (git cherry never reports 0 for squash merges).
    The race guard in _worktree_remove handles the edge case of commits pushed
    after the PR was merged by comparing branch OID to the PR's headRefOid.
    """
    # Resolve the full HEAD once so cache invalidation cannot alias distinct
    # commits that share an abbreviated prefix. Reuse it for the merge guard.
    head_oid = (await repository._git(path, "rev-parse", "HEAD")) if branch else None
    pr = (await fleet_state._pr_status_cached(branch, head_oid)) if branch else None
    own = await repository._own_commits_count(path)
    dirty = await repository._real_dirty(path)
    # Classify the dirt so the preview can tell a tree blocked by real edits
    # apart from one blocked only by leftover session scratch. Both stay
    # non-candidates -- prune never discards files without explicit consent --
    # but the caller needs the difference to know which of the two it is
    # looking at, and whether a discard is even offerable.
    dirt_tracked, dirt_untracked = await repository._dirty_split(path) if dirty else (None, [])
    try:
        age_h = round((time.time() - Path(path).stat().st_ctime) / 3600, 1)
    except OSError:
        age_h = None
    base = {
        "pr": runtime._redact_pr(pr),
        "own": own,
        "dirty": dirty,
        "age_h": age_h,
        **repository._dirt_fields(dirt_tracked, dirt_untracked),
    }
    if dirty is None:
        return {**base, "ok": False, "code": "dirty_check_failed"}
    if fleet_state._is_pr_merged(pr):
        if dirty:
            return {**base, "ok": False, "code": "merged_dirty"}
        # Same squash-safe race guard removal enforces: commits pushed AFTER
        # the merge mean the branch OID diverged from the PR head — surface it
        # at preview time instead of letting the candidate fail every run.
        pr_oid = (
            await fleet_state._fetch_pr_head_oid(branch, repo=(pr or {}).get("_repo"))
            if branch
            else None
        )
        if not head_oid or not pr_oid:
            # Cannot verify the squash-safe guard: removal would refuse this
            # anyway, so never present it as a candidate (fail-closed verdict
            # keeps preview and execution consistent).
            return {**base, "ok": False, "code": "merged_unverified"}
        if not await fleet_state._head_contained_in_pr(path, head_oid, pr_oid):
            return {**base, "ok": False, "code": "merged_new_commits"}
        return {**base, "ok": True, "code": "merged"}
    if fleet_state._is_pr_closed(pr):
        # A CLOSED-unmerged PR is prunable through the MANUAL path only (the
        # reaper filters to code=="merged"; see _auto_prune_once). Unlike a
        # merged tree, nothing guarantees this worktree's content is on the base
        # branch — the PR was declined or superseded while the tree kept moving
        # — so three guards stand between the operator and data loss:
        #
        #  * A dirty tree is REFUSED by default (code "closed_dirty"), reusing
        #    the same dirty/dirty_tracked/force plumbing as merged_dirty. The
        #    dirt breakdown already sits in `base` via _dirt_fields, so the
        #    checklist can tell the operator exactly what a discard/force would
        #    destroy (N modified tracked files, N untracked files) instead of
        #    asking for a blind confirm.
        #  * `own` (commits on this branch not reachable from the base branch,
        #    from _own_commits_count) is surfaced as `unmerged_commits`. A
        #    clean closed tree that is ALSO ahead of base is a stronger warning
        #    than a clean merged tree, whose content is shipped by definition;
        #    the UI raises the alarm on this flag rather than re-deriving
        #    ancestry with a fresh git call.
        unmerged = bool(own and own > 0)
        if dirty:
            return {**base, "ok": False, "code": "closed_dirty", "unmerged_commits": unmerged}
        #  * The verdict is BOUND TO THE BRANCH HEAD, exactly as the merged path
        #    above binds its own. A closed PR is looked up by branch NAME, so a
        #    branch that was reused after its PR closed -- new commits, work the
        #    closed PR never saw, possibly a replacement PR not opened yet --
        #    still resolves to that stale CLOSED verdict. Without this check the
        #    tree would be offered as prunable and removing it would destroy
        #    work that has nothing to do with the PR that was declined. The
        #    guard is fail-closed: an OID that cannot be established withholds
        #    the candidate rather than trusting the name-based lookup.
        pr_oid = (
            await fleet_state._fetch_pr_head_oid(branch, repo=(pr or {}).get("_repo"))
            if branch
            else None
        )
        if not head_oid or not pr_oid:
            return {**base, "ok": False, "code": "closed_unverified", "unmerged_commits": unmerged}
        if not await fleet_state._head_contained_in_pr(path, head_oid, pr_oid):
            return {**base, "ok": False, "code": "closed_new_commits", "unmerged_commits": unmerged}
        return {**base, "ok": True, "code": "closed", "unmerged_commits": unmerged}
    if own == 0 and not dirty:
        if age_h and age_h > 48:
            return {**base, "ok": True, "code": "empty"}
        return {**base, "ok": False, "code": "fresh"}
    return {**base, "ok": False, "code": "active"}


async def _prune_candidates() -> dict:
    worktrees = await repository._discover_worktrees()
    candidates, kept = [], []
    for w in worktrees:
        if w.get("is_main"):
            continue
        name = Path(w["path"]).name
        v = await _prunable(w["path"], w.get("branch"))
        row = {"name": name, "code": v["code"], "branch": w.get("branch")}
        if v["ok"]:
            # A closed-PR candidate carries the ancestry warning so the
            # checklist can flag a clean-but-ahead tree distinctly from a
            # merged one (whose content is shipped by definition). The key is
            # absent on merged rows, which the frontend reads as "not
            # applicable".
            if v.get("code") == "closed":
                row["unmerged_commits"] = bool(v.get("unmerged_commits"))
            candidates.append(row)
        else:
            # Surface dirty flag so the frontend can pre-disable force-selection
            # for dirty+unmerged worktrees (the backend refuses force=True on
            # those anyway — exposing the flag avoids a misleading checkbox).
            if v.get("dirty") is True:
                row["dirty"] = True
                # And the breakdown, so a tree held up only by leftover
                # session scratch can offer a discard instead of reading as
                # permanently stuck. `force` alone is still refused on it;
                # discarding the untracked files is what unblocks it.
                row["dirty_tracked"] = v.get("dirty_tracked")
                row["dirty_untracked"] = v.get("dirty_untracked")
                row["dirty_untracked_paths"] = v.get("dirty_untracked_paths")
            kept.append(row)
    return {"ok": True, "candidates": candidates, "kept": kept, "scanned": len(worktrees) - 1}


async def _prune_run(
    names: list[str],
    force_names: set[str] | None = None,
    discard_paths: dict[str, list[str]] | None = None,
) -> dict:
    # Deduplicate while preserving order: the API accepts any list of names,
    # and a duplicate would spawn two workers racing to remove the SAME
    # worktree — the second one then reports a spurious failure over the
    # first one's success.
    global _prune_task
    _force = force_names or set()
    # Per-name consented untracked sets. Like ``force`` this is an explicit
    # override of the preview verdict, so a named worktree joins the work list
    # and skips the re-preview below; unlike ``force`` it does not claim the
    # commits are shipped, it carries the exact file set the caller was shown,
    # and the removal still applies every gate.
    _discard_paths = discard_paths or {}
    _discard = set(_discard_paths)
    # Forced items (kept worktrees the user overrode) arrive in ``force_names``
    # disjoint from the regular candidate ``names``. Both must be processed, so
    # the work list is the order-preserving union — regulars first, then any
    # forced name not already present. Building ``total``/``items``/the dispatch
    # from this union (rather than ``names`` alone) is what keeps a force-only
    # prune counted: otherwise its ``done`` bump has no matching denominator or
    # item row, producing an impossible ``1/0`` counter and a false failure.
    names = list(dict.fromkeys([*names, *_force, *_discard]))
    async with _PRUNE_LOCK:
        if _PRUNE_STATE["running"]:
            return {"ok": False, "error": "prune already running"}
        _PRUNE_STATE.update(
            {
                "running": True,
                "total": len(names),
                "done": 0,
                "current": None,
                "results": [],
                "items": {nm: {"status": "pending", "error": None} for nm in names},
            }
        )

    items = _PRUNE_STATE["items"]
    sem = asyncio.Semaphore(_PRUNE_CONCURRENCY)
    # ``current`` is kept for API-shape compatibility but under parallel
    # execution it is BEST-EFFORT: one of the currently in-flight items (or
    # None when idle). ``active`` tracks in-flight names so a finished worker
    # can hand ``current`` to a still-running one instead of leaving a
    # completed name dangling.
    active: set[str] = set()

    async def _prune_one(nm: str) -> None:
        # The expensive phases (fresh gh verdict + pod shutdown) run
        # concurrently, capped by the semaphore. The destructive git mutation
        # inside _worktree_remove is serialized on _GIT_MUTATION_LOCK. Every
        # item finalizes EXACTLY once (in ``finally``) to a terminal status and
        # bumps ``done`` — so one item failing (or raising) never wedges the
        # batch or stops the others.
        async with sem:
            active.add(nm)
            _PRUNE_STATE["current"] = nm
            status = "failed"
            error: str | None = None
            result: dict = {"name": nm, "ok": False}
            try:
                items[nm]["status"] = "verifying"
                # Re-resolve and require a fresh prunable verdict immediately
                # before removal — the API accepts any discovered name, so a
                # clean-but-recent worktree must be rejected here.
                target, err = await repository._find_worktree(nm)
                if target is None:
                    error = err
                    result = {"name": nm, "ok": False, "error": err}
                else:
                    is_forced = nm in _force
                    # A regular candidate re-verified as `closed` (a clean,
                    # CLOSED-PR worktree) needs the force PATH at removal: with
                    # force=False, _worktree_remove refuses any tree whose PR is
                    # not merged and is ahead of base, which a closed candidate
                    # routinely is. Force here does NOT mean `git worktree
                    # remove --force` — for a clean unmerged tree that gate
                    # drops the git flag and lets git's own dirty check fire at
                    # removal time (the TOCTOU guard), so a late edit still
                    # blocks it. It only lifts the "PR not merged" refusal, and
                    # only after the fresh _prunable verdict below CONFIRMED the
                    # tree is clean and closed.
                    remove_force = is_forced
                    if not is_forced:
                        verdict = await _prunable(target["path"], target.get("branch"))
                        if verdict.get("code") == "closed" and verdict.get("ok"):
                            remove_force = True
                        if not verdict.get("ok"):
                            # A discard approval overrides ONLY a verdict whose
                            # blocker is the dirt the caller just consented to.
                            # Unlike `force` it does not inherit a blanket
                            # bypass of every other refusal -- `fresh`,
                            # `merged_new_commits`, `merged_unverified` and the
                            # unverifiable `dirty_check_failed` still stand.
                            overridden = (
                                nm in _discard and verdict.get("code") in _DISCARD_OVERRIDABLE_CODES
                            )
                            if not overridden:
                                error = f"not prunable: {verdict.get('code', 'unknown')}"
                                result = {"name": nm, "ok": False, "error": error}
                    if not error:

                        def _progress(phase: str, _nm: str = nm) -> None:
                            items[_nm]["status"] = phase

                        res = await _worktree_remove(
                            nm,
                            force=remove_force,
                            progress=_progress,
                            _caller="prune",
                            discard_untracked_paths=_discard_paths.get(nm),
                        )
                        result = {"name": nm, **res}
                        if res.get("ok"):
                            status, error = "done", None
                        else:
                            status, error = "failed", res.get("error")
            except Exception as exc:  # noqa: BLE001
                error = runtime._redact(str(exc))
                result = {"name": nm, "ok": False, "error": error}
                runtime.logger.exception("dev-fleet prune: item %r failed", nm)
            finally:
                items[nm].update(status=status, error=error)
                _PRUNE_STATE["results"].append(result)
                _PRUNE_STATE["done"] += 1
                active.discard(nm)
                # Never leave a COMPLETED name in ``current``: hand it to any
                # still-in-flight item, or None when this was the last one.
                if _PRUNE_STATE["current"] == nm:
                    _PRUNE_STATE["current"] = next(iter(active), None)

    async def _work() -> None:
        try:
            await asyncio.gather(*(_prune_one(nm) for nm in names), return_exceptions=True)
        finally:
            _PRUNE_STATE["running"] = False
            _PRUNE_STATE["current"] = None

    # Retain the worker so dev_fleet_cleanup can cancel+await it on shutdown
    # (it runs destructive git mutations that must not outlive the gateway).
    # Clear the module handle when the batch finishes so an idle slot never
    # holds a completed task between prunes, mirroring the _ACTIVE_RUNS
    # done-callback convention.
    task = asyncio.create_task(_work())
    _prune_task = task

    def _clear(_t: asyncio.Task) -> None:
        global _prune_task
        if _prune_task is _t:
            _prune_task = None

    task.add_done_callback(_clear)
    return {"ok": True, "total": len(names)}


async def _prune_status() -> dict:
    # Snapshot both the backward-compatible top-level fields and the per-item
    # state machine. Copies are made so the JSON encoder never observes a dict
    # being mutated by an in-flight worker.
    return {
        "running": _PRUNE_STATE["running"],
        "total": _PRUNE_STATE["total"],
        "done": _PRUNE_STATE["done"],
        "current": _PRUNE_STATE["current"],
        "results": list(_PRUNE_STATE["results"]),
        "items": {
            nm: {"status": st.get("status"), "error": st.get("error")}
            for nm, st in _PRUNE_STATE.get("items", {}).items()
        },
    }


# --- background fleet refresher (started on app startup) ---
_NET_REFRESH_S = 60
_refresher_task: asyncio.Task | None = None
_warm_task: asyncio.Task | None = None
_reaper_task: asyncio.Task | None = None
# The in-flight prune batch worker. Retained (not discarded) so
# dev_fleet_cleanup can cancel and await it on shutdown: the worker runs the
# destructive `git worktree remove` / `update-ref -d` mutations under
# _GIT_MUTATION_LOCK, and a batch left running past cleanup would keep mutating
# the shared MAIN_REPO .git state after the gateway exits. Single-flight: only
# one prune batch runs at a time (guarded by _PRUNE_STATE["running"]), so one
# slot suffices.
_prune_task: asyncio.Task | None = None

# Test-only escape hatch: a test that boots the real app via ``create_app()``
# (e.g. to exercise the HMAC middleware) would otherwise start a genuine
# network ``git fetch`` inside ``_status_refresher`` with nothing stubbed,
# leaking a live background task into whichever test runs next. Unset in
# production; ``test/conftest.py`` sets it for every test by default.
_DISABLE_BACKGROUND_ENV = "KIROCREW_DEVFLEET_NO_BACKGROUND"


def _background_tasks_disabled() -> bool:
    return os.environ.get(_DISABLE_BACKGROUND_ENV) == "1"


# Auto-prune reaper (opt-in via dev_fleet.auto_prune.enabled). The poll interval
# is floored so a misconfigured tiny value can't hammer gh/git every cycle.
_AUTO_PRUNE_MIN_INTERVAL_S = 300
_AUTO_PRUNE_DEFAULT_INTERVAL_S = 3600


async def _status_refresher() -> None:
    """Background task: periodically fetch upstream + refresh fleet cache."""
    try:
        repo = repository._repo()
    except repository.RepoUnavailable as exc:
        # No usable checkout to fetch or cache — nothing found, or the configured
        # path is not one. Returning ends the task instead of logging a traceback
        # every cycle forever; the resolved value only changes on restart, so
        # there is nothing to wait for.
        runtime.logger.info("dev-fleet: status refresher idle — %s", exc)
        return
    while True:
        try:
            remote = await repository._upstream_remote()
            await runtime._run_cmd(
                ["git", "-C", repo, "fetch", remote, repository.BASE_BRANCH, "--quiet"],
                timeout=90,
            )
            await fleet_state._fleet_refresh()
        except Exception:
            runtime.logger.exception("dev-fleet status refresher failed")
        await asyncio.sleep(_NET_REFRESH_S)


# --- auto-prune reaper (opt-in) ---------------------------------------------
def _auto_prune_cfg() -> tuple[bool, int]:
    """(enabled, interval_secs) from the ``dev_fleet.auto_prune`` config section.

    Disabled by default — auto-prune REMOVES merged worktrees (and stops their
    pods), so it must be an explicit opt-in. The interval is floored at
    ``_AUTO_PRUNE_MIN_INTERVAL_S`` to protect gh/git from a misconfigured tiny
    value, and read fresh each cycle so toggling the flag takes effect without a
    gateway restart.
    """
    section = repository._load_dev_fleet_cfg().get("auto_prune")
    if not isinstance(section, dict):
        return False, _AUTO_PRUNE_DEFAULT_INTERVAL_S
    # Strict literal-True opt-in: a truthy string like "false" (or any non-empty
    # string / nonzero int) must NEVER arm destructive auto-prune — only a real
    # JSON boolean true does. `bool("false")` is True, so `bool(...)` is unsafe here.
    enabled = section.get("enabled") is True
    raw = section.get("interval_secs", _AUTO_PRUNE_DEFAULT_INTERVAL_S)
    try:
        interval = int(raw)
    except (TypeError, ValueError):
        interval = _AUTO_PRUNE_DEFAULT_INTERVAL_S
    return enabled, max(_AUTO_PRUNE_MIN_INTERVAL_S, interval)


async def _auto_prune_once() -> dict:
    """Remove every MERGED+clean worktree once, reusing the manual-prune path.

    Candidates come from ``_prune_candidates`` but are filtered to
    ``code == "merged"`` (PR MERGED + clean + OID-verified). ``_prune_candidates``
    also surfaces an "empty + stale >48h" class; silently auto-deleting an
    unmerged empty branch (e.g. one created but not yet pushed) on a timer is
    surprising, so that riskier class stays MANUAL-only ("Prune merged"). Each
    kept candidate is removed via ``_worktree_remove(force=False)`` — which stops
    a running pod first (then re-verifies) and applies the squash-safe OID race
    guard. Nothing is force-removed. Best-effort: never raises; returns
    ``{removed, failed}``.

    The ``closed`` class (PR CLOSED without merging) is DELIBERATELY excluded
    here, for a stronger reason than the stale-empty exclusion above. A merged
    worktree is safe to delete because its content is on the base branch by
    definition; a closed one carries no such guarantee — the PR was declined or
    superseded while the tree kept moving, so it routinely holds the only copy
    of work that never landed (measured on a real fleet: one closed worktree
    held a multi-hundred-line uncommitted rewrite, another held brand-new
    untracked source files in no commit and no PR). Reaping that on a timer,
    with no human to read the loss summary, would destroy it irrecoverably.
    Closed worktrees are therefore prunable ONLY through the manual checklist,
    which refuses a dirty tree by default and names what a removal would lose.
    """
    removed: list[str] = []
    failed: list[dict] = []
    try:
        cand = await _prune_candidates()
    except Exception as exc:  # noqa: BLE001
        runtime.logger.exception("dev-fleet auto-prune: candidate scan failed")
        # Surface the scan failure so the reaper still emits a SEL FAILURE event
        # — a failed destructive-op cycle must never be absent from the audit trail.
        return {"removed": removed, "failed": failed, "error": runtime._redact(str(exc))}
    for row in cand.get("candidates", []):
        name = row.get("name")
        # Restrict unattended auto-prune to MERGED worktrees only; the
        # stale-empty AND closed classes stay manual (see docstring). A closed
        # worktree may hold the only copy of unmerged work, so it is never
        # reaped on a timer.
        if not name or row.get("code") != "merged":
            continue
        try:
            res = await _worktree_remove(name, force=False, _caller="reaper")
        except Exception as exc:  # noqa: BLE001
            res = {"ok": False, "error": runtime._redact(str(exc))}
        if res.get("ok"):
            removed.append(name)
        else:
            failed.append({"name": name, "error": res.get("error")})
    return {"removed": removed, "failed": failed, "error": None}


async def _auto_prune_reaper() -> None:
    """Background loop that auto-prunes merged worktrees when enabled.

    Always running but a strict opt-in: each cycle re-reads
    ``dev_fleet.auto_prune`` and does nothing unless ``enabled`` is true, so the
    feature toggles live. A cycle that removes or fails anything is recorded in
    the SEL audit trail (same tamper-evident sink as the manual mutations).
    """
    while True:
        enabled, interval = _auto_prune_cfg()
        # Ask the accessor rather than testing MAIN_REPO for truthiness: a
        # configured path that fails the marker test is TRUTHY but unusable, and
        # a truthiness guard would let the cycle run, raise RepoUnreadable from
        # _prune_candidates, log a traceback, and record a `failure` outcome in
        # the SEL trail every interval — a tamper-evident audit asserting a
        # failure that never happened.
        usable = True
        try:
            repository._repo()
        except repository.RepoUnavailable:
            usable = False
        if enabled and usable:
            try:
                res = await _auto_prune_once()
                had_error = bool(res["failed"] or res.get("error"))
                if res["removed"] or had_error:
                    runtime._sel().log_tool_invocation(
                        session_key="api",
                        source="api",
                        tool_name="dev_fleet_auto_prune",
                        tool_kind="dev_fleet",
                        outcome="failure" if had_error else "success",
                        resources=runtime._redact(",".join(res["removed"])),
                        error=(
                            ""
                            if not had_error
                            else runtime._redact(res.get("error") or str(res["failed"]))[:200]
                        ),
                    )
            except Exception:  # noqa: BLE001
                runtime.logger.exception("dev-fleet auto-prune reaper cycle failed")
        await asyncio.sleep(interval)


__all__ = (
    "_AUTO_PRUNE_DEFAULT_INTERVAL_S",
    "_AUTO_PRUNE_MIN_INTERVAL_S",
    "_DISABLE_BACKGROUND_ENV",
    "_DISCARD_OVERRIDABLE_CODES",
    "_GIT_MUTATION_LOCK",
    "_NET_REFRESH_S",
    "_PRUNE_CONCURRENCY",
    "_PRUNE_LOCK",
    "_PRUNE_STATE",
    "_SYNC_RID",
    "_WT_LOCKS",
    "_auto_prune_cfg",
    "_auto_prune_once",
    "_auto_prune_reaper",
    "_background_tasks_disabled",
    "_pod_checkout_guard",
    "_pod_down",
    "_pod_env",
    "_pod_logs",
    "_pod_provision",
    "_pod_provision_dismiss",
    "_pod_restart",
    "_pod_token",
    "_pod_up",
    "_prunable",
    "_prune_candidates",
    "_prune_dead_sync_base_refs",
    "_prune_run",
    "_prune_status",
    "_prune_task",
    "_read_pin_strict",
    "_reaper_task",
    "_rebase",
    "_rebase_locked",
    "_reclaim_pod_locked",
    "_refresher_task",
    "_status_refresher",
    "_sync",
    "_sync_base_ref",
    "_sync_start_locked",
    "_venv_python",
    "_warm_task",
    "_worktree_remove",
    "_worktree_remove_locked",
    "_wt_lock",
)
