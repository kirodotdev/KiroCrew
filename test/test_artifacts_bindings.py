"""``ArtifactStore`` keeps every member it had, and each is bound from the module that owns it.

The store's code lives in the ``kiro_crew.artifacts_*`` owner modules: a moved member's
body is a module-level function there whose first parameter is ``self``, and the class
binds it by name (``m = _owner.m``, or ``staticmethod(_owner.f)``). The instance state
stays on the instance, so tests and callers see the same object they always did. This
file pins that contract:

* the frozen member inventory -- every member, its kind, its signature (the ``self``
  annotation aside) and the module that owns it;
* each bound member IS its owner's function, and no owner keeps an unbound ``self`` function;
* no bound function uses zero-argument ``super()``, ``__class__`` or a name-mangled
  attribute, which only work inside a class body;
* no owner imports the facade at run time, and an owner imports only owners listed
  before it in ``_OWNERS``;
* each owner's docstring names the instance fields its members read, and each owner
  logs under the facade's logger;
* the facade's mirror (``_facade_mirror``): one object per indexed name, the forwarding
  table unchanged, and a write through ``kiro_crew.artifacts`` reaching every owner that
  reads the name, for every name the test trees patch there;
* the forwarding a read, write, delete, ``hasattr`` or ``dir()`` of a forwarded name
  gets through the facade.
"""

from __future__ import annotations

import ast
import dataclasses
import hashlib
import inspect
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from kiro_crew import _facade_mirror
from kiro_crew import artifacts as art_mod
from kiro_crew.artifacts import ArtifactStore

pytestmark = pytest.mark.xdist_group(name="tree_scan_artifacts_bindings")

#: Member -> (kind, signature digest, owning module). Frozen at the split: the kind and
#: the signature are BASE's; the owner is where the member lives now.
_MEMBERS: dict[str, tuple[str, str, str]] = {
    "_MUTABLE_COMMENT_FIELDS": ("data", "", ""),
    "__init__": ("function", "2714247256ac", "artifacts"),
    "_append_event": ("function", "e4f38696ed41", "artifacts_disk"),
    "_artifact_dir": ("function", "b906b114b39d", "artifacts_disk"),
    "_claim_slug": ("function", "d730801fb7db", "artifacts_disk"),
    "_ensure_content_token_locked": ("function", "34a16185d6fc", "artifacts_disk"),
    "_fire_change": ("function", "8d892e37af44", "artifacts"),
    "_is_live_pointer": ("staticmethod", "90d383534c00", "artifacts"),
    "_is_sweepable_auto_widget": ("function", "41cf3dd63c4f", "artifacts_scans"),
    "_iter_meta_paths": ("function", "81741035df94", "artifacts_disk"),
    "_load_comments": ("function", "f427f0240431", "artifacts_comments"),
    "_load_meta": ("function", "9eb144e76c6f", "artifacts_disk"),
    "_mkdirs": ("function", "182915e53775", "artifacts"),
    "_parse_image_metadata": ("staticmethod", "436e10c38abd", "artifact_store.records"),
    "_parse_publication": ("staticmethod", "5515129f5dd3", "artifact_store.records"),
    "_prune_versions": ("function", "896f3c762f11", "artifacts_disk"),
    "_read_bytes": ("function", "28ad0b76c969", "artifacts_disk"),
    "_read_image_asset_bytes": ("function", "28ad0b76c969", "artifacts_disk"),
    "_read_meta_file": ("function", "201a7da6e6c3", "artifacts_disk"),
    "_read_text": ("function", "cb1683d46405", "artifacts_disk"),
    "_refuse_write_outside_root": ("function", "63c8b896d7a8", "artifacts_disk"),
    "_rescan_comment_anchors_locked": ("function", "62dd383602a3", "artifacts_comments"),
    "_rmtree": ("staticmethod", "826b485bf8d1", "artifacts_disk"),
    "_rotate_content_token_locked": ("function", "4ee404357605", "artifacts_disk"),
    "_snapshot_version": ("function", "60b2445bf44b", "artifacts_disk"),
    "_try_read_source_path": ("function", "fb4032ddaa7b", "artifacts_source_link"),
    "_try_write_source_path": ("function", "1fc470d7808e", "artifacts_source_link"),
    "_unique_slug": ("function", "252deca9f37d", "artifacts_disk"),
    "_write_artifact": ("function", "6308ae1dcd3d", "artifacts_disk"),
    "_write_bytes": ("function", "f5bc83a2c3fa", "artifacts_disk"),
    "_write_comments": ("function", "be7bb4326467", "artifacts_comments"),
    "_write_image_artifact": ("function", "c4418b073cc9", "artifacts_disk"),
    "_write_meta": ("function", "34a16185d6fc", "artifacts_disk"),
    "_write_settled_draft": ("function", "b7caf2f36c67", "artifacts_writes"),
    "_write_text": ("function", "d92fc3a7222c", "artifacts_disk"),
    "add_comment": ("function", "6b133a71159a", "artifacts_comments"),
    "allowed_source_roots": ("function", "4c8ae5b2fe43", "artifacts_source_link"),
    "artifact_index_key": ("staticmethod", "06db94384f74", "artifacts_scans"),
    "clear_publication": ("function", "0feb3239807e", "artifacts"),
    "create": ("function", "8800112eea34", "artifacts_writes"),
    "create_image": ("function", "bf9291cc3a6d", "artifacts_writes"),
    "delete": ("function", "43b04068284b", "artifacts"),
    "delete_comment": ("function", "18734594b0c6", "artifacts_comments"),
    "find_by_artifact_id": ("function", "cb40acb30ee5", "artifacts_scans"),
    "find_by_source_path": ("function", "036457a8b626", "artifacts_scans"),
    "get": ("function", "5a684f593b69", "artifacts"),
    "index_by_artifact_id": ("function", "8cab7a4db01b", "artifacts_scans"),
    "list": ("function", "2ce7f1b1e967", "artifacts"),
    "list_comments": ("function", "f427f0240431", "artifacts_comments"),
    "list_versions": ("function", "2a1d3d0212c8", "artifacts_disk"),
    "mark_webapp_expired": ("function", "9eb144e76c6f", "artifacts"),
    "merge_remote_comments": ("function", "225583aaba8e", "artifacts_comments"),
    "migrate_kinds": ("function", "87bd98a2dfb3", "artifacts_scans"),
    "prune_auto_widgets": ("function", "29a2f4ccf889", "artifacts_scans"),
    "read_image_bytes": ("function", "bcb9ebe64b40", "artifacts"),
    "record_comment_event": ("function", "893a73d5e3c6", "artifacts_comments"),
    "record_impression": ("function", "476a8f52640a", "artifacts"),
    "relocate": ("function", "2326ce4c23cc", "artifacts_source_link"),
    "root": ("property", "b08d8e6a45bd", "artifacts"),
    "set_change_listener": ("function", "4d10301fe268", "artifacts"),
    "set_folder": ("function", "32716b91c358", "artifacts"),
    "set_fork_metadata": ("function", "5a4f908b4a9e", "artifacts"),
    "set_pinned": ("function", "66425dd2ce5a", "artifacts"),
    "set_publication": ("function", "0403ef194407", "artifacts"),
    "settle_blank": ("function", "043dda7da009", "artifacts_writes"),
    "unmark_webapp_expired": ("function", "9eb144e76c6f", "artifacts"),
    "update": ("function", "7a3af5d14e5b", "artifacts_writes"),
    "update_comment": ("function", "3f14f2247459", "artifacts_comments"),
    "update_fork_metadata": ("function", "1f932bc5c33b", "artifacts"),
    "update_publication": ("function", "1f932bc5c33b", "artifacts"),
}

#: The fields every store keeps on the instance, set by ``__init__``.
_STATE_FIELDS = frozenset({"_root", "_lock", "_owner_only", "_change_listener"})


def _owners() -> list[ModuleType]:
    return [sys.modules[name] for name in art_mod._OWNERS]


def _signature_digest(fn: Any) -> str:
    sig = inspect.signature(fn)
    params = list(sig.parameters.values())
    if params and params[0].name == "self":
        params[0] = params[0].replace(annotation=inspect.Parameter.empty)
    return hashlib.sha256(str(sig.replace(parameters=params)).encode()).hexdigest()[:12]


def _member_function(name: str) -> Any:
    value = vars(ArtifactStore)[name]
    if isinstance(value, staticmethod):
        return value.__func__
    if isinstance(value, property):
        return value.fget
    return value


def _tree(module: ModuleType) -> ast.Module:
    return ast.parse(Path(module.__file__ or "").read_text(encoding="utf-8"))


def _self_functions(tree: ast.Module, owner: str = "") -> list[ast.FunctionDef]:
    """The owner's functions the class binds: every ``self``-first function, plus the
    ones the inventory records as static methods of *owner*."""
    statics = {
        name
        for name, (kind, _, home) in _MEMBERS.items()
        if kind == "staticmethod" and f"kiro_crew.{home}" == owner
    }
    return [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and ((node.args.args and node.args.args[0].arg == "self") or node.name in statics)
    ]


# --------------------------------------------------------------------------- #
# The member inventory and the bindings.
# --------------------------------------------------------------------------- #


def test_the_member_set_is_the_frozen_inventory() -> None:
    present = {
        name
        for name in vars(ArtifactStore)
        if not (name.startswith("__") and name.endswith("__")) or name == "__init__"
    }
    assert sorted(present ^ set(_MEMBERS)) == []


@pytest.mark.parametrize("name", sorted(_MEMBERS))
def test_each_member_keeps_its_kind_signature_and_owner(name: str) -> None:
    kind, digest, owner = _MEMBERS[name]
    value = vars(ArtifactStore)[name]
    if kind == "data":
        assert not callable(value)
        return
    assert {
        "staticmethod": isinstance(value, staticmethod),
        "property": isinstance(value, property),
        "function": inspect.isfunction(value),
    }[kind], f"{name} is no longer a {kind}"
    fn = _member_function(name)
    assert not inspect.iscoroutinefunction(fn)
    assert _signature_digest(fn) == digest, f"{name}{inspect.signature(fn)} changed signature"
    assert fn.__module__ == f"kiro_crew.{owner}"
    if owner.startswith("artifacts_"):
        assert vars(sys.modules[f"kiro_crew.{owner}"])[name] is fn


def test_every_owner_holding_a_member_is_listed_in_the_facade() -> None:
    homes = {
        f"kiro_crew.{home}"
        for _kind, _d, home in _MEMBERS.values()
        if home.startswith("artifacts_")
    }
    assert homes == set(art_mod._OWNERS)


@pytest.mark.parametrize("owner", art_mod._OWNERS)
def test_no_owner_keeps_an_unbound_self_function(owner: str) -> None:
    module = sys.modules[owner]
    for node in _self_functions(_tree(module), owner):
        assert node.name in _MEMBERS, f"{owner}.{node.name} is bound on no class"
        assert _member_function(node.name) is vars(module)[node.name]


def _class_only_constructs(fn: ast.FunctionDef) -> list[str]:
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


@pytest.mark.parametrize("owner", art_mod._OWNERS)
def test_no_bound_function_needs_its_class_body(owner: str) -> None:
    functions = _self_functions(_tree(sys.modules[owner]), owner)
    assert functions
    assert {fn.name: _class_only_constructs(fn) for fn in functions} == {
        fn.name: [] for fn in functions
    }


def _runtime_facade_imports(tree: ast.Module) -> list[int]:
    """Lines that import ``kiro_crew.artifacts`` outside a ``TYPE_CHECKING`` block."""
    guarded: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING":
            guarded.update(id(inner) for inner in ast.walk(node))
    lines: list[int] = []
    for node in ast.walk(tree):
        if id(node) in guarded:
            continue
        if isinstance(node, ast.ImportFrom):
            if node.module == art_mod.__name__ or (
                node.module == "kiro_crew" and any(a.name == "artifacts" for a in node.names)
            ):
                lines.append(node.lineno)
        elif isinstance(node, ast.Import):
            if any(a.name == art_mod.__name__ for a in node.names):
                lines.append(node.lineno)
    return lines


def test_the_facade_import_scan_can_fail() -> None:
    tree = ast.parse(
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n    from kiro_crew.artifacts import ArtifactStore\n"
        "def f():\n    from kiro_crew.artifacts import x\n"
        "import kiro_crew.artifacts\n"
        "from kiro_crew import artifacts\n"
    )
    assert sorted(_runtime_facade_imports(tree)) == [5, 6, 7]


@pytest.mark.parametrize("owner", art_mod._OWNERS)
def test_no_owner_imports_the_facade_at_run_time(owner: str) -> None:
    assert _runtime_facade_imports(_tree(sys.modules[owner])) == []


def _owner_imports(tree: ast.Module) -> set[str]:
    """The store's owner modules *tree* imports, by dotted name."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith("kiro_crew.artifacts_"):
                found.add(node.module)
            elif node.module == "kiro_crew":
                found.update(
                    f"kiro_crew.{a.name}" for a in node.names if a.name.startswith("artifacts_")
                )
        elif isinstance(node, ast.Import):
            found.update(a.name for a in node.names if a.name.startswith("kiro_crew.artifacts_"))
    return found


def test_the_owner_import_scan_can_fail() -> None:
    tree = ast.parse(
        "from kiro_crew.artifacts_disk import X\n"
        "from kiro_crew import artifacts_scans as _s, hooks\n"
        "import kiro_crew.artifacts_writes\n"
    )
    assert _owner_imports(tree) == {
        "kiro_crew.artifacts_disk",
        "kiro_crew.artifacts_scans",
        "kiro_crew.artifacts_writes",
    }


def test_each_owner_imports_only_owners_listed_before_it() -> None:
    """``_OWNERS`` lists the owners lowest layer first: an owner imports only owners
    earlier in the tuple, so the order is the import order."""
    owners = art_mod._OWNERS
    later = {
        owner: sorted(_owner_imports(_tree(sys.modules[owner])) - set(owners[:i]))
        for i, owner in enumerate(owners)
    }
    assert later == {owner: [] for owner in owners}


def _fields_read(fns: list[ast.FunctionDef]) -> set[str]:
    """The instance fields (``__init__``'s state, or a class attribute read through
    ``self``) that *fns* read; a store or delete of one is reported as ``=<name>``."""
    names = _STATE_FIELDS | {n for n, (kind, _, _) in _MEMBERS.items() if kind == "data"}
    found: set[str] = set()
    for fn in fns:
        for node in ast.walk(fn):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "self"
                and node.attr in names
            ):
                found.add(node.attr if isinstance(node.ctx, ast.Load) else f"={node.attr}")
    return found


def test_the_field_scan_reports_reads_and_writes() -> None:
    tree = ast.parse("def f(self):\n    self._lock = 1\n    return self._root, self.other\n")
    fn = tree.body[0]
    assert isinstance(fn, ast.FunctionDef)
    assert _fields_read([fn]) == {"=_lock", "_root"}


@pytest.mark.parametrize("owner", art_mod._OWNERS)
def test_each_owner_names_the_fields_it_reads_and_writes_none_of_them(owner: str) -> None:
    module = sys.modules[owner]
    read = _fields_read(_self_functions(_tree(module), owner))
    doc = module.__doc__ or ""
    match = re.search(r"Instance fields read: (.*?)\.\n", doc, re.S)
    assert match is not None, f"{owner} names no instance fields"
    assert set(re.findall(r"``(\w+)``", match.group(1))) == read
    assert "Instance fields written: none." in doc


@pytest.mark.parametrize("owner", art_mod._OWNERS)
def test_owners_log_under_the_facade_logger(owner: str) -> None:
    assert vars(sys.modules[owner])["logger"] is art_mod.logger


# --------------------------------------------------------------------------- #
# The mirror: a write through the facade reaches every owner that reads the name.
# --------------------------------------------------------------------------- #


def _patched_through_the_facade() -> set[str]:
    import test_artifacts_refactor_store_contract as contract

    return contract._corpus_patched_names()


def _reads(module: ModuleType) -> set[str]:
    tree = _tree(module)
    imports = {
        line
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for line in range(node.lineno, (node.end_lineno or node.lineno) + 1)
    }
    return {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name)
        and isinstance(node.ctx, ast.Load)
        and node.lineno not in imports
    }


def _reach_gaps(patched: set[str], index: Any) -> list[str]:
    gaps = []
    for module in _owners():
        for name in sorted(patched & _reads(module)):
            if name in vars(module) and module.__name__ not in index.holders.get(name, ()):
                gaps.append(f"{module.__name__}.{name}")
    return gaps


def test_every_name_patched_through_the_facade_reaches_the_owners_reading_it() -> None:
    patched = _patched_through_the_facade()
    # The scan sees the seams the moved code reads: the clock, a cap, a fence helper.
    assert {"_now_iso", "MAX_VERSIONS", "_open_pinned_for_read", "_validate_slug"} <= patched
    reached = {
        name for module in _owners() for name in patched & _reads(module) if name in vars(module)
    }
    assert {"_now_iso", "MAX_VERSIONS", "_open_pinned_for_read"} <= reached
    assert _reach_gaps(patched, art_mod._MIRROR) == []


def test_the_reach_scan_can_fail() -> None:
    index = art_mod._MIRROR
    owner = "kiro_crew.artifacts_writes"
    holders = dict(index.holders)
    holders["_now_iso"] = tuple(h for h in holders["_now_iso"] if h != owner)
    blind = dataclasses.replace(index, holders=holders)
    assert _reach_gaps({"_now_iso"}, index) == []
    assert _reach_gaps({"_now_iso"}, blind) == [f"{owner}._now_iso"]


def test_the_mirror_indexes_one_object_per_name_and_keeps_the_forwarding_table() -> None:
    index = art_mod._MIRROR
    assert type(art_mod) is _facade_mirror.MirroredModule
    assert vars(art_mod)[_facade_mirror.INDEX_ATTRIBUTE] is index
    assert index.owners == art_mod._OWNERS
    assert index.excluded == ()
    assert index.divergent() == ()
    assert set(index.holders).isdisjoint(art_mod._EXPORTS)
    # The forwarding table is the one the facade declares, and the mirror serves nothing
    # else: every name the facade bound before is still bound on it.
    assert dict(index.forwards) == art_mod._FORWARDS == art_mod._EXPORTS
    assert dict(index.exports) == {}
    assert index.forward_reads == "sys_modules_first"


def test_a_patch_through_the_facade_lands_on_every_holder_and_is_undone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    holders = [sys.modules[name] for name in art_mod._MIRROR.holders["_now_iso"]]
    assert len(holders) >= 2
    before = art_mod._now_iso

    def frozen() -> str:
        return "2026-01-02T03:04:05.000006+00:00"

    with monkeypatch.context() as patch:
        patch.setattr(art_mod, "_now_iso", frozen)
        assert vars(art_mod)["_now_iso"] is frozen
        assert [vars(owner)["_now_iso"] for owner in holders] == [frozen] * len(holders)
    assert vars(art_mod)["_now_iso"] is before
    assert [vars(owner)["_now_iso"] for owner in holders] == [before] * len(holders)


# --------------------------------------------------------------------------- #
# Forwarded names: what a read, write, delete, ``hasattr`` and ``dir()`` get.
# --------------------------------------------------------------------------- #


def _forward(name: str = "MAX_TAGS") -> tuple[str, ModuleType]:
    return name, sys.modules[art_mod._EXPORTS[name]]


def test_a_forwarded_read_answers_from_the_owner_when_it_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    name, owner = _forward()
    assert getattr(art_mod, name) is vars(owner)[name]
    sentinel = object()
    monkeypatch.setattr(owner, name, sentinel)
    assert getattr(art_mod, name) is sentinel
    assert name not in vars(art_mod)


def test_a_read_of_an_unknown_name_raises_the_plain_module_error() -> None:
    with pytest.raises(AttributeError) as raised:
        getattr(art_mod, "_not_a_store_name")
    assert str(raised.value) == "module 'kiro_crew.artifacts' has no attribute '_not_a_store_name'"
    assert getattr(art_mod, "_not_a_store_name", None) is None
    assert not hasattr(art_mod, "_not_a_store_name")


def test_a_forwarded_name_deleted_from_its_owner_reads_as_missing_on_the_facade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``hasattr`` and ``getattr(..., default)`` answer as for a missing name; the error
    names the facade the read went through."""
    name, owner = _forward()
    monkeypatch.delattr(owner, name)
    assert not hasattr(art_mod, name)
    assert getattr(art_mod, name, None) is None
    with pytest.raises(AttributeError) as raised:
        getattr(art_mod, name)
    assert str(raised.value) == f"module 'kiro_crew.artifacts' has no attribute '{name}'"


@pytest.mark.parametrize("name", sorted(art_mod._EXPORTS))
def test_every_forwarded_name_is_listed_and_readable(name: str) -> None:
    assert hasattr(art_mod, name)
    assert name in dir(art_mod)
    assert name not in vars(art_mod)


def test_dir_lists_the_bindings_and_the_forwarding_table() -> None:
    assert dir(art_mod) == sorted(set(vars(art_mod)) | set(art_mod._EXPORTS))


def test_a_forwarded_write_and_delete_reach_the_one_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    name, owner = _forward()
    original = vars(owner)[name]
    with monkeypatch.context() as patch:
        patch.setattr(art_mod, name, 3)
        assert vars(owner)[name] == 3
        assert name not in vars(art_mod)
        assert [h for h, m in art_mod._MIRROR.members_binding(name).items() if m == 3] == [
            owner.__name__
        ]
    assert vars(owner)[name] is original
    with monkeypatch.context() as patch:
        patch.delattr(art_mod, name)
        assert name not in vars(owner)
    assert vars(owner)[name] is original


def test_the_facade_resolver_declines_every_name_the_mirror_serves() -> None:
    resolver = vars(art_mod)["__getattr__"]
    served = sorted(art_mod._MIRROR.served)
    assert served == sorted(art_mod._EXPORTS)
    answered = []
    for name in served:
        try:
            resolver(name)
        except AttributeError:
            continue
        answered.append(name)
    assert answered == []


def test_the_facade_resolver_answers_a_name_added_after_the_install(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    monkeypatch.setitem(art_mod._EXPORTS, "dumps", json.__name__)
    assert vars(art_mod)["__getattr__"]("dumps") is json.dumps


def test_a_name_added_to_the_table_after_the_install_is_forwarded_for_reads_only() -> None:
    """The mirror forwards a write or a delete for the names its install saw. A later
    ``_EXPORTS`` entry is answered on a read by the module ``__getattr__``, and a write of
    it creates the name on the facade and its owners (the mirror's creation rule) instead
    of reaching the entry's module; the delete that undoes the write removes exactly what
    the write created."""
    import json

    sentinel = object()
    with pytest.MonkeyPatch.context() as patch:
        patch.setitem(art_mod._EXPORTS, "dumps", json.__name__)
        assert art_mod.dumps is json.dumps
        art_mod.dumps = sentinel
        try:
            assert vars(art_mod)["dumps"] is sentinel
            assert [vars(owner).get("dumps") for owner in _owners()] == [sentinel] * 5
            assert json.dumps is not sentinel
        finally:
            del art_mod.dumps
        assert art_mod.dumps is json.dumps
    assert "dumps" not in vars(art_mod)
    assert [owner.__name__ for owner in _owners() if "dumps" in vars(owner)] == []


def test_install_refuses_a_facade_resolver_that_answers_a_served_name() -> None:
    def answering(name: str) -> object:
        return object()

    namespace = dict(vars(art_mod))
    namespace.pop(_facade_mirror.INDEX_ATTRIBUTE, None)
    namespace["__getattr__"] = answering
    stand_in = ModuleType(art_mod.__name__)
    stand_in.__dict__.update(namespace)
    with pytest.raises(RuntimeError, match=r"its own module-level __getattr__ answers"):
        _facade_mirror.install(
            stand_in,
            art_mod._OWNERS,
            excluded=(),
            forwards=art_mod._FORWARDS,
            surface=frozenset(namespace),
        )
    assert _facade_mirror.INDEX_ATTRIBUTE not in vars(stand_in)
