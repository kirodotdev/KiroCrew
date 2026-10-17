"""``ContextBuilder`` keeps every member it had, and each moved one is bound from its owner.

The bodies of most builder methods live in ``kiro_crew.context_assembly`` owner modules:
each is a module-level function there whose first parameter is ``self`` (or a plain
function for a static method), and the class in :mod:`kiro_crew.context` binds it by name
(``m = _owner.m``, or ``staticmethod(_owner.f)``). The builder's state stays on the
instance, so class and instance patches of a member keep applying. This file pins that
binding:

* the frozen member inventory -- every member, its kind, whether it is a coroutine and
  the module that owns its body;
* each bound member IS its owner's function object, and no binding owner keeps a
  ``self`` function the class does not bind;
* no bound function uses zero-argument ``super()``, the ``__class__`` cell or a
  name-mangled attribute, which only work inside a class body.
"""

from __future__ import annotations

import ast
import inspect
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from kiro_crew import context as ctx
from kiro_crew.context import ContextBuilder

pytestmark = pytest.mark.xdist_group(name="tree_scan_context_assembly_bindings")

_PACKAGE = "kiro_crew.context_assembly"

#: Member -> (kind, coroutine, owner). Kind and coroutine are BASE's; the owner is the
#: module that holds the body now ("context" for a member still defined in the class).
_MEMBERS: dict[str, tuple[str, bool, str]] = {
    "_CAP_FIGURE_SESSIONS": ("data", False, "context"),
    "_COMPUTER_USE_TOKEN": ("data", False, "context"),
    "_MAX_SUBAGENTS_TOKEN": ("data", False, "context"),
    "_SENT_SKILL_BODY_ENTRIES": ("data", False, "context"),
    "_SENT_SKILL_BODY_SESSIONS": ("data", False, "context"),
    "_SKILL_DELIVERY_AUDIT_NAMES": ("data", False, "context"),
    "_SKILL_DELIVERY_AUDIT_NAME_LEN": ("data", False, "context"),
    "__init__": ("function", False, "context"),
    "ensure_store": ("staticmethod", True, "context"),
    "get_lessons_for": ("staticmethod", False, "context"),
    "get_memory_for": ("staticmethod", False, "context"),
    "_build_member_section": ("function", False, "member_gates"),
    "_build_v2_essentials": ("function", False, "member_gates"),
    "_cap_memo_key": ("staticmethod", False, "agent_spec"),
    "_live_cap_figure": ("staticmethod", False, "agent_spec"),
    "_live_computer_use_gate": ("staticmethod", False, "agent_spec"),
    "_load_agent_prompt": ("staticmethod", False, "agent_spec"),
    "_resolve_agent_prompt": ("function", False, "agent_spec"),
    "_resolve_prompt_templates": ("staticmethod", False, "agent_spec"),
    "_session_cap_figure": ("function", False, "agent_spec"),
    "_session_computer_use_gate": ("function", False, "agent_spec"),
    "_session_reading": ("function", False, "agent_spec"),
    "_substitute_bot_name": ("function", False, "agent_spec"),
    "_dedup_triggered_bodies": ("function", False, "delivery_records"),
    "_forget_shown_lessons": ("function", False, "delivery_records"),
    "_live_shown_lessons": ("function", False, "delivery_records"),
    "_remember_startup_lessons": ("function", False, "delivery_records"),
    "_turn_lessons_block": ("function", False, "delivery_records"),
    "commit_skill_bodies": ("function", False, "delivery_records"),
    "rollback_skill_bodies": ("function", False, "delivery_records"),
    "build_session_context": ("function", False, "session_start"),
    "build_message": ("function", False, "turn_message"),
}

#: The owners whose functions the class binds.
_BINDING_OWNERS = sorted({owner for _, _, owner in _MEMBERS.values() if owner != "context"})


def _owner(stem: str) -> ModuleType:
    return sys.modules[f"{_PACKAGE}.{stem}"]


def _member_function(name: str) -> Any:
    value = vars(ContextBuilder)[name]
    return value.__func__ if isinstance(value, staticmethod) else value


def _tree(module: ModuleType) -> ast.Module:
    return ast.parse(Path(module.__file__ or "").read_text(encoding="utf-8"))


def _bound_functions(stem: str) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """*stem*'s functions the class binds: every ``self``-first function, plus the ones the
    inventory records as static methods owned by *stem*."""
    statics = {
        name
        for name, (kind, _, owner) in _MEMBERS.items()
        if kind == "staticmethod" and owner == stem
    }
    return [
        node
        for node in _tree(_owner(stem)).body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and ((node.args.args and node.args.args[0].arg == "self") or node.name in statics)
    ]


def test_the_member_set_is_the_frozen_inventory() -> None:
    present = {
        name
        for name in vars(ContextBuilder)
        if not (name.startswith("__") and name.endswith("__")) or name == "__init__"
    }
    assert sorted(present ^ set(_MEMBERS)) == []


@pytest.mark.parametrize("name", sorted(_MEMBERS))
def test_each_member_keeps_its_kind_coroutine_and_owner(name: str) -> None:
    kind, coroutine, owner = _MEMBERS[name]
    value = vars(ContextBuilder)[name]
    if kind == "data":
        assert not callable(value), f"{name} is no longer a plain class attribute"
        return
    assert {
        "staticmethod": isinstance(value, staticmethod),
        "function": inspect.isfunction(value),
    }[kind], f"{name} is no longer a {kind}"
    fn = _member_function(name)
    assert inspect.iscoroutinefunction(fn) is coroutine
    if owner == "context":
        assert fn.__module__ == "kiro_crew.context"
        assert fn.__qualname__ == f"ContextBuilder.{name}"
    else:
        assert fn.__module__ == f"{_PACKAGE}.{owner}"
        assert vars(_owner(owner))[name] is fn, f"{name} is not {owner}.{name} itself"


def test_the_bound_members_are_the_owner_functions_through_the_facade() -> None:
    """The facade re-exports nothing in their place: a member reached through
    ``kiro_crew.context.ContextBuilder`` is the owner's own function object."""
    for name, (kind, _, owner) in _MEMBERS.items():
        if owner == "context" or kind == "data":
            continue
        raw = vars(ctx.ContextBuilder)[name]
        fn = raw.__func__ if isinstance(raw, staticmethod) else raw
        assert fn is getattr(_owner(owner), name), name


@pytest.mark.parametrize("stem", _BINDING_OWNERS)
def test_no_binding_owner_keeps_an_unbound_self_function(stem: str) -> None:
    for node in _bound_functions(stem):
        assert node.name in _MEMBERS, f"{stem}.{node.name} is bound on no class"
        assert _MEMBERS[node.name][2] == stem, f"{stem}.{node.name} is bound from elsewhere"
        assert _member_function(node.name) is vars(_owner(stem))[node.name]


def test_the_bound_function_scan_covers_every_moved_member() -> None:
    scanned = {node.name for stem in _BINDING_OWNERS for node in _bound_functions(stem)}
    moved = {name for name, (_, _, owner) in _MEMBERS.items() if owner != "context"}
    assert scanned == moved


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
    fn = tree.body[0]
    assert isinstance(fn, ast.FunctionDef)
    assert sorted(_class_only_constructs(fn)) == ["__class__", "__hidden", "super()"]


def test_the_class_only_scan_allows_dunders_and_explicit_super() -> None:
    tree = ast.parse(
        "def f(self):\n    super(Base, self).g()\n    return self.__dict__, __file__\n"
    )
    fn = tree.body[0]
    assert isinstance(fn, ast.FunctionDef)
    assert _class_only_constructs(fn) == []


@pytest.mark.parametrize("stem", _BINDING_OWNERS)
def test_no_bound_function_needs_its_class_body(stem: str) -> None:
    functions = _bound_functions(stem)
    assert functions, f"{stem} binds nothing: the scan would be vacuous"
    assert {fn.name: _class_only_constructs(fn) for fn in functions} == {
        fn.name: [] for fn in functions
    }
