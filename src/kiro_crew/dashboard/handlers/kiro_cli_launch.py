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
then records the hold (:mod:`kiro_crew.kiro_cli_handoff`) and resets
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
import copy
import json
import logging
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aiohttp import web

from kiro_crew import kiro_cli_handoff
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
#: How long the open waits for a pre-spawn it cancelled to finish unwinding: a
#: cancel mid-spawn first kills the process it started, which can take over 5 s.
_EAGER_SPAWN_CANCEL_WAIT_S = 30.0

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
#: The per-open copy of the chat's agent could not be made in ~/.kiro/agents.
CODE_LAUNCH_SPEC = "kiro_cli_launch_spec"
#: The session's envelope in ~/.kiro/sessions/cli could not be pointed at that
#: copy (kiro-cli's resume runs the agent the envelope names).
CODE_SESSION_FILE = "kiro_cli_session_file"
#: The chat's own agent process was still starting (the pre-spawn), with no
#: turn running, and did not stop in time.
CODE_STARTING = "kiro_cli_starting"
#: The open's record (what a restart would need to bring the turns back) could
#: not be written in Crew's data folder, or an earlier open's turns are still
#: waiting to be saved there.
CODE_RECORD = "kiro_cli_record"
#: A kiro-cli tab redialed after its process ended: it never re-opens by itself.
CODE_ENDED = "kiro_cli_ended"

KIRO_CLI_REFUSAL_CODES: frozenset[str] = frozenset(
    {
        CODE_PROJECT_AGENT,
        CODE_LAUNCH_SPEC,
        CODE_SESSION_FILE,
        CODE_STARTING,
        CODE_RECORD,
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
    CODE_LAUNCH_SPEC: (
        "Crew couldn't write the temporary copy of this chat's agent that kiro-cli "
        "runs to ~/.kiro/agents. Check that the folder is writable and the agent's "
        "file reads as a valid agent"
    ),
    CODE_SESSION_FILE: (
        "Crew couldn't point this chat's kiro-cli session file in ~/.kiro/sessions/cli "
        "at the copy of its agent without tool pre-approvals. Check that the folder "
        "is writable, then try again"
    ),
    CODE_STARTING: "This chat's agent is still starting. Try again in a moment",
    CODE_RECORD: (
        "Crew couldn't save what is needed to keep the turns made in kiro-cli in "
        "this chat. Check that Kiro Crew's data folder is writable, then try again"
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
    #: Starts what the chat queued while the terminal held it (a monitor wake, a
    #: delivery): the queue drain leaves those entries in place during the hold.
    drain_queue: Callable[[], None] | None = field(default=None, repr=False)
    #: Appends kiro-cli's turns to the chat's transcript as ``(role, text)``.
    #: Called with the turns and how many were left out; True once they are on
    #: disk (or have nowhere to go).
    record_turns: Callable[[list[tuple[str, str]], int], Awaitable[bool]] | None = field(
        default=None, repr=False
    )
    #: The session's kiro-cli log and its size when the terminal took it over.
    log_path: str = ""
    log_offset: int = 0
    #: The launch copy of the chat's agent spec this open wrote, removed on release.
    launch_spec: str = ""
    #: Whether this open's on-disk record (:func:`kiro_cli_handoff.record_open`)
    #: was written, and so is removed with the hold once its turns are imported.
    recorded: bool = False
    #: Tags this open's imported rows, so a retried import adds each one once.
    import_id: str = ""
    #: The kiro-cli session this open handed over, and what its envelope named
    #: and trusted before the open changed it (see :func:`_point_session_agent`);
    #: ``None`` until the envelope was changed, and again once it is put back.
    sid: str = ""
    session_agent: dict | None = None
    #: False while the terminal's turns still owe a durable import (see settle).
    _imported: bool = field(default=True, repr=False)
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
            self.notify(
                _held_notice(self.agent_without_spec, asks_before_tools=bool(self.launch_spec))
            )

    async def settle(self) -> None:
        """Bring the turns made in kiro-cli into the chat's transcript.

        Awaited by the hold's release, before the chat may run again. The chat's
        next turn may resume by REPLAYING its own transcript into a fresh
        session (a dashboard chat with MCP Tool Search on, the default) rather
        than loading the native one, so turns that are only in kiro-cli's log
        would be lost to it. Recording them here carries them either way, and
        shows them in the chat.
        """
        from kiro_crew.dashboard.chat_utils_awaits import drained_to_thread

        record_turns = self.record_turns
        importing = bool(self._announced and self.log_path and record_turns is not None)
        if importing:
            # Cleared before the first await and set only once the rows are on
            # disk: an import that fails or times out, at any await here, keeps
            # the open's record, and the chat's next turn retries from it.
            self._imported = False
        if self.session_agent is not None:
            await drained_to_thread(self._restore_session)
        if not importing or record_turns is None:
            return
        turns, left_out = await asyncio.to_thread(
            _read_terminal_turns, self.log_path, self.log_offset
        )
        # The count of rows left out is said rather than dropped silently (the
        # agent has those only if the chat resumes natively, not when it
        # replays this transcript), and it is written with the rows, so a
        # closed chat's transcript says it too.
        self._imported = await record_turns(turns, left_out) if turns or left_out else True

    def on_released(self) -> None:
        """The hold's release callback: remove the launch spec and the open's
        record (its turns are in the chat now), and say kiro-cli ended, only
        if it ran."""
        self._restore_session()
        _remove_launch_spec(self.launch_spec)
        self._forget_if_imported()
        if self._announced and self.notify is not None:
            self.notify(_RELEASED_NOTICE)
        if self.drain_queue is not None:
            self.drain_queue()

    def release(self) -> None:
        """Give the hold back (the terminal never started, or has ended)."""
        if self._released:
            return
        self._released = True
        kiro_cli_handoff.release(self.session_key, self.terminal_id)
        self._restore_session()
        _remove_launch_spec(self.launch_spec)
        self._forget_if_imported()

    def _restore_session(self) -> None:
        """Put the session's envelope back as the open found it. Blocking (a
        small read and write); the release runs it off the loop in ``settle``,
        so only an open that never started does it here. One that fails keeps
        the open's record, and the chat's next turn restores it from there
        (:func:`recover_left_open`)."""
        if self.session_agent is None:
            return
        try:
            _restore_session_agent(self.sid, self.session_agent)
        except OSError:
            logger.warning("kiro-cli: could not restore the session's agent", exc_info=True)
            return
        self.session_agent = None

    def _forget_if_imported(self) -> None:
        """Remove the open's record unless its turns still owe a durable import
        or its session's envelope is not restored yet."""
        if self.recorded and self._imported and self.session_agent is None:
            kiro_cli_handoff.forget_open(self.session_key)


#: At most this many of the newest rows are recorded, each at most this long.
_TURNS_MAX_ROWS = 400
_TURN_MAX_CHARS = 100_000


#: Leads a row whose start was cut to fit :data:`_TURN_MAX_CHARS`.
_CLIPPED_PREFIX = "(The start of this message from kiro-cli was too long to add here.)\n\n"


def _clip_turn(text: str) -> str:
    """*text* within :data:`_TURN_MAX_CHARS`, keeping its END and saying so:
    a reply split around tool calls ends in its result, which is what the
    conversation continues from."""
    if len(text) <= _TURN_MAX_CHARS:
        return text
    if text.startswith(_CLIPPED_PREFIX):
        text = text[len(_CLIPPED_PREFIX) :]
    return _CLIPPED_PREFIX + text[-(_TURN_MAX_CHARS - len(_CLIPPED_PREFIX)) :]


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
#: The largest log record read back; a prompt or reply past it is counted, not held.
_TURN_LINE_MAX_BYTES = 2 * 1024 * 1024


def _turn_head(peek, start: int) -> str:
    """The role (``user``/``assistant``) of the record at *start* by its head
    alone, or ``""`` when it is not a prompt or reply."""
    peek.seek(start)
    head = peek.read(_KIND_HEAD_BYTES)
    return next((r for marker, r in _TURN_KINDS.items() if marker in head), "")


def _read_terminal_turns(path: str, offset: int) -> tuple[list[tuple[str, str]], int]:
    """``(rows, left_out)``: ``(role, text)`` for each prompt and reply kiro-cli
    appended to its log after *offset*, and how many older rows were left out.
    Blocking (reads the file).

    The delta is read through the repo's bounded JSONL reader
    (:func:`kiro_crew.jsonl_util.bounded_raw_records_with_offsets`), which never
    holds a record past :data:`_TURN_LINE_MAX_BYTES`, and only ``Prompt`` (the
    user) and ``AssistantMessage`` records are parsed; an assistant reply split
    around tool calls is joined into one row. Tool results cost nothing against
    the caps. A prompt or reply past the byte cap is left out and counted. When
    the delta holds more than :data:`_TURNS_MAX_ROWS` rows the NEWEST are kept
    -- the chat continues from the end of the conversation -- and the older ones
    are counted too, for the chat to say so. A log that is
    now SHORTER than *offset* was rewritten, not appended to, so nothing is read
    rather than replaying the whole conversation twice.
    """
    from kiro_crew.jsonl_util import bounded_raw_records_with_offsets

    rows: collections.deque[tuple[str, str]] = collections.deque(maxlen=_TURNS_MAX_ROWS)
    seen = 0
    too_long = 0
    last_role = ""
    try:
        with open(path, "rb") as fh, open(path, "rb") as peek:
            fh.seek(0, os.SEEK_END)
            if fh.tell() <= offset:
                return [], 0
            fh.seek(offset)
            records = bounded_raw_records_with_offsets(
                fh, Path(path), cap=_TURN_LINE_MAX_BYTES, label="kiro-cli turns"
            )
            # An over-cap record is reported by the reader without its true
            # end. It finished if another record follows it; a last one
            # finished only if the file ends in a line break, since one
            # kiro-cli never finished writing is no message.
            oversized_turn = False
            for start, _end, record in records:
                too_long += oversized_turn
                oversized_turn = False
                if record is None:
                    skipped_role = _turn_head(peek, start)
                    if skipped_role:
                        # A turn left out still separates the turns around it:
                        # a reply after a skipped prompt is a new row, never
                        # joined to the reply before it.
                        oversized_turn = True
                        last_role = skipped_role
                    continue
                head = record[:_KIND_HEAD_BYTES]
                role = next((r for marker, r in _TURN_KINDS.items() if marker in head), "")
                if not role:
                    continue
                try:
                    entry = json.loads(record)
                except ValueError:
                    continue  # a partial trailing line
                data = entry.get("data") if isinstance(entry, dict) else None
                text = _text_parts(data.get("content")) if isinstance(data, dict) else ""
                if not text:
                    if role == "user":
                        # A prompt with no text (an image alone) is still a
                        # turn: the reply after it is a new row.
                        last_role = role
                    continue
                if rows and role == last_role == "assistant" and rows[-1][0] == "assistant":
                    rows[-1] = (role, _clip_turn(f"{rows[-1][1]}\n\n{text}"))
                else:
                    rows.append((role, _clip_turn(text)))
                    seen += 1
                last_role = role
            if oversized_turn:
                peek.seek(-1, os.SEEK_END)
                too_long += peek.read(1) in (b"\n", b"\r")
    except FileNotFoundError:
        # No log, nothing kiro-cli said to bring in. Any other read error is
        # raised: it is not "no turns", and the caller keeps its record to retry.
        return [], 0
    return list(rows), seen - len(rows) + too_long


#: Row meta naming which import of which open a row came from (``<id>:<n>``), so
#: a retried import adds only the rows a failed or interrupted one did not.
_IMPORT_META_KEY = "kiro_cli_import"


#: One row an import writes: ``(role, text, cls, import tag, row meta)``.
_ImportEntry = tuple[str, str, str, str, dict]


def _import_entries(
    rows: list[tuple[str, str, str]],
    import_id: str,
    *,
    left_out: int = 0,
    notices: tuple[str, ...] = (),
) -> list[_ImportEntry]:
    """Every row one import writes, each tagged so a retry adds it once: the
    notice of rows left out (ahead of the kept ones, which are the newest),
    kiro-cli's rows, then *notices*. The same list goes to an open chat and to
    a closed chat's transcript, so neither loses the count of what was left out."""
    kind = {"kind": _notice_kind()}
    entries: list[_ImportEntry] = []
    if left_out:
        entries.append(
            (
                "assistant",
                _LEFT_OUT_NOTICE.format(count=left_out),
                "msg msg-a",
                f"{import_id}:left-out",
                kind,
            )
        )
    entries += [
        (role, text, cls, f"{import_id}:{n}", dict(_ORIGIN_META))
        for n, (role, text, cls) in enumerate(rows)
    ]
    entries += [
        ("assistant", text, "msg msg-a", f"{import_id}:notice:{n}", kind)
        for n, text in enumerate(notices)
    ]
    return entries


def _imported_tags(messages: object) -> set[str]:
    tags: set[str] = set()
    for row in messages if isinstance(messages, list) else ():
        meta = row.get("meta") if isinstance(row, dict) else None
        tag = meta.get(_IMPORT_META_KEY) if isinstance(meta, dict) else None
        if isinstance(tag, str):
            tags.add(tag)
    return tags


def _append_to_archived_transcript(
    conversation_log: object, key: str, created_at: str, rows: list[_ImportEntry]
) -> int:
    """Append *rows* (:func:`_import_entries`) to the closed chat's transcript
    *key*; how many were written. Blocking (takes the transcript's lock); a
    failed write raises.

    Under the lock the transcript must still be the one the chat last saved:
    present, and with the ``created_at`` the chat observed, so a transcript
    deleted meanwhile is never recreated. A row whose import tag is already
    there is not written again, so a retried import is idempotent. Each row
    gets its own fresh id, so content-equal rows ("continue", "Done.") are each
    kept.
    """
    from kiro_crew.history import mint_row_mid

    log: Any = conversation_log
    with log.atomic_appends(key):
        if not log.has_log(key):
            return 0
        if created_at and str(log.get_metadata(key).get("created_at") or "") != created_at:
            return 0
        have = _imported_tags(log.read_messages(key))
        written = 0
        for role, text, cls, tag, meta in rows:
            if tag in have:
                continue
            log.append(
                key,
                role,
                text,
                cls=cls,
                mid=mint_row_mid(),
                extra_meta={**meta, _IMPORT_META_KEY: tag},
            )
            written += 1
    return written


async def _persist_live_slot(state: Any, slot: Any, history_key: str) -> bool:
    """Write chat *slot*'s transcript now and say whether it landed. The
    periodic flush would write it within seconds, but an import's record is
    only removed once its rows are on disk."""
    from kiro_crew.dashboard.chat_persistence import save_slot_off_loop

    return bool(
        await save_slot_off_loop(state, slot, best_effort=False, expected_history_key=history_key)
    )


async def _import_turns(
    state: Any,
    live: Any,
    history_key: str,
    created_at: str,
    rows: list[tuple[str, str, str]],
    import_id: str,
    *,
    left_out: int = 0,
    notices: tuple[str, ...] = (),
) -> bool:
    """Record kiro-cli's *rows* in the chat, durably and at most once.

    Into *live* (the chat tab, when it is still this conversation) and then its
    transcript written, or else into the closed chat's transcript. True once
    the rows are on disk, or when they have nowhere to go (the conversation
    was deleted); False when the write did not land, so the caller keeps the
    open's record to retry from. The count of rows *left_out* and *notices*
    are rows of the same import (:func:`_import_entries`), so both paths keep
    them, and a row a previous attempt already wrote is skipped by its tag.
    """
    tagged = _import_entries(rows, import_id, left_out=left_out, notices=notices)
    try:
        if live is not None:
            have = _imported_tags(getattr(live, "messages", None))
            for role, text, cls, tag, meta in tagged:
                if tag in have:
                    continue
                # ``broadcast_user``: these prompts were typed in the terminal,
                # so no composer echo shows them in the chat.
                live.append(
                    role, text, cls, broadcast_user=True, meta={**meta, _IMPORT_META_KEY: tag}
                )
            push = getattr(state, "push_slots_update", None)
            if callable(push):
                push()
            return await _persist_live_slot(state, live, history_key)
        if not tagged or getattr(state, "conversation_log", None) is None:
            return True
        from kiro_crew.dashboard.chat_utils_awaits import drained_to_thread

        await drained_to_thread(
            _append_to_archived_transcript, state.conversation_log, history_key, created_at, tagged
        )
        return True
    except Exception:
        logger.warning(
            "kiro-cli: could not add the terminal's turns to the chat's transcript; "
            "the open's record is kept to retry",
            exc_info=True,
        )
        return False


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

    * the project's ``.kiro/agents`` holds anything at all, or cannot be read:
      kiro-cli resolves an agent there by the name DECLARED inside a file, not
      by its file name, and reads ``*.md`` specs as well as ``*.json``, so no
      file there can be ruled out as the agent (or the default) it would run;
    * the project has a workspace MCP file (``.kiro/settings/mcp.json``, or the
      legacy ``.amazonq/mcp.json``) and the agent may include it: the default
      agent, or a user spec with ``includeMcpJson``/``useLegacyMcpJson`` set or
      unreadable.
    """
    agents_dir = os.path.join(project_dir, ".kiro", "agents")
    try:
        if os.listdir(agents_dir):
            return True
    except (FileNotFoundError, NotADirectoryError):
        pass
    except OSError:
        return True
    mcp_files = (
        os.path.join(project_dir, ".kiro", "settings", "mcp.json"),
        os.path.join(project_dir, ".amazonq", "mcp.json"),
    )
    if not any(os.path.lexists(path) for path in mcp_files):
        return False
    if not agent:
        return True
    try:
        _path, spec = _read_user_agent_spec(agent)
    except (OSError, ValueError):
        return True
    return spec.get("includeMcpJson") is not False or spec.get("useLegacyMcpJson") is True


def _user_agent_spec_path(agent: str) -> Path | None:
    """The user-level spec file that declares *agent*, resolved the way Crew
    resolves agents everywhere (a declared ``name`` wins over a file name), or
    ``None``. Blocking. Raises ``ValueError`` when two specs declare it."""
    from kiro_crew.agent import agent_spec_path

    return agent_spec_path(agent)


def _read_user_agent_spec(agent: str) -> tuple[Path, dict]:
    """``(path, spec)`` for user-level agent *agent*, read through the shared
    hardened spec reader. Blocking. Raises ``OSError`` when there is no such
    spec or it cannot be read, ``ValueError`` when it is not a spec object."""
    from kiro_crew.agent_discovery import read_agent_spec_strict

    path = _user_agent_spec_path(agent)
    if path is None:
        raise FileNotFoundError(f"no agent spec declares {agent!r}")
    spec = read_agent_spec_strict(path, operation="kiro_cli_launch_spec", source="dashboard")
    if not isinstance(spec, dict):
        raise ValueError(f"agent spec {agent!r} is not an object")
    return path, spec


def _agent_spec_where(agent: str, project_dir: str) -> tuple[bool, bool]:
    """``(in the user's agents directory, in the project's .kiro/agents)`` for
    *agent*. Blocking (stats files).

    Only the user-level file counts as the agent's spec. kiro-cli resolves
    ``--agent <name>`` from the project folder first, and that folder is one the
    chat's sandboxed agent can write, so a project file of the same name would
    replace the user's spec -- its MCP commands and hooks then run in this
    unsandboxed terminal. The plan refuses the open in that case.
    """
    try:
        user = _user_agent_spec_path(agent) is not None
    except ValueError:
        user = False  # two specs declare the name: kiro-cli's pick is undefined
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
    """Whether the chat's session would start on kiro-cli, a crewmate's harness
    pin included. Blocking (config read). A pin this gateway cannot start
    answers no: that session starts nowhere, so there is nothing to open."""
    from kiro_crew.acp_backends import ACP_BACKEND_KIRO
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.crewmate_harness import CrewmateHarnessUnavailable, crewmate_backend_pin
    from kiro_crew.members import select_provider_backend

    cfg = KiroCrewConfig.load()
    try:
        backend = select_provider_backend(
            session_key,
            cfg.agent.member_acp_backend,
            cfg.agent.acp_backend,
            crew_backend=crewmate_backend_pin(cfg, session_key),
        )
    except CrewmateHarnessUnavailable:
        return False
    return backend == ACP_BACKEND_KIRO


def _files_resumable(sid: str) -> bool:
    from kiro_crew.session_map import session_files_resumable

    return session_files_resumable(sid)


#: Names of the launch copies of agent specs an open writes to the user's agents
#: directory. One per open, named in the open's record before it is written, and
#: removed when its hold is released or, after a restart, by the recovery that
#: reads that record. Nothing sweeps the directory: another gateway sharing it
#: may own a copy there.
_LAUNCH_SPEC_PREFIX = "kirocrew-kiro-cli-open-"


def _launch_spec_path() -> str:
    """A fresh path for one open's launch copy; its stem is what ``--agent`` names."""
    import uuid

    from kiro_crew.config.paths import kiro_agents_dir

    return str(kiro_agents_dir() / f"{_LAUNCH_SPEC_PREFIX}{uuid.uuid4().hex[:16]}.json")


def _write_launch_spec(agent: str, target: str) -> None:
    """Write the launch copy of user-level agent *agent* for one open to
    *target* (from :func:`_launch_spec_path`). Blocking. Raises ``OSError`` or
    ``ValueError`` when the spec cannot be read or the copy cannot be written.

    The copy is the chat's agent -- prompt, MCP servers, model, hooks -- with
    every backend-side grant emptied the way the side reply's read-only spec
    empties them (:func:`~kiro_crew.dashboard.side_readonly_spec.empty_backend_grants`),
    keeping its own deny and ask rules, so kiro-cli asks the owner before each
    tool. It sits beside the original so a relative ``file://`` prompt still
    resolves, and is written owner-only.
    """
    from kiro_crew.dashboard.side_readonly_spec import ReadOnlySpecError, empty_backend_grants

    source, spec = _read_user_agent_spec(agent)
    name = Path(target).stem
    view = copy.deepcopy(spec)
    try:
        empty_backend_grants(view, agent_filename=f"{name}.json", keep_narrowing=True)
    except ReadOnlySpecError as exc:
        raise ValueError(f"agent spec {agent!r}: {exc}") from exc
    view["name"] = name
    view["description"] = (
        f"Kiro Crew's copy of {agent} for one 'Open in kiro-cli' terminal, without "
        f"its tool pre-approvals. Removed when that terminal closes; pick {agent} "
        "instead."
    )
    prompt = view.get("prompt")
    if isinstance(prompt, str) and prompt.startswith("file://"):
        prompt_path = Path(prompt[7:]).expanduser()
        if not prompt_path.is_absolute():
            view["prompt"] = "file://" + (source.parent / prompt_path).absolute().as_posix()
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(view, out, indent=2)
    except BaseException:
        _remove_launch_spec(target)
        raise


def _remove_launch_spec(path: str) -> None:
    if not path:
        return
    with contextlib.suppress(OSError):
        os.unlink(path)


#: The largest session envelope (``<sid>.json``) an open rewrites; kiro-cli's own
#: are a few KB.
_ENVELOPE_MAX_BYTES = 4 * 1024 * 1024


def _read_envelope(sid: str) -> tuple[Path, dict, int]:
    """``(path, envelope, mode)`` for kiro-cli session *sid*'s envelope. Blocking.
    Raises ``OSError`` when it is missing, not a regular file (a symlink is
    refused, not followed) or too big, ``ValueError`` when it does not read as
    an envelope with a ``session_state`` object."""
    import stat

    from kiro_crew.config.paths import kiro_sessions_dir

    path = kiro_sessions_dir() / f"{sid}.json"
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as fh:
        st = os.fstat(fh.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_size > _ENVELOPE_MAX_BYTES:
            raise OSError(f"kiro-cli session envelope {path} is not a small regular file")
        envelope = json.loads(fh.read(_ENVELOPE_MAX_BYTES + 1))
    if not isinstance(envelope, dict) or not isinstance(envelope.get("session_state"), dict):
        raise ValueError(f"kiro-cli session envelope {path} has no session_state object")
    return path, envelope, stat.S_IMODE(st.st_mode)


def _write_envelope(path: Path, envelope: dict, mode: int) -> None:
    from kiro_crew.atomic_write import atomic_write

    atomic_write(path, json.dumps(envelope), mode=mode)


def _empty_session_grants(permissions: object, cwd: str) -> None:
    """Empty, in place, the session-level grants of a kiro-cli envelope's
    ``permissions``: every ``allowed_*`` and ``trusted_*`` list, at any depth,
    except that reading the conversation's own folder stays allowed. Denials
    are kept."""
    if not isinstance(permissions, dict):
        return
    for key, value in permissions.items():
        if isinstance(value, dict):
            _empty_session_grants(value, cwd)
        elif isinstance(value, list) and key.startswith(("allowed", "trusted")):
            permissions[key] = [p for p in value if p == cwd] if key == "allowed_read_paths" else []


def _read_session_agent(sid: str) -> dict:
    """What kiro-cli session *sid*'s envelope names and trusts now: the state
    :func:`_point_session_agent` replaces and :func:`_restore_session_agent`
    puts back. Blocking. Raises ``OSError`` or ``ValueError`` when the envelope
    cannot be read."""
    state = _read_envelope(sid)[1]["session_state"]
    return {
        "agent_name": state.get("agent_name"),
        "permissions": copy.deepcopy(state.get("permissions")),
    }


def _point_session_agent(sid: str, agent: str | None) -> None:
    """Point kiro-cli session *sid* at *agent* for one open and empty its
    session-level grants. Blocking. Raises ``OSError`` or ``ValueError`` when
    the envelope cannot be read or written.

    ``kiro-cli chat --resume-id`` runs the agent the session's envelope names
    (``session_state.agent_name``, the agent the chat last ran) whatever
    ``--agent`` says, and keeps the tools trusted in the session
    (``session_state.permissions``) trusted. So the launch copy reaches the
    terminal only through the envelope, and Crew's session-level grants come
    out of it for the hold. *agent* ``None`` leaves kiro-cli its default agent.
    """
    path, envelope, mode = _read_envelope(sid)
    state = envelope["session_state"]
    state["agent_name"] = agent
    _empty_session_grants(state.get("permissions"), str(envelope.get("cwd") or ""))
    _write_envelope(path, envelope, mode)


def _restore_session_agent(sid: str, saved: object) -> None:
    """Put back what :func:`_point_session_agent` changed in session *sid*'s
    envelope: the agent it named and its session-level grants. Grants kiro-cli
    added in the terminal go with the hold. Blocking. Raises ``OSError`` when
    the envelope cannot be read or rewritten, for a later retry. An envelope
    kiro-cli removed, or left as something that is not one, has nothing to
    put back."""
    if not isinstance(saved, dict) or not isinstance(sid, str) or not _SID_RE.match(sid):
        return
    try:
        path, envelope, mode = _read_envelope(sid)
    except FileNotFoundError:
        return
    except ValueError:
        logger.warning("kiro-cli: session %s's envelope does not read as one", sid)
        return
    state = envelope["session_state"]
    state["agent_name"] = saved.get("agent_name")
    if saved.get("permissions") is not None:
        state["permissions"] = saved["permissions"]
    _write_envelope(path, envelope, mode)


def _session_map(state: object):
    return getattr(getattr(state, "sessions", None), "_session_map", None)


def _allocation_in_flight(sessions: object, key: str) -> bool:
    """Whether a claim or cold start holds an allocation reservation for *key*.
    Fails closed: a manager that cannot answer counts as yes."""
    try:
        return bool(getattr(sessions, "_has_allocation_reservation")(key))
    except Exception:
        logger.debug("kiro-cli: could not read the session's reservations", exc_info=True)
        return True


def _channel_reaches(sessions: object, key: str) -> bool:
    """Whether a channel's inbound messages run on session *key*: a two-way
    (session-resume) mirror binding or a linked Slack thread. Blocking (reads
    the session map). Fails closed: a map that cannot answer counts as yes."""
    try:
        if getattr(sessions, "mirror_accepts_inbound")(key):
            return True
        thread_ts, _channel = getattr(sessions, "get_slack_link")(key)
        return bool(thread_ts)
    except Exception:
        logger.debug("kiro-cli: could not read the chat's channel links", exc_info=True)
        return True


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
        or getattr(slot, "is_restricted", False) is True
    ):
        # A channel's turns reach its session without passing the dashboard
        # admission that enforces the hold, and an app's chat runs a launch
        # copy of its agent that kiro-cli cannot name. An incognito or
        # temporary chat's session files are deleted when its process shuts
        # down, which the handover's reset does: nothing would be left to resume.
        return KiroCliPlan(code=CODE_UNSUPPORTED, session_key=session_key)
    if await asyncio.to_thread(_channel_reaches, getattr(state, "sessions", None), session_key):
        # A dashboard chat a channel resumed (or a Slack thread follows): that
        # channel's turns run on this session without the dashboard admission.
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


def _held_notice(agent_without_spec: str, *, asks_before_tools: bool = False) -> str:
    text = (
        "Opened in kiro-cli in the terminal. This chat is paused until kiro-cli "
        "exits there; then it continues the same conversation."
    )
    if asks_before_tools:
        text += (
            " The terminal runs outside this chat's sandbox, so kiro-cli asks you "
            "there before each tool it runs."
        )
    if agent_without_spec:
        text += (
            f" kiro-cli uses its default agent: {agent_without_spec} has no agent file "
            "kiro-cli can load."
        )
    return text


_LEFT_OUT_NOTICE = (
    "{count} messages from kiro-cli could not be added here, being too many or too "
    "long; the rest follow."
)

#: Marks a row that came from the terminal.
_ORIGIN_META = {"origin": "kiro-cli"}

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
    from kiro_crew.dashboard.chat_utils import effective_session_key, slot_history_key
    from kiro_crew.dashboard.chat_utils_awaits import drained_to_thread
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
        # An earlier open a restart cut short: its turns come in before this
        # open measures where the log ends. If they still could not be saved,
        # this open would replace the only record of them, so it is refused.
        if await recover_left_open(state, slot, session_key) and (
            await asyncio.to_thread(kiro_cli_handoff.has_left_open, session_key)
        ):
            return None, CODE_RECORD
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
        # The plan's reads awaited, and a send that does not take these locks
        # could have dispatched meanwhile (it sets ``running`` at dispatch). So
        # the busy reads are taken again here, with no await from here to the
        # hold: a turn either shows now or is refused by the hold.
        if kiro_cli_handoff.is_held(session_key):
            return None, CODE_ALREADY_OPEN
        if effective_session_key(slot) != session_key:
            return None, CODE_BUSY
        if getattr(slot, "queue_depth", 0):
            return None, CODE_BUSY
        if _switch_target_busy(state, slot, session_key, state.sessions.get_provider(session_key)):
            return None, CODE_BUSY
        # A claim or cold start already admitted (a channel message that passed
        # the allocation door before this hold) would register a second writer
        # after it. Read here, with no await before the hold; an allocation
        # admitted after the hold is refused under the allocator's own lock.
        if _allocation_in_flight(state.sessions, session_key):
            return None, CODE_BUSY
        history_key = slot_history_key(slot)
        opened_identity = str(getattr(slot, "_disk_meta_created_at", "") or "")

        def _same_chat() -> object | None:
            # The chat tab now under this name, only if it is still this
            # conversation: the same slot, or one reopened from the same
            # transcript (same key and creation stamp). A chat made anew under
            # the name after a delete is someone else's conversation.
            live = state._slots.get(slot_name)
            if live is None or slot_history_key(live) != history_key:
                return None
            if live is slot:
                return live
            stamp = str(getattr(live, "_disk_meta_created_at", "") or "")
            return live if opened_identity and stamp == opened_identity else None

        def _notify(text: str) -> None:
            # Runs from the terminal's open and teardown. The chat may be gone.
            live: Any = _same_chat()
            if live is None:
                return
            live.append("assistant", text, "msg msg-a", meta={"kind": SESSION_SWITCH_KIND})
            state.push_slots_update()

        def _drain_queue() -> None:
            # Runs from the hold's release, on the loop. Entries queued while the
            # terminal held the chat were left in place (the queue drain does not
            # start a turn admission would refuse); start them now, the way a
            # queue parked on memory preparation is started.
            from kiro_crew.dashboard.chat_runner import _drain_parked_queues

            live: Any = _same_chat()
            if live is None or not getattr(live, "_queue", None):
                return
            try:
                drain = asyncio.ensure_future(_drain_parked_queues(state, [live.key]))
            except RuntimeError:
                return  # no running loop: the chat's next send drains it
            state._background_tasks.add(drain)
            drain.add_done_callback(state._background_tasks.discard)

        import uuid

        import_id = uuid.uuid4().hex[:16]

        async def _record_turns(turns: list[tuple[str, str]], left_out: int = 0) -> bool:
            # Redaction is the transcript's own: the live broadcast redacts
            # every non-user row and the durable write redacts at its boundary.
            rows = [(r, t, "msg msg-u" if r == "user" else "msg msg-a") for r, t in turns]
            live: Any = _same_chat()
            if live is None and state._slots.get(slot_name) is not None:
                # A different conversation took the name: the one kiro-cli
                # continued was deleted, so its turns have nowhere to go.
                return True
            # Into the live chat, or (closed while kiro-cli ran) into its
            # archived transcript, which a later resume replays; only that same
            # transcript, never one recreated after a delete.
            return await _import_turns(
                state,
                live,
                history_key,
                str(getattr(slot, "_disk_meta_created_at", "") or "") or opened_identity,
                rows,
                import_id,
                left_out=left_out,
            )

        from kiro_crew.config.paths import kiro_sessions_dir

        launch = KiroCliLaunch(
            argv=plan.argv(),
            cwd=plan.cwd,
            session_key=session_key,
            slot_key=slot_name,
            terminal_id=terminal_id,
            agent_without_spec=plan.agent_without_spec,
            notify=_notify,
            drain_queue=_drain_queue,
            record_turns=_record_turns,
            log_path=str(kiro_sessions_dir() / f"{plan.sid}.jsonl"),
            import_id=import_id,
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
            # A cancelled one mid-spawn first kills the process it started,
            # which can take longer than a handshake; the open waits for that
            # rather than calling an idle chat busy.
            eager = getattr(slot, "_eager_spawn_task", None)
            if eager is not None and not eager.done():
                eager.cancel()
                await asyncio.wait({eager}, timeout=_EAGER_SPAWN_CANCEL_WAIT_S)
                if not eager.done():
                    launch.release()
                    return None, CODE_STARTING
            await _reset_slot_session(state, slot, session_key, skip_if_busy=True)
            if state.sessions.get_provider(session_key) is not None:
                # The reset declined, or a process registered after it: a turn
                # that slipped in after the busy read, or (no turn running) a
                # start that was not the pre-spawn's.
                launch.release()
                return None, (
                    CODE_BUSY
                    if getattr(slot, "running", False) or getattr(slot, "queue_depth", 0)
                    else CODE_STARTING
                )
            # A discard (reset, rewind) admitted before the hold may have
            # committed while the reset above waited: the conversation the
            # plan names must still be the one the chat resumes.
            smap = _session_map(state)
            live = smap.live_sid(session_key)[0] if smap is not None else ""
            if live != plan.sid:
                launch.release()
                return None, CODE_BUSY
            launch.sid = plan.sid
            if plan.agent:
                # The terminal runs outside Crew's sandbox, so the chat's agent
                # goes in without its tool pre-approvals: kiro-cli asks the
                # owner before each tool. Same agent otherwise (prompt, MCP
                # servers, model), under a per-open name.
                launch.launch_spec = await asyncio.to_thread(_launch_spec_path)
                launch.argv = [
                    plan.binary,
                    "chat",
                    "--resume-id",
                    plan.sid,
                    "--agent",
                    Path(launch.launch_spec).stem,
                ]
            try:
                session_agent = await asyncio.to_thread(_read_session_agent, plan.sid)
            except (OSError, ValueError):
                logger.warning("kiro-cli: could not read the session's envelope", exc_info=True)
                launch.release()
                return None, CODE_SESSION_FILE
            # Measured once the chat's own process is gone: what kiro-cli
            # appends after this point is what the chat takes back.
            launch.log_offset = await asyncio.to_thread(_log_size, launch.log_path)
            # On disk for a restart: a gateway that stops while the terminal
            # runs never releases this hold, so the chat's next turn finds the
            # record and brings these turns in (recover_left_open).
            try:
                # Drained: a cancelled open must not roll back (release) while
                # its own write still runs, here or in the two writes below.
                await drained_to_thread(
                    kiro_cli_handoff.record_open,
                    session_key,
                    {
                        "history_key": history_key,
                        "created_at": opened_identity,
                        "log_offset": launch.log_offset,
                        "sid": plan.sid,
                        "launch_spec": launch.launch_spec,
                        "import_id": import_id,
                        "session_agent": session_agent,
                    },
                )
            except OSError:
                logger.warning("kiro-cli: could not record the open", exc_info=True)
                launch.release()
                return None, CODE_RECORD
            launch.recorded = True
            # Written only now that the record names them, so a gateway that
            # dies anywhere below leaves its restart what it needs to undo them.
            if plan.agent:
                try:
                    await drained_to_thread(_write_launch_spec, plan.agent, launch.launch_spec)
                except (OSError, ValueError):
                    logger.warning("kiro-cli: could not write the launch spec", exc_info=True)
                    launch.release()
                    return None, CODE_LAUNCH_SPEC
            # kiro-cli's resume runs the agent the session's envelope names, not
            # --agent, and keeps the session's trusted tools: the envelope names
            # the launch copy (or no agent, for kiro-cli's default) and trusts
            # nothing until the release puts it back.
            launch.session_agent = session_agent
            try:
                await drained_to_thread(
                    _point_session_agent,
                    plan.sid,
                    Path(launch.launch_spec).stem if launch.launch_spec else None,
                )
            except (OSError, ValueError):
                logger.warning("kiro-cli: could not point the session at its agent", exc_info=True)
                launch.release()
                return None, CODE_SESSION_FILE
        except BaseException:
            launch.release()
            raise
    return launch, ""


_RECOVERED_NOTICE = (
    "What you said in kiro-cli while this chat was open there is above. It could "
    "not be added when kiro-cli ended (Kiro Crew restarted, or the chat could not "
    "be saved), and this chat continues the conversation from there."
)


def _left_open_turns(record: dict) -> tuple[list[tuple[str, str]], int]:
    """The turns an open cut short by a restart left in kiro-cli's log, read
    from the record's own offset, with the session's envelope put back and the
    launch copy it left removed. Blocking. Raises ``OSError`` when the envelope
    or the log cannot be read. The log and envelope paths are rebuilt from the
    session id, never taken from the record."""
    from kiro_crew.config.paths import kiro_agents_dir, kiro_sessions_dir

    sid = record.get("sid")
    # Before the copy goes: an envelope still naming it would resume the chat
    # on an agent that is gone.
    _restore_session_agent(str(sid or ""), record.get("session_agent"))
    spec = record.get("launch_spec")
    if isinstance(spec, str) and spec:
        path = Path(spec)
        if path.parent == kiro_agents_dir() and path.name.startswith(_LAUNCH_SPEC_PREFIX):
            _remove_launch_spec(str(path))
    offset = record.get("log_offset")
    if not isinstance(sid, str) or not _SID_RE.match(sid) or type(offset) is not int:
        return [], 0
    return _read_terminal_turns(str(kiro_sessions_dir() / f"{sid}.jsonl"), max(offset, 0))


#: One recovery per session at a time: a second entrant (an overlapping send, a
#: turn and a pre-spawn) waits for the first and then finds nothing left to do.
_recovery_locks: dict[str, asyncio.Lock] = {}


async def recover_left_open(state: Any, slot: Any, session_key: str) -> bool:
    """Bring in the turns of an open of *session_key* that were not brought in.

    A gateway that stops while a terminal holds the conversation never runs
    the hold's release (:meth:`KiroCliLaunch.settle`), and a release whose
    import failed keeps the record too, so those turns would be only in
    kiro-cli's log, and a chat that resumes by replaying its own transcript
    would go on without them. The open's record
    (:func:`kiro_cli_handoff.record_open`) stays on disk until they are in;
    this reads it and records the turns, into chat *slot* when it is still that
    conversation, else into its closed transcript, and removes the record only
    once the write landed (:func:`_import_turns`, which skips rows an earlier
    attempt wrote). Every way a dashboard chat starts its agent process calls
    this first: a turn's admission, the send before its busy checks, the
    pre-spawn, and a new open. True when a record was found.
    """
    from kiro_crew.dashboard.chat_utils import slot_history_key
    from kiro_crew.dashboard.chat_utils_awaits import drained_to_thread
    from kiro_crew.messaging.link import canonical_key

    # Kept, never popped: a waiter still holds the lock object, and a fresh one
    # would let a third entrant run beside it. One small entry per session.
    lock = _recovery_locks.setdefault(canonical_key(session_key), asyncio.Lock())
    async with lock:
        try:
            record = await asyncio.to_thread(kiro_cli_handoff.read_left_open, session_key)
        except OSError:
            # There but unreadable: it stays, and allocation keeps refusing the
            # session until it can be read (refuse_if_held).
            return True
        if record is None:
            return False
        try:
            # Drained here and below: a cancelled recovery keeps the lock until
            # its own envelope restore and record removal have finished, so no
            # later open or recovery runs under a worker still writing.
            turns, left_out = await drained_to_thread(_left_open_turns, record)
        except OSError:
            # The envelope or the log could not be read: the record stays for
            # the next try.
            logger.warning("kiro-cli: could not settle a left open", exc_info=True)
            return True
        history_key = str(record.get("history_key") or "")
        created_at = str(record.get("created_at") or "")
        import_id = str(record.get("import_id") or "") or f"open-{record.get('log_offset')}"
        rows = [(r, t, "msg msg-u" if r == "user" else "msg msg-a") for r, t in turns]
        same_chat = bool(
            slot is not None
            and history_key
            and slot_history_key(slot) == history_key
            and (
                not created_at
                or str(getattr(slot, "_disk_meta_created_at", "") or "") == created_at
            )
        )
        done = not history_key or await _import_turns(
            state,
            slot if same_chat else None,
            history_key,
            created_at,
            rows,
            import_id,
            left_out=left_out,
            notices=(_RECOVERED_NOTICE,),
        )
        if done:
            await drained_to_thread(kiro_cli_handoff.forget_open, session_key)
        return True


def _notice_kind() -> str:
    from kiro_crew.dashboard.system_notices import SESSION_SWITCH_KIND

    return SESSION_SWITCH_KIND


def _turn_in_flight(state: Any, slot_name: str, session_key: str) -> bool:
    """Whether chat *slot_name* has a turn running or messages queued: the reads
    the open itself refuses on (:func:`prepare_kiro_cli_launch`), unlocked."""
    from kiro_crew.dashboard.chat_handlers import _switch_target_busy

    slot = state._slots.get(slot_name)
    if slot is None:
        return False
    if getattr(slot, "queue_depth", 0):
        return True
    return bool(
        _switch_target_busy(state, slot, session_key, state.sessions.get_provider(session_key))
    )


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
    if not held and _turn_in_flight(request.app["state"], slot_name, plan.session_key):
        # The menu disables the row while the chat streams; the open would be
        # refused then too, so the route answers the same.
        return web.json_response({"available": False, "code": CODE_BUSY, "reason": _why(CODE_BUSY)})
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
