"""Derive the exported metric schema from the code, for ``schema_manifest.json``.

``src/kiro_crew/metrics/schema_manifest.json`` is the checked-in contract for every
metric a product destination may receive (``PRODUCT_METRICS``). This module builds
the same document from the source tree, so ``test_metrics_schema.py`` can compare
the two and a contributor can regenerate the file instead of typing it:

    python test/metrics_schema_helpers.py --write

Where each field comes from:

* ``kind`` -- the resolver ``test_product_metrics.py`` already uses (a declared
  observable gauge, else a counter; histograms and up/down counters are not
  exportable and fail there first).
* ``scope`` -- ``BEHAVIORAL_SCOPE`` for a counter in ``BEHAVIORAL_METRICS``, else
  the provider's ``_SCOPE``: the same split ``MetricsRecorder.counter`` makes.
* ``temporality`` -- ``temporality.delta_preference()`` for the instrument class,
  else the SDK's cumulative default.
* ``unit`` -- the ``unit=`` argument at the emit site. A wrapper such as
  ``events.emit_counter`` passes none, so ``MetricsRecorder.counter``'s own default
  applies; ``test_metrics_schema.py`` pins that the wrappers really pass none.
* ``attributes`` -- the keys of the attribute dict at every emit site (an AST scan
  of ``src/kiro_crew``), reshaped by ``behavioral_attributes`` for a behavioral
  metric. A key's ``values`` are listed when every site closes it in code: a
  literal, a module constant, ``bool(...)``, ``x if x in TUPLE else OTHER``, or a
  helper whose every ``return`` is one of those. A value the scan cannot close
  (a label function over a catalog, a bucket built in a loop) gets no ``values``:
  the key is pinned and the value set is not.

Nothing here guesses. A name in ``PRODUCT_METRICS`` with no emit site the scan can
read raises :class:`SchemaExtractionError` naming it, so a row is never written
from nothing.
"""

from __future__ import annotations

import ast
import functools
import importlib
import inspect
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Tuple

# is_additive is re-exported: the tests call schema.is_additive.
from kiro_crew.metrics.schema_manifest import (  # noqa: F401
    FIXED_FIELDS,
    is_additive,
    manifest_rows,
    value_tokens,
)

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
PACKAGE = SRC / "kiro_crew"
MANIFEST_PATH = PACKAGE / "metrics" / "schema_manifest.json"

#: Modules that declare instrument names as module constants. The same list
#: ``test_product_metrics.py`` resolves names against.
DECLARING_MODULES = (
    "kiro_crew.metrics.events",
    "kiro_crew.metrics.sessions",
    "kiro_crew.metrics.turns",
    "kiro_crew.metrics.turn_completed",
    "kiro_crew.metrics.monitor_loops",
    "kiro_crew.metrics.crew_log_export",
    "kiro_crew.metrics.session_control_calls",
    "kiro_crew.metrics.work",
    "kiro_crew.metrics.crew_mode",
    "kiro_crew.metrics.feature_usage",
    "kiro_crew.metrics.voice",
    "kiro_crew.metrics.http_metrics",
    "kiro_crew.metrics.db_metrics",
    "kiro_crew.metrics.tool_calls",
    "kiro_crew.metrics.inventory_gauges",
    "kiro_crew.metrics.process_gauges",
    "kiro_crew.metrics.chat_runtime_share",
    "kiro_crew.milestones",
)

#: Emit wrappers that take ``(name, attrs)`` and pass no ``unit``. Pinned by
#: ``test_metrics_schema.py::test_emit_wrappers_pass_no_unit``.
UNITLESS_WRAPPERS = (
    ("kiro_crew.metrics.events", "emit_counter"),
    ("kiro_crew.metrics.crew_log_export", "_record_metrics"),
)

#: ``milestones._emit`` runs every attribute through ``closed_attrs`` before the
#: recorder sees it, so the values a milestone counter can carry are that
#: function's allowed lists, not whatever an op wrote into the event tuple.
MILESTONES_MODULE = "kiro_crew.milestones"

_FALLBACK_UNIT = (
    inspect.signature(importlib.import_module("kiro_crew.metrics.recorder").MetricsRecorder.counter)
    .parameters["unit"]
    .default
)

#: How deep a helper call is followed when closing a value.
_MAX_DEPTH = 4


class SchemaExtractionError(AssertionError):
    """The code-side schema could not be read for a named metric."""


# -- names, kinds, scopes -----------------------------------------------------


def declared_names() -> Dict[str, Tuple[str, str]]:
    """Every declared instrument name -> (declaring module, constant)."""
    found: Dict[str, Tuple[str, str]] = {}
    for module_name in DECLARING_MODULES:
        module = importlib.import_module(module_name)
        for attr, value in vars(module).items():
            if attr.startswith("_") or not isinstance(value, str):
                continue
            if value.startswith("kirocrew."):
                found.setdefault(value, (module_name, attr))
    return found


def gauge_names() -> FrozenSet[str]:
    from kiro_crew.metrics import chat_runtime_share as crs
    from kiro_crew.metrics import inventory_gauges as ig
    from kiro_crew.metrics import process_gauges as pg

    return (
        frozenset(ig.ALL_METRIC_NAMES)
        | frozenset(pg.ALL_METRIC_NAMES)
        | frozenset({crs.GAUGE_PROCESSES, crs.GAUGE_TENANTS})
    )


def _temporality(kind: str) -> str:
    from opentelemetry.sdk.metrics import Counter, ObservableGauge
    from opentelemetry.sdk.metrics.export import AggregationTemporality

    from kiro_crew.metrics.temporality import delta_preference

    sdk_class = {"counter": Counter, "observable_gauge": ObservableGauge}[kind]
    chosen = delta_preference().get(sdk_class, AggregationTemporality.CUMULATIVE)
    return str(chosen.name).lower()


# -- source access ------------------------------------------------------------


@functools.lru_cache(maxsize=None)
def _parse(path: str) -> Tuple[str, ast.Module]:
    text = Path(path).read_text(encoding="utf-8")
    return text, ast.parse(text)


def _module_name(path: Path) -> str:
    rel = path.relative_to(SRC).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


@functools.lru_cache(maxsize=None)
def _module(name: str) -> ModuleType:
    return importlib.import_module(name)


def _rel(path: str) -> str:
    return Path(path).resolve().relative_to(ROOT).as_posix()


# -- value resolution ---------------------------------------------------------

#: An unresolvable value set.
OPEN = None


class _Ctx:
    """Where an expression is evaluated: a module namespace plus bound names."""

    def __init__(
        self,
        namespace: Dict[str, Any],
        func: Optional[ast.AST] = None,
        bindings: Optional[Dict[str, Tuple[ast.expr, "_Ctx"]]] = None,
        depth: int = 0,
    ) -> None:
        self.namespace = namespace
        self.func = func
        self.bindings = bindings or {}
        self.depth = depth
        self._scanned = False
        self._locals: Dict[str, List[ast.expr]] = {}
        self._imports: Dict[str, Any] = {}
        self._other: set = set()

    def _scan(self) -> None:
        if self._scanned or self.func is None:
            return
        self._scanned = True
        for node in _walk_own(self.func):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        self._locals.setdefault(target.id, []).append(node.value)
                    else:
                        self._other.update(_store_names(target))
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                if node.value is not None:
                    self._locals.setdefault(node.target.id, []).append(node.value)
            elif isinstance(node, (ast.AugAssign, ast.For, ast.AsyncFor, ast.comprehension)):
                self._other.update(_store_names(node.target))
            elif isinstance(node, (ast.With, ast.AsyncWith)):
                for item in node.items:
                    if item.optional_vars is not None:
                        self._other.update(_store_names(item.optional_vars))
            elif isinstance(node, ast.NamedExpr):
                self._other.add(node.target.id)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                # A function-local import, the lazy-import idiom the metrics
                # modules use to stay out of the provider's import cycle.
                module = _module(node.module)
                for alias in node.names:
                    self._imports[alias.asname or alias.name] = getattr(
                        module, alias.name, _MISSING
                    )
        for name in self._other:
            self._locals.pop(name, None)

    def local_import(self, name: str) -> Any:
        """The object a function-local ``from X import name`` binds, else ``_MISSING``."""
        self._scan()
        return self._imports.get(name, _MISSING)

    def local_assignments(self, name: str) -> Optional[List[ast.expr]]:
        """Every value *name* is assigned in the enclosing function, or None.

        None when *name* is bound some other way (a loop target, an unpacking,
        an augmented assignment), because then the plain assignments are not
        the whole story.
        """
        self._scan()
        if name in self._other:
            return None
        return self._locals.get(name, [])


def _store_names(target: ast.AST) -> Iterable[str]:
    for node in ast.walk(target):
        if isinstance(node, ast.Name):
            yield node.id


def _walk_own(func: ast.AST) -> Iterable[ast.AST]:
    """Nodes of *func*'s body, not descending into nested functions or lambdas."""
    stack = list(ast.iter_child_nodes(func))
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue
        stack.extend(ast.iter_child_nodes(node))


def _params(func: ast.AST) -> List[str]:
    args = func.args  # type: ignore[attr-defined]
    return [a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)]


def _is_scalar(value: Any) -> bool:
    return isinstance(value, (str, bool, int, float))


def _collection(expr: ast.expr, ctx: _Ctx) -> Optional[FrozenSet[Any]]:
    """The members of a constant str collection *expr* names, or None."""
    obj = _static_object(expr, ctx)
    if isinstance(obj, (tuple, list, set, frozenset)) and obj and all(_is_scalar(v) for v in obj):
        return frozenset(obj)
    if isinstance(obj, dict) and obj and all(isinstance(k, str) for k in obj):
        return frozenset(obj)
    if isinstance(expr, (ast.Tuple, ast.List, ast.Set)):
        out: set = set()
        for elt in expr.elts:
            vals = resolve(elt, ctx)
            if vals is OPEN:
                return None
            out |= vals
        return frozenset(out)
    return None


def _static_object(expr: ast.expr, ctx: _Ctx) -> Any:
    """The module-level object *expr* names (a Name or ``module.attr``), else a sentinel."""
    if isinstance(expr, ast.Name):
        if expr.id in ctx.bindings:
            bound, bound_ctx = ctx.bindings[expr.id]
            return _static_object(bound, bound_ctx)
        if ctx.func is not None and expr.id in _params(ctx.func):
            return _MISSING
        imported = ctx.local_import(expr.id)
        if imported is not _MISSING:
            return imported
        assigned = ctx.local_assignments(expr.id)
        if assigned:
            return _MISSING
        return ctx.namespace.get(expr.id, _MISSING)
    if isinstance(expr, ast.Attribute):
        base = _static_object(expr.value, ctx)
        if isinstance(base, ModuleType):
            return getattr(base, expr.attr, _MISSING)
    return _MISSING


_MISSING = object()


def _membership_guard(test: ast.expr, subject: ast.expr, ctx: _Ctx) -> Optional[FrozenSet[Any]]:
    """The collection *test* proves *subject* is in (``subject in X``), or None."""
    tests = test.values if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.And) else [test]
    want = ast.dump(subject)
    for part in tests:
        if (
            isinstance(part, ast.Compare)
            and len(part.ops) == 1
            and isinstance(part.ops[0], ast.In)
            and ast.dump(part.left) == want
        ):
            members = _collection(part.comparators[0], ctx)
            if members is not None:
                return members
    return None


def resolve(expr: ast.expr, ctx: _Ctx) -> Optional[FrozenSet[Any]]:
    """Every value *expr* can take, or ``OPEN`` when the code does not close it."""
    if ctx.depth > _MAX_DEPTH:
        return OPEN
    if isinstance(expr, ast.Constant):
        return frozenset({expr.value}) if _is_scalar(expr.value) else OPEN
    if isinstance(expr, ast.Name):
        if expr.id in ctx.bindings:
            bound, bound_ctx = ctx.bindings[expr.id]
            return resolve(bound, bound_ctx)
        if ctx.func is not None and expr.id in _params(ctx.func):
            return OPEN
        imported = ctx.local_import(expr.id)
        if imported is not _MISSING:
            return frozenset({imported}) if _is_scalar(imported) else OPEN
        assigned = ctx.local_assignments(expr.id)
        if assigned is None:
            return OPEN
        if assigned:
            return _union(resolve(v, ctx) for v in assigned)
        obj = ctx.namespace.get(expr.id, _MISSING)
        return frozenset({obj}) if _is_scalar(obj) else OPEN
    if isinstance(expr, ast.Attribute):
        obj = _static_object(expr, ctx)
        return frozenset({obj}) if _is_scalar(obj) else OPEN
    if isinstance(expr, ast.IfExp):
        body = resolve(expr.body, ctx)
        if body is OPEN:
            body = _membership_guard(expr.test, expr.body, ctx)
        if body is OPEN:
            return OPEN
        orelse = resolve(expr.orelse, ctx)
        return OPEN if orelse is OPEN else body | orelse
    if isinstance(expr, (ast.Compare, ast.UnaryOp)) and (
        isinstance(expr, ast.Compare) or isinstance(expr.op, ast.Not)
    ):
        return frozenset({False, True})
    if isinstance(expr, ast.Call):
        return _resolve_call(expr, ctx)
    return OPEN


def _union(parts: Iterable[Optional[FrozenSet[Any]]]) -> Optional[FrozenSet[Any]]:
    out: set = set()
    for part in parts:
        if part is OPEN:
            return OPEN
        out |= part
    return frozenset(out)


def _resolve_call(call: ast.Call, ctx: _Ctx) -> Optional[FrozenSet[Any]]:
    if isinstance(call.func, ast.Name) and call.func.id == "bool":
        return frozenset({False, True})
    # MAPPING.get(key, default) over a constant str -> str mapping.
    if isinstance(call.func, ast.Attribute) and call.func.attr == "get":
        mapping = _static_object(call.func.value, ctx)
        if isinstance(mapping, dict) and mapping and all(_is_scalar(v) for v in mapping.values()):
            values = frozenset(mapping.values())
            if len(call.args) < 2:
                return OPEN  # .get() without a default can return None
            default = resolve(call.args[1], ctx)
            return OPEN if default is OPEN else values | default
        return OPEN
    target = _static_object(call.func, ctx)
    if not inspect.isfunction(target):
        return OPEN
    func_node, func_ctx = _function_context(target, call, ctx)
    if func_node is None:
        return OPEN
    returns = [n.value for n in _walk_own(func_node) if isinstance(n, ast.Return)]
    if not returns or any(r is None for r in returns):
        return OPEN
    return _union(resolve(r, func_ctx) for r in returns if r is not None)


def _function_context(target: Any, call: ast.Call, caller: _Ctx) -> Tuple[Optional[ast.AST], _Ctx]:
    """*target*'s def node and a context binding its parameters to *call*'s arguments."""
    try:
        path = inspect.getsourcefile(target)
    except TypeError:
        path = None
    if not path or "<" in path:
        return None, caller
    _, tree = _parse(path)
    node = next(
        (
            n
            for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == target.__name__
        ),
        None,
    )
    if node is None:
        return None, caller
    if any(isinstance(a, ast.Starred) for a in call.args) or any(
        k.arg is None for k in call.keywords
    ):
        return None, caller
    namespace = vars(sys.modules[target.__module__])
    callee = _Ctx(namespace, node, depth=caller.depth + 1)
    positional = [a.arg for a in (*node.args.posonlyargs, *node.args.args)]
    bindings: Dict[str, Tuple[ast.expr, _Ctx]] = {}
    for name, arg in zip(positional, call.args):
        bindings[name] = (arg, caller)
    for kw in call.keywords:
        bindings[kw.arg] = (kw.value, caller)  # type: ignore[index]
    # Defaults for anything the call left out.
    defaults = node.args.defaults
    for name, default in zip(positional[len(positional) - len(defaults) :], defaults):
        bindings.setdefault(name, (default, callee))
    for arg, default in zip(node.args.kwonlyargs, node.args.kw_defaults):
        if default is not None:
            bindings.setdefault(arg.arg, (default, callee))
    callee.bindings = bindings
    return node, callee


def _resolve_dict(expr: ast.expr, ctx: _Ctx) -> Optional[Dict[str, Optional[FrozenSet[Any]]]]:
    """Attribute key -> value set for an attribute-dict expression, or None."""
    if ctx.depth > _MAX_DEPTH:
        return None
    if isinstance(expr, ast.Dict):
        out: Dict[str, Optional[FrozenSet[Any]]] = {}
        for key, value in zip(expr.keys, expr.values):
            if key is None:
                spread = _resolve_dict(value, ctx)
                if spread is None:
                    return None
                out.update(spread)
                continue
            names = resolve(key, ctx)
            if names is OPEN or len(names) != 1:
                return None
            (name,) = names
            if not isinstance(name, str):
                return None
            out[name] = resolve(value, ctx)
        return out
    if isinstance(expr, ast.Name):
        if expr.id in ctx.bindings:
            bound, bound_ctx = ctx.bindings[expr.id]
            return _resolve_dict(bound, bound_ctx)
        assigned = ctx.local_assignments(expr.id)
        if not assigned or len(assigned) != 1:
            return None
        return _resolve_dict(assigned[0], ctx)
    if isinstance(expr, ast.Call):
        if isinstance(expr.func, ast.Name) and expr.func.id == "dict" and not expr.args:
            return _resolve_dict(ast.Dict(keys=[], values=[]), ctx) if not expr.keywords else None
        target = _static_object(expr.func, ctx)
        if not inspect.isfunction(target):
            return None
        func_node, func_ctx = _function_context(target, expr, ctx)
        if func_node is None:
            return None
        returns = [n.value for n in _walk_own(func_node) if isinstance(n, ast.Return)]
        if len(returns) != 1 or returns[0] is None:
            return None
        return _resolve_dict(returns[0], func_ctx)
    return None


# -- emit sites -----------------------------------------------------------------


class EmitSite:
    """One place in the source that records a point of a product metric."""

    def __init__(
        self,
        name: str,
        where: str,
        unit: Optional[str],
        attributes: Optional[Dict[str, Optional[FrozenSet[Any]]]],
        module: str,
    ) -> None:
        self.name = name
        self.where = where
        self.unit = unit
        self.attributes = attributes
        self.module = module


def _enclosing_functions(tree: ast.Module) -> Dict[int, ast.AST]:
    """id(node) -> the innermost function that contains it."""
    owner: Dict[int, ast.AST] = {}

    def visit(node: ast.AST, func: Optional[ast.AST]) -> None:
        for child in ast.iter_child_nodes(node):
            inner = (
                child
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
                else func
            )
            if func is not None:
                owner[id(child)] = func
            visit(child, inner)

    visit(tree, None)
    return owner


def _imported_names(tree: ast.Module) -> Dict[str, Tuple[str, str]]:
    """Local name -> (module, attribute) for every ``from X import Y`` in the file."""
    out: Dict[str, Tuple[str, str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            for alias in node.names:
                out[alias.asname or alias.name] = (node.module, alias.name)
    return out


def _indirect_getters(tree: ast.Module, constants: Dict[str, str]) -> Dict[str, str]:
    """``getter=lambda: CONSTANT`` keywords: getter name -> metric name."""
    out: Dict[str, str] = {}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.keyword)
            and node.arg
            and isinstance(node.value, ast.Lambda)
            and isinstance(node.value.body, ast.Name)
            and node.value.body.id in constants
        ):
            out[node.arg] = constants[node.value.body.id]
    return out


def _gauge_attributes(call: ast.Call, ctx: _Ctx) -> Optional[Dict[str, Optional[FrozenSet[Any]]]]:
    """Attributes of an observable gauge, read from its callback factory's ``Observation``."""
    callbacks = next((k.value for k in call.keywords if k.arg == "callbacks"), None)
    if not isinstance(callbacks, ast.List) or len(callbacks.elts) != 1:
        return None
    factory_call = callbacks.elts[0]
    if not isinstance(factory_call, ast.Call):
        return None
    target = _static_object(factory_call.func, ctx)
    if not inspect.isfunction(target):
        return None
    func_node, func_ctx = _function_context(target, factory_call, ctx)
    if func_node is None:
        return None
    shapes: List[Dict[str, Optional[FrozenSet[Any]]]] = []
    for node in ast.walk(func_node):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "Observation":
            attrs = next((k.value for k in node.keywords if k.arg == "attributes"), None)
            if attrs is None and len(node.args) > 1:
                attrs = node.args[1]
            if attrs is None:
                shapes.append({})
                continue
            owner = _enclosing_functions(_parse(inspect.getsourcefile(target) or "")[1]).get(
                id(node)
            )
            inner = _Ctx(func_ctx.namespace, owner, func_ctx.bindings, func_ctx.depth)
            shape = _resolve_dict(attrs, inner)
            if shape is None:
                return None
            shapes.append(shape)
    if not shapes:
        return None
    return _merge_shapes(shapes)


def _merge_shapes(
    shapes: List[Dict[str, Optional[FrozenSet[Any]]]],
) -> Dict[str, Optional[FrozenSet[Any]]]:
    merged: Dict[str, Optional[FrozenSet[Any]]] = {}
    for shape in shapes:
        for key, values in shape.items():
            if key not in merged:
                merged[key] = values
            elif merged[key] is OPEN or values is OPEN:
                merged[key] = OPEN
            else:
                merged[key] = merged[key] | values  # type: ignore[operator]
    return merged


def emit_sites(names: Iterable[str]) -> Dict[str, List[EmitSite]]:
    """Every emit site the scan finds for each of *names*."""
    wanted = set(names)
    declared = declared_names()
    by_constant = {
        (module, const): name for name, (module, const) in declared.items() if name in wanted
    }
    sites: Dict[str, List[EmitSite]] = {name: [] for name in wanted}
    files: List[Tuple[Path, str, Dict[str, str]]] = []
    getters: Dict[str, str] = {}
    texts = {path: path.read_text(encoding="utf-8") for path in sorted(PACKAGE.rglob("*.py"))}
    for path, text in texts.items():
        module_name = _module_name(path)
        constants: Dict[str, str] = {}
        if any(const in text for (_, const) in by_constant):
            tree = _parse(str(path))[1]
            imported = _imported_names(tree)
            for (decl_module, const), name in by_constant.items():
                if decl_module == module_name:
                    constants[const] = name
                for local, origin in imported.items():
                    if origin == (decl_module, const):
                        constants[local] = name
            # ``getter=lambda: CONSTANT`` hands the name to a module that never
            # imports it (``session_cleanup`` reads its idle-expiry metric name
            # through a dependency bundle); the call site is matched by getter.
            getters.update(_indirect_getters(tree, constants))
        files.append((path, module_name, constants))
    for path, module_name, constants in files:
        if not constants and not any(g in texts[path] for g in getters):
            continue
        tree = _parse(str(path))[1]
        owners = _enclosing_functions(tree)
        namespace: Optional[Dict[str, Any]] = None
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                head = node.args[0] if node.args else None
                rest = node.args[1:]
            elif isinstance(node, ast.Tuple) and len(node.elts) == 2:
                head, rest = node.elts[0], node.elts[1:]
            else:
                continue
            metric = None
            if isinstance(head, ast.Name) and head.id in constants:
                metric = constants[head.id]
            elif (
                isinstance(head, ast.Call)
                and isinstance(head.func, ast.Attribute)
                and head.func.attr in getters
            ):
                metric = getters[head.func.attr]
            if metric is None:
                continue
            if isinstance(node, ast.Call) and _is_declaration_only(node):
                continue
            if namespace is None:
                namespace = vars(_module(module_name))
            ctx = _Ctx(namespace, owners.get(id(node)))
            where = f"{_rel(str(path))}:{node.lineno}"
            if isinstance(node, ast.Call) and _call_name(node) == "create_observable_gauge":
                unit = _keyword_str(node, "unit", ctx)
                attrs = _gauge_attributes(node, ctx)
            elif isinstance(node, ast.Call):
                unit = _keyword_str(node, "unit", ctx) if _call_name(node) == "counter" else None
                attr_expr = next((k.value for k in node.keywords if k.arg == "attrs"), None)
                if attr_expr is None and rest:
                    attr_expr = rest[-1]
                attrs = {} if attr_expr is None else _resolve_dict(attr_expr, ctx)
            else:
                unit = None
                attrs = _resolve_dict(rest[-1], ctx)
            sites[metric].append(EmitSite(metric, where, unit, attrs, module_name))
    return sites


def _call_name(call: ast.Call) -> Optional[str]:
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _is_declaration_only(call: ast.Call) -> bool:
    """A call that only lists names (``frozenset((A, B))``), not one that records."""
    return _call_name(call) in {"frozenset", "set", "tuple", "list", "sorted", "len", "isinstance"}


def _keyword_str(call: ast.Call, name: str, ctx: _Ctx) -> Optional[str]:
    expr = next((k.value for k in call.keywords if k.arg == name), None)
    if expr is None:
        return None
    values = resolve(expr, ctx)
    if values is OPEN or len(values) != 1:
        raise SchemaExtractionError(f"{name}= is not a constant at line {call.lineno}")
    (value,) = values
    return str(value)


# -- the document -----------------------------------------------------------------


def _milestone_close(
    attrs: Dict[str, Optional[FrozenSet[Any]]],
) -> Dict[str, Optional[FrozenSet[Any]]]:
    """What ``milestones.closed_attrs`` lets through for each key."""
    milestones = _module(MILESTONES_MODULE)
    allowed = getattr(milestones, "_ATTR_VALUES")
    out: Dict[str, Optional[FrozenSet[Any]]] = {}
    for key in attrs:
        if key == "milestone":
            # Catalog and tier keys: a set that grows with the catalog.
            out[key] = OPEN
        else:
            out[key] = frozenset(allowed.get(key, ())) | {"other"}
    return out


def _reshape(
    name: str, attrs: Dict[str, Optional[FrozenSet[Any]]]
) -> Dict[str, Optional[FrozenSet[Any]]]:
    """Apply the recorder's behavioral reshape to a key -> values map."""
    from kiro_crew.metrics.behavioral_metrics import behavioral_attributes

    probes = {key: f"\0probe:{key}" for key in attrs}
    reshaped = behavioral_attributes(name, dict(probes))
    by_probe = {probe: key for key, probe in probes.items()}
    out: Dict[str, Optional[FrozenSet[Any]]] = {}
    for key, value in reshaped.items():
        if isinstance(value, str) and value in by_probe:
            out[key] = attrs[by_probe[value]]
        else:
            out[key] = frozenset({value})
    return out


def _sort_values(values: Iterable[Any]) -> List[Any]:
    return sorted(values, key=lambda v: (type(v).__name__, str(v)))


def build_rows(names: Optional[Iterable[str]] = None) -> Dict[str, Dict[str, Any]]:
    """name -> manifest row, derived from the code, for every product metric."""
    from kiro_crew.metrics import provider
    from kiro_crew.metrics.behavioral_metrics import BEHAVIORAL_METRICS, BEHAVIORAL_SCOPE
    from kiro_crew.metrics.product_metrics import PRODUCT_METRICS

    names = sorted(PRODUCT_METRICS if names is None else names)
    declared = declared_names()
    gauges = gauge_names()
    all_sites = emit_sites(names)
    rows: Dict[str, Dict[str, Any]] = {}
    problems: List[str] = []
    for name in names:
        if name not in declared:
            problems.append(f"{name}: no declaring constant")
            continue
        kind = "observable_gauge" if name in gauges else "counter"
        sites = all_sites.get(name, [])
        if not sites:
            problems.append(f"{name}: no emit site found under src/kiro_crew")
            continue
        unreadable = [s.where for s in sites if s.attributes is None]
        if unreadable:
            problems.append(f"{name}: attribute dict not readable at {', '.join(unreadable)}")
            continue
        units = {s.unit if s.unit is not None else _FALLBACK_UNIT for s in sites}
        if len(units) != 1:
            problems.append(f"{name}: emit sites disagree on unit: {sorted(units)}")
            continue
        shapes = [s.attributes for s in sites if s.attributes is not None]
        keysets = {tuple(sorted(shape)) for shape in shapes}
        if len(keysets) != 1:
            # Different sites send different keys: a key is part of the schema
            # when any site sends it.
            pass
        attrs = _merge_shapes(shapes)
        if any(s.module == MILESTONES_MODULE for s in sites):
            attrs = _milestone_close(attrs)
        scope = provider._SCOPE
        if kind == "counter" and name in BEHAVIORAL_METRICS:
            scope = BEHAVIORAL_SCOPE
            attrs = _reshape(name, attrs)
        rows[name] = {
            "name": name,
            "kind": kind,
            "unit": units.pop(),
            "scope": scope,
            "temporality": _temporality(kind),
            "status": "active",
            "attributes": {
                key: ({} if values is OPEN else {"values": _sort_values(values)})
                for key, values in sorted(attrs.items())
            },
        }
    if problems:
        raise SchemaExtractionError(
            "could not read the exported schema from the code:\n  " + "\n  ".join(problems)
        )
    return rows


MANIFEST_COMMENT = (
    "The exported metric schema: every metric in PRODUCT_METRICS, its kind, unit, "
    "instrumentation scope, temporality, attribute keys and closed values. Rows, "
    "keys and values are only ever added; see docs/guides/telemetry-otlp-export.md "
    "(Metric schema contract). Regenerate with: python test/metrics_schema_helpers.py --write"
)


def build_manifest(
    previous: Optional[Dict[str, Any]] = None,
    rows: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """The manifest document for the current code, keeping *previous*'s deprecations.

    A deprecated row, a deprecated key and a deprecated value stay in the file
    after the code stops sending them, so regenerating never drops one. An
    active row, key or value the code does not send is dropped, and
    ``is_additive`` then fails on it until it is marked deprecated by hand.
    """
    rows = {n: json.loads(json.dumps(r)) for n, r in (rows or build_rows()).items()}
    if previous:
        for old in previous.get("metrics", []):
            name = old["name"]
            if name not in rows:
                if old.get("status") == "deprecated":
                    rows[name] = old
                continue
            new_attrs = rows[name]["attributes"]
            for key, old_spec in old.get("attributes", {}).items():
                if key not in new_attrs:
                    if old_spec.get("status") == "deprecated":
                        new_attrs[key] = old_spec
                    continue
                gone = old_spec.get("deprecated_values")
                spec = new_attrs[key]
                if gone and "values" in spec:
                    spec["values"] = [v for v in spec["values"] if v not in gone]
                    spec["deprecated_values"] = gone
            rows[name]["attributes"] = dict(sorted(new_attrs.items()))
    return {"_comment": MANIFEST_COMMENT, "metrics": [rows[n] for n in sorted(rows)]}


# -- the two checks ------------------------------------------------------------


def check_code(document: Dict[str, Any], code_rows: Dict[str, Dict[str, Any]]) -> List[str]:
    """Where the manifest and the code disagree, one line per problem, naming the metric."""
    problems: List[str] = []
    manifest = manifest_rows(document)
    hint = "run: python test/metrics_schema_helpers.py --write"
    for name in sorted(set(code_rows) | set(manifest)):
        code, row = code_rows.get(name), manifest.get(name)
        if row is None:
            problems.append(f"{name}: exported by the code but has no manifest row ({hint})")
            continue
        if code is None:
            if row.get("status") != "deprecated":
                problems.append(
                    f"{name}: has an active manifest row but the code no longer exports it "
                    "(removed or renamed); mark the row deprecated instead"
                )
            continue
        if row.get("status") == "deprecated":
            problems.append(
                f"{name}: is deprecated in the manifest; a deprecated name is never reused"
            )
            continue
        for field in FIXED_FIELDS:
            if row.get(field) != code[field]:
                problems.append(
                    f"{name}: {field} is {code[field]!r} in the code but {row.get(field)!r} "
                    "in the manifest; a changed field needs a new metric name"
                )
        problems.extend(_check_keys(name, row["attributes"], code["attributes"], hint))
    return problems


def _check_keys(name: str, listed: Dict[str, Any], sent: Dict[str, Any], hint: str) -> List[str]:
    problems: List[str] = []
    for key in sorted(set(listed) | set(sent)):
        spec, code = listed.get(key), sent.get(key)
        where = f"{name}: attribute {key!r}"
        if spec is None:
            problems.append(f"{where} is sent by the code but not in the manifest ({hint})")
            continue
        if code is None:
            if spec.get("status") != "deprecated":
                problems.append(
                    f"{where} is in the manifest but the code no longer sends it; "
                    "mark the key deprecated instead"
                )
            continue
        if spec.get("status") == "deprecated":
            problems.append(f"{where} is deprecated; a deprecated key is never reused")
            continue
        listed_values, code_values = value_tokens(spec), value_tokens(code)
        if (listed_values is None) != (code_values is None):
            state = "closed" if code_values is not None else "open"
            problems.append(
                f"{where} has a {state} value set in the code but not in the manifest ({hint})"
            )
            continue
        if listed_values is None or code_values is None:
            continue
        retired = value_tokens(spec, "deprecated_values") or set()
        for value in sorted(code_values & retired):
            problems.append(f"{where} value {value} is deprecated; it is never reused")
        for value in sorted(code_values - listed_values - retired):
            problems.append(f"{where} value {value} is sent by the code but not listed ({hint})")
        for value in sorted(listed_values - code_values):
            problems.append(
                f"{where} value {value} is listed but the code can no longer send it; "
                "move it to deprecated_values instead"
            )
    return problems


def base_manifest() -> Tuple[Optional[Dict[str, Any]], str]:
    """The manifest at the base this change is measured against, and where it came from.

    The base is ``$METRICS_SCHEMA_BASE`` when set, else the merge base with
    ``origin/main``, else ``HEAD^1`` (in CI the checkout is the pull request's
    merge commit, whose first parent is the base branch). Returns ``(None,
    why)`` when there is no base to read, or the base has no manifest yet.
    """
    import os
    import subprocess

    def git(*args: str) -> Optional[str]:
        try:
            done = subprocess.run(
                ["git", *args],
                cwd=ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=60,  # flake-ok: a hang ceiling on a local git read, not a wait a result depends on
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return done.stdout if done.returncode == 0 else None

    candidates = [os.environ.get("METRICS_SCHEMA_BASE", "").strip()]
    merge_base = git("merge-base", "HEAD", "origin/main")
    candidates += [(merge_base or "").strip(), "HEAD^1"]
    rel = MANIFEST_PATH.relative_to(ROOT).as_posix()
    for ref in candidates:
        if not ref or git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}") is None:
            continue
        if not git("ls-tree", "--name-only", ref, "--", rel):
            return None, f"{rel} does not exist at {ref}; nothing to compare yet"
        text = git("show", f"{ref}:{rel}")
        if text is None:
            return None, f"{rel} exists at {ref} but git could not read it"
        return json.loads(text), ref
    return None, "no git base commit could be resolved"


def dump(document: Dict[str, Any]) -> str:
    return json.dumps(document, indent=2, ensure_ascii=True) + "\n"


def load_manifest(path: Path = MANIFEST_PATH) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def main(argv: List[str]) -> int:
    previous = load_manifest() if MANIFEST_PATH.exists() else None
    text = dump(build_manifest(previous))
    if "--write" in argv:
        MANIFEST_PATH.write_text(text, encoding="utf-8")
        print(f"wrote {MANIFEST_PATH.relative_to(ROOT).as_posix()}")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(SRC))
    raise SystemExit(main(sys.argv[1:]))
