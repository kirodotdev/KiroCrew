"""Per-repository checkout reservations that order a branch switch against agent work.

A branch switch rewrites the working tree under every session whose project lies
in the repository. The switch handler refuses while any of those sessions has a
turn running or sub-agents pending, but neither turn admission nor sub-agent
admission takes the switch's lock, so work admitted between that check and the
checkout would read files on one branch and write them back on the other.

The two sides order themselves through this registry, on the event loop:

* the switch calls :func:`reserve` synchronously, BEFORE it awaits its busy
  check, and the reservation is released only when the checkout worker itself
  has finished (:func:`release_when_done`), so a cancelled switch request cannot
  drop it while git is still rewriting files;
* every turn calls :func:`wait_for_checkout` at its entry, before it reads any
  project file, and every sub-agent run awaits
  ``dashboard.repo_checkout_guard.wait_for_subagent_checkout`` just before it
  executes;
* a turn also marks itself unplaced and awaits :func:`wait_for_any_checkout`
  before it claims its session, since where that session runs is the
  allocator's choice, then records the folder the session reports and waits
  out any checkout of it. ``dashboard.repo_checkout_guard.slot_folders`` is what
  the busy check reads;
* a workflow step opens its session outside chat slots and the sub-agent
  manager, so it does the same through :func:`tracked_work`, and the busy check
  reads :func:`tracked_work_folders`;
* underneath all of them, every prompt any agent process is sent -- whatever
  drives it: a channel, a monitor wake, a cron or heartbeat turn, an app -- is
  recorded at the ACP transport from before its write until the backend
  answers it or the process tree is confirmed gone (:func:`prompt_sent`, see
  "Prompts in flight" below), and :func:`tracked_work_folders` reads those too.

Both kinds of work are published before their first step: a turn's task is on
its slot, and a sub-agent run is counted as its parent's pending work. So work
whose entry ran before the reservation is seen by the busy check, which refuses
the switch, and work whose entry runs after it waits until the checkout has
finished. The wait has no deadline: every git call the checkout makes is
bounded, so the worker always finishes, and work that started beside a live
checkout would reopen the race this registry closes.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from typing import Any

logger = logging.getLogger(__name__)

# How often a waiter logs that it is still waiting on a checkout.
WAIT_LOG_SECS = 120.0

_reserved: dict[str, asyncio.Event] = {}


def reserve(root: str) -> bool:
    """Reserve ``root`` (a resolved repository root) for one checkout.

    Returns False when another checkout already holds it.
    """
    if root in _reserved:
        return False
    _reserved[root] = asyncio.Event()
    return True


def release(root: str) -> None:
    """Drop the reservation on ``root`` and wake everything waiting on it."""
    event = _reserved.pop(root, None)
    if event is not None:
        event.set()


def release_when_done(root: str, worker: asyncio.Future) -> None:
    """Release ``root`` once ``worker`` finishes, however its awaiter ends."""

    def _done(future: asyncio.Future) -> None:
        if not future.cancelled():
            # Retrieved so an exception nobody awaited (the switch request was
            # cancelled) is not reported as never retrieved.
            future.exception()
        release(root)

    worker.add_done_callback(_done)


def is_reserved(root: str) -> bool:
    return root in _reserved


def is_any_reserved() -> bool:
    """Whether any checkout is in flight."""
    return bool(_reserved)


def _covering_root(project: str, roots: Iterable[str]) -> str | None:
    """The reserved root that contains ``project`` or that ``project`` contains."""
    resolved = os.path.realpath(project)
    for root in roots:
        try:
            common = os.path.commonpath([resolved, root])
        except ValueError:  # different drives on Windows
            continue
        if common in (resolved, root):
            return root
    return None


async def wait_for_checkout(project: str | None, *, waiter: str = "") -> None:
    """Wait while a checkout holds the repository that ``project`` lies in.

    ``waiter`` names the work that is waiting (a session key or run id) for the
    periodic log line, so a checkout that never finishes can be traced.
    """
    if not project or not _reserved:
        return
    while _reserved:
        snapshot = tuple(_reserved)
        root = await asyncio.to_thread(_covering_root, project, snapshot)
        if root is None:
            # The match ran off the loop, so a switch may have reserved another
            # root meanwhile. Return only when nothing new was reserved since the
            # snapshot: the caller then records its work with no further await.
            if all(reserved in snapshot for reserved in _reserved):
                return
            continue
        event = _reserved.get(root)
        if event is None:
            continue
        try:
            await asyncio.wait_for(event.wait(), WAIT_LOG_SECS)
        except asyncio.TimeoutError:
            logger.warning(
                "%s still waiting on a checkout of %s before starting work",
                waiter or "work",
                root,
            )


async def wait_for_any_checkout(*, waiter: str = "") -> None:
    """Wait until no checkout is in flight anywhere.

    For work whose folder cannot be known yet, so no single repository's
    reservation can be matched against it.
    """
    while _reserved:
        root, event = next(iter(_reserved.items()))
        try:
            await asyncio.wait_for(event.wait(), WAIT_LOG_SECS)
        except asyncio.TimeoutError:
            logger.warning("%s still waiting on a checkout of %s", waiter or "work", root)


#: Ends a step's session for good: called with ``escalate`` false on the first
#: attempt and true on each retry, and answers whether the session's process tree
#: is confirmed gone. Raising counts as not yet.
SessionStop = Callable[[bool], Awaitable[bool]]


class TrackedWork:
    """Session work that runs outside chat slots and the sub-agent manager.

    A workflow step opens its own session through the session manager, so
    neither the slot scan nor the sub-agent registry sees it. It registers
    here instead (:func:`tracked_work`): unplaced while its session is being
    acquired, since where the session lands is the allocator's choice, then
    placed on the folder that session reports (:meth:`place`). A step whose
    session reports no folder stays unplaced for its whole run.
    """

    __slots__ = ("ended_cleanly", "folders", "placed", "provider", "stop", "waiter")

    def __init__(self, waiter: str) -> None:
        self.folders: tuple[str, ...] = ()
        self.placed = False
        self.provider: Any = None
        self.stop: SessionStop | None = None
        self.ended_cleanly = False
        self.waiter = waiter

    async def place(self, provider: Any, *also: object, stop: SessionStop | None = None) -> None:
        """Record where the work runs and wait out a checkout of any of it.

        ``provider`` is the session the work drives: the folder it reports is
        where its native turn runs. ``also`` names any other folder the work
        reads, such as the project its prompt is built against, which can
        differ from a reused session's folder. ``stop`` ends that session for
        good; work that runs a turn passes it, and unless the backend itself
        reported that turn finished (:meth:`turn_ended`) the block's exit
        awaits it before the record goes (:func:`tracked_work`). Everything is recorded with no await
        before it, so a busy check that runs after this call sees it and one
        that ran before it saw the work unplaced. Only the provider's own
        report places the work.
        """
        self.provider = provider
        if stop is not None:
            self.stop = stop
            self.ended_cleanly = False
        session_folder = str(getattr(provider, "cwd", "") or "")
        folders = [f for f in (session_folder, *(str(x or "") for x in also)) if f]
        self.folders = (*self.folders, *folders)
        if session_folder:
            self.placed = True
        for folder in folders:
            await wait_for_checkout(folder, waiter=self.waiter)

    def arm(self, stop: SessionStop | None) -> None:
        """Owe ``stop`` on an exit before the backend reports the turn finished.

        For work that places its session before the turn starts and only runs
        a turn later (a channel turn): armed just before the turn, so an exit
        before it ends nothing, and cleared with ``None`` once the backend has
        reported the turn finished (:meth:`turn_ended`). Synchronous, so it
        changes nothing a busy check reads.
        """
        self.stop = stop
        self.ended_cleanly = False

    def turn_ended(self, event: Any) -> None:
        """Hear the turn's completion event (``stream_and_collect``'s ``on_complete``).

        Only an end of turn the backend itself reported counts: one of the
        protocol's own stop reasons (:data:`BACKEND_STOP_REASONS`) on a
        completion that is not synthetic. The session layer also ends turns on
        its own -- a stalled tool, a cancel the backend never acknowledged, a
        failed compaction, a stale turn it gave up on -- and the collector
        returns early at its tool-call ceiling with no completion at all; in
        each of those the backend may still be running the turn's tools, so the
        step's session is ended. Positive evidence rather than a list of bad
        exits, so an exit added later fails closed.
        """
        self.ended_cleanly = (
            getattr(event, "stop_reason", None) in BACKEND_STOP_REASONS
            and getattr(event, "synthetic_completion", False) is not True
        )


#: The ACP protocol's stop reasons: each is the backend answering the prompt, so
#: its turn is over. An absent reason is a backend that does not fill the field
#: (``classify_stop_reason`` reads it as a normal end too); the session layer
#: always names its own terminals (``timeout``, ``stale_recover`` and the
#: ``error:`` family), and none of them is here.
BACKEND_STOP_REASONS = frozenset(
    {"end_turn", "max_tokens", "max_turn_requests", "refusal", "cancelled", "", None}
)


_tracked: dict[int, TrackedWork] = {}

#: How often a step's session teardown is retried while it cannot finish.
TEARDOWN_RETRY_SECS = 2.0


def process_tree_gone(provider: Any) -> bool:
    """Whether ``provider``'s runtime confirmed its whole process tree exited.

    The runtime's own teardown evidence, set only when the root and every
    descendant it tracked are gone; a session's removal from the registry, or a
    destroy that returned, is not that.
    """
    return getattr(provider, "process_tree_confirmed_dead", None) is True


async def end_managed_session(
    sessions: Any, key: str, provider: Any, *, escalate: bool = False
) -> bool:
    """End ``provider``'s session for good and report whether its process is gone.

    Destroying the session ends its process, which is the only way to stop a
    native turn its cancelled task left running: cancelling the task closes the
    stream but tells the backend nothing until the session's next prompt. The
    destroy is fenced on the provider, re-checked under the registry lock, so a
    session someone else has since put under ``key`` is never destroyed; a
    session a turn holds (a sibling step on a shared named session) is skipped
    and retried. When ``key`` does not hold ``provider`` -- another
    teardown removed it, perhaps failing part-way -- a retry (``escalate``)
    shuts the provider down itself.

    Done only on the runtime's confirmation that the process tree exited
    (:func:`process_tree_gone`).
    """
    if process_tree_gone(provider):
        return True
    if sessions.get_provider(key) is provider:
        generation = sessions.session_generation(key)
        await sessions.destroy_if(key, generation, lambda: sessions.get_provider(key) is provider)
    elif escalate:
        await provider.shutdown()
    return process_tree_gone(provider)


async def _attempt_stop(work: TrackedWork, *, retry: bool) -> bool:
    """One try at ``work.stop``: whether the session's process is now gone.

    A failure is logged by exception type only, and only on the first try:
    session error text can carry conversation content, and a teardown that keeps
    failing is retried every few seconds.
    """
    if work.stop is None:
        return True
    try:
        return await work.stop(retry) is True
    except Exception as exc:  # noqa: BLE001 - retried; the record stays meanwhile
        if not retry:
            logger.warning(
                "%s: session teardown failed (%s); retrying", work.waiter, type(exc).__name__
            )
        return False


async def _retry_stop(work: TrackedWork) -> None:
    """Keep trying ``work.stop`` until the session's process is gone, then drop
    the record.

    Until then the busy check keeps refusing: dropping the record on a teardown
    that did not finish would let a switch run under a process that may still
    be running the turn's tools, so one that never finishes fails closed. A
    retry that is itself cancelled (the gateway shutting down) leaves the
    record.
    """
    warned = False
    while True:
        await asyncio.sleep(TEARDOWN_RETRY_SECS)
        if await _attempt_stop(work, retry=True):
            _tracked.pop(id(work), None)
            return
        if not warned:
            logger.warning("%s: session process not confirmed gone; still busy", work.waiter)
            warned = True


async def _end_session(work: TrackedWork) -> None:
    """End ``work``'s session once, inline; if that does not finish, retry it
    in the background with the record still registered (:func:`_retry_stop`)."""
    if await _attempt_stop(work, retry=False):
        _tracked.pop(id(work), None)
    else:
        _keep_task(asyncio.ensure_future(_retry_stop(work)))


def _keep_task(task: asyncio.Future) -> asyncio.Future:
    """Hold a reference to a teardown until it finishes."""
    _ending.add(task)
    task.add_done_callback(_ending.discard)
    return task


@contextlib.asynccontextmanager
async def tracked_work(*, waiter: str = "") -> AsyncIterator[TrackedWork]:
    """Register session work for the busy check for the length of the block.

    Registered unplaced before the first await, then waits out every checkout
    in flight, so a switch either sees the work (and refuses) or finishes
    before the work acquires its session.

    A block that placed a provider with a ``stop`` ends that session unless it
    finished normally after a turn the backend reported finished
    (:meth:`TrackedWork.turn_ended`) -- so a cancel, an exception, a turn the
    session layer ended itself and a collector that stopped at its tool-call
    ceiling all end it before the record goes
    (:func:`_end_session`): its native turn may still be running tools, and only
    ending the process stops it. The block waits for the first attempt, so a
    pooled worker's session is gone before the pool can hand the worker to
    another step; an attempt that does not finish is retried in the background
    with the record kept. The teardown runs shielded: a second cancel stops the
    wait, not the teardown, and the record stays until the teardown finishes.
    """
    work = TrackedWork(waiter or "work")
    _tracked[id(work)] = work
    finished = False
    try:
        await wait_for_any_checkout(waiter=work.waiter)
        yield work
        finished = True
    finally:
        if work.stop is None or (finished and work.ended_cleanly):
            _tracked.pop(id(work), None)
        else:
            await asyncio.shield(_keep_task(asyncio.ensure_future(_end_session(work))))


# Teardowns of work that ended abnormally, referenced until they finish.
_ending: set[asyncio.Future] = set()


def tracked_work_folders() -> tuple[list[str], bool]:
    """Folders tracked work runs in, and whether every piece of it is placed.

    Covers both kinds of record: work registered through :func:`tracked_work`
    and every prompt in flight on any agent process (:func:`prompt_sent`). A
    prompt whose session reports no folder counts as unplaced.
    """
    works = list(_tracked.values())
    prompts = list(_prompts.values())
    folders = [folder for work in works for folder in work.folders if folder]
    folders += [folder for folder in prompts if folder]
    return folders, all(work.placed for work in works) and all(prompts)


def folders_overlap(folders: Iterable[str], root: str) -> bool:
    """Whether any of ``folders`` lies in ``root`` or contains it."""
    return any(_covering_root(folder, (root,)) is not None for folder in folders)


# ── Prompts in flight: the seam every agent turn passes ──
#
# Every harness sends a turn as an ACP request on one of two transports:
# ``AcpClient._send_request`` (the adapter harnesses: claude, codex, goose,
# opencode and the rest) and ``AcpRuntime``'s three request writers
# (``send_request``, ``send_request_for_answer``, ``_send_and_await``) on the
# shared kiro / KAS / codex runtime. Each calls :func:`before_prompt` before it
# reserves the request id and :func:`prompt_sent` synchronously once it has it,
# before the frame is written; their frame readers call :func:`prompt_answered`
# on every response, whoever reads it; and their teardowns call
# :func:`prompt_process_gone` once the runtime has confirmed the whole process
# tree exited. So whatever drives a turn -- a dashboard chat, a channel, a
# monitor wake, a cron or heartbeat turn, a sub-agent, a workflow step, an app
# -- the busy check sees it from before the send until the backend itself
# answers the prompt or its process is confirmed gone. A cancel, a timeout, a
# synthetic end of turn the session layer makes up, or a caller that stops
# reading leaves the record: none of them stops the backend's tools. The one
# agent worker that speaks no ACP, the knowledge pool's legacy ``CCWorker``,
# records its own prompts the same way.

#: ``(process instance, request id) -> folder`` for each prompt not yet answered.
#: The process instance is a fresh random id per spawn, so a request id a
#: respawned process reuses never answers its predecessor's prompt.
_prompts: dict[tuple[str, int], str] = {}

#: The keys in :data:`_prompts` that are steers rather than turns.
_steers: set[tuple[str, int]] = set()

#: The requests that start agent work: a turn, and a kiro slash command run
#: through its command extension.
TURN_METHODS = frozenset({"session/prompt", "_kiro.dev/commands/execute"})

#: The requests that carry a user's mid-turn message into a running turn. An
#: answer to one is not the end of a turn: the turn it joined is counted on its
#: own prompt. But an adapter that finds no turn running can start one of its
#: own (codex answers ``startedNewTurn``), and nothing then reports when that
#: turn ends, so such a steer stays counted until its process tree is gone.
STEER_METHODS = frozenset({"_session/steer", "_session/steering", "_goose/unstable/session/steer"})

PROMPT_METHODS = TURN_METHODS | STEER_METHODS

#: Request families that rewrite working-tree files themselves: KAS restores a
#: checkpoint's files (``_kiro/checkpoint/revertMultiple``). Matched by prefix so
#: a checkpoint verb added later is counted without a list to update.
RESTORE_METHOD_PREFIXES = ("_kiro/checkpoint/",)

#: The steering outcome by which an adapter says it started a turn of its own.
STARTED_NEW_TURN = "startedNewTurn"

#: Keys of steers answered :data:`STARTED_NEW_TURN`: latched until the process
#: tree is confirmed gone, so no later frame with the same id releases them.
_adapter_turns: set[tuple[str, int]] = set()


def is_tracked(method: str) -> bool:
    """Whether a request starts agent work or rewrites files, so it is counted."""
    return method in PROMPT_METHODS or method.startswith(RESTORE_METHOD_PREFIXES)


async def before_prompt(folder: str, *, waiter: str = "") -> None:
    """Wait out a checkout of ``folder`` (of every repository when it is unknown).

    Returns without suspending when no checkout is in flight, so a prompt sent
    with no switch in progress gains no wait and no reordering. When it does
    wait, it returns only once a check found nothing reserved that covers the
    folder with no await after it (:func:`wait_for_checkout`), so the caller's
    synchronous :func:`prompt_sent` lands before any later switch can reserve.
    """
    if not _reserved:
        return
    if folder:
        await wait_for_checkout(folder, waiter=waiter or "prompt")
    else:
        await wait_for_any_checkout(waiter=waiter or "prompt")


def prompt_sent(instance: str, request_id: int, folder: str, *, method: str = "") -> None:
    """Record a prompt about to be written; ``folder`` is where its session runs."""
    key = (str(instance), request_id)
    _prompts[key] = str(folder or "")
    if method in STEER_METHODS:
        _steers.add(key)


def _issued_id(request_id: Any) -> int | None:
    """``request_id`` when it can be an id this host issued: a plain int, exactly."""
    return request_id if type(request_id) is int else None


def prompt_answered(instance: str, request_id: Any, result: Any = None) -> None:
    """The backend answered request ``request_id`` (any id: most are not prompts).

    Called only with a response frame (no method) and its own ``id`` as the wire
    carried it, unconverted: only an int equal to the issued id answers a prompt,
    so a ``1.9``, a ``"1"`` or a ``true`` never ends prompt 1. A steer answered
    ``startedNewTurn`` stays counted (:data:`STEER_METHODS`).
    """
    if not _prompts:
        return
    rid = _issued_id(request_id)
    if rid is None:
        return
    key = (str(instance), rid)
    if key not in _prompts or key in _adapter_turns:
        return
    if key in _steers:
        _steers.discard(key)
        outcome = result.get("outcome") if isinstance(result, dict) else None
        if outcome == STARTED_NEW_TURN:
            _adapter_turns.add(key)
            return
    _prompts.pop(key, None)


def prompt_process_gone(instance: str) -> None:
    """Drop every prompt of a process whose whole tree is confirmed exited."""
    if not instance or not _prompts:
        return
    for key in [key for key in _prompts if key[0] == instance]:
        _prompts.pop(key, None)
        _steers.discard(key)
        _adapter_turns.discard(key)
