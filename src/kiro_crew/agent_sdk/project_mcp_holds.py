"""Hold back the project MCP servers the person has not approved, at session start.

:func:`held_project_servers` answers which servers of the session folder's own MCP
file this session's host would launch and the record
(:mod:`kiro_crew.project_mcp_approvals`) does not approve. Its three readers ask it
the same way, so they agree:

* the shared runtime (kiro-cli, KAS) adds a server with no tools under each held
  name to the session-level array (:func:`with_held_servers`). That array outranks a
  same-named workspace ``mcp.json`` entry on both hosts, so the file's launch does
  not run;
* a Claude Code session names them in ``disabledMcpjsonServers`` only in its inline
  session settings, which Claude Code reads as rejected; the shared seed carries
  no project-server holds;
* the chat raises one ``mcp.project_server`` card per held server
  (``dashboard.project_mcp_cards``), whose Apply records it.

A file the settings reader refuses is still read for its server entries
(:func:`read_server_entries`), and those servers are held by name like any other.
A file that exists but cannot be read at all is held whole (:func:`project_hold`): a Claude
Code session runs with ``strictMcpConfig``, so no ``.mcp.json`` server starts, and a
kiro-cli or KAS chat, whose host reads the file itself, shows one notice naming it.
A host that reads neither file holds nothing.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKENDS_SEED_LOCAL_SETTINGS,
)
from kiro_crew.agent_sdk.mcp_json_reach import _MCP_JSON_HOSTS
from kiro_crew.mcp_cleanup import include_mcp_json
from kiro_crew.project_mcp_approvals import FolderCheck, ProjectServer, check_folder, hold_files
from kiro_crew.project_mcp_files import CLAUDE_PROJECT_MCP_FILE, KIRO_PROJECT_MCP_FILE

logger = logging.getLogger(__name__)

#: The hosts that read ``<folder>/.kiro/settings/mcp.json`` for an agent that opts in:
#: the same set :func:`kiro_crew.agent_sdk.mcp_json_reach.includes_mcp_json` answers for.
_READS_KIRO_PROJECT_FILE = _MCP_JSON_HOSTS
#: The largest project MCP file :func:`read_server_entries` reads; a larger one is
#: held whole, as a file that cannot be read.
NAMES_READ_MAX_BYTES = 256 * 1024 * 1024


def read_settings_file(path: Path) -> dict[str, Any]:
    """*path* read through the credential-gated reader every session-start read uses."""
    from kiro_crew.acp.session_mcp import _read_mcp_settings

    return _read_mcp_settings(path)


def read_server_entries(folder: str, path: Path, refusal: BaseException) -> dict[str, Any] | None:
    """The ``mcpServers`` objects of a project file :func:`read_settings_file` refused.

    Read only when *path* is a regular file, not a link, that resolves inside
    *folder* and that a fresh file-safety screen accepts. Up to
    :data:`NAMES_READ_MAX_BYTES` are read and parsed as
    JSON. Returns
    ``{"mcpServers": {name: entry}}`` with each string name whose entry is an
    object, or ``None`` when the file is not read or is not such a document.
    Blocking.
    """
    import codecs
    import json

    from kiro_crew.hooks import _opened_file_matches_validated_path, validate_file_path
    from kiro_crew.jsonl_util import open_regular_nofollow

    try:
        if path.is_symlink():
            return None
    except OSError:
        return None
    canonical = validate_file_path(str(path))
    root = os.path.realpath(folder)
    if canonical is None:
        return None
    try:
        inside = canonical != root and os.path.commonpath([root, canonical]) == root
    except ValueError:  # another drive on Windows
        inside = False
    if not inside:
        return None

    def authorize(fd: int) -> None:
        if not _opened_file_matches_validated_path(fd, canonical):
            raise PermissionError("opened file no longer matches the screened path")

    try:
        with open_regular_nofollow(
            canonical, max_bytes=NAMES_READ_MAX_BYTES, authorize=authorize
        ) as handle:
            raw = handle.read()
        doc = json.loads(raw.removeprefix(codecs.BOM_UTF8).decode("utf-8", errors="replace"))
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(doc, dict):
        return None
    declared = doc.get("mcpServers", {})
    if not isinstance(declared, dict):
        return None
    entries = {k: v for k, v in declared.items() if isinstance(k, str) and isinstance(v, dict)}
    return {"mcpServers": entries}


def _reads_kiro_file(backend: str, agent: str, spec: Any) -> bool:
    from kiro_crew.agent_sdk.mcp_json_reach import includes_mcp_json

    if backend == ACP_BACKEND_KIRO:
        # kiro-cli reads an unstated opt-in as true, and a spec Crew cannot read
        # may still be one kiro-cli loads, so both hold.
        if not isinstance(spec, dict):
            return True
        stated = include_mcp_json(spec)
        return True if stated is None else stated
    if includes_mcp_json(spec, backend):
        return True
    if not (backend == ACP_BACKEND_KAS and isinstance(spec, dict)):
        return False
    # KAS's agent files from before the opt-in rule keep loading mcp.json.
    from kiro_crew.acp.kas_mcp_json_legacy import legacy_mcp_json
    from kiro_crew.agent import kiro_agents_dir_path

    return legacy_mcp_json(agent, kiro_agents_dir_path(), spec)


def held_project_servers(
    backend: str,
    agent: str | None,
    work_dir: str | Path | None,
    *,
    registered_agents: list[dict[str, Any]] | None = None,
) -> list[ProjectServer]:
    """The project servers this session's host would launch unapproved. Blocking."""
    return project_hold(backend, agent, work_dir, registered_agents=registered_agents).pending


def project_hold(
    backend: str,
    agent: str | None,
    work_dir: str | Path | None,
    *,
    registered_agents: list[dict[str, Any]] | None = None,
) -> FolderCheck:
    """:func:`held_project_servers`, plus the project file this host reads that is held whole.

    Blocking. Never raises, so a session always starts: a failure holds the file.
    """
    if not work_dir:
        return FolderCheck()
    if backend in ACP_BACKENDS_SEED_LOCAL_SETTINGS:
        file = CLAUDE_PROJECT_MCP_FILE
    elif backend in _READS_KIRO_PROJECT_FILE and agent:
        file = KIRO_PROJECT_MCP_FILE
    else:
        return FolderCheck()
    try:
        found = check_folder(work_dir, files=(file,))
        # The spec is read only for a file that holds something: most folders have none.
        if file == CLAUDE_PROJECT_MCP_FILE or not (found.pending or found.unreadable):
            return found
        if backend == ACP_BACKEND_KAS and registered_agents is not None:
            from kiro_crew.acp.kas_permissions import CREW_SETTINGS_MCP_KEY

            # The registered batch carries each agent's opt-in and declarations.
            readers = []
            for entry in registered_agents:
                stamp = entry.get(CREW_SETTINGS_MCP_KEY)
                if isinstance(stamp, dict) and (
                    stamp.get("optedIn") is True or stamp.get("legacy") is True
                ):
                    own = entry.get("mcpServers")
                    readers.append(
                        set(stamp.get("declared", ())) | set(own if isinstance(own, dict) else ())
                    )
            return FolderCheck(
                [s for s in found.pending if any(s.name not in own for own in readers)],
                found.unreadable if readers else (),
            )
        from kiro_crew.acp.session_mcp import agent_spec_snapshot

        spec: dict[str, Any] | None
        if backend == ACP_BACKEND_KAS:
            from kiro_crew.acp.harness.kas import resolve_projected_spec
            from kiro_crew.config.paths import kiro_agents_dir

            spec, _ = resolve_projected_spec(kiro_agents_dir(), agent or "", work_dir)
        else:
            spec = agent_spec_snapshot(agent, work_dir=work_dir)
        if not _reads_kiro_file(backend, agent or "", spec):
            return FolderCheck()
        own = spec.get("mcpServers") if isinstance(spec, dict) else None
        # A name the spec defines itself is the spec's entry, which the person wrote.
        defined = set(own) if isinstance(own, dict) else set()
        return FolderCheck([s for s in found.pending if s.name not in defined], found.unreadable)
    except Exception as exc:  # noqa: BLE001 - a check must never stop a session starting
        logger.warning("project MCP hold: check failed; holding the file", exc_info=True)
        return hold_files(os.fspath(work_dir), (file,), exc)


def unreadable_notice(backend: str, hold: FolderCheck) -> str:
    """The chat's notice for a file *hold* keeps whole on a host that reads it itself.

    kiro-cli and KAS read ``.kiro/settings/mcp.json`` themselves, so a file Crew
    cannot read holds no name; the notice names it and the way forward. ``""``
    when there is none to show.
    """
    if hold.unreadable and backend in _READS_KIRO_PROJECT_FILE:
        return (
            f"Kiro Crew can't read {hold.unreadable[0]}, so its MCP servers may start "
            "without asking. Fix or delete that file, then start a new chat."
        )
    return ""


def _held_element(name: str, agent: str, work_dir: str | Path) -> dict[str, Any] | None:
    from kiro_crew.acp.session_mcp import acp_server_element
    from kiro_crew.mcp_gateway.rewriter_spec import _refusing_stub_entry

    entry = _refusing_stub_entry(server_name=name, agent_name=agent, work_dir=Path(work_dir))
    return acp_server_element(name, {k: entry[k] for k in ("command", "args", "env")})


def server_name_notice(server: ProjectServer) -> str:
    """Name the file when transport framing would change a server's name."""
    from kiro_crew.acp.transport_framing import _repair_lone_surrogates

    if _repair_lone_surrogates(server.name) != server.name:
        return (
            f"{server.path} names an MCP server whose name Kiro Crew can't use. "
            "Rename it in that file, then start a new chat."
        )
    return ""


def unsendable_project_server(backend: str, agent: str | None, work_dir: str | Path | None) -> str:
    """The refusal for a name the host reads but Crew cannot send unchanged. Blocking.

    Check approved names too. KAS reads its project file regardless of the agent's
    opt-in; kiro-cli honours that opt-in. Unreadable files keep their existing hold.
    Claude callers may keep starting when strictMcpConfig excludes the file.
    """
    from kiro_crew.project_mcp_approvals import _file_servers

    if not work_dir:
        return ""
    if backend in ACP_BACKENDS_SEED_LOCAL_SETTINGS:
        file = CLAUDE_PROJECT_MCP_FILE
    elif backend in _READS_KIRO_PROJECT_FILE:
        file = KIRO_PROJECT_MCP_FILE
    else:
        return ""
    try:
        entries = _file_servers(os.fspath(work_dir), file)
        if not entries:
            return ""
        if backend == ACP_BACKEND_KIRO:
            from kiro_crew.acp.session_mcp import agent_spec_snapshot

            try:
                spec = agent_spec_snapshot(agent, work_dir=work_dir)
            except Exception:  # noqa: BLE001 - an unreadable spec may still read the file
                spec = None
            if not _reads_kiro_file(backend, agent or "", spec):
                return ""
        for name in entries:
            notice = server_name_notice(ProjectServer(os.fspath(work_dir), file, name, {}, ""))
            if notice:
                return notice
    except Exception:  # noqa: BLE001 - unreadable files retain their existing hold
        logger.debug("project MCP name check failed", exc_info=True)
    return ""


def held_server_element(
    server: ProjectServer, agent: str, work_dir: str | Path
) -> tuple[dict[str, Any], str]:
    """Hold one name independently; a failed stub still overrides the file's launch."""
    import sys

    notice = server_name_notice(server)
    try:
        element = _held_element(server.name, agent, work_dir)
        if element is not None:
            return element, notice
    except Exception:  # noqa: BLE001 - one stub must not remove another server's hold
        logger.warning("project MCP hold: could not build a refusal stub", exc_info=True)
    # The host still merges by the exact name. An isolated, empty Python program
    # exits without loading the target or Crew; MCP initialization fails for this
    # server only, rather than restoring the file's unchecked launch.
    return {
        "name": server.name,
        "command": sys.executable,
        "args": ["-I", "-c", ""],
        "env": [],
        "type": "stdio",
    }, notice or (
        f"Kiro Crew couldn't prepare an MCP server in {server.path}. "
        "Check that file and your Kiro Crew installation, then start a new chat."
    )


def with_held_servers(
    servers: list[dict[str, Any]],
    held: list[ProjectServer],
    agent: str,
    work_dir: str | Path,
) -> list[dict[str, Any]]:
    """*servers* plus a server with no tools for each held name the array lacks.

    A name the array already carries is Kiro Crew's own element, which outranks the
    file's entry already. Pure apart from building the stub's launch line.
    """
    present = {e.get("name") for e in servers if isinstance(e, dict)}
    added: list[dict[str, Any]] = []
    for server in held:
        if server.name in present:
            continue
        element, _ = held_server_element(server, agent, work_dir)
        added.append(element)
        present.add(server.name)
    if added:
        from kiro_crew.acp.mcp_session_report import NAME_CAP, sanitize_sink_text

        # A name comes from a file a checkout can supply, so each is sanitized.
        names = sorted(sanitize_sink_text(str(e["name"]), NAME_CAP) for e in added)
        logger.info(
            "project MCP hold: %d server(s) in %s wait for approval: %s",
            len(added),
            KIRO_PROJECT_MCP_FILE,
            ", ".join(names),
        )
    return [*servers, *added]


async def hold_project_servers(
    runtime: Any,
    servers: list[dict[str, Any]],
    agent: str,
    work_dir: str | Path,
    *,
    registered_agents: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """The shared runtime's session array with the held project servers added.

    *runtime* is the ``AcpRuntime`` starting or loading the session. Its file reads
    run off the event loop.
    """
    import asyncio

    backend = runtime.acp_backend
    if backend not in _READS_KIRO_PROJECT_FILE:
        return servers
    params = await asyncio.to_thread(
        _apply_session_hold,
        backend,
        agent,
        {"cwd": work_dir, "mcpServers": servers},
        direct=False,
        registered_agents=registered_agents,
    )
    return params["mcpServers"]


async def hold_session_params(
    backend: str, agent: str | None, params: dict[str, Any]
) -> dict[str, Any]:
    """Hold project servers in direct-client sessions, model probes and parking loads.

    Use the request's folder, not the process's launch folder. Ordinary runtime
    sessions apply their hold before KAS hoisting and remote-workspace rewriting.
    File reads and stub construction stay off-loop.
    """
    import asyncio

    if backend not in _READS_KIRO_PROJECT_FILE | ACP_BACKENDS_SEED_LOCAL_SETTINGS:
        return params
    work_dir = params.get("cwd")
    if not work_dir:
        return params

    return await asyncio.to_thread(_apply_session_hold, backend, agent, params)


def _apply_session_hold(
    backend: str,
    agent: str | None,
    params: dict[str, Any],
    *,
    direct: bool = True,
    registered_agents: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build both session envelopes through the same per-server hold path. Blocking."""
    work_dir = params["cwd"]
    notice = unsendable_project_server(backend, agent, work_dir)
    options = ((params.get("_meta") or {}).get("claudeCode") or {}).get("options") or {}
    strict = backend in ACP_BACKENDS_SEED_LOCAL_SETTINGS and options.get("strictMcpConfig") is True
    if notice and not strict:
        from kiro_crew.acp.transport_errors import AcpToolGateUnroutable

        raise AcpToolGateUnroutable(notice, transient=False, user_worded=True)
    if notice:
        return params  # Claude's strict session never reads the project file.
    file = (
        CLAUDE_PROJECT_MCP_FILE
        if backend in ACP_BACKENDS_SEED_LOCAL_SETTINGS
        else KIRO_PROJECT_MCP_FILE
    )
    try:
        if backend == ACP_BACKEND_KAS and direct:
            # Probes and parking loads can run the engine's default or stored
            # agent. The runtime's named agent cannot exempt these requests.
            hold = check_folder(work_dir, files=(file,))
        else:
            hold = project_hold(backend, agent, work_dir, registered_agents=registered_agents)
    except Exception as exc:  # noqa: BLE001 - a failed check holds all recoverable names
        logger.warning("project MCP hold: check failed; holding the file", exc_info=True)
        hold = hold_files(os.fspath(work_dir), (file,), exc)
    if backend in _READS_KIRO_PROJECT_FILE:
        servers = with_held_servers(
            params.get("mcpServers", []), hold.pending, agent or "", work_dir
        )
        return {**params, "mcpServers": servers}
    meta = dict(params.get("_meta") or {})
    claude = dict(meta.get("claudeCode") or {})
    options = dict(claude.get("options") or {})
    settings = dict(options.get("settings") or {})
    if hold.pending:
        rejected = set(settings.get("disabledMcpjsonServers") or ())
        settings["disabledMcpjsonServers"] = sorted(rejected | {s.name for s in hold.pending})
        options["settings"] = settings
    if hold.unreadable:
        options["strictMcpConfig"] = True
    if not (hold.pending or hold.unreadable):
        return params
    return {**params, "_meta": {**meta, "claudeCode": {**claude, "options": options}}}
