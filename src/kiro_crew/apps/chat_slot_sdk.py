"""Chat Slot SDK -- an app creates and drives its own dashboard chat slots.

An app whose worker is a visible chat (a spec writer's conversation, embedded in
the app's page and listed under the app in the sidebar) needs what the dashboard's
own chat does: create the slot, restore its transcript after a restart, start a
turn or queue one behind a running turn, stop a turn, archive the slot, read its
transcript, and keep an AutoNudge loop on it. Each of those is a private gateway
function. This SDK is the one public door to them, and it fixes the parts an app
must not choose:

* **Identity.** A slot key must be ``<app>-<id>`` (*id* is 1 to 400 of
  ``A-Z a-z 0-9 . _ -``), the grammar ``ctx.turns`` keys use, and every slot this
  SDK opens is stamped with the app's name (``slot._app``), which the chat runner
  hands the tool gate, so ``POLICY ∩ PROFILE`` resolves with this app's profile.
  Every method that acts on a slot first checks that the slot carries THIS app's
  name and key prefix and raises :class:`ChatSlotRefused` otherwise, so an app
  cannot drive, stop or archive a person's chat, a core session, or another app's
  slot that happens to share a key. An unscoped slot under the app's key (a chat a
  person opened with that name) is someone else's and is refused too.
* **Admission.** A turn runs through the gateway's own chat runner, bounded by the
  session's turn ceiling, so it passes the same admission, approval and stop paths
  a typed dashboard message does. A message sent while a turn runs is queued with
  the containment stamp every plain queue producer records, so the drain re-checks
  it against the constraints that hold when it is delivered.
* **Untrusted text.** The ``queued`` row a queued message adds is pushed to every
  connected dashboard, so it carries the text after :class:`ScrubSDK` redaction,
  never the raw message, and a redaction failure withholds it.
* **AutoNudge.** :meth:`ChatSlotSDK.nudges` and
  :meth:`ChatSlotSDK.nudge_maintenance` see only the loops on slots this app
  owns (the live slot's ``_app``, or the owner a saved transcript records), never
  a loop that merely shares the key prefix: an app name can extend another's;
  :meth:`ChatSlotSDK.arm_nudge` arms through the shared authorization chokepoint
  ``POST /api/autonudge`` uses, with ``source="app:<this app>"``.

Present on ``ctx.chat_slots`` only when the manifest declares
``permissions.chatSlots`` as the literal ``true`` (App Kit seam level 7). Every
slot method takes the dashboard state the gateway hands each route as
``request.app["state"]``; the AutoNudge views default to the gateway's own. The
gateway functions behind it are looked up when a method runs, so a context built
before the gateway is ready still works.

It is not a boundary against the app's own in-gateway Python, which runs trusted
and could reach these functions anyway; it is the supported surface, which keeps
working when they move, and the place the identity checks live.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Awaitable, Callable

from kiro_crew.apps.agent_turn_sdk import _KEY_ID_RE
from kiro_crew.apps.scrub_sdk import ScrubSDK

logger = logging.getLogger(__name__)

#: Seconds a stop waits for the cancelled turn task to finish.
TURN_CANCEL_WAIT_SECS = 2.0

#: The turn actor every turn this seam starts carries, queued or direct: the one an
#: app's own ``/api/chat`` send carries, so admission charges the automation budget
#: and the crew log records the app, never a person.
_APP_TURN_ACTOR = "app"

#: The ``queued`` row's text when the message could not be redacted.
QUEUED_TEXT_WITHHELD = "[unavailable: redaction is not available]"


class ChatSlotRefused(PermissionError):
    """The slot or loop is not one of this app's own."""


def _host(module: str, name: str) -> Any:
    """``module.name``, read when called so a moved or replaced name is seen."""
    return getattr(importlib.import_module(module), name)


def _optional_host(module: str, name: str) -> Any | None:
    try:
        return _host(module, name)
    except Exception:  # noqa: BLE001 - any failure means "this gateway has none"
        return None


def _background_tasks(state: Any) -> Any:
    return getattr(state, "_background_tasks", None)


def _track(state: Any, task: asyncio.Task[Any]) -> None:
    tasks = _background_tasks(state)
    if tasks is None:
        return
    tasks.add(task)
    task.add_done_callback(tasks.discard)


def _max_key_chars() -> int:
    """The longest key this seam accepts: the length the slot registry folds a long key to.

    Below the registry's unfolding limit, so the registry keeps the key as spelled,
    and at most as long as a key the gateway itself makes, so the transcript file
    named after it, its ``.lock`` sidecar and its archive segments fit a file name.
    """
    directory = "kiro_crew.dashboard.state_parts.slot_directory"
    return (
        int(_host(directory, "_SLOT_KEY_KEEP_CHARS"))
        + 1
        + int(_host(directory, "_SLOT_KEY_HASH_CHARS"))
    )


def _live(state: Any, key: Any) -> Any:
    getter = getattr(state, "get_slot", None)
    return getter(key) if callable(getter) and isinstance(key, str) else None


async def _load_window(slot: Any) -> None:
    """Read a restored sidebar row's rows in, off the event loop (no-op otherwise)."""
    if getattr(slot, "window_pending", False) is not True:
        return
    await _host("kiro_crew.dashboard.state", "load_window_off_loop")(slot)


async def _await_cancelled(task: asyncio.Task[Any], what: str) -> None:
    """Wait a bounded time for a cancelled *task*; a slow or raising one is logged."""
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=TURN_CANCEL_WAIT_SECS)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        pass
    except Exception:
        logger.debug("turn task raised while %s", what, exc_info=True)


class ChatSlotSDK:
    """Create and drive chat slots for one app, under that app's own key prefix."""

    __slots__ = ("_app", "_scrub", "_http_app")

    def __init__(self, app_name: str, *, http_app: Any = None) -> None:
        """*http_app* is the gateway's Application, read for the dashboard state when
        an AutoNudge call is not handed one."""
        if not isinstance(app_name, str) or not app_name:
            raise ValueError("a chat slot SDK needs the app's name")
        self._app = app_name
        self._scrub = ScrubSDK()
        self._http_app = http_app

    @property
    def app_name(self) -> str:
        return self._app

    # ── identity ────────────────────────────────────────────────────────────

    def is_own_key(self, key: object) -> bool:
        """Whether *key* is spelled as one of this app's slot keys (``<app>-<id>``).

        At most :func:`_max_key_chars` long: a key the registry folds names a
        different slot and transcript than the one spelled, so every lookup by the
        spelled key would miss, and a longer key's transcript file name would not fit.
        """
        prefix = f"{self._app}-"
        return (
            isinstance(key, str)
            and key.startswith(prefix)
            and len(key) <= _max_key_chars()
            and _KEY_ID_RE.fullmatch(key[len(prefix) :]) is not None
        )

    def check_key(self, key: object) -> str:
        """*key* when it is one of this app's slot keys; else :class:`ValueError`."""
        if self.is_own_key(key):
            return key  # type: ignore[return-value]
        raise ValueError(f"slot key {key!r} is not a {self._app!r} key ({self._app}-<id>)")

    def owns(self, slot: Any) -> bool:
        """Whether *slot* is this app's: stamped with its name, under its key prefix."""
        return (
            slot is not None
            and getattr(slot, "_app", None) == self._app
            and self.is_own_key(getattr(slot, "key", None))
        )

    def _require_own(self, slot: Any) -> Any:
        if not self.owns(slot):
            raise ChatSlotRefused(f"that chat slot is not one of {self._app!r}'s own")
        return slot

    def _require_live(self, state: Any, slot: Any) -> Any:
        """*slot* when it is this app's AND the live slot under its key.

        For every call the gateway carries out by key (a turn on the key's session,
        a stop, a title push, an AutoNudge loop): an object the app kept after the
        slot was archived still carries this app's stamp, while its key may now
        hold another app's slot, since one app name can extend another's.
        """
        self._require_own(slot)
        if _live(state, slot.key) is not slot:
            raise ChatSlotRefused(f"chat slot {slot.key!r} is not the live slot under its key")
        return slot

    def _saved_meta(self, state: Any, key: str) -> dict[str, Any]:
        """The metadata line of the saved transcript under *key*; ``{}`` for none. Blocking."""
        log = getattr(state, "conversation_log", None)
        if log is None:
            return {}
        history_key = _host("kiro_crew.dashboard.chat_utils", "_history_key_for")(key)
        meta = log.get_metadata(history_key)
        return meta if isinstance(meta, dict) else {}

    def _saved_owner(self, state: Any, key: str) -> str:
        """The app a saved transcript under *key* records as its owner. Blocking."""
        return str(self._saved_meta(state, key).get("app") or "")

    def _state(self, state: Any) -> Any:
        if state is not None:
            return state
        getter = getattr(self._http_app, "get", None)
        return getter("state") if callable(getter) else None

    # ── slots ───────────────────────────────────────────────────────────────

    def get(self, state: Any, key: str) -> Any | None:
        """The live slot under *key* when it is this app's, else ``None``."""
        self.check_key(key)
        getter = getattr(state, "get_slot", None)
        slot = getter(key) if callable(getter) else None
        return slot if self.owns(slot) else None

    async def restore(self, state: Any, key: str, *, adopt_closed: bool) -> Any | None:
        """Bring *key*'s persisted transcript back into a cold slot; the slot, or ``None``.

        Slots live in memory, so a gateway restart or idle cleanup drops one while
        its transcript stays on disk. Call this before :meth:`open` on a key with no
        live slot, or the conversation starts empty. *adopt_closed* restores a
        transcript recorded as closed too: pass it for a slot whose lifecycle the
        app owns (idle cleanup closes those on idleness alone), and never for a key
        being created anew, which may carry an archived conversation.

        Returns the restored slot only when it is this app's. With no live slot, the
        saved transcript's recorded owner is read first (off the event loop): one
        another app or a person owns is never brought back, so nothing of it reaches
        the dashboard, and :meth:`open` refuses the key. ``None`` also when nothing
        was restored.
        """
        self.check_key(key)
        if state.get_slot(key) is None:
            if await asyncio.to_thread(self._saved_owner, state, key) != self._app:
                return None
        rehydrate = _host(
            "kiro_crew.dashboard.chat_persistence", "rehydrate_slot_from_history_async"
        )
        restored = await rehydrate(state, key, adopt_closed=adopt_closed)
        if not self.owns(restored):
            return None
        await _load_window(restored)
        return restored

    async def open(self, state: Any, key: str) -> tuple[Any, bool]:
        """This app's slot under *key*, created when absent: ``(slot, created)``.

        A slot already under *key* that is not this app's (another app's, or an
        unscoped chat a person opened) raises :class:`ChatSlotRefused` and is left
        untouched: opening it would rewrite its ownership and pull its transcript
        into this app. So does a saved transcript under the key that records any
        other owner, read off the event loop when no slot is live. No await between
        the last lookup and the create, so nothing can put a slot under the key in
        between. Read the key back from ``slot.key``:
        the gateway folds it to the charset of a session file name. A slot restored
        as a sidebar row has its rows read in, off the event loop, before it is
        returned; :meth:`send` refuses one that has not.
        """
        self.check_key(key)
        existing = state.get_slot(key)
        if existing is None:
            meta = await asyncio.to_thread(self._saved_meta, state, key)
            if meta and str(meta.get("app") or "") != self._app:
                raise ChatSlotRefused(f"the saved conversation under {key!r} is not this app's")
            existing = state.get_slot(key)
        if existing is not None:
            self._require_own(existing)
            await _load_window(existing)
            return existing, False
        slot = state.get_or_create_slot(name=key, app=self._app)
        # get_or_create_slot folds the key to the session-file charset and answers
        # a slot already under the folded form; one that is not this app's is
        # refused like an existing one.
        if getattr(slot, "_app", None) != self._app:
            raise ChatSlotRefused(f"chat slot {key!r} is held by someone else")
        return slot, True

    def set_title(self, state: Any, slot: Any, title: str, *, only_untitled: bool = False) -> bool:
        """Title this app's *slot* and push it to the sidebar; whether it was set.

        *only_untitled* leaves a slot that already has a title alone (one the user
        renamed keeps their name). The sidebar push reaches every connected
        dashboard, so the title is stored redacted, and withheld (``False``, the
        slot untouched) when redaction fails.
        """
        self._require_live(state, slot)
        if only_untitled and getattr(slot, "_titled", False):
            return False
        try:
            scrubbed = self._scrub.outbound(str(title)).text
        except Exception:  # noqa: BLE001 - a failed scrub withholds, never passes through
            logger.warning("%s: redaction failed; withholding a slot title", self._app)
            return False
        slot.title = scrubbed
        slot._titled = True
        # The title lives on the transcript's metadata line, and the periodic flush
        # saves only a dirty slot: an idle slot would otherwise keep it in memory only.
        slot._dirty = True
        push = getattr(state, "push_slot_title", None)
        if callable(push):
            push(slot.key, slot.title)
        return True

    def track(self, state: Any, task: asyncio.Task[Any]) -> None:
        """Hold *task* with the gateway's background work until it finishes.

        The gateway cancels and awaits those at shutdown, so an app's follow-up
        task on a slot (a settlement, a release) is not dropped mid-write.
        """
        _track(state, task)

    # ── turns ───────────────────────────────────────────────────────────────

    def send(
        self,
        state: Any,
        slot: Any,
        message: str,
        *,
        meta: dict[str, Any] | None = None,
        append_user: bool = True,
        user_origin: bool = False,
        typed: bool = True,
        on_consumed: Callable[[bool], None] | None = None,
        on_irreversibly_consumed: Callable[[], Awaitable[None] | None] | None = None,
        reservation: asyncio.Task[Any] | None = None,
    ) -> asyncio.Task[Any] | None:
        """Start a turn on this app's *slot*, or queue *message* behind the one running.

        Returns the turn's task, or ``None`` when the message was queued; a queued
        message carries no consumption callbacks, so a caller that needs them
        checks for ``None``. A queued message keeps *user_origin* only when it is
        also *typed*: the queue drain runs every entry as typed, so a composed
        message queued with the person's origin would pass the guards that only a
        typed message may. *reservation* is the task :meth:`reserve` returned:
        while ``slot.task`` is still that reservation it is released and the turn
        starts in its place, with no await in between. *meta*
        rides on the user row (and on the queue entry, so a queued message keeps
        it). *append_user* adds the user row before the turn; pass ``False`` when
        it is already there. *user_origin* says a person sent this text (so a
        session directive in it may act on the user's behalf) and *typed* that
        they typed it rather than composed it from a template; both default to
        the cautious answer. *on_consumed* and *on_irreversibly_consumed* are the
        chat runner's own consumption callbacks, passed through. A slot restored as
        a sidebar row must come through :meth:`open` or :meth:`restore` first, which
        read its rows in off the event loop; one that has not raises
        ``RuntimeError``.
        """
        self._require_live(state, slot)
        if getattr(slot, "window_pending", False) is True:
            raise RuntimeError("open() the slot first: its rows are not read in yet")
        if reservation is not None and getattr(slot, "task", None) is reservation:
            slot.task = None
        if getattr(slot, "running", False):
            self._queue(state, slot, message, meta=meta, user_origin=user_origin and typed)
            return None
        run_chat = _host("kiro_crew.dashboard.chat_runner", "_run_chat")
        spawn = _host("kiro_crew.dashboard.turn_dispatch", "spawn_guarded_turn")
        if append_user:
            # A note a restart left held on this idle slot is owed to this turn,
            # above its user row, as every direct turn starter delivers it.
            flush_restored_hold = _optional_host(
                "kiro_crew.dashboard.slot_buffers", "flush_restored_hold"
            )
            if flush_restored_hold is not None:
                flush_restored_hold(slot, logger=logger)
            if meta:
                slot.append("user", message, meta=meta)
            else:
                slot.append("user", message)
        turn = run_chat(
            state,
            slot,
            message,
            _directive_user_origin=user_origin,
            _message_is_typed=typed,
            _turn_actor=_APP_TURN_ACTOR,
            _on_consumed=on_consumed,
            _on_irreversibly_consumed=on_irreversibly_consumed,
        )
        # Under the operator's unattended-turn cap, as an app's own /api/chat turn
        # is: it queues behind the cap and is counted there (inert for a slot a
        # person is driving).
        capped = getattr(state, "run_background_turn", None)
        if callable(capped):
            turn = capped(slot, turn)
        # The gateway's guarded start, the one /api/chat uses: the session's turn
        # ceiling, the task held with the gateway's background work, a failure card
        # the dashboard shows, and the hand-off of a queue that a turn the cap
        # abandoned before it started leaves behind.
        task = spawn(state, slot, turn)
        slot.task = task
        state.push_slots_update()
        return task

    def _queue(
        self,
        state: Any,
        slot: Any,
        message: str,
        *,
        meta: dict[str, Any] | None,
        user_origin: bool,
    ) -> None:
        try:
            # An app slot is scoped, so an UNSTAMPED entry would fail the drain's
            # closed-world re-check; the stamp records the containment that held
            # at admission, as every plain queue producer does.
            containment_meta = _host("kiro_crew.dashboard.session_control", "containment_meta")
            actor_key = _host("kiro_crew.dashboard.chat_delivery", "TURN_ACTOR_META_KEY")
            slot.queue_append(
                message,
                meta={
                    **(meta or {}),
                    **containment_meta(state, slot),
                    actor_key: _APP_TURN_ACTOR,
                },
                directive_user_origin=user_origin,
            )
        except Exception:
            logger.debug("%s: queueing a message failed", self._app, exc_info=True)
        try:
            # `queued` is not a role the slot keeps off the global push, so this
            # row reaches every connected dashboard: redacted, or withheld.
            slot.append("queued", self._redacted(message))
        except Exception:
            logger.debug("%s: the queued row could not be added", self._app, exc_info=True)
        state.push_slots_update()

    def _redacted(self, text: str) -> str:
        if not isinstance(text, str) or not text:
            return text or ""
        try:
            return self._scrub.outbound(text).text
        except Exception:  # noqa: BLE001 - a failed scrub withholds, never passes through
            logger.warning("%s: redaction failed; withholding a queued message", self._app)
            return QUEUED_TEXT_WITHHELD

    def reserve(self, state: Any, slot: Any) -> asyncio.Task[Any] | None:
        """Mark this app's idle *slot* busy for the calling task; the reservation, or ``None``.

        For a request that must validate across awaits before it starts a turn:
        while the reservation holds, every other starter sees the slot running and
        queues. A dashboard message that passed its own idle check first may still
        take ``slot.task``, so compare it with the returned reservation before
        starting the turn. When the calling task ends while it still holds the
        reservation, it is released, and anything queued meanwhile is handed to the
        gateway's queue drain. ``None`` when a turn is already running.
        """
        self._require_live(state, slot)
        if getattr(slot, "running", False):
            return None
        reservation = asyncio.current_task()
        if reservation is None:  # pragma: no cover - a route always runs in a task
            return None
        slot.task = reservation

        def _release(done: asyncio.Task[Any]) -> None:
            if getattr(slot, "task", None) is not done:
                return
            slot.task = None
            if not getattr(slot, "_queue", None):
                return
            start_next = _host("kiro_crew.dashboard.chat_runner", "_start_next_queued_turn")
            drain = asyncio.create_task(start_next(state, slot))
            slot.task = drain
            _track(state, drain)

        reservation.add_done_callback(_release)
        return reservation

    def discard_queued(self, slot: Any) -> None:
        """Drop everything that would start a successor turn on this app's *slot*.

        The chat runner's end-of-turn block runs on a cancel as on a clean finish,
        and it starts the next queued message, re-queues unconsumed steers, or runs
        a pending subagent synthesis. A stop that only ended the turn would hand the
        agent its next prompt. :meth:`stop_turn` and :meth:`archive` call this
        before they stop anything; call it yourself only to clear without stopping.
        """
        self._require_own(slot)
        for attr in ("_queue", "_pending_steers"):
            seq = getattr(slot, attr, None)
            if seq is None:
                continue
            try:
                seq.clear()
            except Exception:
                logger.debug("%s: could not clear %s", self._app, attr, exc_info=True)
        try:
            slot._pending_synthesis = False
        except Exception:
            logger.debug("%s: could not clear the pending synthesis", self._app, exc_info=True)

    async def stop_turn(self, state: Any, slot: Any) -> bool:
        """Stop the turn running on this app's *slot*, keeping the slot and its transcript.

        Drops queued work first, then asks the session to stop cooperatively
        (skipped while an automatic compaction holds the session, which would
        decline it), then cancels the turn task and waits up to
        :data:`TURN_CANCEL_WAIT_SECS` for it. Returns whether a turn was running.
        """
        self._require_live(state, slot)
        if not getattr(slot, "running", False):
            return False
        history_key = _host("kiro_crew.dashboard.chat_utils", "_history_key_for")(slot.key)
        self.discard_queued(slot)
        # Read before the cooperative stop's await: the turn can end during it and a
        # successor take ``slot.task``, which this stop must not cancel.
        task = getattr(slot, "task", None)
        compacting = _optional_host("kiro_crew.session_lifecycle", "compaction_in_flight")
        sessions: Any = getattr(state, "sessions", None)
        if compacting is None or not compacting(sessions, history_key):
            try:
                await sessions.stop_turn(history_key, force=False)
            except Exception:
                logger.debug("%s: cooperative stop failed", self._app, exc_info=True)
        if task is not None and not task.done():
            task.cancel()
            await _await_cancelled(task, "stopping a turn")
        return True

    async def archive(self, state: Any, slot: Any) -> bool:
        """Remove this app's *slot*, ending its turn, and save its transcript as closed.

        Through the gateway's own close (``close_slot``, what a tab's close button
        runs), which retires the slot's AutoNudge loop, cancels the turn, persists
        the slot as closed, tears its session down, and owns every race on the way:
        a refused save, a session deleted meanwhile, a same-key slot opened during
        the close. Queued work is dropped first, so the cancelled turn starts
        nothing. A *slot* that is not the one the registry holds under its key
        (replaced since the caller captured it) is left alone. A slot-close hook
        this app registered runs, as for a person's close.

        Returns ``False`` when the close did not commit: the gateway has put the
        slot back where the key allows, and the caller should not report the
        conversation gone.
        """
        self._require_own(slot)
        if state.get_slot(slot.key) is not slot:
            return True
        self.discard_queued(slot)
        close = _host("kiro_crew.dashboard.chat_handlers", "close_slot")
        try:
            await close(state, slot, slot.key)
        except Exception:
            logger.warning("%s: closing a slot did not commit", self._app, exc_info=True)
            return False
        return True

    async def transcript(self, state: Any, key: str) -> list[Any]:
        """The rows of this app's slot *key*: the live slot's, else the saved ones.

        Raw rows, as the slot holds them: redact before serving them anywhere. A
        live slot under *key* that is not this app's yields ``[]``, and so does a
        saved transcript that does not record this app as its owner. Every disk
        read runs off the event loop.
        """
        self.check_key(key)
        live = state.get_slot(key)
        if live is not None:
            if not self.owns(live):
                return []
            await _load_window(live)
            if getattr(live, "messages", None):
                return list(live.messages)
        log = getattr(state, "conversation_log", None)
        if log is None:
            return []
        history_key = _host("kiro_crew.dashboard.chat_utils", "_history_key_for")(key)

        def _read() -> list[Any]:
            if self._saved_owner(state, key) != self._app:
                return []
            return list(log.read_messages(history_key) or [])

        return await asyncio.to_thread(_read)

    # ── AutoNudge ───────────────────────────────────────────────────────────

    def nudges(self, state: Any = None) -> AppNudges | None:
        """The live AutoNudge loops on this app's slots, or ``None`` when it is not running.

        A loop is this app's when a live slot under its key is (``owns``); a key
        spelled under the app's prefix proves nothing, since another app's name
        can extend this one's. A loop whose slot is not live is not listed here;
        :meth:`nudge_maintenance` reads saved ownership for those. *state*
        defaults to the gateway's dashboard state. Answers without blocking, so
        it may be read on the event loop.
        """
        get_instance = _optional_host("kiro_crew.autonudge", "get_instance")
        service = get_instance() if get_instance is not None else None
        if service is None:
            return None
        return AppNudges(self, service, self._state(state))

    @asynccontextmanager
    async def nudge_maintenance(self, state: Any = None) -> AsyncIterator[AppNudges]:
        """This app's loops in the AutoNudge store, serialized with startup and peers.

        For recovery work that must see durable loops while no timer can fire: a
        loop paused with ``deactivate_and_wait`` stays paused until it is removed
        or the block exits. Ownership is read once on entry: a live slot's owner,
        else the owner its saved transcript records (read off the event loop).
        """
        state = self._state(state)
        service_cls = _host("kiro_crew.autonudge", "AutoNudgeService")
        async with service_cls.maintenance_service() as view:
            keys = {
                str(getattr(loop, "slot_key", "") or "")
                for loop in view.list_all()
                if self.is_own_key(getattr(loop, "slot_key", None))
            }
            cold = [key for key in keys if _live(state, key) is None]
            saved = await asyncio.to_thread(
                lambda: {key for key in cold if self._saved_owner(state, key) == self._app}
            )
            yield AppNudges(self, view, state, maintenance=True, saved_owned=frozenset(saved))

    async def arm_nudge(
        self,
        state: Any,
        slot: Any,
        *,
        message: str,
        idle_secs: int,
        max_cycles: int,
        stop_sentinel_path: str = "",
        caller: str = "",
    ) -> tuple[Any | None, str | None, int]:
        """Arm an AutoNudge loop on this app's *slot*: ``(loop, error, status)``.

        Through the authorization chokepoint ``POST /api/autonudge`` uses, so the
        slot-ownership checks, message limits, the sensitive stop-sentinel refusal
        and the audit row are the same; ``source`` is always ``app:<this app>``.
        *caller* is the requesting user, for the audit row. A loop is armed only
        when *error* is ``None``. When AutoNudge is not running the answer is
        ``(None, <reason>, 503)``.
        """
        self._require_live(state, slot)
        get_instance = _optional_host("kiro_crew.autonudge", "get_instance")
        service = get_instance() if get_instance is not None else None
        if service is None:
            return None, "AutoNudge is not running", 503
        authorize = _host("kiro_crew.autonudge_authz", "authorize_and_add_nudge")
        loop, error, status = await authorize(
            svc=service,
            state=state,
            slot_key=slot.key,
            message=message,
            idle_secs=idle_secs,
            max_cycles=max_cycles,
            stop_sentinel_path=stop_sentinel_path,
            source=f"app:{self._app}",
            caller=caller,
        )
        return loop, error, status


class AppNudges:
    """One app's view of AutoNudge: only the loops on slots it owns.

    Built by :meth:`ChatSlotSDK.nudges` over the live service and by
    :meth:`ChatSlotSDK.nudge_maintenance` over the maintenance view. A loop is
    the app's when the slot under its key is: the live slot's ``_app``, or, in
    the maintenance view, the owner the slot's saved transcript records. Any
    other loop is invisible here, and acting on one by id raises
    :class:`ChatSlotRefused`.
    """

    __slots__ = ("_sdk", "_service", "_state", "_maintenance", "_saved_owned")

    def __init__(
        self,
        sdk: ChatSlotSDK,
        service: Any,
        state: Any,
        *,
        maintenance: bool = False,
        saved_owned: frozenset[str] = frozenset(),
    ) -> None:
        self._sdk = sdk
        self._service = service
        self._state = state
        self._maintenance = maintenance
        self._saved_owned = saved_owned

    def _own(self, loop: Any) -> bool:
        key = getattr(loop, "slot_key", None) if loop is not None else None
        if not self._sdk.is_own_key(key):
            return False
        live = _live(self._state, key)
        if live is not None:
            return self._sdk.owns(live)
        return key in self._saved_owned

    def get_by_slot(self, key: str) -> Any | None:
        """The loop on this app's slot *key*, or ``None``."""
        if not self._sdk.is_own_key(key):
            return None
        loop = self._service.get_by_slot(key)
        return loop if self._own(loop) else None

    def list_all(self) -> list[Any]:
        """Every loop on one of this app's slot keys, active or not."""
        return [loop for loop in self._service.list_all() if self._own(loop)]

    def _require_loop(self, loop_id: str) -> None:
        by_id = getattr(self._service, "get_by_id", None)
        if callable(by_id):
            found = self._own(by_id(loop_id))
        else:
            found = any(str(getattr(loop, "id", "")) == loop_id for loop in self.list_all())
        if not found:
            raise ChatSlotRefused(f"AutoNudge loop {loop_id!r} is not one of this app's")

    async def remove(self, loop_id: str, *, stop_reason: str = "") -> bool:
        """Remove this app's loop *loop_id*; whether it was removed from the store.

        *stop_reason* names it in the stop line. The live service answers ``False``
        when it refused the removal (a maintenance quiesce holds the loop), and the
        loop stays stored. Inside :meth:`ChatSlotSDK.nudge_maintenance` the view
        removes under the lock it already holds, returning nothing and raising on a
        failed write, so a removal that returns is ``True``; no stop line is written
        there, so *stop_reason* is not recorded.
        """
        self._require_loop(loop_id)
        if self._maintenance:
            await self._service.remove(loop_id)
            return True
        return bool(await self._service.remove(loop_id, stop_reason=stop_reason))

    async def remove_on_slot(
        self, key: str, *, only_loop_id: str | None = None, stop_reason: str = ""
    ) -> bool:
        """Remove the loop on this app's slot *key*; whether one was removed.

        *only_loop_id* pins the removal to a loop the caller captured earlier: the
        lookup is by key, and a loop armed on the key since is someone else's run.
        A removal the service refused answers ``False``, as :meth:`remove` does.
        """
        loop = self.get_by_slot(key)
        if loop is None:
            return False
        loop_id = str(getattr(loop, "id", "") or "")
        if not loop_id or (only_loop_id is not None and loop_id != only_loop_id):
            return False
        return await self.remove(loop_id, stop_reason=stop_reason)

    async def deactivate_and_wait(self, loop_id: str, *, stopped_reason: str | None = None) -> bool:
        """Pause this app's loop *loop_id* and wait out a fire in flight; whether it was found.

        Inside :meth:`ChatSlotSDK.nudge_maintenance` the pause holds until the loop
        is removed or the block exits.
        """
        self._require_loop(loop_id)
        if self._maintenance:
            done = await self._service.deactivate_and_wait(loop_id, stopped_reason=stopped_reason)
        else:
            done = await self._service.deactivate_and_wait(loop_id)
        return bool(done)
