"""Removing one Dev Fleet worktree.

``_worktree_remove`` takes the worktree's lock and runs the gated removal: the
live-target and removal-lease checks, the pod shutdown and pin re-verification, the
PR-shipped and dirt gates, and the ``git worktree remove`` itself under
``_GIT_MUTATION_LOCK``, which every removal path (the single remove, prune workers,
the auto-prune reaper) shares. Reached through ``worktree_ops``, its facade.
"""

from __future__ import annotations

import asyncio
from typing import Callable

from kiro_crew.apps.builtins.dev_fleet import fleet_state, live, repository, runtime
from kiro_crew.apps.builtins.dev_fleet.worktree_ops_pods import _reclaim_pod_locked, _wt_lock
from kiro_crew.executors import subprocess_executor
from kiro_crew.loop_lock import LoopBoundLock

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
#: Ceiling on ``git worktree remove``. ``live._REMOVAL_LEASE_GRACE_SECS`` must exceed it:
#: a lapsed removal lease keeps excluding cutovers/restarts for the grace barrier, and the
#: barrier is only a guarantee while the mutation it shields cannot outlive it. A test
#: pins the ordering so a bump here cannot silently reopen the overlap window.
_GIT_WORKTREE_REMOVE_TIMEOUT_SECS = 60
#: Every refusal that stems from the gateway not vouching for the removal starts with
#: this consequence-first sentence; the post-spawn interpretation matches on it.
_LEASE_REFUSAL_PREFIX = (
    "refusing: the gateway could not confirm that no cutover overlaps this removal"
)


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
                "refusing: a rebase or pod start is in progress for this worktree -- "
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

    # Pointer state comes from the gateway (the pointer file is masked from this
    # backend). An outage there is NOT "nothing is live": refusing is the only
    # answer that cannot delete the checkout a cutover is staged on.
    try:
        live_path = await live._live_worktree_path(fresh=True)
    except live.PointerUnavailable as exc:
        runtime.logger.warning(
            "worktree remove: live-target state unavailable: %s", runtime._redact(str(exc))
        )
        return {
            "ok": False,
            "error": (
                "refusing: cannot verify which checkout is live or staged "
                "-- retry when the gateway answers"
            ),
        }
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
    #   - dirty is not False → refuse outright
    #   - dirty is False → drop --force so git's own dirty check is the
    #     atomic last line against edits arriving in the check-to-removal
    #     window (mirrors the unmerged-clean TOCTOU pattern)
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
    #
    # ``_MAKE_LIVE_LOCK`` serialises removals against each other in THIS
    # process, but the cutover (and the gateway restart that tree-kills this
    # backend) runs in the GATEWAY process, so the gateway-held removal LEASE is
    # what actually excludes them (live.py). The lease is refused while a
    # cutover is in flight, and a broker outage is a refusal too: this removal
    # cannot prove no cutover is staging the very path it is about to delete.
    async with live._MAKE_LIVE_LOCK, live.removal_lease(path) as _leased:
        if not _leased:
            # Two causes with opposite remedies, so one assertive message each.
            if _leased.refusal == "unavailable":
                return {
                    "ok": False,
                    "error": (
                        "refusing: can't reach the gateway to check for a staged cutover "
                        "-- confirm it's running, then retry"
                    ),
                }
            return {
                "ok": False,
                "error": (
                    "refusing: a Make Live cutover is in progress or this worktree's pod "
                    "is starting -- retry once it has completed"
                ),
            }
        # Protection re-check under the lock closes the TOCTOU window between
        # the eager checks at function entry and the actual deletion.
        try:
            _live2 = await live._live_worktree_path(fresh=True)
            _staged2 = await live._staged_target_resolved()
        except live.PointerUnavailable as exc:
            runtime.logger.warning(
                "worktree remove: live-target state unavailable: %s", runtime._redact(str(exc))
            )
            return {
                "ok": False,
                "error": (
                    "refusing: cannot verify which checkout is live or staged "
                    "-- retry when the gateway answers"
                ),
            }
        if _live2 is not None and repository._same_path(path, _live2):
            return {
                "ok": False,
                "error": (
                    "refusing: this worktree became the live gateway -- "
                    "switch the gateway to another checkout first"
                ),
            }
        if _staged2 is not None and repository._same_path(path, _staged2):
            return {
                "ok": False,
                "error": (
                    "refusing: this worktree is a staged Make Live cutover "
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
            loop = asyncio.get_running_loop()
            try:
                await loop.run_in_executor(subprocess_executor(), runtime.rt.require_backend)
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
            except runtime.rt.PodError as exc:
                return {
                    "ok": False,
                    "error": f"cannot verify pod backend: {runtime._redact(str(exc))}",
                }
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
            # The removal lease excludes a gateway cutover/restart from landing on
            # this worktree, and it is heartbeated while we queued here. If a
            # renewal was REFUSED (the gateway restarted and forgot the lease), the
            # exclusion is gone: stop before the mutation rather than inside it.
            if live.removal_lease_lost(path):
                return {
                    "ok": False,
                    "error": (f"{_LEASE_REFUSAL_PREFIX} -- nothing was changed; retry the removal"),
                }
            # TOCTOU recheck: the pod-inactive verification above happened BEFORE
            # this lock was acquired. Under parallel prune a worker can queue here
            # behind other removals — long enough for another session to restart
            # the pod. Removing the checkout under a live pod would leave its
            # gateway running from deleted files, so re-verify inactivity now.
            if runtime._POD_AVAILABLE and cfg:
                loop = asyncio.get_running_loop()
                try:
                    await loop.run_in_executor(subprocess_executor(), runtime.rt.require_backend)
                except runtime.rt.PodBackendAbsent:
                    pass  # backend provably absent — no pods can exist
                except runtime.rt.PodError as exc:
                    return {
                        "ok": False,
                        "error": (
                            "cannot re-verify pod backend before removal: "
                            f"{runtime._redact(str(exc))}"
                        ),
                    }
                else:
                    try:
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
            # Execute the approved untracked-only discard. Once it runs, work the
            # user approved for deletion is gone even if the removal itself is later
            # refused — so when a discard is pending THIS is the point of no return,
            # and the lease is proven fresh here (a renewal through the gateway, not
            # the possibly-stale heartbeat view) before anything is destroyed. The
            # git spawn below re-proves it through the pre-spawn gate; a refusal
            # THERE, after the discard, is reported as such rather than as "retry".
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
            discard_ran = False
            if pending_discard is not None:
                if not await live.confirm_removal_lease(path):
                    return {
                        "ok": False,
                        "error": (
                            f"{_LEASE_REFUSAL_PREFIX}, so the approved untracked files "
                            "were left in place -- nothing was deleted; retry the removal"
                        ),
                    }
                discard_ran = True
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
                    # A per-file helper can fail on the Nth entry after deleting
                    # N-1, and new dirt can appear after every approved file is
                    # gone: neither refusal is a no-op, so count what is already
                    # deleted and say it.
                    gone = await loop.run_in_executor(
                        subprocess_executor(),
                        repository._count_missing,
                        path,
                        pending_discard,
                    )
                    runtime.logger.info(
                        "worktree_removal_audit: worktree=%s branch=%s caller=%s "
                        "approved_discard=%s discard_err=%s tracked_after=%s "
                        "untracked_after=%s already_deleted=%s "
                        "action=refused_discard_incomplete",
                        name,
                        branch,
                        _caller,
                        len(pending_discard),
                        bool(discard_err),
                        post_tracked,
                        len(post_untracked),
                        gone,
                    )
                    _reason = discard_err or (
                        "the worktree is not clean after the discard: "
                        f"{len(post_untracked)} untracked file(s) remain"
                        + (", and tracked files changed" if post_tracked else "")
                    )
                    _already = (
                        f"{gone} of {len(pending_discard)} approved untracked "
                        "file(s) were already deleted; "
                        if gone
                        else "no approved file was deleted; "
                    )
                    return {
                        "ok": False,
                        "error": f"{_reason} -- {_already}removal aborted",
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

            # The point of no return is guarded by an explicit lease RENEWAL, run by
            # ``_run_cmd`` after its sandbox-preparation hop and immediately before
            # the child is spawned — nothing of unbounded duration sits between the
            # proof and the mutation. A renewal that succeeds means the gateway
            # excludes cutovers/restarts from this worktree for a TTL, and for the
            # grace barrier beyond that should this process die mid-mutation.
            async def _lease_gate() -> str | None:
                if await live.confirm_removal_lease(path):
                    return None
                return f"{_LEASE_REFUSAL_PREFIX} -- git was not run; retry the removal"

            # Run the destructive mutation uninterruptibly. On gateway
            # shutdown dev_fleet_cleanup cancels the prune worker; a naive
            # cancel would either SIGKILL the child (via _run_cmd's handler) or,
            # with a bare shield, unwind this frame and release
            # _GIT_MUTATION_LOCK / _MAKE_LIVE_LOCK while the detached child is
            # still writing. _run_uninterruptible holds this frame -- and the
            # locks -- until the timeout-bounded `git worktree remove` has
            # finished, then lets the cancellation propagate at that safe point.
            rc, stdout, stderr = await runtime._run_uninterruptible(
                runtime._run_cmd(
                    cmd, timeout=_GIT_WORKTREE_REMOVE_TIMEOUT_SECS, pre_spawn=_lease_gate
                )
            )
            if rc != 0:
                # The pre-spawn lease gate refused: git never ran, so this is not
                # a git failure and must not be read as "dirty at removal". With
                # no discard pending nothing was destroyed and a plain refusal is
                # right; after a discard it falls through to the discard-aware
                # report below, which says what is already gone.
                if stderr.startswith(_LEASE_REFUSAL_PREFIX) and not discard_ran:
                    return {"ok": False, "error": stderr}
                # When the removal runs without --force (TOCTOU guard for
                # clean-unmerged override), a git refusal means the tree became
                # dirty in the window — surface it as a specific audit event. A
                # lease-gate refusal is not one: git never ran.
                lease_refused = stderr.startswith(_LEASE_REFUSAL_PREFIX)
                if force and not force_use_git_force and not lease_refused:
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
        # A removed checkout materially changes worktree disk use, and this is
        # the chokepoint every removal path already routes through: drop the
        # disk cache's freshness stamp so the next /disk poll re-aggregates
        # instead of serving pre-removal totals for the rest of the TTL.
        fleet_state._disk_invalidate()
        return {
            "ok": True,
            "removed": True,
            "stopped_pod": stopped_pod,
            "reclaimed_pod_home": reclaimed_pod_home,
            "pr": runtime._redact_pr(pr),
        }
