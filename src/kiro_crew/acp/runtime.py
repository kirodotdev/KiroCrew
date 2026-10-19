"""ACP Runtime for multiplexed kiro-cli sessions.

Single-reader demux architecture: one AcpRuntime owns the subprocess and a
reader task that routes frames by sessionId to per-session queues. Each
``AcpSessionHandle`` (in ``session_handle_class.py``) owns one sessionId + queue and
provides the prompt/cancel/approve/reject API.

The per-session handle, the runtime protocol it depends on, and the runtime
exceptions live in ``session_handle_class.py`` / ``session_handle_runtime_contract.py``
(the lower layer); they are re-exported here so ``from kiro_crew.acp.runtime import
AcpSessionHandle`` (and the exceptions) keeps working for existing callers and tests.
"""

from __future__ import annotations

import asyncio
import functools
import importlib
import logging
import os
import re
import signal
import subprocess  # noqa: F401  (read at call time by the process-table helpers)
import sys
import time
import uuid
import weakref  # noqa: F401  (read at call time by session-start admission)
from collections import deque
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any, Awaitable, Callable, NamedTuple, TypeVar

from kiro_crew import (
    _facade_mirror,
    acp_tool_gate,
    agent_scratch,
    member_essential_context,
    platform_compat,
    runtime_death,
)
from kiro_crew.acp import runtime_process_tree
from kiro_crew.acp import runtime_session_create as _rt_create
from kiro_crew.acp import runtime_session_load as _rt_load
from kiro_crew.acp import runtime_session_setup as _rt_setup
from kiro_crew.acp import runtime_spawn_entry as _rt_entry
from kiro_crew.acp import runtime_spawn_launch as _rt_launch
from kiro_crew.acp import runtime_start
from kiro_crew.acp import runtime_tree_kill as _rt_tree
from kiro_crew.acp._dispatch import (
    agent_version_from_init,
    attach_kas_custom_agents,
    build_session_new_params,
    parse_session_modes,
    redact_backend_text,
    redact_text,
)
from kiro_crew.acp._dispatch import reject_option_id as _reject_option_id
from kiro_crew.acp._dispatch import (
    set_mode_params,
)
from kiro_crew.acp._frame_record import record_frame
from kiro_crew.acp.agent_env import agent_env_overlay
from kiro_crew.acp.client import (
    _apply_pod_home_remap,
    _is_safe_oauth_url,
    _KiroExecutableTrustError,
    _loggable_request_id,
    apply_pod_bundle_spawn,
    finish_suspended_spawn,
)
from kiro_crew.acp.harness import (
    HarnessAdapter,
    NotificationAliases,
    SessionExtras,
    SpawnContext,
    SpawnPlan,
    harness_for,
)
from kiro_crew.acp.harness.kas import PROTOCOL_VERSION_KAS
from kiro_crew.acp.harness.kiro import KIRO_CLI_SUBCMD, PROTOCOL_VERSION
from kiro_crew.acp.kas_agents import hoist_managed_servers
from kiro_crew.acp.kas_host_auth import HostAuthCallbackError
from kiro_crew.acp.kas_transport import (
    KAS_AUTH_CALLBACK_ERROR_CODE,
    METHOD_KAS_AUTH_GET_ACCESS_TOKEN,
)
from kiro_crew.acp.launch import LaunchRequest, LaunchTools, launch
from kiro_crew.acp.mcp_mount_check import schedule_mount_check
from kiro_crew.acp.mcp_ref_guard import (
    warn_remote_header_secret_refs,
    warn_unresolved_server_refs,
)
from kiro_crew.acp.mcp_session_report import (
    NAME_CAP,
    active_custom_agent,
    required_managed_servers,
    roster_names,
    sanitize_sink_text,
)
from kiro_crew.acp.runtime_process_tree import (
    ChildRecord,
    _capture_child_records,
    _get_child_pids,
    _kill_escaped_children,
)
from kiro_crew.acp.runtime_session_create import (
    _ENTITLEMENT_PROBE_TIMEOUT,
    _ENTITLEMENT_PROBE_TTL_SECS,
    AcpSessionStartTimeout,
)
from kiro_crew.acp.runtime_session_setup import (
    AcpToolSurfaceBindingError,
    AcpWorkspaceBindingError,
    _disable_check_scope,
    _MirroredSessionMcp,
    _pooled_session_servers_and_ref_spec,
    _ref_spec_snapshot,
)
from kiro_crew.acp.runtime_spawn_launch import (
    _ACP_RUNTIME_RESPAWN_BACKOFF_S,
    _INITIALIZE_TIMEOUT,
    _T,
    _launch_tools,
    _point_private_state_at_scratch,
    _retrying_spawn_factory,
    _seed_private_state,
)
from kiro_crew.acp.runtime_start import (
    _INIT_NOTIFICATION_BUFFER_LIMIT,
    _SESSION_NEW_TIMEOUT,
    _START_COLLECT_TIMEOUT_DEFAULT,
    StartCollector,
    StartPermit,
    _cold_start_counts,
    _resolve_start_collect_timeout,
    _split_init_frames,
)
from kiro_crew.acp.runtime_tree_kill import _escapee_is_still_ours, _prune_dead_descendants
from kiro_crew.acp.session_handle import (
    NATIVE_CHILD_ROSTER_CAP,
    AcpFrameTooLarge,
    AcpModeNotFound,
    AcpRequestTimeout,
    AcpRuntimeDead,
    AcpRuntimeError,
    AcpRuntimeOverloaded,
    AcpRuntimeProtocol,
    AcpSessionHandle,
    _load_watchdog_settings,
    advertised_models_from_session,
)
from kiro_crew.acp.session_mcp import (
    agent_spec_snapshot,
    session_mcp_disabled_tools,
    session_mcp_server_is_disabled,
)
from kiro_crew.acp.transport_errors import (
    is_auth_failure_output,
    is_sandbox_init_failure_output,
    registration_throttle_line,
)

# The block-wide noqa covers _RESPONSE_WRITE_MIN_PROGRESS_BYTES, which is bound
# for re-export rather than used here: the refactor-facade tests pin runtime and
# client to one shared copy of this floor (test_acp_refactor_transport_framing /
# test_acp_refactor_facade), while _stall_window_phrase owns the floor's only read.
from kiro_crew.acp.transport_framing import (  # noqa: F401
    _RESPONSE_WRITE_BOUND_SECS,
    _RESPONSE_WRITE_MIN_PROGRESS_BYTES,
    _STDOUT_BUFFER_LIMIT,
    OversizeLineUnrecoverable,
    RequestWriteResult,
    _drain_oversize_line,
    _stall_window_phrase,
    encode_frame,
    response_write_window_secs,
    settle_drain,
    write_notification_best_effort,
    write_request_frame_bounded,
    write_response_frame_bounded,
)
from kiro_crew.acp.types import (
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKENDS_MARKDOWN_AGENT_SPECS,
    ACP_BACKENDS_SERIAL_SESSION_STARTS,
    ACP_BACKENDS_SESSION_MCP_ARRAY,
    MCP_ROSTER_COMPLETE_NOTE,
    METHOD_AGENT_SWITCHED,
    METHOD_KAS_MCP_RESET_SERVER,
    METHOD_KAS_OPEN_EXTERNAL_URL,
    METHOD_MCP_OAUTH_REQUEST,
    METHOD_MCP_SERVER_INIT_FAILURE,
    METHOD_MCP_SERVER_INITIALIZED,
    METHOD_REQUEST_PERMISSION,
    METHOD_SESSION_LOAD,
    METHOD_SESSION_NEW,
    METHOD_SET_MODE,
    JsonRpcMessage,
    JsonRpcRequest,
    backends_retired_by_host_logout,
    overlay_project_scope,
)
from kiro_crew.agent import ensure_agent_materialized, markdown_spec_for_agent
from kiro_crew.agent_files import kas_wire_agent_id
from kiro_crew.agent_sdk.drivers import acp as acp_driver
from kiro_crew.agent_sdk.tool_search import (
    ToolSearchSettings,
    kas_client_meta_settings,
    spec_grants_tool_search,
    with_client_meta_settings,
)
from kiro_crew.agent_spec_format import NATIVE_SKILL_ALIAS_PREFIX
from kiro_crew.browser_cli.launch import browser_session_env, browser_socket_env
from kiro_crew.config import live
from kiro_crew.config.paths import data_home, kiro_agents_dir
from kiro_crew.constants import (
    INITIALIZE_TIMEOUT_SECS,
    KIROCREW_RUNTIME_AGENT_ENV,
    KIROCREW_SPAWN_HOME_ENV,
    KIROCREW_SPAWN_INSTANCE_ENV,
)
from kiro_crew.dashboard.side_readonly_spec import unavailable_mode_explanation
from kiro_crew.env import augmented_path, resolve_krb5_ccname
from kiro_crew.executors import subprocess_executor
from kiro_crew.json_line import parse_json_object_line
from kiro_crew.mcp_gateway.claim import mint_stub_session_token, send_claim
from kiro_crew.mcp_gateway.session_servers import (
    attach_stub_session_token,
    injection_server_names,
    pooled_session_servers,
)
from kiro_crew.member_essential_context import MemberEssentialContextError
from kiro_crew.metrics.events import (
    CHILD_PERMISSION_DENIED,
    CHILD_PERMISSION_ROUTED,
    DROPPED_FRAMES,
    SKILL_VIEW_FALLBACKS,
    emit_counter,
)
from kiro_crew.owner_only_files import ensure_directory
from kiro_crew.providers.mirrors.registry import has_mirror, mirror_for
from kiro_crew.resource_status import inject_xdist_auto_cap
from kiro_crew.runtime_ownership import authorize_runtime_kill, outstanding_leases
from kiro_crew.sandbox import (
    BoundWorkspaceMismatch,
    _forward_ssh_auth_sock,
    agents_slice_throttling,
    assert_voice_runtime_outside_agent_workspace,
    bind_voice_safe_agent_workspace_async,
    cgroup_scope_argv,
    create_subprocess_limited,
    name_scope_unit,
    release_bound_agent_workspace,
    resolve_bound_session_workspace,
    scope_unit_name,
    scrub_agent_subprocess_env,
    wrap_argv,
    wrap_argv_async,
    wrapped_by_crew_sandbox,
)
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.session_pid import (
    _pgroup_has_member_besides,
    _pid_gone_or_unmanaged,
    _pid_start_token,
    _replace_child_pids,
    _signal_orphaned_runtime_group,
    _track_pid,
    _track_session_pid,
    _untrack_child_pids,
    _untrack_pid,
    _untrack_pid_if_dead,
    _untrack_root_by_identity,
    _untrack_session_pid,
    group_vouching_available,
    register_protected_pid,
    unregister_protected_pid,
)
from kiro_crew.session_token_sig import publish_session_token
from kiro_crew.start_priority import (
    START_QUEUE_ADMISSION,
    START_QUEUE_SESSION_NEW,
    StartPriority,
    notify_start_queue,
)
from kiro_crew.validation import MAX_ACP_SESSION_ID_LEN, MODEL_ID_RE

logger = logging.getLogger(__name__)


__all__ = [
    "AcpRuntime",
    "AcpRuntimeError",
    "AcpSessionStartTimeout",
    "AcpToolSurfaceBindingError",
    "AcpWorkspaceBindingError",
    "AcpRuntimeDead",
    "AcpRequestTimeout",
    "AcpRuntimeOverloaded",
    "SessionStartGate",
    "StartCollector",
    "AcpRuntimeProtocol",
    "AcpSessionHandle",
    # Re-exported from the harnesses that own them, so a caller that read them
    # off this module keeps working and there is still exactly one declaration
    # of each per-host value.
    "KIRO_CLI_SUBCMD",
    "PROTOCOL_VERSION",
    "PROTOCOL_VERSION_KAS",
]


# ── AcpRuntime ──


# How many in-flight request ids to name in the oversize-frame warning. A dropped
# frame can carry a response; when its head names the awaited request, that
# request fails with AcpFrameTooLarge, and otherwise its caller times out, which
# naming what was in flight at the drop makes attributable. Capped so the line
# stays bounded.
_DROP_IDS_IN_LOG = 8

# The ``id`` of a JSON-RPC envelope near the front of a dropped oversize frame. A
# numeric id only: this runtime numbers its own requests, and a string id or a
# nested mode ``"id": "<name>"`` is never one of them.
_OVERSIZE_FRAME_ID_RE = re.compile(rb'"id"\s*:\s*(\d{1,18})\s*[,}]')


def _oversize_frame_request_id(head: bytes) -> int | None:
    """The request id a dropped oversize frame answered, when its head names one.

    A frame carrying ``"method"`` is a request or notification FROM the host, whose
    id counter is independent of ours, so it never names one of our requests.
    """
    if b'"method"' in head:
        return None
    match = _OVERSIZE_FRAME_ID_RE.search(head)
    if match is None:
        return None
    # The envelope's own id precedes its payload; one found after ``result`` /
    # ``error`` began is a nested field, not the envelope's.
    for key in (b'"result"', b'"error"'):
        at = head.find(key)
        if 0 <= at < match.start():
            return None
    return int(match.group(1))


def _oversize_frame_message(size: int) -> str:
    """The error an awaited request fails with when its reply was dropped as oversize."""
    return (
        f"The agent backend's reply was {size:,} bytes, over Kiro Crew's "
        f"{_STDOUT_BUFFER_LIMIT:,}-byte frame limit, so it was dropped. On kiro-cli, a "
        "session/new or session/load reply lists every agent in ~/.kiro/agents with its "
        "description and welcomeMessage, so an agent package whose specs carry very large "
        "welcomeMessage text is the usual cause: trim those fields, or remove agents you "
        "do not use."
    )


# Cap on the stderr text folded into a process-exit death reason. The reason
# is what the chat error card shows, so it must stay one readable line: the
# LAST non-empty stderr line the drain captured, truncated to this many
# characters -- roughly one card line. The 20-line ring itself is unchanged.
_STDERR_REASON_TAIL_CHARS = 200
# The one stderr signature that names a host fault rather than a kiro-cli
# fault: every sandboxed spawn fails with it once the runtime tmpfs the
# launcher stages mount sources on runs out of space or inodes. Point the
# operator at the doctor check that measures that filesystem.
_ENOSPC_MARKER = "no space left on device"
_ENOSPC_HINT = (
    "the runtime tmp filesystem is out of space or inodes; run `kirocrew doctor` "
    "(Runtime tmpfs section)"
)


def _proven_death_cause(tail: str) -> tuple[str, str] | None:
    """The line in *tail* that EXPLAINS a death and its operator hint, or None.

    Returns ``(evidence, hint)`` -- the matched line, and any pointer that line
    earns -- rather than one joined string, because the caller caps what it shows.
    Joining first would let a long line push its own hint past the cap, which is
    precisely backwards: the hint is the short part the operator needs and the
    evidence is the long part that can afford to be trimmed. ``hint`` is empty
    when the signature carries no pointer.

    A child's last stderr line is not evidence of why it died, and reporting it
    as the cause sends the reader after the wrong thing. Measured over one
    gateway's fleet: every sampled runtime death carried ``rc=-15``, an ordinary
    SIGTERM teardown, while the ``HTTP 404`` registration line that gets pasted
    as the reason appears in 3 of 41 deaths and once on a runtime that does not
    die at all. So the cause slot is reserved for a shape that
    describes a death, and an unexplained one is reported as the exit status it
    is, with the tail kept for whoever asks the log for it.

    Both signatures are matched per LINE by their own helpers and searched over
    the WHOLE retained tail rather than only its last line, because which line a
    child printed last is a race with its own flushing and says nothing about
    which line matters.
    """
    throttled = registration_throttle_line(tail)
    if throttled:
        return throttled, ""
    for line in tail.splitlines():
        if _ENOSPC_MARKER in line.lower():
            return line.strip(), _ENOSPC_HINT
    return None


def _rc_phrase(rc: object) -> str:
    """``rc=N``, and for a signal death the SIGNAL's name beside it.

    A negative returncode is POSIX's ``-signum``, which is the whole exit status
    of a killed child -- and the number alone is the part an operator has to look
    up. Naming it is what makes the ordinary teardown legible as one:
    ``rc=-15 (signal SIGTERM)`` reads as "something asked it to stop", where a
    bare ``rc=-15`` beside a stray stderr line reads as a crash.

    Anything that is not a negative int -- a real exit code, the ``?`` a
    never-spawned process yields -- is rendered unchanged, so this only ever adds
    a name it can prove.
    """
    if isinstance(rc, int) and not isinstance(rc, bool) and rc < 0:
        try:
            name = signal.Signals(-rc).name
        except ValueError:
            return f"rc={rc}"
        return f"rc={rc} (signal {name})"
    return f"rc={rc}"


# JSON-RPC 2.0 "Method not found" — the reader loop answers an ownerless
# server→client request with this itself (see _answer_ownerless_request);
# mirrors the private constant AcpClient keeps for its own dispatch sites.
_JSONRPC_METHOD_NOT_FOUND = -32601
_REQUEST_TIMEOUT = 30.0
# How long an MCP sign-in may hold its slot. The engine answers the reset when
# its connect attempt ends, which the server's connect timeout bounds: 60 s by
# default and at most 600 s. This only frees the slot if no answer ever comes,
# so it sits past the longest attempt: a slot freed while the engine can still
# send that attempt's link would hand the link to the next sign-in.
_MCP_SIGN_IN_TIMEOUT = 630.0
# How long a credential re-attempt (a reset without startOAuth) may hold off a
# sign-in. It opens no consent URL, so the slot does not have to outlast the
# longest connect: past the engine's default 60 s connect timeout it is freed,
# and a URL the engine still sends for it is refused because no sign-in is in
# flight, or would reach a sign-in started since, which shows its own server.
_MCP_REATTEMPT_TIMEOUT = 75.0
# JSON-RPC server-error code for a consent URL Crew did not show anyone.
_MCP_URL_NOT_OPENED = -32000


class _PendingRequests(dict):
    """``{req_id: Future}`` for awaited control-plane requests, with ownership transfer.

    A request that timed out is normally popped: nobody will read its answer.
    ``adopt(req_id)`` is the other choice -- keep the entry so the late answer
    still resolves the future, and record who owns it now. The reader loop
    keeps popping on response exactly as before; only the timeout path changes.
    """

    def __init__(self) -> None:
        super().__init__()
        self.adopted: set[int] = set()

    def adopt(self, req_id: int) -> "asyncio.Future[dict[str, Any]] | None":
        future = self.get(req_id)
        if future is None:
            return None
        self.adopted.add(req_id)
        return future

    def pop(self, key, default=None):  # type: ignore[override]
        self.adopted.discard(key)
        return super().pop(key, default)

    def clear(self) -> None:
        self.adopted.clear()
        super().clear()


# Caps for the MCP progress line attached to a session-start timeout: a
# 70-server agent must not turn one error into a multi-kilobyte string, and
# neither a server's own error text nor its NAME is trusted for length. Both are
# config-derived, and an installed app supplies its own server names.
_MCP_PROGRESS_NAME_CAP = 8
_MCP_PROGRESS_ERROR_CAP = 120
_MCP_PROGRESS_NAME_LEN_CAP = 64


def _strip_unprintable(text: str) -> str:
    """Drop the control characters a whitespace collapse cannot reach.

    ``str.split`` removes whitespace controls (newline, tab, CR), but ESC and
    the other non-whitespace controls survive it, and a terminal rendering the
    gateway log interprets them -- an MCP server's failure text could forge or
    recolor terminal output. Spaces are printable, so a collapsed string keeps
    its word separation.
    """
    return "".join(ch for ch in text if ch.isprintable())


def _sanitize_progress_name(name: str) -> str:
    """Make one MCP server name safe to put in a log line and an exception.

    A name is config-derived, so an installed app chooses it. Four hazards, all
    closed here rather than at each use: an embedded newline would forge a line
    in the gateway log, a non-whitespace control (ESC) would inject terminal
    escapes into it, an unbounded name would defeat the count cap that keeps
    one error from becoming a wall of text, and a name carrying
    credential-shaped text would leak it into a sink the error message reaches.
    Whitespace collapse and the control strip run AFTER redaction so a
    redaction marker cannot reintroduce a break.
    """
    scrubbed, _ = redact_exfiltration_urls(name)
    scrubbed, _ = redact_credentials(scrubbed)
    return _strip_unprintable(" ".join(scrubbed.split()))[:_MCP_PROGRESS_NAME_LEN_CAP]


def _capped_names(names: list[str]) -> str:
    """Join names for an error line, truncating the tail to a countable summary.

    A pure formatter: names arrive already sanitized from the two points that
    admit them, so a composite like ``name (error)`` keeps its own error cap
    instead of being re-truncated to a name's length.
    """
    head = names[:_MCP_PROGRESS_NAME_CAP]
    rest = len(names) - len(head)
    joined = ", ".join(head)
    return f"{joined} (+{rest} more)" if rest > 0 else joined


# Requests a backend in ACP_BACKENDS_SERIAL_SESSION_STARTS answers one at a time.
# kiro-cli (2.26 and earlier) awaits each of these inside its ACP connection's
# dispatch loop, so while one waits on a slow MCP server or the model list, every
# message behind it waits too, including the next session start. A start sent
# behind one therefore spends its own budget on the earlier request.
_ONE_AT_A_TIME_METHODS = frozenset({METHOD_SESSION_NEW, METHOD_SESSION_LOAD, METHOD_SET_MODE})


def _queued_behind_note(ahead: list[tuple[str, float]]) -> str:
    """Describe the requests a timed-out session start was queued behind.

    ``ahead`` holds ``(method, seconds sent before this start)`` for each request
    in :data:`_ONE_AT_A_TIME_METHODS` that was outstanding when the start went out
    and was STILL unanswered at its deadline. An empty list says nothing.
    """
    if not ahead:
        return ""
    counts: dict[str, int] = {}
    for method, _age in ahead:
        counts[method] = counts.get(method, 0) + 1
    kinds = ", ".join(f"{n} {m}" for m, n in sorted(counts.items()))
    oldest = max(age for _m, age in ahead)
    return (
        f"queued behind {len(ahead)} earlier request(s) this agent process had not "
        f"answered by the deadline ({kinds}; oldest sent {oldest:.0f}s earlier), "
        "and it answers those one at a time"
    )


# Teardown must be snappy: a session is usually terminated on a hot path
# (background task done, subagent reaped). kiro-cli's terminate handler responds
# as soon as it enqueues the eviction (the actual shutdown runs in its actor
# loop), so a healthy runtime acks well under this bound; a slow/dead one must
# not turn teardown into a multi-second stall.
_TERMINATE_TIMEOUT = 5.0

# Default recycling thresholds for long-lived multiplexed runtimes (see
# _is_stale()). These are conservative defaults chosen to recycle well before
# the unbounded growth observed in production (multi-GB RSS after ~24h of
# uptime with no per-turn compaction) while still amortizing process-spawn
# cost across many background prompts.
_DEFAULT_MAX_AGE_SECS = 6 * 3600  # 6 hours
_DEFAULT_MAX_RSS_MB = 500.0  # 500 MiB

# Below this uptime the RSS staleness probe is skipped entirely (see
# _is_stale()). A freshly-(re)used runtime has not had time to grow, so this
# keeps the hot get_bg_session reuse path — which holds _bg_runtime_lock —
# CPU-only for young runtimes and only pays the offloaded RSS probe once a
# runtime has lived long enough to plausibly have ballooned.
_RSS_PROBE_MIN_AGE_SECS = 300.0  # 5 minutes

# ── Awaited-request error formatting ──
#
# kiro-cli returns this when session/set_mode names an agent it cannot resolve:
# no ``<name>.json`` in its agents directory, or one published after the process
# started, which kiro-cli 2.26.0 does not pick up. The wire shape is a
# bare -32603 "Internal error", so nothing about the frame says which.
# The name charset is bounded to what a real spec filename can hold (see
# validation of agent names elsewhere) rather than a greedy match, so a hostile
# or malformed backend string is not echoed back into a user-facing message.
_MODE_NOT_FOUND_RE = re.compile(r"""Mode ['"](?P<name>[A-Za-z0-9._-]{1,64})['"] not found""")


def _format_runtime_rpc_error(error: object) -> str:
    """Format an awaited-request JSON-RPC error into user-facing text.

    Awaited requests are the handshake ones — ``initialize``, ``session/new``,
    ``session/set_mode`` — so this is NOT the same population as
    ``client._format_acp_error``, which rewrites PROMPT-time provider failures
    (throttling, auth, 5xx) and has no branch that matches a missing agent spec.
    The two are deliberately separate rather than merged: their inputs come from
    different protocol phases and share no shape.

    Exactly one shape is rewritten today: ``Mode '<name>' not found``. Left raw it
    surfaces to the user as ``RPC error: {'code': -32603, 'message': 'Internal
    error', 'data': "Mode 'kirocrew' not found"}`` — which names an internal ACP
    concept and reads as a backend bug. For an authored spec the cause is a local
    file and the fix is one command. For a skill-view alias it is neither: the
    alias is generated, so setup does not restore it, and the file may be on
    disk but unloaded by this process, so that text names the retry and the
    projection switch instead. Every other shape falls through to the raw dict,
    so a shape nobody has classified is surfaced rather than swallowed.
    """
    if isinstance(error, dict):
        match = _MODE_NOT_FOUND_RE.search(str(error.get("data", "") or ""))
        if match:
            name = match.group("name")
            if name.startswith(NATIVE_SKILL_ALIAS_PREFIX):
                # A skill-view alias is Crew-generated, not an installed spec, so
                # setup cannot restore it, and "Mode not found" does not mean the
                # file is missing: kiro-cli also answers it for a spec that was
                # published after the process started.
                from kiro_crew.acp.skill_projection import remembered_view_source

                source = remembered_view_source(name)
                who = f"agent '{source}'" if source else "this agent"
                return (
                    f"kiro-cli could not switch to the skill view '{name}' that Kiro "
                    f"Crew generated for {who}. It may be on disk in "
                    f"{kiro_agents_dir()} but not loaded by this kiro-cli process. "
                    f"Start a new session to retry. If it keeps failing, set "
                    f"KIROCREW_NATIVE_SKILL_PROJECTION=0 in the gateway's environment "
                    f"and restart the gateway."
                )
            # ``--clean`` is not the repair: it skips merging the existing
            # config, so it drops the operator's own MCP servers and tools.
            return (
                f"Agent spec '{name}' is not installed: kiro-cli found no "
                f"'{name}.json' in {kiro_agents_dir()}. Every turn fails until it "
                f"is restored — repair with `kirocrew setup --agent-only`, "
                f"then restart the gateway."
            )
    return f"RPC error: {error}"


def _runtime_rpc_exception(error: object) -> AcpRuntimeError:
    """The exception an awaited request fails with; :func:`_format_runtime_rpc_error`'s text.

    A missing mode is an :class:`AcpModeNotFound` carrying the name, so the
    ``set_mode`` path can recover from a skill-view alias the host has not
    loaded yet without parsing the sentence it would show a user.
    """
    message = _format_runtime_rpc_error(error)
    if isinstance(error, dict):
        match = _MODE_NOT_FOUND_RE.search(str(error.get("data", "") or ""))
        if match:
            return AcpModeNotFound(message, match.group("name"))
    return AcpRuntimeError(message)


# After a skill-view alias is published, how long ``session/set_mode`` waits
# before each retry of a "not found" answer. kiro-cli reloads its agents
# directory 500 ms after the last data write to it (a trailing debounce, then a
# full rescan; measured selectable 0.7-1.5 s after publication) and never on a
# rename, and a miss does not trigger a reload itself. Short even steps land
# soon after a reload; the total (3 s) stays well inside the set_mode budget, and
# after it the session start fails with the restart remedy rather than activate
# any older copy of the agent.
_PROJECTED_MODE_RETRY_DELAYS_SECS: tuple[float, ...] = (0.5, 0.5, 0.5, 0.5, 1.0)


async def _prepare_projection_to_completion(
    prepare: Callable[..., Any], *args: Any, **kwargs: Any
) -> tuple[Any, bool]:
    """Run *prepare* in a worker thread and wait for it even through cancellation.

    Returns ``(result, cancelled)``. A cancelled start must not leave the
    projection lock while its worker is still running: the worker may already
    have read an edit, and the caller adopts that view before re-raising. The
    wait is bounded by the preparation itself (its alias lock times out)."""
    worker = asyncio.ensure_future(asyncio.to_thread(prepare, *args, **kwargs))
    cancelled = False
    while not worker.done():
        try:
            # ``wait`` never cancels the worker when this task is cancelled.
            await asyncio.wait({worker})
        except asyncio.CancelledError:
            cancelled = True
    if cancelled and worker.exception() is not None:
        # The worker's own failure must not replace the cancellation the caller
        # asked for; an unadopted preparation is recorded by the caller either way.
        raise asyncio.CancelledError
    return worker.result(), cancelled


# ── Unroutable-frame drop accounting ──
#
# The reader drops any frame it cannot route (see _reader_loop). That is
# CORRECT behaviour, but logging it per frame is not: kiro-cli is multiplexed,
# so every frame for a torn-down or not-yet-registered sessionId takes the drop
# branch, and a backend that keeps streaming after teardown makes that an
# unbounded STEADY STATE, not a burst. Measured on an operator host: ~60
# lines/second for 6+ hours from one gateway PID, taking 33–59% of every
# gateway.log rotation — which, at RotatingFileHandler(maxBytes=2MB,
# backupCount=3) (see cli_logging.py), rolls the genuine diagnostics needed for an
# incident out of the retained 8MB window before anyone can read them.
#
# So the per-frame line is collapsed into a periodic count keyed by
# (sessionId, method). The key must stay PER SESSION: the incident's decisive
# signal was that two DIFFERENT session UUIDs were flooding at once, which a
# single global tally would hide.
_DROP_SUMMARY_INTERVAL_SECS = 60.0
# Hard cap on distinct (sessionId, method) keys held between flushes. Both
# halves of the key are backend-controlled, so an unbounded map would be a
# memory sink; reaching the cap forces an early flush instead of growing.
_DROP_SUMMARY_MAX_KEYS = 64
# Backend-controlled key text is redacted whole and then capped before it is
# stored, so a pathological sessionId/method (a stdout line may be up to
# _STDOUT_BUFFER_LIMIT) cannot be retained at full length by the map either.
_DROP_SUMMARY_KEY_MAX_CHARS = 80
# Stands in for the sessionId half of the key on the no-sessionId broadcast
# path, which has no session to name.
_DROP_NO_SESSION = "-"
# Stands in for EITHER half of the key when the backend supplied no usable
# string: an absent `method`, or a value of the wrong JSON type (see
# _drop_key_part).
_DROP_KEY_PLACEHOLDER = "?"

# SEL/metric reason for a permission request auto-rejected while the last
# subagent roster named more children than NATIVE_CHILD_ROSTER_CAP. Distinct
# from `unregistered_session_auto_reject` because the two say different things:
# that reason claims the backend never announced the session, and past the cap
# the backend may well have — the id simply is not remembered, so it cannot be
# verified as this owner's child. Auditing an announced child's denial as
# "unregistered" would make the one signal that a cap truncation cost a real
# approval indistinguishable from an ordinary unknown-session frame.
_ROSTER_OVERFLOW_REJECT_REASON = "roster_overflow_auto_reject"
# The default: the sessionId is in no roster this runtime remembers, and no
# roster it remembers was truncated either, so the backend genuinely never
# announced this child to us.
_UNREGISTERED_REJECT_REASON = "unregistered_session_auto_reject"


class AcpRuntimeStdinStalled(AcpRuntimeDead):
    """The shared stdin made no progress for the response-write bound.

    A subclass so the off-loop auto-answer can keep its ``send_stalled`` audit
    reason (the bound fires inside ``send_response``, ahead of that path's own
    outer timeout) while every ``except AcpRuntimeDead`` handler keeps working.
    """


# Minimum seconds between throttled DEBUG summaries of repeated roster
# truncation. A `subagent/list_update` is a backend-controlled notification
# re-broadcast on every child status change, so above the cap the WARNING it
# earns is a steady state at the frame rate, not a burst — the same retention
# hazard `_note_dropped_frame` exists to avoid, one level louder. Bound to the
# unroutable-frame interval rather than given a second value: both answer the
# one question "how often may a suppressed high-frequency condition restate
# itself in gateway.log", and a second knob is a second throttle to reason
# about in the same module.
_ROSTER_OVERFLOW_SUMMARY_INTERVAL_SECS = _DROP_SUMMARY_INTERVAL_SECS


# Re-exported from the module that owns the resolver, not re-declared. A second literal
# here is free to drift from the name the spawn actually uses, and the reclaim sweep
# projects its marker set from the same registry the owner's value is asserted against.
# ``CLIENT_NAME``/``CLIENT_VERSION`` ride along for the same reason: BOTH transports
# send them in one ``clientInfo`` object, so a second pair here would be free to
# report a different client to the same host -- which is exactly how the flat
# ``clientName`` regression stayed invisible on one transport while the other was
# correct.
from kiro_crew.acp.client import (  # noqa: E402  (re-export, not a new name)
    CLIENT_NAME,
    CLIENT_VERSION,
    KIRO_CLI_BIN,
)

# Re-exported, not re-declared. Each host's ACP revision and its own ``acp``
# subcommand belong to that host's harness, which is what the handshake now
# reads; a second copy here would be free to drift from the value actually sent,
# and these two disagree on the field's TYPE as well as its value.


def _drop_key_part(value: object) -> str:
    """Bounded, hashable string for one half of a (sessionId, method) drop key.

    BOTH halves arrive verbatim from backend JSON: `JsonRpcMessage.from_dict`
    copies `method` and `params` with no validation, so the `str | None`
    annotation is documentation, not enforcement — `{"method": 123}` yields an
    int, and `params.sessionId` is `Any`. Slicing such a value raises TypeError
    *inside* `_reader_loop`, the single owner of this process's stdout, which
    marks the runtime dead and tears down EVERY multiplexed session on it. One
    malformed frame must not cost every session, so anything that is not a
    `str` (including the legitimate absent-`method` `None`) becomes the
    placeholder before it is sliced or used as a dict key.
    """
    if not isinstance(value, str):
        return _DROP_KEY_PLACEHOLDER
    # Redact BEFORE the retention cap: the key text is backend-authored and
    # the flush logs it, so a slice taken first could sever a credential at
    # the cut into a fragment that matches none of the redactor's patterns.
    return redact_backend_text(value)[:_DROP_SUMMARY_KEY_MAX_CHARS]


if TYPE_CHECKING:
    from kiro_crew.acp.skill_projection import NativeSkillProjection

#: The owner modules whose functions ``AcpRuntime`` binds as its members.
_MEMBER_OWNERS = frozenset(
    {
        "kiro_crew.acp.runtime_tree_kill",
        "kiro_crew.acp.runtime_spawn_launch",
        "kiro_crew.acp.runtime_spawn_entry",
        "kiro_crew.acp.runtime_session_setup",
        "kiro_crew.acp.runtime_session_create",
        "kiro_crew.acp.runtime_session_load",
    }
)


def _own_methods(cls: type) -> type:
    """Make each member *cls* binds from an owner module read as a method of *cls*.

    A bound function annotates ``self`` so mypy checks its body as a method body;
    this drops that annotation from the runtime object and names the function after
    the class attribute (``AcpRuntime._spawn_admitted``), so its ``inspect.signature``
    and ``__qualname__`` are what the one-module class had, and so are the reprs of
    the coroutines and tasks it starts. Only ``__module__`` moves: it names the owner
    module that holds the body.
    """
    for name, member in vars(cls).items():
        function = getattr(member, "__func__", member)
        if getattr(function, "__module__", None) in _MEMBER_OWNERS:
            function.__annotations__.pop("self", None)
            function.__qualname__ = f"{cls.__qualname__}.{name}"
    return cls


@_own_methods
class AcpRuntime:
    """Owns one kiro-cli acp subprocess with single-reader demux.

    The _reader_task is the ONLY coroutine that reads from stdout.
    It routes frames by:
      - 'id' field in _pending_requests → resolve Future (for send_and_await)
      - 'id' field in _routed_requests → put in session queue (for prompt responses)
      - params.sessionId → _session_queues[sessionId].put(msg)
      - no sessionId → broadcast to all session queues
    """

    # Fields a bound member assigns first, outside ``__init__``: declared for type
    # checkers only, so the class carries no annotations at run time and a runtime
    # built with ``__new__`` still reads them through the ``getattr`` defaults their
    # readers pass.
    if TYPE_CHECKING:
        _native_skill_projection: NativeSkillProjection | None
        _skill_projection_generation: int
        _spawn_skill_projection: NativeSkillProjection | None

    # The process tree and its teardown: ``kiro_crew.acp.runtime_tree_kill``.
    _discard_bound_workspace = _rt_tree._discard_bound_workspace
    _root_identity = _rt_tree._root_identity
    _root_identity_holds = _rt_tree._root_identity_holds
    refresh_process_tree = _rt_tree.refresh_process_tree
    _snapshot_descendants = _rt_tree._snapshot_descendants
    _signal_tree = _rt_tree._signal_tree
    kill = _rt_tree.kill
    _kill_inner = _rt_tree._kill_inner
    # Launching the child process and its handshake: ``kiro_crew.acp.runtime_spawn_launch``.
    _discard_sandbox_cleanup = _rt_launch._discard_sandbox_cleanup
    _to_thread_guarding_sandbox = _rt_launch._to_thread_guarding_sandbox
    _resolve_spawn_plan = _rt_launch._resolve_spawn_plan
    _initialize_handshake = _rt_launch._initialize_handshake
    _spawn_admitted = _rt_launch._spawn_admitted
    _failed_start_cleanup = _rt_launch._failed_start_cleanup
    _record_tree_before_failed_start_kill = _rt_launch._record_tree_before_failed_start_kill
    _kill_failed_start_escapees = _rt_launch._kill_failed_start_escapees
    _handshake_client_capabilities = _rt_launch._handshake_client_capabilities
    _projected_spawn_spec = _rt_launch._projected_spawn_spec
    _source_agent = staticmethod(_rt_launch._source_agent)
    # The spawn entry point: ``kiro_crew.acp.runtime_spawn_entry``.
    spawn = _rt_entry.spawn
    _spawn_admitted_rederiving_once = _rt_entry._spawn_admitted_rederiving_once
    _revive_after_failed_start = _rt_entry._revive_after_failed_start
    # What a session start is built from: ``kiro_crew.acp.runtime_session_setup``.
    _session_work_dir = _rt_setup._session_work_dir
    _refuse_if_loader_unreachable = _rt_setup._refuse_if_loader_unreachable
    _kas_custom_agents = _rt_setup._kas_custom_agents
    _mount_member_panel = _rt_setup._mount_member_panel
    _session_start_budget = _rt_setup._session_start_budget
    _mirrored_session_mcp = _rt_setup._mirrored_session_mcp
    _mirrored_spec_check_needed = _rt_setup._mirrored_spec_check_needed
    _member_projection_needs_confirmation = _rt_setup._member_projection_needs_confirmation
    _require_unchanged_mirrored_spec = _rt_setup._require_unchanged_mirrored_spec
    _confirm_member_projection = _rt_setup._confirm_member_projection
    _own_stub_session = _rt_setup._own_stub_session
    _unpooled_control_planes = _rt_setup._unpooled_control_planes
    _copy_native_launch_sources = _rt_setup._copy_native_launch_sources
    _guard_unresolved_mcp_refs = _rt_setup._guard_unresolved_mcp_refs
    _start_mount_check = _rt_setup._start_mount_check
    # ``session/new`` end to end: ``kiro_crew.acp.runtime_session_create``.
    create_session = _rt_create.create_session
    _collect_late_start = _rt_create._collect_late_start
    _teardown_late_session = _rt_create._teardown_late_session
    start_collectors = _rt_create.start_collectors
    _finish_create_session = _rt_create._finish_create_session
    probe_advertised_models = _rt_create.probe_advertised_models
    # ``session/load`` end to end: ``kiro_crew.acp.runtime_session_load``.
    load_session = _rt_load.load_session
    _wait_managed_mcp = _rt_load._wait_managed_mcp

    def __init__(
        self,
        work_dir: str | Path | None = None,
        agent: str = CLIENT_NAME,
        sandbox_mode: str = "auto",
        extra_env: dict[str, str] | None = None,
        mcp_gateway_overlay: str | Path | None = None,
        mcp_gateway_socket: str | Path | None = None,
        max_age_secs: float = _DEFAULT_MAX_AGE_SECS,
        max_rss_mb: float = _DEFAULT_MAX_RSS_MB,
        model: str | None = None,
        expect_mcp_reports: bool = True,
        acp_backend: str = ACP_BACKEND_KIRO,
        crew_agent: str = "",
        member_context: bool = False,
        memory_mode: str = "persistent",
        tool_search: ToolSearchSettings | None = None,
        shared_scratch: Path | None = None,
    ):
        if work_dir:
            self._work_dir = Path(work_dir)
        else:
            # config.paths is a stdlib-only leaf: importing it here can't
            # re-enter the config.loader -> providers.acp -> acp.client cycle.
            from kiro_crew.config.paths import config_dir

            self._work_dir = config_dir() / "workspace"
        self._agent = agent
        # Canonical Kiro Crew agent identity (a cfg.agents key) resolved by the
        # surface that created this runtime — a DIFFERENT namespace from
        # ``agent`` (the kiro template the process spawns with). Default for
        # sessions created on this runtime; a warm-pool rekey overwrites it so
        # later sessions inherit the claiming crew, not the pool's spawn state.
        self._crew_agent = crew_agent
        self._acp_backend = acp_backend
        # The operator's MCP Tool Search choice, for hosts that take it over the
        # wire (``client_meta_settings``). None leaves the handshake as the harness
        # declares it. ``_tool_search_wire`` is what was actually sent, kept so a
        # later session on this process can be judged against the process-wide
        # setting it inherits (see create_session).
        self._tool_search = tool_search
        self._tool_search_wire: dict[str, Any] = {}
        # Resolved on FIRST USE, never here: ``ACP_BACKENDS_KNOWN`` admits
        # backends the shared-process runtime has no harness for, and provider
        # safety constructs a runtime for every one of them to prove the reader
        # loop survives a recorder fault. Resolving in __init__ would make those
        # constructions raise, so the failure is deferred to the first seam that
        # actually needs a host's answer -- a runtime that never spawns and never
        # starts a session never needs one.
        self._harness_resolved: HarnessAdapter | None = None
        # Whether THIS process was spawned with Crew as the engine's auth owner
        # (relay started without ``--auth-method cli`` because the Crew vault
        # held an identity at spawn). Decided once in _resolve_spawn_plan and
        # read by the reader loop: a credential callback is answered from the
        # vault only on a process that was spawned expecting it.
        self._kas_host_auth = False
        # First answered credential callback per runtime is logged at INFO as a
        # positive "the engine is drawing its credential from Crew" signal; later
        # ones (the engine refreshes ahead of expiry) drop to DEBUG.
        self._kas_host_auth_logged = False
        # The one MCP sign-in in flight on this process, as (session_id,
        # server_name). The engine's consent-URL request names neither, so at
        # most one sign-in runs at a time and its URL goes to the session that
        # started it -- see begin_mcp_sign_in.
        self._mcp_sign_in: tuple[str, str] | None = None
        # MCP re-attempts in flight on this process, as (session_id,
        # server_name): a reset without startOAuth for a server that failed
        # for want of a credential -- see begin_mcp_reattempt.
        self._mcp_reattempts: set[tuple[str, str]] = set()
        if model is not None:
            if not MODEL_ID_RE.match(model):
                raise ValueError(
                    f"Invalid model identifier: {model!r} — must match "
                    f"^[a-zA-Z0-9][a-zA-Z0-9._-]{{0,127}}$"
                )
        self._model = model
        self._sandbox_mode = sandbox_mode
        self._member_context = member_context
        if memory_mode not in {"persistent", "incognito", "temporary"}:
            raise ValueError("Invalid session memory mode")
        self.recording_allowed = memory_mode == "persistent"
        self._native_launch_sources: dict[str, str] = {}
        # Set only by the non-member launch record below: the projected view
        # alias the record was taken from, and the view's declared resources,
        # whose on-disk total bounds every read of the record.
        self._native_launch_view_alias: str | None = None
        self._native_launch_resources: list[Any] | None = None
        self._extra_env = extra_env or {}
        self._mcp_gateway_overlay = str(mcp_gateway_overlay) if mcp_gateway_overlay else None
        self._mcp_gateway_socket = str(mcp_gateway_socket) if mcp_gateway_socket else None
        # Whether sessions on this runtime should hold drain_init() open for
        # slow MCP servers (the no-report ceiling). A runtime whose agent is
        # KNOWN to have zero MCP servers — the kirocrew-lite background runtime,
        # whose config Kiro Crew itself writes with an empty mcpServers map —
        # opts out so hot one-liner paths (chat titles, suggestions, STT
        # endpointing) don't pay a full ceiling wait that can never be armed.
        self._expect_mcp_reports = expect_mcp_reports
        self._sandbox_cleanup: str | None = None
        self._bound_workspace_fd: int | None = None
        self._spawn_work_dir = str(self._work_dir)
        # The session tree's work directory when this runtime is not the tree's
        # first process (a companion runtime spawned for a parent's subagents,
        # or the successor of a recycled ``_bg`` runtime). Re-validated at spawn
        # (``agent_scratch.shared_scratch_window``); ``None`` means this process
        # starts a tree and its own directory is the work directory. Once live,
        # the process adds itself to the tree's owner marker beside every other
        # live user (``agent_scratch.adopt_owner``), so the sweep keeps the tree
        # while any of them runs.
        self._shared_scratch: Path | None = Path(shared_scratch) if shared_scratch else None
        self._scratch_dir: Path | None = None
        # What the pre-spawn freshness check verified, for the post-handshake half of
        # the bracket. ``None`` until a spawn takes it, and ``None`` for every agent
        # that mirrors no other spec.
        self._derived_spec_snapshot: Any = None

        # Recycling thresholds — see _is_stale(). Long-lived multiplexed
        # runtimes (e.g. the kirocrew-lite background runtime) have no
        # per-turn compaction, so age/RSS are the only signals available to
        # bound unbounded growth.
        #
        # The operator's values, held as the INPUT to the host's own policy: a
        # spawn passes them through ``harness.reclaim_policy`` so a host known to
        # leak faster can narrow them with no branch here, and a host that does
        # not narrow leaves the operator's configuration as the whole answer.
        # Applied at spawn rather than here because a runtime is constructible for
        # a backend the shared-process runtime has no harness for, and because
        # neither threshold means anything before a process exists.
        self._max_age_secs = max_age_secs
        self._max_rss_mb = max_rss_mb
        # Which processes the ceiling above is measured over. None = the whole
        # descendant subtree, which is every kiro-family host. The spawn plan carries
        # a bounded host's depth relative to the exact pid Crew launches.
        self._max_rss_depth: int | None = None

        # session/new + session/load budget — resolved lazily on first use
        # (never in __init__: KiroCrewConfig.load() is a synchronous disk
        # read + schema validation on a cache miss, and runtimes are
        # constructed on the event loop) and cached for the runtime's
        # lifetime. See _session_start_budget().
        self._session_start_timeout: float | None = None

        # Process state
        self._process: asyncio.subprocess.Process | None = None
        self._pid: int | None = None
        # The root's process-start identity, read once at spawn. It is what lets
        # an OBSERVED death retire the registry lines that name this process and
        # no other: the number alone can be recycled before the write lands.
        self._spawn_start_token: str | None = None
        self._start_time: str | None = None
        self._spawn_monotonic: float | None = None
        # pid -> (start_id, basename): the record shape session_pid verifies a
        # descendant's identity against before it signals one, so a recycled pid
        # is skipped. Written by _snapshot_descendants at spawn and on every
        # session start -- nothing populated it before, which left
        # _provider_descendant_records with only its live walk, and a walk can
        # run only while the root is alive.
        self._child_pids: dict[int, ChildRecord] = {}
        # Names THIS spawn of the shared child process (fresh per spawn, cleared
        # with the process) — the identity a resource minted by the child is
        # compared against later. See AcpClient.process_instance for why the
        # session id cannot serve: a resume reuses it on a new process.
        self._process_instance: str = ""
        # The scope unit name systemd ACTUALLY received for this spawn, empty when
        # no scope was created. Held separately from the token because the token
        # alone cannot answer that question: name_scope_unit degrades to a no-op on
        # a host without cgroup delegation, so deriving the name from the token at
        # log time would announce a scope that does not exist. Set from the argv
        # the wrap returned, and cleared with the process.
        self._scope_unit: str = ""

        # Single reader task — the ONLY coroutine that reads stdout
        self._reader_task: asyncio.Task | None = None  # type: ignore[type-arg]
        self._stderr_task: asyncio.Task | None = None  # type: ignore[type-arg]
        # Watches the root process's OWN exit, independent of the stdout stream.
        # The reader loop observes a death only as EOF on stdout, which does not
        # arrive while a descendant holds the root's inherited write end, so a
        # dead-but-unreaped root is invisible to its waiters for the life of the
        # survivor. This task fails them on the exit itself.
        self._exit_watch_task: asyncio.Task | None = None  # type: ignore[type-arg]

        # Demux routing
        self._pending_requests: _PendingRequests = _PendingRequests()
        # Method and send time of each awaited request in _ONE_AT_A_TIME_METHODS.
        # An id stays listed only while _pending_requests still holds it, so a
        # timed-out start that a collector adopted still counts.
        self._one_at_a_time_sent: dict[int, tuple[str, float]] = {}
        # session/new requests whose caller timed out but whose answer is still
        # owned (RFC §4.4); keyed by request id, settled by the collector.
        self._start_collectors: dict[int, StartCollector] = {}
        # Cleanup deadline for those collectors; resolved off-loop like the
        # session-start budget and cached for the runtime's lifetime.
        self._start_collect_timeout: float | None = None
        # Maps req_id → sessionId for responses that should be routed to a session queue
        # (e.g. session/prompt response signals turn completion and must reach the session)
        self._routed_requests: dict[int, str] = {}
        self._session_queues: dict[str, asyncio.Queue[JsonRpcMessage | None]] = {}
        # sessionId -> the ``model`` kiro-cli's last ``_kiro.dev/agent/switched``
        # named for it. ``session/set_mode`` answers ``{}``; this notification,
        # sent before that answer, is the only report of the model the activated
        # agent's spec put the session on. Read by ``_served_default_after_mode``.
        self._switched_models: dict[str, str] = {}
        # OAuth notifications can precede the session/new or session/load
        # response that reveals which queue to register. Stage only those
        # frames while an init is active, then transfer the matching session's
        # frames into its queue. The bounded buffer is cleared when the last
        # concurrent init finishes so an abandoned URL cannot reach a later
        # session that happens to reuse the same id. A start whose caller timed
        # out keeps its own copy on its StartCollector instead, so this deque
        # never outlives the init scope whose progress it describes (see
        # _stage_init_frame).
        self._session_inits_in_flight = 0
        self._pending_init_notifications: deque[JsonRpcMessage] = deque(
            maxlen=_INIT_NOTIFICATION_BUFFER_LIMIT
        )
        self._next_id = 1
        self._initialized = False
        # Whether kiro-cli advertised session/load support in its initialize
        # response. Mirrors AcpClient._can_load_session — load_session() guards
        # on it so we never issue session/load against a backend that lacks it.
        self._can_load_session = False
        # promptCapabilities from the initialize response (e.g. {"image": true}).
        # Empty until the handshake completes, so callers fail CLOSED and send
        # text-only rather than guessing a modality the agent never advertised.
        self._prompt_capabilities: dict = {}
        # The whole ``agentCapabilities`` object from the handshake. Retained
        # because a host's session-level MCP array has to be narrowed against
        # what it actually advertised: sending an element whose transport it
        # never declared can make it refuse the entire session/new rather than
        # skip that one server. Empty before the handshake, and empty on a host
        # that advertises nothing -- which the harness must read as "nothing is
        # known", never as "nothing is supported".
        self._agent_capabilities: dict = {}
        # agentInfo.version from the initialize response: the version of the
        # binary THIS process is executing, which can differ from the one on
        # disk after an in-place upgrade. Empty until the handshake completes.
        self._agent_version = ""
        # Entitlement probe state (probe_advertised_models): single-flight lock
        # plus a short-TTL cache of the last non-empty answer.
        self._entitlement_probe_lock = asyncio.Lock()
        # One descendant pass at a time. Each pass reads the tree and rewrites
        # this root's block from what it read, so two overlapping passes race:
        # the one that finishes last wins, and if that is the OLDER read, the
        # newer pass's descendants are dropped from both the record and the file.
        # Sessions start concurrently on a shared runtime, so this is reachable.
        self._descendant_scan_lock = asyncio.Lock()
        # Two independent clocks so a failure never extends the life of an old
        # success: ``_result_at`` is stamped ONLY when a non-empty result is
        # stored (that result may be replayed until it expires), while
        # ``_attempt_at`` is stamped on EVERY completed attempt incl.
        # failure/empty (it suppresses re-opening a session within the TTL but
        # never replays a stale result).
        self._entitlement_probe_result_at = 0.0
        self._entitlement_probe_attempt_at = 0.0
        self._entitlement_probe_result: list[dict[str, str]] = []
        self._dead = False
        # Set only after kill confirms the root exited and no tracked
        # descendant survived. Consumers use it to decide whether a work
        # directory is safe to reclaim.
        self._process_tree_confirmed_dead = False
        self._death_summary: str | None = None
        # The composed summary's parts, so the post-reap amendment rebuilds the
        # line instead of editing its text -- a tail carrying this format's own
        # shape must never be mistaken for the status field.
        self._death_reason = ""
        self._death_label = ""
        self._death_tail = ""
        # Severity _mark_dead settled on, after its refuse-the-downgrade
        # guard. The post-reap amendment logs at the SAME severity, so an
        # operator filtering on WARNING never sees the death without the
        # exit code that followed it.
        self._death_expected = False
        # Set by ``_mark_dead(stdin_stalled=True)``: the runtime was condemned
        # because the child stopped reading stdin, with the child still alive.
        # Any prompt frame already in that pipe may yet be read, so every
        # session with an unanswered prompt reports its death as an ambiguous
        # delivery (``AcpSessionHandle.prompt_outstanding_on_stall``).
        self._stdin_stall_death = False
        # The sessions whose turn was active when that stall condemned the
        # runtime, frozen at ``_mark_dead`` before any turn's teardown runs.
        self._stall_turn_sessions: frozenset[str] = frozenset()
        self._last_activity: float = 0.0
        self._stderr_lines: list[str] = []
        # Latched auth-failure observation. ``_stderr_lines`` is a 20-line ring,
        # so on a noisy startup the auth line can be evicted before anything asks
        # about it — and the question is only ever asked LATER, once a request
        # times out or the runtime dies. Re-scanning a buffer that no longer holds
        # the evidence answers "no auth problem", which is indistinguishable from
        # a real negative. Latch on arrival instead: an auth failure observed once
        # stays observed for the life of this runtime, which is correct because
        # nothing about a rejected credential un-rejects itself mid-process.
        self._saw_auth_failure = False
        # Latched sandbox-init-refusal observation, latched for the same reason
        # and at the same sink as the auth latch above: the question ("why is this
        # runtime dead?") is asked after the ring has already turned over on a
        # chatty startup, and a re-scan that misses the line answers "not a
        # sandbox problem" indistinguishably from a real negative.
        #
        # Its LIFETIME is narrower than the auth latch's, though, and deliberately:
        # this one is a verdict about THIS CHILD'S STARTUP, and it is spent once
        # startup has demonstrably SUCCEEDED -- which is the first session handle,
        # not the ``initialize`` handshake. Startup continues past the handshake
        # through ``session/new``, and a sandboxed MCP launcher that the child
        # starts for that session can refuse there: closing the window at the
        # handshake would leave every such refusal unclassified, on the very
        # translation site (``create_session``) added to catch it.
        #
        # See the clear in ``_finish_create_session`` and the arming guard in
        # ``_drain_stderr``.
        self._saw_sandbox_init_failure = False
        # Whether a session handle has ever been produced on this runtime. The
        # startup window the latch above arms in, and the reason it is a separate
        # flag from ``_initialized``: the handshake is the middle of startup, not
        # its end.
        self._first_session_ready = False
        # Which isolation layer wrapped this runtime's child, recorded by
        # ``_spawn_admitted`` off the argv its wrap returned. False until then --
        # also the safe default for the classifier, since a spawn that never
        # reached the wrap cannot have been refused by it.
        self._sandbox_wrapped_by_crew = False
        # The mask set this spawn asked for, so a trusted corroboration run can
        # exercise the same mounts rather than a weaker profile.
        self._sandbox_hidden_dirs: tuple[str, ...] = ()
        # Unroutable-frame drop accounting: (sessionId, method) → count since
        # the last flush, plus the monotonic timestamp of that flush (0.0 = no
        # window open yet; the first counted drop opens it). Written ONLY from
        # _reader_loop (the single stdout owner) and its flush helper, so a plain
        # dict needs no lock — asyncio.ensure_future(self._reader_loop()) is
        # called exactly once, in spawn(), and never re-entered.
        self._dropped_frames: dict[tuple[str, str], int] = {}
        # In-flight SEL audit tasks for auto-rejected permission requests;
        # held only to keep them alive (see _answer_unroutable_permission).
        self._audit_tasks: set[asyncio.Task] = set()
        # In-flight ANSWER tasks (the coroutines that write the rejection
        # response), tracked separately from the SEL audit tasks above: the
        # flood cap below must count only tasks that can block on stdin
        # drain() — audit tasks are short-lived thread offloads, and letting
        # them satisfy the cap would let a burst of ordinary audits trip a
        # false mark_dead that kills every multiplexed session.
        self._answer_tasks: set[asyncio.Task] = set()
        # Volume bound for in-flight auto-answer tasks. Each task can block
        # on stdin drain() against a backend that floods permission frames
        # while never reading its stdin — unbounded, that grows the task set
        # until the gateway OOMs. Awaiting an answer inline on the reader
        # would hand the same hostile backend a demux freeze for every
        # session, so the bound treats a capacity timeout as a dead pipe
        # (see _wait_for_answer_capacity).
        self._max_answer_tasks: int = 128
        # Bounded discrimination wait at the cap (see
        # _wait_for_answer_capacity):
        # small enough that a wedged pipe is condemned promptly, large enough
        # that a responsive backend's in-flight answers can complete.
        self._answer_cap_wait_secs: float = 5.0
        # Backend-internal subagent session ids, snapshotted from each
        # `_kiro.dev/subagent/list_update` frame (a FULL list every time, so
        # replacement — not accumulation — keeps it bounded and current).
        # Membership proves an unregistered sessionId is a real backend child.
        # `_subagent_owner` records WHICH registered session was the sole
        # consumer when the announce arrived — routing later requires the sole
        # queue to still be that exact session, so a warm-reused runtime whose
        # session was swapped can never inherit a stale child's approvals.
        # Both are cleared when the owning session unregisters.
        self._subagent_sessions: set[str] = set()
        self._subagent_owner: str | None = None
        # How many ids the LAST roster named past NATIVE_CHILD_ROSTER_CAP.
        # Non-zero means an announced child may be unrecognisable here, so a
        # permission request for an unknown session is audited under
        # _ROSTER_OVERFLOW_REJECT_REASON instead of being called unregistered.
        self._subagent_roster_overflow: int = 0
        # Roster-truncation LOG accounting, the same shape _note_dropped_frame
        # uses: the first truncated snapshot of an episode warns, every later
        # one inside that episode is tallied here and folded into a throttled
        # DEBUG summary. `_roster_overflow_repeats` counts those later
        # snapshots, `_roster_overflow_peak` is the largest tail any of them
        # named, and `_roster_overflow_summary_at` is the monotonic timestamp
        # of the last summary — 0.0 means no episode is open, so the next
        # truncation is loud again. Written only from the demux loop's snapshot
        # handler and from unregister_session, so no lock is needed.
        self._roster_overflow_repeats: int = 0
        self._roster_overflow_peak: int = 0
        self._roster_overflow_summary_at: float = 0.0
        # Sessions with an ACTIVELY CONSUMING prompt dispatch loop, marked by
        # AcpSessionHandle.prompt() around its dispatch (all exit paths,
        # including timeout/cancel/synthetic completion, unmark in a finally).
        # _routed_requests is NOT usable for this: it also holds set_mode /
        # steer / config request ids, and a timed-out prompt leaves its entry
        # until the backend response arrives — either would make routing
        # believe a consumer exists and park a child request unread.
        self._turn_active_sessions: set[str] = set()
        self._dropped_frames_flushed_at: float = 0.0

    @property
    def pid(self) -> int | None:
        return self._pid

    @property
    def spawn_monotonic(self) -> float | None:
        """Monotonic time this process was spawned, or ``None`` before spawn."""
        return self._spawn_monotonic

    @property
    def entitlement_probe_result_at(self) -> float:
        """Monotonic time the stored probe answer arrived (0.0 before any).

        :meth:`probe_advertised_models` serves either a fresh answer or a replay
        of this stored one; in both cases the answer is dated by this clock, so a
        caller that stores what it was served dates its snapshot from here rather
        than from its own call time, which for a replay would be LATER than the
        data and would raise its own freshness floor above it.
        """
        return self._entitlement_probe_result_at

    @property
    def work_scratch_dir(self) -> Path | None:
        """The session tree's work directory this process exposes as ``$KIROCREW_SCRATCH``.

        The inherited directory when this runtime joined an existing tree, else
        its own allocation; ``None`` before spawn or when allocation failed. This
        is what a spawn made ON BEHALF of a session on this runtime -- a
        companion runtime, a dedicated subagent process, a recycled successor --
        is handed as its ``shared_scratch``, so the whole tree keeps one work
        directory however many processes it spans.
        """
        return self._shared_scratch or self._scratch_dir

    @property
    def process_instance(self) -> str:
        """Identity of the CURRENT child process instance (``""`` when none).

        Fresh per spawn, so equality distinguishes the process that minted a
        resource from any successor — including one that resumed the same ACP
        session id. Liveness is asked separately (:meth:`is_alive`).
        """
        return self._process_instance if self._process is not None else ""

    @property
    def acp_backend(self) -> str:
        """Which ACP backend this runtime's process speaks.

        Public because the backend has to survive being read back off a
        started provider: the runtime is the only object that still knows it
        once ``AcpProvider`` swaps its placeholder client for a session
        provider.
        """
        return self._acp_backend

    @property
    def _harness(self) -> HarnessAdapter:
        """This backend's strategy object -- the runtime's only per-host answer.

        Every per-host question is asked here, so a host added later answers all
        of them in one file rather than in scattered arms nothing points at.

        ``harness_for`` raises ``ValueError`` for a backend with no harness, and
        that is the intended outcome: silently inheriting kiro-cli's argv,
        protocol version and teardown verb would start a session that then
        behaves wrongly, which is far harder to attribute than a refusal here.
        """
        # ``getattr`` for the same reason the projection path uses it: a caller
        # that only needs the agent projection constructs a bare runtime with
        # ``object.__new__`` and sets the two fields it cares about, so the cache
        # slot may not exist. Same lazy resolution either way.
        harness = getattr(self, "_harness_resolved", None)
        if harness is None:
            harness = harness_for(self._acp_backend)
            self._harness_resolved = harness
        return harness

    @property
    def uses_kiro_identity_store(self) -> bool:
        """True when this runtime's process signs in from kiro-cli's own store.

        Membership in ``backends_retired_by_host_logout()`` (harness-parity
        H5/H14). ``AcpRuntime`` is not an ``LLMProvider``, but the identity-change
        sweep reaches shared runtimes as well as session providers, so it
        declares the same capability under the same name -- letting that sweep
        ask both families one question instead of probing private attributes.
        """
        return self._acp_backend in backends_retired_by_host_logout()

    @property
    def supports_image_prompt(self) -> bool:
        """True when the agent advertised ``promptCapabilities.image``.

        Fails closed: an un-handshaked or silent backend reports False, so the
        prompt path sends text only instead of an image block the agent may
        reject.
        """
        return bool(self._prompt_capabilities.get("image", False))

    @property
    def agent_version(self) -> str:
        """``agentInfo.version`` the agent reported at ``initialize`` (``""`` until then).

        This is the version of the binary the process is RUNNING, which is what
        a capability decision about a live session must key on: after an
        in-place kiro-cli upgrade the file on disk is newer than every process
        spawned before it.
        """
        return self._agent_version

    def is_alive(self) -> bool:
        """True if the underlying process exists and has not exited."""
        return self._process is not None and self._process.returncode is None and not self._dead

    @property
    def stdin_stall_death(self) -> bool:
        """Whether this runtime died of a stdin stall, its child left alive."""
        return self._stdin_stall_death

    def stdin_reader_may_live(self) -> bool:
        """Whether a process that can still read this runtime's stdin may run.

        Blocking (a process-group scan), so call it off the loop. The pipe is
        inherited inside the root's process group -- the root may be a sandbox
        launcher whose child reads it -- while stdio MCP servers lead their own
        groups, so the answer is the root (proven by identity, which holds
        whether or not this handle still references it) or any live member of
        its group. Conservative on an unreadable group: True.
        """
        pid = self._pid  # pid-owner-ok: the runtime reads its own root
        if pid is None:
            return False
        if self._root_identity() == "holds":
            return True
        if platform_compat.IS_WINDOWS:
            return False
        return _pgroup_has_member_besides(pid, pid)

    def turn_active_at_stall(self, session_id: str) -> bool:
        """Whether *session_id* had a turn running when a stdin stall killed this runtime.

        Frozen at the death, so a reader that runs after the turn's own teardown
        (the provider's exception translation) still gets the answer.
        """
        return session_id in self._stall_turn_sessions

    @property
    def process_tree_confirmed_dead(self) -> bool:
        """Whether teardown confirmed the root and every tracked child exited."""
        return self._process_tree_confirmed_dead is True

    def death_summary(self) -> str | None:
        """One-line death attribution, or None while alive.

        Composed once by ``_mark_dead`` (reason + returncode + stderr tail).
        Lets a consumer that only observes the death through a poisoned
        session queue — e.g. a turn's frame wait — report WHO/WHY instead
        of a bare "process died".
        """
        return self._death_summary

    def _stale_by_age(self) -> bool:
        """True if uptime exceeds max_age_secs. Cheap, no I/O — safe to call
        under a lock. Does NOT consider RSS (see _is_stale for that).

        NOT a recycle predicate: RSS, not age, is the growth mode this class
        was observed failing on, so a reuse decision MUST ask _is_stale().
        Reaching for this one because it is cheaper is what left the shared
        background runtime unbounded. No production caller today.
        """
        if self._pid is None or self._spawn_monotonic is None:
            return False
        return (time.monotonic() - self._spawn_monotonic) > self._max_age_secs

    async def _is_stale(self) -> str | None:
        """Return the recycle reason ('age' or 'rss'), or None if not stale.

        Distinct from is_alive(): a runtime can be perfectly healthy (process
        running, protocol responsive) yet still be "stale" — e.g. the
        kirocrew-lite background runtime observed growing unbounded (multi-GB
        RSS) over ~24h of uptime because the multiplexed design has no per-turn
        compaction or lifetime cap. Callers should check this alongside
        is_alive() and stop reusing a stale process: kill() and respawn when the
        active session count is 0, and otherwise DETACH it (park it to drain,
        respawn for new callers, reap on its last unregister) rather than
        deferring. Waiting for an idle window is not a bound — a multiplexed
        runtime under sustained background load never has one, which is how the
        multi-GB growth above went unchecked.

        RSS is measured across the whole descendant tree (_get_rss_tree_mb):
        under the Linux namespace sandbox self._pid is the launcher parent, and
        the real kiro-cli child is what grows. The RSS probe shells out / reads
        /proc, so it is offloaded to subprocess_executor() to keep the event
        loop free.

        The RSS probe is gated behind _RSS_PROBE_MIN_AGE_SECS: a freshly-(re)used
        runtime returns None without any executor round-trip, so the hot reuse
        path in get_bg_session (which holds _bg_runtime_lock) stays CPU-only for
        young runtimes. The lock IS deliberately held across the probe for older
        runtimes, busy or idle; the age gate bounds how often that happens, and a
        runtime that answers "stale" is displaced rather than re-probed.
        """
        if self._pid is None:
            return None

        if self._spawn_monotonic is not None:
            age = time.monotonic() - self._spawn_monotonic
            if age > self._max_age_secs:
                return "age"
            if age < _RSS_PROBE_MIN_AGE_SECS:
                # Too young to have grown — skip the offloaded RSS probe.
                return None

        rss_mb = await asyncio.get_running_loop().run_in_executor(
            subprocess_executor(),
            runtime_process_tree._get_rss_tree_mb,
            self._pid,
            self._max_rss_depth,
        )
        if self._max_rss_depth is not None:
            # A bounded scope rests on a structural fact about the host's process
            # tree (which generation the resident process sits at). Record what the
            # bound captured against the ceiling it is judged by, so a host release
            # that moves that process out of scope is attributable from the log
            # rather than showing up only as a ceiling that never trips.
            logger.debug(
                "rss probe: backend=%r pid=%s depth=%s rss_mb=%s ceiling_mb=%s",
                self.acp_backend,
                self._pid,
                self._max_rss_depth,
                rss_mb,
                self._max_rss_mb,
            )
        if rss_mb is not None and rss_mb > self._max_rss_mb:
            return "rss"

        return None

    def has_active_sessions(self) -> bool:
        """True if any session is currently registered on this runtime.

        Killing a runtime while a co-tenant session is registered drops that
        session's in-flight prompt/response. Every recycle path now asks
        ``has_active_or_initializing_sessions`` instead, which closes the
        registration window this one leaves open; no production caller remains.
        """
        return bool(self._session_queues)

    def has_active_or_initializing_sessions(self) -> bool:
        """True if any session is registered OR still being created.

        ``has_active_sessions`` sees only REGISTERED queues, and
        ``create_session`` registers outside the runtime lock -- so a co-tenant
        whose ``session/new`` is in flight is momentarily invisible to it, and
        killing the runtime under it surfaces as ``AcpRuntimeDead`` on work the
        user never connected to whatever prompted the kill.

        This is therefore the predicate every recycle and displacement decision
        asks, because it also counts ``_session_inits_in_flight``: a runtime with
        an initializing session is treated as busy and parked to drain rather
        than killed, so no caller has to absorb that window with a respawn.
        The init scope opens before ``create_session``'s admission gate, so a
        claim still queued behind the gate already counts.
        """

        return bool(self._session_queues) or self._session_inits_in_flight > 0

    # ── Lifecycle ──

    #: How long a revive waits for the failed child's reply tasks to stop once
    #: cancelled. None of them absorbs a cancellation, so this is only a bound.
    _REVIVE_ANSWER_DRAIN_TIMEOUT = 1.0

    #: How long the failed-start reap waits for SIGKILLed descendants to be
    #: reaped by their new parent before reporting them as survivors.
    _FAILED_START_REAP_WAIT = 1.0

    #: One retry for a descendant scan that came back empty. The spawned root
    #: is a launcher that forks the agent, which forks again, so a scan racing a
    #: cold start can legitimately see nothing. A single retry is enough because
    #: every later session start scans again. Class attribute so tests can zero
    #: it rather than pay it.
    _DESCENDANT_RESCAN_DELAY = 0.5

    #: Why a teardown reached nothing, per ``_root_identity`` verdict. "holds" is
    #: reachable here too: the tree kill ran and the root exited under it, which
    #: is a race, not an identity failure.
    _ROOT_UNREACHED_REASON_BY_VERDICT = {
        "holds": "its root exited between the identity check and the signal",
        "mismatch": "its number is another process's now",
        "unknown": "its identity could not be read",
    }

    # Grace window for SIGTERM before escalating, and the post-SIGKILL reap
    # window. Class attributes so tests can shrink them.
    _KILL_TERM_TIMEOUT = 5.0
    _KILL_REAP_TIMEOUT = 2.0
    # Cadence at which the exit watcher polls process.returncode. returncode is
    # set by _process_exited (the SIGCHLD/waitpid path) the instant the root is
    # reaped, independent of pipe disconnection, so a short poll notices the
    # exit even while a surviving descendant holds stdout open and no EOF or
    # broken-pipe wakes process.wait(). Small enough to keep the fail-fast
    # promise, large enough not to busy-spin the loop.
    _EXIT_WATCH_POLL_INTERVAL = 0.25

    # After the exit is confirmed, give the reader a bounded window to drain any
    # final frame the backend wrote just before exiting -- a completed turn's
    # response sitting in the pipe the reader has not pulled yet. The reader
    # stamps ``_last_activity`` on every frame, so progress is observable: keep
    # yielding while it keeps draining, stop once it has been quiet for one grace
    # slice (nothing left to pull) or the total cap is reached. The cap bounds
    # the fail-fast delay a genuinely-empty pipe adds; the per-slice yield lets
    # the reader run between checks.
    _EXIT_DRAIN_GRACE = 0.05
    _EXIT_DRAIN_MAX = 1.0

    # ── Reader Task (single owner of stdout) ──

    def _snapshot_subagent_sessions(self, params: dict) -> None:
        """Replace the known backend-subagent session-id set from a list_update.

        The frame carries the backend's FULL current subagent list (kiro-cli
        rebuilds it from `orchestrated_sessions` on every change), so replacing
        the set keeps it bounded and self-cleaning: terminated children vanish
        from the next update. Ids are backend-controlled — length-capped and
        type-checked so a hostile payload cannot grow memory unboundedly.

        Membership here is what the routing branch decides a child's approvals
        on, so the entry bound is the SHARED native-child cap
        `NATIVE_CHILD_ROSTER_CAP` — the same number `AcpSessionHandle` remembers
        child ids under — and never a tighter per-frame slice. A tighter bound
        would deny mode parity to children the very same payload made the
        handle count: an id the set does not hold is unrecognisable, its
        `session/update` is a counted drop and its permission request is
        auto-rejected, so a cap below the handle's turns one announced roster
        into two governance classes by list position alone.

        Ids past the cap are counted in `_subagent_roster_overflow`, and the
        truncation is reported through `_note_roster_overflow`: a truncated tail
        otherwise reads exactly like a roster that never named those children.
        The count is per SNAPSHOT, not cumulative — the frame is the full list,
        so replacement makes it idempotent under repeated identical rosters even
        above the cap (the handle's counter accumulates instead, because ids
        reach it one frame at a time and only a per-turn total can describe that
        stream). The LOG is episode-scoped rather than per-snapshot, which is a
        different lifetime and deliberately so: see `_note_roster_overflow`.

        The scan visits every entry even above the cap, on the SHARED demux
        loop. That is a constant factor on work already done: `json.loads` has
        walked the whole list before this method is called, and the frame is
        already bounded by `_STDOUT_BUFFER_LIMIT`. Slicing to save the pass is
        what cost the tail its governance.
        """
        raw = params.get("subagents")
        if not isinstance(raw, list):
            return
        ids: set[str] = set()
        overflow = 0
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            sid = entry.get("sessionId") or entry.get("session_id")
            if not isinstance(sid, str) or not sid or len(sid) > MAX_ACP_SESSION_ID_LEN:
                continue
            if sid in ids:
                continue
            if len(ids) >= NATIVE_CHILD_ROSTER_CAP:
                # Past the cap an id is counted, never stored: recognising a
                # repeat would mean remembering it, which is the one thing the
                # cap refuses.
                overflow += 1
                continue
            ids.add(sid)
        self._subagent_sessions = ids
        self._subagent_roster_overflow = overflow
        if overflow:
            self._note_roster_overflow(overflow)
        else:
            self._end_roster_overflow_episode()
        # Ownership is provable only when exactly one session is registered:
        # the announce demonstrably belongs to it. Otherwise no owner, and
        # routing stays fail-closed.
        self._subagent_owner = (
            next(iter(self._session_queues)) if len(self._session_queues) == 1 else None
        )

    def _note_roster_overflow(self, overflow: int) -> None:
        """Report a truncated roster snapshot: loud ONCE per episode, then counted.

        The first truncation an episode sees is a `WARNING`, because a truncated
        tail is otherwise invisible — nothing else in the log distinguishes it
        from a roster that never named those children, and an operator has to
        see it to decide whether the cap needs raising. What must not recur is
        the per-FRAME repeat: `subagent/list_update` is a backend-controlled
        notification re-broadcast on every child status change, so above the cap
        each rebroadcast re-earns the same warning at the backend's frame rate.
        Measured on this handler: 10 over-cap snapshots → 10 identical WARNING
        records at ~245 message bytes each, whether the frames are byte-identical
        or differ only in a child's `status`. That is the retention hazard
        `_note_dropped_frame` documents above (~60 lines/second rolling incident
        evidence out of `gateway.log`'s 8MB window), one level louder.

        So later truncations inside the episode are tallied and flushed as one
        throttled `DEBUG` summary, at most every
        `_ROSTER_OVERFLOW_SUMMARY_INTERVAL_SECS`, carrying how many snapshots
        repeated and the LARGEST tail they named. The peak, not the latest
        value: sizing the cap reads the worst case, and which value happens to
        be current at an arbitrary flush instant is noise.

        **The reset boundary is the truncation EPISODE** — it re-arms when the
        overflow count returns to 0, which is exactly the two events that
        already retire the snapshot attribution: a later roster that truncated
        nothing, and the owning session unregistering. One lifetime governs both
        halves of the same signal, so the loud line and the auto-reject reason
        can never disagree about whether the cap is under pressure. A plain
        per-interval re-arm is the alternative, and it is what the drop counter
        does, but the drop summary is `DEBUG`: re-arming a WARNING on a timer
        restates it every interval for as long as the steady state lasts, which
        is the volume this throttle exists to remove.

        The residual, stated rather than implied: a tail that GROWS inside one
        episode (40 ids, later 40000) is loud only at its first value, and the
        growth is visible in the summary's peak at `DEBUG`. Re-warning on growth
        would need a second threshold — a second throttle shape in a module that
        already has one.

        Synchronous and awaitless, like the drop counter, because the caller is
        on the shared demux loop.
        """
        now = time.monotonic()
        if self._roster_overflow_summary_at == 0.0:
            # No episode open: this snapshot is the loud one, and it opens the
            # window the repeats below are measured against. __init__ cannot
            # supply that baseline — a runtime may be constructed long before
            # spawn(), and a stale timestamp would make the first repeat flush
            # immediately instead of aggregating.
            self._roster_overflow_summary_at = now
            logger.warning(
                "subagent roster announced %d children past the %d-id recognition "
                "cap; their session/update frames are counted drops and their "
                "permission requests are auto-rejected (reason %s) rather than "
                "reaching the approval pipeline. Further truncated snapshots are "
                "summarized at DEBUG until a roster inside the cap arrives",
                overflow,
                NATIVE_CHILD_ROSTER_CAP,
                _ROSTER_OVERFLOW_REJECT_REASON,
            )
            return
        self._roster_overflow_repeats += 1
        self._roster_overflow_peak = max(self._roster_overflow_peak, overflow)
        if now - self._roster_overflow_summary_at >= _ROSTER_OVERFLOW_SUMMARY_INTERVAL_SECS:
            self._flush_roster_overflow(now)

    def _flush_roster_overflow(self, now: float | None = None) -> None:
        """Emit the repeated-truncation summary and reopen the throttle window."""
        self._roster_overflow_summary_at = time.monotonic() if now is None else now
        if not self._roster_overflow_repeats:
            return
        logger.debug(
            "subagent roster truncated on %d further snapshot(s); largest tail %d "
            "id(s) past the %d-id recognition cap",
            self._roster_overflow_repeats,
            self._roster_overflow_peak,
            NATIVE_CHILD_ROSTER_CAP,
        )
        self._roster_overflow_repeats = 0
        self._roster_overflow_peak = 0

    def _end_roster_overflow_episode(self) -> None:
        """Close a truncation episode: flush the residual count, re-arm the warning.

        Called wherever `_subagent_roster_overflow` returns to 0 — a roster
        inside the cap, or the owning session unregistering. Flushing first is
        what keeps a SHORT episode honest: repeats accrued in under one interval
        would otherwise be dropped on the floor, and then a truncation storm
        that ends quickly would report only its first frame. Re-arming is what
        keeps the NEXT truncation loud; without it the throttle would swallow a
        genuinely new one for the rest of the runtime's life.
        """
        self._flush_roster_overflow()
        self._roster_overflow_summary_at = 0.0

    async def _wait_for_answer_capacity(
        self,
        msg: JsonRpcMessage,
        *,
        request_kind: str,
        session_id: str = "",
        audit_reason: str | None = None,
    ) -> bool:
        """Wait briefly for shared answer capacity or condemn a wedged pipe.

        Server-to-client requests require a response, so overflowing answers
        cannot take the notification counted-drop path. A responsive backend
        may fill the set with already-buffered requests before completed-task
        callbacks run; one completion admits the current request. No
        completion within the bound means writes are wedged, so marking the
        runtime dead resolves every pending wait instead of leaving the remote
        requester unanswered indefinitely.
        """

        def _deny() -> bool:
            """Refuse admission, recording the decision first.

            A refusal that reaches a caller which had already been admitted to
            wait must leave a SEL record, or a permission decision that denied a
            real tool invocation is indistinguishable from one never made.
            """
            if audit_reason is not None:
                self._audit_denied_off_loop(msg, session_id, audit_reason)
            return False

        # Deliberately NOT audited: on an already-dead runtime a flooding
        # backend's frames are gated out here, and auditing each one would grow
        # audit tasks without bound — the very failure the cap prevents.
        if self._dead:
            return False
        if len(self._answer_tasks) < self._max_answer_tasks:
            return True

        done, _pending = await asyncio.wait(
            set(self._answer_tasks),
            timeout=self._answer_cap_wait_secs,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if done:
            # asyncio schedules task callbacks separately from waking waiters.
            # Remove completed entries here so admitting the replacement never
            # transiently exceeds the shared cap; the callbacks remain an
            # idempotent cleanup backstop.
            self._answer_tasks.difference_update(done)
            if self._dead:
                # A concurrent waiter condemned the runtime while this one was
                # parked: capacity freed but admission still fails, so this
                # refusal owes an audit like any other.
                return _deny()
            return True

        logger.error(
            "answer-task cap (%d) reached at %s request id=%s%s and no "
            "in-flight answer completed in %gs — backend is flooding frames "
            "while not reading stdin; marking runtime dead so every pending "
            "wait resolves",
            self._max_answer_tasks,
            request_kind,
            _loggable_request_id(msg.id),
            f" for session {_loggable_request_id(session_id)}" if session_id else "",
            self._answer_cap_wait_secs,
        )
        # Audit before condemning the runtime, so the record for this decision
        # cannot race the wait-resolution _mark_dead triggers.
        refusal = _deny()
        self._mark_dead(
            f"{request_kind}-answer task cap reached (backend not reading)", stdin_stalled=True
        )
        return refusal

    async def _spawn_answer_task(
        self,
        msg: JsonRpcMessage,
        session_id: str,
        *,
        reason: str = _UNREGISTERED_REJECT_REASON,
    ) -> None:
        """Spawn a bounded off-loop auto-answer for an unroutable permission request.

        Off-loop because the answer's ``send_response`` can block on stdin
        ``drain()`` against a backend that is not reading — awaiting inline
        would freeze the shared reader (every session's demux) on one hostile
        or wedged backend. Bounded because each blocked task is retained in
        ``_answer_tasks``: a backend that floods permission frames while never
        reading stdin would otherwise grow that set until the gateway OOMs.
        At capacity the shared admission wait either observes progress or
        marks the runtime dead so the requester cannot remain unanswered.
        """
        if not await self._wait_for_answer_capacity(
            msg,
            request_kind="permission",
            session_id=session_id,
            audit_reason="answer_task_cap_runtime_dead",
        ):
            return
        _t = asyncio.ensure_future(
            self._answer_unroutable_permission(msg, session_id, reason=reason)
        )
        self._answer_tasks.add(_t)
        _t.add_done_callback(self._answer_tasks.discard)

    async def _answer_unroutable_permission(
        self,
        msg: JsonRpcMessage,
        session_id: str,
        *,
        reason: str = _UNREGISTERED_REJECT_REASON,
    ) -> None:
        """Answer a permission REQUEST for a session with no registered queue.

        The ACP contract for a server→client request is that the client always
        replies; kiro-cli's own TUI answers even unowned-session permission
        requests (``cancelled``) rather than dropping them. Auto-reject is
        deliberate and conservative: never auto-approve here — no policy engine
        has seen this tool call, and an approve would grant an invisible
        escalation. Per-frame WARNING is safe (unlike the drop counter's flood
        case): each request corresponds to one pending tool approval and the
        backend cannot re-emit it without a new turn.
        """
        params = msg.params if isinstance(msg.params, dict) else {}
        option_id = _reject_option_id(params)
        if option_id is not None:
            result = {"outcome": {"outcome": "selected", "optionId": option_id}}
        else:
            result = {"outcome": {"outcome": "cancelled"}}
        tool_call = params.get("toolCall")
        raw_title = tool_call.get("title") if isinstance(tool_call, dict) else None
        # The title is backend/LLM-authored and may embed a credential-bearing
        # command line — redact BEFORE truncating (truncation first could clip
        # a secret mid-token so the redaction patterns no longer match, leaking
        # a credential prefix into the logs).
        # Bound the redaction input (backend-controlled) BEFORE the regex
        # passes, generously above the display cap so a clipped secret
        # cannot straddle the boundary the display truncation makes.
        title = redact_text(str(raw_title)[:4096])[:120] if raw_title else "<unknown>"
        logger.warning(
            "auto-rejected permission request id=%s for session %s "
            "(tool: %s, reason: %s): no surface on this client can answer it "
            "right now; answering with %s so the backend subagent gets a tool "
            "error instead of hanging",
            _loggable_request_id(msg.id),
            _loggable_request_id(session_id),
            title,
            reason,
            result["outcome"]["outcome"],
        )
        try:
            # Bounded send: an answer that cannot be written within the
            # timeout means the backend is not reading its stdin at all —
            # the pipe is wedged, and every further frame from it would
            # stack another blocked task (the OOM vector). Marking the
            # runtime dead resolves EVERY pending wait by teardown, so no
            # request is left unanswered and nothing accumulates. This is
            # the outer guard; send_response's own drain bound fires first
            # on a paused writer and surfaces as AcpRuntimeDead below.
            await asyncio.wait_for(self.send_response(msg.id, result), timeout=30.0)
        except asyncio.TimeoutError:
            logger.error(
                "answer for permission request id=%s could not be written in "
                "30s — backend not reading stdin; marking runtime dead",
                _loggable_request_id(msg.id),
            )
            # Audit BEFORE returning: the denial DECISION was made even
            # though delivery failed — mandatory SEL coverage applies to
            # every decision, not just successfully delivered ones.
            self._audit_denied_off_loop(
                msg, session_id, f"{reason}:send_stalled_runtime_dead", title=title
            )
            self._mark_dead(
                "permission-answer write stalled (backend not reading)", stdin_stalled=True
            )
            return
        except AcpRuntimeStdinStalled:
            # send_response's own no-progress bound fired ahead of the outer
            # guard and already marked the runtime dead; same disposition, same
            # audit reason, so the record does not depend on which bound won.
            self._audit_denied_off_loop(
                msg, session_id, f"{reason}:send_stalled_runtime_dead", title=title
            )
            return
        except AcpRuntimeDead:
            # Runtime died mid-answer; the backend's wait dies with it.
            self._audit_denied_off_loop(
                msg, session_id, f"{reason}:runtime_dead_mid_answer", title=title
            )
            return
        except Exception:
            # This coroutine runs as a RETAINED TASK off the reader loop, so
            # an unexpected send failure would otherwise be swallowed with
            # the task — the child never gets an answer and waits on a
            # stranded oneshot, the exact hang this path exists to prevent.
            # A response write that fails for any reason other than the
            # already-handled dead-runtime case means the pipe cannot be
            # trusted: log and mark the runtime dead so the child's wait
            # dies with the process instead of hanging invisibly.
            logger.exception(
                "failed to answer unroutable permission request id=%s — "
                "marking runtime dead so the requester cannot hang",
                _loggable_request_id(msg.id),
            )
            self._audit_denied_off_loop(
                msg, session_id, f"{reason}:send_failed_runtime_dead", title=title
            )
            self._mark_dead("unroutable-permission answer failed")
            return
        # Every permission decision is SEL-audited (repo convention; see
        # _audit_denied_off_loop for the off-loop/lazy-import rationale).
        self._audit_denied_off_loop(msg, session_id, reason, title=title)

    def _audit_denied_off_loop(
        self,
        msg: JsonRpcMessage,
        session_id: str,
        reason: str,
        *,
        title: str | None = None,
    ) -> None:
        """SEL-audit a denied permission decision without blocking the caller.

        Every permission decision leaves a SEL record (repo convention; the
        dashboard deny path does the same). Off the calling task because
        ``sel()`` may do blocking filesystem work on first use (e.g. Windows
        ACLs). The decision is already made, so an audit failure must not
        undo or delay it; the failure is swallowed after logging. Lazy
        import: a module-level import of ``kiro_crew.sel`` would be circular
        (same pattern as sandbox.py).
        """
        if title is None:
            _params = msg.params if isinstance(msg.params, dict) else {}
            _tc = _params.get("toolCall")
            _raw = _tc.get("title") if isinstance(_tc, dict) else None
            title = redact_text(str(_raw)[:4096])[:120] if _raw else "<unknown>"
        # Hang-resilience series: every runtime-side denial funnels through
        # here, so one emit covers unroutable/between-turns/cap/send-failure
        # denials. ``reason`` is the closed SEL enum (low-cardinality).
        emit_counter(
            CHILD_PERMISSION_DENIED,
            {"surface": "runtime", "reason": reason},
        )
        request_id = msg.id if isinstance(msg.id, (str, int)) else ""
        # SNAPSHOT the attribution key NOW: the audit closure runs later on a
        # worker thread, and `_subagent_owner` is mutable (unregister/session
        # swap). Reading it at execution time would write the wrong owner —
        # or the bare PID — into an immutable SEL row.
        session_key = f"acp:{self._subagent_owner or self._pid}:{session_id}"

        def _audit() -> None:
            try:
                from kiro_crew.sel import sel

                sel().log_tool_invocation(
                    session_key=session_key,
                    agent="kirocrew",
                    source="acp_runtime",
                    tool_name=title,
                    outcome="denied",
                    request_id=request_id,
                    error=reason,
                )
            except Exception:
                logger.exception("SEL audit for auto-rejected permission failed")

        audit_task = asyncio.ensure_future(asyncio.to_thread(_audit))
        # Retain the task so it cannot be garbage-collected mid-flight; the
        # done callback drops the reference and surfaces nothing (audit
        # failures are already logged inside _audit).
        self._audit_tasks.add(audit_task)
        audit_task.add_done_callback(self._audit_tasks.discard)

    def _note_dropped_frame(self, session_id: object, method: object) -> None:
        """Count one unroutable frame, flushing a summary at most once per interval.

        Replaces a per-frame log line (see the drop-accounting constants above).
        Cheap and synchronous by design: it is called from the hot demux path
        and must not await, so there is no timer task to leak and no blocking
        I/O beyond the throttled ``logger.debug`` the flush itself emits.

        Both arguments are backend-controlled and deliberately typed `object`:
        they are normalized through `_drop_key_part`, which is the only thing
        that keeps a wrong-typed value from raising in the shared reader.
        """
        key = (_drop_key_part(session_id), _drop_key_part(method))
        # Hang-resilience series: classify the drop by method so dashboards
        # can alert on the pre-fix hang signature. ``method_class`` is a
        # closed 3-value enum — the raw method (backend-controlled) never
        # becomes an attribute value.
        _m = method if isinstance(method, str) else ""
        if _m == METHOD_REQUEST_PERMISSION:
            _mclass = "permission"
        elif _m in self._notification_aliases().session_update:
            # EVERY session-update spelling this host uses classifies as
            # "update": a dashboard alerting on the pre-fix hang signature must
            # see a dropped extension-method child update the same way it sees
            # the plain spelling, and a spelling the host's aliases omit would be
            # counted as "other" and lost.
            _mclass = "update"
        else:
            _mclass = "other"
        emit_counter(DROPPED_FRAMES, {"method_class": _mclass})
        now = time.monotonic()
        if self._dropped_frames_flushed_at == 0.0:
            # First drop of this runtime's life opens the window. __init__ cannot
            # supply the baseline (a runtime may be constructed long before
            # spawn()), and a stale 0.0 would make every first drop flush
            # immediately instead of aggregating.
            self._dropped_frames_flushed_at = now
        counts = self._dropped_frames
        if key not in counts and len(counts) >= _DROP_SUMMARY_MAX_KEYS:
            # A wide fan-out of distinct keys inside one interval must not grow
            # the map; report what we have and start a fresh window.
            self._flush_dropped_frames(now)
        counts[key] = counts.get(key, 0) + 1
        if now - self._dropped_frames_flushed_at >= _DROP_SUMMARY_INTERVAL_SECS:
            self._flush_dropped_frames(now)

    def _flush_dropped_frames(self, now: float | None = None) -> None:
        """Emit one summary record per (sessionId, method) and reset the window.

        Called on the interval from _note_dropped_frame and unconditionally when
        the reader loop exits, so a low-rate trickle is reported late rather
        than swallowed. A key seen once in an otherwise idle hour is therefore
        reported at the next drop or at loop exit — deliberately traded for
        having no wakeup timer on the event loop.
        """
        self._dropped_frames_flushed_at = time.monotonic() if now is None else now
        counts = self._dropped_frames
        if not counts:
            return
        for (session_id, method), count in counts.items():
            logger.debug(
                "Dropped %d unroutable frame(s) for session %s (method=%s)",
                count,
                _loggable_request_id(session_id),
                _loggable_request_id(method),
            )
        counts.clear()

    async def _reader_loop(self) -> None:
        """Single reader task — owns stdout exclusively. Routes frames by type.

        Routing:
          1. Response with id in _pending_requests → resolve Future
          2. Response with id in _routed_requests → put in session queue
          3. Notification with params.sessionId → session queue
          4. Request (method + id) with no sessionId → answered ONCE at
             connection level (-32601), never broadcast
          5. No sessionId → broadcast to all queues
        """
        assert self._process and self._process.stdout
        stdout = self._process.stdout
        # This host's spellings for the three aliasable inbound events, read ONCE
        # per reader rather than per frame: they are fixed for the process's life
        # and this is the hot demux path.
        _aliases = self._notification_aliases()
        _session_update = _aliases.session_update
        _subagent_list_update = _aliases.subagent_list_update
        _mcp_init = _aliases.mcp_init

        try:
            while True:
                try:
                    line = await stdout.readuntil(b"\n")
                except asyncio.IncompleteReadError as exc:
                    # EOF, possibly holding a trailing unterminated line. Keep
                    # readline()'s old shape: hand the partial to the parser, and
                    # an empty partial falls through to the exit branch below.
                    line = exc.partial
                except asyncio.LimitOverrunError as exc:
                    # ONE oversize frame must not kill the demux — same invariant
                    # as the non-dict and non-numeric-id guards below. Tearing
                    # the runtime down here ends EVERY multiplexed session
                    # mid-turn, which is what users see as "process exited /
                    # chat failure" after a single huge tool result.
                    #
                    # _drain_oversize_line consumes the whole line THROUGH its
                    # terminating newline and discards it, so the stream is back
                    # on a frame boundary and no byte-slice of the oversize line
                    # ever reaches json.loads. Its budget is per call and needs no
                    # cross-iteration state, because every call that returns ends
                    # on a boundary — so a replay of oversize-but-terminated
                    # frames is survivable frame after frame.
                    #
                    # An awaited request whose response was in a dropped frame is
                    # never orphaned: when the frame's head names it, it fails at
                    # once with AcpFrameTooLarge (below); otherwise
                    # _send_and_await's wait_for(timeout=...) ends it, and the ids
                    # in flight at the drop are logged so that timeout is
                    # attributable.
                    head = bytearray()
                    try:
                        dropped = await _drain_oversize_line(stdout, exc, head=head)
                    except asyncio.IncompleteReadError:
                        # The same observed EOF as the empty-line branch below,
                        # reached with a torn frame in hand; the exit is confirmed
                        # and retired the same way.
                        self._mark_dead("stdout closed mid-oversize-line")
                        await self._retire_tracking_after_exit()
                        return
                    except OversizeLineUnrecoverable as fatal:
                        logger.error("stdout unrecoverable: %s", fatal)
                        self._mark_dead(f"stdout overrun: {fatal}")
                        return
                    logger.warning(
                        "dropped an oversize stdout frame (%d bytes); resynced at "
                        "next frame (in-flight awaited=%s routed=%s): %s",
                        dropped,
                        sorted(self._pending_requests)[:_DROP_IDS_IN_LOG],
                        sorted(self._routed_requests)[:_DROP_IDS_IN_LOG],
                        exc,
                    )
                    # A reply to an awaited request fails THAT request now, with
                    # the size and the limit, instead of the timeout it would
                    # otherwise hit much later under an unrelated name.
                    req_id = _oversize_frame_request_id(bytes(head))
                    pending = self._pending_requests.get(req_id) if req_id is not None else None
                    if pending is not None and not pending.done():
                        self._pending_requests.pop(req_id, None)
                        pending.set_exception(AcpFrameTooLarge(_oversize_frame_message(dropped)))
                    continue

                if not line:
                    rc = self._process.returncode if self._process else "?"
                    self._mark_dead(self._exit_reason(rc))
                    # The one death path that OBSERVES the exit rather than
                    # causing it. ``kill()`` retires the registry entries after
                    # its own reap; nothing else did, so an externally killed
                    # root stayed in ``kiro_session_pids.txt`` until the
                    # periodic sweep's next tick -- up to 300s of a dead pid a
                    # reaper could resignal.
                    await self._retire_tracking_after_exit()
                    return

                self._last_activity = time.monotonic()

                # Only a JSON object is a frame. Every other line is skipped here
                # and never reaches the crash arm below, which would mark this
                # single-owner reader dead and end EVERY multiplexed session:
                # text that is not JSON, a bare scalar or array (`123`, `"foo"`,
                # `[1,2]`, `null`), nesting past the decoder's ceiling
                # (RecursionError), and an integer past the int-string digit
                # limit. Bytes that are not UTF-8 decode with replacement, as in
                # AcpClient._read_message, so one bad byte inside a string costs
                # that character, not the frame.
                parsed = parse_json_object_line(line, errors="replace")
                if parsed is None:
                    if self.recording_allowed:
                        logger.debug("stdout line that is not a JSON object: %s", line[:200])
                    continue
                data: dict[str, Any] = parsed

                # Opt-in raw-frame recording for the replay corpus. A no-op
                # unless KIROCREW_ACP_RECORD_FRAMES names a directory: it
                # returns before awaiting anything, so an ordinary run pays one
                # env lookup. When it IS set the frame is queued for the
                # recorder's own writer thread rather than written here,
                # because a filesystem syscall on this loop stalls every
                # multiplexed session. It never raises -- see
                # kiro_crew.acp._frame_record.
                if self.recording_allowed:
                    await record_frame(self._acp_backend, data, len(line))

                projection = getattr(self, "_native_skill_projection", None)
                if projection is not None:
                    data = projection.frame(data)
                msg = JsonRpcMessage.from_dict(data)

                # Route responses
                if msg.id is not None and (msg.result is not None or msg.error is not None):
                    # JSON-RPC allows string ids, and this runtime only ever
                    # issues int ids — but the id in the response is agent-
                    # controlled. int("req-1") / int([...]) raises ValueError/
                    # TypeError, which the catch-all below turns into
                    # _mark_dead, poisoning EVERY multiplexed session over one
                    # unmatched frame. Same invariant as the non-dict guard
                    # above: skip the frame, don't kill the demux.
                    try:
                        req_id = msg.id if isinstance(msg.id, int) else int(msg.id)
                    except (TypeError, ValueError, OverflowError):
                        # OverflowError: json parses 1e9999 to float("inf"),
                        # which int() rejects differently from a bad string.
                        #
                        # Left per-frame on purpose (same for the unmatched-id
                        # line below), unlike the two session-routing drops:
                        # here the ID is the whole diagnostic value, and it is a
                        # distinct value per frame — aggregating by it would give
                        # the counter an unbounded key space, while aggregating
                        # without it would throw away the only datum that
                        # identifies the correlation bug. Both branches also
                        # require a response-shaped frame, i.e. one per request
                        # THIS runtime issued (bounded by turns), so neither has
                        # the after-teardown steady state that made the
                        # unknown-session line a flood.
                        logger.debug("Response with non-numeric id %r dropped", msg.id)
                        continue

                    # Check awaited requests first (init, session/new, set_mode)
                    future = self._pending_requests.pop(req_id, None)
                    if future and not future.done():
                        if msg.error:
                            future.set_exception(_runtime_rpc_exception(msg.error))
                        else:
                            future.set_result(msg.result or {})
                        continue

                    # Check routed requests (prompt response → session queue)
                    session_id = self._routed_requests.pop(req_id, None)
                    if session_id and session_id in self._session_queues:
                        await self._session_queues[session_id].put(msg)
                        continue

                    logger.debug("Unmatched response id=%d", req_id)
                    continue

                # Inbound server→client REQUEST (method + id, no result/error).
                # A connection-level request Crew answers ITSELF is one this host's
                # harness names, and today that is the engine's credential callback
                # (_kiro/auth/getAccessToken) — answered only on a process spawned
                # with Crew as the auth owner (see _resolve_spawn_plan /
                # kas_transport.build_kas_argv). It carries no sessionId — the
                # first one arrives before session/new has even returned — so it is
                # handled here, OFF this loop: resolving and possibly refreshing a
                # token must not block stdout demux for every other multiplexed
                # session. On a cli-owned spawn the frame never arrives, and a
                # method the harness does not name falls through to the -32601
                # ownerless answer below rather than ever being paid with a
                # credential. The harness resolves that list from
                # ACP_BACKENDS_HOST_AUTH_CALLBACK, so it and this guard cannot
                # disagree about who answers what.
                if (
                    msg.id is not None
                    and msg.result is None
                    and msg.error is None
                    and self._kas_host_auth
                    and msg.method in self._harness.host_answered_methods
                ):
                    # Same bounded progress-or-dead admission as permission
                    # answers: this is a request, so the counted-drop path that
                    # is valid for notifications is not — and it shares the one
                    # answer-task set so the combined total stays under the real
                    # resource ceiling.
                    if not await self._wait_for_answer_capacity(msg, request_kind="KAS auth"):
                        continue
                    _auth_task = asyncio.ensure_future(
                        self._answer_host_request(msg.id, msg.method or "")
                    )
                    self._answer_tasks.add(_auth_task)
                    _auth_task.add_done_callback(self._answer_tasks.discard)
                    continue
                # The consent URL of an MCP sign-in this runtime started. It
                # names no session, so it is answered here and handed to the
                # session that owns the sign-in; a host that never declared the
                # capability falls through to the -32601 answer below.
                if (
                    msg.id is not None
                    and msg.result is None
                    and msg.error is None
                    and msg.method == METHOD_KAS_OPEN_EXTERNAL_URL
                    and self._harness.opens_external_urls
                ):
                    if not await self._wait_for_answer_capacity(msg, request_kind="MCP sign-in"):
                        continue
                    _url_task = asyncio.ensure_future(
                        self._answer_open_external_url(msg.id, msg.params)
                    )
                    self._answer_tasks.add(_url_task)
                    _url_task.add_done_callback(self._answer_tasks.discard)
                    continue
                # Any other request that arrives without a sessionId is
                # unroutable and is answered -32601 by
                # _answer_ownerless_request below, rather than being left to
                # hang.

                # Route notifications by sessionId
                session_id = (msg.params or {}).get("sessionId")
                if not session_id and _subagent_list_update and msg.method == _subagent_list_update:
                    # Snapshot backend-internal subagent session ids before the
                    # broadcast below delivers the frame to the UI consumers.
                    # Each frame carries the FULL current list, so replace.
                    self._snapshot_subagent_sessions(msg.params or {})
                if session_id:
                    # A frame tagged with a sessionId belongs to exactly one
                    # session. Route to it if registered; otherwise DROP it.
                    # Broadcasting a known-but-unregistered session's frame to
                    # every other session would be cross-talk.
                    queue = self._session_queues.get(session_id)
                    if queue is not None:
                        if msg.is_method(METHOD_AGENT_SWITCHED):
                            self._note_switched_model(session_id, msg.params)
                        await queue.put(msg)
                    elif (
                        session_id in self._subagent_sessions
                        and self._subagent_owner is not None
                        and list(self._session_queues) == [self._subagent_owner]
                        and (
                            msg.is_method(METHOD_REQUEST_PERMISSION)
                            or msg.method in _session_update
                        )
                    ):
                        # A frame for a backend-internal subagent the backend
                        # itself announced via `subagent/list_update`, on a
                        # runtime with an UNAMBIGUOUS consumer (exactly one
                        # registered session — the dashboard-slot shape).
                        #
                        # - session/update — under EITHER spelling: kiro-cli
                        #   2.21.x emits child updates as the extension method
                        #   `_kiro.dev/session/update` where earlier versions
                        #   used plain `session/update`. Routed so the
                        #   consumer's per-toolCallId caches capture the
                        #   child's REAL command bytes; the handle re-tags
                        #   them as crew activity, never as parent transcript.
                        #   Both spellings must route: a dropped child update
                        #   leaves the caches empty, child MCP identity
                        #   unverified, and every auto-approve path falls to
                        #   the interactive card.
                        # - session/request_permission: routed so the child's
                        #   approval flows through the exact policy pipeline a
                        #   main-agent approval takes — with the command bytes
                        #   above, mode behavior (normal/read/trust/yolo) is
                        #   IDENTICAL to the main agent's. Dropping a REQUEST
                        #   is never an option: it strands the backend's
                        #   response oneshot and wedges the child's whole tool
                        #   batch until process teardown, with every approval in
                        #   that batch hanging invisibly for as long as that
                        #   runtime lives.
                        #
                        # With several registered sessions the frame names no
                        # owner; a permission request then falls to the
                        # fail-closed auto-answer below and updates are
                        # counted drops as before.
                        #
                        # A permission REQUEST is routed only while the owner
                        # has an in-flight prompt (an outstanding routed
                        # request = the dispatch loop is consuming the queue).
                        # Between turns nothing reads the queue until the next
                        # prompt's drain, so a background child's request
                        # would sit unanswered — the original hang with extra
                        # steps. Answer it fail-closed NOW instead.
                        _owner_turn_active = self._subagent_owner in self._turn_active_sessions
                        if (
                            msg.id is not None
                            and msg.is_method(METHOD_REQUEST_PERMISSION)
                            and not _owner_turn_active
                        ):
                            await self._spawn_answer_task(
                                msg,
                                session_id,
                                # Registered + announced — the owner just
                                # has no in-flight prompt. A distinct SEL
                                # tag keeps normal background-child
                                # behavior distinguishable from a real
                                # misconfiguration in the audit trail.
                                reason="owner_no_active_turn",
                            )
                            # Yield so spawned answer tasks actually RUN
                            # between frames: with 129+ frames already
                            # buffered, readline() returns without
                            # suspending, and the reader would hit the
                            # flood cap before any answer task had a chance
                            # to complete — falsely killing a responsive
                            # runtime. One loop-tick lets quick answers
                            # drain; a genuinely wedged backend still
                            # accumulates blocked tasks and trips the cap.
                            await asyncio.sleep(0)
                        elif not _owner_turn_active:
                            # An UPDATE between the owner's turns (either
                            # session/update spelling). Nothing reads the
                            # queue until the next prompt's dispatch loop,
                            # and _run_turn clears the per-toolCallId caches
                            # at turn start and then discards stale
                            # non-permission frames from the queue — so a
                            # between-turns update can never contribute a
                            # cache write or an activity event. Queueing it
                            # would only grow an unbounded queue in gateway
                            # memory while the slot idles (session queues
                            # have no depth cap). Unlike a REQUEST there is
                            # no protocol obligation to answer, so take the
                            # counted-drop path.
                            self._note_dropped_frame(session_id, msg.method)
                        else:
                            # Hang-resilience series: a child permission
                            # request delivered to the mode-parity pipeline.
                            # Counted so routed requests are observable next
                            # to the dropped-frame counter — a permission
                            # request that is neither routed nor answered
                            # leaves its crew waiting on a prompt nobody can
                            # see.
                            if msg.id is not None and msg.is_method(METHOD_REQUEST_PERMISSION):
                                emit_counter(CHILD_PERMISSION_ROUTED, {"surface": "runtime"})
                            await next(iter(self._session_queues.values())).put(msg)
                    elif msg.id is not None and msg.is_method(METHOD_REQUEST_PERMISSION):
                        # Unannounced, ambiguous, or announced past the
                        # recognition cap: nobody on this client can see or
                        # answer the prompt — answer NOW with the request's own
                        # least-destructive reject option so the backend
                        # subagent gets a tool error instead of hanging. Never
                        # auto-APPROVE: no policy engine has seen the tool call
                        # and, past the cap, the id cannot be shown to be this
                        # owner's child at all.
                        #
                        # The reason names WHICH of those it was. Past the cap a
                        # roster truncation, not the backend's silence, is what
                        # cost this child its approval card, and that is the one
                        # thing an operator can act on (the two counts in the
                        # snapshot warning above are the same signal).
                        await self._spawn_answer_task(
                            msg,
                            session_id,
                            reason=(
                                _ROSTER_OVERFLOW_REJECT_REASON
                                if self._subagent_roster_overflow
                                else _UNREGISTERED_REJECT_REASON
                            ),
                        )
                        # Same yield rationale as the routed-owner branch above.
                        await asyncio.sleep(0)
                    elif msg.method in _mcp_init and (
                        # Read live, never hoisted beside _mcp_init: collectors
                        # appear and settle during one reader lifetime.
                        self._session_inits_in_flight
                        or self._start_collectors
                    ):
                        self._stage_init_frame(msg)
                    else:
                        # Counted, not logged per frame: this is the measured
                        # flood (transcript replay during session/load, plus any
                        # backend still streaming after teardown).
                        self._note_dropped_frame(session_id, msg.method)
                    continue

                # No sessionId. An id-carrying frame that still has a method is
                # a server→client REQUEST that names no session — it expects
                # exactly ONE response, so the runtime answers it at connection
                # level (same shape as the KAS auth callback above) instead of
                # broadcasting. Broadcasting would hand it to EVERY registered
                # session's dispatch loop, each of which replies -32601 on the
                # shared stdin: one id, N responses — a JSON-RPC protocol
                # violation that widens with session sharing. Frames with an id
                # but NO method are responses (e.g. a result of null slips past
                # the result/error check above); their handling is unchanged.
                if msg.id is not None and msg.method is not None:
                    # Same volume bound as the permission auto-answers: each
                    # reply can block on stdin drain() against a backend that
                    # floods frames while never reading, so the task must be
                    # retained (a bare ensure_future can be GC'd mid-flight)
                    # and counted. Past the cap the frame takes the counted-
                    # drop path — the flooding backend hangs on its own
                    # unanswered request instead of growing the task set.
                    if len(self._answer_tasks) >= self._max_answer_tasks:
                        self._note_dropped_frame(_DROP_NO_SESSION, msg.method)
                        continue
                    _t = asyncio.ensure_future(self._answer_ownerless_request(msg.id, msg.method))
                    self._answer_tasks.add(_t)
                    _t.add_done_callback(self._answer_tasks.discard)
                    continue

                # No sessionId → genuinely global notification; broadcast to all.
                if self._session_queues:
                    # Snapshot: `await queue.put` yields, and a concurrent
                    # unregister_session() could pop mid-iteration otherwise.
                    _queues = list(self._session_queues.values())
                    # Fanning one ownerless frame out to SEVERAL sessions means
                    # at most one recipient produced it and nothing says which,
                    # so mark it: a consumer that measures its own activity (the
                    # subagent idle-stall clock) must not count another tenant's
                    # traffic. A lone session IS the sole owner, so it is left
                    # unmarked and keeps reading the frame as its own.
                    if len(_queues) > 1:
                        msg.fanout_no_owner = True
                    for queue in _queues:
                        await queue.put(msg)
                else:
                    # Same unbounded shape as the unknown-session branch: with
                    # zero registered sessions EVERY global notification lands
                    # here, so a backend that keeps streaming after the last
                    # teardown floods at frame rate. Counted the same way, with
                    # a sentinel for the session half of the key.
                    self._note_dropped_frame(_DROP_NO_SESSION, msg.method)

        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.error(
                "Reader loop crashed: %s",
                exc if self.recording_allowed else type(exc).__name__,
                exc_info=self.recording_allowed,
            )
            self._mark_dead(f"reader crash: {exc}")
        finally:
            # Report the residual count on EVERY exit (EOF, overrun, cancel,
            # crash) so a trickle that never reached the interval is still
            # accounted for instead of vanishing with the task. Both throttled
            # summaries: the reader is the only thing that feeds either, so its
            # exit is the last chance to state what they were holding.
            self._flush_dropped_frames()
            self._flush_roster_overflow()

    def _notification_aliases(self) -> NotificationAliases:
        """This host's inbound method spellings, or none when no harness serves it.

        The reader loop is the one seam that must not demand a harness. It is
        driven for every id in ``ACP_BACKENDS_KNOWN``, two of which the
        shared-process runtime has no harness for, and a refusal inside the single
        stdout owner would mark the runtime dead and take every multiplexed session
        with it. The SPAWN is where an unserved backend is refused, before any
        process exists.

        Declaring no aliases DROPS the three aliasable events rather than reading
        them as kiro's, which is the direction that cannot mistake one host for
        another. Routing a frame that names its own session sits upstream of this
        and is unaffected.
        """
        try:
            return self._harness.notification_aliases
        except ValueError:
            return NotificationAliases()

    async def _answer_get_access_token(self, request_id: int | str) -> None:
        """Answer this host's credential callback, by its own method name.

        Kept under this name because it is the shape callers and tests ask for;
        the method the host actually sent is resolved from its harness.
        """
        await self._answer_host_request(request_id, METHOD_KAS_AUTH_GET_ACCESS_TOKEN)

    async def _answer_host_request(self, request_id: int | str, method: str) -> None:
        """Answer a connection-level request this host's harness claims.

        Runs OFF the reader loop. The response is built by the HARNESS that named
        ``method`` in ``host_answered_methods``, never by a hardcoded answerer:
        the guard upstream already accepts any method a host claims, so a fixed
        answerer would hand one host's credential to another host that happened to
        claim a different method — the mistaken-identity failure this layer exists
        to prevent. For KAS that answer resolves and refreshes under the
        cross-process lock and withholds the refresh token; it is never cached
        here and never logged.

        On any failure the host is sent a JSON-RPC error, which it treats as an
        expired credential and turns into its sign-in prompt, rather than being
        left to hang on the callback. The INFO line is the positive "host is
        drawing its credential from Crew" signal, logged once per runtime and
        only AFTER a response was actually written, so a callback that failed
        never counts as served.
        """
        try:
            result = await self._harness.answer_request(method)
        except HostAuthCallbackError as exc:
            # str(exc) is token-free by construction (see kas_host_auth).
            logger.warning("KAS auth callback failed: %s", exc)
            try:
                await self.send_error(request_id, KAS_AUTH_CALLBACK_ERROR_CODE, str(exc))
            except AcpRuntimeDead:
                pass
            return
        try:
            await self.send_response(request_id, result)
        except AcpRuntimeDead:
            # Process gone before the answer could be written; nothing to do.
            return
        if not self._kas_host_auth_logged:
            self._kas_host_auth_logged = True
            logger.info(
                "KAS auth callback served from Crew vault — agent=%s (PID %s)",
                self._agent or "<none>",
                self._pid,
            )
        else:
            logger.debug(
                "KAS auth callback served from Crew vault — agent=%s (PID %s)",
                self._agent or "<none>",
                self._pid,
            )

    def mcp_sign_in_holds(self, session_id: str, server_name: str) -> bool:
        """Whether the runtime holds this session and server's sign-in slot."""
        return self._mcp_sign_in == (session_id, server_name)

    def begin_mcp_sign_in(self, session_id: str, server_name: str) -> bool:
        """Start an explicit OAuth sign-in for one MCP server of one session.

        Sends ``_kiro/mcp/resetServer`` with ``startOAuth`` in the background: the
        engine answers only when the connect attempt ends, which is after the user
        signs in or after the server's connect timeout. The consent URL arrives
        meanwhile as ``_kiro/openExternalUrl`` and is delivered to *session_id*.

        Returns False, starting nothing, when the host sends no consent URL, the
        session is not registered, or another sign-in is in flight: the URL
        request names neither session nor server, so a second concurrent sign-in
        would make its link unattributable. The caller offers again later.

        The slot is held until the reset is answered, even after its session
        unregisters: that session's link can still arrive, and it is refused
        because the session's queue is gone rather than handed to a newer
        sign-in.
        """
        if not self._harness.opens_external_urls or self._dead:
            return False
        if self._mcp_sign_in is not None or session_id not in self._session_queues:
            return False
        if self._mcp_reattempts:
            # A consent URL the engine sent for a re-attempt would be handed
            # to this sign-in's server; wait until the re-attempts are answered.
            return False
        self._mcp_sign_in = (session_id, server_name)
        task = asyncio.ensure_future(self._run_mcp_sign_in(session_id, server_name))
        self._answer_tasks.add(task)
        task.add_done_callback(self._answer_tasks.discard)
        return True

    async def _run_mcp_sign_in(self, session_id: str, server_name: str) -> None:
        """Hold the sign-in slot until the engine's reset answer arrives."""
        entry = (session_id, server_name)
        try:
            await self._send_and_await(
                METHOD_KAS_MCP_RESET_SERVER,
                {"sessionId": session_id, "serverName": server_name, "startOAuth": True},
                timeout=_MCP_SIGN_IN_TIMEOUT,
            )
        except Exception as exc:  # noqa: BLE001 -- the next offer retries
            logger.info(
                "MCP sign-in for %s ended without an answer: %s",
                sanitize_sink_text(server_name, NAME_CAP),
                type(exc).__name__,
            )
        finally:
            if self._mcp_sign_in == entry:
                self._mcp_sign_in = None

    @property
    def reattempts_mcp_servers(self) -> bool:
        """Whether this host can connect one failed MCP server of a session again.

        The capability :meth:`begin_mcp_reattempt` checks first; a session asks
        it to decide whether a credential failure is still being re-attempted
        or should be named for a new session.
        """
        return bool(self._harness.opens_external_urls) and not self._dead

    def begin_mcp_reattempt(self, session_id: str, server_name: str) -> bool:
        """Ask the engine to connect a failed MCP server of one session again.

        For a server that failed to start for want of a credential that may
        exist now. Sends ``_kiro/mcp/resetServer`` with ``startOAuth``
        false in the background; the server's next ``_kiro/mcp/status`` entry
        reports whether it connected. The host that takes this request is the
        one that sends consent URLs, so the same capability gates both.

        Returns False, starting nothing, on a host without the request, when
        the session is not registered, while an OAuth sign-in holds the slot
        (a consent URL this reset produced would be handed to that sign-in's
        server), or while a re-attempt of the same server is still unanswered.
        Re-attempts of different servers may run together: none of them opens
        a consent URL, and one that arrives with no sign-in in flight is
        refused.
        """
        if not self.reattempts_mcp_servers:
            return False
        entry = (session_id, server_name)
        if self._mcp_sign_in is not None or session_id not in self._session_queues:
            return False
        if entry in self._mcp_reattempts:
            return False
        self._mcp_reattempts.add(entry)
        task = asyncio.ensure_future(self._run_mcp_reattempt(session_id, server_name))
        self._answer_tasks.add(task)
        task.add_done_callback(self._answer_tasks.discard)
        return True

    async def _run_mcp_reattempt(self, session_id: str, server_name: str) -> None:
        """Hold the re-attempt until the engine's reset answer arrives."""
        entry = (session_id, server_name)
        try:
            await self._send_and_await(
                METHOD_KAS_MCP_RESET_SERVER,
                {"sessionId": session_id, "serverName": server_name, "startOAuth": False},
                timeout=_MCP_REATTEMPT_TIMEOUT,
            )
        except Exception as exc:  # noqa: BLE001 -- the next turn may try again
            logger.info(
                "MCP re-attempt for %s ended without an answer: %s",
                sanitize_sink_text(server_name, NAME_CAP),
                type(exc).__name__,
            )
        finally:
            self._mcp_reattempts.discard(entry)

    async def _answer_open_external_url(self, request_id: int | str, params: Any) -> None:
        """Deliver an MCP consent URL to the session whose sign-in is in flight.

        The URL becomes an ordinary ``_kiro.dev/mcp/oauth_request`` frame on that
        session's queue, so it takes the same safety checks and banner as the
        kiro-cli engine's own request. It is refused with a JSON-RPC error when
        no sign-in is in flight (the link cannot be attributed to a server), when
        the owning session is gone, or when the URL is not http(s), which the
        banner would refuse. The engine raises on that error, so its connect
        attempt ends at once instead of waiting for a callback no user was shown.
        """
        url = params.get("url") if isinstance(params, dict) else None
        entry = self._mcp_sign_in
        queue = self._session_queues.get(entry[0]) if entry is not None else None
        refusal = ""
        if entry is None:
            refusal = "no MCP sign-in is in flight"
        elif queue is None:
            refusal = "the MCP sign-in's session has left"
        elif not isinstance(url, str) or not _is_safe_oauth_url(url):
            refusal = "the consent URL is not an http(s) URL"
        else:
            session_id, server_name = entry
            await queue.put(
                JsonRpcMessage(
                    method=METHOD_MCP_OAUTH_REQUEST,
                    params={"sessionId": session_id, "serverName": server_name, "oauthUrl": url},
                )
            )
        try:
            if refusal:
                logger.info("MCP consent URL not opened: %s", refusal)
                await self.send_error(request_id, _MCP_URL_NOT_OPENED, refusal)
            else:
                await self.send_response(request_id, {"success": True})
        except AcpRuntimeDead:
            return

    async def _answer_ownerless_request(self, request_id: int | str, method: str) -> None:
        """Answer a server→client request that names no session with -32601.

        Runs OFF the reader loop (same shape as the KAS auth callback) so a
        stalled stdin drain cannot block stdout demux for every multiplexed
        session. The routed case — an unknown request WITH a sessionId — is
        deliberately not handled here: it is delivered to that session's queue
        and answered once by its dispatch loop (``server_request_unknown``).
        """
        logger.debug(
            "Ownerless server request answered -32601 — method=%s id=%s",
            _loggable_request_id(method),
            _loggable_request_id(request_id),
        )
        try:
            await self.send_error(request_id, _JSONRPC_METHOD_NOT_FOUND, "Method not found")
        except AcpRuntimeDead:
            pass

    def saw_not_logged_in(self) -> bool:
        """True if kiro-cli reported an auth failure on stderr.

        Lets callers translate a runtime death into AcpAuthRequired (an
        actionable login prompt) instead of a generic process-death error —
        parity with AcpClient, which inspects stderr the same way.

        Recognises the full auth vocabulary rather than the literal banner
        ``not logged in``: a real expired bearer token writes
        ``AccessDeniedException: "Invalid token"`` and ``the bearer token
        included in the request is invalid`` and says ``not logged in`` nowhere,
        so a single-banner regex answers False on exactly the state this exists
        to detect, and the operator is shown a ``session/new`` timeout instead.

        Reads the latch, not the ring buffer: see ``_saw_auth_failure``.
        """
        return self._saw_auth_failure

    async def settle_stderr(self, timeout: float = 0.5) -> None:
        """Give the stderr drain a bounded chance to finish before its latches are read.

        The latches (:meth:`saw_sandbox_init_failure`, :meth:`saw_not_logged_in`)
        are written by the ``_drain_stderr`` TASK, while a death is discovered on
        the stdout side: ``_reader_loop`` sees EOF and ``_mark_dead`` fails the
        pending ``initialize`` future SYNCHRONOUSLY. Both tasks become runnable
        together -- which is the ordinary shape of a real refusal, since the child
        writes its signature and closes stdout at once -- so a caller that reads a
        latch straight off that failure can win the race and see ``False`` for a
        line already in the pipe.

        Bounded and swallowing, because the caller is already on a failure path:
        the worst case of not settling is the generic error it would have produced
        anyway, and no failure here may become a second failure. The same budget
        as ``AcpClient._read_message``'s own EOF drain, but shielded: this child
        may still be alive and the drain is still the latches' only writer. A
        cancel of the caller propagates; see :func:`settle_drain`.
        """
        await settle_drain(self._stderr_task, timeout)

    def saw_sandbox_init_failure(self) -> bool:
        """True if an OS sandbox told this runtime's child it could not initialize.

        Lets callers translate a runtime death into ``AcpSandboxInitFailed`` -- a
        non-retryable error naming the layer that refused and its switch --
        instead of a generic process-death error the retry ladder then reproduces
        on a host that will refuse identically. Parity with
        :meth:`saw_not_logged_in`, including reading the latch rather than the
        ring buffer.
        """
        return self._saw_sandbox_init_failure

    @property
    def sandbox_wrapped_by_crew(self) -> bool:
        """Whether Kiro Crew's own sandbox wrapped this runtime's child.

        The one fact a sandbox refusal's stderr cannot carry: WHICH layer to turn
        off. A harness's internal sandbox nested inside Crew's wrap fails with the
        harness's wording while the layer the operator must change is Crew's.
        """
        return self._sandbox_wrapped_by_crew

    def redacted_stderr_tail(self) -> str:
        """The retained stderr lines, newline-joined and redacted.

        A SEPARATE reader from :meth:`death_summary`, which exists to be read by a
        person: it folds the same lines onto one line behind a ``returncode``
        prefix. Anything that classifies PER LINE -- the launcher's own refusal
        test -- sees nothing in that shape, so it needs the lines as lines.

        Redacted like every other path out of this buffer: child stderr is
        untrusted subprocess output that can echo a credential. Empty for a
        restricted session, which retains nothing.
        """
        if not self._stderr_lines:
            return ""
        tail = "\n".join(self._stderr_lines)
        tail, _ = redact_exfiltration_urls(tail)
        tail, _ = redact_credentials(tail)
        return tail

    @property
    def sandbox_mode(self) -> str:
        """The sandbox tier this runtime's child was spawned under."""
        return self._sandbox_mode

    @property
    def sandbox_hidden_dirs(self) -> tuple[str, ...]:
        """The extra path masks this spawn asked its sandbox for.

        Read with :attr:`sandbox_mode` when a trusted corroboration run has to
        rebuild the profile this spawn was actually refused under.
        """
        return self._sandbox_hidden_dirs

    async def _exit_watch_loop(self) -> None:
        """Fail the runtime on the root process's OWN exit, not on stdout EOF.

        The reader loop observes a death only as EOF on stdout. A descendant
        that inherited the root's write end holds that pipe open after the root
        is gone, so EOF does not arrive and the reader -- parked on ``readuntil``
        while nothing is being written -- cannot notice the broken pipe either.
        The death is then invisible until the survivor exits (the pipe finally
        closing) or idle expiry force-reaps the session, on the order of an hour.

        This watcher keys on the exit itself, by polling ``process.returncode``.
        ``returncode`` is set by the event loop's ``_process_exited`` -- the
        SIGCHLD/waitpid path -- the instant the root is reaped, independent of
        whether the pipes have disconnected. ``process.wait()`` cannot serve
        here: its waiters are woken only from ``_call_connection_lost``, after
        ALL pipe transports disconnect, so a descendant holding stdout open
        parks ``wait()`` exactly as long as it parks the reader -- the watcher
        would never fire for the very case it exists to catch. The poll sees a
        non-``None`` returncode as soon as the root is gone, whatever the pipes
        are doing. A still-``None`` returncode is NOT an exit (a closed pipe the
        module deliberately refuses to treat as death), so the watcher keeps
        looping rather than marking dead.

        On the confirmed exit it first gives the reader a bounded window to
        drain any final frame still readable on stdout -- a completed turn's
        response the backend wrote just before exiting -- then marks the runtime
        dead, which fails every pending request and poisons every session queue,
        so a waiting turn surfaces ``AcpProcessDied`` in seconds instead of
        waiting for a backstop timer. The drain yields in short slices while the
        reader keeps routing frames (it stamps ``_last_activity`` per frame) and
        ends the moment the reader goes quiet with an empty buffer, so it costs
        the fail-fast path at most one idle slice on a pipe with nothing left.

        The reader keeps priority: the drain window lets an EOF that arrives in
        the same breath (the ordinary same-second close) run the reader's own
        death+retire path first, and ``_mark_dead`` is idempotent, so the loser
        of that race is a no-op. The exit reason is read AFTER the returncode is
        observed, so it carries the real code rather than ``<not reaped>``.
        """
        process = self._process
        if process is None:
            return
        try:
            while process.returncode is None:
                await asyncio.sleep(self._EXIT_WATCH_POLL_INTERVAL)
        except asyncio.CancelledError:
            raise
        except Exception:
            # A returncode read that cannot complete (a mock without the
            # attribute, an OS reap race) leaves death detection to the reader's
            # EOF path. It must never crash the runtime.
            logger.debug(
                "AcpRuntime: exit watcher could not poll the runtime process", exc_info=True
            )
            return
        # Let the reader drain whatever is already readable before the mark.
        # The backend can write a final response and exit; those bytes sit in
        # the pipe the reader has not pulled yet. _mark_dead clears pending
        # routes and poisons the session queues, so marking before that frame is
        # routed loses a completed turn's response. Yield in short slices while
        # the reader keeps making progress (it stamps _last_activity per frame),
        # and stop as soon as it goes quiet for one slice -- nothing left to pull
        # -- or the total cap is reached. This drains readable frames WITHOUT
        # waiting for EOF, which the surviving descendant holding stdout open may
        # never deliver. An EOF observed in the same breath still wins: the
        # reader's own death+retire path runs during the grace, and _mark_dead is
        # idempotent so the slower observer here is a no-op.
        loop = asyncio.get_event_loop()
        deadline = loop.time() + self._EXIT_DRAIN_MAX
        while not self._dead and loop.time() < deadline:
            before = self._last_activity
            await asyncio.sleep(self._EXIT_DRAIN_GRACE)
            if self._dead:
                break
            if self._last_activity == before and not self._reader_has_buffered_input():
                # The reader processed nothing this slice and nothing is waiting
                # in its buffer: the pipe is drained, stop waiting.
                break
        # Mark dead only once -- a reader-crash, broken-pipe or write-stall path
        # may already have set it before the root exited. But those paths mark
        # dead WITHOUT retiring the registry, so the confirmed exit still owes
        # the retirement: run it whether or not this watcher is the one that
        # marked. _retire_tracking_after_exit is identity-guarded and idempotent,
        # so a second call once the reader's EOF branch has already retired is a
        # no-op, and a stale PID-ledger line a non-retiring death mark left
        # standing is cleared the moment the exit is confirmed.
        if not self._dead:
            rc = process.returncode
            self._mark_dead(self._exit_reason(rc))
        # The reader is still parked on a stdout that never reaches EOF, so it
        # will not run the retire path the EOF branch owns; run it here instead.
        await self._retire_tracking_after_exit()

    def _reader_has_buffered_input(self) -> bool:
        """Whether stdout still holds bytes the reader has not consumed.

        ``StreamReader`` keeps already-read bytes in a private ``_buffer`` until
        a ``readuntil``/``read`` consumes them. A non-empty buffer means a frame
        (or part of one) is still waiting to be routed, so the exit-drain window
        must keep yielding to the reader rather than mark the runtime dead. The
        private attribute is read defensively: any failure reports "nothing
        buffered" so the drain ends on its cap rather than crashing.
        """
        process = self._process
        stdout = process.stdout if process is not None else None
        if stdout is None:
            return False
        try:
            return bool(stdout._buffer)  # type: ignore[attr-defined]
        except Exception:
            return False

    def _exit_reason(self, rc: object) -> str:
        """The death reason for a process that exited: its exit status, and a CAUSE only if proven.

        A bare ``rc=1`` told the operator nothing when every tool started failing
        because the runtime tmpfs had run out of inodes, so a cause belongs here
        when there is one. What does NOT belong is the child's last stderr line
        used AS the cause: it is whatever the child happened to flush last, which
        on a measured fleet put ``HTTP 404 Not Found`` on the card for deaths
        whose actual exit status was ``rc=-15`` -- a plain SIGTERM teardown. So
        :func:`_proven_death_cause` decides, over the whole retained tail, and an
        unexplained death reads as the signal or code it was.

        The tail is not lost, it is demoted: the last line goes to the debug log,
        redacted, for a reader who is already looking at this runtime. The card
        and the death summary keep their own separate ``stderr_tail`` field, so
        the evidence is still one hop away from anyone who needs it.
        """
        reason = f"process exited ({_rc_phrase(rc)})"
        if not self.recording_allowed:
            return reason
        tail = "\n".join(self._stderr_lines)
        if not tail.strip():
            return reason
        # A child's stderr is untrusted text that can echo a token or an
        # authority-bearing URL (a failed login prints the header it sent), and
        # both sinks below are real -- the reason travels to the session card and
        # the SEL, the debug line lands in the gateway log -- so redact before
        # either, and before any cut, which then cannot split a secret into a
        # half the redactor fails to recognise.
        tail, _ = redact_credentials(tail)
        tail, _ = redact_exfiltration_urls(tail)
        last = next((ln for ln in reversed(tail.splitlines()) if ln.strip()), "")
        if last:
            logger.debug(
                "AcpRuntime (PID %s) exit stderr tail: %s",
                self._pid,
                last[:_STDERR_REASON_TAIL_CHARS],
            )
        cause = _proven_death_cause(tail)
        if cause is None:
            return reason
        evidence, hint = cause
        # The signature was matched on the whole line, so a cause past the cap
        # still earns its slot; only the EVIDENCE is cut. The hint is appended
        # after the cut so a long line cannot push the operator's pointer out of
        # the message it is the whole point of.
        if len(evidence) > _STDERR_REASON_TAIL_CHARS:
            evidence = evidence[:_STDERR_REASON_TAIL_CHARS] + "…"
        reason = f"{reason}: {evidence}"
        if hint:
            reason = f"{reason} — {hint}"
        return reason

    # Rendered in place of a returncode that is not knowable YET. Both say
    # "no exit status", and an operator reading only the log cannot tell them
    # from a signal-killed child whose status was never captured -- which is
    # exactly the report this labelling answers.
    _RC_NOT_REAPED = "<not reaped>"
    _RC_NO_PROCESS = "<no process>"

    def _returncode_label(self) -> str:
        """This runtime's exit status for the death log, or why there is none.

        ``_kill_inner`` marks the death BEFORE it signals and reaps (pending
        waiters must learn of the death first), so every kill of a live
        runtime reads ``returncode is None`` here. So does a reader crash or a
        broken pipe on a child that is still running, and a never-spawned
        runtime has no process to ask at all. None of the three is "died by
        signal, status unknown", so none of them prints a bare ``None``.
        """
        if self._process is None:
            return self._RC_NO_PROCESS
        rc = self._process.returncode
        return self._RC_NOT_REAPED if rc is None else str(rc)

    def _compose_death_summary(self, reason: str, rc: str, tail: str) -> None:
        """Retain the one-line attribution ``death_summary()`` hands out.

        The parts are kept alongside it so the post-reap amendment can
        RECOMPOSE the line from them. Rewriting the composed text instead
        would search the child's stderr tail as well, and a tail that happened
        to carry this method's own ``[returncode=...]`` shape would be edited
        into an exit status -- the diagnostic corrupting the evidence it exists
        to carry.
        """
        self._death_reason = reason
        self._death_tail = tail
        self._death_summary = f"{reason} [returncode={rc}] stderr_tail: {tail}"

    def _note_reaped_after_kill(self, rc: object) -> None:
        """Fill in the exit status the kill path's death line could not know.

        Called once the reap has completed, while the process handle is still
        held. The retained summary is amended because it OUTLIVES the log --
        it rides ``AcpProcessDied`` into a turn's error and a cron's
        ``last_error`` -- and one line is logged at the death's own severity
        so the gateway log holds the code too. Silent when the status is still
        unknown (both waits timed out): ``<not reaped>`` is then still true.
        """
        self._note_reaped(rc, after_kill=True)

    def _note_reaped(self, rc: object, *, after_kill: bool) -> None:
        """Amend the death summary with *rc*; log it with the death's true origin.

        ``after_kill`` selects the wording, nothing else. The observed-exit path
        (the reader hitting EOF on a root that died from OUTSIDE -- an OOM kill,
        an operator's ``pkill``) must not log "reaped after kill": ``kill()``'s
        own contract is that a line saying "killed" names WHO killed, and a kill
        Kiro Crew never issued has no such name. Attribution is the whole reason
        the two paths log differently.
        """
        if rc is None or self._death_summary is None:
            return
        if self._death_label != self._RC_NOT_REAPED:
            return
        self._death_label = str(rc)
        self._compose_death_summary(self._death_reason, self._death_label, self._death_tail)
        log = logger.info if self._death_expected else logger.warning
        # Two LITERAL templates rather than one with the origin interpolated:
        # the raw template is what log filters and the tests select on, and
        # "reaped after kill" is the string they already match.
        if after_kill:
            log("AcpRuntime reaped after kill (PID %s): returncode=%s", self._pid, rc)
        else:
            log("AcpRuntime reaped after an observed exit (PID %s): returncode=%s", self._pid, rc)

    def _mark_dead(
        self, reason: str, *, expected: bool = False, stdin_stalled: bool = False
    ) -> None:
        """Mark runtime dead, fail all pending requests, poison all session queues.

        ``stdin_stalled`` records that the death is a stdin stall with the child
        still alive (see ``stdin_stall_death``), BEFORE the queues are poisoned,
        so every session woken by the poison reads it.

        ``expected`` selects only the log severity: a deliberate teardown (a
        warm-pool TTL recycle, a session shutdown) logs at INFO, while every
        genuine death path (process exit, reader crash, broken pipe, ...) keeps
        today's WARNING. The default is False so any death path added later is
        a WARNING without having to opt in. Everything else — the ``_dead``
        early return, PID unshielding, failing pending futures, poisoning
        session queues — is identical on both paths.
        """
        if self._dead:
            return
        self._stdin_stall_death = stdin_stalled
        if stdin_stalled:
            self._stall_turn_sessions = frozenset(self._turn_active_sessions)
        # Read co-tenancy BEFORE the flag flips. Both readings go through
        # liveness -- ``outstanding_leases`` excludes a dead runtime's leases by
        # design, and ``is_alive()`` consults ``_dead`` -- so a count taken after
        # the assignment below is zero for every death, which would report every
        # shared process as single-tenant and defeat the attribution entirely.
        _leases_at_death = outstanding_leases(self)
        _acp_sessions_at_death = len(self._session_queues)
        self._dead = True
        # A process that already exited on its own is a genuine death being
        # reaped, not a teardown this caller initiated — refuse the downgrade
        # regardless of call site. This closes the race where a replacement
        # path observes is_alive() == False (returncode set by the child
        # watcher) and kill()s before the reader loop has marked the death.
        if expected and self._process is not None and self._process.returncode is not None:
            expected = False
        self._death_expected = expected
        # Release the sweep-protection shield on ANY death path (EOF, rc!=0,
        # stdout overrun, reader crash, broken pipe) — not just kill(). Otherwise
        # the dead PID lingers in _PROTECTED_PIDS forever and, after PID reuse,
        # could shield a genuinely-orphaned process from the orphan sweep.
        if self._pid:
            try:
                unregister_protected_pid(self._pid)
            except Exception:
                logger.debug(
                    "AcpRuntime: unregister protected pid failed for %s", self._pid, exc_info=True
                )
        # Diagnostic context: process returncode + tail of captured stderr so
        # operators can tell an OOM/crash from a clean exit without DEBUG logs.
        rc = self._returncode_label()
        if self.recording_allowed:
            tail = " | ".join(self._stderr_lines[-5:]) if self._stderr_lines else "<none>"
        else:
            # Reader exceptions and child stderr can echo session content.
            # Restricted runs retain lifecycle facts, including the exit code.
            reason = "runtime stopped" if expected else "runtime failed"
            tail = "<not retained>"
            self._stderr_lines.clear()
        # Redact BEFORE composing: the summary outlives this method — it is
        # retained for death_summary(), appended to AcpProcessDied, and a
        # cron turn's failure stringifies that exception into job.last_error,
        # which persists to sandbox-visible crons.json. Child stderr is
        # external-subprocess output that can carry credential material
        # (same treatment as the send-path's 'ACP process exited' detail).
        tail, _ = redact_exfiltration_urls(tail)
        tail, _ = redact_credentials(tail)
        # Retain the summary for death_summary(): consumers that learn of the
        # death only through a poisoned queue (a live turn's frame wait) can
        # then attach WHO/WHY to their own error instead of raising bare.
        self._death_label = rc
        self._compose_death_summary(reason, rc, tail)
        # Classify the death ONCE, here, where it is detected -- and before the
        # pending futures are failed and the queues poisoned just below, so a
        # tenant woken by its poison sentinel can already read why. Every tenant
        # then reads this one record instead of forming its own account of a
        # process event it only saw one session's corner of.
        runtime_death.announce(
            self,
            reason=reason,
            expected=expected,
            leases=_leases_at_death,
            acp_sessions=_acp_sessions_at_death,
        )
        log = logger.info if expected else logger.warning
        log(
            "AcpRuntime dead (PID %s): %s [returncode=%s] stderr_tail: %s",
            self._pid,
            reason,
            rc,
            tail,
        )

        exc = AcpRuntimeDead(reason)
        for future in self._pending_requests.values():
            if not future.done():
                future.set_exception(exc)
        self._pending_requests.clear()
        self._pending_init_notifications.clear()
        # Also drop routed-request correlations: on death no reader will pop
        # them, and if a session is never destroyed the entry would otherwise
        # linger. unregister_session() also sweeps these per-session; this is
        # belt-and-suspenders for the process-death-before-response case.
        self._routed_requests.clear()

        for queue in list(self._session_queues.values()):
            try:
                queue.put_nowait(None)  # poison sentinel
            except asyncio.QueueFull:
                pass

    async def _reap_after_cancelled_tracking(self, tracking: asyncio.Future[None]) -> None:
        """Let the tracking worker finish, then reap the child. Never raises.

        Run as its own task by the spawn cancellation guard, which shields it,
        so a cancellation aimed at the spawn cannot interrupt either step.
        ``asyncio.wait`` neither cancels *tracking* nor raises its exception; its
        outcome does not matter here, only that it is over before the untrack
        the reap performs.
        """
        await asyncio.wait({tracking})
        try:
            await self.kill(reason="reap after cancelled spawn tracking")
        except Exception:
            logger.debug(
                "AcpRuntime: kill after cancelled PID tracking failed for %s",
                self._pid,
                exc_info=True,
            )

    async def _retire_tracking_after_exit(self) -> None:
        """Drop this root's registry entries once its exit is CONFIRMED, not inferred.

        Called from the two death paths that observe an exit instead of causing
        one: the reader loop's EOF branch, and the ``_exit_watch_loop`` that
        polls ``process.returncode`` so a death with stdout held open by a
        survivor is still seen. ``_kill_inner`` untracks after it
        reaps; a root killed from outside (an OOM kill, a ``pkill``, an operator)
        reached no such step, so its ``kiro_session_pids.txt`` / ``kiro_pids.txt``
        lines survived until the periodic sweep's next tick, bounded only by
        ``SessionCleanup.MAX_TICK_INTERVAL_SECS`` (300s). A registry line naming a
        dead pid is the hazard the start token exists to blunt, not one to leave
        standing for five minutes when the process that owns the line has already
        watched it die.

        A closed stdout is NOT an exit: a backend can close its pipe and keep
        running, and untracking a live process would hide it from every reaper
        for the host's uptime. So the exit is waited for, bounded by the same
        window the kill path gives a SIGKILLed child to be reaped, and the pid is
        re-probed after the wait. Any doubt -- the wait timed out, the pid still
        answers, the probe raised -- RETAINS the entries, which is the direction
        every reaper in ``session_pid`` already fails toward.

        The write itself is bound to the process, not the number: once the root
        is gone its number is free, and a replacement root this gateway spawns
        can be handed it before the write lands, so a prefix-matched untrack
        could erase the successor's only durable record. ``_untrack_root_by_identity``
        removes the session line only when its recorded start token is the one
        read at THIS spawn, and the bare root line only when the number is dead
        at the moment of a probe taken under the bare file's own lock -- a live
        holder of the number keeps its line. The root's own lines only: a
        descendant that outlived the root is reparented and still running, and
        its own ``kiro_pids.txt`` line is what the sweep reaps it by.

        Offloaded to a thread because the untrack takes the file locks the sweep
        contends for, and this runs on the event loop.
        """
        process = self._process
        if process is None:
            return
        pid = process.pid
        try:
            if process.returncode is None:
                await asyncio.wait_for(process.wait(), timeout=self._KILL_REAP_TIMEOUT)
        except asyncio.TimeoutError:
            logger.debug(
                "AcpRuntime: stdout closed but PID %s has not exited within %.1fs; "
                "leaving it tracked for the sweep",
                pid,
                self._KILL_REAP_TIMEOUT,
            )
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug(
                "AcpRuntime: could not confirm the exit of PID %s; leaving it tracked",
                pid,
                exc_info=True,
            )
            return
        # The wait just measured the status ``_mark_dead`` could not know (it ran
        # on EOF, before the reap): amend the retained summary the same way the
        # kill path does, so the ``AcpProcessDied`` a turn or a cron records
        # carries the real code instead of ``<not reaped>``. Worded as an
        # OBSERVED exit: nothing here issued a kill, and a log line that says
        # "killed" without naming who killed would defeat the attribution the
        # kill path's own docstring requires.
        self._note_reaped(process.returncode, after_kill=False)
        if platform_compat.pid_exists(pid):
            return
        try:
            retired = await asyncio.to_thread(
                _untrack_root_by_identity, pid, self._spawn_start_token
            )
        except Exception:
            logger.debug("AcpRuntime: PID untracking after exit failed for %s", pid, exc_info=True)
            return
        if not retired:
            logger.debug(
                "AcpRuntime: registry lines for PID %s were not provably this runtime's; "
                "leaving them for the sweep",
                pid,
            )
            return
        logger.info("AcpRuntime: retired registry tracking for PID %d after it exited", pid)

    # ── Protocol Interface (used by AcpSessionHandle) ──

    async def send_request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        on_reserved: "Callable[[int], None] | None" = None,
    ) -> int:
        """Send a JSON-RPC request and return the request id.

        The response will be routed to the session's queue (via _routed_requests)
        so AcpSessionHandle can detect turn completion. For requests that need
        an immediate response (init, session/new), use _send_and_await instead.

        ``on_reserved`` is called with the id BEFORE the write, whose ``drain()``
        can suspend: the response can reach the session queue during that
        suspension, so a caller that must claim it registers the id by then.
        """
        if not self._process or not self._process.stdin:
            raise AcpRuntimeDead("process not running")
        if self._dead:
            raise AcpRuntimeDead("runtime is dead")

        projection = getattr(self, "_native_skill_projection", None)
        if projection is not None:
            params = projection.request(method, params)
        req_id = self._next_id
        self._next_id += 1

        # Register for session routing so the response goes to the right queue
        session_id = params.get("sessionId")
        if session_id and session_id in self._session_queues:
            self._routed_requests[req_id] = session_id
        if on_reserved is not None:
            on_reserved(req_id)

        req = JsonRpcRequest(method=method, params=params, id=req_id)
        data = encode_frame(req.to_dict())

        try:
            # Bounded on the reader's PROGRESS, not held across a raw drain: on
            # the shared stdin a request write that parks while kiro-cli is
            # flow-control-paused must not hold the write lock and wedge every
            # other session. On a stall _write_request_bounded marks the runtime
            # dead and raises AcpRuntimeStdinStalled (an AcpRuntimeDead) -- caught
            # below so the routing registration is dropped like any other death.
            await self._write_request_bounded(data, req_id, method)
        except AcpRuntimeDead:
            self._routed_requests.pop(req_id, None)
            raise
        except (BrokenPipeError, ConnectionResetError) as exc:
            self._routed_requests.pop(req_id, None)
            self._mark_dead(f"pipe broken: {exc}")
            raise AcpRuntimeDead(f"pipe broken: {exc}") from exc

        self._last_activity = time.monotonic()
        return req_id

    async def send_notification(self, method: str, params: dict[str, Any]) -> None:
        """Send a JSON-RPC notification (no id, no response expected).

        Unlike send_request, this does NOT allocate an id or register routing,
        so it leaves no _routed_requests entry to leak when the server (per the
        ACP spec) sends no response back (e.g. session/cancel).
        """
        if not self._process or not self._process.stdin:
            raise AcpRuntimeDead("process not running")
        if self._dead:
            raise AcpRuntimeDead("runtime is dead")

        msg = {"jsonrpc": "2.0", "method": method, "params": params}
        data = encode_frame(msg)

        try:
            # A notification (session/cancel) is fire-and-forget and is the one
            # signal that can end a wedged turn: wait for the lock under the
            # no-progress bound, append unlocked if it does not come, and never
            # park on a drain behind a reader that stopped.
            outcome = await write_notification_best_effort(
                self._process.stdin,
                self._stdin_write_lock(),
                data,
                bound_secs=_RESPONSE_WRITE_BOUND_SECS,
                before_write=self._refuse_write_if_dead,
            )
        except (BrokenPipeError, ConnectionResetError) as exc:
            self._mark_dead(f"pipe broken: {exc}")
            raise AcpRuntimeDead(f"pipe broken: {exc}") from exc

        # Only a drained frame is evidence the backend moved: an unlocked append
        # or a stall must not refresh the activity clock the idle/wedged-turn
        # probes read -- that would defer the probe at the very moment a failed
        # write showed the backend is not reading.
        if outcome == "drained":
            self._last_activity = time.monotonic()
        else:
            logger.debug(
                "send_notification method=%s: %s; activity clock not refreshed",
                _loggable_request_id(method),
                outcome,
            )

    def _refuse_write_if_dead(self) -> None:
        """Re-check under the write lock, right before a frame goes out.

        A writer that queued for the lock while a sibling's stall marked the
        runtime dead must not write into the pipe afterwards: ``_mark_dead``
        has already failed every pending request and poisoned every session
        queue, and the recovery path requeues the turn -- a prompt written now
        would run a second time. Every runtime writer calls this immediately
        before writing: under the lock when it holds one, and on the unlocked
        best-effort notification append as well.
        """
        if self._dead:
            raise AcpRuntimeDead("runtime is dead")

    def _stdin_write_lock(self) -> asyncio.Lock:
        """The one lock every stdin write on this runtime takes (see
        ``await_under_no_progress_bound`` in transport_framing for why the bound needs
        it). Created on first use so a runtime built without ``__init__`` (test
        doubles) has one."""
        lock = getattr(self, "_stdin_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            self._stdin_lock = lock
        return lock

    # ── Skill-view projection: one preparation at a time, newest view wins ──
    #
    # Every session start re-prepares the native skill projection and sends the
    # alias it published, and starts on one runtime overlap. A preparation reads
    # the agent specs BEFORE it waits on the cross-process alias lock, so without
    # ordering a start that read the spec before an edit revoked a grant could
    # return last, become ``_native_skill_projection`` and activate that alias.
    #
    # Two mechanisms close it, and each needs the other:
    #
    # * ``_skill_projection_lock`` serializes preparation AND adoption per runtime,
    #   so preparations read the specs in the order they are adopted. It is held
    #   only across the ``to_thread`` preparation and the in-memory adoption,
    #   never across ``session/set_mode`` (which can take a whole handshake budget
    #   while a switched-to MCP server boots), and never across blocking I/O on
    #   the loop: the file I/O runs in the worker thread.
    # * a generation, issued under the lock before the read, stamps each adopted
    #   projection. Adoption refuses one older than the adopted generation, and a
    #   start whose ``set_mode`` is still pending re-checks it before every attempt
    #   and after the answer: a newer view adopted meanwhile is the one it uses, or,
    #   once the host has already answered for the older alias, the start fails.
    #   The after-answer check first takes the lock itself, so a preparation that
    #   was still running when the answer arrived is adopted before it is judged.
    #   A preparation runs to completion even when its start is cancelled, and its
    #   view is adopted before the cancellation propagates; one that yields no
    #   view (or raises) is recorded as unadopted, and until a later view is
    #   adopted a pending start cannot prove its view current and fails.
    #   The lock is what makes the counter mean "read later": issued outside it, a
    #   later generation could belong to a thread that read the spec first.
    #
    # Lock order (outermost first): ``_skill_projection_lock`` (asyncio, this
    # runtime) -> the alias file lock ``_projection_alias_lock`` (flock, taken in
    # the worker thread) -> the workspace ``cli.json`` settings lock -> the
    # projection lease. Nothing that holds a file lock ever awaits the asyncio
    # lock: the file locks are taken and released inside one synchronous call on a
    # worker thread, so the thread cannot wait on the loop, and the loop never
    # blocks on a file lock. ``_stdin_write_lock`` is never taken while this lock
    # is held, because the send happens after adoption releases it, and the
    # after-answer check takes this lock with no other lock held.

    def _skill_projection_lock(self) -> asyncio.Lock:
        """The per-runtime lock around skill-view preparation and adoption.

        Created on first use, like :meth:`_stdin_write_lock`, so a runtime built
        without ``__init__`` (test doubles) has one."""
        lock = getattr(self, "_skill_projection_lock_obj", None)
        if lock is None:
            lock = asyncio.Lock()
            self._skill_projection_lock_obj = lock
        return lock

    def _issue_skill_projection_generation(self) -> int:
        """The next preparation generation. Call with the projection lock held."""
        generation = int(getattr(self, "_skill_projection_issued", 0)) + 1
        self._skill_projection_issued = generation
        return generation

    def _adopted_skill_projection_generation(self) -> int:
        return int(getattr(self, "_skill_projection_generation", 0))

    def _note_unadopted_skill_projection(self, generation: int) -> None:
        """Record that preparation *generation* ended without being adopted.

        It may have read an edit no adopted view reflects (it returned no view, or
        raised), so until a later preparation is adopted a pending start cannot
        prove its view is current. Call with the projection lock held."""
        if generation > self._unadopted_skill_projection_generation():
            self._skill_projection_unadopted = generation

    def _unadopted_skill_projection_generation(self) -> int:
        return int(getattr(self, "_skill_projection_unadopted", 0))

    def _adopt_skill_projection(self, prepared: Any, generation: int) -> bool:
        """Make *prepared* this runtime's projection unless a newer one was adopted.

        Call with the projection lock held. Returns ``False`` -- and changes
        nothing -- for a preparation older than (or as old as) the adopted one."""
        if generation <= self._adopted_skill_projection_generation():
            return False
        self._native_skill_projection = prepared
        self._skill_projection_generation = generation
        return True

    async def _write_response_bounded(
        self,
        data: bytes,
        request_id: str | int,
        *,
        before_write: Callable[[], None] | None = None,
        withdrawal_check: Callable[[], None] | None = None,
    ) -> None:
        """Write a response/error frame under the write lock and a no-progress bound.

        Mirror of ``AcpClient._write_response_bounded`` for the shared runtime.
        Every multiplexed session shares this one stdin, so a response can wait
        behind another session's multi-MB prompt frame on a flow-control-paused
        writer while the backend is perfectly healthy; the shared helper only
        gives up when the write buffer has not shrunk for
        ``_RESPONSE_WRITE_BOUND_SECS`` -- nothing consumed, reader gone -- which
        on a shared pipe is a dead runtime, not a slow session. Marks the runtime
        dead (resolving every pending wait by teardown) and raises
        ``AcpRuntimeDead``, which the session provider translates to
        ``AcpProcessDied`` so the caller takes the same session-reset +
        bounded-requeue recovery the broken-pipe case uses. The request id
        appears only through ``_loggable_request_id``. A caller's
        ``before_write`` runs under the lock after the dead-runtime check, and
        its ``withdrawal_check`` ahead of it (see :meth:`send_response`).
        """
        assert self._process is not None and self._process.stdin is not None

        def checks() -> None:
            # A frame its caller withdrew is not written, whatever the pipe's
            # state; the caller's other check is read on a live runtime only, so
            # a runtime that died while this writer waited reports the death.
            if withdrawal_check is not None:
                withdrawal_check()
            self._refuse_write_if_dead()
            if before_write is not None:
                before_write()

        if await write_response_frame_bounded(
            self._process.stdin,
            self._stdin_write_lock(),
            data,
            bound_secs=_RESPONSE_WRITE_BOUND_SECS,
            before_write=checks,
        ):
            return
        safe_id = _loggable_request_id(request_id)
        window = response_write_window_secs(self._process.stdin, _RESPONSE_WRITE_BOUND_SECS)
        logger.warning(
            "ACP runtime stdin stalled: %s "
            "while delivering response to req=%s; marking runtime dead",
            _stall_window_phrase(self._process.stdin, window),
            safe_id,
        )
        self._mark_dead("response write stalled (backend not reading stdin)", stdin_stalled=True)
        raise AcpRuntimeStdinStalled(
            f"stdin stalled: no write progress for {window:g}s while "
            f"delivering response to req={safe_id}"
        )

    async def _write_request_bounded(self, data: bytes, request_id: int, method: str) -> None:
        """Write a REQUEST frame under the write lock and the same no-progress bound.

        The request twin of :meth:`_write_response_bounded`, for the same
        shared-runtime hazard: one stdin serves every multiplexed session, so a
        request write (``session/prompt``, ``session/new``, ``set_mode``,
        ``_session/steering``) that holds the lock across an unbounded ``drain()``
        parks forever WITH the lock held whenever kiro-cli is flow-control-paused
        -- busy generating on one lane and not reading stdin, the state right
        after ``spawn_run`` fans several session prompts onto the one pipe. Every
        other session's stdin write then queues behind that lock at 0 CPU while
        the busy lane keeps streaming stdout and looks healthy. The bound is on
        PROGRESS, not elapsed time, so a caller-sized frame (a prompt may carry
        any number of image blocks) stays legal: a reader still consuming keeps
        the wait alive, and only a writer whose buffer has not shrunk for
        ``_RESPONSE_WRITE_BOUND_SECS`` is the reader-gone stall.

        Both phases mark the runtime dead and raise
        :class:`AcpRuntimeStdinStalled`, which the session provider translates to
        ``AcpProcessDied`` so the caller takes the ownership-authorized session-
        reset recovery the broken-pipe case uses. This path does NOT kill the
        child: a ``DRAIN_STALL`` can leave the frame buffered in a kiro-cli that
        resumes, but killing cannot be guaranteed (a close/EOF keeps flushing the
        buffered bytes and the wedged child is the one that will not exit in a kill
        grace, so death is unconfirmed before the raise) and, on this shared
        runtime, signalling the child from a write path bypasses the
        ``authorize_runtime_kill`` ownership gate and teardown barrier the only
        sanctioned teardown takes, terminating sibling sessions' work unauthorized.
        Instead a ``DRAIN_STALL`` raises with ``ambiguous_delivery`` set, which
        rides through ``_translate_dead`` into ``build_recovery_requeue`` and makes
        the recovery resume from restored state rather than replay the prompt the
        paused backend may have consumed; a ``LOCK_STALL`` wrote no byte, so it is
        not ambiguous and its replay is safe. The method name and request id appear
        only through ``_loggable_request_id``.
        """
        assert self._process is not None and self._process.stdin is not None
        result = await write_request_frame_bounded(
            self._process.stdin,
            self._stdin_write_lock(),
            data,
            bound_secs=_RESPONSE_WRITE_BOUND_SECS,
            before_write=self._refuse_write_if_dead,
        )
        if result is RequestWriteResult.OK:
            return
        safe_id = _loggable_request_id(request_id)
        window = response_write_window_secs(self._process.stdin, _RESPONSE_WRITE_BOUND_SECS)
        logger.warning(
            "ACP runtime stdin stalled: %s while sending request method=%s req=%s; "
            "marking runtime dead (%s)",
            _stall_window_phrase(self._process.stdin, window),
            _loggable_request_id(method),
            safe_id,
            # Which phase stalled, for the log only -- recovery is the same either
            # way (see below).
            (
                "frame never written"
                if result is RequestWriteResult.LOCK_STALL
                else "frame already buffered"
            ),
        )
        # Both phases mark the runtime dead and raise -- no direct child kill from
        # here. On the shared runtime the child serves every multiplexed session,
        # and signalling it from a write path bypasses the ownership
        # authorization + teardown barrier (authorize_runtime_kill) that the only
        # sanctioned teardown takes, so it would terminate sibling sessions' work
        # unauthorized. Instead the two phases differ ONLY in the recovery hint
        # they carry: a LOCK_STALL wrote no byte, so the recovery's prompt replay
        # is the frame's first and only delivery (replay safe); a DRAIN_STALL left
        # the frame in the transport, which a kiro-cli that merely paused reading
        # could still consume -- so it is flagged ambiguous_delivery, and the
        # recovery resumes from restored state rather than replaying the prompt
        # verbatim (which would run its tools twice). The flag rides the stall
        # through _translate_dead onto AcpProcessDied; the authorized teardown
        # reaps the old child through the ownership path.
        drain_stall = result is RequestWriteResult.DRAIN_STALL
        self._mark_dead(
            (
                "request write stalled after the frame was buffered"
                if drain_stall
                else "request write stalled waiting for stdin lock"
            ),
            stdin_stalled=True,
        )
        raise AcpRuntimeStdinStalled(
            f"stdin stalled: no write progress for {window:g}s while "
            f"sending request req={safe_id}",
            ambiguous_delivery=drain_stall,
        )

    async def send_request_for_answer(
        self,
        method: str,
        params: dict[str, Any],
        on_registered: "Callable[[asyncio.Future[dict[str, Any]]], None] | None" = None,
    ) -> "asyncio.Future[dict[str, Any]]":
        """Write a request and return a future for its answer, off the session queue.

        For a request a session sends WHILE its own turn is streaming, whose answer
        the sender needs. :meth:`send_request` routes the answer into the session's
        queue, where the turn's dispatch loop is the only reader, so waiting for it
        there would mean pulling the turn's frames out from under that loop.
        :meth:`_send_and_await` resolves off the queue but owns the wait and drops
        the registration when it times out. This registers the future the same way
        (``_pending_requests``, which the reader resolves before any routing) and
        hands it back, so the caller can bound the wait and still observe an answer
        that arrives after the bound.

        The future carries the answer's ``result`` object, or an
        :class:`AcpRuntimeError` for an error answer, or :class:`AcpRuntimeDead`
        if the process dies first -- the same three outcomes ``_send_and_await``
        gives.

        ``on_registered`` is called with the future BEFORE the write, whose
        ``drain()`` can suspend: the reader may resolve the future during that
        suspension, so a caller that must see the answer in order with the frames
        after it has to hold the future by then.
        """
        if not self._process or not self._process.stdin:
            raise AcpRuntimeDead("process not running")
        if self._dead:
            raise AcpRuntimeDead("runtime is dead")

        req_id = self._next_id
        self._next_id += 1
        projection = getattr(self, "_native_skill_projection", None)
        if projection is not None:
            params = projection.request(method, params)
        req = JsonRpcRequest(method=method, params=params, id=req_id)
        data = encode_frame(req.to_dict())

        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending_requests[req_id] = future
        if on_registered is not None:
            on_registered(future)
        try:
            # Bounded like send_request: a raw drain here would hold the
            # shared write lock against a flow-control-paused kiro-cli. A
            # stall marks the runtime dead and raises AcpRuntimeStdinStalled (an
            # AcpRuntimeDead), caught below so the pending future is retrieved or
            # cancelled rather than left unretrieved.
            await self._write_request_bounded(data, req_id, method)
        except AcpRuntimeDead:
            # Reached two ways, both of which have already resolved the future
            # before the raise, so retrieve or cancel it here only to stop
            # asyncio logging a handled error as unhandled: _refuse_write_if_dead
            # (a sibling stall marked the runtime dead while this caller held the
            # lock wait), or _write_request_bounded's own stall paths -- both of
            # which call _mark_dead and fail this pending future first.
            self._pending_requests.pop(req_id, None)
            if future.done():
                if not future.cancelled():
                    future.exception()
            else:
                future.cancel()
            raise
        except (BrokenPipeError, ConnectionResetError) as exc:
            self._pending_requests.pop(req_id, None)
            self._mark_dead(f"pipe broken: {exc}")
            raise AcpRuntimeDead(f"pipe broken: {exc}") from exc

        self._last_activity = time.monotonic()
        return future

    def forget_request(self, future: "asyncio.Future[dict[str, Any]]") -> None:
        """Stop waiting for a :meth:`send_request_for_answer` answer.

        Drops the registration so an answer that never comes holds nothing, and
        cancels the future so any callback on it runs once. A no-op when the answer
        already arrived or the runtime has died (both already dropped it).
        """
        for req_id, pending in list(self._pending_requests.items()):
            if pending is future:
                self._pending_requests.pop(req_id, None)
                break
        if not future.done():
            future.cancel()

    async def send_response(
        self,
        request_id: str | int,
        result: dict[str, Any],
        *,
        before_write: Callable[[], None] | None = None,
        withdrawal_check: Callable[[], None] | None = None,
    ) -> None:
        """Send a JSON-RPC response (for server→client requests like permission).

        ``before_write`` runs under the write lock right before the frame is
        written, after the dead-runtime check, and may raise to abort the write
        with nothing written. ``withdrawal_check`` runs under the same lock ahead
        of the dead-runtime check and may raise the same way: a frame its caller
        withdrew is not written, whatever the pipe's state.
        """
        if not self._process or not self._process.stdin:
            raise AcpRuntimeDead("process not running")
        if self._dead:
            raise AcpRuntimeDead("runtime is dead")

        msg = {"jsonrpc": "2.0", "id": request_id, "result": result}
        data = encode_frame(msg)

        try:
            await self._write_response_bounded(
                data,
                request_id,
                before_write=before_write,
                withdrawal_check=withdrawal_check,
            )
        except (BrokenPipeError, ConnectionResetError) as exc:
            self._mark_dead(f"pipe broken: {exc}")
            raise AcpRuntimeDead(f"pipe broken: {exc}") from exc

    async def send_error(self, request_id: str | int, code: int, message: str) -> None:
        """Send a JSON-RPC error response."""
        if not self._process or not self._process.stdin:
            raise AcpRuntimeDead("process not running")
        if self._dead:
            raise AcpRuntimeDead("runtime is dead")

        msg = {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
        data = encode_frame(msg)

        try:
            await self._write_response_bounded(data, request_id)
        except (BrokenPipeError, ConnectionResetError) as exc:
            self._mark_dead(f"pipe broken: {exc}")
            raise AcpRuntimeDead(f"pipe broken: {exc}") from exc

    def unregister_session(self, session_id: str) -> None:
        """Unregister a session queue (called by AcpSessionHandle.destroy)."""
        self._session_queues.pop(session_id, None)
        self._switched_model_record().pop(session_id, None)
        # Clean up any pending routed requests for this session
        stale = [k for k, v in self._routed_requests.items() if v == session_id]
        for k in stale:
            del self._routed_requests[k]
        # The departing session takes its subagent ownership with it: a later
        # session on this warm runtime must never inherit a stale child's
        # approvals (the announce set is re-learned from the next
        # subagent/list_update, which re-establishes ownership explicitly).
        self._turn_active_sessions.discard(session_id)
        if self._subagent_owner == session_id:
            self._subagent_owner = None
            self._subagent_sessions = set()
            # The overflow count describes the set that just went away, so it
            # travels with it: a stale non-zero value would keep auditing later
            # unknown-session denials as cap truncations on a runtime whose
            # roster is empty.
            self._subagent_roster_overflow = 0
            # Same boundary for the log: the departing roster's truncation
            # episode is over, so its residual repeat count is flushed and the
            # next owner's first truncation is loud again.
            self._end_roster_overflow_episode()
        logger.debug("Removed session %s", session_id)

    def mark_turn_active(self, session_id: str, active: bool) -> None:
        """Record whether a session's prompt dispatch loop is consuming.

        Called by ``AcpSessionHandle.prompt()`` on entry and (in a finally)
        on every exit path. Child permission requests are routed only while
        the owner is marked active; otherwise they are answered fail-closed
        immediately, because nothing reads the queue until the next turn.
        """
        if active:
            self._turn_active_sessions.add(session_id)
        else:
            self._turn_active_sessions.discard(session_id)

    async def terminate_session(self, session_id: str) -> None:
        """Evict a session from kiro-cli (freeing its memory), then unregister locally.

        Sends the ``_kiro.dev/session/terminate`` request so the multiplexed
        kiro-cli process drops this session from its in-memory session map and
        shuts the session's agent down (reaping its MCP child processes). WITHOUT
        this, a finished session's transcript + context stay resident in the
        shared process for its entire lifetime — so RSS grows without bound as
        background tasks and subagents accumulate (the multiplexed design has no
        per-turn compaction, so per-session eviction is the only reclaim signal).

        This is co-tenant-safe: it targets exactly one ``sessionId`` and never
        touches the process, unlike ``kill()`` (which would take every sibling
        session down with it).

        Best-effort and bounded: teardown must never hang or raise. If the
        runtime is already dead the session's memory died with the process, so
        the round-trip is skipped. The local ``unregister_session`` ALWAYS runs
        (``finally``) so the reader loop stops routing to an abandoned queue even
        when the terminate request could not be delivered — including when the
        enclosing task is cancelled mid-await (``asyncio.CancelledError`` is a
        ``BaseException``, so it would otherwise slip past the ``except Exception``).

        The DELIVERY comes from the harness's own ``TeardownPolicy``, not from one
        shape applied to every host: a verb the host answers is a request bounded by
        ``_TERMINATE_TIMEOUT``, and a verb it does not answer is a notification. The
        distinction is invisible in the method name, and getting it wrong is expensive
        in the direction that looks like nothing is wrong — the eviction still happens,
        just a whole budget later, with a timeout logged against it.
        """
        try:
            if not self._dead and self._process is not None:
                policy = self._harness.teardown
                try:
                    if policy.notification:
                        # A verb the host does not answer. Awaiting a reply here would
                        # spend the whole teardown budget on every eviction and report
                        # it as a control-plane timeout, so the delivery follows what
                        # the harness declares rather than one shape for every host.
                        await self.send_notification(policy.method, {"sessionId": session_id})
                    else:
                        await self._send_and_await(
                            policy.method,
                            {"sessionId": session_id},
                            timeout=_TERMINATE_TIMEOUT,
                        )
                except Exception:
                    logger.debug(
                        "session teardown failed for %s (runtime dead/slow); "
                        "proceeding with local unregister",
                        session_id,
                        exc_info=True,
                    )
        finally:
            self.unregister_session(session_id)

    def _session_teardown_method(self) -> str:
        """The verb that frees one session on this backend.

        kiro-cli's terminate evicts the session from the process and leaves the
        transcript on disk for the caller to deal with. KAS offers no evict-only
        equivalent, so its delete does both at once.

        That difference is invisible here but matters to the caller: on KAS the
        local ``AcpSessionHandle._cleanup_transcript`` is a NO-OP, because it
        unlinks from kiro-cli's sessions dir and KAS keeps its own store. So the
        ``keep_transcript`` guard does not protect anything on KAS — a KAS
        session's record is gone once this verb returns. The only capability that
        loses is opportunistic ``spawn_continue`` on a shared subagent, which
        degrades to a typed ``conversation_gone`` and a re-spawn; explicitly
        continuable runs are dedicated sessions that never reach this path.
        """
        return self._harness.teardown.method

    # ── Session Management ──

    def _mcp_init_progress(self, expected: Any, *, queued_behind: bool = False) -> str:
        """Describe MCP registration progress for a session start that stalled.

        Reads the frames the reader loop already staged in
        ``_pending_init_notifications`` so a session-start timeout can name the
        servers that never reported, instead of reporting only the elapsed
        budget. Must run BEFORE ``_finish_session_init``, which drops that
        buffer once the last in-flight init closes.

        ``expected`` is the ``mcpServers`` array sent with the request. Its
        entries carry the roster names, and that is what makes the ABSENT
        servers nameable rather than only the present ones.

        The text says what that roster IS. On kiro-cli the array holds only the
        broker stubs Kiro Crew injects (``pooled_session_servers``); the agent
        spec's own servers are started by the backend and are not in it, and
        the backend's session-start steps after MCP init are not observable
        from here at all. A bare ``4/4 MCP server(s) reported`` therefore read
        as "all MCP is up, so MCP is the problem" -- a field report was
        triaged that way on the strength of the suffix alone -- when it only
        ever meant that the four injected servers had spoken. The count is
        now labelled ``session-injected``, and a complete roster is followed
        by what it does and does not cover, so a reader is not sent to chase
        MCP for a stall that is past it.

        Reports are runtime-wide rather than per-session: a request that never
        answered has no session id to match its frames against, so a concurrent
        init is called out in the text instead of being silently folded in. What
        keeps that bounded to the CONCURRENT inits is that a start whose caller
        already gave up keeps its own frames on its :class:`StartCollector`
        instead of here — un-keyed by name, they would otherwise be read as this
        attempt's progress and hide the servers that never reported.
        Likewise the staging deque is bounded, so on a very large fleet the
        reported count is a floor, not an exact tally.
        """
        roster = [
            _sanitize_progress_name(str(e.get("name") or ""))
            for e in (expected if isinstance(expected, list) else [])
            if isinstance(e, dict) and e.get("name")
        ]
        ready: list[str] = []
        failed: list[str] = []
        failure_text: dict[str, str] = {}
        awaiting_auth: list[str] = []
        for msg in self._pending_init_notifications:
            params = msg.params if isinstance(msg.params, dict) else {}
            name = _sanitize_progress_name(
                str(params.get("serverName") or params.get("name") or "")
            )
            if not name:
                continue
            if msg.is_method(METHOD_MCP_SERVER_INITIALIZED):
                if name not in ready:
                    ready.append(name)
            elif msg.is_method(METHOD_MCP_SERVER_INIT_FAILURE):
                # A failed server's error text can carry connection strings or
                # tokens from its startup, so it takes the same scrub the
                # dashboard banner applies before it lands in an exception.
                err, _ = redact_exfiltration_urls(str(params.get("error") or ""))
                err, _ = redact_credentials(err)
                err = _strip_unprintable(" ".join(err.split()))[:_MCP_PROGRESS_ERROR_CAP]
                if name not in failed:
                    failed.append(name)
                if err:
                    failure_text[name] = err
            elif msg.is_method(METHOD_MCP_OAUTH_REQUEST):
                if name not in awaiting_auth:
                    awaiting_auth.append(name)

        reported = set(ready) | set(failed)
        parts: list[str] = []
        if roster:
            # Count only reports that belong to the roster. kiro-cli initializes
            # the agent spec's own servers as well as the session-injected ones,
            # so the staged frames are a SUPERSET of the roster and a raw
            # len(reported) can exceed the denominator -- "2/1 reported". The
            # out-of-roster servers still appear by name in the failed and
            # awaiting-authorization buckets, where naming them is the point.
            parts.append(
                f"{len(reported & set(roster))}/{len(roster)} session-injected "
                "MCP server(s) reported"
            )
            silent = [n for n in roster if n not in reported]
            if silent:
                parts.append(f"no report from {_capped_names(silent)}")
            elif not set(failed) & set(roster) and not queued_behind:
                # Every roster member reported READY. A member that reported an
                # init failure counts as reported (so it is never chased as
                # silent) but is named under ``failed:`` below, and the stall
                # may be in it -- so the "not in those servers" verdict is
                # withheld then. It is withheld too when the start was sent
                # behind unanswered requests: the budget may have gone to those
                # before this start ran at all, which is not "later in startup".
                parts.append(MCP_ROSTER_COMPLETE_NOTE)
        else:
            # No roster to attribute against: these reports belong to the agent
            # spec's own servers or to a concurrent start, so the count is not
            # labelled "session-injected" here.
            parts.append(f"{len(reported)} MCP server report(s), roster unknown")
        if failed:
            parts.append(
                "failed: "
                + _capped_names(
                    [f"{n} ({failure_text[n]})" if n in failure_text else n for n in failed]
                )
            )
        if awaiting_auth:
            parts.append(f"awaiting authorization: {_capped_names(awaiting_auth)}")
        if self._session_inits_in_flight > 1:
            parts.append(
                f"{self._session_inits_in_flight} session inits in flight, "
                "so these reports are runtime-wide"
            )
        return "; ".join(parts)

    def _session_start_stalled(
        self, exc: AcpRequestTimeout, method: str, expected: Any
    ) -> AcpRequestTimeout:
        """Attach MCP progress to a session-start timeout before it reaches the user.

        Session start is the one request whose cost is dominated by work the
        runtime can observe, so the bare budget is the least useful half of the
        answer. Returns a replacement to raise rather than raising here, so the
        caller keeps the ``from exc`` chain.
        """
        queued = _queued_behind_note(getattr(exc, "queued_behind", None) or [])
        progress = "; ".join(
            part
            for part in (queued, self._mcp_init_progress(expected, queued_behind=bool(queued)))
            if part
        )
        logger.warning("%s stalled: %s", method, progress or "no MCP reports staged")
        # Tag the exception the caller will raise as a SESSION-START failure,
        # whichever of the two it is: both reach a self-driving caller as "the
        # cycle never got a session", and the tag is how that caller counts the
        # streak without reading the message text.
        exc.session_start_failed = True
        if not progress:
            return exc
        replacement = AcpRequestTimeout(f"{exc} ({progress})")
        replacement.session_start_failed = True
        return replacement

    def _stage_init_frame(self, msg: JsonRpcMessage) -> None:
        """Hold one MCP-init frame until the session id that claims it is known.

        ``session/new`` can emit OAuth and MCP registration frames before its
        response, and the response is what gives ``create_session`` the id needed
        to register the queue. Registration frames matter beyond logging:
        ``drain_init()`` arms its idle shortcut on the first one, so dropping them
        here makes the session look report-less and pay the full no-report
        ceiling.

        Two holders, and a frame goes to both — the in-flight init scope's deque,
        which ``_mcp_init_progress`` also reads un-keyed, and every live
        :class:`StartCollector`, which owns a start whose id is still unknown.
        Neither can hand a frame to the other's session (both claim by the id in
        the frame, and two ``session/new`` answers never carry the same one), and
        both are bounded, so a frame nobody claims cannot accumulate.
        """
        if self._session_inits_in_flight:
            self._pending_init_notifications.append(msg)
        for collector in self._start_collectors.values():
            collector.stage_init_frame(msg)

    def _finish_session_init(self, session_id: str) -> list[JsonRpcMessage]:
        """Take staged init frames for one session and close its init scope."""
        matched, self._pending_init_notifications = _split_init_frames(
            self._pending_init_notifications, session_id
        )
        self._session_inits_in_flight -= 1
        if self._session_inits_in_flight == 0:
            # Anything unmatched belongs to a failed/abandoned init. Never let
            # it survive into the next session creation attempt.
            self._pending_init_notifications.clear()
        return matched

    def _activates_agent_by_mode(self) -> bool:
        """Whether ``session/set_mode`` names something this host can resolve.

        Read from ``ACP_BACKEND_ROUTING`` rather than declared here or asked of
        the harness: the hosts whose privileged tools are governed by an agent
        spec are exactly the hosts that HAVE an agent to activate, and a second
        copy of that membership would be free to disagree with the one that
        decides. kiro-cli and the KAS relay are that population.

        A host routed another way has no agent id to send. codex's modes are its
        own permission tiers (``read-only`` / ``agent``), so a Crew agent name
        resolves to nothing there and the request faults — taking down, on the
        cleanup path below, a session that had started fine.
        """
        return acp_tool_gate.routing_for(self.acp_backend) is acp_tool_gate.Routing.AGENT_SPEC

    @staticmethod
    def _mode_available(agent: str, resp: dict[str, Any]) -> bool:
        """Whether ``set_mode`` should be attempted for ``agent`` given a
        ``session/new``|``session/load`` response.

        True when the backend advertised no ``modes`` list at all (older kiro-cli
        / offline fake backend — attempt for backward compatibility) OR the agent
        is in the advertised ``availableModes``. False when a modes list WAS
        advertised (even an empty one) and the agent is absent — the case that
        would otherwise fault with ``-32603 "Mode '<agent>' not found"``. An
        explicitly-empty ``availableModes: []`` therefore fails closed, not open.
        """
        ids, _current, advertised = parse_session_modes(resp)
        if not advertised:
            return True
        return agent in ids

    async def _verify_spawn_agent_active(
        self,
        session_id: str,
        resp: dict[str, Any],
        *,
        override: str | None,
    ) -> None:
        """Fail closed when the agent the ``--agent`` flag selected never loaded.

        Guard (A2) — the spawn-flag half of Guard (A) in :meth:`create_session`.
        ``set_mode`` only ever activates an EXPLICIT override (or, on KAS, the
        injected default), so on kiro-cli the agent chosen by ``--agent`` — the
        agent of every ordinary session — reaches no availability check at all.
        It needs one, because that spec can fail to load silently: kiro-cli
        validates ``~/.kiro/agents/<agent>.json`` with ``deny_unknown_fields``
        and, on ANY unknown field, rejects the spec wholesale and runs its own
        default agent instead. :func:`kiro_crew.agent.migrate_agent_specs` exists
        to strip the two keys already known to trip it; nothing validates the
        rest, and a spec written by another tool (or by a product this install
        superseded) can carry more.

        Nothing downstream notices the substitution. The ``set_mode`` response is
        never read back, ``currentModeId`` is never re-compared, and
        :mod:`kiro_crew.acp.mcp_session_report` only LOGS — its own docstring
        forbids reading a missing report as "not mounted". The session then runs
        with NONE of Kiro Crew's control plane, while the global provider
        ``mcp.json`` that Kiro Crew pins off only on specs IT writes stays
        merged. So third-party MCP servers declared there keep working and every
        Kiro Crew tool the injected prompt names — ``learn_add`` among them —
        answers "does not exist", which the agent reports to the user as its
        memory being unavailable.

        Skipped when an override is requested: Guard (A) checks the agent
        ``set_mode`` will activate.

        ``currentModeId`` is read as PROOF, in both directions. A live probe of
        this backend settles what it means: a spec that loads is reported as the
        current mode AND listed in ``availableModes``, while a spec the backend
        refuses is absent from the list and ``currentModeId`` names the backend's
        own default instead. So a non-empty ``currentModeId`` naming something
        OTHER than the spawn agent is positive evidence of the substitution, and
        fails closed even when no advertised list came back. Treating a
        current-mode mismatch as an extra ADMIT is the hole this avoids: a
        ``currentModeId``-only response naming a substituted agent would otherwise
        sail through the compatibility escape.

        That escape is therefore narrow. It applies only when the response names
        NO current mode, where the advertised list is the sole signal and its
        absence is no evidence of a substitution -- so older kiro-cli and the
        offline fake backend behave exactly as before.

        Asked of the harness, which answers yes only for a host whose argv
        actually carries ``--agent``. Never phrased as an inequality: a negative
        test would silently capture every host added later (harness-parity H5),
        and a host that activates its agent by ``set_mode`` already has that
        response as proof -- on KAS the agent travels over the wire as an injected
        custom agent and Guard (A) covers it. Asked of the host rather than of
        "the KAS projection came back None" so the same call serves
        :meth:`load_session`, which never builds that projection.
        """
        if override or not self._agent:
            return
        if self._harness.verifies_agent_activation:
            spawn_agent = self._agent
            ids, current, _adv = parse_session_modes(resp)
            if current:
                if current == spawn_agent:
                    return
            elif self._mode_available(spawn_agent, resp):
                return
            await self.terminate_session(session_id)
            raise AcpRuntimeError(
                f"Agent {spawn_agent!r} was spawned with --agent but is not the "
                f"agent this session is running (current mode: "
                f"{current or '(none reported)'}; advertised: {ids or 'none'}). "
                + await self._spawn_agent_not_loaded_reason(spawn_agent)
            )

    async def _spawn_agent_not_loaded_reason(self, spawn_agent: str) -> str:
        """Why the ``--agent`` spec did not load, and the remedy, for Guard (B).

        Crew's roster lists an agent defined as one markdown file (``<name>.md``,
        the v3 / Kiro IDE form) for every backend, but a host that answers False
        to ``reads_markdown_agent_specs`` discovers ``*.json`` alone, so such an
        agent reaches this guard exactly like a missing JSON spec does -- and the
        generic advice ("run setup to rewrite the agent config") would send the
        operator to repair a file that is not the problem. The markdown check is
        asked only once the failure has ALREADY happened, on the refusal branch,
        so the spawn path itself gains no gate and no failure mode
        (harness-parity H13): the host answers from
        ``ACP_BACKENDS_MARKDOWN_AGENT_SPECS``, never "is kiro", and a host added
        later that reads markdown is handed the generic text unchanged.
        """
        if not self._harness.reads_markdown_agent_specs:
            markdown_spec = await asyncio.to_thread(
                markdown_spec_for_agent, spawn_agent, self._work_dir
            )
            if markdown_spec is not None:
                from kiro_crew.acp_backends import backend_to_stored

                capable = ", ".join(
                    repr(b)
                    for b in sorted(map(backend_to_stored, ACP_BACKENDS_MARKDOWN_AGENT_SPECS))
                )
                return (
                    f"{spawn_agent!r} is defined in markdown ({markdown_spec.name}); the "
                    f"{self._harness.backend or 'kiro'!r} backend loads JSON agent specs "
                    f"only, so it ran its own default agent instead, which would "
                    f"silently drop every Kiro Crew tool the agent's prompt relies on. "
                    f"Switch agent.acp_backend to {capable} or add a JSON spec for this "
                    f"agent."
                )
        return (
            f"Its ~/.kiro/agents/{spawn_agent}.json is missing, or the backend "
            f"refused to load it. Refusing to run the backend's own default "
            f"agent in its place, which would silently drop every Kiro Crew "
            f"tool the agent's prompt relies on. Run `kirocrew setup "
            f"--agent-only` to rewrite the agent config."
        )

    def _resolve_start_alias(self, projection: Any, mode_agent: str) -> str:
        """Resolve *mode_agent*'s alias for a session START, launch-agent-aware.

        The launched agent's own activation is allowed at EVERY start even with
        no prepared view (the process is already running as it, and a shared
        runtime starts many sessions as self._agent over its life), so it takes
        ``spawn_agent`` (which keeps the authored name) rather than the strict
        ``agent``. Every OTHER mode_agent takes the strict resolver, so a switch
        to a mode this view never prepared is still rejected. Used at the initial
        resolution AND at both supersession re-checks, so a newer EMPTY projection
        adopted by a concurrent start does not reject the unchanged launch agent
        the strict resolver has no entry for."""
        if mode_agent and mode_agent == self._agent:
            return str(projection.spawn_agent(mode_agent))
        return str(projection.agent(mode_agent))

    def _superseding_alias(self, projection: Any, mode_agent: str) -> str:
        """*mode_agent*'s alias in a newer view adopted while its start was pending.

        The start's own preparation is older than that view, so its alias is never
        sent again; when the newer view does not offer the agent at all, the
        start fails rather than fall back to the older one."""
        try:
            return self._resolve_start_alias(projection, mode_agent)
        except ValueError as exc:
            emit_counter(SKILL_VIEW_FALLBACKS, {"outcome": "refused_superseded"})
            raise AcpRuntimeError(
                f"Agent {mode_agent!r} changed while this session was starting, and its "
                f"new skill view cannot be used ({exc}). The session was not started on "
                "the copy from before the change, which could carry permissions the change "
                "removed. Fix the agent and start the session again; if it keeps failing, "
                "restart the gateway."
            ) from exc

    def _refuse_if_view_unverified(self, mode_agent: str, used_generation: int) -> None:
        """Fail a start while the newest preparation of this runtime was not adopted.

        A preparation that began after this start's own and ended without a view
        (no view could be prepared, or it raised) may have read an edit to
        *mode_agent*'s spec that no adopted view reflects, so nothing proves the
        alias this start sends, or the host activated, is current. A view adopted
        after that preparation read the specs later still, and clears it."""
        unadopted = self._unadopted_skill_projection_generation()
        if unadopted <= max(used_generation, self._adopted_skill_projection_generation()):
            return
        emit_counter(SKILL_VIEW_FALLBACKS, {"outcome": "refused_superseded"})
        logger.warning(
            "AcpRuntime set_mode: a newer skill-view preparation did not complete while "
            "agent=%s was starting; refusing to start the session on a view it cannot "
            "verify",
            mode_agent,
        )
        raise AcpRuntimeError(
            f"Agent {mode_agent!r}: a newer preparation of its skill view did not "
            "complete while this session was starting, so nothing proves the view "
            "kiro-cli would run still matches the agent's spec. The session was not "
            "started. Start it again; if it keeps failing, restart the gateway."
        )

    def _switched_model_record(self) -> dict[str, str]:
        """The per-session ``agent/switched`` model record, created on first use.

        Read through this rather than the attribute because a runtime built with
        ``object.__new__`` (the shape several start-path tests use) never ran
        ``__init__``, and a start must not fail for want of an empty record.
        """
        record: dict[str, str] | None = getattr(self, "_switched_models", None)
        if record is None:
            record = {}
            self._switched_models = record
        return record

    def _note_switched_model(self, session_id: str, params: dict[str, Any] | None) -> None:
        """Record the model a ``_kiro.dev/agent/switched`` frame names for *session_id*.

        Called from the reader loop before the frame is queued, so the record is
        in place by the time the ``session/set_mode`` answer that follows it
        resolves. A frame naming no model leaves the previous record alone.
        """
        model = (params or {}).get("model")
        if isinstance(model, str) and model.strip():
            self._switched_model_record()[session_id] = model.strip()

    def take_switched_model(self, session_id: str) -> str:
        """Remove and return the model recorded for *session_id*, or ``""``.

        For a session handle whose ``set_mode`` switches a live session's agent:
        it clears the record before sending, so only that activation's frame is
        read back, and takes it before the next turn.
        """
        return self._switched_model_record().pop(session_id, "")

    async def _served_default_after_mode(self, handle: AcpSessionHandle, session_id: str) -> None:
        """Re-run the served-default check for the model ``set_mode`` just applied.

        Both session-start paths check the served default right after storing the
        ``session/new`` / ``session/load`` response, which is BEFORE the mode is
        activated. kiro-cli's ``set_mode`` puts the session on the model the
        activated agent's spec pins, undoing that earlier switch, and
        ``agent.model`` is copied into ``kirocrew.json``. An unentitled default
        then fails the first prompt with "no access to model".

        The model judged is the one kiro-cli itself reported in the
        ``_kiro.dev/agent/switched`` frame for this activation, not a second read
        of the spec file: the file can change after kiro-cli read it, and only
        the host knows which copy of the agent it loaded. No frame, or a frame
        without a model (older kiro-cli), is no evidence and changes nothing.
        """
        activated = self._switched_model_record().pop(session_id, "")
        if activated:
            await handle.ensure_served_default(activated_model=activated)

    def _refuse_if_view_superseded(
        self, mode_agent: str, sent_alias: str, used_generation: int
    ) -> None:
        """Fail a start the host answered after a newer view changed its agent.

        A concurrent start may adopt a newer projection while this start's
        ``set_mode`` is pending. Nothing says whether the host activated the older
        alias before or after that newer preparation read the spec, so when the
        newer view gives *mode_agent* a different alias the session may be running
        a view an edit has since revoked grants from, and it is not started. A
        newer view that leaves this agent's alias unchanged refuses nothing. Call
        with the projection lock held, so no preparation is mid-flight."""
        self._refuse_if_view_unverified(mode_agent, used_generation)
        if self._adopted_skill_projection_generation() == used_generation:
            return
        projection = getattr(self, "_native_skill_projection", None)
        try:
            newest = (
                self._resolve_start_alias(projection, mode_agent)
                if projection is not None
                else None
            )
        except ValueError:
            newest = None
        if newest == sent_alias:
            return
        emit_counter(SKILL_VIEW_FALLBACKS, {"outcome": "refused_superseded"})
        logger.warning(
            "AcpRuntime set_mode: skill view %s for agent=%s was replaced by a newer "
            "preparation while the request was pending; refusing to start the session",
            sent_alias,
            mode_agent,
        )
        raise AcpRuntimeError(
            f"Agent {mode_agent!r} changed while this session was starting, and kiro-cli "
            f"may have activated the skill view prepared before the change ({sent_alias}), "
            "which could carry permissions the change removed. The session was not "
            "started. Start it again; if it keeps failing, restart the gateway."
        )

    async def _activate_mode_bracketed(
        self,
        session_id: str,
        mode_agent: str,
        *,
        budget: float,
        payload_snapshot: Any,
        wire_registered: bool,
        mode_id: str | None = None,
    ) -> str | None:
        """Send ``session/set_mode`` for *mode_agent* inside the derived-spec bracket.

        *mode_id* is the id the host registered *mode_agent* under when that
        differs (``kas_wire_agent_id``); it is what ``set_mode`` names, and
        *mode_agent* stays the id every gate and message uses.

        ONE body for both session-start paths (create and resume), because the bracket
        is a protocol and a protocol written twice is two protocols the moment one copy
        is edited. Any failure terminates *session_id* first: ``session/new`` or
        ``session/load`` already succeeded, so the session exists in the host and a
        plain local unregister would leak it in the shared process.

        A ``set_mode`` naming an agent activates that agent's spec, and the spec it
        activates needs the same bracket the spawn's does. Neither other gate reaches
        here: the spawn gate is keyed to ``self._agent``, so a SHARED runtime spawned
        as one agent and switched to another on this line passes no gate, and a host
        that builds no in-process tool surface never runs ``session_mcp``'s gate.

        WHERE the spec is consumed differs by host, and that decides which snapshot the
        post-check may use -- exactly ONE per consumed load:

        * ``wire_registered`` -- the spec was consumed at ``session/new``, in the wire
          payload built under the projection's own gate; ``set_mode`` activates what is
          already registered and re-reads nothing. A fresh read HERE would judge the
          file while the host holds the payload, so a revocation landing between the
          build and this line would pass that read while the registered definition
          still carried the grants it removed. The post-check compares against the
          payload's OWN snapshot. No gate call on this path: a second snapshot for one
          consumed load is the defect, not a safeguard.
        * otherwise -- the spec is consumed HERE: kiro-cli reads it from disk at
          ``set_mode`` and boots that agent's MCP servers. Gated BEFORE the send,
          because a stale mirror activated here mounts and auto-approves a server the
          default agent does not have.

        After the send returns the host has consumed the spec, which closes the window
        the snapshot opened: a write landing before that point is caught, and one
        landing after cannot change what was already consumed. Same answer as the
        ``initialize`` bracket -- a session that may have activated an unverified spec
        must not survive.
        """
        from kiro_crew.agent import (
            DerivedSpecStale,
            require_fresh_derived_spec,
            require_unchanged_derived_spec,
        )

        if wire_registered:
            mode_snapshot = payload_snapshot
        else:
            try:
                mode_snapshot = await asyncio.to_thread(
                    require_fresh_derived_spec, mode_agent, self._work_dir
                )
            except DerivedSpecStale as exc:
                await self.terminate_session(session_id)
                raise AcpRuntimeError(str(exc)) from exc
        try:
            projection_now: Any = None
            used_generation = 0
            if getattr(self, "_native_skill_projection", None) is not None:
                from kiro_crew.acp.skill_projection import (
                    CrossProcessProjectionLockTimeout,
                    InProcessProjectionWaitTimeout,
                    prepare_native_skill_projection,
                )

                # Preparation and adoption run under the per-runtime projection
                # lock, so overlapping starts read the specs in the order their
                # views are adopted (see _skill_projection_lock for the lock order).
                async with self._skill_projection_lock():
                    previous = getattr(self, "_native_skill_projection", None)
                    generation = self._issue_skill_projection_generation()
                    adopted = False
                    try:
                        # Keep the transport mode selected at spawn for this process.
                        # The rollback environment switch takes effect after restart.
                        lock_errors: list[OSError] = []
                        prepared, cancelled = await _prepare_projection_to_completion(
                            prepare_native_skill_projection,
                            self._work_dir,
                            enabled=True,
                            on_lock_unavailable=lock_errors.append,
                        )
                        if prepared is not None:
                            # set_mode always names the FRESH alias, never an older
                            # one: an alias the host loaded earlier (the spawn one
                            # included) may hold a generation of the spec an edit has
                            # since revoked grants from. Inbound frames keep
                            # translating every alias this process published before
                            # (``recognise``), so an agent the host still lists under
                            # an older alias stays selectable. Holding
                            # ``_spawn_skill_projection`` keeps those aliases -- and so
                            # the host's view of them -- out of the prune.
                            spawn = getattr(self, "_spawn_skill_projection", None)
                            if spawn is not None:
                                prepared.recognise(spawn)
                            if previous is not None:
                                prepared.recognise(previous)
                            # Under the lock a fresh generation is always the
                            # newest; the refusal keeps an older preparation from
                            # ever replacing a newer one should adoption be reached
                            # any other way.
                            adopted = self._adopt_skill_projection(prepared, generation)
                        if cancelled:
                            # Adopted first: a view this preparation read must not
                            # vanish with the start that asked for it.
                            raise asyncio.CancelledError
                        if prepared is None:
                            # No view could be prepared (the alias lock is busy), so
                            # nothing proves any alias this process holds still says
                            # what the agent's spec says now. Fail the start rather
                            # than activate one.
                            emit_counter(SKILL_VIEW_FALLBACKS, {"outcome": "refused_unprepared"})
                            logger.warning(
                                "skill projection: re-preparation unavailable at set_mode; "
                                "refusing to start agent %r on a view it cannot verify",
                                mode_agent,
                            )
                            # Name a holder only when the preparer proved one: its
                            # in-process wait timing out means another start in THIS
                            # gateway; the alias file lock timing out means another
                            # process. Anything else (a bad lock file, the settings
                            # lock, a lease, a failed write) names no holder.
                            if any(
                                isinstance(e, InProcessProjectionWaitTimeout) for e in lock_errors
                            ):
                                held_by = (
                                    "another session start in this gateway still "
                                    "held the skill-view lock"
                                )
                            elif any(
                                isinstance(e, CrossProcessProjectionLockTimeout)
                                for e in lock_errors
                            ):
                                held_by = "the skill-view lock is held by another Kiro Crew process"
                            else:
                                held_by = "a lock or file it needs was unavailable"
                            raise AcpRuntimeError(
                                f"Agent {mode_agent!r}: its skill view could not be "
                                f"prepared ({held_by}), so this session was not "
                                "started. Try again in a moment; if it keeps failing, "
                                "restart the gateway."
                            )
                    finally:
                        if not adopted:
                            self._note_unadopted_skill_projection(generation)
                    projection_now = self._native_skill_projection
                    used_generation = self._adopted_skill_projection_generation()
            # set_mode is a handshake request: switching to an agent boots THAT
            # agent's MCP servers, the same server (re-)initialization that gives
            # session/new and session/load their 90s budget. A switched-to server
            # pending OAuth holds the response for its full 30s wait, so the generic
            # _REQUEST_TIMEOUT would turn set_mode into the SAME race the
            # session-start floor exists to prevent (see _SESSION_NEW_TIMEOUT).
            #
            # kiro-cli 2.25/2.26 does not reload its agents directory on the rename
            # that publishes an alias, so a fresh one can answer ``Mode '<alias>' not
            # found`` while the file is on disk. A miss for the alias this projection
            # translated *mode_agent* to -- and for nothing else -- forces a rescan and
            # is retried after _PROJECTED_MODE_RETRY_DELAYS_SECS. When the host still
            # has not loaded it, the start FAILS with the remedy: no other copy of the
            # agent (an older alias, or the authored spec kiro-cli cached) is proven
            # to match the spec on disk, so none is activated in its place. Every other
            # error propagates as before.
            params = set_mode_params(session_id, mode_id or mode_agent)
            # Translated from the projection this bracket adopted and sent as
            # fixed wire params -- past the send's own translation, which reads
            # ``_native_skill_projection`` at send time. The projection lock is
            # NOT held across the send, so a concurrent start may adopt a newer
            # view while this request is pending or between retries: before each
            # attempt the alias is re-derived from the newest adopted view (never
            # an older one), and an answer that arrives after a newer view
            # changed this agent fails the start (``_refuse_if_view_superseded``).
            # ``translate`` is passed only when a projection exists, so the send
            # is the same call as before for every caller without one.
            wire = params
            sent_alias: str | None = None
            untranslated: dict[str, Any] = {}
            if projection_now is not None:
                # The launched agent's own activation is allowed at EVERY session
                # start, even with no prepared view: the process is already running
                # as it, and a shared runtime starts many sessions as self._agent
                # over its life, so this is not a one-shot token to consume -- doing
                # so would break the second shared session that legitimately starts
                # as the same agent. _resolve_start_alias keeps the authored name
                # for self._agent; every OTHER mode_agent takes the strict agent(),
                # so a switch to a mode this projection never prepared is still
                # rejected and an agent cannot escape its scope. The SAME resolver
                # runs at both supersession re-checks below, so a newer empty
                # projection adopted by a concurrent start does not reject the
                # unchanged launch agent. The general (mid-session) set_mode path
                # stays strict as before -- this allowance is scoped to the start
                # bracket and keyed on self._agent, not on a mutable token.
                sent_alias = self._resolve_start_alias(projection_now, mode_agent)
                wire = {**params, "modeId": sent_alias}
                untranslated = {"translate": False}
            retries = iter(_PROJECTED_MODE_RETRY_DELAYS_SECS)
            missed: str | None = None
            while True:
                if (
                    sent_alias is not None
                    and self._adopted_skill_projection_generation() != used_generation
                ):
                    projection_now = self._native_skill_projection
                    used_generation = self._adopted_skill_projection_generation()
                    newest = self._superseding_alias(projection_now, mode_agent)
                    if newest != sent_alias:
                        # The retry schedule and the miss record belong to the
                        # alias being replaced; the newer one starts its own.
                        sent_alias = newest
                        wire = {**params, "modeId": sent_alias}
                        retries = iter(_PROJECTED_MODE_RETRY_DELAYS_SECS)
                        missed = None
                if sent_alias is not None:
                    self._refuse_if_view_unverified(mode_agent, used_generation)
                try:
                    await self._send_and_await(
                        METHOD_SET_MODE, wire, timeout=budget, **untranslated
                    )
                except AcpModeNotFound as exc:
                    if sent_alias is None or exc.mode_id != sent_alias:
                        raise
                    missed = exc.mode_id
                    delay = next(retries, None)
                    if delay is None:
                        emit_counter(SKILL_VIEW_FALLBACKS, {"outcome": "refused_unloaded"})
                        logger.warning(
                            "AcpRuntime set_mode: kiro-cli has not loaded skill view %s for "
                            "agent=%s after a forced reload and %d attempt(s); refusing to "
                            "start the session on any other copy of the agent",
                            missed,
                            mode_agent,
                            1 + len(_PROJECTED_MODE_RETRY_DELAYS_SECS),
                        )
                        raise AcpRuntimeError(
                            f"Agent {mode_agent!r} changed since this kiro-cli process "
                            f"started, and kiro-cli has not loaded its new skill view "
                            f"({missed}) after a forced reload. The session was not started "
                            "on an older copy of the agent, which could carry permissions "
                            "the change removed. Restart the gateway to apply the change."
                        ) from exc
                    # kiro-cli reloads on a data write, never on the rename that
                    # published the alias, and a miss triggers no reload: rewrite
                    # the alias's own bytes in place so it rescans, then retry.
                    from kiro_crew.acp.skill_projection import announce_alias

                    await asyncio.to_thread(announce_alias, exc.mode_id)
                    await asyncio.sleep(delay)
                    continue
                break
            if sent_alias is not None:
                # Wait out any preparation already running: one that began before
                # this answer may have read an edit the host's activation predates,
                # and it is adopted only when it finishes. Nothing else is held
                # here, so this cannot invert the lock order.
                async with self._skill_projection_lock():
                    self._refuse_if_view_superseded(mode_agent, sent_alias, used_generation)
            if missed is not None:
                emit_counter(SKILL_VIEW_FALLBACKS, {"outcome": "loaded_after_retry"})
                logger.info(
                    "AcpRuntime set_mode: kiro-cli loaded skill view %s for agent=%s "
                    "after a reload",
                    missed,
                    mode_agent,
                )
        except Exception:
            await self.terminate_session(session_id)
            raise
        try:
            await asyncio.to_thread(require_unchanged_derived_spec, mode_snapshot)
        except DerivedSpecStale as exc:
            await self.terminate_session(session_id)
            raise AcpRuntimeError(str(exc)) from exc
        return sent_alias

    # ── Internal Helpers ──

    def _one_at_a_time_ahead(self, method: str, req_id: int) -> list[tuple[int, str, float]]:
        """Record *req_id* if the backend serializes it; return what it was sent behind.

        Returns ``(id, method, seconds since sent)`` for every request in
        :data:`_ONE_AT_A_TIME_METHODS` still awaiting an answer, but only when
        *method* is a session start, the one caller that reports it, and only on a
        backend in ``ACP_BACKENDS_SERIAL_SESSION_STARTS``. Answered or abandoned
        ids are dropped here, so the map never outgrows ``_pending_requests``.
        """
        sent = self._one_at_a_time_sent
        for stale in [rid for rid in sent if rid not in self._pending_requests]:
            del sent[stale]
        # H6: only a backend measured to serialize these is described as having
        # made this start wait; an overlap on any other harness says nothing.
        if (
            method not in _ONE_AT_A_TIME_METHODS
            or self._acp_backend not in ACP_BACKENDS_SERIAL_SESSION_STARTS
        ):
            return []
        now = time.monotonic()
        ahead = (
            [(rid, m, now - t) for rid, (m, t) in sent.items()]
            if method in (METHOD_SESSION_NEW, METHOD_SESSION_LOAD)
            else []
        )
        sent[req_id] = (method, now)
        return ahead

    async def _send_and_await(
        self,
        method: str,
        params: dict[str, Any],
        timeout: float = _REQUEST_TIMEOUT,
        *,
        translate: bool = True,
    ) -> dict[str, Any]:
        """Send a JSON-RPC request and await the response via _pending_requests.

        Used for control-plane requests (initialize, session/new, set_mode)
        where we need the response immediately rather than routing it to a
        session queue. ``timeout`` bounds the wait — teardown paths pass a
        tighter value than the default so an unresponsive runtime can't stall
        session eviction. ``translate=False`` sends *params* as given, past the
        skill projection's name translation: the one caller is the ``set_mode``
        bracket, which passes params it translated itself and, between attempts,
        re-derives them only from the newest adopted projection
        (:meth:`_activate_mode_bracketed`).
        """
        if not self._process or not self._process.stdin:
            raise AcpRuntimeDead("process not running")
        if self._dead:
            raise AcpRuntimeDead("runtime is dead")

        req_id = self._next_id
        self._next_id += 1
        ahead = self._one_at_a_time_ahead(method, req_id)

        projection = getattr(self, "_native_skill_projection", None)
        if projection is not None and translate:
            params = projection.request(method, params)
        req = JsonRpcRequest(method=method, params=params, id=req_id)
        data = encode_frame(req.to_dict())

        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending_requests[req_id] = future

        try:
            # Bounded like send_request: the control-plane
            # requests (initialize, session/new, set_mode) share the same
            # drain under the shared write lock and must not park it against
            # a flow-control-paused kiro-cli. A stall raises AcpRuntimeStdinStalled
            # (an AcpRuntimeDead), caught below to retrieve/cancel the future.
            await self._write_request_bounded(data, req_id, method)
        except AcpRuntimeDead:
            # Reached through _refuse_write_if_dead (a sibling stall marked the
            # runtime dead while this caller held the lock wait) OR through
            # _write_request_bounded's own stall paths -- both of which call
            # _mark_dead. They all fail this future before the raise, so retrieve
            # its exception (or cancel a still-pending one) rather than drop the
            # last reference unretrieved and have asyncio log a handled error as
            # unhandled.
            self._pending_requests.pop(req_id, None)
            if future.done():
                if not future.cancelled():
                    future.exception()
            else:
                future.cancel()
            raise
        except (BrokenPipeError, ConnectionResetError) as exc:
            self._pending_requests.pop(req_id, None)
            self._mark_dead(f"pipe broken: {exc}")
            raise AcpRuntimeDead(f"pipe broken: {exc}") from exc

        self._last_activity = time.monotonic()

        stage = {
            "initialize": "initialize",
            METHOD_SESSION_NEW: "session_new",
            METHOD_SESSION_LOAD: "session_load",
            METHOD_SET_MODE: "set_mode",
        }.get(method)
        started = time.monotonic()
        try:
            result = await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            if method == METHOD_SESSION_NEW:
                # The answer may still come, and it names a session the runtime
                # has CREATED, so keep the future registered for the reader loop
                # and hand ownership to the caller's StartCollector via ``adopt``
                # (RFC §4.4) instead of leaving an unowned session in the shared
                # process. ``wait_for`` cancelled the future; register a fresh one
                # bound to the same id. Every other method is popped: a future
                # left registered with no reader is failed by the cleanup kill's
                # ``_mark_dead`` and logged as an exception nobody retrieved.
                fresh: asyncio.Future[dict[str, Any]] = loop.create_future()
                self._pending_requests[req_id] = fresh
                adopt = getattr(self._pending_requests, "adopt", None)
                adopted = adopt(req_id) if adopt is not None else fresh
            else:
                self._pending_requests.pop(req_id, None)
                adopted = None
            active_starts, queued_starts = _cold_start_counts()
            if self._process is None:
                process_state = "absent"
            elif self._process.returncode is None:
                process_state = "running"
            else:
                process_state = "exited"
            logger.warning(
                "acp_startup_stage stage=%s outcome=timeout timeout_method=%s "
                "timeout_budget_s=%g duration_ms=%.1f active_starts=%d "
                "queued_starts=%d process_state=%s stderr_lines=%d",
                stage or "control",
                method,
                timeout,
                (time.monotonic() - started) * 1000.0,
                active_starts,
                queued_starts,
                process_state,
                min(len(self._stderr_lines), 20),
            )
            # Name the budget: a session-start timeout (90s) must be
            # distinguishable from a generic control-plane one (30s).
            timeout_exc = AcpRequestTimeout(f"Request {method} timed out after {timeout:g}s")
            # Only a request the process had still not answered at this deadline
            # is reported: on a backend that answers them one at a time, this
            # start had not begun while that one was outstanding. One answered
            # earlier may have cost a few seconds or nothing, so it is left out
            # rather than blamed for the whole budget.
            unanswered = [(m, age) for rid, m, age in ahead if rid in self._pending_requests]
            if unanswered:
                setattr(timeout_exc, "queued_behind", unanswered)
            if adopted is not None:
                # What create_session needs to build the collector.
                setattr(timeout_exc, "req_id", req_id)
                setattr(timeout_exc, "adopted_future", adopted)
            raise timeout_exc
        if stage is not None:
            active_starts, queued_starts = _cold_start_counts()
            logger.info(
                "acp_startup_stage stage=%s outcome=ready timeout_method=%s "
                "timeout_budget_s=%g duration_ms=%.1f active_starts=%d queued_starts=%d",
                stage,
                method,
                timeout,
                (time.monotonic() - started) * 1000.0,
                active_starts,
                queued_starts,
            )
        return result

    async def _drain_stderr(self) -> None:
        """Drain stderr to prevent subprocess blocking."""
        assert self._process and self._process.stderr
        stderr = self._process.stderr
        try:
            while True:
                line = await stderr.readline()
                if not line:
                    break
                text = line.decode(errors="replace").strip()
                if text:
                    if self.recording_allowed:
                        self._stderr_lines.append(text)
                        if len(self._stderr_lines) > 20:
                            self._stderr_lines = self._stderr_lines[-20:]
                    # Latch here, at the sink, because this is the only point at
                    # which every line is guaranteed to have been seen. The
                    # trim above is what makes it necessary: nobody asks about
                    # auth until a request has already timed out, by which time a
                    # chatty startup can have pushed the auth line out of the ring.
                    # Deliberately does not log: the line below already emits this
                    # text at debug, so a second record here would add no content
                    # and only raise arbitrary matched stderr to a default-visible
                    # level, against this sink's own convention. The condition is
                    # surfaced where it is actionable instead -- as AcpAuthRequired.
                    if not self._saw_auth_failure and is_auth_failure_output(text):
                        self._saw_auth_failure = True
                    # Same sink, same reason as the auth latch: matched
                    # unconditionally (not only when ``recording_allowed``), so a
                    # restricted session that retains no stderr still gets the
                    # actionable classification rather than a bare exit code.
                    #
                    # SCOPED TO THE PRE-INITIALIZE WINDOW, unlike the auth latch,
                    # and the asymmetry is the point. A rejected credential does
                    # not un-reject itself, so latching it for the runtime's life
                    # is correct. This signature is not that: the child's OWN
                    # sandbox can refuse mid-life when the HARNESS spawns a tool
                    # subprocess, which says nothing about whether the agent
                    # process can start. A life-long latch would turn the next
                    # unrelated death -- a broken pipe, an OOM kill -- into a
                    # permanent "your sandbox is broken", and permanently is
                    # exactly how long the wrong verdict would last. A refusal
                    # that genuinely stops the child from starting is always
                    # printed before ``initialize`` completes, so the window that
                    # matters closes there.
                    if (
                        not self._first_session_ready
                        and not self._saw_sandbox_init_failure
                        and is_sandbox_init_failure_output(text)
                    ):
                        self._saw_sandbox_init_failure = True
                    if self.recording_allowed:
                        logger.debug("stderr: %s", text[:200])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # An overlong stderr line (ValueError / LimitOverrunError from
            # readline when no newline fits the buffer) or a low-level read
            # error must not kill this task with an unhandled exception. Log and
            # exit the drain cleanly rather than leaving a dead task behind.
            logger.debug(
                "stderr drain task exiting on error: %s",
                exc if self.recording_allowed else type(exc).__name__,
                exc_info=self.recording_allowed,
            )


# --------------------------------------------------------------------------- #
# Compatibility facade. The module-level code for the RSS and process-table helpers
# and session-start admission lives in the owner modules imported above, and every
# name that moved stays readable as ``kiro_crew.acp.runtime.<name>``:
#
# * A moved name this module's own code reads, and that nothing patches through
#   this module, is an ordinary import above: the owner's object, bound here.
# * Every other moved name is FORWARDED. ``__getattr__`` reads it from its owner,
#   and ``_facade_mirror`` sends a write or delete there, so a patch of
#   ``kiro_crew.acp.runtime.<name>`` reaches the owner's own callers. A forwarded name
#   is absent from this module's namespace on purpose -- a binding here would
#   shadow the owner for every later read -- and this module's code reads it as
#   ``<owner>.<name>``.
# * The modules the moved code probes with (``os``, ``platform_compat``,
#   ``subprocess``, ``sys``, ``time``, ``weakref``) and the two session-handle names
#   the start collector uses stay bound here. Each helper listed in the facade
#   test's ``_SEAM_IMPORTS`` imports them from this module when it runs, so a test
#   that rebinds one on ``kiro_crew.acp.runtime`` reaches the moved caller as it did
#   before the move.
#
# ``test/test_acp_refactor_facade.py`` pins both halves: a name a test patches
# through this module is forwarded, and a forwarded name is never bound here.
# --------------------------------------------------------------------------- #
#: Owner module -> every name this module forwards to it.
_EXPORTS_BY_OWNER: dict[str, tuple[str, ...]] = {
    "kiro_crew.acp.runtime_process_tree": (
        "_get_rss_mb",
        "_own_children",
        "_iter_descendant_pids",
        "_ProcessTable",
        "_PS_TABLE_TTL_S",
        "_ps_table_lock",
        "_ps_table_cache",
        "_reset_ps_table_cache",
        "_ps_process_table",
        "_rss_tree_mb_for_pids",
        "_get_rss_tree_mb",
    ),
    "kiro_crew.acp.runtime_start": (
        "_COLD_START_MAX_CONCURRENT",
        "_ColdStartAdmission",
        "_cold_start_admissions",
        "_cold_start_admissions_lock",
        "_cold_start_admission",
        "_SESSION_START_CONCURRENCY_DEFAULT",
        "_SESSION_START_CONCURRENCY_FLOOR",
        "_COLLECTOR_PERMIT_HEADROOM",
        "_resolve_session_start_concurrency",
        "_record_session_start",
        "SessionStartGate",
        "_session_start_gates",
        "_session_start_gates_lock",
        "session_start_gate",
        "StartAdopter",
        "START_OUTCOME_ADOPTED",
        "START_OUTCOME_TORN_DOWN",
        "START_OUTCOME_ABANDONED",
        "START_OUTCOME_RUNTIME_DEAD",
        "START_OUTCOME_ERROR",
        "_resolve_session_start_timeout",
    ),
    # Public names this module bound for the spawn tail, which the launch tail reads now.
    "kiro_crew.acp.launch": (
        "KIROCREW_SPAWNED_ENV",
        "KIROCREW_SPAWNED_VALUE",
        "RLIMIT_PROFILE_SESSION_HOST",
    ),
}


def _index_exports() -> dict[str, str]:
    """Invert the owner table into forwarded name -> owner."""
    return {name: owner for owner, names in _EXPORTS_BY_OWNER.items() for name in names}


#: Forwarded name -> the dotted NAME of its owner, never the module object: the owner
#: is read from :data:`sys.modules` on each use, so a module purged and imported again
#: is seen at once instead of this table forwarding to the old copy. Bound empty
#: before the install below, which fills it with every name the mirror serves.
_EXPORTS: dict[str, str] = {}


def _owner(name: str) -> ModuleType:
    """Return the module that owns forwarded *name*, resolved on each access.

    ``importlib.import_module`` answers from :data:`sys.modules`, the one place a
    module is stored, so a purged or replaced owner is seen at once; and it waits on
    that module's import lock while its body is still running, where a bare
    ``sys.modules`` read would hand a second thread a half-built owner.
    """
    return importlib.import_module(_EXPORTS[name])


# Hidden from type checkers: mypy types every unknown attribute of a module that
# defines ``__getattr__`` as ``Any``, so a mistyped ``runtime.<name>`` would type-check.
# mypy sees the forwarded names through the ``TYPE_CHECKING`` imports below instead.
if not TYPE_CHECKING:

    def __getattr__(name: str) -> Any:
        """Read a name added to ``_EXPORTS`` after the install from its owner (:pep:`562`).

        ``_facade_mirror.MirroredModule`` answers every name the mirror serves, so this
        declines each of them: ``install`` refuses a resolver that answers one.
        """
        if name not in _EXPORTS or name in _MIRROR.served:
            raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
        return getattr(_owner(name), name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_EXPORTS))


# The ``runtime_*`` owner modules hold code that was this module's own: the
# ``AcpRuntime`` members the class binds, and the module-level names re-imported
# above, which this module keeps binding. ``_facade_mirror`` keeps the whole patch
# surface: a name in ``_EXPORTS`` is forwarded to its one owner exactly as before
# (read, write and delete there; ``dir()`` lists it), and a write of any other name
# this module binds reaches every runtime owner binding the same object -- the
# readers one global of this module had before the split. ``surface`` is this
# module's own namespace, so an owner's private helper is never part of the facade.
# ``test/test_runtime_bindings.py`` pins the reach.
#: The runtime's owner modules, lowest layer first: an owner imports only owners
#: listed before it.
_RUNTIME_OWNERS: tuple[str, ...] = (
    "kiro_crew.acp.runtime_tree_kill",
    "kiro_crew.acp.runtime_spawn_launch",
    "kiro_crew.acp.runtime_spawn_entry",
    "kiro_crew.acp.runtime_session_setup",
    "kiro_crew.acp.runtime_session_create",
    "kiro_crew.acp.runtime_session_load",
)
_MIRROR = _facade_mirror.install(
    sys.modules[__name__],
    _RUNTIME_OWNERS,
    excluded=(),
    forwards=_index_exports(),
    forward_reads="import_module",
    surface=frozenset(globals()),
)
_EXPORTS.update(_MIRROR.served)

# Installed last, so the forwarding is live for every caller but never runs while this
# module is still binding its own names.
sys.modules[__name__].__class__ = _facade_mirror.MirroredModule

if TYPE_CHECKING:  # the forwarded names, visible to type checkers and IDEs
    from kiro_crew.acp.launch import (  # noqa: F401
        KIROCREW_SPAWNED_ENV,
        KIROCREW_SPAWNED_VALUE,
        RLIMIT_PROFILE_SESSION_HOST,
    )
    from kiro_crew.acp.runtime_process_tree import (  # noqa: F401
        _PS_TABLE_TTL_S,
        _get_rss_mb,
        _get_rss_tree_mb,
        _iter_descendant_pids,
        _own_children,
        _ProcessTable,
        _ps_process_table,
        _ps_table_cache,
        _ps_table_lock,
        _reset_ps_table_cache,
        _rss_tree_mb_for_pids,
    )
    from kiro_crew.acp.runtime_start import (  # noqa: F401
        _COLD_START_MAX_CONCURRENT,
        _COLLECTOR_PERMIT_HEADROOM,
        _SESSION_START_CONCURRENCY_DEFAULT,
        _SESSION_START_CONCURRENCY_FLOOR,
        START_OUTCOME_ABANDONED,
        START_OUTCOME_ADOPTED,
        START_OUTCOME_ERROR,
        START_OUTCOME_RUNTIME_DEAD,
        START_OUTCOME_TORN_DOWN,
        SessionStartGate,
        StartAdopter,
        _cold_start_admission,
        _cold_start_admissions,
        _cold_start_admissions_lock,
        _ColdStartAdmission,
        _record_session_start,
        _resolve_session_start_concurrency,
        _resolve_session_start_timeout,
        _session_start_gates,
        _session_start_gates_lock,
        session_start_gate,
    )
