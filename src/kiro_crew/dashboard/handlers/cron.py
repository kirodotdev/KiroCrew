"""Cron job and Lessons CRUD API handlers."""

from __future__ import annotations

import importlib as _importlib
import sys as _sys
from typing import TYPE_CHECKING

from kiro_crew import _facade_mirror

# This module is the facade of the cron and lesson routes: every name is reached through
# it as ``kiro_crew.dashboard.handlers.cron.<name>``. A read is served by the owner that
# binds the name, and a write -- ``monkeypatch.setattr``, ``mock.patch`` -- reaches every
# owner holding it (:mod:`kiro_crew._facade_mirror`). Owners never import this module. New
# work goes to the owner of its responsibility:
#   ``cron_shared`` -- the refusals, limits and caller checks several cron routes share
#   ``cron_caller_session`` -- recognizing the session a member-scoped call comes from
#   ``cron_lesson_supersede`` -- retiring the lessons a new rule contradicts
#   ``cron_lessons`` -- the lesson routes: create, delete and list
#   ``cron_jobs`` -- creating, updating, enabling and deleting jobs, and the cron tools route
#   ``cron_script_source`` -- reading and rendering a script job's source
#   ``cron_secret_grants`` -- approving, denying and revoking a job's secret grant
#   ``cron_runs`` -- running, cancelling and acknowledging a job, and its run history
#   ``cron_board`` -- the job list and the folders it is grouped into
#: Owner modules, lowest layer first: the first holder of a name answers its reads.
_OWNERS = (
    "kiro_crew.dashboard.handlers.cron_shared",
    "kiro_crew.dashboard.handlers.cron_caller_session",
    "kiro_crew.dashboard.handlers.cron_lesson_supersede",
    "kiro_crew.dashboard.handlers.cron_lessons",
    "kiro_crew.dashboard.handlers.cron_jobs",
    "kiro_crew.dashboard.handlers.cron_script_source",
    "kiro_crew.dashboard.handlers.cron_secret_grants",
    "kiro_crew.dashboard.handlers.cron_runs",
    "kiro_crew.dashboard.handlers.cron_board",
)  # the loop below imports them in this order
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Name the facade serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(_facade_mirror.install(_sys.modules[__name__], _OWNERS, excluded=()).served)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
#: What a star import binds: every public name of the unsplit module's namespace.
__all__ = sorted(n for n in set(globals()) | set(_EXPORTS) if not n.startswith("_"))
if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.dashboard.handlers.cron_board import (  # noqa: F401
        _cron_folders_lock,
        _get_cron_folders_lock,
        api_cron_folders,
        api_cron_folders_create,
        api_cron_folders_delete,
        api_cron_folders_update,
        api_crons,
        logger,
    )
    from kiro_crew.dashboard.handlers.cron_caller_session import (  # noqa: F401
        _headless_mode_refusal,
        _is_temporary_transcript,
        _recognize_session,
    )
    from kiro_crew.dashboard.handlers.cron_jobs import (  # noqa: F401
        _MAX_BATCH_DELETE,
        _MAX_CRON_BODY_BYTES,
        _T,
        _ChatFolderGone,
        _persist_holding_folder,
        _resolve_chat_folder_id,
        _resolve_one_shot_at,
        _schema_field,
        _unknown_chat_folder_response,
        api_cron_batch_delete,
        api_cron_delete,
        api_cron_enable,
        api_cron_tools,
        api_cron_update,
        api_crons_create,
    )
    from kiro_crew.dashboard.handlers.cron_lesson_supersede import (  # noqa: F401
        _CONTRADICTION_MODEL,
        _CONTRADICTION_PROMPT,
        _CONTRADICTION_TIMEOUT,
        _classify_contradiction,
        _resolve_and_supersede,
        _resolve_contradictions,
    )
    from kiro_crew.dashboard.handlers.cron_lessons import (  # noqa: F401
        _LESSON_EMBED_DEADLINE_SECS,
        _candidate_applies,
        _embed_lesson_bounded,
        _lesson_jsonl_store,
        _lesson_list_page,
        _lesson_scope_selector,
        _load_workspace_lessons,
        _prepare_member_lesson_store,
        _surface_lesson_saved,
        api_lessons,
        api_lessons_create,
        api_lessons_delete,
    )
    from kiro_crew.dashboard.handlers.cron_runs import (  # noqa: F401
        api_cron_ack,
        api_cron_cancel,
        api_cron_history,
        api_cron_history_all,
        api_cron_history_detail,
        api_cron_run,
        api_cron_to_chat,
    )
    from kiro_crew.dashboard.handlers.cron_script_source import (  # noqa: F401
        _SCRIPT_SOURCE_MAX_BYTES,
        _display_source,
        _read_script_source_sync,
        api_cron_script_source,
    )
    from kiro_crew.dashboard.handlers.cron_secret_grants import (  # noqa: F401
        _SHA256_HEX_RE,
        _is_str_mapping,
        _promote_pending_grant,
        api_cron_secret_grant,
    )
    from kiro_crew.dashboard.handlers.cron_shared import (  # noqa: F401
        _CRON_BUSY_BODY,
        _CRON_BUSY_STATUS,
        _app_caller,
        _audit_app_cron,
        _audit_unavailable_response,
        _cron_unreadable_response,
        _invalid_path_id_response,
        _redacted_grant_map,
        _refuse_foreign_app_job,
        _sel,
    )
if TYPE_CHECKING:  # names it serves from outside the family, from their homes
    from kiro_crew import (  # noqa: F401
        model_registry,
    )
    from kiro_crew.config.loader import (  # noqa: F401
        KiroCrewConfig,
        config_dir,
    )
    from kiro_crew.config.memory_sections import (  # noqa: F401
        memory_writes_off_key,
    )
    from kiro_crew.context import (  # noqa: F401
        ContextBuilder,
    )
    from kiro_crew.cron import (  # noqa: F401
        CronPauseNotSaved,
        CronPendingMismatch,
        CronStoreBusy,
        CronStoreUnreadable,
        is_valid_timezone,
        parse_time_string,
    )
    from kiro_crew.cron_script import (  # noqa: F401
        _read_script_body,
        bump_grant_epoch,
        commit_grant_epoch,
        compute_secret_env_pin,
        delivery_fingerprint,
        peek_grant_epoch,
        resolve_script_path,
        validate_secret_env_grant,
    )
    from kiro_crew.cron_service.model import (  # noqa: F401
        validate_auto_pause_after_failures,
    )
    from kiro_crew.dashboard.chat_persistence import (  # noqa: F401
        _restore_dismissed_source_links,
    )
    from kiro_crew.dashboard.cron_inject import (  # noqa: F401
        apply_cron_turn_timeout,
        chat_folder_exists,
        hydrate_slot_from_history,
        inject_cron_result_to_dashboard,
        move_cron_job_tab,
        prefetch_cron_dismissed,
        restore_cron_turn_timeout,
    )
    from kiro_crew.dashboard.cron_timeout_field import (  # noqa: F401
        timeout_secs_from_body,
    )
    from kiro_crew.dashboard.handlers._shared import (  # noqa: F401
        _blocks_reads_session,
        _get_active_workspace,
        _get_lessons,
        _get_memory,
        _is_restricted_session,
        _owner_denial_response,
        _probe_persisted_session,
        _redact_memory_field,
        guard_owner_surface_routes,
        read_bounded_json,
        require_owner_dashboard_request,
        resolve_lesson_memory_store,
    )
    from kiro_crew.dashboard.handlers.source_providers import (  # noqa: F401
        is_owner_dashboard_request,
    )
    from kiro_crew.dashboard.slot_ownership import (  # noqa: F401
        app_holds_gateway_key,
    )
    from kiro_crew.dashboard.state import (  # noqa: F401
        DashboardState,
        SlotOrigin,
        note_crew_log_class,
    )
    from kiro_crew.executors import (  # noqa: F401
        discovery_executor,
    )
    from kiro_crew.history import (  # noqa: F401
        is_incognito_transcript,
    )
    from kiro_crew.hooks import (  # noqa: F401
        FileTooLargeError,
        safe_read_file_bytes_nolink,
    )
    from kiro_crew.lesson_validation import (  # noqa: F401
        LESSON_APPLIES_ON_TOPIC,
        LESSON_APPLIES_UNSTATED,
        LESSON_APPLIES_VALUES,
        LESSON_REFUSED_AT_CAPACITY,
    )
    from kiro_crew.lesson_validation import (  # noqa: F401
        authored_lesson_applies as _authored_lesson_applies,
    )
    from kiro_crew.lesson_validation import (  # noqa: F401
        contains_volatile_lesson_fact,
    )
    from kiro_crew.llm_helpers import (  # noqa: F401
        run_bg_oneliner,
    )
    from kiro_crew.loop_lock import (  # noqa: F401
        LoopBoundLock,
    )
    from kiro_crew.messaging.link import (  # noqa: F401
        is_channel_session_key,
    )
    from kiro_crew.project_scope import (  # noqa: F401
        canonical_scope,
        scope_is_admissible,
        scope_selector_is_inadmissible,
    )
    from kiro_crew.secrets import (  # noqa: F401
        SecretVault,
    )
    from kiro_crew.security import (  # noqa: F401
        redact_credentials,
        redact_exfiltration_urls,
    )
    from kiro_crew.validation import (  # noqa: F401
        _MODEL_NAME_RE,
        ALLOWED_LESSON_SCOPES,
        CHANNEL_ID_RE,
        CHANNEL_MAX_LEN,
        CRON_ADD_SCHEMA,
        LEARN_ADD_SCHEMA,
        LESSON_LIST_LIMIT,
        LESSON_LIST_LIMIT_MAX,
        MAX_CRON_MESSAGE,
        MAX_SHORT_STRING,
        SLACK_THREAD_TS_RE,
        WORKSPACE_NAME_RE,
        FieldSpec,
        ValidationError,
        clamp_query_offset,
        normalize_lesson_category,
        validate_string_field,
        validate_tool_args,
    )
