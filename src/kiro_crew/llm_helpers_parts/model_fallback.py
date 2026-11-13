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

from kiro_crew.agent_sdk.backends import ACP_BACKENDS_MODEL_AT_LAUNCH
from kiro_crew.agent_sdk.drivers.acp import resolve_pin_spelling_on
from kiro_crew.agent_sdk.drivers.acp_turn_vocab import LLMProvider, advertised_model_ids
from kiro_crew.agent_sdk.drivers.acp_vocab import (
    engine_served_model,
    same_model,
    tracker_of,
    walk_outcome,
)
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.llm_helpers_parts.backend_errors import TRANSIENT_RETRIES
from kiro_crew.messaging.link import canonical_key
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

logger = logging.getLogger("kiro_crew.llm_helpers")


def first_advertised_fallback(advertised: Any, rejected: str | None) -> str | None:
    """First advertised model that is neither *rejected* nor ``"auto"``.

    The replacement when the configured model (often ``"auto"``) is refused
    mid-prompt, e.g. on a GovCloud partition that does not serve ``"auto"``.
    Shared by :func:`run_bg_oneliner` and :func:`stream_and_collect`.
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


# Throttle-exhaustion fallback chain (agent.fallback_model).
#
# When the same-model transient budget (TRANSIENT_RETRIES) is spent on a
# throttle/capacity error, an ordered chain of fallback models is tried instead
# of surfacing the error. It is never silent: every swap is logged at warning
# and published on the provider (TURN_FALLBACK_ATTR) so the delivering surface
# can show a notice; the interactive path also announces it in chat
# (_on_acp_transient_exhausted in dashboard/chat_turn/acp_recovery.py). An
# empty chain disables the feature and the error surfaces unchanged.

# Attempts per candidate: initial + ONE ~2s retry, not a fresh TRANSIENT_RETRIES
# budget. Throttle exhaustion is often cell-wide and hits every model, so deep
# per-candidate retries only re-confirm a correlated outage slowly. One retry
# covers a single 5xx on a healthy candidate and bounds the worst case (~35s of
# backoff across a 4-candidate chain).
FALLBACK_CANDIDATE_ATTEMPTS = 2


def fallback_rewound_transient_budget() -> int:
    """Same-model counter value that grants a fresh fallback candidate its budget.

    The dashboard's interactive ladder holds no :class:`FallbackState` between
    turns: after a swap it rewinds ``slot._transient_5xx_retries`` so the
    candidate gets :data:`FALLBACK_CANDIDATE_ATTEMPTS` - 1 further same-model
    retries (the re-queued turn is the first attempt). Deriving it here keeps
    the per-candidate budget in one place with
    :meth:`FallbackState.should_retry_active`. Clamped at zero: an attempts
    value above TRANSIENT_RETRIES + 1 cannot be expressed by this counter and
    collapses to the full same-model budget.
    """
    return max(0, TRANSIENT_RETRIES - (FALLBACK_CANDIDATE_ATTEMPTS - 1))


# Provider attribute carrying the active fallback as ``(primary, candidate)``.
# It is the sticky-restore marker (the next stream_and_collect on the provider
# probes one ``set_model(primary)``) and the visibility source for unattended
# surfaces (cron/heartbeat prepend a warning to the result). Cleared only on a
# successful restore, never at turn end: the swap is sticky for the session.
TURN_FALLBACK_ATTR = "_kc_active_fallback"

# Exception attribute carrying the chain-exhaustion story, set when every
# candidate also failed. :func:`append_fallback_story` adds it to the terminal
# error so an unattended failure names the whole walk, not just the last error.
FALLBACK_STORY_ATTR = "_kc_fallback_story"

# Bound on the story text. The walked ids come from ``agent.fallback_model``
# config (LLM-reachable via MCP) and the advertised check fails OPEN on an empty
# list, so an arbitrarily long string can reach the walk: bound and redact it
# here before it rides any error surface (WS frames, Slack alerts, logs).
_FALLBACK_STORY_CAP = 500


def fallback_story_of(exc: BaseException) -> str:
    """The chain-exhaustion story carried on *exc*, redacted+capped, or ``""``.

    Anything but a non-empty string is treated as absent: a frozen exception
    type may have refused the set, and a hostile ``__getattribute__`` must not
    break error delivery. Redaction and the :data:`_FALLBACK_STORY_CAP` bound
    live here so every consumer gets the same safe text. Never raises.
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

    Used by unattended surfaces (cron failure alerts, sub-agent ``info.error``,
    the heartbeat failure log); the dashboard rebuilds a richer story from
    ``slot._fallback_walked``. With no story *text* is returned unchanged
    (capped at *budget* when given). Never raises.

    :param budget: optional total length bound. The error text is trimmed to
        leave the story room, but never below half the budget: an oversized
        story (up to :data:`_FALLBACK_STORY_CAP`, which may equal the caller's
        cap) must not evict the error, so past that floor the story tail is
        cut. ``None`` appends unbounded (caller owns the cap); a negative value
        becomes 0, since a negative slice would drop the bound.
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


def annotate_model_fallback(text: str, provider: Any, *, stop_reason: str | None = None) -> str:
    """Prepend the throttle-fallback warning to a delivered unattended result.

    Unattended surfaces (cron/heartbeat results, the sub-agent completion
    event) have no chat card, so the result text carries the warning. The
    :data:`TURN_FALLBACK_ATTR` marker is left in place: the swap is sticky, so
    every run on the fallback repeats the warning until the restore probe moves
    the session back. Model ids come from config (LLM-reachable via MCP), so
    they are redacted. Never raises: on failure the un-annotated *text* is
    returned, so an annotation error cannot fail a successful run.

    *stop_reason* is the run's terminal stop reason, which decides how the
    ENGINE's fallback line ends (answered vs no model answered; see
    ``acp.engine_fallback.walk_outcome``). ``None`` means the caller saw no
    terminal, and the line reads as answered, since a result is being delivered.
    """
    try:
        text = _annotate_engine_fallback(text, provider, stop_reason)
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


def _annotate_engine_fallback(text: str, provider: Any, stop_reason: str | None) -> str:
    """Prepend the ENGINE's fallback line for this run, when the engine moved it.

    kiro-cli can move a run onto another model inside one prompt
    (``acp/engine_fallback.py``). The line ends the way the dashboard's notice
    does, by the same rule (``walk_outcome``): a refusal terminal reads as no
    model answered. With no terminal seen (*stop_reason* ``None``) it reads as
    answered. The ids were accepted only as grammar tokens the credential scrub
    leaves unchanged (``parse_model_id``), so the line needs no further scrub.
    """
    tracker = tracker_of(provider)
    if tracker is None:
        return text
    walk = tracker.walk
    outcome = "answered" if stop_reason is None else walk_outcome(stop_reason)
    # Crew's own fallback restores its primary next run, so no promotion stands.
    line = walk.line(outcome, promoted=not provider_fallback_active(provider))
    if not line:
        return text
    return f"⚠ {line}\n\n{text}" if text else f"⚠ {line}"


def engine_attributed_model(pin: str, provider: Any) -> str:
    """The model a usage row records for a run Crew put on *pin*.

    THE shared rule for a move kiro-cli made inside one prompt
    (``acp/engine_fallback.py``): when the engine ran *provider*'s last prompt on
    another model, that model answered the run and *pin* did not, so the row
    names it (``engine_served_model``: the promoted model, else the last hop's
    target). Otherwise *pin* stands. The dashboard, cron, heartbeat and
    sub-agent rows all record through this, so a turn-scoped hop and a
    promotion are billed the same way everywhere, and on top of Crew's own
    fallback: the engine's target answered even a turn Crew moved first.
    """
    return engine_served_model(provider) or pin


def provider_fallback_active(provider: Any) -> bool:
    """True while *provider* carries an active fallback marker.

    The shared usage-attribution guard: while a fallback serves the session,
    an explicit pin (``job.model`` / ``info.model`` / ``slot.model``) must not
    be recorded as the turn's model, or the usage row bills the fallback's
    spend to a model that never ran. Callers blank the pin when this is true
    and defer to ``model_source``, which reads the model that actually ran.
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

    Skips empties, the currently-active (failing) model, and, when an
    advertised list is known, any id the backend did not advertise.
    ``"auto"`` is a real candidate (the backend's availability-aware routing)
    and passes the same check, so a partition without it skips it instead of
    sending a no-op swap. An EMPTY advertised list fails OPEN: entitlement
    unknown is not entitlement denied (as in ``model_is_unusable``), and the
    substitute ``set_model`` re-validates against the live list anyway.

    A persisted entry can carry a stale ``<namespace>::<bare-id>`` qualifier
    while the session advertises the bare id, so membership and the
    active-model skip go through :func:`resolve_pin_spelling_on` rather than a
    literal match. The CHAIN's own spelling is returned
    (``FallbackState.next_candidate`` finds it with ``remaining.index``);
    wire-facing callers re-fold it via :func:`_fallback_wire_spelling`.
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

    ``FallbackState`` bookkeeping keeps the chain's own spelling, but anything
    compared against SERVED models (the ``set_model`` call, the swap witness,
    the :data:`TURN_FALLBACK_ATTR` marker, the active/walked records) needs the
    ADVERTISED spelling: a ``<namespace>::`` spelling there makes
    ``AcpClient.set_model``'s explicit-pick guard raise and desynchronizes the
    restore probe. Falls back to the candidate's own spelling when the
    advertised set cannot resolve it (fails open, like
    :func:`next_fallback_candidate`).
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

        The one home of the per-candidate retry budget; the dashboard's
        counter rewind derives from the same constant
        (:func:`fallback_rewound_transient_budget`). ``True``: retry the
        active candidate once more (the attempt is already recorded).
        ``False``: no candidate is active or its budget is spent, so advance
        via :func:`advance_fallback_candidate`.
        """
        if self.active is None or self.attempts >= FALLBACK_CANDIDATE_ATTEMPTS:
            return False
        self.attempts += 1
        return True

    def exhaustion_story(self) -> str | None:
        """One-line story of a spent chain walk, or ``None`` when none ran.

        ``None`` (nothing walked, e.g. every candidate unadvertised) means the
        error surfaces unchanged, with no story attached.
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

    ``stream_and_collect`` (Case 2.75), the sub-agent transient ladder and the
    dashboard's ``_fallback_swap_for_turn`` all advance through this body, so
    marker semantics and skip rules cannot diverge. It seeds
    ``fb_state.primary`` from a surviving sticky marker first (only the marker
    remembers the true primary of a session already on a fallback), else the
    active model; skips the primary, unadvertised ids and the failing
    candidate; applies the first candidate whose ``set_model`` lands; publishes
    :data:`TURN_FALLBACK_ATTR`; and logs the swap warning. The candidate is
    applied and recorded under its advertised spelling
    (:func:`_fallback_wire_spelling`) so it matches what the restore probe
    later compares. Returns that spelling, or ``None`` when the chain is spent
    or the provider has no ``set_model``; the caller then surfaces the
    original error.
    """
    advertised = provider_advertised_ids(provider)
    # An empty read means auto-routed (``provider_active_model`` filters
    # ``"auto"``) or unknown; either way ``"auto"`` is the honest primary. It
    # makes the restore probe re-enter auto routing instead of clearing with an
    # empty primary (which lets the backfill pin the fallback for good), skips
    # an auto->auto no-op swap, and gives the notice card a real primary.
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
        # "" and returns without switching), and an unwitnessed swap would
        # announce a model that never took over. A provider exposing no model
        # string cannot be witnessed, so it fails open and set_model is trusted.
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
        # ``auto`` names no model. A harness that takes its model at launch
        # (Strands) launches its settings' model for it, and the restore probe
        # compares the marker against what the session runs, so the record is the
        # served model there. Every other harness keeps ``auto`` (H13).
        applied = wire
        # The wrapper (``AcpProvider``) carries no backend; its client does. Read
        # here only, so the spelling fold above keeps its own backend reading.
        launch_backend = backend or provider_backend(pick_epoch_host(provider))
        if wire.strip().lower() == "auto" and launch_backend in ACP_BACKENDS_MODEL_AT_LAUNCH:
            applied = provider_active_model(provider) or wire
        fb_state.active = applied
        fb_state.attempts = 1
        fb_state.walked.append(applied)
        try:
            setattr(provider, TURN_FALLBACK_ATTR, (fb_state.primary, applied))
        except Exception:
            logger.debug("publishing fallback marker failed", exc_info=True)
        logger.warning(
            "model fallback: %s -> %s (reason=throttle-exhaustion, surface=%s%s)",
            fb_state.primary or "?",
            applied,
            surface,
            log_suffix,
        )
        return applied


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

    Serializes switches for aliases of ONE session: a channel-born slot and
    its dashboard twin drive one wire session through different slot objects,
    so per-slot locks cannot order them. ``chat_handlers`` takes it between
    ``slot._lock`` and ``slot._model_pick_lock`` (lock order documented
    there); the chat runner's refusal-fallback restore takes it before the
    pick lock, so a restore's ``set_model(primary)`` cannot overwrite an alias
    pick. It lives here because ``chat_handlers`` imports from the runner and
    the reverse import would be a cycle.

    Keyed on the CANONICAL session key: a Slack session can be addressed by a
    bare legacy ``thread_ts`` and by ``slack:<thread_ts>``, which
    ``SessionManager`` folds onto one session, so both must take one lock.

    A ``WeakValueDictionary``, so a lock is collected once no request holds it.

    An ``asyncio.Lock`` binds to the loop it is first contended on and raises
    ``RuntimeError`` on any other loop, so a cached lock bound to another loop
    (a test's old loop, an embedder's fresh loop) is replaced, not returned.
    Holders on the old loop keep theirs; two loops cannot contend anyway.
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

    Prefers ``provider.set_model``, else the wrapped client's
    (``provider.client`` / ``provider._client``). Callers pre-filter candidates
    against the advertised list, so the explicit-pick guard in
    ``AcpClient.set_model`` / ``AcpSessionProvider.set_model`` does not fire.
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

    The witness reader for fallback transitions: unlike
    :func:`provider_active_model` it keeps ``"auto"``, so a caller can see
    whether a non-raising ``set_model`` moved the session.
    ``resolve_usable_model`` collapses an unservable target to ``""`` and
    ``set_model`` returns WITHOUT switching; reading "didn't raise" as
    "switched" would clear sticky state while still on the fallback, and the
    backfill would then pin it for good.
    """
    for attr in ("served_model", "_model"):
        try:
            val = getattr(provider, attr, "")
        except Exception:  # pragma: no cover - exotic property getters
            val = ""
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


class ActiveModelLine:
    """A built prompt kept naming the model that serves it across a retry loop.

    The prompt for a goose turn carries an ``[ACTIVE MODEL]`` line naming the model
    the session served when the prompt was built. A retry loop that switches the
    model before it re-sends that prompt (a fallback swap, a restore of the primary,
    a rejected-model retry) would otherwise tell the new model it is the old one.
    Build one right after the prompt is built, then send :meth:`current` on every
    attempt: it rewrites the line when the provider's ``served_model`` has moved
    since the prompt last named it, and is the prompt unchanged otherwise.

    The model is read the way the prompt builder read it, through the provider's
    public ``served_model`` accessor; a provider without a readable one never
    rewrites anything.
    """

    def __init__(self, message: str, provider: Any) -> None:
        self._provider = provider
        # Every restatement starts from the prompt as built, so an attempt whose
        # model reads as unreported (which drops the line) cannot lose it for the
        # attempts after it.
        self._built = message
        self._built_model = self._served()
        self._message = message
        self._stated = self._built_model

    def _served(self) -> str:
        try:
            value = getattr(self._provider, "served_model", "")
        except Exception:  # pragma: no cover - exotic property getters
            return ""
        return value if isinstance(value, str) else ""

    def current(self) -> str:
        """The prompt, its ``[ACTIVE MODEL]`` line naming the model serving now."""
        served = self._served()
        if served != self._stated and isinstance(self._built, str):
            # Imported here: context assembly is above this layer, and a module-level
            # import would make the facade serve the name.
            from kiro_crew.context_assembly.sections import restate_active_model

            self._message = restate_active_model(self._built, self._built_model, served)
            self._stated = served
        return self._message

    def restate_tail(self, sent: str) -> tuple[str, str] | None:
        """*sent*, which ends with the prompt this line holds, with that tail restated.

        For a caller that prepended to the built prompt before sending it. Returns the
        new sent prompt and the restated built prompt, or ``None`` when the line is
        already right or *sent* does not end with the prompt held (a step that did
        more than prepend leaves the prompt as it is).
        """
        held = self._message
        restated = self.current()
        if restated is held or not sent.endswith(held):
            return None
        return sent[: len(sent) - len(held)] + restated, restated


def _consume_engine_promotion(provider: Any, candidate: str, current: str) -> bool:
    """Whether kiro-cli's last promotion moved *provider* from *candidate* to *current*.

    On a match the promotion is consumed: the caller moves its marker to
    *current*, so a later promotion chains from there, not from *candidate*.
    The unattended surfaces' counterpart of the dashboard's retarget
    (``dashboard/engine_fallback.py``). Never raises.
    """
    try:
        tracker = tracker_of(provider)
        if tracker is None or not tracker.promotion:
            return False
        frm, to = tracker.promotion
        if not (same_model(candidate, frm) and same_model(current, to)):
            return False
        tracker.promotion = None
        return True
    except Exception:
        return False


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

    The one restore-probe body: unattended surfaces call it bare (state from
    :data:`TURN_FALLBACK_ATTR`); the dashboard's slot probe
    (``chat_runner._probe_fallback_restore_for_slot_locked``) passes slot state
    and the hooks below. It restores only while the session is still on the
    fallback; a model change in between clears the sticky state without
    touching the model, so a later explicit pick is never overridden. Success
    clears the state and logs (no chat notice); failure keeps the fallback for
    this turn. Never raises.

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
        # A hostile marker property, a raising ``__bool__``/``__str__`` or a
        # malformed tuple skips the probe instead of raising.
        if state is None:
            marker = getattr(provider, TURN_FALLBACK_ATTR, None)
            if not marker:
                return
            primary, candidate = marker
        else:
            primary, candidate = state
            if not candidate:
                return
        # Coerce once. The marker path has no empty-candidate return (a
        # ``(primary, "")`` marker still probes and clears); the state path
        # matches the slot probe's no-active-fallback no-op.
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
        # A raising heal/clear hook would abort the turn it runs at the start
        # of, which is worse than the stale state it was tidying.
        try:
            fn()
        except Exception:
            logger.debug("fallback restore %s hook failed", what, exc_info=True)

    _clear = clear if clear is not None else _default_clear
    current = provider_active_model(provider)
    if state is None and _consume_engine_promotion(provider, candidate, current):
        if same_model(current, primary):
            # The engine moved back onto the primary: the fallback is over.
            _run_hook(_clear, "clear")
            return
        # The engine, not the user, moved the session off Crew's fallback:
        # the fallback still stands, so the marker follows and the restore runs.
        candidate = current
        try:
            setattr(provider, TURN_FALLBACK_ATTR, (primary, current))
        except Exception:
            logger.debug("fallback restore: could not move the marker", exc_info=True)
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
    # Witness the restore before clearing: set_model(primary) can be a silent
    # no-op (an "auto" primary the partition does not advertise resolves to
    # ""). Clearing while still on the fallback lets the next backfill pin it
    # for good, so keep the marker and retry at the next turn start.
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
        # record, a half-cleared record is worse than a missed heal, and
        # keeping it would not buy a retry of the heal.
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

    The config is a SINGLE value and every surface derives the order here:
    ``""`` disables the feature (``()``, so Case 2.75 stays inert); ``"auto"``
    (the default) yields ``("auto",)``, the backend's availability-aware
    routing; a concrete id yields ``(id, "auto")``, the pin first and
    ``"auto"`` as the final fallthrough.

    ``KiroCrewConfig.load()`` is fingerprint-cached, so the steady-state cost
    is two stats, safe to run on the event loop every turn.
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
