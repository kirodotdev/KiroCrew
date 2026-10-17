"""``TelegramClient`` is one class composed from owner modules; these tests pin how.

``kiro_crew.telegram.client`` keeps the client's state, its Bot API methods, the update
parse and handler dispatch, and the public limits and records. An owner function whose
first parameter is ``self`` is bound as the class attribute of the same name, and every
other moved name is served from its owner through ``kiro_crew._facade_mirror``. Pinned
here: the owner set and its layer order, that each moved member is its owner's own
function with its base kind and signature, the rules a function bound from outside the
class body needs, and the logger name, which ``test_telegram_album.py`` captures. The
layout is described in ``docs/system-specs/modules/messaging.md`` (Telegram's renderer
and client composition).
"""

from __future__ import annotations

import ast
import importlib
import inspect
import logging
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

from kiro_crew.subprocess_utf8 import UTF8_TEXT
from kiro_crew.telegram import client as FACADE_MODULE

FACADE = "kiro_crew.telegram.client"
STEM = "client"
FACADE_PATH = Path(FACADE_MODULE.__file__).resolve()
TELEGRAM_DIR = FACADE_PATH.parent
TEST_DIR = Path(__file__).resolve().parent
CLS = FACADE_MODULE.TelegramClient

#: Owner modules, lowest layer first: an owner imports only owners listed before it.
OWNERS = (
    "client_http",
    "client_polling",
    "client_albums",
)

#: ``TelegramClient`` members an owner defines, by owner, each bound as the class attribute.
MOVED_MEMBERS = {
    "client_http": (
        "_proxy_for",
        "_call_raw",
        "_ensure_session",
        "_api",
        "_api_multipart",
        "_api_request",
    ),
    "client_polling": (
        "_load_offset",
        "_discard_legacy_offset",
        "_save_offset",
        "_notify_status",
        "_poll_backoff",
        "_polling_loop",
        "_get_updates",
        "_resolve_updates",
        "_persistable_offset",
        "_maybe_persist_offset",
        "_persist_offset",
    ),
    "client_albums": (
        "_buffer_album_member",
        "_arm_album_timer",
        "_album_flush_after",
        "_flush_album",
        "_flush_all_albums",
    ),
}

#: Every member the class had at the base, with its kind and its signature as
#: ``inspect`` renders it. A moved function annotates ``self`` for the type checker;
#: that one annotation is the only difference the comparison removes.
BASE_MEMBERS = {
    "__init__": (
        "function",
        "(self, *, token: 'str', on_message: 'Callable[[TelegramInbound], Awaitable[None]] | None' = None, on_callback: 'Callable[[TelegramCallback], Awaitable[None]] | None' = None, polling_timeout: 'int' = 30, proxy: 'str | None' = None, offset_path: \"'Path | None'\" = None) -> 'None'",
    ),
    "start": ("coroutine", "(self) -> 'None'"),
    "_load_offset": ("function", "(self) -> 'int'"),
    "_discard_legacy_offset": ("function", "(self) -> 'None'"),
    "_save_offset": ("function", "(self, offset: 'int') -> 'None'"),
    "close": ("coroutine", "(self) -> 'None'"),
    "set_message_handler": (
        "function",
        "(self, on_message: 'Callable[[TelegramInbound], Awaitable[None]]') -> 'None'",
    ),
    "send_message": (
        "coroutine",
        "(self, chat_id: 'int', text: 'str', *, parse_mode: 'str | None' = None, reply_markup: 'dict | None' = None, retry_plain: 'bool' = True, reply_to_message_id: 'int | None' = None, message_thread_id: 'int | None' = None, disable_notification: 'bool' = False) -> 'int | None'",
    ),
    "send_rich_message": (
        "coroutine",
        "(self, chat_id: 'int', markdown: 'str', *, reply_markup: 'dict | None' = None, message_thread_id: 'int | None' = None, disable_notification: 'bool' = False, reply_to_message_id: 'int | None' = None) -> 'int | None'",
    ),
    "send_message_draft": (
        "coroutine",
        "(self, chat_id: 'int', draft_id: 'int', text: 'str', *, parse_mode: 'str | None' = None, message_thread_id: 'int | None' = None) -> 'bool'",
    ),
    "edit_message": (
        "coroutine",
        "(self, chat_id: 'int', message_id: 'int', text: 'str', *, parse_mode: 'str | None' = None, reply_markup: 'dict | None' = None, retry_plain: 'bool' = True) -> 'bool'",
    ),
    "edit_message_reply_markup": (
        "coroutine",
        "(self, chat_id: 'int', message_id: 'int', reply_markup: 'dict | None' = None) -> 'bool'",
    ),
    "send_typing": (
        "coroutine",
        "(self, chat_id: 'int', *, message_thread_id: 'int | None' = None) -> 'None'",
    ),
    "answer_callback": (
        "coroutine",
        "(self, callback_query_id: 'str', text: 'str' = '') -> 'None'",
    ),
    "delete_message": ("coroutine", "(self, chat_id: 'int', message_id: 'int') -> 'None'"),
    "set_message_reaction": (
        "coroutine",
        "(self, chat_id: 'int', message_id: 'int', emoji: 'str') -> 'bool'",
    ),
    "send_photo": (
        "coroutine",
        "(self, chat_id: 'int', photo: \"'OutboundFile'\", *, caption: 'str | None' = None, parse_mode: 'str | None' = None, reply_markup: 'dict | None' = None, message_thread_id: 'int | None' = None, disable_notification: 'bool' = False) -> 'int | None'",
    ),
    "send_document": (
        "coroutine",
        "(self, chat_id: 'int', document: \"'OutboundFile'\", *, caption: 'str | None' = None, message_thread_id: 'int | None' = None, disable_notification: 'bool' = True) -> 'int | None'",
    ),
    "send_voice": (
        "coroutine",
        "(self, chat_id: 'int', audio: 'bytes', *, filename: 'str', mime: 'str', caption: 'str | None' = None, message_thread_id: 'int | None' = None, disable_notification: 'bool' = True, reply_to_message_id: 'int | None' = None) -> 'int | None'",
    ),
    "send_media_group": (
        "coroutine",
        "(self, chat_id: 'int', photos: \"'Sequence[OutboundFile]'\", *, message_thread_id: 'int | None' = None, disable_notification: 'bool' = False) -> 'list[int]'",
    ),
    "_proxy_for": ("function", "(self, url: 'str') -> 'str | None'"),
    "download_file": ("coroutine", "(self, file_id: 'str', dest: 'str') -> 'None'"),
    "set_my_commands": ("coroutine", "(self, commands: 'list[dict[str, str]]') -> 'bool'"),
    "_call_raw": (
        "coroutine",
        "(self, method: 'str', params: 'dict', timeout: 'int' = 15) -> 'Any'",
    ),
    "get_me": ("coroutine", "(self) -> 'dict'"),
    "_notify_status": ("function", "(self, healthy: 'bool', reason: 'str') -> 'None'"),
    "_poll_backoff": ("function", "(self, attempt: 'int', reason: 'str') -> 'float'"),
    "_polling_loop": ("coroutine", "(self) -> 'None'"),
    "_get_updates": ("coroutine", "(self) -> 'list[dict] | None'"),
    "_buffer_album_member": (
        "function",
        "(self, group_id: 'str', inbound: 'TelegramInbound', *, update_id: 'int' = 0) -> 'None'",
    ),
    "_arm_album_timer": ("function", "(self, group_id: 'str') -> 'None'"),
    "_album_flush_after": ("coroutine", "(self, group_id: 'str', delay: 'float') -> 'None'"),
    "_flush_album": ("function", "(self, group_id: 'str') -> 'None'"),
    "_flush_all_albums": ("function", "(self) -> 'None'"),
    "_build_inbound": ("staticmethod", "(msg: 'dict') -> 'TelegramInbound'"),
    "_offset_lock": ("property", "(self) -> 'asyncio.Lock'"),
    "_resolve_updates": ("function", "(self, update_ids: \"'Iterable[int]'\") -> 'None'"),
    "_persistable_offset": ("function", "(self) -> 'int'"),
    "_maybe_persist_offset": ("function", "(self) -> 'None'"),
    "_persist_offset": ("coroutine", "(self) -> 'None'"),
    "_track": ("function", "(self, task: 'asyncio.Task[None]') -> 'None'"),
    "_spawn_handler": (
        "function",
        "(self, inbound: 'TelegramInbound', update_ids: \"'tuple[int, ...]'\" = ()) -> 'None'",
    ),
    "_dispatch": ("function", "(self, update: 'dict') -> 'None'"),
    "_invoke_message": (
        "coroutine",
        "(self, inbound: 'TelegramInbound', update_ids: \"'tuple[int, ...]'\" = ()) -> 'None'",
    ),
    "_invoke_callback": (
        "coroutine",
        "(self, callback: 'TelegramCallback', update_ids: \"'tuple[int, ...]'\" = ()) -> 'None'",
    ),
    "_ensure_session": ("coroutine", "(self) -> 'aiohttp.ClientSession'"),
    "_api": (
        "coroutine",
        "(self, method: 'str', params: 'dict', timeout: 'int' = 30, *, record: 'bool' = True, err_out: 'dict | None' = None) -> 'Any'",
    ),
    "_api_multipart": (
        "coroutine",
        "(self, method: 'str', params: 'dict', files: \"'Sequence[OutboundFile]'\", *, field_names: \"'Sequence[str]'\", filenames: \"'Sequence[str] | None'\" = None, timeout: 'int' = 120, err_out: 'dict | None' = None) -> 'Any'",
    ),
    "_api_request": (
        "coroutine",
        "(self, method: 'str', body: \"'Callable[[], dict]'\", timeout: 'int' = 30, *, record: 'bool' = True, err_out: 'dict | None' = None) -> 'Any'",
    ),
}

#: Constructs that stay in ``client.py``. ``__init__`` and ``_track`` hold the
#: ``_handler_tasks`` literal ``test_update_check_install_aware_background_work.py``
#: finds by globbing ``*/client.py``. The decorated members stay because a bound
#: module function cannot carry the decorator, the records and the auth error keep
#: their ``__module__``, and the public text helpers and limits stay with them.
FACADE_PINNED = frozenset(
    {
        "TelegramClient",
        "__init__",
        "_track",
        "_offset_lock",
        "_build_inbound",
        "TelegramAuthError",
        "TelegramInbound",
        "TelegramCallback",
        "_flatten_text_links",
        "_mention_handles",
        "truncate_html_safe",
        "_cap_text",
        "normalize_reaction_emoji",
        "REACTION_EMOJI",
        "_MAX_ENTITY_LEN",
    }
)


def _owner(stem: str) -> ModuleType:
    return importlib.import_module(f"kiro_crew.telegram.{stem}")


def _source(stem: str) -> str:
    return (TELEGRAM_DIR / f"{stem}.py").read_text(encoding="utf-8")


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


def _describe(member: Any) -> tuple[str, str]:
    """A class member's kind and ``inspect`` signature, less the owner's ``self`` annotation."""
    if isinstance(member, property):
        kind, fn = "property", member.fget
    elif isinstance(member, staticmethod):
        kind, fn = "staticmethod", member.__func__
    elif inspect.isfunction(member):
        kind = "coroutine" if inspect.iscoroutinefunction(member) else "function"
        fn = member
    else:
        return "attribute", repr(member)
    signature = str(inspect.signature(fn)).replace(f"self: '{CLS.__name__}'", "self", 1)
    return kind, signature


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
    on_disk = {path.stem for path in TELEGRAM_DIR.glob(f"{STEM}_*.py")}
    assert on_disk == set(OWNERS)
    assert FACADE_MODULE._OWNERS == tuple(f"kiro_crew.telegram.{stem}" for stem in OWNERS)


def test_an_owner_imports_only_owners_below_it_and_never_its_facade() -> None:
    for index, stem in enumerate(OWNERS):
        loaded = _runtime_imports(_source(stem))
        assert FACADE not in loaded, stem
        siblings = {
            name.rsplit(".", 1)[-1]
            for name in loaded
            if name.startswith(f"kiro_crew.telegram.{STEM}_") and name.count(".") == 2
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


def test_the_class_keeps_every_base_member_and_gains_none() -> None:
    members = {
        n for n in vars(CLS) if (not n.startswith("__") or n == "__init__") and n != "_abc_impl"
    }
    assert members == set(BASE_MEMBERS)


def test_every_base_member_keeps_its_kind_and_signature() -> None:
    members = vars(CLS)
    for name, described in BASE_MEMBERS.items():
        assert _describe(members[name]) == described, name


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


def test_the_records_and_the_auth_error_stay_in_the_facade() -> None:
    for name in ("TelegramAuthError", "TelegramInbound", "TelegramCallback", "TelegramClient"):
        assert getattr(FACADE_MODULE, name).__module__ == FACADE, name
