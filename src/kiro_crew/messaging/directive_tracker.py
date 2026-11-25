"""One turn's session-directive bookkeeping, shared by every channel-side consumer.

A stateless session-bound MCP tool (``session_directive.DIRECTIVE_TOOLS``)
returns a marker instead of acting on its caller's session. Whoever reads the
turn's raw provider events applies that marker against its own session, and
the forgery gate, sub-agent isolation and single-consume rules that decide
whether a marker is honoured live here, once.

:class:`kiro_crew.messaging.driver.TurnDriver` feeds every channel turn through
one tracker. A caller that streams with ``llm_helpers.stream_and_collect``
instead (the Slack thread's plain auto-nudge wake) hands
``messaging.driver.directive_event_observer(tracker)`` to its ``on_event``
hook, so a directive applied in that turn takes effect there too. The
consumer itself is injected (``messaging.dispatch.build_directive_consumer``),
so this module never names a session, and it reads no event-kind constants:
routing a raw event by kind stays in the driver, behind the agent-backend
import boundary.

Two delivery paths, the same two the dashboard consumer has. On kiro-cli the
call carries a trusted ``_meta.kiro`` identity and the marker in the result is
applied. A backend that emits no tool name there (KAS) still has the
OUT-OF-BAND record the tool parked on the gateway: the tracker digests the
call's raw input and claims the record parked under that digest during this
turn, through the consumer's ``claim_parked`` (see
``kiro_crew.directive_queue``). The claim is injected with the consumer, so this
module still imports nothing from the dashboard.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Awaitable, Callable

from kiro_crew import session_directive
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

#: ``(kind, args) -> awaitable``; see ``messaging.driver.DirectiveConsumer``.
DirectiveConsumer = Callable[[str, dict[str, Any]], Awaitable[Any]]

#: ``(input_digest, not_before) -> record | None``: removes and returns the ONE
#: record parked for the consumer's own session under that digest no earlier
#: than *not_before*. A consumer that offers out-of-band delivery exposes it as
#: its ``claim_parked`` attribute; one that does not leaves that path off.
ParkedClaim = Callable[[str, float], "dict[str, Any] | None"]


class DirectiveTracker:
    """Record directive-tool calls and apply their results, for ONE turn.

    Build one per turn. ``terminal_applied`` turns True once an applied
    directive's consumer answered ``True`` (the structured terminal-turn
    signal, e.g. ``nothing_to_do``).
    """

    def __init__(self, consumer: DirectiveConsumer, *, audit_caller: str = "turn_driver") -> None:
        self.consumer = consumer
        self.audit_caller = audit_caller
        self.terminal_applied = False
        # tool_call_id -> canonical directive-tool name, recorded at
        # EVENT_TOOL_CALL from the trusted ``_meta.kiro`` identity and consumed
        # at the matching EVENT_TOOL_RESULT.
        self._pending: dict[str, str] = {}
        # tool_call_ids owned by a NATIVE (in-session) sub-agent, announced via
        # EVENT_SUBAGENT_ACTIVITY before the child's flat tool_call/tool_result
        # frames arrive on this same stream. A native child's directive call
        # carries a genuine core-MCP identity but has no independently bindable
        # session, so it must be REFUSED rather than applied to the parent —
        # sub-agent isolation, mirroring the dashboard consumer's
        # ``_native_tc_card`` refusal.
        self._native_tool_call_ids: set[str] = set()
        # Directive tool_call_ids this turn has ALREADY consumed. Consumption
        # pops ``_pending``, so without this a later result frame for an applied
        # directive is indistinguishable from one that never had an identity —
        # and would log the "NOT APPLIED" diagnostic for a directive that in
        # fact applied. Diagnostic-only bookkeeping: nothing reads it to
        # authorize anything.
        self._consumed: set[str] = set()
        # Identity OBSERVED on each tool_call frame, for the NOT-APPLIED
        # diagnostic only. It cannot be read off the result frame: the
        # tool_call_update path builds its event with no identity fields, so
        # they are always "" there. Nothing reads this to authorize anything —
        # the grant still comes solely from directive_tool_for at call time.
        self._seen_identity: dict[str, tuple[str, str]] = {}
        # Out-of-band delivery: the consumer's own claim on the gateway-parked
        # records for ITS session, or None when it offers none.
        self._claim_parked: ParkedClaim | None = getattr(consumer, "claim_parked", None)
        # The turn bound for that claim: the tracker is built per turn before
        # the prompt goes out, so a record parked before now belongs to an
        # earlier turn and must not be claimed.
        self._turn_started = time.monotonic()
        # tool_call_id -> input digest of a call that resolves to a directive
        # tool, the selector for the parked record. A SELECTOR, never a grant:
        # the applied payload is always the record's.
        self._digests: dict[str, str] = {}

    def record_tool_call(self, event: Any) -> None:
        """Forgery gate: record the directive-tool name ONLY from the trusted
        ``_meta.kiro`` identity — never the LLM-authored title.

        The single shared predicate (also used by the dashboard consumer)
        requires Kiro Crew's OWN core MCP server and a canonical directive-tool
        name; a shell tool (no mcp_server_name, canonical tool_name like
        "execute_bash") or a third-party server exposing a same-named tool can
        never register here.
        """
        if not event.tool_call_id:
            return
        server = getattr(event, "mcp_server_name", "") or ""
        tool_name = getattr(event, "tool_name", "") or ""
        canonical = session_directive.directive_tool_for(server, tool_name)
        if canonical:
            self._pending[event.tool_call_id] = canonical
        else:
            self._seen_identity[event.tool_call_id] = (server, tool_name)
        # The out-of-band selector, recorded the way the dashboard consumer
        # records it: trusted identity first, else the backend's own WIRE title
        # (KAS stamps ``@kirocrew-core/<tool>``). Never ``event.title``, which a
        # shell call fills from model-written text. Recorded for every
        # resolvable call, a native child's included, so a parent frame sharing
        # a child's digest can be refused below.
        if self._claim_parked is None:
            return
        resolved = session_directive.directive_tool_from_call(
            server, tool_name, getattr(event, "wire_title", "") or ""
        )
        raw = getattr(event, "raw_tool_params", None)
        if resolved and raw is not None:
            self._digests[event.tool_call_id] = session_directive.event_input_digest(
                resolved, raw, server, tool_name
            )

    def on_subagent_activity(self, event: Any) -> None:
        """Remember which tool_call_ids belong to a native child session."""
        if event.tool_call_id and getattr(event, "sub_session_id", ""):
            self._native_tool_call_ids.add(event.tool_call_id)

    async def on_tool_result(self, event: Any) -> None:
        """Decode one directive-tool result and apply it via the consumer.

        SINGLE-CONSUME: one tool call can surface more than one result frame
        (a mid-stream content frame and the final ``status=completed``
        rawOutput frame), so the mapping is popped BEFORE the consumer runs —
        a second frame must never re-apply the effect (two armed loops, a
        repeated mutation). A mid-stream frame with no decodable marker leaves
        the mapping in place for the final frame. Mirrors the dashboard
        consumer in ``chat_runner``.
        """
        pending = self._pending
        consumed = self._consumed
        tool = pending.get(event.tool_call_id or "", "")
        if not tool:
            if await self._claim_out_of_band(event):
                return
            # DIAGNOSTIC ONLY (never a grant) — mirrors the dashboard consumer.
            # A marker with no recorded identity is the right outcome for a
            # forged result AND what a backend emitting no ``_meta.kiro``
            # produces for a real directive; the gate is silent, so log enough
            # to tell those apart.
            if (event.tool_call_id or "") not in consumed and session_directive.has_marker(
                event.tool_output
            ):
                logger.warning(
                    "session-directive NOT APPLIED: marker present but the tool "
                    "call carried no core-MCP identity "
                    "(tool_call_id=%s, mcp_server_name=%r, tool_name=%r, "
                    "expected mcp_server_name=%r). Either a forged marker, or "
                    "this ACP backend does not emit _meta.kiro identity.",
                    event.tool_call_id,
                    self._seen_identity.get(event.tool_call_id or "", ("", ""))[0],
                    self._seen_identity.get(event.tool_call_id or "", ("", ""))[1],
                    session_directive.CORE_MCP_SERVER,
                )
            return
        if event.tool_call_id in self._native_tool_call_ids:
            # Sub-agent ISOLATION: a native child's tool calls surface as flat
            # events on the parent stream with a genuine core-MCP identity, but
            # the child has no independently bindable session — applying its
            # directive here would let a sub-agent arm/mutate its PARENT. Pop
            # (terminal for this call) and audit the refusal so the denial is
            # never a silent drop, mirroring the dashboard consumer.
            pending.pop(event.tool_call_id, None)
            if event.tool_call_id:
                consumed.add(event.tool_call_id)
            sel().log_api_access(
                caller=self.audit_caller,
                operation="session_directive",
                outcome="denied",
                source="messaging",
                resources=(
                    f"tool={tool} tool_call_id={event.tool_call_id} "
                    "reason=native_subagent_isolation"
                ),
            )
            logger.info(
                "session-directive %r from a native sub-agent refused "
                "(tool_call_id=%s): no session of its own to act on",
                tool,
                event.tool_call_id,
            )
            return
        output = event.tool_output or ""
        args = session_directive.decode(output, tool)
        if args is None:
            if getattr(event, "tool_final", False):
                if session_directive.is_refusal(output):
                    # The tool DECLINED and said so in its own result text (an
                    # oversized payload, a schema rejection ahead of the handler,
                    # or a session this effect can never apply to): nothing was
                    # applied and the model was told. Terminal for this call.
                    pending.pop(event.tool_call_id, None)
                    if event.tool_call_id:
                        consumed.add(event.tool_call_id)
                    logger.info(
                        "session-directive REFUSED for %r (tool_call_id=%s, "
                        "out_len=%d): the tool returned a tagged refusal instead "
                        "of a directive; nothing applied",
                        tool,
                        event.tool_call_id,
                        len(output),
                    )
                else:
                    # Authenticated directive tool, final frame, no marker:
                    # the effect is being dropped outright. Never let that be
                    # silent — and name BOTH causes that reach here now that
                    # every by-design decline is tagged: the marker was mangled
                    # in transport, or the tool raised past its own return so its
                    # decline never passed refuse_if_markerless. Asserting one
                    # cause sends an operator hunting a bug that is not there.
                    logger.warning(
                        "session-directive decode FAILED for %r (tool_call_id=%s, "
                        "out_len=%d) — effect dropped. Either the marker was lost "
                        "in transport (a marker-escaping regression) or the tool "
                        "raised past its own return, so its decline was never "
                        "tagged a refusal",
                        tool,
                        event.tool_call_id,
                        len(output),
                    )
            return
        pending.pop(event.tool_call_id, None)
        if event.tool_call_id:
            consumed.add(event.tool_call_id)
        await self._apply(tool, args)

    async def _apply(self, kind: str, args: dict[str, Any]) -> None:
        try:
            applied = await self.consumer(kind, args)
        except Exception:
            # The consumer is injected code applying a side effect; a failure
            # there must never abort the rest of the turn's stream.
            logger.warning("session-directive consumer failed for %r", kind, exc_info=True)
            return
        if applied is True:
            self.terminal_applied = True

    def _audit_denied(self, tool_call_id: str, reason: str) -> None:
        sel().log_api_access(
            caller=self.audit_caller,
            operation="session_directive",
            outcome="denied",
            source="messaging",
            resources=f"tool_call_id={tool_call_id} reason={reason}",
        )

    async def _claim_out_of_band(self, event: Any) -> bool:
        """Apply the gateway-parked record for a call with no trusted identity.

        Returns True when this frame is settled here (applied or refused), so
        the caller skips the NOT-APPLIED diagnostic. Mirrors the dashboard
        consumer (``dashboard/chat_turn/tool_results.py``): the CALL INPUT
        picks the record, the record supplies the payload, and the result body
        is never read for either.
        """
        tcid = event.tool_call_id or ""
        claim = self._claim_parked
        digest = self._digests.get(tcid, "")
        if claim is None or not digest or tcid in self._consumed:
            return False
        if not (
            getattr(event, "tool_final", False) or session_directive.has_marker(event.tool_output)
        ):
            # A mid-stream frame can precede the tool's own publish; leave the
            # claim to the final frame.
            return False
        if tcid in self._native_tool_call_ids:
            # Sub-agent ISOLATION, checked BEFORE the claim. The child's tool
            # parked its record under the PARENT's session key, so a claim here
            # would apply a child's directive to the parent. Refuse, and retire
            # exactly that record (same digest, this turn) so it cannot be
            # claimed by a later parent frame; a record the parent parked under
            # other arguments survives.
            # The child's digest is KEPT: if its frame lands first and retires
            # the parent's identical record, the parent's own frame must still
            # see the shared digest below and refuse the record that remains.
            self._consumed.add(tcid)
            claim(digest, self._turn_started)
            self._audit_denied(tcid, "native_subagent_isolation")
            logger.info(
                "session-directive from a native sub-agent refused out of band "
                "(tool_call_id=%s): no session of its own to act on",
                tcid,
            )
            return True
        if any(
            self._digests.get(other) == digest
            for other in self._native_tool_call_ids
            if other != tcid
        ):
            # PROVENANCE AMBIGUOUS: a native child's call this turn has the same
            # digest, so the parked record may be the child's. Refuse rather
            # than guess; the child's own frame retires it above.
            self._consumed.add(tcid)
            self._audit_denied(tcid, "digest_shared_with_native_subagent")
            logger.warning(
                "session-directive NOT CLAIMED (tool_call_id=%s): a native "
                "sub-agent call in this turn shares this frame's input digest, "
                "so the parked record's provenance is ambiguous.",
                tcid,
            )
            return True
        record = claim(digest, self._turn_started)
        if not record:
            return False
        # SINGLE-CONSUME: the queue removed the record under its lock, and this
        # call is marked consumed before the consumer runs.
        self._consumed.add(tcid)
        self._digests.pop(tcid, None)
        kind = str(record.get("kind") or "")
        logger.info(
            "session-directive applied OUT OF BAND (tool_call_id=%s, kind=%s): "
            "the call carried no trusted tool name; the gateway-parked record "
            "selected by its input digest was used.",
            tcid,
            kind,
        )
        await self._apply(kind, dict(record.get("args") or {}))
        return True
