"""Trusted bridge between organization records and existing member storage."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from kiro_crew.organization import OWNER, ROLES, OrganizationError, OrganizationStore


def owner_chat_request(state: Any, session_key: str) -> str:
    """Resolve gateway-only, live owner provenance after private-caller auth."""
    if session_key:
        for slot in state._slots.values():
            grant = getattr(slot, "_organization_owner_request", None)
            if (
                grant
                and grant[0] == session_key
                and getattr(slot, "_active_turn_session_key", "") == session_key
                and slot.running
            ):
                return str(grant[1])
    raise OrganizationError(
        "owner_chat_required",
        "Start a new task during a direct chat request from the owner. "
        "For automated wakes, continue the assignments already in your inbox.",
        403,
    )


def runtime_status() -> dict[str, Any]:
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.members import select_provider_backend
    from kiro_crew.organization_policy import require_runtime

    cfg = KiroCrewConfig.load()
    backend = select_provider_backend(
        "member-organization", cfg.agent.member_acp_backend, cfg.agent.acp_backend
    )
    try:
        require_runtime(backend=backend, session_key="member-organization")
        return {"ready": True, "backend": backend, "reason": ""}
    except (ValueError, OSError) as exc:
        return {"ready": False, "backend": backend, "reason": str(exc)}


def verify_member(member: dict[str, Any]) -> None:
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.memory_stores import require_member_memory_store

    cfg = KiroCrewConfig.load()
    if require_member_memory_store(cfg, member["name"]) != member["memory_store"]:
        raise OrganizationError(
            "member_binding_changed",
            "The member's private memory binding changed. Restore it before running this team.",
        )


def create_member(
    store: OrganizationStore,
    actor: str,
    *,
    role: str,
    manager_id: str | None,
    name: str = "",
    reservation: str = "",
) -> str:
    """Provision private memory, protect identity, then publish the config delta.

    The async caller holds the existing config mutation lock and drains this
    operation on cancellation. Partial failures retain a refused identity and
    its memory; they never turn into an ordinary unguarded member.
    """
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.config.sections import KiroCrewAgentConfig
    from kiro_crew.member_memory_auth import require_member_memory_creation
    from kiro_crew.memory_stores import (
        persist_member_config,
        provision_member_memory,
        retire_unpublished_member_memory_store,
    )
    from kiro_crew.validation import _AGENT_NAME_RE

    if role not in ROLES:
        raise OrganizationError("invalid_role", "Choose a supported role.", 400)
    cfg = KiroCrewConfig.load()
    if not name:
        suffix = (reservation or uuid.uuid4().hex)[:8]
        name = f"{role.title()}-{suffix}"
    if not isinstance(name, str) or len(name) > 128 or not _AGENT_NAME_RE.fullmatch(name):
        raise OrganizationError("invalid_name", "Choose a valid member name.", 400)
    if name in cfg.agents:
        raise OrganizationError("member_exists", "A member with this name already exists.")
    require_member_memory_creation(name)
    workspace = "default"
    if manager_id:
        parent = next(
            (member for member in store.snapshot()["members"] if member["id"] == manager_id), None
        )
        if parent is None or parent["name"] not in cfg.agents:
            raise OrganizationError("manager_not_found", "Choose an existing manager.", 404)
        workspace = cfg.agents[parent["name"]].workspace
    cfg.agents[name] = KiroCrewAgentConfig(
        kiro_agent="kirocrew",
        workspace=workspace,
        description=f"Organization {role}",
        source="kirocrew",
        model="auto",
        triggers="",
    )
    memory_store = provision_member_memory(cfg, name)
    member_id = ""
    try:
        member_id = store.enroll(
            actor,
            name=name,
            memory_store=memory_store,
            role=role,
            manager_id=manager_id,
            reservation=reservation,
            provisioning=True,
        )
        persist_member_config(cfg, name, create=True)
        store.finish_provisioning(member_id, succeeded=True)
    except BaseException:
        if member_id:
            store.finish_provisioning(member_id, succeeded=False)
        # Archive only an allocation which config did not publish.
        current = KiroCrewConfig.load()
        if name not in current.agents or current.agents[name].memory_store != memory_store:
            retire_unpublished_member_memory_store(memory_store, name)
        raise
    return member_id


async def create_member_async(
    store: OrganizationStore,
    actor: str,
    *,
    role: str,
    manager_id: str | None,
    name: str = "",
    reservation: str = "",
) -> str:
    from kiro_crew.dashboard.handlers.agents import _drained_to_thread, _get_config_lock

    async with _get_config_lock():
        return await _drained_to_thread(
            lambda: create_member(
                store, actor, role=role, manager_id=manager_id, name=name, reservation=reservation
            )
        )


async def hire(store: OrganizationStore, actor: str, role: str) -> dict[str, str]:
    reservation = await asyncio.to_thread(store.reserve_hire, actor, role)
    if reservation["member_id"]:
        return {"member_id": reservation["member_id"], "outcome": "reused"}
    try:
        member_id = await create_member_async(
            store, actor, role=role, manager_id=actor, reservation=reservation["reservation"]
        )
        return {"member_id": member_id, "outcome": "created"}
    except BaseException:
        from kiro_crew.dashboard.handlers.agents import _drained_to_thread

        await _drained_to_thread(store.abandon_hire, reservation["reservation"])
        raise


async def apply_action(
    store: OrganizationStore, actor: str, action: str, values: dict[str, Any]
) -> dict[str, Any]:
    """Shared actions after owner or private-caller authentication."""
    if action == "hire":
        return await hire(store, actor, values["role"])
    if action == "assign":
        task_id = await asyncio.to_thread(
            store.assign,
            actor,
            values["recipient"],
            title=values["title"],
            acceptance=values["acceptance"],
            parent_id=values.get("parent_id"),
        )
        return {"task_id": task_id}
    if action == "message":
        message_id = await asyncio.to_thread(
            store.message, actor, values["recipient"], values["text"]
        )
        return {"message_id": message_id}
    if action == "report":
        await asyncio.to_thread(
            store.report, actor, values["task_id"], values["status"], values["text"]
        )
    elif action == "review":
        await asyncio.to_thread(
            store.review, actor, values["task_id"], values["verdict"], values["text"]
        )
    elif action == "retry":
        await asyncio.to_thread(store.retry, actor, values["member_id"])
    elif action == "create_member" and actor == OWNER:
        member_id = await create_member_async(
            store,
            actor,
            role=values["role"],
            manager_id=values.get("manager_id"),
            name=values.get("name", ""),
        )
        return {"member_id": member_id}
    elif action == "configure" and actor == OWNER:
        if values["enabled"]:
            status = await asyncio.to_thread(runtime_status)
            if not status["ready"]:
                raise OrganizationError("organization_runtime_unavailable", status["reason"])
        await asyncio.to_thread(
            store.configure,
            actor,
            revision=values["revision"],
            concurrency=values["concurrency"],
            enabled=values["enabled"],
            staffing=values["staffing"],
        )
    elif action == "move_member" and actor == OWNER:
        await asyncio.to_thread(
            store.move_member, actor, values["member_id"], values.get("manager_id")
        )
    elif action == "retire" and actor == OWNER:
        await asyncio.to_thread(store.retire, actor, values["member_id"])
    else:
        raise OrganizationError("unknown_action", "This organization action is not available.", 400)
    return {"ok": True}
