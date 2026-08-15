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
    ConfigReadError,
    KiroCrewConfig,
    config_local_path,
    config_path,
    read_config_for_update,
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


def _unparseable_config_reason() -> str | None:
    """Return a denial reason when the on-disk config cannot be trusted.

    ``KiroCrewConfig.load()`` deliberately DEGRADES: it catches
    ``json.JSONDecodeError``/``OSError``, logs a warning and returns DEFAULTS.
    That is right for an ordinary consumer and wrong here — an unparseable
    ``config.json`` would turn a narrowed ``publish.allowed_destinations`` back
    into the empty (allow-every-destination) default and silently reopen the path
    the operator closed. Because ``load()`` cannot fail, a ``try/except`` around
    it never fires, so this chokepoint has to ask the question itself.

    It asks through :func:`read_config_for_update`, which already IS the repo's
    fail-closed config read (absent → ``{}``, unreadable or non-object →
    ``ConfigReadError``, ``UnicodeDecodeError`` named explicitly because it is a
    ``ValueError`` rather than an ``OSError``). Hand-rolling a second spelling of
    "a config parse must not degrade" would give the invariant two homes and let
    them drift; the name reads oddly here only because nothing is being updated.

    Both files are checked: the overlay ``config.local.json`` is deep-merged over
    the base and swallows its own parse errors the same way, so a corrupt overlay
    hides an allowlist just as effectively as a corrupt base.

    A file that is simply ABSENT is not an error — that is the standalone default,
    and an unnamed publish is ungoverned and permitted.

    These names are module-scope imports, unlike the ``kiro_crew.platform`` ones
    inside :func:`publish_denied_reason`: ``config.loader`` is already a
    module-scope dependency of this file (``KiroCrewConfig``), so the CPP
    import-direction invariant has nothing to say about importing more of it.
    """
    for path in (config_path(), config_local_path()):
        try:
            read_config_for_update(path)
        except ConfigReadError as e:
            logger.warning("publish denied: %s", e)
            return f"publishing denied: {path.name} could not be read as a JSON object"
    return None


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
    # The parseability check comes FIRST and is not redundant with the try/except
    # below: ``load()`` swallows its own parse errors and returns defaults, so a
    # corrupt config would present as an EMPTY allowlist — indistinguishable from
    # an operator who never narrowed it — and silently reopen a closed path. The
    # except is kept for anything else ``load()`` can raise.
    unparseable = _unparseable_config_reason()
    if unparseable:
        _audit_deny(
            session_key=session_key,
            provider_name=provider_name,
            rule="publish.allowed_destinations",
            layer="config",
            reason=unparseable,
        )
        return unparseable
    try:
        allowed = KiroCrewConfig.load().publish.allowed_destinations
    except Exception:
        logger.debug("publish config load failed; failing closed", exc_info=True)
        # Audited like every other refusal: this was the last denial path that
        # returned silently, which made `_audit_deny`'s "every layer routes
        # through here" claim false. A refusal an operator cannot find in the
        # audit log is indistinguishable to them from the publish never having
        # been attempted.
        reason = "publishing denied: publish config could not be loaded"
        _audit_deny(
            session_key=session_key,
            provider_name=provider_name,
            rule="publish.allowed_destinations",
            layer="config",
            reason=reason,
        )
        return reason
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
