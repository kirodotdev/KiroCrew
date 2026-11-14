"""Opt-in native Kiro memory for new personal chats, separate from Crew memory.

With ``agent.orchestrator_memory`` off, Crew states no native memory policy and
KAS runs on its own defaults. With it on, local KAS processes and excluded
sessions explicitly disable native memory, and personal dashboard chats may
request read-only recall with reflection off. A saved KAS session keeps its
policy, so withdrawing recall requires a fresh SID and Crew transcript replay on
the next cold start, not a live policy change.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from kiro_crew.acp_backends import ACP_BACKEND_KAS
from kiro_crew.constants import is_owner_minted_session_key

logger = logging.getLogger(__name__)

MODE_READ_ONLY = "read_only"
MODE_DISABLED = "disabled"
MEMORY_TOOL_ID = "memory"
_REFLECTION_CONFIG_ID = "memoryReflection"
_RECALLING_SESSION_MODES = frozenset({"persistent", "incognito"})


@dataclass(frozen=True, slots=True)
class MemoryPlan:
    """The new session's policy, saved session's policy, and withdrawal decision."""

    mode: str
    resume_mode: str
    rotate: bool


def memory_plan(
    *,
    setting_on: bool,
    kas_backend: bool,
    session_key: str,
    session_memory_mode: str,
    owning_app: str,
    recorded_mode: str,
    cloud_session: bool = False,
    personal_session: bool = False,
) -> MemoryPlan:
    """Choose native memory without granting it to existing or cloud chats.

    With the setting off the plan states no policy, for new and resumed
    sessions alike, but a saved reading session still rotates.
    """
    if not kas_backend or cloud_session:
        return MemoryPlan(mode="", resume_mode="", rotate=False)
    if not setting_on:
        return MemoryPlan(mode="", resume_mode="", rotate=recorded_mode == MODE_READ_ONLY)
    mode = MODE_DISABLED
    if personal_session and _recalls_host_memories(
        session_key=session_key,
        session_memory_mode=session_memory_mode,
        owning_app=owning_app,
    ):
        mode = MODE_READ_ONLY
    # Loading an explicit KAS record cannot withdraw its memory access.
    rotate = recorded_mode == MODE_READ_ONLY and mode != MODE_READ_ONLY
    return MemoryPlan(mode=mode, resume_mode=recorded_mode or MODE_DISABLED, rotate=rotate)


def _recalls_host_memories(*, session_key: str, session_memory_mode: str, owning_app: str) -> bool:
    """Allow recall only in the local operator's personal interactive chat."""
    if not is_owner_minted_session_key(session_key):
        return False
    if owning_app or session_memory_mode not in _RECALLING_SESSION_MODES:
        return False
    # Members live above the ACP layer and cannot be imported at module load.
    from kiro_crew.members import is_member_session_key

    if is_member_session_key(session_key):
        return False
    return not crew_reachable_by_others()


def crew_reachable_by_others() -> bool:
    """Withhold personal memories when sharing is configured or cannot be read."""
    try:
        from kiro_crew.shared_crew.access import setup_paths_dict
        from kiro_crew.shared_crew.contracts import PrincipalKind, Provenance
        from kiro_crew.shared_crew.crew_mode import network_configured
        from kiro_crew.shared_crew.people import registry as people_registry

        if network_configured(setup_paths_dict()):
            return True
        return any(
            observation.principal.kind is PrincipalKind.HUMAN
            and observation.principal.provenance is not Provenance.LOCAL_OPERATOR
            for observation in people_registry().snapshot()
        )
    except Exception:  # noqa: BLE001 -- unreadable sharing state withholds personal memory
        logger.debug("shared crew mode unreadable; withholding memory", exc_info=True)
        return True


def orchestrator_memory_enabled() -> bool:
    """Whether ``agent.orchestrator_memory`` is a JSON ``true``. Blocking on a config cache miss.

    Off when the config cannot be loaded, which leaves KAS on its own defaults.
    """
    try:
        # Imported per call, like the ACP layer's other config readers, so loading
        # this module does not load the config plane.
        from kiro_crew.config.loader import KiroCrewConfig

        return KiroCrewConfig.load().agent.orchestrator_memory is True
    except Exception:  # noqa: BLE001 -- config must never break a session start
        logger.debug("config unavailable; stating no native memory policy", exc_info=True)
        return False


async def runtime_memory_mode(backend: str, requested: str | None) -> str:
    """The KAS memory mode a session states; ``""`` states none.

    A string request is the session's own policy and is kept, ``""`` included,
    so a live reload re-sends exactly what its SID stated. ``None`` means the
    caller chose nothing: it is ``disabled`` while ``agent.orchestrator_memory``
    is on, so workers that bypass allocation are still excluded from recall.
    Other engines get no mode. The Kiro Cloud harness removes the setting from
    its own request (:func:`without_memory_settings`), so a cloud session states
    no policy.
    """
    if backend != ACP_BACKEND_KAS:
        return ""
    if requested is not None:
        if requested not in {"", MODE_DISABLED, MODE_READ_ONLY}:
            raise ValueError("Unsupported native Kiro memory mode")
        return requested
    enabled = await asyncio.to_thread(orchestrator_memory_enabled)
    return MODE_DISABLED if enabled else ""


def memory_settings(mode: str) -> dict[str, Any]:
    """State both native memory fields so no device default can fill either one."""
    return {"memory": {"mode": mode, "reflection": False}}


def sent_memory_mode(params: dict[str, Any]) -> str:
    """The native memory mode *params* asks KAS for, or ``""`` when it states none."""
    meta = params.get("_meta")
    kiro = meta.get("kiro") if isinstance(meta, dict) else None
    settings = kiro.get("settings") if isinstance(kiro, dict) else None
    memory = settings.get("memory") if isinstance(settings, dict) else None
    mode = memory.get("mode") if isinstance(memory, dict) else None
    return mode if isinstance(mode, str) else ""


def without_memory_settings(kiro: dict[str, Any]) -> dict[str, Any]:
    """A copy of a ``_meta.kiro`` envelope that states no native memory policy."""
    settings = kiro.get("settings")
    if not isinstance(settings, dict) or "memory" not in settings:
        return dict(kiro)
    rest = {key: value for key, value in settings.items() if key != "memory"}
    out = {key: value for key, value in kiro.items() if key != "settings"}
    if rest:
        out["settings"] = rest
    return out


def require_memory_ack(response: dict[str, Any], mode: str) -> None:
    """Refuse recall unless KAS confirms its policy and reflection control.

    Engines without native memory may omit its acknowledgement for disabled
    sessions. Any returned policy must match, including on legacy resumes.
    """
    if not mode:
        return
    meta = response.get("_meta")
    config = meta.get("memoryConfig") if isinstance(meta, dict) else None
    if config is None and mode == MODE_DISABLED:
        return
    from kiro_crew.acp.session_handle import AcpRuntimeError

    if (
        not isinstance(config, dict)
        or config.get("mode") != mode
        or config.get("reflection") is not False
    ):
        raise AcpRuntimeError(
            "Kiro did not confirm the requested native memory policy; "
            "this session cannot receive chat prompts."
        )
    if mode == MODE_DISABLED:
        return
    options = response.get("configOptions")
    if isinstance(options, list):
        for option in options:
            if not isinstance(option, dict) or option.get("id") != _REFLECTION_CONFIG_ID:
                continue
            values = option.get("options")
            if (
                option.get("type") == "select"
                and option.get("currentValue") == "off"
                and isinstance(values, list)
                and any(isinstance(value, dict) and value.get("value") == "off" for value in values)
            ):
                return
    raise AcpRuntimeError(
        "Kiro did not advertise the native memory reflection control; "
        "read-only memory requires a supported Kiro engine."
    )


def mount_memory_tool(mode: str, tools: Any) -> Any:
    """Add native retrieval to a recalling agent's tool allowlist, once."""
    if mode != MODE_READ_ONLY or not isinstance(tools, list):
        return tools
    if MEMORY_TOOL_ID in tools:
        return tools
    return [*tools, MEMORY_TOOL_ID]
