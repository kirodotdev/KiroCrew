"""Dashboard aiohttp application factory and startup."""

from __future__ import annotations

import asyncio
import contextlib
import errno  # noqa: F401
import faulthandler
import functools
import logging
import os
import socket  # noqa: F401
import stat  # noqa: F401
import sys  # noqa: F401
import time
from collections.abc import Awaitable, Callable, Sequence  # noqa: F401
from importlib import import_module  # noqa: F401
from pathlib import Path, PureWindowsPath  # noqa: F401
from typing import TYPE_CHECKING, Any, NamedTuple  # noqa: F401
from urllib.parse import quote  # noqa: F401

from aiohttp import web

from kiro_crew import platform_compat, port_resolution, shutdown_event  # noqa: F401
from kiro_crew._facade_compose import compose as _facade_compose
from kiro_crew.apps.backend import (  # noqa: F401
    start_deferred_app_backends,
    start_enabled_app_backends,
)
from kiro_crew.apps.hook_reconcile import init_hook_reconciler, stop_hook_reconciler  # noqa: F401
from kiro_crew.apps.hooks_integration import (  # noqa: F401
    _stop_spawned_backends,
    init_hooks_system,
    on_gateway_shutdown,
    on_gateway_startup,
)
from kiro_crew.apps.manager import cleanup_migrated_builtin, register_builtin_apps
from kiro_crew.autonudge import get_instance as _autonudge_get  # noqa: F401
from kiro_crew.autonudge_authz import authorize_and_add_nudge  # noqa: F401
from kiro_crew.browser_cli import launch as browser_cli_launch
from kiro_crew.browser_cli import launcher as browser_cli_launcher  # noqa: F401
from kiro_crew.browser_cli import managed_launcher as browser_cli_managed_launcher  # noqa: F401
from kiro_crew.browser_cli import snapshots as browser_cli_snapshots
from kiro_crew.browser_cli import token as browser_cli_token
from kiro_crew.browser_cli import view as browser_cli_view  # noqa: F401
from kiro_crew.channel_transcript_migration import migrate_channel_transcripts  # noqa: F401
from kiro_crew.config import data_home
from kiro_crew.config.loader import (  # noqa: F401
    STT_PROVIDER_LOCAL,
    KiroCrewConfig,
    consume_managed_service_launch_environment,
    degraded_config_files,
    load_loop_stall_exit_after,
    refresh_config_meta_stamp,
    refresh_materialized_agents,
    resolve_loop_stall_exit_after,
    tailnet_effective_allowed_logins,
    tailnet_identity_unknown,
)
from kiro_crew.crewmate_prune_migration import prune_synced_crewmates  # noqa: F401
from kiro_crew.dashboard import (  # noqa: F401
    cautious_boot,
    channel_slots,
    chat,
    crewmate_feed,
    handlers,
    members_roster_goals,
    slot_retention,
    tailnet,
    tailnet_serve,
)
from kiro_crew.dashboard.approval_posture import trust_carries  # noqa: F401
from kiro_crew.dashboard.chat_utils import (  # noqa: F401
    effective_session_key,
    wire_session_subagent_probe,
)
from kiro_crew.dashboard.crash_dump_store import (  # noqa: F401
    claim_dump_notification,
    dump_age_seconds,
    dump_replay_lines,
    newest_dump_with_stacks,
    open_dump_file,
    record_healthy_boot,
    rotate_dumps,
    sweep_stale_dumps,
)
from kiro_crew.dashboard.dist_file_response import DistFileResponse  # noqa: F401
from kiro_crew.dashboard.handlers.artifacts import (  # noqa: F401
    api_artifact_asset,
    api_artifact_comments,
    api_artifact_delete,
    api_artifact_delete_comment,
    api_artifact_detail,
    api_artifact_edit_comment,
    api_artifact_events,
    api_artifact_folder_create,
    api_artifact_folder_delete,
    api_artifact_folder_update,
    api_artifact_folders,
    api_artifact_mark_review,
    api_artifact_materialize,
    api_artifact_overwrite_remote,
    api_artifact_post_comment,
    api_artifact_publish,
    api_artifact_publish_providers,
    api_artifact_pull_latest,
    api_artifact_record_event,
    api_artifact_refresh_sharing,
    api_artifact_relocate,
    api_artifact_reopen_comment,
    api_artifact_reply_comment,
    api_artifact_reprobe_notice,
    api_artifact_resolve_comment,
    api_artifact_session_docs,
    api_artifact_set_folder,
    api_artifact_set_pinned,
    api_artifact_settle_blank,
    api_artifact_unpublish,
    api_artifact_update,
    api_artifact_update_sharing,
    api_artifact_upstream_status,
    api_artifact_version_detail,
    api_artifact_versions,
    api_artifacts_create,
    api_artifacts_list,
    api_remote_artifact_comments,
    api_remote_artifact_delete_comment,
    api_remote_artifact_get,
    api_remote_artifact_mark_review,
    api_remote_artifact_post_comment,
    api_remote_artifact_reply_comment,
    api_remote_artifacts_browse,
    api_remote_artifacts_clone,
    api_remote_artifacts_fork,
)
from kiro_crew.dashboard.handlers.feedback import setup_feedback_routes
from kiro_crew.dashboard.handlers.knowledge import setup_knowledge_routes
from kiro_crew.dashboard.handlers.link_meta import setup_link_meta_routes
from kiro_crew.dashboard.handlers.secrets import setup_secrets_routes
from kiro_crew.dashboard.handlers.source_providers import (  # noqa: F401
    register_status_delta_sink,
    unregister_status_delta_sink,
)
from kiro_crew.dashboard.handlers.spawn_resume import setup_spawn_resume_routes  # noqa: F401
from kiro_crew.dashboard.handlers.weixin_qr import setup_weixin_routes
from kiro_crew.dashboard.handlers.whatsapp_setup import setup_whatsapp_routes
from kiro_crew.dashboard.kiro_relay.caller import published_relay_caller  # noqa: F401
from kiro_crew.dashboard.kiro_relay.ingress import (  # noqa: F401
    RELAY_INGRESS_APP_KEY,
    RelayIngress,
    relay_ingress_middleware,
)
from kiro_crew.dashboard.listener_guard import (  # noqa: F401
    LISTENER_LOST_EXIT_CODE,
    ListenerGuard,
    release_site,
)
from kiro_crew.dashboard.loop_watchdog import LoopStallWatchdog  # noqa: F401
from kiro_crew.dashboard.origin import (  # noqa: F401
    AUDIT_CLAIMED_KEY,
    PROBE_PATHS,
    bind_address_for,
    build_allowed_origins,
    check_host,
    check_origin,
    dashboard_socket_path,
    frame_ancestors_value,
    is_proxied_request,
    mark_audit_claimed,
    resolve_dashboard_host,
    should_canonicalize_host,
)
from kiro_crew.dashboard.port_reclaim import (  # noqa: F401
    FOREIGN_HOLDER,
    HEALTHY_PEER,
    NO_HOLDER,
    RECLAIMED,
    reclaim_stale_gateway_port,
)
from kiro_crew.dashboard.relay_headers import relay_header_strip_middleware  # noqa: F401
from kiro_crew.dashboard.routes import register_all
from kiro_crew.dashboard.server_internal_paths import (  # noqa: F401
    _CSRF_SAFE_METHODS,
    _MIXED_INTERNAL_API_PATHS,
    _PRE_AUDIT_DENY_STATUSES,
    _STRICT_INTERNAL_API_PATHS,
    _VIA_PROXY_SUFFIX,
)
from kiro_crew.dashboard.server_response_headers import (  # noqa: F401
    _IMMUTABLE_CACHE_CONTROL,
    _IMMUTABLE_PATH_PREFIXES,
    _NO_STORE_CACHE_CONTROL,
    _PERMISSIONS_POLICY,
    _PNA_REQUEST_HEADER,
    _PNA_RESPONSE_HEADER,
    _VENDOR_CORS_HEADER_VALUE,
    _VENDOR_PATH_PREFIX,
    _VENDOR_PREFLIGHT_MAX_AGE_SECS,
    _WORKER_ASSET_MARKER,
    _WORKER_CACHE_CONTROL,
)
from kiro_crew.dashboard.server_runtime import api_entrypoint as _owner_api_entrypoint
from kiro_crew.dashboard.server_runtime import app_platform as _owner_app_platform
from kiro_crew.dashboard.server_runtime import browser_lifecycle as _owner_browser_lifecycle
from kiro_crew.dashboard.server_runtime import config_watch as _owner_config_watch
from kiro_crew.dashboard.server_runtime import crewmate_prune as _owner_crewmate_prune
from kiro_crew.dashboard.server_runtime import diagnostics as _owner_diagnostics
from kiro_crew.dashboard.server_runtime import heartbeat as _owner_heartbeat
from kiro_crew.dashboard.server_runtime import inherited_trust as _owner_inherited_trust
from kiro_crew.dashboard.server_runtime import instances_lifecycle as _owner_instances_lifecycle
from kiro_crew.dashboard.server_runtime import listener as _owner_listener
from kiro_crew.dashboard.server_runtime import listener_claims as _owner_listener_claims
from kiro_crew.dashboard.server_runtime import maintenance as _owner_maintenance
from kiro_crew.dashboard.server_runtime import mcp_routes as _owner_mcp_routes
from kiro_crew.dashboard.server_runtime import middleware_chain as _owner_middleware_chain
from kiro_crew.dashboard.server_runtime import owner_notices as _owner_owner_notices
from kiro_crew.dashboard.server_runtime import prevent_sleep as _owner_prevent_sleep
from kiro_crew.dashboard.server_runtime import safety_grants as _owner_safety_grants
from kiro_crew.dashboard.server_runtime import security_headers as _owner_security_headers
from kiro_crew.dashboard.server_runtime import security_middleware as _owner_security_middleware
from kiro_crew.dashboard.server_runtime import service_hooks as _owner_service_hooks
from kiro_crew.dashboard.server_runtime import session_restore as _owner_session_restore
from kiro_crew.dashboard.server_runtime import skill_learning as _owner_skill_learning
from kiro_crew.dashboard.server_runtime import static_assets as _owner_static_assets
from kiro_crew.dashboard.server_runtime import stt_hooks as _owner_stt_hooks
from kiro_crew.dashboard.server_runtime import tunnel as _owner_tunnel
from kiro_crew.dashboard.server_runtime import workflow_startup as _owner_workflow_startup
from kiro_crew.dashboard.server_runtime.api_entrypoint import (  # noqa: F401
    start_api_server,
)
from kiro_crew.dashboard.server_runtime.app_platform import (  # noqa: F401
    _kick_default_app_installs,
    _reconcile_app_resources,
    _start_app_backends,
    _start_bound_port_app_backends,
    _warm_builtin_app_names,
    _warm_materialized_agents,
)
from kiro_crew.dashboard.server_runtime.browser_lifecycle import (  # noqa: F401
    _prune_browser_snapshots_loop,
    _register_browser_install_cleanup,
    _register_browser_view_cleanup,
)
from kiro_crew.dashboard.server_runtime.config_watch import (  # noqa: F401
    _kick_config_watch,
    _register_config_watch,
)
from kiro_crew.dashboard.server_runtime.crewmate_prune import (  # noqa: F401
    _claimed_dashboard_slots,
    _converge_channel_transcripts,
    _crewmate_prune_gate_holds_path,
    _kick_crewmate_prune,
    _kick_deferred_transcript_removal,
    _register_change_card_hook,
    _register_crewmate_prune_gate,
    await_crewmate_prune_settled,
)
from kiro_crew.dashboard.server_runtime.diagnostics import (  # noqa: F401
    _precompute_telemetry,
    _register_diag_recorder_shutdown,
    _start_diag_recorder,
)
from kiro_crew.dashboard.server_runtime.heartbeat import (  # noqa: F401
    _dispatch_healthy_boot_marker,
    _open_loop_watchdog,
    _register_watchdog_shutdown,
    _report_prior_crash_dump,
    _start_loop_heartbeat,
)
from kiro_crew.dashboard.server_runtime.inherited_trust import (  # noqa: F401
    _clear_override_derived_trust,
    _suspend_override_derived_trust,
)
from kiro_crew.dashboard.server_runtime.instances_lifecycle import (  # noqa: F401
    _register_instances_hooks,
    _retake_hops_then_revive,
    _revive_intended_instances,
)
from kiro_crew.dashboard.server_runtime.listener import (  # noqa: F401
    SecondaryLoopback,
    _bind_once,
    _export_bound_port,
    _export_reserved_bind_evidence,
    _holds_every_loopback_family,
    _register_unix_socket_cleanup,
    _remove_stale_unix_socket,
    _reserve_dashboard_port,
    _resolved_bound_host,
    _resolved_bound_port,
    _start_secondary_loopback_site,
    _start_site,
    _start_unix_site,
)
from kiro_crew.dashboard.server_runtime.listener_claims import (  # noqa: F401
    _arm_listener_guard,
    _arm_secondary_listener_guard,
    _live_sibling_port,
    _note_listener_sidecar,
    _reconcile_listener_publication,
    _register_listener_guard_shutdown,
    _republish_listener_sidecar,
    _request_listener_lost_exit,
    _secondary_listener_given_up,
    _withdraw_listener_sidecar,
    _write_instance_credentials,
    _write_secret_file,
)
from kiro_crew.dashboard.server_runtime.maintenance import (  # noqa: F401
    _kick_connections_warm_scavenge,
    _kick_display_verdicts,
    _kick_knowledge_orphan_reclaim,
    _kick_local_decision_model,
    _kick_otel_crew_log_subscriber,
    _kick_owner_only_sweep,
    _kick_pinned_order_load,
    _kick_session_search_index,
    _own_host_warm_done,
    _prebuild_memory_after_preparation,
    _prebuild_memory_in_thread,
    _register_connections_warm_lifecycle,
    _register_own_host_warm,
)
from kiro_crew.dashboard.server_runtime.mcp_routes import (  # noqa: F401
    _deferred,
    _deferred_work_ledger,
    _register_mcp_routes,
)
from kiro_crew.dashboard.server_runtime.middleware_chain import (  # noqa: F401
    _dashboard_canonical_redirect,
    _insert_enterprise_gate,
    _install_api_middlewares,
    _install_dashboard_middlewares,
    _resolve_tailnet_trust,
    _tailnet_origin_enabled,
)
from kiro_crew.dashboard.server_runtime.owner_notices import (  # noqa: F401
    _dispatch_owner_dm,
    _dm_owner,
    _notify_owner_channels,
)
from kiro_crew.dashboard.server_runtime.prevent_sleep import (  # noqa: F401
    _arm_prevent_sleep_poll,
    _register_prevent_sleep_shutdown,
    _release_sleep_block,
    _should_prevent_sleep,
    _tailnet_publish_keeps_awake,
)
from kiro_crew.dashboard.server_runtime.safety_grants import (  # noqa: F401
    _apply_startup_yolo,
    _armed_unattended_loops,
    _dispatch_override_expiry_notification,
    _notify_restart_dropped_grant,
    _notify_slack_override_expired,
    _notify_unattended_expiry,
    _override_expiry_dm_text,
    _take_prior_dropped_grant,
    _unattended_expiry_text,
)
from kiro_crew.dashboard.server_runtime.security_headers import (  # noqa: F401
    _apply_security_headers,
    _asset_cache_control,
    _extra_frame_ancestors,
    _finalize_asset_cache_control,
    _install_asset_cache_control_finalizer,
    _vendor_preflight_handler,
)
from kiro_crew.dashboard.server_runtime.security_middleware import (  # noqa: F401
    _audit_denied,
    _make_csrf_middleware,
    _make_deny_audit_middleware,
    _make_host_validation_middleware,
    _mixed_internal_api_paths,
    _would_soften_a_strict_path,
    audit_actor,
    build_host_canonical_redirect,
)
from kiro_crew.dashboard.server_runtime.service_hooks import (  # noqa: F401
    _register_kiro_service_shutdown,
    _wire_status_delta_sink,
)
from kiro_crew.dashboard.server_runtime.session_restore import (  # noqa: F401
    _restore_dashboard_sessions,
)
from kiro_crew.dashboard.server_runtime.skill_learning import (  # noqa: F401
    _auto_create_consolidator,
    _pending_skill_notification,
    _register_pending_skill_hooks,
)
from kiro_crew.dashboard.server_runtime.static_assets import (  # noqa: F401
    _dist_file_handler,
    _is_anchored,
    _register_dist_static_routes,
    _resolve_dist_file,
    _serve_dist_file,
    _window_entry_handler,
    discover_app_window_entries,
)
from kiro_crew.dashboard.server_runtime.stt_hooks import (  # noqa: F401
    _import_stt_engine,
    _log_prewarm_outcome,
    _register_stt_hooks,
    _stt_idle_sweep,
    _stt_startup_prewarm,
)
from kiro_crew.dashboard.server_runtime.tunnel import (  # noqa: F401
    _start_aea_tunnel,
    _wire_tunnel_shutdown,
)
from kiro_crew.dashboard.server_runtime.workflow_startup import (  # noqa: F401
    _initialize_workflow_service,
    _kick_workflow_initialization,
    _register_workflow_lifecycle,
)
from kiro_crew.dashboard.slot_ownership import slot_ownership_middleware  # noqa: F401
from kiro_crew.dashboard.slowloris import (  # noqa: F401
    build_hardened_runner,
    reject_compressed_body_middleware,
)
from kiro_crew.dashboard.state import _DEFAULT_PORT, DashboardState
from kiro_crew.dashboard.token_auth import (  # noqa: F401
    _cookie_port_from_host,
    _is_spa_shell_request,
    internal_path_matches,
    is_csrf_exempt,
    register_app_window_paths,
    token_auth_middleware,
    token_embed_parent_port,
    warm_auth_singletons,
)
from kiro_crew.deploy import _register_core_skills as _register_deploy_skills
from kiro_crew.deploy.handlers import register_routes as _register_deploy_routes
from kiro_crew.executors import static_asset_executor  # noqa: F401
from kiro_crew.executors import subprocess_executor
from kiro_crew.hooks import ScriptHookStore, set_global_hook_store
from kiro_crew.instances import run_marker  # noqa: F401
from kiro_crew.instances.registry import InstancesRegistry  # noqa: F401
from kiro_crew.instances.ssh_tunnel_manager import SshTunnelManager, TunnelState  # noqa: F401
from kiro_crew.mate_config_migration import migrate_existing_crewmates
from kiro_crew.mcp_gateway.socketsec import chmod_socket_0600  # noqa: F401
from kiro_crew.metrics.http_metrics import (  # noqa: F401
    make_route_latency_middleware,
    record_boot_to_ready,
)
from kiro_crew.owner_only_files import tighten_data_home  # noqa: F401
from kiro_crew.platform import (
    async_safe_context_call,
    current_context,
    safe_context_call,
)
from kiro_crew.power import SleepInhibitor  # noqa: F401
from kiro_crew.pre_exec_stops import register_pre_exec_stop  # noqa: F401
from kiro_crew.safety_override import (  # noqa: F401
    POLICY_REVOKED_SOURCE,
    apply_config_duration,
    describe_dropped_grant,
    grant_declared_yolo,
    safety_override,
    take_dropped_grant,
)
from kiro_crew.security import redact_credentials, redact_exfiltration_urls  # noqa: F401
from kiro_crew.security.argv_floor import warm_own_host_names  # noqa: F401
from kiro_crew.sel import sel, sel_is_warm, warm_sel_singleton  # noqa: F401
from kiro_crew.sel_request_provenance import bind_request_forwarded  # noqa: F401
from kiro_crew.skill_usage import register_skill_read_observer
from kiro_crew.skills import (  # noqa: F401
    SkillsLoader,
    set_pending_consumed_hook,
    set_pending_staged_hook,
)
from kiro_crew.stall_attribution import attribute_dump, describe  # noqa: F401
from kiro_crew.tunnel.setup import setup_tunnel  # noqa: F401

if TYPE_CHECKING:
    from kiro_crew.dashboard._types import (  # noqa: F401
        ContextBuilder,
        ConversationLog,
        CronService,
        HistoryConsolidator,
        LessonStore,
        SessionManager,
        SubagentManager,
        TaskRunner,
    )
    from kiro_crew.kiro_prerequisite import KiroPrerequisiteService

# aiohttp's static file handler uses its own ``mimetypes.MimeTypes()`` instance
# (``aiohttp.web_fileresponse.CONTENT_TYPES``) which does NOT load the system
# mime.types database.  Font extensions are missing from the built-in Python
# fallback, so aiohttp returns ``application/octet-stream`` for .woff/.woff2/.ttf.
# Register the correct font MIME types into that singleton at import time so ALL
# static routes (including ``/fonts``) serve proper Content-Type headers.
from aiohttp.web_fileresponse import CONTENT_TYPES as _AIOHTTP_CONTENT_TYPES

_AIOHTTP_CONTENT_TYPES.add_type("font/woff", ".woff")
_AIOHTTP_CONTENT_TYPES.add_type("font/woff2", ".woff2")
_AIOHTTP_CONTENT_TYPES.add_type("font/ttf", ".ttf")

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
_DIST_DIR = _STATIC_DIR / "dist"

# How often the prevent-sleep poll re-evaluates whether the host should be kept
# awake. It only needs to beat OS idle-sleep timers (minutes), so a coarse
# interval keeps the overhead negligible; a turn shorter than one interval never
# outlasts a sleep timer, so not catching it is harmless.
_PREVENT_SLEEP_POLL_INTERVAL_SECS = 15.0

# How long the speech idle-sweep task waits before importing the recogniser package.
# Its only job is to keep boot clean: the import pulls numpy and the binding, and the
# hook that starts this task runs before either socket binds. Anything past the first
# few seconds of boot works, since the sweep's own interval is a minute.
_STT_SWEEP_BOOT_DELAY_SECS = 30.0

# How long the boot prewarm waits before loading the speech model. Shorter than the
# sweep's delay because this one is racing a user: its whole purpose is to be resident
# BEFORE the first dictation, and someone who opens the dashboard to dictate does it
# within seconds. Still non-zero, so the load never competes with binding the
# listener, serving the first page, or restoring sessions.
_STT_PREWARM_BOOT_DELAY_SECS = 5.0

# How recent a real decode must be for the boot prewarm to load the speech model.
# The weights are one anonymous native buffer (1.6 GB for large-v3-turbo) held for
# `stt.idle_evict_secs`, so a host that does not dictate skips the boot warm and keeps
# the pointer-down prewarm.
_STT_BOOT_PREWARM_RECENT_USE_SECS = 14 * 86400


#: The tailnet publish state is a subprocess round trip (`tailscale serve
#: status`), and the prevent-sleep poll runs every 15s — far too often to spawn a
#: CLI each time. Cached SEPARATELY from the mobile-access card's own reads, which
#: stay live on purpose: a stale awake decision costs at most one window of
#: battery, while a stale card would show the operator the wrong next action.
_TAILNET_AWAKE_TTL_SECS = 60.0

#: ``(monotonic expiry, published)``. Module-level so both server entrypoints
#: share one cache rather than each paying its own subprocess.
_tailnet_awake_cache: tuple[float, bool] = (0.0, False)


# Base Content-Security-Policy applied to all dashboard responses.
# See ``_apply_security_headers`` for the full rationale and the
# instances-mode ``frame-src`` extension.
_BASE_CSP = (
    "default-src 'self'; "
    # https://esm.sh: MCP App (SEP-1865) srcdoc iframes INHERIT this header
    # CSP (a srcdoc document has no HTTP response of its own), and the real
    # excalidraw/pdf MCP apps load their ESM runtime (React, @excalidraw/…)
    # from esm.sh via importmap. Without these allowances the app's module
    # imports are blocked no matter what the per-app srcdoc <meta> CSP says
    # (when two policies apply, the most restrictive wins per directive).
    # Same pattern as the widget CDN allowances (tailwind/jsdelivr/cdnjs).
    # 'wasm-unsafe-eval': the Pierre highlight workers tokenize with the
    # shiki-wasm engine (website/src/pierre/config.ts, PIERRE_REGEX_ENGINE —
    # chosen there because the JS engine has no backtracking ceiling and a
    # pathological grammar match kills the renderer as a cage OOM).
    # WebAssembly.compile/instantiate requires this source expression in the
    # executing context's script-src, and a same-origin worker takes its CSP
    # from its own script RESPONSE — this header — not from the document that
    # spawned it. Without it the tokenizer worker's WASM instantiation is
    # refused and every diff surface dies on first highlight. It permits ONLY
    # WebAssembly compilation, never JS eval ('unsafe-eval' stays out).
    "script-src 'self' 'unsafe-inline' 'wasm-unsafe-eval' "
    "https://cdn.tailwindcss.com https://cdn.jsdelivr.net https://cdnjs.cloudflare.com "
    "https://esm.sh; "
    # The UI's two brand faces (Space Grotesk, JetBrains Mono) are served from
    # this origin (website/src/assets/fonts/, hashed into /assets/ by Vite), so
    # style-src and font-src name no third-party font host; 'self' covers them.
    "style-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com https://cdn.jsdelivr.net "
    "https://esm.sh; "
    "img-src 'self' data: blob: https:; "
    "font-src 'self' data: https://esm.sh; "
    # Loopback http(s) origins ({connect_src_extra}) mirror the frame-src note
    # below: WebPreviewPanel does not merely FRAME the local dev server, it also
    # polls it with a no-cors `fetch` liveness probe (a cross-origin iframe
    # cannot report that its server died). Framing without connecting made that
    # probe throw on every tick, so two strikes flipped a perfectly healthy
    # preview to "server stopped responding" and unmounted the iframe. The
    # probe is no-cors, so no response data is ever readable — this admits the
    # reachability check only, and to the same origins frame-src already allows.
    "connect-src 'self' ws://localhost:* ws://127.0.0.1:* "
    "https://esm.sh{connect_src_extra}; "
    "media-src 'self' blob:; "
    "worker-src 'self' blob:; "
    # https://*.cloudfront.net: live preview iframes for deployed webapp
    # artifacts (WebAppArtifactCard / WebAppThumb). The artifact-deploy
    # contract only ever produces `<dist-id>.cloudfront.net` URLs; the FE
    # additionally gates on that exact host shape (framablePreviewUrl) so a
    # crafted webapp_metadata URL on any other host is never framed.
    # http://127.0.0.1:* / http://localhost:*: the Web Preview panel
    # (WebPreviewPanel) frames a local dev/static server. Always admitted so
    # the feature works in the packaged dashboard, not only in instances mode.
    # The panel isolates the preview host from the dashboard host
    # (isolatePreviewHost) so host-scoped dashboard cookies are never sent to
    # the framed server. The *.localhost tunnel wildcard stays instances-gated.
    "frame-src 'self' blob: https://*.cloudfront.net{frame_src_extra}; "
    "object-src 'none'; base-uri 'self'; frame-ancestors {frame_ancestors}"
)

# Loopback preview origins — always framable AND connectable (see the
# frame-src / connect-src notes above). Aligned with the URLs
# WebPreviewPanel.normalizeUrl accepts: http+https on every loopback host, so a
# preview never renders blank due to a CSP-blocked frame, nor gets declared
# unreachable due to a CSP-blocked liveness probe.
#
# IPv6 loopback ([::1]) is deliberately OMITTED. A CSP host-source that pairs a
# bracketed IPv6 literal with a wildcard port — `http://[::1]:*` — is invalid
# per the CSP grammar, so Chromium drops that ENTIRE source and logs
# "contains an invalid source: 'http://[::1]:*'". Because the source was being
# dropped anyway, `[::1]:*` never actually admitted anything; removing it is
# behaviour-preserving for IPv4 loopback (127.0.0.1 / localhost / 0.0.0.0, whose
# non-bracketed literals accept a wildcard port) and only silences the console
# error the pet page surfaced. There is no wildcard-port form Chromium accepts
# for a bracketed IPv6 host, so IPv6 loopback preview cannot be expressed here
# without pinning a specific port — which the arbitrary-port preview use case
# rules out.
# The client mirrors this set in `isEmbeddableLoopbackOrigin`
# (website/src/lib/tunnelOrigin.ts) to decide, before mounting a remote-crew
# pane, whether the dashboard's own origin can embed it. Edit the two in step:
# admitting a new frame-src origin here (e.g. [::1] or https *.localhost) while
# the client stays unchanged leaves the pane silently refused on an origin the
# server now allows.
_LOOPBACK_FRAME_SRC = (
    " http://127.0.0.1:* http://localhost:* http://0.0.0.0:*"
    " https://127.0.0.1:* https://localhost:* https://0.0.0.0:*"
)
# Additional tunnel wildcard, only when the instances feature is enabled.
# Mirrored client-side in isEmbeddableLoopbackOrigin (see above).
_INSTANCES_FRAME_SRC_EXTRA = " http://*.localhost:*"

# Max size of a single incoming HTTP header field, raised from aiohttp's
# 8190-byte default. Browser cookies are not port-isolated (RFC 6265), so on
# 127.0.0.1 the per-port mc_token_<port>/mc_refresh_<port> cookies of every
# gateway instance pile up in one shared Cookie header. At the 8190 default
# that header crosses the limit after ~16 ports and aiohttp's C parser rejects
# the request with 400 LineTooLong BEFORE any handler runs — so the request
# that would prune the jar can never execute. This headroom lets an oversized
# request reach the handler, which then expires the other-port cookies (see
# refresh_tokens.foreign_port_cookies) so the jar self-trims. 32 KiB stays well
# under a DoS-relevant size while covering ~60 accumulated ports plus other
# request headers.
_MAX_HEADER_FIELD_SIZE = 32 * 1024

# Upper bound on the tunnel teardown at shutdown. The provider behind the
# ``TunnelProvider`` seam may talk to a remote control plane (or supervise a
# child process), so an unbounded await here could hang ``runner.cleanup()``
# forever and wedge the whole gateway exit. 5s is generous for a local
# teardown and still well inside the desktop app's shutdown window.
_TUNNEL_STOP_TIMEOUT_SECS = 5.0


# URL prefix for app-shipped standalone HTML windows. One namespace keeps app
# window URLs from colliding with the SPA's own routes, and the two path segments
# after it mirror the on-disk `<app>/<name>.html` exactly — see
# `discover_app_window_entries` for what the previous flat scheme cost.
APP_WINDOW_URL_PREFIX = "app-windows"


#: Where Vite mirrors each app's standalone window entries inside the build.
_APP_WINDOWS_SUBDIR = "src/apps"


# Windows TcpTimedWaitDelay default: remnant connections from the previous
# generation pin the port for up to 4 minutes, during which an exclusive
# (SO_EXCLUSIVEADDRUSE) bind is refused. The reservation ladder stretches to
# this budget ONLY on Windows and ONLY when the reclaim probe found no live
# holder — see _reserve_dashboard_port.
_TIME_WAIT_BUDGET_SECS = 240


SECONDARY_LOOPBACK_FOR = {"127.0.0.1": "::1", "::1": "127.0.0.1"}

#: Hostnames that name a SET of loopback listeners rather than one, so reaching
#: them can land on either family. The client side keeps the same list
#: (``AMBIGUOUS_LOOPBACK_NAMES`` in ``website/electron/local-token.js``), because
#: both sides answer the same question: may a credential be sent to this host?
AMBIGUOUS_LOOPBACK_HOSTS = frozenset({"localhost", "kirocrew.localhost"})


_UNATTENDED_EXPIRY_TITLE = "🔒 Auto-approve expired while an unattended run was in progress"


# How long a mutating request waits for the startup crewmate prune before it is
# answered 503. The pass is marker-gated and runs immediately after the bind, so
# on every boot but the first after the upgrade the wait is the few milliseconds
# the pass takes to find the marker; on that first boot it is one config read
# and one read of each candidate's DM transcript.
_CREWMATE_PRUNE_GATE_TIMEOUT_S = 60.0
_CREWMATE_PRUNE_GATE_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
#: Held whatever the method, so the roster is read once the pass has settled
#: and never lists a row the pass is removing. ``GET /api/members`` is not a
#: pure read either: it calls ``MemberEventLogService.ensure`` for every row
#: and ``reconcile_member_config`` appends to the member log. Every
#: ``/api/members`` route reaches the same rows, so the whole prefix waits.
_CREWMATE_PRUNE_GATE_HELD_PREFIXES = ("/api/members",)


# Strong references to in-flight warm tasks, so the loop cannot collect one
# before it finishes.
_OWN_HOST_WARM_TASKS: "set[asyncio.Future[None]]" = set()


# Deep link at the approval toggle itself (Settings -> Skills, highlighted), so
# the notification can offer the opt-out at the exact moment the user is being
# asked to review yet another candidate. Same highlight=key:<configKey> format
# the frontend's <SettingRef> builds, consumed by useSettingHighlight.
_SKILL_APPROVAL_SETTING_URL = "/settings/skills?highlight=key:skills.approval_required"


async def start_dashboard(
    sessions: SessionManager,
    crons: CronService,
    lessons: LessonStore,
    port: int = _DEFAULT_PORT,
    subagents: SubagentManager | None = None,
    context_builder: ContextBuilder | None = None,
    conversation_log: ConversationLog | None = None,
    consolidator: HistoryConsolidator | None = None,
    task_runner: TaskRunner | None = None,
    slack_connected: bool = False,
    local_only: bool = True,
    configured_host: str = "",
    dashboard_url: str = "",
    slack_client: Any = None,
    owner_id: str = "",
    assume_kiro_ready: bool = False,
    kiro_prerequisite_service: "KiroPrerequisiteService | None" = None,
    defer_channel_agent_resume: bool = False,
    schedule_memory_preparation: "Callable[[], asyncio.Task[None] | None] | None" = None,
) -> tuple[web.AppRunner, DashboardState]:
    """Start the dashboard web server.  Returns ``(runner, state)``."""
    # Channels retain this same runner on the gateway, independently of state.
    # Close shared admission before the first startup await, not just the UI pointer.
    if task_runner is not None:
        task_runner.defer_workflow_attachment()
    # The generated service marker describes this launch, not every process the
    # dashboard may later spawn. Snapshot it before starting app backends or
    # child terminals, then use only that snapshot to choose the watchdog grace.
    _launch_environment = consume_managed_service_launch_environment()

    # Auto-create consolidator if conversation_log available but no consolidator
    if consolidator is None and conversation_log is not None:
        consolidator = _auto_create_consolidator(
            sessions, lessons, context_builder, conversation_log
        )

    state = DashboardState(
        sessions=sessions,
        crons=crons,
        lessons=lessons,
        start_time=time.time(),
        subagents=subagents,
        context_builder=context_builder,
        conversation_log=conversation_log,
        consolidator=consolidator,
        task_runner=task_runner,
        slack_client=slack_client,
        owner_id=owner_id,
    )
    # Before anything can serve a request: until _restore_dashboard_sessions has
    # rebuilt the slot set, a list read here would be missing every tab it has
    # not reached yet.
    state.begin_slot_list_rebuild()

    # --- Pending-skill approval notifications ---
    _register_pending_skill_hooks(state)

    state._hook_store = ScriptHookStore()
    set_global_hook_store(state._hook_store)

    # Credit the skill-usage ledger for skill bodies the model reads directly
    # (a file-read tool or `cat`), which bypass the loader entirely.
    register_skill_read_observer(state.context_builder)

    # Wire script hooks into subagent tool execution path
    if state.subagents is not None:
        state.subagents.hook_store = state._hook_store

    # Visible notice + pct reset when auto-compaction fires on a dashboard session
    state.wire_session_compact_callback()
    # Visible notice when the watchdog recycles a dashboard session (e.g. RSS)
    state.wire_session_recycle_callback()
    # The RSS ceiling must not recycle a parent whose sub-agents are still
    # running on its runtime; the manager cannot see them without this probe.
    wire_session_subagent_probe(state)
    # Visible notice in a channel that just lost its session-resume binding
    state.wire_session_unbind_listener()
    # Crew-log class record for a binding that just COMMITTED, taken before anything
    # can be routed through it
    state.wire_session_bind_listener()
    # Lands a waiting model pick when a channel or cron turn ends, a boundary
    # no dashboard turn passes
    state.wire_session_release_listener()

    app = web.Application(
        client_max_size=60 * 1024 * 1024
    )  # 60 MB: covers a 50 MB BUFFERED upload + multipart overhead. NOT a
    # ceiling on every upload: aiohttp enforces this in Request.read()/.post(),
    # not on the streaming multipart() reader, so the video path in
    # handlers/files.py streams past it under its own _MAX_VIDEO_UPLOAD_BYTES
    # (pinned by test_streaming_bypasses_the_app_client_max_size). Reading this
    # number as a global request cap is the false invariant to avoid.
    app["state"] = state

    # Bind the serving loop once, here: this runs ON that loop, so every
    # surface that later hands work in from a foreign thread -- slots
    # coalescing, an off-loop websocket send, the log handler's fan-out --
    # resolves the same loop instead of each latching its own copy from
    # whichever thread happens to arrive first.
    state.bind_serving_loop(asyncio.get_running_loop())
    crewmate_feed.start(state)  # the goal and waiting folds a crewmate's page reads
    # The roster's has_goal / heartbeat_on re-read after every goal write.
    members_roster_goals.start_roster_refresh(state)
    # Voice settings live in slack/handler's module state and are otherwise
    # loaded only on the Slack startup path (set_orch_cfg) — without this a
    # dashboard-only gateway (no Slack tokens) resets TTS to defaults on
    # every restart (see load_voice_reply_config).
    from kiro_crew.slack.handler import load_voice_reply_config

    await asyncio.to_thread(load_voice_reply_config)
    # ── Tunnel teardown (FIRST cleanup hook, deliberately) ───────────────────
    # aiohttp dispatches ``on_cleanup`` in registration order and gateway
    # shutdown has a hard deadline, so this is registered ahead of every other
    # cleanup hook: behind them it can be starved — instances cleanup waiting on
    # SSH children that ignore SIGTERM eats the deadline, the gateway
    # force-exits, and the tunnel is never stopped. Safe this early: the hook
    # only reads ``state.tunnel_manager`` lazily at shutdown, long after
    # ``setup_tunnel`` assigns it further below, and this is still well before
    # ``runner.setup()`` freezes the signal lists. See ``_wire_tunnel_shutdown``.
    _wire_tunnel_shutdown(app, state)
    # Enterprise build only: no kiro login, no Crew. The gateway hands in the
    # service whose boot probe saw the login; a caller that did not run that
    # check gets it here, still before the bind. A failure exits non-zero.
    from kiro_crew.enterprise_boot import require_kiro_login
    from kiro_crew.kiro_prerequisite import KiroPrerequisiteService

    if kiro_prerequisite_service is None:
        kiro_prerequisite_service = await require_kiro_login(assume_ready=assume_kiro_ready)
    if kiro_prerequisite_service is None:
        kiro_prerequisite_service = await asyncio.to_thread(
            KiroPrerequisiteService,
            assume_ready=assume_kiro_ready,
        )
    app["kiro_prerequisite_service"] = kiro_prerequisite_service
    state.kiro_prerequisite_service = app["kiro_prerequisite_service"]
    # Enterprise build only: the gate starts open on that verified login and
    # closes if it expires. The middleware chain installed below reads it.
    from kiro_crew.dashboard.enterprise_gate_middleware import install_enterprise_gate

    install_enterprise_gate(app, app["kiro_prerequisite_service"])
    # A signed-out bundled kiro-cli-chat fails a prompt as a bare connection
    # error; the ACP layer asks this service's whoami verdict to tell it apart.
    from kiro_crew.kiro_prerequisite import register_turn_auth_service

    register_turn_auth_service(state.kiro_prerequisite_service)
    # Seed the retirement baseline with the account on disk RIGHT NOW, before
    # anything can spawn a kiro-backed child. Every child postdates this read,
    # so the once-per-lifetime unset-baseline boot sweep -- which on a live
    # gateway can never satisfy its completion precondition and degenerates
    # into a retire/respawn loop -- is unnecessary: a real account change after
    # this still compares unequal and sweeps. A store that cannot be
    # fingerprinted refuses the seed and keeps the fail-safe sweep.
    await app["kiro_prerequisite_service"].seed_sessions_baseline()
    # Stamp every kiro-backed spawn with the account the store holds at that
    # moment: the turn gate compares those stamps against its fresh read, so a
    # child from an account round trip NO read ever observed -- the one case
    # the seeded baseline and the interim latch are both blind to -- is still
    # retired before reuse (see flag_identity_stamp_mismatches). Unwired (the
    # CLI, tests), spawns stay unstamped and keep the pre-stamping behavior.
    state.sessions.spawn_identity_reader = app["kiro_prerequisite_service"].read_spawn_identity
    # Probe Kiro readiness during boot rather than on the dashboard's first
    # status request: the cold probe spawns sandboxed CLI subprocesses and can
    # take seconds, which is what made the first-run setup chrome visible to
    # returning users. Fire-and-forget — a warm-up is never a boot dependency,
    # and the task is cancelled by the service's shutdown hook.
    app["kiro_prerequisite_service"].warm_up()
    state.load_folders()
    # Off-loop: a large cron_folders.json would otherwise block the event
    # loop with synchronous file I/O + JSON parsing during startup.
    await asyncio.to_thread(state.load_cron_folders)
    # Off-loop: a large chat_pins.json must not block the event loop at startup.
    await asyncio.to_thread(state.load_chat_pins)
    # chat_reactions.json is loaded lazily on the first reaction request
    # (state.ensure_chat_reactions_loaded), not here: a stored-data-sized parse
    # must not run on the gateway boot path before the site starts listening.
    # Off-loop: load_tags runs a synchronous save_tags() during load (status
    # back-fill / seed) which fsyncs on the event loop; a large tags.json —
    # including preserved-but-malformed rows — must not stall startup.
    await asyncio.to_thread(state.load_tags)
    app["port"] = port
    app["dashboard_url"] = dashboard_url

    # Route pull-request status deltas to owner websockets. Extracted so the
    # register + shutdown-cleanup contract is unit-testable without booting the
    # whole gateway (see test_wire_status_delta_sink_registers_and_cleans_up).
    _wire_status_delta_sink(app, state)

    _precompute_telemetry(state)

    # MCP tool routes (shared with start_api_server)
    _register_mcp_routes(app)

    # Install persistent log ring buffer (captures logs even when Logs page is closed)
    ring_handler = handlers.install_log_ring_handler()
    if ring_handler:
        ring_handler.set_state(state)

    # Page routes
    # The route table lives in ``dashboard/routes/``, one module per section.
    # aiohttp resolves in REGISTRATION order and several routes rely on a literal
    # path preceding a pattern that would swallow it, so ``register_all`` calls the
    # slices in the table's original sequence -- see that package's docstring.
    register_all(app)

    # Register built-in apps (idempotent — surfaces baked-in features in App Store).
    # Runs on the executor: escalation cleanup can traverse/delete legacy app
    # dirs, which must not block the event loop during startup.
    await asyncio.get_running_loop().run_in_executor(subprocess_executor(), register_builtin_apps)

    # Warm the gate's builtin app names and the materialized-agent snapshot, then
    # reconcile every enabled app's resources; each runs its I/O on the executor.
    await _warm_builtin_app_names()
    await _warm_materialized_agents()
    await _reconcile_app_resources()

    # One-time migration: disable stale deploy_web builtin installs (now core module).
    # Idempotent — logs once and silently succeeds if already gone.
    # R34 F1: the cleanup reads/deletes files under the data dir — run it off
    # the event loop so wedged filesystem I/O cannot block gateway startup.
    from kiro_crew.apps.builtins import _MIGRATED_BUILTINS

    def _run_migrated_cleanup() -> None:
        for _migrated in _MIGRATED_BUILTINS:
            try:
                _result = cleanup_migrated_builtin(_migrated)
                if not _result.ok:
                    logger.warning(
                        "migrated builtin cleanup failed for %s: %s", _migrated, _result.error
                    )
                elif _result.message and "cleaned up" in _result.message:
                    logger.info("migrated builtin cleanup: %s — %s", _migrated, _result.message)
            except Exception:  # noqa: BLE001
                logger.debug("migrated builtin cleanup skipped for %s", _migrated)

    await asyncio.to_thread(_run_migrated_cleanup)

    # Core deploy module routes (folded from deploy_web app)
    _register_deploy_routes(app)

    # Core deploy skills — symlink into <home>/skills/ so the agent can load them.
    # Offloaded: copytree/rmtree/stat are blocking filesystem calls.
    await asyncio.to_thread(_register_deploy_skills)

    # Knowledge Library. ``setup_knowledge_routes`` reads the lazy store, whose
    # constructor prepares and opens SQLite files (owner-only preparation,
    # schema DDL, migrations): that is file I/O, so it is built on a worker
    # here and route registration finds it ready. Same work, off the loop.
    await asyncio.to_thread(lambda: state.knowledge_store)
    setup_knowledge_routes(app)
    setup_weixin_routes(app)
    setup_feedback_routes(app)
    setup_secrets_routes(app)
    setup_whatsapp_routes(app)

    # Link previews (chat unfurl). Route is always registered; the handler gates
    # itself on cfg.dashboard.link_previews, so toggling the feature needs no
    # gateway restart.
    setup_link_meta_routes(app)

    # Reserve the dashboard port BEFORE the app-backend boot pass below, and
    # publish the reserved socket's REAL name as bound-port evidence (the
    # origin/proof injection in apps.backend is fail-closed on
    # KIROCREW_BOUND_PORT). Bound-and-LISTENING is the point: this gateway
    # OWNS the port kernel-hard — a squatter cannot overlap-bind it while
    # backends spawn trusting its value, which is what made exporting the mere
    # CONFIGURED port a credential-exposure window (a backend would present
    # its X-App-Secret to whatever answered there). Nothing is served yet —
    # connections queue in the backlog until the runner wraps this socket and
    # starts accepting — so no HTTP lifecycle handler can race the boot pass,
    # and an early child callback waits instead of being refused. Binding here
    # also makes --port auto (port == 0) real before the spawn: every
    # boot-spawned backend gets the true origin, fixed and auto alike.
    _dashboard_sock = await _reserve_dashboard_port(bind_address_for(local_only), port)
    runner: web.AppRunner | None = None
    try:
        _bind_ip = _export_reserved_bind_evidence(_dashboard_sock)

        await _start_app_backends(app, state)

        # Edition-contributed dashboard routes + background services (CPP
        # DashboardContributor seam). The Default contributes nothing, so the public
        # dashboard is unchanged. Routes are mounted HERE — before the SPA static
        # catch-all below and well before ``runner.setup()`` freezes the route table
        # and the on_startup/on_cleanup signal lists (see _register_instances_hooks).
        # Fail-closed: a non-standalone host that cannot compose its companion raises.
        safe_context_call(
            lambda: current_context().dashboard.contribute_routes(app),
            fallback=None,
            log_message="dashboard.contribute_routes failed; no edition routes mounted",
        )

        # The service lifecycle hooks are async; they route through
        # ``async_safe_context_call`` so they share the SAME fail-closed discipline as
        # every sync seam call (re-raise ``PlatformCompositionError`` from a host that
        # could not compose its companion; degrade any other transient service error,
        # logged, rather than bricking the gateway start/stop) — kept in one place so
        # a future fail-closed policy change cannot diverge per hand-written copy.
        async def _contrib_startup(app_: web.Application) -> None:
            await async_safe_context_call(
                lambda: current_context().dashboard.start_services(app_),
                fallback=None,
                log_message="dashboard.start_services failed; no edition services",
            )

        async def _contrib_shutdown(app_: web.Application) -> None:
            await async_safe_context_call(
                lambda: current_context().dashboard.stop_services(app_),
                fallback=None,
                log_message="dashboard.stop_services failed",
            )

        app.on_startup.append(_contrib_startup)
        app.on_cleanup.append(_contrib_shutdown)

        # Static files — the React dist/ build, registered whether or not it is
        # built yet (each route resolves static/dist per request), then static/.
        _register_dist_static_routes(app, _DIST_DIR)
        if _STATIC_DIR.is_dir():
            app.router.add_static(
                "/static",
                _STATIC_DIR,
                show_index=False,
                append_version=True,
            )
        else:
            logger.warning("Static dir not found: %s", _STATIC_DIR)

        # ── Middleware ────────────────────────────────────────────────────────────

        # The static handler's FileResponse decides 200-vs-404 only in prepare(),
        # after the header middleware has already stamped immutable. This hook sees
        # the final status and strips immutable from any /assets/ error.
        _install_asset_cache_control_finalizer(app)

        # Tailnet origin (RFC §4): this machine's own MagicDNS name, so
        # `tailscale serve` works without the operator hand-writing dashboard.url.
        # Off by default; resolved in a thread so the daemon call cannot stall the
        # loop; "" whenever Tailscale is absent, stopped, or produced nothing that
        # validated.
        _cfg = KiroCrewConfig.load()
        _ts_cfg = _cfg.dashboard.tailscale
        _tailnet_host = await tailnet.resolve_tailnet_host(_ts_cfg.enabled)
        _tailnet_trust = await _resolve_tailnet_trust(_cfg)
        if _tailnet_host:
            logger.info(
                "tailnet access enabled: trusting origin https://%s (bind and auth unchanged)",
                _tailnet_host,
            )
        # Keep the initial snapshot on both startup surfaces for compatibility.
        # Runtime-aware handlers read the mutable state installed below, which can
        # acquire one validated origin after a Tailscale/Gateway boot race.
        app["tailnet_host"] = _tailnet_host
        app["tailnet_resolved_at"] = int(time.time()) if _tailnet_host else 0
        # The governance-filtered identity-trust value the middleware was built
        # with, for handlers the middleware bypasses (POST /api/auth/refresh must
        # re-bind a rotated access token to the same verified peer identity).
        app["tailnet_trust"] = _tailnet_trust
        # The relay client's door into this app (dashboard/kiro_relay). Built off:
        # the relay client, and the setting that turns this on, arrive together in a
        # later change. Off, the ingress middleware hands every request on to the
        # client-sent relay-header strip.
        app[RELAY_INGRESS_APP_KEY] = RelayIngress(enabled=False)
        app["allowed_origins"] = build_allowed_origins(
            port, local_only, configured_host, tailnet_host=_tailnet_host
        )
        # Exposed to handlers (e.g. knowledge.pick_folder) that only make sense when
        # the browser and gateway are co-located on localhost.
        app["local_only"] = local_only

        # DNS-rebinding defense-in-depth — shared factory (single source of truth
        # for the barrier AND the PROBE_PATHS exemption; see
        # _make_host_validation_middleware).
        host_validation_middleware = _make_host_validation_middleware("dashboard_user")
        # Same factory as the headless server's barrier, so the CSRF exemption set is
        # one decision rather than two (see _make_csrf_middleware).
        csrf_middleware = _make_csrf_middleware("dashboard_user")
        # Audit boundary for refusals raised before sel_audit_middleware runs. Same
        # factory as the headless server's, so the guarantee cannot hold on one
        # entrypoint and not the other (see _make_deny_audit_middleware).
        deny_audit_middleware = _make_deny_audit_middleware("dashboard_user")

        # Generate per-session secret for local app / IPC authentication.
        # NOTE: file write (and parent mkdir) deferred until after port bind
        # succeeds — both live in _write_secret_file, offloaded below — to avoid
        # poisoning the secret file when a second instance fails to start and to
        # keep blocking fs I/O off the event loop.
        _secret_path = data_home() / ".local_secret"
        _internal_secret = os.urandom(16).hex()
        app["local_secret"] = _internal_secret

        host_canonical_redirect = _dashboard_canonical_redirect(local_only, state)

        # Warm the auth singletons (signing secret + revoked-nonce store) off the
        # event loop BEFORE building the middleware chain, so no blocking key-file
        # I/O lands on the loop on the first auth op.
        await warm_auth_singletons()

        # Warm the SecurityEventLog singleton off the loop before any handler or
        # middleware can be its first touch, so a first ``log_api_access`` is a
        # non-blocking enqueue on every path — call sites need no per-site
        # ``asyncio.to_thread`` hop. Best-effort inside the helper: a
        # failed warm never blocks readiness.
        await warm_sel_singleton()

        # The ordered chain and the dashboard_url token-auth invariant live with the
        # headless chain in one owner, so the two cannot drift apart unseen.
        _install_dashboard_middlewares(
            app,
            deny_audit_middleware=deny_audit_middleware,
            host_canonical_redirect=host_canonical_redirect,
            host_validation_middleware=host_validation_middleware,
            csrf_middleware=csrf_middleware,
            internal_secret=_internal_secret,
            port=port,
            local_only=local_only,
            tailnet_trust=_tailnet_trust,
            tailnet_host=_tailnet_host,
            configured_host=configured_host,
            dashboard_url=dashboard_url,
        )

        # Register only after the final allowed-origin set is selected.  The startup
        # hook schedules a sleeping background task and returns immediately, so this
        # cannot extend listener startup; cleanup owns cancellation before aiohttp
        # freezes the signal lists in runner.setup().
        tailnet.install_tailnet_origin_recovery(
            app,
            enabled=_ts_cfg.enabled,
            initial_host=_tailnet_host,
            load_enabled=_tailnet_origin_enabled,
        )

        # ── Loop stall watchdog and diagnostic recorder shutdown ─────────────────
        # Registered HERE, before ``runner.setup()`` freezes the app's signal lists;
        # both are created after setup and resolved lazily when the hooks fire.
        _register_watchdog_shutdown(app, state)
        _register_diag_recorder_shutdown(app)

        # ── Prevent-sleep inhibitor shutdown ─────────────────────────────────────
        # Registered HERE (before runner.setup freezes the signal lists) for the
        # same reason as the watchdog hook above. The inhibitor + poll task are
        # created after runner.setup by _arm_prevent_sleep_poll and released here.
        _register_prevent_sleep_shutdown(app, state)
        # Listener guard detach hook -- same ordering constraint; the guard itself
        # is armed after the TCP site binds (below).
        _register_listener_guard_shutdown(app, state)

        _register_kiro_service_shutdown(app)

        # Releases the resident speech model (148MB default, 1.6GB largest) when idle
        # and at shutdown. Registered here, before runner.setup freezes the signal lists.
        _register_stt_hooks(app)
        # Own-address read for the ssh self-target floor, started at boot.
        _register_own_host_warm(app)
        # Live config: one poller for every writer (dashboard, CLI, $EDITOR), started
        # on_startup because it needs the running loop; primed with this boot's config.
        _register_config_watch(app, state, _cfg)

        # ── Instances (multi-instance management) ────────────────────────────────
        # Register the opt-in instances startup/cleanup hooks HERE, before
        # ``runner.setup()`` freezes the app's signal lists. See
        # ``_register_instances_hooks`` for why ordering matters.
        _register_instances_hooks(app, state, port)
        # Install cleanup stays first, before browser relay/session shutdown.
        _register_browser_install_cleanup(app, state)
        _register_browser_view_cleanup(app, state)
        _register_connections_warm_lifecycle(app, state)
        _register_workflow_lifecycle(app, state)
        _register_crewmate_prune_gate(app, state)
        _register_change_card_hook(app)

        # Unix-socket cleanup hook — registered before runner.setup freezes the
        # signal lists; the path itself only becomes known after the site starts
        # (below), hence the holder indirection.
        _unix_socket_holder: dict[str, Path | None] = {"path": None}
        _register_unix_socket_cleanup(app, _unix_socket_holder)

        # Hardened runner: bounds the request-line/header read time (slowloris /
        # CWE-400) and reaps idle keep-alive connections. See dashboard.slowloris.
        # max_field_size is raised from aiohttp's 8190 default so the accumulated
        # shared per-port cookie jar can't 400 at the parser before a handler
        # prunes it (see refresh_tokens.foreign_port_cookies).
        runner = build_hardened_runner(app, max_field_size=_MAX_HEADER_FIELD_SIZE)
        await runner.setup()
        # Serve on the socket reserved BEFORE the app-backend boot pass (see
        # _reserve_dashboard_port above): the socket is already listening, so
        # SockSite.start()'s create_server re-listen is a harmless backlog update
        # and starting to ACCEPT here drains any callbacks that queued during the
        # pass. The origin evidence the backends were spawned with is this
        # socket's own kernel-assigned name.
        site = web.SockSite(runner, _dashboard_sock)
        await site.start()
    except BaseException:
        # Every post-reservation failure must release both the app processes and
        # the socket before another process can claim this trusted origin.
        if runner is not None:
            with contextlib.suppress(Exception):
                await runner.cleanup()
        with contextlib.suppress(Exception):
            await _stop_spawned_backends()
        _dashboard_sock.close()
        raise
    # After the bind, never on the boot path (no-new-work-on-gateway-boot-path):
    # from here an app cron naming a missing folder creates it through the
    # dashboard's own folder service. Boot-time registration stays read-only.
    from kiro_crew.dashboard.handlers.cron_board import wire_app_cron_folder_creator

    wire_app_cron_folder_creator(state)
    # The listener is up -- keep it up. One failed accept() on Windows would
    # otherwise close it for the life of the process (see listener_guard).
    _arm_listener_guard(state, runner, site)
    # One-time prune of the crewmates an enrol-on-mount agent sync generated
    # from the user's own specs (see crewmate_prune_migration). Kicked here,
    # right after the bind, as a tracked background task -- its cost scales
    # with the session count, so it stays off the boot path and nothing here
    # awaits it -- while the gate armed before the bind holds every mutating
    # request until it settles.
    _kick_crewmate_prune(state)
    _kick_pinned_order_load(state)
    # (No _export_bound_port republish here: the reservation above already
    # exported this same socket's name before the spawn pass — the one
    # authoritative write on this path. The headless entrypoint, which binds
    # via _start_site with no reservation step, still exports post-listen.)
    await _start_bound_port_app_backends()
    _kick_default_app_installs(state)
    # Additional kernel-verifiable transport for the internal API (POSIX only;
    # degrades to TCP-only on any failure — see _start_unix_site).
    _unix_socket_holder["path"] = await _start_unix_site(runner, port)
    # Hold the OTHER loopback family too, so a client dialling a NAME cannot be
    # answered by anyone else -- see _start_secondary_loopback_site. None when
    # there is no second listener, and the client then signs in explicitly.
    # Resolved ONCE, and used for both the second bind and the publication below.
    # Under `--port auto` the requested port is 0 and stays 0, so binding the
    # second family on it lands on an unrelated ephemeral port while the sidecar
    # is filed under the real one -- which would publish coverage for an address
    # nothing listens on and leave the real one free for anyone to take.
    _bound_port = _resolved_bound_port(runner, port)
    _second_loopback = await _start_secondary_loopback_site(runner, _bound_port, _bind_ip)

    # Port bind succeeded — now safe to write the secret file. Offloaded:
    # _write_secret_file does blocking fs I/O (os.open/os.close, plus the
    # owner-only lockdown on Windows), so it must not run on the
    # event loop (no-blocking-call-on-event-loop). The port is passed so the
    # credential is published per listener, not only into the shared file every
    # gateway in this data home writes (see _write_instance_credentials). The
    # bound ADDRESS goes with it because a port number names a set of listeners:
    # the same port on another address is a different party, and a client that
    # dialled one must not resolve the other's credential.
    try:
        await asyncio.get_running_loop().run_in_executor(
            subprocess_executor(),
            _write_instance_credentials,
            _secret_path,
            _bound_port,
            _bind_ip,
            _internal_secret,
            (_second_loopback.address,) if _second_loopback else (),
        )
    except OSError:
        await runner.cleanup()
        raise

    # Published -- now the guards may maintain those claims. Recorded after the
    # write, so a claim that never landed is never withdrawn or re-published.
    _note_listener_sidecar(state, "primary", _bound_port, _bind_ip, _internal_secret)
    # The publication above ran in an executor, so it is an AWAIT between each bind
    # and the line that records its claim -- and one failed accept() on Windows
    # closes a LISTEN socket for good while the process lives on. A death inside
    # that await is invisible to both halves: for the second family the guard is
    # not armed yet, and for the primary the guard IS armed but has no claim to
    # withdraw, which a withdrawal answers True without touching the file. Either
    # way the sidecar advertises an address this gateway does not hold, and arming
    # earlier only moves the hole, because the write is in flight either way. So
    # every claim is reconciled against its live socket the moment it is recorded.
    _reconcile_listener_publication(state, "primary", _bound_port, _bind_ip)
    if _second_loopback is not None:
        _note_listener_sidecar(
            state, "secondary", _bound_port, _second_loopback.address, _internal_secret
        )
        _arm_secondary_listener_guard(state, runner, _second_loopback, _bound_port)
        _reconcile_listener_publication(state, "secondary", _bound_port, _second_loopback.address)

    # Listener is bound and credentials are published — now kick the warm
    # crash-residue scavenge. Deliberately NOT an on_startup hook: those run
    # inside runner.setup(), before the bind, and the scavenge's deferred
    # import must never sit in front of the listener
    # (no-new-work-on-gateway-boot-path).
    _kick_workflow_initialization(state)
    _kick_connections_warm_scavenge(state)
    _kick_session_search_index(state)
    _kick_config_watch(app, state)
    _kick_local_decision_model(state)
    # Same shape for the knowledge store's writer-locked orphan sweep: it left
    # the constructor (which runs pre-bind, on the loop) and runs here on a
    # worker thread once requests are already being served.
    _kick_knowledge_orphan_reclaim(state)
    # And for the data home's owner-only mode repair, whose walk grows with the
    # files the user has accumulated.
    _kick_owner_only_sweep(state)
    _kick_display_verdicts(state)
    # Bind the crew-log push to this loop and register it with the session emitter,
    # once the listener is serving: installing it imports and builds the publisher,
    # which the crew log's default-on flag would otherwise put in front of the bind.
    # It is installed here rather than on a first request because the frame exists
    # so a watching client learns of a growth it did not ask for.
    handlers.install_crew_log_publisher(state)
    # The OTel usage export's own subscription to the same bus, gated on telemetry
    # consent rather than on the crew-log flag above: a no-op when
    # ``telemetry.enabled`` is off (the default), so an unopted host installs no
    # subscriber and sends nothing. Kicked as a tracked task off the loop, not called
    # inline: on an opted-in host the first ``get_recorder()`` the install reaches
    # builds the recorder synchronously (SDK import plus the install-id write), which
    # must not sit in front of the publish (``no-new-work-on-gateway-boot-path``). See
    # ``maintenance._kick_otel_crew_log_subscriber``.
    #
    # WHAT THE GATE DOES NOT COVER, said plainly: the usage FOLDS themselves are
    # members of the crew log's own ``_FOLDS``, so they advance in the eager worker's
    # pass and are written into the savepoint on every host that keeps a crew log,
    # consent or none. That cost is a handful of integers per session and the enum
    # ``otel_usage`` folds an agent name down to; only the egress is behind the
    # consent flag. See ``crew_log_export.install_otel_crew_log_subscriber``.
    _kick_otel_crew_log_subscriber(state)

    # Event-loop heartbeat and its off-loop stall watchdog (see the heartbeat owner).
    _loop_watchdog = await _open_loop_watchdog(_launch_environment)
    _heap_trim_maintainer = platform_compat.HeapTrimMaintainer()
    # Held on ``state`` so the task is not collected.
    state._loop_heartbeat = _start_loop_heartbeat(state, _loop_watchdog, _heap_trim_maintainer)

    # ── Prevent-sleep poll ───────────────────────────────────────────────────
    # Keep the host awake while a turn is in flight (opt-in via
    # dashboard.prevent_sleep), or while the dashboard is published on the
    # tailnet (opt-out via dashboard.tailscale.keep_awake). Shared with the
    # headless --slack-only entrypoint.
    _arm_prevent_sleep_poll(state, port)

    # Arm the stall watchdog only when faulthandler is enabled — i.e. under the
    # real gateway entrypoint (see cli `gateway` dispatch). Tests that spin up
    # the dashboard directly don't enable faulthandler, so they don't leak a
    # watchdog thread; the heartbeat still beats it harmlessly.
    if faulthandler.is_enabled():
        _loop_watchdog.start()
    # Stopped on shutdown via the ``_watchdog_shutdown`` on_cleanup hook,
    # which is registered before ``runner.setup()`` freezes the signal lists.

    # ── Diagnostic recorder ──────────────────────────────────────────────────
    # On by default, per the design: the recorder starts no process-killing timer,
    # and a test that spins the dashboard up directly gets a task it cancels on
    # cleanup rather than a leaked thread. The switch is read HERE, before the
    # import, so an operator who turned it off pays neither the import nor the
    # construction on the boot path. The off values are spelled out rather than
    # imported from the module, because importing it is the cost being avoided.
    _diag_off = os.environ.get("KIROCREW_DIAG_RECORDER", "").strip().lower() in (
        "0",
        "false",
        "no",
        "off",
    )
    if not _diag_off:
        _start_diag_recorder(state)
    state._loop_watchdog = _loop_watchdog  # prevent GC; stop on cleanup

    # Surface any prior crash dump from a previous gateway session.
    await _report_prior_crash_dump(state)

    # Fire background MCP probe at startup (non-blocking). The probe spawns a
    # handshake subprocess per configured MCP server, so under cautious boot it
    # gets its own launch window instead of landing on top of the app backends.
    await cautious_boot.pause_before("MCP server probe")
    # Held in state._background_tasks: the loop keeps only a weak reference to a
    # task, so a dropped one can be garbage-collected before the probe finishes.
    probe_task = asyncio.create_task(handlers._bg_mcp_probe(state.sessions))
    state._background_tasks.add(probe_task)
    probe_task.add_done_callback(state._background_tasks.discard)

    # Refresh config.json's meta stamp when an upgrade left it naming the
    # previous build. Post-bind and fire-and-forget (never awaited on
    # the boot path), and the file I/O runs in a thread so the version check —
    # one small fixed-path file, O(1), rewrite only on mismatch — never holds
    # the event loop. Two locks cover both writer generations: the refresh
    # itself goes through update_config_locked (sidecar advisory lock), and
    # the loop-side asyncio config lock is held around the off-thread call so
    # the legacy writers that serialize on that lock alone cannot land inside
    # the refresh's read→write window. Best-effort: a stale stamp is a
    # diagnostic blemish, so a failure here is logged and boot proceeds.
    async def _refresh_meta_stamp() -> None:
        try:
            async with handlers._get_config_lock():
                if await asyncio.to_thread(refresh_config_meta_stamp):
                    logger.info("config.json meta stamp refreshed to the running version")
        except Exception:
            logger.debug("config meta stamp refresh failed", exc_info=True)

    _stamp_task = asyncio.create_task(_refresh_meta_stamp())
    state._background_tasks.add(_stamp_task)
    _stamp_task.add_done_callback(state._background_tasks.discard)

    # Start terminal orphan reaper (kills PTYs with no WS past the reaper window)
    _reaper = asyncio.create_task(handlers.reap_orphaned_terminals(app))
    _reaper.add_done_callback(lambda t: t.result() if not t.cancelled() else None)
    state._terminal_reaper = _reaper  # prevent GC

    # Point every `playwright-cli` invocation at the service-owned snapshot
    # directory, and keep that directory bounded.
    #
    # The variable goes on the GATEWAY's own environment because the agent runs the
    # CLI as a shell command in a descendant process: an env var is the only channel
    # that reaches an invocation the gateway never constructs. An invocation that
    # misses it writes into whatever directory the agent happened to be in, where
    # the pruner does not look and files accumulate without bound. The CLI accepts
    # this only as an env var, since `--config` is rejected on the follow-up
    # commands that make up most of a session.
    os.environ.update(browser_cli_snapshots.cli_env_overrides())
    # The optional attach token rides the same channel for the same reason: the
    # agent runs the CLI as a shell command, so only an inherited environment
    # reaches it. Absent by default, in which case this adds nothing.
    os.environ.update(browser_cli_token.cli_env_overrides())
    # Name the engine Kiro Crew actually installs. The CLI's own default is the
    # branded Chrome channel at an OS path the product never provisions, so
    # without this the first browse fails on a host where every readiness signal
    # is honestly green. Same channel and same reason as the two above; defers to
    # an operator who set the variable themselves.
    #
    # Off the event loop: computing the override writes the config file, and this
    # runs on the gateway's startup path.
    os.environ.update(await asyncio.to_thread(browser_cli_launch.cli_env_overrides))
    _snap_pruner = asyncio.create_task(_prune_browser_snapshots_loop())
    _snap_pruner.add_done_callback(lambda t: t.result() if not t.cancelled() else None)
    state._browser_snapshot_pruner = _snap_pruner  # prevent GC

    # Start terminal title poller (pushes live foreground-command / cwd titles)
    _title_poller = asyncio.create_task(handlers.poll_terminal_titles(app))
    _title_poller.add_done_callback(lambda t: t.result() if not t.cancelled() else None)
    state._terminal_title_poller = _title_poller  # prevent GC

    # Start periodic flush loop for crash protection (saves dirty slots every 5s)
    state.start_flush_loop()

    # Restore sessions — always restore foldered/pinned sessions; optionally restore recent ones.
    # NOTE: Even with restore_sessions=false, foldered and pinned sessions are restored
    # so the Explorer tree stays populated.  Users can unpin or remove from folder to dismiss.
    cfg = KiroCrewConfig.load()
    # Offloaded: arming has a fail-closed ``approval_modes`` gate, so
    # ``grant_declared_yolo`` resolves governance -- an ``iterdir`` + per-file
    # ``stat`` walk of the profiles dir. We are inside ``async def start_dashboard``,
    # so running it inline stalls the gateway's loop, and on slow storage it stalls
    # the heartbeat with it.
    await asyncio.to_thread(_apply_startup_yolo, state, cfg)

    # Wire safety override expiry notifications
    def _on_override_expired(source: str) -> None:
        """Notify all interfaces when safety override expires.

        Runs the inherited-trust teardown first so a TTL lapse -- which reaches this
        directly, with no separate synchronous call -- still clears everything. A
        policy revocation has already run it inline by the time this fires, and it is
        idempotent, so the two paths need no branch between them.
        """
        _clear_override_derived_trust(state, source)
        state.broadcast_ws("yolo_expired", {"source": source})
        state.push_slots_update()
        # Slack notification (prevent GC with background_tasks set)
        _dispatch_override_expiry_notification(
            state, functools.partial(_notify_slack_override_expired, state), source
        )
        # An expiry that lands on an unattended run is the one case that cannot
        # self-report: nobody is present to answer the prompts it produces.
        _notify_unattended_expiry(state, source)

    safety_override().on_expired = _on_override_expired
    # The synchronous half, for the one caller that cannot wait for the loop: a
    # ceiling install that denies ``yolo`` revokes from whatever thread installed it.
    safety_override().on_policy_revoked = functools.partial(_clear_override_derived_trust, state)
    # The pre-publication half: suspend inherited slot trust while a new ceiling is
    # being resolved, and get it back if the ceiling still permits.
    safety_override().on_policy_suspend = functools.partial(_suspend_override_derived_trust, state)

    await _notify_restart_dropped_grant(state)

    # Converge leftover channel transcript copies BEFORE the restores read them.
    await _converge_channel_transcripts(state)

    # Move every existing crewmate still bound to an agent onto its worker and
    # its own file, carrying that agent's kit, so the product never carries two
    # kinds of crewmate at once. Additive and idempotent (see
    # mate_config_migration.py), so a failure here is logged and left for the
    # next boot rather than blocking startup.
    # The move is one way: nothing undoes it.
    try:
        await asyncio.to_thread(migrate_existing_crewmates, cfg)
    except Exception:
        logger.warning("mate-owned-config migration failed", exc_info=True)

    await _restore_dashboard_sessions(state, cfg)

    # Crew Mode's start pass (learn closed roots, move roots onto a conductor an
    # update changed) and its retry of busy roots, now that the restore has rebuilt
    # the slot set it reads. The dashboard alone starts it: a headless gateway
    # restores no slots, so it would have nothing to move and a name to misrecord.
    from kiro_crew.dashboard import crew_mode_migration

    _crew_task = asyncio.create_task(crew_mode_migration.run(state))
    state._background_tasks.add(_crew_task)
    _crew_task.add_done_callback(state._background_tasks.discard)

    # A crewmate the migration above moved tells its user once, in its own
    # thread; a thread not open yet is offered it at its next turn end.
    from kiro_crew.dashboard.mate_thread_wakes import offer_upgrade_notices

    _notice_task = asyncio.create_task(offer_upgrade_notices(state))
    state._background_tasks.add(_notice_task)
    _notice_task.add_done_callback(state._background_tasks.discard)

    # Relaunch agents in non-archived channels. A gateway defers this batch
    # until its restore/open task completes; a standalone dashboard preserves
    # the existing immediate behavior.
    from kiro_crew.channel import ChannelManager, run_channel_agent
    from kiro_crew.dashboard.handlers_channel import _spawn_agent_task

    mgr = ChannelManager(
        broadcast_fn=state.broadcast_ws,
        max_channels=cfg.agent.max_channels,
        max_agents=cfg.agent.max_channel_agents,
    )
    state.channel_manager = mgr
    restored_agents = [
        (channel.id, agent_id, agent)
        for channel in mgr._channels.values()
        for agent_id, agent in channel.members.items()
    ]

    def _resume_channel_agents() -> None:
        for channel_id, agent_id, restored_agent in restored_agents:
            channel = mgr.get(channel_id)
            if channel is None:
                continue
            agent = channel.members.get(agent_id)
            # A handler may add, dismiss, replace or start an agent while the
            # gateway prepares memory. Resume only the exact object loaded at
            # construction, and never start one a live request already owned.
            if agent is not restored_agent or agent._task is not None:
                continue
            agent.state = "pending"
            _spawn_agent_task(
                agent,
                run_channel_agent(agent, channel, state.sessions, is_yolo=lambda: state._yolo),
            )

    if defer_channel_agent_resume:
        # The gateway resumes them after its memory barrier, behind
        # ``await_crewmate_prune_settled`` (GatewayOrchestrator.run).
        state.resume_channel_agents = _resume_channel_agents
    else:
        # A resumed channel agent binds its crewmate to a session; the prune
        # must have judged every candidate before that binding can appear.
        await await_crewmate_prune_settled(state, before="channel agent resume")
        _resume_channel_agents()

    # ── AEA Tunnel ───────────────────────────────────────────────────────────
    await _start_aea_tunnel(app, state, cfg, port)

    # Boot-to-ready (rec #1): full dashboard init is complete and the server is
    # about to accept traffic. Privacy-safe — the only labels are the fixed
    # ``server``/``outcome`` enums. Best-effort; never blocks the return.
    # Publish the gateway's shared memory task first and do not yield between
    # these assignments. create_task cannot enter its restore/open worker until
    # this coroutine yields back to the gateway after returning the ready state.
    if schedule_memory_preparation is not None:
        state.memory_startup_task = schedule_memory_preparation()
    state.ready = True
    record_boot_to_ready((time.time() - state.start_time) * 1000.0, server="dashboard")
    # Tells the NEXT boot that this instance got the whole startup battery
    # away, so a stall from here on does not implicate the battery.
    _dispatch_healthy_boot_marker(state)

    return runner, state


# Every function the owners define runs on this module's globals, so a patch of
# ``kiro_crew.dashboard.server.<name>`` reaches it wherever it lives; see
# ``kiro_crew.dashboard.server_runtime``. Run once, after this body has bound every
# name.
_facade_compose(
    globals(),
    (
        _owner_api_entrypoint,
        _owner_app_platform,
        _owner_browser_lifecycle,
        _owner_config_watch,
        _owner_crewmate_prune,
        _owner_diagnostics,
        _owner_heartbeat,
        _owner_inherited_trust,
        _owner_instances_lifecycle,
        _owner_listener,
        _owner_listener_claims,
        _owner_maintenance,
        _owner_mcp_routes,
        _owner_middleware_chain,
        _owner_owner_notices,
        _owner_prevent_sleep,
        _owner_safety_grants,
        _owner_security_headers,
        _owner_security_middleware,
        _owner_service_hooks,
        _owner_session_restore,
        _owner_skill_learning,
        _owner_static_assets,
        _owner_stt_hooks,
        _owner_tunnel,
        _owner_workflow_startup,
    ),
)
