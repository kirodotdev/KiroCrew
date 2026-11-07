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
from collections.abc import Iterable
from pathlib import Path
from types import ModuleType
from typing import Any

import owner_import_scan
import pytest

import kiro_crew
from kiro_crew.discord import client as FACADE_MODULE

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

SHARED_FACADE_CHECKS = (
    "the_class_body_scan_can_fail",
    "the_facade_installs_its_mirror_when_run_as_a_script",
    "every_owner_logs_under_the_facades_name",
    "a_moved_member_is_its_owners_function",
    "every_owner_function_of_self_is_bound_on_the_class",
    "no_bound_function_needs_the_class_body",
    "each_guarded_construct_stays_in_the_facade",
    "the_owners_are_every_sibling_module_in_layer_order",
    "an_owner_imports_only_owners_below_it_and_never_its_facade",
    "the_class_keeps_every_base_member",
    "every_base_member_keeps_its_kind_and_signature",
    "only_the_family_imports_an_owner",
    "the_owner_import_scan_finds_an_outside_importer",
    "each_moved_class_is_served_whole_from_its_owner",
)


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


# ── the class surface ────────────────────────────────────────────────────────────


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


def _owner_importers(sources: Iterable[tuple[Path, str]], root: Path) -> set[str]:
    """The files among *sources* (relative to *root*) whose code imports a family owner."""
    return owner_import_scan.owner_importers(
        sources, root, lambda name: name.startswith(OWNER_PREFIX)
    )
