"""``decide`` -- the one function a point calls, and every refusal in front of it.

Four refusals, cheapest first, each returning ``None``: the keystone
``decisions_consent.json`` does not consent for the configured provider endpoint
(``decisions.consent.permits``); *point* is not in :data:`DECISION_POINT_NAMES`;
the session hashes outside ``decisions.bucket``; the state or a question carries a
credential or an exfiltration-shaped URL. Then the call itself, which returns
``None`` on a timeout, a provider failure, or an answer outside the declared domain.

The first three write nothing at all, which is what makes "no consent leaves the
log directory empty" checkable. The scrub and the call failures write one row,
because a silent scrub is the worst outcome available: the operator would read a
missing row as "not firing" when the seam fires and refuses every time.

Two properties the order is chosen for. Consent is the first thing checked and the
keystone read is the only await before the refusal, so a seam without consent is
provably inert rather than merely fast (pinned by
``test_decisions_gate.TestDisabledPerformsNoAwait``). And the scrub sits BEFORE
the network, so no transport can exist above it.

Config comes from the live watcher's snapshot, read ONCE per call, never from
disk; consent comes from the keystone, never from ``config.json`` (that file is
agent-writable, so it carries no ``enabled`` at all). Anything unreadable -- no
snapshot, no section, a keystone that is absent, corrupt or not a literal
``true`` for this endpoint, a bucket that is not a number, an attribute read that
raises -- resolves to OFF: fail-closed is the only safe direction for a gate whose
open state sends conversation text to a third party.

Two budgets, both named here so a caller can size its own outer wait: the provider
call is bounded by :func:`timeout_secs`, and the row write by
:data:`_LOG_BUDGET_SECS` plus, only after an overrun, the bounded
:data:`_LOG_COMMIT_GRACE_SECS`. Nothing this module logs carries a provider message
or a traceback -- a row's ``error`` is one of the identifiers below and the application
log gets the exception CLASS only, because both artifacts are readable and a provider
can quote the request back.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import threading
import time
from hashlib import sha256
from typing import Any, Mapping, Sequence

from kiro_crew import credential_patterns as _cred
from kiro_crew.config.sections import (
    DECISION_HISTORY_BUDGET_DEFAULT,
    DECISION_PROVIDER_ENDPOINT_DEFAULT,
    DECISION_PROVIDER_MODEL_DEFAULT,
)
from kiro_crew.decisions import consent as _consent
from kiro_crew.decisions import log as _log
from kiro_crew.decisions import provider as _provider
from kiro_crew.decisions.types import (
    SCORE_MAX_LEVELS,
    SCORE_MIN_LEVELS,
    Answer,
    Answers,
    Box,
    Choice,
    ImageAsset,
    Noul,
    Question,
    Score,
    is_model_id,
    question_texts,
)

logger = logging.getLogger(__name__)

#: Decision points this build ships; an absent name is refused. Lives here, not in
#: ``config.sections``: nothing in the config is keyed by point name, and this keeps
#: the config loader off a hot path's import graph.
DECISION_POINT_NAMES = (
    "skills.select",
    "tool.risk",
    "message.steer",
    "model.route",
    "compaction.keep",
    "memory.recall",
    "nudge.wake",
    "browser.step",
)

#: The ONE point that has two providers, so its authority is not simply "the
#: keystone consents"; the lane selection below is keyed on it. Its module in
#: ``decisions/points/`` owns the questions and the verdict mapping and does not
#: know which lane answered.
JUDGE_POINT = "nudge.wake"

#: The two lanes, owned by the provider registry (``decisions.provider``).
#: :data:`LANE_ENDPOINT` is the configured System One endpoint, hosted or an
#: attested local preset; :data:`LANE_SESSION_LLM` (``LANE_LLM``) is one tool-less
#: model call on the session's own provider. ``decisions.nudge_wake.provider`` also
#: accepts ``auto``, which resolves to one of the two. The endpoint lane's VALUE is
#: ``"jev"`` because it is persisted in config, the dashboard's judge rows and the
#: log; ``LANE_JEV`` is an alias of it.
LANE_ENDPOINT = _provider.LANE_ENDPOINT
LANE_SESSION_LLM = _provider.LANE_SESSION_LLM
LANE_LLM = LANE_SESSION_LLM
LANE_JEV = LANE_ENDPOINT

#: The LLM lane's budget floor and ceiling, in seconds, from the registry entry
#: that owns them (``provider.SESSION_LLM_TIMEOUT_WINDOW``). ``provider.timeout_ms``
#: is sized for a System One call (1 s default, ~100 ms in practice), too short for
#: a text model, so the configured value is CLAMPED into this window: a raised
#: ``timeout_ms`` is honoured up to a ceiling that keeps one tick from holding its
#: loop for a minute.
_LLM_TIMEOUT_MIN_SECS, _LLM_TIMEOUT_MAX_SECS = _provider.SESSION_LLM_TIMEOUT_WINDOW

#: Points whose request carries TOOL-CALL ARGUMENTS, and which therefore need the
#: keystone's ``tool_args`` scope on top of consent itself
#: (``consent.consented_tool_args``). A point is in here because of what it SENDS:
#: consent covers only the text the owner reviewed (the message excerpt and the
#: candidate descriptions), so an absent scope refuses the point outright.
POINTS_NEEDING_TOOL_ARGS = frozenset({"tool.risk"})

#: Points whose request carries a WHOLE SLOT TRANSCRIPT -- the conversation text and
#: every tool input in it -- and which therefore need the keystone's ``compaction``
#: scope (``consent.consented_compaction``). Separate from ``tool_args`` (the
#: arguments of the call about to run): this is everything the session has run, in
#: a request one to two orders of magnitude larger.
POINTS_NEEDING_COMPACTION = frozenset({"compaction.keep"})

#: Points whose request carries the TEXT OF RECALLED MEMORIES, and which therefore
#: need the keystone's ``memory_text`` scope (``consent.consented_memory_text``). A
#: recalled memory is text the AGENT wrote turns or days ago, unlike a message the
#: owner just typed or a description this build shipped, so it has its own scope.
POINTS_NEEDING_MEMORY_TEXT = frozenset({"memory.recall"})

#: Points whose request carries EVIDENCE GATHERED FROM OTHER SESSIONS AND THIRD
#: PARTIES -- a watched worker's transcript tail, a bot's review comment body, a
#: work-ledger event -- and which therefore need the keystone's ``nudge_evidence``
#: scope (``consent.consented_nudge_evidence``). Separate from ``compaction`` (the
#: owning session's own transcript): this is text from conversations the owner was
#: not in and from a forge they do not control.
POINTS_NEEDING_NUDGE_EVIDENCE = frozenset({"nudge.wake"})

#: Points whose request carries the OUTLINE OF THE AGENT'S BROWSER TAB, and which
#: therefore need the keystone's ``browser`` scope (``consent.consented_browser``).
#: It reads as granted when absent: the owner chose that an armed decision model
#: helps the browser by default, and an explicit ``false`` turns it off. Its
#: screenshots are a request scope of their own (:data:`IMAGE_SCOPES`), on the same
#: default-on terms and sent only to a provider that declares images.
POINTS_NEEDING_BROWSER = frozenset({"browser.step"})

#: Request scopes that authorize sending an IMAGE. ``decide(images=...)`` refuses
#: unless the request names one of these, so no caller can send a picture under a
#: text scope: an image cannot be scrubbed the way text is.
IMAGE_SCOPES = frozenset({_consent.STATE_KEY_BROWSER_IMAGES})

#: The model id sent when the config leaves ``provider.model`` empty -- the same
#: fallback ``impl_systemone`` applies, so the id the scrub clears is the id sent.
_DEFAULT_MODEL = DECISION_PROVIDER_MODEL_DEFAULT

#: Bucket modulus and upper clamp, matching the config's declared 0..100 bounds.
_BUCKET_MOD = 100

#: Provider budget used when the config carries no usable ``timeout_ms``.
_DEFAULT_TIMEOUT_MS = 1000.0
_MIN_TIMEOUT_SECS = 0.001

#: How long the row write may hold the caller before the receipt gets a short grace
#: period to observe a definitive commit or refusal. The write is one ``O_APPEND`` of
#: a few hundred bytes, so the first bound exists only so a stalled filesystem cannot
#: make an observation cost the turn.
_LOG_BUDGET_SECS = 0.05

#: Extra time for an append that crossed the write budget to publish its commit signal
#: or finish with a refusal. If neither happens, the receipt remains unknown rather
#: than reporting a false refusal while the worker may still commit.
_LOG_COMMIT_GRACE_SECS = 0.10

#: A row's ``error`` is one of these -- an identifier an operator can act on, never
#: a provider message, which is unbounded and can quote the request back.
ERROR_TIMEOUT = "timeout"
ERROR_PROVIDER = "provider"
ERROR_INVALID_RESULT = "invalid-result"
ERROR_SCRUBBED_CREDENTIAL = "scrubbed:credential"
ERROR_SCRUBBED_URL = "scrubbed:exfiltration-url"
ERROR_SCRUBBED_SCAN_FAILED = "scrubbed:scan-failed"
#: The unclipped text was longer than :data:`MAX_UNCLIPPED_SCAN_CHARS`, so it was
#: not scanned. Its own category so a long paste never reads as a scanner fault.
ERROR_SCRUBBED_SCAN_BOUND = "scrubbed:scan-bound"
#: ``provider.model`` is not a model id (``types.MODEL_ID_RE``). The field is in
#: the agent-writable config and goes on the wire verbatim, so anything but a
#: short identifier is refused as an egress channel, not sent as a model name.
ERROR_SCRUBBED_MODEL = "scrubbed:provider-model"

#: The categories that mean nothing was sent; a row's ``scrubbed`` flag is
#: membership here rather than a prefix match.
SCRUB_ERRORS = (
    ERROR_SCRUBBED_CREDENTIAL,
    ERROR_SCRUBBED_URL,
    ERROR_SCRUBBED_SCAN_FAILED,
    ERROR_SCRUBBED_SCAN_BOUND,
    ERROR_SCRUBBED_MODEL,
)

#: Longest *unclipped* text the scrub reads (see :func:`decide`). A point clips its
#: excerpt at a few thousand characters; this leaves room past that clip for a
#: credential that starts inside it, a PEM block included. Longer text refuses:
#: any cut of the scan can leave a credential's anchor past the cut.
MAX_UNCLIPPED_SCAN_CHARS = 8192

#: Credential spellings refused locally, before the canonical scanner. Compiled
#: here because ``credential_patterns`` exports pattern SOURCE strings. This is the
#: scrubber-side AWS spelling, not the wider redaction one, and not the whole of
#: the scrub: ``VENDOR_TOKEN_PATTERNS`` carries a generic ``sk-`` form that
#: ``redact_credentials`` does not, which is why both run. The shape-only JWT
#: spelling is left to ``redact_credentials``, which counts a hit only when its
#: header is a JSON object, so a state naming ``honeyJar.atlassian.net`` passes.
_CREDENTIAL_RE = re.compile(
    "|".join([_cred.AWS_KEY_ID] + [frag for _label, frag in _cred.VENDOR_TOKEN_PATTERNS])
)


def _probability(value: object) -> bool:
    """Whether *value* is a real number in 0..1 (a bool is not a probability)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(value) and 0.0 <= value <= 1.0


def _answers_are_valid(answers: Answers, questions: list[Question]) -> bool:
    """Whether every question got exactly one answer inside its own domain."""
    if not isinstance(answers, dict):
        return False
    by_id = {question.id: question for question in questions}
    if set(answers) != set(by_id):
        return False
    for question_id, question in by_id.items():
        answer = answers[question_id]
        if not isinstance(answer, Answer) or answer.id != question_id:
            return False
        if not _probability(answer.p):
            return False
        if not _value_in_domain(answer.value, question):
            return False
    return True


def _value_in_domain(value: object, question: object) -> bool:
    """Whether *value* lies in *question*'s own domain, by question type.

    One branch per type, so a type with no ``options`` never reaches the
    ``Choice`` check, and a question of any other class is out of domain rather
    than an exception: this runs outside ``decide``'s ``try``, so a raise here
    would reach the caller instead of becoming the ``None`` a refusal is.
    """
    if isinstance(question, Choice):
        return isinstance(value, str) and value in question.options
    if isinstance(question, Noul):
        return _probability(value)
    if isinstance(question, Box):
        return _is_box(value)
    if isinstance(question, Score):
        levels = len(question.levels)
        if not SCORE_MIN_LEVELS <= levels <= SCORE_MAX_LEVELS:
            return False
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False
        return math.isfinite(value) and 0.0 <= value <= levels - 1
    return False


def _is_box(value: object) -> bool:
    """Whether *value* is ``{left, top, right, bottom}`` fractions with a positive area."""
    if not isinstance(value, dict) or set(value) != {"left", "top", "right", "bottom"}:
        return False
    if not all(_probability(value[edge]) for edge in value):
        return False
    return bool(value["left"] < value["right"] and value["top"] < value["bottom"])


def _snapshot() -> Any:
    """The live config snapshot, or ``None``.

    The ``config.live`` import is function-local because it pulls the whole loader
    in, and this package is imported lazily from a hot path precisely to avoid that.
    """
    from kiro_crew.config import live

    return live.snapshot()


def _decisions_config(config: Any | None) -> Any | None:
    """The ``decisions`` section of *config*, or of the live snapshot.

    *config* is an injection seam for tests and for a caller that already holds a
    config; ``None`` means "read the snapshot".
    """
    cfg = config if config is not None else _snapshot()
    return None if cfg is None else getattr(cfg, "decisions", None)


def configured_endpoint(config: Any | None = None) -> str:
    """The endpoint a decision would be sent to under *config* (default when unset).

    The same fallback ``impl_systemone`` applies, spelled here so the consent check and
    the send cannot disagree about where "" points.
    """
    decisions = _decisions_config(config)
    provider = getattr(decisions, "provider", None)
    raw = _consent.normalize_endpoint(getattr(provider, "endpoint", ""))
    return raw or DECISION_PROVIDER_ENDPOINT_DEFAULT


def configured_provider_model(config: Any | None) -> str:
    """The configured ``provider.model`` as written, for telling a local preset apart."""
    provider = getattr(_decisions_config(config), "provider", None)
    return str(getattr(provider, "model", "") or "")


#: Endpoints already warned about, so a mismatch is said once, not once per message.
_unconsented_warned: set[str] = set()

#: Whether the governance denial has been said out loud yet, so a pinned-off fleet
#: gets one line rather than one per message.
_capability_denied_warned = False


def _capability_denied(session_key: str | None, *, local: bool = False) -> bool:
    """Whether the governance ceiling withdraws the seam. Filesystem IO.

    *local* adds ``capabilities.decisions_local`` to ``capabilities.decisions``:
    true when the configured provider is a local preset, so a fleet that permits
    the hosted endpoint can still withdraw a model on this machine. A hosted deny covers it.

    *session_key* is the turn's own identity (runtime state, not caller input like the
    ``X-Session-Key`` header), so a profile bound to THIS surface is consulted. With no
    session the probe's own default surface applies.

    Imported here, not at module scope: the probe pulls in
    ``platform.governance_profiles``, which only an install past consent should pay for.
    """
    global _capability_denied_warned
    from kiro_crew.decisions.capability import DASHBOARD_SURFACE_KEY, is_decisions_denied

    if not is_decisions_denied(session_key or DASHBOARD_SURFACE_KEY, local=local):
        return False
    if not _capability_denied_warned:
        _capability_denied_warned = True
        logger.warning(
            "decisions: the seam is withdrawn by governance (%s); nothing is sent "
            "even though consent is on",
            (
                "capabilities.decisions or capabilities.decisions_local"
                if local
                else "capabilities.decisions"
            ),
        )
    return True


def _targets_denied(
    config: Any | None, session_key: str | None, point: str, model: str, lane: str
) -> bool:
    """Whether the ceiling's ``points`` / ``models`` / ``endpoints`` lists refuse this
    request. Filesystem IO; never raises (an error denies).

    The ``models`` and ``endpoints`` lists bind the decision PROVIDER, which only
    the Jev lane uses. The judge's LLM lane sends to the session's own model
    provider through the session runner, which picks the served model itself, so
    it is asked about its point only.
    """
    from kiro_crew.decisions.capability import DASHBOARD_SURFACE_KEY, targets_denied

    try:
        jev = lane == LANE_JEV
        endpoint = configured_endpoint(config) if jev else None
        if endpoint is not None:
            from kiro_crew.decisions.impl_sagemaker import is_sagemaker_endpoint, policy_endpoint

            # The ceiling is asked about the AWS host the request reaches, not the
            # endpoint name; an address that names no host raises and denies below.
            if is_sagemaker_endpoint(endpoint):
                endpoint = policy_endpoint(endpoint)
        return targets_denied(
            session_key or DASHBOARD_SURFACE_KEY,
            point=point,
            model=model if jev else None,
            endpoint=endpoint,
        )
    except Exception as exc:
        logger.debug("decisions: %s target check failed (%s)", point, type(exc).__name__)
        return True


def _consented_for(
    config: Any | None,
    session_key: str | None = None,
    point: str | None = None,
    scopes: Sequence[str] = (),
) -> bool:
    """Read the keystone and hold it against the configured endpoint. Filesystem IO.

    A mismatch -- consent recorded for one address, config now naming another --
    is a refusal, and it is said out loud once per address: this is the state a
    redirected ``provider.endpoint`` produces, and an operator must be able to tell
    it apart from "off".

    Every ``decide`` and ``is_enabled`` path funnels its one keystone read through
    here, so this is where the GOVERNANCE ceiling (``capabilities.decisions``) makes
    a consented keystone inert. It is checked AFTER the keystone on purpose: an
    install without consent must cost nothing, and the governed probe writes an
    audited SEL row on every evaluation.

    *point* lets a scoped point (:data:`POINTS_NEEDING_TOOL_ARGS` and the like) be
    refused on a keystone that consents to sending but not to THAT category. It is
    checked here, not in the IO-free :func:`_sampled`, to reuse this keystone read.
    ``None`` asks for no scope.

    *scopes* names further keystone scopes one request needs (:data:`REQUEST_SCOPES`),
    held against the same read so scope and endpoint come from one snapshot.
    """
    endpoint = configured_endpoint(config)
    from kiro_crew.decisions.local_models import ENDPOINT_NONE

    # "No decision model" chosen: nothing to send to, whatever the keystone says --
    # so the keystone is not even read.
    if endpoint == ENDPOINT_NONE:
        return False
    state = _consent.load_state()
    if _consent.permits(endpoint, state):
        if not _scope_consented(point, state):
            return False
        if not _request_scopes_consented(scopes, state):
            return False
        from kiro_crew.decisions.capability import is_local_preset, names_local_preset

        model = configured_provider_model(config)
        local = is_local_preset(endpoint, model)
        # A preset's address is sent to only while the runtime attests the server on
        # it. Until then -- the model downloading, starting, or a program that took
        # the port -- whatever listens there is not known to be ours.
        if not local and names_local_preset(endpoint, model):
            return False
        return not _capability_denied(session_key, local=local)
    if _consent.is_enabled(state) and endpoint not in _unconsented_warned:
        _unconsented_warned.add(endpoint)
        logger.warning(
            "decisions: consent was given for a different provider endpoint; "
            "nothing is sent until the owner consents again in Settings"
        )
    return False


#: Points already warned about for a missing scope, so a consented install that has
#: not opted in says so once rather than once per tool call.
_unscoped_warned: set[str] = set()


#: Which KEYSTONE FIELD each scoped point's consent is recorded in, as
#: ``point -> state key``. The dashboard's per-point panel writes this exact key back
#: through ``PUT /api/decisions/consent``. Built from the same sets as the enforcement
#: table below, so a point is both enforced and listed; ``test_decisions_gate.py``
#: pins the two against each other.
POINT_SCOPE_KEYS: dict[str, str] = {
    **{p: _consent.STATE_KEY_TOOL_ARGS for p in POINTS_NEEDING_TOOL_ARGS},
    **{p: _consent.STATE_KEY_COMPACTION for p in POINTS_NEEDING_COMPACTION},
    **{p: _consent.STATE_KEY_MEMORY_TEXT for p in POINTS_NEEDING_MEMORY_TEXT},
    **{p: _consent.STATE_KEY_NUDGE_EVIDENCE for p in POINTS_NEEDING_NUDGE_EVIDENCE},
    **{p: _consent.STATE_KEY_BROWSER for p in POINTS_NEEDING_BROWSER},
}


#: What each scoped point needs, as ``point -> (keystone reader, the switch's own
#: words)``. ONE table rather than a predicate per scope: every entry is the same
#: three facts, and a second copy of the walk is a second place to forget a scope --
#: which for a gate whose open state sends conversation text means sending a
#: category nobody consented to.
_POINT_SCOPES: dict[str, tuple[str, str]] = {
    **{p: ("consented_tool_args", "tool-call arguments") for p in POINTS_NEEDING_TOOL_ARGS},
    **{
        p: ("consented_compaction", "the conversation and its tool-call inputs")
        for p in POINTS_NEEDING_COMPACTION
    },
    **{
        p: ("consented_memory_text", "the text of recalled memories")
        for p in POINTS_NEEDING_MEMORY_TEXT
    },
    **{
        p: ("consented_nudge_evidence", "evidence from other sessions and third parties")
        for p in POINTS_NEEDING_NUDGE_EVIDENCE
    },
    **{
        p: ("consented_browser", "the outline of the agent's browser tab")
        for p in POINTS_NEEDING_BROWSER
    },
}


def _scope_consented(point: str | None, state: dict) -> bool:
    """Whether *point*'s EXTRA egress category is consented to. Never raises.

    ``True`` for every point that sends nothing beyond what the main switch
    records, so ``skills.select`` is untouched by this and pays nothing for it.

    A missing scope is said out loud ONCE per point, at WARNING, for the reason the
    endpoint mismatch beside it is: an owner who consented before this scope existed
    sees the feature do nothing, and "you consented to sending, but not to sending
    this" is the one fact that tells that apart from a broken build.
    """
    # Bound to a local ``str`` so the warning set below is keyed by a name rather
    # than by an optional: ``""`` is not a member of the table, so a caller with no
    # point of its own takes the first return.
    name = point or ""
    scope = _POINT_SCOPES.get(name)
    if scope is None:
        return True
    reader_name, category = scope
    try:
        if bool(getattr(_consent, reader_name)(state)):
            return True
    except Exception:
        # An unreadable scope is an unconsented scope: this decides whether a new
        # category of conversation content leaves the machine.
        logger.debug("decisions: %s scope unreadable; refusing %s", category, name)
        return False
    if name not in _unscoped_warned:
        _unscoped_warned.add(name)
        logger.warning(
            "decisions: %s needs consent to send %s, which this machine has not "
            "given; turn on that switch in Settings to enable it. Nothing is sent "
            "for this point until then",
            name,
            category,
        )
    return False


#: Scopes a caller may require for ONE request on top of its point's own
#: (``decide(scopes=...)``), keyed by the keystone field the consent card writes,
#: as ``field -> (keystone reader, the switch's own words)``. For a point whose
#: requests mostly send what the main switch covers but sometimes carry a wider
#: category: ``model.route`` classifies an owner-typed message on a chat turn and
#: an agent-written tool argument on a sub-agent start.
REQUEST_SCOPES: dict[str, tuple[str, str]] = {
    _consent.STATE_KEY_TOOL_ARGS: ("consented_tool_args", "tool-call arguments"),
    _consent.STATE_KEY_BROWSER_IMAGES: (
        "consented_browser_images",
        "screenshots of the agent's browser tab",
    ),
}


def _request_scopes_consented(scopes: Sequence[str], state: dict) -> bool:
    """Whether every scope in *scopes* is consented in *state*. Never raises.

    A scope this table does not know refuses, and so does an unreadable one: each
    decides whether a category of content leaves the machine.
    """
    for scope in scopes:
        entry = REQUEST_SCOPES.get(scope)
        if entry is None:
            logger.debug("decisions: unknown request scope %r; refusing", scope)
            return False
        reader_name, category = entry
        try:
            if not bool(getattr(_consent, reader_name)(state)):
                logger.debug("decisions: request needs consent to send %s; refusing", category)
                return False
        except Exception:
            logger.debug("decisions: %s scope unreadable; refusing", category)
            return False
    return True


def in_bucket(session_key: str | None, bucket: object) -> bool:
    """Whether *session_key* falls inside a *bucket*-percent sample.

    The digest is the SAME one the log's ``session`` field carries, so a row is
    enough to re-derive why it was sampled without keeping the key.

    ``0`` admits nothing and ``100`` admits everything, both as closed forms. An
    out-of-range or unreadable value is clamped rather than rejected: consent
    is the switch, and a typo'd bucket must not become a second, undocumented way
    to disable the seam.
    """
    try:
        wanted = int(bucket)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        wanted = _BUCKET_MOD
    wanted = max(0, min(_BUCKET_MOD, wanted))
    digest = sha256((session_key or "").encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") % _BUCKET_MOD < wanted


def timeout_secs(config: Any | None = None, *, lane: str = LANE_ENDPOINT) -> float:
    """The budget one ``decide`` call is bounded by, in seconds. Never raises.

    Public so a caller that schedules ``decide`` as a task uses the SAME number for
    its outer wait. Always finite and positive, so a bad ``timeout_ms`` cannot become
    a value ``wait_for`` rejects or a deadline that never expires.

    A *lane* whose registry entry carries a ``timeout_window`` is clamped into it
    (the LLM lane, ``_LLM_TIMEOUT_MIN_SECS.._LLM_TIMEOUT_MAX_SECS``): one model call
    cannot finish inside a budget sized for a ~100 ms System One call.
    """
    try:
        provider = getattr(_decisions_config(config), "provider", None)
        ms = float(getattr(provider, "timeout_ms", _DEFAULT_TIMEOUT_MS))
    except Exception:
        ms = _DEFAULT_TIMEOUT_MS
    if not math.isfinite(ms):
        ms = _DEFAULT_TIMEOUT_MS
    secs = max(_MIN_TIMEOUT_SECS, ms / 1000.0)
    window = _provider.timeout_window(lane)
    if window is not None:
        low, high = window
        return min(high, max(low, secs))
    return secs


def _judge_config(config: Any | None) -> Any | None:
    """The ``decisions.nudge_wake`` section of *config*, or of the live snapshot.

    Read through ``getattr`` rather than by attribute access so a config object
    that predates the section -- an older snapshot, a test double, a hand-trimmed
    file -- reads as absent instead of raising. Absent resolves to every default.
    """
    return getattr(_decisions_config(config), "nudge_wake", None)


def judge_lane(config: Any | None = None, *, endpoint_consented: bool) -> str:
    """Which lane :data:`JUDGE_POINT` would use: :data:`LANE_ENDPOINT` or :data:`LANE_LLM`.

    ``decisions.nudge_wake.provider`` decides; ``auto`` (the default) means the
    endpoint when it is ARMED for this point, the small model otherwise.
    *endpoint_consented* is that arming, resolved by the caller: consent for the
    configured endpoint AND this point's own scope.

    An explicit ``jev`` (the endpoint lane's stored value) is honoured even with no
    consent, and the caller then refuses: answering from a model other than the one
    the owner named would be worse than doing nothing.

    Never raises: an unreadable section resolves the same way an absent one does.
    """
    try:
        provider = str(getattr(_judge_config(config), "provider", "") or "").strip().lower()
    except Exception:
        provider = ""
    if provider == LANE_ENDPOINT:
        return LANE_ENDPOINT
    if provider == LANE_LLM:
        return LANE_LLM
    return LANE_ENDPOINT if endpoint_consented else LANE_LLM


def lane_model(config: Any | None = None, *, lane: str) -> str:
    """The model id *lane* would name, for the scrub to bound before anything is sent.

    Both lanes put an agent-writable id somewhere it must not reach unbounded (the
    wire, or the session layer's model selection), so both go through the
    ``ERROR_SCRUBBED_MODEL`` refusal in :func:`scrub_reason`.

    :data:`LANE_ENDPOINT` gives ``provider.model`` with the shipped fallback.
    :data:`LANE_LLM` resolves an empty ``llm_model`` to ``impl_llm.JUDGE_MODEL_DEFAULT``
    ("inherit": the runner passes no model), the same word the picker shows.
    """
    if lane == LANE_LLM:
        from kiro_crew.decisions.impl_llm import JUDGE_MODEL_DEFAULT

        try:
            configured = str(getattr(_judge_config(config), "llm_model", "") or "").strip()
        except Exception:
            configured = ""
        return configured or JUDGE_MODEL_DEFAULT
    provider = getattr(_decisions_config(config), "provider", None)
    return str(getattr(provider, "model", "") or _DEFAULT_MODEL)


def _oracle(lane: str, provider: Any, model: str = "") -> _provider.DecisionProvider:
    """A fresh provider for one decision on *lane*, chosen by the registry.

    ``decisions.provider.select`` picks the registered spec; its factory imports the
    implementation at call time so neither ``httpx`` nor the session layer lands on
    the hot paths' import graph.

    *model* is the id :func:`lane_model` resolved (the one the scrub bounded and the
    log records); the LLM lane asks for exactly it.
    """
    endpoint = _consent.normalize_endpoint(getattr(provider, "endpoint", ""))
    spec = _provider.select(
        lane, endpoint=endpoint, model=str(getattr(provider, "model", "") or "")
    )
    return spec.build(provider, model)


def _point_scope_granted(point: str, state: dict) -> bool:
    """Whether *point*'s OWN evidence scope is recorded on the keystone. Fail-closed.

    Deliberately NOT :func:`_scope_consented`, which answers ``True`` for a point with
    no registered scope. Here no scope means nothing authorized the endpoint to receive
    this point's transcripts, review comments and ledger events, so the answer is
    ``False``. Reads the same ``_POINT_SCOPES`` table as the enforcement path.
    """
    scope = _POINT_SCOPES.get(point)
    if scope is None:
        return False
    reader_name, category = scope
    try:
        return bool(getattr(_consent, reader_name)(state))
    except Exception:
        logger.debug("decisions: %s scope unreadable; refusing %s", category, point)
        return False


def _judge_authority(
    config: Any | None, session_key: str | None, *, endpoint_consented: bool
) -> tuple[str, bool]:
    """``(lane, authorized)`` for :data:`JUDGE_POINT`. Filesystem IO on this thread.

    The two lanes are authorized by different things, because they send to different
    places:

    * The endpoint lane sends conversation state to a paid third party, so it needs
      the keystone in full: consent for the endpoint (``endpoint_consented``, read by
      the caller off the event loop, fleet ceiling included) AND the point's own
      ``nudge_evidence`` scope, read here fail-closed.
    * The LLM lane adds no destination: its evidence goes to the provider the owner's
      sessions already send to. The judge only chooses QUIET against firing and is
      fail-open, so the worst case is one delayed wake. ``provider = llm`` plus a
      ``judge`` spec on the loop is the whole authorization (RFC ``rfc-wake-judge``).

    ``auto`` resolves against the endpoint ARMED, not merely consented, so a missing
    scope gives the small model instead of a refusal; a pinned ``jev`` still refuses.
    The FLEET ceiling (``capabilities.decisions``) binds both lanes, so the LLM lane
    pays the governed probe itself.

    Fail-closed on anything unreadable: ``(LANE_ENDPOINT, False)``, a tick that fires
    exactly as the ungated timer would.
    """
    try:
        endpoint_armed = endpoint_consented and _point_scope_granted(
            JUDGE_POINT, _consent.load_state()
        )
        lane = judge_lane(config, endpoint_consented=endpoint_armed)
        if lane == LANE_ENDPOINT:
            return LANE_ENDPOINT, endpoint_armed
        return LANE_LLM, not _capability_denied(session_key)
    except Exception as exc:
        logger.debug("decisions: judge authority unreadable (%s)", type(exc).__name__)
        return LANE_ENDPOINT, False


def judge_evidence_scope_granted(
    *, session_key: str | None = None, config: Any | None = None
) -> bool:
    """Whether the owner granted :data:`JUDGE_POINT`'s OWN egress scope. Never raises.

    Narrower than :func:`is_enabled`, which the LLM lane satisfies on the provider key
    alone. A loop whose owner armed no brief lacks the ``judge`` spec half of that
    lane's authorization, so screening it asks whether this point's own egress scope
    was granted. Fail-closed: anything unreadable answers False.
    """
    try:
        cfg = config if config is not None else _snapshot()
        if cfg is None:
            return False
        if not _consented_for(cfg, session_key, JUDGE_POINT):
            return False
        return _point_scope_granted(JUDGE_POINT, _consent.load_state())
    except Exception as exc:
        logger.debug("decisions: judge scope unreadable (%s)", type(exc).__name__)
        return False


def history_budget_chars(config: Any | None = None) -> int:
    """Characters of prior conversation this decision may carry. Never raises.

    The SMALLER of what ``config.json`` asks for (a preference, agent-writable) and
    what the keystone recorded the owner reviewing (the authorization, sealed
    read-only in every sandbox). Raising the budget past the reviewed ceiling takes
    a new consent; an agent rewriting ``config.json`` cannot.

    0 whenever either side is unreadable, including a consent with no recorded
    ceiling: that owner reviewed a request with no prior turns.

    The keystone read is filesystem IO on the caller's thread.
    """
    asked = _budget("history_budget_chars", DECISION_HISTORY_BUDGET_DEFAULT, config)
    if asked <= 0:
        return 0
    try:
        ceiling = _consent.consented_history_budget()
    except Exception:
        logger.debug("decisions: history ceiling unreadable; sending no prior turns")
        return 0
    return min(asked, ceiling)


def model_route_map(config: Any | None = None) -> dict[str, str]:
    """``decisions.model_route`` as a ``{tier: model_id}`` mapping. Never raises.

    ``""`` is KEPT for a tier, because it is the shipped value and it means
    "inherit -- leave this turn's model alone", which the log and the strip report
    rather than treat as absence. Only a non-string is dropped.

    Returns ``{}`` for an absent or unreadable section. Every tier then reads as
    unpinned, which applies nothing -- the fail-closed direction for a value that
    decides what a turn costs.
    """
    try:
        raw = getattr(_decisions_config(config), "model_route", None)
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        tier: model.strip()
        for tier, model in raw.items()
        if isinstance(tier, str) and isinstance(model, str)
    }


def model_route_derive(config: Any | None = None) -> bool:
    """``decisions.model_route_derive``: whether empty tiers are derived. Never raises.

    ``False`` for an absent, unreadable or non-bool value, so every failure keeps
    the shipped behaviour where an unpinned tier applies nothing.
    """
    try:
        raw = getattr(_decisions_config(config), "model_route_derive", False)
    except Exception:
        return False
    return raw is True


def _budget(name: str, default: int, config: Any | None = None) -> int:
    """One bounded integer knob off the decisions section, or *default*."""
    try:
        value = int(getattr(_decisions_config(config), name, default))
    except Exception:
        return default
    return max(0, value)


def _scan_text(state: dict | str, questions: list[Question], model: str = "") -> str:
    """Everything the scrub must clear, as one string.

    The questions and the provider model id are scanned alongside the state
    because they leave the machine in the same request: "the state was clean" says
    nothing about the rubric sent with it, and ``provider.model`` comes from the
    agent-writable config. A ``dict`` is rendered with ``json.dumps`` rather than
    ``str()``, which can elide content behind a ``__repr__`` -- rendering the way
    the wire will is what makes the scan see what the wire sees.
    """
    if isinstance(state, str):
        rendered = state
    else:
        import json

        try:
            rendered = json.dumps(state, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            rendered = repr(state)
    parts = [rendered, model]
    for question in questions:
        parts.extend(question_texts(question))
    return "\n".join(parts)


def scrub_reason(
    state: dict | str,
    questions: list[Question],
    *,
    model: str = _DEFAULT_MODEL,
    unclipped: str = "",
) -> str | None:
    """A :data:`SCRUB_ERRORS` category, or ``None`` when the request may be sent.

    The model id (agent-writable config) is bounded to an identifier first. Then the
    local regex, then both canonical scanners over state, questions and model; any
    warning, or a scanner that fails, refuses the whole request. The reason is a
    category, never the matched text, which would put the credential into the log.

    *unclipped* is scanned but never sent: the text a point clipped to build *state*,
    since a credential cut in half matches no pattern yet would still go on the wire.
    Text longer than :data:`MAX_UNCLIPPED_SCAN_CHARS` refuses unread.
    """
    if not is_model_id(model):
        return ERROR_SCRUBBED_MODEL
    if len(unclipped) > MAX_UNCLIPPED_SCAN_CHARS:
        return ERROR_SCRUBBED_SCAN_BOUND
    text = _scan_text(state, questions, model) + "\n" + unclipped
    if _CREDENTIAL_RE.search(text) is not None:
        return ERROR_SCRUBBED_CREDENTIAL
    try:
        from kiro_crew.security.redaction import redact_credentials

        _cleaned, warnings = redact_credentials(text)
    except Exception:
        logger.warning("decisions: credential scan failed; refusing the state")
        return ERROR_SCRUBBED_SCAN_FAILED
    if warnings:
        return ERROR_SCRUBBED_CREDENTIAL
    try:
        from kiro_crew.security import redact_exfiltration_urls

        _cleaned, warnings = redact_exfiltration_urls(text)
    except Exception:
        logger.warning("decisions: exfiltration URL scan failed; refusing the state")
        return ERROR_SCRUBBED_SCAN_FAILED
    return ERROR_SCRUBBED_URL if warnings else None


def _request_needs(
    questions: Sequence[Question], images: Mapping[str, ImageAsset] | None = None
) -> frozenset[str]:
    """What a request needs from its provider beyond the text question types:
    ``image`` when it carries an image, ``box`` when it asks a Box question."""
    needs: set[str] = set()
    if images:
        needs.add(_provider.CAPABILITY_IMAGE)
    if any(isinstance(q, Box) for q in questions):
        needs.add(_provider.KIND_BOX)
    return frozenset(needs)


def provider_supports(needs: frozenset[str], config: Any | None = None) -> bool:
    """Whether the endpoint lane's configured provider declares every one of *needs*.

    Pure: the registry selection reads the config snapshot and the preset catalog,
    no file and no network. Unreadable reads as unsupported, so an image is never
    sent to a provider nobody established can read one.
    """
    if not needs:
        return True
    try:
        provider = getattr(_decisions_config(config), "provider", None)
        spec = _provider.select(
            LANE_ENDPOINT,
            endpoint=_consent.normalize_endpoint(getattr(provider, "endpoint", "")),
            model=str(getattr(provider, "model", "") or ""),
        )
    except Exception:
        return False
    return needs <= spec.supports


def capabilities(config: Any | None = None) -> _provider.Capabilities | None:
    """What the endpoint lane's configured provider declares, or ``None``. Pure.

    A caller that adapts its pace or its inputs (the browser's ``reflex`` loop)
    reads this instead of a provider's name. Grants nothing: every request still
    goes through :func:`decide`, which re-checks support, consent and scopes.
    """
    try:
        cfg = config if config is not None else _snapshot()
        provider = getattr(_decisions_config(cfg), "provider", None)
        spec = _provider.select(
            LANE_ENDPOINT,
            endpoint=_consent.normalize_endpoint(getattr(provider, "endpoint", "")),
            model=str(getattr(provider, "model", "") or ""),
        )
    except Exception:
        return None
    return spec.capabilities()


async def warm(point: str, *, session_key: str | None = None) -> bool:
    """Open the provider's connection before a burst of decisions. Never raises.

    Only where :func:`decide` would send (the same consent, governance and bucket
    checks), and only to a provider whose client offers ``warm()``; it carries no
    state and no question. ``False`` when nothing was opened.
    """
    try:
        cfg = _snapshot()
        if cfg is None:
            return False
        consented = await asyncio.to_thread(_consented_for, cfg, session_key, point)
        if point == JUDGE_POINT or not _sampled(point, session_key, cfg, consented=consented):
            return False
        provider = getattr(_decisions_config(cfg), "provider", None)
        model = lane_model(cfg, lane=LANE_ENDPOINT)
        # The fleet's lists bind the HEAD too: a warm-up is a request to the endpoint.
        if await asyncio.to_thread(_targets_denied, cfg, session_key, point, model, LANE_ENDPOINT):
            return False
        oracle = _oracle(LANE_ENDPOINT, provider, model)
        opener = getattr(oracle, "warm", None)
        if opener is None:
            return False
        await asyncio.wait_for(opener(), timeout=timeout_secs(cfg) + 1.0)
        return True
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.debug("decisions: warm(%s) failed (%s)", point, type(exc).__name__)
        return False


def request_scope_granted(scope: str) -> bool:
    """Whether the keystone records *scope* (:data:`REQUEST_SCOPES`). Filesystem IO.

    Only the scope: consent itself, the endpoint and governance are
    :func:`is_enabled`'s question. Unknown or unreadable reads False.
    """
    try:
        return _request_scopes_consented((scope,), _consent.load_state())
    except Exception:
        return False


def _sampled(point: str, session_key: str | None, config: Any | None, *, consented: bool) -> bool:
    """Refusals 1-3, shared by :func:`is_enabled` and :func:`decide` so the two
    cannot drift. No ``await``, no IO, no import of an implementation: the one
    read with IO -- the keystone -- is done by the caller and passed in as
    *consented*, so each caller can do it off the event loop in its own way."""
    if consented is not True:
        return False
    decisions = _decisions_config(config)
    if decisions is None:
        return False
    if point not in DECISION_POINT_NAMES:
        # WARNING, not debug: the caller is code in this repo, so an unknown name
        # is a typo in a point file rather than an operator's config.
        logger.warning("decisions: unknown point %r (known: %s)", point, DECISION_POINT_NAMES)
        return False
    return in_bucket(session_key, getattr(decisions, "bucket", _BUCKET_MOD))


def is_enabled(point: str, *, session_key: str | None = None, config: Any | None = None) -> bool:
    """Whether *point* would get past refusals 1-3 right now. Never raises.

    For a hook whose STATE is expensive to build -- walking the skill tree, reading
    frontmatter -- which would otherwise do that work on the default configuration
    and hand it to a ``decide`` that refuses on its first line. It is not a second
    gate and grants nothing: ``decide`` re-runs every refusal, so skipping it is
    merely wasteful and racing a config change costs one row.
    """
    try:
        # ONE snapshot for the whole check, so the endpoint the keystone is held
        # against and the bucket read below cannot come from two different
        # config generations. Synchronous keystone read: this runs on the
        # caller's thread, which in production is an executor worker (see
        # points/skills_select.py), never the event loop.
        cfg = config if config is not None else _snapshot()
        if cfg is None:
            return False
        consented = _consented_for(cfg, session_key, point)
        # The judge is the one point whose authority is not simply the keystone:
        # its LLM lane runs on the provider key alone. Resolved here as well as in
        # ``decide`` so a hook that skips expensive state building on a False reads
        # the same answer the call would give.
        authorized = (
            _judge_authority(cfg, session_key, endpoint_consented=consented)[1]
            if point == JUDGE_POINT
            else consented
        )
        return _sampled(point, session_key, cfg, consented=authorized)
    except Exception as exc:
        logger.debug("decisions: is_enabled(%s) failed (%s)", point, type(exc).__name__)
        return False


async def decide(
    point: str,
    state: dict | str,
    questions: list[Question],
    *,
    session_key: str | None = None,
    config: Any | None = None,
    extra: dict[str, Any] | None = None,
    receipt: dict[str, Any] | None = None,
    scopes: Sequence[str] = (),
    images: Mapping[str, ImageAsset] | None = None,
    max_wait_s: float | None = None,
    unclipped: str = "",
) -> Answers | None:
    """Ask *questions* about *state* at *point*, or return ``None``.

    ``None`` is the ONLY failure signal and it is never exceptional: every refusal,
    every provider error, every timeout and an unreadable config all return it. A
    caller therefore needs no try/except and no enable check of its own --
    ``answers = await decide(...)`` then ``if answers is None: <existing
    behaviour>`` is the complete integration.

    ``asyncio.CancelledError`` is the one exception that propagates: cancellation
    is the caller going away, not a decision failure, and swallowing it would break
    structured concurrency.

    *config* injects a config instead of reading the live snapshot; production
    callers leave it unset.

    *extra* is written onto whichever row this call produces (a split menu's round,
    a shared turn id) and is never sent, so ``_scan_text`` skips it; a key naming a
    core row field is dropped by the log. A refusal that writes no row (no consent,
    unknown point, unsampled) writes no extra either.

    *receipt* reports whether this attempted decision is on record: ``row_written``
    starts ``False``, and once an append starts it resolves to true on commitment or
    false on a definitive refusal, even if the retention sweep outlives the write
    budget. If neither arrives within the bounded grace it stays ``None`` and the
    gate logs one warning naming the point, the row and the time waited. Without a
    receipt the call returns when the write budget expires and pays no grace.

    *scopes* names keystone scopes THIS request needs beyond its point's own
    (:data:`REQUEST_SCOPES`). They are checked against the same keystone read that
    authorizes the endpoint, so a consent change between a caller's own check and
    the send cannot let the request through under the wider category; a missing,
    unknown or unreadable scope refuses with no row.

    *images* are pictures the state names by asset id. They are sent only when
    *scopes* names one of :data:`IMAGE_SCOPES` -- an image cannot be scrubbed, so it
    needs a grant of its own -- and only to a provider whose registry entry
    declares ``image`` (and ``box`` for a Box question). A request that needs
    either from a provider that does not declare it, or a Box question with no
    image, refuses with no row and no IO.

    *max_wait_s* narrows the provider budget for this one call; it never widens it.

    *unclipped* is the text a point clipped to build *state*, usually the new
    message. It is scanned by :func:`scrub_reason` and never sent, so a credential
    the clip halved still refuses the request.
    """
    if receipt is not None:
        receipt["row_written"] = False
    needs = _request_needs(questions, images)
    if _provider.KIND_BOX in needs and not images:
        return None
    if images and not (
        IMAGE_SCOPES.intersection(scopes)
        and all(isinstance(asset, ImageAsset) for asset in images.values())
    ):
        logger.debug("decisions: %s images refused: no image scope named", point)
        return None

    # Guarded because *config* may be an arbitrary object whose attribute reads
    # raise, and this seam must never alter the turn it sits in.
    try:
        # ONE snapshot, resolved before the first await and used for every read
        # below: the endpoint consent is checked against, the bucket, the budget
        # and the provider the request is sent to. Reading the live snapshot
        # twice would let a config swap during the keystone await pass consent on
        # the old endpoint and then send to the new one.
        cfg = config if config is not None else _snapshot()
        if cfg is None:
            # An unprimed watcher fails CLOSED here rather than letting a later
            # helper re-read a snapshot that may have appeared in the meantime.
            return None
        if not provider_supports(needs, cfg):
            return None
        # The keystone is a file read, so it leaves the event loop; everything
        # else `_sampled` checks is attribute reads on the snapshot.
        consented = await asyncio.to_thread(_consented_for, cfg, session_key, point, scopes)
        # Which provider answers, and on whose authority. Every point but the judge
        # has exactly one lane and exactly one authority -- the keystone -- so this
        # is inert for all of them. The judge's LLM lane runs on its provider key
        # alone, because it adds no destination: the model provider the session
        # already sends to. ``_judge_authority`` is where that reasoning lives.
        # Off the loop for the same reason the keystone read is: it may run the
        # governed capability probe, which reads from disk.
        lane, authorized = LANE_ENDPOINT, consented
        if point == JUDGE_POINT:
            lane, authorized = await asyncio.to_thread(
                _judge_authority, cfg, session_key, endpoint_consented=consented
            )
        if not _sampled(point, session_key, cfg, consented=authorized):
            return None
        budget = timeout_secs(cfg, lane=lane)
        if max_wait_s is not None and math.isfinite(max_wait_s) and max_wait_s > 0:
            budget = min(budget, max_wait_s)
        provider = getattr(_decisions_config(cfg), "provider", None)
    except Exception as exc:
        logger.debug("decisions: %s config read failed (%s)", point, type(exc).__name__)
        return None

    async def _write(
        *,
        latency_ms: int,
        answers: Answers | None,
        error: str | None,
        meta: dict[str, Any] | None = None,
    ) -> None:
        # Guarded here as well as inside ``log.append``: ``append`` protects the
        # WRITE, this protects BUILDING the row, which renders values an
        # implementation supplied. The class only, never a message, for the same
        # reason.
        row_written: bool | None = False
        try:
            # Provider-stamped metadata (model, usage, request id) is merged
            # UNDER the caller's extra, so a point's own field always wins a name
            # collision and the metadata only ever adds fields. Both are bounded
            # again by the log's own ``extra`` handling.
            row_extra = extra
            if meta:
                row_extra = {**meta, **(extra or {})}
            row = _log.build_row(
                point=point,
                session_key=session_key,
                latency_ms=latency_ms,
                answers=answers,
                scrubbed=error in SCRUB_ERRORS,
                error=error,
                extra=row_extra,
            )
            # A caller that asked for no receipt observes nothing about commitment, so
            # it gets the bare call this seam has always made: no event to set, no
            # keyword to accept, and no grace to pay. The receipt path is the only one
            # that needs a commit signal, so it is the only one that creates it.
            commit_event = threading.Event() if receipt is not None else None
            row_written = None
            append_started = time.monotonic()
            append_task = asyncio.create_task(
                asyncio.to_thread(_log.append, row)
                if commit_event is None
                else asyncio.to_thread(_log.append, row, commit_event=commit_event)
            )
            try:
                row_written = await asyncio.wait_for(asyncio.shield(append_task), _LOG_BUDGET_SECS)
            except asyncio.TimeoutError:
                if commit_event is None:
                    # Nothing reads a receipt here, so the write budget is the end of
                    # what this call waits for.
                    return
                commit_wait = asyncio.create_task(
                    asyncio.to_thread(commit_event.wait, _LOG_COMMIT_GRACE_SECS)
                )
                await asyncio.wait(
                    (append_task, commit_wait),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if commit_event.is_set():
                    row_written = True
                elif append_task.done():
                    row_written = append_task.result() is True
                else:
                    # Neither outcome arrived, so the receipt stays unknown and a
                    # caller that scores only a confirmed row (the wake judge's join
                    # key) loses this one. The worker may still commit it, so the
                    # receipt is not turned into a refusal: the loss is logged.
                    logger.warning(
                        "decisions: %s row %s commit unconfirmed after %.3fs; receipt left unknown",
                        point,
                        row.get("verdict_id") or row.get("ts"),
                        time.monotonic() - append_started,
                    )
                if not commit_wait.done():
                    commit_wait.cancel()
        except Exception as exc:
            row_written = False
            logger.warning("decisions: could not record %s row (%s)", point, type(exc).__name__)
        finally:
            if receipt is not None:
                receipt["row_written"] = row_written

    # The model id the SELECTED lane will name, so the scanned id IS the sent id.
    # For every lane but the judge's LLM one this is ``provider.model`` with the
    # same fallback the System One client applies, unchanged.
    model = lane_model(cfg, lane=lane)
    # The scrub runs first: the list check below writes the model id to the SEL,
    # so an id the scrub refuses (a pasted credential) never reaches that row.
    refusal = scrub_reason(state, questions, model=model, unclipped=unclipped)
    if refusal is not None:
        await _write(latency_ms=0, answers=None, error=refusal)
        return None
    # The fleet's lists: which points, models and endpoints the seam may use. Like
    # the capability row in ``_consented_for``, a refusal here writes no decision
    # row (its audit is the SEL ``governance_decision`` row) and costs nothing on
    # an unconsented install, which never reaches this line.
    if await asyncio.to_thread(_targets_denied, cfg, session_key, point, model, lane):
        return None

    started = time.monotonic()
    oracle: Any = None
    # The LLM lane is a model call that fails fast (~3 s) and often enough to cost a
    # full turn each time, so it gets ONE retry inside the same budget. A second
    # failure still lands on ERROR_PROVIDER, and the caller fires as before.
    attempts = 2 if lane == LANE_LLM else 1

    async def _ask() -> Answers:
        nonlocal oracle
        for attempt in range(1, attempts + 1):
            try:
                # Constructed inside the guard so a lane import or constructor failure
                # is the same never-raise ERROR_PROVIDER as a call failure, not an
                # exception out of ``decide``. Bound to a name so its response
                # metadata can be read after a successful call.
                oracle = _oracle(lane, provider, model)
                # Assets only to a provider that declared images (checked above), so
                # every other provider keeps the two-argument call.
                if images:
                    return await oracle.ask(state, questions, assets=images)
                return await oracle.ask(state, questions)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if attempt >= attempts:
                    raise
                logger.info(
                    "decisions: %s provider call failed (%s); retrying once",
                    point,
                    type(exc).__name__,
                )
        raise AssertionError("unreachable")  # pragma: no cover

    try:
        answers = await asyncio.wait_for(_ask(), timeout=budget)
    except asyncio.TimeoutError:
        # Named apart from the generic branch: "timeout" is the one failure an
        # operator can act on mechanically (raise timeout_ms, or accept the rate).
        await _write(latency_ms=_elapsed_ms(started), answers=None, error=ERROR_TIMEOUT)
        return None
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        # The class, and no traceback: a provider message -- and the locals a
        # traceback renders -- can quote the request back. The class still
        # separates a transport failure from a protocol one. INFO, so the cause of
        # a provider error is readable in the gateway log without DEBUG.
        logger.info("decisions: %s provider call failed (%s)", point, type(exc).__name__)
        await _write(latency_ms=_elapsed_ms(started), answers=None, error=ERROR_PROVIDER)
        return None

    latency_ms = _elapsed_ms(started)
    if not answers or not _answers_are_valid(answers, questions):
        # An implementation returning an empty or out-of-domain mapping broke its
        # contract (raise, never return either), so it is recorded as an
        # error rather than as an answer.
        await _write(latency_ms=latency_ms, answers=None, error=ERROR_INVALID_RESULT)
        return None

    # Written with the answers in hand, outside ``budget`` and under
    # ``_LOG_BUDGET_SECS``: the write cannot spend the provider deadline, cannot
    # hold the caller longer than that budget, and cannot cost it the result.
    # The lane's provider-stamped metadata (model, usage, request id) rides onto
    # this row only -- an error row never carries it, because an oracle sets it
    # only on a fully-valid response. A lane that stamps none (the LLM judge)
    # leaves the attribute unset and the row simply has no metadata.
    meta = getattr(oracle, "last_response_meta", None)
    await _write(latency_ms=latency_ms, answers=answers, error=None, meta=meta)
    return answers


def _elapsed_ms(started: float) -> int:
    """Whole milliseconds since *started* (a ``time.monotonic()`` reading)."""
    return int((time.monotonic() - started) * 1000)
