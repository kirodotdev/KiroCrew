"""Whether the account can use a model a Settings picker pins.

A role or agent model pin is judged against the model list the newest live session
of the same backend advertised, with the same predicate the session-start withhold
uses, and is revalidated against a fresh list while one is being fetched.

Served through :mod:`kiro_crew.dashboard.handlers.core`, which callers and tests
import and patch; this module never imports it."""

from __future__ import annotations

import logging
from typing import Any

from aiohttp import web

from kiro_crew.config.loader import KiroCrewConfig

logger = logging.getLogger("kiro_crew.dashboard.handlers.core")


def _provider_backend(provider: object) -> str | None:
    """Return an active provider's backend when its public shape exposes one."""
    client = getattr(provider, "client", None)
    backend = getattr(client, "backend", None)
    if isinstance(backend, str):
        return backend
    backend = getattr(provider, "backend", None)
    return backend if isinstance(backend, str) else None


def _advertised_evidence_provider(
    request: web.Request, *, backend: str | None = None
) -> tuple[Any, list[str]] | None:
    """The live provider whose advertised list judges a pin, with its ids.

    NEWEST session first, the same order :func:`_entitled_kiro_models` reads the
    picker's evidence in: ``active_providers()`` walks live sessions in creation
    order, and a session started BEFORE a plan change still holds the list it
    captured at its own ``session/new``. Reading the oldest one would judge a pin
    against pre-downgrade entitlements -- accepting exactly the model the account
    lost -- and would let the pin validator and the picker disagree about which
    session speaks for the account. When *backend* is supplied, providers for
    other namespaces cannot supply evidence about the target agent's entitlement.
    Returns ``None`` when no matching session advertised anything.
    """
    from kiro_crew.agent_sdk.backends import model_registry_namespace
    from kiro_crew.agent_sdk.drivers.acp import advertised_model_ids

    try:
        providers = request.app["state"].sessions.active_providers()
    except (KeyError, AttributeError):
        return None
    for provider in reversed(list(providers)):
        pb = _provider_backend(provider)
        if backend is not None and (
            pb is None or model_registry_namespace(pb) != model_registry_namespace(backend)
        ):
            continue
        getter = getattr(provider, "available_models", None)
        if not callable(getter):
            continue
        try:
            ids = advertised_model_ids(getter())
        except Exception:
            continue
        if ids:
            return provider, ids
    return None


def _active_advertised_ids(request: web.Request, *, backend: str | None = None) -> list[str] | None:
    """Advertised model ids for a backend namespace, or None if unknown.

    Uses the shared :func:`advertised_model_ids` shape parser so this
    validation sees exactly what the session-init withhold check sees. Returns
    ``None`` when no session has initialized / nothing was advertised, so callers
    treat entitlement as UNKNOWN rather than denying on no evidence. When
    *backend* is supplied, providers for other namespaces cannot supply evidence
    about the target agent's entitlement. The session read is the newest one
    (:func:`_advertised_evidence_provider`).
    """
    found = _advertised_evidence_provider(request, backend=backend)
    return found[1] if found is not None else None


# Answer while a role-pin revalidation is still in flight past the read deadline.
# A denial, not an acceptance: the snapshot in hand would refuse the pin and the
# fresh answer has not landed, so accepting would write a pin on no evidence. The
# probe keeps running and heals the snapshot in place, so a retry is answered by it.
_ROLE_PIN_REVALIDATING = (
    "Model availability for this account is being re-checked; try again in a few seconds."
)


# Bound on how many times the role-pin revalidation reselects the newest evidence
# provider when a newer session registers mid-await. Small: normal churn settles
# in one or two, and the cap only stops a pathological session-churn storm from
# spinning -- it never rejects a pin, it just stops re-probing and proceeds.
_ROLE_PIN_REVALIDATION_MAX_RESELECTS = 4


async def _revalidate_role_pin_evidence(
    value: str, request: web.Request, *, backend: str | None = None
) -> str | None:
    """Revalidate the advertised snapshot a role pin is about to be judged by.

    :func:`_validate_role_model` is synchronous (it runs as a ``validate_fn`` and
    under the crew handlers' config lock), so it cannot probe. It judges the pin
    against the newest live session's ``session/new`` snapshot -- one unconfirmed
    answer that a startup race can leave at the free-tier default, which would
    deny a pin the account is entitled to. This is the awaited step its callers
    run FIRST: it hands the same provider the validator will read to the read-path
    revalidation ``/api/models`` uses (``maybe_refresh_available_models``, declared
    on the provider ABC). That seam owns every decision -- whether the pin would
    drop at all (``catalog_row_would_drop``), whether the snapshot is suspect or
    was probe-confirmed recently (a fresh list is not re-probed), and the probe
    itself with its freshness floor at the snapshot's capture time -- and heals
    the snapshot IN PLACE, so the validator that follows reads the fresh answer.

    Returns ``None`` to proceed, or a denial reason while the probe is still in
    flight past its deadline. A probe that FAILS proceeds on the snapshot as it
    was (fail open, as on the picker read path).
    """
    if not value or value == "auto":
        return None
    from kiro_crew.agent_sdk.drivers.acp import EntitlementRevalidating

    # Reselect the newest evidence provider after each refresh: a newer
    # startup-race session can register DURING the await, and the validator that
    # follows reads whichever session is newest THEN -- so a provider refreshed
    # here must still be the newest when the await returns, or its fresh answer is
    # not the evidence that gets used. Loop until the newest provider is unchanged
    # across its own refresh (bounded, so a session-churn storm cannot spin here);
    # each iteration only re-probes when the newest actually moved.
    seen: set[int] = set()
    for _ in range(_ROLE_PIN_REVALIDATION_MAX_RESELECTS):
        found = _advertised_evidence_provider(request, backend=backend)
        if found is None:
            return None
        provider, ids = found
        if id(provider) in seen:
            # Already refreshed this newest provider and nothing newer displaced
            # it: its fresh answer is the evidence the validator will read.
            return None
        seen.add(id(provider))
        try:
            # The pin is judged as a one-row catalog beside the rows the snapshot
            # already serves. Those extra rows never drop, so they cannot trigger
            # a probe; they only keep the seam's fail-open rule (no surviving row =
            # a namespace mismatch, not an entitlement answer) from reading a lone
            # pin as that mismatch and skipping the revalidation.
            await provider.maybe_refresh_available_models([value, *ids])
        except EntitlementRevalidating:
            return _ROLE_PIN_REVALIDATING
        except Exception:
            logger.debug("role-pin entitlement revalidation failed", exc_info=True)
            return None
    return None


def _active_provider_name() -> str:
    """The configured agent provider. FILESYSTEM IO -- never call this on the loop.

    ``KiroCrewConfig.load()`` deep-copies the validated dict even on a cache hit and
    reads plus validates files on a miss. ``_model_rejected_reason`` performs exactly
    this read when its caller supplies no provider, and the ``validate_fn`` hooks are
    called SYNCHRONOUSLY from ``api_kirocrew_config_patch`` -- so the handler resolves
    it with ``asyncio.to_thread`` once per request and hands the answer down
    (``no-blocking-call-on-event-loop``).

    Returns ``""`` when the config cannot be read, which is the same value
    ``_model_rejected_reason`` falls back to, so the answer does not change.
    """
    try:
        return KiroCrewConfig.load().agent.provider
    except Exception:  # pragma: no cover - config load is resilient
        return ""


def _active_provider_and_pin_backend() -> tuple[str, str]:
    """``(agent.provider, agent.acp_backend)`` in one read. FILESYSTEM IO -- off the loop.

    The role, fallback and decision pins ``api_kirocrew_config_patch`` validates
    run on the DEFAULT harness (``agent.acp_backend``; ``""`` is kiro), so that
    harness is the one whose live catalog may judge them. Resolved beside the
    provider so the request pays one config read, not two, and handed to both
    the revalidation and the validator: the newest-first evidence scan keeps
    only providers in that harness's model-registry namespace. Without the
    scope, a member DM session on another harness (``agent.member_acp_backend``)
    created AFTER the default-harness session would be the newest evidence and
    its catalog would deterministically reject every id of the default harness.
    """
    try:
        agent = KiroCrewConfig.load().agent
    except Exception:  # pragma: no cover - config load is resilient
        return "", ""
    return str(getattr(agent, "provider", "") or ""), str(getattr(agent, "acp_backend", "") or "")


def _no_kiro_id_reason(value: str, backend: str | None) -> str | None:
    """Why *value* cannot be pinned on *backend*: a registered model kiro-cli has no id for."""
    from kiro_crew.agent_sdk.capabilities import MODEL_NAMESPACE_ACP, capabilities_for
    from kiro_crew.model_registry import lacks_acp_id

    on_kiro = capabilities_for(backend or "").model_id_namespace == MODEL_NAMESPACE_ACP
    if on_kiro and lacks_acp_id(value):
        return f"{value!r} is not a model kiro-cli serves; choose another model or 'auto'."
    return None


def _validate_role_model(
    value: str,
    request: web.Request,
    provider: str | None = None,
    *,
    backend: str | None = None,
) -> str | None:
    """Reject a per-role model pin the account cannot use; ``None`` = allow.

    ``""`` / ``"auto"`` always allow (they defer to the chat default). Otherwise
    reuse the per-session provider guard (rejects display-only canonical keys for
    the active provider), then — when a live advertised set is known — apply the
    SAME entitlement predicate the session-init withhold uses
    (:func:`model_is_unusable`) so the picker and the wire cannot disagree.
    No advertised set => accept (entitlement unknowable; don't accuse on no
    evidence), matching that predicate's own conservative default -- except a
    pin on an ``acp``-namespace backend (kiro, kas, the empty default) that
    ``lacks_acp_id`` flags.

    *provider* is the ``validate_fn`` hooks' shared third argument and is not
    read here. *backend* is the harness the edited agent will use: it decides
    whether a canonical key is display-only (:func:`_model_rejected_reason`,
    which then pays no config read of its own) and scopes any live catalog.
    Omit it when that identity is unavailable.
    """
    if not value or value == "auto":
        return None
    from kiro_crew.agent_sdk.drivers.acp import model_is_unusable
    from kiro_crew.dashboard.chat_handlers import _model_rejected_reason

    reason = _model_rejected_reason(value, backend)
    if reason:
        return reason
    advertised = (
        _active_advertised_ids(request)
        if backend is None
        else _active_advertised_ids(request, backend=backend)
    )
    if advertised is None:
        return _no_kiro_id_reason(value, backend)
    if model_is_unusable(value, advertised):
        usable = ", ".join(advertised[:8]) or "auto"
        return f"{value!r} is not available on your account; choose one of: {usable}, or 'auto'."
    return None
