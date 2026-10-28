"""``validation.py`` keeps its BASE surface while its definitions live in ``validation_parts``.

``kiro_crew.validation`` is a mirror facade (:mod:`kiro_crew._facade_mirror`) over the
owner modules of ``kiro_crew.validation_parts``. ``test/test_facade_mirror_guards.py``
holds it to the contract every mirrored facade shares: one object per name, the
``TYPE_CHECKING`` manifest, no owner importing its facade, and every patch through it
reaching every module that holds the name. These checks pin what is particular to this
facade:

* every top-level name the one-module file defined still resolves through the facade as
  the object its one owner defines, and ``dir()`` lists it (the posture view and its
  tests walk ``dir(kiro_crew.validation)`` for the schema registries);
* the owners are exactly the package's modules, imported eagerly, lowest layer first, and
  each imports only owners listed before it;
* the facade imports nothing beyond its BASE import header and its own glue -- the
  property the ``setup.cfg`` F401 row for it switches off -- and a planted import is
  reported;
* the two exceptions keep the facade's dotted name in a traceback, and the truncation
  warning keeps the facade's logger name;
* a git ref is still checked component by component.
"""

from __future__ import annotations

import ast
import importlib
import logging
import os
import pkgutil
import subprocess
import sys
import traceback
from pathlib import Path

import pytest

import kiro_crew
from kiro_crew import _facade_mirror, validation
from kiro_crew import validation_parts as _parts
from kiro_crew.subprocess_utf8 import UTF8_TEXT

FACADE = "kiro_crew.validation"
PACKAGE = "kiro_crew.validation_parts"
_FACADE_PATH = Path(validation.__file__)

#: Every top-level name the one-module file defined (frozen at the split).
_BASE_DEFINED = frozenset("""
    AGENT_NAME_HINT
    ALLOWED_HOOK_EVENTS ALLOWED_LESSON_APPLIES ALLOWED_LESSON_CATEGORIES ALLOWED_LESSON_SCOPES
    ALLOWED_SCHEDULE_KINDS ARTIFACT_AGENT_MARKER ARTIFACT_COMMENT_TEXT_MAX ARTIFACT_CONTENT_MAX
    ARTIFACT_DELETE_COMMENT_REASON_MAX ARTIFACT_DELETE_COMMENT_SCHEMA ARTIFACT_DELETE_SCHEMA
    ARTIFACT_FOLDER_CREATE_SCHEMA ARTIFACT_FOLDER_DELETE_SCHEMA ARTIFACT_FOLDER_LIST_SCHEMA
    ARTIFACT_FOLDER_MOVE_SCHEMA ARTIFACT_FOLDER_RENAME_SCHEMA ARTIFACT_GET_COMMENTS_SCHEMA
    ARTIFACT_GET_SCHEMA ARTIFACT_LIST_SCHEMA ARTIFACT_MARK_REVIEW_SCHEMA ARTIFACT_MOVE_SCHEMA
    ARTIFACT_POST_COMMENT_SCHEMA ARTIFACT_REPLY_COMMENT_SCHEMA ARTIFACT_REVERT_SCHEMA
    ARTIFACT_SAVE_SCHEMA ARTIFACT_SLUG_RE ARTIFACT_UPDATE_SCHEMA ARTIFACT_VERSIONS_SCHEMA
    ARTIFACT_WEBAPP_METADATA_MAX_BYTES ASK_QUESTION_SCHEMA ASK_WAIT_SLICE_SECS
    AUTONUDGE_STOP_SCHEMA BROADCAST_RESPONSE_MARGIN_SECS BROADCAST_TARGET_ALLOWANCE_SECS
    CHANNEL_ID_RE CHANNEL_MAX_LEN CHAT_FOLDER_CREATE_SCHEMA CHAT_FOLDER_DELETE_SCHEMA
    CHAT_FOLDER_FILE_SELF_SCHEMA CHAT_FOLDER_MOVE_SCHEMA CHAT_FOLDER_MOVE_SESSION_SCHEMA
    CHAT_FOLDER_TREE_SCHEMA CHAT_FOLDER_UPDATE_SCHEMA CHAT_SESSION_PIN_MOVE_SCHEMA
    CHAT_SESSION_PIN_SCHEMA CHAT_TAG_ASSIGN_SCHEMA CHAT_TAG_COLUMN_CREATE_SCHEMA
    CHAT_TAG_COLUMN_LIST_SCHEMA CHAT_TAG_COLUMN_MOVE_SCHEMA CHAT_TAG_CREATE_SCHEMA
    CHAT_TAG_LIST_SCHEMA CHAT_TAG_SCHEMA CHAT_TAG_UPDATE_SCHEMA CREW_LOG_LIST_SCHEMA
    CREW_LOG_PROJECTION_SCHEMA CREW_LOG_READ_SCHEMA CRON_ADD_SCHEMA CRON_LIST_SCHEMA
    CRON_PAUSE_SCHEMA CRON_REMOVE_SCHEMA CRON_RESUME_SCHEMA CRON_SESSION_RE
    DASHBOARD_APPLY_SCHEMA DASHBOARD_FIELDS_SCHEMA DASHBOARD_PREVIEW_SCHEMA
    DASHBOARD_ROLLBACK_SCHEMA DASHBOARD_SAVE_SCHEMA DASHBOARD_SNAPSHOT_SCHEMA
    DASHBOARD_TEMPLATES_SCHEMA
    DASHBOARD_TYPES_SCHEMA DASHBOARD_WRITE_NO_VALUE DASHBOARD_WRITE_SCHEMA
    DASHBOARD_WRITE_TOOL_SCHEMA DEBUG_GATEWAY_SCHEMA DEBUG_PROCESSES_SCHEMA
    DEBUG_REFUSALS_SCHEMA DEBUG_SNAPSHOTS_SCHEMA DEBUG_THREADS_SCHEMA DELETE_MESSAGE_SCHEMA
    DEPLOY_ARTIFACT_SCHEMA DESIGN_TWEAK_UPDATE_THREAD_SCHEMA FILE_READ_SCHEMA FILE_SEND_SCHEMA
    FILE_WRITE_SCHEMA FOLLOWUP_BRANCH_RE FieldSpec GET_CHAT_SESSION_SCHEMA GUIDE_CANCEL_SCHEMA
    GUIDE_LIST_ACTIONS_SCHEMA GUIDE_START_SCHEMA GUIDE_STATUS_SCHEMA HOOK_CREATE_SCHEMA
    HOOK_UPDATE_SCHEMA ISSUE_RADAR_CREW_READ_SCHEMA ISSUE_RADAR_CREW_RECORD_SCHEMA
    ISSUE_RADAR_RECORD_INVESTIGATION_SCHEMA JIRA_ISSUE_KEY_RE JIRA_PROJECT_KEY_RE
    JIRA_SITE_ID_RE JIRA_SITE_URL_RE JSONRPC_INTERNAL_ERROR JSONRPC_INVALID_PARAMS
    JSONRPC_INVALID_REQUEST JSONRPC_PARSE_ERROR JUDGE_OFF_KEY JUDGE_SPEC_KEYS
    JsonRpcEnvelopeError KIRO_CLI_LOGS_SCHEMA KNOWLEDGE_ADD_DOCUMENT_SCHEMA
    KNOWLEDGE_DEDUP_SCHEMA KNOWLEDGE_LIST_SOURCES_SCHEMA LEARN_ADD_SCHEMA LEARN_LIST_SCHEMA
    LEARN_REMOVE_SCHEMA LESSON_LIST_LIMIT LESSON_LIST_LIMIT_MAX LESSON_LIST_OFFSET_MAX
    LIST_SESSIONS_SCHEMA LOCAL_KNOWLEDGE_SEARCH_SCHEMA MAX_ACP_SESSION_ID_LEN
    MAX_BROADCAST_TARGETS MAX_CRON_MESSAGE MAX_FOLLOWUP_BRANCH MAX_FOLLOWUP_DESCRIPTION
    MAX_FOLLOWUP_ITEMS MAX_FOLLOWUP_PROMPT MAX_FOLLOWUP_TITLE MAX_JUDGE_CRITERION_CHARS
    MAX_JUDGE_TARGETS MAX_JUDGE_TARGET_CHARS MAX_LONG_STRING MAX_MEDIUM_STRING MAX_RESPONSE_LEN
    MAX_SESSION_STATUS_ROWS MAX_SESSION_STATUS_TITLE_CHARS MAX_SKILL_KEY_CHARS MAX_TOOL_NAME_LEN
    MCP_COMPUTER_SCHEMAS MCP_CORE_SCHEMAS MCP_CREW_LOG_SCHEMAS MCP_CRON_SCHEMAS
    MCP_DASHBOARD_SCHEMAS MCP_DEBUG_SCHEMAS MCP_GUIDE_SCHEMAS MCP_MATE_SCHEMAS MCP_PANEL_SCHEMAS
    MCP_WORK_SCHEMAS MODEL_ID_RE MODEL_NAME_HINT MODEL_PICKER_HIDDEN_MODELS_MAX
    MODEL_PICKER_HIDDEN_MODEL_ID_RE
    MONITOR_INSPECT_SCHEMA MONITOR_START_SCHEMA MONITOR_STOP_SCHEMA MONITOR_UPDATE_SCHEMA
    MONITOR_WATCH_SCHEMA McpTextContent NOTHING_TO_DO_SCHEMA OPS_MISSION_CONTROL_ALLOWED_CALLS
    OPS_MISSION_CONTROL_API_SCHEMA PANEL_PUBLISH_SCHEMA PANEL_TEMPLATES_SCHEMA POD_DOWN_SCHEMA
    POD_LS_SCHEMA POD_STATUS_SCHEMA POD_UP_SCHEMA QUALIFIED_MODEL_PATTERN
    READ_SLACK_PROFILE_SCHEMA REGISTERED_AGENT_NAME_RE REGISTER_HOOK_SCHEMA
    RESET_CONVERSATION_SCHEMA RESOURCE_STATUS_SCHEMA ROUTE_CREW_SCHEMA
    SEARCH_CHAT_HISTORY_SCHEMA SELECT_CREW_SCHEMA SEND_MESSAGE_SCHEMA SEND_NOTIFICATION_SCHEMA
    SESSION_ADOPT_SCHEMA SESSION_BROADCAST_SCHEMA SESSION_CLOSE_SCHEMA SESSION_COLOR_CHOICES
    SESSION_CONTINUE_SCHEMA SESSION_CREATE_SCHEMA SESSION_END_WAIT_SCHEMA SESSION_FORK_SCHEMA
    SESSION_GENERATE_TITLE_SCHEMA SESSION_HANDOFF_SCHEMA SESSION_HISTORY_LIST_SCHEMA
    SESSION_LEDGER_READ_SCHEMA SESSION_LEDGER_RECORD_SCHEMA SESSION_MOVE_TO_COLUMN_SCHEMA
    SESSION_PALETTE_SIZE SESSION_READ_MESSAGE_SCHEMA SESSION_RELEASE_SCHEMA
    SESSION_RELOAD_SCHEMA SESSION_RENAME_SCHEMA SESSION_RETRY_SCHEMA SESSION_REVIVE_SCHEMA
    SESSION_SEND_SCHEMA SESSION_SET_COLOR_SCHEMA SESSION_SET_MODEL_SCHEMA SESSION_STATUS_SCHEMA
    SESSION_STOP_SCHEMA SESSION_SUMMARY_SCHEMA SET_PROJECT_SCHEMA SKILL_DISCOVER_SCHEMA
    SKILL_FETCH_SCHEMA SKILL_SEARCH_SCHEMA SLACK_THREAD_TS_RE SPAWN_BATCH_MEMBERS_MAX
    SPAWN_CONTINUE_SCHEMA SPAWN_LIST_SCHEMA SPAWN_RELEASE_SCHEMA SPAWN_RUN_SCHEMA
    SPAWN_RUN_TASK_ITEM_SCHEMA SPAWN_STATUS_SCHEMA SPAWN_STEER_SCHEMA SPAWN_SUB_AGENTS_SCHEMA
    SUGGEST_FOLLOWUP_SCHEMA TASK_RUN_SCHEMA TEMPLATE_NAME_HINT TEMPLATE_NAME_RE
    THEME_CONSENT_SHA_RE ToolSchema
    UPDATE_MESSAGE_SCHEMA USER_ID_RE USER_MAX_LEN ValidationError WAIT_SCHEMA WORK_DISPATCH_SCHEMA
    WORKFLOW_AUTHOR_SCHEMA WORKFLOW_LIBRARY_LIST_SCHEMA WORKFLOW_RERUN_SCHEMA
    WORKFLOW_RUN_ID_SCHEMA WORKFLOW_RUN_SCHEMA WORKSPACE_NAME_RE WORK_BRIEF_SCHEMA
    WORK_LEDGER_READ_SCHEMA WORK_LEDGER_REBUILD_SCHEMA WORK_LEDGER_RECORD_SCHEMA
    WORK_REPORT_SCHEMA _ABSOLUTE_PATH_RE _AGENT_NAME_RE _ALLOWED_CONTROL _ALLOWED_FORMAT
    _ANNOTATION_SCHEMA_KEYWORDS _ARTIFACT_COMMENT_ID_RE _ARTIFACT_FOLDER_NAME_MAX
    _ARTIFACT_FOLDER_REF_MAX _ARTIFACT_KIND_RE _ARTIFACT_SCOPE_RE _ARTIFACT_SLUG_RE
    _ARTIFACT_SOURCE_RE _ASK_ANSWERS_HEADER _ASK_ANSWER_LINE_OVERHEAD _ASK_MAX_ANSWER_LEN
    _ASK_MAX_DESC_LEN _ASK_MAX_HEADER_LEN _ASK_MAX_LABEL_LEN _ASK_MAX_OPTIONS _ASK_MAX_QUESTIONS
    _ASK_MAX_QUESTION_LEN _ASK_PAIR_SEPARATOR _BACKEND_NAME_RE _CHANNEL_TARGET_INCOMPATIBLE
    _CHAT_TAG_MAX_ITEMS _CHAT_TAG_NAME_MAX _CHAT_TAG_REF_MAX _CLAMP_NOTE _CLAMP_NOTE_RE
    _CREW_LOG_UNIT_MAX _CU_APP_FIELD _CU_AX_ACTION_RE _CU_CLICK_METHODS _CU_DRAG_PATHS
    _CU_ELEMENT_FIELD _CU_KEY_SPEC_RE _CU_LAUNCH_APP_FIELD _CU_MOUSE_BUTTONS
    _CU_OPTIONAL_ELEMENT_FIELD _CU_SCROLL_DIRECTIONS _DEBUG_MAX_ARG_CHARS
    _DEBUG_MAX_SAMPLE_SECONDS _DEBUG_PROCESS_FORMATS _DEBUG_THREAD_MODES
    _DESIGN_TWEAK_ALLOWED_STATUSES _DESIGN_TWEAK_MAX_TEXT _EMOJI_NAME_RE _FS_PATH_PATTERN
    _GIT_RESERVED_REFS _GIT_SHA_RE _GUIDE_ID_PATTERN _HIDDEN_CATEGORIES _ISO_DATE_RE
    _ISSUE_RADAR_CREW_CLEARABLE_FIELDS _ISSUE_RADAR_CREW_EVENT_KINDS _ISSUE_RADAR_CREW_PHASES
    _ISSUE_RADAR_CREW_SKIP_SCOPES _ISSUE_RADAR_ITEM_KINDS _ISSUE_RADAR_MAX_ITEM_NUMBER
    _ISSUE_RADAR_PROVIDERS _ISSUE_RADAR_STATUSES _JOB_ID_RE _JSON_SCHEMA_TYPES _MAX_EXACT_INT
    _MCP_ARGS_MAX_DEPTH _MCP_ARGS_MAX_STRING _MODEL_NAME_RE _MODEL_SEGMENT _MONITOR_WATCH_FIELD
    _OMC_API_METHODS _OMC_API_PATHS _OMC_MAX_BODY _OMC_QUERY_RE _PATTERN_CHILD_SRC
    _PATTERN_MAX_LEN _PATTERN_TIMEOUT_SECS _PATTERN_VALUE_MAX_LEN _POD_WORKTREE_MAX_LEN
    _QUALIFIED_MODEL_MAX_CHARS _SEND_MESSAGE_SESSION_RE _SLACK_TS_RE _SUPPORTED_SCHEMA_KEYWORDS
    _TARGET_ID_RE _WATCH_WORK_LEDGER _WF_RUN_ID_RE _WM_LIFECYCLE_STATUSES _WM_LIST_CAP
    _WM_PROFILE_RE _WM_SLUG_RE _WM_URL_RE _WORK_ITEM_STATES _WORK_RECORD_ACTIONS _WORK_STATUSES
    _WORK_VERDICTS _bounded_pattern_search _coerce_number_to_string_for_string_field
    _cu_coord_field _drop_hidden_except_marks _json_equal _json_type_ok
    _reject_malformed_keyword_shapes _reject_null_enabled _reject_unsupported_keywords
    _trim_edges _validate_artifact_save _validate_artifact_tags _validate_channel_routing
    _validate_chat_tag _validate_crew_record_couples_phase_to_an_event
    _validate_cron_add_requires_message_or_script _validate_cron_secret_request
    _validate_dashboard_write_tool _validate_followup_items _validate_hook_create
    _validate_hook_has_action _validate_hook_regex _validate_hook_update
    _validate_monitor_runtime _validate_omc_api _validate_session_set_model
    _validate_set_project _validate_webapp_metadata_shape _validate_work_artifacts
    _validate_work_report bounded_session_id build_tool_response clamp_query_offset clamp_report
    clamp_to_max_len coerce_mcp_tool_args coerce_mcp_tool_args_json_schema format_ask_answers
    infer_use_case is_registered_agent_name is_valid_followup_branch judge_is_off
    normalize_lesson_category normalize_theme_consent_sha normalize_unicode sanitize_json_values
    sanitize_response sanitize_string strip_hidden_unicode validate_api_body
    validate_ask_user_question validate_field validate_jsonrpc_request validate_jsonrpc_response
    validate_judge_spec validate_mcp_tool_arguments validate_string_field validate_tool_args
    """.split())

#: Every name the one-module file's import statements bound. They stay bound on the
#: facade because callers read them as its attributes.
_BASE_IMPORTS = frozenset("""
    ARTIFACT_MAX_CONTENT_BYTES ARTIFACT_TAG_MAX AWS_PROFILE_NAME_RE Any
    CHANNEL_OWNER_DM_NAMESPACES EFFORT_VALUES Iterable LESSON_APPLIES_VALUES MAX_BANNER_CHARS
    MAX_MONITOR_AGENT_TURNS MAX_MONITOR_CADENCE_SECS MAX_MONITOR_PROVIDER_ERRORS
    MAX_MONITOR_STOP_REASON_CHARS MAX_MONITOR_TOKENS MAX_MONITOR_WAKE_INSTRUCTIONS_CHARS
    MAX_RUNTIME_CEILING_SECS MAX_SHORT_STRING MAX_TOOL_RESULT_CHARS MIN_MONITOR_CADENCE_SECS
    PureWindowsPath SCOPE_FRAGMENT_RE SLACK_NAMESPACE SUBAGENT_MAX_TURNS_CEILING
    WAIT_TOOL_MAX_SECS WINDOWS_DEVICE_STEMS WORK_BLOCKED_REASONS WORK_ITEM_STATES
    WORK_REASON_STATUSES WORK_VERDICTS WORK_WORKER_STATUSES _ARTIFACT_KINDS _cu_types _goals
    _normalize_artifact_tag dataclass field json logging publicly_armable_kinds
    publicly_armable_objectives re subprocess sys unicodedata validate_runtime_secs
    """.split())

#: The names the facade's own glue imports: the owner loop, the mirror, its class swap.
_GLUE_IMPORTS = frozenset({"TYPE_CHECKING", "_facade_mirror", "_importlib", "_sys"})


def _index() -> _facade_mirror.MirrorIndex:
    index = vars(validation)[_facade_mirror.INDEX_ATTRIBUTE]
    assert isinstance(index, _facade_mirror.MirrorIndex)
    return index


def _tree(module_name: str) -> ast.Module:
    path = Path(importlib.import_module(module_name).__file__)
    return ast.parse(path.read_text(encoding="utf-8"))


def _owner_names() -> dict[str, set[str]]:
    """Owner -> the names it defines at its top level."""
    return {owner: _facade_mirror._defined_names(_tree(owner)) for owner in validation._OWNERS}


def _is_type_checking(node: ast.stmt) -> bool:
    return isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING"


def unexpected_imports(source: str, owner_names: dict[str, set[str]]) -> list[str]:
    """Names *source* binds by a run-time import beyond the frozen inventory.

    The inventory is the BASE imports, the glue's own, and from each owner module only
    names that owner defines. Imports under ``if TYPE_CHECKING:`` bind nothing at run
    time and are the mirror guard's business, so they are skipped.
    """
    found: list[str] = []
    for node in ast.parse(source).body:
        if _is_type_checking(node):
            continue
        if isinstance(node, ast.Import):
            bound = [(alias.asname or alias.name.split(".")[0], None) for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module != "__future__":
            bound = [(alias.asname or alias.name, node.module) for alias in node.names]
        else:
            continue
        for name, module in bound:
            if module in owner_names:
                if name not in owner_names[module]:
                    found.append(f"{module}.{name}")
            elif name not in _BASE_IMPORTS | _GLUE_IMPORTS:
                found.append(name)
    return found


def test_every_base_name_resolves_as_the_object_its_one_owner_defines() -> None:
    owners = _owner_names()
    homes: dict[str, list[str]] = {}
    for owner, names in owners.items():
        for name in names:
            homes.setdefault(name, []).append(owner)
    assert {name for name in _BASE_DEFINED if len(homes.get(name, [])) != 1} == set()
    assert set(homes) == _BASE_DEFINED, "an owner defines a name the one module did not"
    listed = set(dir(validation))
    for name in sorted(_BASE_DEFINED):
        (owner,) = homes[name]
        assert getattr(validation, name) is getattr(sys.modules[owner], name), name
        assert name in listed, name


def test_every_base_import_stays_bound_on_the_facade_as_its_home_object() -> None:
    bound = vars(validation)
    owners = [sys.modules[owner] for owner in validation._OWNERS]
    for name in sorted(_BASE_IMPORTS):
        assert name in bound, name
        holders = [vars(owner)[name] for owner in owners if name in vars(owner)]
        assert holders and all(held is bound[name] for held in holders), name


def test_the_owners_are_the_package_modules_lowest_layer_first() -> None:
    package = {f"{PACKAGE}.{info.name}" for info in pkgutil.iter_modules(_parts.__path__)}
    assert set(validation._OWNERS) == package
    assert _index().owners == validation._OWNERS


def test_each_owner_imports_only_owners_listed_before_it() -> None:
    owners = list(validation._OWNERS)
    late: dict[str, list[str]] = {}
    for position, owner in enumerate(owners):
        imported = {
            node.module
            for node in ast.walk(_tree(owner))
            if isinstance(node, ast.ImportFrom) and node.module
        }
        later = sorted(m for m in imported if m in owners and owners.index(m) >= position)
        if later:
            late[owner] = later
    assert late == {}


def test_importing_the_facade_loads_every_owner(tmp_path: Path) -> None:
    """Checked in a fresh interpreter: the facade imports its owners while it loads, so
    a probe that imports only ``kiro_crew.validation`` (the cycle-breaking imports in
    the member tests, the leaf check in test_agent_import_hoist) sees the whole graph."""
    env = dict(os.environ)
    env["PYTHONPATH"] = (
        str(Path(kiro_crew.__file__).resolve().parents[1]) + os.pathsep + env.get("PYTHONPATH", "")
    )
    probe = (
        "import sys, kiro_crew.validation as v; "
        "print(sorted(o for o in v._OWNERS if o not in sys.modules))"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        check=True,
        env=env,
        cwd=tmp_path,
        timeout=60,
        **UTF8_TEXT,
    )
    assert out.stdout.splitlines()[-1] == "[]", out.stderr


def test_the_facade_imports_nothing_beyond_its_frozen_inventory() -> None:
    source = _FACADE_PATH.read_text(encoding="utf-8")
    assert unexpected_imports(source, _owner_names()) == []


def test_the_inventory_check_reports_a_planted_import() -> None:
    """The F401 stand-in must be able to fail: a stray import, and an owner import of a
    name that owner does not define, are both reported."""
    source = _FACADE_PATH.read_text(encoding="utf-8")
    planted = (
        source
        + "\nimport tempfile\nfrom kiro_crew.validation_parts.input_bounds import FieldSpec\n"
    )
    assert unexpected_imports(planted, _owner_names()) == [
        "tempfile",
        "kiro_crew.validation_parts.input_bounds.FieldSpec",
    ]


@pytest.mark.parametrize("name", ["ValidationError", "JsonRpcEnvelopeError"])
def test_an_exception_keeps_the_facade_dotted_name(name: str) -> None:
    """A traceback, a log line and a channel reply render a raised class as
    ``module.qualname``; both exceptions still name the module callers import them from."""
    cls = getattr(validation, name)
    assert cls.__module__ == FACADE
    if name == "ValidationError":
        exc = cls("field", "message")
    else:
        exc = cls("field", "message", req_id=1, method="m", invalid_params=False)
    text = "".join(traceback.format_exception_only(type(exc), exc))
    assert text.startswith(f"{FACADE}.{name}: field: message")


@pytest.mark.parametrize("name", ["FieldSpec", "ToolSchema", "McpTextContent"])
def test_a_data_class_keeps_its_owner_module(name: str) -> None:
    """Nothing reads these classes' module; they keep their owner's, so annotation
    lookups resolve against the namespace that defines them."""
    cls = getattr(validation, name)
    assert cls.__module__.startswith(PACKAGE + ".")
    assert getattr(sys.modules[cls.__module__], name) is cls


def test_a_truncated_response_warns_under_the_facade_logger(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger=FACADE):
        assert validation.sanitize_response("abcdefgh", max_len=3).startswith("abc")
    assert [record.name for record in caplog.records] == [FACADE]


@pytest.mark.parametrize(
    ("branch", "valid"),
    [
        ("feat/x", True),
        ("a/b/c", True),
        ("feat/x.lock", False),
        ("feat/x.", False),
        ("feat/CON", False),
        ("feat/con.txt", False),
        ("feat/ok/aux", False),
    ],
)
def test_a_git_ref_is_checked_component_by_component(branch: str, valid: bool) -> None:
    """``is_valid_followup_branch`` splits a ref into its ``/`` components, a ref and not
    a filesystem path, and refuses any component git or Windows cannot hold."""
    assert validation.is_valid_followup_branch(branch) is valid
