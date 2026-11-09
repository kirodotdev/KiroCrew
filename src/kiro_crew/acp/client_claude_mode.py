"""A claude session's requested ``auto`` mode: applied before its first prompt, then followed.

The seed asks for ``auto`` as ``permissions.defaultMode``, which the Claude Agent SDK
drops from that tier, so it does not apply the request; a session without an inherited
or resumed mode starts in ``default``. ``session/set_mode``
``auto`` after the starting-mode pin and the startup model applies the request
(:func:`_apply_claude_requested_mode`). From then on every mode report the session
reads is followed (:func:`_note_claude_mode_report`). A report read during start
that leaves it in a mode which approves on its own moves it back to ``default`` before
the first prompt; the same report after start has settled stops the captured process
before the reader can dispatch an agent-started turn. An ``approval_modes`` ceiling installed
mid-session uses one registered deny hook and a weak live-client registry to stop the
captured process and then record its unsettled unasked calls. ``ClaudeAcpClient`` owns
the transport, start-drain, readiness and model-push overrides that place the requested
mode steps in ``client_claude_mode_seams``, so the shared ``AcpClient`` paths remain
unchanged.

Each function here whose first parameter is ``self`` is a ``ClaudeAcpClient`` method: the
class binds it by name, so it reads and writes the session's own state on the instance,
and a patch of the method on the class or on an instance reaches every caller.

Instance fields written: ``_claude_auto_withdrawn``, ``_claude_confirmed_mode``,
``_claude_mode_restore_due``, ``_claude_mode_settling``, ``_claude_mode_watched``,
``_claude_policy_deny_due``, ``claude_mode_pinned_notice``.

Instance fields read: ``_mcp_notifications``, ``_process``, ``_session_id``.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import threading
import weakref
from typing import TYPE_CHECKING, Any

from kiro_crew import platform_compat, process_identity
from kiro_crew.acp import batch_guard, runtime_process_tree
from kiro_crew.acp.client_claude_session import (
    _CLAUDE_MODE_PINNED_NOTICE,
    _CLAUDE_PINNED_MODE,
    _set_claude_pinned_mode_or_stop,
)
from kiro_crew.acp.client_permissions import _scrub_observed
from kiro_crew.acp.client_tool_events import (
    _settle_unsettled_unasked_tool_calls,
    _write_unasked_tool_call_rows,
    _write_unasked_tool_call_rows_sync,
)
from kiro_crew.acp.harness_notices import MODE_ID_MAX
from kiro_crew.acp.transport_errors import AcpError, AcpTimeoutError
from kiro_crew.acp.types import (
    CC_PERMISSION_MODE_AUTO,
    CC_PERMISSION_MODE_DEFAULT,
    UPDATE_CONFIG_OPTION,
    UPDATE_CURRENT_MODE,
    JsonRpcMessage,
)
from kiro_crew.executors import subprocess_executor
from kiro_crew.sel import sel

if TYPE_CHECKING:
    from kiro_crew.acp.client import ClaudeAcpClient

#: The client's own logger: this code logs under that name, as every client owner does.
logger = logging.getLogger("kiro_crew.acp.client")


#: claude permission modes other than ``auto`` in which a call can run without a
#: ``session/request_permission``: ``acceptEdits`` approves file edits, and
#: ``bypassPermissions`` every call. Crew never asks for either; a mode report naming
#: one on a session that requested ``auto`` is restored during start or stops the
#: settled process (:func:`_note_claude_mode_report`), and
#: ``_maybe_audit_unasked_tool_call`` attributes an unasked call to it.
_CLAUDE_MODES_THAT_APPROVE_ON_THEIR_OWN = frozenset({"acceptEdits", "bypassPermissions"})

#: The ACP tool kinds ``acceptEdits`` approves: file edits, the write kinds the batch
#: guard names. Any other call that runs unasked in that mode was approved by one of
#: Claude's own rules, which Crew cannot name.
_CLAUDE_ACCEPT_EDITS_KINDS = batch_guard._WRITE_KINDS

#: Requested-``auto`` clients and the event loop each session runs on. Weak keys keep
#: the policy callback from extending a client's lifetime; the lock covers snapshots
#: taken on the ceiling-installing thread while a session registers on its loop.
_live_requested_auto_clients: weakref.WeakKeyDictionary[
    ClaudeAcpClient, asyncio.AbstractEventLoop
] = weakref.WeakKeyDictionary()
_live_requested_auto_clients_lock = threading.Lock()
_CLAUDE_POLICY_HOOK_OWNER = "claude-requested-auto"


def _watch_live_requested_auto_client(client: ClaudeAcpClient) -> None:
    """Remember a requested-``auto`` client and the loop that owns its transport."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # Production session read-back runs on its client loop. Unit-level callers
        # that invoke the synchronous tracker without one have no live transport.
        return
    with _live_requested_auto_clients_lock:
        _live_requested_auto_clients[client] = loop


def _live_requested_auto_snapshot() -> list[tuple[ClaudeAcpClient, asyncio.AbstractEventLoop]]:
    """Snapshot the weak registry for the ceiling-installing thread."""
    with _live_requested_auto_clients_lock:
        return list(_live_requested_auto_clients.items())


def _captured_process_is_live(client: ClaudeAcpClient, process: Any) -> bool:
    """Whether *process* is still this client's live process."""
    return (
        process is not None
        and getattr(client, "_process", None) is process
        and process.returncode is None
    )


def _stop_claude_without_its_loop(
    client: ClaudeAcpClient,
    process: Any,
    *,
    start_time: str | None = None,
    child_pids: dict[int, Any] | None = None,
) -> None:
    """Stop only the captured adapter tree when its event loop cannot clean up.

    This is the synchronous fallback for :meth:`AcpClient._kill_process`. On POSIX
    the recorded start identity is checked before and after child discovery, and
    :func:`process_identity.isolated_group_of` brackets the group lookup with the
    same check before the verified group is signalled. If the root identity is
    unknown or changed, no root scan or signal is attempted; only recorded
    recorded children are swept. On Windows the spawn-time identity pins the exact
    tree; without it only the captured process object is killed.
    """
    if process is None or process.returncode is not None:
        return
    pid = getattr(process, "pid", None)
    if type(pid) is not int or pid <= 1:
        try:
            process.kill()
        except (ProcessLookupError, OSError):
            pass
        return
    still_current = getattr(client, "_process", None) is process
    if start_time is None and still_current:
        start_time = getattr(client, "_start_time", None)
    if platform_compat.IS_WINDOWS and start_time is None:
        try:
            process.kill()
        except (ProcessLookupError, OSError):
            pass
        return
    merged = dict(
        child_pids
        if child_pids is not None
        else getattr(client, "_child_pids", {}) if still_current else {}
    )
    root_matches = platform_compat.IS_WINDOWS or (
        start_time is not None and platform_compat.get_process_start_id(pid) == start_time
    )
    if root_matches:
        try:
            fresh = runtime_process_tree._get_child_pids(pid)
            if platform_compat.IS_WINDOWS or (
                platform_compat.get_process_start_id(pid) == start_time
            ):
                new_pids = [child for child in fresh if child not in merged]
                if new_pids:
                    merged.update(runtime_process_tree._capture_child_records(new_pids))
            else:
                root_matches = False
        except Exception:
            logger.warning("could not snapshot a captured claude process tree", exc_info=True)
    try:
        if platform_compat.IS_WINDOWS:
            assert start_time is not None
            if not platform_compat.kill_process_tree_pinned(
                pid, start_time, platform_compat.SIGKILL
            ):
                process.kill()
        elif root_matches:
            group = process_identity.isolated_group_of(pid, start_time)
            if group is not None:
                platform_compat.kill_process_group(group, platform_compat.SIGKILL)
    except (ProcessLookupError, OSError, ValueError):
        if platform_compat.IS_WINDOWS:
            try:
                process.kill()
            except (ProcessLookupError, OSError):
                pass
    except Exception:
        logger.warning(
            "could not stop a captured claude session after its event loop closed",
            exc_info=True,
        )
    try:
        runtime_process_tree._kill_escaped_children(merged)
    except Exception:
        logger.warning("could not sweep a captured claude process tree", exc_info=True)


def _stop_claude_without_its_loop_and_record(client: ClaudeAcpClient, process: Any) -> None:
    """Settle calls, stop a closed-loop session, then write its rows synchronously."""
    try:
        rows = _settle_unsettled_unasked_tool_calls(client)
    except Exception:
        rows = []
        logger.warning(
            "could not record unasked claude calls before stopping the session",
            exc_info=True,
        )
    try:
        _stop_claude_without_its_loop(client, process)
    except Exception:
        logger.warning("could not stop a claude session whose loop closed", exc_info=True)
    _write_unasked_tool_call_rows_sync(rows)


async def _stop_claude_auto_for_policy(
    client: ClaudeAcpClient, process: Any, *, unexpected_mode: str | None = None
) -> None:
    """Stop one captured approving process, then record its unsettled calls.

    The identity check keeps a callback queued for an earlier session from touching a
    replacement process. Nothing awaits between that check and settling the current
    turn's unasked calls. The ordinary client stop kills the process tree; its
    synchronous fallback receives the same child snapshot if cancellation or a loop
    failure prevents that cleanup. That fallback can wait on a Windows tree drain or
    spawn ``ps``/``pgrep``, so it runs on the executor ``_kill_process`` uses, not on
    this loop. ``unexpected_mode`` distinguishes a settled mode report from an
    installed-ceiling denial; both fail closed in the same way.
    """
    if not _captured_process_is_live(client, process):
        return
    start_time = getattr(client, "_start_time", None)
    child_pids = dict(getattr(client, "_child_pids", {}))
    try:
        rows = _settle_unsettled_unasked_tool_calls(client)
    except Exception:
        # Audit is best-effort, as on terminal tool updates. Its own failure must
        # not leave an approving mode live.
        rows = []
        logger.warning(
            "could not record unasked claude calls before stopping the session",
            exc_info=True,
        )
    if unexpected_mode is None:
        logger.warning(
            "the approval_modes policy denied 'trust' while this claude session was in a "
            "mode that approves calls without asking; the session is stopped before "
            "another call can run"
        )
    else:
        logger.warning(
            "this settled requested-auto claude session reported mode %r, which approves "
            "calls without asking; the session is stopped before an agent-started turn "
            "can run",
            _scrub_observed(unexpected_mode),
        )
    loop = asyncio.get_running_loop()
    fallback = functools.partial(
        _stop_claude_without_its_loop,
        client,
        process,
        start_time=start_time,
        child_pids=child_pids,
    )
    try:
        await client._claude_stop_session()
    except asyncio.CancelledError:
        await asyncio.shield(loop.run_in_executor(subprocess_executor(), fallback))
        raise
    except Exception:
        await asyncio.shield(loop.run_in_executor(subprocess_executor(), fallback))
        logger.warning("could not stop a claude requested-auto session", exc_info=True)
    finally:
        if unexpected_mode is None:
            client._claude_policy_deny_due = False
        await _write_unasked_tool_call_rows(rows)


def _schedule_claude_stop(
    client: ClaudeAcpClient, process: Any, *, unexpected_mode: str | None = None
) -> asyncio.Task[None] | None:
    """Schedule one tree stop for *process* on its owning event loop."""
    if not _captured_process_is_live(client, process):
        return None
    existing = getattr(client, "_claude_policy_stop_task", None)
    if (
        existing is not None
        and not existing.done()
        and getattr(client, "_claude_policy_stop_process", None) is process
    ):
        return existing
    task = asyncio.create_task(
        _stop_claude_auto_for_policy(client, process, unexpected_mode=unexpected_mode)
    )
    client._claude_policy_stop_process = process
    client._claude_policy_stop_is_policy = unexpected_mode is None
    client._claude_policy_stop_task = task
    return task


def _policy_stop_for_current_process(
    client: ClaudeAcpClient,
) -> asyncio.Task[None] | None:
    """Return the policy stop task that owns this client's current process."""
    task = getattr(client, "_claude_policy_stop_task", None)
    if (
        task is not None
        and getattr(client, "_claude_policy_stop_is_policy", False)
        and getattr(client, "_claude_policy_stop_process", None)
        is getattr(client, "_process", None)
    ):
        return task
    return None


async def _finish_policy_stop_if_due(client: ClaudeAcpClient) -> bool:
    """Let an installed-ceiling stop own the current process, if one is due."""
    task = _policy_stop_for_current_process(client)
    if task is not None:
        await asyncio.shield(task)
        return True
    if not getattr(client, "_claude_policy_deny_due", False):
        return False
    if _claude_in_approving_mode(client):
        await _stop_claude_auto_for_policy(client, getattr(client, "_process", None))
    else:
        client._claude_policy_deny_due = False
    return True


def _start_claude_policy_stop(
    client_ref: weakref.ReferenceType[ClaudeAcpClient], process: Any
) -> None:
    """Create the policy stop task on the loop that owns the captured process."""
    client = client_ref()
    if client is None or not getattr(client, "_claude_policy_deny_due", False):
        return
    _schedule_claude_stop(client, process)


def _on_claude_trust_denied(denied: frozenset[str]) -> None:
    """Stop captured approving sessions when an installed ceiling denies Trust."""
    if "trust" not in denied:
        return
    for client, loop in _live_requested_auto_snapshot():
        process = getattr(client, "_process", None)
        if not _captured_process_is_live(client, process) or not _claude_in_approving_mode(client):
            continue
        if not getattr(client, "_claude_policy_deny_due", False):
            client._claude_policy_deny_due = True
            # Record the resolved policy denial on the installing thread. Any
            # pre-prompt backstop that is already running sees the mark and does not
            # duplicate it before this process stops.
            _audit_claude_requested_auto(client, "approval_mode_denied_by_policy")
        if loop.is_closed() or not loop.is_running():
            _stop_claude_without_its_loop_and_record(client, process)
            client._claude_policy_deny_due = False
            continue
        try:
            loop.call_soon_threadsafe(_start_claude_policy_stop, weakref.ref(client), process)
        except RuntimeError:
            # A loop that closes between the check and the enqueue cannot run the
            # normal stop, so the captured adapter is killed synchronously.
            _stop_claude_without_its_loop_and_record(client, process)
            client._claude_policy_deny_due = False


def _bounded_adapter_mode_id(value: Any) -> str | None:
    """Return one bounded adapter-reported mode id, else ``None``."""
    return value if isinstance(value, str) and len(value) <= MODE_ID_MAX else None


def _reported_mode(msg: JsonRpcMessage, session_id: str | None) -> str | None:
    """The bounded mode a ``session/update`` for ``session_id`` reports, else ``None``.

    claude-agent-acp reports a mode change as a ``current_mode_update``, or as a
    ``config_option_update`` whose ``mode`` option carries the new ``currentValue``:
    its 0.100.0 answer to ``set_mode`` ``auto``
    (``test/fixtures/claude_mode_pin/requested-auto-applied.jsonl``) sends only the
    second. Adapter-reported ids longer than :data:`MODE_ID_MAX` are ignored.
    """
    params = msg.params if isinstance(msg.params, dict) else {}
    update = params.get("update")
    if params.get("sessionId") != session_id or not isinstance(update, dict):
        return None
    kind = update.get("sessionUpdate")
    if kind == UPDATE_CURRENT_MODE:
        return _bounded_adapter_mode_id(update.get("currentModeId"))
    if kind == UPDATE_CONFIG_OPTION:
        options = update.get("configOptions")
        for option in options if isinstance(options, list) else []:
            if isinstance(option, dict) and option.get("id") == "mode":
                return _bounded_adapter_mode_id(option.get("currentValue"))
    return None


def _claude_requested_auto_has_callable_surface(client: ClaudeAcpClient) -> bool:
    """Whether this requested-``auto`` session has any tool Crew must govern."""
    return (
        getattr(client, "_permission_mode", None) == CC_PERMISSION_MODE_AUTO
        and not getattr(client, "_side_read_only", False)
        and not getattr(client, "_spec_zero_tools", False)
    )


def _track_claude_starting_mode(
    self: ClaudeAcpClient, resp: dict, pinned: str | None = None
) -> None:
    """Record the mode a claude session starts in, and follow it from here on.

    Runs right after :meth:`_pin_claude_starting_mode` on every path that establishes
    a session, and ``pinned`` is what that returned: when the pin moved the session,
    it is in ``pinned``, not in the mode ``resp`` still reports. Otherwise it is in
    ``modes.currentModeId``, and with no ``modes`` block nothing is known.

    For a session that requested :data:`CC_PERMISSION_MODE_AUTO` and has a
    callable tool surface, it also turns on ``_claude_mode_watched``: from here on
    every bounded mode report off the wire for this session updates
    ``_claude_confirmed_mode`` (:meth:`_note_claude_mode_report`), whatever Crew is
    waiting for when it arrives. Side Chat and zero-tool sessions are not watched or
    registered because no mode can approve a callable tool there.
    ``_claude_mode_settling`` is set until the start's last step clears it: the
    Claude-owned start drain on a cold start, the end of ``new_conversation`` on a
    fresh one. Until then the start owns the session's mode, so a report read
    meanwhile is restored before the first prompt rather than stopping the session.
    """
    modes = resp.get("modes")
    current = pinned if isinstance(pinned, str) else None
    if current is None and isinstance(modes, dict):
        current = _bounded_adapter_mode_id(modes.get("currentModeId"))
    self._claude_confirmed_mode = current
    self._claude_mode_restore_due = False
    self._claude_policy_deny_due = False
    self._claude_auto_withdrawn = False
    self._claude_mode_settling = True
    self._claude_mode_watched = _claude_requested_auto_has_callable_surface(self)
    if self._claude_mode_watched:
        _watch_live_requested_auto_client(self)


def _note_claude_mode_report(
    self: ClaudeAcpClient, msg: JsonRpcMessage
) -> asyncio.Task[None] | None:
    """Follow one frame's mode report for a session that requested ``auto``.

    ``_read_message`` hands every frame it reads off the wire here while
    ``_claude_mode_watched`` is set. Reports read before the session's start has
    settled (``_claude_mode_settling``: from the read-back after the pin through the
    start drain of a cold start or the end of a fresh conversation) keep the restore
    path, because that start owns stdout and restores before the first prompt; the
    answer to its own ``set_mode`` ``auto`` is one of them. Once the start has
    settled, a report that newly puts the session in
    ``auto``, ``acceptEdits`` or ``bypassPermissions`` schedules the same captured-tree
    stop as a live policy denial. A turn the agent starts on its own does not pass
    through ``ensure_ready``, so waiting for another prompt would leave that turn in
    the approving mode. The caller awaits the returned task before it can read and
    dispatch the next frame; the stop itself never starts a second stdout reader.
    """
    reported = _reported_mode(msg, getattr(self, "_session_id", None))
    if reported is None or reported == getattr(self, "_claude_confirmed_mode", None):
        return None
    self._claude_confirmed_mode = reported
    self._claude_mode_restore_due = reported in _CLAUDE_MODES_THAT_APPROVE_ON_THEIR_OWN or (
        reported == CC_PERMISSION_MODE_AUTO and getattr(self, "_claude_auto_withdrawn", False)
    )
    if not getattr(self, "_claude_mode_settling", False) and _claude_in_approving_mode(self):
        return _schedule_claude_stop(
            self, getattr(self, "_process", None), unexpected_mode=reported
        )
    return None


def _claude_auto_permitted() -> bool:
    """Whether the ``approval_modes`` ceiling lets a session hold Claude's ``auto``.

    In ``auto`` Claude's classifier approves a call without asking anyone, which is
    what a session Trust grants, so the mode is asked as ``trust``
    (``approval_mode_policy.session_mode_permitted``). A memory read of the installed
    ceiling; it answers deny while a ceiling whose rule narrows the scope is being
    installed.
    """
    from kiro_crew.approval_mode_policy import session_mode_permitted

    return session_mode_permitted("trust")


def _claude_in_approving_mode(client: ClaudeAcpClient) -> bool:
    """Whether a requested-``auto`` session is in a mode that approves without asking.

    ``auto`` and the modes that approve on their own
    (:data:`_CLAUDE_MODES_THAT_APPROVE_ON_THEIR_OWN`) all approve a call without
    asking, so none of them is kept once the ceiling denies ``trust``.
    """
    confirmed = getattr(client, "_claude_confirmed_mode", None)
    return getattr(client, "_claude_mode_watched", False) and (
        confirmed == CC_PERMISSION_MODE_AUTO or confirmed in _CLAUDE_MODES_THAT_APPROVE_ON_THEIR_OWN
    )


async def _claude_auto_verdict_is_final() -> bool:
    """Wait out a ceiling install whose rule may narrow ``trust``; whether the verdict is final.

    While such a ceiling is being installed, ``session_mode_permitted`` answers deny
    as a placeholder (``approval_mode_policy.verdict_pending``). A requested ``auto``
    is applied at session start only, so committing that placeholder would keep a
    session out of ``auto`` for its life. This waits for the final answer, off the
    loop (``ensure_resolved``, bounded), as a caller that commits a mode does.
    ``False`` when the install outlasts that bound: the deny then stands for this
    session, but it is no policy decision, so no denial is recorded for it.
    """
    from kiro_crew import approval_mode_policy

    if approval_mode_policy.verdict_pending():
        await asyncio.to_thread(approval_mode_policy.ensure_resolved)
    return not approval_mode_policy.verdict_pending()


def _audit_claude_requested_auto(client: ClaudeAcpClient, outcome: str) -> None:
    """Record the disposition of a requested claude ``auto`` mode.

    The record every approval-mode change writes (``mode_change:<mode>``), under
    the session's key. ``enabled`` means Crew applied the request,
    ``approval_mode_denied_by_policy`` means the policy ceiling refused it, and
    ``kept_inherited_mode`` means a resumed or inherited mode that approves on its
    own was retained: the inherited-mode pin was off, or does not move that mode.
    Best-effort: an SEL failure never changes the mode.
    """
    errors = {
        "approval_mode_denied_by_policy": "mode_disabled_by_policy",
        "kept_inherited_mode": "requested_mode_not_applied",
    }
    try:
        sel().log_api_access(
            caller=getattr(client, "_session_key", None) or "",
            operation="mode_change:claude_auto",
            outcome=outcome,
            source="acp",
            resources=getattr(client, "_session_id", None) or "",
            error=errors.get(outcome, ""),
        )
    except Exception:
        logger.warning("SEL audit of a requested claude auto mode failed", exc_info=True)


async def _audit_claude_requested_auto_off_loop(client: ClaudeAcpClient, outcome: str) -> None:
    """Write a requested-mode SEL row without doing first-use I/O on the ACP loop."""
    await asyncio.to_thread(_audit_claude_requested_auto, client, outcome)


async def _restore_claude_asking_mode_if_due(self: ClaudeAcpClient) -> None:
    """Move a session a mode report put in an approving mode back to ``default``.

    Runs where this client's session lifecycle already owns the stream: at the end
    of ``_initialize_session`` or ``new_conversation``, and in ``ensure_ready`` before
    the caller writes a prompt. Those paths cannot restore concurrently on one client:
    the cold-start drain is inside ``ensure_ready``, a fresh-conversation reset runs
    only on an idle pooled client, and a warm ``ensure_ready`` owns stdout through the
    idle-reader wrapper.

    A move that is refused or unanswered stops the session, as a failed pin does, so
    no prompt runs with Crew's tools in a mode that approves on its own and that Crew
    did not ask for. The mark is cleared only once the move is answered, so a prompt
    cancelled before then leaves it for the next prompt.

    The same pre-prompt backstop applies when the ``approval_modes`` ceiling denies
    ``trust`` (:func:`_claude_auto_permitted`). A deny installed while a live process
    is in an approving mode marks the denial, records it and schedules that captured
    process to stop. If this restore is already waiting for ``session/set_mode`` when
    the stop lands, the read fails with the process-exit error; the next prompt starts
    a fresh session, whose start reads the denying ceiling and keeps ``default``. A
    ceiling still being installed here is waited out first
    (:func:`_claude_auto_verdict_is_final`). After that wait, and after each later
    audit or mode request, the deny mark and current-process stop task are re-read;
    once the hook fires, its stop remains the only owner.
    """
    client = self
    due = getattr(client, "_claude_mode_restore_due", False)
    approving = _claude_in_approving_mode(client)
    if await _finish_policy_stop_if_due(client):
        return
    final = await _claude_auto_verdict_is_final() if approving else True
    # A deny hook can run on the installing thread while the verdict wait runs.
    # Re-read both its mark and its task before auditing or sending any mode request;
    # that captured-process stop is the only owner once the hook has fired.
    if await _finish_policy_stop_if_due(client):
        return
    due = getattr(client, "_claude_mode_restore_due", False)
    approving = _claude_in_approving_mode(client)
    denied = approving and not _claude_auto_permitted()
    if not (due or denied):
        return
    if denied:
        if final:
            logger.warning(
                "the approval_modes policy does not permit 'trust', and this claude "
                "session is in %r, which approves calls without asking; it is moved to "
                "%r before its next prompt",
                _scrub_observed(getattr(client, "_claude_confirmed_mode", None) or ""),
                _CLAUDE_PINNED_MODE,
            )
            await _audit_claude_requested_auto_off_loop(client, "approval_mode_denied_by_policy")
            if await _finish_policy_stop_if_due(client):
                return
        else:
            logger.warning(
                "the approval_modes verdict for 'trust' is not final yet (a ceiling is "
                "still being installed), and this claude session is in %r, which "
                "approves calls without asking; it is moved to %r before its next prompt",
                _scrub_observed(getattr(client, "_claude_confirmed_mode", None) or ""),
                _CLAUDE_PINNED_MODE,
            )
        stopped = (
            "could not move a claude session to the asking permission mode after the "
            "approval_modes policy denied 'trust'; the session was stopped"
        )
    else:
        logger.warning(
            "a mode report moved this claude session to %r, which approves calls without "
            "asking Crew's gate and which Crew did not ask for or had withdrawn (a model "
            "without Auto mode support, or an unanswered request); it is moved back to %r "
            "before its next prompt",
            _scrub_observed(getattr(client, "_claude_confirmed_mode", None) or ""),
            _CLAUDE_PINNED_MODE,
        )
        stopped = (
            "could not move a claude session back to the asking permission mode after a "
            "mode report Crew did not ask for; the session was stopped"
        )
    await _set_claude_pinned_mode_or_stop(client, client._session_id, stopped)
    if await _finish_policy_stop_if_due(client):
        return
    # Cleared only once the move is answered: a prompt cancelled before that leaves
    # the session marked, so the next prompt moves it.
    client._claude_mode_restore_due = False
    client._claude_policy_deny_due = False
    client._claude_confirmed_mode = _CLAUDE_PINNED_MODE


async def _leave_claude_approving_mode_for_model_switch(self: ClaudeAcpClient) -> None:
    """Move a requested-``auto`` session to ``default`` before Crew switches its model.

    claude-agent-acp moves an ``auto`` session to ``acceptEdits`` when the switch lands
    on a model without Auto mode support, and a turn the agent starts on its own runs
    without passing through ``ensure_ready``, so the switch must reach the adapter
    while the session asks. A session confirmed in ``auto`` or in a mode that approves
    on its own (:data:`_CLAUDE_MODES_THAT_APPROVE_ON_THEIR_OWN`) is moved; a move that
    is refused or unanswered stops the session, and no switch is sent. Any other mode
    already asks and is left alone. The Claude-owned ``_push_model_config_option``
    calls this only for a session that follows its requested mode
    (:attr:`_follows_requested_mode`): one that requested nothing keeps the mode it
    inherited across a switch, as on main.

    The session stays in ``default`` for the rest of its life: a requested ``auto``
    is applied at session start only (:meth:`_apply_claude_requested_mode`), so no
    later mode report can follow a switch. Leaving ``auto`` logs one warning; the
    session is never in ``auto`` again, so it is logged once per session.
    """
    confirmed = getattr(self, "_claude_confirmed_mode", None)
    if confirmed != CC_PERMISSION_MODE_AUTO and confirmed not in (
        _CLAUDE_MODES_THAT_APPROVE_ON_THEIR_OWN
    ):
        return
    if confirmed == CC_PERMISSION_MODE_AUTO:
        logger.warning(
            "this claude session's model is being switched; the requested permission "
            "mode %r is not applied again after a model change, so the session runs in "
            "%r and asks before tools run until it is started again",
            CC_PERMISSION_MODE_AUTO,
            _CLAUDE_PINNED_MODE,
        )
    else:
        logger.info(
            "claude session in %r is moved to %r before its model is switched",
            _scrub_observed(confirmed or ""),
            _CLAUDE_PINNED_MODE,
        )
    await _set_claude_pinned_mode_or_stop(
        self,
        self._session_id,
        "could not move a claude session to the asking permission mode before "
        "switching its model; the session was stopped and the model was not switched",
    )
    self._claude_confirmed_mode = _CLAUDE_PINNED_MODE
    self._claude_mode_restore_due = False


async def _apply_claude_requested_mode(self: ClaudeAcpClient, resp: dict) -> None:
    """Apply a requested ``auto`` or report why the session keeps its current mode.

    The seed writes the request as ``permissions.defaultMode`` in the session's
    ``settings.local.json``, but the Claude Agent SDK drops an ``auto`` or
    ``bypassPermissions`` default mode from the project and local tiers
    (``filterEscalatingDefaultMode``, read by claude-agent-acp's
    ``SettingsManager``), so the seed does not apply the request; without an inherited
    or resumed mode the session starts in ``default``. Measured on
    claude-agent-acp 0.97.0 and 0.100.0. ``session/set_mode`` ``auto`` before the
    first prompt is what applies the opt-in.

    Acts only when this session requested :data:`CC_PERMISSION_MODE_AUTO`, is
    neither a Side Chat session nor a zero-tool spec (both must stay narrow),
    did not leave a foreign ``settings.local.json`` out
    (:meth:`_claude_session_excludes_local`), has a permission surface Crew
    governs, is permitted by the enterprise ``approval_modes`` ceiling, and lists
    ``auto`` among ``modes.availableModes``. A session already confirmed in ``auto``
    needs no write. Any other mode is retained: an asking or narrower mode such as
    ``plan`` gets one warning, while an inherited ``acceptEdits`` or
    ``bypassPermissions`` also gets a ``mode_change:claude_auto`` SEL record with
    outcome ``kept_inherited_mode`` and a warning that says Crew keeps that mode and
    that ``KIROCREW_CC_PIN_INHERITED_MODE=1`` moves a session carrying Crew's tools
    that starts or resumes in ``bypassPermissions`` to ``default`` first, so that
    ``auto`` can then be applied; the pin does not move ``acceptEdits``. A missing mode
    report is kept with a warning because Crew cannot safely replace an unconfirmed
    mode. An excluded
    session cannot keep ``auto`` (:meth:`_exclude_foreign_local_settings`), so
    it keeps the mode the pin left it in (``default`` from an approving start,
    as its ``claude_mode_pinned_notice`` tells the chat), and
    ``_maybe_audit_unasked_tool_call`` writes no classifier row for it.

    A session whose foreign ``settings.local.json`` could not be left out (a
    project file that sets ``hooks`` or ``sandbox`` or cannot be examined, or an
    adapter below the floor) loads that file, so Crew does not govern its
    permission surface and withholds its MCP tools. It is not moved to ``auto``
    either: one warning, the exclusion path's chat notice, no record, and it keeps
    ``default``.

    Runs after the startup model selection, so ``auto`` is asked for on the model
    the session will run: a switch to a model without Auto mode support would move
    an ``auto`` applied before it to ``acceptEdits``. The mode the session is in is
    ``_claude_confirmed_mode``, recorded after the pin
    (:meth:`_track_claude_starting_mode`) and kept current by the mode reports read
    since. When the pin moved the session (with ``KIROCREW_CC_PIN_INHERITED_MODE``
    on, a requested ``auto`` session that resumes in ``bypassPermissions``, or one
    that reports no mode), that is ``default``, and this moves it on to ``auto``
    when ``modes.availableModes`` offers it; a session that reported no ``modes``
    block offers nothing, so it keeps ``default`` with the warning below.
    Off the exclusion path the pin never moves an ``auto`` it finds on a session
    that requested it, so the two never undo each other.

    A ``set_mode`` refused with an error answer, or ``auto`` missing from the list,
    logs one warning and the session keeps ``default``, the narrower mode; the
    request did not land, so it is not stopped for this. Only a JSON-RPC error frame
    (an ``AcpError`` that carries its ``code``) is a refusal: a child that exits, a
    broken pipe or a shutdown while the request is sent or awaited propagates and
    ends the start, as it does during the startup model, so the caller retries or
    restarts the session. A ``set_mode`` left
    unanswered (``AcpTimeoutError``) may still be applied after the wait, so the
    session is moved back to ``default`` (:func:`_set_claude_pinned_mode_or_stop`),
    and a move that fails stops it. A cancel once the request is on the wire is the
    same unknown answer; the Claude-owned start drain that called this ends the
    process on it (``client_claude_mode_seams._drain_notifications``, and
    ``new_conversation``'s own teardown). On a model without Auto mode support the
    adapter answers ``{}`` but puts the session in ``acceptEdits``, reported by a
    mode report (``SessionModeManager.selectMode``). That mode approves an edit
    without asking Crew's gate, and it is Crew's own request that put the session
    there, so the warning names it and the session is moved back to ``default``
    the same way; a move that fails stops the session, as a failed pin does. A
    report buffered during the wait is read here; one that arrives after the answer
    is followed by :meth:`_note_claude_mode_report` and undone before the first
    prompt.

    The ceiling is asked for ``trust`` (:func:`_claude_auto_permitted`): ``auto``
    lets Claude's classifier approve a call without asking anyone, which is what a
    session Trust grants. A managed fleet whose ``approval_modes`` rule denies
    ``trust`` ("force interactive approval") therefore gets no ``set_mode`` ``auto``:
    one warning, an SEL ``mode_change:claude_auto`` record with outcome
    ``approval_mode_denied_by_policy``, and the session keeps ``default`` as a refused
    request leaves it, still watched so its mode reports and SEL rows stay current.
    An applied ``auto`` writes the same record with outcome ``enabled``. A ceiling
    still being installed is waited out first (:func:`_claude_auto_verdict_is_final`);
    one that outlasts the wait keeps ``default`` with a warning and no record. A deny
    installed later stops the captured process through the registered deny hook
    (:func:`_on_claude_trust_denied`), which then records any unsettled unasked calls,
    and the ceiling is read again before each prompt
    (:meth:`_restore_claude_asking_mode_if_due`). A ``set_mode`` ``auto`` left
    unanswered and undone is marked withdrawn (``_claude_auto_withdrawn``), so an
    ``auto`` the adapter applies after the undo is moved back too.

    Leaves ``_claude_confirmed_mode`` at ``auto`` once ``set_mode`` ``auto`` is
    answered and no report for this session buffered during the wait names another
    (:func:`_reported_mode`), and at ``default`` once a move back is made.
    ``_maybe_audit_unasked_tool_call`` reads it to attribute an unasked call.
    """
    if not _claude_requested_auto_has_callable_surface(self):
        return
    # The exclusion path runs a requested ``auto`` pinned to ``default`` and says
    # so in the chat; moving it back would make that notice false.
    if self._claude_session_excludes_local():
        return
    confirmed = getattr(self, "_claude_confirmed_mode", None)
    if confirmed == CC_PERMISSION_MODE_AUTO:
        return
    if confirmed != CC_PERMISSION_MODE_DEFAULT:
        if confirmed in _CLAUDE_MODES_THAT_APPROVE_ON_THEIR_OWN:
            logger.warning(
                "this claude session requested permission mode %r, but it started or "
                "resumed in mode %r, which approves calls without asking; Crew keeps "
                "that mode. KIROCREW_CC_PIN_INHERITED_MODE=1 moves a session that "
                "carries Crew's tools and starts or resumes in 'bypassPermissions' to "
                "'default' first, so that %r can then be applied",
                CC_PERMISSION_MODE_AUTO,
                _scrub_observed(confirmed),
                CC_PERMISSION_MODE_AUTO,
            )
            await _audit_claude_requested_auto_off_loop(self, "kept_inherited_mode")
        elif confirmed is None:
            logger.warning(
                "this claude session requested permission mode %r, but the adapter did "
                "not report its current mode; the request is not applied because Crew "
                "cannot replace an unconfirmed mode",
                CC_PERMISSION_MODE_AUTO,
            )
        else:
            logger.warning(
                "this claude session requested permission mode %r, but it resumed or "
                "started in mode %r; Crew keeps that asking or narrower mode rather "
                "than replacing it",
                CC_PERMISSION_MODE_AUTO,
                _scrub_observed(confirmed),
            )
        return
    if not self._permission_surface_governed:
        # The foreign file could not be left out, so it loads: its own
        # ``permissions.allow`` and Claude's classifier would both approve unasked.
        logger.warning(
            "this claude session requested permission mode %r, but its folder's "
            ".claude/settings.local.json is not Crew's and could not be left out, so "
            "Crew does not govern its permissions; it keeps %r. To get %r back, remove "
            "that file from this folder (or let Crew own it), or use the Kiro CLI harness.",
            CC_PERMISSION_MODE_AUTO,
            CC_PERMISSION_MODE_DEFAULT,
            CC_PERMISSION_MODE_AUTO,
        )
        self.claude_mode_pinned_notice = _CLAUDE_MODE_PINNED_NOTICE.format(
            mode=CC_PERMISSION_MODE_AUTO
        )
        return
    final = await _claude_auto_verdict_is_final()
    if not _claude_auto_permitted():
        if final:
            logger.warning(
                "this claude session requested permission mode %r, which approves calls "
                "without asking as a session Trust does, but the approval_modes policy "
                "does not permit 'trust'; the session keeps %r",
                CC_PERMISSION_MODE_AUTO,
                CC_PERMISSION_MODE_DEFAULT,
            )
            await _audit_claude_requested_auto_off_loop(self, "approval_mode_denied_by_policy")
        else:
            logger.warning(
                "this claude session requested permission mode %r, but the "
                "approval_modes verdict for 'trust' is not final yet (a ceiling is still "
                "being installed); the session keeps %r",
                CC_PERMISSION_MODE_AUTO,
                CC_PERMISSION_MODE_DEFAULT,
            )
        return
    modes = resp.get("modes")
    available = modes.get("availableModes") if isinstance(modes, dict) else None
    # Only a string id can name a mode; anything else the adapter sends is skipped.
    ids = (
        {m["id"] for m in available if isinstance(m, dict) and isinstance(m.get("id"), str)}
        if isinstance(available, list)
        else set()
    )
    if CC_PERMISSION_MODE_AUTO not in ids:
        logger.warning(
            "this claude session requested permission mode %r, but the adapter does "
            "not offer it; the session keeps %r",
            CC_PERMISSION_MODE_AUTO,
            CC_PERMISSION_MODE_DEFAULT,
        )
        return
    session_id = resp.get("sessionId") or self._session_id
    buffered = getattr(self, "_mcp_notifications", [])
    floor = len(buffered)
    try:
        await self._claude_set_mode(session_id, CC_PERMISSION_MODE_AUTO)
    except AcpTimeoutError:
        # No answer is not a refusal: the adapter may still apply ``auto``, or its
        # ``acceptEdits`` fallback, after the wait gave up, so the request is undone.
        logger.warning(
            "the claude adapter did not answer the requested permission mode %r in "
            "time; this session is moved back to %r before its first prompt",
            CC_PERMISSION_MODE_AUTO,
            _CLAUDE_PINNED_MODE,
        )
        await _set_claude_pinned_mode_or_stop(
            self,
            session_id,
            "could not move a claude session back to the asking permission mode "
            "after the adapter left a requested auto mode unanswered; the session "
            "was stopped",
        )
        self._claude_confirmed_mode = _CLAUDE_PINNED_MODE
        self._claude_mode_restore_due = False
        # The unanswered request may still land after the undo; it is not kept.
        self._claude_auto_withdrawn = True
        return
    except AcpError as exc:
        if exc.code is None:
            # Only a JSON-RPC error frame carries a code, and only an error frame is
            # the adapter refusing the mode. A child that exited, a broken pipe or a
            # shutdown is no answer: it ends the start, as it does during the
            # startup model (``_push_model_config_option``), so the caller retries
            # or restarts the session.
            raise
        logger.warning(
            "could not apply the requested permission mode %r to this claude session "
            "(%s); the session keeps %r",
            CC_PERMISSION_MODE_AUTO,
            type(exc).__name__,
            CC_PERMISSION_MODE_DEFAULT,
        )
        return
    landed = CC_PERMISSION_MODE_AUTO
    for msg in buffered[floor:]:
        reported = _reported_mode(msg, session_id)
        if reported is not None:
            landed = reported
    if landed != CC_PERMISSION_MODE_AUTO:
        logger.warning(
            "this claude session requested permission mode %r; the adapter put it in "
            "%r instead (its fallback for a model without Auto mode support), so it "
            "is moved back to %r before its first prompt",
            CC_PERMISSION_MODE_AUTO,
            _scrub_observed(landed),
            _CLAUDE_PINNED_MODE,
        )
        # Crew's own request moved the session; ``acceptEdits`` approves an edit
        # without asking Crew's gate, so it goes back to the ``default`` it was in.
        if landed != _CLAUDE_PINNED_MODE:
            await _set_claude_pinned_mode_or_stop(
                self,
                session_id,
                "could not move a claude session back to the asking permission mode "
                "after the adapter answered a requested auto mode with another mode; "
                "the session was stopped",
            )
        self._claude_confirmed_mode = _CLAUDE_PINNED_MODE
        self._claude_mode_restore_due = False
        return
    self._claude_confirmed_mode = landed
    self._claude_mode_restore_due = False
    await _audit_claude_requested_auto_off_loop(self, "enabled")
    logger.info(
        "claude session was in %r; applied the requested permission mode %r before "
        "its first prompt",
        CC_PERMISSION_MODE_DEFAULT,
        CC_PERMISSION_MODE_AUTO,
    )


def _unasked_call_attribution(self: ClaudeAcpClient, kind: str) -> dict[str, Any]:
    """What let an unasked call of a requested-``auto`` session run, for its SEL row.

    Read from the mode the session is confirmed in (``_claude_confirmed_mode``, which
    follows its mode reports, :meth:`_note_claude_mode_report`): ``decided_by``
    ``claude_classifier`` in a confirmed ``auto``; ``claude_permission_mode`` in a
    confirmed ``bypassPermissions``, which approves every call, and for a file edit
    (:data:`_CLAUDE_ACCEPT_EDITS_KINDS`) in a confirmed ``acceptEdits``; and
    ``claude_unattributed`` for any other call in ``acceptEdits``, which that mode does
    not approve and which one of Claude's own rules let run. A session confirmed in
    ``default``, ``plan``, ``dontAsk`` or no mode still gets its row, with
    ``decided_by`` ``claude_unattributed``: the asking rules or one of Claude's own
    rules let the call run, and neither the classifier nor a mode approved it.
    ``permission_mode`` names the mode in every row.
    """
    confirmed = getattr(self, "_claude_confirmed_mode", None)
    if confirmed == CC_PERMISSION_MODE_AUTO:
        reason, decided_by = "claude_auto_mode", "claude_classifier"
    elif confirmed in _CLAUDE_MODES_THAT_APPROVE_ON_THEIR_OWN:
        reason = decided_by = "claude_permission_mode"
    else:
        # A refused or unoffered ``set_mode``, an undone one, a ``plan`` start, or a
        # mode nothing confirmed: the call still gets its row, but the asking rules or
        # Claude's own rules let it run, so it names no approver.
        reason, decided_by = "claude_unasked_call", "claude_unattributed"
    if confirmed == "acceptEdits" and kind not in _CLAUDE_ACCEPT_EDITS_KINDS:
        # ``acceptEdits`` approves file edits only; this call ran unasked under one of
        # Claude's own rules (a pre-approval), which Crew cannot name.
        reason, decided_by = "claude_unasked_call", "claude_unattributed"
    # The mode is the adapter's own string, scrubbed as every log line of it is.
    return {
        "reason": reason,
        "decided_by": decided_by,
        "permission_mode": _scrub_observed(confirmed),
    }


def _register_policy_deny_hook() -> None:
    """Register Claude's live requested-mode stop callback once by owner."""
    from kiro_crew.approval_mode_policy import register_deny_hook

    register_deny_hook(_CLAUDE_POLICY_HOOK_OWNER, _on_claude_trust_denied)


_register_policy_deny_hook()
