"""vibe, launched one process per session by ``AcpClient``.

Mistral's Vibe coding agent serves ACP from its own binary (``vibe-acp``), so its
executable comes off the shared self-served ladder: no adapter package, no Node
floor, and -- because its launch row holds every decision as a value -- no
decision of its own to make here. That is the whole point of landing the harness
DORMANT: the launch is already a fact the table owns, so this adapter is the
thinnest one in the folder and stays that way until routing is measured.

Two absences are deliberate and are what "dormant" means in code:

* **No MCP-array warm.** vibe's projection is declared ``no-channel``
  (``providers/mirrors/registry``): no live capture has shown the
  ``session/new`` ``mcpServers`` array mounted by ``vibe-acp``, so the session
  warms nothing and the spawn sends no servers it cannot vouch for.
* **No credential mask.** The refuse-then-mask preflight is keyed on the routing
  question, and this harness's routing is ``Routing.UNVERIFIED`` -- outside
  ``tool_gate.ENFORCED_ROUTINGS`` -- so ``adapter_hidden_credential_dirs``
  resolves nothing for it and there is no mask to resolve or spare here. The
  read-gate floor still fences its credential leaf from the agent's file tools.

When selectability lands, both absences are revisited with the routing entry: a
measured mode pin (the ``mode`` config option / ``session/set_mode`` route to
``ask``) moves the routing to a verified member, and the mask question moves with
it -- see the worked example in ``docs/system-specs/modules/harness-onboarding.md``.
"""

from __future__ import annotations

from kiro_crew.acp import launch as launch_mod
from kiro_crew.acp.harness.base import ProcessAdapter, SpawnContext, SpawnPlan
from kiro_crew.agent_sdk.backends import ACP_BACKEND_VIBE

__all__ = ["VibeLaunch"]


class VibeLaunch(ProcessAdapter):
    """vibe: a self-served binary whose launch row is the whole plan."""

    backend = ACP_BACKEND_VIBE

    async def resolve_spawn(self, ctx: SpawnContext) -> SpawnPlan:
        """Resolve the binary; nothing else is known about this spawn yet."""
        _vibe_bin, argv, spawn_label, stderr_label = await launch_mod.resolve_self_served_launch(
            self.backend
        )
        # No session MCP warm (no-channel: nothing to mount), no credential mask
        # (routing UNVERIFIED: no mask resolves for this harness), and no seed
        # (the candidate mode pin is unmeasured, so seeding a value here would
        # be the routing claim this harness is dormant to avoid). The plan is
        # therefore exactly the launch row's own four answers.
        return SpawnPlan(
            argv=argv,
            spawn_label=spawn_label,
            stderr_label=stderr_label,
        )

    def apply_spawn_env(self, env: dict[str, str], *, spawned_binary: str | None = None) -> None:
        """Nothing beyond the shared environment: no override reaches this child yet."""
