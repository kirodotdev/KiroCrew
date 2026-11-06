"""Open a chat's own kiro-cli conversation in the built-in terminal.

``GET /api/terminal/kiro-cli/{slot}`` says whether the chat can be opened in
kiro-cli and with which agent, so the chat menu can offer, explain or withhold
the action. The terminal WebSocket (``?kiro_cli_slot=<slot>``) then opens it:
:func:`prepare_kiro_cli_launch` hands the conversation over and returns the
argv the PTY runs.

Nothing the request carries reaches the command line except the chat slot's
NAME, which only selects a chat the gateway already holds. Only a chat on the
Kiro Agent harness opens: the argv is ``<kiro-cli> chat --agent-engine v3
--resume-id <sid>`` (kiro-cli's v3 store, handled in :mod:`.kiro_cli_launch_v3`).
The Kiro CLI harness, Claude Code, Codex and every other harness are refused.
The argv is built from:

* the kiro-cli binary :func:`kiro_crew.kiro_cli.resolve_kiro_cli` finds without
  the inherited ``PATH`` (the PTY starts in the chat's project folder, so a
  relative or agent-writable ``PATH`` entry must not pick what runs);
* the kiro-cli session id the session map holds for the chat's session key,
  accepted only in v3's id shape (``sess_<uuid>``) and only while its
  conversation is on disk;
* the agent the chat's bindings resolve to, used only when it has the plain
  name shape and an agent file kiro-cli can load: ``session.json`` names a
  per-open copy of it for the hold. A Crew-internal agent with no file falls
  back to kiro-cli's default agent, and the availability answer says so.

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
import functools
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
from kiro_crew.dashboard.handlers import kiro_cli_launch_v3, terminal
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

#: An agent name that names one file in an agents directory and nothing else.
_AGENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
#: A Kiro Agent chat's conversation, in kiro-cli's v3 store (:mod:`.kiro_cli_launch_v3`).
ENGINE_V3 = "v3"
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
#: The "Open in kiro-cli" Feature Preview is off (the default).
CODE_PREVIEW_OFF = "kiro_cli_preview_off"
CODE_PROJECT_AGENT = "kiro_cli_project_agent"
#: The per-open copy of the chat's agent could not be made in ~/.kiro/agents.
CODE_LAUNCH_SPEC = "kiro_cli_launch_spec"
#: The session's files in ~/.kiro/sessions could not be read or updated: the
#: envelope pointed at that copy (kiro-cli's resume runs the agent the envelope
#: names), or the log's current end measured.
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
        CODE_PREVIEW_OFF,
    }
)

_WHY: dict[str, str] = {
    CODE_UNKNOWN_CHAT: "This chat is not loaded",
    CODE_UNSUPPORTED: "This chat runs through a channel or an app, so it cannot open in kiro-cli",
    CODE_NOT_KIRO: "Open in kiro-cli works only for chats on the Kiro Agent harness",
    CODE_NO_SESSION: "This chat has no kiro-cli conversation yet. Send it a message first",
    CODE_NO_LOG: "This chat's kiro-cli conversation log is missing",
    CODE_NO_PROJECT: "This chat's project folder does not exist",
    CODE_NOT_INSTALLED: "kiro-cli is not installed where the gateway looks for it",
    CODE_BUSY: "This chat is busy. Wait for its turn and queued messages to finish",
    CODE_SUBAGENTS: "This chat has sub-agents working. Wait for them to finish",
    CODE_ALREADY_OPEN: "This chat is already open in kiro-cli in another terminal",
    CODE_REMOTE: "This chat runs on a remote environment",
    CODE_TERMINAL_DISABLED: "The terminal panel is turned off",
    CODE_PREVIEW_OFF: (
        "Open in kiro-cli is a feature preview. Turn it on under Settings > "
        "Developer > Feature previews"
    ),
    CODE_PROJECT_AGENT: (
        "This project has its own kiro-cli agents, hooks or MCP config, which kiro-cli "
        "would run outside Crew's sandbox. Open kiro-cli there yourself if you trust it"
    ),
    CODE_LAUNCH_SPEC: (
        "Crew couldn't write the temporary copy of this chat's agent that kiro-cli "
        "runs to ~/.kiro/agents. Check that the folder is writable and the agent's "
        "file reads as a valid agent"
    ),
    CODE_SESSION_FILE: (
        "Crew couldn't read or update this chat's kiro-cli session files in "
        "~/.kiro/sessions (they point kiro-cli at the copy of its agent without "
        "tool pre-approvals). Check that the folder is readable and writable, then "
        "try again"
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
    #: Which kiro-cli store the conversation is in: always :data:`ENGINE_V3`
    #: for a plan that opens (only a Kiro Agent chat does).
    engine: str = ""

    @property
    def ok(self) -> bool:
        return not self.code

    def argv(self) -> list[str]:
        """The command. v3 resumes on the agent its ``session.json`` names
        (pointed at the launch copy for the hold), never ``--agent``."""
        return [self.binary, "chat", *kiro_cli_launch_v3.ENGINE_ARGS, "--resume-id", self.sid]


#: Releases started by :meth:`KiroCliLaunch.release_soon`, kept referenced
#: until they finish.
_release_tasks: set[asyncio.Task] = set()


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
    #: Where the session's ``messages.jsonl`` ended when the terminal took it over.
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
    #: The plan's :attr:`KiroCliPlan.engine`: which store the session is in.
    engine: str = ""
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

        # Settling is this open's release under way: the hold's own release
        # follows it, so the terminal's cleanup (:meth:`arelease`) landing
        # mid-settle must not drop the hold while the import still runs.
        self._released = True
        try:
            await self._import_terminal_turns()
        finally:
            # The open's files go back off the loop, while the hold still
            # stands, however the import ended (a cancelled or timed-out one
            # included): the envelope, the launch copy, and the record once the
            # turns are durably in.
            await drained_to_thread(self._cleanup_files)

    async def _import_terminal_turns(self) -> None:
        from kiro_crew.dashboard.chat_utils_awaits import drained_to_thread

        record_turns = self.record_turns
        importing = bool(self._announced and self.engine and record_turns is not None)
        if importing:
            # Cleared before the first await and set only once the rows are on
            # disk: an import that fails or times out, at any await here, keeps
            # the open's record, and the chat's next turn retries from it.
            self._imported = False
        if self.session_agent is not None:
            await drained_to_thread(self._restore_session)
        if not importing or record_turns is None:
            return
        try:
            turns, left_out = await asyncio.to_thread(
                kiro_cli_launch_v3.read_turns, self.sid, self.log_offset
            )
        except _SessionFileUnusable:
            # Never readable: said in the chat, and nothing is owed to retry.
            logger.warning("kiro-cli: the session log is not a file it can read", exc_info=True)
            if self.notify is not None:
                self.notify(_UNREADABLE_NOTICE)
            self._imported = True
            return
        # The count of rows left out is said rather than dropped silently (the
        # agent has those only if the chat resumes natively, not when it
        # replays this transcript), and it is written with the rows, so a
        # closed chat's transcript says it too.
        self._imported = await record_turns(turns, left_out) if turns or left_out else True

    def on_released(self) -> None:
        """The hold's release callback, on the loop: say kiro-cli ended, only
        if it ran, and start what the chat queued meanwhile. No file work here:
        :meth:`settle` (or :meth:`arelease`) put the open's files back off the
        loop before the hold went. Marks this open released: a later
        :meth:`arelease` (the terminal's own cleanup after a failed start) must
        not repeat anything against a newer open of the same chat."""
        self._released = True
        if self._announced and self.notify is not None:
            self.notify(_RELEASED_NOTICE)
        if self.drain_queue is not None:
            self.drain_queue()

    async def arelease(self) -> None:
        """Give the hold back when the terminal never started: the open's files
        go back off the loop first (drained, so a cancel cannot cut it short),
        then the hold, so no newer open of the chat can find them half put back."""
        from kiro_crew.dashboard.chat_utils_awaits import drained_to_thread

        if self._released:
            return
        self._released = True
        await drained_to_thread(self._cleanup_files)
        kiro_cli_handoff.release(self.session_key, self.terminal_id)

    def release_soon(self) -> None:
        """:meth:`arelease` as its own task, for a caller on the loop that must
        not suspend (a section of the terminal handler that stays atomic)."""
        if self._released:
            return
        task = asyncio.ensure_future(self.arelease())
        _release_tasks.add(task)
        task.add_done_callback(_release_tasks.discard)

    def release(self) -> None:
        """:meth:`arelease` for a caller off the event loop. Blocking."""
        if self._released:
            return
        self._released = True
        self._cleanup_files()
        kiro_cli_handoff.release(self.session_key, self.terminal_id)

    def _cleanup_files(self) -> None:
        """Put back what the open changed on disk: the session's envelope, the
        launch copy, and the open's record once its turns are in. Blocking."""
        self._restore_session()
        _remove_launch_spec(self.launch_spec)
        self._forget_if_imported()

    def _restore_session(self) -> None:
        """Put the session's envelope back as the open found it. Blocking (a
        small read and write), so always run off the loop. One that fails keeps
        the open's record, and the chat's next turn restores it from there
        (:func:`recover_left_open`)."""
        if self.session_agent is None:
            return
        try:
            kiro_cli_launch_v3.restore_session_agent(self.sid, self.session_agent)
        except OSError:
            logger.warning("kiro-cli: could not restore the session's agent", exc_info=True)
            return
        self.session_agent = None

    def _forget_if_imported(self) -> None:
        """Remove the open's record unless its turns still owe a durable import
        or its session's envelope is not restored yet."""
        if self.recorded and self._imported and self.session_agent is None:
            kiro_cli_handoff.forget_open(self.session_key, import_id=self.import_id)


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


#: The largest log record read back; a prompt or reply past it is counted, not held.
_TURN_LINE_MAX_BYTES = 2 * 1024 * 1024


class _SessionFileUnusable(OSError):
    """A kiro-cli session file that is there but can never be read as one: not
    a regular file, a link, or over its size cap. Unlike a transient read error
    (``EIO``, ``EACCES``), retrying cannot help, so the open's record treats it
    as nothing to bring in rather than keeping the chat refused for good."""


def _checked_session_fd(fd: int, path: str | Path):
    """A binary reader of the session file *fd* was opened on, once the
    descriptor shows a regular file with no other name: a hard link would let
    an agent that can write the session folder point the read at any file the
    owner can read, and its text would come into the chat. Closes *fd* and
    raises :class:`_SessionFileUnusable` otherwise."""
    import stat

    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise _SessionFileUnusable(f"kiro-cli session file {path} is not a regular file")
        if info.st_nlink > 1:
            raise _SessionFileUnusable(f"kiro-cli session file {path} has another name")
        if getattr(os, "O_NONBLOCK", 0):
            os.set_blocking(fd, True)
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


@dataclass(frozen=True)
class _LogFormat:
    """How one kiro-cli store's log names its prompts and replies."""

    #: Marker in a record's head -> the role (``user``/``assistant``) it holds.
    kinds: dict[bytes, str]
    #: How far into a record its marker is looked for.
    head_bytes: int
    #: A parsed record's text; ``""`` for a turn with no text, ``None`` for a
    #: record whose head looked like a turn but is none.
    text: Callable[[object], str | None]


def _turn_head(peek, start: int, fmt: _LogFormat) -> str:
    """The role (``user``/``assistant``) of the record at *start* by its head
    alone, or ``""`` when it is not a prompt or reply."""
    peek.seek(start)
    head = peek.read(fmt.head_bytes)
    return next((r for marker, r in fmt.kinds.items() if marker in head), "")


def _read_turns(
    fh, peek, path: str, offset: int, fmt: _LogFormat
) -> tuple[list[tuple[str, str]], int]:
    """``(rows, left_out)``: ``(role, text)`` for each prompt and reply kiro-cli
    appended to its log after *offset*, and how many older rows were left out.
    Blocking. *fh* and *peek* are two readers of the same log.

    The delta is read through the repo's bounded JSONL reader
    (:func:`kiro_crew.jsonl_util.bounded_raw_records_with_offsets`), which never
    holds a record past :data:`_TURN_LINE_MAX_BYTES`, and only the records
    *fmt* marks as a prompt or reply are parsed; an assistant reply split
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
    fh.seek(0, os.SEEK_END)
    if fh.tell() <= offset:
        return [], 0
    fh.seek(offset)
    records = bounded_raw_records_with_offsets(
        fh, Path(path), cap=_TURN_LINE_MAX_BYTES, label="kiro-cli turns"
    )
    # An over-cap record is reported by the reader without its true end. It
    # finished if another record follows it; a last one finished only if the
    # file ends in a line break, since one kiro-cli never finished writing is
    # no message.
    oversized_turn = False
    for start, _end, record in records:
        too_long += oversized_turn
        oversized_turn = False
        if record is None:
            skipped_role = _turn_head(peek, start, fmt)
            if skipped_role:
                # A turn left out still separates the turns around it: a reply
                # after a skipped prompt is a new row, never joined to the
                # reply before it.
                oversized_turn = True
                last_role = skipped_role
            continue
        head = record[: fmt.head_bytes]
        role = next((r for marker, r in fmt.kinds.items() if marker in head), "")
        if not role:
            continue
        try:
            entry = json.loads(record)
        except (ValueError, RecursionError):
            # A partial trailing line, or one nested past the parser's depth
            # (an agent can write the log): either is no turn to bring in.
            continue
        text = fmt.text(entry)
        if text is None:
            continue
        if not text:
            if role == "user":
                # A prompt with no text (an image alone) is still a turn: the
                # reply after it is a new row.
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
    # Every row through the delivery chain's own sanitizer, whatever role it
    # claims: kiro-cli's log is writable by any sandboxed kiro-cli, so a
    # "Prompt" in it is not proof the owner typed it, and a user row is stored
    # and shown as written. The same rule a queued message from anyone but the
    # session's own human follows (chat_delivery.queued_text_for_display).
    from kiro_crew.dashboard.chat_delivery import sanitize_outbound

    entries += [
        (role, sanitize_outbound(text), cls, f"{import_id}:{n}", dict(_ORIGIN_META))
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


def _persisted_import_tags(conversation_log: object, key: str) -> set[str]:
    """The import tags already on disk for chat transcript *key*. Blocking.

    The current file and the rows size rotation moved into the archive: an
    import can carry more rows than rotation keeps, so a retry after a rotation
    must still see the rows it wrote before it. An archive that cannot be read
    is logged and left out rather than refusing the chat until it can be read.
    """
    log: Any = conversation_log
    tags = _imported_tags(log.read_messages(key))
    try:
        tags |= _imported_tags(log.read_rotated_messages_chained(key))
    except OSError:
        logger.warning(
            "kiro-cli: could not read the chat's rotated transcript; a retried "
            "import checks only the current file",
            exc_info=True,
        )
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
        have = _persisted_import_tags(log, key)
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


#: Times an import re-reads when the chat tab it targets changed under its reads.
_IMPORT_RESOLVE_ATTEMPTS = 3


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
    current: Callable[[], Any] | None = None,
) -> bool:
    """Record kiro-cli's *rows* in the chat, durably and at most once.

    Into *live* (the chat tab, when it is still this conversation) and then its
    transcript written, or else into the closed chat's transcript. *current*
    answers which chat tab holds this conversation NOW (``None`` when none
    does); it is asked again after the reads this awaits, so a chat closed, or
    replaced by a new one under its name, meanwhile is never written through. True once
    the rows are on disk, or when they have nowhere to go (the conversation
    was deleted); False when the write did not land, so the caller keeps the
    open's record to retry from. The count of rows *left_out* and *notices*
    are rows of the same import (:func:`_import_entries`), so both paths keep
    them, and a row a previous attempt already wrote is skipped by its tag.
    """
    from kiro_crew.dashboard.chat_utils_awaits import drained_to_thread
    from kiro_crew.dashboard.state import load_window_off_loop

    tagged = _import_entries(rows, import_id, left_out=left_out, notices=notices)
    resolve = current if current is not None else (lambda: live)
    try:
        have: set[str] = set()
        for _attempt in range(_IMPORT_RESOLVE_ATTEMPTS):
            if live is None:
                break
            # A chat restored lazily after a restart has not read its
            # transcript yet: read it off the loop before its rows are touched.
            await load_window_off_loop(live)
            # The chat's window holds only its newest rows, so rows an earlier
            # attempt saved are looked for on disk too, rotated ones included.
            log = getattr(state, "conversation_log", None)
            have = (
                await drained_to_thread(_persisted_import_tags, log, history_key)
                if log is not None
                else set()
            )
            # Asked after the last await, with none before the appends: the
            # tab these rows go to is the one holding the conversation now.
            now = resolve()
            if now is live:
                break
            live = now
        else:
            # The tab kept changing under the reads: the guarded transcript
            # write below, which checks the conversation itself, takes them.
            live = None
        if live is not None:
            have |= _imported_tags(getattr(live, "messages", None))
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


def _user_agent_spec_path(agent: str) -> Path | None:
    """The user-level spec file that declares *agent*, resolved the way a
    session of it is (a declared ``name`` wins over a file name, and
    ``agent.spec_pins`` picks between specs declaring one name), or ``None``.
    Blocking. Raises ``ValueError`` when two specs declare it and no pin
    chooses, or the pin names none of them."""
    from kiro_crew.agent import session_agent_spec_path

    return session_agent_spec_path(agent)


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


def _configured_backend(session_key: str) -> str | None:
    """The harness the chat's session would start on, a crewmate's harness pin
    included, or ``None`` for a pin this gateway cannot start (that session
    starts nowhere, so there is nothing to open). Blocking (config read)."""
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.crewmate_harness import CrewmateHarnessUnavailable, crewmate_backend_pin
    from kiro_crew.members import select_provider_backend

    cfg = KiroCrewConfig.load()
    try:
        return select_provider_backend(
            session_key,
            cfg.agent.member_acp_backend,
            cfg.agent.acp_backend,
            crew_backend=crewmate_backend_pin(cfg, session_key),
        )
    except CrewmateHarnessUnavailable:
        return None


def _configured_backend_is_kas(session_key: str) -> bool:
    """Whether the chat's session would start on the Kiro Agent harness. Blocking."""
    from kiro_crew.acp_backends import ACP_BACKEND_KAS

    return _configured_backend(session_key) == ACP_BACKEND_KAS


def _engine_for(session_key: str, label: str) -> str | None:
    """:data:`ENGINE_V3` when chat *session_key*'s conversation is one kiro-cli
    may open, else ``None``. Blocking (config read).

    Only the Kiro Agent harness opens, and both the harness that wrote the
    conversation (the session map's *label*) and the one the chat would start
    on now must be it: a chat whose harness changed since starts a new
    conversation, so the old one is not its own. The Kiro CLI harness, Claude
    Code, Codex and every other harness are refused.
    """
    from kiro_crew.agent_sdk.kas_tool_names import is_kas_provider_label

    if is_kas_provider_label(label) and _configured_backend_is_kas(session_key):
        return ENGINE_V3
    return None


def _files_resumable(sid: str) -> bool:
    return kiro_cli_launch_v3.files_resumable(sid)


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

    The copy is the chat's agent in the format kiro-cli v3 loads, with every
    pre-approval dropped and its own deny and ask rules kept, so kiro-cli asks
    the owner before each tool (:func:`.kiro_cli_launch_v3.launch_view`). It
    sits beside the original so a relative ``file://`` prompt still resolves,
    and is written owner-only.
    """
    source, spec = _read_user_agent_spec(agent)
    view = kiro_cli_launch_v3.launch_view(agent, Path(target).stem, spec, source.parent)
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
    the session map). Fails closed: a map that cannot answer counts as yes.

    The Slack read goes first: it takes the map's lock, so a mirror link whose
    claim (``chat_mirror``, inside that lock) checked before the open's hold has
    landed by the time the mirror flag is read after it."""
    try:
        thread_ts, _channel = getattr(sessions, "get_slack_link")(key)
        if thread_ts:
            return True
        return bool(getattr(sessions, "mirror_accepts_inbound")(key))
    except Exception:
        logger.debug("kiro-cli: could not read the chat's channel links", exc_info=True)
        return True


#: This feature's name under ``dashboard.feature_previews`` (``FEATURE_PREVIEW_FLAGS``).
PREVIEW_NAME = "open_in_kiro_cli"


async def preview_on() -> bool:
    """Whether the "Open in kiro-cli" Feature Preview is on. Off by default.

    Read from the config watcher's last applied copy, so a switch flipped in
    Settings counts at the next open with no restart; from disk, off the loop,
    in a process with no watcher. Anything unreadable reads as off: this gate
    decides whether a chat may leave Crew's sandbox for a terminal.
    """
    from kiro_crew.config import live as config_live

    cfg = config_live.snapshot()
    if cfg is None:
        from kiro_crew.config.loader import KiroCrewConfig

        try:
            cfg = await asyncio.to_thread(KiroCrewConfig.load)
        except Exception:
            logger.warning("kiro-cli: could not read the config; the preview reads as off")
            return False
    previews = getattr(getattr(cfg, "dashboard", None), "feature_previews", None)
    return isinstance(previews, dict) and previews.get(PREVIEW_NAME) is True


async def plan_kiro_cli_launch(request: web.Request, slot_name: str) -> KiroCliPlan:
    """Read-only: what opening chat *slot_name* in kiro-cli would run, or why not.

    Every input is the gateway's own: the slot it holds, its session map, its
    config and the files on disk. Run again by the open itself, inside its locks.
    Refused first while the Feature Preview is off; that gates only new opens,
    so a chat a terminal already holds still comes back when kiro-cli exits.
    """
    from kiro_crew.dashboard.chat_utils import effective_session_key
    from kiro_crew.messaging.link import is_channel_session_key

    if not await preview_on():
        return KiroCliPlan(code=CODE_PREVIEW_OFF)
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
    # The Kiro Agent harness only (kiro-cli's v3 store): the Kiro CLI harness,
    # Claude Code, Codex and any other harness are refused here, where every
    # handover passes, not only hidden in the menu.
    engine = await asyncio.to_thread(_engine_for, session_key, label)
    if engine is None:
        return KiroCliPlan(code=CODE_NOT_KIRO, session_key=session_key)
    if not sid or not kiro_cli_launch_v3.SID_RE.match(sid):
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
    if await asyncio.to_thread(kiro_cli_launch_v3.workspace_config_refusal, cwd):
        return KiroCliPlan(code=CODE_PROJECT_AGENT, session_key=session_key, sid=sid)
    return KiroCliPlan(
        session_key=session_key,
        sid=sid,
        cwd=cwd,
        binary=binary,
        agent=agent,
        agent_without_spec="" if agent else wanted[:128],
        engine=engine,
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
    ``await launch.arelease()`` if the terminal does not start (its session teardown
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
    from kiro_crew.dashboard.state import load_window_off_loop
    from kiro_crew.dashboard.system_notices import SESSION_SWITCH_KIND

    plan = await plan_kiro_cli_launch(request, slot_name)
    if not plan.ok:
        return None, plan.code
    state = request.app["state"]
    slot = state._slots.get(slot_name)
    if slot is None:
        return None, CODE_UNKNOWN_CHAT
    # A chat restored lazily after a restart reads its transcript off the loop,
    # before anything below (or the hold's notices) touches its rows.
    await load_window_off_loop(slot)
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

        def _post(live: Any, text: str) -> None:
            live.append("assistant", text, "msg msg-a", meta={"kind": SESSION_SWITCH_KIND})
            state.push_slots_update()

        async def _post_loaded(live: Any, text: str) -> None:
            await load_window_off_loop(live)
            # Still this conversation's tab after the read, or the notice
            # would land in a chat made anew under the name meanwhile.
            if _same_chat() is live:
                _post(live, text)

        def _notify(text: str) -> None:
            # Runs from the terminal's open and teardown, on the loop. The chat
            # may be gone, or reopened lazily from disk with its transcript not
            # yet read: that read goes off the loop and the notice follows it.
            live: Any = _same_chat()
            if live is None:
                return
            if getattr(live, "window_pending", False) is not True:
                _post(live, text)
                return
            task = asyncio.ensure_future(_post_loaded(live, text))
            state._background_tasks.add(task)
            task.add_done_callback(state._background_tasks.discard)

        def _drain_queue() -> None:
            # Runs from the hold's release, on the loop. Entries queued while the
            # terminal held the chat were left in place (the queue drain does not
            # start a turn admission would refuse); start them now, the way a
            # queue parked on memory preparation is started.
            from kiro_crew.dashboard.chat_runner import _drain_parked_queues

            live: Any = _same_chat()
            if live is None or not getattr(live, "_queue", None):
                return

            async def _drain_loaded() -> None:
                # The drain writes the next entry's row: a lazily restored
                # chat reads its transcript off the loop first.
                await load_window_off_loop(live)
                if _same_chat() is live:
                    await _drain_parked_queues(state, [live.key])

            try:
                drain = asyncio.ensure_future(_drain_loaded())
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
                current=_same_chat,
            )

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
            import_id=import_id,
            engine=plan.engine,
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
                    await launch.arelease()
                    return None, CODE_STARTING
            await _reset_slot_session(state, slot, session_key, skip_if_busy=True)
            if state.sessions.get_provider(session_key) is not None:
                # The reset declined, or a process registered after it: a turn
                # that slipped in after the busy read, or (no turn running) a
                # start that was not the pre-spawn's.
                await launch.arelease()
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
                await launch.arelease()
                return None, CODE_BUSY
            # A channel link made after the plan's check: read again now the hold
            # stands. A link written after this read sees the hold and refuses
            # (``link_slot_to_slack``, the mirror link), so one side always yields.
            if await asyncio.to_thread(_channel_reaches, state.sessions, session_key):
                await launch.arelease()
                return None, CODE_UNSUPPORTED
            launch.sid = plan.sid
            if plan.agent:
                # The terminal runs outside Crew's sandbox, so the chat's agent
                # goes in without its tool pre-approvals: kiro-cli asks the
                # owner before each tool. Same agent otherwise (prompt, MCP
                # servers, model), under a per-open name.
                launch.launch_spec = await asyncio.to_thread(_launch_spec_path)
            try:
                session_agent = await asyncio.to_thread(
                    kiro_cli_launch_v3.read_session_agent, plan.sid
                )
            except (OSError, ValueError):
                logger.warning("kiro-cli: could not read the session's envelope", exc_info=True)
                await launch.arelease()
                return None, CODE_SESSION_FILE
            # Measured once the chat's own process is gone: what kiro-cli
            # appends after this point is what the chat takes back.
            try:
                launch.log_offset = await asyncio.to_thread(kiro_cli_launch_v3.log_size, plan.sid)
            except OSError:
                logger.warning("kiro-cli: could not read where the session log ends", exc_info=True)
                await launch.arelease()
                return None, CODE_SESSION_FILE
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
                        "engine": plan.engine,
                    },
                )
            except OSError:
                logger.warning("kiro-cli: could not record the open", exc_info=True)
                await launch.arelease()
                return None, CODE_RECORD
            launch.recorded = True
            # Written only now that the record names them, so a gateway that
            # dies anywhere below leaves its restart what it needs to undo them.
            if plan.agent:
                try:
                    await drained_to_thread(_write_launch_spec, plan.agent, launch.launch_spec)
                except (OSError, ValueError):
                    logger.warning("kiro-cli: could not write the launch spec", exc_info=True)
                    await launch.arelease()
                    return None, CODE_LAUNCH_SPEC
            # kiro-cli's resume runs the agent ``session.json`` names
            # (``agentMode``), not ``--agent``: it names the launch copy (or
            # kiro-cli's default) until the release puts it back.
            launch.session_agent = session_agent
            try:
                await drained_to_thread(
                    kiro_cli_launch_v3.point_session_agent,
                    plan.sid,
                    Path(launch.launch_spec).stem if launch.launch_spec else None,
                )
            except (OSError, ValueError):
                logger.warning("kiro-cli: could not point the session at its agent", exc_info=True)
                await launch.arelease()
                return None, CODE_SESSION_FILE
        except BaseException:
            await launch.arelease()
            raise
    return launch, ""


_UNREADABLE_NOTICE = (
    "What was said in kiro-cli while this chat was open there could not be added "
    "here: its session log in ~/.kiro/sessions is no longer a file Crew can "
    "read. This chat continues from what it already shows."
)

_RECOVERED_NOTICE = (
    "What you said in kiro-cli while this chat was open there is above. It could "
    "not be added when kiro-cli ended (Kiro Crew restarted, or the chat could not "
    "be saved), and this chat continues the conversation from there."
)


def _left_open_turns(record: dict) -> tuple[list[tuple[str, str]], int]:
    """The turns an open cut short by a restart left in kiro-cli's log, read
    from the record's own offset, with the session's ``session.json`` put back
    and the launch copy it left removed. Blocking. Raises ``OSError`` when the
    session cannot be read; :class:`_SessionFileUnusable` for one that never
    can (a record of any store but v3 included). The session is found from
    its id, never from a path in the record."""
    from kiro_crew.config.paths import kiro_agents_dir

    sid = record.get("sid")

    def _remove_copy() -> None:
        spec = record.get("launch_spec")
        if isinstance(spec, str) and spec:
            path = Path(spec)
            if path.parent == kiro_agents_dir() and path.name.startswith(_LAUNCH_SPEC_PREFIX):
                _remove_launch_spec(str(path))

    if record.get("engine") != ENGINE_V3 or not isinstance(sid, str):
        # Only a Kiro Agent chat opens: a record naming anything else is no
        # session this can read or put back, and keeping it would refuse the
        # chat for good.
        _remove_copy()
        raise _SessionFileUnusable("the open's record names no Kiro Agent session")
    # Before the copy goes: a session still naming it would resume the chat
    # on an agent that is gone. A session that can never be read again takes
    # its record with it, so the copy goes then too: nothing else would remove it.
    try:
        kiro_cli_launch_v3.restore_session_agent(sid, record.get("session_agent"))
    except _SessionFileUnusable:
        _remove_copy()
        raise
    _remove_copy()
    offset = record.get("log_offset")
    if not kiro_cli_launch_v3.SID_RE.match(sid) or type(offset) is not int:
        return [], 0
    return kiro_cli_launch_v3.read_turns(sid, max(offset, 0))


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

    # No record, nothing to settle: one stat, no yield and no lock entry, so a
    # caller with no open (nearly every send and request) is unchanged. Only
    # forget_open removes a record, inside the lock after its writes.
    if not kiro_cli_handoff.has_left_open(session_key):
        return False
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
        # Asked again on the loop, after the read: an open that took its hold
        # while the worker read can have written this record, and it is that
        # live open's to settle. Putting its session file back and removing its
        # launch copy now would hand the terminal the agent's pre-approvals.
        if kiro_cli_handoff.is_held_any_spelling(session_key):
            return False
        try:
            if state is None:
                # SessionManager callers have no dashboard state: use the live
                # one when there is one; an API-only gateway still owns the
                # durable transcript.
                from types import SimpleNamespace

                from kiro_crew.history import ConversationLog
                from kiro_crew.slack.handler import get_dashboard_state

                state = get_dashboard_state()
                if state is None:
                    state = SimpleNamespace(
                        conversation_log=await asyncio.to_thread(ConversationLog)
                    )
            if slot is None:
                # A caller with no tab: the tab showing this conversation now, if
                # any, so it shows the rows at once and its next save keeps them.
                hist, made = str(record.get("history_key") or ""), str(
                    record.get("created_at") or ""
                )
                slots = getattr(state, "_slots", None)
                if hist and isinstance(slots, dict):
                    slot = next(
                        (
                            s
                            for s in slots.values()
                            if slot_history_key(s) == hist
                            and (
                                not made
                                or str(getattr(s, "_disk_meta_created_at", "") or "") == made
                            )
                        ),
                        None,
                    )
            # Drained here and below: a cancelled recovery keeps the lock until
            # its own envelope restore and record removal have finished, so no
            # later open or recovery runs under a worker still writing.
            turns, left_out = await drained_to_thread(_left_open_turns, record)
            notices: tuple[str, ...] = (_RECOVERED_NOTICE,)
        except _SessionFileUnusable:
            # Never readable, so retrying would refuse the session for good: the
            # chat is told, and the record goes once that notice is written.
            logger.warning("kiro-cli: a left open's log is not a file it can read", exc_info=True)
            turns, left_out, notices = [], 0, (_UNREADABLE_NOTICE,)
        except OSError:
            # The envelope or the log could not be read: the record stays for
            # the next try.
            logger.warning("kiro-cli: could not settle a left open", exc_info=True)
            return True
        history_key = str(record.get("history_key") or "")
        created_at = str(record.get("created_at") or "")
        import_id = str(record.get("import_id") or "") or f"open-{record.get('log_offset')}"
        rows = [(r, t, "msg msg-u" if r == "user" else "msg msg-a") for r, t in turns]

        def _conversation_tab(candidate: Any) -> Any:
            if (
                candidate is not None
                and history_key
                and slot_history_key(candidate) == history_key
                and (
                    not created_at
                    or str(getattr(candidate, "_disk_meta_created_at", "") or "") == created_at
                )
            ):
                return candidate
            return None

        slot_key = getattr(slot, "key", None)

        def _current_tab() -> Any:
            # The tab under the chat's key now, if it still holds this
            # conversation: the reads above and in the import await.
            slots = getattr(state, "_slots", None)
            if not isinstance(slots, dict) or not isinstance(slot_key, str):
                return None
            return _conversation_tab(slots.get(slot_key))

        done = not history_key or await _import_turns(
            state,
            _conversation_tab(slot),
            history_key,
            created_at,
            rows,
            import_id,
            left_out=left_out,
            notices=notices,
            current=_current_tab if slot_key is not None else None,
        )
        if done:
            await drained_to_thread(
                functools.partial(
                    kiro_cli_handoff.forget_open,
                    session_key,
                    import_id=str(record.get("import_id") or ""),
                )
            )
        return True


async def settle_session_left_open(*session_keys: str) -> None:
    """Settle the left opens of *session_keys* for a caller with no dashboard
    tab (the SessionManager's allocation and discard), ahead of their refusal
    reads. A held key is its live open's to settle. A failure keeps the record,
    and the caller's own worded refusal then says why."""
    for key in dict.fromkeys(session_keys):
        if kiro_cli_handoff.is_held(key) or not kiro_cli_handoff.has_left_open(key):
            continue
        try:
            await recover_left_open(None, None, key)
        except Exception:  # noqa: BLE001 -- the record stays for the next try
            logger.warning("kiro-cli: could not settle a left open", exc_info=True)


async def settle_before_rewrite(state: Any, slot: Any) -> None:
    """Bring in an open's left turns before a rewrite of chat *slot*'s history
    or conversation (a rewind, regenerate, variant switch, edit-resend or
    conversation reset), so the rewrite treats them like every other row instead
    of a later recovery adding them after it. A record this cannot settle is
    then refused under the slot's lock (:func:`kiro_cli_handoff.handover_refusal`)."""
    from kiro_crew.dashboard.chat_utils import effective_session_key

    await recover_left_open(state, slot, effective_session_key(slot))


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
