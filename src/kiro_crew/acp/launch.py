"""Starting an ACP host process: the one launch tail, and what hosts share to get there.

Both drivers start their child here. ``AcpRuntime`` (one process, many sessions) and
``AcpClient`` (one process per session) each resolve a :class:`SpawnPlan` from their
host's adapter -- a :class:`~kiro_crew.acp.harness.base.HarnessAdapter` on the
runtime, a :class:`~kiro_crew.acp.harness.base.ProcessAdapter` or kiro-cli's own arm
on the client -- and hand it to :func:`launch`, which owns everything from there to
a created, still-suspended process:

1. the pod bundle swap and the sandbox-delegation verdict;
2. the per-process scratch window, joined to the session tree's when one exists;
3. the OS sandbox wrap with the host's credential mask, and the cgroup scope;
4. the child's environment, assembled in one fixed order around each driver's own
   steps (session identity on the client, the spawn instance on the runtime);
5. the macOS workspace binding and the suspended spawn itself.

What a driver does AFTER the process exists -- resuming it, recording its identity,
tracking its pids, draining its stderr -- stays with that driver, because the two
reap a failed start differently on purpose.

The OS-facing collaborators arrive as :class:`LaunchTools`, which each driver builds
from its OWN module's bindings at the moment it launches. That is what keeps a test
that rebinds ``kiro_crew.acp.client.wrap_argv`` (or the runtime's) reaching the
launch it drives, and it is the seam a test of this module fills with fakes.

The rest of this module is what several per-process hosts share before the tail:
the Node-adapter resolution ladder (claude-agent-acp, codex-acp, pi-acp), the
self-served binary ladder (opencode, goose, DeepSeek Harness), the per-gateway
resolution caches' generation fence, the enforced-host credential-mask preflight,
and the plumbing of a host's read-back child. A host's own knowledge lives in its
adapter module under :mod:`kiro_crew.acp.harness`; nothing here names one host.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any, Awaitable, Callable, Iterator, Mapping, Protocol, Sequence

from kiro_crew import acp_tool_gate, platform_compat
from kiro_crew.acp.transport_errors import AcpError, AcpToolGateUnroutable
from kiro_crew.acp.transport_framing import _STDOUT_BUFFER_LIMIT
from kiro_crew.agent_sdk.backends import launch_for
from kiro_crew.constants import KIROCREW_SPAWNED_ENV, KIROCREW_SPAWNED_VALUE
from kiro_crew.env import describe_search_path, mise_data_dir
from kiro_crew.sandbox import RLIMIT_PROFILE_SESSION_HOST

# The client's logger name, because the helpers below were the client's and every
# log line they emit has always been filed there. The launch tail itself logs under
# the DRIVER's logger, handed in with its tools, for the same reason.
logger = logging.getLogger("kiro_crew.acp.client")

__all__ = [
    "ACP_SDK_DEP_MARKER",
    "LaunchHost",
    "LaunchRequest",
    "LaunchTools",
    "LaunchedProcess",
    "bump_resolution_generation",
    "launch",
    "resolve_self_served_launch",
]

#: A direct runtime dependency of every Node ACP adapter (claude-agent-acp, codex-acp,
#: pi-acp). Its reachability is a cheap completeness check: a copy that cannot import
#: it would crash at import with ``ERR_MODULE_NOT_FOUND: @agentclientprotocol/sdk`` --
#: after the spawn -- so such a copy is rejected and the ladder moves to the next
#: candidate. "Reachable" means what it means to Node: present in some
#: ``node_modules`` on the walk UP from the entry script's REAL path
#: (:func:`_vendored_adapter_entry`). An ordinary ``npm install`` hoists the
#: dependency flat into the same root as the adapter; a ``file:`` / ``npm link``
#: install is a symlink whose dependencies sit under the link target's own
#: ``node_modules`` and hoists nothing, so a check pinned to the hoisted root alone
#: would reject every linked adapter as incomplete.
ACP_SDK_DEP_MARKER = Path("@agentclientprotocol") / "sdk"


# ── Executable resolution: the Node-adapter ladder ──


# Launchers that carry the adapter's entry script as their next argument.  A
# label taken from argv[0] alone would read "node" for every adapter resolved
# to a script rather than a native binary.
_ADAPTER_INTERPRETERS = frozenset({"node", "node.exe"})


def _is_adapter_package_entry(program: str, pkg_entry: Path) -> bool:
    """Whether *program* is *pkg_entry* sitting under some node_modules root."""
    parts = Path(program).parts
    wanted = pkg_entry.parts
    if len(parts) < len(wanted):
        return False
    return [p.casefold() for p in parts[-len(wanted) :]] == [p.casefold() for p in wanted]


def _named_by_override(program: str, override_env: str | None) -> bool:
    """Whether the operator's override is what supplied *program*.

    The resolution ladder takes the override as its first candidate verbatim, so
    an equality test against the resolved program is what separates a deliberate
    override from the adapter's own installed entry.
    """
    if not override_env:
        return False
    override = os.environ.get(override_env, "").strip()
    if not override:
        return False
    return os.path.normpath(os.path.expanduser(override)) == os.path.normpath(program)


def _adapter_spawn_label(
    argv: Sequence[str],
    seam: str,
    *,
    pkg_entry: Path | None = None,
    override_env: str | None = None,
) -> str:
    """Keep a stable seam label while identifying the resolved program.

    Both ACP seams resolve their binary through a documented environment
    override (``CLAUDE_AGENT_ACP_BIN``, ``CODEX_ACP_BIN``), and either may point
    at a dispatch shim or a vendored build that is not the seam's own adapter.
    The seam is useful to existing log parsers, while the resolved program proves
    which adapter command that seam actually launched.
    """
    if not argv:
        return seam
    program = argv[0]
    if Path(program).name.casefold() in _ADAPTER_INTERPRETERS:
        # A bare interpreter identifies no adapter at all.
        if len(argv) <= 1:
            return seam
        program = argv[1]
        # An adapter installed as a Node package resolves to its own
        # `dist/index.js`, whose basename names the packaging rather than the
        # adapter, so the seam alone is the useful identity there. That shortcut
        # is only honest for the package's OWN entry under its own scope: a
        # script the operator's override supplied, or any other `index.js`, is
        # the one record of which build actually launched, so its path stays.
        if (
            pkg_entry is not None
            and not _named_by_override(program, override_env)
            and _is_adapter_package_entry(program, pkg_entry)
        ):
            return seam
    return f"{seam} via {program}" if program else seam


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
    a home directory reached through a symlink (a home under a mounted volume).
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


#: Sentinel for "not yet resolved": every host's cached resolution starts here, and the
#: install probe compares a cache against this one object by identity.
_UNRESOLVED: object = object()


def _vendored_acp_roots(pkg_dir: Path | None = None) -> list[Path]:
    """Directories that may contain a project-local ``node_modules`` copy of a
    Node ACP adapter.

    Harness-neutral: the roots are plain ``node_modules`` directories, and each
    adapter resolver joins its own package path onto them, so this is shared by
    the claude and codex resolvers rather than duplicated per harness.

    A project-local install (``npm i @agentclientprotocol/<adapter>`` in the
    repo, or a copy bundled next to the installed package) lets the gateway run
    without a global npm install — useful in non-login launchd/systemd contexts
    with a minimal PATH.  Resolution still falls back to global / PATH installs
    in each ``_resolve_*_acp_bin``; these roots are just preferred.

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


def _resolve_node_adapter_argv(
    *,
    bin_name: str,
    override_env: str,
    vendored_entry: Callable[[], str | None],
) -> tuple[list[str] | None, str]:
    """Find a Node stdio adapter's entry and the PATH searched for it.

    ONE ladder for every adapter published as an npm package -- claude-agent-acp,
    codex-acp and pi-acp today -- so an operator debugging one is debugging all of
    them, and a harness added later is one call with three arguments rather than a
    fourth copy. The first item is argv suitable for subprocess use
    (``["node", "script.js"]`` or ``["/path/to/binary"]``), or ``None`` when nothing
    was found; the second is the PATH searched at the last rung. Node is resolved
    explicitly rather than left to a ``#!/usr/bin/env node`` shebang, which fails
    in non-interactive daemon contexts (mise shims need a cwd with ``.mise.toml``
    or a working global config).

    Resolution order:
      1. *override_env* (explicit override; need not be executable -- a
         non-executable script is auto-wrapped with node).
      2. *vendored_entry*: a project-local ``node_modules`` copy (from ``npm
         install`` in the repo, a ``file:`` / ``npm link`` install, or a copy
         bundled next to the package), accepted only when Node could import the
         adapter's dependency from the entry's real path -- no global install
         required, and no ESM import crash after the spawn. A copy that is
         skipped is logged, so the fall-through to a global copy is never silent.
      3. ``mise which <bin_name>`` (respects all mise config).
      4. Direct glob under mise installs (fallback if mise exec fails).
      5. Augmented PATH (includes mise shims, nvm, fnm, volta, npm -g).
    """
    # Read through the client at call time: its unit tests rebind
    # ``kiro_crew.acp.client.subprocess_mod`` under ``_mise_which``, and that rebind has
    # to reach every ladder that asks mise; a rebind of the client's ``augmented_path``
    # has to reach the directory list this ladder searches and reports.
    from kiro_crew.acp.client import _mise_which, augmented_path

    candidates: list[str] = []

    override = os.environ.get(override_env)
    if override and Path(override).is_file():
        candidates.append(override)

    # Project-local node_modules copy. Preferred over PATH-based resolution
    # because it needs no global install and works in non-login gateway
    # contexts (launchd/systemd) with a minimal PATH.
    vendored = vendored_entry()
    if vendored:
        candidates.append(vendored)

    # Preferred: ask mise directly -- respects MISE_DATA_DIR, global config,
    # and .mise.toml regardless of the user's installation layout.
    mise_resolved = _mise_which(bin_name)
    if mise_resolved:
        candidates.append(mise_resolved)

    # Fallback: search mise installs directory directly (handles case where
    # `mise which` fails due to missing global config in daemon context).
    mise_installs = _mise_node_installs_dir()
    if mise_installs.is_dir():
        for bin_path in sorted(mise_installs.glob("*/bin/" + bin_name), reverse=True):
            if bin_path.is_file():
                candidates.append(str(bin_path))
                break

    # Also search augmented PATH (includes mise shims) as fallback.
    # Covers nvm, fnm, volta, and plain `npm i -g` installations.
    search_path = augmented_path(os.environ.get("PATH", ""))
    on_path = shutil.which(bin_name, path=search_path)
    if on_path:
        candidates.append(on_path)

    for script in candidates:
        resolved = str(Path(script).resolve())
        node = _resolve_node_for_script(resolved)
        if node:
            return [node, resolved], search_path
        # Directly runnable (a real executable on POSIX; a .exe/.cmd/etc. on
        # Windows)? Run it as-is. A bare .js is NOT directly runnable on Windows
        # (is_executable_file excludes it), so it correctly falls through to be
        # wrapped with node below -- matching the POSIX no-x-bit behavior.
        # Casing-normalize (Windows): a `which`-resolved .EXE must reach a
        # launcher-style shim with its true on-disk name (see _normalize_exe_casing).
        if platform_compat.is_executable_file(script):
            return [_normalize_exe_casing(script) or script], search_path
        node_on_path = shutil.which("node", path=search_path)
        if node_on_path:
            return [node_on_path, resolved], search_path

    return None, search_path


def _node_module_search_dirs(start: Path) -> Iterator[Path]:
    """The ``node_modules`` directories Node searches for a bare import from *start*.

    Node's ``NODE_MODULES_PATHS``: every ancestor of *start* (itself included)
    contributes ``<ancestor>/node_modules``, except an ancestor that IS a
    ``node_modules`` directory, from the innermost outward to the filesystem root.
    *start* must already be a REAL path: Node resolves a module's symlinks before
    looking for that module's imports (``--preserve-symlinks`` is off by default),
    which is why a ``file:`` / ``npm link`` install finds its dependencies beside
    the link TARGET rather than at the hoisted root it is linked from.
    """
    for ancestor in (start, *start.parents):
        if ancestor.name == "node_modules":
            continue
        yield ancestor / "node_modules"


def _vendored_adapter_entry(
    pkg_entry: Path, dep_marker: Path, pkg_dir: Path | None = None
) -> str | None:
    """The first project-local copy of a Node ACP adapter that Node itself could run.

    ONE check for the three adapter resolvers (claude-agent-acp, codex-acp,
    pi-acp): each joins its own package entry and dependency marker onto the
    shared roots (:func:`_vendored_acp_roots`), so there is no per-harness copy of
    the completeness rule to drift. The helper is harness-neutral and adds nothing
    to the Kiro path (H13).

    A copy is accepted when its dependency marker is reachable the way Node
    resolves a bare import from the ENTRY'S REAL PATH -- some ``node_modules`` on
    the walk up from where the entry script really lives holds it. An ordinary
    ``npm install`` satisfies that at the hoisted root; a ``file:`` / ``npm link``
    install is a symlink that hoists nothing and satisfies it under the link
    target's own ``node_modules``, which a check pinned to the hoisted root alone
    cannot see. An entry whose dependency is reachable nowhere would die at ESM
    import -- after the spawn -- so it is refused, and the refusal is logged: a
    silent fall-through to a global copy on PATH is how a locally patched adapter
    runs as the unpatched global build with nothing to say so.
    """
    for root in _vendored_acp_roots(pkg_dir):
        entry = root / pkg_entry
        if not entry.is_file():
            continue
        real_entry = Path(os.path.realpath(entry))
        for node_modules in _node_module_search_dirs(real_entry.parent):
            if (node_modules / dep_marker).is_dir():
                return str(entry)
        logger.warning(
            "Skipping project-local ACP adapter %s: %s is not importable from its real "
            "location %s (no node_modules on the walk up from there holds it); the next "
            "candidate on the ladder that resolves, if any, is used instead. For a file: "
            "or npm link install, run npm install inside the linked checkout.",
            entry,
            dep_marker.as_posix(),
            real_entry.parent,
        )
    return None


# ── The per-gateway resolution caches' generation fence, and the self-served ladder ──


#: Resolved binary per self-served harness, keyed by backend id. ONE mapping rather
#: than one module global each: the resolution is the same three rungs for every
#: member, so a per-harness global would be three copies of one cache. Read and
#: written only through :func:`_resolve_self_served_bin` and the driver's
#: cached-negative seam, which is why an absent key means "not looked at yet" and a
#: ``(None, path)`` value means "looked, and it is not here".
_self_served_bin_caches: dict[str, tuple[str | None, str]] = {}


#: Per-backend resolution generation, bumped by every deliberate cache clear.
#:
#: The caches above are all written AFTER an ``await``: a site checks the sentinel,
#: offloads the resolve, and only then assigns. So a resolution that began before an
#: operator installed a component can complete after a re-check cleared the cache, and
#: its assignment would stamp that stale miss back over the cleared sentinel -- the
#: panel having already reported the harness ready, and the next spawn failing on the
#: revived miss.
#:
#: A resolution captures the generation before it awaits and publishes only if the
#: generation is still current. Keyed by BACKEND rather than by cache name because a
#: clear is per harness and pi keeps two caches under one id, so one bump has to fence
#: both.
_resolution_generation: dict[str, int] = {}


def _resolution_epoch(backend: str) -> int:
    """The generation a resolution should capture before it awaits."""
    return _resolution_generation.get(backend, 0)


def bump_resolution_generation(backend: str) -> None:
    """Invalidate every resolution currently in flight for *backend*.

    Called by ``agent_sdk.drivers.acp.forget_cached_resolution`` alongside the sentinel
    reset. The sentinel is what makes the NEXT spawn resolve; this is what stops an
    OLDER one from publishing over it.
    """
    _resolution_generation[backend] = _resolution_generation.get(backend, 0) + 1


def _resolve_self_served_bin(backend: str) -> tuple[str | None, str]:
    """Find *backend*'s own executable and the PATH searched for it.

    Three rungs, the plain-binary ladder: explicit override, then mise, then the
    augmented PATH. No ``node_modules`` rung and no node resolution, because every
    harness this serves is a binary that speaks ACP itself rather than a Node entry
    script -- which is exactly what membership in ``ACP_BACKEND_LAUNCH`` asserts.

    The binary name and the override variable come from that record, so a harness is
    resolved by its row rather than by a function of its own.

    Returns ``(None, search_path)`` when it is absent, so the caller reports what was
    searched rather than raising from inside the resolver.
    """
    # Read through the client at call time: its unit tests rebind
    # ``kiro_crew.acp.client.subprocess_mod`` under ``_mise_which``, and that rebind has
    # to reach every ladder that asks mise; a rebind of the client's ``augmented_path``
    # has to reach the directory list this ladder searches and reports.
    from kiro_crew.acp.client import _mise_which, augmented_path

    launch = launch_for(backend)
    search_path = augmented_path(os.environ.get("PATH", ""))

    override = os.environ.get(launch.bin_env_var)
    if override and platform_compat.is_executable_file(override):
        return _normalize_exe_casing(override) or override, search_path

    mise_resolved = _mise_which(launch.binary)
    if mise_resolved:
        return mise_resolved, search_path

    on_path = shutil.which(launch.binary, path=search_path)
    if on_path:
        return _normalize_exe_casing(on_path) or on_path, search_path

    return None, search_path


# ── The enforced-host credential-mask preflight ──


# The enforced-adapter preflight (sandbox-backend probe + credential-mask
# resolution) is blocking filesystem work run off the loop; this bounds the
# wait for it. Sized for a cold sandbox probe (its own subprocess budget is
# 20 s) plus canonical resolution of the home and override roots on a slow
# disk, with headroom. On expiry the adapter is REFUSED, never started with
# its mask missing.
_SANDBOX_PREFLIGHT_TIMEOUT = 60.0


def _sandbox_preflight(backend: str, mode: str) -> tuple[str, ...]:
    """Refuse an unmasked enforced adapter, then resolve its credential mask.

    One function so the caller pays ONE ``asyncio.to_thread`` hop for both steps:
    ``enforce_sandbox_floor`` probes for a sandbox backend and
    ``adapter_hidden_credential_dirs`` resolves the home and every env-override root,
    and both are blocking filesystem work that must not run on the event loop.

    Raises :class:`AcpToolGateUnroutable` when this session would spawn the adapter
    with its mask dropped; returns the mask otherwise (empty for a harness this core
    does not enforce, so their spawn arguments stay byte-identical).
    """
    try:
        acp_tool_gate.enforce_sandbox_floor(backend, mode)
        return acp_tool_gate.adapter_hidden_credential_dirs(backend)
    except acp_tool_gate.ToolGateUnroutable as exc:
        # Translate at the boundary, exactly as the session-routing path does.
        # ``acp_tool_gate`` is a LEAF that cannot import this module, so its
        # ToolGateUnroutable is a plain ``Exception``: it is neither an
        # ``AcpError`` (so the transport ladder in ``ensure_ready`` cannot see
        # it) nor the ``AcpToolGateUnroutable`` the dedicated non-retrying
        # handler names (an unrelated class). Raised raw, a sandbox-floor
        # refusal therefore escaped ``ensure_ready`` uncaught and skipped the
        # cleanup every other refusal path runs. ``from None`` because the
        # wrapper carries the whole actionable message already.
        raise AcpToolGateUnroutable(str(exc)) from None


async def _run_preflight_bounded(
    preflight: Callable[[str, str], tuple[str, ...]], backend: str, mode: str
) -> tuple[str, ...]:
    """Run *preflight* off the loop and give up after ``_SANDBOX_PREFLIGHT_TIMEOUT``.

    The mask half of the preflight canonicalizes the home and every override root
    on disk, and on a stalled mount that wait has no natural end: nothing else on
    the spawn path bounds it (``ensure_ready`` times the ACP handshake, which comes
    AFTER the spawn), so without this the only backstop was the subagent startup
    watchdog. Expiry raises :class:`AcpError`, the retryable kind: a stall is a
    transient fact about the disk, not a configuration fact like
    :class:`AcpToolGateUnroutable`, so the one retry ``ensure_ready`` grants is
    the right shape. The adapter is never started without its mask.

    Takes the preflight as a parameter so the deadline is testable without a
    spawn; each enforced host's adapter passes :func:`_sandbox_preflight`.
    """
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(preflight, backend, mode), timeout=_SANDBOX_PREFLIGHT_TIMEOUT
        )
    except asyncio.TimeoutError:
        raise AcpError(
            f"Could not start the {backend} adapter: computing its sandbox credential "
            "mask needs the home and credential roots resolved on disk, and that did "
            f"not finish within {_SANDBOX_PREFLIGHT_TIMEOUT:.0f} s (a stalled or very "
            "slow filesystem). The adapter is not started without its mask; retry "
            "once the disk responds."
        ) from None


# ── A host's read-back child ──


def _unlink_readback_launcher(path: str) -> None:
    """Remove a sandbox launcher artifact whose child has already exited."""
    try:
        os.remove(path)
    except OSError:
        pass


#: How much of a refused read-back child's stderr is examined at all.
#: A harness is free to write a screenful of banner, or a hundred megabytes, and
#: this only ever needs the tail, where a launcher puts its verdict. Bounding the
#: scan bounds the matching work; nothing outside the window is read.
_READBACK_STDERR_SCAN_CHARS = 3200


#: How many recognised fault shapes one refusal reports, most specific first.
#: A shebang fault spells two at once (``bad interpreter: No such file or
#: directory``) and both halves are worth having; past that a refusal is being
#: padded rather than explained.
_READBACK_FAULT_MAX_SHAPES = 2


#: The CLOSED vocabulary of exec-failure shapes a refused read-back can report.
#:
#: Each entry pairs a pattern matched against the child's stderr with the phrase
#: THIS MODULE publishes when it matches, so published text is always a literal
#: written here and never a byte the child wrote. That is the point rather than a
#: side effect. The child is a foreign harness binary and its stderr can hold
#: whatever the operator's environment put in front of it, a credential included;
#: any scheme that ECHOES those bytes has to prove no credential survives, which
#: means proving a negative about arbitrary bytes against redactor patterns that
#: need contiguity and label anchors. One inserted byte -- a line wrap, an SGR
#: colour code -- breaks the anchor while leaving every character of the secret
#: sitting in the text. With an SGR colour code inside a ``glpat-`` token body, 530
#: of 700 splices leave the whole token readable that way: rejoining the run
#: destroys the ``-`` the pattern anchors on, and not rejoining leaves the ``[31m``
#: residue inside it. Reporting a MATCH removes the question instead of answering
#: it -- there is no path from a child byte to published text, so there is nothing
#: left to prove about the bytes.
#:
#: Covers what BOTH read-backs hit, which is why it reaches past exec failures: the
#: pi read-back's launcher refuses an exec, while the opencode read-back parses a
#: config document and can reject the flags it was handed. A shape neither of them
#: produces is not worth carrying.
#:
#: Ordered most specific first, because the shapes overlap: a shebang fault reads
#: ``bad interpreter: No such file or directory``, where the interpreter is the
#: cause and the missing file only its symptom.
#:
#: What this deliberately drops is the DETAIL inside a recognised message -- which
#: line of the config failed to parse, which path the OS refused. A capture would
#: put child bytes back in the output and reopen the whole question for the sake of
#: a number the harness repeats the moment the operator runs it themselves.
#:
#: Case-insensitive, and matched as substrings rather than whole lines, because the
#: launcher's wording differs by platform -- ``/bin/sh``, ``dyld``, ``cmd.exe`` and
#: Node each frame these differently -- while the fault underneath does not.
_READBACK_FAULT_SHAPES: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"bad interpreter", re.IGNORECASE),
        "its shebang interpreter could not be run",
    ),
    (
        re.compile(
            r"bad CPU type|Exec format error|ENOEXEC|cannot execute binary file",
            re.IGNORECASE,
        ),
        "it is built for a different CPU or executable format",
    ),
    (
        re.compile(r"code ?signature|Killed: ?9", re.IGNORECASE),
        "the OS killed it over its code signature",
    ),
    (
        re.compile(r"Library not loaded|image not found|shared object file", re.IGNORECASE),
        "a shared library it needs is missing",
    ),
    (
        re.compile(r"unknown (?:flag|option|argument)|unrecognized (?:option|argument)", re.I),
        "the gateway passed it a flag this harness version does not accept",
    ),
    (
        re.compile(
            r"cannot parse|parse error|syntax ?error|unexpected token|unexpected end of"
            r"|invalid JSON|JSONDecodeError|YAMLException",
            re.IGNORECASE,
        ),
        "its configuration could not be parsed",
    ),
    (
        re.compile(r"Operation not permitted|EPERM", re.IGNORECASE),
        "the OS denied the operation, as a sandbox, quarantine or privacy policy does",
    ),
    (
        re.compile(r"Permission denied|EACCES", re.IGNORECASE),
        "the OS refused to execute it",
    ),
    (
        re.compile(r"Text file busy", re.IGNORECASE),
        "the file was still being written",
    ),
    (
        re.compile(r"Is a directory", re.IGNORECASE),
        "the path is a directory, not a program",
    ),
    (
        re.compile(r"Too many levels of symbolic links", re.IGNORECASE),
        "its path loops through symlinks",
    ),
    (
        re.compile(r"No such file or directory|ENOENT|not found", re.IGNORECASE),
        "the path does not exist",
    ),
)


def _readback_stderr_diagnosis(stderr: object) -> str:
    """What a refused read-back child's stderr says went wrong, in this module's words.

    A gate read-back that fails reports its child's exit code, and that code alone
    names a verdict without a cause: on the pi read-back the launcher is ``/bin/sh``
    exec'ing the resolved harness binary, so ``exit 126`` is the shell refusing the
    exec, and an exec the OS denied (``Permission denied``), a shebang it cannot
    resolve (``bad interpreter``) and a binary built for another architecture
    (``Bad CPU type in executable``) are three different faults with three different
    fixes. Only the child knows which one happened, so the refusal that reaches the
    operator carries it.

    What the refusal does NOT carry is the child's own bytes. The stderr is matched
    against :data:`_READBACK_FAULT_SHAPES` and the phrase written there for the
    matching shape is what gets published, so the output is drawn from a closed
    vocabulary defined in this module. Nothing has to be proved about the child's
    bytes because none of them are published -- see that constant for the measured
    reason echoing the bytes cannot offer the same guarantee.

    An unrecognised stderr answers ``""``, and the caller then reports the bare exit
    code with no diagnosis. The caller still separates that from a SILENT child, so
    "said something we do not recognise" and "said nothing at all" stay different
    answers to the operator.

    Non-strings and blank stderr answer ``""``.
    """
    if not isinstance(stderr, str) or not stderr:
        return ""
    window = stderr[-_READBACK_STDERR_SCAN_CHARS:]
    matched: list[str] = []
    for pattern, phrase in _READBACK_FAULT_SHAPES:
        if pattern.search(window) and phrase not in matched:
            matched.append(phrase)
            if len(matched) == _READBACK_FAULT_MAX_SHAPES:
                break
    return "; ".join(matched)


def _readback_detail_with_diagnosis(detail: str, stderr: object) -> str:
    """*detail* plus what the child said about its own failure, when that is known.

    Three outcomes, and the operator needs them apart. A recognised fault appends
    the vocabulary phrase. Stderr holding something unrecognised says so without
    quoting it, because "the harness explained itself and we could not read the
    explanation" points at this vocabulary needing a shape, while a SILENT child
    points at the harness. Nothing on stderr leaves *detail* alone.
    """
    diagnosis = _readback_stderr_diagnosis(stderr)
    if diagnosis:
        return f"{detail}: {diagnosis}"
    if isinstance(stderr, str) and stderr.strip():
        return f"{detail}, and its stderr holds no message this gateway recognises"
    return detail


async def resolve_self_served_launch(backend: str) -> tuple[str, list[str], str, str]:
    """The binary, argv, spawn label and stderr label for a self-served harness.

    ONE resolution for every member of ``ACP_BACKEND_LAUNCH``, because for those
    harnesses all four answers are values their row already holds: the binary that
    serves ACP, the args that follow it, and the two labels derived from the same
    pair. A harness whose argv needs a decision is not a member and does not call
    this.

    Off-loop, like the per-harness resolutions it replaces: the ladder reads the
    environment and stats candidate paths. Cached per backend for the gateway's
    life, which is the contract the install probe's ``restart_required`` answer
    reports on.

    Kiro-cli does not reach here and neither do the three Node adapters, so this
    adds no step and no conditional to their construction paths (harness-parity
    H13).
    """
    launch = launch_for(backend)
    if backend in _self_served_bin_caches:
        binary, search_path = _self_served_bin_caches[backend]
    else:
        epoch = _resolution_epoch(backend)
        resolved = await asyncio.to_thread(_resolve_self_served_bin, backend)
        # Publish only under the generation this resolve started in. A clear that
        # landed while it ran means the answer predates an install, so writing it
        # would undo the clear -- see ``_resolution_generation``. This session still
        # uses its own answer: it began before the install and that verdict is
        # honest for itself. Reading the local rather than re-subscripting keeps a
        # concurrent pop from raising ``KeyError`` here.
        if _resolution_epoch(backend) == epoch:
            _self_served_bin_caches[backend] = resolved
        binary, search_path = resolved
    if not binary:
        raise AcpError(
            f"{launch.binary} not found "
            f"({describe_search_path(search_path)}). Install it with "
            f"'{launch.install_command}', or set {launch.bin_env_var} to the "
            f"executable. {launch.missing_hint}"
        )
    return binary, [binary, *launch.acp_args], launch.spawn_label, launch.binary


# ── The launch tail ──


class LaunchHost(Protocol):
    """The driver state :func:`launch` writes while it runs, and the driver's helpers.

    Both drivers carry these under the same names, and every one of them is read by
    the driver's own teardown and reset paths, so the tail writes them at exactly
    the points the drivers always have: a reset that lands mid-launch finds the
    sandbox launcher and the scratch window it has to reclaim.
    """

    _work_dir: Path
    _scratch_dir: Path | None
    _shared_scratch: Path | None
    _sandbox_cleanup: str | None
    _sandbox_wrapped_by_crew: bool
    _sandbox_hidden_dirs: tuple[str, ...]
    _spawn_work_dir: str
    _bound_workspace_fd: int | None

    async def _discard_bound_workspace(self) -> None: ...

    def _discard_sandbox_cleanup(self) -> None: ...

    async def _to_thread_guarding_sandbox(
        self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any
    ) -> Any: ...


@dataclass(frozen=True)
class LaunchTools:
    """The OS-facing collaborators the tail calls, as one driver's module binds them.

    The production values are the driver module's own names, read when it launches,
    so a patch of ``kiro_crew.acp.client.<name>`` or ``kiro_crew.acp.runtime.<name>``
    reaches the launch that driver starts. A test of :func:`launch` hands in fakes.
    """

    logger: logging.Logger
    platform_compat: ModuleType
    agent_scratch: ModuleType
    apply_pod_bundle_spawn: Callable[..., tuple[list[str], bool]]
    forward_ssh_auth_sock: Callable[[], bool]
    wrap_argv_async: Callable[..., Awaitable[tuple[list[str], str | None]]]
    wrap_argv: Callable[..., Any]
    wrapped_by_crew_sandbox: Callable[[Sequence[str]], bool]
    cgroup_scope_argv: Callable[[list[str]], list[str]]
    augmented_path: Callable[[str], str]
    scrub_agent_subprocess_env: Callable[..., dict[str, str]]
    apply_pod_home_remap: Callable[..., dict[str, str]]
    browser_session_env: Callable[[Mapping[str, str]], dict[str, str]]
    browser_socket_env: Callable[[Mapping[str, str]], dict[str, str]]
    inject_xdist_auto_cap: Callable[[dict[str, str]], Any]
    bind_voice_safe_agent_workspace_async: Callable[[Any], Awaitable[tuple[str, int | None]]]
    create_subprocess_limited: Callable[..., Awaitable[asyncio.subprocess.Process]]
    retrying_spawn_factory: Callable[..., Awaitable[asyncio.subprocess.Process]] | None = None


def _unscoped(argv: list[str]) -> tuple[list[str], str]:
    return argv, ""


def _no_env_step(env: dict[str, str]) -> None:
    return None


@dataclass(frozen=True)
class LaunchRequest:
    """One launch: the host's plan, the driver's facts, and the driver's own steps.

    The steps are the points where the two drivers genuinely differ, each called
    at one fixed position in the tail so the order of the child's environment is
    the same for every host on a driver:

    * ``env_before_scrub`` -- after the base environment and before the scrub:
      session identity and credential repair on the client, the harness's own
      variables on the runtime. It may hop off the loop, through the guarded hop.
    * ``env_after_scrub`` -- after ``KIROCREW_RUNTIME_PYTHON``.
    * ``env_after_marker`` -- after the orphan-sweep marker: the runtime's spawn
      instance and data home.
    * ``env_after_scratch`` -- after the scratch variables: the runtime's
      private-state directory.
    * ``scope_argv`` -- after the cgroup wrap: the runtime names its scope.
    """

    argv: list[str]
    backend: str
    sandbox_mode: str
    extra_env: Mapping[str, str]
    scratch_label: str
    env_before_scrub: Callable[[dict[str, str], str | None], Awaitable[dict[str, str]]]
    extra_hidden_dirs: tuple[str, ...] = ()
    extra_expose_files: tuple[str, ...] = ()
    internal_sandbox: bool = False
    pod_home_remap: bool = False
    #: The runtime allocates scratch through its guarded hop; the client never has.
    guard_scratch_hops: bool = False
    env_after_scrub: Callable[[dict[str, str]], None] = field(default=_no_env_step)
    env_after_marker: Callable[[dict[str, str]], None] = field(default=_no_env_step)
    env_after_scratch: Callable[[dict[str, str]], None] = field(default=_no_env_step)
    scope_argv: Callable[[list[str]], tuple[list[str], str]] = field(default=_unscoped)


@dataclass(frozen=True)
class LaunchedProcess:
    """A created, still-suspended host process, and what its launch decided."""

    process: asyncio.subprocess.Process
    scope_unit: str = ""


async def launch(host: LaunchHost, request: LaunchRequest, tools: LaunchTools) -> LaunchedProcess:
    """Start *request*'s host process for *host*, suspended, through the shared tail.

    Writes the driver's scratch, sandbox and workspace state as it goes (see
    :class:`LaunchHost`). Every off-loop step between the sandbox wrap and the
    workspace binding goes through the driver's guarded hop, so the launcher the wrap
    wrote is reclaimed if one of them raises, a cancellation included; a failed spawn
    releases the launcher and the bound workspace. Raises whatever the failing step
    raised.
    """
    # OS-level sandbox: wrap the command to hide sensitive paths.
    # strip_python_env keeps the host PYTHONPATH/PYTHONHOME out of kiro-cli's
    # foreign MCP subprocesses (which bundle their own interpreter + deps).
    # is_kiro_cli is membership in ACP_BACKENDS_INTERNAL_SANDBOX
    # (harness-parity H7), not "not claude": the flag makes wrap_argv SKIP
    # Crew's seatbelt on macOS and grants Windows's Kiro-only delegation in
    # favour of the harness's own internal sandbox, so a harness without one
    # must never be granted it by the absence of another harness.
    #
    # Inside a pod both answers come from apply_pod_bundle_spawn, which is
    # where the ONE reason lives: the pod HOME remap breaks the toolbox shim's
    # own sandbox, so the child runs the bundle binary and Crew's launcher
    # wraps it. Off-loop because the resolution stats the candidate path.
    argv, delegate_internal_sandbox = await asyncio.to_thread(
        tools.apply_pod_bundle_spawn, request.argv, backend=request.backend
    )
    spawned_binary = argv[0] if argv else None
    # Per-process scratch containment. Allocated BEFORE the sandbox is built:
    # the scratch ROOT is masked for every sandboxed process
    # (``sandbox._CREW_HIDDEN_LEAVES``), so this child's own directory is
    # re-exposed as a PRIVATE window (siblings stay hidden). Fail-open; owner
    # recorded after spawn; reclamation is liveness-keyed, never age-keyed.
    scratch_hop = (
        host._to_thread_guarding_sandbox if request.guard_scratch_hops else asyncio.to_thread
    )
    if host._shared_scratch is None and host._scratch_dir is not None:
        # A respawn: the directory the previous process exposed IS this
        # session's tree -- the children it spawned mounted it, and the work it
        # staged is there -- so the new process joins it instead of starting an
        # empty one that hides that work until the old directory is reclaimed.
        # Validated and adopted below like any inherited tree; dropped if it was
        # swept meanwhile.
        host._shared_scratch = host._scratch_dir
    host._scratch_dir = None
    try:
        host._scratch_dir = await scratch_hop(
            tools.agent_scratch.allocate_scratch, request.scratch_label
        )
    except (OSError, tools.agent_scratch.ScratchBoundaryError):
        # The boundary refusal joins OSError HERE and deliberately not at
        # record_owner after the spawn: no child exists yet, so there is nothing
        # to stop, and scratch is hygiene rather than a spawn prerequisite.
        tools.logger.warning(
            "agent-scratch: could not allocate; spawning with inherited temp",
            exc_info=True,
        )
    scratch_window = (str(host._scratch_dir),) if host._scratch_dir is not None else ()
    # The session tree's work directory, when this process is not the tree's
    # first: a second window into the masked root, re-validated now because the
    # allocation it names may have been swept since it was recorded, and dropped
    # -- not re-created -- if so.
    if host._shared_scratch is not None:
        host._shared_scratch = await scratch_hop(
            tools.agent_scratch.shared_scratch_window, host._shared_scratch
        )
    if host._shared_scratch is not None:
        scratch_window = (*scratch_window, str(host._shared_scratch))
    # Resolve the SSH_AUTH_SOCK forward opt-in OFF the event loop (the config
    # load may stat/read config) ONCE, then pass the resolved boolean into both
    # the sandbox wrap below and the parent-side scrub further down, so neither
    # reads config synchronously on the loop. Scoped to this agent spawn:
    # generic launchers default the flag off and keep scrubbing the socket.
    forward_ssh_auth_sock = await asyncio.to_thread(tools.forward_ssh_auth_sock)
    argv, host._sandbox_cleanup = await tools.wrap_argv_async(
        argv,
        mode=request.sandbox_mode,
        strip_python_env=True,
        forward_ssh_auth_sock=forward_ssh_auth_sock,
        # Credential homes the standard tier exposes for kiro-cli's sake and that
        # an enforced host has no claim on. Empty for every harness this core does
        # not enforce, so their spawn arguments are unchanged.
        extra_hidden_dirs=request.extra_hidden_dirs,
        extra_private_dirs=scratch_window,
        extra_expose_files=request.extra_expose_files,
        is_kiro_cli=delegate_internal_sandbox,
        _prepare=tools.wrap_argv,
    )
    # Which isolation layer this spawn actually got, recorded HERE from the argv
    # the wrap returned -- the wrap's own record of the branch it took, which no
    # later re-derivation from mode + platform + settings can match (see
    # ``sandbox.wrapped_by_crew_sandbox``). Read back only when a sandbox-init
    # refusal has to name the layer to turn off; the cgroup scope below prepends
    # its own tokens, so the read happens before it.
    host._sandbox_wrapped_by_crew = tools.wrapped_by_crew_sandbox(argv)
    host._sandbox_hidden_dirs = tuple(request.extra_hidden_dirs)
    # cgroup v2 scope (OUTERMOST): bound this agent + all its MCP-server / tool
    # descendants with pids.max (fork bomb) + memory.max (RSS balloon). No-op +
    # loud warning where cgroup delegation is unavailable. --scope execs into the
    # target, so the pid the driver records is still the real child. Off-loop:
    # the first call probes /proc + /sys and the config read touches the config
    # dir. Guarded: the wrap above allocated the sandbox temp file, so a
    # cancellation here must not orphan it.
    argv = await host._to_thread_guarding_sandbox(tools.cgroup_scope_argv, argv)
    argv, scope_unit = request.scope_argv(argv)

    env = {**os.environ}
    if request.extra_env:
        env.update(request.extra_env)
    env["PATH"] = tools.augmented_path(env.get("PATH", ""))
    env = await request.env_before_scrub(env, spawned_binary)
    # Match the OS launchers' sensitive + Python env scrub in the parent. Windows
    # Kiro delegation has no POSIX `env -u` wrapper, so this is the enforcement
    # point there. Kept after the driver's credential repair so no resolver can
    # reintroduce a denied variable. forward_ssh_auth_sock is the opt-in resolved
    # off-loop above and reused here.
    env = tools.scrub_agent_subprocess_env(env, forward_ssh_auth_sock=forward_ssh_auth_sock)
    # Bundled skill scripts must not depend on a system ``python`` name. The
    # desktop bundles carry their interpreter outside the user's PATH, while this
    # path is already running under the exact environment that can import
    # ``kiro_crew``. Overwritten after the scrub and after ``extra_env`` so agent
    # configuration cannot redirect the trusted read gate to a foreign interpreter.
    env["KIROCREW_RUNTIME_PYTHON"] = sys.executable
    request.env_after_scrub(env)
    # Pod-scoped kiro-cli children write their OWN MCP OAuth grants, confined to
    # the pod's tree instead of the real host's -- see
    # ``acp.client._apply_pod_home_remap``. No-op outside a pod (KIROCREW_POD is
    # not exactly "1") and for every harness outside ACP_BACKENDS_POD_HOME_REMAP,
    # which is deliberately its own set rather than the internal-sandbox one (H6).
    env = tools.apply_pod_home_remap(env, pod_home_remap=request.pod_home_remap)
    # Positive-identity marker for the orphan sweep: kiro-cli and every MCP server
    # it spawns inherit this, so escaped launcher trees (``npx @playwright/mcp`` ->
    # node) are identifiable as ours.
    env[KIROCREW_SPAWNED_ENV] = KIROCREW_SPAWNED_VALUE
    request.env_after_marker(env)
    # Own browser session per agent process: the CLI resolves a nameless command to
    # one shared ``default`` browser, so without this two agents navigate and close
    # each other's pages (see browser_session_env). Per PROCESS, not per agent: a
    # shared runtime's sessions share this one.
    browser_env = tools.browser_session_env(env)
    env.update(browser_env)
    if browser_env:
        lifecycle_env = {**os.environ, **browser_env}
        env.update(await host._to_thread_guarding_sandbox(tools.browser_socket_env, lifecycle_env))
    # The scratch dir was allocated before the sandbox wrap (carved out of the
    # masked root there); hand it to the child as its temp.
    if host._scratch_dir is not None:
        env.update(tools.agent_scratch.scratch_env(host._scratch_dir, shared=host._shared_scratch))
    elif host._shared_scratch is not None:
        # Own allocation failed (inherited temp) but the tree's work directory is
        # mounted: the prompt-visible name still points there.
        env["KIROCREW_SCRATCH"] = str(host._shared_scratch)
    request.env_after_scratch(env)
    # Memory-aware cap for pytest-xdist's ``-n auto``: xdist sizes auto to the CPU
    # count, ignoring memory, so a full-suite run in an agent turn can spawn
    # cpu_count workers x ~1 GB each and exhaust the host. xdist honors
    # PYTEST_XDIST_AUTO_NUM_WORKERS when resolving auto, so seeding it here bounds
    # ONLY auto resolution -- explicit ``-n N``, non-xdist runs, and venvs without
    # xdist are unaffected. Respects a value already present in the env; see
    # resource_status.inject_xdist_auto_cap. Off-loop: resolving the cap reads the
    # raw config. Guarded: the sandbox temp file is live.
    await host._to_thread_guarding_sandbox(tools.inject_xdist_auto_cap, env)

    await host._discard_bound_workspace()
    if request.internal_sandbox:
        host._spawn_work_dir, host._bound_workspace_fd = (
            await tools.bind_voice_safe_agent_workspace_async(host._work_dir)
        )
    # Process-group isolation for clean tree-kill. Both flags explicit (NOT via
    # **dict unpack -- that breaks mypy's Popen overload resolution on the build
    # fleet). POSIX: start_new_session=True calls setsid so a kill can killpg the
    # whole group; creationflags resolves to 0. Windows: no setsid, so
    # CREATE_NEW_PROCESS_GROUP makes the child tree taskkill /T-reapable and stops
    # an inherited Ctrl-C propagating into the gateway; CREATE_NO_WINDOW suppresses
    # the console window a windowless gateway would otherwise pop. The child starts
    # SUSPENDED so its resource ceiling is applied before it runs a single
    # instruction; the driver resumes it.
    factory: Callable[..., Awaitable[asyncio.subprocess.Process]] = functools.partial(
        tools.create_subprocess_limited,
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=host._spawn_work_dir,
        limit=_STDOUT_BUFFER_LIMIT,
        env=env,
        start_new_session=tools.platform_compat.IS_POSIX,
        creationflags=(
            tools.platform_compat.CREATE_NEW_PROCESS_GROUP
            | tools.platform_compat._SUBPROCESS_NO_WINDOW
            | tools.platform_compat.CREATE_SUSPENDED
        ),
        # None off macOS, where nothing binds. When set, the child enters the
        # workspace through this verified descriptor instead of resolving
        # ``cwd``'s pathname, which a same-UID symlink retarget could aim elsewhere
        # in between; ``cwd`` stays the same directory by name.
        chdir_fd=host._bound_workspace_fd,
        profile=RLIMIT_PROFILE_SESSION_HOST,
    )
    if tools.retrying_spawn_factory is not None:
        factory = functools.partial(tools.retrying_spawn_factory, factory)
    try:
        process = await tools.platform_compat.create_windows_cleanup_owned_process(factory)
    except BaseException:
        await host._discard_bound_workspace()
        host._discard_sandbox_cleanup()
        raise
    return LaunchedProcess(process=process, scope_unit=scope_unit)
