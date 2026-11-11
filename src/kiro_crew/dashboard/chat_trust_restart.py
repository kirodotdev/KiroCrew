"""Telling the owner that a gateway restart ended a chat's approval mode.

A chat's Trust and Read modes (``_ChatSlot._trust`` / ``._trust_reads``) live in
memory only and are never persisted (session.md), so every exit ends them: the
in-place re-exec behind the dashboard Restart button and an applied update,
``kirocrew restart``, and a supervisor relaunch after a crash. That is the design,
and this module keeps it. What it adds is the notice: without one, a loop or a
long task left running in a trusted chat waits on an approval prompt nobody is
watching, and the owner finds it stalled with no word of why.

* :func:`record` writes which open chats hold a mode to :data:`RECORD_FILE` in the
  data home. The periodic flush and the shutdown save call it, off the event loop,
  so a crash loses at most one flush interval and a chosen restart loses nothing.
* :func:`arm` runs at boot before the restore: it takes the previous process's
  record (reads it once and deletes it) and only then lets this process write.
* :func:`report` runs after the restore and posts one bell note and one chat row
  for each recorded chat the restore rebuilt without its mode.

A chat whose tools still run unasked with no one acting is owed nothing, so it
gets no notice. An app-owned chat (``_ChatSlot._app``) is never recorded: its mode
is the app's to set, not the owner's, and the app that drives it sets it again (an
app's watchdog can give its worker Trust back on every poll). At report time a chat
is skipped when a live auto-approve grant reaches it (a declared one boot armed
again before the restore), and a crewmate's conductor chat or worker is skipped
when its Profile (``agents.<name>.approval_mode``) seeds the recorded mode back at
its next turn (``crewmate_approval.seed_for_turn``).

The record grants nothing. No reader turns it back into a mode, so a forged one
can at most cause a spurious notice, and only on a chat that is open; the notice
names the live chat's own title, never text from the file. That is why it is not
signed and needs no sandbox fence.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import config_dir

logger = logging.getLogger(__name__)

#: The record of which chats held an approval mode, under the data home.
RECORD_FILE = "chat_trust_held.json"

_RECORD_VERSION = 1

#: One key and one mode per trusted chat is far below this. The bound keeps a file
#: this module did not write from turning the boot read into an unbounded one.
_MAX_RECORD_BYTES = 256 * 1024

#: The per-state writer :func:`arm` attaches; its absence is what keeps
#: :func:`record` from writing before the previous record was taken.
_WRITER_ATTR = "_chat_trust_record"

#: The chat row's ``meta["kind"]``, a wire value the dashboard keys its localized
#: card on (``TrustResetNoticeCard.tsx``). The card's one button sends the row's
#: ``meta["approval_mode"]`` through the picker's own route, so the person, not
#: this module, turns the mode back on. The row text stays the fallback for
#: readers that cannot localize.
ROW_KIND = "trust_reset_notice"

#: The modes a restart ends, with the name the chat's approval picker shows.
_MODE_LABELS = {"trust": "Trust", "trust_reads": "Read"}


@dataclass(frozen=True, order=True)
class HeldMode:
    """One open chat that held an approval mode."""

    key: str
    mode: str


@dataclass
class _Writer:
    lock: threading.Lock = field(default_factory=threading.Lock)
    # What this process last wrote. Empty at arm, because arm deletes the file.
    written: frozenset[HeldMode] = frozenset()


def _held_mode(slot: Any) -> str:
    if getattr(slot, "_trust", False) is True:
        return "trust"
    if getattr(slot, "_trust_reads", False) is True:
        return "trust_reads"
    return ""


def _held_now(state: Any) -> frozenset[HeldMode]:
    """The open chats that hold a mode now, among those a restart restores."""
    building = getattr(state, "_slots_under_construction", ())
    held: set[HeldMode] = set()
    for key, slot in list(getattr(state, "_slots", {}).items()):
        # The same set ``open_slots.json`` keeps: an incognito or temporary chat
        # does not come back, so there is no chat to tell.
        if getattr(slot, "memory_mode", "persistent") != "persistent" or key in building:
            continue
        # An app sets its own chat's mode again after a restart, so the owner is
        # owed no notice for it (see the module docstring).
        if getattr(slot, "_app", ""):
            continue
        mode = _held_mode(slot)
        if mode:
            held.add(HeldMode(key, mode))
    return frozenset(held)


def record(state: Any) -> None:
    """Write which open chats hold an approval mode, when that set changed.

    Writes nothing before :func:`arm` ran on *state*, so this process cannot
    overwrite the previous process's record before it is read. Never raises: a
    lost record costs the owner a notice, never a grant.
    """
    writer = getattr(state, _WRITER_ATTR, None)
    if not isinstance(writer, _Writer):
        return
    try:
        # Sampled under the lock, so the flush thread and the shutdown thread
        # cannot land an older sample after a newer one.
        with writer.lock:
            held = _held_now(state)
            if held == writer.written:
                return
            path = config_dir() / RECORD_FILE
            if held:
                chats = [{"key": h.key, "mode": h.mode} for h in sorted(held)]
                payload = json.dumps({"version": _RECORD_VERSION, "chats": chats})
                atomic_write(path, payload, mode=0o600)
            else:
                path.unlink(missing_ok=True)
            writer.written = held
    except Exception:
        logger.debug("Could not record the chats holding an approval mode", exc_info=True)


def _read_record(path: Path) -> str | None:
    """The record's text, or ``None`` when there is no regular file to read.

    Opened by descriptor, non-blocking and without following a link, and read only
    when ``fstat`` says it is a regular file under the bound: a FIFO planted at the
    path would otherwise block the boot read forever. Windows has neither flag, so
    a link is refused there by the ``is_symlink`` check before the open.
    """
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        if path.is_symlink():
            return None
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_RECORD_BYTES:
            return None
        return os.read(fd, _MAX_RECORD_BYTES).decode("utf-8", errors="replace")
    finally:
        os.close(fd)


def _parse(text: str) -> tuple[HeldMode, ...]:
    try:
        data = json.loads(text)
    except ValueError:
        return ()
    if not isinstance(data, dict) or data.get("version") != _RECORD_VERSION:
        return ()
    chats = data.get("chats")
    if not isinstance(chats, list):
        return ()
    held: dict[HeldMode, None] = {}
    for chat in chats:
        if not isinstance(chat, dict):
            continue
        key, mode = chat.get("key"), chat.get("mode")
        if isinstance(key, str) and key and mode in _MODE_LABELS:
            held[HeldMode(key, mode)] = None
    return tuple(held)


def arm(state: Any) -> tuple[HeldMode, ...]:
    """Take the previous process's record, then let this process write its own.

    Run once per boot, off the event loop, before the open-tab restore. The file
    is deleted whatever it holds, so one restart is reported once. Never raises.
    """
    held: tuple[HeldMode, ...] = ()
    try:
        path = config_dir() / RECORD_FILE
        try:
            text = _read_record(path)
            if text is not None:
                held = _parse(text)
        finally:
            path.unlink(missing_ok=True)
    except Exception:
        logger.debug("Could not take the previous chat approval-mode record", exc_info=True)
    setattr(state, _WRITER_ATTR, _Writer())
    return held


def note_title(mode: str) -> str:
    """The bell note's title for a chat whose *mode* a restart ended."""
    return f"{_MODE_LABELS[mode]} mode was turned off by a restart"


def note_body(mode: str, chat_title: str) -> str:
    """The bell note's body: what ended, what it means for running work, the remedy."""
    label = _MODE_LABELS[mode]
    return (
        f'Kiro Crew restarted, and a restart ends a chat\'s approval mode, so "{chat_title}" '
        f"is out of {label} mode and its tools ask for approval again. A loop or task "
        f"left running in it waits for that approval. Turn {label} mode back on in the "
        "chat if you still want it."
    )


def row_text(mode: str) -> str:
    """The system row shown in the chat itself."""
    label = _MODE_LABELS[mode]
    return (
        f"{label} mode was turned off because Kiro Crew restarted, so tools ask for "
        f"approval again. Turn {label} mode back on if you still want it."
    )


def _covers(granted: str, mode: str) -> bool:
    """Whether a chat holding *granted* still has *mode*: Trust covers Read."""
    return granted == mode or (granted == "trust" and mode in _MODE_LABELS)


def _auto_approved(state: Any, slot: Any) -> bool:
    """Whether a live auto-approve grant reaches a dashboard turn in *slot*.

    The same test the tool gate makes (``chat_turn.tool_approval``). Boot arms a
    declared grant (``agent.dangerously_skip_permissions``, ``gateway --approval
    yolo``) again before the restore, so a chat it reaches asks for nothing. The
    note would then be false, and its remedy harmful: Trust picked on one chat
    ends a declared grant for every other chat (``chat_api.slot_approval``).
    Never raises; an unanswerable check counts as no grant, so the note is posted.
    """
    try:
        return state.is_yolo_active() is True and state.yolo_reaches(slot) is True
    except Exception:
        logger.debug("Could not read the auto-approve grant for a restored chat", exc_info=True)
        return False


def _owed(state: Any, slot: Any, mode: str) -> bool:
    """Whether restored *slot* asks for approval where its recorded *mode* did not."""
    return not _covers(_held_mode(slot), mode) and not _auto_approved(state, slot)


def _profile_modes(slots: list[Any]) -> dict[str, str]:
    """The mode each crewmate chat in *slots* takes from its Profile, by slot key.

    The mode ``crewmate_approval.seed_for_turn`` gives a crewmate's conductor chat
    or worker at its next turn: ``agents.<name>.approval_mode``, an unset one read
    as ``MEMBER_APPROVAL_MODE_UNSET_EFFECTIVE``, clamped to what the policy allows.
    A chat no crewmate owns is absent. MAY BLOCK (the config, each session's
    record, a policy install): call it off the event loop. Never raises.
    """
    modes: dict[str, str] = {}
    try:
        from kiro_crew.approval_mode_policy import clamp_session_mode, ensure_resolved
        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.config.sections import MEMBER_APPROVAL_MODE_UNSET_EFFECTIVE
        from kiro_crew.dashboard.chat_utils import slot_history_key
        from kiro_crew.dashboard.crewmate_approval import crewmate_name_for
        from kiro_crew.execution_context import read_session_execution

        config = KiroCrewConfig.load()
        for slot in slots:
            try:
                execution = read_session_execution(slot_history_key(slot))
                name = crewmate_name_for(config, execution)
            except Exception:  # noqa: BLE001 - an unreadable record is no crewmate's
                continue
            if not name:
                continue
            if not modes:
                # A final policy answer before the clamp, as the seed waits for one.
                ensure_resolved()
            mode = getattr(config.agents.get(name), "approval_mode", "") or ""
            modes[slot.key] = clamp_session_mode(mode or MEMBER_APPROVAL_MODE_UNSET_EFFECTIVE)
    except Exception:
        logger.debug("Could not read the crewmate Profile modes of restored chats", exc_info=True)
    return modes


async def report(state: Any, held: tuple[HeldMode, ...]) -> None:
    """Tell the owner about each recorded chat the restore left without its mode.

    One ``agent`` bell note per chat, whose ``meta.slot`` gives the detail panel
    its open-chat action, and one system row in the chat. A chat that is not open,
    that holds the mode again, that a live auto-approve grant reaches, or that is
    a crewmate's chat its Profile seeds the mode back into at its next turn, is
    owed nothing. The mode itself is never restored. Never raises: boot must not
    fail over a notice.
    """
    if not held:
        return
    from kiro_crew.dashboard.state import load_window_off_loop

    lost: list[tuple[HeldMode, Any]] = []
    for entry in held:
        slot = state.get_slot(entry.key)
        if slot is not None and _owed(state, slot, entry.mode):
            lost.append((entry, slot))
    if not lost:
        return
    profile_modes = await asyncio.to_thread(_profile_modes, [slot for _, slot in lost])
    for entry, slot in lost:
        try:
            if state.get_slot(entry.key) is not slot or not _owed(state, slot, entry.mode):
                continue
            # The seed runs once per live slot, so only an unseeded chat gets it back.
            if not getattr(slot, "_member_approval_seeded", True) and _covers(
                profile_modes.get(slot.key, ""), entry.mode
            ):
                continue
            state.notify(
                "agent",
                note_title(entry.mode),
                note_body(entry.mode, slot.display_title),
                meta={"slot": slot.key, "approval_mode": entry.mode},
            )
            # A restored chat's window may still be on disk; appending reads it, so
            # the read happens off the loop first.
            await load_window_off_loop(slot)
            if state.get_slot(entry.key) is slot:
                slot.append(
                    "system",
                    row_text(entry.mode),
                    "msg msg-info",
                    meta={"kind": ROW_KIND, "approval_mode": entry.mode},
                )
        except Exception:
            logger.warning(
                "Could not report the restart-ended approval mode of chat %s",
                entry.key,
                exc_info=True,
            )
