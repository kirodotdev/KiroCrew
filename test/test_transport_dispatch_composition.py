"""Composition contract between ``DiscordDispatcher`` and its ``transport_dispatch_*`` owners.

``kiro_crew.discord.transport_dispatch`` keeps the dispatcher's state, its front door and
the constructs repository guards read in that file, and binds the functions of the owner
modules beside it (``kiro_crew.discord.transport_dispatch_<role>``) as class attributes.
What this file pins is what a caller of the dispatcher observes independently of where
each rule lives:

* the module keeps every name it bound at the base the split was cut from, a moved name
  by identity with its owner's; ``from ... import *`` binds the same public names; and
  the mirror serves exactly the moved names the facade does not bind itself;
* the class keeps every member it had, with the same kind, coroutine-ness and
  signature, and a member an owner defines is that owner's function itself;
* no bound function needs a class body: no zero-argument ``super()``, ``__class__`` or
  name-mangled attribute;
* a patch applied to the facade reaches the moved code reading it, shown by calling each
  owner that reads one of the four names the tests rebind there;
* the owners follow the placement rules: imports between owners in one direction, one
  logger, eager loading, call-time dashboard imports, a docstring naming the instance
  fields each owner reads and changes, every coroutine in the sync-io gate's reach, and
  none of the constructs ``_FACADE_PINNED`` keeps in the facade;
* the "Discord direct send" redaction sink names the owner that holds ``!compact``'s
  redaction pass.

``test/test_facade_mirror_guards.py`` holds this facade, like every facade that installs
the mirror, to the mirror's own contract: every name the test trees patch through it
reaches every owner binding it, and no owner imports it at run time.
"""

from __future__ import annotations

import ast
import enum
import importlib
import importlib.util
import inspect
import logging
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from test_discord import _dispatcher, _drop_live_config_snapshot  # noqa: F401

import kiro_crew.discord.transport_dispatch as td
from kiro_crew import _facade_mirror
from kiro_crew.discord.client import DiscordInteraction

pytestmark = pytest.mark.xdist_group(name="tree_scan_discord_transport_dispatch")

FACADE = "kiro_crew.discord.transport_dispatch"
FACADE_PATH = Path(td.__file__).resolve()
PACKAGE_DIR = FACADE_PATH.parent
REPO = FACADE_PATH.parents[3]

#: The owners the dispatcher is composed from, lowest layer first. Spelled out rather
#: than globbed, so a module added or removed is a deliberate change to the composition.
OWNERS = (
    "origin",
    "model_picker",
    "commands",
    "interactions",
    "midturn",
    "spawn_approval",
    "ledger",
)

#: Every name the facade bound at the base the split was cut from, private ones
#: included: tests and callers import several of them, and a name that silently stops
#: resolving breaks a caller nobody can see from here. A name an import in the facade
#: gained since is listed here too, since it is not one of the composition's own.
BASE_NAMES = frozenset(
    {
        "APPROVAL_INTERACTIVE",
        "ATTACHMENT_PLACEHOLDER",
        "Any",
        "Approvals",
        "Asker",
        "CHAT_TYPE_DIRECT",
        "ChannelAgentPicks",
        "ChannelLink",
        "ChannelTurns",
        "ConversationOwnershipConflict",
        "ConversationState",
        "DENY_CAUSE_APPROVAL_TIMEOUT",
        "DISCORD_CAPABILITIES",
        "DISCORD_DRIFT",
        "DiscordApprovalDecider",
        "DiscordDispatcher",
        "DiscordRenderer",
        "DiscordSessionResume",
        "InboundMessage",
        "InboundRoute",
        "IngestLimits",
        "KiroCrewConfig",
        "MonitorCompletionHook",
        "MonitorDispatchResult",
        "MonitorWake",
        "NamedTuple",
        "ReceiptQueue",
        "ReceiptSurface",
        "ResumeReleaseError",
        "RoutingDecision",
        "SimpleNamespace",
        "Stats",
        "TYPE_CHECKING",
        "TurnOutcome",
        "TurnRecord",
        "Verdict",
        "_BUSY_OPTIONS_REFUSAL",
        "_CHANNEL",
        "_CHAT_TYPE_THREAD",
        "_DEFAULT_KIROCREW_AGENT",
        "_DETACH_EXEMPT_COMMANDS",
        "_MAX_COLLAPSE",
        "_MAX_COLLAPSED_ATTACHMENTS",
        "_MODEL_PICKER_LIMIT",
        "_MODEL_PICKER_MAX",
        "_MODEL_PICKER_TTL_SECS",
        "_ModelPicker",
        "_NOT_A_SENDER",
        "_ORIGIN_PREFIX",
        "_QueuedOrigin",
        "_RELEASE_FAILURE",
        "_REPLY_COMMANDS",
        "_RESUMED_BUSY_REFUSAL",
        "_STALE_OPTIONS_REFUSAL",
        "_STEER_ACK_EMOJI",
        "_STYLE_DANGER",
        "_STYLE_SUCCESS",
        "_UNTAGGED_OPTIONS_REFUSAL",
        "_clamp_pct",
        "_coerce_snowflakes",
        "_drains_after",
        "_entry_owner",
        "_inbound_origin",
        "_origin_kwargs",
        "_queued_origin",
        "admit_inbound_callback",
        "annotations",
        "append_attachment_context",
        "asynccontextmanager",
        "asyncio",
        "build_dm_session_key",
        "build_help_text",
        "build_model_components",
        "cast",
        "channel_inbound_permitted",
        "channel_outbound_permitted",
        "cleanup_attachments",
        "compact_unsupported_backend",
        "compact_unsupported_reply",
        "context_recycle_warning",
        "dataclass",
        "describe_grant_lifetime",
        "drain_until_quiet",
        "drain_when_free",
        "entry_channel",
        "entry_person_origin",
        "handle_channel_agent_command",
        "is_bare_mid_turn_override",
        "live",
        "logger",
        "logging",
        "mint_row_mid",
        "owner_token",
        "parse_command",
        "parse_command_argument",
        "parse_mid_turn_override",
        "parse_session_key",
        "person_priority",
        "person_tag",
        "process_discord_attachments",
        "rebind_conversation_location",
        "receipt_address_key",
        "recycle_backend",
        "recycle_warning_should_send",
        "redact",
        "redact_credentials",
        "redact_exfiltration_urls",
        "redact_for_display",
        "redact_local_paths",
        "refused_resume_is_restricted",
        "register_drain",
        "release_conversation_location",
        "reserve_new_generation",
        "safety_override",
        "seed_generation",
        "sel",
        "session_is_restricted",
        "session_provenance_tag",
        "stop_running_turn",
        "tag_entry",
        "time",
        "unknown_command_usage",
        "unpressed_wait_answer",
        "uploads_restricted",
    }
)

#: Module-level names that moved, and the owner that now defines each.
MOVED_NAMES = {
    "origin": (
        "_CHANNEL",
        "_CHAT_TYPE_THREAD",
        "_NOT_A_SENDER",
        "_ORIGIN_PREFIX",
        "_QueuedOrigin",
        "_entry_owner",
        "_inbound_origin",
        "_origin_kwargs",
        "_queued_origin",
    ),
    "model_picker": (
        "_MODEL_PICKER_LIMIT",
        "_MODEL_PICKER_MAX",
        "_MODEL_PICKER_TTL_SECS",
        "_ModelPicker",
    ),
    "commands": ("_RELEASE_FAILURE", "_REPLY_COMMANDS"),
    "interactions": ("_UNTAGGED_OPTIONS_REFUSAL",),
    "midturn": ("_RESUMED_BUSY_REFUSAL",),
}

#: The moved names the facade's own code does not read, so it serves them from their
#: owner through the mirror rather than importing them back.
SERVED = frozenset(
    {
        "_MODEL_PICKER_LIMIT",
        "_MODEL_PICKER_MAX",
        "_MODEL_PICKER_TTL_SECS",
        "_NOT_A_SENDER",
        "_ORIGIN_PREFIX",
        "_RESUMED_BUSY_REFUSAL",
        "_UNTAGGED_OPTIONS_REFUSAL",
        "_inbound_origin",
    }
)

#: The names the composition added to the facade: one handle per owner whose members the
#: class binds, the mirror and its install.
ADDED_NAMES = frozenset(
    {
        "_EXPORTS",
        "_OWNERS",
        "_SERVED",
        "_commands",
        "_facade_mirror",
        "_importlib",
        "_interactions",
        "_ledger",
        "_midturn",
        "_model_picker",
        "_spawn_approval",
        "_sys",
    }
)

#: Dispatcher members an owner defines, by owner. Each is bound on the class as the
#: owner's own function.
MOVED_MEMBERS = {
    "model_picker": ("_apply_model", "_handle_model", "_model_choices", "_prune_model_pickers"),
    "commands": (
        "_channel_reply",
        "_handle_compact",
        "_handle_link",
        "_handle_status",
        "_handle_unlink",
        "_run_reply_command",
    ),
    "interactions": (
        "_interaction_actionable",
        "_interaction_reply",
        "_on_command_interaction",
        "on_interaction",
    ),
    "midturn": ("_handle_busy", "_handle_resumed_busy"),
    "spawn_approval": (
        "_spawn_approval_actionable",
        "_spawn_chat_target",
        "_spawn_prompt_channel_permitted",
        "_spawn_prompt_destination_permitted",
        "deliver_spawn_approval",
    ),
    "ledger": (
        "_persist_turn",
        "_record_turn",
        "_session_restricted",
        "_uploads_restricted",
    ),
}

#: Every member the class had at the base: (kind, coroutine, signature as ``inspect``
#: renders it). A moved function annotates ``self`` for the type checker; that one
#: annotation is the only difference the comparison removes.
BASE_MEMBERS: dict[str, tuple[str, bool, str]] = {
    "__init__": (
        "function",
        False,
        (
            "(self, *, sessions: \"'SessionManager'\", ctx_builder: \"'ContextBuilder'\", cfg: "
            "\"'KiroCrewConfig'\", allowed_user_ids: 'set[str]', allowed_thread_ids: 'set[str] "
            "| None' = None, agent: 'str | None' = None, conv_log: \"'ConversationLog | "
            "None'\" = None, approval_mode: 'str' = 'interactive') -> 'None'"
        ),
    ),
    "_apply_model": (
        "function",
        True,
        ("(self, scope_id: 'str', session_key: 'str', model_id: 'str') -> 'str'"),
    ),
    "_authorized": ("function", False, "(self, user_id: 'str') -> 'bool'"),
    "_channel_reply": ("function", False, "(self, channel_id: 'str') -> \"'ReplyFn'\""),
    "_channel_turns": ("function", False, "(self) -> 'ChannelTurns'"),
    "_drain_queue": ("function", True, "(self, session_key: 'str') -> 'None'"),
    "_enqueue_with_receipt": (
        "function",
        True,
        (
            "(self, session_key: 'str', channel_id: 'str', text: 'str', *, attachments: "
            "'list[Any] | None' = None, origin: '_QueuedOrigin', person_origin: 'bool' = "
            "False) -> 'bool'"
        ),
    ),
    "_handle_busy": (
        "function",
        True,
        (
            "(self, session_key: 'str', msg: 'InboundMessage', text: 'str', override_mode: "
            "'str | None') -> 'None'"
        ),
    ),
    "_handle_compact": (
        "function",
        True,
        (
            "(self, user_id: 'str', channel_id: 'str', thread_id: 'str', resumed_key: 'str | "
            "None') -> 'None'"
        ),
    ),
    "_handle_link": (
        "function",
        True,
        (
            "(self, user_id: 'str', channel_id: 'str', thread_id: 'str', resumed_key: 'str | "
            "None') -> 'None'"
        ),
    ),
    "_handle_model": (
        "function",
        True,
        ("(self, channel_id: 'str', scope_id: 'str', session_key: 'str', arg: 'str') -> " "'None'"),
    ),
    "_handle_resumed_busy": (
        "function",
        True,
        (
            "(self, session_key: 'str', msg: 'InboundMessage', text: 'str', override_mode: "
            "'str | None') -> 'None'"
        ),
    ),
    "_handle_status": (
        "function",
        True,
        ("(self, reply: \"'ReplyFn'\", scope_id: 'str | None' = None) -> 'None'"),
    ),
    "_handle_stop": (
        "function",
        True,
        (
            "(self, user_id: 'str', channel_id: 'str', thread_id: 'str', resumed_key: 'str | "
            "None') -> 'None'"
        ),
    ),
    "_handle_unlink": (
        "function",
        True,
        ("(self, user_id: 'str', channel_id: 'str', thread_id: 'str' = '') -> 'None'"),
    ),
    "_inbound_session_key": (
        "function",
        False,
        ("(self, user_id: 'str', channel_id: 'str', thread_id: 'str' = '') -> 'str'"),
    ),
    "_interaction_actionable": (
        "function",
        True,
        ("(self, user_id: 'str', thread_id: 'str') -> 'bool'"),
    ),
    "_interaction_reply": (
        "function",
        False,
        ("(self, itx: \"'DiscordInteraction'\") -> \"'ReplyFn'\""),
    ),
    "_live_cfg": ("function", False, "(self) -> \"'KiroCrewConfig'\""),
    "_maybe_notice": (
        "function",
        True,
        (
            "(self, channel_id: 'str', scope_id: 'str', session_key: 'str', provider: 'Any') "
            "-> 'None'"
        ),
    ),
    "_model_choices": (
        "function",
        False,
        ("(self, session_key: 'str') -> 'tuple[tuple[str, str], ...]'"),
    ),
    "_on_command_interaction": (
        "function",
        True,
        ("(self, itx: \"'DiscordInteraction'\") -> 'None'"),
    ),
    "_origin_mirror_link": ("function", False, "(self, channel_id: 'str') -> 'ChannelLink'"),
    "_persist_turn": (
        "function",
        False,
        (
            "(self, session_key: 'str', user_text: 'str', reply_text: 'str', is_new: 'bool', "
            "agent: 'str | None' = None, mirror_mids: 'tuple[str, str] | None' = None, "
            "extra_row: 'tuple[str, str, str, str | None] | None' = None) -> 'None'"
        ),
    ),
    "_prune_model_pickers": ("function", False, "(self, now: 'float') -> 'None'"),
    "_pump_queue": (
        "function",
        True,
        ("(self, session_key: 'str', foreign_channels: 'set[str]') -> 'None'"),
    ),
    "_receipt_flip_locked": (
        "function",
        True,
        (
            "(self, session_key: 'str', channel_id: 'str', answered: 'list[str]', deferred: "
            "'int' = 0, *, owner: 'str') -> 'None'"
        ),
    ),
    "_receipt_surface": ("function", False, "(self, channel_id: 'str') -> 'ReceiptSurface'"),
    "_record_turn": ("function", True, "(self, record: 'TurnRecord') -> 'None'"),
    "_render_config": ("function", False, "(self) -> 'tuple[bool, bool]'"),
    "_resolve_agent": ("function", False, "(self, scope_id: 'str | None' = None) -> 'str'"),
    "_routing_turn": (
        "function",
        False,
        ("(self, channel_id: 'str') -> \"'AsyncIterator[list[int]]'\""),
    ),
    "_run_reply_command": (
        "function",
        True,
        (
            "(self, cmd: 'str', reply: \"'ReplyFn'\", *, user_id: 'str', thread_id: 'str', "
            "text: 'str') -> 'None'"
        ),
    ),
    "_scope_id": ("staticmethod", False, "(user_id: 'str', thread_id: 'str' = '') -> 'str'"),
    "_seed_gen": ("function", False, "(self, scope_id: 'str') -> 'int'"),
    "_session_key": ("function", False, "(self, user_id: 'str', thread_id: 'str' = '') -> 'str'"),
    "_session_restricted": ("function", True, "(self, session_key: 'str') -> 'bool'"),
    "_soft_threshold": ("function", False, "(self) -> 'int'"),
    "_spawn_approval_actionable": (
        "function",
        True,
        ("(self, channel_id: 'str', thread_id: 'str', user_id: 'str') -> 'bool'"),
    ),
    "_spawn_chat_target": (
        "function",
        False,
        ("(self, parent_session_key: 'str') -> 'tuple[str, str, str, str] | None'"),
    ),
    "_spawn_prompt_channel_permitted": ("function", True, "(self, request_id: 'str') -> 'bool'"),
    "_spawn_prompt_destination_permitted": (
        "function",
        False,
        ("(self, channel_id: 'str', thread_id: 'str', user_id: 'str') -> 'bool'"),
    ),
    "_surface_own_session": ("function", True, "(self) -> 'None'"),
    "_uploads_restricted": ("function", True, "(self, session_key: 'str') -> 'bool'"),
    "current_session_key": ("function", False, "(self, user_id: 'str') -> 'str'"),
    "deliver_spawn_approval": (
        "function",
        True,
        (
            "(self, request_id: 'str', description: 'str', parent_session_key: 'str') -> "
            "'bool | None'"
        ),
    ),
    "handle_message": (
        "function",
        True,
        (
            "(self, msg: 'InboundMessage', *, drain: 'bool' = True, interpret_commands: "
            "'bool' = True, origin_tag: 'str' = '', monitor_completion: "
            "'MonitorCompletionHook | None' = None, monitor_session_key: 'str | None' = "
            "None) -> 'MonitorDispatchResult | None'"
        ),
    ),
    "is_authorized": ("function", False, "(self, user_id: 'str') -> 'bool'"),
    "on_interaction": ("function", True, "(self, itx: \"'DiscordInteraction'\") -> 'None'"),
    "reconfigure": ("function", False, "(self, section: 'Any') -> 'None'"),
    "register_allowed_thread": ("function", False, "(self, thread_id: 'str') -> 'None'"),
}


def _owner(stem: str) -> ModuleType:
    return importlib.import_module(f"{FACADE}_{stem}")


def _owner_path(stem: str) -> Path:
    return PACKAGE_DIR / f"transport_dispatch_{stem}.py"


def _owner_sources() -> dict[str, str]:
    return {stem: _owner_path(stem).read_text(encoding="utf-8") for stem in OWNERS}


def _unwrap(member: Any) -> Any:
    return member.__func__ if isinstance(member, staticmethod) else member


def _kind(member: Any) -> str:
    if isinstance(member, staticmethod):
        return "staticmethod"
    return "function" if inspect.isfunction(member) else type(member).__name__


def _signature(member: Any) -> str:
    return str(inspect.signature(_unwrap(member))).replace("self: 'DiscordDispatcher'", "self", 1)


# ── the module surface ───────────────────────────────────────────────────────────


def test_every_base_name_still_resolves() -> None:
    assert sorted(name for name in BASE_NAMES if not hasattr(td, name)) == []


def test_a_moved_name_is_its_owners_object() -> None:
    for stem, names in MOVED_NAMES.items():
        owner = _owner(stem)
        for name in names:
            assert getattr(td, name) is vars(owner)[name], f"{stem}.{name}"


def test_a_moved_class_keeps_its_owner_module_and_is_no_error_or_enum() -> None:
    """Nothing reads these classes' module: no repr, log line, pickle or error text
    names it. An exception or an enum would show its module to a user, and none moved."""
    assert td._QueuedOrigin.__module__ == f"{FACADE}_origin"
    assert td._ModelPicker.__module__ == f"{FACADE}_model_picker"
    for stem, names in MOVED_NAMES.items():
        for name in names:
            value = getattr(td, name)
            if isinstance(value, type):
                assert not issubclass(value, (BaseException, enum.Enum)), f"{stem}.{name}"


def test_the_facade_added_only_its_owner_handles_and_the_mirror() -> None:
    bound = {name for name in vars(td) if not (name.startswith("__") and name.endswith("__"))}
    assert bound - BASE_NAMES == ADDED_NAMES
    assert BASE_NAMES - bound == SERVED
    for stem in OWNERS:
        handle = vars(td).get(f"_{stem}")
        if handle is not None:
            assert handle is _owner(stem), stem
    assert {f"_{stem}" for stem, members in MOVED_MEMBERS.items() if members} <= ADDED_NAMES


def test_the_mirror_serves_exactly_the_moved_names_the_facade_does_not_bind() -> None:
    index = vars(td)[_facade_mirror.INDEX_ATTRIBUTE]
    assert type(td) is _facade_mirror.MirroredModule
    assert set(index.served) == SERVED
    assert set(td._SERVED) == SERVED
    assert td._EXPORTS == dict(index.served)
    assert index.excluded == ()
    for name in SERVED:
        stem = next(stem for stem, names in MOVED_NAMES.items() if name in names)
        assert index.served[name] == f"{FACADE}_{stem}", name


def test_a_star_import_binds_the_base_public_names(tmp_path: Path) -> None:
    public = sorted(name for name in BASE_NAMES if not name.startswith("_"))
    assert td.__all__ == public
    probe = tmp_path / "discord_dispatch_star_probe.py"
    probe.write_text(f"from {FACADE} import *  # noqa: F401,F403\n", encoding="utf-8")
    spec = importlib.util.spec_from_file_location("discord_dispatch_star_probe", probe)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert sorted(name for name in vars(module) if not name.startswith("__")) == public


def test_the_owner_list_is_the_layer_order_and_names_every_owner_on_disk() -> None:
    assert td._OWNERS == tuple(f"{FACADE}_{stem}" for stem in OWNERS)
    on_disk = sorted(p.stem for p in PACKAGE_DIR.glob("transport_dispatch_*.py"))
    assert on_disk == sorted(f"transport_dispatch_{stem}" for stem in OWNERS)
    # A package spelled like the facade would also match the queue-drain guard's
    # ``rglob("transport_dispatch.py")``.
    assert not list(PACKAGE_DIR.glob("transport_dispatch_*/"))


def test_a_fresh_import_loads_every_owner() -> None:
    probe = (
        "import sys, kiro_crew.discord.transport_dispatch\n"
        "print(sorted(m for m in sys.modules if m.startswith("
        "'kiro_crew.discord.transport_dispatch_')))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=True,
    )
    assert result.stdout.strip() == str(sorted(f"{FACADE}_{stem}" for stem in OWNERS))


# ── the class surface ────────────────────────────────────────────────────────────


def test_every_base_member_keeps_its_kind_coroutine_and_signature() -> None:
    members = vars(td.DiscordDispatcher)
    for name, (kind, coroutine, signature) in BASE_MEMBERS.items():
        assert name in members, name
        member = members[name]
        assert _kind(member) == kind, name
        assert inspect.iscoroutinefunction(_unwrap(member)) is coroutine, name
        assert _signature(member) == signature, name


def test_the_class_gained_no_member() -> None:
    members = {name for name in vars(td.DiscordDispatcher) if not name.startswith("__")}
    assert members == set(BASE_MEMBERS) - {"__init__"}


def test_a_moved_member_is_its_owners_function() -> None:
    members = vars(td.DiscordDispatcher)
    for stem, names in MOVED_MEMBERS.items():
        owner = _owner(stem)
        for name in names:
            assert members[name] is vars(owner)[name], f"{stem}.{name}"
            assert members[name].__module__ == owner.__name__, f"{stem}.{name}"


def _self_functions(source: str) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [
        node
        for node in ast.parse(source).body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.args.args
        and node.args.args[0].arg == "self"
    ]


def test_every_owner_function_taking_self_is_a_bound_member() -> None:
    for stem, source in _owner_sources().items():
        assert sorted(fn.name for fn in _self_functions(source)) == sorted(
            MOVED_MEMBERS.get(stem, ())
        ), stem


def _class_only_constructs(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    """What breaks when a method body leaves its class: ``super()`` with no arguments, the
    ``__class__`` cell, and a name-mangled ``__attr`` (``__x`` without a trailing ``__``)."""
    found: list[str] = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == "super" and not node.args:
                found.append("super()")
        if isinstance(node, ast.Name) and node.id == "__class__":
            found.append("__class__")
        spelled = node.attr if isinstance(node, ast.Attribute) else getattr(node, "id", None)
        if isinstance(spelled, str) and spelled.startswith("__") and not spelled.endswith("__"):
            found.append(spelled)
    return found


def test_the_class_only_scan_can_fail() -> None:
    tree = ast.parse("def f(self):\n    super().g()\n    return __class__, self.__hidden\n")
    assert sorted(_class_only_constructs(tree.body[0])) == ["__class__", "__hidden", "super()"]


@pytest.mark.parametrize("stem", OWNERS)
def test_no_bound_function_needs_its_class_body(stem: str) -> None:
    functions = _self_functions(_owner_sources()[stem])
    assert {fn.name: _class_only_constructs(fn) for fn in functions} == {
        fn.name: [] for fn in functions
    }


# ── patch reach ──────────────────────────────────────────────────────────────────


def _command(name: str) -> DiscordInteraction:
    return DiscordInteraction(
        interaction_id="i1",
        interaction_token="tok",
        channel_id="c1",
        user_id="u1",
        message_id="",
        kind=2,
        command_name=name,
    )


def _press(custom_id: str) -> DiscordInteraction:
    return DiscordInteraction(
        interaction_id="i2",
        interaction_token="tok",
        channel_id="c1",
        user_id="u1",
        message_id="m1",
        custom_id=custom_id,
    )


@pytest.mark.asyncio
async def test_a_facade_patch_reaches_each_moved_reader(monkeypatch: pytest.MonkeyPatch) -> None:
    """One call per rebound name an owner reads, each answered by the facade's stand-in."""
    reached: list[str] = []
    d, cli, _sess = _dispatcher({"u1"})

    async def _deny(channel: str) -> bool:
        reached.append("channel_inbound_permitted")
        return False

    monkeypatch.setattr(td, "channel_inbound_permitted", _deny)
    await d.on_interaction(_command("status"))
    assert cli.responses and "disabled by policy" in cli.responses[-1][1]
    assert await d._spawn_prompt_channel_permitted("spawn:a") is False

    async def _permit(channel: str) -> bool:
        return True

    async def _withhold(channel: str) -> bool:
        reached.append("channel_outbound_permitted")
        return False

    monkeypatch.setattr(td, "channel_inbound_permitted", _permit)
    monkeypatch.setattr(td, "channel_outbound_permitted", _withhold)
    await d.on_interaction(_press("a:r1:n1:1"))
    assert cli.edits == []

    def _active() -> Any:
        reached.append("safety_override")
        return SimpleNamespace(is_active=lambda: True)

    def _lifetime() -> str:
        reached.append("describe_grant_lifetime")
        return "for 30 minutes"

    monkeypatch.setattr(td, "safety_override", _active)
    monkeypatch.setattr(td, "describe_grant_lifetime", _lifetime)
    replies: list[str] = []

    async def _reply(text: str) -> None:
        replies.append(text)

    await d._handle_status(_reply)
    assert replies and "YOLO ON (for 30 minutes)" in replies[-1]

    assert reached == [
        "channel_inbound_permitted",
        "channel_inbound_permitted",
        "channel_outbound_permitted",
        "safety_override",
        "describe_grant_lifetime",
    ]


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


def test_the_import_scan_reads_what_runs_at_load_and_nothing_under_type_checking() -> None:
    planted = (
        f"from {FACADE} import a\n"
        "import kiro_crew.dashboard.state\n"
        "if TYPE_CHECKING:\n"
        "    from kiro_crew.discord import transport_dispatch_origin\n"
    )
    loaded = _imported_modules(_module_level_imports(ast.parse(planted)))
    assert FACADE in loaded and "kiro_crew.dashboard.state" in loaded
    assert "kiro_crew.discord.transport_dispatch_origin" not in loaded


def test_an_owner_imports_only_owners_below_it() -> None:
    """The owners import each other in one direction, so the graph has no cycle and the
    first holder of a name is the layer that defines it."""
    for position, stem in enumerate(OWNERS):
        loaded = _imported_modules(_module_level_imports(ast.parse(_owner_sources()[stem])))
        siblings = {
            m.split(".")[2].removeprefix("transport_dispatch_")
            for m in loaded
            if m.startswith(f"{FACADE}_")
        }
        assert siblings <= set(OWNERS[:position]), (stem, siblings)


def test_every_owner_logs_under_the_facades_name() -> None:
    loggers = {
        stem: value
        for stem in OWNERS
        for value in vars(_owner(stem)).values()
        if isinstance(value, logging.Logger)
    }
    assert set(loggers) == {"commands", "interactions", "midturn", "model_picker", "spawn_approval"}
    assert {logger.name for logger in loggers.values()} == {FACADE}
    assert td.logger is logging.getLogger(FACADE)


def test_dashboard_imports_stay_call_time() -> None:
    """The dashboard imports the channel packages on its boot path, so a channel
    module reaches ``kiro_crew.dashboard`` only inside the function that needs it."""
    sources = {"transport_dispatch": FACADE_PATH.read_text(encoding="utf-8"), **_owner_sources()}
    for stem, source in sources.items():
        loaded = _imported_modules(_module_level_imports(ast.parse(source)))
        assert not [m for m in loaded if m.startswith("kiro_crew.dashboard")], stem


def _instance_fields() -> set[str]:
    """The dispatcher's instance fields: every ``self.<name>`` its methods assign."""
    fields: set[str] = set()
    sources = [FACADE_PATH.read_text(encoding="utf-8"), *_owner_sources().values()]
    for source in sources:
        for node in ast.walk(ast.parse(source)):
            targets: list[ast.expr] = []
            if isinstance(node, ast.Assign):
                targets = list(node.targets)
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
            for target in targets:
                if (
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "self"
                ):
                    fields.add(target.attr)
    return fields


def _fields_touched(source: str, fields: set[str]) -> set[str]:
    return {
        node.attr
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
        and node.attr in fields
    }


def test_each_owner_docstring_names_the_instance_fields_it_touches() -> None:
    fields = _instance_fields()
    assert {"client", "sessions", "_model_pickers", "dashboard_state"} <= fields
    for stem, source in _owner_sources().items():
        doc = ast.get_docstring(ast.parse(source)) or ""
        touched = _fields_touched(source, fields)
        # Non-vacuous: every owner holding a member touches the dispatcher's state.
        assert bool(touched) == bool(MOVED_MEMBERS.get(stem)), stem
        missing = sorted(f for f in touched if f"``{f}``" not in doc)
        assert missing == [], (stem, missing)


#: Module names ``scripts/check_sync_io_in_async.py`` resolves, and only from an
#: ``import <module>`` in the same file: an owner that took one through another module's
#: import would drop its coroutines out of that gate.
_SYNC_IO_MODULES = frozenset({"time", "os", "subprocess", "socket", "requests", "httpx"})


def _sync_io_gate() -> ModuleType:
    """``scripts/check_sync_io_in_async.py``, loaded by path (``scripts/`` is no package)."""
    path = REPO / "scripts" / "check_sync_io_in_async.py"
    spec = importlib.util.spec_from_file_location("_discord_dispatch_sync_io_gate", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _from_imported_sync_io_modules(source: str) -> set[str]:
    return {
        alias.asname or alias.name
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
        if (alias.asname or alias.name) in _SYNC_IO_MODULES
    }


def test_no_owner_takes_a_sync_io_module_through_a_from_import() -> None:
    assert _SYNC_IO_MODULES <= set(_sync_io_gate().MODULE_FUNCS)
    blind = {
        stem: sorted(found)
        for stem, source in _owner_sources().items()
        if (found := _from_imported_sync_io_modules(source))
    }
    assert blind == {}


def _with_blocking_call(source: str, call: str) -> tuple[str, int]:
    """*source* with *call* as the first statement of its first top-level coroutine,
    and the call's line."""
    coroutine = next(n for n in ast.parse(source).body if isinstance(n, ast.AsyncFunctionDef))
    first = coroutine.body[0]
    lines = source.splitlines(keepends=True)
    lines.insert(first.lineno - 1, " " * first.col_offset + call + "\n")
    return "".join(lines), first.lineno


def _coroutine_owners() -> list[str]:
    return [
        stem
        for stem, source in _owner_sources().items()
        if any(isinstance(n, ast.AsyncFunctionDef) for n in ast.parse(source).body)
    ]


@pytest.mark.parametrize("stem", [s for s in OWNERS if s != "origin"])
def test_the_sync_io_gate_reports_a_blocking_call_planted_in_each_coroutine_owner(
    stem: str,
) -> None:
    """Every owner's coroutines are in the gate's reach: a blocking call planted in its
    first one, beside the ``import time`` the rule above requires, is reported."""
    assert stem in _coroutine_owners()
    planted, line = _with_blocking_call(_owner_sources()[stem], "time.sleep(1)")
    planted += "\nimport time\n"
    reported = {
        found for found, _family, _provenance in _sync_io_gate()._violations_in_source(planted)
    }
    assert line in reported


def test_the_gate_reach_check_tells_a_blind_spelling_from_a_seen_one() -> None:
    blind = "from kiro_crew.discord.transport_dispatch import time\n\n\nasync def f():\n    pass\n"
    assert _from_imported_sync_io_modules(blind) == {"time"}
    planted, line = _with_blocking_call(blind, "time.sleep(1)")
    assert line not in {f for f, _fam, _prov in _sync_io_gate()._violations_in_source(planted)}
    assert _coroutine_owners() == [s for s in OWNERS if s != "origin"]


#: The constructs no owner may hold, each with the guard that reads it in
#: ``transport_dispatch.py`` by path or by the module's text. The ones in
#: ``_FORBIDDEN_IN_THE_FACADE_TOO`` are shapes a guard forbids in the facade as well: a
#: guard that requires its construct in the facade fails by itself when the construct
#: leaves, while a guard that forbids a shape goes blind when the shape lands in an owner
#: instead, so ``_facade_pinned_shapes`` makes both cases fail here, by name.
_FACADE_PINNED = {
    "the queue drain": "test_queue_drain_wake: async def _drain_queue in transport_dispatch.py",
    "a receipt flip": "test_queue_receipt: every flip call names its owner",
    "the receipt address key": "test_queue_receipt: ReceiptSurface beside receipt_address_key(",
    "the stop handler": "test_stop_clears_only_its_own_sender: async def _handle_stop",
    "the turn pipeline": "test_session_control_owner_dm, test_channel_turn_structure",
    "the start-priority asker": "test_start_priority: one Asker( in transport_dispatch.py",
    "the inbound admission": "test_update_check_install_aware: admission before parsing",
    "the context-usage read": "test_reinjection_gate: a compacting module also delegates",
    "a name-grant check": "test_name_grant_surfaces: name_grant",
    "the forgeable spawn title": 'test_name_grant_surfaces: title == "spawn_run"',
    "an agent-SDK import": "check_agent_sdk_boundary: kiro_crew.acp / kiro_crew.providers",
}
_FORBIDDEN_IN_THE_FACADE_TOO = frozenset(
    {"a name-grant check", "the forgeable spawn title", "an agent-SDK import"}
)


def _facade_pinned_shapes(source: str) -> list[str]:
    """The ``_FACADE_PINNED`` labels whose construct *source* holds."""
    from test_queue_receipt import _FLIP_CALLEES

    tree = ast.parse(source)
    called = {
        n.func.attr if isinstance(n.func, ast.Attribute) else n.func.id
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, (ast.Attribute, ast.Name))
    }
    coroutines = {n.name for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)}
    imported = _imported_modules(
        [n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]
    )
    held = {
        "the queue drain": "_drain_queue" in coroutines,
        "a receipt flip": bool(_FLIP_CALLEES & called),
        "the receipt address key": "receipt_address_key" in called,
        "the stop handler": "_handle_stop" in coroutines,
        "the turn pipeline": "ChannelTurns" in called,
        "the start-priority asker": "Asker" in called,
        "the inbound admission": "admit_inbound_callback" in called,
        "the context-usage read": bool({"check_context_usage", "compact_if_needed"} & called),
        "a name-grant check": "name_grant" in source,
        "the forgeable spawn title": 'title == "spawn_run"' in source,
        "an agent-SDK import": any(
            m == root or m.startswith(f"{root}.")
            for m in imported
            for root in ("kiro_crew.acp", "kiro_crew.providers")
        ),
    }
    assert held.keys() == _FACADE_PINNED.keys()
    return [label for label, hit in held.items() if hit]


def test_no_owner_holds_what_the_facade_guards_read_by_path() -> None:
    held = {stem: _facade_pinned_shapes(source) for stem, source in _owner_sources().items()}
    assert not {stem: labels for stem, labels in held.items() if labels}
    in_facade = set(_facade_pinned_shapes(FACADE_PATH.read_text(encoding="utf-8")))
    assert in_facade == _FACADE_PINNED.keys() - _FORBIDDEN_IN_THE_FACADE_TOO


#: One planted construct per ``_FACADE_PINNED`` label, for the scan's self-test.
_PLANTED = {
    "the queue drain": "async def _drain_queue(self, key):\n    pass\n",
    "a receipt flip": 'async def f(self, key):\n    await self._receipt_flip_locked(key, owner="")\n',
    "the receipt address key": 'def f(c):\n    return receipt_address_key("discord", c)\n',
    "the stop handler": "async def _handle_stop(self, u, c, t, k):\n    pass\n",
    "the turn pipeline": "def f(s):\n    return ChannelTurns('discord', sessions=s)\n",
    "the start-priority asker": "def f(k, c):\n    return Asker(k, c)\n",
    "the inbound admission": "async def f(s):\n    await admit_inbound_callback(s)\n",
    "the context-usage read": "def f(self, k, p):\n    return self.sessions.check_context_usage(k, p)\n",
    "a name-grant check": "def f(cmd):\n    from kiro_crew import name_grant\n",
    "the forgeable spawn title": 'def f(title):\n    return title == "spawn_run"\n',
    "an agent-SDK import": "import kiro_crew.acp\n",
}


@pytest.mark.parametrize("label", sorted(_FACADE_PINNED))
def test_the_placement_scan_finds_each_construct_it_pins(label: str) -> None:
    assert _facade_pinned_shapes(_PLANTED[label]) == [label]


def test_the_placement_scan_passes_the_constructs_an_owner_may_hold() -> None:
    allowed = (
        "async def f(self, key, msg):\n"
        "    if not await self._enqueue_with_receipt(key, 'c', 't', origin=None):\n"
        "        await self.handle_message(msg)\n"
        "    self.client.remember_dm_recipient('c', 'u')\n"
    )
    assert _facade_pinned_shapes(allowed) == []
    assert _PLANTED.keys() == _FACADE_PINNED.keys()


def test_the_direct_send_sink_names_the_owner_that_redacts_compact() -> None:
    """``security_posture`` registers "Discord direct send" at the module holding
    ``_handle_compact``'s redaction pass, and that pass is the composed one that runs
    both scanners, so the posture panel names where the redaction happens and its
    checks read that code."""
    from kiro_crew import security_posture

    sinks = {label: module for label, module, _detail in security_posture._REDACTION_SINKS}
    assert sinks["Discord direct send"] == "discord/transport_dispatch_commands.py"
    compact = inspect.getsource(_owner("commands")._handle_compact)
    assert "redact(" in compact
