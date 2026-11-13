"""GET /api/sel/recurring-failures -- recurring tool failures, owner only.

Serves :func:`kiro_crew.failure_recurrence.scan` over the security event log, so
the Memory page can offer each finding as a lesson the owner drafts and saves.
Under ``/api/sel/`` on purpose: ``/api/lessons`` is a mixed internal path that an
MCP caller reaches with the internal secret, and the sandbox withholds this log
from foreign-harness children, so these rows stay behind the same owner gate as
``/api/sel/events``. The rows carry less than that route does: no session key,
and no command text.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

from aiohttp import web

from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request
from kiro_crew.dashboard.handlers.core import _sel
from kiro_crew.executors import discovery_executor
from kiro_crew.failure_recurrence import scan

#: The window the page reads, compared with the one before it.
WINDOW_DAYS = 7
#: The most findings one response carries; they arrive most hits first.
MAX_FINDINGS = 50


async def api_sel_recurring_failures(request: web.Request) -> web.Response:
    """The recurring failures of the last week, most hits first.

    Read off the loop on the discovery pool for the reason ``api_sel_events``
    gives: the read is blocking file IO, and the first ``_sel()`` call builds the
    singleton. The successful read is audited after the rows are captured, so the
    audit row never lands in the window it reports on.
    """
    denial = await require_owner_dashboard_request(request, "sel.failures.read")
    if denial is not None:
        return denial

    def _read_then_audit() -> list[dict]:
        audit = _sel()
        found = scan(audit, now=datetime.now(timezone.utc), window=timedelta(days=WINDOW_DAYS))
        audit.log_api_access(
            caller=str(request.get("user") or "owner"),
            operation="sel.failures.read",
            outcome="allowed",
            source="dashboard",
        )
        return [asdict(item) for item in found[:MAX_FINDINGS]]

    rows = await asyncio.get_running_loop().run_in_executor(discovery_executor(), _read_then_audit)
    return web.json_response({"failures": rows, "window_days": WINDOW_DAYS})
