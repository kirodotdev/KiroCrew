"""``kirocrew usage``: the install's recent credit spend, read from the terminal.

``_usage_report`` reads the same per-turn rows as the Usage tab's "Spend over time"
chart (``usage_series.load_rows``), aggregates them with the pure
``usage_series.build_report`` and prints a plain table, or with ``--json`` one JSON
document. It needs no running gateway and writes nothing.

An owner of the mirror facade ``kiro_crew.cli_commands``: callers and tests reach every
name here as ``kiro_crew.cli_commands.<name>``, and a patch through the facade reaches
this module. This module never imports the facade.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # annotations only: a runtime import would be a public facade name
    from datetime import date
    from typing import Any

#: Decimals a credits or cost figure prints with in the table.
_MONEY_DECIMALS = 2
#: How ``__unattributed__`` reads in the table, as the chart labels that key.
_UNATTRIBUTED_LABEL = "(unattributed)"


def _window_days(args: argparse.Namespace, today: date, ceiling: int) -> int:
    """Whole local days the report covers, clamped to ``[1, ceiling]``."""
    days = args.days if args.since is None else (today - args.since).days + 1
    return max(1, min(days, ceiling))


def _figure(value: Any, decimals: int | None) -> str:
    if value is None:
        return "-"
    return f"{value:.{decimals}f}" if decimals is not None else str(value)


def _print_table(report: dict[str, Any], unattributed_key: str) -> None:
    grouping = report["by"]
    print(
        f"Usage {report['from']} .. {report['to']} "
        f"({report['days']} day{'s' if report['days'] != 1 else ''}, by {grouping})"
    )
    if report["truncated"]:
        since = report["complete_from"]
        print(
            "This window may undercount: the usage store held more rows than one read keeps"
            + (f"; every turn from {since} on is counted." if since else ".")
        )
    if not report["totals"]["turns"]:
        print("No usage recorded in this window.")
        return
    columns: list[tuple[str, str, int | None]] = [
        ("credits", "Credits", _MONEY_DECIMALS),
        ("turns", "Turns", None),
    ]
    # Like the page's Cost column: shown only when some row of the window carries one.
    for field, title, decimals in (("cost", "Cost", _MONEY_DECIMALS), ("tokens", "Tokens", None)):
        if any(row[field] is not None for row in report["rows"]):
            columns.append((field, title, decimals))
    lines = [
        (_UNATTRIBUTED_LABEL if row["key"] == unattributed_key else row["key"], row)
        for row in report["rows"]
    ]
    lines.append(("TOTAL", report["totals"]))
    cells = [[label] + [_figure(row[f], d) for f, _t, d in columns] for label, row in lines]
    header = [grouping.capitalize()] + [title for _f, title, _d in columns]
    widths = [max(len(r[i]) for r in [header, *cells]) for i in range(len(header))]
    for row_cells in [header, *cells]:
        label, *figures = row_cells
        print(
            "  ".join(
                [label.ljust(widths[0])] + [v.rjust(w) for v, w in zip(figures, widths[1:])]
            ).rstrip()
        )


def _usage_report(args: argparse.Namespace) -> None:
    """Print the window's spend as a table, or as one JSON document with ``--json``."""
    # Here, not at module scope: the facade imports every owner, so a top-level
    # import would load the dashboard handlers for every command it serves.
    from kiro_crew.dashboard.handlers import usage_series

    today = datetime.now().astimezone().date()
    days = _window_days(args, today, usage_series.WINDOW_DAYS)
    scan = usage_series.load_rows()
    report = usage_series.build_report(
        scan.rows,
        by=args.by,
        days=days,
        dropped=scan.dropped,
        complete_from=scan.complete_from,
        today=today,
    )
    if args.json:
        print(json.dumps(report))
        return
    _print_table(report, usage_series.UNATTRIBUTED_KEY)
