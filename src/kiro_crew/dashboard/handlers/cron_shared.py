"""The refusals, limits and caller checks several cron routes share.

The answers a route gives when the cron store is busy or unreadable, when a path
id is malformed and when a secret decision's audit record cannot be written; the
redacted secret-grant map a job is shown with; which app a call comes from, and the
refusal of another app's job; and the late-bound SEL accessor.

Served through :mod:`kiro_crew.dashboard.handlers.cron`, which callers and tests import and patch; this module
never imports it.
"""

from __future__ import annotations

import logging

from aiohttp import web

from kiro_crew.cron import CronStoreUnreadable
from kiro_crew.dashboard.handlers._shared import _owner_denial_response
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.validation import MAX_SHORT_STRING

logger = logging.getLogger("kiro_crew.dashboard.handlers.cron")


# 409 Conflict body returned when a cron-store mutator times out waiting for the
# store lock (CronStoreBusy). Contention is transient (a large atomic save on
# network storage, the CLI process, or the off-loop batch worker), so the client
# should retry rather than treat it as a hard failure. See CronService mutators.
_CRON_BUSY_STATUS = 409
_CRON_BUSY_BODY = {"error": "cron store busy, please retry", "retryable": True}


# Returned when the store cannot be WRITTEN because the last read of it failed
# (CronStoreUnreadable). 409 for the same reason as busy above -- the request
# conflicts with the current state of the resource -- but explicitly
# `retryable: False`: an unreadable file does not heal on its own, so a client
# that retries on busy must NOT retry on this. The exception already carries the
# one action that resolves it (move the file aside), so its message is surfaced
# verbatim rather than restated.
#
# The status and the code are written as LITERALS at the json_response call
# rather than hoisted into module constants, because `test_error_code_contract`
# buckets a computed `status=` as `dynamic_status` and caps that bucket
# deliberately -- a named constant is indistinguishable, to a static scan, from
# computing the status to evade the gate. Literals make this response decidable:
# it scores `compliant` instead of consuming cap.


def _audit_unavailable_response(decision: str) -> web.Response:
    """503 for a grant decision refused because its audit record could not be
    written. Every secret decision — approve, deny, revoke — is audit-or-deny:
    the SEL intent record is written synchronously (``critical=True``) BEFORE
    the store mutates, and an unwritable store refuses the decision with
    nothing changed. Privilege-reducing decisions take the same gate on
    purpose: a revocation nobody can later account for is exactly the kind of
    event the audit log exists to record, and the operator keeps unaudited
    kill switches (pausing the job, deleting the vault entry) for an outage.
    """
    return web.json_response(
        {
            "error": f"audit log unavailable — the {decision} was NOT applied; "
            "fix the audit store and decide again",
            "code": "audit_unavailable",
        },
        status=503,
    )


def _redacted_grant_map(m: dict[str, str]) -> dict[str, str] | None:
    """Owner-view copy of a grant mapping with every key AND value scanned.

    The cron store is agent-writable and ``_job_from_record`` loads these
    dicts verbatim (the write-path grant grammar only covers
    product-mediated writes), so — like every sibling serialized field —
    nothing agent-authored reaches the dashboard without the
    credential/exfiltration redaction. The same verbatim load means the
    value may not even BE a dict (an agent can write a list or a string
    into the store field): anything non-dict serializes as None instead of
    crashing the owner's Schedule poll with a 500.
    """
    if not isinstance(m, dict) or not m:
        return None
    return {
        redact_credentials(redact_exfiltration_urls(str(k))[0])[0]: redact_credentials(
            redact_exfiltration_urls(str(v))[0]
        )[0]
        for k, v in m.items()
    }


def _cron_unreadable_response(exc: CronStoreUnreadable) -> web.Response:
    """Translate a refused write into a structured, non-retryable 409."""
    return web.json_response(
        {"error": str(exc), "code": "cron_store_unreadable", "retryable": False},
        status=409,
    )


def _invalid_path_id_response(value: str, name: str) -> web.Response | None:
    """Guard a URL path id (job_id/run_id/folder_id) for non-empty, bounded length.

    Returns a 400 ``invalid_<name>`` response when ``value`` is empty or longer
    than ``MAX_SHORT_STRING``, else ``None``. This is the single validator the
    cron routes apply to every path-param id — the job/run routes and both
    cron-folder routes — so a malformed id is rejected before any lock
    acquisition, thread dispatch, or state lookup. These ids are server-minted,
    so an over-long value only arrives from a malformed/hostile client.
    """
    if not value or len(value) > MAX_SHORT_STRING:
        return web.json_response(
            {"error": f"invalid {name} format", "code": f"invalid_{name}"}, status=400
        )
    return None


def _sel():
    """Late-binding _sel() for test monkeypatch compatibility."""
    import kiro_crew.dashboard.handlers as _pkg  # noqa: F811

    return _pkg.sel()


def _app_caller(request: web.Request) -> str:
    """The calling app's name, or ``""`` when the caller is not an app.

    The token middleware publishes the claim as a ``str``; ``None`` (absent)
    is the internal-secret transport. Anything else is not an app caller,
    matching the ``== ""`` test the owner gate applies.
    """
    app = request.get("app")
    return app if isinstance(app, str) else ""


def _audit_app_cron(app: str, operation: str, outcome: str, resources: str) -> None:
    """Write the app-attributed SEL row for one app cron decision.

    A bare enqueue: SEL is warmed at gateway startup
    (sel.warm_sel_singleton); guarded because a FAILED warm leaves
    construction to retry here.
    """
    try:
        _sel().log_api_access(
            caller=f"app:{app}",
            operation=operation,
            outcome=outcome,
            source="dashboard",
            resources=resources,
        )
    except Exception:  # pragma: no cover - audit must never change the outcome
        logger.debug("SEL audit for app cron %s failed", operation, exc_info=True)


async def _refuse_foreign_app_job(
    request: web.Request, state: DashboardState, job_ids: list[str], operation: str
) -> web.Response | None:
    """Refuse an APP caller acting on any cron job it does not own.

    ``app == ""`` callers are ruled on by ``require_owner_dashboard_request``
    already, and an internal-secret caller carries no app claim; both skip
    this. An app token may reach these routes (``docs/app-kit/api-reference.md``
    lists them), but only for its OWN jobs: the ones whose host-written
    ``created_by`` is its ``app:<name>`` stamp. A job without that stamp -- the
    person's, another app's, or one that does not exist -- is refused with the
    owner gate's own 403 (``_owner_denial_response``), so a missing id reads the
    same as a foreign one.

    Both outcomes are SEL-audited under ``app:<name>``, the caller that actually
    decided the outcome. ``require_owner_dashboard_request`` is not reused for
    the denial: it records ``request["user"]``, which for an app token is the
    person the token was minted for, not the app acting.

    Every id is checked before the caller acts on any, so a batch that names one
    foreign job changes nothing. The lookup is cache-only: ``created_by`` never
    changes after creation, and a stale miss can only refuse, never allow.
    """
    app = _app_caller(request)
    if not app:
        return None
    # Function-local for the reason ``api_crons`` gives: importing
    # ``kiro_crew.apps.cron_sdk`` runs ``kiro_crew.apps.__init__`` and its cycle.
    from kiro_crew.apps.cron_sdk import app_owner_name

    refused = next(
        (
            job_id
            for job_id in job_ids
            if app_owner_name(getattr(state.crons.get_job(job_id), "created_by", None)) != app
        ),
        None,
    )
    # One SEL row per decision, allow and deny alike.
    _audit_app_cron(
        app,
        operation,
        "allowed" if refused is None else "denied",
        ",".join(job_ids) if refused is None else refused,
    )
    if refused is not None:
        return _owner_denial_response(request)
    return None
