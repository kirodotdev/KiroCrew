"""``SkillsLoader`` keeps every member it had, and each moved body is bound from its owner.

The loader's state stays on the instance; a member whose body moved lives as a module
function of the same name in a ``kiro_crew.skills_<role>`` owner, and the class binds it
(``member = _owner.member``, or ``staticmethod(_owner.member)``). This file pins that
binding contract for the whole facade family:

* the frozen member inventory -- every member, its kind, whether it is a coroutine, and the
  module that owns it (``skills`` for a member the class body still defines);
* each bound member IS its owner's function, and no owner keeps an unbound ``self`` function;
* no bound function uses zero-argument ``super()``, ``__class__`` or a name-mangled
  attribute, which only work inside a class body;
* each owner imports only owners listed before it;
* each owner's docstring names the instance fields its members write and read, and each
  owner logs on the facade's ``kiro_crew.skills`` logger;
* a moved exception or enum keeps the facade's ``__module__``, which a traceback names it
  by, and every other moved class keeps its owner's;
* the facade mirror serves only the two pending-queue hooks, and a patched walk,
  catalog or tool-read bound reaches the owner that reads it.

``test_facade_mirror_guards.py`` checks, for every mirrored facade, ``kiro_crew.skills``
included, that the family holds one object per name, that no owner imports its facade,
and that every name the test trees patch through the facade reaches its readers.

The member signatures themselves are pinned by ``test_skills_composition_contract.py``.
"""

from __future__ import annotations

import ast
import enum
import inspect
import logging
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

import kiro_crew.skills as sk
from kiro_crew import _facade_mirror
from kiro_crew.skills import SkillsLoader
from kiro_crew.testing.wait import wait_until

pytestmark = pytest.mark.xdist_group(name="tree_scan_skills_bindings")

#: Member -> (kind, coroutine, owning module). Frozen at the split: the kind and coroutine
#: flag are BASE's; the owner is where the member's body lives now.
_MEMBERS: dict[str, tuple[str, object, str]] = {
    "_ALLOWED_CANDIDATE_TOP": ("data", False, ""),
    "__init__": ("function", False, "skills"),
    "_adopt_extra_paths": ("function", False, "skills_discovery"),
    "_adopt_snapshot": ("function", False, "skills"),
    "_append_project_skill_bodies": ("function", False, "skills"),
    "_archive_root": ("function", False, "skills"),
    "_audit_project_skill_enforcement": ("function", False, "skills_body_reads"),
    "_auto_activity": ("function", False, "skills"),
    "_auto_created_ts": ("function", False, "skills"),
    "_auto_slug_available": ("function", False, "skills"),
    "_auto_slug_claim_lock": ("function", False, "skills"),
    "_auto_slug_from_name": ("staticmethod", False, "skills"),
    "_body_hits": ("function", False, "skills"),
    "_body_matches": ("function", False, "skills"),
    "_cached_frontmatter": ("function", False, "skills_body_reads"),
    "_candidate_has_symlink": ("staticmethod", False, "skills_candidate_tree"),
    "_candidate_layout_findings_at": ("function", False, "skills_candidate_tree"),
    "_candidate_layout_ok": ("function", False, "skills_candidate_tree"),
    "_catalog_fingerprint_hint": ("function", False, "skills"),
    "_catalog_fingerprints_for": ("staticmethod", False, "skills"),
    "_catalog_scope_id": ("function", False, "skills"),
    "_catalog_scope_key": ("function", False, "skills"),
    "_catalog_worker_loop": ("function", False, "skills"),
    "_collect_scripts": ("staticmethod", False, "skills_candidate_tree"),
    "_collect_scripts_pinned": ("function", False, "skills_candidate_tree"),
    "_confined_frontmatter_and_size": ("function", False, "skills_body_reads"),
    "_create_skill_pinned": ("function", False, "skills"),
    "_cron_referenced_skills": ("staticmethod", False, "skills"),
    "_emit_lazy_load_metric": ("staticmethod", False, "skills_body_reads"),
    "_exact_read_while_building": ("function", False, "skills"),
    "_get_disabled_app_names": ("function", False, "skills"),
    "_invalidate_iter_cache": ("function", False, "skills"),
    "_is_pending_slug_safe": ("staticmethod", False, "skills"),
    "_is_user_authored": ("function", False, "skills_selection"),
    "_iter": ("function", False, "skills"),
    "_iter_uncached": ("function", False, "skills"),
    "_iter_visible": ("function", False, "skills"),
    "_key_denotes_path": ("staticmethod", False, "skills"),
    "_legacy_context": ("function", False, "skills"),
    "_load_catalog_snapshot": ("function", False, "skills"),
    "_max_triggered_now": ("function", False, "skills_selection"),
    "_on_config_change": ("function", True, "skills_discovery"),
    "_owned_hint": ("function", False, "skills"),
    "_owning_app": ("function", False, "skills"),
    "_parse_frontmatter": ("staticmethod", False, "skills"),
    "_parse_frontmatter_text": ("staticmethod", False, "skills"),
    "_pending_root": ("function", False, "skills"),
    "_pending_scripts_verdict": ("function", False, "skills_review"),
    "_pending_scripts_verdict_at": ("function", False, "skills_review"),
    "_prune_versions": ("function", False, "skills"),
    "_rank_key": ("function", False, "skills"),
    "_read_candidate_pinned": ("function", False, "skills_review"),
    "_read_enumerated_skill_bytes": ("function", False, "skills_body_reads"),
    "_read_exact_key": ("function", False, "skills_body_reads"),
    "_read_global_skill_text": ("function", False, "skills_body_reads"),
    "_read_pending_meta": ("function", False, "skills_review"),
    "_readable_frontmatter": ("function", False, "skills"),
    "_recency_boost": ("function", False, "skills"),
    "_record_use": ("function", False, "skills"),
    "_redact_deep": ("function", False, "skills_review"),
    "_redact_file_in_place": ("function", False, "skills_review"),
    "_redact_text": ("staticmethod", False, "skills_review"),
    "_redact_validation_report": ("function", False, "skills_review"),
    "_repo_scope_satisfied": ("staticmethod", False, "skills_selection"),
    "_request_catalog_refresh": ("function", False, "skills"),
    "_resolve_path": ("function", False, "skills"),
    "_resolve_path_and_root": ("function", False, "skills"),
    "_resolve_snapshot_version": ("function", False, "skills"),
    "_rewrite_update_frontmatter": ("staticmethod", False, "skills"),
    "_run_catalog_build": ("function", False, "skills"),
    "_safe_name": ("staticmethod", False, "skills"),
    "_scoped_entries": ("function", False, "skills"),
    "_screen_extra_paths": ("staticmethod", False, "skills_discovery"),
    "_served_key_by_realpath": ("function", False, "skills"),
    "_short_desc": ("staticmethod", False, "skills"),
    "_snapshot_admitted_roots": ("function", False, "skills"),
    "_trusted_project_key": ("function", False, "skills_body_reads"),
    "_usage_fields": ("function", False, "skills"),
    "_user_first": ("function", False, "skills_selection"),
    "_validate_and_redact_candidate": ("function", False, "skills_review"),
    "_versions_root": ("function", False, "skills"),
    "_vet_unconfined_path": ("function", False, "skills"),
    "_write_skill_md": ("staticmethod", False, "skills"),
    "approve_pending_as_read": ("function", False, "skills_review"),
    "approve_pending_skill_checked": ("function", False, "skills_review"),
    "approve_pending_update_checked": ("function", False, "skills_review"),
    "archive_auto_skill": ("function", False, "skills"),
    "catalog_project_skills": ("function", False, "skills_review"),
    "catalog_status": ("function", False, "skills"),
    "close": ("function", False, "skills"),
    "confined_triggered": ("function", False, "skills"),
    "create_auto_skill": ("function", False, "skills"),
    "create_skill": ("function", False, "skills"),
    "credit_skill_reads": ("function", False, "skills"),
    "delete_skill": ("function", False, "skills"),
    "dismiss_all_pending": ("function", False, "skills"),
    "dismiss_pending_skill": ("function", False, "skills"),
    "dismiss_pending_slugs": ("function", False, "skills"),
    "find_similar": ("function", False, "skills"),
    "get_always_skills": ("function", False, "skills_selection"),
    "get_auto_skill_version": ("function", False, "skills"),
    "get_context": ("function", False, "skills"),
    "get_pending_skill": ("function", False, "skills_review"),
    "get_triggered_skills": ("function", False, "skills_selection"),
    "has_dollar_candidate": ("staticmethod", False, "skills"),
    "is_auto_generated": ("function", False, "skills"),
    "list_archived_auto_skills": ("function", False, "skills"),
    "list_auto_skills": ("function", False, "skills"),
    "list_pending_skills": ("function", False, "skills_review"),
    "list_skills": ("function", False, "skills"),
    "load_skill": ("function", False, "skills_body_reads"),
    "pending_candidate_is_staged": ("function", False, "skills"),
    "preview_pending_update": ("function", False, "skills_review"),
    "prune_pending": ("function", False, "skills"),
    "read_auto_skill_body": ("function", False, "skills"),
    "read_scoped_skill": ("function", False, "skills_body_reads"),
    "read_scoped_skill_page": ("function", False, "skills_body_reads"),
    "reconfigure": ("function", False, "skills_discovery"),
    "resolve_dollar_skills": ("function", False, "skills"),
    "resolve_ledger_aliases": ("function", False, "skills"),
    "resolve_tool_read_keys": ("function", False, "skills"),
    "restore_auto_skill": ("function", False, "skills"),
    "run_skill_lifecycle": ("function", False, "skills"),
    "scoped_skills": ("function", False, "skills_selection"),
    "search_skills": ("function", False, "skills"),
    "search_skills_report": ("function", False, "skills"),
    "set_inject_on_trigger": ("function", False, "skills"),
    "set_pinned": ("function", False, "skills"),
    "split_triggered": ("function", False, "skills"),
    "stage_skill_candidate": ("function", False, "skills"),
    "strip_frontmatter": ("staticmethod", False, "skills"),
    "sync_builtins": ("function", False, "skills_builtin_sync"),
    "trigger_hint": ("function", False, "skills"),
    "update_auto_skill": ("function", False, "skills"),
    "update_skill": ("function", False, "skills"),
}


#: The pending-queue hook globals: their setters rebind them in their owner, so the facade
#: serves them from there instead of binding a copy of its own.
#: How long a test waits for a fresh loader's background catalog walk: well over the
#: loader's own two-second cold-start budget and under half the suite's 120 s timeout.
_CATALOG_SETTLE_SECS = 30.0

_SERVED = {
    "_PENDING_CONSUMED_HOOK": "kiro_crew.skills_auto_model",
    "_PENDING_STAGED_HOOK": "kiro_crew.skills_auto_model",
}


def _owners() -> list[ModuleType]:
    return [sys.modules[name] for name in sk._OWNERS]


def _member_function(name: str) -> Any:
    value = vars(SkillsLoader)[name]
    return value.__func__ if isinstance(value, staticmethod) else value


def _tree(module: ModuleType) -> ast.Module:
    return ast.parse(Path(module.__file__ or "").read_text(encoding="utf-8"))


def _self_functions(
    tree: ast.Module, owner: str = ""
) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """The owner's functions the class binds: every ``self``-first function, plus the
    ones the inventory records as static methods of *owner*."""
    statics = {
        name
        for name, (kind, _co, home) in _MEMBERS.items()
        if kind == "staticmethod" and home and f"kiro_crew.{home}" == owner
    }
    return [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and ((node.args.args and node.args.args[0].arg == "self") or node.name in statics)
    ]


def test_the_member_set_is_the_frozen_inventory() -> None:
    present = {
        name
        for name in vars(SkillsLoader)
        if not (name.startswith("__") and name.endswith("__")) or name == "__init__"
    }
    assert sorted(present ^ set(_MEMBERS)) == []


@pytest.mark.parametrize("name", sorted(_MEMBERS))
def test_each_member_keeps_its_kind_shape_and_owner(name: str) -> None:
    kind, coroutine, owner = _MEMBERS[name]
    value = vars(SkillsLoader)[name]
    if kind == "data":
        assert not callable(value), f"{name} is no longer data"
        return
    assert {
        "staticmethod": isinstance(value, staticmethod),
        "function": inspect.isfunction(value),
    }[kind], f"{name} is no longer a {kind}"
    fn = _member_function(name)
    shape = inspect.iscoroutinefunction(fn) or (inspect.isasyncgenfunction(fn) and "agen")
    assert (shape or False) == coroutine
    assert fn.__module__ == f"kiro_crew.{owner}"
    if owner != "skills":
        assert vars(sys.modules[f"kiro_crew.{owner}"])[name] is fn


def test_every_owner_is_listed_in_the_facade() -> None:
    homes = {owner for _kind, _co, owner in _MEMBERS.values() if owner and owner != "skills"}
    listed = {name.rsplit(".", 1)[-1] for name in sk._OWNERS}
    assert homes <= listed
    assert all(name.startswith("kiro_crew.skills_") for name in sk._OWNERS)


@pytest.mark.parametrize("owner", sk._OWNERS)
def test_no_owner_keeps_an_unbound_self_function(owner: str) -> None:
    module = sys.modules[owner]
    for node in _self_functions(_tree(module), owner):
        assert node.name in _MEMBERS, f"{owner}.{node.name} is bound on no class"
        assert _member_function(node.name) is vars(module)[node.name]


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


@pytest.mark.parametrize("owner", sk._OWNERS)
def test_no_bound_function_needs_its_class_body(owner: str) -> None:
    functions = _self_functions(_tree(sys.modules[owner]), owner)
    assert {fn.name: _class_only_constructs(fn) for fn in functions} == {
        fn.name: [] for fn in functions
    }


def _owner_imports(tree: ast.Module) -> set[str]:
    """The ``skills_*`` owner modules *tree* imports, by dotted name."""
    owners = set(sk._OWNERS)
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module in owners:
                found.add(node.module)
            elif node.module == "kiro_crew":
                found.update(
                    f"kiro_crew.{a.name}" for a in node.names if f"kiro_crew.{a.name}" in owners
                )
        elif isinstance(node, ast.Import):
            found.update(a.name for a in node.names if a.name in owners)
    return found


def test_the_owner_import_scan_can_fail() -> None:
    tree = ast.parse(
        "from kiro_crew.skills_discovery import _within_any\n"
        "from kiro_crew import skills_review as _review, pinned_fs\n"
        "import kiro_crew.skills_selection\n"
    )
    assert _owner_imports(tree) == {
        "kiro_crew.skills_discovery",
        "kiro_crew.skills_review",
        "kiro_crew.skills_selection",
    }


def test_each_owner_imports_only_owners_listed_before_it() -> None:
    """``_OWNERS`` lists the owners lowest layer first: an owner imports only owners
    earlier in the tuple, so the order is the import order."""
    owners = sk._OWNERS
    later = {
        owner: sorted(_owner_imports(_tree(sys.modules[owner])) - set(owners[:i]))
        for i, owner in enumerate(owners)
    }
    assert later == {owner: [] for owner in owners}


#: Calls on ``self.<field>`` that change the field's value in place.
_MUTATORS = frozenset(
    {"add", "discard", "append", "pop", "update", "clear", "setdefault", "move_to_end", "popitem"}
)


def _self_field(node: ast.AST) -> str | None:
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        if node.value.id == "self":
            return node.attr
    return None


def _fields(fns: list[ast.FunctionDef | ast.AsyncFunctionDef]) -> tuple[set[str], set[str]]:
    """The ``self`` fields *fns* write and read. Writes: a store or delete of the
    attribute or of a subscript on it, and a mutator call on it. Reads: every other load."""
    written: set[str] = set()
    read: set[str] = set()
    for fn in fns:
        for node in ast.walk(fn):
            name = _self_field(node)
            if name is not None:
                (written if isinstance(node.ctx, (ast.Store, ast.Del)) else read).add(name)
            if isinstance(node, ast.Subscript) and isinstance(node.ctx, (ast.Store, ast.Del)):
                target = _self_field(node.value)
                if target is not None:
                    written.add(target)
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute) and func.attr in _MUTATORS:
                    target = _self_field(func.value)
                    if target is not None:
                        written.add(target)
    return written - set(_MEMBERS), read - written - set(_MEMBERS)


def test_the_field_scan_counts_every_read_and_write_shape() -> None:
    tree = ast.parse(
        "def f(self):\n"
        "    self.a = 1\n    del self.b\n    self.c[k] = v\n    del self.d[k]\n"
        "    self.e.add(1)\n    self.f.setdefault(k, v)\n"
        "    return self.h, self.e, self.load_skill\n"
    )
    fn = tree.body[0]
    assert isinstance(fn, ast.FunctionDef)
    assert _fields([fn]) == ({"a", "b", "c", "d", "e", "f"}, {"h"})


def _listed(doc: str, label: str) -> set[str]:
    match = re.search(rf"{label}: (.*?)\.(?:\n\n|\n?$)", doc, re.S)
    return set(re.findall(r"``(\w+)``", match.group(1))) if match else set()


@pytest.mark.parametrize("owner", sk._OWNERS)
def test_each_owner_names_the_fields_it_writes_and_reads(owner: str) -> None:
    module = sys.modules[owner]
    written, read = _fields(_self_functions(_tree(module), owner))
    doc = module.__doc__ or ""
    assert _listed(doc, "Instance fields written") == written
    assert _listed(doc, "Instance fields read") == read


@pytest.mark.parametrize("owner", sk._OWNERS)
def test_owners_log_under_the_facade_logger(owner: str) -> None:
    """caplog and filters key on ``kiro_crew.skills``; an owner's own ``__name__`` logger
    is a sibling of it, not a child, so its DEBUG records would fall outside them."""
    module = sys.modules[owner]
    assert getattr(module, "logger", sk.logger) is sk.logger
    assert sk.logger is logging.getLogger("kiro_crew.skills")


def _moved_classes() -> dict[str, tuple[type, str]]:
    """Every class an owner defines at module level -> the class and its owner."""
    found: dict[str, tuple[type, str]] = {}
    for module in _owners():
        for node in _tree(module).body:
            if isinstance(node, ast.ClassDef):
                found[node.name] = (vars(module)[node.name], module.__name__)
    return found


def test_a_moved_exception_keeps_the_facade_module_name() -> None:
    """A traceback, and an error chain rendered as ``module.qualname``, names a raised class
    by its ``__module__``, so a moved exception or enum keeps ``kiro_crew.skills``. A moved
    data class keeps its owner's: no repr, log line or pickle in the tree reads it."""
    classes = _moved_classes()
    named = {
        name
        for name, (cls, _home) in classes.items()
        if issubclass(cls, (BaseException, enum.Enum))
    }
    assert named == {"PendingApprovalRefused"}
    for name, (cls, home) in sorted(classes.items()):
        assert cls.__module__ == ("kiro_crew.skills" if name in named else home), name


# --------------------------------------------------------------------------- #
# The facade mirror
# --------------------------------------------------------------------------- #


def _index() -> _facade_mirror.MirrorIndex:
    index = vars(sk)[_facade_mirror.INDEX_ATTRIBUTE]
    assert isinstance(index, _facade_mirror.MirrorIndex)
    return index


def test_the_mirror_serves_only_the_two_hooks() -> None:
    index = _index()
    assert type(sk) is _facade_mirror.MirroredModule
    assert index.owners == sk._OWNERS
    assert dict(index.served) == _SERVED
    assert sk._EXPORTS == _SERVED


def test_every_served_name_is_defined_by_its_owner_and_declared_for_type_checkers() -> None:
    """A served name is an owner's own definition (no name imported from elsewhere is
    served), and the facade's ``TYPE_CHECKING`` block imports it from that owner."""
    manifest: dict[str, str] = {}
    for node in _tree(sk).body:
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING":
            for sub in node.body:
                if isinstance(sub, ast.ImportFrom) and sub.module:
                    manifest.update({alias.name: sub.module for alias in sub.names})
    assert manifest == _SERVED
    for name, owner in _SERVED.items():
        assert name in _facade_mirror._defined_names(_tree(sys.modules[owner]))
        assert name not in vars(sk)


def test_a_hook_global_is_read_and_written_on_its_one_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = sys.modules["kiro_crew.skills_auto_model"]
    seen: list[dict] = []
    monkeypatch.setattr(sk, "_PENDING_STAGED_HOOK", sk._PENDING_STAGED_HOOK)
    sk.set_pending_staged_hook(seen.append)
    assert sk._PENDING_STAGED_HOOK is owner._PENDING_STAGED_HOOK
    assert "_PENDING_STAGED_HOOK" not in vars(sk)
    sk._emit_pending_staged({"slug": "s"})
    assert seen == [{"slug": "s"}]


def test_a_patched_walk_bound_reaches_the_walk_that_owns_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_PROJECT_SKILL_MAX_DEPTH`` lives in ``skills_discovery`` and is read there as a
    global; a patch through the facade must still stop the confined walk's descent."""
    if not sk.skill_trust.project_skill_traversal_supported():
        pytest.skip("the confined walk needs descriptor-relative no-follow traversal")
    project = tmp_path / "proj"
    nested = project / ".kiro" / "skills" / "family" / "leaf"
    nested.mkdir(parents=True)
    (nested / "SKILL.md").write_text("---\nname: leaf\n---\nbody\n", encoding="utf-8")
    base = project / ".kiro" / "skills"

    def names() -> list[str]:
        return [name for name, _path in sk._iter_skill_files(base, confine_to=(str(project),))]

    assert names() == ["family/leaf"]
    monkeypatch.setattr(sk, "_PROJECT_SKILL_MAX_DEPTH", 1)
    assert sys.modules["kiro_crew.skills_discovery"]._PROJECT_SKILL_MAX_DEPTH == 1
    assert names() == []


def test_a_patched_catalog_bound_reaches_the_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_COLD_CATALOG_WAIT_SECS`` lives in ``skills_discovery`` and ``skill_runtime.catalog``
    reads it through the facade; a patch through the facade reaches both. With no budget
    the cold path must not wait at all, so the waits observed tell the two values apart."""
    loader = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)
    try:
        waits: list[float | None] = []

        class _NeverDone:
            def wait(self, timeout: float | None = None) -> bool:
                waits.append(timeout)
                return False

        monkeypatch.setattr(loader, "_request_catalog_refresh", lambda _key: _NeverDone())
        monkeypatch.setattr(sk, "_COLD_CATALOG_WAIT_SECS", 0.0)
        assert sys.modules["kiro_crew.skills_discovery"]._COLD_CATALOG_WAIT_SECS == 0.0
        assert loader._iter() == []
        assert loader.catalog_status() == "building"
        assert waits == []
    finally:
        loader.close()


def test_a_patched_tool_read_bound_reaches_the_read_credit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_CONTENT_READ_TOOLS`` lives in ``skills_body_reads`` and ``skill_runtime.read_credit``
    reads it through the facade; a patch through the facade decides which tool reads.

    The catalog is settled first: a fresh root's first enumeration waits only the cold
    budget for the background walk, and a loaded runner can outlast it. ``_iter`` does
    not read ``_CONTENT_READ_TOOLS``, so the patch alone decides the answers below."""
    root = tmp_path / "skills"
    skill = root / "alpha" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: alpha\ndescription: a\n---\nbody\n", encoding="utf-8")
    loader = SkillsLoader(skills_path=root, install_builtins=False)
    try:

        def _listed() -> bool:
            loader._iter()
            return loader.catalog_status() == "complete"

        wait_until(
            _listed,
            timeout=_CATALOG_SETTLE_SECS,
            describe=lambda: f"catalog_status={loader.catalog_status()!r}",
        )
        assert loader.resolve_tool_read_keys("planted_reader", {"path": str(skill)}) == []
        monkeypatch.setattr(sk, "_CONTENT_READ_TOOLS", frozenset({"planted_reader"}))
        assert sys.modules["kiro_crew.skills_body_reads"]._CONTENT_READ_TOOLS == {"planted_reader"}
        assert loader.resolve_tool_read_keys("planted_reader", {"path": str(skill)}) == ["alpha"]
    finally:
        loader.close()
