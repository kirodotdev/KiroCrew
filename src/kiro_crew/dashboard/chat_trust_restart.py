"""A chat's Trust or Trust reads kept across a gateway exit the user did not choose.

An owner's Trust and Trust reads on a chat (``_ChatSlot._trust`` / ``._trust_reads``)
stay on across every exit the user did not choose: the in-place re-exec behind the
dashboard Restart button and an applied update, ``kirocrew restart``, a supervisor
relaunch after a crash or kill, and a host reboot. A stop the user chose ends them
(``kept_grants.boot_verdict``). A chat that comes back without its mode is told why,
because a loop or a long task left running in it would otherwise wait on an approval
prompt nobody is watching.

* :func:`record` writes the open chats that hold a mode to the signed
  ``kept_grants.CHATS_RECORD``. The periodic flush and the shutdown save call it off
  the event loop, and so does a write :func:`note_trust_changed` schedules right
  after any slot's mode changes (:func:`start_prompt_writer`), so a mode turned off
  just before a crash does not come back.
* :func:`arm` runs at boot before the restore: it takes the previous process's record
  and only then lets this process write.
* :func:`restore_or_report` runs after the restore. A kept chat gets its mode back
  through the picker's own writes, audited as ``mode_change:<mode>``, its workers
  follow it the way a picker pick makes them, and the chat gets one note and one row
  saying the mode was kept. A recorded chat that came back without its mode gets the
  notice that it was turned off.

Which chats are kept: an owner's chat whose mode is its own. Not kept, and reported
as today: an app-owned chat (``_app``, never recorded, because the app sets its mode
again), a worker whose posture its creator handed it (``_posture_inherited``, which
follows its creator again), a crewmate chat its Profile seeded (``_member_approval
_seeded``, seeded again at its next turn), and a Crew Mode card's crew-member trust
(``_trust_card_scoped``). A restore also needs the chat to address the same session
it did, and the ceiling in force to permit the mode (``session_mode_permitted``).

At report time a chat is skipped when a live auto-approve grant reaches it (a declared
one boot armed again before the restore), and a crewmate's conductor chat or worker
when its Profile (``agents.<name>.approval_mode``) seeds the recorded mode back at its
next turn (``crewmate_approval.seed_for_turn``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
import threading
import weakref
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kiro_crew import kept_grants
from kiro_crew.config.loader import config_dir

logger = logging.getLogger(__name__)

#: The unsigned record an earlier release wrote at the data-home root. Read once, for
#: the turned-off notice only, and deleted.
LEGACY_RECORD_FILE = "chat_trust_held.json"

_RECORD_KIND = "chats"

#: A legacy record is far below this. The bound keeps a file this module did not write
#: from turning the boot read into an unbounded one.
_MAX_RECORD_BYTES = 256 * 1024

#: The per-state writer :func:`arm` attaches; its absence is what keeps
#: :func:`record` from writing before the previous record was taken.
_WRITER_ATTR = "_chat_trust_record"
#: The sessions an explicit pick touched since boot (:func:`note_mode_pick`).
_PICKS_ATTR = "_chat_trust_picks"
_ALL_SESSIONS = "*"
#: Set once this boot's restore has run (:func:`restore_or_report`).
_RESTORED_ATTR = "_chat_trust_restored"

#: The chat row's ``meta["kind"]``, a wire value the dashboard keys its localized
#: card on (``TrustResetNoticeCard.tsx``). The card's one button sends the row's
#: ``meta["approval_mode"]`` through the picker's own route, so the person, not
#: this module, turns the mode back on. The row text stays the fallback for
#: readers that cannot localize.
ROW_KIND = "trust_reset_notice"

#: The modes a chat can hold, with the name the chat's approval picker shows.
_MODE_LABELS = {"trust": "Trust", "trust_reads": "Read"}

#: How long a burst of mode changes settles before one record write.
_PROMPT_WRITE_SETTLE_S = 0.05


@dataclass
class _PromptTarget:
    """The booted state whose changes :func:`note_trust_changed` records, and its loop."""

    state: "weakref.ReferenceType[Any]"
    loop: asyncio.AbstractEventLoop
    scheduled: bool = False


_prompt_target: _PromptTarget | None = None


@dataclass(frozen=True, order=True)
class HeldMode:
    """One open chat that held an approval mode.

    *session* is the session the chat addressed and *keep* whether the mode is the
    owner's own and comes back after an exit the user did not choose.
    """

    key: str
    mode: str
    session: str = ""
    keep: bool = False


@dataclass
class _Writer:
    # Serializes the writers, which run off the loop; held across the file write.
    lock: threading.Lock = field(default_factory=threading.Lock)
    # Orders a write's commit against a mode change. Held for a look at memory and
    # one rename or unlink, never across a write, so the loop may take it.
    commit: threading.Lock = field(default_factory=threading.Lock)
    # Bumped by every mode change; a write commits only while it is unchanged.
    gen: int = 0
    # What the record on disk holds: at arm, the verified record this boot took, so
    # the first sample that differs rewrites or removes it, even when nothing changes.
    written: frozenset[HeldMode] = frozenset()
    # The verified entries the boot restore has not resolved yet. Until it has, they
    # count as held, so a flush or a mode change in the boot window cannot drop a
    # grant a crash would then lose. An explicit pick or the restore's end clears it.
    pending: frozenset[HeldMode] = frozenset()


def _held_mode(slot: Any) -> str:
    if getattr(slot, "_trust", False) is True:
        return "trust"
    if getattr(slot, "_trust_reads", False) is True:
        return "trust_reads"
    return ""


def _keepable(slot: Any) -> bool:
    """Whether *slot*'s mode is the owner's own pick on it (see the module docstring)."""
    return not (
        getattr(slot, "_posture_inherited", False)
        or getattr(slot, "_member_approval_seeded", False)
        or getattr(slot, "_trust_card_scoped", False)
    )


def _session_of(slot: Any) -> str:
    from kiro_crew.dashboard.chat_utils_slots import effective_session_key

    try:
        return effective_session_key(slot)
    except Exception:
        return ""


def _held_now(state: Any) -> frozenset[HeldMode]:
    """The open chats that hold a mode now, among those a restart restores."""
    building = getattr(state, "_slots_under_construction", ())
    held: set[HeldMode] = set()
    for key, slot in list(getattr(state, "_slots", {}).items()):
        # The same set ``open_slots.json`` keeps: an incognito or temporary chat
        # does not come back, so there is no chat to keep or tell.
        if getattr(slot, "memory_mode", "persistent") != "persistent" or key in building:
            continue
        # An app sets its own chat's mode again after a restart.
        if getattr(slot, "_app", ""):
            continue
        mode = _held_mode(slot)
        if mode:
            held.add(HeldMode(key, mode, _session_of(slot), _keepable(slot)))
    return frozenset(held)


def _held_or_pending(state: Any, writer: _Writer) -> frozenset[HeldMode]:
    """What the record must hold: the live modes, plus each unresolved kept entry.

    Call with ``writer.commit`` held. A chat that holds a mode now is recorded as it
    is; a pending entry fills in only for a chat that holds none yet.
    """
    held = _held_now(state)
    if not writer.pending:
        return held
    keys = {h.key for h in held}
    return held | frozenset(p for p in writer.pending if p.key not in keys)


def note_trust_changed() -> None:
    """Called by a slot's mode setter on every change. Safe on any thread.

    Does no file write: a change that takes a mode away unlinks the record at once
    (:func:`_drop_record_if_narrowed`), and the rewrite runs in the loop's executor.

    Schedules one record write on the booted gateway's loop (:func:`start_prompt_writer`),
    run in its executor after the burst settles, so a mode turned off moments before a
    crash is not restored by the next boot.
    """
    target = _prompt_target
    if target is None:
        return
    _drop_record_if_narrowed(target)
    try:
        target.loop.call_soon_threadsafe(_schedule_prompt_write, target)
    except RuntimeError:
        pass  # the loop is closed: the gateway is going, and its shutdown save records


def _drop_record_if_narrowed(target: _PromptTarget) -> None:
    """Delete the chats record now when a chat it holds has lost that mode.

    A mode turned off must not come back after a crash in the moment before the
    scheduled rewrite lands, so the record that would restore it goes on the
    changing thread, before the change is acknowledged; the rewrite then records
    the chats still holding a mode. Every change also bumps the writer's generation,
    so a write sampled before it is never put in place after it. Takes only the
    writer's commit lock, which no file write holds. Never raises.
    """
    try:
        state = target.state()
        writer = getattr(state, _WRITER_ATTR, None) if state is not None else None
        if not isinstance(writer, _Writer):
            return
        with writer.commit:
            writer.gen += 1
            now = {(h.key, h.mode) for h in _held_or_pending(state, writer)}
            if any((h.key, h.mode) not in now for h in writer.written):
                kept_grants.remove_record(kept_grants.CHATS_RECORD)
                writer.written = frozenset()
    except Exception:
        logger.debug("Could not drop the chats record after a mode change", exc_info=True)


def _schedule_prompt_write(target: _PromptTarget) -> None:
    if target.scheduled or target is not _prompt_target:
        return
    target.scheduled = True
    target.loop.call_later(_PROMPT_WRITE_SETTLE_S, _launch_prompt_write, target)


def _launch_prompt_write(target: _PromptTarget) -> None:
    target.scheduled = False
    state = target.state()
    if state is None or target is not _prompt_target:
        return
    target.loop.run_in_executor(None, record, state)


def record(state: Any) -> None:
    """Write which open chats hold an approval mode, when that set changed.

    Writes nothing before :func:`arm` ran on *state*, so this process cannot
    overwrite the previous process's record before it is read. Never raises.
    """
    writer = getattr(state, _WRITER_ATTR, None)
    if not isinstance(writer, _Writer):
        return
    try:
        # The flush thread, the shutdown thread and the prompt writer take turns, and
        # each commits only while no mode changed since its sample, so an older
        # sample never lands after a newer change.
        with writer.lock:
            with writer.commit:
                gen = writer.gen
                held = _held_or_pending(state, writer)
                if held == writer.written:
                    return

            # What the writer knows is on disk changes under the same lock as the
            # file, so a mode change that follows sees the record it must remove.
            def _committed() -> None:
                writer.written = held

            if held:
                chats = [
                    {"key": h.key, "mode": h.mode, "session": h.session, "keep": h.keep}
                    for h in sorted(held)
                ]
                kept_grants.write_record(
                    kept_grants.CHATS_RECORD,
                    _RECORD_KIND,
                    {"chats": chats},
                    commit=(writer.commit, lambda: writer.gen == gen),
                    on_commit=_committed,
                )
            else:
                with writer.commit:
                    if writer.gen == gen:
                        kept_grants.remove_record(kept_grants.CHATS_RECORD)
                        _committed()
    except Exception:
        logger.debug("Could not record the chats holding an approval mode", exc_info=True)


def start_prompt_writer(state: Any) -> None:
    """Record *state*'s mode changes as they happen, on the running loop.

    Called once by the booting gateway, on its loop, after :func:`arm`. Holds *state*
    weakly. The periodic flush and the shutdown save keep recording as well.
    """
    global _prompt_target
    _prompt_target = _PromptTarget(state=weakref.ref(state), loop=asyncio.get_running_loop())


def _reset_for_tests() -> None:
    """Stop recording changes as they happen."""
    global _prompt_target
    _prompt_target = None


def _read_legacy(path: Path) -> str | None:
    """The legacy record's text, or ``None`` when there is no regular file to read.

    Opened by descriptor, non-blocking and without following a link, and read only
    when ``fstat`` says it is a regular file under the bound.
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


def _parse_chats(chats: Any, *, signed: bool) -> tuple[HeldMode, ...]:
    """The entries of a record's ``chats`` list. Only a *signed* record can keep a mode."""
    if not isinstance(chats, list):
        return ()
    held: dict[HeldMode, None] = {}
    for chat in chats:
        if not isinstance(chat, dict):
            continue
        key, mode = chat.get("key"), chat.get("mode")
        if not (isinstance(key, str) and key and mode in _MODE_LABELS):
            continue
        raw_session = chat.get("session") if signed else ""
        session = raw_session if isinstance(raw_session, str) else ""
        keep = signed and chat.get("keep") is True and bool(session)
        held[HeldMode(key, mode, session if keep else "", keep)] = None
    return tuple(held)


def _take_legacy() -> tuple[HeldMode, ...]:
    path = config_dir() / LEGACY_RECORD_FILE
    try:
        text = _read_legacy(path)
        if text is None:
            return ()
        try:
            data = json.loads(text)
        except ValueError:
            return ()
        if not isinstance(data, dict) or data.get("version") != 1:
            return ()
        return _parse_chats(data.get("chats"), signed=False)
    finally:
        path.unlink(missing_ok=True)


def _audit(operation: str, outcome: str, resources: str) -> None:
    try:
        from kiro_crew.sel import sel

        sel().log_api_access(
            caller="dashboard:kept_after_restart",
            operation=operation,
            outcome=outcome,
            resources=resources,
        )
    except Exception:
        logger.warning("SEL audit failed for %s", operation, exc_info=True)


def arm(state: Any) -> tuple[HeldMode, ...]:
    """Take the previous process's record, then let this process write its own.

    Run once per boot, off the event loop, before the open-tab restore. After a stop
    the user chose, the record was already cleared (``kept_grants.boot_verdict``):
    each chat it held is audited as turned off and nothing is returned, since the
    user ended the modes on purpose. Never raises.
    """
    held: tuple[HeldMode, ...] = ()
    signed: tuple[HeldMode, ...] = ()
    try:
        legacy = _take_legacy()
        if kept_grants.boot_verdict() == kept_grants.VERDICT_KEPT:
            body = kept_grants.read_record(kept_grants.CHATS_RECORD, _RECORD_KIND)
            signed = _parse_chats(body.get("chats"), signed=True) if body else ()
            # The signed record is this release's; a legacy one only adds notices.
            known = {entry.key for entry in signed}
            held = signed + tuple(entry for entry in legacy if entry.key not in known)
        else:
            cleared = kept_grants.cleared_record(kept_grants.CHATS_RECORD)
            for entry in _parse_chats((cleared or {}).get("chats"), signed=True):
                _audit("mode_change:normal", "disabled", f"{entry.key}, cleared_by_user_stop")
    except Exception:
        logger.debug("Could not take the previous chat approval-mode record", exc_info=True)
    setattr(state, _WRITER_ATTR, _Writer(written=frozenset(signed), pending=frozenset(signed)))
    return held


def note_mode_pick(state: Any, slot: Any) -> None:
    """Record an explicit approval-mode pick on *slot* (every chat when ``None``).

    Called by ``apply_approval_mode``, the one path every mode pick takes, before it
    writes anything. A kept mode the boot restore has not applied yet to that session
    is then not applied: the owner's newer choice wins, even a Normal picked on a chat
    that is still Normal. Never raises.
    """
    try:
        picked = getattr(state, _PICKS_ATTR, None)
        if not isinstance(picked, set):
            picked = set()
            setattr(state, _PICKS_ATTR, picked)
        picked.add(_session_of(slot) if slot is not None else _ALL_SESSIONS)
        if not getattr(state, _RESTORED_ATTR, False):
            # Durable before it is acknowledged: the record the boot has not applied
            # yet goes, so a crash cannot bring back what this pick overrode. Fewer
            # kept chats after such a crash, never more.
            writer = getattr(state, _WRITER_ATTR, None)
            if isinstance(writer, _Writer):
                with writer.commit:
                    writer.gen += 1
                    writer.written = frozenset()
                    writer.pending = frozenset()
                    kept_grants.remove_record(kept_grants.CHATS_RECORD)
            else:
                kept_grants.remove_record(kept_grants.CHATS_RECORD)
    except Exception:
        logger.debug("Could not record an approval-mode pick", exc_info=True)


def note_title(mode: str) -> str:
    """The bell note's title for a chat whose *mode* a restart ended."""
    return f"{_MODE_LABELS[mode]} mode was turned off by a restart"


def note_body(mode: str, chat_title: str) -> str:
    """The bell note's body: what ended, what it means for running work, the remedy."""
    label = _MODE_LABELS[mode]
    return (
        f'Kiro Crew restarted and "{chat_title}" came back out of {label} mode, so its '
        f"tools ask for approval again. A loop or task left running in it waits for that "
        f"approval. Turn {label} mode back on in the chat if you still want it."
    )


def row_text(mode: str) -> str:
    """The system row shown in the chat itself."""
    label = _MODE_LABELS[mode]
    return (
        f"{label} mode was turned off because Kiro Crew restarted, so tools ask for "
        f"approval again. Turn {label} mode back on if you still want it."
    )


def kept_note_title(mode: str) -> str:
    """The bell note's title for a chat whose *mode* a restart kept."""
    return f"{_MODE_LABELS[mode]} mode was kept after a restart"


def kept_note_body(mode: str, chat_title: str) -> str:
    """The bell note's body for a kept mode: what stayed on and what ends it."""
    label = _MODE_LABELS[mode]
    return (
        f'Kiro Crew restarted without being stopped, so "{chat_title}" is still in '
        f"{label} mode. It stays on until you pick another mode or stop Kiro Crew."
    )


def kept_row_text(mode: str) -> str:
    """The system row shown in a chat whose mode a restart kept."""
    label = _MODE_LABELS[mode]
    return (
        f"{label} mode is still on: Kiro Crew restarted without being stopped. "
        "Stopping Kiro Crew turns it off."
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


def _undo_restore(
    state: Any, slot: Any, sharing: list[Any], entry: HeldMode, *, policy_written: bool
) -> None:
    """Take back a refused restore: its flags, and the session policy it wrote.

    A ceiling that changed under the restore refused the mode. The policy is cleared
    unless a live YOLO grant reaches the chat, which keeps it as the deny hook does.
    """
    for other in sharing:
        other._trust = False
        other._trust_reads = False
    if policy_written:
        try:
            from kiro_crew.safety_override import safety_override

            keep = safety_override().has_grant() and state.yolo_reaches(slot) is True
        except Exception:
            keep = False
        if not keep:
            state.sessions.set_approval_policy(entry.session, "")
    _audit(f"mode_change:{entry.mode}", "denied", f"{entry.key}, reason:refused")


def _restore_kept(state: Any, slot: Any, entry: HeldMode) -> bool:
    """Give restored *slot* back its kept mode, as the picker writes it. Returns success.

    Refused, with the denial audited, when the slot addresses another session than
    the one recorded, belongs to an app, or the ceiling in force does not permit
    the mode. The writes are the picker's for one chat (``apply_approval_mode``):
    every slot sharing the session takes the flags, the session's stored policy
    follows, the chat becomes the person's own pick, and its inheriting workers take
    its posture through the same walk a pick runs.
    """
    from kiro_crew.approval_mode_policy import session_mode_permitted
    from kiro_crew.dashboard.chat_handlers import _apply_inherited_postures
    from kiro_crew.dashboard.chat_utils_slots import effective_session_key

    if getattr(slot, "_app", "") or effective_session_key(slot) != entry.session:
        _audit(f"mode_change:{entry.mode}", "denied", f"{entry.key}, reason:session_changed")
        return False
    if not session_mode_permitted(entry.mode):
        _audit(f"mode_change:{entry.mode}", "denied", f"{entry.key}, reason:policy")
        return False
    picked: set[str] = getattr(state, _PICKS_ATTR, set())
    if _ALL_SESSIONS in picked or entry.session in picked:
        _audit(f"mode_change:{entry.mode}", "denied", f"{entry.key}, reason:picked_since_boot")
        return False
    sharing = [s for s in list(state._slots.values()) if effective_session_key(s) == entry.session]
    for other in sharing:
        if entry.mode == "trust":
            other._trust = True
            other._trust_card_scoped = False
        else:
            other._trust = False
            other._trust_reads = True
            other._trust_scope = ""
    if _held_mode(slot) != entry.mode:
        _undo_restore(state, slot, sharing, entry, policy_written=False)
        return False
    state.sessions.set_approval_policy(entry.session, "auto" if entry.mode == "trust" else "")
    # Checked again after the write, as a pick is: a ceiling that denied the mode in
    # between already cleared the policy, and this write must not stand over it.
    if _held_mode(slot) != entry.mode:
        _undo_restore(state, slot, sharing, entry, policy_written=True)
        return False
    _audit(f"mode_change:{entry.mode}", "enabled", f"{entry.key}, kept_after_restart")
    try:
        _apply_inherited_postures(
            state, slot, may_raise=True, audit_caller=lambda _c: "dashboard:kept_after_restart"
        )
    except Exception:
        logger.warning("Could not hand a kept mode to the workers of %s", entry.key, exc_info=True)
    return True


async def _post(state: Any, slot: Any, entry: HeldMode, *, kept: bool) -> None:
    """One bell note and one chat row about *entry*'s mode, kept or turned off."""
    from kiro_crew.dashboard.state import load_window_off_loop

    if kept:
        title, body = kept_note_title(entry.mode), kept_note_body(entry.mode, slot.display_title)
        text, meta = kept_row_text(entry.mode), {"approval_mode": entry.mode}
    else:
        title, body = note_title(entry.mode), note_body(entry.mode, slot.display_title)
        text, meta = row_text(entry.mode), {"kind": ROW_KIND, "approval_mode": entry.mode}
    state.notify("agent", title, body, meta={"slot": slot.key, "approval_mode": entry.mode})
    # A restored chat's window may still be on disk; appending reads it, so the read
    # happens off the loop first.
    await load_window_off_loop(slot)
    if state.get_slot(entry.key) is slot:
        slot.append("system", text, "msg msg-info", meta=meta)


async def restore_or_report(state: Any, held: tuple[HeldMode, ...]) -> None:
    """Give each kept chat its mode back, and tell the owner about the ones that lost it.

    A kept entry whose chat is open and may still hold the mode gets it back and one
    note and row saying so. Every other recorded chat that came back without its mode
    gets the turned-off note and row, unless it is owed nothing: it holds the mode
    again, a live auto-approve grant reaches it, or it is a crewmate's chat its
    Profile seeds the mode back into at its next turn. Never raises: boot must not
    fail over this.
    """
    try:
        await _restore_or_report(state, held)
    finally:
        # Resolved: from here the live modes alone are what the record holds.
        writer = getattr(state, _WRITER_ATTR, None)
        if isinstance(writer, _Writer):
            with writer.commit:
                writer.pending = frozenset()
        setattr(state, _RESTORED_ATTR, True)


async def _restore_or_report(state: Any, held: tuple[HeldMode, ...]) -> None:
    if not held:
        return
    lost: list[tuple[HeldMode, Any]] = []
    for entry in held:
        slot = state.get_slot(entry.key)
        if slot is None:
            continue
        try:
            if entry.keep and _restore_kept(state, slot, entry):
                await _post(state, slot, entry, kept=True)
                continue
        except Exception:
            logger.warning("Could not keep the approval mode of chat %s", entry.key, exc_info=True)
        if _owed(state, slot, entry.mode):
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
            await _post(state, slot, entry, kept=False)
        except Exception:
            logger.warning(
                "Could not report the restart-ended approval mode of chat %s",
                entry.key,
                exc_info=True,
            )
