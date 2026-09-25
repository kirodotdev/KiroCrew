"""Operator-defined harnesses through the SDK boundary.

Application code -- the platform bootstrap that registers ``harnesses.json`` at
boot, and the dashboard handler that lists what the registry made of each
descriptor -- must not import the ACP layer directly
(``scripts/check_agent_sdk_boundary.py``). This module is the one crossing for
the operator-harness surface: three names, each a thin call into
:mod:`kiro_crew.acp.harness.operator_registry`, imported at call time so this
module stays as cheap to import as the rest of the SDK facade and adds no
module-scope edge into ``kiro_crew.acp`` (the registry module imports
``kiro_crew.acp.harness``, which in turn reaches the descriptor loader; pulling
that in at SDK import time would re-open the cycle the leaf
:mod:`kiro_crew.agent_sdk.backends` exists to avoid).
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, Optional


def load_and_register_operator_descriptors(
    *, path: "Optional[os.PathLike[str] | str]" = None
) -> None:
    """Read ``harnesses.json`` and register every valid, routable descriptor.

    Best-effort by contract: an unreadable file leaves the builtin harnesses
    serving. Invalid and unselectable descriptors are recorded for the
    diagnostics readers below rather than raised.
    """
    from kiro_crew.acp.harness.operator_registry import (
        load_and_register_operator_descriptors as _load,
    )

    _load(path=path)


def invalid_operator_harnesses() -> dict[str, list[str]]:
    """Descriptor id -> validation errors, for every descriptor that failed to load."""
    from kiro_crew.acp.harness.operator_registry import invalid_operator_harnesses as _invalid

    return _invalid()


def unselectable_operator_harnesses() -> dict[str, str]:
    """Descriptor id -> reason, for every valid descriptor the registry refused to
    make selectable (no honest permission routing declared)."""
    from kiro_crew.acp.harness.operator_registry import (
        unselectable_operator_harnesses as _unselectable,
    )

    return _unselectable()


def operator_backend_models(backend_id: str) -> "list[str] | None":
    """The model-name list a registered operator backend should offer, or
    ``None`` for a builtin / unknown id.

    Resolves the descriptor's ``model_source`` here so the caller (the
    ``/api/models`` endpoint) stays out of the ACP layer: a ``static`` descriptor
    yields its declared ``models``; an ``acp_advertised`` one yields what its
    harness advertised on ``session/new`` (the model registry's cross-session
    cache, keyed by the backend's own namespace). See
    :func:`kiro_crew.acp.harness.operator_registry.operator_backend_models`."""
    from kiro_crew.acp.harness.descriptor import MODEL_SOURCE_STATIC
    from kiro_crew.acp.harness.operator_registry import operator_backend_models as _models

    info = _models(backend_id)
    if info is None:
        return None
    source, static_models = info
    if source == MODEL_SOURCE_STATIC:
        return list(static_models)
    from kiro_crew import model_registry
    from kiro_crew.agent_sdk.backends import model_registry_namespace

    return list(model_registry.advertised_models(model_registry_namespace(backend_id)))


def unverified_operator_harnesses() -> frozenset[str]:
    """Ids of registered operator backends whose ONLY missing piece is the
    end-to-end routing attestation. These get a Verify action in Settings."""
    from kiro_crew.acp.harness.operator_registry import unverified_operator_harnesses as _unverified

    return _unverified()


def is_registered_operator_backend(backend_id: str) -> bool:
    """True when *backend_id* is a descriptor this loader registered (valid and
    routable, verified or not). A builtin, an invalid entry and an unroutable
    descriptor all answer False: none of them has a routing claim to probe."""
    from kiro_crew.acp.harness.operator_registry import registered_operator_descriptor

    return registered_operator_descriptor(backend_id) is not None


async def verify_operator_backend_routing(
    backend_id: str, cfg: Any, *, agent: str | None = None
) -> dict[str, Any]:
    """Probe *backend_id*'s harness end to end and, on a verified verdict, record
    the attestation and make the backend selectable now.

    *cfg* is the loaded ``KiroCrewConfig``; the probe builds the DESCRIPTOR's own
    provider from it -- ``AcpProvider(acp_backend=backend_id, ...)`` with the
    selected agent and the configured sandbox -- rather than calling the per-chat
    factory: an unverified descriptor is unselectable, so the factory's selection
    gate would degrade the pick to the configured default and the probe would run
    (and attest) the wrong backend. The provider's identity is asserted inside the
    probe before any verdict counts.

    *agent* is the agent the probe runs under; ``None`` means the configured
    default agent. The attestation is bound to that agent: a descriptor spawn for
    an agent the record does not name is refused, so a backend that should serve
    several agents is verified once per agent (each verified verdict adds its
    agent to the same record while the descriptor and binary are unchanged).

    Returns the verification as a plain dict (``verdict``, ``reason``, ...) plus
    ``selectable`` for the resulting registry state and ``agent`` for the agent
    probed. Raises ``ValueError`` for an id that is not a registered operator
    descriptor -- the caller turns that into its own 404, since a builtin has no
    routing claim to verify.
    """
    from kiro_crew.acp.harness.operator_registry import (
        POLICY_DENIED_REASON,
        mark_routing_verified,
        registered_operator_descriptor,
    )
    from kiro_crew.acp.harness.routing_verification import (
        VERDICT_INCONCLUSIVE,
        record_attestation,
        verify_routing,
    )
    from kiro_crew.agent_sdk.backends import selectable_backends

    descriptor = registered_operator_descriptor(backend_id)
    if descriptor is None:
        raise ValueError(f"{backend_id!r} is not a registered operator backend")

    agent_cfg = getattr(cfg, "agent", None)
    probe_agent = (agent if agent is not None else getattr(agent_cfg, "default_agent", "")) or ""

    def build_provider(session_key: str, cwd: str) -> Any:
        from kiro_crew.providers.acp import AcpProvider

        return AcpProvider(
            work_dir=cwd,
            agent=probe_agent or None,
            sandbox_mode=getattr(agent_cfg, "sandbox", "auto") or "auto",
            session_key=session_key,
            acp_backend=backend_id,
        )

    result = await verify_routing(descriptor, build_provider, agent=probe_agent)
    payload = result.as_dict()
    payload["selectable"] = False
    payload["agent"] = probe_agent
    if result.verified:
        # The probe resolved and digested the binary before it ran and re-checked
        # it after; the record is bound to THOSE bytes and to the agent the probe
        # ran under, and refused if the file has changed since. Digesting and
        # rewriting the store are file I/O -- off the loop, like every other
        # blocking call the gateway makes.
        try:
            record = await asyncio.to_thread(
                record_attestation,
                descriptor,
                mechanism=descriptor.routing,
                evidence={
                    "permission_requests": result.permission_requests,
                    "permission_request_classes": list(
                        result.details.get("permission_request_classes") or []
                    ),
                    "elapsed_secs": round(result.elapsed_secs, 2),
                },
                resolved_executable=result.details.get("executable_path"),
                expected_digest=result.details.get("executable_digest"),
                agent=probe_agent,
            )
        except ValueError as exc:
            payload["verdict"] = VERDICT_INCONCLUSIVE
            payload["reason"] = str(exc)
            return payload
        # Registry mutation stays on the loop (its readers are loop-side); the
        # governance narrowing is re-applied inside, so a policy-denied backend
        # comes back verified but not selectable, with the reason on its row.
        if not mark_routing_verified(backend_id, record):
            payload["policy_denied"] = True
            payload["reason"] = f"{payload['reason']}; {POLICY_DENIED_REASON}"
    payload["selectable"] = backend_id in selectable_backends()
    return payload


__all__ = [
    "invalid_operator_harnesses",
    "is_registered_operator_backend",
    "load_and_register_operator_descriptors",
    "operator_backend_models",
    "unselectable_operator_harnesses",
    "unverified_operator_harnesses",
    "verify_operator_backend_routing",
]
