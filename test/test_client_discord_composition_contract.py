"""``DiscordClient`` is one class composed from owner modules; these tests pin how.

``kiro_crew.discord.client`` keeps the client's state and lifecycle, the REST verbs and
the bodies they send, the attachment download, the DM pairing cache and the thread check.
An owner function whose first parameter is ``self`` is bound as the class attribute of the
same name, and every other moved name is served from its owner through
``kiro_crew._facade_mirror``. Pinned here: the owner set and its layer order, that each
moved member is its owner's own function with its base kind and signature, the rules a
function bound from outside the class body needs, the logger name the rate-limit and
Gateway tests capture, and the records that moved whole. The layout is described in
``docs/system-specs/modules/messaging.md`` (Discord's renderer and client composition).
"""

from __future__ import annotations

import ast
import importlib
import inspect
import logging
import os
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import source_corpus

import kiro_crew
from kiro_crew.discord import client as FACADE_MODULE
from kiro_crew.subprocess_utf8 import UTF8_TEXT

FACADE = "kiro_crew.discord.client"
STEM = "client"
FACADE_PATH = Path(FACADE_MODULE.__file__).resolve()
DISCORD_DIR = FACADE_PATH.parent
SRC = Path(kiro_crew.__file__).resolve().parent
CLS = FACADE_MODULE.DiscordClient
OWNER_PREFIX = "kiro_crew.discord.client_"
#: An owner the scan must find importing a lower owner, so it cannot pass vacuously.
FAMILY_IMPORTER = "discord/client_recheck.py"

# Walks src/kiro_crew for owner imports; keep it on one worker.
pytestmark = pytest.mark.xdist_group(name="tree_scan_client_discord_composition_contract")

#: Owner modules, lowest layer first: an owner imports only owners listed before it.
OWNERS = (
    "client_rest",
    "client_recheck",
    "client_gateway",
)

#: ``DiscordClient`` members an owner defines, by owner, each bound as the class attribute.
MOVED_MEMBERS = {
    "client_rest": (
        "_ensure_session",
        "_hold_key",
        "_set_hold",
        "_note_headers",
        "_await_capacity",
        "_note_rate_limit",
        "_breaker_allows",
        "_note_invalid",
        "_api_request",
    ),
    "client_recheck": (
        "_destination_still_permitted",
        "_roster_still_permits",
        "_audit_mid_send_decision",
        "_wait_then_revalidate",
    ),
    "client_gateway": (
        "_gateway_loop",
        "_run_connection",
        "_handle_frame",
        "_identify",
        "_start_heartbeat",
        "_stop_heartbeat",
        "_heartbeat_loop",
        "_on_dispatch",
        "_invoke_message",
        "_invoke_interaction",
    ),
}

#: Every member the class had at the base, with its kind and its signature as
#: ``inspect`` renders it. A moved function annotates ``self`` for the type checker;
#: that one annotation is the only difference the comparison removes.
BASE_MEMBERS = {
    "__init__": (
        "function",
        "(self, *, token: 'str', on_message: 'Callable[[DiscordInbound], Awaitable[None]] | None' = None, on_interaction: 'Callable[[DiscordInteraction], Awaitable[None]] | None' = None, enable_guild_threads: 'bool' = False, proxy: 'str | None' = None) -> 'None'",
    ),
    "wait_ready": ("coroutine", "(self, timeout: 'float' = 15.0) -> 'bool'"),
    "_notify_state": ("function", "(self, connected: 'bool', error: 'str') -> 'None'"),
    "start": ("coroutine", "(self) -> 'None'"),
    "close": ("coroutine", "(self) -> 'None'"),
    "set_message_handler": (
        "function",
        "(self, on_message: 'Callable[[DiscordInbound], Awaitable[None]]') -> 'None'",
    ),
    "send_message": (
        "coroutine",
        "(self, channel_id: 'str', text: 'str', *, components: 'list[dict] | None' = None, reply_to_message_id: 'str | None' = None) -> 'str | None'",
    ),
    "send_message_result": (
        "coroutine",
        "(self, channel_id: 'str', text: 'str', *, files: 'Sequence[OutboundFile]' = (), components: 'list[dict] | None' = None, reply_to_message_id: 'str | None' = None) -> 'DiscordApiResult'",
    ),
    "edit_message_result": (
        "coroutine",
        "(self, channel_id: 'str', message_id: 'str', text: 'str', *, files: 'Sequence[OutboundFile]' = (), components: 'list[dict] | None' = None) -> 'DiscordApiResult'",
    ),
    "create_thread_from_message": (
        "coroutine",
        "(self, channel_id: 'str', message_id: 'str', name: 'str') -> 'str | None'",
    ),
    "edit_message": (
        "coroutine",
        "(self, channel_id: 'str', message_id: 'str', text: 'str', *, components: 'list[dict] | None' = None) -> 'bool'",
    ),
    "edit_message_with_files_outcome": (
        "coroutine",
        "(self, channel_id: 'str', message_id: 'str', text: 'str', files: 'Sequence[OutboundFile]', *, components: 'list[dict] | None' = None) -> 'str'",
    ),
    "send_message_with_files": (
        "coroutine",
        "(self, channel_id: 'str', text: 'str', files: 'Sequence[OutboundFile]', *, components: 'list[dict] | None' = None, reply_to_message_id: 'str | None' = None) -> 'str | None'",
    ),
    "send_document": (
        "coroutine",
        "(self, channel_id: 'str', document: 'OutboundFile', *, caption: 'str | None' = None, reply_to_message_id: 'str | None' = None) -> 'str | None'",
    ),
    "edit_message_with_files": (
        "coroutine",
        "(self, channel_id: 'str', message_id: 'str', text: 'str', files: 'Sequence[OutboundFile]', *, components: 'list[dict] | None' = None) -> 'bool'",
    ),
    "send_typing": ("coroutine", "(self, channel_id: 'str') -> 'None'"),
    "add_reaction": (
        "coroutine",
        "(self, channel_id: 'str', message_id: 'str', emoji: 'str') -> 'None'",
    ),
    "register_application_commands": (
        "coroutine",
        "(self, commands: 'list[dict[str, Any]]') -> 'bool'",
    ),
    "respond_interaction": (
        "coroutine",
        "(self, interaction_id: 'str', interaction_token: 'str', text: 'str', *, ephemeral: 'bool' = True, components: 'list[dict] | None' = None, destination: 'str' = '') -> 'bool'",
    ),
    "ack_component_interaction": (
        "coroutine",
        "(self, interaction_id: 'str', interaction_token: 'str', *, destination: 'str' = '') -> 'None'",
    ),
    "download_attachment": ("coroutine", "(self, url: 'str', dest: 'str') -> 'None'"),
    "create_dm_channel": ("coroutine", "(self, user_id: 'str') -> 'str'"),
    "remember_dm_recipient": ("function", "(self, channel_id: 'str', user_id: 'str') -> 'None'"),
    "_remember_dm_pairing": ("function", "(self, channel_id: 'str', user_id: 'str') -> 'None'"),
    "cached_dm_recipient": ("function", "(self, channel_id: 'str') -> 'str | None'"),
    "is_thread_channel": ("coroutine", "(self, channel_id: 'str') -> 'bool'"),
    "edit_message_components": (
        "coroutine",
        "(self, channel_id: 'str', message_id: 'str', components: 'list[dict]') -> 'bool'",
    ),
    "_gateway_loop": ("coroutine", "(self) -> 'None'"),
    "_run_connection": ("coroutine", "(self) -> 'None'"),
    "_handle_frame": ("coroutine", "(self, ws: 'Any', frame: 'dict') -> 'None'"),
    "_identify": ("coroutine", "(self, ws: 'Any') -> 'None'"),
    "_start_heartbeat": ("function", "(self, ws: 'Any', interval: 'float') -> 'None'"),
    "_stop_heartbeat": ("function", "(self) -> 'None'"),
    "_heartbeat_loop": ("coroutine", "(self, ws: 'Any', interval: 'float') -> 'None'"),
    "_on_dispatch": ("function", "(self, event: 'str', d: 'dict') -> 'None'"),
    "_invoke_message": ("coroutine", "(self, inbound: 'DiscordInbound') -> 'None'"),
    "_invoke_interaction": ("coroutine", "(self, interaction: 'DiscordInteraction') -> 'None'"),
    "_ensure_session": ("coroutine", "(self) -> 'aiohttp.ClientSession'"),
    "_hold_key": ("function", "(self, route: 'str') -> 'str'"),
    "_set_hold": ("function", "(self, key: 'str', delay: 'float') -> 'None'"),
    "_note_headers": ("function", "(self, route: 'str', headers: 'Mapping[str, str]') -> 'None'"),
    "_await_capacity": ("coroutine", "(self, route: 'str', *, exempt: 'bool') -> 'float'"),
    "_destination_still_permitted": (
        "coroutine",
        "(self, path: 'str', destination_hint: 'str' = '') -> 'SendPermission'",
    ),
    "_roster_still_permits": ("function", "(self, destination: 'str') -> 'SendPermission'"),
    "_audit_mid_send_decision": (
        "function",
        "(self, destination: 'str', authority: 'str', outcome: 'str') -> 'None'",
    ),
    "_wait_then_revalidate": (
        "coroutine",
        "(self, path: 'str', delay: 'float', destination_hint: 'str' = '') -> 'SendPermission'",
    ),
    "_note_rate_limit": (
        "function",
        "(self, route: 'str', data: 'Any', headers: 'Mapping[str, str]') -> 'float'",
    ),
    "_breaker_allows": ("function", "(self) -> 'bool'"),
    "_note_invalid": ("function", "(self, status: 'int', headers: 'Mapping[str, str]') -> 'None'"),
    "api_json": (
        "coroutine",
        "(self, method: 'str', path: 'str', payload: 'dict | list | None', *, timeout: 'int' = 30, destination: 'str' = '', recheck_destination: 'bool' = True) -> 'DiscordApiResult'",
    ),
    "api_files": (
        "coroutine",
        "(self, method: 'str', path: 'str', payload: 'dict[str, Any]', files: 'Sequence[OutboundFile]', *, timeout: 'int' = 60, filenames: 'Sequence[str] | None' = None) -> 'DiscordApiResult'",
    ),
    "_api": (
        "coroutine",
        "(self, method: 'str', path: 'str', payload: 'dict | list | None', timeout: 'int' = 30, *, destination: 'str' = '') -> 'Any'",
    ),
    "_api_multipart": (
        "coroutine",
        "(self, method: 'str', path: 'str', payload: 'dict[str, Any]', files: 'Sequence[OutboundFile]', timeout: 'int' = 60, *, filenames: 'Sequence[str] | None' = None) -> 'Any'",
    ),
    "_api_request": (
        "coroutine",
        "(self, method: 'str', path: 'str', *, timeout: 'int', build: 'Callable[[], dict[str, Any]]', destination: 'str' = '', recheck_destination: 'bool' = True) -> 'DiscordApiResult'",
    ),
}

#: Classes that moved whole, by the owner that defines them.
MOVED_CLASSES = {
    "DiscordApiResult": "client_rest",
    "SendPermission": "client_recheck",
    "DiscordInbound": "client_gateway",
    "DiscordInteraction": "client_gateway",
}

#: Constructs that stay in ``client.py``. ``__init__`` and ``close`` hold the
#: ``_handler_tasks`` literal ``test_update_check_install_aware_background_work.py``
#: finds by globbing ``*/client.py``, and ``_safe_description`` is the redaction
#: pass of the "Discord attachment description" posture sink, which names this file.
FACADE_PINNED = frozenset(
    {
        "DiscordClient",
        "__init__",
        "close",
        "_safe_description",
    }
)


def _owner(stem: str) -> ModuleType:
    return importlib.import_module(f"kiro_crew.discord.{stem}")


def _source(stem: str) -> str:
    return (DISCORD_DIR / f"{stem}.py").read_text(encoding="utf-8")


def _runtime_imports(source: str) -> set[str]:
    """Modules *source* imports when it loads; ``if TYPE_CHECKING:`` blocks excluded."""
    loaded: set[str] = set()
    for node in ast.parse(source).body:
        if isinstance(node, ast.Import):
            loaded |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            loaded.add(node.module)
            loaded |= {f"{node.module}.{alias.name}" for alias in node.names}
    return loaded


def _describe(member: Any, cls: type) -> tuple[str, str]:
    """A class member's kind and ``inspect`` signature, less the owner's ``self`` annotation."""
    if isinstance(member, property):
        kind, fn = "property", member.fget
    elif isinstance(member, staticmethod):
        kind, fn = "staticmethod", member.__func__
    elif isinstance(member, classmethod):
        kind, fn = "classmethod", member.__func__
    elif inspect.isfunction(member):
        kind = "coroutine" if inspect.iscoroutinefunction(member) else "function"
        fn = member
    elif isinstance(member, (dict, list, set)):
        # Mutable class state (the decider's registries): its contents are runtime state.
        return "attribute", type(member).__name__
    else:
        return "attribute", repr(member)
    signature = str(inspect.signature(fn)).replace(f"self: '{cls.__name__}'", "self", 1)
    return kind, signature


def _members(cls: type) -> set[str]:
    kept = ("__init__", "__call__")
    return {n for n in vars(cls) if (not n.startswith("__") or n in kept) and n != "_abc_impl"}


def _class_body_reads(source: str) -> list[str]:
    """What only a class body provides: zero-argument ``super()``, ``__class__``, mangling."""
    hits: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "super"
            and not node.args
        ):
            hits.append(f"{node.lineno}: super()")
        elif isinstance(node, ast.Name) and node.id == "__class__":
            hits.append(f"{node.lineno}: __class__")
        elif (
            isinstance(node, ast.Attribute)
            and node.attr.startswith("__")
            and not node.attr.endswith("__")
        ):
            hits.append(f"{node.lineno}: {node.attr}")
    return sorted(hits)


# ── the owners ───────────────────────────────────────────────────────────────────


def test_the_owners_are_every_sibling_module_in_layer_order() -> None:
    on_disk = {path.stem for path in DISCORD_DIR.glob(f"{STEM}_*.py")}
    assert on_disk == set(OWNERS)
    assert FACADE_MODULE._OWNERS == tuple(f"kiro_crew.discord.{stem}" for stem in OWNERS)


def test_an_owner_imports_only_owners_below_it_and_never_its_facade() -> None:
    for index, stem in enumerate(OWNERS):
        loaded = _runtime_imports(_source(stem))
        assert FACADE not in loaded, stem
        siblings = {
            name.rsplit(".", 1)[-1]
            for name in loaded
            if name.startswith(f"kiro_crew.discord.{STEM}_") and name.count(".") == 2
        }
        assert siblings <= set(OWNERS[:index]), stem


def test_every_owner_logs_under_the_facades_name() -> None:
    assert FACADE_MODULE.logger is logging.getLogger(FACADE)
    for stem in OWNERS:
        logger = vars(_owner(stem)).get("logger")
        if logger is not None:
            assert logger is logging.getLogger(FACADE), stem


def test_the_facade_installs_its_mirror_when_run_as_a_script(tmp_path: Path) -> None:
    """Under ``python -m`` the facade is ``__main__``: one logger by literal name keeps
    the family to one object per name, which the mirror's install requires."""
    result = subprocess.run(
        [sys.executable, "-m", FACADE],
        capture_output=True,
        cwd=tmp_path,
        env=dict(os.environ),
        timeout=120,
        **UTF8_TEXT,
    )
    assert result.returncode == 0, result.stderr


# ── the class surface ────────────────────────────────────────────────────────────


def test_the_class_keeps_every_base_member() -> None:
    assert sorted(set(BASE_MEMBERS) - _members(CLS)) == []


def test_every_base_member_keeps_its_kind_and_signature() -> None:
    members = vars(CLS)
    for name, described in BASE_MEMBERS.items():
        assert _describe(members[name], CLS) == described, name


def test_a_moved_member_is_its_owners_function() -> None:
    members = vars(CLS)
    for stem, names in MOVED_MEMBERS.items():
        owner = _owner(stem)
        for name in names:
            assert members[name] is getattr(owner, name), f"{stem}.{name}"
            assert members[name].__module__ == owner.__name__, f"{stem}.{name}"
            assert members[name].__qualname__ == name, f"{stem}.{name}"


def test_every_owner_function_of_self_is_bound_on_the_class() -> None:
    bound = {name for names in MOVED_MEMBERS.values() for name in names}
    for stem in OWNERS:
        owner = _owner(stem)
        for name, value in vars(owner).items():
            if not inspect.isfunction(value) or value.__module__ != owner.__name__:
                continue
            if list(inspect.signature(value).parameters)[:1] == ["self"]:
                assert name in bound and vars(CLS)[name] is value, f"{stem}.{name}"


def test_no_bound_function_needs_the_class_body() -> None:
    """A function bound into the class from an owner was compiled outside it: it has no
    ``__class__`` cell, so a zero-argument ``super()`` or a ``__class__`` read raises, and
    a ``self.__name`` is never mangled. So no owner spells one; a method that needs one
    stays in the class body."""
    assert {stem: hits for stem in OWNERS if (hits := _class_body_reads(_source(stem)))} == {}


def test_the_class_body_scan_can_fail() -> None:
    planted = "def f(self):\n    super().g()\n    return __class__, self.__secret, self.__dict__\n"
    assert _class_body_reads(planted) == ["2: super()", "3: __class__", "3: __secret"]


def _top_level_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names |= {t.id for t in node.targets if isinstance(t, ast.Name)}
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def test_each_guarded_construct_stays_in_the_facade() -> None:
    """The constructs ``FACADE_PINNED`` names are defined in the facade's own file."""
    tree = ast.parse(FACADE_PATH.read_text(encoding="utf-8"))
    cls_node = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == CLS.__name__
    )
    members = {
        node.name
        for node in cls_node.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert sorted(FACADE_PINNED - (_top_level_names(tree) | members)) == []
    for stem in OWNERS:
        owned = _top_level_names(ast.parse(_source(stem)))
        assert sorted(FACADE_PINNED & owned) == [], stem


def _imported_modules(node: ast.Import | ast.ImportFrom, module: str) -> list[str]:
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
                name.startswith(OWNER_PREFIX) for name in _imported_modules(node, module)
            ):
                found.add(rel.as_posix())
    return found


def test_only_the_family_imports_an_owner() -> None:
    """Callers reach the owners' names through the facade, never by importing an owner."""
    family = {f"discord/{stem}.py" for stem in (STEM, *OWNERS)}
    sources = source_corpus.iter_candidate_sources(require_any=(f"{STEM}_",))
    importers = _owner_importers(
        ((path, text) for path, text in sources if path.is_relative_to(SRC)), SRC
    )
    assert FAMILY_IMPORTER in importers  # owners import the owners below them
    assert importers - family == set()


def test_the_owner_import_scan_finds_an_outside_importer(tmp_path: Path) -> None:
    root = tmp_path / "kiro_crew"
    owner = OWNERS[0]
    sources = {
        root / "pkg" / "caller.py": f"from kiro_crew.discord.{owner} import x\n",
        root / "other.py": f"from kiro_crew.discord import {owner} as low\n",
        root / "discord" / "relative.py": f"from .{owner} import x\n",
        root / "discord" / "deep" / "up.py": f"from .. import {owner}\n",
        root / "facade_user.py": f"from kiro_crew.discord.{STEM} import x\n",
    }
    assert _owner_importers(sources.items(), root) == {
        "pkg/caller.py",
        "other.py",
        "discord/relative.py",
        "discord/deep/up.py",
    }


def test_each_moved_class_is_served_whole_from_its_owner() -> None:
    """A class that moved keeps one object: the facade serves the owner's, whose
    ``__module__`` names the owner. Nothing keys behaviour on that module name."""
    for name, stem in MOVED_CLASSES.items():
        owner = _owner(stem)
        assert getattr(FACADE_MODULE, name) is getattr(owner, name), name
        assert getattr(owner, name).__module__ == owner.__name__, name
