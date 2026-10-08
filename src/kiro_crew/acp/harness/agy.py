"""Antigravity CLI (agy), launched one process per session by ``AcpClient``."""

from __future__ import annotations

from kiro_crew.acp import launch as launch_mod
from kiro_crew.acp.harness.base import ProcessAdapter, SpawnContext, SpawnPlan
from kiro_crew.agent_sdk.backends import ACP_BACKEND_AGY

__all__ = ["AgyLaunch"]


class AgyLaunch(ProcessAdapter):
    """Google Antigravity CLI (agy): a self-served binary serving ACP."""

    backend = ACP_BACKEND_AGY

    async def resolve_spawn(self, ctx: SpawnContext) -> SpawnPlan:
        session = ctx.session
        assert session is not None, "agy is launched for one session"
        _agy_bin, argv, spawn_label, stderr_label = await launch_mod.resolve_self_served_launch(
            self.backend
        )
        await session._prepare_session_mcp()
        return SpawnPlan(
            argv=argv,
            spawn_label=spawn_label,
            stderr_label=stderr_label,
        )

    def apply_spawn_env(self, env: dict[str, str], *, spawned_binary: str | None = None) -> None:
        """Nothing beyond the standard environment."""
