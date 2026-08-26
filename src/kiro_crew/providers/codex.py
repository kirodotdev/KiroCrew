"""Dedicated provider and factory for the official Codex ACP adapter."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kiro_crew.acp.client import CodexAcpClient
from kiro_crew.effort import EFFORT_LEVELS
from kiro_crew.providers.acp import AcpProvider
from kiro_crew.providers.base import LLMEvent

if TYPE_CHECKING:
    from kiro_crew.config.loader import KiroCrewConfig

logger = logging.getLogger(__name__)


class CodexAcpProvider(AcpProvider):
    """ACP provider whose client construction is owned by Codex, not Kiro."""

    def __init__(
        self,
        work_dir: str | Path | None = None,
        model: str | None = None,
        agent: str | None = None,
        sandbox_mode: str = "auto",
        session_key: str | None = None,
        channel_id: str | None = None,
        extra_env: dict[str, str] | None = None,
        effort_per_model: dict[str, str] | None = None,
        effort_defaults: object = None,
        tool_search: bool | None = None,
        tool_search_min_pct: object = None,
        tool_search_min_tokens: object = None,
        mcp_gateway_overlay: str | Path | None = None,
        mcp_gateway_settings_mcp_json: str | Path | None = None,
        mcp_gateway_socket: str | Path | None = None,
        permission_mode: str | None = None,
        crew_agent: str | None = None,
    ) -> None:
        client_kwargs: dict[str, Any] = {
            "work_dir": work_dir,
            "model": model,
            "sandbox_mode": sandbox_mode,
            "session_key": session_key,
            "channel_id": channel_id,
            "extra_env": extra_env,
            "mcp_gateway_overlay": mcp_gateway_overlay,
            "mcp_gateway_settings_mcp_json": mcp_gateway_settings_mcp_json,
            "mcp_gateway_socket": mcp_gateway_socket,
            "permission_mode": permission_mode,
        }
        if agent:
            client_kwargs["agent"] = agent
        self._client = CodexAcpClient(**client_kwargs)
        self._child_fidelity_aware = False
        self._crew_agent = crew_agent or ""
        self._history_replay_needed = False
        self._compact_result: dict | None = None
        self._effort_per_model = dict(effort_per_model or {})
        self._effort_defaults = effort_defaults
        self._tool_search = tool_search
        self._tool_search_min_pct = tool_search_min_pct
        self._tool_search_min_tokens = tool_search_min_tokens

    @property
    def is_codex_backend(self) -> bool:
        return True

    def _current_model_supports_effort(self) -> bool:
        return bool(self._client.get_valid_effort_levels())

    def _apply_effort_overlay(self) -> None:
        """Codex never writes Kiro cli.json overlays."""

    def _apply_tool_search_overlay(self) -> None:
        """Codex does not use Kiro Tool Search settings."""

    async def _set_codex_effort(self, level: str) -> None:
        valid = self._client.get_valid_effort_levels()
        if not valid:
            logger.debug("codex-acp exposes no reasoning_effort option; skipping")
            return
        candidate = level
        if candidate not in valid:
            try:
                start = EFFORT_LEVELS.index(level)
            except ValueError as exc:
                raise ValueError(f"Codex did not advertise effort {level!r}") from exc
            candidates = [item for item in reversed(EFFORT_LEVELS[: start + 1]) if item in valid]
            if not candidates:
                raise ValueError(f"Codex did not advertise effort {level!r}")
            candidate = candidates[0]
        await self._client.set_config_option(self._client.effort_config_id, candidate)

    async def _apply_initial_effort(self) -> None:
        level = self._resolve_effort()
        if not level:
            return
        try:
            await self._set_codex_effort(level)
        except Exception:
            logger.warning("Codex initial effort apply failed", exc_info=True)

    async def change_effort(self, level: str) -> bool:
        if not self._current_model_supports_effort():
            return False
        from kiro_crew.dashboard.chat_persistence import get_reasoning_effort_values

        if not level or level not in get_reasoning_effort_values():
            raise ValueError(f"invalid effort level {level!r}")
        model = self._client._model
        previous = self._effort_per_model.get(model)
        self._effort_per_model[model] = level
        try:
            await self._set_codex_effort(level)
        except Exception:
            if previous is None:
                self._effort_per_model.pop(model, None)
            else:
                self._effort_per_model[model] = previous
            raise
        return True

    async def clear_effort(self) -> bool:
        self._effort_per_model.pop(self._client._model, None)
        return False

    async def stream_command(self, command: str) -> AsyncIterator[LLMEvent]:
        async for event in self._client.stream_events(command):
            yield self._to_llm_event(event)

    async def cleanup_session(self, session_id: str) -> None:
        return None


def create_codex_provider_factory(cfg: "KiroCrewConfig") -> Callable[..., CodexAcpProvider]:
    """Build the Codex factory without entering Kiro's construction path."""
    from kiro_crew.config.loader import _session_work_dir, resolve_crew_identity
    from kiro_crew.mcp_gateway.rewriter import default_overlay_dir, default_socket_path

    gateway = cfg.mcp_gateway
    overlay: str | None
    socket: str | None
    settings: str | None
    if gateway.stub_servers:
        overlay = gateway.overlay_dir or str(default_overlay_dir())
        socket = gateway.socket_path or str(default_socket_path())
        settings = str(Path(overlay).parent / "settings" / "mcp.json")
    else:
        overlay = socket = settings = None

    def factory(
        session_key: str | None = None,
        agent: str | None = None,
        channel_id: str | None = None,
        model_override: str | None = None,
        cwd: str | None = None,
        extra_env: dict[str, str] | None = None,
        reasoning_effort_override: str | None = None,
        crew_agent: str | None = None,
        **_kwargs: object,
    ) -> CodexAcpProvider:
        model = model_override or cfg.agent.model
        effort = reasoning_effort_override or (
            cfg.agent.resolve_effort("background")
            if agent in ("kirocrew-lite", "kirocrew-heartbeat")
            else cfg.agent.reasoning_effort
        )
        return CodexAcpProvider(
            work_dir=Path(cwd) if cwd else _session_work_dir(session_key),
            model=model,
            agent=agent,
            crew_agent=resolve_crew_identity(cfg, agent, crew_agent),
            sandbox_mode=cfg.agent.sandbox,
            session_key=session_key,
            channel_id=channel_id,
            extra_env=extra_env,
            effort_per_model={model: effort} if model and effort else {},
            mcp_gateway_overlay=overlay,
            mcp_gateway_settings_mcp_json=settings,
            mcp_gateway_socket=socket,
        )

    return factory
