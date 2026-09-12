"""Session lifecycle, usage, search, approvals, and reset handlers."""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import re
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Collection

from kiro_crew.loop_lock import LoopBoundLock

if TYPE_CHECKING:
    from kiro_crew.providers.base import LLMProvider  # noqa: F811

from aiohttp import web

# circular import: handlers/__init__.py re-exports this module's handlers, so
# a `from ... import` of individual names would fail mid-cycle. `import ... as`
# binds via sys.modules and defers attribute access to call time, which also
# keeps tests' monkeypatching of handlers.redact_* effective (late binding).
import kiro_crew.dashboard.handlers as _h
from kiro_crew import session_directive
from kiro_crew.acp.client import _resolve_kiro_bin_for_spawn

# The migration module owns the pre-migration leftover-tab spelling.
from kiro_crew.channel_transcript_migration import _orphan_target_stem
from kiro_crew.config.paths import kiro_agents_dir
from kiro_crew.cron import CronStoreBusy, CronStoreUnreadable, cron_owner_matches
from kiro_crew.dashboard import directive_queue
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
from kiro_crew.messaging.link import canonical_key
from kiro_crew.sandbox import (
    cgroup_scope_argv,
    configured_sandbox_mode,
    create_subprocess_limited,
    scrub_agent_subprocess_env,
    wrap_argv,
)
from kiro_crew.security import redact, redact_credentials, redact_exfiltration_urls
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


_health_cache: dict[str, dict] = {}
_health_cache_ts: float = 0.0
_health_lock = LoopBoundLock()
_HEALTH_REFRESH_SECS = 15


async def api_sessions_health(request: web.Request) -> web.Response:
    """GET /api/sessions/health — slots flagged as stalled from log scan."""
    global _health_cache, _health_cache_ts
    now = time.monotonic()
    if now - _health_cache_ts > _HEALTH_REFRESH_SECS:
        async with _health_lock:
            # Re-check after acquiring lock (another request may have refreshed)
            if time.monotonic() - _health_cache_ts > _HEALTH_REFRESH_SECS:
                try:
                    from kiro_crew.dashboard import session_health

                    _health_cache = await asyncio.to_thread(session_health.compute_session_health)
                    _health_cache_ts = time.monotonic()
                except Exception:
                    logger.warning("session_health scan failed", exc_info=True)
                    _health_cache_ts = time.monotonic()
    return web.json_response({"stalled": _health_cache})


_usage_cache: dict[str, object] = {}
_usage_cache_ts: float = 0.0
_USAGE_REFRESH_SECS = 600  # background refresh every 10 min
# Ceiling on ONE whole refresh. Sized above the sum of the inner bounded steps
# (whoami ≤30s + the billed scrape ≤60s, plus the unbounded API read between
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

# --- Text-scrape gate ------------------------------------------------------
# The `/usage` text scrape is a REAL billed kiro-cli chat turn, unlike the
# GetUsageLimits API read the primary path uses. It runs on a timer for as long
# as a dashboard tab is open, so an ungated fallback bills the user forever just
# to render a credit meter. Hence: opt-in via config, logged once when it is
# skipped, and backed off when it repeatedly fails.

#: True once the "scrape is disabled" notice has been logged. The refresh runs
#: every _USAGE_REFRESH_SECS forever, so logging per cycle would fill the log
#: with a message that never changes.
_usage_scrape_disabled_logged = False
#: Consecutive scrape attempts that produced no usable credit plan.
_usage_scrape_failures = 0
#: monotonic deadline before which no further scrape is attempted.
_usage_scrape_backoff_until = 0.0
#: Consecutive failures tolerated before the scrape is parked. Two refresh
#: intervals of bad luck stay within normal retry; a third means the scrape is
#: broken (kiro-cli format change, wedged CLI, revoked auth), and every further
#: attempt spends credits for output that cannot be parsed.
_USAGE_SCRAPE_FAILURE_THRESHOLD = 3
#: How long a broken scrape is parked. Long relative to the 10-minute refresh so
#: a persistent breakage costs a handful of turns per day, not one per interval.
_USAGE_SCRAPE_BACKOFF_SECS = 6 * 3600


def _text_scrape_enabled() -> bool:
    """True when the user has opted in to the credit-spending `/usage` scrape.

    Fails CLOSED: any error reading config means the scrape does not run, so a
    malformed config can never silently start billing chat turns. Blocking I/O
    (stat + parse), so callers offload it.
    """
    try:
        from kiro_crew.config.loader import KiroCrewConfig

        return bool(KiroCrewConfig.load().dashboard.usage_text_scrape_enabled)
    except Exception:
        logger.debug("usage text-scrape gate unreadable; treating as disabled", exc_info=True)
        return False


def _log_scrape_disabled_once() -> None:
    """Announce the skipped scrape exactly once per process."""
    global _usage_scrape_disabled_logged
    if _usage_scrape_disabled_logged:
        return
    _usage_scrape_disabled_logged = True
    logger.info(
        "Kiro usage: the API returned no credit plan and the /usage text scrape "
        "is disabled, so the credit pill stays unavailable. The scrape is a "
        "billed kiro-cli chat turn every %ds; enable it with "
        "dashboard.usage_text_scrape_enabled = true in config.json if you want "
        "to pay for the readout.",
        _USAGE_REFRESH_SECS,
    )


def _scrape_in_backoff() -> bool:
    """True while a repeatedly-failing scrape is parked."""
    return time.monotonic() < _usage_scrape_backoff_until


def _record_scrape_outcome(success: bool) -> None:
    """Track consecutive scrape failures and park the scrape once they pile up.

    Every attempt costs credits, so a scrape that cannot produce a usable plan
    must stop retrying on each TTL expiry. Any success clears the counter, so a
    transient hiccup does not accumulate toward the ceiling.
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
            "plan; pausing the scrape for %ds so it stops spending credits on "
            "unusable output.",
            _usage_scrape_failures,
            _USAGE_SCRAPE_BACKOFF_SECS,
        )


def _cache_without_scrape(
    api_usage: object, identity: dict[str, object], reason: str | None = None
) -> None:
    """Cache the best available value when the scrape is not going to run.

    Degrades rather than erroring: keep a previously-good value (dimmed
    ``stale``) so the pill does not blink out, otherwise surface whatever
    partial fields the API did return alongside ``available: False`` — the
    frontend's existing signal to hide the pill instead of rendering blanks.
    ``reason`` (when given) rides that unavailable marker so the frontend can
    explain WHY instead of hiding silently: the opt-in scrape being off is a
    permanent, user-addressable state, unlike a cold-start failure, and hiding
    it leaves users with no hint that a knob exists.

    Preserving is gated on ``_same_identity``: with the scrape disabled, a
    plan-less API answer recurs every refresh forever, so an unguarded preserve
    would serve the PREVIOUS account's balance and email indefinitely after a
    switch A->B. An unproven identity (missing or mismatched email / start_url,
    including an account that never carried one) therefore reports unavailable
    instead — hiding the pill is a cosmetic loss, attributing one account's
    spend to another is not.

    ``identity`` is this refresh's whoami, resolved before the API attempt, so a
    switch landing inside that attempt is caught on the following refresh rather
    than this one.
    """
    global _usage_cache, _usage_cache_ts
    if _usage_cache.get("credits_plan") is not None and _same_identity(_usage_cache, identity):
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


def _cache_transient_failure() -> None:
    """Record a transient usage-fetch failure without blanking the pill.

    A timeout, an unexpected error, or a single unparseable scrape is transient.
    Overwriting a previously-good cache with ``{"available": False}`` on any of
    these hid the credit pill entirely for up to a full refresh interval — the
    "disappearing pill" bug. Instead, when we already hold a good value, keep it
    and flag it ``stale`` (the dashboard can dim it); only fall back to
    ``available: False`` when there is no prior value to show (e.g. a cold-start
    failure), preserving the original hide-on-no-data behavior. The definitive
    "kiro-cli absent" case still sets ``available: False`` directly at its call
    site — that is not transient.
    """
    global _usage_cache, _usage_cache_ts
    if _usage_cache.get("credits_plan") is not None:
        _usage_cache = {**_usage_cache, "stale": True}
    else:
        _usage_cache = {"available": False}
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
    ``{}`` on any failure — identity is decorative, so it must never break the
    credit readout. stdout is untrusted: only the LEADING JSON object is parsed
    (kiro-cli appends a non-JSON "Profile:" block after it), values must be
    strings, and each is length-bounded before it can reach the cache/UI.
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
        full = raw  # keep the whole output; the ARN lives AFTER the JSON object
        # Take only the first {...} block; trailing "Profile:\n<name>" is not JSON.
        depth = 0
        start = raw.find("{")
        if start < 0:
            return {}
        for i in range(start, len(raw)):
            if raw[i] == "{":
                depth += 1
            elif raw[i] == "}":
                depth -= 1
                if depth == 0:
                    raw = raw[start : i + 1]
                    break
        else:
            return {}
        data = json.loads(raw)
        if not isinstance(data, dict):
            return {}
        out_map: dict[str, object] = {}
        for src, dst, cap in (
            ("email", "email", 254),
            ("accountType", "account_type", 60),
            ("startUrl", "start_url", 200),
        ):
            v = data.get(src)
            if isinstance(v, str) and v:
                out_map[dst] = v[:cap]
        # whoami's own profile ARN, printed in the trailing (non-JSON) "Profile:"
        # block. Private (leading underscore): used only to prove this identity
        # belongs to the same account the credit numbers came from, and stripped
        # before anything is cached or served.
        m = re.search(r"arn:aws:codewhisperer:[^\s\"']+", full)
        if m:
            out_map["_profile_arn"] = m.group(0)[:200]
        return out_map
    except (asyncio.TimeoutError, ValueError, OSError):
        logger.debug("whoami identity fetch failed", exc_info=True)
        return {}
    except Exception:
        logger.debug("whoami identity fetch failed (unexpected)", exc_info=True)
        return {}
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


async def _fetch_usage_bg() -> None:
    """Background task: fetch usage and update cache."""
    global _usage_cache, _usage_cache_ts, _usage_fetching
    if _usage_fetching:
        return
    _usage_fetching = True
    proc = None
    sandbox_cleanup = None
    kiro_bin: str | None = None
    # Only a refresh that actually SPAWNED the billed scrape feeds the failure
    # backoff — an API-path error or a missing kiro-cli says nothing about
    # whether the scrape works.
    scrape_attempted = False

    async def _refresh() -> None:
        nonlocal proc, sandbox_cleanup, kiro_bin, scrape_attempted
        global _usage_cache, _usage_cache_ts

        kiro_bin = await _resolve_kiro_bin_for_spawn()
        if not kiro_bin:
            # kiro-cli absent (non-Kiro provider): cache an unavailable marker so
            # the dashboard hides the credit pill instead of polling forever.
            _publish_usage({"available": False})
            return
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
        # contain one, and the billed text scrape is no better a source. The
        # ``reason`` rides the existing unavailable-marker shape so the
        # frontend can say WHY instead of hiding the pill without explanation.
        account_type = identity.get("account_type")
        if (
            isinstance(account_type, str)
            and re.sub(r"[^a-z0-9]", "", account_type.lower()) == "apikey"
        ):
            _publish_usage({"available": False, "reason": "api_key_auth"})
            logger.info("Kiro usage: not available under API key auth; skipping fetch")
            return
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
        # could not be resolved at all, both still get the free API call instead of
        # the credit-consuming text scrape, while an unprovable credential is still
        # refused.
        #
        # Runs on the subprocess pool (not the default to_thread pool): the client
        # makes blocking urllib calls that can hang on DNS / a wedged TLS
        # handshake, so they are isolated from the maintenance/cron pools. Fails
        # closed (returns None) so we fall through to the text scrape rather than
        # showing a fabricated number.
        api_usage = await asyncio.get_running_loop().run_in_executor(
            subprocess_executor(),
            functools.partial(kiro_usage_api.fetch_usage_limits, expected_arn=expected_arn),
        )
        if api_usage and api_usage.get("credits_plan") is not None:
            # API output is untrusted too: redact every string leaf before caching.
            api_usage = {k: _redact_strings(v) for k, v in api_usage.items()}
            # Strip the private coupling metadata before it can reach the cache.
            api_arn = api_usage.pop("_profile_arn", None)
            # Attach the signed-in identity ONLY when it provably belongs to the
            # account these credits were billed to (see _identity_matches_account).
            # Anchoring already guarantees this whenever an ARN existed on both
            # sides; the check is kept as the independent assertion of it, and
            # still carries the no-ARN (Builder ID) case on its own.
            if identity and _identity_matches_account(api_arn, identity):
                api_usage.update(
                    {k: _redact_strings(v) for k, v in identity.items() if not k.startswith("_")}
                )
            _publish_usage(api_usage)
            logger.info(
                "Kiro usage refreshed (api): %s / %s credits",
                api_usage.get("credits_used", "?"),
                api_usage.get("credits_plan", "?"),
            )
            return
        # Fallback: scrape kiro-cli /usage stdout. Lossy for org-managed accounts
        # on recent kiro-cli (no overage line), but the only source when the API
        # path is unavailable (no token / non-Kiro build).
        #
        # This is a BILLED chat turn, not a free read, and this refresh runs on a
        # timer whenever a dashboard tab is open — so it only happens when the
        # user has explicitly opted in, and stops entirely once it has failed
        # enough times to look broken. Both checks are before the spawn, so a
        # disabled or parked scrape costs nothing at all.
        if not await asyncio.to_thread(_text_scrape_enabled):
            _log_scrape_disabled_once()
            _cache_without_scrape(api_usage, identity, reason="scrape_disabled")
            return
        if _scrape_in_backoff():
            _cache_without_scrape(api_usage, identity)
            return
        scrape_attempted = True
        # Route through the OS-level sandbox, consistent with how the main agent
        # kiro-cli process is spawned (AcpClient._spawn -> wrap_argv) — including
        # the TIER. This is a `kiro-cli chat` invocation, so a hardcoded
        # "standard" asks for stricter isolation than the very same chat binary
        # gets on the interactive path, and fail-closes wherever no backend
        # exists. Doubly wasteful here: the scrape is a BILLED turn, so the
        # refusal also fed the backoff counter that eventually parks it.
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
        proc = await create_subprocess_limited(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=scrub_agent_subprocess_env(),
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=60)
        raw = (out or err or b"").decode(errors="replace")
        parsed = _parse_usage(raw)
        if parsed.get("credits_plan") is not None:
            # A parseable plan means the scrape itself works, so clear any
            # accumulated failures even on the preservation path below (which
            # discards the value for being overage-blind, not for being broken).
            _record_scrape_outcome(True)
            # Converge on the canonical shape (credits_used = total, explicit
            # credits_overage) so the dashboard never branches on source, then
            # redact credentials / exfil URLs from every string leaf before the
            # dict is cached and served (kiro-cli output is untrusted).
            parsed = _normalize_text_usage(parsed)
            # Re-resolve identity ADJACENT to the scrape, BEFORE any decision
            # that consumes it. A profile switch can land in the window between
            # the top-of-refresh whoami and now (the API attempt ≤30s + this
            # scrape ≤60s), so the top identity may already be stale. This fresh
            # value gates the preservation guard below AND labels the scrape if
            # we proceed — both must judge against the same, adjacent identity,
            # else an account switch mid-fallback could keep the previous
            # account's usage/email on screen. whoami costs no credits.
            fresh_identity = await _fetch_whoami(kiro_bin)
            if _text_scrape_regresses_api_value(_usage_cache, parsed, fresh_identity):
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
                return
            parsed = {k: _redact_strings(v) for k, v in parsed.items()}
            # No ARN coupling check here: this scrape IS kiro-cli's own `/usage`
            # output, so it and `fresh_identity` describe the same account by
            # construction — because `fresh_identity` was resolved ADJACENTLY
            # just above, not reused from the top of this refresh. The API branch
            # above does not need this: it gates its merge on
            # `_identity_matches_account`, so a mid-refresh switch makes the stale
            # identity's ARN mismatch the accepted credential's and the email is
            # simply dropped.
            parsed.update(
                {k: _redact_strings(v) for k, v in fresh_identity.items() if not k.startswith("_")}
            )
            _publish_usage(parsed)
            logger.info(
                "Kiro usage refreshed (text): %s credits used",
                parsed.get("credits_used", "?"),
            )
        else:
            # No parseable credit plan this cycle (unrecognized /usage output,
            # or transient garbage). Keep the last good value (stale) rather than
            # blanking the pill; only hide when we have nothing to show.
            _record_scrape_outcome(False)
            _cache_transient_failure()

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
        await asyncio.wait_for(_refresh(), timeout=_USAGE_FETCH_DEADLINE_SECS)
    except asyncio.TimeoutError:
        # Transient hang — keep the last good value (stale) instead of blanking.
        logger.debug("Background usage fetch timed out")
        if scrape_attempted:
            _record_scrape_outcome(False)
        _cache_transient_failure()
    except Exception:
        logger.debug("Background usage fetch failed", exc_info=True)
        if scrape_attempted:
            _record_scrape_outcome(False)
        _cache_transient_failure()
    finally:
        # Always reap the subprocess on any exit path (timeout, error, or task
        # cancellation, which is a BaseException the excepts above don't catch)
        # so a leaked kiro-cli process can't hold the agent lock or keep burning
        # credit quota. kill() is non-blocking; the OS reaps the zombie.
        _usage_fetching = False
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
                # Await termination so the asyncio transport + pipe FDs close
                # (otherwise they leak, and this runs on a timer). Bounded by
                # wait_for so a wedged process can't reintroduce an unbounded hang.
                await asyncio.wait_for(proc.wait(), timeout=5)
            except Exception:
                pass
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
        # text scrape, which spends credits. A faster readout is not worth billing
        # the user for it; a profile switch is picked up on the next interval.
        state: DashboardState = request.app["state"]
        task = asyncio.create_task(_fetch_usage_bg())
        state._background_tasks.add(task)
        task.add_done_callback(state._background_tasks.discard)
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
        return _HistoryDeleteClaim(
            candidate_key,
            candidate_slot,
            history_key,
            session_key,
            generation,
            path_match_verified,
            True,
            getattr(candidate_slot, "task", None),
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
) -> tuple[bool | None, _HistoryDeleteClaim]:
    """Bind slot and cron ownership, then unlink under one transcript lock."""
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
            if skip_pinned:
                result = log.delete_session(key, skip_pinned=True)
            else:
                result = log.delete_session(key)
    except HistoryLockTimeout:
        logger.warning("delete_session: lock timeout, not deleting key=%s", key)
        return False, claim
    return result, resolved_claim


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
        )
    except _OwnerKeyUnreadable:
        return _cron_ownership_unknown_refusal(crons, swept.get(key, ()))

    if ok:
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
    if slot and slot.running and slot.task is not None:
        slot.task.cancel()
        try:
            await asyncio.wait_for(slot.task, timeout=2.0)
        except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
            pass

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


async def api_sessions_clear(request: web.Request) -> web.Response:
    """DELETE /api/sessions — permanently delete closed history sessions only."""
    state: DashboardState = request.app["state"]
    if not state.conversation_log:
        return web.json_response({"error": "no conversation log"}, status=400)

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
        try:
            result, delete_claim = await asyncio.to_thread(
                _delete_history_session,
                log,
                key,
                delete_claim,
                skip_pinned=True,
                exact_owner_keys=swept.get(key, ()),
            )
            if result is None:
                skipped += 1
            elif result:
                cleanup_claims.append((key, delete_claim))
                count += 1
            else:
                failed += 1
        except _OwnerKeyUnreadable:
            undeletable.append({"id": key, "code": CRON_OWNERSHIP_UNKNOWN_CODE})
        except Exception:
            failed += 1
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
    # is_responsive()/the stall watchdog — the periodic idle sweep reads
    # ``last_used`` instead, so without this a session blocking in a long
    # `wait` still ages toward being reaped for idleness.
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


def _read_managed_tool_policy_sync(agent_path: Path) -> dict[str, Any] | None:
    """Read one agent's ``managedToolPolicy`` from disk. Blocking.

    Split out so the whole filesystem transaction -- the existence probe, the
    read and the JSON parse -- crosses to a worker as ONE unit. Offloading only
    the read would leave the ``stat`` and the parse on the gateway's single event
    loop, which is the same defect in a smaller form.

    ``None`` means "no policy to report", and is deliberately distinct from an
    empty dict. The caller answers ``{}`` for both, but only a dict is an agent
    whose config was read and understood, which is what its SEL ``ok`` record
    attests -- an unreadable or malformed file is not an agent with no policy.
    Collapsing the two would start logging success for files this never parsed.
    """
    try:
        if not agent_path.is_file():
            return None
        config = json.loads(agent_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(config, dict):
        # Valid JSON that is not an object (a list, a scalar, null) parses
        # fine, but `.get` on it would raise. It is a malformed spec, so it
        # takes the same disposition as the unparseable case above.
        return None
    policy = config.get("managedToolPolicy", {})
    return policy if isinstance(policy, dict) else None


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
    agent_path = kiro_agents_dir() / f"{agent_name}.json"
    policy = await asyncio.to_thread(_read_managed_tool_policy_sync, agent_path)
    if policy is None:
        # Missing, unreadable, malformed, or a non-dict policy: answer the same
        # empty policy as before and, as before, do not log it as a success.
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
                        # Off-loop: the kill authorizes a saved process group,
                        # which re-reads one /proc identity per member witness --
                        # unbounded in the width of the tree -- and can escalate
                        # SIGTERM to SIGKILL with a waitpid in between. Awaited,
                        # so start_pool below cannot re-spawn while the old tree
                        # is still being torn down.
                        await asyncio.get_running_loop().run_in_executor(
                            subprocess_executor(), _h._sync_kill_provider, p
                        )
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
