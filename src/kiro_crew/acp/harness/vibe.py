"""Mistral Vibe, launched one process per session by ``AcpClient``.

Mistral Vibe serves ACP from its own binary (``vibe-acp``), a Python entry point
shipped by the ``mistral-vibe`` package. There is no npm adapter to resolve and no
Node floor to satisfy -- the published package ships the executable -- so its binary
comes off the shared self-served ladder
(:func:`kiro_crew.acp.launch.resolve_self_served_launch`), from its
``ACP_BACKEND_LAUNCH`` row.

The permission routing is ``Routing.VERIFIED_SEEDED_SETTINGS``. Vibe layers its
configuration as defaults < model catalog < user TOML < project TOML < ``VIBE_*``
environment variables < runtime overrides < agent profile < admin config
(``vibe.core.config.default_orchestrator.build_default_orchestrator``, read against
the installed mistral-vibe 3.0.3), so environment variables Crew puts on the spawn
resolve ABOVE every config file the operator or a project could carry. Four of them
are seeded here, and together they make the harness ask per tool call:

* ``VIBE_DEFAULT_AGENT=ask`` -- the session starts in the ``ask`` agent profile
  (no permission overrides), not the harness's own default ``accept-edits``.
* ``VIBE_ENABLE_AUTO_APPROVE=false`` -- clamps the bypass outright:
  ``_effective_bypass_tool_permissions`` ANDs the flag in, so neither
  ``bypass_tool_permissions = true`` in any file below the environment layer nor
  the ``auto-approve`` profile's own override can turn approvals off, and every
  profile with the YOLO safety level is removed from the picker.
* ``VIBE_BYPASS_TOOL_PERMISSIONS=false`` -- the config-file path of the same
  bypass, overridden directly.
* ``VIBE_DISABLED_AGENTS=["accept-edits", "smart-approve", "auto-approve"]`` --
  removes the remaining auto-approving profiles, so the mode list the session
  advertises is exactly ``ask`` and a ``session/set_mode`` /
  ``session/set_config_option mode=...`` to anything else is refused by the
  harness itself (both verbs are client-to-agent calls; the model cannot send
  them).

The read-back rides the session's own connection: the ``modes`` block of the very
``session/new`` (or ``session/load`` / ``session/fork``) response reports the profile
the harness resolved, and ``AcpClient._verify_vibe_routing`` requires
``currentModeId == "ask"`` AND the advertised mode list to be exactly ``ask``
before the first prompt, refusing the session otherwise. Live captures on
mistral-vibe 3.0.3 (``test/fixtures/acp_frames/vibe/routing-live.jsonl``): with the
seed, a ``write_file`` call raised ``session/request_permission`` and a
``reject_once`` answer failed the call with no file written -- including with a
hostile ``bypass_tool_permissions = true`` + ``default_agent = "auto-approve"`` in
the config file, which the environment layer outranks; without the seed the same
file auto-approved the write with no permission request. Residual, stated rather
than closed: ACP v1 cannot require a prompt for a passive read, and a per-tool
``permission = "always"`` in the operator's OWN config still pre-approves that tool
(the operator's grant, the same posture goose's ``permission.yaml`` carries); what
the seed removes is the AGENT's ability to grant either -- config writes are
outside-workdir or in-workdir writes the ``ask`` profile routes through
``session/request_permission`` like any other tool call. The OS-boundary credential
mask (``tool_gate.adapter_hidden_credential_dirs``) is the compensating control for
reads, as for codex.

The API key reaches the child through the ``MISTRAL_API_KEY`` environment variable,
which Vibe resolves from the inherited process environment above its own
``~/.vibe/.env`` file. For an operator who signed in with ``vibe setup`` and has
the key only in that file, the declaration spares the leaf from the enforced
sandbox mask via ``adapter_own_leaves=(".vibe/.env",)`` (see
``agent_sdk/host_auth.py`` for the accepted-risk note; Vibe does not scrub
key-named variables from its own children, so the vault route is not available to
it).
"""

from __future__ import annotations

from kiro_crew import acp_tool_gate
from kiro_crew.acp import launch as launch_mod
from kiro_crew.acp.harness.base import ProcessAdapter, SpawnContext, SpawnPlan
from kiro_crew.agent_sdk.backends import ACP_BACKEND_VIBE

__all__ = ["VibeLaunch"]

# The environment variable Vibe reads its Mistral API key from. Vibe resolves
# credentials from the inherited process environment first, above its own
# ``~/.vibe/.env`` file, so the inherited variable is authoritative; the file
# itself is the fallback for a ``vibe setup``-only operator and is spared from
# the enforced mask by the auth declaration's ``adapter_own_leaves``.
_ENV_MISTRAL_API_KEY = "MISTRAL_API_KEY"

# The environment variable that overrides the vibe-acp binary path.
_ENV_VIBE_ACP_BIN = "VIBE_ACP_BIN"

#: The seed variables Crew puts on every vibe-acp spawn, in the order they are
#: written onto the session's environment overlay. The first is the primary of the
#: routing pair in ``ACP_BACKEND_PERMISSION_SETTING`` -- read from there, not
#: re-spelled -- because the seed, the read-back and the refusal must agree. The
#: other three clamp the bypasses a mode read-back alone cannot prove absent:
#: ``enable_auto_approve=false`` makes the harness ignore ``bypass_tool_permissions``
#: AND drop every YOLO-safety profile, ``bypass_tool_permissions=false`` overrides
#: the config-file path directly, and the disabled-agents list leaves ``ask`` as the
#: only selectable mode. Verified live on mistral-vibe 3.0.3: with all four set, a
#: config file carrying ``bypass_tool_permissions = true`` and
#: ``default_agent = "auto-approve"`` still started in ``ask``, still asked before a
#: write, and still refused ``session/set_config_option mode=accept-edits``.
_VIBE_ROUTING_SEED_ENV: tuple[tuple[str, str], ...] = (
    ("VIBE_DEFAULT_AGENT", acp_tool_gate.permission_setting_for(ACP_BACKEND_VIBE)[1]),
    ("VIBE_ENABLE_AUTO_APPROVE", "false"),
    ("VIBE_BYPASS_TOOL_PERMISSIONS", "false"),
    ("VIBE_DISABLED_AGENTS", '["accept-edits", "smart-approve", "auto-approve"]'),
)

#: The mode list a seeded session must advertise -- exactly the ``ask`` profile, so
#: the read-back proves the clamp took effect, not just the starting profile. A
#: harness whose available modes carry anything else resolved a permissive
#: override above the seed and is refused.
VIBE_REQUIRED_MODE_IDS: frozenset[str] = frozenset({"ask"})


class VibeLaunch(ProcessAdapter):
    """Mistral Vibe: a self-served binary launched one process per session."""

    backend = ACP_BACKEND_VIBE

    async def resolve_spawn(self, ctx: SpawnContext) -> SpawnPlan:
        """Resolve the binary, warm the session's MCP array, seed, then mask."""
        session = ctx.session
        assert session is not None, "vibe is launched for one session"
        _vibe_bin, argv, spawn_label, stderr_label = await launch_mod.resolve_self_served_launch(
            self.backend
        )
        # Translate the agent spec into this session's MCP array HERE, for the
        # reason the claude and opencode adapters do it in their own launches: the
        # translation reads disk, and doing it at the shared session/new call site
        # would put an executor hop and a new failure mode on EVERY backend's
        # construction path, kiro-cli included (harness-parity H13).
        await session._prepare_session_mcp()
        # The routing seed. Vibe reads its whole config from ``VIBE_*`` variables in
        # a layer above its own files, and reports the profile it resolved in the
        # ``modes`` block of the response that opens or restores the session, so the
        # read-back rides the session's own connection -- no second child, no window
        # in which the session is live and the posture unconfirmed. The seed travels
        # on the session's environment overlay so every child the session starts
        # carries it (``AcpClient._verify_vibe_routing`` performs the read-back).
        session._extra_env = {
            **session._extra_env,
            **dict(_VIBE_ROUTING_SEED_ENV),
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
        """Nothing beyond what the generic path already sets.

        The Mistral API key reaches the child through the inherited environment
        (``MISTRAL_API_KEY``), which Vibe resolves above its own credential file;
        the file itself stays readable for a ``vibe setup``-only operator through
        the declaration's ``adapter_own_leaves`` carve-out, so no key is written
        or injected here.
        """
