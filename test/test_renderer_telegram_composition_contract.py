"""``TelegramRenderer`` is one class composed from owner modules; these tests pin how.

``kiro_crew.telegram.renderer`` keeps the renderer's state and the constructs repository
guards read in that file. An owner function whose first parameter is ``self`` is bound as
the class attribute of the same name, and every other moved name is served from its owner
through ``kiro_crew._facade_mirror``. Pinned here: the owner set and its layer order, that
each moved member is its owner's own function with its base kind and signature, the rules
a function bound from outside the class body needs, the logger name, and, over the owners,
the three sealing and offload rules ``test_split_credential_boundary.py`` runs on
``renderer.py``. The layout is described in ``docs/system-specs/modules/messaging.md``
(Telegram's renderer and client composition).
"""

from __future__ import annotations

import ast
import importlib
import inspect
import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

from kiro_crew.messaging import display_safety
from kiro_crew.subprocess_utf8 import UTF8_TEXT
from kiro_crew.telegram import renderer as FACADE_MODULE

FACADE = "kiro_crew.telegram.renderer"
STEM = "renderer"
FACADE_PATH = Path(FACADE_MODULE.__file__).resolve()
TELEGRAM_DIR = FACADE_PATH.parent
TEST_DIR = Path(__file__).resolve().parent
CLS = FACADE_MODULE.TelegramRenderer

#: Owner modules, lowest layer first: an owner imports only owners listed before it.
OWNERS = (
    "renderer_display",
    "renderer_tables",
    "renderer_approval",
    "renderer_status",
    "renderer_live",
    "renderer_uploads",
)

#: ``TelegramRenderer`` members an owner defines, by owner, each bound as the class attribute.
MOVED_MEMBERS = {
    "renderer_approval": ("on_prompt_choice",),
    "renderer_status": (
        "on_turn_start",
        "_typing_loop",
        "_stop_typing",
        "_note_progress",
        "_stall_mark",
        "attach_context_client",
        "on_thinking",
        "_turn_footer",
        "_thinking_budget",
        "on_tool_call",
        "on_compaction",
    ),
    "renderer_live": (
        "on_text_chunk",
        "_materialize_chip",
        "_rotate_at_markers",
        "_open_new_message",
        "_segment_text",
        "_consume_reply_to",
        "_stream_live",
        "_stream_live_locked",
        "_retire_live_frame",
        "_delete_replaced_frame",
        "note_steer",
        "_chip_for_seal",
    ),
    "renderer_uploads": (
        "authorize_upload_root",
        "_uploads_enabled",
        "_extract_uploads",
        "_append_rejections",
        "_send_uploads",
    ),
}

#: Every member the class had at the base, with its kind and its signature as
#: ``inspect`` renders it. A moved function annotates ``self`` for the type checker;
#: that one annotation is the only difference the comparison removes.
BASE_MEMBERS = {
    "channel_type": ("attribute", "'telegram'"),
    "__init__": (
        "function",
        "(self, client: \"'TelegramClient'\", chat_id: 'int', capabilities: 'TransportCapabilities', *, session_key: 'str' = '', message_thread_id: 'int | None' = None, show_thinking: 'bool' = False, uploads_allowed: 'bool' = True, reply_to_message_id: 'int | None' = None) -> 'None'",
    ),
    "on_turn_start": ("coroutine", "(self) -> 'None'"),
    "_typing_loop": ("coroutine", "(self) -> 'None'"),
    "_stop_typing": ("function", "(self) -> 'None'"),
    "_note_progress": ("function", "(self) -> 'None'"),
    "_stall_mark": ("function", "(self) -> 'str'"),
    "on_text_chunk": ("coroutine", "(self, text: 'str') -> 'None'"),
    "_materialize_chip": ("function", "(self) -> 'None'"),
    "on_steer_consumed": ("coroutine", "(self, summary: 'str' = '') -> 'None'"),
    "_rotate_at_markers": ("coroutine", "(self) -> 'None'"),
    "_rotate_on_length": ("coroutine", "(self) -> 'None'"),
    "_carve_graded": (
        "coroutine",
        "(self, source: 'str', limit: 'int', rendered_cap: 'int') -> 'list[str] | None'",
    ),
    "_open_new_message": ("function", "(self) -> 'None'"),
    "_segment_text": ("function", "(self) -> 'str'"),
    "_consume_reply_to": ("function", "(self) -> 'int | None'"),
    "_stream_live": ("coroutine", "(self, *, force: 'bool' = False) -> 'None'"),
    "_safe_body": ("coroutine", "(self, seg: 'str') -> 'str'"),
    "_stream_live_locked": ("coroutine", "(self, *, force: 'bool') -> 'None'"),
    "_seal_without_rich": ("coroutine", "(self, text: 'str') -> 'tuple[str, str, int | None]'"),
    "attach_context_client": ("function", "(self, client: 'Any') -> 'None'"),
    "authorize_upload_root": (
        "function",
        "(self, root: 'object', scratch_root: 'object' = '') -> 'None'",
    ),
    "_uploads_enabled": ("function", "(self) -> 'bool'"),
    "_extract_uploads": ("coroutine", "(self, text: 'str') -> 'tuple[str, list[OutboundFile]]'"),
    "_append_rejections": (
        "function",
        "(self, body: 'str', rejections: 'list[Rejection]') -> 'str'",
    ),
    "_send_uploads": ("coroutine", "(self, files: 'list[OutboundFile]') -> 'None'"),
    "_seam_safe": ("function", "(self, text: 'str') -> 'str'"),
    "_record_sent": ("function", "(self, text: 'str') -> 'None'"),
    "_seal_current": (
        "coroutine",
        "(self, *, keyboard: 'dict | None' = None, extract_uploads: 'bool' = True, footer: 'str' = '') -> 'None'",
    ),
    "_retire_live_frame": ("coroutine", "(self) -> 'None'"),
    "_delete_replaced_frame": ("coroutine", "(self, mid: 'int | None') -> 'None'"),
    "_seal_text": (
        "coroutine",
        "(self, text: 'str', keyboard: 'dict | None', *, footer: 'str' = '') -> 'None'",
    ),
    "on_thinking": ("coroutine", "(self, text: 'str') -> 'None'"),
    "_turn_footer": ("function", "(self) -> 'str'"),
    "_thinking_budget": ("function", "(self) -> 'int'"),
    "_post_thinking": ("coroutine", "(self) -> 'None'"),
    "on_tool_call": (
        "coroutine",
        "(self, tool_call_id: 'str', title: 'str', tool_kind: 'str' = '', tool_purpose: 'str' = '') -> 'None'",
    ),
    "on_prompt_choice": (
        "coroutine",
        "(self, options: 'list[dict[str, Any]]', request_id: 'str | int', tool_title: 'str' = '', tool_purpose: 'str' = '', tool_input: 'str' = '') -> 'None'",
    ),
    "on_compaction": ("coroutine", "(self, context_usage_pct: 'float') -> 'None'"),
    "note_steer": ("function", "(self, text: 'str') -> 'None'"),
    "on_done": ("coroutine", "(self, stop_reason: 'str' = '') -> 'None'"),
    "_limit": ("function", "(self) -> 'int'"),
    "_rendered_limit": ("function", "(self) -> 'int'"),
    "_rich_limit": ("function", "(self) -> 'int'"),
    "_degraded_table_chunks": ("function", "(self, text: 'str') -> 'list[str]'"),
    "_seal_chunk_html": ("coroutine", "(self, chunk: 'str') -> 'tuple[bool, int | None]'"),
    "_send_lost_part_notice": ("coroutine", "(self) -> 'None'"),
    "_chip_for_seal": ("function", "(self, i: 'int') -> 'str | None'"),
    "_tally_redactions": ("function", "(self, text: 'str') -> 'None'"),
    "_maybe_send_redaction_notice": ("coroutine", "(self) -> 'None'"),
    "close": ("coroutine", "(self, failure_reason: 'str | None' = None) -> 'None'"),
}

#: Constructs repository guards locate in ``renderer.py`` itself: the credential-
#: boundary suite's sinks, seal paths and splitters, the parity suite's one raw
#: translator and its callers, the fallback-pattern suite's ``_strip_md``, and the
#: posture panel's widget-text sink, ``build_inline_keyboard``.
FACADE_PINNED = frozenset(
    {
        "TelegramRenderer",
        "__init__",
        "_rotate_on_length",
        "on_steer_consumed",
        "_seal_without_rich",
        "_seam_safe",
        "_record_sent",
        "_seal_current",
        "_seal_text",
        "_seal_chunk_html",
        "_degraded_table_chunks",
        "_split_markdown",
        "_split_markdown_bounded",
        "_split_markdown_table_aware",
        "_md_to_telegram_html",
        "md_to_telegram_html_safe",
        "_seal_table_fallback",
        "_rendered_len",
        "_strip_md",
        "_BULLET_RE",
        "build_inline_keyboard",
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


def test_the_decider_moved_whole_and_keeps_its_owner_module() -> None:
    owner = _owner("renderer_approval")
    assert FACADE_MODULE.TelegramApprovalDecider is owner.TelegramApprovalDecider
    assert owner.TelegramApprovalDecider.__module__ == owner.__name__


# ── the sealing and offload rules, over the owners ───────────────────────────────
# ``test_split_credential_boundary.py`` runs these three over ``renderer.py``; the
# same rules hold for the code its owners hold, so they run over the owners here.

_SEALERS = frozenset({"_seal_current", "_seal_chunk_html", "_seal_text"})


def _name_of(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _own_body_calls(func: ast.AsyncFunctionDef) -> list[ast.Call]:
    """Every call in *func*'s own body; a nested function runs where it is handed."""
    calls: list[ast.Call] = []
    stack: list[ast.AST] = list(ast.iter_child_nodes(func))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if isinstance(node, ast.Call):
            calls.append(node)
        stack.extend(ast.iter_child_nodes(node))
    return calls


def _functions(source: str) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]


def _sent_tail_writers(source: str) -> set[str]:
    return {
        node.name
        for node in _functions(source)
        for stmt in ast.walk(node)
        if isinstance(stmt, ast.Assign)
        and any(isinstance(t, ast.Attribute) and t.attr == "_sent_tail" for t in stmt.targets)
    }


def _bespoke_seam_rules(source: str) -> set[str]:
    callers = [
        node
        for node in _functions(source)
        if any(
            isinstance(call, ast.Call) and _name_of(call.func) in _SEALERS
            for call in ast.walk(node)
        )
    ]
    return {
        node.name
        for node in callers
        for call in ast.walk(node)
        if isinstance(call, ast.Call)
        and (
            _name_of(call.func) == "repaired_after_a_sent_tail"
            or (
                _name_of(call.func) == "to_thread"
                and call.args
                and _name_of(call.args[0]) == "repaired_after_a_sent_tail"
            )
        )
    }


def _scanners() -> frozenset[str]:
    """``TELEGRAM_SCANNERS`` as the credential-boundary suite spells it, read by syntax."""
    found = []
    for path in sorted(TEST_DIR.glob("test_split_credential_boundary*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Assign)
                and any(
                    isinstance(t, ast.Name) and t.id == "TELEGRAM_SCANNERS" for t in node.targets
                )
                and isinstance(node.value, ast.Call)
                and node.value.args
            ):
                found.append(frozenset(ast.literal_eval(node.value.args[0])))
    assert len(found) == 1, f"TELEGRAM_SCANNERS spelled {len(found)} times"
    return found[0]


def _direct_scans(source: str, scanners: frozenset[str]) -> list[str]:
    return [
        f"{node.name} -> {_name_of(call.func)}"
        for node in _functions(source)
        if isinstance(node, ast.AsyncFunctionDef)
        for call in _own_body_calls(node)
        if _name_of(call.func) in scanners
    ]


def test_no_owner_writes_the_sent_record() -> None:
    assert {stem: w for stem in OWNERS if (w := _sent_tail_writers(_source(stem)))} == {}


def test_no_owner_sealing_caller_carries_its_own_seam_rule() -> None:
    assert {stem: b for stem in OWNERS if (b := _bespoke_seam_rules(_source(stem)))} == {}


def test_no_owner_coroutine_calls_a_scanner_directly() -> None:
    scanners = _scanners()
    assert {"_split_markdown", "repaired_after_a_sent_tail"} <= scanners
    assert {stem: c for stem in OWNERS if (c := _direct_scans(_source(stem), scanners))} == {}


def test_the_owner_sealing_scans_can_fail() -> None:
    planted = (
        "async def rotate(self):\n"
        "    self._sent_tail = ''\n"
        "    await asyncio.to_thread(repaired_after_a_sent_tail, x)\n"
        "    await self._seal_text(x, None)\n"
        "    chunks = _split_markdown(x, 9)\n"
        "    def offloaded():\n"
        "        return _split_markdown(x, 9)\n"
    )
    assert _sent_tail_writers(planted) == {"rotate"}
    assert _bespoke_seam_rules(planted) == {"rotate"}
    assert _direct_scans(planted, frozenset({"_split_markdown"})) == ["rotate -> _split_markdown"]


def test_each_shared_pattern_is_spelled_once_per_owner() -> None:
    """A byte-identical copy of a shared pass compiles to the SAME object, so only a
    second name gives it away; ``test_markdown_link_parentheses`` reads the facade, and
    this reads each owner."""
    shared = {
        name: getattr(display_safety, name)
        for name in vars(display_safety)
        if name.startswith("TELEGRAM_FALLBACK_")
        and isinstance(getattr(display_safety, name), re.Pattern)
    }
    assert shared
    for stem in OWNERS:
        module = _owner(stem)
        for name, pattern in shared.items():
            spelled = {
                n
                for n, value in vars(module).items()
                if isinstance(value, re.Pattern)
                and (value.pattern, value.flags) == (pattern.pattern, pattern.flags)
            }
            assert spelled <= {name}, f"{stem}: {sorted(spelled)}"
