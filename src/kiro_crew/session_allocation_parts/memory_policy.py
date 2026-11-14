"""Prepare a cold-started provider's KAS memory request from its session context."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from kiro_crew.acp_backends import ACP_BACKEND_KAS

if TYPE_CHECKING:
    from kiro_crew.agent_sdk.kas_memory import MemoryPlan
    from kiro_crew.execution_context import ExecutionContext
    from kiro_crew.session_allocation import _AllocationOwner


async def configure_provider_memory(
    provider: Any,
    *,
    owner: _AllocationOwner,
    session_key: str,
    execution: ExecutionContext | None,
    channel_id: str | None,
    crew_agent: str,
) -> MemoryPlan:
    """Set the provider's memory mode and return whether its saved session must rotate."""
    # The agent SDK loads ACP transports that depend on the session layer.
    from kiro_crew.agent_sdk import kas_memory

    kas_backend = bool(getattr(provider, "is_kas_backend", False))
    cloud_session = kas_backend and await asyncio.to_thread(_cloud_session, session_key)
    personal_execution = (
        kas_backend
        and not cloud_session
        and await asyncio.to_thread(_personal_execution, session_key, execution)
    )
    plan = kas_memory.memory_plan(
        setting_on=owner._cfg.agent.orchestrator_memory is True,
        kas_backend=kas_backend,
        cloud_session=cloud_session,
        personal_session=(
            personal_execution
            and not channel_id
            and not crew_agent
            and not any(owner._session_map.get_slack_link(session_key))
            and owner._session_map.get_mirror_link(session_key) is None
            and not owner._session_map.mirror_accepts_inbound(session_key)
        ),
        session_key=session_key,
        session_memory_mode=provider.memory_mode,
        owning_app=provider.owning_app,
        recorded_mode=owner._session_map.get_kas_memory(session_key),
    )
    provider.kas_memory_mode = plan.mode
    provider.kas_memory_new_mode = plan.mode
    return plan


def warm_pool_requires_cold_start(owner: _AllocationOwner) -> bool:
    """Whether session-specific KAS memory makes a precreated provider unusable."""
    return bool(
        owner._cfg.agent.orchestrator_memory is True
        and owner._cfg.agent.acp_backend == ACP_BACKEND_KAS
    )


def _cloud_session(session_key: str) -> bool:
    """Read placement before a memory grant could rotate a cloud session."""
    from kiro_crew.coder.session_launch import KIRO_CLOUD_PROVIDER_ID, SessionEnvironmentChoices
    from kiro_crew.session_environment import choices_path, session_environments_enabled

    if not session_environments_enabled():
        return False
    choice = SessionEnvironmentChoices(choices_path()).resolve_choice(session_key)
    return choice is not None and choice[2] == KIRO_CLOUD_PROVIDER_ID


def _personal_execution(session_key: str, execution: ExecutionContext | None) -> bool:
    """Withhold recall from member identities, including ones a record hides."""
    from kiro_crew._durable_vouch import durable_vouch_path
    from kiro_crew.execution_context import read_vouched_session_execution
    from kiro_crew.members import is_crewmate_worker_session

    if execution is None:
        return False
    if execution.member_id is not None or execution.selection_kind != "template" or execution.app:
        return False
    if is_crewmate_worker_session(session_key):
        return False
    # Protected gateway facts survive a member rewriting its transcript as an owner.
    if read_vouched_session_execution(session_key) is not None:
        return False
    try:
        durable_vouch_path(session_key).lstat()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return False
