"""Session lifecycle, usage, search, approvals, and reset handlers."""

from __future__ import annotations

import asyncio
import functools
import importlib as _importlib
import logging
import os
import re
import sys as _sys
import time
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from kiro_crew.loop_lock import LoopBoundLock

if TYPE_CHECKING:
    from kiro_crew.providers.base import LLMProvider  # noqa: F811

    # ``sessions_parts.restart`` annotates with LLMProvider and reads it from here: this
    # file keeps the family's one provider-layer import, the edge the agent-sdk baseline counts.
    _RESTART_PROVIDER_TYPE = LLMProvider
    # What the mirror at the bottom of this module serves, for type checkers and IDEs:
    # every name an owner defines, then every project name an owner imports.
    from kiro_crew.dashboard.handlers.sessions_parts.usage_scrape import (  # noqa: F401
        _ANSI_ESCAPE_RE,
        _BONUS_COLON_RE,
        _BONUS_DASH_RE,
        _MAX_BONUS_CREDITS,
        _MAX_BONUS_DAYS_LEFT,
        _MAX_BONUS_GRANTS,
        _MAX_BONUS_NAME_CHARS,
        _WHOAMI_UNPROVEN,
        USAGE_SCRAPE_ENGINE_PIN_MIN_VERSION,
        _safe_float,
        _safe_int,
        _usage_scrape_argv,
        _wrap_argv_at_configured_tier,
        _wrap_argv_whoami,
    )
    from kiro_crew.dashboard.handlers.sessions_parts.app_scope import (  # noqa: F401
        _NOT_TRANSCRIPT_OWNER,
        _STATE_APPROVALS_NOT_APP_VISIBLE,
        _app_bulk_history_refusal,
        _app_may_claim_transcript,
        _app_not_found,
        _app_owned_messages,
        _app_owned_sessions,
        _app_owns_transcript,
        _app_transcript_busy,
        _app_transcript_refusal,
        _audit_app_allow,
        _audit_app_decision,
        _audit_app_denial,
        _sel,
    )
    from kiro_crew.dashboard.handlers.sessions_parts.history_reads import (  # noqa: F401
        _DASHBOARD_SLOT_STEM_RE,
        _MACHINE_NAMESPACES,
        _SHORT_SLOT_NAME_RE,
        _AppOwnedKeys,
        _app_search,
        _has_user_content,
        _is_machine_only_session,
        _is_user_listed_row,
        _open_slot_transcript_keys,
        _session_meta_row,
        _sessions_named,
        api_session_archive_list,
        api_session_archive_read,
        api_session_detail,
        api_session_meta,
        api_sessions,
        api_sessions_search,
    )
    from kiro_crew.dashboard.handlers.sessions_parts.summaries import (  # noqa: F401
        _SUMMARIZE_MAX_SESSIONS,
        _SUMMARIZE_MODEL,
        _SUMMARIZE_MSG_LIMIT,
        _SUMMARIZE_NARRATION_PREFIXES,
        _SUMMARIZE_PROMPT,
        _SUMMARIZE_PROSE_MAX_UNSPACED_CHARS,
        _SUMMARIZE_PROSE_MAX_WORDS,
        _SUMMARIZE_PROSE_OPENERS,
        _SUMMARIZE_REFUSAL_MARKERS,
        _SUMMARIZE_TIMEOUT_SECS,
        _build_summary_prompt,
        _summarize_one,
        _summary_is_narrated_refusal,
        api_sessions_summarize,
    )
    from kiro_crew.dashboard.handlers.sessions_parts.delete_claim import (  # noqa: F401
        _CRON_RELEASE_ATTEMPTS,
        _CRON_RELEASE_BACKOFF_SECS,
        CRON_OWNERSHIP_UNKNOWN_CODE,
        CRON_STORE_BUSY_CODE,
        CRON_STORE_UNREADABLE_CODE,
        LEDGER_EXCLUSION_UNWRITABLE_CODE,
        _AppOwnershipLost,
        _HistoryDeleteClaim,
        _OwnerKeyUnreadable,
        _capture_history_delete_claim,
        _cron_ownership_unknown_refusal,
        _cron_store_refusal,
        _delete_history_session,
        _exclude_slot_units,
        _history_delete_candidate_keys,
        _ledger_rollback_refusal,
        _linkable_stems_for_history_key,
        _linked_session_key_for_history_key,
        _live_slot_sid,
        _owner_keys_after_unlink,
        _owner_keys_bound_to_transcript,
        _release_cron_ownership,
        _resolve_history_delete_claim,
        _rollback_ledger_exclusion,
        _stranded_job_ids,
        _unstrand_shared_ledger_unit,
        _warn_stranded_cron_ownership,
        _warn_unprovable_channel_binding,
    )
    from kiro_crew.dashboard.handlers.sessions_parts.slot_teardown import (  # noqa: F401
        _CREW_LOG_TEARDOWN_FLUSH_SECONDS,
        _live_session_units,
        _remove_session_crew_logs,
        _remove_slot_for_history_key,
    )
    from kiro_crew.dashboard.handlers.sessions_parts.delete_routes import (  # noqa: F401
        _claim_history_delete,
        _clear_history_rows,
        _clearable_history_keys,
        _delete_claimed_session,
        _user_only_query,
        api_session_delete,
        api_sessions_clear,
        api_sessions_clearable_count,
    )
    from kiro_crew.dashboard.handlers.sessions_parts.live_turn import (  # noqa: F401
        _provider_steer_stamp,
        _resolve_app_approval,
        _service_wait_ping,
        _wait_end_reason,
        api_approval_resolve,
        api_approvals,
    )
    from kiro_crew.dashboard.handlers.sessions_parts.tool_policy import (  # noqa: F401
        _TOOL_POLICY_MEMO,
        _UNREADABLE_SPEC_WARNED,
        _UNREADABLE_SPEC_WARNED_MAX,
        HONOUR_TOOL_REFS_KEY,
        INCLUDE_LIST_SERVER,
        POLICY_INCLUDE_KEY,
        ManagedToolPolicyUnreadable,
        _ambiguous_spec_reason,
        _policy_answer,
        _read_managed_tool_policy_sync,
        _read_managed_tool_policy_uncached,
        _refuse_if_any_spec_is_unreadable,
        _spec_failure_kind,
        _unreadable_spec_remedy,
        _warn_unreadable_spec_once,
    )
    from kiro_crew.dashboard.handlers.sessions_parts.restart import (  # noqa: F401
        _SHUTDOWN_TIMEOUT_SECS,
        _reset_all_sessions,
        api_sessions_restart,
    )
    import kiro_crew.dashboard.handlers as _h  # noqa: F401
    from kiro_crew import hooks  # noqa: F401
    from kiro_crew import session_directive  # noqa: F401
    from kiro_crew.agent_discovery import AgentsDirMemo  # noqa: F401
    from kiro_crew.agent_discovery import AmbiguousAgentSpecError  # noqa: F401
    from kiro_crew.agent_discovery import SensitiveAgentSpecPathError  # noqa: F401
    from kiro_crew.agent_discovery import _SpecReadRefused  # noqa: F401
    from kiro_crew.agent_discovery import plain_markdown_document  # noqa: F401
    from kiro_crew.agent_discovery import read_agent_spec_strict  # noqa: F401
    from kiro_crew.agent_discovery import spec_by_declared_name  # noqa: F401
    from kiro_crew.agent_sdk.mcp_refs import per_tool_grants  # noqa: F401
    from kiro_crew.agent_spec_format import agent_spec_candidates  # noqa: F401
    from kiro_crew.agent_spec_format import is_markdown_spec  # noqa: F401
    from kiro_crew.agent_spec_format import iter_agent_spec_files  # noqa: F401
    from kiro_crew.channel_transcript_migration import _orphan_target_stem  # noqa: F401
    from kiro_crew.cloud.login_target import parse_whoami_output  # noqa: F401
    from kiro_crew.config.paths import kiro_agents_dir  # noqa: F401
    from kiro_crew.cron import CronStoreBusy  # noqa: F401
    from kiro_crew.cron import CronStoreUnreadable  # noqa: F401
    from kiro_crew.cron import cron_owner_matches  # noqa: F401
    from kiro_crew.dashboard import directive_queue  # noqa: F401
    from kiro_crew.dashboard.body_numbers import body_int  # noqa: F401
    from kiro_crew.dashboard.chat_utils import effective_session_key  # noqa: F401
    from kiro_crew.dashboard.chat_utils import slot_history_key  # noqa: F401
    from kiro_crew.dashboard.handlers._shared import SESSION_SEARCH_TEXT_FIELDS  # noqa: F401
    from kiro_crew.dashboard.handlers._shared import internal_memory_scope  # noqa: F401
    from kiro_crew.dashboard.interaction_coordinator import _slot_decision  # noqa: F401
    from kiro_crew.dashboard.state import _normalize_slot_key  # noqa: F401
    from kiro_crew.history import ConversationLog  # noqa: F401
    from kiro_crew.history import HistoryLockTimeout  # noqa: F401
    from kiro_crew.history import SEARCH_MIN_CHARS  # noqa: F401
    from kiro_crew.history import TranscriptBusy  # noqa: F401
    from kiro_crew.history import TranscriptWithheld  # noqa: F401
    from kiro_crew.history import _archive_dir  # noqa: F401
    from kiro_crew.history import transcript_lock_stems  # noqa: F401
    from kiro_crew.history import transcript_stem  # noqa: F401
    from kiro_crew.history import transcript_stems  # noqa: F401
    from kiro_crew.history import transcript_withholds_derivation  # noqa: F401
    from kiro_crew.kiro_cli import kiro_cli_version_at  # noqa: F401
    from kiro_crew.kiro_cli import pin_kiro_cli  # noqa: F401
    from kiro_crew.label_guard import PROSE_OPENERS  # noqa: F401
    from kiro_crew.label_guard import is_verdict_reply  # noqa: F401
    from kiro_crew.label_guard import looks_like_prose  # noqa: F401
    from kiro_crew.llm_helpers import run_bg_oneliner  # noqa: F401
    from kiro_crew.mcp_discovery import sync_discovered_servers  # noqa: F401
    from kiro_crew.messaging.link import _in_namespace  # noqa: F401
    from kiro_crew.messaging.link import canonical_key  # noqa: F401
    from kiro_crew.platform import redact_log_via_context  # noqa: F401
    from kiro_crew.runtime_ownership import release_session_lease  # noqa: F401
    from kiro_crew.sandbox import configured_sandbox_mode  # noqa: F401
    from kiro_crew.sandbox import wrap_argv  # noqa: F401
    from kiro_crew.security import redact  # noqa: F401
    from kiro_crew.security import redact_credentials  # noqa: F401
    from kiro_crew.security import redact_exfiltration_urls  # noqa: F401
    from kiro_crew.validation import sanitize_string  # noqa: F401

from aiohttp import web

from kiro_crew import _facade_mirror
from kiro_crew.acp.client import _resolve_kiro_bin_for_spawn
from kiro_crew.dashboard.handlers import kiro_usage_api
from kiro_crew.dashboard.handlers._shared import (
    guard_owner_surface_routes,
)
from kiro_crew.dashboard.handlers.sessions_parts.usage_scrape import (
    _REASON_ACCOUNT_UNPROVEN,
    _WHOAMI_DIFFERENT,
    _WHOAMI_SAME,
    _account_type_only,
    _fetch_whoami,
    _fetch_whoami_or_none,
    _normalize_text_usage,
    _parse_usage,
    _redact_strings,
    _same_identity,
    _text_scrape_regresses_api_value,
    _whoami_agreement,
    _wrap_argv_usage_scrape,
)
from kiro_crew.dashboard.kiro_readiness import (
    _POLL_GATE_MAX_AGE_SECS,
    reject_if_kiro_unverified,
)
from kiro_crew.dashboard.session_memory import SessionMemorySampler
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.executors import subprocess_executor
from kiro_crew.kiro_prerequisite import spawn_supervised_oneshot
from kiro_crew.platform_compat import _SUBPROCESS_NO_WINDOW, kill_and_reap
from kiro_crew.sandbox import (
    cgroup_scope_argv,
    scrub_agent_subprocess_env,
)

logger = logging.getLogger("kiro_crew.dashboard.handlers.sessions")

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


async def refresh_session_health(state: Any) -> dict[str, Any]:
    """Recompute the cached session-health verdict, and signal a change.

    The one owner of both the cache and :data:`session_health.SESSION_HEALTH_EVENT`.
    ``GET /api/sessions/health`` calls it to serve a request; the WS driver
    (``dashboard/ws.py``) calls it on a timer so a verdict that moves with the
    CLOCK -- a turn crossing the stall threshold, a queue draining -- is noticed
    while nobody is polling. Both share the ``_HEALTH_REFRESH_SECS`` gate and the
    single-flight lock, so N callers still cost at most one computation per
    interval.

    Returns the cached payload. Never raises: a failed computation logs, marks the
    attempt so it is not retried per request, and leaves the previous value (or
    the empty shape) in place.

    Must be awaited ON the loop: the state snapshot walks live slot objects and
    the signal touches WebSocket clients.
    """
    global _health_cache, _health_cache_ts
    now = time.monotonic()
    if now - _health_cache_ts > _HEALTH_REFRESH_SECS:
        async with _health_lock:
            # Re-check after acquiring lock (another request may have refreshed)
            if time.monotonic() - _health_cache_ts > _HEALTH_REFRESH_SECS:
                try:
                    from kiro_crew.dashboard import session_health

                    # ``state`` is a MagicMock in much of the suite: a missing
                    # ``state`` yields an empty snapshot, and a store that is not
                    # a real TaskStore fails its first read inside the
                    # computation and reads as "unavailable" -- never an error.
                    taskq = getattr(getattr(state, "subagents", None), "_taskq", None)
                    snapshot = session_health.snapshot_state(state)
                    computed = await asyncio.to_thread(
                        session_health.compute_session_health,
                        None,
                        taskq=taskq,
                        monitor=None,
                        snapshot=snapshot,
                    )
                    _health_cache = computed
                    _health_cache_ts = time.monotonic()
                    # Back on the loop: publish only when the VERDICT moved, so a
                    # subscriber that cannot read this endpoint still learns when
                    # to refresh what it can read. Signal only -- no session data.
                    session_health.publish_health_change(state, computed)
                except Exception:
                    logger.warning("session_health computation failed", exc_info=True)
                    _health_cache_ts = time.monotonic()
    payload = _empty_health_payload()
    if isinstance(_health_cache, dict):
        payload.update(_health_cache)
    return payload


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
    per-request SQLite + file scan. The computation itself lives in
    :func:`refresh_session_health`, shared with the WS refresh driver.
    """
    state = request.app.get("state") if hasattr(request.app, "get") else None
    return web.json_response(await refresh_session_health(state))


_usage_cache: dict[str, object] = {}
_usage_cache_ts: float = 0.0
#: Fingerprint of the Crew sign-in identity that produced the cached reading
#: (:func:`kiro_crew.auth.bridge.vault_identity_fingerprint`), or ``""`` when the
#: cache came from the kiro-cli path. Kept beside the cache, never in it, so it
#: is not served.
_usage_cache_vault_fp = ""
_USAGE_REFRESH_SECS = 600  # background refresh every 10 min
# Ceiling on ONE whole refresh. Sized above the sum of the inner bounded steps
# (whoami ≤30s + the /usage scrape ≤60s, plus the unbounded API read between
# them) so a healthy slow refresh still completes, while a wedged one is
# guaranteed to release the in-flight guard instead of parking it forever.
_USAGE_FETCH_DEADLINE_SECS = 180
_usage_fetching = False
# --- Text-scrape back-off --------------------------------------------------
# The `/usage` text scrape is a kiro-cli slash command handled locally: it calls
# the same free GetUsageLimits API the primary path uses and prints the usage
# table, so no prompt reaches a model and the read costs no credits. That holds
# only on kiro-cli's V2 engine -- the V3 engine sends `/usage` to the model as a
# prompt -- so the scrape pins V2 (see :func:`_usage_scrape_argv`). It is the
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


def _publish_usage(payload: dict[str, object], *, vault_fp: str = "") -> None:
    """Atomically replace the cache served by ``/api/sessions/usage``.

    ``vault_fp`` names the Crew sign-in identity the reading belongs to; every
    kiro-cli-path publish leaves it empty, so a vault-path failure can never keep
    a kiro-cli reading on screen as the vault account's.
    """
    global _usage_cache, _usage_cache_ts, _usage_cache_vault_fp
    _usage_cache = payload
    _usage_cache_ts = time.time()
    _usage_cache_vault_fp = vault_fp


def _cache_vault_failure(vault_fp: str, reason: str | None = None) -> None:
    """Vault-path counterpart of :func:`_cache_transient_failure`.

    Keeps a prior reading (dimmed ``stale``) only when it was published for this
    same vault identity; anything else reports unavailable, with ``reason`` when
    the failure is auth-class.
    """
    global _usage_cache, _usage_cache_ts
    if (
        vault_fp
        and _usage_cache.get("credits_plan") is not None
        and _usage_cache_vault_fp == vault_fp
    ):
        _usage_cache = {**_usage_cache, "stale": True}
        _usage_cache_ts = time.time()
        return
    unavailable: dict[str, object] = {"available": False}
    if reason is not None:
        unavailable["reason"] = reason
    _publish_usage(unavailable)


def _vault_owns_usage_sync() -> bool:
    """True when the default harness is KAS AND Crew's sign-in vault holds a usable identity.

    That is exactly when the session the user is chatting in draws its credential
    from the vault (the KAS spawn asks the same :func:`vault_holds_identity`
    question), so the vault account IS the account whose credits the pill must
    show. Any other harness -- the kiro-cli backend above all -- keeps the
    kiro-cli path, so a leftover Crew sign-in never relabels another account's
    usage. Blocking (a config read and a vault read); never raises.
    """
    try:
        # Deferred: kiro_crew.auth pulls the ``cryptography`` wheel, which must
        # not load on the handlers' import path (see kiro_crew.acp.kas_host_auth).
        from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS
        from kiro_crew.auth.bridge import vault_holds_identity
        from kiro_crew.config.loader import KiroCrewConfig

        if KiroCrewConfig.load().agent.acp_backend == ACP_BACKEND_KAS:
            # The spawn's own predicate, with its diagnostics at DEBUG: an unusable
            # entry is a spawn-time WARNING, and this runs on every 30s poll of
            # every open tab.
            return vault_holds_identity(warn=False)
        return False
    except Exception as exc:  # noqa: BLE001 - an unreadable answer keeps the gated kiro-cli path
        logger.debug("Kiro usage: vault ownership probe failed (%s)", type(exc).__name__)
        return False


async def _vault_owns_usage() -> bool:
    """:func:`_vault_owns_usage_sync` off the event loop."""
    return await asyncio.to_thread(_vault_owns_usage_sync)


async def _vault_fingerprint() -> str:
    """The current Crew sign-in's identity fingerprint, read off the loop."""
    # Deferred: see _vault_owns_usage_sync.
    from kiro_crew.auth.bridge import vault_identity_fingerprint

    return await asyncio.to_thread(vault_identity_fingerprint)


async def _cached_reading_is_foreign(payload: dict[str, object], payload_fp: str) -> bool:
    """True when ``payload`` is a reading NOT published for the current Crew sign-in.

    The timed refresh would replace it within ``_USAGE_REFRESH_SECS``, but until
    then a sign-in switch A -> B would serve A's credits under B. Only a cached
    READING counts: an unavailable marker names no account, and treating it as
    foreign would turn a persistent failure into an API call on every poll.

    Judges the SNAPSHOT the caller is about to serve -- the payload and the
    fingerprint it was published under, captured together before any await --
    not the live globals: the fingerprint read below yields, and a refresh that
    publishes B's reading in that window would otherwise make a check against
    the globals pass while the caller still holds A's payload.
    """
    if payload.get("credits_plan") is None:
        return False
    return await _vault_fingerprint() != payload_fp


async def _refresh_from_vault() -> None:
    """Refresh the reading from Crew's own sign-in vault, never from kiro-cli.

    Called only on a :func:`_vault_owns_usage` verdict, so the vault is
    authoritative: every failure -- including the identity vanishing between that
    verdict and this read -- reports unavailable (``signin_required`` when
    auth-class) instead of falling back to kiro-cli, whose readiness gate was
    skipped and whose login may be a different account.

    Identity invariant, mirroring the whoami bracketing on the kiro-cli path: the
    vault identity fingerprint is read immediately before the credential and
    again after the API answers; the reading is published only when both agree.
    A credential failure is judged the same way, against a fingerprint read
    adjacent to the failure rather than the one from before the attempt: the
    refresh waits on the identity's cross-process lock, so a sign-in that
    replaces the account can land while it waits, and judging against ``before``
    would then keep the previous account's reading on screen under the new one.
    """
    # Deferred: see _vault_owns_usage_sync.
    from kiro_crew.auth.bridge import default_token_store
    from kiro_crew.auth.provider import KasAuthProvider, NotAuthenticated
    from kiro_crew.auth.refresh import RefreshBlocked, RefreshRejected

    before = await _vault_fingerprint()
    if not before:
        _cache_vault_failure("", _REASON_SIGNIN_REQUIRED)
        return

    async def _failed(reason: str | None) -> None:
        """Record a credential failure against the identity signed in NOW.

        ``reason`` describes the attempt made for ``before``; when the sign-in
        changed underneath it the failure says nothing about the new account, so
        only the plain unavailable marker is published and the next refresh reads
        the new identity afresh.
        """
        after = await _vault_fingerprint()
        if after != before:
            logger.info(
                "Kiro usage: the Crew sign-in changed during the credential refresh; "
                "discarding the previous identity's reading"
            )
            _cache_vault_failure(after)
            return
        _cache_vault_failure(after, reason)

    try:
        cred = await KasAuthProvider(default_token_store(), allow_env_api_key=False).current()
    except NotAuthenticated:
        _cache_vault_failure("", _REASON_SIGNIN_REQUIRED)
        return
    except (RefreshRejected, RefreshBlocked) as exc:
        # Permanent either way: the issuer refused the grant, or the entry lacks
        # an input the refresh needs (``is_usable`` passed it on the refresh
        # token alone). Nothing changes between polls, so only a new sign-in
        # recovers it, and the pill must say so rather than show a reasonless
        # dash on every poll. Type only: the message can carry response bytes.
        logger.info("Kiro usage: Crew sign-in cannot be refreshed (%s)", type(exc).__name__)
        await _failed(_REASON_SIGNIN_REQUIRED)
        return
    except Exception as exc:  # noqa: BLE001 - a failed refresh is a usage miss, not a crash
        # Transient (issuer 5xx, network after wake): the grant may still be valid,
        # so no sign-in prompt. Type only: a refresh error's message can carry
        # response bytes.
        logger.info("Kiro usage: Crew sign-in refresh failed (%s)", type(exc).__name__)
        await _failed(None)
        return
    result = await asyncio.get_running_loop().run_in_executor(
        subprocess_executor(),
        functools.partial(
            kiro_usage_api.fetch_usage_limits_with_credential,
            cred.access_token,
            cred.profile_arn,
        ),
    )
    after = await _vault_fingerprint()
    if after != before:
        logger.info(
            "Kiro usage: the Crew sign-in changed during the refresh; "
            "discarding the reading fetched for the previous identity"
        )
        _cache_vault_failure(after)
        return
    usage = result.usage
    if usage and usage.get("credits_plan") is not None:
        usage = {k: _redact_strings(v) for k, v in usage.items()}
        usage.pop("_profile_arn", None)
        _publish_usage(usage, vault_fp=before)
        logger.info(
            "Kiro usage refreshed (api, Crew sign-in): %s / %s credits",
            usage.get("credits_used", "?"),
            usage.get("credits_plan", "?"),
        )
        return
    _cache_vault_failure(before, _unavailable_reason(result))


def _publish_account_unproven(identity: dict[str, object], reason: str) -> None:
    """Publish the no-balance marker for an :func:`_account_type_only` sign-in.

    Carries ``identity``'s own public fields (the account type) and nothing of
    an earlier reading: no number this refresh read is published for that shape.
    """
    _publish_usage(
        {
            **{k: _redact_strings(v) for k, v in identity.items() if not k.startswith("_")},
            "available": False,
            "reason": reason,
        }
    )


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


async def _fetch_usage_bg(*, vault_owned: bool = False) -> str | None:
    """Fetch usage and update the cache: the free API first, then the ``/usage``
    scrape whenever the API returns no plan and the scrape is not parked.

    ``vault_owned`` is the caller's :func:`_vault_owns_usage` verdict, taken in
    place of the kiro-cli readiness gate. When true, the reading comes from
    Crew's own sign-in alone (:func:`_refresh_from_vault`) and this refresh never
    resolves or spawns kiro-cli -- not even when that sign-in disappears before
    the refresh reads it, because the gate that would have stopped a signed-out
    kiro-cli was skipped on the strength of this verdict.

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

    A top-of-refresh whoami that reports the ExternalIdP account type and
    nothing naming a user (:func:`_account_type_only`) ends the refresh
    without publishing a reading from either source: no reading could be
    tied to the user, so the cache names the account with no balance and
    ``reason: account_unproven``. ``signin_required`` is published instead only
    on evidence the sign-in itself is broken: the API refused the credential
    (``rejected``), or none was readable (``no_credential``) AND the ``/usage``
    scrape -- run once, for its outcome alone, and parked like any other scrape
    -- fails too. ``no_credential`` alone is not that evidence, exactly as on
    the ordinary path. A timeout or error in this branch leaves the same
    ``account_unproven`` marker when the adjacent whoami is still this shape.

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
    # The top-of-refresh whoami when it took the account-type-only branch, so a
    # timeout or error in that branch leaves that branch's marker (see the
    # handlers below) rather than the ordinary "no reading yet" one.
    type_only_identity: dict[str, object] | None = None

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

        ``kill_and_reap`` kills the whole group, then reaps through a draining
        ``communicate()``: that is what closes the asyncio transport and pipe FDs
        (otherwise they leak, and this runs on a timer), bounded so a wedged
        process cannot reintroduce the unbounded hang. Idempotent:
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
            await kill_and_reap(child, timeout=5)
        except Exception:
            pass

    async def _run_scrape(
        binary: str, before_spawn: Callable[[], Awaitable[None]] | None = None
    ) -> str:
        """Wrap, spawn and read the ``/usage`` scrape; marks the scrape attempted.

        The wrap and the spawn stay in this one function so the spawn's argv
        is visibly the sandboxed, cgroup-scoped one. ``before_spawn`` runs
        after the wrap and immediately before the child starts (the identity
        bracket in ``_refresh``).

        Route through the OS-level sandbox, consistent with how the main agent
        kiro-cli process is spawned (AcpClient._spawn -> wrap_argv) — including
        the TIER. This is a `kiro-cli chat` invocation, so a hardcoded
        "standard" asks for stricter isolation than the very same chat binary
        gets on the interactive path, and fail-closes wherever no backend
        exists. Doubly wasteful here: the refusal also feeds the backoff
        counter that eventually parks the scrape.

        OFF the loop, for two blocking reads: `configured_sandbox_mode()` stats
        (and on a cache miss re-reads + revalidates) config.json, and
        `wrap_argv` -> `detect_backend` can cold-probe the sandbox backend with
        a synchronous `subprocess.run(..., timeout=5)`. The gate above already
        offloads its own config read for the same reason; doing one of the two
        on the loop would leave the freeze this refresh's timer reintroduces
        every interval. Same form and reason as `papyrus/backend/latex._run`.
        """
        nonlocal proc, sandbox_cleanup, scrape_attempted
        scrape_attempted = True
        argv, sandbox_cleanup = await asyncio.get_running_loop().run_in_executor(
            subprocess_executor(),
            _wrap_argv_usage_scrape,
            binary,
        )
        argv = cgroup_scope_argv(argv)  # cgroup DoS ceiling
        if before_spawn is not None:
            await before_spawn()
        proc = await spawn_supervised_oneshot(
            argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=scrub_agent_subprocess_env(),
            # Windows: the gateway has no console, so a helper spawned without
            # CREATE_NO_WINDOW gets a fresh console allocated and flashes on
            # screen ~every 30s. Suppress it (no-op on POSIX, where the flag is
            # 0). Same guard the ACP spawn paths and kiro_prerequisite apply.
            creationflags=_SUBPROCESS_NO_WINDOW,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=60)
        return (out or err or b"").decode(errors="replace")

    async def _account_type_only_reason(auth_state: str, binary: str) -> str:
        """The reason an :func:`_account_type_only` sign-in's marker carries.

        ``rejected`` (the API answered 401 for the credential) is a server-side
        refusal: the remedy is signing in again. ``no_credential`` is not proof
        on its own -- the reader may simply not see the store kiro-cli
        authenticates from, or the stored access token lapsed while kiro-cli sat
        idle with a working refresh token -- so the ``/usage`` scrape runs once
        and its OUTCOME decides: a parseable plan means kiro-cli can still
        authenticate (``account_unproven``), no plan means it cannot
        (``signin_required``). Its number is never read or published, and it
        feeds the scrape back-off like any other attempt; while the scrape is
        parked its repeated failures are the evidence, as on the ordinary path.
        Every other state is ``account_unproven``. The scrape is not bracketed by
        whoami reads because only the reason depends on it, never a number.
        """
        api = kiro_usage_api
        if auth_state == api.AUTH_REJECTED:
            return _REASON_SIGNIN_REQUIRED
        if auth_state != api.AUTH_NO_CREDENTIAL:
            return _REASON_ACCOUNT_UNPROVEN
        if _scrape_in_backoff():
            return _REASON_SIGNIN_REQUIRED
        raw = await _run_scrape(binary)
        works = _parse_usage(raw).get("credits_plan") is not None
        _record_scrape_outcome(works)
        return _REASON_ACCOUNT_UNPROVEN if works else _REASON_SIGNIN_REQUIRED

    def _cache_failure(adjacent: dict[str, object] | None) -> None:
        """Record a timed-out or failed refresh against the adjacent whoami.

        A refresh that took the account-type-only branch, whose adjacent whoami
        is still that shape (or could not be read), leaves that branch's
        no-balance marker: the sign-in is unchanged as far as anything can show,
        and a "no reading yet" marker would promise a reading this sign-in never
        gets. Every other case is :func:`_cache_transient_failure`, as before.
        """
        if type_only_identity is not None and (not adjacent or _account_type_only(adjacent)):
            _publish_account_unproven(adjacent or type_only_identity, _REASON_ACCOUNT_UNPROVEN)
            return
        _cache_transient_failure(adjacent)

    async def _refresh() -> str | None:
        nonlocal kiro_bin, type_only_identity
        global _usage_cache, _usage_cache_ts

        if vault_owned:
            await _refresh_from_vault()
            return None
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
        # The ExternalIdP type and nothing that names the user: no reading could
        # be tied to this user, so nothing the API returns is published and no
        # number is read from the `/usage` scrape (see _account_type_only). The
        # API call still runs for its ``auth_state``, and the scrape runs only
        # when that state alone cannot decide (see _account_type_only_reason):
        # a broken sign-in must keep the ordinary ``signin_required`` remedy,
        # because "this sign-in never shows a balance" would point the user away
        # from the fix, and a working one must not be told to sign in again. The
        # identity fields shown are this whoami's own; no earlier reading is kept.
        if _account_type_only(identity):
            type_only_identity = identity
            type_only_result = await asyncio.get_running_loop().run_in_executor(
                subprocess_executor(),
                functools.partial(kiro_usage_api.fetch_usage_limits, expected_arn=None),
            )
            _publish_account_unproven(
                identity, await _account_type_only_reason(type_only_result.auth_state, kiro_bin)
            )
            logger.info(
                "Kiro usage: the sign-in names a provider and no user; showing the "
                "account without a balance"
            )
            # A corroborating scrape that was the third miss parks the scrape,
            # and the caller learns that as it does on the ordinary path.
            return _parked_by_this_attempt() if scrape_attempted else None
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
        # The scrape reads whichever account kiro-cli has signed in WHEN IT
        # RUNS, so it is bracketed like the API read: one whoami immediately
        # before the spawn (not the top-of-refresh one -- the API attempt and
        # the sandbox wrap sit between) and one immediately after the child
        # returns, before anything is parsed. Only a pair that proves the same
        # account may label and publish what came back between them.
        before: dict[str, object] = {}

        async def _read_before() -> None:
            before.update((await _adjacent_identity()) or {})

        raw = await _run_scrape(kiro_bin, before_spawn=_read_before)
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
        _cache_failure(await _adjacent_identity())
        return _parked_by_this_attempt() if scrape_attempted else None
    except Exception:
        logger.debug("Background usage fetch failed", exc_info=True)
        await _reap_scrape_proc()
        if scrape_attempted:
            _record_scrape_outcome(False)
        _cache_failure(await _adjacent_identity())
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
    #
    # Read on the poll-gate max-age, not the tight destructive bound: a 30s bound
    # matches this endpoint's own 30s poll, so the latch expires at nearly every
    # tick and the request runs a whoami probe inline — the documented-cached
    # read then waits seconds on the probe. The wider window authorizes many
    # polls between probes while still containing the only risk (a browser login
    # spawned on a stale ready=True).
    #
    # A KAS session signed in through Crew reads usage over the direct API and
    # spawns no kiro-cli, so the gate (whose only risk is that spawn) does not
    # apply; the verdict travels into the refresh so it cannot fall back.
    vault_owned = await _vault_owns_usage()
    if not vault_owned:
        blocked = await reject_if_kiro_unverified(request, max_age_secs=_POLL_GATE_MAX_AGE_SECS)
        if blocked is not None:
            return blocked
    # Payload and the fingerprint it was published under, taken in one step (no
    # await between them, and _publish_usage writes both without yielding), so
    # the foreign check below judges exactly what this response serves.
    served, served_fp = _usage_cache, _usage_cache_vault_fp
    refresh_now = False
    if vault_owned:
        if await _cached_reading_is_foreign(served, served_fp):
            # The cached reading belongs to another identity (an earlier Crew
            # sign-in, or the kiro-cli path): never serve it, refresh now.
            served = {"available": False}
            refresh_now = True
    elif _usage_cache.get("credits_plan") is not None and _usage_cache_vault_fp:
        # The vault path published this reading, but the vault does not own
        # usage now (signed out of Crew, or the harness is not KAS): it is not the
        # kiro-cli account's reading, so never serve it, refresh now. Dropped,
        # not just hidden, so the kiro-cli path's same-identity preserve can
        # never keep it and re-trigger this refresh on every poll.
        _publish_usage({"available": False})
        served = _usage_cache
        refresh_now = True
    now = time.time()
    if refresh_now or now - _usage_cache_ts > _USAGE_REFRESH_SECS:
        # Timed refresh only — deliberately not triggered by the kiro-cli auth
        # store changing on disk. That store is shared: `data.sqlite3` holds
        # `conversations`, `history` and `state` alongside `auth_kv`, so ordinary
        # chat traffic rewrites it roughly every 30 seconds; a disk-change trigger
        # would fire on nearly every poll, and each fire can reach the `/usage`
        # text scrape, a minute-scale kiro-cli subprocess. A faster readout is
        # not worth that churn; a profile switch is picked up on the next interval.
        state: DashboardState = request.app["state"]
        task = asyncio.create_task(_fetch_usage_bg(vault_owned=vault_owned))
        state._background_tasks.add(task)
        task.add_done_callback(state._background_tasks.discard)
    return web.json_response({"usage": served})


async def api_sessions_usage_refresh(request: web.Request) -> web.Response:
    """POST /api/sessions/usage/refresh — refresh the credit reading now.

    The account modal's Refresh button. Runs one refresh (the same API-first,
    scrape-second sequence the timer runs) and answers with the cache the GET
    serves -- ``{"usage": <payload>}`` -- so the frontend parses both the same
    way.

    Guards, in order, and why each is here:

    * :func:`reject_if_kiro_unverified` -- the scrape shells out to ``kiro-cli
      chat``, which opens a browser login while signed out (same as the GET).
      Skipped, as on the GET, when :func:`_vault_owns_usage` holds: that
      refresh reads Crew's own sign-in and spawns no kiro-cli.
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
    vault_owned = await _vault_owns_usage()
    if not vault_owned:
        blocked = await reject_if_kiro_unverified(request)
        if blocked is not None:
            return blocked
    if _usage_fetching:
        return web.json_response(
            {"error": "A refresh is already running", "code": "refresh_in_flight"},
            status=409,
        )
    outcome = await _fetch_usage_bg(vault_owned=vault_owned)
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


# ``kiro_crew.dashboard.handlers.sessions`` stays the import path and the patch surface of
# the session routes. This module keeps the usage refresh, whose ``kiro-cli`` spawn resolves
# through the ACP layer, and the health and memory meters; the rest lives in the owner
# modules below, one responsibility each, lowest layer first: the first owner that binds a
# name answers a read of it. ``_facade_mirror`` keeps a patch whole -- a write or delete of
# a name through this module lands in every owner that binds it, and here too when this
# module binds it -- and no owner imports this module at run time.
_OWNERS = (
    "kiro_crew.dashboard.handlers.sessions_parts.usage_scrape",
    "kiro_crew.dashboard.handlers.sessions_parts.app_scope",
    "kiro_crew.dashboard.handlers.sessions_parts.history_reads",
    "kiro_crew.dashboard.handlers.sessions_parts.summaries",
    "kiro_crew.dashboard.handlers.sessions_parts.delete_claim",
    "kiro_crew.dashboard.handlers.sessions_parts.slot_teardown",
    "kiro_crew.dashboard.handlers.sessions_parts.delete_routes",
    "kiro_crew.dashboard.handlers.sessions_parts.live_turn",
    "kiro_crew.dashboard.handlers.sessions_parts.tool_policy",
    "kiro_crew.dashboard.handlers.sessions_parts.restart",
)
for _owner in _OWNERS:
    _importlib.import_module(_owner)
# The three routes that verify and scope their own caller, bound here so the guard at the
# bottom of this module can name them as the ones it leaves unwrapped.
from kiro_crew.dashboard.handlers.sessions_parts.live_turn import (  # noqa: E402, F401
    api_session_directive,
    api_session_keepalive,
)
from kiro_crew.dashboard.handlers.sessions_parts.tool_policy import (  # noqa: E402, F401
    api_session_tool_policy,
)

#: Name this module serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(_facade_mirror.install(_sys.modules[__name__], _OWNERS, excluded=()).served)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
#: What a star import binds: every public name of the family, as the one module bound it.
__all__ = sorted(n for n in set(globals()) | set(_EXPORTS) if not n.startswith("_"))


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
