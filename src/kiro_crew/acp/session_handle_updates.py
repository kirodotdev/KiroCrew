"""``session/update`` frames as ``AcpEvent`` values.

Tool calls and the per-tool-call caches they keep, child-tool frames and structured
status frames.

Each function here whose first parameter is ``self`` is an ``AcpSessionHandle`` method: the
class binds it by name, so it reads and writes the session's own state on the instance,
and a patch of the method on the class or on an instance reaches every caller.

Instance fields written: ``_config_options``, ``_input_wait_emitted``,
``_native_child_tool_call_ids``, ``_prompt_or_tool_seen``, ``_stale_eligible``,
``_status_rejected``, ``_tool_dispatched``, ``_tool_output_seen``, ``last_infra_error``.

Instance fields read: ``_parked_total``, ``_runtime``, ``_session_id``,
``_tool_call_cache_overflow``, ``_tool_call_diff_path``,
``_tool_call_harness_tool_name``, ``_tool_call_identity_unreadable``,
``_tool_call_input_redacted``, ``_tool_call_inputs``, ``_tool_call_is_shell``,
``_tool_call_kind``, ``_tool_call_mcp_server``, ``_tool_call_raw_params``,
``_tool_call_tool_name``, ``_tool_tracker``, ``last_prompt_stats``.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from kiro_crew.acp._dispatch import (
    MAX_ACTIVE_TOOL_CALLS,
    liveness_key,
    parse_session_update,
    parse_usage_cost,
    parse_usage_update,
    release_tool_call_provenance,
    scoped_tool_cache_key,
)
from kiro_crew.acp.liveness import (
    INTERACTIVE_NONE,
    ToolCallState,
    boottime_now,
    classify_interactive_command,
    steady_now,
)
from kiro_crew.acp.types import (
    ACP_BACKEND_KAS,
    ACP_BACKENDS_PERMISSION_KIND_FROM_TOOL_CALL,
    ACP_BACKENDS_UNATTRIBUTED_TERMINAL_ERROR,
    EVENT_STRUCTURED_STATUS,
    EVENT_SUBAGENT_ACTIVITY,
    EVENT_TEXT_CHUNK,
    EVENT_TODO_UPDATE,
    EVENT_TOOL_CALL,
    EVENT_TOOL_RESULT,
    STATUS_EXTENSION_KEY,
    TERMINAL_TOOL_STATUSES,
    UPDATE_AGENT_MESSAGE_CHUNK,
    UPDATE_AGENT_THOUGHT_CHUNK,
    AcpEvent,
    JsonRpcMessage,
    StructuredStatus,
)
from kiro_crew.recovery.ladder import classify_infra_error
from kiro_crew.sel import sel

if TYPE_CHECKING:
    from kiro_crew.acp.session_handle import AcpSessionHandle

#: The session handle's logger: this code logged under that name before the split.
logger = logging.getLogger("kiro_crew.acp.session_handle")


def _report_evicted_attributions(self: AcpSessionHandle) -> None:
    """Say the previous turn's liveness-map overflow once, by count.

    Run at the turn reset, before the map is cleared: the one warning for
    an overflowed turn, naming the bound and how far past it the turn went
    (a map that dropped rows at its cap must not read like one that was
    never that full). A turn that evicted nothing says nothing.
    """
    count = self._tool_tracker.evicted_count
    if not count:
        return
    logger.warning(
        "tool-call liveness map evicted %d attribution(s) at its bound of %d last turn; "
        "once its retained calls settled, that turn was judged as an opaque tool in "
        "flight (the build-scale tool-stall window, never the stale probe) [session=%s]",
        count,
        MAX_ACTIVE_TOOL_CALLS,
        self._session_id,
    )


def _no_tool_in_flight(self: AcpSessionHandle) -> bool:
    """Whether the turn provably has no tool call running.

    An empty attribution map proves it only while the map has never
    evicted this turn: once the tracker's ``evicted`` is set, a call
    whose row was evicted at the cap may still be running, and reading the
    emptied map as "nothing in flight" would arm the stale clock against
    it -- the cancel-probe would then stop a live call as a wedge that did
    not exist. After an eviction the turn is treated as having a tool in
    flight until its ``clear()``: with no state to read, the oracle answers
    UNKNOWN and the tool branch keeps its full build-scale forbearance
    (``tool_stall_suspect_secs`` under the hard cap) before the non-lethal
    tool-stall recovery, the same window any opaque in-flight tool gets.
    The stale clock gives up nothing it could prove.
    """
    return not self._tool_tracker.any_active and not self._tool_tracker.evicted


def _release_settled_tool_call(self: AcpSessionHandle, update: object, scope: str) -> None:
    """Drop a settled call's provenance rows once its result frame reports a
    terminal status.

    The per-turn caches exist for the permission request, which precedes
    execution; after the result frame nothing reads them for that id, so a
    completed, failed, cancelled or refused call's rows are dead weight and
    holding them until the turn's ``clear()`` made the admission's count bound
    a bound on TURN LENGTH -- a long unattended KAS turn past 256 distinct
    calls had every later permission request refused. Released here, the
    population is the calls in flight. Called at every parse site AFTER the
    frame's own readers (the spec-disabled tripwire reads ``raw_params`` at
    the terminal), under the same origin-scoped key the frame was written
    under, so a child's terminal never releases a parent's row. A
    non-terminal status, a frame with no id, or an id no store holds is a
    no-op.
    """
    if not isinstance(update, dict) or update.get("sessionUpdate") != "tool_call_update":
        return
    # KAS only, the same membership that hands the wire the kind cache, the
    # overflow record and the unreadable mark (H13): the release exists to
    # keep the KAS-only admission's count bound a bound on calls in flight,
    # and on a first-class harness the rows are held until the turn's
    # ``clear()`` exactly as on main -- a same-id permission request that
    # followed a terminal status there (a re-ask after reject_once, a
    # mode-change re-prompt) still finds its trusted row.
    if (
        getattr(self._runtime, "acp_backend", None)
        not in ACP_BACKENDS_PERMISSION_KIND_FROM_TOOL_CALL
    ):
        return
    status = update.get("status")
    call_id = update.get("toolCallId")
    if status not in TERMINAL_TOOL_STATUSES or not isinstance(call_id, str) or not call_id:
        return
    release_tool_call_provenance(
        scoped_tool_cache_key(scope, call_id),
        self._tool_call_inputs,
        self._tool_call_input_redacted,
        self._tool_call_is_shell,
        self._tool_call_raw_params,
        self._tool_call_diff_path,
        self._tool_call_kind,
        self._tool_call_mcp_server,
        self._tool_call_tool_name,
        self._tool_call_identity_unreadable,
        self._tool_call_harness_tool_name,
    )


def _emit_tool_interrupted_sel(self: AcpSessionHandle, site: str) -> None:
    """Emit a SEL audit + WARNING when kiro-cli's security filter cancels tools.

    Mirrors AcpClient._emit_tool_interrupted_sel: a permission decision
    Kiro Crew observes but does not control (kiro-cli denied tool execution).
    Best-effort — a failed audit must not break the turn. This handle has no
    Kiro Crew session_key, so the ACP sessionId is recorded in metadata for
    correlation.
    """
    logger.warning("kiro-cli cancelled tool use(s) [site=%s session=%s]", site, self._session_id)
    try:
        sel().log_tool_invocation(
            session_key="",
            source="acp",
            tool_name="kiro_cli_security_filter",
            tool_kind="client_built_in",
            outcome="denied",
            metadata={
                "site": site,
                "reason": "tool_interrupted_marker",
                "session_id": self._session_id,
            },
        )
    except Exception:
        logger.warning("SEL audit failed for tool_interrupted at %s", site, exc_info=True)


def _tool_kind_cache_for_wire(self: AcpSessionHandle) -> dict[str, str] | None:
    """The ``tool_kind_cache`` the wire parsers may use for this harness, or None.

    Members of ``ACP_BACKENDS_PERMISSION_KIND_FROM_TOOL_CALL`` (KAS) carry a
    ``kind`` on the tool_call and none on ``session/request_permission``, so
    the cache lets the permission event keep the harness's WRITE-PLANE
    classification (``edit``/``delete``; the writer in ``_dispatch`` retains
    no other kind, so the store is bounded to that closed vocabulary). Every
    other harness gets ``None``, which disables both the writer and the
    reader -- kiro-cli's permission frames omit ``kind``
    too, but it routes edits by the diff content block already and its
    kindless governance classification is additive, so a carried ``edit``
    would change what its gate sees. Decided per frame by membership, the
    same way ``_handle_update`` gates KAS-only discriminants, so no
    harness's construction path changes (H13).
    ``getattr``: a minimal runtime double carries no backend, and no backend
    is not a member.
    """
    backend = getattr(self._runtime, "acp_backend", None)
    if backend in ACP_BACKENDS_PERMISSION_KIND_FROM_TOOL_CALL:
        return self._tool_call_kind
    return None


def _identity_unreadable_cache_for_wire(self: AcpSessionHandle) -> dict[str, bool] | None:
    """The ``identity_unreadable_cache`` the wire parsers may write, or ``None``.

    Handed only for members of ``ACP_BACKENDS_PERMISSION_KIND_FROM_TOOL_CALL``
    (KAS), the same membership and per-frame shape as the kind cache and the
    overflow record: the unreadable mark exists for the KAS identity channel
    (the one whose halves are bounded at retention) and earns a refusal
    (``_refuse_unreadable_mcp_identity``) that is a KAS refusal mode. Every
    other harness is handed ``None``, so its frames are never marked and the
    refusal never fires on a first-class path (H13).
    """
    backend = getattr(self._runtime, "acp_backend", None)
    if backend in ACP_BACKENDS_PERMISSION_KIND_FROM_TOOL_CALL:
        return self._tool_call_identity_unreadable
    return None


def _tool_call_cache_overflow_for_wire(self: AcpSessionHandle) -> set[str] | None:
    """The overflow record the wire parsers write, or ``None`` for a harness
    the cache admission bound does not govern.

    Handing the record is what opts a harness into ``admit_tool_call_cache_entry``
    (``_dispatch``): the bound, the eviction and the refusal of an unretained
    permission request are the KAS projection's -- KAS is the harness with a
    mountable ``delete_file`` whose write-plane routing the caches carry, and
    the one whose frames this branch measured. Every other harness gets
    ``None`` and retains as it always did, so no first-class path gains a
    refusal mode or changes construction (H13). Same membership set and
    per-frame shape as ``_tool_kind_cache_for_wire``. The attribute itself
    is built for every harness; only what the wire is handed differs.
    """
    backend = getattr(self._runtime, "acp_backend", None)
    if backend in ACP_BACKENDS_PERMISSION_KIND_FROM_TOOL_CALL:
        return self._tool_call_cache_overflow
    return None


def _handle_update(self: AcpSessionHandle, msg: JsonRpcMessage) -> list[AcpEvent]:
    """Process a session/update notification and return events."""
    params = msg.params or {}
    update = params.get("update") or {}
    if not isinstance(update, dict):
        return []

    # A frame the runtime routed here for a backend-internal subagent
    # carries the CHILD's sessionId. Its tool_call/refinement updates are
    # parsed with the SAME shared parser and the SAME per-toolCallId
    # caches as this handle's own tool calls — that is what gives a later
    # child permission request real command bytes (tool_input, is_shell,
    # raw params), so the policy gates evaluate it with main-agent
    # fidelity instead of the LLM-authored title. The parsed events are
    # re-tagged as subagent activity (crew monitor), NOT emitted as this
    # session's own transcript events — a child's text chunks and tool
    # cards must not render as parent output.
    frame_sid = str(params.get("sessionId") or "")
    if frame_sid and frame_sid != self._session_id:
        # Native-subtask residency seam (RFC §14.8): a child-routed frame is
        # the only per-child identity the harness exposes. Counted here so
        # the recovery boundary — cancel/recover the PARENT session, which
        # takes its N native children with it — has a number, and so a
        # display never implies the scheduler can pause one child alone.
        # No HostBudget charge: a native child lives INSIDE the parent's
        # runtime process, whose residency is already charged.
        self._note_native_child(frame_sid)
        # A ``kirocrew/status`` riding a CHILD-routed frame is not this
        # session's status: the native sub-agent has no task row of its own
        # (its recovery boundary is the parent session), so its wait must
        # not be read as the parent's. Rejected, never re-attributed.
        self._note_status_rejected(params, update, "child_origin")
        child_events = parse_session_update(
            update,
            tool_input_cache=self._tool_call_inputs,
            tool_input_redacted_cache=self._tool_call_input_redacted,
            shell_cache=self._tool_call_is_shell,
            raw_params_cache=self._tool_call_raw_params,
            diff_path_cache=self._tool_call_diff_path,
            tool_kind_cache=self._tool_kind_cache_for_wire(),
            mcp_server_name_cache=self._tool_call_mcp_server,
            tool_name_cache=self._tool_call_tool_name,
            harness_tool_name_cache=self._tool_call_harness_tool_name,
            identity_unreadable_cache=self._identity_unreadable_cache_for_wire(),
            cache_scope=frame_sid,
            cache_overflow=self._tool_call_cache_overflow_for_wire(),
        )
        # A child's rows share the parent's stores and the admission's
        # population; released here under the child's own scope, since
        # this arm re-tags only the call and text events and the terminal
        # result never reaches the main route's release. Nothing below
        # reads the settled row: the events out are coarse activity.
        self._release_settled_tool_call(update, frame_sid)
        out: list[AcpEvent] = []
        for ev in child_events:
            if ev.kind == EVENT_TOOL_CALL and ev.tool_call_id:
                # A child's tool call is this session's side effect for
                # replay purposes: the parent prompt spawned it, so a replay
                # would re-run it. Close the registration-throttle window.
                self._prompt_or_tool_seen = True
                out.append(
                    AcpEvent(
                        kind=EVENT_SUBAGENT_ACTIVITY,
                        sub_session_id=frame_sid,
                        tool_call_id=ev.tool_call_id,
                        title=ev.title,
                    )
                )
            elif ev.kind == EVENT_TEXT_CHUNK and ev.text:
                # Same closure as the tool arm: observed child output means
                # the wave did work, so the zero-activity verdict is gone.
                self._prompt_or_tool_seen = True
                out.append(
                    AcpEvent(
                        kind=EVENT_SUBAGENT_ACTIVITY,
                        sub_session_id=frame_sid,
                        text=ev.text,
                    )
                )
            # Thinking chunks, tool results, and refinements update the
            # caches above but emit nothing: the crew monitor only shows
            # coarse activity, and the caches are the security payload.
        return out

    session_update = update.get("sessionUpdate", "")

    # usage_update updates context stats only — it is not an AcpEvent.
    # parse_usage_update reconciles the flat (AcpClient) and nested shapes.
    if session_update == "usage_update":
        used, size = parse_usage_update(update)
        if used is not None and size:
            try:
                if size > 0:
                    self.last_prompt_stats.context_pct = round((used / size) * 100, 1)
                    self.last_prompt_stats.context_used_tokens = int(used)
                    self.last_prompt_stats.context_window_tokens = int(size)
                    # Mark authoritative so metadata pct cannot clobber it.
                    self.last_prompt_stats.context_tokens_from_usage = True
                    self.last_prompt_stats.note_pct_reported()
            except (TypeError, ValueError, ZeroDivisionError):
                pass
        # Session-cumulative billing cost (claude seam); kiro never sends
        # the key so this is None on the kiro path. Delta'd per turn on
        # the stats object (monotonic guard lives there).
        cost = parse_usage_cost(update)
        if cost is not None:
            self.last_prompt_stats.apply_cost_cumulative(cost)
        return []

    # config_option_update: ACP pushes updated configOptions (e.g. after
    # model switch rebuilds effort options). State update, no event emitted.
    if session_update == "config_option_update":
        config_options = update.get("configOptions")
        if isinstance(config_options, list):
            self._config_options = config_options
            self._sync_effort_levels()
        return []

    # KAS folds signals that kiro-cli sends as separate top-level
    # ``_kiro.dev/*`` methods (agent switch, per-turn metadata, compaction
    # status) into ``session/update`` discriminants instead. kiro-cli never
    # emits these discriminants (verified against its source), so this
    # KAS-gated branch restores the same displays without touching the kiro
    # path — a positive ``== ACP_BACKEND_KAS`` check for harness parity (H5).
    # Returns None only for a non-KAS-specific discriminant, so ordinary
    # chunk/tool frames still fall through to the shared parser below.
    if self._runtime.acp_backend == ACP_BACKEND_KAS:
        kas_events = self._handle_kas_update(session_update, update)
        if kas_events is not None:
            return kas_events

    # KAS sub-agent progress: tool_call/tool_call_update frames carrying
    # _meta.kiro.agentSubtaskId or _meta.kiro.pipeline are sub-agent
    # lifecycle frames — intercept PARENT frames and route to the native
    # sub-agent WS path (EVENT_SUBAGENT_LIST) instead of rendering them as
    # ordinary tool calls. CHILD nested tool frames (agentSubtaskId present,
    # but not a parent) emit an activity prefix AND fall through to the
    # shared parser (caches populate + tool events render).
    # Positive KAS gate (H5).
    if self._runtime.acp_backend == ACP_BACKEND_KAS:
        if session_update in ("tool_call", "tool_call_update"):
            kas_sub_events = self._handle_kas_subagent(update)
            if kas_sub_events is not None:
                return kas_sub_events
            _child_prefix = self._build_child_tool_activity_prefix(update)
            if _child_prefix:
                child_tool_call_id = _child_prefix[0].tool_call_id
                self._native_child_tool_call_ids.add(
                    scoped_tool_cache_key(self._session_id, child_tool_call_id)
                )
                # Child nested tool: run the shared parser for its cache
                # SIDE EFFECTS ONLY — the trusted _tool_call_is_shell signal
                # + redacted input that a later permission/result reads — but
                # return ONLY the activity. Surfacing the tool events would
                # render the child tool as a top-level tool; the sub-agent
                # card is fed by the roster + activity, not the raw frame.
                parse_session_update(
                    update,
                    tool_input_cache=self._tool_call_inputs,
                    tool_input_redacted_cache=self._tool_call_input_redacted,
                    shell_cache=self._tool_call_is_shell,
                    raw_params_cache=self._tool_call_raw_params,
                    diff_path_cache=self._tool_call_diff_path,
                    tool_kind_cache=self._tool_kind_cache_for_wire(),
                    mcp_server_name_cache=self._tool_call_mcp_server,
                    tool_name_cache=self._tool_call_tool_name,
                    harness_tool_name_cache=self._tool_call_harness_tool_name,
                    identity_unreadable_cache=self._identity_unreadable_cache_for_wire(),
                    cache_scope=self._session_id,
                    cache_overflow=self._tool_call_cache_overflow_for_wire(),
                )
                self._release_settled_tool_call(update, self._session_id)
                return _child_prefix
        if session_update == "agent_message_chunk":
            kas_chunk_events = self._handle_kas_subagent_chunk(update)
            if kas_chunk_events is not None:
                return kas_chunk_events

    # All other session/update kinds go through the single shared parser so
    # AcpRuntime and AcpClient cannot drift on frame shape or redaction. The
    # parser writes redacted tool inputs into our caller-owned cache AND the
    # trusted shell signal into _tool_call_is_shell (which the permission
    # event later reads); we then derive per-session stale/stall bookkeeping
    # from the returned events.
    events = parse_session_update(
        update,
        tool_input_cache=self._tool_call_inputs,
        tool_input_redacted_cache=self._tool_call_input_redacted,
        shell_cache=self._tool_call_is_shell,
        raw_params_cache=self._tool_call_raw_params,
        diff_path_cache=self._tool_call_diff_path,
        tool_kind_cache=self._tool_kind_cache_for_wire(),
        mcp_server_name_cache=self._tool_call_mcp_server,
        tool_name_cache=self._tool_call_tool_name,
        harness_tool_name_cache=self._tool_call_harness_tool_name,
        identity_unreadable_cache=self._identity_unreadable_cache_for_wire(),
        cache_scope=self._session_id,
        cache_overflow=self._tool_call_cache_overflow_for_wire(),
    )
    if (
        session_update == "agent_message_chunk"
        and not update.get("messageId")
        and self._runtime.acp_backend in ACP_BACKENDS_UNATTRIBUTED_TERMINAL_ERROR
    ):
        # Provenance for the terminal-failure hold in ``_run_turn``. Read off
        # the raw frame here rather than in the shared parser, so the parser's
        # events (and the captured frame corpus) stay the same for every
        # harness that leaves the id off its model text.
        for ev in events:
            if ev.kind == EVENT_TEXT_CHUNK:
                ev.unattributed = True
    filtered_events: list[AcpEvent] = []
    for ev in events:
        if ev.kind == EVENT_TODO_UPDATE and ev.tool_call_id:
            # Ownership is a standing fact about the call id, not a token to
            # spend: one tool_call can be followed by SEVERAL
            # tool_call_update frames (the captured corpus in
            # test/fixtures/acp_frames/kiro/session.jsonl has two for one
            # call), so consuming the entry on the first snapshot would let
            # a second result frame for the same child call through. The
            # per-turn clear beside the sibling per-call caches is what
            # bounds the set.
            if scoped_tool_cache_key(self._session_id, ev.tool_call_id) in (
                self._native_child_tool_call_ids
            ):
                continue
        filtered_events.append(ev)
        if ev.kind == EVENT_TEXT_CHUNK:
            self.last_prompt_stats.text_chunks += 1
            self._stale_eligible = self._no_tool_in_flight()
            self._prompt_or_tool_seen = True
        elif ev.kind == EVENT_TOOL_CALL:
            self._stale_eligible = False
            self._tool_dispatched = True
            self._prompt_or_tool_seen = True
            # The L1 verdict describes the LAST tool result, so a newly
            # dispatched call retires the previous one's. Clearing here and
            # not only on the next result is what covers a call that
            # completes with NO output: _build_tool_result_event returns None
            # for an output-less update, so no EVENT_TOOL_RESULT arrives to
            # overwrite the verdict, and the turn's consumer would re-issue a
            # call whose refusal an intervening call had already superseded.
            self.last_infra_error = None
            # Pre-dispatch interactive classification (RFC §14.6): a TABLE
            # verdict on the trusted shell command (never the LLM-authored
            # title), carried on the oracle's tool state so the window
            # policy and the post-stall classifier read the same value.
            # Non-shell tools are never interactive-risk.
            interactive = (
                classify_interactive_command(ev.shell_command or ev.tool_input)
                if ev.is_shell
                else None
            )
            self._input_wait_emitted = False
            if interactive is not None and interactive.risk != INTERACTIVE_NONE:
                logger.info(
                    "shell tool on session %s classified interactive-risk=%s (%s): %s",
                    self._session_id,
                    interactive.risk,
                    interactive.program,
                    interactive.reason,
                )
            # Attribution snapshot for the liveness oracle: title + the
            # already-redacted input + dispatch time on BOTH clocks (monotonic
            # for elapsed spans, boot for dating a child process against this
            # dispatch) + the parking this turn has banked so far, which
            # bounds how far this stamp can lag the runtime's actual spawn
            # (the park is banked when the consumer returns, i.e. before this
            # frame is processed, so it is complete here) + the trusted shell
            # flag. A new dispatch retires the oracle so its tracked child
            # and counter samples never bleed across tools — including from a
            # walk still running against the previous tool's command.
            inflight_tool = ToolCallState(
                title=ev.title,
                command=ev.tool_input,
                dispatch_ts=time.monotonic(),
                dispatch_boot_ts=boottime_now(),
                dispatch_steady_ts=steady_now(),
                dispatch_parked_secs=self._parked_total,
                is_shell=ev.is_shell,
                tool_name=ev.tool_name,
                # Only a provenance-verified identity names the server, so
                # an unverified frame cannot select the wait contract.
                mcp_server_name=(ev.mcp_server_name if ev.mcp_identity_trusted else ""),
                interactive_risk=(interactive.risk if interactive else INTERACTIVE_NONE),
            )
            # The shared hand-off rule stores the call by id and makes it the
            # judged one (the interactive classification rides as the aux
            # payload, read back via ``_inflight_interactive``). A dispatch
            # always changes the judged call, so retire the oracle.
            # At the cap the oldest attribution is evicted and counted; the
            # turn's overflow is said once, by count, at the next reset
            # (``_report_evicted_attributions``).
            self._tool_tracker.dispatch(
                liveness_key(ev.tool_call_id or ""), inflight_tool, interactive
            )
            self._retire_liveness_state()
        elif ev.kind == EVENT_TOOL_RESULT:
            if not ev.tool_final and ev.tool_call_id:
                # Streamed partial output: the command has acted, so any
                # later non-interactive retry of it is not a safe replay.
                # Bounded like the map, and by it: the key is the liveness
                # key, only a call the map still holds is admitted (an id
                # matters here only while its call is in flight, and an
                # evicted or unknown id must not grow a sibling store past
                # the shared cap), and at the cap the set is pruned to the
                # calls still in flight.
                _seen_key = liveness_key(ev.tool_call_id)
                if _seen_key and _seen_key in self._active_tool_calls:
                    if len(self._tool_output_seen) >= MAX_ACTIVE_TOOL_CALLS:
                        self._tool_output_seen &= set(self._active_tool_calls)
                    self._tool_output_seen.add(_seen_key)
            if ev.tool_status in TERMINAL_TOOL_STATUSES:
                # The shared hand-off rule drops the finished call and, when
                # it was the judged one, re-points to the newest remaining
                # dispatch (or clears). It returns whether the judged call
                # changed — only then is the oracle retired and the input
                # wait reset.
                judged_changed = self._tool_tracker.result(
                    liveness_key(ev.tool_call_id or ""), terminal=True
                )
                self._tool_dispatched = not self._no_tool_in_flight()
                self._stale_eligible = self._no_tool_in_flight()
                if judged_changed:
                    self._input_wait_emitted = False
                    self._retire_liveness_state()
            # L1 of the recovery ladder: classify the result text ONCE, at
            # the layer that owns the protocol. A ``-32001 capacity``
            # refusal from the MCP stub or a gateway ``recoverable_infra``
            # marker is recorded here (with the server's retry hint) and
            # read by the turn's consumer at end of turn; every other
            # result -- including one that merely quotes a marker inside a
            # long document -- leaves it None. Never a security input.
            self.last_infra_error = classify_infra_error(ev.tool_output)
    events = filtered_events
    compaction_event = self._codex_compaction_event(update)
    if compaction_event is not None:
        # APPENDED, not substituted, and appended to the FILTERED list so the
        # status rides with the rows the frame actually surfaced. The frame is
        # also a real tool call this turn made, and the transcript row for it
        # is one the user watched appear -- dropping it to surface the status
        # instead would delete a row to report the thing the row was
        # reporting.
        events.append(compaction_event)
    status_event = self._structured_status_event(msg, params, update)
    if status_event is not None:
        events.append(status_event)
    return events


# ── Structured status protocol (``kirocrew/status``, version 1) ──


def _structured_status_event(
    self: AcpSessionHandle, msg: JsonRpcMessage, params: dict[str, Any], update: dict[str, Any]
) -> AcpEvent | None:
    """The ``EVENT_STRUCTURED_STATUS`` a routed frame carries, or None.

    The ORIGIN RULE (RFC §14.5) — a status is trusted only from the
    execution layer of the session it names:

    1. the frame was ROUTED to this session (``msg.fanout_no_owner`` is
       False — an ownerless frame fanned out to several sessions names no
       owner, so its status is nobody's);
    2. the frame's ``sessionId`` is this handle's (a child-routed frame is
       rejected in ``_handle_update`` before reaching here);
    3. the frame is not a MODEL-TEXT frame (``agent_message_chunk`` /
       ``agent_thought_chunk``): a wait can never be created by prose, and
       a harness has no reason to attach status to a text chunk;
    4. the extension names this session (``session_id`` empty or equal);
    5. shape + ``version == 1`` (:meth:`StructuredStatus.from_meta`).

    The extension is read from ``params._meta`` (notification level, where
    MCP carries its ``progressToken``) and, failing that, ``update._meta``
    (where kiro-cli carries ``_meta.kiro``). Every rejection is counted
    under its reason and logged once per reason per turn as
    ``status_rejected``; an absent extension is the ordinary case and is
    silent.
    """
    meta = params.get("_meta")
    if not (isinstance(meta, dict) and STATUS_EXTENSION_KEY in meta):
        meta = update.get("_meta")
    if not (isinstance(meta, dict) and STATUS_EXTENSION_KEY in meta):
        return None
    if msg.fanout_no_owner:
        self._note_status_rejected(params, update, "fanout_no_owner")
        return None
    if update.get("sessionUpdate") in (UPDATE_AGENT_MESSAGE_CHUNK, UPDATE_AGENT_THOUGHT_CHUNK):
        self._note_status_rejected(params, update, "model_text_frame")
        return None
    status, reason = StructuredStatus.from_meta(meta)
    if status is None:
        self._note_status_rejected(params, update, reason)
        return None
    if status.session_id and status.session_id != self._session_id:
        self._note_status_rejected(params, update, "session_mismatch")
        return None
    return AcpEvent(
        kind=EVENT_STRUCTURED_STATUS,
        tool_call_id=status.tool_call_id,
        status=status,
    )


def _note_status_rejected(
    self: AcpSessionHandle, params: dict[str, Any], update: dict[str, Any], reason: str
) -> None:
    """Count (and log once per reason per turn) a rejected status frame.

    Only called when the frame actually carried the extension key; the
    child-origin caller checks that itself so an ordinary child frame does
    not count as a rejection.
    """
    meta = params.get("_meta")
    if not (isinstance(meta, dict) and STATUS_EXTENSION_KEY in meta):
        meta = update.get("_meta") if isinstance(update, dict) else None
        if not (isinstance(meta, dict) and STATUS_EXTENSION_KEY in meta):
            return
    count = self._status_rejected.get(reason, 0) + 1
    self._status_rejected[reason] = count
    if count == 1:
        logger.warning(
            "status_rejected on session %s: kirocrew/status frame ignored (%s)",
            self._session_id,
            reason,
        )
