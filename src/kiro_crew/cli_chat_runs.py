"""The terminal chats ``kirocrew chat --list`` prints and ``--continue`` reopens.

Each ``kirocrew chat`` run that completes a turn keeps one entry in
``<data home>/cli_chat_runs.json``: the backend session id, the directory the
command was started in, the last message time, the first prompt line, the
``--agent`` name when one was given, and the run's process identity. Nothing else
reads the file; the dashboard History does not list these chats.

``--continue`` reopens the newest entry for the current directory and the same
``--agent`` through the backend's own session restore, on KAS and kiro-cli only.
When the session does not come back it says so and exits non-zero: it never
starts a new chat in its place.

The file sits in a data home the agent can write, so an entry can choose which
session is loaded (a plain id token and text within its length bounds; any other
entry is ignored), refuse it as still open, and set the time and first prompt line
the restore prints. It never chooses the agent: a continued chat's agent always
comes from the command line.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path
from typing import Any, Iterator, Protocol, TypeGuard, cast

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write, read_bytes_with_retry
from kiro_crew.config.paths import data_home
from kiro_crew.platform_lock_compat import file_lock, open_lock_file
from kiro_crew.terminal_safe import safe_terminal_line

logger = logging.getLogger(__name__)

RUNS_FILE = "cli_chat_runs.json"
#: The newest entries kept; an older chat drops off the list.
MAX_RUNS = 50
#: How much of the first prompt line an entry keeps, and so ``--list`` shows.
FIRST_PROMPT_CHARS = 80
#: The longest directory, ``--agent`` name or process token an entry keeps; any Windows path fits.
MAX_FIELD_CHARS = 32_768


class CannotContinue(Exception):
    """``--continue`` cannot reopen the chat; the message says why."""


class _Provider(Protocol):
    """What a run reads off the chat's live provider."""

    @property
    def session_id(self) -> str: ...

    async def start(self) -> None: ...


@dataclass(frozen=True)
class RunEntry:
    session_id: str
    cwd: str
    last_at: float
    first_prompt: str
    #: The ``--agent`` name the run was given; ``""`` when it named none.
    agent: str
    pid: int
    #: ``platform_compat.own_process_start_time()`` of that run; ``""`` where the
    #: platform has no reboot-unique token.
    pid_token: str


def _is_id_token(value: object) -> TypeGuard[str]:
    """Whether *value* is one plain session id (letters, digits, ``-`` and ``_``)."""
    return (
        isinstance(value, str)
        and 0 < len(value) <= 128
        and value.isascii()
        and value.replace("-", "").replace("_", "").isalnum()
    )


def _is_text(value: object, limit: int) -> TypeGuard[str]:
    return isinstance(value, str) and len(value) <= limit


def _when(last_at: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(last_at))


def _is_time(value: object) -> TypeGuard[int | float]:
    """*value* is a time :func:`_when` can show (JSON also holds ``Infinity``)."""
    if not isinstance(value, (int, float)):
        return False
    try:
        _when(float(value))
    except (OverflowError, OSError, ValueError):
        return False
    return True


def _entry(raw: object) -> RunEntry | None:
    """*raw* as an entry, or ``None`` when it is not one."""
    if not isinstance(raw, dict):
        return None
    sid, cwd, last_at, first_prompt, agent, pid, pid_token = (
        raw.get(f.name) for f in fields(RunEntry)
    )
    if not (
        _is_id_token(sid)
        and _is_text(cwd, MAX_FIELD_CHARS)
        and _is_text(first_prompt, FIRST_PROMPT_CHARS)
        and _is_text(agent, MAX_FIELD_CHARS)
        and _is_text(pid_token, MAX_FIELD_CHARS)
        and _is_time(last_at)
        and isinstance(pid, int)
    ):
        return None
    return RunEntry(sid, cwd, float(last_at), first_prompt, agent, pid, pid_token)


def _path() -> Path:
    return data_home() / RUNS_FILE


def _read() -> list[RunEntry]:
    """The recorded chats, newest first; an absent or damaged file holds none.

    Any other read failure raises, so a write never replaces entries it could not read.
    """
    try:
        raw = json.loads(read_bytes_with_retry(_path()))
    except (FileNotFoundError, ValueError):
        return []
    entries = (_entry(item) for item in raw) if isinstance(raw, list) else ()
    return [entry for entry in entries if entry is not None]


def read_runs() -> list[RunEntry]:
    """The recorded chats, newest first; an unreadable file holds none."""
    try:
        return _read()
    except OSError:
        return []


@contextmanager
def _locked() -> Iterator[None]:
    """Serialize the read-modify-write, so two terminals cannot drop an entry."""
    lock_path = _path().with_name(RUNS_FILE + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open_lock_file(lock_path) as fd, file_lock(fd):
        yield


def _save(entries: list[RunEntry]) -> None:
    payload = json.dumps([asdict(e) for e in entries[:MAX_RUNS]], indent=2)
    atomic_write(_path(), payload + "\n")


def _here() -> str:
    return str(Path.cwd().resolve())


def _first_line(text: str) -> str:
    lines = text.strip().splitlines()
    return lines[0][:FIRST_PROMPT_CHARS] if lines else ""


def print_runs() -> None:
    """Print the recorded chats for ``--list``."""
    entries = read_runs()
    if not entries:
        print("No terminal chats recorded yet.")
    for entry in entries:
        when = _when(entry.last_at)
        agent = f"  --agent {entry.agent}" if entry.agent else ""
        line = f"{entry.session_id}  {when}  {entry.cwd}{agent}  {entry.first_prompt}"
        print(safe_terminal_line(line))


def choose_resume(agent: str | None) -> RunEntry:
    """The newest chat in this directory for *agent*, claimed by this process.

    The claim names this process on the entry at once, so a second ``--continue``
    sees the chat as open even before this run completes a turn.
    """
    wanted = agent or ""
    try:
        here = _here()
        with _locked():
            entries = _read()
            entry = next((e for e in entries if e.cwd == here and e.agent == wanted), None)
            if entry is None:
                named = f" for --agent {wanted}" if wanted else " started without --agent"
                raise CannotContinue(f"No earlier terminal chat in this directory{named}.")
            alive = platform_compat.process_incarnation_id(entry.pid)
            if entry.pid_token and alive == entry.pid_token:
                raise CannotContinue(
                    f"That chat is still open in another terminal (process {entry.pid}); "
                    "finish it there first."
                )
            token = platform_compat.own_process_start_time() or ""
            claimed = replace(entry, pid=os.getpid(), pid_token=token)
            _save([claimed if e is entry else e for e in entries])
    except OSError as exc:
        raise CannotContinue(f"Cannot pick the earlier chat: {exc}") from None
    return claimed


def _on_kiro_harness(provider: _Provider) -> bool:
    # Imported here: the context stack is heavy, and ``cli_chat`` loads this module eagerly.
    from kiro_crew.cli_chat_context import _on_kiro_harness as on_kiro

    return on_kiro(provider)


@dataclass
class ChatRun:
    """One ``kirocrew chat`` run: the chat it reopens, if any, and the entry it keeps."""

    #: The ``--agent`` name given on the command line, ``""`` for none.
    agent: str = ""
    #: The entry ``--continue`` chose; ``None`` for a new chat.
    resume: RunEntry | None = None
    #: The session this run's entry names; ``""`` until a turn completed.
    session_id: str = ""
    first_prompt: str = ""


def _record(run: ChatRun, session_id: str, at: float) -> None:
    """Write *run*'s entry for *session_id*, replacing the one it kept before."""
    with _locked():
        entries = _read()
        replaced = {run.session_id, session_id}
        earlier = next((e for e in entries if e.session_id in replaced), None)
        first = earlier.first_prompt if earlier else run.first_prompt
        token = platform_compat.own_process_start_time() or ""
        entry = RunEntry(session_id, _here(), at, first, run.agent, os.getpid(), token)
        kept = [entry, *(e for e in entries if e.session_id not in replaced)]
        # A stable sort: on a tie the entry just written stays first.
        _save(sorted(kept, key=lambda e: e.last_at, reverse=True))


async def start_run(provider: _Provider, run: ChatRun | None) -> None:
    """Start *provider*, restoring the chat *run* continues.

    Raises :class:`CannotContinue` before the start on a backend other than KAS
    and kiro-cli, and after it when the backend came back on any session but the
    one asked for: a load that fails falls through to a fresh session, which
    ``--continue`` must not hand the person as their earlier chat. The caller's
    ``finally`` shuts the provider down either way.
    """
    resume = run.resume if run is not None else None
    if resume is None:
        await provider.start()
        return
    if not _on_kiro_harness(provider):
        raise CannotContinue("--continue works on the KAS and Kiro CLI backends only.")
    cast(Any, provider).client.set_resume_session_id(resume.session_id)
    await provider.start()
    # Read after the start: the kiro path swaps in the client that served the load.
    restored = cast(Any, provider).client.resumed is True
    if not (restored and provider.session_id == resume.session_id):
        sid = resume.session_id
        raise CannotContinue(f"The earlier chat {sid} could not be restored; nothing was sent.")
    when = _when(resume.last_at)
    line = f"↪ Continuing your chat from {when}: {resume.first_prompt}"
    print(safe_terminal_line(line), file=sys.stderr)


async def note_turn(run: ChatRun | None, provider: _Provider, message: str) -> None:
    """Record that a turn the person typed as *message* completed on *provider*."""
    if run is None or not _is_id_token(session_id := provider.session_id):
        return
    run.first_prompt = run.first_prompt or _first_line(message)
    try:
        await asyncio.to_thread(_record, run, session_id, time.time())
    except Exception as exc:
        # The record only serves a later --continue; this chat goes on without it.
        logger.warning("Could not record this terminal chat for --continue: %s", exc)
        return
    run.session_id = session_id
