"""Deterministic backend route for the Code Review Sage builtin.

Kicks off the two-stage review driver **in-process** (no LLM orchestrator), so
the Phase 1 -> Phase 2 switch AND the finalize step (report -> archive -> clean)
are guaranteed to run as Python, never left to an LLM session that can deviate.
The only LLM work is the per-change gate/deep tasks the driver dispatches to a
reusable worker pool (``sage_lib/review_pool.py``) — long-lived ACP sessions reused
across CRs, not per-CR ``/api/spawn`` sub-agents — so reviews run silently.

Why this exists: when an LLM orchestrator session was merely *asked* to shell
``review_driver.py``, it could instead run Phase 1 inline and stop — leaving a
gate-only record, a stale report, and no Phase 2 (observed in practice).
Routing the kickoff through this route removes that discretion entirely.

Registered at gateway startup by ``apps/routes.py:register_app_routes`` (loaded
by file path because the app dir name ``code-review-sage`` is hyphenated and
cannot be imported as a Python package).

Routes (browser-facing, same-origin authed exactly like ``/config``):
  POST /api/apps/code-review-sage/review   {links | changes} -> {run_id, changes}
  GET  /api/apps/code-review-sage/runs                       -> {runs: [...]}

The ``/runs`` registry lets the page render live status and reconstruct it after
navigating away (the backend owns the run, not ephemeral React state). Per-change
detail still comes from the on-disk result records the driver writes.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from aiohttp import web

from kiro_crew import hooks, model_registry

logger = logging.getLogger("kirocrew.app.code-review-sage")

# The app root (.../apps/builtins/code-review-sage) holds ``sage_lib/`` next to this
# ``backend/`` dir. Put it on sys.path so ``from sage_lib import review_driver`` works
# the same way the driver resolves its own siblings.
_APP_ROOT = Path(__file__).resolve().parent.parent
if str(_APP_ROOT) not in sys.path:
    sys.path.insert(0, str(_APP_ROOT))

# Sibling app modules — importable now that the app root is on sys.path (the app
# dir is hyphenated, so these are not auto-discovered packages). Imported at the
# top per the top-level-imports guideline; the sys.path setup above executes at
# module load, so this resolves on first import.
from sage_lib import (  # noqa: E402,E501
    adapters,
    discovery,
    learning,
    pipeline,
    report,
    results,
    review_driver,
    review_pool,
    store,
)

# In-memory run registry, most-recent first. Bounded so it can't grow unbounded.
# Holds lightweight run descriptors the page polls; on-disk result records carry
# per-change detail.
_RUNS: list[dict[str, Any]] = []
_RUNS_MAX = 25
_LOCK = asyncio.Lock()
# Guards the claim/dedup step below. Runs themselves are NOT serialized: each run
# owns a private ``data/runs/<run_id>/`` subtree (results + report), so several
# reviews can be in flight at once. What still needs mutual exclusion is the
# moment a run decides WHICH changes it owns.
# Serializes whole runs. Workers hand results back through a directory shared
# ACROSS runs, so overlapping runs would mean two writers to one path.
_RUN_LOCK = asyncio.Lock()

# Serialises "start a consolidation" against "delete this namespace". Both are
# short critical sections; holding one lock across each removes the interleaving
# rather than trying to place checks around the awaits.
_NS_OPS_LOCK = asyncio.Lock()
# reviewed-key -> run_id for every change a LIVE run has claimed. Two runs must
# never review and post to the same PR concurrently: the old whole-run lock
# prevented that by refusing to overlap at all; this claim registry gets the same
# guarantee while letting unrelated runs proceed in parallel. It also closes a gap
# the old lock never covered — a pasted-link run overlapping a repo run.
_INFLIGHT: dict[str, str] = {}
# staging-key -> the reviewed key that owns it. `_INFLIGHT` alone cannot express
# this: exempting a run's own claims (needed so re-claiming is idempotent) also
# exempted a SECOND change in the same run whose lossy id collapsed onto the same
# staging file — both were kept, both workers wrote one path, and one review was
# adopted under the wrong pull request. Ownership is per CHANGE, not per run.
_STAGE_OWNER: dict[str, str] = {}
# Run ids the user asked to cancel. The driver polls this between changes.
_CANCELLED: set[str] = set()
# Guards copy-on-write updates to a run's per-change ``progress`` map, which the
# (threaded) driver writes and the /runs handler reads concurrently.
_PROGRESS_LOCK = threading.Lock()
# Keep strong refs to background tasks so they aren't garbage-collected mid-flight.
_TASKS: set[asyncio.Task] = set()  # type: ignore[type-arg]
# Dashboard state, captured at route registration. A finished run notifies the
# bell feed, and that happens in a background task with no ``request`` in scope —
# so the state has to be reachable without one. Populated lazily and treated as
# optional everywhere (tests register routes on a bare aiohttp app).
_APP_STATE: dict[str, Any] = {}


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


def _save_runs() -> None:
    """Atomically persist the run registry (0600). Never raises."""
    try:
        f = _runs_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_name(f.name + ".tmp")
        tmp.write_text(json.dumps(_RUNS, indent=2), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, f)
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
                    "delivered are marked as sent; post again to send the rest.")
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
        _save_runs()
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
                continue   # a concurrent run already reviewed this exact head
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


async def _run_review_bg(run: dict, changes: list[str]) -> None:
    """Run the (blocking) driver in a worker thread; update the run record.

    The driver owns the whole lifecycle: review task -> report -> archive ->
    clean. Each task is dispatched to the shared, reusable worker pool
    (``review_pool``) — long-lived ACP sessions, not per-CR ``/api/spawn``
    sub-agents — so reviews run silently (no agent card, no ``:lock:``, no Slack
    relay) and warm workers are reused across CRs with a clean-slate reset
    between them.

    Whole runs ARE serialized against each other, and reviews within a run run one
    at a time. Neither is a performance choice: results come back through
    ``data/results/<change_id>.json``, which is SHARED across runs, so two live runs
    are two writers to one path and a prompt-injected worker could get its findings
    adopted by the other run. Run-scoped subtrees narrow that window but do not
    close it, because the hand-back still crosses the shared path. Serializing is
    what makes adoption attributable.

    The cost is real and worth stating: a second review waits for the first. The
    exchange is a shared-path race for latency, and until the hand-back stops going
    through a shared path, latency is the right thing to give up."""
    run_id = str(run.get("run_id") or "")
    try:
        async with _RUN_LOCK:
            # Serialize the WHOLE run, not just the claim. Workers hand results back
            # through `data/results/<change_id>.json`, which is shared across runs
            # (`publish_to_shared` writes to `results_dir(root, None)`), so two live runs
            # means two writers to one path. The claim cannot substitute for this: it
            # coordinates honest runs, while a prompt-injected worker writes whatever
            # change id it likes and a concurrent run then adopts findings it did not
            # produce. Concurrent starts queue here instead of interleaving.
            # Inner guard, still worth holding: two runs reviewing the same PR, and
            # re-reviewing a head a just-finished run already delivered.
            changes = await asyncio.to_thread(_claim_changes_under_lock, run, changes)
            if not changes:
                run["summary"] = {"ok": True, "changes": 0,
                                  "note": "all PRs already reviewed or in flight "
                                          "in a concurrent run"}
                run["status"] = "done"
                return
            # Bridge the threaded driver to the async pool running on THIS (gateway)
            # event loop. The pool is a lazily-created process-wide singleton.
            loop = asyncio.get_running_loop()
            pool = review_pool.get_pool()
            dispatch = review_pool.make_sync_dispatch(loop, pool)

            # Bracket the batch: begin_batch() lazily spawns the ONE shared runtime;
            # end_batch() (in finally) kills it once this run's reviews all drain — so
            # the subprocess (and its memory) lives exactly as long as the batch. The
            # holder is reference-counted, so overlapping runs share one runtime and the
            # last one out tears it down.
            await pool.begin_batch()
            try:
                summary = await asyncio.to_thread(
                    review_driver.run_review, changes,  # type: ignore[attr-defined]
                    dispatch=dispatch, progress=_make_progress(run),
                    run_id=run_id, cancelled=lambda: run_id in _CANCELLED,
                    # One reviewer at a time. Workers share the staging directory and
                    # each has shell and file tools, so two running at once means one
                    # can write another change's record between that change's slot
                    # being cleared and its own worker writing -- attacker-controlled
                    # findings attributed to the victim pull request. Serializing makes
                    # "the record in this slot" mean "written by the worker just
                    # dispatched". Restoring parallelism needs per-dispatch staging
                    # whose path a sibling worker cannot guess.
                    concurrency=1,
                )
            finally:
                await pool.end_batch()
            run["summary"] = summary
            _collect_delivered(run, summary)
            run["report_slug"] = summary.get("report_slug") or run.get("report_slug")
            recorded = int(summary.get("result_records") or 0)
            deep = int(summary.get("deep_reviewed") or 0)
            attempted = int(summary.get("changes") or 0) - int(summary.get("cancelled") or 0)
            if run_id in _CANCELLED:
                run["status"] = "cancelled"
            elif not summary.get("ok"):
                run["status"] = "error"
            elif attempted > 0 and (recorded == 0 or deep == 0):
                # run_review returns ok=True for any run with >=1 change, so a run
                # whose every change failed used to report "done" with an empty
                # report. Nothing was reviewed; say so rather than letting the UI
                # claim success and then show an empty report.
                #
                # `deep == 0` is checked as well as `recorded == 0`: a change can
                # persist a record and still never be deep-reviewed, which cleared the
                # record count while leaving the report with no findings in it. Both
                # are the same "claimed success, delivered nothing" failure.
                run["status"] = "error"
                run["error"] = _first_change_error(summary) or (
                    "the reviewer produced no result record")
            else:
                # "done" even if SOME changes failed — those are surfaced per change.
                run["status"] = "done"
            if not summary.get("ok"):
                run["error"] = summary.get("error", "review failed")
            else:
                # Durable dedup index: record each reviewed PR's head SHA so a later
                # repo-review skips it until its head changes. Only repo-review runs
                # carry head_shas; pasted-link runs skip this (no-op).
                await asyncio.to_thread(_record_reviewed, run)
    except Exception as exc:  # pragma: no cover - defensive
        run["status"] = "error"
        run["error"] = str(exc)
        logger.warning("code-review-sage run %s failed: %s", run_id, exc, exc_info=True)
    finally:
        run["finished_at"] = _now()
        # Claims MUST be released on every terminal path, or the PRs this run
        # touched stay unreviewable for the life of the process.
        async with _RUN_LOCK:
            _release_claims(run)
        _CANCELLED.discard(run_id)
        async with _LOCK:
            _save_runs()
        await _notify_finished(run)


def _first_change_error(summary: dict) -> str:
    """The most useful per-change failure reason, for a run-level error message."""
    for rec in summary.get("per_change") or []:
        for key in ("deep_error", "gate_error", "skipped_reason"):
            val = str(rec.get(key) or "").strip()
            if val:
                return {
                    "no_review_recorded": "the reviewer finished but wrote no "
                                          "findings record",
                    "review_failed": "the review turn failed",
                }.get(val, val)
    return ""


def _run_headline(run: dict) -> str:
    """One-line human description of a run, for notifications and thread titles."""
    repo = run.get("repo")
    n = len(run.get("changes") or [])
    if repo:
        return f"{repo} · {n} PR{'s' if n != 1 else ''}"
    changes = run.get("changes") or []
    if n == 1 and changes:
        return str(changes[0]).rsplit("github.com/", 1)[-1]
    return f"{n} PR{'s' if n != 1 else ''}"


async def _notify_finished(run: dict) -> None:
    """Push a bell notification when a run reaches a terminal state.

    This is the answer to "there's no way to know when the review is done": the
    page polls while you are looking at it, but a review takes minutes and you
    will be elsewhere by the time it lands. Best-effort — a notification failure
    must never affect the run. Cancelled runs stay silent (the user just cancelled
    it; telling them it stopped is noise)."""
    status = run.get("status")
    if status not in ("done", "error"):
        return
    state = _APP_STATE.get("state")
    if state is None or not hasattr(state, "notify"):
        return
    bands = ((run.get("summary") or {}).get("report") or {}).get("bands") or {}
    red, yellow = int(bands.get("red", 0) or 0), int(bands.get("yellow", 0) or 0)
    if status == "error":
        title = "Code review failed"
        body = f"{_run_headline(run)} — {run.get('error') or 'the run did not complete'}"
    else:
        parts = []
        if red:
            parts.append(f"{red} needs review")
        if yellow:
            parts.append(f"{yellow} worth a glance")
        title = "Code review ready"
        body = f"{_run_headline(run)} — " + (", ".join(parts) if parts else "nothing flagged")
    try:
        # state.notify() is the never-raises legacy adapter over the notification
        # bus; run it off the event loop because its delivery sink persists to disk.
        await asyncio.to_thread(
            state.notify, "agent", title, body,
        )
    except Exception:  # pragma: no cover - best effort
        logger.debug("code-review-sage: run-finished notification failed", exc_info=True)


async def _handle_review(request: web.Request) -> web.Response:
    """POST /api/apps/code-review-sage/review — start a deterministic review run.

    Body: ``{"links": "<pasted CR links>"}`` or ``{"changes": ["CR-1", ...]}``.
    Returns immediately with a ``run_id``; poll ``/runs`` for status."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}

    changes: list[str] = []
    raw = body.get("changes")
    if isinstance(raw, list):
        changes = [str(c).strip() for c in raw if str(c).strip()]
    links = body.get("links") or body.get("input") or ""
    if not changes and links:
        changes = pipeline.parse_batch(str(links))

    if not changes:
        return web.json_response(
            {"code": "no_reviewable_changes", "error": "no reviewable changes — paste one or more PR/CR links"},
            status=400,
        )

    run: dict[str, Any] = {
        "run_id": uuid.uuid4().hex[:12],
        "changes": changes,
        # Same keys the driver writes progress under (GH-<owner>-<repo>-<n>), so the
        # dashboard can align each row with its live phase instead of falling back to
        # a permanent "queued". Paired positionally with ``changes`` for row hrefs.
        "change_ids": [review_driver.change_id_for(c) for c in changes],
        "status": "running",
        "started_at": _now(),
        "progress": {},
    }
    await _record(run)

    task = asyncio.create_task(_run_review_bg(run, changes))
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)

    return web.json_response(
        {"run_id": run["run_id"], "changes": changes, "status": "running"}
    )


def _posting_expected(rec: dict) -> int:
    """How many comments this change had to deliver to count as reviewed.

    `rec.get("posting_expected") or 1` could not tell an ABSENT field from a
    legitimate 0 — and 0 is exactly what the default (non-posting) path writes, so
    `0 or 1` became 1, `0 >= 1` was false, the change never entered reviewed.json,
    and every unchanged PR was re-reviewed forever. Absent still means 1, for
    records written before the field existed.
    """
    raw = rec.get("posting_expected")
    return 1 if raw is None else int(raw)


def _record_reviewed(run: dict) -> None:
    """Upsert this run's SUCCESSFULLY-reviewed PRs into the durable dedup index
    (by head SHA), so a later repo-review skips them until their head changes.

    Only PRs whose deep review actually ran + recorded are marked — a gate crash,
    no-verdict, or dispatch failure must NOT be written to the index, otherwise the
    next non-forced repo-review would silently skip a PR that was never really
    reviewed. (``run_review`` returns ``ok=True`` for any run with >=1 change even
    when individual changes failed, so we intersect ``head_shas`` with the
    per-change outcomes rather than trusting the run-level ok.) Best-effort — a
    failure here never fails the run. No-op for runs without a ``head_shas`` map
    (i.e. pasted-link reviews, where we don't know each PR's head SHA)."""
    shas = run.get("head_shas") or {}
    if not shas:
        return
    per_change = (run.get("summary") or {}).get("per_change") or []
    # per_change records carry the (lossy) change_id; head_shas is keyed by the
    # collision-free reviewed key. Map change_id -> reviewed key via the run's
    # parallel `changes` (URLs) so we can intersect the two keyings and index
    # ONLY the PRs whose deep review recorded a result AND whose draft was
    # actually delivered are indexed. `post_ok` alone is insufficient: it only
    # means the poster AGENT's turn ended normally, not that `gh api` succeeded
    # (a 422 / network error can still end the turn cleanly). So additionally
    # require the poster to have persisted at least the EXPECTED comment count
    # (`posted_comments >= posting_expected`, both written by the deep-review
    # branch). A failed/partial post leaves posted_comments below expected -> the
    # PR is NOT indexed and is re-reviewed next time (fail-safe: re-review, never
    # silent-skip a PR that was never really delivered).
    changes = run.get("changes") or []
    cid_to_rkey = {
        review_driver.change_id_for(u): review_driver.reviewed_key_for(u)
        for u in changes
    }
    reviewed_ok = {
        cid_to_rkey.get(r.get("change_id"))
        for r in per_change
        if r.get("deep_reviewed") and r.get("post_ok")
        and (r.get("posted_comments") or 0) >= _posting_expected(r)
    }
    reviewed_ok.discard(None)
    now = _now()
    rid = run.get("run_id", "")
    entries = {rkey: {"head_sha": sha, "reviewed_at": now, "run_id": rid}
               for rkey, sha in shas.items() if sha and rkey in reviewed_ok}
    if not entries:
        return
    try:
        results.mark_reviewed(entries)
    except Exception:  # pragma: no cover - defensive
        logger.warning("code-review-sage: failed to update reviewed index", exc_info=True)


async def _list_repo_prs(repo: str) -> tuple[str, list[dict]]:
    """Resolve owner/repo from a repo URL and enumerate its OPEN PRs (via `gh`).
    Returns ``("<owner>/<repo>", prs)``. Raises ValueError for a bad URL and
    RuntimeError for a `gh` failure (mapped to 400/502 by the handlers)."""
    owner, name = adapters.parse_repo_url(repo)   # raises on non-GitHub / no owner/repo
    prs = await asyncio.to_thread(pipeline.list_open_prs, owner, name)
    return f"{owner}/{name}", prs


async def _handle_repo_prs(request: web.Request) -> web.Response:
    """GET .../repo-prs?repo=<url> — list a repo's OPEN PRs annotated with
    reviewed / not-reviewed / stale (by head SHA). Does NOT start a review."""
    repo = (request.query.get("repo") or "").strip()
    if not repo:
        return web.json_response({"code": "repo_required", "error": "missing ?repo=<github repo url>"}, status=400)
    try:
        slug, prs = await _list_repo_prs(repo)
    except (adapters.AdapterParseError, adapters.UnsupportedPlatform, ValueError) as e:
        return web.json_response({"code": "invalid_repo_url", "error": f"invalid repo url: {e}"}, status=400)
    except Exception as e:  # gh not authed / network / repo not found
        logger.warning("repo PR list failed: %s", e, exc_info=True)
        # The provider's text can carry repo paths and token hints, so it is logged
        # for the operator and never returned; the code is what a client branches on.
        return web.json_response(
            {"code": "provider_unavailable", "error": "upstream service error"},
            status=502,
        )
    index = await asyncio.to_thread(results.read_reviewed)
    out = []
    for pr in prs:
        url = pr.get("url", "")
        cid = review_driver.change_id_for(url)      # display / response field only
        # Read the dedup index with the SAME collision-free key it is written
        # under (reviewed_key_for), NOT the lossy change-id — otherwise every
        # reviewed PR reads back as "new" (read/write key mismatch).
        rkey = review_driver.reviewed_key_for(url)
        rec = index.get(rkey) or {}
        stored = rec.get("head_sha") or ""
        cur = pr.get("head_sha") or ""
        out.append({
            **pr, "change_id": cid,
            "reviewed": bool(stored) and stored == cur,
            "reviewed_stale": bool(stored) and stored != cur,
            "reviewed_at": rec.get("reviewed_at", ""),
        })
    return web.json_response({"repo": slug, "prs": out, "count": len(out)})


async def _handle_review_repo(request: web.Request) -> web.Response:
    """POST .../review-repo — review all OPEN PRs of a repo in one batch.

    Body: ``{"repo": "<github repo url>", "force": bool}``. By default only PRs
    NOT yet reviewed at their current head SHA are queued; ``force=true`` reviews
    ALL open PRs regardless of the dedup index."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    repo = str(body.get("repo") or "").strip()
    # Strict boolean: only a literal JSON `true` forces a full re-review. A string
    # ("false"), object, or number must NOT accidentally bypass dedup and trigger a
    # costly review of every open PR.
    force = body.get("force") is True
    if not repo:
        return web.json_response({"code": "repo_required", "error": "missing 'repo' (a github repo url)"}, status=400)
    try:
        slug, prs = await _list_repo_prs(repo)
    except (adapters.AdapterParseError, adapters.UnsupportedPlatform, ValueError) as e:
        return web.json_response({"code": "invalid_repo_url", "error": f"invalid repo url: {e}"}, status=400)
    except Exception as e:
        logger.warning("repo review-repo failed: %s", e, exc_info=True)
        # Same treatment as the PR-list path: log the provider text, return only a code.
        return web.json_response(
            {"code": "provider_unavailable", "error": "upstream service error"},
            status=502,
        )

    index = await asyncio.to_thread(results.read_reviewed)
    changes: list[str] = []
    head_shas: dict[str, str] = {}
    skipped = 0
    for pr in prs:
        url = pr.get("url") or ""
        if not url:
            continue
        # Dedup by a COLLISION-FREE canonical key (NOT the lossy change-id, which
        # sanitizes '-'->'_' and would let acme/service-api and acme/service_api
        # share one reviewed.json entry). head_shas is keyed the same way so
        # _record_reviewed round-trips against this index.
        rkey = review_driver.reviewed_key_for(url)
        cur = pr.get("head_sha") or ""
        if not force:
            rec = index.get(rkey) or {}
            if rec.get("head_sha") and rec.get("head_sha") == cur:
                skipped += 1
                continue   # already reviewed at this exact head SHA
        changes.append(url)
        head_shas[rkey] = cur

    if not changes:
        return web.json_response({
            "repo": slug, "changes": [], "skipped": skipped, "status": "noop",
            "message": "all open PRs already reviewed at their current head "
                       "(use force=true to re-review all)",
        })

    run: dict[str, Any] = {
        "run_id": uuid.uuid4().hex[:12],
        "repo": slug,
        "changes": changes,
        "change_ids": [review_driver.change_id_for(c) for c in changes],
        "head_shas": head_shas,        # consumed by _record_reviewed on success
        "force": force,                # skip the under-lock re-dedup for a forced run
        "status": "running",
        "started_at": _now(),
        "progress": {},
    }
    await _record(run)
    task = asyncio.create_task(_run_review_bg(run, changes))
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return web.json_response({
        "run_id": run["run_id"], "repo": slug,
        "changes": changes, "skipped": skipped, "status": "running",
    })


async def _handle_runs(request: web.Request) -> web.Response:
    """GET /api/apps/code-review-sage/runs — current + recent runs (most-recent
    first) plus live worker-pool occupancy, so the page can show rich progress."""
    async with _LOCK:
        runs = list(_RUNS)
    pool: dict | None = None
    try:
        pool = review_pool.pool_stats()
    except Exception:  # pragma: no cover - defensive
        pool = None
    reviewer: dict | None = None
    try:
        # reviewer_info() reads config.json + the agent json — offload its sync
        # file IO off the event loop (consistent with _handle_settings).
        reviewer = await asyncio.to_thread(review_pool.reviewer_info)
    except Exception:  # pragma: no cover - defensive
        reviewer = None
    return web.json_response({"runs": runs, "pool": pool, "reviewer": reviewer})


# --- Per-run endpoints -------------------------------------------------------
# One review = one thread in the UI. These let the page open a specific thread,
# read its report INLINE (no artifact round-trip), stop it, and dismiss it.

def _run_id_param(request: web.Request) -> str:
    """The ``{run_id}`` path param, **validated** rather than repaired.

    Path params are user input even when the ids we mint are not, and this value
    reaches the filesystem. Sanitizing alone is not enough: ``safe_run_id``
    collapses unsafe characters to ``_`` and strips them from the ends, so
    ``<valid-id>!`` would come back as ``<valid-id>`` -- two different URLs
    addressing the same run, and a mangled id quietly acting on a real run's
    report instead of failing. Require the param to already BE its safe form and
    404 otherwise; every id we mint (``uuid4().hex[:12]``) already is.

    404 rather than 400 so a malformed id is indistinguishable from an unknown
    one -- the endpoint reveals nothing about which ids exist.
    """
    raw = (request.match_info.get("run_id") or "").strip()
    if not raw or store.safe_run_id(raw) != raw:
        raise web.HTTPNotFound(
            text=json.dumps({"code": "run_not_found", "error": f"no such run {raw!r}"}),
            content_type="application/json",
        )
    return raw


async def _handle_run_detail(request: web.Request) -> web.Response:
    """GET .../runs/{run_id} — one run, with its report summary if it has one."""
    run_id = _run_id_param(request)
    async with _LOCK:
        run = _find_run(run_id)
        run = dict(run) if run else None
    if run is None:
        return web.json_response({"code": "run_not_found", "error": f"no such run {run_id!r}"}, status=404)
    return web.json_response({"run": run})


async def _handle_run_report(request: web.Request) -> web.Response:
    """GET .../runs/{run_id}/report — the run's Focus Report, as data.

    This is what makes the report viewable INSIDE the app. The rows come from the
    run's own ``report.json`` (already redacted by ``report.build_report``), so
    reading a report never depends on the artifact store — a run whose artifact
    archive failed still renders here."""
    run_id = _run_id_param(request)
    async with _LOCK:
        run = _find_run(run_id)
        known = run is not None
        status = str((run or {}).get("status") or "")
    if not known:
        return web.json_response({"code": "run_not_found", "error": f"no such run {run_id!r}"}, status=404)
    payload = await asyncio.to_thread(report.read_report, None, run_id)
    if payload is None:
        # Not an error: a running run has no report yet, and the page renders
        # progress instead. Say so explicitly rather than 404-ing a live run.
        return web.json_response({
            "run_id": run_id, "status": status, "ready": False,
            "bands": {"red": 0, "yellow": 0, "green": 0}, "rows": [],
            "generated_at": "", "total": 0, "report_slug": None,
        })
    return web.json_response({"run_id": run_id, "status": status, "ready": True, **payload})


async def _handle_run_cancel(request: web.Request) -> web.Response:
    """POST .../runs/{run_id}/cancel — stop a running review.

    Cancellation is COOPERATIVE and the response says so: changes that have not
    started are dropped, but a change already mid-review finishes, because its
    worker session owns an in-flight model turn that cannot be torn down without
    corrupting the shared pool. The UI must not promise an instant stop."""
    run_id = _run_id_param(request)
    async with _LOCK:
        run = _find_run(run_id)
        if run is None:
            return web.json_response({"code": "run_not_found", "error": f"no such run {run_id!r}"}, status=404)
        if run.get("status") != "running":
            return web.json_response(
                {"code": "run_not_running", "error": f"run is {run.get('status')}, not running"}, status=409)
        _CANCELLED.add(run_id)
        run["cancel_requested_at"] = _now()
        _save_runs()
    return web.json_response({
        "ok": True, "run_id": run_id, "status": "cancelling",
        "message": "queued changes dropped; a review already in progress will finish",
    })


async def _handle_run_delete(request: web.Request) -> web.Response:
    """DELETE .../runs/{run_id} — dismiss a finished thread and delete its data."""
    run_id = _run_id_param(request)
    async with _LOCK:
        run = _find_run(run_id)
        if run is None:
            return web.json_response({"code": "run_not_found", "error": f"no such run {run_id!r}"}, status=404)
        if run.get("status") == "running":
            # Deleting a live run's dir underneath the driver would corrupt the
            # run in progress — cancel it first.
            return web.json_response(
                {"code": "run_still_running", "error": "run is still running — cancel it first"}, status=409)
        if run.get("posting"):
            # Posting runs on a TERMINAL run, so the status check above does not
            # cover it. The poster is mid-flight through the shared staging dir and
            # may still be delivering to the pull request; removing the run now
            # loses the record of what landed and lets the poster recreate an
            # orphan run dir after the delete.
            return web.json_response(
                {"code": "run_posting",
                 "error": "this review is still posting its comments — wait for "
                          "it to finish"},
                status=409)
        _RUNS.remove(run)
        _save_runs()
    await asyncio.to_thread(store.remove_run_dir, run_id)
    return web.json_response({"ok": True, "run_id": run_id})


async def _post_comments_bg(run_id: str, run: dict,
                            change_id: str = "",
                            keys: list[str] | None = None,
                            groups: dict[str, list[str] | None] | None = None) -> None:
    """Publish a finished run's recorded findings to its pull request(s).

    Runs on the same worker pool as a review: the poster is a separate, minimal
    turn that publishes the Python-redacted envelope VERBATIM (``gh api``), which
    is what keeps LLM free-text out of the pull request. Nothing here composes
    comment text.
    """
    try:
        loop = asyncio.get_running_loop()
        pool = review_pool.get_pool()
        dispatch = review_pool.make_sync_dispatch(loop, pool)
        await pool.begin_batch()
        try:
            results_out = []
            for i, link in enumerate(run.get("changes") or []):
                cid = (run.get("change_ids") or [None] * (i + 1))[i] \
                    or results.safe_change_id(link)
                # A selection names comments on ONE change, so the others are left
                # alone rather than having an unrelated key list applied to them.
                # With `groups`, each change carries its own key list and the
                # changes it does not name are skipped the same way.
                if groups is not None:
                    if cid not in groups:
                        continue
                    sel = groups[cid]
                elif change_id and cid != change_id:
                    continue
                else:
                    sel = keys
                out = await asyncio.to_thread(
                    review_driver.post_recorded, cid, link,
                    dispatch=dispatch, run_id=run_id, keys=sel)
                out["change_id"] = cid
                results_out.append(out)
        finally:
            await pool.end_batch()

        posted = sum(len(r.get("posted_keys") or []) for r in results_out)
        failed = [r for r in results_out if not r.get("post_ok")]
        async with _LOCK:
            run["posting"] = False
            # Which comments are on the pull request, per change. The UI reads this
            # to mark individual findings as sent, and it is what makes a partial
            # post legible instead of an all-or-nothing "posted" flag.
            delivered = dict(run.get("posted_keys") or {})
            # WHICH pending draft carries them, so the publish action can tell this
            # run's draft from one a later run put in its place.
            draft_ids = dict(run.get("posted_review_ids") or {})
            for r in results_out:
                if r.get("posted_keys"):
                    delivered[str(r.get("change_id"))] = list(r["posted_keys"])
                if r.get("posted_review_id"):
                    draft_ids[str(r.get("change_id"))] = str(r["posted_review_id"])
            run["posted_keys"] = delivered
            run["posted_review_ids"] = draft_ids
            # Only a run with nothing left unposted counts as fully posted.
            remaining = await asyncio.to_thread(_pending_comment_count, run_id, run)
            run["posted_at"] = _now() if remaining == 0 else None
            run["posted_comments"] = posted
            run["post_error"] = "; ".join(
                str(r.get("post_error") or "post failed") for r in failed) or None
            # A successful retry must repair the per-change delivery evidence, not
            # just the run-level counters. `_record_reviewed` reads ONLY
            # `summary.per_change`, so a record still showing the original failure
            # keeps this PR out of the dedup index and the next repo review reviews
            # and posts it a second time. Same helper the first attempt used.
            per_change = (run.get("summary") or {}).get("per_change") or []
            by_cid = {str(r.get("change_id")): r for r in per_change}
            for r in results_out:
                rec = by_cid.get(str(r.get("change_id")))
                if rec is not None:
                    review_driver.apply_post_outcome(rec, r)
            _save_runs()
        # Indexing is what stops the re-review; do it on the retry path too, and
        # only after the records above reflect what actually landed.
        await asyncio.to_thread(_record_reviewed, run)
        await _notify_posted(run, posted, bool(failed))
    except Exception as e:
        logger.exception("posting comments failed")
        async with _LOCK:
            run["posting"] = False
            run["post_error"] = str(e)
            _save_runs()
    finally:
        # Same contract as a review's claims: release on EVERY terminal path, or
        # the change stays unreviewable until the gateway restarts.
        async with _RUN_LOCK:
            _release_claims(run)


async def _notify_posted(run: dict, posted: int, failed: bool) -> None:
    """Tell the user their comments landed. Posting is minutes of pool work, so
    they will be elsewhere by the time it finishes. Best-effort, as with
    ``_notify_finished``."""
    state = _APP_STATE.get("state")
    if state is None or not hasattr(state, "notify"):
        return
    if failed:
        title = "Posting review comments failed"
        body = f"{_run_headline(run)} — {run.get('post_error') or 'the post did not complete'}"
    else:
        title = "Review comments posted"
        body = (f"{_run_headline(run)} — {posted} comment"
                f"{'' if posted == 1 else 's'} on the pull request")
    try:
        await asyncio.to_thread(
            state.notify, "agent", title, body)
    except Exception:
        logger.debug("post notification failed", exc_info=True)


async def _handle_run_post(request: web.Request) -> web.Response:
    """POST .../runs/{run_id}/post — publish this run's findings to the PR.

    Reviews are NOT posted automatically (see ``review.auto_post``, default off):
    writing to a pull request is a side effect the user opts into, so it is a
    deliberate action taken after reading the review. Refused when the run is
    still going, when its records have been cleared, or when it already posted —
    a duplicate post is not undoable from here.
    """
    run_id = _run_id_param(request)
    force = request.query.get("force", "").lower() in ("1", "true", "yes")
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    raw_keys = body.get("keys")
    keys = ([str(k) for k in raw_keys if isinstance(k, (str, int))]
            if isinstance(raw_keys, list) else None)
    change_id = str(body.get("change_id") or "")
    # A deliberate multi-change selection arrives as ONE request carrying a group
    # per change, because `posting` is a per-RUN flag: a client that sent one
    # request per group had every group after the first refused with
    # `already_posting`, since this handler returns as soon as it fires the
    # background task and the flag is still set. Sequencing the client's requests
    # does not help — only the poster clears the flag. One request, one posting
    # cycle, and the per-change key scoping is preserved inside it.
    raw_groups = body.get("groups")
    groups: dict[str, list[str] | None] | None = None
    if isinstance(raw_groups, list) and raw_groups:
        parsed: dict[str, list[str] | None] = {}
        for g in raw_groups:
            if not isinstance(g, dict):
                continue
            cid = str(g.get("change_id") or "")
            if not cid:
                continue
            gk = g.get("keys")
            parsed[cid] = ([str(k) for k in gk if isinstance(k, (str, int))]
                           if isinstance(gk, list) else None)
        groups = parsed or None
    async with _LOCK:
        run = _find_run(run_id)
        if run is None:
            return web.json_response({"code": "run_not_found", "error": f"no such run {run_id!r}"}, status=404)
        if run.get("status") == "running":
            return web.json_response(
                {"code": "run_still_running", "error": "this review is still running; wait for it to finish"},
                status=409)
        if run.get("posting"):
            return web.json_response(
                {"code": "already_posting", "error": "already posting this review"}, status=409)
        # A selection is always allowed through: "already posted" is now a
        # per-comment fact, and post_recorded drops the keys that already landed.
        if run.get("posted_at") and keys is None and not force:
            return web.json_response({
                "code": "already_posted", "error": "this review was already posted",
                "posted_at": run.get("posted_at"),
                "posted_comments": run.get("posted_comments"),
            }, status=409)
        if groups:
            counts = [
                await asyncio.to_thread(
                    _pending_comment_count, run_id, run, gk, cid)
                for cid, gk in groups.items()
            ]
            pending = sum(counts)
        else:
            pending = await asyncio.to_thread(
                _pending_comment_count, run_id, run, keys, change_id)
        if pending == 0:
            return web.json_response({
                "code": "nothing_to_post",
                "error": "nothing to post — those comments are already on the "
                         "pull request, this review recorded no findings, or its "
                         "records were cleared when the report was archived",
            }, status=409)
        # Posting round-trips the record through the SHARED staging dir
        # (publish_to_shared -> poster turn -> adopt_from_shared). The run is
        # terminal, so its review-time claims are long released — a forced
        # re-review of the same change could be staging there right now, and the
        # two would trade records. Hold the same claim posting needs, refusing
        # rather than interleaving; released in `_post_comments_bg`'s finally.
        posting_cids = [
            cid for cid in (run.get("change_ids") or [])
            if cid in groups
        ] if groups else [
            cid for cid in (run.get("change_ids") or [])
            if not change_id or cid == change_id
        ]
        async with _RUN_LOCK:
            blocked = [
                cid for cid in posting_cids
                if (_INFLIGHT.get(_stage_key(cid)) or run_id) != run_id
            ]
            if blocked:
                return web.json_response(
                    {"code": "change_review_in_flight",
                     "error": "a review of this change is in flight; posting now "
                              "would collide with it — try again when it "
                              "finishes"},
                    status=409)
            for cid in posting_cids:
                _INFLIGHT[_stage_key(cid)] = run_id
        run["posting"] = True
        run["post_error"] = None
        _save_runs()

    # Keep a strong ref like the review path does, so the poster cannot be
    # garbage-collected mid-flight and leave `posting` set with nothing to clear
    # it — which would 409 every later post and refuse delete for this run.
    task = asyncio.create_task(
        _post_comments_bg(run_id, run, change_id, keys, groups))
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return web.json_response({
        "ok": True, "run_id": run_id, "posting": True, "pending": pending,
    })


def _collect_delivered(run: dict, summary: dict) -> None:
    """Carry a run's per-change delivery evidence onto the run itself.

    The explicit posting action records which findings landed as
    ``run["posted_keys"][change_id]``; the opt-in ``review.auto_post`` path delivers
    inside the run and so must record it the same way. The publish action reads that
    map to decide whether the PENDING review on the pull request came from THIS run,
    and an auto-posted draft with no entry reads as somebody else's — delivered, then
    refused.

    A change that delivered nothing is left ABSENT rather than written as an empty
    list. Absent and empty both read as not-delivered, and not writing the key keeps
    a later successful retry from having to distinguish them.
    """
    delivered = dict(run.get("posted_keys") or {})
    draft_ids = dict(run.get("posted_review_ids") or {})
    for rec in summary.get("per_change") or []:
        if rec.get("posted_keys"):
            delivered[str(rec.get("change_id"))] = list(rec["posted_keys"])
        # The id of the draft this delivery actually created. A view that offers to
        # publish compares it against what is pending now; a mismatch means a later
        # run replaced the draft and this run's view must not publish it.
        if rec.get("posted_review_id"):
            draft_ids[str(rec.get("change_id"))] = str(rec["posted_review_id"])
    if delivered:
        run["posted_keys"] = delivered
    if draft_ids:
        run["posted_review_ids"] = draft_ids


def _pending_comment_count(run_id: str, run: dict,
                           keys: list[str] | None = None,
                           change_id: str = "") -> int:
    """How many comments this run WOULD still post.

    Read-only: it builds the same draft bodies the poster publishes without
    dispatching anything, so the UI can label the button honestly and the endpoint
    can refuse a no-op. Comments already on the pull request are excluded, which is
    what lets a partially-posted review keep offering the rest.
    """
    total = 0
    changes = run.get("changes") or []
    ids = run.get("change_ids") or []
    delivered = run.get("posted_keys") or {}
    for i, link in enumerate(changes):
        cid = ids[i] if i < len(ids) else results.safe_change_id(link)
        if change_id and cid != change_id:
            continue
        rec = results.read_result(cid, None, run_id)
        if not rec:
            continue
        already = set(rec.get("posted_keys") or delivered.get(cid) or [])
        try:
            for entry in pipeline.build_pending_comments(rec):
                key = str(entry.get("key"))
                if key in already:
                    continue
                if keys is not None and key not in set(keys):
                    continue
                total += 1
        except Exception:  # pragma: no cover - defensive
            logger.debug("pending count failed for %s", cid, exc_info=True)
    return total


async def _handle_run_archive(request: web.Request) -> web.Response:
    """POST .../runs/{run_id}/archive — publish this run's report as an artifact.

    Reports are archived automatically when a run finishes; this is the retry /
    share path for a run whose archive failed (or one archived before the artifact
    was pruned). The report itself lives in the run dir either way."""
    run_id = _run_id_param(request)
    async with _LOCK:
        run = _find_run(run_id)
        if run is None:
            return web.json_response({"code": "run_not_found", "error": f"no such run {run_id!r}"}, status=404)
        existing = run.get("report_slug")
    if existing:
        return web.json_response({"ok": True, "run_id": run_id, "report_slug": existing,
                                  "created": False})
    rd = store.run_dir(run_id) / "report" / "focus-report.html"

    def _archive() -> str | None:
        # Not `read_text`: the report dir is worker-reachable, so a planted
        # symlink at this name would be followed and a local file the dashboard
        # must never see would be archived as a shareable artifact. The shared
        # helper opens O_NOFOLLOW within the reports dir and returns None on a
        # plant, which lands on the same 502 as a missing report.
        html_body = report.read_within_reports(rd, None, run_id)
        if html_body is None:
            return None
        return review_driver.archive_report(html_body)

    slug = await asyncio.to_thread(_archive)
    if not slug:
        return web.json_response(
            {"code": "report_archive_failed",
             "error": "could not archive this report (no report on disk, or the "
                      "artifact API rejected it)"}, status=502)
    await asyncio.to_thread(report.set_report_slug, slug, None, run_id)
    async with _LOCK:
        run = _find_run(run_id)
        if run is not None:
            run["report_slug"] = slug
        _save_runs()
    return web.json_response({"ok": True, "run_id": run_id, "report_slug": slug,
                              "created": True})


# --- Repo + PR discovery -----------------------------------------------------
# So the user picks a PR instead of pasting a URL.

async def _handle_recent_repos(request: web.Request) -> web.Response:
    """GET .../recent-repos[?days=N] — repos the ``gh`` user recently worked on.

    Each row is annotated with ``pinned`` so the picker can show what is already
    in the sidebar. A host without a usable/authenticated ``gh`` returns 200 with
    ``setup_required`` rather than an error status: "you need to set up gh" is a
    normal first-run state for this panel, not a failure."""
    raw_days = (request.query.get("days") or "").strip()
    days = discovery.CONTRIB_WINDOW_DAYS
    if raw_days:
        try:
            days = int(raw_days)
        except ValueError:
            return web.json_response({"code": "invalid_days", "error": "days must be an integer"}, status=400)
        if days < 0 or days > discovery.MAX_WINDOW_DAYS:
            return web.json_response(
                {"code": "invalid_days", "error": f"days must be between 0 and {discovery.MAX_WINDOW_DAYS}"},
                status=400)

    def _load() -> dict:
        pinned = discovery.read_repos()
        pinned_keys = {f"{r['owner']}/{r['repo']}".lower() for r in pinned}
        try:
            login = discovery.current_login()
        except discovery.GhSetupError as exc:
            return {"repos": [], "pinned": pinned, "setup_required": True,
                    "error": str(exc)}
        if not login:
            return {"repos": [], "pinned": pinned, "login": None}
        rows, truncated = discovery.list_contributed_repos(login, within_days=days)
        for row in rows:
            row["pinned"] = row["full_name"].lower() in pinned_keys
        return {"repos": rows, "pinned": pinned, "login": login,
                "truncated": truncated}

    try:
        return web.json_response(await asyncio.to_thread(_load))
    except discovery.GhSetupError as exc:
        return web.json_response({"repos": [], "pinned": [], "setup_required": True,
                                  "error": str(exc)})
    except discovery.GhError as exc:
        return web.json_response({"code": "provider_unavailable", "error": str(exc)}, status=502)


async def _handle_my_repos(request: web.Request) -> web.Response:
    """GET .../my-repos — every repo the ``gh`` user can reach, newest push first.

    The companion to ``/recent-repos``: that one answers "what have I touched
    lately", this one answers "what can I reach at all", which is what you need
    for a repo you own but have not pushed to inside the activity window. Rows are
    annotated with ``pinned``. A host without a usable/authenticated ``gh`` returns
    200 with ``setup_required`` — an unconfigured CLI is a normal first-run state
    for this panel, and the UI still offers manual entry."""

    def _load() -> dict:
        pinned = discovery.read_repos()
        pinned_keys = {f"{r['owner']}/{r['repo']}".lower() for r in pinned}
        try:
            rows, truncated = discovery.list_user_repos()
        except discovery.GhSetupError as exc:
            return {"repos": [], "pinned": pinned, "setup_required": True,
                    "error": str(exc)}
        for row in rows:
            row["pinned"] = row["full_name"].lower() in pinned_keys
        return {"repos": rows, "pinned": pinned, "truncated": truncated}

    try:
        return web.json_response(await asyncio.to_thread(_load))
    except discovery.GhSetupError as exc:
        return web.json_response({"repos": [], "pinned": [], "setup_required": True,
                                  "error": str(exc)})
    except discovery.GhError as exc:
        return web.json_response({"code": "provider_unavailable", "error": str(exc)}, status=502)


def _pull_request_ref(link: str) -> dict | None:
    """Parse a pasted GitHub PR URL into the repo plus the PR's identity.

    Returns None for anything that is not a PR link, so the caller can fall back
    to repo-URL parsing. Deliberately tolerant about what it accepts and strict
    about what it returns: every field here is produced by the same validated
    parser the review path uses, never by string slicing.
    """
    if "/pull/" not in (link or ""):
        return None
    try:
        owner, repo, number = adapters.github_pr_parts(link)
    except (adapters.AdapterParseError, adapters.UnsupportedPlatform, ValueError):
        return None
    return {
        "owner": owner,
        "repo": repo,
        "number": int(number),
        "url": f"https://github.com/{owner}/{repo}/pull/{number}",
        "change_id": adapters.github_change_id(owner, repo, number),
    }


async def _handle_repos(request: web.Request) -> web.Response:
    """GET/POST/DELETE .../repos — the pinned-repo list the sidebar renders.

    POST/DELETE body: ``{"owner": "...", "repo": "..."}`` or ``{"repo": "<url>"}``
    (a repo URL, parsed by the same validator the review path uses)."""
    if request.method == "GET":
        repos = await asyncio.to_thread(discovery.read_repos)
        return web.json_response({"repos": repos})
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    owner = str(body.get("owner") or "").strip()
    name = str(body.get("repo") or "").strip()
    pr: dict | None = None
    if owner and name:
        # An owner/repo pair supplied directly still has to satisfy the same
        # character allowlist as a parsed URL — this value becomes a path segment
        # in a `gh api` call and (via full_name) a stored identifier.
        try:
            owner, name = adapters.parse_repo_url(f"https://github.com/{owner}/{name}")
        except (adapters.AdapterParseError, adapters.UnsupportedPlatform, ValueError) as e:
            return web.json_response({"code": "invalid_repo", "error": f"invalid repo: {e}"}, status=400)
    elif name:
        # A PASTED PR LINK is the common case here: the field is the only place in
        # the app you can type, so that is where a URL from the clipboard lands.
        # Rejecting it and pointing at the paste box was unhelpful — that box only
        # exists once a repo is already picked, which is the thing being asked for.
        # Pin the PR's repo and report the PR back so the caller can open it.
        pr = _pull_request_ref(name)
        if pr is not None:
            owner, name = pr["owner"], pr["repo"]
        else:
            try:
                owner, name = adapters.parse_repo_url(name)
            except (adapters.AdapterParseError, adapters.UnsupportedPlatform,
                    ValueError) as e:
                return web.json_response({"code": "invalid_repo_url", "error": f"invalid repo url: {e}"},
                                         status=400)
    else:
        return web.json_response(
            {"code": "repo_required", "error": "missing 'owner'+'repo' or a repo url in 'repo'"}, status=400)

    if request.method == "POST":
        repos = await asyncio.to_thread(discovery.add_repo, owner, name)
    else:
        repos = await asyncio.to_thread(discovery.remove_repo, owner, name)
    out: dict[str, Any] = {"ok": True, "repos": repos}
    if request.method == "POST":
        # Which repo was just added. The caller previously guessed at repos[0],
        # which is only right if the store happens to prepend.
        out["added"] = {"owner": owner, "repo": name}
    if request.method == "POST" and pr is not None:
        # The caller uses this to open the pasted pull request instead of leaving
        # the user to find it in the list.
        out["pull_request"] = pr
    return web.json_response(out)


# --- Settings (model / effort / active namespaces) ---------------------------
# These let the dashboard read + write the review knobs that live in config.json
# under the "review" section. The generic GET /api/apps/{name}/config already
# exposes the full config (read), so this route is the WRITE path plus a focused
# settings view that also enumerates available models, efforts, and namespaces.

def _load_known_models() -> list[str]:
    """Selectable models for the review-settings dropdown — the registry's
    CANONICAL keys (e.g. ``opus-4.8-1m``), which are the wire/persisted format
    the review worker consumes, NOT the provider ids from ``available_models``.

    Provider ids carry a ``[1m]`` capability suffix (e.g.
    ``global.anthropic.claude-opus-4-8[1m]``); the brackets fail ``_valid_model``'s
    safe-token check, so sourcing the dropdown from provider ids made every 1M
    variant unselectable (the PUT 400'd and the dropdown snapped back — only the
    bracket-free plain ids survived). Canonical keys are bracket-free tokens that
    both pass validation AND match what ``review_pool`` writes into the worker's
    cli.json overlay. Empty on failure; the UI still offers 'Default (agent config)'."""
    try:
        return [row["model_name"] for row in model_registry.display_list("claude_code")]
    except Exception:  # pragma: no cover - defensive
        return []


# Computed once at import (the registry is immutable after load). A module-level
# constant so the settings validator and the /settings enumerator share one list.
_KNOWN_MODELS: list[str] = _load_known_models()


def _known_models() -> list[str]:
    """Back-compat accessor for the known-model allowlist (the constant above)."""
    return _KNOWN_MODELS


def _valid_model(m: str) -> bool:
    """A model id is acceptable if it is a safe token (it becomes a cli.json
    overlay key for the worker subprocess) and, when the registry is available,
    is one it knows."""
    if not m or len(m) > 64 or not all(c.isalnum() or c in "._-" for c in m):
        return False
    known = _KNOWN_MODELS
    return (m in known) if known else True


def _load_review_section() -> dict:
    """Read the persisted review settings (model/effort/active_namespaces)."""
    try:
        cfg = store.load_config()
    except Exception:
        cfg = {}
    review = cfg.get("review") if isinstance(cfg, dict) else None
    if not isinstance(review, dict):
        review = {}
    return {
        "model": review.get("model") or None,
        "effort": review.get("effort", ""),
        "active_namespaces": review.get("active_namespaces") or ["default"],
        "max_concurrent": review_pool.effective_max_concurrent(),
    }


def _write_review_section(patch: dict) -> dict:
    """Merge a partial review-settings patch into config.json atomically. Only
    the model/effort/active_namespaces keys are writable; everything else in the
    config is preserved. Returns the resulting review section."""
    cfg_path = store.data_dir() / "config.json"
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        if not isinstance(cfg, dict):
            cfg = {}
    except (json.JSONDecodeError, OSError, FileNotFoundError):
        store.ensure_layout()
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    review = cfg.get("review")
    if not isinstance(review, dict):
        review = {}

    if "model" in patch:
        m = patch["model"]
        # Empty/None clears the override (inherits the system/agent default model).
        # A non-empty value is validated (safe token + known to the registry when
        # available) before persisting, since it later becomes a cli.json overlay
        # key for the review worker subprocess — raw request input must not reach it.
        if not m:
            review["model"] = None
        elif _valid_model(str(m)):
            review["model"] = str(m)
        else:
            raise ValueError(f"unknown or invalid model {str(m)!r}")
    if "effort" in patch:
        eff = str(patch["effort"]).lower()
        # "" = inherit the model/provider default; otherwise a concrete level.
        review["effort"] = eff if (eff == "" or eff in review_pool.VALID_EFFORTS) else ""
    if "active_namespaces" in patch:
        ns = patch["active_namespaces"]
        if isinstance(ns, list) and ns:
            avail = set(learning.list_namespaces())
            cleaned = [str(n) for n in ns if str(n) in avail]
            review["active_namespaces"] = cleaned or ["default"]
    if "max_concurrent" in patch:
        # How many reviews run at once on the shared runtime. Clamped to
        # [1, MAX_CONCURRENT_CEIL] so "review all" can fan out without letting a
        # user set an unbounded value that would saturate the host.
        try:
            mc = int(patch["max_concurrent"])
        except (TypeError, ValueError):
            raise ValueError("max_concurrent must be an integer")
        review["max_concurrent"] = max(1, min(mc, review_pool.MAX_CONCURRENT_CEIL))

    cfg["review"] = review
    tmp = cfg_path.with_name(cfg_path.name + ".tmp")
    tmp.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, cfg_path)
    return review


async def _handle_settings(request: web.Request) -> web.Response:
    """GET  -> {settings, models, efforts, namespaces}
       PUT  body {model?, effort?, active_namespaces?} -> {ok, settings}."""
    if request.method == "GET":
        # All of this is synchronous file IO (config read + namespaces dir walk +
        # reviewer_info file read) — offload to a thread so it never blocks the
        # shared gateway event loop.
        def _build_settings_response() -> dict:
            try:
                namespaces = learning.list_namespaces()
            except Exception:
                namespaces = ["default"]
            reviewer = None
            if hasattr(review_pool, "reviewer_info"):
                try:
                    reviewer = review_pool.reviewer_info()
                except Exception:
                    reviewer = None
            return {
                "settings": _load_review_section(),
                "models": _known_models(),
                "efforts": list(review_pool.VALID_EFFORTS),
                "namespaces": namespaces,
                "reviewer": reviewer,
                "max_concurrent_max": review_pool.MAX_CONCURRENT_CEIL,
            }
        return web.json_response(await asyncio.to_thread(_build_settings_response))
    # PUT
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}

    # SEL audit for this write path: it changes security-relevant subprocess
    # config — the model/effort flow into the review worker's cli.json overlay,
    # and the active namespaces select which learnings the worker loads. Both the
    # applied write (success) AND a rejected attempt (denied, e.g. an invalid/
    # injected model) are audited. Offloaded (ledger does sync file IO); failure
    # is swallowed so it can never break the response.
    async def _audit_settings(outcome: str) -> None:
        def _emit() -> None:
            from kiro_crew.sel import sel  # circular import: sel->config->apps cycle
            sel().log_api_access(
                caller="code-review-sage", operation="update_review_settings",
                outcome=outcome,
                resources="config.json#review(" + ",".join(sorted(body)) + ")")
        try:
            await asyncio.to_thread(_emit)
        except Exception as exc:
            logger.warning("SEL audit failed for update_review_settings (%s): %s", outcome, exc)

    try:
        review = await asyncio.to_thread(_write_review_section, body)
        await _audit_settings("success")
        return web.json_response({"ok": True, "settings": {
            "model": review.get("model") or None,
            "effort": review.get("effort", ""),
            "active_namespaces": review.get("active_namespaces") or ["default"],
            "max_concurrent": review.get("max_concurrent") or review_pool.effective_max_concurrent(),
        }})
    except ValueError as exc:
        # Bad client input (e.g. unknown model) — a 4xx, not a server fault.
        # Audit the rejected attempt (security-relevant: invalid model injection).
        await _audit_settings("denied")
        return web.json_response({"code": "invalid_request", "ok": False, "error": str(exc)}, status=400)
    except Exception as exc:
        corr = uuid.uuid4().hex[:12]
        logger.warning("settings write failed [%s]: %s", corr, exc, exc_info=True)
        return web.json_response(
            {"code": "internal_error", "ok": False, "error": "internal error", "id": corr}, status=500
        )


async def _handle_namespaces(request: web.Request) -> web.Response:
    """GET    -> {namespaces:[{name, patterns, candidate, active}], active:[...]}
       POST   body {name}            -> create a namespace
       DELETE body {name}            -> delete a (non-default) namespace."""
    if request.method == "GET":
        # list_namespaces() walks a dir and the per-namespace loop does a
        # synchronous read_text()+parse for EACH namespace — unbounded sync IO.
        # Offload the whole build to a thread so it can't freeze the event loop.
        def _build_ns_response() -> dict:
            names = learning.list_namespaces()
            active = set(learning.get_active_namespaces())
            out = []
            for n in names:
                out.append({
                    "name": n,
                    "patterns": len(learning.list_patterns(namespace=n)),
                    "candidate": learning.candidate_count(namespace=n),
                    "active": n in active,
                })
            return {"namespaces": out, "active": sorted(active)}
        try:
            return web.json_response(await asyncio.to_thread(_build_ns_response))
        except Exception as exc:
            logger.warning("namespace list failed: %s", exc, exc_info=True)
            return web.json_response({"namespaces": [], "active": ["default"]})

    try:
        body = await request.json()
    except Exception:
        body = {}
    name = str((body or {}).get("name", "")).strip()
    if not name:
        return web.json_response({"code": "name_required", "ok": False, "error": "name required"}, status=400)

    # Audit namespace create/delete (filesystem ops that change the learning
    # scope). log_api_access does synchronous file IO (appends to the audit
    # ledger), so it is offloaded off the event loop. Audit failure is swallowed
    # so it can never break the response.
    async def _audit(operation: str, ok: bool) -> None:
        def _emit() -> None:
            from kiro_crew.sel import sel  # circular import: sel->config->apps cycle
            sel().log_api_access(
                caller="code-review-sage", operation=operation,
                outcome="success" if ok else "denied",
                resources=f"learnings/namespaces/{name}")
        try:
            await asyncio.to_thread(_emit)
        except Exception as exc:
            logger.warning("SEL audit failed for %s: %s", operation, exc)

    if request.method == "POST":
        res = await asyncio.to_thread(learning.create_namespace, name)
        await _audit("create_namespace", bool(res.get("ok")))
        return web.json_response(res, status=200 if res.get("ok") else 400)
    if request.method == "DELETE":
        # Deleting a namespace and starting a consolidation for it are mutually
        # exclusive, and BOTH sides take `_NS_OPS_LOCK` so there is no interleaving
        # to reason about. Two earlier shapes of this guard were wrong in ways worth
        # recording: a single check before the prune left a window during the
        # prune's await, and adding a SECOND check after the prune closed that
        # window but could reject the delete with the namespace already pruned from
        # the active list -- refusing the request while silently deactivating the
        # namespace it refused to delete. Holding the lock across check + prune +
        # rmtree removes the window instead of splitting it.
        async with _NS_OPS_LOCK:
            if name in _CONSOLIDATING:
                return web.json_response(
                    {"code": "consolidation_in_progress",
                     "error": "a consolidation is running for this namespace — "
                              "wait for it to finish, then delete"},
                    status=409)

            # Delete FIRST, prune only on success -- one offloaded helper, since both
            # halves are synchronous file IO that must never run on the event loop.
            # Order matters: `delete_namespace` refuses the default namespace, an
            # invalid name, an out-of-tree path and a missing directory, and none of
            # those refusals depend on the active list. Pruning first would therefore
            # deactivate a namespace the API then reports it could not delete --
            # reviews quietly stop using it while the caller is told nothing changed.
            def _delete_then_prune() -> dict:
                res = learning.delete_namespace(name)
                if not res.get("ok"):
                    return res
                try:
                    sec = _load_review_section()
                    if name in (sec.get("active_namespaces") or []):
                        remaining = [n for n in sec["active_namespaces"] if n != name]
                        _write_review_section(
                            {"active_namespaces": remaining or ["default"]})
                except Exception:
                    logger.debug("could not prune deleted ns from active list",
                                 exc_info=True)
                return res

            res = await asyncio.to_thread(_delete_then_prune)
        await _audit("delete_namespace", bool(res.get("ok")))  # destructive rmtree
        return web.json_response(res, status=200 if res.get("ok") else 400)
    return web.json_response({"code": "method_not_allowed", "error": "method not allowed"}, status=405)


async def _handle_learnings(request: web.Request) -> web.Response:
    """GET /api/apps/code-review-sage/learnings?namespace=<ns>

    Read-only view of a namespace's self-learning state so the dashboard can show
    what the reviewer has actually learned:
      - ``patterns``   — the consolidated heuristics reviews load (learned-patterns.md)
      - ``candidate``  — pending learnings staged during reviews, awaiting the
                         human-triggered ``learn-from-sage`` consolidation
    Both come from on-disk markdown; consolidation itself is an AI merge run via
    the skill (never a blind REST overwrite), so this endpoint deliberately does
    not mutate anything. ``?namespace=`` defaults to 'default'."""
    ns = request.query.get("namespace") or learning.DEFAULT_NAMESPACE

    def _build() -> dict:
        try:
            patterns = learning.list_patterns(namespace=ns)
        except Exception:
            patterns = []
        try:
            candidate = learning.list_candidate(namespace=ns)
        except Exception:
            candidate = []
        state = dict(_CONSOLIDATE_STATE.get(ns) or {})
        return {"namespace": ns, "patterns": patterns, "candidate": candidate,
                "consolidating": bool(state.get("running")),
                "consolidate_error": state.get("error")}

    try:
        # Namespace-scoped file reads + markdown parse are synchronous IO — keep
        # them off the shared gateway event loop.
        return web.json_response(await asyncio.to_thread(_build))
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("learnings view failed for ns=%s: %s", ns, exc, exc_info=True)
        return web.json_response({"namespace": ns, "patterns": [], "candidate": [],
                                  "consolidating": False, "consolidate_error": None})


# Namespaces with a merge in flight. Consolidation replaces the whole ruleset, so
# two concurrent merges could interleave writes and lose patterns; the claim is
# held for the life of the request rather than per-write.
# A consolidated ruleset is a handful of short patterns; anything larger is not
# a merge result. Bounds the read of an LLM-written file.
_MERGE_MAX_BYTES = 256 * 1024

_CONSOLIDATING: set[str] = set()

# Last outcome per namespace, so a merge that finished while the page was closed
# still reports itself instead of looking like it never ran.
_CONSOLIDATE_STATE: dict[str, dict] = {}


async def _consolidate_bg(ns: str) -> None:
    """Run the one-shot AI merge, then apply it deterministically.

    The worker writes a candidate merge to a scratch file; this reads that file
    and hands it to ``learning.consolidate_apply``, which refuses empty content.
    The model never writes ``learned-patterns.md`` itself, so a truncated or
    chatty turn cannot destroy the ruleset — it just fails to produce a merge.
    """
    out_path = ""
    try:
        ns_dir = await asyncio.to_thread(learning._namespace_dir, ns, None)
        live = await asyncio.to_thread(learning.common_file, None, ns)
        cand = await asyncio.to_thread(learning.candidate_file, None, ns)
        out_path = str(Path(ns_dir) / "learned-patterns.merge.md")
        # The candidates this merge is allowed to consume, captured BEFORE the
        # worker runs. A review that stages a learning while the merge is in
        # flight must not have it cleared by that merge — the worker never saw it,
        # so it is not represented in the merged ruleset and a blanket clear would
        # destroy the only copy.
        # One element per staged entry, not a set: duplicate ids are legitimate
        # (same title+scope re-learned) and the COUNT is what tells
        # clear_candidate how many occurrences this merge is entitled to drop.
        cand_ids = [
            p["id"] for p in await asyncio.to_thread(
                learning.list_candidate, None, ns)
        ]
        # Clear any residue BEFORE dispatching. The worker writes this path and
        # the backend applies it afterwards; a crash between those two steps
        # leaves a stale merge on disk, and the next consolidation whose
        # worker produces nothing would apply that stale output over the live
        # ruleset and clear the candidate that was never merged.
        try:
            Path(out_path).unlink()
        except OSError:
            pass

        loop = asyncio.get_running_loop()
        pool = review_pool.get_pool()
        dispatch = review_pool.make_sync_dispatch(loop, pool)
        await pool.begin_batch()
        try:
            spawn = await asyncio.to_thread(
                dispatch,
                review_driver.build_consolidation_task(
                    ns, str(live), str(cand), out_path),
            )
        finally:
            await pool.end_batch()

        # The merge file is written by the WORKER, which has shell and file
        # tools, so both its path and its content are LLM-influenced. Reading it
        # with a plain open() would follow a symlink planted at that path and
        # copy whatever it points at into learned-patterns.md — a file that is
        # rendered in the UI and injected into every later review prompt. Route
        # the read through the hooks chokepoint, which opens with O_NOFOLLOW,
        # validates the opened inode against the app's own data root, rejects
        # sensitive paths and hardlinks, and caps the size.
        merged = ""
        try:
            raw = await asyncio.to_thread(
                hooks.safe_read_file_bytes_nolink,
                out_path,
                str(ns_dir),
                max_bytes=_MERGE_MAX_BYTES,
            )
        except Exception:
            logger.debug("merge read rejected", exc_info=True)
            raw = None
        if raw is not None:
            merged = raw.decode("utf-8", errors="replace")

        if not merged.strip():
            why = str(spawn.get("error") or "").strip()
            _CONSOLIDATE_STATE[ns] = {
                "running": False,
                "error": why or "the merge produced no file; the ruleset is unchanged",
            }
            return

        # A file on disk is not evidence the merge SUCCEEDED. The worker can fail
        # partway — timeout, cancellation, a tool error — after writing some of the
        # ruleset, and whatever it managed to emit parses fine. Applying that
        # replaces the full ruleset with a truncated one and clears the staged
        # candidates, so the omitted rules are gone from both copies. `spawn["ok"]`
        # was only consulted on the empty path above; a partial file skipped it.
        if not spawn.get("ok"):
            why = str(spawn.get("error") or "").strip()
            _CONSOLIDATE_STATE[ns] = {
                "running": False,
                "error": (why or "the merge did not finish")
                + "; the ruleset and the staged candidates are unchanged",
            }
            return

        # Non-empty is not the same as usable. The merge worker is an LLM, so it
        # can answer in prose ("nothing to merge here") instead of the pattern
        # format. That text is non-empty, so the emptiness check above passes it
        # through, and consolidate_apply would then replace the ENTIRE ruleset
        # with commentary and clear the staged candidates that were the only copy
        # of the pending learnings. Require at least one parseable pattern.
        if not await asyncio.to_thread(learning.parse_patterns, merged):
            _CONSOLIDATE_STATE[ns] = {
                "running": False,
                "error": ("the merge produced no recognizable patterns; "
                          "the ruleset and the staged candidates are unchanged"),
            }
            return

        # No existence re-check here on purpose. Every learning writer mkdirs its
        # parents, so a late apply WOULD resurrect a deleted namespace -- but the
        # `_CONSOLIDATING` claim already brackets this worker's entire lifetime: the
        # handler adds it before its first await and only this function's `finally`
        # releases it, and the delete handler refuses with 409 for that whole span.
        # A second check here would be unreachable, and an unreachable guard implies
        # protection that never actually runs.
        applied = await asyncio.to_thread(
            learning.consolidate_apply, merged, None, ns, cand_ids)
        _CONSOLIDATE_STATE[ns] = {
            "running": False,
            "error": None if applied.get("ok") else str(applied.get("error") or "merge rejected"),
            "patterns_now": applied.get("patterns_now"),
            "consolidated_from_candidate": applied.get("consolidated_from_candidate"),
        }
    except Exception as exc:
        logger.exception("consolidation failed for ns=%s", ns)
        _CONSOLIDATE_STATE[ns] = {"running": False, "error": str(exc)}
    finally:
        _CONSOLIDATING.discard(ns)
        if out_path:
            # The scratch merge is an intermediate; leaving it behind would make
            # the next run's "did the worker write a file?" check read a stale one
            # as success.
            try:
                await asyncio.to_thread(Path(out_path).unlink, True)
            except OSError:
                pass


async def _handle_consolidate(request: web.Request) -> web.Response:
    """POST .../learnings/consolidate — merge staged candidates into the ruleset.

    The merge is a judgment call (is this candidate already covered? do two rules
    collapse?), so it runs as one worker turn on the same pool reviews use. The
    APPLY is deterministic and refuses empty content, which is what keeps a bad
    merge from wiping the reviewer's memory.
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    ns = str(body.get("namespace") or learning.DEFAULT_NAMESPACE)
    if not (ns == learning.DEFAULT_NAMESPACE or learning._is_valid_ns_name(ns)):
        return web.json_response({"code": "invalid_namespace", "error": f"invalid namespace {ns!r}"}, status=400)
    # Claim the namespace BEFORE the first await. Checking membership and then
    # awaiting the staged count left a window where two concurrent requests for
    # one namespace both passed the guard, then both dispatched a merge against
    # the same scratch path — the second overwriting or unlinking the first's
    # output. The check-and-add below has no await between its two halves, so on
    # the single event loop it is atomic; the claim is given back on every early
    # return.
    async with _NS_OPS_LOCK:
        if ns in _CONSOLIDATING:
            return web.json_response(
                {"code": "consolidation_in_progress",
                 "error": "a consolidation is already running for this namespace"},
                status=409)
        # Taken under the lock so an in-flight DELETE cannot land between this
        # check and the claim; the delete handler holds the same lock across its
        # whole check + prune + rmtree.
        _CONSOLIDATING.add(ns)
    try:
        staged = await asyncio.to_thread(learning.candidate_count, None, ns)
    except Exception:
        _CONSOLIDATING.discard(ns)
        raise
    if staged == 0:
        _CONSOLIDATING.discard(ns)
        return web.json_response(
            {"code": "nothing_to_consolidate", "error": "nothing to consolidate — no learnings are staged"}, status=409)

    _CONSOLIDATE_STATE[ns] = {"running": True, "error": None}
    # Strong ref, as with the review and post paths: a collected merge task would
    # leave `_CONSOLIDATING` claimed with nothing to release it, locking this
    # namespace out of consolidation until a restart.
    task = asyncio.create_task(_consolidate_bg(ns))
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return web.json_response({"ok": True, "namespace": ns, "staged": staged,
                              "running": True})


def register_routes(app: web.Application) -> None:
    """Register the deterministic review routes on the gateway app."""
    # Self-heal: ensure the data layout (dirs + config.json with resolved_paths)
    # exists at startup. Without this, the UI gets {} from the generic config
    # endpoint and shows a perpetual "Initializing…" message because it needs
    # resolved_paths.reports to render the main view.
    try:
        store.ensure_layout()
    except Exception:  # pragma: no cover - never break gateway startup
        logger.warning("code-review-sage: ensure_layout failed at startup", exc_info=True)
    _load_runs()  # restore durable job status (mark orphaned 'running' as 'interrupted')
    # A run dir with no registry entry is unreachable residue (crash between the
    # two writes, or an older layout) — reap it once at startup, but OFF the event
    # loop. `register_routes` is a sync function called from `start_dashboard`,
    # which is a coroutine, so everything here runs on the loop: this reap walks
    # every run dir and deletes the unreferenced ones, and its cost grows with
    # accumulated residue, so on a host with stale runs it stalled gateway startup.
    # `ensure_layout` and `_load_runs` above stay inline deliberately — they are
    # bounded, and the routes cannot answer correctly until they have run (an empty
    # `_RUNS` or missing `resolved_paths` is what the UI renders as a perpetual
    # "Initializing…"). Cleanup has no such ordering requirement, so it is the one
    # that can wait.

    async def _reap_on_startup(_app: web.Application) -> None:
        try:
            reaped = await asyncio.to_thread(_reap_orphan_run_dirs)
            if reaped:
                logger.info("code-review-sage: reaped %d orphan run dir(s)", reaped)
        except Exception:  # pragma: no cover - never break startup
            logger.debug("code-review-sage: orphan reap failed", exc_info=True)

    app.on_startup.append(_reap_on_startup)
    # Cache the dashboard state so a finished run can push a bell notification
    # from a background task (no request in scope there). Absent in tests that
    # register routes on a bare app — every read site treats it as optional.
    try:
        state = app.get("state")
        if state is not None:
            _APP_STATE["state"] = state
    except Exception:  # pragma: no cover - defensive
        pass
    app.router.add_post("/api/apps/code-review-sage/review", _handle_review)
    app.router.add_post("/api/apps/code-review-sage/review-repo", _handle_review_repo)
    app.router.add_get("/api/apps/code-review-sage/repo-prs", _handle_repo_prs)
    app.router.add_get("/api/apps/code-review-sage/recent-repos", _handle_recent_repos)
    app.router.add_get("/api/apps/code-review-sage/my-repos", _handle_my_repos)
    app.router.add_get("/api/apps/code-review-sage/repos", _handle_repos)
    app.router.add_post("/api/apps/code-review-sage/repos", _handle_repos)
    app.router.add_delete("/api/apps/code-review-sage/repos", _handle_repos)
    app.router.add_get("/api/apps/code-review-sage/runs", _handle_runs)
    # Per-run (one thread in the UI). Registered AFTER /runs so the static path
    # is matched first and never shadowed by the {run_id} pattern.
    app.router.add_get("/api/apps/code-review-sage/runs/{run_id}", _handle_run_detail)
    app.router.add_delete("/api/apps/code-review-sage/runs/{run_id}", _handle_run_delete)
    app.router.add_get(
        "/api/apps/code-review-sage/runs/{run_id}/report", _handle_run_report)
    app.router.add_post(
        "/api/apps/code-review-sage/runs/{run_id}/cancel", _handle_run_cancel)
    app.router.add_post(
        "/api/apps/code-review-sage/runs/{run_id}/archive", _handle_run_archive)
    app.router.add_post(
        "/api/apps/code-review-sage/runs/{run_id}/post", _handle_run_post)
    app.router.add_get("/api/apps/code-review-sage/settings", _handle_settings)
    app.router.add_put("/api/apps/code-review-sage/settings", _handle_settings)
    app.router.add_get("/api/apps/code-review-sage/namespaces", _handle_namespaces)
    app.router.add_post("/api/apps/code-review-sage/namespaces", _handle_namespaces)
    app.router.add_delete("/api/apps/code-review-sage/namespaces", _handle_namespaces)
    app.router.add_get("/api/apps/code-review-sage/learnings", _handle_learnings)
    app.router.add_post(
        "/api/apps/code-review-sage/learnings/consolidate", _handle_consolidate)

    async def _shutdown_pool(_app: web.Application) -> None:
        """Retire the reusable review workers when the gateway shuts down."""
        try:
            await review_pool.shutdown_pool()
        except Exception:  # pragma: no cover - defensive
            logger.warning("failed to shut down review pool", exc_info=True)

    # register_app_routes runs before runner.setup() freezes the signal lists,
    # so this append is safe; guarded anyway so it can never break startup.
    try:
        app.on_cleanup.append(_shutdown_pool)
    except Exception:  # pragma: no cover - defensive
        logger.warning("could not register review-pool cleanup hook", exc_info=True)

    logger.info("code-review-sage backend routes registered (deterministic review kickoff)")
