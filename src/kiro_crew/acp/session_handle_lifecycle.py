"""The session outside its turn loop.

Awaited control requests and slash commands, cancel, death attribution, destroy and
transcript cleanup.

Each function here whose first parameter is ``self`` is an ``AcpSessionHandle`` method: the
class binds it by name, so it reads and writes the session's own state on the instance,
and a patch of the method on the class or on an instance reaches every caller.

Instance fields written: ``_abandoned_steering``, ``_awaited_responses``,
``_cancel_grace_secs``, ``_cancel_ts``, ``_cancelled``, ``_ingress_seq``,
``_session_key``, ``_stale_probe``, ``_steering_answers``.

Instance fields read: ``_pending_elicitations``, ``_prompt_or_tool_seen``, ``_queue``,
``_runtime``, ``_session_id``, ``_steering_settled``, ``keep_transcript``, ``memory_mode``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from kiro_crew.acp.client import (
    AcpError,
    AcpProcessDied,
    AcpTimeoutError,
    _jsonrpc_error_code,
    _raise_acp_error,
    parse_slash_command,
    registration_rate_limited_error,
    registration_throttle_line,
)
from kiro_crew.acp.types import METHOD_CANCEL, METHOD_COMMANDS_EXECUTE, JsonRpcMessage
from kiro_crew.config.paths import kiro_sessions_dir
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

if TYPE_CHECKING:
    from kiro_crew.acp.session_handle import AcpSessionHandle

#: The session handle's logger: this code logged under that name before the split.
logger = logging.getLogger("kiro_crew.acp.session_handle")


# Unresponsive-cancel budget: after cancel() is sent, if kiro-cli does not
# ack (via a cancelled stopReason on the prompt response) within this window,
# the dispatch loop unblocks the caller with a terminal EVENT_COMPLETE. The
# shared runtime is NOT killed (co-tenant sessions keep running) — mirrors
# AcpClient's _CANCEL_GRACE_SECS floor without the process-kill (which is
# impossible on a multiplexed runtime).
_CANCEL_GRACE_SECS = 10.0


def _died(self: AcpSessionHandle, base: str) -> AcpProcessDied:
    """Build an AcpProcessDied carrying the runtime's death attribution.

    The poison sentinel tells a waiting turn only THAT the runtime died;
    the runtime's ``death_summary()`` (reason + returncode + stderr tail,
    composed at ``_mark_dead`` time) says who/why. Field experience: three
    unattributed mid-turn deaths in five days were undiagnosable from the
    bare message alone. ``getattr``-guarded so a minimal runtime double
    without ``death_summary`` degrades to the bare message.

    A death whose retained stderr shows a throttled dynamic registration
    returns the typed transient subclass instead of the generic death — but
    only while this session has seen NO text chunk and NO tool call
    (``prompt_or_tool_seen``). A stale throttle line surviving in the ring
    past real work must not hand the retry ladders a transient verdict for
    a session whose replay could repeat side effects. The tail is read as
    LINES (``redacted_stderr_tail``), because the summary folds them behind
    a prefix that a per-line signature cannot match — the same reason the
    sandbox corroboration reads it. The typed message keeps one retained
    cause instead of the tail's repeated copies.

    A stdin-stall death is never re-attributed to a throttle: the host
    knows why the runtime died, and the transient verdict would license
    replaying a prompt the live child may still read. It carries
    ``ambiguous_delivery`` when this session's prompt was outstanding
    (``prompt_outstanding_on_stall``).
    """
    stalled = getattr(self._runtime, "stdin_stall_death", False) is True
    if not stalled and not self._prompt_or_tool_seen:
        tail = getattr(self._runtime, "redacted_stderr_tail", lambda: "")()
        cause = registration_throttle_line(tail) if tail else None
        if cause is not None:
            return registration_rate_limited_error(base, cause)
    summary = getattr(self._runtime, "death_summary", lambda: None)()
    return AcpProcessDied(
        f"{base} — {summary}" if summary else base,
        ambiguous_delivery=self.prompt_outstanding_on_stall,
    )


# ── Cancel ──


async def cancel(
    self: AcpSessionHandle, grace_secs: float = 0.0, _stale_probe: bool = False
) -> None:
    """Send session/cancel notification.

    Records the cancel time + grace budget so the dispatch loop can unblock
    the caller if kiro-cli never acks the cancel (no cancelled stopReason on
    the prompt response). On a shared runtime we cannot force-kill the
    process (co-tenant sessions would die), so recovery is a synthesized
    terminal event rather than a hard kill.

    ``_stale_probe`` marks a watchdog probe cancel (internal). A genuine
    (non-probe) cancel SUPERSEDES any pending probe: the flag is cleared so
    the eventual ack is attributed to the user, not reclassified to
    auto-recovery.
    """
    self._stale_probe = _stale_probe
    self._cancelled = True
    self._cancel_hook_tasks()
    self._cancel_ts = time.monotonic()
    self._cancel_grace_secs = max(_CANCEL_GRACE_SECS, grace_secs)
    # cancel is a JSON-RPC notification (no id, no response) — use
    # send_notification so we don't register an unanswerable routing entry.
    await self._runtime.send_notification(
        METHOD_CANCEL,
        {"sessionId": self._session_id},
    )
    # An MCP elicitation still waiting on a person is answered ``cancel`` on the
    # same path: the engine holds its tool call open until the ask is answered,
    # and a session/cancel alone would leave that request unanswered on the
    # wire. Same order as ``AcpClient.cancel_session``: the notification, then
    # every open server->client request it leaves behind.
    await self.cancel_pending_elicitations()


# ── Commands & Config ──


async def send_command(
    self: AcpSessionHandle, command: str, args: dict[str, Any] | None = None
) -> str:
    """Execute a kiro slash command (e.g. '/compact', '/effort').

    Returns the response text (if any). Mirrors AcpClient.send_command.
    """
    if args:
        cmd_name = command.strip().split(None, 1)[0].lstrip("/")
        payload: dict[str, Any] = {
            "sessionId": self._session_id,
            "command": {"command": cmd_name, "args": args},
        }
    else:
        payload = {"sessionId": self._session_id, "command": command}
    req_id = await self._send_awaited(METHOD_COMMANDS_EXECUTE, payload)
    try:
        msg = await self._wait_for_response(req_id, timeout=60.0)
        result = msg.result or {}
        raw = (
            result.get("text", "") or result.get("message", "") if isinstance(result, dict) else ""
        )
        # Two-pass redaction (URLs + credentials) before returning — command
        # output is backend-echoed text that reaches the dashboard. Explicit
        # here (rather than redact_text) so the security control is auditable
        # at the external surface, matching AcpClient.send_command exactly.
        text = str(raw)
        text, _ = redact_exfiltration_urls(text)
        text, _ = redact_credentials(text)
        return text
    except AcpTimeoutError:
        return ""


async def command_result(self: AcpSessionHandle, command: str) -> dict[str, Any]:
    """Execute a native kiro command and return its structured result.

    Mirrors :meth:`AcpClient.command_result`: for internal callers whose
    contract is the command's ``data`` object (the ``/mcp`` and ``/tools``
    inventories). The result is backend output, so a caller must reduce it
    to bounded, typed values before anything reaches an external surface.
    """
    cmd_name, cmd_args = parse_slash_command(command)
    req_id = await self._send_awaited(
        METHOD_COMMANDS_EXECUTE,
        {"sessionId": self._session_id, "command": {"command": cmd_name, "args": cmd_args}},
    )
    msg = await self._wait_for_response(req_id, timeout=60.0)
    result = msg.result
    return result if isinstance(result, dict) else {}


async def _send_awaited(self: AcpSessionHandle, method: str, params: dict[str, Any]) -> int:
    """Send a request whose response a following _wait_for_response claims.

    The id joins _awaited_responses before the write: a response that lands
    on the queue while the write drains would otherwise read as owed to
    nobody and be dropped. _wait_for_response's finally removes it; a failed
    send removes it here.
    """
    reserved: list[int] = []

    def _reserve(req_id: int) -> None:
        reserved.append(req_id)
        self._awaited_responses.add(req_id)

    try:
        return await self._runtime.send_request(method, params, on_reserved=_reserve)
    except BaseException:
        for req_id in reserved:
            self._awaited_responses.discard(req_id)
        raise


def bind_session_key(self: AcpSessionHandle, session_key: str) -> None:
    """Rebind the owning Kiro Crew session on a warm-pool claim.

    The listed-hook record is keyed by that session, so ids listed before the
    claim are unreachable from the new key, and its next list answer replaces
    them.
    """
    self._session_key = session_key


# NOTE: resume is done via AcpRuntime.load_session() (issues session/load
# DIRECTLY under the transcript's own sid). The old per-handle load() is
# intentionally removed: it issued session/load with sessionId=this handle's
# sid — which on the resume path is a FRESH session/new sid, not the
# transcript's — so kiro-cli replayed the old transcript on top of a freshly
# primed session and died / refused. See AcpRuntime.load_session for the fix.


async def destroy(self: AcpSessionHandle) -> None:
    """Terminate this session on kiro-cli, delete its transcript, unregister.

    Sends ``_kiro.dev/session/terminate`` (via the runtime) so the shared
    kiro-cli process frees this session's transcript/context and reaps its
    MCP children — NOT just a local queue unregister. Without the terminate,
    a finished session's state stays resident in the multiplexed process
    forever, so RSS climbs with cumulative sessions (the background-runtime
    unbounded-growth bug). ``terminate_session`` is best-effort + bounded and
    ALWAYS unregisters the queue, so teardown neither hangs nor fails on a
    dead or slow runtime -- but see the `finally` below for the one
    exception it cannot swallow.

    Each session on a shared runtime (a ``_bg`` op or a session-sharing
    subagent) is a distinct ``session/new`` with its own persisted
    ``~/.kiro/sessions/cli/{sid}.json``(+``.jsonl``). The shared runtime is
    not killed on teardown, so we also delete the transcript here — otherwise
    these files would accumulate for the gateway lifetime (titles/
    suggestions/folders/nav run on nearly every chat). Only ephemeral
    sessions call destroy(): main-chat sessions are torn down via
    ``owns_runtime=True`` → ``runtime.kill()`` and intentionally keep their
    transcript for ``session/load`` resume, so cleaning up here is safe.

    Exception: ``keep_transcript=True`` (set by SubagentManager before
    teardown) skips the transcript deletion — subagent transcripts are the
    resume material for ``spawn_continue`` and are lifecycle-managed by the
    tombstone pruner / conversation TTL sweep instead. ``terminate_session``
    still runs unconditionally: it is the RSS reclaim on the multiplexed
    process; only the unlink is deferred.
    """
    # The unlink runs in a `finally`, for the same reason
    # `terminate_session` unregisters the queue in one: that method swallows
    # `Exception`, but `asyncio.CancelledError` is a `BaseException` and
    # propagates straight out of the await. Cancellation is exactly when
    # teardown runs -- gateway shutdown, an abandoned turn -- so the
    # sequential form skipped the cleanup on the path that produces the most
    # of these files, and every survivor is permanent: nothing else deletes
    # an ephemeral session's transcript.
    self._cancel_hook_tasks()
    # Answers this session will never read again must not stay registered on a
    # runtime that outlives it. ``getattr``: a handle can be torn down before
    # (or without) ``__init__`` having run, and teardown must not raise then.
    held = [a for a, _gen, _text in getattr(self, "_steering_answers", ())]
    held += list(getattr(self, "_abandoned_steering", ()))
    if held:
        for _answer in held:
            self._runtime.forget_request(_answer)
        self._steering_answers = []
        self._abandoned_steering = []
    for _flag in list(getattr(self, "_steering_settled", {}).values()):
        if not _flag.done():
            _flag.set_result(False)
    # An elicitation nobody answered is cancelled on the wire before the
    # session goes: the engine is otherwise left holding its tool call open.
    if getattr(self, "_pending_elicitations", None):
        await self.cancel_pending_elicitations()
    try:
        await self._runtime.terminate_session(self._session_id)
    finally:
        if self.memory_mode != "persistent" or not getattr(self, "keep_transcript", False):
            self._cleanup_transcript()


def _cleanup_transcript(self: AcpSessionHandle) -> None:
    """Best-effort delete of this session's kiro-cli transcript files.

    A NO-OP on the KAS backend: this unlinks from kiro-cli's sessions dir and
    KAS keeps its own store, so nothing here matches. The ``keep_transcript``
    guard therefore protects nothing on KAS — that backend's session record is
    already gone, removed by the same verb that freed the session.
    """
    self.cleanup_transcript_files(self._session_id)


def cleanup_transcript_files(sid: str) -> None:
    """Remove only the native transcript belonging to a completed session."""
    if not sid:
        return
    sessions_dir = kiro_sessions_dir().resolve()
    for suffix in (".json", ".jsonl"):
        target = (sessions_dir / f"{sid}{suffix}").resolve()
        # Guard against a crafted sessionId escaping the sessions dir.
        if target.parent != sessions_dir:
            logger.error("destroy: path traversal blocked for %s", target)
            return
        try:
            target.unlink(missing_ok=True)
        except OSError:
            logger.warning("destroy: failed to delete transcript %s", target, exc_info=True)


async def _wait_for_response(
    self: AcpSessionHandle, req_id: int, timeout: float = 30.0
) -> JsonRpcMessage:
    """Drain queue until we get the response for req_id.

    Non-matching frames are NOT dropped: a command/config call
    (send_command / compact / set_config_option) can run concurrently with a
    live prompt turn, and both read this shared queue. Silently discarding
    non-matching frames here would steal the in-flight turn's text/tool
    frames (wedging its dispatch loop). Instead we buffer them and re-inject
    them in the finally so the turn's consumer still sees them (mirrors
    AcpClient.wait_for_compaction).
    """
    deadline = time.monotonic() + timeout
    buffered: list[JsonRpcMessage] = []
    self._awaited_responses.add(req_id)
    try:
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                msg = await asyncio.wait_for(self._queue.get(), timeout=min(remaining, 5.0))
            except asyncio.TimeoutError:
                continue
            if msg is None:
                # Runtime died: re-poison for the live turn / next consumer.
                self._queue.put_nowait(None)
                raise self._died("Runtime process died while waiting for response")
            if msg.is_response_for(req_id):
                if msg.error:
                    # Delegate to the shared raise helper so this path gets
                    # the SAME treatment as AcpClient: actionable prose from
                    # _format_acp_error (model-unavailable / throttle / auth /
                    # 5xx), credential+URL redaction, the transient= verdict
                    # for the chat_runner / llm_helpers retry ladder, and the
                    # AcpPromptBusy subclass for a concurrent in-flight
                    # prompt. Raising a bare f"ACP error: {msg.error}" here
                    # put the raw JSON-RPC dict in front of the user.
                    # The advertised ids let the shared entitlement
                    # discriminator tell "your plan lacks this model"
                    # (terminal) from a capacity blip (retryable).
                    #
                    # The raw JSON-RPC code is re-attached on the way out:
                    # the shared helper decides everything from the frame's
                    # prose, and a bare ``-32602 Invalid params`` carries no
                    # prose to decide from. A config-option write is the one
                    # request whose shape is fixed and whose value is the
                    # only variable, so for it that code IS a value refusal
                    # rather than a malformed request. Nothing else branches
                    # on ``code``, so setting it cannot change any other
                    # path's outcome.
                    try:
                        _raise_acp_error(
                            msg.error,
                            self._advertised_model_ids(),
                            backend=self._runtime.acp_backend,
                        )
                    except AcpError as _exc:
                        if getattr(_exc, "code", None) is None:
                            _exc.code = _jsonrpc_error_code(msg.error)
                        raise
                return msg
            # Not our response — buffer (do not drop) for re-injection,
            # and advance the ingress sequence for EVERY buffered frame:
            # the TOCTOU guard in _dispatch_events snapshots it before the
            # oracle await and compares after, so frames consumed by this
            # concurrent waiter are still detected regardless of queue
            # depth at the time of the check. Responses count too — a
            # buffered response can be the prompt turn's own terminal
            # frame, and skipping it let the watchdog cancel a turn whose
            # completion was sitting in this buffer. A spurious bump only
            # defers one watchdog tick, so over-counting is fail-safe.
            self._ingress_seq += 1
            buffered.append(msg)
        raise AcpTimeoutError(f"Timeout waiting for response to request {req_id}")
    finally:
        self._awaited_responses.discard(req_id)
        for _m in buffered:
            self._queue.put_nowait(_m)
