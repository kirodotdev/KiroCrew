"""Warm-mint patch reach: a name the tests substitute on the facade reaches every owner reading it.

``kiro_crew.connections.warm`` is the warm engine's patch surface, and its ``warm_runtime``
owners read every name the facade defines, and every name a test substitutes on it, through
``_warm_facade()`` at call time. A bare read of such a name in an owner binds the owner's own
copy, so a patch of ``warm.<name>`` stops reaching that read while every behavioural test that
never exercises it stays green. The substituted names are derived here from the test trees' own
patch targets, not listed by hand, so a patch added on the facade is guarded the day it lands.

The composition contract (``test_connections_warm_mint_composition_contract.py``) pins which
facade names each owner CALLS and that no owner calls one of its ``_WARM_SEAMS`` bare. These
tests pin the rest: no owner reads a guarded name by its bare name in any position that runs (a
call, an argument handed to ``asyncio.to_thread``, an ``except`` clause, a constant, a default),
no owner binds one by an import, every late read names a guarded name the facade binds, and a
patch on the facade is what the moved code then runs, for each kind of late read.
"""

from __future__ import annotations

import ast
import asyncio
import functools
import importlib
from pathlib import Path
from typing import Any

import connections_warm_helpers
import facade_mirror_scan
import pytest
import source_corpus

from kiro_crew.connections import warm, warm_runtime
from kiro_crew.connections.mint import _mints

pytestmark = pytest.mark.xdist_group(name="tree_scan_warm_runtime_patch_reach")

# The warm suite's autouse resets of the module singletons, registered here by name.
_clean_mint_table = connections_warm_helpers._clean_mint_table
_clean_warm_runtime = connections_warm_helpers._clean_warm_runtime

FACADE = "kiro_crew.connections.warm"
_THIS_FILE = Path(__file__).resolve()
_CONTRACT = _THIS_FILE.with_name("test_connections_warm_mint_composition_contract.py")

#: Names the OTHER test files are known to patch on the facade, one of each kind an owner reads
#: late: a function an owner calls, one it hands to ``asyncio.to_thread``, a tunable constant, a
#: name the facade only imports, and a coroutine scheduled as a task. The scan below must find
#: all of them, so a scanner that stops matching goes red instead of passing over nothing.
_KNOWN_SEAMS = frozenset(
    {
        "_WARM_KILL_TIMEOUT_SECONDS",
        "_WARM_OAUTH_SETTLE_ROUNDS",
        "_absorb_warm_requests",
        "_log_warm_event",
        "_mint_watcher",
        "_rearm_invalidated_provider",
        "data_home",
        "declared_tool_aliases",
        "warm_mint_all",
    }
)


def _test_tree_files() -> list[Path]:
    root = source_corpus.repo_root()
    tests = root / "test"
    out = []
    for path in source_corpus.repo_files_named(".py"):
        rel = path.relative_to(root)
        if rel.name == "conftest.py" or tests in path.parents or "tests" in rel.parts:
            out.append(path)
    return out


@functools.lru_cache(maxsize=1)
def _corpus_patched_names() -> frozenset[str]:
    """Every name the other test files patch on the facade, scanned once per worker.

    This file is left out: its executed samples patch names on purpose, and counting them would
    let the non-vacuity check below satisfy itself.
    """
    found: set[str] = set()
    for path in _test_tree_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        if "warm" not in text or "connections" not in text or path.resolve() == _THIS_FILE:
            continue
        found |= facade_mirror_scan.patched_names(text, {FACADE: FACADE}).get(FACADE, set())
    return frozenset(found)


def _defined_at_top_level(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


@functools.lru_cache(maxsize=1)
def _facade_defined() -> frozenset[str]:
    """The names ``warm.py`` itself defines: an owner can reach them only through the facade."""
    tree = ast.parse(Path(warm.__file__).read_text(encoding="utf-8"))
    return frozenset(_defined_at_top_level(tree) - {"logger"})


@functools.lru_cache(maxsize=1)
def _contract_seams() -> frozenset[str]:
    """The composition contract's hand-listed ``_WARM_SEAMS``, read as data from its source."""
    for node in ast.parse(_CONTRACT.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "_WARM_SEAMS" for target in node.targets
        ):
            return frozenset(ast.literal_eval(node.value.args[0]))  # type: ignore[attr-defined]
    raise AssertionError("the contract no longer defines _WARM_SEAMS; re-point this reader")


def _guarded() -> frozenset[str]:
    return _corpus_patched_names() | _facade_defined() | _contract_seams()


def _owner_sources() -> dict[str, str]:
    package = Path(warm_runtime.__file__).parent
    return {
        path.stem: path.read_text(encoding="utf-8")
        for path in sorted(package.glob("*.py"))
        if path.name != "__init__.py"
    }


def _is_type_checking(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def _is_late_read(node: ast.AST) -> bool:
    """``_warm_facade().<name>``: the one shape an owner reads a facade name in."""
    return (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "_warm_facade"
        and not node.value.args
    )


class _Reads(ast.NodeVisitor):
    """Each Load of a name that resolves to the module namespace, with Python's own scoping.

    A function, lambda or comprehension scope sees its own bindings and those of the functions
    enclosing it, never a class body's; decorators, defaults and base classes run in the
    enclosing scope. Under ``from __future__ import annotations`` an annotation is never
    evaluated, so it reads nothing.
    """

    def __init__(self, lazy_annotations: bool) -> None:
        self.lazy_annotations = lazy_annotations
        self.scopes: list[set[str]] = []
        #: Names each scope declares ``global``: they resolve to the module, however assigned.
        self.declared: list[set[str]] = []
        self.reads: list[tuple[int, int, str]] = []
        self.local_imports: list[tuple[int, int, str]] = []

    @staticmethod
    def _declared_global(body: list[ast.stmt]) -> set[str]:
        declared: set[str] = set()
        stack: list[ast.AST] = list(body)
        while stack:
            node = stack.pop()
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                continue
            if isinstance(node, ast.Global):
                declared.update(node.names)
            stack.extend(ast.iter_child_nodes(node))
        return declared

    @staticmethod
    def _bound_in(body: list[ast.stmt], args: ast.arguments | None) -> set[str]:
        bound: set[str] = set()
        if args is not None:
            for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]:
                if arg is not None:
                    bound.add(arg.arg)
        stack: list[ast.AST] = list(body)
        while stack:
            node = stack.pop()
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                bound.add(node.name)
                continue  # its body is a scope of its own
            if isinstance(node, (ast.Lambda, ast.ListComp, ast.SetComp, ast.DictComp)):
                continue
            if isinstance(node, ast.GeneratorExp):
                continue
            if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
                bound.add(node.id)
            elif isinstance(node, ast.NamedExpr) and isinstance(node.target, ast.Name):
                bound.add(node.target.id)
            elif isinstance(node, ast.ExceptHandler) and node.name:
                bound.add(node.name)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                bound.update((a.asname or a.name).split(".")[0] for a in node.names)
            stack.extend(ast.iter_child_nodes(node))
        return bound

    def _annotations(self, node: ast.AST) -> None:
        if self.lazy_annotations:
            return
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args
            for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]:
                if arg is not None and arg.annotation is not None:
                    self.visit(arg.annotation)
            if node.returns is not None:
                self.visit(node.returns)

    def _function(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda) -> None:
        if not isinstance(node, ast.Lambda):
            for decorator in node.decorator_list:
                self.visit(decorator)
            self._annotations(node)
        for default in [*node.args.defaults, *node.args.kw_defaults]:
            if default is not None:
                self.visit(default)
        body = node.body if isinstance(node.body, list) else [ast.Expr(node.body)]
        declared = self._declared_global(body)
        self.scopes.append(self._bound_in(body, node.args) - declared)
        self.declared.append(declared)
        for statement in body:
            self.visit(statement)
        self.scopes.pop()
        self.declared.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._function(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self._function(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for expr in [*node.decorator_list, *node.bases, *(k.value for k in node.keywords)]:
            self.visit(expr)
        for statement in node.body:  # a class body sees module names; its methods do not see it
            if isinstance(statement, ast.AnnAssign):
                if statement.value is not None:
                    self.visit(statement.value)
                if not self.lazy_annotations:
                    self.visit(statement.annotation)
                continue
            self.visit(statement)

    def _comprehension(self, node: ast.AST, elements: list[ast.AST]) -> None:
        generators: list[ast.comprehension] = node.generators  # type: ignore[attr-defined]
        self.visit(generators[0].iter)  # the first iterable runs in the enclosing scope
        bound: set[str] = set()
        for generator in generators:
            for sub in ast.walk(generator.target):
                if isinstance(sub, ast.Name):
                    bound.add(sub.id)
        self.scopes.append(bound)
        self.declared.append(set())
        for index, generator in enumerate(generators):
            if index:
                self.visit(generator.iter)
            for condition in generator.ifs:
                self.visit(condition)
        for element in elements:
            self.visit(element)
        self.scopes.pop()
        self.declared.pop()

    def visit_ListComp(self, node: ast.ListComp) -> None:
        self._comprehension(node, [node.elt])

    def visit_SetComp(self, node: ast.SetComp) -> None:
        self._comprehension(node, [node.elt])

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        self._comprehension(node, [node.elt])

    def visit_DictComp(self, node: ast.DictComp) -> None:
        self._comprehension(node, [node.key, node.value])

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            self.visit(node.value)
        self.visit(node.target)
        if not self.lazy_annotations:
            self.visit(node.annotation)

    def visit_If(self, node: ast.If) -> None:
        if not self.scopes and _is_type_checking(node.test):
            for statement in node.orelse:
                self.visit(statement)
            return  # a TYPE_CHECKING block binds nothing at run time
        self.generic_visit(node)

    def _import(self, node: ast.Import | ast.ImportFrom) -> None:
        """An import below the module's top level: in a function, a class or a plain ``if``."""
        for alias in node.names:
            name = (alias.asname or alias.name).split(".")[0]
            self.local_imports.append((node.lineno, node.col_offset, name))

    def visit_Import(self, node: ast.Import) -> None:
        self._import(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        self._import(node)

    def visit_Name(self, node: ast.Name) -> None:
        if not isinstance(node.ctx, ast.Load):
            return
        for bound, declared in zip(reversed(self.scopes), reversed(self.declared)):
            if node.id in declared:
                break  # declared global here: it is the module's name
            if node.id in bound:
                return
        self.reads.append((node.lineno, node.col_offset, node.id))


def bare_reads(source: str, guarded: frozenset[str]) -> list[str]:
    """``line:name`` for every bare run-time read, or import binding, of a *guarded* name.

    A read of a name the enclosing function (or a function enclosing that one) binds for itself
    is that function's own and is skipped. An import that binds a guarded name is reported
    wherever it sits, at module level or inside a function, because that binding is the copy a
    bare read would find. Defining a guarded function or constant at the top level is the one
    binding allowed: it is what the facade re-exports.
    """
    tree = ast.parse(source)
    lazy = any(
        isinstance(node, ast.ImportFrom)
        and node.module == "__future__"
        and any(alias.name == "annotations" for alias in node.names)
        for node in tree.body
    )
    reads = _Reads(lazy)
    found: list[tuple[int, int, str]] = []
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                name = (alias.asname or alias.name).split(".")[0]
                if name in guarded:
                    found.append((node.lineno, -1, f"{name} (imported)"))
            continue
        reads.visit(node)
    found += [(line, col, name) for line, col, name in reads.reads if name in guarded]
    found += [
        (line, col, f"{name} (imported)")
        for line, col, name in reads.local_imports
        if name in guarded
    ]
    return [f"{line}:{name}" for line, _, name in sorted(found)]


def late_reads(source: str) -> list[tuple[int, str]]:
    """``(line, name)`` for every ``_warm_facade().<name>`` read, in source order."""
    reads = [node for node in ast.walk(ast.parse(source)) if _is_late_read(node)]
    ordered = sorted(reads, key=lambda node: (node.lineno, node.col_offset))
    return [(node.lineno, node.attr) for node in ordered]


def test_the_patch_scan_finds_every_known_seam() -> None:
    """Non-vacuity: a scan that finds nothing would pass every reach test over nothing."""
    assert sorted(_KNOWN_SEAMS - _corpus_patched_names()) == []


def test_no_owner_reads_a_guarded_name_by_its_bare_name() -> None:
    guarded = _guarded()
    offenders = {
        owner: reads
        for owner, source in _owner_sources().items()
        if (reads := bare_reads(source, guarded))
    }
    assert offenders == {}, (
        "an owner reads these by their bare names, so a patch on the warm facade misses them; "
        "read each as _warm_facade().<name> at call time"
    )


def test_every_late_read_names_a_guarded_name_the_facade_binds() -> None:
    """A late read is the mechanism for guarded names only, and must resolve on the facade."""
    guarded = _guarded()
    stray = {
        f"{owner}:{line}:{name}"
        for owner, source in _owner_sources().items()
        for line, name in late_reads(source)
        if name not in guarded or not hasattr(warm, name)
    }
    assert stray == set()


def test_the_owners_do_read_the_facade_late() -> None:
    """Non-vacuity of the bare-read check: the guarded names are read, just never bare."""
    read = {name for source in _owner_sources().values() for _, name in late_reads(source)}
    assert {"data_home", "declared_tool_aliases", "warm_mint_all", "_warm_mint"} <= read
    assert read & _corpus_patched_names(), "no owner reads a substituted name at all"


def test_the_bare_read_check_catches_each_shape_it_guards() -> None:
    guarded = frozenset({"data_home", "_log_warm_event", "_WarmMintDied", "_LIMIT", "_hook"})
    # No ``from __future__ import annotations`` here, so annotations are evaluated and count.
    planted = "\n".join(
        [
            "from kiro_crew.config.loader import data_home",  # 1
            "async def handed_off():",
            "    await to_thread(_log_warm_event, 'x')",  # 3
            "def caught():",
            "    try:",
            "        pass",
            "    except _WarmMintDied:",  # 7
            "        pass",
            "def tuned(limit=_LIMIT):",  # 9
            "    return limit",
            "def imported_here():",
            "    from kiro_crew.config.loader import data_home",  # 12
            "    return data_home()",
            "def outer():",
            "    def inner(data_home):",
            "        return data_home",
            "    return data_home()",  # 17
            "if PLATFORM:",
            "    def gated():",
            "        return _hook()",  # 20
            "@_hook",  # 21
            "def decorated():",
            "    return None",
            "class C:",
            "    x = _hook()",  # 25: a class body
            "    y: _hook = 1",  # 26: an evaluated class-body annotation
            "    def method(self):",
            "        return _hook()",  # 28: a method body
            "class D(_hook()):",  # 29: a base
            "    pass",
            "f = lambda: _hook()",  # 31: a lambda body
            "A = [_hook(x) for x in xs]",  # 32: a comprehension element
            "B = [x for x in _hook()]",  # 33: the first iterable
            "E = [x for x in xs if _hook(x)]",  # 34: a condition
            "G = _LIMIT",  # 35: a module-level constant
            "def typed(n: _hook) -> _hook:",  # 36: evaluated annotations, twice
            "    return n",
            "def cached():",
            "    global _hook",
            "    if _hook is None:",  # 40: a global the function also rebinds
            "        _hook = 1",
            "    return _hook",  # 42
        ]
    )
    assert bare_reads(planted, guarded) == [
        "1:data_home (imported)",
        "3:_log_warm_event",
        "7:_WarmMintDied",
        "9:_LIMIT",
        "12:data_home (imported)",
        "17:data_home",
        "20:_hook",
        "21:_hook",
        "25:_hook",
        "26:_hook",
        "28:_hook",
        "29:_hook",
        "31:_hook",
        "32:_hook",
        "33:_hook",
        "34:_hook",
        "35:_LIMIT",
        "36:_hook",
        "36:_hook",
        "40:_hook",
        "42:_hook",
    ]


def test_the_bare_read_check_leaves_a_function_its_own_names() -> None:
    guarded = frozenset({"data_home", "_LIMIT", "_WarmMintRuntime"})
    clean = "\n".join(
        [
            "from __future__ import annotations",
            "from typing import TYPE_CHECKING",
            "if TYPE_CHECKING:",
            "    from kiro_crew.connections.warm import _WarmMintRuntime",
            "def own(data_home):",
            "    return data_home()",
            "def typed(runtime: _WarmMintRuntime) -> _WarmMintRuntime:",
            "    return runtime",
            "def late():",
            "    return _warm_facade().data_home() / _warm_facade()._LIMIT",
            "def _LIMIT():",
            "    return [data_home for data_home in ()]",
            "def closure():",
            "    data_home = 1",
            "    def inner():",
            "        return data_home",
            "    return inner",
            "_TABLE = {data_home: 1 for data_home in ()}",
        ]
    )
    assert bare_reads(clean, guarded) == []
    assert late_reads(clean) == [(10, "data_home"), (10, "_LIMIT")]


def test_a_facade_patch_reaches_a_name_the_facade_only_imports(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Executed, not only parsed: a substitution on the facade is what the moved code runs."""
    generation_tree = importlib.import_module("kiro_crew.connections.warm_runtime.generation_tree")
    spec_body = importlib.import_module("kiro_crew.connections.warm_runtime.spec_body")

    monkeypatch.setattr(warm, "data_home", lambda: tmp_path)
    assert generation_tree._warm_work_dir() == tmp_path / "run" / "connections-warm"

    asked: list[str] = []

    def _declared() -> dict[str, dict[str, str]]:
        asked.append("declared")
        return {}

    monkeypatch.setattr(warm, "declared_tool_aliases", _declared)
    assert spec_body.connections_tool_aliases(["alpha"]) == {}
    assert asked == ["declared"]


@pytest.mark.asyncio
async def test_a_facade_patch_reaches_a_thread_handoff_a_watcher_and_a_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three late reads in one adoption: the liveness verdict the row is judged by, the grant
    watcher scheduled as a task, and the audit event handed to ``asyncio.to_thread``."""
    table_flow = importlib.import_module("kiro_crew.connections.warm_runtime.table_flow")
    watched: list[tuple[str, str, str]] = []
    logged: list[tuple[Any, ...]] = []

    async def _watcher(slug: str, mcp_url: str, token: str) -> None:
        watched.append((slug, mcp_url, token))

    monkeypatch.setattr(warm, "_warm_row_alive", lambda entry: True)
    monkeypatch.setattr(warm, "_mint_watcher", _watcher)
    monkeypatch.setattr(warm, "_log_warm_event", lambda *args: logged.append(args))
    _mints["linear"] = {  # type: ignore[typeddict-item]
        "state": "waiting",
        "shared": True,
        "oauth_url": "https://l/consent",
        "generation": 4,
        "activation": 2,
        "token": "premint",
    }

    token = await table_flow._adopt_shared_row("linear", "https://linear.example/mcp")
    watcher = _mints["linear"]["watcher"]
    await asyncio.wait_for(watcher, timeout=30)

    assert token is not None and token != "premint"
    assert watched == [("linear", "https://linear.example/mcp", token)]
    assert logged == [("connections_warm_mint_adopt", "provider:linear")]


@pytest.mark.asyncio
async def test_a_facade_patch_reaches_a_tunable_and_the_runtime_singleton(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A constant read late (the kill timeout) and the singleton read late (the re-drain)."""
    mint_process = importlib.import_module("kiro_crew.connections.warm_runtime.mint_process")
    table_flow = importlib.import_module("kiro_crew.connections.warm_runtime.table_flow")

    class _Unkillable:
        async def kill(self, **kwargs: Any) -> None:
            await asyncio.get_running_loop().create_future()  # never resolved; the timeout ends it

    monkeypatch.setattr(warm, "_WARM_KILL_TIMEOUT_SECONDS", 0)
    assert await asyncio.wait_for(mint_process._kill_quietly(_Unkillable()), timeout=30) is False

    asked: list[str] = []

    class _Runtime:
        async def redrain_for(self, slug: str) -> None:
            asked.append(slug)

    # Undone inside the test: the autouse singleton reset reads the real runtime on teardown.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(warm, "_warm_mint", _Runtime())
        assert await table_flow.adopt_shared_mint("linear", "https://linear.example/mcp") is None
    assert asked == ["linear"]


@pytest.mark.asyncio
async def test_a_facade_patch_reaches_a_coroutine_scheduled_as_a_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rearms = importlib.import_module("kiro_crew.connections.warm_runtime.rearms")
    started: list[str] = []

    async def _rearm(slug: str) -> list[str]:
        started.append(slug)
        return [slug]

    monkeypatch.setattr(warm, "_rearm_invalidated_provider", _rearm)
    # A bare-read regression would run the real coroutine; an empty scan keeps it from spawning.
    monkeypatch.setattr(warm, "_audited_mintable_providers", lambda: ([], True))
    async with connections_warm_helpers._rearm_registry() as registry:
        rearms.rearm_invalidated_provider("vercel")
        task = registry["vercel"]
        assert await asyncio.wait_for(task, timeout=30) == ["vercel"]
    assert started == ["vercel"]


def test_the_test_tree_walk_reaches_this_file() -> None:
    walked = {path.resolve() for path in _test_tree_files()}
    assert _THIS_FILE in walked
    assert (source_corpus.repo_root() / "test" / "test_connections_warm.py").resolve() in walked


def test_leaf_test_scope_derives_this_file_as_a_corpus_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It reads the whole test tree, so a leaf-only diff must still run it."""
    root = source_corpus.repo_root()
    monkeypatch.syspath_prepend(str(root / "scripts"))
    leaf_test_scope = importlib.import_module("leaf_test_scope")
    assert _THIS_FILE.relative_to(root).as_posix() in leaf_test_scope.corpus_gates(root)
