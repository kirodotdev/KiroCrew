"""Bill data — Cost Explorer month-to-date + projection, cached daily.

One CE query per account per day (~$0.01 each, data lags ~24 h), cached in
the app data dir; the page always renders from cache with its age labelled.
The projection is local arithmetic (MTD extrapolated over the month), and
budget thresholds are evaluated locally too — no AWS Budgets resource is
ever created (an account-level resource a non-technical user would then own
without knowing it exists).

CALLER CONTRACT: handlers gate with ``refuse_and_log(SERVICE_COST_EXPLORER)``
before calling. Sync, subprocess-bound — call via ``asyncio.to_thread``.
"""

from __future__ import annotations

import calendar
import datetime as dt
import json
import logging
from pathlib import Path
from typing import Any, Optional

from kiro_crew.apps.manager import app_data_dir
from kiro_crew.atomic_write import atomic_write
from kiro_crew.deploy.engine import _checked

logger = logging.getLogger(__name__)

APP_NAME = "aws-control"
_CACHE_TTL_SECS = 24 * 3600


def _cache_path(account: str) -> Path:
    # Account ids are 12 digits (validated upstream); defensive strip anyway.
    safe = "".join(c for c in account if c.isdigit())[:16] or "unknown"
    return app_data_dir(APP_NAME) / "costs" / f"{safe}.json"


def read_cached(account: str) -> Optional[dict[str, Any]]:
    """The cached bill for ``account`` regardless of age (age is labelled)."""
    try:
        data = json.loads(_cache_path(account).read_text(encoding="utf-8"))
        # A corrupted/hand-edited cache decoding to a list or scalar must read
        # as "no cache", not crash the console route on the first .get().
        return data if isinstance(data, dict) else None
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None


def is_fresh(cached: Optional[dict[str, Any]]) -> bool:
    if not cached:
        return False
    try:
        fetched = dt.datetime.fromisoformat(cached["fetchedAt"])
    except (KeyError, ValueError, TypeError):
        # TypeError: corrupted cache carrying a non-string (list/number).
        return False
    if fetched.tzinfo is None:
        # A hand-edited timezone-less stamp would make the subtraction below
        # raise TypeError against the aware now(); treat it as UTC.
        fetched = fetched.replace(tzinfo=dt.timezone.utc)
    age = dt.datetime.now(dt.timezone.utc) - fetched
    return age.total_seconds() < _CACHE_TTL_SECS


def fetch_month_costs(profile: str, region: str, account: str) -> dict[str, Any]:
    """Query CE for this month's spend for THIS account, grouped by service.

    CE is a global endpoint (region-independent), but the profile does NOT decide
    the account on its own: a management (payer) profile returns the whole
    organization's spend, which would then be cached and displayed as this one
    account's bill. The ``LINKED_ACCOUNT`` filter is what makes the number mean
    what the page says it means. It is correct for a standalone account too -- CE
    accepts the dimension and returns that account's own spend -- so there is no
    org-vs-standalone branch to get wrong.

    Amounts are UnblendedCost in USD, rounded to cents for display; the raw
    strings stay in the cache for anything that needs precision.
    """
    today = dt.datetime.now(dt.timezone.utc).date()
    start = today.replace(day=1)
    # CE's End is exclusive; asking through tomorrow includes today's partial.
    end = today + dt.timedelta(days=1)
    scope = json.dumps({"Dimensions": {"Key": "LINKED_ACCOUNT", "Values": [account]}})
    out = _checked(
        [
            "ce",
            "get-cost-and-usage",
            "--time-period",
            f"Start={start.isoformat()},End={end.isoformat()}",
            "--granularity",
            "MONTHLY",
            "--metrics",
            "UnblendedCost",
            "--group-by",
            "Type=DIMENSION,Key=SERVICE",
            "--filter",
            scope,
            "--output",
            "json",
        ],
        profile,
        action="ce:GetCostAndUsage",
        timeout=60,
    )
    data = json.loads(out or "{}")
    by_service: list[dict[str, Any]] = []
    total = 0.0
    for period in data.get("ResultsByTime", []):
        for group in period.get("Groups", []):
            amount = float(group["Metrics"]["UnblendedCost"]["Amount"])
            if amount <= 0:
                continue
            total += amount
            by_service.append(
                {"service": (group.get("Keys") or ["?"])[0], "amount": round(amount, 2)}
            )
    by_service.sort(key=lambda row: -row["amount"])
    days_in_month = calendar.monthrange(today.year, today.month)[1]
    elapsed = max(today.day, 1)
    projected = total / elapsed * days_in_month
    result = {
        "account": account,
        "monthToDate": round(total, 2),
        "projected": round(projected, 2),
        "currency": "USD",
        "byService": by_service[:12],
        "periodStart": start.isoformat(),
        "fetchedAt": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    }
    path = _cache_path(account)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, json.dumps(result, indent=1))
    return result
