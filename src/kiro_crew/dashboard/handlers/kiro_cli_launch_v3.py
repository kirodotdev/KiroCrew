"""The Kiro Agent (KAS) side of "Open in kiro-cli": kiro-cli's v3 session store.

A chat on the Kiro Agent harness runs on kiro-cli's v3 engine
(``kiro-cli acp --agent-engine v3``), which keeps each conversation in its own
folder, ``<kiro home>/sessions/<workspace hash>/sess_<uuid>/``: ``session.json``
names the agent the conversation runs (``agentMode``) and ``messages.jsonl`` is
the conversation, one record per line. ``kiro-cli chat --agent-engine v3
--resume-id <sid>`` resumes it in the terminal from any folder, with the agent
``agentMode`` names, looked up among the agent files kiro-cli loads (the user's
``~/.kiro/agents`` and the project's ``.kiro/agents``). A name it cannot find
runs kiro-cli's default agent instead. The flags on the command line choose
neither: ``--agent`` applies only to a new session.

So the open (:mod:`kiro_crew.dashboard.handlers.kiro_cli_launch`) does this
against these files:

* points ``agentMode`` at a per-open copy of the chat's agent written in the
  agent format v3 loads, with every tool pre-approval left out (or at kiro-cli's
  default agent when the chat's agent has no file), and puts it back on release;
* measures where ``messages.jsonl`` ends, and on release reads back the prompts
  and replies kiro-cli appended after that point.

The chat's own engine writes this folder, and it runs in Crew's sandbox, so a
sandboxed agent can write it too. Every read and write here goes through a
pinned descriptor of the session's own folder (:class:`PinnedDirectory`): a
folder swapped for a link, a file that is a link, a FIFO, a hard link to some
other file or one past its size cap is refused, never followed. The folder is
found by its name alone, and two folders of that name are refused rather than
guessed between, since kiro-cli's own lookup would pick one of them.
"""

from __future__ import annotations

import contextlib
import copy
import errno
import json
import logging
import os
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: v3 session ids: ``sess_`` and a UUID. Anything else is not used, whatever wrote it.
SID_RE = re.compile(
    r"^sess_[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
#: The words between the binary and ``--resume-id`` that select the v3 engine.
ENGINE_ARGS = ("--agent-engine", "v3")
#: kiro-cli v3's own default agent: what ``agentMode`` names when the chat's
#: agent has no file kiro-cli can load. Named rather than left as found, which
#: may be any agent a sandboxed agent wrote there.
DEFAULT_AGENT = "vibe"

_SESSION_FILE = "session.json"
_LOG_FILE = "messages.jsonl"
#: The largest ``session.json`` an open rewrites. It carries the chat's agents
#: in full (prompts included); kiro-cli's own are tens of KB.
_SESSION_MAX_BYTES = 8 * 1024 * 1024
#: At most this many workspace folders are looked through for a session (one
#: per folder kiro-cli ever ran in; a store with more is refused, not walked).
_WORKSPACES_MAX = 20_000


def sessions_root() -> Path:
    """``<kiro home>/sessions``, where v3 keeps its session folders."""
    from kiro_crew.config.paths import kiro_sessions_dir

    return kiro_sessions_dir().parent


def _unusable(message: str) -> OSError:
    from kiro_crew.dashboard.handlers.kiro_cli_launch import _SessionFileUnusable

    return _SessionFileUnusable(message)


@contextlib.contextmanager
def pinned_session(sid: str) -> Iterator[Any]:
    """The session folder of *sid*, pinned. Blocking.

    Raises ``FileNotFoundError`` when no workspace folder holds one,
    :class:`_SessionFileUnusable` when *sid* is not a v3 id, when two do, or
    when the one there is a link or not a folder, and ``OSError`` for a read
    error.
    """
    from kiro_crew import platform_compat

    if not SID_RE.match(sid or ""):
        raise _unusable(f"{sid!r} is not a kiro-cli v3 session id")
    root = sessions_root()
    try:
        top = platform_compat.pinned_directory(root)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise _unusable(f"{root} is not a folder Crew can read") from exc
    with top:
        names = top.names_bounded(_WORKSPACES_MAX)
        if names is None:
            raise _unusable(f"{root} holds too many workspace folders to search")
        # Counted with one ``lstat`` per workspace folder, through links, so an
        # entry of the id anywhere kiro-cli's own lookup might reach counts. Only
        # the one that is used is then opened, through the pin.
        found = [n for n in names if os.path.lexists(os.path.join(top.path, n, sid))]
        if not found:
            raise FileNotFoundError(f"no kiro-cli v3 session {sid} under {root}")
        if len(found) > 1:
            raise _unusable(f"{len(found)} folders under {root} are named {sid}")
        try:
            workspace = top.child(found[0])
        except OSError as exc:
            raise _unusable(f"{found[0]} under {root} is a link or not a folder") from exc
        with workspace:
            try:
                session = workspace.child(sid)
            except OSError as exc:
                raise _unusable(f"session {sid} under {root} is a link or not a folder") from exc
            with session:
                yield session


def _open_pinned_file(pin: Any, name: str):
    """Binary reader of regular file *name* in pinned folder *pin*
    (:class:`kiro_crew.platform_compat.PinnedDirectory`). Blocking. The open
    is the pin's own descriptor-relative one (``dir_fd`` on POSIX, by path
    under a rename-proof handle on Windows), so a name resolved from here
    cannot reach outside the folder that was pinned; it refuses a link and
    does not wait on a FIFO, and the descriptor is then checked
    (:func:`.kiro_cli_launch._checked_session_fd`)."""
    try:
        fd = pin._open_file(name)  # noqa: SLF001 - the pin's own leaf open
    except IsADirectoryError as exc:
        raise _unusable(f"{name} in {pin.path} is a folder") from exc
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EMLINK, errno.ENOTDIR):
            raise _unusable(f"{name} in {pin.path} is a link") from exc
        raise
    from kiro_crew.dashboard.handlers.kiro_cli_launch import _checked_session_fd

    return _checked_session_fd(fd, os.path.join(pin.path, name))


def _write_pinned_file(pin: Any, name: str, text: str) -> None:
    """Replace *name* in pinned folder *pin* with *text*, owner-only. Blocking.
    Through the pin's descriptor where the platform can rename through one
    (POSIX); on Windows the pin keeps the folder from being renamed or replaced
    while it is held, so its path is stable."""
    from kiro_crew import platform_compat
    from kiro_crew.atomic_write import atomic_write

    parent_fd = pin._fd if platform_compat.IS_POSIX else None  # noqa: SLF001
    atomic_write(Path(pin.path) / name, text, mode=0o600, parent_dir_fd=parent_fd)


def _json_document(raw: bytes, path: str | Path) -> object:
    """*raw* parsed as JSON. Raises ``ValueError`` for anything that is not,
    one nested past the parser's depth included: a session file is written by
    whatever runs in the chat's sandbox, and a ``RecursionError`` escaping here
    would skip every handler that puts the open back."""
    try:
        return json.loads(raw)
    except RecursionError as exc:
        raise ValueError(f"kiro-cli session file {path} is nested too deeply") from exc


def _open_entry(session: Any, name: str):
    """Binary reader of regular file *name* in the pinned *session* folder
    (:func:`_open_pinned_file`: no link, FIFO or hard link)."""
    return _open_pinned_file(session, name)


def files_resumable(sid: str) -> bool:
    """Whether *sid* is one v3 session folder holding its ``session.json`` and
    ``messages.jsonl`` as regular files. Blocking. Any doubt answers no."""
    try:
        with pinned_session(sid) as session:
            for name in (_SESSION_FILE, _LOG_FILE):
                with _open_entry(session, name):
                    pass
    except OSError:
        return False
    return True


def log_size(sid: str) -> int:
    """Where *sid*'s ``messages.jsonl`` ends now: the offset the release reads
    from. Blocking. Raises ``OSError`` when it cannot be measured; an unknown
    offset is never read as 0, which would bring the whole conversation in again."""
    with pinned_session(sid) as session, _open_entry(session, _LOG_FILE) as fh:
        return os.fstat(fh.fileno()).st_size


def _read_session(session: Any, sid: str) -> dict:
    with _open_entry(session, _SESSION_FILE) as fh:
        if os.fstat(fh.fileno()).st_size > _SESSION_MAX_BYTES:
            raise _unusable(f"{_SESSION_FILE} of {sid} is too big")
        raw = fh.read(_SESSION_MAX_BYTES + 1)
    document = _json_document(raw, os.path.join(session.path, _SESSION_FILE))
    if not isinstance(document, dict) or document.get("id") != sid:
        raise ValueError(f"{_SESSION_FILE} of {sid} does not describe that session")
    return document


def _write_session(session: Any, document: dict) -> None:
    _write_pinned_file(session, _SESSION_FILE, json.dumps(document, indent=2))


def read_session_agent(sid: str) -> dict:
    """What session *sid* runs now: the state :func:`point_session_agent`
    replaces and :func:`restore_session_agent` puts back. Blocking. Raises
    ``OSError`` or ``ValueError`` when its ``session.json`` cannot be read."""
    with pinned_session(sid) as session:
        document = _read_session(session, sid)
    return {"agentMode": document.get("agentMode")}


def point_session_agent(sid: str, agent: str | None) -> None:
    """Make session *sid* resume on agent *agent* (kiro-cli's default when
    ``None``). Blocking. Raises ``OSError`` or ``ValueError`` when its
    ``session.json`` cannot be read or written."""
    with pinned_session(sid) as session:
        document = _read_session(session, sid)
        document["agentMode"] = agent or DEFAULT_AGENT
        _write_session(session, document)


def restore_session_agent(sid: str, saved: object) -> None:
    """Put back the agent :func:`point_session_agent` replaced. Blocking.
    Raises ``OSError`` when the file cannot be read or rewritten, for a later
    retry. A session kiro-cli removed, or left as something that is not one,
    has nothing to put back."""
    if not isinstance(saved, dict) or not SID_RE.match(sid or ""):
        return
    try:
        with pinned_session(sid) as session:
            try:
                document = _read_session(session, sid)
            except FileNotFoundError:
                return
            if saved.get("agentMode") is None:
                document.pop("agentMode", None)
            else:
                document["agentMode"] = saved["agentMode"]
            _write_session(session, document)
    except FileNotFoundError:
        return
    except ValueError:
        # Not this session's ``session.json``: nothing a retry could put back.
        logger.warning("kiro-cli: session %s's session.json does not read as one", sid)
    # A folder that became a link, or gained a twin, raises
    # (:class:`_SessionFileUnusable`): the open's record is kept, and the
    # recovery that reads it says in the chat that it could not be put back.


# ── the conversation log ──────────────────────────────────────────────────────

#: A record's ``payload.type`` sits after its ``id`` and ``timestamp``, so it is
#: looked for this far into the line.
_HEAD_BYTES = 512
_TURN_KINDS = {
    b'"payload":{"type":"user"': "user",
    b'"payload": {"type": "user"': "user",
    b'"payload":{"type":"assistant"': "assistant",
    b'"payload": {"type": "assistant"': "assistant",
}


def _record_text(entry: object) -> str | None:
    """The text of a v3 log record: a prompt's, or a reply's said text. ``None``
    for a record that is no turn (the model's reasoning)."""
    payload = entry.get("payload") if isinstance(entry, dict) else None
    if not isinstance(payload, dict):
        return None
    kind = payload.get("type")
    if kind == "assistant" and payload.get("operationType") != "Say":
        return None
    content = payload.get("content")
    if kind in ("user", "assistant"):
        return content if isinstance(content, str) else ""
    return None


def log_format() -> Any:
    """How v3's ``messages.jsonl`` names its prompts and replies, for the
    shared reader (:func:`.kiro_cli_launch._read_turns`)."""
    from kiro_crew.dashboard.handlers.kiro_cli_launch import _LogFormat

    return _LogFormat(kinds=_TURN_KINDS, head_bytes=_HEAD_BYTES, text=_record_text)


def read_turns(sid: str, offset: int) -> tuple[list[tuple[str, str]], int]:
    """``(rows, left_out)`` for the prompts and replies kiro-cli appended to
    session *sid*'s log after *offset*. Blocking. A session that is gone has
    nothing to bring in; any other read error raises."""
    from kiro_crew.dashboard.handlers.kiro_cli_launch import _read_turns

    try:
        with (
            pinned_session(sid) as session,
            _open_entry(session, _LOG_FILE) as fh,
            _open_entry(session, _LOG_FILE) as peek,
        ):
            return _read_turns(
                fh,
                peek,
                os.path.join(session.path, _LOG_FILE),
                offset,
                log_format(),
            )
    except FileNotFoundError:
        return [], 0


# ── the launch copy, in the agent format v3 loads ─────────────────────────────

#: What a server entry keeps: how to start or reach it, and what switches it or
#: its tools off. ``autoApprove`` and any key added later stay out.
_SERVER_KEYS = frozenset(
    {"type", "command", "args", "env", "cwd", "url", "headers", "disabled", "disabledTools"}
)
_KEPT_EFFECTS = frozenset({"deny", "ask"})


def _narrowing_rules(block: object) -> list[dict]:
    rules = block.get("rules") if isinstance(block, dict) else None
    return [
        copy.deepcopy(rule)
        for rule in (rules if isinstance(rules, list) else ())
        if isinstance(rule, dict) and rule.get("effect") in _KEPT_EFFECTS
    ]


def launch_view(agent: str, name: str, spec: dict, agents_dir: Path) -> dict:
    """The chat's agent *agent* (its user-level *spec*) as a v3 agent file named
    *name*, with nothing pre-approved. Raises ``ValueError`` when the spec's
    prompt cannot be read.

    Kept: the prompt (read in, as Crew sends it to the engine), the tools in
    v3's own names, the excluded tools, the MCP servers without their
    ``autoApprove``, the resources, and the spec's own deny and ask rules,
    those its ``toolsSettings`` translate to included. Left out: every allow
    rule and ``allowedTools`` (v3 then asks before each tool), the workspace
    and global MCP merge and the powers (``includeMcpJson``/``includePowers``
    false), the model (the session keeps its own) and the hooks, which Crew
    runs itself for a Kiro Agent chat and which have no v3 form here.
    """
    from kiro_crew.agent_sdk.agent_specs import resolve_prompt, tools_settings_permissions
    from kiro_crew.platform.tool_names import (
        expand_kiro_cli_tool_exclusions,
        expand_kiro_cli_tool_names,
    )

    # Raises AgentSpecUnreadable, a ValueError, when the prompt cannot be read.
    prompt = resolve_prompt(spec, agent_id=agent, agents_dir=agents_dir)
    tools = spec.get("tools")
    if tools == "*" or (isinstance(tools, list) and "*" in tools):
        tools_out: str | list[str] = "*"
    else:
        tools_out = expand_kiro_cli_tool_names(
            [t for t in tools if isinstance(t, str) and t] if isinstance(tools, list) else []
        )
    view: dict[str, Any] = {
        "name": name,
        "description": (
            f"Kiro Crew's copy of {agent} for one 'Open in kiro-cli' terminal, without "
            f"its tool pre-approvals. Removed when that terminal closes; pick {agent} "
            "instead."
        ),
        "prompt": prompt,
        "tools": tools_out,
        "includeMcpJson": False,
        "includePowers": False,
    }
    excluded = spec.get("excludedTools")
    if isinstance(excluded, list):
        entries = [t for t in excluded if isinstance(t, str) and t]
        if entries:
            view["excludedTools"] = expand_kiro_cli_tool_exclusions(entries)
    servers = spec.get("mcpServers")
    if isinstance(servers, dict):
        view["mcpServers"] = {
            server: {k: copy.deepcopy(v) for k, v in entry.items() if k in _SERVER_KEYS}
            for server, entry in servers.items()
            if isinstance(server, str) and isinstance(entry, dict)
        }
    resources = spec.get("resources")
    if isinstance(resources, list):
        kept = [r for r in resources if isinstance(r, str) and r]
        if kept:
            view["resources"] = kept
    translated = tools_settings_permissions(spec.get("toolsSettings"))
    view["permissions"] = {
        "rules": [*_narrowing_rules(spec.get("permissions")), *_narrowing_rules(translated)]
    }
    return view


def workspace_config_refusal(project_dir: str) -> bool:
    """Whether the project folder holds kiro-cli v3 config the terminal would
    run. Blocking (lists and stats).

    kiro-cli v3 started in the project loads what its ``.kiro`` folder holds,
    whatever the agent says: agents (one there of the same name would replace
    the launch copy), hooks, powers and extensions, and the workspace MCP file,
    which it starts even for an agent with ``includeMcpJson`` false. The chat's
    sandboxed agent can write that folder, so anything there would put its own
    commands in this unsandboxed terminal. Refused when any of those folders
    holds anything, or cannot be read, and when a workspace MCP file is there.
    """
    kiro = os.path.join(project_dir, ".kiro")
    for folder in ("agents", "hooks", "powers", "extensions", "settings"):
        path = os.path.join(kiro, folder)
        try:
            if os.listdir(path):
                return True
        except (FileNotFoundError, NotADirectoryError):
            if os.path.lexists(path):
                return True  # a file or a dangling link where a folder belongs
        except OSError:
            return True
    return os.path.lexists(os.path.join(project_dir, ".amazonq", "mcp.json"))


__all__ = [
    "DEFAULT_AGENT",
    "ENGINE_ARGS",
    "SID_RE",
    "files_resumable",
    "launch_view",
    "log_size",
    "pinned_session",
    "point_session_agent",
    "read_session_agent",
    "read_turns",
    "restore_session_agent",
    "sessions_root",
    "workspace_config_refusal",
]
