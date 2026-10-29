"""Google Antigravity's standalone ACP server, one process per session.

The ACP Registry distributes Antigravity as two executables that must stay in the
same directory: ``agy_acp_server`` (the process Crew launches) and
``localharness_external`` (the helper that server locates through
``ANTIGRAVITY_HARNESS_PATH``). Neither component is useful without the other, so
resolution treats the pair as one install and reports the absent half explicitly.

This is the standalone ACP runtime, not the interactive ``agy`` terminal UI.

Antigravity advertises the per-tool ``session/request_permission`` flow in its
default mode. Crew writes that mode after ``session/new`` and ``session/load``
through the shared ``Routing.SESSION_CONFIG`` path. The harness also offers
``auto_edit`` and ``yolo``; both suppress permission requests in the harness
itself, so Crew refuses either as a mid-session configuration change. An
authenticated turn proving every native and MCP call takes the default flow has
not been captured, so the adapter does not claim a zero-tool contract.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from kiro_crew import platform_compat
from kiro_crew.acp import launch as launch_mod
from kiro_crew.acp.harness.base import ProcessAdapter, SpawnContext, SpawnPlan
from kiro_crew.acp.transport_errors import AcpError
from kiro_crew.agent_sdk.backends import ACP_BACKEND_ANTIGRAVITY
from kiro_crew.env import describe_search_path

__all__ = [
    "ANTIGRAVITY_ACP_BIN_ENV",
    "ANTIGRAVITY_HARNESS_PATH_ENV",
    "ANTIGRAVITY_PROTOCOL_VERSION",
    "ANTIGRAVITY_VERIFIED_VERSION",
    "AntigravityLaunch",
    "AntigravityResolution",
    "antigravity_cached_resolution",
    "antigravity_not_found_message",
    "forget_antigravity_resolution",
    "probe_antigravity_runtime",
    "resolve_antigravity_runtime",
]

# The ACP protocol and release verified against Google's published runtime.
ANTIGRAVITY_PROTOCOL_VERSION = 1
ANTIGRAVITY_VERIFIED_VERSION = "1.4.4"

# The two published archive members. Windows renames only the main executable.
ANTIGRAVITY_ACP_BIN = "agy_acp_server.exe" if platform_compat.IS_WINDOWS else "agy_acp_server.par"
ANTIGRAVITY_HARNESS_BIN = (
    "localharness_external.exe" if platform_compat.IS_WINDOWS else "localharness_external"
)

#: Operator override for the main executable. The sibling helper is discovered
#: beside it unless ``ANTIGRAVITY_HARNESS_PATH`` names one explicitly.
ANTIGRAVITY_ACP_BIN_ENV = "ANTIGRAVITY_ACP_BIN"

#: The helper variable Antigravity's executable reads. Crew always sets it for the
#: child to the exact sibling it resolved, rather than relying on ambient state.
ANTIGRAVITY_HARNESS_PATH_ENV = "ANTIGRAVITY_HARNESS_PATH"

ANTIGRAVITY_INSTALL_HINT = (
    f"Install the verified Google Antigravity ACP {ANTIGRAVITY_VERIFIED_VERSION} archive from "
    "https://agentclientprotocol.com/registry, or set ANTIGRAVITY_ACP_BIN to "
    "its agy_acp_server executable."
)


@dataclass(frozen=True)
class AntigravityResolution:
    """The two executables one Antigravity install contributes."""

    server: str | None
    helper: str | None
    search_path: str


_cache: AntigravityResolution | None = None
_cache_lock = threading.Lock()
_cache_generation = 0


def _normalize_executable(path: str | None) -> str | None:
    """Return *path* when it is executable, with platform-correct casing."""
    if not path or not platform_compat.is_executable_file(path):
        return None
    return os.path.realpath(path) if platform_compat.IS_WINDOWS else path


def _find_main_executable(env: Mapping[str, str], search_path: str) -> str | None:
    override = env.get(ANTIGRAVITY_ACP_BIN_ENV, "")
    if override:
        return _normalize_executable(os.path.expanduser(override))

    # Keep this ladder consistent with the other self-served harnesses, while
    # requiring the sibling helper below before either executable is reported.
    from kiro_crew.acp.client import _mise_which

    mise_resolved = _mise_which(ANTIGRAVITY_ACP_BIN)
    if mise_resolved:
        return _normalize_executable(mise_resolved)
    return _normalize_executable(shutil.which(ANTIGRAVITY_ACP_BIN, path=search_path))


def _find_helper(env: Mapping[str, str], server: str | None, search_path: str) -> str | None:
    explicit = env.get(ANTIGRAVITY_HARNESS_PATH_ENV, "")
    if explicit:
        return _normalize_executable(os.path.expanduser(explicit))

    if server:
        sibling = _normalize_executable(str(Path(server).with_name(ANTIGRAVITY_HARNESS_BIN)))
        if sibling:
            return sibling
    return _normalize_executable(shutil.which(ANTIGRAVITY_HARNESS_BIN, path=search_path))


def _resolve_antigravity_runtime() -> AntigravityResolution:
    """Resolve both archive members against one environment snapshot. Blocking."""
    from kiro_crew.acp.client import augmented_path

    env = dict(os.environ)
    search_path = augmented_path(env.get("PATH", ""))
    server = _find_main_executable(env, search_path)
    helper = _find_helper(env, server, search_path)
    return AntigravityResolution(server=server, helper=helper, search_path=search_path)


def probe_antigravity_runtime() -> AntigravityResolution:
    """Resolve both components without changing what the next spawn will use.

    Blocking. This is the install probe's read: it must notice a component added
    after the gateway started, while the spawn path keeps its process-lifetime
    answer until an owner-gated re-check clears it.
    """
    return _resolve_antigravity_runtime()


def resolve_antigravity_runtime() -> AntigravityResolution:
    """Return both Antigravity executables, cached for this gateway process.

    Blocking. Spawns call it off the event loop. Ordinary reads must not
    invalidate the cache and change what the next spawn will do behind the
    caller's back; :func:`probe_antigravity_runtime` is the side-effect-free
    fresh read for the install probe.
    """
    global _cache
    with _cache_lock:
        cached = _cache
        generation = _cache_generation
    if cached is not None:
        return cached

    resolved = _resolve_antigravity_runtime()
    with _cache_lock:
        if _cache_generation == generation and _cache is None:
            _cache = resolved
        return _cache or resolved


def antigravity_cached_resolution() -> AntigravityResolution | None:
    """The resolution this process has already cached, if any."""
    with _cache_lock:
        return _cache


def forget_antigravity_resolution() -> None:
    """Drop the process-lifetime resolution after an owner-gated re-check."""
    global _cache, _cache_generation
    with _cache_lock:
        _cache = None
        _cache_generation += 1


def antigravity_not_found_message(resolution: AntigravityResolution) -> str:
    """The actionable error for a half-installed or absent Antigravity pair."""
    missing = []
    if not resolution.server:
        missing.append(ANTIGRAVITY_ACP_BIN)
    if not resolution.helper:
        missing.append(ANTIGRAVITY_HARNESS_BIN)
    searched = describe_search_path(resolution.search_path)
    return (
        f"Google Antigravity ACP is not fully installed: missing {', '.join(missing)} "
        f"({searched}). The main executable and its localharness_external helper are "
        f"distributed together and must stay in the same directory. "
        f"{ANTIGRAVITY_INSTALL_HINT}"
    )


class AntigravityLaunch(ProcessAdapter):
    """Launch Google Antigravity's two-file standalone ACP runtime."""

    backend = ACP_BACKEND_ANTIGRAVITY

    def __init__(self) -> None:
        self._helper_path = ""

    async def resolve_spawn(self, ctx: SpawnContext) -> SpawnPlan:
        """Resolve the pair, warm MCP, then apply the enforced-host credential mask."""
        session = ctx.session
        assert session is not None, "antigravity is launched for one session"
        resolution = await asyncio.to_thread(resolve_antigravity_runtime)
        if not resolution.server or not resolution.helper:
            raise AcpError(antigravity_not_found_message(resolution))

        # The mirror reads the agent spec into this session's MCP cache. Antigravity
        # reads no Kiro agent file of its own, so this call must precede session/new.
        await session._prepare_session_mcp()
        hidden = await launch_mod._run_preflight_bounded(
            launch_mod._sandbox_preflight, self.backend, ctx.sandbox_mode
        )
        self._helper_path = resolution.helper
        return SpawnPlan(
            argv=[resolution.server],
            spawn_label="agy_acp_server",
            stderr_label="agy_acp_server",
            extra_hidden_dirs=hidden,
        )

    def apply_spawn_env(self, env: dict[str, str], *, spawned_binary: str | None = None) -> None:
        """Point the server at the exact helper resolved for this launch."""
        if self._helper_path:
            env[ANTIGRAVITY_HARNESS_PATH_ENV] = self._helper_path
