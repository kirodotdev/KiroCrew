"""Static AST validation for dynamic-workflow scripts (GATE group B, static half).

A workflow script is LLM-authored code, so before the runner ``exec``s it we
statically reject the dangerous constructs and enforce the authoring shape:

* ``import`` / ``from ... import``            — B1 (no arbitrary modules; this also
                                                 blocks importing ``time``/``random``/
                                                 ``uuid`` → the static half of B3)
* references to ``eval``/``exec``/``compile``/``open``/``__import__``/...  — B1
* dunder attribute or name access (``().__class__``, ``__builtins__``)     — B2
* a pure-literal ``META`` dict + an ``async def workflow(ctx)`` entrypoint
* a script-size ceiling

This is only the *static* half of the sandbox. The runner provides the second
half: it ``exec``s the script with a restricted ``__builtins__`` containing only
``SAFE_BUILTINS`` (plus ``ctx``), which is what actually makes ``time``/``random``/
``uuid`` unreachable at run time (the dynamic half of B3) and blocks filesystem/
socket egress (B7).

Spec: ``docs/system-specs/modules/workflows.md``. Never relax a check here without
a matching update to the invariant tests (GATE group B in
``docs/dynamic-workflows/GATES.md``).
"""

from __future__ import annotations

import ast
import symtable
from dataclasses import dataclass, field
from typing import Any

# Max workflow script size in bytes — matches ``_MAX_SCRIPT_BYTES`` in the spec.
MAX_SCRIPT_BYTES = 262144

# The ONLY builtins a workflow script may use. Anything else callable must come
# from ``ctx`` or be defined in the script itself. The runner builds its
# restricted ``__builtins__`` from exactly this set.
# Exception types a workflow may reference in ``try/except`` / ``raise``. These are
# pure class objects — they grant no capability (no I/O, import, or introspection),
# so exposing them is safe, and ``try/except Exception`` is normal, harmless code a
# model naturally writes. Without these, a script using them dies at runtime with
# ``NameError: name 'Exception' is not defined`` (the sandbox's __builtins__ omits
# them). Keep this list to value/error builtins only — never callables that touch
# the host (those stay in FORBIDDEN_NAMES / are simply absent).
SAFE_EXCEPTIONS = frozenset(
    {
        "Exception",
        "BaseException",
        "ValueError",
        "TypeError",
        "KeyError",
        "IndexError",
        "RuntimeError",
        "StopIteration",
        "StopAsyncIteration",
        "ArithmeticError",
        "ZeroDivisionError",
        "AttributeError",
        "LookupError",
        "AssertionError",
        "NotImplementedError",
        "OverflowError",
        "RecursionError",
        "TimeoutError",
    }
)

SAFE_BUILTINS = frozenset(
    {
        "abs",
        "all",
        "any",
        "bool",
        "dict",
        "enumerate",
        "filter",
        "float",
        "frozenset",
        "int",
        "isinstance",
        "issubclass",
        "len",
        "list",
        "map",
        "max",
        "min",
        "range",
        "repr",
        "reversed",
        "round",
        "set",
        "sorted",
        "str",
        "sum",
        "tuple",
        "zip",
    }
    | SAFE_EXCEPTIONS
)

# Names that must never be referenced — escape vectors and introspection hooks.
FORBIDDEN_NAMES = frozenset(
    {
        "eval",
        "exec",
        "compile",
        "open",
        "input",
        "__import__",
        "__builtins__",
        "globals",
        "locals",
        "vars",
        "getattr",
        "setattr",
        "delattr",
        "breakpoint",
        "memoryview",
        "classmethod",
        "staticmethod",
        "super",
        "type",
    }
)

# Modules that especially must not be imported: determinism (B3) and egress (B7).
# All imports are rejected regardless; this set only sharpens the error message.
DETERMINISM_MODULES = frozenset({"time", "random", "uuid", "datetime", "secrets", "os"})

ENTRYPOINT = "workflow"
META_NAME = "META"

# ``ctx`` methods that are SYNCHRONOUS (return None / a plain value) and MUST NOT
# be awaited. Awaiting them raises ``TypeError: object NoneType can't be used in
# 'await' expression`` at run time — a whole class of authoring bug the AST
# sandbox otherwise waves through (``await ctx.phase(...)`` is structurally legal).
SYNC_CTX_METHODS = frozenset({"phase", "log", "nudge"})

# ``ctx`` methods whose result may legitimately be ``None`` (an agent that died or
# whose output failed schema validation returns None; a failed ``parallel`` thunk
# resolves to None). Directly subscripting / ``.get()``-ing / attribute-accessing
# the awaited result without a None-guard is the ``'NoneType' object has no
# attribute 'get'`` / ``not subscriptable`` class of runtime crash.
NULLABLE_CTX_METHODS = frozenset({"agent", "parallel", "pipeline", "workflow", "approve"})


@dataclass
class ValidationResult:
    """Outcome of validating a workflow script.

    ``ok`` is True only when ``errors`` is empty. ``meta`` is the extracted
    ``META`` dict when present and pure-literal, else None.
    """

    ok: bool
    errors: list[str] = field(default_factory=list)
    meta: dict[str, Any] | None = None


def _is_dunder(name: str) -> bool:
    return len(name) >= 5 and name.startswith("__") and name.endswith("__")


class _Validator(ast.NodeVisitor):
    """Walks the whole tree (including helper defs) collecting violations."""

    def __init__(self) -> None:
        self.errors: list[str] = []

    def visit_Import(self, node: ast.Import) -> None:  # noqa: N802 (ast.NodeVisitor API)
        hit = next(
            (a.name for a in node.names if a.name.split(".")[0] in DETERMINISM_MODULES), None
        )
        if hit is not None:
            self.errors.append(
                f"line {node.lineno}: 'import {hit}' is not allowed "
                "(no imports; determinism/egress)"
            )
        else:
            self.errors.append(f"line {node.lineno}: 'import' is not allowed in workflow scripts")

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:  # noqa: N802 (ast.NodeVisitor API)
        self.errors.append(
            f"line {node.lineno}: 'from ... import' is not allowed in workflow scripts"
        )

    def visit_Attribute(self, node: ast.Attribute) -> None:  # noqa: N802 (ast.NodeVisitor API)
        if _is_dunder(node.attr):
            self.errors.append(
                f"line {node.lineno}: dunder attribute access '.{node.attr}' is not allowed"
            )
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:  # noqa: N802 (ast.NodeVisitor API)
        if node.id in FORBIDDEN_NAMES or _is_dunder(node.id):
            self.errors.append(f"line {node.lineno}: use of '{node.id}' is not allowed")
        self.generic_visit(node)


# Names the runner injects into the script's globals besides SAFE_BUILTINS.
# ``ctx`` is the workflow context; ``args`` is NOT a global (it's ``ctx.args``),
# so it is intentionally absent here.
_INJECTED_GLOBALS = frozenset({"ctx"})


def _check_undefined_names(source: str, errors: list[str]) -> None:
    """Flag free names that would raise ``NameError`` at runtime (B3 runtime half,
    caught statically). ``validate`` is otherwise AST-only and cannot see that e.g.
    ``Exception`` or ``json`` is undefined in the sandbox namespace — so an agent
    script using one passes static checks then dies at run time. This resolves the
    script's scopes with ``symtable`` and reports any referenced global that is not
    a safe builtin, not ``ctx``, and not bound at module level (a helper def/assign).

    Conservative by design: only names the symbol table marks as *referenced
    globals* and *not assigned/imported anywhere* are flagged, so legitimate
    module-level helpers and locals never trip it.
    """
    try:
        top = symtable.symtable(source, "<workflow>", "exec")
    except (SyntaxError, ValueError):
        return  # syntax errors already reported by the AST parse path

    # Names bound at module level (defs, assignments) are legal references from
    # nested scopes — collect them so helper functions don't get flagged.
    # ``is_declared_global`` also catches names bound in the module namespace at
    # runtime but not flagged ``is_assigned`` on the top table: a walrus (``:=``)
    # target inside a module-level comprehension (PEP 572 binds it in the
    # enclosing scope) or a ``global`` name assigned from a nested def. The script
    # runs fine, so these must not be reported as undefined.
    module_bound = {
        s.get_name()
        for s in top.get_symbols()
        if s.is_assigned() or s.is_imported() or s.is_namespace() or s.is_declared_global()
    }
    # Only SAFE_BUILTINS are actually available at runtime (the sandbox strips the
    # rest of __builtins__), plus ctx and any module-level helper the script defines.
    allowed = SAFE_BUILTINS | _INJECTED_GLOBALS | module_bound

    flagged: set[str] = set()

    def _scan(table: "symtable.SymbolTable") -> None:
        for sym in table.get_symbols():
            name = sym.get_name()
            if (
                sym.is_referenced()
                and sym.is_global()
                and not sym.is_assigned()
                and name not in allowed
                and not _is_dunder(name)
                and name not in FORBIDDEN_NAMES  # already reported elsewhere
                and name not in flagged
            ):
                flagged.add(name)
        for child in table.get_children():
            _scan(child)

    _scan(top)
    for name in sorted(flagged):
        errors.append(
            f"undefined name '{name}' — not available in the workflow sandbox "
            f"(only ctx, safe builtins, and module-level helpers are; no imports)"
        )


def _is_ctx_call(node: ast.AST, methods: frozenset[str]) -> str | None:
    """If ``node`` is a call ``ctx.<m>(...)`` with ``<m>`` in ``methods``, return
    the method name; else None. Used to spot DSL-contract misuse structurally."""
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if (
        isinstance(func, ast.Attribute)
        and isinstance(func.value, ast.Name)
        and func.value.id == "ctx"
        and func.attr in methods
    ):
        return func.attr
    return None


class _DslContractVisitor(ast.NodeVisitor):
    """Catches two DSL-contract violations the sandbox otherwise permits — both
    observed shipping bad LLM-authored scripts:

    * ``await ctx.phase(...)`` / ``await ctx.log(...)`` — awaiting a SYNC ctx method
      (returns None) → ``TypeError: ... can't be used in 'await' expression``.
    * ``(await ctx.agent(...))[...]`` / ``.get(...)`` / ``.attr`` — dereferencing an
      awaited nullable-result directly, with no None-guard → ``'NoneType' object
      has no attribute 'get'`` / ``not subscriptable`` when the agent returns None.

    Both are structurally legal Python, so ``_Validator`` (security) and
    ``_check_undefined_names`` (name resolution) never flag them; only a contract-
    aware pass does. A dedicated type checker (mypy) would also catch them, but it
    is not importable in the runner's sandboxed runtime — so this deterministic AST
    lint is the enforceable gate, run at authoring time so bad scripts regenerate.
    """

    def __init__(self) -> None:
        self.errors: list[str] = []

    def visit_Await(self, node: ast.Await) -> None:  # noqa: N802 (ast.NodeVisitor API)
        sync = _is_ctx_call(node.value, SYNC_CTX_METHODS)
        if sync is not None:
            self.errors.append(
                f"line {node.lineno}: 'await ctx.{sync}(...)' is invalid — ctx.{sync}() "
                f"is synchronous (returns None); call it without 'await'"
            )
        self.generic_visit(node)

    def _flag_unguarded(self, inner: ast.AST, lineno: int, how: str) -> None:
        # ``inner`` is the expression being dereferenced. Flag when it is an
        # ``await ctx.<nullable>(...)`` directly (the result may be None). Binding
        # to a variable first (``r = await ctx.agent(...); if r: r.get(...)``) is
        # the correct pattern and is intentionally NOT flagged — we only catch the
        # inline, unguarded deref that repeatedly shipped broken.
        if isinstance(inner, ast.Await):
            m = _is_ctx_call(inner.value, NULLABLE_CTX_METHODS)
            if m is not None:
                self.errors.append(
                    f"line {lineno}: {how} on the result of 'await ctx.{m}(...)' without a "
                    f"None-guard — ctx.{m}() may return None (a dead agent or a result that "
                    f"failed schema validation); assign it first and guard, e.g. "
                    f"'r = await ctx.{m}(...); if r: ...'"
                )

    def visit_Subscript(self, node: ast.Subscript) -> None:  # noqa: N802 (ast.NodeVisitor API)
        self._flag_unguarded(node.value, node.lineno, "subscript '[...]'")
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:  # noqa: N802 (ast.NodeVisitor API)
        self._flag_unguarded(node.value, node.lineno, f"attribute '.{node.attr}'")
        self.generic_visit(node)


def _check_dsl_contract(tree: ast.Module, errors: list[str]) -> None:
    """Static half of the DSL-contract gate: awaiting sync ctx methods, and
    dereferencing nullable awaited ctx results without a None-guard."""
    visitor = _DslContractVisitor()
    visitor.visit(tree)
    errors.extend(visitor.errors)


def _extract_meta(tree: ast.Module, errors: list[str]) -> dict[str, Any] | None:
    for node in tree.body:
        # Accept both ``META = {...}`` (Assign) and ``META: dict = {...}``
        # (AnnAssign) — the annotated form parses to a different AST node but is
        # an equally valid module-level META definition.
        if isinstance(node, ast.Assign):
            if not any(isinstance(t, ast.Name) and t.id == META_NAME for t in node.targets):
                continue
            value_node: ast.expr | None = node.value
        elif isinstance(node, ast.AnnAssign):
            if not (isinstance(node.target, ast.Name) and node.target.id == META_NAME):
                continue
            value_node = node.value  # None for a bare ``META: dict`` declaration
        else:
            continue
        if value_node is None:
            errors.append(f"line {node.lineno}: {META_NAME} must be assigned a dict literal")
            return None
        try:
            value = ast.literal_eval(value_node)
        except (ValueError, SyntaxError, TypeError):
            errors.append(
                f"line {node.lineno}: {META_NAME} must be a pure literal "
                "(no calls, names, or comprehensions)"
            )
            return None
        if not isinstance(value, dict):
            errors.append(f"{META_NAME} must be a dict literal")
            return None
        return value
    errors.append(f"{META_NAME} dict is required at module level")
    return None


def _check_entrypoint(tree: ast.Module, errors: list[str]) -> None:
    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == ENTRYPOINT:
            # The first parameter may be positional-only (``def workflow(ctx, /)``),
            # in which case it lives in ``posonlyargs`` rather than ``args``. The
            # runner calls ``workflow(ctx)`` positionally, so either form binds.
            args = node.args.posonlyargs + node.args.args
            if not args or args[0].arg != "ctx":
                errors.append(f"'{ENTRYPOINT}' must take 'ctx' as its first parameter")
            return
        if isinstance(node, ast.FunctionDef) and node.name == ENTRYPOINT:
            errors.append(f"'{ENTRYPOINT}' must be 'async def', not 'def'")
            return
    errors.append(f"missing entrypoint: 'async def {ENTRYPOINT}(ctx)'")


def validate(source: str, *, max_bytes: int = MAX_SCRIPT_BYTES) -> ValidationResult:
    """Statically validate a workflow script's source.

    Returns a ``ValidationResult``; never raises on bad scripts (errors are
    collected). Only raises if ``source`` is not a string.
    """
    if len(source.encode("utf-8")) > max_bytes:
        return ValidationResult(ok=False, errors=[f"script exceeds max size of {max_bytes} bytes"])
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return ValidationResult(ok=False, errors=[f"syntax error: {exc}"])

    errors: list[str] = []
    visitor = _Validator()
    visitor.visit(tree)
    errors.extend(visitor.errors)

    meta = _extract_meta(tree, errors)
    _check_entrypoint(tree, errors)
    # Runtime-half of B3: reject names that would NameError in the sandbox
    # (e.g. an exception type or stdlib name the model used but the sandbox omits),
    # so authoring catches+regenerates before launch instead of failing mid-run.
    _check_undefined_names(source, errors)
    # DSL-contract half: reject awaiting sync ctx methods and unguarded None-deref
    # of awaited nullable ctx results — two authoring-bug classes that shipped
    # runtime crashes (``can't await NoneType``; ``'NoneType' has no attribute``).
    _check_dsl_contract(tree, errors)

    return ValidationResult(ok=not errors, errors=errors, meta=meta)
