"""``model.route`` -- how hard is this turn, and which model should answer it?

The shipped behaviour is that a chat slot runs every turn on one model: the id the
owner pinned, or the backend's own default. That is a single setting for a
conversation whose turns are not one difficulty -- "fix this typo" and "design the
migration" are the same slot and the same price.

This point asks the oracle to put ONE turn in one of three tiers and maps the tier
to a model id from ``decisions.model_route``. A tier the owner pinned sets the
model for that turn; a refusal leaves the session on the model it was already on,
which is exactly what every unconsented install does.

An UNPINNED tier is not a refusal. Every tier ships as ``""`` -- inherit -- because
no model id may be hardcoded as a default (``model-selection.md``): an id an
account is not entitled to fails on the first prompt. So the shipped behaviour is
that the tier is answered, recorded and shown ("complex -> (unpinned)") while the
turn keeps its session's model. That is what makes the feature observable before
anyone pins anything: an owner can read which tier their turns land in, then pin
the ids their own account is offered.

Never behind the owner's back
-----------------------------
The point runs only when the owner SELECTED it. The chat model picker carries an
``Auto (Jev)`` entry -- drawn only while the seam is consented and the governance
ceiling permits it -- and choosing it records :data:`~kiro_crew.dashboard.state`'s
``jev_route`` flag on the slot. A manual model pick clears the flag, so a concrete
pin is never overridden by a tier: the pick IS the answer to the question this
point asks.

Normal chat turns only
----------------------
:func:`routed_model` is called from the dashboard's local turn runner for a turn
whose actor is the user. Cron deliveries, sub-agent turns, crew-relayed turns, app
injections and autonudge wakes are excluded at the call site, because none of them
has an owner watching the price of the answer and each already resolves its model
through its own tier (``agent.role_models``, a crew binding, a cron's own slot).

With ``decisions.model_route_derive`` on, the START of an unpinned sub-agent is
asked about too (:func:`routed_spawn_model`), once, before its process exists --
a start with a model of its own is never asked -- and empty tiers are filled
from the advertised list (:func:`derive_tier_models`).

Runs on the event loop
----------------------
Unlike ``skills.select``, this point's caller is already a coroutine, so the
``decide`` await needs no cross-thread hand-off and no wait budget of its own:
``gate.decide`` bounds the provider call by ``timeout_secs`` and returns ``None``
on expiry. The two filesystem reads this point makes before that -- the history
ceiling and the transcript tail -- are pushed to a thread, because the caller's
thread is the loop that serves every other session.

Everything is a refusal back to the session's own model
-------------------------------------------------------
:func:`routed_model` returns ``None`` for: the seam is off, the session is not
sampled, the answer is outside the three tiers, the mapped id is not one the
provider advertises, the transport failed, or the budget expired. ``None`` means
"leave the model alone", so the call site needs no try/except and no feature check.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import time
import uuid
from typing import Any, Callable, Mapping, Sequence

from kiro_crew import decisions as core
from kiro_crew import model_registry
from kiro_crew.decisions import log as _log
from kiro_crew.decisions.points import build_history, history_budget, prior_turns
from kiro_crew.decisions.types import Answer, Choice, Question

logger = logging.getLogger(__name__)

POINT = "model.route"

#: The question's id, and therefore the key the answer arrives under.
QUESTION_ID = "tier"

#: The three tiers, in the order the question offers them. A CLOSED domain: the
#: gate refuses an answer outside a question's declared options, so a provider
#: inventing a fourth tier reads as an invalid result rather than as a mapping
#: lookup that quietly misses.
TIER_SIMPLE = "simple"
TIER_MEDIUM = "medium"
TIER_COMPLEX = "complex"
TIERS: tuple[str, ...] = (TIER_SIMPLE, TIER_MEDIUM, TIER_COMPLEX)

#: One sentence per tier, sent as part of the question. They describe the WORK,
#: never a model or a price: the mapping from tier to model is the owner's
#: (``decisions.model_route``), and naming a model here would ask the oracle to
#: price the turn rather than to judge it.
TIER_DESCRIPTIONS: dict[str, str] = {
    TIER_SIMPLE: (
        "a short, local, mechanical request answerable in one step -- a rename, "
        "a lookup, a small edit, a direct factual question"
    ),
    TIER_MEDIUM: (
        "ordinary work over a few files or steps -- write a function, explain some "
        "code, fix a bug whose cause is already named"
    ),
    TIER_COMPLEX: (
        "work needing a plan, a trade-off or reasoning across a whole system -- "
        "design, architecture, a diagnosis with no named cause, a risky refactor"
    ),
}

#: The one prompt sentence that is not a tier description: which way an error
#: costs more. It names no model and no price, the rule the descriptions above
#: follow, so the oracle judges the work rather than pricing the turn.
TIER_ASYMMETRY = (
    "Being wrong in the two directions does not cost the same. A turn put in a "
    "lower tier than it needs gets less room to work in and a worse answer; a "
    "turn put higher only costs more. So answer simple only when the request is "
    "self-contained and you can see the whole of it -- a request that is a plan, "
    "a judgement call, several instructions at once, or that names work you "
    "cannot see, is not simple however short it is."
)

#: Characters of the current message sent with the question -- the SAME bound
#: ``skills.select`` applies, because it is the same text answering a question
#: about the same turn, and two different excerpt sizes would mean the consent
#: text describes one of them.
MAX_MESSAGE_CHARS = 2000

#: Characters per token, the same rough divisor ``skills.select`` and
#: ``compaction.keep`` estimate with. A real tokenizer is not worth importing for a
#: fitting decision, and over-estimating costs one downgrade while under-estimating
#: costs a history. It is calibrated on LATIN text, which is why
#: :func:`prompt_tokens` does not apply it to every character.
CHARS_PER_TOKEN = 4

#: Lowest tier probability that may move a turn to a SMALLER context window. A
#: CONSTANT for ``memory.recall``'s reason: it is the meaning of the answer
#: rather than a knob, and a configurable one could turn the guard off unseen.
MIN_DOWNGRADE_P = 0.80

#: Every tier unpinned. No model id is hardcoded here, because one an account is
#: not entitled to fails on the first prompt
#: (``docs/system-specs/common/model-selection.md``); the ids come from
#: ``decisions.model_route``, which the owner writes. An unpinned tier is answered
#: and recorded, and applies nothing.
DEFAULT_TIER_MODELS: dict[str, str] = {tier: "" for tier in TIERS}

#: The credit multiplier the picker's cost badges are relative to -- Auto's own
#: rate. A derived tier is priced against the session's own model, and against
#: this when that model has no known price (the backend's default, a cold list).
BASELINE_MULTIPLIER = 1.0

#: The backend's own-default sentinel. Never a derived target: it names no model,
#: so routing a tier to it would hand the choice to the service-side router.
AUTO_MODEL = "auto"

#: ``source`` on a route record whose model was derived rather than pinned. Absent
#: on every other record, so a record with derivation off is shaped as it always is.
SOURCE_DERIVED = "derived"

#: ``derive`` on a route record when derivation was on and no advertised model had
#: a price, so nothing could be derived. Absent on every other record.
DERIVE_UNPRICED = "unpriced"

#: What the log row and the strip carry for ``model_chosen`` when the answered tier
#: is unpinned. A NAMED value rather than an absent field: "Jev said complex and
#: nothing was applied" is a real outcome an owner acts on by pinning that tier, and
#: a missing key would read as a broken producer.
UNPINNED = ""

#: Row ``error`` categories for a tier that arrived and could NOT be applied.
#: Each is written rather than dropped, because the silent form is the worst
#: outcome available: the owner picked ``Auto (Jev)``, the seam answers every turn,
#: the gate's own call row says the decision succeeded, and every answer is
#: discarded for a reason the log would otherwise not mention.
#:
#: ``model-not-advertised`` -- the tier maps to an id this account cannot run
#: (a config typo, a plan change, a model withdrawn by the provider).
#: ``no-switch-seam`` -- this provider exposes no ``set_model``, so a per-turn
#: model cannot be expressed on it at all.
#: ``switch-failed`` -- the provider refused or failed the switch.
ERROR_UNKNOWN_MODEL = "model-not-advertised"
ERROR_NO_SWITCH = "no-switch-seam"
ERROR_SWITCH_FAILED = "switch-failed"

#: A downgrade the window rules refused: the tier's model has a SMALLER context
#: window than the session's, and either the answer is under
#: :data:`MIN_DOWNGRADE_P` or the turn's own history would not fit there.
ERROR_WINDOW_REFUSED = "smaller-window-refused"

#: The module the strip hand-off lives in, resolved by name at call time so a
#: build without it is a no-op rather than an import error on a turn path.
OUTCOMES_MODULE = "kiro_crew.decisions.outcomes"
PUBLISH_ATTR = "publish"


def tier_models(config: Any | None = None) -> dict[str, str]:
    """The tier-to-model map: all three tiers, each a pin or ``""`` for inherit.

    Always all three, so "this tier is unpinned" has a value rather than being
    inferred from a missing key -- the log and the strip report that state.

    Never raises. An unreadable section reads as every tier unpinned, which applies
    nothing: the fail-closed direction for a value that decides what a turn costs.
    """
    try:
        from kiro_crew.decisions.gate import model_route_map

        configured = model_route_map(config)
    except Exception:
        logger.debug("model.route: tier map unreadable; every tier unpinned", exc_info=True)
        return dict(DEFAULT_TIER_MODELS)
    return {tier: str(configured.get(tier) or "").strip() for tier in TIERS}


def derive_enabled(config: Any | None = None) -> bool:
    """Whether ``decisions.model_route_derive`` is on. Never raises; off on failure."""
    try:
        from kiro_crew.decisions.gate import model_route_derive

        return model_route_derive(config)
    except Exception:
        logger.debug("model.route: derive switch unreadable; off", exc_info=True)
        return False


def _price(rate_of: Callable[[str], float | None], model_id: str) -> float | None:
    """*model_id*'s multiplier through *rate_of*, or ``None`` when it has no price."""
    try:
        rate = rate_of(model_id)
    except Exception:
        return None
    if isinstance(rate, bool) or not isinstance(rate, (int, float)) or rate <= 0:
        return None
    return float(rate)


def _sole_extreme(candidates: Sequence[tuple[str, float]], *, cheapest: bool) -> str:
    """The one candidate at the extreme price, or ``""`` when no single one is.

    A price tie goes to the larger KNOWN context window, so a 1M sibling of the
    same model wins over its 200K spelling and the window guard has nothing to
    refuse. A tie that survives that -- same price, same or unknown window --
    inherits rather than picking by list order, which is not a fact about either
    model.
    """
    if not candidates:
        return ""
    prices = [rate for _, rate in candidates]
    best = min(prices) if cheapest else max(prices)
    names = [name for name, rate in candidates if rate == best]
    if len(names) == 1:
        return names[0]
    windows = {name: known_window(name) or 0 for name in names}
    widest = max(windows.values())
    winners = [name for name in names if windows[name] == widest]
    return winners[0] if widest > 0 and len(winners) == 1 else ""


def derive_tier_models(
    advertised: Sequence[str],
    baseline_model: str = "",
    rate_of: Callable[[str], float | None] | None = None,
) -> dict[str, str]:
    """Tier targets read off the live advertised list and its credit multipliers.

    ``simple`` is the cheapest advertised model priced BELOW the baseline,
    ``medium`` is the baseline itself (``""``: the session keeps its own model),
    and ``complex`` is the dearest advertised model priced ABOVE it. So a derived
    ``simple`` never costs more than the session's own model and a derived
    ``complex`` never costs less. The baseline's price is *baseline_model*'s, or
    :data:`BASELINE_MULTIPLIER` when that model has none.

    Every id comes from *advertised*, the account's own live list, so nothing here
    names a model. An empty list, a list with no prices, and an unbroken tie each
    leave the tier ``""``, which inherits. *rate_of* is the price lookup for the
    session's OWN backend (:func:`rate_lookup`); ``None`` -- no catalog priced that
    backend -- derives nothing.
    """
    derived = dict(DEFAULT_TIER_MODELS)
    if rate_of is None:
        return derived
    lookup = rate_of
    priced: list[tuple[str, float]] = []
    seen: set[str] = set()
    for raw in advertised:
        name = str(raw or "").strip()
        if not name or name.lower() == AUTO_MODEL or name in seen:
            continue
        seen.add(name)
        rate = _price(lookup, name)
        if rate is not None:
            priced.append((name, rate))
    if not priced:
        return derived
    baseline = str(baseline_model or "").strip()
    reference = _price(lookup, baseline) if baseline and baseline.lower() != AUTO_MODEL else None
    if reference is None:
        reference = BASELINE_MULTIPLIER
    derived[TIER_SIMPLE] = _sole_extreme(
        [entry for entry in priced if entry[1] < reference], cheapest=True
    )
    derived[TIER_COMPLEX] = _sole_extreme(
        [entry for entry in priced if entry[1] > reference], cheapest=False
    )
    return derived


def effective_tier_models(
    mapping: Mapping[str, str],
    *,
    advertised: Sequence[str],
    baseline_model: str = "",
    derive: bool = False,
    rate_of: Callable[[str], float | None] | None = None,
) -> tuple[dict[str, str], frozenset[str]]:
    """The tier map a route applies, and which of its tiers were derived.

    With *derive* off this is *mapping* unchanged and no tier is derived. With it
    on, each tier the owner left EMPTY takes :func:`derive_tier_models`' target; a
    tier the owner pinned keeps the pin whatever the derivation says.
    """
    effective = {tier: str(mapping.get(tier, "") or "").strip() for tier in TIERS}
    if not derive:
        return effective, frozenset()
    targets = derive_tier_models(advertised, baseline_model, rate_of)
    derived: set[str] = set()
    for tier in TIERS:
        if not effective[tier] and targets.get(tier):
            effective[tier] = targets[tier]
            derived.add(tier)
    return effective, frozenset(derived)


def rate_lookup(provider: Any) -> Callable[[str], float | None] | None:
    """The credit-multiplier lookup for *provider*'s own backend, or ``None``.

    Prices come from one backend's catalog and are kept under that backend's
    registry namespace, so they are read back only for a session whose backend
    resolves to the same namespace: a harness with a vocabulary of its own gets
    ``None`` -- no prices, nothing derived -- even when its ids fold onto the
    catalog's. The backend is read off the provider and then off the session it
    wraps; a backend outside ``ACP_BACKENDS_KNOWN`` gets ``None``. Never raises.
    """
    if provider is None:
        return None
    try:
        from kiro_crew.agent_sdk.backends import ACP_BACKENDS_KNOWN, model_registry_namespace
        from kiro_crew.llm_helpers import pick_epoch_host, provider_backend

        backend = provider_backend(provider) or provider_backend(pick_epoch_host(provider))
        if backend not in ACP_BACKENDS_KNOWN:
            return None
        namespace = model_registry_namespace(backend)
    except Exception:
        logger.debug("model.route: no price lookup for this session's backend", exc_info=True)
        return None

    def _lookup(model_id: str) -> float | None:
        return model_registry.rate_multiplier(model_id, namespace)

    return _lookup


def has_prices(advertised: Sequence[str], rate_of: Callable[[str], float | None] | None) -> bool:
    """Whether any advertised id has a price under *rate_of*."""
    if rate_of is None:
        return False
    return any(_price(rate_of, str(name or "").strip()) is not None for name in advertised)


#: Escalation is remembered for at most this many session keys; the oldest is
#: dropped first. A forgotten key costs one step of escalation, never a turn.
ESCALATION_MAX_KEYS = 1024

#: The tier each session's last routed unit of work ran at, the tier its next
#: routed turn may not go below, and the keys whose routed turn has not settled.
#: In memory only, and written only while ``decisions.model_route_derive`` is on,
#: so with the switch off nothing here is ever read back.
_LAST_TIER: dict[str, str] = {}
_FLOOR: dict[str, str] = {}
_UNSETTLED: dict[str, str] = {}


def step_up(tier: str) -> str:
    """The tier one above *tier*; ``complex`` stays ``complex``; ``""`` stays ``""``."""
    if tier not in TIERS:
        return ""
    return TIERS[min(TIERS.index(tier) + 1, len(TIERS) - 1)]


def _higher(left: str, right: str) -> str:
    """The higher of two tiers, ``""`` reading as below every tier."""
    if left not in TIERS:
        return right if right in TIERS else ""
    if right not in TIERS:
        return left
    return left if TIERS.index(left) >= TIERS.index(right) else right


def _remember(store: dict[str, str], key: str, tier: str) -> None:
    """Store *tier* for *key*, dropping the oldest key past :data:`ESCALATION_MAX_KEYS`."""
    store.pop(key, None)
    store[key] = tier
    while len(store) > ESCALATION_MAX_KEYS:
        store.pop(next(iter(store)))


def escalated_tier(session_key: str | None, tier: str, *, retried: bool = False) -> str:
    """*tier* raised to this session's escalation floor, or one step for a retry.

    A turn that FAILED leaves a floor one tier above the tier it ran at, so the
    next routed turn runs at least there. A RETRIED turn runs one tier above the
    tier the turn it repeats ran at. Neither ever lowers the answer.
    """
    if not session_key:
        return tier
    floor = _FLOOR.get(session_key, "")
    if retried:
        floor = _higher(floor, step_up(_LAST_TIER.get(session_key, "")))
    return _higher(tier, floor)


def note_routed(session_key: str | None, tier: str) -> None:
    """Record that *session_key*'s current unit of work is routed at *tier*."""
    if not session_key or tier not in TIERS:
        return
    _remember(_LAST_TIER, session_key, tier)
    _remember(_UNSETTLED, session_key, tier)


#: Key on a :func:`routed_model` answer that escalation follows. Never written to
#: a row: :func:`build_outcome` copies named fields only.
TRACKED = "_escalation_tracked"


def note_applied(session_key: str | None, routed: Mapping[str, Any]) -> None:
    """Record *routed*'s tier for :func:`settle` once the turn runs on it.

    Called by the turn runner only on the exits where the session demonstrably
    runs the tier's model -- switched to it, or already on it -- and never where
    the answer is thrown away (a window refusal, no switch seam, a pick that moved
    the premise, a switch that did not take): a tier the turn did not run at is
    not one a failure or a regenerate may step up from. An answer whose switch
    the runner recorded as not taken (``applied`` false: a backend that refused
    the model without raising) is not followed either. A no-op for an answer
    escalation does not follow, which is every answer while
    ``decisions.model_route_derive`` is off.
    """
    if routed.get(TRACKED) and routed.get("applied", True):
        note_routed(session_key, str(routed.get("tier") or ""))


def settle(session_key: str | None, ok: bool | None) -> None:
    """How *session_key*'s routed turn ended: ``True`` ok, ``False`` failed, ``None`` neither.

    A success clears the floor, so the next request is classified on its own. A
    failure leaves a floor one tier above the tier the turn ran at. ``None`` -- a
    Stop press, a recovered stall -- changes nothing. A key with no routed turn in
    flight is a no-op, which is every key while derivation is off.
    """
    if not session_key:
        return
    tier = _UNSETTLED.pop(session_key, "")
    if not tier or ok is None:
        return
    if ok:
        _FLOOR.pop(session_key, None)
    else:
        _remember(_FLOOR, session_key, step_up(tier))


def questions() -> list[Question]:
    """The one question, with each tier's description carried in ``criteria``.

    ONE ``Choice``, because the answer is consumed: a second question would be a
    second thing to reconcile with a turn that runs on exactly one model.

    The per-tier rubric rides in the provider's own per-option ``criteria`` map,
    keyed by the option it describes, rather than being flattened into the prompt.
    Both backends know how to carry it -- ``impl_jev`` sends it as the ``criteria``
    map and ``impl_llm`` renders it beside each option -- so ``Choice`` behaves the
    same whichever oracle answers. At runtime, though, only ``nudge.wake`` is served
    by the LLM lane, so for ``model.route`` it is Jev that receives the rubric; the
    ``impl_llm`` rendering is kept deliberately as groundwork for a future
    LLM-lane point rather than because this point reaches that backend today. The
    prompt keeps only what is NOT per-option -- the question stem and
    :data:`TIER_ASYMMETRY`, which is a property of the work's outcome, not of one
    tier.
    """
    return [
        Choice(
            QUESTION_ID,
            "How hard is this request for an AI coding assistant? "
            f"Answer one of: {', '.join(TIERS)}. {TIER_ASYMMETRY}",
            options=list(TIERS),
            criteria=dict(TIER_DESCRIPTIONS),
        )
    ]


def build_state(
    text: str,
    history_rows: Sequence[Mapping[str, str]] = (),
) -> dict[str, Any]:
    """The state sent to the oracle: this message, and the prior turns.

    No candidate menu and no model id: the question is about the WORK, and the
    tier-to-model mapping is resolved locally afterwards. ``history`` is OMITTED
    when there is none, the same shape rule ``skills.select`` follows, so at the
    shipped budget of 0 the request carries the message alone.
    """
    state: dict[str, Any] = {"message": (text or "")[:MAX_MESSAGE_CHARS]}
    if history_rows:
        state["history"] = [dict(row) for row in history_rows]
    return state


def read_tier(answers: Any) -> str:
    """The tier the answer names, or ``""``. Identity is exact.

    The gate has already held the value against the declared options, so this is
    the second check rather than the only one -- and it is here because the value
    is about to index a map the owner writes, where a near-miss spelling would
    read as an absent key and route the turn somewhere nobody chose.
    """
    if not isinstance(answers, dict):
        return ""
    answer = answers.get(QUESTION_ID)
    if not isinstance(answer, Answer):
        return ""
    value = answer.value
    return value if isinstance(value, str) and value in TIERS else ""


def probability_of(answers: Any) -> float | None:
    """The tier answer's probability, or ``None``. Only read after :func:`read_tier`."""
    if not isinstance(answers, dict):
        return None
    answer = answers.get(QUESTION_ID)
    return answer.p if isinstance(answer, Answer) else None


def resolve_model(tier: str, mapping: Mapping[str, str], advertised: Sequence[str]) -> str:
    """The model id *tier* maps to, or ``""`` when it is not one to switch to.

    ``""`` -- keep the session's current model -- covers an UNPINNED tier (the
    shipped state for all three), a tier absent from the map, and a pin the
    provider does not advertise. The caller tells the first two from the third by
    reading the pin itself (:func:`is_unpinned`), because they are different
    outcomes: unpinned is the documented state and is reported as one, while an
    unusable pin is a finding. The advertised list is the
    same one the dashboard's picker is built from
    (``llm_helpers.provider_advertised_ids``), so "the owner could have picked
    this by hand" and "the tier may route to it" are one test.

    An EMPTY advertised list is "not known", not "nothing is runnable", and reads
    as permitted: the list comes from a live provider read that can be cold, and
    refusing on a cold read would make the feature silently inert on the first
    turn after a restart. The switch itself still fails loudly if the id is wrong.
    """
    wanted = str(mapping.get(tier, "") or "").strip()
    if not wanted:
        return ""
    known = [str(name or "").strip() for name in advertised]
    known = [name for name in known if name]
    if known and not any(_same_model(wanted, name) for name in known):
        return ""
    return wanted


def is_unpinned(tier: str, mapping: Mapping[str, str]) -> bool:
    """Whether *tier* names no model, so the turn keeps the one it is on.

    Separate from :func:`resolve_model` returning ``""`` on purpose: an unpinned
    tier is the shipped, documented state and is REPORTED (one row, one strip
    line), while a pin the account cannot run is a finding and is recorded as an
    error. Both keep the model, and conflating them would hide the second.
    """
    return not str(mapping.get(tier, "") or "").strip()


def _same_model(left: str, right: str) -> bool:
    """Whether two model ids name the same model, tolerating dot/dash and case.

    The canonical registry is the backend's own answer to this question, so it is
    asked first; the fold below is the fallback for an id the registry does not
    list (a GPT/Qwen id, a future model, an operator-typed one), and it matches
    the frontend's ``normalizeModelKey`` fallback exactly.
    """
    try:
        canonical_left = model_registry.canonical_key(left)
        canonical_right = model_registry.canonical_key(right)
        if canonical_left and canonical_right:
            return canonical_left == canonical_right
    except Exception:
        logger.debug("model.route: canonical model comparison unavailable", exc_info=True)
    return left.strip().lower().replace(".", "-") == right.strip().lower().replace(".", "-")


def known_window(model_id: str) -> int | None:
    """*model_id*'s context window in tokens when the registry KNOWS it, else ``None``.

    ``has_known_window`` is the gate rather than ``model_window`` alone, which
    answers a guessed reference for an id it does not list: vetoing a switch on a
    guess pins routing to the session's own model for every unlisted one.

    Asked as written and then case-folded, because BOTH registry lookups are
    spelling-sensitive while :func:`resolve_model` accepts an owner's pin on a lossless
    fold and answers the spelling THEY wrote. Asked only as written, a pin cased
    differently from the registry's entry reads as a window nothing knows -- and that
    is the answer that refuses nothing, so the pin would carry the very shrink these
    rules exist to refuse. ``.`` and ``-`` are deliberately NOT folded together: the
    registry keeps those entries distinct because they are two different windows.
    """
    name = str(model_id or "").strip()
    if not name:
        return None
    for candidate in (name, name.lower()):
        try:
            canonical = model_registry.canonical_key(candidate) or candidate
            found = (
                model_registry.model_window(canonical)
                if model_registry.has_known_window(canonical)
                else None
            )
        except Exception:
            logger.debug("model.route: no known window for %r", model_id, exc_info=True)
            return None
        if isinstance(found, int) and found > 0:
            return found
    return None


def prompt_tokens(text: str) -> int:
    """Estimated tokens the prompt a turn SENDS adds to what a context meter reports.

    The meter answers for the transcript a session ALREADY holds, and the turn a
    window rule is deciding for adds its whole assembled prompt on top -- the person's
    words plus every prefix the turn carries, not the excerpt the answer was
    classified from. Without it a large prompt over a small history passes a fit test
    taken on the history alone and then crosses the threshold at the end of that same
    turn -- which is the compaction the rule exists to prevent, so leaving it out
    makes the rule pass exactly what it is for. An estimate, deliberately: see
    :data:`CHARS_PER_TOKEN`.

    The divisor answers for LATIN text. CJK, Indic and symbol-dense input tokenizes
    several times denser, so applying it to every character reads such a prompt as a
    quarter of its size and clears the shrink this rule exists to refuse -- on
    ordinary input rather than a crafted one. So only ASCII is divided, and every
    other character is counted as a token of its own. That is the conservative
    direction and the only one that is safe here: it can refuse a downgrade the
    prompt would in fact have fitted, which costs one turn on a larger window, while
    the opposite costs a history nothing recovers.
    """
    raw = str(text or "")
    dense = sum(1 for ch in raw if ord(ch) > 127)
    return (len(raw) - dense) // CHARS_PER_TOKEN + dense


def is_smaller_window(*, current: int | None, target: int | None) -> bool:
    """Whether moving from *current* to *target* SHRINKS the room a turn has.

    ``False`` when either window is unknown: an unknown window is not evidence of a
    shrink. THE one place that answers this, because both window rules are scoped by
    it and a caller deciding it a second way could govern a different set of moves.
    """
    if current is None or target is None:
        return False
    return target < current


def permits_smaller_window(p: float | None, *, current: int | None, target: int | None) -> bool:
    """Whether a tier's model may be applied, given the two context windows.

    ``True`` for every move to a window at least as large as the current one: the
    harm is one-directional, and room that does not shrink changes nothing about
    what fits. ``True`` as well when either window is unknown (``None``), which is
    :func:`known_window`'s answer for a model the registry has not met -- refusing
    there makes routing inert on exactly the models nothing is known about.

    A SMALLER window needs the answer's own probability at :data:`MIN_DOWNGRADE_P`
    or above. An absent one does not clear it: the rule asks the answer to carry
    the move, and a row with no number carries nothing. The floor sits in a gap
    rather than on a slope -- tier answers cluster below 0.75 and above 0.86.
    """
    if not is_smaller_window(current=current, target=target):
        return True
    if not isinstance(p, (int, float)) or isinstance(p, bool):
        return False
    return float(p) >= MIN_DOWNGRADE_P


async def routed_model(
    text: str,
    *,
    session_key: str | None = None,
    current_model: str = "",
    advertised: Sequence[str] = (),
    history_source: Callable[[], Sequence[Mapping[str, Any]]] | None = None,
    config: Any | None = None,
    retried: bool = False,
    track: bool = True,
    rate_of: Callable[[str], float | None] | None = None,
    scopes: Sequence[str] = (),
) -> dict[str, Any] | None:
    """The model this turn should run on, or ``None`` to leave it alone.

    Returns the OUTCOME dict the caller applies and the strip renders:
    ``{turn_id, tier, p, model_chosen, baseline_model, latency_ms}``.

    ``model_chosen`` is either a model the provider advertises (or the map's pin
    when the advertised list is unknown), which the caller switches to without a
    second check, or :data:`UNPINNED` (``""``) when the answered tier names no
    model. The second is the shipped state and is not a failure: the caller records
    and publishes it, and switches nothing.

    *current_model* is the model the session WOULD have used -- the served model,
    or ``""`` when the backend serves its own default. It is recorded as
    ``baseline_model`` so the log can answer what the routing changed, and it is
    what a refusal keeps.

    *advertised* is the provider's advertised id list; an empty list reads as
    "not known" (see :func:`resolve_model`).

    With ``decisions.model_route_derive`` on, empty tiers take derived targets
    (:func:`effective_tier_models`, priced through *rate_of*, the session's own
    backend lookup from :func:`rate_lookup`) and the answered tier is raised by
    :func:`escalated_tier` -- one step for a *retried* turn, and to the floor a
    turn too hard for its tier left -- and the answer is marked for
    :func:`note_applied`, which the caller invokes once the turn runs on it. A
    caller whose session key is not the unit of work being escalated (a sub-agent
    start asks under its parent's key) passes ``track=False``, which skips both.
    With the switch off neither *retried* nor *track* changes anything.

    With the switch on and no advertised model priced -- a cold catalog, or a
    backend no catalog prices -- the record says ``derive: "unpriced"``, so an
    owner can read why turning derivation on moved nothing.

    *scopes* are keystone scopes this request needs beyond the point's own,
    checked by the gate against the keystone read that authorizes the send
    (``gate.REQUEST_SCOPES``); a chat turn passes none.

    Never raises except :class:`asyncio.CancelledError`, which ``decide``
    propagates: cancellation is the turn going away, not a decision failure.
    """
    # Read once, before the send, and used for both the apply and the report. Every
    # tier unpinned is NOT a reason to skip the question: the answer is what tells
    # an owner which tier their turns land in, which is how they decide what to pin.
    derive = derive_enabled(config)
    mapping, derived_tiers = effective_tier_models(
        tier_models(config),
        advertised=advertised,
        baseline_model=current_model,
        derive=derive,
        rate_of=rate_of,
    )
    unpriced = derive and not has_prices(advertised, rate_of)
    turn_id = uuid.uuid4().hex[:16]
    trace: dict[str, Any] = {}
    # Bound before the try so the latency below is always measurable, including
    # when the history reads themselves were what took the time.
    started = time.monotonic()
    try:
        # The budget FIRST, off the loop: it reads the keystone as well as the
        # config, and at the shipped default of 0 there is nothing for a
        # transcript read to contribute.
        budget = await asyncio.to_thread(history_budget)
        rows = await asyncio.to_thread(prior_turns, history_source) if budget > 0 else []
        history_rows = build_history(rows, text, history_budget_chars=budget, trace=trace)
        state = build_state(text, history_rows)
        extra: dict[str, Any] = {
            "turn_id": turn_id,
            "history_chars": trace.get("history_chars", 0),
            "truncated": trace.get("truncated", 0),
        }
        answers = await core.decide(
            POINT,
            state,
            questions(),
            session_key=session_key,
            config=config,
            extra=extra,
            scopes=scopes,
            unclipped=text,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        # This sits on the path that answers every message of a routed slot, so
        # the seam may cost an observation and must never cost a turn.
        logger.debug("model.route: keeping the session's own model", exc_info=True)
        return None
    latency_ms = int((time.monotonic() - started) * 1000)
    answered = tier = read_tier(answers)
    if not tier:
        return None
    tracked = derive and track
    if tracked:
        tier = escalated_tier(session_key, tier, retried=retried)
    chosen = UNPINNED if is_unpinned(tier, mapping) else resolve_model(tier, mapping, advertised)
    if not chosen and not is_unpinned(tier, mapping):
        # The tier arrived, the owner DID pin it, and the pin is not one this
        # account can run. The call row the gate wrote says the decision succeeded,
        # so without this row the log would report a healthy seam while every turn
        # kept the old model.
        #
        # Off the loop: ``record_error`` appends to the day-file under a lock and
        # sweeps expired files, and THIS function runs on the event loop. Its own
        # docstring says a caller here hands it to a thread, which is what the
        # runner already does for the two error rows it writes.
        await asyncio.to_thread(
            record_error,
            session_key,
            turn_id=turn_id,
            tier=tier,
            latency_ms=latency_ms,
            error=ERROR_UNKNOWN_MODEL,
        )
        return None
    routed: dict[str, Any] = {
        "turn_id": turn_id,
        "tier": tier,
        "p": probability_of(answers),
        "model_chosen": chosen,
        "baseline_model": current_model or "",
        "latency_ms": latency_ms,
    }
    if chosen and tier in derived_tiers:
        routed["source"] = SOURCE_DERIVED
    if tier != answered:
        routed["escalated_from"] = answered
    if unpriced:
        routed["derive"] = DERIVE_UNPRICED
    if tracked:
        routed[TRACKED] = True
    return routed


def build_outcome(routed: Mapping[str, Any]) -> dict[str, Any]:
    """The fields the outcome row and the strip share, off :func:`routed_model`'s answer.

    ``latency_ms`` is deliberately NOT here: it is a core row field
    (:func:`~kiro_crew.decisions.log.build_row`), so the row carries it at top
    level and an ``extra`` naming it would be dropped.
    """
    row = {
        "turn_id": routed.get("turn_id"),
        "tier": routed.get("tier"),
        "p": routed.get("p"),
        "model_chosen": routed.get("model_chosen"),
        "baseline_model": routed.get("baseline_model") or "",
    }
    # Present only on a derived choice, so a record with derivation off keeps the
    # shape it has always had.
    if routed.get("source"):
        row["source"] = str(routed.get("source"))
    # Present only when escalation raised the answer, for the same reason.
    if routed.get("escalated_from"):
        row["escalated_from"] = str(routed.get("escalated_from"))
    # Present only when derivation was on and had no price to derive from.
    if routed.get("derive"):
        row["derive"] = str(routed.get("derive"))
    # Present only on an APPLY: an unpinned tier switches nothing, so a row saying
    # "not applied" there would name a failure where the shipped state is no pin.
    if "applied" in routed:
        row["applied"] = bool(routed.get("applied"))
        row["model_used"] = str(routed.get("model_used") or "")
    return row


def record_outcome(session_key: str | None, routed: Mapping[str, Any]) -> bool:
    """One outcome row for the routed turn, then the publish hook. Never raises.

    Returns whether the strip was published, for a test to assert on. The publish
    is CONDITIONAL on the write, the same rule ``skills.select`` follows: a strip
    whose durable row was refused describes a decision no verdict could be filed
    against.
    """
    try:
        row = _log.build_row(
            point=POINT,
            session_key=session_key,
            latency_ms=int(routed.get("latency_ms") or 0),
            extra=build_outcome(routed),
        )
        written = _log.append(row)
    except Exception:
        logger.debug("model.route: could not record the outcome row", exc_info=True)
        return False
    if not written:
        logger.debug("model.route: outcome row was not written; not publishing it")
        return False
    return publish_outcome(session_key, row)


def publish_outcome(session_key: str | None, outcome: dict[str, Any]) -> bool:
    """Hand *outcome* to :data:`OUTCOMES_MODULE` if this build has one. Never raises.

    Resolved by name at CALL time rather than imported at module scope: the module
    is optional, and a top-level import would make this point unimportable on a
    build without it. The row is passed exactly as it was written, so what the
    dashboard shows and what the log holds cannot drift.
    """
    try:
        try:
            module = importlib.import_module(OUTCOMES_MODULE)
        except ImportError:
            return False
        publish = getattr(module, PUBLISH_ATTR, None)
        if publish is None:
            return False
        publish(session_key, outcome)
        return True
    except Exception:
        logger.debug("model.route: could not publish the outcome", exc_info=True)
        return False


def record_error(
    session_key: str | None,
    *,
    turn_id: str,
    tier: str,
    latency_ms: int,
    error: str,
    p: float | None = None,
) -> None:
    """One row for a tier that arrived and could not be applied. Never raises.

    Never PUBLISHED: the turn ran on the model it was already on, so there is no
    routing for a receipt to describe and no pair of models for a reader to rate.
    Blocking (the append is filesystem IO), so a caller on the event loop hands it
    to a thread.

    *p* is the refused answer's own probability, recorded when the category is
    about the ANSWER rather than about a model id: a reader tells the two window
    rules apart by whether that number clears the floor.
    """
    extra: dict[str, Any] = {"turn_id": turn_id, "tier": tier}
    if p is not None:
        extra["p"] = p
    try:
        _log.append(
            _log.build_row(
                point=POINT,
                session_key=session_key,
                latency_ms=latency_ms,
                error=error,
                extra=extra,
            )
        )
    except Exception:
        logger.debug("model.route: could not record the %s row", error, exc_info=True)


#: ``subject`` on a route row written for a sub-agent start rather than a chat turn.
SUBJECT_SUBAGENT = "subagent"

#: The keystone scopes a sub-agent start's question needs beyond ``model.route``'s
#: own (``gate.REQUEST_SCOPES``): its task is a tool-call argument.
SPAWN_SCOPES: tuple[str, ...] = ("tool_args",)


def inherited_baseline(provider: Any) -> str | None:
    """The model *provider*'s session runs with routing OFF, or ``None`` when unnameable.

    The baseline a sub-agent start is priced against. NOT the live served model:
    from the second routed turn on that is the previous turn's tier, so a start
    spawned after a ``simple`` turn would be priced against the cheap model. So
    the chat runner's latch is read first, while it still names this session and
    pick epoch; then, while a fallback substitute serves the session, the primary
    it substituted for, and ``None`` when that is unnamed (the substitute is not
    a baseline); and only then the served model. Never raises.
    """
    try:
        from kiro_crew.llm_helpers import (
            TURN_FALLBACK_ATTR,
            pick_epoch_host,
            provider_active_model,
            provider_fallback_active,
            route_baseline_latch,
        )

        latched = route_baseline_latch(pick_epoch_host(provider))
        if latched is not None:
            return latched
        if provider_fallback_active(provider):
            marker = getattr(provider, TURN_FALLBACK_ATTR, None)
            primary = str(marker[0] or "") if isinstance(marker, (tuple, list)) and marker else ""
            return primary or None
        return provider_active_model(provider)
    except Exception:
        logger.debug("model.route: no nameable baseline for a sub-agent start", exc_info=True)
        return None


def record_spawn_outcome(session_key: str | None, routed: Mapping[str, Any], run_id: str) -> bool:
    """One outcome row for a routed sub-agent start. Never raises; never published.

    Written under the PARENT's session key -- the session whose consent and
    sampling the question rode -- and marked ``subject: subagent`` with the run's
    id, so a reader tells it from that session's own turns. Not published: the
    strip belongs to the parent's next reply, which this start did not shape.
    Blocking (the append is filesystem IO), so a caller on the loop hands it to a
    thread.
    """
    extra = build_outcome(routed)
    extra["subject"] = SUBJECT_SUBAGENT
    extra["run_id"] = run_id
    try:
        return bool(
            _log.append(
                _log.build_row(
                    point=POINT,
                    session_key=session_key,
                    latency_ms=int(routed.get("latency_ms") or 0),
                    extra=extra,
                )
            )
        )
    except Exception:
        logger.debug("model.route: could not record the sub-agent row", exc_info=True)
        return False


async def routed_spawn_model(
    task: str,
    *,
    session_key: str | None,
    current_model: str = "",
    advertised: Sequence[str] = (),
    run_id: str = "",
    config: Any | None = None,
    rate_of: Callable[[str], float | None] | None = None,
) -> tuple[str, str]:
    """``(model, tier)`` for an UNPINNED sub-agent start, or ``("", "")`` to leave it.

    Runs only with ``decisions.model_route_derive`` on: with it off a start is
    never asked about, which is how every spawn routes today. It also needs the
    keystone's ``tool_args`` scope, because the task it sends is a tool-call
    argument rather than text the owner typed; without it nothing is sent. The
    task is put in
    a tier by the SAME question a chat turn gets, under the parent's
    *session_key* (its consent and sampling), and the tier resolves through the
    same effective map -- the owner's pins, derived targets for empty tiers.

    *current_model* is the model the start would inherit, which is also the
    baseline a derived tier is priced against. ``""`` comes back for the baseline
    tier, for a tier that resolves to that same model, and for a move to a
    smaller context window the answer's probability does not carry
    (:func:`permits_smaller_window`), which writes the ``smaller-window-refused``
    row a chat turn writes. The tier comes back beside the model so a caller
    can tell a start that was asked from one that was not.
    """
    if not derive_enabled(config):
        return "", ""
    routed = await routed_model(
        task,
        session_key=session_key,
        current_model=current_model,
        advertised=advertised,
        config=config,
        track=False,
        rate_of=rate_of,
        # The task is a TOOL-CALL ARGUMENT (the agent wrote it into ``spawn_run``),
        # not text the owner typed, so it may leave the machine only under the
        # keystone's ``tool_args`` scope -- the consent ``tool.risk`` rides for the
        # same category -- checked by the gate against the very keystone read that
        # authorizes the endpoint.
        scopes=SPAWN_SCOPES,
    )
    if not routed:
        return "", ""
    tier = str(routed.get("tier") or "")
    chosen = str(routed.get("model_chosen") or "")
    if chosen and current_model and _same_model(chosen, current_model):
        chosen = ""
    if chosen and not permits_smaller_window(
        routed.get("p"),
        current=known_window(current_model),
        target=known_window(chosen),
    ):
        await asyncio.to_thread(
            record_error,
            session_key,
            turn_id=str(routed.get("turn_id") or ""),
            tier=tier,
            latency_ms=int(routed.get("latency_ms") or 0),
            error=ERROR_WINDOW_REFUSED,
            p=routed.get("p"),
        )
        return "", tier
    outcome = dict(routed)
    outcome["model_chosen"] = chosen
    await asyncio.to_thread(record_spawn_outcome, session_key, outcome, run_id)
    return chosen, tier
