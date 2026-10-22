"""Publishing a finished run's findings to its pull request.

The explicit post action and its background poster, the posted notification,
finding dismissals, the delivery evidence a run carries and the pending-comment
count. New posting rules go here.

Composed onto :mod:`kiro_crew.apps.builtins.code_review_sage.backend.routes`;
see this package's ``__init__``.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kiro_crew.apps.builtins.code_review_sage.backend.routes import (
        _APP_STATE,
        _INFLIGHT,
        _LOCK,
        _RUN_LOCK,
        _RUNS,
        _TASKS,
        _find_run,
        _now,
        _rebanded_report,
        _record_reviewed,
        _release_claims,
        _run_headline,
        _run_id_param,
        _save_runs,
        _stage_key,
        _write_runs,
        logger,
        pipeline,
        require_owner_dashboard_request,
        results,
        review_driver,
        review_pool,
        store,
        web,
    )


async def _post_comments_bg(
    run_id: str,
    run: dict,
    change_id: str = "",
    keys: list[str] | None = None,
    groups: dict[str, list[str] | None] | None = None,
) -> None:
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
                cid = (run.get("change_ids") or [None] * (i + 1))[i] or results.safe_change_id(link)
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
                sel = await asyncio.to_thread(_without_dismissed, run_id, run, cid, sel)
                if sel is not None and not sel:
                    continue
                out = await asyncio.to_thread(
                    review_driver.post_recorded,
                    cid,
                    link,
                    dispatch=dispatch,
                    run_id=run_id,
                    keys=sel,
                )
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
            run["post_error"] = (
                "; ".join(str(r.get("post_error") or "post failed") for r in failed) or None
            )
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
            await _save_runs()
        # Indexing is what stops the re-review; do it on the retry path too, and
        # only after the records above reflect what actually landed.
        await asyncio.to_thread(_record_reviewed, run)
        await _notify_posted(run, posted, bool(failed))
    except Exception as e:
        logger.exception("posting comments failed")
        async with _LOCK:
            run["posting"] = False
            run["post_error"] = str(e)
            await _save_runs()
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
        body = (
            f"{_run_headline(run)} — {posted} comment"
            f"{'' if posted == 1 else 's'} on the pull request"
        )
    try:
        await asyncio.to_thread(state.notify, "agent", title, body)
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
    owner_denied = await require_owner_dashboard_request(request, "code_review_sage.run_post")
    if owner_denied is not None:
        return owner_denied
    run_id = _run_id_param(request)
    force = request.query.get("force", "").lower() in ("1", "true", "yes")
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    raw_keys = body.get("keys")
    keys = (
        [str(k) for k in raw_keys if isinstance(k, (str, int))]
        if isinstance(raw_keys, list)
        else None
    )
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
            parsed[cid] = (
                [str(k) for k in gk if isinstance(k, (str, int))] if isinstance(gk, list) else None
            )
        groups = parsed or None
    async with _LOCK:
        run = _find_run(run_id)
        if run is None:
            return web.json_response(
                {"code": "run_not_found", "error": f"no such run {run_id!r}"}, status=404
            )
        if run.get("status") == "running":
            return web.json_response(
                {
                    "code": "run_still_running",
                    "error": "this review is still running; wait for it to finish",
                },
                status=409,
            )
        if run.get("posting"):
            return web.json_response(
                {"code": "already_posting", "error": "already posting this review"}, status=409
            )
        # A selection is always allowed through: "already posted" is now a
        # per-comment fact, and post_recorded drops the keys that already landed.
        if run.get("posted_at") and keys is None and not force:
            return web.json_response(
                {
                    "code": "already_posted",
                    "error": "this review was already posted",
                    "posted_at": run.get("posted_at"),
                    "posted_comments": run.get("posted_comments"),
                },
                status=409,
            )
        if groups:
            counts = [
                await asyncio.to_thread(_pending_comment_count, run_id, run, gk, cid)
                for cid, gk in groups.items()
            ]
            pending = sum(counts)
        else:
            pending = await asyncio.to_thread(_pending_comment_count, run_id, run, keys, change_id)
        if pending == 0:
            return web.json_response(
                {
                    "code": "nothing_to_post",
                    "error": "nothing to post — those comments are already on the "
                    "pull request, this review recorded no findings, or its "
                    "records were cleared when the report was archived",
                },
                status=409,
            )
        # Posting round-trips the record through the SHARED staging dir
        # (publish_to_shared -> poster turn -> adopt_from_shared). The run is
        # terminal, so its review-time claims are long released — a forced
        # re-review of the same change could be staging there right now, and the
        # two would trade records. Hold the same claim posting needs, refusing
        # rather than interleaving; released in `_post_comments_bg`'s finally.
        posting_cids = (
            [cid for cid in (run.get("change_ids") or []) if cid in groups]
            if groups
            else [cid for cid in (run.get("change_ids") or []) if not change_id or cid == change_id]
        )
        async with _RUN_LOCK:
            blocked = [
                cid for cid in posting_cids if (_INFLIGHT.get(_stage_key(cid)) or run_id) != run_id
            ]
            if blocked:
                return web.json_response(
                    {
                        "code": "change_review_in_flight",
                        "error": "a review of this change is in flight; posting now "
                        "would collide with it — try again when it "
                        "finishes",
                    },
                    status=409,
                )
            for cid in posting_cids:
                _INFLIGHT[_stage_key(cid)] = run_id
        run["posting"] = True
        run["post_error"] = None
        await _save_runs()

    # Keep a strong ref like the review path does, so the poster cannot be
    # garbage-collected mid-flight and leave `posting` set with nothing to clear
    # it — which would 409 every later post and refuse delete for this run.
    task = asyncio.create_task(_post_comments_bg(run_id, run, change_id, keys, groups))
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return web.json_response(
        {
            "ok": True,
            "run_id": run_id,
            "posting": True,
            "pending": pending,
        }
    )


_DISMISS_REASON_MAX = 500


def _dismissed_keys(run: dict, change_id: str) -> set[str]:
    """The finding keys the user dismissed on one change of this run."""
    per_change = (run.get("dismissed") or {}).get(change_id)
    return set(per_change) if isinstance(per_change, dict) else set()


def _without_dismissed(
    run_id: str, run: dict, change_id: str, sel: list[str] | None
) -> list[str] | None:
    """A post selection with the dismissed findings taken out.

    ``None`` means "every pending comment", so when this change has dismissals the
    full key list is spelled out first and then filtered: a dismissed finding is
    never sent, whether it was picked by hand or swept up by "post all".
    """
    dismissed = _dismissed_keys(run, change_id)
    if not dismissed:
        return sel
    if sel is None:
        rec = results.read_result(change_id, None, run_id)
        if not rec:
            return sel
        sel = [str(e.get("key")) for e in pipeline.build_pending_comments(rec)]
    return [k for k in sel if k not in dismissed]


async def _handle_run_dismiss(request: web.Request) -> web.Response:
    """POST .../runs/{run_id}/dismiss — mark one finding as not worth sending.

    Body: ``{"change_id", "key", "reason"}`` (reason required); ``"dismissed":
    false`` undoes it. The reason is stored on the run record beside
    ``posted_keys``, so it lives and ages out with the review it is about. A dismissed finding is left out of every
    later post. A finding already on the pull request cannot be dismissed: the
    comment is public by then and dismissing it here would change nothing there.
    """
    owner_denied = await require_owner_dashboard_request(request, "code_review_sage.run_dismiss")
    if owner_denied is not None:
        return owner_denied
    run_id = _run_id_param(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    change_id = str(body.get("change_id") or "")
    key = str(body.get("key") or "")
    undo = body.get("dismissed") is False
    raw_reason = body.get("reason")
    reason = raw_reason.strip() if isinstance(raw_reason, str) else ""
    if not pipeline.FINDING_KEY_RE.match(key):
        return web.json_response(
            {"code": "bad_key", "error": "only a finding can be dismissed"}, status=400
        )
    if not undo and not reason:
        return web.json_response(
            {"code": "reason_required", "error": "say why this finding is dismissed"}, status=400
        )
    if len(reason) > _DISMISS_REASON_MAX:
        return web.json_response(
            {
                "code": "reason_too_long",
                "error": f"keep the reason under {_DISMISS_REASON_MAX} characters",
            },
            status=400,
        )
    async with _LOCK:
        run = _find_run(run_id)
        if run is None:
            return web.json_response(
                {"code": "run_not_found", "error": f"no such run {run_id!r}"}, status=404
            )
        if change_id not in (run.get("change_ids") or []):
            return web.json_response(
                {"code": "change_not_found", "error": "that change is not in this review"},
                status=404,
            )
        # While a post is in flight its delivered keys are not on the run yet,
        # so "already on the pull request" cannot be answered; refuse instead.
        if run.get("posting"):
            return web.json_response(
                {
                    "code": "already_posting",
                    "error": "this review is being posted; try again when it finishes",
                },
                status=409,
            )
        if key in set((run.get("posted_keys") or {}).get(change_id) or []):
            return web.json_response(
                {"code": "already_posted", "error": "that finding is already on the pull request"},
                status=409,
            )
        dismissed = dict(run.get("dismissed") or {})
        per_change = dict(dismissed.get(change_id) or {})
        changes: dict[str, Any] = {}
        if undo:
            # "Post all" may have counted this run fully posted only because the
            # finding was dismissed; an undone finding is pending, so the run is not.
            # An undo of a key that was never dismissed changes nothing.
            if key in per_change:
                per_change.pop(key)
                changes["posted_at"] = None
        else:
            per_change[key] = {"reason": store.redact_text(reason)}
        if per_change:
            dismissed[change_id] = per_change
        else:
            dismissed.pop(change_id, None)
        changes["dismissed"] = dismissed
        # The run card's dots read the band tallies folded into the run summary
        # when the review finished. A dismissal can move a row to another band,
        # so refresh those tallies from the re-banded report; the report
        # endpoint re-bands on read, and this keeps the card in step with it.
        summary = run.get("summary")
        if isinstance(summary, dict) and isinstance(summary.get("report"), dict):
            rebanded = await asyncio.to_thread(_rebanded_report, run_id, dismissed)
            if rebanded is not None:
                changes["summary"] = {
                    **summary,
                    "report": {**summary["report"], "bands": rebanded["bands"]},
                }
        # Persist a staged copy first and touch the live run only once the write
        # lands: a concurrent /runs read holds the same run objects, and
        # `_save_runs` swallows a failed write. Either way a dismissal could show
        # as saved and then be lost on restart, making the finding postable again.
        staged = [{**r, **changes} if r is run else r for r in _RUNS]
        try:
            await asyncio.to_thread(_write_runs, json.dumps(staged, indent=2))
        except Exception:
            logger.warning("failed to persist a dismissal", exc_info=True)
            return web.json_response(
                {"code": "save_failed", "error": "could not save the dismissal"}, status=500
            )
        run.update(changes)
    return web.json_response(
        {"ok": True, "run_id": run_id, "change_id": change_id, "key": key, "dismissed": not undo}
    )


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


def _pending_comment_count(
    run_id: str, run: dict, keys: list[str] | None = None, change_id: str = ""
) -> int:
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
        already |= _dismissed_keys(run, cid)
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
