"""Build a ``config.json`` section's DTO from its raw dict, one frame per section.

``KiroCrewConfig._load_resolved`` hands every builder the section it already
extracted and degraded; a builder reads that dict with the field coercers and
returns a fresh dataclass, so a load never shares a mutable value with another
load. Builders are grouped by the module that owns their section's DTO.

This module holds 28 of the loader's ``_build_*`` helpers. Four stay in
``config.loader`` because their placement is pinned there: agent (the
harness-parity review scope and a source check on the ``session_control``
read), session (its pool-size fallback reads the loader's ``DEFAULT_POOL_SIZE``
at call time), telemetry (the metrics spec names the loader as its parser) and
dashboard (the feature map cites the loader's ``folder_sort`` read). Sections
with no helper are built inline in ``_load_resolved`` or by their DTO's own
constructor. A name this module reads is patched here, not on the loader. This
module imports neither the loader nor schema/validation.
"""

from __future__ import annotations

# Computer-use defaults/ceilings come from the feature's constants module rather
# than being re-spelled here (docs/system-specs/common/code-style.md: no
# hardcoded values in business logic).
# ``computer_use.types`` is deliberately dependency-free — it imports nothing from
# ``kiro_crew`` — so this cannot create an import cycle with the config package, and the
# ``computer_use`` package's ``__init__`` pulls in only ``platform_compat`` /
# ``executors`` (both stdlib-only), never ``config``.
from kiro_crew.computer_use.types import DEFAULT_ATTACH_SCREENSHOT as _CU_DEFAULT_ATTACH_SCREENSHOT
from kiro_crew.computer_use.types import DEFAULT_MAX_TREE_DEPTH as _CU_DEFAULT_MAX_TREE_DEPTH
from kiro_crew.computer_use.types import DEFAULT_MAX_TREE_NODES as _CU_DEFAULT_MAX_TREE_NODES
from kiro_crew.computer_use.types import (
    DEFAULT_SCREENSHOT_JPEG_QUALITY as _CU_DEFAULT_SCREENSHOT_JPEG_QUALITY,
)
from kiro_crew.computer_use.types import DEFAULT_SCREENSHOT_MAX_PX as _CU_DEFAULT_SCREENSHOT_MAX_PX
from kiro_crew.computer_use.types import DEFAULT_TEXT_LIMIT as _CU_DEFAULT_TEXT_LIMIT
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
    FORWARD_DECLARED_ENV_DEFAULT,
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
    DEFAULT_AUTO_INGEST_ARTIFACT_KINDS,
    KnowledgeConfig,
    MemoryConfig,
    SessionSummaryConfig,
    SkillsConfig,
    _coerce_embedding_provider,
    _read_auto_add_documents,
)
from kiro_crew.config.sections import (
    ACTIVATION_ALWAYS,
    DEDUP_EVERY_N_SWEEPS_MAX,
    EMBED_RATE_LIMIT_MAX,
    EXTRACTION_POOL_SIZE_MAX,
    EXTRACTION_POOL_SIZE_MIN,
    FOLDER_INGEST_CHUNK_BUDGET_MAX,
    IMPORT_CHUNK_BUDGET_MAX,
    STT_PROVIDER_LOCAL,
    SWEEP_CHUNK_BUDGET_MAX,
    DiscordConfig,
    FeishuConfig,
    IMessageConfig,
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
    DEFAULT_MAX_PARALLEL_STEPS,
    CronHistoryConfig,
    MessagingConfig,
    MonitoringConfig,
    TaskRunnerConfig,
    WatchdogConfig,
)
from kiro_crew.instances.constants import DEFAULT_CONNECT_TIMEOUT_SECS as _DEFAULT_CONNECT_TIMEOUT
from kiro_crew.instances.constants import DEFAULT_MAX_RECOVERY_ATTEMPTS as _DEFAULT_MAX_RECOVERY
from kiro_crew.instances.constants import DEFAULT_MINT_TIMEOUT_SECS as _DEFAULT_MINT_TIMEOUT
from kiro_crew.instances.constants import DEFAULT_PROBE_FAILURE_THRESHOLD as _DEFAULT_PROBE_FAILS
from kiro_crew.instances.constants import DEFAULT_RECOVER_BACKOFF_MAX_SECS as _DEFAULT_BACKOFF_MAX
from kiro_crew.instances.constants import DEFAULT_SSH_COMPRESSION as _DEFAULT_SSH_COMPRESSION
from kiro_crew.instances.constants import DEFAULT_TUNNEL_BASE_PORT as _DEFAULT_TUNNEL_BASE_PORT
from kiro_crew.instances.constants import DEFAULT_WARM_SET_CAP as _DEFAULT_WARM_SET_CAP

# Runtime-budget policy and coercion live in the monitoring limits leaf module,
# keeping the section builders free of duplicated bounds.
from kiro_crew.monitoring.limits import coerce_runtime_ceiling

# The speech-to-text defaults and the model catalog come from the package that
# owns them, so the model menu this schema advertises cannot name a model that
# cannot be downloaded, and a tuning knob cannot document a default the session
# does not use. No cycle: the only config dependency anywhere under
# ``kiro_crew.stt`` is the leaf ``config.paths``, never this module.
from kiro_crew.stt.limits import DEFAULT_IDLE_EVICT_SECS as _STT_DEFAULT_IDLE_EVICT_SECS
from kiro_crew.stt.limits import DEFAULT_PARTIAL_INTERVAL_MS as _STT_DEFAULT_PARTIAL_INTERVAL_MS
from kiro_crew.stt.limits import DEFAULT_SILENCE_MS as _STT_DEFAULT_SILENCE_MS
from kiro_crew.stt.limits import DEFAULT_TIMEOUT_SECS as _STT_DEFAULT_TIMEOUT_SECS
from kiro_crew.stt.limits import MAX_IDLE_EVICT_SECS as _STT_IDLE_EVICT_SECS_MAX
from kiro_crew.stt.limits import MAX_INTERVAL_MS as _STT_INTERVAL_MS_MAX
from kiro_crew.stt.limits import MAX_TIMEOUT_SECS as _STT_MAX_TIMEOUT_SECS
from kiro_crew.stt.limits import MIN_IDLE_EVICT_SECS as _STT_IDLE_EVICT_SECS_MIN
from kiro_crew.stt.limits import MIN_PARTIAL_INTERVAL_MS as _STT_MIN_PARTIAL_INTERVAL_MS
from kiro_crew.stt.limits import MIN_SILENCE_MS as _STT_MIN_SILENCE_MS
from kiro_crew.stt.limits import MIN_TIMEOUT_SECS as _STT_MIN_TIMEOUT_SECS
from kiro_crew.stt.models import DEFAULT_MODEL as _STT_DEFAULT_MODEL

# ---------------------------------------------------------------------------
# Service sections (DTOs in ``config.service_sections``).
# ---------------------------------------------------------------------------


def _build_taskrunner_config(taskrunner_data: dict) -> TaskRunnerConfig:
    return TaskRunnerConfig(
        max_parallel_steps=taskrunner_data.get("max_parallel_steps", DEFAULT_MAX_PARALLEL_STEPS),
        workspace_dir=str(taskrunner_data.get("workspace_dir", "")),
    )


def _build_messaging_config(messaging_data: dict) -> MessagingConfig:
    return MessagingConfig(
        use_transport=bool(messaging_data.get("use_transport", True)),
        dm_scope=str(messaging_data.get("dm_scope", "per-channel-peer")),
        idle_reset_minutes=_coerce_int(messaging_data.get("idle_reset_minutes"), 0),
        daily_reset_hour=_coerce_int(messaging_data.get("daily_reset_hour"), -1),
        queue_mode=str(messaging_data.get("queue_mode", "steer")),
    )


def _build_cron_history_config(cron_history_data: dict) -> CronHistoryConfig:
    return CronHistoryConfig(
        cron_summary_cap=_safe_int(cron_history_data.get("cron_summary_cap", 200), 200),
        cron_trace_cap_kb=_safe_int(cron_history_data.get("cron_trace_cap_kb", 50), 50),
        cron_max_records_per_job=_safe_int(
            cron_history_data.get("cron_max_records_per_job", 100), 100
        ),
        cron_max_index_records=_safe_int(
            cron_history_data.get("cron_max_index_records", 2000), 2000
        ),
    )


def _build_monitoring_config(data: dict, prefer_structured_arming: bool) -> MonitoringConfig:
    return MonitoringConfig(
        prefer_structured_arming=prefer_structured_arming,
        max_runtime_secs=coerce_runtime_ceiling(data.get("max_runtime_secs")),
    )


def _build_watchdog_config(watchdog_data: dict) -> WatchdogConfig:
    return WatchdogConfig(
        check_after_secs=_safe_float(watchdog_data.get("check_after_secs", 60.0), 60.0),
        stale_window_secs=_safe_float(watchdog_data.get("stale_window_secs", 600.0), 600.0),
        tool_stall_suspect_secs=_safe_float(
            watchdog_data.get("tool_stall_suspect_secs", 5400.0), 5400.0
        ),
        tool_stall_hard_cap_secs=_safe_float(
            watchdog_data.get("tool_stall_hard_cap_secs", 7200.0), 7200.0
        ),
        model_silent_probe_secs=_safe_float(
            watchdog_data.get("model_silent_probe_secs", 1800.0), 1800.0
        ),
        remote_flat_probe_secs=_safe_float(watchdog_data.get("remote_flat_probe_secs", 0.0), 0.0),
        wellness_sample_secs=_safe_float(watchdog_data.get("wellness_sample_secs", 3.0), 3.0),
    )


# ---------------------------------------------------------------------------
# Memory sections (DTOs in ``config.memory_sections``).
# ---------------------------------------------------------------------------


def _build_memory_config(memory_data: dict) -> MemoryConfig:
    return MemoryConfig(
        embedding_provider=_coerce_embedding_provider(
            memory_data.get("embedding_provider", "llama_cpp")
        ),
        embedding_dim=memory_data.get("embedding_dim", 1024),
        embedding_threads=_safe_int(memory_data.get("embedding_threads", 4), 4, 1, 256),
        # 0 is the documented "inherit embedding_threads" sentinel, so the
        # floor is 0 rather than 1 — clamping it to 1 would erase a
        # deliberate opt-in to the interactive pool.
        embedding_bulk_threads=_safe_int(memory_data.get("embedding_bulk_threads", 1), 1, 0, 256),
        embedding_bulk_duty=_safe_float(
            memory_data.get("embedding_bulk_duty", 0.2), 0.2, 0.05, 1.0
        ),
        embed_model_url=memory_data.get("embed_model_url", ""),
        embed_model_path=memory_data.get("embed_model_path", ""),
        embed_model_id=memory_data.get("embed_model_id", ""),
        embed_model_stamp=memory_data.get("embed_model_stamp", []),
        embed_model_legacy_ids=memory_data.get("embed_model_legacy_ids", []),
        embed_rebuild_generation=memory_data.get("embed_rebuild_generation", ""),
        semantic_confidence_threshold=_safe_float(
            memory_data.get("semantic_confidence_threshold", 0.8), 0.8, 0.0, 1.0
        ),
        episodic_dedup_threshold=_safe_float(
            memory_data.get("episodic_dedup_threshold", 0.88), 0.88, 0.0, 1.0
        ),
        episodic_max_results=_safe_int(memory_data.get("episodic_max_results", 8), 8, 1, None),
        # Floor of 1, not 0. A normal write keeps the row it writes either way, so a
        # cap of 0 behaves as 1 there: `_enforce_episodic_cap` tombstones every older
        # active V1 row, which is what a cap that small means. A merge-only write is
        # where 0 differs: `active_count >= 0` holds on an empty store, so every one
        # is refused as at-capacity and the ledger import can never index anything.
        episodic_max_count=_safe_int(
            memory_data.get("episodic_max_count", 10_000), 10_000, 1, None
        ),
        decay_rates=(dr if isinstance(dr := memory_data.get("decay_rates", {}), dict) else {}),
        semantic_keys=memory_data.get("semantic_keys", []),
        history_idle_hours=memory_data.get("history_idle_hours", 3.0),
        history_max_days=_safe_nonnegative_int(memory_data.get("history_max_days", 365), 365),
        backup_enabled=_safe_bool(memory_data.get("backup_enabled", True), True),
        backup_keep=_safe_int(memory_data.get("backup_keep", 7), 7, 1, None),
        persistence_enabled=_safe_bool(memory_data.get("persistence_enabled", True), True),
        inject_memory=_safe_bool(memory_data.get("inject_memory", True), True),
        inject_lessons=_safe_bool(memory_data.get("inject_lessons", True), True),
        inject_activity=_safe_bool(memory_data.get("inject_activity", True), True),
        migrated=memory_data.get("migrated", False),
    )


def _build_knowledge_config(knowledge_data: dict) -> KnowledgeConfig:
    return KnowledgeConfig(
        auto_ingest_artifacts=bool(knowledge_data.get("auto_ingest_artifacts", False)),
        auto_ingest_artifact_kinds=[
            k
            for k in knowledge_data.get(
                "auto_ingest_artifact_kinds",
                DEFAULT_AUTO_INGEST_ARTIFACT_KINDS,
            )
            if isinstance(k, str)
        ],
        max_ingest_file_mb=(
            float(mb)
            if isinstance(
                (mb := knowledge_data.get("max_ingest_file_mb", 100.0)),
                (int, float),
            )
            and not isinstance(mb, bool)
            and mb >= 0
            else 100.0
        ),
        embed_timeout_secs=_safe_float(knowledge_data.get("embed_timeout_secs", 10.0), 10.0),
        embed_content_budget=_safe_int(knowledge_data.get("embed_content_budget", 0), 0),
        pool_idle_ttl_secs=_safe_nonnegative_int(
            knowledge_data.get("pool_idle_ttl_secs", 300),
            300,
        ),
        auto_add_documents=_read_auto_add_documents(knowledge_data),
        folder_ingest_chunk_budget=_safe_nonnegative_int(
            knowledge_data.get("folder_ingest_chunk_budget", 300),
            300,
            FOLDER_INGEST_CHUNK_BUDGET_MAX,
        ),
        dedup_every_n_sweeps=_safe_nonnegative_int(
            knowledge_data.get("dedup_every_n_sweeps", 12),
            12,
            DEDUP_EVERY_N_SWEEPS_MAX,
        ),
        doc_ingest_hosts=[
            str(h)
            for h in knowledge_data.get("doc_ingest_hosts", [])
            if isinstance(h, str) and h.strip()
        ],
        sweep_chunk_budget=_safe_nonnegative_int(
            knowledge_data.get("sweep_chunk_budget", 500),
            500,
            SWEEP_CHUNK_BUDGET_MAX,
        ),
        import_chunk_budget=_safe_nonnegative_int(
            knowledge_data.get("import_chunk_budget", 0),
            0,
            IMPORT_CHUNK_BUDGET_MAX,
        ),
        embed_rate_limit=_safe_nonnegative_int(
            knowledge_data.get("embed_rate_limit", 120), 120, EMBED_RATE_LIMIT_MAX
        ),
        extraction_model=str(knowledge_data.get("extraction_model", "")).strip(),
        extraction_pool_size=max(
            EXTRACTION_POOL_SIZE_MIN,
            min(
                EXTRACTION_POOL_SIZE_MAX,
                _safe_nonnegative_int(knowledge_data.get("extraction_pool_size", 3), 3),
            ),
        ),
        extraction_effort=coerce_effort(knowledge_data.get("extraction_effort", "")),
    )


def _build_skills_config(skills_data: dict) -> SkillsConfig:
    # Every default here is READ FROM THE DATACLASS, never written a second time.
    # A config.json omits any key it predates, so a literal in this function is a
    # second declaration of the same default that can drift from the first and then
    # answer with the opposite value, silently, for exactly the installs that have
    # not touched the setting.
    d = SkillsConfig()
    return SkillsConfig(
        max_triggered=_safe_int(skills_data.get("max_triggered", d.max_triggered), d.max_triggered),
        lazy_load=_safe_bool(skills_data.get("lazy_load", d.lazy_load), d.lazy_load),
        auto_create_from_sessions=_safe_bool(
            skills_data.get("auto_create_from_sessions", d.auto_create_from_sessions),
            d.auto_create_from_sessions,
        ),
        auto_refine_on_deviation=_safe_bool(
            skills_data.get("auto_refine_on_deviation", d.auto_refine_on_deviation),
            d.auto_refine_on_deviation,
        ),
        auto_min_tool_calls=_safe_int(
            skills_data.get("auto_min_tool_calls", d.auto_min_tool_calls), d.auto_min_tool_calls
        ),
        auto_similarity_threshold=_safe_float(
            skills_data.get("auto_similarity_threshold", d.auto_similarity_threshold),
            d.auto_similarity_threshold,
        ),
        approval_required=_safe_bool(
            skills_data.get("approval_required", d.approval_required), d.approval_required
        ),
        max_auto_skills=_safe_int(
            skills_data.get("max_auto_skills", d.max_auto_skills), d.max_auto_skills
        ),
        stale_after_days=_safe_int(
            skills_data.get("stale_after_days", d.stale_after_days), d.stale_after_days
        ),
        archive_after_days=_safe_int(
            skills_data.get("archive_after_days", d.archive_after_days), d.archive_after_days
        ),
        pending_ttl_days=_safe_int(
            skills_data.get("pending_ttl_days", d.pending_ttl_days), d.pending_ttl_days
        ),
        generate_scripts=_safe_bool(
            skills_data.get("generate_scripts", d.generate_scripts), d.generate_scripts
        ),
        judge_model=str(skills_data.get("judge_model", d.judge_model) or d.judge_model),
        extra_paths=[p for p in _safe_list(skills_data.get("extra_paths")) if isinstance(p, str)],
        # Security off-switch: malformed values must not become truthy
        # through Python coercion (for example, the string "false").
        project_skills_enabled=(
            skills_data.get("project_skills_enabled", d.project_skills_enabled) is True
        ),
    )


def _build_session_summary_config(session_summary_data: dict) -> SessionSummaryConfig:
    return SessionSummaryConfig(
        enabled=bool(session_summary_data.get("enabled", False)),
        min_user_turns=_safe_int(session_summary_data.get("min_user_turns", 2), 2),
        regenerate_after_turns=_safe_int(session_summary_data.get("regenerate_after_turns", 1), 1),
        max_intents=_safe_int(session_summary_data.get("max_intents", 50), 50),
        max_constraints=_safe_int(session_summary_data.get("max_constraints", 50), 50),
        assistant_excerpt_chars=_safe_int(
            session_summary_data.get("assistant_excerpt_chars", 400), 400
        ),
    )


# ---------------------------------------------------------------------------
# Integration sections (DTOs in ``config.integration_sections``).
# ---------------------------------------------------------------------------


def _build_mcp_config(mcp_data: dict) -> McpConfig:
    """Build the ``mcp`` section in its own frame (see the compound-section rule)."""
    return McpConfig(
        # Kept as authored strings — validation (absolute-only, ``~`` expansion,
        # dedup) belongs to the consumer, kiro_crew.env.augmented_path, so the ONE
        # gate the built-in directories already pass applies to these too instead
        # of a second rule drifting here. Non-strings ARE dropped: the field is
        # typed list[str] and to_dict() round-trips it verbatim into the saved
        # config.
        extra_path_dirs=[
            d for d in _safe_list(mcp_data.get("extra_path_dirs", [])) if isinstance(d, str)
        ],
        # ABSENT takes the documented default (on): an ``autoApprove`` the owner
        # wrote is respected, and nobody has to name this key to get that. Opting
        # out takes a real ``false``; a value of the wrong type is removed by the
        # schema validator before this runs, so it reads as absent and the default
        # applies rather than a guess at what the text meant. The default lives
        # here as well as on the dataclass field because this builder always sets
        # the field explicitly, so the field's own default never reaches a loaded
        # config.
        honour_auto_approve=mcp_data.get("honour_auto_approve", True) is True,
    )


def _build_mcp_gateway_config(mcp_gateway_data: dict) -> McpGatewayConfig:
    _spawn_min = max(1, _safe_int(mcp_gateway_data.get("spawn_concurrency_min", 1), 1))
    _spawn_max = max(_spawn_min, _safe_int(mcp_gateway_data.get("spawn_concurrency_max", 8), 8))
    return McpGatewayConfig(
        enabled=bool(mcp_gateway_data.get("enabled", False)),
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
        apps_enabled=_safe_bool(mcp_gateway_data.get("apps_enabled", True), True),
        # ON by default. The forwarded set is a strict subset of the
        # hashed set and gatewayd re-hashes the sidecar at spawn,
        # forwarding nothing on mismatch, so a forwarded key is one every
        # co-tenant of that backend declared identically. With it off, one
        # ordinary declared key costs the whole server its pooling.
        #
        # Both arguments are True on purpose. A malformed value never
        # reaches this call: ``config.validation`` type-checks first and
        # ``_apply_field_default`` strips a non-boolean so the dataclass
        # default applies, which is why the log says "using default". The
        # fallback here is defence in depth for a bypassed validator, and
        # giving it a different answer than the schema would only put two
        # disagreeing defaults in the file.
        forward_declared_env=_safe_bool(
            mcp_gateway_data.get("forward_declared_env", FORWARD_DECLARED_ENV_DEFAULT),
            FORWARD_DECLARED_ENV_DEFAULT,
        ),
        socket_path=str(mcp_gateway_data.get("socket_path", "")),
        overlay_dir=str(mcp_gateway_data.get("overlay_dir", "")),
        idle_timeout_secs=max(10, _safe_int(mcp_gateway_data.get("idle_timeout_secs", 300), 300)),
        # 0 is meaningful (re-resolve every pass), so the floor is 0 and
        # not the usual "at least something" clamp.
        resolve_once_refresh_hours=max(
            0, _safe_int(mcp_gateway_data.get("resolve_once_refresh_hours", 24), 24)
        ),
        max_backends=max(1, _safe_int(mcp_gateway_data.get("max_backends", 64), 64)),
        # Admission keys. Clamps mirror the dataclass defaults: floor
        # >= 1, ceiling >= floor, initial inside the band; 0 keeps the
        # "auto" meaning on the host-budget ceilings.
        spawn_concurrency_min=_spawn_min,
        spawn_concurrency_max=_spawn_max,
        spawn_concurrency_initial=min(
            _spawn_max,
            max(
                _spawn_min,
                _safe_int(mcp_gateway_data.get("spawn_concurrency_initial", 4), 4),
            ),
        ),
        spawn_queue_wait_secs=max(
            1, _safe_int(mcp_gateway_data.get("spawn_queue_wait_secs", 600), 600)
        ),
        initialize_timeout_secs=max(
            1, _safe_int(mcp_gateway_data.get("initialize_timeout_secs", 10), 10)
        ),
        host_budget_max_procs=max(
            0, _safe_int(mcp_gateway_data.get("host_budget_max_procs", 0), 0)
        ),
        host_budget_max_rss_mb=max(
            0, _safe_int(mcp_gateway_data.get("host_budget_max_rss_mb", 0), 0)
        ),
        host_budget_max_fds=max(0, _safe_int(mcp_gateway_data.get("host_budget_max_fds", 0), 0)),
        poolable_servers=[
            s for s in mcp_gateway_data.get("poolable_servers", []) if isinstance(s, str)
        ],
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
            s.strip()
            for s in mcp_gateway_data.get("pool_identity_env", [])
            if isinstance(s, str) and s.strip()
        ],
        prewarm_count=max(0, _safe_int(mcp_gateway_data.get("prewarm_count", 0), 0)),
        read_buffer_limit_bytes=max(
            1024,
            _safe_int(
                mcp_gateway_data.get("read_buffer_limit_bytes", 64 * 1024 * 1024),
                64 * 1024 * 1024,
            ),
        ),
        response_spill_threshold_bytes=max(
            0,
            _safe_int(
                mcp_gateway_data.get("response_spill_threshold_bytes", 256 * 1024),
                256 * 1024,
            ),
        ),
    )


def _build_instances_config(
    connect_timeout_raw: object, instances_data: dict, mint_timeout_raw: object
) -> InstancesConfig:
    return InstancesConfig(
        enabled=bool(instances_data.get("enabled", False)),
        warm_set_cap=_safe_int(
            instances_data.get("warm_set_cap", _DEFAULT_WARM_SET_CAP), _DEFAULT_WARM_SET_CAP
        ),
        tunnel_base_port=_safe_int(
            instances_data.get("tunnel_base_port", _DEFAULT_TUNNEL_BASE_PORT),
            _DEFAULT_TUNNEL_BASE_PORT,
        ),
        ssh_compression=bool(instances_data.get("ssh_compression", _DEFAULT_SSH_COMPRESSION)),
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
        max_recovery_attempts=_safe_int(
            instances_data.get("max_recovery_attempts", _DEFAULT_MAX_RECOVERY),
            _DEFAULT_MAX_RECOVERY,
        ),
        recover_backoff_max_secs=_safe_float(
            instances_data.get("recover_backoff_max_secs", _DEFAULT_BACKOFF_MAX),
            _DEFAULT_BACKOFF_MAX,
        ),
        probe_failure_threshold=_safe_int(
            instances_data.get("probe_failure_threshold", _DEFAULT_PROBE_FAILS),
            _DEFAULT_PROBE_FAILS,
        ),
    )


def _build_tunnel_config(tunnel_data: dict) -> TunnelConfig:
    return TunnelConfig(
        enabled=bool(tunnel_data.get("enabled", False)),
        name_mode=str(tunnel_data.get("name_mode", "username")),
        name_override=str(tunnel_data.get("name_override", "")),
    )


def _build_publish_config(_dests_raw: list, publish_data: dict) -> PublishConfig:
    return PublishConfig(
        allowed_destinations=[d for d in _dests_raw if isinstance(d, str) and d],
        relocate_roots=[
            r for r in publish_data.get("relocate_roots", []) if isinstance(r, str) and r.strip()
        ],
    )


def _build_computer_use_config(computer_use_data: dict) -> ComputerUseConfig:
    return ComputerUseConfig(
        max_tree_nodes=min(
            _CU_MAX_TREE_NODES,
            max(
                1,
                _safe_int(
                    computer_use_data.get("max_tree_nodes", _CU_DEFAULT_MAX_TREE_NODES),
                    _CU_DEFAULT_MAX_TREE_NODES,
                ),
            ),
        ),
        max_tree_depth=min(
            _CU_MAX_TREE_DEPTH,
            max(
                1,
                _safe_int(
                    computer_use_data.get("max_tree_depth", _CU_DEFAULT_MAX_TREE_DEPTH),
                    _CU_DEFAULT_MAX_TREE_DEPTH,
                ),
            ),
        ),
        text_limit=min(
            _CU_MAX_TEXT_LIMIT,
            max(
                1,
                _safe_int(
                    computer_use_data.get("text_limit", _CU_DEFAULT_TEXT_LIMIT),
                    _CU_DEFAULT_TEXT_LIMIT,
                ),
            ),
        ),
        attach_screenshot=_safe_bool(
            computer_use_data.get("attach_screenshot", _CU_DEFAULT_ATTACH_SCREENSHOT),
            _CU_DEFAULT_ATTACH_SCREENSHOT,
        ),
        screenshot_max_px=min(
            _CU_MAX_SCREENSHOT_MAX_PX,
            max(
                _CU_MIN_SCREENSHOT_MAX_PX,
                _safe_int(
                    computer_use_data.get("screenshot_max_px", _CU_DEFAULT_SCREENSHOT_MAX_PX),
                    _CU_DEFAULT_SCREENSHOT_MAX_PX,
                ),
            ),
        ),
        screenshot_jpeg_quality=min(
            100,
            max(
                1,
                _safe_int(
                    computer_use_data.get(
                        "screenshot_jpeg_quality", _CU_DEFAULT_SCREENSHOT_JPEG_QUALITY
                    ),
                    _CU_DEFAULT_SCREENSHOT_JPEG_QUALITY,
                ),
            ),
        ),
        # Default False: a missing or unparseable value must mean "do not
        # draw on the operator's screen", never the reverse.
        cursor_motion=_safe_bool(computer_use_data.get("cursor_motion", False), False),
    )


# ---------------------------------------------------------------------------
# Messaging channels (DTOs in ``config.sections``).
# ---------------------------------------------------------------------------


def _build_slack_config(slack_data: dict) -> SlackConfig:
    return SlackConfig(
        session_folder=_coerce_session_folder(slack_data.get("session_folder")),
        allowed_users=[
            u
            for u in slack_data.get("allowed_users", [])
            if isinstance(u, dict) and u.get("slack_id")
        ],
        tracking_channels=_validate_tracking_channels(slack_data.get("tracking_channels", [])),
        open_channels=[c for c in slack_data.get("open_channels", []) if isinstance(c, str)],
        command=slack_data.get("command", "kirocrew"),
        forward_to_agent_callback=str(slack_data.get("forward_to_agent_callback") or "").strip(),
        trusted_bot_ids={
            b for b in _safe_list(slack_data.get("trusted_bot_ids")) if isinstance(b, str)
        },
        trusted_bot_turn_limit=_safe_int(slack_data.get("trusted_bot_turn_limit", 5), 5, lo=1),
        allowed_enterprise_ids=[
            e
            for e in slack_data.get("allowed_enterprise_ids", [])
            if isinstance(e, str) and (e.startswith("E") or e.startswith("T"))
        ],
        reactions={
            k: v
            for k, v in _safe_dict(slack_data.get("reactions")).items()
            if isinstance(k, str) and (v is None or (isinstance(v, str) and v))
        },
        reactions_enabled=bool(slack_data.get("reactions_enabled", True)),
        use_tunnel_url=bool(slack_data.get("use_tunnel_url", False)),
        show_thinking=bool(slack_data.get("show_thinking", True)),
        dm_single_session=bool(slack_data.get("dm_single_session", False)),
        home_tab_sessions_per_kind=_safe_int(slack_data.get("home_tab_sessions_per_kind", 5), 5),
        sessions_limit=_safe_int(slack_data.get("sessions_limit", 10), 10),
    )


def _build_telegram_config(telegram_data: dict) -> TelegramConfig:
    return TelegramConfig(
        session_folder=_coerce_session_folder(telegram_data.get("session_folder")),
        enabled=bool(telegram_data.get("enabled", False)),
        bot_token=str(telegram_data.get("bot_token", "")),
        allowed_user_ids=_coerce_int_ids(telegram_data.get("allowed_user_ids")),
        soft_threshold_pct=_threshold_pct(telegram_data.get("soft_threshold_pct"), 80),
        show_thinking=bool(telegram_data.get("show_thinking", False)),
        allow_forum=bool(telegram_data.get("allow_forum", False)),
        voice_replies=bool(telegram_data.get("voice_replies", False)),
        forum_activation=_validate_telegram_activation(
            str(telegram_data.get("forum_activation", "") or ACTIVATION_ALWAYS)
        ),
        allowed_forum_chat_ids=_coerce_int_ids(telegram_data.get("allowed_forum_chat_ids")),
        accounts=_parse_telegram_accounts(telegram_data.get("accounts")),
    )


def _build_weixin_config(weixin_data: dict) -> WeixinConfig:
    return WeixinConfig(
        session_folder=_coerce_session_folder(weixin_data.get("session_folder")),
        enabled=bool(weixin_data.get("enabled", False)),
        token=str(weixin_data.get("token", "")),
        account_id=str(weixin_data.get("account_id", "")),
        base_url=str(weixin_data.get("base_url", "") or "https://ilinkai.weixin.qq.com"),
        dm_policy=str(weixin_data.get("dm_policy", "allowlist") or "allowlist"),
        allowed_user_ids=_coerce_opaque_str_ids(weixin_data.get("allowed_user_ids")),
        soft_threshold_pct=_threshold_pct(weixin_data.get("soft_threshold_pct"), 80),
        hard_threshold_pct=_threshold_pct(weixin_data.get("hard_threshold_pct"), 95),
    )


def _build_whatsapp_config(whatsapp_data: dict) -> WhatsAppConfig:
    return WhatsAppConfig(
        session_folder=_coerce_session_folder(whatsapp_data.get("session_folder")),
        enabled=bool(whatsapp_data.get("enabled", False)),
        dm_policy=str(whatsapp_data.get("dm_policy", "self") or "self"),
        allowed_wa_ids=_coerce_str_ids(whatsapp_data.get("allowed_wa_ids")),
        groups=_coerce_whatsapp_groups(whatsapp_data.get("groups")),
        db_path=str(whatsapp_data.get("db_path", "")),
        soft_threshold_pct=_threshold_pct(whatsapp_data.get("soft_threshold_pct"), 80),
        hard_threshold_pct=_threshold_pct(whatsapp_data.get("hard_threshold_pct"), 95),
    )


def _build_discord_config(discord_data: dict) -> DiscordConfig:
    return DiscordConfig(
        session_folder=_coerce_session_folder(discord_data.get("session_folder")),
        enabled=bool(discord_data.get("enabled", False)),
        bot_token=str(discord_data.get("bot_token", "")),
        # Discord user IDs are numeric snowflakes that exceed 2^53 —
        # keep them as strings (JSON round-trip safe, matches the
        # transport's string comparison).
        allowed_user_ids=_coerce_str_ids(discord_data.get("allowed_user_ids")),
        allowed_thread_ids=_coerce_str_ids(discord_data.get("allowed_thread_ids")),
        allowed_channel_ids=_coerce_str_ids(discord_data.get("allowed_channel_ids")),
        auto_thread=bool(discord_data.get("auto_thread", True)),
        soft_threshold_pct=_threshold_pct(discord_data.get("soft_threshold_pct"), 80),
        reactions_enabled=bool(discord_data.get("reactions_enabled", True)),
        show_thinking=bool(discord_data.get("show_thinking", False)),
    )


def _build_webex_config(webex_data: dict) -> WebexConfig:
    return WebexConfig(
        session_folder=_coerce_session_folder(webex_data.get("session_folder")),
        enabled=bool(webex_data.get("enabled", False)),
        bot_token=str(webex_data.get("bot_token", "")),
        allowed_emails=(
            [e for e in webex_data.get("allowed_emails", []) if isinstance(e, str) and e]
            if isinstance(webex_data.get("allowed_emails", []), list)
            else []
        ),
        # Group spaces are a SECURITY decision, so the read is as explicit
        # as the write: a field the loader forgets is not merely lost, it
        # silently reverts to the safe default on the next restart while
        # the settings panel keeps showing the saved value it read from
        # config.json — the operator sees an enabled space allow-list and
        # the gateway answers nobody.
        allow_group_rooms=bool(webex_data.get("allow_group_rooms", False)),
        allowed_room_ids=[
            r for r in _safe_list(webex_data.get("allowed_room_ids")) if isinstance(r, str) and r
        ],
        reply_in_thread=bool(webex_data.get("reply_in_thread", True)),
        wdm_base=str(webex_data.get("wdm_base", "") or ""),
        soft_threshold_pct=_threshold_pct(webex_data.get("soft_threshold_pct"), 80),
        hard_threshold_pct=_threshold_pct(webex_data.get("hard_threshold_pct"), 95),
    )


def _build_imessage_config(imessage_data: dict) -> IMessageConfig:
    return IMessageConfig(
        session_folder=_coerce_session_folder(imessage_data.get("session_folder")),
        enabled=bool(imessage_data.get("enabled", False)),
        db_path=str(imessage_data.get("db_path", "")),
        allowed_handles=[
            h for h in _safe_list(imessage_data.get("allowed_handles")) if isinstance(h, str) and h
        ],
        service=str(imessage_data.get("service", "") or "imessage"),
        soft_threshold_pct=_threshold_pct(imessage_data.get("soft_threshold_pct"), 80),
        hard_threshold_pct=_threshold_pct(imessage_data.get("hard_threshold_pct"), 95),
    )


def _build_teams_config(teams_data: dict) -> TeamsConfig:
    return TeamsConfig(
        session_folder=_coerce_session_folder(teams_data.get("session_folder")),
        enabled=bool(teams_data.get("enabled", False)),
        app_id=str(teams_data.get("app_id", "")),
        # Secret is env-only (MICROSOFT_APP_PASSWORD). Never sourced from
        # config.json, which the agent can read — keeps the Azure Bot
        # credential out of any agent-readable file.
        app_password="",
        tenant_id=str(teams_data.get("tenant_id", "")),
        allowed_emails=(
            [e for e in teams_data.get("allowed_emails", []) if isinstance(e, str) and e]
            if isinstance(teams_data.get("allowed_emails", []), list)
            else []
        ),
        soft_threshold_pct=_threshold_pct(teams_data.get("soft_threshold_pct"), 80),
        hard_threshold_pct=_threshold_pct(teams_data.get("hard_threshold_pct"), 95),
    )


def _build_wecom_config(wecom_data: dict) -> WeComConfig:
    return WeComConfig(
        session_folder=_coerce_session_folder(wecom_data.get("session_folder")),
        # _safe_bool, not bool(): `bool("false")` is True, so a JSON string
        # would read the operator's "off" as "on" -- enabling a channel,
        # or opening it to every org member, from a config value that says the
        # opposite. A non-bool must read as the default, not as truthy.
        enabled=_safe_bool(wecom_data.get("enabled"), False),
        allowed_users=[
            u
            for u in _safe_list(wecom_data.get("allowed_users"))
            if isinstance(u, dict) and u.get("userid")
        ],
        allow_all_users=_safe_bool(wecom_data.get("allow_all_users"), False),
        ws_url=str(wecom_data.get("ws_url", "wss://openws.work.weixin.qq.com")),
        soft_threshold_pct=_threshold_pct(wecom_data.get("soft_threshold_pct"), 80),
        hard_threshold_pct=_threshold_pct(wecom_data.get("hard_threshold_pct"), 95),
    )


def _build_feishu_config(feishu_data: dict) -> FeishuConfig:
    return FeishuConfig(
        enabled=_safe_bool(feishu_data.get("enabled"), False),
        allowed_open_ids=_coerce_opaque_str_ids(feishu_data.get("allowed_open_ids")),
        # Shape-safe coercion rather than bool() / a raw comprehension:
        # the schema type check already substitutes the default for a
        # wrong-typed value, and these helpers keep the guarantee local
        # to the parse (and dedupe + strip the opaque ou_/oc_ ids).
        allow_group=_safe_bool(feishu_data.get("allow_group"), False),
        allowed_group_ids=_coerce_opaque_str_ids(feishu_data.get("allowed_group_ids")),
        soft_threshold_pct=_safe_int(feishu_data.get("soft_threshold_pct", 80), 80),
        hard_threshold_pct=_safe_int(feishu_data.get("hard_threshold_pct", 95), 95),
        session_folder=_coerce_session_folder(feishu_data.get("session_folder")),
    )


def _build_wakatime_config(wakatime_data: dict) -> WakaTimeConfig:
    return WakaTimeConfig(
        enabled=bool(wakatime_data.get("enabled", False)),
        api_base_url=str(wakatime_data.get("api_base_url", "") or ""),
        send_heartbeats=_safe_bool(wakatime_data.get("send_heartbeats", False), False),
    )


# ---------------------------------------------------------------------------
# Speech-to-text (DTO and degradation rules in ``config.sections``).
# ---------------------------------------------------------------------------


def _build_stt_config(stt_data: dict) -> SttConfig:
    return SttConfig(
        enabled=_safe_bool(stt_data.get("enabled"), True),
        provider=_validated_stt_provider(stt_data.get("provider", STT_PROVIDER_LOCAL)),
        model=_validated_stt_model(stt_data.get("model", _STT_DEFAULT_MODEL)),
        language_code=stt_data.get("language_code", _sections.STT_LANGUAGE_AUTO),
        # Reached through the module rather than re-exported: the loader facade's
        # import list from `sections` is a frozen pre-split snapshot
        # (test_config_module_boundaries), so a new name must not join it.
        polish=_safe_bool(stt_data.get("polish"), False),
        streaming=_safe_bool(stt_data.get("streaming"), True),
        silence_ms=_safe_int(
            stt_data.get("silence_ms"),
            _STT_DEFAULT_SILENCE_MS,
            lo=_STT_MIN_SILENCE_MS,
            hi=_STT_INTERVAL_MS_MAX,
        ),
        partial_interval_ms=_safe_int(
            stt_data.get("partial_interval_ms"),
            _STT_DEFAULT_PARTIAL_INTERVAL_MS,
            lo=_STT_MIN_PARTIAL_INTERVAL_MS,
            hi=_STT_INTERVAL_MS_MAX,
        ),
        idle_evict_secs=_safe_int(
            stt_data.get("idle_evict_secs"),
            _STT_DEFAULT_IDLE_EVICT_SECS,
            lo=_STT_IDLE_EVICT_SECS_MIN,
            hi=_STT_IDLE_EVICT_SECS_MAX,
        ),
        endpointing=_safe_bool(stt_data.get("endpointing"), False),
        dictation_panel=_safe_bool(stt_data.get("dictation_panel"), True),
        timeout_secs=_safe_int(
            stt_data.get("timeout_secs"),
            _STT_DEFAULT_TIMEOUT_SECS,
            lo=_STT_MIN_TIMEOUT_SECS,
            hi=_STT_MAX_TIMEOUT_SECS,
        ),
        transcribe_region=stt_data.get("transcribe_region", "us-east-1"),
        transcribe_profile=stt_data.get("transcribe_profile", ""),
    )
