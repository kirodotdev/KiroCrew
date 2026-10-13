"""Turn an ``AcpClient`` session's tool-call frames into events, and audit them.

Text chunk, tool_call, update and refinement extraction with the per-call provenance
caches the permission path reads, the ACP-layer SEL audit record, the Pre/PostToolUse
hook fires, and the tool results read back from kiro-cli's session JSONL.

Each function here whose first parameter is ``self`` is an ``AcpClient`` method: the
class binds it by name, so it reads and writes the session's own state on the instance,
and a patch of the method on the class or on an instance reaches every caller.

Instance fields written: ``_jsonl_pos``, ``_stale_eligible``, ``_tool_call_diff_path``,
``_tool_call_identity_unreadable``, ``_tool_call_input_redacted``,
``_tool_call_inputs``, ``_tool_call_is_shell``, ``_tool_call_mcp_server``,
``_tool_call_params``, ``_tool_call_tool_name``, ``_tool_dispatched``.

Instance fields read: ``_active_tool_calls``, ``_agent``, ``_audit_source``,
``_observed_tool_calls``, ``_session_id``, ``_session_key``,
``_tool_call_harness_tool_name``, ``_tool_call_unclassified``, ``last_prompt_stats``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING

from kiro_crew.acp._dispatch import (
    _dumps_degraded,
    _measure_tool_output,
    classify_tool_call,
    derive_edit_diff,
    extract_tool_purpose,
    harness_tool_name,
    log_unrenderable_content,
    make_unified_diff,
    redact_text,
)
from kiro_crew.acp._dispatch import select_tool_title as _select_tool_title
from kiro_crew.acp._dispatch import (
    tool_call_content_text,
)
from kiro_crew.acp.client_timeouts import _SEL_AUDIT_TIMEOUT_SECONDS
from kiro_crew.acp.types import (
    ACP_BACKEND_DEEPSEEK,
    EVENT_TOOL_CALL,
    EVENT_TOOL_CALL_UPDATE,
    EVENT_TOOL_RESULT,
    TERMINAL_TOOL_STATUSES,
    UPDATE_AGENT_MESSAGE_CHUNK,
    UPDATE_AGENT_THOUGHT_CHUNK,
    UPDATE_TOOL_CALL,
    AcpEvent,
    JsonRpcMessage,
)
from kiro_crew.agent_sdk.backends import ACP_BACKENDS_PERMISSION_KIND_FROM_TOOL_CALL
from kiro_crew.config.paths import kiro_sessions_dir
from kiro_crew.executors import subprocess_executor
from kiro_crew.hooks import HOOK_EVENT_POST_TOOL_USE, fire_tool_hooks, get_global_hook_store
from kiro_crew.json_line import parse_json_object_line
from kiro_crew.jsonl_util import bounded_raw_records_with_offsets
from kiro_crew.metrics.tool_calls import note_tool_call_started, record_tool_call_finished
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.security.credential_sources import tool_output_fingerprints
from kiro_crew.sel import sel

if TYPE_CHECKING:
    from kiro_crew.acp.client import AcpClient

#: The client's own logger: this code logged under that name before the split.
logger = logging.getLogger("kiro_crew.acp.client")


# Cap the structured-tool-params cache so a stream of ToolCall notifications with
# no matching request_permission can't grow it without bound (the entries are
# popped on the permission event and wholesale-cleared per prompt; this is just a
# backstop for the pathological no-permission case).
_MAX_CACHED_TOOL_PARAMS = 256


# Canonical ACP tool-kind value for shell/exec tools. kiro-cli and
# claude-agent-acp both report shell commands with kind="execute", and so does
# codex-acp -- for its MCP tool calls too. _is_shell_kind() is therefore only
# the kind-half of the rule; the tool_call paths classify the WHOLE frame with
# _dispatch.classify_tool_call, which consults the adapter-authored MCP markers
# before the kind.
_ACP_SHELL_KIND = "execute"


def _is_shell_kind(kind: str | None) -> bool:
    """True when an ACP tool_kind denotes a shell/exec command (kind-only view)."""
    return kind == _ACP_SHELL_KIND


def _make_unified_diff(old: str, new: str, path: str, max_len: int = 65536) -> str:
    """Generate a unified diff string from old/new text, handling empty inputs.

    Thin delegate to :func:`kiro_crew.acp._dispatch.make_unified_diff`, kept as
    a module-level name for this file's call sites and tests; the truncation
    semantics (line-boundary cut + ``DIFF_TRUNCATION_MARK``) live in one place.
    """
    return make_unified_diff(old, new, path, max_len=max_len)


def _extract_text_chunk(self: AcpClient, msg: JsonRpcMessage) -> tuple[str | None, bool]:
    """Extract text from an agent_message_chunk or agent_thought_chunk update.

    Returns (text, is_thinking). is_thinking is True when the chunk is an
    ``agent_thought_chunk`` (claude-agent-acp emits reasoning under this
    dedicated update type) or when an ``agent_message_chunk``'s inner
    content block type indicates reasoning (kiro-cli style).
    """
    params = msg.params or {}
    update = params.get("update", {})
    # The update comes straight from the agent process; a non-dict value
    # (null/list/string) would raise AttributeError here, inside the
    # prompt-turn dispatch path — same boundary rule as _track_usage_update.
    if not isinstance(update, dict):
        return None, False
    kind = update.get("sessionUpdate")
    if kind == UPDATE_AGENT_MESSAGE_CHUNK:
        content = update.get("content", {})
        if not isinstance(content, dict):
            return None, False
        text = content.get("text")
        content_type = content.get("type", "text")
        is_thinking = content_type in ("thinking", "reasoning")
        return text, is_thinking
    if kind == UPDATE_AGENT_THOUGHT_CHUNK:
        content = update.get("content", {})
        if not isinstance(content, dict):
            return None, True
        text = content.get("text")
        return text, True
    return None, False


async def _maybe_audit_tool_call(self: AcpClient, tool_event: "AcpEvent") -> None:
    """Emit a per-tool-call SEL audit for clients with no external audit loop.

    App/worker-pool clients (code-review-sage, knowledge llm_pool) run tools
    through this AcpClient without going through chat_runner or SubagentManager,
    so their tool calls would otherwise never reach the security audit log.
    Gated on ``audit_source`` (None for chat / subagent clients, so they never
    double-log). Best-effort: a SEL failure must never break tool dispatch, but
    is logged at WARNING so audit-pipeline breakage surfaces to on-call.

    The ``sel().log_tool_invocation`` call is offloaded onto
    ``subprocess_executor()`` (the same dedicated pool the child-record
    offloads in this file use, e.g. ``_capture_child_records``) so that any
    SEL-backend I/O (file write, network, DNS) can never block the event loop
    and freeze the gateway heartbeat. The dedicated pool — rather than the
    default executor — isolates a call that can wedge on a stuck kernel
    resource, so a hung SEL backend cannot starve default-pool users. The
    offload is additionally bounded by ``asyncio.wait_for``: if the SEL call
    hangs, the ``await`` cannot stall this turn's tool dispatch indefinitely
    (a pending executor future never raises on its own) — the timeout raises
    ``TimeoutError`` (an ``Exception`` subclass), which the handler below
    swallows so dispatch always proceeds. Per the fault-isolation guideline a
    leaked worker thread is survivable; a stalled dispatch is not.
    ``tool_name``/``tool_kind`` use the same ``or`` fallbacks as the
    observed-tool-call bookkeeping above, so an audit record is always emitted
    with meaningful values rather than lost.
    """
    if not self._audit_source:
        return
    # Bind the guard-narrowed (non-None) audit_source into a local: mypy does
    # not carry the ``if not self._audit_source`` narrowing into the nested
    # lambda closure below, so referencing the attribute directly there would
    # be seen as ``str | None``.
    audit_source = self._audit_source
    try:
        await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(
                subprocess_executor(),
                lambda: sel().log_tool_invocation(
                    session_key=self._session_key or "",
                    agent=self._agent,
                    source=audit_source,
                    tool_name=tool_event.title or "unknown",
                    tool_kind=tool_event.tool_kind or "",
                    outcome="auto_approved",
                ),
            ),
            timeout=_SEL_AUDIT_TIMEOUT_SECONDS,
        )
    except Exception:
        logger.warning("ACP-layer SEL audit failed", exc_info=True)


async def _maybe_fire_pre_tool_hooks(self: AcpClient, tool_event: "AcpEvent") -> None:
    """Fire the PreToolUse HOOK ENGINE for a tool_call, for audit-source clients.

    App/worker-pool clients (code-review-sage, knowledge llm_pool) run their
    tools through this AcpClient without going through chat_runner or
    SubagentManager, so — until this method existed — the script-hook engine
    never fired for them. That silently dropped skill-usage telemetry (a
    PostToolUse hook matching 'Reading *SKILL.md*') for /add_context and every
    other subagent skill load. This brings those clients to hook parity with
    the main agent and SubagentManager subagents.

    Gated on ``audit_source`` (None for chat / subagent clients) so the
    chat/main client is completely unaffected — it fires its own hooks via
    chat_runner and must never double-fire. No-ops if the global hook store is
    not initialized. Best-effort + NON-FATAL: any hook-engine error is caught
    and logged at WARNING (mirroring the SEL audit handler above) and NEVER
    breaks tool dispatch — the hook fire is awaited directly, exactly as
    ``subagent.py`` awaits ``fire_tool_hooks`` (the underlying
    ``run_script_hook`` bounds each script with its own timeout).

    ``fire_tool_hooks`` fires PreToolUse ONLY (see hooks.py) — PostToolUse is
    fired separately in ``_maybe_fire_post_tool_hooks`` once the tool RESULT
    (and its output) is available.
    """
    if not self._audit_source:
        return
    hook_store = get_global_hook_store()
    if hook_store is None:
        return
    try:
        # Redact tool_input before firing user hooks (parity with Post path); addresses security-controls review.
        _redacted_input = tool_event.tool_input
        if isinstance(_redacted_input, str):
            _redacted_input, _ = redact_credentials(_redacted_input)
            _redacted_input, _ = redact_exfiltration_urls(_redacted_input)
        elif _redacted_input is not None:
            # Non-str (dict/list) inputs must ALSO be redacted, not bypassed by
            # the isinstance(str) guard. Serialize to JSON, redact the string,
            # and pass the redacted JSON string (fire_tool_hooks json.loads it,
            # so it expects a str | None — do NOT deserialize back to an object).
            _serialized = json.dumps(_redacted_input)
            _serialized, _ = redact_credentials(_serialized)
            _serialized, _ = redact_exfiltration_urls(_serialized)
            _redacted_input = _serialized
        await fire_tool_hooks(
            hook_store,
            # Fall back to 'unknown' when the event carries no title, matching
            # the Post path's tool_name recovery so a hook matcher sees a
            # consistent name across Pre/Post.
            tool_event.title or "unknown",
            _redacted_input,
            agent_role=self._agent or None,
        )
    except Exception:
        logger.warning("ACP-layer PreToolUse hook failed", exc_info=True)


async def _maybe_fire_post_tool_hooks(self: AcpClient, tool_result_event: "AcpEvent") -> None:
    """Fire the PostToolUse HOOK ENGINE for a tool RESULT, for audit-source clients.

    Companion to ``_maybe_fire_pre_tool_hooks``. The Pre-vs-Post split is
    forced by the hook engine: ``fire_tool_hooks`` fires PreToolUse ONLY (at
    tool_call time the tool has not run and has no output), so PostToolUse must
    fire here, on the RESULT branch. The output MUST be carried on
    ``tool_response={'output': ...}`` — the IDENTICAL shape used by chat_runner
    (main agent) and subagent.py — because the skill-usage emit.sh reads the
    SKILL.md frontmatter out of ``tool_response.output`` (and matches the
    'Reading *SKILL.md*' tool_name). Without the output payload the telemetry
    hook fires blind and captures nothing.

    Gated on ``audit_source`` and no-ops if the global hook store is
    uninitialized. Best-effort + NON-FATAL: any error is caught + logged and
    never breaks dispatch. The tool RESULT event carries no title (see
    ``_build_tool_result_event``), so the tool_name is recovered from
    ``_observed_tool_calls`` (populated on the tool_call above) with the same
    'Running: ' strip ``fire_tool_hooks`` / subagent.py apply, so Pre and Post
    agree on the tool_name a hook matcher sees. ``tool_output`` is already
    redacted at the ACP boundary by ``_build_tool_result_event``.
    """
    if not self._audit_source:
        return
    hook_store = get_global_hook_store()
    if hook_store is None:
        return
    # Fall back to "unknown" to match the Pre path (Pre/Post tool_name consistency).
    tool_name = (
        self._observed_tool_calls.get(tool_result_event.tool_call_id or "", ("unknown", ""))[0]
        or "unknown"
    )
    if tool_name.startswith("Running: "):
        tool_name = tool_name[9:]
    try:
        # Redact before firing user hooks (parity with chat_runner PostToolUse); addresses security-controls review.
        _redacted_output, _ = redact_credentials(tool_result_event.tool_output or "")
        _redacted_output, _ = redact_exfiltration_urls(_redacted_output)
        # Bound the payload handed to user hook scripts to the first 2000 chars
        # (parity with chat_runner/subagent.py [:2000]). Redact-then-truncate is
        # deliberate: redact the FULL output first so secrets anywhere are scrubbed,
        # only THEN truncate — truncating first could leave a secret past char 2000.
        _redacted_output = _redacted_output[:2000]
        await hook_store.fire(
            HOOK_EVENT_POST_TOOL_USE,
            tool_name=tool_name,
            tool_response={"output": _redacted_output},
            agent_role=self._agent or None,
        )
    except Exception:
        logger.warning("ACP-layer PostToolUse hook failed", exc_info=True)


def _emit_tool_interrupted_sel(self: AcpClient, site: str) -> None:
    """Emit a SEL audit event when kiro-cli cancels tool uses via its security filter.

    This is a security-relevant permission decision (kiro-cli denied tool execution)
    that Kiro Crew observes but does not control.  Logged so the audit trail reflects
    that tools were blocked even though the decision was made outside Kiro Crew.
    Also emits a single WARNING log line (grep-friendly for on-call) with session
    correlation — covers all three call sites so none of them fire silently.
    """
    logger.warning("kiro-cli cancelled tool use(s) [site=%s session=%s]", site, self._session_id)
    try:
        # Re-imported at call time on purpose: the module-level binding is
        # captured at import time, so only this rebind resolves the CURRENT
        # ``kiro_crew.sel.sel`` and lets a substituted emitter be observed.
        from kiro_crew.sel import sel

        sel().log_tool_invocation(
            session_key=self._session_key or "",
            source="acp",
            tool_name="kiro_cli_security_filter",
            tool_kind="client_built_in",
            outcome="denied",
            metadata={"site": site, "reason": "tool_interrupted_marker"},
        )
    except Exception:
        logger.warning("SEL audit failed for tool_interrupted at %s", site, exc_info=True)


def _track_tool_call(self: AcpClient, msg: JsonRpcMessage) -> None:
    """Track tool calls in stats (used by send_message/send_message_stream)."""
    params = msg.params or {}
    update = params.get("update", {})
    if not isinstance(update, dict):
        return
    if update.get("sessionUpdate") == UPDATE_TOOL_CALL:
        title = update.get("title", "unknown")
        kind = update.get("kind", "unknown")
        self.last_prompt_stats.tool_calls.append((kind, title))
        logger.debug("ACP tool_call: %s (%s)", title, kind)


def _extract_tool_event(self: AcpClient, msg: JsonRpcMessage) -> AcpEvent | None:
    params = msg.params or {}
    update = params.get("update", {})
    if not isinstance(update, dict):
        return None
    if update.get("sessionUpdate") == UPDATE_TOOL_CALL:
        title = update.get("title", "unknown")
        _wire_title = title if isinstance(title, str) and title != "unknown" else ""
        kind = update.get("kind", "unknown")
        # First PRESENT key, not first truthy one -- an explicit empty
        # rawInput is a real argument set for the directive digest. Mirrors
        # _dispatch._build_tool_call_event.
        raw_input = next(
            (update[k] for k in ("rawInput", "input", "params") if update.get(k) is not None),
            None,
        )
        purpose = extract_tool_purpose(raw_input)
        logger.debug(
            "ACP tool_call raw: %s",
            {k: v for k, v in update.items() if k != "sessionUpdate"},
        )
        # Build initial tool input string from raw params
        tool_call_id = update.get("toolCallId", "")
        # Start the round-trip clock here rather than at the yield: this is
        # the first moment Kiro Crew sees the call, and the same id's terminal
        # status is stamped in _extract_tool_call_update below. Both of this
        # class's message loops reach this method, so instrumenting it covers
        # them without a second call site in each.
        # One classification for the caches, the event and the metric:
        # kind + adapter-authored MCP markers, never the kind alone (see
        # _dispatch.classify_tool_call).
        identity = classify_tool_call(update)
        note_tool_call_started(
            tool_call_id,
            kind=kind,
            mcp_server_name=identity.mcp_server_name,
            # getattr: a real client always carries _session_id, but this
            # extractor is also driven directly with lightweight test
            # doubles (test_acp_tool_identity), and telemetry must not be
            # the reason such a double stops working.
            scope=getattr(self, "_session_id", "") or "",
        )
        input_str = ""
        if tool_call_id and raw_input:
            input_str = (
                _dumps_degraded(raw_input, indent=2)
                if isinstance(raw_input, (dict, list))
                else str(raw_input)
            )
        # For edit tools with diff content blocks, generate unified diff
        found_diff = False
        _diff_path = ""
        content_blocks = update.get("content", [])
        if isinstance(content_blocks, list):
            for cb in content_blocks:
                if isinstance(cb, dict) and cb.get("type") == "diff":
                    old = cb.get("oldText") or ""
                    new = cb.get("newText") or ""
                    path = cb.get("path", "")
                    if tool_call_id and path:
                        self._tool_call_diff_path[tool_call_id] = path
                        _diff_path = path
                    diff_str = _make_unified_diff(old, new, path)
                    if diff_str:
                        input_str = diff_str
                        found_diff = True
                    break
        # Fallback when no diff content block was found: derive from the
        # edit args (strReplace pair, create/insert content). Gated on
        # the EDIT kind — "content"-shaped args exist on non-edit tools.
        if not found_diff and (
            kind == "edit"
            or (isinstance(raw_input, dict) and raw_input.get("command") == "strReplace")
        ):
            diff_str = derive_edit_diff(raw_input)
            if diff_str:
                input_str = diff_str
        # Redact sensitive content before caching/displaying
        input_redacted = False
        if input_str:
            safe_input, _ = redact_exfiltration_urls(input_str)
            safe_input, _ = redact_credentials(safe_input)
            input_redacted = safe_input != input_str
            input_str = safe_input
        if tool_call_id and input_str:
            self._tool_call_inputs[tool_call_id] = input_str
            self._tool_call_input_redacted[tool_call_id] = input_redacted
        # Cache the STRUCTURED raw params (path/url/command) so the later
        # request_permission event can feed the governance gate's arg-derived
        # scopes (filesystem.write / network.egress). Bounded by the same
        # clear() as _tool_call_inputs; capped to avoid unbounded growth on a
        # stream that never sends a matching permission request.
        # Truthy-gated, like _dispatch's raw_params_cache: this cache is the
        # permission event's TRUSTED params source, and an empty ``{}`` here
        # (Claude's initial frame) would be found by ``.get`` and suppress the
        # inline-frame fallback, so the refinement's sensitive path never
        # reached governance. The event's own raw_tool_params keeps the ``{}``.
        if tool_call_id and isinstance(raw_input, dict) and raw_input:
            if len(self._tool_call_params) > _MAX_CACHED_TOOL_PARAMS:
                self._tool_call_params.clear()
            self._tool_call_params[tool_call_id] = raw_input
        # Redact LLM-influenced fields before dashboard display
        if purpose:
            purpose, _ = redact_exfiltration_urls(purpose)
            purpose, _ = redact_credentials(purpose)
        # Prefer rawInput.description over the SDK-provided title (e.g.
        # some backends' Bash tool emits "List Kiro Crew ACP module files"
        # alongside `ls <workspace>/...`). For claude-agent-acp this rarely
        # fires here because the initial tool_call has empty rawInput —
        # the refinement path in `_extract_tool_call_refinement` is what
        # the user actually sees. Same helper is used in both places.
        # Capture the canonical shell signal from the raw kind BEFORE
        # redaction so the later permission_request event (which carries no
        # kind) can inherit it via the toolCallId cache below.
        is_shell = identity.is_shell
        if tool_call_id:
            if identity.kind_resolved:
                self._tool_call_is_shell[tool_call_id] = is_shell
            # An MCP identity is a classification too -- it says "served by a
            # server", which is not a host command. Unclassified means neither
            # the kind, the harness's own shell channel nor an MCP marker said
            # anything (classify_tool_call folds all three; a malformed codex
            # marker resolves nothing and lands here as unclassified).
            #
            # Written through the same compatibility shape as the redaction map
            # below: an instance allocated with ``__new__`` (embedders do this) has
            # no such dict, and assigning into a missing one would raise inside the
            # event builder rather than record anything.
            _unclassified = getattr(self, "_tool_call_unclassified", None)
            if _unclassified is not None:
                _unclassified[tool_call_id] = not (
                    identity.kind_resolved or bool(identity.mcp_server_name)
                )
            # Same lifecycle as is_shell: cache the trusted MCP server
            # identity so the later permission event can inherit it.
            self._tool_call_mcp_server[tool_call_id] = identity.mcp_server_name
            # Cache the trusted tool name too, so the permission event can
            # rebuild mcp__<server>__<tool> for per-tool governance.
            self._tool_call_tool_name[tool_call_id] = identity.tool_name
            # The tool's own name, for the permission event's
            # harness_tool_id on a backend with no _meta.kiro.toolId.
            # getattr for the same reason as _tool_call_unclassified.
            _harness_names = getattr(self, "_tool_call_harness_tool_name", None)
            if _harness_names is not None:
                _harness_name = harness_tool_name(update)
                # dsh forwards the provider's tool-call id verbatim, and its
                # permission frame names only that id. Two calls under one id
                # in a turn leave the name unknown, so neither is read as the
                # read tool the other one is.
                if (
                    self.backend == ACP_BACKEND_DEEPSEEK
                    and _harness_names.get(tool_call_id, _harness_name) != _harness_name
                ):
                    _harness_name = ""
                _harness_names[tool_call_id] = _harness_name
            # KAS only (H13): the mark and the refusal it earns are a KAS
            # refusal mode; a first-class harness's frame is never marked.
            if (
                identity.identity_unreadable
                and self.backend in ACP_BACKENDS_PERMISSION_KIND_FROM_TOOL_CALL
            ):
                self._tool_call_identity_unreadable[tool_call_id] = True
        title = _select_tool_title(title, raw_input, kind, is_shell=is_shell) or ""
        if title:
            title, _ = redact_exfiltration_urls(title)
            title, _ = redact_credentials(title)
        if kind:
            kind, _ = redact_exfiltration_urls(kind)
            kind, _ = redact_credentials(kind)
        self.last_prompt_stats.tool_calls.append((kind, title))
        # Trusted identity from adapter-authored markers (NOT the
        # LLM-authored title), shared with the dispatch builder so both
        # event paths carry the same classifier verdict.
        return AcpEvent(
            kind=EVENT_TOOL_CALL,
            title=title,
            # The backend's OWN title, before select_tool_title swaps in a
            # shell call's description: the directive claim's tool resolver
            # reads this and only this (see AcpEvent.wire_title).
            wire_title=_wire_title,
            tool_kind=kind,
            tool_purpose=purpose,
            tool_input=input_str,
            tool_input_redacted=input_redacted,
            tool_call_id=tool_call_id,
            raw_tool_params=raw_input if isinstance(raw_input, dict) else None,
            is_shell=is_shell,
            # Trusted identity from the adapter-authored markers (NOT the
            # LLM-authored title). Earned only when an identity pair was
            # actually extracted from such a source: a frame with no marker
            # populates nothing and asserts no provenance.
            tool_name=identity.tool_name,
            mcp_server_name=identity.mcp_server_name,
            tool_identity_trusted=identity.tool_identity_trusted,
            mcp_identity_trusted=identity.identity_trusted,
            diff_path=_diff_path,
        )
    return None


def _extract_tool_call_update(self: AcpClient, msg: JsonRpcMessage) -> AcpEvent | None:
    """Extract a real-time tool result from a `tool_call_update` session update.

    kiro-cli streams tool completion via ACP `session/update` notifications
    (not just the JSONL session file). Two updates fire per tool:
      1. A `content` array carrying the tool output as text blocks — arrives
         as soon as the tool finishes, often mid-stream during the agent's
         follow-up text.
      2. A `status: completed` update with `rawOutput.items[].Json.stdout`
         for shell-style tools.
    Both carry the same `toolCallId`; we yield an EVENT_TOOL_RESULT when an
    update provides output or a terminal status. A status-only event carries
    no output. Hooking these gives the inline pill its real output the moment
    the tool finishes, instead of waiting for the kiro-cli JSONL flush at the
    next tool_call boundary or message end.
    """
    params = msg.params or {}
    update = params.get("update", {})
    if not isinstance(update, dict) or update.get("sessionUpdate") != "tool_call_update":
        return None
    tool_use_id = update.get("toolCallId", "")
    if not tool_use_id:
        return None
    # A terminal status is useful even when output parsing finds no text: it
    # closes the observed round-trip without claiming a result body. A
    # non-terminal status leaves the clock running for the real completion.
    record_tool_call_finished(
        tool_use_id,
        status=update.get("status"),
        scope=getattr(self, "_session_id", "") or "",
    )

    output_parts: list[str] = []

    # Path 1: `content` blocks (arrive during tool execution / mid-stream)
    content = update.get("content")
    if isinstance(content, list):
        for block in content:
            text = tool_call_content_text(block)
            if text:
                output_parts.append(text)

    # Path 2: `rawOutput` (arrives with status=completed) — fallback when
    # there were no content blocks (e.g. some tools only emit rawOutput).
    # kiro-cli tool results land here in two shapes:
    #   items[].Text  — fs_read contents, shell-style text, etc.
    #   items[].Json  — structured tool output (use .stdout when present)
    if not output_parts:
        raw_output = update.get("rawOutput")
        if isinstance(raw_output, dict):
            items = raw_output.get("items", [])
            if isinstance(items, list):
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    if "Text" in item and item.get("Text"):
                        output_parts.append(str(item["Text"]))
                        continue
                    j = item.get("Json")
                    if isinstance(j, dict):
                        if "stdout" in j and j.get("stdout"):
                            output_parts.append(str(j["stdout"]))
                        else:
                            output_parts.append(_dumps_degraded(j, default=str))
            # Path 3: an object that is not that envelope at all. Mirrors
            # ``_dispatch._build_tool_result_event`` -- ``rawOutput`` is
            # unstructured passthrough, so ``items[]`` is one producer's
            # wrapper and an unrecognised object is not evidence the tool
            # produced nothing. Returning None here is costlier than in the
            # dispatch parser: both call sites use the result to disarm the
            # stall watchdog and to fire PostToolUse hooks, so a dropped
            # event leaves the watchdog armed and the hooks unfired. Gated on
            # the ABSENCE of ``items`` so no ``items[]`` envelope changes.
            # No per-part cut here or above: parts are collected RAW and
            # the single bound is applied AFTER redaction at the end of this
            # method, because a cut taken before redaction can split a
            # credential into fragments no pattern matches.
            if raw_output and "items" not in raw_output:
                output_parts.append(_dumps_degraded(raw_output, default=str))

    tool_status = str(update.get("status") or "")
    if not output_parts:
        if tool_status not in TERMINAL_TOOL_STATUSES:
            log_unrenderable_content(logger, tool_use_id, content)
            return None
        return AcpEvent(
            kind=EVENT_TOOL_RESULT,
            tool_call_id=tool_use_id,
            tool_final=tool_status == "completed",
            tool_status=tool_status,
        )

    final_joined = "\n".join(output_parts)
    # Redact the WHOLE join, then bound -- never the reverse. Bounding first
    # can split a credential across the cut into fragments no pattern
    # matches: with a connection URI whose "@" lands on byte 8000, the head
    # slice keeps "://user:password" and drops the "@" the prefilter needs,
    # so the password reaches the dashboard in clear text. Same ordering as
    # `_dispatch._build_tool_result_event` and as `_compaction_detail` below.
    _redacted = redact_text(final_joined)
    tool_output_digest, tool_output_bytes = _measure_tool_output(_redacted)
    final_output = _redacted[:8000]
    return AcpEvent(
        kind=EVENT_TOOL_RESULT,
        tool_call_id=tool_use_id,
        tool_output=final_output,
        tool_output_digest=tool_output_digest,
        tool_output_bytes=tool_output_bytes,
        # Same contract as `_dispatch._build_tool_result_event`: only a
        # result the redactor changed can hold a credential to trace.
        tool_output_credentials=(
            tool_output_fingerprints(final_joined) if _redacted != final_joined else ()
        ),
        tool_final=update.get("status") == "completed",
        tool_status=str(update.get("status") or ""),
    )


def _extract_tool_call_refinement(self: AcpClient, msg: JsonRpcMessage) -> AcpEvent | None:
    """Extract a refined title/kind/input from a `tool_call_update`.

    claude-agent-acp emits two events per tool: an initial `tool_call`
    on streaming `content_block_start` (when `chunk.input` is still empty,
    so the title falls back to the generic tool name like "Terminal" or
    "grep"), then a follow-up `tool_call_update` once `chunk.input` is
    fully streamed — that update carries the populated `rawInput` and a
    refined `title`/`kind` from the upstream `toolInfoFromToolUse`
    (e.g. `"ls <workspace>/notes"`).

    We yield an EVENT_TOOL_CALL_UPDATE so the dashboard can patch the
    existing pill / persisted message in place — see the matching
    handler in `chat_runner.py`. Returns None when the update only
    carries output (handled separately by `_extract_tool_call_update`).
    """
    params = msg.params or {}
    update = params.get("update", {})
    if not isinstance(update, dict) or update.get("sessionUpdate") != "tool_call_update":
        return None
    tool_use_id = update.get("toolCallId", "")
    if not tool_use_id:
        return None
    title = update.get("title")
    kind = update.get("kind")
    raw_input = update.get("rawInput")
    # Only emit when at least one refinement field is present. Pure-output
    # updates (content/rawOutput only) are handled by the result extractor.
    if title is None and kind is None and not raw_input:
        return None
    # The refinement carries the COMPLETE params (Claude streams an empty
    # rawInput on the initial tool_call). Refresh the permission event's
    # trusted-params cache from it, as _dispatch._build_tool_refinement_event
    # does, so governance's sensitive-path scope reads the real arguments.
    if tool_use_id and isinstance(raw_input, dict) and raw_input:
        if len(self._tool_call_params) > _MAX_CACHED_TOOL_PARAMS:
            self._tool_call_params.clear()
        self._tool_call_params[tool_use_id] = raw_input
    # Build the input string the same way `_extract_tool_event` does so
    # the merged toolLog entry / message meta lines up across both events.
    input_str = ""
    if isinstance(raw_input, (dict, list)) and raw_input:
        input_str = _dumps_degraded(raw_input, indent=2)
    elif isinstance(raw_input, str):
        input_str = raw_input
    # Edit-style diff content blocks: prefer the rendered unified diff over
    # the raw input dict (mirrors `_extract_tool_event`).
    _diff_path = ""
    content_blocks = update.get("content", [])
    if isinstance(content_blocks, list):
        for cb in content_blocks:
            if isinstance(cb, dict) and cb.get("type") == "diff":
                old = cb.get("oldText") or ""
                new = cb.get("newText") or ""
                path = cb.get("path", "")
                if path:
                    self._tool_call_diff_path[tool_use_id] = path
                    _diff_path = path
                diff_str = _make_unified_diff(old, new, path)
                if diff_str:
                    input_str = diff_str
                break
    input_redacted = False
    if input_str:
        safe_input, _ = redact_exfiltration_urls(input_str)
        safe_input, _ = redact_credentials(safe_input)
        input_redacted = safe_input != input_str
        input_str = safe_input
        self._tool_call_inputs[tool_use_id] = input_str
        self._tool_call_input_redacted[tool_use_id] = input_redacted
    # Refresh the cached shell signal only when this refinement carries a
    # kind. A refinement that omits kind must NOT clobber a True cached by
    # the initial tool_call notification (kind is optional on updates).
    # Cache off the RAW kind, not the redacted kind_str. Resolved BEFORE the
    # title so the label rule sees the real classification rather than a
    # missing kind.
    _identity = classify_tool_call(update)
    if _identity.kind_resolved:
        self._tool_call_is_shell[tool_use_id] = _identity.is_shell
    is_shell = self._tool_call_is_shell.get(tool_use_id, False)
    # Prefer rawInput.description over the SDK-supplied title (e.g.
    # Bash's "List Kiro Crew ACP module files" rather than `ls <workspace>/...`).
    # Same helper as `_extract_tool_event` so the rule is consistent.
    # A refinement carrying a title but no rawInput still overwrites the
    # pill, so the command has to be recoverable from the params the initial
    # tool_call cached — otherwise a backend that sends a generic title on
    # both events lands that label on a pill the first event got right.
    _title_params: object = raw_input
    if not (isinstance(raw_input, dict) and raw_input):
        _title_params = self._tool_call_params.get(tool_use_id)
    title_source = _select_tool_title(title, _title_params, kind, is_shell=is_shell)
    title_str = ""
    if title_source:
        title_str, _ = redact_exfiltration_urls(title_source)
        title_str, _ = redact_credentials(title_str)
    kind_str = ""
    if isinstance(kind, str) and kind:
        kind_str, _ = redact_exfiltration_urls(kind)
        kind_str, _ = redact_credentials(kind_str)
    # The refinement's rawInput is the COMPLETE params object, so it carries
    # the reserved purpose argument too. Read it here or the purpose is lost
    # whenever the initial tool_call streamed an empty rawInput — and
    # consumers that treat an empty purpose as "fall back to the raw title"
    # (the session list's running-status line) would replace a good purpose
    # with a command. Mirrors `_dispatch._build_tool_refinement_event`.
    purpose = extract_tool_purpose(raw_input)
    if purpose:
        purpose, _ = redact_exfiltration_urls(purpose)
        purpose, _ = redact_credentials(purpose)
    return AcpEvent(
        kind=EVENT_TOOL_CALL_UPDATE,
        title=title_str,
        wire_title=title if isinstance(title, str) else "",
        tool_kind=kind_str,
        tool_purpose=purpose,
        tool_input=input_str,
        tool_input_redacted=input_redacted,
        tool_call_id=tool_use_id,
        raw_tool_params=raw_input if isinstance(raw_input, dict) else None,
        is_shell=is_shell,
        diff_path=_diff_path,
    )


async def _read_new_tool_results(self: AcpClient) -> list[AcpEvent]:
    results = await asyncio.to_thread(self._read_new_tool_results_sync)
    if results:
        self._active_tool_calls.difference_update(event.tool_call_id or "" for event in results)
        self._tool_dispatched = self._active_tool_calls.possibly_open
        self._stale_eligible = not self._active_tool_calls.possibly_open
    return results


def _read_new_tool_results_sync(self: AcpClient) -> list[AcpEvent]:
    """Read new ToolResults entries from the kiro-cli session JSONL file."""
    if not self._session_id:
        return []
    jsonl_path = kiro_sessions_dir() / f"{self._session_id}.jsonl"
    if not jsonl_path.exists():
        return []
    results: list[AcpEvent] = []
    try:
        # Read as bytes and decode each record on its own with
        # ``errors="replace"``: kiro-cli writes UTF-8, but a strict text
        # read raises on one bad byte BEFORE the offset below is saved, so
        # every later scan would fail at the same place for the rest of
        # the session. The capped reader bounds one record's memory, and
        # its offsets are byte offsets to resume from.
        with open(jsonl_path, "rb") as f:
            f.seek(self._jsonl_pos)
            for _start, end, record in bounded_raw_records_with_offsets(
                f, jsonl_path, label="JSONL tool results"
            ):
                if record is not None and not record.endswith((b"\n", b"\r")):
                    break  # partial line — retry next call
                # Saved before the line is parsed, so a line this scan
                # cannot use (an over-cap one included) costs that line
                # only, and the lines after it are still read.
                self._jsonl_pos = end
                if record is None:
                    continue
                try:
                    results.extend(self._jsonl_tool_results(record))
                except Exception:  # noqa: BLE001 - one line, not the scan
                    logger.debug("JSONL: skipped a tool-result line", exc_info=True)
    except Exception:
        logger.debug("Failed to read JSONL for tool results", exc_info=True)
    if results:
        logger.debug("JSONL: read %d tool result(s) from %s", len(results), jsonl_path.name)
    return results


def _jsonl_tool_results(line: bytes) -> list[AcpEvent]:
    """The tool results one session JSONL line carries; ``[]`` for any other line.

    Anything that is not the expected shape is skipped at its own level --
    a line that is not a JSON object, a ``data`` or ``content`` that is not
    an object or list, an item that is not an object, a result without a
    non-empty string ``toolUseId``, a ``stdout`` that is not a string.
    """
    entry = parse_json_object_line(line, errors="replace")
    if entry is None or entry.get("kind") != "ToolResults":
        return []
    data = entry.get("data")
    if not isinstance(data, dict):
        return []
    content = data.get("content")
    if not isinstance(content, list):
        return []
    results: list[AcpEvent] = []
    for c in content:
        if not isinstance(c, dict) or c.get("kind") != "toolResult":
            continue
        tr = c.get("data")
        if not isinstance(tr, dict):
            continue
        tool_use_id = tr.get("toolUseId")
        if not isinstance(tool_use_id, str) or not tool_use_id:
            continue
        tr_content = tr.get("content")
        if not isinstance(tr_content, list):
            continue
        output_parts: list[str] = []
        for rc in tr_content:
            if not isinstance(rc, dict):
                continue
            if rc.get("kind") == "json":
                d = rc.get("data", {})
                if isinstance(d, dict) and "stdout" in d:
                    out = d.get("stdout", "")
                    if isinstance(out, str) and out:
                        output_parts.append(out[:4000])
                else:
                    output_parts.append(_dumps_degraded(d, indent=2)[:4000])
            elif rc.get("kind") == "text":
                output_parts.append(str(rc.get("data", ""))[:4000])
        if output_parts:
            joined = "\n".join(output_parts)
            results.append(
                AcpEvent(
                    kind=EVENT_TOOL_RESULT,
                    tool_call_id=tool_use_id,
                    tool_output=joined[:8000],
                    # A kiro-cli result read back from its session
                    # file traces credentials like a streamed one.
                    tool_output_credentials=tool_output_fingerprints(joined),
                )
            )
    return results
