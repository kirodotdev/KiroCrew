"""Plan-credit alerts: one notice when the credits used cross a percentage the user set.

``telemetry.plan_alert_pcts`` lists the percentages of the plan's credits that matter
(for example ``[50, 80, 100]``); the default is empty, which turns the feature off.
After each usage refresh the reading published for the top-bar pill is compared with
that list. Each percentage notifies once per plan period: a notice names the highest
percentage newly crossed, and the lower ones crossed in the same step are marked as
told, so a jump from 40 to 85 percent is one notice, not two.

What was told is kept in ``plan-credit-alerts.json`` under the config directory, so a restart
does not tell the same crossing again. A new plan period (the reading's ``resets``
date changes) or a used-credit count that falls back under a told percentage starts
the list over.

Only a current reading counts: an unavailable one, a stale one dimmed after a failed
refresh, or one without a plan size neither notifies nor changes what was told.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from pathlib import Path
from typing import Any

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.config.paths import config_dir
from kiro_crew.notifications.bus import NotificationPayload

logger = logging.getLogger("kiro_crew.dashboard.plan_credit_alerts")

#: Where the told percentages are kept, beside ``config.json``.
STATE_FILE = "plan-credit-alerts.json"
#: Notification channel and group: the existing agent channel, one group for all crossings.
CHANNEL = "system.agent"
GROUP_KEY = "plan-credit-alert"


def _state_path() -> Path:
    return config_dir() / STATE_FILE


def _read_record(path: Path) -> dict[str, Any]:
    """What was told, or an empty record for a missing or unreadable file."""
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return record if isinstance(record, dict) else {}


def _number(value: object) -> float | None:
    """*value* as a finite non-negative number, else ``None`` (a bool is not a number)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and value >= 0 else None


def decide(
    usage: dict[str, Any], pcts: list[float], record: dict[str, Any]
) -> tuple[str | None, dict[str, Any]]:
    """The notice text to send, if any, and the record to keep.

    Pure: no I/O. ``record`` is ``{"period": str, "told": [percent, ...]}`` as stored.
    A reading that cannot be judged returns ``(None, record)`` untouched.
    """
    used, plan = _number(usage.get("credits_used")), _number(usage.get("credits_plan"))
    if not usage.get("credits_plan") or usage.get("stale") or used is None or not plan:
        return None, record
    percent = used / plan * 100
    period = str(usage.get("resets") or "")
    told = [t for t in record.get("told", []) if isinstance(t, (int, float))]
    if record.get("period") != period:
        told = []
    # A count that fell back under a told percentage (a refund, or a reset the
    # `resets` date did not show) makes that percentage tellable again.
    told = [t for t in told if t <= percent]
    crossed = [p for p in pcts if p <= percent and p not in told]
    kept = {"period": period, "told": sorted(set(told) | set(crossed))}
    if not crossed:
        return None, kept
    top = max(crossed)
    shown = f"{percent:.0f}" if percent >= 10 else f"{percent:.1f}"
    text = (
        f"You have used {shown}% of your plan credits ({used:g} of {plan:g}), "
        f"which is past your {top:g}% alert."
    )
    return text, kept


def _evaluate(usage: dict[str, Any]) -> str | None:
    """Blocking part: read the setting and the record, decide, store the record."""
    pcts = KiroCrewConfig.load().telemetry.plan_alert_pcts
    if not pcts:
        return None
    path = _state_path()
    record = _read_record(path)
    text, kept = decide(usage, list(pcts), record)
    if kept != record:
        try:
            atomic_write(path, json.dumps(kept), restrict_to_owner=True)
        except OSError:
            # Unable to remember: skip this notice rather than repeat it every refresh.
            logger.warning("Plan credit alert: cannot store what was told", exc_info=True)
            return None
    return text


async def notify_if_crossed(state: Any, usage: dict[str, Any]) -> None:
    """Send the notice for a newly crossed percentage, once. Best effort, never raises."""
    try:
        text = await asyncio.to_thread(_evaluate, usage)
        bus = getattr(state, "notification_bus", None)
        if text and bus is not None:
            bus.push(
                NotificationPayload(
                    source="system",
                    channel=CHANNEL,
                    kind="agent",
                    title="Plan credits",
                    body=text,
                    group_key=GROUP_KEY,
                )
            )
    except Exception:  # noqa: BLE001 - an alert miss must not break the usage refresh
        logger.warning("Plan credit alert failed", exc_info=True)
