"""Checks the ``test_*_bindings.py`` files share, written once and run per file.

Each bindings file pins one class whose members are bound from owner modules. Some of
its checks read only that file's own helpers (``_fields``, ``_members`` ...) and
constants (``_MEMBERS`` ...), so the body is the same in every file. A file opts in by
naming those checks in a module-level ``SHARED_BINDINGS_CHECKS`` tuple; discovery is
:mod:`shared_checks_discovery`.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import shared_checks_discovery

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
