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
* :func:`kiro_crew.agent_sdk.backends.register_governed_backend` -- the
  selection side. Makes a ROUTED, attested id offerable in the backend switch,
  with the deployment's ``agent_backend`` verdict applied in the same step.

Ordering within one valid descriptor matters and is fixed here:
``register_known_backend`` FIRST (it records the routing the selection side then
reads), the harness registration, and ``register_governed_backend`` LAST and
ONLY when the descriptor is selectable. A descriptor with no recognized routing is
registered as KNOWN (spellable, nameable in a rule) but NOT selectable -- the
visible-but-unselectable row D3 wants, whose reason stays retrievable via
:func:`invalid_operator_harnesses` / :func:`unselectable_operator_harnesses` for the
future Settings surface.

Idempotence is a hard requirement: ``bootstrap_context`` can run more than once in
a process (``cli.main`` then ``run_gateway``), and re-registering a known id raises.
So :func:`load_and_register_operator_descriptors` SKIPS an id already registered,
which makes a second boot a no-op rather than a crash.

A note on lifecycle (D4, H13): this is NOT on the Kiro construction path. The
gateway runs it through ``operator_backends.register_operator_backends`` as a
contained background task AFTER ``boot_platform`` returns; ``bootstrap_context``
and the public edition's ``ProviderRegistry.register_acp_backends`` never call it.
Two consequences the loader is written for. First, the gateway's config instance
was loaded before any operator id was registered, so its ``agent.acp_backend``
was coerced against a registry without them; the caller re-resolves that field
from the spelling the load kept (``acp_backend_persisted``) once this returns.
Second, the boot-time ``agent_backend`` governance pass has already run, so each
descriptor is registered with the deployment's verdict applied in the same step
(``policy_permits`` + ``register_governed_backend``): a policy-denied descriptor
is known and visible-but-unselectable with :data:`POLICY_DENIED_REASON` and is
never selectable for even the instant between its registration and a recompute --
which matters because the attestation check between two descriptors digests a
binary, so that instant could be long.

A note on freshness: this reads ``harnesses.json`` ONCE, at boot. An edit to that
file takes effect on the next gateway start, not live -- the same restart the
docstring of every boot-time registration implies, and stated for the operator in
the module docstring of :mod:`kiro_crew.acp.harness.descriptor`.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Mapping, Optional

from kiro_crew.acp.harness import register_operator_harness
from kiro_crew.acp.harness.descriptor import (
    ROUTING_AGENT_SPEC,
    ROUTING_SESSION_CONFIG,
    HarnessDescriptor,
    load_operator_descriptors,
)
from kiro_crew.acp.harness.routing_verification import (
    UNVERIFIED_REASON,
    descriptor_fingerprint,
    is_attested,
)
from kiro_crew.agent_sdk.backends import (
    Routing,
    register_governed_backend,
    register_known_backend,
)

logger = logging.getLogger(__name__)

#: Descriptors that FAILED to parse/validate, keyed by the id they were filed
#: under, mapped to their diagnosable reasons. The ``invalid()`` shape the salvage
#: registry carried, kept for the future Settings surface: a malformed entry costs
#: its row, never boot, and the row stays retrievable so an operator can be shown
#: what is wrong. Populated by :func:`load_and_register_operator_descriptors`.
_INVALID: dict[str, list[str]] = {}

#: Valid descriptors that registered as KNOWN but NOT SELECTABLE -- their routing
#: was absent, unrecognized, or declared but not yet verified end to end -- keyed
#: by id and mapped to the reason. Distinct from :data:`_INVALID`: these parsed
#: cleanly and can be spelled and named in a rule; they simply cannot be offered
#: as a session backend. The visible-but-unselectable row D3 wants.
_UNSELECTABLE: dict[str, str] = {}

#: The subset of :data:`_UNSELECTABLE` whose ONLY missing piece is the routing
#: attestation (``routing_verification``): a recognized routing was declared, the
#: descriptor registered as known and runnable, and a successful probe makes it
#: selectable without a restart (:func:`mark_routing_verified`). The listing
#: offers these a Verify action; the unroutable rest get none.
_UNVERIFIED: set[str] = set()


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

    Called once per gateway start from the additive post-boot step
    (``operator_backends.register_operator_backends``, a background task the
    gateway never awaits on its boot path), after the platform booted and its
    governance pass ran; a CLI command or an app server booting the platform never
    calls it (H13). Never raises:
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
    3. ``register_governed_backend`` -- ONLY when the descriptor is selectable (a
       recognized routing) AND attested, with the deployment's ``agent_backend``
       verdict (``policy_permits``) applied in the same step, so a policy-denied
       descriptor joins the baseline but never the selectable set. An unroutable,
       unverified or policy-denied descriptor is left known-but-unselectable with
       its reason in :data:`_UNSELECTABLE`.

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
        # The operator register is the authoritative "already registered BY THIS
        # LOADER" signal -- register_operator_harness is what writes it. The known
        # set is NOT that signal: a builtin's id is in it from module import, so a
        # descriptor that names ``kiro`` or ``claude`` would read as "already done"
        # and vanish without a diagnosable row. That collision falls through to the
        # registrar below, which refuses it, and the refusal is recorded invalid.
        if _is_registered_operator_id(backend_id):
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

        # The label is not only a display name: ``provider_label`` persists a
        # session under it, ``detect_provider_switch`` compares it, and cleanup
        # routes on it (harness-parity H11). A label another backend already
        # answers to would file this backend's sessions under that backend --
        # ``"acp"`` would make them kiro sessions and get them pruned. Refused here,
        # where both the builtin mapping and the registered labels are visible. The
        # reason names the FIELD, never the label or the other backend: both are
        # operator text on a listing every authenticated user can read.
        taken_by = _label_owner(descriptor.label)
        if taken_by is not None:
            _INVALID.setdefault(
                backend_id,
                [
                    "display_name is already the provider label of another backend; "
                    "sessions are persisted and cleaned up by label, so two backends "
                    "cannot share one"
                ],
            )
            continue

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
        except ValueError as exc:
            # A registration collision (an id a builtin already serves) or a
            # malformed id the descriptor layer did not catch: record it invalid with
            # the registrar's own reason so the row is diagnosable, and keep serving
            # everything else.
            logger.warning("operator harness %r could not be registered", backend_id, exc_info=True)
            _INVALID.setdefault(backend_id, [f"could not be registered: {exc}"])
            continue

        if not descriptor.selectable:
            _UNSELECTABLE[backend_id] = (
                "no recognized routing declared, so nothing establishes that its "
                "tool calls reach the host permission gate"
            )
            continue
        # A declared routing is a CLAIM. Selectability needs the gateway's own
        # evidence that the harness asks before it acts: an attestation this
        # gateway recorded after an end-to-end probe (routing_verification), keyed
        # by the descriptor's spawn fingerprint so an edit to the binary, argv,
        # routing or option revokes it by construction. Without one the backend is
        # known and runnable-for-verification, but no chat can pick it.
        if not is_attested(descriptor):
            _UNSELECTABLE[backend_id] = UNVERIFIED_REASON
            _UNVERIFIED.add(backend_id)
            continue
        _register_governed(backend_id)


def _register_governed(backend_id: str) -> bool:
    """Register *backend_id* selectable with the deployment's verdict applied atomically.

    True when it is selectable afterwards; False when the ``agent_backend`` policy
    denies it (it is in the baseline, so a loosened policy restores it, and never
    in the selectable set), with :data:`POLICY_DENIED_REASON` recorded as the row's
    reason. A registrar refusal (unknown id, ``UNVERIFIED`` routing) is recorded as
    the row's reason and answers False as well.
    """
    from kiro_crew.agent_backend_governance import policy_permits

    permitted = policy_permits(backend_id)
    try:
        register_governed_backend(backend_id, permitted=permitted)
    except ValueError as exc:
        # Known but refused selectability -- record the reason and leave it
        # visible-but-unselectable rather than aborting the whole load.
        _UNSELECTABLE[backend_id] = str(exc)
        logger.warning("operator harness %r is known but not selectable: %s", backend_id, exc)
        return False
    if not permitted:
        _UNSELECTABLE[backend_id] = POLICY_DENIED_REASON
    return permitted


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


def unverified_operator_harnesses() -> frozenset[str]:
    """Ids whose only missing piece is the end-to-end routing attestation.

    A subset of :func:`unselectable_operator_harnesses`'s keys. These are the
    rows Settings offers a Verify action for; an unroutable descriptor (no
    recognized routing) is not among them because there is nothing to verify.
    """
    return frozenset(_UNVERIFIED)


def registered_operator_descriptor(backend_id: str) -> Optional[HarnessDescriptor]:
    """The registered descriptor for *backend_id*, or ``None`` for a builtin/unknown."""
    from kiro_crew.acp.harness import _OPERATOR_REGISTER

    return _OPERATOR_REGISTER.get(backend_id)


#: The reason recorded for a verified backend the deployment's ``agent_backend``
#: policy denies: verified routing is necessary for selectability, not sufficient.
POLICY_DENIED_REASON = (
    "routing verified, but this deployment's agent_backend policy does not permit "
    "the backend, so it is not selectable"
)


def mark_routing_verified(backend_id: str, record: Mapping[str, Any]) -> bool:
    """Project a just-written attestation into the live registry. True when the
    backend is selectable afterwards; False when the deployment's policy denies it.

    The boot-time gate reads the attestation file; this is the live half so a
    successful Verify in Settings does not need a gateway restart. *record* is the
    attestation :func:`routing_verification.record_attestation` just wrote (the
    caller ran that off the event loop); this function does no file I/O of its
    own, and it refuses an id that is not a registered operator descriptor or a
    record whose fingerprint is not the live descriptor's.

    Registration widens the baseline, and the ``agent_backend`` governance
    pass that ran at boot has not seen this id, so the deployment's verdict is
    applied IN the registration (``policy_permits`` + ``register_governed_backend``)
    rather than by a recompute after it: an administrator's denial holds for a
    backend the owner verified after that boot, and there is no instant in which
    the denied id is selectable. A policy-denied id stays unselectable with
    :data:`POLICY_DENIED_REASON` and is NOT offered for verification again --
    verification is not what it lacks.
    """
    descriptor = registered_operator_descriptor(backend_id)
    if descriptor is None:
        raise ValueError(f"{backend_id!r} is not a registered operator backend")
    if record.get("fingerprint") != descriptor_fingerprint(descriptor):
        raise ValueError(
            f"the attestation does not match the current descriptor for {backend_id!r}"
        )
    _UNVERIFIED.discard(backend_id)
    if _register_governed(backend_id):
        _UNSELECTABLE.pop(backend_id, None)
        return True
    return False


def revoke_routing_verification(backend_id: str, reason: str) -> None:
    """Take an operator backend OUT of the selectable set now, with *reason*.

    Called when the executable a spawn is about to exec does not match the
    verified digest (``routing_verification.spawn_attestation_problem``): the
    attestation on disk is dropped, the id leaves the selectable registry, and
    Settings shows the row as unverified with the reason and a Verify action. A
    builtin or unknown id is refused -- only registered descriptors carry a
    verification to revoke.

    SYNCHRONOUS, including the store rewrite (a JSON read-modify-write under the
    store lock, with the Windows rename backoff ``atomic_write`` carries), so it
    is for a caller that is already off the event loop or has no loop. The spawn
    path runs on the loop and uses :func:`revoke_routing_verification_async`,
    which offloads the disk half and applies the registry half here.
    """
    _require_registered_for_revoke(backend_id)
    _revoke_attestation_on_disk(backend_id)
    _withdraw_routing_verification(backend_id, reason)


async def revoke_routing_verification_async(backend_id: str, reason: str) -> None:
    """:func:`revoke_routing_verification` for a caller ON the event loop.

    The attestation store rewrite is file I/O -- an open, a parse, an atomic
    replace with its rename backoff -- and on slow storage it stalls the loop,
    which is what the spawn path must never do. So the disk half runs in a worker
    thread, and the registry half (module-level sets other loop code reads) is
    applied on the loop afterwards, so no reader sees a half-withdrawn id. The
    registry is withdrawn even when the store rewrite raises: the spawn already
    found bytes that do not match the attestation, and a registry that kept the
    backend selectable while the store held a stale grant would be the worse
    state; the error is logged and the next verify rewrites the store.
    """
    _require_registered_for_revoke(backend_id)
    try:
        await asyncio.to_thread(_revoke_attestation_on_disk, backend_id)
    except OSError:
        logger.warning(
            "routing attestation for %r could not be removed from the store; the "
            "backend is withdrawn from the selectable set regardless",
            backend_id,
            exc_info=True,
        )
    _withdraw_routing_verification(backend_id, reason)


def _require_registered_for_revoke(backend_id: str) -> None:
    if registered_operator_descriptor(backend_id) is None:
        raise ValueError(f"{backend_id!r} is not a registered operator backend")


def _revoke_attestation_on_disk(backend_id: str) -> None:
    """The disk half of a revoke: drop the attestation record. File I/O."""
    from kiro_crew.acp.harness.routing_verification import revoke_attestation

    revoke_attestation(backend_id)


def _withdraw_routing_verification(backend_id: str, reason: str) -> None:
    """The registry half of a revoke: in-memory only, no I/O."""
    from kiro_crew.agent_sdk.backends import unregister_selectable_backend

    unregister_selectable_backend(backend_id)
    _UNSELECTABLE[backend_id] = f"{reason}; {UNVERIFIED_REASON}"
    _UNVERIFIED.add(backend_id)


def operator_backend_models(backend_id: str) -> "tuple[str, tuple[str, ...]] | None":
    """A registered operator backend's ``(model_source, models)``, or ``None``.

    ``None`` when ``backend_id`` names no registered operator descriptor (a
    builtin, or nothing). For a static descriptor ``models`` is its declared
    catalog; for an ``acp_advertised`` one ``models`` is empty and the caller
    reads the live/cached advertised list from the model registry instead. This
    is the read the ``/api/models`` endpoint uses so an operator backend's picker
    offers ITS models rather than falling through to kiro-cli's ``--list-models``
    catalog (which the descriptor's harness would reject).
    """
    from kiro_crew.acp.harness import _OPERATOR_REGISTER

    descriptor = _OPERATOR_REGISTER.get(backend_id)
    if descriptor is None:
        return None
    return (descriptor.model_source, tuple(descriptor.models))


def _is_registered_operator_id(backend_id: str) -> bool:
    """True when an earlier bootstrap pass registered ``backend_id`` from a descriptor.

    Reads the operator register -- the table :func:`register_operator_harness`
    writes -- rather than ``ACP_BACKENDS_KNOWN``, which also holds every builtin
    from module import and so cannot distinguish "wired by a prior pass" from "a
    descriptor colliding with a builtin id". Deferred import: the harness package
    imports this module.
    """
    from kiro_crew.acp.harness import _OPERATOR_REGISTER

    return backend_id in _OPERATOR_REGISTER


def _label_owner(label: str) -> Optional[str]:
    """The backend id already answering to *label*, or ``None`` when it is free.

    Checks the builtin mapping (``PROVIDER_LABEL_BY_BACKEND``, which also carries
    kiro's DEFAULT label) and then every registered id's recorded label. Deferred
    import: ``acp.types`` is above the vocabulary leaf this module builds on.
    """
    from kiro_crew.acp.types import PROVIDER_LABEL_BY_BACKEND
    from kiro_crew.agent_sdk.backends import known_backends, provider_label_for

    for builtin_id, builtin_label in PROVIDER_LABEL_BY_BACKEND.items():
        if builtin_label == label:
            return builtin_id
    # ``provider_label_for`` answers only for registered ids (empty for a
    # builtin), so walking the whole known set visits exactly the registered ones.
    for known_id in known_backends():
        if provider_label_for(known_id) == label:
            return known_id
    return None


def _reset_operator_diagnostics() -> None:
    """TEST-ONLY: clear the invalid/unselectable diagnostic maps.

    Paired with ``backends._reset_registered_backends`` and
    ``harness._reset_operator_register`` so one test's boot-load cannot leak its
    diagnostic rows into the next. Not called by product code -- the load runs once
    at boot.
    """
    _INVALID.clear()
    _UNSELECTABLE.clear()
    _UNVERIFIED.clear()
