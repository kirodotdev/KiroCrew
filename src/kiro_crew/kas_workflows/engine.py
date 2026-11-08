"""Which workflow engine serves a backend, asked of its capabilities.

Every branch between Crew's own ``ctx``-script engine and a harness's native
engine reads :func:`engine_for`, never the backend id, so a harness that later
brings its own engine joins by capability membership alone.
"""

from __future__ import annotations

from typing import Any

from kiro_crew.agent_sdk.capabilities import capabilities_for

#: Crew's own engine (:mod:`kiro_crew.workflows`). Serves every harness.
ENGINE_CREW = "crew"

#: The harness's native engine, driven over ``_kiro/workflow/*``.
ENGINE_KAS = "kas"

#: The one gateway setting that opts into Kiro Agent's native workflows.
KAS_WORKFLOWS_CONFIG_KEY = "agent.kas_workflows"


def engine_for(backend: str) -> str:
    """The engine that owns workflow definitions and runs for *backend*.

    Pure capability membership: this is what a backend COULD serve, not what is
    turned on. Whether the KAS surfaces actually run is :func:`kas_workflows_enabled`,
    which also reads the operator's opt-in.
    """
    return ENGINE_KAS if capabilities_for(backend).native_workflows else ENGINE_CREW


def backend_brings_kas_workflows(backend: str) -> bool:
    """Whether *backend* is a Kiro Agent backend that brings its own workflows.

    The capability half of the gate, independent of the opt-in — the settings UI
    reads it to explain whether the switch has any effect on this backend.
    """
    return engine_for(backend) == ENGINE_KAS


def kas_workflows_enabled(config: Any) -> bool:
    """THE single gate for every KAS-native workflow surface.

    True only when both halves hold: the agent backend is Kiro Agent (so there
    is a native engine to drive) AND the operator turned on ``agent.kas_workflows``.
    One gateway setting replaced the old per-device preview flag and the separate
    chat-workflows toggle, so every surface — the Workflows tab, the
    Task Runner, chat workflow tools, plan/question holds and boot recovery — asks
    this one question. Off on any doubt.
    """
    agent = getattr(config, "agent", None)
    if agent is None:
        return False
    return backend_brings_kas_workflows(getattr(agent, "acp_backend", "")) and bool(
        getattr(agent, "kas_workflows", False)
    )
