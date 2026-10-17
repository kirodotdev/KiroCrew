"""AcpSessionHandle — one multiplexed ACP session on a shared runtime.

Split out of ``runtime.py`` to keep the two responsibilities in separate files:

- ``session_handle.py`` (this file): the per-session API surface — one
  ``sessionId`` + its ``asyncio.Queue``, the prompt/cancel/approve/reject event
  loop, and the per-session stale/stall watchdog. Depends only on the runtime
  *protocol* (``AcpRuntimeProtocol``), the shared dispatch parser, and the ACP
  types — never on the concrete ``AcpRuntime``. A member whose responsibility has an
  owner module lives in that ``session_handle_*`` owner, and the class body binds it
  by name; the rest are defined here.
- ``runtime.py``: ``AcpRuntime`` — owns the subprocess + single-reader demux and
  constructs handles.

The runtime exceptions (``AcpRuntimeError`` / ``AcpRuntimeDead``) and the
``AcpRuntimeProtocol`` live in ``session_handle_runtime_contract.py`` (the lower
layer) and are re-exported here, so ``runtime.py`` imports them from this module
without a circular import; ``runtime.py`` re-exports them so
existing ``from kiro_crew.acp.runtime import AcpSessionHandle`` call sites keep
working.
"""

from __future__ import annotations

import asyncio
import importlib as _importlib
import logging
import sys as _sys
import time
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from kiro_crew import _facade_mirror, acp_tool_gate, model_registry, permission_floor
from kiro_crew.acp import kas_wire
from kiro_crew.acp import session_handle_compaction as _sh_compaction
from kiro_crew.acp import session_handle_dispatch as _sh_dispatch
from kiro_crew.acp import session_handle_lifecycle as _sh_lifecycle
from kiro_crew.acp import session_handle_turn as _sh_turn
from kiro_crew.acp import session_handle_watchdog as _sh_watchdog
from kiro_crew.acp._dispatch import (
    DRAIN_YIELD_AFTER_S,
    MAX_ACTIVE_TOOL_CALLS,
    TOOL_CALL_CACHE_MAX_ENTRIES,
    TOOL_CALL_CACHE_MAX_PAYLOAD_CHARS,
    build_permission_event,
    classify_notification,
    error_is_refusal_terminal,
    identified_mcp_call,
    is_mcp_tool_approval,
    is_unattributed_error_candidate,
    kas_crew_agent_id,
    liveness_key,
    parse_codex_compaction_update,
    parse_metadata,
    parse_prompt_token_usage,
    parse_refusal,
    parse_session_update,
    parse_text_chunk,
    parse_usage_cost,
    parse_usage_update,
    prompt_is_adapter_command,
    redact_text,
    reject_option_id,
    release_tool_call_provenance,
    scope_overflowed,
    scoped_tool_cache_key,
    set_mode_params,
    set_model_params,
    unattributed_terminal_error,
)
from kiro_crew.acp.client import (
    _COMPACTION_FAILED_TURN_BUDGET,
    DEFAULT_MODEL,
    AcpClient,
    AcpError,
    AcpModelUnavailable,
    AcpProcessDied,
    AcpTimeoutError,
    AcpToolGateUnroutable,
    _effective_prompt_timeout_async,
    _is_config_value_rejection,
    _is_safe_oauth_url,
    _is_tool_interrupted_marker,
    _jsonrpc_error_code,
    _loggable_request_id,
    _push_model_via_effort_split,
    _raise_acp_error,
    advertised_model_ids,
    catalog_row_would_drop,
    compaction_failure_detail,
    compaction_failure_is_transient,
    format_command_result,
    parse_slash_command,
    pick_served_default,
    prompt_timeout_for_ceiling,
    registration_rate_limited_error,
    registration_throttle_line,
    resolve_usable_model,
)
from kiro_crew.acp.liveness import (
    EVIDENCE_ESTABLISHED_FLAT,
    EVIDENCE_MCP_SUBTREE_ACTIVE,
    EVIDENCE_PLATFORM_LIMITED,
    EVIDENCE_REMOTE_FLAT,
    EVIDENCE_SHELL_CHILD_ABSENT,
    INTERACTIVE_NARROWING_RISKS,
    INTERACTIVE_NONE,
    VERDICT_DEAD,
    VERDICT_STUCK_INPUT,
    VERDICT_UNKNOWN,
    VERDICT_WORKING,
    InFlightToolTracker,
    InteractiveClassification,
    LivenessOracle,
    ToolCallState,
    _consume_future_exception,
    boottime_now,
    classify_interactive_command,
    consult_offloaded,
    steady_now,
)
from kiro_crew.acp.mcp_reattempt import MAX_ATTEMPTS as REATTEMPT_MAX
from kiro_crew.acp.mcp_reattempt import is_recoverable_auth_failure
from kiro_crew.acp.mcp_session_report import (
    BUCKET_CAP,
    NAME_CAP,
    KasMcpReadiness,
    McpSessionReport,
    sanitize_sink_text,
)
from kiro_crew.acp.prompt_blocks import build_prompt_blocks, summarize_prompt_structure
from kiro_crew.acp.session_handle_compaction import _POST_COMPACTION_METADATA_GRACE_SECS
from kiro_crew.acp.session_handle_lifecycle import _CANCEL_GRACE_SECS
from kiro_crew.acp.session_handle_runtime_contract import (
    AcpAgentUnavailable,
    AcpFrameTooLarge,
    AcpModeNotFound,
    AcpRequestTimeout,
    AcpRuntimeDead,
    AcpRuntimeError,
    AcpRuntimeOverloaded,
    AcpRuntimeProtocol,
)
from kiro_crew.acp.session_handle_watchdog import (
    _TURN_BOUNDED_WINDOWS,
    _TURN_CEILING_WINDOW_FRACTION,
    _WORKING_LOG_INTERVAL_SECS,
    _WORKING_NEVER_LOGGED,
    _WORKING_WARN_AFTER_SECS,
    _WORKING_WARN_DEADLINE_FRACTION,
    INTERACTIVE_POLICY_CANCEL,
    INTERACTIVE_POLICY_WAIT,
    WatchdogSettings,
    _load_watchdog_settings,
    _watchdog_evidence_class,
)
from kiro_crew.acp.skill_reads import SkillReadTracker
from kiro_crew.acp.types import (
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKENDS_ADVERTISED_MODEL_SELECTION,
    ACP_BACKENDS_EFFORT_FROM_ADVERTISED_OPTION,
    ACP_BACKENDS_HOOKS_LIST,
    ACP_BACKENDS_INLINE_COMPACTION,
    ACP_BACKENDS_MODEL_EFFORT_PAIR_IDS,
    ACP_BACKENDS_MODEL_VIA_CONFIG_OPTION,
    ACP_BACKENDS_PERMISSION_KIND_FROM_TOOL_CALL,
    ACP_BACKENDS_STEER,
    ACP_BACKENDS_STEERING_REQUEST,
    ACP_BACKENDS_STRUCTURED_REFUSAL,
    ACP_BACKENDS_UNATTRIBUTED_TERMINAL_ERROR,
    EVENT_AGENT_SWITCHED,
    EVENT_CLEAR_STATUS,
    EVENT_COMPACTION_STATUS,
    EVENT_COMPLETE,
    EVENT_MCP_OAUTH_REQUEST,
    EVENT_MCP_SERVER_INIT_FAILURE,
    EVENT_MCP_SERVER_INITIALIZED,
    EVENT_STEER_CLEARED,
    EVENT_STEER_CONSUMED,
    EVENT_STEER_QUEUED,
    EVENT_STRUCTURED_STATUS,
    EVENT_SUBAGENT_ACTIVITY,
    EVENT_SUBAGENT_LIST,
    EVENT_TEXT_CHUNK,
    EVENT_TODO_UPDATE,
    EVENT_TOOL_CALL,
    EVENT_TOOL_CALL_UPDATE,
    EVENT_TOOL_RESULT,
    JSONRPC_METHOD_NOT_FOUND,
    MAX_STEERING_ANSWERS,
    MAX_STEERING_TEXT_CHARS,
    METHOD_CANCEL,
    METHOD_COMMANDS_EXECUTE,
    METHOD_KAS_MCP_STATUS,
    METHOD_KAS_SESSION_COMPACT,
    METHOD_MCP_OAUTH_REQUEST,
    METHOD_PROMPT,
    METHOD_REQUEST_PERMISSION,
    METHOD_SESSION_STEERING,
    METHOD_SET_CONFIG_OPTION,
    METHOD_SET_MODE,
    METHOD_SET_MODEL,
    MODEL_CONFIG_ID,
    OPTION_ALLOW_ALWAYS,
    OPTION_ALLOW_ONCE,
    OUTCOME_CANCELLED,
    OUTCOME_SELECTED,
    PROGRESS_SOURCE_PROCESS_EVIDENCE,
    STATUS_EXTENSION_KEY,
    STATUS_ORIGIN_LIVENESS_ORACLE,
    STATUS_PHASE_WAITING,
    STEERING_ANSWER_WAIT_SECS,
    STEERING_INJECTED,
    STEERING_STARTED_NEW_TURN,
    STOP_REASON_CANCELLED,
    STOP_REASON_COMPACTION_FAILED,
    STOP_REASON_CONTENT_FILTERED_WIRE,
    STOP_REASON_END_TURN,
    STOP_REASON_REFUSAL,
    STOP_REASON_STALE_RECOVER,
    STOP_REASON_TOOL_STALL,
    TERMINAL_TOOL_STATUSES,
    UPDATE_AGENT_MESSAGE_CHUNK,
    UPDATE_AGENT_THOUGHT_CHUNK,
    UPDATE_CURRENT_MODE,
    UPDATE_SESSION_INFO,
    WAIT_REASON_INPUT,
    AcpEvent,
    AcpPromptStats,
    JsonRpcMessage,
    StructuredStatus,
    effort_config_option_id,
    steering_outcome,
)
from kiro_crew.agent_sdk.capabilities import capabilities_for
from kiro_crew.agent_sdk.drivers.acp import EntitlementRevalidating  # noqa: F401 - raised here
from kiro_crew.config.paths import kiro_sessions_dir
from kiro_crew.constants import COMPACT_WAIT_TIMEOUT_SECS
from kiro_crew.executors import subprocess_executor
from kiro_crew.metrics.events import CHILD_PERMISSION_DENIED, emit_counter
from kiro_crew.platform.context import redact_log_via_context
from kiro_crew.recovery.ladder import InfraError, classify_infra_error
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel
from kiro_crew.trust_patterns import GrantDeadline, GrantEnded, grant_deadline_guard
from kiro_crew.validation import MAX_ACP_SESSION_ID_LEN

logger = logging.getLogger(__name__)

#: Hook executions one session may have running at once. A hook runs for up to
#: its own timeout, so this bounds the processes a peer can hold open.
_MAX_INFLIGHT_HOOK_EXECUTIONS = 4

#: The backend's hooks requests, answered by
#: :meth:`AcpSessionHandle._answer_kas_hooks_request`. A frozenset so the
#: dispatch test is one membership check. ``executeHook`` is the one that runs a
#: command, and :func:`kas_wire.hooks_execute` owns every gate it passes.
_KAS_HOOKS_METHODS = frozenset(
    {
        kas_wire.METHOD_HOOKS_LIST,
        kas_wire.METHOD_HOOKS_SESSION_START,
        kas_wire.METHOD_HOOKS_EXECUTE,
    }
)

# ── Constants ──

# Read-path entitlement revalidation (see
# ``AcpSessionHandle.maybe_refresh_available_models``). These bound how eagerly
# the dashboard picker re-asks the backend what the account can run; they are a
# scheduling policy, never an entitlement decision.
#
# A session-init snapshot captured within this many seconds of the runtime's
# spawn fell inside the startup window where the degraded (free-tier default)
# answer is resolved, so it is treated as suspect and revalidated.
_READ_PATH_SPAWN_RACE_SECS = 90.0
# A session probes on the read path at most once per this interval, so a hot
# dashboard poll does not re-probe on every runtime-probe TTL expiry forever.
# The interval binds a probe-CONFIRMED snapshot and every non-auto-only
# snapshot; an UNCONFIRMED auto-only snapshot may re-probe (it always earns a
# probe), bounded by the runtime's own single-flight probe TTL — which now
# covers the failure/empty path too, so even a failing probe is re-asked at
# most once per that TTL, not on every read.
_READ_PATH_REPROBE_MIN_INTERVAL_SECS = 300.0
# The picker read path awaits the probe at most this long, then raises
# EntitlementRevalidating so the endpoint returns its degraded (503) response
# and the frontend keeps its last-good list and polls again; the shielded probe
# keeps running and the next read serves its landed result. Kept under the
# remote-hub cold-path budget (DEFAULT_MODELS_CAPABILITY_PROXY_TIMEOUT_SECS in
# instances/constants.py: 5 + 10 + 3 < 20) so proxied /api/models never times
# out mid-revalidation.
_READ_PATH_PROBE_DEADLINE_SECS = 3.0

# How long AcpSessionHandle.set_model waits for a ``session/set_model`` reply.
# kiro-cli 2.28 answers in well under a second (measured: 0.03-0.04s), so this
# bounds only an adapter that never answers; that silence is read as accepted.
_SET_MODEL_REPLY_TIMEOUT = 3.0


# Harness-native subtask boundary (RFC §14.8, SPEC-ADDENDUM §7, parity row H16).
#: Typed refusal prefix for a ``spawn_continue``-style resume that names a
#: native child session id: the child has no conversation, task row or
#: runtime of its own, so the only resumable identity is its parent's.
NATIVE_CHILD_NOT_RESUMABLE = "native_child_not_resumable"
#: ``HostBudget.snapshot()["uncharged"]`` key under which observed native
#: children are REPORTED — never charged (they live inside the parent's
#: already-charged runtime process).
NATIVE_CHILDREN_UNCHARGED_KIND = "native_children"
#: Bound on distinct native child ids remembered per turn. Ids are
#: backend-controlled bytes; past the cap they are counted in
#: ``native_child_overflow`` rather than stored, so a flooding harness cannot
#: grow gateway memory through this set. The same number bounds
#: ``AcpRuntime._subagent_sessions``: recognition there decides a child's
#: approvals, and a roster the handle counts in full but the runtime recognises
#: only part of would split one announced roster into two governance classes.
NATIVE_CHILD_ROSTER_CAP = 4096
#: Bound on each backend-authored display string STORED in a native-child
#: roster row (name, title, status). The row count cap bounds memory only when
#: every field is bounded too — 4096 rows of an unbounded title is unbounded.
#: Far above any real sub-agent name, so a clipped label means a hostile or
#: broken payload rather than a long one.
NATIVE_CHILD_LABEL_CAP = 512


def _bounded_label(value: object) -> str:
    """One backend-authored display string, bounded for STORAGE.

    Applied at the point a native-child roster row is written, because that is
    the only place the string is RETAINED: a per-row byte bound plus the row
    cap is what makes the roster's memory claim true, and a bound applied at a
    render site downstream would leave the store itself unbounded.
    """
    return str(value)[:NATIVE_CHILD_LABEL_CAP]


# Bounds on one session's MCP launch roster (``mcp_launch_roster``), applied where
# it is RETAINED. The population is the session's MCP servers, which the session
# report already bounds by ``BUCKET_CAP`` servers and ``NAME_CAP`` name length, so
# the roster shares those two. The argv bounds are this structure's own: a real
# launch is a command and a few dozen arguments at most, and an entry past them is
# refused whole, never truncated, because a truncated argv matches a different
# process (or none) and attribution must not rest on it.
_LAUNCH_MAX_ARGS = 64
_LAUNCH_MAX_TOKEN = 4096
# One argv per source naming the server (the agent spec, the wire array), with
# room for a wire array that names a server twice.
_LAUNCHES_PER_SERVER = 4


def _launch_argv(entry: Mapping[str, Any]) -> tuple[str, ...] | None:
    """``(command, *args)`` for one stdio server entry, or None.

    None for a remote (``url``) server, which has no process, and for an entry
    whose launch cannot be predicted: no command, or ``args`` that is not a list
    of strings (the host could not start it as written either).
    """
    command = entry.get("command")
    if not isinstance(command, str) or not command.strip():
        return None
    args = entry.get("args", ())
    if args is None:
        args = ()
    if not isinstance(args, (list, tuple)) or not all(isinstance(a, str) for a in args):
        return None
    return (command, *args)


def mcp_launch_roster(
    spec: object, wire_servers: object
) -> tuple[dict[str, tuple[tuple[str, ...], ...]], int]:
    """One session's :data:`~kiro_crew.acp.liveness.LaunchRoster`, and how many
    stdio entries it refused for a bound. Pure, and never raises.

    *spec* is the agent spec the session runs (its ``mcpServers`` map is what the
    host mounts natively); *wire_servers* is the ``mcpServers`` array the session
    sent. Either may be None or malformed, which contributes nothing. An entry is
    refused whole -- no name, no argv kept -- when its name is past ``NAME_CAP``,
    its argv past :data:`_LAUNCH_MAX_ARGS` arguments or :data:`_LAUNCH_MAX_TOKEN`
    characters in any one string, its server past ``BUCKET_CAP`` servers, or its
    launch past :data:`_LAUNCHES_PER_SERVER` for that server. A refused server is
    simply not found by the scan, which keeps its full window.
    """
    roster: dict[str, list[tuple[str, ...]]] = {}
    refused = 0

    def _add(name: object, entry: object) -> None:
        nonlocal refused
        if not isinstance(name, str) or not name or not isinstance(entry, Mapping):
            return
        argv = _launch_argv(entry)
        if argv is None:
            return
        oversized = len(argv) > _LAUNCH_MAX_ARGS + 1 or any(
            len(token) > _LAUNCH_MAX_TOKEN for token in argv
        )
        known = roster.get(name)
        if not oversized and known is not None and argv in known:
            return
        if (
            oversized
            or len(name) > NAME_CAP
            or (known is None and len(roster) >= BUCKET_CAP)
            or (known is not None and len(known) >= _LAUNCHES_PER_SERVER)
        ):
            refused += 1
            return
        roster.setdefault(name, []).append(argv)

    try:
        servers = spec.get("mcpServers") if isinstance(spec, Mapping) else None
        if isinstance(servers, Mapping):
            for name, entry in servers.items():
                _add(name, entry)
        if isinstance(wire_servers, (list, tuple)):
            for element in wire_servers:
                if isinstance(element, Mapping):
                    _add(element.get("name"), element)
    except Exception:
        logger.debug("MCP launch roster unreadable", exc_info=True)
        return {}, 0
    return {name: tuple(argvs) for name, argvs in roster.items()}, refused


# How long a new prompt waits for codex steering answers still owed from the
# previous turn, so an adapter-owned turn one of them started is cancelled before
# our prompt goes out (``_settle_abandoned_steering``). The request's own wire and
# bounds are shared with ``AcpClient`` (see ``acp.types``).
_STEERING_SETTLE_SECS = 5.0


# MCP-server-init drain (parity with AcpClient._drain_notifications): after
# set_mode, briefly consume the session queue so MCP-init/oauth/config frames
# are processed before the first prompt, instead of racing into the first turn.
_MCP_DRAIN_DURATION = 1.0
_MCP_DRAIN_IDLE_EXIT = 0.25
# Hard ceiling while NO MCP server has reported yet. The idle shortcut is only
# meaningful once reporting has begun — before the first registration frame,
# queue silence just means the server is still booting (an npx-based stdio
# server spends seconds on npm resolution plus a Node boot before emitting
# anything), so the drain keeps waiting up to this ceiling instead. Sized to
# cover a realistic npx cold start (npm resolve + Node boot, observed 1-6s)
# while bounding the cost for a session whose agent config has no MCP servers
# at all — the one case that pays the full ceiling, since nothing ever arms
# the idle exit. Sessions with fast servers are unaffected: their registration
# frames are staged during session/new and arm the idle exit immediately.
_MCP_DRAIN_NO_REPORT_CEILING = 6.0
# Notification actions that count as "an MCP server reported in" for the
# drain's arming logic. OAuth requests count: a server that asks for OAuth has
# booted and reached its auth step, which is the same liveness signal.
_MCP_DRAIN_REPORT_ACTIONS = frozenset(
    {
        "mcp_server_initialized",
        "mcp_server_init_failure",
        "mcp_oauth_request",
    }
)
_SENTINEL = object()


def parse_advertised_models(resp: dict[str, Any]) -> list[dict[str, str]]:
    """The normalized advertised-model list from a ``session/new``/``session/load``
    response (``[]`` when the backend advertised nothing).

    Accepts both response shapes: a ``models`` object
    ``{availableModels: [...], currentModelId}`` and a bare list under either
    key. A falsy ``models`` value (``{}``, ``[]``, ``None``) falls through to a
    top-level ``availableModels`` — callers that gate on ``models`` themselves
    should pass a wrapped envelope (e.g. ``{"models": models}``) so the
    fallback cannot source the list from a payload their gate never saw. Normalization matches
    :meth:`AcpSessionHandle._normalize_models`, so a
    probe's answer and a session-init snapshot are directly comparable.
    """
    models = resp.get("models") or resp.get("availableModels")
    if isinstance(models, dict):
        avail = models.get("availableModels", [])
        if isinstance(avail, list):
            return AcpSessionHandle._normalize_models(avail)
        return []
    if isinstance(models, list):
        return AcpSessionHandle._normalize_models(models)
    return []


def _select_options(entries: object) -> list[dict[str, Any]]:
    """Flatten one level of provider GROUPS out of a select's option list.

    ACP lets a select group its options: an entry may carry no ``value`` of its own
    and hold its real choices in a nested ``options`` list instead. A filter that
    keeps only entries with a ``value`` therefore empties on a grouped select, and an
    empty list reads as "this harness advertised no models" -- which for a harness
    whose advertised list is the ONLY vocabulary its ``set_config_option`` accepts
    means no model can ever be offered or resolved. ONE level, deliberately: a group
    holding groups is not a shape any harness here serves, and recursing without
    bound would let a malformed payload spin. Both levels are narrowed against
    non-list wire data, so a malformed group is skipped rather than aborting the list.
    """
    flattened: list[dict[str, Any]] = []
    if not isinstance(entries, list):
        return flattened
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("value"):
            flattened.append(entry)
            continue
        nested = entry.get("options")
        if not isinstance(nested, list):
            continue
        flattened.extend(o for o in nested if isinstance(o, dict) and o.get("value"))
    return flattened


def models_from_config_options(resp: dict[str, Any], backend: str) -> dict[str, Any] | None:
    """A ``models`` envelope synthesized from a ``model`` select, or ``None``.

    Some hosts advertise no ``models`` object at all and put their model list in
    ``configOptions`` instead, as a ``select`` whose ``options`` carry the ids
    ``session/set_config_option`` accepts -- so a caller that reads only ``models``
    records nothing and the picker it feeds is empty. Gated on membership in
    ``ACP_BACKENDS_ADVERTISED_MODEL_SELECTION`` (harness-parity H6): the fold is
    only meaningful where the advertised list IS the vocabulary, and a host outside
    that set keeps whatever the static registry gave it.

    Authored once because both drivers need the same answer -- ``AcpClient`` on the
    per-session path and ``AcpSessionHandle`` on the shared-runtime one -- and a
    second copy is free to disagree about the select's shape.
    """
    if backend not in ACP_BACKENDS_ADVERTISED_MODEL_SELECTION:
        return None
    return model_select_envelope(resp)


def model_select_envelope(resp: dict[str, Any]) -> dict[str, Any] | None:
    """The ``model`` select of a session response as a ``models`` envelope, ungated.

    The shape walk behind :func:`models_from_config_options`, without its
    membership gate, for a caller that asks the select one narrow question rather
    than adopting it as the session's advertised list.
    """
    for opt in resp.get("configOptions") or []:
        if not isinstance(opt, dict) or opt.get("id") != "model" or opt.get("type") != "select":
            continue
        options = _select_options(opt.get("options"))
        if not options:
            return None
        envelope: dict[str, Any] = {
            "availableModels": [
                {
                    "modelId": o["value"],
                    "name": o.get("name") or o["value"],
                    "description": o.get("description") or "",
                }
                for o in options
            ]
        }
        current = opt.get("currentValue")
        if isinstance(current, str) and current:
            envelope["currentModelId"] = current
        return envelope
    return None


def session_models_envelope(resp: dict[str, Any], backend: str) -> Any:
    """The ``models`` payload of a session response, with the select folded in.

    One home for "where does this host's model list live", so a reader cannot know
    about the ``models`` object and not about the ``configOptions`` select. Returns
    whatever shape the response carried when it carried one, the synthesized
    envelope when it did not and the host advertises a ``model`` select, and the
    original absent value when neither applies -- so a caller's own shape branches
    stay exactly as they were.
    """
    models = resp.get("models") or resp.get("availableModels")
    if models is None or models == {} or models == []:
        models = models_from_config_options(resp, backend) or models
    return models


def advertised_models_from_session(resp: dict[str, Any], backend: str) -> list[dict[str, str]]:
    """The normalized advertised-model list for a session response, either shape.

    What a caller wants when it needs the LIST and not the envelope -- the
    entitlement probe, which re-asks the question on a throwaway session. Reading
    ``parse_advertised_models`` alone answers ``[]`` for a host whose list is a
    ``configOptions`` select, and an empty probe result is contractually "no
    evidence", so the snapshot it exists to correct would never heal.
    """
    env = session_models_envelope(resp, backend)
    if isinstance(env, dict):
        return parse_advertised_models({"models": env})
    if isinstance(env, list):
        return parse_advertised_models({"availableModels": env})
    return []


_OWNER_PREFIX = "kiro_crew.acp.session_handle_"


def _own_methods(cls: type) -> type:
    """Make each member *cls* binds from an owner module read as a method of *cls*.

    A bound function annotates ``self`` so mypy checks its body as a method body;
    this drops that annotation from the runtime object and names the function after
    the class attribute (``AcpSessionHandle._run_turn``), so its ``inspect.signature``
    and ``__qualname__`` are what the one-module class had, and so are the reprs of
    the coroutines and tasks it starts. Only ``__module__`` moves: it names the owner
    module that holds the body.
    """
    for name, member in vars(cls).items():
        function = getattr(member, "__func__", member)
        if getattr(function, "__module__", "").startswith(_OWNER_PREFIX):
            function.__annotations__.pop("self", None)
            function.__qualname__ = f"{cls.__qualname__}.{name}"
    return cls


@_own_methods
class AcpSessionHandle:
    """Handle for a single ACP session on a shared runtime.

    Owns one sessionId + asyncio.Queue. Reads events from the queue (fed by
    AcpRuntime's reader task) and provides prompt/cancel/approve/reject API.
    """

    # Fields a bound member assigns first, outside ``__init__``: declared here for type
    # checkers only, so a handle built with ``__new__`` still reads them through the
    # ``getattr`` defaults their readers pass.
    _compaction_terminal_seen: bool
    _kas_compact_turn: bool

    # The session's stale/stall watchdog: ``kiro_crew.acp.session_handle_watchdog``.
    parked_for_secs = _sh_watchdog.parked_for_secs
    _end_human_wait = _sh_watchdog._end_human_wait
    note_keepalive = _sh_watchdog.note_keepalive
    _runtime_idle_secs = _sh_watchdog._runtime_idle_secs
    is_responsive = _sh_watchdog.is_responsive
    rebind_watchdog = _sh_watchdog.rebind_watchdog
    _retire_liveness_state = _sh_watchdog._retire_liveness_state
    _consult_oracle_offloaded = _sh_watchdog._consult_oracle_offloaded
    _working_tool_bound_reached = _sh_watchdog._working_tool_bound_reached
    _log_working_deferral = _sh_watchdog._log_working_deferral
    _emit_watchdog_metric = _sh_watchdog._emit_watchdog_metric
    _end_stalled_tool = _sh_watchdog._end_stalled_tool
    # The session outside its turn loop: ``kiro_crew.acp.session_handle_lifecycle``.
    _died = _sh_lifecycle._died
    cancel = _sh_lifecycle.cancel
    send_command = _sh_lifecycle.send_command
    command_result = _sh_lifecycle.command_result
    _send_awaited = _sh_lifecycle._send_awaited
    bind_session_key = _sh_lifecycle.bind_session_key
    destroy = _sh_lifecycle.destroy
    _cleanup_transcript = _sh_lifecycle._cleanup_transcript
    cleanup_transcript_files = staticmethod(_sh_lifecycle.cleanup_transcript_files)
    _wait_for_response = _sh_lifecycle._wait_for_response
    # Context compaction: ``kiro_crew.acp.session_handle_compaction``.
    compact = _sh_compaction.compact
    _kas_compact_request = _sh_compaction._kas_compact_request
    _kas_compact_advertised = _sh_compaction._kas_compact_advertised
    _settle_kas_compaction = _sh_compaction._settle_kas_compaction
    _inline_turn_finished_cleanly = _sh_compaction._inline_turn_finished_cleanly
    _compacts_inline = _sh_compaction._compacts_inline
    wait_for_compaction = _sh_compaction.wait_for_compaction
    _drain_post_compaction_metadata = _sh_compaction._drain_post_compaction_metadata
    _track_prompt_usage = _sh_compaction._track_prompt_usage
    _track_metadata = _sh_compaction._track_metadata
    _backfill_context_window = _sh_compaction._backfill_context_window
    _settle_codex_compaction = _sh_compaction._settle_codex_compaction
    _codex_compaction_event = _sh_compaction._codex_compaction_event
    # One turn end to end: ``kiro_crew.acp.session_handle_turn``.
    wait_turn_done = _sh_turn.wait_turn_done
    prompt = _sh_turn.prompt
    stream_command = _sh_turn.stream_command
    _run_turn = _sh_turn._run_turn
    _close_abandoned_turn = _sh_turn._close_abandoned_turn
    # The per-turn event loop: ``kiro_crew.acp.session_handle_dispatch``.
    _dispatch_events = _sh_dispatch._dispatch_events
    _classify = _sh_dispatch._classify

    def __init__(
        self,
        session_id: str,
        queue: asyncio.Queue[JsonRpcMessage | None],
        runtime: AcpRuntimeProtocol,
        watchdog: WatchdogSettings | None = None,
        crew_agent: str = "",
        session_key: str = "",
        bound_cwd: str = "",
    ) -> None:
        self._session_id = session_id
        # The Kiro Crew session that OWNS this ACP session, threaded from the
        # runtime's create/load paths the way ``crew_agent`` is, and rebound on a
        # warm-pool claim. The hooks execute path keys its listed-id record and
        # its governance resolution by it; the ACP ``sessionId`` a request names is
        # host-supplied and is never used for either. Empty for a pooled session
        # nobody has claimed yet, which the execute path refuses.
        self._session_key = session_key
        self._listed_hooks = kas_wire.ListedHookStore()
        # Strong references to in-flight hook executions: the loop holds only a
        # weak one, and a collected task would leave its request unanswered.
        self._hook_tasks: set[asyncio.Task[None]] = set()
        # Same reason, for the ``session/cancel`` a late ``startedNewTurn`` steering
        # answer sends from a done-callback (see ``_steer_via_steering_request``).
        self._steering_cancel_tasks: set[asyncio.Future[None]] = set()
        # codex ``_session/steering`` answers awaiting settlement by the turn they
        # were aimed at: ``(answer, prompt generation, echo text)``.
        self._steering_answers: list[tuple[asyncio.Future[dict[str, Any]], int, str]] = []
        # Per answer in ``_steering_answers``: resolved True when the dispatch loop
        # settles it inside its turn, False when it is dropped unsettled. The steer
        # call returns this, so True always means "settled before the terminal".
        self._steering_settled: dict[asyncio.Future[dict[str, Any]], asyncio.Future[bool]] = {}
        # Answers still awaited after their turn ended, kept only so a late
        # ``startedNewTurn`` can be cancelled. Counted against the same bound as
        # ``_steering_answers`` and forgotten on ``destroy``, so the runtime's
        # registration of an answer that never comes is bounded too.
        self._abandoned_steering: list[asyncio.Future[dict[str, Any]]] = []
        # Set by a denied approval on a codex turn: nothing injected after it can
        # survive the cancel, so no later answer in the turn settles as consumed.
        self._turn_steering_denied: bool = False
        # Wrapped text of this turn's codex steers proven read before its
        # terminal; reported consumed only if the turn ends cleanly
        # (``_release_proven_steers``).
        self._steers_proven: list[str] = []
        # Wrapped text of this turn's codex steers whose ``steer()`` call has
        # already returned True. Only these are released as consumed: the
        # caller persists the steer's row synchronously on that return, so a
        # consumption report can never clear a pending entry whose row does
        # not exist yet. Matched by identity (``_release_proven_steers``).
        self._steers_accepted: list[str] = []
        # True once this turn's prompt has been written to the adapter. A codex
        # steer before that point reaches an adapter with no turn of ours and is
        # answered ``startedNewTurn``, whose untargeted cancel could then land on
        # the prompt about to go out, so no steer is sent until it is set.
        self._prompt_written: bool = False
        self.native_context_documents: dict[str, str] = {}
        self.native_context_documents_fit: Callable[[], bool] | None = None
        # Whether THIS session's array carries the member session-control entry.
        # Set by the composer that decided it (create and load alike), and read
        # by the context builder so the member operating-mode block is injected
        # only when the session actually holds the tools that block teaches.
        self.member_dispatch_mounted: bool = False
        self._queue = queue
        self._runtime = runtime
        # The directory THIS session was opened against, which on a shared runtime is
        # not the runtime's own: sessions for different projects live on one process.
        self._bound_cwd = bound_cwd
        # When True, destroy() skips the transcript unlink (subagent
        # continuability: the transcript is spawn_continue's resume material).
        self.keep_transcript = False
        self.memory_mode = "persistent"
        # Token carried by THIS session's injected broker-stub entries
        # (``mcp_gateway.claim.mint_stub_session_token``), set by the runtime
        # that created the session. It is what a later claim-push names so
        # gatewayd re-targets this session's stub connections and not every
        # session's on the shared runtime. Empty when the gateway injected no
        # stubs. Never logged: it is a bearer name for this session's identity.
        self.stub_session_token: str = ""
        # Watchdog windows are snapshotted here (construction time) so the
        # dispatch loop never reads config; the liveness oracle carries the
        # per-session evidence state (tracked child, counter samples).
        # ``crew_agent`` is the CANONICAL crew identity resolved by the surface
        # that owns it and plumbed down (see _load_watchdog_settings): it keys
        # the per-agent watchdog_tool_stall_* overrides by direct config lookup.
        # It must NEVER become an OTel metric attribute (free-form =>
        # cardinality bomb; see metrics/schema.py) — telemetry carries the
        # agent_override BOOLEAN. An explicit ``watchdog`` always wins
        # verbatim: the async creation paths (runtime.create_session /
        # load_session) resolve it off-loop and hand it in, so the synchronous
        # load below is only the fallback for direct constructions (tests).
        self._crew_agent = crew_agent
        self._watchdog = watchdog if watchdog is not None else _load_watchdog_settings(crew_agent)
        # How this session launched its stdio MCP servers, recorded by the
        # runtime once the session's array is final (``record_mcp_launches``).
        # The oracle's remote_flat scan reads it to find the in-flight tool's
        # own server process; empty means no server can be found, which keeps
        # the full window.
        self._mcp_launches: dict[str, tuple[tuple[str, ...], ...]] = {}
        self._oracle = LivenessOracle(
            sample_min_secs=self._watchdog.wellness_sample_secs,
            server_launches=self._remote_flat_launches,
        )
        # Keep the executor future, not an await-scoped flag: wait_for can time
        # out while the underlying thread continues its /proc walk. A pending
        # future makes the next watchdog tick answer UNKNOWN instead of
        # submitting a second job, so a wedged walk cannot stack blocked workers
        # in the shared subprocess_executor().
        self._consult_future: asyncio.Future[tuple[str, str]] | None = None
        # The in-flight tool hand-off rule, shared with the sub-agent reaper
        # (``InFlightToolTracker``): parallel calls can finish in either order,
        # so each attribution is retained by toolCallId until its terminal result
        # and the judged call falls back to the newest remaining one. The
        # pre-dispatch interactive classification of a shell call rides as the
        # tracker's aux payload (``None`` for a non-shell tool). ``ToolCallState``
        # does not carry the id, and the ``waiting_input`` status must name the
        # call, so the tracker keeps it too (``_inflight_tool_call_id`` below).
        # Bounded by ``MAX_ACTIVE_TOOL_CALLS``: a backend that emits distinct
        # tool_call frames whose results never report a terminal status would
        # otherwise grow the tracker for the length of the turn. At the cap the
        # OLDEST attribution is evicted and the call just dispatched is always
        # kept, so the oracle keeps reporting the tool actually in flight rather
        # than failing permissive on stall detection.
        self._tool_tracker = InFlightToolTracker(max_calls=MAX_ACTIVE_TOOL_CALLS)
        # toolCallIds that streamed output (a non-final tool_call_update with
        # content) this turn. A command that already produced output may have
        # already acted, so a non-interactive retry of it is never ``safe_retry``.
        self._tool_output_seen: set[str] = set()
        # ``kirocrew/status`` rejections this turn, keyed by reason, so the
        # ``status_rejected`` log line fires once per reason per turn rather than
        # once per frame from a misbehaving emitter.
        self._status_rejected: dict[str, int] = {}
        # Whether the ``waiting_input`` status for the CURRENT stall was already
        # yielded (the ``wait`` policy keeps the turn open across ticks and must
        # not re-emit it every tick).
        self._input_wait_emitted = False
        # Native (harness-internal) child session ids seen on child-routed
        # frames this turn — the residency count at the parent recovery
        # boundary (RFC §14.8). Reset per turn with the roster. Bounded by
        # NATIVE_CHILD_ROSTER_CAP; ids past the cap are counted, not stored.
        self._native_child_sids: set[str] = set()
        self._native_child_overflow = 0
        # Last monotonic ts a WORKING-verdict deferral was logged (rate limit).
        self._working_logged_ts = _WORKING_NEVER_LOGGED
        # Terminal compaction status captured by compact() while draining its
        # own prompt turn (kiro-cli may emit _kiro.dev/compaction/status
        # BEFORE end_turn). wait_for_compaction() consumes it first so the
        # drain never strands a caller into a spurious 120s timeout.
        self._compact_result: dict[str, str] | None = None
        # Set when a codex compaction's ``started`` frame is seen inside a turn,
        # cleared by its terminal. It guards TWO directions. A LOADED session
        # replays a past compaction as a ``tool_call`` that is already
        # ``completed``, so only a terminal following a ``started`` seen in THIS
        # turn describes work this turn did -- reading a replayed one as live would
        # reset the context meter against a window nobody just summarized. And a
        # compaction that ERRORS reports nothing at all: codex-acp's ``runCompact``
        # never resolves, so the ``session/prompt`` request goes unanswered, which
        # leaves this armed for ``_settle_codex_compaction`` at the turn's terminal.
        self._codex_compaction_pending = False
        self._cancelled = False
        # Unresponsive-cancel tracking (mirrors AcpClient._cancel_ts /
        # _cancel_grace_secs). Set by cancel(); the dispatch loop uses them to
        # unblock the caller if kiro-cli never acks the cancel.
        self._cancel_ts = 0.0
        self._cancel_grace_secs = _CANCEL_GRACE_SECS
        self._turn_done = asyncio.Event()
        self._turn_done.set()
        # Count of prompts this handle has started. A codex steering answer reads
        # it to tell "the turn the steer was aimed at" from a newer one of ours
        # (see ``_steer_via_steering_request``).
        self._prompt_starts = 0
        self._stale_eligible = False
        # Latched on this session's FIRST text chunk or tool_call and never
        # cleared: the registration-throttle death classification is refused
        # once work has been observed, so the transient verdict it hands the
        # retry ladders can only ever license replaying a session that provably
        # did nothing. Per SESSION, not per turn or per process — replay safety
        # is a fact about what THIS session's consumer may re-run, and the
        # process-level fact (a stale throttle line in a shared runtime's ring)
        # must not license replaying a sibling that already acted.
        self._prompt_or_tool_seen = False
        # Set when a genuine stale turn is probed via session/cancel; read by the
        # unresponsive-cancel branch to distinguish a confirmed wedge (signal
        # auto-recovery) from an ordinary unacked cancel (unblock caller).
        self._stale_probe = False
        self._tool_dispatched = False
        self._last_stop_reason = ""
        # The most recent tool result's infrastructure-error classification
        # (``recovery.ladder.InfraError``) or None. Public, like
        # ``last_compaction_transient``: the dashboard reads it through getattr
        # on whichever client class serves the slot.
        self.last_infra_error: InfraError | None = None
        # Monotonically increasing count of NOTIFICATION frames delivered to this
        # session by the shared queue. Incremented in _wait_for_response whenever
        # it consumes a notification (not a response) from the queue while
        # buffering for a concurrent command call. The TOCTOU guard in
        # _dispatch_events snapshots this before the oracle await and compares
        # after: an advance means a real activity frame arrived while the oracle
        # was executing (even if _wait_for_response had consumed it from the
        # queue in the meantime). Pure queue-depth checks cannot see frames that
        # are temporarily held in a concurrent consumer's buffer list.
        self._ingress_seq: int = 0
        # Consumer-park accounting, read by the idle clocks in _dispatch_events
        # and by external observers via parked_for_secs(). `_parked_total` is
        # cumulative for the turn; `_parked_since` is set only while suspended at
        # a yield in prompt().
        self._parked_total: float = 0.0
        self._parked_since: float | None = None
        # Last /api/session-keepalive touch addressed to THIS session (monotonic,
        # 0.0 for none), and `_parked_total` when it landed. Written by
        # note_keepalive(); folded into the tool-idle clock in _dispatch_events.
        # Never reset per turn: a touch from an earlier turn is older than this
        # turn's own frames, so the fold can never pick it up.
        self._keepalive_ts: float = 0.0
        self._keepalive_parked: float = 0.0
        # Monotonic timestamp of a `failed` compaction status seen this turn
        # (None otherwise). Arms the post-failure budget in _dispatch_events,
        # which ends an abandoned turn instead of draining to the ceiling.
        self._compaction_failed_at: float | None = None
        # The request id of a prompt whose consumer left before its turn ended
        # (a cancelled or closed stream), or None. The backend may still be
        # running that turn and writing its frames to this session's queue, so
        # the next turn cancels it and waits for its answer before sending
        # (``_close_abandoned_turn``).
        self._abandoned_prompt_id: int | None = None
        # Retryability of the LAST failed compaction, read by the dashboard's
        # STOP_REASON_COMPACTION_FAILED branch to decide between re-queuing the
        # abandoned message and giving up. Public (no leading underscore)
        # because that consumer reaches it through getattr on whichever of the
        # two client classes is serving the slot. Verdict only — see the twin
        # comment in AcpClient for why the reason text is not forwarded.
        self.last_compaction_transient: bool = False
        # Consumers that implement the low-fidelity child downgrade (dashboard
        # card / interactive approver) opt IN; for everyone else the handle
        # itself fail-closes low-fidelity child permission requests below, so
        # a consumer that predates the fidelity contract can never auto-approve
        # a child on agent-authored context. Full-fidelity child events flow to
        # every consumer unchanged (mode parity everywhere).
        self.child_fidelity_aware: bool = False
        # User-visible notices for permission requests this handle answered
        # itself (fail-close gate below, pre-turn drain): flushed as
        # EVENT_SUBAGENT_ACTIVITY at the next dispatch so the rejection shows
        # on the child's crew card instead of vanishing into the log.
        self._pending_reject_notices: list[tuple[str, str]] = []
        # In-flight SEL audit tasks for handle-owned permission rejections
        # (fail-close gate, pre-turn drain) — retained so they cannot be
        # garbage-collected mid-flight. Mirrors AcpRuntime._audit_tasks.
        self._audit_tasks: set[asyncio.Task[None]] = set()
        # Set when a permission event is yielded, cleared when it is answered.
        # Distinguishes "waiting for a human" (legitimate, bounded elsewhere)
        # from "the consumer stopped pulling for some other reason".
        self._awaiting_permission: bool = False
        # toolCallId -> redacted input string, written by the shared parser so a
        # later tool result can recover its originating input (mirrors AcpClient).
        self._tool_call_inputs: dict[str, str] = {}
        # Same-key provenance for ``_tool_call_inputs``.  The cached text is
        # intentionally redacted; this bit lets an approval surface fail closed
        # without retaining or forwarding the removed secret bytes.
        self._tool_call_input_redacted: dict[str, bool] = {}
        # toolCallId -> is_shell, cached from the tool_call notification so the
        # later permission_request event (which carries no trusted kind) can
        # inherit the canonical shell signal. Mirrors AcpClient's cache and is
        # the ONLY trusted source build_permission_event reads for is_shell.
        self._tool_call_is_shell: dict[str, bool] = {}
        # toolCallId -> raw structured params (dict) cached from the tool_call
        # notification so the later permission_request event can carry
        # raw_tool_params for the governance keystone (sensitive-path /
        # write-protected-config) checks. Mirrors AcpClient's _tool_call_params.
        self._tool_call_raw_params: dict[str, dict] = {}
        # Skill bodies this session's tool calls read directly, credited to the
        # usage ledger once each read completes. The same tracker AcpClient
        # owns, so a backend served by the shared runtime earns the credit a
        # per-process one does. Same per-turn lifecycle as the caches above.
        self._skill_reads = SkillReadTracker()
        # toolCallId -> path named by the tool_call's diff content block, so the
        # permission event can carry diff_path for the edit gate when the
        # params themselves carry no path key. Same lifecycle as the caches above.
        self._tool_call_diff_path: dict[str, str] = {}
        # toolCallId -> harness-classified ``kind`` from the tool_call frame, so a
        # permission_request that omits ``kind`` still reaches the gate's
        # kind-keyed tiers. Allocated like every sibling cache; whether it is
        # HANDED to the wire parsers is decided per frame by
        # ``_tool_kind_cache_for_wire`` (members of
        # ``ACP_BACKENDS_PERMISSION_KIND_FROM_TOOL_CALL`` only), so construction
        # is the same for every harness and a non-member's path is unchanged.
        self._tool_call_kind: dict[str, str] = {}
        # Emitting scopes (sessionIds) whose tool_call frames the per-turn cache
        # admission bound refused this turn (``_dispatch.admit_tool_call_cache_entry``:
        # too many distinct ids, or a frame string over its size bound). Holds
        # fixed-size scope DIGESTS (``overflow_scope_key``), never the scope text
        # -- the scope is a backend-authored string the admission may have
        # refused for its size -- one per scope, never per id, and past the
        # shared entry cap the ``OVERFLOW_EVERY_SCOPE`` marker instead, so the
        # record is bounded whatever the backend sends. Read by
        # ``_refuse_unretained_provenance``: a permission request from a recorded
        # scope whose id misses every cache is REFUSED rather than yielded,
        # because a consumer would otherwise judge a call whose kind, identity
        # and target were never retained. Same lifecycle as the caches above.
        self._tool_call_cache_overflow: set[str] = set()
        # toolCallId -> trusted MCP server name (_meta.kiro.mcpServerName) cached
        # from the tool_call notification so the later permission_request event
        # can carry mcp_server_name (empty on the permission payload). This is
        # what lets hooks.on_tool_call's app-own-server auto-approve fire on the
        # permission path. Mirrors _tool_call_is_shell.
        self._tool_call_mcp_server: dict[str, str] = {}
        # Trusted tool name (_meta.kiro.toolName) cached like _tool_call_mcp_server
        # so the permission event can rebuild mcp__<server>__<tool> for per-tool
        # governance in the app-own-server auto-approve.
        self._tool_call_tool_name: dict[str, str] = {}
        # toolCallId -> the tool's own name its tool_call frame stated, for the
        # permission event's harness_tool_id (see _dispatch.harness_tool_name).
        self._tool_call_harness_tool_name: dict[str, str] = {}
        # Per scoped toolCallId, True when the tool_call frame PRESENTED an MCP
        # identity half the name bound refused (``mcp_identity_unreadable``):
        # the call names a tool Crew cannot retain, and its permission request
        # is refused (``_refuse_unreadable_mcp_identity``) rather than judged
        # under the empty name the identity caches hold. Sparse (written only
        # when True), a sibling of the caches above under the same admission,
        # cleared, evicted and released with them.
        self._tool_call_identity_unreadable: dict[str, bool] = {}
        # Parent-scoped cache keys populated by tagged native-child tool calls.
        # Cleared per turn beside the sibling per-call caches below.
        self._native_child_tool_call_ids: set[str] = set()
        # Server names for which a mid-session MCP OAuth banner was already
        # emitted, so we don't spam duplicates. Discarded on the matching
        # server_initialized / server_init_failure so a later token-expiry
        # retry can re-surface. Instance-scoped (NOT reset per turn) — mirrors
        # AcpClient._oauth_emitted_servers.
        self._oauth_emitted_servers: set[str] = set()
        # OAuth requests collected by drain_init(). Dashboard startup drains
        # this list through AcpSessionProvider after create_session returns.
        self._pending_oauth_requests: list[dict[str, str]] = []
        # Servers whose last ``_kiro/mcp/status`` entry for this session was an
        # authorization failure and that have not connected since. Kept across a
        # timed-out sign-in, whose entry reports ``failedAuthorization`` false,
        # so the next turn offers the sign-in again.
        self._mcp_sign_in_needed: set[str] = set()
        self._mcp_sign_in_last_offered = ""
        # Tracked servers whose status reported ``connected`` and whose
        # completion the dispatch loop has not yet yielded. KAS sends no
        # server-initialized frame for a completed sign-in, so the loop yields
        # ``EVENT_MCP_SERVER_INITIALIZED`` for these itself and the dashboard
        # closes the Authorize banner.
        self._mcp_sign_in_completed: set[str] = set()
        # How many status entries the sign-in tracker last dropped past
        # ``BUCKET_CAP``; a change is logged once, so a steady overflow does not
        # repeat the warning on every snapshot.
        self._mcp_sign_in_dropped = 0
        # Servers whose last status entry for this session was ``failed`` with
        # an error that reads as a missing or refused credential (not an OAuth
        # authorization failure, which the sign-in above handles), and how many
        # times each was re-attempted. Both bounded by ``BUCKET_CAP`` names.
        # See kiro_crew.acp.mcp_reattempt.
        self._mcp_reattempt_waiting: set[str] = set()
        self._mcp_reattempt_counts: dict[str, int] = {}
        # What THIS session's MCP servers reported at init — parity with
        # AcpClient._mcp_report. On the shared runtime the frames are staged
        # per sessionId before this handle's queue exists, so the report is
        # genuinely this session's and not the process's.
        self._mcp_report = McpSessionReport()
        # (server, tool) pairs this session's agent spec switches off, set by the
        # runtime from the mirror's session projection. The wire cannot carry the
        # restriction on a mirrored host -- there is no per-tool deny channel in
        # ``mcpServers`` -- so it is honoured at the permission request instead.
        # Empty for every host whose MCP surface needs no projection, which makes
        # ``_deny_spec_disabled_tool`` a single falsy read on those sessions.
        # Mirrors ``AcpClient._spec_denied_tools``.
        self.spec_denied_tools: frozenset[tuple[str, str]] = frozenset()
        # The parsed agent spec this session's mirrored array was built from, set by
        # the runtime from the same projection. None on a host with no mirror. A
        # member session's loaded-check reads this, never a re-read of the file.
        self.consumed_agent_spec: dict[str, Any] | None = None
        self.mirror_used: bool = False
        # A member session's confirmation result, recorded by the runtime from the
        # mirrored arm: the saved template whose consumed projection matched, plus
        # the projection gaps that keep the Capabilities view "unverified". Both
        # stay at their defaults for a host with no mirror and for a session whose
        # consumed spec matched no saved intent.
        self.confirmed_projection_template: str = ""
        self.capability_projection_gaps: tuple[str, ...] = ()
        # The capabilities the agent batch this session registered auto-approves
        # (see ``kas_agents.projected_auto_approved``); None when no batch was sent.
        self.kas_auto_approved: frozenset[str] | None = None
        # The agent this session runs: the one the batch was registered for, then
        # whichever a KAS mode switch moves it to ("" with no batch). A turn that
        # names no agent runs this one, so its spec hooks are this agent's.
        self.kas_projected_agent: str = ""
        # The agent batch this session registered, so a mode switch can answer
        # what the agent it moves to auto-approves.
        self.kas_registered_agents: list[dict[str, Any]] = []
        # JSON-RPC request id -> {"once","always","reject"} optionId map, so
        # approve_tool / reject_tool echo the exact ids the agent advertised
        # (kiro "allow_once"/"allow_always"; claude-agent-acp "allow"/"reject").
        self._permission_options: dict[str | int, dict[str, str]] = {}
        # Request id -> the permission event built for it, so approve_tool can
        # put the request through the security floor (``permission_floor``)
        # whichever consumer answers it.
        self._permission_gate_events: dict[str | int, AcpEvent] = {}
        # req_ids of in-flight _wait_for_response calls (send_command /
        # set_config_option / compact). The prompt dispatch loop shares this
        # session's queue, so when it dequeues one of these responses it uses
        # this set to hand it back promptly (vs. dropping / holding to turn end).
        self._awaited_responses: set[int] = set()
        self.last_prompt_stats = AcpPromptStats()
        # State tracking (populated from session/new response via store_session_config)
        self._model: str = ""
        self.active_agent: str = ""
        # Model id kiro-cli RESOLVED the session to (from currentModelId in the
        # session/new|load response), kept separate from _model (the user-picked
        # alias) so it feeds ONLY the context-window backfill — never slot.model
        # (mirrors AcpClient._resolved_model_id; avoids the profile-id
        # pinning trap where a resolved profile id poisons slot.model).
        self._resolved_model_id: str = ""
        # True after set_mode switched a live session's agent, until the model
        # kiro-cli reported for that activation has been judged against the
        # served list (see _served_default_after_set_mode).
        self._mode_model_unjudged: bool = False
        # The model a non-strict config-option push was refused on, or the
        # startup pin the provider factory withheld because the account does not
        # serve it, or ``""`` (mirrors AcpClient.model_pin_refused). Either way
        # the session stays on the backend default without raising, so this is
        # the only trace of it.
        self.model_pin_refused: str = ""
        # The bare model a pair pin landed as when its effort was refused
        # (mirrors AcpClient.model_pin_partial).
        self.model_pin_partial: str = ""
        self._config_options: list[dict[str, Any]] = []
        self._available_models: list[dict[str, str]] = []
        # Read by KAS only: the listed id a session born on an unlisted ``auto`` moves to,
        # or "". One answer read from the ``model`` select, never the list itself:
        # nobody has shown that list to be the account's complete served set, so
        # it must not become the entitlement list the picker and the explicit-pick
        # guard narrow by.
        self._kas_auto_fallback: str = ""
        # Read-path revalidation bookkeeping (see maybe_refresh_available_models).
        # The session-init snapshot is one unconfirmed answer captured at one
        # instant, and the read path (the dashboard picker filter) has no
        # explicit-pick refusal to trigger the refresh-before-refuse path — so it
        # must decide for itself whether a snapshot that would NARROW the catalog
        # is trustworthy. These three fields are the staleness signals it reads;
        # each is a monotonic timestamp or a confirmation flag, never entitlement
        # evidence (the keep/drop verdict stays with ``catalog_row_would_drop``).
        # 0.0 = never captured (no session/new stored a list yet).
        self._available_models_captured_at: float = 0.0
        # True once a probe (refresh_available_models) has confirmed the snapshot
        # against the live backend — the strongest "trust it" signal.
        self._available_models_probe_confirmed: bool = False
        # Monotonic time the read-path heuristic last kicked a probe for THIS
        # session, so a hot dashboard poll cannot re-probe every TTL expiry
        # forever once a legitimately-narrow snapshot has been confirmed.
        self._available_models_read_probe_at: float = 0.0
        # Single in-flight read-path refresh task per handle. The read path
        # shields it, so a deadline miss raises EntitlementRevalidating (the
        # endpoint answers 503 and the client re-polls) WITHOUT cancelling the
        # probe — the task keeps running to completion so its throwaway session
        # is cleaned up and the next read serves its result.
        self._read_refresh_task: asyncio.Task[list[dict[str, str]]] | None = None
        # Last KAS mode id seen on a current_mode_update, so a re-assert of the
        # already-current mode does not surface a spurious agent-switch echo
        # (kiro-cli only emits on a real _kiro.dev/agent/switched). None = unseen.
        self._last_kas_mode_id: str | None = None
        # KAS sub-agent roster keyed by agentSubtaskId. Each entry is shaped for
        # EVENT_SUBAGENT_LIST consumption by _native_subagent_sync in chat_runner.
        self._kas_subagent_roster: dict[str, dict[str, Any]] = {}

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def prompt_or_tool_seen(self) -> bool:
        """True once this session observed a text chunk or a tool call.

        The registration-throttle death classification reads it (here and in
        ``AcpSessionProvider._translate_dead``): the transient verdict may only
        license replaying a session that provably produced no output and ran no
        tool, so the window closes at the first observed event and never
        reopens.
        """
        return self._prompt_or_tool_seen

    @property
    def prompt_outstanding_on_stall(self) -> bool:
        """Whether this session's death may have left its prompt delivered.

        True when the runtime died of a stdin stall with the child still alive
        while this session's turn was running with its prompt frame written.
        That frame is in the pipe the child stopped reading, so the child may
        still read it and act: the death is an ambiguous delivery (see
        ``AcpProcessDied``) for every such session, not only for the one whose
        write tripped the bound. The turn state is the runtime's snapshot at the
        death, so the answer holds after this turn's own teardown has run.
        """
        at_stall = getattr(self._runtime, "turn_active_at_stall", None)
        return (
            bool(self._prompt_written) and callable(at_stall) and at_stall(self._session_id) is True
        )

    @property
    def is_turn_active(self) -> bool:
        # Factor _cancelled (parity with AcpClient.has_active_turn) so a second
        # cancel() is a no-op early-return instead of re-sending session/cancel.
        # Also require the runtime alive (parity: AcpClient checks _is_process_alive)
        # so a turn on a dead runtime reads inactive -> AcpProvider.cancel() returns
        # "no_turn" instead of firing cancel_session on a corpse.
        return (not self._turn_done.is_set()) and (not self._cancelled) and self._runtime.is_alive()

    @property
    def has_unfinished_turn(self) -> bool:
        """True if the native turn has not reached its done boundary and the
        runtime is alive — INDEPENDENT of ``_cancelled`` (unlike
        :attr:`is_turn_active`).

        A turn that has been ``cancel()``'d but whose turn-done ack has not yet
        arrived still holds the native turn open; the shutdown drain must still
        wait on it before the runtime is killed, or kiro-cli's session lock is
        left held (the empty-response-after-restart bug).
        """
        return (not self._turn_done.is_set()) and self._runtime.is_alive()

    @property
    def parked_since(self) -> float | None:
        """Monotonic timestamp the current park began, or None if not parked.

        Exposed so an observer can latch on a park's IDENTITY rather than its
        duration: a park that outlives the observer's tick would otherwise be
        re-reported on every pass.
        """
        return self._parked_since

    @property
    def awaiting_permission(self) -> bool:
        """True while a permission event has been yielded and not yet answered.

        A turn parked here is waiting for a HUMAN, which is not a stall: that wait
        is already bounded by ``agent.tool_approval_timeout_secs``. An external
        observer must exclude it — otherwise every approval prompt reads as a
        stalled turn, and two components end up racing to end the same wait on
        different budgets.
        """
        return self._awaiting_permission

    # ── Tool Approval ──

    async def approve_tool(
        self,
        request_id: str | int,
        option_id: str | None = None,
        *,
        deadline: GrantDeadline | None = None,
        withdraw_if: Callable[[], bool] | None = None,
    ) -> bool:
        """Approve a pending permission request.

        ``option_id`` overrides the auto-resolved id when provided. Otherwise the
        optionIds the agent advertised (recorded by build_permission_event) are
        consulted — picking the "always" variant when the caller asked for the
        "allow_always" id, else the "once" variant. Falls back to the kiro
        literals when nothing was recorded. This keeps kiro-cli
        ("allow_once"/"allow_always") and claude-agent-acp ("allow"/"allow_always")
        working without the caller knowing the backend.

        Every approval first passes the security floor
        (:mod:`kiro_crew.permission_floor`): a request the deny floor or the
        sensitive-path checks refuse is REJECTED here, whichever consumer asked
        to approve it and whether or not that consumer consulted the gate.

        ``deadline`` is set only by an approval a timed grant backs. It is
        checked under the write lock, right before the answer is written: if it
        has passed, nothing is written, the request stays pending as it was, and
        :class:`~kiro_crew.trust_patterns.GrantEnded` tells the caller to ask
        the person instead. Without it the approval is sent as before.

        *withdraw_if* is read after the floor's thread hop and under the write
        lock right before the answer is written; once it holds, nothing is
        written, the request's records are put back, and
        :class:`~kiro_crew.permission_floor.ApprovalWithdrawn` is raised. Under
        the lock it is read ahead of the runtime's liveness check, which
        ``deadline`` follows.
        """
        gate_event = self._permission_gate_events.pop(request_id, None)
        # No recorded event means no request this transport built, so there is
        # nothing the floor could judge: refuse rather than approve unjudged.
        if gate_event is None:
            reason: str | None = permission_floor.REASON_NO_EVENT
        else:
            reason = await asyncio.to_thread(permission_floor.refusal_for, gate_event)
        if reason is not None:
            logger.warning(
                "approve_tool: security floor rejected req=%s: %s",
                _loggable_request_id(request_id),
                permission_floor.loggable_reason(reason),
            )
            await asyncio.to_thread(
                permission_floor.audit_refusal, gate_event, reason, request_id=request_id
            )
            await self.reject_tool(request_id)
            return False
        if withdraw_if is not None and withdraw_if():
            # The floor's thread hop is an await: read again before anything else
            # is taken from the request's records.
            self._restore_withdrawn(request_id, gate_event, None)
            raise permission_floor.ApprovalWithdrawn
        resolved_id = option_id
        recorded = self._permission_options.pop(request_id, None)
        if deadline is None:
            # Answered, so the turn is not waiting on a human. Also closes the
            # human-wait segment of the park so an observer does not attribute the
            # person's thinking time to the consumer (see _end_human_wait).
            self._end_human_wait()
        if recorded:
            if resolved_id is None:
                resolved_id = recorded.get("once") or recorded.get("always")
            elif resolved_id == OPTION_ALLOW_ALWAYS:
                resolved_id = recorded.get("always") or recorded.get("once") or resolved_id
            elif resolved_id == OPTION_ALLOW_ONCE:
                resolved_id = recorded.get("once") or resolved_id
        if resolved_id is None:
            resolved_id = OPTION_ALLOW_ONCE
        # The wait for the write lock is the last await before the answer goes
        # out, so the runtime reads the withdrawal again under that lock, ahead
        # of its own liveness check, and the grant's deadline after it: a runtime
        # that died during the wait reports the death, not an ended grant.
        recheck: dict[str, Callable[[], None]] = {}
        if withdraw_if is not None:
            recheck["withdrawal_check"] = lambda: permission_floor.raise_if_withdrawn(withdraw_if)
        if deadline is not None:
            recheck["before_write"] = grant_deadline_guard(deadline, request_id)
        try:
            await self._runtime.send_response(
                request_id,
                {"outcome": {"outcome": OUTCOME_SELECTED, "optionId": resolved_id}},
                **recheck,
            )
        except GrantEnded:
            # Nothing was written: put the request back exactly as it was.
            if gate_event is not None:
                self._permission_gate_events[request_id] = gate_event
            if recorded is not None:
                self._permission_options[request_id] = recorded
            raise
        except permission_floor.ApprovalWithdrawn:
            self._restore_withdrawn(request_id, gate_event, recorded)
            raise
        if deadline is not None:
            # Answered only now: until the write the request was still pending.
            self._end_human_wait()
        return True

    def _restore_withdrawn(
        self,
        request_id: str | int,
        gate_event: AcpEvent | None,
        recorded: dict[str, str] | None,
    ) -> None:
        """Put back what a withdrawn approval took, so the request is pending as before."""
        if gate_event is not None:
            self._permission_gate_events[request_id] = gate_event
        if recorded is not None:
            self._permission_options[request_id] = recorded

    async def reject_tool(self, request_id: str | int) -> None:
        """Reject a pending permission request.

        Prefers a clean ``selected`` reject using the reject optionId the agent
        advertised (claude-agent-acp offers ``reject`` → behavior:"deny",
        surfacing a clear "permission denied" rather than the cryptic "Tool use
        aborted" the adapter throws on a ``cancelled`` outcome). Falls back to
        ``cancelled`` when no deny-shaped option was advertised — NOT a per-tool
        signal: kiro-cli maps it to cancelling the TURN, auto-denying every
        later tool call in it without prompting.
        """
        recorded = self._permission_options.pop(request_id, None)
        self._permission_gate_events.pop(request_id, None)
        # Answered (see approve_tool) — a rejection ends the human wait too.
        self._end_human_wait()
        self._note_steering_denial()
        reject_id = recorded.get("reject") if recorded else None
        if reject_id:
            await self._runtime.send_response(
                request_id,
                {"outcome": {"outcome": OUTCOME_SELECTED, "optionId": reject_id}},
            )
        else:
            # Same last-resort warning as AcpClient.reject_tool — this is the
            # second of the two ``cancelled`` fallback sites, and the cascade
            # it can trigger is otherwise silent.
            logger.warning(
                "reject_tool: no deny option advertised for req=%s; answering "
                "'cancelled', which the backend may treat as cancelling the "
                "remainder of the turn's tool calls",
                _loggable_request_id(request_id),
            )
            await self._runtime.send_response(
                request_id,
                {"outcome": {"outcome": OUTCOME_CANCELLED}},
            )

    async def _deny_spec_disabled_tool(self, event: AcpEvent) -> bool:
        """Refuse a call the agent spec switched off. True when it was refused.

        The mirrored counterpart of ``AcpClient._deny_spec_disabled_tool``, and the
        identity read is the SAME function on both drivers
        (:func:`~kiro_crew.acp._dispatch.identified_mcp_call`) rather than a second
        copy of it -- a driver that read identity its own way would be a restriction
        that holds on one transport and not the other.

        Both drivers refuse on the same grounds: the pair is in this session's deny
        set, proved from a channel the model cannot reach (the preceding
        ``tool_call`` frame's own ``server``/``tool``, or the ``_meta`` identity the
        harness published). A False means "not refused BY THIS", nothing more -- an
        unidentifiable call is left to the consumer's own gate, exactly as it is on
        the client's event-yielding path.

        Refused HERE rather than after the yield, because a switched-off tool is not
        a decision to offer anyone: a consumer that auto-approves on hooks or trust
        would answer it without a human, and a human offered the choice is being
        asked to re-decide something the spec already settled.

        The audit is recorded first and runs off the loop, so it can neither delay
        the refusal nor be lost when the (bounded) reject write fails.
        """
        if not self.spec_denied_tools:
            return False
        identity = identified_mcp_call(event)
        if identity is None or identity not in self.spec_denied_tools:
            return False
        server, tool = identity
        logger.warning(
            "session MCP: refusing %r on %r -- the agent spec switches it off and this "
            "transport has no wire channel for that restriction, so it is honoured at "
            "the permission request [session=%s]",
            tool,
            server,
            self._session_id,
        )
        self._audit_handle_reject(
            event.request_id,
            f"mcp__{server}__{tool}",
            "spec_disabled_tool",
            sub_session_id=event.sub_session_id or "",
        )
        await self.reject_tool(event.request_id)
        return True

    def _report_evicted_attributions(self) -> None:
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

    def _no_tool_in_flight(self) -> bool:
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

    def _release_settled_tool_call(self, update: object, scope: str) -> None:
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

    def _tripwire_spec_disabled_tool(self, result: AcpEvent, msg: JsonRpcMessage) -> None:
        """Make a switched-off tool that RAN loud, whatever let it run.

        The two refusals above fire on a permission REQUEST, and whether codex sends
        one for a given call is the adapter's behaviour -- read from its source, not
        measured here. This is the in-band check that does not depend on it: the
        result frame for a completed call carries the same ``toolCallId`` the
        ``tool_call`` frame cached its ``rawInput = {server, tool}`` under, so a
        completed call whose pair is in the deny set is detectable from Crew's own
        side of the wire -- read under the SAME origin-scoped key the ``tool_call``
        frame wrote it under, because a bare id would miss on every session and read as
        "this call carries no identity", which is the silent direction.

        A TRIPWIRE, not enforcement -- the call has already run -- so it logs at
        WARNING and audits as a security-relevant observation. That turns an adapter
        release which stopped prompting from a silent drift into a red line in the
        log and the SEL. Cheap (two dict lookups) and a no-op with an empty deny set,
        which is every host that needs no projection.

        The same authoring as ``AcpClient._tripwire_spec_disabled_tool``, because the
        deny set has three readers on that driver and a transport carrying only two
        of them is a restriction whose failure is invisible on one side.
        """
        if not self.spec_denied_tools or not result.tool_final:
            return
        scope = str((msg.params or {}).get("sessionId") or self._session_id)
        params = self._tool_call_raw_params.get(
            scoped_tool_cache_key(scope, result.tool_call_id or "")
        )
        if not isinstance(params, dict):
            return
        server, tool = params.get("server"), params.get("tool")
        if not (isinstance(server, str) and isinstance(tool, str)):
            return
        if (server, tool) not in self.spec_denied_tools:
            return
        logger.warning(
            "session MCP: a call to %r on %r COMPLETED although the agent spec switches it "
            "off; the backend ran it without asking permission, so the per-call refusal "
            "never saw it -- check the adapter's approval behaviour [session=%s]",
            tool,
            server,
            self._session_id,
        )
        self._audit_handle_reject(
            None,
            f"mcp__{server}__{tool}",
            "spec_disabled_tool_completed",
            outcome="ran_despite_spec_disable",
        )

    async def _refuse_unidentifiable_mcp_approval(
        self, msg: JsonRpcMessage, event: AcpEvent
    ) -> bool:
        """Refuse an MCP approval this session cannot check against its deny set.

        ``AcpClient`` carries this refusal on ``_handle_permission`` alone, because
        that is its site with no human. This handle has no second site: the event it
        yields is what a consumer reads, and a consumer auto-approves by hook glob,
        by ``auto_approve_tools`` pattern and in trust mode. So "unidentified" cannot
        fall toward asking here either -- on a session whose spec switched tools off,
        an MCP tool approval whose call cannot be identified is REFUSED.

        Reachable rather than theoretical: codex marks its STANDALONE approval with
        ``_meta.is_mcp_tool_approval`` too, and a standalone one has no preceding
        ``tool_call`` frame, so nothing cached its ``(server, tool)``. Left to the
        consumer, that is a switched-off tool running on an auto-approve.

        Narrow by construction. It fires only on a session that HAS a deny set, which
        is a mirrored host whose agent spec switched something off; every other
        session reads one falsy attribute and returns. The tool name is the one thing
        this record cannot say, so it is audited as ``mcp__unidentified``.
        """
        if not self.spec_denied_tools:
            return False
        if not is_mcp_tool_approval(msg, event) or identified_mcp_call(event) is not None:
            return False
        logger.warning(
            "session MCP: refusing an MCP tool approval this session cannot identify -- "
            "the agent spec switches tools off here and an unidentified call cannot be "
            "checked against that, while a consumer may auto-approve it [session=%s]",
            self._session_id,
        )
        self._audit_handle_reject(
            event.request_id,
            "mcp__unidentified",
            "spec_disabled_tool_unidentified_call",
            sub_session_id=event.sub_session_id or "",
        )
        await self.reject_tool(event.request_id)
        return True

    async def _refuse_unreadable_mcp_identity(self, event: AcpEvent) -> bool:
        """Refuse a permission request whose MCP identity was PRESENT but unreadable.

        The tool_call frame (or this permission frame's own ``mcpTool.identity``)
        named a server or tool in a spelling the MCP-name bound refused -- longer
        than what Crew's own tool surface admits. That is not a call that names
        no tool: it is a call whose exact per-tool deny cannot be checked, and
        left to a consumer it is judged under an EMPTY identity, where a
        ``@server/tool`` rule does not bind and the title-keyed grant loop may
        approve it. So it is refused here, before any consumer sees it, on every
        harness alike (a fact about the frame, not about a backend), and audited
        as ``mcp_identity_unreadable``. A frame that names nothing is untouched.
        """
        if not event.mcp_identity_unreadable:
            return False
        logger.warning(
            "session MCP: refusing a permission request whose MCP identity is unreadable -- "
            "the frame named a server or tool longer than the tool surface admits, so the "
            "per-tool rules it is under cannot be checked and a consumer would judge it "
            "under no identity [session=%s]",
            self._session_id,
        )
        self._audit_handle_reject(
            event.request_id,
            "mcp__unreadable",
            "mcp_identity_unreadable",
            sub_session_id=event.sub_session_id or "",
        )
        await self.reject_tool(event.request_id)
        return True

    async def _refuse_unretained_provenance(self, msg: JsonRpcMessage, event: AcpEvent) -> bool:
        """Refuse a permission request for a tool call whose provenance the
        per-turn caches never retained.

        ``_dispatch.admit_tool_call_cache_entry`` refuses a tool_call frame past
        the shared count bound or over the payload bound and records the emitting
        scope in ``_tool_call_cache_overflow``. The permission request for that
        call then misses every cache: no trusted params, no shell classification,
        no identity, no kind. Left to a consumer, that request is judged by its
        title alone -- and under AUTO_APPROVE the title tier and the permission
        floor are the only tiers left, so a mounted KAS ``delete_file`` of a
        write-protected config flooded past the cap would stop being refused by
        the write-plane target gate. So a request that carries NO retained
        provenance from a scope that overflowed this turn is REFUSED here, before
        any consumer sees it; the bound narrows what is provable, never what is
        permitted. A request whose id WAS retained (any cache hit) is untouched,
        as is every request from a scope the bound never refused.
        """
        _params = msg.params if isinstance(msg.params, dict) else {}
        scope = str(_params.get("sessionId") or self._session_id)
        if not scope_overflowed(self._tool_call_cache_overflow, scope):
            return False
        if event.raw_params_trusted or event.shell_classified or event.mcp_identity_trusted:
            return False
        # The bounds are named here as well as at the admission site, so the
        # operator reading the refusal alone knows which limit a call can meet:
        # more than the count of distinct calls in flight, or one call whose
        # rendered input exceeds the payload bound (a write that large is
        # refused for its size, not its target).
        logger.warning(
            "refusing a permission request whose tool call the per-turn provenance caches did "
            "not retain (cache bound reached this turn: more than %d distinct calls in flight, "
            "or a call whose input exceeds %d chars): kind, identity and target are unknown, "
            "so it cannot be judged [session=%s]",
            TOOL_CALL_CACHE_MAX_ENTRIES,
            TOOL_CALL_CACHE_MAX_PAYLOAD_CHARS,
            self._session_id,
        )
        self._audit_handle_reject(
            event.request_id,
            event.title or "",
            "provenance_cache_overflow",
            sub_session_id=event.sub_session_id or "",
        )
        await self.reject_tool(event.request_id)
        return True

    def _audit_handle_reject(
        self,
        request_id: str | int | None,
        title: str,
        error: str,
        sub_session_id: str = "",
        outcome: str = "denied",
    ) -> None:
        """SEL-audit a permission decision this handle made ITSELF.

        ``outcome`` is a parameter because one caller is not a rejection:
        :meth:`_tripwire_spec_disabled_tool` records that a switched-off call RAN, and
        writing that down as ``denied`` would put a false record in the SEL -- the one
        place an operator goes to find out what happened. Every other caller takes the
        default.

        The fail-close fidelity gate and the pre-turn drain answer requests
        that never reach a consumer, so no consumer-side audit fires — every
        permission decision must still leave a SEL record (repo convention;
        the runtime's unregistered-session auto-reject does the same).
        Off-loop (``asyncio.to_thread``) and BEFORE the reject goes on the
        wire: sel() may do blocking filesystem work on first use, so the audit
        never delays the answer, and the reject is a bounded write that can
        fail -- the decision must leave its SEL record whether or not the wire
        accepted it (the same audit-first ordering chat_runner's deny paths
        keep). Title is backend/LLM-authored: bounded then redacted before it
        is stored.
        """
        safe_title = redact_text(str(title)[:4096])[:120] if title else "<unknown>"
        rid = request_id if isinstance(request_id, (str, int, float)) else ""
        # Hang-resilience series: handle-owned denials (fail-close fidelity
        # gate, pre-turn drain). CHILD-origin only — the pre-turn drain also
        # answers abandoned PARENT-turn requests (sub_session_id empty), and
        # counting those would corrupt the child-denial series.
        if sub_session_id:
            emit_counter(
                CHILD_PERMISSION_DENIED,
                {"surface": "session_handle", "reason": error},
            )

        def _audit() -> None:
            try:
                from kiro_crew.sel import sel

                sel().log_tool_invocation(
                    # Child rejections carry the CHILD's id in the key —
                    # attributing them only to the parent would erase the
                    # traceability the audit exists to provide.
                    session_key=(
                        f"acp:{self._session_id}:{sub_session_id}"
                        if sub_session_id
                        else f"acp:{self._session_id}"
                    ),
                    agent="kirocrew",
                    source="acp_session_handle",
                    tool_name=safe_title,
                    outcome=outcome,
                    request_id=rid,
                    error=error,
                )
            except Exception:
                logger.exception("SEL audit for handle-rejected permission failed")

        audit_task = asyncio.ensure_future(asyncio.to_thread(_audit))
        self._audit_tasks.add(audit_task)
        audit_task.add_done_callback(self._audit_tasks.discard)

    # ── Session Configuration ──

    async def set_mode(self, agent_name: str) -> None:
        """Activate an agent via session/set_mode.

        A stored skill-view name maps back to the agent it was built from first,
        so the projection sends that agent's CURRENT view, never the stored one.
        """
        from kiro_crew.acp.skill_projection import RetiredSkillView, resolve_source_agent

        try:
            agent_name = await resolve_source_agent(agent_name)
        except RetiredSkillView as exc:
            raise AcpRuntimeError(str(exc)) from exc
        # send_request only queues the request; it does not await a mode ACK.
        self.active_agent = ""
        take = getattr(self._runtime, "take_switched_model", None)
        if take is not None:
            # Only the agent/switched frame THIS activation sends may be judged.
            take(self._session_id)
        await self._runtime.send_request(
            METHOD_SET_MODE,
            set_mode_params(self._session_id, agent_name),
        )
        if take is not None:
            self._mode_model_unjudged = True

    async def _served_default_after_set_mode(self) -> None:
        """Judge the model a mid-session :meth:`set_mode` put the session on.

        kiro-cli moves the session to the model the switched-to agent's spec
        pins and reports it only in ``_kiro.dev/agent/switched``, which the
        runtime's reader records for this session as it arrives. Runs before
        each turn while a switch is unjudged: a recorded model goes through
        :meth:`ensure_served_default` the way the start paths' activation does,
        so an unserved one is moved to a served model before the prompt and a
        served one is adopted with no extra write. Until the frame has arrived
        nothing is judged and the next turn looks again; a frame without a
        model records nothing, so it changes nothing. The frame is kiro-cli's
        own, so a host that never sends it never has a model to judge.
        """
        take = getattr(self._runtime, "take_switched_model", None)
        activated = take(self._session_id) if take is not None else ""
        if not activated:
            return
        self._mode_model_unjudged = False
        await self.ensure_served_default(activated_model=activated)

    async def set_model(self, model_id: str) -> None:
        """Switch model via session/set_model.

        This is the shared-runtime SUBSTITUTE path (background one-liners, tips,
        contradiction sweep, and any caller that did not pre-guard an explicit
        user pick). ``resolve_usable_model`` maps the request to what the account
        can run: a served id is sent; a bare pin a pair-id harness serves only as
        the model half of its advertised ``<model>[<effort>]`` rows is sent as
        that bare id; ``"auto"`` is sent only when the backend
        advertises it; and anything else — ``"auto"`` on a partition that doesn't
        serve it, or an unentitled concrete id — resolves to ``""``,
        meaning **inherit the session's backend default** (the served model
        ``session/new`` assigned). So this path never puts an unserved model on
        the wire, exactly like the interactive ``_wire_model_id``
        reset-to-default. Explicit user picks raise instead, upstream in
        ``AcpSessionProvider.set_model`` / ``AcpClient.set_model``; on a harness in
        ``ACP_BACKENDS_MODEL_VIA_CONFIG_OPTION`` the adapter can still refuse a
        pick that passed those guards, and that refusal is recorded in
        ``model_pin_refused`` for the caller to read back
        (``chat_handlers._try_live_model_switch`` does).
        """
        # Each call describes only itself: a refusal recorded by an earlier call
        # must not survive a later one, or a read-after-call reports a pick that
        # landed (or inherited the default) as refused.
        self.model_pin_refused = ""
        # Backend-aware: on a ``<model>[<effort>]`` pair-id harness the advertised
        # list is the picker's vocabulary while the ``model`` config option's is
        # the BARE id, so a bare pin misses the list yet is exactly what the wire
        # takes. Without the backend this layer answers the provider's already
        # resolved pin with a SECOND withhold, and the pin is dropped after all.
        resolved = resolve_usable_model(
            model_id,
            self._advertised_model_ids(),
            backend=self._runtime.acp_backend,
        )
        if not resolved:
            # Inherit the backend default — nothing to send. For the ephemeral
            # _bg session the current model IS session/new's served default.
            return
        backend = self._runtime.acp_backend
        if backend in ACP_BACKENDS_MODEL_VIA_CONFIG_OPTION:
            # A harness whose adapter judges the model VALUE rather than only the
            # option: the spelling Crew stored may not be the spelling this build
            # serves, so the candidate ladder decides. Non-strict, because this
            # is the substitute path — an exhausted ladder means "stay on the
            # backend default", the same answer an unresolvable id gets above.
            applied = await self._push_model_config_option(resolved, strict=False)
            if not applied:
                self.model_pin_refused = resolved
                return
            # Record the spelling that actually went on the wire, not the one
            # asked for: the context meter looks the window up by this id, and a
            # fallback spelling can carry a different one.
            resolved = applied
        elif backend == ACP_BACKEND_KAS:
            # KAS implements no ``session/set_model``; the model is one of its
            # session config options instead. Same effect, different verb — so
            # the bookkeeping below is shared rather than duplicated.
            #
            # Deliberately NOT folded into the branch above, and KAS is
            # deliberately absent from that membership set: KAS advertises the
            # option and accepts the resolved id, so a ladder here would only add
            # retries after a failure that is already terminal, and it would turn
            # a refusal into a silent stay-on-default where today it raises.
            await self.set_config_option(MODEL_CONFIG_ID, resolved)
        else:
            # Waits for the adapter's answer so an ERROR reply is not dropped by
            # the pre-turn drain while the bookkeeping below records a model the
            # session is not serving. A refusal follows the contract of the
            # config-option branch above: the session stays on what it serves
            # and the refused id is recorded for the caller to read back, so the
            # dashboard answers the pick with a 4xx instead of a reset. Silence is
            # not a refusal: an adapter that never answers this request kept the
            # unawaited behaviour, so a timeout records the switch as before.
            req_id = await self._send_awaited(
                METHOD_SET_MODEL,
                set_model_params(self._session_id, resolved),
            )
            try:
                await self._wait_for_response(req_id, timeout=_SET_MODEL_REPLY_TIMEOUT)
            except AcpTimeoutError:
                logger.info(
                    "session/set_model(%s) on %s got no reply within %.0fs; recording it",
                    resolved,
                    self._session_id,
                    _SET_MODEL_REPLY_TIMEOUT,
                )
            except AcpProcessDied:
                raise
            except AcpError as exc:
                logger.warning(
                    "session/set_model(%s) on %s was refused: %s",
                    resolved,
                    self._session_id,
                    redact_log_via_context(str(exc)),
                )
                self.model_pin_refused = resolved
                return
        self._model = resolved
        self.model_pin_refused = ""
        if getattr(self, "_mode_model_unjudged", False):
            # The session runs this model now, not the one an earlier set_mode
            # activation put it on, so that activation's frame is not judged.
            self._mode_model_unjudged = False
            take = getattr(self._runtime, "take_switched_model", None)
            if take is not None:
                take(self._session_id)
        # Parity with AcpClient.set_model: keep _resolved_model_id in sync so
        # _backfill_context_window looks up the NEW model's window after a switch
        # (otherwise the context meter converts pct against the stale session/new
        # model until the next session refresh).
        self._resolved_model_id = resolved
        # Also rebase the meter stats themselves — the old model's window and
        # its authoritative usage_update no longer describe this session
        # (mirrors AcpClient.set_model).
        win = (
            model_registry.model_window(resolved)
            if model_registry.has_known_window(resolved)
            else None
        )
        self.last_prompt_stats.rebase_to_window(win or 0)

    async def _push_model_config_option(self, model_id: str, *, strict: bool) -> str:
        """Push the model over ``session/set_config_option``, trying each spelling.

        Returns the spelling that was accepted, or ``""`` when none was and the
        session must stay on the backend default.

        Three answers are possible when a write fails, and conflating any two of
        them breaks a session:

        * ``unknown config option`` — this adapter build has no model option at
          all, so no spelling can help. Strict re-raises; otherwise the caller
          stays on the default.
        * ``config option model`` in the message — the adapter named the option
          while refusing the VALUE, so the next spelling is worth trying.
        * a bare ``-32602`` — the request shape here is fixed and the value is
          the only thing that varies, so the code IS the refusal. Read as a
          protocol failure instead, it re-raises, the session init fails, and a
          model pin carried over from another harness kills every session at
          startup.

        Anything else is a transport or protocol fault and must keep propagating:
        swallowing it would report a model switch that never reached the process.

        ``strict=True`` (an explicit user pick) raises
        :class:`~kiro_crew.acp.client.AcpModelUnavailable` on exhaustion, because
        reporting success while running something else is worse than failing.
        ``strict=False`` (a substitute or inherited value) returns ``""``.
        """
        # Each push describes only itself; the split below sets it again.
        self.model_pin_partial = ""
        last_exc: AcpError | None = None
        # ONE home for the spelling ladder, imported rather than copied: the
        # order is a fact about how a model id is spelled on the wire, not about
        # which driver is asking, and two copies of an ORDER drift silently --
        # both still return candidates and only a cold cache on one driver shows
        # which list ran.
        for cand in AcpClient._model_config_candidates(model_id):
            try:
                await self.set_config_option(MODEL_CONFIG_ID, cand)
            except AcpError as exc:
                lowered = str(exc).lower()
                if "unknown config option" in lowered:
                    if strict:
                        raise
                    logger.debug(
                        "adapter exposes no %r config option; skipping model push",
                        MODEL_CONFIG_ID,
                    )
                    return ""
                if not _is_config_value_rejection(exc, MODEL_CONFIG_ID, self._runtime.acp_backend):
                    raise
                last_exc = exc
                continue
            if cand != model_id:
                logger.info(
                    "ACP model %r rejected by the adapter; applied fallback spelling %r",
                    model_id,
                    cand,
                )
            return cand
        # Every spelling refused as one value. A ``<model>[<effort>]`` pair --
        # the shape codex-acp advertises but its ``model`` option does not take --
        # is applied as its two halves instead (shared seam with AcpClient).
        split_applied = await _push_model_via_effort_split(
            self, self._runtime.acp_backend, model_id
        )
        if split_applied:
            return split_applied
        # Redacted through the platform context before the id reaches a log or an
        # exception message: it is caller-supplied text on a path that ends up in
        # front of a user, and this process can compose a companion redactor -- so
        # the baseline two-pass call would scan it with the weaker pass. Same call
        # this file already makes for the unserved-default warning, which is the
        # same shape of value going to the same kind of place.
        _rejected_log = redact_log_via_context(str(model_id))
        advertised_ids = self._advertised_model_ids()
        if strict:
            raise AcpModelUnavailable(
                _rejected_log,
                advertised_ids,
                backend=self._runtime.acp_backend,
                # Only a pair-id harness earns the adapter-mismatch wording: on
                # those the advertised list IS the entitlement, so refusing
                # something on it is the adapter contradicting itself. Elsewhere an
                # advertised id may simply be out of the account's reach, and the
                # entitlement wording plus the `whoami` hint is the true answer.
                advertised_but_refused=(
                    model_id in advertised_ids
                    and self._runtime.acp_backend in ACP_BACKENDS_MODEL_EFFORT_PAIR_IDS
                ),
            ) from last_exc
        logger.warning(
            "ACP model %s rejected by the adapter; staying on the backend default %s "
            "(advertised: %s)",
            _rejected_log,
            self._resolved_model_id or DEFAULT_MODEL,
            ", ".join(advertised_ids) or "none",
        )
        return ""

    async def steer(self, message: str) -> bool:
        """Inject a mid-turn steer into the running turn.

        Two transports. kiro-cli takes its ``_session/steer`` ext-method
        fire-and-forget (mirrors AcpClient.steer): the reply streams back inside
        the SAME in-flight session/prompt, the steering_consumed notification is
        the authoritative signal (surfaced as EVENT_STEER_CONSUMED), and the
        request response is not awaited. A backend in
        ``ACP_BACKENDS_STEERING_REQUEST`` (codex) is delivered by
        :meth:`_steer_via_steering_request`, which awaits only the adapter's
        answer: True means codex accepted the steer, not that the turn consumed
        it. Consumption is reported separately, as EVENT_STEER_CONSUMED at the
        turn's clean terminal, and a steer never reported consumed is queued by
        the caller. Returns False for an empty message or no active session on
        either transport.
        """
        text = (message or "").strip()
        if not text or not self._session_id:
            return False
        if self._runtime.acp_backend in ACP_BACKENDS_STEERING_REQUEST:
            return await self._steer_via_steering_request(text)
        wrapped = f"<user_message>\n{text}\n</user_message>"
        await self._runtime.send_request(
            "_session/steer",
            {"sessionId": self._session_id, "message": wrapped},
        )
        # Stamped HERE, at the innermost write, because this is the one point
        # every steer funnels through: the dashboard steers the inner client
        # directly while the IM transports steer the provider wrapper, and both
        # end up on this line. A reader of the stamp therefore needs no
        # per-transport wiring. See ``last_steer_monotonic``.
        self._last_steer_monotonic = time.monotonic()
        return True

    async def _steer_via_steering_request(self, text: str) -> bool:
        """Deliver a user steer over codex-acp's ``_session/steering`` request.

        See ``ACP_BACKENDS_STEERING_REQUEST`` for what was measured. The adapter
        sends no ``steering_consumed`` echo, so the request's own answer is the
        only delivery evidence. It is awaited until it arrives, the turn it was
        aimed at ends, or ``STEERING_ANSWER_WAIT_SECS`` pass, whichever is first;
        the bound exists because the dashboard composer awaits this call inside a
        send request the browser aborts.

        * ``injected`` -- True is returned, which means accepted, not consumed:
          the caller keeps its pending entry for the text. The answer stays
          registered with the turn it was aimed at, whose dispatch loop proves it
          was read before the terminal (at a between-frames point with nothing
          buffered, see :meth:`_take_injected_steers`) and reports it as
          ``EVENT_STEER_CONSUMED`` only when that turn ends cleanly and this call
          has already returned True (see :meth:`_release_proven_steers`). A turn that ends on a denied approval,
          a cancel, a refusal or an error reports nothing, and the caller's
          teardown queues the pending text, so a steer codex dropped with its
          turn runs again instead of being lost. An ``injected`` answered after
          its turn ended returns False.
        * ``startedNewTurn`` -- no turn was running by the time the adapter looked,
          so it began one that no ``session/prompt`` of ours owns. That turn is
          cancelled and False is returned, which sends the caller down its queue
          path: the text runs once, as the next turn Crew starts itself.
        * ``failed``, an error answer, or an answer of any other shape -- False, and
          the caller queues it.
        * the turn ends before any answer -- False, and the caller queues it. A late
          ``startedNewTurn`` is still cancelled.

        This is at-least-once, not exactly-once. codex-acp guarantees no ordering
        between the steering answer and the prompt's terminal, and ``injected`` is
        an ack that the text entered the turn's input, not a consumption receipt.
        When the terminal is read first, or is already buffered when the answer
        is looked at, or the turn ends in a way that may have dropped the text,
        the steer is not reported consumed and the caller queues it, so a steer
        codex did act on in that window runs a second time -- visibly, as its
        own turn. That is the requeue path's documented cost
        (``_requeue_unconsumed_steers``), preferred to the silent loss the
        opposite choice produces.

        The ``is_turn_active`` and ``_prompt_written`` checks are what keep
        ``startedNewTurn`` rare: a steer is only sent once a prompt of ours has been
        written and is still in flight, so the adapter can only start a turn of its
        own when ours ends inside the request's round trip, and never while our
        prompt is still being built.

        Nothing is sent while the turn is parked on an approval. codex answers that
        request ``injected``, but its only reject option cancels the turn and drops
        what was injected with it (see ``ACP_BACKENDS_STEER``), so a steer settled
        there would be reported delivered and never run. Returning False sends it
        down the queue path instead, and it runs as the next turn. The same goes
        for a steer past ``MAX_STEERING_TEXT_CHARS`` or while
        ``MAX_STEERING_ANSWERS`` are held (the cap counts unanswered, abandoned
        and proven-but-unreported steers).
        """
        if not self.is_turn_active or not self._prompt_written or self._awaiting_permission:
            return False
        held = (
            len(self._steering_answers) + len(self._abandoned_steering) + len(self._steers_proven)
        )
        if len(text) > MAX_STEERING_TEXT_CHARS or held >= MAX_STEERING_ANSWERS:
            return False
        session_id = self._session_id
        aimed_at = self._prompt_starts
        turn_done = self._turn_done
        # Registered from inside the write, before its drain() can suspend: the
        # reader may resolve the answer during that suspension, and the dispatch
        # loop must already hold it when the turn's next frame arrives.
        entry: list[tuple[asyncio.Future[dict[str, Any]], int, str]] = []

        def _register(fut: "asyncio.Future[dict[str, Any]]") -> None:
            entry.append((fut, aimed_at, f"<user_message>\n{text}\n</user_message>"))
            self._steering_answers.append(entry[0])
            self._steering_settled[fut] = asyncio.get_running_loop().create_future()

        def _unregister() -> None:
            # Every exit that does not return True revokes the steer, wherever the
            # dispatch loop has put it: still registered, or already proven and
            # waiting for the clean-terminal report. A proven entry left behind
            # would be reported consumed to a caller that never saw True. Matched
            # by identity, so an identical text sent twice keeps the other one.
            if not entry:
                return
            if entry[0] in self._steering_answers:
                self._steering_answers.remove(entry[0])
            else:
                for i, proven in enumerate(self._steers_proven):
                    if proven is entry[0][2]:
                        del self._steers_proven[i]
                        break
            self._steering_settled.pop(entry[0][0], None)

        try:
            answer = await self._runtime.send_request_for_answer(
                METHOD_SESSION_STEERING,
                {"sessionId": session_id, "prompt": [{"type": "text", "text": text}]},
                on_registered=_register,
            )
        except BaseException as exc:
            # The request may already be registered (``_register`` runs before the
            # write, and the write's ``drain()`` can suspend). Drop that registration
            # on every exit, cancellation included, or its answer would later settle
            # this steer as consumed while the caller, which never saw True, has
            # already moved on: the text would run with no transcript row and no
            # requeue. Unsettled, the caller's own bookkeeping queues it instead.
            if entry:
                self._runtime.forget_request(entry[0][0])
            _unregister()
            if not isinstance(exc, Exception):
                raise
            if getattr(exc, "ambiguous_delivery", False) is True:
                # The frame WAS written and sits in a pipe the live child may
                # still read: not "not written". Raised so the caller requeues
                # the steer as possibly delivered instead of as fresh text.
                raise
            logger.debug("steering request not written for %s: %s", session_id, type(exc).__name__)
            return False

        def _cancel_unowned_turn() -> None:
            # The turn the adapter started runs outside any prompt of ours, so
            # nothing would read or bound it; the caller queues the text instead.
            # ``session/cancel`` names no turn, so once a NEWER prompt of ours is
            # running it would cancel that one instead -- the user's queued turn.
            # ``_settle_abandoned_steering`` resolves these answers before the next
            # prompt starts, so this branch is a backstop, not the normal path.
            if self._prompt_starts != aimed_at and self.is_turn_active:
                logger.warning(
                    "steer on %s started an adapter-owned turn after our next prompt "
                    "began; not cancelling, it would hit ours",
                    session_id,
                )
                return
            logger.info(
                "steer on %s started an adapter-owned turn; cancelling it and queueing",
                session_id,
            )

            async def _send_cancel() -> None:
                # Best-effort: a dead adapter has no turn left to cancel, and the
                # failure must not surface as an unhandled task exception.
                try:
                    await self._runtime.send_notification(METHOD_CANCEL, {"sessionId": session_id})
                except Exception as exc:
                    logger.warning(
                        "cancel of adapter-owned turn on %s failed: %s",
                        session_id,
                        type(exc).__name__,
                    )

            task = asyncio.ensure_future(_send_cancel())
            self._steering_cancel_tasks.add(task)
            task.add_done_callback(self._steering_cancel_tasks.discard)

        def _late_answer(fut: "asyncio.Future[dict[str, Any]]") -> None:
            if fut.cancelled() or fut.exception() is not None:
                return
            outcome = steering_outcome(fut.result())
            if outcome == STEERING_STARTED_NEW_TURN:
                _cancel_unowned_turn()
            elif outcome == STEERING_INJECTED:
                logger.warning("steering answer on %s: injected after its turn ended", session_id)

        def _abandon() -> None:
            # Stop tracking this answer for settlement, but keep it awaited within
            # the bound so a late ``startedNewTurn`` is still cancelled and the
            # runtime's registration of it stays counted until it resolves.
            _unregister()
            if answer.done():
                # Already answered (a cancelled caller can unwind in the same loop
                # iteration the answer resolved in): act on it now, so a
                # ``startedNewTurn`` still gets its cancel.
                _late_answer(answer)
                return
            self._abandoned_steering.append(answer)

            def _release(fut: "asyncio.Future[dict[str, Any]]") -> None:
                if fut in self._abandoned_steering:
                    self._abandoned_steering.remove(fut)

            answer.add_done_callback(_release)
            answer.add_done_callback(_late_answer)

        ended = asyncio.ensure_future(turn_done.wait())
        try:
            waiters: set[asyncio.Future[Any]] = {answer, ended}
            await asyncio.wait(
                waiters, timeout=STEERING_ANSWER_WAIT_SECS, return_when=asyncio.FIRST_COMPLETED
            )
        except BaseException:
            # A cancelled caller cannot settle this steer, but the runtime still
            # owes its answer, so the answer moves to the abandoned set.
            _abandon()
            raise
        finally:
            ended.cancel()
        if getattr(self._runtime, "stdin_stall_death", False) is True and not (
            answer.done() and not answer.cancelled() and answer.exception() is None
        ):
            # The frame was written, then the runtime died of a stdin stall
            # before answering: the live child may still read and inject it.
            if answer.done():
                _unregister()
            else:
                _abandon()
            raise AcpRuntimeDead(
                "runtime died of a stdin stall after the steering frame was written",
                ambiguous_delivery=True,
            )
        if not answer.done():
            # The turn ended first, or the answer outlasted the bound the caller's
            # own request can wait: the caller queues the text.
            _abandon()
            return False
        if answer.cancelled() or answer.exception() is not None:
            # An error answer: an adapter build without the method answers -32601.
            _unregister()
            # The class only: the error text is the adapter's, and a log line is not
            # the place to carry whatever it chose to put there.
            logger.info(
                "steering request on %s refused: %s",
                session_id,
                "cancelled" if answer.cancelled() else type(answer.exception()).__name__,
            )
            return False
        outcome = steering_outcome(answer.result())
        if outcome == STEERING_STARTED_NEW_TURN:
            _unregister()
            _cancel_unowned_turn()
            return False
        if outcome != STEERING_INJECTED:
            _unregister()
            return False
        if turn_done.is_set() or self._prompt_starts != aimed_at:
            # Answered, but its turn is already over: nothing will report it
            # consumed, so the caller queues the text.
            _unregister()
            return False
        # ``injected``: accepted. The entry stays registered for this turn's
        # dispatch loop, which reports it consumed only when the turn ends
        # cleanly (see ``_release_proven_steers``); on a denial, a cancel or any
        # other ending it reports nothing, and the caller's pending entry for the
        # text is queued by the turn's teardown instead of being lost.
        self._steering_settled.pop(answer, None)
        self._steers_accepted.append(entry[0][2])
        self._last_steer_monotonic = time.monotonic()
        return True

    async def _settle_abandoned_steering(self) -> None:
        """Resolve codex steering answers owed from the last turn before a new prompt.

        A steer whose turn ended before its answer can still be answered
        ``startedNewTurn``: the adapter then runs a turn of its own on this
        session. Its cancel is sent from the answer's callback, and
        ``session/cancel`` names no turn, so it is only safe while no prompt of
        ours is running. This runs before the next prompt starts, waits up to
        ``_STEERING_SETTLE_SECS`` for those answers, and waits for the cancels
        they schedule, so an adapter-owned turn is stopped before our prompt is
        written and none of its updates reach that prompt's queue. An answer
        still missing after the bound is forgotten and one ``session/cancel`` is
        sent in its place, which stops a turn it may already have started.
        """
        pending = [a for a in self._abandoned_steering if not a.done()]
        if pending:
            await asyncio.wait(pending, timeout=_STEERING_SETTLE_SECS)
        # Let the answers' callbacks run and schedule their cancels.
        await asyncio.sleep(0)
        unanswered = [a for a in self._abandoned_steering if not a.done()]
        for answer in unanswered:
            self._runtime.forget_request(answer)
        self._abandoned_steering = []
        if unanswered:
            logger.warning(
                "%d steering answer(s) on %s never arrived; cancelling before the next prompt",
                len(unanswered),
                self._session_id,
            )
            try:
                await self._runtime.send_notification(
                    METHOD_CANCEL, {"sessionId": self._session_id}
                )
            except Exception as exc:
                logger.warning(
                    "pre-prompt cancel on %s failed: %s", self._session_id, type(exc).__name__
                )
        if self._steering_cancel_tasks:
            await asyncio.gather(*list(self._steering_cancel_tasks), return_exceptions=True)

    def _take_injected_steers(self, can_settle: bool = True) -> list[str]:
        """Echo text for every codex steer this turn now knows was ``injected``.

        Called by the prompt dispatch loop between frames: after the previous
        frame is fully handled and before the next is dequeued. ``can_settle`` is
        True only while nothing is buffered on the session queue. Kiro's
        reader resolves an answer inline and routes frames into that queue
        inline, one stdout line at a time, so an answer that is done while the
        queue is empty was read after every frame already handled and before
        the terminal, which would otherwise have ended the turn. Anywhere
        else the order cannot be read, so nothing settles: the entries stay, and
        a turn that ends with them unsettled has its steers queued by the caller
        (a visible duplicate at worst, never a steer marked delivered to a turn
        that had finished). Entries aimed at an earlier prompt are dropped
        unsettled: that turn's teardown already requeued them, and settling one
        against a newer turn would mark a steer delivered that this turn never
        received. Unanswered entries stay registered.
        """
        if (
            not self._steering_answers
            or not can_settle
            or self._turn_steering_denied
            or self._cancelled
        ):
            return []
        flags = getattr(self, "_steering_settled", {})
        settled: list[str] = []
        waiting: list[tuple["asyncio.Future[dict[str, Any]]", int, str]] = []
        for answer, aimed_at, wrapped in self._steering_answers:
            flag = flags.get(answer)
            ok = False
            if aimed_at != self._prompt_starts:
                pass
            elif not answer.done():
                waiting.append((answer, aimed_at, wrapped))
                continue
            elif not answer.cancelled() and answer.exception() is None:
                ok = steering_outcome(answer.result()) == STEERING_INJECTED
            if ok:
                settled.append(wrapped)
            if flag is not None and not flag.done():
                flag.set_result(ok)
        self._steering_answers = waiting
        return settled

    def _release_proven_steers(self, reason: str, refusal: Any) -> list[str]:
        """Echo text for this turn's proven codex steers, at a clean terminal only.

        codex's reject and ``session/cancel`` both end the turn and drop what was
        injected into it, so a steer is reported consumed only once the turn it
        entered has ended normally: no denied approval, no cancel, and a stop
        reason of exactly ``end_turn``. That is an allowlist on purpose: a bare
        wire ``refusal`` carries no ``RefusalInfo``, and error reasons such as a
        tool stall are not clean endings either. On any other ending the
        proven steers are dropped here unreported, and the caller's pending entry
        for each is queued by the turn's teardown, so the text runs again rather
        than being lost. The list is emptied either way.

        A proven steer whose ``steer()`` call has not yet returned True is not
        released either, even at a clean ``end_turn``. The caller writes the
        steer's transcript row synchronously on that return, so a report ahead
        of it would clear the pending entry before any row exists, and a
        shutdown landing in between would lose both. Held back, the entry stays
        pending: the caller resumes after the turn is done and returns False,
        and the teardown queues the text. That is the requeue path's visible
        duplicate, not a loss, and nothing on the terminal path waits on it.
        """
        proven, self._steers_proven = self._steers_proven, []
        accepted, self._steers_accepted = self._steers_accepted, []
        if (
            self._cancelled
            or self._turn_steering_denied
            or refusal
            or reason != STOP_REASON_END_TURN
        ):
            return []
        return [p for p in proven if any(p is a for a in accepted)]

    def _note_steering_denial(self) -> None:
        """Record that an approval in this codex turn was denied.

        codex offers no per-tool reject: its reject cancels the turn and drops
        what was injected into it. Nothing in the turn is then reported consumed
        (see ``_release_proven_steers``), so every steer injected into it is
        queued by the caller. A no-op on every other backend, whose reject does
        not discard injected text.
        """
        if self._runtime.acp_backend not in ACP_BACKENDS_STEERING_REQUEST:
            return
        self._turn_steering_denied = True

    # Monotonic stamp of the last steer handed to the backend, 0.0 when this
    # session has never been steered. Read by the dashboard's keepalive route to
    # decide whether a sleeping `wait` should return early: a steer can only be
    # injected at a model-inference boundary, and an in-flight tool call is the
    # absence of one, so a sleep that outlasts the steer would hold the user's
    # correction in the backend's queue until it elapses.
    #
    # Deliberately monotonic, not wall clock: it is only ever compared against
    # another monotonic stamp taken in the same process (the sleep's start), and
    # mixing the two clocks is how a suspend-resume silently reorders them.
    _last_steer_monotonic: float = 0.0

    @property
    def last_steer_monotonic(self) -> float:
        """Monotonic time of the last steer written to the backend (0.0 if none)."""
        return self._last_steer_monotonic

    @property
    def supports_steer(self) -> bool:
        """True when this session's host takes a user's mid-turn message.

        Membership in ``ACP_BACKENDS_STEER`` (harness-parity H6) or in
        ``ACP_BACKENDS_STEERING_REQUEST``, read from the runtime's own backend id.
        The first is kiro-cli's ``_session/steer``, the same answer, from the same
        table, that ``AcpClient.supports_steer`` gives; the second is codex-acp's
        ``_session/steering``, which only this handle speaks. A capability is
        granted by opt-in membership, so a host the runtime learns to drive does
        not inherit an extension it never demonstrated: answering True for one
        would advertise the steer affordance and then meet the user's mid-turn
        correction with ``-32601``.
        """
        backend = self._runtime.acp_backend
        return backend in ACP_BACKENDS_STEER or backend in ACP_BACKENDS_STEERING_REQUEST

    @property
    def supports_refusal_steer(self) -> bool:
        """True when a deny notice steered into the refused turn reaches the model.

        Narrower than :attr:`supports_steer`: only ``ACP_BACKENDS_STEER``. codex
        takes a user's steer, but its approval answer cancels the turn and the
        injected text goes with it, so a deny notice there keeps the recovery
        continuation instead.
        """
        return self._runtime.acp_backend in ACP_BACKENDS_STEER

    @property
    def steer_needs_loss_recovery(self) -> bool:
        """True when an accepted steer can still be dropped with its turn.

        codex (``ACP_BACKENDS_STEERING_REQUEST``) drops injected text when a later
        approval in the turn is denied or the turn is cancelled, so an accepted
        steer is reported consumed only at a clean terminal and the caller must
        keep and requeue it otherwise. Only the dashboard composer, whose pending
        entry the turn's teardown requeues, steers such a session; the
        provider-wrapper surfaces (messaging channels, Side Chat, ``spawn_steer``)
        read this and queue instead.
        """
        return self._runtime.acp_backend in ACP_BACKENDS_STEERING_REQUEST

    async def set_config_option(self, config_id: str, value: str) -> None:
        """Set a session config option (e.g. effort level).

        Sends session/set_config_option JSON-RPC request. When the response
        carries the session's full ``configOptions`` (the ACP response shape),
        they replace the cached ones: a write can change which options exist --
        KAS serves its ``effortLevel`` select only once a concrete model is set --
        and the matching ``config_option_update`` notification sits buffered on
        the queue until the next turn drains it, so a follow-up write gated on
        :meth:`supports_config_option` would otherwise read the stale list.
        """
        req_id = await self._send_awaited(
            METHOD_SET_CONFIG_OPTION,
            {"sessionId": self._session_id, "configId": config_id, "value": value},
        )
        msg = await self._wait_for_response(req_id, timeout=10.0)
        result = getattr(msg, "result", None)
        config_options = result.get("configOptions") if isinstance(result, dict) else None
        if isinstance(config_options, list):
            self._config_options = config_options
            self._sync_effort_levels()

    async def apply_session_permission_routing(self) -> None:
        """Make a ``SESSION_CONFIG`` harness actually ask, or refuse to run it.

        Self-gating on the harness's routing mechanism, so it is inert for every
        harness whose privileged tools are made to ask some other way: kiro-cli
        and the KAS relay both route through the agent spec
        (``Routing.AGENT_SPEC``), so the body below never runs for them and
        neither backend sends an extra write.

        Two outcomes, and each is a different verdict on purpose:

        * the option was not advertised -> ``INDETERMINATE``, because Crew cannot
          tell what the adapter will do, and "cannot tell" must not read as armed;
        * the write was rejected -> ``BYPASSED``, an observed failure rather than
          missing evidence.

        Only the enforced mechanisms refuse, and ``enforce_runtime_routing`` owns
        that decision, so the scope is not re-derived here.
        """
        backend = self._runtime.acp_backend
        if acp_tool_gate.routing_for(backend) is not acp_tool_gate.Routing.SESSION_CONFIG:
            return
        if self.mirror_used and self._runtime._member_projection_needs_confirmation(
            self.mirror_used
        ):
            await self._runtime._confirm_member_projection(self)
        option_id, value = acp_tool_gate.permission_config_for(backend)
        issue = acp_tool_gate.session_config_issue(backend, self._config_options)
        if issue:
            # Not advertised: INDETERMINATE, never BYPASSED. The adapter may well
            # ask anyway; Crew simply has no evidence, and the enforcement treats
            # the two identically while the message stays honest.
            try:
                acp_tool_gate.enforce_runtime_routing(
                    backend,
                    issue,
                    verdict=acp_tool_gate.Verdict.INDETERMINATE,
                    remedy=acp_tool_gate.remediation_for(backend),
                )
            except acp_tool_gate.ToolGateUnroutable as exc:
                raise AcpToolGateUnroutable(str(exc)) from None
            return

        try:
            await self.set_config_option(option_id, value)
        except AcpError as exc:
            # The option was advertised and the write still failed, so this is an
            # observed bypass rather than missing evidence.
            try:
                acp_tool_gate.enforce_runtime_routing(
                    backend,
                    "the adapter rejected its required session permission configuration",
                    verdict=acp_tool_gate.Verdict.BYPASSED,
                    remedy=acp_tool_gate.remediation_for(backend),
                )
            except acp_tool_gate.ToolGateUnroutable as gate_exc:
                raise AcpToolGateUnroutable(str(gate_exc)) from exc
            return

        logger.info(
            "ACP permission route armed: %s=%s (%s)",
            option_id,
            value,
            acp_tool_gate.label_for(backend),
        )

    # ── State tracking ──

    @property
    def model(self) -> str:
        """Current model name for this session."""
        return self._model

    @property
    def served_model(self) -> str:
        """Backend-resolved model id serving this session (``""`` until known).

        Prefers the explicit ``set_model`` assignment (``_model``), falling
        back to the ``session/new|load`` response's ``currentModelId``
        (``_resolved_model_id``) so a session running on the backend-selected
        DEFAULT is still readable — ``_model`` stays ``""`` on that path.
        Both sources are backend-confirmed; the requested alias is never
        reported here. May be a profile-form id, which is a valid wire id.
        """
        return self._model or self._resolved_model_id

    @property
    def agent_version(self) -> str:
        """The version the shared process RUNS (``""`` until its handshake).

        Delegates to the runtime because the handshake is per process, not per
        session: every handle on one runtime reports the same value.
        """
        return self._runtime.agent_version

    @property
    def config_options(self) -> list[dict[str, Any]]:
        """ACP-reported configOptions (effort, model, mode selectors)."""
        return self._config_options

    @property
    def available_models(self) -> list[dict[str, str]]:
        """Models advertised by the backend at session init."""
        return list(self._available_models)

    def _advertised_model_ids(self) -> list[str]:
        """Advertised model ids, for the model-rejection error path.

        Parity with ``AcpClient._advertised_model_ids``. Empty when the backend
        advertised nothing (no session yet, or a backend that omits ``models``),
        which the error path reads as "entitlement unknown" and leaves the
        transient/capacity handling alone.

        This handle is the shared-runtime path every dashboard chat takes, so
        without it the entitlement discrimination in ``_model_is_unentitled``
        would only ever fire for direct-spawn ``AcpClient`` sessions.
        """
        ids = []
        for entry in self._available_models:
            model_id = entry.get("modelId") if isinstance(entry, dict) else None
            if isinstance(model_id, str) and model_id.strip():
                ids.append(model_id)
        return ids

    def supports_config_option(self, config_id: str) -> bool:
        """Whether the session advertised a config option with this id.

        Returns True when no config options were reported yet (lazy backend).
        """
        if not self._config_options:
            return True
        return any(
            isinstance(opt, dict) and opt.get("id") == config_id for opt in self._config_options
        )

    def get_valid_effort_levels(self) -> list[str]:
        """Return valid effort levels from config options, preserving order.

        The option id is resolved per backend (``effort`` for most,
        ``reasoning_effort`` for codex-acp): a hard-coded spelling returns an
        empty list on a backend that spells it differently, which every caller
        reads as "this model has no effort levels".
        """
        effort_option = effort_config_option_id(self._runtime.acp_backend)
        for opt in self._config_options:
            if not isinstance(opt, dict):
                continue
            if opt.get("id") == effort_option:
                options = opt.get("options", [])
                if isinstance(options, list):
                    return [
                        o.get("value", "")
                        for o in options
                        if isinstance(o, dict) and o.get("value")
                    ]
        return []

    def store_session_config(self, resp: dict[str, Any]) -> None:
        """Extract configOptions and available models from session/new or session/load response.

        Called after create_session() or load() to populate state.
        """
        modes = resp.get("modes")
        current_agent = modes.get("currentModeId") if isinstance(modes, dict) else None
        self.active_agent = current_agent if isinstance(current_agent, str) else ""
        config_options = resp.get("configOptions")
        if isinstance(config_options, list):
            self._config_options = config_options
            self._sync_effort_levels()
            # Read the whole select once and keep only its answer, so a long list
            # is never truncated and never stored. Only the KAS arm of
            # ``ensure_served_default`` reads it.
            select = model_select_envelope(resp) or {}
            ids = [
                m["modelId"]
                for m in select.get("availableModels", [])
                if isinstance(m.get("modelId"), str)
            ]
            self._kas_auto_fallback = (
                pick_served_default("auto", ids) if select.get("currentModelId") == "auto" else ""
            )
        # Where this host's model list lives is asked in ONE place
        # (``session_models_envelope``): a host in
        # ``ACP_BACKENDS_ADVERTISED_MODEL_SELECTION`` advertises no ``models`` object
        # and puts the list in a ``configOptions`` ``model`` select, and a reader that
        # knows about one shape and not the other is how the entitlement probe came to
        # answer ``[]`` for codex while this path answered correctly. Absent stays
        # absent, so the shape branches below are untaken exactly as before.
        models = session_models_envelope(resp, self._runtime.acp_backend)
        if isinstance(models, dict):
            # Record the resolved model id (kiro-cli's currentModelId) so
            # _backfill_context_window can look up the window on pct-only
            # metadata even when the user never explicitly switched models.
            current_model_id = models.get("currentModelId")
            if isinstance(current_model_id, str) and current_model_id:
                self._resolved_model_id = current_model_id
            avail = models.get("availableModels", [])
            if isinstance(avail, list):
                # The shape walk is delegated to the canonical parser so this
                # snapshot and a probe's answer stay directly comparable.
                # The envelope is the same checked-binding discipline
                # as the client's, though here the parser's dict-or-list
                # fallback is unreachable by construction (the inner dict
                # always has a key). The isinstance check above is the
                # assignment gate: a non-list ``availableModels`` must not
                # clobber a previously-stored list. A well-formed EMPTY list
                # still overwrites — that asymmetry with
                # ``AcpClient._capture_available_models`` (non-empty guard) is
                # deliberate, not an oversight.
                self._available_models = parse_advertised_models(
                    {"models": {"availableModels": avail}}
                )
                self._mark_available_models_captured()
            # A backend may advertise its model list without echoing
            # ``currentModelId`` (it is best-effort in the ACP shape). When it
            # names exactly one model that IS the served model unambiguously, so
            # adopt it as the resolved id — otherwise ``served_model`` reports
            # ``""`` for the whole session on an unpinned run (no ``set_model``,
            # no ``currentModelId``), which is why the panel's model chip stays
            # blank until completion fills it from a different source. With two
            # or more advertised and no ``currentModelId`` the served choice is
            # genuinely unknown, so leave it empty rather than guess.
            if not self._resolved_model_id and len(self._available_models) == 1:
                self._resolved_model_id = self._available_models[0]["modelId"]
        elif isinstance(models, list):
            self._available_models = parse_advertised_models({"availableModels": models})
            self._mark_available_models_captured()

    async def ensure_served_default(self, *, activated_model: str = "") -> None:
        """Move an inheriting pooled session off a backend default it cannot run.

        The pooled twin of ``AcpClient._ensure_served_default``.
        ``store_session_config`` records ``session/new``'s ``currentModelId``
        as the session's resolved model without judging it against the list the
        same response advertised. A partition does not have to serve the model
        its backend defaults to — an account whose region omits ``"auto"`` can
        be handed ``"auto"`` at birth — and then every prompt on this session
        dies with "your account does not have access to model 'auto'".

        The kiro backend judges any current id: its advertised ids are exactly
        the ids ``session/set_model`` accepts, so "absent from the advertised
        list" genuinely means unusable there. KAS judges only an ``auto``
        default, against its ``model`` select (see the branch below).

        Routed through :meth:`set_model` rather than a second wire call, so the
        KAS-vs-``session/set_model`` verb choice and the window/meter rebase
        stay in one place. On kiro the id handed to it is an advertised one; on
        KAS it comes from the host's own select and passes because KAS
        advertises no list. Either way ``resolve_usable_model`` lets it through.

        ``_model`` is restored afterwards. That field is the session's INTENT
        (``""``/``"auto"`` mean "inherit"), and it is what the warm-pool
        re-apply and the slot backfill read: left as the fallback id, a fresh
        pooled session would be pinned to whichever model happened to be first
        on the list, and an unpinned slot would stop following the default.
        Only ``_resolved_model_id`` — what the session actually runs — changes.

        ``activated_model`` is the model a ``session/set_mode`` just applied.
        kiro-cli switches the session to the model the activated agent's spec
        pins, replacing whatever it ran before — including the served model an
        earlier call here moved it to. The id recorded from ``session/new`` is
        stale then, so the caller passes the model kiro-cli reported for the
        activation and it is judged instead.
        """
        if self._runtime.acp_backend == ACP_BACKEND_KIRO:
            if activated_model.strip():
                self._resolved_model_id = activated_model.strip()
            unserved = self._resolved_model_id or ""
            fallback = pick_served_default(unserved, self._advertised_model_ids())
            if not fallback:
                return
            _unserved_log = redact_log_via_context(str(unserved))
            logger.warning(
                "ACP backend default %s is not in this account's served list (advertised: %s); "
                "switching session %s to %s",
                _unserved_log,
                ", ".join(self._advertised_model_ids()),
                self._session_id,
                fallback,
            )
            intent = self._model
            try:
                await self.set_model(fallback)
            finally:
                self._model = intent
        elif self._runtime.acp_backend == ACP_BACKEND_KAS:
            # KAS defaults a new session to ``auto`` and lists its models only in
            # the ``model`` select. That list answers ONE question here: is the
            # ``auto`` this session sits on missing from it? A listed id is served,
            # so moving to one is safe even if the list is incomplete. A concrete
            # current model is never judged against it, since a partial list would
            # move a session off a model the account can run.
            fallback = self._kas_auto_fallback
            if not fallback:
                return
            logger.warning(
                "KAS session %s defaults to auto, which its model select does not list; "
                "switching to %s",
                self._session_id,
                fallback,
            )
            intent = self._model
            try:
                await self.set_model(fallback)
            except (AcpError, AcpRequestTimeout) as exc:
                # session/new already succeeded; a refused switch must not fail
                # the session start. Stay on the backend default, the substitute
                # path's own contract.
                logger.warning(
                    "KAS session %s could not switch off auto: %s",
                    self._session_id,
                    redact_log_via_context(str(exc)),
                )
            finally:
                self._model = intent

    @staticmethod
    def _normalize_models(advertised: list[Any]) -> list[dict[str, str]]:
        """Normalize advertised models to ``{modelId, name, description}`` with
        guaranteed keys — the canonical normalization. Every consumer
        (``parse_advertised_models``, and through it ``store_session_config``,
        ``AcpClient._capture_available_models``, and the pooled-runtime
        entitlement probe ``AcpRuntime.probe_advertised_models``) shares this
        shape, so probe answers and session-init snapshots are directly
        comparable and the dashboard model dropdown gets a stable shape
        regardless of backend."""
        captured: list[dict[str, str]] = []
        for m in advertised:
            if not isinstance(m, dict):
                continue
            model_id = m.get("modelId") or m.get("value") or ""
            if not model_id:
                continue
            captured.append(
                {
                    "modelId": str(model_id),
                    "name": str(m.get("name") or model_id),
                    "description": str(m.get("description") or ""),
                }
            )
        return captured

    async def refresh_available_models(self, *, force: bool = False) -> list[dict[str, str]]:
        """Re-resolve the advertised-model snapshot against the live backend.

        ``_available_models`` is otherwise written once, from this session's own
        ``session/new`` — an answer the backend resolved from the account state
        it held at that instant. When that answer was degraded (a lookup racing
        a token refresh answers with the default tier), the session refuses
        models the account actually has, for its whole life. Every consumer of
        entitlement — the explicit-pick refusal, the prompt-time pin withhold,
        the dashboard picker filter — reads through this handle's snapshot, so
        one refresh heals them all.

        The snapshot is replaced only by a NON-EMPTY probe result: a failed or
        empty probe is not evidence about entitlement, so the prior snapshot is
        kept. Returns the probe result either way, so callers can distinguish
        "revalidated" from "could not revalidate".

        ``force`` is forwarded to the runtime probe: a user action (the
        explicit-pick refusal heal, the spawn-time pin withhold) passes
        ``force=True`` so it earns a fresh probe instead of being refused on a
        recent no-evidence failure replay. The read path leaves it False.
        """
        # The floor is this snapshot's capture time, so a non-empty return is
        # always at least as new as the snapshot it replaces: a broader answer
        # cached on the shared runtime before this session captured a narrower
        # one is never replayed over it. The stored snapshot is then dated by the
        # ANSWER's own clock (the runtime's result clock, which is the arrival
        # time of a fresh answer and the original arrival time of a replayed
        # one), never by this call's time: dating a replay by the call would
        # raise this handle's floor above the data it holds, so its next refresh
        # within the TTL would open a real session/new instead of replaying, and
        # a replay captured inside the spawn-race window would be re-dated past
        # it and marked confirmed, disabling the read-path heal for this handle.
        asked_at = time.monotonic()
        fresh = await self._runtime.probe_advertised_models(
            force=force, not_before=self._available_models_captured_at
        )
        if fresh:
            served_at = self._runtime.entitlement_probe_result_at
            self._available_models = list(fresh)
            # A runtime that answered without stamping its result clock (a probe
            # seam that bypasses the store) is dated by the call instead, which
            # is still never later than the answer.
            self._available_models_captured_at = served_at if served_at > 0.0 else asked_at
            self._available_models_probe_confirmed = True
        return fresh

    def _mark_available_models_captured(self) -> None:
        """Stamp the session-init snapshot's capture time (an unconfirmed
        answer). A snapshot written from ``session/new`` is NOT probe-confirmed:
        only :meth:`refresh_available_models` sets that flag, because only a live
        re-probe proves the answer is not the startup-race default."""
        self._available_models_captured_at = time.monotonic()
        self._available_models_probe_confirmed = False

    async def maybe_refresh_available_models(self, catalog_ids: list[str]) -> list[dict[str, str]]:
        """Revalidate the snapshot on the READ path when it would narrow the catalog.

        The dashboard picker filter narrows the ``--list-models`` catalog through
        the newest live session's ``availableModels`` snapshot. When that snapshot
        is the startup-race default (an entitlement lookup racing a token refresh
        answered with the free tier), the picker hides models the account has and
        ``auto`` chats silently inherit the degraded default — and because no
        explicit pick is ever refused, the refresh-before-refuse path never fires.
        This is the read-path counterpart: revalidate the snapshot BEFORE the
        picker trusts it to hide anything.

        ``catalog_ids`` is the full ``--list-models`` catalog (the ids the picker
        would offer unfiltered). The verdict of what to keep/drop is NOT decided
        here — it is :func:`catalog_row_would_drop`, the same per-row verdict the
        picker endpoint applies, built on ``model_is_unusable`` (the single
        spelling of "what this account can run") and ``resolve_pin_spelling``.
        This method only decides WHETHER
        the snapshot is trustworthy enough to narrow with, and reuses the existing
        :meth:`refresh_available_models` heal path when it is not (no second
        probe, no second parser).

        Staleness heuristic (a scheduling decision, never an entitlement one):
        probe only when the snapshot would actually narrow the catalog (some row
        drops and the endpoint does not fail open to the full catalog) AND one of

        * it was never probe-confirmed, or
        * it was captured within ``_READ_PATH_SPAWN_RACE_SECS`` of runtime spawn
          (the exact window the degraded answer is resolved in), or
        * it advertises only ``auto`` against a richer catalog — the strongest
          staleness signal.

        Rate limit: a probe is skipped when this session probed on the read path
        within ``_READ_PATH_REPROBE_MIN_INTERVAL_SECS`` AND the snapshot is either
        probe-confirmed or not auto-only, so a hot poll does not re-probe on every
        runtime TTL expiry forever. An UNCONFIRMED auto-only snapshot is exempt
        from the interval — it always gets to probe — while a probe-CONFIRMED
        auto-only snapshot (a genuine free-tier account really is ``auto``-only)
        honours the interval like any other rather than re-probing forever. The
        runtime's own single-flight probe TTL bounds the cost of the exempt case.

        Fast path: the probe runs as a single in-flight task per handle, shielded
        under ``_READ_PATH_PROBE_DEADLINE_SECS``. On deadline expiry this RAISES
        :class:`EntitlementRevalidating` while the task KEEPS RUNNING to
        completion — so its throwaway probe session is cleaned up and a later
        read serves the corrected list, and the endpoint returns its degraded
        response (rather than serving the un-revalidated snapshot as a live 200
        the frontend caches). A subsequent read while the same task is still in
        flight awaits it too, so it never bypasses the raise. A probe FAILURE
        (as opposed to a timeout) NEVER makes the picker worse: the current
        snapshot is returned unchanged (fail open).
        """
        snapshot = list(self._available_models)
        advertised = advertised_model_ids(snapshot)
        if not advertised:
            # No live list to narrow with — nothing to revalidate, fail open.
            return snapshot
        # Count only rows the picker would actually hide AND a fresher snapshot
        # could restore, using the endpoint's own per-row verdict
        # (``catalog_row_would_drop``): ``auto`` is always kept, an advertised or
        # ``ns::``-foldable row is kept, and an empty id drops against every
        # snapshot, so none of those can justify a probe.
        dropped = [
            cid for cid in catalog_ids if cid.strip() and catalog_row_would_drop(cid, advertised)
        ]
        survivors = [
            cid
            for cid in catalog_ids
            if cid.strip()
            and cid.strip().lower() not in ("auto", "default")
            and not catalog_row_would_drop(cid, advertised)
        ]
        advertises_auto = any(a.strip().lower() in ("auto", "default") for a in advertised)
        # The endpoint FAILS OPEN — serves the whole catalog unfiltered — when the
        # snapshot does not advertise ``auto`` and no non-``auto`` row survives
        # (a namespace mismatch rather than an entitlement answer). A snapshot in
        # that state hides nothing, so it is not narrowing either.
        fails_open = not advertises_auto and not survivors
        would_narrow = bool(dropped) and not fails_open
        if not would_narrow:
            # The snapshot keeps the whole catalog, so a stale snapshot cannot
            # currently hide anything — do not spend a probe.
            return snapshot
        auto_only = len(advertised) == 1 and advertised[0].strip().lower() == "auto"
        now = time.monotonic()
        spawn_at = self._runtime.spawn_monotonic
        within_spawn_race = (
            spawn_at is not None
            and self._available_models_captured_at > 0.0
            and (self._available_models_captured_at - spawn_at) <= _READ_PATH_SPAWN_RACE_SECS
        )
        suspect = not self._available_models_probe_confirmed or within_spawn_race or auto_only
        if not suspect:
            return snapshot
        # An in-flight probe from an earlier read (its deadline expired but the
        # shielded task kept running) MUST be awaited, not bypassed: on the
        # frontend's degraded re-poll the interval gate below would otherwise
        # return the un-revalidated snapshot as a normal answer, the endpoint
        # would serve it as a live 200, and the corrected list this very probe is
        # fetching would never reach the picker. So when a task is still running,
        # skip the interval gate and fall through to await it — the poll gets the
        # landed result or raises EntitlementRevalidating again.
        task = self._read_refresh_task
        in_flight = task is not None and not task.done()
        if not in_flight:
            recently_probed = (
                self._available_models_read_probe_at > 0.0
                and (now - self._available_models_read_probe_at)
                < _READ_PATH_REPROBE_MIN_INTERVAL_SECS
            )
            # The interval applies to a probe-CONFIRMED snapshot and to every
            # non-auto-only snapshot: a genuine free-tier account is legitimately
            # auto-only, so once confirmed it must not re-probe on every poll. An
            # UNCONFIRMED auto-only snapshot is exempt from the interval — it
            # always gets to probe (its docstring promise), and the runtime's own
            # single-flight probe TTL still bounds the cost of a burst.
            if recently_probed and (self._available_models_probe_confirmed or not auto_only):
                return snapshot
            self._available_models_read_probe_at = now
            task = asyncio.ensure_future(self.refresh_available_models())
            self._read_refresh_task = task
        # Non-None in both branches: in-flight reused an existing task, else one
        # was just started above.
        assert task is not None
        try:
            # Shield so a timeout leaves the task RUNNING (it finishes the probe
            # and cleans up its throwaway session); we just stop waiting on it.
            await asyncio.wait_for(asyncio.shield(task), timeout=_READ_PATH_PROBE_DEADLINE_SECS)
        except (TimeoutError, asyncio.TimeoutError):
            # The probe did not land inside the deadline and is STILL RUNNING.
            # We must not return the un-revalidated snapshot as a normal answer:
            # the picker endpoint serves that as a live HTTP 200 that the
            # frontend caches with no refetch, so the corrected list this probe
            # is fetching would never be served. Signal "revalidation in flight"
            # so the endpoint returns its degraded response and the frontend
            # keeps its last-good list and polls again; the next read (once the
            # task has landed) serves the corrected list.
            raise EntitlementRevalidating from None
        except Exception:
            # Fail open exactly as today: no evidence never worsens the picker.
            logger.debug("read-path entitlement revalidation failed", exc_info=True)
            return list(self._available_models)
        # refresh_available_models already replaced the snapshot in place on a
        # non-empty probe and kept it on an empty one; either way the live
        # snapshot is the answer to narrow with.
        return list(self._available_models)

    def _sync_effort_levels(self) -> None:
        """Push ACP-reported effort levels to the global validation set (parity
        with AcpClient._sync_effort_levels). Without this, the unified kiro path
        never refreshes the reasoning-effort allow-list. A backend in
        ``ACP_BACKENDS_EFFORT_FROM_ADVERTISED_OPTION`` advertises a per-session
        list: it joins the validation set but never replaces the global fallback
        order other slots are served. Function-level import avoids the
        chat_persistence -> dashboard -> session -> acp import cycle."""
        levels = self.get_valid_effort_levels()
        if not levels:
            return
        if self._runtime.acp_backend in ACP_BACKENDS_EFFORT_FROM_ADVERTISED_OPTION:
            # circular import: chat_persistence -> dashboard -> session -> acp
            from kiro_crew.dashboard.chat_persistence import register_reasoning_effort_values

            register_reasoning_effort_values(levels)
            return
        # circular import: chat_persistence -> dashboard -> session -> acp
        from kiro_crew.dashboard.chat_persistence import update_reasoning_effort_values

        update_reasoning_effort_values(levels)

    # ── In-flight tool state (delegated to the shared tracker) ──
    # These read-only views keep the call sites that read the judged tool
    # unchanged while the hand-off rule lives in ``InFlightToolTracker``. The
    # dispatch / terminal / prompt-start writes go through the tracker directly
    # (see ``_dispatch_events`` and ``prompt``).

    @property
    def _inflight_tool(self) -> ToolCallState | None:
        """The :class:`ToolCallState` a stall verdict is read against."""
        return self._tool_tracker.current

    @_inflight_tool.setter
    def _inflight_tool(self, value: ToolCallState | None) -> None:
        # A direct set stages one call (the single-slot shape the watchdog tests
        # use to force the oracle path): ``None`` clears, otherwise it becomes the
        # judged call under the empty-id slot. The normal dispatch path writes
        # through ``_tool_tracker.dispatch`` with the real toolCallId.
        if value is None:
            self._tool_tracker.clear()
        else:
            self._tool_tracker.dispatch("", value)

    @property
    def _active_tool_calls(
        self,
    ) -> dict[str, tuple[ToolCallState, "InteractiveClassification | None"]]:
        """Live ``{toolCallId: (state, interactive)}`` map, backed by the shared
        tracker. Read-only by convention — the hand-off goes through the tracker
        (see ``_dispatch_events`` and ``prompt``)."""
        return self._tool_tracker.active_calls  # type: ignore[return-value]

    @property
    def _inflight_interactive(self) -> "InteractiveClassification | None":
        """Pre-dispatch interactive classification of the in-flight SHELL tool
        (``None`` when no shell tool is in flight). Carried as the tracker's aux
        payload. Read by the tool branch's window policy and the post-stall
        classifier; never by the oracle."""
        aux = self._tool_tracker.current_aux
        return aux  # type: ignore[return-value]

    @property
    def _inflight_tool_call_id(self) -> str:
        """toolCallId of the in-flight tool ("" when none)."""
        return self._tool_tracker.current_id

    def _remote_flat_launches(self) -> dict[str, tuple[tuple[str, ...], ...]] | None:
        """The launch roster declared to the oracle's socket scan, or None while
        the ``remote_flat`` window is off.

        None reads as undeclared, so with ``watchdog.remote_flat_probe_secs`` at
        0 the oracle never tags ``remote_flat`` and the evidence, the metric
        bucket and the window all stay as they were. Read per call, so a config
        reload that turns the key on or off takes effect on the next probe.
        """
        if self._watchdog.remote_flat_probe_secs <= 0:
            return None
        return self._mcp_launches

    def record_mcp_launches(self, spec: Any, wire_servers: Any) -> None:
        """Record how this session launched its stdio MCP servers.

        *spec* is the agent spec the session runs and *wire_servers* the final
        ``mcpServers`` array it sent; both were already read by the runtime, so
        this is in-memory, synchronous and never raises. Replaces any earlier
        roster: ``session/load`` re-declares the session's servers. Entries the
        roster refuses for a bound are counted and logged once per recording, so
        a server missing from the scan is never silent.
        """
        try:
            roster, refused = mcp_launch_roster(spec, wire_servers)
        except Exception:
            logger.debug(
                "session %s: MCP launch roster not recorded", self._session_id, exc_info=True
            )
            roster, refused = {}, 0
        self._mcp_launches = roster
        if refused:
            logger.warning(
                "session %s: %d MCP server launch(es) past the roster bounds are not "
                "recorded; the remote_flat window cannot attribute a call to them",
                self._session_id,
                refused,
            )

    def _emit_tool_interrupted_sel(self, site: str) -> None:
        """Emit a SEL audit + WARNING when kiro-cli's security filter cancels tools.

        Mirrors AcpClient._emit_tool_interrupted_sel: a permission decision
        KiroCrew observes but does not control (kiro-cli denied tool execution).
        Best-effort — a failed audit must not break the turn. This handle has no
        KiroCrew session_key, so the ACP sessionId is recorded in metadata for
        correlation.
        """
        logger.warning(
            "kiro-cli cancelled tool use(s) [site=%s session=%s]", site, self._session_id
        )
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

    def queued_frame_count(self) -> int:
        """How many frames are waiting on this session's queue right now.

        For a caller that has to decide which frames predate a request it is
        about to send: read this first, then send. The answer is only meaningful
        at that instant, which is why it is the caller's to take rather than
        something ``drain_init`` re-derives later.
        """
        return self._queue.qsize()

    def _owns_mcp_frame(self, msg: JsonRpcMessage) -> bool:
        """Whether *msg* names THIS session as the server registration's owner.

        The one spelling of MCP-frame ownership on the shared runtime, used by
        both the raw-report path and the events that feed the live one, so the
        two cannot answer it differently.

        A POSITIVE test, deliberately: the runtime's ``fanout_no_owner`` marks a
        sessionless frame only once more than one queue is registered, because
        its original consumer (the subagent idle-stall clock) is right to treat
        a lone session as the sole owner of whatever arrives. This view is not —
        it publishes server names and failure reasons as "what THIS session
        mounted", so a co-tenant that emits a sessionless frame before it has
        registered its own queue would otherwise have its servers attributed
        here. Requiring the frame to name us refuses that regardless of how many
        queues exist, and it stays correct when a third transport arrives.
        """
        params = msg.params if isinstance(msg.params, dict) else {}
        return bool(self._session_id) and params.get("sessionId") == self._session_id

    def _accept_oauth_request(self, msg: JsonRpcMessage) -> dict[str, str] | None:
        """Validate and deduplicate one MCP OAuth notification."""
        params = msg.params if isinstance(msg.params, dict) else {}
        server_name = str(params.get("serverName") or params.get("name") or "")
        oauth_url = str(params.get("oauthUrl") or params.get("url") or "")
        if not _is_safe_oauth_url(oauth_url):
            if oauth_url:
                logger.warning(
                    "ACP: refusing unsafe MCP OAuth URL for %s",
                    server_name or "(unknown)",
                )
            return None
        if not server_name:
            logger.warning("ACP: dropping MCP OAuth request with empty serverName")
            return None
        if server_name in self._oauth_emitted_servers:
            logger.debug("ACP: dropping duplicate MCP OAuth request for %s", server_name)
            return None
        self._oauth_emitted_servers.add(server_name)
        return {"serverName": server_name, "oauthUrl": oauth_url}

    def pop_pending_oauth_requests(self) -> list[dict[str, str]]:
        """Drain OAuth requests captured while this session initialized."""
        pending = list(self._pending_oauth_requests)
        self._pending_oauth_requests.clear()
        return pending

    def mcp_session_report(self) -> McpSessionReport:
        """This session's MCP registration report — parity with AcpClient.

        Does NOT drain: the report is the session's standing answer to "which
        servers actually started here". An unreported server means *not
        reported*, never *not mounted*.
        """
        return self._mcp_report

    async def wait_mcp_ready(
        self,
        required: tuple[str, ...],
        timeout: float,
        *,
        stale_report_frames: int = 0,
        tool_policy: dict[str, Any] | None = None,
        injected: frozenset[str] = frozenset(),
    ) -> None:
        """Wait for KAS's active managed roster and catalog, or fail before prompting.

        ``injected`` is the subset of ``required`` that travelled in the request's
        own ``mcpServers`` array; see :class:`KasMcpReadiness`.
        """
        readiness = KasMcpReadiness(self._session_id, required, tool_policy, injected)
        self._mcp_report.include_configured(required)
        deadline = time.monotonic() + timeout
        stale = max(0, stale_report_frames)
        while readiness.pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AcpRequestTimeout(
                    f"KAS managed MCP readiness timed out after {timeout:g}s: {readiness.pending}"
                )
            try:
                msg = await asyncio.wait_for(self._queue.get(), remaining)
            except asyncio.TimeoutError as exc:
                raise AcpRequestTimeout(
                    f"KAS managed MCP readiness timed out after {timeout:g}s: {readiness.pending}"
                ) from exc
            if msg is None:
                self._queue.put_nowait(None)
                raise AcpRuntimeDead("Runtime exited while waiting for managed MCP readiness")
            try:
                # Config/OAuth side effects also belong to external servers and
                # pre-mode frames. They must not become readiness requirements.
                self._apply_init_notification(msg, classify_notification(msg))
            except Exception:
                logger.debug("error processing init notification", exc_info=True)
            if stale:
                stale -= 1
                continue
            self._mcp_report.record_frame(msg, owned=self._owns_mcp_frame(msg))
            readiness.record(msg)
            if readiness.failure:
                raise AcpRuntimeError(f"KAS managed MCP initialization failed: {readiness.failure}")
        for name in required:
            self._mcp_report.record_event(EVENT_MCP_SERVER_INITIALIZED, name)

    def _note_mcp_sign_in_status(self, msg: JsonRpcMessage, *, offer: bool) -> None:
        """Track which of this session's servers need an OAuth sign-in.

        Read from this session's own ``_kiro/mcp/status`` snapshots. A server is
        added on ``failedAuthorization`` and removed once it connects, is
        disabled, or leaves the snapshot. A tracked server that connects is
        also recorded as a completed sign-in for the dispatch loop to yield as
        ``EVENT_MCP_SERVER_INITIALIZED``. With ``offer`` a sign-in is then
        offered; the dispatch loop passes it, because a link started there
        arrives while the loop is reading. Session start and the pre-turn drain
        read without offering: a link they started would land between turns,
        and a start that fails is torn down.

        Bounded like the session's MCP report reads the same frame: at most
        ``BUCKET_CAP`` entries, and a name over ``NAME_CAP`` is skipped rather
        than truncated, because a cut name names a server the engine does not
        have. The completed set is bounded the same way: it is drained only by
        the dispatch loop, so a completion that would take it past
        ``BUCKET_CAP`` is dropped rather than held. Every dropped entry is
        counted and the count is logged when it changes, so a server that is
        never offered a sign-in, or whose completion is never yielded, is named
        as such.
        """
        if not msg.is_method(METHOD_KAS_MCP_STATUS) or not self._owns_mcp_frame(msg):
            return
        servers = msg.params.get("servers")
        if not isinstance(servers, list):
            return
        present: set[str] = set()
        dropped = max(0, len(servers) - BUCKET_CAP)
        for server in servers[:BUCKET_CAP]:
            if not isinstance(server, dict) or not isinstance(server.get("name"), str):
                continue
            name = server["name"]
            if not name or len(name) > NAME_CAP:
                dropped += 1
                continue
            present.add(name)
            if server.get("failedAuthorization") is True:
                self._mcp_sign_in_needed.add(name)
                self._mcp_reattempt_waiting.discard(name)
            elif server.get("status") in ("connected", "disabled"):
                tracked = name in self._mcp_sign_in_needed or (
                    name in self._mcp_reattempt_waiting
                    and self._mcp_reattempt_counts.get(name, 0) > 0
                )
                if server.get("status") == "connected" and tracked:
                    # Only a server that was being signed in to (or re-attempted)
                    # counts as a completion; an ordinary connected server yields
                    # nothing.
                    # A full set refuses the name: between-turn reads never
                    # drain it, so an unbounded add grows with every snapshot.
                    if (
                        name in self._mcp_sign_in_completed
                        or len(self._mcp_sign_in_completed) < BUCKET_CAP
                    ):
                        self._mcp_sign_in_completed.add(name)
                    else:
                        dropped += 1
                self._mcp_sign_in_needed.discard(name)
                self._mcp_reattempt_waiting.discard(name)
            elif (
                server.get("status") == "failed"
                and name not in self._mcp_sign_in_needed
                and name not in self._mcp_reattempt_waiting
                and is_recoverable_auth_failure(server.get("errorMessage"))
            ):
                self._mcp_reattempt_waiting.add(name)
        self._mcp_sign_in_needed &= present
        self._mcp_reattempt_waiting &= present
        self._sync_mcp_reattempting()
        if dropped != self._mcp_sign_in_dropped:
            self._mcp_sign_in_dropped = dropped
            if dropped:
                logger.warning(
                    "MCP sign-in tracking skipped %d status entr%s on session %s "
                    "(over %d servers, a name over %d characters, or %d "
                    "completions still to yield); those servers are not offered "
                    "a sign-in or their completion is not yielded",
                    dropped,
                    "y" if dropped == 1 else "ies",
                    self._session_id,
                    BUCKET_CAP,
                    NAME_CAP,
                    BUCKET_CAP,
                )
        if offer:
            self._offer_mcp_sign_in()

    def _offer_mcp_sign_in(self) -> None:
        """Ask the runtime to start one pending sign-in, if it can take one.

        Called at turn start, after the pre-turn drain, and from the dispatch
        loop on a status snapshot: both points have a reader for the link the
        engine sends back. Never called during a session-start drain.

        The server's banner dedupe is cleared first: its earlier link is dead
        once a new sign-in starts, and the new link must not be dropped as a
        duplicate.
        """
        begin = getattr(self._runtime, "begin_mcp_sign_in", None)
        if begin is None or not self._session_id:
            return
        names = sorted(self._mcp_sign_in_needed)
        start = next(
            (i for i, name in enumerate(names) if name > self._mcp_sign_in_last_offered),
            0,
        )
        for name in names[start:] + names[:start]:
            if begin(self._session_id, name):
                self._mcp_sign_in_last_offered = name
                self._oauth_emitted_servers.discard(name)
                return

    def _sync_mcp_reattempting(self) -> None:
        """Tell the MCP report which failed servers this session still re-attempts.

        Only on a host that can reconnect one server; elsewhere the set is
        empty and every credential failure is named for a new session.
        """
        report = getattr(self, "_mcp_report", None)
        if report is None:
            return
        pending: set[str] = set()
        if self._runtime_reattempts_mcp():
            pending = {
                name
                for name in self._mcp_reattempt_waiting
                if self._mcp_reattempt_counts.get(name, 0) < REATTEMPT_MAX
            }
        report.set_reattempting(pending)

    def _runtime_reattempts_mcp(self) -> bool:
        """Whether this session's host re-attempts a credential-failed server.

        Asks the runtime's capability rather than whether the method exists:
        every runtime has ``begin_mcp_reattempt``, and on a host without the
        reset request it refuses every call, so a server counted as pending
        there would never be named for a new session.
        """
        if getattr(self._runtime, "begin_mcp_reattempt", None) is None:
            return False
        return getattr(self._runtime, "reattempts_mcp_servers", False) is True

    def _reattempt_failed_mcp_servers(self) -> None:
        """Re-attempt servers that failed for want of a credential.

        Called at turn start, beside the sign-in offer: by then the user or
        the agent may have obtained the credential the server lacked. Each
        waiting server is re-attempted at most ``REATTEMPT_MAX`` times per
        session, and at most ``BUCKET_CAP`` distinct servers are ever counted,
        so the budget holds however many names the engine reports. A healthy
        session has no waiting server and does no work here. Nothing is sent
        while an OAuth sign-in holds the runtime's slot or on a host without
        the reset request; the runtime refuses those, no attempt is spent, and
        the server waits for the next turn.
        """
        if not self._mcp_reattempt_waiting or not self._session_id:
            return
        begin = getattr(self._runtime, "begin_mcp_reattempt", None)
        if begin is None or not self._runtime_reattempts_mcp():
            return
        for name in sorted(self._mcp_reattempt_waiting):
            attempts = self._mcp_reattempt_counts.get(name, 0)
            if attempts >= REATTEMPT_MAX:
                continue
            if attempts == 0 and len(self._mcp_reattempt_counts) >= BUCKET_CAP:
                continue
            if not begin(self._session_id, name):
                continue
            self._mcp_reattempt_counts[name] = attempts + 1
            if attempts + 1 >= REATTEMPT_MAX:
                self._sync_mcp_reattempting()
            logger.info(
                "MCP server %s on %s failed for a credential; re-attempt %d of %d",
                sanitize_sink_text(name, NAME_CAP),
                self._session_id,
                attempts + 1,
                REATTEMPT_MAX,
            )

    def _apply_init_notification(self, msg: JsonRpcMessage, action: str) -> None:
        """Initialization side effects shared by the drain and readiness barrier."""
        self._note_mcp_sign_in_status(msg, offer=False)
        params = msg.params if isinstance(msg.params, dict) else {}
        if action == "update":
            update = params.get("update") or {}
            if isinstance(update, dict) and update.get("sessionUpdate") == "config_option_update":
                cfg = update.get("configOptions")
                if isinstance(cfg, list):
                    self._config_options = cfg
                    self._sync_effort_levels()
        elif action == "mcp_oauth_request":
            request = self._accept_oauth_request(msg)
            if request is not None:
                self._pending_oauth_requests.append(request)
        elif action == "mcp_server_init_failure":
            logger.info(
                "MCP server init failure on %s: %s",
                self._session_id,
                params.get("serverName") or "",
            )

    async def drain_init(
        self,
        duration: float = _MCP_DRAIN_DURATION,
        idle_exit: float = _MCP_DRAIN_IDLE_EXIT,
        no_report_ceiling: float | None = None,
        stale_report_frames: int = 0,
    ) -> None:
        """Drain MCP-init / oauth / config frames from the queue after set_mode.

        Parity with AcpClient._drain_notifications. During session setup there is
        no in-flight prompt, so every frame on this session's queue is an
        init-time notification. Draining them here keeps them out of the first
        turn's event stream and gives MCP servers a window to report in before
        the first prompt races ahead.

        The idle shortcut means "quiet AFTER the servers reported", not "quiet,
        therefore done": until the first MCP registration frame (initialized /
        init_failure / oauth_request) is observed, queue silence is treated as a
        server still booting and the drain keeps waiting, bounded by
        ``no_report_ceiling`` (defaults to ``_MCP_DRAIN_NO_REPORT_CEILING``,
        resolved at call time so tests can shrink the module constant). Once a
        report has been seen, the drain allows up to ``duration`` more and exits
        after ``idle_exit`` seconds of silence, so warm sessions — whose
        registration frames were staged during session/new — arm immediately and
        pay no extra latency. ``config_option_update`` frames refresh cached
        configOptions; OAuth requests remain drainable via
        ``pop_pending_oauth_requests``; everything else is logged/discarded.
        Best-effort — never raises.

        ``stale_report_frames``: how many frames on the queue describe the roster
        that initialized during ``session/new``. For a session whose mode was
        then SWITCHED via ``set_mode``, that is the PRE-switch agent's roster,
        and the switched-to agent's own servers may still be booting. Those
        frames are still drained and processed normally; they just neither arm
        the idle shortcut nor enter the report.

        The COUNT is the caller's to measure, and it must be read before the
        ``set_mode`` request goes out — the only moment "already queued" and
        "pre-switch" mean the same thing. Measuring it here instead would count
        the switched-to agent's own registrations, which the backend can emit
        before it answers set_mode, and consume them without recording: a
        session left at a false "no report" for as long as its servers keep
        talking.
        """
        if no_report_ceiling is None:
            no_report_ceiling = _MCP_DRAIN_NO_REPORT_CEILING
        start = time.monotonic()
        deadline = start + duration
        hard_deadline = start + max(duration, no_report_ceiling)
        # A nonpositive ceiling means "do not hold for a first report" — the
        # caller knows no MCP server can register (MCP-free runtime). The idle
        # shortcut is then active from the start, i.e. the pre-fix behavior.
        reported = no_report_ceiling <= 0.0
        # Exactly the frames the caller counted before it sent set_mode are the
        # pre-switch agent's. A count, not a test for emptiness: a queue that the
        # ACTIVE agent refills before the backlog is drained never goes empty, so
        # an emptiness test would keep the flag set for the whole drain and skip
        # recording every report the switched-to agent makes. And it comes from
        # the CALLER rather than a qsize() read here, because by the time this
        # runs the active agent's own registrations may already have landed.
        stale_frames = max(0, stale_report_frames)
        drained = 0
        while True:
            now = time.monotonic()
            limit = deadline if reported else hard_deadline
            if now >= limit:
                break
            remaining = limit - now
            try:
                msg = await asyncio.wait_for(
                    self._queue.get(),
                    timeout=min(remaining, idle_exit) if reported else remaining,
                )
            except asyncio.TimeoutError:
                # Armed: queue went quiet after reporting — servers are done.
                # Unarmed: the no-report ceiling elapsed with nothing to show.
                break
            if msg is None:
                # Runtime died during init — re-poison so the next consumer sees it.
                await self._queue.put(None)
                break
            drained += 1
            # This frame is the pre-switch agent's iff the snapshot still has
            # room for it; the counter is spent here so a refill cannot buy a
            # later frame the same treatment.
            stale_backlog = stale_frames > 0
            if stale_backlog:
                stale_frames -= 1
            try:
                action = classify_notification(msg)
                if not stale_backlog:
                    # Deliberately narrower than the "still drained and
                    # processed normally" treatment the stale backlog gets
                    # otherwise: those frames describe the PRE-switch agent's
                    # roster, so recording one could show a server the current
                    # agent does not have as mounted here. Skipping it can
                    # instead leave a server that is genuinely up looking
                    # unreported until it reports again — and that is the safe
                    # direction, because the report renders an unreported server
                    # as "no report", never as "not mounted".
                    self._mcp_report.record_frame(msg, owned=self._owns_mcp_frame(msg))
                if not reported and not stale_backlog and action in _MCP_DRAIN_REPORT_ACTIONS:
                    # First server report: arm the idle shortcut and give the
                    # remaining servers up to ``duration`` from this point.
                    reported = True
                    deadline = time.monotonic() + duration
                self._apply_init_notification(msg, action)
            except Exception:
                logger.debug("drain_init: error processing init frame", exc_info=True)
        if drained:
            logger.debug("drain_init: drained %d init frame(s) for %s", drained, self._session_id)

    def _is_kas_hooks_request(self, msg: JsonRpcMessage) -> bool:
        """Whether this frame is a hooks request THIS session may answer.

        A named predicate rather than an inline condition, so the backend clause
        has a test that fails when it is removed: asserting the membership set's
        contents cannot catch a deleted membership CHECK, and the check is the part
        that keeps operator-authored hook commands away from a backend that never
        defined the channel.

        Five conditions, all required: the frame is a request (it carries an id and
        so needs a response), its method is a string, that string is one of the
        hooks methods, this session's backend is in the capability set, and -- for
        ``executeHook``, the one that runs a command -- the handshake announced
        hooks. The type check is load-bearing, not defensive: ``method`` carries
        whatever the peer put on the wire, and a JSON list or object there makes the
        membership test raise :class:`TypeError` inside the dispatch loop.

        The announce clause keeps the execute path dark in fact, not only by hint:
        a backend that sends ``executeHook`` unasked is answered ``-32601`` like any
        method it was never offered.
        """
        return (
            msg.id is not None
            and isinstance(msg.method, str)
            and msg.method in _KAS_HOOKS_METHODS
            and self._runtime.acp_backend in ACP_BACKENDS_HOOKS_LIST
            and (msg.method != kas_wire.METHOD_HOOKS_EXECUTE or kas_wire.hooks_announced())
        )

    async def _answer_kas_hooks_request(self, msg: JsonRpcMessage) -> None:
        """Answer one hooks request from the backend.

        The answers are built in :mod:`kiro_crew.acp.kas_wire`, which owns the
        shapes; this is the route that carries them.

        ``list`` and ``sessionStart`` stay on this loop: each reads an in-memory
        dict and nothing else, so a thread hop would buy nothing. ``list`` records
        what it answered under this handle's OWNING session, which is the record
        ``executeHook`` checks.

        ``executeHook`` runs as a task of its own. It waits on a subprocess for up
        to the hook's timeout, and this loop demuxes every frame of the turn --
        including the cancel that would end it.
        """
        params = msg.params if isinstance(msg.params, dict) else {}
        if msg.method == kas_wire.METHOD_HOOKS_EXECUTE:
            if len(self._hook_tasks) >= _MAX_INFLIGHT_HOOK_EXECUTIONS:
                # Refused before a task exists, so a flood of execute frames
                # holds a bounded number of hook processes, never one per frame.
                # Audited like every other refusal on this path.
                reason = "too many hooks already running"
                await asyncio.to_thread(
                    kas_wire.audit_execute_refusal,
                    self._session_key,
                    self._crew_agent,
                    params,
                    reason,
                )
                await self._send_hook_error(msg.id, f"Refused by Kiro Crew: {reason}")
                return
            task = asyncio.create_task(self._answer_kas_hook_execute(msg.id, params))
            self._hook_tasks.add(task)
            task.add_done_callback(self._hook_tasks.discard)
            return
        if msg.method == kas_wire.METHOD_HOOKS_LIST:
            result = kas_wire.hooks_list_response(
                params, session_key=self._session_key, listed=self._listed_hooks
            )
        else:
            result = kas_wire.hooks_session_start_response(params)
        await self._runtime.send_response(msg.id, result)

    async def _answer_kas_hook_execute(self, request_id: Any, params: dict) -> None:
        """Run one listed hook and answer the request, refusal included.

        A refusal is answered as a JSON-RPC error carrying its reason, never as a
        result: a result carries an exit code, and a command that never started has
        none. An unexpected failure is answered the same way, and a cancelled run
        is answered ``cancelled`` before the cancellation propagates, so the
        backend's turn is never left waiting on a request nobody will answer.
        """
        try:
            result = await kas_wire.hooks_execute(
                params,
                session_key=self._session_key,
                agent=self._crew_agent,
                listed=self._listed_hooks,
            )
        except asyncio.CancelledError:
            try:
                await self._runtime.send_response(request_id, {"exitCode": -1, "cancelled": True})
            except Exception:
                logger.warning(
                    "KAS executeHook cancel answer undeliverable for %s", self._session_id
                )
            raise
        except kas_wire.HookExecuteRefused as exc:
            logger.info("KAS executeHook refused for %s: %s", self._session_id, exc)
            await self._send_hook_error(request_id, f"Refused by Kiro Crew: {exc}")
            return
        except Exception:
            logger.exception("KAS executeHook failed for %s", self._session_id)
            await self._send_hook_error(request_id, "Kiro Crew could not run the hook")
            return
        try:
            await self._runtime.send_response(request_id, result)
        except Exception:
            logger.warning("KAS executeHook answer undeliverable for %s", self._session_id)

    def _cancel_hook_tasks(self) -> None:
        """Cancel every in-flight hook execution this session started.

        ``run_script_hook`` kills the hook's process tree when it is cancelled
        mid-wait, so a cancelled turn or a torn-down session leaves no hook running.
        """
        # Read defensively: a handle assembled without ``__init__`` carries none.
        for task in list(getattr(self, "_hook_tasks", ())):
            task.cancel()

    async def _send_hook_error(self, request_id: Any, message: str) -> None:
        try:
            await self._runtime.send_error(request_id, kas_wire.HOOK_EXECUTE_REFUSED_CODE, message)
        except Exception:
            logger.warning("KAS executeHook refusal undeliverable for %s", self._session_id)

    def _build_permission_event(self, msg: JsonRpcMessage) -> AcpEvent | None:
        """Build an AcpEvent for a permission request via the shared parser.

        Delegates to _dispatch.build_permission_event so this transport reads the
        kiro/claude payload shape (toolCall-nested title/kind/toolCallId, option
        normalization, is_shell from the trusted tool_call cache) identically to
        AcpClient. Records the advertised optionIds so approve/reject echo them.
        """
        _perm_params = msg.params if isinstance(msg.params, dict) else {}
        event, recorded = build_permission_event(
            msg,
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
            # ORIGIN-BOUND provenance: cache entries are keyed by the
            # emitting frame's sessionId, so a child cannot replay a consumed
            # parent toolCallId to inherit trusted params for a different
            # operation, while same-origin repeat frames still resolve.
            cache_scope=str(_perm_params.get("sessionId") or self._session_id),
            kas_consent_meta=self._runtime.acp_backend == ACP_BACKEND_KAS,
            harness_backend=self._runtime.acp_backend,
        )
        if event is None:
            return None
        if recorded is not None and event.request_id != "":
            self._permission_options[event.request_id] = recorded
        if event.request_id != "":
            self._permission_gate_events[event.request_id] = event
        # A frame the runtime routed here for a backend-internal subagent
        # carries the CHILD's sessionId, not this handle's. Mark the origin so
        # the policy consumer can tell reduced-fidelity requests apart. Child
        # tool_call/refinement frames ARE routed into the caches above (same
        # parser as slot-owned frames), so a well-behaved child carries full
        # structured context; the low-fidelity downgrade applies only when the
        # provenance flags say the context never arrived (frame race, drop).
        frame_sid = str((msg.params or {}).get("sessionId") or "")
        if frame_sid and frame_sid != self._session_id:
            event.sub_session_id = frame_sid
        return event

    def _handle_kas_update(self, session_update: str, update: dict) -> list[AcpEvent] | None:
        """Map a KAS-only ``session/update`` discriminant to Crew events.

        Returns a (possibly empty) event list for a discriminant KAS uses in
        place of a kiro-cli ``_kiro.dev/*`` method, or ``None`` when the
        discriminant is not KAS-specific (ordinary chunk/tool frames) so the
        caller falls through to the shared parser. Only ever reached on the KAS
        backend.
        """
        if session_update == UPDATE_CURRENT_MODE:
            # KAS agent-switch echo (kiro-cli: _kiro.dev/agent/switched). The new
            # mode id stands in for kiro-cli's agentName. KAS re-emits this to
            # report the CURRENT mode, so suppress only a repeat of the
            # already-emitted mode (a no-op re-assert); every actual change is
            # emitted. First-sight is NOT suppressed: the session's initial
            # current_mode_update is drained pre-prompt without reaching here, so
            # the first frame that does reach here is a real switch that must not
            # be dropped.
            mode_id = update.get("currentModeId")
            if not (isinstance(mode_id, str) and mode_id):
                return []
            # Deduped on the WIRE id: a crewmate registered as ``vibe-crewmate``
            # and the engine's own ``vibe`` are two modes, though both report
            # under the name ``vibe`` below.
            if mode_id == self._last_kas_mode_id:
                return []
            self._last_kas_mode_id = mode_id
            registered = getattr(self, "kas_registered_agents", None)
            # Back to the crewmate's own id when this is an agent the batch
            # registered under a wire id (see attach_kas_custom_agents).
            crew_id = kas_crew_agent_id(registered, mode_id)
            # The session now runs this agent, so a turn that names none meets
            # this agent's spec hooks, not the one the batch was built for, and
            # what it auto-approves is this agent's entry in the registered batch
            # -- chosen by wire id, so a built-in of the same name never inherits
            # the crewmate's approvals.
            if self.kas_projected_agent:
                from kiro_crew.acp.kas_agents import switched_auto_approved

                self.kas_projected_agent = crew_id
                self.kas_auto_approved = switched_auto_approved(registered, mode_id)
            return [AcpEvent(kind=EVENT_AGENT_SWITCHED, text=crew_id)]
        if session_update == UPDATE_SESSION_INFO:
            return self._handle_kas_session_info(update)
        # available_commands_update / any other KAS discriminant falls through to
        # the shared parser, which already returns [] for it — Crew surfaces no
        # available-commands UI for any backend (kiro-cli's
        # _kiro.dev/commands/available is likewise unconsumed), so there is
        # nothing to render and no separate branch is needed.
        return None

    def _handle_kas_session_info(self, update: dict) -> list[AcpEvent]:
        """Map a KAS ``session_info_update`` (``_meta.kiro`` union) to events.

        This one discriminant carries what kiro-cli splits across separate
        methods: ``context_usage`` (the context meter) and ``turn_completion``
        (per-turn billing) together reconstruct the single ``_kiro.dev/metadata``
        frame, while the ``summarization_*`` kinds are KAS's compaction status
        (kiro-cli: ``_kiro.dev/compaction/status``) and the ``steering_*`` kinds
        are KAS's mid-turn steer echo (kiro-cli: ``session/update`` steer
        discriminants, handled by the "steer" action).
        """
        kiro = kas_wire.kiro_meta(update)
        if kiro is None:
            return []
        kind = kiro.get(kas_wire.FIELD_KIND)
        if kind == kas_wire.KIND_CONTEXT_USAGE:
            self._apply_kas_context_pct(kiro.get(kas_wire.FIELD_USAGE_PERCENTAGE))
            return []
        if kind == kas_wire.KIND_TURN_COMPLETION:
            self._apply_kas_turn_completion(kiro)
            return []
        if kind in kas_wire.SUMMARIZATION_KINDS:
            if kind == kas_wire.KIND_SUMMARIZATION_COMPLETED:
                # Pre-compaction counts no longer describe the session — drop
                # them so the meter resets and fresh telemetry re-derives real
                # numbers (parity with the kiro-cli compaction handler).
                self.last_prompt_stats.reset_after_compaction()
                self._compaction_failed_at = None
                status_type = "completed"
            elif kind == kas_wire.KIND_SUMMARIZATION_FAILED:
                status_type = "failed"
                # Parity with AcpClient._handle_compaction_status: log the WHOLE
                # frame at WARNING. Without it a KAS summarization failure
                # leaves the chat row as the only record of it — and when the
                # row's reason collapses to a placeholder there is nothing to
                # grep server-side and no way to learn which field the reason
                # actually arrived in.
                # redact_text, not the bare frame: conversationSummary rides in
                # this payload, so an unredacted dump would persist whatever the
                # conversation contained -- a pasted credential included -- into
                # gateway.log. Same scrub the notice below applies, for the same
                # reason.
                logger.warning("KAS summarization failed — raw frame: %s", redact_text(str(kiro)))
                # KAS is the third producer of a failed compaction status and
                # rides the SAME dispatch loop, so it gets the same bounded
                # post-failure wait — a KAS turn abandoned after failed
                # summarization must not drain to the ceiling either.
                self._compaction_failed_at = time.monotonic()
                self.last_compaction_transient = compaction_failure_is_transient(kiro)
            else:
                status_type = "started"
            if status_type != "started":
                self._compaction_terminal_seen = True
            # conversationSummary is backend-echoed, LLM-influenced text that
            # reaches the dashboard — redact exfil URLs/credentials first.
            summary = redact_text(str(kiro.get(kas_wire.FIELD_CONVERSATION_SUMMARY, "") or ""))
            if status_type == "failed":
                # conversationSummary is empty on failure, so the notice would
                # collapse to "unknown error" here too — carry KAS's own reason
                # (redacted + bounded by the helper).
                summary = compaction_failure_detail(kiro)
            return [AcpEvent(kind=EVENT_COMPACTION_STATUS, text=status_type, title=summary)]
        if kind in kas_wire.STEERING_KINDS:
            # KAS mid-turn steer echo. kiro-cli sends these as `session/update`
            # discriminants (handled by the "steer" action); KAS instead puts the
            # kind under `_meta.kiro`, so route it here. `injected` is the
            # settling signal (→ EVENT_STEER_CONSUMED, which _settle_consumed_steers
            # consumes); queued/cleared mirror the kiro path. Never trust
            # backend-echoed steer text — redact before it reaches any surface.
            if kind == kas_wire.KIND_STEERING_CLEARED:
                return [AcpEvent(kind=EVENT_STEER_CLEARED)]
            text = redact_text(str(kiro.get(kas_wire.FIELD_CONTENT) or ""))
            steer_kind = (
                EVENT_STEER_CONSUMED
                if kind == kas_wire.KIND_STEERING_INJECTED
                else EVENT_STEER_QUEUED
            )
            return [AcpEvent(kind=steer_kind, text=text)]
        return []

    def _handle_kas_subagent(self, update: dict) -> list[AcpEvent] | None:
        """Route KAS PARENT sub-agent frames to EVENT_SUBAGENT_LIST.

        Returns a list of events when the frame is a PARENT sub-agent lifecycle
        frame (``kind:"agent-subtask"`` or ``pipeline``), or ``None`` when it is
        a child nested tool or an ordinary frame that should fall through to the
        shared parser (so caches populate and tool events render).
        """
        kiro = kas_wire.kiro_meta(update)
        if kiro is None:
            return None

        agent_subtask_id = kiro.get(kas_wire.FIELD_AGENT_SUBTASK_ID)
        pipeline = kiro.get(kas_wire.FIELD_PIPELINE)

        if not agent_subtask_id and not pipeline:
            return None

        # ANY frame carrying subtask lineage means the wave has started: a
        # spawned child can mutate state before its first activity frame is
        # observed, so the parent lifecycle frame itself closes the
        # registration-throttle window. Placed at the acceptance gate so the
        # pipeline arm, the parent arm and the child fall-through all close it.
        self._prompt_or_tool_seen = True

        # Pipeline frame: one entry per stage.
        if isinstance(pipeline, dict):
            stages = pipeline.get(kas_wire.FIELD_STAGES)
            if isinstance(stages, list):
                for stage in stages:
                    if not isinstance(stage, dict):
                        continue
                    s_id = stage.get(kas_wire.FIELD_AGENT_SUBTASK_ID)
                    if not isinstance(s_id, str) or not s_id:
                        continue
                    s_status = _bounded_label(stage.get("status") or "in_progress")
                    s_name = _bounded_label(stage.get("name") or stage.get("role") or "")
                    # Native-subtask residency seam (RFC §14.8): counted on
                    # the parent, never charged — see native_child_sessions.
                    # The set's admission answer also gates the display row, so
                    # this dict cannot hold an id the count refused (over-long,
                    # the parent's own, or past NATIVE_CHILD_ROSTER_CAP) and
                    # cannot outgrow the cap within a turn.
                    if not self._note_native_child(s_id):
                        continue
                    self._kas_subagent_roster[s_id] = {
                        "sessionId": s_id,
                        "sessionName": s_name,
                        "agentName": s_name,
                        "initialQuery": s_name,
                        "status": {"type": s_status, "message": ""},
                    }
            return [
                AcpEvent(
                    kind=EVENT_SUBAGENT_LIST,
                    subagents=list(self._kas_subagent_roster.values()),
                )
            ]

        # Individual agent-subtask frame (kind == "agent-subtask") → PARENT.
        is_parent = kiro.get(kas_wire.FIELD_KIND) == kas_wire.KIND_AGENT_SUBTASK

        if is_parent:
            subtask_id = str(agent_subtask_id)
            status = _bounded_label(update.get("status") or "in_progress")
            title = _bounded_label(update.get("title") or "")
            name = title.replace("Sub-agent: ", "") if title.startswith("Sub-agent: ") else title
            # Native-subtask residency seam (RFC §14.8): counted on the
            # parent, never charged — see native_child_sessions. Same
            # admission answer gates the row as in the stage loop above; the
            # frame is still a PARENT sub-agent frame either way, so the list
            # event is emitted (returning None would re-render it as an
            # ordinary tool call).
            if self._note_native_child(subtask_id):
                self._kas_subagent_roster[subtask_id] = {
                    "sessionId": subtask_id,
                    "sessionName": title,
                    "agentName": name,
                    "initialQuery": title,
                    "status": {"type": status, "message": ""},
                }
            return [
                AcpEvent(
                    kind=EVENT_SUBAGENT_LIST,
                    subagents=list(self._kas_subagent_roster.values()),
                )
            ]

        # Child nested tool_call/tool_call_update (has agentSubtaskId but NOT
        # kind:"agent-subtask" or pipeline) → return None so the caller falls
        # through to parse_session_update (populates caches + renders tool
        # events). The caller prepends the activity prefix separately.
        return None

    def _handle_kas_subagent_chunk(self, update: dict) -> list[AcpEvent] | None:
        """Route KAS agent_message_chunk with agentSubtaskId to activity.

        Returns a list when the chunk belongs to a child sub-agent, else None
        (fall through to normal chunk handling).
        """
        kiro = kas_wire.kiro_meta(update)
        if kiro is None:
            return None
        subtask_id = kiro.get(kas_wire.FIELD_AGENT_SUBTASK_ID)
        if not isinstance(subtask_id, str) or not subtask_id:
            return None
        # Must NOT have kind:"agent-subtask" or pipeline — those are parent frames
        if kiro.get(kas_wire.FIELD_KIND) == kas_wire.KIND_AGENT_SUBTASK or kiro.get(
            kas_wire.FIELD_PIPELINE
        ):
            return None
        text, _thinking = parse_text_chunk(update)
        if not text or _thinking:
            # A child's private reasoning (thinking/reasoning content) must not
            # surface as visible sub-agent activity — parity with the kiro native
            # subagent path, which only forwards non-thinking agent_message_chunk.
            return []
        # Observed child output: the wave did work, so the registration-throttle
        # window closes — the same closure the tool prefix builder applies.
        self._prompt_or_tool_seen = True
        return [
            AcpEvent(
                kind=EVENT_SUBAGENT_ACTIVITY,
                sub_session_id=subtask_id,
                text=redact_text(text),
            )
        ]

    def _build_child_tool_activity_prefix(self, update: dict) -> list[AcpEvent]:
        """Build an EVENT_SUBAGENT_ACTIVITY prefix for a child nested tool frame.

        Called when ``_handle_kas_subagent`` returns None (child tool) so the
        activity attribution is emitted BEFORE the tool events from
        ``parse_session_update``.
        """
        kiro = kas_wire.kiro_meta(update)
        if kiro is None:
            return []
        subtask_id = kiro.get(kas_wire.FIELD_AGENT_SUBTASK_ID)
        if not isinstance(subtask_id, str) or not subtask_id:
            return []
        tool_call_id = str(update.get("toolCallId") or "")
        if not tool_call_id:
            return []
        # A KAS child's nested tool call is this session's side effect for
        # replay purposes — the same closure every other child tool route
        # applies. Latched here, in the one builder every caller shares, so a
        # new call site cannot forget it.
        self._prompt_or_tool_seen = True
        title = redact_text(str(update.get("title") or ""))
        return [
            AcpEvent(
                kind=EVENT_SUBAGENT_ACTIVITY,
                sub_session_id=subtask_id,
                tool_call_id=tool_call_id,
                title=title,
            )
        ]

    def _apply_kas_context_pct(self, pct: object) -> None:
        """Apply a KAS ``context_usage`` percentage to the context meter.

        A real ``usage_update`` is authoritative (``context_tokens_from_usage``)
        and must not be clobbered. ``sanitize_pct`` (shared with the kiro-cli
        metadata path) clamps NaN/±inf/out-of-range and returns None when the
        value is absent or unparseable, so malformed telemetry degrades to
        "no reading" rather than aborting the active turn.
        """
        pct_f = self.last_prompt_stats.sanitize_pct(pct)
        if pct_f is None or self.last_prompt_stats.context_tokens_from_usage:
            return
        self.last_prompt_stats.context_pct = pct_f
        self.last_prompt_stats.note_pct_reported()
        self._backfill_context_window(pct_f)

    def _apply_kas_turn_completion(self, kiro: dict) -> None:
        """Set per-turn credits from a KAS ``turn_completion`` frame.

        Delegates the ``promptTurnSummaries`` credit sum (only ``unit ==
        "credit"`` entries count; the acp provider bills in credits) to
        ``kas_wire.turn_credits``. The frame carries the whole turn's summary, so
        the total is ASSIGNED, not accumulated: a duplicate or resume-replayed
        ``turn_completion`` reports the same total and must not inflate the
        displayed cost. A malformed frame (``turn_credits`` returns ``None``)
        leaves the prior value untouched rather than zeroing.
        """
        total = kas_wire.turn_credits(kiro)
        if total is not None:
            self.last_prompt_stats.credits = total

    def _tool_kind_cache_for_wire(self) -> dict[str, str] | None:
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

    def _identity_unreadable_cache_for_wire(self) -> dict[str, bool] | None:
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

    def _tool_call_cache_overflow_for_wire(self) -> set[str] | None:
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

    def _handle_update(self, msg: JsonRpcMessage) -> list[AcpEvent]:
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
        self, msg: JsonRpcMessage, params: dict[str, Any], update: dict[str, Any]
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
        self, params: dict[str, Any], update: dict[str, Any], reason: str
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

    @property
    def status_rejections(self) -> dict[str, int]:
        """Per-reason count of ``kirocrew/status`` frames rejected this turn."""
        return dict(self._status_rejected)

    @property
    def inflight_interactive(self) -> InteractiveClassification | None:
        """The in-flight shell tool's interactive classification, if any."""
        return self._inflight_interactive

    @property
    def native_child_sessions(self) -> frozenset[str]:
        """Harness-native child session ids observed this turn.

        The count at the parent recovery boundary (RFC §14.8): these children
        have identity (a session id) and attributable tool events, but no
        cancel or resume of their own — ``session/cancel`` on THIS session is
        the only lever, and it takes all of them. Display-only otherwise; a
        scheduler must never read this as N pausable tasks.

        Fed by three seams, all on the execution layer and never on model
        text: child-routed ``session/update`` frames (``_handle_update``), the
        ``_kiro.dev/session/update`` child stream (``_dispatch_events``), and
        the roster notifications — kiro-cli ``subagent_list`` when this handle
        is the roster's sole owner, KAS ``agentSubtaskId`` / pipeline stages
        (``_handle_kas_subagent``). A ``spawn_run`` child of this session is
        NOT here: it is a Kiro Crew task row with its own handle, and native
        grandchildren under it are counted on THAT handle (parity row H16).
        """
        return frozenset(self._native_child_sids)

    @property
    def native_child_overflow(self) -> int:
        """Native child ids seen past :data:`NATIVE_CHILD_ROSTER_CAP` this turn
        — counted, never stored, and deliberately never de-duplicated:
        de-duplicating an id means remembering it, which is the one thing the
        cap exists to refuse."""
        return self._native_child_overflow

    def _note_native_child(self, child_sid: object) -> bool:
        """Count one native child id on this handle (bounded, type-checked).

        Ids are backend-controlled: non-strings, empties, over-long values and
        this handle's own id are ignored; past the roster cap the id is
        counted in ``native_child_overflow`` instead of stored.

        Returns whether the id is TRACKED in ``native_child_sessions`` after
        the call — True for an id already known, which is the ordinary
        re-report of a status change. A caller that keeps its own per-child row
        (:attr:`_kas_subagent_roster`) keys that row on this answer, so this
        set is the single bound on every native-child store on the handle and
        no such store can outgrow :data:`NATIVE_CHILD_ROSTER_CAP`. An id this
        set refused must get no row: a row for an unremembered id could never
        be recognised as a duplicate, so it would reintroduce exactly the
        unbounded growth the cap refuses.
        """
        if (
            not isinstance(child_sid, str)
            or not child_sid
            or len(child_sid) > MAX_ACP_SESSION_ID_LEN
        ):
            return False
        if child_sid == self._session_id:
            # A parent is never its own sub-agent, in the count or in a roster row.
            return False
        if child_sid in self._native_child_sids:
            return True
        if len(self._native_child_sids) >= NATIVE_CHILD_ROSTER_CAP:
            self._native_child_overflow += 1
            return False
        self._native_child_sids.add(child_sid)
        return True

    def _note_native_roster(self, subagents: object) -> None:
        """Count every child id in a roster notification (kiro-cli
        ``subagent_list`` shape: ``sessionId`` / ``session_id`` per entry).

        Every entry is offered to :meth:`_note_native_child`, whose cap is the
        only entry bound: an id is stored below :data:`NATIVE_CHILD_ROSTER_CAP`
        and counted in ``native_child_overflow`` past it. A tighter slice here
        would drop a long roster's tail from BOTH the set and the counter, and
        drop it invisibly — a truncated tail reads exactly like a roster that
        never named the child, so the residency number would under-report and
        those children would lose their typed
        :meth:`native_child_resume_refusal`. Memory is bounded by the cap and
        not by the entry count, and the per-frame work is proportional to a
        payload the reader has already parsed — the same shape as the KAS stage
        loop in :meth:`_handle_kas_subagent`.
        """
        if not isinstance(subagents, list):
            return
        for entry in subagents:
            if isinstance(entry, dict):
                self._note_native_child(entry.get("sessionId") or entry.get("session_id"))

    def native_child_resume_refusal(self, conversation_id: str) -> str | None:
        """The typed reason a ``spawn_continue``-style resume of
        ``conversation_id`` must be refused, or None when the id is not one of
        this handle's native children.

        A native child has no conversation, task row or runtime of its own —
        resuming "it" can only mean resuming its parent — so the refusal names
        the parent and the lever that exists. Structured (prefix
        :data:`NATIVE_CHILD_NOT_RESUMABLE`) so a caller can act on it rather
        than on the generic ``conversation_gone`` a lookup miss would produce.
        """
        if conversation_id not in self._native_child_sids:
            return None
        return (
            f"{NATIVE_CHILD_NOT_RESUMABLE}: {conversation_id} is a harness-native "
            f"child of session {self._session_id}; it has no conversation of its "
            "own and cannot be resumed, steered or cancelled independently -- "
            "continue or cancel the parent session instead"
        )

    def report_native_children(self, budget: Any) -> int:
        """Report this turn's native children to a ``HostBudget``-shaped
        ``budget`` as UNCHARGED residency, returning the count reported.

        Reporting only: the budget's ``procs`` / ``rss_mb`` / ``fds`` counters
        and its admission decisions are untouched, because the children run
        inside the parent's runtime process, which is already charged. Duck-
        typed on ``report_uncharged(kind, count)`` so the daemon's budget and
        a gateway-side health mirror take the same call.
        """
        count = len(self._native_child_sids) + self._native_child_overflow
        report = getattr(budget, "report_uncharged", None)
        if callable(report):
            report(NATIVE_CHILDREN_UNCHARGED_KIND, count, label=self._session_id)
        return count

    def _input_wait_status(self, verdict: str, evidence: str) -> StructuredStatus | None:
        """The ``waiting_input`` status for the current stall, or None.

        A stall is classified as waiting for input on either of two grounds,
        and on nothing else: the oracle's own STUCK_INPUT evidence (Linux: a
        flat subtree with a process blocked reading a tty / stdin pipe), or a
        ``platform_limited`` no-progress verdict on a command the tool layer
        classified as prompt-shaped (``INTERACTIVE_NARROWING_RISKS``) — the
        one case the missing stdin evidence would have answered. A flat
        build with no interactive classification stays an opaque tool stall.

        ``safe_retry`` is True only when the classifier's read-only verdict
        holds AND the call streamed no output: a command that has printed may
        have acted, and a non-interactive retry must never replay a side
        effect (SPEC-ADDENDUM §6). ``cancellable`` is always True (the call
        can be cancelled via ``session/cancel``); ``resumable`` is False (a
        blocked process cannot be resumed with input from here).
        """
        interactive = self._inflight_interactive
        if verdict == VERDICT_STUCK_INPUT:
            grounds = "stuck_input"
        elif (
            evidence.startswith(EVIDENCE_PLATFORM_LIMITED)
            and interactive is not None
            and interactive.risk in INTERACTIVE_NARROWING_RISKS
        ):
            grounds = "platform_limited+interactive_risk"
        else:
            return None
        tool_call_id = self._inflight_tool_call_id
        output_seen = bool(tool_call_id) and tool_call_id in self._tool_output_seen
        safe_retry = bool(interactive and interactive.replay_safe and not output_seen)
        return StructuredStatus(
            session_id=self._session_id,
            tool_call_id=tool_call_id,
            phase=STATUS_PHASE_WAITING,
            wait_reason=WAIT_REASON_INPUT,
            progress_source=PROGRESS_SOURCE_PROCESS_EVIDENCE,
            cancellable=True,
            resumable=False,
            safe_retry=safe_retry,
            origin=STATUS_ORIGIN_LIVENESS_ORACLE,
            evidence=f"{grounds}: {evidence}",
        )


# --------------------------------------------------------------------------- #
# Compatibility facade. ``AcpSessionHandle``'s moved members live in the owner modules
# below, one responsibility each, bound by name in the class body, and the module-level
# code moved with the members that read it. Every name this module bound before the
# owners existed stays readable as ``kiro_crew.acp.session_handle.<name>``:
#
# * Every import of this module stays bound here, so a patch of
#   ``kiro_crew.acp.session_handle.<name>`` reaches every owner that binds the same
#   object (``_facade_mirror``), as it reached this module's own code.
# * A moved name that callers or this module's own code read is an ordinary import above.
# * Every other moved name is SERVED: absent from this namespace on purpose, read from
#   its owner through the mirror, and a write or delete through this module lands on
#   every owner binding it.
#
# ``test/test_session_handle_bindings.py`` pins the member inventory, the binding and the
# reach.
# --------------------------------------------------------------------------- #
#: Owner modules, lowest layer first: an owner imports only owners listed before it.
_SESSION_HANDLE_OWNERS: tuple[str, ...] = (
    "kiro_crew.acp.session_handle_runtime_contract",
    "kiro_crew.acp.session_handle_watchdog",
    "kiro_crew.acp.session_handle_lifecycle",
    "kiro_crew.acp.session_handle_compaction",
    "kiro_crew.acp.session_handle_turn",
    "kiro_crew.acp.session_handle_dispatch",
)
for _owner in _SESSION_HANDLE_OWNERS:
    _importlib.import_module(_owner)
#: Module-level names this module bound before the split that only owner code reads.
_SERVED: frozenset[str] = frozenset(
    {
        "_COMMAND_TURN_TIMEOUT_SECS",
        "_DRAIN_CLOSED_STOP_REASONS",
        "_PROMPT_TRANSPORT_COMMANDS",
        "_clamp_to_prompt_ceiling",
        "_warn_if_above_chat_ceiling",
    }
)
#: Name this module serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_MIRROR = _facade_mirror.install(
    _sys.modules[__name__],
    _SESSION_HANDLE_OWNERS,
    excluded=(),
    surface=frozenset(globals()) | _SERVED,
)
_EXPORTS.update(_MIRROR.served)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule

if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.acp.session_handle_turn import (
        _COMMAND_TURN_TIMEOUT_SECS,
        _DRAIN_CLOSED_STOP_REASONS,
        _PROMPT_TRANSPORT_COMMANDS,
    )
    from kiro_crew.acp.session_handle_watchdog import (
        _clamp_to_prompt_ceiling,
        _warn_if_above_chat_ceiling,
    )
