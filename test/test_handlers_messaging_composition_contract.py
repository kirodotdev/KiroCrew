"""The dashboard messaging handlers keep their surface and contracts while their owners move.

``kiro_crew.dashboard.handlers.messaging`` is the messaging API's import path and its
patch surface. Most of what it defined now lives in the modules of
``kiro_crew.dashboard.messaging_api``, one responsibility each, and
``messaging_api.compose`` runs every function they define on the facade's globals.
These tests pin:

* the surface: every name the facade bound before the split still resolves on it,
  the routes and the package re-exports dispatch to the facade's objects, the spawn
  routes stay guarded, and the seams other modules import keep their identity;
* the composition: every owner function runs on the facade's globals, reads only
  names the facade binds, and captures no name a test rebinds on the facade;
* the guards: what repository guards read in ``messaging.py`` by path stays there,
  and each guard widened to read the owners still fails when its construct is
  planted in an owner;
* the send route's split: its audit row still reads what a fallback leg reached
  when that leg is cancelled part-way.
"""

from __future__ import annotations

import ast
import asyncio
import builtins
import dis
import hashlib
import importlib
import importlib.util
import inspect
import pkgutil
import re
import shutil
import subprocess
import sys
import textwrap
import types
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from source_corpus import repo_files_named, repo_root

import kiro_crew.dashboard.handlers as handlers_pkg
import kiro_crew.dashboard.handlers.messaging as msg
from kiro_crew.dashboard import messaging_api
from kiro_crew.subprocess_utf8 import UTF8_TEXT

_FACADE = msg.__name__
_FACADE_PATH = Path(msg.__file__).resolve()
_OWNER_PACKAGE = messaging_api.__name__
_OWNER_DIR = Path(messaging_api.__file__).resolve().parent
_SRC = _FACADE_PATH.parents[3]

#: Every module-level name ``messaging`` bound at the base the split was cut from:
#: what it defined and what it imported, private names included, because tests and
#: production read private names off it too. A name bound only by ``import <module>``
#: of a stdlib or third-party module is left out: nothing reads ``json`` or ``re``
#: off the facade, and pinning one would fail on the removal of an unused import.
_BASE_NAMES = frozenset("""
        Any BLOCKS_REMOTE_MEDIA_ERROR CHANNEL_CONFIG_SECTIONS CHANNEL_ID_RE CHANNEL_MAX_LEN
        CHANNEL_SEND_NAMESPACES CRON_NOTIFICATION_KIND CRON_NOTIFY_END CRON_NOTIFY_PREFIX
        CRON_SESSION_RE Callable ChannelLink CronStoreBusy CronStoreUnreadable
        DEFAULT_COMMAND_TIMEOUT_MS DEFAULT_DRAIN_WAIT_MS DEFERRED_QUEUED_REASONS
        DISMISSAL_FAILED DISMISSAL_LOG_ABSENT DISMISSAL_LOG_COMMITTED DISMISSAL_LOG_FAILED
        DISMISSAL_NO_FOLDER DM_TARGET_PREFIX DashboardState HOST_SESSION_KEY
        IMESSAGE_SERVICES IS_MACOS KiroCrewConfig LINK_WINDOW_SECS
        NATIVE_CHILD_NOT_RESUMABLE NoPanelError NotificationPayload
        NotificationValidationError OPTIONS_FALLBACK_TEXT PERSISTED_SUBAGENT_REPLAY_KEEP
        PERSISTED_SUBAGENT_REPLAY_MAX_AGE_SECS PanelRecords Path PostedOptions
        QueueFullError QueuedReadUnavailable QueuedRun QueuedRunListing ROUTE_PREFIX
        SESSION_LINK_ACTION SLACK_NAMESPACE SLACK_THREAD_TS_RE SPAWN_RUN_SCHEMA
        SUBAGENT_COMPLETION_KIND SUBAGENT_COMPLETION_META_KEY SUCCESSOR_UNKNOWN
        TELEGRAM_ACTIVATIONS ValidationError _ABSENT _CHANNEL_SDK_EXTRA _CHANNEL_TYPE_RE
        _COLLECTED_IDS_CAP _COLLECTED_ID_MAX_LEN _ChannelSendFailed _CredentialTupleChanged
        _DISCORD_TOKEN_RE _DM_TARGET_PREFIX _EMOJI_NAME_RE _IDENTITY_LESS_RUN_CONTROL
        _LockedSectionWrite _MAX_BLOCKS _MAX_WALK_DEPTH _RESERVED_SESSION_TARGETS
        _SEND_MESSAGE_CHANNEL_TYPES _SLACK_ONLY_BODY_FIELDS _SLACK_SECRET_FIELDS
        _SLACK_SECTION_TEXT_MAX _SLACK_TS_MAX_LEN _SPAWN_REJECTED_CODE
        _SPAWN_STATUS_MAX_GREP_LEN _SPAWN_STATUS_MAX_LINES _TEAMS_CREDENTIAL_REJECT_STATUSES
        _TELEGRAM_TOKEN_RE _TOKEN_VERIFY_TIMEOUT _ThresholdPairInverted
        _WEBEX_VERIFY_TIMEOUT _agent_dir _apply_result_view _audit_allow _audit_deny
        _awaiting_spawn_approval _blocks_request_remote_media _browser_install_active
        _browser_install_conflict _browser_install_job _browser_install_status
        _browser_view_payload _channel_delivery_key _channel_sdk_status _clean_id_list
        _clean_imessage_path _coerce_like _continue_on_loop _deliver_channel_dm
        _deliver_to_channel _deny_non_owner_browser_request _discord_config_save_locked
        _feishu_config_save_locked _hot_apply_after_write _imessage_config_save _is_slack_ts
        _is_valid_feishu_id _is_valid_imessage_handle _is_valid_teams_principal
        _is_valid_webex_email _is_valid_wecom_userid _loader _log_panel_dismissal
        _mask_secret _missing_scope_message _native_child_refusal _owner_dm_target
        _pip_install_channel_available _queue_unreadable _queued_lookup _queued_not_started
        _queued_run _queued_run_payload _queued_runs _redact _redact_all
        _remove_queued_by_id _resolve_session_link_url _resolve_session_target
        _retry_failed_run _run_belongs_to_caller _sanitize_blocks _sel
        _send_to_channel_target _session_link_blocks _slack_config_save_locked
        _spawn_on_loop _spawn_request_memory_mode _spawn_result_view _spawn_scope_refusal
        _start_browser_install_job _teams_config_save _telegram_config_save_locked
        _terminate_install_scope _threshold_pct_rejection _validate_discord_token
        _validate_slack_token _validate_teams_app_credentials _validate_telegram_token
        _validate_webex_token _vet_channel_send _webex_config_save _wecom_config_save_locked
        _wide_env_refusal _write_env_off_loop _write_env_updates _write_env_updates_locked
        annotations api_browser_command api_browser_command_drain api_browser_command_result
        api_browser_engine_install api_browser_install_get api_browser_install_start
        api_browser_open api_browser_token_put api_browser_view_get api_browser_view_start
        api_channel_folder_backfill api_delete_message api_discord_config_get
        api_discord_config_save api_feishu_config_get api_feishu_config_save
        api_imessage_config_get api_imessage_config_save api_notification_ack
        api_notification_agent_push api_notification_channel_settings
        api_notification_channels api_notification_delete api_notification_unack
        api_notifications api_notifications_ack_all api_notifications_clear api_send_message
        api_slack_config_get api_slack_config_save api_slack_manifest api_slack_pins
        api_slack_profile api_slack_reactions api_spawn api_spawn_continue api_spawn_delete
        api_spawn_list api_spawn_lost api_spawn_mark_collected api_spawn_release
        api_spawn_retry api_spawn_status api_spawn_steer api_spawn_stop_all
        api_teams_activity api_teams_config_get api_teams_config_save
        api_telegram_config_get api_telegram_config_save api_update_message
        api_webex_config_get api_webex_config_save api_wecom_config_get
        api_wecom_config_save atomic_write backfill_channel_folder
        blocks_request_remote_media browser_cli_install browser_cli_launcher
        browser_cli_token browser_cli_view browser_install_job build_options_blocks
        caller_names_a_missing_slot cast channel_restart_required chunk_for_transport
        chunk_text classify_persisted_ending clean_session_folder config_path
        dashboard_slot_key delivery_confirmed display_safe_for drained_to_thread
        effective_session_key effort_applied_note effort_drop_reason ensure_channel_folder
        extract_options format_overflow generate_token get_command_bus
        guard_owner_surface_routes internal_memory_scope is_direct_local_request
        is_proxied_request is_sensitive_path live logger mint_options_token
        parent_spawn_allowlists parent_work_supported persisted_replay_denial_reason
        persisted_snapshot_denial_reason pip_extra_install_command platform_compat
        read_bounded_json read_config_text read_panel_records read_state read_tombstone
        record_panel_dismissal_outcome redact_credentials redact_exfiltration_urls
        redact_for_display rehydrate_slot_from_history_async remember_slack_options
        run_to_completion slack_options_owner_key slot_owner_snapshot sole_direct_target
        stop_browser_install
        stored_folder_name subagent_event_slot validate_tool_args
        warm_project_agents_for_spawn web
    """.split())

#: The owners the facade composes. Adding or removing one changes the composition,
#: so the set is spelled out rather than globbed.
_OWNER_MODULES = (
    "spawn",
    "run_control",
    "run_views",
    "notifications",
    "proactive_send",
    "channel_delivery",
    "slack_settings",
    "discord_settings",
    "telegram_settings",
    "teams_settings",
    "webex_settings",
    "imessage_settings",
    "wecom_settings",
    "feishu_settings",
)

#: Each moved name and the owner its responsibility puts it in.
_BASE_OWNERS: dict[str, tuple[str, ...]] = {
    "spawn": tuple("""
        _continue_on_loop _spawn_on_loop _spawn_request_memory_mode api_spawn
        api_spawn_continue
        """.split()),
    "run_control": tuple("""
        _log_panel_dismissal _native_child_refusal _queue_unreadable _queued_lookup
        _queued_not_started _queued_run _queued_run_payload _queued_runs _retry_failed_run
        _spawn_scope_refusal
        api_spawn_delete api_spawn_lost api_spawn_mark_collected api_spawn_release
        api_spawn_retry api_spawn_steer api_spawn_stop_all
        """.split()),
    "run_views": tuple("""
        _apply_result_view _awaiting_spawn_approval _redact _spawn_result_view
        api_spawn_list api_spawn_status
        """.split()),
    "notifications": tuple("""
        api_notification_ack api_notification_channel_settings api_notification_channels
        api_notification_delete api_notification_unack api_notifications
        api_notifications_ack_all api_notifications_clear
        """.split()),
    "proactive_send": tuple("""
        _redact_all _resolve_session_link_url _resolve_session_target _sanitize_blocks
        _session_link_blocks api_delete_message api_update_message
        """.split()),
    "channel_delivery": tuple("""
        _ChannelSendFailed _channel_delivery_key _deliver_channel_dm _deliver_to_channel
        _owner_dm_target _send_to_channel_target _vet_channel_send
        """.split()),
    "slack_settings": tuple("""
        _slack_config_save_locked _validate_slack_token api_slack_config_get
        api_slack_config_save
        """.split()),
    "discord_settings": tuple("""
        _discord_config_save_locked _validate_discord_token api_discord_config_get
        api_discord_config_save
        """.split()),
    "telegram_settings": tuple("""
        _telegram_config_save_locked _validate_telegram_token api_telegram_config_get
        api_telegram_config_save
        """.split()),
    "teams_settings": tuple("""
        _is_valid_teams_principal _teams_config_save _validate_teams_app_credentials
        api_teams_config_get api_teams_config_save
        """.split()),
    "webex_settings": tuple("""
        _coerce_like _is_valid_webex_email _validate_webex_token _webex_config_save
        api_webex_config_get api_webex_config_save
        """.split()),
    "imessage_settings": tuple("""
        _clean_imessage_path _imessage_config_save _is_valid_imessage_handle
        api_imessage_config_get
        """.split()),
    "wecom_settings": tuple("""
        _is_valid_wecom_userid _wecom_config_save_locked api_wecom_config_get
        api_wecom_config_save
        """.split()),
    "feishu_settings": tuple("""
        _channel_sdk_status _feishu_config_save_locked _is_valid_feishu_id
        api_feishu_config_get api_feishu_config_save
        """.split()),
}

#: SHA-256 of the sorted ``"<name> <kind> <signature>"`` lines of every name in
#: ``_BASE_OWNERS``, captured from the one-module file before the split: each moved
#: name keeps the kind and signature it had there.
_BASE_SHAPE_DIGEST = "140f95078d00ee2c3b73e500e5ec24d015d4dd307d18a5cf2efbbf232c4dfc3d"


#: Helpers the split of ``api_send_message`` added to the send owner: the body it
#: reads, the record its fallback legs fill, and the legs, audit and answer.
_SEND_SPLIT = (
    "_SendMessageBody",
    "_SendMessageOutcome",
    "_read_send_message",
    "_deliver_send_message_fallback",
    "_post_send_message_to_slack",
    "_audit_send_message",
    "_send_message_response",
)

#: The ``api_spawn*`` routes that scope their own caller and run unwrapped; every
#: other one runs behind the owner-surface guard the facade applies.
_MEMBER_SCOPED = frozenset(
    {
        "api_spawn",
        "api_spawn_continue",
        "api_spawn_lost",
        "api_spawn_mark_collected",
        "api_spawn_list",
        "api_spawn_stop_all",
    }
)


def _owner(stem: str) -> types.ModuleType:
    return importlib.import_module(f"{_OWNER_PACKAGE}.{stem}")


def _owners() -> list[types.ModuleType]:
    return [_owner(info.name) for info in pkgutil.iter_modules([str(_OWNER_DIR)])]


def _owner_sources() -> dict[str, str]:
    return {
        path.stem: path.read_text(encoding="utf-8") for path in sorted(_OWNER_DIR.glob("[!_]*.py"))
    }


def _owner_functions() -> list[tuple[str, types.FunctionType]]:
    """``(label, function)`` for every function an owner's file defines at top level
    or as a member of a class the owner defines."""
    found: list[tuple[str, types.FunctionType]] = []
    for owner in _owners():
        for name, value in vars(owner).items():
            members = [(name, value)]
            if isinstance(value, type) and value.__module__ == owner.__name__:
                members = [(f"{name}.{k}", v) for k, v in vars(value).items()]
            for label, member in members:
                fn = getattr(member, "__func__", member)
                if isinstance(fn, types.FunctionType) and fn.__code__.co_filename == owner.__file__:
                    found.append((f"{owner.__name__.rsplit('.', 1)[-1]}.{label}", fn))
    return found


def _global_names(code: types.CodeType):
    """Every global a code object and its nested code objects read or write."""
    for instruction in dis.get_instructions(code):
        if instruction.opname in ("LOAD_GLOBAL", "STORE_GLOBAL", "DELETE_GLOBAL"):
            yield instruction.argval
    for constant in code.co_consts:
        if isinstance(constant, types.CodeType):
            yield from _global_names(constant)


def _run_child(tmp_path: Path, script: str, *args: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script), *args],
        capture_output=True,
        timeout=120,
        cwd=str(tmp_path),
        **UTF8_TEXT,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("ok")


# ── the surface ───────────────────────────────────────────────────────────────


def test_every_name_the_facade_bound_at_the_base_still_resolves() -> None:
    """Routes, the handlers package, other handlers and tests read private names off
    the facade as well as public ones, so every module-level binding survives."""
    assert len(_BASE_NAMES) > 270
    assert sorted(name for name in _BASE_NAMES if not hasattr(msg, name)) == []


def test_a_fresh_interpreter_sees_every_base_public_name(tmp_path: Path) -> None:
    """The public names resolve in a process that imports nothing else first, and the
    handlers package's re-exports are the facade's own objects there too."""
    public = sorted(name for name in _BASE_NAMES if not name.startswith("_"))
    assert len(public) > 150
    _run_child(
        tmp_path,
        """
        import sys
        import kiro_crew.dashboard.handlers as pkg
        import kiro_crew.dashboard.handlers.messaging as msg
        missing = [n for n in sys.argv[1:] if not hasattr(msg, n)]
        assert missing == [], missing
        foreign = [n for n in sys.argv[1:] if hasattr(pkg, n) and n.startswith("api_")
                   and getattr(pkg, n) is not getattr(msg, n)]
        assert foreign == [], foreign
        print("ok")
        """,
        *public,
    )


def _package_reexports() -> list[str]:
    tree = ast.parse(Path(handlers_pkg.__file__).read_text(encoding="utf-8"))
    return [
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == _FACADE
        for alias in node.names
    ]


def _server_handler_names() -> set[str]:
    """``handlers.<name>`` attributes ``dashboard/server.py`` registers or calls, with
    the ``server_runtime`` owners it composes (the MCP route table lives in one)."""
    from kiro_crew.dashboard import server

    owners = sorted((Path(server.__file__).parent / "server_runtime").glob("[!_]*.py"))
    assert owners, "expected the server_runtime owners beside server.py"
    trees = [
        ast.parse(path.read_text(encoding="utf-8")) for path in (Path(server.__file__), *owners)
    ]
    return {
        node.attr
        for tree in trees
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "handlers"
        and node.attr in vars(msg)
    }


def test_the_routes_and_the_package_reach_the_facade_objects() -> None:
    """Every route the gateway registers for a messaging handler, every name the
    handlers package re-exports, and every handler ``server.py`` names is the
    facade's object, so the guarded ``api_spawn*`` wrappers are what is served."""
    from kiro_crew.dashboard import routes

    app = web.Application()
    routes.register_all(app)
    served = [
        route.handler
        for route in app.router.routes()
        if getattr(msg, getattr(route.handler, "__name__", ""), None) is not None
        and route.handler.__module__ == _FACADE
    ]
    assert len({id(h) for h in served}) >= 28
    assert [h.__name__ for h in served if getattr(msg, h.__name__) is not h] == []
    reexports = _package_reexports()
    assert len(reexports) == 59
    assert [n for n in reexports if getattr(handlers_pkg, n) is not getattr(msg, n)] == []
    server_names = _server_handler_names()
    assert {"api_spawn", "api_send_message", "stop_browser_install"} <= server_names
    assert len(server_names) >= 29
    assert [n for n in server_names if getattr(handlers_pkg, n) is not getattr(msg, n)] == []


@pytest.mark.parametrize(
    "name", sorted(n for n in _BASE_NAMES if n.startswith("api_spawn")), ids=str
)
def test_a_spawn_route_is_guarded_around_its_composed_function(name: str) -> None:
    """The facade composes its owners BEFORE its owner-surface guard runs, so each
    guarded ``api_spawn*`` route wraps the composed function, and a member-scoped
    route is the composed function itself."""
    route = getattr(msg, name)
    inner = inspect.unwrap(route)
    assert inner.__globals__ is vars(msg)
    assert inner is vars(_owner(Path(inner.__code__.co_filename).stem))[name]
    if name in _MEMBER_SCOPED:
        assert route is inner
    else:
        assert route is not inner and route.__wrapped__ is inner


def test_the_seams_other_modules_import_keep_their_identity() -> None:
    """``files.py``, the QR and WhatsApp setup handlers and the Slack gateway import
    names from the facade; each still resolves there, as the same object."""
    from kiro_crew.dashboard.handlers import files, weixin_qr, whatsapp_setup

    assert files._resolve_session_target is msg._resolve_session_target
    assert weixin_qr.is_direct_local_request is msg.is_direct_local_request
    assert whatsapp_setup.is_direct_local_request is msg.is_direct_local_request
    lazy: set[str] = set()
    for path in [_SRC / "kiro_crew/slack/gateway.py", *(_SRC / "kiro_crew/slack").rglob("*.py")]:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        lazy |= {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module == _FACADE
            for alias in node.names
        }
    assert "_slot_for_parent" in lazy
    assert sorted(name for name in lazy if not hasattr(msg, name)) == []


#: Names the moved code imports in its own body, from the module that owns them,
#: and must keep importing from there (not from a re-route).
_IMPORTED_BY_NAME = {
    "_resolve_channel_target": "kiro_crew.dashboard.chat_runner",
    "_run_chat": "kiro_crew.dashboard.chat_runner",
    "_get_config_lock": "kiro_crew.dashboard.handlers.agents",
    "is_allowed_user": "kiro_crew.slack.handler",
    "is_tracked_channel": "kiro_crew.slack.handler",
}


def test_the_names_the_handlers_import_by_name_are_not_rerouted() -> None:
    sources = [_FACADE_PATH.read_text(encoding="utf-8"), *_owner_sources().values()]
    found: dict[str, set[str]] = {name: set() for name in _IMPORTED_BY_NAME}
    for source in sources:
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name in found:
                        found[alias.name].add(node.module or "")
    assert found == {name: {module} for name, module in _IMPORTED_BY_NAME.items()}


def test_a_star_import_carries_the_moved_public_names(tmp_path: Path) -> None:
    """The facade declares no ``__all__``, so every public binding goes out."""
    assert not hasattr(msg, "__all__")
    probe = tmp_path / "messaging_star_probe.py"
    probe.write_text(
        "from kiro_crew.dashboard.handlers.messaging import *  # noqa: F401,F403\n",
        encoding="utf-8",
    )
    spec = importlib.util.spec_from_file_location("messaging_star_probe", probe)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name in ("api_spawn", "api_spawn_list", "stop_browser_install", "parent_work_supported"):
        assert getattr(module, name) is getattr(msg, name)


# ── the split send route ──────────────────────────────────────────────────────


class _SendRequest:
    """``POST /api/send-message`` double: the app state and a JSON body."""

    def __init__(self, state: Any, body: dict) -> None:
        self.app = {"state": state}
        self.headers: dict[str, str] = {}
        self._body = body

    async def json(self) -> dict:
        return self._body

    def get(self, key: str, default: Any = None) -> Any:
        return default


def test_a_cancelled_slack_post_still_audits_the_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fallback legs record what they reached on one outcome record, so a Slack
    post cancelled mid-flight is audited as the failed attempt it was, exactly as
    the one-function route did with its locals, and the cancellation propagates."""
    sel = MagicMock()
    monkeypatch.setattr(msg, "_sel", lambda: sel)
    client = MagicMock()
    client.open_dm = AsyncMock(return_value="D1")
    client.post_message = AsyncMock(side_effect=asyncio.CancelledError)
    state = MagicMock()
    state.slack_client = client
    state.owner_id = "U0OWNER"
    request = _SendRequest(state, {"text": "hello", "session": "slack"})
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(msg.api_send_message(request))  # type: ignore[arg-type]
    rows = [c.kwargs for c in sel.log_tool_invocation.call_args_list]
    assert rows == [
        {
            "session_key": "dashboard",
            "tool_name": "send_message",
            "outcome": "error",
            "downstream_service": "dashboard",
            "resources": "fallback=owner_dm",
            "error": "",
        }
    ]
    client.post_message.assert_awaited_once()


def test_the_send_split_lives_in_the_send_owner() -> None:
    owner = _owner("proactive_send")
    for name in _SEND_SPLIT:
        assert getattr(msg, name) is vars(owner)[name]
    assert msg._SendMessageOutcome.__module__ == owner.__name__


# ── the composition ───────────────────────────────────────────────────────────


def test_the_owner_set_is_the_package() -> None:
    assert {info.name for info in pkgutil.iter_modules([str(_OWNER_DIR)])} == set(_OWNER_MODULES)
    assert set(_BASE_OWNERS) == set(_OWNER_MODULES)


def _shape(obj: object) -> str:
    if inspect.isclass(obj):
        return "class"
    prefix = "async def " if inspect.iscoroutinefunction(obj) else "def "
    return prefix + str(inspect.signature(obj))  # type: ignore[arg-type]


def test_every_moved_name_is_one_object_in_its_owner() -> None:
    """The facade's binding of a moved name is the owner's object (through the
    guard, for a guarded route), in the owner its responsibility names."""
    strays = [
        f"{owner}:{name}"
        for owner, names in _BASE_OWNERS.items()
        for name in names
        if inspect.unwrap(getattr(msg, name)) is not vars(_owner(owner)).get(name)
    ]
    assert strays == []


def test_the_moved_names_keep_their_base_shapes() -> None:
    lines = sorted(
        f"{name} {_shape(getattr(msg, name))}" for names in _BASE_OWNERS.values() for name in names
    )
    assert len(lines) == 86
    digest = hashlib.sha256("\n".join(lines).encode()).hexdigest()
    assert digest == _BASE_SHAPE_DIGEST, "\n".join(lines)


def _facade_module_assignments() -> set[str]:
    tree = ast.parse(_FACADE_PATH.read_text(encoding="utf-8"))
    names = set()
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else []
        if isinstance(node, ast.AnnAssign):
            targets = [node.target]
        names |= {t.id for t in targets if isinstance(t, ast.Name)}
    return names


#: Definitions that stay in the facade file, each because something reads it there:
#: the census-pinned Slack routes and the redact-then-authorize block of
#: ``api_send_message`` with its origin-session injection; the two routes and the
#: iMessage save entry that path-keyed registers name; the two predicates unowned
#: specs cite by this path; and the shared channel-config transaction every
#: settings owner commits through.
_FACADE_DEFS = (
    "api_send_message",
    "api_slack_pins",
    "api_slack_reactions",
    "api_slack_profile",
    "_missing_scope_message",
    "api_notification_agent_push",
    "api_teams_activity",
    "api_imessage_config_save",
    "api_channel_folder_backfill",
    "api_slack_manifest",
    "_run_belongs_to_caller",
    "parent_work_supported",
    "_LockedSectionWrite",
    "_ThresholdPairInverted",
    "_CredentialTupleChanged",
    "_sel",
    "_is_slack_ts",
    "_mask_secret",
    "_clean_id_list",
    "_threshold_pct_rejection",
    "_wide_env_refusal",
    "_write_env_off_loop",
    "_write_env_updates",
    "_write_env_updates_locked",
)


def test_facade_state_stays_on_the_facade() -> None:
    """Owner functions reach module state by name through the facade's namespace,
    so a test that rebinds one there is the binding every function sees -- which
    holds only while no owner keeps a copy."""
    state = _facade_module_assignments()
    assert {"logger", "_SPAWN_REJECTED_CODE", "_COLLECTED_IDS_CAP", "_ABSENT"} <= state
    assert {
        o.__name__: sorted(state & set(vars(o))) for o in _owners() if state & set(vars(o))
    } == {}


@pytest.mark.parametrize("name", _FACADE_DEFS)
def test_a_facade_definition_stays_in_the_facade_file(name: str) -> None:
    obj = inspect.unwrap(getattr(msg, name))
    code = getattr(obj, "__code__", None)
    if code is not None:
        assert Path(code.co_filename).resolve() == _FACADE_PATH
    else:
        assert obj.__module__ == _FACADE
    assert [o.__name__ for o in _owners() if name in vars(o)] == []


def test_the_owners_log_as_the_facade() -> None:
    """Log capture keyed to ``kiro_crew.dashboard.handlers.messaging`` keeps seeing
    the moved sites: an owner function logs through the facade's ``logger``."""
    assert msg.logger.name == _FACADE
    readers = [
        label for label, fn in _owner_functions() if "logger" in set(_global_names(fn.__code__))
    ]
    assert len(readers) >= 15


def test_every_owner_function_runs_on_the_facade_globals() -> None:
    """A patch of ``kiro_crew.dashboard.handlers.messaging.<name>`` reaches an owner
    function only because the function reads the facade's globals, not its own."""
    labels = {label for label, _ in _owner_functions()}
    assert len(labels) >= sum(len(names) for names in _BASE_OWNERS.values())
    strays = [
        label
        for label, fn in _owner_functions()
        if fn.__globals__ is not vars(msg) or fn.__module__ != _FACADE
    ]
    assert strays == []


def test_the_sweep_reports_a_global_the_facade_does_not_bind() -> None:
    """The name sweep can fail, nested bodies included."""

    def _probe() -> object:
        def _inner() -> object:
            return _absent_from_the_messaging_namespace  # noqa: F821

        return _inner

    assert "_absent_from_the_messaging_namespace" in set(_global_names(_probe.__code__))


def test_every_global_an_owner_function_reads_is_bound_on_the_facade() -> None:
    """An owner's own imports are inert for its functions, so a name missing from
    the facade surfaces only when its line runs -- often inside an ``except`` that
    turns the NameError into a refusal. The sweep makes it a test failure instead."""
    namespace = vars(msg)
    unresolved = sorted(
        (label, name)
        for label, fn in _owner_functions()
        for name in set(_global_names(fn.__code__))
        if name not in namespace and not hasattr(builtins, name)
    )
    assert unresolved == []


def test_a_patch_of_the_facade_reaches_an_owner_function(monkeypatch: pytest.MonkeyPatch) -> None:
    """The contract the rebinding exists for, across two owners: the queued-run
    payload (run_control) redacts through ``_redact`` (run_views)."""
    monkeypatch.setattr(msg, "_redact", lambda text: f"redacted:{text}")
    queued = types.SimpleNamespace(
        id="r1", task="t", agent="a", accepted_at=0, reason="", reason_detail="", resuming=""
    )
    payload = msg._queued_run_payload(queued)  # type: ignore[arg-type]
    assert (payload["task"], payload["agent"]) == ("redacted:t", "redacted:a")


def test_module_and_qualname_still_name_the_facade() -> None:
    """Reprs and pickling by reference read as before the split: every owner
    function resolves back through its own ``__module__`` and ``__qualname__``."""
    wrong = []
    for label, fn in _owner_functions():
        target: object = sys.modules[fn.__module__]
        for part in fn.__qualname__.split("."):
            target = (
                vars(target).get(part) if isinstance(target, type) else getattr(target, part, None)
            )
            target = getattr(target, "__func__", target)
        if inspect.unwrap(target) is not fn:  # type: ignore[arg-type]
            wrong.append(label)
    assert wrong == []
    assert msg._ChannelSendFailed.__module__ == f"{_OWNER_PACKAGE}.channel_delivery"


def test_a_moved_function_reads_its_source_from_its_owner() -> None:
    source = inspect.getsource(msg.api_spawn_status)
    assert source.startswith("async def api_spawn_status(")
    assert (
        inspect.getsourcefile(inspect.unwrap(msg.api_spawn_status)) == _owner("run_views").__file__
    )


# ── one edge ──────────────────────────────────────────────────────────────────


def _package_of(path: Path) -> str:
    parts = list(path.resolve().relative_to(_SRC).with_suffix("").parts)
    return ".".join(parts[:-1])


def _import_targets(tree: ast.Module, package: str) -> list[tuple[ast.AST, str]]:
    """``(node, dotted module)`` for every module a tree imports, spelled any way:
    ``import a.b``, ``from a import b``, relative imports resolved against
    *package*, and a string-literal ``import_module(...)`` / ``__import__(...)``."""
    found: list[tuple[ast.AST, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((node, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                base = importlib.util.resolve_name("." * node.level + base, package)
            found.append((node, base))
            found.extend((node, f"{base}.{alias.name}") for alias in node.names)
        elif (
            isinstance(node, ast.Call)
            and getattr(node.func, "attr", getattr(node.func, "id", ""))
            in ("import_module", "__import__")
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
            and not node.args[0].value.startswith(".")
        ):
            found.append((node, node.args[0].value))
    return found


def _within(target: str, module: str) -> bool:
    return target == module or target.startswith(f"{module}.")


def _type_checking_nodes(tree: ast.Module) -> set[int]:
    """Nodes under a module-level ``if TYPE_CHECKING:`` body; its ``else`` runs."""
    return {
        id(sub)
        for node in tree.body
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING"
        for stmt in node.body
        for sub in ast.walk(stmt)
    }


def _owner_runtime_edges(source: str, package: str) -> list[int]:
    """Lines where an owner imports the facade or an owner outside ``TYPE_CHECKING``."""
    tree = ast.parse(source)
    guarded = _type_checking_nodes(tree)
    return sorted(
        {
            node.lineno
            for node, target in _import_targets(tree, package)
            if (_within(target, _FACADE) or _within(target, _OWNER_PACKAGE))
            and id(node) not in guarded
        }
    )


@pytest.mark.parametrize(
    ("source", "flagged"),
    [
        ("from . import spawn\n", True),
        ("from .run_views import api_spawn_list\n", True),
        ("from ..handlers import messaging\n", True),
        ("from kiro_crew.dashboard.handlers import messaging\n", True),
        ("import kiro_crew.dashboard.handlers.messaging as handlers\n", True),
        ("def f():\n    from kiro_crew.dashboard.handlers.messaging import _sel\n", True),
        (
            "import importlib\nimportlib.import_module('kiro_crew.dashboard.handlers.messaging')\n",
            True,
        ),
        ("__import__('kiro_crew.dashboard.messaging_api.spawn')\n", True),
        ("if TYPE_CHECKING:\n    from kiro_crew.dashboard.handlers.messaging import _sel\n", False),
        ("from kiro_crew.dashboard import chat_utils\n", False),
        ("import kiro_crew.dashboard.handlers.messaging_elsewhere\n", False),
    ],
)
def test_the_owner_edge_check_sees_every_spelling(source: str, flagged: bool) -> None:
    assert bool(_owner_runtime_edges(source, _OWNER_PACKAGE)) is flagged


def test_nothing_but_the_facade_imports_an_owner() -> None:
    """The facade is the one import path and the one patch surface."""
    importers = []
    for path in sorted((_SRC / "kiro_crew").rglob("*.py")):
        resolved = path.resolve()
        if _OWNER_DIR in resolved.parents or resolved == _FACADE_PATH or "_vendor" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        if "messaging_api" not in text:
            continue
        tree = ast.parse(text)
        importers.extend(
            f"{path.relative_to(_SRC)}:{node.lineno}"
            for node, target in _import_targets(tree, _package_of(path))
            if _within(target, _OWNER_PACKAGE)
        )
    assert importers == []


def test_an_owner_imports_the_facade_and_its_siblings_only_for_type_checking() -> None:
    offenders = [
        f"{stem}:{line}"
        for stem, source in _owner_sources().items()
        for line in _owner_runtime_edges(source, _OWNER_PACKAGE)
    ]
    assert offenders == []


#: Packages no owner imports in any spelling, ``TYPE_CHECKING`` included: the
#: agent-SDK boundary counts a type-only edge too, and the chat runner and chat
#: handler owners are reached only through their own facades.
_OWNER_FORBIDDEN = (
    "kiro_crew.acp",
    "kiro_crew.providers",
    "kiro_crew.dashboard.chat_turn",
    "kiro_crew.dashboard.chat_api",
)


def test_no_owner_imports_an_acp_provider_or_chat_owner_module() -> None:
    offenders = [
        f"{stem}:{node.lineno}:{target}"
        for stem, source in _owner_sources().items()
        for node, target in _import_targets(ast.parse(source), _OWNER_PACKAGE)
        if any(_within(target, forbidden) for forbidden in _OWNER_FORBIDDEN)
    ]
    assert offenders == []


def test_a_fresh_facade_import_loads_every_owner(tmp_path: Path) -> None:
    """Importing the facade imports every owner with it: none loads lazily on a
    later call, so the import order stays the one the one-module file had."""
    _run_child(
        tmp_path,
        """
        import sys
        import kiro_crew.dashboard.handlers.messaging
        missing = [n for n in sys.argv[1:]
                   if f"kiro_crew.dashboard.messaging_api.{n}" not in sys.modules]
        assert missing == [], missing
        print("ok")
        """,
        *_OWNER_MODULES,
    )


def test_a_second_facade_import_recomposes_the_owners_onto_it(tmp_path: Path) -> None:
    _run_child(
        tmp_path,
        """
        import sys
        import kiro_crew.dashboard.handlers.messaging as first
        del sys.modules["kiro_crew.dashboard.handlers.messaging"]
        import kiro_crew.dashboard.handlers.messaging as second
        from kiro_crew.dashboard.messaging_api import run_views
        assert second is not first
        assert second.api_spawn_status.__wrapped__.__globals__ is vars(second)
        assert run_views.api_spawn_list is second.api_spawn_list
        print("ok")
        """,
    )


# ── the patch reach ───────────────────────────────────────────────────────────

_PATCH_CALLS = ("setattr", "patch.object", "delattr")
_MULTIPLE_OPTIONS = frozenset({"spec", "create", "spec_set", "autospec", "new_callable"})
_FACADE_STRING = re.compile(r"""^kiro_crew\.dashboard\.handlers\.messaging\.(\w+)$""")

#: The patches whose attribute the scan cannot resolve from the source: each reads
#: its validator name from the channel-env test's module-level table. Keyed by
#: (test file, enclosing function); the names are the rows of that table's validator
#: column the function drives.
_RESOLVED_DYNAMIC_PATCHES = {
    ("test_channel_env_write_off_loop.py", "_drive"): frozenset(
        {
            "_validate_slack_token",
            "_validate_discord_token",
            "_validate_webex_token",
            "_validate_telegram_token",
        }
    ),
    ("test_env_file_bom.py", "test_a_wide_env_answers_409_with_the_remedy_and_changes_nothing"): (
        frozenset({"_validate_slack_token", "_validate_webex_token"})
    ),
    (
        "test_env_file_bom.py",
        "test_a_save_that_keeps_its_config_says_the_other_settings_were_saved",
    ): (frozenset({"_validate_discord_token", "_validate_telegram_token"})),
}


def _facade_aliases(tree: ast.Module) -> set[str]:
    """Every expression spelling a test module binds to the facade, to a fixed point."""
    aliases = {_FACADE, f"sys.modules[{_FACADE!r}]", f'sys.modules["{_FACADE}"]'}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            aliases |= {a.asname for a in node.names if a.name == _FACADE and a.asname}
        elif isinstance(node, ast.ImportFrom) and node.module == "kiro_crew.dashboard.handlers":
            aliases |= {a.asname or a.name for a in node.names if a.name == "messaging"}
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target, value = node.targets[0], node.value
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                target, value = node.target, node.value
            else:
                continue
            if not isinstance(target, ast.Name) or target.id in aliases:
                continue
            if ast.unparse(value) in aliases or _imports_the_facade(value):
                aliases.add(target.id)
                changed = True
    return aliases


def _imports_the_facade(node: ast.AST) -> bool:
    """``import_module(<facade>)``, or ``__import__(<facade>, fromlist=...)`` with a
    non-empty fromlist (which returns the facade itself, not its root package)."""
    if not (
        isinstance(node, ast.Call)
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == _FACADE
    ):
        return False
    func = ast.unparse(node.func)
    if func.endswith("import_module"):
        return True
    fromlist = {k.arg: k.value for k in node.keywords}.get("fromlist")
    if fromlist is None and len(node.args) >= 4:
        fromlist = node.args[3]
    return (
        func == "__import__" and isinstance(fromlist, (ast.List, ast.Tuple)) and bool(fromlist.elts)
    )


def _parametrized_strings(function: ast.AST) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for decorator in getattr(function, "decorator_list", []):
        if not (
            isinstance(decorator, ast.Call)
            and ast.unparse(decorator.func).endswith("parametrize")
            and len(decorator.args) >= 2
            and isinstance(decorator.args[0], ast.Constant)
            and isinstance(decorator.args[1], (ast.List, ast.Tuple))
        ):
            continue
        names = [n.strip() for n in str(decorator.args[0].value).split(",")]
        if len(names) == 1:
            values = {e.value for e in decorator.args[1].elts if isinstance(e, ast.Constant)}
            if values and all(isinstance(v, str) for v in values):
                found[names[0]] = values
    return found


def _resolve_name(node: ast.AST, params: dict[str, set[str]]) -> set[str] | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, ast.Name) and node.id in params:
        return set(params[node.id])
    return None


def _resolve_target(node: ast.AST, params: dict[str, set[str]]) -> set[str] | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        match = _FACADE_STRING.match(node.value)
        return {match.group(1)} if match else set()
    if isinstance(node, ast.JoinedStr) and ast.unparse(node).startswith(f"f'{_FACADE}."):
        tail = node.values[-1]
        if len(node.values) == 2 and isinstance(tail, ast.FormattedValue):
            return _resolve_name(tail.value, params)
        return None
    return set()


def _patched_names_in(text: str) -> tuple[set[str], set[str]]:
    """``(names, dynamic)``: first-level names one test source rebinds on the
    facade, and the enclosing functions of each patch whose name the scan cannot
    resolve -- which fails the reach test closed unless it is a resolved one."""
    if "messaging" not in text:
        return set(), set()
    tree = ast.parse(text)
    aliases = _facade_aliases(tree)
    found: set[str] = set()
    dynamic: set[str] = set()
    functions = [
        n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    seen: set[int] = set()
    scopes = [(tree, {}, "<module>")] + [
        (fn, _parametrized_strings(fn), fn.name) for fn in functions
    ]
    for scope, params, label in reversed(scopes):
        for node in ast.walk(scope):
            if id(node) in seen:
                continue
            seen.add(id(node))
            if isinstance(node, ast.Call):
                func = ast.unparse(node.func)
                kw = {k.arg: k.value for k in node.keywords if k.arg}
                target = node.args[0] if node.args else kw.get("target")
                if target is not None and (
                    ast.unparse(target) in aliases or _imports_the_facade(target)
                ):
                    if func.endswith(_PATCH_CALLS):
                        name = (
                            node.args[1]
                            if len(node.args) >= 2
                            else kw.get("attribute", kw.get("name"))
                        )
                        resolved = _resolve_name(name, params) if name is not None else None
                        if resolved is None:
                            dynamic.add(label)
                        else:
                            found |= resolved
                    elif func.endswith("patch.multiple"):
                        if any(k.arg is None for k in node.keywords):
                            dynamic.add(label)
                        found |= {k for k in kw if k not in _MULTIPLE_OPTIONS}
                elif target is not None and func.split(".")[-1] in ("patch", "setattr", "delattr"):
                    resolved = _resolve_target(target, params)
                    if resolved is None:
                        dynamic.add(label)
                    else:
                        found |= resolved
            elif isinstance(node, (ast.Assign, ast.AugAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Attribute) and ast.unparse(target.value) in aliases:
                        found.add(target.attr)
    found |= set(re.findall(r"""["']kiro_crew\.dashboard\.handlers\.messaging\.(\w+)["']""", text))
    return found, dynamic


def _facade_patched_names() -> set[str]:
    root = repo_root()
    here = Path(__file__).resolve()
    found: set[str] = set()
    unresolved = []
    for path in repo_files_named(".py"):
        parts = path.relative_to(root).parts
        in_tests = parts[0] == "test" or (parts[0] == "src" and "tests" in parts)
        if in_tests and path.resolve() != here:
            names, dynamic = _patched_names_in(path.read_text(encoding="utf-8", errors="replace"))
            found |= names
            for label in dynamic:
                key = (path.name, label)
                if key in _RESOLVED_DYNAMIC_PATCHES:
                    found |= _RESOLVED_DYNAMIC_PATCHES[key]
                else:
                    unresolved.append(key)
    assert unresolved == [], "a test patches a facade name the scan cannot resolve"
    return found


def _captured_names(source: str) -> set[str]:
    """Names an owner module binds or evaluates when it LOADS, outside
    ``TYPE_CHECKING``: everything a later patch of the facade cannot reach."""
    tree = ast.parse(source)
    guarded = _type_checking_nodes(tree)
    found: set[str] = set()

    def loads(node: ast.AST) -> set[str]:
        return {
            n.id for n in ast.walk(node) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
        }

    def visit(statements: list[ast.stmt]) -> None:
        for node in statements:
            if id(node) in guarded:
                continue
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                found.update((a.asname or a.name).split(".")[0] for a in node.names)
                found.update(a.name.split(".")[-1] for a in node.names)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for part in node.decorator_list + node.args.defaults:
                    found.update(loads(part))
                for part in node.args.kw_defaults:
                    if part is not None:
                        found.update(loads(part))
            elif isinstance(node, ast.ClassDef):
                for part in node.decorator_list + node.bases + [k.value for k in node.keywords]:
                    found.update(loads(part))
                for stmt in node.body:
                    if isinstance(stmt, (ast.AnnAssign, ast.Assign)) and stmt.value is not None:
                        found.update(loads(stmt.value))
            elif isinstance(node, (ast.If, ast.Try, ast.With, ast.For, ast.While)):
                for field in ("test", "iter", "items"):
                    value = getattr(node, field, None)
                    if isinstance(value, ast.AST):
                        found.update(loads(value))
                    elif isinstance(value, list):
                        for item in value:
                            found.update(loads(item))
                for block in ("body", "orelse", "finalbody"):
                    visit(getattr(node, block, []))
                for handler in getattr(node, "handlers", []):
                    if handler.type is not None:
                        found.update(loads(handler.type))
                    visit(handler.body)
            elif not (
                node is tree.body[0]
                and isinstance(node, ast.Expr)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            ):
                found.update(loads(node))

    visit(tree.body)
    return found


def test_the_patch_scan_reads_every_spelling() -> None:
    planted = (
        "import importlib, sys\n"
        "import kiro_crew.dashboard.handlers.messaging as handlers\n"
        "from kiro_crew.dashboard.handlers import messaging as ms\n"
        "facade = importlib.import_module('kiro_crew.dashboard.handlers.messaging')\n"
        "alias = facade\n"
        "held = sys.modules['kiro_crew.dashboard.handlers.messaging']\n"
        "def test(monkeypatch):\n"
        "    monkeypatch.setattr(handlers, 'first', 1)\n"
        "    monkeypatch.setattr(ms, 'second', 2)\n"
        "    patch.object(alias, 'third')\n"
        "    ms.fourth = 4\n"
        "    monkeypatch.setattr('kiro_crew.dashboard.handlers.messaging.fifth', 5)\n"
        "    monkeypatch.setattr(ms.Shared, 'attr', 7)\n"
        "    monkeypatch.setattr(other, 'not_the_facade', 8)\n"
        "    monkeypatch.delattr(ms, 'sixth')\n"
        "    patch.object(target=alias, attribute='seventh')\n"
        "    patch.multiple(held, eighth=1, create=True)\n"
        "@pytest.mark.parametrize('which', ['ninth', 'tenth'])\n"
        "def test_param(which):\n"
        "    patch(f'kiro_crew.dashboard.handlers.messaging.{which}')\n"
        "def test_dunder(monkeypatch):\n"
        "    mod = __import__('kiro_crew.dashboard.handlers.messaging', fromlist=['x'])\n"
        "    patch.object(mod, 'eleventh')\n"
        "    patch.object(__import__('kiro_crew.dashboard.handlers.messaging', fromlist=['y']), 'twelfth')\n"
        "    patch.object(__import__('kiro_crew.dashboard.handlers.messaging'), 'not_the_facade')\n"
    )
    names, dynamic = _patched_names_in(planted)
    assert names == {
        "first",
        "second",
        "third",
        "fourth",
        "fifth",
        "sixth",
        "seventh",
        "eighth",
        "ninth",
        "tenth",
        "eleventh",
        "twelfth",
    }
    assert dynamic == set()
    unresolvable = (
        "from kiro_crew.dashboard.handlers import messaging as ms\n"
        "def _drive(monkeypatch, name):\n"
        "    monkeypatch.setattr(ms, name, 1)\n"
    )
    assert _patched_names_in(unresolvable) == (set(), {"_drive"})


def test_the_capture_scan_flags_what_an_owner_evaluates_when_it_loads() -> None:
    planted = (
        '"""An owner."""\n'
        "from typing import TYPE_CHECKING\n"
        "import asyncio as aio\n"
        "if TYPE_CHECKING:\n"
        "    from kiro_crew.dashboard.handlers.messaging import _sel\n"
        "LIMIT = _CAP * 2\n"
        "def f(x=_DEFAULT, *, y=_KW):\n"
        "    return _sel(), is_direct_local_request\n"
        "class C(_Base):\n"
        "    attr: int = _CLASS_BODY\n"
    )
    captured = _captured_names(planted)
    assert {"aio", "asyncio", "_CAP", "_DEFAULT", "_KW", "_Base", "_CLASS_BODY"} <= captured
    assert {"_sel", "is_direct_local_request"} & captured == set()


def test_no_owner_captures_a_name_tests_rebind_on_the_facade() -> None:
    """An owner that imported, defaulted or evaluated a rebound name when it loaded
    would keep that object, and a patch of the facade would silently stop applying
    there. An owner may DEFINE one: the facade's binding of it is the composed copy
    (through the guard, for a guarded route), and every caller reads it through the
    facade's globals."""
    patched = _facade_patched_names()
    assert {
        "_sel",
        "is_direct_local_request",
        "_write_env_updates",
        "_write_env_off_loop",
        "read_state",
        "generate_token",
        "_spawn_scope_refusal",
        "warm_project_agents_for_spawn",
        "_validate_telegram_token",
        "_deliver_channel_dm",
        "_send_to_channel_target",
    } <= patched
    assert len(patched) >= 35
    for stem, source in _owner_sources().items():
        assert _captured_names(source) & patched == set(), stem
        defined = {name for name in vars(_owner(stem)) if name in patched}
        assert all(
            inspect.unwrap(getattr(msg, name)) is vars(_owner(stem))[name] for name in defined
        ), stem


# ── the path-keyed guards keep their reach ────────────────────────────────────

#: Constructs repository guards read in ``dashboard/handlers/messaging.py`` by
#: path: the cron origin-session dispatch the turn-timeout scan counts, the slot
#: task publish the slot-reader inventory keys by
#: ``api_send_message``, and the two bounded body reads the JSON-body register
#: keys by function. An owner that grew one would move it out of the guard's
#: sight, so each stays in the facade.
_STAYS_IN_THE_FACADE = (
    r"spawn_guarded_turn\(",
    r"\b_run_chat\(",
    r"\bslot\.task = task\b",
    r"await read_bounded_json\(",
)


@pytest.mark.parametrize("pattern", _STAYS_IN_THE_FACADE)
def test_a_construct_a_guard_reads_in_the_facade_stays_there(pattern: str) -> None:
    assert re.search(pattern, _FACADE_PATH.read_text(encoding="utf-8"))
    holders = [stem for stem, source in _owner_sources().items() if re.search(pattern, source)]
    assert holders == []


def test_the_census_sites_stay_in_the_facade() -> None:
    """``test_security_posture``'s census pins twelve gate-side baseline log sites
    to this path; every one stays here and no owner grows one."""
    from test_security_posture import _BASELINE_LOG_SITE_CENSUS, _gate_side_baseline_log_sites

    sites = _gate_side_baseline_log_sites(_FACADE_PATH.read_text(encoding="utf-8"))
    assert len(sites) == _BASELINE_LOG_SITE_CENSUS["dashboard/handlers/messaging.py"] == 12
    grown = {
        stem: len(found)
        for stem, source in _owner_sources().items()
        if (found := _gate_side_baseline_log_sites(source))
    }
    assert grown == {}


def test_every_owner_coroutine_file_is_checked_for_config_dir() -> None:
    import test_no_config_dir_in_async as guard

    with_async = {
        f"dashboard/messaging_api/{path.name}"
        for path in _OWNER_DIR.glob("[!_]*.py")
        if "async def " in path.read_text(encoding="utf-8")
    }
    assert len(with_async) == len(_OWNER_MODULES)
    assert with_async <= set(guard._ASYNC_CHECKED_FILES)


def _mirror(tmp_path: Path) -> Path:
    """A checkout-shaped copy of every file the widened guards read."""
    root = tmp_path / "checkout"
    rels = [
        "dashboard/handlers/messaging.py",
        "dashboard/ws.py",
        "subagent_persistence.py",
        "platform/update_layout.py",
        *(f"dashboard/messaging_api/{path.name}" for path in _OWNER_DIR.glob("*.py")),
    ]
    for rel in rels:
        target = root / "src/kiro_crew" / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(_SRC / "kiro_crew" / rel, target)
    (root / "test").mkdir()
    return root


def _red_green(root: Path, check: Any, owner: str, old: str | None, new: str) -> None:
    """*check* passes on the copy, fails once *new* is planted in *owner* (replacing
    every *old*, or appended), and passes again once the owner is restored."""
    path = root / "src/kiro_crew/dashboard/messaging_api" / f"{owner}.py"
    text = path.read_text(encoding="utf-8")
    check()
    if old is None:
        path.write_text(text + new, encoding="utf-8")
    else:
        assert old in text, old
        path.write_text(text.replace(old, new), encoding="utf-8")
    with pytest.raises(AssertionError):
        check()
    path.write_text(text, encoding="utf-8")
    check()


def _facade_in(root: Path) -> str:
    return str(root / "src/kiro_crew/dashboard/handlers/messaging.py")


def test_the_parked_run_ratchet_reads_the_owners(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import test_subagent_spawn_approval_parked_6484 as guard

    root = _mirror(tmp_path)
    monkeypatch.setattr(msg, "__file__", _facade_in(root))
    check = (
        guard.TestParkedRunIsVisibleOnBothReadPaths().test_both_endpoints_use_the_shared_predicate
    )
    _red_green(root, check, "run_views", "if _awaiting_spawn_approval(info):", "if info.done:")


def test_the_listing_grant_ratchet_reads_the_owners(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import test_subagent_panel_durable_replay as guard

    root = _mirror(tmp_path)
    cls = guard.TestThePersistedGrantIsRecordedNotJustTheRefusals
    monkeypatch.setattr(cls, "ROOT", root)
    check = cls().test_the_rest_listing_records_the_same_grant
    old = '_audit_allow(auditee, "api_spawn_list")'
    _red_green(root, check, "run_views", old, '_audit_allow(auditee, "api_spawn_rows")')


def test_the_panel_reader_ratchet_reads_the_owners(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import test_subagent_panel_durable_replay as guard

    root = _mirror(tmp_path)
    monkeypatch.setattr(
        guard, "__file__", str(root / "test" / "test_subagent_panel_durable_replay.py")
    )
    check = (
        guard.TestADismissalHoldsOnBothReaders().test_each_durable_source_is_reached_through_the_reader_that_filters_it
    )
    _red_green(root, check, "run_views", None, "\n# _panel_record(folder)\n")


def test_the_options_stub_ratchet_reads_the_owners(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import test_slack_options_lifecycle as guard

    root = _mirror(tmp_path)
    monkeypatch.setattr(msg, "__file__", _facade_in(root))
    check = (
        guard.TestControlPostedAfterTheWindowIsSpent().test_send_message_options_use_the_safe_fallback_stub
    )
    _red_green(root, check, "proactive_send", "OPTIONS_FALLBACK_TEXT,", "text=text,")


def test_the_splitter_offload_ratchet_reads_the_owners(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import test_split_credential_boundary as guard

    root = _mirror(tmp_path)
    monkeypatch.setattr(guard, "SRC", root / "src/kiro_crew")
    check = (
        guard.TestTheDashboardHandlerLegsOffloadTheSplitter().test_both_dashboard_legs_offload_the_splitter
    )
    old = "units = await asyncio.to_thread(\n        chunk_for_transport,"
    _red_green(root, check, "channel_delivery", old, "units = chunk_for_transport(")


def test_the_named_delegation_ratchet_reads_the_owners(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import test_crew_members_doc as guard

    root = _mirror(tmp_path)
    doc = guard.DOC.read_text(encoding="utf-8")
    monkeypatch.setattr(guard, "__file__", str(root / "test" / "test_crew_members_doc.py"))

    def check() -> None:
        guard.test_empty_triggers_only_skip_routing_not_named_delegation(doc)

    _red_green(root, check, "spawn", None, '\n# "crew_delegation_disabled"\n')


def test_the_config_dir_ratchet_reads_the_owners(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import test_no_config_dir_in_async as guard

    root = _mirror(tmp_path)
    monkeypatch.setattr(guard, "SRC", root / "src/kiro_crew")
    check = guard.TestNoConfigDirInAsync().test_update_layout_channel_helpers_never_maintain
    _red_green(root, check, "notifications", None, "\n\nasync def _planted():\n    config_dir()\n")


# ── compose, on its own ───────────────────────────────────────────────────────


def _write_module(tmp_path: Path, name: str, source: str) -> types.ModuleType:
    path = tmp_path / f"{name}.py"
    path.write_text(textwrap.dedent(source), encoding="utf-8")
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_compose_rebinds_functions_and_class_members(tmp_path: Path) -> None:
    """On synthetic modules: a module function, a nested function, a method, a
    static method and a property of an owner class all read the host namespace
    afterwards; a function the owner merely imported is left alone; the owner class
    keeps its own module; and a second compose onto a fresh namespace moves them."""
    owner = _write_module(
        tmp_path,
        "messaging_api_compose_owner_probe",
        """
        from os.path import join

        def helper():
            return VALUE

        def outer():
            def inner():
                return VALUE
            return inner

        class Tally:
            def method(self):
                return VALUE

            @staticmethod
            def static():
                return VALUE

            @property
            def value(self):
                return VALUE
        """,
    )
    namespace = {"__name__": "messaging_api_compose_host_probe", "VALUE": "host"}
    namespace["helper"] = owner.helper
    messaging_api.compose(namespace, (owner,))

    assert namespace["helper"]() == "host" and owner.helper is namespace["helper"]
    assert owner.outer()() == "host"
    assert owner.Tally().method() == "host"
    assert owner.Tally.static() == "host"
    assert owner.Tally().value == "host"
    assert owner.join.__module__ != "messaging_api_compose_host_probe"
    assert owner.helper.__module__ == "messaging_api_compose_host_probe"
    assert owner.Tally.__module__ == "messaging_api_compose_owner_probe"
    namespace["VALUE"] = "patched"
    assert namespace["helper"]() == "patched"

    fresh = {"__name__": "messaging_api_compose_host_probe", "VALUE": "fresh"}
    messaging_api.compose(fresh, (owner,))
    assert owner.helper() == "fresh"


# ── a moved settings owner on its own: the Telegram, Slack and WeCom savers ────


def _saver_put(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, channel: str, body: object
) -> tuple[int, Any]:
    """Drive ``PUT /api/<channel>/config`` with the config and ``.env`` in *tmp_path*."""
    from aiohttp.test_utils import TestClient, TestServer

    import kiro_crew.config.loader as loader

    env = tmp_path / ".env"
    if not env.exists():
        env.write_text("", encoding="utf-8")
    monkeypatch.setattr(loader, "env_path", lambda: env)
    monkeypatch.setattr(loader, "config_path", lambda: tmp_path / "config.json")
    monkeypatch.setattr(msg, "is_direct_local_request", lambda req: True)

    async def _run() -> tuple[int, Any]:
        app = web.Application()
        app.router.add_put(f"/api/{channel}/config", getattr(msg, f"api_{channel}_config_save"))
        async with TestClient(TestServer(app)) as client:
            resp = await client.put(f"/api/{channel}/config", json=body)
            return resp.status, await resp.json()

    return asyncio.run(_run())


def _telegram_put(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, body: dict) -> tuple[int, Any]:
    return _saver_put(monkeypatch, tmp_path, "telegram", body)


@pytest.mark.parametrize(
    ("body", "error"),
    [
        ({"bot_token_clear": "yes"}, "bot_token_clear must be a boolean"),
        ({"bot_token": "123:ab cd"}, "bot_token must not contain whitespace"),
        ({"bot_token": "nope"}, "bot_token must look like <bot_id>:<secret> from @BotFather"),
        ({"enabled": "yes"}, "enabled must be a boolean"),
        ({"allowed_user_ids": "1"}, "allowed_user_ids must be a list"),
        ({"show_thinking": "yes"}, "show_thinking must be a boolean"),
        ({"voice_replies": "yes"}, "voice_replies must be a boolean"),
        ({"allow_forum": "yes"}, "allow_forum must be a boolean"),
        ({"allowed_forum_chat_ids": "x"}, "allowed_forum_chat_ids must be a list"),
        ({"allowed_forum_chat_ids": ["abc"]}, "invalid Telegram chat ID: abc (integer IDs only)"),
    ],
)
def test_the_telegram_saver_refuses_a_bad_field_with_its_text(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, body: dict, error: str
) -> None:
    """Validate-first: each bad field is a 400 naming it, and nothing is written."""
    assert _telegram_put(monkeypatch, tmp_path, body) == (400, {"error": error})
    assert not (tmp_path / "config.json").exists()


def test_the_telegram_saver_refuses_an_unknown_forum_activation_by_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    status, payload = _telegram_put(monkeypatch, tmp_path, {"forum_activation": "bogus"})
    assert status == 400 and payload["code"] == "invalid_forum_activation"
    assert payload["error"] == "forum_activation must be one of " + ", ".join(
        sorted(msg.TELEGRAM_ACTIVATIONS)
    )


def test_the_telegram_saver_stages_the_toggles_it_validated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import json

    activation = sorted(msg.TELEGRAM_ACTIVATIONS - {"always"})[0]
    body = {
        "show_thinking": True,
        "voice_replies": True,
        "forum_activation": activation,
        "allow_forum": True,
        "allowed_forum_chat_ids": ["-1001", "", "-1001"],
    }
    status, payload = _telegram_put(monkeypatch, tmp_path, body)
    assert status == 200 and payload["ok"] is True
    stored = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))["telegram"]
    assert {k: stored[k] for k in body} == {**body, "allowed_forum_chat_ids": [-1001]}


@pytest.mark.parametrize("channel", ["slack", "wecom"])
@pytest.mark.parametrize(
    ("body", "status", "error"),
    [
        ([1], 400, "body must be an object"),
        ({"session_folder": 5}, 400, "Folder name must be text"),
        ({"session_folder": "a/b"}, 400, "Folder name cannot contain / or \\"),
    ],
)
def test_the_slack_and_wecom_savers_refuse_a_bad_body_with_its_text(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    channel: str,
    body: object,
    status: int,
    error: str,
) -> None:
    assert _saver_put(monkeypatch, tmp_path, channel, body) == (status, {"error": error})
    assert not (tmp_path / "config.json").exists()


@pytest.mark.parametrize("channel", ["slack", "wecom"])
def test_a_config_json_that_is_not_an_object_is_corrupt_to_the_savers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, channel: str
) -> None:
    """The BOM-aware read still answers a non-object ``config.json`` as corrupt."""
    cfg = tmp_path / "config.json"
    cfg.write_text("[1]", encoding="utf-8")
    assert _saver_put(monkeypatch, tmp_path, channel, {}) == (
        500,
        {"error": "config.json is corrupt"},
    )
    assert cfg.read_text(encoding="utf-8") == "[1]"


@pytest.mark.parametrize(
    ("body", "error"),
    [
        ({"bot_token": "xoxb-a b"}, "bot_token must not contain whitespace"),
        ({"owner_id": "X123"}, "owner_id must be a Slack member ID (starts with U or W)"),
        ({"command": "bad cmd!"}, "command must be alphanumeric/-/_ and at most 32 chars"),
        ({"allowed_enterprise_ids": ["zzz"]}, "invalid enterprise ID: zzz"),
        ({"reactions_enabled": "yes"}, "reactions_enabled must be a boolean"),
    ],
)
def test_the_slack_saver_refuses_a_bad_field_with_its_text(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, body: dict, error: str
) -> None:
    assert _saver_put(monkeypatch, tmp_path, "slack", body) == (400, {"error": error})
    assert not (tmp_path / "config.json").exists()


def test_the_slack_saver_refuses_a_token_slack_rejects(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A pasted env line is stripped to the token, which Slack is asked about first."""
    seen: list[tuple[str, str]] = []

    async def _reject(key: str, token: str) -> str:
        seen.append((key, token))
        return "invalid_auth"

    monkeypatch.setattr(msg, "_validate_slack_token", _reject)
    body = {"bot_token": "SLACK_BOT_TOKEN=xoxb-1"}
    assert _saver_put(monkeypatch, tmp_path, "slack", body) == (
        400,
        {"error": "bot_token rejected by Slack (invalid_auth)"},
    )
    assert seen == [("SLACK_BOT_TOKEN", "xoxb-1")]
    assert (tmp_path / ".env").read_text(encoding="utf-8") == ""


def test_the_slack_saver_stores_a_token_slack_could_not_be_asked_about(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def _unreachable(key: str, token: str) -> str:
        raise OSError("offline")

    monkeypatch.setattr(msg, "_validate_slack_token", _unreachable)
    # Recorded first, so the save's own write to the process environment is undone.
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-old")
    status, payload = _saver_put(monkeypatch, tmp_path, "slack", {"bot_token": "xoxb-2"})
    assert status == 200 and payload["ok"] is True, payload
    assert "SLACK_BOT_TOKEN=xoxb-2" in (tmp_path / ".env").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("body", "error"),
    [
        ({"bot_token": "x" * 257}, "bot secret is implausibly long"),
        ({"enabled": "yes"}, "enabled must be a boolean"),
        ({"allowed_user_ids": "a"}, "allowed_user_ids must be a list"),
    ],
)
def test_the_wecom_saver_refuses_a_bad_field_with_its_text(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, body: dict, error: str
) -> None:
    assert _saver_put(monkeypatch, tmp_path, "wecom", body) == (400, {"error": error})
    assert not (tmp_path / "config.json").exists()


def test_the_wecom_saver_stages_the_fields_it_validated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import json

    body = {"allowed_user_ids": ["a.b", "", "a.b"], "session_folder": "Work"}
    status, payload = _saver_put(monkeypatch, tmp_path, "wecom", body)
    assert status == 200 and payload["ok"] is True, payload
    stored = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))["wecom"]
    assert stored["allowed_users"] == [{"userid": "a.b", "name": ""}]
    assert stored["session_folder"] == "Work"


# ── the review rules keep their reach ─────────────────────────────────────────


def _globstar(pattern: str) -> re.Pattern[str]:
    """An AUTOSDE file pattern as a regex: ``**/`` spans zero or more directories,
    ``*`` and ``?`` stay inside one path segment."""
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out, i = out + "(?:[^/]+/)*", i + 3
        elif pattern.startswith("**", i):
            out, i = out + ".*", i + 2
        elif pattern[i] in "*?":
            out, i = out + ("[^/]*" if pattern[i] == "*" else "[^/]"), i + 1
        else:
            out, i = out + re.escape(pattern[i]), i + 1
    return re.compile(out + r"\Z")


#: Each facade the review rules name, and the owner package composed into it.
_FACADE_OWNER_DIRS = {
    "src/kiro_crew/dashboard/handlers/messaging.py": "src/kiro_crew/dashboard/messaging_api",
    "src/kiro_crew/dashboard/chat_handlers.py": "src/kiro_crew/dashboard/chat_api",
}


def _rules_missing_owners(rules: list[dict]) -> tuple[set[str], dict[str, list[str]]]:
    """``(ids of rules matching a facade, {id: owner files those rules miss})``."""
    root = repo_root()
    matched: set[str] = set()
    missing: dict[str, list[str]] = {}
    for rule in rules:
        patterns = [_globstar(p) for p in rule.get("file-patterns", [])]
        for facade, owner_dir in _FACADE_OWNER_DIRS.items():
            if not any(p.match(facade) for p in patterns):
                continue
            matched.add(rule["id"])
            owners = sorted(
                path.relative_to(root).as_posix() for path in (root / owner_dir).glob("*.py")
            )
            gaps = [o for o in owners if not any(p.match(o) for p in patterns)]
            if gaps:
                missing.setdefault(rule["id"], []).extend(gaps)
    return matched, missing


def _autosde_rules() -> list[dict]:
    import yaml

    root = repo_root()
    return [
        rule
        for name in ("AUTOSDE.yaml", "website/AUTOSDE.yaml")
        for rule in yaml.safe_load((root / name).read_text(encoding="utf-8"))["custom-rules"]
    ]


def test_the_globstar_matcher_reads_patterns_as_the_reviewers_do() -> None:
    assert _globstar("src/a/**/*.py").match("src/a/b.py")
    assert _globstar("src/a/**/*.py").match("src/a/x/y/b.py")
    assert not _globstar("src/a/*.py").match("src/a/x/b.py")
    assert _globstar("src/a/**").match("src/a/x/b.py")


def test_every_review_rule_on_a_facade_also_covers_its_owners() -> None:
    """A rule that reviews the facade reviews the code composed into it: an owner
    outside its patterns would take moved code out of that rule's sight."""
    rules = _autosde_rules()
    matched, missing = _rules_missing_owners(rules)
    assert {
        "memory-store-seam",
        "no-new-work-on-gateway-boot-path",
        "feature-map-correctness",
    } <= matched
    assert missing == {}
    narrowed = [
        (
            {
                **rule,
                "file-patterns": [p for p in rule["file-patterns"] if "messaging_api" not in p],
            }
            if rule["id"] == "memory-store-seam"
            else rule
        )
        for rule in rules
    ]
    assert _rules_missing_owners(narrowed)[1].keys() == {"memory-store-seam"}
