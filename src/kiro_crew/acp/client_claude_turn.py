"""What a claude-agent-acp prompt turn reads that no other backend's does.

Its compaction notices, which arrive as plain assistant text and, for an automatic
compaction, with no terminal; a native ``compaction_update`` it claims; the
``_session/steering`` floor of its adapter; a turn whose only unfinished calls are
background agents'; and the debug line for a session update this client does not
handle. ``AcpClient`` reaches each through a hook whose default is what every other
backend does, and ``ClaudeAcpClient`` binds the functions here.

Each function here whose first parameter is ``self`` is a ``ClaudeAcpClient`` method:
the class binds it by name, so it reads and writes the session's own state on the
instance, and a patch of the method on the class or on an instance reaches every caller.

Instance fields written: ``_claude_compaction_pending``, ``_claude_native_compaction_id``,
``_compaction_failed_at``, ``last_compaction_transient``.

Instance fields read: ``_agent_version``, ``_native_compaction_states``,
``last_prompt_stats``.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from kiro_crew.acp import claude_background_calls
from kiro_crew.acp._dispatch import (
    NATIVE_COMPACTION_CANCELLED,
    liveness_key,
    parse_claude_compaction_notice,
    redact_text,
)
from kiro_crew.acp.client_claude_session import (
    CLAUDE_ACP_STEERING_MIN_VERSION,
    _adapter_version_at_least,
)
from kiro_crew.acp.transport_errors import compaction_failure_is_transient
from kiro_crew.acp.types import (
    EVENT_COMPACTION_STATUS,
    STOP_REASON_END_TURN,
    AcpEvent,
    JsonRpcMessage,
)

if TYPE_CHECKING:
    from kiro_crew.acp.client import ClaudeAcpClient

#: The client's own logger: this code logged under that name before the split.
logger = logging.getLogger("kiro_crew.acp.client")


def _claude_compaction_event(self: ClaudeAcpClient, chunk: str) -> AcpEvent | None:
    """Reclassify a claude-agent-acp compaction notice chunk as an event.

    The Claude adapter reports compaction as plain assistant text rather
    than an out-of-band notification, so this is the claude-side twin of
    ``_handle_compaction_status``: it applies the same state mutations
    (arm/disarm the post-failure budget, drop the stale context counts) and
    returns the EVENT_COMPACTION_STATUS every consumer already understands.
    ``None`` means the chunk is ordinary assistant text and must be yielded
    as-is.

    Backend-gated by its class: the markers are anchored and kiro-cli has no
    reason to emit them, but only the Claude adapter is a KNOWN producer, so only
    ``ClaudeAcpClient`` reads them; every other backend's
    ``_harness_compaction_event`` answers None and no other backend's prose can
    be reinterpreted as a control frame here.

    Callers MUST still forward the text. The event is a SIDE EFFECT, never
    a substitute for the chunk: the adapter ships these notices as ordinary
    assistant text with no marker of any kind, so recognizing one is a guess
    about prose, and a layer that swallowed the chunk would turn any wrong
    guess into deleted model output. A caller that yields structured events
    marks the forwarded chunk ``control_notice`` instead, so a consumer can
    show the text without counting it as the turn's own answer.

    A terminal is only accepted while a compaction is actually in flight.
    ``Compacting completed.`` standing alone is just prose — a user can ask
    for exactly that reply — and swallowing it would delete the answer AND
    reset the context counters against a window nobody summarized.  The
    ``started`` arm cannot be gated the same way: it is what arms the flag.
    """
    parsed = parse_claude_compaction_notice(chunk)
    if parsed is None:
        return None
    status_type, detail = parsed
    if status_type != "started" and not self._claude_compaction_pending:
        return None
    logger.info("Compaction status (claude): %s", status_type)
    self._claude_compaction_pending = status_type == "started"
    if status_type == "started":
        # An automatic compaction announced by this notice sends no terminal,
        # and only a turn that ends naturally settles it. Forget the pictures now,
        # so one that is then cut off or never settled costs one more copy of a
        # picture, never a lost one.
        self._forget_sent_images()
    if status_type == "completed":
        self._compaction_failed_at = None
        self.last_prompt_stats.reset_after_compaction()
        self._forget_sent_images()
    elif status_type == "failed":
        logger.warning("Compaction failed (claude): %s", detail or "no reason reported")
        # Arm the bounded post-failure wait, exactly as the kiro-cli path
        # does: the backend may never answer the prompt this compaction
        # was for.
        self._compaction_failed_at = time.monotonic()
        # The adapter ships prose, not a payload, so the parsed notice text
        # IS the whole reason — wrap it in the shape the classifier reads
        # rather than teaching it a second input type. ``reason`` is a
        # reason-bearing key, so the scan sees it.
        self.last_compaction_transient = compaction_failure_is_transient({"reason": detail or ""})
    # Backend-echoed text on its way to the dashboard — redact before it can
    # reach any surface (parity with the kiro-cli/KAS compaction summaries).
    return AcpEvent(kind=EVENT_COMPACTION_STATUS, text=status_type, title=redact_text(detail))


def _settle_claude_compaction(self: ClaudeAcpClient, reason: str) -> AcpEvent | None:
    """Synthesize the terminal an AUTOMATIC claude compaction never sends.

    The adapter emits ``Compacting completed.`` only from the SDK's
    ``compact_result`` status, which it documents as the manual ``/compact``
    signal; an automatic mid-turn compaction takes its ``compact_boundary``
    case instead and emits only a ``usage_update``.  So the turn ends with a
    dangling ``started``.  Called at the turn's terminal, this closes it out
    so consumers leave their compacting state and the context meter resets.
    A native start without a terminal keeps this same fallback; settling its
    ID also prevents a delayed native terminal from resetting counts again.

    Reporting ``completed`` is a statement about what the backend did, not a
    guess — but ONLY for *reason* ``end_turn``.  The turn having reached its
    own natural terminal is the whole evidence that the compaction finished:
    a compaction that had FAILED would have said so (the adapter emits its
    failure text from the same status handler that emits the success text).
    A turn that ends any other way — cancelled by the user's Stop, refused,
    or cut off on a limit — carries no such evidence, so it settles the
    pending flag WITHOUT claiming success: no ``completed`` event, no
    context-counter reset, and no clearing of a recorded failure.  Otherwise
    pressing Stop mid-compaction would fabricate a successful compaction and
    reset the meter against a context that was never actually summarized.
    """
    if not self._claude_compaction_pending:
        return None
    self._claude_compaction_pending = False
    native_id = getattr(self, "_claude_native_compaction_id", None)
    if native_id is not None:
        self._native_compaction_states._record_key(
            native_id,
            "completed" if reason == STOP_REASON_END_TURN else NATIVE_COMPACTION_CANCELLED,
        )
        self._claude_native_compaction_id = None
    if reason != STOP_REASON_END_TURN:
        logger.info(
            "Compaction status (claude): pending compaction abandoned, turn ended %r",
            reason or "unknown",
        )
        return None
    logger.info("Compaction status (claude): completed (synthesized at turn end)")
    self._compaction_failed_at = None
    self.last_prompt_stats.reset_after_compaction()
    self._forget_sent_images()
    # ``synthesized`` marks this terminal as manufactured at the turn's end
    # rather than observed mid-turn. It arrives AFTER every text chunk of the
    # turn, so a consumer that treats a compaction terminal as a segment
    # boundary would discard the answer a backend produced after compacting.
    return AcpEvent(kind=EVENT_COMPACTION_STATUS, text="completed", title="", synthesized=True)


def _harness_compaction_event(self: ClaudeAcpClient, chunk: str) -> AcpEvent | None:
    """The compaction event a claude text chunk carries, or None (:meth:`_claude_compaction_event`)."""
    return self._claude_compaction_event(chunk)


def _settle_harness_compaction(self: ClaudeAcpClient, reason: str) -> AcpEvent | None:
    """Close out a claude compaction the turn left pending (:meth:`_settle_claude_compaction`)."""
    return self._settle_claude_compaction(reason)


def _reset_harness_compaction(self: ClaudeAcpClient) -> None:
    """Forget the last turn's pending claude compaction as a new prompt starts."""
    self._claude_compaction_pending = False
    self._claude_native_compaction_id = None


def _claims_native_compaction(
    self: ClaudeAcpClient, status_type: str, update: dict[str, Any]
) -> bool:
    """Record a native ``compaction_update`` as this session's pending compaction.

    Always claimed on claude: a native start without a terminal is settled at the
    turn's end by :meth:`_settle_claude_compaction`, which also records the ID so a
    delayed native terminal cannot reset the counts again.
    """
    self._claude_compaction_pending = status_type == "started"
    self._claude_native_compaction_id = (
        liveness_key(update["compactionId"]) if self._claude_compaction_pending else None
    )
    return True


def _note_unhandled_session_update(self: ClaudeAcpClient, kind: str) -> None:
    """Log, at debug, a ``session/update`` kind this client does not handle."""
    logger.debug("Unhandled session update type: %s", kind)


def _steering_adapter_floor_met(self: ClaudeAcpClient) -> bool:
    """Whether this claude-agent-acp is at or above :data:`CLAUDE_ACP_STEERING_MIN_VERSION`,
    the release that takes ``_session/steering`` as this client sends it."""
    return _adapter_version_at_least(
        getattr(self, "_agent_version", ""), CLAUDE_ACP_STEERING_MIN_VERSION
    )


async def _settle_background_only(
    self: ClaudeAcpClient, msg: JsonRpcMessage
) -> JsonRpcMessage | None:
    """An ``end_turn`` answer for a prompt error that names only background agents'
    calls, or None to keep the error (:mod:`kiro_crew.acp.claude_background_calls`)."""
    return await claude_background_calls.settle_background_only(self, msg)
