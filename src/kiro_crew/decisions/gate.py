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
:data:`_LOG_BUDGET_SECS` on top of it. Nothing this module logs carries a provider
message or a traceback -- a row's ``error`` is one of the identifiers below and the
application log gets the exception CLASS only, because both artifacts are readable
and a provider can quote the request back.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from hashlib import sha256
from typing import Any

from kiro_crew import credential_patterns as _cred
from kiro_crew.config.sections import (
    DECISION_HISTORY_BUDGET_DEFAULT,
    DECISION_PROVIDER_ENDPOINT_DEFAULT,
    DECISION_PROVIDER_MODEL_DEFAULT,
)
from kiro_crew.decisions import consent as _consent
from kiro_crew.decisions import log as _log
from kiro_crew.decisions.types import Answer, Answers, Question, is_model_id

logger = logging.getLogger(__name__)

#: Decision points this build ships; an absent name is refused. Lives with the
#: seam, not in ``config.sections``: nothing in the config is keyed by point name
#: -- ``decisions.model_route`` is keyed by TIER, not by point -- and keeping the
#: tuple here keeps the config loader off a hot path's import graph.
DECISION_POINT_NAMES = ("skills.select", "tool.risk", "message.steer", "model.route")

#: Points whose request carries TOOL-CALL ARGUMENTS, and which therefore need the
#: keystone's ``tool_args`` scope on top of consent itself
#: (``consent.consented_tool_args``). A point is in here because of what it SENDS,
#: not what it decides: consent is recorded against the text the owner reviewed, so
#: a record written before that scope existed authorizes the message excerpt and the
#: candidate descriptions and nothing wider. Absent scope refuses the point
#: outright, which is what makes an already-consented install inert for it rather
#: than retroactively signed up.
POINTS_NEEDING_TOOL_ARGS = frozenset({"tool.risk"})

#: The model id sent when the config leaves ``provider.model`` empty -- the same
#: fallback ``impl_jev`` applies, so the id the scrub clears is the id sent.
_DEFAULT_MODEL = DECISION_PROVIDER_MODEL_DEFAULT

#: Bucket modulus and upper clamp, matching the config's declared 0..100 bounds.
_BUCKET_MOD = 100

#: Provider budget used when the config carries no usable ``timeout_ms``.
_DEFAULT_TIMEOUT_MS = 1000.0
_MIN_TIMEOUT_SECS = 0.001

#: How long the row write may hold the caller, on top of the provider budget. The
#: write is one ``O_APPEND`` of a few hundred bytes, so this exists only so a
#: stalled filesystem cannot make an observation cost the turn. On expiry the
#: awaiting side gives up and ``decide`` returns; the worker thread is NOT
#: cancellable, so the append may still land afterwards -- acceptable for a write
#: that cannot corrupt a line. An outer wait therefore needs ``timeout_secs()``
#: plus this.
_LOG_BUDGET_SECS = 0.05

#: A row's ``error`` is one of these -- an identifier an operator can act on, never
#: a provider message, which is unbounded and can quote the request back.
ERROR_TIMEOUT = "timeout"
ERROR_PROVIDER = "provider"
ERROR_INVALID_RESULT = "invalid-result"
ERROR_SCRUBBED_CREDENTIAL = "scrubbed:credential"
ERROR_SCRUBBED_URL = "scrubbed:exfiltration-url"
ERROR_SCRUBBED_SCAN_FAILED = "scrubbed:scan-failed"
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
    ERROR_SCRUBBED_MODEL,
)

#: Credential spellings refused locally, before the canonical scanner. Compiled
#: here because ``credential_patterns`` exports pattern SOURCE strings. This is the
#: scrubber-side AWS spelling, not the wider redaction one, and not the whole of
#: the scrub: ``VENDOR_TOKEN_PATTERNS`` carries a generic ``sk-`` form that
#: ``redact_credentials`` does not, which is why both run.
_CREDENTIAL_RE = re.compile(
    "|".join(
        [_cred.AWS_KEY_ID, _cred.JWT_MULTI_SEGMENT]
        + [frag for _label, frag in _cred.VENDOR_TOKEN_PATTERNS]
    )
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
        if not isinstance(answer.value, str) or answer.value not in question.options:
            return False
    return True


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

    The same fallback ``impl_jev`` applies, spelled here so the consent check and
    the send cannot disagree about where "" points.
    """
    decisions = _decisions_config(config)
    provider = getattr(decisions, "provider", None)
    raw = _consent.normalize_endpoint(getattr(provider, "endpoint", ""))
    return raw or DECISION_PROVIDER_ENDPOINT_DEFAULT


#: Endpoints already warned about, so a mismatch is said once, not once per message.
_unconsented_warned: set[str] = set()

#: Whether the governance denial has been said out loud yet, so a pinned-off fleet
#: gets one line rather than one per message.
_capability_denied_warned = False


def _capability_denied(session_key: str | None) -> bool:
    """Whether the ``capabilities.decisions`` ceiling withdraws the seam. Filesystem IO.

    *session_key* is the turn's own identity, which is what a profile binds on, so a
    profile bound to THIS surface is consulted rather than a dashboard one. It is
    runtime state, not caller input -- unlike the ``X-Session-Key`` header the two
    dashboard callers deliberately ignore. A caller with no session at all names no
    surface, so the probe's own default applies; there is nothing more specific to
    honour, and every layer above the surface still binds.

    Imported HERE rather than at module scope: the probe reaches
    ``platform.governance_profiles``, and this package is imported inside hot
    callers, so that graph is paid only by an install that actually got past
    consent.
    """
    global _capability_denied_warned
    from kiro_crew.decisions.capability import DASHBOARD_SURFACE_KEY, is_decisions_denied

    if not is_decisions_denied(session_key or DASHBOARD_SURFACE_KEY):
        return False
    if not _capability_denied_warned:
        _capability_denied_warned = True
        logger.warning(
            "decisions: the seam is withdrawn by governance "
            "(capabilities.decisions); nothing is sent even though consent is on"
        )
    return True


def _consented_for(
    config: Any | None, session_key: str | None = None, point: str | None = None
) -> bool:
    """Read the keystone and hold it against the configured endpoint. Filesystem IO.

    A mismatch -- consent recorded for one address, config now naming another --
    is a refusal, and it is said out loud once per address: this is the state a
    redirected ``provider.endpoint`` produces, and an operator must be able to tell
    it apart from "off".

    The GOVERNANCE ceiling is held above the keystone, and this is the chokepoint
    that makes an already-consented keystone inert rather than carried over: every
    ``decide`` and ``is_enabled`` path funnels its one keystone read through here,
    so a fleet that pins ``capabilities.decisions`` off does not have to reach any
    other call site. Checked AFTER the keystone, deliberately: an install without
    consent -- the default, and the overwhelming majority -- must keep costing
    nothing, and the governed probe writes an audited SEL row on every evaluation.
    Past this line consent IS on, which is precisely when a fleet denial is a fact
    an auditor needs recorded.

    *point* names the caller's decision point, so a point in
    :data:`POINTS_NEEDING_TOOL_ARGS` can be refused on a keystone that consents to
    sending but not to sending TOOL ARGUMENTS. Checked here rather than in
    :func:`_sampled` because the state this needs is the one read this function
    already did -- ``_sampled`` is deliberately IO-free -- so the scope costs no
    second keystone read, and because this is the documented chokepoint every
    ``decide`` and ``is_enabled`` path funnels through. ``None`` asks for no scope
    and is what a caller with no point of its own gets.
    """
    state = _consent.load_state()
    endpoint = configured_endpoint(config)
    if _consent.permits(endpoint, state):
        if not _tool_args_scoped(point, state):
            return False
        return not _capability_denied(session_key)
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


def _tool_args_scoped(point: str | None, state: dict) -> bool:
    """Whether *point*'s tool-argument egress is consented to. Never raises.

    ``True`` for every point that does not send tool arguments, so
    ``skills.select`` is untouched by this and pays nothing for it.

    A missing scope is said out loud ONCE per point, at WARNING, for the reason the
    endpoint mismatch beside it is: an owner who consented before this scope existed
    sees the feature do nothing, and "you consented to sending, but not to sending
    this" is the one fact that tells that apart from a broken build.
    """
    if point is None or point not in POINTS_NEEDING_TOOL_ARGS:
        return True
    try:
        if _consent.consented_tool_args(state):
            return True
    except Exception:
        # An unreadable scope is an unconsented scope: this decides whether a new
        # category of conversation content leaves the machine.
        logger.debug("decisions: tool-argument scope unreadable; refusing %s", point)
        return False
    if point not in _unscoped_warned:
        _unscoped_warned.add(point)
        logger.warning(
            "decisions: %s needs consent to send tool-call arguments, which this "
            "machine has not given; turn on the tool-argument switch in Settings to "
            "enable it. Nothing is sent for this point until then",
            point,
        )
    return False


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


def timeout_secs(config: Any | None = None) -> float:
    """The budget one ``decide`` call is bounded by, in seconds. Never raises.

    Public because a caller that schedules ``decide`` as a task needs the SAME
    number for its outer wait: a helper waiting for a budget it invented would
    either abandon a call the gate was still going to answer, or wait past the
    deadline the gate already enforces.

    Always finite and positive, so a missing, non-numeric, infinite or
    non-positive ``timeout_ms`` cannot become a value ``wait_for`` rejects or a
    deadline that never expires.
    """
    try:
        provider = getattr(_decisions_config(config), "provider", None)
        ms = float(getattr(provider, "timeout_ms", _DEFAULT_TIMEOUT_MS))
    except Exception:
        ms = _DEFAULT_TIMEOUT_MS
    if not math.isfinite(ms):
        ms = _DEFAULT_TIMEOUT_MS
    return max(_MIN_TIMEOUT_SECS, ms / 1000.0)


def history_budget_chars(config: Any | None = None) -> int:
    """Characters of prior conversation this decision may carry. Never raises.

    The SMALLER of what ``config.json`` asks for and what the keystone recorded the
    owner reviewing. Two files because they answer different questions: the config
    is a preference and is agent-writable, the keystone is the authorization and is
    sealed read-only in every sandbox. So lowering the budget stays an ordinary
    config edit, while raising it past the reviewed ceiling takes a new consent --
    and an agent that rewrites ``config.json`` moves nothing, because the ceiling
    is not there.

    0 whenever either side is unreadable, which includes every consent recorded
    before the ceiling existed: those owners reviewed a request carrying the
    message excerpt and the candidate descriptions, and this is what keeps that
    true for them.

    Read here rather than in the point for the same reason :func:`timeout_secs`
    is: the snapshot read and its fallbacks live with the gate, so a point never
    imports the config loader onto its own hot path. The keystone read is
    filesystem IO on the CALLER's thread, which is the executor worker the point
    already reads consent on.
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

    Read here for the same reason :func:`timeout_secs` and
    :func:`history_budget_chars` are: the snapshot read and its fallbacks live
    with the gate, so a point never imports the config loader onto its own hot
    path.

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
        parts.append(str(getattr(question, "prompt", "") or ""))
        parts.extend(str(option) for option in getattr(question, "options", ()) or ())
    return "\n".join(parts)


def scrub_reason(
    state: dict | str, questions: list[Question], *, model: str = _DEFAULT_MODEL
) -> str | None:
    """A :data:`SCRUB_ERRORS` category, or ``None`` when the request may be sent.

    The model id's shape first: the one field of the request that is neither
    ours nor the conversation's but the agent-writable config's, so it is bounded
    to an identifier before anything is scanned. Then the local regex, then both
    canonical scanners over state, questions and model together; any warning
    refuses the whole request. A scanner that itself fails also refuses -- an
    external request cannot be cleared by a scan that did not complete. The reason
    is a category, never the matched text, which would put the credential into
    the log.
    """
    if not is_model_id(model):
        return ERROR_SCRUBBED_MODEL
    text = _scan_text(state, questions, model)
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
        return _sampled(point, session_key, cfg, consented=_consented_for(cfg, session_key, point))
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

    *extra* is written onto whichever row this call produces -- the round of a
    split menu, a turn id shared by several calls -- and is never sent: it takes
    no part in ``_scan_text`` because it never reaches the provider, and a key
    naming a core row field is dropped by the log. A refusal that writes no row
    (no consent, unknown point, unsampled) writes no extra either, which is the
    same claim as before: those three touch no disk.
    """
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
        # The keystone is a file read, so it leaves the event loop; everything
        # else `_sampled` checks is attribute reads on the snapshot.
        consented = await asyncio.to_thread(_consented_for, cfg, session_key, point)
        if not _sampled(point, session_key, cfg, consented=consented):
            return None
        budget = timeout_secs(cfg)
        provider = getattr(_decisions_config(cfg), "provider", None)
    except Exception as exc:
        logger.debug("decisions: %s config read failed (%s)", point, type(exc).__name__)
        return None

    async def _write(*, latency_ms: int, answers: Answers | None, error: str | None) -> None:
        # Guarded here as well as inside ``log.append``: ``append`` protects the
        # WRITE, this protects BUILDING the row, which renders values an
        # implementation supplied. The class only, never a message, for the same
        # reason.
        try:
            row = _log.build_row(
                point=point,
                session_key=session_key,
                latency_ms=latency_ms,
                answers=answers,
                scrubbed=error in SCRUB_ERRORS,
                error=error,
                extra=extra,
            )
            await asyncio.wait_for(asyncio.to_thread(_log.append, row), _LOG_BUDGET_SECS)
        except Exception as exc:
            logger.warning("decisions: could not record %s row (%s)", point, type(exc).__name__)

    # The same fallback ``JevOracle`` applies, so the scanned id IS the sent id.
    model = str(getattr(provider, "model", "") or _DEFAULT_MODEL)
    refusal = scrub_reason(state, questions, model=model)
    if refusal is not None:
        await _write(latency_ms=0, answers=None, error=refusal)
        return None

    started = time.monotonic()
    try:
        from kiro_crew.decisions.impl_jev import JevOracle

        answers = await asyncio.wait_for(JevOracle(provider).ask(state, questions), timeout=budget)
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
        # separates a transport failure from a protocol one.
        logger.debug("decisions: %s provider call failed (%s)", point, type(exc).__name__)
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
    await _write(latency_ms=latency_ms, answers=answers, error=None)
    return answers


def _elapsed_ms(started: float) -> int:
    """Whole milliseconds since *started* (a ``time.monotonic()`` reading)."""
    return int((time.monotonic() - started) * 1000)
