"""Which launch a standing MCP tool grant approves: bound grants (stage 2), observed ones (stage 1).

A grant in ``hooks.auto_approve_tool_identities`` or an ``auto_approve_tools``
pattern names a server by NAME (``@server/tool``). Any server mounted under that
name used to get the grant.

Stage 2 (bound grants). The approval card's "Always allow this tool" and the MCP
tool list's toggle record, in the same locked write as the grant, the launch ids
of every definition of that server the granting session could see
(``hooks.auto_approve_tool_identity_launches``, :func:`session_launch_ids`). A
grant with that record approves a call only when the session's launch ids for
the server are all in it (:func:`bound_launch_holds`); otherwise the gate gives
no verdict and the call asks. The record lives in ``config.json``, whose
security keys an agent cannot change without the owner applying it
(``config.loosening_provenance``). That check runs on the gate's path, for a
bound grant only: the reads are the bounded, non-blocking, stat-cached settings
reads below and the agent-spec reader's own directory-revision memo.

Stage 1 (unbound grants, observe only). A grant with no record -- every grant
written before stage 2, and every ``auto_approve_tools`` pattern -- approves as
before. When it approves a call, the launch ids are compared with the first set
seen for the name in this process; a new id is logged and SEL-audited once, by
count and grant kind only (no server or tool names, the way ``hook_manager``
audits grant-set changes). That path NEVER changes a verdict and never touches
the filesystem on the gate's own path: the gate only queues the call
(``observe_granted_call``), and one daemon thread does the reads. A full queue
drops the observation; a read that hangs stalls only that thread.

shortcut: the session's launch is read from the definitions it can see, not
recorded from the mount it made, and the project folder is known only where the
session stamps it (``spec_write_cwd``: KAS and claude); elsewhere project-level
files are not read. Upgrade with a session-recorded mount.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import stat
import threading
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Largest settings file read; a bigger one reads as no servers.
_MAX_FILE_BYTES = 1 << 20
#: Server names kept in the first-seen map; past this, new names are not tracked.
_MAX_SERVERS = 512
#: Reported (server, launch) pairs kept; past this, a new pair is not reported.
_MAX_REPORTED = _MAX_SERVERS * 4
#: Observations waiting for the worker; past this, new ones are dropped.
_MAX_PENDING = 64
#: Settings files whose parsed servers are kept, keyed by stat.
_MAX_CACHED_FILES = 64

_LOCK = threading.Lock()
_FIRST_SEEN: dict[str, frozenset[str]] = {}
_REPORTED: set[tuple[str, str]] = set()
_FILE_CACHE: dict[str, tuple[tuple[int, int], dict[str, Any]]] = {}
#: Observations each cap dropped, reported once per process (``_note_overflow``).
_DROPPED: dict[str, int] = {"names": 0, "reports": 0, "queued": 0}
_OVERFLOW_NOTED = False
_PENDING: queue.Queue[tuple[str, str, str, str]] = queue.Queue(maxsize=_MAX_PENDING)
_WORKER: threading.Thread | None = None


def _folds_folder(command: str, args: Iterable[str], folder: str) -> bool:
    """Whether the launch reads a path relative to its working folder.

    A command with a path separator that is not absolute (``./server``,
    ``bin/run``) is run from the folder. An argument is folded only when it is
    not absolute, not an option, and names something that exists in the folder
    (``node index.js``): ``@scope/pkg`` for ``npx`` names no file there, so a
    normal package launch keeps one id in every project.
    """
    if not os.path.isabs(command) and (os.sep in command or "/" in command):
        return True
    for arg in args:
        if not arg or arg.startswith("-") or "://" in arg or arg.startswith("~"):
            continue
        if os.path.isabs(arg):
            continue
        if folder and os.path.exists(os.path.join(folder, arg)):
            return True
    return False


def launch_id(entry: Mapping[str, Any], work_dir: str) -> str | None:
    """A stable id for one MCP server definition, or ``None`` when it has none.

    stdio: the gateway's own ``hash_command(command, args)``; when the command or
    a script argument is relative, the resolved working folder is folded in, so
    ``node index.js`` in two checkouts is two launches. HTTP/SSE: the URL. Env is
    left out on purpose: a secret rotation must not read as a new server.
    """
    from kiro_crew.mcp_cleanup import mcp_entry_is_remote
    from kiro_crew.mcp_gateway.hashing import hash_command

    command = entry.get("command")
    # A remote entry may carry a placeholder ``command``; the session connects to
    # its ``url``, so the URL is what identifies it.
    if isinstance(command, str) and command and not mcp_entry_is_remote(entry):
        raw_args = entry.get("args")
        args = [str(a) for a in raw_args] if isinstance(raw_args, list) else []
        cwd = entry.get("cwd")
        folder = work_dir if os.path.isabs(work_dir or "") else ""
        if isinstance(cwd, str) and cwd:
            cwd = os.path.expanduser(cwd)
            if os.path.isabs(cwd):
                folder = cwd
            elif folder:
                folder = os.path.join(folder, cwd)
            else:
                folder = ""
        digest = hash_command(command, args)
        if not _folds_folder(command, args, folder):
            return digest
        if not folder:
            return None
        return hashlib.sha256(f"{digest}\0{os.path.realpath(folder)}".encode()).hexdigest()
    url = entry.get("url")
    if isinstance(url, str) and url.strip():
        return hashlib.sha256(f"url\0{url.strip()}".encode()).hexdigest()
    return None


def _read_settings(path: Path, *, follow_links: bool) -> bytes | None:
    """The bytes of one settings file, read bounded and non-blocking; ``None`` if refused.

    ``O_NONBLOCK`` so a FIFO cannot block the open, a regular-file check on the
    opened descriptor, and a size cap. *follow_links* False refuses a link (a
    file a cloned project controls).
    """
    from kiro_crew.bounded_read import bounded_read_fd

    if not follow_links and path.is_symlink():
        return None
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
    if not follow_links:
        flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > _MAX_FILE_BYTES:
            return None
        data = bounded_read_fd(fd, _MAX_FILE_BYTES + 1)
        return None if len(data) > _MAX_FILE_BYTES else data
    finally:
        os.close(fd)


def _servers_in(path: Path, *, follow_links: bool) -> dict[str, Any]:
    """``mcpServers`` of one settings file, cached by stat; ``{}`` when unreadable."""
    try:
        st = path.stat()
    except OSError:
        return {}
    key = str(path)
    sig = (st.st_mtime_ns, st.st_size)
    cached = _FILE_CACHE.get(key)
    if cached is not None and cached[0] == sig:
        return cached[1]
    servers: dict[str, Any] = {}
    try:
        raw = _read_settings(path, follow_links=follow_links)
        data = json.loads(raw) if raw is not None else None
        found = data.get("mcpServers") if isinstance(data, dict) else None
        servers = found if isinstance(found, dict) else {}
    except (OSError, ValueError):
        servers = {}
    if len(_FILE_CACHE) >= _MAX_CACHED_FILES:
        _FILE_CACHE.clear()
    _FILE_CACHE[key] = (sig, servers)
    return servers


def _spec_servers(agents_dir: Path, agent: str) -> dict[str, Any]:
    """``mcpServers`` of *agent*'s spec in *agents_dir*, read the way a KAS session reads it.

    ``agent_sdk.agent_specs.load_agent_spec``: JSON or markdown, a spec declaring
    the name wins. ``{}`` when there is no readable spec.
    """
    from kiro_crew.agent_sdk.agent_specs import AgentSpecUnreadable, load_agent_spec

    if not agents_dir.is_dir():
        return {}
    try:
        spec = load_agent_spec(agents_dir, agent)
    except (AgentSpecUnreadable, OSError, ValueError):
        return {}
    found = spec.get("mcpServers") if isinstance(spec, dict) else None
    return found if isinstance(found, dict) else {}


def _definition_sources(agent: str, work_dir: str) -> list[dict[str, Any]]:
    """Every ``mcpServers`` map a session's server under one name can come from."""
    from kiro_crew.config.paths import data_home, kiro_agents_dir, project_agents_dir

    agents_dir = kiro_agents_dir()
    project = Path(work_dir) if work_dir and os.path.isabs(work_dir) else None
    safe = bool(agent) and "/" not in agent and "\\" not in agent and ".." not in agent
    sources: list[dict[str, Any]] = []
    if safe:
        if project is not None:
            sources.append(_spec_servers(project_agents_dir(project), agent))
        sources.append(_spec_servers(agents_dir, agent))
    if project is not None:
        sources.append(_servers_in(project / ".kiro" / "settings" / "mcp.json", follow_links=False))
    sources.append(_servers_in(agents_dir.parent / "settings" / "mcp.json", follow_links=True))
    sources.append(_servers_in(data_home() / "mcp.json", follow_links=True))
    return sources


def session_launch_ids(server: str, *, agent: str, work_dir: str) -> frozenset[str]:
    """Launch ids of every definition of *server* (case-folded) the session can see."""
    wanted = server.lower()
    ids: set[str] = set()
    for servers in _definition_sources(agent, work_dir):
        for name, entry in servers.items():
            if isinstance(name, str) and name.lower() == wanted and isinstance(entry, dict):
                found = launch_id(entry, work_dir)
                if found:
                    ids.add(found)
    return frozenset(ids)


def bound_launch_holds(server: str, bound: frozenset[str], *, agent: str, work_dir: str) -> bool:
    """Whether a grant bound to *bound* may approve a call to *server* (stage 2).

    True only when the session can see at least one definition of *server* and
    every launch id it sees is in *bound*. No definition, an unreadable one, or a
    failed read answers False, so the call asks. Never raises.
    """
    try:
        wanted = server.lower()
        seen = False
        for servers in _definition_sources(agent, work_dir):
            for name, entry in servers.items():
                if not (isinstance(name, str) and name.lower() == wanted):
                    continue
                found = launch_id(entry, work_dir) if isinstance(entry, dict) else None
                # A definition with no id (a relative launch with no known folder)
                # cannot be shown to be one the grant was given for.
                if found is None or found not in bound:
                    return False
                seen = True
        return seen
    except Exception:
        logger.debug("bound tool grant launch check failed", exc_info=True)
        return False


def identity_grant_approves(config: Any, call: Any, agent: str, identity_ref: str) -> bool:
    """Whether an ``auto_approve_tool_identities`` grant in *config* approves *identity_ref*.

    Called by the gate's ``operator-grants`` tier for a VERIFIED identity only. A
    BOUND grant (an exact entry with an ``auto_approve_tool_identity_launches``
    record) approves only when :func:`bound_launch_holds`; on a mismatch nothing
    approves and the call asks. An UNBOUND grant keeps the stage-1 rule:
    case-insensitive equality, observed off the gate's path. While any spelling
    of the identity is bound, no unbound spelling approves it, so a hand-written
    case variant cannot step around a binding.
    """
    bindings = config.auto_approve_tool_identity_launches
    grants = config.auto_approve_tool_identities
    bound = bindings.get(identity_ref)
    if bound is not None and identity_ref in grants:
        work_dir = str(getattr(call, "spec_write_cwd", "") or "")
        if bound_launch_holds(call.mcp_server, bound, agent=agent, work_dir=work_dir):
            return True
        logger.info(
            "a bound tool grant did not approve a call: the server's launch is not one "
            "the grant was given for, so the call asks"
        )
        return False
    folded = identity_ref.lower()
    if any(ref.lower() == folded for ref in bindings):
        return False
    if any(grant.lower() == folded for grant in grants):
        observe_granted_call(call, agent, "identity")
        return True
    return False


def observe(server: str, *, agent: str, work_dir: str, grant: str) -> None:
    """Record, never decide: note a granted call whose server launch is new for its name.

    *grant* is ``identity`` (``auto_approve_tool_identities``) or ``pattern``
    (``auto_approve_tools``). Returns nothing and never raises.
    """
    try:
        if not server:
            return
        ids = session_launch_ids(server, agent=agent, work_dir=work_dir)
        if not ids:
            return
        key = server.lower()
        with _LOCK:
            bound = _FIRST_SEEN.get(key)
            fresh: list[str] = []
            if bound is None:
                if len(_FIRST_SEEN) < _MAX_SERVERS:
                    _FIRST_SEEN[key] = ids
                else:
                    _DROPPED["names"] += 1
            else:
                fresh = [i for i in sorted(ids - bound) if (key, i) not in _REPORTED]
                if fresh and len(_REPORTED) + len(fresh) > _MAX_REPORTED:
                    _DROPPED["reports"] += 1
                    fresh = []
                _REPORTED.update((key, i) for i in fresh)
        if not fresh:
            return
        logger.warning(
            "a standing tool grant (%s) approved a call to a server whose launch "
            "differs from the first one seen under that name; observe-only, the "
            "call was approved as before",
            grant,
        )
        from kiro_crew.sel import sel

        sel().log_api_access(
            caller="hook_gate",
            operation="tool_grant_launch_mismatch",
            outcome="observed",
            source="hooks",
            resources=f"grant={grant} new_launches={len(fresh)}",
        )
    except Exception:
        logger.debug("tool grant launch audit failed", exc_info=True)


def _note_overflow() -> None:
    """Say once, with counts, that a cap stopped the audit from seeing everything.

    Runs on the worker, never the gate. Later drops are still counted in
    ``_DROPPED``; the one notice is what tells the operator the record is partial.
    """
    global _OVERFLOW_NOTED
    with _LOCK:
        if _OVERFLOW_NOTED or not any(_DROPPED.values()):
            return
        _OVERFLOW_NOTED = True
        counts = dict(_DROPPED)
    summary = " ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    logger.warning(
        "tool grant launch audit reached a cap and is no longer complete (%s); "
        "observe-only, no grant verdict changed",
        summary,
    )
    from kiro_crew.sel import sel

    sel().log_api_access(
        caller="hook_gate",
        operation="tool_grant_launch_audit_truncated",
        outcome="observed",
        source="hooks",
        resources=f"dropped {summary}",
    )


def observe_granted_call(call: Any, agent: str, grant: str) -> None:
    """Queue :func:`observe` for a call the operator-grants tier just auto-approved.

    Called by that tier (``hook_runtime.gate_tiers._tier_operator_grants``) for both grant
    lists, only when the call carries a verified MCP identity. It runs on the
    gate's path, so it does no I/O: it queues the call for the worker thread and
    returns, dropping the observation when the queue is full. Never raises.
    """
    global _WORKER
    try:
        server = str(getattr(call, "mcp_server", "") or "")
        if not server:
            return
        item = (server, agent or "", str(getattr(call, "spec_write_cwd", "") or ""), grant)
        try:
            _PENDING.put_nowait(item)
        except queue.Full:
            with _LOCK:
                _DROPPED["queued"] += 1
            return
        with _LOCK:
            if _WORKER is None or not _WORKER.is_alive():
                _WORKER = threading.Thread(target=_drain, name="grant-launch-audit", daemon=True)
                _WORKER.start()
    except Exception:
        logger.debug("tool grant launch audit not queued", exc_info=True)


def _drain() -> None:
    while True:
        server, agent, work_dir, grant = _PENDING.get()
        try:
            observe(server, agent=agent, work_dir=work_dir, grant=grant)
            _note_overflow()
        except Exception:
            logger.debug("tool grant launch audit failed", exc_info=True)
        finally:
            _PENDING.task_done()


def _wait_for_tests() -> None:
    _PENDING.join()


def _reset_for_tests() -> None:
    global _OVERFLOW_NOTED
    _PENDING.join()
    _OVERFLOW_NOTED = False
    with _LOCK:
        _FIRST_SEEN.clear()
        _REPORTED.clear()
        for k in _DROPPED:
            _DROPPED[k] = 0
        _FILE_CACHE.clear()
