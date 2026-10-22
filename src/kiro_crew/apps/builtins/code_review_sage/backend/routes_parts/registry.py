"""The durable run registry and the per-change claims.

Persisting and restoring ``runs.json``, retention by position, the live-run
predicate, the orphan run-dir reap, and the in-flight claims that keep two runs
off one pull request and one staging path. New run-registry rules go here.

Composed onto :mod:`kiro_crew.apps.builtins.code_review_sage.backend.routes`;
see this package's ``__init__``.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.apps.builtins.code_review_sage.backend.routes import (
        _INFLIGHT,
        _LOCK,
        _PROGRESS_LOCK,
        _RUNS,
        _RUNS_MAX,
        _STAGE_OWNER,
        atomic_write,
        logger,
        results,
        review_driver,
        store,
    )


def _make_progress(run: dict):
    """Build a thread-safe progress callback the driver calls as each change moves
    through its phases (queued -> gating -> deep -> done/blocked/failed). Updates
    are copy-on-write so the /runs reader never sees a half-mutated dict."""

    def cb(change_id: str, phase: str, extra: dict | None = None) -> None:
        with _PROGRESS_LOCK:
            prog = dict(run.get("progress") or {})
            entry = {"phase": phase}
            if extra:
                entry.update(extra)
            prog[str(change_id)] = entry
            run["progress"] = prog

    return cb


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# --- Durable job-status store -------------------------------------------------
# The run registry is the ONLY thing we persist: it lets the page reflect the
# current/last status of review jobs across navigation AND gateway restarts,
# instead of starting from scratch. Per-change result records are transient
# scratch the driver clears after each run — they are NOT a durable store and
# are never used for cross-run dedup (re-reviewing the same change is expected).


def _runs_file() -> Path:
    return store.data_dir() / "runs.json"


def _write_runs(payload: str) -> None:
    """Blocking half of :func:`_save_runs` — never call this on the event loop.

    This whole function is a blocking syscall sequence — mkdir, temp creation,
    lockdown (in-process per ``platform_compat``), payload write, replace — and
    filesystem calls can stall on a slow volume, so it belongs in a worker
    thread (the same reason ``_write_review_section`` and
    ``store.remove_run_dir`` are offloaded).
    """
    f = _runs_file()
    # Guarded rather than a bare `mkdir(parents=True)`: the write below refuses a
    # linked parent, but a bare mkdir has already BUILT the tree inside the link's
    # target by the time it runs, so the refusal arrives after the directories
    # exist where the planter wants them. The refusal has to come first.
    store.mkdir_refusing_links(f.parent)
    # The shared helper replaces through ``replace_with_retry``, so a transient
    # Windows sharing violation does not silently lose the save. It also
    # picks a random mkstemp name, applies the owner-only lockdown to the temp
    # BEFORE the payload lands, and refuses to follow a planted parent link --
    # the three properties the review worker's writable tree requires, since it
    # is prompt-injectable and shares this directory.
    atomic_write(f, payload, restrict_to_owner=True)


async def _save_runs() -> None:
    """Atomically persist the run registry (owner-only). Never raises.

    The registry is serialized HERE, on the loop, so the payload is a consistent
    snapshot taken under whatever lock the caller holds; only the file write and
    the lockdown are offloaded. Serializing inside the thread instead would let
    ``_RUNS`` mutate mid-iteration.
    """
    try:
        payload = json.dumps(_RUNS, indent=2)
        await asyncio.to_thread(_write_runs, payload)
    except Exception:  # pragma: no cover - defensive
        logger.warning("failed to persist runs.json", exc_info=True)


def _load_runs() -> None:
    """Load persisted runs on startup. Any run still marked ``running`` is
    re-marked ``interrupted`` — its in-process driver thread did not survive the
    restart, so it can't be resumed and must not show as live.

    The posting phase needs the same recovery for the same reason, and it is a
    separate flag: ``posting`` is persisted while the delivery task runs, but that
    task is an ``asyncio`` task in this process, so a restart leaves the flag set
    with nothing driving it. Left alone the run is stranded for good — the post
    endpoint 409s on ``already_posting``, ``_is_live`` keeps retention from
    evicting it, and delete refuses it. Clearing the flag here is safe because
    ``posted_keys`` is only written on delivery evidence, so whatever actually
    landed stays recorded and ``_pending_comment_count`` offers exactly the
    remainder on the next post.
    """
    global _RUNS
    try:
        f = _runs_file()
        if not f.is_file():
            return
        data = json.loads(f.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            return
        for r in data:
            if not isinstance(r, dict):
                continue
            if r.get("status") == "running":
                r["status"] = "interrupted"
                r["error"] = "Interrupted by a gateway restart — re-run the review."
                r.setdefault("finished_at", _now())
            if r.get("posting"):
                r["posting"] = False
                r["post_error"] = (
                    "Posting was interrupted by a gateway restart — comments already "
                    "delivered are marked as sent; post again to send the rest."
                )
        _RUNS = data[:_RUNS_MAX]
    except Exception:  # pragma: no cover - defensive
        logger.warning("failed to load runs.json", exc_info=True)


def _is_live(run: dict) -> bool:
    """True while a run still owns its on-disk subtree.

    Two phases are live, and they are NOT the same condition. ``status ==
    "running"`` covers the review; posting happens AFTER the run reaches a terminal
    status, so a run that is mid-delivery reports a terminal status while
    ``posting`` is true. The delete handler has always checked both (its own
    comment: "Posting runs on a TERMINAL run, so the status check above does not
    cover it"); retention checked only the first, so an actively-posting run could
    be evicted and have its directory deleted while the poster was still writing to
    the pull request — losing the record of what landed.

    One predicate so the two callers cannot drift apart again.
    """
    return str(run.get("status") or "") == "running" or bool(run.get("posting"))


async def _record(run: dict) -> None:
    async with _LOCK:
        _RUNS.insert(0, run)
        # Runs that fall off the end of the registry take their on-disk subtree
        # with them. Without this, every review would leave a results+report dir
        # behind forever — the registry is bounded but the disk would not be.
        #
        # Retention is by POSITION, so with enough concurrent reviews the oldest
        # entry past the cap can still be LIVE. Evicting it deleted the subtree
        # underneath a live review: its endpoints started 404ing and the report it
        # was about to write was orphaned. Only terminal runs are evictable; a live
        # one is kept even if that pushes the registry past the cap, and it becomes
        # evictable on the next _record after it finishes.
        keep: list[dict] = []
        evicted: list[str] = []
        for i, r in enumerate(_RUNS):
            if i < _RUNS_MAX or _is_live(r):
                keep.append(r)
            else:
                evicted.append(str(r.get("run_id") or ""))
        _RUNS[:] = keep
        await _save_runs()
    for run_id in evicted:
        if run_id:
            await asyncio.to_thread(store.remove_run_dir, run_id)


def _find_run(run_id: str) -> dict | None:
    """Locate a run in the registry by id (exact match)."""
    for r in _RUNS:
        if str(r.get("run_id") or "") == run_id:
            return r
    return None


def _reap_orphan_run_dirs() -> int:
    """Delete run subtrees with no corresponding registry entry.

    Covers the crash window where a run dir was created but the registry write
    that would own it never landed, and any residue from an older layout. Called
    once at startup; never raises."""
    try:
        known = {store.safe_run_id(str(r.get("run_id") or "")) for r in _RUNS}
        removed = 0
        for on_disk in store.list_run_ids():
            if on_disk not in known:
                if store.remove_run_dir(on_disk):
                    removed += 1
        return removed
    except Exception:  # pragma: no cover - defensive
        logger.debug("code-review-sage: orphan run-dir reap failed", exc_info=True)
        return 0


def _stage_key(change_id: str) -> str:
    """The identity the SHARED staging path collapses a change id down to.

    ``results.safe_change_id`` maps every character outside ``[A-Za-z0-9._-]`` to
    ``_``, so two DIFFERENT changes can name one staging file — e.g. ``owner/repo#5``
    and ``owner_repo#5`` both stage as ``owner_repo_5.json``. The reviewed key is
    faithful, so claiming on it alone would let both run at once and let one
    worker's record be adopted by the other's run: the silent-empty-report failure
    this module exists to prevent. Claim this coarser identity as well, so changes
    that share a staging path are serialized while everything else stays parallel.
    """
    return "stage:" + results.safe_change_id(change_id)


def _claim_changes_under_lock(run: dict, changes: list[str]) -> list[str]:
    """Decide which changes this run OWNS, and claim them.

    Two filters, both required, applied while ``_RUN_LOCK`` is held:

    1. **Already in flight.** Drop any change another LIVE run has claimed, under
       either its faithful reviewed key or its (lossy) staging key. Runs execute
       concurrently now, so without this two runs started seconds apart against the
       same repo would both review and both post to the same PR.
    2. **Already reviewed at this head.** Re-filter against the CURRENT reviewed
       index, dropping PRs a run that finished after this one was queued has since
       delivered. (Skipped for forced runs and pasted-link runs, which carry no
       ``head_shas``.)

    Keeps ``run['changes'|'change_ids'|'head_shas']`` consistent with the kept
    subset so ``_record_reviewed`` still round-trips. Sync (reads the index) — call
    via a thread from the event loop.
    """
    run_id = str(run.get("run_id") or "")
    head_shas = run.get("head_shas") or {}
    forced = bool(run.get("force"))
    index = {} if (forced or not head_shas) else results.read_reviewed()

    kept: list[str] = []
    kept_shas: dict[str, str] = {}
    seen_rkeys: set[str] = set()
    skipped_inflight = 0
    for url in changes:
        rkey = review_driver.reviewed_key_for(url)
        skey = _stage_key(review_driver.change_id_for(url))
        # Drop a change this batch already kept. The claim checks below all
        # (correctly) let the SAME run re-claim a key it owns, so without this a
        # repeated URL -- or two spellings that map to one reviewed key -- passes
        # every gate and lands in `kept` twice. Two workers then review one change
        # through one staging path, and the second write overwrites or consumes the
        # first, so the run reports success with a report missing a change.
        #
        # Not counted as `skipped_inflight`: that number means another run holds the
        # claim, and surfacing a caller's duplicate as contention would misreport
        # why a change was dropped.
        if rkey in seen_rkeys:
            continue
        seen_rkeys.add(rkey)
        # A live claim on the faithful reviewed key by ANOTHER run blocks it.
        rkey_owner = _INFLIGHT.get(rkey)
        if rkey_owner is not None and rkey_owner != run_id:
            skipped_inflight += 1
            continue
        # Then the staging key across runs. This read matters on its own: the
        # POSTING path claims `_INFLIGHT[skey]` without recording a
        # `_STAGE_OWNER` (it holds a change id, not a reviewed key), so checking
        # only `_STAGE_OWNER` let a review claim a change whose staging file a
        # live post was mid-round-trip through — the two then swapped records.
        skey_owner = _INFLIGHT.get(skey)
        if skey_owner is not None and skey_owner != run_id:
            skipped_inflight += 1
            continue
        # Finally the staging key WITHIN this run: exempt only for the SAME change
        # re-claiming it, so two changes sharing a staging path still collide.
        stage_owner = _STAGE_OWNER.get(skey)
        if stage_owner is not None and stage_owner != rkey:
            skipped_inflight += 1
            continue
        if head_shas and not forced:
            intended = head_shas.get(rkey, "")
            rec = index.get(rkey) or {}
            if intended and rec.get("head_sha") == intended:
                continue  # a concurrent run already reviewed this exact head
        _INFLIGHT[rkey] = run_id
        _INFLIGHT[skey] = run_id
        _STAGE_OWNER[skey] = rkey
        kept.append(url)
        if rkey in head_shas:
            kept_shas[rkey] = head_shas[rkey]

    run["changes"] = kept
    run["change_ids"] = [review_driver.change_id_for(c) for c in kept]
    run["head_shas"] = kept_shas
    if skipped_inflight:
        run["skipped_inflight"] = skipped_inflight
    return kept


def _release_claims(run: dict) -> None:
    """Drop this run's in-flight claims. Must run for EVERY terminal path —
    including failure and cancellation — or the claimed PRs become permanently
    unreviewable until the gateway restarts."""
    run_id = str(run.get("run_id") or "")
    for key, owner in list(_INFLIGHT.items()):
        if owner == run_id:
            _INFLIGHT.pop(key, None)
            # Staging ownership is released with the claim it belongs to,
            # otherwise the path stays permanently unclaimable.
            _STAGE_OWNER.pop(key, None)
