"""Construct the orchestrator: the initial value of every field its owners share.

``__init__`` records the run's options and credentials, runs each channel's
hoist, registers the config appliers, and gives every service, task and update
field its not-yet-started value. ``GatewayOrchestrator`` declares those fields
for the type checker; this module assigns them.

Orchestrator fields read: none.
Orchestrator fields written: ``_adaptive_controller``, ``_allowed_users``,
``_app_token``, ``_approval_mode``, ``_auto_migrate_task``, ``_background_tasks``,
``_batch_progress``, ``_bot_token``, ``_cfg``, ``_channel_handles``,
``_channel_restart_gen``, ``_channel_restart_lock``, ``_channel_restart_tasks``,
``_channel_transports_started``, ``_config_subs``, ``_console_script_repair_task``,
``_cron_armed``, ``_cron_injecting``, ``_cron_reconciled``, ``_dashboard_runner``,
``_feature_video_task``, ``_handler_tasks``, ``_inbound_replay_task``, ``_json_ready``,
``_mandatory_update_deferred_at``, ``_mandatory_update_deferred_key``,
``_marker_clear_pending``, ``_marker_write_task``, ``_mcp_gateway_manager``,
``_mcp_launch_approval_ready``, ``_mcp_resolve_home``, ``_mcp_resolve_prefetch``,
``_mcp_target_env``, ``_memory_repair_cursor``, ``_memory_repair_stop``,
``_memory_repair_task``, ``_memory_startup``, ``_memory_startup_task``,
``_model_download_task``, ``_no_crons``, ``_no_dashboard``, ``_no_open``,
``_open_channels``, ``_owner_id``, ``_pending_queue``, ``_pending_update_mandatory``,
``_pending_update_mandatory_key``, ``_pending_update_respawn``, ``_port_override``,
``_profile_watch_task``, ``_runner_admission``, ``_runner_admission_adopted``,
``_running_script_ids``, ``_session_environments_ready``, ``_session_tasks``, ``_sign_in_refresher_task``,
``_slack_enabled``, ``_socket_client``, ``_subagent_coalescer_inst``, ``_test_mode``,
``_tracking_channels``, ``_update_apply_deferred``, ``_update_check_task``,
``_wecom_client``, ``_wheel_update_noticed``, ``autonudge_svc``, ``channel_history``,
``consolidator``, ``conv_log``, ``cron_svc``, ``ctx_builder``, ``dashboard_state``,
``heartbeat_svc``, ``secretary_svc``, ``sessions``, ``slack``, ``slack_command``,
``subagent_mgr``, ``task_runner``.

Composed by :mod:`kiro_crew.slack.gateway`, whose globals its functions run on;
see :mod:`kiro_crew.slack.gateway_runtime`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import asyncio
    import threading

    from kiro_crew.slack.gateway import (
        CRED_OWNER_ID,
        CRED_SLACK_APP_TOKEN,
        CRED_SLACK_BOT_TOKEN,
        GatewayOrchestrator,
        KiroCrewConfig,
        Mapping,
        config_dir,
        logger,
    )


def __init__(
    self: GatewayOrchestrator,
    cfg: KiroCrewConfig,
    *,
    no_dashboard: bool = False,
    no_crons: bool = False,
    no_open: bool = False,
    port_override: str | None = None,
    json_ready: bool = False,
    approval_mode: str | None = None,
    test_mode: bool = False,
    credentials: Mapping[str, str] | None = None,
) -> None:
    # NOTE: test_heartbeat_prompt_deliver.py creates instances via __new__
    # (bypassing __init__). Update that fixture if new attributes are added.
    self._cfg = cfg
    self._no_dashboard = no_dashboard
    self._no_crons = no_crons
    self._no_open = no_open
    self._port_override = port_override
    self._json_ready = json_ready
    self._approval_mode = approval_mode
    self._test_mode = test_mode
    # *credentials* is ``load_credentials()`` already run off the event loop
    # (``run_gateway``): resolving a secret:// reference reads the vault.
    creds = dict(credentials) if credentials is not None else cfg.load_credentials()
    self._app_token = creds.get(CRED_SLACK_APP_TOKEN, "")
    self._bot_token = creds.get(CRED_SLACK_BOT_TOKEN, "")
    self._owner_id = creds.get(CRED_OWNER_ID, "")
    # Multi-user access is disabled — only owner is authorized.
    # Prune stale allowed_users entries from config and warn.
    stale = {u["slack_id"] for u in cfg.slack.allowed_users} - (
        {self._owner_id} if self._owner_id else set()
    )
    if stale:
        logger.warning(
            "Pruning %d stale allowlist entries (multi-user disabled): %s",
            len(stale),
            stale,
        )
    self._allowed_users = {self._owner_id} if self._owner_id else set()
    self._tracking_channels = {
        c["channel_id"] for c in cfg.slack.tracking_channels if c.get("channel_id")
    }
    self._open_channels = set(cfg.slack.open_channels)
    self._slack_enabled = bool(self._app_token and self._bot_token)
    # One hoist per channel, in roster order. Each reads the credential
    # store + that channel's config section into the ``_<channel>_*``
    # attributes its ``maybe_start_*`` factory consumes. Kept as methods so
    # ``restart_channel`` can re-run exactly one of them against a fresh
    # config when a connection parameter changes.
    self._hoist_wecom(cfg, creds)
    self._hoist_telegram(cfg, creds)
    self._hoist_weixin(cfg, creds)
    self._hoist_whatsapp(cfg, creds)
    self._hoist_feishu(cfg, creds)
    self._hoist_discord(cfg, creds)
    self._hoist_webex(cfg, creds)
    self._hoist_imessage(cfg, creds)
    self._hoist_teams(cfg, creds)
    self.slack_command = cfg.slack.command
    # Serializes restart_channel: two config writes landing within one
    # poll interval must close-then-start the same channel in order.
    self._channel_restart_lock = asyncio.Lock()
    # Set once _start_channel_transports has run; the restart applier defers
    # before that, because the boot loop is about to start every channel
    # from the hoist above and a concurrent start would double it.
    self._channel_transports_started = False
    self._channel_restart_tasks = set()
    self._channel_restart_gen = {}
    self._session_environments_ready = False
    self._config_subs = []
    self._register_config_appliers()

    # Services (initialized in start())
    self.slack = None
    self.sessions = None
    self.ctx_builder = None
    self.conv_log = None
    self.consolidator = None
    self.cron_svc = None
    self._cron_reconciled = False
    self._cron_armed = False
    self.heartbeat_svc = None
    # Declared here, not just assigned in `_init_autonudge`: that method
    # returns early when `KIROCREW_AUTONUDGE=0`, BEFORE its only assignment,
    # so with the flag off the attribute never existed at all -- and the
    # seven `if self.autonudge_svc:` sites in the loop-CRUD handlers below
    # would raise AttributeError rather than read a default.
    self.autonudge_svc = None
    # Secretary runtime service removed (Amazon-internal). Attribute stays
    # as an inert None so other modules referencing it degrade gracefully.
    self.secretary_svc = None
    self.subagent_mgr = None
    # Adaptive concurrency controller: started by _start_adaptive_controller
    # after dashboard and task-store readiness; stopped in shutdown before
    # the manager is cancelled so no actuator fires into a closing manager.
    self._adaptive_controller = None
    self._subagent_coalescer_inst = None
    # Wave accounting for the completion digest (batch_id -> progress).
    self._batch_progress = {}
    self._cron_injecting = {}  # parent_key → pending injection count
    self._running_script_ids = set()  # job IDs with in-flight script/command execution
    self.task_runner = None
    # Runner admission over the task queue, attached by _wire_runner_admission
    self._runner_admission = None
    # Whether the consumers' orphan-adoption sweep has already been handed
    # out over a store that existed. The first wiring pass can run while the
    # store is still opening off the loop, and adopting nothing is not the
    # same as having adopted: _runner_admission_store_ready re-attaches then,
    # and this keeps that from becoming a SECOND sweep over the same rows.
    self._runner_admission_adopted = False
    self.channel_history = None
    self.dashboard_state = None
    self._background_tasks = set()  # prevent GC of fire-and-forget tasks
    # Approval-state persistence is scheduled while the MCP broker starts,
    # but its task waits for the dashboard/API readiness boundary.
    self._mcp_launch_approval_ready = asyncio.Event()
    self._memory_startup = None
    self._memory_startup_task = None
    self._memory_repair_task = None
    self._memory_repair_stop = threading.Event()
    self._memory_repair_cursor = ""
    # Dedicated ownership for the repair's dep_sync/pip process tree. The
    # general set only prevents task GC; shutdown must cancel and await this
    # task so _check_console_script can kill and reap its child group.
    self._console_script_repair_task = None
    self._marker_write_task = None
    # Shutdown waits on this task so an in-flight sign-in renewal can
    # persist its replacement credential before the hard exit.
    self._sign_in_refresher_task = None
    # Set by the shutdown path when the marker write is still in flight:
    # tells the writer thread to self-clear after publishing, closing the
    # write-after-clear race without any event-loop dependency.
    self._marker_clear_pending = threading.Event()
    self._dashboard_runner = None
    # Enterprise build: the service whose boot probe saw the kiro login, handed
    # to the dashboard so its gate starts open on that verdict. None in oss.
    self._boot_kiro_service = None
    self._handler_tasks = set()  # type: ignore[type-arg]
    self._session_tasks = {}  # type: ignore[type-arg]
    self._pending_queue = {}
    self._socket_client = None
    self._wecom_client = None  # set by maybe_start_wecom
    # Registry-owned live channel handles ({channel_type: client}). The
    # per-channel _<type>_client attributes are legacy mirrors kept in sync
    # by messaging.registry.start_channels until the config schema retires
    # them; shutdown closes through THIS dict.
    self._channel_handles = {}
    # Detached boot task that replays the durable inbound spool.
    # Held on the instance so the task is not garbage-collected mid-flight.
    self._inbound_replay_task = None
    self._model_download_task = None
    self._feature_video_task = None
    self._auto_migrate_task = None
    # Recurring update coordinator, started after signal handlers are
    # installed. Cancelled on shutdown so stalled network or installer work
    # cannot hold the process open.
    self._update_check_task = None
    # Periodic governance-profile re-projection; cancelled at shutdown beside the
    # distribution refresher, so no rebuild starts into a closing gateway.
    self._profile_watch_task = None
    self._update_apply_deferred = False
    self._pending_update_respawn = None
    # Whether the pending restart serves a policy floor, and its target key:
    # kept beside the respawn so every retry runs under the same mandatory
    # grace instead of restarting the clock.
    self._pending_update_mandatory = False
    self._pending_update_mandatory_key = ""
    self._mandatory_update_deferred_at = None
    self._mandatory_update_deferred_key = None
    # Managed-venv notices already sent in this process, so each reaches the
    # operator once per release: the signed metadata excludes this host
    # (``incompatible:<version>``), or applying it would detach the
    # sandbox's AppArmor profile (``reattach:<version>``). Nothing here
    # suppresses an attempt: incompatibility is decided again, before any
    # download, on every cycle.
    self._wheel_update_noticed = set()
    self._mcp_gateway_manager = None
    # Detached pre-resolve pass for npm-launcher MCP targets. Held so the
    # loop keeps a strong reference (a bare create_task is only weakly held)
    # and so broker shutdown can cancel an install still in flight.
    self._mcp_resolve_prefetch = None
    # The rewriter's ``KIROCREW_MCP_TARGET_*`` mapping from the last broker
    # start, kept so an explicit refresh resolves the same launches the
    # daemon is actually serving rather than a freshly re-derived guess.
    self._mcp_target_env = {}
    # Resolved here, in sync construction, because config_dir() does file IO
    # and must never be called from an async path. The store
    # lives beside the rest of the data home for the life of the process.

    self._mcp_resolve_home = str(config_dir())
