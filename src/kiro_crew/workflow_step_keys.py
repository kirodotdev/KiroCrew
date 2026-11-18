"""Session-key prefixes of the sessions a workflow run opens for its own agents."""

from __future__ import annotations

#: The session-key prefix of a workflow run's warm pool workers
#: (``workflows.agent_pool``). One worker serves several steps of its run in turn.
WORKFLOW_POOL_KEY_PREFIX = "wf-pool:"

#: The session-key prefix of a workflow authoring session.
WORKFLOW_AUTHOR_KEY_PREFIX = "wf-author:"

#: Session-key prefixes of a workflow RUN's step sessions, each followed by
#: ``<run_id>:``: a per-call step and a named chain without a memory scope
#: (``wf:``), a pool worker, an unpooled overflow step and a memory-scoped
#: named chain (``wf-worker:``).
WORKFLOW_RUN_STEP_KEY_PREFIXES: tuple[str, ...] = (
    "wf:",
    WORKFLOW_POOL_KEY_PREFIX,
    "wf-unpooled:",
    "wf-worker:",
)

#: Session-key prefixes of the sessions a workflow run opens for its own agents:
#: the run's step sessions and the authoring session. The workflow service mints
#: each one; no caller supplies it.
WORKFLOW_STEP_KEY_PREFIXES: tuple[str, ...] = (
    *WORKFLOW_RUN_STEP_KEY_PREFIXES,
    WORKFLOW_AUTHOR_KEY_PREFIX,
)


def is_workflow_step_key(key: str) -> bool:
    """Whether *key* names a session a workflow run opened for one of its agents."""
    return isinstance(key, str) and key.startswith(WORKFLOW_STEP_KEY_PREFIXES)


def is_workflow_pool_key(key: str) -> bool:
    """Whether *key* names a pool worker, which the next step of its run reuses."""
    return isinstance(key, str) and key.startswith(WORKFLOW_POOL_KEY_PREFIX)
