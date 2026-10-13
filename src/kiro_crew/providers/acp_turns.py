"""Run an ``AcpProvider`` session's turns on its inner client.

Prompt and slash-command turns and their event translation, manual compaction and the
wait for its result, cancel and steer.

Each function here whose first parameter is ``self`` is an ``AcpProvider`` method: the
class binds it by name, so it reads and writes the session's own state on the instance,
and a patch of the method on the class or on an instance reaches every caller.

Instance fields written: ``_compact_result``.

Instance fields read: ``_client``, ``essential_delivery``.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from collections.abc import AsyncIterator
from contextlib import aclosing
from typing import TYPE_CHECKING, Any

from kiro_crew.acp.client import AcpClient, AcpError
from kiro_crew.acp.types import (
    ACP_BACKENDS_KIRO_SLASH_COMMANDS,
    EVENT_COMPACTION_STATUS,
    STOP_REASON_CANCELLED,
    STOP_REASON_END_TURN,
)
from kiro_crew.constants import COMPACT_WAIT_TIMEOUT_SECS
from kiro_crew.providers.base import CancelOutcome, LLMEvent

if TYPE_CHECKING:
    from kiro_crew.providers.acp import AcpProvider

#: The provider's own logger: this code logged under that name before the split.
logger = logging.getLogger("kiro_crew.providers.acp")


def _to_llm_event(e: Any) -> LLMEvent:
    return LLMEvent(
        kind=e.kind,
        text=e.text,
        tool_call_id=e.tool_call_id,
        title=e.title,
        wire_title=e.wire_title,
        tool_kind=e.tool_kind,
        tool_purpose=e.tool_purpose,
        context_usage_pct=e.context_usage_pct,
        stop_reason=e.stop_reason,
        refusal=e.refusal,
        synthetic_completion=e.synthetic_completion,
        request_id=e.request_id,
        options=e.options,
        tool_input=e.tool_input,
        tool_input_redacted=e.tool_input_redacted,
        tool_output=e.tool_output,
        tool_output_digest=e.tool_output_digest,
        tool_output_bytes=e.tool_output_bytes,
        # The fingerprints the credential card traces a source by. Dropping
        # them leaves every credential in the reply with no source.
        tool_output_credentials=e.tool_output_credentials,
        tool_final=e.tool_final,
        # Dropping this clears the task panel and records an empty plan/updated.
        todo=e.todo,
        # Forwarded beside `tool_final` because it is NOT derivable from it:
        # `tool_final` is true only for a completed call, so a consumer that
        # needs to know a tool FAILED has this field or nothing. Dropping it
        # here would leave every non-dashboard consumer of the provider
        # interface unable to tell a failure from a call still in progress.
        tool_status=e.tool_status,
        usage=e.usage,
        raw_tool_params=e.raw_tool_params,
        # PROVENANCE flags for the child-fidelity gate. Dropping these
        # zeroes them to their False defaults, which flips
        # child_low_fidelity to True for EVERY child permission event that
        # crosses this provider — the full-fidelity half of the feature
        # (mode-parity auto-approval, unannotated card) would be inert on
        # the primary interactive surface. Same trap for
        # mcp_identity_trusted: dropping it revokes the verified-identity
        # half (child_mcp_identity_trusted) for every crossing event.
        raw_params_trusted=e.raw_params_trusted,
        shell_classified=e.shell_classified,
        # Dropping this would let a KAS spawn reach the prompt unvetted
        # against the spawn policy on this surface.
        spawn_target=e.spawn_target,
        tool_identity_trusted=e.tool_identity_trusted,
        mcp_identity_trusted=e.mcp_identity_trusted,
        mcp_identity_unreadable=e.mcp_identity_unreadable,
        kas_builtin_ids=e.kas_builtin_ids,
        # Dropping this would refuse every deepseek read on a side turn.
        deepseek_tool_name=e.deepseek_tool_name,
        server_name=e.server_name,
        oauth_url=e.oauth_url,
        subagents=e.subagents,
        # Compaction provenance. Dropping either one zeroes it to False and
        # silently disarms a consumer guard on the PRIMARY interactive
        # surface: without ``synthesized`` the dashboard treats the terminal
        # manufactured at turn end as a mid-turn segment boundary and clears
        # the answer a backend produced after compacting, and without
        # ``control_notice`` it counts the adapter's "Compacting..." notice
        # as the turn's reply, which shadows the post-compaction
        # continuation and leaves the request unanswered.
        synthesized=e.synthesized,
        control_notice=e.control_notice,
        # The fork point a later rewind cuts the native conversation at.
        message_id=e.message_id,
        runtime_global=e.runtime_global,
        sub_session_id=e.sub_session_id,
        is_shell=e.is_shell,
        # The harness's own tool id for a permission request. Dropping it
        # leaves every KAS call unnamed, so no tool-scoped spec hook fires.
        harness_tool_id=e.harness_tool_id,
        # Dropping it leaves a claude read tool unprovable under READ_ONLY,
        # so every Side Chat lookup on claude is refused.
        harness_builtin_tool=e.harness_builtin_tool,
        # Canonical, non-model-authored tool identity (_meta.kiro). The
        # session-directive forgery gate in chat_runner keys on THESE, so
        # dropping them here silently discards every session-bound tool's
        # effect (the gate sees an empty server name and never records).
        tool_name=e.tool_name,
        mcp_server_name=e.mcp_server_name,
        diff_old_text=e.diff_old_text,
        diff_path=e.diff_path,
        # Typed execution-layer status (``kirocrew/status`` / the watchdog's
        # ``waiting_input`` classification). The consumers read it here:
        # chat_runner's tool-stall continuation takes ``wait_reason`` from
        # it before the evidence text, and the sub-agent run loop yields
        # its lane slot (``waiting_input`` WaitRecord) on it.
        status=e.status,
    )


async def stream(
    self: AcpProvider, message: str, timeout: float | None = None, *, allow_image: bool = True
) -> AsyncIterator[LLMEvent]:
    # The direct client can respawn in ensure_ready; resolve that BEFORE
    # comparing receipts so a recycled conversation receives the full text.
    if isinstance(self._client, AcpClient):
        await self._client.ensure_ready()
    send = self._client.stream_events
    if not allow_image:
        send = functools.partial(send, allow_image=False)
    if timeout is not None:
        send = functools.partial(send, timeout=timeout)
    async with aclosing(
        self.essential_delivery.stream(message, send, lambda: self.context_incarnation)
    ) as events:
        async for e in events:
            yield self._to_llm_event(e)


async def stream_command(self: AcpProvider, command: str) -> AsyncIterator[LLMEvent]:
    # _kiro.dev/commands/execute is a kiro extension, so only
    # ACP_BACKENDS_KIRO_SLASH_COMMANDS members can be sent it. Everyone else
    # routes through session/prompt, which an adapter interprets natively for
    # the commands its SDK supports (/compact, /help, /model, /context, …).
    # Commands it doesn't recognise (kiro-only ones like /agent, /experiment,
    # /hooks) flow through as conversational prompt text — a softer failure
    # mode than a -32601 "Method not found" on the whole call.
    #
    # Membership rather than "not claude": the RPC is the narrow capability
    # here, so a harness added later must opt in to it, not inherit it and
    # hard-error on every slash command a user types.
    # /compact and /clear discard native history; invalidate before dispatch
    # so the next warm turn resends the complete snapshot even when the
    # status receipt is missing or arrives late.
    self.essential_delivery.prepare_command(command)
    send = (
        self._client.stream_events
        if self._client.backend not in ACP_BACKENDS_KIRO_SLASH_COMMANDS
        else self._client.stream_command
    )
    async with aclosing(
        self.essential_delivery.stream(command, send, lambda: self.context_incarnation)
    ) as events:
        async for e in events:
            yield self._to_llm_event(e)


async def compact(self: AcpProvider, context: str = "") -> None:
    """Trigger native /compact with optional context-preserving prompt."""
    self.essential_delivery.invalidate()
    if context:
        # Truncate to avoid overwhelming the compact prompt
        prompt = context[:4000] if len(context) > 4000 else context
        message = f"/compact Preserve this session context in the summary:\n{prompt}"
    else:
        message = "/compact"
    # BOTH backends send /compact through session/prompt.
    # - claude-agent-acp does not implement _kiro.dev/commands/execute;
    #   the claude backend handles the slash command natively in-prompt.
    # - kiro-cli DOES advertise commands/execute, but its STRING form
    #   (`"command": "/compact"`) makes kiro-cli 2.14.0 exit rc=0 without
    #   a response — live-probe confirmed for /compact AND /help, while
    #   the object form (`{"command": "help", "args": {}}`, used by
    #   /effort) works. The prompt transport is the path the dashboard's
    #   manual /compact and Slack's !compact have always used: kiro ACKs
    #   the prompt (end_turn) and then emits _kiro.dev/compaction/status
    #   started/completed, which wait_for_compaction() picks up.
    # Capture a terminal status emitted MID-TURN (before end_turn) while
    # draining — otherwise it would be consumed and lost, stranding a
    # subsequent wait_for_compaction() (task_executor, wecom, cli_chat)
    # until timeout even though the compact succeeded.
    self._compact_result = None
    async for event in self._client.stream_events(message):
        if event.kind == EVENT_COMPACTION_STATUS and event.text in (
            "completed",
            "failed",
        ):
            self._compact_result = {
                "type": event.text,
                "summary": event.title or "",
            }


async def cancel(self: AcpProvider, *, wait_ack_timeout: float = 0.0) -> CancelOutcome:
    """Cancel in-flight operation via ACP session/cancel."""
    if not self._client.has_active_turn():
        logger.debug("provider.cancel: no active turn, skip")
        return "no_turn"
    self.essential_delivery.invalidate()
    try:
        # Pass the ack budget so the client's read-grace window matches how
        # long we will actually wait below — otherwise a budget above the
        # 10s floor is silently capped and the turn is torn down early.
        await self._client.cancel_session(grace_secs=wait_ack_timeout)
    except AcpError:
        logger.debug("provider.cancel: cancel_session raised AcpError", exc_info=True)
        return "error"
    if wait_ack_timeout <= 0:
        # Fire-and-forget: cancel notification sent, caller does not
        # wait for the agent's ack. Return "acked" optimistically so
        # callers that don't care about confirmation stay happy. Any
        # caller that needs a real ack MUST pass a positive timeout.
        logger.debug("provider.cancel: wait_ack_timeout=0, returning acked")
        return "acked"
    try:
        reason = await self._client.wait_turn_done(timeout=wait_ack_timeout)
        logger.debug("provider.cancel: wait_turn_done returned reason=%r", reason)
        if reason in (STOP_REASON_CANCELLED, STOP_REASON_END_TURN):
            return "acked"
        logger.debug("Unexpected stop reason after cancel: %r", reason)
        return "timeout"
    except asyncio.TimeoutError:
        logger.debug("provider.cancel: wait_turn_done timed out after %.1fs", wait_ack_timeout)
        return "timeout"


async def steer(self: AcpProvider, message: str) -> bool:
    """Delegate a mid-turn steer to the inner client (kiro-cli
    ``_session/steer``). Fire-and-forget; returns False if not steerable.

    Refused (False, so the caller queues) when the inner session can lose a
    delivered steer to a later denied approval: this wrapper is what the
    messaging channels, Side Chat and ``spawn_steer`` steer, and only the
    dashboard composer steers such a session. See
    :attr:`steer_needs_loss_recovery`."""
    if self.steer_needs_loss_recovery:
        return False
    return await self._client.steer(message)


def _inline_turn_finished_cleanly(self: AcpProvider) -> bool:
    """Whether the last turn reached its own end boundary uncancelled.

    A cancelled turn is NOT a completed compaction, and this is the arm where
    getting that wrong is most expensive: a false ``completed`` resets the
    context meter and arms the compaction cooldown, so the session stays full
    AND stops retrying. The turn reaching its own end boundary is the whole
    evidence an inline harness offers, so the absence of that boundary has to
    withhold the answer.

    The test is POSITIVE -- the stop reason must BE ``end_turn`` -- rather than
    "not cancelled". Excluding cancels alone accepts every other way a turn can
    fail to finish: a refusal, a token limit, a stop reason this build does not
    recognise, or a turn that never reported one at all. Each of those means the
    compaction did not run, and each would otherwise be reported as a success.

    The turn state itself belongs to the client, which is where the question is
    answered (``AcpClient.turn_finished_cleanly``) -- reading its private
    ``_cancelled`` / ``_last_stop_reason`` from out here would decide a
    compaction's fate from a shape this class does not own. Probed rather than
    called outright, and fails CLOSED when the probe is absent: a placeholder or
    double that cannot answer does not get the shortcut.
    """
    probe = getattr(self._client, "turn_finished_cleanly", None)
    return bool(probe()) if callable(probe) else False


async def wait_for_compaction(
    self: AcpProvider, timeout: float = COMPACT_WAIT_TIMEOUT_SECS
) -> dict:
    """Wait for compaction completed/failed after stream ends.

    Consumes the result compact() captured mid-turn if there is one
    (kiro-cli may emit the terminal status before end_turn), otherwise
    delegates to the client's queue wait (async-after-end_turn case).
    On a cached ``completed``, still grace-drains the inner client for
    kiro's post-compaction metadata so the mid-turn path reports real
    numbers too — mirrors ``AcpSessionHandle.wait_for_compaction``.
    """
    cached = getattr(self, "_compact_result", None)
    if cached is not None:
        self._compact_result = None
        if cached.get("type") == "completed":
            drain = getattr(self._client, "_drain_post_compaction_metadata", None)
            if drain is not None:
                await drain()
        return cached
    if self.capabilities.compacts_inline and self._inline_turn_finished_cleanly():
        # A member of ``ACP_BACKENDS_INLINE_COMPACTION`` finished its
        # compaction inside the ``session/prompt`` turn, and that turn is
        # over by the time anyone reaches this method. claude-agent-acp
        # compacts natively in-prompt; opencode serves ``/compact`` out of
        # its prompt handler
        # (``test/fixtures/acp_frames/opencode/compact-live.jsonl``), and goose
        # does the same (``test/fixtures/acp_frames/goose/compact-live.jsonl``).
        # None of them emits a compaction status, so the queue wait below has
        # nothing to receive and would spend the whole
        # ``COMPACT_WAIT_TIMEOUT_SECS`` proving it. pi looks the same in its own
        # source and is absent for want of a driven capture, which is recorded
        # on ``ACP_BACKENDS_COMPACT``.
        #
        # HERE rather than in ``compact()``, because this is the one method
        # BOTH routes to a compaction reach. ``compact()`` covers the
        # seventeen call sites that pair it with this wait, but the session
        # layer's autocompact drives the harness through
        # ``stream_command("/compact")`` and never calls ``compact()`` at
        # all -- so an arm placed there left the automatic path, the one no
        # user action is needed to reach, still stranding.
        #
        # No ``summary``: an inline harness reports no summary text, and an
        # invented one would be echoed to the user as the backend's own.
        return {"type": "completed", "summary": ""}
    return await self._client.wait_for_compaction(timeout)
