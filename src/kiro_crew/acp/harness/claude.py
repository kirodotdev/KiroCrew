"""claude-agent-acp, launched one process per session by ``AcpClient``.

The adapter is a Node stdio server published on npm; it delegates each model turn
to ``@anthropic-ai/claude-agent-sdk``. Launching it is four facts, all here: where
its entry script is (the shared Node-adapter ladder, cached for the gateway's life),
which version is installed (read before the spawn so the settings writer can apply
its ``settingSources`` floor), the per-session settings seed and MCP array the
session must have in place first, and the native ``claude`` binary the SDK is
pointed at through ``CLAUDE_CODE_EXECUTABLE``.

The settings seed itself is the SESSION's: it is written again when the advertised
models arrive and on a live model switch, and removed at teardown, so the session
keeps it and this adapter only says when the launch needs it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from pathlib import Path

from kiro_crew import model_registry
from kiro_crew.acp import launch as launch_mod
from kiro_crew.acp.harness.base import ProcessAdapter, SpawnContext, SpawnPlan
from kiro_crew.acp.transport_errors import AcpError
from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_NODE_ADAPTER_PACKAGES,
    ACP_BACKEND_PROCESS_NAMES,
    NODE_ADAPTER_ENTRY_SEGMENTS,
    model_registry_namespace,
)
from kiro_crew.env import describe_search_path

# The client's logger name: these lines have always been filed under it.
logger = logging.getLogger("kiro_crew.acp.client")

__all__ = [
    "CLAUDE_ACP_BIN",
    "CLAUDE_ACP_NPM_PKG",
    "CLAUDE_CODE_BIN",
    "ClaudeLaunch",
]

CLAUDE_ACP_BIN = ACP_BACKEND_PROCESS_NAMES[ACP_BACKEND_CLAUDE]
# On-disk name of the Claude backend CLI.  The claude-agent-acp adapter
# delegates the actual model turn to @anthropic-ai/claude-agent-sdk, which
# needs a per-platform native binary (~250 MB each).  Those ship as npm
# optionalDependencies that a plain ``npm i -g
# @agentclientprotocol/claude-agent-acp`` may omit, so the SDK can fail
# session/new with "Claude native binary not found for <platform>".  The SDK
# does NOT auto-discover a `claude` on PATH — it only looks for that bundled
# native package — so having it installed on the host is not enough; we point
# the adapter at it explicitly via CLAUDE_CODE_EXECUTABLE (the env var the
# adapter forwards to the SDK as pathToClaudeCodeExecutable).
# ``augmented_path()`` includes the common Node install locations
# (mise/nvm/fnm/volta shims, npm global bin), so this resolves with no user
# action when the binary is on PATH; otherwise the adapter surfaces its own
# native-binary error.
CLAUDE_CODE_BIN = "claude"
# npm package that provides the claude-agent-acp binary.  Install it publicly
# with ``npm i -g @agentclientprotocol/claude-agent-acp`` (or add it as a
# project dependency); resolution also accepts a copy under a project-local
# ``node_modules`` so no global install is strictly required.
CLAUDE_ACP_NPM_PKG = ACP_BACKEND_NODE_ADAPTER_PACKAGES[ACP_BACKEND_CLAUDE]
# Entry script relative to the installed package directory (its package.json
# "bin" field).  Locates a copy under a project ``node_modules``.
_CLAUDE_ACP_PKG_ENTRY = Path(CLAUDE_ACP_NPM_PKG, *NODE_ADAPTER_ENTRY_SEGMENTS)
# The adapter's own import of the ACP SDK, the completeness check every Node
# adapter shares (see ``acp.launch.ACP_SDK_DEP_MARKER``).
_CLAUDE_ACP_DEP_MARKER = launch_mod.ACP_SDK_DEP_MARKER

# Cache the PATH with the resolution result. A failed resolve is cached too, so
# recomputing PATH at the error site could report directories that were never searched.
_claude_acp_argv_cache: tuple[list[str] | None, str] | object = launch_mod._UNRESOLVED


def _resolve_vendored_claude_acp(pkg_dir: Path | None = None) -> str | None:
    """Return the path to a vendored claude-agent-acp entry script, or None.

    The claude spelling of the ONE shared check,
    :func:`~kiro_crew.acp.launch._vendored_adapter_entry`:
    ``<root>/@agentclientprotocol/claude-agent-acp/dist/index.js`` under each
    candidate ``node_modules`` root, accepted only when Node could import the
    adapter's dependency from the entry's real location.  *pkg_dir* is threaded
    through so tests can inject a fake package layout.
    """
    return launch_mod._vendored_adapter_entry(
        _CLAUDE_ACP_PKG_ENTRY, _CLAUDE_ACP_DEP_MARKER, pkg_dir=pkg_dir
    )


def _resolve_claude_acp_bin() -> tuple[list[str] | None, str]:
    """Find the claude-agent-acp Node entry script and its searched PATH.

    The shared ladder (:func:`~kiro_crew.acp.launch._resolve_node_adapter_argv`) with
    this adapter's three parameters; ``CLAUDE_AGENT_ACP_BIN`` is the override.
    """
    return launch_mod._resolve_node_adapter_argv(
        bin_name=CLAUDE_ACP_BIN,
        override_env="CLAUDE_AGENT_ACP_BIN",
        vendored_entry=_resolve_vendored_claude_acp,
    )


def _resolve_claude_code_executable() -> str | None:
    """Find the Claude backend CLI binary for CLAUDE_CODE_EXECUTABLE.

    The claude-agent-acp adapter forwards this env var to
    @anthropic-ai/claude-agent-sdk as ``pathToClaudeCodeExecutable``, letting
    the SDK use an existing ``claude`` install instead of the per-platform
    native binary package (~250 MB) that a plain npm install may omit.  The SDK
    does not search PATH itself, so this resolution is required even when the
    host has the ``claude`` binary installed.

    Resolution order:
      1. ``CLAUDE_CODE_EXECUTABLE`` env var (explicit override; honoured as-is).
      2. ``mise which claude`` (respects MISE_DATA_DIR and all mise config).
      3. Augmented PATH (``env.augmented_path`` — includes mise/nvm/fnm/volta
         shims and the npm global bin), so a non-login launchd/systemd gateway
         still finds an installed ``claude``.

    Returns the resolved path, or ``None`` when no ``claude`` is found.
    """
    # Read through the client at call time: its unit tests rebind
    # ``kiro_crew.acp.client.subprocess_mod`` under ``_mise_which``, and the client's
    # ``augmented_path`` for the directories searched.
    from kiro_crew.acp.client import _mise_which, augmented_path

    override = os.environ.get("CLAUDE_CODE_EXECUTABLE")
    if override and Path(override).is_file():
        return override

    mise_resolved = _mise_which(CLAUDE_CODE_BIN)
    if mise_resolved:
        return mise_resolved

    search_path = augmented_path(os.environ.get("PATH", ""))
    # Casing-normalize (Windows): a `which`-resolved .EXE reaches the launcher shim
    # with its true on-disk name (see ``acp.launch._normalize_exe_casing``).
    return launch_mod._normalize_exe_casing(shutil.which(CLAUDE_CODE_BIN, path=search_path))


def _claude_adapter_installed_version(argv: list[str]) -> str:
    """The ``version`` in the package.json of the claude-agent-acp *argv* runs, or ``""``.

    Read before spawn so the settings writer can apply the ``settingSources``
    floor before the session's MCP array is first resolved, rather than after
    the handshake. Only a manifest named :data:`CLAUDE_ACP_NPM_PKG` counts; a
    wrapper or an override that names something else answers ``""``, which the
    floor reads as below it. Blocking (small file reads); callers run it off the
    loop. The handshake's ``agentInfo.version`` is still checked before the first
    prompt.
    """
    for element in argv:
        try:
            here = Path(os.path.realpath(element))
        except (OSError, ValueError):
            continue
        if not here.is_file():
            continue
        shim_roots = [here.parent / "node_modules"]
        if here.parent.name == ".bin":
            shim_roots.append(here.parent.parent)
        candidates = [root / CLAUDE_ACP_NPM_PKG / "package.json" for root in shim_roots]
        candidates += [d / "package.json" for d in list(here.parents)[:4]]
        for manifest in candidates:
            try:
                if manifest.stat().st_size > 1 << 20:
                    continue
                data = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, ValueError, RecursionError):
                continue
            if isinstance(data, dict) and data.get("name") == CLAUDE_ACP_NPM_PKG:
                version = data.get("version")
                return version.strip() if isinstance(version, str) else ""
    return ""


class ClaudeLaunch(ProcessAdapter):
    """claude-agent-acp: a Node adapter, seeded before it starts."""

    backend = ACP_BACKEND_CLAUDE

    async def resolve_spawn(self, ctx: SpawnContext) -> SpawnPlan:
        """Resolve the adapter, read its version, then seed the session it will serve.

        The session's model is folded onto the exact spelling claude-agent-acp
        advertised (from the persisted provider-model cache warmed by a prior
        session), so a model the static registry does not carry still resolves to
        the versioned ``[1m]`` id the backend serves rather than a bare form that
        collapses to the base window. Done first so the seed carries the same id
        the wire will. No-op on a cold cache (first-ever session), which is why
        ``_apply_startup_model`` folds AGAIN after ``session/new`` has warmed it.
        """
        global _claude_acp_argv_cache  # noqa: PLW0603
        session = ctx.session
        assert session is not None, "claude-agent-acp is launched for one session"
        session._model = model_registry.resolve_wire_model_id(
            session._model, model_registry_namespace(self.backend)
        )
        cached: tuple[list[str] | None, str] | object = _claude_acp_argv_cache
        if cached is launch_mod._UNRESOLVED:
            # Fenced on the resolution generation -- see ``acp.launch._resolution_generation``.
            epoch = launch_mod._resolution_epoch(self.backend)
            cached = await asyncio.to_thread(_resolve_claude_acp_bin)
            if launch_mod._resolution_epoch(self.backend) == epoch:
                _claude_acp_argv_cache = cached
        claude_argv, acp_search_path = cached if isinstance(cached, tuple) else (None, "")
        if not isinstance(claude_argv, list) or not claude_argv:
            raise AcpError(
                f"{CLAUDE_ACP_BIN} not found "
                f"({describe_search_path(acp_search_path)}). Install it with "
                f"'npm i -g {CLAUDE_ACP_NPM_PKG}' (or add it as a project "
                f"dependency), or set CLAUDE_AGENT_ACP_BIN to its entry script."
            )
        argv: list[str] = claude_argv
        # The installed adapter's own version, read BEFORE the seed: the writer
        # applies the settingSources floor with it, so the exclusion is decided
        # before the MCP array below is first resolved and nothing has to be
        # re-resolved on the loop after the handshake. Off-loop: it reads files.
        session._agent_version_read = False
        session._claude_adapter_disk_version = await asyncio.to_thread(
            _claude_adapter_installed_version, argv
        )
        # Per-session settings seed (permissions.defaultMode + the availableModels
        # allowlist that unlocks the 1M-token window). It MUST run on the PRIMARY
        # spawn path — not only the rare model-substitution retry — or a claude
        # session collapses to the 200K default.
        await session._seed_session_settings()
        # Translate the agent spec into the session MCP array HERE, and only AFTER
        # the seed above: the array is withheld entirely unless Crew authored
        # settings.local.json, so resolving it first would read the ownership flag
        # before the writer had set it and withhold the tools of every session. Not
        # at the session/new call site either: that site is shared with kiro-cli,
        # and the translation reads disk. Resolving it here keeps the shared site a
        # synchronous in-memory read, so the kiro construction path gains no
        # executor hop and no new failure mode (harness-parity H13).
        await session._prepare_session_mcp()
        return SpawnPlan(
            argv=argv,
            spawn_label=launch_mod._adapter_spawn_label(
                argv,
                CLAUDE_ACP_BIN,
                pkg_entry=_CLAUDE_ACP_PKG_ENTRY,
                override_env="CLAUDE_AGENT_ACP_BIN",
            ),
            stderr_label=launch_mod._adapter_spawn_label(
                argv,
                "claude-acp",
                pkg_entry=_CLAUDE_ACP_PKG_ENTRY,
                override_env="CLAUDE_AGENT_ACP_BIN",
            ),
        )

    def apply_spawn_env(self, env: dict[str, str], *, spawned_binary: str | None = None) -> None:
        """Point the adapter's SDK at a native ``claude`` binary, unless one is named.

        The SDK needs a native Claude binary Crew does not vendor and does NOT search
        PATH for ``claude`` itself, so it is named explicitly. Only set when unset, so
        an operator override always wins.
        """
        if env.get("CLAUDE_CODE_EXECUTABLE"):
            return
        claude_exe = _resolve_claude_code_executable()
        if claude_exe:
            env["CLAUDE_CODE_EXECUTABLE"] = claude_exe
        else:
            logger.warning(
                "%s not found on PATH; the claude-agent-acp adapter will "
                "fail with 'Claude native binary not found'. Set "
                "CLAUDE_CODE_EXECUTABLE.",
                CLAUDE_CODE_BIN,
            )
