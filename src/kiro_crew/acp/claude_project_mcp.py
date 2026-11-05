"""The project ``.mcp.json`` servers a claude session must not start unapproved.

Claude Code reads every ``.mcp.json`` from the session's working directory up to, not
including, the filesystem root as project-scope MCP servers. Its interactive CLI asks
before it starts one; a non-interactive session, which is what claude-agent-acp runs,
approves each pending server by itself when the ``project`` setting source is on. A
cloned repository therefore starts its own server commands the moment a Crew chat opens
in it.

``disabledMcpjsonServers`` is the one key Claude Code checks before any approval, and it
matches by name. So each session's inline settings list every such name except the
ones the user approved in the user ``settings.json`` of the config directory the claude
child reads (``enabledMcpjsonServers``, or all of them under
``enableAllProjectMcpServers: true``), the place Claude Code's own hint names for a
lasting approval. The seed file does not carry the list, so the user's own ``claude``
in that work dir shows its own approval dialog. An approval matches only the exact
name. Crew also reads the root's ``.mcp.json``, which only adds names, and skips the
child home's own one, found by resolved path (Crew writes its discovered servers there,
and no repository can), unless the work dir is the home dir. When Crew cannot learn
every name, or the names to list pass what one settings argument holds, it reports that
instead, and the session then mounts only Crew's own servers (``strictMcpConfig``). The
walk keeps only the names the list carries and stops at that size, so a tree of large
files is never held at once.

The ``.mcp.json`` files are attacker-controlled. They are read bounded and
non-blocking, a link is followed one hop at a time and only to a path that is not
sensitive and does not depend on the process resolving it (Claude Code follows it in its
own process), and only the keys of the top-level ``mcpServers`` object are taken.
Nothing read here is logged except the path.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Mapping, NamedTuple

from kiro_crew.acp.claude_settings_sources import (
    claude_config_dir,
    claude_home,
    read_claude_settings_bytes,
    read_user_settings,
)
from kiro_crew.security import is_sensitive_path
from kiro_crew.user_json import loads_user_json

logger = logging.getLogger(__name__)

#: The settings key Claude Code checks before every project-server approval.
DISABLED_KEY = "disabledMcpjsonServers"

_MCP_JSON = ".mcp.json"

#: Ceiling on the list as JSON with every non-ASCII character escaped, encoded as a
#: JSON string once more. The SDK hands ``options.settings`` to Claude Code as one
#: ``--settings <json>`` argument: Linux refuses one argument over 128 KiB, and
#: Windows a whole command line over 32,767 characters, Crew's ``--mcp-config``
#: included. The second encoding escapes every ``"`` and ``\``, so it is never
#: shorter than Windows' quoting of the argument, nor than its UTF-8 bytes or UTF-16
#: units. Over it the session mounts only Crew's own servers.
_DISABLED_NAMES_MAX_BYTES = 16 * 1024

#: Links followed in one chain before it counts as a loop (Linux's own limit).
_MAX_LINK_HOPS = 40

#: A link into these means something different in Crew's process and in Claude
#: Code's (``/proc/self/cwd`` is each process's own working directory). ``/dev/stdin``
#: and its siblings are links that lead into one of them.
_PROCESS_RELATIVE_DIRS = ("/proc/", "/dev/fd/")

#: Claude Code turns a one-name string value into a list when it matches this.
_SINGLE_NAME = re.compile(r"[A-Za-z0-9_-]+")

#: Claude Code compares a disabled name that starts with this to the key exactly,
#: not after mapping characters to ``_``.
_PLUGIN_PREFIX = "plugin:"

#: The child-environment names the gate reads. Windows matches an environment name
#: in any case.
_GATE_ENV_NAMES = ("CLAUDE_CONFIG_DIR", "HOME", "USERPROFILE")


class ProjectMcpGate(NamedTuple):
    """What a session's settings must carry to keep unapproved project servers off."""

    #: The names for ``disabledMcpjsonServers``, sorted.
    disabled: list[str]
    #: True when some ``.mcp.json`` could not be read, or the names are too many to send.
    unlisted: bool
    #: The ``.mcp.json`` files Crew could not read or that name a server the list keeps
    #: off.
    sources: tuple[Path, ...] = ()
    #: The config directory whose ``settings.json`` holds the approvals.
    config_dir: Path | None = None


def _process_relative(path: Path) -> bool:
    text = f"{path.as_posix()}/"
    if text.startswith("/"):
        # pathlib keeps a leading "//", which the kernel reads as "/".
        text = "/" + text.lstrip("/")
    return any(text.startswith(prefix) for prefix in _PROCESS_RELATIVE_DIRS)


def _link_target(path: Path) -> Path | None:
    """Where the link at the absolute *path* leads, resolved one link at a time.

    None when any step lands under a process-relative directory, a link cannot be read,
    or the chain does not end.
    """
    pending = list(reversed(path.parts))
    current = Path(pending.pop())
    hops = 0
    while pending:
        part = pending.pop()
        candidate = current.parent if part == ".." else current / part
        try:
            link = os.readlink(candidate) if candidate.is_symlink() else None
        except OSError:
            return None
        if link is None:
            current = candidate
        else:
            hops += 1
            if hops > _MAX_LINK_HOPS:
                return None
            # An absolute link restarts at the root; a relative one at its directory.
            parts = Path(os.path.join(current, link)).parts
            current = Path(parts[0])
            pending.extend(reversed(parts[1:]))
        if _process_relative(current):
            return None
    return current


def _mcp_json_names(path: Path) -> set[str] | None:
    """The server names in one ``.mcp.json``; None when they cannot be learned."""
    try:
        linked = path.is_symlink()
    except OSError:
        return None
    target = path
    if linked:
        resolved = _link_target(path)
        if resolved is None or is_sensitive_path(str(resolved)):
            return None
        target = resolved
    try:
        raw = read_claude_settings_bytes(target, follow_links=False)
    except FileNotFoundError:
        # A link Crew cannot follow to a file may still reach one for Claude Code.
        return None if linked else set()
    if raw is None:
        return None
    try:
        # Claude Code decodes the file as UTF-8 with replacement characters.
        data = loads_user_json(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        # Claude Code starts nothing from a file it cannot parse.
        return set()
    except (RecursionError, ValueError):
        # Valid JSON Python refuses (an integer of more than 4,300 digits, deep
        # nesting), which Claude Code still reads.
        return None
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        return set()
    if any(name.startswith(_PLUGIN_PREFIX) and _as_sent(name) != name for name in servers):
        # The frame carries U+FFFD for the surrogate, and Claude Code compares a
        # ``plugin:`` name exactly, so the listed name would not match the key.
        return None
    return set(servers)


def _as_sent(name: str) -> str:
    """*name* as the frame carries it: each lone surrogate is U+FFFD (``encode_frame``)."""
    return name.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")


def _real(path: Path) -> str | None:
    """*path* resolved, or None when it cannot be."""
    try:
        return os.path.realpath(path)
    except (OSError, ValueError):
        return None


def _search_dirs(work_dir: Path) -> list[Path]:
    """The work dir and each parent, along both the given and the resolved path."""
    dirs: list[Path] = []
    starts = [Path(os.path.abspath(work_dir))]
    try:
        starts.append(Path(os.path.realpath(work_dir)))
    except (OSError, ValueError):
        pass
    for start in starts:
        for directory in (start, *start.parents):
            if directory not in dirs:
                dirs.append(directory)
    return dirs


def _user_names(settings: dict[str, Any], key: str) -> set[str]:
    """The string names under *key* of the user's settings, as Claude Code reads them."""
    value = settings.get(key)
    if isinstance(value, str) and _SINGLE_NAME.fullmatch(value):
        return {value}
    if isinstance(value, list):
        return {item for item in value if isinstance(item, str)}
    return set()


def _sources(*groups: list[Path]) -> tuple[Path, ...]:
    """The paths of *groups*, each file once: the walk can reach a directory two ways."""
    by_file: dict[str, Path] = {}
    for group in groups:
        for path in group:
            by_file.setdefault(os.path.realpath(path.parent), path)
    return tuple(by_file.values())


def _too_many(work_dir: Path, sources: tuple[Path, ...], config_dir: Path | None) -> ProjectMcpGate:
    """The unlisted gate for names past the cap, logged once without a name."""
    logger.warning(
        "the .mcp.json files above %s name more MCP servers than Crew writes into its settings.",
        work_dir,
    )
    return ProjectMcpGate([], True, sources, config_dir)


def project_mcp_gate(
    work_dir: Path, *, config_dir: Path | None, home: Path | None = None
) -> ProjectMcpGate:
    """The ``disabledMcpjsonServers`` list a session in *work_dir* needs.

    *config_dir* is the claude child's config directory, whose user ``settings.json``
    holds the approvals; None approves nothing. A project name the user also disabled
    there stays listed. *home*'s own ``.mcp.json`` is skipped unless *home* is the work
    dir. The walk keeps only the names the list carries and the files that name one,
    and returns unlisted as soon as those names pass ``_DISABLED_NAMES_MAX_BYTES``,
    before it reads another file. Blocking (reads files); callers run it off the loop.
    """
    user = (read_user_settings(config_dir) if config_dir is not None else None) or {}
    # Under enableAllProjectMcpServers the user approved every name, known or not.
    approve_all = user.get("enableAllProjectMcpServers") is True
    approved = set() if approve_all else _user_names(user, "enabledMcpjsonServers")
    user_disabled = _user_names(user, DISABLED_KEY)
    # The home dir by its resolved path, whichever spelling the walk reaches it by.
    home_real = _real(home) if home is not None else None
    if home_real is not None and _real(work_dir) == home_real:
        home_real = None
    listed: set[str] = set()
    # The list's size as the check below measures it: 2, plus each name's own
    # double-encoded length, whose outer quotes stand for its ", " separator.
    size = 2
    listing: list[Path] = []
    unreadable: list[Path] = []
    for directory in _search_dirs(work_dir):
        if home_real is not None and _real(directory) == home_real:
            continue
        path = directory / _MCP_JSON
        names = _mcp_json_names(path)
        if names is None:
            unreadable.append(path)
            continue
        names_one = False
        for name in names:
            if (approve_all or name in approved) and name not in user_disabled:
                continue
            names_one = True
            if name in listed:
                continue
            listed.add(name)
            size += len(json.dumps(json.dumps(name)))
            if size > _DISABLED_NAMES_MAX_BYTES:
                return _too_many(work_dir, _sources(unreadable, [*listing, path]), config_dir)
        if names_one:
            listing.append(path)
    disabled = sorted(listed)
    sources = _sources(unreadable, listing)
    if len(json.dumps(json.dumps(disabled))) > _DISABLED_NAMES_MAX_BYTES:
        return _too_many(work_dir, sources, config_dir)
    return ProjectMcpGate(disabled, bool(unreadable) and not approve_all, sources, config_dir)


def _child_environ(*layers: Mapping[str, str], case_insensitive: bool) -> dict[str, str] | None:
    """*layers* merged into one environment, a later layer winning.

    With *case_insensitive*, as on Windows, a name the gate reads matches in any
    case. Spelled more than one way with different values, it is None: which one the
    child reads is not defined.
    """
    environ: dict[str, str] = {}
    for layer in layers:
        environ.update(layer)
    if case_insensitive:
        for wanted in _GATE_ENV_NAMES:
            values = {value for name, value in environ.items() if name.upper() == wanted}
            if len(values) > 1:
                return None
            if values:
                environ[wanted] = values.pop()
    return environ


def child_project_mcp_gate(
    work_dir: Path,
    *,
    agent_env: Mapping[str, str],
    extra_env: Mapping[str, str],
    case_insensitive: bool = os.name == "nt",
) -> ProjectMcpGate:
    """:func:`project_mcp_gate` against the config directory the claude child reads.

    The child's environment is the gateway's, then ``agent.env``, then the session's
    ``extra_env``, the order the launch applies them in. One the child may read two
    ways names no config directory and no home.
    """
    environ = _child_environ(os.environ, agent_env, extra_env, case_insensitive=case_insensitive)
    if environ is None:
        return project_mcp_gate(work_dir, config_dir=None)
    return project_mcp_gate(
        work_dir, config_dir=claude_config_dir(environ), home=claude_home(environ)
    )


def _files_and_settings(gate: ProjectMcpGate | None) -> tuple[str, str]:
    """The gate's ``.mcp.json`` paths and the approvals file, for a log line."""
    files = ", ".join(str(path) for path in gate.sources) if gate else ""
    config = gate.config_dir if gate else None
    return (
        files or "the project .mcp.json files",
        str(config / "settings.json") if config else "the claude config directory's settings.json",
    )


def log_strict_session(gate: ProjectMcpGate | None, *, old_adapter: bool) -> None:
    """Warn that a session goes strict because of *gate*, naming paths, never names."""
    files, settings = _files_and_settings(gate)
    if gate is not None and old_adapter and not gate.unlisted:
        why = (
            f"{files} hold project MCP servers that are not approved, and this "
            "claude-agent-acp is too old to carry the list that keeps them off. Upgrade "
            f"it, or approve the servers in enabledMcpjsonServers of {settings}"
        )
    else:
        why = (
            f"Crew cannot list every project MCP server in {files}. Remove or fix a file "
            f"you did not write. enableAllProjectMcpServers: true in {settings} keeps "
            "your own servers on, but starts every project server in every repository"
        )
    logger.warning(
        "this claude session mounts only Crew's own MCP servers (strictMcpConfig), so "
        "the user's own and plugin MCP servers are off too: %s.",
        why,
    )


def log_disabled_servers(gate: ProjectMcpGate) -> None:
    """Say that *gate* keeps project servers off, naming paths, never names."""
    files, settings = _files_and_settings(gate)
    logger.warning(
        "project MCP servers in %s are off in this claude session because the user has "
        "not approved them. To start them, list them in enabledMcpjsonServers, or set "
        "enableAllProjectMcpServers: true, in %s; either applies in every repository.",
        files,
        settings,
    )
