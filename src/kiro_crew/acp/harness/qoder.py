"""Qoder CLI, launched one process per session with its permission gate pinned.

Qoder serves ACP from its own binary (``qodercli --acp``), so the executable comes
from the shared self-served resolver.  Its permission route is not seeded in a
config file: Crew appends an inline ``--settings`` document that outranks project
and user configuration on the measured release, pins the default mode explicitly,
and uses ``--strict-mcp-config`` so only Crew's session-array mounts survive.

The child also loses the environment names that could undo that pin from outside
the gate:

* ``QODER_CONFIG_DIR``, ``QODER_CONFIG_DIR_NAME``, ``QODER_USER_CONFIG_DIR``,
  ``QODER_GLOBAL_CONFIG_FILE`` and ``QODER_PROFILE_DIR`` can relocate configuration
  or select another configuration/profile root.
* ``QODER_MCP_CONFIG`` can point at an MCP settings source beside the argv pin.
* ``QODER_PERMISSION_MODE`` can set the permission mode.
* ``QODER_DISABLE_READONLY_SHELL_AUTO_ALLOW`` is permission-behaviour control.
* ``QODER_SANDBOX`` can select Qoder's own execution posture.
* A second strings pass over the readable 1.1.67 bundle found ``QODER_HOME`` behind
  the ``QODER_ENV_HOME`` source identifier, and ``QODER_ENV`` behind
  ``QODER_ENV_ENV``; both are removed, as are their source-identifier spellings.
* ``QODER_ENV_FILE`` is read as an environment-file source and is removed.
* ``QODER_ENV_MODE`` is mode-named, so it is removed even though this exact spelling
  was not visible as a runtime lookup in the readable bundle.
* ``QODER_BOOTSTRAP_AUTH_STATE`` and ``QODER_SDK_AUTH_PAYLOAD_FILE`` inject or locate
  auth state rather than merely carrying a token; removing them isolates login from
  caller-supplied state without deleting a token variable.

These are removed from the inherited child environment; ordinary variables remain.
``QODER_PERSONAL_ACCESS_TOKEN`` is deliberately kept because it is the measured
authentication variable.  ``QODER_AUTH_MANAGED_TOKEN`` and ``QODER_AUTH_STDIN`` are
also kept: they are auth-bearing, but the strings inspected do not show them
relocating config or changing permission posture.  ``QODER_PROJECT_DIR`` is the
project placeholder Qoder exports to hook children; the spawn's own ``cwd`` remains
the Crew-selected project, so it is kept rather than widened into a second cwd mask.
``QODER_USER_INFO`` and ``QODER_ENV_PRODUCT`` were not visible in the readable 1.1.67
bundle and are likewise not permission/config evidence, so they stay.

If an operator authenticates only through a relocated ``QODER_CONFIG_DIR`` or through
bootstrap auth state, this isolation can make the child lose that login even though
the operator's interactive qodercli works; the safe Crew-supported route is qodercli's
default login location or the measured PAT variable.

Qoder exposes no resolved-permission read-back.  The argv is therefore the
route's precondition, while the client's version, mode and unasked-call gates are
the runtime witnesses that the release and session still behave as measured.
"""

from __future__ import annotations

from kiro_crew.acp import launch as launch_mod
from kiro_crew.acp.harness.base import ProcessAdapter, SpawnContext, SpawnPlan
from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_QODER,
    qoder_pinned_argv,
)

__all__ = ["QODER_REMOVED_ENV_VARS", "QoderLaunch"]

#: Environment names Crew refuses to hand the pinned child.  Keep this explicit:
#: a wildcard such as ``*CONFIG*`` would silently remove unrelated diagnostics or
#: provider settings.  The docstring carries the reason per group.
QODER_REMOVED_ENV_VARS: frozenset[str] = frozenset(
    {
        "QODER_CONFIG_DIR",
        "QODER_CONFIG_DIR_NAME",
        "QODER_BOOTSTRAP_AUTH_STATE",
        "QODER_DISABLE_READONLY_SHELL_AUTO_ALLOW",
        "QODER_ENV",
        "QODER_ENV_ENV",
        "QODER_ENV_FILE",
        "QODER_ENV_HOME",
        "QODER_ENV_MODE",
        "QODER_GLOBAL_CONFIG_FILE",
        "QODER_HOME",
        "QODER_MCP_CONFIG",
        "QODER_PERMISSION_MODE",
        "QODER_PROFILE_DIR",
        "QODER_SANDBOX",
        "QODER_SDK_AUTH_PAYLOAD_FILE",
        "QODER_USER_CONFIG_DIR",
    }
)


class QoderLaunch(ProcessAdapter):
    """Qoder's self-served launch with the asking posture pinned on argv."""

    backend = ACP_BACKEND_QODER

    async def resolve_spawn(self, ctx: SpawnContext) -> SpawnPlan:
        """Resolve the binary, warm Crew's session MCP array, then mask and pin."""
        session = ctx.session
        assert session is not None, "qoder is launched for one session"
        _binary, base_argv, spawn_label, stderr_label = await launch_mod.resolve_self_served_launch(
            self.backend
        )
        # Crew's pooled broker stubs are session-array members.  Qoder mounts them
        # and asks their tools; project-owned MCP configuration is dropped instead.
        await session._prepare_session_mcp()
        # Enforced routing: the same refuse-then-mask preflight its siblings run is
        # the compensating control for passive reads ACP's request protocol cannot
        # observe. The adapter's own inferred auth directory is carved out by the
        # credential declaration.
        hidden = await launch_mod._run_preflight_bounded(
            launch_mod._sandbox_preflight, self.backend, ctx.sandbox_mode
        )
        return SpawnPlan(
            argv=qoder_pinned_argv(base_argv),
            spawn_label=spawn_label,
            stderr_label=stderr_label,
            extra_hidden_dirs=hidden,
        )

    def apply_spawn_env(self, env: dict[str, str], *, spawned_binary: str | None = None) -> None:
        """Remove the variables that can bypass or relocate the pinned gate."""
        for name in QODER_REMOVED_ENV_VARS:
            env.pop(name, None)
        # Auth is not permission routing: leave the measured PAT variable intact.
        assert "QODER_PERSONAL_ACCESS_TOKEN" not in QODER_REMOVED_ENV_VARS
