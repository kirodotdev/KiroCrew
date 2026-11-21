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
import logging
import os
import shutil
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any, Protocol, cast

from kiro_crew import model_registry, platform_compat
from kiro_crew.acp import launch as launch_mod
from kiro_crew.acp import raw_sdk_tasks
from kiro_crew.acp.claude_settings_sources import a_source_decides_thinking
from kiro_crew.acp.harness._common import MembershipHarness
from kiro_crew.acp.harness.base import (
    NotificationAliases,
    ProcessAdapter,
    ProcessSession,
    ReclaimPolicy,
    SessionExtras,
    SpawnContext,
    SpawnPlan,
    TeardownPolicy,
)
from kiro_crew.acp.transport_errors import AcpError
from kiro_crew.acp.types import METHOD_SESSION_CLOSE, METHOD_SESSION_UPDATE
from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_NODE_ADAPTER_PACKAGES,
    ACP_BACKEND_PROCESS_NAMES,
    ACP_BACKENDS_AGENT_STARTED_TURNS,
    ACP_BACKENDS_PLAN_CHECKLIST,
    ACP_BACKENDS_SUMMARIZED_THINKING,
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
    "CLAUDE_THINKING_DISPLAY_DEFAULT",
    "CLAUDE_THINKING_DISPLAY_ENV",
    "CLAUDE_TODO_TOOLS_ENV",
    "ClaudeHarness",
    "ClaudeLaunch",
]

#: The ``protocolVersion`` claude-agent-acp takes: the integer the per-session
#: client sends it too (``client_start.PROTOCOL_VERSION_CLAUDE``, which a test holds
#: equal to this one).
PROTOCOL_VERSION_CLAUDE = 1

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
# The native binary inside the npm ``@anthropic-ai/claude-code`` package (its
# package.json "bin" field). On Windows ``npm i -g`` exposes it only through a
# ``claude.cmd`` shim, which Node refuses to spawn without a shell; this is the file
# that shim runs.
_CLAUDE_CODE_NPM_EXE = Path("@anthropic-ai", "claude-code", "bin", "claude.exe")
# Batch files: what Node's spawn rejects with EINVAL when given no shell
# (the CVE-2024-27980 fix), so never a value for CLAUDE_CODE_EXECUTABLE.
_WINDOWS_BATCH_SUFFIXES = frozenset({".cmd", ".bat"})
#: Claude Code's switch for its checklist tools (TodoWrite, or the Task* tools),
#: which it does not offer the model in the adapter's SDK mode unless it is set
#: (measured on Claude Code 2.1.325 through claude-agent-acp 0.107.0). Their list
#: reaches Crew as the ACP ``plan`` update ``acp.plan_checklist`` reads.
CLAUDE_TODO_TOOLS_ENV = "CLAUDE_CODE_ENABLE_TODO_TOOLS"
#: claude-agent-acp's switch for how Claude Code returns the model's thinking:
#: ``summarized`` (a readable summary) or ``omitted`` (thinking blocks with no text,
#: the default of recent models, for which the adapter sends no
#: ``agent_thought_chunk``). Read by claude-agent-acp 0.107.0 from its own process
#: environment at ``session/new`` (``session-env.js`` ``resolveThinkingConfig``), so a
#: value in a Claude Code settings file's ``env`` never reaches it; 0.94.0 does not
#: read it.
CLAUDE_THINKING_DISPLAY_ENV = "CLAUDE_CODE_THINKING_DISPLAY"
#: The display a claude launch asks for when nothing else decides it.
CLAUDE_THINKING_DISPLAY_DEFAULT = "summarized"

# Cache the PATH with the resolution result. A failed resolve is cached too, so
# recomputing PATH at the error site could report directories that were never searched.
_claude_acp_argv_cache: tuple[list[str] | None, str] | object = launch_mod._UNRESOLVED

# The ``agentInfo.version`` each adapter argv reported at its latest handshake in this
# process. A wrapper script has no package.json beside it, so this is the only version
# a pre-spawn check can read for one (:func:`_claude_adapter_known_version`).
_claude_adapter_handshake_versions: dict[tuple[str, ...], str] = {}


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


#: ``platform.machine()`` spellings -> the arch suffix of the SDK's native packages.
_SDK_NATIVE_ARCH = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}


def _sdk_bundles_native_binary(resolved: object) -> bool:
    """Whether the adapter's SDK has its per-platform native binary installed.

    *resolved* is the adapter resolution (``(argv, search_path)``), cached by the
    spawn that runs first. The SDK ships that binary as an optional dependency,
    ``@anthropic-ai/claude-agent-sdk-<platform>-<arch>[-musl]``, and Node resolves
    it by walking up from the adapter's entry script, so this walks the same way
    and looks for the ``claude`` file inside. Blocking: it stats files.
    """
    import platform
    import sys

    argv = resolved[0] if isinstance(resolved, tuple) and resolved else None
    arch = _SDK_NATIVE_ARCH.get(platform.machine().lower())
    if not isinstance(argv, list) or not arch:
        return False
    pattern = f"claude-agent-sdk-{sys.platform}-{arch}*"
    for part in argv:
        entry = Path(str(part))
        if not entry.is_file():
            continue
        for directory in Path(os.path.realpath(entry)).parents:
            scope = directory / "node_modules" / "@anthropic-ai"
            if scope.is_dir() and any(
                (pkg / name).is_file()
                for pkg in scope.glob(pattern)
                for name in ("claude", "claude.exe")
            ):
                return True
    return False


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

    The adapter starts this file with Node's ``execFile``/``spawn`` and no shell, so
    on Windows a lookup that lands on the npm ``claude.cmd`` shim is replaced by the
    ``claude.exe`` the shim runs (:func:`_spawnable_claude_executable`).
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
        return _spawnable_claude_executable(mise_resolved)

    search_path = augmented_path(os.environ.get("PATH", ""))
    # Casing-normalize (Windows): a `which`-resolved .EXE reaches the launcher shim
    # with its true on-disk name (see ``acp.launch._normalize_exe_casing``).
    return _spawnable_claude_executable(
        launch_mod._normalize_exe_casing(shutil.which(CLAUDE_CODE_BIN, path=search_path))
    )


def _spawnable_claude_executable(found: str | None) -> str | None:
    """*found*, unless it is a Windows batch shim Node cannot spawn without a shell.

    A ``.cmd``/``.bat`` is swapped for the npm package's ``claude.exe`` beside it
    (:func:`~kiro_crew.acp.launch._npm_windows_shim_target`). A batch shim with no
    such binary answers ``None``: handing it over would only move the failure into
    the adapter as ``spawn EINVAL``, while an unset variable lets the SDK use its own
    bundled binary or say plainly that none is installed.
    """
    if found is None or not platform_compat.IS_WINDOWS:
        return found
    if Path(found).suffix.casefold() not in _WINDOWS_BATCH_SUFFIXES:
        return found
    target = launch_mod._npm_windows_shim_target(found, CLAUDE_CODE_BIN, _CLAUDE_CODE_NPM_EXE)
    if target:
        return target
    logger.warning(
        "%s resolves to the batch shim %s, which Node cannot spawn without a shell, "
        "and no %s was found beside it; not passing it as CLAUDE_CODE_EXECUTABLE. "
        "Set CLAUDE_CODE_EXECUTABLE to the claude.exe to use.",
        CLAUDE_CODE_BIN,
        found,
        _CLAUDE_CODE_NPM_EXE.as_posix(),
    )
    return None


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
    return launch_mod._node_adapter_manifest_version(argv, CLAUDE_ACP_NPM_PKG)


def _claude_adapter_known_version(argv: list[str]) -> str:
    """The version the claude-agent-acp *argv* runs, as known before a spawn, or ``""``.

    The adapter's own package.json first (:func:`_claude_adapter_installed_version`),
    so an upgrade is seen at once. When none is found, as for a wrapper script, the
    ``agentInfo.version`` the same argv reported at its latest handshake in this
    process, and ``""`` until one has. A stale answer cannot widen a session: its own
    handshake is checked against the floor again before its first prompt.
    Blocking (small file reads); callers run it off the loop.
    """
    return _claude_adapter_installed_version(argv) or _claude_adapter_handshake_versions.get(
        tuple(argv), ""
    )


class ClaudeProcessSession(ProcessSession, Protocol):
    """The claude-agent-acp session :class:`ClaudeLaunch` launches: a
    :class:`ProcessSession` that also seeds its own settings file and carries the
    adapter's argv and on-disk version, which the settings writer and the
    handshake read back. ``ClaudeAcpClient`` is the production implementation.
    """

    _claude_adapter_argv: tuple[str, ...]
    _claude_adapter_disk_version: str

    async def _seed_session_settings(self) -> None:
        """Seed the session's own settings file, best-effort."""

    async def _prepare_claude_side_read_denies(self) -> None:
        """Build a Side Chat session's sensitive-path deny rules off the loop, fail closed."""

    async def _prepare_claude_held_asks(self) -> None:
        """Read a Bot worker session's held-limit asks off the loop."""

    def _refuse_claude_session_below_floor_before_spawn(self, installed_version: str) -> None:
        """Refuse an option-bound session the installed claude-agent-acp is too old for."""


async def prepare_claude_session(session: ClaudeProcessSession, argv: list[str]) -> None:
    """Do the per-session work a claude session needs before ``session/new``.

    Everything here reads or writes THIS session's state and nothing about the
    process it will run in, so it is a function of the session and the adapter
    argv rather than a step of one spawn: a process serving several sessions
    runs it once per session. ``ClaudeLaunch.resolve_spawn`` calls it for the
    one session its process serves. The order is load-bearing; each step says why.
    """
    # The adapter's version, read BEFORE the seed: the writer
    # applies the settingSources floor with it, so the exclusion is decided
    # before the MCP array below is first resolved and nothing has to be
    # re-resolved on the loop after the handshake. Off-loop: it reads files.
    session._agent_version_read = False
    session._claude_adapter_argv = tuple(argv)
    # The package's own version is kept apart from the handshake fallback: only
    # it may refuse a session before spawn, because a remembered handshake is
    # never refreshed for a session that does not start.
    installed = await asyncio.to_thread(_claude_adapter_installed_version, argv)
    session._claude_adapter_disk_version = installed or _claude_adapter_handshake_versions.get(
        tuple(argv), ""
    )
    # Per-session settings seed: permissions.defaultMode, the served ``model``
    # once the advertised cache is warm, and the availableModels allowlist when
    # another settings source defines one. It MUST run on the PRIMARY spawn
    # path — not only the rare model-substitution retry — or the session
    # starts without them.
    await session._seed_session_settings()
    # A Side Chat session carries deny rules for the host's sensitive paths in
    # its session options; they are built here, off the loop, because the
    # envelope that ships them is built synchronously at session/new.
    await session._prepare_claude_side_read_denies()
    # Translate the agent spec into the session MCP array HERE, and only AFTER
    # the seed above: the array is withheld entirely unless Crew authored
    # settings.local.json, so resolving it first would read the ownership flag
    # before the writer had set it and withhold the tools of every session. Not
    # at the session/new call site either: that site is shared with kiro-cli,
    # and the translation reads disk. Resolving it here keeps the shared site a
    # synchronous in-memory read, so the kiro construction path gains no
    # executor hop and no new failure mode (harness-parity H13).
    await session._prepare_session_mcp()
    # A Bot worker's held-limit asks name its MCP servers, so they are read after
    # the array, and off the loop: the ledger and the execution record are files.
    await session._prepare_claude_held_asks()
    # Known before any process starts: an installed adapter below the
    # session-options floor cannot serve a session that needs them, so it is
    # refused here rather than after an adapter and a Claude Code process start.
    session._refuse_claude_session_below_floor_before_spawn(installed)


class ClaudeLaunch(ProcessAdapter):
    """claude-agent-acp: a Node adapter, seeded before it starts."""

    backend = ACP_BACKEND_CLAUDE
    #: The work directory of the session this launch serves, set by
    #: :meth:`resolve_spawn`, whose project settings :meth:`apply_spawn_env` reads.
    _work_dir: Path | None = None

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
        session = ctx.session
        assert session is not None, "claude-agent-acp is launched for one session"
        self._work_dir = Path(ctx.work_dir) if ctx.work_dir is not None else None
        session._model = model_registry.resolve_wire_model_id(
            session._model, model_registry_namespace(self.backend)
        )
        argv = await _resolve_claude_argv()
        await prepare_claude_session(cast(ClaudeProcessSession, session), argv)
        # Bound here rather than in AcpClient.__init__ for the same reason: the
        # constructor is shared with kiro-cli.
        if self.backend in ACP_BACKENDS_AGENT_STARTED_TURNS:
            session._install_idle_reader()
        return _claude_spawn_plan(argv)

    async def prepare_spawn_env(
        self,
        env: dict[str, str],
        *,
        to_thread: Callable[..., Awaitable[Any]],
    ) -> None:
        """:meth:`apply_spawn_env` through the guarded hop: its lookup runs ``mise``."""
        await to_thread(self.apply_spawn_env, env)

    def apply_spawn_env(self, env: dict[str, str], *, spawned_binary: str | None = None) -> None:
        """Point the adapter's SDK at a native ``claude`` binary, unless one is named.

        The SDK needs a native Claude binary Crew does not vendor and does NOT search
        PATH for ``claude`` itself, so it is named explicitly. Only set when unset, so
        an operator override always wins.

        Also sets the launch's environment defaults (:func:`_apply_claude_env_defaults`).
        """
        _apply_claude_env_defaults(env, self.backend, self._work_dir)
        _point_at_claude_code(env)


async def _resolve_claude_argv() -> list[str]:
    """The claude-agent-acp entry argv, resolved once per resolution generation.

    Shared by both drivers' launches. Raises ``AcpError`` naming the install
    command when no adapter is found.
    """
    global _claude_acp_argv_cache  # noqa: PLW0603
    cached: tuple[list[str] | None, str] | object = _claude_acp_argv_cache
    if cached is launch_mod._UNRESOLVED:
        # Fenced on the resolution generation -- see ``acp.launch._resolution_generation``.
        epoch = launch_mod._resolution_epoch(ACP_BACKEND_CLAUDE)
        cached = await asyncio.to_thread(_resolve_claude_acp_bin)
        if launch_mod._resolution_epoch(ACP_BACKEND_CLAUDE) == epoch:
            _claude_acp_argv_cache = cached
    claude_argv, acp_search_path = cached if isinstance(cached, tuple) else (None, "")
    if not isinstance(claude_argv, list) or not claude_argv:
        raise AcpError(
            f"{CLAUDE_ACP_BIN} not found "
            f"({describe_search_path(acp_search_path)}). Install it with "
            f"'npm i -g {CLAUDE_ACP_NPM_PKG}' (or add it as a project "
            f"dependency), or set CLAUDE_AGENT_ACP_BIN to its entry script."
        )
    return list(claude_argv)


def _claude_spawn_plan(argv: list[str], **kwargs: Any) -> SpawnPlan:
    """The plan for a claude-agent-acp spawn of *argv*, labelled as both drivers label it."""
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
        **kwargs,
    )


def _bounded_command_list(session_id: str, msg: Any) -> Any:
    """A copy of command list *msg* holding only what the slash menu reads, within
    the bounds a registered session's list is kept to
    (``client_transport.bounded_commands``)."""
    from kiro_crew.acp.client_transport import bounded_commands
    from kiro_crew.acp.types import UPDATE_AVAILABLE_COMMANDS, JsonRpcMessage

    commands = ((msg.params or {}).get("update") or {}).get("availableCommands")
    kept = bounded_commands(session_id, commands)
    return JsonRpcMessage(
        method=METHOD_SESSION_UPDATE,
        params={
            "sessionId": session_id,
            "update": {"sessionUpdate": UPDATE_AVAILABLE_COMMANDS, "availableCommands": kept},
        },
    )


def _is_command_list(msg: Any) -> bool:
    """Whether *msg* is a ``session/update`` carrying an ``available_commands_update``."""
    from kiro_crew.acp.types import UPDATE_AVAILABLE_COMMANDS

    params = getattr(msg, "params", None)
    update = params.get("update") if isinstance(params, dict) else None
    return (
        getattr(msg, "method", None) == METHOD_SESSION_UPDATE
        and isinstance(update, dict)
        and update.get("sessionUpdate") == UPDATE_AVAILABLE_COMMANDS
    )


def _apply_claude_env_defaults(env: dict[str, str], backend: str, work_dir: Path | None) -> None:
    """Set each variable this launch defaults, unless the environment names it.

    A value already in the environment, an empty one included, is the operator's
    (the gateway's own environment, ``agent.env`` or a session's ``extra_env``).

    - :data:`CLAUDE_TODO_TOOLS_ENV` ``=1`` on a backend in
      ``ACP_BACKENDS_PLAN_CHECKLIST``: Claude Code then offers its checklist
      tools, whose list the adapter sends as the ``plan`` the task progress bar
      draws.
    - :data:`CLAUDE_THINKING_DISPLAY_ENV` ``=summarized`` on a backend in
      ``ACP_BACKENDS_SUMMARIZED_THINKING``, so the adapter streams the model's
      thinking as text. Not when a Claude Code settings file decides thinking
      (:func:`a_source_decides_thinking`: turns it off, sets its budget or
      chooses its display): the adapter turns a display into an explicit
      ``--thinking=adaptive`` and an explicit display, which would override
      that file. Blocking: it reads those files, so it runs off the loop with
      the rest of a launch's ``apply_spawn_env``.
    """
    if backend in ACP_BACKENDS_PLAN_CHECKLIST:
        env.setdefault(CLAUDE_TODO_TOOLS_ENV, "1")
    if backend not in ACP_BACKENDS_SUMMARIZED_THINKING or CLAUDE_THINKING_DISPLAY_ENV in env:
        return
    # The child's own config directory when the environment names one.
    config_dir = env.get("CLAUDE_CONFIG_DIR")
    if not a_source_decides_thinking(work_dir, config_dir=Path(config_dir) if config_dir else None):
        env[CLAUDE_THINKING_DISPLAY_ENV] = CLAUDE_THINKING_DISPLAY_DEFAULT


def _point_at_claude_code(env: dict[str, str]) -> None:
    """Name a native ``claude`` binary for the adapter's SDK, unless one is named."""
    if env.get("CLAUDE_CODE_EXECUTABLE"):
        return
    claude_exe = _resolve_claude_code_executable()
    if claude_exe:
        env["CLAUDE_CODE_EXECUTABLE"] = claude_exe
    elif _sdk_bundles_native_binary(_claude_acp_argv_cache):
        # The SDK runs its own binary; nothing is missing.
        logger.debug("%s not on PATH; the adapter's SDK ships its own", CLAUDE_CODE_BIN)
    else:
        logger.warning(
            "%s not found on PATH; the claude-agent-acp adapter will "
            "fail with 'Claude native binary not found'. Set "
            "CLAUDE_CODE_EXECUTABLE.",
            CLAUDE_CODE_BIN,
        )


class ClaudeHarness(MembershipHarness):
    """claude-agent-acp as one process that several sessions share (``AcpRuntime``).

    Registered so the shared runtime can drive it and so it can be tested, and not
    yet a member of ``ACP_BACKENDS_ACP_RUNTIME``: no session takes this path until
    the routing change names it. Each session's own setup, which a
    process-per-session client does at spawn, is done per session here
    (:attr:`per_session_state`), through :class:`ClaudeSharedSession`.
    """

    backend = ACP_BACKEND_CLAUDE

    #: Adapter-only RSS ceiling, in MB, over :attr:`CORE_RSS_DEPTH`. The adapter
    #: alone is about 110 MB; each session's ``claude`` child and the MCP servers it
    #: starts sit below it and are left out, because they grow with the number of
    #: sessions, and a whole-subtree ceiling would recycle -- and so end -- every
    #: chat on the process once a few were open.
    CORE_RSS_CEILING_MB = 512.0

    #: Generations below the adapter the ceiling above counts: none.
    CORE_RSS_DEPTH = 0

    per_session_state = True

    #: The per-chat client's ``_reassert_adapter_resolved_model``, on the shared handle.
    reasserts_reported_model = True

    #: The process's working directory, recorded by :meth:`resolve_spawn`.
    _work_dir: Path | None = None

    #: What an ended session's raw SDK state keeps for the next session of the
    #: same id (``raw_sdk_tasks.forget_tasks``): a reload after an MCP change ends
    #: the old state and registers a new one, as the per-session client replaces
    #: its session on one object. Per instance (:meth:`_carried_raw_sdk`).
    _CARRIED_RAW_SDK_CAP = 64

    def _carried_raw_sdk(self) -> OrderedDict[str, Any]:
        carried = self.__dict__.get("_carried_raw_sdk_by_session")
        if carried is None:
            carried = self.__dict__["_carried_raw_sdk_by_session"] = OrderedDict()
        return carried

    def _early_command_lists(self) -> OrderedDict[str, Any]:
        """Command lists that named a session before its state was registered, by
        session id, newest last, at most ``_CARRIED_RAW_SDK_CAP``. Per instance."""
        early = self.__dict__.get("_early_command_lists_by_session")
        if early is None:
            early = self.__dict__["_early_command_lists_by_session"] = OrderedDict()
        return early

    # ── Seam 1: spawn ──

    async def resolve_spawn(self, ctx: SpawnContext) -> SpawnPlan:
        """The adapter entry argv, measured adapter-only. No seed: that is per session."""
        from kiro_crew.acp.harness.codex import _sandbox_wrapper_generations
        from kiro_crew.acp.session_handle import AcpRuntimeError

        try:
            argv = await _resolve_claude_argv()
        except AcpError as exc:
            raise AcpRuntimeError(str(exc)) from exc
        # The process's own directory, which decides whether a Claude Code
        # settings file there decides thinking (``_apply_claude_env_defaults``).
        self._work_dir = Path(ctx.work_dir) if ctx.work_dir is not None else None
        # A sandbox launcher that stays resident sits between the spawned pid and
        # the adapter, so the adapter-only scope starts one generation lower.
        wrapper_generations = await asyncio.to_thread(
            _sandbox_wrapper_generations, ctx.sandbox_mode
        )
        return _claude_spawn_plan(argv, rss_depth=self.CORE_RSS_DEPTH + wrapper_generations)

    def apply_spawn_env(
        self,
        env: dict[str, str],
        *,
        spawned_binary: str | None = None,
        cli_owned_auth: bool = False,
    ) -> None:
        """Take kiro-cli's API key out, set the launch's environment defaults, and
        name the native ``claude`` for the SDK, as the per-session launch does."""
        from kiro_crew.config import loader as loader_mod

        loader_mod.strip_kiro_cli_api_key(env)
        _apply_claude_env_defaults(env, self.backend, self._work_dir)
        _point_at_claude_code(env)

    @property
    def verifies_agent_activation(self) -> bool:
        """No: the adapter takes no ``--agent``, and its modes are permission modes."""
        return False

    # ── Seam 2: initialize ──

    @property
    def protocol_version(self) -> Any:
        return PROTOCOL_VERSION_CLAUDE

    @property
    def client_capabilities(self) -> dict[str, Any]:
        """What ``AcpClient`` declares to claude-agent-acp, unchanged."""
        from kiro_crew.acp.client_native_children import CLAUDE_CLIENT_CAPABILITIES

        return CLAUDE_CLIENT_CAPABILITIES

    # ── Seam 3: session/new and session/load ──

    async def session_extras(self, agent: str, **_kwargs: Any) -> SessionExtras:
        """Empty: claude has no custom-agent channel. Its per-session payload is
        the envelope (:meth:`session_meta`) and the mirrored array."""
        return SessionExtras()

    def session_mcp_servers(
        self,
        requested: list[dict[str, Any]],
        *,
        agent_capabilities: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """The mirrored array as built: the adapter accepts every shape Crew sends."""
        return requested

    @property
    def wants_session_file_on_load(self) -> bool:
        """No: the adapter finds the session from its id."""
        return False

    async def prepare_session(
        self,
        *,
        work_dir: str | Path,
        agent: str | None,
        model: str,
        session_key: str,
        channel_id: str,
        memory_mode: str,
        options: Mapping[str, Any] | None = None,
    ) -> Any:
        """Write THIS session's settings seed in its own folder, before its request.

        *options* carry the chat's own ``permission_mode``, ``side_read_only`` and
        ``include_memory``, the three a per-chat client takes at construction, and
        its ``model``, which wins over the runtime's *model*.

        On a process-per-session client the seed is written at spawn, which on a
        shared process would seed only the first session: a later one, in another
        folder above all, would start in whatever mode the user's own settings name.
        So it is written here, per session. Refusals (a side session with no deny
        rules, an adapter below the session-options floor) raise before anything
        is sent.
        """
        from kiro_crew.acp.claude_shared_session import ClaudeSharedSession
        from kiro_crew.acp.session_handle import AcpRuntimeError
        from kiro_crew.agent import DerivedSpecStale, require_fresh_derived_spec

        chosen = dict(options or {})
        # The chat's own model, over the runtime's, which on a process shared by a
        # session tree is the founder's (``ChatRuntimeKey`` leaves it unkeyed).
        model = str(chosen.pop("model", "") or "") or model
        # The spec freshness gate the spawn runs for the founder's agent, run for
        # THIS session's agent too: its spec is read per session, not per process.
        try:
            await asyncio.to_thread(require_fresh_derived_spec, agent, work_dir)
        except DerivedSpecStale as exc:
            raise AcpRuntimeError(str(exc)) from exc
        state = ClaudeSharedSession(
            work_dir=work_dir,
            model=model_registry.resolve_wire_model_id(
                model, model_registry_namespace(self.backend)
            ),
            agent=agent,
            session_key=session_key,
            channel_id=channel_id,
            **chosen,
        )
        state.memory_mode = memory_mode
        argv = await _resolve_claude_argv()
        try:
            await state.prepare(argv)
        except BaseException:
            await self.end_session(state)
            raise
        return state

    def permission_surface_owned(self, state: Any) -> bool:
        """Whether this session's seed governs its permission surface: the mirror
        withholds the whole array otherwise, as on the per-session client."""
        return bool(state is not None and state._permission_surface_governed)

    def session_meta(
        self,
        state: Any,
        mcp_servers: list[dict[str, Any]],
        *,
        zero_tools: bool = False,
        stub_token: str = "",
    ) -> dict[str, Any] | None:
        """The ``_meta.claudeCode`` envelope the per-session client sends, for this session.

        Plus this session's identity in ``options.env``. A per-chat client puts the
        session key and its stub token on its process's environment, and the tools
        Claude Code runs (a ``kirocrew`` call from Bash) read them there. On a shared
        process that environment is the founding session's, so each session carries
        its own here instead; the adapter spreads ``options.env`` over the process
        environment for that session's ``claude`` child only.
        """
        if state is None:
            return None
        if not getattr(state, "_spec_allowlists_adopted", False):
            from kiro_crew.acp.session_handle import AcpRuntimeError

            # Fail closed: an envelope without the spec's allowlists would let the
            # session's shell and write tools run unasked.
            raise AcpRuntimeError(
                "claude shared session: the spec's allowlists were not read before " "the envelope"
            )
        # The spec's "tools": [] as the mirror's projection read it: a zero-tool
        # session whose array still mounts a server is refused here.
        state._spec_zero_tools = zero_tools
        # The array this start sends, which the starting-mode pin reads to tell a
        # session carrying Crew's tools from one with none (on the per-session
        # client, ``_begin_session_report`` records it).
        state._mcp_mount_servers = list(mcp_servers)
        # The same array as the session's own cache: the envelope's forced asks for
        # Crew-owned Connection servers are read from it (``_bridged_server_asks``).
        state._session_mcp_cache = list(mcp_servers)
        # The held-limit asks for this array: the built-ins ``prepare`` read, plus an
        # ``mcp__<server>`` ask per server it sends when a held limit covers MCP, as
        # ``mate_held_rules.claude_ask_rules`` builds them on the per-session client.
        # Rebuilt from the stored base, so a second start (a retry) does not double it.
        held_asks = list(getattr(state, "_claude_held_builtin_asks", ()))
        if getattr(state, "_claude_held_mcp", False):
            from kiro_crew.agent_sdk.claude_worker_tools import claude_mcp_server_rule

            held_asks.extend(
                claude_mcp_server_rule(e["name"])
                for e in mcp_servers
                if isinstance(e, dict) and isinstance(e.get("name"), str) and e["name"]
            )
        state._claude_held_asks = tuple(dict.fromkeys(held_asks))
        meta = state._harness_session_meta(mcp_servers)
        identity = state.session_identity_env(stub_token)
        if identity:
            options = meta.setdefault("claudeCode", {}).setdefault("options", {})
            options["env"] = {**dict(options.get("env") or {}), **identity}
        return meta

    async def after_session_start(
        self,
        state: Any,
        session_id: str,
        resp: dict[str, Any],
        *,
        send_request: Callable[[str, dict[str, Any]], Awaitable[Any]],
        close_session: Callable[[], Awaitable[None]],
        agent_version: str = "",
    ) -> None:
        """Pin the asking mode on this session before its first prompt.

        A session that cannot be pinned is closed on its own (``session/close``) and
        refused; the process and every other session on it keep running.
        """
        if state is None:
            return
        # A session of this id that just ended (a reload) hands its dashboard mark
        # and the Workflow card ids it saw to this one.
        carried = self._carried_raw_sdk().pop(session_id, None)
        if carried is not None:
            own = state._raw_sdk
            own.dashboard_turn = own.dashboard_turn or carried.dashboard_turn
            for card in carried.seen_cards:
                own.seen_cards.setdefault(card, None)
        # A command list that arrived before this session's state was registered.
        early = self._early_command_lists().pop(session_id, None)
        if early is not None:
            try:
                state._note_advertised_commands(early)
            except Exception:  # noqa: BLE001 - a bad list must not fail the start
                logger.debug("reading an early claude command list failed", exc_info=True)
        state.bind_started(
            session_id,
            agent_version,
            send_request=send_request,
            close_session=close_session,
        )
        await state._pin_starting_mode(resp)

    async def end_session(self, state: Any) -> None:
        """Remove this session's seed, as the per-session client does at teardown.

        Once per session: a refused start and the teardown it triggers may both
        reach here."""
        if state is None or getattr(state, "_shared_session_ended", False):
            return
        state._shared_session_ended = True
        # Its background tasks end with it: the cards a dashboard kept open for
        # them are closed (``raw_sdk_tasks.forget_tasks``), as on the per-session
        # client when its session or process goes.
        raw_sdk_tasks.forget_tasks(state)
        session_id = getattr(state, "_session_id", None)
        if session_id:
            carried = self._carried_raw_sdk()
            carried[session_id] = state._raw_sdk
            carried.move_to_end(session_id)
            # shortcut: a session closed for good leaves its entry until the cap
            # drops it; key the carry on evict only if this ever holds much.
            while len(carried) > self._CARRIED_RAW_SDK_CAP:
                carried.popitem(last=False)
        try:
            await state._discard_settings_seed()
        except Exception:
            logger.warning(
                "claude shared session: the settings seed was not removed", exc_info=True
            )
        finally:
            state._reset_settings_seed_state()

    def take_session_frame(self, state: Any, msg: Any) -> bool:
        """Read a ``_claude/sdkMessage`` of this session into its state.

        Claude Code's background-task and API-retry messages
        (``acp.raw_sdk_tasks.take``), read as the per-session client's transport
        reader reads them: into the session's own ``_raw_sdk``, put on no queue, so
        a turn, a waiting request and an idle reader all read past them. Any other
        frame is not taken.
        """
        if state is None:
            # A command list can arrive in the same burst as ``session/new``'s
            # answer, before the session's state is registered: kept by session
            # id (bounded) and applied when the session starts
            # (:meth:`after_session_start`).
            if _is_command_list(msg):
                from kiro_crew.acp.client_transport import (
                    ADVERTISED_COMMANDS_SESSION_ID_MAX_CHARS,
                )

                early = self._early_command_lists()
                session_id = str((msg.params or {}).get("sessionId") or "")
                if session_id and len(session_id) <= ADVERTISED_COMMANDS_SESSION_ID_MAX_CHARS:
                    early[session_id] = _bounded_command_list(session_id, msg)
                    early.move_to_end(session_id)
                    while len(early) > self._CARRIED_RAW_SDK_CAP:
                        early.popitem(last=False)
            return False
        try:
            # The session's own command list, kept as the per-chat client keeps it
            # and within the same bounds as an early one
            # (``client_transport._note_advertised_commands``); the frame goes on.
            state._note_advertised_commands(msg)
        except Exception:  # noqa: BLE001 - one bad list must not stop the reader
            logger.debug("reading a claude command list failed", exc_info=True)
        try:
            return raw_sdk_tasks.take(state, msg)
        except Exception:  # noqa: BLE001 - one bad message must not stop the reader
            logger.debug("reading a claude SDK message failed", exc_info=True)
            return getattr(msg, "method", None) == raw_sdk_tasks.METHOD_CLAUDE_SDK_MESSAGE

    async def adopt_substituted_model(self, state: Any, substitute: str) -> bool:
        """Adopt the model an organization-policy advisory names, and re-seed, once.

        The advisory ("Model X is restricted ... Using Y instead.") answers
        ``session/new`` with a ``-32603`` error and no session; the runtime parses
        *substitute* out of it. As the per-chat client does before re-issuing
        (``_retry_after_model_substitution``), this names the session's pin when the
        backend serves it, else *substitute* (``_substitution_retry_model``), and
        re-seeds ``settings.local.json`` with it, so the runtime's one retry starts on
        a model the gateway serves. When the seed already names the served pin and the
        gateway refused it, this falls back once to *substitute*; when the seed already
        names *substitute*, it answers False.
        """
        if state is None or not substitute:
            return False
        model = state._substitution_retry_model(substitute)
        if model == getattr(state, "_adopted_substitute_model", None):
            if model == substitute:
                return False
            # The served pin was refused as well: fall back once to the model the
            # backend offers, since a session on it beats one that never starts.
            model = substitute
        # The name comes from the backend's advisory, so the log line does not
        # carry it: the session's own model row shows which model it runs.
        logger.warning(
            "claude shared session: session/new named a substitute model; "
            "adopting it and retrying once"
        )
        state._model = model
        state._adopted_substitute_model = model
        try:
            await asyncio.to_thread(state._write_claude_local_settings)
        except (OSError, ValueError, TypeError):
            logger.warning(
                "claude shared session: re-seed after substitution failed", exc_info=True
            )
        return True

    async def adopt_session_spec(self, state: Any, spec: dict[str, Any] | None) -> None:
        """Read the spec's shell and write allowlists from the spec the projection read
        (``ClaudeSharedSession.adopt_allowlist_spec``), for the envelope's ask rules
        and the handle's stamps alike."""
        if state is not None:
            await state.adopt_allowlist_spec(spec)

    def bind_session_handle(self, state: Any, handle: Any) -> None:
        """Stamp the spec's ``toolsSettings`` shell and write allowlists on the handle.

        Claude never reads ``toolsSettings``; the per-chat client stamps these on its
        own permission requests (``client_mcp_array``), and on the shared runtime the
        session handle builds the requests, so it carries them instead.
        """
        if state is None:
            return
        handle.spec_shell_allowlist = getattr(state, "_spec_shell_allowlist", None)
        handle.spec_write_allowlist = getattr(state, "_spec_write_allowlist", None)
        handle.spec_write_cwd = str(getattr(state, "_spec_write_cwd", "") or "")

    # ── Seam 4: inbound requests the host answers ──

    @property
    def host_answered_methods(self) -> tuple[str, ...]:
        """None: claude-agent-acp asks Crew for no connection-level callback."""
        return ()

    async def answer_request(self, method: str) -> dict[str, Any]:
        """Never called: :attr:`host_answered_methods` is empty."""
        raise NotImplementedError(
            f"claude harness answers no inbound request, including {method!r}"
        )

    # ── Seam 5: notification aliases ──

    @property
    def notification_aliases(self) -> NotificationAliases:
        """Plain ACP ``session/update`` only."""
        return NotificationAliases(session_update=(METHOD_SESSION_UPDATE,))

    # ── Seam 6: teardown ──

    @property
    def teardown(self) -> TeardownPolicy:
        """``session/close``, awaited: it ends one session and keeps its record.

        Recorded on claude-agent-acp 0.94.0
        (``test/fixtures/acp_frames/claude/new-conversation-live.jsonl``): after
        close the session answers ``Session not found``, and a later ``session/load``
        restores it. ``session/delete`` would remove the record, so it is not used.
        """
        return TeardownPolicy(method=METHOD_SESSION_CLOSE, notification=False)

    # ── Seam 9: reclaim ──

    def reclaim_policy(self, *, max_age_secs: float, max_rss_mb: float) -> ReclaimPolicy:
        """The adapter-only ceiling over :attr:`CORE_RSS_DEPTH`; age passes through.

        The operator's ``max_rss_mb`` counts the whole subtree, a different unit from
        the adapter-only scope this spawn is measured over, so it is not carried over.
        """
        return ReclaimPolicy(max_age_secs=max_age_secs, max_rss_mb=self.CORE_RSS_CEILING_MB)
