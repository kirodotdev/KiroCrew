"""Starting and driving review runs.

The pasted-link and whole-repo kickoffs, the repo PR listing they annotate, the
background driver bracket on the shared worker pool, the run's terminal status
and failure reason, the reviewed-head dedup index and the finished-run
notification. New review-run rules go here.

Composed onto :mod:`kiro_crew.apps.builtins.code_review_sage.backend.routes`;
see this package's ``__init__``.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kiro_crew.apps.builtins.code_review_sage.backend.routes import (
        _APP_STATE,
        _CANCELLED,
        _LOCK,
        _RUN_LOCK,
        _RUNS,
        _TASKS,
        _claim_changes_under_lock,
        _collect_delivered,
        _make_progress,
        _now,
        _record,
        _release_claims,
        _save_runs,
        adapters,
        logger,
        pipeline,
        require_owner_dashboard_request,
        results,
        review_driver,
        review_pool,
        web,
    )


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
                run["summary"] = {
                    "ok": True,
                    "changes": 0,
                    "note": "all PRs already reviewed or in flight " "in a concurrent run",
                }
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
            #
            # Runtime preflight, read ONCE and reused: when the host cannot spawn a
            # reviewer (no kiro-cli, ACP runtime unimportable) the batch is never
            # opened — begin_batch would raise inside the spawn with a generic
            # message — and the same verdict is handed to run_review, which fails
            # every change fast with a reason naming the missing runtime. One
            # reading keeps the bracket and the driver's verdict consistent.
            # Offloaded: the executable resolution stats candidates across every
            # PATH entry, and one stale network mount there would stall the loop.
            runtime_error = await asyncio.to_thread(review_pool.runtime_preflight)
            # `begin_batch` is the only thing that can answer the sandbox question,
            # because the delegation decision is made inside the spawn (on Windows
            # Kiro Crew has no native backend, so a review is confined only when
            # kiro-cli's own sandbox takes it). A refusal is therefore reported
            # through the SAME channel as a missing kiro-cli — every change fails
            # fast with a reason naming the config key — instead of escaping as an
            # undiscriminated run error. `batch_open` is tracked separately so a
            # refused spawn never calls end_batch() on a batch that never opened.
            batch_open = False
            if not runtime_error:
                try:
                    await pool.begin_batch()
                    batch_open = True
                except review_pool.ReviewRuntimeUnavailable as exc:
                    runtime_error = str(exc)
            try:
                summary = await asyncio.to_thread(
                    review_driver.run_review,
                    changes,  # type: ignore[attr-defined]
                    dispatch=dispatch,
                    progress=_make_progress(run),
                    run_id=run_id,
                    cancelled=lambda: run_id in _CANCELLED,
                    preflight=lambda: runtime_error,
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
                if batch_open:
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
                # whose every change failed would otherwise report "done" with an
                # empty report. Nothing was reviewed; say so rather than letting the
                # UI claim success and then show an empty report.
                #
                # `deep == 0` is checked as well as `recorded == 0`: a change can
                # persist a record and still never be deep-reviewed, which leaves a
                # non-zero record count with no findings in the report. Both are the
                # same "claimed success, delivered nothing" failure.
                run["status"] = "error"
                run["error"] = _first_change_error(summary) or (
                    "the reviewer produced no result record"
                )
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
            if run["status"] == "error":
                # The cause as a TOKEN, beside the sentence rather than instead of
                # it. `_first_change_failure` collapses the token into English for
                # `error`, so without this the payload names the cause nowhere and
                # the dashboard's translator has to recognize causes by their
                # wording -- which silently reverts a card to untranslated
                # pass-through the next time a message is reworded, with no test
                # going red. Set for BOTH error branches above: the summary-level
                # `error` sentence and the per-change one describe the same
                # `per_change` records, so the token is derived from those either
                # way. Absent (never empty-string) when no record carried one, so a
                # reader cannot mistake "no token" for a token.
                reason = _first_change_failure(summary)[1]
                if reason:
                    run["reason"] = reason
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
            await _save_runs()
        await _notify_finished(run)


def _first_change_failure(summary: dict) -> tuple[str, str]:
    """The most useful per-change failure, as ``(sentence, reason_token)``.

    Both halves come from the SAME record in one pass, which is the whole reason
    this is not two functions. The sentence is often built from ``deep_error`` --
    prose from the spawn -- while the token lives in that record's
    ``skipped_reason``; a second independent scan for the token would happily
    return a LATER record's token and pair it with this record's sentence,
    labelling a failure with an unrelated cause.

    The token half is ``""`` when the chosen record kept no ``skipped_reason``.
    """
    for rec in summary.get("per_change") or []:
        for key in ("deep_error", "gate_error", "skipped_reason"):
            val = str(rec.get(key) or "").strip()
            if val:
                sentence = {
                    "no_review_recorded": "the reviewer finished but wrote no " "findings record",
                    "review_record_incomplete": "the reviewer wrote a findings "
                    "record but never completed the "
                    "review",
                    # Reason-level fallback only: a preflight-failed record
                    # carries the specific runtime message in its error fields,
                    # which the key order above prefers, and the run-level error
                    # for that case comes from the summary's own error. Kept so
                    # the reason itself always renders as a cause, never as a
                    # bare enum value.
                    "runtime_unavailable": "the reviewer never ran: its agent "
                    "runtime is unavailable on this host",
                    "review_failed": "the review turn failed",
                }.get(val, val)
                return sentence, str(rec.get("skipped_reason") or "").strip()
    return "", ""


def _first_change_error(summary: dict) -> str:
    """The most useful per-change failure reason, for a run-level error message."""
    return _first_change_failure(summary)[0]


def _run_headline(run: dict) -> str:
    """One-line human description of a run, for notifications and thread titles."""
    repo = run.get("repo")
    n = len(run.get("changes") or [])
    if repo:
        return f"{repo} · {n} PR{'s' if n != 1 else ''}"
    changes = run.get("changes") or []
    if n == 1 and changes:
        link = str(changes[0])
        try:
            host, owner, name, number = adapters.github_pr_ref(link)
        except adapters.AdapterError:
            return link.rsplit("github.com/", 1)[-1]
        # github.com stays host-less (the unambiguous common case); a GitHub
        # Enterprise PR carries its host so two instances' PRs read apart.
        prefix = "" if host == "github.com" else f"{host}/"
        return f"{prefix}{owner}/{name}/pull/{number}"
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
            state.notify,
            "agent",
            title,
            body,
        )
    except Exception:  # pragma: no cover - best effort
        logger.debug("code-review-sage: run-finished notification failed", exc_info=True)


async def _handle_review(request: web.Request) -> web.Response:
    """POST /api/apps/code-review-sage/review — start a deterministic review run.

    Body: ``{"links": "<pasted CR links>"}`` or ``{"changes": ["CR-1", ...]}``.
    Returns immediately with a ``run_id``; poll ``/runs`` for status."""
    owner_denied = await require_owner_dashboard_request(request, "code_review_sage.review")
    if owner_denied is not None:
        return owner_denied
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
            {
                "code": "no_reviewable_changes",
                "error": "no reviewable changes — paste one or more PR/CR links",
            },
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

    return web.json_response({"run_id": run["run_id"], "changes": changes, "status": "running"})


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
        review_driver.change_id_for(u): review_driver.reviewed_key_for(u) for u in changes
    }
    reviewed_ok = {
        cid_to_rkey.get(r.get("change_id"))
        for r in per_change
        if r.get("deep_reviewed")
        and r.get("post_ok")
        and (r.get("posted_comments") or 0) >= _posting_expected(r)
    }
    reviewed_ok.discard(None)
    now = _now()
    rid = run.get("run_id", "")
    entries = {
        rkey: {"head_sha": sha, "reviewed_at": now, "run_id": rid}
        for rkey, sha in shas.items()
        if sha and rkey in reviewed_ok
    }
    if not entries:
        return
    try:
        results.mark_reviewed(entries)
    except Exception:  # pragma: no cover - defensive
        logger.warning("code-review-sage: failed to update reviewed index", exc_info=True)


async def _list_repo_prs(repo: str) -> tuple[str, list[dict]]:
    """Resolve host/owner/repo from a repo URL and enumerate its OPEN PRs (via
    `gh`). Returns ``("<owner>/<repo>", prs)`` (host-qualified for a GitHub
    Enterprise host). Raises ValueError for a bad URL and RuntimeError for a
    `gh` failure (mapped to 400/502 by the handlers)."""
    host, owner, name = adapters.parse_repo_ref(repo)  # raises on unknown host / no owner/repo
    prs = await asyncio.to_thread(pipeline.list_open_prs, owner, name, host=host)
    slug = f"{owner}/{name}" if host == "github.com" else f"{host}/{owner}/{name}"
    return slug, prs


async def _handle_repo_prs(request: web.Request) -> web.Response:
    """GET .../repo-prs?repo=<url> — list a repo's OPEN PRs annotated with
    reviewed / not-reviewed / stale (by head SHA). Does NOT start a review."""
    repo = (request.query.get("repo") or "").strip()
    if not repo:
        return web.json_response(
            {"code": "repo_required", "error": "missing ?repo=<github repo url>"}, status=400
        )
    try:
        slug, prs = await _list_repo_prs(repo)
    except (adapters.AdapterParseError, adapters.UnsupportedPlatform, ValueError) as e:
        return web.json_response(
            {"code": "invalid_repo_url", "error": f"invalid repo url: {e}"}, status=400
        )
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
        cid = review_driver.change_id_for(url)  # display / response field only
        # Read the dedup index with the SAME collision-free key it is written
        # under (reviewed_key_for), NOT the lossy change-id — otherwise every
        # reviewed PR reads back as "new" (read/write key mismatch).
        rkey = review_driver.reviewed_key_for(url)
        rec = index.get(rkey) or {}
        stored = rec.get("head_sha") or ""
        cur = pr.get("head_sha") or ""
        out.append(
            {
                **pr,
                "change_id": cid,
                "reviewed": bool(stored) and stored == cur,
                "reviewed_stale": bool(stored) and stored != cur,
                "reviewed_at": rec.get("reviewed_at", ""),
            }
        )
    return web.json_response({"repo": slug, "prs": out, "count": len(out)})


async def _handle_review_repo(request: web.Request) -> web.Response:
    """POST .../review-repo — review all OPEN PRs of a repo in one batch.

    Body: ``{"repo": "<github repo url>", "force": bool}``. By default only PRs
    NOT yet reviewed at their current head SHA are queued; ``force=true`` reviews
    ALL open PRs regardless of the dedup index."""
    owner_denied = await require_owner_dashboard_request(request, "code_review_sage.review_repo")
    if owner_denied is not None:
        return owner_denied
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
        return web.json_response(
            {"code": "repo_required", "error": "missing 'repo' (a github repo url)"}, status=400
        )
    try:
        slug, prs = await _list_repo_prs(repo)
    except (adapters.AdapterParseError, adapters.UnsupportedPlatform, ValueError) as e:
        return web.json_response(
            {"code": "invalid_repo_url", "error": f"invalid repo url: {e}"}, status=400
        )
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
                continue  # already reviewed at this exact head SHA
        changes.append(url)
        head_shas[rkey] = cur

    if not changes:
        return web.json_response(
            {
                "repo": slug,
                "changes": [],
                "skipped": skipped,
                "status": "noop",
                "message": "all open PRs already reviewed at their current head "
                "(use force=true to re-review all)",
            }
        )

    run: dict[str, Any] = {
        "run_id": uuid.uuid4().hex[:12],
        "repo": slug,
        "changes": changes,
        "change_ids": [review_driver.change_id_for(c) for c in changes],
        "head_shas": head_shas,  # consumed by _record_reviewed on success
        "force": force,  # skip the under-lock re-dedup for a forced run
        "status": "running",
        "started_at": _now(),
        "progress": {},
    }
    await _record(run)
    task = asyncio.create_task(_run_review_bg(run, changes))
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return web.json_response(
        {
            "run_id": run["run_id"],
            "repo": slug,
            "changes": changes,
            "skipped": skipped,
            "status": "running",
        }
    )


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
