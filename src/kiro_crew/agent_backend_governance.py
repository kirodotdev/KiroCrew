"""Which ACP harness THIS DEPLOYMENT may select — the ``agent_backend`` scope.

Two different questions decide whether a backend can be chosen:

* **Can this build serve it?** ``acp_backends.selectable_backends()`` — a capability
  fact contributed by the edition that registered the provider. Not governable: a
  policy cannot conjure a harness the build has no code for.
* **May this deployment select it?** the ``agent_backend`` governance scope,
  answered here, so a managed fleet can qualify one harness and bound the rest.

## Where this is enforced, and why it is the only place it can be

By NARROWING the registry itself — :func:`narrow_selectable_backends`, driven from
``bootstrap_context`` after ``set_context`` and after every edition's
``register_acp_backends``, so the first session already sees the policy.

Nothing downstream gains a check: the single selectability gate
``resolve_selected_backend`` already reads ``selectable_backends()`` per call, so a
persisted value the policy denies degrades to the floor with a logged reason on the
next load, and the PATCH allowlist, ``GET /api/config/schema`` and the provider
factory all inherit the narrowed answer for free.

## What this promise covers, and what it deliberately does not

The set is decided at GATEWAY START. A ceiling installed mid-process — a fleet policy
picked up by ``policy_distribution``'s poll thread, or ``refresh_now()`` from the CLI —
does NOT re-derive it, and that is a choice rather than an oversight.

Recomputing the registry on that path would bind the new ceiling for backend
SELECTION while leaving sessions and pooled providers already running a now-denied
harness untouched. That is an enforcement that reads as complete and is not, which is
worse than a narrow promise kept: an operator would see the option disappear and
conclude the harness had stopped being used. Retiring live work needs a
session-lifecycle path that does not exist yet, so this scope promises only what it can
keep — a policy change binds on the next gateway start — and says so in the dashboard
panel and in ``docs/system-specs/modules/governance.md``.

The recompute is nevertheless ``baseline - denied`` ASSIGNED not subtracted
(:func:`~kiro_crew.acp_backends.apply_selectable_denials`), so it is idempotent and
restores what a stricter earlier pass removed. That costs nothing at boot and means
adding the runtime call site, once retirement exists, is a one-line change rather than
a redesign.

Three documented invariants make every other position wrong, which is worth stating
because a per-call check reads as the more obvious design:

* **H3** — the one gate ``resolve_selected_backend`` runs inside
  ``KiroCrewConfig.load()`` and must never read the platform context, because
  ``current_context()``'s lazy branch loads config and would re-enter that load.
* **H4** — selectability has exactly ONE gate. A second derivation (an intersection
  in the dashboard handler, say) is the drift a registry replaced in the first place.
* **H13** — the Kiro construction path gains no conditional, no new required
  argument and no new failure mode in service of an adapter, and it names
  ``create_provider_factory`` as a constrained site.

Narrowing at the registry satisfies all three: the context is installed at both call
sites (so no re-entrant load), the registry stays the single source, and no call site
changes.

## Additive over a floor

``{"agent_backend": {"mode": "allow", "allow": ["claude"]}}`` means **also allow
claude**, not **only claude**: :data:`~kiro_crew.acp_backends.GOVERNANCE_FLOOR_BACKEND`
is never submitted to the scope, so no rule can remove it. The exclusive reading was
rejected because it can empty the selectable set, and an install with no startable
harness cannot be repaired from the dashboard — the trust-root policy is the one file
the dashboard may not write.

## Disposition on error: closed, which here does not mean bricked

A governance error denies the backend it was evaluating. That is fail-closed (an
unqualified harness never becomes selectable on an unreadable policy) while still
leaving the floor selectable, which is only true BECAUSE of the floor — without it,
closed and bricked would be the same state.
"""

from __future__ import annotations

import logging

from kiro_crew.acp_backends import (
    GOVERNANCE_FLOOR_BACKEND,
    POLICY_ID_BY_BACKEND,
    apply_selectable_denials,
    registered_backends,
    selectable_backends,
)

logger = logging.getLogger(__name__)

#: Scope key in ``SCOPE_CATALOG``. Named once so the reader, the audit record and
#: the deny message cannot spell it differently.
SCOPE = "agent_backend"


def _policy_id(backend: str) -> str:
    """Policy-facing spelling of *backend*.

    Unknown ids pass through verbatim rather than raising: this is a gate, and an id
    it does not recognize must reach the scope to be judged (where an allow-mode rule
    denies it by omission), not bypass the gate on a KeyError.
    """
    return POLICY_ID_BY_BACKEND.get(backend, backend)


def _audit(backend: str, decision: object, permitted: bool) -> None:
    """Record ONE governance decision on the security event log; never raise.

    Both directions, unlike the publish gate which audits denials only. That gate
    is evaluated per candidate row on every panel open, so auditing permits would
    turn an authorization log into a page-view log; this runs once per gateway
    start and once per pushed ceiling, so the full record is cheap — and "which
    harnesses did this deployment admit" is the question an operator actually
    reconstructs afterwards, which a denials-only log cannot answer.

    ``"allowed"``/``"denied"`` is the vocabulary ``log_governance_decision`` pins in
    its own docstring (``sel.py``) and the one the canonical helper
    (``governance_profiles``) emits. A local spelling like ``"permitted"`` would make
    this scope's records the only ones a log query for allowed decisions misses.
    """
    try:
        from kiro_crew import sel as _sel_mod

        _sel_mod.sel().log_governance_decision(
            session_key=_host_session_key(),
            tool_name=f"agent_backend:{_policy_id(backend)}",
            scope=SCOPE,
            item=_policy_id(backend),
            outcome="allowed" if permitted else "denied",
            rule=str(getattr(decision, "rule", "") or ""),
            layer=str(getattr(decision, "layer", "") or ""),
            reason=str(getattr(decision, "reason", "") or ""),
        )
    except Exception:
        logger.debug("agent_backend governance audit failed for %r", backend, exc_info=True)


def _host_session_key() -> str:
    """``HOST_SESSION_KEY`` — the sentinel for an in-process host action.

    NOT the empty string, which is the whole point: an empty key classifies to
    surface ``unknown`` and matches no profile, so a host-bound profile
    (``bind: {type: surface, id: host}``) would be silently ignored and the harness
    it denies would stay selectable. Resolved through a deferred import because this
    module is imported from ``bootstrap`` at boot and must not pull the platform
    tree onto that path at module load.
    """
    from kiro_crew.platform.governance_profiles import HOST_SESSION_KEY

    return HOST_SESSION_KEY


def _scope_permits(backend: str) -> bool:
    """Ask the ``agent_backend`` scope about ONE non-floor backend, and audit it.

    Bound to the HOST surface (see :func:`_host_session_key`) — this is an
    in-process boot-time decision with no user-facing surface behind it, the same
    binding the app-activation and messaging host gates use.

    Fails closed on any error. ``governance_permits(fail_closed=True)`` produces the
    DENY for errors raised inside it; the ``except`` here covers what it does not
    swallow (an absent platform context, an import failure).
    """
    try:
        from kiro_crew.platform.governance_profiles import governance_permits

        decision = governance_permits(
            SCOPE,
            _policy_id(backend),
            session_key=_host_session_key(),
            fail_closed=True,
        )
        permitted = bool(getattr(decision, "permitted", False))
        _audit(backend, decision, permitted)
        return permitted
    except Exception:
        logger.debug(
            "agent_backend governance evaluation failed for %r; denying", backend, exc_info=True
        )
        _audit(backend, None, False)
        return False


def narrow_selectable_backends() -> list[str]:
    """Recompute which backends this deployment may select. Returns what was removed.

    Called from BOTH ``bootstrap_context`` (after the context installs and every
    edition has finished registering) and ``policy_distribution.apply_ceiling``
    (whenever a ceiling is installed at runtime). Both sit outside
    ``KiroCrewConfig.load()``, so ``governance_permits`` can read the ceiling without
    re-entering that load.

    Iterates the BASELINE, not the currently-selectable set: asking the narrowed set
    what to narrow is how a one-way ratchet gets built, and it is exactly what made
    the earlier destructive version unable to restore a loosened policy.

    The floor is skipped, not asked about — it is what guarantees the set cannot be
    emptied. Never raises: a boot that aborts because a policy could not be evaluated
    is worse than one that starts on the floor alone, and the same holds for a runtime
    refresh, whose caller keeps serving traffic either way.
    """
    try:
        denied = {
            backend
            for backend in sorted(registered_backends())
            if backend != GOVERNANCE_FLOOR_BACKEND and not _scope_permits(backend)
        }
        removed = sorted(apply_selectable_denials(denied))
        if removed:
            logger.info(
                "agent_backend policy removed %s from the selectable set; selectable now: %s",
                ", ".join(repr(_policy_id(b)) for b in removed),
                ", ".join(repr(b) for b in sorted(selectable_backends())),
            )
        return removed
    except Exception:
        logger.warning("narrow_selectable_backends failed; registry unchanged", exc_info=True)
        return []


__all__ = ["SCOPE", "narrow_selectable_backends"]
