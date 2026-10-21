"""The security event log and posture routes, and the SEL accessor the core routes share.

``_sel`` resolves the security event log through the handlers package at call time, so
a test that patches ``kiro_crew.dashboard.handlers.sel`` is seen by every route that
audits through it. The routes read the log (``/api/sel/events``), verify its chain
(``/api/sel/verify``) and summarise the security counters and posture.

Served through :mod:`kiro_crew.dashboard.handlers.core`, which callers and tests
import and patch; this module never imports it."""

from __future__ import annotations

import asyncio
import logging

from aiohttp import web

from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request
from kiro_crew.executors import discovery_executor
from kiro_crew.security_posture import build_posture_snapshot_async, posture_counts_async

logger = logging.getLogger("kiro_crew.dashboard.handlers.core")


def _sel():
    """Late-binding _sel() for test monkeypatch compatibility."""
    import kiro_crew.dashboard.handlers as _pkg  # noqa: F811 — circular import

    return _pkg.sel()


# ── Security Event Log API ──


async def api_sel_events(request: web.Request) -> web.Response:
    """GET /api/sel/events — recent security events, owner only.

    The rows are the security audit trail itself, and they name the resources a
    decision was about: a file held back by the scanner, a service a grant
    covered. A dashboard session is not by itself the owner -- the messaging
    bridges mint a presigned token whose subject is the allowed user's own id --
    so serving these rows to any authenticated session hands one principal the
    other's audit trail. The check delegates to
    :func:`is_owner_dashboard_request` rather than re-deriving the rule, so this
    surface cannot drift from the secrets vault and the delivery-consent gate.

    The gate is :func:`require_owner_dashboard_request`, the one every other
    owner-only dashboard surface calls, rather than a second spelling of the same
    rule: it makes the owner decision, audits the refusal, and relabels a session
    signed before an owner was configured to ``401 stale_session_reauth``, since
    that caller IS the owner and re-signing in is the remedy. Its denial audit is
    an enqueue against the singleton warmed at startup, which is the whole reason
    the shared spelling can stay this small.

    A read that SUCCEEDS is audited too, and that row is this handler's own: a
    trail carrying only refusals says who was turned away and never says the log
    was read. It is written where the read is, off the loop, because the first
    ``_sel()`` call constructs the singleton -- reading the HMAC key and scanning
    the log tail -- and that must not happen on the event loop. It is written AFTER
    the rows are captured: ``recent()`` flushes the write queue before it walks the
    log, so a row enqueued first would be served back as the newest event.
    """
    denial = await require_owner_dashboard_request(request, "sel.events.read")
    if denial is not None:
        return denial

    try:
        limit = min(int(request.query.get("limit", "100")), 1000)
    except (TypeError, ValueError):
        limit = 100
    # recent() reads the WHOLE audit-log file with blocking IO: it is one JSONL
    # file pruned by age, so `limit` bounds the rows returned, not the bytes
    # read. Called inline it stalls the whole event loop, so it must be
    # offloaded. Use the DISCOVERY pool, not maintenance_executor: this handler
    # is browser-triggerable, so multiple tabs or pollers could otherwise occupy
    # the workers the orphan-reaping sweeps need to recover from an event-loop
    # wedge.
    # _sel() is called INSIDE the callable, not while building it: the first
    # call constructs the singleton, which reads/creates the HMAC key and scans
    # the log tail. Evaluating it here would leave that IO on the loop.

    def _read_then_audit() -> list[dict]:
        """Serve the read, then record it -- one hop for both, in that order.

        ``recent()`` opens with ``flush()``, so a row enqueued before it is on disk
        by the time the walk runs and comes back as the newest record: the caller
        would receive its own audit row in place of a real event, and ``limit=1``
        would return nothing else at all. So the rows are captured first. The write
        still shares the hop, because the first ``_sel()`` call constructs the
        singleton -- reading the HMAC key and scanning the log tail -- and that must
        not happen on the event loop.
        """
        audit = _sel()
        rows = audit.recent(limit=limit)
        audit.log_api_access(
            caller=str(request.get("user") or "owner"),
            operation="sel.events.read",
            outcome="allowed",
            source="dashboard",
        )
        return rows

    events = await asyncio.get_running_loop().run_in_executor(
        discovery_executor(), _read_then_audit
    )
    return web.json_response({"events": events, "count": len(events)})


async def api_sel_verify(request: web.Request) -> web.Response:
    """GET /api/sel/verify — verify HMAC chain integrity.

    ``integrity`` is ``unverifiable`` when the segment dir refused to pin (or
    was swapped mid-verification): the rotated segments were not checked, and
    the endpoint must not answer ``ok`` over the live log alone. ``detail``
    carries the reason and is empty when verifiable.
    """

    # Same offload rationale as api_sel_events, including deferring _sel() into
    # the callable: verify_integrity() reads the whole log file to check the HMAC
    # chain end to end and must not run on the event loop.
    def _verify():
        log = _sel()
        return log.verify_integrity(detailed=True), log.dropped_events

    result, dropped = await asyncio.get_running_loop().run_in_executor(
        discovery_executor(), _verify
    )
    if not result.history_verifiable:
        integrity = "unverifiable"
    elif result.total == result.valid:
        integrity = "ok"
    else:
        integrity = "compromised"
    return web.json_response(
        {
            "total": result.total,
            "valid": result.valid,
            "integrity": integrity,
            "tampered": result.total - result.valid,
            "detail": result.reason,
            "dropped_events": dropped,
        }
    )


async def api_security_stats(_request: web.Request) -> web.Response:
    """GET /api/security/stats — live security feature counts.

    Every count is DERIVED from the control it describes (``security_posture``),
    so a pill can never drift from the thing it claims to measure. ``denied_commands``
    is the user/governance-effective count, which the posture registry deliberately
    does not carry: the registry lists the built-in RULE TABLE (what ships), while
    this field reports what is currently enforced after opt-outs and policy pins.

    The dashboard does not call this — Settings → Security reads
    ``/api/security/posture``, which carries these same counts PLUS the items behind
    them. Kept as a stable, narrow counts-only endpoint for external/API callers.
    Uses ``posture_counts_async`` rather than the full snapshot so serving three
    integers does not build (and serialize) the whole ~45 KB item payload.
    """
    denied = 0
    try:
        from kiro_crew.dashboard.handlers.security import build_denied_commands_snapshot_async

        # Offloaded to a thread executor — reads denied_commands.json + walks the
        # governance profile store (blocking FS I/O) off the event loop.
        denied = (await build_denied_commands_snapshot_async())["effective_count"]
    except Exception:
        logger.warning("Failed to load denied commands count", exc_info=True)

    counts = await posture_counts_async()
    return web.json_response(
        {
            "denied_commands": denied,
            "suspicious_patterns": counts.get("suspicious_patterns"),
            "tool_schemas": counts.get("tool_schemas"),
            "redaction_paths": counts.get("redaction_paths"),
        }
    )


async def api_security_posture(_request: web.Request) -> web.Response:
    """GET /api/security/posture — expandable detail behind each posture count.

    Read-only and posture-only: control definitions and derived counts, never
    credential material, governance rule contents, or user data. See
    ``security_posture`` for the disclosure contract.
    """
    return web.json_response(await build_posture_snapshot_async())
