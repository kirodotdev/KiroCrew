"""Full-spec adoption belongs to a fresh member runtime, never to a save."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kiro_crew.session_allocation import SessionRegistryState

from kiro_crew import agent_state
from kiro_crew.agent_capabilities import (
    prepare_member_capabilities,
    reconcile_member_capabilities,
)
from kiro_crew.config import KiroCrewConfig
from kiro_crew.config.loader import default_project_dir, resolve_crew_identity
from kiro_crew.platform.governance_profiles import governance_answer_generation


@dataclass(frozen=True)
class CapabilityPreparation:
    member: str
    template: str = ""
    revision: str = ""
    project: str = ""
    governance_generation: object = None
    mcp_servers: tuple[str, ...] = ()


@dataclass(frozen=True)
class LoadedCapabilities:
    member: str
    template: str
    revision: str
    process_instance: str
    session_id: str
    governance_generation: object
    mcp_servers: tuple[str, ...] = ()


class CapabilityStartupError(RuntimeError):
    """A fresh runtime could not prove it loaded the selected member spec."""


def prepare_runtime(
    agent: str | None, crew_agent: str | None, cwd: str | None
) -> CapabilityPreparation:
    """Off-loop cold-start preparation; do not call for an existing session."""
    cfg = KiroCrewConfig.load()
    member = resolve_crew_identity(cfg, agent, crew_agent)
    if not member:
        return CapabilityPreparation("")
    binding = cfg.agents.get(member)
    if binding is None:
        raise CapabilityStartupError("capability_member_missing")
    try:
        intent = agent_state.get_capabilities(binding.kiro_agent)
    except (OSError, ValueError):
        # Enrollment lives in the one shared sidecar. An unreadable file
        # cannot prove this member is NOT enrolled, so inferring legacy mode
        # would start an enrolled member on the shared warm process. Refuse
        # with a closed code; non-crew sessions never reach this read.
        raise CapabilityStartupError("capability_state_unreadable") from None
    if intent is None:
        return CapabilityPreparation(member)
    reconcile_member_capabilities(member)
    if not cwd:
        cfg = KiroCrewConfig.load()
        cwd = default_project_dir(cfg.agents[member].workspace)
    prepared = prepare_member_capabilities(member, cwd)
    if not prepared.get("revision") or not prepared.get("template"):
        raise CapabilityStartupError("capability_generation_missing")
    return CapabilityPreparation(
        member,
        prepared["template"],
        prepared["revision"],
        str(Path(cwd).resolve()) if cwd else "",
        governance_answer_generation(),
        tuple(prepared.get("mcp_servers", ())),
    )


def verify_saved(prepared: CapabilityPreparation, cwd: str) -> None:
    """Recheck saved bytes, ownership, cwd and governance after startup off-loop."""
    actual_project = str(Path(cwd).resolve()) if cwd else ""
    if actual_project != prepared.project:
        raise CapabilityStartupError("capability_runtime_cwd_changed")
    current = prepare_member_capabilities(prepared.member, cwd)
    if (
        current["template"] != prepared.template
        or current["revision"] != prepared.revision
        or governance_answer_generation() != prepared.governance_generation
    ):
        raise CapabilityStartupError("capability_startup_raced")


def loaded_stamp(provider: Any, prepared: CapabilityPreparation) -> LoadedCapabilities:
    """Only an observed active template on a dedicated new process is evidence."""
    if (
        provider.member_capabilities_supported is not True
        or provider.loaded_capability_template != prepared.template
        or not provider.process_instance
        or not provider.session_id
        or not provider.is_process_alive()
        or governance_answer_generation() != prepared.governance_generation
    ):
        raise CapabilityStartupError("capability_runtime_unverified")
    return LoadedCapabilities(
        prepared.member,
        prepared.template,
        prepared.revision,
        provider.process_instance,
        provider.session_id,
        prepared.governance_generation,
        prepared.mcp_servers,
    )


def runtime_view(state: SessionRegistryState, member: str, saved_revision: str) -> dict[str, Any]:
    """Project allocation-owned state on the event loop; no IO or mutation."""
    rows = []
    for key, session in list(state.sessions.items()):
        if session.capability_member != member:
            continue
        stamp = session.loaded_capabilities
        provider = session.provider
        status = "pending"
        error_code = ""
        if stamp is not None and stamp.revision == saved_revision:
            status = "unverified"
            if (
                provider.is_process_alive()
                and provider.process_instance == stamp.process_instance
                and provider.session_id == stamp.session_id
                and provider.loaded_capability_template == stamp.template
                and governance_answer_generation() == stamp.governance_generation
            ):
                status = "applied"
                report = provider.mcp_session_report()
                payload = report.payload() if report is not None else None
                if payload and (payload.get("failed") or payload.get("unresolved_refs")):
                    status = "failed"
                    error_code = "capability_mcp_failed"
                elif payload and payload.get("awaiting_auth"):
                    status = "pending"
                    error_code = "capability_mcp_auth_required"
                elif stamp.mcp_servers and (
                    not payload or not set(stamp.mcp_servers) <= set(payload.get("ready", []))
                ):
                    status = "unverified"
                    error_code = "capability_mcp_unreported"
        rows.append(
            {
                "session_key": key,
                "status": status,
                "busy": session.semaphore.locked(),
                "loaded_revision": stamp.revision if stamp else "",
                **({"error_code": error_code} if error_code else {}),
            }
        )
    for key, attempt in state.capability_failures.items():
        if attempt["member"] == member and key not in state.sessions:
            rows.append({"session_key": key, **attempt})
    statuses = {row["status"] for row in rows}
    status = next(
        (value for value in ("failed", "pending", "unverified", "applied") if value in statuses),
        "pending" if saved_revision else "unverified",
    )
    return {
        "status": status,
        "saved_revision": saved_revision,
        "sessions": rows,
    }
