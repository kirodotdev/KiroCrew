"""Session lifecycle, usage, search, approvals, and reset handlers."""

from __future__ import annotations

import asyncio
import codecs
import functools
import hashlib
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Collection

from kiro_crew.loop_lock import LoopBoundLock

if TYPE_CHECKING:
    from kiro_crew.providers.base import LLMProvider  # noqa: F811

from aiohttp import web

# circular import: handlers/__init__.py re-exports this module's handlers, so
# a `from ... import` of individual names would fail mid-cycle. `import ... as`
# binds via sys.modules and defers attribute access to call time, which also
# keeps tests' monkeypatching of handlers.redact_* effective (late binding).
import kiro_crew.dashboard.handlers as _h
from kiro_crew import hooks, session_directive
from kiro_crew.acp.client import _resolve_kiro_bin_for_spawn
from kiro_crew.agent_discovery import (
    AgentsDirMemo,
    AmbiguousAgentSpecError,
    SensitiveAgentSpecPathError,
    _SpecReadRefused,
    read_agent_spec_strict,
    spec_by_declared_name,
)
from kiro_crew.agent_spec_format import (
    agent_spec_candidates,
    is_markdown_spec,
    iter_agent_spec_files,
)

# The migration module owns the pre-migration leftover-tab spelling.
from kiro_crew.channel_transcript_migration import _orphan_target_stem
from kiro_crew.cloud.login_target import parse_whoami_output
from kiro_crew.config.paths import kiro_agents_dir
from kiro_crew.cron import CronStoreBusy, CronStoreUnreadable, cron_owner_matches
from kiro_crew.dashboard import backend_pins, directive_queue
from kiro_crew.dashboard.chat_utils import (
    effective_session_key,
    slot_history_key,
)
from kiro_crew.dashboard.handlers import kiro_usage_api
from kiro_crew.dashboard.handlers._shared import (
    SESSION_SEARCH_TEXT_FIELDS,
    guard_owner_surface_routes,
    internal_memory_scope,
)
from kiro_crew.dashboard.kiro_readiness import reject_if_kiro_unverified
from kiro_crew.dashboard.session_memory import SessionMemorySampler
from kiro_crew.dashboard.state import DashboardState, _normalize_slot_key
from kiro_crew.executors import subprocess_executor
from kiro_crew.history import (
    SEARCH_MIN_CHARS,
    ConversationLog,
    HistoryLockTimeout,
    _archive_dir,
    is_incognito_transcript,
    transcript_lock_stems,
    transcript_stem,
    transcript_stems,
)
from kiro_crew.llm_helpers import run_bg_oneliner
from kiro_crew.mcp_discovery import sync_discovered_servers
from kiro_crew.messaging.link import _in_namespace, canonical_key
from kiro_crew.pinned_fs import open_fenced_for_read
from kiro_crew.platform import redact_log_via_context
from kiro_crew.sandbox import (
    cgroup_scope_argv,
    configured_sandbox_mode,
    create_subprocess_limited,
    scrub_agent_subprocess_env,
    wrap_argv,
)
from kiro_crew.security import (
    is_sensitive_canonical_path,
    redact,
    redact_credentials,
    redact_exfiltration_urls,
)
from kiro_crew.validation import sanitize_string

logger = logging.getLogger(__name__)

_SHUTDOWN_TIMEOUT_SECS = 10


def _sel():
    """Late-binding _sel() for test monkeypatch compatibility."""
    import kiro_crew.dashboard.handlers as _pkg  # noqa: F811 — circular import

    return _pkg.sel()


# One sampler per process: it carries the CPU jiffy baseline and the rolling load
# window, both of which are meaningless if rebuilt per request (a fresh baseline
# always reports CPU as unknown, and a fresh window is always empty).
_memory_sampler = SessionMemorySampler()


async def api_sessions_memory(request: web.Request) -> web.Response:
    """GET /api/sessions/memory — per-session and per-task memory footprint."""
    state: DashboardState = request.app["state"]
    # Built on the loop, not in the sampling thread: it walks live slot objects.
    # Pure dict work, so it costs nothing here. Guarded because `state` is a
    # MagicMock in much of the suite, whose attribute call returns a mock rather
    # than a dict — the sampler must receive a real mapping or nothing.
    # Built on the loop, not in the sampling thread: it walks live slot objects,
    # and it is pure dict work. `hasattr` because a stub state in the suite may not
    # carry the method at all; validating the VALUE is `_spend_for_session`'s job,
    # so it is not repeated here — one owner for that rule.
    aliases = state.spend_slot_by_session() if hasattr(state, "spend_slot_by_session") else None
    payload = await _memory_sampler.sample(
        state.sessions,
        getattr(state, "subagents", None),
        get_slot=state.get_slot,
        spend_slot_by_session=aliases,
    )
    return web.json_response(payload)


_health_cache: dict[str, Any] = {}
_health_cache_ts: float = 0.0
_health_lock = LoopBoundLock()
_HEALTH_REFRESH_SECS = 15


def _empty_health_payload() -> dict[str, Any]:
    """The payload shape when nothing has been computed yet (or the compute failed)."""
    return {
        "stalled": {},
        "slots": {},
        "waiting": [],
        "recovering": [],
        "queued": {"available": False, "count": 0, "oldest_wait_secs": 0.0, "by_state": {}},
        "effective_caps": {},
        "degrade_reason": None,
        "counts": {"running": 0, "queued": 0, "waiting": 0, "recovering": 0, "stalled": 0},
    }


async def api_sessions_health(request: web.Request) -> web.Response:
    """GET /api/sessions/health — structured session health.

    ``{stalled, slots, waiting, recovering, queued, effective_caps,
    degrade_reason, counts, ...}`` from task rows + slot state + ACP handle
    liveness (``dashboard/session_health.py``); the log scan is a secondary
    evidence source only. ``stalled`` keeps its pre-structured shape
    (``{slot_key: {reason, since_ts, ...}}``) so an older client still reads it.

    The slot snapshot is taken ON the loop (it walks live slot objects), the
    classification, store read and log tail run off it. Cached for
    ``_HEALTH_REFRESH_SECS`` so a busy dashboard cannot turn this into a
    per-request SQLite + file scan.
    """
    global _health_cache, _health_cache_ts
    now = time.monotonic()
    if now - _health_cache_ts > _HEALTH_REFRESH_SECS:
        async with _health_lock:
            # Re-check after acquiring lock (another request may have refreshed)
            if time.monotonic() - _health_cache_ts > _HEALTH_REFRESH_SECS:
                try:
                    from kiro_crew.dashboard import session_health

                    # ``request.app`` is a MagicMock in much of the suite: a
                    # missing ``state`` yields an empty snapshot, and a store that
                    # is not a real TaskStore fails its first read inside the
                    # computation and reads as "unavailable" -- never an error.
                    state = request.app.get("state") if hasattr(request.app, "get") else None
                    taskq = getattr(getattr(state, "subagents", None), "_taskq", None)
                    snapshot = session_health.snapshot_state(state)
                    _health_cache = await asyncio.to_thread(
                        session_health.compute_session_health,
                        None,
                        taskq=taskq,
                        monitor=None,
                        snapshot=snapshot,
                    )
                    _health_cache_ts = time.monotonic()
                except Exception:
                    logger.warning("session_health computation failed", exc_info=True)
                    _health_cache_ts = time.monotonic()
    payload = _empty_health_payload()
    if isinstance(_health_cache, dict):
        payload.update(_health_cache)
    return web.json_response(payload)


_usage_cache: dict[str, object] = {}
_usage_cache_ts: float = 0.0
_USAGE_REFRESH_SECS = 600  # background refresh every 10 min
# Ceiling on ONE whole refresh. Sized above the sum of the inner bounded steps
# (whoami ≤30s + the /usage scrape ≤60s, plus the unbounded API read between
# them) so a healthy slow refresh still completes, while a wedged one is
# guaranteed to release the in-flight guard instead of parking it forever.
_USAGE_FETCH_DEADLINE_SECS = 180
_usage_fetching = False
_MAX_BONUS_GRANTS = 32
_MAX_BONUS_NAME_CHARS = 100
_MAX_BONUS_CREDITS = 1_000_000.0
_MAX_BONUS_DAYS_LEFT = 3_650
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
_BONUS_DASH_RE = re.compile(r"^([\d.]+)/([\d.]+)\s+used\s+\((\d+)\s+days?\s+left\)$")
_BONUS_COLON_RE = re.compile(
    r"^(.+?):\s*([\d.]+)/([\d.]+)\s*\(expires\s+in\s+(\d+)\s+days?\)$",
    re.IGNORECASE,
)

# --- Text-scrape back-off --------------------------------------------------
# The `/usage` text scrape is a kiro-cli slash command handled locally: it calls
# the same free GetUsageLimits API the primary path uses and prints the usage
# table, so no prompt reaches a model and the read costs no credits. It is the
# automatic fallback whenever the API path returns no plan. What it does cost is
# a subprocess (whoami + the scrape, up to a minute and a half) on every refresh
# interval, so a scrape that keeps producing unparseable output (kiro-cli format
# change, wedged CLI, revoked auth) is parked instead of retried forever.

#: Consecutive scrape attempts that produced no usable credit plan.
_usage_scrape_failures = 0
#: monotonic deadline before which no further scrape is attempted.
_usage_scrape_backoff_until = 0.0
#: Consecutive failures tolerated before the scrape is parked. Two refresh
#: intervals of bad luck stay within normal retry; a third means the scrape is
#: broken, and every further attempt is a subprocess spent on output that cannot
#: be parsed.
_USAGE_SCRAPE_FAILURE_THRESHOLD = 3
#: How long a broken scrape is parked. Long relative to the 10-minute refresh so
#: a persistent breakage costs a handful of attempts per day, not one per
#: interval.
_USAGE_SCRAPE_BACKOFF_SECS = 6 * 3600


def _scrape_in_backoff() -> bool:
    """True while a repeatedly-failing scrape is parked."""
    return time.monotonic() < _usage_scrape_backoff_until


def _record_scrape_outcome(success: bool) -> None:
    """Track consecutive scrape failures and park the scrape once they pile up.

    A scrape that cannot produce a usable plan must stop retrying on each TTL
    expiry: each attempt is a minute-scale subprocess for nothing. Any success
    clears the counter, so a transient hiccup does not accumulate toward the
    ceiling.
    """
    global _usage_scrape_failures, _usage_scrape_backoff_until
    if success:
        _usage_scrape_failures = 0
        _usage_scrape_backoff_until = 0.0
        return
    _usage_scrape_failures += 1
    if _usage_scrape_failures >= _USAGE_SCRAPE_FAILURE_THRESHOLD:
        _usage_scrape_backoff_until = time.monotonic() + _USAGE_SCRAPE_BACKOFF_SECS
        logger.warning(
            "Kiro usage: %d consecutive /usage text scrapes yielded no credit "
            "plan; pausing the scrape for %ds so it stops spawning kiro-cli for "
            "unusable output.",
            _usage_scrape_failures,
            _USAGE_SCRAPE_BACKOFF_SECS,
        )


#: Pill ``reason`` for an auth-class usage failure: no live credential could be
#: read, or the API refuses the one that was. The remedy is a fresh sign-in.
_REASON_SIGNIN_REQUIRED = "signin_required"


def _unavailable_reason(api_result: kiro_usage_api.UsageResult) -> str | None:
    """Pick the pill's ``reason`` for a refresh that ends without a reading.

    An auth-class ``auth_state`` (no live credential was readable, or the API
    rejected the one that was) reports ``signin_required``: the free API was
    never answered about this account, and the ``/usage`` scrape needs the same
    sign-in, so the one remedy is signing in again. Anything the API path could
    not prove was an auth problem reports no reason -- a spurious "sign in again"
    on a working sign-in would be the same class of defect in the other direction.

    This is a message only; the scrape decision does not consult it. An empty
    candidate list does NOT prove kiro-cli cannot authenticate, because kiro-cli
    may authenticate from a store this module does not enumerate (see
    :func:`_identity_matches_account`), so the scrape still runs and its own
    outcome decides what the pill shows.
    """
    if api_result.auth_state in (
        kiro_usage_api.AUTH_NO_CREDENTIAL,
        kiro_usage_api.AUTH_REJECTED,
    ):
        return _REASON_SIGNIN_REQUIRED
    return None


def _cache_without_scrape(
    api_usage: object, identity: dict[str, object] | None, reason: str | None = None
) -> None:
    """Cache the best available value when the parked scrape is not going to run.

    Degrades rather than erroring: keep a previously-good value (dimmed
    ``stale``) so the pill does not blink out, otherwise surface whatever
    partial fields the API did return alongside ``available: False`` — the
    frontend's signal to show a no-reading dash (and the modal's Refresh) instead
    of rendering blanks.
    ``reason`` (when given) rides that unavailable marker so the frontend can
    explain WHY instead of hiding silently.

    Preserving is gated on ``_same_identity``: while the scrape is parked, a
    plan-less API answer recurs every refresh for hours, so an unguarded preserve
    would serve the PREVIOUS account's balance and email indefinitely after a
    switch A->B. An unproven identity (missing or mismatched email / start_url,
    including an account that never carried one) therefore reports unavailable
    instead — a dash on the pill is a cosmetic loss, attributing one account's
    spend to another is not.

    ``identity`` is a whoami resolved ADJACENTLY, right before this call, not the
    one read at the top of the refresh: the API attempt sits between the two,
    and a profile switch landing inside it must be judged against the account
    that is signed in now. ``None`` (kiro-cli never resolved) proves nothing, so
    nothing is preserved.
    """
    global _usage_cache, _usage_cache_ts
    if (
        _usage_cache.get("credits_plan") is not None
        and identity is not None
        and _same_identity(_usage_cache, identity)
    ):
        _usage_cache = {**_usage_cache, "stale": True}
    else:
        partial = (
            {k: _redact_strings(v) for k, v in api_usage.items()}
            if (isinstance(api_usage, dict))
            else {}
        )
        partial.pop("_profile_arn", None)
        unavailable: dict[str, object] = {**partial, "available": False}
        if reason is not None:
            unavailable["reason"] = reason
        _usage_cache = unavailable
    _usage_cache_ts = time.time()


def _safe_float(text: str) -> float | None:
    """Parse a float, returning None on malformed input instead of raising."""
    try:
        return float(text)
    except ValueError:
        return None


def _safe_int(text: str) -> int | None:
    """Parse a regex-constrained integer without letting huge input raise."""
    try:
        return int(text)
    except ValueError:
        return None


def _parse_usage(raw: str) -> dict[str, object]:
    """Parse structured fields from kiro-cli /usage output."""
    clean = _ANSI_ESCAPE_RE.sub("", raw)
    result: dict[str, object] = {"raw": ""}

    lines = clean.splitlines()
    usage_lines: list[str] = []
    capture = False
    for line in lines:
        if "Estimated Usage" in line:
            capture = True
        if capture:
            usage_lines.append(line)
    result["raw"] = "\n".join(usage_lines).strip()

    # Parse fields. First-wins on each field so a duplicate header or echoed
    # line later in the (untrusted) output can't overwrite a real value, and
    # malformed numbers skip the field via _safe_float rather than aborting.
    for line in usage_lines:
        if "resets on" in line and "resets" not in result:
            m = re.search(r"resets on (\S+)", line)
            if m:
                result["resets"] = m.group(1)
            if "|" in line:
                result["plan"] = line.rsplit("|", 1)[-1].strip()
        if "Credits used" in line and "credits_used" not in result:
            m = re.search(r"Credits used:\s*([\d.]+)", line)
            if m:
                v = _safe_float(m.group(1))
                if v is not None:
                    result["credits_used"] = v
        if "Est. cost" in line and "cost_usd" not in result:
            m = re.search(r"\$([\d.]+)", line)
            if m:
                v = _safe_float(m.group(1))
                if v is not None:
                    result["cost_usd"] = v
        if "covered in plan" in line and "credits_plan" not in result:
            m = re.search(r"\(([\d.]+)\s+of\s+([\d.]+)", line)
            if m:
                covered = _safe_float(m.group(1))
                plan = _safe_float(m.group(2))
                if covered is not None and plan is not None:
                    result["credits_covered"] = covered
                    result["credits_plan"] = plan
        if "billed at" in line and "overage_rate" not in result:
            m = re.search(r"\$([\d.]+)\s+per", line)
            if m:
                # Coerce to float so both sources emit one type for the
                # canonical shape and consumers never branch on source.
                rate = _safe_float(m.group(1))
                if rate is not None:
                    result["overage_rate"] = rate

    # Kiro CLI has shipped both "name: used/total (expires in N days)" and
    # "name - used/total used (N days left)". Parse both bounded formats and
    # retain every active grant; one malformed line must not poison the rest.
    bonus_credits: list[dict[str, object]] = []
    in_bonus_section = False
    for raw_line in usage_lines:
        line = raw_line.strip()
        if "bonus credits:" in line.casefold():
            in_bonus_section = True
            continue
        if not in_bonus_section:
            continue
        if line.startswith("Credits") or line.startswith("Overages:"):
            break
        if not line:
            continue
        if " - " in line:
            name, usage_text = line.rsplit(" - ", 1)
            match = _BONUS_DASH_RE.fullmatch(usage_text.strip())
            values = match.groups() if match else None
        else:
            match = _BONUS_COLON_RE.fullmatch(line)
            if match:
                name = match.group(1)
                values = match.group(2), match.group(3), match.group(4)
            else:
                name = ""
                values = None
        name = name.strip()
        if not values or not name or len(name) > _MAX_BONUS_NAME_CHARS:
            continue
        used = _safe_float(values[0])
        total = _safe_float(values[1])
        days_left = _safe_int(values[2])
        if (
            used is None
            or total is None
            or days_left is None
            or used < 0
            or total <= 0
            or used > _MAX_BONUS_CREDITS
            or total > _MAX_BONUS_CREDITS
            or days_left > _MAX_BONUS_DAYS_LEFT
            or not name.isprintable()
        ):
            continue
        bonus_credits.append({"name": name, "used": used, "total": total, "days_left": days_left})
        if len(bonus_credits) >= _MAX_BONUS_GRANTS:
            break
    # Preserve an observed empty section as an explicit empty list, so callers
    # can distinguish "no active grants" from an output format with no section.
    if in_bonus_section:
        result["bonus_credits"] = bonus_credits
    return result


def _normalize_text_usage(parsed: dict[str, object]) -> dict[str, object]:
    """Convert the text-scrape parse result to the canonical usage shape.

    Emits the canonical usage shape the dashboard consumes so it never branches
    on source:
      credits_used = TOTAL used, credits_overage = overage above plan,
      credits_covered = in-plan portion, credits_plan = limit, percentage.

    In the raw text, "Credits used:" is the OVERAGE field (0 for org accounts,
    and absent entirely on kiro-cli 2.11.x), while "(X of Y covered in plan)" is
    the in-plan covered/limit. Total = covered + overage. When the text carries
    no overage, this honestly reports covered==total.
    """
    covered = parsed.get("credits_covered")
    plan = parsed.get("credits_plan")
    if not isinstance(covered, (int, float)) or not isinstance(plan, (int, float)):
        # No usable credit plan — preserve whatever parsed (e.g. just {"raw": ...}).
        return dict(parsed)
    raw_used = parsed.get("credits_used")
    overage = float(raw_used) if isinstance(raw_used, (int, float)) else 0.0
    total = float(covered) + overage
    out: dict[str, object] = dict(parsed)
    out["credits_used"] = total
    out["credits_overage"] = overage
    out["credits_covered"] = float(covered)
    out["credits_plan"] = float(plan)
    out["percentage"] = round(total / plan * 100, 1) if plan else 0.0
    out["source"] = "text"
    return out


def _same_identity(cached: dict[str, object], identity: dict[str, object]) -> bool:
    """True only when ``cached`` provably belongs to the current ``identity``.

    Matches on BOTH the account email AND the SSO ``start_url`` (the IAM
    Identity Center instance): the same human email can appear across different
    Identity Center orgs, so email alone does not prove the same account —
    pairing it with the issuer URL does. Every compared field must be a
    non-empty string and equal; anything missing or mismatched is UNPROVEN
    (``False``). The current identity's values are routed through the same
    redaction the cached copy went through so the two are compared in the same
    form.

    This is the gate that stops an account switch A->B (with the API failing on
    B's refresh) from keeping A's usage AND A's email on screen — including the
    same-email-different-org case — without it, preserving the prior cache would
    leak the previous account's data.
    """

    def _red(v: object) -> object:
        return _redact_strings(v) if isinstance(v, str) else v

    for key in ("email", "start_url"):
        a = cached.get(key)
        b = _red(identity.get(key))
        if not (isinstance(a, str) and isinstance(b, str) and a and a == b):
            return False
    return True


#: Verdicts of :func:`_whoami_agreement` on the two whoami snapshots of one refresh.
_WHOAMI_SAME = "same"
_WHOAMI_DIFFERENT = "different"
_WHOAMI_UNPROVEN = "unproven"


def _whoami_agreement(before: dict[str, object], after: dict[str, object]) -> str:
    """Judge whether two whoami snapshots taken during one refresh PROVE one account.

    The refresh reads whoami at its top (the credential anchor) and again right
    before it publishes an API reading, and that reading may be published only
    on ``_WHOAMI_SAME``: a profile switch that lands between the two reads would
    otherwise cache the FIRST account's balance and email under the second
    account's session. The verdict is field-wise over the identity evidence --
    the account email, its SSO ``start_url`` and the profile ARN:

    * ``_WHOAMI_SAME`` -- at least one field is a non-empty string in BOTH
      snapshots and equal, and no field carried by either snapshot disagrees.
      That is at least the proof :func:`_same_identity` demands (email AND
      ``start_url``, so the same-email-different-org case is a mismatch) and
      also serves the shapes that carry less: a social login reports email and
      ARN, a Builder ID account reports its email alone.
    * ``_WHOAMI_DIFFERENT`` -- a field is carried by one snapshot and not the
      other, or carried by both with different values. That is a switch, or a
      session that lapsed mid-refresh; the reading belongs to whoever was
      signed in a moment ago and nothing of it may be published or kept.
    * ``_WHOAMI_UNPROVEN`` -- NEITHER snapshot carries any identity evidence
      (kiro-cli printed no identity both times). Nothing proves the account,
      so the API reading is not published: with no anchoring ARN the API
      accepted whichever stored token it found, and the sign-out half of a
      profile switch reads exactly like this while the outgoing account's
      token is still on disk. The caller falls back to kiro-cli's own
      ``/usage`` panel, which describes the signed-in account by construction.

    Two empty snapshots therefore never count as agreement: absence of a
    switch is not evidence of an account.
    """
    proven = False
    for key in ("email", "start_url", "_profile_arn"):
        a, b = before.get(key), after.get(key)
        if a is None and b is None:
            continue
        if isinstance(a, str) and isinstance(b, str) and a and a == b:
            proven = True
            continue
        return _WHOAMI_DIFFERENT
    return _WHOAMI_SAME if proven else _WHOAMI_UNPROVEN


def _text_scrape_regresses_api_value(
    prev: object, new: dict[str, object], identity: dict[str, object]
) -> bool:
    """True when a fresh text-scrape would clobber a richer API-sourced value.

    The text scrape is overage-blind for org-managed accounts: recent kiro-cli
    dropped the overage line from ``/usage`` stdout, so ``_normalize_text_usage``
    caps ``credits_used`` at the plan (``covered + 0``) and reports zero overage.
    The API path (``GetUsageLimits``) still returns the true total. When the API
    call transiently fails and we fall back to the scrape, accepting that capped
    value would overwrite the good API number and flip the pill from the real
    figure (e.g. 41,336/10,000 = 413%) to a misleading 10,000/10,000 = 100%,
    hiding all overage — the observed oscillation bug.

    Guard against exactly that, but ONLY when it is safe to keep the prior value:
      * the cached value is ``source == "api"`` (authoritative), AND
      * it provably belongs to the CURRENT identity (``_same_identity``) — so an
        account switch never pins the previous account's usage/email, AND
      * it is the SAME billing cycle — BOTH ``resets`` dates present, non-empty
        and equal; a missing or changed date lets the lower scrape win, AND
      * it reports strictly more usage than the overage-blind scrape can see.
    Text-only environments (no API prior) update normally, and a genuine
    billing-cycle reset is reported by the primary API path, which runs first
    every cycle, so this never pins a stale-high value once the API recovers.
    """
    if not isinstance(prev, dict) or prev.get("source") != "api":
        return False
    if not _same_identity(prev, identity):
        return False
    # Preserve ONLY within the same, provable billing cycle: both reset dates
    # must be present, non-empty, and equal. A missing date on either side
    # (e.g. GetUsageLimits omitting nextDateReset) is unprovable, so let the
    # lower scrape win — a cycle rollover must never pin last cycle's total.
    # Both sources emit `resets` as "%Y-%m-%d", so equality is apples-to-apples.
    prev_resets = prev.get("resets")
    new_resets = new.get("resets")
    if not (
        isinstance(prev_resets, str)
        and prev_resets
        and isinstance(new_resets, str)
        and new_resets
        and prev_resets == new_resets
    ):
        return False
    prev_used = prev.get("credits_used")
    new_used = new.get("credits_used")
    if not isinstance(prev_used, (int, float)) or not isinstance(new_used, (int, float)):
        return False
    return prev_used > new_used


def _redact_strings(value: object) -> object:
    """Recursively redact credentials / exfil URLs from every string leaf.

    Walks dicts and lists so nested values cannot bypass redaction, used on
    untrusted kiro-cli output before it is cached and served to the dashboard.
    """
    if isinstance(value, str):
        value, _ = redact_exfiltration_urls(value)
        value, _ = redact_credentials(value)
        return value
    if isinstance(value, dict):
        return {k: _redact_strings(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_strings(v) for v in value]
    return value


def _publish_usage(payload: dict[str, object]) -> None:
    """Atomically replace the cache served by ``/api/sessions/usage``."""
    global _usage_cache, _usage_cache_ts
    _usage_cache = payload
    _usage_cache_ts = time.time()


def _cache_transient_failure(identity: dict[str, object] | None, reason: str | None = None) -> None:
    """Record a usage-fetch failure without blanking the pill for the same account.

    A timeout, an unexpected error, or a single unparseable scrape is transient.
    Overwriting a previously-good cache with ``{"available": False}`` on any of
    these hid the credit pill entirely for up to a full refresh interval — the
    "disappearing pill" bug. Instead, when we already hold a good value FOR THE
    SAME ACCOUNT, keep it and flag it ``stale`` (the dashboard can dim it); fall
    back to ``available: False`` when there is no prior value to show (e.g. a
    cold-start failure). The definitive "kiro-cli absent" case still sets
    ``available: False`` directly at its call site — that is not transient.

    Preserving is gated on ``_same_identity``, exactly as :func:`_cache_without_scrape`
    gates it: a plan-less refresh is what an account switch A->B looks like when
    B's credential has lapsed (the API reports no plan, the scrape prints no
    table), and that shape recurs on every interval until B signs in again. An
    unguarded preserve would keep A's balance and email on screen under B's
    session the whole time. ``identity`` is this refresh's whoami; ``None`` means
    it was never resolved (the refresh failed before or during whoami), and an
    unresolved or unproven identity reports unavailable rather than preserving —
    a dash on the pill is a cosmetic loss, attributing one account's balance to
    another is not.

    ``reason`` rides the unavailable marker when the caller knows why the read
    ended empty (the API's auth-class verdict, see :func:`_unavailable_reason`),
    so the pill can name the remedy instead of hiding.
    """
    global _usage_cache, _usage_cache_ts
    if (
        _usage_cache.get("credits_plan") is not None
        and identity is not None
        and _same_identity(_usage_cache, identity)
    ):
        _usage_cache = {**_usage_cache, "stale": True}
    else:
        _usage_cache = {"available": False}
        if reason is not None:
            _usage_cache["reason"] = reason
    _usage_cache_ts = time.time()


def _wrap_argv_at_configured_tier(argv: list[str]) -> tuple[list[str], str | None]:
    """Sandbox-wrap a one-shot ``kiro-cli`` argv at the configured tier.

    BLOCKING on two counts, which is why both callers hand it to an executor
    rather than calling it inline: :func:`configured_sandbox_mode` stats (and on a
    cache miss re-reads and revalidates) ``config.json``, and ``wrap_argv`` ->
    ``detect_backend`` can cold-probe the sandbox backend with a synchronous
    ``subprocess.run(..., timeout=5)``. Both reads must therefore happen in the
    worker thread — resolving the mode on the loop and passing it in would leave
    half the blocking work behind.

    Exists so the mode resolution and the wrap cannot drift apart between the
    identity fetch and the usage scrape: they spawn the same binary and must take
    the same tier.

    ``is_kiro_cli=True`` is explicit because ``_spawns_kiro_cli``'s basename test
    only matches a literal ``kiro-cli``: a Windows ``kiro-cli.exe``, a wrapper
    shim, or a ``KIROCREW_KIRO_BIN`` pointing at a nonstandard launch path all
    read as "not kiro-cli". The positive classification is also the security gate
    for default Windows delegation to Kiro's internal sandbox; basename inference
    cannot grant it. Both callers here spawn kiro-cli by construction, and both ACP
    spawn paths pass the same flag for the same reason.
    """
    return wrap_argv(argv, mode=configured_sandbox_mode(), is_kiro_cli=True)


def _wrap_argv_usage_scrape(kiro_bin: str) -> tuple[list[str], str | None]:
    """Executor entrypoint for the ``/usage`` scrape's wrap (see
    :func:`_wrap_argv_at_configured_tier` for why this runs off the loop)."""
    return _wrap_argv_at_configured_tier(
        [kiro_bin, "chat", "--no-interactive", "--agent", "kirocrew-lite", "/usage"]
    )


def _wrap_argv_whoami(kiro_bin: str) -> tuple[list[str], str | None]:
    """Executor entrypoint for the ``whoami`` identity fetch's wrap (see
    :func:`_wrap_argv_at_configured_tier` for why this runs off the loop)."""
    return _wrap_argv_at_configured_tier([kiro_bin, "whoami", "--format", "json"])


async def _fetch_whoami(kiro_bin: str) -> dict[str, object]:
    """Return the signed-in identity from ``kiro-cli whoami --format json``.

    Answers "who is this Kiro account?" — the credit API cannot: GetUsageLimits
    carries no identity, and the SSO token cache holds only opaque tokens (no
    email/openid scopes). kiro-cli resolves the identity itself, so it is the
    only local source of the account email.

    Returns a dict with any of ``email`` / ``account_type`` / ``start_url``, or
    ``{}`` on any failure — identity is decorative here, so it must never break
    the credit readout. A caller that must tell "could not read" apart from
    "no identity" uses :func:`_fetch_whoami_or_none`.
    """
    return (await _fetch_whoami_or_none(kiro_bin)) or {}


async def _fetch_whoami_or_none(kiro_bin: str) -> dict[str, object] | None:
    """Run ``kiro-cli whoami --format json`` and parse it, keeping failure distinct.

    Returns the parsed identity when whoami answered (``{}`` when it exited
    cleanly reporting none), and ``None`` when nothing is known — it timed out,
    could not start, or exited nonzero without printing an identity. stdout is
    untrusted: only the LEADING JSON object is parsed (kiro-cli appends a
    non-JSON "Profile:" block after it), values must be strings, and each is
    length-bounded before it can reach the cache/UI.
    """
    proc = None
    cleanup = None
    try:
        # Configured tier, not a hardcoded "standard": this is the same binary
        # chat spawns, so it must not demand stricter isolation than chat does.
        # Where the operator set agent.sandbox="off" (isolation deferred to
        # kiro-cli's own internal sandbox), the pinned "standard" tier could
        # silently diverge from chat and drop the identity this readout labels the
        # credit numbers with. The explicit Kiro classification also lets the
        # default Windows tier delegates through Kiro's internal sandbox.
        # Off the loop: see _wrap_argv_at_configured_tier for the two blocking reads.
        argv, cleanup = await asyncio.get_running_loop().run_in_executor(
            subprocess_executor(), _wrap_argv_whoami, kiro_bin
        )
        argv = cgroup_scope_argv(argv)
        proc = await create_subprocess_limited(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=scrub_agent_subprocess_env(),
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=30)
        raw = (out or err or b"").decode(errors="replace")
        # The JSON scrape is shared with the cloud launch paths
        # (cloud/login_target.parse_whoami_output): leading object only, string
        # values, length-bounded. The ARN lives AFTER the JSON object.
        out_map: dict[str, object] = dict(parse_whoami_output(raw))
        if not out_map:
            # Nonzero without an identity: an expired or broken session, a CLI
            # fault -- nothing is known. A clean exit with no identity is known.
            return None if proc.returncode else {}
        # whoami's own profile ARN, printed in the trailing (non-JSON) "Profile:"
        # block. Private (leading underscore): used only to prove this identity
        # belongs to the same account the credit numbers came from, and stripped
        # before anything is cached or served.
        m = re.search(r"arn:aws:codewhisperer:[^\s\"']+", raw)
        if m:
            out_map["_profile_arn"] = m.group(0)[:200]
        return out_map
    except (asyncio.TimeoutError, ValueError, OSError):
        logger.debug("whoami identity fetch failed", exc_info=True)
        return None
    except Exception:
        logger.debug("whoami identity fetch failed (unexpected)", exc_info=True)
        return None
    finally:
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
                await asyncio.wait_for(proc.wait(), timeout=5)
            except Exception:
                pass
        if cleanup:
            try:
                os.remove(cleanup)
            except OSError:
                pass


async def fetch_local_identity() -> dict[str, object] | None:
    """Return this machine's signed-in Kiro identity, ``{}`` or ``None``.

    The one dashboard-side door to ``kiro-cli whoami``: it resolves the same
    binary chat spawns and runs :func:`_fetch_whoami_or_none` at the configured
    sandbox tier. Other handlers (the Remote Crew launch form's identity
    preselect) call this instead of reaching into the ACP layer themselves, so
    the agent-backend boundary stays where this module already crosses it.
    ``{}`` when kiro-cli is absent (non-Kiro provider) or whoami answered with
    no identity -- this machine has no sign-in to inherit; ``None`` when whoami
    could not answer -- the identity is unknown, and a caller must not present
    the default as if it had been read.
    """
    kiro_bin = await _resolve_kiro_bin_for_spawn()
    if not kiro_bin:
        return {}
    return await _fetch_whoami_or_none(kiro_bin)


def _identity_matches_account(api_arn: object, identity: dict[str, object]) -> bool:
    """True only when ``identity`` provably describes the account billed for the credits.

    ``fetch_usage_limits`` tries several candidate credentials (IDE cache first,
    then the kiro-cli store) and keeps whichever the API accepted, while
    ``kiro-cli whoami`` always reports kiro-cli's own identity. With two
    different accounts signed in those are different accounts — showing one's
    email above the other's overage would misattribute a bill.

    The ONLY accepted proof is a matching profile ARN on both sides. In
    particular, "there was just one credential we could read" is NOT proof:
    kiro-cli may authenticate from a store this module does not enumerate (e.g.
    a platform-specific app-data path), so a lone readable credential does not
    establish that whoami used it.

    Consequence, deliberately: accounts with no profile ARN at all (individual /
    Builder ID) can never be proven, so they show no identity. Under-reporting an
    identity is a cosmetic gap; mislabelling whose overage bill this is, is not.
    """
    whoami_arn = identity.get("_profile_arn")
    return isinstance(api_arn, str) and isinstance(whoami_arn, str) and api_arn == whoami_arn


#: Outcome of a refresh whose API read returned no plan while the scrape was
#: parked: no reading was fetched, and :func:`_cache_without_scrape` decided what
#: the cache holds (a same-identity prior reading dimmed ``stale``, otherwise an
#: unavailable marker).
_SKIPPED_SCRAPE_PARKED = "scrape_parked"


async def _fetch_usage_bg() -> str | None:
    """Fetch usage and update the cache: the free API first, then the ``/usage``
    scrape whenever the API returns no plan and the scrape is not parked.

    Single-flight through ``_usage_fetching``: a refresh already in progress
    makes this call return at once, so the timer and the account modal's
    Refresh can never run two kiro-cli subprocesses for one reading.

    Identity invariant, for BOTH sources: a reading is published only when a
    whoami taken immediately before the credential read and one taken
    immediately after it PROVE the same account (:func:`_whoami_agreement` is
    ``same``). The API read is bracketed by the top-of-refresh whoami (nothing
    but synchronous checks sit between the two) and one adjacent to its
    publish; the ``/usage`` scrape is bracketed by a whoami taken right before
    the spawn and one taken right after the child returns, before its output
    is parsed. ``different`` publishes nothing and keeps nothing of the prior
    account; ``unproven`` (no identity either side) publishes nothing either
    -- the API branch then falls back to the scrape, and the scrape branch,
    having nothing left to fall back to, reports unavailable. A kiro-cli whose
    whoami prints no identity therefore yields no reading at all: that is the
    intended posture, since a switch between two empty snapshots cannot be
    detected. Every failure path judges what it keeps against the whoami
    adjacent to that decision, never the top-of-refresh one.

    Returns ``_SKIPPED_SCRAPE_PARKED`` when the API yielded no plan AND the
    scrape is parked -- skipped for being parked already, OR attempted in this
    very refresh and parked by that attempt's failure (the third miss). It is
    the one outcome the refresh route reports differently, because no reading
    was fetched and none will be until the park lifts: the cache then holds a
    same-identity prior reading dimmed ``stale``, or an unavailable marker.
    Every other outcome, including the early return above, returns ``None``.
    """
    global _usage_cache, _usage_cache_ts, _usage_fetching
    if _usage_fetching:
        return None
    _usage_fetching = True
    proc = None
    sandbox_cleanup = None
    kiro_bin: str | None = None
    # Only a refresh that actually SPAWNED the scrape feeds the failure
    # backoff — an API-path error or a missing kiro-cli says nothing about
    # whether the scrape works.
    scrape_attempted = False

    async def _adjacent_identity() -> dict[str, object] | None:
        """Re-resolve whoami adjacent to a credential read or a cache decision.

        The identity read at the top of the refresh can be a minute or more old
        by the time a source has answered (the API attempt plus the scrape's
        own timeout), and a profile switch can land inside that window. Judging
        against the top-of-refresh value would then match the PREVIOUS account:
        a failure path would keep its balance on screen under the new one, and
        a success path would publish its balance and email as the new one's. So
        each credential read is bracketed by a whoami immediately before and
        one immediately after it (:func:`_whoami_agreement` decides whether the
        pair proves one account), and each failure path judges what it keeps
        against a whoami resolved adjacently. ``None`` when kiro-cli was never
        resolved: nothing can be proven, nothing is kept or published. whoami
        costs no credits and is bounded, so each call is one short subprocess.
        """
        return await _fetch_whoami(kiro_bin) if kiro_bin else None

    async def _reap_scrape_proc() -> None:
        """Kill and reap the ``/usage`` scrape child, once, if it is still running.

        kill() is non-blocking; the wait is what closes the asyncio transport
        and pipe FDs (otherwise they leak, and this runs on a timer), bounded
        so a wedged process cannot reintroduce the unbounded hang. Idempotent:
        the handle is dropped once reaped, so the ``finally`` below has nothing
        left to do after a handler already reaped it.
        """
        nonlocal proc
        if proc is None:
            return
        child, proc = proc, None
        if child.returncode is not None:
            return
        try:
            child.kill()
            await asyncio.wait_for(child.wait(), timeout=5)
        except Exception:
            pass

    async def _refresh() -> str | None:
        nonlocal proc, sandbox_cleanup, kiro_bin, scrape_attempted
        global _usage_cache, _usage_cache_ts

        kiro_bin = await _resolve_kiro_bin_for_spawn()
        if not kiro_bin:
            # kiro-cli absent (non-Kiro provider): cache an unavailable marker so
            # the dashboard shows its no-reading dash instead of polling forever.
            _publish_usage({"available": False})
            return None
        # Identity FIRST, because it is the anchor for credential selection.
        # ``whoami`` is kiro-cli's own account, and it costs no credits; passing
        # its profile ARN into fetch_usage_limits is what stops a still-valid
        # credential from a signed-out profile supplying the numbers. Fetched
        # once here and reused by both the API and text branches below.
        identity = await _fetch_whoami(kiro_bin)
        # Fail fast on API-key auth. kiro-cli's whoami reports the AuthMethod
        # enum variant ``ApiKey``; the compare normalizes case and strips
        # separators so an upstream respelling (``API_KEY``, ``Api-Key``)
        # still fails fast instead of silently regressing to the slow path —
        # such accounts hold no SSO/OIDC bearer token, so ``fetch_usage_limits``
        # would spend its full timeout walking credential stores that cannot
        # contain one, and the text scrape is no better a source. The
        # ``reason`` rides the existing unavailable-marker shape so the
        # frontend can say WHY, with a label specific to this auth type.
        account_type = identity.get("account_type")
        if (
            isinstance(account_type, str)
            and re.sub(r"[^a-z0-9]", "", account_type.lower()) == "apikey"
        ):
            _publish_usage({"available": False, "reason": "api_key_auth"})
            logger.info("Kiro usage: not available under API key auth; skipping fetch")
            return None
        raw_arn = identity.get("_profile_arn")
        expected_arn = raw_arn if isinstance(raw_arn, str) and raw_arn else None
        # Primary source: the real GetUsageLimits API. It reads the live bearer
        # token kiro-cli already maintains and returns the true used/limit/overage,
        # so it survives kiro-cli stdout format changes, including a text format
        # that drops the overage line.
        #
        # Both ARN values are safe to pass. An ARN anchors on identity; None
        # anchors on PROVENANCE (kiro-cli's own auth store only) — see
        # fetch_usage_limits. So an account with no profile ARN, and a whoami that
        # could not be resolved at all, both still get the API call ahead of the
        # slower text scrape, while an unprovable credential is still refused.
        #
        # Runs on the subprocess pool (not the default to_thread pool): the client
        # makes blocking urllib calls that can hang on DNS / a wedged TLS
        # handshake, so they are isolated from the maintenance/cron pools. Fails
        # closed (returns None) so we fall through to the text scrape rather than
        # showing a fabricated number.
        api_result = await asyncio.get_running_loop().run_in_executor(
            subprocess_executor(),
            functools.partial(kiro_usage_api.fetch_usage_limits, expected_arn=expected_arn),
        )
        # ``usage`` is the number; ``auth_state`` is why it is missing when it is,
        # and is only ever consulted to pick the unavailable message below.
        api_usage = api_result.usage
        if api_usage and api_usage.get("credits_plan") is not None:
            # API output is untrusted too: redact every string leaf before caching.
            api_usage = {k: _redact_strings(v) for k, v in api_usage.items()}
            # Strip the private coupling metadata before it can reach the cache.
            api_arn = api_usage.pop("_profile_arn", None)
            # The reading is judged against a whoami resolved HERE, adjacent to
            # the publish, not against the top-of-refresh one. The API attempt
            # sat between the two (up to its own timeout), and a profile switch
            # can land inside it; the top identity anchored the credential the
            # API used, so comparing the API's ARN with THAT identity passes by
            # construction and can prove nothing about who is signed in now.
            fresh_identity = (await _adjacent_identity()) or {}
            agreement = _whoami_agreement(identity, fresh_identity)
            if agreement == _WHOAMI_DIFFERENT:
                # The numbers belong to the account that was signed in a moment
                # ago: nothing of it is published, and the failure path judges
                # the cache against the account that is signed in now, so
                # nothing of the previous one is kept either.
                logger.info(
                    "Kiro usage: the signed-in account changed during the refresh; "
                    "discarding the reading fetched for the previous one"
                )
                _cache_transient_failure(fresh_identity)
                return None
            if agreement == _WHOAMI_SAME:
                # Attach the signed-in identity ONLY when it provably belongs to
                # the account these credits were billed to (see
                # _identity_matches_account) -- the ADJACENT identity, so the
                # fields published are the account's current ones. The no-ARN
                # (Builder ID) case carries no proof and publishes the numbers
                # alone.
                if _identity_matches_account(api_arn, fresh_identity):
                    api_usage.update(
                        {
                            k: _redact_strings(v)
                            for k, v in fresh_identity.items()
                            if not k.startswith("_")
                        }
                    )
                _publish_usage(api_usage)
                logger.info(
                    "Kiro usage refreshed (api): %s / %s credits",
                    api_usage.get("credits_used", "?"),
                    api_usage.get("credits_plan", "?"),
                )
                return None
            # Unproven: kiro-cli printed no identity either time, so the API --
            # anchored on nothing -- may have read whichever stored token it
            # found, the outgoing account's included. The reading is dropped
            # here, and dropped BEFORE the fallback below so that neither the
            # parked-scrape marker nor anything else can surface its numbers.
            # The `/usage` scrape that follows is kiro-cli's own panel: it
            # describes the signed-in account by construction and labels
            # itself from its own adjacent whoami.
            logger.info(
                "Kiro usage: no signed-in identity could be proven for the API "
                "reading; falling back to the /usage scrape"
            )
            api_usage = None
        # Fallback: scrape kiro-cli /usage stdout. `/usage` is a slash command
        # kiro-cli answers locally from the same free GetUsageLimits call, so
        # this costs no credits; what it costs is a subprocess. Lossy for
        # org-managed accounts on recent kiro-cli (no overage line), but the only
        # source when the API path is unavailable (no readable token / non-Kiro
        # build). It stops once it has failed enough times to look broken; that
        # check is before the spawn, so a parked scrape costs nothing at all.
        if _scrape_in_backoff():
            # Judged against a whoami resolved HERE, not the top-of-refresh one:
            # the API attempt sits between them and a profile switch can land
            # inside it (see _adjacent_identity). The API's partial fields (a
            # plan name, a reset date) came from a credential anchored on the
            # TOP identity, so they ride the unavailable marker only when this
            # whoami and the top one prove the same account.
            parked_identity = (await _adjacent_identity()) or {}
            if _whoami_agreement(identity, parked_identity) != _WHOAMI_SAME:
                api_usage = None
            _cache_without_scrape(
                api_usage, parked_identity, reason=_unavailable_reason(api_result)
            )
            return _SKIPPED_SCRAPE_PARKED
        scrape_attempted = True
        # Route through the OS-level sandbox, consistent with how the main agent
        # kiro-cli process is spawned (AcpClient._spawn -> wrap_argv) — including
        # the TIER. This is a `kiro-cli chat` invocation, so a hardcoded
        # "standard" asks for stricter isolation than the very same chat binary
        # gets on the interactive path, and fail-closes wherever no backend
        # exists. Doubly wasteful here: the refusal also feeds the backoff
        # counter that eventually parks the scrape.
        #
        # OFF the loop, for two blocking reads: `configured_sandbox_mode()` stats
        # (and on a cache miss re-reads + revalidates) config.json, and
        # `wrap_argv` -> `detect_backend` can cold-probe the sandbox backend with
        # a synchronous `subprocess.run(..., timeout=5)`. The gate above already
        # offloads its own config read for the same reason; doing one of the two
        # on the loop would leave the freeze this refresh's timer reintroduces
        # every interval. Same form and reason as `papyrus/backend/latex._run`.
        argv, sandbox_cleanup = await asyncio.get_running_loop().run_in_executor(
            subprocess_executor(),
            _wrap_argv_usage_scrape,
            kiro_bin,
        )
        argv = cgroup_scope_argv(argv)  # cgroup DoS ceiling
        # The scrape reads whichever account kiro-cli has signed in WHEN IT
        # RUNS, so it is bracketed like the API read: one whoami immediately
        # before the spawn (not the top-of-refresh one -- the API attempt and
        # the sandbox wrap sit between) and one immediately after the child
        # returns, before anything is parsed. Only a pair that proves the same
        # account may label and publish what came back between them.
        before = (await _adjacent_identity()) or {}
        proc = await create_subprocess_limited(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=scrub_agent_subprocess_env(),
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=60)
        raw = (out or err or b"").decode(errors="replace")
        after = (await _adjacent_identity()) or {}
        parsed = _parse_usage(raw)
        if parsed.get("credits_plan") is not None:
            # A parseable plan means the scrape itself works, so clear any
            # accumulated failures even on the paths below that discard the
            # value (for being overage-blind, or for an unproven account --
            # neither is the scrape being broken).
            _record_scrape_outcome(True)
            agreement = _whoami_agreement(before, after)
            if agreement != _WHOAMI_SAME:
                # `different`: the child ran across a switch, so its numbers
                # belong to whoever was signed in at that instant -- unknowable,
                # so unpublishable. `unproven`: no identity either side, so a
                # switch between the two would be invisible; there is no source
                # left to fall back to, so this refresh ends with no reading.
                # Either way the cache is judged against the account signed in
                # NOW, so nothing of a previous one is kept.
                logger.info(
                    "Kiro usage: the signed-in account could not be proven across "
                    "the /usage scrape (%s); discarding its reading",
                    agreement,
                )
                _cache_transient_failure(after)
                return None
            # Converge on the canonical shape (credits_used = total, explicit
            # credits_overage) so the dashboard never branches on source, then
            # redact credentials / exfil URLs from every string leaf before the
            # dict is cached and served (kiro-cli output is untrusted).
            parsed = _normalize_text_usage(parsed)
            # `after` gates the preservation guard below AND labels the scrape if
            # we proceed -- both judge against the same adjacent identity, the
            # one proven to have been signed in throughout the read.
            if _text_scrape_regresses_api_value(_usage_cache, parsed, after):
                # The overage-blind text scrape reports less usage than a richer
                # API value we already hold (the API call just transiently
                # failed) that belongs to THIS account AND THIS billing cycle.
                # Overwriting it would flip the pill from the true overage to a
                # capped 100%, so keep the API figure and only dim it as stale.
                _usage_cache = {**_usage_cache, "stale": True}
                _usage_cache_ts = time.time()
                logger.info(
                    "Kiro usage: kept API value (%s credits) over "
                    "overage-blind text scrape (%s)",
                    _usage_cache.get("credits_used", "?"),
                    parsed.get("credits_used", "?"),
                )
                return None
            parsed = {k: _redact_strings(v) for k, v in parsed.items()}
            # No ARN coupling check here: this scrape IS kiro-cli's own `/usage`
            # output, and `before`/`after` have just proven that one account was
            # signed in on both sides of it, so the output and `after` describe
            # the same account. The API branch follows the same contract with
            # one more step: its numbers come from a credential anchored on the
            # top-of-refresh whoami, so after its own bracket proves `same` it
            # also couples the identity fields to the billed ARN
            # (`_identity_matches_account`). Both branches publish only what the
            # account signed in throughout the read can be shown to own.
            parsed.update(
                {k: _redact_strings(v) for k, v in after.items() if not k.startswith("_")}
            )
            _publish_usage(parsed)
            logger.info(
                "Kiro usage refreshed (text): %s credits used",
                parsed.get("credits_used", "?"),
            )
            return None
        # No parseable credit plan this cycle (unrecognized /usage output, or
        # transient garbage). Keep the last good value (stale) when it belongs to
        # this same account rather than blanking the pill; hide when we have
        # nothing provably ours to show. The account is judged against `after`,
        # the whoami adjacent to this decision, not the top-of-refresh one.
        # When the API had already classified the failure as auth-class, the
        # scrape failing too is the expected shape of a lapsed sign-in, so the
        # marker names that remedy.
        _record_scrape_outcome(False)
        _cache_transient_failure(after, reason=_unavailable_reason(api_result))
        return _parked_by_this_attempt()

    def _parked_by_this_attempt() -> str | None:
        # A failure that was the third miss has just parked the scrape. The
        # caller pressed Refresh and got no reading; it must also learn that
        # pressing again is pointless until the park lifts, exactly as it does
        # when the scrape was parked before the press. The API yielded no plan
        # whenever a scrape was attempted, so the park alone decides.
        return _SKIPPED_SCRAPE_PARKED if _scrape_in_backoff() else None

    try:
        # ONE deadline over the whole refresh. Every await inside is either
        # already bounded or an executor call that can block on DNS or a wedged
        # TLS handshake; without a ceiling on the total, one such hang means
        # this coroutine never reaches its `finally`, `_usage_fetching` stays
        # True for the process lifetime, and every later refresh returns at the
        # guard above — so the cache is never populated and the dashboard's
        # credit pill shows "Checking usage..." forever with nothing logged.
        # A timeout here lands in the handler below, which keeps the last good
        # value or marks usage unavailable, so the pill always resolves.
        return await asyncio.wait_for(_refresh(), timeout=_USAGE_FETCH_DEADLINE_SECS)
    except asyncio.TimeoutError:
        # Transient hang — keep the last good value (stale) for the same account
        # instead of blanking. The account is re-resolved here too: the hang may
        # have outlasted a profile switch. The wedged scrape is reaped FIRST:
        # the adjacent whoami is another kiro-cli subprocess of up to its own
        # timeout, and a child left running through it would outlive the
        # deadline this handler exists to enforce, holding the agent lock and
        # its sandbox scope for that whole time.
        logger.debug("Background usage fetch timed out")
        await _reap_scrape_proc()
        if scrape_attempted:
            _record_scrape_outcome(False)
        _cache_transient_failure(await _adjacent_identity())
        return _parked_by_this_attempt() if scrape_attempted else None
    except Exception:
        logger.debug("Background usage fetch failed", exc_info=True)
        await _reap_scrape_proc()
        if scrape_attempted:
            _record_scrape_outcome(False)
        _cache_transient_failure(await _adjacent_identity())
        return _parked_by_this_attempt() if scrape_attempted else None
    finally:
        # Always reap the subprocess on any exit path (the handlers above did
        # it before their whoami; this covers the success path's early returns
        # and task cancellation, a BaseException the excepts above don't catch)
        # so a leaked kiro-cli process can't hold the agent lock or keep a
        # sandbox scope alive.
        _usage_fetching = False
        await _reap_scrape_proc()
        if sandbox_cleanup:
            try:
                os.remove(sandbox_cleanup)
            except OSError:
                pass


async def api_sessions_usage(request: web.Request) -> web.Response:
    """GET /api/sessions/usage — cached kiro credit usage (background refresh)."""
    # Same browser-storm guard as api_models: the /usage scrape shells out to
    # `kiro-cli chat --no-interactive ... /usage`, which auto-opens a browser
    # login while signed out. This endpoint is polled every 30s by the top-bar
    # credit pill, so an unauthenticated gateway spawned a browser every 30s.
    blocked = await reject_if_kiro_unverified(request)
    if blocked is not None:
        return blocked
    now = time.time()
    if now - _usage_cache_ts > _USAGE_REFRESH_SECS:
        # Timed refresh only — deliberately not triggered by the kiro-cli auth
        # store changing on disk. That store is shared: `data.sqlite3` holds
        # `conversations`, `history` and `state` alongside `auth_kv`, so ordinary
        # chat traffic rewrites it roughly every 30 seconds; a disk-change trigger
        # would fire on nearly every poll, and each fire can reach the `/usage`
        # text scrape, a minute-scale kiro-cli subprocess. A faster readout is
        # not worth that churn; a profile switch is picked up on the next interval.
        state: DashboardState = request.app["state"]
        task = asyncio.create_task(_fetch_usage_bg())
        state._background_tasks.add(task)
        task.add_done_callback(state._background_tasks.discard)
    return web.json_response({"usage": _usage_cache})


async def api_sessions_usage_refresh(request: web.Request) -> web.Response:
    """POST /api/sessions/usage/refresh — refresh the credit reading now.

    The account modal's Refresh button. Runs one refresh (the same API-first,
    scrape-second sequence the timer runs) and answers with the cache the GET
    serves -- ``{"usage": <payload>}`` -- so the frontend parses both the same
    way.

    Guards, in order, and why each is here:

    * :func:`reject_if_kiro_unverified` -- the scrape shells out to ``kiro-cli
      chat``, which opens a browser login while signed out (same as the GET).
    * Single flight -- while a refresh is in progress (the timer's or another
      click's) a second POST is refused with 409 ``refresh_in_flight`` rather
      than started: a refresh is a whoami + scrape subprocess pair that can
      take up to :data:`_USAGE_FETCH_DEADLINE_SECS`, and two of them for one
      reading is waste. Checked synchronously against ``_usage_fetching`` with
      no await in between, and :func:`_fetch_usage_bg` sets the flag before
      its first await, so two requests on the same loop cannot both pass.
    * Back-off -- reported, not pre-checked. The refresh runs the free API
      attempt regardless of the scrape's state; only when the API yields no
      plan AND the scrape is parked -- parked before this refresh, or parked
      BY this refresh's own failed attempt (the third miss) -- does the
      response carry ``skipped: "scrape_parked"`` with ``retry_after``
      (seconds until the park lifts), telling the UI no new reading was
      fetched and none will be until then: the ``usage`` it received is a
      same-identity prior reading dimmed ``stale``, or an unavailable marker
      (see :func:`_cache_without_scrape` / :func:`_cache_transient_failure`).
    """
    blocked = await reject_if_kiro_unverified(request)
    if blocked is not None:
        return blocked
    if _usage_fetching:
        return web.json_response(
            {"error": "A refresh is already running", "code": "refresh_in_flight"},
            status=409,
        )
    outcome = await _fetch_usage_bg()
    if outcome == _SKIPPED_SCRAPE_PARKED:
        parked_for = max(1, int(_usage_scrape_backoff_until - time.monotonic() + 0.999))
        return web.json_response(
            {
                "usage": _usage_cache,
                "skipped": _SKIPPED_SCRAPE_PARKED,
                "retry_after": parked_for,
            }
        )
    return web.json_response({"usage": _usage_cache})


def _open_slot_transcript_keys(state: DashboardState) -> set[str]:
    """Every transcript key and filename stem a live slot could be reading.

    A session in this set is reachable as an open tab. That single fact drives
    two callers: the bulk delete must not touch it, and the Older-sessions list
    must not repeat it (that list is the complement of the open tabs above it).

    Resolved FROM the slot, never derived from its name. A channel-born slot's
    transcript is its ``linked_session_key`` (``slack:<ts>``), so a hand-built
    ``dashboard:<slot>`` name would miss every channel tab. ``list_sessions``
    reports filename STEMS, so each candidate contributes its key and every
    stem it can occupy. The latter includes the pre-migration bare Slack stem;
    protecting only ``slack_<ts>`` would let Clear All unlink ``<ts>`` while
    that same open tab was still reading it.

    Both candidates are included because a slot's write target and its DISPLAY
    source can differ: a channel tab the dashboard could not bind runs under
    ``dashboard:<stem>`` while the conversation on screen lives in the channel
    transcript. Choosing between them would make the answer depend on provenance
    resolving correctly, and provenance is exactly what a legacy transcript
    cannot supply. Both names belong to the SAME slot, so covering both is safe
    in either direction — it can only protect, or hide, a transcript that one
    slot could itself be showing.
    """
    from kiro_crew.dashboard.chat_utils import slot_history_key, slot_transcript_key

    keys: set[str] = set()
    # Snapshot the values: a concurrent turn can add or remove a slot while this
    # iterates, and a dict mutated mid-iteration raises.
    for slot in list(state._slots.values()):
        for candidate in (slot_history_key(slot), slot_transcript_key(slot.key)):
            keys.add(candidate)
            keys.update(transcript_stems(candidate))
    return keys


#: Session namespaces whose transcripts are a machine run rather than a conversation
#: anyone addressed. This membership is a PRESENTATION judgement for one pane, not a
#: shared roster: two other modules carry similar-looking tuples that answer different
#: questions, and none of the three agree.
#:
#: * ``handlers/_shared.py`` — colon-only, wf-family only, to dispatch a memory-mode
#:   lookup. Includes ``wf-scope``.
#: * ``handlers/cron.py`` — colon-only, wf-family plus ``subagent``, to answer whether a
#:   session is live and shared. Omits ``wf-scope``; suspected pre-existing gap in that
#:   predicate rather than a deliberate exclusion.
#: * this one — matches PERSISTED folded keys via ``_in_namespace``, adds ``secretary``
#:   and ``channel``, and deliberately excludes ``cron``, ``side`` and ``taskrunner``
#:   because each of those holds conversations a reader started.
#:
#: So do not hoist the three into one constant: it would force a shared meaning none of
#: them has, and the next namespace would have to be correct for all three at once.
#:
#: It is spelled literally rather than derived from
#: ``messaging.link._TELEMETRY_LOCAL_PREFIXES`` because that registry exists to bound
#: telemetry label cardinality, and ``wf-unpooled``, ``wf-worker`` and ``wf-scope`` are
#: all live session keys absent from it — a derived filter cannot see them.
#:
#: Absent on purpose:
#:
#: * ``dashboard`` and the channel namespaces (``slack``, ``discord``, …) — the
#:   reader's own conversations.
#: * ``cron`` — a job without ``hide_in_chat`` backs a real chat slot, and the key
#:   does not record which kind wrote it.
#: * ``side`` — the slot's own side panel, which the reader typed into; ``sel.py``
#:   attributes ``side:`` to the ``dashboard`` surface for that reason.
#: * ``taskrunner`` — ``POST /api/taskrunner/{id}/to-chat`` opens a real chat slot on
#:   ``taskrunner:<task_id>:chat:<token>`` and titles it ``Plan: <task_id>``, so the
#:   namespace holds conversations as well as runs and the key cannot reliably tell
#:   them apart. Listing a plain ``taskrunner_`` run is the declared residual, and it
#:   is the safe one: showing a machine row costs less than hiding a conversation.
#:
#: Anything not named here stays listed, which is the direction every residual in
#: this filter points.
_MACHINE_NAMESPACES: tuple[str, ...] = (
    # fmt: off
    "subagent", "secretary", "channel",
    "wf", "wf-pool", "wf-unpooled", "wf-worker", "wf-author", "wf-scope"
    # fmt: on
)


def _is_machine_only_session(key: str) -> bool:
    """True when *key* sits in a namespace no user ever addressed directly.

    Matched through ``_in_namespace`` because this reads a PERSISTED name:
    ``history._safe_key`` folds ``subagent:<id>`` to the stem ``subagent_<id>``, so
    the colon spelling every other subagent guard in the tree uses can never match
    here. That fold is the whole defect this filter repairs.
    """
    return any(_in_namespace(key, ns) for ns in _MACHINE_NAMESPACES)


async def api_sessions(request: web.Request) -> web.Response:
    """GET /api/sessions — list conversation session files.

    Query params:
      - ``limit``: max sessions to return (default 50, max 200)
      - ``offset``: skip first N sessions (default 0)
      - ``preview``: when truthy, attach a redacted last-message ``preview``
        to each returned session (bounded tail read; page-scoped so the
        default list stays a cheap metadata scan)
      - ``exclude_open``: when truthy, drop sessions a live slot already holds
        open. Opt-in, not the default: the full inventory is what the memory
        "Consolidate all" action and the command palette's recents read, and
        both would silently skip the user's active conversations if this
        endpoint decided on their behalf. Only the caller rendering the
        complement of the open tabs asks for it.
      - ``user_only``: when truthy, drop sessions whose key is in a machine-only
        namespace (see :data:`_MACHINE_NAMESPACES`). Opt-in for the same reason as
        ``exclude_open``: a subagent or workflow transcript is still a session the
        memory-consolidation and recents callers must see. Only the sidebar's
        Older-sessions pane asks, because it is the surface that presents these
        rows as a LIST OF CONVERSATIONS — and a machine transcript carries no
        title, so it renders its own storage key as the row label there.

    Returns ``{sessions, total, has_more}`` for pagination.
    """
    state: DashboardState = request.app["state"]
    if not state.conversation_log:
        return web.json_response({"sessions": [], "total": 0, "has_more": False})
    try:
        limit = min(int(request.query.get("limit", "50")), 200)
    except (TypeError, ValueError):
        limit = 50
    try:
        offset = int(request.query.get("offset", "0"))
    except (TypeError, ValueError):
        offset = 0
    want_preview = (request.query.get("preview") or "").lower() in ("1", "true", "yes")
    exclude_open = (request.query.get("exclude_open") or "").lower() in ("1", "true", "yes")
    user_only = (request.query.get("user_only") or "").lower() in ("1", "true", "yes")
    # list_sessions() globs, stats, and reads the first line of EVERY session file
    # in the history dir — O(all sessions). At 2000 sessions, that's ~200 ms of
    # blocking IO (measured: 208 ms / 2000 files on a dev host). Running that on
    # the event loop freezes chat, heartbeat, and every other coroutine for the
    # full duration. Offload to a worker thread.
    all_sessions = await asyncio.to_thread(state.conversation_log.list_sessions)
    if exclude_open:
        open_keys = _open_slot_transcript_keys(state)
        # Fold through ``_canonical_key`` as well: ``list_sessions`` deduplicates
        # by canonical name but reports the RAW stem of whichever file won on
        # mtime, so a resume round-trip's ``dashboard_dashboard_<name>`` file
        # reaches here under a name no slot ever produces. Without the fold that
        # session is listed as a second, separate conversation.
        canon = state.conversation_log._canonical_key
        all_sessions = [
            s
            for s in all_sessions
            if s.get("key", "") not in open_keys and canon(s.get("key", "")) not in open_keys
        ]
    if user_only:
        all_sessions = [s for s in all_sessions if not _is_machine_only_session(s.get("key", ""))]
    # Count AFTER the exclusion so the page, ``total`` and ``has_more`` describe
    # one list. The client advances its offset by the number of rows it received,
    # so filtering on its side instead would skip or repeat rows across pages.
    total = len(all_sessions)
    page = all_sessions[offset : offset + limit]
    if want_preview:
        log = state.conversation_log

        def _attach_previews(sessions: list[dict]) -> None:
            def _sanitize(text: str) -> str:
                # Injected so redaction runs BEFORE the preview's length cap:
                # a credential split by truncation leaves a partial token the
                # patterns cannot match, letting its raw prefix through.
                text, _ = _h.redact_exfiltration_urls(text)
                text, _ = _h.redact_credentials(text)
                return text

            for s in sessions:
                preview = log.last_message_preview(s.get("key", ""), sanitize=_sanitize)
                if preview:
                    s["preview"] = preview

        # Tail reads are sync file IO — keep them off the event loop.
        await asyncio.get_running_loop().run_in_executor(None, _attach_previews, page)
    return web.json_response(
        {
            "sessions": page,
            "total": total,
            "has_more": offset + limit < total,
        }
    )


_SUMMARIZE_MAX_SESSIONS = 8  # bound cost/latency: only the top-N get an LLM pass
_SUMMARIZE_MODEL = "auto"  # inherit the governed default; a hardcoded id 400s where unavailable
_SUMMARIZE_MSG_LIMIT = 12  # messages fed to the summarizer per session
_SUMMARIZE_TIMEOUT_SECS = (
    30  # per-session deadline so one stalled prompt can't pin the shared _bg session
)
_SUMMARIZE_PROMPT = (
    "Summarize the following conversation in ONE terse line (max 18 words), "
    "describing what the user and assistant are working on. No preamble, no "
    "quotes, no trailing period. If the topic is unclear, reply exactly SKIP.\n\n"
    "===== CONVERSATION =====\n"
    "{transcript}\n"
    "===== END ====="
)


def _build_summary_prompt(messages: list[dict]) -> str | None:
    """Build a one-line-summary prompt from a session's recent messages."""
    lines: list[str] = []
    for m in messages[:_SUMMARIZE_MSG_LIMIT]:
        role = m.get("role", "")
        content = " ".join(str(m.get("content", "")).split())
        if role in ("user", "assistant") and content:
            lines.append(f"{role}: {content[:300]}")
    if not lines:
        return None
    return _SUMMARIZE_PROMPT.format(transcript="\n".join(lines))


async def _summarize_one(state: DashboardState, key: str) -> str:
    """Generate a one-line LLM summary for a single session. "" on any failure.

    Mirrors dashboard.chat_title._generate_title_via_kiro: uses an ephemeral
    background session on the cheap/fast model and destroys it in a finally.
    Best-effort — every failure path returns "" so the caller falls back to the
    session's stored title.
    """
    log = state.conversation_log
    if not log:
        return ""
    loop = asyncio.get_running_loop()
    # get_metadata + recent do synchronous full-file reads (read_text + per-line
    # JSON parse, up to 2MB). Offload to the executor so a batch of large session
    # files never freezes the gateway event loop — mirrors api_sessions above.
    meta = await loop.run_in_executor(None, log.get_metadata, key)
    # Defense in depth: never summarize an incognito/temporary session even if a
    # caller somehow passes its key.
    if is_incognito_transcript(meta.get("memory_mode")):
        return ""
    # Cache: a summary persisted in a sidecar file is reusable as long as the
    # session file hasn't changed since it was generated. session_mtime advances
    # only on real message appends (preserved across metadata writes), so it is a
    # cheap, exact staleness signal — a repeat list_sessions(summarize=true) for
    # an unchanged session pays zero LLM cost. The cache lives in a sidecar
    # (never the session JSONL) so summarizing an *active* session never rewrites
    # its log and cannot lose a concurrently-appended message.
    sig = await loop.run_in_executor(None, log.session_mtime, key)
    # Captured WITH the signature: a rewrite during the model call below
    # preserves the mtime while advancing this counter, and stamping the new
    # content identity onto the older summary would bless it as fresh.
    generation = await loop.run_in_executor(None, log.rotation_generation, key)
    cached = await loop.run_in_executor(None, log.get_cached_summary, key)
    if cached:
        return str(cached)
    messages = await loop.run_in_executor(
        None,
        functools.partial(
            log.recent, key, max_messages=_SUMMARIZE_MSG_LIMIT, roles={"user", "assistant"}
        ),
    )
    prompt = _build_summary_prompt(messages)
    if not prompt:
        return ""
    try:
        text = await run_bg_oneliner(
            state.sessions, prompt, model=_SUMMARIZE_MODEL, timeout=_SUMMARIZE_TIMEOUT_SECS
        )
    except Exception:
        logger.debug("Session summary generation failed for %s", key, exc_info=True)
        return ""
    summary = text.strip().strip('"').strip("'").strip(".")
    if not summary or summary.upper() == "SKIP":
        return ""
    summary, _ = redact_exfiltration_urls(summary)
    summary, _ = redact_credentials(summary)
    summary = summary[:200]
    # Persist for reuse in a sidecar cache (best-effort; keyed by the mtime we
    # observed above so a concurrent append invalidates it on the next call).
    # Writing the sidecar never touches the session JSONL, so it cannot race a
    # concurrent append or reorder list_sessions.
    if sig is not None:
        try:
            await loop.run_in_executor(
                None,
                functools.partial(log.set_cached_summary, key, summary, sig, generation),
            )
        except Exception:
            logger.debug("Failed to persist summary cache for %s", key, exc_info=True)
    return summary


async def api_sessions_summarize(request: web.Request) -> web.Response:
    """POST /api/sessions/summarize — one-line LLM summaries for given sessions.

    Body: ``{"keys": ["<session_key>", ...]}``. Only the first
    ``_SUMMARIZE_MAX_SESSIONS`` keys are summarized (cost/latency bound); the
    rest are silently skipped and the caller falls back to their titles.
    Returns ``{"summaries": {key: one_line_summary}}`` — keys that produced no
    usable summary are omitted. Best-effort: a per-session failure never fails
    the whole request.
    """
    state: DashboardState = request.app["state"]
    if not state.conversation_log:
        return web.json_response({"summaries": {}})
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON body"}, status=400)
    keys = body.get("keys") if isinstance(body, dict) else None
    if not isinstance(keys, list):
        return web.json_response({"error": "keys must be a list"}, status=400)
    # Dedupe while preserving order, drop non-strings, then bound the count.
    seen: set[str] = set()
    ordered: list[str] = []
    for k in keys:
        if isinstance(k, str) and k and k not in seen:
            seen.add(k)
            ordered.append(k)
    ordered = ordered[:_SUMMARIZE_MAX_SESSIONS]

    summaries: dict[str, str] = {}
    for key in ordered:
        if not state.conversation_log.has_log(key):
            continue
        summary = await _summarize_one(state, key)
        if summary:
            summaries[key] = summary
    return web.json_response({"summaries": summaries})


async def api_sessions_search(request: web.Request) -> web.Response:
    """GET /api/sessions/search — content search over session JSONL files.

    Query params:
      - ``q``: search string (min 2 chars; empty returns no results)
      - ``limit``: max results (default 50, max 200)

    Returns ``{sessions}`` — same metadata shape as:func:`api_sessions`.
    Session titles may be LLM-generated and are redacted before return.
    """
    state: DashboardState = request.app["state"]
    if not state.conversation_log:
        return web.json_response({"sessions": []})
    q = sanitize_string(request.query.get("q", "")).strip()[:256]
    if len(q) < SEARCH_MIN_CHARS:
        return web.json_response({"sessions": []})
    try:
        limit = max(1, min(int(request.query.get("limit", "50")), 200))
    except (TypeError, ValueError):
        limit = 50
    sessions = await asyncio.get_running_loop().run_in_executor(
        None, state.conversation_log.search_sessions, q, limit
    )
    for s in sessions:
        for field in SESSION_SEARCH_TEXT_FIELDS:
            value = s.get(field)
            if value:
                s[field] = redact(value)
    return web.json_response({"sessions": sessions})


async def api_session_detail(request: web.Request) -> web.Response:
    """GET /api/sessions/{key} — return messages for a session."""
    state: DashboardState = request.app["state"]
    key = request.match_info["key"]
    if not state.conversation_log:
        return web.json_response([])
    # read_messages() opens and parses the transcript on a cache miss, which for
    # the multi-MB sessions a long-lived store accumulates is 100-300 ms of
    # blocking file IO — on the event loop, stalling every other request. Off the
    # loop, like every other conversation_log read in this module (list_sessions,
    # get_metadata, session_mtime, search_sessions, delete_session).
    messages = await asyncio.to_thread(state.conversation_log.read_messages, key)
    return web.json_response(messages)


async def _owner_keys_bound_to_transcript(crons: Any, keys: Collection[str]) -> dict[str, set[str]]:
    """Per history key, the STORE-side owner keys whose transcript is that row.

    Returns ``{history key: exact owner keys}``. Raises ``CronStoreBusy`` (past the
    shared bounded backoff) and ``CronStoreUnreadable`` — the store's own two ways
    of saying it cannot be seen — so the caller refuses the delete with a distinct
    code for each. Any other failure is logged and answers ``{}``: unknown, proceed
    loudly.

    The transcript's ``linked_session_key`` is the funnel's primary source for a
    channel session's exact owner key, but it cannot be the only one. Two states
    have a channel-owned job and NO such metadata, and neither is an error:

    * the ORDERING WINDOW — ``cron_add`` stamps the channel key on the job in one
      transaction while the slot save publishes ``linked_session_key`` in
      another, so a delete landing between them reads absent metadata over a job
      that is already owned.
    * a LEGACY transcript written before that metadata existed at all.

    In both, "absent" means "not recorded here", not "no owner to lose" — and
    absent is also the ordinary state of every dashboard session, so refusing the
    delete is not available as an answer. So the binding is resolved from the
    OTHER side: the store knows each job's exact owner key, and this row's own
    transcript stem says which owner keys name THIS conversation. A job whose
    owner folds onto that stem owns this row, and its exact key -- the spelling
    the release needs and the stem cannot reconstruct (``_safe_key`` maps every
    ``:`` to ``_``) -- is right there on the job.

    Matched on the row's OWN stem, never a stripped one. An owner matches when
    this row's stem is one of the filenames THAT owner logs under -- its canonical
    stem or, for a Slack thread predating the ``slack:<ts>`` key, the bare
    ``thread_ts`` stem ``ConversationLog._path`` still falls back to (hence
    ``transcript_stems``: the canonical stem alone misses a legacy transcript's
    owner silently, and the delete then strands it). What this deliberately does
    NOT do is strip a ``dashboard_`` prefix off the ROW to reach ``slack_<ts>``,
    even though ``dashboard_slack_<ts>`` is a real shape for a pre-migration
    leftover tab whose conversation IS ``slack:<ts>``: the stripped form names a
    DIFFERENT session, derived from a string a client chose (``POST
    /api/chat/slots`` takes the slot name), so honouring it releases a LIVE
    channel session's jobs when an unrelated dashboard row is deleted for being
    spelled like it.

    No marker on the transcript can license that fold. ``linked_session_key`` and
    ``channel_origin`` both live on the metadata line of an AGENT-WRITABLE file,
    so an agent that can create the lookalike transcript can write the marker onto
    it — a marker-gated fold is as reachable as an ungated one, one step longer.
    Nor is there a server-side record to consult instead: the absent-metadata case
    is precisely the SLOTLESS one, so there is no live slot holding provenance.

    Hence the rule this function fails closed on: provenance for a destructive
    CROSS-SESSION action must come from outside agent-writable storage, and where
    none exists the action is not inferred. The cost is a genuinely channel-born
    leftover keeping a job owned by a session that is gone — bounded, visible
    (:func:`_warn_unprovable_channel_binding` names the owner, the candidate ids
    and the recovery command) and RECOVERABLE by id with ``kirocrew cron adopt
    <id> --release``. A forged release is none of those things.

    A PRESENT ``linked_session_key`` is a different question and is still
    honoured: :func:`_linked_session_key_for_history_key` asks whether the claim
    the row already carries is about ITSELF, comparing the agent-written value
    against the route-derived history key. That comparison can only NARROW what a
    row reaches, never widen it to a session the row does not name.

    Swept for EVERY delete rather than only when metadata is absent. Knowing it
    is absent means having read it, and that read lives inside the same lock hold
    as the unlink (see :func:`_delete_history_session`) precisely so no
    writer can land a key between them; splitting the hold to decide whether to
    sweep would reopen that window to save one cache-warm read. When metadata IS
    present the sweep simply agrees with it, and a duplicate candidate costs
    nothing: ``release_jobs_owned_by`` re-resolves ownership and liveness inside
    the store lock, so nothing here can release a job the store disagrees about.

    Reads through ``owner_keys_async`` — a STRICT locked scan that reloads through
    ``_sync_for_write`` and RAISES on either store failure. Neither cache-shaped
    read can be used here, and the distinction is the whole point of the sweep:
    ``list_jobs`` lags a cross-process write by up to one timer poll, and
    ``list_jobs_async`` locks but DEGRADES — it swallows ``CronStoreBusy`` to
    return the cache, and syncs through ``_sync()``, whose ``_load`` flattens an
    unreadable store to an empty job list. Both answer "no owners" for a store
    that could not be read, which is the one answer this caller must not accept:
    it is about to destroy the last record of the binding.

    Only the STORE's own two ways of saying "you cannot see me" fail CLOSED, and
    each PROPAGATES so the caller can name the cause: sustained ``CronStoreBusy``
    (retried on the shared backoff first) and ``CronStoreUnreadable``. In those the
    job set is unknown rather than empty, so the caller refuses instead of
    unlinking the last record of a binding it could not check, and answers with a
    code and remedy specific to the cause — a busy store wants a retry, an
    unreadable one wants the file repaired, and the by-id CLI cannot help with
    either. An UNEXPECTED exception is different in kind: not the store's verdict
    but a defect or an environment failure this sweep never named (a mock in a
    test, an ``OSError`` off a network mount), on a read that fronts EVERY history
    delete and the bulk clear. Refusing on it would let one cron-read fault block
    all history deletion, so the unknown case proceeds LOUDLY: the exception, the
    session keys and the by-id recovery command are logged at WARNING, no owner is
    released on the strength of a read that did not happen, and the delete goes
    ahead. That is the strand-and-recover posture
    :func:`_warn_unprovable_channel_binding` already takes — a job left owned by a
    departed session is bounded, visible and recoverable by id, and the
    alternative here is unbounded. An object with no ``owner_keys_async`` is a
    different case entirely and is quiet: nothing to sweep, nothing withheld.

    The stranded-binding warning is emitted only where the row's name suggests a
    binding this refuses to infer, which is rare — a leftover-shaped row with a
    matching owner in the store — so the ordinary delete pays nothing for it.
    """
    wanted = [k for k in keys if k]
    reader = getattr(crons, "owner_keys_async", None)
    if crons is None or not wanted or reader is None:
        return {}
    owners: set[str] = set()
    for attempt in range(_CRON_RELEASE_ATTEMPTS):
        try:
            owners = {str(owner) for owner in await reader() if owner}
        except CronStoreBusy:
            if attempt + 1 < _CRON_RELEASE_ATTEMPTS:
                await asyncio.sleep(_CRON_RELEASE_BACKOFF_SECS * (attempt + 1))
                continue
            logger.warning("History delete: cron store busy, owner sweep not performed")
            raise
        except CronStoreUnreadable as exc:
            logger.warning("History delete: cron owner sweep not performed: %s", exc)
            raise
        except Exception as exc:
            # Unknown, proceed loudly -- see the docstring. Not the store's own
            # verdict, and this sweep fronts every history delete, so the fault
            # is named with its recovery rather than allowed to block them all.
            logger.warning(
                "History delete: cron owner sweep failed unexpectedly for %s (%s: %s); cron "
                "ownership is unknown and the delete proceeds, so a job owned by this session "
                "may be left owned by a session that no longer exists — release it by id with "
                "`kirocrew cron adopt <id> --release` (`kirocrew cron list` shows the "
                "owners)",
                ", ".join(wanted),
                type(exc).__name__,
                exc,
                exc_info=True,
            )
            return {}
        break
    found: dict[str, set[str]] = {}
    for history_key in wanted:
        stem = transcript_stem(history_key)
        matched = {owner for owner in owners if stem in transcript_stems(owner)}
        if matched:
            found[history_key] = matched
        # Not folded in: the stripped stem names ANOTHER session, on the strength
        # of this row's name alone. Say so instead, so the strand is recoverable.
        stripped = _orphan_target_stem(stem)
        if stripped:
            unprovable = {owner for owner in owners if stripped in transcript_stems(owner)}
            if unprovable:
                _warn_unprovable_channel_binding(crons, unprovable, history_key)
    return found


async def _owner_keys_after_unlink(crons: Any, keys: Collection[str]) -> dict[str, set[str]]:
    """The post-unlink owner re-sweep: the pre-sweep's read, the opposite failure direction.

    Both permanent-delete paths take :func:`_owner_keys_bound_to_transcript` twice:
    once BEFORE the unlink, when a store that cannot be seen refuses the delete,
    and once AFTER it has committed, to catch the one arrival the first pass
    cannot see. The pre-sweep knows only the jobs that existed when it scanned,
    and a channel ``cron_add`` commits under its own store lock: a job born
    between the scan and the unlink is stamped with a key the delete retires, and
    the store's add-time guard does not cover it (it resolves a ``cron:`` parent
    id; a channel key names no job). The single delete explains the window and
    why the two locks are not spanned; the bulk clear pays ONE such read for the
    whole batch, so *keys* is a collection.

    Here the rows are already gone, so refusing is not available. The store's own
    two failures -- busy past the shared backoff, unreadable -- are therefore
    named LOUDLY instead: the deleted rows, the cause and the by-id recovery
    (``cron adopt <id> --release``), at WARNING, and the caller releases what the
    pre-sweep found. That is the stance the sweep itself takes on an unexpected
    error, and the strand is bounded and recoverable by id. An add landing after
    this pass is the ordinary stranded case the release already reports.
    """
    try:
        return await _owner_keys_bound_to_transcript(crons, keys)
    except (CronStoreBusy, CronStoreUnreadable) as exc:
        logger.warning(
            "History delete: %s deleted, but the post-delete owner scan could not read the "
            "cron store (%s), so a job created during the delete may still be owned "
            "by a deleted session — release it by id with `kirocrew cron adopt <id> --release` "
            "(`kirocrew cron list` shows the owners)",
            ", ".join(key for key in keys if key),
            exc,
        )
        return {}


def _stranded_job_ids(crons: Any, owner_keys: set[str]) -> list[str]:
    """Ids of jobs owned by *owner_keys*, for a warning to NAME. Never decides.

    Cache-only (``list_jobs``) and never raises ``CronStoreBusy``, so this
    diagnostic cannot fail for the same reason a release just did. Its staleness
    is acceptable precisely because nothing acts on it — see
    ``CronService.release_jobs_owned_by``, which re-resolves ownership inside the
    store lock. Answers ``[]`` rather than propagating, since a warning that
    raises replaces the message an operator needs with a traceback.
    """
    try:
        return sorted(
            job.id
            for job in crons.list_jobs(include_disabled=True)
            if getattr(job, "session_key", "")
            and any(
                cron_owner_matches(getattr(job, "session_key", ""), target) for target in owner_keys
            )
        )
    except Exception:
        logger.warning("History delete: could not enumerate stranded cron jobs", exc_info=True)
        return []


def _warn_unprovable_channel_binding(crons: Any, owner_keys: set[str], history_key: str) -> None:
    """Name a binding the sweep declined to infer, and how to finish it by hand.

    Fires when the deleted row's name STRIPS to a channel stem that some job in
    the store is actually owned under. Either the row is that channel
    conversation's leftover tab file and the job is now genuinely orphaned, or the
    row is an unrelated dashboard session that merely shares the spelling — and
    nothing durable distinguishes them, because every marker that could lives in
    the same agent-writable file as the name (see
    :func:`_owner_keys_bound_to_transcript`).

    Refusing to guess is the safe half; saying nothing would not be. An
    unrecoverable strand is one nobody was told about, so this logs the owner, the
    candidate ids and the one command that completes the release, turning a silent
    conservatism into a documented manual step.
    """
    stranded = _stranded_job_ids(crons, owner_keys)
    logger.warning(
        "History delete: %s strips to a channel stem owning cron job(s), but nothing outside the "
        "agent-writable transcript proves this row IS that conversation, so ownership was NOT "
        "released for owner(s) %s; job(s) %s may now be owned by a session that no longer exists "
        "— release them with `kirocrew cron adopt <id> --release`",
        history_key,
        ", ".join(sorted(owner_keys)),
        ", ".join(stranded) if stranded else "unknown (store not readable from cache)",
    )


#: Machine-readable codes for a delete the cron-ownership check REFUSES. Each names
#: a distinct cause with a distinct remedy, and the dashboard client keys its
#: localized message on the code rather than rendering the English ``error``.
CRON_STORE_UNREADABLE_CODE = "cron_store_unreadable"
CRON_STORE_BUSY_CODE = "cron_store_busy"
CRON_OWNERSHIP_UNKNOWN_CODE = "cron_ownership_unknown"

#: Reported for a row whose ledger exclusions could not be written. The transcript is
#: left alone: deleting it would leave its work state readable by the next session
#: in the same slot, which is not recoverable by the person it happens to.
LEDGER_EXCLUSION_UNWRITABLE_CODE = "ledger_exclusion_unwritable"


class _OwnerKeyUnreadable(Exception):
    """A delete REFUSED because the exact cron owner key of the row cannot be read.

    Raised by :func:`_delete_history_session` with the row intact. It is
    an exception rather than a ``None`` result so the bulk clear can tell this
    refusal apart from the ``None`` a pinned row answers under ``skip_pinned``,
    and report it by id with :data:`CRON_OWNERSHIP_UNKNOWN_CODE`.
    """


def _cron_store_refusal(exc: Exception, **counts: Any) -> web.Response:
    """409 for a delete whose owner sweep could not SEE the cron store, by cause.

    ``CronStoreUnreadable`` carries the store path and the repair route in its
    own message (``CronService._unreadable_error`` is the one wording every
    surface reads verbatim), so that text is surfaced as-is. The ``kirocrew
    cron adopt`` CLI is deliberately NOT named here: it reads the same file,
    so on an unreadable store it cannot work either. ``CronStoreBusy`` is
    contention and wants nothing but a retry. Neither is a filesystem failure of
    the delete, so neither is ``200 {ok: false}`` -- a 409 is what the dashboard
    client rejects on, which is what keeps the refused row in the sidebar.
    ``counts`` lets the bulk clear carry its ``cleared``/``skipped``/``failed``
    tallies beside the refusal, for callers that read the body.
    """
    if isinstance(exc, CronStoreUnreadable):
        code = CRON_STORE_UNREADABLE_CODE
        error = (
            "the cron store could not be read, so cron ownership could not be checked "
            f"and nothing was deleted: {exc}"
        )
    else:
        code = CRON_STORE_BUSY_CODE
        error = (
            "the cron store is busy, so cron ownership could not be checked and nothing "
            "was deleted; retry in a moment."
        )
    if not counts:
        return web.json_response({"ok": False, "error": error, "code": code}, status=409)
    return web.json_response(
        {
            "ok": False,
            "error": error,
            "code": code,
            "cleared": counts.get("cleared", 0),
            "skipped": counts.get("skipped", 0),
            "failed": counts.get("failed", 0),
        },
        status=409,
    )


def _cron_ownership_unknown_refusal(crons: Any, owner_keys: Collection[str]) -> web.Response:
    """409 for a row whose transcript metadata could not be read.

    Here the remedy IS the CLI plus transcript repair: the store is readable,
    the row is not, so the operator releases candidate jobs by id, repairs the
    row's metadata, and retries. ``owner_keys`` are the owners the store-side
    sweep bound to this row ahead of the read, and the message names their job
    ids when the cache can list them -- a cache-only listing that decides nothing
    (see :func:`_stranded_job_ids`).
    """
    stranded = _stranded_job_ids(crons, set(owner_keys)) if owner_keys else []
    ids = f" (candidate job id(s): {', '.join(stranded)})" if stranded else ""
    return web.json_response(
        {
            "ok": False,
            "error": (
                "the cron jobs this session owns could not be determined because its "
                "transcript metadata is unreadable, so deleting it would leave them owned "
                "by nobody. Release candidate jobs with `kirocrew cron adopt <id> --release`"
                f"{ids} -- `kirocrew cron list` shows the owners -- repair the "
                "transcript metadata, then delete the session again."
            ),
            "code": CRON_OWNERSHIP_UNKNOWN_CODE,
        },
        status=409,
    )


@dataclass(frozen=True)
class _HistoryDeleteClaim:
    """Slot, manager, and exact cron ownership captured before transcript unlink."""

    registry_key: str | None
    slot: Any | None
    history_key: str | None
    session_key: str | None
    session_generation: int
    path_match_verified: bool
    complete: bool
    slot_task: Any | None = None
    cron_owner_keys: frozenset[str] = frozenset()
    #: Unit ids the locked transaction excluded from this slot's ledger. Recorded there
    #: rather than by the caller because only the resolved claim PROVES which slot owns
    #: this transcript: a provisional claim can name a stacked duplicate's slot, and
    #: excluding on it would empty an unrelated LIVE session's record.
    ledger_excluded_units: frozenset[str] = frozenset()
    #: Whether that transaction TOMBSTONED the slot's legacy pre-projection document --
    #: wrote the committed carry marker itself, rather than finding one already there.
    #: Only the writer may take it back, so this travels with the claim to the rollback.
    ledger_carry_tombstoned: bool = False
    #: The ACP session id behind *session_key*, read before the teardown that
    #: destroys the session it names. This is what identifies the session
    #: LEDGER, whose unit id is the ACP id rather than the slot key, and it is
    #: captured here for the same reason every other field is: after the
    #: teardown there is nothing left to ask.
    acp_session_id: str | None = None


def _history_delete_candidate_keys(key: str) -> tuple[str, ...]:
    """Every exact/legacy spelling that may index *key*'s dashboard slot."""
    stripped = key
    if stripped.startswith("dashboard:"):
        stripped = stripped[len("dashboard:") :]
    while stripped.startswith("dashboard_"):
        stripped = stripped[len("dashboard_") :]
    canonical = canonical_key(key)
    return tuple(
        dict.fromkeys(
            (
                key,
                stripped,
                "dashboard_" + key,
                _normalize_slot_key(key),
                canonical,
                _normalize_slot_key(canonical),
            )
        )
    )


def _capture_history_delete_claim(state: DashboardState, key: str) -> _HistoryDeleteClaim:
    """Capture exact slot identity and monotonic manager generation pre-unlink."""
    deleted_stems = transcript_stems(key)
    deleted_identities = {ConversationLog._canonical_key(stem) for stem in deleted_stems}
    for candidate_key in _history_delete_candidate_keys(key):
        candidate_slot = state._slots.get(candidate_key)
        if candidate_slot is None:
            continue
        try:
            history_key = slot_history_key(candidate_slot)
            candidate_stems = transcript_stems(history_key)
            owned_stems = {ConversationLog._canonical_key(stem) for stem in candidate_stems}
        except Exception:
            return _HistoryDeleteClaim(None, None, None, None, 0, True, False)
        if deleted_identities.isdisjoint(owned_stems):
            continue
        # Only byte-identical filename-stem sets prove ownership without I/O.
        # Canonicalization can collapse distinct stacked dashboard files, and
        # canonical Slack keys may resolve to a pre-migration bare file. Resolve
        # every other alias in the delete worker under the transcript lock.
        path_match_verified = deleted_stems == candidate_stems
        try:
            session_key = effective_session_key(candidate_slot)
            generation = state.sessions.session_generation(session_key)
        except Exception:
            return _HistoryDeleteClaim(
                candidate_key,
                candidate_slot,
                history_key,
                None,
                0,
                path_match_verified,
                False,
            )
        # Best-effort and read LAST: an unreadable ACP id must not downgrade a
        # claim the checks above already completed, because every other cleanup
        # this claim authorizes is more important than collecting one crew log --
        # which the retention sweep collects anyway once the session is closed.
        # Type-checked rather than merely truthy: this value goes on to ADDRESS a
        # directory, so anything that is not a real id must read as absent.
        try:
            resumable = state.sessions.resumable_sid(session_key)
        except Exception:
            logger.debug(
                "History delete: ACP session id unreadable for %s; leaving its crew log "
                "to the retention sweep",
                session_key,
                exc_info=True,
            )
            resumable = None
        acp_session_id = resumable if isinstance(resumable, str) and resumable else None
        return _HistoryDeleteClaim(
            candidate_key,
            candidate_slot,
            history_key,
            session_key,
            generation,
            path_match_verified,
            True,
            getattr(candidate_slot, "task", None),
            acp_session_id=acp_session_id,
        )
    return _HistoryDeleteClaim(None, None, None, None, 0, True, True)


def _resolve_history_delete_claim(
    log: ConversationLog,
    key: str,
    claim: _HistoryDeleteClaim,
) -> _HistoryDeleteClaim:
    """Verify ambiguous transcript ownership in the off-loop delete worker."""
    if claim.slot is None or claim.path_match_verified:
        return claim
    if claim.history_key is None:
        return replace(
            claim,
            registry_key=None,
            slot=None,
            session_key=None,
            path_match_verified=True,
            complete=False,
            # Travels with ``session_key``: a claim that names no session must not
            # still name a session's ledger.
            acp_session_id=None,
        )
    try:
        matches = log._path(key).stem == log._path(claim.history_key).stem
    except Exception:
        logger.warning(
            "History delete: could not resolve pre-unlink transcript ownership for %s",
            key,
            exc_info=True,
        )
        return replace(
            claim,
            registry_key=None,
            slot=None,
            session_key=None,
            path_match_verified=True,
            complete=False,
            # Travels with ``session_key``: a claim that names no session must not
            # still name a session's ledger.
            acp_session_id=None,
        )
    if matches:
        return replace(claim, path_match_verified=True)
    # The alias located a slot, but that slot currently resolves to another
    # transcript (for example canonical and legacy Slack files coexist).
    return _HistoryDeleteClaim(None, None, None, None, 0, True, claim.complete)


def _linkable_stems_for_history_key(key: str) -> set[str]:
    """Every filename stem a row named *key* may legitimately be LINKED as.

    ``slot_history_key`` returns ``linked_session_key`` verbatim as the slot's
    transcript key whenever it is set, so the binding is not a free-form pointer:
    a row carrying a linked key must BE that key's own transcript. That makes the
    expected relationship checkable from the history key alone, which is what
    :func:`_linked_session_key_for_history_key` needs — the history key arrives
    from the route or the session list and is not attacker-writable, while the
    metadata value is.

    Only the row's CANONICAL stem. A leftover-shaped name is deliberately NOT
    stripped to reach the channel session it is spelled like: ``dashboard:slack_
    <ts>`` is not the session ``slack:<ts>``, so admitting the stripped form would
    WIDEN the row to a session it does not name -- and since the transcript store
    is agent-writable, a forged ``linked_session_key`` on a lookalike file would
    then release a LIVE channel session's cron ownership on any history delete.
    That is the same stance :func:`_owner_keys_bound_to_transcript` takes on the
    store side, for the same reason, so the two cannot disagree about what a row
    may reach.

    The bare ``thread_ts`` stem a Slack thread predating the canonical
    ``slack:<ts>`` key still logs under needs no entry here: it is contributed on
    the VALUE side by ``transcript_stems``, where ``ConversationLog._path``'s
    fallback lives.

    A genuinely channel-born leftover therefore keeps its job owned by a session
    that is gone. That cost is bounded, visible -- the sweep's
    :func:`_warn_unprovable_channel_binding` names the owner, the candidate job
    ids and the command -- and RECOVERABLE by id through ``kirocrew cron adopt
    <id> --release``. A forged release is none of those things.

    Scoped to VALIDATING a link the row already carries: the row is CLAIMING a
    binding, and the check asks only whether the claim is about itself, comparing
    the agent-written value against the route-derived key. Restricted to the
    canonical stem, that comparison can only ever NARROW what this row reaches.
    """
    stems = {transcript_stem(key)}
    orphan_of = _orphan_target_stem(transcript_stem(key))
    if orphan_of:
        # A leftover-shaped row is NOT the channel session it is spelled like, so
        # the claim is refused rather than honoured. Warned about, not silent: the
        # sweep's _warn_unprovable_channel_binding names the owner, the candidate
        # job ids and the by-id recovery command.
        logger.warning(
            "History delete: session %s is spelled like a leftover of %r; a link it carries "
            "cannot license releasing that session's jobs, so only its own key is accepted",
            key,
            orphan_of,
        )
    return stems


def _linked_session_key_for_history_key(log: Any, key: str) -> tuple[str, bool]:
    """``(exact session key or "", metadata was READABLE)`` for a history row.

    A channel-born tab persists ``linked_session_key`` in its transcript
    metadata because nothing recreates that binding on restart. Cron jobs the
    session created are stamped with THAT key, not with any ``dashboard:``
    spelling of the transcript name, so it is the only string that can match
    their owner — and after the transcript is unlinked there is nowhere left to
    read it from.

    Reads through ``get_metadata_status``, not ``get_metadata``, and reports the
    readability flag rather than folding it away. The two cases the caller has to
    tell apart both answer ``""``:

    * **ABSENT** — no metadata line, or a metadata line carrying no
      ``linked_session_key`` (readable ``True``). ``{}`` is this case, not a
      quieter malformed one: it is the store's own answer for a row with no
      metadata line and for a first line that is not metadata-typed. There is no
      owner key to lose, so the delete proceeds.
    * **UNREADABLE** — an existing metadata line that could not be read, or that
      came back in a shape this cannot trust: a partial write, a prior ENOSPC,
      on-disk corruption, an agent-edited transcript (readable ``False``,
      including every exception this raises). The row MAY name a cron owner, and
      the unlink destroys the only copy, so deleting here strands that ownership
      with nothing left to notice it — :func:`_warn_stranded_cron_ownership`
      fires only when a release FAILS, never when the key was never read. The
      caller refuses the delete instead.

    A successful read is NOT automatically a trustworthy one. Two payloads are
    reported unreadable rather than folded into the absent case, because each
    would otherwise answer ``""`` (or a key that should never have been honoured)
    and let the delete proceed on a row that may well name an owner:

    * a payload that is not a ``dict`` at all — a bare string, a list, a scalar.
      The store's contract is a mapping, so anything else means the line was
      salvaged, hand-edited or partially written, and its ``linked_session_key``
      is unreadable rather than absent.
    * a ``linked_session_key`` present but not a ``str``. The only writer
      (``slot_projection``) persists a string field, so a mapping or a list here
      is corruption; ``str()``-ing it would mint a key that matches no job, and
      the release would report success having matched nothing.

    A ``linked_session_key`` that does not name THIS row (see
    :func:`_linkable_stems_for_history_key`) is a third case, and deliberately
    not an unreadable one: it answers ``("", True)`` with a warning, so the
    delete proceeds and releases nothing on account of the claim. The transcript
    store is agent-writable, so the value is untrusted INPUT to a privileged
    action -- whatever it names is fed to ``release_jobs_owned_by`` as a retired
    owner, and left unvalidated an agent that can write metadata would point its
    own row at a VICTIM session and have this delete clear the cron ownership of
    the victim. Dropping the claim keeps that closed. Refusing the delete did
    more than that: the mismatch is a property of the file, so a legitimate
    pre-migration leftover tab (``dashboard_slack_<ts>``) was refused identically
    on every retry, and the 409 remedy (release by id, delete again) could never
    clear it.
    """
    try:
        reader = getattr(log, "get_metadata_status", None)
        meta, readable = reader(key) if reader is not None else (log.get_metadata(key), True)
    except Exception:
        logger.warning("History delete: metadata read failed for %s", key, exc_info=True)
        return "", False
    if not readable:
        return "", False
    if not isinstance(meta, dict):
        logger.warning(
            "History delete: metadata for %s is not a mapping (%s); treating it as unreadable",
            key,
            type(meta).__name__,
        )
        return "", False
    linked = meta.get("linked_session_key")
    if linked is not None and not isinstance(linked, str):
        logger.warning(
            "History delete: linked_session_key for %s is a %s, not a string; treating the "
            "metadata as unreadable",
            key,
            type(linked).__name__,
        )
        return "", False
    if linked and not set(transcript_stems(linked)) & _linkable_stems_for_history_key(key):
        # NOT the unreadable case. The row is readable and makes a claim this
        # funnel will not honour -- a stem mismatch is "I will not act on this",
        # not "I cannot read this row". Refusing the delete here made a
        # legitimate pre-migration leftover tab (``dashboard_slack_<ts>``)
        # permanently undeletable: the mismatch is a property of the file, so
        # every retry and the 409 remedy itself read it identically. The claim is
        # dropped instead: the delete proceeds and releases NOTHING on its
        # account, so the other session keeps its jobs; a genuine leftover keeps
        # its strand, which the sweep names with the by-id recovery command.
        logger.warning(
            "History delete: session %s claims linked_session_key %r, which is not the "
            "session this transcript belongs to -- a row can only be linked to the session "
            "whose transcript it IS. The claim is not honoured: the row is deleted without "
            "releasing any cron job owned by %r; a job it really owned is recoverable by "
            "id (the owner sweep warning names the command).",
            key,
            linked,
            linked,
        )
        return "", True
    return (linked or ""), True


def _delete_history_session(
    log: ConversationLog,
    key: str,
    claim: _HistoryDeleteClaim,
    *,
    skip_pinned: bool = False,
    exact_owner_keys: Collection[str] = (),
    # Returns a ``session_ledger.SlotExclusion``: the ids added AND whether the slot
    # was tombstoned. Typed loosely for the same reason the module is imported inside
    # the functions that use it -- this handler does not import the ledger at module
    # scope, and the tests pass a stub in its place.
    exclude: "Callable[[str], Any] | None" = None,
) -> tuple[bool | None, _HistoryDeleteClaim]:
    """Bind slot and cron ownership, then unlink under one transcript lock.

    *exclude* records this slot's ledger units and answers the ids it added. It runs
    INSIDE the lock and only once the claim is RESOLVED, because that is the first point
    where the slot owning this transcript is proved: a provisional claim can name a
    stacked duplicate's slot, and excluding on it empties an unrelated live session's
    record. A refusal propagates, so nothing is unlinked when the exclusion cannot be
    written.
    """
    # ``delete_session`` re-enters one of these locks on the same worker thread.
    # Acquire every stable stem in deterministic order so canonical Slack restore
    # and a legacy-file delete cannot synchronize on different sidecars.
    try:
        with log.locked_stems(transcript_lock_stems(key)):
            resolved_claim = _resolve_history_delete_claim(log, key, claim)
            linked, readable = _linked_session_key_for_history_key(log, key)
            if not readable:
                logger.error(
                    "History delete REFUSED for %s: its metadata is unreadable, so the exact "
                    "cron owner key cannot be read and deleting the transcript would strand "
                    "any cron job it owns under a key no session can ever present again. "
                    "The session was NOT deleted. Release its jobs by hand first with "
                    "`kirocrew cron adopt <id> --release` (`kirocrew cron list` shows the "
                    "owners), repair the transcript metadata, then delete it again.",
                    key,
                )
                raise _OwnerKeyUnreadable(key)
            owner_keys = {owner for owner in exact_owner_keys if owner}
            if (
                resolved_claim.complete
                and resolved_claim.path_match_verified
                and resolved_claim.session_key
            ):
                owner_keys.add(resolved_claim.session_key)
            if linked:
                owner_keys.add(linked)
            resolved_claim = replace(
                resolved_claim,
                cron_owner_keys=frozenset(owner_keys),
            )
            if (
                exclude is not None
                and resolved_claim.registry_key
                and resolved_claim.complete
                and resolved_claim.path_match_verified
            ):
                recorded = exclude(resolved_claim.registry_key)
                resolved_claim = replace(
                    resolved_claim,
                    ledger_excluded_units=frozenset(recorded.added),
                    ledger_carry_tombstoned=recorded.carry_tombstoned,
                )
            if skip_pinned:
                result = log.delete_session(key, skip_pinned=True)
            else:
                result = log.delete_session(key)
    except HistoryLockTimeout:
        logger.warning("delete_session: lock timeout, not deleting key=%s", key)
        return False, claim
    return result, resolved_claim


async def _unstrand_shared_ledger_unit(
    state: DashboardState, session_ledger: Any, claim: _HistoryDeleteClaim
) -> None:
    """Undo the exclusion when ANOTHER retained key still runs this ACP session.

    Two keys imported onto one ACP session share its crew-log unit. Deleting one of
    them excludes that unit, but the delete preserves it for the survivor -- whose
    ledger would then read empty for good, because the rollback for a delete that did
    not happen never covers a delete that did. The exclusion belongs to the conversation
    that went, so it comes back out when the session is still somebody's.
    """
    units = tuple(claim.ledger_excluded_units)
    sid = claim.acp_session_id or ""
    if not units or not sid or not claim.registry_key:
        return
    try:
        # EXCLUDING the key this delete retired. Its own mapping can still be present
        # here, and a lookup that returns it reads as "nobody else has this session",
        # which is the answer that leaves the real survivor's record empty.
        survivor = state.sessions.find_key_by_sid(sid, exclude=claim.session_key or "")
    except Exception:
        logger.warning(
            "History delete: could not check whether %r still runs session %r; "
            "its ledger exclusions stand",
            claim.registry_key,
            sid,
            exc_info=True,
        )
        return
    if not survivor:
        return
    logger.info(
        "History delete: session %r is still run by %r, so slot %r's ledger exclusion "
        "of its unit is taken back",
        sid,
        survivor,
        claim.registry_key,
    )
    # The transcript IS gone here, so this is not a delete that did not happen: only the
    # shared unit comes back out, and the slot's legacy document stays tombstoned.
    await _rollback_ledger_exclusion(session_ledger, claim, restore_carry=False)


async def _rollback_ledger_exclusion(
    session_ledger: Any, claim: _HistoryDeleteClaim, *, restore_carry: bool
) -> None:
    """Take back what the locked transaction excluded, for a delete that did not happen.

    Only the ids that transaction reported adding, so a concurrent delete of the same
    slot keeps its own. Off-loop because the rewrite fsyncs under the slot's lock.

    *restore_carry* has NO default, so every caller states which case it is. True where
    the delete did not proceed: the session still exists, so the legacy document this
    transaction tombstoned is still owed to it. False where the transcript IS gone and
    only a shared unit comes back out -- a delete of this slot proceeded, and the
    document may be the deleted conversation's, so the tombstone stands.

    Runs when EITHER the ids or the tombstone need taking back. Gating it on the ids
    alone would skip the whole rollback for a delete that recorded no units and
    tombstoned the slot, which is the empty-unit delete.
    """
    units = tuple(claim.ledger_excluded_units)
    lift = restore_carry and claim.ledger_carry_tombstoned
    if not claim.registry_key or not (units or lift):
        return
    await asyncio.to_thread(
        session_ledger.unexclude_units, claim.registry_key, units, restore_carry=lift
    )


def _ledger_rollback_refusal() -> web.Response:
    """503 for a rollback that could not be written: the record reads empty until it is."""
    return web.json_response(
        {
            "error": (
                "This session was not deleted, but restoring its ledger record failed, so "
                "the record reads empty. Try again."
            ),
            "code": "ledger_rollback_failed",
        },
        status=503,
    )


def _exclude_slot_units(
    state: DashboardState, session_ledger: Any, claim: _HistoryDeleteClaim
) -> Any:
    """List the slot's crew-log units and exclude them; return what was RECORDED.

    A ``session_ledger.SlotExclusion``: the ids this transaction added and whether it
    tombstoned the slot's legacy document. Both are what a rollback may take back, and
    neither is inferable afterwards, which is why they travel with the claim.

    One thread hop for the whole transaction, because both halves block: the listing
    walks the crew-log root and reads a header per unit, and the write fsyncs under the
    slot's lock. Only the ids this call added come back, so a rollback cannot take away
    an exclusion a concurrent delete of the same slot recorded.

    A slot key is RECYCLED, and the listing is by slot key, so the units it returns are
    not all this conversation's: a reset in the window before the delete gives the slot a
    live successor whose unit lands under the same key. Two things keep the successor's
    record out of it -- the captured session GENERATION must still be current, and the
    unit the slot is serving on NOW is never excluded. The removal itself is fenced by
    ACP session id and does not need this; the exclusion does, because its key is the
    recyclable one.

    BOTH are re-read AFTER the listing, immediately before the ids are persisted, because
    either read taken before it cannot describe what the listing then returned. What makes
    the second generation read sufficient rather than merely narrower: a successor's unit
    is written by a session that published its reservation on the slot key first, and that
    publication ADVANCES the generation -- so a unit new enough to be a successor's cannot
    be in the listing without the generation having already moved by the time it is
    re-read. The first read stays, to refuse a stale claim before doing the walk at all.
    """
    from kiro_crew.crew_log.store import session_units_for_slot

    slot_key = claim.registry_key or ""

    def prove_generation() -> None:
        """Refuse unless the slot is still on the generation the claim captured."""
        if not claim.session_key:
            return
        try:
            if state.sessions.session_generation(claim.session_key) != claim.session_generation:
                raise session_ledger.LedgerExclusionError(
                    f"slot {slot_key!r} was reset before the delete; its units are a "
                    "successor's and must not be excluded"
                )
        except session_ledger.LedgerExclusionError:
            raise
        except Exception as exc:
            # Cannot prove the generation, so cannot prove which conversation the units
            # belong to. Refusing leaves the row deletable again; excluding could empty a
            # live successor's record for good.
            raise session_ledger.LedgerExclusionError(
                f"slot {slot_key!r}'s session generation is unreadable"
            ) from exc

    def live_unit() -> str:
        """The unit the slot serves NOW, or ``""`` when that is this conversation's own.

        The live unit is protected ONLY when it is not this conversation's. A slot still
        serving the session being deleted reports that session as live, and skipping it
        would leave the deleted conversation's own unit foldable -- the post-unlink stage
        would then be the only thing excluding it, and a failure there has nothing left
        to refuse. Anything else the slot is serving now belongs to a successor.
        """
        sid = _live_slot_sid(state, slot_key)
        return "" if sid == (claim.acp_session_id or "") else sid

    prove_generation()
    protect = live_unit()
    units = tuple(unit for unit in session_units_for_slot(slot_key) if unit != protect)
    # Re-read, in this order: the live unit first, so a successor that has recorded but
    # whose generation this thread has not observed yet is still dropped, then the
    # generation as the LAST thing before the write.
    protect_now = live_unit()
    units = tuple(unit for unit in units if unit != protect_now)
    prove_generation()
    return session_ledger.exclude_units(slot_key, units)


def _live_slot_sid(state: DashboardState, slot_key: str) -> str:
    """The ACP session id the slot is serving on RIGHT NOW, or ``""``.

    Read at exclusion time rather than taken from the claim, because the claim names the
    conversation being deleted and this names whoever holds the slot key now.
    """
    from kiro_crew.crew_log.resolve import unit_for_session_key

    try:
        slot = state._slots.get(slot_key)
        if slot is None:
            return ""
        return str(unit_for_session_key(state.sessions, effective_session_key(slot)) or "")
    except Exception:
        logger.debug("History delete: resolving the slot's live unit failed", exc_info=True)
        return ""


async def api_session_delete(request: web.Request) -> web.Response:
    """DELETE /api/sessions/{key} — permanently delete a history session."""
    state: DashboardState = request.app["state"]
    key = request.match_info["key"]
    if not state.conversation_log:
        return web.json_response({"error": "no conversation log"}, status=400)

    # Freeze the slot/transcript/manager route before the first await. The strict
    # cron-store scan establishes exact owner keys independently of that route.
    delete_claim = _capture_history_delete_claim(state, key)
    crons = getattr(state, "crons", None)
    try:
        swept = await _owner_keys_bound_to_transcript(crons, (key,))
    except (CronStoreBusy, CronStoreUnreadable) as exc:
        return _cron_store_refusal(exc)

    # The ledger exclusion is written BEFORE the unlink, and a failure REFUSES the
    # delete with the row intact. It has to be this side of the unlink to be a
    # precondition at all: the crew-log removal below runs after the transcript is
    # already gone, so a failure there has nothing left to refuse, and the
    # surviving units of this slot would stay foldable by the next session on the
    # same recycled slot key. The claim's candidate slot is tied to this transcript
    # by its filename stems; if the delete then does not proceed, the exclusion is
    # rolled back, because those units belong to a session that still exists.
    from kiro_crew import session_ledger

    # Resolve ambiguous slot ownership and read linked_session_key inside the
    # same canonical-plus-legacy lock set, before the unlink destroys either
    # piece of evidence. An unreadable owner claim refuses with the row intact.
    try:
        ok, delete_claim = await asyncio.to_thread(
            _delete_history_session,
            state.conversation_log,
            key,
            delete_claim,
            exact_owner_keys=swept.get(key, ()),
            exclude=lambda _slot: _exclude_slot_units(state, session_ledger, delete_claim),
        )
    except _OwnerKeyUnreadable:
        return _cron_ownership_unknown_refusal(crons, swept.get(key, ()))
    except session_ledger.LedgerExclusionError:
        # Nothing was unlinked: the exclusion is written inside the same hold, before the
        # delete, so a refusal leaves the row intact.
        return web.json_response(
            {
                "error": (
                    "This session's ledger exclusions could not be recorded, so it was "
                    "not deleted. Deleting it now would leave its work state readable by "
                    "the next session in the same slot."
                ),
                "code": LEDGER_EXCLUSION_UNWRITABLE_CODE,
            },
            status=409,
        )

    if not ok:
        # Nothing was destroyed, so nothing may stay excluded: those units are a live
        # session's own record. A rollback that cannot be written answers retryable
        # rather than reporting a clean refusal over a record that now reads empty.
        try:
            await _rollback_ledger_exclusion(session_ledger, delete_claim, restore_carry=True)
        except session_ledger.LedgerExclusionError:
            return _ledger_rollback_refusal()

    if ok:
        try:
            await _unstrand_shared_ledger_unit(state, session_ledger, delete_claim)
        except session_ledger.LedgerExclusionError:
            # The row IS gone: this runs after the unlink, so the answer must not say the
            # delete was refused. What is left is a surviving session whose record reads
            # empty, which is reported for what it is.
            logger.error(
                "History delete: %s was deleted, but a session sharing its crew log unit "
                "still has that unit excluded from its ledger record; restore it by hand "
                "in the slot's control directory",
                key,
            )
        # Catch a job created after the strict pre-scan but before the unlink.
        # The row is already gone, so a failed second scan is loud but cannot
        # refuse; the pre-unlink owner claim still proceeds to cleanup.
        after = await _owner_keys_after_unlink(crons, (key,))
        delete_claim = replace(
            delete_claim,
            cron_owner_keys=(delete_claim.cron_owner_keys | frozenset(after.get(key, ()))),
        )
        try:
            await _remove_slot_for_history_key(state, key, delete_claim=delete_claim)
        except Exception:
            logger.warning("cleanup failed for session %s", key, exc_info=True)
        state.push_slots_update()
        state.push_refresh("history")
    return web.json_response({"ok": ok})


# A release that loses the store-lock race is not recoverable later: the session
# is already gone, nothing re-runs this funnel, and the job's owner key can never
# be presented again. CronStoreBusy is transient contention (a large atomic save
# on network storage, the CLI process, the off-loop batch worker), so a short
# bounded backoff clears essentially every real case; past that the failure is
# reported loudly instead of dropped.
_CRON_RELEASE_ATTEMPTS = 3
_CRON_RELEASE_BACKOFF_SECS = 0.2


def _warn_stranded_cron_ownership(crons: Any, owner_keys: set[str], reason: str) -> None:
    """Name the owner and the jobs whose ownership could not be released."""
    stranded = _stranded_job_ids(crons, owner_keys)
    logger.warning(
        "History delete: cron ownership release FAILED (%s) after %d attempt(s) for owner(s) %s; "
        "job(s) %s may still be owned by a session that no longer exists — release them with "
        "`kirocrew cron adopt <id> --release`",
        reason,
        _CRON_RELEASE_ATTEMPTS,
        ", ".join(sorted(owner_keys)),
        ", ".join(stranded) if stranded else "unknown (store not readable from cache)",
    )


async def _release_cron_ownership(crons: Any, owner_keys: set[str]) -> list[str]:
    """Release every cron job owned by a RETIRED key among ``owner_keys``.

    Delegates to :meth:`CronService.release_jobs_owned_by`, which resolves BOTH
    decisions inside the store lock on its freshly reloaded state: which keys name
    a retired principal, and which jobs still carry one of them. Nothing is
    filtered here, deliberately — every candidate key goes down. Reading either
    decision from ``list_jobs`` would read a cache with up to one timer-poll
    interval of cross-process staleness, and both answers are then wrong in a way
    that strands jobs this funnel will never revisit: a job re-adopted since the
    read gets its new owner cleared, and a cron deleted by the CLI inside the
    window is called live so its children are never released.

    :class:`CronStoreBusy` is retried with a bounded backoff. Any failure that
    outlives the retries — sustained contention, or an unreadable store that
    ``_sync_for_write`` refuses to write over — is surfaced by
    :func:`_warn_stranded_cron_ownership` naming the owner and the candidate job
    ids, because nothing re-runs this funnel and the deleted session can never
    present its key again. Returns the ids released (empty when there was nothing
    to release, and when the release ultimately failed).
    """
    keys = {k for k in owner_keys if k}
    if crons is None or not keys:
        return []
    reason = "cron store busy"
    for attempt in range(_CRON_RELEASE_ATTEMPTS):
        try:
            return await crons.release_jobs_owned_by(keys)
        except CronStoreBusy:
            if attempt + 1 < _CRON_RELEASE_ATTEMPTS:
                await asyncio.sleep(_CRON_RELEASE_BACKOFF_SECS * (attempt + 1))
        except Exception as exc:
            # Not contention, so a retry cannot help (an unreadable store, a
            # failed save). Still report it through the recovery path rather than
            # letting the caller's generic handler log it without the ids or the
            # command that fixes it.
            reason = f"{type(exc).__name__}: {exc}"
            break
    _warn_stranded_cron_ownership(crons, keys, reason)
    return []


async def _remove_slot_for_history_key(
    state: DashboardState,
    key: str,
    *,
    delete_claim: _HistoryDeleteClaim | None = None,
) -> None:
    """Remove only the exact slot and manager generation captured before unlink."""
    claim = delete_claim or _capture_history_delete_claim(state, key)
    slot = None
    if (
        claim.slot is not None
        and claim.registry_key is not None
        and claim.complete
        and claim.path_match_verified
    ):
        current_slot = state._slots.get(claim.registry_key)
        ownership_current = False
        if current_slot is claim.slot and claim.session_key is not None:
            try:
                ownership_current = (
                    slot_history_key(current_slot) == claim.history_key
                    and effective_session_key(current_slot) == claim.session_key
                    and getattr(current_slot, "task", None) is claim.slot_task
                    and state.sessions.session_generation(claim.session_key)
                    == claim.session_generation
                )
            except Exception:
                logger.warning(
                    "History delete: captured slot ownership is unreadable for %s; "
                    "preserving the slot",
                    key,
                    exc_info=True,
                )
        if current_slot is claim.slot and ownership_current:
            popped = state._slots.pop(claim.registry_key, None)
            if popped is claim.slot:
                slot = claim.slot
        else:
            logger.info(
                "History delete: slot %s was replaced, rerouted, or reclaimed; preserving it",
                claim.registry_key,
            )

    # The chat's backend pin lives in the gateway-private store keyed by slot key,
    # not in the transcript just unlinked, so it is dropped here or it would
    # outlive the chat and apply to a later slot created under the same name. Only
    # for the slot THIS call popped, and only while the store still names that
    # slot as the pin's owner (its ``created_at``, compared and deleted in one
    # locked read-modify-write): a replacement chat created under the same key
    # while this await was pending carries a different creation identity, so its
    # pin survives a delete that finishes late. With no popped slot there is no
    # owner to condition on, and nothing is removed -- an unowned stale entry can
    # only ever be overwritten by a later pin on that key, never applied to one.
    # Best-effort: a pin that cannot be dropped is such a stale entry, not a
    # routing decision for this deleted chat.
    if slot is not None:
        try:
            await asyncio.to_thread(backend_pins.forget_pin, slot.key, owner=slot.created_at)
        except OSError:
            logger.warning("History delete: backend pin for %s could not be dropped", slot.key)

    target_session_key = claim.session_key if slot is not None else None
    target_session_generation = claim.session_generation

    def live_slot_owns_session(session_key: str) -> bool:
        """Fail safe when any current slot owns or cannot resolve *session_key*."""
        canonical_session_key = canonical_key(session_key)
        for current_slot in tuple(state._slots.values()):
            try:
                if canonical_key(effective_session_key(current_slot)) == canonical_session_key:
                    return True
            except Exception:
                logger.warning(
                    "History delete: current slot session owner unreadable for %s; "
                    "preserving destructive cleanup",
                    key,
                    exc_info=True,
                )
                return True
        return False

    def live_runtime_owns_session(session_key: str) -> bool:
        """Fail safe when a slot or SessionManager runtime still owns the key."""
        if live_slot_owns_session(session_key):
            return True
        canonical_session_key = canonical_key(session_key)
        try:
            manager_keys = state.sessions.session_keys()
            return any(
                canonical_key(manager_key) == canonical_session_key for manager_key in manager_keys
            )
        except Exception:
            logger.warning(
                "History delete: current SessionManager owners unreadable for %s; "
                "preserving cron ownership",
                key,
                exc_info=True,
            )
            return True

    if slot:
        cancelled = state.cancel_questions_for_slot(slot.key)
        if cancelled:
            logger.info(
                "History delete: cancelled %d pending question(s) on slot %s",
                cancelled,
                slot.key,
            )
    if slot:
        teardown_tasks = {
            task
            for task in (slot.task, slot._stage_controller_task)
            if task is not None and not task.done()
        }
        if teardown_tasks:
            for task in teardown_tasks:
                task.cancel()
            try:
                await asyncio.wait_for(
                    asyncio.gather(*teardown_tasks, return_exceptions=True),
                    timeout=2.0,
                )
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                pass

    session_destroyed = False
    if slot and target_session_key is not None:
        try:
            destroyed = await state.sessions.destroy_if(
                target_session_key,
                target_session_generation,
                lambda: not live_slot_owns_session(target_session_key),
                preserve_autocompact_override=True,
            )
            if not destroyed:
                logger.info(
                    "History delete: session %s was replaced, busy, reserved, or has "
                    "a current slot owner; preserving it",
                    target_session_key,
                )
            session_destroyed = bool(destroyed)
        except Exception:
            logger.warning(
                "History delete: conditional session destroy failed for %s",
                target_session_key,
                exc_info=True,
            )

    # Pins, work ledgers, and autocompact overrides are independent state. A
    # transcript can be created or restored by another process after any owner
    # scan, so deleting those sidecars cannot be made atomic here. Preserve them:
    # stale state is reversible, while deleting a successor's state is not.

    # The append-only SESSION LEDGER is not one of those sidecars, and the
    # difference is mechanical rather than a re-reading of the rule above. What
    # makes a work ledger unsafe to delete here is that its identity is the SLOT
    # KEY, which is recycled -- a successor tab in the same slot legitimately
    # inherits and resumes that record, and no check here can prove one is not
    # about to. A session's log is keyed by the ACP SESSION ID, which never
    # names a different conversation, so removing it cannot reach a successor's
    # state.
    #
    # Gated on the teardown having SUCCEEDED, not merely on having been
    # attempted. ``destroy_if`` refuses when the session was replaced by a
    # successor generation, is busy, or still has a live slot owner -- and in each
    # of those cases the session it names is preserved and may be writing right
    # now, so removing its crew log would take a live conversation's log. The write
    # lease is not a substitute for this check: ownership ends BETWEEN turns by
    # design, so an idle-but-live session holds nothing for the removal to be
    # refused by. A refused teardown therefore leaves the crew log alone and the
    # retention sweep collects it once the session is genuinely closed.
    #
    # Only an id this claim PROVED is used -- captured pre-unlink, dropped on every
    # path that disowned the slot -- so an unresolvable one removes nothing.
    if session_destroyed and claim.acp_session_id:
        # A key still mapping to this session id VETOES the removal. The unit is
        # keyed by the ACP id, so a second key pointing at that id shares this very
        # crew log, and the teardown above removed only ONE mapping -- the other holder
        # can still resume, and its log is still needed. Two keys on one sid is a
        # state the system itself produces: importing a transferred session twice
        # allocates a new slot key each time and deliberately leaves the source
        # intact (see dashboard/session_transfer.py). Reading the map to WITHHOLD a
        # deletion is safe in the way reading it to authorize one is not -- a forged
        # or emptied map can only make this keep more than it must.
        try:
            retained_key = state.sessions.find_key_by_sid(claim.acp_session_id)
        except Exception:
            # An unreadable map is not evidence that nothing else maps this id.
            retained_key = "<unreadable session map>"
        if retained_key is not None:
            logger.info(
                "History delete: session id for %s is still mapped; leaving its crew log "
                "to retention",
                key,
            )
        else:
            # Every slot spelling this delete established for itself. The removal
            # requires the unit's own header to name one of them, because the id
            # above came from ``session_map.json`` -- inside the agent-visible tree --
            # while the header is written once inside the fenced crew log tree and
            # never rewritten. A mapping that named another conversation's session
            # would aim this removal at that conversation's crew log; its header would
            # not name this slot. The candidate spellings are included beside the
            # live slot key so a legacy spelling of the same slot is not read as a
            # different one.
            proven_slots = frozenset(
                spelling
                for spelling in (
                    getattr(claim.slot, "key", None),
                    claim.session_key,
                    *_history_delete_candidate_keys(key),
                )
                if isinstance(spelling, str) and spelling
            )
            await asyncio.to_thread(
                _remove_session_crew_log, claim.acp_session_id, key, proven_slots
            )

    # Cron ownership is different from the independent sidecars above: every key
    # here came from the strict store scan or from linked_session_key while the
    # transcript lock was held. Re-check the live slot and SessionManager
    # registry after the teardown awaits, then compare-and-clear only those exact
    # retired owners in the cron store's own lock. A successor already presenting
    # one of the keys keeps it.
    proven_owner_keys = {owner for owner in claim.cron_owner_keys if owner}
    releasable_owner_keys = {
        owner for owner in proven_owner_keys if not live_runtime_owns_session(owner)
    }
    preserved_owner_keys = proven_owner_keys - releasable_owner_keys
    if preserved_owner_keys:
        logger.info(
            "History delete: current runtime owner(s) still present for %s; preserving cron "
            "ownership for %s",
            key,
            ", ".join(sorted(preserved_owner_keys)),
        )
    try:
        released = await _release_cron_ownership(
            getattr(state, "crons", None), releasable_owner_keys
        )
    except Exception:
        logger.warning("History delete: cron ownership release failed for %s", key, exc_info=True)
    else:
        if released:
            logger.info(
                "History delete: released %d cron job(s) for %s: %s",
                len(released),
                key,
                ", ".join(released),
            )


#: How long the delete funnel waits for the teardown entry to land before taking
#: the lease. Short on purpose: the flush is what lets the emitter release this
#: session's cached handle, and with it the write lease this removal must claim.
#: A timeout is not a failure -- the entry stays owed, so the unit becomes
#: collectable by the retention sweep even when this pass is refused.
_CREW_LOG_TEARDOWN_FLUSH_SECONDS = 2.0


def _remove_session_crew_log(
    session_id: str, history_key: str, proven_slots: "frozenset[str]"
) -> None:
    """Remove the append-only crew log of *session_id*. Never raises.

    Imported lazily: this handler module is loaded on every startup while the
    crew log store is only reachable behind ``KIROCREW_CREW_LOG``, so a
    launch without the flag should not pay for the import.

    *proven_slots* is every slot spelling this delete established for itself, and
    the unit's own HEADER has to name one of them. The id alone is not enough,
    because it arrives from ``session_map.json`` -- a file inside the agent-visible
    tree -- so a mapping that named another conversation's session would aim this
    removal at that conversation's crew log. The header is the independent answer: it
    is written once at creation inside the fenced crew log tree and never rewritten,
    so it does not move when a mapping does, and a unit belonging to another slot
    fails the check. Slot recycling does not weaken it, because the id is what
    selects the unit and the slot only has to prove the unit belonged to the slot
    being deleted.

    A header that cannot be proved -- unreadable, or carrying no slot, which is the
    case for a session that never ran on a dashboard slot -- removes NOTHING and
    leaves the unit to the retention sweep, which collects it on age once its close
    has landed.

    Best-effort, like every other step of this teardown. The transcript row is
    already gone by the time this runs, so raising would turn a crew log that could
    not be collected into a failed delete the user has to retry against a row that
    is absent -- and the retention sweep collects it on age regardless.
    ``owned`` is the ordinary answer when a queued write still holds the lease,
    and it is not an error: that pass simply does nothing.
    """
    try:
        from kiro_crew.crew_log import emit as crew_log_emit
        from kiro_crew.crew_log.schema import KIND_SESSION
        from kiro_crew.crew_log.store import (
            REMOVE_REMOVED,
            remove_unit,
            unit_header_slot,
        )

        # LET THE TEARDOWN ENTRY LAND FIRST, and not as a courtesy: until it does,
        # this removal cannot succeed at all. ``destroy`` -- the teardown checked
        # above -- writes ``session/closed`` through the emitter's own thread, and
        # the emitter releases this session's cached handle, with the write lease
        # that handle carries, only once that entry lands. A removal claiming the
        # lease ``sole`` is refused while the handle is held, so without this
        # barrier the funnel answers ``owned`` for every session the gateway
        # actually emitted for. Measured directly: a crew log opened through the
        # emitter answers ``owned``, and ``removed`` only after its close lands.
        #
        # A timeout is not a failure and is not treated as one. The entry stays
        # owed, so the unit becomes collectable by the retention sweep -- which
        # needs that same close, since a unit whose newest lifecycle entry is not
        # a close reads as OPEN whatever its age. This pass simply does nothing.
        crew_log_emit.flush(timeout=_CREW_LOG_TEARDOWN_FLUSH_SECONDS)

        header_slot = unit_header_slot(KIND_SESSION, session_id)
        if header_slot is None or header_slot not in proven_slots:
            logger.info(
                "History delete: the session's log for %s names slot %r, which this "
                "delete did not prove; leaving it to retention",
                history_key,
                header_slot,
            )
            return

        # Every OTHER unit of this slot is excluded from the ledger fold before this
        # one is removed. A slot accumulates one unit per reset, this funnel removes
        # only the conversation's own, and the survivors stay readable -- so a fresh
        # session on the same recycled slot key would fold them and read a deleted
        # conversation's goal and phase. The slot's OTHER units are excluded before the
        # transcript is unlinked, under the delete's own lock and against its captured
        # generation; this stage runs after the unlink, where a rescan by the recyclable
        # slot key could pick up a successor's unit and hide a live conversation's
        # record for good. So only THIS conversation's unit is excluded here -- the one
        # the header check above just proved belongs to the proved slot.
        from kiro_crew import session_ledger

        session_ledger.exclude_units(header_slot, (session_id,))

        # Accept-all guard, deliberately. The sweep's guard re-reads the crew log
        # because ITS reason is a property of the file -- an age it sampled outside
        # the lease. This caller's reason is not in the file at all: the session
        # this crew log belongs to was destroyed by the teardown above, which is
        # checked before this runs, and the header check above already proved the
        # unit is this slot's. Re-reading the age here would answer a question
        # nobody asked, and a close entry still queued behind the teardown would
        # make the honest answer "not closed" and skip a crew log whose session is
        # gone.
        status = remove_unit(KIND_SESSION, session_id, guard=lambda _dir: True)
    except session_ledger.LedgerExclusionError:
        # The exclusion is what stops a later session on this recycled slot key from
        # folding the units this delete leaves behind. Without it the removal is
        # refused outright: the conversation stays visible and can be deleted again,
        # whereas its state showing up in a stranger's session cannot be undone.
        logger.error(
            "History delete: refusing to remove the session's log for %s -- slot %r's "
            "surviving units could not be excluded from its ledger record",
            history_key,
            header_slot,
        )
        return
    except Exception:
        logger.warning(
            "History delete: could not remove the session's log for %s",
            history_key,
            exc_info=True,
        )
        return
    if status == REMOVE_REMOVED:
        logger.info("History delete: removed the session's log for %s", history_key)
    else:
        logger.info(
            "History delete: the session's log for %s not removed (%s); leaving it to retention",
            history_key,
            status,
        )


async def api_sessions_clear(request: web.Request) -> web.Response:
    """DELETE /api/sessions — permanently delete closed history sessions only."""
    state: DashboardState = request.app["state"]
    if not state.conversation_log:
        return web.json_response({"error": "no conversation log"}, status=400)

    from kiro_crew import session_ledger

    log = state.conversation_log
    clearable, skipped, unreadable = await asyncio.to_thread(_clearable_history_keys, state, log)

    # One strict owner scan covers the whole selection. A known cron-store
    # failure refuses before any transcript is unlinked.
    crons = getattr(state, "crons", None)
    try:
        swept = await _owner_keys_bound_to_transcript(crons, clearable)
    except (CronStoreBusy, CronStoreUnreadable) as exc:
        return _cron_store_refusal(exc, cleared=0, skipped=skipped, failed=0)

    count = 0
    failed = 0
    cleanup_claims: list[tuple[str, _HistoryDeleteClaim]] = []
    undeletable: list[dict[str, str]] = [
        {"id": key, "code": CRON_OWNERSHIP_UNKNOWN_CODE} for key in unreadable
    ]
    for key in clearable:
        # Re-check per iteration so a tab opened during an earlier await wins.
        if key in _open_slot_transcript_keys(state):
            skipped += 1
            continue

        delete_claim = _capture_history_delete_claim(state, key)
        # Per ROW, the same precondition the single delete applies: the ledger
        # exclusion is written before this row's transcript is unlinked, because a
        # failure after the unlink has nothing left to refuse and would leave this
        # slot's surviving units foldable by the next session on the recycled key. A
        # row whose exclusion cannot be written is reported undeletable and its
        # transcript is left alone; a row that then does not delete is rolled back.
        try:
            result, delete_claim = await asyncio.to_thread(
                _delete_history_session,
                log,
                key,
                delete_claim,
                skip_pinned=True,
                exact_owner_keys=swept.get(key, ()),
                exclude=lambda _slot: _exclude_slot_units(state, session_ledger, delete_claim),
            )
            if result is None:
                skipped += 1
                await _rollback_ledger_exclusion(session_ledger, delete_claim, restore_carry=True)
            elif result:
                # Counted as CLEARED whatever the unstrand does: the transcript is gone,
                # so calling the row undeletable would be false. A failure there leaves a
                # sharing session's record excluded, which is logged for what it is.
                try:
                    await _unstrand_shared_ledger_unit(state, session_ledger, delete_claim)
                except session_ledger.LedgerExclusionError:
                    logger.error(
                        "Bulk clear: %s was deleted, but a session sharing its crew log "
                        "unit still has that unit excluded from its ledger record; restore "
                        "it by hand in the slot's control directory",
                        key,
                    )
                cleanup_claims.append((key, delete_claim))
                count += 1
            else:
                failed += 1
                await _rollback_ledger_exclusion(session_ledger, delete_claim, restore_carry=True)
        except _OwnerKeyUnreadable:
            undeletable.append({"id": key, "code": CRON_OWNERSHIP_UNKNOWN_CODE})
        except session_ledger.LedgerExclusionError:
            # The exclusion or its rollback could not be written, so this row's transcript
            # is left alone and the row says why. Nothing was unlinked: the exclusion runs
            # inside the same hold, before the delete.
            undeletable.append({"id": key, "code": LEDGER_EXCLUSION_UNWRITABLE_CODE})
        except Exception:
            failed += 1
            # The rollback can fail too, and it must not turn one row's failure into the
            # whole batch's: the remaining rows are still deletable.
            try:
                await _rollback_ledger_exclusion(session_ledger, delete_claim, restore_carry=True)
            except session_ledger.LedgerExclusionError:
                logger.error(
                    "Bulk clear: %s was not deleted and its ledger exclusion could not be "
                    "rolled back; that session's record reads empty until it is restored "
                    "by hand in the slot's control directory",
                    key,
                )
            logger.warning("api_sessions_clear: delete raised for %s", key, exc_info=True)

    if cleanup_claims:
        # One post-unlink scan catches owners created anywhere in the batch
        # window. Extend each immutable claim before its fenced cleanup runs.
        after = await _owner_keys_after_unlink(
            crons, [cleanup_key for cleanup_key, _claim in cleanup_claims]
        )
        cleanup_tasks = [
            _remove_slot_for_history_key(
                state,
                cleanup_key,
                delete_claim=replace(
                    delete_claim,
                    cron_owner_keys=(
                        delete_claim.cron_owner_keys | frozenset(after.get(cleanup_key, ()))
                    ),
                ),
            )
            for cleanup_key, delete_claim in cleanup_claims
        ]
        await asyncio.gather(*cleanup_tasks, return_exceptions=True)
    if count:
        state.push_slots_update()
        state.push_refresh("history")
    logger.info(
        "api_sessions_clear: cleared=%d skipped=%d failed=%d undeletable=%d",
        count,
        skipped,
        failed,
        len(undeletable),
    )
    return web.json_response(
        {
            "ok": failed == 0,
            "cleared": count,
            "skipped": skipped,
            "failed": failed,
            "undeletable": undeletable,
        }
    )


def _clearable_history_keys(
    state: DashboardState,
    log: Any,
) -> tuple[list[str], int, list[str]]:
    """The history sessions a bulk clear would remove.

    ONE implementation, shared by ``api_sessions_clear`` and the clearable-count
    endpoint, so the number a confirmation displays cannot drift from the set the
    delete takes. Two implementations would let the dialog
    promise a number the delete does not honour, which is the whole reason a
    count exists.

    Takes NO age cutoff, deliberately. ``DELETE /api/sessions`` accepts none and
    removes every clearable session, so a count filtered by age would report a
    SUBSET of what the delete then permanently unlinks — a confirmation showing a
    smaller number than the delete honours. There is no cutoff to offer until the
    delete itself grows one, and then both sides grow it together through this
    function.

    Returns ``(clearable, skipped, unreadable)``. A session is skipped when it is
    reachable as an open tab or when its metadata says ``pinned``. A session whose
    metadata could not be read is excluded and listed in ``unreadable`` by key,
    rather than folded into a count the caller cannot act on. Present but
    unparseable metadata is unreadable: ``get_metadata_status`` returns
    ``({}, False)``, so both the preview and delete exclude it rather than treating
    missing identity as permission. The bulk clear reports those rows by id and
    code; the single delete refuses them with 409 ``cron_ownership_unknown``.

    Reads the filesystem (``list_sessions`` globs and stats every session file),
    so callers offload it off the event loop.
    """
    open_keys = _open_slot_transcript_keys(state)

    clearable: list[str] = []
    skipped = 0
    unreadable: list[str] = []
    for row in log.list_sessions():
        key = row.get("key", "")
        if not key:
            continue
        if key in open_keys:
            skipped += 1
            continue
        # Mirror delete_session(skip_pinned=True): pinned and unreadable metadata
        # both mean "leave it alone". This read is unlocked, so what comes back is
        # a SNAPSHOT the delete may narrow: it re-checks pinned under its lock and
        # re-checks open tabs per iteration, so a session pinned or reopened after
        # this pass is skipped there. The count can therefore over-report a
        # concurrent change, but nothing here can make the delete take a session
        # its own locked check refuses.
        try:
            meta, readable = log.get_metadata_status(key)
        except Exception:
            # Not reachable through get_metadata_status's documented returns; a
            # genuinely unexpected failure must not read as permission to delete.
            unreadable.append(key)
            continue
        if not readable or not isinstance(meta, dict):
            # Reported by key rather than folded into ``skipped``: the single
            # delete refuses this row with 409 cron_ownership_unknown, and a
            # clear that only said "skipped: N" hid which rows can never be
            # cleared and why (the by-id remedy needs the id).
            unreadable.append(key)
            continue
        if meta.get("pinned"):
            skipped += 1
            continue
        clearable.append(key)
    return clearable, skipped, unreadable


async def api_sessions_clearable_count(request: web.Request) -> web.Response:
    """GET /api/sessions/clearable/count — how many sessions a bulk clear removes.

    The confirmation for a bulk delete has to state how many sessions it will
    remove, and ``DELETE /api/sessions`` offers no way to learn that before
    committing. This answers the question and nothing else — it
    is a GET, so no code path here can delete anything.

    A GET rather than a ``dry_run`` flag on the DELETE, deliberately departing
    from ``POST /api/system/session-storage/cleanup``'s pattern: a flag on the
    destructive verb means a caller that drops the flag deletes instead of
    counting, while a GET cannot delete however it is called.

    Takes no parameters. The count is of exactly the set ``DELETE /api/sessions``
    removes, because that delete accepts no cutoff — see
    :func:`_clearable_history_keys` for why offering one here would report a
    subset of what the delete actually takes.
    """
    state: DashboardState = request.app["state"]
    if not state.conversation_log:
        return web.json_response(
            {"error": "no conversation log", "code": "count_unavailable"}, status=400
        )

    clearable, _skipped, _unreadable = await asyncio.to_thread(
        _clearable_history_keys, state, state.conversation_log
    )
    return web.json_response({"sessions": len(clearable)})


# ── Approvals ──


async def api_approvals(request: web.Request) -> web.Response:
    """GET /api/approvals — list pending tool approvals."""
    state: DashboardState = request.app["state"]
    return web.json_response(list(state._pending_approvals.values()))


async def api_approval_resolve(request: web.Request) -> web.Response:
    """POST /api/approvals/{id}/{action} — approve, reject, or reject_once."""
    state: DashboardState = request.app["state"]
    approval_id = request.match_info["id"]
    action = request.match_info["action"]
    if action not in ("approve", "reject", "reject_once"):
        return web.json_response({"error": "invalid action"}, status=400)
    ok = state.resolve_approval(
        approval_id, action == "approve", rejected_once=action == "reject_once"
    )
    if not ok:
        return web.json_response({"error": "not found or expired"}, status=404)
    return web.json_response({"ok": True})


async def api_session_directive(request: web.Request) -> web.Response:
    """POST /api/session-directive — park a validated session directive.

    The provider-neutral leg of the session-directive protocol. Its only caller is
    a Kiro Crew directive tool (``mcp_tools.control._emit_directive``), which has
    already validated the payload; this route carries that payload to the gateway
    OUT OF BAND so the turn's consumer can apply it without having to trust the
    model-visible marker in the tool result. See
    :mod:`kiro_crew.dashboard.directive_queue` for why that is not weaker than the
    marker gate it backs up.

    Authenticated via X-Internal-Secret, and the session is selected by the
    X-Session-Key header every MCP subprocess already sends — the SAME shape as
    ``api_session_keepalive``. The header is not taken on faith: on the unix
    socket ``token_auth`` kernel-verifies the peer and denies 403 when it resolves
    to a session key other than the declared one, which is the check that stops a
    caller parking a directive against somebody else's session.

    A call that derives no directive (unknown tool, validation refusal) is a 400,
    not a silent drop: the only legitimate callers are Kiro Crew's own directive
    tools, so a request that does not derive did not come from one.
    """
    _, refusal = await internal_memory_scope(
        request, "api_session_directive", claimed_session=request.headers.get("X-Session-Key", "")
    )
    if refusal is not None:
        return refusal
    # Re-assert the caller's locality BEFORE the header is read. The route is in
    # server.py's strict allowlist, but a ``local_only=False`` deployment
    # reclassifies strict paths as MIXED — so the auth middleware also admits a
    # cookie/token-authenticated browser caller here, and such a caller picks its
    # own ``X-Session-Key``. That is somebody else's session: a parked record is
    # applied verbatim by the next consumer frame in the named turn, so accepting
    # it would hand a remote cookie holder a cross-session mutation (arm a loop,
    # retarget a project). ``internal_auth`` is set only after a constant-time
    # ``X-Internal-Secret`` match on a same-machine transport; ``peer_verified``
    # only after the kernel-attested AF_UNIX peer resolved to the DECLARED key —
    # either one is positive proof of a local caller, and a cookie carries
    # neither. Same predicate as ``handlers/updates.py``'s host-locality gate.
    if not (request.get("internal_auth") or request.get("peer_verified")):
        logger.warning(
            "session-directive REFUSED (not_local_caller): neither internal_auth nor "
            "peer_verified was set, so the caller could not be proven local. "
            "Nothing was parked."
        )
        return web.json_response(
            {"error": "local caller required", "code": "not_local_caller"},
            status=403,
        )
    session_key = request.headers.get("X-Session-Key", "").strip()
    if not session_key:
        logger.warning(
            "session-directive REFUSED (session_key_required): the caller sent no "
            "X-Session-Key, so no session could be named and nothing was parked. On a "
            "backend that emits no _meta.kiro identity this is fatal: the marker cannot "
            "be trusted either, so the directive is dropped entirely."
        )
        return web.json_response(
            {"error": "X-Session-Key required", "code": "session_key_required"},
            status=400,
        )
    body: dict = {}
    try:
        if request.can_read_body:
            parsed = await request.json()
            if isinstance(parsed, dict):
                body = parsed
    except Exception:
        return web.json_response(
            {"error": "malformed JSON body", "code": "invalid_body"}, status=400
        )
    # The body names the CALL -- the directive tool's name and the raw
    # ``tools/call`` arguments the MCP stub served -- and nothing else. The
    # payload is DERIVED here by re-running that tool on those arguments
    # (``mcp_core.derive_directive``), and the claim key is computed here from the
    # same pair. A caller who can reach this route therefore controls only what
    # the named session's own call would have produced: it cannot pair payload X
    # with the digest of call Y, which a caller-supplied (args, input_digest) body
    # allowed and which is exactly the substitution the two-channel design exists
    # to prevent.
    # Lazy on purpose: mcp_core is the MCP server module and imports every tool
    # handler; no dashboard module loads it at import time, and this route is
    # the only one that needs it.
    from kiro_crew import mcp_core

    tool = str(body.get("tool") or "").strip()
    raw_args = body.get("raw_args")
    if not isinstance(raw_args, dict):
        raw_args = {}
    if not tool and "kind" in body:
        # The body shape a Kiro Crew MCP server from BEFORE the call-input
        # protocol sends: the directive's ``kind``/``args`` rather than the call.
        # That server is running older code than this gateway -- a pooled MCP
        # backend that outlived a code change -- and every directive it emits
        # will land here until it is replaced. Name that, rather than the
        # generic "not derivable", because the generic wording sent an operator
        # to the directive tools when the fault was a stale process.
        logger.warning(
            "session-directive REFUSED (stale_mcp_backend) for session_key=%r: the "
            "MCP server sent a pre-call-input body (kind=%r) -- it is running OLDER "
            "code than this gateway. A pooled backend survived a code change; run "
            "`kirocrew restart` (which replaces the MCP gateway daemon too) or "
            "`kirocrew doctor` to see the daemon's code revision. Nothing was parked.",
            session_key,
            body.get("kind"),
        )
        return web.json_response(
            {
                "error": "MCP server runs older code than the gateway; restart it",
                "code": "stale_mcp_backend",
            },
            status=400,
        )
    derived = mcp_core.derive_directive(tool, raw_args, session_key)
    if derived is None:
        logger.warning(
            "session-directive REFUSED (not_derivable) for session_key=%r tool=%r: "
            "re-running the tool on the reported arguments published no directive "
            "(unknown tool, validation refusal, or handler error). Nothing was parked.",
            session_key,
            tool,
        )
        return web.json_response(
            {"error": "directive could not be derived from the call", "code": "not_derivable"},
            status=400,
        )
    kind, args = derived
    input_digest = session_directive.call_input_digest(tool, raw_args)
    try:
        record_id = directive_queue.publish(session_key, kind, args, input_digest)
    except ValueError as exc:
        logger.warning(
            "session-directive REFUSED (invalid_directive) for session_key=%r kind=%r: "
            "%s. Nothing was parked.",
            session_key,
            kind,
            exc,
        )
        return web.json_response({"error": str(exc), "code": "invalid_directive"}, status=400)
    return web.json_response({"ok": True, "id": record_id})


async def api_session_keepalive(request: web.Request) -> web.Response:
    """POST /api/session-keepalive — refresh activity timestamp on the
    session's provider so idle-detection/stale-checks don't SIGTERM a
    session that's intentionally blocking in a long-running MCP tool
    (e.g. the `wait` tool).

    Authenticated via X-Internal-Secret; session is selected via the
    X-Session-Key header that all MCP subprocesses already send.

    Doubles as the sleeping `wait` tool's only inbound channel. When the body
    names a ``wait_id`` the reply may carry ``end_wait: <wait_id>``, which the
    tool treats as "return early, keep the turn". The body is optional and every
    field in it is advisory: a caller that sends ``{}`` gets the original
    touch-only behaviour.
    """
    _, refusal = await internal_memory_scope(
        request, "api_session_keepalive", claimed_session=request.headers.get("X-Session-Key", "")
    )
    if refusal is not None:
        return refusal
    state: DashboardState = request.app["state"]
    session_key = request.headers.get("X-Session-Key", "").strip()
    if not session_key:
        return web.json_response({"error": "X-Session-Key required"}, status=400)
    provider = state.sessions.get_provider(session_key)
    if provider is None:
        return web.json_response({"error": "session not found"}, status=404)
    body: dict = {}
    try:
        if request.can_read_body:
            parsed = await request.json()
            if isinstance(parsed, dict):
                body = parsed
    except Exception:
        # A malformed body must never cost the session its keepalive — that is
        # the half of this route that keeps the watchdog from killing the ACP
        # subprocess mid-wait.
        body = {}
    try:
        provider.touch_activity()
    except Exception as exc:
        logger.debug("touch_activity failed for %s: %s", session_key, exc)
        return web.json_response({"error": "touch failed"}, status=500)
    # Also advance the session's own last_used clock. touch_activity() only
    # refreshes the ACP runtime's activity timestamp, which feeds
    # is_responsive()/the stall watchdog, while the periodic idle sweep reads
    # ``last_used`` instead. The sweep skips any session whose turn permit is
    # held, and a tool reaching this route runs inside such a turn, so the idle
    # verdict for a session blocking in a long `wait` is settled by that guard
    # rather than by this touch. What the touch buys is the boundary: it leaves
    # ``last_used`` pointing at the end of the turn's work instead of its start,
    # so once the permit drops the sweep measures idleness from when the session
    # went quiet.
    try:
        touched = getattr(state.sessions, "touch", None)
        if callable(touched):
            touched(session_key)
    except Exception:
        logger.debug("last_used touch failed for %s", session_key, exc_info=True)
    reply: dict = {"ok": True}
    wait_id = str(body.get("wait_id") or "").strip()[:64]
    if wait_id:
        _service_wait_ping(state, session_key, wait_id, body, reply, provider)
    return web.json_response(reply)


def _wait_end_reason(slot, wait_id: str, provider: Any) -> str | None:
    """Why this sleep should return early, or None to keep sleeping.

    Exactly two reasons, and the narrowness is the design:

    ``"user"``
        The End-wait button parked an explicit request naming this ``wait_id``.

    ``"steer"``
        A mid-turn steer reached the backend AFTER this sleep began. kiro-cli
        can only inject a steer at a model-inference boundary and an in-flight
        tool call is the absence of one, so without this the user's correction
        sits in the backend's steer queue until the sleep elapses — up to the
        tool's 1800s ceiling — while the agent sleeps through it.

    "After this sleep began" is decided by comparing the provider's steer stamp
    against the reading taken when this sleep was minted, so the handler reads
    no clock of its own: the only two values ever compared are two readings of
    the same monotonic source. That is deliberate — a wall-clock stamp on one
    side and a monotonic one on the other is how a suspend silently reorders
    the comparison.

    Re-taking the baseline at every mint is also what makes the reason fire
    once. A steer the backend has accepted but not yet injected stays newer
    than nothing at all, so without a per-sleep baseline it would end sleep
    after sleep for the rest of the turn and hand the model a `wait` that
    returns instantly.

    Not extended to the other long block on this route. ``spawn_sub_agents``
    can wait 7200s on live sub-agents and pings the same endpoint, but sends no
    ``wait_id`` and so never reaches this decision — an exclusion worth keeping
    deliberately: ending a sleep discards nothing, while cutting a sub-agent
    collection short orphans work that keeps running with nobody left to read
    its results.
    """
    if slot._end_wait_request and slot._end_wait_request == wait_id:
        return "user"
    tracked = slot._wait_state or {}
    if tracked.get("wait_id") != wait_id:
        # Not the sleep this slot is tracking (contested identity, stale ping).
        return None
    steered_at = _provider_steer_stamp(provider)
    return "steer" if steered_at > slot._wait_steer_baseline else None


def _provider_steer_stamp(provider: Any) -> float:
    """Monotonic time of the session's last steer, 0.0 when never steered or
    when the provider does not expose one (a non-kiro backend, a test double).

    Read through ``getattr`` rather than an interface method because this route
    is reached by every backend, and a missing stamp must read as "no steer" —
    the direction that keeps sleeping — rather than raise on the keepalive that
    stops the watchdog killing the session mid-sleep.
    """
    try:
        return float(getattr(provider, "last_steer_monotonic", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _service_wait_ping(
    state: DashboardState,
    session_key: str,
    wait_id: str,
    body: dict,
    reply: dict,
    provider: Any = None,
) -> None:
    """Track an in-flight `wait` sleep and hand back any early-end request.

    Mutates ``reply`` in place, adding ``end_wait`` when the sleep should return
    early (see :func:`_wait_end_reason`). Silent no-op when the calling session
    has no dashboard tab (a Slack/cron session can call `wait` too — it just has
    nothing to render a countdown on).
    """
    # Local import: this module's other chat_utils uses are function-local for
    # the same circular-import reason (handlers/__init__ re-exports this module).
    from kiro_crew.dashboard.chat_utils import dashboard_slot_key

    slot_name = dashboard_slot_key(session_key)
    slot = state.get_slot(slot_name) if slot_name else None
    if slot is None:
        return
    now = time.time()
    # How stale the incumbent's last ping must be before its sleep is presumed
    # gone. 2.5 intervals tolerates one dropped ping plus scheduling jitter
    # without tolerating a sleep that is simply still running.
    try:
        interval = float(body.get("interval") or 5.0)
    except (TypeError, ValueError):
        interval = 5.0
    window = max(2.0, min(60.0, interval) * 2.5)
    if body.get("wait_done"):
        # Only the wait that owns the state may retire it, so a late final ping
        # from a previous sleep cannot blank the countdown of the current one.
        if slot._wait_state and slot._wait_state.get("wait_id") == wait_id:
            slot._wait_state = None
            slot._end_wait_request = None
            slot._wait_last_ping = 0.0
            slot._wait_steer_baseline = 0.0
            state.push_slots_update()
        return
    # ── Ambiguous-identity guard ──
    # `_resolve_session_key()` answers per RUNTIME, not per ACP session: with the
    # MCP gateway disabled (the default) KIROCREW_SESSION_KEY is unset and one MCP
    # process serves the whole runtime, so a subagent's `wait` and its parent's
    # resolve to the SAME session key and land on this one slot. Taking the newer
    # wait over would then attribute one sleep's countdown to the other's pill,
    # and worse, hand the user's End-wait click to whichever sleep polled next.
    #
    # The ping doubles as a heartbeat, which makes the ambiguity detectable: a
    # second wait_id arriving while the incumbent is still pinging means two
    # sleeps genuinely share this slot. There is no way to tell which one the
    # user is looking at, so track neither, and stay that way for the rest of the
    # turn (see the latch below -- a self-expiring window flapped the hole back
    # open). Deliberately a containment, not a cure: the cure is per-session
    # identity, which this cannot synthesize.
    # Tracked in https://github.com/kirodotdev/KiroCrew/issues/2347, which also
    # lists this guard among the things to delete once identity is fixed.
    if slot._wait_contested:
        # Latched for the REST OF THE TURN, not for a fixed window. An expiring
        # window reopened the hole it was built to close: both sleeps keep
        # pinging, so on expiry whichever pinged first re-minted state, and for
        # up to one ping interval that wait's id and deadline were published and
        # painted onto the OTHER one's pill -- with a live button that would end
        # the wrong sleep. Re-detection closed it again a ping later, so the
        # attribution flapped open every window instead of staying shut.
        if slot._wait_state is not None or slot._end_wait_request is not None:
            slot._wait_state = None
            slot._end_wait_request = None
            slot._wait_last_ping = 0.0
            slot._wait_steer_baseline = 0.0
            state.push_slots_update()
        return
    prev = slot._wait_state
    if prev and prev.get("wait_id") != wait_id:
        if now - slot._wait_last_ping < window:
            slot._wait_contested = True
            slot._wait_state = None
            slot._end_wait_request = None
            slot._wait_last_ping = 0.0
            slot._wait_steer_baseline = 0.0
            logger.info("two concurrent waits share session %s; countdown suppressed", session_key)
            state.push_slots_update()
            return
        # The incumbent stopped pinging: its sleep is over (a missed wait_done, a
        # killed MCP process, a hard stop). Safe to hand the slot to this one.
    if not prev or prev.get("wait_id") != wait_id:
        try:
            remaining = max(0, min(1800, int(body.get("remaining") or 0)))
            total = max(0, min(1800, int(body.get("seconds") or 0)))
        except (TypeError, ValueError):
            remaining, total = 0, 0
        slot._wait_state = {
            "wait_id": wait_id,
            "seconds": total,
            # Absolute deadline on the DASHBOARD's clock, derived once on first
            # sight from the tool's own remaining budget. Two reasons not to
            # recompute it every ping: the countdown would jitter by one
            # round-trip each tick, and the tool's monotonic clock has no shared
            # epoch it could send instead.
            "deadline_ts": now + remaining,
        }
        # A brand-new wait cannot inherit an end request aimed at an older one.
        slot._end_wait_request = None
        # Baseline for the steer reason: re-read on every mint, so only a steer
        # that lands after THIS sleep began can end it. No clock read here —
        # the baseline and the later comparison are two readings of the same
        # provider stamp.
        slot._wait_steer_baseline = _provider_steer_stamp(provider)
        slot._wait_last_ping = now
        state.push_slots_update()
    else:
        # Heartbeat only. Held OFF the wire payload so the deadline the browser
        # counts down against stays byte-identical between pushes.
        slot._wait_last_ping = now
    reason = _wait_end_reason(slot, wait_id, provider)
    if reason is not None:
        # Consume exactly once. Leaving it set would make the NEXT wait in this
        # session return instantly, which is the failure mode a session-scoped
        # boolean flag would have had.
        slot._end_wait_request = None
        slot._wait_state = None
        slot._wait_last_ping = 0.0
        slot._wait_steer_baseline = 0.0
        reply["end_wait"] = wait_id
        logger.info("wait ending early for %s (reason=%s)", session_key, reason)
        state.push_slots_update()


class ManagedToolPolicyUnreadable(Exception):
    """An agent's spec exists but its ``managedToolPolicy`` cannot be read.

    Distinct from "this agent has no policy": an operator may have written an
    exclusion that is unreadable from here, so the two must not share one
    answer on the wire. A caller that cannot tell them apart has to treat an
    unreadable deny as no deny, which silently widens it.
    """


# The fence probe's head: a UTF-8 BOM (3 bytes) plus the longest opening fence
# line (``---\r\n``, 5 bytes) is 8, so 64 leaves the probe nothing to judge but
# the fence -- which is the point. Never the file's length.
_FENCE_PROBE_BYTES = 64


def _read_head(fd: int, limit: int) -> tuple[bytes, bool]:
    """The first *limit* bytes of *fd*, and whether the file continues past them.

    Read with ``os.read`` straight off the descriptor, ``limit + 1`` bytes in
    all: a buffered reader would pull its own 8 KiB block to answer a 64-byte
    question, and the one extra byte is what tells a file that ENDS inside the
    head from one that was cut there -- the decode step treats a multibyte
    sequence broken at the cut as incomplete, and one broken at end-of-file as
    the parser's own decode failure.
    """
    data = b""
    while len(data) <= limit:
        chunk = os.read(fd, limit + 1 - len(data))
        if not chunk:
            break
        data += chunk
    return data[:limit], len(data) > limit


def _unc_refused(spelling: str) -> bool:
    """The Windows UNC trusted-root gate, composed as the strict spec reader composes it.

    A UNC path names a HOST: on Windows, resolving or opening one is an outbound
    SMB connection the path's author controls, so only the shares
    :func:`kiro_crew.hooks.unc_probe_allowed` names are admitted -- the two
    predicates ``hooks.validate_file_path`` and the strict spec reader both
    apply, on the spelling before the resolve and on the resolved target after
    it. Every other platform answers ``False``.
    """
    return (
        os.name == "nt" and hooks.is_unc_shape(spelling) and not hooks.unc_probe_allowed(spelling)
    )


def _plain_markdown_document(path: Path) -> bool:
    """Whether *path* is a markdown document with no OPENING frontmatter fence.

    The rule this repo already applies twice (``connections/ownership.py``,
    ``agent_discovery.agent_spec_stems``): a plain markdown file dropped into
    the agents directory -- a README, a shared prompt fragment -- is not a
    spec. It declares nothing and hides nothing, so it cannot hold any agent's
    policy.

    Deliberately NOT ``split_markdown_spec(text) is None``: that also folds in
    a document whose fence OPENS and never closes, which announced itself as a
    spec and may be a truncated real one -- the guard must keep refusing on
    those. The probe here is the opening-fence test ``split_markdown_spec``
    applies first: BOM aside, the document starts with a ``---`` line.

    The answer is ``True`` only for a document PROVEN fence-less: its head
    decodes as the UTF-8 the strict parser (``parse_agent_spec_bytes``) reads
    every spec as, and the decoded text does not open with a fence. A head that
    is not UTF-8 -- a UTF-16 or UTF-32 BOM (``0xFF``/``0xFE`` are never UTF-8
    bytes), a Latin-1 byte, a file that ends inside a multibyte sequence -- is
    ``False``: the parser could not decode it, so nothing here can say what
    fence test the parser would have applied, and a UTF-16-saved spec whose
    decoded text opens with a fence must keep the refusal rather than have its
    exclusion list skipped as prose. A multibyte character cut by the 64-byte
    bound is NOT that case: the head is decoded with ``final=False`` when the
    file continues past it, so a sequence split at the cut is held back, not
    raised. A UTF-8 BOM is stripped from the decoded text exactly as the
    parser strips it.

    The read path is the strict spec reader's own, never
    :func:`kiro_crew.hooks.validate_file_path`: that gate re-resolves the path
    on the two-worker ``mc-pathres`` pool and fails closed when the pool misses
    its budget, and this probe runs on every policy request after the strict
    reader already refused -- the exact saturation
    :func:`kiro_crew.agent_discovery.read_agent_spec_strict` was moved off
    that pool to survive, which would otherwise turn a stray ``.md`` back into
    a denial of every agent whenever the pool is busy. What this path refuses,
    and nothing else: a spelling or a resolved target that is a UNC path
    outside the trusted roots, on Windows, checked before and after the
    resolve as the strict reader checks it (:func:`_unc_refused`); a
    spelling ``Path.resolve(strict=True)`` cannot canonicalise (absent, broken
    or looping link, permission) -- a link at the name is otherwise FOLLOWED,
    as the strict reader follows it, and its target is what is judged; a
    resolved path :func:`kiro_crew.security.is_sensitive_canonical_path`
    fences, the gate that submits nothing to the pool off the event loop (this
    runs under ``asyncio.to_thread``); and, from
    :func:`kiro_crew.pinned_fs.open_fenced_for_read`, a link at the final
    component of the RESOLVED path (the re-point window between the resolve
    and the open), a hardlinked or non-regular inode, an inode whose kernel
    path cannot be read back, and an opened inode whose kernel path differs
    from the judged one and is itself fenced. Size is not judged (below), and
    no SEL row is written here: the strict reader already audited any
    sensitive-target denial for this path.

    The read is BOUNDED at ``_FENCE_PROBE_BYTES`` through :func:`_read_head`:
    the strict reader refuses an oversize file AT the cap precisely so it is
    never slurped into memory, and an unbounded re-read here would hand the
    loop an attacker-sized allocation whose ``MemoryError`` escapes every
    fail-closed arm. The fence test needs only the first bytes (a BOM plus one
    ``---`` line), so 64 is generous, and an over-cap plain document is still
    skipped: the probe judges its opening, not its length.

    Every failure -- unresolvable, a refused open, an unreadable descriptor, an
    undecodable or NUL-bearing head -- is ``False``, because the caller is
    deciding whether to SKIP a file its strict reader already refused, and a
    file that cannot even be probed is unknown, not ignorable: fail closed,
    the guard keeps raising.
    """
    if _unc_refused(str(path)):
        return False
    try:
        real = path.resolve(strict=True)
    except (OSError, RuntimeError):
        # OSError: absent, broken link, permission; RuntimeError: pathlib's
        # signal for a symlink loop on the Pythons that raise it as such.
        return False
    real_str = str(real)
    if _unc_refused(real_str) or is_sensitive_canonical_path(real_str):
        return False
    try:
        fd = open_fenced_for_read(real, fence=is_sensitive_canonical_path)
    except OSError:
        return False
    try:
        head, truncated = _read_head(fd, _FENCE_PROBE_BYTES)
    except OSError:
        return False
    finally:
        os.close(fd)
    try:
        text = codecs.getincrementaldecoder("utf-8")().decode(head, final=not truncated)
    except UnicodeDecodeError:
        return False
    if "\x00" in text:
        return False
    if text.startswith("\ufeff"):
        text = text[1:]
    return not text.startswith(("---\n", "---\r\n"))


def _spec_failure_kind(path: Path, exc: BaseException) -> str:
    """WHY *path* failed the strict read, in plain words and with NO path in it.

    Every answer is a PREDICATE phrase -- it reads after "``<file>`` is" and
    after "could not be read (" alike -- because two templates splice it in.

    The reason this feeds crosses the wire to the MCP client and lands in the
    model-visible refusal text, so it names the failure class rather than
    quoting ``str(exc)``: the strict reader's own messages carry the full
    path (``f"{path}: {exc}"``, the AppleDouble and size-cap arms) and an
    ``OSError`` carries ``filename``. The classes are the ones
    :func:`kiro_crew.agent_discovery.read_agent_spec_strict` documents; an
    unfamiliar one still gets a usable name from its class.
    """
    if path.name.startswith("._"):
        return "an AppleDouble sidecar, not a spec"
    if isinstance(exc, json.JSONDecodeError):
        return "not valid JSON"
    if isinstance(exc, UnicodeDecodeError):
        return "not UTF-8 text"
    if isinstance(exc, SensitiveAgentSpecPathError):
        return "a path the spec reader refuses"
    if isinstance(exc, OSError) and isinstance(exc.__cause__, _SpecReadRefused):
        # The strict reader maps the pinned open's refusal to a generic
        # ``EACCES``; its ``strerror`` is the reader's own placeholder, which
        # would only restate "could not be read". The cause says why.
        return (
            "not a plain readable file: a link at its name, a hardlinked or "
            "non-regular inode, or a target the spec reader fences"
        )
    if isinstance(exc, OSError):
        # ``strerror`` is the C library's text ("Permission denied"); the path
        # lives in ``filename`` and is deliberately left out.
        return f"unreadable ({exc.strerror or exc.__class__.__name__})"
    if isinstance(exc.__cause__, hooks.FileTooLargeError):
        return "larger than the spec size cap"
    if is_markdown_spec(path):
        # No causal clause: the parser refuses a closed fence too (frontmatter
        # that is a list, nested too deeply, a bare ``on:`` key YAML reads as a
        # bool), so naming one cause would send the operator to check a fence
        # that is closed. The strict reader's own messages for this family are
        # path-free, but not every ValueError reaching here is, so the class is
        # named rather than the text quoted.
        return "markdown frontmatter the spec parser refuses"
    return f"not a spec ({exc.__class__.__name__})"


def _ambiguous_spec_reason(agent_name: str, exc: AmbiguousAgentSpecError) -> str:
    """The wire ``reason`` for two specs declaring *agent_name*: names, not paths.

    The exception's own message quotes each file's full path for the terminal
    it was written for; this text reaches the MCP client's model-visible
    refusal, so it carries the same files by name only (``repr``'d, as
    untrusted input from a user-writable directory) and the remedy. Falls back
    to the message when the raiser supplied no paths -- the one other raiser,
    ``agent.agent_spec_path``, is never reached from here.
    """
    if not exc.paths:
        return str(exc)
    names = ", ".join(repr(path.name) for path in exc.paths)
    return (
        f"{len(exc.paths)} specs in the agents directory declare the name {agent_name!r}: "
        f"{names}. Which one is live is undefined, so the policy for {agent_name!r} is "
        f"unknown. Remove or rename one of them in the agents directory (~/.kiro/agents "
        f"unless relocated); no restart needed."
    )


def _unreadable_spec_remedy(path: Path) -> str:
    """The one sentence an operator can act on, appended to every refusal.

    The filename is ``repr``'d: it is untrusted input from a user-writable,
    tool-shared directory and this text reaches a terminal and the model. The
    directory is named by role, not by path -- the reason is client-visible.
    """
    return (
        f"Move or fix {path.name!r} in the agents directory (~/.kiro/agents unless "
        f"relocated); no restart needed."
    )


# ``(path digest, mtime_ns)`` pairs already warned about. The client re-asks
# for its policy on every ``tools/call`` and a refusal is never memoized, so
# without this the gateway log would carry one WARNING per refused tool call
# for as long as the file stays broken. A fix or a re-break changes the mtime
# and is logged again. Bounded in BOTH dimensions: the entry count is capped
# (clearing costs one repeated line, nothing else), and each entry retains a
# fixed-size SHA-256 digest of the path rather than the path itself, so the
# cap bounds the bytes held and not only the number of items -- the path is
# needed once, for the log line, and never read back out of here.
_UNREADABLE_SPEC_WARNED: set[tuple[bytes, int]] = set()
_UNREADABLE_SPEC_WARNED_MAX = 1024


def _warn_unreadable_spec_once(path: Path, kind: str) -> None:
    """Name *path* in the gateway log, once per on-disk revision of it.

    The FULL path goes here, ``%r``'d: the gateway log is local to the
    operator, and it is the one place the reason on the wire (name only) can be
    joined back to a location. The SEL row the caller writes per request
    carries the wire reason, so the audit trail is complete without this line;
    this is for the operator tailing the log.
    """
    try:
        revision = path.stat().st_mtime_ns
    except OSError:
        revision = -1
    key = (hashlib.sha256(str(path).encode("utf-8", "surrogateescape")).digest(), revision)
    if key in _UNREADABLE_SPEC_WARNED:
        return
    if len(_UNREADABLE_SPEC_WARNED) >= _UNREADABLE_SPEC_WARNED_MAX:
        _UNREADABLE_SPEC_WARNED.clear()
    _UNREADABLE_SPEC_WARNED.add(key)
    # The filename is untrusted input from a user-writable directory and this
    # line persists in gateway.log: the same log-egress redaction every other
    # operational line carrying foreign text applies (``redact_log_via_context``,
    # the non-raising spelling for a log site), so a credential-shaped name is
    # scrubbed before it is written. ``%r`` still escapes control bytes.
    logger.warning(
        "agent spec %r could not be read (%s); every session whose agent has no "
        "spec of its own is refused its managed tools until it is moved or fixed",
        redact_log_via_context(str(path)),
        kind,
    )


def _refuse_if_any_spec_is_unreadable(agents_dir: Path, agent_name: str) -> None:
    """Raise when a spec in *agents_dir* cannot be read, so "no match" is honest.

    :func:`spec_by_declared_name` resolves an agent by parsing every spec and
    comparing its declared ``name``. Its reader folds each refusal into "not a
    usable spec", which a scan for a name cannot tell apart from "a spec for
    someone else" -- so an unparseable file leaves the scan reporting no match
    when the policy it was looking for may be inside that very file.

    A file's declared name cannot be recovered without reading it, and its
    filename does not have to carry it, so there is no sound way to narrow this
    to "specs that could be *agent_name*". The honest answer while any spec is
    unreadable is that this agent's policy is unknown. Fixing or removing the
    file clears it, and the refusal is audited by the caller.

    One exception, taken only after the strict read already refused: a markdown
    file with no opening frontmatter fence is not a spec at all (see
    :func:`_plain_markdown_document`), so it is skipped rather than allowed to
    deny every agent that has no spec of its own. A FENCED document that fails
    to parse still raises: its declared name is unrecoverable, so the policy
    stays unknown.

    Uses :func:`read_agent_spec_strict`, the reader that keeps the failure class,
    for exactly the reason its docstring gives: this caller needs to know WHY --
    and the refusal says WHICH: the message names the file (name only, the
    directory is the caller's) and the failure kind in words, then what to do.
    The verdict is unchanged by that; only its text is. Without the name, the
    operator told to "fix or remove the unreadable spec" had to validate every
    file in the directory by hand to find it.
    """
    for path in iter_agent_spec_files(agents_dir):
        try:
            read_agent_spec_strict(path, operation="session_tool_policy", source="dashboard")
        except (OSError, ValueError) as exc:
            if is_markdown_spec(path) and _plain_markdown_document(path):
                # Not a spec (no opening fence): it cannot declare a policy,
                # so it must not turn into a denial of every other agent.
                continue
            kind = _spec_failure_kind(path, exc)
            _warn_unreadable_spec_once(path, kind)
            raise ManagedToolPolicyUnreadable(
                f"agent spec {path.name!r} in the agents directory could not be read "
                f"({kind}), so the policy for {agent_name!r} is unknown: it may be "
                f"the file that declares it. {_unreadable_spec_remedy(path)}"
            ) from exc


# Resolved policies keyed by agents directory and agent name, each pinned to
# the stat-only revision of the directory they were read under. The read below
# parses EVERY spec in the directory to find one declared name, and refuses
# only after a second strict pass over all of them; a managed MCP server asks
# for its policy on ordinary request traffic, so with a couple of thousand
# installed specs each request costs the worker thread most of a second of
# GIL-holding path validation and JSON parsing. An unchanged directory answers
# from here for the price of one ``scandir``. Its own instance, not shared with
# the KAS projection's: the two reads carry different SEL ``operation`` labels.
_TOOL_POLICY_MEMO: AgentsDirMemo[dict[str, Any] | None] = AgentsDirMemo()


def _read_managed_tool_policy_sync(agents_dir: Path, agent_name: str) -> dict[str, Any] | None:
    """:func:`_read_managed_tool_policy_uncached`, answered from the memo while
    *agents_dir* is unchanged. Blocking; runs on a worker like the read it wraps.

    Only a resolved answer -- a policy, or ``None`` for an agent that has none
    -- is memoized. A refusal (:class:`ManagedToolPolicyUnreadable`,
    :class:`~kiro_crew.agent_discovery.AmbiguousAgentSpecError`) propagates out
    of :class:`~kiro_crew.agent_discovery.AgentsDirMemo` unstored and so is
    re-derived on every call: it names an operator error the caller audits per
    request, and the state it reports is the one a fix to the directory clears.
    The revision and store rules are the memo's; see its docstring.
    """
    return _TOOL_POLICY_MEMO.get(
        agents_dir, agent_name, lambda: _read_managed_tool_policy_uncached(agents_dir, agent_name)
    )


def _read_managed_tool_policy_uncached(agents_dir: Path, agent_name: str) -> dict[str, Any] | None:
    """Read one agent's ``managedToolPolicy`` from disk. Blocking.

    Split out so the whole filesystem transaction -- the existence probe, the
    read and the JSON parse -- crosses to a worker as ONE unit. Offloading only
    the read would leave the ``stat`` and the parse on the gateway's single event
    loop, which is the same defect in a smaller form.

    The spec is resolved by its declared ``name`` first, through
    :func:`kiro_crew.agent_discovery.spec_by_declared_name`, and
    ``<agent_name>.json`` is read only when no spec declares the name -- the
    same order the KAS projection uses to start the session, so the policy
    handed to a session's MCP servers is the policy of the spec that session
    runs. A package-installed agent is namespaced on disk as
    ``<package>-<name>.json`` and dispatchable under its bare name, so a
    filename-only read here would hand exactly that agent an empty policy;
    and a misnamed ``<agent_name>.json`` declaring some other agent must not
    hand this one that agent's policy. The scan reads specs it did not name in
    a user-writable directory, so it goes through the hardened reader,
    labelled ``session_tool_policy`` / ``dashboard``.

    ``None`` means "this agent has no policy to report" -- no spec file, a spec
    that declares none, or a fence-less ``<agent_name>.md`` in the filename slot,
    which is a prose document and not a spec (:func:`_plain_markdown_document`).
    The caller answers ``{}`` for it, without a SEL ``ok`` record when nothing
    was parsed.

    Raises :class:`ManagedToolPolicyUnreadable` when a spec EXISTS but its
    policy cannot be determined (unparseable, valid JSON that is not an object,
    or a ``managedToolPolicy`` of the wrong shape). That is not "no policy": the
    operator may have written an exclusion this cannot see, so the caller must
    not answer it with the same empty body a policy-free agent gets.

    Propagates :class:`kiro_crew.agent_discovery.AmbiguousAgentSpecError` when
    two specs declare *agent_name*: that is not "no policy" either, and the
    caller records it as a denial rather than answering it silently.
    """
    # The file the policy was read from, when it was a direct-filename read.
    # The declared-name scan returns a parse and not a path (see
    # ``spec_by_declared_name``: a path to reopen would put a second read
    # outside the guards), so a shape refusal on ITS result names the agent
    # only -- which identifies the spec, since exactly one declares that name.
    spec_path: Path | None = None
    try:
        config: Any = spec_by_declared_name(
            agents_dir, agent_name, operation="session_tool_policy", source="dashboard"
        )
        if config is None:
            # ``<name>.json`` then ``<name>.md``: beside a twin the JSON wins,
            # the order every direct-filename fallback uses (see
            # ``kas_agents.load_agent_spec``).
            present = [p for p in agent_spec_candidates(agents_dir, agent_name) if p.is_file()]
            if not present:
                # No spec matched by declared name and none by filename. That is
                # "no policy" only if every spec in the directory was READABLE:
                # the scan above resolves a name by parsing each file, and its
                # reader folds a refusal into "no match", so an unparseable spec
                # is indistinguishable from one declaring a different name. A
                # package-installed agent is namespaced on disk
                # (``<package>-<name>.json``) and has no bare filename
                # candidate, so the scan is the ONLY thing that could have found
                # its policy -- and a file this cannot read may be exactly it.
                _refuse_if_any_spec_is_unreadable(agents_dir, agent_name)
                return None
            spec_path = present[0]
            # The hardened reader: the agents directory is user-writable, so
            # a symlink here is not followed to a sensitive target.
            try:
                config = read_agent_spec_strict(
                    spec_path, operation="session_tool_policy", source="dashboard"
                )
            except (OSError, ValueError):
                if not (is_markdown_spec(spec_path) and _plain_markdown_document(spec_path)):
                    raise
                # ``<agent_name>.md`` with no opening fence and no JSON twin is
                # not this agent's spec: it is a prose document sharing the
                # name (see ``_plain_markdown_document``), so it cannot hold a
                # policy any more than a stray ``README.md`` can. The same
                # not-a-spec rule the enumeration guard applies, at the one
                # other place a fence-less file is parsed as a spec -- and the
                # same disposition as no candidate at all. A fenced document
                # that fails to parse re-raised above: it announced itself as
                # a spec, so its policy stays unknown.
                _refuse_if_any_spec_is_unreadable(agents_dir, agent_name)
                return None
    except AmbiguousAgentSpecError:
        # A ``ValueError`` subclass, so it is named BEFORE the parse-failure arm
        # below or it would be swallowed as "no policy" instead of propagating.
        raise
    except (OSError, ValueError) as exc:
        # The file is there and could not be read or parsed. Whatever exclusions
        # it declares are unknown, so this is reported as unknown. The prefix is
        # the one an earlier reader of this arm matches on; the name and kind
        # follow it. ``spec_path`` is unset only when the directory WALK itself
        # raised (``spec_by_declared_name`` and ``iter_agent_spec_files`` both
        # propagate the glob's ``OSError``): there is no file to name, so that
        # arm keeps its class-name-only text.
        if spec_path is None:
            raise ManagedToolPolicyUnreadable(
                f"agent spec for {agent_name!r} could not be read: {exc.__class__.__name__}"
            ) from exc
        kind = _spec_failure_kind(spec_path, exc)
        _warn_unreadable_spec_once(spec_path, kind)
        raise ManagedToolPolicyUnreadable(
            f"agent spec for {agent_name!r} could not be read: {spec_path.name!r} is "
            f"{kind}. {_unreadable_spec_remedy(spec_path)}"
        ) from exc
    if not isinstance(config, dict):
        # Valid JSON that is not an object (a list, a scalar, null) parses
        # fine, but `.get` on it would raise. It is a malformed spec, so it
        # takes the same disposition as the unparseable case above. Only the
        # direct read lands here -- the scan matches ``dict`` specs only -- so
        # ``spec_path`` is set; the bare form is kept for the type checker.
        if spec_path is None:
            raise ManagedToolPolicyUnreadable(
                f"agent spec for {agent_name!r} is valid JSON but not an object"
            )
        raise ManagedToolPolicyUnreadable(
            f"agent spec for {agent_name!r} could not be read: {spec_path.name!r} is "
            f"valid JSON but not an object. {_unreadable_spec_remedy(spec_path)}"
        )
    policy = config.get("managedToolPolicy", {})
    if isinstance(policy, dict):
        return policy
    # A policy of the wrong shape is a policy this cannot read, not an absent
    # one: the operator wrote something here and its meaning is unknown.
    if spec_path is None:
        raise ManagedToolPolicyUnreadable(
            f"managedToolPolicy for {agent_name!r} is {type(policy).__name__}, not an "
            f"object. Fix the spec declaring name {agent_name!r} in the agents "
            f"directory (~/.kiro/agents unless relocated); no restart needed."
        )
    raise ManagedToolPolicyUnreadable(
        f"managedToolPolicy for {agent_name!r} in {spec_path.name!r} is "
        f"{type(policy).__name__}, not an object. {_unreadable_spec_remedy(spec_path)}"
    )


async def api_session_tool_policy(request: web.Request) -> web.Response:
    """GET /api/session-tool-policy — return managedToolPolicy for the
    calling session's agent.

    Used by managed MCP servers (kirocrew-core, kirocrew-cron) to filter
    their tool lists per-agent.  Returns {"exclude": [...]} on success,
    or 400/404 when the session cannot be identified (deny-by-default:
    callers that cannot prove identity get an error, not an empty policy).
    Authenticated via X-Internal-Secret + X-Session-Key.
    """
    _, refusal = await internal_memory_scope(
        request, "api_session_tool_policy", claimed_session=request.headers.get("X-Session-Key", "")
    )
    if refusal is not None:
        return refusal
    state: DashboardState = request.app["state"]
    session_key = request.headers.get("X-Session-Key", "").strip()
    if not session_key:
        _sel().log_api_access(
            caller="unknown",
            operation="session_tool_policy",
            outcome="denied",
            source="dashboard",
            resources="missing X-Session-Key",
        )
        return web.json_response({"error": "X-Session-Key required"}, status=400)

    # Resolve agent name from session
    agent_name = ""

    # Dashboard slot
    if session_key.startswith("dashboard:"):
        slot_key = session_key[len("dashboard:") :]
        slot = state.get_slot(slot_key)
        if slot:
            agent_name = slot.agent
    # Subagent — look up in SubagentManager
    elif session_key.startswith("subagent:"):
        if state.subagents:
            subagent_id = session_key[len("subagent:") :]
            info = state.subagents.get(subagent_id)
            if info:
                agent_name = info.agent
    # Cron — fall through to session manager lookup below
    elif session_key.startswith("cron:"):
        pass

    # Also check session manager for agent name
    if not agent_name and state.sessions:
        agent_name = state.sessions.get_agent(session_key)

    if not agent_name:
        _sel().log_api_access(
            caller=session_key,
            operation="session_tool_policy",
            outcome="denied",
            source="dashboard",
            resources="agent not resolved",
        )
        return web.json_response({"error": "agent not resolved"}, status=404)

    # Sanitize agent_name to prevent path traversal
    if "/" in agent_name or "\\" in agent_name or ".." in agent_name:
        _sel().log_api_access(
            caller=session_key,
            operation="session_tool_policy",
            outcome="denied",
            source="dashboard",
            resources=f"invalid agent_name={agent_name!r}",
        )
        return web.json_response({"error": "invalid agent name"}, status=400)

    # Read agent config from disk, OFF the event loop. A managed MCP server
    # calls this to filter its tool list, so it runs on ordinary request traffic
    # rather than at startup: the stat, the read and the parse would otherwise
    # execute on the single loop every gateway request shares.
    try:
        policy = await asyncio.to_thread(
            _read_managed_tool_policy_sync, kiro_agents_dir(), agent_name
        )
    except AmbiguousAgentSpecError as exc:
        # Two specs declare this agent's name. The policy is undefined, not
        # empty, so it is answered with a status the caller cannot mistake for
        # a policy-free agent, and recorded as a denial naming both files. The
        # SEL row keeps the exception's own message, full paths included --
        # the audit trail is local. The wire ``reason`` names the files
        # WITHOUT their directory, in the shape every other refusal here takes:
        # it reaches the MCP client's model-visible error text, and a full
        # path there discloses the account name and on-disk layout.
        _sel().log_api_access(
            caller=session_key,
            operation="session_tool_policy",
            outcome="denied",
            source="dashboard",
            resources=f"agent={agent_name}",
            error=str(exc),
        )
        return web.json_response(
            {
                "error": f"The policy for agent {agent_name!r} could not be determined.",
                "code": "policy_unreadable",
                "reason": _ambiguous_spec_reason(agent_name, exc),
            },
            status=409,
        )
    except ManagedToolPolicyUnreadable as exc:
        # The spec is present and its policy could not be determined. Answering
        # {} here would be byte-identical to an agent that legitimately has no
        # policy, and the caller would run a tool this cannot prove is allowed.
        _sel().log_api_access(
            caller=session_key,
            operation="session_tool_policy",
            outcome="denied",
            source="dashboard",
            resources=f"agent={agent_name}",
            error=str(exc),
        )
        return web.json_response(
            {
                "error": f"The policy for agent {agent_name!r} could not be determined.",
                "code": "policy_unreadable",
                "reason": str(exc),
            },
            status=409,
        )
    if policy is None:
        # This agent has no policy: no spec file, or a spec declaring none.
        # A genuinely absent policy is an empty one, so the answer is unchanged
        # -- and still not logged as a success, since nothing was parsed.
        return web.json_response({})

    _sel().log_api_access(
        caller=session_key,
        operation="session_tool_policy",
        outcome="ok",
        source="dashboard",
        resources=f"agent={agent_name}",
    )
    return web.json_response(policy)


async def _reset_all_sessions(request: web.Request) -> int:
    """Reset all active sessions so they pick up config changes.

    Reloads provider factory (handles provider switch ACP→CC or vice versa),
    shuts down all active sessions AND drains the warm pool (pre-spawned
    processes loaded the old MCP config at spawn time).
    New sessions cold-start on next message.
    Returns the number of sessions reset.
    """
    state: DashboardState = request.app["state"]
    sessions = state.sessions

    # Reload factory so provider switch takes effect immediately
    await sessions.reload_provider_factory()

    # Pop all active sessions
    providers: list[LLMProvider] = []
    count = sessions.count
    if count > 0:
        providers = await sessions.drain_all_providers()

    # Drain warm pool — pre-spawned processes have stale MCP config
    pool_providers = await sessions.drain_warm_pool()
    providers.extend(pool_providers)

    if count > 0 or pool_providers:
        logger.info(
            "Reset %d session(s) + %d pool process(es) after config change",
            count,
            len(pool_providers),
        )

    state.broadcast_ws("sessions_restarting", {"status": "restarting"})

    async def _background_restart() -> None:
        if providers:

            async def _safe_shutdown(p: LLMProvider) -> None:
                _timeout = _SHUTDOWN_TIMEOUT_SECS
                try:
                    await asyncio.wait_for(p.shutdown(), timeout=_timeout)
                except asyncio.TimeoutError:
                    logger.warning(
                        "Session shutdown hung past %.1fs; forcing kill",
                        _timeout,
                    )
                    try:
                        # The kill signals the provider's whole process group and
                        # then waits out a bounded SIGTERM grace, so it blocks for
                        # as long as that grace -- never inline on the event loop
                        # (AUTOSDE: no-blocking-call-on-event-loop), which is what
                        # every other caller of it already avoids. Awaited so the
                        # tree is reaped before ``start_pool`` below spawns its
                        # replacements, and concurrent across the ``gather``, so N
                        # hung providers cost one grace rather than N in series.
                        await asyncio.get_running_loop().run_in_executor(
                            subprocess_executor(), _h._sync_kill_provider, p
                        )
                    except RuntimeError:
                        # The executor is already shut down -- a gateway teardown
                        # racing this restart. Run the kill on a plain daemon
                        # thread instead: still off the loop, and far better than
                        # skipping it, which is what leaks the tree.
                        threading.Thread(
                            target=_h._sync_kill_provider, args=(p,), daemon=True
                        ).start()
                    except Exception:
                        logger.exception("Force-kill fallback also failed for %r", p)
                except Exception:
                    pass

            await asyncio.gather(*[_safe_shutdown(p) for p in providers])

        sessions._pool_started = False
        await sessions.start_pool(blocking=False)
        logger.info("Background session restarted")
        state.push_refresh("agents")
        state.push_slots_update()
        state.broadcast_ws("sessions_restarting", {"status": "ready"})

    task = asyncio.create_task(_background_restart())
    state._background_tasks.add(task)
    task.add_done_callback(state._background_tasks.discard)

    return count


async def api_sessions_restart(request: web.Request) -> web.Response:
    """POST /api/sessions/restart — reset all kiro-cli sessions.

    Forces fresh context injection on the next message. Use after editing
    memory, lessons, or skills to pick up changes immediately.

    Also syncs MCP servers from mcp.json → kirocrew.json so newly
    installed servers (e.g. via AIM) are picked up on restart.
    """
    # Sync MCP servers before restarting so new installs take effect.
    # Run in thread — the sync does blocking file I/O. Cap at 30s so a hung
    # rebuild doesn't stall the restart. sync_discovered_servers serializes
    # against the /api/mcp/sync handler's run of the same sequence.
    synced = 0
    sync_ok = True
    try:
        to_sync = await asyncio.wait_for(asyncio.to_thread(sync_discovered_servers), timeout=30)
        synced = len(to_sync)
    except Exception:
        # The restart still proceeds (it applies whatever IS on disk), but the
        # response says the reconcile failed rather than reporting a success
        # the on-disk config does not back.
        sync_ok = False
        logger.warning("MCP server sync failed before restart", exc_info=True)
    count = await _reset_all_sessions(request)
    return web.json_response(
        {"ok": True, "sessions_reset": count, "mcp_synced": synced, "mcp_sync_ok": sync_ok}
    )


async def api_session_archive_list(request: web.Request) -> web.Response:
    """GET /api/session/archive?key=... — list archive files for a session key."""
    from typing import Any

    from kiro_crew.history import _archive_dir, _safe_key

    key = request.query.get("key", "").strip()
    adir = _archive_dir()
    if not adir.exists():
        return web.json_response({"archives": []})
    prefix = f"{_safe_key(key)}__" if key else ""

    def _collect() -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for p in adir.glob(f"{prefix}*.jsonl"):
            try:
                st = p.stat()
            except OSError:
                continue
            stem = p.stem
            # Archive filenames use '__' delimiter: {safekey}__{stamp}.jsonl
            sep = stem.find("__")
            safekey = stem[:sep] if sep >= 0 else stem
            stamp = stem[sep + 2 :] if sep >= 0 else ""
            items.append(
                {
                    "name": p.name,
                    "key": safekey,
                    "stamp": stamp,
                    "size": st.st_size,
                    "mtime": st.st_mtime,
                }
            )
        items.sort(key=lambda x: x["mtime"], reverse=True)
        return items

    items = await asyncio.to_thread(_collect)
    return web.json_response({"archives": items})


async def api_session_archive_read(request: web.Request) -> web.Response:
    """GET /api/session/archive/{name} — read a single archive file as JSONL text."""
    name = request.match_info.get("name", "")
    if not name.endswith(".jsonl"):
        return web.json_response({"error": "invalid archive name"}, status=400)
    adir = _archive_dir().resolve()
    try:
        resolved = (adir / name).resolve()
    except (OSError, RuntimeError, ValueError):
        return web.json_response({"error": "invalid archive name"}, status=400)
    # Canonical path check: file must be a direct child of the archive dir.
    if resolved.parent != adir:
        return web.json_response({"error": "invalid archive name"}, status=400)

    def _read_capped(p: Path, limit: int = 250_000) -> str:
        with p.open(encoding="utf-8") as f:
            data = f.read(limit)
        # Truncate at last newline to keep NDJSON valid.
        if len(data) == limit:
            nl = data.rfind("\n")
            if nl > 0:
                data = data[: nl + 1]
        return data

    try:
        raw = await asyncio.to_thread(_read_capped, resolved)
    except FileNotFoundError:
        return web.json_response({"error": "not found"}, status=404)
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning("Failed to read archive %s: %s", name, exc)
        return web.json_response({"error": "unreadable archive"}, status=422)
    # Archives contain LLM output; redact credentials and exfiltration URLs before serving.
    redacted = await asyncio.to_thread(lambda: redact(raw))
    return web.Response(text=redacted, content_type="application/x-ndjson")


# Every ``api_session*`` handler is an owner surface, so a private member's
# internal call is refused before it runs (audit label = handler name). These
# three verify and scope their own caller instead.
guard_owner_surface_routes(
    globals(),
    prefix="api_session",
    member_scoped=frozenset(
        {"api_session_directive", "api_session_keepalive", "api_session_tool_policy"}
    ),
)
