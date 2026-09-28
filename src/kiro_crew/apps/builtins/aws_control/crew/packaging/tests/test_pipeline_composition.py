"""The crew bundle builder's composition: one facade over the ``pipeline`` owners.

``packaging.build`` was one module, and every caller -- the CLI, the suites in this
directory, the ``python -m packaging.build`` driver -- still reaches the builder through it.
The rules it runs live in ``packaging.pipeline``, one owner per responsibility. Three
properties have to hold for that split to be invisible, and each is pinned here in the
direction that would catch a regression rather than the direction that restates the code.

* Every name the one-module builder bound still resolves on the facade, to the object its
  owner holds (:data:`FROZEN_NAMES`, frozen rather than derived, because a list derived from
  the facade agrees with any facade).
* A write through the facade lands on the owner that defines the name, and reaches every
  caller of it: an owner calls a function another owner defines through that owner's module,
  never through a copy imported by name. So ``monkeypatch.setattr(mod, ...)`` -- a few dozen
  sites in this directory -- patches the builder as it did when it was one module, and its
  undo, like ``mock.patch``'s, puts back exactly what it replaced. A write that may create
  the name cannot round-trip (its undo only deletes), and a write of a name the facade does
  not forward, or of a constant an owner imports by name, reaches no owner that runs it.
  :class:`TestPatchSpellings` reads every write through the facade in the repository's
  tests, in each spelling it resolves, and refuses those.
* The owners form one acyclic stack under the facade and none of them imports it.
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import os
import runpy
import sys
import types
from collections.abc import Iterator
from pathlib import Path
from typing import NamedTuple
from unittest import mock

import pytest

from .test_producer import BUILD_PY, CREW_ROOT, PIPELINE_DIR, builder_owners, load_build

REPO_ROOT = CREW_ROOT.parents[5]
FACADE_NAME = "kiro_crew.apps.builtins.aws_control.crew.packaging.build"
PIPELINE_PACKAGE = "kiro_crew.apps.builtins.aws_control.crew.packaging.pipeline"

#: The owners in the facade's layer order, lowest first.
LAYERS: tuple[str, ...] = (
    "contract",
    "scan",
    "sensitive",
    "pinned",
    "destination",
    "hashing",
    "crew",
    "candidates",
    "plan",
    "prompt",
    "spec",
    "layout",
    "staging",
    "report",
    "transaction",
    "cli",
)

#: Every module-level name the one-module ``build.py`` bound, dunders excluded. A name
#: leaves this list only when the symbol it names is deliberately deleted.
FROZEN_NAMES: tuple[str, ...] = (
    "BUNDLE_VERSION",
    "BuildReport",
    "Callable",
    "Candidate",
    "Drift",
    "ExportRefused",
    "IO",
    "Leak",
    "PLAN_FILENAME",
    "PLAN_VERSION",
    "Path",
    "Plan",
    "PurePosixPath",
    "REPORT_VERSION",
    "ResolvedCrew",
    "SpecResult",
    "_AWS_KEY_PREFIXES",
    "_B64_DECODE_BUDGET",
    "_B64_RUN_RE",
    "_BARE_SECRET_ENTROPY_MIN",
    "_BARE_SECRET_HEX_ONLY_RE",
    "_BARE_SECRET_LEN",
    "_BARE_SECRET_MAX_LOWER_RUN",
    "_BARE_SECRET_MAX_VOWEL_RATIO",
    "_BARE_SECRET_RUN_RE",
    "_BARE_SECRET_VOWELS",
    "_BUILD_WRITES_EMPTY",
    "_BUILTIN_TOOL_GROUPS",
    "_CANONICAL_CREDENTIAL_RE",
    "_CANONICAL_REDACTOR",
    "_CONTAINER_OWNED_MCP",
    "_CREDENTIAL_DIR_PARTS",
    "_CREDENTIAL_NAME_RE",
    "_CapturedTree",
    "_DROPPED_SPEC_KEYS",
    "_HARD_CREDENTIAL_RE",
    "_HARD_LINK_UNSUPPORTED_ERRNOS",
    "_HARD_PATTERNS",
    "_KINDS",
    "_MAX_PROMPT_BYTES",
    "_MAX_REDIRECT_HOPS",
    "_NOFOLLOW_READ_FLAGS",
    "_PLAN_INSTRUCTIONS",
    "_RUN_ID",
    "_SENSITIVE_RELATIVE_DIRS",
    "_STAGING_MARKER_BODY",
    "_STAGING_MARKER_TOKEN",
    "_STAGING_OWNED_TOP_LEVEL",
    "_VENDOR_TOKEN_COMPILED",
    "_VENDOR_TOKEN_PATTERNS",
    "_bare_secret_decodes_to_printable",
    "_bare_secret_window_is_key",
    "_canonical_server",
    "_clean_mcp_server",
    "_cmd_build",
    "_cmd_plan",
    "_copy_skill",
    "_decision_set",
    "_default_config_dir",
    "_default_kiro_home",
    "_denied_list",
    "_dir_fd_closed",
    "_dir_fd_supported",
    "_dispose_via_private_aside",
    "_inline_prompt",
    "_inspect_captured_tree_fd",
    "_is_plain_file_no_follow",
    "_is_redirecting_entry",
    "_is_shape_this_build_never_writes",
    "_looks_sensitive_standalone",
    "_marker_is_ours",
    "_marker_lines_are_this_run",
    "_nofollow_primitive_available",
    "_open_captured_dir_fd",
    "_open_dir_nofollow_pinned",
    "_open_leaf_no_reparse",
    "_open_leaf_nofollow_at",
    "_print_decision",
    "_publish_report",
    "_purge_staging_best_effort",
    "_purge_via_private_aside",
    "_read_bytes_openat",
    "_read_regular_leaf_fd",
    "_read_text",
    "_read_text_nofollow",
    "_read_text_openat",
    "_redirect_between",
    "_refuse_redirects_in_chain",
    "_refuse_report_dir_without_hard_link_support",
    "_refuse_share_reached_through_ancestors",
    "_refuse_unc_out",
    "_refuse_unless_launchable",
    "_refuse_unless_our_report",
    "_refuse_unless_this_build_wrote_it",
    "_refuse_unusable_parent",
    "_refuse_without_nofollow_primitive",
    "_require_plan_include",
    "_resolve_prompt_path",
    "_rmtree_pinned",
    "_scan_bare_secret_runs",
    "_scan_decoded_runs",
    "_sha",
    "_source_from",
    "_staged_tree_hash",
    "_tree_hash",
    "_unlink_out_leaf_best_effort",
    "_validated_crew_name",
    "_verify_build_wrote_captured_fd",
    "_verify_captured_is_staging_fd",
    "_walk_no_reparse",
    "_within",
    "_write_bytes_nofollow",
    "_write_guarded",
    "_write_marker_exclusive",
    "_write_nofollow",
    "annotations",
    "argparse",
    "base64",
    "build_bundle",
    "build_spec",
    "bundle_digest",
    "dataclass",
    "datetime",
    "enumerate_all",
    "errno",
    "field",
    "hashlib",
    "json",
    "main",
    "math",
    "mcp_candidates",
    "merge_plans",
    "os",
    "re",
    "read_agent_spec",
    "read_plan",
    "redact_credentials",
    "refused_by_location",
    "refused_by_name",
    "resolve_crew",
    "scan_text",
    "skill_candidates",
    "stat",
    "sys",
    "timezone",
    "uuid",
    "verify",
    "write_plan",
)

_ABSENT = object()


@pytest.fixture(scope="module")
def facade() -> types.ModuleType:
    """The builder as the rest of the tree imports it."""
    return importlib.import_module(FACADE_NAME)


def _owner(name: str) -> types.ModuleType:
    return importlib.import_module(f"{PIPELINE_PACKAGE}.{name}")


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), str(path))


def _defined(tree: ast.Module) -> set[str]:
    """The names a module's own top level defines: its defs, classes and assignments.

    Names an ``except`` fallback assigns count, and so do the names a guarded ``try`` imports
    from outside the package: the owner holding them is the one place they live.
    """
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, ast.Try):
            for inner in ast.walk(node):
                if isinstance(inner, ast.alias):
                    names.add(inner.asname or inner.name)
                elif isinstance(inner, ast.Name) and isinstance(inner.ctx, ast.Store):
                    names.add(inner.id)
                elif isinstance(inner, ast.AnnAssign) and isinstance(inner.target, ast.Name):
                    names.add(inner.target.id)
    return names


def _functions(tree: ast.Module) -> set[str]:
    return {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}


def _owner_trees() -> dict[str, ast.Module]:
    return {leaf: _tree(PIPELINE_DIR / f"{leaf}.py") for leaf in LAYERS}


def _relative_imports(tree: ast.Module) -> list[tuple[str, list[str], int]]:
    """``(module, names, level)`` for every relative import anywhere in *tree*."""
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level:
            found.append((node.module or "", [a.name for a in node.names], node.level))
    return found


def _by_name_imports() -> dict[str, set[str]]:
    """Owner-defined name -> the owners that import it by name rather than through its module."""
    importers: dict[str, set[str]] = {}
    for leaf, tree in _owner_trees().items():
        for module, names, level in _relative_imports(tree):
            if level == 1 and module:
                for name in names:
                    importers.setdefault(name, set()).add(leaf)
    return importers


# ---------------------------------------------------------------------------
# The surface
# ---------------------------------------------------------------------------


class TestTheSurfaceSurvivesTheSplit:
    def test_the_frozen_inventory_is_not_empty(self) -> None:
        # An emptied list would make the cases below pass while checking nothing.
        assert len(FROZEN_NAMES) > 140

    @pytest.mark.parametrize("name", FROZEN_NAMES)
    def test_every_name_the_module_bound_still_resolves(
        self, facade: types.ModuleType, name: str
    ) -> None:
        value = getattr(facade, name, _ABSENT)
        assert value is not _ABSENT, f"build.{name} no longer resolves"
        owner = facade._EXPORTS.get(name)
        if owner is not None:
            held = vars(importlib.import_module(f"{facade.__package__}.{owner}"))[name]
            assert held is value, f"build.{name} is not the object {owner} holds"

    def test_every_exported_name_is_one_its_owner_defines(self, facade) -> None:
        # Derived from the owners' own source, so a name an owner gains is exported and a
        # name listed for an owner that does not define it is caught.
        expected: dict[str, str] = {}
        for leaf, tree in _owner_trees().items():
            for name in _defined(tree):
                assert name not in expected, f"{name} is defined by {expected[name]} and {leaf}"
                expected[name] = f"pipeline.{leaf}"
        assert facade._EXPORTS == expected

    def test_the_layers_are_the_pipeline_on_disk(self) -> None:
        on_disk = {p.stem for p in PIPELINE_DIR.glob("*.py") if p.stem != "__init__"}
        assert on_disk == set(LAYERS)

    def test_no_exported_name_is_bound_in_the_facade(self, facade) -> None:
        # A binding here would shadow the owner for every later read, so a write that lands on
        # the owner would stop being what the facade answers.
        assert not set(facade._EXPORTS) & set(vars(facade))

    def test_a_read_through_the_facade_answers_from_the_owner_on_every_access(self, facade) -> None:
        replacement = object()
        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(_owner("pinned"), "_is_redirecting_entry", replacement)
            assert facade._is_redirecting_entry is replacement

    def test_a_missing_name_is_an_attribute_error(self, facade) -> None:
        # ``hasattr``, ``getattr(..., default)`` and ``mock.patch`` all rely on it.
        assert not hasattr(facade, "_no_such_builder_name")
        with pytest.raises(AttributeError):
            facade.no_such_builder_name  # noqa: B018

    def test_dir_lists_the_frozen_names(self, facade) -> None:
        assert set(FROZEN_NAMES) <= set(dir(facade))

    def test_a_star_import_carries_exactly_the_public_names_of_the_inventory(
        self, facade, tmp_path: Path
    ) -> None:
        # ``__all__`` is derived; the machinery binds only private names, so nothing it needs
        # leaks into a star importer's namespace and nothing public goes missing. The star
        # importer is a real module on disk, loaded the way any module is.
        assert set(facade.__all__) == {name for name in FROZEN_NAMES if not name.startswith("_")}
        probe = tmp_path / "star_importer.py"
        probe.write_text(f"from {FACADE_NAME} import *  # noqa: F401,F403\n", encoding="utf-8")
        spec = importlib.util.spec_from_file_location("star_importer", probe)
        assert spec is not None and spec.loader is not None
        importer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(importer)
        assert {k for k in vars(importer) if not k.startswith("__")} == set(facade.__all__)

    def test_a_star_import_without_kiro_crew_security_carries_what_resolves(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Standalone, ``scan`` binds no ``redact_credentials``, and the one-module builder's
        # star import lacked it there. Hidden before the copy loads, because its owners bind
        # their names when they first run.
        monkeypatch.setitem(sys.modules, "kiro_crew.security", None)
        standalone = load_build()
        assert standalone._CANONICAL_REDACTOR is None
        assert "redact_credentials" not in standalone.__all__
        assert "redact_credentials" not in dir(standalone)
        with pytest.raises(AttributeError):
            standalone.redact_credentials  # noqa: B018
        probe = tmp_path / "standalone_star_importer.py"
        star = f"from {standalone.__name__} import *  # noqa: F401,F403\n"
        probe.write_text(star, encoding="utf-8")
        spec = importlib.util.spec_from_file_location("standalone_star_importer", probe)
        assert spec is not None and spec.loader is not None
        importer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(importer)
        public = {name for name in FROZEN_NAMES if not name.startswith("_")}
        carried = {k for k in vars(importer) if not k.startswith("__")}
        assert carried == public - {"redact_credentials"}

    def test_the_type_checker_sees_every_exported_name(self, facade) -> None:
        # ``__getattr__`` is hidden from the checker, so the names it serves at run time are
        # declared to it under ``TYPE_CHECKING``; one missing there would type as an error at a
        # correct call site, and an extra one would hide a stale name.
        declared: dict[str, str] = {}
        for node in _tree(BUILD_PY).body:
            if isinstance(node, ast.If) and ast.unparse(node.test) == "_typing.TYPE_CHECKING":
                for stmt in node.body:
                    if isinstance(stmt, ast.ImportFrom) and stmt.module:
                        declared.update((alias.name, stmt.module) for alias in stmt.names)
                getattr_defs = [
                    stmt
                    for stmt in node.orelse
                    if isinstance(stmt, ast.FunctionDef) and stmt.name == "__getattr__"
                ]
                assert getattr_defs, "__getattr__ must sit in the not-TYPE_CHECKING branch"
        assert declared == dict(facade._EXPORTS)

    def test_the_crew_name_guard_runtime_prose_points_at_still_resolves(self, facade) -> None:
        # ``crew/runtime/container/supervisor/bundle.py`` names ``_validated_crew_name`` in
        # ``packaging/build.py`` as the builder's copy of its own guard.
        assert facade._validated_crew_name is _owner("crew")._validated_crew_name
        with pytest.raises(facade.ExportRefused):
            facade._validated_crew_name("../x")


# ---------------------------------------------------------------------------
# One namespace for writes
# ---------------------------------------------------------------------------


def _spy_report_owner(mod: types.ModuleType, tmp_path: Path) -> list[Path]:
    """Drive a caller in ``report`` of a function ``pinned`` defines, recording each call."""
    seen: list[Path] = []
    real = mod._is_redirecting_entry

    def spy(probe: Path) -> bool:
        seen.append(probe)
        return real(probe)

    setattr(mod, "_is_redirecting_entry", spy)
    try:
        mod._refuse_unless_our_report(tmp_path / "absent.smc-bundle.json", tmp_path / "absent")
    finally:
        setattr(mod, "_is_redirecting_entry", real)
    return seen


class TestOneNamespaceForWrites:
    def test_a_write_through_the_facade_lands_on_the_defining_owner(self, facade) -> None:
        sentinel = object()
        original = facade._redirect_between
        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(facade, "_redirect_between", sentinel)
            assert _owner("pinned")._redirect_between is sentinel
            assert "_redirect_between" not in vars(facade)
        assert _owner("pinned")._redirect_between is original
        assert facade._redirect_between is original

    def test_the_write_reaches_a_caller_in_another_owner(self, tmp_path) -> None:
        # The behaviour, not only the binding: ``_is_redirecting_entry`` is defined in
        # ``pinned`` and called from ``report``, through ``report``'s view of ``pinned``.
        mod = load_build()
        seen = _spy_report_owner(mod, tmp_path)
        assert seen == [tmp_path / "absent.smc-bundle.json"]

    def test_no_owner_holds_another_owners_function_by_name(self) -> None:
        # The property the previous case depends on, for every function: a copy imported by
        # name would keep the unpatched object while the facade's write lands on the owner.
        trees = _owner_trees()
        functions = {leaf: _functions(tree) for leaf, tree in trees.items()}
        offenders = []
        for leaf, tree in trees.items():
            for module, names, level in _relative_imports(tree):
                if level == 1 and module in functions:
                    offenders += [
                        f"{leaf}: {n} from {module}" for n in names if n in functions[module]
                    ]
        assert offenders == []

    def test_monkeypatch_and_mock_patch_round_trip_in_either_order(self, facade) -> None:
        original = facade._read_bytes_openat
        outer, inner = object(), object()
        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(facade, "_read_bytes_openat", outer)
            with mock.patch.object(facade, "_read_bytes_openat", new=inner):
                assert _owner("pinned")._read_bytes_openat is inner
            assert _owner("pinned")._read_bytes_openat is outer
        assert _owner("pinned")._read_bytes_openat is original
        with mock.patch.object(facade, "_read_bytes_openat", new=outer):
            with pytest.MonkeyPatch.context() as patched:
                patched.setattr(facade, "_read_bytes_openat", inner)
                assert _owner("pinned")._read_bytes_openat is inner
            assert _owner("pinned")._read_bytes_openat is outer
        assert _owner("pinned")._read_bytes_openat is original
        assert "_read_bytes_openat" not in vars(facade)

    def test_a_delete_and_restore_through_the_facade_round_trips(self, facade) -> None:
        original = facade._within
        with pytest.MonkeyPatch.context() as patched:
            patched.delattr(facade, "_within")
            assert not hasattr(facade, "_within")
            assert "_within" not in vars(_owner("prompt"))
        assert _owner("prompt")._within is original

    @staticmethod
    def _bare_loads(source: str, exported: set[str] | dict[str, str]) -> list[tuple[int, str]]:
        """Loads of a forwarded name as a bare global, outside import lines."""
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
            and node.id in exported
            and node.lineno not in import_lines
        ]

    def test_the_facade_reads_no_forwarded_name_as_a_bare_global(self, facade) -> None:
        # A function defined in the facade resolves a bare global through the facade's own
        # namespace, which ``__getattr__`` never sees, so such a read would need the facade to
        # bind the name -- the second storage it exists not to have. ``TYPE_CHECKING`` block
        # included: only its import lines may name a forwarded name.
        source = BUILD_PY.read_text(encoding="utf-8")
        assert self._bare_loads(source, facade._EXPORTS) == []

    def test_the_bare_global_scan_can_fail(self, facade) -> None:
        name = "_within"
        assert name in facade._EXPORTS
        sample = f"from x import {name}\ndef f():\n    return {name}\n"
        assert self._bare_loads(sample, facade._EXPORTS) == [(3, name)]

    def test_a_loaded_owner_is_read_and_written_without_calling_import_module(self, facade) -> None:
        # ``importlib.import_module`` is an attribute any test can rebind; a read or a write of
        # an owner already in ``sys.modules`` must not depend on what that patch returns.
        original = facade._within  # the owner is loaded from here on
        prompt = _owner("prompt")
        replacement = object()
        with mock.patch("importlib.import_module", side_effect=AssertionError("resolved")):
            assert facade._within is original
            with pytest.MonkeyPatch.context() as patched:
                patched.setattr(facade, "_within", replacement)
                assert prompt._within is replacement
            assert facade._within is original

    def test_a_write_of_a_name_the_facade_does_not_forward_stays_on_the_facade(self) -> None:
        # Why the patch-site rule below refuses one: ``os`` is bound separately in every owner
        # that imports it, so replacing it here replaces it for no owner.
        mod = load_build()
        fake = types.SimpleNamespace(name="nt")
        with pytest.MonkeyPatch.context() as patched:
            patched.setattr(mod, "os", fake)
            owners = builder_owners(mod)
            assert owners and all(vars(o).get("os") is not fake for o in owners)


# ---------------------------------------------------------------------------
# Every write through the facade
# ---------------------------------------------------------------------------

#: Directory names a repository-wide scan never enters.
_NOT_SCANNED = frozenset({".worktrees", "node_modules", ".venv", "__pycache__"})

#: The ``mock`` patch callables by the dotted path they resolve to: which one, and the index
#: of ``create`` among its positional parameters.
_PATCH_CALLABLES = {
    "unittest.mock.patch": ("patch", 3),
    "unittest.mock.patch.object": ("object", 4),
    "unittest.mock.patch.multiple": ("multiple", 2),
}

#: ``patch.multiple`` keywords that configure the patch rather than name an attribute.
_MULTIPLE_OPTIONS = frozenset({"target", "spec", "create", "spec_set", "autospec", "new_callable"})

#: What a write names when its attribute cannot be read off the source.
_DYNAMIC = "<dynamic>"

#: A loader of a throwaway builder copy, by the dotted path it resolves to.
_LOADER = "kiro_crew.apps.builtins.aws_control.crew.packaging.tests.test_producer.load_build"

#: The one deliberate write the rule below refuses, keyed by file and the test enclosing it:
#: the case that shows a write of an unforwarded name landing on the facade alone.
_ALLOWED_WRITES = frozenset(
    {
        (
            "src/kiro_crew/apps/builtins/aws_control/crew/packaging/tests/"
            "test_pipeline_composition.py",
            "TestOneNamespaceForWrites."
            "test_a_write_of_a_name_the_facade_does_not_forward_stays_on_the_facade",
        )
    }
)

#: A value an expression may denote: a dotted ``path`` (a module, or an attribute reached
#: from one), a ``str``, or the known leading ``prefix`` of a string.
_Value = tuple[str, str]


class _Write(NamedTuple):
    function: str
    form: str
    name: str
    creates: bool
    line: int


class _Resolver:
    """What the names in one module's source may denote, read off its AST alone.

    A name is bound by an import, a ``def``, a plain or annotated assignment in its
    function's scope or an enclosing one -- followed to a fixed point -- or, for a parameter,
    by the module's fixture of that name and by what the module's own calls of that function
    pass for it. A name bound more than once may denote any of its values. An expression the
    reader cannot follow denotes nothing.
    """

    def __init__(self, tree: ast.Module, module: str | None) -> None:
        self._tree = tree
        self._module = module
        self._package = module.rpartition(".")[0] if module else None
        self._parents: dict[ast.AST, ast.AST] = {}
        self._bindings: dict[ast.AST, dict[str, list[ast.AST | frozenset[_Value]]]] = {}
        self._params: dict[ast.AST, set[str]] = {}
        self._fixtures: dict[str, ast.FunctionDef] = {}
        self._calls: dict[str, list[ast.Call]] = {}
        self._defs: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
        self._returning: set[str] = set()
        stack: list[ast.AST] = [tree]
        while stack:
            node = stack.pop()
            for child in ast.iter_child_nodes(node):
                self._parents[child] = node
                stack.append(child)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for name, path in self._imported(node):
                    self._bind(node, name, frozenset({("path", path)}))
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        self._bind(node, target.id, node.value)
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                if isinstance(node.target, ast.Name):
                    self._bind(node, node.target.id, node.value)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                args = node.args
                self._params[node] = {
                    a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)
                }
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    owner = module or "<local>"
                    self._bind(node, node.name, frozenset({("path", f"{owner}.{node.name}")}))
                    self._defs[f"{owner}.{node.name}"] = node
                    if any("fixture" in ast.unparse(d) for d in node.decorator_list):
                        if isinstance(node, ast.FunctionDef) and self.scope_of(node) is tree:
                            self._fixtures[node.name] = node
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                self._calls.setdefault(node.func.id, []).append(node)
        self._following: set[tuple[int, str]] = set()
        self._known: dict[tuple[int, str], set[_Value]] = {}

    def _imported(self, node: ast.Import | ast.ImportFrom) -> list[tuple[str, str]]:
        if isinstance(node, ast.Import):
            return [
                (alias.asname, alias.name) if alias.asname else (alias.name.split(".")[0],) * 2
                for alias in node.names
            ]
        module = node.module or ""
        if node.level:
            if self._package is None:
                return []
            module = importlib.util.resolve_name("." * node.level + module, self._package)
        return [(alias.asname or alias.name, f"{module}.{alias.name}") for alias in node.names]

    def _bind(self, statement: ast.AST, name: str, value: ast.AST | frozenset[_Value]) -> None:
        self._bindings.setdefault(self.scope_of(statement), {}).setdefault(name, []).append(value)

    def scope_of(self, node: ast.AST) -> ast.AST:
        """The function whose body holds *node*, or the module; a decorator is outside."""
        child, parent = node, self._parents.get(node)
        while parent is not None:
            if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                decorators = getattr(parent, "decorator_list", [])
                if not any(child is decorator for decorator in decorators):
                    return parent
            child, parent = parent, self._parents.get(parent)
        return self._tree

    def function_of(self, node: ast.AST) -> str:
        """The dotted class and function names enclosing *node*, ``<module>`` for none."""
        names = []
        parent = self._parents.get(node)
        while parent is not None:
            if isinstance(parent, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                names.append(parent.name)
            parent = self._parents.get(parent)
        return ".".join(reversed(names)) or "<module>"

    @staticmethod
    def is_facade(value: _Value) -> bool:
        return value == ("path", FACADE_NAME)

    def names_the_facade(self, expr: ast.AST | None, scope: ast.AST) -> bool:
        return any(self.is_facade(value) for value in self.values(expr, scope))

    def values(self, expr: ast.AST | None, scope: ast.AST) -> set[_Value]:
        if expr is None:
            return set()
        if isinstance(expr, ast.Constant):
            return {("str", expr.value)} if isinstance(expr.value, str) else set()
        if isinstance(expr, ast.Name):
            return self._name(expr.id, scope)
        if isinstance(expr, ast.Attribute):
            found: set[_Value] = set()
            for value in self.values(expr.value, scope):
                if value[0] == "path":
                    found.add(("path", f"{value[1]}.{expr.attr}"))
                    if expr.attr == "__name__" and self.is_facade(value):
                        found.add(("str", FACADE_NAME))
            return found
        if isinstance(expr, ast.Call):
            called = {text for kind, text in self.values(expr.func, scope) if kind == "path"}
            if _LOADER in called:
                return {("path", FACADE_NAME)}
            returned: set[_Value] = set()
            for path in called & set(self._defs):
                returned |= self._returns(path)
            if returned:
                return returned
            if "importlib.import_module" in called and len(expr.args) == 1:
                return {
                    ("path", text)
                    for kind, text in self.values(expr.args[0], scope)
                    if kind == "str"
                }
            return set()
        if isinstance(expr, ast.JoinedStr):
            return self._joined(expr.values, scope)
        if isinstance(expr, ast.FormattedValue):
            plain = expr.conversion == -1 and expr.format_spec is None
            return self.values(expr.value, scope) if plain else set()
        if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Add):
            return self._joined([expr.left, expr.right], scope)
        return set()

    def _joined(self, parts: list[ast.expr], scope: ast.AST) -> set[_Value]:
        text = ""
        for part in parts:
            values = self.values(part, scope)
            strings = {value for kind, value in values if kind == "str"}
            prefixes = {value for kind, value in values if kind == "prefix"}
            if len(strings) == 1 and not prefixes:
                text += strings.pop()
                continue
            if len(prefixes) == 1 and not strings:
                text += prefixes.pop()
            return {("prefix", text)} if text else set()
        return {("str", text)}

    def _name(self, name: str, scope: ast.AST) -> set[_Value]:
        key = (id(scope), name)
        if key in self._known:
            return self._known[key]
        if key in self._following:
            return set()
        self._following.add(key)
        try:
            found: set[_Value] = set()
            for binding_scope in self._chain(scope):
                bound = self._bindings.get(binding_scope, {}).get(name)
                if bound is not None:
                    for value in bound:
                        found |= (
                            value
                            if isinstance(value, frozenset)
                            else self.values(value, binding_scope)
                        )
                    break
                if name in self._params.get(binding_scope, set()):
                    found = self._fixture(name) | self._arguments(binding_scope, name)
                    break
            self._known[key] = found
            return found
        finally:
            self._following.discard(key)

    def _returns(self, path: str) -> set[_Value]:
        """What a call of this module's function *path* may return."""
        if path in self._returning:
            return set()
        self._returning.add(path)
        try:
            function = self._defs[path]
            found: set[_Value] = set()
            for node in ast.walk(function):
                if isinstance(node, ast.Return) and node.value is not None:
                    found |= self.values(node.value, self.scope_of(node))
            return found
        finally:
            self._returning.discard(path)

    def _fixture(self, name: str) -> set[_Value]:
        """What the module's fixture *name* hands a test that takes it as a parameter."""
        fixture = self._fixtures.get(name)
        if fixture is None:
            return set()
        found: set[_Value] = set()
        for node in ast.walk(fixture):
            if isinstance(node, (ast.Return, ast.Yield)) and node.value is not None:
                found |= self.values(node.value, fixture)
        return found

    def _arguments(self, function: ast.AST, param: str) -> set[_Value]:
        """What this module's calls of *function* pass for its parameter *param*."""
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return set()
        positional = [a.arg for a in (*function.args.posonlyargs, *function.args.args)]
        found: set[_Value] = set()
        for call in self._calls.get(function.name, []):
            argument: ast.expr | None = None
            if param in positional and positional.index(param) < len(call.args):
                argument = call.args[positional.index(param)]
            for keyword in call.keywords:
                if keyword.arg == param:
                    argument = keyword.value
            if argument is not None:
                found |= self.values(argument, self.scope_of(call))
        return found

    def _chain(self, scope: ast.AST) -> Iterator[ast.AST]:
        while scope is not self._tree:
            yield scope
            scope = self.scope_of(scope)
        yield self._tree

    def _attribute_names(self, expr: ast.AST | None, scope: ast.AST) -> set[str]:
        names = {text for kind, text in self.values(expr, scope) if kind == "str"}
        return names or {_DYNAMIC}

    def _string_target(self, expr: ast.AST | None, scope: ast.AST) -> set[str]:
        """The facade attribute a dotted-string target names, when it names one."""
        prefix = FACADE_NAME + "."
        found: set[str] = set()
        for kind, text in self.values(expr, scope):
            if kind == "path" or not text.startswith(prefix):
                continue
            rest = text[len(prefix) :]
            if kind == "prefix":
                found.add(_DYNAMIC)
            elif rest and "." not in rest:
                found.add(rest)
        return found

    def writes(self, node: ast.AST) -> list[tuple[str, str, bool]]:
        """``(form, name, creates)`` for each facade attribute *node* writes."""
        scope = self.scope_of(node)
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.Delete)):
            targets = node.targets if isinstance(node, (ast.Assign, ast.Delete)) else [node.target]
            form = {ast.Assign: "assign", ast.AugAssign: "augassign", ast.Delete: "del"}[type(node)]
            return [
                (form, target.attr, False)
                for target in targets
                if isinstance(target, ast.Attribute) and self.names_the_facade(target.value, scope)
            ]
        if not isinstance(node, ast.Call):
            return []
        keywords = {keyword.arg: keyword.value for keyword in node.keywords if keyword.arg}
        callable_ = next(
            (
                _PATCH_CALLABLES[text]
                for kind, text in self.values(node.func, scope)
                if text in _PATCH_CALLABLES
            ),
            None,
        )
        if callable_ is not None:
            return self._patch_writes(node, scope, keywords, *callable_)
        func = node.func
        if isinstance(func, ast.Name) and func.id in {"setattr", "delattr"}:
            if self.values(func, scope) or not node.args:
                return []  # rebound locally: not the builtin
            if not self.names_the_facade(node.args[0], scope):
                return []
            second = node.args[1] if len(node.args) > 1 else None
            return [(func.id, name, False) for name in self._attribute_names(second, scope)]
        if isinstance(func, ast.Attribute) and func.attr in {"setattr", "delattr"} and node.args:
            return self._monkeypatch_writes(node, scope, keywords, func.attr)
        return []

    def _patch_writes(
        self,
        call: ast.Call,
        scope: ast.AST,
        keywords: dict[str, ast.expr],
        kind: str,
        create_at: int,
    ) -> list[tuple[str, str, bool]]:
        create = keywords.get(
            "create", call.args[create_at] if len(call.args) > create_at else None
        )
        creates = create is not None and not (
            isinstance(create, ast.Constant) and create.value is False
        )
        target = keywords.get("target", call.args[0] if call.args else None)
        names: set[str] = set()
        if kind == "patch":
            names = self._string_target(target, scope)
        elif kind == "object":
            if self.names_the_facade(target, scope):
                attribute = keywords.get("attribute", call.args[1] if len(call.args) > 1 else None)
                names = self._attribute_names(attribute, scope)
        elif self.names_the_facade(target, scope) or ("str", FACADE_NAME) in self.values(
            target, scope
        ):
            names = {name for name in keywords if name not in _MULTIPLE_OPTIONS}
            if any(keyword.arg is None for keyword in call.keywords):
                names.add(_DYNAMIC)
        return [(f"mock.{kind}", name, creates) for name in sorted(names)]

    def _monkeypatch_writes(
        self, call: ast.Call, scope: ast.AST, keywords: dict[str, ast.expr], verb: str
    ) -> list[tuple[str, str, bool]]:
        first = call.args[0]
        string_form = bool(self._string_target(first, scope))
        if string_form:
            names = self._string_target(first, scope)
            raising_at = 2 if verb == "setattr" else 1
        elif self.names_the_facade(first, scope):
            second = call.args[1] if len(call.args) > 1 else None
            names = self._attribute_names(second, scope)
            raising_at = 3 if verb == "setattr" else 2
        else:
            return []
        raising = keywords.get(
            "raising", call.args[raising_at] if len(call.args) > raising_at else None
        )
        creates = verb == "setattr" and not (
            raising is None or (isinstance(raising, ast.Constant) and raising.value is True)
        )
        return [(f"monkeypatch.{verb}", name, creates) for name in sorted(names)]


def _module_name(path: Path) -> str | None:
    """The dotted import name of a file under ``src/``; a file elsewhere has none."""
    relative = path.relative_to(REPO_ROOT)
    if relative.parts[0] != "src":
        return None
    parts = list(relative.with_suffix("").parts[1:])
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _in_a_test_directory(path: Path) -> bool:
    return any(part.endswith("tests") for part in path.relative_to(REPO_ROOT).parts[:-1])


def _facade_writes(source: str, module: str | None = None) -> list[_Write]:
    """Every write of a facade attribute in *source*, in any spelling the reader resolves.

    ``mock.patch``, ``patch.object`` and ``patch.multiple`` reached through any import
    alias, called or used as a decorator, with positional or keyword targets and a target
    string built from the facade's name; ``monkeypatch.setattr`` / ``delattr`` in the object
    and the dotted-string form; the ``setattr`` / ``delattr`` builtins; and assignment,
    augmented assignment and ``del`` of an attribute. The facade is named by an import of
    it, a ``load_build`` copy, a fixture returning one, or an assignment chain to either.
    """
    tree = ast.parse(source)
    resolver = _Resolver(tree, module)
    return [
        _Write(resolver.function_of(node), form, name, creates, node.lineno)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Call, ast.Assign, ast.AugAssign, ast.Delete))
        for form, name, creates in resolver.writes(node)
    ]


def _refusal(write: _Write, by_name: dict[str, set[str]]) -> str | None:
    """Why a write cannot round-trip or cannot reach the builder, or ``None`` when it can."""
    if write.name == _DYNAMIC:
        return "names an attribute the source does not fix, so the rule cannot judge it"
    if write.creates:
        return "may create the name (create not literally False / raising not True), so its undo deletes it"
    if write.name not in _EXPORTED:
        return "writes a name the facade does not forward, so it reaches no owner"
    if write.name in by_name:
        return (
            f"writes a name {sorted(by_name[write.name])} import by name, which keep the old object"
        )
    return None


def _patch_sources() -> Iterator[tuple[Path, str]]:
    """``(path, text)`` for every test module in the repository that may write the facade."""
    needles = ("load_build", "packaging.build", "packaging import build", "..build", "FACADE")
    paths = [*(REPO_ROOT / "test").rglob("*.py")]
    paths += [path for path in (REPO_ROOT / "src").rglob("*.py") if _in_a_test_directory(path)]
    for path in paths:
        if _NOT_SCANNED.intersection(path.relative_to(REPO_ROOT).parts):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if any(needle in text for needle in needles):
            yield path, text


#: The names the facade forwards, read once for the reader cases below.
_EXPORTED: frozenset[str] = frozenset(importlib.import_module(FACADE_NAME)._EXPORTS)

#: Imports most reader cases share.
_CASE_IMPORTS = f"from unittest import mock\nimport {FACADE_NAME} as bk\n"

#: ``(id, source, module, expected (form, name, creates))`` for the reader: each form it
#: must resolve beside a spelling of it that is not a facade write. ``@F@`` is a forwarded
#: function, ``@C@`` a constant owners import by name, ``@FACADE@`` the facade's dotted name.
_READER_CASES: list[tuple[str, str, str | None, list[tuple[str, str, bool]]]] = [
    (
        "patch.object",
        _CASE_IMPORTS + "mock.patch.object(bk, '@F@')\n",
        None,
        [("mock.object", "@F@", False)],
    ),
    (
        "patch.object create=True",
        _CASE_IMPORTS + "mock.patch.object(bk, '@F@', create=True)\n",
        None,
        [("mock.object", "@F@", True)],
    ),
    (
        "patch.object create=False",
        _CASE_IMPORTS + "mock.patch.object(bk, '@F@', create=False)\n",
        None,
        [("mock.object", "@F@", False)],
    ),
    (
        "create=1",
        _CASE_IMPORTS + "mock.patch.object(bk, '@F@', create=1)\n",
        None,
        [("mock.object", "@F@", True)],
    ),
    (
        "create=flag",
        _CASE_IMPORTS + "flag = False\nmock.patch.object(bk, '@F@', create=flag)\n",
        None,
        [("mock.object", "@F@", True)],
    ),
    (
        "create positionally",
        _CASE_IMPORTS + "mock.patch.object(bk, '@F@', mock.DEFAULT, None, True)\n",
        None,
        [("mock.object", "@F@", True)],
    ),
    (
        "another module",
        _CASE_IMPORTS + "import os\nmock.patch.object(os, 'getcwd', create=True)\n",
        None,
        [],
    ),
    (
        "bare patch imported as P",
        f"from unittest.mock import patch as P\nP('{FACADE_NAME}._within', create=True)\n",
        None,
        [("mock.patch", "_within", True)],
    ),
    (
        "bare patch of another module",
        "from unittest.mock import patch\npatch('os.getcwd', create=True)\n",
        None,
        [],
    ),
    (
        "mock as an alias",
        f"from unittest import mock as M\nimport {FACADE_NAME} as b\nM.patch.object(b, '@F@')\n",
        None,
        [("mock.object", "@F@", False)],
    ),
    (
        "unittest.mock by its path",
        f"import unittest.mock\nunittest.mock.patch('{FACADE_NAME}.@F@')\n",
        None,
        [("mock.patch", "@F@", False)],
    ),
    (
        "a third-party mock",
        f"import mock\nimport {FACADE_NAME} as bk\nmock.patch.object(bk, '@F@', create=True)\n",
        None,
        [],
    ),
    (
        "a decorator",
        _CASE_IMPORTS + "@mock.patch.object(bk, '@F@', create=True)\ndef test_x(fake):\n    pass\n",
        None,
        [("mock.object", "@F@", True)],
    ),
    (
        "a bare decorator",
        f"from unittest.mock import patch\n@patch('{FACADE_NAME}.@F@')\ndef test_x(fake):\n    pass\n",
        None,
        [("mock.patch", "@F@", False)],
    ),
    (
        "an f-string of __name__",
        _CASE_IMPORTS + "mock.patch(f'{bk.__name__}._within', create=True)\n",
        None,
        [("mock.patch", "_within", True)],
    ),
    (
        "an f-string of another __name__",
        _CASE_IMPORTS + "import os\nmock.patch(f'{os.__name__}.getcwd', create=True)\n",
        None,
        [],
    ),
    (
        "an f-string of a constant",
        f"from unittest import mock\nFAC = '{FACADE_NAME}'\nmock.patch(f'{{FAC}}.@F@')\n",
        None,
        [("mock.patch", "@F@", False)],
    ),
    (
        "a constant concatenated",
        f"from unittest import mock\nFAC = '{FACADE_NAME}'\nmock.patch(FAC + '._x', create=True)\n",
        None,
        [("mock.patch", "_x", True)],
    ),
    (
        "another constant concatenated",
        "from unittest import mock\nOTHER = 'kiro_crew.config'\nmock.patch(OTHER + '._x', create=True)\n",
        None,
        [],
    ),
    (
        "a deeper dotted string",
        f"from unittest import mock\nmock.patch('{FACADE_NAME}.os.getcwd')\n",
        None,
        [],
    ),
    (
        "an f-string it cannot finish",
        _CASE_IMPORTS + "def test_x(attr):\n    mock.patch(f'{bk.__name__}.{attr}')\n",
        None,
        [("mock.patch", _DYNAMIC, False)],
    ),
    (
        "keyword target and attribute",
        _CASE_IMPORTS + "mock.patch.object(target=bk, attribute='@F@')\n",
        None,
        [("mock.object", "@F@", False)],
    ),
    (
        "keyword target elsewhere",
        _CASE_IMPORTS + "import os\nmock.patch.object(target=os, attribute='getcwd')\n",
        None,
        [],
    ),
    (
        "keyword string target",
        f"from unittest import mock\nmock.patch(target='{FACADE_NAME}.@F@', create=True)\n",
        None,
        [("mock.patch", "@F@", True)],
    ),
    (
        "an unresolved attribute",
        _CASE_IMPORTS + "def test_x(attr):\n    mock.patch.object(bk, attr)\n",
        None,
        [("mock.object", _DYNAMIC, False)],
    ),
    (
        "a resolved local attribute",
        _CASE_IMPORTS + "def test_x():\n    name = '@F@'\n    mock.patch.object(bk, name)\n",
        None,
        [("mock.object", "@F@", False)],
    ),
    (
        "patch.multiple",
        _CASE_IMPORTS + "mock.patch.multiple(bk, create=True, @F@=None)\n",
        None,
        [("mock.multiple", "@F@", True)],
    ),
    (
        "patch.multiple of a string",
        f"from unittest import mock\nmock.patch.multiple('{FACADE_NAME}', @F@=None)\n",
        None,
        [("mock.multiple", "@F@", False)],
    ),
    (
        "patch.multiple elsewhere",
        _CASE_IMPORTS + "import os\nmock.patch.multiple(os, create=True, getcwd=None)\n",
        None,
        [],
    ),
    (
        "**kwargs in patch.multiple",
        _CASE_IMPORTS + "def test_x(kw):\n    mock.patch.multiple(bk, **kw)\n",
        None,
        [("mock.multiple", _DYNAMIC, False)],
    ),
    (
        "an assigned alias",
        _CASE_IMPORTS + "def test_x():\n    alias = bk\n    mock.patch.object(alias, '@F@')\n",
        None,
        [("mock.object", "@F@", False)],
    ),
    (
        "a chain of assignments",
        _CASE_IMPORTS + "a = bk\nb = a\nmock.patch.object(b, '@C@')\n",
        None,
        [("mock.object", "@C@", False)],
    ),
    (
        "import_module",
        f"import importlib\nfrom unittest import mock\nf = importlib.import_module('{FACADE_NAME}')\nmock.patch.object(f, '@F@')\n",
        None,
        [("mock.object", "@F@", False)],
    ),
    (
        "import_module elsewhere",
        "import importlib\nfrom unittest import mock\nf = importlib.import_module('kiro_crew.config')\nmock.patch.object(f, '@F@')\n",
        None,
        [],
    ),
    (
        "a load_build copy",
        f"from {_LOADER.rpartition('.')[0]} import load_build\nmod = load_build()\nmonkeypatch.setattr(mod, '@F@', 1)\n",
        None,
        [("monkeypatch.setattr", "@F@", False)],
    ),
    (
        "a copy through a relative import",
        "from .test_producer import load_build\ndef test_x(monkeypatch):\n    mod = load_build()\n    monkeypatch.setattr(mod, 'os', 1)\n",
        "kiro_crew.apps.builtins.aws_control.crew.packaging.tests.test_case",
        [("monkeypatch.setattr", "os", False)],
    ),
    (
        "a loader of another name",
        "from .other import load_build\nmod = load_build()\nmonkeypatch.setattr(mod, '@F@', 1)\n",
        "kiro_crew.apps.builtins.aws_control.crew.packaging.tests.test_case",
        [],
    ),
    (
        "a fixture's copy",
        f"import pytest\nfrom {_LOADER.rpartition('.')[0]} import load_build\n@pytest.fixture\ndef built():\n    return load_build()\ndef test_x(monkeypatch, built):\n    monkeypatch.setattr(built, '@F@', 1, raising=False)\n",
        None,
        [("monkeypatch.setattr", "@F@", True)],
    ),
    (
        "a helper's copy",
        f"from {_LOADER.rpartition('.')[0]} import load_build\ndef _copy():\n    return load_build(mutate=None)\ndef test_x(monkeypatch):\n    mod = _copy()\n    monkeypatch.setattr(mod, '@F@', 1)\n",
        None,
        [("monkeypatch.setattr", "@F@", False)],
    ),
    (
        "a helper returning something else",
        "import os\ndef _copy():\n    return os\ndef test_x(monkeypatch):\n    mod = _copy()\n    monkeypatch.setattr(mod, 'getcwd', 1)\n",
        None,
        [],
    ),
    (
        "a helper's parameter",
        _CASE_IMPORTS + "def _spy(m):\n    setattr(m, '@C@', 1)\ndef test_x():\n    _spy(bk)\n",
        None,
        [("setattr", "@C@", False)],
    ),
    (
        "a helper handed something else",
        _CASE_IMPORTS
        + "import os\ndef _spy(m):\n    setattr(m, 'environ', 1)\ndef test_x():\n    _spy(os)\n",
        None,
        [],
    ),
    (
        "a parameter no fixture fills",
        "def test_x(monkeypatch, built):\n    monkeypatch.setattr(built, '@F@', 1, raising=False)\n",
        None,
        [],
    ),
    (
        "monkeypatch raising=True",
        _CASE_IMPORTS + "monkeypatch.setattr(bk, '@F@', 1, raising=True)\n",
        None,
        [("monkeypatch.setattr", "@F@", False)],
    ),
    (
        "monkeypatch of an attribute of the facade",
        _CASE_IMPORTS + "monkeypatch.setattr(bk.os, 'name', 'nt')\n",
        None,
        [],
    ),
    (
        "monkeypatch string form",
        f"monkeypatch.setattr('{FACADE_NAME}.os', 1)\n",
        None,
        [("monkeypatch.setattr", "os", False)],
    ),
    (
        "monkeypatch string form elsewhere",
        "monkeypatch.setattr('kiro_crew.hooks.is_unc_shape', 1, raising=False)\n",
        None,
        [],
    ),
    (
        "monkeypatch delattr",
        _CASE_IMPORTS + "monkeypatch.delattr(bk, '@F@')\n",
        None,
        [("monkeypatch.delattr", "@F@", False)],
    ),
    (
        "the setattr builtin",
        _CASE_IMPORTS + "setattr(bk, '@C@', 8)\n",
        None,
        [("setattr", "@C@", False)],
    ),
    (
        "a rebound setattr",
        _CASE_IMPORTS + "def setattr(*a):\n    pass\nsetattr(bk, '@C@', 8)\n",
        None,
        [],
    ),
    ("an attribute assignment", _CASE_IMPORTS + "bk.@C@ = 'x'\n", None, [("assign", "@C@", False)]),
    (
        "an augmented assignment",
        _CASE_IMPORTS + "bk.@C@ += 1\n",
        None,
        [("augassign", "@C@", False)],
    ),
    ("a del", _CASE_IMPORTS + "del bk.@F@\n", None, [("del", "@F@", False)]),
    ("an assignment elsewhere", "import os\nos.environ = {}\n", None, []),
]


def _case(template: str) -> str:
    return (
        template.replace("@F@", "_within")
        .replace("@C@", "_MAX_PROMPT_BYTES")
        .replace("@FACADE@", FACADE_NAME)
    )


class TestPatchSpellings:
    def test_the_case_names_are_what_they_claim(self) -> None:
        assert "_within" in _EXPORTED and "_within" not in _by_name_imports()
        assert "_MAX_PROMPT_BYTES" in _EXPORTED and "_MAX_PROMPT_BYTES" in _by_name_imports()

    @pytest.mark.parametrize(
        ("source", "module", "expected"),
        [(case[1], case[2], case[3]) for case in _READER_CASES],
        ids=[case[0] for case in _READER_CASES],
    )
    def test_the_reader_resolves_every_spelling_and_only_those(
        self, source: str, module: str | None, expected: list[tuple[str, str, bool]]
    ) -> None:
        # A reader that missed a spelling would pass a suite using it; one that read a safe
        # spelling as a facade write would refuse a patch that reaches its owner.
        writes = _facade_writes(_case(source), module)
        assert [(w.form, w.name, w.creates) for w in writes] == [
            (form, _case(name), creates) for form, name, creates in expected
        ]

    def test_the_refusals_are_the_three_the_facade_cannot_honour(self) -> None:
        by_name = _by_name_imports()

        def verdict(name: str, creates: bool) -> str | None:
            return _refusal(_Write("f", "form", name, creates, 1), by_name)

        assert verdict("_within", False) is None
        assert "undo deletes" in (verdict("_within", True) or "")
        assert "does not forward" in (verdict("os", False) or "")
        assert "import by name" in (verdict("_MAX_PROMPT_BYTES", False) or "")
        assert "does not fix" in (verdict(_DYNAMIC, False) or "")

    def test_the_scan_reads_the_suites_writes(self) -> None:
        # Non-vacuity: a reader matching nothing would pass the rule below while checking
        # nothing, so the suite's own facade writes -- a few dozen -- must be found.
        writes = [
            write
            for path, text in _patch_sources()
            for write in _facade_writes(text, _module_name(path))
        ]
        assert len(writes) >= 30, writes

    def test_no_write_through_the_facade_is_one_it_cannot_honour(self) -> None:
        """Every write through the facade can reach its owner and come back out.

        Refused where it is written: a write that may create the name, whose undo only
        deletes and leaves the owner without it for the rest of the process; a name the
        facade does not forward, which lands on the facade and reaches no owner; a name an
        owner imports by NAME from another (a class or a constant), whose importers keep
        their own binding; and a write whose attribute the source does not fix. The scan
        must find exactly the allowlisted demonstration, so the allowlist can neither hide
        a second site nor outlive the one it names.
        """
        by_name = _by_name_imports()
        refused = [
            (path.relative_to(REPO_ROOT).as_posix(), write, reason)
            for path, text in _patch_sources()
            for write in _facade_writes(text, _module_name(path))
            if (reason := _refusal(write, by_name)) is not None
        ]
        found = {(path, write.function) for path, write, _reason in refused}
        unexpected = [
            f"{path}:{write.line} {write.form}({write.name!r}) {reason}"
            for path, write, reason in refused
            if (path, write.function) not in _ALLOWED_WRITES
        ]
        assert (
            found == _ALLOWED_WRITES
        ), f"{unexpected}; allowlisted but not found: {sorted(_ALLOWED_WRITES - found)}"


# ---------------------------------------------------------------------------
# Layering
# ---------------------------------------------------------------------------


def test_an_owner_imports_only_owners_below_it_and_never_the_facade() -> None:
    for index, leaf in enumerate(LAYERS):
        below = set(LAYERS[:index])
        for module, names, level in _relative_imports(_owner_trees()[leaf]):
            assert level == 1, f"{leaf} imports from outside the pipeline package: {module}"
            imported = {module} if module else set(names)
            assert imported <= below, f"{leaf} imports {sorted(imported - below)}"


def test_the_package_init_imports_nothing() -> None:
    tree = _tree(PIPELINE_DIR / "__init__.py")
    assert [type(n).__name__ for n in tree.body] == ["Expr"]


def test_the_facade_imports_no_owner_at_run_time() -> None:
    # Owners are resolved per access from ``sys.modules``; an import binding one here would
    # be the second storage location the facade exists not to have.
    for node in _tree(BUILD_PY).body:
        if isinstance(node, ast.ImportFrom):
            assert not node.level, f"build.py binds {ast.unparse(node)} at run time"


def test_a_loaded_copy_has_the_facades_shape() -> None:
    mod = load_build()
    real = importlib.import_module(FACADE_NAME)
    assert mod._EXPORTS == real._EXPORTS
    assert [o.__name__.rpartition(".")[2] for o in builder_owners(mod)] == sorted(LAYERS)


@pytest.mark.skipif(os.name != "posix", reason="the builder is POSIX-only")
def test_running_build_py_by_path_refuses_with_the_documented_entry(capsys) -> None:
    """``python .../build.py`` has no package to resolve the owners against, so it refuses.

    Run in process through ``runpy``, which gives the file the ``__main__`` name and no
    package, as the interpreter does -- and which installs and removes its own ``__main__``
    module, so the facade's class swap touches only that temporary one.
    """
    with pytest.raises(SystemExit) as caught:
        runpy.run_path(str(BUILD_PY), run_name="__main__")
    assert caught.value.code == 2
    err = capsys.readouterr().err
    assert err.startswith("refused: run the crew bundle builder as `python -m packaging.build`")
