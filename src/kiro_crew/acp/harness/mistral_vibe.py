"""Mistral Vibe, launched one process per session by ``AcpClient``.

The binary is ``vibe-acp`` from the ``mistral-vibe`` Python package, resolved on
the shared self-served ladder from its ``ACP_BACKEND_LAUNCH`` row (no npm
adapter, no Node floor).

Routing is verified-seeded: ``VIBE_*`` variables outrank Vibe's config files,
so the seed below pins the ``ask`` profile and clamps every auto-approve path;
``AcpClient._verify_mistral_vibe_routing`` reads the resolved profile and the
exact mode list back off the session-opening response. Live captures live in
``test/fixtures/acp_frames/mistral-vibe/``.

The API key reaches the child via the inherited ``MISTRAL_API_KEY``; for
operators signed in with ``vibe setup`` the ``~/.vibe/.env`` fallback stays
readable through ``adapter_own_leaves`` (see ``agent_sdk/host_auth.py``).
"""

from __future__ import annotations

from kiro_crew import acp_tool_gate
from kiro_crew.acp import launch as launch_mod
from kiro_crew.acp.harness.base import ProcessAdapter, SpawnContext, SpawnPlan
from kiro_crew.agent_sdk.backends import ACP_BACKEND_MISTRAL_VIBE

__all__ = ["MistralVibeLaunch"]

# Vibe resolves MISTRAL_API_KEY from the process environment above the file.
_ENV_MISTRAL_API_KEY = "MISTRAL_API_KEY"

# The environment variable that overrides the vibe-acp binary path.
_ENV_MISTRAL_VIBE_ACP_BIN = "MISTRAL_VIBE_ACP_BIN"


#: The seed variables Crew puts on every vibe-acp spawn, in the order they are
#: written onto the session's environment overlay. The first is the primary of
#: the routing pair in ``ACP_BACKEND_PERMISSION_SETTING``; the other three clamp
#: the bypasses a mode read-back alone cannot prove absent. Verified live on
#: mistral-vibe 3.0.3 (``test/fixtures/acp_frames/mistral-vibe/``).
_MISTRAL_VIBE_ROUTING_SEED_ENV: tuple[tuple[str, str], ...] = (
    ("VIBE_DEFAULT_AGENT", acp_tool_gate.permission_setting_for(ACP_BACKEND_MISTRAL_VIBE)[1]),
    ("VIBE_ENABLE_AUTO_APPROVE", "false"),
    ("VIBE_BYPASS_TOOL_PERMISSIONS", "false"),
    ("VIBE_DISABLED_AGENTS", '["accept-edits", "smart-approve", "auto-approve"]'),
)

#: The mode list a seeded session must advertise -- exactly the ``ask`` profile, so
#: the read-back proves the clamp took effect, not just the starting profile. A
#: harness whose available modes carry anything else resolved a permissive
#: override above the seed and is refused.
MISTRAL_VIBE_REQUIRED_MODE_IDS: frozenset[str] = frozenset({"ask"})


class MistralVibeLaunch(ProcessAdapter):
    """Mistral Vibe: a self-served binary launched one process per session."""

    backend = ACP_BACKEND_MISTRAL_VIBE

    async def resolve_spawn(self, ctx: SpawnContext) -> SpawnPlan:
        """Resolve the binary, warm the session's MCP array, seed, then mask."""
        session = ctx.session
        assert session is not None, "vibe is launched for one session"
        _vibe_bin, argv, spawn_label, stderr_label = await launch_mod.resolve_self_served_launch(
            self.backend
        )
        # Warm the session's MCP array HERE, not at the shared session/new call
        # site: the translation reads disk, and every other backend constructs
        # through that call site (harness-parity H13).
        await session._prepare_session_mcp()
        # The routing seed. Vibe resolves VIBE_* vars above its config files and
        # reports the profile in the modes block of this very response, so the
        # read-back rides the session's own connection.
        session._extra_env = {
            **session._extra_env,
            **dict(_MISTRAL_VIBE_ROUTING_SEED_ENV),
        }
        # The refuse-then-mask preflight every enforced host runs, keyed on the
        # routing question rather than on this harness's identity.
        hidden = await launch_mod._run_preflight_bounded(
            launch_mod._sandbox_preflight, self.backend, ctx.sandbox_mode
        )
        return SpawnPlan(
            argv=argv,
            spawn_label=spawn_label,
            stderr_label=stderr_label,
            extra_hidden_dirs=hidden,
        )

    def apply_spawn_env(self, env: dict[str, str], *, spawned_binary: str | None = None) -> None:
        """Nothing beyond what the generic path already sets: the key travels via
        the inherited ``MISTRAL_API_KEY``, the ``~/.vibe/.env`` fallback stays
        readable through ``adapter_own_leaves``."""
