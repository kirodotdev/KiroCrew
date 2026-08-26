"""Plane-C publish-governance chokepoint — "may this box publish to <destination>?".

Publishing is a user-driven dashboard HTTP action ("NOT LLM tools"), so the host
PreToolUse gate never sees it.  :func:`publish_denied_reason` is where the
``capabilities.publish`` ceiling and the standalone operator's
``config.publish.allowed_destinations`` allowlist are enforced instead.

It lives in its own module (rather than inside ``dashboard/handlers/artifacts.py``,
where it started) because there is more than one publish surface and they must
share ONE decision, not each grow their own:

* ``api_artifact_publish`` and its sharing/review siblings — the artifact
  registry destinations (``dashboard/handlers/artifacts.py``).
* ``/api/deploy/deploy`` and the ``deploy-web-aws`` row of
  ``GET /api/publish-providers`` — the public-web deploy path, whose destination
  id is :data:`DEPLOY_WEB_PROVIDER_ID`.

Layering (tightest wins):

1. the governance ceiling ∩ profile — the ``capabilities.publish`` gate AND its
   inner ``destinations`` ruleset (item ``destinations:<provider>``).  Read from
   the trust-root ``security_policy.json``, which the agent can neither read nor
   rewrite, so this is the durable operator control;
2. ``config.publish.allowed_destinations`` — the standalone operator's
   narrowing knob (default-open; empty list allows every destination).  It can
   only NARROW: a destination the ceiling denies is never re-permitted here,
   because the security policy is never merged from ``config.json``.

Disposition on error: publishing is an **authorization** decision (bytes leave
the box), so unlike the messaging/cron chokepoints it fails **CLOSED** rather
than degrading to permit. That includes a config we cannot parse:
``KiroCrewConfig.load()`` degrades to defaults on a malformed file, which would
present a narrowed ``allowed_destinations`` as the empty allow-all one, so this
module checks parseability itself rather than inheriting that degrade.
"""
from __future__ import annotations

import logging

from aiohttp import web

from kiro_crew import sel as _sel_mod
from kiro_crew.config.loader import (
    DEGRADED_WHOLE_CONFIG,
    KiroCrewConfig,
    degraded_config_files,
)

logger = logging.getLogger(__name__)

#: Destination id of the core public-web deploy provider (S3 + CloudFront in the
#: user's own AWS account).  Declared here so the provider registry
#: (``apps/routes.py``), the deploy handler (``deploy/handlers.py``) and any
#: operator allowlist all name the same string.
DEPLOY_WEB_PROVIDER_ID = "deploy-web-aws"


def _audit_deny(
    *, session_key: str, provider_name: str, rule: str, layer: str, reason: str
) -> None:
    """Record ONE denial on the security event log; never raise.

    Every layer that can refuse a publish routes its denial through here, so a
    new layer cannot ship with a silent refusal — an operator reconstructing "why
    was this publish refused" must find the answer whichever control fired. Only
    denials are recorded: the provider-listing caller evaluates this gate once per
    candidate row on every panel open, so auditing permits would turn an
    authorization log into a page-view log. The publish itself is audited where
    the bytes leave.
    """
    try:
        _sel_mod.sel().log_governance_decision(
            session_key=session_key,
            tool_name=f"artifact_publish:{provider_name}",
            scope="capabilities.publish",
            item=f"destinations:{provider_name}",
            outcome="denied",
            rule=rule,
            layer=layer,
            reason=reason,
        )
    except Exception:
        logger.debug("publish governance deny audit failed", exc_info=True)


def _read_allowed_destinations() -> tuple[list[str], str | None]:
    """Return ``(allowlist, denial_reason)`` from ONE read of the config.

    The two questions this gate needs — "is the config trustworthy" and "what
    did the operator allow" — are answered from a single ``load()``, because
    answering them from two independent reads is what let a malformed section
    reopen the allowlist (#4057).

    The old shape: a probe called ``read_config_for_update`` (which rejects only
    a non-object TOP level) and, separately, the value came from
    ``KiroCrewConfig.load().publish.allowed_destinations``. A ``config.json`` of
    ``{"publish": []}`` is a valid object at the top level, so the probe PASSED —
    and the loader then coerced the non-dict ``publish`` section to ``{}``, so
    the allowlist came back empty. Empty is indistinguishable from "no
    restriction configured", i.e. default-open. A malformed section did not deny
    publishing and did not surface an error; it removed the restriction.

    Re-reading the FILE here cannot fix that, which is worth stating because it
    is the obvious approach: ``load()`` runs a migration that REWRITES
    ``config.json`` in normalized form, so after the gateway's first load the
    malformed section is gone from disk and any later re-read sees a clean file
    with an empty allowlist. The loader is the only place that ever sees the
    degradation, so it is the loader that reports it — via
    ``degraded_sections``.

    Two shapes deny: a config FILE that could not be read as a JSON object, and
    a ``publish`` section present but not an object. Both arrive through
    ``degraded_sections``.

    NOT handled here, deliberately: a malformed ``allowed_destinations`` INSIDE
    a well-formed section. The loader normalizes that value before this function
    can see it — its comprehension iterates whatever it is given, so the string
    ``"deploy-web"`` becomes the character list ``["d", "e", ...]`` — and a
    check here would be dead code that only looked like a guard. It is the same
    class one level down and wants the same treatment (the loader reporting what
    it discarded), which is a change to the loader's per-field parsing rather
    than to this gate. Filed separately rather than half-done here.

    An ABSENT config or section is not degraded: genuinely unconfigured, and an
    unnamed publish is ungoverned and permitted, so a standalone host is
    unaffected.
    """
    cfg = KiroCrewConfig.load()
    if DEGRADED_WHOLE_CONFIG in cfg.degraded_sections:
        named = ", ".join(degraded_config_files(cfg.degraded_sections)) or "config.json"
        reason = (
            f"publishing denied: {named} could not be read as a JSON object, "
            "so the destination allowlist is unknown; fix the file and restart "
            "the gateway to clear"
        )
        logger.warning("%s", reason)
        return [], reason
    if "publish" in cfg.degraded_sections:
        reason = (
            "publishing denied: the 'publish' config section is malformed "
            "(not a JSON object), so the destination allowlist is unknown; "
            "fix the file and restart the gateway to clear"
        )
        logger.warning("%s", reason)
        return [], reason
    return list(cfg.publish.allowed_destinations), None


def publish_denied_reason(request: web.Request, provider_name: str) -> str | None:
    """Return a denial reason for publishing to ``provider_name``, else ``None``.

    Callers turn a non-``None`` reason into a 403 (or, for a provider *listing*,
    omit the row).  Enforces, tightest-wins:
      1. governance ceiling ∩ profile — ``capabilities.publish`` gate AND its
         inner ``destinations`` ruleset (item ``destinations:<provider>``);
      2. the standalone operator's ``config.publish.allowed_destinations``
         allowlist (default-open, narrow-only — cannot widen past the ceiling).
    A ``PlatformCompositionError`` propagates (fail-closed CPP); any other
    governance error fails CLOSED (DENY) — publishing is an authorization
    decision (bytes leave the box), so unlike the messaging/cron chokepoints it
    must NOT degrade-to-permit. The DENY is produced inside ``governance_permits``
    (``fail_closed=True``), because that helper swallows its own internal errors —
    the ``except`` here only catches errors raised OUTSIDE it.

    Blocking: this reads the trust-root policy, every governance profile, and
    ``config.json`` from disk. Async callers must offload it
    (``await asyncio.to_thread(...)``) rather than stalling the event loop.
    """
    # The three ``kiro_crew.platform`` imports in this function stay FUNCTION-LOCAL
    # on purpose, and hoisting them would be a real regression rather than a style
    # win: the CPP import-direction invariant (see platform-context.md, "Deferred-
    # import exception") is that a lower module never reaches ``platform`` at
    # module-LOAD time, only at call time — ``platform/defaults.py`` imports these
    # lower modules itself. This module is imported at module scope by
    # ``deploy/handlers.py``, ``apps/routes.py`` and ``handlers/artifacts.py``, so a
    # module-scope ``platform`` import here would put the whole platform tree on
    # every gateway boot and invert that direction. ``sel`` and ``KiroCrewConfig``
    # carry no such constraint and ARE imported at module scope above.
    from kiro_crew.platform.context import PlatformCompositionError

    session_key = request.headers.get("X-Session-Key") or ""
    try:
        from kiro_crew.platform.governance_profiles import governance_permits

        decision = governance_permits(
            "capabilities.publish",
            f"destinations:{provider_name}",
            session_key=session_key,
            # Authorization chokepoint: a governance-evaluation error must DENY
            # (bytes leave the box). governance_permits swallows its own internal
            # errors, so the fail-closed DENY has to be produced INSIDE it — the
            # ``except`` below only ever sees errors raised outside
            # governance_permits (e.g. the audit call).
            fail_closed=True,
        )
        # Default to DENY (permitted=False) if the Decision is malformed: this is
        # an exfil authorization chokepoint documented as "must NOT
        # degrade-to-permit", so a missing/odd attr must fail closed, not open.
        if not getattr(decision, "permitted", False):
            _audit_deny(
                session_key=session_key,
                provider_name=provider_name,
                rule=getattr(decision, "rule", ""),
                layer=getattr(decision, "layer", ""),
                reason=getattr(decision, "reason", ""),
            )
            return getattr(decision, "reason", "publishing not permitted by policy")
    except PlatformCompositionError:
        raise
    except Exception:
        # Fail CLOSED: publishing is an authorization decision (bytes leave the
        # box to an external destination), so an unexpected error must DENY
        # rather than degrade-to-permit. governance_permits(fail_closed=True)
        # already denies on ITS own internal errors; this branch is the belt-and-
        # suspenders catch for anything raised OUTSIDE it (e.g. the deny-audit
        # call above), keeping the whole helper deny-on-error.
        try:
            from kiro_crew.platform.governance_profiles import audit_governance_degraded

            audit_governance_degraded(
                "artifact_publish", session_key=session_key, scope="capabilities.publish"
            )
        except Exception:
            logger.debug("publish governance degrade audit unavailable", exc_info=True)
        return "publishing denied: governance could not be evaluated"

    # Config allowlist (default-open, narrow-only). Empty list allows any
    # registered destination; a non-empty list restricts to those provider ids.
    #
    # ONE validated read yields both the verdict and the value. Asking those two
    # questions separately is the defect: `load()` swallows its own parse errors
    # and returns defaults, so a corrupt config (or a malformed `publish`
    # section) presented as an EMPTY allowlist — indistinguishable from an
    # operator who never narrowed it — and silently reopened a closed path.
    try:
        allowed, refusal = _read_allowed_destinations()
    except Exception:
        logger.debug("publish config read failed; failing closed", exc_info=True)
        # Audited like every other refusal: a refusal an operator cannot find in
        # the audit log is indistinguishable to them from the publish never
        # having been attempted.
        allowed, refusal = [], "publishing denied: publish config could not be loaded"
    if refusal:
        _audit_deny(
            session_key=session_key,
            provider_name=provider_name,
            rule="publish.allowed_destinations",
            layer="config",
            reason=refusal,
        )
        return refusal
    if allowed and provider_name not in allowed:
        _audit_deny(
            session_key=session_key,
            provider_name=provider_name,
            rule="publish.allowed_destinations",
            layer="config",
            reason="destination not in the operator allowlist",
        )
        return (
            f"publish destination {provider_name!r} is not in the operator allowlist "
            "— ask whoever administers this deployment, or see deploy-web.md §6.8"
        )
    return None
