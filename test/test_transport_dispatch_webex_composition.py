"""Composition contract between ``WebexDispatcher`` and its ``transport_dispatch_*`` owners.

``kiro_crew.webex.transport_dispatch`` keeps the dispatcher's state, its front door and
the constructs repository guards read in that file, and binds the functions of the owner
modules beside it (``kiro_crew.webex.transport_dispatch_<role>``) as class attributes.
What this file pins is what a caller of the dispatcher observes independently of where
each rule lives:

* the module keeps resolving every name it bound at the base the split was cut from, a
  moved name by identity with its owner's; a star import still binds the base's public
  names; and the mirror serves exactly the moved names the facade does not bind itself;
* the class keeps every member it had, and a member an owner defines is that owner's
  function itself, with the base's kind, coroutine-ness and signature;
* no bound function needs a class body: no zero-argument ``super()``, ``__class__`` or
  name-mangled attribute;
* a patch applied to the facade reaches the moved code reading it, shown by calling the
  owners that read the two names the tests rebind there;
* the owners follow the placement rules: imports between owners in one direction, one
  logger name, eager loading, call-time dashboard imports, a docstring naming the instance
  fields each owner reads and changes, every coroutine in the sync-io gate's reach, none
  of the constructs ``_FACADE_PINNED`` keeps in the facade, and no module outside the
  family importing an owner.

The pins hold the base's names and members as a floor, never as the whole namespace: a
later change that adds a name or a member to the dispatcher is not this split's to
refuse. ``test/test_facade_mirror_guards.py`` holds this facade, like every facade that
installs the mirror, to the mirror's own contract: every name the test trees patch
through it reaches every owner binding it, and no owner imports it at run time.
"""

from __future__ import annotations

import ast
import enum
import importlib
import importlib.util
import inspect
import logging
import sys
from collections.abc import Iterable
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest import mock

import pytest
import source_corpus
from webex_dispatch_helpers import (
    FakeClient,
    FakeCtx,
    FakeProvider,
    FakeSessions,
    _dispatcher,
    _inbound,
)

import kiro_crew.webex.transport_dispatch as td
from kiro_crew import _facade_mirror
from kiro_crew.messaging.queue_drain import reset_drains

pytestmark = pytest.mark.xdist_group(name="tree_scan_webex_transport_dispatch")

FACADE = "kiro_crew.webex.transport_dispatch"
FACADE_PATH = Path(td.__file__).resolve()
PACKAGE_DIR = FACADE_PATH.parent
SRC = FACADE_PATH.parents[1]
REPO = FACADE_PATH.parents[3]

#: The owners the dispatcher is composed from, lowest layer first. Spelled out rather
#: than globbed, so a module added or removed is a deliberate change to the composition.
OWNERS = ("origin", "interactions", "commands")

#: Every name the facade bound at the base the split was cut from, private ones
#: included: tests and callers import several of them, and a name that silently stops
#: resolving breaks a caller nobody can see from here. The modules the base bound with
#: ``import <module>`` (``asyncio``, ``logging``, ``time``) are not part of its surface.
BASE_NAMES = frozenset(
    {
        "APPROVAL_INTERACTIVE",
        "Any",
        "CHAT_TYPE_DIRECT",
        "CHAT_TYPE_FORUM",
        "ChannelAgentPicks",
        "ChannelLink",
        "ChannelTurn",
        "ConversationState",
        "DM_SCOPE_PER_CHANNEL_PEER",
        "InboundRoute",
        "LiveChoices",
        "NamedTuple",
        "PendingApprovals",
        "ROOM_DIRECT",
        "ReceiptQueue",
        "ReceiptSurface",
        "STOP_DECLINED_COMPACTING_TEXT",
        "SessionApprovalDecider",
        "TYPE_CHECKING",
        "UNBIND_REASON_ORIGIN_REBIND",
        "WEBEX_CAPABILITIES",
        "WebexDispatcher",
        "WebexInbound",
        "WebexRenderer",
        "_APPROVALS",
        "_APPROVAL_NOT_PENDING",
        "_APPROVE_REPLIES",
        "_CHANNEL",
        "_COMPACT_FAILURE_TYPES",
        "_COMPACT_TIMEOUT_S",
        "_DEFAULT_KIROCREW_AGENT",
        "_DEFAULT_TTL_SECS",
        "_DENY_REPLIES",
        "_MAX_COLLAPSE",
        "_ORIGIN_PREFIX",
        "_QUEUED_ATTACHMENT_LABEL",
        "_QueuedPlace",
        "_ROOT_THREAD",
        "_SESSIONS_LIST_MAX",
        "_SPACE_ROUTE_PREFIX",
        "_chat_type_of",
        "_entry_owner",
        "_format_ttl",
        "_normalize_threshold_pair",
        "_queued_place",
        "_reply_envelope",
        "_route_of",
        "_ttl_spec",
        "admit_inbound_callback",
        "annotations",
        "append_attachment_context",
        "build_directive_consumer",
        "build_dm_session_key",
        "build_help_text",
        "cards",
        "cast",
        "cleanup_attachments",
        "compact_unsupported_backend",
        "compact_unsupported_reply",
        "compaction_in_flight",
        "consume_stop_declined",
        "context_recycle_warning",
        "decline_stop",
        "describe_grant_lifetime",
        "drain_until_quiet",
        "drive_turn",
        "entries_queued_by",
        "entry_channel",
        "entry_person_origin",
        "force_stop_keeping_others",
        "handle_channel_agent_command",
        "inbound_permitted",
        "is_bare_mid_turn_override",
        "is_unknown_command",
        "legacy_dashboard_mirror_key",
        "live",
        "logger",
        "mint_row_mid",
        "note_user_stop",
        "owner_token",
        "parse_command",
        "parse_command_argument",
        "parse_mid_turn_override",
        "person_priority",
        "person_tag",
        "process_webex_attachments",
        "read_press",
        "receipt_address_key",
        "recycle_backend",
        "recycle_warning_should_send",
        "register_drain",
        "release_conversation_location",
        "replace",
        "reserve_new_generation",
        "resolve_pre_turn",
        "safety_override",
        "seed_generation",
        "sel",
        "sign_mirror_admission",
        "strip_bot_mention",
        "tag_entry",
        "transcript_stem",
        "webex_display_safe",
    }
)

#: Module-level names that moved, and the owner that now defines each.
MOVED_NAMES = {
    "origin": (
        "_CHANNEL",
        "_ORIGIN_PREFIX",
        "_QueuedPlace",
        "_ROOT_THREAD",
        "_SPACE_ROUTE_PREFIX",
        "_chat_type_of",
        "_entry_owner",
        "_queued_place",
        "_reply_envelope",
        "_route_of",
    ),
    "interactions": (
        "_APPROVALS",
        "_APPROVAL_NOT_PENDING",
        "_APPROVE_REPLIES",
        "_DENY_REPLIES",
    ),
    "commands": ("_DEFAULT_TTL_SECS", "_SESSIONS_LIST_MAX", "_format_ttl", "_ttl_spec"),
}

#: The moved names the facade's own code does not read, so it serves them from their
#: owner through the mirror rather than importing them back.
SERVED = frozenset(
    {
        "_APPROVAL_NOT_PENDING",
        "_APPROVE_REPLIES",
        "_DEFAULT_TTL_SECS",
        "_DENY_REPLIES",
        "_ORIGIN_PREFIX",
        "_SESSIONS_LIST_MAX",
        "_SPACE_ROUTE_PREFIX",
        "_format_ttl",
        "_ttl_spec",
    }
)

#: The names the composition added to the facade: one handle per owner whose members the
#: class binds, the mirror and its install, and the base imports kept bound for importers.
ADDED_NAMES = frozenset(
    {
        "_BOUND_FOR_IMPORTERS",
        "_EXPORTS",
        "_OWNERS",
        "_SERVED",
        "_commands",
        "_facade_mirror",
        "_importlib",
        "_interactions",
        "_sys",
    }
)

#: Every member the class had at the base.
BASE_MEMBERS = frozenset(
    {
        "__init__",
        "_apply_model",
        "_approval_key",
        "_bot_name",
        "_bucket_stem",
        "_compact_provider",
        "_drain_queue",
        "_enqueue_with_receipt",
        "_handle_busy",
        "_handle_card_press",
        "_handle_compact",
        "_handle_dashboard",
        "_handle_link",
        "_handle_model",
        "_handle_sessions",
        "_handle_stop",
        "_handle_unlink",
        "_handle_yolo",
        "_live_cfg",
        "_maybe_answer_approval",
        "_maybe_notice",
        "_model_choices",
        "_model_label",
        "_origin_mirror_link",
        "_persist_turn",
        "_pump_queue",
        "_receipt_surface",
        "_reply",
        "_reply_parent",
        "_resolve_agent",
        "_resolve_card_approval",
        "_seed_gen",
        "_sender_allowed",
        "_session_key",
        "_thresholds",
        "current_session_key",
        "handle_message",
    }
)

#: Dispatcher members an owner defines, by owner, each with its base (kind, coroutine,
#: signature as ``inspect`` renders it). A moved function annotates ``self`` for the type
#: checker; that one annotation is the only difference the comparison removes.
MOVED_MEMBERS: dict[str, dict[str, tuple[str, bool, str]]] = {
    "interactions": {
        "_maybe_answer_approval": (
            "function",
            True,
            "(self, inbound: \"'WebexInbound'\", session_key: 'str', *, permitted: 'bool') "
            "-> 'bool'",
        ),
        "_handle_card_press": (
            "function",
            True,
            "(self, inbound: \"'WebexInbound'\", *, permitted: 'bool') -> 'None'",
        ),
        "_resolve_card_approval": (
            "function",
            True,
            "(self, inbound: \"'WebexInbound'\", approved: 'bool', nonce: 'str', request_id: "
            "'str') -> 'None'",
        ),
    },
    "commands": {
        "_model_choices": (
            "function",
            False,
            "(self, session_key: 'str') -> 'list[tuple[str, str]]'",
        ),
        "_handle_model": ("function", True, "(self, inbound: \"'WebexInbound'\") -> 'None'"),
        "_apply_model": (
            "function",
            True,
            "(self, inbound: \"'WebexInbound'\", model_id: 'str', label: 'str') -> 'str'",
        ),
        "_model_label": (
            "staticmethod",
            False,
            "(choices: 'list[tuple[str, str]]', model_id: 'str') -> 'str'",
        ),
        "_bucket_stem": ("function", False, "(self, route: 'str') -> 'str'"),
        "_handle_sessions": ("function", True, "(self, inbound: \"'WebexInbound'\") -> 'None'"),
        "_handle_yolo": ("function", True, "(self, inbound: \"'WebexInbound'\") -> 'None'"),
        "_handle_dashboard": ("function", True, "(self, inbound: \"'WebexInbound'\") -> 'None'"),
    },
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
    return str(inspect.signature(_unwrap(member))).replace("self: 'WebexDispatcher'", "self", 1)


# ── the module surface ───────────────────────────────────────────────────────────


def test_every_base_name_still_resolves() -> None:
    assert sorted(name for name in BASE_NAMES if not hasattr(td, name)) == []


def test_a_moved_name_is_its_owners_object() -> None:
    for stem, names in MOVED_NAMES.items():
        owner = _owner(stem)
        for name in names:
            assert getattr(td, name) is vars(owner)[name], f"{stem}.{name}"


def test_a_moved_class_keeps_its_owner_module_and_is_no_error_or_enum() -> None:
    """Nothing reads this class's module: no repr, log line, pickle or error text names
    it. An exception or an enum would show its module to a user, and none moved."""
    assert td._QueuedPlace.__module__ == f"{FACADE}_origin"
    for stem, names in MOVED_NAMES.items():
        for name in names:
            value = getattr(td, name)
            if isinstance(value, type):
                assert not issubclass(value, (BaseException, enum.Enum)), f"{stem}.{name}"


def test_the_facade_binds_its_owner_handles_and_the_mirror() -> None:
    bound = {name for name in vars(td) if not (name.startswith("__") and name.endswith("__"))}
    assert ADDED_NAMES <= bound
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
    public = {name for name in BASE_NAMES if not name.startswith("_")}
    assert public <= set(td.__all__)
    assert [name for name in td.__all__ if name.startswith("_")] == []
    probe = tmp_path / "webex_dispatch_star_probe.py"
    probe.write_text(f"from {FACADE} import *  # noqa: F401,F403\n", encoding="utf-8")
    spec = importlib.util.spec_from_file_location("webex_dispatch_star_probe", probe)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert public <= {name for name in vars(module) if not name.startswith("__")}


def test_the_owner_list_is_the_layer_order_and_names_every_owner_on_disk() -> None:
    assert td._OWNERS == tuple(f"{FACADE}_{stem}" for stem in OWNERS)
    on_disk = sorted(p.stem for p in PACKAGE_DIR.glob("transport_dispatch_*.py"))
    assert on_disk == sorted(f"transport_dispatch_{stem}" for stem in OWNERS)
    # A package spelled like the facade would also match the queue-drain guard's
    # ``rglob("transport_dispatch.py")``.
    assert not list(PACKAGE_DIR.glob("transport_dispatch_*/"))


def test_the_facade_loads_every_owner_when_it_loads() -> None:
    """The owners are imported by the facade's own body, not on first use: a lazily
    loaded owner would leave the mirror's index short of its names."""
    assert all(name in sys.modules for name in td._OWNERS)
    tree = ast.parse(FACADE_PATH.read_text(encoding="utf-8"))
    loops = [
        node
        for node in tree.body
        if isinstance(node, ast.For)
        and ast.unparse(node.iter) == "_OWNERS"
        and "_importlib.import_module(_owner)" in ast.unparse(node)
    ]
    assert len(loops) == 1


# ── the class surface ────────────────────────────────────────────────────────────


def test_every_base_member_is_still_a_member() -> None:
    members = vars(td.WebexDispatcher)
    assert sorted(BASE_MEMBERS - set(members)) == []


def test_a_moved_member_keeps_its_base_kind_coroutine_and_signature() -> None:
    members = vars(td.WebexDispatcher)
    for stem, moved in MOVED_MEMBERS.items():
        for name, (kind, coroutine, signature) in moved.items():
            member = members[name]
            assert _kind(member) == kind, name
            assert inspect.iscoroutinefunction(_unwrap(member)) is coroutine, name
            assert _signature(member) == signature, name


def test_a_moved_member_is_its_owners_function() -> None:
    members = vars(td.WebexDispatcher)
    for stem, moved in MOVED_MEMBERS.items():
        owner = _owner(stem)
        for name in moved:
            function = _unwrap(members[name])
            assert function is vars(owner)[name], f"{stem}.{name}"
            assert function.__module__ == owner.__name__, f"{stem}.{name}"


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
        bound = {
            name for name, spec in MOVED_MEMBERS.get(stem, {}).items() if spec[0] == "function"
        }
        assert sorted(fn.name for fn in _self_functions(source)) == sorted(bound), stem


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
def test_no_moved_function_needs_its_class_body(stem: str) -> None:
    moved = MOVED_MEMBERS.get(stem, {})
    functions = [
        node
        for node in ast.parse(_owner_sources()[stem]).body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in moved
    ]
    assert len(functions) == len(moved), stem
    assert {fn.name: _class_only_constructs(fn) for fn in functions} == {
        fn.name: [] for fn in functions
    }


# ── patch reach ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_facade_patch_reaches_each_moved_reader() -> None:
    """One call per owner reading a name the tests rebind on the facade, each answered by
    the facade's stand-in: ``safety_override`` and ``sel`` in the ``/yolo`` handler, and
    ``sel`` again in the typed-reply approval intercept."""
    reached: list[str] = []
    # A dispatcher registers its drain process-wide; the one built here must not outlive
    # the test as the channel's wake target.
    reset_drains()
    d = _dispatcher(FakeSessions(FakeProvider([])), FakeCtx(), FakeClient())

    def _grant() -> Any:
        reached.append("safety_override")
        return SimpleNamespace(is_active=lambda: False, deactivate=lambda source: None)

    def _audit() -> Any:
        reached.append("sel")
        return mock.MagicMock()

    try:
        with mock.patch.object(td, "safety_override", _grant), mock.patch.object(td, "sel", _audit):
            await d._handle_yolo(_inbound("/yolo off"))
            assert await d._maybe_answer_approval(_inbound("2"), "k", permitted=True) is True
    finally:
        reset_drains()

    assert reached == ["safety_override", "sel", "sel"]
    assert "YOLO OFF" in d.client.sent[0][1]


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
        "    from kiro_crew.webex import transport_dispatch_origin\n"
    )
    loaded = _imported_modules(_module_level_imports(ast.parse(planted)))
    assert FACADE in loaded and "kiro_crew.dashboard.state" in loaded
    assert "kiro_crew.webex.transport_dispatch_origin" not in loaded


def test_an_owner_imports_only_owners_below_it() -> None:
    """The owners import each other in one direction, so the graph has no cycle and the
    first holder of a name is the layer that defines it."""
    seen_below = set()
    for position, stem in enumerate(OWNERS):
        loaded = _imported_modules(_module_level_imports(ast.parse(_owner_sources()[stem])))
        siblings = {
            m.split(".")[2].removeprefix("transport_dispatch_")
            for m in loaded
            if m.startswith(f"{FACADE}_")
        }
        assert siblings <= set(OWNERS[:position]), (stem, siblings)
        seen_below |= siblings
    # Non-vacuous: the member-holding owners read the route spellings below them.
    assert "origin" in seen_below


def test_every_owner_logs_under_the_facades_name() -> None:
    loggers = {
        stem: value
        for stem in OWNERS
        for value in vars(_owner(stem)).values()
        if isinstance(value, logging.Logger)
    }
    assert {"interactions", "commands"} <= set(loggers)
    assert {logger.name for logger in loggers.values()} == {FACADE}
    assert td.logger is logging.getLogger(FACADE)


def test_dashboard_imports_stay_call_time() -> None:
    """The dashboard imports the channel packages on its boot path, so a channel
    module reaches ``kiro_crew.dashboard`` only inside the function that needs it."""
    sources = {"transport_dispatch": FACADE_PATH.read_text(encoding="utf-8"), **_owner_sources()}
    for stem, source in sources.items():
        loaded = _imported_modules(_module_level_imports(ast.parse(source)))
        assert not [m for m in loaded if m.startswith("kiro_crew.dashboard")], stem
    # Non-vacuous: the dashboard-link handler does import the dashboard, inside its body.
    assert "kiro_crew.dashboard.token_auth" in inspect.getsource(
        _owner("commands")._handle_dashboard
    )


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
    assert {"client", "sessions", "cfg", "conv_log", "_choices", "_model_pref"} <= fields
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
    spec = importlib.util.spec_from_file_location("_webex_dispatch_sync_io_gate", path)
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
    blind = "from kiro_crew.webex.transport_dispatch import time\n\n\nasync def f():\n    pass\n"
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
    "the shared receipt queue": "test_queue_receipt: ReceiptQueue beside _enqueue_with_receipt",
    "the receipt address key": "test_queue_receipt: ReceiptSurface beside receipt_address_key(",
    "the stop handler": "test_stop_clears_only_its_own_sender: async def _handle_stop",
    "the inbound admission": (
        "test_update_check_install_aware_background_work: admission before parsing"
    ),
    "the pre-turn sequence": "test_messaging_pre_turn: resolve_pre_turn( and clear_awaiting(",
    "the start-priority turn": "test_start_priority: one ChannelTurn( in transport_dispatch.py",
    "the mirror admission": "test_channel_transport_outbound_authz: sign_mirror_admission(",
    "the context-usage read": "test_reinjection_gate: check_context_usage beside drive_turn(",
    "a hand-rolled rotation": "test_messaging_pre_turn: no maybe_rotate( in transport_dispatch.py",
    "an agent-SDK import": "check_agent_sdk_boundary: kiro_crew.acp / kiro_crew.providers",
}
_FORBIDDEN_IN_THE_FACADE_TOO = frozenset({"a hand-rolled rotation", "an agent-SDK import"})


def _facade_pinned_shapes(source: str) -> list[str]:
    """The ``_FACADE_PINNED`` labels whose construct *source* holds."""
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
        "the shared receipt queue": "ReceiptQueue" in called,
        "the receipt address key": "receipt_address_key" in called,
        "the stop handler": "_handle_stop" in coroutines,
        "the inbound admission": "admit_inbound_callback" in called,
        "the pre-turn sequence": bool({"resolve_pre_turn", "clear_awaiting"} & called),
        "the start-priority turn": "ChannelTurn" in called,
        "the mirror admission": "sign_mirror_admission" in called,
        "the context-usage read": "check_context_usage" in called,
        "a hand-rolled rotation": "maybe_rotate" in called,
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
    "the shared receipt queue": "def f(self):\n    self._queue = ReceiptQueue()\n",
    "the receipt address key": 'def f(r, t):\n    return receipt_address_key("webex", r, t)\n',
    "the stop handler": "async def _handle_stop(self, inbound):\n    pass\n",
    "the inbound admission": "async def f(s):\n    await admit_inbound_callback(s)\n",
    "the pre-turn sequence": "async def f(c):\n    await resolve_pre_turn(conv=c)\n",
    "the start-priority turn": "def f(p):\n    return ChannelTurn(start_priority=p)\n",
    "the mirror admission": "def f(k, link):\n    return sign_mirror_admission(k, link)\n",
    "the context-usage read": (
        "def f(self, k, p):\n    return self.sessions.check_context_usage(k, p)\n"
    ),
    "a hand-rolled rotation": "def f(c, k):\n    return c.maybe_rotate(k)\n",
    "an agent-SDK import": "import kiro_crew.acp\n",
}


@pytest.mark.parametrize("label", sorted(_FACADE_PINNED))
def test_the_placement_scan_finds_each_construct_it_pins(label: str) -> None:
    assert _facade_pinned_shapes(_PLANTED[label]) == [label]


def test_the_placement_scan_passes_the_constructs_an_owner_may_hold() -> None:
    allowed = (
        "async def f(self, inbound):\n"
        "    route = _route_of(inbound)\n"
        "    await self.handle_message(inbound, interpret_commands=False)\n"
        "    await self._reply(inbound, self._approval_key(route))\n"
    )
    assert _facade_pinned_shapes(allowed) == []
    assert _PLANTED.keys() == _FACADE_PINNED.keys()


# ── who imports an owner ─────────────────────────────────────────────────────────


def _imported_by(node: ast.Import | ast.ImportFrom, module: str) -> list[str]:
    """Dotted names an import statement in *module* reaches, relative spellings resolved."""
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    base = node.module or ""
    if node.level:
        package = module.split(".")[: -node.level]
        base = ".".join([*package, *([node.module] if node.module else [])])
    return [base, *(f"{base}.{alias.name}" for alias in node.names)]


def _owner_importers(sources: Iterable[tuple[Path, str]], root: Path) -> set[str]:
    """The files among *sources* (relative to *root*) whose code imports a family owner."""
    found = set()
    for path, text in sources:
        rel = path.relative_to(root)
        module = ".".join([root.name, *rel.with_suffix("").parts])
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, (ast.Import, ast.ImportFrom)) and any(
                name.startswith(f"{FACADE}_") for name in _imported_by(node, module)
            ):
                found.add(rel.as_posix())
    return found


def test_only_the_family_imports_an_owner() -> None:
    family = {"webex/transport_dispatch.py", *(f"webex/transport_dispatch_{s}.py" for s in OWNERS)}
    sources = source_corpus.iter_candidate_sources(require_all=("transport_dispatch_",))
    importers = _owner_importers(
        ((path, text) for path, text in sources if path.is_relative_to(SRC)), SRC
    )
    # Non-vacuous: the owners import the owner below them.
    assert "webex/transport_dispatch_commands.py" in importers
    assert importers - family == set()


def test_the_owner_import_scan_finds_an_outside_importer(tmp_path: Path) -> None:
    root = tmp_path / "kiro_crew"
    sources = {
        root / "pkg" / "caller.py": f"from {FACADE}_commands import _ttl_spec\n",
        root / "other.py": "from kiro_crew.webex import transport_dispatch_origin as low\n",
        root / "webex" / "relative.py": "from .transport_dispatch_origin import _route_of\n",
        root / "webex" / "deep" / "up.py": "from .. import transport_dispatch_interactions\n",
        root / "facade_user.py": f"from {FACADE} import WebexDispatcher\n",
    }
    assert _owner_importers(sources.items(), root) == {
        "pkg/caller.py",
        "other.py",
        "webex/relative.py",
        "webex/deep/up.py",
    }
