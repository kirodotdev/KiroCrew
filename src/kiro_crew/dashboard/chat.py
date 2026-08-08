"""Dashboard chat — facade module.

Re-exports all public symbols from the extracted chat_*.py submodules
so that existing imports (``from kiro_crew.dashboard.chat import X``)
continue to work without changes.

The actual implementation lives in:
- chat_utils.py       — shared helpers, redaction, model normalization
- chat_persistence.py — session save/restore, history
- chat_runner.py      — _run_chat, streaming, prompt expansion
- chat_handlers.py    — HTTP API endpoints
- chat_orchestrator.py — stage loop, plan actions
- chat_title.py       — title generation, plan metadata
- chat_regenerate.py  — regenerate, variant switch, edit-resend
- chat_folders.py     — folder CRUD, pin, assignment
- chat_voice.py       — TTS config + synthesis (optional)
- chat_slack.py       — Slack link, handoff, channels
- chat_fork.py        — fork session
"""

from __future__ import annotations

# Re-export names that tests monkeypatch on this module
import asyncio  # noqa: F401

# Re-export names that tests monkeypatch on this module
from kiro_crew.config.loader import (  # noqa: F401
    KiroCrewConfig,
    _workspace_name_for_dir,
    config_dir,
    resolve_agent_bindings,
)
from kiro_crew.dashboard.chat_folders import (  # noqa: F401
    api_chat_folder_create,
    api_chat_folder_delete,
    api_chat_folder_update,
    api_chat_folders,
    api_chat_slot_folder,
    api_chat_slot_mode,
    api_chat_slot_pin,
)
from kiro_crew.dashboard.chat_fork import api_chat_slot_fork  # noqa: F401
from kiro_crew.dashboard.chat_handlers import (  # noqa: F401
    MAX_COLOR_INDEX,
    api_chat,
    api_chat_mode,
    api_chat_slot_agent,
    api_chat_slot_approve,
    api_chat_slot_color,
    api_chat_slot_context,
    api_chat_slot_continue,
    api_chat_slot_create,
    api_chat_slot_delete,
    api_chat_slot_detail,
    api_chat_slot_followup,
    api_chat_slot_interrupt,
    api_chat_slot_model,
    api_chat_slot_project,
    api_chat_slot_queue_cancel,
    api_chat_slot_queue_edit,
    api_chat_slot_queue_reorder,
    api_chat_slot_reasoning_effort,
    api_chat_slot_resume,
    api_chat_slot_stop,
    api_chat_slot_workspace,
    api_chat_slots,
    api_chat_slots_cleanup,
    api_chat_slots_model,
    api_recent_projects,
)
from kiro_crew.dashboard.chat_mirror import (  # noqa: F401
    api_channel_targets,
    api_chat_slot_mirror_link,
    api_chat_slot_mirror_pause,
    api_chat_slot_mirror_unlink,
)
from kiro_crew.dashboard.chat_nav import (  # noqa: F401
    api_chat_nav_resolve_links,
)
from kiro_crew.dashboard.chat_orchestrator import (  # noqa: F401
    _build_stage_context,
    _capture_stage_result,
    _previous_result_paths,
    _stage_loop,
    api_chat_plan_action,
)
from kiro_crew.dashboard.chat_persistence import (  # noqa: F401
    _attach_variants,
    _build_history_prefix,
    _rehydrate_slot_from_history,
    _save_slot_to_history,
    restore_open_slots,
    restore_open_slots_async,
    restore_recent_sessions,
    restore_recent_sessions_async,
    save_all_slots_to_history,
)
from kiro_crew.dashboard.chat_regenerate import (  # noqa: F401
    _MAX_VARIANTS,
    api_chat_slot_edit_resend,
    api_chat_slot_regenerate,
    api_chat_slot_switch_variant,
)
from kiro_crew.dashboard.chat_rewind import api_chat_slot_rewind  # noqa: F401
from kiro_crew.dashboard.chat_runner import (  # noqa: F401
    _expand_prompt_mention,
    _flush_segment,
    _run_chat,
)
from kiro_crew.dashboard.chat_slack import (  # noqa: F401
    api_chat_slot_handoff,
    api_chat_slot_slack_link,
    api_chat_slot_slack_pause,
    api_chat_slot_slack_unlink,
    api_handoff_channels,
    api_slack_channels,
)
from kiro_crew.dashboard.chat_tags import (  # noqa: F401
    api_chat_slot_drop,
    api_chat_slot_tags,
    api_chat_tag_column_create,
    api_chat_tag_column_delete,
    api_chat_tag_column_update,
    api_chat_tag_columns,
    api_chat_tag_columns_reorder,
    api_chat_tag_create,
    api_chat_tag_delete,
    api_chat_tag_update,
    api_chat_tags,
)
from kiro_crew.dashboard.chat_title import (  # noqa: F401
    _build_title_prompt,
    _extract_and_redact_plan_metadata,
    _generate_title_via_kiro,
    _maybe_auto_title,
    _persist_title,
    _rephrase_plan_lite,
    _reset_auto_run_for_new_plan,
    api_chat_slot_generate_title,
    api_chat_slot_rename,
)
from kiro_crew.dashboard.chat_utils import (  # noqa: F401
    _BLOCKED_SLASH_COMMANDS,
    _SLASH_COMMANDS,
    _apply_incognito_prefix,
    _broadcast_auto_tool,
    _broadcast_compaction_result,
    _build_stream_chunk,
    _dequeue_next_message,
    _emit_agent_assignment,
    _extract_bash_command,
    _history_key_for,
    _maybe_consolidate,
    _maybe_inject_persona,
    _normalize_model,
    _prepare_messages,
    _redact_deep,
    _redact_for_display,
    _remove_queued_by_id,
    _sync_dashboard_slots,
    _validate_tool_name,
    is_deprecated_model,
)
from kiro_crew.dashboard.chat_voice import (  # noqa: F401
    api_voice_config,
    api_voice_synthesize,
    api_voice_voices,
)
from kiro_crew.security import is_sensitive_path  # noqa: F401
from kiro_crew.sel import sel  # noqa: F401
