"""``capabilities.decisions`` -- the ceiling over every decision model.

The keystone ``decisions_consent.json`` is the OWNER's answer to "may my messages
be sent to the decision model". This module resolves a DIFFERENT question: may this
machine run the seam at all, and with which decision model. The two are not the same
decision and neither substitutes for the other: an owner on a managed laptop can
consent in good faith to a paid external endpoint their fleet has not approved, so
the fleet needs a ceiling that stands above the keystone rather than a second copy
of it.

The row's ``enabled`` is the full off switch. Its ``providers`` list
(:data:`PROVIDERS_LIST`) is the per-model allow list: each decision model the picker
offers has its own entry (:func:`provider_id`), and every check below that reads the
row for the configured provider also asks that provider's entry.

Consulted at TWO chokepoints, because either alone is a half-control:

* ``PUT /api/decisions/consent`` — enabling is refused ``403``, so a denial is
  visible at the moment an owner tries to switch the seam on;
* ``decisions.gate._consented_for`` — the ONE keystone read every ``decide`` and
  ``is_enabled`` path funnels through, so an existing ``"enabled": true`` keystone
  is inert under a denial instead of carried over. Without this half, a fleet that
  pinned the row would still send from any machine consented before the pin.

``GET /api/dashboard/config`` reports the same answer as ``decisions_enabled``, and
under a denial the Feature Previews card draws the administrator's notice in place of
its controls; that read is presentation, never the control (the two chokepoints
above are).

The shape is the one ``dashboard/social_share.py`` established for the same job:

**One pinned surface, every deny honoured.** The evaluation classifies by the
``dashboard:ui`` surface key rather than anything caller-controlled, so a profile
bound to the dashboard surface can withdraw the seam, and a denied decision from
ANY layer withdraws it — there is no "only a policy counts" carve-out, because this
is a per-request question, not a process-wide startup one.

**Every decision is audited.** The evaluation runs through ``vet_and_audit``, so
each answer acted on leaves a ``governance_decision`` SEL row, and a ceiling that
cannot be evaluated is recorded as the denial it produces.

Nothing here imports the config loader, ``aiohttp`` or the session layer at module
scope, so the package's lazy-import discipline (see ``decisions/__init__.py``)
still holds.
"""

from __future__ import annotations

import logging

from kiro_crew.platform.governance_profiles import (
    governance_answer_generation,
    governance_permits,
    poll_profiles_fresh,
    vet_and_audit,
)

logger = logging.getLogger(__name__)

DECISIONS_SCOPE = "capabilities.decisions"

#: The ceiling for a LOCAL PRESET answering instead of hosted Jev. Separate from
#: :data:`DECISIONS_SCOPE` because the two rows answer different questions: the
#: hosted row is about message excerpts reaching a paid third party, and a local
#: preset sends nothing off the machine. This row only NARROWS: a fleet that permits
#: hosted Jev can still deny a local model with it, but a pinned hosted deny covers
#: local models too (:func:`is_decisions_denied`). Only a
#: route-built preset address counts as local (``local_models.active_id``); a
#: hand-written loopback address may be a tunnel to hosted Jev and stays under the
#: hosted row.
DECISIONS_LOCAL_SCOPE = "capabilities.decisions_local"

#: The per-model allow list: an inner ruleset of :data:`DECISIONS_SCOPE` with one
#: entry per decision model the picker offers, asked as ``providers:<id>``. The ids
#: are the picker's own and are stable policy ids (a policy names them, so a rename
#: would turn an allow or deny into a no-op): :data:`PROVIDER_HOSTED` for the hosted
#: provider, each local preset id (``local_models.LOCAL_MODELS``) and
#: :data:`PROVIDER_SAGEMAKER`. A ceiling that does not name the list permits every one,
#: so the standalone default is unchanged. "No model" (:data:`PROVIDER_NONE`) is never
#: asked: it sends and runs nothing, so it is the way out under any ceiling. An
#: endpoint the owner wrote by hand has no id and is never asked either.
PROVIDERS_LIST = "providers"
#: Allow-list id of the hosted provider; the stored preset id, so it never moves.
PROVIDER_HOSTED = "jev"
#: Allow-list id of any ``sagemaker://`` address, valid or not.
PROVIDER_SAGEMAKER = "sagemaker"
#: What ``local_models.active_id`` answers for a hand-written address: not a list id.
_UNGOVERNED = "custom"
#: "No model": never asked about, never refused.
PROVIDER_NONE = "none"

#: Tool name the SEL ``governance_decision`` row carries, so an operator reading
#: the trail can tell this decision apart from the consent rows beside it.
AUDIT_TOOL = "dashboard_config_decisions"

#: Default surface key: the one the two DASHBOARD callers pin. On an HTTP request the
#: ``X-Session-Key`` header is CALLER-CONTROLLED, so classifying by it would let a
#: request carrying ``slack:x`` dodge a profile bound to the ``dashboard`` surface.
#: Same pin, same rationale, as ``dashboard/social_share.py``; the literal is the id
#: the dashboard sends for itself (``api/client.ts``).
#:
#: It is a DEFAULT and not a constant applied to every caller, because the gate path
#: is not an HTTP request: there the key is the runtime's own identity for the turn,
#: which is trusted, and pinning it would leave a profile bound to that surface
#: unconsulted on the one path that actually sends.
DASHBOARD_SURFACE_KEY = "dashboard:ui"

_UNEVALUABLE_REASON = "governance unavailable (fail-closed)"


def provider_id(endpoint: object, model: object) -> str | None:
    """The allow-list id (:data:`PROVIDERS_LIST`) of the decision model *endpoint*/*model* name.

    :data:`PROVIDER_HOSTED` when *endpoint* is the hosted default endpoint exactly, a
    local preset id for the address the provider route builds for that preset,
    :data:`PROVIDER_SAGEMAKER` for any ``sagemaker://`` address (the scheme alone picks
    the SageMaker client, so a malformed one is still that provider) and
    :data:`PROVIDER_NONE` for "No model". *endpoint* is the configured address with the
    unset fallback already applied (``gate.configured_endpoint``).

    ``None`` for any other address: an endpoint the owner wrote by hand is theirs to
    point anywhere, so the list does not govern it. The row's ``enabled`` (the full
    off switch) and the ``endpoints`` list still do, on every path that asks them.

    A preset is read from the address's shape. That is enough for an allow list: a
    preset-shaped address is sent to only while the runtime attests that preset's
    server on it (``gate._consented_for``), so the entry it answers to is the model
    that answers. Every check is an exact comparison; nothing here parses a host.
    """
    from kiro_crew.decisions import local_models
    from kiro_crew.decisions.impl_sagemaker import is_sagemaker_endpoint

    if is_sagemaker_endpoint(endpoint):
        return PROVIDER_SAGEMAKER
    named = local_models.active_id(endpoint, model)
    return None if named == _UNGOVERNED else named


def provider_ids() -> tuple[str, ...]:
    """Every allow-list id a decision model can have, in the picker's order."""
    from kiro_crew.decisions import local_models

    return (
        PROVIDER_HOSTED,
        *(m.id for m in local_models.LOCAL_MODELS),
        PROVIDER_SAGEMAKER,
    )


def names_local_preset(endpoint: object, model: object) -> bool:
    """Whether *endpoint*/*model* have the shape the provider route builds for a preset.

    A statement about TEXT, so it is only for the route's own request, whose
    endpoint it built from a preset id. Anything read back from ``config.json`` --
    which other writers reach -- goes through :func:`is_local_preset`, because the
    shape alone does not say what listens on that port.
    """
    from kiro_crew.decisions.local_models import PRESET_JEV, active_id

    return active_id(endpoint, model) not in (PRESET_JEV, "custom")


def is_local_preset(endpoint: object, model: object, *, serving: bool = True) -> bool:
    """Whether the gateway's own runtime runs the preset *endpoint*/*model* name, there.

    This is what lets :data:`DECISIONS_LOCAL_SCOPE` govern instead of the hosted row,
    so it is not inferred from the configured address: a route-built address in
    ``config.json`` can be written by any config writer and point at whatever listens
    on that port. Only the runtime that started the server can attest it. *serving*
    (the send path) needs the server answering on that port; with ``serving=False``
    (the consent switch, offered while a preset downloads or starts) the runtime only
    has to be preparing that preset for that port.
    """
    from urllib.parse import urlsplit

    from kiro_crew.decisions import local_models
    from kiro_crew.decisions.local_runtime import STATE_IDLE, STATE_RUNNING, get_runtime

    preset = local_models.get(local_models.active_id(endpoint, model))
    if preset is None or not isinstance(endpoint, str):
        return False
    try:
        port = urlsplit(endpoint.strip()).port
    except ValueError:
        return False
    status = get_runtime().status()
    if status["preset"] != preset.id or status["port"] != port:
        return False
    return status["state"] == STATE_RUNNING if serving else status["state"] != STATE_IDLE


def is_decisions_denied(
    surface_key: str = DASHBOARD_SURFACE_KEY,
    *,
    local: bool = False,
    provider: str | None = None,
) -> bool:
    """Return whether the ceiling withdraws the decision seam for *surface_key*.

    *local* adds :data:`DECISIONS_LOCAL_SCOPE` on top of :data:`DECISIONS_SCOPE`
    when a local preset answers; without it only the hosted row is read. Callers derive it from the
    configured provider with :func:`is_local_preset` -- the runtime's attestation --
    never from request input or the configured address alone.

    *provider* is the allow-list id of the decision model the question is about
    (:func:`provider_id`); it adds the ``providers:<id>`` item of
    :data:`PROVIDERS_LIST`, so a model the list does not allow is denied exactly as a
    pinned row is. ``None`` (a hand-written endpoint) or :data:`PROVIDER_NONE` asks no
    item. With an item the
    rows and the item are answered inside one policy-generation bracket, as
    :func:`denied_sides` answers its two rows, so a policy swap between them cannot
    pair one policy's row permit with another's list permit.

    *surface_key* is what a profile binds on. The two dashboard callers leave it at
    the default, because on an HTTP request the equivalent header is caller-supplied
    and honouring it would let a request dodge a dashboard-bound profile. The gate
    passes the turn's own session key, which is runtime state rather than caller
    input: without it a profile bound to a non-dashboard surface would never be
    consulted on the path that sends.

    Resolved through the standard chokepoint helper so this decision comes from the
    same evaluator as every other governed surface, and audited on the way out by
    the same seam (:func:`vet_and_audit`) so it cannot drift from the audit shape
    the other capability rows write.

    FAIL-CLOSED on an evaluation error. The two dispositions are not symmetric: a
    wrong-DENY falls back to the shipped word-overlap skill rule, which is what
    every unconsented install already runs; a wrong-PERMIT sends message excerpts to
    a paid third party on a fleet that forbade it. That puts this row with
    ``capabilities.publish`` / ``telemetry`` / ``social_share``
    (``fail_closed=True``) rather than with the advisory probes. ``fail_closed`` also
    makes ``governance_permits`` audit the degrade as a critical SEL event.

    Blocking (profile resolution may read from disk). Every caller already runs off
    the event loop: the handlers use ``asyncio.to_thread``, and the gate calls it
    inside the one keystone-read hop it already makes.

    **The local row only narrows.** With *local* a denial on the hosted row denies
    the local path too; :data:`DECISIONS_LOCAL_SCOPE` can
    then withdraw a local model the hosted row permits, never permit one the hosted
    row denies. ``capabilities.decisions`` permits while it is absent or named
    without ``enabled``, so a denial on it is an admin's (or a config's) explicit
    pin, or the fail-closed degrade: before ``capabilities.decisions_local`` existed
    that pin withdrew the whole seam, local models included, and an upgrade must not
    widen it. Both rows are evaluated, never short-circuited: each writes its own
    ``governance_decision`` row, and which rows appear must not depend on the
    other row's answer.
    """
    if provider is None or provider == PROVIDER_NONE:
        if local:
            return any(denied_sides(surface_key))
        return _row_denied(surface_key, DECISIONS_SCOPE)
    _prime_profiles()
    before = _answer_generation()
    denied = [_row_denied(surface_key, DECISIONS_SCOPE)]
    if local:
        denied.append(_row_denied(surface_key, DECISIONS_LOCAL_SCOPE))
    denied.append(_row_denied(surface_key, DECISIONS_SCOPE, f"{PROVIDERS_LIST}:{provider}"))
    if not any(denied) and (before is None or _answer_generation() != before):
        logger.debug("decisions governance answer moved mid-evaluation; denying the model")
        _audit_unevaluable(surface_key, DECISIONS_SCOPE)
        return True
    return any(denied)


def permitted_providers(surface_key: str = DASHBOARD_SURFACE_KEY) -> dict[str, bool]:
    """``{provider id: whether the list allows it}`` for every id :func:`provider_ids` names.

    The :data:`PROVIDERS_LIST` answer alone, without the rows: the settings read that
    decides what the picker offers ANDs it with each option's own row and host
    answers. Asked without an SEL row per id, like the picker's per-region host probe
    (:func:`targets_denied` with ``audit=False``): these are offers, not requests, and
    every request that acts on an answer (the provider and consent writes, the send
    path) asks through :func:`is_decisions_denied`, which audits. One policy-generation
    bracket covers every id; if it moved, every id reads as refused for this read.
    Blocking: call off the event loop.
    """
    ids = provider_ids()
    _prime_profiles()
    before = _answer_generation()
    answers = {
        pid: not _row_denied(surface_key, DECISIONS_SCOPE, f"{PROVIDERS_LIST}:{pid}", audit=False)
        for pid in ids
    }
    if before is None or _answer_generation() != before:
        logger.debug("decisions governance answer moved mid-read; offering no model")
        return dict.fromkeys(ids, False)
    return answers


def denied_sides(surface_key: str = DASHBOARD_SURFACE_KEY) -> tuple[bool, bool]:
    """``(hosted_denied, local_denied)``, each row evaluated and audited exactly once.

    The local side holds the hosted row's deny (see :func:`is_decisions_denied`).
    For a reader that reports both sides without auditing the hosted row twice.

    The two rows are separate evaluations, so a central-policy install or a profile
    reload landing between them could pair the OLD hosted permit with the NEW local
    permit and let a local model run past a freshly installed parent deny. So the
    profile store is primed first (:func:`poll_profiles_fresh`, the same refresh the
    hosted row would otherwise do on first touch, which publishes a snapshot and
    bumps the generation), then the governance answer generation is read BEFORE the
    hosted row and again AFTER the local row. Priming first is what lets the bracket
    cover the whole pair without counting the first touch as a swap. If the
    generation moved, the local side fails closed for this one call (audited as
    unevaluable). A wrong-deny falls back to the shipped skill rule and the next call
    re-evaluates both rows, so this never widens access. Blocking, like the rows
    themselves: every caller already runs off the event loop.
    """
    _prime_profiles()
    before = _answer_generation()
    hosted = _row_denied(surface_key, DECISIONS_SCOPE)
    local = _row_denied(surface_key, DECISIONS_LOCAL_SCOPE)
    if not (hosted or local) and (before is None or _answer_generation() != before):
        logger.debug("decisions governance answer moved mid-evaluation; denying local")
        _audit_unevaluable(surface_key, DECISIONS_LOCAL_SCOPE)
        local = True
    return hosted, hosted or local


def _prime_profiles() -> None:
    """Publish any pending profile snapshot before the generation is read. Best-effort."""
    try:
        poll_profiles_fresh()
    except Exception:
        # poll_profiles_fresh already swallows its own errors; a failure here leaves
        # the generation where it was, so the bracket still compares like with like.
        logger.debug("decisions profile prime failed", exc_info=True)


def _answer_generation() -> int | None:
    """The governance answer token, or ``None`` when it cannot be read (fails closed)."""
    try:
        return governance_answer_generation()
    except Exception:
        return None


def _row_denied(surface_key: str, scope: str, item: str = "", *, audit: bool = True) -> bool:
    """Evaluate and audit ONE capability row or list item. Fail-closed; see
    :func:`is_decisions_denied`.

    With ``audit=False`` the same answer is computed and no SEL row is written for
    it, for a reader that asks about many items to decide what to offer rather than
    to act on an answer.
    """
    from kiro_crew.edition_locks import UNNAMED_REASON, follows_policy, locked_off

    try:
        if scope == DECISIONS_SCOPE and not item and locked_off():
            # LTS: content sent to a decision provider follows this hosted
            # row and is off by default (RFC LTS edition, Red "Content sent
            # to external decisions"). Evaluated without the seam's own audit, so
            # the one row written records the final answer.
            decision = governance_permits(
                scope, "", session_key=surface_key, log_warning=False, fail_closed=True
            )
            permitted = bool(getattr(decision, "permitted", False))
            denied = not permitted or not follows_policy(decision)
            reason = UNNAMED_REASON if permitted and denied else getattr(decision, "reason", "")
            if audit:
                _audit_row(surface_key, scope, "denied" if denied else "allowed", reason)
            return denied
        if not audit:
            decision = governance_permits(
                scope, item, session_key=surface_key, log_warning=False, fail_closed=True
            )
            return not getattr(decision, "permitted", False)
        decision = vet_and_audit(
            scope,
            item,
            session_key=surface_key,
            tool_name=AUDIT_TOOL,
            log_warning=False,
            fail_closed=True,
        )
    except Exception:
        # governance_permits converts its own internal errors into a denying
        # Decision, so reaching here means the import, the composition or the call
        # itself failed — the ceiling is unevaluable, which is the same condition
        # as a degrade. Fail closed, and record the denial the seam could not.
        logger.debug("decisions governance probe failed; denying", exc_info=True)
        if audit:
            _audit_unevaluable(surface_key, scope)
        return True
    return not getattr(decision, "permitted", False)


def targets_denied(
    surface_key: str = DASHBOARD_SURFACE_KEY,
    *,
    point: str | None,
    model: str | None,
    endpoint: str | None,
    audit: bool = True,
) -> bool:
    """Whether the ceiling's lists refuse this request's point, model or endpoint.

    Asks :data:`DECISIONS_SCOPE` for ``points:<point>`` and, when the lane sends a
    *model* to a decision provider at an *endpoint*, ``models:<model>`` and
    ``endpoints:<host>`` for every reading of its host. A ruleset the
    policy does not name permits, so a ceiling without these lists answers exactly
    as before. Every item is evaluated and audited, never short-circuited, so which
    rows appear does not depend on another item's answer. Fail-closed, like
    :func:`is_decisions_denied`. Blocking: call off the event loop.

    ``audit=False`` gives the same answer, with the same policy-generation bracket,
    without writing a ``governance_decision`` row for any item: the settings read that
    decides which options to offer asks about a host per region, and those questions
    are not requests. Every request that acts on an answer (the provider and consent
    writes, the send path) keeps the default.
    """
    # No point for a request no decision point makes (the owner's connection test):
    # only its model and endpoint are asked about.
    items = [f"points:{point}"] if point is not None else []
    if model is not None:
        items.append(f"models:{model}")
    if endpoint is not None:
        # Every host a client could read the endpoint as, so a parser
        # disagreement or a root-dot spelling cannot reach a refused host.
        from kiro_crew.platform.governance_parts.matching import url_policy_hosts

        items.extend(f"endpoints:{host}" for host in url_policy_hosts(endpoint))
    # One request's items must be answered by ONE policy: a swap between two of
    # them could pair one policy's permit for the model with another's permit for
    # the endpoint. Bracketed as in :func:`denied_sides`, failing closed if it moved.
    _prime_profiles()
    before = _answer_generation()
    denied = [_row_denied(surface_key, DECISIONS_SCOPE, item, audit=audit) for item in items]
    if not any(denied) and (before is None or _answer_generation() != before):
        logger.debug("decisions governance answer moved mid-evaluation; denying the request")
        if audit:
            _audit_unevaluable(surface_key, DECISIONS_SCOPE)
        return True
    return any(denied)


def _audit_unevaluable(surface_key: str, scope: str = DECISIONS_SCOPE) -> None:
    """Best-effort SEL record for the path ``vet_and_audit`` never reached. Never raises."""
    _audit_row(surface_key, scope, "denied", _UNEVALUABLE_REASON)


def _audit_row(surface_key: str, scope: str, outcome: str, reason: str) -> None:
    """Best-effort SEL ``governance_decision`` row this module writes itself. Never raises."""
    try:
        # Local import is DELIBERATE (matches the other SEL sites): the test suite
        # patches ``kiro_crew.sel.sel``, which a load-time binding would bypass.
        from kiro_crew.sel import sel

        sel().log_governance_decision(
            session_key=surface_key,
            tool_name=AUDIT_TOOL,
            scope=scope,
            item="",
            outcome=outcome,
            reason=reason,
        )
    except Exception:
        # SEL writes to a file, but an audit failure must never wedge the read.
        pass
