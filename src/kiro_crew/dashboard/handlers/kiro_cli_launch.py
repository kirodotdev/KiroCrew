"""Open a chat's own kiro-cli conversation in the built-in terminal.

``GET /api/terminal/kiro-cli/{slot}`` says whether the chat can be opened in
kiro-cli and with which agent, so the chat menu can offer, explain or withhold
the action. The terminal WebSocket (``?kiro_cli_slot=<slot>``) then opens it:
:func:`prepare_kiro_cli_launch` hands the conversation over and returns the
argv the PTY runs.

Nothing the request carries reaches the command line except the chat slot's
NAME, which only selects a chat the gateway already holds. The argv is
``<kiro-cli> chat --resume-id <sid> [--agent <name>]``, built here from:

* the kiro-cli binary :func:`kiro_crew.kiro_cli.resolve_kiro_cli` finds without
  the inherited ``PATH`` (the PTY starts in the chat's project folder, so a
  relative or agent-writable ``PATH`` entry must not pick what runs);
* the kiro-cli session id the session map holds for the chat's session key,
  accepted only in kiro-cli's own UUID shape and only while its conversation
  log is on disk;
* the agent the chat's bindings resolve to, passed only when it has the plain
  name shape and an agent file kiro-cli can load. A Crew-internal agent with no
  file falls back to kiro-cli's default agent, and the availability answer says
  so.

The working directory is the chat's project folder, ``realpath``-resolved and
required to exist.

One process writes a kiro-cli session at a time. The open is refused while the
chat has a turn in flight, a queued message or sub-agent work attached; it
then records the hold (:mod:`kiro_crew.dashboard.kiro_cli_handoff`) and resets
the chat's own agent process with its resume pointer kept, under the same two
locks the reload and the switch handlers take. While the hold stands, a
dashboard turn on the chat refuses at admission and the eager pre-spawn stands
down. The terminal's teardown releases it, and the chat's next turn
``session/load``\\ s the conversation kiro-cli left.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import json
import logging
import os
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

from aiohttp import web

from kiro_crew.dashboard import kiro_cli_handoff
from kiro_crew.dashboard.handlers import terminal
from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request
from kiro_crew.dashboard.handlers.external_terminal import resolve_directory

logger = logging.getLogger(__name__)

__all__ = [
    "KIRO_CLI_REFUSAL_CODES",
    "KiroCliLaunch",
    "KiroCliPlan",
    "api_chat_slot_kiro_cli",
    "plan_kiro_cli_launch",
    "prepare_kiro_cli_launch",
]

#: kiro-cli's session ids are UUIDs. Anything else is not used, whatever wrote it.
_SID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
#: An agent name that names one file in an agents directory and nothing else.
_AGENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
#: How long the open waits for a pre-spawn it cancelled to finish unwinding.
_EAGER_SPAWN_CANCEL_WAIT_S = 5.0

# Refusal codes. The terminal socket sends one as its error ``code``; the chat
# menu reads the same code from the availability route.
CODE_UNKNOWN_CHAT = "kiro_cli_unknown_chat"
CODE_UNSUPPORTED = "kiro_cli_unsupported"
CODE_NOT_KIRO = "kiro_cli_not_kiro"
CODE_NO_SESSION = "kiro_cli_no_session"
CODE_NO_LOG = "kiro_cli_no_log"
CODE_NO_PROJECT = "kiro_cli_no_project"
CODE_NOT_INSTALLED = "kiro_cli_not_installed"
CODE_BUSY = "kiro_cli_busy"
CODE_SUBAGENTS = "kiro_cli_subagents"
CODE_ALREADY_OPEN = "kiro_cli_already_open"
CODE_REMOTE = "kiro_cli_remote"
CODE_TERMINAL_DISABLED = "kiro_cli_terminal_disabled"
CODE_PROJECT_AGENT = "kiro_cli_project_agent"
#: A kiro-cli tab redialed after its process ended: it never re-opens by itself.
CODE_ENDED = "kiro_cli_ended"

KIRO_CLI_REFUSAL_CODES: frozenset[str] = frozenset(
    {
        CODE_PROJECT_AGENT,
        CODE_ENDED,
        CODE_UNKNOWN_CHAT,
        CODE_UNSUPPORTED,
        CODE_NOT_KIRO,
        CODE_NO_SESSION,
        CODE_NO_LOG,
        CODE_NO_PROJECT,
        CODE_NOT_INSTALLED,
        CODE_BUSY,
        CODE_SUBAGENTS,
        CODE_ALREADY_OPEN,
        CODE_REMOTE,
        CODE_TERMINAL_DISABLED,
    }
)

_WHY: dict[str, str] = {
    CODE_UNKNOWN_CHAT: "This chat is not loaded",
    CODE_UNSUPPORTED: "This chat runs through a channel or an app, so it cannot open in kiro-cli",
    CODE_NOT_KIRO: "This chat does not run on kiro-cli",
    CODE_NO_SESSION: "This chat has no kiro-cli conversation yet. Send it a message first",
    CODE_NO_LOG: "This chat's kiro-cli conversation log is missing",
    CODE_NO_PROJECT: "This chat's project folder does not exist",
    CODE_NOT_INSTALLED: "kiro-cli is not installed where the gateway looks for it",
    CODE_BUSY: "This chat is busy. Wait for its turn and queued messages to finish",
    CODE_SUBAGENTS: "This chat has sub-agents working. Wait for them to finish",
    CODE_ALREADY_OPEN: "This chat is already open in kiro-cli in another terminal",
    CODE_REMOTE: "This chat runs on a remote environment",
    CODE_TERMINAL_DISABLED: "The terminal panel is turned off",
    CODE_PROJECT_AGENT: (
        "This project has its own kiro-cli agent or MCP config, which kiro-cli "
        "would run outside Crew's sandbox. Open kiro-cli there yourself if you trust it"
    ),
    CODE_ENDED: "kiro-cli has ended in this tab",
}


def _why(code: str) -> str:
    return _WHY.get(code, "This chat cannot open in kiro-cli")


@dataclass(frozen=True)
class KiroCliPlan:
    """What opening a chat in kiro-cli would run, or why it cannot."""

    code: str = ""
    session_key: str = ""
    sid: str = ""
    cwd: str = ""
    binary: str = ""
    #: The agent passed with ``--agent``; ``""`` when kiro-cli's default is used.
    agent: str = ""
    #: The chat's own agent when it has no file kiro-cli can load, else ``""``.
    agent_without_spec: str = ""

    @property
    def ok(self) -> bool:
        return not self.code

    def argv(self) -> list[str]:
        argv = [self.binary, "chat", "--resume-id", self.sid]
        if self.agent:
            argv += ["--agent", self.agent]
        return argv


@dataclass
class KiroCliLaunch:
    """A handed-over conversation: what the PTY runs, and the hold it now owns."""

    argv: list[str]
    cwd: str
    session_key: str
    slot_key: str
    terminal_id: str
    agent_without_spec: str = ""
    #: Posts a notice row to the chat (``None`` posts nothing).
    notify: Callable[[str], None] | None = field(default=None, repr=False)
    #: Appends kiro-cli's turns to the chat's transcript as ``(role, text)``.
    record_turns: Callable[[list[tuple[str, str]]], None] | None = field(default=None, repr=False)
    #: The session's kiro-cli log and its size when the terminal took it over.
    log_path: str = ""
    log_offset: int = 0
    _released: bool = field(default=False, repr=False)
    _announced: bool = field(default=False, repr=False)

    def announce(self) -> None:
        """Tell the chat kiro-cli now runs its conversation. Called by the
        terminal once the PTY is published, so a refused or failed open never
        claims anything opened; a teardown that already took the hold back
        (a DELETE landing during the publish) leaves nothing to announce."""
        if self._announced or self._released:
            return
        if kiro_cli_handoff.holder_of(self.session_key) != self.terminal_id:
            return
        self._announced = True
        if self.notify is not None:
            self.notify(_held_notice(self.agent_without_spec))

    async def settle(self) -> None:
        """Bring the turns made in kiro-cli into the chat's transcript.

        Awaited by the hold's release, before the chat may run again. The chat's
        next turn may resume by REPLAYING its own transcript into a fresh
        session (a dashboard chat with MCP Tool Search on, the default) rather
        than loading the native one, so turns that are only in kiro-cli's log
        would be lost to it. Recording them here carries them either way, and
        shows them in the chat.
        """
        if not self._announced or not self.log_path or self.record_turns is None:
            return
        turns, left_out = await asyncio.to_thread(
            _read_terminal_turns, self.log_path, self.log_offset
        )
        if left_out and self.notify is not None:
            # Said rather than dropped silently: the agent has these only if the
            # chat resumes natively, not when it replays this transcript.
            self.notify(_LEFT_OUT_NOTICE.format(count=left_out))
        if turns:
            self.record_turns(turns)

    def on_released(self) -> None:
        """The hold's release callback: say kiro-cli ended, only if it ran."""
        if self._announced and self.notify is not None:
            self.notify(_RELEASED_NOTICE)

    def release(self) -> None:
        """Give the hold back (the terminal never started, or has ended)."""
        if self._released:
            return
        self._released = True
        kiro_cli_handoff.release(self.session_key, self.terminal_id)


#: At most this many of the newest rows are recorded, each at most this long.
_TURNS_MAX_ROWS = 400
_TURN_MAX_CHARS = 100_000


def _text_parts(content: object) -> str:
    """The text parts of a kiro-cli log entry's ``content`` (thinking, tool
    calls and images are left out)."""
    if not isinstance(content, list):
        return ""
    texts = []
    for part in content:
        if (
            isinstance(part, dict)
            and part.get("kind") == "text"
            and isinstance(part.get("data"), str)
        ):
            texts.append(part["data"])
    return "\n\n".join(t for t in texts if t.strip())


#: The two entry kinds read back; every other line (tool results above all,
#: which can be megabytes) is recognised from its head and skipped unparsed.
_TURN_KINDS = {
    b'"kind":"Prompt"': "user",
    b'"kind": "Prompt"': "user",
    b'"kind":"AssistantMessage"': "assistant",
    b'"kind": "AssistantMessage"': "assistant",
}
#: How far into a line its ``kind`` is looked for.
_KIND_HEAD_BYTES = 64
#: A line is read in pieces of this size, so no line is ever held whole
#: unless it is a prompt or reply no larger than :data:`_TURN_LINE_MAX_BYTES`.
_READ_CHUNK_BYTES = 64 * 1024
_TURN_LINE_MAX_BYTES = 2 * 1024 * 1024
#: Stands in for a prompt or reply whose log line is past that cap.
_TOO_LONG_TEXT = "(A message from kiro-cli too long to add here.)"


def _log_lines(fh) -> Iterator[tuple[bytes, bool]]:
    """``(line, whole)`` for each line from *fh*'s position, holding at most
    :data:`_TURN_LINE_MAX_BYTES` of one. A line that is not a prompt or reply
    (by its head) is drained in chunks and not yielded. A complete prompt or
    reply past the cap is drained too and yields ``(head, False)``; one past
    the cap that never ended (kiro-cli stopped mid-write) yields nothing.
    ``whole`` is True for a line within the cap, including an unterminated
    last one, which then fails to parse."""
    while True:
        piece = fh.readline(_READ_CHUNK_BYTES)
        if not piece:
            return
        head = piece[:_KIND_HEAD_BYTES]
        wanted = any(marker in head for marker in _TURN_KINDS)
        parts = [piece] if wanted else []
        size = len(piece)
        ended = piece.endswith(b"\n")
        while not ended:
            piece = fh.readline(_READ_CHUNK_BYTES)
            if not piece:
                break
            ended = piece.endswith(b"\n")
            size += len(piece)
            if wanted and size <= _TURN_LINE_MAX_BYTES:
                parts.append(piece)
            else:
                parts = []  # drained, never held
        if not wanted:
            continue
        if size <= _TURN_LINE_MAX_BYTES:
            yield b"".join(parts), True  # an unterminated one fails to parse
        elif ended:
            yield head, False
        # An oversized line kiro-cli never finished writing is no message.


def _read_terminal_turns(path: str, offset: int) -> tuple[list[tuple[str, str]], int]:
    """``(rows, left_out)``: ``(role, text)`` for each prompt and reply kiro-cli
    appended to its log after *offset*, and how many older rows were left out.
    Blocking (reads the file).

    The whole delta is read in bounded chunks (:func:`_log_lines`), and only
    ``Prompt`` (the user) and ``AssistantMessage`` lines are parsed; an
    assistant reply split around tool calls is joined into one row. Tool
    results are drained unheld and cost nothing against the caps; a prompt or
    reply past :data:`_TURN_LINE_MAX_BYTES` is recorded as a short stand-in. When the delta holds more than :data:`_TURNS_MAX_ROWS` rows the NEWEST
    are kept -- the chat continues from the end of the conversation -- and the
    count of the older ones is returned for the chat to say so. A log that is
    now SHORTER than *offset* was rewritten, not appended to, so nothing is read
    rather than replaying the whole conversation twice.
    """
    rows: collections.deque[tuple[str, str]] = collections.deque(maxlen=_TURNS_MAX_ROWS)
    seen = 0
    last_role = ""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            if fh.tell() <= offset:
                return [], 0
            fh.seek(offset)
            for line, whole in _log_lines(fh):
                head = line[:_KIND_HEAD_BYTES]
                role = next((r for marker, r in _TURN_KINDS.items() if marker in head), "")
                if not role:
                    continue
                if whole:
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue  # a partial trailing line
                    data = entry.get("data") if isinstance(entry, dict) else None
                    text = _text_parts(data.get("content")) if isinstance(data, dict) else ""
                else:
                    text = _TOO_LONG_TEXT
                if not text:
                    continue
                if rows and role == last_role == "assistant":
                    rows[-1] = (role, f"{rows[-1][1]}\n\n{text}"[:_TURN_MAX_CHARS])
                else:
                    rows.append((role, text[:_TURN_MAX_CHARS]))
                    seen += 1
                last_role = role
    except OSError:
        return [], 0
    return list(rows), seen - len(rows)


def _log_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _workspace_config_refusal(project_dir: str, agent: str) -> bool:
    """Whether the project folder holds kiro-cli config this open would run.
    Blocking (stats and reads small files).

    kiro-cli started in the project loads the project's ``.kiro/agents`` before
    the user's, and the workspace MCP file for an agent that includes it. The
    chat's sandboxed agent can write that folder, so either would put its own
    commands in this unsandboxed terminal. Refused when:

    * kiro-cli picks its default agent (*agent* empty) and the project has any
      agent file, since one of them may be that default;
    * the project has a workspace MCP file (``.kiro/settings/mcp.json``, or the
      legacy ``.amazonq/mcp.json``) and the agent may include it: the default
      agent, or a user spec with ``includeMcpJson``/``useLegacyMcpJson`` set or
      unreadable.
    """
    from kiro_crew.config.paths import kiro_agents_dir

    agents_dir = os.path.join(project_dir, ".kiro", "agents")
    if not agent:
        try:
            if any(name.endswith(".json") for name in os.listdir(agents_dir)):
                return True
        except OSError:
            pass
    mcp_files = (
        os.path.join(project_dir, ".kiro", "settings", "mcp.json"),
        os.path.join(project_dir, ".amazonq", "mcp.json"),
    )
    if not any(os.path.lexists(path) for path in mcp_files):
        return False
    if not agent:
        return True
    try:
        with open(kiro_agents_dir() / f"{agent}.json", encoding="utf-8") as fh:
            spec = json.load(fh)
    except (OSError, ValueError):
        return True
    if not isinstance(spec, dict):
        return True
    return spec.get("includeMcpJson") is not False or spec.get("useLegacyMcpJson") is True


def _agent_spec_where(agent: str, project_dir: str) -> tuple[bool, bool]:
    """``(in the user's agents directory, in the project's .kiro/agents)`` for
    *agent*. Blocking (stats files).

    Only the user-level file counts as the agent's spec. kiro-cli resolves
    ``--agent <name>`` from the project folder first, and that folder is one the
    chat's sandboxed agent can write, so a project file of the same name would
    replace the user's spec -- its MCP commands and hooks then run in this
    unsandboxed terminal. The plan refuses the open in that case.
    """
    from kiro_crew.config.paths import kiro_agents_dir

    user = os.path.isfile(str(kiro_agents_dir() / f"{agent}.json"))
    project = bool(project_dir) and os.path.lexists(
        os.path.join(project_dir, ".kiro", "agents", f"{agent}.json")
    )
    return user, project


def _resolve_kiro_binary() -> str:
    from kiro_crew.kiro_cli import resolve_kiro_cli

    return resolve_kiro_cli(include_inherited_path=False) or ""


def _resolve_agent(session_key: str, slot_agent: str, agent_kind: str) -> str:
    """The kiro-cli agent the chat's bindings resolve to, or ``""``. Blocking."""
    from kiro_crew.config.loader import KiroCrewConfig, resolve_agent_bindings
    from kiro_crew.session_agent_selection import resolve_session_agent_bindings

    cfg = KiroCrewConfig.load()
    bindings = resolve_session_agent_bindings(
        resolve_agent_bindings, cfg, session_key, slot_agent or None, agent_kind=agent_kind
    )
    return str(getattr(bindings, "kiro_agent", "") or "")


def _configured_backend_is_kiro(session_key: str) -> bool:
    """Whether the chat's session would start on kiro-cli. Blocking (config read)."""
    from kiro_crew.acp_backends import ACP_BACKEND_KIRO
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.members import select_provider_backend

    cfg = KiroCrewConfig.load()
    backend = select_provider_backend(
        session_key, cfg.agent.member_acp_backend, cfg.agent.acp_backend
    )
    return backend == ACP_BACKEND_KIRO


def _files_resumable(sid: str) -> bool:
    from kiro_crew.session_map import session_files_resumable

    return session_files_resumable(sid)


def _session_map(state: object):
    return getattr(getattr(state, "sessions", None), "_session_map", None)


async def plan_kiro_cli_launch(request: web.Request, slot_name: str) -> KiroCliPlan:
    """Read-only: what opening chat *slot_name* in kiro-cli would run, or why not.

    Every input is the gateway's own: the slot it holds, its session map, its
    config and the files on disk. Run again by the open itself, inside its locks.
    """
    from kiro_crew.dashboard.chat_utils import effective_session_key
    from kiro_crew.messaging.link import is_channel_session_key
    from kiro_crew.session_map import resume_decided_by_load

    state = request.app.get("state")
    slots = getattr(state, "_slots", None) or {}
    slot = slots.get(slot_name) if isinstance(slots, dict) and isinstance(slot_name, str) else None
    if slot is None:
        return KiroCliPlan(code=CODE_UNKNOWN_CHAT)
    session_key = effective_session_key(slot)
    if (
        not session_key
        or is_channel_session_key(session_key)
        or getattr(slot, "_app", None)
        or getattr(slot, "app_agent_owner", None)
    ):
        # A channel's turns reach its session without passing the dashboard
        # admission that enforces the hold, and an app's chat runs a launch
        # copy of its agent that kiro-cli cannot name.
        return KiroCliPlan(code=CODE_UNSUPPORTED, session_key=session_key)
    why, _placement_code = await terminal._local_terminal_refusal(request, slot_name)
    if why:
        return KiroCliPlan(code=CODE_REMOTE, session_key=session_key)
    smap = _session_map(state)
    if smap is None:
        return KiroCliPlan(code=CODE_NO_SESSION, session_key=session_key)
    sid, label, mapped_cwd = smap.live_sid(session_key)
    if resume_decided_by_load(label):
        # The sid belongs to another harness (KAS, Claude, Codex...).
        return KiroCliPlan(code=CODE_NOT_KIRO, session_key=session_key)
    if not await asyncio.to_thread(_configured_backend_is_kiro, session_key):
        return KiroCliPlan(code=CODE_NOT_KIRO, session_key=session_key)
    if not sid or not _SID_RE.match(sid):
        return KiroCliPlan(code=CODE_NO_SESSION, session_key=session_key)
    if not await asyncio.to_thread(_files_resumable, sid):
        return KiroCliPlan(code=CODE_NO_LOG, session_key=session_key, sid=sid)
    project = getattr(slot, "project", "") or mapped_cwd
    cwd = await asyncio.to_thread(resolve_directory, project)
    if cwd is None:
        return KiroCliPlan(code=CODE_NO_PROJECT, session_key=session_key, sid=sid)
    binary = await asyncio.to_thread(_resolve_kiro_binary)
    if not binary:
        return KiroCliPlan(code=CODE_NOT_INSTALLED, session_key=session_key, sid=sid, cwd=cwd)
    try:
        wanted = await asyncio.to_thread(
            _resolve_agent,
            session_key,
            str(getattr(slot, "agent", "") or ""),
            str(getattr(slot, "agent_kind", "") or ""),
        )
    except Exception:  # noqa: BLE001 - an unresolvable agent opens with kiro-cli's default
        logger.info("kiro-cli open: agent of %s did not resolve", slot_name, exc_info=True)
        wanted = str(getattr(slot, "agent", "") or "")
    agent = ""
    if wanted and _AGENT_RE.match(wanted):
        in_user, in_project = await asyncio.to_thread(_agent_spec_where, wanted, cwd)
        if in_project:
            # kiro-cli would load the project's file of that name, which the
            # chat's agent can write, instead of the user's spec.
            return KiroCliPlan(code=CODE_PROJECT_AGENT, session_key=session_key, sid=sid)
        if in_user:
            agent = wanted
    if await asyncio.to_thread(_workspace_config_refusal, cwd, agent):
        return KiroCliPlan(code=CODE_PROJECT_AGENT, session_key=session_key, sid=sid)
    return KiroCliPlan(
        session_key=session_key,
        sid=sid,
        cwd=cwd,
        binary=binary,
        agent=agent,
        agent_without_spec="" if agent else wanted[:128],
    )


def _held_notice(agent_without_spec: str) -> str:
    text = (
        "Opened in kiro-cli in the terminal. This chat is paused until kiro-cli "
        "exits there; then it continues the same conversation."
    )
    if agent_without_spec:
        text += (
            f" kiro-cli uses its default agent: {agent_without_spec} has no agent file "
            "kiro-cli can load."
        )
    return text


_LEFT_OUT_NOTICE = (
    "{count} earlier messages from kiro-cli were too many to add here; the newest " "ones follow."
)

_RELEASED_NOTICE = (
    "kiro-cli exited in the terminal. What you said there is above, and this chat "
    "continues the conversation from there on your next message."
)


async def prepare_kiro_cli_launch(
    request: web.Request, slot_name: str, terminal_id: str
) -> tuple[KiroCliLaunch | None, str]:
    """Hand chat *slot_name*'s conversation to terminal *terminal_id*.

    Returns ``(launch, "")``, whose hold the caller must release with
    ``launch.release()`` if the terminal does not start (its session teardown
    releases it otherwise), or ``(None, code)``.
    """
    from kiro_crew.dashboard.chat_handlers import (
        _reset_slot_session,
        _slot_switch_session_lock,
        _subagents_attached_response,
        _switch_target_busy,
    )
    from kiro_crew.dashboard.chat_utils import effective_session_key
    from kiro_crew.dashboard.system_notices import SESSION_SWITCH_KIND

    plan = await plan_kiro_cli_launch(request, slot_name)
    if not plan.ok:
        return None, plan.code
    state = request.app["state"]
    slot = state._slots.get(slot_name)
    if slot is None:
        return None, CODE_UNKNOWN_CHAT
    session_key = plan.session_key
    async with contextlib.AsyncExitStack() as stack:
        # The reload's lock order: the slot's own lock, then the session's
        # switch lock, which a dashboard turn also holds while it registers a
        # cold-started session. Under both, a turn either registered first (the
        # busy read below refuses) or admits after the hold (and refuses there).
        await stack.enter_async_context(slot._lock)
        await stack.enter_async_context(_slot_switch_session_lock(session_key))
        if state._slots.get(slot_name) is not slot:
            return None, CODE_UNKNOWN_CHAT
        if effective_session_key(slot) != session_key:
            return None, CODE_BUSY
        if kiro_cli_handoff.is_held(session_key):
            return None, CODE_ALREADY_OPEN
        if getattr(slot, "queue_depth", 0):
            return None, CODE_BUSY
        if await _subagents_attached_response(state, slot, session_key, "kiro_cli") is not None:
            return None, CODE_SUBAGENTS
        provider = state.sessions.get_provider(session_key)
        if _switch_target_busy(state, slot, session_key, provider):
            return None, CODE_BUSY
        # Plan again under the locks: an agent or project switch that landed
        # while the first plan's reads ran, or while this request waited here,
        # holds these same locks for its whole commit, so this plan describes
        # the bindings the chat has now and they cannot move until the hold.
        plan = await plan_kiro_cli_launch(request, slot_name)
        if not plan.ok:
            return None, plan.code
        if plan.session_key != session_key or state._slots.get(slot_name) is not slot:
            return None, CODE_BUSY

        def _notify(text: str) -> None:
            # Runs from the terminal's open and teardown. The chat may be gone.
            live = state._slots.get(slot_name)
            if live is None:
                return
            live.append("assistant", text, "msg msg-a", meta={"kind": SESSION_SWITCH_KIND})
            state.push_slots_update()

        def _record_turns(turns: list[tuple[str, str]]) -> None:
            live = state._slots.get(slot_name)
            if live is None:
                return
            for role, text in turns:
                cls = "msg msg-u" if role == "user" else "msg msg-a"
                live.append(role, text, cls, meta={"origin": "kiro-cli"})
            state.push_slots_update()

        from kiro_crew.config.paths import kiro_sessions_dir

        launch = KiroCliLaunch(
            argv=plan.argv(),
            cwd=plan.cwd,
            session_key=session_key,
            slot_key=slot_name,
            terminal_id=terminal_id,
            agent_without_spec=plan.agent_without_spec,
            notify=_notify,
            record_turns=_record_turns,
            log_path=str(kiro_sessions_dir() / f"{plan.sid}.jsonl"),
        )
        if not kiro_cli_handoff.hold(
            session_key,
            terminal_id,
            on_release=launch.on_released,
            before_release=launch.settle,
        ):
            return None, CODE_ALREADY_OPEN
        try:
            # A pre-spawn already past its own hold check would register a
            # process that loads this conversation: stop it and let it unwind
            # before the reset below pops whatever it registered.
            eager = getattr(slot, "_eager_spawn_task", None)
            if eager is not None and not eager.done():
                eager.cancel()
                await asyncio.wait({eager}, timeout=_EAGER_SPAWN_CANCEL_WAIT_S)
            reset_ok = await _reset_slot_session(state, slot, session_key, skip_if_busy=True)
            if not reset_ok and state.sessions.get_provider(session_key) is not None:
                # A turn slipped in after the busy read: the reset declined.
                launch.release()
                return None, CODE_BUSY
            if state.sessions.get_provider(session_key) is not None:
                launch.release()
                return None, CODE_BUSY
            # Measured once the chat's own process is gone: what kiro-cli
            # appends after this point is what the chat takes back.
            launch.log_offset = await asyncio.to_thread(_log_size, launch.log_path)
        except BaseException:
            launch.release()
            raise
    return launch, ""


def refusal_message(code: str) -> str:
    """The fixed sentence for refusal *code* (no request text in it)."""
    return _why(code)


async def api_chat_slot_kiro_cli(request: web.Request) -> web.Response:
    """GET /api/terminal/kiro-cli/{slot} -- can this chat open in kiro-cli?

    Owner-only and gated on the terminal panel, like the terminal itself.
    Answers ``{"available": true, "agent": "<name>"|"", "agent_without_spec":
    "<name>"|""}`` or ``{"available": false, "code": "...", "reason": "..."}``.
    Never names the session id, the binary or the folder.
    """
    caller = request.get("user")
    if not caller:
        return web.Response(status=401, text="Unauthorized")
    owner_denied = await require_owner_dashboard_request(request, "terminal.kiro_cli.plan")
    if owner_denied is not None:
        return owner_denied
    if not terminal._is_enabled(request):
        return web.json_response(
            {
                "available": False,
                "code": CODE_TERMINAL_DISABLED,
                "reason": _why(CODE_TERMINAL_DISABLED),
            }
        )
    slot_name = request.match_info.get("slot", "")
    plan = await plan_kiro_cli_launch(request, slot_name)
    if not plan.ok:
        return web.json_response({"available": False, "code": plan.code, "reason": _why(plan.code)})
    held = kiro_cli_handoff.is_held(plan.session_key)
    return web.json_response(
        {
            "available": not held,
            **({"code": CODE_ALREADY_OPEN, "reason": _why(CODE_ALREADY_OPEN)} if held else {}),
            "agent": plan.agent,
            "agent_without_spec": plan.agent_without_spec,
        }
    )


def audit_kiro_cli_open(caller: str, outcome: str, resources: str) -> None:
    """One SEL record for a kiro-cli open, never naming the session id or folder."""
    try:
        terminal._sel().log_api_access(
            caller=caller,
            operation="terminal.kiro_cli.open",
            outcome=outcome,
            source="dashboard",
            resources=resources,
        )
    except Exception:  # pragma: no cover - audit must never change the outcome
        logger.debug("SEL audit for terminal.kiro_cli.open failed", exc_info=True)
