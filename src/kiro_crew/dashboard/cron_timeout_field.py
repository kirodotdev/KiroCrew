"""The ``timeout_secs`` field of the dashboard's cron create and edit requests.

``timeout_secs`` is a job's whole-run budget: the cron service stops a run that
is still going after it and records the run as timed out. The store has always
carried it and the CLI and the cron tools set it; this module is how the two
REST routes, ``POST /api/crons`` and ``PATCH /api/crons/{id}``, read it from a
request body, so both apply one rule.
"""

from __future__ import annotations

from typing import Any

from aiohttp import web

#: The range the cron service accepts (``cron_service.fields``).
TIMEOUT_SECS_MIN = 1
TIMEOUT_SECS_MAX = 86400


def timeout_secs_from_body(body: dict[str, Any]) -> tuple[int | None, web.Response | None]:
    """``(value, None)`` for a valid field, ``(None, None)`` when absent, else ``(None, 400)``.

    A JSON integer in ``1..86400`` is accepted. ``true`` is refused even though
    Python counts it as ``1``, and so are a float, a string and ``null``: a
    client that sends one of those has a bug, and storing a guess would
    become the job's real limit.
    """
    if "timeout_secs" not in body:
        return None, None
    raw = body["timeout_secs"]
    if (
        isinstance(raw, bool)
        or not isinstance(raw, int)
        or not TIMEOUT_SECS_MIN <= raw <= TIMEOUT_SECS_MAX
    ):
        return None, web.json_response(
            {
                "error": (
                    f"timeout_secs must be a whole number of seconds from "
                    f"{TIMEOUT_SECS_MIN} to {TIMEOUT_SECS_MAX}"
                ),
                "code": "invalid_timeout_secs",
            },
            status=400,
        )
    return raw, None
