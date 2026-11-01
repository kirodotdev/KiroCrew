"""Per-session state shared by every live slot on one transcript.

Two live slots are ALIASES when they resolve to one ``slot_history_key``. They then
also share one session (``session_key_for`` returns the shared ``linked_session_key``),
so one kiro-cli process and one working directory. A field that describes that session
cannot be held once per slot: a blank alias's save would write its own empty copy over
the peer's value on the shared transcript.

Such fields live in a :class:`TranscriptBinding`, and aliases hold the SAME binding
object. ``_ChatSlot.project`` reads and writes it through a property, so every
``slot.project = x`` already writes the shared cell and every ``slot.project is token``
rollback check reads it. A rollback therefore skips a peer's newer write (the identity
differs) and restores the whole group otherwise, with no per-peer rollback contract.

:func:`bind_transcript` is the one place a slot's binding is chosen. Call it after
anything that can change the slot's history key.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import _ChatSlot


class TranscriptBinding:
    """The cells a slot shares with every live alias on its transcript."""

    __slots__ = ("project", "assigned", "_commit")

    def __init__(self, project: str = "", assigned: bool = False) -> None:
        self.project = project
        # Whether the group has chosen its directory: it held one, or cleared one it
        # held. A slot born linked and never seeded holds "" without choosing it.
        self.assigned = assigned
        # (token, assigned before it) for the newest provisional commit, so its
        # rollback can put ``assigned`` back. Identity-keyed, like the rollback.
        self._commit: tuple[object, bool] | None = None


def write_project(binding: TranscriptBinding, value: str) -> None:
    """Store *value* and keep ``assigned`` meaning "the group chose this".

    A non-empty directory is a choice, and so is "" over a held directory (a clear).
    "" over "" is not: a computed default that found no directory chose nothing. A
    ``_CommitToken`` write is provisional; :func:`rollback_project` undoes it.
    """
    from kiro_crew.dashboard.chat_handlers import _CommitToken

    if isinstance(value, _CommitToken):
        binding._commit = (value, binding.assigned)
    if value or binding.project:
        binding.assigned = True
    binding.project = value


def rollback_project(slot: Any, token: str, prior: str) -> None:
    """Undo a ``_CommitToken`` commit of ``slot.project`` if it is still the value.

    Restores both the directory and whether it was chosen. A peer that wrote during
    the awaits replaced the token, so this does nothing, and that write stands.
    """
    binding = getattr(slot, "_binding", None)
    if not isinstance(binding, TranscriptBinding):
        # A slot-shaped stand-in with a plain ``project`` attribute.
        if slot.project is token:
            slot.project = prior
        return
    if binding.project is not token:
        return
    commit = binding._commit
    binding.project = prior
    if commit is not None and commit[0] is token:
        binding.assigned = commit[1]
    binding._commit = None


def bind_transcript(state: Any, slot: _ChatSlot) -> None:
    """Point *slot* at the binding its transcript's live aliases hold.

    * Another live slot on the same history key: adopt that slot's binding. The live
      peer wins, because it holds the value the shared session runs with.
    * No live peer, but the slot's binding is still held by a slot on another key (it
      just left an alias group): give it a private copy. It keeps its value and stops
      affecting the group it left.
    * Otherwise keep the binding.

    Membership is derived from ``state._slots`` on every call rather than kept in a
    registry, so a closed slot can never leave a stale group behind.
    """
    from kiro_crew.dashboard.chat_utils_slots import slot_history_key

    own = getattr(slot, "_binding", None)
    slots = getattr(state, "_slots", None)
    if not isinstance(own, TranscriptBinding) or not isinstance(slots, dict):
        return  # a slot-shaped stand-in, or a state with no slot table
    key = slot_history_key(slot)
    # Only real slots carry a binding; a slot-shaped double in ``_slots`` has none.
    live = [
        other
        for other in list(slots.values())
        if other is not slot and isinstance(getattr(other, "_binding", None), TranscriptBinding)
    ]
    for other in live:
        if slot_history_key(other) == key:
            shared = other._binding
            seeded = own.assigned and not shared.assigned and own.project != shared.project
            if own.assigned and not shared.assigned:
                # The peer never chose a directory (born linked, never seeded); the
                # joiner's own value, often just read from disk, is the group's.
                shared.project = own.project
                shared.assigned = True
            slot._binding = shared
            if seeded:
                _reset_if_running(state, slot)
            # A reset armed for the shared session (a project change still waiting
            # on teardown) belongs to the joiner too: it adopts the new directory,
            # so without the flag its turns would reuse the process in the old one.
            pending = getattr(other, "_pending_reset_history_key", None)
            if pending and not slot._pending_reset_history_key:
                from kiro_crew.dashboard.chat_utils import effective_session_key

                if pending == effective_session_key(slot):
                    slot._pending_reset_history_key = pending
            return
    if any(other._binding is slot._binding for other in live):
        slot._binding = TranscriptBinding(
            project=slot._binding.project, assigned=slot._binding.assigned
        )


def _peers(state: Any, slot: Any) -> list[Any]:
    """Every other live slot that holds *slot*'s binding."""
    binding = getattr(slot, "_binding", None)
    slots = getattr(state, "_slots", None)
    if binding is None or not isinstance(slots, dict):
        return []
    return [
        other
        for other in list(slots.values())
        if other is not slot and getattr(other, "_binding", None) is binding
    ]


def live_project_outranks_disk(state: Any, slot: Any) -> bool:
    """Whether a live alias holds a directory that a disk value must not replace.

    A restore or a seed from disk must not overwrite the directory a live peer runs
    the shared session in, which may be newer than disk, an explicit clear included.
    A binding nothing has written is not evidence: it belongs to a slot born linked
    and never seeded (a workflow or task-runner result tab), and skipping disk for it
    would lose the stored directory on the next save.
    """
    binding = getattr(slot, "_binding", None)
    if not isinstance(binding, TranscriptBinding):
        return False
    return binding.assigned and bool(_peers(state, slot))


def _reset_if_running(state: Any, slot: Any) -> None:
    """Arm the deferred reset when the group's directory moved under a live session.

    The session's process keeps the directory it was spawned in until teardown; the
    armed flag makes each alias's next turn describe that directory meanwhile and
    lands the reset at its next consume.
    """
    from kiro_crew.dashboard.chat_utils import effective_session_key

    sessions = getattr(state, "sessions", None)
    key = effective_session_key(slot)
    try:
        running = sessions is not None and sessions.get_provider(key) is not None
    except Exception:  # noqa: BLE001 - a probe failure must not break the join
        running = False
    if running:
        arm_project_reset(state, slot, key)


def arm_project_reset(state: Any, slot: Any, key: str) -> None:
    """Arm the deferred session reset on *slot* and on every live alias.

    A project change on one alias is the group's change, and the aliases run one
    session. A peer whose own flag stayed clear would reuse the process in the old
    directory while its turn describes the new one. The reset is spent only on a real
    teardown, so a second consume after the first one costs at most a cold start.
    """
    slot._pending_reset_history_key = key
    for other in _peers(state, slot):
        other._pending_reset_history_key = key


def seed_project_from_disk(state: Any, slot: Any, value: str) -> None:
    """Write a stored directory onto *slot* unless a live alias's choice outranks it.

    The one entry for disk seeds (restore read and channel surfacing). When the seed
    moves a binding live aliases share, a running session keeps its old directory
    until teardown, so the deferred reset is armed for the group.
    """
    if not value or live_project_outranks_disk(state, slot):
        return
    shared = bool(_peers(state, slot))
    changed = value != slot.project
    slot.project = value
    if shared and changed:
        _reset_if_running(state, slot)
