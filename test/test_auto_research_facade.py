"""The ``handlers`` compatibility facade over the Research Lab campaign engine.

``handlers`` keeps the HTTP adapters and forwards every other historic name to
the ``campaign`` component that binds it. These tests pin the properties that
make that forwarding safe:

* identity -- a forwarded name is the owner's object, and is never also bound
  in ``handlers`` (a second binding would shadow the owner for some callers),
  and ``__all__`` lists every public one for a star import;
* patch reach -- a write or delete through ``handlers`` lands on the owner;
* undo -- ``monkeypatch`` and ``unittest.mock.patch`` (by object or dotted
  path, nested in each other or themselves) restore the owner's binding.
  ``mock.patch(..., create=True)`` of a forwarded name is not supported, because
  its exit deletes the owner's binding. A guard parses the test trees and fails
  any ``patch`` / ``patch.object`` / ``patch.multiple`` call or decorator whose
  target resolves to the facade and whose ``create`` is not literally ``False``
  (see ``_PatchScan`` for the resolved spellings); the allowlist is empty;
* type visibility -- mypy sees each forwarded name imported from its owner,
  because the forwarding ``__getattr__`` is hidden from it;
* layering -- the components form one acyclic stack that never imports the
  facade, reach each other through the module (never a from-import of a
  function) and log under the historic logger name.

The behavioural contract the facade preserves is pinned in
``test_auto_research_campaign_contract.py``.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import logging
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from kiro_crew.apps.builtins.auto_research import handlers as h
from kiro_crew.apps.builtins.auto_research.campaign import (
    LOGGER_NAME,
    lifecycle,
    storage,
    watchdog,
)

_CAMPAIGN = "kiro_crew.apps.builtins.auto_research.campaign"
_CAMPAIGN_DIR = Path(inspect.getsourcefile(storage) or "").resolve().parent
_EMIT_SSE_PATH = "kiro_crew.apps.builtins.auto_research.handlers._emit_sse"

# Bottom to top: a component may import only components listed before it.
_LAYERS = [
    "untrusted",
    "storage",
    "lifecycle",
    "publication",
    "exploration",
    "agent_mode",
    "workflow_mode",
    "watchdog",
    "grill",
]


def _component(name: str):
    return importlib.import_module(f"{_CAMPAIGN}.{name}")


def _component_trees() -> dict[str, ast.Module]:
    return {
        p.stem: ast.parse(p.read_text(encoding="utf-8"))
        for p in sorted(_CAMPAIGN_DIR.glob("*.py"))
        if p.stem != "__init__"
    }


def _facade_tree() -> ast.Module:
    return ast.parse(Path(inspect.getsourcefile(h) or "").read_text(encoding="utf-8"))


def _is_type_checking(test: ast.expr) -> bool:
    return isinstance(test, ast.Name) and test.id == "TYPE_CHECKING"


def _is_not_type_checking(test: ast.expr) -> bool:
    return (
        isinstance(test, ast.UnaryOp)
        and isinstance(test.op, ast.Not)
        and _is_type_checking(test.operand)
    )


class TestIdentity:
    def test_the_facade_module_class_is_installed(self):
        assert type(h).__name__ == "_ReExportModule"

    def test_every_forwarded_name_is_the_owners_object(self):
        wrong = []
        for name, owner_name in h._EXPORTS.items():
            owner = vars(importlib.import_module(owner_name))
            if name not in owner or getattr(h, name) is not owner[name]:
                wrong.append(f"{name} -> {owner_name}")
        assert wrong == []

    def test_each_forwarded_name_has_one_binding(self):
        """A second binding in another component would be missed by a patch
        through the facade, which reaches only the owner."""
        extra = []
        for stem in _LAYERS:
            bound = vars(_component(stem))
            for name, owner_name in h._EXPORTS.items():
                if name in bound and owner_name != f"{_CAMPAIGN}.{stem}":
                    extra.append(f"{name} also bound in {stem}")
        # The status value type is the one sanctioned by-name import.
        assert sorted(e for e in extra if not e.startswith("CampaignStatus ")) == []

    def test_no_forwarded_name_is_also_bound_in_the_facade(self):
        assert sorted(set(h._EXPORTS) & set(vars(h))) == []

    def test_every_owner_is_a_listed_component(self):
        assert set(h._EXPORTS.values()) == {f"{_CAMPAIGN}.{stem}" for stem in _LAYERS}
        assert sorted(p.stem for p in _CAMPAIGN_DIR.glob("*.py")) == sorted(["__init__", *_LAYERS])

    def test_forwarded_names_are_listed_by_dir(self):
        assert set(h._EXPORTS) <= set(dir(h))

    def test_all_is_derived_from_the_module_and_the_table(self):
        """A star import reads ``__all__`` and never reaches ``__getattr__``, so
        the list must carry every public forwarded name; the star import itself
        is pinned in ``test_auto_research_campaign_contract.py``."""
        public = {name for name in set(vars(h)) | set(h._EXPORTS) if not name.startswith("_")}
        assert h.__all__ == sorted(public)
        assert {"LLMPool", "logger", "register_routes"} <= set(h.__all__)
        assert set(h.__all__) <= set(dir(h))

    def test_an_unknown_name_is_still_an_attribute_error(self):
        with pytest.raises(AttributeError, match="has no attribute 'no_such_name'"):
            getattr(h, "no_such_name")
        assert getattr(h, "no_such_name", None) is None


class TestPatchReach:
    def test_monkeypatch_writes_land_on_the_owner_and_restore(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        original = watchdog.POLL_INTERVAL
        with monkeypatch.context() as m:
            m.setattr(h, "POLL_INTERVAL", 0.25)
            assert watchdog.POLL_INTERVAL == 0.25
            assert h.POLL_INTERVAL == 0.25
            assert "POLL_INTERVAL" not in vars(h)
        assert watchdog.POLL_INTERVAL == original

    def test_mock_patch_writes_land_on_the_owner_and_restore(self, tmp_path: Path):
        original = storage.DB_PATH
        with patch.object(h, "DB_PATH", tmp_path / "x.db"):
            assert storage.DB_PATH == tmp_path / "x.db"
            assert storage.db_path() == tmp_path / "x.db"
        assert storage.DB_PATH == original
        with patch("kiro_crew.apps.builtins.auto_research.handlers.RESEARCH_DIR", tmp_path):
            assert storage.research_dir() == tmp_path
        assert "RESEARCH_DIR" not in vars(h)

    @pytest.mark.asyncio
    async def test_a_patched_collaborator_reaches_callers_in_other_components(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """The slot-key helper is used by ``agent_mode`` and ``watchdog``; one
        patch through the facade must reach both callers."""
        looked_up: list[str] = []
        svc = SimpleNamespace(
            get_by_slot=lambda key: looked_up.append(key), remove=AsyncMock(), update=AsyncMock()
        )
        monkeypatch.setattr(h, "_autonudge_instance", lambda: svc)
        monkeypatch.setattr(h, "research_slot_key", lambda cid: f"patched-{cid}")
        monkeypatch.setattr(h, "_campaign_run_is_current", lambda _cid, _started: False)
        await h._stop_loop("0123abcd", remove=True)
        await h._settle_campaign_from_watchdog("0123abcd", [], {}, {}, observed_started_at=1.0)
        assert looked_up == ["patched-0123abcd", "patched-0123abcd"]

    def test_a_delete_is_forwarded_too(self, monkeypatch: pytest.MonkeyPatch):
        original = watchdog.POLL_INTERVAL
        with monkeypatch.context() as m:
            m.delattr(h, "POLL_INTERVAL")
            assert not hasattr(watchdog, "POLL_INTERVAL")
            assert not hasattr(h, "POLL_INTERVAL")
        assert watchdog.POLL_INTERVAL == original

    def test_facade_owned_names_bind_normally(self, monkeypatch: pytest.MonkeyPatch):
        marker = object()
        monkeypatch.setattr(h, "LLMPool", marker)
        assert vars(h)["LLMPool"] is marker


class TestUndo:
    """``mock.patch`` ends a patch of a name absent from ``handlers.__dict__`` with
    ``delattr`` and then, without ``create=True``, sets the original back;
    ``monkeypatch`` writes the remembered value back. Both leave the owner as they
    found it."""

    def test_patch_object_restores_the_owner(self):
        original = lifecycle._emit_sse
        replacement = Mock()
        with patch.object(h, "_emit_sse", replacement):
            assert lifecycle._emit_sse is replacement
        assert lifecycle._emit_sse is original

    def test_patch_by_dotted_path_restores_the_owner(self):
        original = lifecycle._emit_sse
        with patch(_EMIT_SSE_PATH) as replacement:
            assert lifecycle._emit_sse is replacement
        assert lifecycle._emit_sse is original

    def test_nested_patches_restore_one_level_at_a_time(self):
        original = lifecycle._emit_sse
        outer, inner = Mock(), Mock()
        with patch.object(h, "_emit_sse", outer):
            with patch(_EMIT_SSE_PATH, inner):
                assert lifecycle._emit_sse is inner
            assert lifecycle._emit_sse is outer
        assert lifecycle._emit_sse is original

    def test_nested_monkeypatch_undo_restores_the_owner(self, monkeypatch: pytest.MonkeyPatch):
        original = watchdog.POLL_INTERVAL
        first, second = object(), object()
        with monkeypatch.context() as m:
            m.setattr(h, "POLL_INTERVAL", first)
            m.setattr(h, "POLL_INTERVAL", second)
            assert watchdog.POLL_INTERVAL is second
        assert watchdog.POLL_INTERVAL is original

    def test_a_patch_inside_a_monkeypatch_restores_both_levels(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        original = lifecycle._emit_sse
        first = Mock()
        with monkeypatch.context() as m:
            m.setattr(h, "_emit_sse", first)
            with patch.object(h, "_emit_sse", Mock()):
                pass
            assert lifecycle._emit_sse is first
        assert lifecycle._emit_sse is original

    def test_a_monkeypatch_inside_a_patch_restores_both_levels(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        original = lifecycle._emit_sse
        outer = Mock()
        with patch.object(h, "_emit_sse", outer):
            with monkeypatch.context() as m:
                m.setattr(h, "_emit_sse", Mock())
            assert lifecycle._emit_sse is outer
        assert lifecycle._emit_sse is original

    def test_a_monkeypatch_delete_is_real_and_its_undo_restores(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        original = watchdog.POLL_INTERVAL
        with monkeypatch.context() as m:
            m.delattr(h, "POLL_INTERVAL")
            assert not hasattr(watchdog, "POLL_INTERVAL")
        assert watchdog.POLL_INTERVAL is original


_HANDLERS = "kiro_crew.apps.builtins.auto_research.handlers"
_REPO = Path(__file__).resolve().parents[1]
_SRC = _REPO / "src"
_DYNAMIC = "<dynamic>"

#: Deliberate ``create=True`` patches of a forwarded name, keyed by (path relative
#: to the repository, enclosing test). Empty: no test needs one, and the scan
#: must equal this set exactly.
_CREATE_PATCH_ALLOWLIST: frozenset[tuple[str, str]] = frozenset()

#: The patch callables, by the dotted name they resolve to. ``mocker`` is the
#: pytest-mock fixture, which is a parameter rather than an import.
_PATCH_CALLABLES = frozenset({"unittest.mock.patch", "mock.patch", "mocker.patch"})
#: ``create``'s position in each form's signature, for a positional ``create``.
_CREATE_POSITION = {"patch": 3, "object": 4, "multiple": 2}
#: ``patch.multiple`` keywords that configure the patch rather than name a target.
_MULTIPLE_OPTIONS = frozenset({"create", "spec", "spec_set", "autospec", "new_callable"})


@dataclass(frozen=True, order=True)
class _CreatePatch:
    path: str
    function: str
    line: int
    name: str


class _PatchScan:
    """Find ``create``-patches of forwarded names in one source file.

    Targets are resolved from AST nodes, never from unparsed text. A local name
    is resolved through the file's imports (including relative ones) and through
    simple assignments to a fixed point, so ``importlib.import_module(...)``,
    ``sys.modules[...]`` and ``x = y`` aliases of the facade are all the facade.
    A string target may be a literal, a name bound to one, an f-string or a
    ``+`` concatenation whose parts resolve, where ``{module.__name__}`` is that
    module's dotted name. When the target is the facade but the patched name
    cannot be read statically, the hit is ``<dynamic>`` rather than dropped.
    """

    def __init__(self, tree: ast.Module, package: str | None) -> None:
        self.tree = tree
        self.bound: dict[str, str] = {}
        self.strings: dict[str, str] = {}
        self._bind_imports(package)
        self._bind_assignments()

    def _bind_imports(self, package: str | None) -> None:
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.asname:
                        self.bound[alias.asname] = alias.name
                    else:
                        head = alias.name.split(".", 1)[0]
                        self.bound[head] = head
            elif isinstance(node, ast.ImportFrom):
                module = self._absolute(node, package)
                if module is None:
                    continue
                for alias in node.names:
                    self.bound[alias.asname or alias.name] = f"{module}.{alias.name}"

    @staticmethod
    def _absolute(node: ast.ImportFrom, package: str | None) -> str | None:
        if not node.level:
            return node.module
        if package is None:
            return None
        parts = package.split(".")
        if node.level - 1 >= len(parts):
            return None
        base = parts[: len(parts) - (node.level - 1)]
        return ".".join(base + ([node.module] if node.module else []))

    def _bind_assignments(self) -> None:
        assignments = [
            (target.id, node.value)
            for node in ast.walk(self.tree)
            if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None
            for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
            if isinstance(target, ast.Name)
        ]
        for _ in range(2 * len(assignments) + 1):
            changed = False
            for name, value in assignments:
                dotted = self.dotted(value)
                if dotted is not None:
                    changed |= self._bind(self.bound, name, dotted)
                text, complete = self.text(value)
                if complete:
                    changed |= self._bind(self.strings, name, text)
            if not changed:
                return

    @staticmethod
    def _bind(table: dict[str, str], name: str, value: str) -> bool:
        """Bind ``name`` once, rebinding it only to a facade spelling, so the
        fixed point is reached in a few passes and a name that is ever the
        facade stays the facade."""
        current = table.get(name)
        if current is None or (value.startswith(_HANDLERS) and not current.startswith(_HANDLERS)):
            table[name] = value
            return True
        return False

    def dotted(self, node: ast.expr) -> str | None:
        """The dotted name of the module or object ``node`` refers to, if known."""
        if isinstance(node, ast.Name):
            if node.id in self.bound:
                return self.bound[node.id]
            return "mocker" if node.id == "mocker" else None
        if isinstance(node, ast.Attribute):
            base = self.dotted(node.value)
            return f"{base}.{node.attr}" if base else None
        if isinstance(node, ast.Call) and node.args:
            if self.dotted(node.func) in {"importlib.import_module", "sys.modules.get"}:
                text, complete = self.text(node.args[0])
                return text if complete else None
        if isinstance(node, ast.Subscript) and self.dotted(node.value) == "sys.modules":
            text, complete = self.text(node.slice)
            return text if complete else None
        return None

    def text(self, node: ast.expr) -> tuple[str, bool]:
        """The string ``node`` evaluates to: (known leading text, whether complete)."""
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value, True
        if isinstance(node, ast.Name) and node.id in self.strings:
            return self.strings[node.id], True
        if isinstance(node, ast.Attribute) and node.attr == "__name__":
            dotted = self.dotted(node.value)
            if dotted is not None:
                return dotted, True
        if isinstance(node, ast.JoinedStr):
            known = ""
            for part in node.values:
                if isinstance(part, ast.FormattedValue):
                    piece, complete = self.text(part.value)
                    if not complete or part.format_spec is not None:
                        return known + piece, False
                    known += piece
                elif isinstance(part, ast.Constant):
                    known += str(part.value)
            return known, True
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left, complete = self.text(node.left)
            if not complete:
                return left, False
            right, complete = self.text(node.right)
            return left + right, complete
        return "", False

    def _patch_kind(self, call: ast.Call) -> str | None:
        func = self.dotted(call.func)
        if func in _PATCH_CALLABLES:
            return "patch"
        if func is not None:
            base, _, form = func.rpartition(".")
            if base in _PATCH_CALLABLES and form in ("object", "multiple"):
                return form
        return None

    @staticmethod
    def _argument(call: ast.Call, index: int, keyword: str) -> ast.expr | None:
        for kw in call.keywords:
            if kw.arg == keyword:
                return kw.value
        return call.args[index] if len(call.args) > index else None

    def _creates(self, call: ast.Call, kind: str) -> bool:
        create = self._argument(call, _CREATE_POSITION[kind], "create")
        if create is None:
            # ``**kwargs`` may carry ``create``; its value cannot be read.
            return any(kw.arg is None for kw in call.keywords)
        return not (isinstance(create, ast.Constant) and create.value is False)

    def _dotted_target_name(self, node: ast.expr | None) -> str | None:
        if node is None:
            return None
        text, complete = self.text(node)
        if complete:
            module, _, name = text.rpartition(".")
            return name if module == _HANDLERS else None
        # A target that starts with the facade and then turns dynamic still names
        # one of its attributes, unless it goes a level deeper.
        if not text.startswith(_HANDLERS):
            return None
        rest = text[len(_HANDLERS) :]
        if rest == "" or (rest.startswith(".") and "." not in rest[1:]):
            return _DYNAMIC
        return None

    def _is_facade(self, node: ast.expr | None) -> bool:
        if node is None:
            return False
        if self.dotted(node) == _HANDLERS:
            return True
        text, complete = self.text(node)
        return complete and text == _HANDLERS

    def _patched_names(self, call: ast.Call, kind: str) -> list[str]:
        if kind == "patch":
            name = self._dotted_target_name(self._argument(call, 0, "target"))
            return [name] if name else []
        if not self._is_facade(self._argument(call, 0, "target")):
            return []
        if kind == "object":
            attribute = self._argument(call, 1, "attribute")
            if attribute is None:
                return [_DYNAMIC]
            text, complete = self.text(attribute)
            return [text if complete else _DYNAMIC]
        names = [kw.arg for kw in call.keywords if kw.arg and kw.arg not in _MULTIPLE_OPTIONS]
        if any(kw.arg is None for kw in call.keywords):
            names.append(_DYNAMIC)
        return names

    def hits(self, forwarded: set[str]) -> list[tuple[str, int, str]]:
        found = []
        for function, call in _calls_with_enclosing_function(self.tree):
            kind = self._patch_kind(call)
            if kind is None or not self._creates(call, kind):
                continue
            for name in self._patched_names(call, kind):
                if name == _DYNAMIC or name in forwarded:
                    found.append((function, call.lineno, name))
        return sorted(found, key=lambda hit: hit[1])


def _calls_with_enclosing_function(tree: ast.Module) -> list[tuple[str, ast.Call]]:
    """Every call, with the dotted name of the function or class around it. A
    decorator is a child of the function it decorates, so it belongs to that test."""
    found: list[tuple[str, ast.Call]] = []

    def visit(node: ast.AST, scope: tuple[str, ...]) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            scope = (*scope, node.name)
        if isinstance(node, ast.Call):
            found.append((".".join(scope) or "<module>", node))
        for child in ast.iter_child_nodes(node):
            visit(child, scope)

    visit(tree, ())
    return found


def _package_of(path: Path) -> str | None:
    """The package a file under ``src/`` belongs to, for its relative imports."""
    try:
        parts = path.relative_to(_SRC).with_suffix("").parts
    except ValueError:
        return None
    return ".".join(parts[:-1])


def _scan_source(source: str, forwarded: set[str], package: str | None = None) -> list[str]:
    return [name for _, _, name in _PatchScan(ast.parse(source), package).hits(forwarded)]


def _test_sources() -> list[Path]:
    roots = [_REPO / "test", *(p for p in _SRC.rglob("tests") if p.is_dir())]
    return sorted({path for root in roots for path in root.rglob("*.py")})


def _repository_create_patches() -> list[_CreatePatch]:
    forwarded = set(h._EXPORTS)
    found = []
    for path in _test_sources():
        source = path.read_text(encoding="utf-8", errors="replace")
        # Every form the scan resolves names a patch callable and reaches the
        # facade by a spelling that contains ``auto_research``, or by a relative
        # import from inside that package.
        in_package = "auto_research" in path.parts
        if "patch" not in source or not (in_package or "auto_research" in source):
            continue
        scan = _PatchScan(ast.parse(source), _package_of(path))
        relative = path.relative_to(_REPO).as_posix()
        found += [_CreatePatch(relative, *hit) for hit in scan.hits(forwarded)]
    return sorted(found)


_FACADE_IMPORT = "from kiro_crew.apps.builtins.auto_research import handlers as h\n"
#: Prepended to every self-test source: a bare ``patch`` must be the imported one.
_PATCH_IMPORT = "from unittest.mock import patch\n"
_OWNER = "kiro_crew.apps.builtins.auto_research.campaign.lifecycle"

# (source, the names the scan must report); one must-flag case per form.
_MUST_FLAG = {
    "patch.object": (_FACADE_IMPORT + "patch.object(h, '_emit_sse', create=True)", ["_emit_sse"]),
    "dotted literal": (f"patch('{_HANDLERS}._emit_sse', create=True)", ["_emit_sse"]),
    "aliased patch": (
        "from unittest.mock import patch as p\n"
        + _FACADE_IMPORT
        + "p.object(h, 'POLL_INTERVAL', create=True)",
        ["POLL_INTERVAL"],
    ),
    "aliased patch called directly": (
        f"from unittest.mock import patch as _patch\n_patch('{_HANDLERS}._emit_sse', create=True)",
        ["_emit_sse"],
    ),
    "aliased mock module": (
        f"from unittest import mock as m\nm.patch('{_HANDLERS}._emit_sse', create=True)",
        ["_emit_sse"],
    ),
    "imported mock module": (
        "import unittest.mock as um\n"
        + _FACADE_IMPORT
        + "um.patch.multiple(h, POLL_INTERVAL=1, create=True)",
        ["POLL_INTERVAL"],
    ),
    "unaliased mock module": (
        f"import unittest.mock\nunittest.mock.patch('{_HANDLERS}.DB_PATH', create=True)",
        ["DB_PATH"],
    ),
    "mocker fixture": (
        _FACADE_IMPORT
        + "def test_x(mocker):\n    mocker.patch.object(h, '_emit_sse', create=True)",
        ["_emit_sse"],
    ),
    "decorator": (
        _FACADE_IMPORT + "@patch.object(h, '_emit_sse', create=True)\ndef test_x(_m):\n    pass",
        ["_emit_sse"],
    ),
    "f-string __name__": (
        _FACADE_IMPORT + "patch(f'{h.__name__}._emit_sse', create=True)",
        ["_emit_sse"],
    ),
    "f-string constant": (
        f"_MOD = '{_HANDLERS}'\npatch(f'{{_MOD}}._emit_sse', create=True)",
        ["_emit_sse"],
    ),
    "concatenation": (
        f"_MOD = '{_HANDLERS}'\npatch(_MOD + '._emit_sse', create=True)",
        ["_emit_sse"],
    ),
    "__name__ concatenation": (
        _FACADE_IMPORT + "patch(h.__name__ + '._emit_sse', create=True)",
        ["_emit_sse"],
    ),
    "target keyword": (f"patch(target='{_HANDLERS}._emit_sse', create=True)", ["_emit_sse"]),
    "attribute keyword": (
        _FACADE_IMPORT + "patch.object(target=h, attribute='_emit_sse', create=True)",
        ["_emit_sse"],
    ),
    "string multiple target": (
        f"patch.multiple('{_HANDLERS}', POLL_INTERVAL=1, create=True)",
        ["POLL_INTERVAL"],
    ),
    "non-literal create": (f"patch('{_HANDLERS}._emit_sse', create=flag)", ["_emit_sse"]),
    "positional create": (f"patch('{_HANDLERS}._emit_sse', None, None, True)", ["_emit_sse"]),
    "create in **kwargs": (
        _FACADE_IMPORT + "patch.object(h, '_emit_sse', **options)",
        ["_emit_sse"],
    ),
    "import_module alias": (
        f"import importlib\nmod = importlib.import_module('{_HANDLERS}')\nalias = mod\n"
        "patch.object(alias, '_emit_sse', create=True)",
        ["_emit_sse"],
    ),
    "from-imported import_module": (
        f"from importlib import import_module\nmod = import_module('{_HANDLERS}')\n"
        "patch.object(mod, '_emit_sse', create=True)",
        ["_emit_sse"],
    ),
    "sys.modules alias": (
        f"import sys\nmod = sys.modules['{_HANDLERS}']\npatch.object(mod, '_emit_sse', create=True)",
        ["_emit_sse"],
    ),
    "dynamic attribute": (_FACADE_IMPORT + "patch.object(h, name, create=True)", [_DYNAMIC]),
    "dynamic dotted": (
        f"_MOD = '{_HANDLERS}'\npatch(f'{{_MOD}}.{{name}}', create=True)",
        [_DYNAMIC],
    ),
    "dynamic multiple": (_FACADE_IMPORT + "patch.multiple(h, **names, create=True)", [_DYNAMIC]),
}

# (source, package) the scan must report nothing for; one per form above.
_MUST_IGNORE = {
    "create=False": (_FACADE_IMPORT + "patch.object(h, '_emit_sse', create=False)", None),
    "no create": (_FACADE_IMPORT + "patch.object(h, '_emit_sse', Mock())", None),
    "facade-owned name": (_FACADE_IMPORT + "patch.object(h, 'LLMPool', create=True)", None),
    "aliased patch on the owner": (
        f"from unittest.mock import patch as _patch\n_patch('{_OWNER}._emit_sse', create=True)",
        None,
    ),
    "owner, not facade": (f"patch('{_OWNER}._emit_sse', create=True)", None),
    "other module aliased h": (
        "from somewhere import other as h\npatch.object(h, '_emit_sse', create=True)",
        None,
    ),
    "unrelated patch callable": (
        _FACADE_IMPORT + "def patch(*a, **k): pass\nfake.patch.object(h, '_emit_sse', create=True)",
        None,
    ),
    "owner f-string": (
        f"from {_OWNER.rsplit('.', 1)[0]} import lifecycle\n"
        "patch(f'{lifecycle.__name__}._emit_sse', create=True)",
        None,
    ),
    "owner concatenation": (f"_MOD = '{_OWNER}'\npatch(_MOD + '._emit_sse', create=True)", None),
    "keyword forms, create=False": (
        _FACADE_IMPORT + "patch.object(target=h, attribute='_emit_sse', create=False)",
        None,
    ),
    "owner import_module alias": (
        f"import importlib\nmod = importlib.import_module('{_OWNER}')\n"
        "patch.object(mod, '_emit_sse', create=True)",
        None,
    ),
    "dynamic attribute on the owner": (
        f"from {_OWNER.rsplit('.', 1)[0]} import lifecycle\n"
        "patch.object(lifecycle, name, create=True)",
        None,
    ),
    "deeper attribute of the facade": (f"patch('{_HANDLERS}.web.get', create=True)", None),
    "relative import elsewhere": (
        "from .. import handlers\npatch.object(handlers, '_emit_sse', create=True)",
        "kiro_crew.apps.builtins.spec_builder.tests",
    ),
}


class TestNoCreatePatchOfAForwardedName:
    """``mock.patch(..., create=True)`` of a forwarded name deletes the owner's
    binding when it ends, so no test may do it."""

    def test_no_test_patches_a_forwarded_name_with_create(self):
        hits = _repository_create_patches()
        assert sorted({(hit.path, hit.function) for hit in hits}) == sorted(
            _CREATE_PATCH_ALLOWLIST
        ), hits
        assert len(hits) == len(_CREATE_PATCH_ALLOWLIST)

    def test_the_scan_covers_both_test_trees(self):
        sources = {path.relative_to(_REPO).as_posix() for path in _test_sources()}
        assert "test/test_auto_research_facade.py" in sources
        assert "src/kiro_crew/apps/builtins/auto_research/tests/test_windows_support.py" in sources

    def test_a_relative_import_resolves_to_the_facade(self):
        source = _PATCH_IMPORT + (
            "from .. import handlers\npatch.object(handlers, '_emit_sse', create=True)"
        )
        package = _package_of(
            _SRC / "kiro_crew/apps/builtins/auto_research/tests/test_windows_support.py"
        )
        assert package == "kiro_crew.apps.builtins.auto_research.tests"
        assert _scan_source(source, set(h._EXPORTS), package) == ["_emit_sse"]

    def test_a_hit_names_its_enclosing_test(self):
        source = (_PATCH_IMPORT + _FACADE_IMPORT) + (
            "class TestX:\n"
            "    @patch.object(h, 'DB_PATH', create=True)\n"
            "    def test_y(self, _m):\n"
            "        patch.object(h, '_emit_sse', create=True)\n"
        )
        hits = _PatchScan(ast.parse(source), None).hits(set(h._EXPORTS))
        assert hits == [("TestX.test_y", 4, "DB_PATH"), ("TestX.test_y", 6, "_emit_sse")]

    @pytest.mark.parametrize("form", sorted(_MUST_FLAG))
    def test_the_scan_flags_each_form(self, form: str):
        source, expected = _MUST_FLAG[form]
        assert _scan_source(_PATCH_IMPORT + source, set(h._EXPORTS)) == expected

    @pytest.mark.parametrize("form", sorted(_MUST_IGNORE))
    def test_the_scan_ignores_each_near_miss(self, form: str):
        source, package = _MUST_IGNORE[form]
        assert _scan_source(_PATCH_IMPORT + source, set(h._EXPORTS), package) == []


class TestTheFacadesOwnCodeReadsTheOwner:
    """A function defined in the facade resolves a bare global through the facade's
    own namespace, which ``__getattr__`` never answers, so it must reach a
    forwarded name as ``component.name`` instead."""

    @staticmethod
    def _bare_loads(source: str) -> list[tuple[int, str]]:
        tree = ast.parse(source)
        import_lines: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                import_lines.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
        return [
            (node.lineno, node.id)
            for node in ast.walk(tree)
            if isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Load)
            and node.id in h._EXPORTS
            and node.lineno not in import_lines
        ]

    def test_the_facade_reads_no_forwarded_name_as_a_bare_global(self):
        source = Path(inspect.getsourcefile(h) or "").read_text(encoding="utf-8")
        assert self._bare_loads(source) == []

    def test_the_check_can_fail(self):
        source = (
            "if TYPE_CHECKING:\n"
            "    from x import _emit_sse\n"
            "    y: CampaignStatus\n"
            "def f():\n"
            "    return _emit_sse()\n"
        )
        assert self._bare_loads(source) == [(3, "CampaignStatus"), (5, "_emit_sse")]


class TestLoadedOwnersAreReadWithoutImporting:
    def test_reads_and_writes_do_not_call_import_module(self, monkeypatch: pytest.MonkeyPatch):
        """A loaded owner comes from ``sys.modules``; ``import_module`` answers only
        a miss, so a test patching it for its own reasons reroutes nothing here."""
        original = watchdog.POLL_INTERVAL
        marker = object()
        with patch("importlib.import_module", side_effect=AssertionError("import_module")):
            assert h.POLL_INTERVAL is original
            with monkeypatch.context() as m:
                m.setattr(h, "POLL_INTERVAL", marker)
                assert watchdog.POLL_INTERVAL is marker
            assert watchdog.POLL_INTERVAL is original


class TestTypeVisibility:
    def test_the_mirrored_owner_ratchet_sees_the_facade(self):
        """``test_mirrored_owner_storage`` finds the modules it binds by shape;
        hiding ``__getattr__`` from type checkers must not hide the facade."""
        from test_mirrored_owner_storage import mirrors_a_surface, owner_module_tables

        assert mirrors_a_surface(_facade_tree())
        assert owner_module_tables(_facade_tree()) == []

    def test_the_forwarding_getattr_is_hidden_from_type_checkers(self):
        """Visible, it would make mypy type every ``handlers.<name>`` as ``Any``."""
        tree = _facade_tree()
        defined = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "__getattr__"
        ]
        guarded = [
            node
            for block in tree.body
            if isinstance(block, ast.If)
            for node in (
                block.body
                if _is_not_type_checking(block.test)
                else block.orelse if _is_type_checking(block.test) else []
            )
            if isinstance(node, ast.FunctionDef) and node.name == "__getattr__"
        ]
        assert len(defined) == 1
        assert guarded == defined

    def test_type_checkers_import_each_forwarded_name_from_its_owner(self):
        imported = [
            (alias.name if alias.asname is None else f"{alias.name} as {alias.asname}", node.module)
            for block in _facade_tree().body
            if isinstance(block, ast.If) and _is_type_checking(block.test)
            for node in block.body
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
        ]
        assert sorted(imported) == sorted(h._EXPORTS.items())


class TestLayering:
    def test_components_never_import_the_facade(self):
        offenders = []
        for stem, tree in _component_trees().items():
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    names = {a.name for a in node.names}
                    if node.module.endswith("auto_research.handlers") or (
                        node.module.endswith("auto_research") and "handlers" in names
                    ):
                        offenders.append(stem)
                elif isinstance(node, ast.Import):
                    if any(a.name.endswith("auto_research.handlers") for a in node.names):
                        offenders.append(stem)
        assert offenders == []

    def test_components_form_one_acyclic_stack(self):
        edges = {}
        for stem, tree in _component_trees().items():
            deps = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module == _CAMPAIGN:
                    deps |= {a.name for a in node.names if a.name in _LAYERS}
                elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                    f"{_CAMPAIGN}."
                ):
                    deps.add((node.module or "").rsplit(".", 1)[-1])
            edges[stem] = deps
        upward = sorted(
            f"{stem} -> {dep}"
            for stem, deps in edges.items()
            for dep in deps
            if _LAYERS.index(dep) >= _LAYERS.index(stem)
        )
        assert upward == []

    def test_cross_component_references_go_through_the_module(self):
        """Only the ``CampaignStatus`` value type is imported by name; every
        function and mutable module state is reached as ``component.name``."""
        by_name = []
        for stem, tree in _component_trees().items():
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                    f"{_CAMPAIGN}."
                ):
                    names = [a.name for a in node.names]
                    if names != ["CampaignStatus"] or node.module != f"{_CAMPAIGN}.storage":
                        by_name.append(f"{stem}: from {node.module} import {names}")
        assert by_name == []

    @pytest.mark.parametrize("stem", _LAYERS)
    def test_components_log_under_the_historic_logger(self, stem: str):
        logger = vars(_component(stem)).get("logger")
        if logger is not None:
            assert isinstance(logger, logging.Logger)
            assert logger.name == LOGGER_NAME == h.logger.name
