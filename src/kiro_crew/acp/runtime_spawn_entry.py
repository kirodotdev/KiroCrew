"""The runtime's spawn entry point.

``spawn`` under the cold-start admission, the one re-derive retry after a stale spawn
spec, the one retry after moving a broken kiro-cli KAS extract aside, and the revive of a
runtime whose start failed before either retry.

Each function here whose first parameter is ``self`` is an ``AcpRuntime`` method: the class
binds it by name, so it reads and writes the runtime's own state on the instance, and a patch
of the method on the class or on an instance reaches every caller.

Instance fields written: ``_dead``, ``_death_expected``, ``_death_label``,
``_death_reason``, ``_death_summary``, ``_death_tail``, ``_process_tree_confirmed_dead``,
``_kas_extract_broken_message``, ``_kas_extract_server_path``, ``_saw_auth_failure``,
``_saw_delegate_launch_failure``, ``_saw_kas_engine_refusal``, ``_saw_sandbox_init_failure``,
``_scratch_dir``, ``_stall_turn_sessions``, ``_stderr_lines``, ``_stdin_stall_death``,
``chat_joinable``.

Instance fields read: ``_acp_backend``, ``_answer_tasks``, ``_broken_kas_server_path``,
``_initialized``, ``_process``, ``_remote_workspace``, ``_session_queues``,
``_spawn_admitted_repairing_kas_extract``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Callable

from kiro_crew import runtime_death
from kiro_crew.acp import kas_extract_repair, runtime_start
from kiro_crew.acp.session_handle import (
    AcpRequestTimeout,
    AcpRuntimeDead,
    AcpRuntimeError,
    AcpRuntimeOverloaded,
)
from kiro_crew.acp.types import ACP_BACKEND_KAS
from kiro_crew.start_priority import START_QUEUE_ADMISSION, StartPriority, notify_start_queue

if TYPE_CHECKING:
    from kiro_crew.acp.runtime import AcpRuntime

#: The runtime's logger: this code logged under that name before the split.
logger = logging.getLogger("kiro_crew.acp.runtime")


async def spawn(
    self: AcpRuntime,
    *,
    start_priority: StartPriority = StartPriority.BACKGROUND,
    on_gate_queued: Callable[..., None] | None = None,
    on_gate_acquired: Callable[..., None] | None = None,
) -> None:
    """Start the ACP runtime behind the gateway-wide cold-start admission gate.

    ``start_priority`` orders this spawn in the admission queue (rule:
    ``kiro_crew.start_priority``). ``on_gate_queued(queue)`` /
    ``on_gate_acquired(queue_wait_ms, queue)`` bracket the admission wait, with
    ``queue`` = :data:`START_QUEUE_ADMISSION`, as they bracket the
    ``session/new`` gate in :meth:`create_session`.
    """
    if self._process is not None:
        raise AcpRuntimeError("Runtime already spawned")
    self._process_tree_confirmed_dead = False

    admission = runtime_start._cold_start_admission()
    notify_start_queue(logger, on_gate_queued, START_QUEUE_ADMISSION)
    wait_ms = await admission.acquire(start_priority)
    started = time.monotonic()
    outcome = "error"
    try:
        # Inside the try: the permit is released below whatever the callback does.
        notify_start_queue(logger, on_gate_acquired, wait_ms, START_QUEUE_ADMISSION)
        logger.info(
            "acp_cold_start stage=queue_wait outcome=admitted priority=%s wait_ms=%.1f "
            "active_starts=%d queued_starts=%d %s",
            start_priority.value,
            wait_ms,
            admission.active,
            admission.queued,
            admission.semaphore.describe(),
        )
        if self.acp_backend == ACP_BACKEND_KAS:
            await self._spawn_admitted_repairing_kas_extract()
            # The engine this start ended on, after any fallback, decides whether
            # other chats may join the process (``RuntimeOwnership.acquire``). Only a
            # verdict that the engine shares opens it: no verdict (the preview was
            # off when this start resolved its route, even if it is on now) keeps
            # it closed, so absence of evidence keeps one process per chat, at any
            # cap (``RuntimeOwnership.acquire``).
            verdict = getattr(self._harness, "spawn_engine_shares_chats", None)
            self.chat_joinable = verdict is True
        else:
            await self._spawn_admitted_rederiving_once()
        outcome = "ready"
    except asyncio.CancelledError:
        outcome = "cancelled"
        raise
    finally:
        process = self._process
        if process is None:
            process_state = "absent"
        elif process.returncode is None:
            process_state = "running"
        else:
            process_state = "exited"
        logger.info(
            "acp_cold_start stage=spawn outcome=%s duration_ms=%.1f backend=%s "
            "active_starts=%d queued_starts=%d process_state=%s",
            outcome,
            (time.monotonic() - started) * 1000.0,
            self._acp_backend or "kiro",
            admission.active,
            admission.queued,
            process_state,
        )
        admission.release(start_priority)


def kas_extract_broken_message(self: AcpRuntime) -> str:
    """The fix-it message when this runtime's last start died on a broken KAS extract, else ``""``.

    Read by the provider's startup-death translation (``_kas_extract_failure``), beside
    :meth:`saw_delegate_launch_failure`.
    """
    return self._kas_extract_broken_message


def _broken_kas_server_path(self: AcpRuntime) -> str | None:
    """The server path a failed start's kiro-cli could not resolve, from the stderr latch.

    Called only on a KAS runtime (:func:`spawn` routes a KAS start here and no other).
    The line is kiro-cli's own error for its v3 engine, so the bundle route, which runs
    Node directly, cannot print it, and :func:`kas_extract_repair.repair` still requires
    the path to sit in kiro-cli's data directory.
    """
    # ``getattr``: a runtime built without ``__init__`` (test doubles).
    return getattr(self, "_kas_extract_server_path", "") or None


async def _spawn_admitted_repairing_kas_extract(self: AcpRuntime) -> None:
    """:meth:`_spawn_admitted_rederiving_once`, retried ONCE after repairing a broken KAS extract.

    KAS runtimes only: :func:`spawn` calls it when ``acp_backend`` is the KAS harness and
    calls :meth:`_spawn_admitted_rederiving_once` directly for every other harness.

    A start whose kiro-cli exits with ``failed to canonicalize KAS server path``
    found its unpacked engine incomplete, and kiro-cli fails that way on every respawn
    until the directory is moved (:mod:`kiro_crew.acp.kas_extract_repair`). When the
    guards allow, the directory is moved aside and a local-workspace handle starts once
    more, through the same :meth:`_revive_after_failed_start` the re-derive retry uses.
    A remote-workspace handle propagates the first error because its relay is closed.
    When they do not, or the second start fails the same way, the original error propagates
    with :attr:`_kas_extract_broken_message` set, which the provider turns into one
    message naming the directory and the command, and the broken extract is recorded so
    the warm pool stops respawning against it. Every other failure propagates untouched.
    """
    self._kas_extract_broken_message = ""
    try:
        await _spawn_retrying_engine_in_place(self)
        return
    except AcpRuntimeError as first:
        server_path = self._broken_kas_server_path()
        if server_path is None:
            if not await _fell_back_from_preview_bundle(self, first):
                raise
            return
        failure: AcpRuntimeError = first
    verdict = await asyncio.to_thread(kas_extract_repair.repair, server_path)
    if verdict.retry:
        if getattr(self, "_remote_workspace", None):
            # The failed start closed this handle's relay, so the next handle must retry.
            raise failure
        if not await self._revive_after_failed_start():
            # The directory is moved (or already gone), so the next handle's start
            # unpacks a fresh copy: the fix-it message and the pool hold do not apply.
            raise failure
        try:
            await self._spawn_admitted_rederiving_once()
            return
        except AcpRuntimeError as second:
            again = self._broken_kas_server_path()
            if again is None:
                raise
            failure, server_path = second, again
    self._kas_extract_broken_message = kas_extract_repair.fix_message(verdict)
    kas_extract_repair.note_blocked(verdict, server_path)
    raise failure


def _engine_start_failure(exc: AcpRuntimeError) -> bool:
    """Whether a failed start's error came from the engine process itself.

    The engine died before ``initialize`` completed (``AcpRuntimeDead``), or left
    ``initialize`` unanswered while nothing throttled it (``AcpRequestTimeout``
    that is not ``AcpRuntimeOverloaded``). Every other ``AcpRuntimeError`` a
    start raises is a refusal the default route would meet too.
    """
    if isinstance(exc, AcpRuntimeDead):
        return True
    return isinstance(exc, AcpRequestTimeout) and not isinstance(exc, AcpRuntimeOverloaded)


#: Starts of Crew's own engine on a host it already ran on, before the chat's own
#: Retry: the first plus this many more, :data:`ENGINE_RETRY_DELAY_SECS` apart.
ENGINE_RETRIES = 2
ENGINE_RETRY_DELAY_SECS = 5.0

#: What a chat says when Crew's engine still does not start after those retries. The
#: cause is in the gateway log; the chat offers its own Resume.
ENGINE_DID_NOT_START = "Kiro Crew's engine didn't start."


async def _retry_wait(secs: float) -> None:
    """The pause between in-place engine starts; its own seam, so a test records it."""
    await asyncio.sleep(secs)


def _bundle_start(runtime: AcpRuntime) -> bool:
    """Whether this runtime's last plan ran Crew's own engine (any route reason)."""
    harness = getattr(runtime, "_harness", None)
    return bool(getattr(harness, "bundle_engine_version", ""))


async def _fixed_in_place(runtime: AcpRuntime) -> bool:
    """Whether a failed start of Crew's engine is retried here rather than falling back.

    Every bundle route except the two with a kiro-cli fallback: the sharing preview
    (its own cooldown) and the build default on a host the engine never ran on (the
    first switch). Blocking reads off the loop.
    """
    from kiro_crew.acp import kas_build_default, kas_route

    reason = getattr(getattr(runtime, "_harness", None), "spawn_route_reason", "")
    if reason == kas_route.REASON_SHARING_PREVIEW:
        return False
    if reason == kas_route.REASON_BUILD_DEFAULT:
        return await asyncio.to_thread(kas_build_default.already_on_engine)
    return True


async def _spawn_retrying_engine_in_place(runtime: AcpRuntime) -> None:
    """:meth:`_spawn_admitted_rederiving_once`, with Crew's engine fixed in place.

    A start of Crew's own engine that dies or leaves ``initialize`` unanswered
    (:func:`_engine_start_failure`) on a route fixed in place (:func:`_fixed_in_place`)
    is started again, up to :data:`ENGINE_RETRIES` more times
    :data:`ENGINE_RETRY_DELAY_SECS` apart, and then fails with
    :data:`ENGINE_DID_NOT_START`, the cause going to the log. Any other failure
    propagates as it was, for the kiro-cli fallback
    (:func:`_fell_back_from_preview_bundle`). A start that succeeds records that the
    engine ran here (``kas_build_default.remember_engine_ran``).
    """
    from kiro_crew.acp import kas_build_default

    attempt = 0
    while True:
        try:
            await runtime._spawn_admitted_rederiving_once()
        except AcpRuntimeError as exc:
            if not (_bundle_start(runtime) and _engine_start_failure(exc)):
                raise
            if getattr(runtime, "_remote_workspace", None) or not await _fixed_in_place(runtime):
                raise
            logger.warning(
                "Kiro Crew's KAS engine did not start (attempt %d of %d): %s",
                attempt + 1,
                ENGINE_RETRIES + 1,
                exc,
            )
            if attempt >= ENGINE_RETRIES or not await runtime._revive_after_failed_start():
                raise AcpRuntimeError(ENGINE_DID_NOT_START) from exc
            attempt += 1
            await _retry_wait(ENGINE_RETRY_DELAY_SECS)
            continue
        if _bundle_start(runtime):
            await asyncio.to_thread(kas_build_default.remember_engine_ran)
        return


async def _fell_back_from_preview_bundle(runtime: AcpRuntime, failure: AcpRuntimeError) -> bool:
    """Retry a failed start ONCE on the default KAS route when the preview or the
    build default chose the bundle on a host the engine never ran on.

    The ``chat_runtime_sharing`` preview puts KAS on Crew's own engine
    (``kas_route.REASON_SHARING_PREVIEW``), and so does a packaged build's preference
    (``kas_route.REASON_BUILD_DEFAULT``). A plan that built but whose engine
    then died or left ``initialize`` unanswered must not cost the chat: the
    failure is recorded (``kas_route.note_preview_bundle_failed``, or
    ``kas_build_default.note_first_switch_failed`` for a first switch), which keeps
    the route off the bundle, and the sharing gate from sharing, for a cooldown,
    and this handle starts once more, where ``resolve_spawn`` takes the default
    route. True when that start succeeded; the build default's then carries the
    chat's notice. A host the engine already ran on never falls back here: its
    failure was retried in place (:func:`_spawn_retrying_engine_in_place`).

    Only an engine failure counts (:func:`_engine_start_failure`): a refusal that
    has nothing to do with the engine -- a derived spec that changed mid-start, a
    timeout while the agents slice is throttled -- would fail the same way on the
    default route, so it propagates unchanged, records nothing and starts no
    second process. A configured ``agent.kas_bundle`` start, a relay start and a
    remote-workspace handle (whose relay the failed start closed) answer False,
    so their error propagates as before.

    shortcut: a chat whose placement already joined this founding during the
    failed start lands on the fallback process; the gate refuses every placement
    after the record, so at most the starts already in flight share it. Upgrade
    by closing the entry to joins (``RuntimeOwnership.close_to_joins``) here if
    that window is ever seen.
    """
    from kiro_crew.acp import kas_build_default, kas_route

    harness = getattr(runtime, "_harness", None)
    reason = getattr(harness, "spawn_route_reason", "")
    if reason not in (kas_route.REASON_SHARING_PREVIEW, kas_route.REASON_BUILD_DEFAULT):
        return False
    if not _engine_start_failure(failure):
        return False
    if reason == kas_route.REASON_BUILD_DEFAULT:
        if await asyncio.to_thread(kas_build_default.already_on_engine):
            return False
        kas_build_default.note_first_switch_failed(str(failure))
    else:
        kas_route.note_preview_bundle_failed(str(failure))
    if (
        getattr(runtime, "_remote_workspace", None)
        or not await runtime._revive_after_failed_start()
    ):
        return False
    await runtime._spawn_admitted_rederiving_once()
    # The notice names kiro-cli, so it is set only when the respawn ran on the relay.
    respawned = getattr(harness, "spawn_route", None)
    on_relay = respawned is not None and not getattr(respawned, "bundle", True)
    if reason == kas_route.REASON_BUILD_DEFAULT and harness is not None and on_relay:
        harness.engine_fallback_notice = kas_build_default.ENGINE_FALLBACK_NOTICE
    return True


async def _spawn_admitted_rederiving_once(self: AcpRuntime) -> None:
    """``_spawn_admitted``, retried ONCE when the post-handshake bracket fires.

    The bracket around a derived spec's load fails CLOSED: a write to the default
    spec landing between the pre-spawn gate and the ``initialize`` response kills
    the child, because the spec it loaded may be a generation nobody verified. That
    is the right answer for a revocation. It is the WRONG surface for a benign write
    -- the dashboard's MCP sync, an app registration, the periodic rebuild -- whose
    only effect on the worker is that its mirror needs re-deriving, and which
    happens to land inside a spawn's few-hundred-millisecond window. Those are
    ordinary events, and surfacing each as a failed dispatch would make the fleet
    flaky exactly when an operator is changing things.

    So: one retry, and only for THAT refusal. The second attempt runs the pre-spawn
    gate again, which re-derives the mirror from the default as it now stands, and
    spawns a fresh child on it. If the default is still moving the second bracket
    fires too and the refusal propagates -- a file that will not hold still across
    two spawns is not a benign write. Never a loop: the exit condition is another
    process leaving the file alone.

    Narrow on purpose. ``DerivedSpecStale`` from ``_initialize`` is the post-check;
    the pre-spawn gate's own refusal (a mirror that CANNOT be re-derived) arrives as
    ``AcpRuntimeError`` and is not retried, because a second attempt would fail the
    same way for the same reason.

    The first attempt's reap is a ``kill()``, which marks this runtime dead, so the
    retry runs only after :meth:`_revive_after_failed_start` has cleared that death.
    When it cannot -- the reap did not confirm the first child gone, or a reply
    task the first child started would not stop -- the stale spec is reported as
    the failure, unretried: a second child beside a first one this handle still
    holds is exactly the leak the failed-start guard exists to prevent.
    """
    from kiro_crew.agent import DerivedSpecStale

    try:
        await self._spawn_admitted()
    except DerivedSpecStale as first:
        if not await self._revive_after_failed_start():
            logger.warning(
                "acp_cold_start stage=rederive outcome=not_retried backend=%s reason=%s: "
                "the failed start left a child or a reply task this runtime could not "
                "confirm stopped",
                self._acp_backend or "kiro",
                first,
            )
            raise AcpRuntimeError(str(first)) from first
        logger.info(
            "acp_cold_start stage=rederive outcome=retry backend=%s reason=%s",
            self._acp_backend or "kiro",
            first,
        )
        try:
            await self._spawn_admitted()
        except DerivedSpecStale as second:
            raise AcpRuntimeError(str(second)) from second


async def _revive_after_failed_start(self: AcpRuntime) -> bool:
    """Clear the death a failed start's reap left, so this handle can start again.

    The failed-start guard reaps the child with ``kill()``, and ``kill()`` marks
    the runtime dead; every request path refuses a dead runtime, so a second
    start on this handle would have its own ``initialize`` refused with "runtime
    is dead" and never reach the check it is retrying. A start that failed
    before ``initialize`` completed served no session, so its death has no
    tenant left to tell, and clearing it is what lets the same handle start
    again.

    Clears what the dead child left behind that a later reading would attribute
    to the next one: the death state and record, its stderr ring, the auth and
    sandbox latches armed from it, and its scratch directory, which the next
    launch would otherwise adopt as the session's tree. Fields the next spawn
    assigns before it reads them (pid, start identity, capabilities) are left
    for it to assign.

    The child's reply tasks are cancelled and awaited first. ``kill()`` does
    not stop them, and the runtime being dead is the only thing that keeps one
    of them -- a host-auth answer waiting on a token refresh -- from writing its
    reply into the next child's stdin under an id that child never sent.

    Returns False, changing nothing about the death, unless the reap dropped the
    process handle AND confirmed the root and its tracked descendants gone. A
    declined kill, a Windows tree drain still pending, or a root that outlived
    its signals all leave that unconfirmed, and spawning a second child beside
    the first is the leak the guard prevents. Also False once a session is
    registered, which a start that never initialized cannot have, and when a
    reply task outlives its cancellation.
    """
    if (
        not self._dead
        or self._process is not None
        or not self._process_tree_confirmed_dead
        or self._initialized
        or self._session_queues
    ):
        return False
    replies = [task for task in self._answer_tasks if not task.done()]
    for task in replies:
        task.cancel()
    if replies:
        _, still_running = await asyncio.wait(replies, timeout=self._REVIVE_ANSWER_DRAIN_TIMEOUT)
        if still_running:
            return False
    self._scratch_dir = None
    self._dead = False
    self._process_tree_confirmed_dead = False
    self._death_summary = None
    self._death_reason = ""
    self._death_label = ""
    self._death_tail = ""
    self._death_expected = False
    self._stdin_stall_death = False
    self._stall_turn_sessions = frozenset()
    self._stderr_lines.clear()
    self._saw_auth_failure = False
    self._saw_sandbox_init_failure = False
    self._saw_delegate_launch_failure = False
    self._kas_extract_server_path = ""
    self._saw_kas_engine_refusal = False
    runtime_death.retract(self)
    return True
