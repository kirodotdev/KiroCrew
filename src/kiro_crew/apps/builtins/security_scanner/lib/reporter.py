"""Notification selection — only NEW, actionable findings.

A scan re-detects the same issues every run; notifying on all of them would
spam the user (the app-dev cron guidance: never ping for an unchanged
condition). :func:`select_new_actionable` filters to findings that are BOTH
worth acting on AND not already notified, keyed by finding id.

"Actionable" = high/critical severity, OR any finding confirmed exploitable —
an ``exploited`` low-severity finding still matters. ``pattern-learned`` mediums
and below are recorded but do not page the user.
"""
from __future__ import annotations

from .models import Finding

_ACTIONABLE_SEVERITIES = {"high", "critical"}


def is_actionable(finding: Finding) -> bool:
    return finding.severity in _ACTIONABLE_SEVERITIES or finding.status == "exploited"


def select_new_actionable(
    findings: list[Finding],
    already_notified_ids: set[str],
) -> list[Finding]:
    """Return findings to notify on: actionable AND not previously notified.
    De-duplicated by id within the batch, ordered most-severe first."""
    order = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
    seen: set[str] = set()
    out: list[Finding] = []
    for f in findings:
        if f.id in already_notified_ids or f.id in seen:
            continue
        if not is_actionable(f):
            continue
        seen.add(f.id)
        out.append(f)
    out.sort(key=lambda f: (order.get(f.severity, 9), f.topic))
    return out


def format_notification(new_findings: list[Finding], scan_id: str) -> str:
    """Human-readable summary for a notification. Kept short — details live in
    the dashboard."""
    if not new_findings:
        return ""
    lines = [f"🛡️ Security scan {scan_id}: {len(new_findings)} new actionable finding(s)"]
    for f in new_findings[:10]:
        flag = "EXPLOITED" if f.status == "exploited" else f.severity.upper()
        lines.append(f"  • [{flag}] {f.title} ({f.location}) — {f.topic}")
    if len(new_findings) > 10:
        lines.append(f"  …and {len(new_findings) - 10} more")
    return "\n".join(lines)
