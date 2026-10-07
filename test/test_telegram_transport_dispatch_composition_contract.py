"""Composition contract between ``TelegramDispatcher`` and ``telegram/dispatch``.

``kiro_crew.telegram.transport_dispatch`` keeps the dispatcher's state, its front door
and turn engine and the constructs repository guards read in that file, and binds the
methods of the owners under ``kiro_crew/telegram/dispatch/`` as class attributes. What
this file pins is what a caller of the dispatcher observes independently of where each
rule lives:

* the module keeps every name it bound, a moved name by identity with its owner's, and
  ``from ... import *`` exposes the same public names;
* the class keeps every member it had, with the same kind and signature, and a member
  an owner defines is that owner's function itself, not a wrapper around it;
* a patch applied to the facade -- the names the tests rebind there, derived from the
  tests themselves -- still reaches the moved code that consumes it;
* the owners follow the placement rules that make that true: no runtime facade import
  at module level, no bare read of a patched name, siblings reached through their
  module, one logger, eager loading, and none of the constructs ``_FACADE_PINNED``
  lists, which records why each is kept out of the owners.
"""

from __future__ import annotations

import ast
import asyncio
import importlib
import importlib.util
import inspect
import logging
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from test_telegram import _dispatcher, _dm, _press

import kiro_crew.telegram.transport_dispatch as td
from kiro_crew.telegram.renderer import TelegramApprovalDecider

pytestmark = pytest.mark.xdist_group(name="tree_scan_telegram_dispatch")

FACADE = "kiro_crew.telegram.transport_dispatch"
FACADE_PATH = Path(td.__file__).resolve()
DISPATCH_DIR = FACADE_PATH.with_name("dispatch")
REPO = FACADE_PATH.parents[3]

#: The owners the dispatcher is composed from. Spelled out rather than globbed, so a
#: module added or removed is a deliberate change to the composition.
OWNERS = frozenset(
    {
        "addressing",
        "callbacks",
        "commands",
        "midturn",
        "origin",
        "pickers",
        "spawn_approval",
        "voice",
    }
)

#: Every name the facade bound at the base the split was cut from, private ones
#: included: tests and callers import several of them, and a name that silently
#: stops resolving breaks a caller nobody can see from here.
BASE_NAMES = frozenset(
    {
        "ACTIVATION_MENTION",
        "ACTIVATION_OFF",
        "APPROVAL_INTERACTIVE",
        "AcpError",
        "AgentInfo",
        "Any",
        "CHAT_TYPE_DIRECT",
        "CHAT_TYPE_FORUM",
        "ChannelLink",
        "ConversationOwnershipConflict",
        "ConversationState",
        "DENY_CAUSE_APPROVAL_TIMEOUT",
        "DM_SCOPE_UNIFIED",
        "InboundMessage",
        "InboundRoute",
        "IngestLimits",
        "NamedTuple",
        "ReceiptQueue",
        "ReceiptSurface",
        "ResumeReleaseError",
        "RoutingDecision",
        "SessionClosingError",
        "SilentRenderer",
        "SimpleNamespace",
        "Stats",
        "TELEGRAM_CAPABILITIES",
        "TOOL_AUTO_APPROVE",
        "TOOL_DENY",
        "TYPE_CHECKING",
        "TelegramApprovalDecider",
        "TelegramDispatcher",
        "TelegramInboundMessage",
        "TelegramRenderer",
        "TelegramSessionResume",
        "TurnCeilingExceeded",
        "TurnDriver",
        "UnknownMemoryStore",
        "YOLO_PHRASING_PLAIN",
        "_APPROVAL_REGISTRY",
        "_APP_AGENT_LINK_SEP",
        "_AUDIO_MIMES",
        "_BUSY_OPTIONS_REFUSAL",
        "_CHANNEL",
        "_DEFAULT_KIROCREW_AGENT",
        "_DETACH_EXEMPT_COMMANDS",
        "_FAILURE_REASON_MAX_CHARS",
        "_HELP_TEXT",
        "_MAX_COLLAPSE",
        "_MAX_COLLAPSED_ATTACHMENTS",
        "_MENTION_RES",
        "_MODEL_PICKER_MAX",
        "_MODEL_PICKER_TTL_SECS",
        "_NOT_A_SENDER",
        "_ORIGIN_PREFIX",
        "_PICKER_LIMIT",
        "_Picker",
        "_QueuedOrigin",
        "_RELEASE_FAILURE",
        "_STALE_OPTIONS_REFUSAL",
        "_STEER_ACK_EMOJI",
        "_TITLE_MAX_CHARS",
        "_UNTAGGED_OPTIONS_REFUSAL",
        "_VOICE_MIN_CHARS",
        "_agent_is_internal",
        "_audio_mime",
        "_clamp_pct",
        "_coerce_id_set",
        "_entry_owner",
        "_inbound_origin",
        "_mention_re",
        "_origin_kwargs",
        "_queued_origin",
        "_read_bytes",
        "_user_safe_failure_reason",
        "add_trusted_session",
        "admit_inbound_callback",
        "annotations",
        "append_attachment_context",
        "asynccontextmanager",
        "asyncio",
        "auto_title",
        "bind_origin_mirror",
        "build_auto_approve",
        "build_directive_consumer",
        "build_dm_session_key",
        "build_help_text",
        "cast",
        "channel_inbound_permitted",
        "channel_namespace_of",
        "charge_turn_failure",
        "cleanup_attachments",
        "compact_unsupported_backend",
        "compact_unsupported_reply",
        "consume_reinjection",
        "context_recycle_warning",
        "cron_command_reply",
        "dataclass",
        "delivery_is_muted",
        "display_safe",
        "drain_until_quiet",
        "driver_turn_landed",
        "entry_channel",
        "format_ttl",
        "forum_gate_outcome",
        "hook_gate_kwargs",
        "html",
        "is_bare_mid_turn_override",
        "is_session_trusted",
        "list_agents",
        "lists_host_state",
        "live",
        "logger",
        "logging",
        "md_to_telegram_html_safe",
        "mint_row_mid",
        "new_approval_nonce",
        "open_turn_crew_log",
        "os",
        "owner_token",
        "parse_command",
        "parse_command_argument",
        "parse_dashboard_argument",
        "parse_dashboard_ttl",
        "parse_mid_turn_override",
        "parse_session_key",
        "persisted_session_agent",
        "predecessor_sid",
        "privacy_mode",
        "process_telegram_attachments",
        "publish_turn_identity",
        "re",
        "rearm_reinjection",
        "rebind_conversation_location",
        "receipt_address_key",
        "recycle_backend",
        "recycle_warning_should_send",
        "redact",
        "redact_local_paths",
        "refused_resume_is_restricted",
        "register_drain",
        "release_conversation_location",
        "requested_model_sid",
        "reserve_new_generation",
        "rollback_skill_bodies",
        "run_in_embed_pool",
        "run_yolo_command",
        "runtime_death",
        "safety_override",
        "seed_generation",
        "sel",
        "session_blocks_reads",
        "session_is_restricted",
        "session_provenance_tag",
        "session_store_for_turn",
        "slot_workspace",
        "spawn_task_reply",
        "spool_refused_turn",
        "stop_running_turn",
        "suppress",
        "synthesis_settings",
        "synthesize_and_deliver",
        "tag_entry",
        "task_arg_reply",
        "time",
        "turn_ceiling",
        "unpressed_wait_answer",
        "uploads_restricted",
    }
)

#: Module-level names that moved, and the owner that now defines each.
MOVED_NAMES = {
    "origin": (
        "_CHANNEL",
        "_NOT_A_SENDER",
        "_ORIGIN_PREFIX",
        "_QueuedOrigin",
        "_entry_owner",
        "_inbound_origin",
        "_origin_kwargs",
        "_queued_origin",
    ),
    "pickers": (
        "_APP_AGENT_LINK_SEP",
        "_MODEL_PICKER_MAX",
        "_MODEL_PICKER_TTL_SECS",
        "_PICKER_LIMIT",
        "_Picker",
        "_agent_is_internal",
    ),
    "voice": ("_AUDIO_MIMES", "_VOICE_MIN_CHARS", "_audio_mime", "_read_bytes"),
    "addressing": ("_MENTION_RES", "_mention_re"),
    "callbacks": ("_UNTAGGED_OPTIONS_REFUSAL",),
    "commands": ("_RELEASE_FAILURE",),
}

#: Dispatcher members an owner defines, by owner. Each is bound on the class as the
#: owner's own function (wrapped in ``staticmethod`` where it was one).
MOVED_MEMBERS = {
    "addressing": ("_activation_outcome", "_addresses_this_bot", "_reply_target"),
    "callbacks": ("on_callback",),
    "commands": (
        "_handle_compact",
        "_handle_cron",
        "_handle_dashboard",
        "_handle_link",
        "_handle_spawn",
        "_handle_task",
        "_handle_unlink",
        "_handle_yolo",
        "_reply_markdown",
        "_require_direct_chat",
    ),
    "midturn": ("_handle_busy", "_handle_resumed_busy"),
    "pickers": (
        "_agent_choices",
        "_apply_agent",
        "_apply_model",
        "_consume_picker",
        "_handle_agent",
        "_handle_model",
        "_installed_agent_names",
        "_model_choices",
        "_prune_pickers",
    ),
    "spawn_approval": (
        "_spawn_chat_target",
        "_spawn_prompt_destination_permitted",
        "deliver_spawn_approval",
    ),
    "voice": ("_handle_voice", "_speak_reply", "_voice_enabled"),
}

#: Every member the class had at the base, with its kind and its signature as
#: ``inspect`` renders it. A moved method annotates ``self`` for the type checker;
#: that one annotation is the only difference the comparison removes.
BASE_MEMBERS = {
    "__init__": (
        "function",
        "(self, *, sessions: \"'SessionManager'\", ctx_builder: \"'ContextBuilder'\", "
        "cfg: \"'KiroCrewConfig'\", allowed_user_ids: 'set[int]', agent: 'str | None' = None, "
        "conv_log: \"'ConversationLog | None'\" = None, approval_mode: 'str' = 'interactive', "
        "cron_service: \"'CronService | None'\" = None, "
        "subagent_manager: \"'SubagentManager | None'\" = None, "
        "task_runner: \"'TaskRunner | None'\" = None) -> 'None'",
    ),
    "_activation_outcome": ("function", "(self, msg: 'InboundMessage') -> 'str | None'"),
    "_addresses_this_bot": ("function", "(self, msg: 'InboundMessage') -> 'bool'"),
    "_agent_choices": (
        "function",
        "(self, names: 'list[str]') -> 'tuple[tuple[str, str], ...]'",
    ),
    "_apply_agent": ("function", "(self, route: 'tuple[str, str]', agent_id: 'str') -> 'str'"),
    "_apply_model": (
        "function",
        "(self, route: 'tuple[str, str]', model_id: 'str', session_key: 'str | None' = None, "
        "*, store_route_preference: 'bool | None' = None) -> 'str'",
    ),
    "_authorized": ("function", "(self, user_id: 'int') -> 'bool'"),
    "_bind_origin_mirror": (
        "function",
        "(self, session_key: 'str', route: 'tuple[str, str]', chat_id: 'int') -> 'None'",
    ),
    "_blocks_memory_reads": ("function", "(self, session_key: 'str') -> 'bool'"),
    "_callback_session_key": (
        "function",
        "(self, route: 'tuple[str, str]', chat_id: 'int', thread_id: 'int | None', "
        "user_id: 'int', chat_type: 'str') -> 'str'",
    ),
    "_configured_agent": ("function", "(self) -> 'str'"),
    "_consume_picker": (
        "function",
        "(self, cb: \"'TelegramCallback'\", data: 'str', table: 'dict[str, _Picker]', *, "
        "noun: 'str', command: 'str') -> 'tuple[_Picker, str, str] | None'",
    ),
    "_drain_queue": ("function", "(self, session_key: 'str') -> 'None'"),
    "_enqueue_with_receipt": (
        "function",
        "(self, session_key: 'str', chat_id: 'int', text: 'str', *, "
        "thread: 'int | None' = None, attachments: 'list[Any] | None' = None, "
        "privacy_request: 'str' = '', origin: '_QueuedOrigin', "
        "person_origin: 'bool' = False) -> 'bool'",
    ),
    "_handle_agent": (
        "function",
        "(self, route: 'tuple[str, str]', chat_id: 'int', arg: 'str') -> 'None'",
    ),
    "_handle_busy": (
        "function",
        "(self, session_key: 'str', msg: 'InboundMessage', text: 'str', "
        "override_mode: 'str | None', *, thread: 'int | None' = None, "
        "privacy_request: 'str' = '', caller: 'str' = 'system') -> 'None'",
    ),
    "_handle_compact": (
        "function",
        "(self, route: 'tuple[str, str]', chat_id: 'int', *, "
        "session_key: 'str | None' = None) -> 'None'",
    ),
    "_handle_cron": (
        "function",
        "(self, chat_id: 'int', arg: 'str', *, caller: 'str' = '', "
        "thread: 'int | None' = None) -> 'None'",
    ),
    "_handle_dashboard": (
        "function",
        "(self, route: 'tuple[str, str]', chat_id: 'int', text: 'str', "
        "user_id: 'int') -> 'None'",
    ),
    "_handle_link": (
        "function",
        "(self, route: 'tuple[str, str]', chat_id: 'int', *, "
        "resumed_key: 'str | None' = None) -> 'None'",
    ),
    "_handle_model": (
        "function",
        "(self, route: 'tuple[str, str]', chat_id: 'int', arg: 'str', *, "
        "session_key: 'str | None' = None) -> 'None'",
    ),
    "_handle_spawn": (
        "function",
        "(self, route: 'tuple[str, str]', chat_id: 'int', arg: 'str', *, "
        "thread: 'int | None' = None, session_key: 'str | None' = None) -> 'None'",
    ),
    "_handle_stop": (
        "function",
        "(self, route: 'tuple[str, str]', chat_id: 'int', *, origin: '_QueuedOrigin', "
        "session_key: 'str | None' = None) -> 'None'",
    ),
    "_handle_task": (
        "function",
        "(self, chat_id: 'int', arg: 'str', *, route: 'tuple[str, str]', "
        "thread: 'int | None' = None, session_key: 'str | None' = None) -> 'None'",
    ),
    "_handle_title": (
        "function",
        "(self, route: 'tuple[str, str]', chat_id: 'int', arg: 'str', *, "
        "session_key: 'str | None' = None) -> 'None'",
    ),
    "_handle_unlink": ("function", "(self, route: 'tuple[str, str]', chat_id: 'int') -> 'None'"),
    "_handle_voice": (
        "function",
        "(self, route: 'tuple[str, str]', chat_id: 'int', arg: 'str', "
        "thread: 'int | None') -> 'None'",
    ),
    "_handle_yolo": (
        "function",
        "(self, chat_id: 'int', arg: 'str', user_id: 'int', *, "
        "thread: 'int | None' = None) -> 'None'",
    ),
    "_installed_agent_names": ("staticmethod", "() -> 'list[str]'"),
    "_live_cfg": ("function", "(self) -> \"'KiroCrewConfig'\""),
    "_maybe_notice": (
        "function",
        "(self, chat_id: 'int', route: 'tuple[str, str]', session_key: 'str', "
        "provider: 'Any') -> 'None'",
    ),
    "_model_choices": (
        "function",
        "(self, session_key: 'str') -> 'tuple[tuple[str, str], ...]'",
    ),
    "_notify": (
        "function",
        "(self, chat_id: 'int', note: 'str', *, thread: 'int | None' = None) -> 'None'",
    ),
    "_origin_mirror_link": (
        "function",
        "(self, route: 'tuple[str, str]', chat_id: 'int') -> 'ChannelLink'",
    ),
    "_persist_turn": (
        "function",
        "(self, session_key: 'str', user_text: 'str', reply_text: 'str', is_new: 'bool', "
        "agent: 'str | None' = None, mirror_mids: 'tuple[str, str] | None' = None, "
        "auto_title_pending: 'bool' = False) -> 'None'",
    ),
    "_prune_pickers": (
        "staticmethod",
        "(table: 'dict[str, _Picker]', now: 'float') -> 'None'",
    ),
    "_pump_queue": (
        "function",
        "(self, session_key: 'str', foreign_channels: 'set[str]') -> 'None'",
    ),
    "_receipt_flip_locked": (
        "function",
        "(self, session_key: 'str', chat_id: 'int', answered: 'list[str]', "
        "deferred: 'int' = 0, *, owner: 'str') -> 'None'",
    ),
    "_receipt_surface": (
        "function",
        "(self, chat_id: 'int', thread: 'int | None') -> 'ReceiptSurface'",
    ),
    "_reply": (
        "function",
        "(self, chat_id: 'int', text: 'str', *, thread: 'int | None' = None, "
        "**kw: 'Any') -> 'int | None'",
    ),
    "_reply_markdown": (
        "function",
        "(self, chat_id: 'int', text: 'str', *, thread: 'int | None' = None) -> 'int | None'",
    ),
    "_reply_target": (
        "staticmethod",
        "(msg: 'InboundMessage', *, interpret_commands: 'bool') -> 'int | None'",
    ),
    "_require_direct_chat": (
        "function",
        "(self, cmd: 'str', route: 'tuple[str, str]', chat_id: 'int', user_id: 'int', *, "
        "thread: 'int | None', subject: 'str') -> 'bool'",
    ),
    "_resolve_agent": ("function", "(self, route: 'tuple[str, str] | None' = None) -> 'str'"),
    "_rotated_session_key": ("function", "(self, route: 'tuple[str, str]') -> 'str'"),
    "_route_key": (
        "function",
        "(self, *, chat_type: 'str', user_id: 'int', chat_id: 'int', "
        "thread: 'str | int | None') -> 'tuple[str, str]'",
    ),
    "_route_thread": ("staticmethod", "(route: 'tuple[str, str]') -> 'int | None'"),
    "_routing_turn": (
        "function",
        "(self, route_id: 'str') -> \"'AsyncIterator[list[int]]'\"",
    ),
    "_seed_gen": ("function", "(self, route: 'tuple[str, str]') -> 'int'"),
    "_session_key": ("function", "(self, route: 'tuple[str, str]') -> 'str'"),
    "_session_restricted": ("function", "(self, session_key: 'str') -> 'bool'"),
    "_soft_threshold": ("function", "(self) -> 'int'"),
    "_spawn_chat_target": (
        "function",
        "(self, parent_session_key: 'str') -> 'tuple[int, int | None, str] | None'",
    ),
    "_spawn_prompt_destination_permitted": (
        "function",
        "(self, chat_id: 'int', thread_id: 'int | None') -> 'bool'",
    ),
    "_speak_reply": (
        "function",
        "(self, route: 'tuple[str, str]', chat_id: 'int', text: 'str', "
        "thread: 'int | None') -> 'None'",
    ),
    "_uploads_restricted": ("function", "(self, session_key: 'str') -> 'bool'"),
    "_voice_enabled": ("function", "(self, route: 'tuple[str, str]') -> 'bool'"),
    "dashboard_state": ("property", "(self) -> 'Any'"),
    "deliver_spawn_approval": (
        "function",
        "(self, request_id: 'str', description: 'str', "
        "parent_session_key: 'str') -> 'bool | None'",
    ),
    "handle_message": (
        "function",
        "(self, msg: 'InboundMessage', *, drain: 'bool' = True, "
        "interpret_commands: 'bool' = True, privacy_request: 'str' = '', "
        "origin_tag: 'str' = '') -> 'None'",
    ),
    "on_callback": ("function", "(self, cb: \"'TelegramCallback'\") -> 'None'"),
    "reconfigure": ("function", "(self, section: 'Any') -> 'None'"),
}

#: Members the composition added: the turn engine ``handle_message`` hands an admitted
#: message to, and the midturn owner's hand-off of a message into a busy RESUMED
#: dashboard session. Anything else new on the class is unexplained surface.
ADDED_MEMBERS = frozenset({"_run_turn", "_handle_resumed_busy"})


def _owner(stem: str) -> ModuleType:
    return importlib.import_module(f"kiro_crew.telegram.dispatch.{stem}")


def _owner_sources() -> dict[str, str]:
    return {stem: (DISPATCH_DIR / f"{stem}.py").read_text(encoding="utf-8") for stem in OWNERS}


def _kind(member: Any) -> str:
    if isinstance(member, staticmethod):
        return "staticmethod"
    if isinstance(member, property):
        return "property"
    return "function" if inspect.isfunction(member) else type(member).__name__


def _signature(member: Any) -> str:
    fn = member.fget if isinstance(member, property) else member
    fn = fn.__func__ if isinstance(fn, staticmethod) else fn
    return str(inspect.signature(fn)).replace("self: 'TelegramDispatcher'", "self", 1)


# ── the module surface ───────────────────────────────────────────────────────────


def test_every_base_name_is_still_bound() -> None:
    assert sorted(BASE_NAMES - set(vars(td))) == []


def test_a_moved_name_is_its_owners_object() -> None:
    for stem, names in MOVED_NAMES.items():
        owner = _owner(stem)
        for name in names:
            assert getattr(td, name) is getattr(owner, name), f"{stem}.{name}"


def test_a_moved_class_keeps_its_owner_module() -> None:
    assert td._QueuedOrigin.__module__ == "kiro_crew.telegram.dispatch.origin"
    assert td._Picker.__module__ == "kiro_crew.telegram.dispatch.pickers"


def test_the_only_names_the_facade_added_are_its_owner_handles() -> None:
    added = set(vars(td)) - BASE_NAMES - {n for n in vars(td) if n.startswith("__")}
    assert added == {f"_{stem}" for stem in OWNERS - {"origin"}}
    for name in added:
        assert vars(td)[name] is _owner(name[1:])


def test_a_star_import_exposes_the_base_public_names(tmp_path: Path) -> None:
    probe = tmp_path / "telegram_dispatch_star_probe.py"
    probe.write_text(f"from {FACADE} import *  # noqa: F401,F403\n", encoding="utf-8")
    spec = importlib.util.spec_from_file_location("telegram_dispatch_star_probe", probe)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    exported = {n for n in vars(module) if not n.startswith("__")}
    assert exported == {n for n in BASE_NAMES if not n.startswith("_")}


def test_the_facade_is_a_plain_module() -> None:
    # No forwarding: every name is a real binding, so a patch on the facade is a
    # patch on the module the owners read through.
    assert type(td) is ModuleType
    assert "__getattr__" not in vars(td)


def test_a_fresh_import_loads_every_owner() -> None:
    probe = (
        "import sys, kiro_crew.telegram.transport_dispatch\n"
        "print(sorted(m for m in sys.modules if m.startswith('kiro_crew.telegram.dispatch.')))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=True,
    )
    assert result.stdout.strip() == str(sorted(f"kiro_crew.telegram.dispatch.{s}" for s in OWNERS))


# ── the class surface ────────────────────────────────────────────────────────────


def test_every_base_member_keeps_its_kind_and_signature() -> None:
    members = vars(td.TelegramDispatcher)
    for name, (kind, signature) in BASE_MEMBERS.items():
        assert name in members, name
        assert _kind(members[name]) == kind, name
        assert _signature(members[name]) == signature, name


def test_the_class_gained_only_the_turn_engine() -> None:
    members = {n for n in vars(td.TelegramDispatcher) if not n.startswith("__")}
    assert members - set(BASE_MEMBERS) == ADDED_MEMBERS
    assert inspect.iscoroutinefunction(td.TelegramDispatcher._run_turn)


def test_a_moved_member_is_its_owners_function() -> None:
    members = vars(td.TelegramDispatcher)
    for stem, names in MOVED_MEMBERS.items():
        owner = _owner(stem)
        for name in names:
            bound = members[name]
            fn = bound.__func__ if isinstance(bound, staticmethod) else bound
            assert fn is getattr(owner, name), f"{stem}.{name}"
            assert fn.__module__ == owner.__name__, f"{stem}.{name}"


def test_coroutine_members_stay_coroutines() -> None:
    for name in BASE_MEMBERS:
        member = vars(td.TelegramDispatcher)[name]
        fn = member.__func__ if isinstance(member, staticmethod) else member
        if isinstance(fn, property):
            continue
        source = inspect.getsource(fn).lstrip()
        assert inspect.iscoroutinefunction(fn) == source.startswith("async def"), name


# ── patch reach ──────────────────────────────────────────────────────────────────


def _facade_aliases(tree: ast.Module) -> set[str]:
    """Names a test module binds to the facade module object."""
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == FACADE and alias.asname:
                    aliases.add(alias.asname)
        elif isinstance(node, ast.ImportFrom) and node.module == "kiro_crew.telegram":
            for alias in node.names:
                if alias.name == "transport_dispatch":
                    aliases.add(alias.asname or alias.name)
        elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            func = node.value.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            args = node.value.args
            if (
                name in {"import_module", "__import__"}
                and args
                and isinstance(args[0], ast.Constant)
                and args[0].value == FACADE
            ):
                aliases |= {t.id for t in node.targets if isinstance(t, ast.Name)}
    return aliases


def _patched_names_in(source: str) -> set[str]:
    """Names one test module rebinds on the facade, by any spelling."""
    tree = ast.parse(source)
    aliases = _facade_aliases(tree)
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            args = list(node.args)
            if name in {"setattr", "object", "delattr"} and len(args) >= 2:
                target, attr = args[0], args[1]
                if (
                    isinstance(target, ast.Name)
                    and target.id in aliases
                    and isinstance(attr, ast.Constant)
                    and isinstance(attr.value, str)
                ):
                    found.add(attr.value)
            for arg in args[:1] + [kw.value for kw in node.keywords if kw.arg == "target"]:
                if (
                    isinstance(arg, ast.Constant)
                    and isinstance(arg.value, str)
                    and arg.value.startswith(f"{FACADE}.")
                ):
                    found.add(arg.value[len(FACADE) + 1 :].split(".")[0])
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id in aliases
                ):
                    found.add(target.attr)
    return found


def _facade_patched_names() -> set[str]:
    """Every name any test rebinds on ``kiro_crew.telegram.transport_dispatch``."""
    found: set[str] = set()
    roots = [REPO / "test", REPO / "src" / "kiro_crew" / "apps" / "builtins"]
    for root in roots:
        for path in root.rglob("test_*.py"):
            text = path.read_text(encoding="utf-8", errors="replace")
            if "transport_dispatch" not in text:
                continue
            found |= _patched_names_in(text)
    return found


def test_the_patch_scan_reads_every_spelling() -> None:
    planted = (
        "import kiro_crew.telegram.transport_dispatch as tg\n"
        "from kiro_crew.telegram import transport_dispatch as td\n"
        "import importlib\n"
        "mod = importlib.import_module('kiro_crew.telegram.transport_dispatch')\n"
        "from unittest.mock import patch\n"
        "def test_x(monkeypatch):\n"
        "    monkeypatch.setattr(tg, 'alpha', 1)\n"
        "    patch.object(td, 'beta')\n"
        "    setattr(mod, 'gamma', 2)\n"
        "    td.delta = 3\n"
        "    patch('kiro_crew.telegram.transport_dispatch.epsilon')\n"
        "    patch(target='kiro_crew.telegram.transport_dispatch.zeta.inner')\n"
        "    monkeypatch.setattr(other, 'not_this', 1)\n"
        "    patch('kiro_crew.telegram.renderer.nor_this')\n"
    )
    assert _patched_names_in(planted) == {"alpha", "beta", "gamma", "delta", "epsilon", "zeta"}


def test_no_owner_binds_a_name_tests_rebind_on_the_facade() -> None:
    """An owner holding its own binding of a rebound name would keep calling the
    unpatched object, and the test patching the facade would pass while exercising
    nothing. So the moved code reads each one through the facade at call time."""
    patched = _facade_patched_names()
    # Non-vacuous: the scan sees the seams the dispatcher's own suites rebind.
    assert {
        "Stats",
        "TelegramApprovalDecider",
        "TelegramRenderer",
        "TurnDriver",
        "add_trusted_session",
        "channel_inbound_permitted",
        "cleanup_attachments",
        "list_agents",
        "publish_turn_identity",
        "sel",
        "synthesize_and_deliver",
        "unpressed_wait_answer",
    } <= patched
    for stem in OWNERS:
        bound = {name for name, value in vars(_owner(stem)).items() if not inspect.ismodule(value)}
        assert bound & patched == set(), stem


def _bare_reads(source: str, names: set[str]) -> list[str]:
    """``line: name`` for every bare Load of *names*, outside imports."""
    hits = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in names:
            hits.append(f"{node.lineno}: {node.id}")
    return hits


def test_an_owner_reads_a_rebound_name_through_the_facade() -> None:
    patched = _facade_patched_names()
    offenders = {
        stem: hits
        for stem, source in _owner_sources().items()
        if (hits := _bare_reads(source, patched))
    }
    assert offenders == {}


def test_the_bare_read_scan_can_fail() -> None:
    planted = "def f(self):\n    sel().log_api_access()\n    return facade.sel\n"
    assert _bare_reads(planted, {"sel"}) == ["2: sel"]


def test_every_facade_read_has_its_call_time_import() -> None:
    """``facade.<name>`` must be the function-local import of the facade module."""
    for stem, source in _owner_sources().items():
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            uses = any(
                isinstance(n, ast.Attribute)
                and isinstance(n.value, ast.Name)
                and n.value.id == "facade"
                for n in ast.walk(node)
            )
            if not uses:
                continue
            imports = [
                n
                for n in node.body
                if isinstance(n, ast.ImportFrom)
                and n.module == "kiro_crew.telegram"
                and [(a.name, a.asname) for a in n.names] == [("transport_dispatch", "facade")]
            ]
            assert imports, f"{stem}.{node.name} reads facade.* without importing it"


@pytest.mark.asyncio
async def test_a_facade_patch_reaches_each_moved_reader(monkeypatch: pytest.MonkeyPatch) -> None:
    """One call per rebound name an owner reads, each answered by the facade's stand-in."""
    reached: list[str] = []

    rows: list[dict[str, Any]] = []
    monkeypatch.setattr(
        td, "sel", lambda: SimpleNamespace(log_api_access=lambda **kw: rows.append(kw))
    )

    def _agents() -> list[Any]:
        reached.append("list_agents")
        return []

    monkeypatch.setattr(td, "list_agents", _agents)
    assert td.TelegramDispatcher._installed_agent_names() == []

    async def _speak(deliver: Any, text: str, **_settings: Any) -> bool:
        reached.append("synthesize_and_deliver")
        return False

    monkeypatch.setattr(td, "synthesize_and_deliver", _speak)
    d, cli, sess = _dispatcher({7})
    await d._speak_reply(("direct", "7"), 7, "x" * 60, None)

    async def _deny(channel: str) -> bool:
        reached.append("channel_inbound_permitted")
        return False

    monkeypatch.setattr(td, "channel_inbound_permitted", _deny)
    await d.on_callback(_press("m:0"))
    monkeypatch.setattr(td, "channel_inbound_permitted", AsyncMock(return_value=True))

    granted: list[Any] = []
    monkeypatch.setattr(td, "add_trusted_session", lambda key, sessions=None: granted.append(key))

    class _Decider:
        @staticmethod
        def key(session_key: str, rid: str) -> str:
            reached.append("TelegramApprovalDecider")
            return TelegramApprovalDecider.key(session_key, rid)

        @staticmethod
        def is_pending(key: str, nonce: str) -> bool:
            return True

        @staticmethod
        def resolve_global(key: str, approved: bool, nonce: str = "") -> bool:
            return True

    monkeypatch.setattr(td, "TelegramApprovalDecider", _Decider)
    await d.on_callback(_press("a:r1:n1:t"))
    assert granted == [d._session_key(("direct", "7"))]
    assert [r["operation"] for r in rows] == ["telegram.trust_session"]

    async def _answer(channel: str, rid: str) -> bool:
        reached.append("unpressed_wait_answer")
        return True

    monkeypatch.setattr(td, "TelegramApprovalDecider", TelegramApprovalDecider)
    monkeypatch.setattr(td, "unpressed_wait_answer", _answer)
    import kiro_crew.telegram.renderer as renderer_mod

    monkeypatch.setattr(renderer_mod, "_APPROVAL_TIMEOUT_S", 0.05)
    assert await d.deliver_spawn_approval("spawn:a", "d", "telegram:kirocrew:direct:7") is True
    TelegramApprovalDecider.retire(
        TelegramApprovalDecider.key("telegram:kirocrew:direct:7", "spawn:a")
    )

    assert reached == [
        "list_agents",
        "synthesize_and_deliver",
        "channel_inbound_permitted",
        "TelegramApprovalDecider",
        "unpressed_wait_answer",
    ]
    assert sess.successes == [] and cli.sent[-1][1] is not None


# ── owner placement ──────────────────────────────────────────────────────────────


def _module_level_imports(tree: ast.Module) -> list[ast.stmt]:
    """Import statements that run when the module loads (``TYPE_CHECKING`` excluded)."""
    out: list[ast.stmt] = []
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            out.append(node)
        elif isinstance(node, ast.If) and ast.unparse(node.test) != "TYPE_CHECKING":
            out.extend(n for n in node.body if isinstance(n, (ast.Import, ast.ImportFrom)))
    return out


def _imported_modules(nodes: list[ast.stmt]) -> list[str]:
    names = []
    for node in nodes:
        if isinstance(node, ast.Import):
            names += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names += [node.module] + [f"{node.module}.{a.name}" for a in node.names]
    return names


def test_no_owner_imports_the_facade_when_it_loads() -> None:
    for stem, source in _owner_sources().items():
        loaded = _imported_modules(_module_level_imports(ast.parse(source)))
        assert FACADE not in loaded, stem
        assert not any(m.startswith(f"{FACADE}.") for m in loaded), stem


def test_an_owner_reaches_a_sibling_through_its_module() -> None:
    for stem, source in _owner_sources().items():
        for node in _module_level_imports(ast.parse(source)):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                "kiro_crew.telegram.dispatch."
            ):
                pytest.fail(f"{stem} imports {node.module} names directly")


def test_the_owners_import_graph_is_acyclic() -> None:
    edges = {}
    for stem, source in _owner_sources().items():
        loaded = _imported_modules(_module_level_imports(ast.parse(source)))
        edges[stem] = {
            m.rsplit(".", 1)[-1]
            for m in loaded
            if m.startswith("kiro_crew.telegram.dispatch.") and m.rsplit(".", 1)[-1] in OWNERS
        }
    done: set[str] = set()

    def visit(stem: str, path: tuple[str, ...]) -> None:
        assert stem not in path, f"import cycle: {path + (stem,)}"
        for nxt in edges[stem]:
            if nxt not in done:
                visit(nxt, path + (stem,))
        done.add(stem)

    for stem in OWNERS:
        visit(stem, ())


def test_every_owner_logs_under_the_facades_name() -> None:
    for stem in OWNERS:
        logger = vars(_owner(stem)).get("logger")
        if logger is not None:
            assert logger is logging.getLogger(FACADE), stem


def test_the_package_initializer_imports_nothing() -> None:
    tree = ast.parse((DISPATCH_DIR / "__init__.py").read_text(encoding="utf-8"))
    assert [n for n in tree.body if not isinstance(n, ast.Expr)] == []


#: The constructs no owner may hold, each with the reason. All but the two in
#: ``_FORBIDDEN_IN_THE_FACADE_TOO`` stay in ``transport_dispatch.py``, either because a
#: repository guard locates them there by path or by ``inspect.getsource`` of that
#: module, or (where marked) as a placement rule that keeps the turn engine whole. Those
#: two are shapes a guard forbids in the facade, which it can only read there. A guard
#: that requires its construct in the facade fails by itself when the construct leaves;
#: a guard that forbids a shape goes blind when the shape lands in an owner instead, so
#: ``_facade_pinned_shapes`` makes both cases fail here, by name. Unguarded
#: ``renderer.close()`` is not listed: test_close_guard_parity scans the owners for it.
_FACADE_PINNED = {
    "a redactor call": "test_security_posture: the 'Telegram failure reason' sink",
    "begin_turn": "test_turn_ceiling: GATED composes the ceiling around begin_turn",
    "the turn ceiling": "test_turn_ceiling: GATED (turn_ceiling.gate, TurnCeilingExceeded)",
    "the muted renderer": "test_turn_ceiling: MUTE_AWARE (SilentRenderer, render_refusal)",
    "the hook gate splat": "test_hooks: helper_sites (hook_gate_kwargs)",
    "the turn's memory store": "test_memory_v2_isolation: the session_key store seam",
    "the breaker charge": "placement rule: charge_turn_failure ends the turn engine",
    "the tool gate": "placement rule: on_tool_call belongs to the turn engine",
    "the auto-title pin": "test_messaging_auto_title: pin_record, try_claim, is_titled",
    "a name-grant check": "test_name_grant_surfaces: name_grant",
    "the forgeable spawn title": 'test_name_grant_surfaces: title == "spawn_run"',
    "the reservation sweep": "test_early_approval_press_13578: discard_session",
    "a receipt flip": "test_queue_receipt: every flip call names its owner",
    "the queue drain": "test_queue_drain_wake: async def _drain_queue",
    "an agent-SDK import": "check_agent_sdk_boundary: kiro_crew.acp / kiro_crew.providers",
}
_FORBIDDEN_IN_THE_FACADE_TOO = frozenset({"a name-grant check", "the forgeable spawn title"})


def _facade_pinned_shapes(source: str) -> list[str]:
    """The ``_FACADE_PINNED`` labels whose construct *source* holds."""
    from test_queue_receipt import _FLIP_CALLEES
    from test_security_posture import _REDACTOR_CALL_RE

    tree = ast.parse(source)
    called = {
        n.func.attr if isinstance(n.func, ast.Attribute) else n.func.id
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, (ast.Attribute, ast.Name))
    }
    named = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | {
        n.id for n in ast.walk(tree) if isinstance(n, ast.Name)
    }
    dotted = {ast.unparse(n) for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    imported = _imported_modules(
        [n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]
    )
    held = {
        "a redactor call": bool(_REDACTOR_CALL_RE.search(source)),
        "begin_turn": "begin_turn" in called,
        "the turn ceiling": "turn_ceiling.gate" in dotted or "TurnCeilingExceeded" in named,
        "the muted renderer": bool({"SilentRenderer", "render_refusal"} & named),
        "the hook gate splat": "hook_gate_kwargs" in named,
        "the turn's memory store": "session_store_for_turn" in named,
        "the breaker charge": "charge_turn_failure" in named,
        "the tool gate": "on_tool_call" in called,
        "the auto-title pin": bool({"pin_record", "try_claim", "is_titled"} & named),
        "a name-grant check": "name_grant" in source,
        "the forgeable spawn title": 'title == "spawn_run"' in source,
        "the reservation sweep": "discard_session" in named,
        "a receipt flip": bool(_FLIP_CALLEES & called),
        "the queue drain": "_drain_queue"
        in {n.name for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)},
        "an agent-SDK import": any(
            m.startswith(("kiro_crew.acp", "kiro_crew.providers")) for m in imported
        ),
    }
    assert held.keys() == _FACADE_PINNED.keys()
    return [label for label, hit in held.items() if hit]


def test_no_owner_holds_what_the_facade_guards_read_by_path() -> None:
    """No owner holds a construct listed in ``_FACADE_PINNED``.

    Those are: a redactor call, ``begin_turn``, the turn ceiling, the muted renderer,
    the hook gate splat, the turn's memory store, the breaker charge, the tool gate,
    the auto-title pin, a name-grant check, the forgeable spawn title, the reservation
    sweep, a receipt flip, the queue drain and an agent-SDK import, for the reason each
    entry records. The facade holds exactly the ones that stay there, which also shows
    the scan recognises each construct as the dispatcher really spells it.
    """
    held = {stem: _facade_pinned_shapes(source) for stem, source in _owner_sources().items()}
    assert not {stem: labels for stem, labels in held.items() if labels}
    assert not (DISPATCH_DIR / "transport_dispatch.py").exists()
    in_facade = set(_facade_pinned_shapes(FACADE_PATH.read_text(encoding="utf-8")))
    assert in_facade == _FACADE_PINNED.keys() - _FORBIDDEN_IN_THE_FACADE_TOO


#: One planted construct per ``_FACADE_PINNED`` label, for the scan's self-test.
_PLANTED = {
    "a redactor call": "def f(text):\n    return redact(text)\n",
    "begin_turn": "def f(self, key):\n    self.sessions.begin_turn(key)\n",
    "the turn ceiling": (
        "def f(turn_ceiling, key):\n    with turn_ceiling.gate(key):\n        pass\n"
    ),
    "the muted renderer": "def f(renderer):\n    return SilentRenderer(renderer)\n",
    "the hook gate splat": "def f(gate, event):\n    gate(**hook_gate_kwargs(event))\n",
    "the turn's memory store": "def f(key):\n    return session_store_for_turn(key)\n",
    "the breaker charge": "async def f(s, key):\n    await charge_turn_failure(s, key)\n",
    "the tool gate": "def f(self, call):\n    self.hooks.on_tool_call(call)\n",
    "the auto-title pin": ("def f(auto_title, log, key):\n    auto_title.pin_record(log, key)\n"),
    "a name-grant check": "def f(cmd):\n    from kiro_crew import name_grant\n",
    "the forgeable spawn title": 'def f(title):\n    return title == "spawn_run"\n',
    "the reservation sweep": "def f(loop, seam, key):\n    loop.call_soon(seam.discard_session, key)\n",
    "a receipt flip": 'async def f(self, key):\n    await self._receipt_flip_locked(key, owner="")\n',
    "the queue drain": "async def _drain_queue(self, key):\n    pass\n",
    "an agent-SDK import": "import kiro_crew.acp\n",
}


@pytest.mark.parametrize("label", sorted(_FACADE_PINNED))
def test_the_placement_scan_finds_each_construct_it_pins(label: str) -> None:
    assert _facade_pinned_shapes(_PLANTED[label]) == [label]


def test_the_placement_scan_passes_the_constructs_an_owner_may_hold() -> None:
    allowed = (
        "async def f(self, renderer, key):\n"
        "    try:\n"
        "        await renderer.close()\n"
        "    finally:\n"
        "        await self._run_turn(key, title='x')\n"
        "    return self._enqueue_with_receipt(key, owner=key)\n"
    )
    assert _facade_pinned_shapes(allowed) == []
    assert _PLANTED.keys() == _FACADE_PINNED.keys()


def test_dashboard_imports_stay_call_time() -> None:
    """The dashboard imports the channel packages on its boot path, so a channel
    module reaches ``kiro_crew.dashboard`` only inside the function that needs it."""
    sources = {"transport_dispatch": FACADE_PATH.read_text(encoding="utf-8"), **_owner_sources()}
    for stem, source in sources.items():
        loaded = _imported_modules(_module_level_imports(ast.parse(source)))
        assert not [m for m in loaded if m.startswith("kiro_crew.dashboard")], stem
    lazy = re.findall(
        r"^ +from kiro_crew\.dashboard\.[\w.]+ import", _owner_sources()["commands"], re.M
    )
    assert len(lazy) == 2, "the /kirocrew dashboard handler's two call-time imports"


def test_the_seam_import_comment_tells_the_reader_why() -> None:
    source = FACADE_PATH.read_text(encoding="utf-8")
    assert "``# noqa: F401`` marks an import an owner reads through this" in source


def test_a_turn_that_runs_reaches_the_turn_engine_once() -> None:
    d, _cli, sess = _dispatcher({7})
    calls: list[str] = []
    real = d._run_turn

    async def _count(msg: Any, text: str, **kw: Any) -> None:
        calls.append(text)
        await real(msg, text, **kw)

    d._run_turn = _count  # type: ignore[method-assign]
    asyncio.run(d.handle_message(_dm("hello")))
    asyncio.run(d.handle_message(_dm("/ping")))
    assert calls == ["hello"]
    assert sess.successes == ["telegram:kirocrew:direct:7"]
