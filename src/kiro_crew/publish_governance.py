"""Plane-C publish-governance chokepoint — "may this box publish to <destination>?".

Publishing is a user-driven dashboard HTTP action ("NOT LLM tools"), so the host
PreToolUse gate never sees it.  :func:`publish_denied_reason` is where the
``capabilities.publish`` ceiling and the standalone operator's
``config.publish.allowed_destinations`` allowlist are enforced instead.

It lives in its own module (rather than inside ``dashboard/handlers/artifacts.py``,
where it started) because there is more than one publish surface and they must
share ONE decision, not each grow their own:

* ``api_artifact_publish`` and its sharing/review siblings — the artifact
  registry destinations (``dashboard/handlers/artifacts_parts/``).
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
from contextvars import ContextVar

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
#: (``apps/routes_listing.py``), the deploy handler (``deploy/handlers.py``) and any
#: operator allowlist all name the same string.
DEPLOY_WEB_PROVIDER_ID = "deploy-web-aws"


#: Who can read a public web deploy: anyone with the link.
DEPLOY_WEB_AUDIENCE = "public"

#: The lists only the public web deploy checks today. Every other destination
#: (artifact publish, the drives) is refused unless each of these lists permits
#: every value: those paths take their target from ``meta.json``, which an agent
#: can write and ``publish_sync`` reads again after this check, so a
#: value-by-value check there is not yet sound.
_DEPLOY_ONLY_LISTS = ("regions", "audience", "accounts")

_SCOPE = "capabilities.publish"


def _audit_deny(
    *,
    session_key: str,
    provider_name: str,
    rule: str,
    layer: str,
    reason: str,
    item: str = "",
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
            scope=_SCOPE,
            item=item or f"destinations:{provider_name}",
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
    reopen the allowlist.

    Two independent reads cannot do it: a probe like ``read_config_for_update``
    (which rejects only a non-object TOP level) plus a value taken from
    ``KiroCrewConfig.load().publish.allowed_destinations`` passes a ``config.json``
    of ``{"publish": []}`` — a valid object at the top level — and the loader then
    coerces the non-dict ``publish`` section to ``{}``, so the allowlist comes back
    empty. Empty is indistinguishable from "no restriction configured", i.e.
    default-open. A malformed section would not deny publishing and would not
    surface an error; it would remove the restriction.

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


def publish_denied_reason(
    request: web.Request, provider_name: str, *, lists_checked_by_caller: bool = False
) -> str | None:
    """Return a denial reason for publishing to ``provider_name``, else ``None``.

    The request's only input is its ``X-Session-Key``; :func:`session_denied_reason`
    takes the decision.
    """
    return session_denied_reason(
        request.headers.get("X-Session-Key") or "",
        provider_name,
        lists_checked_by_caller=lists_checked_by_caller,
    )


#: The session key of the request a publish send runs under, bound by
#: :func:`bind_egress_session`. ``None`` means no request bound one.
_EGRESS_SESSION: ContextVar[str | None] = ContextVar("publish_egress_session", default=None)


def bind_egress_session(request: web.Request) -> None:
    """Record the request's session key for :func:`dispatch_denied_reason`.

    aiohttp runs each request in its own task, so the value is scoped to that
    request and to the tasks and threads it starts.
    """
    _EGRESS_SESSION.set(request.headers.get("X-Session-Key") or "")


def dispatch_denied_reason(provider_name: str) -> str | None:
    """The enterprise check at the point bytes leave for a destination.

    ``publish_provider`` runs it before every send method of a provider, with the
    registry key the provider was resolved under. That key is the destination the
    bytes go to, so the decision is on the value used, not on an earlier read of
    the artifact's agent-writable ``meta.json``. A send with no bound request is
    denied: the session's profile cannot be known.
    """
    session_key = _EGRESS_SESSION.get()
    if session_key is None:
        reason = "publishing denied: no request is bound to this send"
        _audit_deny(
            session_key="",
            provider_name=provider_name,
            rule="publish.dispatch",
            layer="dispatch",
            reason=reason,
        )
        return reason
    return session_denied_reason(session_key, provider_name)


def session_denied_reason(
    session_key: str, provider_name: str, *, lists_checked_by_caller: bool = False
) -> str | None:
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
    # ``deploy/handlers.py``, ``apps/routes.py`` and
    # ``handlers/artifacts_parts/route_plumbing.py``, so a module-scope ``platform``
    # import here would put the whole platform tree on every gateway boot and invert
    # that direction. ``sel`` and ``KiroCrewConfig`` carry no such constraint and ARE
    # imported at module scope above.
    from kiro_crew.platform.context import PlatformCompositionError

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

    # Only the core deploy path checks the lists value by value, so only it may
    # skip this refusal. An app can declare a row under the same id, and its
    # endpoint never reaches ``_do_deploy``.
    if not (provider_name == DEPLOY_WEB_PROVIDER_ID and lists_checked_by_caller):
        for list_name in _DEPLOY_ONLY_LISTS:
            if not publish_list_unrestricted(session_key, list_name):
                reason = (
                    f"publishing to {provider_name!r} denied: the policy bounds "
                    f"{list_name}, and only the public web deploy can check it yet"
                )
                _audit_deny(
                    session_key=session_key,
                    provider_name=provider_name,
                    rule=list_name,
                    layer="policy",
                    reason=reason,
                    item=f"{list_name}:",
                )
                return reason

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


def _ruleset_unrestricted(ruleset: object) -> bool:
    """Whether *ruleset* permits every value, the unknown one included.

    Only a deny list with no entries, or an allow list naming ``*``, does. A
    composed ruleset does when both halves do; any other shape counts as
    restricting, so an unknown kind fails closed.
    """
    from kiro_crew.platform.governance import ScopedRuleset, _AndRuleset

    if isinstance(ruleset, _AndRuleset):
        return _ruleset_unrestricted(ruleset.outer) and _ruleset_unrestricted(ruleset.inner)
    if isinstance(ruleset, ScopedRuleset):
        if ruleset.mode == "deny":
            return not ruleset.deny
        # Every publish list uses the ``identifier`` matcher; a literal ``*`` is
        # the one entry read as "every value". Anything else counts as restricting.
        return ruleset.matcher == "identifier" and "*" in ruleset.allow
    return False


def publish_list_unrestricted(session_key: str, list_name: str) -> bool:
    """Whether publish's *list_name* list permits every value at both levels.

    Asks what the list allows, not whether it is present: in an enterprise build
    every list is present (the baseline fills it), so presence says nothing. A
    level with no ``capabilities.publish`` control, or one without the list,
    leaves it open at that level; a disabled gate permits nothing. ``False``
    when the question cannot be answered, so a caller that needs an open list
    refuses.
    """
    from kiro_crew.platform.context import PlatformCompositionError, current_context

    try:
        from kiro_crew.platform.governance import CapabilityGate, policy_level_control
        from kiro_crew.platform.governance_profiles import resolve_active_scope

        ceiling = getattr(current_context(), "governance", None)
        profile = resolve_active_scope(session_key)
        levels = (
            policy_level_control(ceiling, _SCOPE),
            profile.get(_SCOPE) if profile is not None else None,
        )
    except PlatformCompositionError:
        raise
    except Exception:
        logger.debug("publish list probe failed; treating %s as bounded", list_name, exc_info=True)
        return False
    for level in levels:
        if level is None:
            continue
        if not isinstance(level, CapabilityGate) or not level.enabled:
            return False
        ruleset = level.scopes.get(list_name)
        if ruleset is not None and not _ruleset_unrestricted(ruleset):
            return False
    return True


def publish_list_denied_reason(
    session_key: str, provider_name: str, list_name: str, value: str
) -> str | None:
    """Return a denial reason when *value* is not in publish's *list_name* list.

    Asks ``capabilities.publish`` item ``<list_name>:<value>`` (``regions``,
    ``audience`` or ``accounts``) through the same fail-closed chokepoint as
    :func:`publish_denied_reason`, and audits a refusal. ``None`` when permitted.
    Blocking, like that function.
    """
    from kiro_crew.platform.context import PlatformCompositionError

    item = f"{list_name}:{value}"
    try:
        from kiro_crew.platform.governance_profiles import governance_permits

        decision = governance_permits(_SCOPE, item, session_key=session_key, fail_closed=True)
        if getattr(decision, "permitted", False):
            return None
        reason = getattr(decision, "reason", "") or "not permitted by policy"
        _audit_deny(
            session_key=session_key,
            provider_name=provider_name,
            rule=getattr(decision, "rule", ""),
            layer=getattr(decision, "layer", ""),
            reason=reason,
            item=item,
        )
    except PlatformCompositionError:
        raise
    except Exception:
        logger.debug("publish list governance failed; failing closed", exc_info=True)
        reason = "governance could not be evaluated"
    return f"{list_name} {value!r} is not permitted: {reason}"


def deploy_target_denied_reason(session_key: str, region: str) -> str | None:
    """Return a denial reason for a public web deploy to *region*, else ``None``.

    Asks ``audience:public`` (a web deploy is world-readable), then
    ``regions:<region>``. A deploy is refused outright while ``accounts``
    restricts anything: every AWS call in a deploy resolves the profile by name
    again, so an account read once cannot bind the credentials the upload uses.
    Blocking.
    """
    provider = DEPLOY_WEB_PROVIDER_ID
    for list_name, value in (("audience", DEPLOY_WEB_AUDIENCE), ("regions", region)):
        reason = publish_list_denied_reason(session_key, provider, list_name, value)
        if reason:
            return reason
    if publish_list_unrestricted(session_key, "accounts"):
        return None
    reason = (
        "the policy bounds deploy accounts, and binding a deploy to one account "
        "is not supported yet"
    )
    _audit_deny(
        session_key=session_key,
        provider_name=provider,
        rule="accounts",
        layer="policy",
        reason=reason,
        item="accounts:",
    )
    return reason
