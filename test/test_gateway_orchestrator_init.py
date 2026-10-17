"""The orchestrator's constructor, and the fields it and the channel hoists assign.

``__init__`` is composed from ``gateway_runtime/orchestrator_init.py`` and the channel
hoists from ``gateway_runtime/channel_boot.py``. Both assign fields through an
annotated ``self: GatewayOrchestrator`` parameter, where an inline annotation is a
type error, so every field they assign is declared on the class instead, without a
value. These tests pin that the class declares exactly those fields, carrying the
annotations the constructor and the hoists wrote inline, that a declaration creates
no class attribute (a ``__new__`` fixture that skips ``__init__`` still gets
``AttributeError``), and that the constructor keeps its name, module and signature.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from kiro_crew.slack import gateway as gw_mod
from kiro_crew.slack.gateway import GatewayOrchestrator
from kiro_crew.slack.gateway_runtime import channel_boot, orchestrator_init

#: ``(function, field, annotation)`` for every ``self.<field>: <annotation> = ...``
#: the constructor and the channel hoists carried inline, read from their source by
#: AST when they moved into their owners, in source order.
_STRIPPED = (
    ("__init__", "_allowed_users", "set[str]"),
    ("__init__", "_tracking_channels", "set[str]"),
    ("__init__", "_open_channels", "set[str]"),
    ("__init__", "_channel_restart_tasks", "set[asyncio.Task[None]]"),
    ("__init__", "_channel_restart_gen", "dict[str, int]"),
    ("__init__", "_config_subs", "list[Subscription]"),
    ("__init__", "slack", "RealSlackClient | None"),
    ("__init__", "sessions", "SessionManager | None"),
    ("__init__", "ctx_builder", "ContextBuilder | None"),
    ("__init__", "conv_log", "ConversationLog | None"),
    ("__init__", "consolidator", "HistoryConsolidator | None"),
    ("__init__", "cron_svc", "CronService | None"),
    ("__init__", "heartbeat_svc", "HeartbeatService | None"),
    ("__init__", "autonudge_svc", "AutoNudgeService | None"),
    ("__init__", "secretary_svc", "object | None"),
    ("__init__", "subagent_mgr", "SubagentManager | None"),
    ("__init__", "_adaptive_controller", "AdaptiveController | None"),
    ("__init__", "_subagent_coalescer_inst", '"SubagentEventCoalescer | None"'),
    ("__init__", "_batch_progress", "dict[str, dict]"),
    ("__init__", "_cron_injecting", "dict[str, int]"),
    ("__init__", "_running_script_ids", "set[str]"),
    ("__init__", "task_runner", "TaskRunner | None"),
    ("__init__", "_runner_admission", "Any"),
    ("__init__", "channel_history", "ChannelHistory | None"),
    ("__init__", "dashboard_state", "DashboardState | None"),
    ("__init__", "_background_tasks", "set[asyncio.Task]"),
    ("__init__", "_memory_startup", "MemoryStartup | None"),
    ("__init__", "_memory_startup_task", "asyncio.Task | None"),
    ("__init__", "_memory_repair_task", "asyncio.Task | None"),
    ("__init__", "_console_script_repair_task", '"asyncio.Task[None] | None"'),
    ("__init__", "_marker_write_task", '"asyncio.Task[None] | None"'),
    ("__init__", "_sign_in_refresher_task", '"asyncio.Task[None] | None"'),
    ("__init__", "_dashboard_runner", "web.AppRunner | None"),
    ("__init__", "_handler_tasks", "set[asyncio.Task]"),
    ("__init__", "_session_tasks", "dict[str, asyncio.Task]"),
    ("__init__", "_pending_queue", "dict[str, list]"),
    ("__init__", "_socket_client", "WSSocketModeClient | None"),
    ("__init__", "_wecom_client", '"WeComClient | None"'),
    ("__init__", "_channel_handles", "dict[str, object]"),
    ("__init__", "_inbound_replay_task", '"asyncio.Task[None] | None"'),
    ("__init__", "_model_download_task", '"asyncio.Task[bool] | None"'),
    ("__init__", "_feature_video_task", '"asyncio.Task[bool] | None"'),
    ("__init__", "_auto_migrate_task", '"asyncio.Task[None] | None"'),
    ("__init__", "_update_check_task", '"asyncio.Task[None] | None"'),
    ("__init__", "_profile_watch_task", '"asyncio.Task[None] | None"'),
    ("__init__", "_pending_update_respawn", "Callable[[], str] | None"),
    ("__init__", "_mandatory_update_deferred_at", "float | None"),
    ("__init__", "_mandatory_update_deferred_key", "str | None"),
    ("__init__", "_wheel_update_noticed", "set[str]"),
    ("__init__", "_mcp_gateway_manager", "GatewayManager | None"),
    ("__init__", "_mcp_resolve_prefetch", "asyncio.Task[None] | None"),
    ("__init__", "_mcp_target_env", "dict[str, str]"),
    ("__init__", "_mcp_resolve_home", "str"),
    ("_hoist_telegram", "_telegram_allowed_user_ids", "list[int]"),
    ("_hoist_telegram", "_telegram_allow_forum", "bool"),
    ("_hoist_telegram", "_telegram_allowed_forum_chat_ids", "list[int]"),
    ("_hoist_telegram", "_telegram_client", '"TelegramClient | None"'),
    ("_hoist_weixin", "_weixin_account_id", "str"),
    ("_hoist_weixin", "_weixin_base_url", "str"),
    ("_hoist_weixin", "_weixin_dm_policy", "str"),
    ("_hoist_weixin", "_weixin_allowed_user_ids", "list[str]"),
    ("_hoist_weixin", "_weixin_client", '"WeixinClient | None"'),
    ("_hoist_whatsapp", "_whatsapp_client", '"WhatsAppClient | None"'),
    ("_hoist_feishu", "_feishu_allowed_open_ids", "list[str]"),
    ("_hoist_feishu", "_feishu_allow_group", "bool"),
    ("_hoist_feishu", "_feishu_allowed_group_ids", "list[str]"),
    ("_hoist_discord", "_discord_allowed_user_ids", "list[str]"),
    ("_hoist_discord", "_discord_allowed_thread_ids", "list[str]"),
    ("_hoist_discord", "_discord_allowed_channel_ids", "list[str]"),
    ("_hoist_discord", "_discord_client", '"DiscordClient | None"'),
    ("_hoist_webex", "_webex_allowed_emails", "list[str]"),
    ("_hoist_webex", "_webex_client", '"WebexClient | None"'),
    ("_hoist_imessage", "_imessage_client", '"IMessageClient | None"'),
    ("_hoist_teams", "_teams_allowed_emails", "list[str]"),
    ("_hoist_teams", "_teams_client", '"TeamsClient | None"'),
)

#: Declared before the constructor moved: each is first assigned in another owner.
_DECLARED_FOR_OTHER_OWNERS = frozenset(
    {
        "_slack_connect_error",
        "_adaptive_start_sub",
        "vector_memory",
        "_dashboard_port",
        "_configured_host",
        "_local_only",
        "_member_reconcile_task",
        "_persistence_watchdog_task",
        "_cron_quarantine",
    }
)

#: ``inspect.signature(GatewayOrchestrator)`` before the move.
_SIGNATURE = (
    "(cfg: 'KiroCrewConfig', *, no_dashboard: 'bool' = False, no_crons: 'bool' = False, "
    "no_open: 'bool' = False, port_override: 'str | None' = None, json_ready: 'bool' = "
    "False, approval_mode: 'str | None' = None, test_mode: 'bool' = False, credentials: "
    "'Mapping[str, str] | None' = None) -> 'None'"
)


def _class_declarations() -> dict[str, str]:
    """``{field: annotation text}`` for the class body's value-free declarations."""
    path = Path(gw_mod.__file__)
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    cls = next(
        n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "GatewayOrchestrator"
    )
    out: dict[str, str] = {}
    for node in cls.body:
        if isinstance(node, ast.AnnAssign) and node.value is None:
            assert isinstance(node.target, ast.Name)
            assert node.target.id not in out, node.target.id
            out[node.target.id] = ast.get_source_segment(source, node.annotation) or ""
    return out


def _owner_functions() -> list[ast.FunctionDef]:
    """The constructor and the nine hoists, parsed from their owners' source."""
    found: list[ast.FunctionDef] = []
    for module, wanted in (
        (orchestrator_init, lambda name: name == "__init__"),
        (channel_boot, lambda name: name.startswith("_hoist_")),
    ):
        path = Path(module.__file__)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        found += [n for n in tree.body if isinstance(n, ast.FunctionDef) and wanted(n.name)]
    assert sorted(f.name for f in found) == sorted(
        ["__init__"]
        + [
            f"_hoist_{c}"
            for c in (
                "wecom",
                "telegram",
                "weixin",
                "whatsapp",
                "feishu",
                "discord",
                "webex",
                "imessage",
                "teams",
            )
        ]
    )
    return found


def _assigned_fields(functions: list[ast.FunctionDef]) -> set[str]:
    return {
        node.attr
        for fn in functions
        for node in ast.walk(fn)
        if isinstance(node, ast.Attribute)
        and isinstance(node.ctx, ast.Store)
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
    }


def test_every_inline_annotation_became_a_class_declaration_with_its_text() -> None:
    declared = _class_declarations()

    assert len(_STRIPPED) == 75
    assert len({field for _fn, field, _ann in _STRIPPED}) == 75
    for function, field, annotation in _STRIPPED:
        assert declared.get(field) == annotation, (function, field)


def test_the_class_declares_exactly_the_fields_its_owners_assign() -> None:
    declared = set(_class_declarations())
    assigned = _assigned_fields(_owner_functions())

    assert {field for _fn, field, _ann in _STRIPPED} <= assigned
    assert assigned - declared == set(), "assigned in the constructor or a hoist, not declared"
    assert declared - assigned == _DECLARED_FOR_OTHER_OWNERS


def test_no_owner_assignment_carries_an_inline_annotation() -> None:
    inline = [
        (fn.name, node.target.attr)
        for fn in _owner_functions()
        for node in ast.walk(fn)
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Attribute)
    ]

    assert inline == []


def test_a_declaration_creates_no_class_attribute() -> None:
    bare = GatewayOrchestrator.__new__(GatewayOrchestrator)

    for field in _class_declarations():
        assert field not in vars(GatewayOrchestrator), field
        with pytest.raises(AttributeError):
            getattr(bare, field)


def test_the_constructor_keeps_its_name_module_and_signature() -> None:
    init = GatewayOrchestrator.__init__

    assert init.__name__ == "__init__"
    assert init.__qualname__ == "GatewayOrchestrator.__init__"
    assert init.__module__ == "kiro_crew.slack.gateway"
    assert str(inspect.signature(GatewayOrchestrator)) == _SIGNATURE


def test_the_type_checkers_constructor_stub_is_the_owners_signature() -> None:
    """The facade binds ``__init__`` under the ``else`` of ``if TYPE_CHECKING:`` and gives
    the type checker a stub in that branch; the stub must be the owner's signature, or
    constructor calls are checked against a signature that does not run."""
    facade = Path(gw_mod.__file__)
    tree = ast.parse(facade.read_text(encoding="utf-8"))
    cls = next(
        n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "GatewayOrchestrator"
    )
    stubs = [
        fn
        for node in cls.body
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING"
        for fn in node.body
        if isinstance(fn, ast.FunctionDef) and fn.name == "__init__"
    ]
    owner_path = Path(orchestrator_init.__file__)
    owner = next(
        n
        for n in ast.parse(owner_path.read_text(encoding="utf-8")).body
        if isinstance(n, ast.FunctionDef) and n.name == "__init__"
    )
    (stub,) = stubs
    assert ast.unparse(owner.args.args[0].annotation) == "GatewayOrchestrator"
    owner.args.args[0].annotation = None
    assert ast.dump(stub.args) == ast.dump(owner.args)
    assert ast.dump(stub.returns) == ast.dump(owner.returns)
