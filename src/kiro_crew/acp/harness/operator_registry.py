"""Load ``harnesses.json`` at boot and register every valid operator harness.

This module is the ONE orchestration point that turns operator descriptors (data
on disk) into a servable, selectable backend. It sits between three seams that
each own a narrower job and deliberately know nothing of each other:

* :func:`kiro_crew.acp.harness.descriptor.load_operator_descriptors` -- pure
  parse. Reads the file, returns ``(valid, invalid)``, never registers or raises.
* :func:`kiro_crew.agent_sdk.backends.register_known_backend` -- the vocabulary
  side. Makes an id spellable, routed, labelled, policy-nameable, own-namespaced,
  and (with ``runtime=True``) served on ``AcpRuntime``.
* :func:`kiro_crew.acp.harness.register_operator_harness` -- the runtime side.
  Makes the id resolvable through ``harness_for`` as a ``DescriptorHarness``.
* :func:`kiro_crew.agent_sdk.backends.register_selectable_backend` -- the
  selection side. Makes a ROUTED id offerable in the backend switch.

Ordering within one valid descriptor matters and is fixed here:
``register_known_backend`` FIRST (it records the routing ``register_selectable_backend``
then reads), the harness registration, and ``register_selectable_backend`` LAST and
ONLY when the descriptor is selectable. A descriptor with no recognized routing is
registered as KNOWN (spellable, nameable in a rule) but NOT selectable -- the
visible-but-unselectable row D3 wants, whose reason stays retrievable via
:func:`invalid_operator_harnesses` / :func:`unselectable_operator_harnesses` for the
future Settings surface.

Idempotence is a hard requirement: ``bootstrap_context`` can run more than once in
a process (``cli.main`` then ``run_gateway``), and re-registering a known id raises.
So :func:`load_and_register_operator_descriptors` SKIPS an id already registered,
which makes a second boot a no-op rather than a crash.

A note on precedence, load-bearing for the ordering constraint (D4): this runs
BEFORE the first config load resolves ``agent.acp_backend``, because a persisted
value naming an operator harness must survive ``resolve_selected_backend`` -- which
reads the selectable registry live. ``bootstrap_context`` calls this immediately
after ``set_context`` and before both ``register_acp_backends`` and the governance
narrowing that re-runs ``resolve_selected_backend`` on ``cfg.agent.acp_backend``.

A note on freshness: this reads ``harnesses.json`` ONCE, at boot. An edit to that
file takes effect on the next gateway start, not live -- the same restart the
docstring of every boot-time registration implies, and stated for the operator in
the module docstring of :mod:`kiro_crew.acp.harness.descriptor`.
"""

from __future__ import annotations

import logging
from typing import Optional

from kiro_crew.acp.harness import register_operator_harness
from kiro_crew.acp.harness.descriptor import (
    ROUTING_AGENT_SPEC,
    ROUTING_SESSION_CONFIG,
    HarnessDescriptor,
    load_operator_descriptors,
)
from kiro_crew.agent_sdk.backends import (
    ACP_BACKENDS_KNOWN,
    Routing,
    register_known_backend,
    register_selectable_backend,
)

logger = logging.getLogger(__name__)

#: Descriptors that FAILED to parse/validate, keyed by the id they were filed
#: under, mapped to their diagnosable reasons. The ``invalid()`` shape the salvage
#: registry carried, kept for the future Settings surface: a malformed entry costs
#: its row, never boot, and the row stays retrievable so an operator can be shown
#: what is wrong. Populated by :func:`load_and_register_operator_descriptors`.
_INVALID: dict[str, list[str]] = {}

#: Valid descriptors that registered as KNOWN but NOT SELECTABLE -- their routing
#: was absent or unrecognized -- keyed by id and mapped to the reason. Distinct from
#: :data:`_INVALID`: these parsed cleanly and can be spelled and named in a rule;
#: they simply cannot be offered as a session backend until they declare how their
#: permission decision is delivered. The visible-but-unselectable row D3 wants.
_UNSELECTABLE: dict[str, str] = {}


def _routing_enum_for(descriptor: HarnessDescriptor) -> Optional[Routing]:
    """The ``backends.Routing`` a descriptor's routing string maps to, or ``None``.

    ``None`` means "no verified routing" -- the descriptor registers as
    known-but-unselectable. The two descriptor routing constants and the two enum
    members share their wire values (``"agent_spec"`` / ``"session_config"``), but
    this maps them explicitly rather than by ``Routing(descriptor.routing)`` so an
    unrecognized string returns ``None`` here instead of raising a ``ValueError``
    the caller would have to catch -- the descriptor layer already validated the
    string, and anything it let through that is not one of these two is, by
    definition, the unselectable case.
    """
    if descriptor.routing == ROUTING_AGENT_SPEC:
        return Routing.AGENT_SPEC
    if descriptor.routing == ROUTING_SESSION_CONFIG:
        return Routing.SESSION_CONFIG
    return None


def load_and_register_operator_descriptors(*, path=None) -> None:
    """Read ``harnesses.json`` and register every valid operator harness.

    Called once from ``bootstrap_context`` after the context is installed and
    before the first config load resolves ``agent.acp_backend`` (D4). Never raises:
    a parse failure is recorded in :data:`_INVALID`, and a single descriptor that
    cannot be registered is logged and skipped, so one bad entry costs its row and
    nothing more -- the gateway still boots on the builtin harnesses.

    For each VALID descriptor, in order:

    1. ``register_known_backend`` -- makes the id spellable, routed, labelled,
       policy-nameable, own-namespaced, and served on ``AcpRuntime``
       (``runtime=True``, the default: a ``DescriptorHarness`` only exists on path
       A). Its routing is the descriptor's, mapped to the enum;
       ``permission_config`` is passed only for a ``session_config`` descriptor,
       from the ``(option, value)`` the descriptor validated.
    2. ``register_operator_harness`` -- makes the id resolvable through
       ``harness_for`` as a ``DescriptorHarness`` built from this descriptor.
    3. ``register_selectable_backend`` -- ONLY when the descriptor is selectable (a
       recognized routing). An unroutable descriptor is left known-but-unselectable
       with its reason in :data:`_UNSELECTABLE`.

    Idempotent: an id already in ``ACP_BACKENDS_KNOWN`` is skipped, so a second
    bootstrap pass is a no-op rather than a re-registration crash.
    """
    valid, invalid = load_operator_descriptors(path=path)

    for harness_id, reasons in invalid:
        _INVALID[harness_id] = list(reasons)
        logger.warning("operator harness %r ignored: %s", harness_id, "; ".join(reasons))

    for descriptor in valid:
        backend_id = descriptor.id
        # Idempotent: a second bootstrap pass must not re-register (which raises).
        # Membership in the known set is the authoritative "already registered"
        # signal -- register_known_backend is what writes it, so an id present here
        # was already fully wired by an earlier pass.
        if backend_id in ACP_BACKENDS_KNOWN:
            continue

        routing_enum = _routing_enum_for(descriptor)
        # A descriptor with no recognized routing is still KNOWN (spellable,
        # nameable). It is registered as UNVERIFIED so register_selectable_backend
        # will refuse it, which is the visible-but-unselectable state.
        effective_routing = routing_enum if routing_enum is not None else Routing.UNVERIFIED
        permission_config = None
        if routing_enum is Routing.SESSION_CONFIG and descriptor.permission_config is not None:
            permission_config = (
                descriptor.permission_config.option,
                descriptor.permission_config.value,
            )

        try:
            register_known_backend(
                backend_id,
                label=descriptor.label,
                routing=effective_routing,
                permission_config=permission_config,
                model_namespace=backend_id,
                runtime=True,
            )
            register_operator_harness(descriptor)
        except ValueError:
            # A registration collision or malformed id that the descriptor layer
            # did not catch: record it as invalid and keep serving everything else.
            logger.warning("operator harness %r could not be registered", backend_id, exc_info=True)
            _INVALID.setdefault(backend_id, ["could not be registered"])
            continue

        if descriptor.selectable:
            try:
                register_selectable_backend(backend_id)
            except ValueError as exc:
                # Known but refused selectability -- record the reason and leave it
                # visible-but-unselectable rather than aborting the whole load.
                _UNSELECTABLE[backend_id] = str(exc)
                logger.warning(
                    "operator harness %r is known but not selectable: %s", backend_id, exc
                )
        else:
            _UNSELECTABLE[backend_id] = (
                "no recognized routing declared, so nothing establishes that its "
                "tool calls reach the host permission gate"
            )


def invalid_operator_harnesses() -> dict[str, list[str]]:
    """Descriptors that failed to parse/validate, id -> reasons (a copy).

    The ``invalid()`` shape, for the Settings surface: what is wrong with each entry
    an operator wrote that could not become a harness at all.
    """
    return {k: list(v) for k, v in _INVALID.items()}


def unselectable_operator_harnesses() -> dict[str, str]:
    """Valid-but-unselectable operator harnesses, id -> reason (a copy).

    Distinct from :func:`invalid_operator_harnesses`: these parsed cleanly and are
    spellable and nameable, but declared no verified routing, so they are visible in
    Settings with a reason rather than offered as a session backend.
    """
    return dict(_UNSELECTABLE)


def _reset_operator_diagnostics() -> None:
    """TEST-ONLY: clear the invalid/unselectable diagnostic maps.

    Paired with ``backends._reset_registered_backends`` and
    ``harness._reset_operator_register`` so one test's boot-load cannot leak its
    diagnostic rows into the next. Not called by product code -- the load runs once
    at boot.
    """
    _INVALID.clear()
    _UNSELECTABLE.clear()
