"""Persistent app members and their assignments on the gateway session manager."""

from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kiro_crew.config import KiroCrewConfig
from kiro_crew.config.sections import KiroCrewAgentConfig
from kiro_crew.execution_context import member_config_for_id
from kiro_crew.memory_stores import (
    persist_member_config,
    provision_member_memory,
    require_member_memory_store,
    retire_unpublished_allocation,
)
from kiro_crew.workflows.registry import _await_owned

from . import store


@dataclass(frozen=True)
class Role:
    name: str
    template: str
    description: str


ROLES = {
    "discovery": Role(
        "auto-improvement-scout",
        "auto-improvement-scout",
        "Finds concrete improvement candidates and hands evidence to the Engineer.",
    ),
    "implementation": Role(
        "auto-improvement-engineer",
        "auto-improvement-engineer",
        "Implements the Scout's candidates; deterministic tests decide what is kept.",
    ),
}
_team_lock = threading.Lock()


def _team_path() -> Path:
    return store.data_dir() / "crew.json"


def _read_team() -> dict[str, str]:
    path = _team_path()
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or any(
        role not in ROLES or not isinstance(identity, str) or not identity
        for role, identity in data.items()
    ):
        raise ValueError("Auto-Improvement member identities are unreadable")
    return data


def resolve_role(role: str, identity: str):
    """Resolve by immutable ID so a member rename cannot move an assignment."""
    config = KiroCrewConfig.load()
    name, member = member_config_for_id(config, identity)
    if member.source != store.APP_NAME or member.kiro_agent != ROLES[role].template:
        raise ValueError(f"The Auto-Improvement {role} member has a different template or owner")
    require_member_memory_store(config, name)
    return config, name, member


def ensure_team() -> dict[str, str]:
    """Create missing roles once; retain member edits, IDs, rules and memory."""
    with _team_lock:
        identities = _read_team()
        for role, spec in ROLES.items():
            if role in identities:
                resolve_role(role, identities[role])
                continue
            config = KiroCrewConfig.load()
            existing = config.agents.get(spec.name)
            if existing is None:
                member = KiroCrewAgentConfig(
                    kiro_agent=spec.template,
                    description=spec.description,
                    source=store.APP_NAME,
                )
                config.agents[spec.name] = member
                previous_store = member.memory_store
                previous_id = member.member_id
                try:
                    provision_member_memory(config, spec.name)
                    persist_member_config(config, spec.name, create=True)
                except BaseException:
                    if member.memory_store != previous_store:
                        retire_unpublished_allocation(
                            config,
                            spec.name,
                            member.memory_store,
                            previous_store=previous_store,
                            previous_member_id=previous_id,
                        )
                    raise
                existing = member
            if existing.source != store.APP_NAME or existing.kiro_agent != spec.template:
                raise ValueError(f"Crew Member {spec.name!r} already belongs to another purpose")
            require_member_memory_store(config, spec.name)
            if not existing.member_id:
                raise ValueError(f"Crew Member {spec.name!r} has no private member identity")
            identities[role] = existing.member_id
            store.write_json_atomic(_team_path(), identities)
        return identities


@dataclass(frozen=True)
class GatewayRuntime:
    sessions: Any
    context_builder: Any
    loop: asyncio.AbstractEventLoop


_runtime: GatewayRuntime | None = None


def attach_gateway(state: Any) -> None:
    global _runtime
    if state is not None and state.sessions is not None and state.context_builder is not None:
        _runtime = GatewayRuntime(state.sessions, state.context_builder, asyncio.get_running_loop())


async def on_startup(ctx: Any) -> None:
    # Startup and enable share this hook. Provisioning is off the gateway loop.
    await _await_owned(asyncio.create_task(asyncio.to_thread(ensure_team)))


async def on_shutdown(ctx: Any) -> None:
    from .runner import get_supervisor

    await _await_owned(asyncio.create_task(asyncio.to_thread(get_supervisor().stop)))


def build_runner(*, stop_check, on_activity):
    # The provider stack is deliberately lazy: this module is also a boot hook.
    from ..spine.crew_runner import CrewRunner

    if _runtime is None or not _runtime.loop.is_running():
        raise RuntimeError("Auto-Improvement requires the gateway's member session runtime")
    return CrewRunner(_runtime, ensure_team(), stop_check=stop_check, on_activity=on_activity)
