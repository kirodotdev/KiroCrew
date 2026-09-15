"""Dashboard HTTP handlers for the WakaTime integration.

Two read endpoints, both single-owner (no per-user identity: this is the local
dashboard owner's own configured WakaTime account):

- ``GET /api/wakatime/stats`` — aggregate stats for a named range, for the
  productivity view (coding time, language/project breakdown).
- ``GET /api/wakatime/export`` — hours grouped by project over a date range,
  as CSV (a download) or JSON, for billable-hours export.

When the integration is disabled or unconfigured the endpoints return a 200 with
``{"configured": false}`` rather than an error, so the frontend renders an
ordinary "connect WakaTime" empty state instead of an error banner. When the
range is empty but the upstream call itself failed, they return a 502 rather
than a false-empty result, so an outage is never mistaken for real zero data.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import urllib.parse
from collections import defaultdict
from datetime import date
from typing import Any

from aiohttp import web

logger = logging.getLogger(__name__)

# Ranges the WakaTime stats endpoint accepts. Guard the query param against this
# allowlist so an arbitrary value is never interpolated into the upstream path.
_ALLOWED_RANGES = frozenset(
    {
        "last_7_days",
        "last_30_days",
        "last_6_months",
        "last_year",
        "today",
        "yesterday",
    }
)
_DEFAULT_RANGE = "last_7_days"

# YYYY-MM-DD is the only date shape WakaTime's summaries endpoint accepts.


def _valid_date(value: str) -> bool:
    """True only for a real calendar date in YYYY-MM-DD form.

    A shape-only regex accepts 2026-02-30, a plausible operator typo, which the
    upstream then rejects into an empty result — a false zero-hour export.
    fromisoformat validates both the format and the calendar.
    """
    try:
        # fromisoformat also accepts compact forms like "20260901" (3.11+), so
        # round-trip through isoformat() to hold the promised YYYY-MM-DD shape.
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def _upstream_error() -> web.Response:
    return web.json_response(
        {
            "error": "WakaTime is unreachable or the API key was rejected",
            "code": "upstream_unavailable",
        },
        status=502,
    )


async def api_wakatime_stats(request: web.Request) -> web.Response:
    """GET /api/wakatime/stats?range=last_7_days — aggregate coding stats.

    Returns the WakaTime ``stats`` payload (languages, projects, totals) for the
    range, ``{"configured": false}`` when the integration is off, or a 502 when
    the upstream call fails (never a false-empty payload).
    """
    range_param = request.query.get("range", _DEFAULT_RANGE)
    if range_param not in _ALLOWED_RANGES:
        return web.json_response(
            {
                "error": f"unsupported range; allowed: {sorted(_ALLOWED_RANGES)}",
                "code": "invalid_range",
            },
            status=400,
        )

    # Import the optional WakaTime subsystem lazily, on first request, so it
    # stays off the gateway boot path (handlers/__init__ is imported at startup).
    from kiro_crew.wakatime import WakaTimeUnavailableError, service

    # build_client() does synchronous config-load + vault-decrypt I/O; run it off
    # the event loop so a request never stalls the loop on filesystem reads.
    client = await asyncio.to_thread(service.build_client)
    if client is None:
        return web.json_response({"configured": False})

    try:
        # fetch_stats RAISES on an upstream failure rather than degrading to {},
        # so an outage is reported as a 502, never a false-empty stats payload.
        data = await client.fetch_stats(range_param)
    except WakaTimeUnavailableError:
        return _upstream_error()
    finally:
        await client.close()

    return web.json_response({"configured": True, "range": range_param, "stats": data})


# Characters a spreadsheet treats as the start of a formula. A project name
# is external data (it comes from WakaTime), so a name like "=cmd|..." would
# execute on import if written raw. Prefixing an apostrophe forces the cell to
# be read as text; the leading control chars are equivalents some parsers honor.
_CSV_FORMULA_TRIGGERS = ("=", "+", "-", "@", "\t", "\r")


def _csv_safe(value: str) -> str:
    """Neutralize a leading formula trigger so a CSV cell imports as text."""
    if value and value[0] in _CSV_FORMULA_TRIGGERS:
        return "'" + value
    return value


def _rows_by_project(summaries: list[dict]) -> list[dict[str, Any]]:
    """Fold WakaTime daily summaries into per-project totals.

    Each summary day carries a ``projects`` list of ``{name, total_seconds}``;
    sum the seconds per project name across the range.
    """
    totals: dict[str, float] = defaultdict(float)
    for day in summaries:
        for proj in day.get("projects") or []:
            if not isinstance(proj, dict):
                continue
            name = proj.get("name")
            seconds = proj.get("total_seconds")
            if isinstance(name, str) and isinstance(seconds, (int, float)):
                totals[name] += float(seconds)
    rows = [
        {
            "project": name,
            "seconds": round(secs, 2),
            "hours": round(secs / 3600.0, 4),
        }
        for name, secs in sorted(totals.items(), key=lambda kv: kv[1], reverse=True)
    ]
    return rows


async def api_wakatime_export(request: web.Request) -> web.Response:
    """GET /api/wakatime/export?start=&end=&format=csv|json — billable hours.

    Hours grouped by project over ``start``..``end`` (inclusive, YYYY-MM-DD).
    ``format`` is ``csv`` (default, a download) or ``json``.
    """
    start = request.query.get("start", "")
    end = request.query.get("end", "")
    fmt = request.query.get("format", "csv").lower()

    if not _valid_date(start) or not _valid_date(end):
        return web.json_response(
            {"error": "start and end must be YYYY-MM-DD", "code": "invalid_date"},
            status=400,
        )
    if start > end:
        return web.json_response(
            {"error": "start must not be after end", "code": "invalid_range"},
            status=400,
        )
    if fmt not in ("csv", "json"):
        return web.json_response(
            {"error": "format must be csv or json", "code": "invalid_format"},
            status=400,
        )

    # Import the optional WakaTime subsystem lazily, on first request, so it
    # stays off the gateway boot path (handlers/__init__ is imported at startup).
    from kiro_crew.wakatime import WakaTimeUnavailableError, service

    # build_client() does synchronous config-load + vault-decrypt I/O; run it off
    # the event loop so a request never stalls the loop on filesystem reads.
    client = await asyncio.to_thread(service.build_client)
    if client is None:
        return web.json_response({"configured": False})

    try:
        # fetch_summaries RAISES on an upstream failure of THIS endpoint, rather
        # than degrading to []. A blank billing export must not pass off an
        # outage as "zero hours worked", and only the summaries call itself can
        # tell those apart — a probe of a different endpoint cannot.
        summaries = await client.fetch_summaries(start, end)
    except WakaTimeUnavailableError:
        return _upstream_error()
    finally:
        await client.close()

    rows = _rows_by_project(summaries)

    if fmt == "json":
        # An attachment disposition (mirroring the CSV branch) so the browser
        # downloads a file rather than navigating the dashboard away to a raw
        # JSON blob with no way back.
        payload = json.dumps({"configured": True, "start": start, "end": end, "projects": rows})
        filename = f"wakatime-hours-{start}-to-{end}.json"
        quoted = urllib.parse.quote(filename, safe="")
        return web.Response(
            body=payload.encode("utf-8"),
            content_type="application/json",
            charset="utf-8",
            headers={
                "Content-Disposition": f"attachment; filename*=UTF-8''{quoted}",
                "X-Content-Type-Options": "nosniff",
            },
        )

    # CSV: generated in-memory, returned with a download disposition.
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["project", "hours", "seconds"])
    for r in rows:
        # Project names are neutralized by _csv_safe against formula injection;
        # the taint rule cannot see that wrapper, so scope a waiver to this sink.
        # nosemgrep: python.django.security.injection.csv-writer-injection.csv-writer-injection
        writer.writerow([_csv_safe(r["project"]), r["hours"], r["seconds"]])
    filename = f"wakatime-hours-{start}-to-{end}.csv"
    quoted = urllib.parse.quote(filename, safe="")
    return web.Response(
        body=buf.getvalue().encode("utf-8"),
        content_type="text/csv",
        charset="utf-8",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quoted}",
            "X-Content-Type-Options": "nosniff",
        },
    )
