"""Shared LLM interaction helpers — stream collection, JSON parsing, history saving.

Eliminates duplicate code across gateway, handler, dashboard, taskrunner,
subagent, and history modules.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from enum import Enum
from typing import TYPE_CHECKING, Any

from kiro_crew import name_grant
from kiro_crew.acp.client import AcpError, AcpPromptBusy, advertised_model_ids
from kiro_crew.acp.types import EVENT_STEER_CONSUMED, TurnUsage
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.hooks import fire_tool_hooks, get_global_hook_store
from kiro_crew.platform.context import redact_via_context
from kiro_crew.providers.base import (
    EVENT_COMPLETE,
    EVENT_PERMISSION_REQUEST,
    EVENT_TEXT_CHUNK,
    EVENT_TOOL_CALL,
    LLMEvent,
    LLMProvider,
    resolve_billing_stats,
)
from kiro_crew.security import (
    MAX_SCANNABLE_COMMAND_CHARS,
    is_denied,
    is_sensitive_bash_command,
    is_sensitive_path,
    redact_credentials,
    redact_exfiltration_urls,
)
from kiro_crew.sel import sel as _sel

_PROMPT_BUSY_RETRIES = 2
_PROMPT_BUSY_DELAY = 1.5  # seconds between retries

# Cap on the wrapper chain walked to find a turn's billing stats. A provider is
# wrapped at most a few layers deep, so a longer walk means a cycle or an
# unrelated object graph, not a deeper seam.
_WRAPPER_WALK_MAX_NODES = 8

# Sentinel for "no prior stats object was observed", distinct from a provider that
# legitimately exposes None. Used by provider_last_turn_usage's identity guard.
_NO_PRIOR_STATS = object()

# Where stream_and_collect hands its caller the billing it observed across ALL
# attempts of one logical turn. A retry installs fresh per-turn stats, so a
# post-hoc read of last_prompt_stats sees only the final attempt and silently
# drops an attempt that was billed before a transient error. This carries the sum
# instead, and provider_last_turn_usage consumes it. The value is a
# (stats object, TurnUsage) pair: the provider outlives the turn, so the identity
# of the stats the sum was computed from is what tells a later turn that the sum
# is not its own.
_TURN_BILLED_ATTR = "_kc_turn_billed"

# Transient backend (Bedrock 5xx / throttle / stream-reset) retry budget. These
# are server-side hiccups where the credential is VALID — retry helps, re-auth
# does not. Kept separate from the prompt-busy budget above.
_TRANSIENT_RETRIES = 3
_TRANSIENT_DELAY = 2.0  # base seconds; exponential backoff + jitter

# Per-process RNG for retry jitter, auto-seeded from os.urandom at import. The
# entropy seed spreads jitter uniformly *across* processes/machines — so a
# fleet-wide transient (e.g. several gateways hitting the same backend 5xx at the
# same minute) doesn't retry in lockstep and re-thunder the recovering backend.
# Auto-seeding (rather than os.getpid()) is container-safe: under Docker/ECS/K8s
# the gateway is commonly PID 1, so a PID seed would be identical fleet-wide and
# collapse the spread. Tests that need determinism patch asyncio.sleep (so the
# jitter value is never observed) or reseed _JITTER_RNG in a fixture.
_JITTER_RNG = random.Random()

# Substrings (lowercased) that mark a RETRYABLE transient backend failure.
# Matched against the formatted AcpError message (see acp.client._format_acp_error).
# Auth/validation markers are deliberately ABSENT so those fail fast — a retry
# cannot fix an expired token or a bad request, and silently retrying them would
# only delay the correct "re-auth"/"fix the request" signal to the operator.
_TRANSIENT_MARKERS = (
    "internal server error",
    "internal error: api error",
    "serviceunavailable",
    "service unavailable",
    "throttl",  # ThrottlingException + "Bedrock is throttling"
    "toomanyrequests",
    "servicequotaexceeded",
    "modelstreamerror",
    "connection reset",
    "connectionreset",
    "dispatch failure",  # AWS SDK connector-level I/O failure (conn/DNS/TLS drop)
    "dispatchfailure",  # Rust DispatchFailure variant (unspaced)
    # Model-unavailable capacity/rollout, matched against _format_acp_error's
    # wording. Two phrasings are listed: the current "on the backend" text
    # (#1550) and the pre-2026-08 "on Bedrock" one, so a transcript or log line
    # written by an older gateway still classifies. Any future rewording of
    # that branch must add its marker here.
    #
    # Deliberately does NOT cover the sibling unentitled-model branch: that one
    # is terminal by design (_model_is_unentitled), so a marker matching it
    # would resurrect the pointless retry loop #1550 removed.
    "is unavailable on the backend",
    "is unavailable on bedrock",
    # kiro-cli >= 2.16 nameless capacity wording ("The model you've selected
    # is temporarily unavailable..."). The formatter now rewrites it into the
    # "on the backend" prose above, but a transcript or history line written
    # by a pre-rewrite gateway carries the raw passthrough — this marker keeps
    # those classifying. Deliberately skips the "you've" apostrophe so
    # straight and typographic quotes both match the substring.
    "selected is temporarily unavailable",
    "transient error (http 5xx)",  # _format_acp_error's generic-5xx message
)


def _is_transient_acp_error(msg: str) -> bool:
    """True iff an AcpError message looks like a retryable transient backend
    failure. Auth failures are explicitly excluded (they need re-auth, not retry)."""
    low = msg.lower()
    if (
        "authentication failed" in low
        or "accessdenied" in low
        or "expiredtoken" in low
        or "unrecognizedclient" in low
        or "invalidsignature" in low
    ):
        return False
    return any(m in low for m in _TRANSIENT_MARKERS)


# ── Public reuse surface for callers with their own stream loop ──
#
# stream_and_collect owns the transient retry for unattended callers, but the
# interactive dashboard/Slack path (dashboard.chat_runner) consumes the ACP
# stream directly and cannot funnel through stream_and_collect. These thin
# wrappers let it reuse the SAME classifier, retry budget, and backoff curve —
# one source of truth, no duplicated heuristics.

# Public name for the transient retry budget (number of retries after the
# initial attempt). Re-exported so external callers don't import the private.
TRANSIENT_RETRIES = _TRANSIENT_RETRIES


def is_transient_backend_error(msg: str) -> bool:
    """True iff *msg* (a formatted AcpError string) is a retryable transient
    backend failure (5xx / throttle / stream-reset) rather than an
    auth/validation error. Public alias of :func:`_is_transient_acp_error`."""
    return _is_transient_acp_error(msg)


def acp_error_is_transient(exc: BaseException) -> bool:
    """Authoritative retry-eligibility decider for an ACP error.

    Prefers the structured verdict carried on ``AcpError.transient`` — classified
    from the RAW JSON-RPC error at raise time (see
    ``acp.client._is_transient_raw_error``) — so the retry decision is
    independent of how the user-facing message is worded. Falls back to
    string-matching the formatted message for exceptions raised without the flag
    (legacy raise paths, non-``AcpError`` exceptions, tests).

    The formatted message may rewrite a generic 5xx into a friendly string that
    the marker-based string classifier alone does not recognise; relying on the
    structured flag keeps that case retryable."""
    flag = getattr(exc, "transient", None)
    if isinstance(flag, bool):
        return flag
    return is_transient_backend_error(str(exc))


def transient_retry_delay(attempt: int) -> float:
    """Backoff delay (seconds) for the *attempt*-th (1-based) transient retry.

    Exponential (base ``_TRANSIENT_DELAY``, doubling per attempt) plus
    per-process jitter, so every caller that retries transient backend errors
    backs off on the identical curve and co-located peers don't retry in
    lockstep (see ``_JITTER_RNG``)."""
    base = _TRANSIENT_DELAY * (2 ** (attempt - 1))
    return base + _JITTER_RNG.random() * 0.25 * base


def first_advertised_fallback(advertised: Any, rejected: str | None) -> str | None:
    """First advertised model that is neither *rejected* nor ``"auto"``.

    Used as the reactive replacement when the configured model (often ``"auto"``)
    is refused mid-prompt — e.g. on a GovCloud partition that does not serve the
    ``"auto"`` sentinel.  Shared by :func:`run_bg_oneliner` and
    :func:`stream_and_collect` so every background LLM path has the same
    fallback behaviour.
    """
    rej = (rejected or "").strip().lower()
    for m in advertised or []:
        if not isinstance(m, str) or not m.strip():
            continue
        low = m.strip().lower()
        if low == rej or low == "auto":
            continue
        return m
    return None


# ── Throttle-exhaustion fallback chain (agent.fallback_model) ──
#
# When the active model's same-model transient budget (_TRANSIENT_RETRIES)
# exhausts on a throttle/capacity error, an ordered chain of fallback models is
# tried instead of surfacing the error. This lives entirely on the Kiro Crew
# side (kiro-cli
# has no fallback mechanism) and is NEVER silent: every swap is logged at
# warning, published on the provider (TURN_FALLBACK_ATTR) so the delivering
# surface can prepend a visible notice, and — on the interactive path —
# announced in chat (see dashboard/chat_runner). An empty chain (the default)
# disables the feature: behavior is byte-for-byte the pre-feature error surface.

# Attempts per fallback candidate: initial + ONE ~2s retry — deliberately NOT a
# fresh _TRANSIENT_RETRIES budget. Throttle-exhaustion events are often
# cell-scoped and model-agnostic (a frontend admission-tier outage takes out
# ALL models in the cell), so deep per-candidate retries mostly re-confirm a
# correlated outage slowly. One retry covers the uncorrelated single-shot 5xx
# on a healthy candidate while keeping the worst case bounded (~35s of backoff
# across a 4-candidate chain).
FALLBACK_CANDIDATE_ATTEMPTS = 2


def fallback_rewound_transient_budget() -> int:
    """Same-model counter value that grants a fresh fallback candidate its budget.

    The dashboard's interactive ladder does not hold a :class:`FallbackState`
    between turns — after a swap it rewinds ``slot._transient_5xx_retries`` so
    the candidate gets exactly :data:`FALLBACK_CANDIDATE_ATTEMPTS` - 1 further
    passes through the same-model retry branch (the re-queued turn itself is
    the first attempt) before the next exhaustion advances the chain. Deriving
    the rewind here keeps the per-candidate budget in ONE place with
    :meth:`FallbackState.should_retry_active`, the encoding the unattended
    surfaces use. Clamped at zero so the counter can never go negative:
    a FALLBACK_CANDIDATE_ATTEMPTS above TRANSIENT_RETRIES + 1 cannot be
    expressed by this counter encoding at all — it collapses to the full
    same-model budget (the documented "deliberately not a fresh full budget"
    stance caps the useful range at TRANSIENT_RETRIES + 1).
    """
    return max(0, TRANSIENT_RETRIES - (FALLBACK_CANDIDATE_ATTEMPTS - 1))


# Provider attribute carrying the active fallback as ``(primary, candidate)``.
# Doubles as (a) the sticky-restore marker — the next stream_and_collect call
# on the same provider probes one ``set_model(primary)`` restore — and (b) the
# visibility source for unattended surfaces (cron/heartbeat read it after the
# turn and prepend a warning line to the delivered result). Cleared on a
# successful restore, never on turn completion: the swap is sticky for the
# remainder of the session by design.
TURN_FALLBACK_ATTR = "_kc_active_fallback"

# Exception attribute carrying the chain-exhaustion story (set by the fallback
# walks when every candidate also failed). The delivering surface appends it to
# the terminal error text via :func:`append_fallback_story` so an unattended
# failure names the whole walk, not just the last candidate's error.
FALLBACK_STORY_ATTR = "_kc_fallback_story"

# Bound on the story text a consumer will accept. The walked ids originate in
# ``agent.fallback_model`` config (LLM-reachable via MCP) and the advertised
# check fails OPEN on an empty list, so an arbitrarily long config string can
# reach the walk — bound and redact it centrally before it rides any error
# surface (WS frames, Slack alerts, log lines).
_FALLBACK_STORY_CAP = 500


def fallback_story_of(exc: BaseException) -> str:
    """The chain-exhaustion story carried on *exc*, redacted+capped, or ``""``.

    Reads :data:`FALLBACK_STORY_ATTR`; anything but a non-empty string is
    treated as absent (the attribute is best-effort — a frozen exception type
    may have refused the set, and a hostile ``__getattribute__`` must not
    break error delivery). Redaction and the :data:`_FALLBACK_STORY_CAP`
    bound live HERE so every consumer (cron alert, sub-agent error, heartbeat
    log) gets the same safe text — no per-surface drift. Never raises.
    """
    try:
        story = getattr(exc, FALLBACK_STORY_ATTR, None)
        if not isinstance(story, str) or not story:
            return ""
        story = redact_credentials(redact_exfiltration_urls(story)[0])[0]
    except Exception:  # noqa: BLE001 — a story must never break error delivery
        logger.debug("fallback story read/redaction failed", exc_info=True)
        return ""
    return story[:_FALLBACK_STORY_CAP]


def append_fallback_story(text: str, exc: BaseException, *, budget: int | None = None) -> str:
    """Append *exc*'s chain-exhaustion story to terminal error *text*.

    THE consumer for :data:`FALLBACK_STORY_ATTR` on unattended surfaces
    (cron failure alerts, sub-agent ``info.error``, the heartbeat failure
    log). The interactive dashboard does not use it — it rebuilds a richer
    story from ``slot._fallback_walked``. No story ⇒ *text* is returned
    unchanged (capped at *budget* when one is given). The story arrives
    redacted+capped from :func:`fallback_story_of`. Never raises.

    :param budget: optional total length bound for the composite. The ERROR
        text is trimmed to leave the story room — the story is the part a
        verbose backend error must never push out — but keeps a FLOOR of half
        the budget: an oversized story (config-sourced ids can approach
        :data:`_FALLBACK_STORY_CAP`, which may equal a caller's cap) must not
        evict the actual error either, so past the floor it is the story tail
        that truncates. Degenerate budgets (a handful of characters — no real
        caller passes one) keep the error head and may lose the story
        entirely. ``None`` appends unbounded (caller owns the cap); a
        negative value is normalized to 0 (a negative slice would DROP the
        bound instead of tightening it).
    """
    if budget is not None and budget < 0:
        budget = 0
    story = fallback_story_of(exc)
    if not story:
        return text if budget is None else text[:budget]
    if budget is not None:
        # Reserve " [" + story + "]" out of the budget, but never trim the
        # error text below half the budget; the final cap then truncates the
        # story tail instead.
        text = text[: max(budget // 2, budget - len(story) - 3)]
    out = f"{text} [{story}]" if text else story
    return out if budget is None else out[:budget]


def annotate_model_fallback(text: str, provider: Any) -> str:
    """Prepend the throttle-fallback warning to a delivered unattended result.

    Unattended surfaces (cron/heartbeat results, the sub-agent completion
    event) have no chat card to announce a fallback swap on, so the delivered
    result text itself carries the warning — the same visibility contract as
    the interactive notice card. The marker is read from
    :data:`TURN_FALLBACK_ATTR` (set by the shared fallback walk) and left in
    place: the swap is sticky for the session, so every run served by the
    fallback repeats the warning until the restore probe moves the session
    back. Model ids come from config, which is LLM-reachable via MCP — redact
    before they reach Slack/dashboard. One body for every surface (was two
    spellings: ``slack/gateway`` + an inline block in ``subagent``). Never
    raises: an annotation failure must not turn a successful run into a
    failed one — the un-annotated *text* is returned instead.
    """
    try:
        fb = getattr(provider, TURN_FALLBACK_ATTR, None)
        if not fb:
            return text
        primary, candidate = fb
        # Same threat model as _FALLBACK_STORY_CAP: the ids originate in
        # config (LLM-reachable via MCP), so bound them as well as redacting.
        safe_primary = redact_credentials(redact_exfiltration_urls(str(primary))[0])[0][
            :_FALLBACK_STORY_CAP
        ]
        safe_candidate = redact_credentials(redact_exfiltration_urls(str(candidate))[0])[0][
            :_FALLBACK_STORY_CAP
        ]
        line = (
            f"⚠️ Model '{safe_primary}' throttled; this run was served by fallback "
            f"'{safe_candidate}'."
        )
        return f"{line}\n\n{text}" if text else line
    except Exception:  # noqa: BLE001 — annotation is best-effort visibility
        logger.debug("fallback annotation failed", exc_info=True)
        return text


def provider_fallback_active(provider: Any) -> bool:
    """True while *provider* carries an active fallback marker.

    THE shared usage-attribution guard: while a fallback serves the session,
    an explicit model pin (``job.model`` / ``info.model`` / ``slot.model``)
    must NOT be recorded as the turn's model — the durable usage row would
    bill the fallback's spend to a model that never executed. Callers blank
    the explicit value when this is true (mirroring the ``_seq_downgraded``
    precedent), deferring to ``model_source`` — which reads the model that
    actually ran.
    """
    marker = getattr(provider, TURN_FALLBACK_ATTR, None)
    return isinstance(marker, (tuple, list)) and len(marker) >= 2


def next_fallback_candidate(
    chain: Sequence[str],
    active_model: str,
    advertised: Sequence[str] | None,
) -> str | None:
    """First usable fallback candidate from *chain*, or ``None``.

    Skips empties, the currently-active model (a chain entry equal to what is
    already failing cannot help), and — when an advertised list is known — any
    id the backend did not advertise (unentitled/unknown). ``"auto"`` is a
    legitimate candidate (the backend's availability-aware routing) and is
    filtered by the same advertised check: a partition that does not serve
    ``"auto"`` skips it rather than sending a no-op swap. An EMPTY advertised
    list fails OPEN (candidates accepted): entitlement unknown is not
    entitlement denied, matching ``model_is_unusable``'s stance, and the
    substitute ``set_model`` path re-validates against the live list anyway.
    """
    adv = {a.strip().lower() for a in (advertised or []) if isinstance(a, str) and a.strip()}
    act = (active_model or "").strip().lower()
    for cand in chain:
        if not isinstance(cand, str):
            continue
        low = cand.strip().lower()
        if not low or low == act:
            continue
        if adv and low not in adv:
            logger.debug("model fallback: skipping %r (not advertised)", cand)
            continue
        return cand
    return None


@dataclass
class FallbackState:
    """Walk state for one logical turn's fallback-chain traversal.

    ``pos`` is the next chain index to consider (monotonic — a candidate is
    never revisited), ``active`` the candidate currently being attempted,
    ``attempts`` how many attempts the active candidate has consumed (capped at
    :data:`FALLBACK_CANDIDATE_ATTEMPTS`), ``primary`` the model that was active
    when the chain walk started, and ``walked`` every candidate actually tried
    (for the chain-exhausted error story).
    """

    chain: tuple[str, ...]
    pos: int = 0
    active: str | None = None
    attempts: int = 0
    primary: str = ""
    walked: list[str] = dataclass_field(default_factory=list)

    def next_candidate(self, active_model: str, advertised: Sequence[str] | None) -> str | None:
        """Advance to and return the next usable candidate, or ``None``."""
        remaining = self.chain[self.pos :]
        cand = next_fallback_candidate(remaining, active_model, advertised)
        if cand is None:
            self.pos = len(self.chain)
            return None
        self.pos += remaining.index(cand) + 1
        return cand

    def should_retry_active(self) -> bool:
        """Consume one more attempt on the active candidate, if budget remains.

        THE single home of the per-candidate retry budget/trigger (was three
        spellings: ``stream_and_collect`` Case 2.75, the sub-agent ladder, and
        the dashboard's counter rewind — the last derives its counter from the
        same constant via :func:`fallback_rewound_transient_budget`). ``True``
        means the caller retries the active candidate once more (the attempt is
        already recorded); ``False`` means no candidate is active or its
        :data:`FALLBACK_CANDIDATE_ATTEMPTS` budget is spent — advance the chain
        via :func:`advance_fallback_candidate`.
        """
        if self.active is None or self.attempts >= FALLBACK_CANDIDATE_ATTEMPTS:
            return False
        self.attempts += 1
        return True

    def exhaustion_story(self) -> str | None:
        """One-line story of a spent chain walk, or ``None`` when none ran.

        ``None`` (nothing was actually walked — e.g. every candidate was
        skipped as unadvertised) means the error should surface exactly as it
        did before the fallback feature existed, with no story attached.
        """
        if not self.walked:
            return None
        return (
            f"{self.primary or 'the selected model'} throttled; "
            f"fallbacks {', '.join(self.walked)} also unavailable"
        )


async def advance_fallback_candidate(
    provider: Any,
    fb_state: "FallbackState",
    *,
    surface: str,
    log_suffix: str = "",
) -> str | None:
    """One chain-walk step — THE shared advance used by every fallback surface.

    ``stream_and_collect`` (Case 2.75), the sub-agent transient ladder, and the
    dashboard's ``_fallback_swap_for_turn`` all advance the chain through this
    single body so throttle classification, marker semantics, and skip rules
    cannot diverge across surfaces. It: seeds ``fb_state.primary`` from a
    surviving sticky marker first (a session already on a fallback whose true
    primary only the marker remembers) and the active model second; walks the
    chain skipping the primary, unadvertised ids, and the currently-active
    (failing) candidate; applies the first candidate whose substitute
    ``set_model`` lands; publishes the sticky marker
    (:data:`TURN_FALLBACK_ATTR`); and emits the greppable swap warning.
    Returns the applied candidate, or ``None`` when the chain is exhausted or
    the provider exposes no ``set_model`` seam — the caller then surfaces the
    original error exactly as before this feature existed.
    """
    advertised = provider_advertised_ids(provider)
    # An empty read means the session is auto-routed (``provider_active_model``
    # deliberately filters the ``"auto"`` sentinel) or genuinely unknown; either
    # way ``"auto"`` is the honest primary. Seeding it (a) makes the restore
    # probe re-enter auto routing instead of hitting the dashboard's
    # stale-clear arm with an empty primary (which would let the backfill pin
    # the fallback permanently), (b) suppresses the auto->auto no-op swap via
    # the active-skip below, and (c) keeps the notice card naming a real
    # primary instead of a placeholder.
    active = provider_active_model(provider) or (fb_state.active or "") or "auto"
    if not fb_state.primary:
        _marker = getattr(provider, TURN_FALLBACK_ATTR, None)
        _marker_primary = ""
        if isinstance(_marker, (tuple, list)) and _marker and isinstance(_marker[0], str):
            _marker_primary = _marker[0].strip()
        fb_state.primary = _marker_primary or active
    set_model_fn = resolve_substitute_set_model(provider)
    if set_model_fn is None:
        return None
    while True:
        cand = fb_state.next_candidate(fb_state.primary or active, advertised)
        if cand is None:
            return None
        if cand.strip().lower() == (active or "").strip().lower():
            # With a marker-seeded primary, the chain can still name the
            # CURRENTLY-failing fallback the session sits on — retrying it is
            # what this walk exists to escape.
            continue
        _raw_before = provider_raw_model(provider)
        try:
            await set_model_fn(cand)
        except Exception:
            logger.debug(
                "model fallback: set_model(%r) failed; skipping candidate",
                cand,
                exc_info=True,
            )
            continue
        # Witness the swap before publishing: a non-raising set_model can be a
        # silent no-op (resolve_usable_model collapses an unservable target to
        # "" and returns without switching). Publishing an unwitnessed swap
        # would announce a model that never took over and retry the same
        # failing model. Only enforceable when the model attribute is readable
        # (a provider exposing no model string cannot be witnessed — fail open,
        # matching the pre-existing trust in set_model for such providers).
        _raw_after = provider_raw_model(provider)
        if (
            _raw_before
            and _raw_after == _raw_before
            and _raw_after.strip().lower() != cand.strip().lower()
        ):
            logger.debug(
                "model fallback: set_model(%r) was a silent no-op (model still %r); "
                "skipping candidate",
                cand,
                _raw_after,
            )
            continue
        fb_state.active = cand
        fb_state.attempts = 1
        fb_state.walked.append(cand)
        try:
            setattr(provider, TURN_FALLBACK_ATTR, (fb_state.primary, cand))
        except Exception:
            logger.debug("publishing fallback marker failed", exc_info=True)
        logger.warning(
            "model fallback: %s -> %s (reason=throttle-exhaustion, surface=%s%s)",
            fb_state.primary or "?",
            cand,
            surface,
            log_suffix,
        )
        return cand


def resolve_substitute_set_model(provider: Any) -> Callable[[str], Awaitable[None]] | None:
    """The provider's substitute-path ``set_model`` coroutine, or ``None``.

    Prefers ``provider.set_model``; falls back to the wrapped client
    (``provider.client`` / ``provider._client``) for wrappers like
    ``AcpProvider`` that do not re-export it. Callers pre-filter candidates
    against the advertised list, so the explicit-pick guard inside
    ``AcpClient.set_model`` / ``AcpSessionProvider.set_model`` does not fire
    for a served candidate.
    """
    try:
        fn = getattr(provider, "set_model", None)
    except Exception:  # pragma: no cover - exotic property getters
        fn = None
    if callable(fn):
        return fn
    for attr in ("client", "_client"):
        try:
            inner = getattr(provider, attr, None)
        except Exception:  # pragma: no cover - exotic property getters
            inner = None
        try:
            fn = getattr(inner, "set_model", None) if inner is not None else None
        except Exception:  # pragma: no cover - exotic property getters
            fn = None
        if callable(fn):
            return fn
    return None


def provider_advertised_ids(provider: Any) -> list[str]:
    """Advertised model ids from the provider, ``[]`` when unknown."""
    getter = getattr(provider, "available_models", None)
    if not callable(getter):
        return []
    try:
        return advertised_model_ids(getter())
    except Exception:
        return []


def provider_active_model(provider: Any) -> str:
    """The model currently serving the provider's session, ``""`` if unknown."""
    for attr in ("served_model", "_model"):
        try:
            val = getattr(provider, attr, "")
        except Exception:  # pragma: no cover - exotic property getters
            val = ""
        if isinstance(val, str) and val.strip() and val.strip().lower() != "auto":
            return val.strip()
    return ""


def provider_raw_model(provider: Any) -> str:
    """The provider's raw model attribute, ``"auto"`` INCLUDED, ``""`` if unknown.

    The witness reader for fallback state transitions: unlike
    :func:`provider_active_model` (which filters the ``"auto"`` sentinel for
    walk semantics), this reports the attribute verbatim so a caller can
    observe whether a non-raising ``set_model`` actually moved the session.
    ``resolve_usable_model`` collapses an unservable target to ``""`` and
    ``set_model`` then returns WITHOUT switching — treating "didn't raise" as
    "switched" is what let a no-op restore clear sticky state while still on
    the fallback (and the backfill then pinned it permanently).
    """
    for attr in ("served_model", "_model"):
        try:
            val = getattr(provider, attr, "")
        except Exception:  # pragma: no cover - exotic property getters
            val = ""
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


async def probe_fallback_restore(
    provider: Any,
    *,
    surface: str = "unattended",
    state: tuple[Any, Any] | None = None,
    stale: bool = False,
    clear: Callable[[], None] | None = None,
    on_restored: Callable[[], None] | None = None,
    log_suffix: str = "",
) -> None:
    """One ``set_model(primary)`` restore probe at the start of a turn.

    THE single restore-probe body: the unattended surfaces call it bare (state
    read from :data:`TURN_FALLBACK_ATTR`), and the dashboard's slot probe wraps
    it (``chat_runner._probe_fallback_restore_for_slot_locked``) with the
    slot-held state and hooks below, so the probe/witness/clear sequencing
    cannot diverge across surfaces. The restore only fires while the session is
    still on the fallback this feature set (a user/session-level model change
    in between clears the sticky state without touching the model — never
    override an explicit later pick). Success clears the state and logs
    (recovery is the quiet default: no chat notice); failure keeps the fallback
    for this turn. Never raises.

    :param state: ``(primary, candidate)`` override. Default: read the
        provider's :data:`TURN_FALLBACK_ATTR` marker (no-op when absent).
    :param stale: caller-known extra staleness the marker cannot see (the
        dashboard's explicit-pick generation check) — treated exactly like the
        session having moved off the fallback.
    :param clear: replaces the default marker clear (the dashboard drops slot
        fields AND the marker as one logical record).
    :param on_restored: hook running after a witnessed restore, before *clear*
        (the dashboard's slot-model heal).
    :param log_suffix: appended inside the log parentheses, e.g. ``", slot=k"``.
    """
    try:
        # The whole state read is guarded: a hostile marker property, a
        # raising ``__bool__``/``__str__``, or a malformed tuple must not
        # break the "never raises" contract — an unreadable state simply
        # skips the probe.
        if state is None:
            marker = getattr(provider, TURN_FALLBACK_ATTR, None)
            if not marker:
                return
            primary, candidate = marker
        else:
            primary, candidate = state
            if not candidate:
                return
        # Coerce ONCE; every later comparison uses the plain string. The
        # marker path deliberately has no empty-candidate early return (a
        # ``(primary, "")`` marker still probes and clears — pre-existing
        # semantics), while the state path mirrors the slot probe's
        # no-active-fallback no-op.
        candidate = "" if candidate is None else str(candidate)
        primary_missing = not primary
    except Exception:
        logger.debug("fallback restore: unreadable fallback state; skipping probe", exc_info=True)
        return

    def _default_clear() -> None:
        try:
            setattr(provider, TURN_FALLBACK_ATTR, None)
        except Exception:
            pass

    def _run_hook(fn: Callable[[], None], what: str) -> None:
        # Caller-supplied hooks must not break the never-raises contract: a
        # failing heal/clear aborts the TURN it runs at the start of, which is
        # far worse than the stale state it was tidying.
        try:
            fn()
        except Exception:
            logger.debug("fallback restore %s hook failed", what, exc_info=True)

    _clear = clear if clear is not None else _default_clear
    current = provider_active_model(provider)
    if current and candidate and current.strip().lower() != candidate.strip().lower():
        # The session moved off our fallback by other means (explicit pick,
        # session reset). The sticky state is stale — drop it, restore nothing.
        _run_hook(_clear, "clear")
        return
    if stale or primary_missing:
        _run_hook(_clear, "clear")
        return
    set_model_fn = resolve_substitute_set_model(provider)
    if set_model_fn is None:
        return
    try:
        await set_model_fn(primary)
    except Exception as exc:
        logger.info(
            "model fallback: primary %s still unavailable (%s); staying on %s " "(surface=%s%s)",
            primary,
            exc,
            candidate,
            surface,
            log_suffix,
        )
        return
    # Witness the restore before clearing: a non-raising set_model(primary)
    # can be a silent no-op (e.g. an "auto" primary on a partition that
    # stopped advertising it resolves to "" and returns without switching).
    # Clearing the marker while still on the fallback would let the next
    # backfill pin the temporary fallback permanently. Keep the marker and
    # retry at the next turn start instead.
    _raw = provider_raw_model(provider)
    if _raw and candidate and _raw.strip().lower() == candidate.strip().lower():
        logger.info(
            "model fallback: restore to %s was a silent no-op (still on %s); "
            "keeping fallback (surface=%s%s)",
            primary,
            candidate,
            surface,
            log_suffix,
        )
        return
    if on_restored is not None:
        # A failed heal does not block the clear: the two writes are one
        # logical record, a half-cleared record is worse than a missed heal,
        # and the stale-state paths above never heal either — retaining the
        # record would not buy a retry of the heal. The dashboard's actual
        # hooks are plain attribute writes that realistically cannot raise;
        # this guard is for the "never raises" contract, not an expected path.
        _run_hook(on_restored, "on_restored")
    _run_hook(_clear, "clear")
    logger.warning(
        "model fallback: restored %s -> %s (reason=primary-recovered, surface=%s%s)",
        candidate,
        primary,
        surface,
        log_suffix,
    )


def configured_fallback_chain() -> tuple[str, ...]:
    """The throttle-fallback chain derived from ``agent.fallback_model``, or ``()``.

    The config is a SINGLE value; the walk order is derived here (the one
    derivation every surface shares): ``""`` disables the feature everywhere
    (``()`` — callers pass it straight to ``fallback_models=`` and Case 2.75
    stays inert); ``"auto"`` (the default) yields ``("auto",)`` — defer to the
    backend's availability-aware routing; a concrete id yields
    ``(id, "auto")`` — the pinned fallback first, ``"auto"`` as the final
    fallthrough (the backend routes to whatever is actually available).

    ``KiroCrewConfig.load()`` is fingerprint-cached (mtime/size/mode of both
    config files), so the steady-state cost is two stats — the same read the
    interactive turn path already performs inline on the event loop every
    turn.
    """
    try:
        fm = KiroCrewConfig.load().agent.fallback_model
    except Exception:
        return ()
    if not fm:
        return ()
    if fm == "auto":
        return ("auto",)
    return (fm, "auto")


class PromptBusyExhaustedError(Exception):
    """Provider was shut down after prompt-busy retries were exhausted."""


if TYPE_CHECKING:
    from kiro_crew.history import ConversationLog
    from kiro_crew.hooks import HookManager

logger = logging.getLogger(__name__)


def record_interaction_event(client: LLMProvider, session_key: str, surface: str) -> None:
    """Record one per-interaction telemetry event via the PlatformContext seam.

    The Default ``TelemetryProvider.record_event`` is a no-op, so standalone is
    unchanged; a companion records one event per successful turn. Payload is
    strictly metadata (session key, surface, model) — never prompt/response text
    or file contents. Best-effort: a telemetry failure never affects the turn.

    Shared by every surface (dashboard, Slack) so the payload shape and the
    model-extraction reflection cannot drift between call sites.
    """
    from kiro_crew.platform import current_context

    try:
        # Resolve the active model across backend shapes. After Kiro startup the
        # provider's ``_client`` is an ``AcpSessionProvider`` that exposes the
        # model via a ``model`` property (backed by ``_handle.model``); before
        # startup / for the raw client it is the ``_model`` attribute. Try the
        # property first, then the raw attr, on the inner client then the outer.
        inner = getattr(client, "_client", client)
        model = ""
        for obj in (inner, client):
            model = getattr(obj, "model", "") or getattr(obj, "_model", "") or ""
            if model:
                break
        current_context().telemetry.record_event(
            "interaction",
            {"session_key": session_key, "surface": surface, "model": model},
        )
    except Exception:
        logger.debug("telemetry.record_event(interaction) failed", exc_info=True)


def _extract_tool_input_strings(tool_input: str) -> list[str]:
    """Extract all string values from a JSON tool_input for security scanning.

    Recursively walks nested dicts and lists to find all string values,
    ensuring sensitive paths in nested structures like
    ``{"args": {"path": "~/.aws/credentials"}}`` are not missed.

    Handles dict, list, plain-string, and malformed JSON gracefully. On
    parse failure, returns the raw string itself as the single candidate.
    """
    if not tool_input:
        return []
    try:
        parsed = json.loads(tool_input)
    except ValueError:
        # Not JSON — treat the raw string as a path/command candidate
        return [tool_input]
    if isinstance(parsed, str):
        return [parsed]

    results: list[str] = []

    def _collect(obj: object) -> None:
        if isinstance(obj, str) and obj:
            results.append(obj)
        elif isinstance(obj, dict):
            for v in obj.values():
                _collect(v)
        elif isinstance(obj, list):
            for item in obj:
                _collect(item)

    _collect(parsed)
    return results


# Longest single string the tool_input scan will attempt.
#
# The scan runs on a worker thread, but CPython's ``re`` HOLDS the GIL for the
# whole of one match call (measured: a 5-8 s ``search`` on a worker left the
# main thread exactly one tick on 3.10 and 3.12), so the hop yields between
# strings, never within one. The ceiling is therefore the liveness bound for a
# single string as well as the WORKER's: an unbounded string parks the loop and
# the thread for as long as the scan takes, and the permission request behind
# it -- the turn -- waits with it.
#
# The anchor rewrite in this change cut the constant but NOT the growth -- the
# scan is still superlinear in the length of one line. Measured on one dev box:
#
#     4 KiB 0.16s | 8 KiB 0.31s | 16 KiB 1.14s | 20 KiB 1.52s | 32 KiB 3.79s
#
# 16->32 KiB is 3.3x for 2x the input (~n^1.7), so extrapolation is the wrong
# instinct here and a ceiling has to be set from the measured curve. A CI runner
# under parallel load came in roughly an order of magnitude slower than this box,
# which is what sets the margin: 20 KiB is ~1.5s here and ~15s there, under the
# 25s loop watchdog on both.
#
# Exceeding it is FAIL-CLOSED: the call is denied, never skipped. A skip would
# convert a liveness bug into a security hole by letting unscanned input through
# the deny surface; a denial only refuses input we cannot prove safe, and is
# strictly better than the alternative it replaces, which was crashing the
# gateway and losing the whole turn.
#
# Known cost of that trade: a permission-gated write of a benign file larger
# than this lands its whole content in ``tool_input`` and is refused. The durable
# fix is to stop running the SHELL-COMMAND matcher over fields that never carry a
# command. Tracked in https://github.com/kirodotdev/KiroCrew/issues/8053.
#
# One number for both tiers: the shell gate refuses a command above
# ``security.MAX_SCANNABLE_COMMAND_CHARS`` on its own (every caller, not only
# this one), so aliasing it here is what keeps "too long for the tool_input
# scan" and "too long for the command gate" the same size.
_MAX_SCANNABLE_TOOL_INPUT_CHARS = MAX_SCANNABLE_COMMAND_CHARS


def _title_denial(
    title: str,
    denied_regexes: list[str] | None,
) -> tuple[str, str] | None:
    """Return the always-enforced denial for the tool *title*, or ``None``.

    The title is the request's primary subject -- for a shell tool it IS the
    command -- and it goes through the same three predicates as every
    tool_input string. Pure and synchronous like :func:`_first_tool_input_denial`,
    and run on the same worker hop: the field crash this exists for was the
    sensitive-path gate scanning a ~9 KB TITLE for 25 s on the event loop, so
    a hop that offloaded only the tool_input strings left the crash path in
    place. The tuple is ``(kind, reason)`` with *kind* ``"path"`` / ``"bash"`` /
    ``"regex"``; the reasons are the exact strings the on-loop checks produced.
    """
    if is_sensitive_path(title):
        return ("path", f"Blocked: sensitive path: {title}")
    bash_reason = is_sensitive_bash_command(title)
    if bash_reason:
        return ("bash", bash_reason)
    deny_reason = is_denied(title, denied_regexes=denied_regexes)
    if deny_reason:
        return ("regex", deny_reason)
    return None


def _first_tool_input_denial(
    strings: list[str],
    denied_regexes: list[str] | None,
) -> tuple[str, str, str] | None:
    """Return the first tool_input denial among *strings*, or ``None``.

    Pure, synchronous, and blocking: the three predicates are regex-heavy and
    ``_extract_tool_input_strings`` hands over EVERY string in the payload, so a
    single long document body can occupy the calling thread for seconds. It
    therefore runs on a worker thread (one hop for the whole loop, not one per
    string). CPython's ``re`` HOLDS the GIL for the whole of one match call
    (measured: a 5-8 s ``search`` on a worker left the main thread a single
    tick on 3.10 and 3.12), so the hop keeps the loop live BETWEEN strings,
    not within one; within one string the only liveness guarantees are the
    linear patterns and the length check against
    :data:`_MAX_SCANNABLE_TOOL_INPUT_CHARS`, which also bounds the worker's
    own wall clock -- a denied oversized string is recoverable, a worker parked
    for minutes on a pathological payload is not -- and an oversized one is
    denied rather than scanned or skipped.

    The tuple is ``(kind, reason, matched_string)`` where *kind* is
    ``"path"`` / ``"bash"`` / ``"regex"`` / ``"oversize"``. Mechanism
    classification stays with the caller on the event loop, because it consults
    the HookManager.
    """
    for s in strings:
        if len(s) > _MAX_SCANNABLE_TOOL_INPUT_CHARS:
            # Fail closed: too long to scan inside the loop's liveness budget,
            # so it cannot be shown safe and is refused.
            return (
                "oversize",
                (
                    "Blocked: a tool_input string is too large to security-scan "
                    f"({len(s)} chars > {_MAX_SCANNABLE_TOOL_INPUT_CHARS} limit); "
                    "refused rather than left unscanned"
                ),
                s[:64],
            )
        if is_sensitive_path(s):
            return ("path", f"Blocked: sensitive path in tool_input: {s}", s)
        _input_bash = is_sensitive_bash_command(s)
        if _input_bash:
            return ("bash", _input_bash, s)
        _input_deny = is_denied(s, denied_regexes=denied_regexes)
        if _input_deny:
            return ("regex", _input_deny, s)
    return None


# ── Tool Approval Policies ──


class ToolApprovalPolicy(Enum):
    """How to handle tool permission requests during streaming."""

    AUTO_APPROVE = "auto_approve"
    REJECT_ALL = "reject_all"
    HOOK_BASED = "hook_based"


# ── Stream and Collect ──


async def run_bg_oneliner(
    sessions: Any,
    prompt: str,
    *,
    model: str | None = None,
    sel_source: str = "bg_oneliner",
    sel_session_key: str = "_bg",
    timeout: float | None = None,
    strict_model: bool = False,
) -> str:
    """Stream a single prompt through an ephemeral background session and return
    the accumulated text.

    ``strict_model=True`` makes the requested ``model`` a hard requirement
    rather than a preference: a failed ``set_model`` override raises instead
    of silently degrading to the session default, and the reactive
    rejected-model fallback below is disabled. Callers whose RESULT is only
    meaningful on that exact model (e.g. the poisoned-conversation canary in
    chat_runner, which uses a success as evidence to discard a conversation
    served by that same model) must set it; ordinary best-effort callers
    (title/nav/folder generation) keep the default lenient behavior.

    Consolidates the identical "acquire a ``_bg`` session -> best-effort pin the
    cheap model -> drive the event loop -> ``destroy()`` in ``finally``" skeleton
    that was copied across title, link-label, folder-icon, and session-summary
    generation. The task is tool-free by contract: permission requests are
    rejected and **always** SEL-logged as ``denied`` — every permission decision
    must be audited (``backend-security-controls``). Callers may override
    ``sel_source`` to attribute the denial to their feature; callers that omit it
    are audited under the generic ``"bg_oneliner"`` source rather than silently
    dropping the SEL event.

    Errors propagate to the caller (the ``_bg`` session is still ``destroy()``-ed
    in ``finally``): callers that want best-effort "" fallback wrap the call
    themselves, while callers that surface the failure (title/nav) get it
    unchanged. ``sessions`` is duck-typed (a ``SessionManager``-like object
    exposing ``get_bg_session()``) rather than statically imported, so this
    low-level helper stays free of a dashboard/session import cycle.
    """
    session = await sessions.get_bg_session()
    # The stats object as it stands BEFORE this turn. The runner replaces it when
    # a turn actually begins, so comparing identity at teardown separates a turn
    # that ran from one whose dispatch failed while the previous turn's already
    # recorded credits were still installed.
    stats_before = _billing_stats(session)
    # Wall clock for the turn itself, started after the session is in hand so the
    # acquire wait is not charged as turn time. The acp provider never fills
    # TurnUsage.duration_ms, so this local measurement is the only duration a
    # background row can carry, and every other dispatch surface supplies one.
    turn_started = time.monotonic()

    async def _drive(model_to_use: str | None) -> str:
        text = ""
        set_model = getattr(session, "set_model", None)
        # Pass the caller's preference (often the governed "auto") to set_model,
        # which resolves it against the session's advertised model list at the
        # wire chokepoint (AcpSessionHandle.set_model -> resolve_usable_model):
        # a hardcoded/unentitled id, or "auto" on a partition that does not serve
        # it, is swapped for the first advertised model instead of
        # reaching the wire and failing mid-prompt with Invalid model ID.
        # Best-effort: a failed override falls back to the default — unless
        # strict_model, where running on any other model would make the
        # result meaningless (see docstring), so the failure propagates.
        if model_to_use and set_model is not None:
            try:
                await set_model(model_to_use)
            except Exception:
                if strict_model:
                    raise
                logger.debug(
                    "bg oneliner: model override to %s failed; using default", model_to_use
                )
            else:
                if strict_model:
                    # POST-CONDITION, not just no-exception: the substitute-style
                    # set_model seam can silently inherit the session default
                    # when the requested id is absent from the advertised set
                    # (resolve_usable_model returns "" → no-op, no raise). A
                    # strict caller (the poisoned-conversation canary) must
                    # never run on any other model — verify the session now
                    # SERVES the requested id and refuse otherwise. Unreadable
                    # served model ⇒ cannot verify ⇒ refuse.
                    served = str(getattr(session, "served_model", "") or "").strip()
                    if served != model_to_use:
                        raise RuntimeError(
                            "run_bg_oneliner(strict_model=True): session serves "
                            f"{served or 'unknown'!r}, not the required {model_to_use!r}"
                        )
        elif strict_model and model_to_use and set_model is None:
            # No override seam at all: cannot guarantee the model — refuse
            # rather than silently answer from the session default.
            raise RuntimeError(
                "run_bg_oneliner(strict_model=True) requires a session with set_model()"
            )
        async for event in session.prompt(prompt):
            if event.kind == EVENT_TEXT_CHUNK:
                text += event.text
            elif event.kind == EVENT_PERMISSION_REQUEST:
                # Audit the denial BEFORE rejecting: every permission decision
                # must be SEL-logged (backend-security-controls), and a
                # reject_tool transport failure must NOT skip the audit.
                # ``sel_source`` carries a non-empty default so callers that
                # don't attribute a feature still produce an audit record.
                _sel().log_tool_invocation(
                    session_key=sel_session_key,
                    tool_name=getattr(event, "title", "unknown") or "unknown",
                    outcome="denied",
                    source=sel_source or "bg_oneliner",
                    request_id=str(event.request_id),
                )
                await session.reject_tool(event.request_id)
            elif event.kind == EVENT_TOOL_CALL:
                # Tool-free by contract, but an AUTO-APPROVED tool arrives with no
                # permission request to reject — audit it so no invocation escapes
                # the SEL log (backend-security-controls; mirrors the cron/
                # contradiction bg path this helper subsumes).
                _sel().log_tool_invocation(
                    session_key=sel_session_key,
                    tool_name=getattr(event, "title", "unknown") or "unknown",
                    outcome="allowed",
                    source=sel_source or "bg_oneliner",
                )
            elif event.kind == EVENT_COMPLETE:
                break
        return text

    async def _run(model_to_use: str | None) -> str:
        if timeout is not None:
            return await asyncio.wait_for(_drive(model_to_use), timeout)
        return await _drive(model_to_use)

    try:
        try:
            return await _run(model)
        except AcpError as exc:
            # Reactive fallback: the model was rejected mid-prompt — e.g. "auto"
            # on a partition that does not serve it, or any id the
            # account cannot run. The advertised list can't be used
            # to gate "auto" statically (it is a sentinel, never advertised), so
            # this is the layer that turns your spec's "else the first available
            # model" into action: retry ONCE with the first advertised model that
            # is neither the rejected id nor "auto". Only fires when the raise-time
            # classifier tagged a rejected model AND named an advertised set.
            rejected = getattr(exc, "rejected_model", None)
            advertised = getattr(exc, "advertised", None) or []
            fallback = (
                first_advertised_fallback(advertised, rejected)
                if rejected and not strict_model
                else None
            )
            if not fallback:
                raise
            logger.warning(
                "bg oneliner: model %r rejected; retrying once with %r", rejected, fallback
            )
            return await _run(fallback)
    finally:
        # Account BEFORE destroy(): the turn's billing lives on the session this
        # tears down. Every caller of this helper — titles, link labels, folder
        # icons, session summaries, tips, the canary — reaches the provider
        # through here and none of them recorded spend of their own, so the
        # bill moved while the usage store stayed empty. Recording at this one
        # point covers all of them and a new caller cannot forget to.
        # The inner finally is load-bearing: persisting is an await, and
        # CancelledError is a BaseException that no `except Exception` catches,
        # so without it a cancellation landing on that await would skip
        # destroy() and leak the session's runtime.
        try:
            try:
                # Imported here rather than at module scope because the usage
                # module's own import chain reaches back into this one -- history
                # and several dashboard handlers import ToolApprovalPolicy /
                # run_bg_oneliner from here -- so a module-scope import raises
                # ImportError against a partially initialized llm_helpers. It
                # also pulls ~600 modules that every consumer of this low-level
                # module would otherwise pay for at boot.
                from kiro_crew.dashboard.handlers.usage import persist_token_record_async

                usage = provider_last_turn_usage(session, since=stats_before)
                # One shared predicate across every persist gate: a claude-seam
                # turn recovered through the live-stats path can bill cost or
                # cache tokens with zero credits AND zero fresh token counts;
                # a gate testing only the kiro dimensions silently drops it.
                if usage_has_billing(usage):
                    await persist_token_record_async(
                        sel_session_key,
                        # The model the session SERVED, never the one requested: a
                        # rejected preference is replaced by the reactive fallback
                        # above, so recording the request would bill the spend to a
                        # model that did not run. An unreadable served model falls
                        # through to model_source rather than naming a guess.
                        str(getattr(session, "served_model", "") or "").strip(),
                        usage,
                        _provider_label(session),
                        surface=f"bg:{sel_source}",
                        elapsed_ms=int((time.monotonic() - turn_started) * 1000),
                        model_source=session,
                    )
            except Exception:
                logger.debug("bg oneliner accounting failed source=%s", sel_source, exc_info=True)
        finally:
            await session.destroy()


def _billing_stats(provider: Any) -> Any:
    """The per-turn stats object behind *provider*, or ``None``.

    Returned as the object rather than a value so callers can compare identity:
    the runner installs a FRESH stats object as it begins a turn, which is what
    tells a completed turn apart from one that never started.

    Each holder is read through :func:`resolve_billing_stats`, the one spelling
    the wrappers use too: a provider's declared
    :meth:`LLMProvider.billing_stats` wins, and the ``last_prompt_stats``
    fallback keeps every holder that was found before the capability existed --
    the raw ``AcpClient``, and the doubles that stand in for a runner. One
    holder's broken read is skipped rather than abandoning the walk, so a faulty
    wrapper cannot lose a turn whose billing a later node still carries.
    """
    try:
        holders = _billing_stat_holders(provider)
    except Exception:
        logger.debug("billing stats holder walk failed", exc_info=True)
        return None
    for node in holders:
        try:
            stats = resolve_billing_stats(node)
        except Exception:
            logger.debug("billing stats read failed for one holder", exc_info=True)
            continue
        if stats is not None:
            return stats
    return None


def _attempt_usage(provider: Any, *, since: Any = _NO_PRIOR_STATS) -> TurnUsage:
    """Billing accrued on ONE attempt, read from the provider's live stats.

    ``since`` is the stats object observed before the attempt. The runner installs
    a fresh stats object as it begins a turn, with the credit counters at zero; a
    dispatch that fails BEFORE that point -- a busy session, a dead runtime --
    leaves the PREVIOUS turn's object in place, still carrying credits that were
    already recorded. Comparing identity reports nothing in that case instead of
    billing the earlier turn a second time.
    """
    stats = _billing_stats(provider)
    if stats is None:
        return TurnUsage()
    if since is not _NO_PRIOR_STATS and stats is since:
        return TurnUsage()
    try:
        # Prefer the stats object's own converter: it is the single source of
        # truth for stats -> TurnUsage and carries every billing dimension the
        # turn filled (claude seam: token counts + cache fields + cost_usd; kiro:
        # credits). Duck-typed so the doubles in tests (and any stats holder
        # predating the converter) fall through to the credits-only constructor,
        # which is byte-identical for the kiro seam. The converter's failure is
        # contained so a faulty to_turn_usage degrades to the credits read
        # rather than silently zeroing a turn that previously billed.
        to_usage = getattr(stats, "to_turn_usage", None)
        if callable(to_usage):
            try:
                usage = to_usage()
            except Exception:
                logger.debug("to_turn_usage failed; falling back to credits", exc_info=True)
                usage = None
            if isinstance(usage, TurnUsage):
                return usage
        return TurnUsage(credits=float(getattr(stats, "credits", 0.0) or 0.0))
    except Exception:
        logger.debug("attempt usage read failed", exc_info=True)
    return TurnUsage()


def usage_has_billing(usage: TurnUsage) -> bool:
    """True when *usage* carries any billing dimension worth a row.

    The single predicate behind every persist gate. Three hand-maintained
    copies of ``credits or input_tokens or output_tokens`` is how the claude
    seam's ``cost_usd`` (and a cost-free cache-only turn) got dropped in the
    first place (#6758); a gate that reads this cannot drift from its siblings
    when the next billing dimension is added.
    """
    return bool(
        usage.credits
        or usage.cost_usd
        or usage.input_tokens
        or usage.output_tokens
        or usage.cache_creation_tokens
        or usage.cache_read_tokens
    )


def _sum_usage(left: TurnUsage, right: TurnUsage) -> TurnUsage:
    """Add two attempts' billing. Never raises; unknown fields stay at zero."""
    try:
        return TurnUsage(
            credits=float(left.credits or 0.0) + float(right.credits or 0.0),
            input_tokens=int(getattr(left, "input_tokens", 0) or 0)
            + int(getattr(right, "input_tokens", 0) or 0),
            output_tokens=int(getattr(left, "output_tokens", 0) or 0)
            + int(getattr(right, "output_tokens", 0) or 0),
            cache_creation_tokens=int(getattr(left, "cache_creation_tokens", 0) or 0)
            + int(getattr(right, "cache_creation_tokens", 0) or 0),
            cache_read_tokens=int(getattr(left, "cache_read_tokens", 0) or 0)
            + int(getattr(right, "cache_read_tokens", 0) or 0),
            cost_usd=float(getattr(left, "cost_usd", 0.0) or 0.0)
            + float(getattr(right, "cost_usd", 0.0) or 0.0),
        )
    except Exception:
        logger.debug("usage sum failed", exc_info=True)
        return left


def provider_last_turn_usage(provider: Any, *, since: Any = _NO_PRIOR_STATS) -> TurnUsage:
    """Best-effort read of the just-completed turn's billing usage.

    ``stream_and_collect`` breaks on ``EVENT_COMPLETE`` and returns only text,
    discarding the event's ``usage``. Background surfaces that dispatch through
    it (cron, heartbeat, autonudge, workflow, task-runner self-review) therefore
    have no event to hand :func:`persist_token_record_async`. This recovers the
    turn's billing and wraps it in a ``TurnUsage`` so it can be passed straight
    through as the ``event`` argument.

    A turn can span several attempts: ``stream_and_collect`` retries a busy
    session and a transient backend error, and each retry installs fresh per-turn
    stats. Reading the provider's live stats afterwards therefore sees only the
    LAST attempt and loses any earlier attempt that was already billed. So the
    total ``stream_and_collect`` accumulated is preferred when present, and
    consumed here -- one turn's billing is reported once. Callers that drive
    ``provider.stream`` themselves publish no total and fall back to the live
    read, which is what ``since`` guards (see :func:`_attempt_usage`).

    The total is published WITH the stats object it was computed from, and is
    accepted only while that object is still the provider's current one. The
    provider outlives the turn -- the shared background session is reused by every
    background caller, and a Slack session by every turn in its thread -- so a
    total left unread by one turn would otherwise be consumed by the next one,
    billing that turn's spend to the wrong surface and losing its own. Today every
    reader happens to be paired with a publish in the same turn, which makes the
    residue unreachable; the guard is here so that safety does not depend on the
    next caller preserving that pairing. A fresh turn installs fresh stats, so a
    stale total simply fails the identity check and the live read takes over.

    On the kiro (acp) seam the only non-zero per-turn billing signal is
    ``credits``; on the claude seam the token counts, cache fields, and
    ``cost_usd`` are filled instead. Whichever dimensions the turn's stats
    carried come through :meth:`to_turn_usage` intact. Providers that expose
    no stats (non-ACP backends, test doubles) yield an empty ``TurnUsage``
    (credits=0). Never raises.
    """
    try:
        accumulated = getattr(provider, _TURN_BILLED_ATTR, None)
        if isinstance(accumulated, tuple) and len(accumulated) == 2:
            published_stats, total = accumulated
            # Clear on every read, match or not: a total that failed the identity
            # check belongs to a turn that is already over and must not be seen
            # again by a third one.
            try:
                delattr(provider, _TURN_BILLED_ATTR)
            except Exception:
                logger.debug("clearing accumulated turn usage failed", exc_info=True)
            if isinstance(total, TurnUsage) and published_stats is _billing_stats(provider):
                return total
    except Exception:
        logger.debug("accumulated turn usage read failed", exc_info=True)
    return _attempt_usage(provider, since=since)


def _billing_stat_holders(provider: Any) -> "list[Any]":
    """Objects that may carry ``last_prompt_stats``, nearest wrapper first.

    The compatibility path behind :func:`_billing_stats`, and the lookup
    :func:`_provider_label` still needs: the turn-runner sits behind a different
    attribute per seam: the acp provider keeps it on ``_client``, the session
    provider on ``_handle``, and the shared background session hands non-kiro
    callers a thin adapter whose only link to the runner is ``_sess.provider``.
    Walking all of them keeps a background turn on the claude_code / bedrock seam
    from reporting 0 credits for a turn that was billed. Bounded and
    identity-deduped so a self-referential wrapper chain cannot loop.

    A name absent from this tuple is exactly how a new seam's spend went
    unreported, which is why the billing read now prefers the provider's declared
    :meth:`LLMProvider.billing_stats` and only falls back here.
    """
    out: list[Any] = []
    seen: set[int] = set()
    frontier: list[Any] = [provider]
    while frontier and len(out) < _WRAPPER_WALK_MAX_NODES:
        node = frontier.pop(0)
        if node is None or id(node) in seen:
            continue
        seen.add(id(node))
        out.append(node)
        for attr in ("_client", "_handle", "_sess", "provider"):
            frontier.append(getattr(node, attr, None))
    return out


def _provider_label(provider: Any) -> str:
    """Backend label for the usage row's ``provider`` dimension. Never raises.

    Left unset the row lands with ``provider=""`` and drops out of the usage
    page's provider and provider-model breakdowns, so a background turn's spend
    would be recorded yet unattributable to a backend.

    ``provider_label`` is the shared resolver for this vocabulary -- the same
    ``acp`` / ``claude_code`` / ``kas`` keys the session map and resume-compat
    check use -- and it answers from the backend that actually served the turn,
    which is the precedence this module already applies to the model and the
    agent. Reading ``config.agent.provider`` instead would report a declaration:
    that field's enum admits only ``acp``, so a claude_code-backed session would
    be labelled ``acp``.

    The resolver only recognises a provider it is handed directly, and the shared
    background session wraps one behind ``_sess.provider``, so the wrapper chain
    is walked and the first node that names a non-default backend wins. Falling
    back to the default label rather than to ``""`` matches every other writer of
    this field.
    """
    try:
        from kiro_crew.acp.types import PROVIDER_LABEL_DEFAULT
        from kiro_crew.providers.acp import provider_label

        for node in _billing_stat_holders(provider):
            label = provider_label(node)
            if label and label != PROVIDER_LABEL_DEFAULT:
                return label
        return PROVIDER_LABEL_DEFAULT
    except Exception:
        logger.debug("resolving provider label failed", exc_info=True)
        return ""


@asynccontextmanager
async def background_turn(
    sessions: Any,
    *,
    task: str,
    agent: "str | None" = None,
) -> "AsyncIterator[Any]":
    """Take the shared background session for ONE turn, then release and account.

    Every background caller needs the same three steps around its prompt: acquire
    the shared session (which takes its per-session semaphore), release that
    semaphore in a ``finally`` or the next caller deadlocks on it, and recycle the
    session afterwards. Callers that hand-rolled those steps had no reason to also
    record what the turn cost, so background spend reached the provider's bill
    without ever reaching the usage store — invisible on the dashboard even though
    the account balance moved. Recording here makes it structural: a turn taken
    through this manager is accounted for, and a new background caller cannot
    forget to.

    ``task`` labels the work in the ``surface`` dimension as ``bg:<task>`` so spend
    is attributable per background job instead of pooling into one anonymous
    bucket. Keep it a short fixed label — never per-session text, which would make
    the dimension unbounded.

    ``agent`` is forwarded to ``get_or_create`` ONLY when a caller supplies it:
    the key decides which session is returned, but the agent decides what the
    session is created AS, so injecting a default here would silently change that
    for callers that deliberately pass none. The recorded row still names the
    background agent so the dimension is never blank.

    Exceptions are deliberately NOT swallowed. Callers distinguish "the session
    could not be acquired" (nothing was sent, so nothing was billed) from "the turn
    ran and failed" (billed), and collapsing the two corrupts the retry budgets
    built on that distinction. Only the accounting write is best-effort.
    """
    from kiro_crew.session import BACKGROUND_AGENT, BACKGROUND_KEY  # circular import

    if agent is None:
        client, _new, _resumed = await sessions.get_or_create(BACKGROUND_KEY)
    else:
        client, _new, _resumed = await sessions.get_or_create(BACKGROUND_KEY, agent=agent)
    # The stats object as it stands BEFORE this turn. The shared session serves
    # many turns, and the runner replaces this object only once a turn actually
    # begins, so identity is what separates a turn that ran from one whose
    # dispatch failed while a previous, already-recorded turn's credits were
    # still installed.
    stats_before = _billing_stats(client)
    # Wall clock for the turn itself, started after the acquire so queue wait is
    # not charged as turn time. The acp provider never fills TurnUsage.duration_ms,
    # so this is the only duration a background row can carry.
    turn_started = time.monotonic()
    try:
        yield client
    finally:
        turn_elapsed_ms = int((time.monotonic() - turn_started) * 1000)
        # Snapshot the billing BEFORE releasing. ``last_prompt_stats`` is shared
        # mutable state on the session: the next waiter's turn carries it over
        # (zeroing credits) or starts accumulating its own, so a read after
        # release attributes that waiter's spend to this task, or loses the row
        # entirely. This read is a synchronous attribute walk, so taking it here
        # costs nothing and keeps release ahead of every await.
        usage = provider_last_turn_usage(client, since=stats_before)
        # Release before any await: it is synchronous, so no cancellation can
        # land between the turn ending and the next caller being unblocked.
        # CancelledError is a BaseException that no `except Exception` catches,
        # and an await ordered ahead of this would let a cancelled task hold the
        # shared semaphore forever.
        try:
            sessions.release(BACKGROUND_KEY)
        except Exception:
            logger.debug("background session release failed task=%s", task, exc_info=True)
        # Recycle sits in a finally for the same cancellation reason, and follows
        # accounting because it may replace the provider entirely.
        try:
            try:
                # Same cycle as the oneliner's teardown: the usage module's import
                # chain reaches back into this one (history and several dashboard
                # handlers import ToolApprovalPolicy / run_bg_oneliner from here),
                # so a module-scope import raises ImportError against a partially
                # initialized llm_helpers, and it would pull ~600 modules into
                # every consumer's boot.
                from kiro_crew.dashboard.handlers.usage import persist_token_record_async

                # A turn that never reached the provider bills nothing and has no
                # row to write; the same guard the chat path applies keeps
                # acquire-time failures from landing as zero-credit noise. The
                # shared predicate covers the claude seam's cost and cache
                # dimensions alongside the kiro credits/token signals.
                if usage_has_billing(usage):
                    await persist_token_record_async(
                        BACKGROUND_KEY,
                        "",
                        usage,
                        _provider_label(client),
                        surface=f"bg:{task}",
                        agent=agent or BACKGROUND_AGENT,
                        elapsed_ms=turn_elapsed_ms,
                        model_source=client,
                    )
            except Exception:
                logger.debug("background turn accounting failed task=%s", task, exc_info=True)
        finally:
            try:
                await sessions.recycle_background()
            except Exception:
                logger.debug("background recycle failed task=%s", task, exc_info=True)


async def stream_and_collect(
    provider: LLMProvider,
    message: str,
    *,
    approval_policy: ToolApprovalPolicy = ToolApprovalPolicy.AUTO_APPROVE,
    hooks: HookManager | None = None,
    on_chunk: Callable[[str], None] | None = None,
    on_tool_approval: Callable[[LLMEvent], Awaitable[bool]] | None = None,
    on_steer_consumed: Callable[[str], None] | None = None,
    on_complete: Callable[[LLMEvent], None] | None = None,
    on_tool_gate: Callable[[str, bool, bool], None] | None = None,
    retry_transient: bool = True,
    max_turns: int | None = None,
    session_key: str = "",
    agent: str = "",
    app: str = "",
    model_fallback: bool = False,
    fallback_models: Sequence[str] = (),
) -> str:
    """Stream a message through an LLM provider and collect the full response.

    This is the core pattern used by cron, heartbeat, subagent, consolidator,
    taskrunner, and title generation.

    Args:
        provider: The LLM provider to stream through.
        message: The prompt to send.
        approval_policy: How to handle tool permission requests.
        hooks: HookManager for HOOK_BASED approval policy.
        on_chunk: Optional callback invoked with each text chunk (for progress).
        on_tool_approval: Optional async callback for interactive approval.
        on_steer_consumed: Optional callback invoked with the backend's
            ``steering_consumed`` echo text. A mid-turn steer is a
            fire-and-forget write, so this echo is the ONLY authoritative signal
            that the backend injected it; a caller that steers must observe this
            to know which of its steers to requeue when the turn ends.
        on_complete: Optional callback invoked with the provider's raw
            ``EVENT_COMPLETE``. It is not invoked when the stream exhausts or
            the caller cancels before that event. Raising from the callback is
            swallowed so observation cannot fail the completed turn.
        on_tool_gate: Optional callback invoked once per tool permission
            decision with ``(tool_title, approved, security_blocked)``. Lets a
            caller tell "the model did work" apart from "every tool the model
            attempted was blocked" — a distinction the returned text cannot
            carry, because a model whose tools were all refused still returns
            plausible prose. ``security_blocked`` is True only for the
            unconditional deny checks (sensitive path, sensitive bash, a deny
            pattern); a governance ``TOOL_DENY`` and an unattended-approval
            timeout are refusals that say nothing about the job, so they arrive
            with ``approved=False`` and ``security_blocked=False``.
            Delivered when the attempt settles, not mid-stream, and decisions
            from an abandoned retry attempt are discarded: they describe work
            the final turn never did. ``tool_title`` is LLM-authored: redact it
            before display or persistence. Raising from the callback is
            swallowed; observing a gate decision must never fail the turn.
        retry_transient: When True (default), transient backend errors are
            retried in-place with bounded backoff. Set False from callers that
            already own an outer transient-retry loop, so the inner arm doesn't
            compound their attempts (retry-layer amplification).
        max_turns: Optional cap on tool-call iterations per prompt. When reached,
            the event loop breaks and returns whatever text has been collected.
            None (default) means no limit.
        session_key: Calling surface's session key, forwarded to the PreToolUse
            gate. Empty (default) preserves every existing caller's behavior.
        agent: Calling agent name, forwarded to the gate alongside *session_key*.
        app: Owning app name, forwarded to the gate so the app's governance
            PROFILE is resolved — not just the enterprise ceiling.

            All three matter for ``HOOK_BASED`` callers specifically. The gate
            resolves ``ceiling ∩ profile``, and it can only look up a profile it
            has been told the name of; with all three empty it applied the
            ceiling alone, so an app profile narrowing (say) ``filesystem.write``
            was silently not enforced for tools this helper approved. Callers
            using ``REJECT_ALL`` or ``AUTO_APPROVE`` are unaffected — the first
            runs no tools, the second never consults the gate.
        fallback_models: Ordered chain of model ids tried when the same-model
            transient budget exhausts on a throttle/capacity error (Case 2.75).
            Empty (the default) disables the chain — behavior is byte-for-byte
            today's fail-loudly. Requires ``retry_transient=True`` (a caller
            that owns the outer transient loop also owns any fallback policy).
            Every swap is logged at warning and published on the provider via
            :data:`TURN_FALLBACK_ATTR`; the swap is sticky for the session and
            a later call on the same provider probes one primary restore.

    Returns:
        The complete response text.
    """
    transient_attempts = 0
    _model_fallback_attempted = False
    attempt = 0
    _fb_chain = tuple(
        m.strip() for m in (fallback_models or ()) if isinstance(m, str) and m.strip()
    )
    _fb_state = FallbackState(_fb_chain) if _fb_chain else None
    # Cross-attempt tool-activity flag for the fallback chain ONLY. Case 2's
    # same-model retry keys off ``result_text`` alone (pre-existing behavior,
    # pinned byte-for-byte by the empty-chain regression tests), but the chain
    # replays the ORIGINAL prompt up to FALLBACK_CANDIDATE_ATTEMPTS × len(chain)
    # more times — a tool that completed an external mutation before any text
    # streamed would be re-run on every one of them. Same activity predicate as
    # the sub-agent ladder and the dashboard's ``_turn_emitted``: any fired
    # tool call blocks the replay, text or no text.
    _fb_tool_activity = False
    # Sticky-restore probe (§restore policy): if an earlier turn on this
    # provider fell back, try ONCE to move back to the primary before this
    # turn streams. Quiet on success (log only); a still-throttled primary
    # keeps the fallback for this turn.
    if getattr(provider, TURN_FALLBACK_ATTR, None) is not None:
        await probe_fallback_restore(provider, surface="stream_and_collect")
    # Accumulates across attempts, so it lives OUTSIDE the retry loop: a turn that
    # was billed and then retried must report the sum, not the last attempt.
    turn_billed = TurnUsage()
    while True:
        result_text = ""
        tool_call_count = 0
        # Consumption is committed on every exit EXCEPT a retry. A retry re-sends the
        # original message without the steer, so committing there would mark a steer
        # delivered that the model never saw. Every other exit — success or failure — is
        # terminal for this steer: the backend already consumed it, and `consumed` is what
        # suppresses the requeue, so dropping the acknowledgement makes the cleanup hand an
        # already-answered question back and ask it twice. Re-initialised per attempt.
        consumed_this_attempt: list[str] = []
        # Same per-attempt discipline as the steer acknowledgements above, and for the
        # same reason: a retry re-sends the original message, so decisions from an
        # abandoned attempt describe work the final turn never did. Committing them
        # would let a refusal from a discarded attempt outvote a clean retry and fail
        # a healthy job.
        gate_this_attempt: list[tuple[str, bool, bool]] = []
        # Tool calls that reached the gate, and tool calls that actually ran.
        # A tool auto-approved upstream never raises a permission request, so it
        # executes without a gate decision: correlating the two by
        # ``tool_call_id`` is what lets a caller see that work happened.
        gate_decided_ids: set[str] = set()
        executed_calls: list[tuple[str, str]] = []
        retrying = False
        # Billing accrued on THIS attempt is measured against the stats object as
        # it stands now: a retry installs a fresh one, so without a per-attempt
        # baseline an attempt that was billed and then failed is invisible.
        attempt_stats_before = _billing_stats(provider)
        try:
            async for event in provider.stream(message):
                if event.kind == EVENT_TEXT_CHUNK:
                    result_text += event.text
                    if on_chunk:
                        on_chunk(event.text)
                elif event.kind == EVENT_PERMISSION_REQUEST:
                    # Captures this decision's outcome and mechanism, so a hard
                    # security block is distinguishable from a governance denial
                    # or an unattended-approval timeout. Only the former says
                    # anything about the job itself.
                    _decision: list[tuple[str, str]] = []
                    approved = await _resolve_permission(
                        provider,
                        event,
                        approval_policy,
                        hooks,
                        on_tool_approval,
                        session_key=session_key,
                        agent=agent,
                        app=app,
                        on_decision=lambda outcome, mech: _decision.append((outcome, mech)),
                    )
                    if on_tool_gate:
                        _mech = _decision[-1][1] if _decision else ""
                        gate_this_attempt.append(
                            (event.title or "", approved, _mech.startswith("always_deny"))
                        )
                        if event.tool_call_id:
                            gate_decided_ids.add(event.tool_call_id)
                    if not approved:
                        continue
                elif event.kind == EVENT_TOOL_CALL:
                    tool_call_count += 1
                    # Sticky across attempts (never reset in the retry loop):
                    # once ANY attempt fired a tool, the fallback chain must
                    # not replay the original prompt — see _fb_tool_activity.
                    _fb_tool_activity = True
                    if on_tool_gate:
                        executed_calls.append((event.tool_call_id or "", event.title or ""))
                    if max_turns is not None and tool_call_count > max_turns:
                        logger.warning(
                            "max_turns=%d exceeded (%d tool calls), breaking",
                            max_turns,
                            tool_call_count,
                        )
                        _sel().log_tool_invocation(
                            session_key="",
                            source="llm_helpers",
                            tool_name=event.title or "",
                            tool_kind=event.tool_kind,
                            outcome="denied_max_turns",
                            metadata={"max_turns": max_turns, "count": tool_call_count},
                        )
                        break
                    # Fire PreToolUse hooks for auto-approved tools (informational only)
                    _sel().log_tool_invocation(
                        session_key="",
                        source="llm_helpers",
                        tool_name=event.title,
                        tool_kind=event.tool_kind,
                        outcome="auto_approved",
                    )
                    await fire_tool_hooks(
                        get_global_hook_store(),
                        event.title,
                        event.tool_input,
                    )
                elif event.kind == EVENT_STEER_CONSUMED:
                    consumed_this_attempt.append(event.text or "")
                elif event.kind == EVENT_COMPLETE:
                    if on_complete:
                        try:
                            on_complete(event)
                        except Exception:
                            logger.debug("on_complete callback failed", exc_info=True)
                    break
            return result_text
        except AcpError as exc:
            msg = str(exc)
            # Prompt-busy is matched STRUCTURALLY first, with the substring kept
            # as a fallback. _format_acp_error rewrites the backend's "prompt
            # already in progress" into friendly prose that no longer carries
            # the marker, so a string-only check silently loses BOTH arms below
            # (cancel+retry and PromptBusyExhaustedError) for any producer that
            # formats before raising — which the shared-runtime AcpSessionHandle
            # now does. Unattended callers (workflows/agent_pool, handlers/side,
            # the subagent-completion injector) depend on those arms to reset a
            # wedged parent session, so losing them surfaces a generic failure
            # and leaves the session stuck. The fallback still covers
            # unformatted / history-restored messages.
            busy = isinstance(exc, AcpPromptBusy) or "already in progress" in msg

            # ── Case 1: prompt-busy (provider mid-turn) — cancel + retry. ──
            if busy:
                if attempt >= _PROMPT_BUSY_RETRIES:
                    # Provider is permanently stuck — kill it so the next
                    # get_or_create cold-starts a fresh process.
                    logger.warning(
                        "Prompt busy after %d retries, shutting down provider", _PROMPT_BUSY_RETRIES
                    )
                    try:
                        await provider.shutdown()
                    except Exception:
                        logger.debug("Provider shutdown after busy retries failed", exc_info=True)
                    raise PromptBusyExhaustedError(msg) from exc
                logger.warning(
                    "Prompt busy (attempt %d/%d), cancelling and retrying: %s",
                    attempt + 1,
                    _PROMPT_BUSY_RETRIES,
                    exc,
                )
                try:
                    await provider.cancel()
                except Exception:
                    logger.debug("Cancel before retry failed", exc_info=True)
                await asyncio.sleep(_PROMPT_BUSY_DELAY * (2**attempt))
                attempt += 1
                retrying = True
                continue

            # ── Case 2: transient backend (Bedrock 5xx / throttle / stream) ──
            # Credential is valid; the server hiccupped. Retry with exponential
            # backoff + jitter. Distinct budget from prompt-busy.
            #
            # Guards:
            #   - retry_transient: callers that own an outer transient loop pass
            #     False so the inner arm doesn't compound their attempts.
            #   - `not result_text`: only retry if NO tokens have streamed yet.
            #     A partial response must not be retried — the re-run would
            #     duplicate the already-emitted output.
            if (
                retry_transient
                and not result_text
                and acp_error_is_transient(exc)
                and transient_attempts < _TRANSIENT_RETRIES
            ):
                transient_attempts += 1
                # Exponential backoff with per-process jitter (see _JITTER_RNG):
                # deterministic within a process for tests, uniform across the
                # fleet so co-located peers don't retry in lockstep.
                delay = transient_retry_delay(transient_attempts)
                logger.warning(
                    "Transient backend error (attempt %d/%d), retrying in %.1fs: %s",
                    transient_attempts,
                    _TRANSIENT_RETRIES,
                    delay,
                    exc,
                )
                await asyncio.sleep(delay)
                retrying = True
                continue

            # ── Case 2.75: throttle-exhaustion fallback chain ──
            # The same-model budget (Case 2) is spent and the error is still
            # transient (throttle/capacity — a throttle carries no rejection
            # metadata, so Case 2.5 can never fire for it). Walk the configured
            # chain: substitute set_model, then re-prompt. Two attempts per
            # candidate (initial + one ~2s retry — see FALLBACK_CANDIDATE_
            # ATTEMPTS), advance on transient failure, propagate non-transient
            # immediately (the classifier gate above already ensures that).
            # Empty chain ⇒ this block is inert and Case 3 surfaces the error
            # exactly as before this feature existed.
            #
            # ``not _fb_tool_activity`` is load-bearing over and above
            # ``not result_text``: a tool call can complete an EXTERNAL
            # MUTATION before any text streams, and unlike Case 2's bounded
            # same-model retry (pre-existing semantics, deliberately
            # untouched), the chain replays the original prompt on every
            # candidate — re-running that mutation each time. Any fired tool
            # across ANY attempt disables the chain for this call; the error
            # then surfaces exactly as it did before this feature.
            if (
                retry_transient
                and not result_text
                and not _fb_tool_activity
                and _fb_state is not None
                and acp_error_is_transient(exc)
                and transient_attempts >= _TRANSIENT_RETRIES
            ):
                if _fb_state.should_retry_active():
                    # Final attempt on the current candidate — the shared
                    # budget body already recorded it.
                    delay = transient_retry_delay(1)
                    logger.warning(
                        "model fallback: candidate %s still failing (attempt %d/%d), "
                        "retrying in %.1fs: %s",
                        _fb_state.active,
                        _fb_state.attempts,
                        FALLBACK_CANDIDATE_ATTEMPTS,
                        delay,
                        exc,
                    )
                    await asyncio.sleep(delay)
                    retrying = True
                    continue
                # Advance to the next usable candidate via the shared walk
                # step (marker-seeded primary, skip-active, substitute
                # set_model, sticky-marker publish, greppable warning).
                _cand = await advance_fallback_candidate(
                    provider, _fb_state, surface="stream_and_collect"
                )
                if _cand is not None:
                    await asyncio.sleep(transient_retry_delay(1))
                    retrying = True
                    continue
                _story = _fb_state.exhaustion_story()
                if _story:
                    # Chain exhausted: surface the ORIGINAL error class with the
                    # chain's story attached for the delivering surface, and
                    # keep the incident greppable.
                    logger.warning(
                        "model fallback: chain exhausted (%s); surfacing original error: %s",
                        _story,
                        exc,
                    )
                    try:
                        setattr(exc, FALLBACK_STORY_ATTR, _story)
                    except Exception:
                        pass
                # Fall through to Case 2.5 / Case 3.

            # ── Case 2.5: model rejected (e.g. "auto" on GovCloud) — retry once
            # with the first advertised model. ──
            # Same reactive fallback as run_bg_oneliner: some partitions do not
            # serve the "auto" sentinel, and the advertised list cannot gate it
            # statically. When the backend rejects a model AND names available
            # alternatives, retry ONCE with the first usable advertised model.
            # Only fires when no tokens have streamed (safe to replay) and the
            # error carries rejection metadata.
            #
            # OPT-IN (model_fallback=True): a silent model swap is only correct
            # for a caller that did NOT choose the model — a background/system
            # turn on the governed "auto" (history consolidation). An interactive
            # turn where the user picked a model must surface the rejection, not
            # swap underneath them (AGENTS.md), so the default is off.
            rejected = getattr(exc, "rejected_model", None)
            advertised = getattr(exc, "advertised", None) or []
            if (
                model_fallback
                and not result_text
                and rejected
                and advertised
                and not _model_fallback_attempted
            ):
                fallback = first_advertised_fallback(advertised, rejected)
                if fallback:
                    _model_fallback_attempted = True
                    set_model_fn = getattr(provider, "set_model", None)
                    if set_model_fn:
                        try:
                            await set_model_fn(fallback)
                        except Exception:
                            logger.debug(
                                "set_model(%r) failed during model fallback",
                                fallback,
                                exc_info=True,
                            )
                    logger.warning(
                        "stream_and_collect: model %r rejected; " "retrying once with %r",
                        rejected,
                        fallback,
                    )
                    retrying = True
                    continue

            # ── Case 3: fatal (auth, validation, exhausted retries) — propagate. ──
            raise
        finally:
            # Runs before the value reaches the caller on success, and before the exception
            # propagates on failure, so the acknowledgement always precedes the cleanup that
            # would otherwise requeue the question.
            #
            # Billing is folded in on EVERY attempt, including the ones abandoned by a
            # retry: an attempt whose metering frame landed before the error was billed,
            # and the retry replaces the stats object that carried it. The total is
            # published on the attempt that is terminal -- the same `not retrying`
            # condition the callbacks below use -- so one logical turn publishes once,
            # whether it returns or raises.
            turn_billed = _sum_usage(
                turn_billed, _attempt_usage(provider, since=attempt_stats_before)
            )
            if not retrying:
                try:
                    setattr(
                        provider,
                        _TURN_BILLED_ATTR,
                        (_billing_stats(provider), turn_billed),
                    )
                except Exception:
                    # A provider that refuses the attribute (slots, frozen doubles)
                    # simply leaves the caller on the live-stats fallback.
                    logger.debug("publishing accumulated turn usage failed", exc_info=True)
            if not retrying and on_steer_consumed:
                for consumed_text in consumed_this_attempt:
                    on_steer_consumed(consumed_text)
            if not retrying and on_tool_gate:
                for gate_title, gate_approved, gate_blocked in gate_this_attempt:
                    try:
                        on_tool_gate(gate_title, gate_approved, gate_blocked)
                    except Exception:
                        # A caller's bookkeeping must never abort the turn.
                        logger.debug("on_tool_gate callback failed", exc_info=True)
                # A tool that executed without a matching gate decision was
                # permitted upstream, so it counts as an approval: work happened.
                # An id-less execution cannot be correlated, so it counts too —
                # over-reporting work risks a missed detection, under-reporting
                # it fails a job that did something.
                for _exec_id, _exec_title in executed_calls:
                    if _exec_id and _exec_id in gate_decided_ids:
                        continue
                    try:
                        on_tool_gate(_exec_title, True, False)
                    except Exception:
                        logger.debug("on_tool_gate callback failed", exc_info=True)


async def stream_and_collect_json(
    provider: LLMProvider,
    message: str,
    *,
    approval_policy: ToolApprovalPolicy = ToolApprovalPolicy.AUTO_APPROVE,
    hooks: HookManager | None = None,
    model_fallback: bool = False,
) -> dict | None:
    """Stream a message and parse the response as JSON.

    Combines ``stream_and_collect`` with ``parse_llm_json``.
    Returns parsed dict or None on failure.
    """
    text = await stream_and_collect(
        provider,
        message,
        approval_policy=approval_policy,
        hooks=hooks,
        model_fallback=model_fallback,
    )
    return parse_llm_json(text)


async def _resolve_permission(
    provider: LLMProvider,
    event: LLMEvent,
    policy: ToolApprovalPolicy,
    hooks: HookManager | None,
    on_tool_approval: Callable[[LLMEvent], Awaitable[bool]] | None = None,
    session_key: str = "",
    agent: str = "",
    app: str = "",
    on_decision: Callable[[str, str], None] | None = None,
) -> bool:
    """Resolve a tool permission request. Returns True if approved.

    *on_decision*, when given, receives ``(outcome, mechanism)`` for the single
    decision this call makes — the same pair that reaches the SEL audit row.
    ``mechanism`` is one of ``"always_deny"`` / ``"always_deny_input"`` (the
    unconditional checks), ``"always_deny_hook"`` (a HookManager security deny),
    ``"policy_deny"`` (the governance ceiling, whether reached through the hook
    gate or through a regex-tier rule that only a governance PIN put back into
    the effective set), or ``""`` for an approval or an interactive rejection.
    The ``always_deny`` prefix is therefore what marks a refusal as "the attempt
    itself was the problem"; everything else describes policy state or an absent
    approver. Note that the same regex match can land under either label — what
    decides it is whether the rule survives without its pin.
    """
    from kiro_crew.hooks import TOOL_AUTO_APPROVE, TOOL_DENY
    from kiro_crew.sel import sel

    def _log(outcome: str, **extra):
        # Single funnel for every decision path, so the sink cannot miss one.
        if on_decision is not None:
            _meta = extra.get("metadata") or {}
            try:
                on_decision(outcome, str(_meta.get("mechanism") or ""))
            except Exception:
                logger.debug("on_decision sink failed", exc_info=True)
        sel().log_tool_invocation(
            session_key=session_key,
            agent=agent,
            tool_name=event.title,
            tool_kind=event.tool_kind,
            outcome=outcome,
            request_id=event.request_id,
            **extra,
        )

    if policy == ToolApprovalPolicy.REJECT_ALL:
        await provider.reject_tool(event.request_id)
        _log("rejected", metadata={"reason": "reject_all_policy"})
        return False

    # ── Always-enforced deny checks (regardless of approval policy) ──
    # These run even for AUTO_APPROVE callers (workflows, crons, etc.)
    # to ensure BUILTIN_DENY_PATTERNS and sensitive-path protection cannot
    # be bypassed by callers that skip HookManager wiring.
    normalized = event.title or ""
    if not normalized:
        await provider.reject_tool(event.request_id)
        _log("denied", error="Blocked: missing tool title", metadata={"mechanism": "always_deny"})
        return False
    # Honor the user's Settings>Security opt-out + governance pins on this
    # surface too (cron / Slack / workflow / heartbeat). Without threading the
    # effective set, is_denied() fails closed to ALL built-ins here, which would
    # re-introduce "disabled but still blocked" on every non-dashboard surface.
    # No HookManager (rare) → None → fail-closed default (all built-ins).
    _denied_regexes = hooks.effective_denied_regexes() if hooks is not None else None

    def _regex_deny_mechanism(probe: str, unconditional: str) -> str:
        """Classify an already-decided regex-tier deny by its provenance.

        A governance pin re-adds a built-in rule the user disabled, so the SAME
        match can mean either "this host enforces this rule" or "policy
        currently overrides this host's opt-out". Only the former says anything
        about the tool being attempted; treating the latter as a security block
        durably auto-pauses a cron that a later policy loosening cannot revive,
        because clearing the pause never restores ``enabled``.

        Re-runs the match against the pin-free set — deny path only, so the
        common allow path pays nothing — and reports a match that survives ONLY
        with pins as policy state.
        """
        if hooks is None:
            return unconditional
        try:
            _unpinned = hooks.effective_denied_regexes(include_governance_pins=False)
        except Exception:
            # Classification must never change the deny itself. An unresolvable
            # opt-out state falls back to the unconditional label: over-counting
            # a block is recoverable by an operator, silently not counting one
            # restores the runaway this gate exists to catch.
            logger.debug("deny provenance unresolved; reporting unconditional", exc_info=True)
            return unconditional
        return unconditional if is_denied(probe, denied_regexes=_unpinned) else "policy_deny"

    # Defense-in-depth: the title AND every string in event.tool_input go through
    # the same three predicates. The title usually carries the full path/command
    # (kiro-cli convention), but tool_input may contain additional arguments or
    # the actual path when the title is a generic tool name (e.g. "Read", "Bash").
    _tool_input = event.tool_input or ""
    _input_strings = _extract_tool_input_strings(_tool_input) if _tool_input else []

    def _scan_off_loop() -> tuple[str, str, str, str] | None:
        # One worker hop for the title and the whole tool_input loop. Both are
        # regex-heavy over agent-supplied text; on the event loop a ~9 KB shell
        # title held the loop past the 25 s stall watchdog and took the gateway
        # down (the title tier used to run inline here while only the tool_input
        # tier was offloaded, so that crash path survived the first offload).
        # ``re`` HOLDS the GIL for one match call, so the hop does not keep the
        # loop live inside a single scan -- the linear patterns and the size
        # ceiling do that; what the hop buys is the realpath I/O inside
        # ``is_sensitive_path`` (which does release the GIL) and yields between
        # the strings. Title first, so a request denied on its title
        # reports the title-tier reason and mechanism exactly as before.
        title_hit = _title_denial(normalized, _denied_regexes)
        if title_hit is not None:
            return (title_hit[0], title_hit[1], normalized, "always_deny")
        if _input_strings:
            input_hit = _first_tool_input_denial(_input_strings, _denied_regexes)
            if input_hit is not None:
                return (*input_hit, "always_deny_input")
        return None

    _hit = await asyncio.to_thread(_scan_off_loop)
    if _hit is not None:
        _kind, _reason, _matched, _tier = _hit
        await provider.reject_tool(event.request_id)
        _log(
            "denied",
            error=_reason,
            metadata={
                "mechanism": (_regex_deny_mechanism(_matched, _tier) if _kind == "regex" else _tier)
            },
        )
        return False

    if policy == ToolApprovalPolicy.HOOK_BASED and hooks:
        tool_result = hooks.on_tool_call(
            event.title,
            session_key=session_key,
            agent=agent,
            app=app,
            tool_kind=event.tool_kind,
            raw_params=event.raw_tool_params,
            command=event.shell_command,
            is_shell=event.is_shell,
        )
        if tool_result.action == TOOL_DENY:
            await provider.reject_tool(event.request_id)
            # A hook deny is either a hard security check or the governance
            # ceiling. Only the former says the attempt itself was the problem,
            # and the distinction rides on the result's own field rather than
            # its reason text.
            _log(
                "denied",
                error=tool_result.reason,
                metadata={
                    "mechanism": (
                        "always_deny_hook" if tool_result.security_deny else "policy_deny"
                    )
                },
            )
            return False
        if tool_result.action == TOOL_AUTO_APPROVE:
            # The hook granted this by NAME (its `auto_approve_tools` globs, or
            # the read-only allowlist). Verify it UNCONDITIONALLY: this helper
            # serves unattended callers (cron / autonudge / heartbeat / Meetings
            # transcript turns), some of which pass no approver, and the shell
            # resolves the command's program names again through a PATH that can
            # lead with agent-writable directories. A refusal DOWNGRADES to the
            # caller's normal path — the interactive approver when one is
            # present, else deny-by-default (reject) below — never a
            # silent auto-approve of a shadowed name on an unwatched turn.
            _ng_refusal = await name_grant.refusal_for_event(event)
            if _ng_refusal is None:
                await provider.approve_tool(event.request_id)
                _log("auto_approved", metadata={"reason": "hook_auto_approve"})
                return True
            logger.warning(
                "declining a hook auto-approve: %s; the request falls through "
                "to this caller's approval path",
                _ng_refusal.log_text,
            )
            name_grant.log_decline(
                source="",
                session_key=session_key,
                agent=agent,
                event=event,
                refusal=_ng_refusal,
                tier="hook_auto_approve",
                sel_factory=sel,
            )
            # No interactive approver on this caller: the hook grant was the
            # only positive authorization and it was withheld, so fall through
            # to deny-by-default rather than the caller-less auto-approve below.
            if on_tool_approval is None:
                await provider.reject_tool(event.request_id)
                _log("rejected", metadata={"reason": "name_grant_headless_reject"})
                return False

    # Interactive approval if callback provided
    if on_tool_approval:
        approved = await on_tool_approval(event)
        if not approved:
            await provider.reject_tool(event.request_id)
            _log("rejected", metadata={"reason": "interactive_rejected"})
            return False

    # Default: auto-approve
    await provider.approve_tool(event.request_id)
    _log("auto_approved")
    return True


# ── JSON Parsing ──


_JSON_DECODER = json.JSONDecoder()


def _extract_json_of_type(
    text: str,
    expected_type: type | tuple[type, ...],
    prefer: Callable[[Any], bool] | None = None,
) -> dict | list | None:
    """Extract the first top-level JSON value of *expected_type* embedded in prose.

    Scans successive ``{`` (dict) or ``[`` (list) offsets and uses the stdlib
    ``raw_decode`` to parse a complete JSON value at each — this validates the
    full JSON grammar and correctly handles nesting and string escapes. Returns
    the first value that matches *expected_type*, or None.

    Scanning successive offsets (rather than committing to the first delimiter)
    is what makes this robust to a stray structural brace in the prose preamble
    (e.g. ``"use {placeholder}: {\\"a\\": 1}"``). Only TOP-LEVEL matches count: a
    ``{`` nested inside an earlier-starting ``[ ... ]`` is consumed by that
    array's decode, so a dict request never digs a nested object out of a
    surrounding array.

    When *prefer* is given, a preferred value is returned only when the choice
    is UNAMBIGUOUS: all preferred matches in the text must be equal (a model
    restating the same payload twice is not ambiguity). Two or more DIFFERENT
    preferred matches return None — the caller cannot know which one is the
    real payload, and guessing (e.g. executing a worked example that precedes
    the actual plan) is worse than failing. When no preferred match exists,
    the first type-matching value is returned as a fallback.
    """
    preferred: list[dict | list] = []
    fallback: dict | list | None = None
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        # Only attempt a decode at a JSON container start. Scanning BOTH
        # delimiters in positional order (not just the expected one) is what
        # prevents digging a nested object out of a surrounding array: a
        # leading "[ ... ]" is decoded as a list, found to be the wrong type,
        # and skipped past in full — so a dict request on "[1, {\\"a\\":2}]"
        # returns None rather than the inner {"a":2}.
        if ch not in "{[":
            i += 1
            continue
        try:
            data, end = _JSON_DECODER.raw_decode(text, i)
        except RecursionError:
            # Adversarially deep nesting (e.g. "[" * 100_000 in prose): the
            # stdlib decoder recurses per nesting level and overflows long
            # before any structural bound. This text is untrusted model output,
            # and callers handle only JSONDecodeError (parse_json's ValueError
            # contract, the spine extractor's never-raises contract) — so the
            # error must not escape. Fail the WHOLE scan closed: a truncated
            # scan cannot certify a preferred match as unambiguous, so keeping
            # candidates collected before the bomb would let a worked example
            # launder past the ambiguity refusal (GPT review, #4974 round 4).
            # Callers already have recovery paths for None (schema retry loop,
            # the spine's forcing re-emit); salvaging a prefix of a reply that
            # contains a nesting bomb is not worth defeating them.
            return None
        except json.JSONDecodeError:
            i += 1
            continue
        if isinstance(data, expected_type):
            if prefer is None:
                return data  # type: ignore[return-value]
            if prefer(data):
                preferred.append(data)
            elif fallback is None:
                fallback = data  # type: ignore[assignment]
        # Valid JSON that is not an immediate result — skip past its full extent.
        i = end
    if preferred:
        first = preferred[0]
        if all(candidate == first for candidate in preferred[1:]):
            return first
        return None
    return fallback


def _parse_llm(text: str, expected_type: type) -> dict | list | None:
    """Parse JSON from LLM output, tolerating fences and surrounding prose.

    Background turns (e.g. memory consolidation) run on a shared lite session.
    On the Claude Code backend that session is not tool/persona-scoped the way
    kiro's no-tools lite agent is, so the model may wrap the JSON in prose. To
    keep consolidation from silently no-opping, fall back to extracting the
    first top-level JSON value of the expected type when a strict parse fails.
    """
    text = text.strip()
    if not text:
        return None
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        data = json.loads(text)
        if isinstance(data, expected_type):
            return data  # type: ignore[return-value]
        return None
    except json.JSONDecodeError:
        # Fallback: extract the first top-level JSON value of the expected type
        # embedded in prose (scans successive delimiters, validates via stdlib).
        result = _extract_json_of_type(text, expected_type)
        if result is None:
            logger.debug("Failed to parse LLM JSON: %.200s", text)
        return result


def parse_llm_json(text: str) -> dict | None:
    """Parse JSON dict from LLM output, stripping markdown fences if present."""
    return _parse_llm(text, dict)  # type: ignore[return-value]


def parse_llm_json_list(text: str) -> list | None:
    """Parse a JSON array from LLM output, stripping markdown fences."""
    return _parse_llm(text, list)  # type: ignore[return-value]


# ── Conversation History Helpers ──


def save_conversation_turn(
    log: ConversationLog,
    key: str,
    user_text: str,
    assistant_text: str,
    source_thread: str | None = None,
    source_user: str | None = None,
    agent: str | None = None,
) -> None:
    """Save a user+assistant conversation turn to the history log.

    Consolidates the repeated pattern of appending user and assistant
    messages with provenance tracking.  When *agent* is supplied it is
    recorded in the session metadata on file creation so that
    ``/kirocrew sessions`` displays the correct agent name.
    """
    log.append(
        key,
        "user",
        # The one place all twelve callers of this helper persist a user row, which is
        # served back to dashboard readers; each caller's prompt value is untouched.
        redact_via_context(user_text),
        source_thread=source_thread,
        source_user=source_user,
        agent=agent,
    )
    if assistant_text:
        log.append(
            key,
            "assistant",
            assistant_text,
            source_thread=source_thread,
            source_user=source_user,
        )


async def save_conversation_turn_off_loop(
    log: ConversationLog,
    key: str,
    user_text: str,
    assistant_text: str,
    source_thread: str | None = None,
    source_user: str | None = None,
    agent: str | None = None,
) -> str | None:
    """Save a turn without blocking (or fail-fast-dropping on) the event loop.

    Returns the ``ts`` of the row this turn ended on, read back INSIDE the atomic
    hold so it is this turn's own row and not some later writer's. A caller that
    stamps something with "how far the conversation had got" needs that value, and
    re-reading the tail afterwards is not the same thing: the permit for this
    session is released before the caller gets here, so a queued second turn can
    land its rows in between and the re-read would return ITS position. Taking the
    value under the lock we already hold costs nothing and removes the window
    rather than narrowing it.

    :func:`save_conversation_turn` makes TWO ``ConversationLog.append`` calls, and
    append acquires a cross-process flock and writes to disk -- ~12 ms each on a
    large transcript. Called directly from an ``async def`` that is worse than
    slow: on a running loop ``_locked`` makes a single NON-blocking acquire and
    raises :class:`~kiro_crew.history.HistoryLockTimeout` on any concurrent
    holder, and most callers swallow that, so the durable copy was dropped
    exactly when another writer was active. Off the loop the same primitive takes
    the patient poll-to-deadline path instead.

    This is the single choke point for every async caller, so the offload cannot
    be forgotten at a new call site and the ten Slack sites do not each restate
    it.

    Unlike :func:`~kiro_crew.history.append_off_loop`, this **awaits** the write
    rather than firing it at the executor and returning. That difference is
    deliberate: callers here go on to refresh a dashboard tab or hand the session
    to consolidation, both of which read the transcript back, so the turn has to
    be on disk before the caller continues. ``append_off_loop`` has no such
    reader and can afford to be fire-and-forget.

    The whole turn is written under one :meth:`~kiro_crew.history.ConversationLog.atomic_appends`
    hold. ``append`` locks per ROW, so without it two concurrent turns for the
    same session could interleave into ``user_A, user_B, assistant_A,
    assistant_B`` -- turns that no longer pair up, which no timestamp ordering can
    repair because each row's ``ts`` is individually correct. On the loop that was
    impossible (a synchronous caller never yields between its two appends), so the
    hazard is introduced BY offloading and has to be closed here rather than
    inherited.
    """

    def _write() -> str | None:
        with log.atomic_appends(key):
            save_conversation_turn(
                log,
                key,
                user_text,
                assistant_text,
                source_thread=source_thread,
                source_user=source_user,
                agent=agent,
            )
            # Reentrant for the same key on the same thread (see
            # ``atomic_appends``), so this reuses the hold rather than
            # deadlocking on it -- and reading it here rather than after the
            # hold is released is the whole point.
            return log.last_row_ts(key)

    return await asyncio.to_thread(_write)
