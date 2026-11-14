"""``kirocrew cron history``: read a cron job's past runs from the terminal.

Read-only. It reads the same ``CronHistoryStore`` the dashboard's history routes
read, through the same three public readers, and redacts the same fields those
routes redact, so it shows nothing the Schedule page hides.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from typing import Any, NoReturn

from kiro_crew.cron import CronService
from kiro_crew.cron_history import _sandbox_confined
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.terminal_safe import safe_terminal_line

_REDACTED_KEYS = ("job_name", "summary", "error", "trace")


def _redact(row: dict[str, Any]) -> dict[str, Any]:
    for key in _REDACTED_KEYS:
        if row.get(key):
            row[key] = redact_credentials(redact_exfiltration_urls(row[key])[0])[0]
    return row


def _fail(message: str) -> NoReturn:
    print(f"Error: {message}", file=sys.stderr)
    sys.exit(1)


def _keep(row: dict[str, Any], status: str | None) -> bool:
    if status is None:
        return True
    if status == "error":
        return row.get("status") != "success"
    return row.get("status") == status


def _when(ts: Any) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(ts)))
    except (TypeError, ValueError, OverflowError, OSError):
        return "-"


def _seconds(row: dict[str, Any]) -> str:
    try:
        return f"{float(row.get('duration_ms') or 0) / 1000:.1f}s"
    except (TypeError, ValueError):
        return "-"


def _first_line(text: Any) -> str:
    if not isinstance(text, str):
        return ""
    return next((line.strip() for line in text.splitlines() if line.strip()), "")


def _print_list(rows: list[dict[str, Any]], with_job: bool) -> None:
    for row in rows:
        parts = [_when(row.get("started_at"))]
        if with_job:
            job_id = str(row.get("job_id", ""))
            name = str(row.get("job_name") or job_id)
            parts.append(job_id if name == job_id else f"{job_id} ({name})")
        parts += [
            str(row.get("status", "")),
            _seconds(row),
            f"run={row.get('run_id', '')}",
        ]
        note = _first_line(row.get("error")) or _first_line(row.get("summary"))
        if note:
            parts.append(note)
        print(safe_terminal_line("  ".join(parts)))


def _print_detail(row: dict[str, Any]) -> None:
    header = (
        ("run", row.get("run_id", "")),
        ("job", row.get("job_id", "")),
        ("trigger", row.get("trigger", "")),
        ("status", row.get("status", "")),
        ("started", _when(row.get("started_at"))),
        ("finished", _when(row.get("finished_at"))),
        ("duration", _seconds(row)),
    )
    for label, value in header:
        print(safe_terminal_line(f"{label}: {value}"))
    for key in ("summary", "error", "trace"):
        text = row.get(key)
        if not isinstance(text, str) or not text:
            continue
        print(f"{key}:")
        for line in text.splitlines():
            print("  " + safe_terminal_line(line))


def cron_history(svc: CronService, args: argparse.Namespace) -> None:
    """Print a job's (or every job's) past runs, or one run's full record."""
    history = svc.get_history()
    job_id = args.job_id
    run_id = getattr(args, "run_id", None)
    if run_id is not None and (args.all or not job_id):
        _fail("--run needs a job id: kirocrew cron history <job-id> --run <run-id>")
    detail: dict[str, Any] | None = None
    rows: list[dict[str, Any]] = []
    try:
        if run_id is not None:
            detail = asyncio.run(history.get_run_detail(job_id, run_id))
        elif args.all:
            rows, _ = asyncio.run(history.get_all_history(limit=sys.maxsize, offset=0))
        else:
            rows, _ = asyncio.run(history.get_job_history(job_id, limit=sys.maxsize, offset=0))
    except ValueError:
        _fail(f"invalid job id: {safe_terminal_line(job_id or '')}")
    # A disabled store reads as empty, and so does an agent sandbox: on Linux it
    # masks cron-history with an empty writable directory the store accepts.
    # Either way "no runs" would be false.
    if not history.enabled or (not (rows or detail) and _sandbox_confined()):
        _fail(
            "cron run history is not readable from this process; "
            "the dashboard Schedule page shows it."
        )
    if run_id is not None:
        if not detail:
            print(f"Run not found: {safe_terminal_line(run_id)}", file=sys.stderr)
            sys.exit(1)
        _redact(detail)
        if args.json:
            print(json.dumps(detail, indent=2))
        else:
            _print_detail(detail)
        return
    # Filtered before the limit, as the dashboard's hide_idle filters before paging.
    shown = [row for row in rows if _keep(row, args.status)]
    total = len(shown)
    runs = shown[: args.limit]
    if args.all:
        names = {j.id: j.name for j in svc.list_jobs(include_disabled=True)}
        for row in runs:
            jid = row.get("job_id", "")
            row["job_name"] = names.get(jid) or jid
    for row in runs:
        _redact(row)
    if args.json:
        print(json.dumps({"runs": runs, "total": total}, indent=2))
        return
    if not runs:
        scope = "" if args.all else f" for job {safe_terminal_line(job_id)}"
        # Name the filter: "no runs" alone would read as an empty history.
        status = f" with status {args.status}" if args.status else ""
        print(f"No runs{status} recorded{scope}.")
        return
    _print_list(runs, with_job=args.all)
