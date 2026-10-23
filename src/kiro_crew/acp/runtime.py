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
from kiro_crew.acp import kas_secret_storage
from kiro_crew.acp import runtime_frame_accounting as _rt_frames
from kiro_crew.acp import runtime_host_requests as _rt_host
from kiro_crew.acp import runtime_liveness as _rt_liveness
from kiro_crew.acp import runtime_mode_activation as _rt_mode
from kiro_crew.acp import runtime_process_tree
from kiro_crew.acp import runtime_reader_loop as _rt_reader
from kiro_crew.acp import runtime_session_create as _rt_create
from kiro_crew.acp import runtime_session_load as _rt_load
from kiro_crew.acp import runtime_session_registry as _rt_registry
from kiro_crew.acp import runtime_session_setup as _rt_setup
from kiro_crew.acp import runtime_spawn_entry as _rt_entry
from kiro_crew.acp import runtime_spawn_launch as _rt_launch
from kiro_crew.acp import runtime_start
from kiro_crew.acp import runtime_tree_kill as _rt_tree
from kiro_crew.acp import runtime_wire as _rt_wire
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
from kiro_crew.acp.runtime_class import AcpRuntime
from kiro_crew.acp.runtime_frame_accounting import (
    _DROP_KEY_PLACEHOLDER,
    _DROP_NO_SESSION,
    _DROP_SUMMARY_INTERVAL_SECS,
    _DROP_SUMMARY_KEY_MAX_CHARS,
    _DROP_SUMMARY_MAX_KEYS,
    _ROSTER_OVERFLOW_REJECT_REASON,
    _ROSTER_OVERFLOW_SUMMARY_INTERVAL_SECS,
    _drop_key_part,
)
from kiro_crew.acp.runtime_host_requests import (
    _JSONRPC_METHOD_NOT_FOUND,
    _MCP_REATTEMPT_TIMEOUT,
    _MCP_SECRET_REFUSED,
    _MCP_SIGN_IN_TIMEOUT,
    _MCP_URL_NOT_OPENED,
    _UNREGISTERED_REJECT_REASON,
)
from kiro_crew.acp.runtime_liveness import (
    _DEFAULT_MAX_AGE_SECS,
    _DEFAULT_MAX_RSS_MB,
    _ENOSPC_HINT,
    _ENOSPC_MARKER,
    _RSS_PROBE_MIN_AGE_SECS,
    _STDERR_REASON_TAIL_CHARS,
    _proven_death_cause,
    _rc_phrase,
)
from kiro_crew.acp.runtime_mode_activation import (
    _PROJECTED_MODE_RETRY_DELAYS_SECS,
    _prepare_projection_to_completion,
)
from kiro_crew.acp.runtime_process_tree import (
    ChildRecord,
    _capture_child_records,
    _get_child_pids,
    _kill_escaped_children,
)
from kiro_crew.acp.runtime_reader_loop import (
    _DROP_IDS_IN_LOG,
    _MODE_NOT_FOUND_RE,
    _OVERSIZE_FRAME_ID_RE,
    _format_runtime_rpc_error,
    _oversize_frame_message,
    _oversize_frame_request_id,
    _runtime_rpc_exception,
)
from kiro_crew.acp.runtime_remote_workspace import (  # noqa: F401
    _WORKSPACE_TOOLS_BY_BUILTIN,
    _WORKSPACE_TOOLS_BY_KAS_TOOL,
    _restricts_replaced_builtins,
    _without_local_only_grants,
    _workspace_grants,
)
from kiro_crew.acp.runtime_session_create import (
    _ENTITLEMENT_PROBE_TIMEOUT,
    _ENTITLEMENT_PROBE_TTL_SECS,
    AcpSessionStartTimeout,
)
from kiro_crew.acp.runtime_session_registry import (
    _MCP_PROGRESS_ERROR_CAP,
    _MCP_PROGRESS_NAME_CAP,
    _MCP_PROGRESS_NAME_LEN_CAP,
    _TERMINATE_TIMEOUT,
    _capped_names,
    _queued_behind_note,
    _sanitize_progress_name,
    _strip_unprintable,
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
from kiro_crew.acp.runtime_wire import (
    _ONE_AT_A_TIME_METHODS,
    _REQUEST_TIMEOUT,
    AcpRuntimeStdinStalled,
    _PendingRequests,
)
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
    "kiro_crew.acp.runtime_session_registry",
    "kiro_crew.acp.runtime_mode_activation",
    "kiro_crew.acp.runtime_wire",
    "kiro_crew.acp.runtime_liveness",
    "kiro_crew.acp.runtime_host_requests",
    "kiro_crew.acp.runtime_frame_accounting",
    "kiro_crew.acp.runtime_reader_loop",
    "kiro_crew.acp.runtime_remote_workspace",
    "kiro_crew.acp.runtime_class",
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
