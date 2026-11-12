"""The Codex ACP adapter: one Node process, N sessions, and its own confinement.

``@agentclientprotocol/codex-acp`` is a Node stdio server. It boots a single
``codex app-server`` child and translates ACP onto that server's operations. The
``codex`` CLI does not serve ACP itself -- it reads ``acp`` as a prompt -- so the
adapter is the transport, not an optimization.

**One process already hosts many sessions, which is why this harness exists.** The
adapter keeps ``this.sessions = new Map()`` and each ACP session id is a Codex
thread id on the one shared ``codex app-server`` child. N Crew sessions on one
adapter is therefore the adapter's own design, not something Crew layers on top.
:class:`~kiro_crew.acp.client.AcpClient` starts one adapter per session and pays N
Node processes and N app-servers for N sessions; ``AcpRuntime`` pays one.

**What a harness earns, and what it does not.** ``AcpProvider`` builds its own
``AcpRuntime`` per chat, so being runnable on the runtime does not by itself put two
chats on one process. Which hosts put a SUBAGENT's session on the parent's runtime is
``ACP_BACKENDS_SESSION_SHARING``, and codex is a member: a foreground chat still owns
its own runtime, and its subagents ride on that one rather than paying a Node process
and an app-server each.

Sharing here does not mean a session that outlives its parent. Teardown still sends
``session/close`` and the session still leaves the process -- a resident subagent
session would hold its own MCP fleet on a runtime nobody is using. What a later
``spawn_continue`` addresses is the thread ``codex`` persisted under ``CODEX_HOME``,
restored with ``session/load``; ``ACP_BACKENDS_HARNESS_OWNED_SESSIONS`` is the set
that says codex resolves such a load from the sessionId alone.

Eviction is a separate question with a separate answer, and the answer did not
change. The teardown Crew sends is ``session/close``, and it evicts: after it the
sessionId stops answering, so a session Crew drops actually leaves the process. That
is what admits codex to ``ACP_BACKENDS_SESSION_EVICTION``, and through it to every
path that creates and destroys sessions on a shared process -- the high-churn
background handles (``session._bg_runtime_backends``), warm pooled reuse, the
entitlement probe. One teardown verb answers for all of them, which is the point of
declaring it on the harness rather than gating each caller.

**One thing is global on a shared process, and a caller has to know it.**
``providers/set`` restarts the ``codex app-server`` child and then re-resumes every
thread on it. On a per-session adapter that is one user's session. Here it is EVERY
session on the process. Nothing in this module sends ``providers/set``, and nothing
should start sending it per session.

The host's ``codex``, behind a version floor
--------------------------------------------
codex-acp runs ``app-server`` from ``CODEX_PATH`` when it is set and from the
``@openai/codex`` it bundles otherwise. The operator's own ``codex`` is the one
their login and model provider were set up for, so Crew hands it over -- but only
when its ``--version``, read inside the session's sandbox, is at least the bundled
package's version. The adapter is released against that bundled build; a host
binary that is older, or whose version cannot be read, keeps the adapter on the
bundled Codex, and an INFO line says which case it was. An operator's
``CODEX_PATH``, empty included, is never second-guessed.

Codex is confined by Crew's sandbox and by nothing else
------------------------------------------------------
``ACP_BACKENDS_INTERNAL_SANDBOX`` does not name codex, so :attr:`internal_sandbox`
is False and Crew's own seatbelt/bubblewrap wrapper is the only OS confinement a
codex session gets. The Codex sandbox modes the adapter can apply are in-process
policy, not an OS sandbox Crew's could nest inside.

Crew's tool gate ENFORCES codex, so the spawn carries a credential mask on its
``SpawnPlan``. For an enforced host that mask is the only thing between a
third-party binary and the operator's credential homes: ACP cannot make such a host
ask about a passive read.

Two tiers hand back an UNWRAPPED child and drop that mask on the floor -- ``off``,
and a host with no sandbox backend where unsandboxed exec is opted in. An enforced
host that spawned there would run a third-party binary with the operator's credential
homes readable and nothing compensating for it, so :func:`resolve_spawn_masks`
REFUSES those tiers instead of returning an empty mask.

Permission routing is asserted per session, not seeded to a file
----------------------------------------------------------------
codex-acp's default ``agent`` mode writes inside the workspace without asking. Its
ACP v1 ``mode`` selector is the enforceable boundary, written over
``session/set_config_option`` -- ``Routing.SESSION_CONFIG`` in
``ACP_BACKEND_ROUTING``, which both drivers read for themselves. This harness
declares no routing member: a second copy of that table would be free to disagree
with the one that decides. There is no settings file to author first either, which
is why this harness has no ordering constraint the claude one has. Crew writes
``external-sandbox`` when the adapter offers it (Crew's OS wrap, Codex's
``untrusted`` approvals, no second OS sandbox) and ``read-only`` otherwise. Either
still permits passive READS; ACP v1 has no way to require a prompt for those, and
what makes the residual gap survivable is that mask.

The session array is the whole tool surface, and one bad element costs all of it
--------------------------------------------------------------------------------
codex reads no agent spec, so ``session/new``'s ``mcpServers`` is everything the
session will ever have. Since 2.3.0 the adapter validates that array against the
ACP schema and fails the WHOLE ``session/new`` over one element it will not take:
an ``sse`` element while ``mcpCapabilities`` advertises ``sse: false``, or a
control element typed ``nonsense-type``. Up to 2.2.2 a bare one was ACCEPTED and
never wired, a session running with a tool the operator declared simply absent.

Either way the narrowing matters.
:meth:`CodexHarness.session_mcp_servers` is therefore the ONLY thing standing
between the array Crew composed and an array the adapter will honour, narrowed
against the capabilities THIS session's handshake reported.

Empty capabilities mean "nothing is known", never "nothing is supported": on a
handshake that advertised none, the array passes through untouched. Narrowing to
nothing there would strip every tool from every session.

The recycle ceiling is measured, and the scope is what it measures
-----------------------------------------------------------------
Seam 9 is overridden, because the runtime's default cannot express this host's
shape. The default ceiling is 500 MB over the adapter's whole descendant subtree,
which was chosen for kiro-cli, where the growth is in one child.

A codex process is three things, and only two of them are Crew's business. The
adapter is FLAT: 103 MB at zero sessions, 85 MB at eight -- it multiplexes without
growing. ``codex app-server`` grows gently and sub-linearly, +14 MB per session on
a minimal config and +37 MB with a host MCP registry. Together those two are the
core: 224 MB idle, and 522 MB at eight sessions in the worse of the two runs.

The third thing is a per-session fleet of MCP servers that ``codex`` starts from
its OWN configuration -- roughly 44 processes per session on a real host config,
none of it shared between sessions, and it appears even when Crew passes
``mcpServers: []``. That is 2751 MB at ONE session, 21551 MB at eight.

So the subtree total crosses 500 MB on the FIRST prompt of the FIRST session, on
both runs, and the default ceiling recycles a healthy process before it has served
a single turn. Raising the ceiling instead does not work either: the per-session
term is set by the user's own codex configuration, so any subtree number Crew
picks is either useless or arbitrary.

The scope is therefore the fix, and the ceiling follows from it: measure the core
(``rss_depth=1`` -- the adapter and its direct children) against 1024 MB, which
leaves real headroom over the measured 522 MB and still catches a genuine leak in
the part of the process Crew put there. The fleet is not ignored so much as
attributed: its size is a fact about the operator's codex config, and a ceiling is
not the instrument that governs it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import subprocess as subprocess_mod
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from kiro_crew import acp_tool_gate, platform_compat
from kiro_crew.acp import launch as launch_mod
from kiro_crew.acp.codex_sqlite import SQLITE_HOME_ENV, seed_private_state
from kiro_crew.acp.codex_subagent_wire import CODEX_CLIENT_CAPABILITIES
from kiro_crew.acp.harness._common import MembershipHarness
from kiro_crew.acp.harness.base import (
    NotificationAliases,
    ReclaimPolicy,
    SessionExtras,
    SpawnContext,
    SpawnPlan,
    TeardownPolicy,
)
from kiro_crew.acp.types import (
    ACP_BACKEND_CODEX,
    METHOD_SESSION_CLOSE,
    METHOD_SESSION_UPDATE,
)
from kiro_crew.agent_sdk.codex_transcripts import delete_codex_session_files
from kiro_crew.providers.mirrors.codex import drop_unadvertised_transports
from kiro_crew.sandbox import detect_backend, wrap_argv, wrap_argv_async

__all__ = ["PROTOCOL_VERSION_CODEX", "CodexHarness"]

logger = logging.getLogger(__name__)

#: codex-acp numbers ACP revisions, like the claude adapter and unlike kiro-cli's
#: date string. Kept as this harness's OWN literal even though the integer matches
#: claude's today: a divergence should be a one-line edit here rather than a silent
#: downgrade of whichever harness moved first.
PROTOCOL_VERSION_CODEX = 1

#: Where ``codex`` keeps its SQLite databases; defaults to ``CODEX_HOME``.
_SQLITE_HOME_ENV = SQLITE_HOME_ENV

#: The whole codex configuration directory; MCP fleet is started from its config.toml.
_CODEX_HOME_ENV = "CODEX_HOME"


#: Inert transport values for a disable entry. codex parses every
#: ``mcp_servers`` entry with ``TryFrom<RawMcpServerConfig>``, which needs exactly
#: one of ``command`` (stdio) or ``url`` (streamable HTTP) and fails the whole
#: ``thread/start`` with ``invalid transport`` otherwise. codex re-reads
#: ``config.toml`` on every ``thread/start`` while ``CODEX_CONFIG`` is fixed for
#: the adapter's lifetime, so a server removed or renamed after the spawn leaves
#: the override standing alone. Carrying the transport key the server had at spawn
#: keeps that orphan a valid, disabled entry. Over a live entry it only replaces
#: the command or URL of a server that ``enabled = false`` stops codex starting.
_DISABLED_STDIO_COMMAND = "true"
_DISABLED_HTTP_URL = "http://127.0.0.1/"


def _read_codex_mcp_disable_overrides(config_file: Path) -> dict[str, dict[str, Any]]:
    """Return one ``enabled = false`` entry per ``mcp_servers`` server, or ``{}``.

    Each entry also carries a placeholder for the transport key the server
    uses (``command`` or ``url``), so it still parses if the server is gone
    from ``config.toml`` by the time codex reads it. A server with neither key
    is left out: codex already rejects it without this override. Parsed with
    stdlib ``tomllib``, in config.toml order. A missing, unreadable or
    malformed file yields ``{}``, so the caller injects no override. Blocking
    file I/O: call it through ``asyncio.to_thread``.
    """
    try:
        if not config_file.is_file():
            return {}
        text = config_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return {}
    servers = data.get("mcp_servers", {})
    if not isinstance(servers, dict):
        return {}
    overrides: dict[str, dict[str, Any]] = {}
    for name, entry in servers.items():
        if not isinstance(entry, dict):
            continue
        if "command" in entry:
            overrides[name] = {"enabled": False, "command": _DISABLED_STDIO_COMMAND}
        elif "url" in entry:
            overrides[name] = {"enabled": False, "url": _DISABLED_HTTP_URL}
    return overrides


#: The variable codex-acp reads for the ``codex`` binary it runs ``app-server`` from.
#: Unset or empty, the adapter runs the Codex it ships as an npm dependency.
CODEX_PATH_ENV = "CODEX_PATH"

#: On-disk name of the host's own Codex CLI.
CODEX_CLI_BIN = "codex"

#: The npm package codex-acp depends on for the Codex it bundles. Its manifest's
#: ``version`` is the floor a host ``codex`` has to meet to be handed over.
CODEX_NPM_PKG = "@openai/codex"

#: How many directories above the adapter's entry script the bundled-Codex lookup
#: climbs. Node resolves a dependency from the nearest ``node_modules`` upwards; a
#: nested copy sits two levels above ``dist/index.js`` and a hoisted one at most a
#: few more, so the bound only stops a walk that has left the install.
_BUNDLED_CODEX_MAX_LEVELS = 8

#: Largest ``package.json`` the lookup will parse, the same bound the claude
#: adapter's manifest reader applies.
_MANIFEST_MAX_BYTES = 1 << 20

#: The ``--version`` child is bounded: a binary that hangs on the flag leaves the
#: adapter on its bundled Codex rather than holding the spawn open.
_HOST_CODEX_VERSION_TIMEOUT_S = 10.0

#: Characters a resolved path must not carry on Windows, where codex-acp starts
#: ``"<CODEX_PATH>" app-server`` through ``cmd.exe``: ``%`` expands a variable even
#: inside the quotes, and ``"`` ends them.
_WINDOWS_SHELL_UNSAFE = frozenset('%"')

#: The Windows variables a process needs to start at all, carried into the
#: ``--version`` child on Windows only. A child without ``SystemRoot`` cannot
#: resolve side-by-side assemblies or initialize Winsock, and an npm-installed
#: ``codex.cmd`` needs ``ComSpec`` and ``PATHEXT`` to reach ``node.exe``; the rest
#: are the temp and profile locations a starting process reads. Location hints,
#: not secrets. Matched with :func:`platform_compat.env_key_allowed`, so the
#: upper-cased keys ``os.environ`` yields on Windows are found.
_WINDOWS_STARTUP_ENV_KEYS = frozenset(
    {"COMSPEC", "PATHEXT", "SystemRoot", "TEMP", "TMP", "USERPROFILE", "WINDIR"}
)


def _resolve_host_codex() -> str | None:
    """The host's own ``codex`` CLI, for ``CODEX_PATH``, or ``None``.

    The ``codex`` the operator installed is the one their ``codex login``, their
    ``~/.codex/config.toml`` and their model provider were set up for. The copy
    codex-acp bundles is upstream's build at whatever version the adapter pinned: a
    provider reached through a vendor build (Bedrock through an internal
    distribution, for one) is unknown to it, and ``session/new`` then fails with
    ``-32000 Authentication required`` and advertises no models. So the host's
    binary is a candidate, and :func:`_host_codex_decision` hands it over only
    when it is at least as new as the Codex the adapter bundles.

    Resolution order, the same ladder ``claude.py`` walks for its CLI:
      1. ``mise which codex`` (respects MISE_DATA_DIR and all mise config).
      2. Augmented PATH (``env.augmented_path`` -- mise/nvm/fnm/volta shims and the
         npm global bin), so a non-login launchd/systemd gateway still finds it.

    Blocking (one bounded ``mise`` subprocess and a PATH walk); callers run it off
    the loop.
    """
    # Read through the client at call time: its unit tests rebind
    # ``kiro_crew.acp.client.subprocess_mod`` under ``_mise_which``.
    from kiro_crew.acp.client import _mise_which, augmented_path

    resolved = _mise_which(CODEX_CLI_BIN)
    if not resolved:
        search_path = augmented_path(os.environ.get("PATH", ""))
        # Casing-normalize (Windows): see ``acp.launch._normalize_exe_casing``.
        resolved = launch_mod._normalize_exe_casing(shutil.which(CODEX_CLI_BIN, path=search_path))
    if not resolved:
        return None
    if platform_compat.IS_WINDOWS and _WINDOWS_SHELL_UNSAFE.intersection(resolved):
        logger.warning(
            "%s at %r contains a character cmd.exe would expand, so codex-acp is "
            "left on its bundled Codex. Set %s to choose a binary.",
            CODEX_CLI_BIN,
            resolved,
            CODEX_PATH_ENV,
        )
        return None
    return resolved


def _parse_codex_version(text: str) -> tuple[int, int, int] | None:
    """The first ``X.Y.Z`` triple in *text*, or ``None`` when there is none.

    Upstream prints ``codex-cli 0.147.0`` and a vendor build ``0.165.0.810
    (stable)``; both carry the triple this compares. A whitespace token counts
    when its first three dot-separated parts are all ASCII digits.
    """
    for token in (text or "").split():
        parts = token.split(".")
        if len(parts) >= 3 and all(p.isascii() and p.isdigit() for p in parts[:3]):
            return int(parts[0]), int(parts[1]), int(parts[2])
    return None


def _bundled_codex_version(argv: list[str]) -> tuple[int, int, int] | None:
    """The version of the :data:`CODEX_NPM_PKG` the adapter at *argv* loads, or ``None``.

    The lookup follows Node's own order from the adapter's entry script (the last
    element of *argv*, through its realpath): each ancestor directory's
    ``node_modules/@openai/codex``, nearest first, skipping a directory that is
    itself a ``node_modules`` the way Node does. That finds a copy nested inside
    the adapter's package and a hoisted one beside it alike. Only a manifest
    named :data:`CODEX_NPM_PKG` counts, and the climb and the file size are both
    bounded. Blocking (small file reads); callers run it off the loop.
    """
    if not argv:
        return None
    try:
        start = Path(os.path.realpath(argv[-1])).parent
    except (OSError, ValueError):
        return None
    for directory in [start, *start.parents][:_BUNDLED_CODEX_MAX_LEVELS]:
        if directory.name == "node_modules":
            continue
        manifest = directory / "node_modules" / CODEX_NPM_PKG / "package.json"
        try:
            if manifest.stat().st_size > _MANIFEST_MAX_BYTES:
                continue
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError, RecursionError):
            continue
        if not isinstance(data, dict) or data.get("name") != CODEX_NPM_PKG:
            continue
        version = data.get("version")
        return _parse_codex_version(version) if isinstance(version, str) else None
    return None


def _windows_startup_env(environ: Mapping[str, str]) -> dict[str, str]:
    """The :data:`_WINDOWS_STARTUP_ENV_KEYS` entries of *environ*; empty off Windows. Pure."""
    if not platform_compat.IS_WINDOWS:
        return {}
    return {
        key: value
        for key, value in environ.items()
        if platform_compat.env_key_allowed(key, _WINDOWS_STARTUP_ENV_KEYS)
    }


def _codex_version_child(
    argv: list[str],
    *,
    search_path: str,
    home: str,
    cwd: str | None,
    startup_env: Mapping[str, str] | None = None,
) -> tuple[int, int, int] | None:
    """Run the already-wrapped ``codex --version`` *argv* and parse what it prints.

    Blocking; callers run it off the loop. The child's environment is ``PATH``
    and ``HOME``, plus *startup_env*: on Windows, the start-up variables
    :func:`_windows_startup_env` selects, and nothing on POSIX. So nothing else of
    the gateway's own reaches a binary resolved from directories an agent may
    influence. Any failure -- the child would not start, timed out, exited
    non-zero, or printed no triple -- answers ``None``. A timeout kills the
    probe's whole process group and reaps the child, so a host binary that hangs
    on ``--version`` leaves no running descendant behind.
    """
    env = dict(startup_env or {})
    env["PATH"] = search_path
    if home:
        env["HOME"] = home
    try:
        proc = subprocess_mod.Popen(
            argv,
            cwd=cwd or None,
            env=env,
            stdin=subprocess_mod.DEVNULL,
            stdout=subprocess_mod.PIPE,
            stderr=subprocess_mod.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=platform_compat.IS_POSIX,
            creationflags=(
                platform_compat.CREATE_NEW_PROCESS_GROUP if platform_compat.IS_WINDOWS else 0
            ),
        )
    except (OSError, ValueError, subprocess_mod.SubprocessError):
        return None
    try:
        stdout, _ = proc.communicate(timeout=_HOST_CODEX_VERSION_TIMEOUT_S)
    except (OSError, ValueError, subprocess_mod.SubprocessError):
        return None
    finally:
        if proc.returncode is None:
            # Signal before polling or waiting: the unreaped child still owns its pid.
            platform_compat.kill_popen_tree(proc)
            with contextlib.suppress(OSError, ValueError, subprocess_mod.SubprocessError):
                proc.communicate(timeout=platform_compat.REAP_TIMEOUT_SECS)
            # A pipe failure must not prevent collection of the direct child.
            with contextlib.suppress(OSError, ValueError, subprocess_mod.SubprocessError):
                proc.wait(timeout=platform_compat.REAP_TIMEOUT_SECS)
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                with contextlib.suppress(OSError, ValueError):
                    stream.close()
    if proc.returncode != 0:
        return None
    return _parse_codex_version(stdout)


async def _host_codex_version(
    host_codex: str,
    *,
    ctx: SpawnContext,
    hidden: tuple[str, ...],
    expose: tuple[str, ...],
) -> tuple[int, int, int] | None:
    """What ``<host_codex> --version`` reports, run inside the session's sandbox.

    The host binary comes from PATH directories an agent may write to, so the
    gateway never executes it unconfined: the child is wrapped in the same
    sandbox tier and credential mask the adapter itself gets, as the opencode
    harness does for its own version child, and the launcher artifact is
    reclaimed once the child has exited. Its environment is the augmented
    ``PATH`` and ``HOME`` from ``ctx.environ``, plus on Windows the start-up
    variables :func:`_windows_startup_env` selects from it.
    """
    # circular import: kiro_crew.acp.client imports the harness registry.
    from kiro_crew.acp.client import augmented_path

    search_path = augmented_path(ctx.environ.get("PATH", ""))
    home = ctx.environ.get("HOME") or str(ctx.home)
    startup_env = _windows_startup_env(ctx.environ)
    cwd = str(ctx.work_dir) if ctx.work_dir else None
    version_argv, cleanup = await wrap_argv_async(
        [host_codex, "--version"],
        mode=ctx.sandbox_mode,
        strip_python_env=True,
        extra_hidden_dirs=hidden,
        extra_expose_files=expose,
        _prepare=wrap_argv,
    )
    try:
        return await asyncio.to_thread(
            _codex_version_child,
            version_argv,
            search_path=search_path,
            home=home,
            cwd=cwd,
            startup_env=startup_env,
        )
    finally:
        if cleanup:
            await asyncio.to_thread(launch_mod._unlink_readback_launcher, cleanup)


def _format_version(version: tuple[int, int, int]) -> str:
    return ".".join(str(part) for part in version)


def _host_codex_decision(
    host_codex: str | None,
    host_version: tuple[int, int, int] | None,
    bundled_version: tuple[int, int, int] | None,
) -> tuple[str | None, str]:
    """``(CODEX_PATH value or None, the reason)`` for one spawn. Pure.

    The host ``codex`` is handed over only when both versions are known and the
    host's is at least the bundled one: the adapter is released against its own
    bundled Codex, so an older host binary is one it was never tested with, and
    an unreadable version cannot be shown to meet the floor. Every other answer
    leaves the adapter on the Codex it bundles.
    """
    if bundled_version is None:
        return None, (
            f"the version of the bundled {CODEX_NPM_PKG} is unreadable, so codex-acp "
            "runs the Codex it bundles"
        )
    bundled = _format_version(bundled_version)
    if not host_codex:
        return (
            None,
            f"no host {CODEX_CLI_BIN} found; codex-acp runs the Codex it bundles ({bundled})",
        )
    if host_version is None:
        return None, (
            f"the host {CODEX_CLI_BIN} at {host_codex} reported no readable version; "
            f"codex-acp runs the Codex it bundles ({bundled})"
        )
    host = _format_version(host_version)
    if host_version < bundled_version:
        return None, (
            f"the host {CODEX_CLI_BIN} at {host_codex} is {host}, older than the bundled "
            f"{bundled}; codex-acp runs the Codex it bundles"
        )
    return host_codex, (
        f"codex-acp runs app-server from the host's {CODEX_CLI_BIN} at {host_codex} "
        f"({host}, at least the bundled {bundled})"
    )


def _sandbox_wrapper_generations(sandbox_mode: str) -> int:
    """Crew-owned processes between the spawned pid and the harness's adapter.

    ``1`` for a backend that ``fork()``s and stays resident as the parent, ``0``
    for one that execs into the target or does not wrap at all. Only a bounded RSS
    scope reads this: an unbounded measurement sums the whole subtree, where an
    extra generation at the top changes nothing.

    Failure answers ``0``, which is the fail-safe direction here. Too small an
    offset stops the sum one generation short and can only UNDER-count, so the
    ceiling is reached later rather than a healthy process being recycled early.
    """
    try:
        return 1 if detect_backend(sandbox_mode) == "namespace" else 0
    except Exception:
        logger.debug("sandbox wrapper-generation probe failed", exc_info=True)
        return 0


async def resolve_spawn_masks(sandbox_mode: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """``(hidden_dirs, expose_files)`` for the spawn, or a refusal.

    Two halves of one decision, resolved together and BEFORE the process exists:

    * the credential mask Crew's gate denies an enforced adapter at the OS boundary,
      and
    * the read-only files that come back through the backend's own carve-out (the
      other half of the Bedrock trade -- ``.aws`` stays hidden and only
      ``.aws/config`` returns).

    Raises when this session would spawn the adapter with its mask dropped: several
    ``wrap_argv`` paths return without applying ``extra_hidden_dirs``, and an
    enforced adapter started unmasked has no compensating control at all, so the
    refusal belongs here rather than after the spawn.

    The first half touches the filesystem (a cold sandbox probe shells out, and the
    mask canonicalizes the home plus every env-override root), so it runs in one
    worker thread with a bounded wait -- a stalled mount would otherwise hold the
    spawn open until the startup watchdog. The second half is pure path projection
    over the mask just resolved, which is why that mask is HANDED IN rather than
    re-derived: re-deriving it would put the filesystem read back on the loop.

    Keyed on the ROUTING, never on codex's identity: the preflight re-checks
    ``acp_tool_gate.is_enforced`` itself, so this cannot mask a harness this core
    does not enforce, and a future ``SESSION_CONFIG`` harness gets the same
    treatment by declaring the same routing.
    """
    from kiro_crew.acp import client as client_mod

    hidden = await client_mod._run_preflight_bounded(
        client_mod._sandbox_preflight, ACP_BACKEND_CODEX, sandbox_mode
    )
    expose = acp_tool_gate.adapter_expose_files(ACP_BACKEND_CODEX, hidden)
    return hidden, expose


class CodexHarness(MembershipHarness):
    """The codex-acp host."""

    #: ``CODEX_CONFIG`` disable-override built for the most recent background-runtime
    #: spawn, or empty string when no override was computed.  Consumed by
    #: :meth:`apply_spawn_env` which runs on the spawn worker after the full env
    #: (including ``agent.env``) is assembled.
    _bg_codex_config: str = ""

    backend = ACP_BACKEND_CODEX

    #: Core-only RSS ceiling, in MB, measured over ``rss_depth`` below.
    #:
    #: 1024 against a measured core of 224 MB idle and 522 MB at eight sessions.
    #: Not comparable to the runtime's own 500: that one counts the whole subtree,
    #: this one counts two processes, and the module docstring has the numbers.
    CORE_RSS_CEILING_MB = 1024.0

    #: Generations below the adapter that the ceiling above is measured over. 1 is
    #: the adapter plus ``codex app-server``; the per-session MCP fleet ``codex``
    #: starts from its own config sits one generation further down and is excluded.
    CORE_RSS_DEPTH = 1

    #: The host ``codex`` :meth:`resolve_spawn` decided to hand this spawn as
    #: ``CODEX_PATH``, or ``None`` for the adapter's bundled Codex. Set by the
    #: plan and read by :meth:`apply_spawn_env` of the same launch.
    _host_codex: str | None = None

    # ── Seam 1: spawn ──

    async def resolve_spawn(self, ctx: SpawnContext) -> SpawnPlan:
        """The resolved entry script, plus the mask the adapter is confined by.

        codex-acp takes no argv of its own: any invocation enters stdio-server mode
        and blocks on stdin. So there is no ``--agent`` (the adapter reads no
        ``~/.kiro/agents/<name>.json``; everything Crew mounts on the session
        arrives in the ``session/new`` array instead) and no ``--model`` (the model is chosen
        per session over ``session/set_config_option``, so pinning one at process
        start would apply it to every session on this process).

        The mask is resolved HERE, in the same thread-hop budget as the argv search,
        and a refusal aborts the spawn: this host's privileged tools do not ask by
        construction, so an empty mask is a missing control rather than a
        simplification.

        ``host_auth`` stays False. codex holds its own credential and raises nothing
        at Crew, so a process started expecting to answer a callback would be
        waiting for a frame that never arrives.

        Which ``codex`` the adapter runs ``app-server`` from is decided here too,
        after the mask, because deciding it runs the host binary and that child
        needs the mask: with no ``CODEX_PATH`` in the environment, the host's
        ``codex`` is chosen when its ``--version`` (run in the session's sandbox)
        is at least the version of the Codex the adapter bundles, read from that
        package's manifest. Anything else -- no host ``codex``, either version
        unreadable, or an older host -- leaves the adapter on its bundled Codex,
        with one INFO line naming why. An operator's ``CODEX_PATH``, empty
        included, skips the whole decision.

        The binary resolution is delegated to :mod:`kiro_crew.acp.client` rather than
        copied. Its order is an operator-facing contract -- explicit
        ``CODEX_ACP_BIN``, then a project-local ``node_modules`` copy, then mise,
        then PATH -- and two copies of it would drift into two different answers to
        "why did it pick that one?".
        """
        # circular import: kiro_crew.acp.client imports the harness registry this
        # module is part of, so both of these resolve at call time rather than at
        # module scope.
        from kiro_crew.acp import client as client_mod
        from kiro_crew.acp.session_handle import AcpRuntimeError

        argv, search_path = await asyncio.to_thread(client_mod._resolve_codex_acp_bin)
        if not argv:
            # The wording comes from the client too, not just the resolution: it is
            # the operator's only instruction for fixing the install, and two
            # authorings of it drift. The search path is threaded through so a
            # "searched ..." line can never name a directory the search skipped.
            raise AcpRuntimeError(client_mod.codex_acp_not_found_message(search_path))
        hidden, expose = await resolve_spawn_masks(ctx.sandbox_mode)
        self._host_codex = await self._decide_host_codex(
            ctx, list(argv), hidden=hidden, expose=expose
        )
        wrapper_generations = await asyncio.to_thread(
            _sandbox_wrapper_generations, ctx.sandbox_mode
        )
        # Every ``codex app-server`` opens the same SQLite files by default and
        # they lock each other out: Codex Desktop plus a Crew runtime fail new
        # sessions with ``database is locked``. So each runtime gets its own
        # CODEX_SQLITE_HOME (a per-process directory no other process is handed).
        # Only the databases move: config, auth and the thread rollouts stay in CODEX_HOME,
        # and a thread resumes from its rollout (measured on codex 0.159), so
        # spawn_continue still works across runtimes. Seed the private index
        # from a completed SQLite snapshot so a large rollout history does not
        # need rebuilding at every start. Each process still owns its database.
        # An operator-supplied location reaches the child without preparation.
        #
        # Background sessions (kirocrew-lite) additionally disable every
        # configured MCP server via CODEX_CONFIG.  codex starts a per-session
        # fleet (~44 processes) from config.toml for each session/new; session/close
        # evicts the session but never terminates those children.  Injecting
        # CODEX_CONFIG = {"mcp_servers": {<name>: {"enabled": false, ...}}} makes
        # codex skip initializing every server (McpServerConfig.enabled, documented
        # in codex-rs/config/src/mcp_types.rs:234).  CODEX_HOME stays shared and
        # untouched; only the child's environment gains the override.
        sqlite_home_set = bool(ctx.environ.get(_SQLITE_HOME_ENV))
        from kiro_crew.session import BACKGROUND_AGENT as _bg_agent  # circular at module scope

        is_background = ctx.agent == _bg_agent

        # For background sessions: read config.toml once, off the event loop, and
        # build the CODEX_CONFIG override that disables every configured MCP
        # server.  Stored in self._bg_codex_config and consumed by apply_spawn_env
        # after the full env (including agent.env) is assembled, so it can see
        # whether CODEX_CONFIG is already set and leave an existing value alone.
        #
        # Names are read once, at spawn, while codex re-reads config.toml on
        # every thread/start. Each entry therefore carries its own transport
        # placeholder (see _DISABLED_STDIO_COMMAND), so a server removed or
        # renamed later, or one read from a CODEX_HOME that agent.env then
        # replaces, is an orphan that still parses instead of failing every
        # background session/new.
        #
        # Known residuals: a server renamed after spawn starts its fleet again
        # under the new name until the runtime recycles, and one that switches
        # between command and url under the same name conflicts with the
        # placeholder until then. Project-level .codex/config.toml overrides
        # are not covered. A session/new that carries mcpServers replaces the
        # whole mcp_servers table in codex-acp's createSessionConfig, dropping
        # this override for any session that mounts servers that way.
        self._bg_codex_config = ""
        if is_background:
            _codex_home = Path(ctx.environ.get(_CODEX_HOME_ENV) or str(ctx.home / ".codex"))
            _overrides = await asyncio.to_thread(
                _read_codex_mcp_disable_overrides, _codex_home / "config.toml"
            )
            if _overrides:
                self._bg_codex_config = json.dumps(
                    {"mcp_servers": _overrides},
                    separators=(",", ":"),
                )

        return SpawnPlan(
            argv=list(argv),
            rss_depth=self.CORE_RSS_DEPTH + wrapper_generations,
            extra_hidden_dirs=hidden,
            extra_expose_files=expose,
            private_state_env=None if sqlite_home_set else _SQLITE_HOME_ENV,
            private_state_seed=seed_private_state,
        )

    async def _decide_host_codex(
        self,
        ctx: SpawnContext,
        argv: list[str],
        *,
        hidden: tuple[str, ...],
        expose: tuple[str, ...],
    ) -> str | None:
        """The ``CODEX_PATH`` this spawn gets from Crew, or ``None``.

        ``None`` without a word when the operator set ``CODEX_PATH`` (their value
        reaches the child unchanged). Otherwise the cheap reads go first -- the
        bundled manifest, then the host lookup -- so a spawn whose floor cannot be
        read never starts the host binary at all.
        """
        if CODEX_PATH_ENV in ctx.environ:
            return None
        bundled_version = await asyncio.to_thread(_bundled_codex_version, argv)
        host_codex = None
        host_version = None
        if bundled_version is not None:
            host_codex = await asyncio.to_thread(_resolve_host_codex)
            if host_codex:
                host_version = await _host_codex_version(
                    host_codex, ctx=ctx, hidden=hidden, expose=expose
                )
        chosen, reason = _host_codex_decision(host_codex, host_version, bundled_version)
        logger.info("%s", reason)
        return chosen

    def apply_spawn_env(
        self,
        env: dict[str, str],
        *,
        spawned_binary: str | None = None,
        cli_owned_auth: bool = False,
    ) -> None:
        """Take kiro-cli's API key OUT, and point the adapter at the host's ``codex``.

        A foreign adapter must never receive kiro-cli's key, and removing it is the
        positive action here rather than an omission.

        ``CODEX_PATH`` names the ``codex`` binary the adapter spawns ``app-server``
        from. Crew fills it with the host ``codex`` :meth:`resolve_spawn` chose for
        this launch -- one at least as new as the Codex the adapter bundles -- and
        only when the name is ABSENT, so an operator's value always wins, an empty
        one included, which is how an operator asks for the adapter's bundled Codex
        on a host that has its own. With no host ``codex`` chosen the name stays
        unset and the adapter runs the copy it bundles. Nothing is resolved or run
        here: the decision and its version child belong to the plan.

        Two more variables this host needs are left exactly as the operator set
        them, and reaching the child through the ambient copy of the environment is
        how they get there. Each is READ by something outside Crew, so a Crew-side
        default would be a value Crew invented for a consumer it does not own.

        ``CODEX_HOME`` moves the whole ``codex`` configuration directory. It is
        also a MEMORY lever, not only an isolation one: the per-session MCP fleet
        is started from the config found there, which is the difference between the
        two measurement runs in this module's docstring.

        ``AWS_CONFIG_FILE`` (with ``AWS_SHARED_CREDENTIALS_FILE``) is the one whose
        DIRECTORY matters as much as its value. A wrapper that writes its own
        config does so atomically -- a temporary file in that same directory, then
        a rename -- so a read-only directory fails the write, and the process dies
        before ACP is reached. Nothing here fences that directory, and nothing here
        should start: ``resolve_spawn_masks`` hides credential homes and re-exposes
        only ``.aws/config``, as a read-only COPY, so the default ``~/.aws`` is not
        a directory this child can write in.

        THE REMEDY IS THE OPERATOR'S, and it is one step: point ``AWS_CONFIG_FILE``
        and ``AWS_SHARED_CREDENTIALS_FILE`` at a writable directory OUTSIDE the
        hidden tree -- any path the mask does not cover -- which is what the probe
        that found this did. Making ``~/.aws`` itself writable is the alternative
        and is refused: it means moving that path off the read-only copy primitive
        onto one that un-hides the real credential tree, which weakens the mask for
        every session to save one step in a wrapper's setup.
        """
        from kiro_crew.config import loader as loader_mod

        loader_mod.strip_kiro_cli_api_key(env)
        if CODEX_PATH_ENV not in env and self._host_codex:
            env[CODEX_PATH_ENV] = self._host_codex
        # codex-acp otherwise drops session entries whose names occur in global
        # config. That keeps an unbound global dashboard server in place of the
        # gateway's verified mount, on both session/new and session/load.
        env["DISABLE_MCP_CONFIG_FILTERING"] = "true"
        # Opt the adapter into its host-sandbox mode. resolve_spawn_masks refuses
        # every unwrapped tier, so this child always runs inside Crew's wrap. An
        # adapter that does not know the flag ignores it and keeps read-only.
        env["CODEX_ACP_EXTERNAL_SANDBOX"] = "true"
        # Inject CODEX_CONFIG to disable the per-session MCP fleet for background
        # sessions.  apply_spawn_env runs after agent.env is merged (launch.py:1014),
        # so checking env.get here correctly sees operator/agent.env values and will
        # not overwrite them.  A skipped injection is logged: without it the
        # background runtime starts the full MCP fleet per session again.
        if self._bg_codex_config:
            if env.get("CODEX_CONFIG"):
                logger.warning(
                    "codex background runtime: CODEX_CONFIG is already set, so the "
                    "MCP-server disable override was not applied; configured MCP "
                    "servers will start for every background session"
                )
            else:
                env["CODEX_CONFIG"] = self._bg_codex_config

    @property
    def verifies_agent_activation(self) -> bool:
        """No -- there is no spawn flag whose effect could go unconfirmed.

        codex takes no ``--agent``, so nothing was selected at spawn for a later
        check to confirm.
        """
        return False

    # ── Seam 2: initialize ──

    @property
    def protocol_version(self) -> Any:
        return PROTOCOL_VERSION_CODEX

    @property
    def client_capabilities(self) -> dict[str, Any]:
        """The shared declaration plus ``subagents`` and ``session.compaction``.

        ``subagents`` is what makes codex-acp give each native subagent its own
        ACP session, announced on the parent and streamed under the child's id.
        The constant's comment names the handlers it promises.
        """
        return CODEX_CLIENT_CAPABILITIES

    # ── Seam 3: session/new and session/load extras ──

    async def session_extras(
        self,
        agent: str,
        *,
        work_dir: str | Path | None,
        mcp_gateway_overlay: Any = None,
        member_dispatch: bool = False,
        crew_panel: bool = False,
        session_key: str = "",
        worker_stub_names: frozenset[str] | None = None,
        engine_features: frozenset[str] = frozenset(),
        session_cwd: str | Path | None = None,
        ssh_auth_sock_forwarded: bool = False,
        governance_app: str | None = None,
    ) -> SessionExtras:
        """Empty. codex has no custom-agent channel to register anything on.

        ``custom_agents`` registers agent definitions on a kiro-family host that has
        no ``--agent`` flag. codex has no such flag either, and no such channel --
        what it needs per session is the ``mcpServers`` array, which
        :meth:`session_mcp_servers` narrows.
        """
        return SessionExtras()

    def session_mcp_servers(
        self,
        requested: list[dict[str, Any]],
        *,
        agent_capabilities: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Drop the elements whose transport THIS session's adapter did not advertise.

        Narrowed at session start rather than when the array was built, for a timing
        reason: the array is assembled on the spawn path, before the adapter process
        exists, while the transports it accepts are unknown until ``initialize``
        answers. Reading them here uses what this session was told instead of what
        some adapter version once said, and stays a pure in-memory pass.

        An UNKNOWN handshake passes the array through untouched -- an absent
        ``agentCapabilities``, an absent ``mcpCapabilities``, or one that is not a
        mapping. Narrowing to nothing there would strip every tool from every
        session on a host that simply did not say.

        What an unadvertised element costs depends on its shape, and both answers
        are measured (``test_real_codex_acp_accepts_the_crew_stdio_element``). An
        element that parses as its transport -- an sse element carrying the
        schema-required ``headers`` array, which is the shape Crew's translation
        emits -- is named and refused with ``-32600``, and the refusal fails the
        WHOLE ``session/new``: every server in the array lost over one entry. An
        element that does not parse as its transport (the same sse without
        ``headers``) falls to the untagged variant and is accepted with no error at
        all, the server silently never wired. So this narrowing is load-bearing
        from both directions: on Crew's own array it is what keeps one unsupported
        entry from costing the session, and on a malformed one it is the only check
        at all, since nothing downstream can detect a server that was never wired.
        """
        advertised = agent_capabilities.get("mcpCapabilities")
        if not isinstance(advertised, dict) or not advertised:
            return requested
        return drop_unadvertised_transports(list(requested), dict(advertised))

    @property
    def wants_session_file_on_load(self) -> bool:
        """No -- the adapter locates the session from its id.

        It keeps its own session records, so there is no Crew-side transcript to
        name, and sending a path it cannot read would advertise a file that is not
        there.
        """
        return False

    # ── Seam 4: inbound requests the host answers ──

    @property
    def host_answered_methods(self) -> tuple[str, ...]:
        """None. codex asks Crew for nothing at the connection level.

        Empty rather than absent, so a reader can tell "this host needs no callback"
        from "nobody has checked". KAS is the contrast: it raises
        ``_kiro/auth/getAccessToken`` when Crew owns the credential.
        """
        return ()

    async def answer_request(self, method: str) -> dict[str, Any]:
        """Never called: :attr:`host_answered_methods` is empty.

        Raises rather than returning ``{}`` -- an empty result would let a frame
        this harness never claimed be answered as if it had.
        """
        raise NotImplementedError(f"codex harness answers no inbound request, including {method!r}")

    # ── Seam 5: notification aliases ──

    @property
    def notification_aliases(self) -> NotificationAliases:
        """Plain ACP, with no forked spellings.

        ``session/update`` only: codex sends no ``_kiro.dev`` alias, sends no
        ownerless subagent roster (each child is announced by a ``session/update``
        on its parent's session instead), and stages no MCP-init frame. Declared explicitly rather
        than inherited from the kiro family, whose ``_kiro.dev/*`` vocabulary exists
        because KAS is reached THROUGH kiro-cli's relay -- codex is not.
        """
        return NotificationAliases(session_update=(METHOD_SESSION_UPDATE,))

    # ── Seam 6: teardown ──

    @property
    def teardown(self) -> TeardownPolicy:
        """The standard ACP ``session/close``, awaited, and it evicts.

        kiro-cli's ``_kiro.dev/session/terminate`` and KAS's ``_kiro/session/delete``
        are extensions codex-acp answers with method-not-found. Measured on
        codex-acp 1.11.0 (``session/set_config_option`` as the liveness oracle):
        after close the oracle answers ``-32603``, a second close returns ``{}``
        again (double-terminate is safe), and a fresh ``session/new`` still works.
        ``session/cancel`` only interrupts a turn and leaves the session resident,
        so a harness tearing down with it does not belong in
        ``ACP_BACKENDS_SESSION_EVICTION``.

        Evict, not delete: the thread record survives under ``CODEX_HOME``, and
        ``session/load`` on the closed id replays the conversation, also from a
        restarted adapter. ``ACP_BACKENDS_SESSION_SHARING`` membership rests on
        this. ``session/delete`` would ARCHIVE the thread and make later loads fail,
        and the archive still keeps the rollout on disk under ``archived_sessions/``.
        So Crew deletes a disposable session's rollout files itself, after the
        teardown: ``delete_disposable_record`` names ``agent_sdk.codex_transcripts``,
        which reads ``CODEX_HOME`` from the runtime's ``extra_env`` over the gateway's.

        Must be a REQUEST (``notification=False``): sent as a notification the
        adapter ignores it silently and every session leaks. Bounded by
        ``_TERMINATE_TIMEOUT``. The gated test
        ``test_codex_session_mcp.py::test_real_codex_acp_session_close_evicts``
        repeats the measurement against each installed adapter.
        """
        return TeardownPolicy(
            method=METHOD_SESSION_CLOSE,
            notification=False,
            delete_disposable_record=delete_codex_session_files,
        )

    # ── Seam 9: recycle ──

    def reclaim_policy(self, *, max_age_secs: float, max_rss_mb: float) -> ReclaimPolicy:
        """Core-only RSS, against this host's own ceiling. Age passes through.

        The measurement and the numbers are in this module's docstring. In short:
        the adapter's whole subtree is dominated by a per-session MCP fleet
        ``codex`` starts from the operator's own config, so a subtree ceiling
        recycles a healthy process on its first prompt, and no subtree number Crew
        could pick fixes that -- the per-session term is not Crew's to know.

        The operator's ``max_rss_mb`` is not carried over, and that is the one place
        this seam's usual rule does not hold. Narrowing a ceiling keeps its unit;
        changing ``rss_depth`` changes it, so the incoming number describes a
        quantity this policy does not measure. Passing it through would apply a
        subtree budget to two processes.

        ``max_age_secs`` IS passed through, because nothing about age changes with
        the scope.
        """
        if max_rss_mb != self.CORE_RSS_CEILING_MB:
            # The substitution is the one place an operator's configured number does
            # not reach a host, so it is stated at INFO rather than left to be read
            # off this docstring. An operator who raised the runtime ceiling and saw
            # no change on codex has the reason in their own log, with both numbers
            # and the scope that makes them different units.
            logger.info(
                "codex reclaim: RSS ceiling %.0f MB over the core scope (depth %d), "
                "not the configured %.0f MB, which counts the whole subtree "
                "(different unit; see CodexHarness.reclaim_policy)",
                self.CORE_RSS_CEILING_MB,
                self.CORE_RSS_DEPTH,
                max_rss_mb,
            )
        return ReclaimPolicy(
            max_age_secs=max_age_secs,
            max_rss_mb=self.CORE_RSS_CEILING_MB,
        )
