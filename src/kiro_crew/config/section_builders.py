"""Build a ``config.json`` section's DTO from its raw dict, one frame per section.

The loader's ``build_config`` hands every builder the section it already
extracted and degraded; a builder reads that dict through
``sections.SectionReader``, which resolves an omitted key and a coercer's
fallback to the default declared on the DTO's field (the few deliberate
departures are listed in the config spec), and returns a fresh dataclass, so a
load never shares a mutable value with another load. Builders are grouped by the
module that owns their section's DTO.

This module holds 27 of the loader's ``_build_*`` helpers. Four stay in
``config.loader`` because their placement is pinned there: agent (the
harness-parity review scope), session (its pool-size fallback reads the
loader's ``DEFAULT_POOL_SIZE`` at call time), telemetry (the metrics spec names
the loader as its parser) and dashboard (the feature map cites the loader's
``folder_sort`` read). Sections with no helper are built inline in
``build_config`` or by their DTO's own constructor. A name this module reads is
patched here, not on the loader. This module imports neither the loader nor
schema/validation.
"""

from __future__ import annotations

# Computer-use ceilings come from the feature's constants module rather than
# being re-spelled here (docs/system-specs/common/code-style.md: no hardcoded
# values in business logic); its defaults are the ComputerUseConfig fields'.
# ``computer_use.types`` is deliberately dependency-free — it imports nothing from
# ``kiro_crew`` — so this cannot create an import cycle with the config package, and the
# ``computer_use`` package's ``__init__`` pulls in only ``platform_compat`` /
# ``executors`` (both stdlib-only), never ``config``.
from kiro_crew.computer_use.types import MAX_SCREENSHOT_MAX_PX as _CU_MAX_SCREENSHOT_MAX_PX
from kiro_crew.computer_use.types import MAX_TEXT_LIMIT as _CU_MAX_TEXT_LIMIT
from kiro_crew.computer_use.types import MAX_TREE_DEPTH_LIMIT as _CU_MAX_TREE_DEPTH
from kiro_crew.computer_use.types import MAX_TREE_NODES_LIMIT as _CU_MAX_TREE_NODES
from kiro_crew.computer_use.types import MIN_SCREENSHOT_MAX_PX as _CU_MIN_SCREENSHOT_MAX_PX
from kiro_crew.config import sections as _sections
from kiro_crew.config.fields import (
    _coerce_int,
    _safe_bool,
    _safe_dict,
    _safe_float,
    _safe_int,
    _safe_list,
    _safe_nonnegative_int,
)
from kiro_crew.config.integration_sections import (
    ComputerUseConfig,
    InstancesConfig,
    McpConfig,
    McpGatewayConfig,
    PublishConfig,
    TunnelConfig,
    _resolve_stub_overrides,
    _resolve_stub_roster,
    _resolve_stub_servers,
)
from kiro_crew.config.memory_sections import (
    KnowledgeConfig,
    MemoryConfig,
    SessionSummaryConfig,
    SkillsConfig,
    _coerce_embedding_provider,
    _read_auto_add_documents,
)
from kiro_crew.config.sections import (
    DEDUP_EVERY_N_SWEEPS_MAX,
    EMBED_RATE_LIMIT_MAX,
    EXTRACTION_POOL_SIZE_MAX,
    EXTRACTION_POOL_SIZE_MIN,
    FOLDER_INGEST_CHUNK_BUDGET_MAX,
    IMPORT_CHUNK_BUDGET_MAX,
    SWEEP_CHUNK_BUDGET_MAX,
    DiscordConfig,
    FeishuConfig,
    IMessageConfig,
    SectionReader,
    SlackConfig,
    SttConfig,
    TeamsConfig,
    TelegramConfig,
    WakaTimeConfig,
    WebexConfig,
    WeComConfig,
    WeixinConfig,
    WhatsAppConfig,
    _coerce_int_ids,
    _coerce_opaque_str_ids,
    _coerce_session_folder,
    _coerce_str_ids,
    _coerce_whatsapp_groups,
    _parse_telegram_accounts,
    _threshold_pct,
    _validate_telegram_activation,
    _validate_tracking_channels,
    _validated_stt_model,
    _validated_stt_provider,
    coerce_effort,
)
from kiro_crew.config.service_sections import (
    CronHistoryConfig,
    MessagingConfig,
    MonitoringConfig,
    TaskRunnerConfig,
    WatchdogConfig,
)
from kiro_crew.instances.constants import DEFAULT_CONNECT_TIMEOUT_SECS as _DEFAULT_CONNECT_TIMEOUT
from kiro_crew.instances.constants import DEFAULT_MINT_TIMEOUT_SECS as _DEFAULT_MINT_TIMEOUT

# Runtime-budget policy and coercion live in the monitoring limits leaf module,
# keeping the section builders free of duplicated bounds.
from kiro_crew.monitoring.limits import coerce_runtime_ceiling

# The speech-to-text bounds come from the package that owns them, so a tuning
# knob cannot be clamped past what the session accepts; its defaults are the
# SttConfig fields'. No cycle: the only config dependency anywhere under
# ``kiro_crew.stt`` is the leaf ``config.paths``, never this module.
from kiro_crew.stt.limits import MAX_IDLE_EVICT_SECS as _STT_IDLE_EVICT_SECS_MAX
from kiro_crew.stt.limits import MAX_INTERVAL_MS as _STT_INTERVAL_MS_MAX
from kiro_crew.stt.limits import MAX_TIMEOUT_SECS as _STT_MAX_TIMEOUT_SECS
from kiro_crew.stt.limits import MIN_IDLE_EVICT_SECS as _STT_IDLE_EVICT_SECS_MIN
from kiro_crew.stt.limits import MIN_PARTIAL_INTERVAL_MS as _STT_MIN_PARTIAL_INTERVAL_MS
from kiro_crew.stt.limits import MIN_SILENCE_MS as _STT_MIN_SILENCE_MS
from kiro_crew.stt.limits import MIN_TIMEOUT_SECS as _STT_MIN_TIMEOUT_SECS

# ---------------------------------------------------------------------------
# Service sections (DTOs in ``config.service_sections``).
# ---------------------------------------------------------------------------


def _build_taskrunner_config(taskrunner_data: dict) -> TaskRunnerConfig:
    section = SectionReader(TaskRunnerConfig, taskrunner_data)
    return TaskRunnerConfig(
        max_parallel_steps=section.get("max_parallel_steps"),
        workspace_dir=str(section.get("workspace_dir")),
    )


def _build_messaging_config(messaging_data: dict) -> MessagingConfig:
    section = SectionReader(MessagingConfig, messaging_data)
    return MessagingConfig(
        use_transport=bool(section.get("use_transport")),
        dm_scope=str(section.get("dm_scope")),
        idle_reset_minutes=section.read("idle_reset_minutes", _coerce_int),
        daily_reset_hour=section.read("daily_reset_hour", _coerce_int),
        queue_mode=str(section.get("queue_mode")),
    )


def _build_cron_history_config(cron_history_data: dict) -> CronHistoryConfig:
    section = SectionReader(CronHistoryConfig, cron_history_data)
    return CronHistoryConfig(
        cron_summary_cap=section.read("cron_summary_cap", _safe_int),
        cron_trace_cap_kb=section.read("cron_trace_cap_kb", _safe_int),
        cron_max_records_per_job=section.read("cron_max_records_per_job", _safe_int),
        cron_max_index_records=section.read("cron_max_index_records", _safe_int),
    )


def _build_monitoring_config(data: dict, prefer_structured_arming: bool) -> MonitoringConfig:
    section = SectionReader(MonitoringConfig, data)
    return MonitoringConfig(
        prefer_structured_arming=prefer_structured_arming,
        max_runtime_secs=coerce_runtime_ceiling(section.get("max_runtime_secs")),
    )


def _build_watchdog_config(watchdog_data: dict) -> WatchdogConfig:
    section = SectionReader(WatchdogConfig, watchdog_data)
    return WatchdogConfig(
        check_after_secs=section.read("check_after_secs", _safe_float),
        stale_window_secs=section.read("stale_window_secs", _safe_float),
        tool_stall_suspect_secs=section.read("tool_stall_suspect_secs", _safe_float),
        tool_stall_hard_cap_secs=section.read("tool_stall_hard_cap_secs", _safe_float),
        model_silent_probe_secs=section.read("model_silent_probe_secs", _safe_float),
        remote_flat_probe_secs=section.read("remote_flat_probe_secs", _safe_float),
        wellness_sample_secs=section.read("wellness_sample_secs", _safe_float),
    )


# ---------------------------------------------------------------------------
# Memory sections (DTOs in ``config.memory_sections``).
# ---------------------------------------------------------------------------


def _build_memory_config(memory_data: dict) -> MemoryConfig:
    section = SectionReader(MemoryConfig, memory_data)
    return MemoryConfig(
        embedding_provider=_coerce_embedding_provider(section.get("embedding_provider")),
        embedding_dim=section.get("embedding_dim"),
        embedding_threads=section.read("embedding_threads", _safe_int, 1, 256),
        # 0 is the documented "inherit embedding_threads" sentinel, so the
        # floor is 0 rather than 1 — clamping it to 1 would erase a
        # deliberate opt-in to the interactive pool.
        embedding_bulk_threads=section.read("embedding_bulk_threads", _safe_int, 0, 256),
        embedding_bulk_duty=section.read("embedding_bulk_duty", _safe_float, 0.05, 1.0),
        embed_model_url=section.get("embed_model_url"),
        embed_model_path=section.get("embed_model_path"),
        embed_model_id=section.get("embed_model_id"),
        embed_model_stamp=section.get("embed_model_stamp"),
        embed_model_legacy_ids=section.get("embed_model_legacy_ids"),
        embed_rebuild_generation=section.get("embed_rebuild_generation"),
        semantic_confidence_threshold=section.read(
            "semantic_confidence_threshold", _safe_float, 0.0, 1.0
        ),
        episodic_dedup_threshold=section.read("episodic_dedup_threshold", _safe_float, 0.0, 1.0),
        episodic_max_results=section.read("episodic_max_results", _safe_int, 1, None),
        # Floor of 1, not 0. A normal write keeps the row it writes either way, so a
        # cap of 0 behaves as 1 there: `_enforce_episodic_cap` tombstones every older
        # active V1 row, which is what a cap that small means. A merge-only write is
        # where 0 differs: `active_count >= 0` holds on an empty store, so every one
        # is refused as at-capacity and the ledger import can never index anything.
        episodic_max_count=section.read("episodic_max_count", _safe_int, 1, None),
        decay_rates=(
            dr
            if isinstance(dr := section.get("decay_rates"), dict)
            else section.default("decay_rates")
        ),
        semantic_keys=section.get("semantic_keys"),
        history_idle_hours=section.get("history_idle_hours"),
        history_max_days=section.read("history_max_days", _safe_nonnegative_int),
        backup_enabled=section.read("backup_enabled", _safe_bool),
        backup_keep=section.read("backup_keep", _safe_int, 1, None),
        persistence_enabled=section.read("persistence_enabled", _safe_bool),
        inject_memory=section.read("inject_memory", _safe_bool),
        inject_lessons=section.read("inject_lessons", _safe_bool),
        inject_lessons_per_turn=section.read("inject_lessons_per_turn", _safe_bool),
        inject_activity=section.read("inject_activity", _safe_bool),
        migrated=section.get("migrated"),
    )


def _build_knowledge_config(knowledge_data: dict) -> KnowledgeConfig:
    section = SectionReader(KnowledgeConfig, knowledge_data)
    return KnowledgeConfig(
        auto_ingest_artifacts=bool(section.get("auto_ingest_artifacts")),
        auto_ingest_artifact_kinds=[
            k for k in section.get("auto_ingest_artifact_kinds") if isinstance(k, str)
        ],
        max_ingest_file_mb=(
            float(mb)
            if isinstance(
                (mb := section.get("max_ingest_file_mb")),
                (int, float),
            )
            and not isinstance(mb, bool)
            and mb >= 0
            else section.default("max_ingest_file_mb")
        ),
        embed_timeout_secs=section.read("embed_timeout_secs", _safe_float),
        embed_content_budget=section.read("embed_content_budget", _safe_int),
        pool_idle_ttl_secs=section.read("pool_idle_ttl_secs", _safe_nonnegative_int),
        auto_add_documents=_read_auto_add_documents(knowledge_data),
        folder_ingest_chunk_budget=section.read(
            "folder_ingest_chunk_budget", _safe_nonnegative_int, FOLDER_INGEST_CHUNK_BUDGET_MAX
        ),
        dedup_every_n_sweeps=section.read(
            "dedup_every_n_sweeps", _safe_nonnegative_int, DEDUP_EVERY_N_SWEEPS_MAX
        ),
        doc_ingest_hosts=[
            str(h) for h in section.get("doc_ingest_hosts") if isinstance(h, str) and h.strip()
        ],
        sweep_chunk_budget=section.read(
            "sweep_chunk_budget", _safe_nonnegative_int, SWEEP_CHUNK_BUDGET_MAX
        ),
        import_chunk_budget=section.read(
            "import_chunk_budget", _safe_nonnegative_int, IMPORT_CHUNK_BUDGET_MAX
        ),
        embed_rate_limit=section.read(
            "embed_rate_limit", _safe_nonnegative_int, EMBED_RATE_LIMIT_MAX
        ),
        extraction_model=str(section.get("extraction_model")).strip(),
        extraction_pool_size=max(
            EXTRACTION_POOL_SIZE_MIN,
            min(
                EXTRACTION_POOL_SIZE_MAX,
                section.read("extraction_pool_size", _safe_nonnegative_int),
            ),
        ),
        extraction_effort=coerce_effort(section.get("extraction_effort")),
    )


def _build_skills_config(skills_data: dict) -> SkillsConfig:
    section = SectionReader(SkillsConfig, skills_data)
    return SkillsConfig(
        max_triggered=section.read("max_triggered", _safe_int),
        lazy_load=section.read("lazy_load", _safe_bool),
        auto_create_from_sessions=section.read("auto_create_from_sessions", _safe_bool),
        auto_refine_on_deviation=section.read("auto_refine_on_deviation", _safe_bool),
        auto_min_tool_calls=section.read("auto_min_tool_calls", _safe_int),
        auto_similarity_threshold=section.read("auto_similarity_threshold", _safe_float),
        approval_required=section.read("approval_required", _safe_bool),
        max_auto_skills=section.read("max_auto_skills", _safe_int),
        stale_after_days=section.read("stale_after_days", _safe_int),
        archive_after_days=section.read("archive_after_days", _safe_int),
        pending_ttl_days=section.read("pending_ttl_days", _safe_int),
        generate_scripts=section.read("generate_scripts", _safe_bool),
        judge_model=str(section.get("judge_model") or section.default("judge_model")),
        extra_paths=[p for p in _safe_list(section.get("extra_paths")) if isinstance(p, str)],
        # Security off-switch: malformed values must not become truthy
        # through Python coercion (for example, the string "false").
        project_skills_enabled=section.get("project_skills_enabled") is True,
    )


def _build_session_summary_config(session_summary_data: dict) -> SessionSummaryConfig:
    section = SectionReader(SessionSummaryConfig, session_summary_data)
    return SessionSummaryConfig(
        enabled=bool(section.get("enabled")),
        min_user_turns=section.read("min_user_turns", _safe_int),
        regenerate_after_turns=section.read("regenerate_after_turns", _safe_int),
        max_intents=section.read("max_intents", _safe_int),
        max_constraints=section.read("max_constraints", _safe_int),
        assistant_excerpt_chars=section.read("assistant_excerpt_chars", _safe_int),
    )


# ---------------------------------------------------------------------------
# Integration sections (DTOs in ``config.integration_sections``).
# ---------------------------------------------------------------------------


def _build_mcp_config(mcp_data: dict) -> McpConfig:
    """Build the ``mcp`` section in its own frame (see the compound-section rule)."""
    section = SectionReader(McpConfig, mcp_data)
    return McpConfig(
        # Kept as authored strings — validation (absolute-only, ``~`` expansion,
        # dedup) belongs to the consumer, kiro_crew.env.augmented_path, so the ONE
        # gate the built-in directories already pass applies to these too instead
        # of a second rule drifting here. Non-strings ARE dropped: the field is
        # typed list[str] and to_dict() round-trips it verbatim into the saved
        # config.
        extra_path_dirs=[
            d for d in _safe_list(section.get("extra_path_dirs")) if isinstance(d, str)
        ],
        # ABSENT takes the documented default (on): an ``autoApprove`` the owner
        # wrote is respected, and nobody has to name this key to get that. Opting
        # out takes a real ``false``; a value of the wrong type is removed by the
        # schema validator before this runs, so it reads as absent and the default
        # applies rather than a guess at what the text meant.
        honour_auto_approve=section.get("honour_auto_approve") is True,
    )


def _build_mcp_gateway_config(mcp_gateway_data: dict) -> McpGatewayConfig:
    section = SectionReader(McpGatewayConfig, mcp_gateway_data)
    _spawn_min = max(1, section.read("spawn_concurrency_min", _safe_int))
    _spawn_max = max(_spawn_min, section.read("spawn_concurrency_max", _safe_int))
    return McpGatewayConfig(
        enabled=bool(section.get("enabled")),
        # Absent -> True so installs that never configured this keep
        # rendering. A malformed value cannot be distinguished here: the
        # schema validator REMOVES an invalid value before the loader
        # parses (see config/validation.py ``_apply_field_default``), so a
        # hand-edited ``"false"`` arrives as absent and resolves to True,
        # with a warning logged naming the field. ``_safe_bool`` is
        # belt-and-braces for a schema gap, not the acting guard — the
        # acting guard against a truthy string is the validator, since
        # ``bool("false")`` is True. The write path is where an opt-out is
        # actually enforced: the endpoint rejects any non-boolean body.
        apps_enabled=section.read("apps_enabled", _safe_bool),
        # ON by default. The forwarded set is a strict subset of the
        # hashed set and gatewayd re-hashes the sidecar at spawn,
        # forwarding nothing on mismatch, so a forwarded key is one every
        # co-tenant of that backend declared identically. With it off, one
        # ordinary declared key costs the whole server its pooling.
        #
        # A malformed value never reaches this call: ``config.validation``
        # type-checks first and ``_apply_field_default`` strips a non-boolean so
        # the dataclass default applies, which is why the log says "using
        # default". The fallback here is defence in depth for a bypassed
        # validator, and it is that same field default.
        forward_declared_env=section.read("forward_declared_env", _safe_bool),
        socket_path=str(section.get("socket_path")),
        overlay_dir=str(section.get("overlay_dir")),
        idle_timeout_secs=max(10, section.read("idle_timeout_secs", _safe_int)),
        # 0 is meaningful (re-resolve every pass), so the floor is 0 and
        # not the usual "at least something" clamp.
        resolve_once_refresh_hours=max(0, section.read("resolve_once_refresh_hours", _safe_int)),
        max_backends=max(1, section.read("max_backends", _safe_int)),
        # Admission keys. Clamps mirror the dataclass defaults: floor
        # >= 1, ceiling >= floor, initial inside the band; 0 keeps the
        # "auto" meaning on the host-budget ceilings.
        spawn_concurrency_min=_spawn_min,
        spawn_concurrency_max=_spawn_max,
        spawn_concurrency_initial=min(
            _spawn_max,
            max(
                _spawn_min,
                section.read("spawn_concurrency_initial", _safe_int),
            ),
        ),
        spawn_queue_wait_secs=max(1, section.read("spawn_queue_wait_secs", _safe_int)),
        initialize_timeout_secs=max(1, section.read("initialize_timeout_secs", _safe_int)),
        host_budget_max_procs=max(0, section.read("host_budget_max_procs", _safe_int)),
        host_budget_max_rss_mb=max(0, section.read("host_budget_max_rss_mb", _safe_int)),
        host_budget_max_fds=max(0, section.read("host_budget_max_fds", _safe_int)),
        poolable_servers=[s for s in section.get("poolable_servers") if isinstance(s, str)],
        stub_servers=_resolve_stub_servers(mcp_gateway_data),
        # The operator's deviations, kept ALONGSIDE the resolved set above
        # rather than folded away: ``stub_servers`` here is already the
        # effective answer, so a writer that wants to record a new
        # decision needs to see which ones are decisions and which came
        # from the roster. Shares the resolver with the runtime so a
        # non-bool value is dropped in exactly one place.
        stub_overrides=_resolve_stub_overrides(mcp_gateway_data),
        # The file's own roster, carried so ``save()`` can put it back
        # instead of flattening it to the effective set above. See the
        # field's own comment for why that flattening is a data loss.
        _stub_roster=_resolve_stub_roster(mcp_gateway_data),
        # Hand-editable list of env NAMES; keep only strings and drop
        # blanks so a stray null or nested object cannot reach the
        # hashing layer as a key. Not deduplicated here — every consumer
        # builds a frozenset from it.
        pool_identity_env=[
            s.strip() for s in section.get("pool_identity_env") if isinstance(s, str) and s.strip()
        ],
        prewarm_count=max(0, section.read("prewarm_count", _safe_int)),
        read_buffer_limit_bytes=max(
            1024,
            section.read("read_buffer_limit_bytes", _safe_int),
        ),
        response_spill_threshold_bytes=max(
            0,
            section.read("response_spill_threshold_bytes", _safe_int),
        ),
    )


def _build_instances_config(
    connect_timeout_raw: object, instances_data: dict, mint_timeout_raw: object
) -> InstancesConfig:
    section = SectionReader(InstancesConfig, instances_data)
    return InstancesConfig(
        enabled=bool(section.get("enabled")),
        warm_set_cap=section.read("warm_set_cap", _safe_int),
        tunnel_base_port=section.read("tunnel_base_port", _safe_int),
        ssh_compression=bool(section.get("ssh_compression")),
        connect_timeout_secs=(
            _safe_float(connect_timeout_raw, _DEFAULT_CONNECT_TIMEOUT)
            if connect_timeout_raw is not None
            else None
        ),
        mint_timeout_secs=(
            _safe_float(mint_timeout_raw, _DEFAULT_MINT_TIMEOUT)
            if mint_timeout_raw is not None
            else None
        ),
        max_recovery_attempts=section.read("max_recovery_attempts", _safe_int),
        recover_backoff_max_secs=section.read("recover_backoff_max_secs", _safe_float),
        probe_failure_threshold=section.read("probe_failure_threshold", _safe_int),
    )


def _build_tunnel_config(tunnel_data: dict) -> TunnelConfig:
    section = SectionReader(TunnelConfig, tunnel_data)
    return TunnelConfig(
        enabled=bool(section.get("enabled")),
        name_mode=str(section.get("name_mode")),
        name_override=str(section.get("name_override")),
    )


def _build_publish_config(_dests_raw: list, publish_data: dict) -> PublishConfig:
    section = SectionReader(PublishConfig, publish_data)
    return PublishConfig(
        allowed_destinations=[d for d in _dests_raw if isinstance(d, str) and d],
        relocate_roots=[
            r for r in section.get("relocate_roots") if isinstance(r, str) and r.strip()
        ],
    )


def _build_computer_use_config(computer_use_data: dict) -> ComputerUseConfig:
    section = SectionReader(ComputerUseConfig, computer_use_data)
    return ComputerUseConfig(
        max_tree_nodes=min(
            _CU_MAX_TREE_NODES,
            max(
                1,
                section.read("max_tree_nodes", _safe_int),
            ),
        ),
        max_tree_depth=min(
            _CU_MAX_TREE_DEPTH,
            max(
                1,
                section.read("max_tree_depth", _safe_int),
            ),
        ),
        text_limit=min(
            _CU_MAX_TEXT_LIMIT,
            max(
                1,
                section.read("text_limit", _safe_int),
            ),
        ),
        attach_screenshot=section.read("attach_screenshot", _safe_bool),
        screenshot_max_px=min(
            _CU_MAX_SCREENSHOT_MAX_PX,
            max(
                _CU_MIN_SCREENSHOT_MAX_PX,
                section.read("screenshot_max_px", _safe_int),
            ),
        ),
        screenshot_jpeg_quality=min(
            100,
            max(
                1,
                section.read("screenshot_jpeg_quality", _safe_int),
            ),
        ),
        # Default False: a missing or unparseable value must mean "do not
        # draw on the operator's screen", never the reverse.
        cursor_motion=section.read("cursor_motion", _safe_bool),
    )


# ---------------------------------------------------------------------------
# Messaging channels (DTOs in ``config.sections``).
# ---------------------------------------------------------------------------


def _build_slack_config(slack_data: dict) -> SlackConfig:
    section = SectionReader(SlackConfig, slack_data)
    return SlackConfig(
        session_folder=_coerce_session_folder(section.get("session_folder")),
        allowed_users=[
            u for u in section.get("allowed_users") if isinstance(u, dict) and u.get("slack_id")
        ],
        tracking_channels=_validate_tracking_channels(section.get("tracking_channels")),
        open_channels=[c for c in section.get("open_channels") if isinstance(c, str)],
        command=section.get("command"),
        forward_to_agent_callback=str(
            section.get("forward_to_agent_callback") or section.default("forward_to_agent_callback")
        ).strip(),
        # A set field that config.json stores as a list: an omitted key is the field
        # default as built, and a stored value that is not a list trusts no bot.
        trusted_bot_ids=(
            {b for b in _safe_list(slack_data["trusted_bot_ids"]) if isinstance(b, str)}
            if "trusted_bot_ids" in slack_data
            else section.default("trusted_bot_ids")
        ),
        trusted_bot_turn_limit=section.read("trusted_bot_turn_limit", _safe_int, lo=1),
        allowed_enterprise_ids=[
            e
            for e in section.get("allowed_enterprise_ids")
            if isinstance(e, str) and (e.startswith("E") or e.startswith("T"))
        ],
        reactions={
            k: v
            for k, v in _safe_dict(section.get("reactions")).items()
            if isinstance(k, str) and (v is None or (isinstance(v, str) and v))
        },
        reactions_enabled=bool(section.get("reactions_enabled")),
        use_tunnel_url=bool(section.get("use_tunnel_url")),
        show_thinking=bool(section.get("show_thinking")),
        dm_single_session=bool(section.get("dm_single_session")),
        home_tab_sessions_per_kind=section.read("home_tab_sessions_per_kind", _safe_int),
        sessions_limit=section.read("sessions_limit", _safe_int),
        # Default False: a missing or unparseable value must mean "open no
        # thread", never the reverse.
        auto_link_sessions=section.read("auto_link_sessions", _safe_bool),
    )


def _build_telegram_config(telegram_data: dict) -> TelegramConfig:
    section = SectionReader(TelegramConfig, telegram_data)
    return TelegramConfig(
        session_folder=_coerce_session_folder(section.get("session_folder")),
        enabled=bool(section.get("enabled")),
        bot_token=str(section.get("bot_token")),
        allowed_user_ids=_coerce_int_ids(section.get("allowed_user_ids")),
        soft_threshold_pct=section.read("soft_threshold_pct", _threshold_pct),
        show_thinking=bool(section.get("show_thinking")),
        allow_forum=bool(section.get("allow_forum")),
        voice_replies=bool(section.get("voice_replies")),
        forum_activation=_validate_telegram_activation(
            str(section.get("forum_activation") or section.default("forum_activation"))
        ),
        allowed_forum_chat_ids=_coerce_int_ids(section.get("allowed_forum_chat_ids")),
        # The parser reads raw JSON entries, so it is handed only a stored value;
        # an omitted key is the field default as built.
        accounts=(
            _parse_telegram_accounts(telegram_data["accounts"])
            if "accounts" in telegram_data
            else section.default("accounts")
        ),
    )


def _build_weixin_config(weixin_data: dict) -> WeixinConfig:
    section = SectionReader(WeixinConfig, weixin_data)
    return WeixinConfig(
        session_folder=_coerce_session_folder(section.get("session_folder")),
        enabled=bool(section.get("enabled")),
        token=str(section.get("token")),
        account_id=str(section.get("account_id")),
        base_url=str(section.get("base_url") or section.default("base_url")),
        dm_policy=str(section.get("dm_policy") or section.default("dm_policy")),
        allowed_user_ids=_coerce_opaque_str_ids(section.get("allowed_user_ids")),
        soft_threshold_pct=section.read("soft_threshold_pct", _threshold_pct),
        hard_threshold_pct=section.read("hard_threshold_pct", _threshold_pct),
    )


def _build_whatsapp_config(whatsapp_data: dict) -> WhatsAppConfig:
    section = SectionReader(WhatsAppConfig, whatsapp_data)
    return WhatsAppConfig(
        session_folder=_coerce_session_folder(section.get("session_folder")),
        enabled=bool(section.get("enabled")),
        dm_policy=str(section.get("dm_policy") or section.default("dm_policy")),
        allowed_wa_ids=_coerce_str_ids(section.get("allowed_wa_ids")),
        groups=_coerce_whatsapp_groups(section.get("groups")),
        db_path=str(section.get("db_path")),
        soft_threshold_pct=section.read("soft_threshold_pct", _threshold_pct),
        hard_threshold_pct=section.read("hard_threshold_pct", _threshold_pct),
    )


def _build_discord_config(discord_data: dict) -> DiscordConfig:
    section = SectionReader(DiscordConfig, discord_data)
    return DiscordConfig(
        session_folder=_coerce_session_folder(section.get("session_folder")),
        enabled=bool(section.get("enabled")),
        bot_token=str(section.get("bot_token")),
        # Discord user IDs are numeric snowflakes that exceed 2^53 —
        # keep them as strings (JSON round-trip safe, matches the
        # transport's string comparison).
        allowed_user_ids=_coerce_str_ids(section.get("allowed_user_ids")),
        allowed_thread_ids=_coerce_str_ids(section.get("allowed_thread_ids")),
        allowed_channel_ids=_coerce_str_ids(section.get("allowed_channel_ids")),
        auto_thread=bool(section.get("auto_thread")),
        soft_threshold_pct=section.read("soft_threshold_pct", _threshold_pct),
        reactions_enabled=bool(section.get("reactions_enabled")),
        show_thinking=bool(section.get("show_thinking")),
    )


def _build_webex_config(webex_data: dict) -> WebexConfig:
    section = SectionReader(WebexConfig, webex_data)
    return WebexConfig(
        session_folder=_coerce_session_folder(section.get("session_folder")),
        enabled=bool(section.get("enabled")),
        bot_token=str(section.get("bot_token")),
        allowed_emails=(
            [e for e in section.get("allowed_emails") if isinstance(e, str) and e]
            if isinstance(section.get("allowed_emails"), list)
            else section.default("allowed_emails")
        ),
        # Group spaces are a SECURITY decision, so the read is as explicit
        # as the write: a field the loader forgets is not merely lost, it
        # silently reverts to the safe default on the next restart while
        # the settings panel keeps showing the saved value it read from
        # config.json — the operator sees an enabled space allow-list and
        # the gateway answers nobody.
        allow_group_rooms=bool(section.get("allow_group_rooms")),
        allowed_room_ids=[
            r for r in _safe_list(section.get("allowed_room_ids")) if isinstance(r, str) and r
        ],
        reply_in_thread=bool(section.get("reply_in_thread")),
        wdm_base=str(section.get("wdm_base") or section.default("wdm_base")),
        soft_threshold_pct=section.read("soft_threshold_pct", _threshold_pct),
        hard_threshold_pct=section.read("hard_threshold_pct", _threshold_pct),
    )


def _build_imessage_config(imessage_data: dict) -> IMessageConfig:
    section = SectionReader(IMessageConfig, imessage_data)
    return IMessageConfig(
        session_folder=_coerce_session_folder(section.get("session_folder")),
        enabled=bool(section.get("enabled")),
        db_path=str(section.get("db_path")),
        allowed_handles=[
            h for h in _safe_list(section.get("allowed_handles")) if isinstance(h, str) and h
        ],
        service=str(section.get("service") or section.default("service")),
        soft_threshold_pct=section.read("soft_threshold_pct", _threshold_pct),
        hard_threshold_pct=section.read("hard_threshold_pct", _threshold_pct),
    )


def _build_teams_config(teams_data: dict) -> TeamsConfig:
    section = SectionReader(TeamsConfig, teams_data)
    return TeamsConfig(
        session_folder=_coerce_session_folder(section.get("session_folder")),
        enabled=bool(section.get("enabled")),
        app_id=str(section.get("app_id")),
        # Secret is env-only (MICROSOFT_APP_PASSWORD). Never sourced from
        # config.json, which the agent can read — keeps the Azure Bot
        # credential out of any agent-readable file.
        app_password=section.default("app_password"),
        tenant_id=str(section.get("tenant_id")),
        allowed_emails=(
            [e for e in section.get("allowed_emails") if isinstance(e, str) and e]
            if isinstance(section.get("allowed_emails"), list)
            else section.default("allowed_emails")
        ),
        soft_threshold_pct=section.read("soft_threshold_pct", _threshold_pct),
        hard_threshold_pct=section.read("hard_threshold_pct", _threshold_pct),
    )


def _build_wecom_config(wecom_data: dict) -> WeComConfig:
    section = SectionReader(WeComConfig, wecom_data)
    return WeComConfig(
        session_folder=_coerce_session_folder(section.get("session_folder")),
        # _safe_bool, not bool(): `bool("false")` is True, so a JSON string
        # would read the operator's "off" as "on" -- enabling a channel,
        # or opening it to every org member, from a config value that says the
        # opposite. A non-bool must read as the default, not as truthy.
        enabled=section.read("enabled", _safe_bool),
        allowed_users=[
            u
            for u in _safe_list(section.get("allowed_users"))
            if isinstance(u, dict) and u.get("userid")
        ],
        allow_all_users=section.read("allow_all_users", _safe_bool),
        ws_url=str(section.get("ws_url")),
        soft_threshold_pct=section.read("soft_threshold_pct", _threshold_pct),
        hard_threshold_pct=section.read("hard_threshold_pct", _threshold_pct),
    )


def _build_feishu_config(feishu_data: dict) -> FeishuConfig:
    section = SectionReader(FeishuConfig, feishu_data)
    return FeishuConfig(
        enabled=section.read("enabled", _safe_bool),
        allowed_open_ids=_coerce_opaque_str_ids(section.get("allowed_open_ids")),
        # Shape-safe coercion rather than bool() / a raw comprehension:
        # the schema type check already substitutes the default for a
        # wrong-typed value, and these helpers keep the guarantee local
        # to the parse (and dedupe + strip the opaque ou_/oc_ ids).
        allow_group=section.read("allow_group", _safe_bool),
        allowed_group_ids=_coerce_opaque_str_ids(section.get("allowed_group_ids")),
        soft_threshold_pct=section.read("soft_threshold_pct", _safe_int),
        hard_threshold_pct=section.read("hard_threshold_pct", _safe_int),
        session_folder=_coerce_session_folder(section.get("session_folder")),
    )


def _build_wakatime_config(wakatime_data: dict) -> WakaTimeConfig:
    section = SectionReader(WakaTimeConfig, wakatime_data)
    return WakaTimeConfig(
        enabled=bool(section.get("enabled")),
        api_base_url=str(section.get("api_base_url") or section.default("api_base_url")),
        send_heartbeats=section.read("send_heartbeats", _safe_bool),
    )


# ---------------------------------------------------------------------------
# Speech-to-text (DTO and degradation rules in ``config.sections``).
# ---------------------------------------------------------------------------


def _build_stt_config(stt_data: dict) -> SttConfig:
    section = SectionReader(SttConfig, stt_data)
    return SttConfig(
        enabled=section.read("enabled", _safe_bool),
        provider=_validated_stt_provider(section.get("provider")),
        model=_validated_stt_model(section.get("model")),
        language_code=section.get("language_code"),
        # Reached through the module rather than re-exported: the loader facade's
        # import list from `sections` is a frozen pre-split snapshot
        # (test_config_module_boundaries), so a new name must not join it.
        polish=section.read("polish", _safe_bool),
        streaming=section.read("streaming", _safe_bool),
        silence_ms=section.read(
            "silence_ms", _safe_int, lo=_STT_MIN_SILENCE_MS, hi=_STT_INTERVAL_MS_MAX
        ),
        partial_interval_ms=section.read(
            "partial_interval_ms",
            _safe_int,
            lo=_STT_MIN_PARTIAL_INTERVAL_MS,
            hi=_STT_INTERVAL_MS_MAX,
        ),
        idle_evict_secs=section.read(
            "idle_evict_secs", _safe_int, lo=_STT_IDLE_EVICT_SECS_MIN, hi=_STT_IDLE_EVICT_SECS_MAX
        ),
        endpointing=section.read("endpointing", _safe_bool),
        dictation_panel=section.read("dictation_panel", _safe_bool),
        timeout_secs=section.read(
            "timeout_secs", _safe_int, lo=_STT_MIN_TIMEOUT_SECS, hi=_STT_MAX_TIMEOUT_SECS
        ),
        transcribe_region=section.get("transcribe_region"),
        transcribe_profile=section.get("transcribe_profile"),
        transcribe_vocabulary=_sections._validated_transcribe_vocabulary(
            section.get("transcribe_vocabulary")
        ),
    )
