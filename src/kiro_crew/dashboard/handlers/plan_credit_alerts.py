"""Plan-credit alerts: one notice when the credits used cross a percentage the user set.

``telemetry.plan_alert_pcts`` lists the percentages of the plan's credits that matter
(for example ``[50, 80, 100]``); the default is empty, which turns the feature off.
After each usage refresh the reading published for the top-bar pill is compared with
that list. Each percentage notifies once per plan period and account: a notice names
the highest percentage newly crossed, and the lower ones crossed in the same step are
marked as told, so a jump from 40 to 85 percent is one notice, not two.

What was told is kept in ``plan-credit-alerts.json`` under the config directory, so a restart
does not tell the same crossing again. The record keeps one entry per account, under a
digest of the account (:func:`account_key`, so no e-mail or user id is stored), for at
most :data:`MAX_ACCOUNTS` accounts. Each entry holds the plan period and the
percentages told in it. A new plan period (the reading's ``resets`` date changes) or a
used-credit count that falls back under a told percentage starts that account's list
over. One account's crossings never silence another's, and a switch to another account
and back does not tell the first account's crossings again.

Only a current reading that names its account counts: an unavailable one, a stale one
dimmed after a failed refresh, one without a plan size, or one that names no account
neither notifies nor changes what was told. Evaluations claim a crossing one at a time
(:data:`_record_lock`), so two refreshes that finish together tell it once.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import threading
from pathlib import Path
from typing import Any

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.config.paths import config_dir
from kiro_crew.notifications.bus import NotificationPayload

logger = logging.getLogger("kiro_crew.dashboard.plan_credit_alerts")

#: Where the told percentages are kept, beside ``config.json``.
STATE_FILE = "plan-credit-alerts.json"
#: The most accounts the record keeps; a new one past this drops the least recently told.
MAX_ACCOUNTS = 16
#: Notification channel and group: the existing agent channel, one group for all crossings.
CHANNEL = "system.agent"
GROUP_KEY = "plan-credit-alert"
#: Held across each evaluation's read, decision and write of the record. Evaluations run
#: on worker threads (``asyncio.to_thread``), so a thread lock is what makes claiming a
#: crossing one step: a second evaluation reads the record only after the first stored it.
_record_lock = threading.Lock()


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


def _text(value: object) -> str:
    """*value* when it is a string, else ``""``."""
    return value if isinstance(value, str) else ""


def account_key(usage: dict[str, Any], *, vault_fp: str = "", user_id: str | None = None) -> str:
    """A digest naming the account *usage* was read for, or ``""`` when nothing names it.

    The identity the usage refresh publishes a reading under: the fingerprint of Crew's
    own sign-in for a reading taken from it (*vault_fp*), otherwise the IdC userId kept
    beside the reading (*user_id*) with the e-mail and ``start_url`` the reading carries.
    Hashed, so the record holds no e-mail or user id.
    """
    if vault_fp:
        parts = ["vault", vault_fp]
    else:
        parts = ["cli", _text(user_id), _text(usage.get("email")), _text(usage.get("start_url"))]
        if not any(parts[1:]):
            return ""
    return hashlib.sha256(json.dumps(parts).encode("utf-8")).hexdigest()


def decide(
    usage: dict[str, Any], pcts: list[float], record: dict[str, Any], *, account: str
) -> tuple[str | None, dict[str, Any]]:
    """The notice text to send, if any, and the record to keep.

    Pure: no I/O. ``record`` is ``{"account": str, "period": str, "told": [percent, ...]}``
    as stored, and ``account`` is the reading's :func:`account_key`. A reading that cannot
    be judged, one whose ``account`` is ``""`` included, returns ``(None, record)`` untouched.
    """
    used, plan = _number(usage.get("credits_used")), _number(usage.get("credits_plan"))
    if not account:
        return None, record
    if not usage.get("credits_plan") or usage.get("stale") or used is None or not plan:
        return None, record
    percent = used / plan * 100
    period = str(usage.get("resets") or "")
    told = [t for t in record.get("told", []) if isinstance(t, (int, float))]
    if record.get("account") != account or record.get("period") != period:
        # What another account, or another period, was told says nothing about this one.
        told = []
    # A count that fell back under a told percentage (a refund, or a reset the
    # `resets` date did not show) makes that percentage tellable again.
    told = [t for t in told if t <= percent]
    crossed = [p for p in pcts if p <= percent and p not in told]
    kept = {"account": account, "period": period, "told": sorted(set(told) | set(crossed))}
    if not crossed:
        return None, kept
    top = max(crossed)
    shown = f"{percent:.0f}" if percent >= 10 else f"{percent:.1f}"
    text = (
        f"You have used {shown}% of your plan credits ({used:g} of {plan:g}), "
        f"which is past your {top:g}% alert."
    )
    return text, kept


def _evaluate(usage: dict[str, Any], account: str) -> str | None:
    """Blocking part: read the setting, then read, decide and store the record as one step.

    The file holds one entry per account (``{"accounts": {digest: entry}}``), and only
    this reading's account entry is decided and rewritten, so a switch to another
    account and back finds the first account's crossings still told.
    """
    pcts = KiroCrewConfig.load().telemetry.plan_alert_pcts
    if not pcts:
        return None
    path = _state_path()
    with _record_lock:
        stored = _read_record(path).get("accounts")
        accounts = dict(stored) if isinstance(stored, dict) else {}
        entry = accounts.get(account)
        entry = entry if isinstance(entry, dict) else {}
        text, kept = decide(usage, list(pcts), entry, account=account)
        if kept != entry:
            # The newest entry goes last, so the oldest is the one a full map drops.
            accounts.pop(account, None)
            accounts[account] = kept
            while len(accounts) > MAX_ACCOUNTS:
                accounts.pop(next(iter(accounts)))
            try:
                atomic_write(path, json.dumps({"accounts": accounts}), restrict_to_owner=True)
            except OSError:
                # Unable to remember: skip this notice rather than repeat it every refresh.
                logger.warning("Plan credit alert: cannot store what was told", exc_info=True)
                return None
    return text


async def enabled() -> bool:
    """True when ``telemetry.plan_alert_pcts`` lists a percentage. Never raises.

    The setting is read off the loop, and one that cannot be read is off.
    """
    try:
        pcts = await asyncio.to_thread(lambda: KiroCrewConfig.load().telemetry.plan_alert_pcts)
    except Exception:  # noqa: BLE001 - an unreadable setting reads as off
        logger.debug("Plan credit alert: the setting could not be read", exc_info=True)
        return False
    return bool(pcts)


async def notify_if_crossed(
    state: Any, usage: dict[str, Any], *, vault_fp: str = "", user_id: str | None = None
) -> None:
    """Send the notice for a newly crossed percentage, once per account. Best effort, never raises.

    *vault_fp* and *user_id* are the identity the reading was published under, as the usage
    cache keeps it beside the reading; with the reading's own e-mail and ``start_url`` they
    name the account the record is kept for (:func:`account_key`).
    """
    try:
        account = account_key(usage, vault_fp=vault_fp, user_id=user_id)
        text = await asyncio.to_thread(_evaluate, usage, account)
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
