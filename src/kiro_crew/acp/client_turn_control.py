"""Control an ``AcpClient`` turn from outside it.

Steers (kiro ``_session/steer``, claude ``_session/steering``, goose's steer request,
opencode's second ``session/prompt``),
cancel, slash commands, and the turn-state queries callers poll.

Each function here whose first parameter is ``self`` is an ``AcpClient`` method: the
class binds it by name, so it reads and writes the session's own state on the instance,
and a patch of the method on the class or on an instance reaches every caller.

Instance fields written: ``_active_run_id``, ``_cancel_grace_secs``, ``_cancel_ts``,
``_cancelled``, ``_last_activity``, ``_orphan_steers``, ``_orphan_steers_process``,
``_permission_options``, ``_steer_answers``, ``_steer_turn``, ``_steering_waiter_map``,
``_steers_awaiting_echo``, ``_turn_permission_denied``.

Instance fields read: ``_active_tool_calls``, ``_agent``, ``_agent_version``, ``_background_launches``,
``_last_stop_reason``, ``_permission_gate_events``, ``_process``, ``_session_id``,
``_session_key``, ``_stdin_stalled``, ``_turn_done``, ``_turn_lock``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import aclosing
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, AsyncGenerator, AsyncIterator, Collection

from kiro_crew import sel as sel_module
from kiro_crew.acp import pi_steer_turn, transport_framing
from kiro_crew.acp.client_claude_session import (
    CLAUDE_ACP_STEERING_MIN_VERSION,
    _adapter_version_at_least,
)
from kiro_crew.acp.client_timeouts import (
    _CANCEL_GRACE_SECS,
    _CONCURRENT_STEER_CANCEL_SECS,
    _CONCURRENT_STEER_GRACE_SECS,
    _MAX_CONCURRENT_STEER_CHARS,
    _MAX_CONCURRENT_STEERS,
    _READ_TIMEOUT,
    _SEL_AUDIT_TIMEOUT_SECONDS,
    _STEERING_WAIT_SLICE_SECS,
    _effective_prompt_timeout_async,
)
from kiro_crew.acp.transport_errors import AcpError, AcpProcessDied, AcpTimeoutError
from kiro_crew.acp.transport_framing import write_notification_best_effort
from kiro_crew.acp.types import (
    ACP_BACKEND_GOOSE,
    ACP_BACKEND_PI,
    ACP_BACKENDS_STEER_CONCURRENT_PROMPT,
    ACP_BACKENDS_STEERING_REQUEST,
    EVENT_COMPLETE,
    EVENT_STEER_CONSUMED,
    MAX_STEERING_ANSWERS,
    MAX_STEERING_TEXT_CHARS,
    METHOD_CANCEL,
    METHOD_COMMANDS_EXECUTE,
    METHOD_PROMPT,
    METHOD_REQUEST_PERMISSION,
    METHOD_SESSION_STEERING,
    METHOD_SESSION_UPDATE,
    OUTCOME_CANCELLED,
    STEERING_ANSWER_WAIT_SECS,
    STEERING_INJECTED,
    STOP_REASON_COMPACTION_FAILED,
    STOP_REASON_END_TURN,
    UPDATE_SESSION_INFO,
    UPDATE_USER_MESSAGE_CHUNK,
    AcpEvent,
    JsonRpcMessage,
    steering_outcome,
)
from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_LAUNCH,
    ACP_BACKEND_NODE_ADAPTER_PACKAGES,
)
from kiro_crew.executors import subprocess_executor
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

if TYPE_CHECKING:
    from kiro_crew.acp.client import AcpClient

#: The client's own logger: this code logged under that name before the split.
logger = logging.getLogger("kiro_crew.acp.client")


@dataclass
class _ConcurrentSteerTurn:
    """One prompt turn's concurrent-prompt steers, from first write to settle.

    ``steers`` maps each request id to its wrapped text, registered before the
    write; ``answers`` holds each answer read, with whether another loop's output
    came first; ``accepted`` is the ids whose ``steer()`` returned True. Once
    ``closed``, every id not in ``answers`` has been made an orphan.
    """

    steers: dict[int, str] = field(default_factory=dict)
    answers: dict[int, tuple[JsonRpcMessage, bool]] = field(default_factory=dict)
    accepted: set[int] = field(default_factory=set)
    closed: bool = False


# Named by the path callers import it from, as it was before the split.
_ConcurrentSteerTurn.__module__ = "kiro_crew.acp.client"


def steering_advertised(init_resp: object) -> bool:
    """Whether an ``initialize`` result advertises the ``_session/steering`` request.

    The extension is advertised at the TOP level of the result, as
    ``_meta.steering.supported``, a sibling of ``agentCapabilities``. Only a
    literal ``true`` counts.
    """
    meta = init_resp.get("_meta") if isinstance(init_resp, dict) else None
    steering = meta.get("steering") if isinstance(meta, dict) else None
    return isinstance(steering, dict) and steering.get("supported") is True


#: The oldest goose that serves ``_goose/unstable/session/steer`` as Crew sends it:
#: 1.38.0 ("Steering messages with ACP", block/goose#9560) added the request with
#: ``expectedRunId``, the ``{runId, messageId}`` answer, ``activeRunId`` on
#: ``session_info_update`` and the ``_meta.goose.steer`` echo. 1.37.0 has none of
#: them, so its answer is ``-32601`` and every steer queues.
GOOSE_STEER_MIN_VERSION = (1, 38, 0)

#: The release each adapter needs before the card's ``mid_turn_steer`` line is true,
#: with the command that updates it. Below it the session still runs and a steer
#: queues, so a new chat gets one notice naming the upgrade instead of a refusal.
_STEER_MIN_VERSIONS: dict[str, tuple[tuple[int, int, int], str]] = {
    ACP_BACKEND_CLAUDE: (
        CLAUDE_ACP_STEERING_MIN_VERSION,
        f"npm i -g {ACP_BACKEND_NODE_ADAPTER_PACKAGES[ACP_BACKEND_CLAUDE]}",
    ),
    ACP_BACKEND_GOOSE: (
        GOOSE_STEER_MIN_VERSION,
        ACP_BACKEND_LAUNCH[ACP_BACKEND_GOOSE].install_command,
    ),
}


def steer_version_issue(backend: str, agent_version: str) -> str:
    """The upgrade notice for *agent_version* of *backend*, or ``""`` when it is not
    known to be too old to steer. An absent or unreadable version gets none, as for
    pi. Compared by :func:`_adapter_version_at_least`, the parser the steer gate
    itself reads, so the notice cannot disagree with whether the session steers."""
    floor_and_fix = _STEER_MIN_VERSIONS.get(backend)
    # Readable means it parses at all: every readable version is at least 0.0.0.
    if floor_and_fix is None or not _adapter_version_at_least(agent_version, (0, 0, 0)):
        return ""
    floor, fix = floor_and_fix
    if _adapter_version_at_least(agent_version, floor):
        return ""
    minimum = ".".join(str(part) for part in floor)
    return (
        f"{backend} {agent_version} is older than {minimum}, so a message you send "
        f"while it works waits until it finishes. "
        f"Update it: run '{fix}', then start a new chat."
    )


def format_command_result(result: dict) -> str:
    """Extract displayable text from a commands/execute response.

    Module-level (not a method) because both native slash-command paths need
    it: AcpClient.stream_command (direct-spawn sessions) and
    AcpSessionHandle.stream_command (shared-runtime sessions).

    The output is two-pass redacted (URLs + credentials) HERE, in the shared
    helper, so every present and future caller inherits the security control
    (command output is backend-echoed text that reaches the dashboard) instead
    of each call site re-discovering it. Call-site re-redaction stays
    harmless — both passes are idempotent.
    """
    data = result.get("data")
    message = result.get("message", "")
    text = ""
    # Structured data — format as readable JSON block
    if isinstance(data, dict) and data:
        # Filter out agent/model metadata (handled separately)
        display = {k: v for k, v in data.items() if k not in ("agent", "model")}
        if display:
            block = json.dumps(display, indent=2)
            text = f"{message}\n```json\n{block}\n```" if message else f"```json\n{block}\n```"
    if not text:
        text = message or ""
    if text:
        text, _ = redact_exfiltration_urls(text)
        text, _ = redact_credentials(text)
    return text


def parse_slash_command(command: str) -> tuple[str, dict]:
    """Parse ``/foo bar baz`` into TuiCommand ``(name, args)``.

    Shared by AcpClient.stream_command and AcpSessionHandle.stream_command —
    both send the OBJECT form (``{command, args}``) because kiro-cli 2.14.0
    returns no response on the string form of ``_kiro.dev/commands/execute``.
    """
    parts = command.strip().split(None, 1)
    name = parts[0].lstrip("/") if parts else command.lstrip("/")
    value = parts[1] if len(parts) > 1 else None
    args: dict = {"value": value} if value else {}
    return name, args


# A member's own mid-turn steer request on THIS client, spoken in place of the
# ``_session/steering`` request claude takes
# (``AcpClient._steer_via_steering_request``). goose's
# ``_goose/unstable/session/steer`` takes ``{sessionId, expectedRunId, prompt}``,
# where the run id is the one goose announced in
# ``session_info_update._meta.goose.activeRunId`` when the prompt started; it
# answers ``{runId, messageId}`` once the text is queued for the run, and
# ``-32602`` when no run is active or the id names another run. A member with
# neither is not offered a steer on this client. The wait for the answer and the
# bounds on steers held for one turn are the shared ``STEERING_ANSWER_WAIT_SECS``,
# ``MAX_STEERING_TEXT_CHARS`` and ``MAX_STEERING_ANSWERS``.
_GOOSE_SESSION_STEER = "_goose/unstable/session/steer"
_CLIENT_STEERING_REQUEST_METHODS: dict[str, str] = {ACP_BACKEND_GOOSE: _GOOSE_SESSION_STEER}


async def command_result(
    self: AcpClient, command: str, args: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Execute a native kiro command and return its structured result.

    Internal callers use this when the command's ``data`` object is the
    contract (for example ``/mcp`` and ``/tools`` inventories). The result
    is backend output and must be reduced to a bounded, typed payload before
    it reaches an external surface. The TuiCommand object form is required:
    current kiro-cli can exit without a response for the legacy string form.
    """
    await self.ensure_ready()
    cmd_name, cmd_args = parse_slash_command(command)
    payload: dict[str, Any] = {
        "sessionId": self._session_id,
        "command": {"command": cmd_name, "args": args if args is not None else cmd_args},
    }
    req_id = await self._send_request(METHOD_COMMANDS_EXECUTE, payload)
    result = await self._wait_for_response(req_id, timeout=60.0)
    return result if isinstance(result, dict) else {}


async def send_command(self: AcpClient, command: str, args: dict | None = None) -> str:
    """Execute a kiro slash command (e.g. '/compact', '/usage', '/effort').

    Returns the response text (if any).  For streaming output use
    :meth:`stream_command` instead.

    When *args* is provided (e.g. ``{"level": "high"}`` for ``/effort``),
    the TuiCommand object form ``{command, args}`` is used so kiro-cli
    receives the arguments — the plain-string form silently drops them.
    Otherwise the plain-string form is kept for backward compat with
    older kiro-cli.
    """
    await self.ensure_ready()
    if args:
        cmd_name = command.strip().split(None, 1)[0].lstrip("/")
        payload: dict = {
            "sessionId": self._session_id,
            "command": {"command": cmd_name, "args": args},
        }
    else:
        payload = {"sessionId": self._session_id, "command": command}
    req_id = await self._send_request(METHOD_COMMANDS_EXECUTE, payload)
    try:
        result = await self._wait_for_response(req_id, timeout=60.0)
        raw = result.get("text", "") or result.get("message", "")
        if raw:
            # Two-pass redaction (URLs + credentials) to match the shared
            # AcpSessionHandle.send_command path: a URL-only pass leaves
            # tokens/keys in slash-command output.
            raw, _ = redact_exfiltration_urls(raw)
            raw, _ = redact_credentials(raw)
        return raw
    except AcpTimeoutError:
        logger.debug("Command '%s' response timed out (may still be running)", command)
        return ""


async def stream_command(
    self: AcpClient, command: str, timeout: float | None = None
) -> AsyncIterator[AcpEvent]:
    """Execute a slash command and yield streaming AcpEvents.

    Uses ``_kiro.dev/commands/execute`` with the TuiCommand object
    format (``{command, args}``) so kiro-cli executes the command
    natively and streams full output via ``session/update``.
    """
    timeout = await _effective_prompt_timeout_async(timeout)
    self._cancelled = False
    await self.ensure_ready()

    cmd_name, cmd_args = parse_slash_command(command)
    req_id = await self._send_request(
        METHOD_COMMANDS_EXECUTE,
        {
            "sessionId": self._session_id,
            "command": {"command": cmd_name, "args": cmd_args},
        },
    )
    async for event in self._dispatch_events(req_id, timeout, extract_agent_from_result=True):
        yield event


async def cancel_session(self: AcpClient, grace_secs: float = 0.0) -> None:
    """Cancel the current in-flight operation via ACP session/cancel.

    Per ACP spec, session/cancel is a JSON-RPC notification (no id).
    The ack arrives as stopReason:"cancelled" on the session/prompt
    response, not as a response to this message.

    ``grace_secs`` is the caller's cooperative-cancel ack budget. The read
    loop aborts the turn as "unresponsive" once this elapses, so it must
    not be shorter than the budget the caller will wait on; we raise the
    per-cancel grace to ``max(floor, grace_secs)`` so a configured budget
    above the 10s floor genuinely extends the window instead of the loop
    bailing early and forcing a session-losing hard kill.
    """
    if not self._session_id:
        logger.debug("cancel_session: no session_id, skip")
        return
    self._cancelled = True
    self._cancel_ts = time.monotonic()
    self._cancel_grace_secs = max(_CANCEL_GRACE_SECS, grace_secs)
    logger.debug(
        "cancel_session: sending session/cancel notification (sid=%s, turn_done=%s, proc_alive=%s)",
        self._session_id,
        self._turn_done.is_set(),
        self._is_process_alive(),
    )
    if not self._process or not self._process.stdin:
        logger.debug("cancel_session: process not running")
        return
    try:
        notification = {
            "jsonrpc": "2.0",
            "method": METHOD_CANCEL,
            "params": {"sessionId": self._session_id},
        }
        data = transport_framing.encode_frame(notification)
        # Best effort, never swallowed by the write lock: a cancel is the
        # one signal that can end a wedged turn, so it is appended unlocked
        # if the lock does not come within the no-progress bound.
        outcome = await write_notification_best_effort(
            self._process.stdin,
            self._stdin_write_lock(),
            data,
            bound_secs=transport_framing._RESPONSE_WRITE_BOUND_SECS,
        )
        # Only a drained frame is evidence the backend moved: an unlocked
        # append or a stall must not refresh the activity clock the
        # wedged-turn probes read.
        if outcome == "drained":
            self._last_activity = time.monotonic()
        logger.debug("cancel_session: wrote session/cancel notification (%s)", outcome)
    except Exception:
        logger.debug("Cancel notification failed", exc_info=True)
    # ACP: after session/cancel the client MUST answer every permission request
    # still open with the ``cancelled`` outcome. A harness that waits for that
    # answer before it acks the cancel (goose, pi) otherwise holds the turn open
    # until the caller's ack budget runs out and the process is hard-killed --
    # the path every Stop takes on a surface that did not reject the open
    # approval first. ``_permission_options`` holds the requests not yet
    # answered: ``reject_tool`` and ``approve_tool``'s auto-resolve path pop
    # their entry, and a new turn clears the map. An ``approve_tool`` call with
    # an explicit ``option_id`` leaves its entry behind; no caller passes one to
    # this client.
    # The same holds for an MCP elicitation still waiting on a person: the
    # engine keeps its tool call open until the ask is answered.
    await self.cancel_pending_elicitations()
    open_requests = list(self._permission_options)
    self._permission_options.clear()
    if not open_requests:
        return
    for request_id in open_requests:
        # A cancelled gate dialog is not an approval, so pi's tripwire treats
        # the call exactly as it treats a rejected one.
        self._note_pi_gate_denied(request_id)

    await self._audit_cancelled_approvals(open_requests)
    for request_id in open_requests:
        try:
            await self._send_response(request_id, {"outcome": {"outcome": OUTCOME_CANCELLED}})
        except Exception:
            logger.debug(
                "cancel_session: answering an open permission request failed",
                exc_info=True,
            )
            break


async def _audit_cancelled_approvals(
    self: AcpClient, request_ids: list[str | int], *, cause: str = ""
) -> None:
    """Write one SEL record per approval Crew answers ``cancelled``.

    Each cancelled approval is a denial Crew made, so it gets its own record
    before the answers go out. ONE off-loop hop for all of them, with the
    accessor inside it (an unwarmed ``sel()`` initialises on the calling
    thread), bounded the way ``_maybe_audit_tool_call`` bounds its write, so a
    stuck SEL backend costs the caller at most one audit timeout. *cause*, when
    set, is recorded as the metadata ``cause``.
    """
    metadata = {"backend": self.backend, **({"cause": cause} if cause else {})}

    def _audit_cancelled() -> None:
        log = sel_module.sel()
        for request_id in request_ids:
            log.log_tool_invocation(
                session_key=self._session_key or "",
                agent=self._agent,
                source="acp",
                tool_name="approval_cancel",
                tool_kind="permission",
                outcome="rejected_on_cancel",
                request_id=request_id,
                metadata=metadata,
            )

    try:
        await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(subprocess_executor(), _audit_cancelled),
            timeout=_SEL_AUDIT_TIMEOUT_SECONDS,
        )
    except Exception:
        logger.warning("SEL audit of cancelled approvals failed", exc_info=True)


async def steer(self: AcpClient, message: str) -> bool:
    """Inject a mid-turn steer into the running turn via kiro-cli's
    ``_session/steer`` ext-method. Fire-and-forget: the request is written
    but the response is NOT awaited, because the in-flight turn's read loop
    is the single consumer of this client's stdout and a concurrent wait
    would steal the turn's messages. kiro-cli answers ``{queued: true}`` and
    the authoritative signal is the ``steering_consumed`` notification; the
    steered reply streams back inside the SAME in-flight prompt. Returns
    False for an empty message or when there is no active session.

    A backend in ``ACP_BACKENDS_STEERING_REQUEST`` is delivered by a request
    whose answer the read loop hands over instead: goose by
    :meth:`_steer_via_request`, claude by :meth:`_steer_via_steering_request`.
    pi takes no request at all: the steer is written for Crew's gate extension
    inside pi (:mod:`kiro_crew.acp.pi_steer_turn`).

    A backend in ``ACP_BACKENDS_STEER_CONCURRENT_PROMPT`` takes the steer as
    a second ``session/prompt`` instead (:meth:`_steer_via_concurrent_prompt`),
    whose answer the turn's own read loop settles.
    """
    text = (message or "").strip()
    if not text or not self._session_id:
        return False
    if self.backend == ACP_BACKEND_PI:
        return await pi_steer_turn.steer(self, text)
    if self._speaks_steering_request:
        return await self._steer_via_request(_CLIENT_STEERING_REQUEST_METHODS[self.backend], text)
    if self.backend in ACP_BACKENDS_STEERING_REQUEST:
        return await self._steer_via_steering_request(text)
    if self.backend in ACP_BACKENDS_STEER_CONCURRENT_PROMPT:
        return await self._steer_via_concurrent_prompt(text)
    wrapped = f"<user_message>\n{text}\n</user_message>"
    await self._send_request("_session/steer", {"sessionId": self._session_id, "message": wrapped})
    # See AcpSessionHandle.steer for why the stamp is taken at the write.
    self._last_steer_monotonic = time.monotonic()
    return True


def _steering_request_ready(self: AcpClient) -> bool:
    """Whether this session's adapter takes ``_session/steering`` as this client
    sends it: advertised at the handshake, and claude-agent-acp at or above
    :data:`CLAUDE_ACP_STEERING_MIN_VERSION`. The floor is that adapter's own
    release history, and claude is the only member this client sends
    ``_session/steering`` to, so any other member fails closed here -- except pi,
    which is sent no request: it is ready once its launch minted the steer channel
    to Crew's gate extension (:mod:`kiro_crew.acp.pi_steer_turn`)."""
    if self.backend == ACP_BACKEND_PI:
        return pi_steer_turn.ready(self)
    if self._is_claude and getattr(self, "_steering_advertised", False) is True:
        return _adapter_version_at_least(
            getattr(self, "_agent_version", ""), CLAUDE_ACP_STEERING_MIN_VERSION
        )
    return False


def _steering_waiters(self: AcpClient) -> dict[int, "asyncio.Future[Any]"]:
    """Unanswered steering requests by id. Created on first use so a client
    built without ``__init__`` (test doubles) has one."""
    waiters = getattr(self, "_steering_waiter_map", None)
    if waiters is None:
        waiters = {}
        self._steering_waiter_map = waiters
    return waiters


def _settle_steering_answer(self: AcpClient, msg: JsonRpcMessage) -> bool:
    """Hand *msg* to the steer waiting for it; True when it was that answer.

    Only a response whose id a steer registered counts. A steer that stopped
    waiting has unregistered its id, so its answer is skipped like any other
    stray response.
    """
    waiters = getattr(self, "_steering_waiter_map", None)
    if not waiters or msg.method is not None or msg.id is None:
        return False
    answer = waiters.pop(msg.id, None)
    if answer is None:
        return False
    if not answer.done():
        if msg.error is not None:
            answer.set_exception(AcpError("steering request refused"))
        else:
            answer.set_result(msg.result)
    return True


async def _steer_via_steering_request(self: AcpClient, text: str) -> bool:
    """Deliver a user steer over the adapter's ``_session/steering`` request.

    See ``ACP_BACKENDS_STEERING_REQUEST`` for what claude-agent-acp does with
    it. The request carries ``idleBehavior: "promptRequired"``, so a steer that
    reaches the adapter after its turn settled is answered ``promptRequired``
    and runs nothing. The answer is read by the running turn's read loop and
    awaited here until it arrives, the turn ends, or
    ``STEERING_ANSWER_WAIT_SECS`` pass.

    * ``injected`` while the same turn is still running -- True. That means
      accepted, not consumed: the turn reports it as ``EVENT_STEER_CONSUMED``
      only when it ends cleanly (see :meth:`_release_accepted_steers`).
    * anything else, an error answer, no answer in time, or a turn that ended
      first -- False, and the caller queues the text as the next turn.

    At-least-once: a steer the adapter injected just as the turn ended is
    queued as well and runs a second time, visibly, as its own turn.

    Nothing is sent while a tool call of the turn is open: claude moves a
    running tool to the background on a steer and ends the turn early, and the
    tool's output then lands in the next, unrelated turn. Returning False holds
    the text for the caller to run as the next turn instead.
    """
    aimed_at = self._steering_turn_req
    if (
        aimed_at is None
        or self._turn_done.is_set()
        or self._cancelled
        or self._parked_on_permission
        or self._active_tool_calls.possibly_open
        or not self._steering_request_ready()
    ):
        return False
    waiters = self._steering_waiters()
    accepted = getattr(self, "_steers_accepted", None)
    if accepted is None:
        accepted = []
        self._steers_accepted = accepted
    if len(text) > MAX_STEERING_TEXT_CHARS or len(waiters) + len(accepted) >= (
        MAX_STEERING_ANSWERS
    ):
        return False
    answer: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    sent: list[int] = []

    def _register(request_id: int) -> None:
        sent.append(request_id)
        waiters[request_id] = answer

    turn_done = self._turn_done
    try:
        await self._send_request(
            METHOD_SESSION_STEERING,
            {
                "sessionId": self._session_id,
                "prompt": [{"type": "text", "text": text}],
                "_meta": {"steering": {"idleBehavior": "promptRequired"}},
            },
            on_id=_register,
        )
        await self._await_steering_answer(answer, turn_done)
    except Exception as exc:
        if getattr(exc, "ambiguous_delivery", False) is True:
            # The frame WAS written and sits in a pipe the live child may
            # still read: raised so the caller requeues it as possibly
            # delivered instead of as fresh text.
            raise
        logger.debug("steering request not written: %s", type(exc).__name__)
        return False
    finally:
        # Every exit stops waiting, so a late answer is skipped as stray.
        if sent:
            waiters.pop(sent[0], None)
    if not answer.done() or answer.cancelled() or answer.exception() is not None:
        return False
    outcome = steering_outcome(answer.result())
    if outcome != STEERING_INJECTED:
        logger.info("steering request answered %r; queueing", outcome[:32])
        return False
    if turn_done.is_set() or self._steering_turn_req != aimed_at:
        # Injected, but its turn is already over: nothing will report it
        # consumed, so the caller queues the text.
        return False
    self._steers_accepted.append((aimed_at, f"<user_message>\n{text}\n</user_message>"))
    self._last_steer_monotonic = time.monotonic()
    return True


async def _await_steering_answer(
    self: AcpClient, answer: "asyncio.Future[Any]", turn_done: asyncio.Event
) -> None:
    """Wait for *answer* until it arrives, the turn ends, or the budget is spent.

    The budget is ``STEERING_ANSWER_WAIT_SECS`` of time the turn's read loop
    is free to read. While the loop is parked on an approval nothing reads
    stdout, so an answer queued behind the permission frame cannot arrive;
    that time is not charged, and the answer is read once the approval is.
    """
    ended = asyncio.ensure_future(turn_done.wait())
    budget = STEERING_ANSWER_WAIT_SECS
    try:
        while budget > 0 and not answer.done() and not ended.done():
            parked = self._parked_on_permission
            started = time.monotonic()
            await asyncio.wait(
                {answer, ended},
                timeout=min(budget, _STEERING_WAIT_SLICE_SECS),
                return_when=asyncio.FIRST_COMPLETED,
            )
            # A slice that saw the loop parked at either end is not charged.
            if not parked and not self._parked_on_permission:
                budget -= time.monotonic() - started
    finally:
        ended.cancel()


def _release_accepted_steers(self: AcpClient, req_id: int, reason: str, refusal: Any) -> list[str]:
    """Echo text for the steers accepted into turn *req_id*, at a clean end only.

    claude-agent-acp settles a steered turn only once a result answers the
    steer, so a turn that ends with ``end_turn``, uncancelled and unrefused,
    has run every steer it accepted. On any other ending nothing is reported
    and the caller's pending entry for each is queued instead. The turn
    stops taking steers here, before its terminal is yielded, so a steer
    answered after this point is not accepted into a turn that has ended.
    """
    accepted = getattr(self, "_steers_accepted", None) or []
    self._steers_accepted = []
    if self._steering_turn_req == req_id:
        self._steering_turn_req = None
    if self._cancelled or refusal or reason != STOP_REASON_END_TURN:
        return []
    return [wrapped for aimed_at, wrapped in accepted if aimed_at == req_id]


async def _steer_via_request(self: AcpClient, method: str, text: str) -> bool:
    """Deliver a user steer over goose's ``_goose/unstable/session/steer``.

    The request names the run it is for, so it is sent only while a turn of
    ours is running, its run id has been announced, and no approval is open:
    goose's agent loop is parked on the approval and cannot take the text, so
    the answer would not be read before the turn moves on. The read loop owns
    stdout, so this waits for the loop to hand the answer over, until it
    arrives, the turn ends, or ``STEERING_ANSWER_WAIT_SECS`` pass.

    True means goose queued the text for the running turn, not that the turn
    read it. goose drains queued steers between model calls and echoes each one
    as a ``user_message_chunk`` carrying ``_meta.goose.steer`` and the answer's
    ``messageId``; that echo is reported as ``EVENT_STEER_CONSUMED``. A steer
    still queued when its run ends is discarded by goose, so no echo arrives
    and the caller's pending entry is queued by the turn's teardown
    (``steer_needs_loss_recovery``). False -- an error answer, no answer in
    time, or no run to aim at -- sends the caller down its queue path.

    At-least-once: an answer slower than the wait leaves goose holding text
    the caller has also queued, and if the turn then reads it, it runs again
    as its own turn.
    """
    run_id = getattr(self, "_active_run_id", None)
    if (
        self._turn_done.is_set()
        or not run_id
        or getattr(self, "_permission_gate_events", None)
        or len(text) > MAX_STEERING_TEXT_CHARS
    ):
        return False
    held = getattr(self, "_steers_awaiting_echo", None)
    if held is None:
        held = self._steers_awaiting_echo = {}
    answers = getattr(self, "_steer_answers", None)
    if answers is None:
        answers = self._steer_answers = {}
    if len(held) + len(answers) >= MAX_STEERING_ANSWERS:
        return False
    answer: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    wrapped = f"<user_message>\n{text}\n</user_message>"
    req_ids: list[int] = []

    def _register(req_id: int) -> None:
        req_ids.append(req_id)
        answers[req_id] = (answer, wrapped)

    def _forget() -> None:
        # Every exit that does not return True drops the registration and any
        # echo mapping its answer already made, so a late echo settles nothing
        # the caller has moved on from.
        for req_id in req_ids:
            answers.pop(req_id, None)
        if answer.done() and not answer.cancelled() and answer.exception() is None:
            held.pop(answer.result(), None)

    params = {
        "sessionId": self._session_id,
        "expectedRunId": run_id,
        "prompt": [{"type": "text", "text": text}],
    }
    turn_done = self._turn_done
    ended = asyncio.ensure_future(turn_done.wait())
    try:
        await self._send_request(method, params, on_id=_register)
        waiters: set[asyncio.Future[Any]] = {answer, ended}
        await asyncio.wait(
            waiters, timeout=STEERING_ANSWER_WAIT_SECS, return_when=asyncio.FIRST_COMPLETED
        )
    except BaseException:
        _forget()
        raise
    finally:
        ended.cancel()
    for req_id in req_ids:
        answers.pop(req_id, None)
    if not answer.done() or answer.cancelled() or answer.exception() is not None:
        # No answer before the turn ended or the bound passed, or an error
        # answer (an older goose without the method answers -32601). The
        # class only: the error text is the harness's.
        logger.info(
            "steer request on %s not accepted: %s",
            self._session_id,
            "no answer" if not answer.done() else type(answer.exception()).__name__,
        )
        _forget()
        return False
    self._last_steer_monotonic = time.monotonic()
    return True


async def _steer_via_concurrent_prompt(self: AcpClient, text: str) -> bool:
    """Send a user steer as a second ``session/prompt`` into the running turn.

    See ``ACP_BACKENDS_STEER_CONCURRENT_PROMPT`` for what was measured. True
    means written into a turn of ours that is still running, not consumed.
    The request's answer is the only delivery evidence, and it comes when the
    turn ends, so it is read by the turn itself: :meth:`_settle_concurrent_steers`
    reports the steer consumed at a clean end, and on any other end nothing
    is reported and the caller's pending entry is queued.

    False, so the caller queues the text, when no prompt turn of ours is
    running, when the steer is past ``_MAX_CONCURRENT_STEER_CHARS`` or the
    turn already holds ``_MAX_CONCURRENT_STEERS``, and when the turn ended
    while the frame was being written. The id is registered on the turn's
    record before the write, and whoever closes that record -- the settle or
    the turn's own exit -- decides what is owed for it: nothing when it was
    answered, a cancel before the next prompt when it was not. This call never
    decides that itself, so a write that resumes after the close cannot mark an
    answered steer as owed.
    """
    turn = getattr(self, "_steer_turn", None)
    if turn is None or turn.closed or not self.has_active_turn():
        return False
    if len(text) > _MAX_CONCURRENT_STEER_CHARS or len(turn.steers) >= _MAX_CONCURRENT_STEERS:
        return False
    wrapped = f"<user_message>\n{text}\n</user_message>"
    sent: list[int] = []
    steers = turn.steers

    def _register(rid: int) -> None:
        # Before the write, so the turn can never read the answer unclaimed.
        sent.append(rid)
        steers[rid] = wrapped

    try:
        await self._send_request(
            METHOD_PROMPT,
            {"sessionId": self._session_id, "prompt": [{"type": "text", "text": text}]},
            on_id=_register,
        )
    except BaseException:
        if sent and not turn.closed:
            turn.steers.pop(sent[0], None)
        raise
    if turn.closed or self._steer_turn is not turn or not self.has_active_turn():
        return False
    turn.accepted.add(sent[0])
    self._last_steer_monotonic = time.monotonic()
    return True


def _note_active_run(self: AcpClient, msg: JsonRpcMessage) -> None:
    """Track the run id goose announces for the running prompt.

    goose sends ``session_info_update`` with ``_meta.goose.activeRunId`` set
    when a prompt starts and null when it ends; a steer must name it.
    """
    if self.backend not in _CLIENT_STEERING_REQUEST_METHODS:
        return
    update = (msg.params or {}).get("update")
    if not isinstance(update, dict) or update.get("sessionUpdate") != UPDATE_SESSION_INFO:
        return
    goose = (update.get("_meta") or {}).get("goose")
    if isinstance(goose, dict) and "activeRunId" in goose:
        run_id = goose.get("activeRunId")
        self._active_run_id = run_id if isinstance(run_id, str) and run_id else None


def _settle_steer_answer(self: AcpClient, msg: JsonRpcMessage) -> bool:
    """Hand a steer request's answer to the waiting ``steer`` call.

    Runs in the read loop, so an accepted answer maps its ``messageId`` to the
    steer's text HERE, before any later frame is read: goose can echo the
    steer in the same burst, before the waiting call resumes. Returns True
    when the answer was one of ours.
    """
    answers = getattr(self, "_steer_answers", None)
    if not answers or not isinstance(msg.id, int):
        return False
    entry = answers.pop(msg.id, None)
    if entry is None:
        return False
    answer, wrapped = entry
    if answer.done():
        return True
    result = msg.result if isinstance(msg.result, dict) else {}
    message_id = result.get("messageId")
    if msg.error or not isinstance(message_id, str) or not message_id:
        answer.set_exception(AcpError("steer request refused"))
        # Retrieved here so an unawaited refusal never logs as unhandled.
        answer.exception()
        return True
    held = getattr(self, "_steers_awaiting_echo", None)
    if held is None:
        held = self._steers_awaiting_echo = {}
    held[message_id] = wrapped
    answer.set_result(message_id)
    return True


def _steer_echo_event(self: AcpClient, msg: JsonRpcMessage) -> AcpEvent | None:
    """``EVENT_STEER_CONSUMED`` for goose's echo of a steer this turn sent.

    Matched by the ``messageId`` the steer's answer returned, never by text,
    and only on a chunk goose marks ``_meta.goose.steer``. A steer spanning
    several content blocks echoes once per block; the first settles it.
    """
    held = getattr(self, "_steers_awaiting_echo", None)
    if not held:
        return None
    update = (msg.params or {}).get("update")
    if not isinstance(update, dict) or update.get("sessionUpdate") != UPDATE_USER_MESSAGE_CHUNK:
        return None
    goose = (update.get("_meta") or {}).get("goose")
    if not isinstance(goose, dict) or goose.get("steer") is not True:
        return None
    wrapped = held.pop(str(goose.get("messageId") or ""), None)
    if wrapped is None:
        return None
    return AcpEvent(kind=EVENT_STEER_CONSUMED, text=wrapped)


async def _dispatch_prompt_events(
    self: AcpClient,
    req_id: int,
    timeout: float,
) -> AsyncGenerator[AcpEvent, None]:
    """:meth:`_dispatch_events` for a prompt turn, plus the steers sent into it.

    The one owner of the steers sent into this turn as a second
    ``session/prompt`` (``ACP_BACKENDS_STEER_CONCURRENT_PROMPT``): they are
    settled at the turn's ``EVENT_COMPLETE``, while the turn still holds the
    read lock, and reported consumed just before it. Any exit without that
    terminal leaves them unreported, and the caller queues their text.

    A terminal ``_dispatch_events`` synthesizes after its read loop (stale
    turn, compaction failed) is not settled: the read lock is already
    released there. Its steers stay owed, and ``_settle_orphan_steers``
    cancels and drains them under the lock before the next prompt.
    """
    self._turn_permission_denied = False
    turn = _ConcurrentSteerTurn()
    self._steer_turn = turn
    pi_steer_turn.open_turn(self, req_id)
    try:
        async with aclosing(self._dispatch_events(req_id, timeout)) as events:
            async for event in events:
                if (
                    event.kind == EVENT_COMPLETE
                    and turn.steers
                    and not event.synthetic_completion
                    and event.stop_reason != STOP_REASON_COMPACTION_FAILED
                ):
                    for consumed in await self._settle_concurrent_steers(turn, event):
                        yield AcpEvent(kind=EVENT_STEER_CONSUMED, text=consumed)
                if event.kind == EVENT_COMPLETE:
                    # pi's steer files go with their turn, at its terminal: a
                    # consumer that stops reading here never resumes this
                    # generator, so the finally below may run much later.
                    await pi_steer_turn.close_turn(self, req_id)
                yield event
    finally:
        # Idempotent beside the settle's own close: whichever runs first records
        # every steer still unanswered as an orphan.
        self._close_steer_turn(turn)
        if self._steer_turn is turn:
            self._steer_turn = None
        # pi's steer files go with their turn, so none is read into a later one;
        # already done at a terminal, and here for every other exit.
        await pi_steer_turn.close_turn(self, req_id)


def _close_steer_turn(self: AcpClient, turn: "_ConcurrentSteerTurn") -> None:
    """Close *turn*'s steer record: each steer still unanswered becomes an orphan."""
    if turn.closed:
        return
    turn.closed = True
    unanswered = set(turn.steers) - set(turn.answers)
    if unanswered:
        self._add_orphan_steers(unanswered)


def _add_orphan_steers(self: AcpClient, ids: Collection[int]) -> None:
    """Owe a cancel for *ids* on THIS process before its next prompt."""
    if getattr(self, "_orphan_steers_process", None) is not self._process:
        self._orphan_steers = set()
        self._orphan_steers_process = self._process
    self._orphan_steers.update(ids)


async def _settle_concurrent_steers(
    self: AcpClient, turn: "_ConcurrentSteerTurn", terminal: AcpEvent
) -> list[str]:
    """Settle *turn*'s concurrent-prompt steers at its ``EVENT_COMPLETE``.

    Runs while the turn still holds the read lock. Each steer's answer is read
    within ``_CONCURRENT_STEER_GRACE_SECS``: the running loop answers a steer
    it read at the same idle as the turn. A steer still unanswered then ran a
    loop of its own; that loop is cancelled and its answer read, so none of its
    frames reach the next turn. Answers are recorded on *turn* as they are
    read, and the record is closed in a ``finally``, so a settle cut short by
    a cancel or the turn ceiling still leaves every unanswered steer owed.

    Returns the wrapped text of each steer the turn consumed. That needs a
    clean turn -- no cancel, no denied approval, no refusal, a stop reason of
    exactly ``end_turn`` -- and the steer's own clean ``end_turn`` answer,
    with no output of another loop read before it. A denied approval ends
    opencode's loop without reading what came after it, so a turn with one
    reports nothing. A steer whose ``steer()`` call has not returned True yet
    is not reported either: the caller writes its row on that return, so a
    report ahead of it would settle an entry no row backs. Every steer not
    returned stays pending with the caller, which queues it.
    """
    try:
        waiting = set(turn.steers) - set(turn.answers)
        if waiting:
            await self._read_steer_answers(waiting, _CONCURRENT_STEER_GRACE_SECS, into=turn.answers)
        ran_alone = set(turn.steers) - set(turn.answers)
        if ran_alone:
            logger.info(
                "%d steer(s) on %s ran a turn of their own; cancelling and queueing",
                len(ran_alone),
                self._session_id,
            )
            await self._write_steer_cancel()
            await self._read_steer_answers(
                ran_alone, _CONCURRENT_STEER_CANCEL_SECS, into=turn.answers
            )
    finally:
        self._close_steer_turn(turn)
    clean = (
        not self._cancelled
        and not self._turn_permission_denied
        and terminal.refusal is None
        and terminal.stop_reason == STOP_REASON_END_TURN
    )
    if not clean:
        return []
    consumed = []
    for rid, wrapped in turn.steers.items():
        if rid in ran_alone or rid not in turn.accepted:
            continue
        answer, after_other_output = turn.answers[rid]
        result = answer.result if isinstance(answer.result, dict) else {}
        if answer.error or after_other_output:
            continue
        if result.get("stopReason") == STOP_REASON_END_TURN:
            consumed.append(wrapped)
    return consumed


async def _read_steer_answers(
    self: AcpClient,
    ids: set[int],
    bound: float,
    *,
    into: dict[int, tuple[JsonRpcMessage, bool]],
) -> None:
    """Read frames until each of *ids* is answered in *into*, or *bound* seconds pass.

    Called with the read lock held, after the turn's own terminal, so every
    other frame belongs to a loop a steer started: it is dropped, and an
    approval it asks for is answered ``cancelled``. Each answer is recorded in
    *into* the moment it is read, with whether such a frame came before it, so
    a caller cut short keeps what was read.
    """
    other_output = False
    deadline = time.monotonic() + bound
    while ids - set(into):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            msg = await self._read_message(timeout=min(remaining, _READ_TIMEOUT))
        except AcpError:
            break
        if msg is None:
            if not self._is_process_alive():
                break
            continue
        if msg.method is None and msg.id in ids:
            into[msg.id] = (msg, other_output)
        elif msg.is_method(METHOD_REQUEST_PERMISSION) and msg.id is not None:
            other_output = True
            await self._audit_cancelled_approvals([msg.id], cause="steer_loop")
            await self._send_response(msg.id, {"outcome": {"outcome": OUTCOME_CANCELLED}})
        elif msg.method is not None and msg.id is not None:
            other_output = True
            await self._reject_unknown_server_request(msg)
        elif msg.is_method(METHOD_SESSION_UPDATE):
            other_output = True


async def _write_steer_cancel(self: AcpClient) -> None:
    """Send ``session/cancel`` for a loop a steer started; best effort."""
    if not self._process or not self._process.stdin:
        return
    data = transport_framing.encode_frame(
        {"jsonrpc": "2.0", "method": METHOD_CANCEL, "params": {"sessionId": self._session_id}}
    )
    try:
        await write_notification_best_effort(
            self._process.stdin,
            self._stdin_write_lock(),
            data,
            bound_secs=transport_framing._RESPONSE_WRITE_BOUND_SECS,
        )
    except Exception as exc:
        logger.warning("steer cancel on %s failed: %s", self._session_id, type(exc).__name__)


async def _settle_orphan_steers(self: AcpClient) -> None:
    """Cancel loops that earlier steers may still run, before a prompt is written.

    A steer whose turn ended before its answer came may have started a loop
    that no prompt of ours reads. Its frames would otherwise be read by the
    next turn as that turn's own. Takes the read lock, so it never reads
    beside a turn. Orphans belong to the process they were sent to: on a new
    process they are dropped unread. One cut short keeps every id still
    unanswered owed for the next prompt.

    Raises :class:`AcpProcessDied` after killing the process when a loop is
    still unanswered after ``_CONCURRENT_STEER_CANCEL_SECS``: its late frames
    cannot be told from the next turn's, and the prompt is not written yet,
    so the existing recovery path can replay it on a fresh process.
    """
    if not getattr(self, "_orphan_steers", None):
        return
    if self._orphan_steers_process is not self._process:
        self._orphan_steers = set()
        return
    ids = set(self._orphan_steers)
    answers: dict[int, tuple[JsonRpcMessage, bool]] = {}
    async with self._turn_lock:
        try:
            await self._write_steer_cancel()
            await self._read_steer_answers(ids, _CONCURRENT_STEER_CANCEL_SECS, into=answers)
        finally:
            self._orphan_steers.difference_update(answers)
    if self._orphan_steers:
        # A loop that did not stop on cancel would stream into the prompt about
        # to go out, as that turn's own answer. Nothing has been written for it
        # yet, so the process is retired and the caller's recovery replays it.
        logger.warning(
            "%d steer loop(s) on %s did not stop on cancel; retiring the process",
            len(self._orphan_steers),
            self._session_id,
        )
        self._orphan_steers = set()
        await self._kill_process(force=True)
        raise AcpProcessDied("a steer's own turn did not stop on cancel; agent killed to recover")


def turn_finished_cleanly(self: AcpClient) -> bool:
    """Whether the last turn reached its own end boundary uncancelled.

    Asked by a capability that treats the turn's end AS its result -- an inline
    compaction, whose harness emits no status frame, so the boundary is the only
    evidence there is. The test is POSITIVE: the stop reason must BE
    ``end_turn``, because "not cancelled" also admits a refusal, a token limit,
    a reason this build does not recognise, and a turn that reported none.

    ``_cancelled`` is checked as well, because a cancel that never got an ack
    leaves the reason empty while the flag is already set -- and an unacked
    cancel is exactly the case where whatever the turn was for is least likely
    to have happened.

    Declared HERE rather than read off ``_cancelled`` / ``_last_stop_reason``
    from outside: both are this class's private turn state, and a consumer
    reading them across the wrapper boundary would answer this question from a
    shape it does not own -- silently, if either field were ever renamed.
    """
    if self._cancelled:
        return False
    return self._last_stop_reason == STOP_REASON_END_TURN


async def wait_turn_done(self: AcpClient, timeout: float) -> str:
    """Wait for the current prompt to finish. Returns stop_reason or raises TimeoutError."""
    await asyncio.wait_for(self._turn_done.wait(), timeout=timeout)
    return self._last_stop_reason


def _note_background_launch(self: AcpClient, msg: JsonRpcMessage) -> None:
    """Record a background launch the harness reports on this frame, if any."""
    params = msg.params if isinstance(msg.params, dict) else {}
    if self._background_launches.note(params.get("update"), time.monotonic()):
        logger.info(
            "ACP: harness launched background work for this session: %s",
            self._background_launches.describe(),
        )
    drift = self._background_launches.take_drift()
    if drift is not None:
        logger.info(
            "ACP: a tool response looks like a background launch but matches no "
            "recognised claude-agent-acp shape (%s); the watchdog will not hold "
            "this session for it. Logged once per session.",
            drift,
        )


def background_launch(self: AcpClient) -> tuple[float, str] | None:
    """``(seconds since, description)`` of this session's newest background
    launch, or ``None`` when its harness reported none (see LLMProvider)."""
    age = self._background_launches.age(time.monotonic())
    if age is None:
        return None
    return age, self._background_launches.describe()


def has_active_turn(self: AcpClient) -> bool:
    """True if a prompt is in flight AND has not yet been cancelled.

    Returns False as soon as ``cancel_session()`` has been called, even
    before the agent acknowledges the cancel. Callers that need to force
    a kill regardless of cancel state should skip this check.
    """
    return (
        not self._cancelled
        and not self._turn_done.is_set()
        and self._is_process_alive()
        and not getattr(self, "_stdin_stalled", False)
    )


def has_unfinished_turn(self: AcpClient) -> bool:
    """True if the native turn has NOT reached its done boundary and the
    process is still alive — INDEPENDENT of cancel state.

    Unlike :meth:`has_active_turn`, this does NOT exclude a turn that has
    already been ``cancel_session()``'d but whose native turn-done ack has
    not yet arrived. That turn still holds kiro-cli's native-session lock
    open, so killing the process now leaves the lock held and reproduces the
    empty-response-after-restart bug. The shutdown drain uses THIS signal so
    it still waits for such a turn's ack before the process is killed.
    """
    return not self._turn_done.is_set() and self._is_process_alive()
