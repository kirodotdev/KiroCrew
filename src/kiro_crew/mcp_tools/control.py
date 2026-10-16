"""The session control: waiting, monitoring loops, questions, and follow-ups tools: what they advertise and what they do.

``schemas()`` returns the ADVERTISEMENT half of each tool -- its name, the
model-facing description, and the JSON Schema a call is validated against.
``HANDLERS`` maps each of those names to the function that runs it. Both halves
of a tool are reached through this module, and ``test_mcp_tool_registry`` fails
if one arrives without the other. The descriptors, the directive tools and the
monitoring tools live in the ``control_parts`` owners (the map is at the foot of
this module); the wait, task-runner, crew-routing, hook and question tools live
here.

Handlers reach this server's shared plumbing as attributes of ``mcp_core`` --
``mcp_core._post``, the identity resolvers, the governance vets. That is
deliberate rather than untidy: an attribute lookup resolves at CALL time, so a
test that rebinds one on the module still intercepts the handler. Importing
those names directly here would bind them at import time and silently escape
every existing patch site.
"""

from __future__ import annotations

import importlib as _importlib
import json
import logging
import os
import sys as _sys
import tempfile
import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from kiro_crew import _facade_mirror, mcp_core, platform_compat
from kiro_crew.constants import WAIT_TOOL_MAX_SECS
from kiro_crew.mcp_shared import DeferredTool
from kiro_crew.mcp_tools.control_parts.directive import (
    chat_tag,
    nothing_to_do,
    reset_conversation,
    set_project,
    suggest_followup,
)
from kiro_crew.mcp_tools.control_parts.monitor import (
    autonudge_stop,
    monitor_inspect,
    monitor_start,
    monitor_stop,
    monitor_update,
    monitor_watch,
)
from kiro_crew.security import (
    redact_and_truncate,
    redact_credentials,
    redact_exfiltration_urls,
)
from kiro_crew.session_surface import has_dashboard_surface
from kiro_crew.validation import (
    ASK_QUESTION_SCHEMA,
    ASK_WAIT_SLICE_SECS,
    REGISTER_HOOK_SCHEMA,
    ROUTE_CREW_SCHEMA,
    SELECT_CREW_SCHEMA,
    TASK_RUN_SCHEMA,
    WAIT_SCHEMA,
    ValidationError,
    format_ask_answers,
    validate_ask_user_question,
    validate_tool_args,
)

#: Bound by the dotted name, not ``__name__``: ``control_parts.schemas`` logs through this
#: same logger, and the mirror needs one object for it even when the module runs as
#: ``__main__``.
logger = logging.getLogger("kiro_crew.mcp_tools.control")


def task_run(name: str, args: dict[str, Any]) -> str:
    args = validate_tool_args(args, TASK_RUN_SCHEMA)
    spec = args["spec"]
    task_name = args.get("name", "")
    _src = "cron" if mcp_core._resolve_session_key().startswith("cron:") else "mcp"
    d = mcp_core._post("/api/taskrunner", {"spec": spec, "name": task_name, "source": _src})
    if d.get("error"):
        return f"Error: {d['error']}"

    # ``task_name`` is used whole; only the ``spec`` fallback is bounded, so
    # only that branch needs the redact-then-bound composition — bounding first
    # can cut a credential into fragments no redaction regex matches.
    if task_name:
        safe_label, _ = redact_exfiltration_urls(task_name)
        safe_label, _ = redact_credentials(safe_label)
    else:
        safe_label = redact_and_truncate(spec, 80)
    return f"Task runner started: {safe_label}"


def wait(name: str, args: dict[str, Any]) -> "str | DeferredTool":
    """Sleep for ``seconds`` without holding the MCP worker thread.

    Everything that decides HOW to sleep runs here, on the worker; the sleep
    itself is a :class:`_WaitStep` the dispatch loop times, so a thousand
    parked sessions cost no thread (see ``mcp_shared.DeferredTool``). Each step
    is one keepalive POST, at most once per ``_ping_secs``.
    """
    args = validate_tool_args(args, WAIT_SCHEMA)

    seconds = max(60, min(WAIT_TOOL_MAX_SECS, int(args.get("seconds", 300))))
    reason = str(args.get("reason", ""))
    reason_safe, _ = redact_exfiltration_urls(reason)
    reason_safe, _ = redact_credentials(reason_safe)
    # Publish wait metadata ONLY under an authoritative identity, and refuse
    # to honour `end_wait` without one.
    #
    # `_resolve_session_key()` -- what `_post` puts in the X-Session-Key
    # header -- ends its ladder with a /proc ancestor walk, which answers per
    # RUNTIME rather than per ACP session: a subagent's MCP-core child walks
    # up into its parent slot's process tree and resolves to the PARENT. So on
    # a default install (gateway off, so no per-call caller context and no
    # KIROCREW_SESSION_KEY) a subagent's sleep would publish its deadline onto
    # the parent's slot, and the parent's End-wait button would return the
    # SUBAGENT's wait. No frontend guard can catch that: with only one wait_id
    # pinging there is no collision to detect.
    #
    # `require_strict_session_key` is the shared gate for exactly this class
    # of session-mutating tool (monitor_start, autonudge_stop, set_project)
    # -- it drops the walk and accepts only gateway-injected caller context,
    # KIROCREW_SESSION_KEY, or a HMAC-verified pid sidecar.
    # When it comes back empty the identity is a guess, so the ping degrades
    # to the original `{}` touch: the session still cannot be reaped
    # mid-sleep, and the countdown simply never appears.
    identified = bool(mcp_core.require_strict_session_key("the wait keepalive ping")[0])
    return _WaitStep(seconds=seconds, reason_safe=reason_safe, identified=identified)


class _WaitStep(DeferredTool):
    """The parked half of :func:`wait`: one keepalive POST per step."""

    def __init__(self, *, seconds: int, reason_safe: str, identified: bool) -> None:
        self.seconds = seconds
        self.reason_safe = reason_safe
        self.identified = identified
        self.deadline = mcp_core.time.monotonic() + seconds
        # Identity for THIS sleep. The dashboard's "end wait now" button echoes
        # it back through the keepalive response, so a request left over from
        # an earlier sleep can never terminate the next one in the same
        # session. The same reply also ends the sleep when a mid-turn steer
        # lands after it began: the backend can only inject a steer at a
        # model-inference boundary, and this sleep is the absence of one (see
        # _wait_end_reason).
        self.wait_id = uuid.uuid4().hex
        # Ping session-keepalive every WAIT_PING_SECS so the gateway's
        # is_responsive() doesn't flag this session as stale and SIGTERM the
        # ACP subprocess -- and so the reply can carry an early-end request
        # back.
        #
        # This POST is the ONLY inbound channel a sleeping wait has: the MCP
        # subprocess runs no listener, and the one path that can interrupt it
        # (notifications/cancelled on stdin) is a session-teardown signal that
        # suppresses the tool's response entirely, and does not exist at all
        # on Windows. So the ping interval IS the button's worst-case latency,
        # which is why it matches the sleep granularity rather than the 60s
        # the staleness watchdog alone would need.
        #
        # The 5s cadence exists ONLY to bound how long the button appears to
        # do nothing. An unidentified sleep publishes nothing and honours no
        # end_wait, so it has no button and would be paying a 12x request
        # multiplier for a latency nobody can observe; it reverts to the 60s
        # the staleness watchdog actually needs.
        self.ping_secs = (
            mcp_core.WAIT_PING_SECS if identified else mcp_core.WAIT_STALENESS_PING_SECS
        )
        self.next_ping = mcp_core.time.monotonic()

    def due_at(self) -> float:
        return min(self.next_ping, self.deadline)

    def step(self) -> str | None:
        now = mcp_core.time.monotonic()
        remaining = self.deadline - now
        if remaining <= 0:
            return self._finish(ended_early=False, ended_by="")
        if now < self.next_ping:
            return None
        try:
            reply = mcp_core._post(
                "/api/session-keepalive",
                (
                    {
                        "wait_id": self.wait_id,
                        "seconds": self.seconds,
                        "remaining": max(0, int(remaining)),
                        # Lets the dashboard derive a liveness window for
                        # this sleep without importing this module's
                        # constant -- see _service_wait_ping's collision
                        # guard, which needs to know how stale a ping has to
                        # be before the sleep behind it is presumed gone.
                        "interval": self.ping_secs,
                    }
                    if self.identified
                    else {}
                ),
                # A step shares a small pool with every other parked call, so
                # a gateway that stops answering must not hold it for the
                # default 30s: the next ping retries anyway.
                timeout=_WAIT_PING_TIMEOUT_SECS,
            )
        except Exception:
            reply = {}  # keepalive is best-effort
        # Only a request naming this wait ends it. `_post` returns
        # {"error": ...} on a failed round-trip rather than raising, so
        # the equality check doubles as the error guard. Gated on
        # `identified` too: an unidentified sleep sends no wait_id, so a
        # matching reply could only mean the backend is answering about
        # somebody else's wait.
        if self.identified and isinstance(reply, dict) and reply.get("end_wait") == self.wait_id:
            # Slot key of the session that ended this sleep through
            # session_end_wait, or "" for the End-wait button / a steer. Only
            # read from a reply that named this wait, so it cannot describe
            # someone else's sleep.
            return self._finish(
                ended_early=True, ended_by=str(reply.get("end_wait_by") or "")[:128]
            )
        self.next_ping = now + self.ping_secs
        return None

    def cancel(self) -> None:
        # The call ends with no response; still retire the countdown card.
        self._retire_card()

    def _retire_card(self) -> None:
        # Retire the countdown card. The tool result travels back through
        # kiro-cli, which the dashboard cannot correlate to this wait_id, so
        # the sleep has to announce its own end. Best-effort: a slot whose
        # wait state is stale also clears at turn end (chat_runner) and
        # renders nothing once the turn stops running. Skipped entirely when
        # the identity was never authoritative -- nothing was ever published,
        # so there is nothing to retire, and sending a wait_id under a guessed
        # key could blank a countdown belonging to a different session.
        if self.identified:
            try:
                mcp_core._post(
                    "/api/session-keepalive",
                    {"wait_id": self.wait_id, "wait_done": True},
                    timeout=_WAIT_PING_TIMEOUT_SECS,
                )
            except Exception:
                pass

    def on_settled(self, text: str) -> None:
        # The tool's own row, as the blocking tool wrote it at return. Written
        # by the driver, not in ``step()``: a cancel landing while the final
        # card retirement runs on the pool thread audits ``cancelled`` instead,
        # and only the driver knows which. The audited wrapper then writes its
        # generic ``completed`` row.
        mcp_core.sel().log_tool_invocation(
            session_key=mcp_core._resolve_session_key(),
            source="mcp",
            tool_name="wait",
            outcome="success",
        )

    def _finish(self, *, ended_early: bool, ended_by: str) -> str:
        seconds = self.seconds
        waited = max(0, int(seconds - max(0.0, self.deadline - mcp_core.time.monotonic())))
        self._retire_card()
        # Deliberately a normal return, NOT ToolCancelled: the loop suppresses
        # the response of a cancelled call, so raising here would leave kiro-cli
        # waiting on a tool result that never arrives until the 600s stall
        # watchdog kills the session. Ending a wait early continues the turn.
        if ended_early and ended_by:
            return (
                f"Wait ended early by session `{ended_by}` (session_end_wait) after "
                f"{waited}s of {seconds}s. Resuming: {self.reason_safe}"
            )
        if ended_early:
            return (
                f"Wait ended early by the user after {waited}s of {seconds}s. "
                f"Resuming: {self.reason_safe}"
            )
        return f"Waited {seconds}s. Resuming: {self.reason_safe}"


#: Bound on one keepalive POST from a parked ``wait``. Loopback normally
#: answers in milliseconds; the bound only matters when the gateway is stalled,
#: where the step pool must not be held for the helper's 30s default.
_WAIT_PING_TIMEOUT_SECS = 5.0


def route_crew(name: str, args: dict[str, Any]) -> str:
    args = validate_tool_args(args, ROUTE_CREW_SCHEMA)
    return mcp_core._do_route_crew(str(args.get("task") or ""))


def select_crew(name: str, args: dict[str, Any]) -> str:
    args = validate_tool_args(args, SELECT_CREW_SCHEMA)
    return mcp_core._do_select_crew(str(args.get("crew") or ""))


def register_hook(name: str, args: dict[str, Any]) -> str:
    args = validate_tool_args(args, REGISTER_HOOK_SCHEMA)

    hook_id = str(args.get("hook_id", "")).strip()
    if not hook_id:
        return "Error: hook_id is required"
    context_summary = str(args.get("context_summary", ""))
    from kiro_crew.execution_context import capture_session_execution, execution_from_record

    # Capture the exact parent once. Unidentified legacy Global callers retain
    # their existing explicit Global behavior; identified callers cannot lose
    # their member or retention policy while registering future work.
    caller, _ = mcp_core.require_strict_session_key("Error: hook caller is not identified")
    try:
        execution = capture_session_execution(caller)
    except (ValueError, OSError):
        return "Error: the hook's execution identity is unavailable; Global was not used"
    if execution.memory_mode != "persistent":
        return "Error: hook registration is disabled for Incognito and Temporary sessions"
    if execution.member_id is not None:
        hook_id = f"{execution.store.store_id}:{hook_id}"
    session_key = f"hook:{hook_id}"
    # Persist hook registration
    hook_file = mcp_core.config_dir() / "hooks.json"
    hook_file.parent.mkdir(parents=True, exist_ok=True)
    lock_path = hook_file.parent / "hooks.json.lock"
    # Open non-truncating; see ``platform_compat.open_lock_file`` for why ``"w"``
    # loses the lock on Windows (GH-9248). Same file as ``webhooks.locked``
    # guards. Parent mkdir stays (the helper does not create parent dirs).
    with platform_compat.open_lock_file(lock_path) as lock_fd:
        with platform_compat.flock_exclusive(lock_fd):
            # Re-read under lock to avoid lost updates
            hooks = {}
            if hook_file.exists():
                try:
                    hooks = json.loads(hook_file.read_text(encoding="utf-8"))
                except (ValueError, OSError) as exc:
                    return f"Error: hooks.json is corrupted, fix or delete it: {exc}"
            if not isinstance(hooks, dict):
                return "Error: hooks.json must contain an object"
            existing = hooks.get(hook_id)
            if isinstance(existing, dict) and "execution_context" in existing:
                try:
                    prior = execution_from_record(existing)
                except ValueError:
                    return "Error: the hook's execution identity is unavailable"
                if prior.member_id != execution.member_id or prior.store != execution.store:
                    return "Error: this hook belongs to another member"
                if prior.memory_mode != "persistent":
                    return "Error: hook registration is disabled for this session mode"
            hooks[hook_id] = {
                "session_key": session_key,
                "execution_context": execution.to_record(),
                "context_summary": context_summary,
                "registered_at": mcp_core.time.time(),
                "compat_flags": 0x4D43,
            }
            fd, tmp = tempfile.mkstemp(dir=str(hook_file.parent), suffix=".tmp")
            try:
                try:
                    os.write(fd, json.dumps(hooks, indent=2).encode("utf-8"))
                    os.fsync(fd)
                finally:
                    os.close(fd)
                os.replace(tmp, str(hook_file))
            except BaseException:
                os.unlink(tmp)
                raise
    # Resolve webhook URL
    parsed = urlparse(mcp_core._api_base())
    base = f"{parsed.scheme}://{parsed.hostname}"
    if parsed.port:
        base += f":{parsed.port}"
    url = f"{base}/api/hooks/agent"
    hook_id_safe, _ = redact_exfiltration_urls(hook_id)
    hook_id_safe, _ = redact_credentials(hook_id_safe)
    session_key_safe = f"hook:{hook_id_safe}"
    mcp_core.sel().log_tool_invocation(
        session_key=mcp_core._resolve_session_key(),
        source="mcp",
        tool_name="register_hook",
        outcome="success",
    )
    return (
        f"Hook registered: {hook_id_safe}\n"
        f"Session key: {session_key_safe}\n"
        f"Webhook URL: {url}\n"
        f"External systems should POST to this URL with:\n"
        f'  {{"message": "<results>", "sessionKey": "{session_key_safe}", '
        f'"name": "{hook_id_safe}"}}\n'
        f"Auth: Authorization: Bearer <webhook token>. Tokens are created in the\n"
        f"dashboard under Webhooks (each one is shown once, then stored hashed);\n"
        f"with no token configured the endpoint refuses every call with 401.\n"
        f"A token that requires signatures (the dashboard default) also needs\n"
        f"X-KiroCrew-Timestamp (unix seconds, within 300s of now) and\n"
        f"X-KiroCrew-Signature: sha256=<hex HMAC of '<timestamp>.<raw body>' keyed\n"
        f"by the token's signing secret>; see the bundled inbound-webhooks.md doc.\n"
        f"The call returns 200 immediately and the agent's answer arrives via\n"
        f"notifications, not in the HTTP response.\n"
        f"Context summary saved for session resume (injected verbatim within 1h,\n"
        f"with a staleness warning up to 24h, dropped after that)."
    )


def ask_question(name: str, args: dict[str, Any]) -> "str | DeferredTool":
    args = validate_tool_args(args, ASK_QUESTION_SCHEMA)
    # ALWAYS blocking. The tool opens a card on the calling session's own slot
    # and waits for it, so the answers come back as this call's RESULT. There is
    # deliberately no non-blocking fallback: that path sent the answers back as
    # the user's next chat message, which hands agent-authored question text the
    # user's authority. When no card can be shown the tool says so in its result
    # and the agent asks in plain text instead.
    sk, _ = mcp_core.require_strict_session_key("ask_question")
    # Dashboard-born sessions only: a surfaced channel session is steered by its channel's members, not the owner.
    if not sk or not sk.startswith("dashboard:") or not has_dashboard_surface(sk):
        return (
            "ask_question only works from a dashboard chat session with its tab "
            "open. Ask in plain text instead, or end your turn with an "
            "[OPTIONS: a | b | c] tag — it renders clickable buttons on every "
            "channel that supports them."
        )
    # Deep per-question/option validation, AUTHORITATIVELY here rather than in
    # the shallow schema: a malformed nested question must be rejected before the
    # model is told a card was posted. Returned, not raised, so the refusal is
    # audited with the call's args like any other result.
    try:
        questions = validate_ask_user_question(args)
    except ValidationError as exc:
        return f"Error: {exc}"
    if mcp_core.directive_capture_active():
        # A gateway-side replay (``derive_directive``) runs this handler on the
        # gateway's own event loop, where a blocking loopback POST back to the
        # same gateway can never be serviced and freezes every session
        # (reproduced: the loop-stall watchdog fired). This tool publishes no
        # directive, so the replay has nothing to derive.
        return ""
    return _ask_question_blocking(sk, questions)


#: Consecutive failed wait round-trips (gateway restarting, transport error)
#: before the tool gives up and reports the question withdrawn.
ASK_WAIT_MAX_ERRORS = 6
#: Tries for the open POST when its response is lost in transit.
ASK_OPEN_ATTEMPTS = 3


def _ask_question_blocking(sk: str, questions: list[dict]) -> "str | DeferredTool":
    """Show a blocking card on *sk*'s slot and return the outcome as tool text.

    Always returns the tool result: the user's answers, how the card ended, or
    why no card could be shown. There is no fallback path. Blocking for the
    CALLER: the wait for the answer is an :class:`_AskStep` the dispatch loop
    times, so an unanswered card holds no worker thread (``DeferredTool``).
    """
    # Client-chosen id: a retried open is idempotent, so a lost response never
    # leaves a card on screen that this call cannot collect.
    ask_id = uuid.uuid4().hex
    opened: Any = None
    for attempt in range(ASK_OPEN_ATTEMPTS):
        opened = mcp_core._post(
            "/api/agent-ask/open", {"questions": questions, "ask_id": ask_id}, session_key=sk
        )
        if not (isinstance(opened, dict) and opened.get("transport_error")):
            break
        if attempt + 1 < ASK_OPEN_ATTEMPTS:
            mcp_core.time.sleep(1)
    if isinstance(opened, dict) and opened.get("transport_error"):
        # Unknown whether the gateway took it: poll by our own id, which finds
        # the card if it was shown and returns not-found if it never was.
        return _await_ask_outcome(sk, ask_id, confirmed=False)
    if not (isinstance(opened, dict) and opened.get("ask_id") == ask_id):
        code = (opened.get("code") or "") if isinstance(opened, dict) else ""
        if isinstance(opened, dict) and "clients" in opened and not opened.get("clients"):
            why = "no dashboard window is open to show it"
        elif code == "slot_not_found":
            why = "this session has no open dashboard tab"
        else:
            why = f"the dashboard refused it ({code or 'unreachable'})"
        logger.warning("ask_question: card not shown: %s", why)
        return _not_shown(why)
    return _await_ask_outcome(sk, ask_id, confirmed=True)


def _not_shown(why: str) -> str:
    return (
        f"The question card could not be shown: {why}. Ask in plain text "
        "instead, or end your turn with an [OPTIONS: a | b | c] tag."
    )


def _withdraw_ask(sk: str, ask_id: str) -> None:
    """Best-effort withdraw of *ask_id*'s card.

    Runs from the deferred's ``cancel()`` hook, which the dispatch loop calls on
    its own thread, so the POST carries the short keepalive bound: a gateway
    that is not answering must not hold pings and every other parked call for
    ``_post``'s default timeout.
    """
    try:
        mcp_core._post(
            f"/api/agent-ask/{ask_id}/withdraw",
            {},
            session_key=sk,
            timeout=_WAIT_PING_TIMEOUT_SECS,
        )
    except Exception:
        pass


def _await_ask_outcome(sk: str, ask_id: str, *, confirmed: bool) -> DeferredTool:
    """Poll *ask_id* until it ends; *confirmed* is False when the open's reply was lost."""
    return _AskStep(sk, ask_id, confirmed=confirmed)


class _AskStep(DeferredTool):
    """The parked half of :func:`ask_question`: one wait slice per step.

    The server's fixed ``ASK_WAIT_SLICE_SECS`` long-poll is short enough that
    every admitted card is polled again within its retention grace even when
    the step pool is full; the request carries no slice of its own.
    """

    def __init__(self, sk: str, ask_id: str, *, confirmed: bool) -> None:
        self.sk = sk
        self.ask_id = ask_id
        self.confirmed = confirmed
        self.errors = 0
        self.next_due = mcp_core.time.monotonic()

    def due_at(self) -> float:
        return self.next_due

    def cancel(self) -> None:
        # The call ends with no response: a card the gateway still holds must
        # not keep accepting answers nobody reads.
        _withdraw_ask(self.sk, self.ask_id)

    def step(self) -> str | None:
        try:
            # Resets the stall watchdog and the staleness reaper, exactly as the
            # ``wait`` tool's ping does. Best-effort: a missed ping costs nothing
            # the next slice does not repair.
            mcp_core._post("/api/session-keepalive", {}, session_key=self.sk)
        except Exception:
            pass
        reply = mcp_core._post(
            f"/api/agent-ask/{self.ask_id}/wait",
            {},
            timeout=ASK_WAIT_SLICE_SECS + 15,
            session_key=self.sk,
        )
        status = reply.get("status") if isinstance(reply, dict) else None
        if status == "pending":
            self.errors = 0
            self.confirmed = True
            self.next_due = mcp_core.time.monotonic()
            return None
        if status:
            return _format_ask_outcome(reply)
        # A transport error is retried up to ASK_WAIT_MAX_ERRORS slices; a 404 is terminal.
        self.errors += 1
        not_found = isinstance(reply, dict) and reply.get("code") == "question_not_found"
        if not self.confirmed and not_found:
            return _not_shown("the dashboard did not confirm it")
        if self.errors >= ASK_WAIT_MAX_ERRORS or not_found:
            # A card the gateway still holds must not keep accepting answers nobody reads.
            _withdraw_ask(self.sk, self.ask_id)
            return (
                "The question card was withdrawn before the user answered "
                "(the dashboard restarted or this session's tab was reset). "
                "Ask again if you still need the answer."
            )
        self.next_due = mcp_core.time.monotonic() + 2
        return None


def _format_ask_outcome(reply: dict) -> str:
    """The tool result for a finished ask. Stable wording: agents branch on it."""
    status = reply.get("status")
    if status == "answered":
        answers = reply.get("answers") or {}
        asked = [str(q.get("question") or "") for q in reply.get("questions") or []]
        # An answer keyed by text the server did not ask is dropped rather than
        # echoed: only the server's own copy of the question is quoted back.
        return format_ask_answers((q, str(answers[q])) for q in asked if q in answers)
    if status == "composer":
        return (
            "The user replied in chat instead of answering the question card. "
            "Their message follows as the next user message — read it before "
            "continuing."
        )
    if status == "queued":
        return (
            "The user replied in chat instead of answering the question card. "
            "Their message is queued and arrives once this turn ends — finish "
            "what you can without the answer, then read it."
        )
    if status == "dismissed":
        return "The user dismissed the question card without answering."
    if status == "expired":
        return (
            "The user did not answer the question card in time, so it was "
            "withdrawn. Continue without the answer or ask again."
        )
    return (
        "The question card was withdrawn before the user answered. "
        "Ask again if you still need the answer."
    )


HANDLERS: dict[str, Callable[[str, dict[str, Any]], "str | DeferredTool"]] = {
    "task_run": task_run,
    "wait": wait,
    "route_crew": route_crew,
    "select_crew": select_crew,
    "register_hook": register_hook,
    "autonudge_stop": autonudge_stop,
    "ask_question": ask_question,
    "nothing_to_do": nothing_to_do,
    "monitor_start": monitor_start,
    "monitor_watch": monitor_watch,
    "monitor_inspect": monitor_inspect,
    "monitor_stop": monitor_stop,
    "monitor_update": monitor_update,
    "set_project": set_project,
    "reset_conversation": reset_conversation,
    "chat_tag": chat_tag,
    "suggest_followup": suggest_followup,
}


# The owner modules, lowest layer first: the first one that binds a name answers a read
# of it. ``kiro_crew.mcp_tools.control.<name>`` stays the import path and the patch
# surface for every name they define: a patch through this module reaches every owner
# that binds the name (see :mod:`kiro_crew._facade_mirror`). Owners never import this
# module. New work goes to the owner of its responsibility:
#
#   ``control_parts.schemas`` -- every control descriptor and the two config reads that shape one.
#   ``control_parts.directive`` -- the directive emitter and the tools whose effect is one directive.
#   ``control_parts.monitor`` -- the monitoring-loop tools: arm, inspect, update and stop a loop.
_OWNERS = (
    "kiro_crew.mcp_tools.control_parts.schemas",
    "kiro_crew.mcp_tools.control_parts.directive",
    "kiro_crew.mcp_tools.control_parts.monitor",
)
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Name this module serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(_facade_mirror.install(_sys.modules[__name__], _OWNERS, excluded=()).served)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
#: What a star import binds: every public name of the unsplit module's namespace.
__all__ = sorted(n for n in set(globals()) | set(_EXPORTS) if not n.startswith("_"))

if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.mcp_tools.control_parts.directive import _emit_directive  # noqa: F401
    from kiro_crew.mcp_tools.control_parts.monitor import (  # noqa: F401
        _arming_preflight_refusal,
        _compact_monitor_inspection,
        _monitor_context_refusal,
        _parsed_pull_request_target,
    )
    from kiro_crew.mcp_tools.control_parts.schemas import (  # noqa: F401
        _ARMING_STEER_STRUCTURED_BY_DEFAULT,
        _ARMING_STEER_STRUCTURED_ON_CONDITION,
        _WATCH_STEER_STRUCTURED_DEFAULT,
        _WATCH_WORK_LEDGER,
        _ending_clause,
        _prefers_structured_arming,
        schemas,
    )
if TYPE_CHECKING:  # names the owners import from elsewhere in kiro_crew, still served here
    from kiro_crew import autonudge, session_directive  # noqa: F401
    from kiro_crew.autonudge_judge import ending_phrase, screen_phrase  # noqa: F401
    from kiro_crew.config.loader import KiroCrewConfig  # noqa: F401
    from kiro_crew.mcp_tools._limits import (  # noqa: F401
        _MONITOR_DEFAULT_MAX_CYCLES,
        _MONITOR_DEFAULT_MAX_RUNTIME_SECS,
    )
    from kiro_crew.monitoring.limits import (  # noqa: F401
        DEFAULT_RUNTIME_CEILING_SECS,
        runtime_ceiling_secs,
    )
    from kiro_crew.monitoring.models import (  # noqa: F401
        DEFAULT_MONITOR_AGENT_TURNS,
        DEFAULT_MONITOR_CADENCE_SECS,
        DEFAULT_MONITOR_PROVIDER_ERRORS,
        DEFAULT_MONITOR_RUNTIME_SECS,
        DEFAULT_MONITOR_TOKENS,
        MAX_MONITOR_AGENT_TURNS,
        MAX_MONITOR_CADENCE_SECS,
        MAX_MONITOR_CHECK_NAMES,
        MAX_MONITOR_PROVIDER_ERRORS,
        MAX_MONITOR_TOKENS,
        MAX_MONITOR_WAKE_INSTRUCTIONS_CHARS,
        MIN_MONITOR_CADENCE_SECS,
        PULL_REQUEST_SUPERSEDED_INCOMPLETE_IDENTITY,
        retained_outcome_blocks_rearm,
    )
    from kiro_crew.monitoring.registry import (  # noqa: F401
        publicly_armable_kinds,
        publicly_armable_objectives,
    )
    from kiro_crew.monitoring.targets import normalize_pull_request_target  # noqa: F401
    from kiro_crew.validation import (  # noqa: F401
        AUTONUDGE_STOP_SCHEMA,
        CHAT_TAG_SCHEMA,
        MONITOR_INSPECT_SCHEMA,
        MONITOR_START_SCHEMA,
        MONITOR_STOP_SCHEMA,
        MONITOR_UPDATE_SCHEMA,
        MONITOR_WATCH_SCHEMA,
        NOTHING_TO_DO_SCHEMA,
        RESET_CONVERSATION_SCHEMA,
        SET_PROJECT_SCHEMA,
        SUGGEST_FOLLOWUP_SCHEMA,
        validate_judge_spec,
    )
