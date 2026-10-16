"""Pod, worktree, sync, rebase, and prune operations for Dev Fleet."""

from __future__ import annotations

import asyncio
import importlib as _importlib
import os
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

from kiro_crew import _facade_mirror
from kiro_crew.apps.builtins.dev_fleet import fleet_state, repository, runtime
from kiro_crew.apps.builtins.dev_fleet.worktree_ops_pods import _wt_lock
from kiro_crew.apps.builtins.dev_fleet.worktree_ops_removal import _worktree_remove
from kiro_crew.loop_lock import LoopBoundLock

# --- rebase ---


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
        # Same fileless refusal the removal path gives. Name the dirt so
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
    # Re-resolved HERE, not inherited from discovery. Discovery latches once per
    # process (`_DISCOVERY_DONE`), so a base resolved at startup is the only answer the
    # process would ever hold -- and a refusal would then not clear until a restart. A
    # few short git reads on an operation that already fetches is what makes that
    # promise true.
    #
    # Resolved into LOCALS, never into the shared `repository.BASE_BRANCH` global.
    # `_sync_start_locked` checks `HEAD == BASE_BRANCH` and then re-reads that global
    # across several awaits before it fetches and merges; a rebase mutating the global
    # in that window (default main->trunk) would make the sync fetch and merge a base
    # it never validated, holding only `_wt_lock` and never `_SYNC_LOCK`. Keeping the
    # answer local removes the shared mutable state the two operations contended over,
    # rather than serializing two unrelated subsystems under one lock: this rebase acts
    # only on the base it itself resolved, and the sync's global is left untouched.
    base_branch, base_positive, base_remote = await repository._resolve_base_snapshot()
    if base_branch is None:
        base_branch = repository.BASE_BRANCH
    # Before the fetch, and before anything is rewritten: a base branch nobody stated
    # is a guess, and this is the one operation here that cannot be undone from its own
    # result -- a clean replay onto the wrong base returns ok and names no rollback.
    # The dirt gate above already refuses on the cheaper hazard. Checked against the
    # LOCAL snapshot, so the verdict is about the base this rebase will act on.
    base_refusal = repository.base_branch_mutation_refusal(base_branch, base_positive)
    if base_refusal is not None:
        return {"ok": False, "error": base_refusal}
    # The SAME remote the snapshot verified the base against -- not a remote re-derived
    # from `branch.<base>.remote`, which can name a different one (`origin` advertises
    # `main` while `branch.main.remote = upstream`) and let a clean replay rewrite the
    # worktree onto a base the positive verdict never checked. Base and remote are one
    # snapshot; the fetch and the rebase use its remote.
    remote = base_remote
    if await repository._git(path, "fetch", remote, base_branch, timeout=90) is None:
        return {"ok": False, "error": f"git fetch {remote} {base_branch} failed"}
    rc, stdout, stderr = await runtime._run_cmd(
        ["git", "-C", path, "rebase", f"{remote}/{base_branch}"],
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
    # The per-worktree verdict (_prunable) is several read-only git calls plus
    # one or more networked gh calls (a PR-status lookup, its per-repo fallback
    # traversal, and a merged/closed head-OID check), so a plain serial loop
    # scales with fleet size: a 111-worktree fleet floors at ~57s even at one gh
    # call each, double the gateway app proxy's 30s _PROXY_TIMEOUT, so the
    # preview returns 504 and the button never renders. Run the verdicts
    # concurrently under the same _PRUNE_CONCURRENCY bound the parallel prune
    # workers use -- this path is read-only git (rev-parse, status,
    # rev-list/cherry, merge-base) so it never touches _GIT_MUTATION_LOCK, which
    # only the destructive removal path holds.
    prunable = [w for w in worktrees if not w.get("is_main")]
    sem = asyncio.Semaphore(_PRUNE_CONCURRENCY)

    async def _verdict(w: dict) -> tuple[dict, dict]:
        async with sem:
            v = await _prunable(w["path"], w.get("branch"))
        return w, v

    # gather preserves the argument order in its result list regardless of
    # completion order, so candidates/kept below stay deterministic (input
    # discovery order) even though the verdicts finish out of order.
    verdicts = await asyncio.gather(*(_verdict(w) for w in prunable))

    candidates, kept = [], []
    for w, v in verdicts:
        name = Path(w["path"]).name
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
    return {"ok": True, "candidates": candidates, "kept": kept, "scanned": len(prunable)}


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
        # every cycle forever. A checkout found LATER restarts it from
        # ``_ensure_repo_resolved``, which is the only thing that can change this
        # answer inside one process.
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


async def _ensure_repo_resolved() -> None:
    """Re-run main-checkout discovery while no VALID checkout is resolved, and start the
    status refresher when an attempt resolves one.

    ``/api/fleet`` calls this per poll. A resolved and valid install returns at the
    first guard before any await, so the cost is paid only in the two states that have
    no fleet to serve -- nothing found, and a configured path carrying no markers --
    and the setup card or banner the user is looking at stops being a lie within one
    poll instead of at the next gateway restart.

    The refresher start lives here rather than in ``repository`` because the loop
    and its task handle are owned at this level and ``repository`` sits below it in
    the component DAG. Starting it is not optional bookkeeping: ``_status_refresher``
    RETURNS when there is no usable checkout rather than idling, so a late resolution
    that left it stopped would serve a fleet whose rows never refresh again — the setup
    card disappears, the page looks alive, and nothing fetches. That is a worse
    state than the honest "restart the gateway" this replaces.
    """
    global _refresher_task
    # A resolved and VALID checkout returns here before any await. A resolved-but-invalid
    # one falls through, because the operator can still correct the path its banner names
    # and `repository` reopens that latch once the configured string changes.
    if repository.MAIN_REPO and not repository._REPO_INVALID_MSG:
        return
    await repository.ensure_main_repo_discovered()
    try:
        repository._repo()
    except repository.RepoUnavailable:
        # Nothing found, or the path found carries no markers. Asked through the
        # accessor rather than by testing `MAIN_REPO` for truthiness: an invalid path is
        # truthy, and `_status_refresher` returns on its first line for one, so starting
        # it would mint a task per poll that dies immediately.
        return
    if _background_tasks_disabled():
        return
    if _refresher_task is None or _refresher_task.done():
        _refresher_task = asyncio.create_task(_status_refresher())


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
    "_ensure_repo_resolved",
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


# This module is the facade of Dev Fleet's worktree operations, and every name is reached
# through it as ``worktree_ops.<name>`` (``server.py`` re-exports ``__all__``): a read is
# served by the owner that binds the name, and a write -- ``monkeypatch.setattr``,
# ``mock.patch`` -- reaches every owner holding it (:mod:`kiro_crew._facade_mirror`). Owners
# never import this module. Rebase, prune and the background tasks stay here; new work goes
# to the owner of its responsibility:
#   ``worktree_ops_sync`` -- Pull + Build sync of the main checkout
#   ``worktree_ops_pods`` -- pod actions, and the per-worktree lock ``_wt_lock``
#   ``worktree_ops_removal`` -- removing one worktree, and ``_GIT_MUTATION_LOCK``

#: Owner modules, lowest layer first: the first holder of a name answers its reads.
_OWNERS = (
    "kiro_crew.apps.builtins.dev_fleet.worktree_ops_sync",
    "kiro_crew.apps.builtins.dev_fleet.worktree_ops_pods",
    "kiro_crew.apps.builtins.dev_fleet.worktree_ops_removal",
)
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Name the facade serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(_facade_mirror.install(sys.modules[__name__], _OWNERS, excluded=()).served)
sys.modules[__name__].__class__ = _facade_mirror.MirroredModule

if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.apps.builtins.dev_fleet.worktree_ops_pods import (  # noqa: F401
        _POD_START_REFUSAL,
        _WT_LOCKS,
        _is_number,
        _mint_pod_token_locked,
        _pod_checkout_guard,
        _pod_down,
        _pod_env,
        _pod_logs,
        _pod_ls,
        _pod_provision,
        _pod_provision_dismiss,
        _pod_restart,
        _pod_status,
        _pod_token,
        _pod_up,
        _pod_up_locked,
        _read_pin_strict,
        _reclaim_pod_locked,
        _token_windows,
    )
    from kiro_crew.apps.builtins.dev_fleet.worktree_ops_removal import (  # noqa: F401
        _GIT_MUTATION_LOCK,
        _GIT_WORKTREE_REMOVE_TIMEOUT_SECS,
        _LEASE_REFUSAL_PREFIX,
        _worktree_remove_locked,
    )
    from kiro_crew.apps.builtins.dev_fleet.worktree_ops_sync import (  # noqa: F401
        _SYNC_RID,
        _SYNC_RUNNER_BOOTSTRAP,
        _frontend_build_steps,
        _prune_dead_sync_base_refs,
        _sync,
        _sync_base_ref,
        _sync_start_locked,
        _venv_python,
    )

if TYPE_CHECKING:  # names it serves from outside the owner family, from their homes
    from kiro_crew import (  # noqa: F401
        dep_sync,
        frontend,
        hooks,
        platform_compat,
    )
    from kiro_crew.apps.builtins.dev_fleet import (  # noqa: F401
        live,
        npm_preflight,
    )
    from kiro_crew.executors import (  # noqa: F401
        subprocess_executor,
    )
    from kiro_crew.git_config_hooks import (  # noqa: F401
        ConfigHookScanError,
    )
    from kiro_crew.sandbox import (  # noqa: F401
        sandboxed_spawn_argv,
        shielded_prepare_off_loop,
    )
