"""goose, launched one process per session by ``AcpClient``.

goose serves ACP from its own binary (``goose acp``), so its executable comes off the
shared self-served ladder and there is no adapter package and no Node floor. Two
things make its launch its own: the builtin extension it is told to load beside
Crew's servers, and where its permission route comes from -- goose resolves
``GOOSE_MODE`` out of its own ENVIRONMENT, above its config file, so the mode is
seeded on the session's environment here and read back off the very response that
opens or restores the session (``AcpClient._verify_goose_routing``), not by a
read-back child of its own.
"""

from __future__ import annotations

from kiro_crew import acp_tool_gate
from kiro_crew.acp import launch as launch_mod
from kiro_crew.acp.harness.base import ProcessAdapter, SpawnContext, SpawnPlan
from kiro_crew.agent_sdk.backends import ACP_BACKEND_GOOSE

__all__ = ["GooseLaunch"]

# The channel Crew's permission routing travels down on this harness: goose resolves
# its mode from a PLAIN ENVIRONMENT VARIABLE, above its own config file, so the seed
# needs neither a file nor a JSON document. Verified on this harness by resolving a
# config that declares ``GOOSE_MODE: auto`` and reading ``approve`` back off the
# session.
_ENV_GOOSE_MODE = "GOOSE_MODE"
# The builtin extension Crew asks goose to load. goose REPLACES its configured
# extensions with the client's ``mcpServers`` array, so a session handed Crew's
# servers and nothing else carries no shell and no file tools at all. Naming it on
# the command line restores those alongside Crew's own, and they route through the
# same permission mode as everything else.
_GOOSE_BUILTIN_ARG = "--with-builtin"
_GOOSE_BUILTIN_DEVELOPER = "developer"
# goose's auto-approving mode (``auto``) is never named here: the seed is the one
# place a mode value enters the child and it carries the required mode, so the auto
# mode has no path onto the wire by construction. ``session/set_mode`` would accept
# it, which is why no constant for it exists to be passed.


class GooseLaunch(ProcessAdapter):
    """goose: a self-served binary whose mode is seeded on the session's environment."""

    backend = ACP_BACKEND_GOOSE

    async def resolve_spawn(self, ctx: SpawnContext) -> SpawnPlan:
        """Resolve the binary, warm the session's MCP array, then mask and seed."""
        session = ctx.session
        assert session is not None, "goose is launched for one session"
        _goose_bin, argv, spawn_label, stderr_label = await launch_mod.resolve_self_served_launch(
            self.backend
        )
        # The builtin extension travels on the ARGV rather than in the session array,
        # because it is not one of Crew's servers: it is the harness's own shell and
        # file tools, which this harness drops when a client supplies
        # ``mcpServers``. Appended AFTER the shared resolution, so the label stays
        # the harness plus its ACP subcommand and does not grow a builtin an
        # operator did not name.
        argv = [*argv, _GOOSE_BUILTIN_ARG, _GOOSE_BUILTIN_DEVELOPER]
        # Translate the agent spec into this session's MCP array HERE, for the
        # reason the claude and opencode adapters do it in their own launches: the
        # translation reads disk, and doing it at the shared session/new call site
        # would put an executor hop and a new failure mode on EVERY backend's
        # construction path, kiro-cli included (harness-parity H13).
        await session._prepare_session_mcp()
        # The same refuse-then-mask preflight the other enforced hosts run, keyed on
        # the same routing question rather than on this harness's identity: it is
        # ENFORCED, so the OS credential mask is the compensating control for the
        # passive reads ACP v1 cannot make it ask about. This harness re-exposes no
        # file from inside the mask, so no expose list is resolved for it.
        hidden = await launch_mod._run_preflight_bounded(
            launch_mod._sandbox_preflight, self.backend, ctx.sandbox_mode
        )
        # The routing seed. There is no read-back child here and no wrapped second
        # spawn: this harness reports the mode it resolved in the ``modes`` block of
        # the very response that opens or restores the session, so the read-back
        # rides the session's own connection. What travels here is only the seed,
        # written onto the session's own environment overlay so every child the
        # session starts carries it.
        session._extra_env = {
            **session._extra_env,
            _ENV_GOOSE_MODE: acp_tool_gate.permission_setting_for(self.backend)[1],
        }
        return SpawnPlan(
            argv=argv,
            spawn_label=spawn_label,
            stderr_label=stderr_label,
            extra_hidden_dirs=hidden,
        )

    def apply_spawn_env(self, env: dict[str, str], *, spawned_binary: str | None = None) -> None:
        """Nothing beyond the seed, which reaches the child through the session's overlay."""
