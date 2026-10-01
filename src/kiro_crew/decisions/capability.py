"""``capabilities.decisions`` — the one switch that withdraws the Jev seam.

The keystone ``decisions_consent.json`` is the OWNER's answer to "may my messages
be sent to Jev". This module resolves a DIFFERENT question: may this machine run
the seam at all. The two are not the same decision and neither substitutes for the
other — an owner on a managed laptop can consent in good faith to a paid external
endpoint their fleet has not approved, so the fleet needs a ceiling that stands
above the keystone rather than a second copy of it.

Consulted at TWO chokepoints, because either alone is a half-control:

* ``PUT /api/decisions/consent`` — enabling is refused ``403``, so a denial is
  visible at the moment an owner tries to switch the seam on;
* ``decisions.gate._consented_for`` — the ONE keystone read every ``decide`` and
  ``is_enabled`` path funnels through, so an existing ``"enabled": true`` keystone
  is inert under a denial instead of carried over. Without this half, a fleet that
  pinned the row would still send from any machine consented before the pin.

``GET /api/dashboard/config`` reports the same answer as ``decisions_enabled`` so
the Feature Previews card is not drawn at all; that read is presentation, never the
control (the two chokepoints above are).

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

from kiro_crew.platform.governance_profiles import vet_and_audit

logger = logging.getLogger(__name__)

DECISIONS_SCOPE = "capabilities.decisions"

#: The ceiling for a LOCAL PRESET answering instead of hosted Jev. Separate from
#: :data:`DECISIONS_SCOPE` because the two rows answer different questions: the
#: hosted row is about message excerpts reaching a paid third party, and a local
#: preset sends nothing off the machine. A fleet that pins the hosted row off for
#: that reason can still allow -- or deny -- a local model with this row. Only a
#: route-built preset address counts as local (``local_models.active_id``); a
#: hand-written loopback address may be a tunnel to hosted Jev and stays under the
#: hosted row.
DECISIONS_LOCAL_SCOPE = "capabilities.decisions_local"

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


def is_decisions_denied(surface_key: str = DASHBOARD_SURFACE_KEY, *, local: bool = False) -> bool:
    """Return whether the ceiling withdraws the decision seam for *surface_key*.

    *local* selects the row: :data:`DECISIONS_LOCAL_SCOPE` when a local preset
    answers, :data:`DECISIONS_SCOPE` otherwise. Callers derive it from the
    configured provider with :func:`is_local_preset` -- the runtime's attestation --
    never from request input or the configured address alone.

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
    """
    try:
        decision = vet_and_audit(
            DECISIONS_LOCAL_SCOPE if local else DECISIONS_SCOPE,
            "",
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
        _audit_unevaluable(surface_key, DECISIONS_LOCAL_SCOPE if local else DECISIONS_SCOPE)
        return True
    return not getattr(decision, "permitted", False)


def _audit_unevaluable(surface_key: str, scope: str = DECISIONS_SCOPE) -> None:
    """Best-effort SEL record for the path ``vet_and_audit`` never reached. Never raises."""
    try:
        # Local import is DELIBERATE (matches the other SEL sites): the test suite
        # patches ``kiro_crew.sel.sel``, which a load-time binding would bypass.
        from kiro_crew.sel import sel

        sel().log_governance_decision(
            session_key=surface_key,
            tool_name=AUDIT_TOOL,
            scope=scope,
            item="",
            outcome="denied",
            reason=_UNEVALUABLE_REASON,
        )
    except Exception:
        # SEL writes to a file, but an audit failure must never wedge the read.
        pass
