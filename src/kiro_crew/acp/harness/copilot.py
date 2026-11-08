"""GitHub Copilot CLI, launched one process per session by ``AcpClient``.

Copilot CLI serves ACP from its own binary (``copilot --acp --stdio``), so its
executable comes off the shared self-served ladder and there is no adapter package.
Three things are its own:

* **Its permission route is an ACP v1 session config option**, ``allow_all``, which
  Crew writes ``off`` after ``session/new`` and ``session/load`` and before the first
  prompt (``Routing.SESSION_CONFIG``, applied by
  ``AcpClient._apply_session_permission_routing``). Nothing is seeded here for it.
* **Two environment variables are REMOVED from the child**, because each is a
  permission source Copilot CLI reads above the session: ``COPILOT_ALLOW_ALL``
  auto-approves every tool, and its exact value ``true`` additionally trusts the
  working directory without asking -- which loads that directory's hooks, plugins
  and MCP servers. ``COPILOT_ASSISTED_APPROVAL`` replaces the approval prompt with a
  probabilistic judge.
* **Crew's stdio MCP servers travel on the launch, not on the session.** Copilot CLI
  1.0.100 REJECTS a stdio element in the ``session/new`` ``mcpServers`` array
  ("Rejecting non-http/sse MCP server ... from client" in its own log, no error on
  the wire, the server never started), and serves only ``http``/``sse`` from there.
  It takes stdio servers from ``--additional-mcp-config``, measured end to end: the
  named server was asked ``initialize``, ``tools/list`` and ``tools/call`` and each
  call raised ``session/request_permission`` first. So the stdio half of the
  session's projected array is written to a launch config file and named on the
  argv, while the wire array still carries the whole projection -- the ``http`` and
  ``sse`` elements Copilot CLI does take from there, and the roster Crew's MCP report
  and mount check read. No server is mounted twice: each transport has one channel.

  The file, not inline JSON on the argv, because an element's ``env`` carries a
  session credential on Crew's own control plane and a command line is readable by
  every local user. The file is owner-only, in the data home's masked
  ``auth-store-staging`` directory, handed back read-only to this child alone, and is
  removed as soon as ``initialize`` answers (Copilot CLI reads it when it boots;
  measured), on a failed handshake and on a refused spawn too.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import tempfile
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.acp import launch as launch_mod
from kiro_crew.acp._dispatch_parts.tool_call_provenance import TOOL_CALL_CACHE_MAX_KEY_CHARS
from kiro_crew.acp.harness.base import ProcessAdapter, SpawnContext, SpawnPlan
from kiro_crew.acp.mcp_startable import prune_unstartable
from kiro_crew.acp.transport_errors import AcpError, AcpToolGateUnroutable
from kiro_crew.agent_sdk.backends import ACP_BACKEND_COPILOT
from kiro_crew.config.paths import config_dir
from kiro_crew.hidden_leaf_staging import auth_store_staging_dir
from kiro_crew.security.paths import is_sensitive_path

__all__ = [
    "COPILOT_DROPPED_ENV",
    "CopilotLaunch",
    "copilot_installed_version",
    "copilot_mcp_config",
    "copilot_repo_config_issue",
    "discard_copilot_mcp_config",
    "write_copilot_mcp_config",
]

#: Variables a Crew child never inherits. The first two are permission sources
#: Copilot CLI reads above the session (``copilot help environment``,
#: ``--assisted-approval``). The dist and package-cache ones select which JS build
#: runs, which ``--no-auto-update`` would otherwise not hold to the verified range.
#: The rest opt a session into autopilot after planning, or into loading a
#: repository's hooks, extensions or workspace MCP servers; read in 1.0.100's
#: bundle on the interactive and prompt paths, dropped here so no path inherits them.
COPILOT_DROPPED_ENV: tuple[str, ...] = (
    "COPILOT_ALLOW_ALL",
    "COPILOT_ASSISTED_APPROVAL",
    "COPILOT_CLI_DIST_DIR",
    "COPILOT_CLI_RESOLVED_DIST_DIR",
    "COPILOT_PKG_CACHE_HOME",
    "COPILOT_PLAN_THEN_AUTOPILOT",
    "COPILOT_PROMPT_MODE_REPO_HOOKS",
    "COPILOT_PROMPT_MODE_EXTENSIONS",
    "COPILOT_PROMPT_MODE_WORKSPACE_MCP",
    "GITHUB_COPILOT_PROMPT_MODE_REPO_HOOKS",
    # Git's own discovery variables: Copilot CLI finds the repository whose
    # configuration it loads through git, so one of these inherited from the
    # gateway could point it at a root ``copilot_repo_config_issue`` never checked.
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
)

#: The launch option Copilot CLI reads extra MCP servers from, and the prefix that
#: makes its value a file path rather than inline JSON.
_ADDITIONAL_MCP_CONFIG_ARG = "--additional-mcp-config"
_FILE_VALUE_PREFIX = "@"
#: The file-name prefix of a launch config inside the auth-store staging directory,
#: followed by the writing gateway's pid. The directory is shared with the gateway's
#: own auth-store temps, so the sweep only ever touches names carrying this prefix.
_MCP_CONFIG_PREFIX = "copilot-mcp-"
#: A config this gateway wrote that is older than this was left by a spawn that
#: never reached ``initialize`` (the start budget is far shorter), so a later write
#: removes it. Files of a gateway that is not running go at once.
_STALE_CONFIG_SECS = 600.0

#: The npm package Copilot CLI ships as, whose manifest names the installed release.
_COPILOT_NPM_PACKAGE = "@github/copilot"
#: How many directories above the resolved binary a manifest is looked for in. The
#: npm bin link resolves to ``npm-loader.js`` at the package root, so one is enough
#: on POSIX; the margin covers a loader moved one level down.
_COPILOT_MANIFEST_SEARCH_DEPTH = 3


def copilot_installed_version(copilot_bin: str) -> str:
    """The release the npm install *copilot_bin* runs, or ``""`` when unreadable.

    Read from ``@github/copilot``'s own ``package.json``, not by running the binary:
    the install probe is polled by the dashboard, and a few file reads are not a
    child process per poll. ``--no-auto-update`` keeps the session on this release,
    so it is the one ``initialize`` will report. On POSIX the bin link resolves into
    the package; on Windows the bin is a ``copilot.cmd`` shim in the npm prefix, with
    the package under that directory's ``node_modules`` (or beside it, from
    ``node_modules/.bin``). A standalone build carries no manifest and answers
    ``""``: an unread version is not a wrong one, and the handshake still checks.

    Blocking (reads files); the probe runs off the loop.
    """
    try:
        here = Path(os.path.realpath(copilot_bin)).parent
    except (OSError, ValueError):
        return ""
    roots = [here / "node_modules"]
    if here.name == ".bin":
        roots.append(here.parent)
    candidates = [root / _COPILOT_NPM_PACKAGE / "package.json" for root in roots]
    candidates += [
        directory / "package.json"
        for directory in [here, *here.parents][:_COPILOT_MANIFEST_SEARCH_DEPTH]
    ]
    for manifest in candidates:
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("name") == _COPILOT_NPM_PACKAGE:
            version = data.get("version")
            return version.strip() if isinstance(version, str) else ""
    return ""


def copilot_mcp_config(elements: list[Any]) -> dict[str, Any] | None:
    """Copilot CLI's ``mcpServers`` document for the stdio half of *elements*.

    *elements* is the session's projected ACP array (``acp_server_element`` shape:
    ``name``, ``command``, ``args``, ``env`` as name/value pairs). Only stdio
    elements are taken -- an element carrying ``url`` stays on the wire, where
    Copilot CLI accepts it. ``tools: ["*"]`` because the projection already decided
    which servers enter the array; narrowing a server per tool withholds it whole
    (``providers/mirrors/copilot.py``), so there is nothing finer to say here.
    ``None`` when no stdio element is left, so no file and no argument is added.
    """
    servers: dict[str, Any] = {}
    for element in elements:
        if not isinstance(element, dict) or element.get("url"):
            continue
        name = element.get("name")
        command = element.get("command")
        if not isinstance(name, str) or not name or not isinstance(command, str) or not command:
            continue
        args = element.get("args")
        env: dict[str, str] = {}
        for pair in element.get("env") or []:
            if isinstance(pair, dict) and isinstance(pair.get("name"), str):
                env[pair["name"]] = str(pair.get("value", ""))
        servers[name] = {
            "type": "local",
            "command": command,
            "args": [str(arg) for arg in args] if isinstance(args, list) else [],
            "env": env,
            "tools": ["*"],
        }
    return {"mcpServers": servers} if servers else None


def _config_directory() -> str:
    """The directory launch configs are written in: the masked auth-store staging dir.

    The launch file carries a session credential, so it lives in the data home's
    ``auth-store-staging`` directory, which every agent sandbox masks as a whole
    DIRECTORY and ``security.paths`` fences from agents' file tools -- the place the
    gateway already stages its own credential-bearing files. The spawn hands this one
    file back, read-only, to the Copilot CLI child it is for alone
    (``SpawnPlan.extra_expose_files``), so no other session's sandbox can read it.
    :func:`~kiro_crew.hidden_leaf_staging.auth_store_staging_dir` creates it owner-only
    and refuses a directory that is a link or that another account could write.
    Resolved with ``realpath``, the spelling the sandbox's own mask and carve-out use:
    on macOS a data home reached through a symlink (``/tmp`` -> ``/private/tmp``)
    would otherwise name a path the carve-out does not match, and the child could not
    read its own config. On Linux the child sees a read-only copy inside its own
    namespace for its lifetime; its processes already hold the same credential in its
    MCP servers' environment, and no other session's namespace has it. Blocking.
    """
    home = Path(os.path.realpath(str(config_dir())))
    return str(auth_store_staging_dir(home))


def _sweep_stale_configs(directory: str) -> None:
    """Remove configs a dead gateway, or a spawn that never started, left behind."""
    now = time.time()
    own = os.getpid()
    for name in os.listdir(directory):
        if not name.startswith(_MCP_CONFIG_PREFIX):
            continue
        pid_text = name[len(_MCP_CONFIG_PREFIX) :].split("-", 1)[0]
        full = os.path.join(directory, name)
        try:
            pid = int(pid_text)
            mtime = os.lstat(full).st_mtime
        except (ValueError, OSError):
            continue
        dead = pid != own and not platform_compat.pid_exists(pid)
        if dead or (pid == own and now - mtime > _STALE_CONFIG_SECS):
            with suppress(OSError):
                os.remove(full)


def write_copilot_mcp_config(document: dict[str, Any]) -> str:
    """Write *document* to a fresh owner-only file and return its path. Blocking."""
    directory = _config_directory()
    _sweep_stale_configs(directory)
    # ``mkstemp`` creates the file 0600 and O_EXCL, so no other user can read it
    # and no pre-placed link is followed.
    fd, path = tempfile.mkstemp(
        prefix=f"{_MCP_CONFIG_PREFIX}{os.getpid()}-", suffix=".json", dir=directory
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(document, handle)
    except BaseException:
        # A half-written file still carries part of a session credential.
        discard_copilot_mcp_config(path)
        raise
    return path


def discard_copilot_mcp_config(path: str) -> None:
    """Remove a launch config; one already gone is the same outcome. Blocking."""
    if path:
        with suppress(FileNotFoundError):
            os.remove(path)


def _has_entry(directory: str, *, prefix: str = "", suffix: str = "") -> str:
    """The first entry under *directory* named ``prefix*suffix`` (case-folded), or ``""``."""
    try:
        names = sorted(os.listdir(directory))
    except (NotADirectoryError, FileNotFoundError):
        return ""
    except OSError:
        # Present but unreadable: fail closed, the harness may read what Crew cannot.
        return directory
    for name in names:
        lowered = name.lower()
        if lowered.startswith(prefix) and lowered.endswith(suffix):
            return os.path.join(directory, name)
    return ""


#: Project files Copilot CLI 1.0.100 acts on from the folder it is pointed at, each a
#: way for repository content to run a command without a permission request. The
#: hooks were MEASURED running over ACP in an untrusted folder; a workspace
#: ``.mcp.json`` server was measured starting in a folder Copilot's own config
#: trusted (and not in an untrusted one); the rest are read off its bundle as loaded
#: from the project (``.github/copilot`` settings carry hooks and pre-approved URLs,
#: extensions launch processes, plugins and LSP configs name commands). Refused on
#: presence rather than on Copilot's trust list, because that list lives in its
#: credential file and a folder can become trusted without Crew seeing it.
#: Directories whose entries are listed: ``(relative directory, prefix, suffix)``,
#: an empty suffix meaning any entry. A listing that fails for a reason other than
#: absence is a finding (fail closed).
_PROJECT_CONFIG_DIRS: tuple[tuple[str, str, str], ...] = (
    (".github/hooks", "", ".json"),
    (".github/copilot", "settings", ".json"),
    (".github/extensions", "", ""),
    (".github/plugin", "plugin", ".json"),
    (".claude-plugin", "plugin", ".json"),
)
#: Single files probed by their exact name, so an ancestor Crew may traverse but not
#: list (a ``0711`` home root) is not mistaken for a finding: the harness opens these
#: by name the same way.
_PROJECT_CONFIG_FILES: tuple[str, ...] = (".mcp.json", ".github/mcp.json", ".github/lsp.json")


def _project_config_in(directory: str) -> str:
    """The first project config file Copilot CLI would act on in *directory*, or ``""``."""
    for relative in _PROJECT_CONFIG_FILES:
        candidate = os.path.join(directory, relative)
        if os.path.lexists(candidate):
            return candidate
    for relative, prefix, suffix in _PROJECT_CONFIG_DIRS:
        found = _has_entry(os.path.join(directory, relative), prefix=prefix, suffix=suffix)
        if found:
            return found
    return ""


#: How many Copilot tool calls one turn may hold an identity record for (ambiguous,
#: or unmatched but server-shaped) before the turn stops trusting any: past it a
#: record is not kept, and :data:`COPILOT_IDENTITY_OVERFLOW` makes every later
#: Copilot permission request this turn refused rather than judged without one.
COPILOT_IDENTITY_RECORDS_MAX = 256
COPILOT_IDENTITY_OVERFLOW = "\x00copilot-identity-overflow"
#: A served tool's title is ``<server>-<tool>``; past this it is not kept whole.
COPILOT_TITLE_MAX_CHARS = 512


def note_copilot_identity_record(records: set[str], ambiguous: set[str], key: str) -> None:
    """Add *key* to *records*, or mark the turn overflowed when it cannot be kept.

    Both the count and each key are bounded: an id longer than the shared caches'
    own key cap (:data:`TOOL_CALL_CACHE_MAX_KEY_CHARS`) is never stored, so the cap
    on the count bounds the memory too, and the overflow refuses rather than lets a
    call through unrecorded.
    """
    if len(key) > TOOL_CALL_CACHE_MAX_KEY_CHARS or (
        len(records) >= COPILOT_IDENTITY_RECORDS_MAX and key not in records
    ):
        ambiguous.add(COPILOT_IDENTITY_OVERFLOW)
    else:
        records.add(key)


#: A ``gitdir:`` pointer is one short line; anything longer is not one.
_GIT_POINTER_MAX_BYTES = 4096


def _is_git_root(directory: str) -> bool:
    """Whether *directory* holds a ``.git`` git itself would stop at.

    A directory, or a ``gitdir:`` pointer file whose target exists. A pointer git
    rejects does not end the harness's own upward search, so it does not end this one.

    The pointer is read from the unsandboxed gateway, so the read is fenced: a
    ``.git`` that is a symbolic link, a hard link, a FIFO or any other non-regular
    file is never opened (a workspace could point it at a credential file, or wedge
    the spawn on a FIFO), the file must be of pointer size, a path the sensitive-path
    keystone protects is not opened at all, and the file opened must be the one
    inspected. A refused read answers
    "not a root", which only walks the search further up, so it refuses more, never
    less.
    """
    marker = os.path.join(directory, ".git")
    if os.path.isdir(marker):
        return True
    try:
        seen = os.lstat(marker)
    except OSError:
        return False
    # A link, a FIFO (whose open would block), a device, a hard-linked file, or one
    # too big to be a pointer is never opened.
    if (
        not stat.S_ISREG(seen.st_mode)
        or seen.st_nlink != 1
        or seen.st_size > _GIT_POINTER_MAX_BYTES
        or is_sensitive_path(os.path.realpath(marker))
    ):
        return False
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_NOCTTY", 0)
    )
    try:
        fd = os.open(marker, flags)
    except OSError:
        return False
    try:
        info = os.fstat(fd)
        # The file opened must be the one inspected: a swap between the two (the
        # only race left where O_NOFOLLOW is missing, as on Windows) is refused.
        if (info.st_dev, info.st_ino) != (seen.st_dev, seen.st_ino) or not stat.S_ISREG(
            info.st_mode
        ):
            return False
        first = os.read(fd, _GIT_POINTER_MAX_BYTES).decode("utf-8").splitlines()[0].strip()
    except (OSError, UnicodeDecodeError, IndexError):
        return False
    finally:
        os.close(fd)
    if not first.startswith("gitdir:"):
        return False
    target = first[len("gitdir:") :].strip()
    return bool(target) and os.path.isdir(os.path.join(directory, target))


def _ancestors(start: str) -> list[str]:
    """*start* and its parents up to its git root, or to the filesystem root."""
    out: list[str] = []
    current = start
    while True:
        out.append(current)
        if _is_git_root(current):
            return out
        parent = os.path.dirname(current)
        if parent == current:
            return out
        current = parent


def copilot_repo_config_issue(work_dir: object) -> str:
    """Why a Copilot CLI session must not start in *work_dir*, or ``""``. Blocking.

    Copilot CLI 1.0.100 runs a repository's own ``.github/hooks/*.json`` commands
    (``sessionStart``, ``preToolUse`` ...) with NO ``session/request_permission`` --
    measured over ACP in a folder its config trusted and in one it did not, and a
    ``disableAllHooks`` in its own config did not stop them -- and acts on the other
    project files in :data:`_PROJECT_CONFIG_FILES` and :data:`_PROJECT_CONFIG_DIRS`. Any of them would act without Kiro
    Crew's approval, so a session is refused where one exists: in the working
    directory and every parent up to its git root (the filesystem root when there is
    none), along the path as given AND as resolved, since the harness is handed the
    first and a symlinked ancestor can make the second a different chain.
    """
    if not work_dir:
        return ""
    given = os.path.abspath(str(work_dir))
    resolved = os.path.realpath(given)
    seen: set[str] = set()
    for directory in [*_ancestors(given), *_ancestors(resolved)]:
        if directory in seen:
            continue
        seen.add(directory)
        found = _project_config_in(directory)
        if found:
            return (
                f"This project carries GitHub Copilot CLI's own configuration ({found}). "
                "Copilot CLI runs a repository's hook commands, and project MCP servers, "
                "without asking for permission, so they would bypass Kiro Crew's tool "
                "approval. Kiro Crew therefore does not start a Copilot CLI session here; "
                "this applies only to the Copilot CLI harness. Remove or rename that file, "
                "or pick another harness in Settings › Agent Harness, then start a new chat."
            )
    return ""


class CopilotLaunch(ProcessAdapter):
    """Copilot CLI: a self-served binary whose asking is a session config option."""

    backend = ACP_BACKEND_COPILOT

    async def resolve_spawn(self, ctx: SpawnContext) -> SpawnPlan:
        """Resolve the binary, warm the MCP array, write its stdio half, then mask."""
        session = ctx.session
        assert session is not None, "copilot is launched for one session"
        # Before anything is written or resolved: a project whose own Copilot CLI
        # configuration would run commands unasked is refused, every spawn and
        # respawn (a resume re-checks the folder as it is now).
        repo_issue = await asyncio.to_thread(
            copilot_repo_config_issue, session._spawn_work_dir or ctx.work_dir
        )
        if repo_issue:
            # A fact about the folder, so a retry refuses the same way: every surface
            # shows the remedy rather than a generic "try again".
            raise AcpToolGateUnroutable(repo_issue, transient=False, user_worded=True)
        (
            _bin,
            argv,
            spawn_label,
            stderr_label,
        ) = await launch_mod.resolve_self_served_launch(self.backend)
        # The agent spec is translated into this session's MCP array HERE, for the
        # reason every array-backed adapter does it in its own launch: the
        # translation reads disk, and doing it at the shared session/new call site
        # would put an executor hop and a new failure mode on every backend's
        # construction path, kiro-cli included (harness-parity H13).
        await session._prepare_session_mcp()
        # The same unstartable-command prune the wire array gets
        # (ACP_BACKENDS_PRUNE_UNSTARTABLE_MCP): Copilot CLI drops such a server
        # with a log line and nothing on the wire, so it is left out here and named
        # on the session's MCP report when the wire array is pruned the same way.
        kept, _pruned = await asyncio.to_thread(prune_unstartable, session._session_mcp_servers())
        document = copilot_mcp_config(kept)
        # A respawn writes a fresh file; one an earlier spawn of this session never
        # got to discard goes first.
        await session._discard_copilot_mcp_config()
        if document is not None:
            try:
                path = await asyncio.to_thread(write_copilot_mcp_config, document)
            except OSError as exc:
                raise AcpError(
                    "Kiro Crew could not write the MCP server list this Copilot CLI "
                    f"session starts with ({exc}). Fix the permissions or free space "
                    "under the Kiro Crew config directory and start a new chat.",
                    transient=False,
                    user_worded=True,
                ) from exc
            session._copilot_mcp_config_path = path
            # Appended AFTER the shared resolution, so the spawn label stays the
            # harness plus its ACP arguments and carries no per-session path.
            argv = [*argv, _ADDITIONAL_MCP_CONFIG_ARG, _FILE_VALUE_PREFIX + path]
        # The same refuse-then-mask preflight the other enforced hosts run, keyed on
        # the routing question rather than on this harness's identity. ACP v1 cannot
        # make Copilot CLI ask about a passive read inside its own directory, so the
        # OS credential mask is the compensating control, as it is for Codex.
        try:
            hidden = await launch_mod._run_preflight_bounded(
                launch_mod._sandbox_preflight, self.backend, ctx.sandbox_mode
            )
        except BaseException:
            # A refused spawn never starts a child to read the file, and it carries
            # a session credential, so it does not wait for a later sweep.
            await session._discard_copilot_mcp_config()
            raise
        return SpawnPlan(
            argv=argv,
            spawn_label=spawn_label,
            stderr_label=stderr_label,
            extra_hidden_dirs=hidden,
            # The launch config is handed back to THIS child alone; every other
            # sandbox masks the directory it lives in.
            extra_expose_files=(
                (session._copilot_mcp_config_path,) if session._copilot_mcp_config_path else ()
            ),
        )

    def apply_spawn_env(self, env: dict[str, str], *, spawned_binary: str | None = None) -> None:
        """Drop the permission sources Copilot CLI would otherwise read above the session."""
        for name in COPILOT_DROPPED_ENV:
            env.pop(name, None)
