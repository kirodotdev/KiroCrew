"""Session lifecycle, usage, search, approvals, and reset handlers."""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.providers.base import LLMProvider  # noqa: F811

from aiohttp import web

# circular import: handlers/__init__.py re-exports this module's handlers, so
# a `from ... import` of individual names would fail mid-cycle. `import ... as`
# binds via sys.modules and defers attribute access to call time, which also
# keeps tests' monkeypatching of handlers.redact_* effective (late binding).
import kiro_crew.dashboard.handlers as _h
from kiro_crew.acp.client import _resolve_kiro_bin_for_spawn
from kiro_crew.config.paths import kiro_agents_dir
from kiro_crew.dashboard.handlers import kiro_usage_api
from kiro_crew.dashboard.kiro_readiness import reject_if_kiro_unverified
from kiro_crew.dashboard.session_memory import SessionMemorySampler
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.executors import subprocess_executor
from kiro_crew.history import INCOGNITO_MEMORY_MODES, SEARCH_MIN_CHARS, _archive_dir
from kiro_crew.llm_helpers import run_bg_oneliner
from kiro_crew.mcp_discovery import (
    discover_servers_to_sync,
    register_servers_for_cc,
    sync_to_agent_config,
)
from kiro_crew.sandbox import (
    cgroup_scope_argv,
    configured_sandbox_mode,
    create_subprocess_limited,
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


async def api_sessions_context(request: web.Request) -> web.Response:
    """GET /api/sessions/context — context usage for all active sessions."""
    state: DashboardState = request.app["state"]
    return web.json_response({"sessions": state.sessions.context_info()})


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
_health_lock: asyncio.Lock | None = None
_HEALTH_REFRESH_SECS = 15


async def api_sessions_health(request: web.Request) -> web.Response:
    """GET /api/sessions/health — slots flagged as stalled from log scan."""
    global _health_cache, _health_cache_ts, _health_lock
    if _health_lock is None:
        _health_lock = asyncio.Lock()
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
_usage_fetching = False

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


def _cache_without_scrape(api_usage: object, identity: dict[str, object]) -> None:
    """Cache the best available value when the scrape is not going to run.

    Degrades rather than erroring: keep a previously-good value (dimmed
    ``stale``) so the pill does not blink out, otherwise surface whatever
    partial fields the API did return alongside ``available: False`` — the
    frontend's existing signal to hide the pill instead of rendering blanks.

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
        partial = {k: _redact_strings(v) for k, v in api_usage.items()} if (
            isinstance(api_usage, dict)
        ) else {}
        partial.pop("_profile_arn", None)
        _usage_cache = {**partial, "available": False}
    _usage_cache_ts = time.time()


def _safe_float(text: str) -> float | None:
    """Parse a float, returning None on malformed input instead of raising."""
    try:
        return float(text)
    except ValueError:
        return None


def _parse_usage(raw: str) -> dict[str, object]:
    """Parse structured fields from kiro-cli /usage output."""
    clean = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", raw)
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

    # Bonus / promotional credits are a separate pool kiro-cli lists in its own
    # section after the plan, e.g.:
    #     Bonus Credits:
    #       Welcome bonus: 386.34/500 (expires in 15 days)
    # This pool is drawn down BEFORE the plan, so while it lasts the plan meter
    # barely moves — which looked like a frozen counter until we surfaced it.
    # Capture the first bonus line (first-wins) so the dashboard can pool it into
    # the header total and show it in the credits modal.
    in_bonus = False
    for line in usage_lines:
        if "Bonus Credits" in line:
            in_bonus = True
            continue
        if not in_bonus or "bonus_limit" in result:
            continue
        m = re.search(r"([A-Za-z][A-Za-z0-9 ]*?):\s*([\d.]+)\s*/\s*([\d.]+)", line)
        if not m:
            continue
        used = _safe_float(m.group(2))
        limit = _safe_float(m.group(3))
        if used is not None and limit is not None and limit > 0:
            result["bonus_label"] = m.group(1).strip()
            result["bonus_used"] = used
            result["bonus_limit"] = limit
            exp = re.search(r"\(([^)]*expires[^)]*)\)", line, re.IGNORECASE)
            if exp:
                result["bonus_expires_label"] = exp.group(1).strip()
    return result


def _normalize_text_usage(parsed: dict[str, object]) -> dict[str, object]:
    """Convert the text-scrape parse result to the canonical usage shape.

    Emits the canonical usage shape the dashboard consumes so it never branches
    on source:
      credits_used = TOTAL used, credits_overage = overage above plan,
      credits_covered = in-plan portion, credits_plan = limit, percentage.

    In the raw text, "Credits used:" is the OVERAGE field (0 for org accounts,
    and absent entirely on kiro-cli 2.11.x), while "(X of Y covered in plan)" is
    the in-plan covered/limit. Total = covered + overage. Post-regression the
    text carries no overage, so this honestly reports covered==total.
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
        isinstance(prev_resets, str) and prev_resets
        and isinstance(new_resets, str) and new_resets
        and prev_resets == new_resets
    ):
        return False
    prev_used = prev.get("credits_used")
    new_used = new.get("credits_used")
    if not isinstance(prev_used, (int, float)) or not isinstance(
        new_used, (int, float)
    ):
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
    read as "not kiro-cli". On macOS with ``agent.sandbox="off"`` that
    misclassification skips the delegation branch — and with it the credential-env
    scrub — so the child would inherit the sensitive environment. Both callers
    here spawn kiro-cli by construction, and both ACP spawn paths pass the same
    flag for the same reason.
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
        # kiro-cli's own internal sandbox) on a host with no backend, the pinned
        # "standard" fail-closed and silently dropped the identity this readout
        # labels the credit numbers with — failure here is non-fatal by design,
        # so the symptom is a permanently blank email, not an error.
        # Off the loop: see _wrap_argv_at_configured_tier for the two blocking reads.
        argv, cleanup = await asyncio.get_running_loop().run_in_executor(
            subprocess_executor(), _wrap_argv_whoami, kiro_bin
        )
        argv = cgroup_scope_argv(argv)
        proc = await create_subprocess_limited(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
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
    except (asyncio.TimeoutError, json.JSONDecodeError, ValueError, OSError):
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
    return (
        isinstance(api_arn, str)
        and isinstance(whoami_arn, str)
        and api_arn == whoami_arn
    )


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
    try:
        kiro_bin = await _resolve_kiro_bin_for_spawn()
        if not kiro_bin:
            # kiro-cli absent (non-Kiro provider): cache an unavailable marker so
            # the dashboard hides the credit pill instead of polling forever.
            _usage_cache = {"available": False}
            _usage_cache_ts = time.time()
            return
        # Identity FIRST, because it is the anchor for credential selection.
        # ``whoami`` is kiro-cli's own account, and it costs no credits; passing
        # its profile ARN into fetch_usage_limits is what stops a still-valid
        # credential from a signed-out profile supplying the numbers. Fetched
        # once here and reused by both the API and text branches below.
        identity = await _fetch_whoami(kiro_bin)
        raw_arn = identity.get("_profile_arn")
        expected_arn = raw_arn if isinstance(raw_arn, str) and raw_arn else None
        # Primary source: the real GetUsageLimits API. It reads the live bearer
        # token kiro-cli already maintains and returns the true used/limit/overage,
        # so it survives kiro-cli stdout format changes (the regression that dropped
        # the overage line).
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
                    {
                        k: _redact_strings(v)
                        for k, v in identity.items()
                        if not k.startswith("_")
                    }
                )
            _usage_cache = api_usage
            _usage_cache_ts = time.time()
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
            _cache_without_scrape(api_usage, identity)
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
                {
                    k: _redact_strings(v)
                    for k, v in fresh_identity.items()
                    if not k.startswith("_")
                }
            )
            _usage_cache = parsed
            _usage_cache_ts = time.time()
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


async def api_sessions(request: web.Request) -> web.Response:
    """GET /api/sessions — list conversation session files.

    Query params:
      - ``limit``: max sessions to return (default 50, max 200)
      - ``offset``: skip first N sessions (default 0)
      - ``preview``: when truthy, attach a redacted last-message ``preview``
        to each returned session (bounded tail read; page-scoped so the
        default list stays a cheap metadata scan)

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
    all_sessions = state.conversation_log.list_sessions()
    total = len(all_sessions)
    page = all_sessions[offset : offset + limit]
    if want_preview:
        log = state.conversation_log

        def _attach_previews(sessions: list[dict]) -> None:
            for s in sessions:
                preview = log.last_message_preview(s.get("key", ""))
                if preview:
                    preview, _ = _h.redact_exfiltration_urls(preview)
                    preview, _ = _h.redact_credentials(preview)
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
_SUMMARIZE_TIMEOUT_SECS = 30  # per-session deadline so one stalled prompt can't pin the shared _bg session
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
    if str(meta.get("memory_mode", "")).lower() in INCOGNITO_MEMORY_MODES:
        return ""
    # Cache: a summary persisted in a sidecar file is reusable as long as the
    # session file hasn't changed since it was generated. session_mtime advances
    # only on real message appends (preserved across metadata writes), so it is a
    # cheap, exact staleness signal — a repeat list_sessions(summarize=true) for
    # an unchanged session pays zero LLM cost. The cache lives in a sidecar
    # (never the session JSONL) so summarizing an *active* session never rewrites
    # its log and cannot lose a concurrently-appended message.
    sig = await loop.run_in_executor(None, log.session_mtime, key)
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
                functools.partial(log.set_cached_summary, key, summary, sig),
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
        title = s.get("title")
        if title:
            title, _ = _h.redact_exfiltration_urls(title)
            title, _ = _h.redact_credentials(title)
            s["title"] = title
        snip = s.get("snippet")
        if snip:
            snip, _ = _h.redact_exfiltration_urls(snip)
            snip, _ = _h.redact_credentials(snip)
            s["snippet"] = snip
    return web.json_response({"sessions": sessions})


async def api_session_detail(request: web.Request) -> web.Response:
    """GET /api/sessions/{key} — return messages for a session."""
    state: DashboardState = request.app["state"]
    key = request.match_info["key"]
    if not state.conversation_log:
        return web.json_response([])
    return web.json_response(state.conversation_log.read_messages(key))


async def api_session_delete(request: web.Request) -> web.Response:
    """DELETE /api/sessions/{key} — permanently delete a history session."""
    state: DashboardState = request.app["state"]
    key = request.match_info["key"]
    if not state.conversation_log:
        return web.json_response({"error": "no conversation log"}, status=400)
    # delete_session enters _locked (flock acquire + os.close); offload off the
    # loop so a wedged cross-process peer can't freeze chat/WS/heartbeat.
    ok = await asyncio.to_thread(state.conversation_log.delete_session, key)
    if ok:
        try:
            await _remove_slot_for_history_key(state, key)
        except Exception:
            logger.warning("cleanup failed for session %s", key, exc_info=True)
        state.push_slots_update()
        state.push_refresh("history")
    return web.json_response({"ok": ok})


async def _remove_slot_for_history_key(state: DashboardState, key: str) -> None:
    """Remove the active chat slot corresponding to a history key.

    Slot keys may be the raw history key (``dashboard_chat-X-TS`` when
    resumed from history) or the stripped form (``chat-X-TS`` for
    sessions that were never closed and resumed).  Try the exact key
    first, then the stripped variant.  Also kills the kiro-cli session
    to prevent orphaned processes.
    """
    from kiro_crew.dashboard.state import _normalize_slot_key

    slot = state._slots.pop(key, None)
    if not slot:
        stripped = key
        if stripped.startswith("dashboard:"):
            stripped = stripped[len("dashboard:") :]
        while stripped.startswith("dashboard_"):
            stripped = stripped[len("dashboard_") :]
        slot = state._slots.pop(stripped, None)
    if not slot:
        # Reverse: history key has no prefix, but slot was stored with one
        slot = state._slots.pop("dashboard_" + key, None)
    if not slot:
        # A channel-born slot's name is the key folded to the filename
        # charset, which none of the prefix probes above produce. Without
        # this the slot outlives its deleted history and keeps a kiro-cli
        # process alive.
        slot = state._slots.pop(_normalize_slot_key(key), None)
    if slot:
        # A pending ask_question is owned by the slot's running turn, but its
        # future lives in DashboardState rather than on slot.task. History
        # deletion tears down that task and provider directly, bypassing the
        # normal stop/delete handlers; resolve the wait first so the MCP HTTP
        # request returns and its finally block retracts the now-stale card.
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
    # Kill the kiro-cli subprocess to free resources
    if slot:
        try:
            # Destroy the session the slot actually RUNS, not one derived from
            # the history key. A channel-born slot runs the channel's own
            # session, so ``_history_key_for`` would name a key no session has:
            # the provider survives the delete and its next inbound message
            # recreates the transcript the user just removed.
            from kiro_crew.dashboard.chat_utils import effective_session_key

            await state.sessions.destroy(effective_session_key(slot))
        except Exception:
            pass


async def api_sessions_clear(request: web.Request) -> web.Response:
    """DELETE /api/sessions — permanently delete closed history sessions only.

    Skips sessions currently open in the sidebar (any slot in
    ``state._slots``) and sessions with ``pinned=True`` on disk.
    Bulk-archiving open unpinned/idle sessions is out of scope here.
    """
    state: DashboardState = request.app["state"]
    if not state.conversation_log:
        return web.json_response({"error": "no conversation log"}, status=400)

    from kiro_crew.dashboard.chat_utils import slot_history_key, slot_transcript_key
    from kiro_crew.history import _safe_key

    protected: set[str] = set()
    for slot in state._slots.values():
        # Protect EVERY transcript this slot could be reading, not just the one
        # it currently writes. ``list_sessions`` reports filename stems, so each
        # candidate contributes its key AND its stem: ``_safe_key`` is the
        # function that produced the filename, and a single-colon replace would
        # leave a multi-colon channel key like ``discord:kirocrew:direct:123``
        # mapped to a stem that does not exist, putting a live conversation
        # outside ``protected`` so this bulk delete removes it.
        #
        # The union matters because a slot's write target and its DISPLAY source
        # can differ: a channel tab the dashboard could not bind runs under
        # ``dashboard:<stem>`` while the conversation on screen lives in the
        # channel transcript. Choosing between them here would make deletion
        # depend on provenance resolving correctly, and provenance is exactly
        # what a legacy transcript cannot supply. Protection only ever PREVENTS
        # a delete, so covering both candidates is the safe direction: the worst
        # case is that Clear All skips a transcript nobody is reading.
        for candidate in (slot_history_key(slot), slot_transcript_key(slot.key)):
            protected.add(candidate)
            protected.add(_safe_key(candidate))

    sessions = state.conversation_log.list_sessions()
    count = 0
    skipped = 0
    failed = 0
    cleanup_tasks = []
    for s in sessions:
        key = s["key"]
        if key in protected:
            skipped += 1
            continue
        try:
            meta = state.conversation_log.get_metadata(key)
        except Exception:
            logger.warning(
                "api_sessions_clear: unreadable metadata for %s, skipping", key, exc_info=True
            )
            skipped += 1
            continue
        if not isinstance(meta, dict):
            skipped += 1
            continue
        if meta.get("pinned"):
            skipped += 1
            continue
        try:
            # delete_session enters _locked (flock + os.close) — offload off the
            # event loop so a wedged peer can't stall the bulk clear on it.
            if await asyncio.to_thread(state.conversation_log.delete_session, key):
                cleanup_tasks.append(_remove_slot_for_history_key(state, key))
                count += 1
            else:
                failed += 1
        except Exception:
            failed += 1
            logger.warning("api_sessions_clear: delete raised for %s", key, exc_info=True)
    if cleanup_tasks:
        await asyncio.gather(*cleanup_tasks, return_exceptions=True)
    if count:
        state.push_slots_update()
        state.push_refresh("history")
    logger.info("api_sessions_clear: cleared=%d skipped=%d failed=%d", count, skipped, failed)
    return web.json_response(
        {"ok": failed == 0, "cleared": count, "skipped": skipped, "failed": failed}
    )


# ── Approvals ──


async def api_approvals(request: web.Request) -> web.Response:
    """GET /api/approvals — list pending tool approvals."""
    state: DashboardState = request.app["state"]
    return web.json_response(list(state._pending_approvals.values()))


async def api_approval_resolve(request: web.Request) -> web.Response:
    """POST /api/approvals/{id}/{action} — approve or reject."""
    state: DashboardState = request.app["state"]
    approval_id = request.match_info["id"]
    action = request.match_info["action"]
    if action not in ("approve", "reject"):
        return web.json_response({"error": "invalid action"}, status=400)
    ok = state.resolve_approval(approval_id, action == "approve")
    if not ok:
        return web.json_response({"error": "not found or expired"}, status=404)
    return web.json_response({"ok": True})


async def api_session_keepalive(request: web.Request) -> web.Response:
    """POST /api/session-keepalive — refresh activity timestamp on the
    session's provider so idle-detection/stale-checks don't SIGTERM a
    session that's intentionally blocking in a long-running MCP tool
    (e.g. the `wait` tool).

    Authenticated via X-Internal-Secret; session is selected via the
    X-Session-Key header that all MCP subprocesses already send.
    """
    state: DashboardState = request.app["state"]
    session_key = request.headers.get("X-Session-Key", "").strip()
    if not session_key:
        return web.json_response({"error": "X-Session-Key required"}, status=400)
    provider = state.sessions.get_provider(session_key)
    if provider is None:
        return web.json_response({"error": "session not found"}, status=404)
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
    return web.json_response({"ok": True})


async def api_session_tool_policy(request: web.Request) -> web.Response:
    """GET /api/session-tool-policy — return managedToolPolicy for the
    calling session's agent.

    Used by managed MCP servers (kirocrew-core, kirocrew-cron) to filter
    their tool lists per-agent.  Returns {"exclude": [...]} on success,
    or 400/404 when the session cannot be identified (deny-by-default:
    callers that cannot prove identity get an error, not an empty policy).
    Authenticated via X-Internal-Secret + X-Session-Key.
    """
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
        slot_key = session_key[len("dashboard:"):]
        slot = state.get_slot(slot_key)
        if slot:
            agent_name = slot.agent
    # Subagent — look up in SubagentManager
    elif session_key.startswith("subagent:"):
        if state.subagents:
            subagent_id = session_key[len("subagent:"):]
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

    # Read agent config from disk
    agent_path = kiro_agents_dir() / f"{agent_name}.json"
    if not agent_path.is_file():
        return web.json_response({})

    try:
        config = json.loads(agent_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return web.json_response({})

    policy = config.get("managedToolPolicy", {})
    if not isinstance(policy, dict):
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
            count, len(pool_providers),
        )

    state.broadcast_ws("sessions_restarting", {"status": "restarting"})

    async def _background_restart() -> None:
        if providers:

            async def _safe_shutdown(p: LLMProvider) -> None:
                import kiro_crew.dashboard.handlers as _h  # noqa: F811

                _timeout = _SHUTDOWN_TIMEOUT_SECS
                try:
                    await asyncio.wait_for(p.shutdown(), timeout=_timeout)
                except asyncio.TimeoutError:
                    logger.warning(
                        "Session shutdown hung past %.1fs; forcing kill",
                        _timeout,
                    )
                    try:
                        _h._sync_kill_provider(p)
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
    # Run in thread — discover/sync do blocking file I/O and subprocess calls.
    # Cap at 30s so a hung kiro-cli subprocess doesn't stall the restart.
    synced = 0
    try:

        async def _sync() -> int:
            to_sync = await asyncio.to_thread(discover_servers_to_sync)
            if to_sync:
                ok: bool = await asyncio.to_thread(sync_to_agent_config, to_sync)
                # Register for CC unconditionally (CC uses its own .mcp.json)
                await asyncio.to_thread(register_servers_for_cc, to_sync)
                if ok:
                    return len(to_sync)
            return 0

        synced = await asyncio.wait_for(_sync(), timeout=30)
    except Exception:
        logger.warning("MCP server sync failed before restart", exc_info=True)
    count = await _reset_all_sessions(request)
    return web.json_response({"ok": True, "sessions_reset": count, "mcp_synced": synced})


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
    redacted = await asyncio.to_thread(
        lambda: redact(raw)
    )
    return web.Response(text=redacted, content_type="application/x-ndjson")
