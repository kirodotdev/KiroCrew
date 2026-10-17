"""Per-session delivery records: what each session was already sent.

Two bounded, oldest-evicted records the builder keeps per session key, both keyed by
``ContextBuilder._cap_memo_key``:

* the shown-lesson record (``_ShownLessons``, defined in ``store_admission``): started
  from the session-start lessons block, reset when a compaction re-injection runs, and
  extended by the per-message lessons block, which ``_turn_lessons_block`` hands to
  ``store_admission.turn_lessons_block``;
* the trigger-matched skill bodies a session was sent, with the per-turn undo record
  that ``rollback_skill_bodies`` and ``commit_skill_bodies`` settle once the turn's fate
  is known.

Builder state read and written: ``_lessons_shown`` and ``_lessons_shown_lock``;
``_sent_skill_bodies``, ``_sent_skill_agents``, ``_sent_skill_bodies_undo`` and
``_sent_skill_bodies_lock``; the bounds ``_SENT_SKILL_BODY_SESSIONS`` and
``_SENT_SKILL_BODY_ENTRIES`` are read through the builder, so an instance override
applies.

New per-session delivery records belong here.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from kiro_crew.context_assembly import store_admission as _store_admission

if TYPE_CHECKING:
    from kiro_crew.context import _ShownLessons

logger = logging.getLogger("kiro_crew.context")


def _remember_startup_lessons(self, session_key: str, block: str) -> None:
    """Start *session_key*'s shown-lesson record from its session-start block."""
    from kiro_crew import context as ctx  # circular import: the facade imports this owner

    key = self._cap_memo_key(session_key)
    with self._lessons_shown_lock:
        self._lessons_shown[key] = ctx._ShownLessons(startup_block=block)
        self._lessons_shown.move_to_end(key)
        while len(self._lessons_shown) > ctx._LESSONS_SHOWN_SESSIONS:
            self._lessons_shown.popitem(last=False)


def _forget_shown_lessons(self, session_key: str) -> None:
    """Compaction dropped every block this session was shown; lessons may come back."""
    from kiro_crew import context as ctx  # circular import: the facade imports this owner

    key = self._cap_memo_key(session_key)
    with self._lessons_shown_lock:
        if key in self._lessons_shown:
            self._lessons_shown[key] = ctx._ShownLessons()


def _live_shown_lessons(self, session_key: str) -> _ShownLessons:
    """*session_key*'s record in place now, made the newest; the caller holds the lock.

    A session with none -- the setting turned on after it started, or its
    record dropped -- starts an empty one.
    """
    from kiro_crew import context as ctx  # circular import: the facade imports this owner

    key = self._cap_memo_key(session_key)
    record = self._lessons_shown.get(key)
    if record is None:
        record = ctx._ShownLessons()
        self._lessons_shown[key] = record
    self._lessons_shown.move_to_end(key)
    while len(self._lessons_shown) > ctx._LESSONS_SHOWN_SESSIONS:
        self._lessons_shown.popitem(last=False)
    return record


def _turn_lessons_block(
    self,
    text: str,
    session_key: str,
    *,
    workspace: str | None,
    memory_store: str | None,
    project: str | None,
    member: str,
    execution_context: Any,
    context_groups: frozenset[str] | None,
) -> str:
    """The ``memory.inject_lessons_per_turn`` block for one follow-up message, or ``""``.

    Contract and rationale: ``context_assembly.store_admission.turn_lessons_block``.
    """
    return _store_admission.turn_lessons_block(
        self,
        text,
        session_key,
        workspace=workspace,
        memory_store=memory_store,
        project=project,
        member=member,
        execution_context=execution_context,
        context_groups=context_groups,
    )


def _dedup_triggered_bodies(
    self,
    session_key: str | None,
    agent: str | None,
    reset: bool,
    candidates: list[tuple[str, str]],
) -> set[str]:
    """Return the subset of *candidates* to DEMOTE from body to pointer.

    *candidates* is ``[(skill_key, body_sha256), ...]`` for the skills whose
    body this turn would inject. A skill is demoted when the SAME body hash
    was already recorded in this session — the provider replays native
    history, so a second identical copy adds nothing. A skill whose hash
    differs (an edited skill) is not demoted, so the new body arrives. The
    record is written at build time and read on the next match, so the dedup
    holds at EVERY ``build_message`` caller with no per-caller wiring. The
    check-and-record is one guarded transaction.

    *reset* True (a fresh session, or the first turn after a compaction, or
    an agent switch on this key) drops the prior record first: those are the
    turns whose provider window cannot hold the earlier bodies, so
    everything sends again. A missing ``session_key`` (CLI, tests) keeps no
    record and demotes nothing.

    Fails SAFE in both directions. A rare turn that builds but never lands
    records its body, so the next turn demotes it to the POINTER line — the
    agent still learns the skill applies, which is today's behaviour for an
    ``inject_on_trigger: false`` skill, not silence. And a missed reset
    likewise demotes to a pointer, never to nothing.

    Each build stashes the prior value of every entry it writes into
    ``_sent_skill_bodies_undo[key]`` so a caller whose turn never lands can
    call :meth:`rollback_skill_bodies` and undo this build's writes — the
    record then does not outlive the provider copy it names. Only fresh
    records and hash changes are captured; a demote touches an entry to the
    LRU end without changing its value, so it needs no undo.
    """
    if not session_key:
        return set()
    key = self._cap_memo_key(session_key)
    # Store a fixed-size digest of the agent, not the raw agent name: the
    # caller supplies `agent` at whatever length it likes and this entry
    # leaves `_sent_skill_agents` only by eviction, so an oversized name
    # would stay retained -- the same "cap the count AND the stored field"
    # reason `_cap_memo_key` digests the session key. `None` (no agent) must
    # stay distinct from any digest, so it is carried through unchanged.
    agent_key = None if agent is None else self._cap_memo_key(agent)
    demote: set[str] = set()
    undo: dict[str, str | None] = {}
    with self._sent_skill_bodies_lock:
        if reset or self._sent_skill_agents.get(key) != agent_key:
            self._sent_skill_bodies.pop(key, None)
        # A reset (or agent switch) drops the record, so this build starts
        # the session's history fresh; its own writes are the only thing a
        # rollback of THIS turn should undo, so start the undo log empty.
        self._sent_skill_bodies_undo.pop(key, None)
        # Move this session to the END of the LRU maps (pop + reinsert the
        # SAME record object) so eviction drops the least-recently-touched
        # session -- the insertion-ordered shape `_cap_figures` uses.
        sent = self._sent_skill_bodies.pop(key, {})
        self._sent_skill_bodies[key] = sent
        self._sent_skill_agents.pop(key, None)
        self._sent_skill_agents[key] = agent_key
        for skill_key, digest in candidates:
            # Store under a fixed-size digest of the skill key, not the key
            # itself: a skill key reaches here from the caller at whatever
            # length it likes and an inner entry leaves the record only by
            # eviction, so an oversized key would stay retained. Digesting it
            # makes `_SENT_SKILL_BODY_ENTRIES` govern bytes as well as count,
            # the same reason `_cap_memo_key` digests the session key.
            skey = self._cap_memo_key(skill_key)
            if sent.get(skey) == digest:
                demote.add(skill_key)
                # Touch so the per-session cap keeps the still-matching skill.
                sent[skey] = sent.pop(skey)
            else:
                # Capture the prior value once (None if the entry is new) so
                # a rollback restores exactly what stood before this build.
                if skey not in undo:
                    undo[skey] = sent.get(skey)
                sent.pop(skey, None)
                sent[skey] = digest
        # Bound the inner dict AND the session map with the SAME cap that
        # admits entries, so what is retained equals what was admitted. A
        # session that matches more than `_SENT_SKILL_BODY_ENTRIES` distinct
        # skills over its life, or more than `_SENT_SKILL_BODY_SESSIONS`
        # sessions live at once, drops its oldest record — and a dropped
        # entry re-injects that body on its next match. That is fail-safe (a
        # re-sent body, never a dropped skill) but it silently narrows dedup
        # coverage, so report each forced eviction rather than evicting
        # quietly: an operator watching a session matching an unusually wide
        # skill set can see the record is at its bound.
        while len(sent) > self._SENT_SKILL_BODY_ENTRIES:
            dropped = next(iter(sent))
            sent.pop(dropped, None)
            logger.debug(
                "skill-body dedup: per-session entry cap %d reached for "
                "session=%s; oldest skill record evicted, its body "
                "re-injects on next match",
                self._SENT_SKILL_BODY_ENTRIES,
                key,
            )
        while len(self._sent_skill_bodies) > self._SENT_SKILL_BODY_SESSIONS:
            oldest = next(iter(self._sent_skill_bodies))
            self._sent_skill_bodies.pop(oldest, None)
            self._sent_skill_agents.pop(oldest, None)
            self._sent_skill_bodies_undo.pop(oldest, None)
            logger.debug(
                "skill-body dedup: session cap %d reached; oldest session "
                "record evicted, its bodies re-inject on next match",
                self._SENT_SKILL_BODY_SESSIONS,
            )
        # Keep only THIS build's undo entry for the session, and only for
        # keys that survived the cap above: an eviction drops a key from
        # `sent`, so retaining its undo row would hold a row the per-session
        # count already refused — the undo map must obey the same bound as
        # the record it reverses. A landed turn never rolls back and the
        # next build overwrites this entry; a dropped undo row only means an
        # evicted body re-injects on its next match, which is already the
        # eviction's fail-safe.
        undo = {skey: prior for skey, prior in undo.items() if skey in sent}
        if undo:
            self._sent_skill_bodies_undo[key] = undo
        else:
            self._sent_skill_bodies_undo.pop(key, None)
    return demote


def rollback_skill_bodies(self, session_key: str | None) -> None:
    """Undo the current turn's skill-body dedup writes for *session_key*.

    Call at a turn's ``finally`` when the turn did NOT land (a provider
    error, a cancel, a driver fault): the prompt that carried the freshly
    recorded bodies never reached the provider window, so the build-time
    record names bodies the model never received. Restoring the pre-build
    values makes the next turn re-inject them as full bodies rather than
    demote them to pointers.

    Acts on an undo entry only. A landed build clears its own undo entry
    through :meth:`commit_skill_bodies`, and turns on one session key are
    serialized, so an armed undo entry belongs to the current turn's own
    un-settled build — a turn that built no context (a slash command, an
    early return) finds nothing armed and is a no-op. Idempotent and
    best-effort: a missing key or record is a no-op, and it never raises so
    the turn's own outcome stands. A caller that skips it keeps the fail-safe
    (the next turn demotes to a pointer, not silence).
    """
    if not session_key:
        return
    key = self._cap_memo_key(session_key)
    with self._sent_skill_bodies_lock:
        undo = self._sent_skill_bodies_undo.pop(key, None)
        if not undo:
            return
        sent = self._sent_skill_bodies.get(key)
        if sent is None:
            # The record was evicted or reset since the build; nothing of
            # this build survives to undo.
            return
        for skey, prior in undo.items():
            if prior is None:
                sent.pop(skey, None)
            else:
                sent[skey] = prior


def commit_skill_bodies(self, session_key: str | None) -> None:
    """Discard the current turn's rollback state after its turn LANDED.

    Call at a turn's ``finally`` when the turn landed. The build-time record
    stays (the bodies reached the provider window), but the undo entry that
    would let a rollback erase them is dropped, so a later turn that never
    lands cannot roll back a build that already succeeded. Without this a
    landed build's undo stays armed until the next build, and the next
    non-landing turn's ``finally`` would roll back the earlier LANDED build,
    wrongly re-injecting bodies the window already holds. Idempotent and
    best-effort: a missing key or no armed entry is a no-op; never raises.
    """
    if not session_key:
        return
    key = self._cap_memo_key(session_key)
    with self._sent_skill_bodies_lock:
        self._sent_skill_bodies_undo.pop(key, None)
