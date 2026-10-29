"""``DashboardState`` keeps its BASE surface while its methods live in ``state_parts``.

``DashboardState`` was one class in ``dashboard/state.py``. Its shell now lives in
``state_parts/dashboard_state.py`` and binds methods from the collaborators named in the
``state_parts`` package docstring. These tests freeze what the move must not change,
measured on the one-module file: every member keeps its kind, coroutine-ness and
signature, and each bound method IS its collaborator's function, reads as a method of
the class and does not depend on the class body it left. The checks' self-tests live
with the shared helpers in ``test_state_parts_facade.py``.
"""

from __future__ import annotations

import inspect
import typing

from test_state_parts_facade import (
    _bound_functions,
    _class_body_dependencies,
    _member_table,
    _owner_modules,
)

from kiro_crew.dashboard.state import DashboardState

#: ``DashboardState`` members before the split: name -> (kind, coroutine, async
#: generator, ``inspect.signature``).
_BASE_STATE_MEMBERS: dict[str, tuple[str, bool, bool, str]] = {
    "__init__": (
        "function",
        False,
        False,
        "(self, sessions: 'SessionManager', crons: 'CronService', lessons: "
        "'LessonStore', start_time: 'float', subagents: 'SubagentManager | None' = None, "
        "context_builder: 'ContextBuilder | None' = None, conversation_log: "
        "'ConversationLog | None' = None, consolidator: 'HistoryConsolidator | None' = "
        "None, task_runner: 'TaskRunner | None' = None, slack_client: 'Any' = None, "
        "owner_id: 'str' = '')",
    ),
    "_atomic_write_json": ("staticmethod", False, False, "(path: 'Path', data: 'Any') -> 'None'"),
    "_atomic_write_json_strict": (
        "staticmethod",
        False,
        False,
        "(path: 'Path', data: 'Any') -> 'None'",
    ),
    "_audit_and_broadcast_approval": (
        "function",
        False,
        False,
        "(self, session_key: 'str', approval_id: 'str', approved: "
        "'bool', decision: 'str' = '') -> 'None'",
    ),
    "_audit_approval": (
        "function",
        False,
        False,
        "(self, session_key: 'str', approval_id: 'str', approved: 'bool', "
        "decision: 'str' = '') -> 'None'",
    ),
    "_background_turn_sema": ("function", False, False, "(self) -> 'asyncio.Semaphore'"),
    "_broadcast": ("function", False, False, "(self, note: 'dict[str, Any]') -> 'None'"),
    "_broadcast_chat_message": (
        "function",
        False,
        False,
        "(self, slot_key: 'str', msg: 'dict') -> 'None'",
    ),
    "_broadcast_question_retired": (
        "function",
        False,
        False,
        "(self, slot_key: 'str', cards: 'list[str] | list[tuple[str, "
        "bool]]', *, reason: 'str | None' = None) -> 'None'",
    ),
    "_channel_link_is_live": ("function", False, False, "(self, link: 'ChannelLink') -> 'bool'"),
    "_complete_slot_list_rebuild_if_ready": (
        "function",
        False,
        False,
        "(self, *, deadline_reached: 'bool' = False) -> 'bool'",
    ),
    "_count_lessons": ("function", False, False, "(self) -> 'int | None'"),
    "_deferred_slots_flush": (
        "function",
        False,
        False,
        "(self, operation: 'str', retries_remaining: 'int') -> 'None'",
    ),
    "_deliver_note": ("function", False, False, "(self, note: 'dict[str, Any]') -> 'None'"),
    "_do_slots_broadcast": ("function", False, False, "(self) -> 'None'"),
    "_drop_if_stalled": ("function", False, False, "(self, ws: 'web.WebSocketResponse') -> 'bool'"),
    "_drop_orphaned_mcp_report": (
        "function",
        False,
        False,
        "(self, slot: \"'_ChatSlot'\") -> 'None'",
    ),
    "_emit_member_slot_transitions": ("function", False, False, "(self) -> 'None'"),
    "_expire_slot_list_rebuild": ("function", False, False, "(self) -> 'None'"),
    "_fan_out_web_push": ("function", False, False, "(self, note: 'dict[str, Any]') -> 'None'"),
    "_flush_dirty_slots": ("function", False, False, "(self) -> 'None'"),
    "_flush_loop": ("function", True, False, "(self) -> 'None'"),
    "_has_legacy_slots_audience": ("function", False, False, "(self) -> 'bool'"),
    "_has_slot_patch_clients": ("function", False, False, "(self) -> 'bool'"),
    "_lineage_rows": ("function", False, False, "(self) -> 'list[dict[str, Any]]'"),
    "_link_slack_persisted": (
        "function",
        False,
        False,
        "(self, slot: 'Any', slot_name: 'str', thread_ts: 'str', " "channel_id: 'str') -> 'None'",
    ),
    "_notify_channel_compaction": (
        "function",
        True,
        False,
        "(self, key: 'str', pct: 'float', *, success: 'bool', outcome: "
        "'str' = 'compacted') -> 'None'",
    ),
    "_notify_inbound_unbind": (
        "function",
        True,
        False,
        "(self, key: 'str', link: 'ChannelLink', reason: 'str') -> 'None'",
    ),
    "_on_unbind_notice_done": (
        "function",
        False,
        False,
        "(self, task: \"'asyncio.Task'\") -> 'None'",
    ),
    "_on_ws_send_done": ("function", False, False, "(self, task: 'asyncio.Task') -> 'None'"),
    "_partition_preserving": (
        "staticmethod",
        False,
        False,
        "(raw: 'list[Any]', predicate: 'Callable[[Any], bool]', noun: "
        "'str', source_file: 'str') -> 'tuple[list[Any], list[Any]]'",
    ),
    "_persist_context_snapshots": ("function", False, False, "(self) -> 'None'"),
    "_persist_cron_folders": (
        "function",
        False,
        False,
        "(self, folders: 'list[dict[str, Any]]') -> 'None'",
    ),
    "_persist_open_slots": ("function", False, False, "(self) -> 'None'"),
    "_persistent_slot_list_hold": ("function", False, False, "(self) -> 'str | None'"),
    "_prune_source_activity": (
        "function",
        False,
        False,
        "(self, now: 'float') -> 'dict[str, float]'",
    ),
    "_push_slots": ("function", False, False, "(self) -> 'None'"),
    "_record_member_row": (
        "function",
        False,
        False,
        "(self, slot_key: 'str', msg: 'dict') -> 'None'",
    ),
    "_redact_questions": (
        "function",
        False,
        False,
        "(self, questions: 'list[dict]') -> 'list[dict]'",
    ),
    "_remove_ws": ("function", False, False, "(self, ws: 'web.WebSocketResponse') -> 'None'"),
    "_reproject_slots": (
        "function",
        False,
        False,
        "(self, slots_data: 'list[dict]', *, include_check_status: 'bool' = "
        "False, dashboard_user: 'bool' = False) -> 'list[dict]'",
    ),
    "_rewrite_notifications_async": ("function", True, False, "(self) -> 'None'"),
    "_running_loop": ("staticmethod", False, False, "() -> 'asyncio.AbstractEventLoop | None'"),
    "_schedule_trailing_flush": ("function", False, False, "(self, delay: 'float') -> 'None'"),
    "_send_slot_patch": ("function", False, False, "(self, data: 'dict[str, Any]') -> 'None'"),
    "_send_web_push_all": ("function", True, False, "(self, note: 'dict[str, Any]') -> 'None'"),
    "_send_ws_all": (
        "function",
        False,
        False,
        "(self, msg_type: 'str', data: 'object', msg: 'str') -> 'None'",
    ),
    "_send_ws_owners": (
        "function",
        False,
        False,
        "(self, msg: 'str', *, skip_slot_patch_clients: 'bool' = False) -> " "'None'",
    ),
    "_serialize_for_client": (
        "function",
        False,
        False,
        "(self, ws: 'web.WebSocketResponse', msg_type: 'str', data: "
        "'object', default_msg: 'str') -> 'str'",
    ),
    "_serialize_subagent_batch": (
        "function",
        False,
        False,
        "(self, ws: 'web.WebSocketResponse', msg_type: 'str', data: "
        "'object', default_msg: 'str') -> 'str'",
    ),
    "_slot_chip_urls": ("staticmethod", False, False, "(slot: '_ChatSlot') -> 'list[str]'"),
    "_slot_links": (
        "function",
        False,
        False,
        "(self, slot: '_ChatSlot') -> 'tuple[list[dict[str, Any]], bool, str, str]'",
    ),
    "_spawn_unbind_notice": (
        "function",
        False,
        False,
        "(self, key: 'str', link: 'ChannelLink', reason: 'str') -> 'None'",
    ),
    "_spawn_ws_send": (
        "function",
        False,
        False,
        "(self, ws: 'web.WebSocketResponse', msg: 'str') -> 'None'",
    ),
    "_take_slots_audience": ("function", False, False, "(self) -> 'bool'"),
    "_trailing_slots_flush": ("function", False, False, "(self) -> 'None'"),
    "_unbind_notice_title": ("function", False, False, "(self, key: 'str') -> 'str'"),
    "_write_folders_confirmed": (
        "function",
        False,
        False,
        "(self, path: 'Path', snapshot: 'list[dict[str, Any]]') -> " "'None'",
    ),
    "_ws_client_allowed": (
        "function",
        False,
        False,
        "(self, ws: 'web.WebSocketResponse', msg_type: 'str', data: 'object') " "-> 'bool'",
    ),
    "_yolo": ("property", False, False, "(self) -> 'bool'"),
    "ack_notification": ("function", True, False, "(self, ts: 'str') -> 'bool'"),
    "add_fold_watch_listener": (
        "function",
        False,
        False,
        "(self, listener: 'Callable[[], None]') -> 'Callable[[], None]'",
    ),
    "adopt_pinned_session_order": (
        "function",
        False,
        False,
        "(self, read: 'tuple[list[str], bool] | None') -> 'bool'",
    ),
    "approval_timeout_for": ("function", False, False, "(self, slot: \"'_ChatSlot'\") -> 'float'"),
    "arm_slot_list_rebuild_deadline": ("function", False, False, "(self) -> 'None'"),
    "background_turn_stats": ("function", False, False, "(self) -> 'dict[str, int]'"),
    "begin_slot_construction": ("function", False, False, "(self, key: 'str') -> 'None'"),
    "begin_slot_list_rebuild": ("function", False, False, "(self) -> 'None'"),
    "bind_serving_loop": (
        "function",
        False,
        False,
        "(self, loop: 'asyncio.AbstractEventLoop') -> 'None'",
    ),
    "broadcast_browser_event": (
        "function",
        False,
        False,
        "(self, event_type: 'str', data: 'dict') -> 'None'",
    ),
    "broadcast_context_usage": (
        "function",
        False,
        False,
        "(self, slot_key: 'str', payload: 'dict') -> 'None'",
    ),
    "broadcast_slot_fold": (
        "function",
        False,
        False,
        "(self, slot: 'str', legacy: 'WsPayload', delta: 'WsPayload | None') -> 'None'",
    ),
    "broadcast_ws": (
        "function",
        False,
        False,
        "(self, msg_type: 'str', data: 'WsPayload') -> 'None'",
    ),
    "broadcast_ws_owners": (
        "function",
        False,
        False,
        "(self, msg_type: 'str', data: 'WsPayload') -> 'None'",
    ),
    "broadcast_ws_subagent_subscribers": (
        "function",
        False,
        False,
        "(self, msg_type: 'str', data: 'WsPayload') -> 'None'",
    ),
    "bump_pinned_order_rev": ("function", False, False, "(self) -> 'None'"),
    "bump_tags_generation": ("function", False, False, "(self) -> 'None'"),
    "cancel_questions_for_slot": ("function", False, False, "(self, slot_key: 'str') -> 'int'"),
    "channel_status": ("function", False, False, "(self) -> 'dict[str, dict[str, Any]]'"),
    "clear_notifications": ("function", True, False, "(self) -> 'None'"),
    "clear_question_pending": (
        "function",
        False,
        False,
        "(self, slot_key: 'str', *, blocking: 'bool | None' = None, "
        "card_id: 'str | None' = None, reason: 'str | None' = None) -> "
        "'bool'",
    ),
    "clear_update_progress": ("function", False, False, "(self) -> 'None'"),
    "close_all_ws": ("function", True, False, "(self) -> 'None'"),
    "context_snapshot_for": ("function", False, False, "(self, slot_key: 'str') -> 'dict | None'"),
    "create_cron_folder": (
        "function",
        False,
        False,
        "(self, name: 'str', folder_id: 'str') -> 'dict'",
    ),
    "creator_slot_count": ("function", False, False, "(self, creator_key: 'str') -> 'int'"),
    "dashboard_user_ws_count": ("function", False, False, "(self) -> 'int'"),
    "delete_cron_folder": ("function", False, False, "(self, folder_id: 'str') -> 'bool'"),
    "delete_notification": ("function", True, False, "(self, ts: 'str') -> 'bool'"),
    "deliver_ws_owners": (
        "function",
        True,
        False,
        "(self, msg_type: 'str', data: 'WsPayload') -> 'int'",
    ),
    "disable_yolo": ("function", False, False, "(self) -> 'None'"),
    "effective_max_background_turns": ("function", False, False, "(self) -> 'int'"),
    "enable_yolo": ("function", False, False, "(self, *, from_config: 'bool' = False) -> 'None'"),
    "end_slot_construction": ("function", False, False, "(self, key: 'str') -> 'None'"),
    "ensure_context_snapshots_loaded": ("function", False, False, "(self) -> 'bool'"),
    "ensure_dynamic_card_store": ("function", False, False, "(self) -> 'Any'"),
    "finish_slot_list_rebuild": ("function", False, False, "(self) -> 'None'"),
    "flush_slot_now": ("function", False, False, "(self, slot: '_ChatSlot') -> 'None'"),
    "fold_apply_watch": ("function", False, False, "(self) -> 'frozenset[str]'"),
    "fold_watch_routing": (
        "function",
        False,
        False,
        "(self) -> 'tuple[bool, bool, frozenset[str]]'",
    ),
    "folder_breadcrumb": (
        "function",
        False,
        False,
        "(self, folder_id: 'str', sep: 'str' = ' › ') -> 'str'",
    ),
    "folders_generation": ("function", False, False, "(self) -> 'int'"),
    "get_channel_transport": (
        "function",
        False,
        False,
        "(self, channel_type: 'str') -> \"'MessagingTransport | None'\"",
    ),
    "get_linked_slot": (
        "function",
        False,
        False,
        "(self, session_key: 'str') -> \"'_ChatSlot | None'\"",
    ),
    "get_or_create_slot": (
        "function",
        False,
        False,
        "(self, name: 'str | None' = None, agent: 'str' = '', workspace: 'str' "
        "= 'default', model: 'str' = '', mode: 'str' = '', memory_mode: 'str | "
        "None' = None, ephemeral: 'bool | None' = None, app: 'str' = '', "
        "linked_session_key: 'str' = '', channel_origin: 'bool' = False, "
        "origin: 'str | None' = None, *, count_user_session: 'bool' = False) "
        "-> '_ChatSlot'",
    ),
    "get_slot": ("function", False, False, "(self, name: 'str') -> '_ChatSlot | None'"),
    "has_slot": ("function", False, False, "(self, name: 'str') -> 'bool'"),
    "hold_folders": (
        "function",
        True,
        False,
        "(self, section: 'Callable[[list[dict[str, Any]]], Awaitable[_T]]') -> '_T'",
    ),
    "idle_source_link_urls": ("function", False, False, "(self) -> 'set[str]'"),
    "is_yolo_active": ("function", False, False, "(self) -> 'bool'"),
    "knowledge_store": ("property", False, False, "(self)"),
    "link_slack": (
        "function",
        False,
        False,
        "(self, slot_name: 'str', thread_ts: 'str', channel_id: 'str') -> 'None'",
    ),
    "linked_session_busy": ("function", False, False, "(self, session_key: 'str') -> 'bool'"),
    "live_slot_count": ("function", False, False, "(self) -> 'int'"),
    "ensure_chat_reactions_loaded": ("function", True, False, "(self) -> 'None'"),
    "load_chat_pins": ("function", False, False, "(self) -> 'None'"),
    "load_chat_reactions": ("function", False, False, "(self) -> 'None'"),
    "load_cron_folders": ("function", False, False, "(self) -> 'None'"),
    "load_folders": ("function", False, False, "(self) -> 'None'"),
    "load_tags": ("function", False, False, "(self) -> 'None'"),
    "manual_session_order": (
        "property",
        False,
        False,
        "(self) -> 'pinned_session_order.ManualOrder'",
    ),
    "mark_question_pending": (
        "function",
        False,
        False,
        "(self, slot_key: 'str', *, blocking: 'bool', card_id: 'str', "
        "questions: 'list[dict] | None' = None, native: 'bool' = False) -> "
        "'None'",
    ),
    "mutate_folders": (
        "function",
        True,
        False,
        "(self, mutate: 'Callable[[list[dict[str, Any]]], tuple[bool, _T]]', "
        "on_committed: 'Callable[[], None] | None' = None, prepare: 'Callable[[], "
        "Awaitable[None]] | None' = None) -> '_T'",
    ),
    "native_subagent_snapshots": (
        "function",
        False,
        False,
        "(self, terminal_limit: 'int' = 50, ttl_secs: 'float' = 3600.0) "
        "-> 'list[dict[str, object]]'",
    ),
    "note_crew_log_class": ("function", False, False, "(self, slot: 'Any') -> 'None'"),
    "note_source_activity": ("function", False, False, "(self, key: 'str') -> 'None'"),
    "notify": (
        "function",
        False,
        False,
        "(self, kind: 'str', title: 'str', body: 'str', *, meta: 'dict | None' = None, "
        "url: 'str | None' = None, actions: 'list[dict[str, Any]] | None' = None, channel: "
        "'str | None' = None, member: 'dict[str, str] | None' = None, publisher_session: "
        "'str | None' = None) -> 'None'",
    ),
    "notify_dashboard_card": (
        "function",
        False,
        False,
        "(self, slot: \"'_ChatSlot'\", reason: 'str') -> 'None'",
    ),
    "open_agent_ask": (
        "function",
        True,
        False,
        "(self, ask_id: 'str', slot_key: 'str', session_key: 'str', questions: "
        "'list[dict]') -> 'int | None'",
    ),
    "pending_coordinator_approvals": (
        "function",
        False,
        False,
        "(self, slot_key: 'str') -> 'list[dict]'",
    ),
    "pin_rank": ("function", False, False, "(self, slot: 'Any') -> 'int | None'"),
    "pinned_session_flags": ("function", False, False, "(self) -> 'dict[str, bool]'"),
    "post_question_card": (
        "function",
        True,
        False,
        "(self, slot_key: 'str', questions: 'list[dict]', *, native: 'bool' = " "False) -> 'int'",
    ),
    "push_artifact_update": (
        "function",
        False,
        False,
        "(self, slug: 'str', version: 'int', *, deleted: 'bool' = False) -> " "'None'",
    ),
    "push_lineage_patch": ("function", False, False, "(self) -> 'None'"),
    "push_refresh": ("function", False, False, "(self, *kinds: 'str') -> 'None'"),
    "push_session_summary": ("function", False, False, "(self, key: 'str') -> 'None'"),
    "push_slot_patch": (
        "function",
        False,
        False,
        "(self, key: 'str', fields: 'Iterable[str]') -> 'None'",
    ),
    "push_slot_removed": ("function", False, False, "(self, key: 'str') -> 'None'"),
    "push_slot_title": (
        "function",
        False,
        False,
        "(self, key: 'str', title: 'str', *, full: 'bool' = True) -> 'None'",
    ),
    "push_slots_update": (
        "function",
        False,
        False,
        "(self, *, legacy_only: 'bool' = False) -> 'None'",
    ),
    "push_source_status": ("function", False, False, "(self, delta: 'dict') -> 'None'"),
    "push_subagent_patch": ("function", False, False, "(self) -> 'None'"),
    "push_update_progress": (
        "function",
        False,
        False,
        "(self, step: 'str', detail: 'str' = '') -> 'None'",
    ),
    "read_durable_tags_snapshot": (
        "function",
        False,
        False,
        "(self) -> '_DurableTagSnapshot | None'",
    ),
    "read_folders": (
        "function",
        True,
        False,
        "(self, read: 'Callable[[list[dict[str, Any]]], _T]') -> '_T'",
    ),
    "read_pinned_session_order": (
        "function",
        False,
        False,
        "(self) -> 'tuple[list[str], bool] | None'",
    ),
    "recent_question_resolutions": ("function", False, False, "(self) -> 'dict[str, str]'"),
    "refresh_slot_source_status": ("function", False, False, "(self, key: 'str') -> 'None'"),
    "register_channel_transport": (
        "function",
        False,
        False,
        "(self, transport: \"'MessagingTransport'\") -> 'None'",
    ),
    "register_sse": ("function", False, False, "(self) -> 'asyncio.Queue[dict[str, Any]]'"),
    "register_ws": (
        "function",
        False,
        False,
        "(self, ws: 'web.WebSocketResponse', *, owner: 'bool' = False) -> 'None'",
    ),
    "release_deleted_slot_keys": (
        "function",
        False,
        False,
        "(self, keys: 'Iterable[str]') -> 'None'",
    ),
    "remove_chat_pins_for_slots": (
        "function",
        True,
        False,
        "(self, slot_keys: 'set[str]') -> 'int'",
    ),
    "rename_cron_folder": (
        "function",
        False,
        False,
        "(self, folder_id: 'str', name: 'str') -> 'dict | None'",
    ),
    "replace_channel_slot_holds": (
        "function",
        False,
        False,
        "(self, keys: 'Iterable[str]', *, hold_channels: 'bool') -> 'bool'",
    ),
    "request_approval": (
        "function",
        True,
        False,
        "(self, approval_id: 'str', source: 'str', tool: 'str', *, tool_input: "
        "'str' = '', tool_purpose: 'str' = '', slot: 'str' = '', is_background: "
        "'bool' = False) -> 'bool'",
    ),
    "request_question": (
        "function",
        True,
        False,
        "(self, ask_id: 'str', slot_key: 'str', questions: 'list[dict]', "
        "timeout: 'int | None' = None) -> 'dict[str, str] | None'",
    ),
    "reseed_slot_counter": ("function", False, False, "(self) -> 'None'"),
    "resolve_approval": (
        "function",
        False,
        False,
        "(self, approval_id: 'str', approved: 'bool', *, rejected_once: 'bool' = "
        "False) -> 'bool'",
    ),
    "resolve_question": (
        "function",
        False,
        False,
        "(self, ask_id: 'str', answers: 'dict[str, str] | None', *, reason: 'str "
        "| None' = None, responder: 'Any' = None, response_id: 'str' = '') -> 'bool'",
    ),
    "resolve_skill_review_notifications": (
        "function",
        True,
        False,
        "(self, slug: 'str', consumed_at: 'str') -> 'int'",
    ),
    "resolve_slot": ("function", False, False, "(self, name: 'str') -> '_ChatSlot | None'"),
    "resolve_slot_approval": (
        "function",
        False,
        False,
        "(self, slot: 'Any', approval_id: 'str', approved: 'bool', *, "
        "rejected_once: 'bool' = False, expected_future: "
        "'asyncio.Future[str] | None' = None) -> 'bool'",
    ),
    "resolve_state_approval": (
        "function",
        False,
        False,
        "(self, approval_id: 'str', approved: 'bool') -> 'bool'",
    ),
    "run_background_turn": (
        "function",
        True,
        False,
        "(self, slot: \"'_ChatSlot'\", coro: 'Any') -> 'Any'",
    ),
    "running_session_keys": ("function", False, False, "(self) -> 'frozenset[str]'"),
    "save_chat_pins": ("function", False, False, "(self) -> 'None'"),
    "save_chat_reactions": (
        "function",
        False,
        False,
        "(self, records: 'list[dict] | None' = None) -> 'None'",
    ),
    "save_cron_folders": ("function", False, False, "(self) -> 'None'"),
    "save_folders": ("function", False, False, "(self) -> 'None'"),
    "save_pinned_session_order": ("function", False, False, "(self, keys: 'list[str]') -> 'None'"),
    "save_tag_boards": ("function", False, False, "(self) -> 'None'"),
    "save_tag_boards_snapshot": (
        "function",
        False,
        False,
        "(self, snapshot: 'list[dict]') -> 'None'",
    ),
    "save_tags": ("function", False, False, "(self) -> 'None'"),
    "save_tags_snapshot": ("function", False, False, "(self, snapshot: 'list[dict]') -> 'None'"),
    "send_members_subscribed": (
        "function",
        True,
        False,
        "(self, ws: 'web.WebSocketResponse') -> 'None'",
    ),
    "serialize_slot": (
        "function",
        False,
        False,
        "(self, slot: '_ChatSlot', *, include_check_status: 'bool' = False, "
        "dashboard_user: 'bool' = False) -> 'dict[str, Any]'",
    ),
    "serialize_slot_views": (
        "function",
        False,
        False,
        "(self, *, owner: 'bool') -> 'tuple[list[dict], list[dict], " "list[dict] | None]'",
    ),
    "serialize_slots": (
        "function",
        False,
        False,
        "(self, *, include_check_status: 'bool' = False, dashboard_user: 'bool' = "
        "False) -> 'list'",
    ),
    "served_bundle_id": ("staticmethod", False, False, "(index: 'Path | None' = None) -> 'str'"),
    "serving_loop": ("property", False, False, "(self) -> \"'asyncio.AbstractEventLoop | None'\""),
    "set_dynamic_cards_enabled": ("function", False, False, "(self, enabled: 'bool') -> 'None'"),
    "set_fold_apply_capable": (
        "function",
        False,
        False,
        "(self, ws: 'web.WebSocketResponse', capable: 'bool') -> 'bool'",
    ),
    "set_fold_delta_capable": (
        "function",
        False,
        False,
        "(self, ws: 'web.WebSocketResponse', capable: 'bool') -> 'bool'",
    ),
    "set_fold_watch": (
        "function",
        False,
        False,
        "(self, ws: 'web.WebSocketResponse', slots: 'object') -> 'frozenset[str]'",
    ),
    "slot_exists": ("function", False, False, "(self, name: 'str') -> 'bool'"),
    "slot_list_held_keys": ("function", False, False, "(self) -> 'list[str]'"),
    "slot_list_hold_channels": ("function", False, False, "(self) -> 'bool'"),
    "source_link_urls": ("function", False, False, "(self) -> 'list[str]'"),
    "source_link_urls_for_slot": ("function", False, False, "(self, key: 'str') -> 'list[str]'"),
    "spend_slot_by_session": ("function", False, False, "(self) -> 'dict[str, str]'"),
    "start_flush_loop": ("function", False, False, "(self) -> 'None'"),
    "status_snapshot": (
        "function",
        False,
        False,
        "(self, *, cron_jobs: 'int | None' = None, lessons: 'int | None' = None, "
        "update_available: 'bool | None' = None, update_can_apply: 'bool' = "
        "False, update_check_status: 'str' = 'unchecked', update_command: 'str' = "
        "'', update_latest_version: 'str' = '', update_latest_version_display: "
        "'str' = '', update_channel: 'str' = '', update_channel_move_pending: "
        "'bool' = False, update_managed_by: 'str' = '', update_commits_ahead: "
        "'int' = 0, update_commits_behind: 'int' = 0, update_last_checked_at: "
        "'float | None' = None, update_check_interval_secs: 'int' = 43200, "
        "update_required: 'bool' = False, update_min_version: 'str' = '', "
        "update_can_arm: 'bool' = False, update_auto_effect: 'str' = 'unknown', "
        "update_bundled_by_app: 'bool' = False, version_display: 'str' = '', "
        "bundle_id: 'str' = '') -> 'dict[str, Any]'",
    ),
    "subscribe_logs": ("function", False, False, "(self, ws: 'web.WebSocketResponse') -> 'None'"),
    "subscribe_subagents": (
        "function",
        False,
        False,
        "(self, ws: 'web.WebSocketResponse') -> 'None'",
    ),
    "suspend_slots_push": (
        "function",
        False,
        False,
        "(self) -> \"'Iterator[Callable[[str], None]]'\"",
    ),
    "tags_generation": ("function", False, False, "(self) -> 'int'"),
    "unack_notification": ("function", True, False, "(self, ts: 'str') -> 'bool'"),
    "unregister_sse": (
        "function",
        False,
        False,
        "(self, q: 'asyncio.Queue[dict[str, Any]]') -> 'None'",
    ),
    "unregister_ws": ("function", False, False, "(self, ws: 'web.WebSocketResponse') -> 'None'"),
    "unsubscribe_logs": ("function", False, False, "(self, ws: 'web.WebSocketResponse') -> 'None'"),
    "unsubscribe_subagents": (
        "function",
        False,
        False,
        "(self, ws: 'web.WebSocketResponse') -> 'None'",
    ),
    "wait_agent_ask": (
        "function",
        True,
        False,
        "(self, ask_id: 'str', session_key: 'str', slice_secs: 'float') -> 'dict | " "None'",
    ),
    "wire_session_bind_listener": ("function", False, False, "(self) -> 'None'"),
    "wire_session_compact_callback": ("function", False, False, "(self) -> 'None'"),
    "wire_session_recycle_callback": ("function", False, False, "(self) -> 'None'"),
    "wire_session_release_listener": ("function", False, False, "(self) -> 'None'"),
    "wire_session_unbind_listener": ("function", False, False, "(self) -> 'None'"),
    "withdraw_agent_ask": (
        "function",
        False,
        False,
        "(self, ask_id: 'str', session_key: 'str') -> 'bool'",
    ),
    "ws_client_count": ("function", False, False, "(self) -> 'int'"),
}

#: ``typing.get_type_hints`` raised on these members of the one-module class too.
_STATE_HINTS_UNRESOLVED_AT_BASE = frozenset(
    {"__init__", "get_channel_transport", "register_channel_transport"}
)
#: Members whose hints name ``_ChatSlot``, which their collaborator imports only under
#: ``TYPE_CHECKING``: a hint resolves in the collaborator's globals, not the class's module.
_STATE_HINTS_UNRESOLVED_BY_THE_SPLIT = frozenset(
    {
        "_drop_orphaned_mcp_report",
        "_slot_chip_urls",
        "_slot_links",
        "approval_timeout_for",
        "flush_slot_now",
        "notify_dashboard_card",
        "run_background_turn",
        "serialize_slot",
    }
)


def test_the_state_keeps_every_base_member_with_its_kind_and_signature() -> None:
    assert _member_table(DashboardState) == _BASE_STATE_MEMBERS


def test_each_bound_state_member_is_its_collaborators_function() -> None:
    bound = _bound_functions(DashboardState)
    assert len(bound) > 150, "almost nothing is bound -- the shell drifted"
    owners = {m.__name__: m for m in _owner_modules().values()}
    for name, fn in bound.items():
        assert inspect.unwrap(vars(owners[fn.__module__])[name]) is inspect.unwrap(fn), name


def test_a_bound_state_member_reads_as_a_method_of_the_class() -> None:
    for name, fn in _bound_functions(DashboardState).items():
        assert fn.__qualname__ == f"DashboardState.{name}", name
        assert "self" not in fn.__annotations__, name


def test_no_bound_state_member_depends_on_the_class_body_it_left() -> None:
    offenders = {
        name: deps
        for name, fn in _bound_functions(DashboardState).items()
        if (deps := _class_body_dependencies(inspect.getsource(inspect.unwrap(fn))))
    }
    assert offenders == {}


def test_state_type_hints_resolve_as_before_except_the_named_members() -> None:
    unresolved = set()
    for name, raw in vars(DashboardState).items():
        fn = raw.fget if isinstance(raw, property) else getattr(raw, "__func__", raw)
        if not inspect.isfunction(fn):
            continue
        try:
            typing.get_type_hints(fn)
        except NameError:
            unresolved.add(name)
    assert unresolved == _STATE_HINTS_UNRESOLVED_AT_BASE | _STATE_HINTS_UNRESOLVED_BY_THE_SPLIT
