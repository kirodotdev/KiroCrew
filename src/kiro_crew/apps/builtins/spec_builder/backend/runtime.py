"""Spec worker session binding: slot scoping, turn relay, stop, transcript.

The single place a spec's chat slot is materialized and scoped to this app and
its validated project, the relay that starts or queues a turn in it with its
structural provenance, the stops that end a turn or archive the slot, and the
redacted transcript served to the embedded chat.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from kiro_crew.dashboard.chat_persistence import rehydrate_slot_from_history_async

from .parsers import _SLOT_KEY_RE, _redact, _redact_and_truncate, _usable_name
from .repository import APP_NAME, _audit, _load_settings, _safe_dir, _slot_key

try:
    from kiro_crew.constants import CHAT_TURN_TIMEOUT
except Exception:  # pragma: no cover - constant always present in prod
    CHAT_TURN_TIMEOUT = 1800  # type: ignore[assignment]

# dashboard.server imports builtin route modules during startup. These imports
# stay deferred inside dispatch, transcript, and teardown helpers to avoid
# closing that import cycle.

logger = logging.getLogger("kirocrew.app.spec-builder")


async def _restore_worker_transcript(state: Any, name: str, *, adopt_closed: bool) -> None:
    """Bring this spec's persisted conversation back into a cold worker slot.

    Slots are in-memory: a gateway restart or idle cleanup drops the worker's chat
    while its transcript remains on disk. Read endpoints must rehydrate before
    materializing an empty slot because core's resume returns early once a slot
    exists.

    ``adopt_closed`` is the CALLER's decision, not a constant. For a spec already
    in the index it is True: the worker is not a tab the user closed, its lifecycle
    belongs to the spec, and idle-slot cleanup marks it closed on idleness alone.
    For a spec being CREATED it must be False -- a delete leaves the archived
    transcript on disk under a key derived from the name, so creating a new spec
    with an already-used name would hand the fresh agent the deleted spec's
    conversation.

    Best-effort by design. A missing, malformed or foreign transcript must leave
    the app working: the caller falls through to creating a fresh slot, and the
    ownership check it applies afterwards is what keeps a foreign transcript from
    being adopted.
    """
    try:
        restored = await rehydrate_slot_from_history_async(
            state, _slot_key(name), adopt_closed=adopt_closed
        )
    except Exception:
        logger.warning("spec %s: restoring the worker transcript failed", name, exc_info=True)
        return
    if restored is not None:
        _audit("spec_transcript_restored", name)


def _slot_identity_moved(name: str, slot_key: str) -> bool:
    """True when ``name`` does not resolve to the key this request captured.

    ``_slot_key`` reads the module-global ``_SLOT_KEYS``, which a delete +
    same-name recreate rewrites to a fresh per-creation key. Any resolution taken
    AFTER an await can therefore name a different spec than the one the request
    began with, so the captured key is the identity and this is the check that it
    still holds. A moved mapping means our spec was replaced while we waited: the
    request must touch nothing rather than adopt the replacement's slot and stamp
    its own project onto it.
    """
    if _slot_key(name) == slot_key:
        return False
    _audit("spec_slot_replaced_midflight", name, outcome="denied")
    logger.warning(
        "spec %s was replaced while its slot was being acquired — refusing the stale request",
        name,
    )
    return True


async def _ensure_worker_slot(
    state: Any, name: str, meta: dict, *, adopt_closed: bool = True
) -> Any:
    """Materialize this spec's worker slot, SCOPED, and return it.

    The single place a spec slot comes into existence. It exists because
    ``get_or_create_slot`` only stamps ``app`` on NEWLY created slots, and
    because a slot created by any OTHER path is unscoped: a spec discovered on
    disk (created by the Kiro CLI/IDE) has no slot until something makes one,
    and if the embedded chat's ``POST /api/chat`` got there first the slot came
    up with no ``_app`` (so it surfaced in the main sidebar) and no ``project``
    (so approved tools ran from the gateway's own working directory instead of
    the user's project). Creating it HERE, from the indexed metadata, means the
    first thing that touches a spec's slot always scopes it.

    Refuses a slot that ANOTHER app already owns. ``get_or_create_slot`` keys off
    the name, so a foreign app holding ``spec-builder-<name>`` would otherwise be
    silently re-owned here -- its ``_app`` overwritten and its ``project``
    repointed at our spec's directory, taking the slot (and its transcript) away
    from the app that created it. Mirrors the ownership check
    ``_teardown_worker_slot`` already applies before deleting a slot.
    """
    if state is None:
        return None
    # The NAME is untrusted here for the same reason the indexed working_dir is:
    # handlers reach this with a key read back from index.json, which is app state
    # on disk that the agent this app runs can be talked into rewriting. From here
    # the name becomes a slot key and then a history key. Re-assert the same
    # grammar and redaction-stability predicate creation and discovery enforce.
    if not _usable_name(name):
        _audit("spec_slot_name_denied", _redact_and_truncate(name, 64), outcome="denied")
        logger.warning("refusing a spec slot for a name that fails the grammar")
        return None
    # Resolve once before any await so a same-name recreation cannot swap the
    # identity mid-flight.
    slot_key = _slot_key(name)
    existing = state.get_slot(slot_key) if hasattr(state, "get_slot") else None
    if existing is None:
        # Pull the transcript back before anything creates an empty slot under
        # this key. A restored slot lands in
        # state._slots, so the ownership check below governs it exactly as it
        # governs a live one: a transcript whose metadata says another app owns
        # it is refused, not adopted.
        await _restore_worker_transcript(state, name, adopt_closed=adopt_closed)
        if _slot_identity_moved(name, slot_key):
            return None
        existing = state.get_slot(slot_key) if hasattr(state, "get_slot") else None
    if existing is not None:
        owner = getattr(existing, "_app", None)
        # Only a slot ALREADY owned by this app may be adopted. An UNSCOPED slot
        # under our key is somebody else's conversation -- a main-chat session
        # that happens to be named `spec-builder-<x>` -- and adopting it
        # rewrote its ownership, repointed its project and pulled its transcript
        # into this app. The embedded chat mounts only after this endpoint has
        # created and scoped the slot, so nothing legitimate arrives unscoped.
        if owner != APP_NAME:
            _audit(
                "spec_slot_foreign_denied",
                f"{name}: owned by {owner or 'nobody'}",
                outcome="denied",
            )
            logger.warning(
                "spec slot %s is owned by %s — refusing to take it over", name, owner or "nobody"
            )
            return None
        slot = existing
        created = False
    else:
        slot = state.get_or_create_slot(name=slot_key, app=APP_NAME)
        created = True
    # The indexed working_dir is NOT trusted input. It is app state on disk, and
    # the agent this app runs can be talked into rewriting files -- so a rewritten
    # index entry would become the worker's cwd on the next message, and relative
    # reads from a credential directory would sidestep every per-path check this
    # app makes. Re-validate through the same chokepoint every caller-supplied
    # directory passes, off the event loop, and REFUSE the slot if it does not
    # hold: a spec whose working dir is unusable must not run at all.
    #
    # ABSENT counts as unusable, which is why this is not gated on `wd` being
    # truthy. `create` rejects an empty or relative working_dir with a 400 and
    # discovery always stamps the root it scanned, so no legitimate entry reaches
    # here without one -- but deleting the key is exactly the edit the agent can
    # make, and skipping the check for it left the slot with no project at all.
    # An unscoped slot is worse than a mis-scoped one: chat_runner passes
    # cwd=slot.project, so the worker's CLI would inherit the GATEWAY's working
    # directory and run every approved relative tool from there.
    wd = str(meta.get("working_dir", ""))
    safe_wd = await asyncio.to_thread(_safe_dir, wd) if wd else None
    if safe_wd is None:
        _audit("spec_working_dir_denied", f"{name}: {_redact(wd)}", outcome="denied")
        logger.warning("spec %s has no usable indexed working_dir — refusing", name)
        return None
    # The app-wide default model, read only for a slot this call CREATED and
    # that has no explicit pick: a per-slot model set through the chat API stays
    # authoritative, and an existing slot restored across a gateway restart must
    # keep running exactly as it was -- the help copy promises a changed default
    # applies to spec sessions started AFTER the change, so re-stamping an
    # adopted slot here would contradict it. Off the loop like every other file
    # read on this path; the identity re-check below covers this await window as
    # well as _safe_dir's.
    default_model = ""
    if created and not str(getattr(slot, "model", "") or ""):
        default_model = str((await asyncio.to_thread(_load_settings)).get("model", "") or "")
    # Second window: _safe_dir ran off-loop, so re-assert the identity before
    # stamping ownership and the project onto the slot. Without this a stale
    # request repointed a replacement spec's worker at ITS OWN directory.
    if _slot_identity_moved(name, slot_key):
        return None
    try:
        slot._app = APP_NAME
        # cwd for the worker's CLI process (chat_runner: cwd=slot.project).
        # Without it the agent must `cd <project>` before every command, which
        # turns every tool pill in the chat into identical cd-noise -- and for a
        # discovered spec it would edit files outside the project entirely.
        if safe_wd is not None:
            slot.project = str(safe_wd)
        # '' = inherit: the session layer's resolution chain applies unchanged.
        # A concrete pick rides slot.model, which chat_runner already resolves
        # first — and if the pick stops being served, its withhold keeps the pin
        # and runs the turn on the backend default with a notice.
        if default_model and not str(getattr(slot, "model", "") or ""):
            slot.model = default_model
        if not getattr(slot, "_titled", False):
            slot.title = f"Spec: {name}"
            slot._titled = True
            if hasattr(state, "push_slot_title"):
                state.push_slot_title(slot.key, slot.title)
    except Exception:
        logger.debug("slot scoping failed for %s", name, exc_info=True)
    return slot


#: Distinguishes "caller did not capture an identity" (legacy, unpinned) from
#: "caller captured NOTHING, so there is nothing of ours to act on". Passing
#: ``None`` for a pin must not silently degrade to unpinned.
_UNPINNED: Any = object()


def _discard_queued_work(slot: Any) -> None:
    """Drop everything that would start a SUCCESSOR turn on this slot.

    Ending a turn is not the same as stopping the work. ``_run_chat`` swallows
    its ``CancelledError`` instead of re-raising, so its end-of-turn block runs
    on a cancel exactly as it does on a clean finish -- and that block requeues
    unconsumed steers, then starts the next queued message, and otherwise hands
    a pending synthesis to ``_run_pending_synthesis``. So a Pause or a Delete
    that only stopped the turn handed the agent its next prompt: it kept editing
    the user's spec files after the click, and for Delete it kept writing into a
    directory the request was about to archive.

    Three sources can each relaunch, so all three are dropped:
    ``_queue`` (queued messages), ``_pending_steers`` (requeued to the HEAD of
    the queue by the end-of-turn block, so they become queue items) and
    ``_pending_synthesis`` (a subagent-synthesis turn).

    Call this BEFORE any stop -- cooperative or cancel. A cooperative
    ``stop_turn`` ends the turn too, so clearing after it races the successor.

    Attribute-tolerant on purpose: a foreign or partially-built slot may not
    carry these, and failing to discard must never be what breaks teardown.
    """
    for attr in ("_queue", "_pending_steers"):
        seq = getattr(slot, attr, None)
        if seq is None:
            continue
        try:
            seq.clear()
        except Exception:
            logger.debug("could not clear %s during stop", attr, exc_info=True)
    try:
        slot._pending_synthesis = False
    except Exception:
        logger.debug("could not clear _pending_synthesis during stop", exc_info=True)


async def _teardown_worker_slot(
    state: Any, name: str, *, only_slot: Any = _UNPINNED, require_archive: bool = False
) -> bool:
    """Remove this spec's worker slot, cancelling any in-flight turn.

    Mirrors the gateway's own slot-delete sequence: pop from the registry BEFORE
    any await (so nothing can re-enter it mid-teardown), then cancel the running
    task and await it with a bounded shield, then persist the slot as closed.

    Only ever touches a slot this app owns (``slot._app == APP_NAME``) — a
    foreign or unscoped slot is left alone rather than deleted by name collision.

    ``only_slot`` pins it to the exact slot OBJECT the caller captured. The
    registry is keyed by name, so an abort path that tears down "by name" would
    destroy the slot of a same-name spec created while the request was in flight.

    Returns False ONLY when ``require_archive`` was asked for and persisting the
    conversation failed. Every refusal path returns True: there is no transcript of
    OURS at risk (no slot, a replacement, or a foreign owner), so a caller must not
    treat it as data loss and abort.
    """
    if state is None:
        return True
    if only_slot is None:
        return True  # pinned, but nothing was captured -> nothing of ours to tear down
    # The captured slot's own key wins when the caller pinned one: recomputing from
    # the name would look up a DIFFERENT slot once keys are per-creation.
    slot_key = getattr(only_slot, "key", None) or _slot_key(name)
    if not isinstance(slot_key, str) or not _SLOT_KEY_RE.match(slot_key):
        slot_key = _slot_key(name)
    try:
        slot = state.get_slot(slot_key)
    except Exception:
        slot = None
    if slot is None:
        return True
    if only_slot is not _UNPINNED and slot is not only_slot:
        logger.warning("refusing to tear down slot %s: replaced since capture", slot_key)
        return True
    if getattr(slot, "_app", None) != APP_NAME:
        logger.warning("refusing to tear down slot %s: not owned by %s", slot_key, APP_NAME)
        return True
    # Before the cancel below: _run_chat's end-of-turn block would otherwise
    # start the next queued prompt, so the agent would keep writing into a spec
    # directory this request is about to archive.
    _discard_queued_work(slot)
    try:
        state._slots.pop(slot_key, None)
    except Exception:
        logger.debug("slot registry pop failed for %s", slot_key, exc_info=True)
    task = getattr(slot, "task", None)
    if getattr(slot, "running", False) and task is not None:
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=2.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        except Exception:
            logger.debug("worker task raised during teardown of %s", slot_key, exc_info=True)
    # circular import (see module header): dashboard.server imports this module.
    from kiro_crew.dashboard.chat_persistence import save_slot_off_loop

    try:
        await save_slot_off_loop(state, slot, closed=True, best_effort=not require_archive)
    except Exception:
        # The transcript is the user's data. A caller that is about to drop the
        # spec from the index (delete) asks for require_archive, because reporting
        # success here would discard a conversation that was never written. The
        # slot is put back so the caller can restore the entry and the user can
        # retry; callers that do not require the archive keep the old
        # best-effort behaviour (an abort path has already lost the race).
        logger.warning("closing save failed for %s", slot_key, exc_info=True)
        if require_archive:
            try:
                state._slots[slot_key] = slot
            except Exception:
                logger.warning("could not restore slot %s after a failed archive", slot_key)
            _audit("spec_slot_archive_failed", name, outcome="denied")
            return False
    _audit("spec_slot_teardown", name)
    return True


async def _halt_active_turn(state: Any, name: str, *, only_slot: Any = _UNPINNED) -> bool:
    """Stop the spec slot's in-flight turn, keeping the slot and its transcript.

    Unlike ``_teardown_worker_slot`` (used by DELETE) this does not remove the
    slot -- Pause must leave the conversation intact so the user can resume.
    Returns True when a running turn was stopped.
    """
    if only_slot is None:
        return False  # pinned, but nothing was captured
    slot_key = getattr(only_slot, "key", None) or _slot_key(name)
    slot = state.get_slot(slot_key) if state is not None else None
    if slot is None or not getattr(slot, "running", False):
        return False
    if only_slot is not _UNPINNED and slot is not only_slot:
        logger.warning("refusing to stop slot %s: replaced since capture", slot_key)
        return False
    # Ownership must be EXACT, as it is in _ensure_worker_slot and
    # _teardown_worker_slot. Tolerating an unscoped owner here meant a plain
    # `POST /api/chat` on slot `spec-builder-<name>` -- somebody else's
    # conversation that merely shares the key -- could be cancelled mid-turn by
    # this app's Stop button, losing that turn's response.
    if getattr(slot, "_app", None) != APP_NAME:
        return False
    # Before BOTH stops below. The cooperative stop_turn also ends the turn, so
    # clearing after it would race _run_chat's end-of-turn block into starting
    # the next queued prompt -- Pause would return ok while the agent carried on.
    _discard_queued_work(slot)
    try:
        # circular import (see module header): dashboard.server imports us.
        from kiro_crew.dashboard.chat_utils import _history_key_for

        await state.sessions.stop_turn(_history_key_for(slot.key), force=False)
    except Exception:
        logger.debug("cooperative stop failed for %s", name, exc_info=True)
    task = getattr(slot, "task", None)
    if task is not None and not task.done():
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=2.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        except Exception:
            logger.debug("worker task raised while pausing %s", name, exc_info=True)
    return True


# ── slot turn relay (embedded chat) ──────────────────────────────────────────


def _dispatch_turn(
    state: Any,
    slot: Any,
    message: str,
    *,
    message_meta: dict[str, str] | None = None,
    append_user: bool = True,
    directive_user_origin: bool = False,
    on_consumed: Callable[[bool], None] | None = None,
    on_irreversibly_consumed: Callable[[], Awaitable[None] | None] | None = None,
) -> asyncio.Task[Any] | None:
    """Relay a turn into the spec's agent slot with its structural provenance."""
    if getattr(slot, "running", False):
        try:
            # Deferred to avoid the dashboard import cycle. A spec slot is
            # app-scoped, so an UNMARKED plain entry would fail the drain's
            # closed-world re-check.
            # The stamp records app=True at admission, which the drain treats as
            # designed behaviour rather than a containment change.
            from kiro_crew.dashboard.session_control import containment_meta

            slot.queue_append(
                message,
                meta=containment_meta(state, slot),
                directive_user_origin=directive_user_origin,
            )
        except Exception:
            logger.debug("queue_append failed", exc_info=True)
        try:
            # _redact, not the raw message: `queued` is NOT one of the roles
            # _ChatSlot.append suppresses the global SSE push for (only "chunk",
            # "done" and "user" are), so this text goes to every connected
            # dashboard client. The host sanitizes the stored value on its own
            # steer/queue paths for the same reason -- raw content must not reach
            # an external surface -- and _redact is this module's copy of that
            # chain, failing closed when the security module is unavailable.
            slot.append("queued", _redact(message))
        except Exception:
            pass
        state.push_slots_update()
        return None
    # circular import (see module header): dashboard.server imports this module.
    from kiro_crew.dashboard.chat_runner import _run_chat

    try:
        # Deferred like the other dashboard imports; the resolver follows a
        # raised agent.chat_turn_timeout_secs above the 2h default and runs
        # OFF the event loop (inside the task, via asyncio.to_thread).
        from kiro_crew.dashboard.turn_dispatch import bounded_chat_turn
    except Exception:  # pragma: no cover - resolver always present in prod
        bounded_chat_turn = None  # type: ignore[assignment]

    if append_user:
        if message_meta:
            slot.append("user", message, meta=message_meta)
        else:
            slot.append("user", message)
    run_chat = _run_chat(
        state,
        slot,
        message,
        _directive_user_origin=directive_user_origin,
        _on_consumed=on_consumed,
        _on_irreversibly_consumed=on_irreversibly_consumed,
    )
    if bounded_chat_turn is not None:
        task = asyncio.create_task(bounded_chat_turn(run_chat))
    else:
        task = asyncio.create_task(asyncio.wait_for(run_chat, timeout=float(CHAT_TURN_TIMEOUT)))
    slot.task = task
    state._background_tasks.add(task)
    task.add_done_callback(state._background_tasks.discard)
    state.push_slots_update()
    return task


def _reserve_slot_turn(state: Any, slot: Any) -> asyncio.Task[Any] | None:
    """Make every ordinary turn starter observe this slot as busy across awaits.

    The request task is a temporary turn owner. A dashboard chat request that passed
    its first idle check before this reservation may still overwrite ``slot.task``;
    callers therefore pass the returned identity through to the final dispatch gate.
    The done callback only clears its own reservation and cannot erase such a turn.
    """
    if getattr(slot, "running", False):
        return None
    reservation = asyncio.current_task()
    if reservation is None:  # pragma: no cover - handlers always run in a task
        return None
    slot.task = reservation

    def _release(done: asyncio.Task[Any]) -> None:
        if getattr(slot, "task", None) is not done:
            return
        slot.task = None
        if not getattr(slot, "_queue", None):
            return
        # A generic chat message that arrived while validation was in flight was
        # legitimately queued behind the reservation. If validation refuses, no
        # decision turn exists to drain it, so hand the queue to the host runner.
        from kiro_crew.dashboard.chat_runner import _start_next_queued_turn

        drain = asyncio.create_task(_start_next_queued_turn(state, slot))
        slot.task = drain
        state._background_tasks.add(drain)
        drain.add_done_callback(state._background_tasks.discard)

    reservation.add_done_callback(_release)
    return reservation


async def _serialize_messages(state: Any, slot_key: str) -> list[dict]:
    """Return the spec slot's transcript for the embedded chat view. Prefers the
    live in-memory slot (includes in-progress turns); falls back to the persisted
    session log. Content is redacted before leaving the backend.

    ASYNC because the fallback reads the persisted transcript: a whole JSONL file
    off disk, which is exactly the case that matters (a rehydrated session with no
    in-memory messages, i.e. right after a gateway restart, which is when the user
    opens the spec again). Doing that inline stalled the gateway event loop for
    the length of the file.
    """
    msgs: list[Any] = []
    slot = state.get_slot(slot_key)
    if slot is not None and getattr(slot, "messages", None):
        msgs = list(slot.messages)
    else:
        try:
            # circular import (see module header): dashboard.server imports us.
            from kiro_crew.dashboard.chat_utils import _history_key_for

            if getattr(state, "conversation_log", None) is not None:
                msgs = await asyncio.to_thread(
                    state.conversation_log.read_messages, _history_key_for(slot_key)
                )
        except Exception:
            logger.debug("read_messages failed for %s", slot_key, exc_info=True)
    out: list[dict] = []
    for m in msgs:
        if isinstance(m, dict):
            role, content, ts = m.get("role", ""), m.get("content", ""), m.get("ts", "")
        else:
            role = getattr(m, "role", "")
            content = getattr(m, "content", "")
            ts = getattr(m, "ts", "")
        if role == "system":
            continue
        if role == "tool":
            # Mirror the main chat: surface tool activity as a compact line
            # (first line, bounded) so the embedded chat shows the agent working.
            first = (content or "").strip().splitlines()[0] if content else ""
            out.append({"role": "tool", "content": _redact_and_truncate(first, 200), "ts": ts})
            continue
        out.append({"role": role, "content": _redact(content or ""), "ts": ts})
    return out
