"""Checks the ``test_*_bindings.py`` files share, written once and run per file.

Each bindings file pins one class whose members are bound from owner modules. Some of
its checks read only that file's own helpers (``_fields``, ``_members`` ...),
constants (``_MEMBERS`` ...) or path, so the body is the same in every file. A file opts in by
naming those checks in a module-level ``SHARED_BINDINGS_CHECKS`` tuple; discovery is
:mod:`shared_checks_discovery`.
"""

from __future__ import annotations

import ast
import functools
import importlib
import sys
from pathlib import Path

import pytest
import shared_checks_discovery
import source_corpus

TEST_DIR = Path(__file__).resolve().parent

#: Every check a file can opt into. A name outside this set is a typo that would
#: otherwise opt the file out silently.
KNOWN_CHECKS = frozenset(
    {
        "the_field_scan_counts_every_read_and_write_shape",
        "the_member_set_is_the_frozen_inventory",
        "the_class_only_scan_can_fail",
        "the_resolution_scan_can_fail",
        "nothing_else_in_src_imports_an_owner",
        "the_signature_digest_ignores_an_object_address_in_either_spelling",
        "the_bound_function_scans_include_every_static_method",
        "the_kind_reader_tells_a_coroutine_from_a_function",
        "each_owner_imports_only_owners_listed_before_it",
        "the_class_only_scan_sees_each_shape",
        "each_class_an_owner_defines_carries_its_frozen_module",
        "the_test_tree_walk_reaches_every_collected_tree",
        "leaf_test_scope_derives_this_file_as_a_corpus_gate",
    }
)

_OPTED_IN = shared_checks_discovery.discover(
    TEST_DIR, "test_*_bindings.py", "SHARED_BINDINGS_CHECKS"
)
_each = _OPTED_IN.each
_load = shared_checks_discovery.load


def test_every_opt_in_is_found_parses_and_names_known_checks() -> None:
    """Discovery finds every file naming the tuple, each tuple parses and names only
    known checks, and every known check runs on some file: a discovery that went blind
    would collect zero params and pass."""
    naming = {
        path.stem
        for path in TEST_DIR.glob("test_*_bindings.py")
        if "SHARED_BINDINGS_CHECKS" in path.read_text(encoding="utf-8")
    }
    assert naming == set(_OPTED_IN.checks)
    assert _OPTED_IN.errors == []
    assert _OPTED_IN.unknown(KNOWN_CHECKS) == {}
    assert _OPTED_IN.unused(KNOWN_CHECKS) == []


@_each("the_field_scan_counts_every_read_and_write_shape")
def test_the_field_scan_counts_every_read_and_write_shape(contract: str) -> None:
    subject = _load(contract)
    tree = ast.parse(
        "def f(self):\n"
        "    self.a = 1\n    del self.b\n    self.c[k] = v\n    del self.d[k]\n"
        "    self.e.add(1)\n    self.f.setdefault(k, v)\n    setattr(self, 'g', 1)\n"
        "    return self.h, getattr(self, 'i', None), hasattr(self, 'j'), self.e\n"
    )
    assert subject._fields([tree.body[0]]) == ({"a", "b", "c", "d", "e", "f", "g"}, {"h", "i", "j"})


@_each("the_member_set_is_the_frozen_inventory")
def test_the_member_set_is_the_frozen_inventory(contract: str) -> None:
    subject = _load(contract)
    assert sorted(subject._members()) == sorted(subject._MEMBERS)


@_each("the_class_only_scan_can_fail")
def test_the_class_only_scan_can_fail(contract: str) -> None:
    subject = _load(contract)
    tree = ast.parse("def f(self):\n    super().g()\n    return __class__, self.__hidden\n")
    fn = tree.body[0]
    assert isinstance(fn, ast.FunctionDef)
    assert sorted(subject._class_only_constructs(fn)) == ["__class__", "__hidden", "super()"]


@_each("the_resolution_scan_can_fail")
def test_the_resolution_scan_can_fail(contract: str) -> None:
    subject = _load(contract)

    def reads_a_missing_global(self: object) -> object:
        return _a_name_no_module_binds  # type: ignore[name-defined]  # noqa: F821

    assert subject._unresolved(reads_a_missing_global) == ["_a_name_no_module_binds"]


@_each("nothing_else_in_src_imports_an_owner")
def test_nothing_else_in_src_imports_an_owner(contract: str) -> None:
    subject = _load(contract)
    assert subject._owner_importers(subject._REPO_ROOT / "src") == []


@_each("the_signature_digest_ignores_an_object_address_in_either_spelling")
def test_the_signature_digest_ignores_an_object_address_in_either_spelling(contract: str) -> None:
    subject = _load(contract)

    def f(x: object = None) -> None: ...

    lower = subject._ADDRESS.sub(" at 0x", "(x=<object object at 0x7f1a2b3c4d5e>)")
    upper = subject._ADDRESS.sub(" at 0x", "(x=<object object at 0x000001A9BFA701B0>)")
    assert lower == upper == "(x=<object object at 0x>)"
    assert len(subject._signature_digest(f)) == 12


@_each("the_bound_function_scans_include_every_static_method")
def test_the_bound_function_scans_include_every_static_method(contract: str) -> None:
    subject = _load(contract)
    statics = {
        name: f"{subject._PACKAGE}.{home}"
        for name, (kind, _, _, home) in subject._MEMBERS.items()
        if kind == "staticmethod" and home != subject._CLASS_HOME
    }
    assert statics
    scanned = {
        fn.name
        for owner in set(statics.values())
        for fn in subject._self_functions(subject._tree(sys.modules[owner]), owner)
    }
    assert set(statics) <= scanned


@_each("the_kind_reader_tells_a_coroutine_from_a_function")
def test_the_kind_reader_tells_a_coroutine_from_a_function(contract: str) -> None:
    subject = _load(contract)

    async def stand_in(self: object) -> None:
        return None

    assert (subject._kind(stand_in), subject._kind(lambda self: None)) == ("coroutine", "function")


@_each("each_owner_imports_only_owners_listed_before_it")
def test_each_owner_imports_only_owners_listed_before_it(contract: str) -> None:
    subject = _load(contract)
    owners = subject.store_module._STORE_OWNERS
    later = {
        owner: sorted(
            name
            for name in subject._runtime_imports(subject._tree(sys.modules[owner]))
            if name in owners and name not in owners[:i]
        )
        for i, owner in enumerate(owners)
    }
    assert later == {owner: [] for owner in owners}


@_each("the_class_only_scan_sees_each_shape")
@pytest.mark.parametrize(
    ("source", "flagged"),
    [
        ("def f(self):\n    return super().g()\n", True),
        ("def f(self):\n    return __class__\n", True),
        ("def f(self):\n    return self.__secret\n", True),
        ("def f(self):\n    return super(type(self), self).g()\n", False),
        ("def f(self):\n    return self.__dict__, self._private\n", False),
    ],
)
def test_the_class_only_scan_sees_each_shape(contract: str, source: str, flagged: bool) -> None:
    subject = _load(contract)
    (fn,) = subject._self_functions(ast.parse(source))
    assert bool(subject._class_only_constructs(fn)) is flagged


@_each("each_class_an_owner_defines_carries_its_frozen_module")
def test_each_class_an_owner_defines_carries_its_frozen_module(contract: str) -> None:
    subject = _load(contract)
    found = {
        node.name: vars(module)[node.name].__module__
        for module in subject._owners()
        for node in subject._tree(module).body
        if isinstance(node, ast.ClassDef)
    }
    assert found == subject._CLASS_MODULES


@_each("the_test_tree_walk_reaches_every_collected_tree")
def test_the_test_tree_walk_reaches_every_collected_tree(contract: str) -> None:
    files = _load(contract)._test_tree_files()
    assert TEST_DIR / f"{contract}.py" in files
    assert source_corpus.repo_root() / "conftest.py" in files
    assert any("/apps/builtins/" in path.as_posix() for path in files)
    assert len(files) > 1000


@functools.lru_cache(maxsize=1)
def _corpus_gates() -> frozenset[str]:
    root = source_corpus.repo_root()
    return frozenset(importlib.import_module("leaf_test_scope").corpus_gates(root))


@_each("leaf_test_scope_derives_this_file_as_a_corpus_gate")
def test_leaf_test_scope_derives_this_file_as_a_corpus_gate(
    contract: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The file reads the whole test tree, so a leaf-only diff must still run it."""
    root = source_corpus.repo_root()
    monkeypatch.syspath_prepend(str(root / "scripts"))
    assert (TEST_DIR / f"{contract}.py").relative_to(root).as_posix() in _corpus_gates()
