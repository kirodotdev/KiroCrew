"""Centralized input/output validation for MCP tools and API endpoints.

All tool inputs from untrusted sources (LLM, end user, other MCP tools)
are validated here before execution.  Responses are sanitized and
truncated before returning to callers.

Implements: SDO-183 (Tool Input and Response Validation)
- Schema validation with type enforcement
- Length and size limits
- Unicode normalization and hidden character stripping
- Allow-list approach for enums and key patterns
- Semantic/business logic checks (positive numbers, valid timestamps, etc.)
- Response truncation to prevent resource exhaustion
"""

from __future__ import annotations

import importlib as _importlib
import json
import logging
import re
import subprocess
import sys
import sys as _sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import PureWindowsPath
from typing import TYPE_CHECKING, Any, Iterable

from kiro_crew import _facade_mirror
from kiro_crew import member_goals as _goals

# The artifact tag rule lives with the store's other field grammar and is read
# here so the tool gate and the store cannot disagree about a tag. Import-safe:
# ``artifact_store.rules`` loads the record dataclasses and the slug hash
# fallback, never ``kiro_crew.artifacts`` (the service module whose import from
# here would close the ``artifacts -> hooks -> webhooks -> validation`` cycle).
from kiro_crew.artifact_store.rules import ALLOWED_KINDS as _ARTIFACT_KINDS
from kiro_crew.artifact_store.rules import MAX_TAG_LEN as ARTIFACT_TAG_MAX
from kiro_crew.artifact_store.rules import normalize_tag as _normalize_artifact_tag

# Computer-use tool names and their argument bounds. Safe to import at module
# scope: ``computer_use.types`` is deliberately dependency-free (it imports
# nothing from ``kiro_crew`` and never touches ctypes), so there is no cycle and
# no native library is loaded on any platform. Aliased so the schema block below
# reads as "the computer-use vocabulary" rather than bare names.
from kiro_crew.computer_use import types as _cu_types
from kiro_crew.config.sections import SUBAGENT_MAX_TURNS_CEILING

# ``MAX_SHORT_STRING`` is re-exported, not just used: it is part of this
# module's surface and 15 other modules read it from here. It is DEFINED in
# ``constants`` so ``execution_context`` can have it without this module's
# import graph -- see the comment at its definition.
from kiro_crew.constants import (
    ARTIFACT_MAX_CONTENT_BYTES,
    AWS_PROFILE_NAME_RE,
    CHANNEL_OWNER_DM_NAMESPACES,
    MAX_BANNER_CHARS,
    MAX_SHORT_STRING,
    SLACK_NAMESPACE,
    WAIT_TOOL_MAX_SECS,
    WINDOWS_DEVICE_STEMS,
)

# Reasoning-effort vocabulary: ``effort.py`` is the single source of truth for
# the valid levels; EFFORT_VALUES additionally admits ``""`` ("unset — defer to
# the role pin / provider default"). Import-safe: ``effort`` pulls in only
# ``model_registry`` (stdlib-only), so no cycle back into validation.
from kiro_crew.effort import EFFORT_VALUES
from kiro_crew.lesson_validation import LESSON_APPLIES_VALUES
from kiro_crew.monitoring.limits import MAX_RUNTIME_CEILING_SECS, validate_runtime_secs
from kiro_crew.monitoring.models import (
    MAX_MONITOR_AGENT_TURNS,
    MAX_MONITOR_CADENCE_SECS,
    MAX_MONITOR_PROVIDER_ERRORS,
    MAX_MONITOR_STOP_REASON_CHARS,
    MAX_MONITOR_TOKENS,
    MAX_MONITOR_WAKE_INSTRUCTIONS_CHARS,
    MIN_MONITOR_CADENCE_SECS,
)
from kiro_crew.monitoring.registry import (
    publicly_armable_kinds,
    publicly_armable_objectives,
)
from kiro_crew.project_scope import SCOPE_FRAGMENT_RE
from kiro_crew.session_directive import MAX_TOOL_RESULT_CHARS
from kiro_crew.work_vocab import (
    WORK_BLOCKED_REASONS,
    WORK_ITEM_STATES,
    WORK_REASON_STATUSES,
    WORK_VERDICTS,
    WORK_WORKER_STATUSES,
)

# This module is the import path of every name the validation layer offers. The imports
# above stay bound because callers read them here; the definitions live in the owner
# modules of ``kiro_crew.validation_parts``, and a write through this module (a patch)
# reaches every module holding the name (:mod:`kiro_crew._facade_mirror`). Owners never
# import this module. New work goes to the owner of its responsibility:
#   ``input_bounds`` -- string-length tiers, fan-out caps and the identifier grammars
#   ``text_sanitizer`` -- hidden-character stripping and string / response sanitization
#   ``field_engine`` -- ``ValidationError`` and the ``FieldSpec`` / ``ToolSchema`` validator
#   ``json_schema_args`` -- tool arguments checked against a declared MCP ``inputSchema``
#   ``wire_envelopes`` -- JSON-RPC envelopes, the tool-result frame, API request bodies
#   ``core_agent_schemas`` -- core tools: spawn, lessons, session ledger, monitor loops
#   ``ask_question_card`` -- the ``ask_question`` bounds, schema and payload validator
#   ``core_turn_schemas`` -- core tools: messages, skills, project, tags, follow-ups, workflows
#   ``artifact_schemas`` -- artifact tools, and the artifact and chat folder / tag tools
#   ``app_schemas`` -- the builtin apps' agent tools
#   ``cron_schemas`` -- the ``kirocrew-cron`` tools and ``MCP_CRON_SCHEMAS``
#   ``dashboard_route_schemas`` -- the dashboard's hook and file route bodies
#   ``messaging_session_schemas`` -- messaging, crews, knowledge, history and session control
#   ``server_registries`` -- ``MCP_CORE_SCHEMAS`` and ``MCP_DASHBOARD_SCHEMAS``
#   ``computer_use_schemas`` -- the ``kirocrew-computer`` tools and ``MCP_COMPUTER_SCHEMAS``
#   ``opt_in_server_schemas`` -- the opt-in servers: crew log, debug, work, panel, guide, mate
#: Owner modules, lowest layer first: the first holder of a name answers its reads.
_OWNERS = (
    "kiro_crew.validation_parts.input_bounds",
    "kiro_crew.validation_parts.text_sanitizer",
    "kiro_crew.validation_parts.field_engine",
    "kiro_crew.validation_parts.json_schema_args",
    "kiro_crew.validation_parts.wire_envelopes",
    "kiro_crew.validation_parts.core_agent_schemas",
    "kiro_crew.validation_parts.ask_question_card",
    "kiro_crew.validation_parts.core_turn_schemas",
    "kiro_crew.validation_parts.artifact_schemas",
    "kiro_crew.validation_parts.app_schemas",
    "kiro_crew.validation_parts.cron_schemas",
    "kiro_crew.validation_parts.dashboard_route_schemas",
    "kiro_crew.validation_parts.messaging_session_schemas",
    "kiro_crew.validation_parts.server_registries",
    "kiro_crew.validation_parts.computer_use_schemas",
    "kiro_crew.validation_parts.opt_in_server_schemas",
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
    from kiro_crew.validation_parts.app_schemas import (  # noqa: F401
        _DESIGN_TWEAK_ALLOWED_STATUSES,
        _DESIGN_TWEAK_MAX_TEXT,
        _GIT_SHA_RE,
        _ISSUE_RADAR_CREW_CLEARABLE_FIELDS,
        _ISSUE_RADAR_CREW_EVENT_KINDS,
        _ISSUE_RADAR_CREW_PHASES,
        _ISSUE_RADAR_CREW_SKIP_SCOPES,
        _ISSUE_RADAR_ITEM_KINDS,
        _ISSUE_RADAR_MAX_ITEM_NUMBER,
        _ISSUE_RADAR_PROVIDERS,
        _ISSUE_RADAR_STATUSES,
        _OMC_API_METHODS,
        _OMC_API_PATHS,
        _OMC_MAX_BODY,
        _OMC_QUERY_RE,
        _POD_WORKTREE_MAX_LEN,
        DESIGN_TWEAK_UPDATE_THREAD_SCHEMA,
        ISSUE_RADAR_CREW_READ_SCHEMA,
        ISSUE_RADAR_CREW_RECORD_SCHEMA,
        ISSUE_RADAR_RECORD_INVESTIGATION_SCHEMA,
        OPS_MISSION_CONTROL_ALLOWED_CALLS,
        OPS_MISSION_CONTROL_API_SCHEMA,
        POD_DOWN_SCHEMA,
        POD_LS_SCHEMA,
        POD_STATUS_SCHEMA,
        POD_UP_SCHEMA,
        _validate_crew_record_couples_phase_to_an_event,
        _validate_omc_api,
    )
    from kiro_crew.validation_parts.artifact_schemas import (  # noqa: F401
        _ARTIFACT_COMMENT_ID_RE,
        _ARTIFACT_FOLDER_NAME_MAX,
        _ARTIFACT_FOLDER_REF_MAX,
        _ARTIFACT_KIND_RE,
        _ARTIFACT_SCOPE_RE,
        _ARTIFACT_SLUG_RE,
        _ARTIFACT_SOURCE_RE,
        _CHAT_TAG_MAX_ITEMS,
        _CHAT_TAG_NAME_MAX,
        _CHAT_TAG_REF_MAX,
        _WM_LIFECYCLE_STATUSES,
        _WM_LIST_CAP,
        _WM_PROFILE_RE,
        _WM_SLUG_RE,
        _WM_URL_RE,
        ARTIFACT_AGENT_MARKER,
        ARTIFACT_COMMENT_TEXT_MAX,
        ARTIFACT_CONTENT_MAX,
        ARTIFACT_DELETE_COMMENT_REASON_MAX,
        ARTIFACT_DELETE_COMMENT_SCHEMA,
        ARTIFACT_DELETE_SCHEMA,
        ARTIFACT_FOLDER_CREATE_SCHEMA,
        ARTIFACT_FOLDER_DELETE_SCHEMA,
        ARTIFACT_FOLDER_LIST_SCHEMA,
        ARTIFACT_FOLDER_MOVE_SCHEMA,
        ARTIFACT_FOLDER_RENAME_SCHEMA,
        ARTIFACT_GET_COMMENTS_SCHEMA,
        ARTIFACT_GET_SCHEMA,
        ARTIFACT_LIST_SCHEMA,
        ARTIFACT_MARK_REVIEW_SCHEMA,
        ARTIFACT_MOVE_SCHEMA,
        ARTIFACT_POST_COMMENT_SCHEMA,
        ARTIFACT_REPLY_COMMENT_SCHEMA,
        ARTIFACT_REVERT_SCHEMA,
        ARTIFACT_SAVE_SCHEMA,
        ARTIFACT_UPDATE_SCHEMA,
        ARTIFACT_VERSIONS_SCHEMA,
        ARTIFACT_WEBAPP_METADATA_MAX_BYTES,
        CHAT_FOLDER_CREATE_SCHEMA,
        CHAT_FOLDER_DELETE_SCHEMA,
        CHAT_FOLDER_FILE_SELF_SCHEMA,
        CHAT_FOLDER_MOVE_SCHEMA,
        CHAT_FOLDER_MOVE_SESSION_SCHEMA,
        CHAT_FOLDER_TREE_SCHEMA,
        CHAT_FOLDER_UPDATE_SCHEMA,
        CHAT_SESSION_PIN_MOVE_SCHEMA,
        CHAT_SESSION_PIN_SCHEMA,
        CHAT_TAG_ASSIGN_SCHEMA,
        CHAT_TAG_COLUMN_CREATE_SCHEMA,
        CHAT_TAG_COLUMN_LIST_SCHEMA,
        CHAT_TAG_COLUMN_MOVE_SCHEMA,
        CHAT_TAG_CREATE_SCHEMA,
        CHAT_TAG_LIST_SCHEMA,
        CHAT_TAG_UPDATE_SCHEMA,
        DEPLOY_ARTIFACT_SCHEMA,
        SESSION_MOVE_TO_COLUMN_SCHEMA,
        _validate_artifact_save,
        _validate_artifact_tags,
        _validate_webapp_metadata_shape,
    )
    from kiro_crew.validation_parts.ask_question_card import (  # noqa: F401
        _ASK_ANSWER_LINE_OVERHEAD,
        _ASK_ANSWERS_HEADER,
        _ASK_MAX_ANSWER_LEN,
        _ASK_MAX_DESC_LEN,
        _ASK_MAX_HEADER_LEN,
        _ASK_MAX_LABEL_LEN,
        _ASK_MAX_OPTIONS,
        _ASK_MAX_QUESTION_LEN,
        _ASK_MAX_QUESTIONS,
        _ASK_PAIR_SEPARATOR,
        ASK_QUESTION_SCHEMA,
        ASK_WAIT_SLICE_SECS,
        format_ask_answers,
        validate_ask_user_question,
    )
    from kiro_crew.validation_parts.computer_use_schemas import (  # noqa: F401
        _CU_APP_FIELD,
        _CU_AX_ACTION_RE,
        _CU_CLICK_METHODS,
        _CU_DRAG_PATHS,
        _CU_ELEMENT_FIELD,
        _CU_KEY_SPEC_RE,
        _CU_LAUNCH_APP_FIELD,
        _CU_MOUSE_BUTTONS,
        _CU_OPTIONAL_ELEMENT_FIELD,
        _CU_SCROLL_DIRECTIONS,
        MCP_COMPUTER_SCHEMAS,
        _cu_coord_field,
    )
    from kiro_crew.validation_parts.core_agent_schemas import (  # noqa: F401
        _BACKEND_NAME_RE,
        _MONITOR_WATCH_FIELD,
        _WATCH_WORK_LEDGER,
        AUTONUDGE_STOP_SCHEMA,
        FILE_SEND_SCHEMA,
        JUDGE_OFF_KEY,
        JUDGE_SPEC_KEYS,
        LEARN_ADD_SCHEMA,
        LEARN_LIST_SCHEMA,
        LEARN_REMOVE_SCHEMA,
        MAX_JUDGE_CRITERION_CHARS,
        MAX_JUDGE_TARGET_CHARS,
        MAX_JUDGE_TARGETS,
        MONITOR_INSPECT_SCHEMA,
        MONITOR_START_SCHEMA,
        MONITOR_STOP_SCHEMA,
        MONITOR_UPDATE_SCHEMA,
        MONITOR_WATCH_SCHEMA,
        RESOURCE_STATUS_SCHEMA,
        SESSION_LEDGER_READ_SCHEMA,
        SESSION_LEDGER_RECORD_SCHEMA,
        SPAWN_BATCH_MEMBERS_MAX,
        SPAWN_CONTINUE_SCHEMA,
        SPAWN_LIST_SCHEMA,
        SPAWN_RELEASE_SCHEMA,
        SPAWN_RUN_SCHEMA,
        SPAWN_RUN_TASK_ITEM_SCHEMA,
        SPAWN_STATUS_SCHEMA,
        SPAWN_STEER_SCHEMA,
        SPAWN_SUB_AGENTS_SCHEMA,
        TASK_RUN_SCHEMA,
        _validate_monitor_runtime,
        judge_is_off,
        validate_judge_spec,
    )
    from kiro_crew.validation_parts.core_turn_schemas import (  # noqa: F401
        _ABSOLUTE_PATH_RE,
        _GIT_RESERVED_REFS,
        _WF_RUN_ID_RE,
        CHAT_TAG_SCHEMA,
        DELETE_MESSAGE_SCHEMA,
        FOLLOWUP_BRANCH_RE,
        KIRO_CLI_LOGS_SCHEMA,
        MAX_FOLLOWUP_BRANCH,
        MAX_FOLLOWUP_DESCRIPTION,
        MAX_FOLLOWUP_ITEMS,
        MAX_FOLLOWUP_PROMPT,
        MAX_FOLLOWUP_TITLE,
        NOTHING_TO_DO_SCHEMA,
        RESET_CONVERSATION_SCHEMA,
        SET_PROJECT_SCHEMA,
        SKILL_DISCOVER_SCHEMA,
        SKILL_FETCH_SCHEMA,
        SKILL_SEARCH_SCHEMA,
        SUGGEST_FOLLOWUP_SCHEMA,
        UPDATE_MESSAGE_SCHEMA,
        WORKFLOW_AUTHOR_SCHEMA,
        WORKFLOW_LIBRARY_LIST_SCHEMA,
        WORKFLOW_RERUN_SCHEMA,
        WORKFLOW_RUN_ID_SCHEMA,
        WORKFLOW_RUN_SCHEMA,
        _validate_chat_tag,
        _validate_followup_items,
        _validate_set_project,
        is_valid_followup_branch,
    )
    from kiro_crew.validation_parts.cron_schemas import (  # noqa: F401
        CRON_ADD_SCHEMA,
        CRON_LIST_SCHEMA,
        CRON_PAUSE_SCHEMA,
        CRON_REMOVE_SCHEMA,
        CRON_RESUME_SCHEMA,
        MCP_CRON_SCHEMAS,
        _validate_cron_add_requires_message_or_script,
        _validate_cron_secret_request,
    )
    from kiro_crew.validation_parts.dashboard_route_schemas import (  # noqa: F401
        _FS_PATH_PATTERN,
        FILE_READ_SCHEMA,
        FILE_WRITE_SCHEMA,
        HOOK_CREATE_SCHEMA,
        HOOK_UPDATE_SCHEMA,
        _reject_null_enabled,
        _validate_hook_create,
        _validate_hook_has_action,
        _validate_hook_regex,
        _validate_hook_update,
    )
    from kiro_crew.validation_parts.field_engine import (  # noqa: F401
        _CLAMP_NOTE,
        _CLAMP_NOTE_RE,
        _MAX_EXACT_INT,
        FieldSpec,
        ToolSchema,
        ValidationError,
        _coerce_number_to_string_for_string_field,
        clamp_report,
        clamp_to_max_len,
        coerce_mcp_tool_args,
        coerce_mcp_tool_args_json_schema,
        validate_field,
        validate_tool_args,
    )
    from kiro_crew.validation_parts.input_bounds import (  # noqa: F401
        _AGENT_NAME_RE,
        _JOB_ID_RE,
        _MODEL_NAME_RE,
        _MODEL_SEGMENT,
        _QUALIFIED_MODEL_MAX_CHARS,
        ALLOWED_HOOK_EVENTS,
        ALLOWED_LESSON_APPLIES,
        ALLOWED_LESSON_CATEGORIES,
        ALLOWED_LESSON_SCOPES,
        ALLOWED_SCHEDULE_KINDS,
        ARTIFACT_SLUG_RE,
        BROADCAST_RESPONSE_MARGIN_SECS,
        BROADCAST_TARGET_ALLOWANCE_SECS,
        CHANNEL_ID_RE,
        CHANNEL_MAX_LEN,
        CRON_SESSION_RE,
        JIRA_ISSUE_KEY_RE,
        JIRA_PROJECT_KEY_RE,
        JIRA_SITE_ID_RE,
        JIRA_SITE_URL_RE,
        LESSON_LIST_LIMIT,
        LESSON_LIST_LIMIT_MAX,
        LESSON_LIST_OFFSET_MAX,
        MAX_ACP_SESSION_ID_LEN,
        MAX_BROADCAST_TARGETS,
        MAX_CRON_MESSAGE,
        MAX_LONG_STRING,
        MAX_MEDIUM_STRING,
        MAX_RESPONSE_LEN,
        MAX_SESSION_STATUS_ROWS,
        MAX_SESSION_STATUS_TITLE_CHARS,
        MAX_SKILL_KEY_CHARS,
        MAX_TOOL_NAME_LEN,
        MODEL_ID_RE,
        MODEL_PICKER_HIDDEN_MODEL_ID_RE,
        MODEL_PICKER_HIDDEN_MODELS_MAX,
        QUALIFIED_MODEL_PATTERN,
        REGISTERED_AGENT_NAME_RE,
        SLACK_THREAD_TS_RE,
        TEMPLATE_NAME_RE,
        THEME_CONSENT_SHA_RE,
        USER_ID_RE,
        USER_MAX_LEN,
        WORKSPACE_NAME_RE,
        bounded_session_id,
        clamp_query_offset,
        infer_use_case,
        is_registered_agent_name,
        normalize_lesson_category,
        normalize_theme_consent_sha,
    )
    from kiro_crew.validation_parts.json_schema_args import (  # noqa: F401
        _ANNOTATION_SCHEMA_KEYWORDS,
        _JSON_SCHEMA_TYPES,
        _MCP_ARGS_MAX_DEPTH,
        _MCP_ARGS_MAX_STRING,
        _PATTERN_CHILD_SRC,
        _PATTERN_MAX_LEN,
        _PATTERN_TIMEOUT_SECS,
        _PATTERN_VALUE_MAX_LEN,
        _SUPPORTED_SCHEMA_KEYWORDS,
        _bounded_pattern_search,
        _json_equal,
        _json_type_ok,
        _reject_malformed_keyword_shapes,
        _reject_unsupported_keywords,
        validate_mcp_tool_arguments,
    )
    from kiro_crew.validation_parts.messaging_session_schemas import (  # noqa: F401
        _CHANNEL_TARGET_INCOMPATIBLE,
        _EMOJI_NAME_RE,
        _ISO_DATE_RE,
        _SEND_MESSAGE_SESSION_RE,
        _SLACK_TS_RE,
        _TARGET_ID_RE,
        GET_CHAT_SESSION_SCHEMA,
        KNOWLEDGE_ADD_DOCUMENT_SCHEMA,
        KNOWLEDGE_DEDUP_SCHEMA,
        KNOWLEDGE_LIST_SOURCES_SCHEMA,
        LIST_SESSIONS_SCHEMA,
        LOCAL_KNOWLEDGE_SEARCH_SCHEMA,
        READ_SLACK_PROFILE_SCHEMA,
        REGISTER_HOOK_SCHEMA,
        ROUTE_CREW_SCHEMA,
        SEARCH_CHAT_HISTORY_SCHEMA,
        SELECT_CREW_SCHEMA,
        SEND_MESSAGE_SCHEMA,
        SEND_NOTIFICATION_SCHEMA,
        SESSION_ADOPT_SCHEMA,
        SESSION_BROADCAST_SCHEMA,
        SESSION_CLOSE_SCHEMA,
        SESSION_CONTINUE_SCHEMA,
        SESSION_CREATE_SCHEMA,
        SESSION_END_WAIT_SCHEMA,
        SESSION_FORK_SCHEMA,
        SESSION_GENERATE_TITLE_SCHEMA,
        SESSION_HISTORY_LIST_SCHEMA,
        SESSION_READ_MESSAGE_SCHEMA,
        SESSION_RELEASE_SCHEMA,
        SESSION_RELOAD_SCHEMA,
        SESSION_RENAME_SCHEMA,
        SESSION_RETRY_SCHEMA,
        SESSION_REVIVE_SCHEMA,
        SESSION_SEND_SCHEMA,
        SESSION_SET_MODEL_SCHEMA,
        SESSION_STATUS_SCHEMA,
        SESSION_STOP_SCHEMA,
        SESSION_SUMMARY_SCHEMA,
        WAIT_SCHEMA,
        _validate_channel_routing,
        _validate_session_set_model,
    )
    from kiro_crew.validation_parts.opt_in_server_schemas import (  # noqa: F401
        _CREW_LOG_UNIT_MAX,
        _DEBUG_MAX_ARG_CHARS,
        _DEBUG_MAX_SAMPLE_SECONDS,
        _DEBUG_PROCESS_FORMATS,
        _DEBUG_THREAD_MODES,
        _GUIDE_ID_PATTERN,
        _WORK_ITEM_STATES,
        _WORK_RECORD_ACTIONS,
        _WORK_STATUSES,
        _WORK_VERDICTS,
        CREW_LOG_LIST_SCHEMA,
        CREW_LOG_PROJECTION_SCHEMA,
        CREW_LOG_READ_SCHEMA,
        DASHBOARD_APPLY_SCHEMA,
        DASHBOARD_FIELDS_SCHEMA,
        DASHBOARD_PREVIEW_SCHEMA,
        DASHBOARD_ROLLBACK_SCHEMA,
        DASHBOARD_SAVE_SCHEMA,
        DASHBOARD_TEMPLATES_SCHEMA,
        DASHBOARD_TYPES_SCHEMA,
        DASHBOARD_WRITE_NO_VALUE,
        DASHBOARD_WRITE_SCHEMA,
        DASHBOARD_WRITE_TOOL_SCHEMA,
        DEBUG_GATEWAY_SCHEMA,
        DEBUG_PROCESSES_SCHEMA,
        DEBUG_REFUSALS_SCHEMA,
        DEBUG_SNAPSHOTS_SCHEMA,
        DEBUG_THREADS_SCHEMA,
        GUIDE_CANCEL_SCHEMA,
        GUIDE_LIST_ACTIONS_SCHEMA,
        GUIDE_START_SCHEMA,
        GUIDE_STATUS_SCHEMA,
        MCP_CREW_LOG_SCHEMAS,
        MCP_DEBUG_SCHEMAS,
        MCP_GUIDE_SCHEMAS,
        MCP_MATE_SCHEMAS,
        MCP_PANEL_SCHEMAS,
        MCP_WORK_SCHEMAS,
        PANEL_PUBLISH_SCHEMA,
        PANEL_TEMPLATES_SCHEMA,
        WORK_BRIEF_SCHEMA,
        WORK_LEDGER_READ_SCHEMA,
        WORK_LEDGER_REBUILD_SCHEMA,
        WORK_LEDGER_RECORD_SCHEMA,
        WORK_REPORT_SCHEMA,
        _validate_dashboard_write_tool,
        _validate_work_artifacts,
        _validate_work_report,
    )
    from kiro_crew.validation_parts.server_registries import (  # noqa: F401
        MCP_CORE_SCHEMAS,
        MCP_DASHBOARD_SCHEMAS,
    )
    from kiro_crew.validation_parts.text_sanitizer import (  # noqa: F401
        _ALLOWED_CONTROL,
        _ALLOWED_FORMAT,
        _HIDDEN_CATEGORIES,
        _drop_hidden_except_marks,
        _trim_edges,
        normalize_unicode,
        sanitize_json_values,
        sanitize_response,
        sanitize_string,
        strip_hidden_unicode,
    )
    from kiro_crew.validation_parts.wire_envelopes import (  # noqa: F401
        JSONRPC_INTERNAL_ERROR,
        JSONRPC_INVALID_PARAMS,
        JSONRPC_INVALID_REQUEST,
        JSONRPC_PARSE_ERROR,
        JsonRpcEnvelopeError,
        McpTextContent,
        build_tool_response,
        validate_api_body,
        validate_jsonrpc_request,
        validate_jsonrpc_response,
        validate_string_field,
    )
