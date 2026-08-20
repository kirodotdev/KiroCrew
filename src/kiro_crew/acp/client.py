"""ACP client — JSON-RPC 2.0 over stdio with `kiro-cli acp` or `claude-agent-acp`.

Protocol (ACP JSON-RPC 2.0):
  initialize → session/new → session/set_mode → session/set_model → session/prompt
  (claude backend: skips set_mode and uses session/set_config_option for model)

Agent selection: ``session/set_mode`` with ``modeId`` activates the agent
config (prompt, tools, resources).  MCP servers are passed explicitly
in ``session/new`` via the ``mcpServers`` parameter.

Permission flow:
  ← session/request_permission (server→client REQUEST with uuid id)
  → {result: {outcome: {outcome: "selected", optionId: "allow_once"}}}
"""

from __future__ import annotations

import asyncio
import difflib
import functools
import glob
import json
import logging
import os
import re
import shutil
import signal
import stat
import subprocess as subprocess_mod
import sys
import time
from collections import deque
from contextlib import aclosing
from pathlib import Path
from typing import Any, AsyncGenerator, AsyncIterator, Callable, Sequence, TypeVar

from kiro_crew import model_registry, platform_compat
from kiro_crew.acp._dispatch import (
    _kiro_mcp_server_name,
    _kiro_tool_name,
    extract_tool_purpose,
    parse_session_modes,
    parse_usage_update,
)
from kiro_crew.acp.liveness import (
    VERDICT_WORKING,
    LivenessOracle,
    _consume_future_exception,
    consult_offloaded,
)
from kiro_crew.acp.prompt_blocks import build_prompt_blocks
from kiro_crew.acp.types import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_KIRO,
    ACP_BACKENDS_INTERNAL_SANDBOX,
    ACP_BACKENDS_STEER,
    ACP_CLIENT_CAPABILITIES,
    EVENT_AGENT_SWITCHED,
    EVENT_CLEAR_STATUS,
    EVENT_COMPACTION_STATUS,
    EVENT_COMPLETE,
    EVENT_MCP_OAUTH_REQUEST,
    EVENT_MCP_SERVER_INIT_FAILURE,
    EVENT_MCP_SERVER_INITIALIZED,
    EVENT_PERMISSION_REQUEST,
    EVENT_SUBAGENT_ACTIVITY,
    EVENT_SUBAGENT_LIST,
    EVENT_TEXT_CHUNK,
    EVENT_THINKING_CHUNK,
    EVENT_TOOL_CALL,
    EVENT_TOOL_CALL_UPDATE,
    EVENT_TOOL_RESULT,
    KNOWN_SESSION_UPDATES,
    METHOD_AGENT_SWITCHED,
    METHOD_CANCEL,
    METHOD_CLEAR_STATUS,
    METHOD_COMMANDS_EXECUTE,
    METHOD_COMPACTION_STATUS,
    METHOD_INITIALIZE,
    METHOD_KIRO_SESSION_UPDATE,
    METHOD_MCP_OAUTH_REQUEST,
    METHOD_MCP_SERVER_INIT_FAILURE,
    METHOD_MCP_SERVER_INITIALIZED,
    METHOD_METADATA,
    METHOD_PROMPT,
    METHOD_REQUEST_PERMISSION,
    METHOD_SESSION_LOAD,
    METHOD_SESSION_NEW,
    METHOD_SESSION_UPDATE,
    METHOD_SET_MODE,
    METHOD_SET_MODEL,
    METHOD_SUBAGENT_LIST_UPDATE,
    OPTION_ALLOW_ALWAYS,
    OPTION_ALLOW_ONCE,
    OUTCOME_CANCELLED,
    OUTCOME_SELECTED,
    STOP_REASON_END_TURN,
    UPDATE_AGENT_MESSAGE_CHUNK,
    UPDATE_AGENT_THOUGHT_CHUNK,
    UPDATE_CONFIG_OPTION,
    UPDATE_TOOL_CALL,
    UPDATE_USAGE,
    AcpEvent,
    AcpPromptStats,
    JsonRpcMessage,
    JsonRpcRequest,
    TurnUsage,
)
from kiro_crew.agent import ensure_agent_materialized
from kiro_crew.config.paths import kiro_sessions_dir
from kiro_crew.constants import (
    COMPACT_WAIT_TIMEOUT_SECS,
    KIROCREW_SPAWNED_ENV,
    KIROCREW_SPAWNED_VALUE,
)
from kiro_crew.env import augmented_path, mise_data_dir, resolve_krb5_ccname
from kiro_crew.executors import subprocess_executor
from kiro_crew.hooks import (
    HOOK_EVENT_POST_TOOL_USE,
    fire_tool_hooks,
    get_global_hook_store,
)
from kiro_crew.kiro_cli import resolve_kiro_cli
from kiro_crew.mcp_gateway.claim import schedule_claim
from kiro_crew.mcp_gateway.session_servers import pooled_session_servers
from kiro_crew.resource_status import inject_xdist_auto_cap
from kiro_crew.sandbox import (
    RLIMIT_PROFILE_SESSION_HOST,
    apply_windows_resource_ceiling,
    cgroup_scope_argv,
    create_subprocess_limited,
    scrub_agent_denied_env,
    wrap_argv,
)
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel
from kiro_crew.skill_usage import get_global_skill_read_observer

logger = logging.getLogger(__name__)

CLIENT_NAME = "kirocrew"
CLIENT_VERSION = "0.1.2"
_T = TypeVar("_T")
# kiro-cli uses a date-stamped protocol; claude-agent-acp follows the
# upstream ACP SDK (numeric integer, currently 1).  See acp.types.
PROTOCOL_VERSION = "2025-08-22"
PROTOCOL_VERSION_CLAUDE = 1
DEFAULT_MODEL = "auto"

KIRO_CLI_BIN = "kiro-cli"
KIRO_CLI_SUBCMD = "acp"

CLAUDE_ACP_BIN = "claude-agent-acp"
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
CLAUDE_ACP_NPM_PKG = "@agentclientprotocol/claude-agent-acp"
# Entry script relative to the installed package directory (its package.json
# "bin" field).  Used to locate a copy under a project ``node_modules``.
_CLAUDE_ACP_PKG_ENTRY = Path(CLAUDE_ACP_NPM_PKG) / "dist" / "index.js"
# A direct runtime dependency of the adapter that npm hoists flat into the
# same node_modules root.  Its presence is a cheap completeness check: a
# copy missing it would crash at import with
# ``ERR_MODULE_NOT_FOUND: @agentclientprotocol/sdk``, so we reject such an
# incomplete root and fall through to the next candidate.
_CLAUDE_ACP_DEP_MARKER = Path("@agentclientprotocol") / "sdk"

# High-frequency, content-free adapter stderr diagnostics that _drain_stderr()
# drops instead of forwarding as per-line WARNINGs.  The driving case is the
# claude-agent-acp "Unexpected case: {...thinking_tokens...}" line.  Mechanism
# (confirmed by reading the vendored adapter, dist/acp-agent.js): the backend
# emits a `system` message with subtype `thinking_tokens`, but the adapter's
# `switch (message.subtype)` enumerates ~18 known subtypes (init, status,
# compact_boundary, memory_recall, api_retry, ...) and routes anything else to
# `default: unreachable(message)`, which does `logger.error("Unexpected case:
# " + JSON.stringify(message))` to stderr — one line per token delta.  Measured
# at ~10 lines/sec during active thinking (one line per 2-4 thinking tokens; the
# payload is only estimated_tokens/_delta/uuid/session_id — no response content,
# so dropping them loses nothing).
#
# This is a forward-compat gap in the adapter, NOT new behavior in a specific
# backend build: the `thinking_tokens` event is present in both 2.1.165.357
# and 2.1.168.358 (verified by string-matching both bundled `claude` binaries —
# identical occurrences), so it predates the .168 update that drew attention to
# it.  Exactly when it began appearing in our logs is unconfirmed.  The cleaner
# long-term fix is upstream (add a `thinking_tokens` case to the adapter / bump
# the vendored version); this filter is the version-agnostic stopgap that also
# absorbs the next unenumerated subtype's flood.
#
# Why drop rather than just downgrade the level:
#   1. Log hygiene — gateway.log uses a RotatingFileHandler(maxBytes=2MB,
#      backupCount=3) (see cli.py), so a sustained burst rolls genuine
#      diagnostics out of the retained 8MB window.
#   2. Event-loop load — the file handler is a plain *synchronous* handler and
#      _drain_stderr runs as a task on the gateway event loop, so each forwarded
#      line costs a synchronous file write + two regex redaction passes on the
#      same loop that streams responses.  Per-session the cost is small; it
#      compounds across concurrent thinking sessions.
# (This is a log-volume / event-loop-load reduction, NOT a fix for any
# turn-stall or "agent not responding" symptom — no such causal link was
# established.)
#
# Match on a stable substring (not the full JSON) so the filter survives field
# changes, and keep the tuple NARROW so a genuine error line is never silently
# swallowed.
_SUPPRESSED_STDERR_MARKERS = ("thinking_tokens",)
# Minimum seconds between throttled debug summaries of the suppressed-line count,
# so the suppression itself stays observable without re-introducing a flood.
_SUPPRESSED_STDERR_SUMMARY_INTERVAL_SECS = 60.0


class _KiroExecutableTrustError(RuntimeError):
    """Resolved Kiro CLI bytes are not approved for credential-bearing ACP."""


def _is_safe_oauth_url(url: str) -> bool:
    """Reject anything that isn't http(s) — `<a href>` will execute javascript:/data:."""
    if not url:
        return False
    lower = url.lower()
    return lower.startswith("https://") or lower.startswith("http://")


def _normalize_exe_casing(path: str | None) -> str | None:
    """On Windows, return *path* with its TRUE on-disk casing (via realpath).

    Some Windows multiplexer launchers derive which tool to run from their own
    ``argv[0]`` basename, CASE-SENSITIVELY. But ``shutil.which`` builds the
    resolved name's extension from ``PATHEXT``, which may list ``.EXE`` upper-
    case — so it can return ``...\\kiro-cli.EXE`` even though the file on disk
    is ``kiro-cli.exe``. Spawned under the wrong casing, such a launcher can fail
    to dispatch and exit immediately, breaking the ACP pipe. ``os.path.realpath``
    restores the true directory-entry casing. No-op on POSIX (case-sensitive FS;
    realpath only follows symlinks). Returns None unchanged.
    """
    if path is None or not platform_compat.IS_WINDOWS:
        return path
    try:
        return os.path.realpath(path)
    except OSError:
        return path


def _resolve_kiro_bin() -> str | None:
    """Resolve the user's installed Kiro CLI, to be launched in place.

    Returns the installed binary's own path. KiroCrew never copies the CLI and
    executes the copy: Kiro CLI 2.15+ dispatches subcommands by exec'ing a
    sibling executable resolved relative to its own path, which a copy into a
    private directory destroys.
    """

    executable = resolve_kiro_cli()
    if not executable:
        return executable
    # Deferred to keep the low-level resolver import graph acyclic:
    # kiro_prerequisite imports sandbox helpers that this module also uses.
    from kiro_crew.kiro_prerequisite import snapshot_trusted_acp_executable

    try:
        if platform_compat.IS_WINDOWS:
            snapshot = snapshot_trusted_acp_executable(
                executable,
                platform_name="win32",
                environ=os.environ,
            )
        else:
            snapshot = snapshot_trusted_acp_executable(executable)
    except (OSError, ValueError) as exc:
        raise _KiroExecutableTrustError(str(exc)) from exc
    return snapshot.launch_path


async def _resolve_kiro_bin_for_spawn() -> str | None:
    """Resolve the Kiro CLI path off the event loop.

    Plain ``to_thread`` — deliberately NOT shielded. The shield existed only to
    reclaim a snapshot descriptor when the caller was cancelled mid-resolve;
    with the CLI launched in place there is no resource to reclaim. Keeping the
    shield would actively harm: ``asyncio.shield`` only marks the inner task's
    result retrieved when the OUTER future is cancelled, but here it is the
    awaiting task that gets cancelled, so a resolve that raises concurrently
    (e.g. mid-self-update, when the binary transiently fails the runnable check)
    leaves an unretrieved exception. That surfaces at GC as "Task exception was
    never retrieved" and the gateway's asyncio handler writes a full false
    ASYNCIO UNHANDLED record to crash.log for an ordinary tab close.
    """

    return await asyncio.to_thread(_resolve_kiro_bin)


def _mise_which(tool: str) -> str | None:
    """Ask mise for the resolved path of *tool*.

    Respects MISE_DATA_DIR, global config, and .mise.toml — works
    regardless of how the user configured their mise installation.
    Returns None if mise isn't installed or the tool isn't registered.
    """
    mise_bin = shutil.which("mise")
    if not mise_bin:
        return None
    try:
        result = subprocess_mod.run(
            [mise_bin, "which", tool],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            path = result.stdout.strip()
            if Path(path).is_file():
                return path
    except (subprocess_mod.TimeoutExpired, OSError):
        pass
    return None


def _mise_node_installs_dir() -> Path:
    """Canonical path to mise's Node installs directory.

    The data root comes from :func:`kiro_crew.env.mise_data_dir` so that
    ``MISE_DATA_DIR`` and ``XDG_DATA_HOME`` are honoured — the previous
    hardcoded ``~/.local/share/mise`` silently missed installs on any host
    with a relocated mise data dir, while the env helper already resolved the
    same root correctly for the build toolchain.
    """
    return Path(mise_data_dir(str(Path.home()))) / "installs" / "node"


def _resolve_node_for_script(script_path: str) -> str | None:
    """Derive the correct node binary for a script installed under mise.

    If *script_path* lives under mise's Node installs dir (see
    :func:`_mise_node_installs_dir` — honours ``MISE_DATA_DIR`` /
    ``XDG_DATA_HOME``), return the co-located ``bin/node``.  This avoids
    reliance on shim resolution which requires mise global config and a
    cooperative cwd.

    Resolves both $HOME and the script path to real paths to handle
    symlinked home directories (e.g. /home/user -> /local/home/user).
    """
    resolved = Path(script_path).resolve()
    mise_installs = _mise_node_installs_dir().resolve()
    try:
        rel = resolved.relative_to(mise_installs)
        version_dir = mise_installs / rel.parts[0]
        node_bin = version_dir / "bin" / "node"
        if platform_compat.is_executable_file(node_bin):
            return str(node_bin)
    except (ValueError, IndexError):
        pass
    return None


_UNRESOLVED: object = object()  # sentinel for "not yet resolved"
_claude_acp_argv_cache: list[str] | None | object = _UNRESOLVED


def _vendored_claude_acp_roots(pkg_dir: Path | None = None) -> list[Path]:
    """Directories that may contain a project-local ``node_modules`` copy of
    the claude-agent-acp adapter.

    A project-local install (``npm i @agentclientprotocol/claude-agent-acp`` in
    the repo, or a copy bundled next to the installed package) lets the gateway
    run without a global npm install — useful in non-login launchd/systemd
    contexts with a minimal PATH.  Resolution still falls back to global / PATH
    installs in ``_resolve_claude_acp_bin``; these roots are just preferred.

    *pkg_dir* (the installed ``kiro_crew`` package directory) defaults to this
    module's location; it is a parameter so tests can inject a fake layout.
    """
    roots: list[Path] = []

    # 1. Bundled alongside the installed package (optional vendored copy).
    if pkg_dir is None:
        pkg_dir = Path(__file__).resolve().parent.parent  # .../kiro_crew
    roots.append(pkg_dir / "_vendor" / "node_modules")

    # 2. Explicit project dir (KIROCREW_PROJECT_DIR points at the repo root):
    #    its ``node_modules`` from a local ``npm install``.
    proj = os.environ.get("KIROCREW_PROJECT_DIR", "")
    if proj:
        roots.append(Path(proj) / "node_modules")

    return roots


def _resolve_vendored_claude_acp(pkg_dir: Path | None = None) -> str | None:
    """Return the path to a vendored claude-agent-acp entry script, or None.

    Looks for ``<root>/@agentclientprotocol/claude-agent-acp/dist/index.js``
    under each candidate ``node_modules`` root.  Returns the first existing
    entry script (a plain Node script — the caller wraps it with ``node``).

    A root is accepted only when the adapter's hoisted dependency marker
    (``@agentclientprotocol/sdk``) is also present, so an incomplete vendored
    copy (entry script but missing deps) is skipped in favour of a complete
    one rather than picked and crashed at ESM import time.
    """
    for root in _vendored_claude_acp_roots(pkg_dir):
        entry = root / _CLAUDE_ACP_PKG_ENTRY
        if entry.is_file() and (root / _CLAUDE_ACP_DEP_MARKER).is_dir():
            return str(entry)
    return None


def _resolve_claude_acp_bin() -> list[str] | None:
    """Find the claude-agent-acp Node entry script and return argv.

    Returns a list suitable for subprocess argv (e.g. ``["node", "script.js"]``
    or ``["/path/to/binary"]``).  Explicitly resolves the node binary to
    avoid relying on ``#!/usr/bin/env node`` shebang resolution which fails
    in non-interactive daemon contexts (mise shims require cwd with
    .mise.toml or a working global config).

    Resolution order:
      1. ``CLAUDE_AGENT_ACP_BIN`` env var (explicit override; need not be
         executable — non-executable scripts are auto-wrapped with node).
      2. Project-local ``node_modules`` copy (from ``npm install`` in the repo
         or a copy bundled next to the package) — no global install required.
      3. ``mise which claude-agent-acp`` (respects all mise config).
      4. Direct glob under mise installs (fallback if mise exec fails).
      5. Augmented PATH (includes mise shims, nvm, fnm, volta, npm -g).
    """
    candidates: list[str] = []

    override = os.environ.get("CLAUDE_AGENT_ACP_BIN")
    if override and Path(override).is_file():
        candidates.append(override)

    # Project-local node_modules copy.  Preferred over PATH-based resolution
    # because it needs no global install and works in non-login gateway
    # contexts (launchd/systemd) with a minimal PATH.
    vendored = _resolve_vendored_claude_acp()
    if vendored:
        candidates.append(vendored)

    # Preferred: ask mise directly — respects MISE_DATA_DIR, global config,
    # and .mise.toml regardless of the user's installation layout.
    mise_resolved = _mise_which(CLAUDE_ACP_BIN)
    if mise_resolved:
        candidates.append(mise_resolved)

    # Fallback: search mise installs directory directly (handles case where
    # `mise which` fails due to missing global config in daemon context).
    mise_installs = _mise_node_installs_dir()
    if mise_installs.is_dir():
        for bin_path in sorted(mise_installs.glob("*/bin/" + CLAUDE_ACP_BIN), reverse=True):
            if bin_path.is_file():
                candidates.append(str(bin_path))
                break

    # Also search augmented PATH (includes mise shims) as fallback.
    # Covers nvm, fnm, volta, and plain `npm i -g` installations.
    search_path = augmented_path(os.environ.get("PATH", ""))
    on_path = shutil.which(CLAUDE_ACP_BIN, path=search_path)
    if on_path:
        candidates.append(on_path)

    for script in candidates:
        resolved = str(Path(script).resolve())
        node = _resolve_node_for_script(resolved)
        if node:
            return [node, resolved]
        # Directly runnable (a real executable on POSIX; a .exe/.cmd/etc. on
        # Windows)? Run it as-is. A bare .js is NOT directly runnable on Windows
        # (is_executable_file excludes it), so it correctly falls through to be
        # wrapped with node below — matching the POSIX no-x-bit behavior.
        # Casing-normalize (Windows): a `which`-resolved .EXE must reach a
        # launcher-style shim with its true on-disk name (see _normalize_exe_casing).
        if platform_compat.is_executable_file(script):
            return [_normalize_exe_casing(script) or script]
        node_on_path = shutil.which("node", path=search_path)
        if node_on_path:
            return [node_on_path, resolved]

    return None


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
    override = os.environ.get("CLAUDE_CODE_EXECUTABLE")
    if override and Path(override).is_file():
        return override

    mise_resolved = _mise_which(CLAUDE_CODE_BIN)
    if mise_resolved:
        return mise_resolved

    search_path = augmented_path(os.environ.get("PATH", ""))
    # Casing-normalize (Windows): a `which`-resolved .EXE reaches the launcher shim
    # with its true on-disk name (see _normalize_exe_casing).
    return _normalize_exe_casing(shutil.which(CLAUDE_CODE_BIN, path=search_path))


def _resolve_ssh_auth_sock(env: dict[str, str]) -> None:
    """Ensure SSH_AUTH_SOCK points to a live agent socket.

    The gateway's inherited value may be stale after an ssh-agent restart.
    Re-discovers the current agent socket without spawning a login shell.

    - macOS: launchd listener path changes on reboot
    - Linux: ssh-agent sockets live under /tmp/ssh-*/agent.*
    - Windows: no-op — there is no ``SSH_AUTH_SOCK`` (Win32 OpenSSH agent uses a
      named pipe, which needs no repair). Bare ``os.getuid()`` below would also
      ``AttributeError`` on win32, so return early; this function runs in the
      spawn prelude for BOTH ACP backends.
    """
    if platform_compat.IS_WINDOWS:
        return

    current = env.get("SSH_AUTH_SOCK", "")
    if current and os.path.exists(current):
        return  # already valid

    if sys.platform == "darwin":
        patterns = ["/tmp/com.apple.launchd.*/Listeners"]
    else:
        uid = os.getuid()
        patterns = [
            "/tmp/ssh-*/agent.*",
            f"/run/user/{uid}/ssh-agent.socket",
            f"/run/user/{uid}/keyring/ssh",
        ]

    for pattern in patterns:
        candidates = [p for p in glob.glob(pattern) if stat.S_ISSOCK(os.stat(p).st_mode)]
        if candidates:
            best = max(candidates, key=lambda p: os.path.getmtime(p))
            env["SSH_AUTH_SOCK"] = best
            logger.debug("Resolved SSH_AUTH_SOCK → %s", best)
            return


def _resolve_spawn_env(env: dict[str, str], *, kiro_api_key: bool = False) -> dict[str, str]:
    """Repair stale credential pointers in *env* before an agent spawn.

    Bundles :func:`_resolve_ssh_auth_sock` (glob + stat over ``/tmp``) and
    :func:`resolve_krb5_ccname` (lstat/stat of ``/tmp/krb5cc_<uid>``) so the
    spawn path pays ONE thread hop for both. Both resolvers issue synchronous
    filesystem syscalls whose latency scales with the ``/tmp`` entry count, so
    they must never run on the event loop — call this via
    ``asyncio.to_thread``. Mutates *env* in place and returns it for
    convenience.

    With ``kiro_api_key=True`` (the kiro-cli backend), also re-injects the
    CLI's own model credential from the data home's ``.env`` when the Docker
    entrypoint scrubbed it out of the parent environ — the child authenticates
    from its environment, so without this an API-key container loses model
    auth. With ``kiro_api_key=False`` (a foreign backend) the credential is
    actively STRIPPED instead: it is kiro-cli's alone, and the deny scrub
    deliberately exempts it, so an inherited copy would otherwise ride into a
    foreign agent process. The file read is IO, which is why both branches
    ride this same off-loop hop.
    """
    _resolve_ssh_auth_sock(env)
    resolve_krb5_ccname(env)
    # Deferred import: this module keeps config.loader off its import graph
    # (in-file convention; see the _prompt_timeout lazy-import note).
    from kiro_crew.config.loader import inject_kiro_cli_api_key, strip_kiro_cli_api_key

    if kiro_api_key:
        inject_kiro_cli_api_key(env)
    else:
        strip_kiro_cli_api_key(env)
    return env


# Subprocess stdout buffer — kiro-cli can send large JSON-RPC lines (tool outputs)
_STDOUT_BUFFER_LIMIT = 10 * 1024 * 1024  # 10MB
# Ceiling on the bytes discarded while draining ONE oversize line. Per drain call
# and expressed in BYTES: each call provably ends ON a frame boundary, so a replay
# of many legitimately-oversize-but-terminated frames each gets its own budget and
# stays survivable. A count of oversize FRAMES would kill the runtime on exactly
# that replay. Only a single blob that never terminates can exhaust this.
_OVERSIZE_DRAIN_MAX_BYTES = 16 * _STDOUT_BUFFER_LIMIT  # 160MB


class OversizeLineUnrecoverable(Exception):
    """An oversize stdout line exceeded the drain budget without terminating."""


async def _drain_oversize_line(
    reader: asyncio.StreamReader, exc: asyncio.LimitOverrunError
) -> int:
    """Discard one oversize line ENTIRELY, leaving the stream on a frame boundary.

    Called after ``readuntil(b"\\n")`` raised ``LimitOverrunError``, which consumes
    nothing. ``exc.consumed`` is the already-buffered prefix that provably holds no
    separator, so consuming it cannot cross into the next frame; retrying
    ``readuntil`` then either returns the remainder of the line or raises for
    another step. Same consume-prefix-and-retry drain as
    ``mcp_gateway/backend.py::run_stdout_pump``; a plain ``read(n)`` would instead
    eat into the NEXT frame.

    The recovered remainder is **discarded, never parsed**. It is a byte-slice of
    the line cut at an arbitrary offset, so it can split a multibyte UTF-8
    character — and ``json.loads`` on that raises ``UnicodeDecodeError``, which is
    NOT a ``json.JSONDecodeError`` and would escape the caller's non-JSON guard
    into its crash handler, killing every multiplexed session over one oversize
    frame.

    Returns the bytes discarded. Raises ``OversizeLineUnrecoverable`` past
    ``_OVERSIZE_DRAIN_MAX_BYTES`` (the stream is garbage, not merely verbose) and
    propagates ``IncompleteReadError`` on EOF mid-drain so the caller can use its
    normal end-of-stream path.
    """
    discarded = 0
    while True:
        if exc.consumed <= 0:
            # Unreachable via CPython, whose consumed always exceeds the reader's
            # limit; guarded because a zero would make this loop spin without
            # awaiting and starve the event loop.
            raise OversizeLineUnrecoverable(
                f"stream reported a {exc.consumed}-byte oversize prefix"
            )
        discarded += len(await reader.readexactly(exc.consumed))
        if discarded > _OVERSIZE_DRAIN_MAX_BYTES:
            raise OversizeLineUnrecoverable(
                f"discarded {discarded} bytes with no frame boundary "
                f"(limit {_OVERSIZE_DRAIN_MAX_BYTES})"
            )
        try:
            return discarded + len(await reader.readuntil(b"\n"))
        except asyncio.LimitOverrunError as again:
            exc = again

# Max consecutive empty reads before checking if process is alive
_MAX_CONSECUTIVE_EMPTY = 5

# Cap the structured-tool-params cache so a stream of ToolCall notifications with
# no matching request_permission can't grow it without bound (the entries are
# popped on the permission event and wholesale-cleared per prompt; this is just a
# backstop for the pathological no-permission case).
_MAX_CACHED_TOOL_PARAMS = 256

#: Basename a skill body lives under. Duplicated from ``skills`` deliberately —
#: the ACP layer must not import the skills machinery just to test a substring.
_SKILL_FILE_BASENAME = "SKILL.md"

#: Backstop on the per-session set of tool-call ids already credited as skill
#: reads. Far above any real turn's distinct skill reads; bounds memory for a
#: long-lived session at the cost of at most one duplicate credit after a reset.
_MAX_NOTED_SKILL_READS = 512


def _mentions_skill_file(raw_params: dict | None, command: str | None) -> bool:
    """Whether a tool call's arguments name a skill body at all.

    A cheap pre-filter so observing skill reads costs a substring scan on the
    overwhelming majority of tool calls, which touch no skill. Scans only string
    and string-sequence values, since a model-authored argument dict may hold
    arbitrary shapes.
    """
    if isinstance(command, str) and _SKILL_FILE_BASENAME in command:
        return True
    if not isinstance(raw_params, dict):
        return False
    for value in raw_params.values():
        if isinstance(value, str):
            if _SKILL_FILE_BASENAME in value:
                return True
        elif isinstance(value, (list, tuple)):
            if any(
                isinstance(v, str) and _SKILL_FILE_BASENAME in v for v in value
            ):
                return True
    return False


# Emitted by kiro-cli as a plain agent_message_chunk when its built-in, non-overridable
# security filter cancels every tool use in an assistant turn (e.g. shell commands
# containing "credentials").  After this text kiro-cli returns to an idle state waiting
# for the next user prompt and NEVER sends a ``complete`` response for the in-flight
# ``session/prompt`` — so without special handling KiroCrew waits the full 2h timeout.
# Treating this chunk as end-of-turn unblocks the caller; the text itself is still
# yielded so the user/agent sees what happened.  We use an exact (stripped) match so
# the detection does not fire if the model merely quotes the marker string in prose.
_TOOL_INTERRUPTED_MARKER = "Tool uses were interrupted, waiting for the next user prompt"


def _is_tool_interrupted_marker(chunk: str) -> bool:
    """Exact match against the kiro-cli security-filter interrupt marker."""
    return chunk.strip() == _TOOL_INTERRUPTED_MARKER


# Timeouts for session initialization steps
_INIT_TIMEOUT = 240.0  # 4 min — MCP servers can be slow to initialize
# set_mode/set_model: fire-and-forget.  kiro-cli accepts these commands
# but usually never sends a JSON-RPC response — MCP servers load
# asynchronously.  Any late responses land in _buffer and are harmlessly
# skipped by _process_message() during the next prompt read loop.
_DRAIN_DURATION = 1.0  # hard cap on draining MCP server init notifications
# Idle early-exit: once no init notification has arrived for this long, MCP
# servers have gone quiet and we stop draining instead of always waiting the full
# _DRAIN_DURATION. The cap still bounds genuinely slow servers; the idle window
# short-circuits the common fast case (servers quiet well under the cap), cutting
# time-to-first-token on new sessions without risking a missed banner from an
# active server. Must stay strictly below _DRAIN_DURATION, otherwise the hard cap
# fires first and the idle path becomes dead code.
_DRAIN_IDLE_EXIT = 0.5
_DEFAULT_PROMPT_TIMEOUT = 7200.0  # 2 hours — allow very long tool execution
# Slack the transport leaves ABOVE the configured turn ceiling. The dashboard's
# own deadline (turn_dispatch._bounded_turn) must always fire first so the user
# sees the "turn hit the N-hour limit" card; a transport cut at the same instant
# would race it and report a raw timeout instead.
_PROMPT_TIMEOUT_MARGIN_SECS = 60.0


def prompt_timeout_for_ceiling(configured: float) -> float:
    """Pure transport-timeout math for an already-known turn ceiling.

    Extracted from :func:`resolve_prompt_timeout` so callers that ALREADY hold
    a loaded config (e.g. ``session_handle._load_watchdog_settings``) can bound
    against the ceiling without a second synchronous ``KiroCrewConfig.load()``.
    """
    if configured <= 0:
        return _DEFAULT_PROMPT_TIMEOUT
    if configured <= _DEFAULT_PROMPT_TIMEOUT:
        # At or below the default the transport keeps its historical wait —
        # byte-identical behaviour for every existing install. The margin is
        # only added ABOVE the default, where the transport must outlive the
        # raised dashboard ceiling.
        return _DEFAULT_PROMPT_TIMEOUT
    return configured + _PROMPT_TIMEOUT_MARGIN_SECS


def resolve_prompt_timeout() -> float:
    """Per-prompt transport timeout, honouring the configured turn ceiling.

    ``agent.chat_turn_timeout_secs`` may be raised above
    :data:`_DEFAULT_PROMPT_TIMEOUT` (up to the loader's ``CHAT_TURN_TIMEOUT_MAX``)
    for long unattended turns. The transport wait must then outlive the
    dashboard's ceiling — otherwise the transport cuts the turn first and the
    larger configured value is a limit the system does not honour (the exact
    dishonesty ``turn_dispatch.chat_turn_timeout_secs`` clamps against).

    Never returns less than :data:`_DEFAULT_PROMPT_TIMEOUT`: a LOWERED turn
    ceiling is enforced by the dashboard's own deadline, and shrinking the
    transport wait with it would also shrink the budget of non-dashboard
    callers (subagents, review runs) that share this default.

    Config is imported lazily: ``config.loader`` reaches this module through
    ``acp.session_handle``, so a module-level import would be a cycle.
    """
    try:
        from kiro_crew.config.loader import KiroCrewConfig

        configured = float(KiroCrewConfig.load().agent.chat_turn_timeout_secs)
    except Exception:
        logger.debug("turn-ceiling config unavailable; transport keeps default", exc_info=True)
        return _DEFAULT_PROMPT_TIMEOUT
    return prompt_timeout_for_ceiling(configured)


def _effective_prompt_timeout(timeout: float | None) -> float:
    """An explicit caller timeout wins; ``None`` resolves from config."""
    return float(timeout) if timeout is not None else resolve_prompt_timeout()


async def _effective_prompt_timeout_async(timeout: float | None) -> float:
    """Async twin of :func:`_effective_prompt_timeout` for prompt dispatch.

    The ``None`` path reads config from disk (:func:`resolve_prompt_timeout`
    → ``KiroCrewConfig.load()``: stat, read, validate), so resolving it inline
    in an ``async def`` would block the event loop for every session sharing
    it. Offload to a thread, matching this module's convention for filesystem
    work (see ``_resolve_kiro_bin_async``).
    """
    if timeout is not None:
        return float(timeout)
    return await asyncio.to_thread(resolve_prompt_timeout)


_READ_TIMEOUT = 20.0
# After a compaction `completed` status, kiro-cli emits a fresh
# `_kiro.dev/metadata` with the real post-compaction contextUsagePercentage
# about ~1s later (live-probe confirmed). Wait up to this long for it so the
# meter can report accurate numbers instead of the reset/unknown fallback.
_POST_COMPACTION_METADATA_GRACE_SECS = 5.0
# After streaming content, if no new data arrives for this many seconds,
# treat the turn as done.  Handles kiro-cli silently finishing without
# sending the JSON-RPC `result` response.
_STALE_TURN_TIMEOUT = 90.0
# After a tool is DISPATCHED, if no data of ANY kind (tool result, progress
# update, permission request, completion) arrives for this many seconds, treat
# the turn as a dead stall and exit.  Unlike _STALE_TURN_TIMEOUT this does NOT
# require _stale_eligible (which is cleared the moment a tool_call is yielded),
# so it catches the "tool dispatched but never resolves" hang that otherwise
# runs to the caller's full prompt timeout — e.g. a cron job dispatching a tool
# that silently never returns, burning the whole job timeout.  Long real tools
# keep resetting the timer via tool_call_update progress frames and tool
# results, so this only trips on a genuine stall.
_TOOL_STALL_TIMEOUT = 600.0
_CANCEL_GRACE_SECS = 10.0  # grace window for cooperative cancel ack
# Absolute safety cap for _wait_for_response's activity-based deadline. The
# per-call deadline resets on every received frame (so a long session/load
# replay that streams the whole transcript as notifications is not killed),
# but never extends past this hard ceiling.
_WAIT_RESPONSE_MAX_TIMEOUT = 600.0  # 10 min absolute ceiling
# Upper bound on the offloaded ACP-layer SEL audit emit. Auditing is best-effort
# and must never gate tool dispatch, so a wedged SEL backend is abandoned (the
# worker thread may leak, which is survivable) after this timeout.
_SEL_AUDIT_TIMEOUT_SECONDS = 5.0
# JSON-RPC 2.0 reserved error code for an unrecognized method — used to answer
# unknown server→client requests so the agent fails fast instead of hanging.
_JSONRPC_METHOD_NOT_FOUND = -32601


# Legacy kiro permission options omit the spec-mandated `kind` field. Only
# synthesize a kind for these well-known literals — unknown ids stay empty
# so we don't fabricate intent the agent didn't express.
_LEGACY_OPTION_KIND: dict[str, str] = {
    OPTION_ALLOW_ONCE: "allow_once",
    "allow": "allow_once",
    OPTION_ALLOW_ALWAYS: "allow_always",
    "reject_once": "reject_once",
    "reject_always": "reject_always",
}

# Canonical ACP tool-kind value for shell/exec tools. kiro-cli and
# claude-agent-acp both report shell commands with kind="execute". This is the
# ONE place the ACP shell literal lives — _is_shell_kind() maps it to the
# provider-agnostic AcpEvent.is_shell flag the dashboard validates against.
_ACP_SHELL_KIND = "execute"


def _is_shell_kind(kind: str | None) -> bool:
    """True when an ACP tool_kind denotes a shell/exec command."""
    return kind == _ACP_SHELL_KIND


class AcpError(Exception):
    """Base ACP error.

    ``transient`` carries the retry-eligibility verdict computed from the RAW
    JSON-RPC error at raise time (see :func:`_is_transient_raw_error`), so the
    retry layer (``llm_helpers``, ``chat_runner``) decides retryability
    independently of how :func:`_format_acp_error` words the user-facing
    message. ``None`` means "unclassified" — callers fall back to
    string-matching the formatted message.
    """

    def __init__(self, *args: object, transient: bool | None = None) -> None:
        super().__init__(*args)
        self.transient = transient
        # Reactive-fallback metadata, set by :func:`_raise_acp_error` when a
        # prompt-time error names a rejected model (so run_bg_oneliner can retry
        # once with a served model). Guarded so AcpModelUnavailable — which sets
        # ``advertised`` BEFORE calling super().__init__ — is not clobbered.
        if not hasattr(self, "rejected_model"):
            self.rejected_model: str | None = None
        if not hasattr(self, "advertised"):
            self.advertised: list[str] = []


class AcpTimeoutError(AcpError):
    """Prompt timed out."""

    def __init__(self, partial_output: str = ""):
        self.partial_output = partial_output
        super().__init__("ACP prompt timed out")


class AcpPermissionNeeded(AcpError):  # noqa: N818
    """Tool approval required."""

    def __init__(self, prompt: str, response_so_far: str = ""):
        self.prompt = prompt
        self.response_so_far = response_so_far
        super().__init__("Permission needed")


class AcpProcessDied(AcpError):  # noqa: N818
    """kiro-cli process exited unexpectedly."""


class AcpAuthRequired(AcpError):  # noqa: N818
    """kiro-cli is not authenticated — the user must run ``kiro-cli login``.

    Non-retryable: respawning the process hits the same wall, so callers must
    surface the actionable message and skip the retry ladder rather than
    reset-and-requeue the turn.
    """


class AcpModelUnavailable(AcpError):  # noqa: N818
    """An explicitly requested model is not available to this account.

    A DISTINCT type because the semantics differ from every other ``set_model``
    failure. The generic ones ("the call didn't land") are legitimately handled
    by tearing the session down and cold-starting. This one means "the request
    itself is invalid, and no amount of restarting changes that" — falling back
    to a reset would destroy a live conversation and then quietly land on a
    different model, reporting success. Callers must surface it (4xx / user
    error), not recover from it.

    Non-retryable: ``transient`` is fixed False, since no retry earns an
    entitlement.
    """

    def __init__(self, model_id: str, advertised: Sequence[str] | None = None) -> None:
        self.model_id = model_id
        self.advertised = list(advertised or [])
        usable = ", ".join(self.advertised) if self.advertised else "none advertised"
        # The identity hint is CONDITIONAL by construction ("if you expected") and
        # names only a read-only probe. A user genuinely on a free tier is
        # correctly served by the first sentence and should not be nudged toward
        # re-authenticating, so this must never read as an instruction to log out:
        # `whoami` answers "which tier am I actually on" for the user who signed in
        # to the wrong one, and merely confirms the situation for everyone else.
        super().__init__(
            f"The model {model_id!r} is not available on your account. "
            f"Available models: {usable}. "
            f"If you expected this model to be included in your plan, check which "
            f"account you are signed in as with `kiro-cli whoami` — a Builder ID "
            f"sign-in carries a different entitlement than organization SSO.",
            transient=False,
        )


class AcpPromptBusy(AcpError):  # noqa: N818
    """A prompt is already in progress on this session.

    The backend still has an in-flight prompt (tool stall, timeout, or race
    between messages). Callers should reset the session so the next message
    cold-starts cleanly.
    """


# kiro-cli emits a "not logged in" banner on stderr when the user's session
# has expired. Detected during spawn/prompt so we can raise AcpAuthRequired
# (non-retryable) instead of churning through the retry ladder.
_NOT_LOGGED_IN_RE = re.compile(r"not\s+logged\s+in", re.IGNORECASE)
_NOT_LOGGED_IN_MESSAGE = (
    "kiro-cli is not logged in. Run `kiro-cli login` in your terminal, " "then start a new chat."
)


# ── Transient-error classification (shared by _format_acp_error and
# _is_transient_raw_error) ──
#
# Single source of truth for "is this ACP backend error a momentary,
# retry-worthy hiccup?". The user-facing message formatter AND the
# retry-eligibility classifier both key off these patterns, so the two can
# never drift again: otherwise the formatter could rewrite a generic 5xx into a
# friendly string the marker-based retry classifier does not recognise, silently
# preventing the retry from firing.
#
# Scopes mirror _format_acp_error's if/elif chain: model-unavailable matches
# the provider `data` field only (it extracts the model name from a structured
# string); throttle, auth, and the 5xx family match the combined
# `data + message` haystack so a 5xx token in either field is caught.
_RE_MODEL_UNAVAILABLE = re.compile(r"[Tt]he model '([^']+)' is not available")
# kiro-cli >= 2.16 rewording of the same capacity/rollout rejection, which
# names NO model: "The model you've selected is temporarily unavailable.
# Please use '/model' to select a different model and try again." Without its
# own pattern this wording fell through to the unknown-shape branch and was
# classified terminal, so unattended callers (cron, subagents, consolidation)
# failed fast on a momentary blip their retry ladder exists to absorb — the
# exact drift hazard the marker-coupling note above warns about. The quote
# class covers both the straight and typographic apostrophe in "you've".
_RE_MODEL_TEMP_UNAVAILABLE = re.compile(
    r"[Tt]he model you['\u2019]ve selected is temporarily unavailable"
)
# MPS ValidationException wording for a model the partition/account does not
# serve — distinct from the "is not available" capacity string above. Covers the
# ``auto`` sentinel in partitions that do not serve it.
_RE_INVALID_MODEL_ID = re.compile(r"[Ii]nvalid model ID:\s*([^\s,;'\"]+)")
_RE_THROTTLE_NAMED = re.compile(
    r"\b(ThrottlingException|TooManyRequestsException|ServiceQuotaExceededException)\b"
)
_RE_THROTTLE_GENERIC = re.compile(r"\b(rate.?limit|throttl(?:e|ed|ing))\b", re.IGNORECASE)
_RE_AUTH = re.compile(
    r"\b(AccessDenied(?:Exception)?|UnauthorizedException|ExpiredToken(?:Exception)?"
    r"|InvalidSignatureException|UnrecognizedClientException)\b"
)
_RE_5XX_NAMED = re.compile(
    r"\b(InternalServerError|InternalFailure|ServiceUnavailable(?:Exception)?"
    r"|DispatchFailure|ConnectionReset(?:Error)?)\b"
)
_RE_5XX_STATUS = re.compile(r"(?:HTTP|status)\s*(?:code\s*)?(?:50[0234]|529)\b", re.IGNORECASE)
# Genuine retry hint only. "response stream" is deliberately NOT matched here,
# because that would make this branch a catch-all: kiro-cli wraps EVERY mid-stream
# provider failure as "Encountered an error in the response stream: <real cause>",
# so the wrapper prefix alone — present on quota exhaustion, validation errors,
# anything — would classify the error as a momentary 5xx, tell the user to retry,
# and DISCARD the real cause. A monthly-usage-limit rejection would surface as
# "The model backend hit a transient error (HTTP 5xx)" and burn the retry ladder.
# The wrapper is a transport envelope, not a signal about the failure inside it;
# classification reads the inner detail (see _provider_detail).
_RE_5XX_HINT = re.compile(r"(please try again)", re.IGNORECASE)
# Session expiry, by HTTP status. An expired session is rejected with 401/403,
# and nothing else in this module recognised those codes: the error fell through
# to the 5xx family (a co-occurring DispatchFailure/ConnectionReset from the
# aborted request is enough to match) and the user was told to retry or switch
# models, neither of which can succeed against an expired login. Status is the
# primary signal because the rejection carries no explanatory wording.
_RE_AUTH_STATUS = re.compile(r"(?:HTTP|status)\s*(?:code\s*)?(?:401|403)\b", re.IGNORECASE)
# Session expiry, by wording. Complements the status match for backends that
# describe the expiry in prose without a machine-readable code. Deliberately
# excludes Bedrock's named exceptions, which _RE_AUTH already owns.
_RE_SESSION_EXPIRED = re.compile(
    r"\b(?:session\s+(?:has\s+)?expired|session\s+timed?\s*out"
    r"|login\s+(?:has\s+)?expired|authentication\s+(?:has\s+)?expired"
    r"|not\s+logged\s+in|not\s+authenticated"
    r"|re-?authenticate|login\s+required|auth(?:entication)?\s+required)\b",
    re.IGNORECASE,
)
# Credential REJECTED rather than expired. Switching the active Kiro account
# invalidates the credential a long-lived kiro-cli child still holds, and the
# upstream rejection reports only that the bearer token is invalid: it carries
# no status code and never uses expiry wording, so neither _RE_AUTH_STATUS nor
# _RE_SESSION_EXPIRED matches it and the failure reaches the user as the raw
# upstream string with no sign-in affordance. Grouped with session expiry
# because the remedy is identical — sign in again; no retry can make a rejected
# credential valid. The gap between the two words is fenced to one sentence and
# one line so the pattern cannot span unrelated errors in a combined haystack.
_RE_INVALID_BEARER = re.compile(
    r"\b(?:bearer\s+token\b[^.\n]{0,80}?\binvalid|invalid\s+bearer\s+token)\b",
    re.IGNORECASE,
)


def _is_session_expired(haystack: str) -> bool:
    """True when the session credential is expired or rejected, not a backend fault.

    All three signals are terminal: retrying can neither refresh a login nor
    revive a credential the upstream has rejected. Checked before the 5xx family
    so an aborted request's transport error does not shadow the real cause.
    """
    return bool(
        _RE_AUTH_STATUS.search(haystack)
        or _RE_SESSION_EXPIRED.search(haystack)
        or _RE_INVALID_BEARER.search(haystack)
    )


# Account/plan capacity is EXHAUSTED — terminal. Distinct from a throttle: a
# throttle clears in seconds and a retry is the right move, whereas a spent
# monthly allowance does not come back until it resets, so retrying only adds
# latency before the same rejection. Checked BEFORE the throttle branch because
# some limit messages also carry rate-limit-ish wording.
_RE_USAGE_LIMIT = re.compile(
    r"\b(?:monthly|daily|weekly)\s+(?:usage\s+)?limit\b"
    r"|\busage\s+limit\s+has\s+been\s+reached\b"
    r"|\b(?:MonthlyLimitError|FreeTierLimitExceeded)\b",
    re.IGNORECASE,
)
# kiro-cli's generic wrapper for a backend generation failure that died BEFORE
# the response stream was established (so no request_id, no error class, and
# none of the tokens above). Observed a case where data was exactly "Kiro
# failed to generate a response" while an independent gateway was
# simultaneously getting model-unavailable for the same model family. These
# pre-stream failures are overwhelmingly momentary capacity or rollout blips,
# so they are retry-worthy. Matched against the provider `data` field only
# (like model-unavailable): the phrase is a provider wrapper, never JSON-RPC
# boilerplate, and scoping to `data` keeps a stray echo in `message` from
# flipping an otherwise-terminal error.
_RE_GENERATE_FAILED = re.compile(r"failed to generate a response", re.IGNORECASE)

# kiro-cli's wording for a concurrent in-flight prompt on the session, read by
# the user-facing formatter below and by `_raise_acp_error`'s AcpPromptBusy
# classification. One pattern, but two haystacks: the formatter scopes to the
# provider `data` field while the classifier also searches the JSON-RPC
# `message`, so an echo carried only by `message` raises AcpPromptBusy under the
# unrecognised-shape text.
_PROMPT_BUSY_RE = re.compile(r"already in progress", re.IGNORECASE)

# kiro-cli's envelope for a failure that happened mid-stream. The text after the
# colon is the provider's own message — the same words the CLI prints in a
# terminal — so it is what a user needs to see.
_RE_STREAM_ENVELOPE = re.compile(
    r"^\s*Encountered an error in the response stream:\s*", re.IGNORECASE
)
# Trailing "(request_id: ...)" is stripped from the detail because every branch
# re-appends it via req_id_suffix; leaving it would print the id twice.
_RE_TRAILING_REQ_ID = re.compile(r"\s*\(request_id:\s*[0-9a-fA-F-]+\)\s*$")


def _provider_detail(data: str) -> str:
    """The provider's own error text, unwrapped from kiro-cli's stream envelope.

    Returns "" when *data* carries nothing worth showing. Used by the
    unknown-shape fallback so an unrecognised provider failure surfaces its real
    message (CLI parity) instead of a ``repr`` of the JSON-RPC dict. Recognised
    failure modes keep their curated guidance and do not call this.
    """
    detail = _RE_STREAM_ENVELOPE.sub("", str(data or "")).strip()
    detail = _RE_TRAILING_REQ_ID.sub("", detail).strip()
    return detail


def _model_is_unentitled(data: str, available_models: Sequence[str] | None) -> str | None:
    """Return the rejected model name iff the account is not entitled to it.

    Upstream reports entitlement failures and transient capacity failures with
    the SAME string ("The model 'X' is not available"), so the string alone
    cannot tell them apart. The advertised model list can: it is captured at
    session init from what this account is actually served, so a rejected model
    that is absent from it was never on offer -- an entitlement problem no retry
    can fix. A rejected model that IS advertised really is a transient
    capacity/rollout blip.

    Returns None when the model is advertised, when nothing was rejected, or
    when *available_models* is None/empty (entitlement unknowable -- treat as
    transient rather than telling a user their plan lacks a model on no
    evidence).

    Both :func:`_format_acp_error` and :func:`_is_transient_raw_error` route
    through this single helper so the user-facing wording and the retry verdict
    cannot drift apart -- see the drift warning above.
    """
    match = _RE_MODEL_UNAVAILABLE.search(data)
    if not match:
        return None
    if not available_models:
        return None
    rejected = match.group(1)
    # Compare case-insensitively on the bare id: the rejection echoes back the
    # id that was sent, but casing has no meaning in these ids and an
    # entitled-but-differently-cased match must not be reported as unentitled.
    advertised = {m.strip().lower() for m in available_models if m and m.strip()}
    if rejected.strip().lower() in advertised:
        return None
    return rejected


def _is_transient_raw_error(
    error: object, available_models: Sequence[str] | None = None
) -> bool:
    """True iff a raw ACP JSON-RPC ``error`` is a retryable transient backend
    failure (Bedrock 5xx / throttle / model-unavailable rollout) rather than an
    auth/validation/unknown error that a retry cannot fix.

    Classifies from the RAW ``{code, message, data}`` — never the formatted
    user-facing string — so the retry decision is independent of message
    wording. :class:`AcpError` carries this verdict (``.transient``) to the
    retry layer (``llm_helpers``, ``chat_runner``). Precedence mirrors
    :func:`_format_acp_error`: unentitled-model(terminal) →
    usage-limit(terminal) → model-unavailable → throttle → auth(terminal) →
    generic 5xx / pre-stream generation failure → unknown(terminal).

    *available_models* is this account's advertised set when the caller knows
    it. It only ever makes the verdict MORE conservative: a model the account
    was never offered is terminal instead of being retried to no purpose. Omit
    it and behaviour is unchanged from before this parameter existed.
    """
    if not isinstance(error, dict):
        return False
    data = str(error.get("data", "") or "")
    message = str(error.get("message", "") or "")
    haystack = f"{data} {message}"
    if _model_is_unentitled(data, available_models):
        # Terminal: the account is not entitled to this model, so every retry
        # spends latency to reproduce the same rejection.
        return False
    if _RE_USAGE_LIMIT.search(haystack):
        # Terminal: the allowance is spent until it resets. Ahead of the throttle
        # check so limit wording that also reads as rate-limiting stays terminal.
        return False
    if _RE_MODEL_UNAVAILABLE.search(data):
        return True
    if _RE_MODEL_TEMP_UNAVAILABLE.search(data):
        # Nameless capacity rejection (kiro-cli >= 2.16 wording). Matched
        # against `data` only, like its named sibling above, so a phrase echo
        # in the JSON-RPC `message` can't flip an otherwise-terminal error.
        # No entitlement check is possible (the wording names no model), but
        # the bounded retry budget caps the cost if one ever slips through.
        return True
    if _RE_THROTTLE_NAMED.search(haystack) or _RE_THROTTLE_GENERIC.search(haystack):
        return True
    if _RE_AUTH.search(haystack):
        # Auth is terminal — a retry can't fix an expired/denied credential.
        return False
    if _is_session_expired(haystack):
        # Session expiry is terminal — retrying can't refresh an expired login.
        return False
    return bool(
        _RE_5XX_NAMED.search(haystack)
        or _RE_5XX_STATUS.search(haystack)
        or _RE_5XX_HINT.search(haystack)
        or _RE_GENERATE_FAILED.search(data)
    )


def advertised_model_ids(entries: object) -> list[str]:
    """Model ids out of an ``availableModels``-shaped list, defensively.

    The advertised list is remote input reshaped by several backends, so this
    tolerates anything that is not a list of ``{"modelId": ...}`` dicts and
    returns what it can. Shared by the three call sites that pre-flight a model
    so none of them re-derives the shape — and so a surprising payload degrades
    to "entitlement unknown" (empty list -> :func:`model_is_unusable` allows the
    send) instead of raising inside session startup.
    """
    if not isinstance(entries, (list, tuple)):
        return []
    ids: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        model_id = entry.get("modelId") or entry.get("value") or ""
        if isinstance(model_id, str) and model_id.strip():
            ids.append(model_id)
    return ids


def model_is_unusable(model_id: str, advertised: Sequence[str] | None) -> bool:
    """True when *advertised* is known and excludes *model_id*.

    The counterpart to :func:`_model_is_unentitled`, moved BEFORE the wire: that
    one explains a rejection after the fact, this one declines to send a model
    the backend already told us the account cannot run. Deliberately ONE shared
    predicate rather than a copy per call site — the same reason #1550 made the
    formatter and the retry classifier share a discriminator: two spellings of
    "can this account use it" would eventually disagree.

    Returns False — allow the send — whenever entitlement is unknowable: an
    empty/None advertised set (no session yet, or a backend that omits
    ``models``) must not be read as "nothing is allowed", which would withhold
    every model on a backend that simply does not advertise.

    Only meaningful where the advertised ids share a namespace with *model_id*,
    and callers gate on that. kiro-cli's advertised ids are exactly the ids
    ``session/set_model`` accepts, so an id absent from the list is genuinely
    unusable. The claude backend advertises BARE ids (``claude-opus-4-8[1m]``)
    while the configured model is the prefixed provider id
    (``global.anthropic.claude-opus-4-8[1m]``), so comparing those two
    namespaces would call every legitimate model unusable; that backend
    announces its own substitutions through the ``session/new`` advisory
    instead (see ``_new_session_following_substitution``).
    """
    if not advertised:
        return False
    wanted = model_id.strip().lower()
    return wanted not in {m.strip().lower() for m in advertised if m and m.strip()}


def resolve_usable_model(preferred: str, advertised: Sequence[str] | None) -> str:
    """Resolve a SUBSTITUTE (non-explicit) model choice to what the account can
    run, mirroring the interactive path's reset-to-default (``_wire_model_id``).

    Returns ``""`` to mean **"do NOT override — inherit the session's backend
    default"** (the served model ``session/new`` already assigned), so the wire
    never receives a model the partition does not serve. Rules:

      - empty ``preferred``           -> ``""`` (already inheriting the default);
      - ``advertised`` unknown/empty  -> ``""`` for the ``"auto"`` sentinel (never
        send a literal ``"auto"`` we cannot verify — some partitions do not
        serve it), else trust a concrete caller-supplied id (nothing to check
        it against);
      - ``"auto"``                    -> ``"auto"`` IFF the backend advertises it,
        else ``""`` — exactly ``_wire_model_id``'s
        ``"auto" if "auto" in advertised else ""``;
      - concrete + usable             -> that id;
      - concrete + not served         -> ``""`` (inherit the served default rather
        than substituting a possibly-unavailable ``"auto"``).

    The EXPLICIT user-pick paths do NOT use this: they ``raise``
    (``model_is_unusable``) so a user who chose a model sees an error, not a swap.
    A reactive retry (``run_bg_oneliner``) remains a thin backstop for the
    fail-open case where ``advertised`` was unknown at send time.
    """
    if not preferred:
        return ""
    if not advertised:
        return "" if preferred == "auto" else preferred
    ids = [m for m in advertised if m and m.strip()]
    if preferred == "auto":
        return "auto" if not model_is_unusable("auto", ids) else ""
    if not model_is_unusable(preferred, ids):
        return preferred
    return ""


def _format_acp_error(error: object, available_models: Sequence[str] | None = None) -> str:
    """Format a JSON-RPC error from the ACP backend into actionable user text.

    The ACP backend (kiro-cli or claude-agent-acp) surfaces upstream Bedrock
    failures as JSON-RPC ``error`` objects with shape
    ``{"code": int, "message": str, "data": str}``.  The ``data`` field
    typically contains the raw provider error string and a request_id.

    For known failure modes (model unavailable, throttling, auth) we rewrite
    the message into concrete recovery steps.  For everything else we fall
    back to the previous behaviour ``"Prompt error: <raw dict>"`` so we don't
    swallow new error shapes.

    The provider request_id is preserved in every variant so that operators
    can correlate against support tickets and Bedrock logs.

    Security: the ``data`` field originates from upstream and may contain
    credential patterns or exfiltration URLs (especially in the fallback
    path that echoes the raw dict).  The return value is therefore passed
    through ``redact_credentials`` and ``redact_exfiltration_urls`` before
    being raised to the dashboard / Slack / CLI surfaces.
    """
    if isinstance(error, dict):
        data = str(error.get("data", "") or "")
        message = str(error.get("message", "") or "")
        haystack = f"{data} {message}"

        req_id_match = re.search(r"request_id:\s*([0-9a-fA-F-]+)", data)
        req_id_suffix = f" (request_id: {req_id_match.group(1)})" if req_id_match else ""

        # Entitlement failure: this account was never offered the model, so the
        # capacity/rollout advice below would be actively misleading (there is
        # nothing to wait for). Checked FIRST because upstream uses the same
        # string for both cases.
        unentitled = _model_is_unentitled(data, available_models)
        if unentitled:
            usable = [m.strip() for m in (available_models or []) if m and m.strip()]
            # Cap the list: an account with many models would otherwise bury the
            # message, and the picker shows the full set anyway.
            shown = ", ".join(usable[:8])
            more = f" (+{len(usable) - 8} more)" if len(usable) > 8 else ""
            formatted = (
                f"Your account does not have access to model '{unentitled}'. "
                f"Available to you: {shown}{more}. Pick one in the model picker, "
                f"or set agent.model to 'auto' to let the backend choose a model "
                f"your plan includes. Retrying will not help."
                f"{req_id_suffix}"
            )
        # Bedrock model alias resolved to a version that is currently
        # unavailable (capacity throttle, region rollout in progress,
        # deprecated, etc.).
        elif _RE_USAGE_LIMIT.search(haystack):
            # Plan allowance exhausted. Quote the provider's own sentence rather
            # than paraphrasing it: it is the authoritative statement of WHICH
            # limit was hit, and the CLI shows exactly this, so the dashboard
            # matching it means the two surfaces cannot tell different stories.
            _limit_detail = _provider_detail(data)
            # The provider sentence has no trailing period, so add one before
            # appending guidance or the two run together as one sentence.
            if _limit_detail and _limit_detail[-1] not in ".!?":
                _limit_detail += "."
            formatted = (
                f"{_limit_detail} Retrying will not help until the limit resets. "
                f"Check your plan's usage allowance, or switch to a model or "
                f"account tier with remaining capacity."
                f"{req_id_suffix}"
            )
        elif _RE_MODEL_UNAVAILABLE.search(data):
            model = str(_RE_MODEL_UNAVAILABLE.search(data).group(1))  # type: ignore[union-attr]
            formatted = (
                f"Model '{model}' is unavailable on the backend right now "
                f"(capacity throttle or region rollout). Try: (1) pick a "
                f"different model in the model picker, (2) set agent.model to "
                f"'auto' in ~/.kiro/crew/config.json, or (3) wait a minute and "
                f"retry."
                f"{req_id_suffix}"
            )
        elif _RE_MODEL_TEMP_UNAVAILABLE.search(data):
            # Same capacity/rollout rejection as above in kiro-cli >= 2.16
            # wording, which names no model. Rewritten for the same reasons as
            # its named sibling: the provider's advice quotes the '/model' TUI
            # command, which does nothing in the dashboard, Slack, or a cron —
            # and the "is unavailable on the backend" prose keeps the
            # _TRANSIENT_MARKERS string fallback recognising it for free.
            formatted = (
                "The selected model is unavailable on the backend right now "
                "(capacity throttle or region rollout). Try: (1) pick a "
                "different model in the model picker, (2) set agent.model to "
                "'auto' in ~/.kiro/crew/config.json, or (3) wait a minute and "
                "retry."
                f"{req_id_suffix}"
            )
        elif _RE_THROTTLE_NAMED.search(haystack) or _RE_THROTTLE_GENERIC.search(haystack):
            # Bedrock throttle / rate limit. Cover both AWS service exception
            # names and the generic phrasing the ACP backend sometimes uses.
            formatted = (
                "Bedrock is throttling requests. Try: (1) wait a few seconds and "
                "retry, or (2) switch to a different model in the picker (e.g. sonnet)."
                f"{req_id_suffix}"
            )
        elif _RE_AUTH.search(haystack):
            # Bedrock auth failure — almost always missing/expired AWS
            # credentials.
            formatted = (
                "Bedrock authentication failed. Refresh your AWS credentials "
                "(e.g. re-run your SSO/login or 'aws sso login'), then retry. If "
                "the failure persists, check that the configured AWS profile has "
                "Bedrock InvokeModel access."
                f"{req_id_suffix}"
            )
        elif _is_session_expired(haystack):
            # Session expiry (401/403, or prose saying as much) — distinct from
            # the Bedrock credential errors above. Retrying or switching models
            # cannot succeed, so the message must not suggest either.
            formatted = (
                "Your session has expired. Run `kiro-cli login` in your "
                "terminal to sign back in, then start a new chat. "
                "Retrying or switching models will not help — this is a "
                "sign-in issue, not a backend error."
                f"{req_id_suffix}"
            )
        elif (
            _RE_5XX_NAMED.search(haystack)
            or _RE_5XX_STATUS.search(haystack)
            or _RE_5XX_HINT.search(haystack)
        ):
            # Transient backend 5xx — Bedrock/Codewhisperer surfaces a
            # momentary InternalServerError (often wrapped in a
            # CodewhispererChatResponseStream ServiceError with a
            # "please try again" hint). Distinct from throttling: this is a
            # server-side blip, not a rate limit, so the guidance is just to
            # retry rather than switch models or back off.
            #
            # Match the combined `data + message` haystack so a 5xx token in
            # either field is caught. Scanning `message` is safe
            # here precisely because we require a real transient *token* (named
            # exception, HTTP/status-50[0234]/529, or an explicit retry hint):
            # -32603's canonical message is literally "Internal error", which
            # carries no such token, so a bare uncaught -32603 (malformed-
            # request, context-length, deterministic backend bug) still falls
            # through to the unknown-shape branch rather than being mis-told to
            # retry a condition that will never succeed. A bare numeric like
            # "max_tokens 500" is likewise not a status code and won't match.
            formatted = (
                "The model backend hit a transient error (HTTP 5xx). This is "
                "usually momentary — retry in a moment. If it keeps happening, "
                "switch to a different model in the picker."
                f"{req_id_suffix}"
            )
        elif _RE_GENERATE_FAILED.search(data):
            # kiro-cli's generic pre-stream generation failure ("Kiro failed
            # to generate a response"): the backend call died before a
            # response stream existed, so there is no request_id and no error
            # class. Almost always a momentary model capacity / rollout blip
            # — same guidance as the 5xx branch. Kept as its own branch (not
            # folded into _RE_5XX_HINT) because it matches `data` only, so a
            # stray phrase echo in the JSON-RPC `message` can't flip an
            # otherwise-terminal error to transient.
            formatted = (
                "The model failed to generate a response (transient error — the "
                "backend call died before streaming started, usually a momentary "
                "capacity blip). Retry in a moment; if it keeps happening, switch "
                "to a different model in the picker."
                f"{req_id_suffix}"
            )
        elif _PROMPT_BUSY_RE.search(data):
            # The backend still has an in-flight prompt on this session.
            # This means a previous turn didn't complete cleanly (tool stall,
            # timeout, or race between messages). The session will auto-recover
            # on the next attempt once the stale turn expires.
            formatted = (
                "I'm still processing a previous request. Please wait a moment "
                "and try again — if it persists, send `!restart` to reset the session."
            )
        else:
            # Unrecognised failure mode. Show the PROVIDER'S OWN message when
            # there is one — it is the true error, and the same words the CLI
            # prints, so the two surfaces agree. This is the path every provider
            # failure without a curated branch above takes; an over-broad 5xx
            # match would swallow such an error and report a momentary blip, so
            # the real cause would never reach the user at all.
            #
            # Falls back to the raw dict only when there is no usable detail
            # (empty/odd data), so a genuinely opaque shape still loses nothing.
            # Redaction below scrubs any embedded secrets either way.
            _detail = _provider_detail(data)
            if _detail:
                # Keep the JSON-RPC `message` too when it carries signal. For
                # -32603 it is the fixed boilerplate "Internal error" (pure
                # noise next to the provider text), but other codes use it as
                # the actual summary, and dropping it there would lose the only
                # description the error has.
                _summary = "" if message.strip().lower() == "internal error" else message.strip()
                if _summary and _summary.lower() not in _detail.lower():
                    formatted = f"{_summary}: {_detail}{req_id_suffix}"
                else:
                    formatted = f"{_detail}{req_id_suffix}"
            else:
                formatted = f"Prompt error: {error}"
    else:
        formatted = f"Prompt error: {error}"

    # Defense-in-depth: scrub any credentials or suspicious exfiltration URLs
    # that may have been embedded in the upstream provider response before
    # the message reaches dashboard / Slack / CLI surfaces.
    redacted, url_warnings = redact_exfiltration_urls(formatted)
    redacted, cred_warnings = redact_credentials(redacted)
    if url_warnings or cred_warnings:
        # Log so security review can spot when an upstream provider is
        # echoing sensitive content back. The warning lists are bounded
        # (one entry per match) and intentionally do NOT include the matched
        # values themselves — those have already been redacted.
        logger.warning(
            "ACP error contained sensitive content (scrubbed before raise): "
            "%d suspicious url(s), %d credential pattern(s)",
            len(url_warnings),
            len(cred_warnings),
        )
    return redacted


# ---------------------------------------------------------------------------
def _rejected_model_from_error(error: object) -> str | None:
    """Return the model id a prompt-time error reports as invalid/unavailable.

    Powers the reactive fallback in ``run_bg_oneliner``: on the SUBSTITUTE
    (background) path, a rejected model is retried once against the account's
    advertised list. Matches both the MPS ``Invalid model ID: X``
    ValidationException (including the ``auto`` sentinel where a partition does
    not serve it) and the ``The model 'X' is not
    available`` wording. Returns None when the error names no specific model.
    """
    if not isinstance(error, dict):
        return None
    data = f"{error.get('data', '')} {error.get('message', '')}"
    m = _RE_INVALID_MODEL_ID.search(data) or _RE_MODEL_UNAVAILABLE.search(data)
    return m.group(1) if m else None


def _raise_acp_error(error: object, available_models: Sequence[str] | None = None) -> None:
    """Format and raise the appropriate AcpError subclass for *error*.

    Delegates formatting to ``_format_acp_error`` and raises either
    ``AcpPromptBusy`` (when the backend reports a concurrent in-flight prompt)
    or the generic ``AcpError`` for all other cases.

    *available_models* is passed to BOTH the formatter and the transient
    classifier so a model-rejection's wording and its retry verdict are decided
    from the same evidence.
    """
    formatted = _format_acp_error(error, available_models)
    # Detect prompt-busy from the raw error (before formatting rewrites it)
    raw_data = ""
    if isinstance(error, dict):
        raw_data = f"{error.get('data', '')} {error.get('message', '')}"
    if _PROMPT_BUSY_RE.search(raw_data):
        raise AcpPromptBusy(formatted)
    err = AcpError(formatted, transient=_is_transient_raw_error(error, available_models))
    # Tag a model-rejection so the SUBSTITUTE (background) retry layer can pick a
    # served model; harmless on every other error (attributes just stay unset).
    rejected = _rejected_model_from_error(error)
    if rejected:
        err.rejected_model = rejected
        err.advertised = list(available_models or [])
    raise err


# Matches claude-agent-acp policy-substitution advisories:
#   Model "X" is restricted by your organization's settings. Using Y instead.
# Emitted when admin-tier policy (managed-settings / policyHelper) or the
# Bedrock headless tier substitutes the requested model. The substitute is
# already in effect and the session is live; claude-agent-acp wraps it as a
# JSON-RPC -32603 error frame only because requested != applied. Informational,
# not fatal -- so we keep the session instead of raising.
_MODEL_SUBSTITUTION_ADVISORY_RE = re.compile(
    r"is\s+restricted\b.+\bUsing\s+\S+\s+instead",
    re.IGNORECASE | re.DOTALL,
)


def _extract_advisory_detail(error: object) -> str:
    """Pull the ``data.details`` (or plain string ``data``) out of an ACP error.

    Centralizes the shape-handling for the model-substitution advisory so the
    detector, the substitute-extractor, and the runtime advisory handler all
    move in lockstep when the advisory format evolves. Returns an empty string
    if the error is not a dict, has no data, or carries no details.
    """
    if not isinstance(error, dict):
        return ""
    data = error.get("data")
    if isinstance(data, dict):
        return str(data.get("details", "") or "")
    if isinstance(data, str):
        return data
    return ""


def _is_model_substitution_advisory(error: object) -> bool:
    """True iff *error* is a claude-agent-acp model-substitution advisory.

    The adapter emits this on session/new (and session/load /
    set_config_option) when policy substitutes the requested model. The
    substitution is already applied -- the session is live on the substitute
    -- but the response carries an error frame rather than a warning. Treat it
    as non-fatal: log and continue. The match is deliberately narrow (code
    -32603 AND a detail string containing BOTH 'is restricted' and 'Using X
    instead') so genuine -32603 internal errors, invalid params, malformed
    sessions, throttles, etc. still raise.
    """
    if not isinstance(error, dict):
        return False
    if error.get("code") != -32603:
        return False
    detail = _extract_advisory_detail(error)
    if not detail:
        return False
    return bool(_MODEL_SUBSTITUTION_ADVISORY_RE.search(detail))


# Captures the model id the backend says it will serve instead, from the same
# advisory: "... Using <model-id> instead." The substitute id is whitespace-free
# (e.g. ``global.anthropic.claude-sonnet-4-6[1m]``), so ``\S+`` lifts it cleanly.
_MODEL_SUBSTITUTE_RE = re.compile(
    r"\bUsing\s+(?P<model>\S+)\s+instead\b",
    re.IGNORECASE,
)


def _substitute_model_from_advisory(error: object) -> str | None:
    """Return the model id the gateway substituted to, or None.

    Parses the ``-32603`` advisory ("Model X is restricted ... Using Y
    instead.") and returns ``Y`` -- the model the gateway will actually serve.
    The caller adopts it and re-issues ``session/new`` so a real session is
    created (the advisory itself returns no sessionId). Returns None when the
    error is not a substitution advisory or the substitute can't be parsed.
    """
    if not _is_model_substitution_advisory(error):
        return None
    detail = _extract_advisory_detail(error)
    match = _MODEL_SUBSTITUTE_RE.search(detail)
    if not match:
        return None
    # Strip surrounding quotes/trailing punctuation a variant phrasing might add.
    model = match.group("model").strip().strip("\"'").rstrip(".,;")
    return model or None


def _get_child_pids(parent_pid: int | None, _visited: set[int] | None = None) -> list[int]:
    """Return PIDs of all descendants recursively (best-effort).

    Uses a visited set to prevent infinite loops from PID cycles.
    On Linux, reads /proc/<pid>/task/*/children (kernel-provided, fast).
    Falls back to pgrep -P on other platforms.
    """
    if not parent_pid:
        return []
    if _visited is None:
        _visited = set()
    if parent_pid in _visited:
        return []
    _visited.add(parent_pid)

    direct = _direct_children(parent_pid)
    all_pids = []
    for cpid in direct:
        if cpid not in _visited:
            all_pids.append(cpid)
            all_pids.extend(_get_child_pids(cpid, _visited))
    return all_pids


def _direct_children(pid: int) -> list[int]:
    """Return direct child PIDs. Uses /proc on Linux, pgrep on other POSIX.

    Windows: returns ``[]`` — there is no pgrep, and the tree kill goes through
    ``kill_process_tree`` (``taskkill /T``), which walks descendants itself, so
    the escaped-child sweep this feeds is a POSIX-only concern.
    """
    if platform_compat.IS_WINDOWS:
        return []
    if sys.platform == "linux":
        try:
            children: list[int] = []
            tasks_dir = Path(f"/proc/{pid}/task")
            if tasks_dir.is_dir():
                for tid in tasks_dir.iterdir():
                    cf = tid / "children"
                    if cf.exists():
                        children.extend(int(p) for p in cf.read_text().split() if p.strip())
            if children:
                return children
        except Exception:
            pass  # fall through to pgrep
    try:
        # timeout so a hung pgrep cannot occupy a subprocess_executor worker
        # indefinitely (the ps spawns in _get_start_time/_read_basename already
        # cap at 2s; this path must match or a wedged pgrep starves the pool).
        out = subprocess_mod.check_output(
            ["pgrep", "-P", str(pid)], stderr=subprocess_mod.DEVNULL, timeout=2
        )
        return [int(p) for p in out.decode().split() if p.strip()]
    except Exception:
        return []


def _get_start_time(pid: int) -> int | None:
    """Read process start time to detect PID recycling.

    Windows: returns ``None``. It feeds the POSIX-only escaped-child sweep
    (a no-op on win32, where ``taskkill /T`` already walks the tree), so a
    missing start time has no effect there and avoids spawning a failing ``ps``.
    """
    if platform_compat.IS_WINDOWS:
        return None
    try:
        if sys.platform == "linux":
            stat = Path(f"/proc/{pid}/stat").read_text()
            fields = stat.rsplit(")", 1)[1].split()
            return int(fields[19])  # field 22 = starttime
        # macOS: use ps -o lstart= (absolute start timestamp, constant for process lifetime)
        ps_bin = platform_compat.trusted_system_bin("ps")
        if ps_bin is None:
            return None
        out = subprocess_mod.check_output(
            [ps_bin, "-o", "lstart=", "-p", str(pid)], stderr=subprocess_mod.DEVNULL, timeout=2
        )
        return hash(out.strip())  # stable per-process, changes on recycle
    except Exception:
        return None


def finish_suspended_spawn(process: asyncio.subprocess.Process, pid: int, *, label: str) -> None:
    """Apply the Windows resource ceiling to a just-spawned child, then resume it.

    **Call this from an executor, never inline on the event loop.** On Windows it
    reads the config file (through ``apply_windows_resource_ceiling``) and walks
    two Toolhelp snapshots — the process table for the ownership check and the
    system-wide THREAD table to resume — so a slow config store or a loaded
    machine would otherwise stall every other session on that loop. Both ACP
    spawn sites wrap it in ``run_in_executor(subprocess_executor(), ...)``. It
    stays synchronous rather than becoming a coroutine because every step is
    blocking ctypes work with no await point to offer.

    Both ACP spawn sites (:meth:`AcpClient._spawn` and ``AcpRuntime._spawn``)
    create the session host with ``creationflags |=
    platform_compat.CREATE_SUSPENDED`` and call this immediately afterwards. On
    POSIX every step is a no-op — ``CREATE_SUSPENDED`` is 0 there, so the child
    was never suspended — which keeps one code path for both platforms.

    Why suspended: ``cgroup_scope_argv`` is a no-op on Windows (no systemd), so
    without this the agent and every MCP server it spawns would run with NO
    fork-bomb and NO memory-DoS ceiling. A Job object cannot be an argv prefix,
    so it must be attached to a live pid — and job membership covers a member's
    FUTURE descendants only. Attaching to an already-running kiro-cli would
    therefore leave a window in which it could spawn an MCP server that escapes
    the ceiling. ``CREATE_SUSPENDED`` closes that window by construction: the
    child has not executed a single instruction, so it provably has no
    descendants. Assign the job, then resume.

    A resume failure is FATAL, but only when the child is actually there: a
    process that exists yet cannot be resumed is alive-but-frozen, and letting it
    masquerade as a running agent would hang the session on the ACP handshake
    with no diagnosis. Kill it and raise instead. If the pid is already gone
    there is nothing frozen to worry about — it exited on its own — so note it
    and let the handshake surface the real error.

    The ceiling itself fails SOFT (``apply_windows_resource_ceiling`` logs a
    SECURITY warning and returns False): a missing ceiling must not break the
    gateway. Only the resume may abort the spawn, which is why it runs from a
    ``finally`` — a raising ceiling must still leave the child resumed or killed,
    never frozen.

    The two DESTRUCTIVE steps are gated on confirmed ownership: the pid's parent
    must be this process. A Job object would impose a process and memory ceiling
    on a stranger, and the unresumable branch KILLS what it is holding, so
    neither may ever act on a pid we did not create. The resume itself is NOT
    gated, because ``ResumeThread`` on a thread that is not suspended is a
    documented no-op (its suspend count is already 0) — and leaving our own child
    frozen would hang the session forever on the handshake with no diagnosis.
    That asymmetry is deliberate: an unconfirmed pid loses only its ceiling,
    which already fails soft by contract, while nothing can wedge or die by
    mistake.
    """
    owned = not platform_compat.IS_WINDOWS or platform_compat.get_ppid(pid) == os.getpid()
    try:
        if owned:
            apply_windows_resource_ceiling(pid)
        else:
            logger.debug(
                "PID %d is not a confirmed child of this process; skipping the Windows "
                "resource ceiling rather than bounding a foreign process",
                pid,
            )
    finally:
        if platform_compat.IS_WINDOWS and not platform_compat.resume_process_main_thread(pid):
            if owned and platform_compat.pid_exists(pid):
                logger.error(
                    "Could not resume suspended %s (PID %d); killing it rather than "
                    "leaving a frozen process that looks like a live agent",
                    label,
                    pid,
                )
                try:
                    process.kill()
                except Exception:
                    logger.debug("kill of unresumable child failed", exc_info=True)
                raise AcpError(
                    f"failed to resume {label} (PID {pid}) after applying Windows "
                    f"Job object resource limits"
                )
            logger.debug(
                "Nothing to resume for PID %d — it is gone, or not ours to kill; the "
                "handshake will report the real failure",
                pid,
            )


def _read_basename(pid: int) -> bytes | None:
    """Read the executable basename for a PID (platform-aware).

    POSIX only in practice — the escaped-child sweep that consumes this value is a
    Windows no-op — but the helper still short-circuits on win32 (returning None)
    so a stray future caller doesn't crash trying to invoke ``ps`` / read /proc.
    """
    if platform_compat.IS_WINDOWS:
        return None
    try:
        if sys.platform == "linux":
            cmdline_path = Path(f"/proc/{pid}/cmdline")
            if not cmdline_path.exists():
                return None
            cmdline = cmdline_path.read_bytes()
            if not cmdline:
                return None
            return cmdline.split(b"\x00", 1)[0].rsplit(b"/", 1)[-1]
        else:
            ps_bin = platform_compat.trusted_system_bin("ps")
            if ps_bin is None:
                return None
            out = subprocess_mod.check_output(
                [ps_bin, "-o", "comm=", "-p", str(pid)], stderr=subprocess_mod.DEVNULL, timeout=2
            )
            name = out.strip()
            if not name:
                return None
            return name.rsplit(b"/", 1)[-1]
    except Exception:
        return None


# Type alias for child PID records: (start_time, recorded_basename)
ChildRecord = tuple[int | None, bytes | None]


def _capture_child_records(pids: list[int]) -> dict[int, ChildRecord]:
    """Capture (start_time, basename) for each pid as a ChildRecord map.

    On macOS ``_get_start_time`` / ``_read_basename`` shell out to ``ps`` (and
    ``_get_child_pids`` to ``pgrep``), which can block during the subprocess
    spawn (fork/exec). Callers running on the event loop MUST invoke this via
    ``run_in_executor(subprocess_executor(), ...)`` so the spawns happen on a
    worker thread and never wedge the loop.
    """
    return {p: (_get_start_time(p), _read_basename(p)) for p in pids}


def _is_our_child(
    pid: int, expected_start: int | None = None, expected_basename: bytes | None = None
) -> bool:
    """Verify a PID still belongs to a process we spawned (deny-by-default).

    Compares recorded basename and start_time against live values. No hardcoded
    allowlist — any binary recorded at spawn time is automatically supervised.
    Returns False for recycled PIDs or unreadable processes.
    """
    try:
        # Start-time check: definitive PID recycling detection (always required)
        actual_start = _get_start_time(pid)
        if expected_start is None or actual_start is None:
            logger.debug("PID %d start time unavailable — denying (fail-closed)", pid)
            return False
        if actual_start != expected_start:
            logger.debug("PID %d start time mismatch (recycled)", pid)
            return False
        # Basename check: catches recycling to a different binary with same start slot.
        # Deny-by-default: if no basename was recorded (a legacy record predating
        # basename recording), we deny rather than skip the check. Returning False here
        # causes the caller (_kill_escaped_children) to SKIP the kill — we won't
        # SIGKILL a process we can't positively confirm is ours. This is the safe
        # direction: avoids killing a recycled PID that belongs to another user.
        # Trade-off: legacy in-memory records (no basename) are left alive until
        # the session restarts and re-records them with basenames.
        if expected_basename is None:
            logger.debug("PID %d has no recorded basename — denying (fail-closed)", pid)
            return False
        actual_basename = _read_basename(pid)
        if actual_basename is None:
            return False  # process gone
        if actual_basename != expected_basename:
            logger.debug(
                "PID %d basename mismatch (recorded=%r, actual=%r)",
                pid,
                expected_basename,
                actual_basename,
            )
            return False
        return True
    except Exception:
        return False


def _kill_escaped_children(child_pids: dict[int, int | None] | dict[int, ChildRecord]) -> None:
    """SIGKILL descendants that survived killpg (different PGID). Kills leaf-first.

    POSIX-only sweep: it cleans up children that reparented out of the killed
    process group (e.g. MCP servers). On Windows there are no process groups —
    ``kill_process_tree`` already used ``taskkill /T`` to walk the whole child
    tree — so there is nothing left to sweep, and the raw ``os.kill`` /
    ``signal.SIGKILL`` below are unavailable there. No-op on win32.
    """
    if platform_compat.IS_WINDOWS:
        return
    for cpid in reversed(list(child_pids.keys())):
        try:
            os.kill(cpid, 0)  # still alive?
            record = child_pids.get(cpid)
            # Support both old (int|None) and new (tuple) record shapes
            if isinstance(record, tuple):
                expected_start, expected_basename = record
            else:
                expected_start = record
                expected_basename = None
            if not _is_our_child(
                cpid, expected_start=expected_start, expected_basename=expected_basename
            ):
                logger.debug("Skipping PID %d — not our process (recycled?)", cpid)
                continue
            os.kill(cpid, signal.SIGKILL)
            logger.debug("Killed escaped child PID %d", cpid)
        except (ProcessLookupError, OSError):
            pass


def _make_unified_diff(old: str, new: str, path: str, max_len: int = 6000) -> str:
    """Generate a unified diff string from old/new text, handling empty inputs."""
    old_lines = (old if old.endswith("\n") else old + "\n").splitlines(keepends=True) if old else []
    new_lines = (new if new.endswith("\n") else new + "\n").splitlines(keepends=True) if new else []
    udiff = difflib.unified_diff(old_lines, new_lines, fromfile=path, tofile=path, n=3)
    return "".join(udiff).rstrip()[:max_len]


def _select_tool_title(
    title: object,
    raw_input: object,
    kind: object = None,
    *,
    is_shell: bool | None = None,
) -> str | None:
    """Pick the pill label, preferring a human-readable `description` when present.

    Some backends' Bash tool emits a `description` field alongside `command`
    (e.g. "List KiroCrew ACP module files" rather than `ls /workplace/...`).
    We surface it on the pill when supplied, then the literal shell command for
    a shell tool, and only then the SDK-provided `title`. Used by both
    `_extract_tool_event` (initial tool_call) and
    `_extract_tool_call_refinement` (the second-phase tool_call_update from
    claude-agent-acp) so the title rule stays consistent across both events.

    The command outranks `title` because backends disagree on what `title`
    holds for a shell call: some send the invocation itself, others a generic
    kind label ("Run Command") that names no command at all. A genuinely
    human-readable label arrives as `description`, which still wins.

    `is_shell` overrides the kind-derived classification for a caller holding a
    RESOLVED signal — a tool_call_update may omit `kind` entirely, and reading
    that absence as non-shell would put the generic title back on a pill the
    initial tool_call had already labelled with its command.
    """
    if isinstance(raw_input, dict):
        desc = raw_input.get("description")
        if isinstance(desc, str) and desc.strip():
            return desc
    kind_str = kind if isinstance(kind, str) else None
    shell = _is_shell_kind(kind_str) if is_shell is None else is_shell
    # Shell kinds only, so an fs tool's operation name ("strReplace") is never
    # mistaken for a command.
    if shell and isinstance(raw_input, dict):
        cmd = raw_input.get("command")
        if isinstance(cmd, str) and cmd.strip():
            return cmd
    # The flat title field defaults to an "unknown" sentinel when a backend
    # omits it; treat that (and blanks) as absent rather than surfacing it.
    if isinstance(title, str) and title and title != "unknown":
        return title
    return None


class AcpClient:
    """JSON-RPC 2.0 client over stdio with kiro-cli acp."""

    def __init__(
        self,
        work_dir: str | Path | None = None,
        model: str | None = None,
        agent: str = CLIENT_NAME,
        sandbox_mode: str = "auto",
        session_key: str | None = None,
        channel_id: str | None = None,
        extra_env: dict[str, str] | None = None,
        acp_backend: str = "",
        audit_source: str | None = None,
        mcp_gateway_overlay: str | Path | None = None,
        mcp_gateway_settings_mcp_json: str | Path | None = None,
        mcp_gateway_socket: str | Path | None = None,
        permission_mode: str | None = None,
    ):
        if work_dir:
            self._work_dir = Path(work_dir)
        else:
            # config.paths is a stdlib-only leaf: importing it here can't
            # re-enter the config.loader -> providers.acp -> acp.client cycle.
            from kiro_crew.config.paths import config_dir

            self._work_dir = config_dir() / "workspace"
        # Once-per-instance guard for the ensure_ready work-dir check: True
        # after the first (off-loop) mkdir, so the per-prompt warm path pays
        # no filesystem syscall at all.
        self._work_dir_ready = False
        self._model = model or DEFAULT_MODEL
        self._agent = agent
        self._sandbox_mode = sandbox_mode
        self._acp_backend = acp_backend
        # Claude backend permission mode (Auto-mode / permission-UI parity).
        # Inert on the kiro-cli path and unused by the public core; a companion
        # that drives the _is_claude seam reads/writes it and wires the
        # permission-mode method set + settings.local.json defaultMode. None =
        # the backend default.
        self._permission_mode = permission_mode
        self._session_key = session_key
        # When set, this client emits a per-tool-call SEL audit from the ACP
        # dispatch loop. Used by app/worker-pool clients (e.g. code-review-sage,
        # knowledge llm_pool) that have no external audit loop. Left None for
        # chat / subagent clients, which already audit via chat_runner /
        # SubagentManager, so they never double-log.
        self._audit_source = audit_source
        self._channel_id = channel_id
        self._extra_env = extra_env or {}
        # MCP gateway overlay: when set, the broker stubs in its rewritten specs
        # are injected into this session at ACP session/new, where they outrank
        # the same-named entries in the agent spec. Nothing is written to the
        # user's project or to ~/.kiro/agents. None = pooling off.
        self._mcp_gateway_overlay = str(mcp_gateway_overlay) if mcp_gateway_overlay else None
        self._mcp_gateway_settings_mcp_json = (
            str(mcp_gateway_settings_mcp_json) if mcp_gateway_settings_mcp_json else None
        )
        self._mcp_gateway_socket = str(mcp_gateway_socket) if mcp_gateway_socket else None
        self._sandbox_cleanup: str | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._pid: int | None = None
        self._start_time: int | None = None  # process start time for PID recycling detection
        self._session_id: str | None = None
        self._next_id = 1
        self._buffer: deque[JsonRpcMessage] = deque(maxlen=100)
        self._mcp_notifications: list[JsonRpcMessage] = []
        # MCP OAuth requests collected during session init from
        # `_kiro.dev/mcp/oauth_request` notifications. Drained by callers via
        # `pop_pending_oauth_requests()` after `ensure_ready()` so the UI can
        # surface an Authorize button to the user. Each entry: {"serverName", "oauthUrl"}.
        self._pending_oauth_requests: list[dict[str, str]] = []
        # Server names already surfaced to the UI in this ACP session — kiro-cli
        # may emit `_kiro.dev/mcp/oauth_request` multiple times per server (e.g.
        # once per probe attempt). Dedupe so the user sees one banner per server.
        self._oauth_emitted_servers: set[str] = set()
        self._cancelled = False
        self._cancel_ts: float = 0.0
        # Cooperative-cancel read-grace for the CURRENT cancel. Defaults to the
        # module floor but is raised to the caller's ack budget by
        # cancel_session() so a configured soft_stop_budget_secs > 10 actually
        # extends the window instead of being silently capped (the read loop
        # would otherwise abort the turn at 10s while the soft waiter blocked
        # the full budget, then hard-kill — losing the session).
        self._cancel_grace_secs: float = _CANCEL_GRACE_SECS
        self._resume_session_id: str | None = None
        self._resumed = False
        self._can_load_session = False
        # Models advertised by the backend in the session/new (or session/load)
        # response. claude-agent-acp returns the real versioned Claude list
        # (Opus 4.8/4.7, Sonnet 4.6, …); kiro-cli returns its own. Captured so
        # the dashboard model dropdown reflects what the backend actually
        # offers rather than a hardcoded guess. Each entry: {modelId, name,
        # description}.
        self._available_models: list[dict[str, str]] = []
        # Mode ids the backend advertised at session init (session/new|load
        # `modes.availableModes`). Empty when the backend omits `modes` (older
        # kiro-cli / offline fake) — the set_mode guard treats empty as "attempt"
        # for backward compatibility. Populated by _store_session_config.
        self._available_mode_ids: list[str] = []
        # Whether the backend advertised a `modes` list at all (even an empty
        # one). Distinguishes "unknown, attempt for backward compat" (False)
        # from "advertised zero/some modes, honor the list" (True) so an
        # explicitly-empty availableModes fails closed rather than attempting.
        self._modes_advertised: bool = False
        # Model kiro-cli/claude-agent-acp actually resolved to (may differ
        # from self._model when that's the "auto" sentinel). Used to look up
        # the context window when usage_update isn't sent (see _track_metadata).
        self._resolved_model_id: str | None = None
        # Model the backend last substituted to via the -32603 admin-tier policy
        # advisory ("Using X instead"). Set by _wait_for_response when it sees the
        # advisory; consumed by the session/new path to re-issue creation on the
        # model the gateway will actually serve (the advisory carries no
        # sessionId, so the first attempt creates nothing). None = no substitution.
        self._last_substitution_model: str | None = None
        self._child_pids: dict[int, ChildRecord] = {}  # pid → (start_time, basename)
        self.last_prompt_stats = AcpPromptStats()
        self._tool_call_inputs: dict[str, str] = {}
        # Map toolCallId → is_shell, cached from the tool_call notification so
        # the later permission_request event (which carries no kind) can inherit
        # the canonical shell signal. Mirrors _tool_call_inputs lifecycle.
        self._tool_call_is_shell: dict[str, bool] = {}
        # toolCallIds already credited to the skill-usage ledger as a body read.
        # The arguments arrive on either the initial tool_call or its refinement
        # depending on the provider, so both are observed and this prevents one
        # read being counted twice. Mirrors _tool_call_is_shell's lifecycle.
        self._skill_read_noted: set[str] = set()
        # toolCallId -> skill keys resolved at call time, credited only when
        # the tool reports completion so a denied read leaves no delivery.
        self._pending_skill_reads: dict[str, list[str]] = {}
        # Map toolCallId → trusted MCP server name (_meta.kiro.mcpServerName),
        # cached from the tool_call notification so the later permission_request
        # event (which carries no _meta) can inherit it — the signal the
        # app-own-server auto-approve keys on. Mirrors _tool_call_is_shell.
        self._tool_call_mcp_server: dict[str, str] = {}
        # Map toolCallId → trusted tool name (_meta.kiro.toolName), cached like
        # _tool_call_mcp_server so the permission_request event can rebuild the
        # canonical mcp__<server>__<tool> for per-tool governance in the
        # app-own-server auto-approve.
        self._tool_call_tool_name: dict[str, str] = {}
        # Structured raw tool params (rawInput dict) keyed by toolCallId, cached
        # from the ToolCall notification so the later request_permission event —
        # which carries only a truncated title — can recover the real path/url
        # the governance gate needs (filesystem.write / network.egress scopes).
        self._tool_call_params: dict[str, dict] = {}
        # Map JSON-RPC request id → {"once": optionId, "always": optionId} so
        # the host can echo back the exact optionIds the agent advertised.
        # kiro-cli uses "allow_once"/"allow_always"; claude-agent-acp uses
        # "allow"/"allow_always". Falling back to OPTION_ALLOW_ONCE causes
        # claude-agent-acp to reject the response.
        self._permission_options: dict[str | int, dict[str, str]] = {}
        self._stderr_lines: deque[str] = deque(maxlen=20)
        self._jsonl_pos: int = 0  # track read position in session JSONL for tool results
        self._stderr_task: asyncio.Task | None = None  # type: ignore[type-arg]
        self._last_activity: float = time.monotonic()
        self._turn_done: asyncio.Event = asyncio.Event()
        # Serializes whole read turns on this client's single stdout StreamReader.
        # An asyncio StreamReader permits exactly ONE waiting reader; the shared
        # `_bg` session is streamed by ~8 callers and the per-session Semaphore(1)
        # does not cover abnormal-exit overlap (a turn dying mid-readline leaves a
        # parked read the next caller collides with -> "readuntil() called while
        # another coroutine is already waiting"). Acquired at the top of
        # _prompt_loop, released in its finally.
        #
        # Finalization caveat: a consumer that `return`s on "complete" without
        # exhausting _prompt_loop leaves the async-gen SUSPENDED; CPython runs
        # its finally via a *deferred* scheduled athrow (next loop tick), not at
        # the consumer's return. So the lock releases promptly on a live loop but
        # NOT synchronously. send_message_stream wraps the loop in `aclosing(...)`
        # to force deterministic release on its hot path; the other consumers
        # rely on the next-tick finalization.
        #
        # Coverage caveat: this lock only covers reads inside _prompt_loop.
        # _read_message also has callers OUTSIDE the loop (_wait_for_response
        # during init, wait_for_compaction). Those run in distinct lifecycle
        # phases that do not overlap a streaming _bg turn, so they are not
        # serialized here; if that ever changes, the readuntil race could recur.
        self._turn_lock: asyncio.Lock = asyncio.Lock()
        self._stale_eligible: bool = False  # set by _dispatch_events after text chunks
        # Set when a tool_call is yielded, cleared when the tool resolves
        # (tool_call_update result) or the turn starts/completes.  NOT cleared
        # on arbitrary inbound frames — that would disarm the watchdog after a
        # single progress frame.  Gates the _TOOL_STALL_TIMEOUT watchdog so a
        # dispatched-but-never-resolved tool can't hang the whole turn.
        self._tool_dispatched: bool = False
        # Liveness oracle for the stale-turn gate: before ending a silent turn
        # at _STALE_TURN_TIMEOUT, consult /proc evidence so a backend that is
        # provably working (CPU/IO movement in the subprocess subtree) is not
        # reaped. Mirrors the kiro shared-runtime path (AcpSessionHandle), which
        # already defers on a WORKING verdict instead of a blunt wall-clock.
        self._liveness_oracle = LivenessOracle()
        # Keep the executor future, not an await-scoped flag: wait_for can time
        # out while the underlying thread continues its /proc walk. A pending
        # future prevents silent-read polling from stacking blocked workers.
        self._consult_future: asyncio.Future[tuple[str, str]] | None = None
        # Record every observed tool_call (id -> (title, kind)) so the
        # PostToolUse hook fire can recover the tool_name from the result's
        # tool_call_id — the RESULT event carries no title. See
        # _maybe_fire_post_tool_hooks.
        self._observed_tool_calls: dict[str, tuple[str, str]] = {}
        self._last_stop_reason: str = ""
        # Dynamic config from ACP session/new response and config_option_update notifications.
        # Only the effort configOptions are consumed (model lists come from
        # _capture_available_models, which parses the real dict-shaped `models`).
        self._acp_config_options: list[dict] = []

    @property
    def backend(self) -> str:
        """ACP backend identifier (e.g. ACP_BACKEND_CLAUDE for claude-agent-acp)."""
        return getattr(self, "_acp_backend", "")

    @property
    def _is_claude(self) -> bool:
        return self.backend == ACP_BACKEND_CLAUDE

    @property
    def _is_kiro(self) -> bool:
        """True when this client drives kiro-cli (the AcpClient default).

        AcpClient serves exactly two backends — kiro-cli and the dormant claude
        seam — so this is the positive spelling of the sites that used to read
        ``not self._is_claude`` (harness-parity H5). KAS runs on AcpRuntime, not
        AcpClient, so it never reaches this property.
        """
        return self.backend == ACP_BACKEND_KIRO

    def _pooled_mcp_servers(self) -> list[dict[str, Any]]:
        """Broker-stub ``mcpServers`` entries for this session's ``session/new``.

        A session-injected server outranks the same-named entry in the resolved
        agent spec, so injecting the stubs here is what actually pools the
        servers — nothing is written to the user's project or to
        ``~/.kiro/agents/``. Empty when the shared gateway is disabled.
        """
        return pooled_session_servers(
            self._mcp_gateway_overlay, self._agent, self._channel_id
        )

    def _claude_session_mcp_servers(self) -> list:
        """MCP server array passed to a claude ``session/new`` / ``session/load``.

        Overridable seam for the dormant ``_is_claude`` backend. The Default is
        ``[]`` so the public core (kiro-cli only, which gets its servers via
        ``--agent``) is byte-identical. An internal companion that re-registers
        a Claude backend over the ``ACP_BACKEND_CLAUDE`` seam overrides this to
        inject the kirocrew-core/cron + user MCP servers — the claude adapter
        does not read ``kirocrew.mcp.json`` on its own, so without this a claude
        session would have zero MCP tools.
        """
        return []

    @property
    def is_ready(self) -> bool:
        return self._process is not None and self._session_id is not None

    def _is_process_alive(self) -> bool:
        return self._process is not None and self._process.returncode is None

    def is_process_alive(self) -> bool:
        """True if the underlying process exists and has not exited."""
        return self._is_process_alive()

    @property
    def exit_code(self) -> int | None:
        """Return the process exit code, or None if still running / never started."""
        return self._process.returncode if self._process else None

    def is_responsive(self, stale_threshold: float = 600.0) -> bool:
        """True if process is alive AND has had I/O activity within threshold seconds."""
        if not self._is_process_alive():
            return False
        return (time.monotonic() - self._last_activity) < stale_threshold

    def touch_activity(self) -> None:
        """Refresh _last_activity without I/O. Used by long-running MCP tools
        (e.g. the `wait` tool) to prevent is_responsive() from flagging a
        deliberately-idle session as stale and triggering SIGTERM."""
        self._last_activity = time.monotonic()

    @property
    def resumed(self) -> bool:
        """True if the last session was restored via session/load."""
        return self._resumed

    def set_resume_session_id(self, sid: str) -> None:
        """Set a kiro-cli session ID to restore via session/load on next ensure_ready()."""
        self._resume_session_id = sid

    def rekey(
        self,
        session_key: str,
        channel_id: str | None = None,
        crew_agent: str = "",
        watchdog: object | None = None,
    ) -> None:
        """Re-key this client for a different session (used by warm pool).

        ``crew_agent`` and ``watchdog`` exist only for signature parity with
        AcpSessionProvider.rekey (session.py calls provider.client.rekey
        uniformly): this client's dispatch loop carries no per-agent watchdog
        snapshot, so both are accepted and deliberately not stored."""
        self._session_key = session_key
        self._channel_id = channel_id
        self._last_activity = time.monotonic()
        # The prompt stats' context fields describe whatever this runtime did
        # BEFORE the handoff — carry_over() deliberately preserves them across
        # turn boundaries, so without this reset a recycled runtime hands its
        # previous session's context_pct to the new chat and the first
        # check_context_usage() compacts an empty conversation (#2932).
        self.last_prompt_stats.reset_context_state()
        # Claim-push: tell gatewayd this runtime PID now belongs to
        # ``session_key`` so every MCP stub connection under it carries the
        # right ``_meta.caller`` immediately — event-driven replacement for
        # the stub-side recaller poll (whose bounded budget stranded pool
        # runtimes claimed late). Fire-and-forget; no-ops without a gateway
        # socket or a live process.
        schedule_claim(
            self._mcp_gateway_socket,
            self._process.pid if self._process else None,
            session_key,
            channel_id,
        )

    async def set_model(self, model_id: str) -> None:
        """Switch model on a running session (used by warm pool post-claim)."""
        if not self._session_id:
            raise AcpError("Cannot set model before session is initialized")
        # Unlike the spawn path, this is an explicit request for THIS model, so
        # a silent downgrade would report success while running something else.
        # Refuse before the wire and name what the account can use.
        # AcpModelUnavailable (not a bare AcpError) so callers can tell "invalid
        # request" from "the call didn't land": the generic failure is recovered
        # by resetting the session, which for THIS case would destroy a live
        # conversation and then land on a different model anyway.
        #
        # Callers passing an INHERITED value (warm-pool post-claim re-apply of a
        # persisted slot model) must pre-check with model_is_unusable and skip
        # instead of calling into here — otherwise the same stale setting that is
        # quietly withheld on a cold start would raise and kill a warm claim,
        # making the outcome depend on whether a pooled process happened to exist.
        if self._is_kiro and self._model_is_unusable(model_id):
            _rejected_log, _ = redact_exfiltration_urls(str(model_id))
            _rejected_log, _ = redact_credentials(_rejected_log)
            raise AcpModelUnavailable(_rejected_log, self._advertised_model_ids())
        if self._is_claude:
            await self.set_config_option("model", model_id)
        else:
            await self._send_request(
                METHOD_SET_MODEL,
                {"sessionId": self._session_id, "modelId": model_id},
            )
        self._model = model_id
        self._resolved_model_id = model_id
        # The previous model's window (and its authoritative usage_update, if
        # any) no longer describe this session — rebase the meter stats to the
        # new model so the context meter updates without waiting for the next
        # turn's telemetry (and so _backfill_context_window is un-gated).
        win = (
            model_registry.model_window(model_id)
            if model_registry.has_known_window(model_id)
            else None
        )
        self.last_prompt_stats.rebase_to_window(win or 0)

    def _capture_available_models(self, session_resp: dict) -> None:
        """Record the model list the backend advertised in a session response.

        The ACP ``session/new`` / ``session/load`` response carries a
        ``models`` object ``{availableModels: [{modelId, name, description}],
        currentModelId}``. We keep the list so the dashboard dropdown shows the
        real backend models (e.g. the versioned Claude list from
        claude-agent-acp) instead of a hardcoded guess. Best-effort and never
        raises — a backend that omits ``models`` simply leaves the list empty.

        Also records ``currentModelId`` for ``_track_metadata``'s context
        window lookup.
        """
        models = session_resp.get("models")
        if not isinstance(models, dict):
            return
        current_model_id = models.get("currentModelId")
        if isinstance(current_model_id, str) and current_model_id:
            self._resolved_model_id = current_model_id
        advertised = models.get("availableModels")
        if not isinstance(advertised, list):
            return
        captured: list[dict[str, str]] = []
        for m in advertised:
            if not isinstance(m, dict):
                continue
            model_id = m.get("modelId") or m.get("value") or ""
            if not model_id:
                continue
            captured.append(
                {
                    "modelId": str(model_id),
                    "name": str(m.get("name") or model_id),
                    "description": str(m.get("description") or ""),
                }
            )
        if captured:
            self._available_models = captured

    def available_models(self) -> list[dict[str, str]]:
        """Models advertised by the backend at session init (may be empty)."""
        return list(self._available_models)

    def _advertised_model_ids(self) -> list[str]:
        """Advertised model ids, for the model-rejection error path.

        Empty when the backend advertised nothing (no session yet, or a backend
        that omits ``models``), which the error path reads as "entitlement
        unknown" and leaves the existing transient/capacity handling alone.
        """
        ids = []
        for entry in self._available_models:
            model_id = entry.get("modelId") if isinstance(entry, dict) else None
            if isinstance(model_id, str) and model_id.strip():
                ids.append(model_id)
        return ids

    def _model_is_unusable(self, model_id: str) -> bool:
        """Whether this session's advertised set excludes *model_id*.

        Thin bind of :func:`model_is_unusable` to the set captured at
        ``session/new`` / ``session/load``, so this client and the
        ``providers.acp`` live path share one definition of entitlement.
        """
        return model_is_unusable(model_id, self._advertised_model_ids())

    async def _apply_startup_model(self) -> None:
        """Apply the configured model to a freshly initialized session.

        Split out of ``_init_session`` step 5 so the withhold decision is
        reachable without standing up a whole session.

        The model here was NOT chosen for this turn: it arrives from the agent
        spec, the config default, or a slot value persisted before the account's
        entitlements were known. So when the backend has already told us the
        account cannot run it, withholding beats failing — the user did not pick
        this model and cannot be expected to know why every turn dies. The
        session simply stays on the backend's own default, which ``session/new``
        already applied and reported as ``currentModelId``.

        Note this fixes the WIRE, not the stored setting: the persisted config /
        slot value is untouched, so a picker reading it still shows the model
        that was withheld. Healing the stored value is a separate change.

        An EXPLICIT switch is handled the opposite way in :meth:`set_model`:
        there the user asked for that exact model, and quietly running another
        one would be a lie.
        """
        if not self._model or self._model == DEFAULT_MODEL:
            logger.info("ACP model: %s (from agent config)", self._model or "auto")
            return
        if self._is_kiro and self._model_is_unusable(self._model):
            _withheld_log, _ = redact_exfiltration_urls(str(self._model))
            _withheld_log, _ = redact_credentials(_withheld_log)
            logger.warning(
                "ACP model %s is not available to this account; staying on the "
                "backend default %s (advertised: %s)",
                _withheld_log,
                self._resolved_model_id or DEFAULT_MODEL,
                ", ".join(self._advertised_model_ids()),
            )
            # Record the session as running the default rather than the value we
            # declined: the "!= DEFAULT_MODEL" test above is also what the
            # warm-pool re-apply path reads (session_provider), so leaving the
            # unusable id here would re-offer it on every claim.
            self._model = DEFAULT_MODEL
            return
        if self._is_claude:
            await self.set_config_option("model", self._model)
        else:
            await self._send_request(
                METHOD_SET_MODEL,
                {"sessionId": self._session_id, "modelId": self._model},
            )
        logger.info("ACP model: %s", self._model)

    async def set_config_option(self, config_id: str, value: str) -> None:
        """Set a session config option (e.g. effort level) via session/set_config_option."""
        if not self._session_id:
            raise AcpError("Cannot set config option before session is initialized")
        req_id = await self._send_request(
            "session/set_config_option",
            {"sessionId": self._session_id, "configId": config_id, "value": value},
        )
        await self._wait_for_response(req_id, timeout=10.0)

    # ── Dynamic Config from ACP ──

    def _store_session_config(self, resp: dict) -> None:
        """Extract effort configOptions from a session/new or session/load response.

        Model lists are captured separately by ``_capture_available_models``,
        which parses the real dict-shaped ``models`` payload
        (``{availableModels: [...]}``); only the ``configOptions`` effort
        selector is consumed here.
        """
        logger.debug("_store_session_config keys: %s", list(resp.keys()))
        config_options = resp.get("configOptions")
        if isinstance(config_options, list):
            self._acp_config_options = config_options
            logger.debug("ACP config options loaded: %d entries", len(config_options))
            self._sync_effort_levels()
        # Capture advertised mode ids + whether a modes list was advertised at
        # all, so step 4's set_mode can fail closed against a requested agent the
        # backend never loaded (would fault with "Mode '<agent>' not found").
        # Assigned unconditionally so a re-init that omits `modes` clears any
        # stale state rather than guarding on it.
        self._available_mode_ids, _current_mode, self._modes_advertised = (
            parse_session_modes(resp)
        )

    def _handle_config_option_update(self, msg: JsonRpcMessage) -> None:
        """Process a config_option_update session notification.

        ACP emits this when config changes (e.g. model switch rebuilds effort options).
        The payload is a full configOptions array that replaces the previous one.
        """
        params = msg.params or {}
        update = params.get("update", {})
        if not isinstance(update, dict):
            return
        config_options = update.get("configOptions")
        if isinstance(config_options, list):
            self._acp_config_options = config_options
            logger.debug("ACP config options updated: %d entries", len(config_options))
            self._sync_effort_levels()

    def _sync_effort_levels(self) -> None:
        """Push ACP-reported effort levels to the global validation set."""
        levels = self.get_valid_effort_levels()
        if levels:
            # circular import: chat_persistence → dashboard → session → acp.client
            from kiro_crew.dashboard.chat_persistence import update_reasoning_effort_values

            update_reasoning_effort_values(levels)

    @property
    def acp_config_options(self) -> list[dict]:
        """Config options reported by ACP (effort, model, mode selectors)."""
        return self._acp_config_options

    def supports_config_option(self, config_id: str) -> bool:
        """Whether the session advertised a config option with this id.

        Older claude-agent-acp builds do not expose an ``effort`` selector at
        all; pushing ``session/set_config_option`` for it then fails with
        ``Unknown config option`` (a -32603 Internal error, distinct from a
        value-level rejection). Callers gate on this so an adapter that lacks
        the option is a silent no-op rather than a noisy error + session reset.

        Returns True when no config options were reported yet, so that a
        backend which advertises options lazily (after the first turn) is not
        permanently treated as unsupported.
        """
        if not self._acp_config_options:
            return True
        return any(
            isinstance(opt, dict) and opt.get("id") == config_id for opt in self._acp_config_options
        )

    def get_valid_effort_levels(self) -> list[str]:
        """Return valid effort levels from ACP config, preserving ACP order.

        Parses configOptions for the entry with id="effort" and extracts its
        options[].value list in the order ACP reported them.
        """
        for opt in self._acp_config_options:
            if not isinstance(opt, dict):
                continue
            if opt.get("id") == "effort":
                options = opt.get("options", [])
                if isinstance(options, list):
                    return [o["value"] for o in options if isinstance(o, dict) and "value" in o]
        return []

    def _next_req_id(self) -> int:
        rid = self._next_id
        self._next_id += 1
        return rid

    # ── Process Management ──

    def _discard_sandbox_cleanup(self) -> None:
        """Unlink and forget the sandbox temp file allocated by ``wrap_argv``.

        wrap_argv writes a launcher/profile file that the spawned child
        consumes at exec. Once no child will exec it — the spawn failed, was
        cancelled, or the process is being reset — it must be removed here, or
        each attempt leaks one file into the temp dir for the gateway's
        lifetime (nothing else references the path after ``_spawn`` reassigns
        ``self._sandbox_cleanup``).
        """
        if self._sandbox_cleanup:
            try:
                os.remove(self._sandbox_cleanup)
            except OSError:
                pass
            self._sandbox_cleanup = None

    async def _to_thread_guarding_sandbox(
        self, fn: Callable[..., _T], /, *args: Any, **kwargs: Any
    ) -> _T:
        """``asyncio.to_thread`` that discards the sandbox file on failure.

        After ``wrap_argv`` has allocated the sandbox temp file, every
        suspension point before the exec is a leak window: a cancellation
        (turn cancel, session close, shutdown) unwinds ``_spawn`` without
        reaching ``_reset_state``, orphaning the file. Route any offload in
        that window through here so the file is removed before re-raising.
        """
        try:
            return await asyncio.to_thread(fn, *args, **kwargs)
        except BaseException:
            self._discard_sandbox_cleanup()
            raise

    async def _spawn(self) -> None:
        """Start the ACP backend subprocess with stdio pipes.

        KiroCrew's public core only ever drives the kiro-cli backend. The
        claude-agent-acp branch below is the dormant protocol seam (see
        ``ACP_BACKEND_CLAUDE``): the public provider factory never selects it,
        so it is unreachable here, but an internal companion that re-registers
        a Claude backend reuses this same client over the seam.
        """
        # Off-loop: mkdir is a blocking syscall and the parent dirs may live on
        # slow storage; the loop must never wait on the kernel here.
        await asyncio.to_thread(self._work_dir.mkdir, parents=True, exist_ok=True)

        if self._is_claude:
            # Dormant seam — see method docstring. Binary resolution only; the
            # ~/.claude registration glue (settings.local.json, the MCP-registry
            # reader) lived in the deleted cc_agent module and is re-added by the
            # internal companion, not the public core.
            #
            # Per-session settings seed: a companion attaches
            # _write_claude_local_settings (permissions.defaultMode + the
            # availableModels allowlist that unlocks the 1M-token window). It
            # MUST run on the PRIMARY spawn path — not only the rare
            # model-substitution retry at _new_session_following_substitution —
            # or a claude session collapses to the 200K default. Guarded via
            # getattr so the public core (no such method) is byte-identical.
            _seed = getattr(self, "_write_claude_local_settings", None)
            if callable(_seed):
                try:
                    _seed()
                except (OSError, ValueError, TypeError):
                    logger.warning("initial seed of settings.local.json failed", exc_info=True)
            global _claude_acp_argv_cache  # noqa: PLW0603
            if _claude_acp_argv_cache is _UNRESOLVED:
                _claude_acp_argv_cache = await asyncio.to_thread(_resolve_claude_acp_bin)
            claude_argv = _claude_acp_argv_cache
            if not isinstance(claude_argv, list) or not claude_argv:
                raise AcpError(
                    f"{CLAUDE_ACP_BIN} not found. Install it with "
                    f"'npm i -g {CLAUDE_ACP_NPM_PKG}' (or add it as a project "
                    f"dependency), or set CLAUDE_AGENT_ACP_BIN to its entry "
                    f"script."
                )
            argv: list[str] = claude_argv
        else:
            try:
                kiro_bin = await _resolve_kiro_bin_for_spawn()
            except _KiroExecutableTrustError as exc:
                raise AcpError(str(exc)) from exc
            if not kiro_bin:
                raise AcpError(f"{KIRO_CLI_BIN} not found in PATH")
            # Self-heal (B): ensure the managed default agent file exists before
            # this --agent spawn, so kiro-cli registers the mode and step 4's
            # set_mode succeeds instead of faulting "Mode not found". Best-effort,
            # off the loop; non-managed agents fall through to the step-4 guard.
            try:
                await asyncio.to_thread(ensure_agent_materialized, self._agent)
            except Exception:
                logger.warning("pre-spawn agent materialization failed", exc_info=True)
            argv = [kiro_bin, KIRO_CLI_SUBCMD, "--agent", self._agent]

        # OS-level sandbox: wrap the command to hide sensitive paths.
        # strip_python_env keeps the host PYTHONPATH/PYTHONHOME out of kiro-cli's
        # foreign MCP subprocesses (which bundle their own interpreter + deps).
        # is_kiro_cli is membership in ACP_BACKENDS_INTERNAL_SANDBOX
        # (harness-parity H7), not "not claude": the flag makes wrap_argv SKIP
        # Crew's seatbelt on macOS in favour of the harness's own internal
        # sandbox, so a harness without one must never be granted it by the
        # absence of another harness.
        argv, self._sandbox_cleanup = wrap_argv(
            argv,
            mode=self._sandbox_mode,
            strip_python_env=True,
            is_kiro_cli=self.backend in ACP_BACKENDS_INTERNAL_SANDBOX,
        )
        # cgroup v2 scope (OUTERMOST): bound this agent + all its MCP-server /
        # tool descendants with pids.max (fork bomb) + memory.max (RSS balloon).
        # No-op + loud warning where cgroup delegation is unavailable. --scope
        # execs into the target, so self._pid below is still the real child.
        # Off-loop: first call probes /proc + /sys and the config read touches
        # the config dir (mkdir + file read) — blocking syscalls that must not
        # run on the loop. Guarded: wrap_argv above allocated the sandbox temp
        # file, so a cancellation here must not orphan it.
        argv = await self._to_thread_guarding_sandbox(cgroup_scope_argv, argv)

        # Build the child environment (process-group isolation flags are set on
        # the spawn kwargs below, per-platform).
        env = {**os.environ}
        if self._extra_env:
            env.update(self._extra_env)
        # Parent-level scrub of gateway-owned channel credentials. The default
        # auto/standard sandbox launcher strips _AGENT_DENIED_ENV_KEYS only for
        # cc/strict, and this path copies a raw os.environ + wrap_argv (not
        # sandboxed_spawn_argv), so without this the Slack/WeCom/Telegram tokens
        # seeded into os.environ by load_credentials() would be inherited by the
        # agent subprocess on the default tier. Leaves the AWS/SSH env the
        # standard sandbox intentionally exposes untouched.
        env = scrub_agent_denied_env(env)
        env["PATH"] = augmented_path(env.get("PATH", ""))
        if self._is_claude and not env.get("CLAUDE_CODE_EXECUTABLE"):
            # Dormant seam (see _spawn docstring): the adapter's SDK needs a
            # native Claude binary we don't vendor and does NOT search PATH for
            # `claude` itself, so point it at one explicitly when the seam is
            # driven. Only set when unset so an operator override always wins.
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
        if self._session_key:
            env["KIROCREW_SESSION_KEY"] = self._session_key
        else:
            env.pop("KIROCREW_SESSION_KEY", None)
        if self._channel_id:
            env["KIROCREW_CHANNEL_ID"] = self._channel_id
        else:
            env.pop("KIROCREW_CHANNEL_ID", None)

        # Resolve SSH_AUTH_SOCK dynamically — the gateway's env may be stale
        # after an ssh-agent restart — and KRB5CCNAME to a FILE: ccache (the
        # kernel keyring, the default on some Linux distros, is invisible to
        # this child, so Kerberos-gated MCP servers fail without it). Covers
        # the session agent and all ACP-provider subagents, which spawn through
        # this same path. The same hop settles the CLI's own KIRO_API_KEY:
        # re-injected from .env for the kiro-cli backend (post-scrub Docker),
        # actively stripped for a foreign backend, which must never receive it
        # (see config.loader.inject/strip_kiro_cli_api_key). All of this
        # glob/stat/reads under /tmp and the data home, so it runs off-loop in
        # ONE thread hop. Guarded: the sandbox temp file is live, so a
        # cancellation here must not orphan it.
        env = await self._to_thread_guarding_sandbox(
            functools.partial(_resolve_spawn_env, kiro_api_key=self._is_kiro), env
        )
        # Positive-identity marker for the orphan sweep: kiro-cli and every MCP
        # server it spawns inherit this, so escaped launcher trees (``npx
        # @playwright/mcp`` -> node) are identifiable as ours.
        env[KIROCREW_SPAWNED_ENV] = KIROCREW_SPAWNED_VALUE
        # Memory-aware cap for pytest-xdist's ``-n auto``: xdist sizes auto to
        # the CPU count, ignoring memory, so a full-suite run in an agent turn
        # can spawn cpu_count workers x ~1 GB each and exhaust the host. xdist
        # honors PYTEST_XDIST_AUTO_NUM_WORKERS when resolving auto, so seeding
        # it here bounds ONLY auto resolution — explicit ``-n N``, non-xdist
        # runs, and venvs without xdist are unaffected. Respects a value
        # already present in the env; see resource_status.inject_xdist_auto_cap.
        # Off-loop: resolving the cap reads the raw config, and that read
        # enters config_dir() (mkdir + file IO + JSON parse) — blocking
        # syscalls that must not run on the loop. Guarded: the sandbox temp
        # file is live, so a cancellation here must not orphan it.
        await self._to_thread_guarding_sandbox(inject_xdist_auto_cap, env)

        # Process-group isolation for clean tree-kill. Pass both flags explicitly
        # (NOT via **dict unpack — that breaks mypy's Popen overload resolution on
        # the build fleet). POSIX: start_new_session=True calls setsid so
        # _kill_process can killpg the whole group; creationflags resolves to 0
        # (no-op). Windows: no setsid (start_new_session is silently ignored), so
        # CREATE_NEW_PROCESS_GROUP makes the child tree taskkill /T-reapable and
        # stops an inherited Ctrl-C propagating into the gateway. The flag comes
        # from platform_compat (getattr) so referencing it doesn't fail mypy's
        # [attr-defined] check on Linux where subprocess.* lacks it.
        self._process = await create_subprocess_limited(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self._work_dir),
            limit=_STDOUT_BUFFER_LIMIT,
            env=env,
            start_new_session=platform_compat.IS_POSIX,
            creationflags=(
                platform_compat.CREATE_NEW_PROCESS_GROUP
                | platform_compat._SUBPROCESS_NO_WINDOW
                | platform_compat.CREATE_SUSPENDED
            ),
            profile=RLIMIT_PROFILE_SESSION_HOST,
        )
        self._pid = self._process.pid
        _spawn_label = (
            "claude-agent-acp" if self._is_claude else f"{KIRO_CLI_BIN} {KIRO_CLI_SUBCMD}"
        )
        # Windows resource ceiling, applied while the child is still SUSPENDED,
        # then resumed. No-op on POSIX (CREATE_SUSPENDED is 0 there). OFFLOADED
        # because the Windows path reads the config file and walks the process
        # and thread tables (see the note on finish_suspended_spawn); the child
        # is frozen until it returns, so this is the one await the spawn cannot
        # skip.
        await asyncio.get_running_loop().run_in_executor(
            subprocess_executor(),
            functools.partial(
                finish_suspended_spawn, self._process, self._pid, label=_spawn_label
            ),
        )
        self._start_time = await asyncio.get_running_loop().run_in_executor(
            subprocess_executor(), _get_start_time, self._pid
        )
        logger.info("Spawned %s (PID %d)", _spawn_label, self._pid)
        # Track root PID and do an early descendant scan.  kiro-cli forks
        # child processes quickly after launch.  Recording them here means
        # _kill_process() can clean up even if _initialize_session() fails.
        from kiro_crew.session import (  # circular: session -> config.loader -> providers.acp -> acp.client
            _track_child_pids,
            _track_pid,
            _track_session_pid,
        )

        # The PID-file trackers each take an exclusive file lock and do a
        # read-modify-append under it — blocking syscalls that must not run
        # on the event loop: ensure_ready() awaits _spawn() from the loop on
        # every cold start, so a contended or wedged lock holder here would
        # stall every task including the liveness heartbeat. Ride the same
        # executor as the descendant scans below.
        _loop = asyncio.get_running_loop()
        await _loop.run_in_executor(subprocess_executor(), _track_pid, self._pid)
        # Separate file for startup cleanup.
        await _loop.run_in_executor(subprocess_executor(), _track_session_pid, self._pid)
        await asyncio.sleep(0.3)
        early_descendants = await _loop.run_in_executor(
            subprocess_executor(), _get_child_pids, self._pid
        )
        if early_descendants:
            self._child_pids = await _loop.run_in_executor(
                subprocess_executor(), _capture_child_records, early_descendants
            )
            await _loop.run_in_executor(
                subprocess_executor(), _track_child_pids, self._child_pids, self._pid or 0
            )
            logger.info("Early tracking %d descendants of PID %d", len(self._child_pids), self._pid)

        if self._process.stderr:
            self._stderr_task = asyncio.ensure_future(self._drain_stderr(self._process.stderr))

    async def _drain_stderr(self, stderr: asyncio.StreamReader) -> None:
        # Count of suppressed high-frequency marker lines (see
        # _SUPPRESSED_STDERR_MARKERS) and the monotonic timestamp of the last
        # throttled summary, so a thinking burst is observable in the log
        # without re-introducing the per-delta flood it replaced.
        suppressed = 0
        last_summary = time.monotonic()
        while True:
            line = await stderr.readline()
            if not line:
                break
            text = line.decode(errors="replace").strip()
            if not text:
                continue
            # Liveness must advance for EVERY line, including suppressed ones:
            # the adapter is provably alive while emitting them, and the idle
            # watchdog (is_responsive) must not kill an actively-thinking turn.
            # One monotonic read, reused for the throttle check below.
            now = time.monotonic()
            self._last_activity = now
            if any(marker in text for marker in _SUPPRESSED_STDERR_MARKERS):
                # Drop the line: no per-occurrence WARNING, and crucially do not
                # append to the bounded _stderr_lines ring buffer — otherwise a
                # thinking burst evicts the last real errors from diagnostics.
                suppressed += 1
                if now - last_summary >= _SUPPRESSED_STDERR_SUMMARY_INTERVAL_SECS:
                    logger.debug("suppressed %d adapter stderr marker line(s)", suppressed)
                    suppressed = 0
                    last_summary = now
                continue
            self._stderr_lines.append(text)
            redacted, _ = redact_exfiltration_urls(text)
            redacted, _ = redact_credentials(redacted)
            _bin_label = "claude-acp" if self._is_claude else KIRO_CLI_BIN
            logger.warning("%s stderr: %s", _bin_label, redacted)
        if suppressed:
            # Flush the residual count once the stream closes so the final burst
            # is still accounted for.
            logger.debug("suppressed %d adapter stderr marker line(s)", suppressed)

    async def _snapshot_process_tree(self) -> None:
        """Discover and track the full process tree after MCP servers are loaded.

        Merges with any early snapshot taken in _spawn().  MCP servers
        (the internal MCP server, node) may not exist until after _initialize_session().
        """
        _loop = asyncio.get_running_loop()
        descendants = await _loop.run_in_executor(subprocess_executor(), _get_child_pids, self._pid)
        if not descendants:
            # Retry once — children may not have forked yet
            await asyncio.sleep(0.5)
            descendants = await _loop.run_in_executor(
                subprocess_executor(), _get_child_pids, self._pid
            )

        new_pids = [p for p in descendants if p not in self._child_pids]
        if new_pids:
            self._child_pids.update(
                await _loop.run_in_executor(subprocess_executor(), _capture_child_records, new_pids)
            )

        if self._child_pids:
            from kiro_crew.session import _track_child_pids

            _track_child_pids(self._child_pids, parent_pid=self._pid or 0)
            logger.info("Tracked %d descendant PIDs for PID %d", len(self._child_pids), self._pid)

    async def _kill_process(self, *, force: bool = False) -> None:
        """Kill the subprocess and wait for it to exit.

        Uses process groups (killpg) for clean tree kill, then sweeps
        child PIDs that escaped to a different PGID.

        Args:
            force: If True, kill immediately (used during shutdown).
        """
        if not self._process or self._process.returncode is not None:
            return
        pid = self._pid
        if pid is None:  # narrow for mypy — set at _spawn time under the process guard
            return
        # Close pipes first to unblock any pending reads/writes
        for pipe in (self._process.stdin, self._process.stdout, self._process.stderr):
            if pipe:
                try:
                    pipe.close()  # type: ignore[union-attr]
                except Exception:
                    pass

        # Snapshot child PIDs before killing — children in different
        # process groups survive killpg (kiro-cli-chat acp leak).
        # Merge stored snapshot (from init, catches reparented-to-init PIDs)
        # with fresh scan (catches children spawned after init).
        _loop = asyncio.get_running_loop()
        fresh = await _loop.run_in_executor(subprocess_executor(), _get_child_pids, pid)
        stored = self._child_pids
        # Build merged dict: pid → (start_time, basename) (stored has both, fresh needs capture)
        merged: dict[int, ChildRecord] = dict(stored)
        new_pids = [p for p in fresh if p not in merged]
        if new_pids:
            # capture (start_time, basename) off-loop — on macOS these spawn `ps`
            merged.update(
                await _loop.run_in_executor(subprocess_executor(), _capture_child_records, new_pids)
            )

        if not force:
            try:
                # POSIX: killpg(getpgid) tears down the whole group (setsid at
                # spawn). Windows: taskkill /T /F walks the child tree instead
                # (no process groups) — platform_compat dispatches both. Async
                # variant offloads the Windows taskkill spawn to
                # subprocess_executor so the event loop keeps ticking while
                # taskkill.exe runs.
                await platform_compat.kill_process_tree_async(pid, platform_compat.SIGTERM)
            except (ProcessLookupError, OSError):
                pass
            try:
                await asyncio.wait_for(self._process.wait(), timeout=3.0)
                # _kill_escaped_children -> _is_our_child -> _get_start_time/
                # _read_basename spawn `ps` on macOS; run it off the loop.
                await _loop.run_in_executor(subprocess_executor(), _kill_escaped_children, merged)
                return
            except asyncio.TimeoutError:
                pass
        # Force kill (async variant offloads Windows taskkill).
        try:
            await platform_compat.kill_process_tree_async(pid, platform_compat.SIGKILL)
        except (ProcessLookupError, OSError):
            try:
                self._process.kill()
            except (ProcessLookupError, OSError):
                pass
        await _loop.run_in_executor(subprocess_executor(), _kill_escaped_children, merged)
        try:
            await asyncio.wait_for(self._process.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            logger.warning("PID %s did not exit after force kill", pid)

    def _retire_liveness_state(self) -> None:
        """Release the tracked consult and swap in a fresh, configured oracle.

        Both boundaries that drop a movement baseline — turn start in
        ``_prompt_loop`` under ``_turn_lock``, and process reset — retire rather
        than ``reset()``,
        and both must retire the future TOGETHER with the oracle. Their lifetimes
        cannot diverge: if only the oracle were replaced, a walk wedged during the
        previous turn would answer every later poll with "prior consult still in
        flight", so the new turn never samples its own process and the 90s cutoff
        completes it early — the truncation this gate exists to prevent.

        Releasing the future costs at most one abandoned worker per boundary
        instead of per silent read, which is the bound this gate is actually for.
        A walk submitted through ``_consult_liveness_model_wait`` already carries a
        retrieval callback from submission time, so its eventual exception is
        consumed even though nobody awaits it any more; the consume/attach here
        additionally covers a future that did not come from that path.

        ``fresh()`` carries the configuration over: a default-constructed
        replacement would silently repoint a caller-supplied /proc root, clock or
        sampling interval. ``getattr`` because the low-level PID lifecycle tests
        build clients with ``__new__``.
        """
        prior_consult = getattr(self, "_consult_future", None)
        self._consult_future = None
        if prior_consult is not None:
            if prior_consult.done():
                _consume_future_exception(prior_consult)
            else:
                prior_consult.add_done_callback(_consume_future_exception)
        retiring = getattr(self, "_liveness_oracle", None)
        self._liveness_oracle = retiring.fresh() if retiring is not None else LivenessOracle()

    def _reset_state(self) -> None:
        """Reset all session state (call after process is dead)."""
        if self._process:
            for pipe in (self._process.stdin, self._process.stdout, self._process.stderr):
                if pipe:
                    try:
                        pipe.close()  # type: ignore[union-attr]
                    except Exception:
                        pass
        # Clean up sandbox temp files (macOS seatbelt profile)
        self._discard_sandbox_cleanup()
        # Remove settings.local.json so bypassPermissions doesn't persist after crash
        if self._is_claude:
            _stale = self._work_dir / ".claude" / "settings.local.json"
            try:
                _stale.unlink(missing_ok=True)
            except OSError:
                pass
        # Save PIDs before clearing state — needed for untracking
        saved_pid = self._pid
        saved_child_pids = self._child_pids
        self._process = None
        self._pid = None
        # A walk wedged on the dead PID's /proc entry can never speak for the
        # replacement process, so release it with the oracle it sampled into.
        self._retire_liveness_state()
        self._session_id = None
        self._buffer.clear()
        self._stderr_lines.clear()
        if self._stderr_task and not self._stderr_task.done():
            self._stderr_task.cancel()
        self._stderr_task = None
        self._cancelled = False
        self._cancel_ts = 0.0
        self._cancel_grace_secs = _CANCEL_GRACE_SECS
        self._resumed = False
        self._turn_done = asyncio.Event()
        self._last_stop_reason = ""
        self._pending_oauth_requests.clear()
        self._oauth_emitted_servers.clear()
        # Untrack PIDs from the orphan tracking files — but ONLY those confirmed
        # dead. A child or root still alive after teardown survived the kill
        # (killpg only reaches the kiro-cli process group, so children in other
        # groups can outlive it, and a mid-init crash can race the descendant
        # scan in _kill_process()). Retaining a survivor's entry keeps it visible
        # to the periodic orphan sweep and next-startup cleanup_orphaned_sessions(),
        # which reap it; untracking one would orphan it permanently (all sweep
        # mechanisms key off these files) — the memory-leak this guards against.
        # These untrack helpers live in kiro_crew.session, which imports this
        # module transitively, so they must be imported inline.
        from kiro_crew.session import _untrack_child_pids, _untrack_pid, _untrack_session_pid
        from kiro_crew.session_pid import _pid_gone_or_unmanaged

        if saved_child_pids:
            dead_children = {
                pid: rec for pid, rec in saved_child_pids.items() if _pid_gone_or_unmanaged(pid)
            }
            survivors = [p for p in saved_child_pids if p not in dead_children]
            if dead_children:
                try:
                    _untrack_child_pids(dead_children)
                except Exception:
                    logger.debug(
                        "untracking child PIDs %s failed", list(dead_children), exc_info=True
                    )
            if survivors:
                logger.warning(
                    "Retained tracking for %d live child PID(s) that survived "
                    "teardown; orphan sweep will reap them: %s",
                    len(survivors),
                    survivors,
                )
        # Untrack parent kiro-cli PID (only if confirmed dead)
        if saved_pid is not None:
            if _pid_gone_or_unmanaged(saved_pid):
                try:
                    _untrack_pid(saved_pid)
                except Exception:
                    logger.debug("untracking PID %s failed", saved_pid, exc_info=True)
                try:
                    _untrack_session_pid(saved_pid)
                except Exception:
                    logger.debug("untracking session PID %s failed", saved_pid, exc_info=True)
            else:
                logger.warning(
                    "Retained tracking for live root PID %s that survived "
                    "teardown; orphan sweep will reap it",
                    saved_pid,
                )
        self._child_pids = {}

    async def _new_session_following_substitution(self) -> dict:
        """Issue ``session/new``; if the gateway substitutes the model, adopt it
        and re-issue once so a real session is actually created.

        The admin-tier policy advisory ("Model X is restricted ... Using Y
        instead.") comes back as a ``-32603`` *error frame with no sessionId* --
        the first attempt creates nothing. ``_wait_for_response`` records the
        substitute model in ``self._last_substitution_model``; here we pin
        ``self._model`` to it, re-seed ``settings.local.json`` (the claude
        backend builds a fresh SettingsManager per session/new, so the new model
        takes effect), and re-issue. Idempotent and bounded to ONE retry -- if the
        gateway substitutes again to the same/another restricted id we stop
        rather than loop. Returns the session/new response dict (possibly still
        without a sessionId, which the caller treats as a hard failure).

        The claude-backed substitution retry path is the dormant ``_is_claude``
        seam (kiro-cli never emits this advisory); the public core drives only
        kiro-cli, so ``mcpServers`` stays ``[]`` and the settings re-seed is
        best-effort via ``getattr`` — the deleted cc_agent glue is re-added by
        the internal companion, not the public core.
        """
        new_params: dict = {
            "cwd": str(self._work_dir),
            # kiro-cli loads servers from --agent; claude-agent-acp must be
            # told here -- it does not read kirocrew.mcp.json on its own. The
            # Default hook returns [] (kiro-cli path unchanged); an internal
            # companion that drives the _is_claude seam overrides
            # _claude_session_mcp_servers() to populate the claude MCP array.
            # Pooled broker stubs are appended for kiro-cli: a session-injected
            # server outranks the same-named entry in the agent spec, which is
            # how pooling takes effect without writing a spec anywhere.
            "mcpServers": [
                *self._claude_session_mcp_servers(),
                *(await asyncio.to_thread(self._pooled_mcp_servers)),
            ],
        }
        if self._is_claude:
            new_params["_meta"] = {"claudeCode": {"options": {}}}

        self._last_substitution_model = None
        session_id = await self._send_request(METHOD_SESSION_NEW, new_params)
        session_resp = await self._wait_for_response(session_id, timeout=_INIT_TIMEOUT)

        # Happy path: a real session came back.
        if session_resp.get("sessionId"):
            return session_resp

        # Substitution path: the gateway named a model it WILL serve. Adopt it,
        # re-seed settings so the backend resolves to it, and re-issue once.
        substitute = self._last_substitution_model
        if self._is_claude and substitute and substitute != self._model:
            # Redact the substitute name before logging. It is parsed straight
            # from the untrusted ACP backend advisory (data.details "Using X
            # instead") and the gateway log fans out to the dashboard activity
            # feed and Slack -- mirror the dual-redaction discipline applied
            # to the source advisory in _wait_for_response.
            _sub_log, _ = redact_exfiltration_urls(str(substitute))
            _sub_log, _ = redact_credentials(_sub_log)
            logger.warning(
                "ACP session/new returned a substitution advisory with no session; "
                "adopting gateway-served model %r and retrying session creation.",
                _sub_log,
            )
            self._model = substitute
            # Re-seed settings.local.json so the fresh SettingsManager the adapter
            # builds for the retry resolves the substitute model (it merges
            # settings sources each session/new). The re-seed helper lives in the
            # internal companion's cc_agent glue; guard so the public core (which
            # never reaches this dormant _is_claude branch) does not AttributeError.
            _reseed = getattr(self, "_write_claude_local_settings", None)
            if callable(_reseed):
                try:
                    _reseed()
                except (OSError, ValueError, TypeError):
                    # Narrow to realistic re-seed failure modes: OSError covers
                    # disk / permission errors on the atomic write; ValueError
                    # and TypeError cover registry / json shape surprises.
                    # Never let re-seed failure mask the retry -- worst case, the
                    # adapter resolves to whatever it had cached and we still
                    # retry session/new on the substitute path.
                    logger.warning("re-seed of settings.local.json failed", exc_info=True)
            self._last_substitution_model = None
            retry_id = await self._send_request(METHOD_SESSION_NEW, new_params)
            session_resp = await self._wait_for_response(retry_id, timeout=_INIT_TIMEOUT)

        return session_resp

    async def _initialize_session(self) -> None:
        """Handshake: initialize → session/load or session/new → set_mode → set_model."""
        # 1. Initialize
        protocol_version: int | str = (
            PROTOCOL_VERSION_CLAUDE if self._is_claude else PROTOCOL_VERSION
        )
        init_id = await self._send_request(
            METHOD_INITIALIZE,
            {
                "protocolVersion": protocol_version,
                "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
                "clientCapabilities": ACP_CLIENT_CAPABILITIES,
            },
        )
        init_resp = await self._wait_for_response(init_id, timeout=_INIT_TIMEOUT)
        logger.info("ACP initialized (protocol=%s)", init_resp.get("protocolVersion"))

        # Check if kiro-cli supports session/load
        self._can_load_session = init_resp.get("agentCapabilities", {}).get("loadSession", False)

        # 2. Try session/load if we have a resume ID and kiro-cli supports it
        self._resumed = False
        resume_sid = self._resume_session_id
        self._resume_session_id = None  # consume — no retry loop

        if resume_sid and self._can_load_session:
            # Only attempt session/load when the prior session transcript
            # actually exists on disk. Without this guard a stale persisted SID
            # (e.g. a slot reopened for a brand-new conversation) triggers a
            # session/load that REPLAYS the old transcript on top of the fresh
            # system prompt + memory injection — inflating base context to
            # ~38% on turn 1. kiro-cli stores transcripts at ~/.kiro/sessions/
            # cli/<sid>.json; a missing transcript falls back to session/new
            # (a genuinely fresh start).
            if self._is_claude:
                # Dormant seam: claude session/load takes no file path, and the
                # SDK transcript-path resolver lived in the deleted cc cleanup
                # helper. The internal companion re-adds it; the public core
                # simply attempts the load.
                session_file = ""
                file_ok = True
            else:
                session_file = str(
                    kiro_sessions_dir() / f"{resume_sid}.json"
                )
                file_ok = Path(session_file).exists()
            if file_ok:
                try:
                    load_params: dict = {
                        "sessionId": resume_sid,
                        "cwd": str(self._work_dir),
                        # kiro-cli gets its servers via --agent; the claude
                        # backend must receive them here (it does not read
                        # kirocrew.mcp.json itself). Default [] leaves kiro-cli
                        # unchanged; a companion overrides the hook (see
                        # session/new above). Pooled stubs are re-declared so a
                        # resumed session keeps talking to the broker.
                        "mcpServers": [
                            *self._claude_session_mcp_servers(),
                            *(await asyncio.to_thread(self._pooled_mcp_servers)),
                        ],
                    }
                    if self._is_claude:
                        load_params["_meta"] = {"claudeCode": {"options": {}}}
                    else:
                        load_params["_meta"] = {"_kiro.dev/session_file": session_file}
                    load_id = await self._send_request(METHOD_SESSION_LOAD, load_params)
                    load_resp = await self._wait_for_response(load_id, timeout=_INIT_TIMEOUT)
                    if "modes" in load_resp:
                        self._session_id = resume_sid
                        self._resumed = True
                        self._capture_available_models(load_resp)
                        self._store_session_config(load_resp)
                        logger.info("ACP session resumed: %s", resume_sid)
                except (AcpError, AcpTimeoutError):
                    logger.info(
                        "session/load failed for %s, falling back to session/new", resume_sid
                    )
            else:
                logger.info("Session file missing for %s, skipping load", resume_sid)

        # 3. Create new session if load didn't succeed. On the claude backend a
        # model-substitution advisory comes back as an error frame with NO
        # sessionId; _new_session_following_substitution adopts the substitute
        # model and re-issues once so a real session is actually created.
        if not self._session_id:
            # Capture the requested model before the helper runs. The helper resets
            # self._last_substitution_model = None on every return path, so we can't
            # use it to detect whether substitution happened. Comparing self._model
            # before vs after is the reliable signal.
            model_before = self._model
            session_resp = await self._new_session_following_substitution()
            self._session_id = session_resp.get("sessionId")
            self._capture_available_models(session_resp)
            self._store_session_config(session_resp)
            if not self._session_id:
                # Both the initial attempt and the substitution retry failed to
                # yield a session. Raise a clear, actionable error instead of
                # letting the next step die on the opaque "Cannot set config
                # option before session is initialized" guard.
                # self._model can be backend-derived (the substitute adopted
                # by _new_session_following_substitution from the ACP advisory),
                # and AcpError chains through to the dashboard activity feed
                # and Slack. Dual-redact before interpolating, same discipline
                # as the logger paths.
                # Standardize redacted-local naming across this file: the
                # convention is _<source>_log so the reader sees what was redacted.
                _model_for_error_log, _ = redact_exfiltration_urls(str(self._model))
                _model_for_error_log, _ = redact_credentials(_model_for_error_log)
                raise AcpError(
                    "session/new returned no sessionId"
                    + (
                        f" even after adopting substitute model {_model_for_error_log!r}"
                        if self._model != model_before
                        else ""
                    )
                    + "; the backend did not create a session."
                )
            # self._model can be backend-derived if _new_session_following_substitution
            # adopted the gateway substitute. Redact it consistently with the
            # warning-log path so any URL / credential-shaped substitute id never
            # reaches the dashboard activity feed or Slack unredacted.
            _model_log, _ = redact_exfiltration_urls(str(self._model))
            _model_log, _ = redact_credentials(_model_log)
            logger.info("ACP session created: %s (model=%s)", self._session_id, _model_log)
        self._last_activity = time.monotonic()

        # Seek to end of JSONL so we only read new tool results.
        # claude-agent-acp stores sessions via its own SDK, not ~/.kiro/ — skip.
        if self._session_id and self._is_kiro:
            _jpath = kiro_sessions_dir() / f"{self._session_id}.jsonl"
            try:
                self._jsonl_pos = _jpath.stat().st_size if _jpath.exists() else 0
            except OSError:
                self._jsonl_pos = 0

        # 4. Activate agent via set_mode (claude-agent-acp does not support set_mode — skip).
        #    Guard (A): fire only when the backend advertised this agent, or
        #    advertised no modes at all (older kiro-cli / fake → attempt,
        #    backward-compatible). If modes ARE advertised but this agent is
        #    absent, its ~/.kiro/agents/<agent>.json didn't load — FAIL CLOSED
        #    with an actionable error rather than silently running kiro-cli's
        #    default (broader) mode, which for a restricted agent is a privilege
        #    escalation. Self-heal (B, in _spawn) regenerates the managed default
        #    so the common case never reaches this branch.
        if self._is_kiro:
            if not self._modes_advertised or self._agent in self._available_mode_ids:
                await self._send_request(
                    METHOD_SET_MODE,
                    {"sessionId": self._session_id, "modeId": self._agent},
                )
                logger.info("ACP agent activated: %s", self._agent)
            else:
                raise AcpError(
                    f"Agent mode {self._agent!r} is not available on this session "
                    f"(advertised modes: {self._available_mode_ids or 'none'}); its "
                    f"~/.kiro/agents/{self._agent}.json is likely missing. Refusing "
                    f"to run the backend default mode in its place. Run "
                    f"`kirocrew setup --agent-only` to materialize the agent config."
                )

        # 5. Set model — override if KiroCrew config specifies non-default.
        await self._apply_startup_model()

        # Drain MCP server init notifications
        await self._drain_notifications()

    async def ensure_ready(self) -> None:
        """Ensure process is spawned and session is initialized.

        Runs before EVERY prompt, so the warm path must stay syscall-free:
        the work dir is created once per instance (off-loop) and remembered —
        a per-prompt mkdir would tax every prompt with a blocking syscall
        whose latency scales with the parent directory's entry count.
        Re-creating the directory later could not repair a live child anyway:
        a process's cwd is bound to the inode, not the path.
        """
        if not self._work_dir_ready:
            await asyncio.to_thread(self._work_dir.mkdir, parents=True, exist_ok=True)
            self._work_dir_ready = True
        if self._process and self._process.returncode is None and self._session_id:
            return

        # Telemetry (kirocrew.session.startup.duration): time the cold-start work
        # below (spawn + session init) and emit in the finally so every exit path
        # — success, auth-required, error — is measured. The warm fast-path above
        # is intentionally NOT measured (no startup work). Best-effort: a
        # telemetry failure must never affect session startup.
        _startup_t0 = time.monotonic()
        _startup_spawned = False
        # Default "error": any exit that is NOT the explicit success path below
        # — including an unexpected non-Acp exception propagating through the
        # finally — is recorded as a failure, never a false "ready".
        _startup_outcome = "error"
        try:
            # Retry once — kiro-cli first launch can be slow (MCP server init),
            # and transient failures (MCP crash, bad config read) are recoverable.
            for attempt in range(2):
                try:
                    if self._process and self._process.returncode is not None:
                        self._reset_state()

                    if not self._process:
                        await self._spawn()
                        _startup_spawned = True

                    await self._initialize_session()
                    try:
                        await self._snapshot_process_tree()
                    except Exception:
                        logger.warning("Failed to snapshot process tree", exc_info=True)

                    _startup_outcome = "ready"
                    return
                except (AcpTimeoutError, AcpError) as exc:
                    if attempt == 0:
                        logger.warning("ACP init failed (%s), retrying with fresh process...", exc)
                        await self._kill_process(force=True)
                        self._reset_state()
                    else:
                        # AcpAuthRequired subclasses AcpError; label it distinctly
                        # so a not-logged-in exit is never counted as a generic
                        # startup error. (The fork has no separate auth fail-fast
                        # branch — retry semantics stay unchanged.)
                        _startup_outcome = (
                            "auth_required" if isinstance(exc, AcpAuthRequired) else "error"
                        )
                        await self._kill_process(force=True)
                        self._reset_state()
                        raise
        finally:
            try:
                # circular import: importing get_recorder at module top would
                # form config.loader -> acp.types -> acp.client -> metrics.provider
                # -> config.loader (provider reads KiroCrewConfig). Keep it lazy so
                # provider is never loaded during config.loader's import chain.
                from kiro_crew.metrics.provider import get_recorder

                get_recorder().histogram(
                    "kirocrew.session.startup.duration",
                    (time.monotonic() - _startup_t0) * 1000.0,
                    unit="ms",
                    attrs={"outcome": _startup_outcome, "spawned": _startup_spawned},
                )
            except Exception:  # never let telemetry break session startup
                logger.debug("session startup metric emit failed", exc_info=True)

    async def shutdown(self) -> None:
        """Gracefully stop the ACP process."""
        # `_reset_state` in a `finally`, because `_kill_process` can leave
        # through several doors: it awaits four `run_in_executor` calls (child
        # scan, record capture, escaped-child sweep) that are not individually
        # guarded, `subprocess_executor()` refuses new work once the loop is
        # tearing down, and `asyncio.CancelledError` is a `BaseException` --
        # shutdown being exactly when cancellation arrives.
        #
        # Nothing retries. Every caller treats this as terminal and drops the
        # client immediately afterwards (`AcpWorker` and `_shutdown_quietly`
        # both `except Exception: log` and then set their reference to None), so
        # a skipped reset is permanent: the pipes stay open, the sandbox temp
        # files stay on disk, and for the claude backend
        # `.claude/settings.local.json` -- written to hold `bypassPermissions`
        # for the session -- survives the process it belonged to.
        #
        # Running it after a failed kill is safe by construction: `_reset_state`
        # untracks only PIDs it confirms dead and deliberately RETAINS tracking
        # for survivors so the orphan sweep still reaps them.
        try:
            await self._kill_process(force=True)
        finally:
            self._reset_state()  # untracks all PIDs (root + children)

    # ── JSON-RPC Transport ──

    async def _send_request(self, method: str, params: dict) -> int:
        if not self._process or not self._process.stdin:
            raise AcpError("ACP process not running")

        req_id = self._next_req_id()
        req = JsonRpcRequest(method=method, params=params, id=req_id)
        data = json.dumps(req.to_dict()) + "\n"
        try:
            self._process.stdin.write(data.encode())
            await self._process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise AcpProcessDied(f"ACP process pipe broken: {exc}") from exc
        self._last_activity = time.monotonic()
        return req_id

    async def _send_response(self, request_id: str | int, result: dict) -> None:
        if not self._process or not self._process.stdin:
            raise AcpError("ACP process not running")

        msg = {"jsonrpc": "2.0", "id": request_id, "result": result}
        data = json.dumps(msg) + "\n"
        try:
            self._process.stdin.write(data.encode())
            await self._process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise AcpProcessDied(f"ACP process pipe broken: {exc}") from exc
        self._last_activity = time.monotonic()

    async def _send_error(self, request_id: str | int, code: int, message: str) -> None:
        """Send a JSON-RPC 2.0 error response for a server→client request.

        Used to answer an unrecognized inbound request (e.g. ``fs/read_text_file``,
        ``terminal/create``) with ``-32601 Method not found`` so the agent fails
        fast instead of blocking forever waiting for a response we'd never send.
        """
        if not self._process or not self._process.stdin:
            raise AcpError("ACP process not running")

        msg = {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
        data = json.dumps(msg) + "\n"
        try:
            self._process.stdin.write(data.encode())
            await self._process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise AcpProcessDied(f"ACP process pipe broken: {exc}") from exc
        self._last_activity = time.monotonic()

    async def _read_message(self, timeout: float = _READ_TIMEOUT) -> JsonRpcMessage | None:
        if self._cancelled:
            if time.monotonic() - self._cancel_ts > self._cancel_grace_secs:
                raise AcpError("Cancel grace window exceeded; agent unresponsive")

        if self._buffer:
            return self._buffer.popleft()

        if not self._process or not self._process.stdout:
            raise AcpError("ACP process not running")

        try:
            line = await asyncio.wait_for(self._process.stdout.readline(), timeout=timeout)
        except asyncio.TimeoutError:
            return None
        except (ValueError, asyncio.LimitOverrunError) as exc:
            # A single JSON-RPC line exceeded the stdout StreamReader buffer
            # (_STDOUT_BUFFER_LIMIT). This does NOT corrupt the stream: before
            # raising ValueError, readline() deletes the oversize line through
            # its terminating newline when one is already buffered, else clears
            # the buffer, then resumes the transport (CPython
            # asyncio.streams.StreamReader.readline — its docstring states this).
            # So drop the frame and let the caller read the next one, exactly
            # like the blank-line and non-JSON paths below; raising
            # AcpProcessDied here would end a healthy live turn over one
            # unreadably large frame.
            #
            # NOTE the deliberate asymmetry with AcpRuntime._reader_loop, which
            # additionally enforces a drain budget: that reader is a standalone
            # task with no deadline, so an endlessly unterminated stream needs an
            # explicit terminal state there. HERE every call is bounded by the
            # caller's `timeout` and the callers run their own deadlines, so the
            # worst case is one turn ending on its deadline instead of a frame —
            # no unbounded state, and still strictly better than killing the turn
            # on the first oversize frame. Computing a byte budget would require
            # readuntil (readline reports neither the branch taken nor the bytes
            # dropped), i.e. hand-rolling readline's buffer repair on the path
            # that is NOT the reported failure.
            logger.warning("Dropped an oversize ACP stdout frame: %s", exc)
            return None
        if not line:
            # EOF — process likely died or closing. Check and avoid busy-loop.
            if self._process and self._process.returncode is not None:
                if self._stderr_task and not self._stderr_task.done():
                    try:
                        await asyncio.wait_for(self._stderr_task, timeout=0.5)
                    except (Exception, asyncio.CancelledError):
                        pass
                stderr_tail = "; ".join(self._stderr_lines) if self._stderr_lines else ""
                if stderr_tail:
                    from kiro_crew.security import redact_credentials, redact_exfiltration_urls

                    stderr_tail, _ = redact_exfiltration_urls(stderr_tail)
                    stderr_tail, _ = redact_credentials(stderr_tail)
                detail = f" — {stderr_tail}" if stderr_tail else ""
                raise AcpError(f"ACP process exited (code={self._process.returncode}){detail}")
            await asyncio.sleep(0.1)
            return None

        text = line.decode(errors="replace").strip()
        if not text:
            return None

        self._last_activity = time.monotonic()

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            logger.debug("Skipping non-JSON line from ACP: %.100s", text)
            return None

        return JsonRpcMessage(
            id=data.get("id"),
            method=data.get("method"),
            result=data.get("result"),
            error=data.get("error"),
            params=data.get("params"),
        )

    async def _wait_for_response(self, req_id: int, timeout: float = 50.0) -> dict:
        """Block until a JSON-RPC response matching *req_id* arrives.

        Explicitly classifies JSON-RPC 2.0 messages with the same method-aware
        discipline as ``JsonRpcMessage.is_response_for`` (a *response* has an
        id + no method; a *request* has both id AND method):

        - Notifications (method + no id): buffered in ``_mcp_notifications`` for
          ``_drain_notifications`` to process.
        - Matching response (id == req_id + no method): returned.
        - Server→client request (method + id) and foreign-id responses
          (id != req_id + no method): collected into a LOCAL ``deferred`` list
          and re-injected into ``self._buffer`` IN ORDER once the matching
          response arrives or on timeout.

        Server requests and foreign responses must NOT be re-appended to
        ``self._buffer`` mid-loop and ``continue``-d: ``_read_message`` pops
        ``self._buffer`` first, so it would immediately re-read the same frame,
        re-defer it, and spin until the deadline (the original bug). Holding
        them in a local list until exit guarantees forward progress while
        preserving the frame so a later ``_prompt_loop`` / ``_process_message``
        can answer the deferred ``session/request_permission`` request.

        The deadline is **activity-based**: any received message (notification,
        deferred frame, or the matching response) resets it to ``now + timeout``,
        bounded by an absolute ``_WAIT_RESPONSE_MAX_TIMEOUT`` safety cap. This
        keeps a long ``session/load`` replay (which streams the entire prior
        transcript as ``session/update`` notifications before resolving) alive
        instead of being killed by a fixed wall-clock and silently falling back
        to ``session/new``.
        """
        from kiro_crew import shutdown_event

        start = time.monotonic()
        deadline = start + timeout
        hard_deadline = start + max(timeout, _WAIT_RESPONSE_MAX_TIMEOUT)
        # Frames that are not the awaited response but must survive this call.
        deferred: list[JsonRpcMessage] = []

        def _reinject() -> None:
            # Re-inject in order at the FRONT of the buffer so a later
            # _prompt_loop / _process_message sees them before newer frames.
            for d in reversed(deferred):
                self._buffer.appendleft(d)
            deferred.clear()

        while time.monotonic() < deadline and time.monotonic() < hard_deadline:
            if shutdown_event.is_set():
                _reinject()
                raise AcpError("Shutdown in progress")
            remaining = min(deadline, hard_deadline) - time.monotonic()
            if remaining <= 0:
                break
            msg = await self._read_message(timeout=min(remaining, _READ_TIMEOUT))
            if msg is None:
                continue
            # Activity-based deadline: extend on any received frame, capped by
            # the hard safety deadline. Safe for init/handshake callers — only
            # extends while the agent is actively sending us data.
            deadline = min(time.monotonic() + timeout, hard_deadline)
            if msg.is_response_for(req_id):
                _reinject()
                if msg.error:
                    if _is_model_substitution_advisory(msg.error):
                        # Admin-tier / headless-tier policy substituted the
                        # requested model. The session is already live on the
                        # substitute -- keep going; log loudly so operators see it.
                        detail = _extract_advisory_detail(msg.error)
                        self._last_substitution_model = _substitute_model_from_advisory(msg.error)
                        # Redact before logging. The advisory payload originates
                        # from the ACP backend and flows to the dashboard activity
                        # feed and Slack via the gateway log. Match the existing
                        # _format_acp_error pattern in this file.
                        _payload_log = detail if detail else str(msg.error)
                        _payload_log, _ = redact_exfiltration_urls(_payload_log)
                        _payload_log, _ = redact_credentials(_payload_log)
                        logger.warning(
                            "ACP backend substituted model (policy): %s",
                            _payload_log,
                        )
                        return msg.result or {}
                    # Dual-redact msg.error before interpolating into the AcpError.
                    # msg.error is the wire-derived JSON-RPC error frame from the ACP
                    # backend; AcpError propagates to the dashboard activity feed and
                    # Slack via the same path as the logger sinks. Same redaction
                    # discipline as the substitution-advisory log site above.
                    _err_log, _ = redact_exfiltration_urls(str(msg.error))
                    _err_log, _ = redact_credentials(_err_log)
                    raise AcpError(f"JSON-RPC error: {_err_log}")
                return msg.result or {}
            # Notification (has method, no id) — buffer for drain.
            if msg.method and msg.id is None:
                self._mcp_notifications.append(msg)
                logger.debug("Buffered notification: %s (req=%d)", msg.method, req_id)
                continue
            # Server→client request (method AND id) or a foreign-id response
            # (id != req_id, no method). Defer locally — do NOT re-append to
            # self._buffer here, that would spin (see docstring). Re-injected
            # in order on return/raise so the permission request survives.
            if msg.method:
                logger.debug(
                    "Deferring inbound server request: method=%s id=%s (waiting for %d)",
                    msg.method,
                    msg.id,
                    req_id,
                )
            else:
                logger.debug(
                    "Deferring non-matching response: id=%s (waiting for %d)", msg.id, req_id
                )
            deferred.append(msg)

        _reinject()
        raise AcpTimeoutError()

    async def _drain_notifications(
        self,
        duration: float = _DRAIN_DURATION,
        idle_exit: float = _DRAIN_IDLE_EXIT,
    ) -> None:
        """Drain init notifications (buffered + live) and log MCP servers.

        Captures `_kiro.dev/mcp/oauth_request` into `_pending_oauth_requests` so
        callers can surface an Authorize prompt after `ensure_ready()` returns.

        Exits early once no notification has arrived for ``idle_exit`` seconds
        (MCP servers have gone quiet), bounded by the ``duration`` hard cap. This
        avoids always waiting the full cap on the common fast path while still
        giving genuinely slow servers up to ``duration`` to report in.
        """
        deadline = time.monotonic() + duration
        last_activity = time.monotonic()
        drained = 0
        mcp_servers: list[str] = []

        def _capture_oauth(msg: JsonRpcMessage) -> None:
            if not msg.is_method(METHOD_MCP_OAUTH_REQUEST):
                return
            params = msg.params if isinstance(msg.params, dict) else {}
            server_name = str(params.get("serverName") or params.get("name") or "")
            oauth_url = str(params.get("oauthUrl") or params.get("url") or "")
            # Drop unsafe-scheme URLs *before* recording dedupe so a later safe
            # retry for the same server still gets through.
            if not _is_safe_oauth_url(oauth_url):
                if oauth_url:
                    logger.warning(
                        "ACP: refusing unsafe MCP OAuth URL for %s", server_name or "(unknown)"
                    )
                return
            # Without a server_name we can't reliably correlate this banner with
            # the matching server_initialized/server_init_failure notification —
            # the discard path keys on server_name only.  Drop rather than risk
            # a permanently-stuck dedupe entry.
            if not server_name:
                logger.warning("ACP: dropping MCP OAuth request with empty serverName")
                return
            if server_name in self._oauth_emitted_servers:
                logger.debug("ACP: dropping duplicate MCP OAuth request for %s", server_name)
                return
            self._oauth_emitted_servers.add(server_name)
            self._pending_oauth_requests.append({"serverName": server_name, "oauthUrl": oauth_url})
            logger.info("ACP: MCP OAuth request for %s", server_name)

        def _capture_config_update(msg: JsonRpcMessage) -> None:
            if not msg.is_method(METHOD_SESSION_UPDATE):
                return
            params = msg.params or {}
            update = params.get("update", {})
            if isinstance(update, dict) and update.get("sessionUpdate") == UPDATE_CONFIG_OPTION:
                self._handle_config_option_update(msg)

        # Process notifications buffered during _wait_for_response
        for msg in self._mcp_notifications:
            drained += 1
            _capture_oauth(msg)
            _capture_config_update(msg)
            name = ""
            if isinstance(msg.params, dict):
                name = msg.params.get("name") or msg.params.get("serverName") or ""
            if name or "mcp" in (msg.method or ""):
                mcp_servers.append(name or msg.method or "unknown")
        self._mcp_notifications.clear()

        while True:
            # Single time snapshot per iteration so the deadline and idle checks
            # can't diverge on a loaded host (CR feedback).
            now = time.monotonic()
            remaining = deadline - now
            if remaining <= 0:
                break
            # Early-exit once servers have been quiet for the idle window. Poll in
            # idle-sized slices (capped by remaining) so we notice quiet promptly.
            idle_remaining = idle_exit - (now - last_activity)
            if idle_remaining <= 0:
                break
            try:
                read_msg = await self._read_message(timeout=min(remaining, idle_remaining, 2.0))
                if not read_msg:
                    continue
                # Any received message counts as activity (servers still talking),
                # resetting the idle window even if it carries no method.
                last_activity = time.monotonic()
                if not read_msg.method:
                    continue
                drained += 1
                _capture_oauth(read_msg)
                _capture_config_update(read_msg)
                if "mcp" in (read_msg.method or ""):
                    name = ""
                    if isinstance(read_msg.params, dict):
                        name = (
                            read_msg.params.get("name") or read_msg.params.get("serverName") or ""
                        )
                    mcp_servers.append(name or read_msg.method)
            except AcpError:
                break
        if mcp_servers:
            logger.info("ACP: MCP servers loaded: %s", ", ".join(mcp_servers))

    def pop_pending_oauth_requests(self) -> list[dict[str, str]]:
        """Drain and return MCP OAuth requests captured during session init.

        Each entry has keys ``serverName`` and ``oauthUrl``. Callers (typically
        the dashboard chat runner) surface these to the UI as an Authorize
        prompt — kiro-cli's local callback handles the rest of the OAuth flow.
        """
        out = list(self._pending_oauth_requests)
        self._pending_oauth_requests.clear()
        return out

    # ── Prompt Loop Helpers ──

    def _process_message(self, msg: JsonRpcMessage, req_id: int) -> str:
        """Classify a message into an action string.

        Actions: "complete", "error", "permission", "update", "metadata",
        "server_request_unknown", "skip".
        """
        if msg.is_response_for(req_id):
            return "error" if msg.error else "complete"

        if msg.is_method(METHOD_REQUEST_PERMISSION):
            return "permission"

        if msg.is_method(METHOD_SESSION_UPDATE):
            return "update"

        if msg.is_method(METHOD_METADATA):
            return "metadata"

        if msg.is_method(METHOD_COMPACTION_STATUS):
            return "compaction"

        if msg.is_method(METHOD_CLEAR_STATUS):
            return "clear"

        if msg.is_method(METHOD_AGENT_SWITCHED):
            return "agent_switched"

        if msg.is_method(METHOD_MCP_OAUTH_REQUEST):
            return "mcp_oauth_request"

        if msg.is_method(METHOD_MCP_SERVER_INITIALIZED):
            return "mcp_server_initialized"

        if msg.is_method(METHOD_MCP_SERVER_INIT_FAILURE):
            return "mcp_server_init_failure"

        if msg.is_method(METHOD_SUBAGENT_LIST_UPDATE):
            return "subagent_list"

        if msg.is_method(METHOD_KIRO_SESSION_UPDATE):
            return "subagent_activity"

        # Unknown server→client REQUEST (has both method AND id). Per JSON-RPC
        # the agent blocks until it gets a response, so it must be answered
        # (with -32601 by the dispatch sites) rather than silently skipped —
        # otherwise the agent hangs forever. Known requests (request_permission)
        # are handled above; only genuinely unrecognized requests reach here.
        if msg.method is not None and msg.id is not None:
            return "server_request_unknown"

        return "skip"

    async def _prompt_loop(
        self,
        req_id: int,
        timeout: float,
    ) -> AsyncGenerator[tuple[str, JsonRpcMessage], None]:
        """Core prompt read loop. Yields (action, msg) pairs.

        Always releases ``_turn_done`` on exit — including abnormal exits
        (process death, cancel-grace exceeded, or a caller that raises on an
        ``error`` action and closes this generator). Without the ``finally``,
        those paths bypass the callers' trailing ``_turn_done.set()`` and a
        ``wait_turn_done`` waiter (the cooperative-stop ack) blocks for its
        full budget before escalating to a session-losing hard kill.
        """
        # L1 turn-lock: serialize the whole read turn on this client's single
        # stdout StreamReader so two _bg streaming turns can't both park on
        # readline() and trip "readuntil() called while another coroutine is
        # already waiting". Acquired here (every streaming consumer funnels
        # through _prompt_loop), released in the finally — see the __init__
        # comment for the finalization + coverage caveats.
        await self._turn_lock.acquire()
        try:
            # Retire the liveness state HERE, under the lock, because this is the
            # one point every prompt path funnels through: send_message (via
            # _read_prompt_response), send_message_stream, and _dispatch_events.
            # Retiring in a caller's prologue instead would (a) miss the direct
            # prompt APIs, leaving their next turn gated by the previous turn's
            # wedged walk, and (b) run BEFORE this acquire, letting a queued turn
            # clear the active turn's tracked consult and so allow a second walk
            # while the first is still pending.
            self._retire_liveness_state()
            deadline = time.monotonic() + timeout
            consecutive_empty = 0
            last_data_ts = time.monotonic()

            while time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break

                msg = await self._read_message(timeout=min(remaining, _READ_TIMEOUT))
                if msg is None:
                    consecutive_empty += 1
                    if consecutive_empty >= _MAX_CONSECUTIVE_EMPTY and not self._is_process_alive():
                        rc = self._process.returncode if self._process else "?"
                        raise AcpProcessDied(f"Process exited during prompt (exit code {rc})")
                    # Staleness check: if caller set _stale_eligible (text was
                    # streamed) and kiro-cli has gone silent, exit early.
                    # Fold in _last_activity (refreshed by the stderr drain) so a
                    # turn that is still streaming thinking_tokens on stderr —
                    # between its final text chunk and its next tool call — is not
                    # mistaken for silence. Using only the stdout clock
                    # (last_data_ts) trips a false stale-turn on every multi-turn
                    # reasoning step, burning ~_STALE_TURN_TIMEOUT s per turn and
                    # reaping long subagent runs at the timeout.
                    last_seen = max(last_data_ts, self._last_activity)
                    if self._stale_eligible:
                        # Consult the liveness oracle on EVERY silent read once
                        # text has streamed — not only at the timeout. The oracle
                        # needs a prior sample to compute a movement delta (its
                        # first call after a boundary retirement always reads
                        # UNKNOWN/"sampling"),
                        # so a single consult at the 90s mark would always reap.
                        # Sampling each silent read primes the baseline and keeps
                        # the movement window recent, mirroring the kiro path's
                        # per-tick consult. The consult is /proc-based and
                        # offloaded so it cannot block the read loop, and degrades
                        # to a non-WORKING verdict on any error (fail toward
                        # reaping). In production, silent reads recur at the
                        # ~_READ_TIMEOUT cadence, giving well-spaced samples.
                        verdict, evidence = await self._consult_liveness_model_wait()
                        if (time.monotonic() - last_seen) > _STALE_TURN_TIMEOUT:
                            # Past the cutoff: a backend still doing work (CPU/IO
                            # movement in its subtree — a long model generation or
                            # a spawned build) reads WORKING and we keep waiting.
                            # Only WORKING extends; any other verdict
                            # (DEAD/UNKNOWN/STUCK_INPUT) preserves the conservative
                            # end-the-turn behavior, so hang recovery is never
                            # weakened (still bounded by _DEFAULT_PROMPT_TIMEOUT).
                            if verdict == VERDICT_WORKING:
                                logger.debug(
                                    "Stale-turn deferral for req %d — idle %.0fs but "
                                    "backend WORKING (%s)",
                                    req_id,
                                    time.monotonic() - last_seen,
                                    evidence,
                                )
                                continue
                            logger.warning(
                                "Stale turn detected for req %d — no data for %.0fs after text was streamed "
                                "(liveness=%s: %s). Treating as complete.",
                                req_id,
                                time.monotonic() - last_seen,
                                verdict,
                                evidence,
                            )
                            return
                    # Tool-stall watchdog: a tool was dispatched but NOTHING has
                    # come back (no result, no progress, no permission) for the
                    # stall window.  This is the silent-hang case where
                    # _stale_eligible is False (cleared on tool_call) so the
                    # check above never fires.
                    #
                    # Recovery (not just detection): the original bare ``return``
                    # abandoned the turn but left the kiro-cli child ALIVE
                    # mid-prompt, so the slot wedged and every later prompt hit
                    # "Prompt already in progress" until the whole backend was
                    # killed by hand.  Instead, kill the wedged child so the next
                    # prompt cold-starts, and raise AcpProcessDied so the existing
                    # recovery path takes over: the dashboard resets the session
                    # and re-queues the message (bounded by _acp_pipe_death_retries
                    # → "Session stuck" after 3), and cron/other callers surface a
                    # clean error instead of a hung turn.  _kill_process only
                    # touches the subprocess/pipes (never _turn_lock), so it is
                    # safe to call here — the finally still releases the lock.
                    #
                    # Use max(last_data_ts, _last_activity) so that MCP tools
                    # which ping /api/session-keepalive (wait, spawn_sub_agents)
                    # keep the watchdog satisfied even though no JSON-RPC frames
                    # arrive on stdout during their execution.
                    _tool_last_seen = max(last_data_ts, self._last_activity)
                    if (
                        self._tool_dispatched
                        and (time.monotonic() - _tool_last_seen) > _TOOL_STALL_TIMEOUT
                    ):
                        _stall_idle = time.monotonic() - _tool_last_seen
                        logger.warning(
                            "Tool stall detected for req %d — tool dispatched but no data for %.0fs. "
                            "Killing agent to recover the slot.",
                            req_id,
                            _stall_idle,
                        )
                        await self._kill_process(force=True)
                        raise AcpProcessDied(
                            f"tool stalled — no data for {_stall_idle:.0f}s; agent killed to recover"
                        )
                    continue

                consecutive_empty = 0
                last_data_ts = time.monotonic()
                # NB: do NOT clear _tool_dispatched here.  The last_data_ts reset
                # above already prevents false positives for tools that stream
                # progress frames (each frame restarts the _TOOL_STALL_TIMEOUT
                # countdown).  Clearing the flag on every inbound frame would
                # disarm the watchdog after a single progress frame, so a tool
                # that emits one frame then silently stalls would hang anyway —
                # exactly the bug this watchdog targets.  The flag is cleared
                # only when a tool actually resolves or the turn completes
                # (see _dispatch_events).
                self.last_prompt_stats.event_count += 1

                action = self._process_message(msg, req_id)
                yield action, msg
        finally:
            self._turn_lock.release()
            # Release any cooperative-stop waiter regardless of how the loop
            # ends. The callers set the precise stop reason on the clean
            # "complete" path before this runs (idempotent); on abnormal exit
            # the reason stays "" → provider.cancel reports "timeout" →
            # escalates to hard kill, the correct outcome for a dead turn.
            if not self._turn_done.is_set():
                self._turn_done.set()

    async def _consult_liveness_model_wait(self) -> tuple[str, str]:
        """Liveness verdict for the stale-turn gate, offloaded off the loop.

        The oracle walks ``/proc`` for the backend subprocess subtree — a
        synchronous filesystem walk that can block on a wedged fd — so it runs
        on ``subprocess_executor()`` under a bounded timeout, the same treatment
        the runtime's other /proc probes get. Any failure or timeout degrades to
        a non-WORKING verdict so the caller falls through to ending the turn
        (fail toward reaping, never toward hanging).

        Scope, mirroring the shared-runtime oracle this converges onto:
        - Linux-only evidence. On a host without ``/proc`` the verdict is
          UNKNOWN → the turn is reaped at the cutoff exactly as before this
          gate existed, so the change is behavior-preserving off Linux and a
          strict improvement on the gateway's Linux deploy target.
        - Subtree-aggregate movement. A busy *unrelated* descendant (e.g. an
          MCP child polling) can read WORKING even if the model turn itself is
          wedged with a lost completion frame, extending that turn to the 2h
          ``_DEFAULT_PROMPT_TIMEOUT`` backstop rather than reaping at 90s. This
          is an inherent property of the shared ``LivenessOracle`` (the kiro
          path has it too); tighter per-branch attribution belongs in
          ``liveness.py``, shared by both callers, not here.

        A timed-out await does not stop its executor thread. The one-outstanding-
        walk bound, the refused-submission-reads-UNKNOWN contract, and exception
        retrieval all live in the shared :func:`consult_offloaded` guard.
        ``_prompt_loop`` retires the tracked future at turn start under
        ``_turn_lock``, so a walk abandoned by one turn never gates the next (at
        the cost of one abandoned worker per turn, versus one per silent read
        before this guard existed).
        """
        return await consult_offloaded(
            self,
            self._liveness_oracle.check_model_wait,
            (getattr(self, "_pid", None),),
            executor_factory=subprocess_executor,
            log_label="liveness consult",
        )

    # ── Public API ──

    async def send_message(self, message: str, timeout: float | None = None) -> str:
        """Send a prompt and return the full response text."""
        timeout = await _effective_prompt_timeout_async(timeout)
        self._cancelled = False
        self._turn_done.clear()
        await self.ensure_ready()

        req_id = await self._send_prompt(message)
        return await self._read_prompt_response(req_id, timeout)

    async def send_message_stream(
        self, message: str, timeout: float | None = None
    ) -> AsyncIterator[str]:
        """Send a prompt and yield text chunks as they arrive."""
        timeout = await _effective_prompt_timeout_async(timeout)
        # NOTE: PreToolUse/PostToolUse hooks are intentionally NOT fired on this
        # streaming path today. No audit_source (worker-pool) consumer uses
        # send_message_stream — hook instrumentation lives on the _read_prompt_response
        # path (_maybe_fire_pre_tool_hooks / _maybe_fire_post_tool_hooks). If a future
        # streaming subagent adopts this method, mirror that Pre/Post instrumentation here.
        self._cancelled = False
        self._turn_done.clear()
        await self.ensure_ready()

        req_id = await self._send_prompt(message)
        self.last_prompt_stats = self.last_prompt_stats.carry_over()

        # aclosing(): _prompt_loop holds _turn_lock and releases it in its
        # finally. Consumers below `return` on "complete" without exhausting the
        # loop, which leaves the async-generator SUSPENDED — and CPython
        # finalizes async-gens via a *deferred* scheduled athrow, not at the
        # return point, so the lock would stay held past the turn (next _bg
        # caller blocks = the freeze). aclosing() runs aclose() deterministically
        # on block exit, firing the finally and releasing the lock immediately.
        async with aclosing(self._prompt_loop(req_id, timeout)) as _loop:
            async for action, msg in _loop:
                if action == "complete":
                    reason = ""
                    result = msg.result or {}
                    if isinstance(result, dict):
                        reason = result.get("stopReason", "") or ""
                    self._last_stop_reason = reason
                    self._turn_done.set()
                    return
                if action == "error":
                    _raise_acp_error(msg.error, self._advertised_model_ids())
                if action == "permission":
                    await self._handle_permission(msg)
                elif action == "server_request_unknown":
                    await self._reject_unknown_server_request(msg)
                elif action == "update":
                    self._track_usage_update(msg)
                    chunk, is_thinking = self._extract_text_chunk(msg)
                    if chunk and not is_thinking:
                        self.last_prompt_stats.text_chunks += 1
                        yield chunk
                        if _is_tool_interrupted_marker(chunk):
                            self._emit_tool_interrupted_sel("send_message_stream")
                            # send_message_stream yields only text chunks (str),
                            # not AcpEvent objects. Tool-result events are a
                            # different shape and cannot be yielded here; callers
                            # of this API do not consume them. Unlike
                            # _dispatch_events (which yields AcpEvent and must
                            # drain tool results before EVENT_COMPLETE), we just
                            # return — no further text will arrive from kiro-cli.
                            return
                    self._track_tool_call(msg)
                elif action == "metadata":
                    self._track_metadata(msg)
                elif action == "compaction":
                    self._handle_compaction_status(msg)

        # Loop ended without "complete" — timeout or process death.
        self._last_stop_reason = ""
        self._turn_done.set()

    async def stream_events(
        self,
        message: str,
        timeout: float | None = None,
    ) -> AsyncIterator[AcpEvent]:
        """Send a prompt and yield AcpEvent objects (text, tool_call, permission, complete)."""
        timeout = await _effective_prompt_timeout_async(timeout)
        self._cancelled = False
        self._turn_done.clear()
        await self.ensure_ready()
        req_id = await self._send_prompt(message)
        async for event in self._dispatch_events(req_id, timeout):
            yield event

    async def _dispatch_events(
        self,
        req_id: int,
        timeout: float,
        *,
        extract_agent_from_result: bool = False,
    ) -> AsyncIterator[AcpEvent]:
        """Shared event dispatch loop for prompts and commands."""
        self.last_prompt_stats = self.last_prompt_stats.carry_over()
        self._tool_call_inputs.clear()
        self._tool_call_is_shell.clear()
        self._skill_read_noted.clear()
        self._pending_skill_reads.clear()
        self._tool_call_mcp_server.clear()
        self._tool_call_tool_name.clear()
        self._tool_call_params.clear()
        # Reset the per-turn observed-tool-call bookkeeping (see __init__).
        self._observed_tool_calls.clear()
        # Clear stale permission options so an aborted/cancelled request from
        # a prior turn cannot leak into this one (memory + correctness).
        self._permission_options.clear()
        self._stale_eligible = False
        self._tool_dispatched = False
        got_complete = False
        saw_agent_switch = False

        async for action, msg in self._prompt_loop(req_id, timeout):
            if action != "update":
                logger.debug("ACP event: method=%s id=%s action=%s", msg.method, msg.id, action)

            # Reset staleness only on events that indicate active work.
            # Passive updates (usage_update, tool_call_update after completion,
            # available_commands) must NOT reset it — they can arrive after the
            # final text chunk when kiro-cli has finished but hasn't sent the
            # complete response yet.
            if action != "update":
                self._stale_eligible = False

            if action == "complete":
                got_complete = True
                result = msg.result or {}
                reason = ""
                if isinstance(result, dict):
                    reason = result.get("stopReason", "") or ""
                if extract_agent_from_result and isinstance(result, dict):
                    # commands/execute returns output in result fields,
                    # not via session/update chunks — yield as text.
                    text = self._format_command_result(result)
                    if text:
                        yield AcpEvent(kind=EVENT_TEXT_CHUNK, text=text)
                    if not saw_agent_switch:
                        data = result.get("data", {})
                        if isinstance(data, dict) and data.get("agent"):
                            agent_info = data["agent"]
                            name = (
                                agent_info.get("name", "") if isinstance(agent_info, dict) else ""
                            )
                            if name:
                                yield AcpEvent(kind=EVENT_AGENT_SWITCHED, text=name)
                # Flush any remaining tool results before completing
                for tr_event in await asyncio.to_thread(self._read_new_tool_results_sync):
                    yield tr_event
                # Turn is over — disarm the stall watchdog.
                self._tool_dispatched = False
                self._last_stop_reason = reason
                self._turn_done.set()
                yield AcpEvent(
                    kind=EVENT_COMPLETE,
                    stop_reason=reason,
                    usage=TurnUsage(credits=self.last_prompt_stats.credits),
                )
                return
            if action == "error":
                _raise_acp_error(msg.error, self._advertised_model_ids())
            if action == "permission":
                yield self._build_permission_event(msg)
            elif action == "server_request_unknown":
                await self._reject_unknown_server_request(msg)
            elif action == "update":
                self._track_usage_update(msg)
                chunk, is_thinking = self._extract_text_chunk(msg)
                if chunk:
                    # Before yielding text, check for tool results from JSONL
                    for tr_event in await asyncio.to_thread(self._read_new_tool_results_sync):
                        yield tr_event
                    kind = EVENT_THINKING_CHUNK if is_thinking else EVENT_TEXT_CHUNK
                    if not is_thinking:
                        self.last_prompt_stats.text_chunks += 1
                        self._stale_eligible = True
                    yield AcpEvent(kind=kind, text=chunk)
                    if not is_thinking and _is_tool_interrupted_marker(chunk):
                        # kiro-cli's built-in security filter cancelled the turn's tools.
                        # It will not send a ``complete`` response — synthesize one so the
                        # caller exits instead of waiting 2 hours for the prompt timeout.
                        # (_emit_tool_interrupted_sel logs + audits the cancellation.)
                        self._emit_tool_interrupted_sel("_dispatch_events")
                        got_complete = True
                        for tr_event in await asyncio.to_thread(self._read_new_tool_results_sync):
                            yield tr_event
                        yield AcpEvent(
                            kind=EVENT_COMPLETE,
                            usage=TurnUsage(credits=self.last_prompt_stats.credits),
                        )
                        return
                tool_event = self._extract_tool_event(msg)
                if tool_event:
                    self._stale_eligible = False
                    # Arm the tool-stall watchdog: if no further data arrives
                    # within _TOOL_STALL_TIMEOUT, _prompt_loop treats the turn
                    # as dead instead of hanging to the full prompt timeout.
                    self._tool_dispatched = True
                    # Record every observed tool_call so PostToolUse can recover
                    # tool_name from _observed_tool_calls (see
                    # _maybe_fire_post_tool_hooks).
                    if tool_event.tool_call_id:
                        self._observed_tool_calls[tool_event.tool_call_id] = (
                            tool_event.title or "unknown",
                            tool_event.tool_kind or "",
                        )
                    # Check for results from previous tool before yielding new tool_call
                    for tr_event in await asyncio.to_thread(self._read_new_tool_results_sync):
                        yield tr_event
                    # ACP-layer tool audit for clients with no external audit
                    # loop (e.g. app worker pools). No-op unless audit_source is set.
                    await self._maybe_audit_tool_call(tool_event)
                    # Co-located with the SEL audit Pre-side: fire the PreToolUse
                    # HOOK ENGINE so app/worker-pool subagents reach hook parity
                    # with the main agent / SubagentManager. PostToolUse fires
                    # separately on the tool_result branch below (fire_tool_hooks
                    # is Pre-only). No-op unless audit_source is set.
                    await self._maybe_note_skill_read(tool_event)
                    await self._maybe_fire_pre_tool_hooks(tool_event)
                    yield tool_event
                # Real-time tool result from `tool_call_update` session updates.
                # kiro-cli emits these the moment a tool completes — fires before
                # the JSONL flush, so the inline pill gets its output the instant
                # the tool finishes instead of waiting for the next tool call or
                # message end.  See `_extract_tool_call_update` for the dual-path
                # (content blocks vs rawOutput) details.
                tool_result_event = self._extract_tool_call_update(msg)
                if tool_result_event:
                    # The dispatched tool produced a result — disarm the stall
                    # watchdog.  (Cleared here, not on every inbound frame, so a
                    # tool that streams progress then silently stalls is still
                    # caught.)
                    self._tool_dispatched = False
                    # Fire the PostToolUse HOOK ENGINE now that the tool RESULT
                    # (and its output) exists — the Pre-vs-Post split is required
                    # because fire_tool_hooks above is PreToolUse-only. No-op
                    # unless audit_source is set.
                    self._maybe_credit_skill_read(tool_result_event)
                    await self._maybe_fire_post_tool_hooks(tool_result_event)
                    yield tool_result_event
                # claude-agent-acp emits a separate `tool_call_update` carrying
                # the refined title / kind / rawInput once `chunk.input` finishes
                # streaming (the initial `tool_call` arrives with empty input and
                # generic title like "Terminal"/"grep").  Yield as a refinement
                # event so the dashboard pill + persisted message can be
                # patched in place — see `EVENT_TOOL_CALL_UPDATE` in chat_runner.
                tool_refine_event = self._extract_tool_call_refinement(msg)
                if tool_refine_event:
                    await self._maybe_note_skill_read(tool_refine_event)
                    yield tool_refine_event
            elif action == "metadata":
                self._track_metadata(msg)
            elif action == "compaction":
                self._handle_compaction_status(msg)
                params = msg.params or {}
                status = params.get("status", {})
                status_type = status.get("type", "") if isinstance(status, dict) else str(status)
                summary = params.get("summary", "")
                yield AcpEvent(kind=EVENT_COMPACTION_STATUS, text=status_type, title=summary)
            elif action == "clear":
                yield AcpEvent(kind=EVENT_CLEAR_STATUS)
            elif action == "subagent_list":
                params = msg.params or {}
                _subs = params.get("subagents")
                logger.debug(
                    "ACP subagent_list received: %s entries",
                    len(_subs) if isinstance(_subs, list) else "n/a",
                )
                if isinstance(_subs, list):
                    yield AcpEvent(kind=EVENT_SUBAGENT_LIST, subagents=_subs)
            elif action == "subagent_activity":
                # _kiro.dev/session/update: a sub-agent session's own update,
                # tagged with its sessionId. Carries either:
                # - tool_call_chunk (inner tool starting) with toolCallId/title
                # - agent_message_chunk with text (sub-agent's streamed output)
                params = msg.params or {}
                _ssid = str(params.get("sessionId") or "")
                _upd_raw = params.get("update")
                _upd = _upd_raw if isinstance(_upd_raw, dict) else {}
                _su_kind = str(_upd.get("sessionUpdate") or "")
                _tcid = str(_upd.get("toolCallId") or "")
                # Prefer the nested ``content.text`` shape kiro-cli 2.10.0 emits
                # (via the shared chunk extractor); fall back to the flat
                # top-level ``text`` for older payloads. Fall back on a falsy
                # chunk (None OR empty string) so an empty nested content.text
                # does not shadow a populated flat ``text``.
                _su_chunk, _su_thinking = self._extract_text_chunk(msg)
                _su_text = str(_su_chunk or (_upd.get("text") or ""))
                if _ssid and _tcid:
                    # Sub-agent output is LLM-influenced — redact the title before
                    # it reaches the dashboard/persisted message.
                    _su_title, _ = redact_exfiltration_urls(str(_upd.get("title") or ""))
                    _su_title, _ = redact_credentials(_su_title)
                    yield AcpEvent(
                        kind=EVENT_SUBAGENT_ACTIVITY,
                        sub_session_id=_ssid,
                        tool_call_id=_tcid,
                        title=_su_title,
                    )
                elif _ssid and _su_text and _su_kind == "agent_message_chunk" and not _su_thinking:
                    # Sub-agent's streamed text output — the real content; redact
                    # exfil URLs + credentials the sub-agent may have emitted.
                    # Skip reasoning/thinking blocks (is_thinking) — those are the
                    # sub-agent's internal reasoning, not user-visible output, and
                    # the flat pre-port read never surfaced them.
                    _su_text, _ = redact_exfiltration_urls(_su_text)
                    _su_text, _ = redact_credentials(_su_text)
                    yield AcpEvent(
                        kind=EVENT_SUBAGENT_ACTIVITY,
                        sub_session_id=_ssid,
                        text=_su_text,
                    )
            elif action == "agent_switched":
                saw_agent_switch = True
                params = msg.params or {}
                agent_name = params.get("agentName", "")
                yield AcpEvent(kind=EVENT_AGENT_SWITCHED, text=agent_name)
            elif action == "mcp_oauth_request":
                params = msg.params or {}
                server_name = str(params.get("serverName") or params.get("name") or "")
                oauth_url = str(params.get("oauthUrl") or params.get("url") or "")
                # Reject unsafe-scheme URLs *before* recording dedupe so a later
                # safe retry for the same server still gets through.
                if not _is_safe_oauth_url(oauth_url):
                    if oauth_url:
                        logger.warning(
                            "ACP: refusing unsafe mid-session MCP OAuth URL for %s",
                            server_name or "(unknown)",
                        )
                    continue
                # Without a server_name we can't correlate this banner with the
                # later server_initialized/server_init_failure notification (the
                # discard path keys on server_name only).
                if not server_name:
                    logger.warning(
                        "ACP: dropping mid-session MCP OAuth request with empty serverName"
                    )
                    continue
                if server_name in self._oauth_emitted_servers:
                    logger.debug(
                        "ACP: dropping duplicate mid-session MCP OAuth request for %s",
                        server_name,
                    )
                    continue
                self._oauth_emitted_servers.add(server_name)
                logger.info("ACP: MCP OAuth request mid-session for %s", server_name)
                yield AcpEvent(
                    kind=EVENT_MCP_OAUTH_REQUEST,
                    server_name=server_name,
                    oauth_url=oauth_url,
                )
            elif action == "mcp_server_initialized":
                params = msg.params or {}
                server_name = str(params.get("serverName") or params.get("name") or "")
                if server_name:
                    logger.info("ACP: MCP server initialized: %s", server_name)
                    # Allow re-emission of oauth_request if this server's token expires later.
                    self._oauth_emitted_servers.discard(server_name)
                    yield AcpEvent(
                        kind=EVENT_MCP_SERVER_INITIALIZED,
                        server_name=server_name,
                    )
            elif action == "mcp_server_init_failure":
                params = msg.params or {}
                server_name = str(params.get("serverName") or params.get("name") or "")
                err = str(params.get("error") or "")
                if server_name:
                    logger.warning("ACP: MCP server init failure: %s — %s", server_name, err)
                    # The current banner is in a closed (failed) state — clear
                    # the dedupe entry so kiro-cli's next oauth_request retry
                    # for this server surfaces a new banner instead of being
                    # silently dropped.
                    self._oauth_emitted_servers.discard(server_name)
                    yield AcpEvent(
                        kind=EVENT_MCP_SERVER_INIT_FAILURE,
                        server_name=server_name,
                        text=err,
                    )

        if not got_complete:
            self._last_stop_reason = ""
            self._turn_done.set()
            # If text was streamed, this is a stale turn (kiro-cli finished
            # but never sent `result`).  Yield a synthetic complete so callers
            # finalize normally instead of showing a timeout error.
            if self._stale_eligible:
                logger.info(
                    "Synthesizing EVENT_COMPLETE after stale turn (chunks=%d)",
                    self.last_prompt_stats.text_chunks,
                )
                yield AcpEvent(
                    kind=EVENT_COMPLETE,
                    stop_reason=STOP_REASON_END_TURN,
                    usage=TurnUsage(credits=self.last_prompt_stats.credits),
                )
                return
            raise AcpTimeoutError()

    async def approve_tool(
        self,
        request_id: str | int,
        option_id: str | None = None,
        *,
        always: bool = False,
    ) -> None:
        """Approve a pending session/request_permission.

        ``option_id`` overrides the auto-resolved id when provided. Otherwise
        the recorded options for ``request_id`` are consulted — picking the
        "always" variant if ``always=True``, else the "once" variant. This
        keeps kiro-cli ("allow_once"/"allow_always") and claude-agent-acp
        ("allow"/"allow_always") working without caller knowledge.
        """
        resolved_id = option_id
        if resolved_id is None:
            recorded = self._permission_options.pop(request_id, None)
            # A recorded entry may carry only a "reject" id (a request that
            # advertised a reject option but no allow option), so use .get and
            # fall back to the canonical allow id rather than KeyError-ing.
            resolved_id = (recorded or {}).get("always" if always else "once")
            if resolved_id is None:
                resolved_id = OPTION_ALLOW_ALWAYS if always else OPTION_ALLOW_ONCE
        await self._send_response(
            request_id,
            {"outcome": {"outcome": OUTCOME_SELECTED, "optionId": resolved_id}},
        )

    async def reject_tool(self, request_id: str | int) -> None:
        """Reject a pending session/request_permission.

        Prefers a clean ``selected`` reject using the reject optionId the agent
        advertised (claude-agent-acp offers ``reject`` → behavior:"deny",
        surfacing a clear "permission denied" rather than the cryptic
        "Tool use aborted" the adapter throws on a ``cancelled`` outcome).
        Falls back to ``cancelled`` when no reject option was advertised
        (kiro-cli), which kiro handles as an ordinary rejection.
        """
        recorded = self._permission_options.pop(request_id, None)
        reject_id = recorded.get("reject") if recorded else None
        if reject_id:
            await self._send_response(
                request_id, {"outcome": {"outcome": OUTCOME_SELECTED, "optionId": reject_id}}
            )
        else:
            await self._send_response(request_id, {"outcome": {"outcome": OUTCOME_CANCELLED}})

    async def send_command(self, command: str, args: dict | None = None) -> str:
        """Execute a kiro slash command (e.g. '/compact', '/usage', '/effort').

        Returns the response text (if any).  For streaming output use
        :meth:`stream_command` instead.

        When *args* is provided (e.g. ``{"level": "high"}`` for ``/effort``),
        the TuiCommand object form ``{command, args}`` is used so kiro-cli
        receives the arguments — the plain-string form silently drops them.
        Otherwise the plain-string form is kept for backward compat with
        older kiro-cli.
        """
        await self.ensure_ready()
        if args:
            cmd_name = command.strip().split(None, 1)[0].lstrip("/")
            payload: dict = {
                "sessionId": self._session_id,
                "command": {"command": cmd_name, "args": args},
            }
        else:
            payload = {"sessionId": self._session_id, "command": command}
        req_id = await self._send_request(METHOD_COMMANDS_EXECUTE, payload)
        try:
            result = await self._wait_for_response(req_id, timeout=60.0)
            raw = result.get("text", "") or result.get("message", "")
            if raw:
                # Two-pass redaction (URLs + credentials) to match the shared
                # AcpSessionHandle.send_command path: a URL-only pass leaves
                # tokens/keys in slash-command output.
                raw, _ = redact_exfiltration_urls(raw)
                raw, _ = redact_credentials(raw)
            return raw
        except AcpTimeoutError:
            logger.debug("Command '%s' response timed out (may still be running)", command)
            return ""

    async def stream_command(
        self, command: str, timeout: float | None = None
    ) -> AsyncIterator[AcpEvent]:
        """Execute a slash command and yield streaming AcpEvents.

        Uses ``_kiro.dev/commands/execute`` with the TuiCommand object
        format (``{command, args}``) so kiro-cli executes the command
        natively and streams full output via ``session/update``.
        """
        timeout = await _effective_prompt_timeout_async(timeout)
        self._cancelled = False
        await self.ensure_ready()

        cmd_name, cmd_args = self._parse_slash_command(command)
        req_id = await self._send_request(
            METHOD_COMMANDS_EXECUTE,
            {
                "sessionId": self._session_id,
                "command": {"command": cmd_name, "args": cmd_args},
            },
        )
        async for event in self._dispatch_events(req_id, timeout, extract_agent_from_result=True):
            yield event

    @staticmethod
    def _format_command_result(result: dict) -> str:
        """Extract displayable text from a commands/execute response."""
        import json as _json

        data = result.get("data")
        message = result.get("message", "")
        # Structured data — format as readable JSON block
        if isinstance(data, dict) and data:
            # Filter out agent/model metadata (handled separately)
            display = {k: v for k, v in data.items() if k not in ("agent", "model")}
            if display:
                return (
                    f"{message}\n```json\n{_json.dumps(display, indent=2)}\n```"
                    if message
                    else f"```json\n{_json.dumps(display, indent=2)}\n```"
                )
        return message or ""

    @staticmethod
    def _parse_slash_command(command: str) -> tuple[str, dict]:
        """Parse ``/foo bar baz`` into TuiCommand ``(name, args)``."""
        parts = command.strip().split(None, 1)
        name = parts[0].lstrip("/") if parts else command.lstrip("/")
        value = parts[1] if len(parts) > 1 else None
        args: dict = {"value": value} if value else {}
        return name, args

    async def cancel_session(self, grace_secs: float = 0.0) -> None:
        """Cancel the current in-flight operation via ACP session/cancel.

        Per ACP spec, session/cancel is a JSON-RPC notification (no id).
        The ack arrives as stopReason:"cancelled" on the session/prompt
        response, not as a response to this message.

        ``grace_secs`` is the caller's cooperative-cancel ack budget. The read
        loop aborts the turn as "unresponsive" once this elapses, so it must
        not be shorter than the budget the caller will wait on; we raise the
        per-cancel grace to ``max(floor, grace_secs)`` so a configured budget
        above the 10s floor genuinely extends the window instead of the loop
        bailing early and forcing a session-losing hard kill.
        """
        if not self._session_id:
            logger.debug("cancel_session: no session_id, skip")
            return
        self._cancelled = True
        self._cancel_ts = time.monotonic()
        self._cancel_grace_secs = max(_CANCEL_GRACE_SECS, grace_secs)
        logger.debug(
            "cancel_session: sending session/cancel notification (sid=%s, turn_done=%s, proc_alive=%s)",
            self._session_id,
            self._turn_done.is_set(),
            self._is_process_alive(),
        )
        if not self._process or not self._process.stdin:
            logger.debug("cancel_session: process not running")
            return
        try:
            notification = {
                "jsonrpc": "2.0",
                "method": METHOD_CANCEL,
                "params": {"sessionId": self._session_id},
            }
            data = json.dumps(notification) + "\n"
            self._process.stdin.write(data.encode())
            await self._process.stdin.drain()
            self._last_activity = time.monotonic()
            logger.debug("cancel_session: wrote session/cancel notification")
        except Exception:
            logger.debug("Cancel notification failed", exc_info=True)

    async def steer(self, message: str) -> bool:
        """Inject a mid-turn steer into the running turn via kiro-cli's
        ``_session/steer`` ext-method. Fire-and-forget: the request is written
        but the response is NOT awaited, because the in-flight turn's read loop
        is the single consumer of this client's stdout and a concurrent wait
        would steal the turn's messages. kiro-cli answers ``{queued: true}`` and
        the authoritative signal is the ``steering_consumed`` notification; the
        steered reply streams back inside the SAME in-flight prompt. Returns
        False for an empty message or when there is no active session.
        """
        text = (message or "").strip()
        if not text or not self._session_id:
            return False
        wrapped = f"<user_message>\n{text}\n</user_message>"
        await self._send_request(
            "_session/steer", {"sessionId": self._session_id, "message": wrapped}
        )
        return True

    @property
    def supports_steer(self) -> bool:
        """True when the backend implements ``_session/steer`` (mid-turn steer).

        Membership in ``ACP_BACKENDS_STEER`` (harness-parity H6), so a harness
        added later does not inherit the extension from ``not _is_claude``.
        """
        return self.backend in ACP_BACKENDS_STEER

    async def wait_turn_done(self, timeout: float) -> str:
        """Wait for the current prompt to finish. Returns stop_reason or raises TimeoutError."""
        await asyncio.wait_for(self._turn_done.wait(), timeout=timeout)
        return self._last_stop_reason

    def has_active_turn(self) -> bool:
        """True if a prompt is in flight AND has not yet been cancelled.

        Returns False as soon as ``cancel_session()`` has been called, even
        before the agent acknowledges the cancel. Callers that need to force
        a kill regardless of cancel state should skip this check.
        """
        return not self._cancelled and not self._turn_done.is_set() and self._is_process_alive()

    def has_unfinished_turn(self) -> bool:
        """True if the native turn has NOT reached its done boundary and the
        process is still alive — INDEPENDENT of cancel state.

        Unlike :meth:`has_active_turn`, this does NOT exclude a turn that has
        already been ``cancel_session()``'d but whose native turn-done ack has
        not yet arrived. That turn still holds kiro-cli's native-session lock
        open, so killing the process now leaves the lock held and reproduces the
        empty-response-after-restart bug. The shutdown drain uses THIS signal so
        it still waits for such a turn's ack before the process is killed.
        """
        return not self._turn_done.is_set() and self._is_process_alive()

    # ── Private Helpers ──

    async def _send_prompt(self, message: str) -> int:
        # Shared with AcpSessionHandle.prompt via prompt_blocks so the two paths
        # cannot drift.
        return await self._send_request(
            METHOD_PROMPT,
            {
                "sessionId": self._session_id,
                # Offloaded: see the note in session_handle.prompt -- image
                # reads and base64 encoding must not block the event loop.
                "prompt": await asyncio.to_thread(build_prompt_blocks, message),
            },
        )

    async def _read_prompt_response(self, req_id: int, timeout: float) -> str:
        output: list[str] = []
        self.last_prompt_stats = self.last_prompt_stats.carry_over()

        async for action, msg in self._prompt_loop(req_id, timeout):
            if action == "complete":
                reason = ""
                result = msg.result or {}
                if isinstance(result, dict):
                    reason = result.get("stopReason", "") or ""
                self._last_stop_reason = reason
                self._turn_done.set()
                return "".join(output)
            if action == "error":
                _raise_acp_error(msg.error, self._advertised_model_ids())
            if action == "permission":
                await self._handle_permission(msg)
            elif action == "server_request_unknown":
                await self._reject_unknown_server_request(msg)
            elif action == "update":
                self._track_usage_update(msg)
                chunk, is_thinking = self._extract_text_chunk(msg)
                if chunk and not is_thinking:
                    output.append(chunk)
                    self.last_prompt_stats.text_chunks += 1
                    if _is_tool_interrupted_marker(chunk):
                        self._emit_tool_interrupted_sel("_read_prompt_response")
                        return "".join(output)  # see _dispatch_events for rationale
                self._track_tool_call(msg)
                # Mirror _dispatch_events for the send_message (worker-pool)
                # dispatch path: send_message drives tools through here, not
                # through the stream _dispatch_events, so without this
                # app/worker-pool subagents never reached hook/SEL parity. All
                # gating stays inside the _maybe_* methods (self._audit_source),
                # so main-chat send_message callers (audit_source=None) are no-op.
                tool_event = self._extract_tool_event(msg)
                if tool_event:
                    # Record every observed tool_call so PostToolUse can recover
                    # tool_name from _observed_tool_calls (see _maybe_fire_post_tool_hooks).
                    if tool_event.tool_call_id:
                        self._observed_tool_calls[tool_event.tool_call_id] = (
                            tool_event.title or "unknown",
                            tool_event.tool_kind or "",
                        )
                    await self._maybe_audit_tool_call(tool_event)
                    await self._maybe_note_skill_read(tool_event)
                    await self._maybe_fire_pre_tool_hooks(tool_event)
                tool_result_event = self._extract_tool_call_update(msg)
                if tool_result_event:
                    self._maybe_credit_skill_read(tool_result_event)
                    await self._maybe_fire_post_tool_hooks(tool_result_event)
            elif action == "metadata":
                self._track_metadata(msg)
            elif action == "compaction":
                self._handle_compaction_status(msg)

        self._last_stop_reason = ""
        self._turn_done.set()
        raise AcpTimeoutError(partial_output="".join(output))

    async def _handle_permission(self, msg: JsonRpcMessage) -> None:
        """Auto-approve tool permissions."""
        request_id = msg.id if msg.id is not None else ""

        params = msg.params or {}
        tool_call = params.get("toolCall", {})
        title = tool_call.get("title", "unknown")
        logger.info("Auto-approving tool: %s", title)

        await self.approve_tool(request_id)

    async def _reject_unknown_server_request(self, msg: JsonRpcMessage) -> None:
        """Answer an unrecognized server→client request with -32601.

        KiroCrew implements only ``session/request_permission`` as an inbound
        server request. Any other request (e.g. ``fs/read_text_file``,
        ``terminal/create``) has no handler, but JSON-RPC requires a response or
        the agent blocks forever. Reply ``Method not found`` so it fails fast.
        """
        if msg.id is None:
            return
        logger.warning("ACP: rejecting unknown server request: method=%s id=%s", msg.method, msg.id)
        await self._send_error(msg.id, _JSONRPC_METHOD_NOT_FOUND, f"Method not found: {msg.method}")

    def _extract_text_chunk(self, msg: JsonRpcMessage) -> tuple[str | None, bool]:
        """Extract text from an agent_message_chunk or agent_thought_chunk update.

        Returns (text, is_thinking). is_thinking is True when the chunk is an
        ``agent_thought_chunk`` (claude-agent-acp emits reasoning under this
        dedicated update type) or when an ``agent_message_chunk``'s inner
        content block type indicates reasoning (kiro-cli style).
        """
        params = msg.params or {}
        update = params.get("update", {})
        # The update comes straight from the agent process; a non-dict value
        # (null/list/string) would raise AttributeError here, inside the
        # prompt-turn dispatch path — same boundary rule as _track_usage_update.
        if not isinstance(update, dict):
            return None, False
        kind = update.get("sessionUpdate")
        if kind == UPDATE_AGENT_MESSAGE_CHUNK:
            content = update.get("content", {})
            if not isinstance(content, dict):
                return None, False
            text = content.get("text")
            content_type = content.get("type", "text")
            is_thinking = content_type in ("thinking", "reasoning")
            return text, is_thinking
        if kind == UPDATE_AGENT_THOUGHT_CHUNK:
            content = update.get("content", {})
            if not isinstance(content, dict):
                return None, True
            text = content.get("text")
            return text, True
        return None, False

    def _track_usage_update(self, msg: JsonRpcMessage) -> None:
        """Track context usage and config updates from session update notifications."""
        params = msg.params or {}
        update = params.get("update", {})
        kind = update.get("sessionUpdate") if isinstance(update, dict) else None
        if kind == UPDATE_USAGE:
            # used/size come straight from the agent process; a malformed
            # value (string, list, bool, NaN/Infinity, bignum beyond float
            # range) must degrade to "absent" rather than raise mid-turn.
            # parse_usage_update validates both fields at the shared
            # chokepoint used by AcpSessionHandle._handle_update too.
            used, size = parse_usage_update(update)
            if used is not None and size and size > 0:
                self.last_prompt_stats.context_pct = round((used / size) * 100, 1)
                # Keep the raw counts so the dashboard token text uses the real
                # served window (size) instead of re-deriving it from the model id.
                self.last_prompt_stats.context_used_tokens = int(used)
                self.last_prompt_stats.context_window_tokens = int(size)
                # Mark the counts authoritative so a later metadata
                # contextUsagePercentage cannot clobber this token-derived pct.
                self.last_prompt_stats.context_tokens_from_usage = True
                self.last_prompt_stats.note_pct_reported()
            else:
                logger.debug("usage_update missing used/size: %s", update)
        elif kind == UPDATE_CONFIG_OPTION:
            self._handle_config_option_update(msg)
        elif self._is_claude and kind and kind not in KNOWN_SESSION_UPDATES:
            logger.debug("Unhandled session update type: %s", kind)

    async def _maybe_audit_tool_call(self, tool_event: "AcpEvent") -> None:
        """Emit a per-tool-call SEL audit for clients with no external audit loop.

        App/worker-pool clients (code-review-sage, knowledge llm_pool) run tools
        through this AcpClient without going through chat_runner or SubagentManager,
        so their tool calls would otherwise never reach the security audit log.
        Gated on ``audit_source`` (None for chat / subagent clients, so they never
        double-log). Best-effort: a SEL failure must never break tool dispatch, but
        is logged at WARNING so audit-pipeline breakage surfaces to on-call.

        The ``sel().log_tool_invocation`` call is offloaded onto
        ``subprocess_executor()`` (the same dedicated pool the child-record
        offloads in this file use, e.g. ``_capture_child_records``) so that any
        SEL-backend I/O (file write, network, DNS) can never block the event loop
        and freeze the gateway heartbeat. The dedicated pool — rather than the
        default executor — isolates a call that can wedge on a stuck kernel
        resource, so a hung SEL backend cannot starve default-pool users. The
        offload is additionally bounded by ``asyncio.wait_for``: if the SEL call
        hangs, the ``await`` cannot stall this turn's tool dispatch indefinitely
        (a pending executor future never raises on its own) — the timeout raises
        ``TimeoutError`` (an ``Exception`` subclass), which the handler below
        swallows so dispatch always proceeds. Per the fault-isolation guideline a
        leaked worker thread is survivable; a stalled dispatch is not.
        ``tool_name``/``tool_kind`` use the same ``or`` fallbacks as the
        observed-tool-call bookkeeping above, so an audit record is always emitted
        with meaningful values rather than lost.
        """
        if not self._audit_source:
            return
        # Bind the guard-narrowed (non-None) audit_source into a local: mypy does
        # not carry the ``if not self._audit_source`` narrowing into the nested
        # lambda closure below, so referencing the attribute directly there would
        # be seen as ``str | None``.
        audit_source = self._audit_source
        try:
            await asyncio.wait_for(
                asyncio.get_running_loop().run_in_executor(
                    subprocess_executor(),
                    lambda: sel().log_tool_invocation(
                        session_key=self._session_key or "",
                        agent=self._agent,
                        source=audit_source,
                        tool_name=tool_event.title or "unknown",
                        tool_kind=tool_event.tool_kind or "",
                        outcome="auto_approved",
                    ),
                ),
                timeout=_SEL_AUDIT_TIMEOUT_SECONDS,
            )
        except Exception:
            logger.warning("ACP-layer SEL audit failed", exc_info=True)

    async def _maybe_note_skill_read(self, tool_event: "AcpEvent") -> None:
        """Resolve which skills a tool call is about to read, crediting later.

        Lives here because the ACP layer is the one place that sees EVERY
        surface's tool calls — dashboard, Slack, subagents, task runner. The
        per-surface permission gate (``HookManager.on_tool_call``) is not usable
        for this: file reads are auto-approved, so they never reach it.

        Resolution is filesystem-bound (a skills-tree walk after cache expiry,
        plus a ``resolve()`` per served skill), so it is offloaded to a thread —
        on the event loop it would stall every session in the gateway. Nothing
        is recorded here: the keys are held until ``_maybe_credit_skill_read``
        sees the tool complete, so a denied or failed read leaves no delivery.

        Fires for the initial ``tool_call`` and its ``tool_call_update``
        refinement, whichever first carries the arguments (claude-agent-acp
        leaves ``rawInput`` empty on the initial notification), deduped by
        ``tool_call_id``.

        Gated on the skill basename appearing in the arguments BEFORE any
        offload, so a tool call unrelated to skills costs one substring scan.
        Whether the call is a content-delivering READ (rather than a delete,
        move, or grep that merely names the path) is decided by the observer.
        Failures are swallowed: telemetry must not disturb the tool call.
        """
        observer = get_global_skill_read_observer()
        if observer is None:
            return
        tool_id = tool_event.tool_call_id or ""
        if tool_id and tool_id in self._skill_read_noted:
            return
        raw_params = tool_event.raw_tool_params
        command = tool_event.shell_command
        if not _mentions_skill_file(raw_params, command):
            return
        if tool_id:
            if len(self._skill_read_noted) >= _MAX_NOTED_SKILL_READS:
                # A single turn cannot legitimately hold this many distinct
                # skill reads; drop the tracking wholesale rather than letting
                # it grow for the life of the session. Worst case after a reset
                # is one duplicate credit, not a leak.
                self._skill_read_noted.clear()
                self._pending_skill_reads.clear()
            self._skill_read_noted.add(tool_id)
        try:
            keys = await asyncio.to_thread(
                observer.resolve_tool_read_keys,
                tool_event.tool_name or "",
                raw_params,
                command,
            )
        except Exception:
            logger.warning("skill-read resolution failed", exc_info=True)
            return
        if keys and tool_id:
            self._pending_skill_reads[tool_id] = keys

    def _maybe_credit_skill_read(self, tool_result_event: "AcpEvent") -> None:
        """Credit the reads resolved for a tool call that has now completed.

        Only a ``status == "completed"`` result (``tool_final``) credits, so a
        read that was denied, errored, or never ran contributes no delivery.
        In-memory only — the ledger debounces its own disk write — so this is
        safe to run inline on the event loop.
        """
        if not tool_result_event.tool_final:
            return
        tool_id = tool_result_event.tool_call_id or ""
        keys = self._pending_skill_reads.pop(tool_id, None) if tool_id else None
        if not keys:
            return
        observer = get_global_skill_read_observer()
        if observer is None:
            return
        try:
            observer.credit_skill_reads(keys)
        except Exception:
            logger.warning("skill-read credit failed", exc_info=True)

    async def _maybe_fire_pre_tool_hooks(self, tool_event: "AcpEvent") -> None:
        """Fire the PreToolUse HOOK ENGINE for a tool_call, for audit-source clients.

        App/worker-pool clients (code-review-sage, knowledge llm_pool) run their
        tools through this AcpClient without going through chat_runner or
        SubagentManager, so — until this method existed — the script-hook engine
        never fired for them. That silently dropped skill-usage telemetry (a
        PostToolUse hook matching 'Reading *SKILL.md*') for /add_context and every
        other subagent skill load. This brings those clients to hook parity with
        the main agent and SubagentManager subagents.

        Gated on ``audit_source`` (None for chat / subagent clients) so the
        chat/main client is completely unaffected — it fires its own hooks via
        chat_runner and must never double-fire. No-ops if the global hook store is
        not initialized. Best-effort + NON-FATAL: any hook-engine error is caught
        and logged at WARNING (mirroring the SEL audit handler above) and NEVER
        breaks tool dispatch — the hook fire is awaited directly, exactly as
        ``subagent.py`` awaits ``fire_tool_hooks`` (the underlying
        ``run_script_hook`` bounds each script with its own timeout).

        ``fire_tool_hooks`` fires PreToolUse ONLY (see hooks.py) — PostToolUse is
        fired separately in ``_maybe_fire_post_tool_hooks`` once the tool RESULT
        (and its output) is available.
        """
        if not self._audit_source:
            return
        hook_store = get_global_hook_store()
        if hook_store is None:
            return
        try:
            # Redact tool_input before firing user hooks (parity with Post path); addresses security-controls review.
            _redacted_input = tool_event.tool_input
            if isinstance(_redacted_input, str):
                _redacted_input, _ = redact_credentials(_redacted_input)
                _redacted_input, _ = redact_exfiltration_urls(_redacted_input)
            elif _redacted_input is not None:
                # Non-str (dict/list) inputs must ALSO be redacted, not bypassed by
                # the isinstance(str) guard. Serialize to JSON, redact the string,
                # and pass the redacted JSON string (fire_tool_hooks json.loads it,
                # so it expects a str | None — do NOT deserialize back to an object).
                _serialized = json.dumps(_redacted_input)
                _serialized, _ = redact_credentials(_serialized)
                _serialized, _ = redact_exfiltration_urls(_serialized)
                _redacted_input = _serialized
            await fire_tool_hooks(
                hook_store,
                # Fall back to 'unknown' when the event carries no title, matching
                # the Post path's tool_name recovery so a hook matcher sees a
                # consistent name across Pre/Post; addresses a code-review finding.
                tool_event.title or "unknown",
                _redacted_input,
                agent_role=self._agent or None,
            )
        except Exception:
            logger.warning("ACP-layer PreToolUse hook failed", exc_info=True)

    async def _maybe_fire_post_tool_hooks(self, tool_result_event: "AcpEvent") -> None:
        """Fire the PostToolUse HOOK ENGINE for a tool RESULT, for audit-source clients.

        Companion to ``_maybe_fire_pre_tool_hooks``. The Pre-vs-Post split is
        forced by the hook engine: ``fire_tool_hooks`` fires PreToolUse ONLY (at
        tool_call time the tool has not run and has no output), so PostToolUse must
        fire here, on the RESULT branch. The output MUST be carried on
        ``tool_response={'output': ...}`` — the IDENTICAL shape used by chat_runner
        (main agent) and subagent.py — because the skill-usage emit.sh reads the
        SKILL.md frontmatter out of ``tool_response.output`` (and matches the
        'Reading *SKILL.md*' tool_name). Without the output payload the telemetry
        hook fires blind and captures nothing.

        Gated on ``audit_source`` and no-ops if the global hook store is
        uninitialized. Best-effort + NON-FATAL: any error is caught + logged and
        never breaks dispatch. The tool RESULT event carries no title (see
        ``_build_tool_result_event``), so the tool_name is recovered from
        ``_observed_tool_calls`` (populated on the tool_call above) with the same
        'Running: ' strip ``fire_tool_hooks`` / subagent.py apply, so Pre and Post
        agree on the tool_name a hook matcher sees. ``tool_output`` is already
        redacted at the ACP boundary by ``_build_tool_result_event``.
        """
        if not self._audit_source:
            return
        hook_store = get_global_hook_store()
        if hook_store is None:
            return
        # Fall back to "unknown" to match the Pre path (Pre/Post tool_name consistency).
        tool_name = (
            self._observed_tool_calls.get(tool_result_event.tool_call_id or "", ("unknown", ""))[0]
            or "unknown"
        )
        if tool_name.startswith("Running: "):
            tool_name = tool_name[9:]
        try:
            # Redact before firing user hooks (parity with chat_runner PostToolUse); addresses security-controls review.
            _redacted_output, _ = redact_credentials(tool_result_event.tool_output or "")
            _redacted_output, _ = redact_exfiltration_urls(_redacted_output)
            # Bound the payload handed to user hook scripts to the first 2000 chars
            # (parity with chat_runner/subagent.py [:2000]). Redact-then-truncate is
            # deliberate: redact the FULL output first so secrets anywhere are scrubbed,
            # only THEN truncate — truncating first could leave a secret past char 2000.
            _redacted_output = _redacted_output[:2000]
            await hook_store.fire(
                HOOK_EVENT_POST_TOOL_USE,
                tool_name=tool_name,
                tool_response={"output": _redacted_output},
                agent_role=self._agent or None,
            )
        except Exception:
            logger.warning("ACP-layer PostToolUse hook failed", exc_info=True)

    def _emit_tool_interrupted_sel(self, site: str) -> None:
        """Emit a SEL audit event when kiro-cli cancels tool uses via its security filter.

        This is a security-relevant permission decision (kiro-cli denied tool execution)
        that KiroCrew observes but does not control.  Logged so the audit trail reflects
        that tools were blocked even though the decision was made outside KiroCrew.
        Also emits a single WARNING log line (grep-friendly for on-call) with session
        correlation — covers all three call sites so none of them fire silently.
        """
        logger.warning(
            "kiro-cli cancelled tool use(s) [site=%s session=%s]", site, self._session_id
        )
        try:
            # Re-imported at call time on purpose: the module-level binding is
            # captured at import time, so only this rebind resolves the CURRENT
            # ``kiro_crew.sel.sel`` and lets a substituted emitter be observed.
            from kiro_crew.sel import sel

            sel().log_tool_invocation(
                session_key=self._session_key or "",
                source="acp",
                tool_name="kiro_cli_security_filter",
                tool_kind="client_built_in",
                outcome="denied",
                metadata={"site": site, "reason": "tool_interrupted_marker"},
            )
        except Exception:
            logger.warning("SEL audit failed for tool_interrupted at %s", site, exc_info=True)

    def _track_tool_call(self, msg: JsonRpcMessage) -> None:
        """Track tool calls in stats (used by send_message/send_message_stream)."""
        params = msg.params or {}
        update = params.get("update", {})
        if not isinstance(update, dict):
            return
        if update.get("sessionUpdate") == UPDATE_TOOL_CALL:
            title = update.get("title", "unknown")
            kind = update.get("kind", "unknown")
            self.last_prompt_stats.tool_calls.append((kind, title))
            logger.debug("ACP tool_call: %s (%s)", title, kind)

    def _extract_tool_event(self, msg: JsonRpcMessage) -> AcpEvent | None:
        params = msg.params or {}
        update = params.get("update", {})
        if not isinstance(update, dict):
            return None
        if update.get("sessionUpdate") == UPDATE_TOOL_CALL:
            title = update.get("title", "unknown")
            kind = update.get("kind", "unknown")
            raw_input = update.get("rawInput") or update.get("input") or update.get("params")
            purpose = extract_tool_purpose(raw_input)
            logger.debug(
                "ACP tool_call raw: %s",
                {k: v for k, v in update.items() if k != "sessionUpdate"},
            )
            # Build initial tool input string from raw params
            tool_call_id = update.get("toolCallId", "")
            input_str = ""
            if tool_call_id and raw_input:
                input_str = (
                    json.dumps(raw_input, indent=2)
                    if isinstance(raw_input, (dict, list))
                    else str(raw_input)
                )
            # For edit tools with diff content blocks, generate unified diff
            found_diff = False
            content_blocks = update.get("content", [])
            if isinstance(content_blocks, list):
                for cb in content_blocks:
                    if isinstance(cb, dict) and cb.get("type") == "diff":
                        old = cb.get("oldText") or ""
                        new = cb.get("newText") or ""
                        path = cb.get("path", "")
                        diff_str = _make_unified_diff(old, new, path)
                        if diff_str:
                            input_str = diff_str
                            found_diff = True
                        break
            # Fallback for strReplace when no diff content block was found
            if (
                not found_diff
                and isinstance(raw_input, dict)
                and raw_input.get("command") == "strReplace"
            ):
                old = raw_input.get("oldStr") or ""
                new = raw_input.get("newStr") or ""
                path = raw_input.get("path") or ""
                if old or new:
                    diff_str = _make_unified_diff(old, new, path)
                    if diff_str:
                        input_str = diff_str
            # Redact sensitive content before caching/displaying
            if input_str:
                input_str, _ = redact_exfiltration_urls(input_str)
                input_str, _ = redact_credentials(input_str)
            if tool_call_id and input_str:
                self._tool_call_inputs[tool_call_id] = input_str
            # Cache the STRUCTURED raw params (path/url/command) so the later
            # request_permission event can feed the governance gate's arg-derived
            # scopes (filesystem.write / network.egress). Bounded by the same
            # clear() as _tool_call_inputs; capped to avoid unbounded growth on a
            # stream that never sends a matching permission request.
            if tool_call_id and isinstance(raw_input, dict):
                if len(self._tool_call_params) > _MAX_CACHED_TOOL_PARAMS:
                    self._tool_call_params.clear()
                self._tool_call_params[tool_call_id] = raw_input
            # Redact LLM-influenced fields before dashboard display
            if purpose:
                purpose, _ = redact_exfiltration_urls(purpose)
                purpose, _ = redact_credentials(purpose)
            # Prefer rawInput.description over the SDK-provided title (e.g.
            # some backends' Bash tool emits "List KiroCrew ACP module files"
            # alongside `ls /workplace/...`). For claude-agent-acp this rarely
            # fires here because the initial tool_call has empty rawInput —
            # the refinement path in `_extract_tool_call_refinement` is what
            # the user actually sees. Same helper is used in both places.
            # Capture the canonical shell signal from the raw kind BEFORE
            # redaction so the later permission_request event (which carries no
            # kind) can inherit it via the toolCallId cache below.
            is_shell = _is_shell_kind(kind)
            if tool_call_id:
                self._tool_call_is_shell[tool_call_id] = is_shell
                # Same lifecycle as is_shell: cache the trusted MCP server
                # identity so the later permission event can inherit it.
                self._tool_call_mcp_server[tool_call_id] = _kiro_mcp_server_name(update)
                # Cache the trusted tool name too, so the permission event can
                # rebuild mcp__<server>__<tool> for per-tool governance.
                self._tool_call_tool_name[tool_call_id] = _kiro_tool_name(update)
            title = _select_tool_title(title, raw_input, kind, is_shell=is_shell) or ""
            if title:
                title, _ = redact_exfiltration_urls(title)
                title, _ = redact_credentials(title)
            if kind:
                kind, _ = redact_exfiltration_urls(kind)
                kind, _ = redact_credentials(kind)
            self.last_prompt_stats.tool_calls.append((kind, title))
            # Trusted identity from _meta.kiro (NOT the LLM-authored title) —
            # shared with the _dispatch builder so both event paths carry it.
            return AcpEvent(
                kind=EVENT_TOOL_CALL,
                title=title,
                tool_kind=kind,
                tool_purpose=purpose,
                tool_input=input_str,
                tool_call_id=tool_call_id,
                raw_tool_params=raw_input if isinstance(raw_input, dict) else None,
                is_shell=is_shell,
                tool_name=_kiro_tool_name(update),
                mcp_server_name=_kiro_mcp_server_name(update),
            )
        return None

    def _extract_tool_call_update(self, msg: JsonRpcMessage) -> AcpEvent | None:
        """Extract a real-time tool result from a `tool_call_update` session update.

        kiro-cli streams tool completion via ACP `session/update` notifications
        (not just the JSONL session file). Two updates fire per tool:
          1. A `content` array carrying the tool output as text blocks — arrives
             as soon as the tool finishes, often mid-stream during the agent's
             follow-up text.
          2. A `status: completed` update with `rawOutput.items[].Json.stdout`
             for shell-style tools.
        Both carry the same `toolCallId`; we yield an EVENT_TOOL_RESULT on
        whichever provides output. Hooking these gives the inline pill its real
        output the moment the tool finishes, instead of waiting for the kiro-cli
        JSONL flush at the next tool_call boundary or message end.
        """
        params = msg.params or {}
        update = params.get("update", {})
        if not isinstance(update, dict) or update.get("sessionUpdate") != "tool_call_update":
            return None
        tool_use_id = update.get("toolCallId", "")
        if not tool_use_id:
            return None

        output_parts: list[str] = []

        # Path 1: `content` blocks (arrive during tool execution / mid-stream)
        content = update.get("content")
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                inner = block.get("content")
                if isinstance(inner, dict) and inner.get("type") == "text":
                    text = inner.get("text", "")
                    if text:
                        output_parts.append(str(text)[:4000])

        # Path 2: `rawOutput` (arrives with status=completed) — fallback when
        # there were no content blocks (e.g. some tools only emit rawOutput).
        # kiro-cli tool results land here in two shapes:
        #   items[].Text  — fs_read contents, shell-style text, etc.
        #   items[].Json  — structured tool output (use .stdout when present)
        if not output_parts:
            raw_output = update.get("rawOutput")
            if isinstance(raw_output, dict):
                items = raw_output.get("items", [])
                if isinstance(items, list):
                    for item in items:
                        if not isinstance(item, dict):
                            continue
                        if "Text" in item and item.get("Text"):
                            output_parts.append(str(item["Text"])[:4000])
                            continue
                        j = item.get("Json")
                        if isinstance(j, dict):
                            if "stdout" in j and j.get("stdout"):
                                output_parts.append(str(j["stdout"])[:4000])
                            else:
                                output_parts.append(json.dumps(j, default=str)[:4000])

        if not output_parts:
            return None

        final_output = "\n".join(output_parts)[:8000]
        final_output, _ = redact_exfiltration_urls(final_output)
        final_output, _ = redact_credentials(final_output)
        return AcpEvent(
            kind=EVENT_TOOL_RESULT,
            tool_call_id=tool_use_id,
            tool_output=final_output,
            tool_final=update.get("status") == "completed",
        )

    def _extract_tool_call_refinement(self, msg: JsonRpcMessage) -> AcpEvent | None:
        """Extract a refined title/kind/input from a `tool_call_update`.

        claude-agent-acp emits two events per tool: an initial `tool_call`
        on streaming `content_block_start` (when `chunk.input` is still empty,
        so the title falls back to the generic tool name like "Terminal" or
        "grep"), then a follow-up `tool_call_update` once `chunk.input` is
        fully streamed — that update carries the populated `rawInput` and a
        refined `title`/`kind` from the upstream `toolInfoFromToolUse`
        (e.g. `"ls /local/home/user/.kiro/crew/workspace"`).

        We yield an EVENT_TOOL_CALL_UPDATE so the dashboard can patch the
        existing pill / persisted message in place — see the matching
        handler in `chat_runner.py`. Returns None when the update only
        carries output (handled separately by `_extract_tool_call_update`).
        """
        params = msg.params or {}
        update = params.get("update", {})
        if not isinstance(update, dict) or update.get("sessionUpdate") != "tool_call_update":
            return None
        tool_use_id = update.get("toolCallId", "")
        if not tool_use_id:
            return None
        title = update.get("title")
        kind = update.get("kind")
        raw_input = update.get("rawInput")
        # Only emit when at least one refinement field is present. Pure-output
        # updates (content/rawOutput only) are handled by the result extractor.
        if title is None and kind is None and not raw_input:
            return None
        # Build the input string the same way `_extract_tool_event` does so
        # the merged toolLog entry / message meta lines up across both events.
        input_str = ""
        if isinstance(raw_input, (dict, list)) and raw_input:
            try:
                input_str = json.dumps(raw_input, indent=2)
            except (TypeError, ValueError):
                input_str = str(raw_input)
        elif isinstance(raw_input, str):
            input_str = raw_input
        # Edit-style diff content blocks: prefer the rendered unified diff over
        # the raw input dict (mirrors `_extract_tool_event`).
        content_blocks = update.get("content", [])
        if isinstance(content_blocks, list):
            for cb in content_blocks:
                if isinstance(cb, dict) and cb.get("type") == "diff":
                    old = cb.get("oldText") or ""
                    new = cb.get("newText") or ""
                    path = cb.get("path", "")
                    diff_str = _make_unified_diff(old, new, path)
                    if diff_str:
                        input_str = diff_str
                    break
        if input_str:
            input_str, _ = redact_exfiltration_urls(input_str)
            input_str, _ = redact_credentials(input_str)
            self._tool_call_inputs[tool_use_id] = input_str
        # Refresh the cached shell signal only when this refinement carries a
        # kind. A refinement that omits kind must NOT clobber a True cached by
        # the initial tool_call notification (kind is optional on updates).
        # Cache off the RAW kind, not the redacted kind_str. Resolved BEFORE the
        # title so the label rule sees the real classification rather than a
        # missing kind.
        if isinstance(kind, str) and kind:
            self._tool_call_is_shell[tool_use_id] = _is_shell_kind(kind)
        is_shell = self._tool_call_is_shell.get(tool_use_id, False)
        # Prefer rawInput.description over the SDK-supplied title (e.g.
        # Bash's "List KiroCrew ACP module files" rather than `ls /workplace/...`).
        # Same helper as `_extract_tool_event` so the rule is consistent.
        # A refinement carrying a title but no rawInput still overwrites the
        # pill, so the command has to be recoverable from the params the initial
        # tool_call cached — otherwise a backend that sends a generic title on
        # both events lands that label on a pill the first event got right.
        _title_params: object = raw_input
        if not (isinstance(raw_input, dict) and raw_input):
            _title_params = self._tool_call_params.get(tool_use_id)
        title_source = _select_tool_title(title, _title_params, kind, is_shell=is_shell)
        title_str = ""
        if title_source:
            title_str, _ = redact_exfiltration_urls(title_source)
            title_str, _ = redact_credentials(title_str)
        kind_str = ""
        if isinstance(kind, str) and kind:
            kind_str, _ = redact_exfiltration_urls(kind)
            kind_str, _ = redact_credentials(kind_str)
        # The refinement's rawInput is the COMPLETE params object, so it carries
        # the reserved purpose argument too. Read it here or the purpose is lost
        # whenever the initial tool_call streamed an empty rawInput — and
        # consumers that treat an empty purpose as "fall back to the raw title"
        # (the session list's running-status line) would replace a good purpose
        # with a command. Mirrors `_dispatch._build_tool_refinement_event`.
        purpose = extract_tool_purpose(raw_input)
        if purpose:
            purpose, _ = redact_exfiltration_urls(purpose)
            purpose, _ = redact_credentials(purpose)
        return AcpEvent(
            kind=EVENT_TOOL_CALL_UPDATE,
            title=title_str,
            tool_kind=kind_str,
            tool_purpose=purpose,
            tool_input=input_str,
            tool_call_id=tool_use_id,
            raw_tool_params=raw_input if isinstance(raw_input, dict) else None,
            is_shell=is_shell,
        )

    def _read_new_tool_results_sync(self) -> list[AcpEvent]:
        """Read new ToolResults entries from the kiro-cli session JSONL file."""
        if not self._session_id:
            return []
        jsonl_path = kiro_sessions_dir() / f"{self._session_id}.jsonl"
        if not jsonl_path.exists():
            return []
        results: list[AcpEvent] = []
        try:
            with open(jsonl_path, "r") as f:
                f.seek(self._jsonl_pos)
                while True:
                    line = f.readline()
                    if not line:
                        break
                    if not line.endswith("\n"):
                        break  # partial line — retry next call
                    self._jsonl_pos = f.tell()
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if entry.get("kind") != "ToolResults":
                        continue
                    for c in entry.get("data", {}).get("content", []):
                        if c.get("kind") != "toolResult":
                            continue
                        tr = c.get("data")
                        if not isinstance(tr, dict):
                            continue
                        tool_use_id = tr.get("toolUseId", "")
                        output_parts: list[str] = []
                        for rc in tr.get("content", []):
                            if not isinstance(rc, dict):
                                continue
                            if rc.get("kind") == "json":
                                d = rc.get("data", {})
                                if isinstance(d, dict) and "stdout" in d:
                                    out = d.get("stdout", "")
                                    if out:
                                        output_parts.append(out[:4000])
                                else:
                                    output_parts.append(json.dumps(d, indent=2)[:4000])
                            elif rc.get("kind") == "text":
                                output_parts.append(str(rc.get("data", ""))[:4000])
                        if output_parts:
                            results.append(
                                AcpEvent(
                                    kind=EVENT_TOOL_RESULT,
                                    tool_call_id=tool_use_id,
                                    tool_output="\n".join(output_parts)[:8000],
                                )
                            )
        except Exception:
            logger.debug("Failed to read JSONL for tool results", exc_info=True)
        if results:
            logger.debug("JSONL: read %d tool result(s) from %s", len(results), jsonl_path.name)
        return results

    def _build_permission_event(self, msg: JsonRpcMessage) -> AcpEvent:
        request_id = msg.id if msg.id is not None else ""
        params = msg.params or {}
        # The permission payload comes straight from the agent process. A
        # malformed toolCall (null/list/string) or options (null/dict/string,
        # or non-dict entries) would raise AttributeError/TypeError here — and
        # this runs inside the prompt-turn event generator, so the crash tears
        # down the whole turn instead of degrading to the default options.
        tool_call = params.get("toolCall", {})
        if not isinstance(tool_call, dict):
            tool_call = {}
        title = tool_call.get("title", "unknown")
        # The ACP toolCall carries a `kind` ("execute" for Bash, "read"/"edit"/
        # …). Carry it onto the event as display/telemetry metadata. NOTE: the
        # tool-name length-cap exemption does NOT key off this value — it uses
        # the is_shell flag resolved below from the trusted tool_call cache, not
        # the agent-influenced permission payload. Missing/empty stays "".
        tool_kind = tool_call.get("kind", "")
        # ACP spec uses optionId/name + kind ("allow_once"|"allow_always"|
        # "reject_once"|"reject_always"); kiro-cli uses id/label
        # with id values "allow_once"/"allow_always". Accept both shapes and
        # remember the actual optionIds keyed by kind so approve_tool/
        # reject_tool can echo the exact id the agent advertised.
        options: list[dict[str, str]] = []
        kind_to_id: dict[str, str] = {}
        raw_options = params.get("options", [])
        if not isinstance(raw_options, list):
            raw_options = []
        for o in raw_options:
            if not isinstance(o, dict):
                continue
            opt_id = o.get("optionId") or o.get("id") or ""
            opt_label = o.get("name") or o.get("label") or ""
            opt_kind = o.get("kind") or ""
            # A non-string id would crash opt_id.lower() below (and non-string
            # label/kind would leak into the typed options list) — skip them.
            if not isinstance(opt_id, str) or not opt_id:
                continue
            if not isinstance(opt_label, str):
                opt_label = ""
            if not isinstance(opt_kind, str):
                opt_kind = ""
            options.append({"id": opt_id, "label": opt_label})
            if not opt_kind:
                # Only synthesize a kind for well-known literals; unknown ids
                # leave kind empty so we don't mis-classify agent intent.
                opt_kind = _LEGACY_OPTION_KIND.get(opt_id.lower(), "")
            if opt_kind:
                kind_to_id.setdefault(opt_kind, opt_id)
        if not options:
            options = [
                {"id": OPTION_ALLOW_ONCE, "label": "Allow once"},
                {"id": OPTION_ALLOW_ALWAYS, "label": "Allow always"},
            ]
            kind_to_id = {"allow_once": OPTION_ALLOW_ONCE, "allow_always": OPTION_ALLOW_ALWAYS}
        # Record optionIds the agent advertised so approve_tool / reject_tool
        # can echo the exact ids. We record when EITHER an allow option (for
        # approve) OR a reject option (for a clean reject) was advertised.
        # claude-agent-acp advertises a {kind:"reject_once", optionId:"reject"}
        # option whose selection yields behavior:"deny" — sending that is far
        # better than a "cancelled" outcome, which the adapter turns into the
        # cryptic "Tool use aborted". kiro-cli advertises no reject option, so
        # reject_tool falls back to "cancelled" there (handled as a clean
        # rejection by kiro).
        any_allow = kind_to_id.get("allow_once") or kind_to_id.get("allow_always")
        any_reject = kind_to_id.get("reject_once") or kind_to_id.get("reject_always")
        if request_id != "" and (any_allow is not None or any_reject is not None):
            recorded: dict[str, str] = {}
            if any_allow is not None:
                recorded["once"] = kind_to_id.get("allow_once") or any_allow
                recorded["always"] = kind_to_id.get("allow_always") or any_allow
            if any_reject is not None:
                recorded["reject"] = any_reject
            self._permission_options[request_id] = recorded

        # Resolve full tool input — the preceding ToolCall session/notification
        # carries the complete params that we cache by toolCallId.  The
        # request_permission message only has a truncated human-readable title.
        tool_input = ""
        tool_call_id = tool_call.get("toolCallId", "")

        # 1. Look up cached input from the ToolCall notification
        if tool_call_id and tool_call_id in self._tool_call_inputs:
            tool_input = self._tool_call_inputs.pop(tool_call_id)

        # Recover the STRUCTURED raw params cached at the ToolCall notification
        # (or carried inline on this permission message) so the governance gate
        # can evaluate the filesystem.write (edit path) / network.egress (fetch
        # url) scopes — the title alone cannot carry these.
        raw_params: dict | None = None
        if tool_call_id and tool_call_id in self._tool_call_params:
            raw_params = self._tool_call_params.pop(tool_call_id)

        # 2. Fallback: check if toolCall itself carries input/params
        if not tool_input:
            raw_input = tool_call.get("input") or tool_call.get("params")
            if raw_input:
                tool_input = (
                    json.dumps(raw_input, indent=2)
                    if isinstance(raw_input, (dict, list))
                    else str(raw_input)
                )
                if raw_params is None and isinstance(raw_input, dict):
                    raw_params = raw_input
        elif raw_params is None:
            # tool_input came from the cache; the permission message may still
            # carry an inline params dict the gate can use.
            inline = tool_call.get("input") or tool_call.get("params")
            if isinstance(inline, dict):
                raw_params = inline

        # Resolve the canonical shell signal. SECURITY (deny-by-default): the
        # ONLY trusted source is the value cached from the preceding tool_call
        # notification (keyed by toolCallId). We deliberately do NOT fall back
        # to the permission payload's own `kind`: that field is agent/LLM-
        # influenced, and trusting it to waive the tool-name length cap on the
        # very name being validated would let a malicious agent set
        # kind="execute" on the permission payload to bypass the check. On a
        # cache miss is_shell stays False and the length cap is enforced.
        #
        # Use .get() (not .pop()): a later tool_call_update refinement for the
        # same toolCallId reads this same cache, so popping here would make that
        # refinement wrongly report is_shell=False if it arrives after the
        # permission event. The per-turn dispatch-loop .clear() handles cleanup.
        cached_shell = self._tool_call_is_shell.get(tool_call_id) if tool_call_id else None
        is_shell = bool(cached_shell)
        if cached_shell is None and tool_input:
            # Input resolved but the shell signal did not — the cache invariant
            # (tool_call precedes permission) did not hold. Surface it so the
            # miss is observable rather than silently enforcing the length cap.
            logger.info(
                "Permission event resolved tool_input but missed is_shell cache "
                "(req=%s tool_call_id=%s)",
                request_id,
                tool_call_id,
            )

        logger.info("Permission requested for tool: %s (req=%s)", title, request_id)
        if logger.isEnabledFor(logging.DEBUG):
            _tc_redacted = repr(tool_call)
            _tc_redacted, _ = redact_exfiltration_urls(_tc_redacted)
            _tc_redacted, _ = redact_credentials(_tc_redacted)
            logger.debug("Permission toolCall payload: %s", _tc_redacted)
        return AcpEvent(
            kind=EVENT_PERMISSION_REQUEST,
            request_id=request_id,
            title=title,
            tool_kind=tool_kind,
            options=options,
            tool_input=tool_input,
            tool_call_id=tool_call_id,
            raw_tool_params=raw_params,
            is_shell=is_shell,
            # Trusted MCP server identity recovered from the preceding tool_call
            # (the permission payload has no _meta). .get() mirrors the is_shell
            # cache-read; empty on a miss (fail-closed for app-own auto-approve).
            mcp_server_name=(
                self._tool_call_mcp_server.get(tool_call_id, "") if tool_call_id else ""
            ),
            # Trusted tool name recovered from the preceding tool_call, mirroring
            # mcp_server_name — lets the app-own-server auto-approve govern the
            # canonical mcp__<server>__<tool> on this permission path.
            tool_name=(
                self._tool_call_tool_name.get(tool_call_id, "") if tool_call_id else ""
            ),
        )

    def _backfill_context_window(self, pct: float) -> None:
        """Derive window/used tokens from a percentage-only reading.

        Thin wrapper binding this client's resolved model id; the shared logic
        lives on ``AcpPromptStats.backfill_context_window`` (the AcpSessionHandle
        path delegates to the same method, so the two can no longer drift).
        """
        self.last_prompt_stats.backfill_context_window(
            pct, self._resolved_model_id or self._model
        )

    def _track_metadata(self, msg: JsonRpcMessage) -> None:
        params = msg.params or {}
        # A real usage_update is authoritative for both the token counts AND the
        # pct derived from them. kiro's metadata percentage can measure a
        # different window, so applying it here would desync the headline % from
        # the "used / total" token text (e.g. 73% shown next to 408K / 1000K).
        # sanitize_pct is the shared coercion (the KAS usagePercentage path uses
        # it too): it clamps NaN/±inf/out-of-range and returns None when absent.
        pct_f = self.last_prompt_stats.sanitize_pct(params.get("contextUsagePercentage"))
        if pct_f is not None and not self.last_prompt_stats.context_tokens_from_usage:
            self.last_prompt_stats.context_pct = pct_f
            self.last_prompt_stats.note_pct_reported()
            self._backfill_context_window(pct_f)
        # kiro streams per-turn billing as meteringUsage entries (unit="credit").
        # Accumulate across the turn's metadata notifications; reset per turn by
        # the AcpPromptStats re-init in _dispatch_events/send_message_stream.
        metering = params.get("meteringUsage")
        if isinstance(metering, list):
            for entry in metering:
                if isinstance(entry, dict) and entry.get("unit") == "credit":
                    try:
                        self.last_prompt_stats.credits += float(entry.get("value", 0) or 0)
                    except (TypeError, ValueError):
                        pass

    def _handle_compaction_status(self, msg: JsonRpcMessage) -> None:
        """Log a ``_kiro.dev/compaction/status`` notification and, on
        completion, drop the now-stale context-usage counts.

        This is the single chokepoint every compaction-status arrival routes
        through (all prompt dispatch loops and ``wait_for_compaction``), so the
        reset cannot be missed by one path. Without it the pre-compaction
        counts survive — and their ``context_tokens_from_usage=True`` flag
        blocks ``_track_metadata`` from applying any fresh percentage — so the
        dashboard's context meter kept showing the old usage after a compact.
        """
        params = msg.params or {}
        status = params.get("status", "")
        logger.info("Compaction status: %s", status)
        # On failure, kiro-cli's notification carries no dedicated error/reason
        # field today (only `status.type` + an optional `summary`, which is
        # populated on success but typically empty on failure). Log the full
        # raw params at WARNING so a future occurrence is actually debuggable
        # instead of surfacing only "unknown error" to the user with nothing
        # to grep for server-side. See Mesh compaction-spam investigation.
        s_type = status.get("type", "") if isinstance(status, dict) else str(status)
        if s_type == "failed":
            logger.warning("Compaction failed — raw notification params: %s", params)
        elif s_type == "completed":
            self.last_prompt_stats.reset_after_compaction()

    async def wait_for_compaction(self, timeout: float = COMPACT_WAIT_TIMEOUT_SECS) -> dict:
        """Read messages until compaction completed/failed arrives. Returns status dict.

        On ``completed``, keeps draining for a short grace window: kiro-cli
        emits a fresh ``_kiro.dev/metadata`` with the REAL post-compaction
        ``contextUsagePercentage`` about a second after the completed status
        (live-probe confirmed). ``_handle_compaction_status`` has already
        dropped the stale counts (clearing the authoritative flag), so that
        metadata re-derives accurate numbers — the caller's ``context_usage``
        broadcast then reports the true compacted size instead of an unknown.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            msg = await self._read_message(timeout=min(remaining, _READ_TIMEOUT))
            if msg is None:
                continue
            if msg.is_method(METHOD_COMPACTION_STATUS):
                self._handle_compaction_status(msg)
                params = msg.params or {}
                status = params.get("status", {})
                s_type = status.get("type", "") if isinstance(status, dict) else str(status)
                if s_type in ("completed", "failed"):
                    self._track_metadata(msg)
                    if s_type == "completed":
                        await self._drain_post_compaction_metadata()
                    return {"type": s_type, "summary": params.get("summary", "")}
            elif msg.is_method(METHOD_METADATA):
                self._track_metadata(msg)
            else:
                # Don't drop — buffer for later processing
                if msg.method and not msg.id:
                    self._mcp_notifications.append(msg)
        return {"type": "timeout"}

    async def _drain_post_compaction_metadata(
        self, grace: float = _POST_COMPACTION_METADATA_GRACE_SECS
    ) -> None:
        """Drain for the post-compaction ``_kiro.dev/metadata`` notification.

        Returns as soon as a metadata frame carrying a real
        ``contextUsagePercentage`` is applied — a credits-only/empty metadata
        frame is consumed but does NOT end the drain, or the usage frame
        behind it would be stranded and the meter would fall back to the
        reset/unknown state. Gives up quietly at the grace deadline (the
        meter then self-corrects on the next turn's telemetry). Non-metadata
        notifications are buffered exactly like the main wait loop. Process
        death (``AcpError``) propagates — the outer ``wait_for_compaction``
        contract lets it, and swallowing it here would report a completed
        compaction on a dead runtime.
        """
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            try:
                msg = await self._read_message(timeout=remaining)
            except AcpError:
                raise
            except Exception:
                return
            if msg is None:
                continue
            if msg.is_method(METHOD_METADATA):
                self._track_metadata(msg)
                if (msg.params or {}).get("contextUsagePercentage") is not None:
                    return
                continue
            if msg.method and not msg.id:
                self._mcp_notifications.append(msg)
