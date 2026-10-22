"""The per-run endpoints: one review is one thread in the UI.

The validated ``{run_id}`` parameter, a run's detail, its re-banded report, cancel,
delete and the report archive. Posting and dismissing findings are
``publishing``. New per-run endpoints go here.

Composed onto :mod:`kiro_crew.apps.builtins.code_review_sage.backend.routes`;
see this package's ``__init__``.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.apps.builtins.code_review_sage.backend.routes import (
        _CANCELLED,
        _LOCK,
        _RUNS,
        _find_run,
        _now,
        _save_runs,
        report,
        require_owner_dashboard_request,
        review_driver,
        store,
        web,
    )


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
        return web.json_response(
            {"code": "run_not_found", "error": f"no such run {run_id!r}"}, status=404
        )
    return web.json_response({"run": run})


def _rebanded_report(run_id: str, dismissed: dict) -> dict | None:
    """The run's stored report with each row's band recomputed for its dismissals.

    A row's band is fixed when the report is written, but a dismissed finding no
    longer counts against the change, so the band the user sees must come from
    ``classify()`` run again with that finding removed. ``report.reband`` keeps
    every other reason a row is red or yellow (design verdict, blast, branch gate,
    regression, an AI override) and only drops the dismissed finding's weight.
    Sync: it reads report.json and the result records, so callers run it off
    the event loop."""
    payload = report.read_report(None, run_id)
    if payload is None or not dismissed:
        return payload
    return report.reband(payload, dismissed, None, run_id, store.read_config_quiet())


async def _handle_run_report(request: web.Request) -> web.Response:
    """GET .../runs/{run_id}/report — the run's Focus Report, as data.

    This is what makes the report viewable INSIDE the app. The rows come from the
    run's own ``report.json`` (already redacted by ``report.build_report``), so
    reading a report never depends on the artifact store — a run whose artifact
    archive failed still renders here. Rows whose findings were dismissed are
    re-banded on the way out (see ``_rebanded_report``)."""
    run_id = _run_id_param(request)
    async with _LOCK:
        run = _find_run(run_id)
        known = run is not None
        status = str((run or {}).get("status") or "")
        dismissed = dict((run or {}).get("dismissed") or {})
    if not known:
        return web.json_response(
            {"code": "run_not_found", "error": f"no such run {run_id!r}"}, status=404
        )
    payload = await asyncio.to_thread(_rebanded_report, run_id, dismissed)
    if payload is None:
        # Not an error: a running run has no report yet, and the page renders
        # progress instead. Say so explicitly rather than 404-ing a live run.
        return web.json_response(
            {
                "run_id": run_id,
                "status": status,
                "ready": False,
                "bands": {"red": 0, "yellow": 0, "green": 0},
                "rows": [],
                "generated_at": "",
                "total": 0,
                "report_slug": None,
            }
        )
    return web.json_response({"run_id": run_id, "status": status, "ready": True, **payload})


async def _handle_run_cancel(request: web.Request) -> web.Response:
    """POST .../runs/{run_id}/cancel — stop a running review.

    Cancellation is COOPERATIVE and the response says so: changes that have not
    started are dropped, but a change already mid-review finishes, because its
    worker session owns an in-flight model turn that cannot be torn down without
    corrupting the shared pool. The UI must not promise an instant stop."""
    owner_denied = await require_owner_dashboard_request(request, "code_review_sage.run_cancel")
    if owner_denied is not None:
        return owner_denied
    run_id = _run_id_param(request)
    async with _LOCK:
        run = _find_run(run_id)
        if run is None:
            return web.json_response(
                {"code": "run_not_found", "error": f"no such run {run_id!r}"}, status=404
            )
        if run.get("status") != "running":
            return web.json_response(
                {"code": "run_not_running", "error": f"run is {run.get('status')}, not running"},
                status=409,
            )
        _CANCELLED.add(run_id)
        run["cancel_requested_at"] = _now()
        await _save_runs()
    return web.json_response(
        {
            "ok": True,
            "run_id": run_id,
            "status": "cancelling",
            "message": "queued changes dropped; a review already in progress will finish",
        }
    )


async def _handle_run_delete(request: web.Request) -> web.Response:
    """DELETE .../runs/{run_id} — dismiss a finished thread and delete its data."""
    owner_denied = await require_owner_dashboard_request(request, "code_review_sage.run_delete")
    if owner_denied is not None:
        return owner_denied
    run_id = _run_id_param(request)
    async with _LOCK:
        run = _find_run(run_id)
        if run is None:
            return web.json_response(
                {"code": "run_not_found", "error": f"no such run {run_id!r}"}, status=404
            )
        if run.get("status") == "running":
            # Deleting a live run's dir underneath the driver would corrupt the
            # run in progress — cancel it first.
            return web.json_response(
                {"code": "run_still_running", "error": "run is still running — cancel it first"},
                status=409,
            )
        if run.get("posting"):
            # Posting runs on a TERMINAL run, so the status check above does not
            # cover it. The poster is mid-flight through the shared staging dir and
            # may still be delivering to the pull request; removing the run now
            # loses the record of what landed and lets the poster recreate an
            # orphan run dir after the delete.
            return web.json_response(
                {
                    "code": "run_posting",
                    "error": "this review is still posting its comments — wait for " "it to finish",
                },
                status=409,
            )
        _RUNS.remove(run)
        await _save_runs()
    await asyncio.to_thread(store.remove_run_dir, run_id)
    return web.json_response({"ok": True, "run_id": run_id})


async def _handle_run_archive(request: web.Request) -> web.Response:
    """POST .../runs/{run_id}/archive — publish this run's report as an artifact.

    Reports are archived automatically when a run finishes; this is the retry /
    share path for a run whose archive failed (or one archived before the artifact
    was pruned). The report itself lives in the run dir either way."""
    owner_denied = await require_owner_dashboard_request(request, "code_review_sage.run_archive")
    if owner_denied is not None:
        return owner_denied
    run_id = _run_id_param(request)
    async with _LOCK:
        run = _find_run(run_id)
        if run is None:
            return web.json_response(
                {"code": "run_not_found", "error": f"no such run {run_id!r}"}, status=404
            )
        existing = run.get("report_slug")
    if existing:
        return web.json_response(
            {"ok": True, "run_id": run_id, "report_slug": existing, "created": False}
        )
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
            {
                "code": "report_archive_failed",
                "error": "could not archive this report (no report on disk, or the "
                "artifact API rejected it)",
            },
            status=502,
        )
    await asyncio.to_thread(report.set_report_slug, slug, None, run_id)
    async with _LOCK:
        run = _find_run(run_id)
        if run is not None:
            run["report_slug"] = slug
        await _save_runs()
    return web.json_response({"ok": True, "run_id": run_id, "report_slug": slug, "created": True})
