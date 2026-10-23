"""The model fallback chain, and switching the model of a live session.

Choosing the next ``agent.fallback_model`` candidate when the active model's transient
budget is spent, the visible story of a walk, probing a restore of the primary, the
substitute ``set_model`` seam and the provider's model and backend reads, the route
baseline latch, and the per-session lock a slot's model switch holds.

Served through :mod:`kiro_crew.llm_helpers`, which callers and tests import and
patch; this module never imports it.
"""

from __future__ import annotations

import asyncio
import logging
import weakref
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Any

from kiro_crew.agent_sdk.drivers.acp import resolve_pin_spelling_on
from kiro_crew.agent_sdk.drivers.acp_turn_vocab import LLMProvider, advertised_model_ids
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.llm_helpers_parts.backend_errors import TRANSIENT_RETRIES
from kiro_crew.messaging.link import canonical_key
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

logger = logging.getLogger("kiro_crew.llm_helpers")


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
# surface can prepend a visible notice, and — on the interactive path — announced in chat (see
# _on_acp_transient_exhausted in dashboard/chat_turn/acp_recovery.py). An empty chain (the
# default) disables the feature: behavior is byte-for-byte the pre-feature error surface.

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


def provider_model_pin_refused(provider: LLMProvider) -> bool:
    """True when *provider*'s adapter refused its pinned model at startup.

    A config-option backend applies a pin non-strictly: a refusal leaves the
    session on the backend default and raises nothing. A kiro session withholds
    a startup pin the account does not serve, with the same result. The session
    records the refused or withheld id as ``model_pin_refused``. Callers treat a
    True here exactly as a caught model-unavailable error: annotate the
    downgrade and blank the explicit pin on the usage row.
    """
    # Declared on LLMProvider. The str check keeps a test double's auto-made
    # attribute from reading as a refusal.
    value = provider.model_pin_refused
    return isinstance(value, str) and bool(value)


def provider_model_pin_partial(provider: LLMProvider) -> str:
    """The bare model *provider* runs when its pair pin only half applied.

    A ``<model>[<effort>]`` pin is applied as two writes. When the model lands
    and the effort does not, the session runs the bare model, not the pin and
    not the default. Returns that bare model, or ``""``. A caller billing by the
    pin bills this value instead.
    """
    value = provider.model_pin_partial
    return value if isinstance(value, str) else ""


def next_fallback_candidate(
    chain: Sequence[str],
    active_model: str,
    advertised: Sequence[str] | None,
    *,
    backend: str = "",
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

    A persisted chain entry can carry a stale ``<namespace>::<bare-id>``
    qualifier from the catalog that advertised it (the catalog/session
    spelling-mismatch class)
    while the session advertises the bare id, so membership is judged through
    :func:`resolve_pin_spelling` (the shared fold) rather than literally — an
    entry absent under BOTH spellings is still skipped, and the active-model
    skip applies to the folded spelling too. The CHAIN's own spelling is what
    is returned (``FallbackState.next_candidate`` locates the applied
    candidate with ``remaining.index``); wire-facing consumers re-fold it via
    :func:`_fallback_wire_spelling`.
    """
    adv = [a for a in (advertised or []) if isinstance(a, str) and a.strip()]
    act = (active_model or "").strip().lower()
    for cand in chain:
        if not isinstance(cand, str):
            continue
        low = cand.strip().lower()
        if not low or low == act:
            continue
        if adv:
            served = resolve_pin_spelling_on(cand, adv, backend=backend)
            if not served:
                logger.debug("model fallback: skipping %r (not advertised)", cand)
                continue
            if served.strip().lower() == act:
                # Post-fold active skip: a qualified entry that resolves to
                # the currently-failing model cannot help.
                continue
        return cand
    return None


def _fallback_wire_spelling(
    candidate: str, advertised: Sequence[str] | None, *, backend: str = ""
) -> str:
    """The spelling of *candidate* to send on the wire and keep in records.

    A chain entry stays in its OWN spelling for ``FallbackState`` bookkeeping
    (``remaining.index``), but everything later compared against SERVED models
    — the substitute ``set_model`` call, the swap witness, the sticky
    :data:`TURN_FALLBACK_ATTR` marker the restore probe reads, and the
    active/walked records — must carry the ADVERTISED spelling: a
    ``<namespace>::``-qualified spelling there is one the backend never
    advertised (``AcpClient.set_model``'s explicit-pick guard would raise) and
    desynchronizes the restore probe from the session it watches. Falls back
    to the candidate's own spelling when the advertised set cannot resolve it
    (empty/unknown fails open, matching :func:`next_fallback_candidate`).
    """
    ids = [a for a in (advertised or []) if isinstance(a, str) and a.strip()]
    return (resolve_pin_spelling_on(candidate, ids, backend=backend) if ids else "") or candidate


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

    def next_candidate(
        self, active_model: str, advertised: Sequence[str] | None, *, backend: str = ""
    ) -> str | None:
        """Advance to and return the next usable candidate, or ``None``."""
        remaining = self.chain[self.pos :]
        cand = next_fallback_candidate(remaining, active_model, advertised, backend=backend)
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
    (:data:`TURN_FALLBACK_ATTR`); and emits the greppable swap warning. A
    ``<namespace>::``-qualified chain entry is applied AND recorded under its
    advertised spelling (:func:`_fallback_wire_spelling`) — the wire, the
    marker, and the walked/active records must agree with the served model
    the restore probe later compares against. Returns the applied candidate
    (advertised spelling), or ``None`` when the chain is exhausted or the
    provider exposes no ``set_model`` seam — the caller then surfaces the
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
    backend = provider_backend(provider)
    while True:
        cand = fb_state.next_candidate(fb_state.primary or active, advertised, backend=backend)
        if cand is None:
            return None
        # The chain's own spelling drove the walk bookkeeping; the wire and
        # every served-model comparison below use the advertised spelling.
        wire = _fallback_wire_spelling(cand, advertised, backend=backend)
        if wire.strip().lower() == (active or "").strip().lower():
            # With a marker-seeded primary, the chain can still name the
            # CURRENTLY-failing fallback the session sits on — retrying it is
            # what this walk exists to escape.
            continue
        _raw_before = provider_raw_model(provider)
        try:
            await set_model_fn(wire)
        except Exception:
            logger.debug(
                "model fallback: set_model(%r) failed; skipping candidate",
                wire,
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
            and _raw_after.strip().lower() != wire.strip().lower()
        ):
            logger.debug(
                "model fallback: set_model(%r) was a silent no-op (model still %r); "
                "skipping candidate",
                wire,
                _raw_after,
            )
            continue
        fb_state.active = wire
        fb_state.attempts = 1
        fb_state.walked.append(wire)
        try:
            setattr(provider, TURN_FALLBACK_ATTR, (fb_state.primary, wire))
        except Exception:
            logger.debug("publishing fallback marker failed", exc_info=True)
        logger.warning(
            "model fallback: %s -> %s (reason=throttle-exhaustion, surface=%s%s)",
            fb_state.primary or "?",
            wire,
            surface,
            log_suffix,
        )
        return wire


def pick_epoch_host(provider: Any) -> Any:
    """The one object the explicit-pick epoch lives on for this session.

    A pick and a refusal-fallback restore can hold DIFFERENT layers of the
    same session — the model handler holds the ``AcpProvider`` wrapper while
    the chat runner's acquisition can hand the wrapped client — so both must
    resolve the SAME host or the writer stamps an object the reader never
    sees. The innermost wrapped client wins, following the same unwrap order
    as :func:`resolve_substitute_set_model`; a bare test client resolves to
    itself.
    """
    for attr in ("client", "_client"):
        try:
            inner = getattr(provider, attr, None)
        except Exception:  # pragma: no cover - exotic property getters
            inner = None
        if inner is not None and not callable(inner):
            return inner
    return provider


# Attribute on a session's pick-epoch host carrying the model it runs with
# ``model.route`` routing OFF, as ``(session id, model, pick epoch)``. ONE spelling
# and one validator (:func:`route_baseline_latch`) for both readers: the chat
# runner, which latches it before a session's first routed turn, and the
# sub-agent start, which prices its tiers against it.
ROUTE_BASELINE_ATTR = "_jev_route_baseline"


def _latch_identity(host: Any) -> tuple[str, int]:
    """``(session id, pick epoch)`` of *host*, the two facts a latch is valid for."""
    epoch = getattr(host, "_explicit_pick_epoch", 0)
    epoch = epoch if isinstance(epoch, int) else 0
    sid = str(getattr(host, "_session_id", "") or getattr(host, "session_id", "") or "")
    return sid, epoch


def route_baseline_latch(host: Any) -> str | None:
    """The latched pre-route model on *host*, or ``None`` when no record holds.

    A record holds while it names *host*'s current session and pick epoch: a
    moved epoch is an explicit pick, a different id is a pooled host serving a
    new session. ``""`` is a real latch -- the backend named no model.
    """
    latched = getattr(host, ROUTE_BASELINE_ATTR, None)
    sid, epoch = _latch_identity(host)
    if (
        isinstance(latched, tuple)
        and len(latched) == 3
        and latched[0] == sid
        and latched[2] == epoch
    ):
        return str(latched[1] or "")
    return None


def latch_route_baseline(host: Any, baseline: str) -> None:
    """Record *baseline* on *host* for its current session and pick epoch."""
    sid, epoch = _latch_identity(host)
    setattr(host, ROUTE_BASELINE_ATTR, (sid, baseline, epoch))


_slot_switch_session_locks: "weakref.WeakValueDictionary[str, asyncio.Lock]" = (
    weakref.WeakValueDictionary()
)


def slot_switch_session_lock(session_key: str) -> asyncio.Lock:
    """The per-session lock every explicit model switch runs under.

    Serializes model switches for aliases of ONE session — a channel-born
    slot and its dashboard twin drive one wire session through disjoint slot
    objects, so per-slot locks cannot order their switches. The switch
    handlers in ``chat_handlers`` acquire it between ``slot._lock`` and
    ``slot._model_pick_lock`` (the lock-order contract is documented at
    their acquisition site); the chat runner's refusal-fallback restore
    acquires it before the pick lock, so a restore's ``set_model(primary)``
    await cannot interleave with an alias pick and silently overwrite the
    user's selection. Lives here rather than in
    ``chat_handlers`` because ``chat_handlers`` imports from the runner —
    the runner could not import it back without a cycle.

    Keyed on the CANONICAL spelling of the session key: a Slack session can
    be addressed by its bare legacy ``thread_ts`` (a slot restored from an
    old transcript) and by ``slack:<thread_ts>`` (its canonical sibling), and
    ``SessionManager`` folds the two onto one live session. Two spellings
    that name one session must take one lock, or two aliases would serialize
    against nobody; ``canonical_key`` is the same fold the manager applies.

    A ``WeakValueDictionary`` so a session's lock is collected once no
    request holds it; unrelated sessions resolve different keys and so take
    different locks.

    An ``asyncio.Lock`` binds to the loop it is first contended on and
    raises ``RuntimeError`` when awaited from any other loop. A cached lock
    that is still alive when a different loop asks for the same key (a test
    holding a reference past its per-test loop, an embedder that runs the
    gateway on a fresh loop) is therefore unusable to the caller, so it is
    replaced rather than returned. Holders on the old loop keep their lock;
    the two loops cannot contend with each other in any case.
    """
    session_key = canonical_key(session_key)
    lock = _slot_switch_session_locks.get(session_key)
    if lock is not None and _bound_to_other_loop(lock):
        lock = None
    if lock is None:
        lock = asyncio.Lock()
        _slot_switch_session_locks[session_key] = lock
    return lock


def _bound_to_other_loop(lock: asyncio.Lock) -> bool:
    """Whether ``lock`` is bound to a loop other than the running one.

    ``asyncio.Lock`` records its loop in ``_loop`` on first contention and
    leaves it ``None`` before that; an unbound lock is usable from any loop.
    With no running loop the caller is synchronous setup code and the lock
    is handed back unchanged.
    """
    bound = getattr(lock, "_loop", None)
    if bound is None:
        return False
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        return False
    return bound is not running


def resolve_substitute_set_model(provider: Any) -> Callable[[str], Awaitable[None]] | None:
    """The provider's substitute-path ``set_model`` coroutine, or ``None``.

    Prefers ``provider.set_model``; falls back to the wrapped client
    (``provider.client`` / ``provider._client``) for wrappers that do not
    re-export it. Callers pre-filter candidates
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


def provider_backend(provider: Any) -> str:
    """The provider's ACP backend id, ``""`` when unknown.

    Lets the fallback walk fold a bare pair-id chain entry (e.g. a codex pin)
    to its advertised wire spelling via :func:`resolve_pin_spelling_on`, the
    same backend-aware fold the cold-start, substitute and warm-pool wire
    sites apply; an unknown backend keeps the generic (backend-less) fold.
    """
    for attr in ("backend",):
        try:
            val = getattr(provider, attr, "")
        except Exception:  # pragma: no cover - exotic property getters
            val = ""
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


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
